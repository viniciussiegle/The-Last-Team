"""Your agent's entry point: agent.json runs `uv run --no-sync python agent.py`, in the sandbox and on your machine.

solve() runs three phases, each as short as the evidence allows:
1. The master (Qwen, no website access) turns the brief into goals per website, while the workers' browsers
   already sign in and clear the start-up obstacles.
2. One worker per website (its own browser, at most 4) gathers typed evidence: Clef (clef-flash) is the click
   policy, Qwen reads pages, types searches and solves gates. Workers share facts on a board, so one website's facts
   steer another's searches. Research ends as soon as every worker is done, or earlier once the calculator finds no
   gap and the tender and mail workers are done; a hard deadline and per-worker step, turn and wait budgets bound it.
3. The master decides the bid from the evidence (the brief's formulas computed exactly by pricing.py, a Qwen review
   and one round of follow-ups only when gaps remain), enters it in the submission website and signs it with the TAN
   the mail worker fetches. The submission form is opened and its bidder page filled in a tab of its own as soon as
   the submission website's worker is done, so only the bid's values remain once the bid is decided.
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
from agents import COMMON, Board, Master, Worker, brief_without_credentials
from submit import Submitter
from web import Tab

PLAYWRIGHT = None  # set by main(): solve() attaches the run's other browsers with it


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


def mailbox(plan: dict, workers: dict) -> str | None:
    if plan.get("mailbox_site") in workers:
        return plan["mailbox_site"]
    return next((site for site in workers if "mail" in site.lower() or "mail" in workers[site].site["url"].lower()), None)


def bidder_emails(review: dict, plan: dict, board: Board, mail_site: str | None) -> list[str]:
    """The bidder's own address: the review's or plan's reading first, then addresses the mailbox worker noted."""
    found = [value for value in (review.get("bidder_email"), (plan.get("bidder") or {}).get("email"))
             if isinstance(value, str) and "@" in value]
    notes = [note for note in board.notes if note.startswith(f"[{mail_site}]")] or board.notes
    found += re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "\n".join(notes))
    return list(dict.fromkeys(found))


async def solve(task: dict, page) -> dict | None:
    start = llm.clock()
    trace_dir = Path(os.environ.get("AGENT_TRACE_DIR", ".runs"))
    trace_dir.mkdir(exist_ok=True)
    agents.TRACE = trace_dir / f"trace-{int(time.time())}.jsonl"
    end = start + min(contract.seconds_left(task) - 50, 19.2 * 60)
    research_end = min(start + 7 * 60, end - 6.5 * 60)  # a hard cap: research normally ends when its workers are done
    brief = brief_without_credentials(task["brief"])
    sites = [{"siteId": site["siteId"], "url": site["url"]} for site in task["access"]]
    system = COMMON.format(brief=brief, sites=json.dumps(sites))
    board = Board()
    tabs = await open_tabs(task, page)
    workers = {site["siteId"]: Worker(site, tabs[site["siteId"]], board, system, log) for site in task["access"]}
    master = Master(system, sites, log)

    # Phase 1: the workers start at once with the brief and a standing goal; the master's plan refines their goals
    # when it arrives, without interrupting them.
    for worker in workers.values():
        worker.assign([agents.STANDING_GOAL])
    running = [asyncio.create_task(worker.run(research_end)) for worker in workers.values()]
    plan = await master.make_plan()
    for site_id, worker in workers.items():
        worker.refine(plan["goals"].get(site_id, []) + [f"share early: {item}" for item in plan["share"].get(site_id, [])][:2])
    log(f"{llm.stamp()} [master] plan ready")

    portal = submission_site(task) if task["submission"].get("kind") == "site" else None
    mail_site = mailbox(plan, workers)
    submitter, submitting = None, None

    def start_submission():
        """Open the submission form in a tab of its own and fill what does not depend on the bid."""
        nonlocal submitter, submitting
        if submitting or portal not in tabs:
            return
        submitter = Submitter(Tab(tabs[portal].site, tabs[portal].context, None, log), workers.get(mail_site),
                              plan, brief, log)
        submitting = asyncio.create_task(submitter.run(end, start_url=tabs[portal].page.url))
        log(f"{llm.stamp()} [master] submission form opened while research continues")

    # Phase 2: the workers gather evidence until they are done, the evidence is complete, or the hard deadline.
    jobs = dict(zip(workers, running))
    first = {job: site for site, job in jobs.items()}
    complete_since = None
    while llm.clock() < research_end:
        active = [job for job in running if not job.done()]
        if not active:
            break
        if portal in jobs and not submitting and jobs[portal].done():
            start_submission()
        draft = bid_from(board, plan, brief)
        board.gaps = draft["gaps"]  # every worker sees what the bid still lacks
        essential = [jobs[site] for site in (portal, mail_site) if site in jobs]
        if complete(draft) and all(job.done() for job in essential):
            complete_since = complete_since or llm.clock()
            if llm.clock() - complete_since >= 3:  # a short grace for records still on their way
                log(f"{llm.stamp()} [master] evidence complete; stopping the remaining workers")
                break
        else:
            complete_since = None
        changed = asyncio.ensure_future(board.changed.wait())
        await asyncio.wait(active + [changed], timeout=min(3, max(1, research_end - llm.clock())),
                           return_when=asyncio.FIRST_COMPLETED)
        changed.cancel()
        for job in active:
            if job.done() and job in first:
                log(f"{llm.stamp()} [master] {first[job]} worker finished")
    for job in running:
        job.cancel()
    await asyncio.wait(running, timeout=5)  # let cancelled workers unwind before anyone reuses their tabs
    log(f"{llm.stamp()} [master] research finished; {len(board.evidence.records)} records; {llm.USAGE}")

    # Phase 3: decide the bid. While the calculator still misses facts, every worker gets the open gaps for one
    # more round (the calculator, not a model's judgement, says what is missing) and the master reviews the
    # records in the meantime.
    bid = bid_from(board, plan, brief)
    review = {}
    if bid["gaps"] or not bid["answer"]["positions"]:
        board.gaps = bid["gaps"]
        reviewing = asyncio.create_task(master.review(board, bid))
        if bid["gaps"] and end - llm.clock() > 7 * 60:
            log(f"{llm.stamp()} [master] open gaps go to every worker: " + "; ".join(bid["gaps"])[:600])
            until = min(llm.clock() + 100, end - 6 * 60)
            goal = ("The calculator still misses the facts below. Find and record the ones this website holds, "
                    "reading only what they need; if none of them can be on this website, set done at once.\n"
                    + "\n".join(f"- {gap}" for gap in bid["gaps"][:30]))
            for worker in workers.values():
                worker.assign([goal])
            await asyncio.gather(*(worker.run(until, max_steps=worker.steps + 10) for worker in workers.values()))
        review = await reviewing
        board.add("master", review.get("records") or [], [])
        bid = bid_from(board, plan, brief)
    log(f"{llm.stamp()} [master] bid: " + json.dumps(bid["answer"], ensure_ascii=False))
    log(f"{llm.stamp()} [master] gaps: " + ("; ".join(bid["gaps"]) or "none") + f" | {review.get('comment', '')}")

    if task["submission"].get("kind") == "api":
        return bid["answer"]
    if portal is None:
        log("[master] the submission website is not among the run's websites; nothing submitted")
        return None
    start_submission()
    submitter.set_bid(bid, bidder_emails(review, plan, board, mail_site))
    receipt = await submitting
    log(f"{llm.stamp()} [master] {'submitted' if receipt else 'NOT submitted'}; {llm.USAGE}")
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
