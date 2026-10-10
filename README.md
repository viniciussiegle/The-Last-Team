# The Last Team — RFQ agent

An autonomous agent for the [Atira RFQ challenge](https://ehl-challenge.atira.ai/llms.txt).
The hosted Qwen model chooses which websites, records and documents to inspect,
interprets the evidence, calculates a bid, and completes the portal's TAN signing flow.

## Architecture

| File | Responsibility |
| --- | --- |
| `agent.py` | Load the task, attach to the supplied browser, and create a tab for each site |
| `rfq_agent.py` | Model/tool loop, deadline awareness, evidence memory and submission confirmation |
| `browser_tools.py` | Page observations, browser actions, document extraction and Decimal arithmetic |
| `browsers.py` | Attach over CDP in the sandbox; launch Chromium only for local development |
| `models.py` | Hosted model clients using the platform's proxy |
| `run.py` | Starter kit run contract and API submission plumbing |
| `dev.py` | Load scouting exports as data and start a local run |
| `agent.json` | Reproducible setup and sandbox entry point |

The browser tools expose visible controls, frames, expandable documents and screenshots.
The model selects each navigation, search and document. PDF, ZIP, DOCX and text extraction
is restricted to an observed document URL. Exact arithmetic uses Decimal with explicit
half-up rounding. Previous observations and model-selected notes remain available through
the `recall` and `remember` tools. Browser errors return a fresh observation for recovery.

The agent has a 19-minute working budget, bounded by the actual run deadline. It prompts
the model to finish a signed partial bid when time is short. A saved draft is insufficient:
the agent must see confirmation on the tender portal before reporting completion.

## Generalization to new websites

The package contains no public-scenario answers, tender references, material numbers,
prices, discounts, credentials, or website routes. The run contract supplies site names,
URLs, credentials, submission destination, business rules, and answer schema. The model
discovers controls and document links from the current pages and chooses each action.
Unusual login flows can use model-selected credential inputs. No fixed sequence of clicks
or scenario-specific lookup table drives the investigation.

The reusable strategy is RFQ reasoning: establish current requirements, match materials,
verify commercial evidence, calculate, and sign according to the supplied brief. The
challenge documentation says the final keeps that business task while changing websites
and data. This agent does not assume that arbitrary new business tasks share those rules.

Each run starts with empty evidence, document, and observation memory. Local traces and
scouting records are excluded from packaging. Synthetic tests change hosts, site IDs,
routes, form layouts and credentials, including a login with multiple steps. A separate
optional model test asks the actual agent to complete two small RFQs on generated websites
with fresh material numbers, quantities, prices, lead times, TANs and shuffled form fields.
These checks do not establish accuracy on tomorrow's full challenge. Public scores measure
today's dataset only.

## Setup

Requirements: Python 3.12+, uv, and the authenticated Atira CLI.

```sh
uv sync
uv run playwright install chromium
atira status
```

The sandbox installs the locked dependencies with `uv sync` and supplies Chromium over
CDP. It supplies the task, credentials and hosted model access at runtime.

## Local development

Start the model proxy in one terminal:

```sh
atira models proxy
```

In another terminal, from this directory:

```sh
uv run --no-sync python dev.py
```

`dev.py` parses `atira scout --challenge rfq --env` without evaluating shell code.
It defaults to headless browsing; set `HEADLESS=0` to see the browser.
Scouting submissions do not count toward the challenge.

Runtime traces are saved under `.runs/`; `AGENT_TRACE` overrides the path. Traces can
contain task evidence and must remain excluded from the uploaded package. `.gitignore`
also excludes credentials, the virtual environment, Git and Entire metadata.

On machines with a restricted home directory, uv and Playwright support custom cache
paths through `UV_CACHE_DIR` and `PLAYWRIGHT_BROWSERS_PATH`. Use the same browser path
for installation and execution.

## Test and submit

```sh
uv run --no-sync python -m unittest discover -s tests
RUN_BROWSER_TESTS=1 uv run --no-sync python -m unittest discover -s tests
atira test-run . --challenge rfq --dry-run
atira test-run . --challenge rfq --no-open --follow
atira submit . --challenge rfq --no-open --follow
```

The second test command also exercises Chromium with synthetic pages; it requires the
local Playwright browser installation. The dry run lists the exact package contents. The sandbox test provides the actual
score and browser recording. A scored submission first undergoes the platform's review.

To run the generated RFQs, start the model proxy and set `QWEN_BASE_URL` and
`QWEN_API_KEY` from `atira scout --env`, then run:

```sh
RUN_MODEL_TESTS=1 uv run --no-sync python -m unittest discover -s tests -p test_transfer.py
```

This optional test calls the hosted model and allows three minutes per fixture.

```sh
atira status --challenge rfq
atira logs RUN_ID --follow
```

## Challenge documentation

- [RFQ task and rules](https://ehl-challenge.atira.ai/challenges/rfq.md)
- [Building an agent](https://ehl-challenge.atira.ai/docs/building-an-agent.md)
- [Run contract](https://ehl-challenge.atira.ai/docs/run-contract.md)
- [Fair play](https://ehl-challenge.atira.ai/docs/fair-play.md)
- [Submitting](https://ehl-challenge.atira.ai/docs/submitting.md)
