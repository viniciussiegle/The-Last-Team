# Working agreement

Read CODEX_BUILD_PROMPT.md before building. Astra owns architecture, interfaces,
resolver, rules, prompts, orchestration, snapshots, guards, submission, diagnosis,
milestone commits and human checkpoints. Luna implements one precise delegated
task at a time and returns its diff and checks to Astra; no unreviewed commits.

Only my-agent/ ships. Retain and extend the supplied starter. Python 3.12, uv,
Pydantic v2, Playwright CDP, Decimal and ROUND_HALF_UP are fixed choices.
Scripts must work on native Windows and Linux; use Python for packaging.

## Fair play and data

- A runtime model chooses navigation, searches and relevant documents. Helpers
  may extract a chosen page/document/result set, calculate, validate, retry,
  renew a session and fill forms. No fixed site paths, autonomous evidence
  crawlers or model-only-as-fallback implementations.
- Never persist values observed on public websites anywhere in this repository,
  including ignored files: references, material identifiers, prices, TANs,
  credentials, hostnames, labels, selectors or URLs. Runtime data comes from
  environment, the current run, its brief and observations. Tests are synthetic.
- Store every local run artifact outside the checkout in the configured data
  directory (default under the operating system temporary directory).
- Network access during a run is limited to the platform, model proxy and the
  run's websites. No local models; at most four supplied browsers, using CDP.
- Secrets are environment/team secrets and never enter source or telemetry.
- Never start a scored submission without explicit human approval. Sandbox test
  runs are authorized, with at most four concurrent team tests.
- Run scripts/fairplay_check.py before every package. Only my-agent/ is tarred.
- Frontier stays disabled and FRONTIER_MODEL unset pending human approval.

## Checkpoints

Stop and notify the human before any scored submission; choosing a frontier
model; shipping frontier enabled; unresolved rule ambiguity after two A/B
runs; any fair-play doubt; or half the development frontier budget spent.
After each completed milestone write five lines to PROGRESS.md and commit on
main. Reach the first eligible sandbox test before dashboard polish.

## Generality acceptance

Treat public challenge runs as development data. Never copy their values into
fixtures. Before a holdout evaluation, freeze the package, prompts, model and
configuration hashes; generate fresh synthetic seeds only after freezing.
Store seeds, expected answers, observations and results outside the checkout.
Use independent expected values and varied layouts, labels and information
placement. Exercise the actual model/browser loop, not scripted navigation.
Every used holdout seed is retired. A failure may become a development
regression, but changes must be evaluated on fresh seeds. Do not tune against
a reused holdout or claim that synthetic success guarantees private success.
Gate releases on fairplay plus the documented generality checks; report
navigation failures and variant coverage, not just an aggregate score.

See ARCHITECTURE.md for the current interfaces. Preserve user-owned .codex and
.entire configuration and hooks. Do not repeat login or tooling setup.
