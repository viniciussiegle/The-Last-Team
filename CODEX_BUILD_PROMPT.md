# CODEX BUILD BRIEF — Atira EHL Challenge: "From RFQ to bid" agent

> Lives at the root of the `The-Last-Team` repo. Solo build: one person, one branch (`main`), one package.
>
> **Already done — don't redo:**
> - Atira CLI installed (private Node 24), signed in as captain of "The Last Team" for challenge `rfq`.
> - Atira MCP server configured for Codex.
> - Entire CLI installed with its Codex hooks approved (session checkpoints).
>
> **The submission package is `.\my-agent`** — its `agent.json` is the package root. Only `my-agent/` is ever tarred and uploaded.

---

## How Codex works on this: Astra plans, Luna implements

Two Codex agents share the work.

**Setup check first:**
- If agents named `astra` and `luna` already exist in this Codex setup, use them.
- If not, create them with Codex's native custom-agents mechanism (check the current Codex docs / `codex --help` for the exact config format; project-level config under `.codex/`):
  - `astra` = the strongest available model, high reasoning effort.
  - `luna` = a fast, cheaper model, low reasoning effort.
- If you're unsure which model names are available, **ask me** before creating them.

**Astra — orchestrator, planner, owner of anything that needs judgement:**
- Owns the plan, milestone order, the ⚑ checkpoints and every message to me.
- Writes `AGENTS.md`, the architecture and module interfaces (function signatures, Pydantic models, event schema) **before** handing work to Luna.
- Implements or personally reviews the hard parts:
  - Resolver + Rules Engine (§7) — Astra writes it; Luna may only write its synthetic unit-test fixtures.
  - Agent loop, orchestration and system prompts (§8).
  - Snapshot logic and guards (§5).
  - Submitter flow + deadline watchdog.
- Debugs every test run: `diagnose_run`, `run_logs`, `run_screenshot`, reading score breakdowns, deciding the next change.
- Runs `fairplay_check.py` and reviews every Luna diff before it lands.

**Luna — implementer for well-specified, low-ambiguity tasks:**
- Scaffolding, `pyproject.toml`, `.gitignore`/`.gitattributes`, config loading, env parsing.
- Platform API client from `openapi.json`, telemetry emitter, packaging script, `fairplay_check.py` (to Astra's spec).
- Browser action wrappers, document parsers (ZIP/PDF), German number/date parsing helpers.
- Synthetic test fixtures and unit tests from Astra's spec; self-made HTML snapshot fixtures; mock sites (M5) from Astra's layout spec.
- The entire dashboard (§9), following DESIGN.md.
- README / PROGRESS.md formatting.

**Handoff protocol:**
- Astra gives Luna one task at a time, with: goal, files to touch, interface to implement, acceptance check (a test or command), and files **not** to touch.
- Luna returns a diff + test output and never changes interfaces on its own; if a spec is unclear, Luna asks Astra, not me.
- Independent Luna tasks may run in parallel (e.g. dashboard vs. document parsers).

**Speed rule:** reach the first **eligible test run (M2) as early as possible**, before any polish. Every later milestone builds on a working end-to-end loop.

---

## 0. Mission

Build a **general, model-driven agent system** that turns a public tender into one correct, signed bid.

It works through four German business websites:
- tender portal
- supplier/customer portal
- SAP-style ERP
- webmail

It must stay correct when the websites are **replaced by different software** in the final private run.

**Ranking:**
1. Eligible (right tender reference + right bid/no_bid decision).
2. Quality 0–100.
3. Cost × time.

**Winners are judged on:**
1. Private-dataset run (heaviest).
2. Elegance of the system.
3. Public leaderboard.

**Performance targets:**

| Metric | Target | Current leader (for reference) |
|---|---|---|
| Quality | ≥ 70 | 21.5 |
| Model cost per run | < $0.10 | $0.38 |
| Wall-clock time | < 10 min | ~16 min |

**Optimise in this order:** correctness, generality, speed, cost.

---

## 1. Read these sources first (source of truth, re-read when in doubt)

1. `https://ehl-challenge.atira.ai/llms-full.txt` — every doc in one file.
2. `https://ehl-challenge.atira.ai/challenges/rfq.md` — challenge page, bid rules, answer schema.
3. `https://ehl-challenge.atira.ai/v1/challenges/rfq/scouting` — the live brief JSON (rules + answer_schema + site access shape).
4. `https://ehl-challenge.atira.ai/docs/run-contract.md`, `docs/building-an-agent.md`, `docs/fair-play.md`, `docs/submitting.md`, `docs/api.md`, `openapi.json`.
5. The starter kit: `https://ehl-challenge.atira.ai/starter-kit.tar.gz`. Start from it.

**Where the docs conflict:**
- Trust the **brief** (challenge_version 6) over `docs/scoring.md`. The brief's rule "Scoring in bid cases" is the current scorer.
- Confirm in `run-contract.md` how a `submission.kind == "site"` answer is handed in (portal configurator vs API). Implement exactly what the contract says.

---

## 2. Non-negotiable fair-play rules

Violating any of these gets the package rejected. Write them into `AGENTS.md` so every subagent inherits them.

1. **A model decides at runtime.**
   - It chooses navigation targets, searches and which documents matter.
   - Code helpers may: extract a page the model chose, parse a document the model chose, paginate a result set the model asked for, calculate, validate, retry, re-login and fill forms.
   - **Forbidden:** fixed click paths; crawlers that collect sites and hand everything to one model call; model-as-fallback-only.
2. **Zero hardcoded scenario values anywhere** in the package: code, prompts, comments, tests, fixtures, configs, AGENTS.md, defaults or fallbacks. That covers:
   - tender references
   - material numbers
   - prices, discounts, totals
   - TANs
   - logins
   - public site hostnames
   - site-specific button labels / selectors / URLs

   Everything comes from the run's env (`CHALLENGE_SITE_ACCESS`, `GET /v1/runs/current`) and from the brief at runtime. Facts the brief states for every scenario (e.g. sales director mailbox local part `leitung@`, authorized sender local parts) may be used, but prefer reading them from the brief text at runtime.
3. **Tests use synthetic fixtures only.** Invent your own numbers and IDs. Never paste values observed on the public websites.
4. **Network and browser limits:**
   - No network except the platform API + model proxy + the four websites.
   - No local models.
   - No extra browsers: up to 4 via `agent.json` `"browsers": 4`. Tabs/contexts inside them are fine.
5. **Secrets:** the `OPENAI_API_KEY` lives as a **team secret**, never in the package.
6. **Codex must NEVER run a scored submission (`atira submit` / `POST /submissions`) without explicit human approval.**
   - Test runs are fine to start autonomously (max 4 concurrent per team).
   - Reason: the *latest passing* submission goes into the final run.
7. Build `scripts/fairplay_check.py` and run it before every packaging step. It greps the package for:
   - hostnames of the public sites
   - credential-looking strings
   - tender-reference patterns (e.g. `[A-Z]{2}-\d{4}-\d{3,}`)
   - long digit runs that look like material numbers
   - TAN-like codes
   - the scouting username

   Fail loudly on any hit.

---

## 3. Fixed technical decisions (already made by the human)

| Topic | Decision |
|---|---|
| Language | Python 3.12, `uv`, Pydantic v2, `decimal.Decimal` everywhere money/quantity math happens. |
| Browser | Own thin harness: Playwright `connect_over_cdp` to the sandbox browsers (`CDP_URLS`). Locally, launch own Chromium. **No browser-use / Stagehand libraries.** |
| Observation | DOM-first: cleaned page snapshot with numbered interactive elements, iframes traversed, tables rendered compactly. Screenshot fallback (Qwen is multimodal) when the DOM is uninformative or the model asks to `look()`. |
| Models | **Qwen3.8-Flash-Next = default for everything** (free hosted, $0.15/M in, $0.016/M cached, $0.47/M out). **Cloudflare Clef** = optional cheap yes/no/choice decisions. **OpenAI frontier model = targeted escalation only**, through the run proxy (`OPENAI_BASE_URL` is preset in runs; key as team secret). Model name from env `FRONTIER_MODEL`. |
| Dashboard design | Linear's design language, from `VoltAgent/awesome-design-md` (see §9). |
| Time budget | Overnight. Do all milestones; dashboard polish last. |
| Dev machine | **Native Windows** + VS Code, PowerShell. All repo scripts must be cross-platform Python (no bash-only scripts, no `chmod`, LF line endings enforced via `.gitattributes`). Build the tarball with Python's `tarfile`, not shell `tar`. The sandbox itself is Linux. |

**Frontier budget:** ~€75 of OpenAI hackathon credit, reserved for the agent's frontier fallback (not for Codex itself).
- Enforce a per-run cap (`FRONTIER_MAX_USD_PER_RUN`, default `0.08`).
- Enforce a local dev ledger (`.dev_spend.json`) that hard-stops dev calls at `DEV_FRONTIER_BUDGET_USD` (default `40`).
- Default `FRONTIER_ENABLED=false` until an A/B on test runs proves it raises quality enough to justify cost × time.

---

## 4. Architecture

```
                 ┌──────────────── Orchestrator (code + LLM planner) ─────────────────┐
                 │  owns: run lifecycle, blackboard, deadline watchdog, task queue     │
                 └───────┬───────────────┬───────────────┬───────────────┬─────────────┘
              Browser 1  │    Browser 2  │    Browser 3  │    Browser 4  │
           TenderAgent   │   SpecAgent   │    ErpAgent   │   MailAgent   │   (parallel asyncio)
         (LLM ReAct loop with generic browser tools + record_* tools → typed facts)
                 │               │               │               │
                 └──────► Evidence Ledger (typed records + provenance, append-only) ◄──┘
                                         │
                       Resolver (pure code: latest / exact-reference / ≤ scenario date /
                                 authorized sender / current revision / withdrawals)
                                         │
                       Rules Engine (pure functions, Decimal, ROUND_HALF_UP, full trace)
                                         │  gaps → targeted follow-up tasks back to agents
                       Answer builder + JSON-schema validation
                                         │
                       SubmitterAgent (LLM-driven form filling on tender portal,
                                       TAN from MailAgent at the last moment, confirm dialog)
                                         │
                 Telemetry bus → events.jsonl + stdout "@@EVT {json}" lines → Dashboard
```

### Core principle: the LLM gathers evidence, code does the maths

- Agents never compute prices. They emit **typed records** via tools like `record_qa_entry(...)` and `record_price_condition(...)`, validated by Pydantic. Validation errors go back to the model.
- Agents extract **all candidate records** (every approval mail, every revision, every Q&A entry) with metadata. The **Resolver** applies "latest", "exact reference", "≤ scenario date", "authorized sender", "withdrawn" and "current revision" deterministically.
- This is what defeats look-alike records, superseded revisions and future-dated mails.
- The Rules Engine produces a per-position **calculation trace**: every step's inputs and output. It is shown in the dashboard and written to `notes` (truncated to fit 4000 chars) for debugging.

### Repo layout

Everything that ships lives in `my-agent/`. Dev tools (dashboard, scripts, tests, mock sites, run logs) live at the repo root, outside the package, so they are never uploaded.

```
The-Last-Team/               # repo root (NOT shipped)
  AGENTS.md                  # fair-play rules + conventions for coding agents
  CODEX_BUILD_PROMPT.md      # this file
  PROGRESS.md
  README.md
  .gitignore                 # runs/, .env, .dev_spend.json, *.tar.gz, node_modules, .venv
  dashboard/  scripts/  tests/  runs/   (see below)

my-agent/                    # THE PACKAGE (tar root) — keep existing agent.json, update it
  agent.json                 # {"setup":["uv","sync","--frozen"],"run":["uv","run","python","-m","rfq"],"browsers":4,"models":["qwen","clef"]}
  pyproject.toml / uv.lock
  rfq/
    __main__.py              # entry: load run → orchestrate → submit
    config.py                # all env + tunables (margins, caps, model names)
    platform.py              # run API client (GET /v1/runs/current, brief, answer_schema, expires_at), User-Agent header set
    llm/
      clients.py             # Qwen (OpenAI-compatible), OpenAI frontier, Clef (/v1/decide)
      router.py              # escalation policy + per-run budget guard
      cost.py                # token + simulated-cost accounting per model, per agent
      prompts/               # system prompts as .md files (stable prefixes for caching)
    browser/
      session.py             # attach over CDP (sandbox) / launch (local); one session per site
      snapshot.py            # DOM → compact text with [n] element ids; frames; tables; diff hash
      actions.py             # click, type, select, press, scroll, goto, back, new_tab, look(screenshot)
      guards.py              # cookie walls, error-page retry/backoff, session-expiry relogin, dialogs, self-closing popups
      documents.py           # download model-chosen links; ZIP → files; PDF → text (pdfplumber); nested viewers
      login.py               # generic login: find user/pass fields via snapshot + LLM confirm
    agents/
      base.py                # ReAct loop: observe → think → act; max steps; stuck detection; context compaction
      tender.py              # positions, quantities+units, Q&A (all kinds), deadline (tz), clause keys
      spec.py                # revisions + status, requirements, Kataloggruppe, pieces per set, nutzbarer Weg flags
      erp.py                 # catalogue search by group, material attributes/status, stock, lead times, prices, masses, cartons, setup fee
      mail.py                # inbox + archive search (reference AND topic), approvals, Konditionsregeln, Technikfreigabe, Verpackungstarife, Terminfreigabe, Devisenfixing, Rüstpools, Projektbonus, Preisindizes, TAN
      submitter.py           # register bidder, express interest, configurator, German number format, TAN, confirm
      orchestrator.py        # phases, blackboard, dependency wiring, follow-up task dispatch
    evidence/
      models.py              # Pydantic records (see §6) — every record carries Provenance
      ledger.py              # append-only store + query helpers
      resolver.py            # selection rules (see §7)
    rules/
      money.py               # Decimal helpers, HALF_UP, German number parse/format, unit normalisation
      engine.py              # rule pipeline (see §7), returns Answer + Trace
      allocation.py          # largest-remainder cent allocation (configurable tie-break), marginal bands
    submit/
      answer.py              # build answer dict, validate against runtime answer_schema (jsonschema)
      watchdog.py            # deadline: freeze + submit best answer at expires_at − margin
    telemetry/
      events.py              # emit(type, agent, data) → runs/<id>/events.jsonl + stdout '@@EVT ' lines; screenshots to runs/<id>/shots/

# --- repo root, outside my-agent/ (never shipped) ---
  dashboard/                 # NOT shipped in the submission tarball
    DESIGN.md                # Linear design system (see §9)
    server.py                # FastAPI: list runs, SSE tail of events.jsonl, import sandbox test-run logs via platform API
    web/                     # Vite + React + TypeScript
  scripts/
    run_local.py             # loads `atira scout --env` output, runs agent locally against public sites
    package.py               # fairplay_check on my-agent/ → build agent.tar.gz from my-agent/ only (exclude .venv, __pycache__, .env, runs/)
    fairplay_check.py
    ab_test.py               # start N test runs per config via platform API / atira CLI, collect score breakdowns
  tests/
    rules/                   # synthetic-fixture unit tests for every rule and rounding edge case
    snapshot/                # local HTML fixtures (self-made) for cookie wall, iframe, paged table, ZIP link, login timeout
    mock_sites/              # (M5) self-made adversarial mock sites with DIFFERENT layouts to test generality
```

---

## 5. Browser harness requirements

### Snapshot (`snapshot.py`)
- Visible interactive elements (`a, button, input, select, textarea, [role=button|link|tab|menuitem|option|checkbox|radio], [onclick], [tabindex]`) get stable `[n]` ids, re-assigned per snapshot.
- Render a compact text view:
  - title, URL, headings
  - forms: label → current value
  - tables as pipe rows with header + "page x/y / load more" hints
  - dialogs/popups first
- Strip repeated nav chrome after first view (dedupe by hash).
- Traverse **all frames, including nested iframes**. Prefix frame path.
- Hard cap ~6–8k tokens per snapshot. When truncated, list section anchors the model can `expand(section)`.
- Return a content hash so the loop knows whether an action changed the page. Always re-snapshot after an action; wait for network idle + DOM-stable (short MutationObserver quiet period).

### Actions exposed to the model as tools
- **Navigation and interaction:** `click(id)`, `type(id, text, submit=false)`, `select(id, option)`, `press(key)`, `scroll(dir)`, `goto(url_on_same_site)`, `back()`, `switch_tab(i)`
- **Reading:** `look()` (screenshot to model), `extract_table(id_or_heading, all_pages=false)` (helper paginates only the result set the model selected), `open_document(id)` (download + parse ZIP/PDF/HTML viewer → text)
- **Output:** `record_*` tools (typed facts), `done(summary)`, `need_help(question)` (escalates to orchestrator / frontier)

### Guards (code-level, generic, below the LLM)
- **Cookie/consent walls:** heuristic accept (multilingual labels like "Alle akzeptieren / Akzeptieren / Zustimmen / Accept all"), LLM fallback if heuristic fails.
- **Flaky error pages:** detect 5xx / "Fehler / nicht verfügbar / Service Unavailable" → exponential backoff retry (max 4) → tell the model if still failing.
- **Session expiry:**
  1. Detect a login form or redirect to login.
  2. Auto re-login with this run's credentials.
  3. Navigate back to the last good URL.
  4. Inject "session was renewed" into the next observation.

  The ERP has a short timeout; keep idle gaps small.
- **Dialogs:** capture `page.on("dialog")` and surface the text to the model. **Auto-accept only inside SubmitterAgent's final-confirm step.**
- **Popups:** capture new pages / self-closing windows and keep their content in the observation.
- **Timezones:** parse every date with its tz; store raw + UTC. Scenario date comes from the brief.

---

## 6. Evidence models (conceptual; field names must not assume public-site labels)

Every record has:

```
Provenance(site_id, url, title, frame_path, snippet<=300 chars, observed_at, agent)
```

| Record | Fields |
|---|---|
| `TenderInfo` | reference, title, buyer, deadline_raw, deadline_utc |
| `TenderPosition` | position_id, description, quantity, unit (`piece` / `set` / other raw), requirement_ref |
| `QAEntry` | number, date, kind, position_id?, payload, withdraws_number?, revision_ref?, requirement_ref? |
| `SpecRevision` | revision_id, status (current / superseded / unknown), date, requirements[] |
| `Requirement` | requirement_id, katalog_group, bore, stroke, stroke_kind (nominal / usable = "nutzbarer Weg"), pressure, mounting, certification, pieces_per_set? |
| `Material` | number, status (active / blocked / other raw), katalog_group, bore, stroke, pressure, mounting, certification, stock_qty, lead_weeks_stock, lead_weeks_production, carton_size?, surcharge_mass?, allocation_mass?, setup_fee? |
| `PriceCondition` | material, valid_from, valid_to (inclusive), currency, fx_key?, price_unit_qty (e.g. per 1/100), scales[(min_qty, price)] |
| `MailRecord` | message_id, folder (inbox / archive), sender, date, subject, reference, kind, payload |
| `TAN` | value, requested_at, expires_at |

**QAEntry kinds:**
- `quantity_change`
- `withdrawal`
- `technical_amendment` (named attributes only)
- `condition_group` (Konditionsgruppe key)
- `packaging_tariff_key`
- `delivery_commitment_key`
- `setup_pool_membership`
- `other`

**MailRecord kinds:**
- `discount_approval` — percent, min_order_value?
- `konditionsregeln` — group → applies_to ∈ {base_only, base_plus_surcharge, none}
- `technikfreigabe` — per-end reserve by revision/requirement
- `verpackungstarife` — tariff key → EUR per carton
- `terminfreigabe` — commitment key → material, lead weeks, ceiling qty
- `devisenfixing` — fx key → EUR per currency unit
- `ruestpools` — pool fee + cent-allocation rule text + parsed strategy
- `projektbonus` — marginal bands
- `preisindizes` — metal index code → rates with optional validity windows
- `tan`
- `other`

**Prose clauses** (Konditionsregeln, Rüstpools allocation rule, Projektbonus bands, Preisindizes formula) are parsed by the LLM into structured enums/params, with the raw text kept. If the parse is low-confidence, escalate to the frontier model, within budget.

---

## 7. Resolver + Rules Engine (implement exactly; this is ~90% of the points)

All money math uses `Decimal`. Round with `ROUND_HALF_UP` to cents only where the rule says. Never feed rounded breakdown fields back into the final formula. Every function returns a value plus trace entries.

### Resolver (selection)

- **R-auth:** a record "for key K" applies only if all of these hold:
  - exact tender reference match (normalise whitespace/case only)
  - record date ≤ scenario date
  - sender = authorized local part + bidder mailbox domain

  Then the latest applicable record wins. Authorized pairs, read from the brief at runtime with these as expected values:

  | Subject | Sender local part |
  |---|---|
  | Discount approval | `leitung` |
  | Konditionsregeln | `kalkulation` |
  | Preisindizes | `kalkulation` |
  | Rüstpools | `kalkulation` |
  | Technikfreigabe | `technik` |
  | Verpackungstarife | `logistik` |
  | Terminfreigabe | `logistik` |
  | Devisenfixing | `finanzen` |
  | Projektbonus | `finanzen` |

  A newer record replaces the older one even if its condition later fails. Never revive an older one.
- **Bidder mailbox domain:** derive from the logged-in mailbox / brief. Do not hardcode.
- **Spec:** use the revision marked current. Superseded revisions never apply, even if they are opened first.
- **Technical amendments:** only Q&A amendments naming the governing revision and requirement. Apply per attribute; the latest dated amendment wins per attribute; other attributes are inherited.
- **Quantity:** start from the tender quantity. Drop any `quantity_change` withdrawn by a later `withdrawal`. Use the latest remaining change for that position, else the tender quantity.

### Engine pipeline per scenario

1. **Units.** If the unit is a set ("Satz"), multiply by `pieces_per_set` from the governing spec. All downstream math uses final pieces.
2. **Requirement.** Take the requirement from the current revision + scoped amendments.
   - If `stroke_kind == usable`: nominal stroke = amended usable stroke + 2 × per-end reserve from the revision-specific Technikfreigabe. Other attributes unchanged.
3. **Material.** Find an active material in the same katalog_group where bore, stroke, pressure, mounting and certification all match exactly (normalise units and formatting only).
   - If **any** position has no match: `decision = no_bid`, `positions = []`, `total_net = 0`, `discount_percent = 0` (a conditional approval yields 0 for no_bid).
4. **Price condition.** Pick the one valid on the scenario date (inclusive endpoints).
   1. Normalise the price unit (e.g. per 100 → per piece).
   2. Choose the scale with the highest `min_qty ≤ final quantity`. **Per position, not per material sum.**
   3. Convert currency: price × EUR-per-unit from Devisenfixing (fx key from the condition).
   4. Round HALF_UP to cents → `base_unit_price` (EUR). Use this converted value everywhere downstream, including thresholds.
5. **Discount (order level).** Take the latest R-auth director approval for this exact reference.
   - If it has a minimum order value: undiscounted net = Σ qty × base_unit_price (excluding surcharges, setup, packaging), rounded to cents. Apply the discount only if net ≥ minimum (inclusive); otherwise 0%. No fallback to older approvals.
   - No approval → 0%.
6. **Discount clause per position.** If Q&A assigns a Konditionsgruppe, resolve it in the latest R-auth Konditionsregeln mail to one of {base_only, base_plus_surcharge, none}. Otherwise base_only.
   - Setup fees and packaging are never discounted.
   - The group overrides the default even when two positions share a material.
7. **Mass surcharge per piece.**
   - Use the metal index from the latest R-auth Preisindizes mail. If rates have validity windows, pick the window containing the scenario date (inclusive), not the last rate listed.
   - Combine with the material's surcharge mass. **The exact formula comes from the mail/ERP text.** Parse it, keep it parameterised, trace it.
   - The reported surcharge is always **before** discount.
8. **Setup.** Ordinary setup fee per position (ERP).
   - Shared pool: Q&A names pool members; the Rüstpools mail gives one pool fee + a cent-allocation rule.
   - Allocate by `quantity × allocation_mass` (allocation mass ≠ surcharge mass) using the mail's rule (implement largest-remainder with configurable tie-break; map the mail prose to the strategy).
   - Add each allocation to that position's ordinary setup fee.
9. **Packaging.**
   - Q&A assigns the tariff key; ERP gives pieces per carton.
   - cartons = ceil(final pieces / carton size), per position. The bid quantity is never changed.
   - carton cost = cartons × tariff. `packaging_per_unit = carton_cost / qty`, HALF_UP. Not discounted.
10. **Project bonus.**
    - Eligible members are those the finance mail/Q&A defines. Eligible goods total = Σ undiscounted EUR goods value (qty × base_unit_price).
    - Apply marginal bands, each band only to its slice. Round the aggregate credit HALF_UP to cents.
    - Allocate to eligible lines proportionally to line goods value via largest remainder in cents; ties go to ascending position id.
    - `rebate_total` = the position's whole allocated credit (not per piece). 0.00 for non-members. Independent of discount clauses.
11. **Delivery / lead time.**
    - Pool final quantities of all positions using the same material.
    - If a Q&A commitment key applies to the position, its material matches, and pooled demand ≤ ceiling (inclusive): use the commitment lead time.
    - Otherwise use shared stock: if stock ≥ pooled demand, every such position uses stock lead time; else all use production lead time. No partial allocation.
12. **Final unit price.**
    ```
    unit_price = disc(base) + disc(surcharge) + (setup_ordinary + setup_shared_alloc + carton_cost − bonus_alloc) / qty
    ```
    `disc()` applies the order discount only where the position's clause says. Use unrounded components; round HALF_UP **once** at the end.
13. **Total.** `total_net = Σ qty × rounded unit_price`, rounded HALF_UP. This also guarantees the "arithmetic consistency" points even if inputs are wrong.
14. **Report breakdown fields** (each earns independent credit):
    - `base_unit_price`
    - `surcharge_per_unit` (pre-discount)
    - `packaging_per_unit`
    - `rebate_total`

    Enter `0.00` for known zeros. Leave a field **absent** if unknown; never guess.

**Known ambiguity — make it a config flag and A/B on test runs:** does `surcharge_per_unit` = mass surcharge only, or mass surcharge + (ordinary + shared setup)/qty? Rule text: "Add each allocation to that position's ordinary setup fee. Report surcharge_per_unit BEFORE discount, excluding packaging and bonus." Default to the second interpretation. Settle it with the test-run score breakdown.

**Unit tests:** synthetic fixtures for every step and every rounding/threshold edge, including:
- inclusive date/threshold boundaries
- withdrawal chains
- set conversion
- shared stock across positions
- tie-breaks
- FX
- validity windows
- no_bid path

---

## 8. Agents, orchestration, speed and cost

### Agent loop (`agents/base.py`)
- **System prompt** = stable role + generic site-type knowledge + German business glossary + tool docs. Put it first for prompt caching. Then the task, then the latest observation.
- **Generic glossary** (domain vocabulary is fine; scenario values are not): Bohrung/Kolben-Ø = bore, Hub = stroke, nutzbarer Weg = usable stroke, Betriebsdruck = pressure, Befestigung = mounting, Zertifizierung = certification, Staffel = quantity scale, gültig ab/bis = valid from/to, gesperrt = blocked, Lagerbestand = stock, Lieferzeit = lead time, Satz = set, Revision/Stand = revision, aktuell/gültig vs ersetzt/veraltet = current vs superseded, Archiv = archive, Rabattfreigabe = discount approval.
- **Context hygiene:**
  - Keep the last 2–3 observations verbatim; summarise older ones into a running "what I know / where I've been" note.
  - Facts live in the ledger, not the chat.
  - Hard step cap per task.
  - **Stuck detection:** same page hash 3× → force a different strategy or escalate.
- **Prompts describe site *roles and patterns*, never public-site layouts.** The private run uses different software, navigation and site names, and splits information differently. An agent must be able to find e.g. approval mails even if they live in the "tender portal" of the private dataset. The orchestrator can re-route a missing fact to any site agent.

### Orchestration
- Start all four site agents in parallel at t=0. Each logs in and orients itself.
- **Dependencies via a blackboard:**
  - The ERP agent needs katalog groups + requirement attributes (from SpecAgent) and the Technikfreigabe reserve (from MailAgent). It orients and learns search mechanics while waiting.
  - The MailAgent searches by tender reference AND topic (search is AND-combined). It checks inbox + archive and pages through results.
- After the first pass: Resolver + Engine run, list **missing inputs**, and dispatch targeted follow-up tasks ("find valid price condition for material M", "find pieces per carton for M"). The model still chooses how to find them.
- **SubmitterAgent** can start registration / "express interest" / configurator navigation early on its own tab. It enters values only when the answer is frozen. It requests the TAN **last** (5-minute validity; a new request invalidates the old). It verifies the summary page against the answer before final confirm.
  - Prices go in **German number format** (`1.234,56`).
  - Do not enter the gross/VAT amount.
  - Use explicit "Position auslassen" for positions not computed.
  - It must save often, since sessions drop unsaved entries and going back clears later steps.

### Deadline watchdog
- At `expires_at − SUBMIT_MARGIN_S` (default 300s; tune after measuring submitter duration), freeze the best current answer and submit.
- A run without an answer scores 0; partial answers score per field.

### Model routing (`llm/router.py`)
- **Qwen:** all browsing and extraction.
- **Clef:** optional for binary/choice checks ("is this the current revision?", "which of these results matches the requirement?"). Keep only if it measurably cuts time or tokens.
- **Frontier (OpenAI):** escalate only when
  - a record fails validation twice
  - prose clause parsing is low-confidence
  - the resolver finds irreconcilable conflicts

  Enforce the per-run USD cap. Log every escalation with reason (dashboard shows it).
- **Long timeouts** on the first hosted-model call (cold start can take minutes; warm-up is not billed to time).
- Respect concurrency: Qwen max 32 calls/team, Clef 16.

### Speed and cost levers
- 4 browsers in parallel.
- Small snapshots.
- Cached system prompts.
- No screenshots unless needed.
- Avoid re-reading unchanged pages.
- Parallelise positions inside the ERP agent with extra tabs when there are many positions.

---

## 9. Dashboard (local dev tool, not shipped)

**Goal:** see exactly what the agents are doing and why the answer came out the way it did. It must look like a real product, not an AI template.

### Design source
1. From the repo root (or `dashboard/`) run `npx getdesign@latest add linear.app`.
2. If that fails, download Linear's DESIGN.md from `https://getdesign.md/linear.app/design-md`, or `design-md/linear.app/DESIGN.md` in `github.com/VoltAgent/awesome-design-md`.
3. Place it at `dashboard/DESIGN.md`.
4. **Read it fully before writing any UI.** Translate its tokens into `dashboard/web/src/styles/tokens.css` (CSS variables: colours, type scale, spacing, radii, borders, elevation).
5. Follow its Do's and Don'ts strictly.

### Stack
- FastAPI + SSE (`dashboard/server.py`)
- Vite + React + TypeScript (`dashboard/web`)
- Plain CSS modules or vanilla-extract on top of the tokens
- No shadcn/Material defaults
- Lucide icons at 14–16px only

### Anti-AI-look rules
- No gradient heroes, glow blobs, glassmorphism or emoji.
- No giant rounded stat-card grids, no "AI sparkle" iconography, no purple-gradient buttons.
- Use hairline 1px borders, dense 32px rows and `font-variant-numeric: tabular-nums` for all numbers.
- Use monospace only for IDs/JSON.
- Keyboard navigation: `j/k` to move, `enter` to open, `/` to filter.
- Default dark theme, as Linear's DESIGN.md specifies.

### Data
- The agent emits events via `telemetry.emit()`:
  - `run_started`, `phase`, `agent_step` (goal, thought summary ≤ 200 chars, action, observation size, page URL, latency, tokens, cost, model), `relogin`, `retry`, `escalation`, `record_added`, `record_rejected` (with resolver reason), `trace_step`, `answer_frozen`, `submit_step`, `run_finished`
- Events go to `runs/<run_id>/events.jsonl` and as `@@EVT {...}` stdout lines, so sandbox test-run logs can be imported and replayed.
- Screenshots: JPEG thumbnails saved locally, referenced by path. Never base64 to stdout.

### Views (Linear-style left sidebar + main pane)
1. **Runs list:** local runs + imported test runs. Shows status, quality (if known), cost, duration, config label (e.g. `frontier=off, surcharge=v2`).
2. **Run header:** elapsed vs 20:00 budget bar, phase, cost split by model, tokens in/cached/out, escalations count.
3. **Agent lanes:** one column per agent (Tender / Spec / ERP / Mail / Submitter). Each shows current goal, last action, latest screenshot thumbnail, step count, retries/relogins, status pill.
4. **Timeline:** unified event stream, filter by agent/type, expandable rows (thought summary, action, observation excerpt, latency, cost).
5. **Evidence ledger:** table of all records with provenance link, accepted / rejected + reason (superseded, other reference, future-dated, unauthorized sender, withdrawn).
6. **Calculation trace:** per position, each rule step with inputs → output. Final answer JSON with schema validation status. Diff vs another run.
7. **Score breakdown:** for imported test runs, per-field points from the platform. Side-by-side comparison of two runs, to drive A/B decisions.

Find the platform endpoints for listing runs, logs, screenshots and score breakdowns in `openapi.json` / `docs/api.md`.

---

## 10. Milestones (do in order; each ends with a gate)

**M0 — Scaffold (gate: `uv run python -m rfq --dry` inside `my-agent/` prints parsed run + brief locally)**
- `git pull` first (the checkout is one commit behind `origin/main`).
- Inspect what's already in `my-agent/` (likely the starter kit). Keep and extend it rather than replacing it blindly.
- Verify tools: `atira --help`, the atira MCP tools are visible, `uv --version`, Playwright Chromium installed locally.
- Set up the repo layout, `AGENTS.md`, `.gitattributes`, `.gitignore`, config, platform client.
- Build `telemetry.events` and `scripts/run_local.py` (loads scouting env from `atira scout --challenge rfq --env`).
- Build `fairplay_check.py` and `package.py`.

**M1 — Browser harness (gate: each of the 4 site agents logs in and summarises its site's sections on its own, locally)**
- `session`, `snapshot`, `actions`, `guards`, `documents`, `login`, `agents/base.py`.
- `tests/snapshot` fixtures (self-made HTML).

**M2 — Thin end-to-end (gate: first sandbox test run is eligible: right reference, bid/no_bid, all positions, quantities, consistent total)**
- TenderAgent (reference, positions, quantities incl. Q&A + withdrawals + units), SubmitterAgent full flow incl. TAN and confirm, watchdog.
- Run test runs on `.\my-agent` via the already-configured atira MCP (tools: `test_run`, `wait_for_run`, `diagnose_run`, `run_logs`, `run_screenshot`) or `atira test-run .\my-agent --challenge rfq`.

**M3 — Core rules (gate: test run shows material, quantity, base price, delivery, discount points)**
- SpecAgent, ErpAgent, MailAgent, evidence models, resolver, engine steps 1–5, 11–13, unit tests.

**M4 — Full rule coverage (gate: breakdown fields scoring; record which interpretation wins each ambiguity)**
- Engine steps 6–10 and 14: clauses, amendments, usable stroke, FX, metal index windows, packaging, delivery commitments, setup pools, project bonus.
- Follow-up task loop.

**M5 — Robustness, generality, speed**
- `tests/mock_sites/`: self-made mock versions of the four site roles with **different layouts, navigation and labels**, and information moved between sites. Include cookie wall, timeouts, iframes, ZIPs, paging, look-alikes. The agent must solve them without code changes. This is our proxy for the private dataset.
- Profile time/cost per agent.
- Tune snapshot size, caching, parallelism and margin.
- A/B frontier on/off and Clef on/off with `scripts/ab_test.py` (≥ 2 test runs per config).

**M6 — Dashboard**
- Build per §9. Polish last, but wire telemetry from M0 so earlier milestones are debuggable.

After each milestone:
- Commit.
- Write a 5-line status to `PROGRESS.md`: what works, test-run quality, cost, time, next.
- **Stop for human review at the gates marked ⚑ below.**

---

## 11. ⚑ Human checkpoints — stop and ask before proceeding

1. **Before the first scored submission, and before every later one.** Show:
   - last test-run breakdown
   - cost and time
   - fairplay_check output
   - diff since the last submission

   Remember: the latest passing submission enters the final run.
2. **Choosing `FRONTIER_MODEL`.** Propose the cheapest OpenAI model with tool calls + vision that the proxy accepts (check its published price via the run's proxy info), with the expected per-run cost. Wait for approval.
3. **Enabling frontier in the shipped config.** Show the A/B evidence (quality delta vs cost × time delta).
4. **Ambiguous rule interpretations** where test runs can't disambiguate after 2 A/B runs. Present the options with evidence.
5. **Any fair-play doubt** (e.g. a helper that might look like a crawler). Describe it and ask.
6. **Dev frontier spend reaching 50% of `DEV_FRONTIER_BUDGET_USD`.**

---

## 12. Definition of done

- [ ] Sandbox test runs consistently eligible, quality ≥ 70, < 10 min, < $0.10.
- [ ] Mock-site suite (different layouts) solved without code changes.
- [ ] `fairplay_check.py` clean; no scenario values anywhere; credentials only from env.
- [ ] Unit tests green for every rule and edge case.
- [ ] Dashboard shows a live local run and a replayed sandbox test run, Linear design applied faithfully.
- [ ] `README.md` explains the architecture in one diagram and one page. Elegance is judged.
