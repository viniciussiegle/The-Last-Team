"""Your agent's entry point: agent.json runs `uv run --no-sync python agent.py`, in the sandbox and on your machine.

solve() runs three phases:
1. The master (Qwen, no website access) turns the brief into goals per website, while the workers' browsers
   already sign in and clear the start-up obstacles.
2. One worker per website (its own browser, at most 4) gathers typed evidence: Clef is the click policy, Qwen reads
   pages, types searches and solves gates. Workers share facts on a board, so one website's facts steer another's
   searches.
3. The master decides the bid from the evidence (the brief's formulas computed exactly by pricing.py, then a Qwen
   review), enters it in the submission website and signs it with the TAN the mail worker fetches.
Docs: $API/docs/building-an-agent.md, $API/docs/run-contract.md, and the rules: $API/docs/fair-play.md
"""
import asyncio
import json
import re
import sys
from urllib.parse import urlparse

from playwright.async_api import async_playwright

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
    """One browser per website (the run's browsers over CDP; locally, Chromiums of our own)."""
    count = max(1, len(browsers.cdp_urls())) if browsers.cdp_urls() else len(task["access"])
    tabs = {}
    for index, site in enumerate(task["access"]):
        if index == 0:
            context, site_page = page.context, page
        elif index < count and PLAYWRIGHT is not None:
            _, site_page = await browsers.connect(PLAYWRIGHT, min(index, 3))
            context = site_page.context
        else:  # more websites than browsers: a separate context in browser 1
            context = await page.context.browser.new_context()
            site_page = await context.new_page()
        await site_page.set_viewport_size({"width": 1280, "height": 800})
        tabs[site["siteId"]] = Tab(site, context, site_page, log)
    return tabs


def emails_from(board: Board, mail_site: str | None) -> list[str]:
    """Addresses the mailbox worker noted, in the order it noted them (its own address first, as asked)."""
    notes = [note for note in board.notes if note.startswith(f"[{mail_site}]")] or board.notes
    return list(dict.fromkeys(re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "\n".join(notes))))


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


async def solve(task: dict, page) -> dict | None:
    start = llm.clock()
    end = start + min(contract.seconds_left(task) - 50, 19.2 * 60)
    research_end = min(start + 6 * 60, end - 6.5 * 60)
    brief = brief_without_credentials(task["brief"])
    sites = [{"siteId": site["siteId"], "url": site["url"]} for site in task["access"]]
    system = COMMON.format(brief=brief, sites=json.dumps(sites))
    board = Board()
    tabs = await open_tabs(task, page)
    workers = {site["siteId"]: Worker(site, tabs[site["siteId"]], board, system, log) for site in task["access"]}
    master = Master(system, sites, log)

    # Phase 1: the master plans while the workers sign in (obstacles need no plan).
    running = {site_id: asyncio.create_task(worker.run(research_end)) for site_id, worker in workers.items()}
    plan = await master.make_plan()
    for site_id, worker in workers.items():
        worker.assign(plan["goals"].get(site_id, []), plan["share"].get(site_id))
    log(f"[master] plan ready after {llm.clock() - start:.0f}s")
    site_answer = task["submission"].get("kind") != "api"
    portal = submission_site(task) if site_answer else None
    mail_site = plan.get("mailbox_site") if plan.get("mailbox_site") in workers else next(
        (site for site in workers if "mail" in site.lower() or "mail" in workers[site].site["url"].lower()), None)
    submitter = Submitter(tabs[portal], workers.get(mail_site), bid_from(board, plan, brief), plan, brief, [], log) \
        if portal in tabs else None

    async def prelude():
        """Once the submission website's worker is done, open the bid form and enter the bidder data while the
        other workers still research (the form's later steps need the decided bid)."""
        await running[portal]
        if llm.clock() < research_end - 20:
            submitter.update(bid_from(board, plan, brief), emails_from(board, mail_site))
            try:
                await submitter.run(research_end, prelude=True)
            except Exception as error:
                log(f"[submit] prelude failed: {type(error).__name__}: {error}")

    early = asyncio.create_task(prelude()) if submitter else None

    # Phase 2: the workers gather evidence until done or the research deadline.
    await asyncio.wait(running.values(), timeout=max(1, research_end - llm.clock()))
    if early:
        await asyncio.wait([early], timeout=30)
        early.cancel()
    log(f"[master] research finished after {llm.clock() - start:.0f}s; {len(board.evidence.records)} records; {llm.USAGE}")

    # Phase 3: decide the bid, follow up on gaps while time allows, then submit.
    bid = bid_from(board, plan, brief)
    review = {}
    if bid["gaps"] or not bid["answer"]["positions"]:  # a complete draft needs no second opinion
        review = await master.review(board, bid)
        board.add("master", review.get("records") or [], [])
        bid = bid_from(board, plan, brief)
    followups = {site: goal for site, goal in (review.get("followups") or {}).items() if site in workers and goal}
    if followups and bid["gaps"] and end - llm.clock() > 7.5 * 60:
        log(f"[master] follow-ups: {followups}")
        until = min(llm.clock() + 100, end - 6 * 60)
        for site, goal in followups.items():
            workers[site].assign([str(goal)])
        await asyncio.gather(*(workers[site].run(until, max_steps=workers[site].steps + 15) for site in followups))
        bid = bid_from(board, plan, brief)
    log("[master] bid: " + json.dumps(bid["answer"], ensure_ascii=False))
    log("[master] gaps: " + ("; ".join(bid["gaps"]) or "none") + f" | {review.get('comment', '')}")

    if not submitter:
        return bid["answer"]
    submitter.update(bid, emails_from(board, mail_site))
    receipt = await submitter.run(end)
    log(f"[master] {'submitted' if receipt else 'NOT submitted'} after {llm.clock() - start:.0f}s; {llm.USAGE}")
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
