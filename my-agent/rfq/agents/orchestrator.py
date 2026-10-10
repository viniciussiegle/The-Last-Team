from __future__ import annotations

import asyncio
import json
import re
import time

from playwright.async_api import async_playwright

from ..browser.session import BrowserSession
from ..llm.clients import QwenClient
from .base import BrowserAgent
from .base import function
from ..evidence.ledger import Ledger
from decimal import Decimal

from ..rules.engine import Calculation, calculate
from ..submit.answer import validate_answer
from ..submit.watchdog import deadlines
from .submitter import submit_frozen


async def orient(run, settings, bus):
    shared = {}
    model = QwenClient(settings, bus)
    bus.emit("phase", "orchestrator", {"phase": "orientation"})
    async with async_playwright() as playwright:
        sessions = await asyncio.gather(*(BrowserSession.connect(playwright, settings, site, i, run["access"])
                                         for i, site in enumerate(run["access"])))
        agents = [BrowserAgent(s.site["siteId"], s, model, bus, None, run, settings, shared) for s in sessions]
        deadline = time.monotonic() + min(settings.run_budget_s, 480)
        try:
            results = await asyncio.gather(*(a.run("ORIENTATION ONLY: log in and reach the authenticated working area. Inspect visible navigation menus/sections and search/document affordances. Then immediately call done with a concise summary of the sections and how to search. Do not investigate the tender's facts, read business documents, or enumerate results. Do not submit a bid. Once you can name the site's sections and search mechanism, you are DONE. Summarize within 250 words.", max_steps=25, until=deadline) for a in agents))
            for i, result in enumerate(results):
                if not result.get("summary"):
                    results[i] = await agents[i].summarize()
            bus.emit("run_finished", "orchestrator", {"mode": "orientation", "results": results, "cost": model.total_cost})
            return results
        finally:
            await asyncio.gather(*(s.close() for s in sessions))


async def plan_context(model, run):
    schema = function("run_context", "Extract only explicitly provided task context and rule authority from the runtime brief.", {
        "reference": {"type": "string"}, "scenario_date": {"type": "string"},
        "bidder_domain": {"type": ["string", "null"]},
        "bidder_company": {"type": "string"}, "bidder_tax_id": {"type": "string"},
        "authorized_senders": {"type": "object", "description": "Map MailRecord kind names to authorized sender local parts from the rules."},
        "site_roles": {"type": "object", "description": "Map each runtime siteId to tender, spec, erp or mail based on the brief."}
    }, ["reference", "scenario_date", "authorized_senders", "site_roles"])
    brief = re.sub(r"## Site access\b.*?(?=\n## |\Z)", "", run["brief"], flags=re.S)
    messages = [{"role": "system", "content": "Extract runtime task context. Do not invent unknowns. Call run_context. Mail kinds: discount_approval, konditionsregeln, technikfreigabe, verpackungstarife, terminfreigabe, devisenfixing, ruestpools, projektbonus, preisindizes."},
                {"role": "user", "content": brief + "\nAvailable sites: " + json.dumps([{k: s.get(k) for k in ("siteId", "url")} for s in run["access"]])}]
    reply = await model.complete("planner", messages, [schema], max_tokens=1800)
    if not reply.tool_calls:
        raise ValueError("Planner did not provide structured runtime context")
    return json.loads(reply.tool_calls[0].function.arguments)


def answer_from(ledger, context, settings, bus):
    """The best answer the evidence supports. A run without an answer scores nothing, so a calculation that
    fails still yields the decision and reference, with the failure listed as a gap."""
    try:
        return calculate(ledger, context, settings.surcharge_includes_setup, settings.rules_stage == "core")
    except Exception as error:
        bus.emit("calculation_failed", "rules", {"error": f"{type(error).__name__}: {str(error)[:300]}"})
        answer = {"tender_reference": context["reference"], "decision": "bid", "currency": "EUR",
                  "discount_percent": Decimal(0), "positions": [], "total_net": Decimal(0)}
        return Calculation(answer, gaps=[f"Calculation failed: {type(error).__name__}: {str(error)[:200]}"])


async def next_tasks(model, free, busy, gaps, run, deadline):
    """A planning call decides what each free worker investigates next. Facts may live on any site, so a worker
    may be sent to a site other than its own."""
    tool = function("assign", "Give free workers their next investigation.", {
        "assignments": {"type": "array", "items": {"type": "object", "properties": {
            "worker": {"type": "string"}, "task": {"type": "string"}}, "required": ["worker", "task"]}}}, ["assignments"])
    sites = [{key: site.get(key) for key in ("siteId", "url")} for site in run["access"]]
    prompt = ("Open gaps of the bid calculation:\n" + json.dumps(gaps, ensure_ascii=False, default=str)
              + "\nFree workers: " + ", ".join(free)
              + "\nBusy workers and what each is doing: " + json.dumps(busy, ensure_ascii=False)
              + "\nSites of this run: " + json.dumps(sites)
              + "\nGive each free worker at most one task that closes a gap nobody else is working on. Split independent "
                "work between workers, for example one catalogue group or one topic each. A worker can work on any "
                "site: name the site and what to record. Leave a worker out if nothing useful remains for it.")
    try:
        reply = await asyncio.wait_for(model.complete("planner", [
            {"role": "system", "content": "You coordinate browser workers gathering evidence for a bid. Call assign."},
            {"role": "user", "content": prompt}], [tool], max_tokens=1200), timeout=max(1, deadline - time.monotonic() - 10))
        items = json.loads(reply.tool_calls[0].function.arguments).get("assignments", []) if reply.tool_calls else []
    except Exception:
        return {}
    return {item["worker"]: item["task"] for item in items
            if isinstance(item, dict) and isinstance(item.get("worker"), str) and isinstance(item.get("task"), str)}


TASKS = {
    "tender": "Find the exact tender. First record TenderInfo and every TenderPosition from its bill of quantities, all in one call: the positions are needed by everyone else. Then record ALL clarification/Q&A entries, including withdrawals, amendments and commercial/packaging/delivery/setup/bonus keys. Page through the model-selected clarification result set. Open relevant tender documents when needed. Preserve dates, revision and requirement references. After facts, prepare registration/interest if required, but do not enter prices or sign. Tell others when all positions/Q&A are recorded.",
    "spec": "Find the technical specification for this tender, identify the revision explicitly marked current, and record SpecRevision with all requirements and catalogue groups, exact normalized attributes, pieces per set and usable/nominal stroke. Inspect superseded versions as candidates only when needed to establish revision status. Open the chosen ZIP/PDF/iframe documents. Share groups/requirements promptly so ERP can search. Do not calculate prices.",
    "erp": "Orient in the ERP and learn material/group searches. Watch shared evidence for specification catalogue groups and amended requirements. Search EACH required group precisely, inspect all candidates and record Material attributes/status, stock/lead times, carton size, surcharge and allocation masses, index/formula and ordinary setup fee. Record all dated PriceCondition entries/scales/currency/price units/FX keys for matching candidates. Record SearchCoverage only when every result in the selected group is inspected. Need_help for missing groups while continuing independent orientation; never use an empty broad catalogue crawl. Do not decide no_bid from missing evidence. Record evidenced zero fees/mass explicitly.",
    "mail": "Identify the logged-in bidder mailbox address/domain and record it in TenderInfo. Search by exact tender reference AND each relevant topic, checking inbox AND archive and all result pages. Record all candidate MailRecord approvals with sender/date/reference and structured payloads, including newer future or unrelated records so resolver can reject them. Collect discount approval, commercial clauses, technical reserve, packaging tariff, delivery commitment, FX fixing, setup pools, project bonus and metal index rates with validity windows. Read each full message; preserve raw clause text, do not calculate. Shared Q&A/spec/ERP may reveal keys or groups. Do not request a signing TAN yet."
}


async def orchestrate(run, settings, bus):
    shared = {}
    model = QwenClient(settings, bus)
    ledger = Ledger(bus)
    freeze_at, end_at = deadlines(run, settings)
    bus.emit("run_started", "orchestrator", {"budget_s": settings.run_budget_s, "model": settings.model_name})
    async with async_playwright() as playwright:
        context_task = asyncio.create_task(plan_context(model, run))
        sessions = await asyncio.gather(*(BrowserSession.connect(playwright, settings, site, i, run["access"])
                                         for i, site in enumerate(run["access"])))
        context = await context_task
        shared["runtime_context"] = context
        agents = {}
        for session in sessions:
            role = context["site_roles"].get(session.site["siteId"], "tender")
            agents[role] = BrowserAgent(role, session, model, bus, ledger, run, settings, shared)
        bus.emit("phase", "orchestrator", {"phase": "gather"})
        try:
            worker_tasks = dict(TASKS)
            if settings.rules_stage == "core":
                worker_tasks["mail"] = "Identify the logged-in bidder mailbox domain and record TenderInfo. First find the discount approvals for the exact tender reference in inbox and archive: record each candidate from its list row (sender, date, subject, reference), and open only the newest one from the authorized sender to read its percentage and any threshold. Then find revision-specific technical reserve approvals needed for usable stroke and FX fixing keys needed by ERP conditions. Record typed MailRecord payloads and preserve raw text. Prioritize these core inputs; other commercial adjustment topics are for a later phase. Search exact reference plus topic from observed evidence, never broad crawling. Do not request TAN yet."
                worker_tasks["erp"] += " Core phase priority: matching active materials, valid condition/scales, and stock/lead time. Record these promptly before investigating optional adjustments."
            # Workers start on their own sites. Whenever one becomes free while the calculation still has gaps,
            # a planning call gives it the next investigation, on whichever site holds the missing fact; gathering
            # ends as soon as every position is priced, or at the freeze.
            running = {role: asyncio.create_task(agent.run(worker_tasks.get(role, TASKS["tender"]), until=freeze_at))
                       for role, agent in agents.items()}
            doing = {role: worker_tasks.get(role, TASKS["tender"])[:160] for role in agents}
            planned_for = None
            while time.monotonic() < freeze_at:
                working = [task for task in running.values() if not task.done()]
                if working:
                    await asyncio.wait(working, timeout=min(15, max(0.1, freeze_at - time.monotonic())),
                                       return_when=asyncio.FIRST_COMPLETED)
                calc = answer_from(ledger, context, settings, bus)
                # Only a complete bid ends gathering early; a conclusion not to bid can still be overturned by
                # evidence that arrives before the freeze.
                if calc.complete and calc.answer.get("decision") == "bid":
                    break
                free = [role for role, task in running.items() if task.done()]
                situation = (tuple(sorted(free)), tuple(calc.gaps))
                if not free or situation == planned_for or freeze_at - time.monotonic() < 40:
                    if len(free) == len(running) and situation == planned_for:
                        break  # nobody is working and the planner had nothing further
                    continue
                planned_for = situation
                bus.emit("phase", "orchestrator", {"phase": "followup", "free": free, "gaps": calc.gaps})
                plan = await next_tasks(model, free, {role: doing[role] for role in running if role not in free},
                                        calc.gaps, run, freeze_at)
                for role, text in plan.items():
                    if role in free:
                        doing[role] = text[:160]
                        running[role] = asyncio.create_task(agents[role].run(text, max_steps=30, until=freeze_at))
            for task in running.values():
                task.cancel()
            await asyncio.gather(*running.values(), return_exceptions=True)
            calc = answer_from(ledger, context, settings, bus)
            for step in calc.trace:
                bus.emit("trace_step", "rules", step)
            answer = validate_answer(calc.answer, run["answer_schema"])
            bus.emit("answer_frozen", "orchestrator", {"answer": answer, "gaps": calc.gaps, "complete": calc.complete})
            if not settings.submit_enabled:
                bus.emit("run_finished", "orchestrator", {"result": {"submitted": False, "reason": "submission switched off"}, "cost": model.total_cost})
                return {"submitted": False}
            if run["submission"]["kind"] != "site":
                from ..platform import submit_api_answer
                result = submit_api_answer(run, settings.run_token, answer)
            else:
                target = next(s for s in sessions if s.site["siteId"] == run["submission"]["siteId"])
                submitter = BrowserAgent("submitter", target, model, bus, ledger, run, settings, shared)
                mailbox = agents.get("mail")
                if mailbox is None:
                    raise ValueError("Runtime plan did not identify a mailbox worker")
                try:
                    result = await asyncio.wait_for(submit_frozen(submitter, mailbox, answer, context, end_at), timeout=max(1, end_at - time.monotonic()))
                except asyncio.TimeoutError:
                    result = {"submitted": False, "reason": "submission deadline reached"}
            bus.emit("run_finished", "orchestrator", {"result": result, "cost": model.total_cost})
            return result
        finally:
            await asyncio.gather(*(s.close() for s in sessions))
