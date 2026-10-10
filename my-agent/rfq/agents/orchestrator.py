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
from ..rules.engine import calculate
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


TASKS = {
    "tender": "Find the exact tender and record TenderInfo, every TenderPosition and ALL clarification/Q&A entries, including withdrawals, amendments and commercial/packaging/delivery/setup/bonus keys. Page through the model-selected clarification result set. Open relevant tender documents when needed. Preserve dates, revision and requirement references. After facts, prepare registration/interest if required, but do not enter prices or sign. Tell others when all positions/Q&A are recorded.",
    "spec": "Find the technical specification for this tender, identify the revision explicitly marked current, and record SpecRevision with all requirements and catalogue groups, exact normalized attributes, pieces per set and usable/nominal stroke. Inspect superseded versions as candidates only when needed to establish revision status. Open the chosen ZIP/PDF/iframe documents. Share groups/requirements promptly so ERP can search. Do not calculate prices.",
    "erp": "Orient in the ERP and learn material/group searches. Watch shared evidence for specification catalogue groups and amended requirements. Search EACH required group precisely, inspect all candidates and record Material attributes/status, stock/lead times, carton size, surcharge and allocation masses, index/formula and ordinary setup fee. Record all dated PriceCondition entries/scales/currency/price units/FX keys for matching candidates. Record SearchCoverage only when every result in the selected group is inspected. Need_help for missing groups while continuing independent orientation; never use an empty broad catalogue crawl. Do not decide no_bid from missing evidence. Record evidenced zero fees/mass explicitly.",
    "mail": "Identify the logged-in bidder mailbox address/domain and record it in TenderInfo. Search by exact tender reference AND each relevant topic, checking inbox AND archive and all result pages. Record all candidate MailRecord approvals with sender/date/reference and structured payloads, including newer future or unrelated records so resolver can reject them. Collect discount approval, commercial clauses, technical reserve, packaging tariff, delivery commitment, FX fixing, setup pools, project bonus and metal index rates with validity windows. Read each full message; preserve raw clause text, do not calculate. Shared Q&A/spec/ERP may reveal keys or groups. Do not request a signing TAN yet."
}


async def orchestrate(run, settings, bus):
    shared = {}
    model = QwenClient(settings, bus)
    ledger = Ledger(bus)
    freeze_at, end_at = deadlines(run, settings)
    bus.emit("run_started", "orchestrator", {"budget_s": settings.run_budget_s, "frontier": False})
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
                worker_tasks["mail"] = "Identify the logged-in bidder mailbox domain and record TenderInfo. First find ALL candidate discount approvals for the exact tender reference in inbox and archive, with sender/date/reference/threshold. Then find revision-specific technical reserve approvals needed for usable stroke and FX fixing keys needed by ERP conditions. Record typed MailRecord payloads and preserve raw text. Prioritize these core inputs; other commercial adjustment topics are for a later phase. Search exact reference plus topic from observed evidence, never broad crawling. Do not request TAN yet."
                worker_tasks["erp"] += " Core phase priority: matching active materials, valid condition/scales, and stock/lead time. Record these promptly before investigating optional adjustments."
            initial_deadline = freeze_at - min(180, max(0, freeze_at - time.monotonic()) * 0.25)
            tasks = [asyncio.create_task(agent.run(worker_tasks.get(role, TASKS["tender"]), until=initial_deadline)) for role, agent in agents.items()]
            done, pending = await asyncio.wait(tasks, timeout=max(0, initial_deadline - time.monotonic()))
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            calc = calculate(ledger, context, settings.surcharge_includes_setup, settings.rules_stage == "core")
            # Missing facts are routed by a runtime planner to any site, even if its role differs.
            for round_index in range(3):
                if calc.complete or time.monotonic() >= freeze_at - 20:
                    break
                bus.emit("phase", "orchestrator", {"phase": "followup", "gaps": calc.gaps})
                prompt = "Assign targeted follow-up investigations to site workers. Return JSON object mapping roles to task strings. Available roles: " + ", ".join(agents) + "\nGaps: " + json.dumps(calc.gaps) + "\nShared state: " + json.dumps(shared, default=str)
                try:
                    reply = await asyncio.wait_for(model.complete("planner", [{"role": "system", "content": "You coordinate missing evidence. Return only JSON. Route by observed affordances and task context; facts may live on any site."}, {"role": "user", "content": prompt}], max_tokens=1500), timeout=max(1, freeze_at - time.monotonic() - 10))
                except asyncio.TimeoutError:
                    break
                try:
                    assignments = json.loads((reply.content or "").removeprefix("```json").removesuffix("```").strip())
                except ValueError:
                    break
                followups = [asyncio.create_task(agents[role].run(task, max_steps=25, until=freeze_at)) for role, task in assignments.items() if role in agents]
                if followups:
                    _, unfinished = await asyncio.wait(followups, timeout=max(0, freeze_at - time.monotonic()))
                    for task in unfinished:
                        task.cancel()
                    await asyncio.gather(*followups, return_exceptions=True)
                calc = calculate(ledger, context, settings.surcharge_includes_setup, settings.rules_stage == "core")
            for step in calc.trace:
                bus.emit("trace_step", "rules", step)
            answer = validate_answer(calc.answer, run["answer_schema"])
            bus.emit("answer_frozen", "orchestrator", {"answer": answer, "gaps": calc.gaps, "complete": calc.complete})
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
