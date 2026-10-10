# Runtime architecture and initial contracts

The package extends the supplied run, browser and model plumbing. Runtime
observations go to four model-driven browser workers and an append-only typed
ledger. A deterministic resolver selects evidence; Decimal rules compute the
answer. A model-driven submitter uses the target website and signing mailbox.
The deadline freezes the best validated answer early enough to submit.

## M0 contracts

- `rfq.config.Settings.from_env(env=None) -> Settings`: dataclass with
  `api_url: str | None`, `run_token: str | None`, `scouting: dict | None`,
  `cdp_urls: list[str]`, `headless: bool`, `data_dir: Path`,
  `submit_margin_s: int = 300`, `max_steps: int = 70`,
  `run_budget_s: int = 1100`, `qwen_model: str = "qwen3.8-flash-next"`,
  `frontier_enabled: bool = False`, `frontier_model: str | None = None`.
  Reject frontier enablement for now. Refuse data_dir inside the checkout.
- `rfq.platform.load_run(settings) -> dict`: synchronous authenticated run
  fetch with retries or scouting dict. Preserve unknown platform fields.
  `seconds_left(run, now=None) -> float` handles epoch milliseconds.
  `submit_api_answer(run, token, answer) -> tuple[int,str]` is available only
  when submission.kind is api. No package/submission creation methods.
- `rfq.telemetry.events.EventBus(data_dir, run_id)` creates an external run
  directory; `emit(type: str, agent: str, data: dict) -> dict` adds UTC time,
  run_id, type, agent and data, writes JSONL and prints `@@EVT ` JSON.
  Serialize Decimal/Path/date safely; scrub credential/key/password fields.
- `python -m rfq --dry` loads the task and prints a credentials-redacted run
  summary plus runtime brief. Normal execution will call the orchestrator.
- `scripts/run_local.py [--dry] [--orient] [--budget-s N]` obtains CLI scout
  environment without executing arbitrary shell text, then invokes package
  Python. Use `ATIRA_CLI` override or PATH or the standard local Windows CLI.
- `scripts/fairplay_check.py [package_dir]` scans shipped text for scenario
  reference patterns, long standalone material digit strings, secret literals,
  TAN literals, public website hostnames and scouting usernames. Exit nonzero
  with file/line/reason, never echo matching sensitive content. Exclude lockfile
  numeric checks and only allow documented generic code/schema constants.
- `scripts/package.py [--out PATH]` invokes fairplay first and creates a tar.gz
  with agent.json at archive root, regular files only, excluding environments,
  caches, .env, runtime data and secrets. Default output is external data_dir.

## M1 browser/agent contracts

- BrowserSession holds page/context/browser, allowed site origin, element map,
  latest snapshot, pending dialogs and popup observations. connect uses the
  supplied CDP browser for its index, launching Chromium only locally.
- `observe(session, max_chars=26000) -> Snapshot` traverses every frame and
  binds numbered visible elements. Snapshot contains text, digest and elements.
- `act(session, action: dict) -> str` accepts generic actions using observed
  element IDs. Never accepts arbitrary JavaScript from the model.
- `BrowserAgent.run(task, max_steps=None) -> dict` repeatedly observes, calls
  Qwen, executes selected tools and validates record tools. It shares ledger
  updates and task state. Prompts contain roles, rules and generic vocabulary;
  scenario text is appended only from the current run at runtime.
- Event types: run_started, phase, agent_step, relogin, retry, escalation,
  record_added, record_rejected, trace_step, answer_frozen, submit_step,
  run_finished. Step thought summaries are concise decision explanations.

Evidence and rule interfaces will be specified before their implementation.

## Evidence and calculation contracts

`Ledger(bus).add(kind, data, session, agent, snippet)` validates the named
Pydantic record, attaches browser provenance and appends it. Validation errors
are returned to the model. `of(kind)` returns records; `summary()` serializes
current facts, with no disk store inside the checkout. Record kinds and exact
fields are defined in `rfq/evidence/models.py`; decimal strings are preferred.

`resolve(ledger, context) -> Resolved` selects exact-reference authorized mail
records at/before scenario time, the current spec, unwithdrawn quantities and
revision-scoped amendments. It preserves rejected-record reasons.
`calculate(ledger, context, include_setup_in_surcharge=True) -> Calculation`
returns `answer`, `trace`, `gaps`, and `complete`. Unknown is never zero.
`context` is extracted from this run's brief by Qwen and includes reference,
scenario_date, bidder_domain (when known), authorized_senders by mail kind,
and site-role assignments. No scenario-derived context ships as a constant.

Initial M2 configuration uses `RULES_STAGE=core`: evidence-based material,
quantity, converted base price, approval and shared-stock delivery; its quoted
unit price applies the approved discount to base and keeps totals consistent.
Commercial adjustment breakdowns are absent. `RULES_STAGE=full` enables the
complete adjustment pipeline for later milestone verification. Both paths
retain missing-input diagnostics; neither invents optional breakdown values.
`SURCHARGE_INCLUDES_SETUP=true` is the initial interpretation to test later.

Browser documents: `async open_document(session, element_id) -> str` downloads
only a model-selected observed link/control. Resolve relative href against its
frame URL; restrict to session.allowed_origins. Use context.request.get for
observed direct document URLs or Playwright expect_download for controls.
Parse PDFs, ZIP members, HTML and text in memory, limits 15 MB / 30 members /
100,000 characters. Never traverse archive paths or write documents in repo.
