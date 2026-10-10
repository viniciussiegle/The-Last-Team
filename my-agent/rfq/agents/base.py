from __future__ import annotations

import json
import re
import asyncio
import time
from pathlib import Path
from collections import Counter

from ..browser.actions import act_many
from ..browser.guards import abandon, recover
from ..browser.snapshot import PageUnresponsive, observe


def function(name, description, properties, required):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required}}}


ACTION = {
    "kind": {"type": "string", "enum": ["login", "goto", "click", "type", "fill", "select", "check", "press", "scroll", "back", "switch_tab", "look", "extract_table", "expand", "open_document", "read_all"]},
    "id": {"type": "integer"}, "url": {"type": "string"}, "text": {"type": "string"},
    "submit": {"type": "boolean"}, "option": {"type": "string"}, "key": {"type": "string"},
    "direction": {"type": "string"}, "index": {"type": "integer"}, "section": {"type": "string"},
    "checked": {"type": "boolean"}, "confirm_final": {"type": "boolean"},
    "fields": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "integer"}, "text": {"type": "string"}}, "required": ["id", "text"]}},
    "submit_id": {"type": "integer"},
    "next_id": {"type": "integer", "description": "read_all: the control that leads to the next page of this result set"},
    "reveal_ids": {"type": "array", "items": {"type": "integer"},
                   "description": "read_all: one example of each control that opens a hidden detail of a row or field"},
    "open_ids": {"type": "array", "items": {"type": "integer"},
                 "description": "read_all: links to open and read one after another, such as chosen results or the tabs of one record"},
}

TOOLS = [
    function("browser", "Act on the page, then observe again. IDs come from the latest snapshot. Give one action, or "
             "actions=[...] to run several in order from this same observation when later steps need no new reading.", {
        **ACTION,
        "actions": {"type": "array", "maxItems": 14, "items": {"type": "object", "properties": ACTION, "required": ["kind"]}},
        "reason": {"type": "string", "maxLength": 200}
    }, ["reason"]),
    function("record", "Append a typed fact grounded in the observed page or chosen document. Follow the supplied record schema.", {
        "kind": {"type": "string"}, "data": {"type": "object"},
        "frame_path": {"type": "string", "description": "Observed source frame path, such as frame/0, when the evidence is in a nested frame."},
        "snippet": {"type": "string", "maxLength": 300}
    }, ["kind", "data", "snippet"]),
    function("done", "Task complete; summarize what was found and any unresolved gaps.", {
        "summary": {"type": "string"}, "gaps": {"type": "array", "items": {"type": "string"}},
        "submitted": {"type": "boolean"}, "receipt": {"type": "string"}
    }, ["summary"]),
    function("need_help", "Request a missing dependency or report an evidence conflict.", {
        "question": {"type": "string"}
    }, ["question"]),
    function("wait_for_evidence", "Wait briefly for a specific kind of shared record when another active worker is investigating that dependency. Reuse already available facts first.", {
        "record_kind": {"type": "string"},
        "timeout_seconds": {"type": "number", "minimum": 0, "maximum": 30}
    }, ["record_kind"]),
]

TAN_TOOL = function("fetch_signing_tan", "After requesting a signing code on the portal, ask the mailbox worker to read it. Pass the portal's request number when it shows one. Do not request another code while using this one.", {
    "request_id": {"type": "string"}
}, [])

class BrowserAgent:
    def __init__(self, name, session, model, bus, ledger, run, settings, shared):
        self.name, self.session, self.model, self.bus = name, session, model, bus
        self.ledger, self.run_data, self.settings, self.shared = ledger, run, settings, shared
        self.summary = ""
        self.step_count = 0
        self.tan_fetcher = None
        self.observations = []
        if hasattr(bus, "add_secret"):
            bus.add_secret(settings.run_token)
            for site in run.get("access", []):
                for value in (site.get("credentials") or {}).values():
                    bus.add_secret(value)

    def scrub(self, text: str) -> str:
        """Keep credentials, session headers and the run token out of anything returned to the model."""
        for site in [self.session.site, *self.session.sites_by_origin.values()]:
            for value in (site.get("credentials") or {}).values():
                if isinstance(value, str) and value:
                    text = text.replace(value, "[credential]")
        text = re.sub(r"(?im)(cookie|authorization|x-api-key):[^\n]*", r"\1: [redacted]", text)
        if self.settings.run_token:
            text = text.replace(self.settings.run_token, "[run token]")
        return text

    async def summarize(self):
        reply = await self.model.complete(self.name, [
            {"role": "system", "content": "Summarize only what these browser observations establish. State authenticated site sections and search affordances, and explicitly distinguish unresolved access issues. Be concise, under 250 words. Do not infer unseen data."},
            {"role": "user", "content": "\n\n".join(self.observations[-3:])}], max_tokens=900)
        self.summary = reply.content or "No summary returned"
        result = {"summary": self.summary, "gaps": ["Investigation stopped at its step/time budget; inspect summary for remaining access issues"]}
        self.shared[self.name] = result
        self.bus.emit("agent_done", self.name, result)
        return result

    async def run(self, task: str, max_steps=None, until=None, submit=False, stop_record=None):
        worker_status = self.shared.setdefault("worker_status", {})
        worker_status[self.name] = "running"
        system = Path(__file__).parents[1].joinpath("llm/prompts/browser.md").read_text(encoding="utf-8")
        record_schema = self.ledger.schema_text() if self.ledger else "No evidence records required for orientation."
        system += "\nRECORD CONTRACTS\n" + record_schema
        # Credentials are used by the login helper; the model needs only task rules.
        brief = re.sub(r"## Site access\b.*?(?=\n## |\Z)", "", self.run_data["brief"], flags=re.S)
        task_text = f"Runtime brief:\n{brief}\n\nYOUR TASK:\n{task}"
        history = []
        hashes = []
        tool_result = ""
        reading = ""
        image = None
        slow_reads = 0
        for step in range(max_steps or self.settings.max_steps):
            if until and time.monotonic() >= until:
                worker_status[self.name] = "time limit"
                return {"summary": self.summary, "gaps": ["Task time budget exhausted"]}
            try:
                # Every step of the loop is time-bounded as a whole: one frame that never finishes loading must not
                # leave the worker waiting inside some page call for the rest of the run.
                try:
                    await asyncio.wait_for(recover(self.session, self.bus, self.name), 25)
                except asyncio.TimeoutError:
                    raise PageUnresponsive("The page did not respond while it was being checked") from None
                snapshot = await observe(self.session)
                slow_reads = 0
                self.session.replaced_in_a_row = 0
                self.observations.append(snapshot.text)
                self.observations = self.observations[-3:]
                logged_snapshot = "[Signing mailbox observation omitted]" if self.ledger and self.ledger.allow_tan else snapshot.text
                self.bus.emit("observation", self.name, {"text": logged_snapshot, "page_url": self.session.page.url})
                hashes.append(snapshot.digest)
                if len(hashes) >= 3 and len(set(hashes[-3:])) == 1:
                    tool_result += "\nPage unchanged repeatedly. Choose a DIFFERENT control/search/tab/strategy; do not repeat the same action."
                if not any(e.get("type") == "password" for e in snapshot.elements.values()):
                    self.session.last_good_url = self.session.page.url
                    from ..browser.session import origin
                    self.session.last_good_urls[origin(self.session.page.url)] = self.session.page.url
                shared = json.dumps(self.shared, ensure_ascii=False, default=str)
                known = self.ledger.summary(self.name) if self.ledger else ""
                evidence_counts = Counter(kind for kind, _, _ in self.ledger.records) if self.ledger else {}
                remaining_steps = (max_steps or self.settings.max_steps) - step
                content = f"Steps remaining: {remaining_steps}. Finish with done before exhausting them; state gaps explicitly.\nShared worker updates: {shared[-10000:]}\nEvidence ledger: {known}\nPrevious tool result: {(reading + tool_result)[:45000]}\nCURRENT OBSERVATION:\n{snapshot.text}"
                messages = [{"role": "system", "content": system}, {"role": "user", "content": task_text}]
                messages.extend(history[-6:])
                if image:
                    messages.append({"role": "user", "content": [{"type": "text", "text": content},
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image}}]})
                    image = None
                else:
                    messages.append({"role": "user", "content": content})
                call = self.model.complete(self.name, messages, TOOLS + ([TAN_TOOL] if self.tan_fetcher and submit else []))
                reply = await asyncio.wait_for(call, timeout=max(1, min(360, until - time.monotonic()))) if until else await call
                tool_result = ""
                if not reply.tool_calls:
                    history.append({"role": "assistant", "content": reply.content or ""})
                    tool_result = "Choose a tool to continue or call done."
                    continue
                # Execute one browser action per observation. Multiple record calls are safe.
                browser_used = False
                action_notes = []
                for call in reply.tool_calls:
                    args = json.loads(call.function.arguments)
                    name = call.function.name
                    if name not in {"done", "need_help", "record", "browser", "fetch_signing_tan", "wait_for_evidence"}:
                        raise ValueError("Unknown tool name returned: " + name)
                    if name == "done":
                        worker_status[self.name] = "done"
                        self.summary = args["summary"]
                        self.shared[self.name] = args
                        self.bus.emit("agent_done", self.name, args)
                        return args
                    if name == "need_help":
                        self.shared[self.name] = {"question": args["question"]}
                        self.bus.emit("dependency", self.name, args)
                        tool_result += "Dependency shared. Continue independent investigation or done with explicit gaps.\n"
                    elif name == "wait_for_evidence" and self.ledger:
                        kind = args["record_kind"]
                        timeout = min(30, max(0, float(args.get("timeout_seconds", 20))))
                        if until:
                            timeout = min(timeout, max(0, until - time.monotonic()))
                        changed = await self.ledger.wait_for_change(kind, evidence_counts.get(kind, 0), timeout)
                        self.bus.emit("dependency_wait", self.name, {"kind": kind, "changed": changed})
                        tool_result += ("New shared evidence arrived. Re-read the ledger and continue your assigned task.\n" if changed
                                        else "No new requested evidence yet. Inspect shared worker status and existing facts before deciding the next step.\n")
                    elif name == "fetch_signing_tan" and self.tan_fetcher and submit:
                        tool_result += await self.tan_fetcher(args)
                    elif name == "record":
                        if self.ledger:
                            result = self.ledger.add(args["kind"], args["data"], self.session, self.name, args.get("snippet", ""), args.get("frame_path", "frame/0"))
                            tool_result += str(result) + "\n"
                            if stop_record == args["kind"] and result == "Recorded " + stop_record:
                                worker_status[self.name] = "done"
                                self.summary = "Requested evidence recorded."
                                outcome = {"summary": self.summary, "recorded": stop_record}
                                self.shared[self.name] = outcome
                                self.bus.emit("agent_done", self.name, outcome)
                                return outcome
                        else:
                            tool_result += "Orientation has no record ledger. Summarize the site when done.\n"
                    elif name == "browser":
                        if browser_used:
                            tool_result += "Additional browser action deferred; IDs require a new observation.\n"
                            continue
                        batch = args.get("actions") or [{k: v for k, v in args.items() if k not in ("actions", "reason")}]
                        if not all(isinstance(item, dict) and item.get("kind") for item in batch):
                            raise ValueError("Each browser action needs a kind")
                        if any(item.get("confirm_final") for item in batch) and not submit:
                            raise ValueError("Binding confirmation is reserved for the frozen-answer submitter")
                        browser_used = True
                        reading = ""
                        # Ids are renumbered per observation, so the log names each control as the model saw it.
                        seen = self.session.elements
                        targets = [(seen.get(item["id"]) or {}).get("label", "")[:60] if isinstance(item.get("id"), int) else ""
                                   for item in batch]
                        try:
                            output = await asyncio.wait_for(act_many(self.session, batch), 150)
                        except asyncio.TimeoutError:
                            output = "The action did not finish within 150 seconds and was stopped. Look at the page again."
                        if isinstance(output, dict):
                            image = output["image"]
                            output = output["text"]
                        tool_result += self.scrub(str(output)) + "\n"
                        logged_action = [{"kind": item["kind"], "id": item.get("id"), "values": "[submission fields]"}
                                         if submit and item["kind"] in ("type", "fill") else item for item in batch]
                        logged_action = [{**item, "target": target} if target else item
                                         for item, target in zip(logged_action, targets)]
                        logged_action = logged_action[0] if len(logged_action) == 1 else logged_action
                        self.bus.emit("agent_step", self.name, {"step": self.step_count, "goal": task[:200],
                            "thought": args.get("reason", "")[:200], "action": logged_action,
                            "observation_size": len(snapshot.text), "page_url": self.session.page.url})
                    action_notes.append(f"{name}: {json.dumps(args, ensure_ascii=False)}")
                history.append({"role": "assistant", "content": "\n".join(action_notes)})
                # A long reading is shown once in full; the running history keeps only its start.
                # A long reading stays in front of the model while it only records from it, and leaves with the
                # next browser action; the running history keeps just its start.
                kept = tool_result
                if len(tool_result) > 2500:
                    if browser_used:
                        reading = tool_result + "\n[this reading stays visible until your next browser action]\n"
                        tool_result = ""
                    kept = kept[:1500] + "\n[long reading; kept in view while you record from it]"
                history.append({"role": "user", "content": "Results: " + kept})
                self.step_count += 1
            except asyncio.CancelledError:
                worker_status[self.name] = "paused at phase deadline"
                raise
            except PageUnresponsive:
                # A slow read is simply tried again; only a tab that stays silent is given up.
                slow_reads += 1
                if slow_reads >= 3:
                    slow_reads = 0
                    await abandon(self.session, self.bus, self.name)
                    tool_result = "The tab stopped responding and was replaced. Continue from the new observation."
                else:
                    await asyncio.sleep(1)
            except Exception as error:
                tool_result = self.scrub(f"Tool/observation error: {type(error).__name__}: {str(error)[:1500]}. Adapt using a fresh observation.")
                self.bus.emit("retry", self.name, {"step": step, "reason": tool_result})
        worker_status[self.name] = "step limit"
        return {"summary": self.summary, "gaps": ["Step limit reached"]}
