# Claude Code working notes

@AGENTS.md

The build plan is CODEX_BUILD_PROMPT.md (written for Codex, now executed by
Claude Code). Interfaces are in ARCHITECTURE.md, status in PROGRESS.md.

## Overrides from the human (2026-10-10) — these win over AGENTS.md and the brief

- Work on the local branch `claude-build`. Leave `main` untouched. Never push,
  fetch, pull or otherwise touch a remote. Commit locally after each working
  step; update PROGRESS.md after each milestone.
- Models: Qwen3.8-Flash-Next plus Clef only (hosted, free). There is no OpenAI
  access and no `OPENAI_API_KEY` team secret. The frontier escalation path and
  brief checkpoints 2, 3 and 6 are dropped. Keep the model router
  provider-agnostic (one interface, config-selected model) so another model
  with a proxy key could be plugged in later without code changes.
- Where the brief says "escalate to frontier": re-prompt Qwen with the
  validation error and a focused excerpt, at most two retries, then mark the
  field unknown and leave it blank. Never guess.
- Remaining checkpoints (stop and ask): before every scored submission; a rule
  ambiguity unresolved after two A/B runs; any fair-play doubt.
- Ask the human directly whenever something is ambiguous.
- Working system first (human, 2026-10-10): until a sandbox run scores, do not
  spend time or tokens on holdout gates, generality evaluations or new unit-test
  suites. Sandbox test runs against the real sites are the test loop. Keep the
  existing quick suite green and add tests only where they directly unblock a
  fix. The generality gates in AGENTS.md resume once a run is eligible.
- Keep it simple and light (human, 2026-10-10, said twice): few tests, few
  safeguards, no checking of everything. A safeguard added that day (replacing
  a "stuck" tab) misfired and trapped a worker in a loop, which is the cost of
  piling them on. Prefer one small, smart mechanism over several defensive
  layers, and verify on live runs. Elegance of the system is judged.

- Test runs are allowed again (human, 2026-10-11, later that day): "if we have a
  strong candidate, make a test run on the platform and see where it went
  wrong". At most four at once for the whole team. A scored submission still
  needs the human's explicit yes for that package.
- The work now is the fast line, not `claude-build` (human, 2026-10-11): take the
  teammate's compact Qwen and Clef package that scored 98.7, make it reliable
  and much faster, aiming at the top five by cost × time. It lives on the local
  branch `claude-fast`, checked out next to this copy in `../The-Last-Team-fast`.
- Fixes must be general mechanisms, never special cases (human, 2026-10-11): "make
  sure it doesn't end up becoming a leaking bucket problem where fixing one
  issue creates two more". Measure each on a run before stacking the next.
- What is submitted: test run "n1-8160c8a" (commit `8160c8a` of `claude-fast`,
  package `56188bda1b24`), pushed by the human on 2026-10-11: quality 100,
  $0.0667, 02:11, 8.796 $·s. Four of four test runs of that package scored 100.
  It goes into the final run until a later submission passes review. Do not
  propose replacing it unless a candidate beats it over several test runs
  (earlier commits managed about two runs in three).
- GitHub (human, 2026-10-11): the human pushes, the agent does not (its shell
  is refused a push). `claude-fast` is on `origin` with n1's code. This branch
  goes up with `git push origin claude-build`, run by the human; Entire's push
  hook then uploads the session checkpoints with it. Fetching or pulling still
  needs the human's say.
- Fair play (human, 2026-10-11): get the best results first, then revisit the
  helper that opens views the model names by label before any submission. The
  fast line opens the target of a link a page has disabled; the review of the
  submitted package passed with that in it and without a note.

## The fast line

`../The-Last-Team-fast` (branch `claude-fast`) is its own package at the top
level: `agent.py` (orchestration), `agents.py` (master, workers, prompts),
`web.py` (browser layer), `submit.py` (form filler and signing), `pricing.py`
(calculator and record guide), `llm.py`. Tools for it are outside every
repository, in `../The-Last-Team-run-report/tools`: `test_run.py <working copy>
<label> [commit]` starts a sandbox test run of a commit and keeps its whole
log, `live_view.py <labels>` summarises runs, `smoke_web.py`, `smoke_modal.py`
check the browser layer against a made-up local site. Run them with the
package interpreter of this copy.

## The team's 78.1 agent

A teammate's agent scores 78.1. Its whole loss is four base prices, caused by
its number parser joining every digit of a value that carries a note. The fix
is on the local branch `claude-patch-78`, checked out as a separate working
copy next to this one (`../The-Last-Team-78`): one commit importing the scored
package, one commit with the fix and two tests. It has not been test-run.

## Roles

The main session is Astra: plan, interfaces, resolver, rules engine, agent
loop, prompts, guards, submitter, watchdog, test-run diagnosis, review of every
Luna diff, commits and checkpoints. Well-specified implementation tasks go to
the `luna` subagent (.claude/agents/luna.md), one task at a time, each with
goal, files to touch, interface, acceptance check and files not to touch.
Luna never commits and never starts runs.

## Current milestone

M2 — thin end-to-end. M0 and M1 are done. Gate: the first sandbox test run
that is eligible (right reference, bid/no_bid decision, all positions,
quantities, consistent total). Earlier sandbox runs ended without an answer,
so signing and submission inside the deadline is the open problem. Reach this
gate before any polish, then M3 to M6 in order.

## Commands (run from the repository root)

Use the package interpreter. On Linux replace the path with
`my-agent/.venv/bin/python`.

```powershell
.\my-agent\.venv\Scripts\python.exe -m unittest discover -s tests   # all synthetic tests
.\my-agent\.venv\Scripts\python.exe -m unittest tests.rules.test_engine -v   # one module
.\my-agent\.venv\Scripts\python.exe scripts/fairplay_check.py        # before every package
.\my-agent\.venv\Scripts\python.exe scripts/package.py               # fairplay + tarball outside the checkout
.\my-agent\.venv\Scripts\python.exe scripts/run_local.py --dry       # parsed run and brief, redacted
.\my-agent\.venv\Scripts\python.exe scripts/run_local.py --orient    # log in and summarise each site
```

Sandbox test run (authorised, at most four at once): run the fair-play check,
then use the atira MCP tools `test_run`, `wait_for_run`, `diagnose_run`,
`run_logs` and `run_screenshot`, or `atira test-run .\my-agent --challenge rfq`.
Never call `atira submit` or the MCP submit tool without explicit approval.

Entire records the agent sessions of this repository. Its commit hook asks on
the console whether to link a commit to the session, which an agent's shell
cannot answer, so `.entire/settings.local.json` (not in git) sets
`"commit_linking": "always"`. Without that file a commit made from an agent
session hangs in the hook. The session is attached to this working copy, so
commits made in `../The-Last-Team-fast` carry no checkpoint.

The Atira CLI may be missing from PATH in a long-lived shell; it lives at
`%LOCALAPPDATA%\atira\bin\atira.cmd`. Run artifacts go to the external data
directory (`RFQ_DATA_DIR`, default `%LOCALAPPDATA%\atira-rfq\runs`), never into
the checkout.
