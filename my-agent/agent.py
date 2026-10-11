"""Your agent's entry point: agent.json runs `uv run --no-sync python agent.py`, in the sandbox and on your machine.

solve() runs three phases:
1. The master (Qwen, thinking, no website access) works the brief into a checklist of every fact the answer needs
   and many small tasks, each on one website, while each website's first worker already signs in and clears the
   start-up obstacles.
2. Every task runs as its own worker instance in a tab of its website's browser (one browser per website, at most
   4), with a short prompt. Clef (clef-flash where served) is the click policy; Qwen reads pages, types searches
   and solves gates. Tasks that need another website's values (e.g. one ERP search per catalog group) start once per
   value as soon as it is reported. Research ends when every instance is done, or earlier once the calculator finds
   no gap; a hard deadline and per-instance step, turn and wait budgets bound it.
3. The master (thinking) checks the evidence and the calculator's draft (the brief's formulas computed exactly by
   pricing.py) against the checklist, corrects records and sends follow-up tasks where facts are missing, reviews
   once more, then enters the bid in the submission website and signs it with the TAN the mail worker fetches.
Docs: $API/docs/building-an-agent.md, $API/docs/run-contract.md, and the rules: $API/docs/fair-play.md
"""
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

import agents
import browsers as browsers
import llm
import pricing
import run as contract
from agents import Board, Master, Worker, brief_without_credentials
from submit import Submitter
from web import Tab

PLAYWRIGHT = None  # set by main(): solve() attaches the run's other browsers with it
PER_SITE = 6       # worker instances running at once in one website's browser


def log(text: str):
    print(text if text[:1].isdigit() or text.startswith(" ") else f"{llm.stamp()} {text}", flush=True)


def submission_site(task: dict) -> str | None:
    submission, sites = task["submission"], {site["siteId"]: site for site in task["access"]}
    if submission.get("siteId") in sites:
        return submission["siteId"]
    host = urlparse(submission.get("url", "")).netloc
    return next((key for key, site in sites.items() if urlparse(site["url"]).netloc == host), None)


async def open_tabs(task: dict, page) -> dict[str, Tab]:
    """One browser per website (the run's browsers over CDP; locally, Chromiums of our own), attached in parallel."""
    count = max(1, len(browsers.cdp_urls())) if browsers.cdp_urls() else len(task["access"])

    async def attach(index: int, site: dict) -> Tab:
        if index == 0:
            context, site_page = page.context, page
        elif index < count and PLAYWRIGHT is not None:
            _, site_page = await browsers.connect(PLAYWRIGHT, min(index, 3))
            context = site_page.context
        else:  # more websites than browsers: a separate context in browser 1
            context = await page.context.browser.new_context()
            site_page = await context.new_page()
        await site_page.set_viewport_size({"width": 1280, "height": 800})
        return Tab(site, context, site_page, log)

    tabs = await asyncio.gather(*(attach(index, site) for index, site in enumerate(task["access"])))
    return {site["siteId"]: tab for site, tab in zip(task["access"], tabs)}


def bid_from(board: Board, plan: dict, brief: str) -> dict:
    reference = plan.get("reference") or ""
    scenario = plan.get("scenario_date") or ""
    if not reference:
        found = re.search(r"\(([A-Z]{1,6}-\d{4}-\d{2,6})\)", brief)
        reference = found.group(1) if found else ""
    try:
        return pricing.calculate_bid(board.evidence, reference, scenario)
    except Exception as error:  # malformed evidence must not stop the signed submission
        log(f"[master] calculator error: {type(error).__name__}: {error}")
        return {"answer": {"tender_reference": reference, "decision": "bid", "currency": "EUR",
                           "discount_percent": 0.0, "positions": [], "total_net": 0.0},
                "gaps": [f"calculator error: {error}"], "details": [], "omitted": []}


def complete(bid: dict) -> bool:
    """The calculator found every value of every position: nothing left that more research could add."""
    positions = bid["answer"]["positions"]
    return not bid["gaps"] and bool(positions) and all(
        all(key in entry for key in ("lead_time_weeks", "base_unit_price", "surcharge_per_unit", "packaging_per_unit",
                                     "rebate_total")) for entry in positions)


def mailbox(plan: dict, sites: list[str], tabs: dict) -> str | None:
    if plan.get("mailbox_site") in sites:
        return plan["mailbox_site"]
    return next((site for site in sites if "mail" in site.lower() or "mail" in tabs[site].site["url"].lower()), None)


def bidder_emails(review: dict, plan: dict, board: Board, mail_site: str | None) -> list[str]:
    """The bidder's own address: the review's or plan's reading first, then addresses the mailbox workers noted."""
    found = [value for value in (review.get("bidder_email"), (plan.get("bidder") or {}).get("email"))
             if isinstance(value, str) and "@" in value]
    notes = [note for note in board.notes if mail_site and note.startswith(f"[{mail_site}")] or board.notes
    found += re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "\n".join(notes))
    return list(dict.fromkeys(found))


def task_list(items) -> list[dict]:
    """Follow-ups as tasks: a list of {site, goal, kinds}, or the older {site: goal} mapping."""
    if isinstance(items, dict):
        items = [{"site": site, "goal": goal} for site, goal in items.items()]
    return [item for item in items or [] if isinstance(item, dict) and item.get("site") and item.get("goal")]


class Crew:
    """All worker instances of a run: one first worker per website (its browser's page) plus a tab per task."""

    def __init__(self, tabs: dict[str, Tab], board: Board):
        self.tabs, self.board = tabs, board
        self.first: dict[str, Worker] = {}
        self.workers: list[Worker] = []
        self.jobs: list[asyncio.Task] = []
        self.slots = {site: asyncio.Semaphore(PER_SITE) for site in tabs}
        self.context = ""
        self.until = 0.0
        for site, tab in tabs.items():
            worker = Worker(tab.site, tab, board, "", log)
            worker.spawn = self.delegate
            self.first[site] = worker
            self.workers.append(worker)

    def start_first(self, until: float):
        self.until = until
        for worker in self.first.values():
            self.jobs.append(asyncio.create_task(worker.run(until)))

    def site_done(self, site: str) -> bool:
        return all(job.done() for worker, job in zip(self.workers, self.jobs) if worker.id == site)

    def active(self) -> list[asyncio.Task]:
        return [job for job in self.jobs if not job.done()]

    def launch(self, site: str, goal: str, inputs: str = "", name: str | None = None, until: float | None = None,
               max_steps: int = 30) -> Worker:
        """A new worker instance for one task, in a new tab of its website's browser."""
        number = sum(1 for worker in self.workers if worker.id == site)
        first = self.first[site]
        worker = Worker(first.site, None, self.board, self.context, log, name=name or f"{site}#{number}")
        worker.spawn = self.delegate
        worker.assign([goal], inputs=inputs)
        self.workers.append(worker)

        async def go():
            async with self.slots[site]:
                try:  # the first worker signs in and passes the gates once for the whole session
                    await asyncio.wait_for(first.ready.wait(), 25)
                except asyncio.TimeoutError:
                    pass
                worker.tab = Tab(first.site, first.tab.context, None, log)
                await worker.tab.open(first.site["url"])
                await worker.run(until or self.until, max_steps=max_steps)
        self.jobs.append(asyncio.create_task(go()))
        log(f"[master] started {worker.name}: {goal[:300]}")
        return worker

    async def delegate(self, parent: Worker, goal: str) -> str:
        mine = [worker for worker in self.workers if worker.name.startswith(parent.name + ">")]
        if len(mine) >= 2 or len(goal) < 15 or self.until - llm.clock() < 60:
            return "no helper available: do it yourself"
        self.launch(parent.id, goal, parent.inputs, name=f"{parent.name}>{len(mine) + 1}", max_steps=25)
        return f"helper started: {goal[:120]}"


async def solve(task: dict, page) -> dict | None:
    start = llm.clock()
    trace_dir = Path(os.environ.get("AGENT_TRACE_DIR", ".runs"))
    trace_dir.mkdir(exist_ok=True)
    agents.TRACE = trace_dir / f"trace-{int(time.time())}.jsonl"
    end = start + min(contract.seconds_left(task) - 50, 19.2 * 60)
    research_end = min(start + 8 * 60, end - 7 * 60)  # a hard cap: research normally ends when its workers are done
    brief = brief_without_credentials(task["brief"])
    sites = [{"siteId": site["siteId"], "url": site["url"]} for site in task["access"]]
    board = Board()
    tabs = await open_tabs(task, page)
    crew = Crew(tabs, board)
    master = Master(brief, sites, log)

    # Phase 1: the master thinks the plan through while each website's first worker signs in.
    crew.start_first(research_end)
    plan = await master.make_plan()
    bidder = plan.get("bidder") or {}
    crew.context = (f"Tender reference: {plan.get('reference') or '?'} (exact) · scenario date: "
                    f"{plan.get('scenario_date') or '?'} · bidder: {bidder.get('company') or '?'}"
                    + (f", mail domain {bidder['mail_domain']}" if bidder.get("mail_domain") else ""))
    fanouts = []
    for item in plan["tasks"]:
        if item["for_each"]:
            fanouts.append({**item, "seen": set()})
        elif not crew.first[item["site"]].goals:
            crew.first[item["site"]].context = crew.context
            crew.first[item["site"]].assign([str(item["goal"])])
        else:
            crew.launch(item["site"], str(item["goal"]))
    for worker in crew.first.values():
        worker.context = crew.context
        worker.planned.set()  # a website without a task of its own just finishes its warm-up
    log(f"[master] plan ready; {len(crew.workers)} workers, {len(fanouts)} fan-outs")

    def fan_out():
        for item in fanouts:
            for value, line in board.values(item["for_each"]["kind"], item["for_each"]["field"]):
                key = pricing.norm_ref(value)
                if key not in item["seen"]:
                    item["seen"].add(key)
                    crew.launch(item["site"], str(item["goal"]).replace("{value}", value),
                                inputs=f"{item['for_each']['field']} = {value}\nreported as: {line}")

    portal = submission_site(task) if task["submission"].get("kind") == "site" else None
    mail_site = mailbox(plan, list(tabs), tabs)
    submitter, submitting = None, None

    def start_submission():
        """Open the submission form in a tab of its own and fill what does not depend on the bid."""
        nonlocal submitter, submitting
        if submitting or portal not in tabs:
            return
        submitter = Submitter(Tab(tabs[portal].site, tabs[portal].context, None, log), crew.first.get(mail_site),
                              plan, brief, log)
        submitting = asyncio.create_task(submitter.run(end, start_url=tabs[portal].page.url))
        log(f"[master] submission form opened while research continues")

    # Phase 2: the instances gather evidence until they are done, the evidence is complete, or the hard deadline.
    complete_since = None
    while llm.clock() < research_end:
        fan_out()
        active = crew.active()
        if not active:
            break
        if portal and not submitting and crew.site_done(portal):
            start_submission()
        draft = bid_from(board, plan, brief)
        if complete(draft) and all(crew.site_done(site) for site in (portal, mail_site) if site):
            complete_since = complete_since or llm.clock()
            if llm.clock() - complete_since >= 8:  # a short grace for records still on their way
                log("[master] evidence complete; stopping the remaining workers")
                break
        else:
            complete_since = None
        changed = asyncio.ensure_future(board.changed.wait())
        await asyncio.wait(active + [changed], timeout=min(10, max(1, research_end - llm.clock())),
                           return_when=asyncio.FIRST_COMPLETED)
        changed.cancel()
    for job in crew.jobs:
        job.cancel()
    await asyncio.wait(crew.jobs, timeout=5)  # let cancelled workers unwind before anyone reuses their tabs
    log(f"[master] research finished; {len(board.evidence.records)} records from {len(crew.workers)} workers; {llm.USAGE}")

    # Phase 3: the master reviews (thinking), follows up on missing facts while time allows, and reviews again.
    bid = bid_from(board, plan, brief)
    review = await master.review(board, bid)
    board.add("master", review.get("records") or [], [])
    bid = bid_from(board, plan, brief)
    followups = [item for item in task_list(review.get("followups")) if item["site"] in tabs]
    if followups and end - llm.clock() > 7.5 * 60:
        until = min(llm.clock() + 150, end - 6 * 60)
        crew.until = until
        for item in followups[:10]:
            crew.launch(item["site"], str(item["goal"]), until=until, max_steps=25)
        while llm.clock() < until and crew.active():
            fan_out()
            await asyncio.wait(crew.active(), timeout=min(10, max(1, until - llm.clock())),
                               return_when=asyncio.FIRST_COMPLETED)
        for job in crew.jobs:
            job.cancel()
        await asyncio.wait(crew.jobs, timeout=5)
        bid = bid_from(board, plan, brief)
        if end - llm.clock() > 5.5 * 60:
            final = await master.review(board, bid)
            board.add("master", final.get("records") or [], [])
            review = {**review, **{key: value for key, value in final.items() if value}}
            bid = bid_from(board, plan, brief)
    log("[master] bid: " + json.dumps(bid["answer"], ensure_ascii=False))
    log("[master] gaps: " + ("; ".join(bid["gaps"]) or "none") + f" | {review.get('comment', '')}")

    if task["submission"].get("kind") == "api":
        return bid["answer"]
    if portal is None:
        log("[master] the submission website is not among the run's websites; nothing submitted")
        return None
    start_submission()
    submitter.set_bid(bid, bidder_emails(review, plan, board, mail_site))
    receipt = await submitting
    log(f"[master] {'submitted' if receipt else 'NOT submitted'}; {llm.USAGE}")
    return None


async def main() -> int:
    global PLAYWRIGHT
    task, token = contract.load_run()
    sites = ", ".join(f"{site.get('siteId')} {site.get('url')}" for site in task["access"])
    print(f"Run {task.get('run_id') or '(local: the scouting logins, nothing counts)'}; websites: {sites}; "
          f"answer via {task['submission']['kind']}", flush=True)
    async with async_playwright() as playwright:
        PLAYWRIGHT = playwright
        browser, page = await browsers.connect(playwright)  # more browsers: browsers.connect(playwright, 1), ...
        answer = await solve(task, page)
    if answer is None:
        print("No answer to submit" + (" through the API: the bid was entered on the website"
                                       if task["submission"].get("kind") == "site" else ""), flush=True)
        return 0
    if not token:
        print("Your answer (locally nothing is submitted):", json.dumps(answer, ensure_ascii=False), flush=True)
        return 0
    status, body = contract.submit_answer(task, token, answer)
    print(f"Answer: HTTP {status} {body[:2000]}", flush=True)
    return 0 if status == 202 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
