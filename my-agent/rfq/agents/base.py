from __future__ import annotations

import json
import re
import asyncio
import time
from pathlib import Path

from ..browser.actions import act
from ..browser.guards import recover
from ..browser.snapshot import observe


def function(name, description, properties, required):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required}}}


TOOLS = [
    function("browser", "Perform one model-chosen action, then observe again. IDs come from the latest snapshot.", {
        "kind": {"type": "string", "enum": ["login", "goto", "click", "type", "fill", "select", "check", "press", "scroll", "back", "switch_tab", "look", "extract_table", "expand", "open_document"]},
        "id": {"type": "integer"}, "url": {"type": "string"}, "text": {"type": "string"},
        "submit": {"type": "boolean"}, "option": {"type": "string"}, "key": {"type": "string"},
        "direction": {"type": "string"}, "index": {"type": "integer"}, "section": {"type": "string"},
        "checked": {"type": "boolean"}, "confirm_final": {"type": "boolean"},
        "fields": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "integer"}, "text": {"type": "string"}}, "required": ["id", "text"]}},
        "submit_id": {"type": "integer"}, "reason": {"type": "string", "maxLength": 200}
    }, ["kind", "reason"]),
    function("record", "Append a typed fact grounded in the observed page or chosen document. Follow the supplied record schema.", {
        "kind": {"type": "string"}, "data": {"type": "object"},
        "snippet": {"type": "string", "maxLength": 300}
    }, ["kind", "data", "snippet"]),
    function("done", "Task complete; summarize what was found and any unresolved gaps.", {
        "summary": {"type": "string"}, "gaps": {"type": "array", "items": {"type": "string"}},
        "submitted": {"type": "boolean"}, "receipt": {"type": "string"}
    }, ["summary"]),
    function("need_help", "Request a missing dependency or report an evidence conflict.", {
        "question": {"type": "string"}
    }, ["question"]),
]

TAN_TOOL = function("fetch_signing_tan", "After requesting a TAN on the portal, ask the mailbox worker for the fresh signing code. Never request another portal TAN while using this one.", {}, [])


class BrowserAgent:
    def __init__(self, name, session, model, bus, ledger, run, settings, shared):
        self.name, self.session, self.model, self.bus = name, session, model, bus
        self.ledger, self.run_data, self.settings, self.shared = ledger, run, settings, shared
        self.summary = ""
        self.step_count = 0
        self.tan_fetcher = None
        self.observations = []

    async def summarize(self):
        reply = await self.model.complete(self.name, [
            {"role": "system", "content": "Summarize only what these browser observations establish. State authenticated site sections and search affordances, and explicitly distinguish unresolved access issues. Be concise, under 250 words. Do not infer unseen data."},
            {"role": "user", "content": "\n\n".join(self.observations[-3:])}], max_tokens=900)
        self.summary = reply.content or "No summary returned"
        result = {"summary": self.summary, "gaps": ["Investigation stopped at its step/time budget; inspect summary for remaining access issues"]}
        self.shared[self.name] = result
        self.bus.emit("agent_done", self.name, result)
        return result

    async def run(self, task: str, max_steps=None, until=None, submit=False):
        system = Path(__file__).parents[1].joinpath("llm/prompts/browser.md").read_text(encoding="utf-8")
        record_schema = self.ledger.schema_text() if self.ledger else "No evidence records required for orientation."
        system += "\nRECORD CONTRACTS\n" + record_schema
        # Credentials are used by the login helper; the model needs only task rules.
        brief = re.sub(r"## Site access\b.*?(?=\n## |\Z)", "", self.run_data["brief"], flags=re.S)
        task_text = f"Runtime brief:\n{brief}\n\nYOUR TASK:\n{task}"
        history = []
        hashes = []
        tool_result = ""
        image = None
        for step in range(max_steps or self.settings.max_steps):
            if until and time.monotonic() >= until:
                return {"summary": self.summary, "gaps": ["Task time budget exhausted"]}
            try:
                await recover(self.session, self.bus, self.name)
                snapshot = await observe(self.session)
                self.observations.append(snapshot.text)
                self.observations = self.observations[-3:]
                self.bus.emit("observation", self.name, {"text": snapshot.text, "page_url": self.session.page.url})
                hashes.append(snapshot.digest)
                if len(hashes) >= 3 and len(set(hashes[-3:])) == 1:
                    tool_result += "\nPage unchanged repeatedly. Choose a DIFFERENT control/search/tab/strategy; do not repeat the same action."
                if not any(e.get("type") == "password" for e in snapshot.elements.values()):
                    self.session.last_good_url = self.session.page.url
                shared = json.dumps(self.shared, ensure_ascii=False, default=str)
                known = self.ledger.summary() if self.ledger else ""
                remaining_steps = (max_steps or self.settings.max_steps) - step
                content = f"Steps remaining: {remaining_steps}. Finish with done before exhausting them; state gaps explicitly.\nShared worker updates: {shared[-10000:]}\nEvidence ledger: {known[-16000:]}\nPrevious tool result: {tool_result[-14000:]}\nCURRENT OBSERVATION:\n{snapshot.text}"
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
                    if name not in {"done", "need_help", "record", "browser", "fetch_signing_tan"}:
                        raise ValueError("Unknown tool name returned: " + name)
                    if name == "done":
                        self.summary = args["summary"]
                        self.shared[self.name] = args
                        self.bus.emit("agent_done", self.name, args)
                        return args
                    if name == "need_help":
                        self.shared[self.name] = {"question": args["question"]}
                        self.bus.emit("dependency", self.name, args)
                        tool_result += "Dependency shared. Continue independent investigation or done with explicit gaps.\n"
                    elif name == "fetch_signing_tan" and self.tan_fetcher and submit:
                        tool_result += await self.tan_fetcher()
                    elif name == "record":
                        if self.ledger:
                            result = self.ledger.add(args["kind"], args["data"], self.session, self.name, args.get("snippet", ""))
                            tool_result += str(result) + "\n"
                        else:
                            tool_result += "Orientation has no record ledger. Summarize the site when done.\n"
                    elif name == "browser":
                        if browser_used:
                            tool_result += "Additional browser action deferred; IDs require a new observation.\n"
                            continue
                        if args.get("confirm_final") and not submit:
                            raise ValueError("Binding confirmation is reserved for the frozen-answer submitter")
                        browser_used = True
                        output = await act(self.session, args)
                        if isinstance(output, dict) and output.get("image"):
                            image = output["image"]
                            tool_result += "Screenshot attached.\n"
                        else:
                            tool_result += str(output) + "\n"
                        logged_action = {k: v for k, v in args.items() if k != "reason"}
                        if submit and args.get("kind") in ("type", "fill"):
                            logged_action = {"kind": args["kind"], "id": args.get("id"), "values": "[submission fields]"}
                        self.bus.emit("agent_step", self.name, {"step": self.step_count, "goal": task[:200],
                            "thought": args.get("reason", "")[:200], "action": logged_action,
                            "observation_size": len(snapshot.text), "page_url": self.session.page.url})
                    action_notes.append(f"{name}: {json.dumps(args, ensure_ascii=False)}")
                history.append({"role": "assistant", "content": "\n".join(action_notes)})
                history.append({"role": "user", "content": "Results: " + tool_result[:8000]})
                self.step_count += 1
            except Exception as error:
                tool_result = f"Tool/observation error: {type(error).__name__}: {str(error)[:1500]}. Adapt using a fresh observation."
                for value in (self.session.site.get("credentials") or {}).values():
                    if isinstance(value, str) and value:
                        tool_result = tool_result.replace(value, "[credential]")
                self.bus.emit("retry", self.name, {"step": step, "reason": tool_result})
        return {"summary": self.summary, "gaps": ["Step limit reached"]}
