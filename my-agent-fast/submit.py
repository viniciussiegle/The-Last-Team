"""Phase 3b: enter the master's bid in the submission website's form and sign it.

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
from web import Observation, Tab, match_option, normalize

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


PRELUDE = {"company", "vat_id", "email"}  # bidder data: known before the bid is decided


class Submitter:
    def __init__(self, tab: Tab, mail: Worker | None, bid: dict, plan: dict, brief: str, emails: list[str], log=print):
        self.tab, self.mail, self.log, self.plan = tab, mail, log, plan
        self.brief_submission = brief[brief.find("## Submission"):][:1500] if "## Submission" in brief else brief[-1500:]
        self.update(bid, emails)
        self.fields: dict[tuple, str] = {}       # (kind, label) -> bid field
        self.purposes: dict[tuple, str] = {}     # (kind, label) -> button purpose
        self.layouts: dict[str, str] = {}        # page layout -> page kind
        self.options: dict[tuple, str] = {}      # (field, value, options) -> chosen option
        self.tan_requested = False

    def update(self, bid: dict, emails: list[str]):
        """The master's (final) bid; the prelude may have run on a draft."""
        self.answer = bid["answer"]
        self.required = {str(d["position"]): d.get("required") or {} for d in bid.get("details", [])}
        self.all_positions = list(self.required) or [str(p["position"]) for p in self.answer["positions"]]
        bidder = self.plan.get("bidder") or {}
        self.order = {"company": bidder.get("company"), "vat_id": bidder.get("vat_id"),
                      "email": bidder.get("email") or (emails[0] if emails else None),
                      "offer_type": self.answer["decision"], "discount": percent(self.answer["discount_percent"]),
                      "total": german(self.answer["total_net"]), "ack_docs": True, "own_terms": False}
        self.positions = {str(p["position"]): p for p in self.answer["positions"]}

    # --------------------------------------------------------------- values
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
        """The page kind and its position; one Clef call per new layout maps its controls and buttons."""
        found = [p for p in self.all_positions if re.search(rf"(?<![\d.]){re.escape(p)}(?![\d])", obs.text)]
        layout = self.layout(obs)
        questions = {}
        if layout not in self.layouts:
            questions["page"] = llm.choice("What is this page?", PAGE)
        for control in obs.controls.values():
            key = (control.kind, control.label)
            if len(questions) >= 60:
                break
            if control.kind in ("button", "submit", "reset") and key not in self.purposes:
                questions[f"b{control.id}"] = llm.choice(f"What does the button {control.describe()[:200]} do?", PURPOSES)
            elif control.fillable and key not in self.fields:
                questions[f"f{control.id}"] = llm.choice(
                    f"Which bid field does the control {control.describe()[:220]} hold?", FIELDS)
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
        """Set the first select (page order) whose value differs; True if one changed."""
        for control in sorted(obs.controls.values(), key=lambda c: c.id):
            field = self.fields.get((control.kind, control.label))
            if control.kind == "select" and field in values and not control.disabled:
                option = await self.pick_option(control, field, values[field])
                if option and normalize(option) != normalize(control.value or ""):
                    self.log(f"[submit] {field}: {await self.tab.select(control.id, option)}")
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
                    self.log(f"[submit] {field}: {await self.tab.check(control.id, bool(values[field]))}")
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
    async def run(self, until: float, prelude: bool = False) -> str | None:
        """Enter the bid and sign it. As a prelude (while research still runs) only open the form and enter the
        bidder data, stopping at the first page that asks for anything the bid decides."""
        last, repeats = None, 0
        for _ in range(120):
            if llm.clock() > until:
                self.log("[submit] out of time")
                return None
            obs = await self.tab.observe()
            if obs.password:
                self.log(f"[submit] {await self.tab.login()}")
                continue
            kind, position = await self.understand(obs)
            if kind == "receipt":
                self.log("[submit] receipt: " + " ".join(obs.text.split())[:300])
                return obs.text[:600]
            repeats = repeats + 1 if obs.signature == last else 0
            last = obs.signature
            if prelude and kind == "form":
                asked = {self.fields.get((c.kind, c.label)) for c in obs.controls.values() if c.fillable}
                if asked - PRELUDE - {"other", None} or repeats >= 2:
                    self.log("[submit] prelude ready: " + " ".join(obs.text.split())[:120])
                    return None
            if repeats >= 2:
                await self.repair(obs, position)
                continue
            if kind in ("obstacle", "start"):
                close = self.button(obs, "close")
                target = close.id if close and kind == "obstacle" else await self.choose(
                    obs, "start the offer submission for this tender (register participation first if required)"
                    if kind == "start" else "remove the obstacle")
                self.log(f"[submit] {kind}: {await self.tab.click(target)}")
                continue
            values = self.values_for(position)
            if prelude:
                values = {key: value for key, value in values.items() if key in PRELUDE}
            if position and position not in self.positions and self.button(obs, "omit"):
                self.log(f"[submit] omit {position}: {await self.tab.click(self.button(obs, 'omit').id)}")
                continue
            if await self.fill_selects(obs, values):
                continue
            if self.cascade_pending(obs, values) and self.button(obs, "apply"):
                self.log(f"[submit] apply: {await self.tab.click(self.button(obs, 'apply').id)}")
                continue
            await self.fill_inputs(obs, values)
            tan = self.controls_for(obs, "tan")
            if tan and not (tan[0].value or ""):
                if not self.tan_requested and self.button(obs, "request_tan"):
                    self.log(f"[submit] {await self.tab.click(self.button(obs, 'request_tan').id)}")
                    self.tan_requested = True
                    continue
                code = await self.fetch_tan(obs, until)
                if code:
                    self.log(f"[submit] {await self.tab.fill(tan[0].id, code)}")
                else:
                    self.tan_requested = False  # request a fresh one
                continue
            control = self.button(obs, "submit") if tan else None
            control = control or self.button(obs, "proceed")
            if control:
                self.log(f"[submit] {self.purposes[(control.kind, control.label)]}: {await self.tab.click(control.id)}")
            else:
                await self.repair(obs, position)
        return None

    async def choose(self, obs: Observation, goal: str) -> int:
        options = {str(c.id): c.describe()[:160] for c in obs.controls.values() if not c.disabled}
        answers = await llm.clef({"goal": goal, "page": obs.clip(6000)},
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

    async def fetch_tan(self, obs: Observation, until: float) -> str | None:
        """The mail worker opens the newest TAN message for this tender and reports the code as a note."""
        if not self.mail:
            return None
        request = re.findall(r"(?:Anforderung|request)\D{0,5}(\d+)", obs.text, re.I)
        goal = (f"Find the newest signing TAN message for {self.answer['tender_reference']}"
                + (f" (request {request[-1]})" if request else "")
                + " at the top of the inbox (no search needed), open that message's own page with one read, and "
                  "report its code as the note 'TAN: <code>'. Older TAN messages are invalid.")
        start = len(self.mail.board.notes)
        self.mail.assign([goal])
        self.mail.read_pages.clear()
        worker = asyncio.create_task(self.mail.run(min(until, llm.clock() + 90), max_steps=self.mail.steps + 25))

        def code():
            for note in self.mail.board.notes[start:]:
                match = re.search(r"TAN\W+((?=[A-Za-z-]*\d)[A-Za-z0-9-]{4,16})\b", note.split("]", 1)[-1])
                if match:
                    return match.group(1)
        try:
            while not worker.done() and not code():
                await asyncio.sleep(0.3)
            return code()
        finally:
            worker.cancel()
