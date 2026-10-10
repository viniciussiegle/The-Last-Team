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

The Atira CLI may be missing from PATH in a long-lived shell; it lives at
`%LOCALAPPDATA%\atira\bin\atira.cmd`. Run artifacts go to the external data
directory (`RFQ_DATA_DIR`, default `%LOCALAPPDATA%\atira-rfq\runs`), never into
the checkout.
