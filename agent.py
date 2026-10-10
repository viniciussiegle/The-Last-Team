"""Attach to the challenge browsers and run the model-led RFQ investigation."""
import asyncio
import json
import sys

from playwright.async_api import async_playwright

import browsers
import run as contract
import rfq_agent


def site_contexts(playwright, task: dict) -> dict:
    """Each website gets one browser (its tabs share the session); sites wrap around the run's browsers.
    Browsers are attached on first use."""
    count = min(max(1, len(browsers.cdp_urls()) or 4), len(task["access"]))
    attached = {}
    lock = asyncio.Lock()

    def factory(index):
        async def new_page():
            async with lock:
                if index not in attached:
                    _, page = await browsers.connect(playwright, index)
                    attached[index] = page
                    return page
            return await attached[index].context.new_page()
        return new_page
    return {site["siteId"]: factory(i % count) for i, site in enumerate(task["access"])}


async def solve(task: dict, contexts: dict) -> dict | None:
    """Let the model investigate the live websites in parallel and submit."""
    return await rfq_agent.run(task, contexts)


async def main() -> int:
    task, token = contract.load_run()
    sites = ", ".join(f"{site.get('siteId')} {site.get('url')}" for site in task["access"])
    print(f"Run {task.get('run_id') or '(local: the scouting logins, nothing counts)'}; websites: {sites}; "
          f"answer via {task['submission']['kind']}", flush=True)
    async with async_playwright() as playwright:
        answer = await solve(task, site_contexts(playwright, task))
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
