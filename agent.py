"""Attach to the challenge browser and run the model-led RFQ investigation."""
import asyncio
import json
import sys

from playwright.async_api import async_playwright

import browsers
import run as contract
import rfq_agent


async def solve(task: dict, page) -> dict | None:
    """Give the model one tab per live website and let it investigate and submit."""
    pages = {task["access"][0]["siteId"]: page}
    for site in task["access"][1:]:
        pages[site["siteId"]] = await page.context.new_page()
    return await rfq_agent.run(task, pages)


async def main() -> int:
    task, token = contract.load_run()
    sites = ", ".join(f"{site.get('siteId')} {site.get('url')}" for site in task["access"])
    print(f"Run {task.get('run_id') or '(local: the scouting logins, nothing counts)'}; websites: {sites}; "
          f"answer via {task['submission']['kind']}", flush=True)
    async with async_playwright() as playwright:
        browser, page = await browsers.connect(playwright)  # more browsers: browsers.connect(playwright, 1), ...
        answer = await solve(task, page)
    if task["submission"]["kind"] == "site":
        if answer and answer.get("submitted"):
            print("Signed site submission completed", flush=True)
            return 0
        print("No confirmed signed site submission", flush=True)
        return 1
    if answer is None:
        print("No answer to submit", flush=True)
        return 0
    if not token:
        print("Your answer (locally nothing is submitted):", json.dumps(answer, ensure_ascii=False), flush=True)
        return 0
    status, body = contract.submit_answer(task, token, answer)
    print(f"Answer: HTTP {status} {body[:2000]}", flush=True)
    return 0 if status == 202 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
