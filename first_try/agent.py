"""Your agent's entry point: agent.json runs `uv run --no-sync python agent.py`, in the sandbox and on your machine.

The kit only does the plumbing: run.py reads the run and submits an answer, browsers.py attaches to the run's
browsers, models.py calls the hosted Qwen and Clef. Everything else is yours to build, in `solve` below.
Docs: $API/docs/building-an-agent.md, $API/docs/run-contract.md, and the rules: $API/docs/fair-play.md
"""
import asyncio
import json
import re
import sys

from playwright.async_api import async_playwright

try:  # Package imports when tested/imported; script imports when run directly.
    from . import browsers, models, run as contract
except ImportError:  # pragma: no cover - exercised by `python agent.py`.
    import browsers
    import models  # noqa: F401  models.ask_qwen, models.ask_clef: see models.py
    import run as contract


async def solve(task: dict, page) -> dict | None:
    """Research the supplied sites with a master/worker, five-phase workflow.

    `task` is the run: task["brief"] (the task and its rules), task["access"] (each website's url and login),
    task["submission"] (where the answer goes) and task["answer_schema"]. `page` is a Playwright page in browser 1.

    For an "api" submission, return the answer and main() submits it. For a "site" submission, enter the answer on
    that website yourself, then return None.

    Mind the clock: a scored run has 20 minutes, and `contract.seconds_left(task)` says how many are left. Every part
    of the answer scores on its own, so stop and submit your best answer about a minute before the end; an answer
    that comes too late scores 0.
    """

    # The prompts deliberately keep the phases separate.  This makes the final
    # synthesis useful even when one site is unavailable or when an answer is
    # only obtainable by joining facts from two different sites.
    brief = task.get("brief", "")
    sites = task.get("access", [])
    schema = task.get("answer_schema", {})

    async def ask(prompt: str) -> str:
        """Call the hosted model, while leaving a useful report on failure."""
        try:
            return await models.ask_qwen(prompt)
        except Exception as error:  # a failed worker should not lose all other work
            return f"WORKER ERROR: {type(error).__name__}: {error}"

    def clip(value, limit: int = 14000) -> str:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        return text[:limit] + ("\n...[truncated]" if len(text) > limit else "")

    def parse_json(text: str):
        """Parse JSON from a model response, including a markdown code fence."""
        candidate = text.strip()
        fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", candidate, re.IGNORECASE | re.DOTALL)
        if fenced:
            candidate = fenced.group(1)
        try:
            return json.loads(candidate)
        except (TypeError, ValueError):
            # Models occasionally add a short sentence around otherwise valid JSON.
            starts = [i for i in (candidate.find("{"), candidate.find("[")) if i >= 0]
            if not starts:
                return None
            start = min(starts)
            for end in range(len(candidate), start, -1):
                try:
                    return json.loads(candidate[start:end])
                except ValueError:
                    continue
        return None

    async def inspect_site(site: dict) -> dict:
        """Load a site and collect only information visible to the browser."""
        url = site.get("url", "")
        result = {"siteId": site.get("siteId"), "url": url, "pages": [], "obstacles": []}
        if not url:
            result["obstacles"].append("No URL was supplied")
            return result
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(500)
        except Exception as error:
            result["obstacles"].append(f"Initial navigation failed: {type(error).__name__}: {error}")

        # Many challenge sites use a conventional username/password form.  Do
        # this locally so secrets never need to be put in an LLM prompt.
        credentials = site.get("credentials") or {}
        if credentials:
            try:
                user = page.locator('input[type="email"], input[name*="user" i], input[name*="email" i]').first
                password = page.locator('input[type="password"]').first
                if await user.count() and await password.count():
                    await user.fill(str(credentials.get("username", credentials.get("email", ""))))
                    await password.fill(str(credentials.get("password", "")))
                    submit = page.locator('button[type="submit"], input[type="submit"]').first
                    if await submit.count():
                        await submit.click()
                        await page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception as error:
                result["obstacles"].append(f"Automatic login was inconclusive: {type(error).__name__}: {error}")

        try:
            text = await page.locator("body").inner_text(timeout=10000)
            hrefs = await page.locator("a[href]").evaluate_all(
                "els => els.map(a => ({text: (a.innerText || '').trim(), href: a.href}))"
            )
            links = []
            for link in hrefs:
                href = link.get("href") if isinstance(link, dict) else None
                if href and href.startswith(("http://", "https://")):
                    links.append({"text": (link.get("text") or "")[:200], "url": href})
            result["pages"].append({"url": page.url, "title": await page.title(),
                                    "content": clip(text, 18000), "links": links[:100]})
        except Exception as error:
            result["obstacles"].append(f"Could not read page: {type(error).__name__}: {error}")
        return result

    # Phase 1: the master turns the brief into a concrete research brief.
    master_summary = await ask(f"""You are the master agent, phase 1 of a web research task.
Summarize the task, constraints, required answer fields, and the tools available.
Identify facts that must be looked up and likely joins between different websites.
Do not solve it yet. Return a concise, actionable report.

TASK BRIEF:
{clip(brief)}

ANSWER SCHEMA:
{clip(schema)}

WEBSITES:
{clip([{k: s.get(k) for k in ('siteId', 'url')} for s in sites])}""")

    # Phase 2: independent workers report what each site contains and how it
    # can be searched.  Workers get no credentials, only browser-visible data.
    site_material = [await inspect_site(site) for site in sites]
    async def phase_two_worker(material: dict) -> str:
        return await ask(f"""You are a website worker, phase 2.
Report on this one website for the master agent. Include: relevant provided
content, useful links and navigation/search methods, fields or records that
may answer the task, and obstacles or missing information. Do not invent facts.
The answer may require matching identifiers, names, dates, or amounts with
other websites, so call out join keys explicitly.

MASTER SUMMARY:
{clip(master_summary)}

SITE MATERIAL:
{clip(material)}""")

    # Workers are independent, so dispatch them together to preserve the run's
    # time budget when several websites are supplied.
    phase_two = await asyncio.gather(*(phase_two_worker(material) for material in site_material))

    # Phase 3: the master assigns a site-specific plan, while preserving the
    # cross-site dependencies discovered by the workers.
    phase_three = await ask(f"""You are the master agent, phase 3.
Using the task summary and phase-2 reports, create a separate execution plan
for every website. Each plan must say what to search/click/read, which exact
facts to extract, how to handle obstacles, and how those facts connect to
other sites. Keep the plans factual and prioritize the answer schema.
Return a numbered plan per site; do not produce the final answer yet.

TASK:
{clip(brief)}
SUMMARY:
{clip(master_summary)}
PHASE-2 REPORTS:
{clip(phase_two)}""")

    # Phase 4: workers execute their plans.  They can follow a small number of
    # explicitly planned links, but stay inside the supplied site set.
    allowed_urls = {s.get("url", "") for s in sites}
    async def phase_four_worker(index: int, site: dict) -> str:
        material = site_material[index]
        return await ask(f"""You are an execution worker, phase 4.
Execute the plan for site {site.get('siteId')}. Use the supplied material and
browser-visible evidence. Give a precise report of extracted values, their
source URLs/labels, uncertainty, and any cross-site matches. If the plan asks
for a linked page, explain how it should be reached; do not invent access.

SITE: {clip(site.get('url'))}
ORIGINAL MATERIAL: {clip(material)}
MASTER PLAN: {clip(phase_three)}
OTHER ALLOWED SITE ROOTS: {clip(sorted(allowed_urls))}""")

    phase_four = await asyncio.gather(*(phase_four_worker(i, site) for i, site in enumerate(sites)))

    # Phase 5: one final master call performs the relational join and emits the
    # exact API payload.  It is intentionally the only phase asked for JSON.
    final_prompt = f"""You are the master agent, phase 5 and final synthesis.
Solve the original task from the worker evidence below. Join facts across
websites when identifiers or relationships require it. Resolve conflicts by
preferring direct, specific evidence and state no unsupported facts.

Return ONLY one valid JSON object matching this answer schema. No markdown,
commentary, or extra keys unless the schema permits them. If evidence is
missing, use the schema's appropriate null/empty value rather than guessing.

ORIGINAL TASK:
{clip(brief)}
ANSWER SCHEMA:
{clip(schema)}
PHASE 1 SUMMARY:
{clip(master_summary)}
PHASE 2 REPORTS:
{clip(phase_two)}
PHASE 3 PLAN:
{clip(phase_three)}
PHASE 4 EXECUTION REPORTS:
{clip(phase_four)}"""
    final_text = await ask(final_prompt)
    answer = parse_json(final_text)
    if isinstance(answer, dict):
        return answer

    # A malformed final response is uncommon, but returning an object keeps the
    # API contract intact and makes the failure visible in local runs.
    return {"answer": final_text[:4000]}


async def main() -> int:
    task, token = contract.load_run()
    sites = ", ".join(f"{site.get('siteId')} {site.get('url')}" for site in task["access"])
    print(f"Run {task.get('run_id') or '(local: the scouting logins, nothing counts)'}; websites: {sites}; "
          f"answer via {task['submission']['kind']}", flush=True)
    async with async_playwright() as playwright:
        browser, page = await browsers.connect(playwright)  # more browsers: browsers.connect(playwright, 1), ...
        answer = await solve(task, page)
    if answer is None:
        print("No answer to submit: build your agent in solve() in agent.py", flush=True)
        return 0
    if not token:
        print("Your answer (locally nothing is submitted):", json.dumps(answer, ensure_ascii=False), flush=True)
        return 0
    status, body = contract.submit_answer(task, token, answer)
    print(f"Answer: HTTP {status} {body[:2000]}", flush=True)
    return 0 if status == 202 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

