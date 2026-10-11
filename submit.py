"""Phase 3b: enter the master's bid in the submission website's form and sign it.

The submitter starts before the bid is decided: in a tab of its own it opens the submission form and fills what the
plan already knows (the bidder's data), then waits at the first page that needs a value of the bid until set_bid().

Clef maps each form control to the bid field it stands for and each button to its purpose (apply a configuration,
continue, omit a position, request the TAN, submit), once per page layout; the filler then enters the master's
values, re-reading the page after every server round trip. Qwen steps in when a page does not advance (validation
messages), and the mail worker fetches the signing TAN.
"""
import asyncio
import json
import re
from decimal import Decimal

import llm
import pricing
from agents import Worker
from web import Blocked, Observation, Tab, match_option, normalize

FIELDS = {
    "company": "bidder company name", "vat_id": "bidder VAT identification number (USt-IdNr.)",
    "email": "bidder contact e-mail address",
    "offer_type": "kind of offer: main offer, alternative offer or declaration of non-participation",
    "bore": "configurator attribute: bore / piston diameter (mm)", "stroke": "configurator attribute: stroke (mm)",
    "pressure": "configurator attribute: operating pressure (bar)", "mounting": "configurator attribute: mounting",
    "certification": "configurator attribute: certification",
    "material": "catalog / material number of the offered article",
    "quantity": "offered quantity of this position in pieces",
    "unit_price": "final net unit price per piece (after discount)",
    "base_price": "base price per piece before discount", "surcharge": "surcharges per piece",
    "packaging": "packaging cost per piece", "rebate": "project bonus / rebate for the entire position",
    "lead_time": "delivery lead time in weeks", "discount": "discount percent of the offer",
    "total": "net total of the offer (not the gross amount)",
    "ack_docs": "acknowledgement that the tender documents are accepted",
    "own_terms": "applying the bidder's own terms and conditions (must stay unticked)",
    "tan": "signing TAN", "other": "anything else (search, address bar, notes, gross amount, unrelated)",
}
PURPOSES = {
    "proceed": "save this step and continue to the next step",
    "apply": "apply the selected configuration so dependent options load",
    "omit": "omit / skip this position (partial credit)", "request_tan": "request a signing TAN",
    "submit": "finally and bindingly submit the offer", "back": "go back, cancel or reset entries",
    "add": "add another position", "close": "close an overlay, survey or notice",
    "other": "anything else (navigation, logout, reload, unrelated)",
}
PAGE = {
    "form": "a step of the offer submission form (bidder data, offer type, a position, conditions, declarations, "
            "review and signature)",
    "receipt": "a confirmation that the offer was received (receipt / Eingangsbestätigung)",
    "start": "the tender's page or another page from which the offer submission must be started",
    "obstacle": "a login, cookie wall, popup, survey, error page or overlay",
}
CONFIGURATION = ("bore", "stroke", "pressure", "mounting", "certification", "material")
BIDDER = {"company", "vat_id", "email", "ack_docs", "own_terms", "other"}  # known before the bid is decided


def german(value, places=2) -> str:
    text = f"{Decimal(str(value)):,.{places}f}"
    return text.replace(",", "X").replace(".", ",").replace("X", ".")


def percent(value) -> str:
    number = Decimal(str(value))
    return str(int(number)) if number == number.to_integral() else german(number).rstrip("0")


def plain(value) -> str:
    """A configurator attribute as a form shows it: numbers without locale hints or trailing zeros."""
    number = pricing.number(value, kind=pricing.COUNT) if re.match(r"\s*\d", str(value)) else None
    if number is None:
        return str(value)
    return str(int(number)) if number == number.to_integral() else str(number.normalize())


class Submitter:
    def __init__(self, tab: Tab, mail: Worker | None, plan: dict, brief: str, log=print):
        self.tab, self.mail, self.log = tab, mail, log
        self.plan = plan
        self.answer: dict | None = None
        self.required: dict[str, dict] = {}
        self.all_positions: list[str] = []
        self.positions: dict[str, dict] = {}
        self.emails: list[str] = []
        self.ready = asyncio.Event()
        self.brief_submission = brief[brief.find("## Submission"):][:1500] if "## Submission" in brief else brief[-1500:]
        self.fields: dict[tuple, str] = {}       # (kind, label) -> bid field
        self.purposes: dict[tuple, str] = {}     # (kind, label) -> button purpose
        self.layouts: dict[str, str] = {}        # page layout -> page kind
        self.options: dict[tuple, str] = {}      # (field, value, options) -> chosen option
        self.asked_positions: dict[tuple, str] = {}  # (layout, headings) -> the position Clef named for that page
        self.tan_code: str | None = None     # the code read for the latest request
        self.tan_signed: str | None = None   # the code the form was last signed with
        self.tan_refused: set[str] = set()   # codes the form did not accept
        self.mailbox_before: set[str] = set()  # the mailbox's links before the latest request: what is new is the code
        self.gave_up = False                   # the form gives no way forward: stop trying
        self.tan_requested = False
        self.tan_requests = 0

    def set_bid(self, bid: dict, emails: list[str]):
        """The master's decided bid: the submitter continues past the pages that need it."""
        self.answer = bid["answer"]
        self.required = {str(d["position"]): d.get("required") or {} for d in bid.get("details", [])}
        self.all_positions = list(self.required) or [str(p["position"]) for p in self.answer["positions"]]
        self.positions = {str(p["position"]): p for p in self.answer["positions"]}
        self.emails = emails
        self.ready.set()

    # --------------------------------------------------------------- values
    @property
    def order(self) -> dict:
        bidder = self.plan.get("bidder") or {}
        values = {"company": bidder.get("company"), "vat_id": bidder.get("vat_id"),
                  "email": bidder.get("email") or (self.emails[0] if self.emails else None),
                  "ack_docs": True, "own_terms": False}
        if self.answer:
            values.update({"offer_type": self.answer["decision"], "discount": percent(self.answer["discount_percent"]),
                           "total": german(self.answer["total_net"])})
        return values

    def waits_for_bid(self, obs: Observation) -> bool:
        """Before the bid is decided, a page with any bid-dependent control (or an empty bidder field) must wait."""
        if self.ready.is_set():
            return False
        order = self.order
        for control in obs.controls.values():
            field = self.fields.get((control.kind, control.label))
            if control.fillable and field and field not in BIDDER:
                return True
            if control.fillable and field in BIDDER and field != "other" and order.get(field) in (None, ""):
                return True
            if self.purposes.get((control.kind, control.label)) in ("omit", "request_tan", "submit", "add"):
                return True
        return False

    def values_for(self, position: str | None) -> dict:
        values = dict(self.order)
        entry = self.positions.get(position) if position else None
        if entry:
            required = self.required.get(position, {})
            values.update({key: plain(required[key]) for key in CONFIGURATION[:5] if required.get(key) not in (None, "")})
            values.update({"material": entry["material_number"], "quantity": str(entry["quantity"]),
                           "unit_price": german(entry["unit_price"]), "lead_time": str(entry.get("lead_time_weeks", ""))})
            for key, name in (("base_unit_price", "base_price"), ("surcharge_per_unit", "surcharge"),
                              ("packaging_per_unit", "packaging"), ("rebate_total", "rebate")):
                if key in entry:
                    values[name] = german(entry[key])
        return {k: v for k, v in values.items() if v not in (None, "")}

    # -------------------------------------------------------------- mapping
    @staticmethod
    def layout(obs: Observation) -> str:
        return obs.url.split("?")[0] + "|" + "|".join(sorted({f"{c.kind}:{c.label}" for c in obs.controls.values()}))

    async def understand(self, obs: Observation) -> tuple[str, str | None]:
        """The page kind and its position; one Clef call per new layout maps its controls and buttons.

        A page names the item it asks for in a heading: when exactly one position is named there, that is the
        position, without a model call. Otherwise Clef decides, once per layout and set of headings."""
        def named(text):
            return [p for p in self.all_positions if re.search(rf"(?<![\d.]){re.escape(p)}(?![\d])", text)]
        found = named(obs.text)
        in_headings = named("\n".join(obs.headings))
        layout = self.layout(obs)
        asked = (layout, tuple(obs.headings))
        if len(in_headings) == 1:
            found = in_headings
        elif len(found) > 1 and asked in self.asked_positions:
            found = [self.asked_positions[asked]]
        questions, known = {}, 0
        for control in obs.controls.values():
            key = (control.kind, control.label)
            if len(questions) >= 60:
                break
            if control.kind in ("button", "submit", "reset"):
                known += key in self.purposes
                if key not in self.purposes:
                    questions[f"b{control.id}"] = llm.choice(f"What does the button {control.describe()[:200]} do?", PURPOSES)
            elif control.entry:  # also fields that are disabled for now: they are the same fields once enabled
                known += key in self.fields
                if key not in self.fields:
                    questions[f"f{control.id}"] = llm.choice(
                        f"Which bid field does the control {control.describe()[:220]} hold?", FIELDS)
        if layout not in self.layouts:
            same_address = [kind for other, kind in self.layouts.items() if other.split("|")[0] == layout.split("|")[0]]
            if known and not questions and same_address:
                # the same address showing only controls already understood: the same kind of page as before
                self.layouts[layout] = same_address[-1]
            else:
                questions["page"] = llm.choice("What is this page?", PAGE)
        if len(found) > 1:
            questions["position"] = llm.choice("Which tender position does this form page configure and price?",
                                               {p: f"tender position {p}" for p in found})
        if questions:
            answers = await llm.clef({"goal": "enter and sign the offer", "instructions": self.brief_submission,
                                      "page": obs.clip(6000)}, questions)
            for qid, answer in answers.items():
                if qid[0] in "bf" and qid[1:].isdigit():
                    control = obs.controls[int(qid[1:])]
                    (self.purposes if qid[0] == "b" else self.fields)[(control.kind, control.label)] = answer["choice"]
            if "page" in answers:
                self.layouts[layout] = answers["page"]["choice"]
            if "position" in answers:
                found = [answers["position"]["choice"]]
                self.asked_positions[asked] = found[0]
        return self.layouts.get(layout, "form"), (found[0] if len(found) == 1 else None)

    def controls_for(self, obs: Observation, field: str) -> list:
        return [c for c in obs.controls.values() if self.fields.get((c.kind, c.label)) == field]

    def button(self, obs: Observation, purpose: str):
        for control in obs.controls.values():
            if self.purposes.get((control.kind, control.label)) == purpose and not control.disabled:
                return control
        return None

    async def pick_option(self, control, field: str, value) -> str | None:
        options = [o for o in control.options or [] if o]
        key = (field, str(value), tuple(options))
        if key in self.options:
            return self.options[key]
        if field == "offer_type":
            want = "a regular main offer (decision bid)" if value == "bid" else "a declaration of non-participation (no bid)"
        else:
            chosen = match_option(value, options)
            if chosen or field in ("bore", "stroke", "pressure", "material", "lead_time"):
                self.options[key] = chosen
                return chosen
            want = f"{FIELDS[field]} = {value}"
        if len(options) < 2:
            return None
        answers = await llm.clef({"instructions": self.brief_submission, "field": FIELDS[field], "wanted": want},
                                 {"o": llm.choice(f"Which option means: {want}?", {str(i): o for i, o in enumerate(options)})})
        self.options[key] = options[int(answers["o"]["choice"])]
        return self.options[key]

    # ----------------------------------------------------------------- fill
    async def fill_selects(self, obs: Observation, values: dict) -> bool:
        """Set every select whose wanted option is offered now, in page order. A choice that makes the page load
        something ends the pass, because the page must be read again: True in that case."""
        for control in sorted(obs.controls.values(), key=lambda c: c.id):
            field = self.fields.get((control.kind, control.label))
            if control.kind == "select" and field in values and not control.disabled:
                option = await self.pick_option(control, field, values[field])
                if option and normalize(option) != normalize(control.value or ""):
                    before = self.tab.started
                    self.log(f"[submit] {field}: {await self.tab.select(control.id, option)}")
                    if self.tab.started != before:
                        return True
        return False

    async def fill_inputs(self, obs: Observation, values: dict) -> int:
        """Enter every text field and checkbox of this page in one pass (no round trip in between)."""
        changed = 0
        for control in sorted(obs.controls.values(), key=lambda c: c.id):
            field = self.fields.get((control.kind, control.label))
            if field not in values or control.disabled or control.kind == "select":
                continue
            if control.kind in ("checkbox", "radio", "switch"):
                if bool(control.checked) != bool(values[field]):
                    self.log(f"[submit] {field}: {await self.tab.check(control.id, bool(values[field]), strict=True)}")
                    changed += 1
            elif normalize(control.value or "") != normalize(values[field]):
                self.log(f"[submit] {field}: {await self.tab.fill(control.id, values[field])}")
                changed += 1
        return changed

    def cascade_pending(self, obs: Observation, values: dict) -> bool:
        """A configuration select whose value is known is still disabled or lacks the option: apply first."""
        for control in obs.controls.values():
            field = self.fields.get((control.kind, control.label))
            if control.kind == "select" and field in CONFIGURATION and field in values:
                if control.disabled or (field in ("bore", "stroke", "pressure", "material")
                                        and not match_option(values[field], control.options or [])):
                    return True
        return False

    # ------------------------------------------------------------------ run
    async def run(self, until: float, start_url: str = "") -> str | None:
        """Enter and sign the bid. A page that does not load, a slow server or a failed model call is no reason
        to end without an answer: the form is taken up again from the page as it is, until the time is used up
        or the form itself gives no way forward (no accepted signing code after three requests)."""
        while llm.clock() < until and not self.gave_up:
            try:
                receipt = await self._run(until, start_url)
                if receipt:
                    return receipt
            except Exception as error:
                self.log(f"[submit] {type(error).__name__}: {str(error)[:200]}; taking the form up again")
            await asyncio.sleep(1)
        return None

    async def _run(self, until: float, start_url: str) -> str | None:
        if self.tab.page is None or self.tab.page.is_closed():
            self.log(f"[submit] {await self.tab.open(start_url)}")
        elif not self.tab.page.url.startswith("http"):  # an earlier attempt to open the website got no answer
            self.log(f"[submit] {await self.tab.goto(start_url)}")
        last, repeats, failures = None, 0, 0
        for _ in range(120):
            if llm.clock() > until:
                self.log("[submit] out of time")
                return None
            try:
                obs = await asyncio.wait_for(self.tab.observe(), 20)
                if obs.password:
                    self.log(f"[submit] {await self.tab.login()}")
                    continue
                kind, position = await self.understand(obs)
            except Exception as error:  # a flaky page: observe again, but not forever
                failures += 1
                self.log(f"[submit] page not readable ({type(error).__name__}); {'giving up' if failures > 5 else 'again'}")
                if failures > 5:
                    return None
                await asyncio.sleep(1)
                continue
            if kind == "form" and self.waits_for_bid(obs):
                self.log("[submit] form ready; waiting for the decided bid")
                try:
                    await asyncio.wait_for(self.ready.wait(), max(1.0, until - llm.clock()))
                except asyncio.TimeoutError:
                    self.log("[submit] no bid in time")
                    return None
                self.log("[submit] bid received; continuing")
                last, repeats = None, 0
                continue  # the page may have changed (session timeout) while waiting
            if kind == "receipt":
                self.log("[submit] receipt: " + " ".join(obs.text.split())[:300])
                return obs.text[:600]
            repeats = repeats + 1 if obs.signature == last else 0
            last = obs.signature
            try:
                done = await self.step(obs, kind, position, repeats, until)
            except Blocked as blocked:  # the page is not as it was read (a dialog opened over it): read it again
                self.log(f"[submit] {blocked}; reading the page again")
                last, done = None, False
            if done:
                return None
        return None

    def overlay(self, obs: Observation) -> list[int]:
        """The controls of an open dialog that lies over the form (not a dialog that holds form fields itself)."""
        found = re.search(r"\[dialog[^\]]*\](.*?)\[/dialog\]", obs.text, re.S)
        ids = [int(number) for number in re.findall(r"\[(\d+):", found.group(1))] if found else []
        ids = [cid for cid in ids if cid in obs.controls]
        holds_fields = any(self.fields.get((obs.controls[cid].kind, obs.controls[cid].label), "other") != "other" for cid in ids)
        return [] if holds_fields else ids

    async def step(self, obs: Observation, kind: str, position: str | None, repeats: int, until: float) -> bool:
        """One pass over the page as read. Form actions are strict: they refuse to act through a dialog. True ends
        the run without a receipt."""
        tab = self.tab
        covering = self.overlay(obs)
        if covering:  # a dialog over the form goes first: nothing behind it can be typed or clicked
            close = next((cid for cid in covering
                          if self.purposes.get((obs.controls[cid].kind, obs.controls[cid].label)) == "close"), None)
            if close is None:
                close = await self.choose(obs, "close the dialog that covers the form, without following its links", covering)
            self.log(f"[submit] overlay: {await tab.click(close)}")
            return False
        if repeats >= 2:
            await self.repair(obs, position)
            return False
        if kind in ("obstacle", "start"):
            close = self.button(obs, "close")
            target = close.id if close and kind == "obstacle" else await self.choose(
                obs, "start the offer submission for this tender (register participation first if required)"
                if kind == "start" else "remove the obstacle")
            self.log(f"[submit] {kind}: {await tab.click(target)}")
            return False
        values = self.values_for(position)
        if position and position not in self.positions and self.button(obs, "omit"):
            self.log(f"[submit] omit {position}: {await tab.click(self.button(obs, 'omit').id, strict=True)}")
            return False
        if await self.fill_selects(obs, values):
            return False
        await self.fill_inputs(obs, values)  # same pass: nothing reloaded, so the controls are as observed
        if self.cascade_pending(obs, values) and self.button(obs, "apply"):
            self.log(f"[submit] apply: {await tab.click(self.button(obs, 'apply').id, strict=True)}")
            return False
        tan = self.controls_for(obs, "tan")
        if tan and not (tan[0].value or ""):
            if self.tan_signed:  # signed with this code and back at an empty field: the form refused it
                self.log(f"[submit] the signing code {self.tan_signed} was not accepted")
                self.tan_refused.add(self.tan_signed)
                self.tan_signed = self.tan_code = None
                self.tan_requested = False
            if not self.tan_requested:
                if self.tan_requests >= 3 or not self.button(obs, "request_tan"):
                    self.log("[submit] no accepted signing code after three requests; giving up")
                    self.gave_up = True
                    return True
                self.mailbox_before = await self.mailbox_links()
                self.log(f"[submit] {await tab.click(self.button(obs, 'request_tan').id, strict=True)}")
                self.tan_requested, self.tan_code = True, None
                self.tan_requests += 1
                return False
            self.tan_code = self.tan_code or await self.fetch_tan(until)
            if not self.tan_code or self.tan_code in self.tan_refused:
                self.tan_code, self.tan_requested = None, False  # nothing usable arrived: request once more
                return False
            self.log(f"[submit] {await tab.fill(tan[0].id, self.tan_code)}")
            submit = self.button(obs, "submit")
            if submit:  # typing changed nothing else on the page: sign in the same pass
                self.tan_signed = self.tan_code
                self.log(f"[submit] submit: {await tab.click(submit.id, strict=True)}")
            return False
        control = self.button(obs, "submit") if tan else None
        if control:
            self.tan_signed = tan[0].value
        control = control or self.button(obs, "proceed")
        if control:
            self.log(f"[submit] {self.purposes[(control.kind, control.label)]}: {await tab.click(control.id, strict=True)}")
        else:
            await self.repair(obs, position)
        return False

    async def mailbox_links(self) -> set[str]:
        """The link targets on the mailbox's entry page as it is now, read in a tab of its own."""
        if not self.mail:
            return set()
        try:
            pages = await self.mail.tab.read_pages([self.mail.site["url"]])
            return set(re.findall(r"→(\S+)", pages[0][1])) if pages else set()
        except Exception:
            return set()

    async def choose(self, obs: Observation, goal: str, among: list[int] | None = None) -> int:
        options = {str(c.id): c.describe()[:160] for c in obs.controls.values()
                   if not c.disabled and (among is None or c.id in among)}
        if len(options) == 1:
            return int(next(iter(options)))
        answers = await llm.clef({"goal": goal, "instructions": self.brief_submission, "page": obs.clip(6000)},
                                 {"c": llm.choice(f"Which control should be clicked to {goal}? Avoid decoys.", options)})
        return int(answers["c"]["choice"])

    async def repair(self, obs: Observation, position: str | None):
        """The page does not advance: Qwen reads it (validation messages) and chooses corrective actions."""
        system = ("You enter a finished tender bid into a web form. The values are final: never change them, only "
                  "enter them as the form expects. Reply with JSON {\"actions\": [...]} using the ops click, fill, "
                  "select and check with control ids of the page, e.g. {\"op\": \"fill\", \"id\": 7, \"value\": \"1.234,50\"}.")
        user = (f"SUBMISSION INSTRUCTIONS:\n{self.brief_submission}\n\nVALUES FOR THIS PAGE: "
                f"{json.dumps(self.values_for(position), ensure_ascii=False)}\nPOSITIONS IN THE BID: {list(self.positions)}"
                f" (omit any other position)\n\nPAGE (it did not advance; read its messages):\n{obs.clip(20000)}")
        try:
            reply = await llm.qwen(system, user, max_tokens=800)
        except Exception as error:
            self.log(f"[submit] repair failed: {error}")
            return
        for action in (reply.get("actions") or [])[:10]:
            try:
                cid, op = int(action["id"]), action.get("op")
                if op == "click":
                    result = await self.tab.click(cid)
                elif op == "select":
                    result = await self.tab.select(cid, action.get("option") or action.get("value", ""))
                elif op == "check":
                    result = await self.tab.check(cid, bool(action.get("on", True)))
                else:
                    result = await self.tab.fill(cid, action.get("value", ""))
                self.log(f"[submit] repair: {result}")
            except Exception as error:
                self.log(f"[submit] repair action failed: {error}")
                break

    async def fetch_tan(self, until: float) -> str | None:
        """The mail worker reads the signing TAN that was just requested and reports the code.

        The mailbox's entry page is read in a tab of its own until it shows something it did not show before the
        request (the new message); the model then opens that message and returns the code. Nothing has to be
        searched or unlocked on the way, and the state of the mail worker's main tab does not matter."""
        if not self.mail:
            return None
        pages, patience = [], llm.clock() + 8
        while True:
            try:
                pages = list(await self.mail.tab.read_pages([self.mail.site["url"]]))
            except Exception as error:
                self.log(f"[submit] mailbox not readable: {type(error).__name__}")
            new = set(re.findall(r"→(\S+)", pages[0][1])) - self.mailbox_before if pages else set()
            if new or llm.clock() > patience:
                break
            await asyncio.sleep(0.4)
        goal = (f"A signing TAN for {self.answer['tender_reference']} was requested a moment ago (request number "
                f"{self.tan_requests} of this run) and arrives as a new message. You are given the mailbox as it is "
                "now" + (f"; these links are new since the request: {' '.join(sorted(new))[:300]}" if new else "")
                + ". Open that newest TAN message as its own page with read and return its code in the reply field "
                "'tan'. TAN messages of earlier requests are invalid. If the new message is not listed yet, read "
                "the mailbox address again.")
        self.mail.result.pop("tan", None)
        self.mail.assign([goal], fresh=True)
        self.mail.pending = pages
        worker = asyncio.create_task(self.mail.run(min(until, llm.clock() + 60), max_steps=self.mail.steps + 10))
        try:
            while not worker.done() and not self.mail.result.get("tan"):
                await asyncio.sleep(0.1)
            return self.mail.result.get("tan")
        finally:
            worker.cancel()
