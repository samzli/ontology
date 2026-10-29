# Onti — Technical Documentation

> Audience: developers and integrators. For usage see
> `docs/USER_GUIDE.md` and `docs/ADMIN_GUIDE.md`; for setup see the
> root `README.md`.

---

## 1. Architecture

```text
Browser (vanilla JS)
  │  fetch / SSE
  ▼
FastAPI (main.py)            ← thin HTTP layer only
  │
  ├── modules/auth.py        ← PBKDF2 accounts, signed session cookies
  ├── modules/ontology_service.py  ← rules engine, formulas, graphs,
  │                                    search, health, graph studio
  ├── modules/qa_service.py  ← rules-first / LLM-fallback pipeline
  ├── modules/llm_client.py  ← OpenAI-compatible client, model CRUD
  │                            (system + per-user), token metering
  ├── modules/admin_service.py ← packs, ontology CRUD, datasets,
  │                               import (3 JSON shapes) / export,
  │                               versions + optimistic locking, audit
  ├── modules/analytics.py   ← time series, MoM attribution, scenarios
  ├── modules/report_builder.py ← MD/HTML/PDF assembly
  ├── modules/report_charts.py  ← matplotlib charts + chain graph
  ├── modules/importer.py    ← CSV/XLSX/JSON parse (sheet pick) + column
  │                            mapping + validation; single-file and
  │                            multi-file / folder batch import
  ├── modules/induction.py   ← AI ontology proposals (+ evidence)
  └── modules/db.py          ← SQLite schema, settings, audit, crypto
        ▼
      data/onti.db (SQLite, WAL; /tmp/onti-data on read-only hosts)
```

No external services: one Python process, one SQLite file. The
frontend is server-rendered Jinja2 plus four static JS files
(`common.js`, `graph.js`, `app.js`, `admin.js`) — no build step.

`modules/db.py:_resolve_data_dir` picks the writable data directory:
`ONTI_DATA_DIR` when set, `/tmp/onti-data` when `VERCEL` is present
(the deployment itself is read-only), otherwise the bundled `data/`
folder. On first connection the bundled `data/onti.db` is copied into
that writable directory, so a fresh serverless instance starts with the
same seeded packs, accounts and report metadata. Files under
`data/reports/` that are not present in the writable directory are read
from the bundled copy (`report_builder._abs`). The machine key
(`data/.onti.key`) always stays next to the bundled data, so encrypted
model API keys and signed session cookies are identical across
instances.

## 2. Data model (SQLite)

Core tables (selection):

| Table                                   | Purpose                                         |
| --------------------------------------- | ----------------------------------------------- |
| `users`                                 | accounts, PBKDF2 hashes, roles                  |
| `sessions`                              | session tokens (signed + stored)                |
| `ontology_packs`                        | one row per ontology (see origin note below)    |
| `entities` / `attributes` / `relations` | the ontology schema                             |
| `graph_nodes` / `graph_edges`           | the impact graph (auto-generated if empty)      |
| `metric_formulas` / `question_rules`    | computation + rules engine                      |
| `tables` / `datasets`                   | logical data tables and their versioned sources |
| `question_bank`                         | canonical questions                             |
| `user_questions`                        | per-user private question lists                 |
| `ask_history`                           | one row per answered question (see memory note) |
| `studio_history`                        | Graph studio generation history log             |
| `model_configs`                         | admin-registered models (keys encrypted)        |
| `user_model_configs`                    | per-user Model centre entries (keys encrypted)  |
| `llm_calls`                             | token metering: caller, model, success, latency |
| `audit`                                 | append-only write log                           |
| `app_settings`                          | key/value store: user model + pack selections   |

Four columns deserve detail:

- `ontology_packs.origin` — provenance string (Graph studio
  generation with table list and username, JSON import, or seed) shown
  as the 🧾 badge in the admin pack bar.
- `ontology_packs.version` — optimistic-lock counter bumped by every
  pack write (element upsert/delete, graph sync, import, restore).
  Write endpoints accept an `expected_version` parameter and answer
  HTTP 409 when another session already moved the pack forward, so
  concurrent admin edits never overwrite each other.
- `ontology_proposals.evidence` — the JSON snapshot of *what the model
  was shown* when it drafted the proposal: target table key/label,
  dataset id, the table's column specs and the sample rows
  (`modules/induction.py:_build_evidence`). Returned by
  `list_proposals` alongside `rationale` so the admin review queue can
  render the "Proposing against" panel.
- `ask_history` — user, pack, question, answer excerpt, source,
  matched flag and token count. Powers the Recent asks chips, the
  History view and the AI's cross-session memory.

`studio_history` records every Graph studio action per user: pack,
action (`ontology` / `preview` / `data-upload` / `ontology-upload` /
`report`), title and detail (for reports, the report uid). It powers
the user-facing Generation history card.

Migrations run in `modules/db.py:init_db` — e.g. `ontology_packs`
gains `origin` and `version`, `ask_history` gains `answer`, and
`ontology_proposals` gains `evidence`, all via idempotent `ALTER TABLE`
guards, so older databases upgrade in place.

## 3. Identity, provenance and memory

### 3.1 Pack origin

Every write path that creates a pack records `origin`:

- Graph studio: `Generated from data in Graph studio (tables: t1,
  t2) by <username>` (plus `+ AI enrichment` when requested);
- Admin import: `Imported from JSON file by <admin>`;
- seed packs carry a descriptive origin.

The value is surfaced as the 🧾 badge in the admin pack bar.

The built-in sample pack (`vnb-ape-sample`) is auto-upgraded by
`seed.upgrade_sample_pack` on every startup (idempotent, flagged
`sample_pack_upgraded_v2:{pack_id}`): it adds the five monthly
data-table entities (VNB Monthly, Channel Monthly, Agent Monthly,
Product Mix Monthly, Scenario Analysis) with attributes and relations
(10 entities / 13 relations in total, one relation per table-to-table
link such as channel aggregates VNB, agent rolls up to channel), and
repairs a degraded impact graph (only auto-generated entity nodes with
wiped values) by rebuilding the rich KPI chain — 26 nodes / 30 edges
including the table-level nodes X1–X8.

### 3.2 Graph studio provenance

- `GET /api/graph-studio/sources` annotates every ontology table with
  its source dataset (`dataset_name`, `dataset_file`, `dataset_kind`,
  `dataset_updated` from the newest `datasets` row).
- Every graph node carries `source {kind, detail}`; the UI provenance
  panel and the per-view "Sources used" list render it.
- Views: `lineage` (default), `schema`, `columns` — built by
  `ontology_service.build_studio_graph`.

### 3.3 Per-user scenario packs

`app_settings["user_pack:{uid}"]` stores the pack a user picked in
the header dropdown. `main.py:user_pack_id` resolves it with fallback
to the admin-activated pack, and every user-facing read (graph,
dashboard, analytics, Ask, Graph studio, reports, memory) goes
through `resolve_pack`. `GET /api/packs` lists the visible packs with
`is_active` / `selected` flags, origin and data-source summaries;
`POST /api/my/pack` sets the selection, `{"pack_id": null}` resets it
to the system default.

### 3.4 Ask memory (short + long term)

`main.py:ask_stream` assembles the LLM `history` from:

1. the current session's turns (client-supplied, short-term);
2. the four most recent `ask_history` answers for this user + pack
   (long-term, cross-session), truncated to 600 chars each.

After the stream finishes, the question, answer excerpt, source,
matched flag and token count are inserted into `ask_history`.
`GET /api/ask/history` returns the last 30 asks per active pack.

**Rules-engine month inheritance.** `ontology_service.answer_question`
also accepts the same `history`; when the current question names no
month (`_extract_month_prefixes` finds none), the engine walks the
history from newest to oldest and borrows the month from the most
recent user turn that names one, so follow-ups like *"How does it
compare to the previous month?"* stay on the month asked about before.
Inside rule templates, `month_context(question, rows)` resolves that
month against the data (a year-less name such as "Sep" picks the most
recent matching month), `filter_by_month(rows, month)` selects its
rows, `month_movement(rows, tgt, prev)` aggregates both months and
ranks the biggest cohort movers, and `attr_for(question)` returns the
driver attribution for that month. `/api/ask/compare` builds the same
memory before calling `compare_answers`, so the comparison's
with-ontology side inherits the month identically.

### 3.5 With / without ontology comparison

`POST /api/ask/compare` (body `{question, mode?, history?}`) answers
one question twice and returns both sides plus a value summary. The
same model config answers both; the difference is what context each
side receives:

```json
{
  "question": "...",
  "with_ontology": {
    "label": "With ontology",
    "source": "rules+analysis|rules|analysis|ai|none|error",
    "answer": "...",
    "graph": {"matched": [...], "nodes": [...], "edge_ids": [...]},
    "model": "ontology-engine",
    "grounding": {
      "tables": [...], "metrics": [...],
      "table_count": 6, "metric_count": 6,
      "graph_nodes": 8, "graph_edges": 7
    },
    "analysis": {
      "available": true,
      "headline": "...",
      "month": "2025-06",
      "focus": "movement|mix|cohort|trend|level",
      "steps": [{"n": 1, "method": "...", "detail": "...", "result": "..."}],
      "findings": ["..."],
      "stats": [{"test": "...", "value": "...", "reading": "..."}],
      "depth": {
        "facts": 13, "reasoning_steps": 6, "stat_tests": 7,
        "tables_joined": 4, "dimensions": 6,
        "reconciled": true, "chars": 5381
      }
    },
    "narrative": "..."
  },
  "without_ontology": {
    "label": "Without ontology",
    "source": "ai|unavailable|error",
    "answer": "...",
    "graph": null,
    "model": "...",
    "grounding": {
      "tables": [...],   // data tables actually passed to the baseline prompt
      "metrics": [],     // always empty — no metric definitions
      "table_count": 6,
      "metric_count": 0,
      "graph_nodes": 0,
      "graph_edges": 0
    }
  },
  "value": {
    "tables_used": 6, "metrics_used": 6,
    "chain_nodes": 8,  "chain_edges": 7,
    "baseline_tables_used": 6,   // tables the baseline side also saw
    "baseline_chain_nodes": 0,
    "reasoning_steps": 6, "stat_tests": 7, "findings": 13,
    "tables_joined": 4, "dimensions": 6, "reconciled": true,
    "ontology_chars": 5381, "baseline_chars": 1486
  }
}
```

- **With ontology** runs the normal pipeline synchronously
  (`qa_service.compare_answers`) in three stages:
  1. `deep_analysis.analyze()` — the deterministic reasoning layer (see
     §3.5.1) computes every figure from the pack's own tables;
  2. `rule_answer` — the rules engine renders its canonical answer when
     a rule matches;
  3. the two are concatenated (rule answer first, then the analysis
     body) and `retrieve_subgraph` builds the impact chain.
  `source` is `rules+analysis`, `rules`, or `analysis` accordingly. The
  analysis alone is enough to answer, so the grounded side still works
  when no model is configured. `analysis.depth` carries the counters
  quoted in `value` (steps, statistical tests, findings, tables joined,
  dimensions, reconciliation).
- **Second wheel (`narrative`).** When a model is configured, the same
  model is called a second time with
  `llm.build_synthesis_prompt(analysis, ontology_context, rules_answer)`.
  In that prompt the model is a **narrator only**: it must reuse the
  pre-computed numbers verbatim and explain the reasoning chain. This is
  the "rule engine + LLM" joint mode; it never recomputes, so the
  deterministic evidence stays authoritative.
- **Without ontology** calls the same model with
  `llm.build_baseline_prompt(ontology_service.build_table_profiles(pack))`
  — the **real uploaded data tables are included** so the model can
  reason over company figures, but the ontology layer (entity
  definitions, metric formulas, graph edges, impact chain) is
  deliberately stripped. `graph` is always `null` because there is
  nothing to trace. Grounding reports the same data tables the
  ontology side saw (`tables`, `table_count`) with `metric_count: 0`.
  No conversation history is forwarded, so ontology-grounded context
  cannot leak across.
- Only the grounded answer is written to `ask_history`; the baseline is
  not stored. The endpoint runs the two blocking model calls via
  `asyncio.to_thread`, so the event loop stays free.
- When no model is configured the grounded side still answers through
  the rules engine and the baseline side explains that it needs a model
  (`source: "unavailable"`).
- **Portal rendering.** `static/js/app.js:renderCompareCard` renders
  exactly two columns — the grounded answer (with its impact chain) and
  the baseline answer. No explanatory banner or counter strip is drawn:
  the `value` object stays in the response for API clients and for
  `tests/verify_ontology_value.py`, which scores the gap from it.

#### 3.5.1 Deterministic reasoning layer (`modules/deep_analysis.py`)

`analyze(pack, computed, question, history) -> dict` is model-free: every
number comes from the pack's own rows, so the result is reproducible and
auditable. It runs in a fixed order:

1. **Resolve roles** — read the pack's declarative `analysis` spec (or
   infer it from the ontology) to learn which table/column carries the
   metric, the month, the segments and the driver identity.
2. **Reconcile** — check that segment rows sum to the company total for
   every period and report the max |residual|.
3. **Decompose** — apply the declared identity per segment with
   sequential factor substitution, so the factor effects add exactly to
   the reported move.
4. **Cross-check dimensions** — join `mix` → `product` for the weighted
   margin and split the change into mix vs product-margin effects; join
   `agent_monthly` → `agent_master` for churn-band, certification and
   tenure cohorts.
5. **Statistics** — OLS slope + R², coefficient of variation, z-score of
   the move into the target month, 3-vs-3 momentum, Herfindahl
   concentration, driver correlation and the reconciliation residual.
6. **Explain** — render a focus-aware report (the section matching the
   question type leads), a ranked finding list, a numbered reasoning
   trace and an annotated impact chain walked over the pack's graph.

`detect_focus(question)` selects one of `level`, `movement`, `mix`,
`cohort`, `trend`; follow-up questions inherit the month named earlier
in the conversation, mirroring the rules engine's own memory.

`POST /api/ask-stream` uses the same layer: a rule hit is followed by the
analysis body, and AI mode swaps the plain system prompt for
`llm.build_synthesis_prompt(...)`, which forbids the model from
recomputing.

### 3.6 Global ontology mode

A second, system-wide control decides whether the ontology is used at
all. It is a **client-side preference**, not a server setting: both
portals render the same **Use ontology ON / OFF** control from
`Onti.ontology` in `static/js/common.js`, which persists the choice in
`localStorage` under `onti.useOntology` (default `"on"`) and broadcasts
an `onti:ontology` DOM event on every change.

| Key     | `onti.useOntology`                                               |
| ------- | ---------------------------------------------------------------- |
| Default | ON                                                               |
| Scope   | Per browser; both portals on the same origin share it            |
| Toggle  | `Onti.ontology.mount()` in the header of `app.js` and `admin.js` |
| Read    | `Onti.ontology.enabled()`                                        |

With the switch OFF, no data is sent anywhere different — the front end
simply refuses to render ontology-derived output and says so:

- `loadDashboard` replaces KPI cards with an off panel and hides
  `#dashGraphCard`; `loadAnalytics` and `loadGraphStudio` render the same
  panel instead of charts; the admin `loaders.graph` does likewise.
- `askCore` / `compareCore` delegate to `askWithoutOntology`, which calls
  `POST /api/ask/compare` and renders only its `without_ontology` side
  (badge “Without ontology”, no impact chain).
- `POST /api/reports` accepts `use_ontology: false`; the server removes
  the `evidence`, `charts`, `analytics` and `ontology` sections (and
  falls back to `["summary"]` if that empties the list, so
  `report_builder.build_report` never re-applies its default section
  set), and suffixes the title with “— No ontology (text-only)”.
  Reports therefore still generate and download.
- A dismissible orange `.onto-off-banner` is inserted under the topbar
  by `Onti.ontology.renderBanner()`, and every off panel carries a
  **Turn ontology back on** action. Both listeners re-run the current
  view so the page updates without a reload.

Because the flag is client-side, API clients are unaffected and the
values returned by `/api/dashboard` etc. are unchanged — the mode is a
presentation decision, not a data-permission boundary.

### 3.7 Knowledge-base context builder (no row truncation)

`ontology_service.build_llm_context(pack)` renders the grounding block the
AI mode reads. It deliberately **does not dump raw rows** any more: an
earlier version pasted the first 100 rows of every table into the prompt and
appended "*(remaining N rows omitted)*", which both misled the model — it
answered from a January-only slice of a 24-month table — and inflated the
prompt (slow answers). The profile-first builder summarises each table over
its **full active dataset**:

| Block                | Content                                                                                                                                                   | Cap                       |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------- |
| Column profile       | per column: type, `sum`/`avg`/`min`/`max` for numerics (percent-formatted columns render as %), full sorted distinct values otherwise                     | distinct list ≤ 48 values |
| Month coverage       | month-like columns (key/label contains `month`, `period` or `date`) always list their values, normalised to `YYYY-MM` (`2023-01-01 00:00:00` → `2023-01`) | ≤ 36 distinct             |
| Per-month aggregates | one markdown table of month vs `sum`/`avg` per numeric column, computed in one pass (`_month_buckets`)                                                    | ≤ 36 groups × 8 measures  |
| Sample rows          | 3 verbatim rows for format illustration only                                                                                                              | 3                         |

Everything else (entities, relations, formulas, graph edges) is unchanged.
Callers: `qa_service.stream_answer` / `compare_answers` and
`report_builder.build_report` — all pass no arguments, so all three share
the same complete grounding. `compute_pack` itself always had every row; the
truncation existed only in the prompt layer.

Monthly experience tables are therefore fully covered: the model can list
every month and quote per-month totals (e.g. Aug 55 / Sep 63 lapses) without
ever seeing a "rows omitted" note.

## 4. Models — system, user, resolution

- System models: `model_configs` (admin CRUD, encrypted keys).
- User models: `user_model_configs` (owner-scoped CRUD via
  `/api/my/models*`, encrypted keys, duplicate names rejected 409).
- Selection: `app_settings["user_model:{uid}"]` stores `sys:N`,
  `my:N` or empty. `main.py:_effective_model_cfg` resolves it at ask
  time with fallback to the system default.
- `.env` registration (`llm_client.append_env_model`): writes a
  self-contained block

  ```text
  # >>> onti model: my_azure_gpt >>>
  ONTI_MODEL_MY_AZURE_GPT_NAME="My Azure GPT"
  ONTI_MODEL_MY_AZURE_GPT_BASE_URL="https://..."
  ONTI_MODEL_MY_AZURE_GPT_API_KEY="..."
  ONTI_MODEL_MY_AZURE_GPT_MODEL="gpt-4o"
  # <<< onti model: my_azure_gpt <<<
  ```

  Existing blocks with the same slug are replaced; the DB stays the
  source of truth.
- Testing: `POST /api/my/models/test` accepts `config_id: "my:N"`
  (resolves the user's stored config server-side) or ad-hoc fields.
- Oversight: `GET /api/admin/user-models` returns every user-created
  model with masked keys and owner username.

### 4.1 Connection-test contract

`POST /api/admin/models/test` and `POST /api/my/models/test` share one
response shape. A test that *ran* always answers **HTTP 200**, whatever
the verdict — a negative result is a successful request, not a client
error (encoding it as 400 used to make every failed test look like a
malformed request in the server log and pushed the UI through its
exception path, where the actionable hint is lost). 4xx is reserved for
genuinely bad requests: unknown `config_id` (404), missing
`base_url`/`model` (422).

Response fields:

- `ok` — `true` when connected. The single field the UI branches on.
- `error` — human-readable cause; never raw provider JSON.
- `hint` — the next concrete step.
- `provider_error` — the gateway's own body, or
  `(empty body, HTTP 401)` when it answers silently.
- `provider_code` — provider error code when the body carries one
  (e.g. `30014`).
- `category` — machine-readable class, see below.
- `retryable` — `true` when repeating the test could succeed.
- `latency_ms` — round-trip time of the probe.

Transport exceptions (`httpx.RequestError`) are classified by walking
the whole `__cause__`/`__context__` chain — an
`ssl.SSLCertVerificationError` sits several layers below the
`httpx.ConnectError` that surfaces:

- `tls_certificate` — "The gateway's TLS certificate could not be
  verified"; not retryable.
- `tls_handshake` — "The TLS handshake with the gateway failed"; not
  retryable.
- `dns` — "The gateway's host name could not be resolved"; not
  retryable.
- `protocol` — "The provider closed the connection without a valid
  reply"; not retryable.
- `connection_refused` — "The gateway refused the connection"; not
  retryable.
- `timeout` — "The provider did not answer in time"; retryable.
- `unreachable` — "The provider could not be reached"; retryable.

HTTP-level failures carry `auth` (401/403/30014), `model_not_found`
(404), `rate_limited` (429), `provider_error` (5xx) and `http_error`
(any other status). Only `rate_limited`, `provider_error` and
`408`/`425` are retryable.

The distinct messages matter: lumping a certificate failure into "could
not be reached" sent operators hunting for a network fault when the
only wrong setting was **Verify SSL certificate**.

The probe sends one `POST {base_url}/chat/completions` with a 5-token
`ping` and **waits as long as the model's own `timeout` allows** — a
busy gateway can legitimately queue a trivial prompt for over a minute,
and a shorter hard-coded ceiling turned that into a false "did not
answer in time". Only the *connect* phase is capped (10 s), so a dead
host or closed port still fails fast.

## 5. Ontology generation from data (Graph studio)

`POST /api/graph-studio/infer` (dry run) and
`POST /api/graph-studio/create-ontology` (transactional create) both
accept `ai_enrich: bool`:

1. Structural inference (`ontology_service.infer_ontology`): each
   table → entity; each column → typed attribute; `*_id` columns
   matching another table → ONE_TO_MANY relations.
2. AI enrichment (`main.py:_ai_enrich_ontology`): sends columns and
   sample rows to the effective model, expects strict JSON; fills
   empty entity descriptions and proposes up to eight starter
   questions. Failures degrade silently to the structural result.
3. Create: new pack, schema copied, data rows copied, impact graph
   written, origin recorded, audit entry written; activation is
   automatic.

`create-ontology` additionally records the creation in
`studio_history`, lists the exact source tables in the pack's origin,
and **generates a full report automatically**
(`main.py:_auto_pack_report`): the response carries the report `uid`,
and the title follows `<name> | Scenario: <pack> | Data: <sources>`.
The report is linked from Graph studio's Generation history card
(`GET /api/graph-studio/history`).

### 5.1 Data import and AI proposals (`modules/importer.py`, `modules/induction.py`)

`POST /api/admin/data/upload` carries the whole import in one endpoint
and one shared pipeline (`importer.analyse_upload`, used by both the
preview and the commit):

- **Sheet pick** — `importer.list_sheets` enumerates an workbook's tabs
  (via `pandas.ExcelFile`); `_resolve_sheet` maps a requested sheet
  name, a 0-based index, or the default (first sheet) to
  `pandas.read_excel(sheet_name=…)`. An unknown sheet raises an
  `UploadError` (4xx) that names the available sheets. Non-workbook
  files return no sheets and are read as a single table.
- **Column mapping** — `guess_column_mapping` matches headers to the
  table's non-derived columns by exact key then case-folded substring
  (length ≥ 3). The caller may override per header via the `mapping`
  form field (JSON `header → column key`); `apply_mapping_override`
  re-validates every override and maps `__ignore__` / blank / unknown
  targets to "not imported", so they drop into `unmapped_headers`.
- **Preview vs commit** — `preview_upload` returns the report (valid /
  invalid counts, headers, sheets, `instance_columns`, mapping,
  unmapped headers) without writing. `commit_upload` re-runs the same
  analysis, refuses to write when any row is invalid, and stores the
  valid rows as a new activated dataset (`admin_service.create_dataset`).
- **AI proposals + evidence** — a successful commit schedules
  `induction.induce_from_table` in the background. It sends the table's
  columns and sample rows to the model, which returns strict JSON
  elements each carrying a `rationale`; `_build_evidence` also stores
  the exact table/columns/sample rows it saw in
  `ontology_proposals.evidence`, so the admin review queue can show the
  basis for each proposal.

**Data-type JSON.** `parse_data_file` also accepts `.json` (registered
in `config.ALLOWED_UPLOAD_EXT`). `_read_json_records` decodes the bytes
across common encodings and normalises a top-level list, or a
`{"records" | "data" | "rows": [...]}` wrapper, into rows; a bare
object becomes a single row. A JSON file whose keys look like an
ontology definition (`entities` / `node_types`) is rejected with an
`UploadError` pointing at the pack-import path instead.

**Batch import** — `POST /api/admin/data/batch-upload` (form fields
`pack_id`, `commit`, `plan`, one or more `files`) reuses the same
single-table pipeline for every unit of work. One *item* is a sheet of
a workbook (via `importer.list_items`) or the whole of a CSV/JSON file.

- **Expansion** — `list_items` splits each uploaded file into items;
  folder uploads keep the browser's relative paths and `_basename` /
  `_stem` normalise the names before matching.
- **Routing** — `analyse_batch` resolves each item's target table in
  order of precedence: an explicit `plan` entry (`target` = an existing
  table key or the `__new__` sentinel), else `admin_service.find_table_by_name`
  (exact key, then `ontology_service._key_tokens` set equality, then
  token intersection), else a new table inferred by `_infer_new_table`.
  Items matching no table become `__new__` proposals the admin
  confirms or re-routes in the review UI.
- **New tables** — `admin_service.create_table` inserts the
  `pack_tables` row and its `pack_columns` wholesale; `commit_batch`
  first calls `ontology_service.upsert_entity` / `upsert_attribute`
  per column and runs `sync_visual_graph`, so a batch-created table is
  a first-class part of the ontology, not just a data table.
- **Preview vs commit** — `analyse_batch(with_rows=False)` powers the
  preview and reports per item `{file, item, table key, mapping,
  valid/invalid counts, skip}`; `commit_batch` refuses the whole
  request (`UploadError`, 4xx, nothing written) when any non-skipped
  item is empty or invalid, otherwise writes one activated dataset per
  item and returns `items` plus the `created_tables` it built.

## 6. Reports

`modules/report_builder.py` renders the requested sections into
Markdown, HTML (print-friendly, base64 charts) and PDF. Section
numbering adjusts to what is present. Notable sections:

- `ontology` — pack nodes/links rendered via
  `report_charts.render_chain_graph` into `charts/ontology_graph.png`;
- `analytics` — `analytics.mom_attribution` (headcount / productivity
  / mix effects vs. prior month) and `analytics.agent_stats`
  (per-channel registered/active/avg productivity) tables;
- AI interpretation is appended only when a model is configured.

`POST /api/reports/sample` answers the pack's recommended questions
with the rules engine (`raise_on_template_error=False`), then builds a
report with all sections — no AI cost. Its title carries the scenario
and data sources (`Sample Analytics Report | Scenario: <pack> |
Data: <sources>`). `GET /api/reports/{uid}/view` serves the HTML
inline.

`GET /api/dashboard` assembles the user-portal dashboard in one call:
the resolved pack (name, description, origin), its data sources (kind
and row count), KPI cards and the monthly timeseries. **KPI cards are
driven by the pack's own `metric_formulas`** — each formula whose input
table has rows becomes one card, formatted by its `fmt` / `decimals` —
so any ontology shows its own metrics (VNB/APE, IFRS 17 CSM / ISR / EV,
lapse rates, …) instead of a fixed VNB/APE key list. The response also
returns `formula_count`, letting the UI tell "no formulas declared"
apart from "formulas declared but their tables are still empty". When
the pack carries a compatible monthly series, `analytics.company_series`
adds latest-month APE / VNB / margin / MoM cards. On first call per
user + pack it also generates a full
**"Full Analytics Report"** via `_ensure_auto_report` (idempotent,
flagged `auto_report:<user>:<pack>` in `app_settings`) and returns it
as `auto_report` — the dashboard banner links to
`/api/reports/{uid}/view`.
`GET /api/graph-studio/history` returns the caller's 30
newest `studio_history` rows. `GET /api/admin/user-settings` joins
users with their pack and model selections for the admin support
table.

`GET /api/search/insight?q=` (Ontology workspace instant insight)
returns the search chips, the matched graph nodes (`node_keys`), a
1-hop neighbourhood sub-graph (`subgraph.nodes/links`) and a generated
analysis card (`main.py:_insight_analysis`) describing the top match —
entity (attributes, backing tables, relations, formulas), KPI node
(value, upstream/downstream), formula (expression, unit) or
attribute/relation (mapped to its owner entity).

## 7. Analytics identity

The MoM decomposition is exact (sequential factor substitution, no
residual), per channel:

```text
VNB = active_agents × avg_productivity / 1000 × weighted_margin

agent effect  = (A1 − A0) × P0/1000 × M0
prod effect   = A1 × (P1 − P0)/1000 × M0
mix effect    = A1 × P1/1000 × (M1 − M0)
```

Endpoints: `/api/analytics/timeseries`, `/attribution`, `/scenario`,
`/agent-stats`. The driver-attribution, scenario and agent views read the
built-in VNB/APE monthly schema (`analytics.REQUIRED_TABLES`:
`vnb_monthly`, `channel_monthly`, `agent_monthly`, `agent_master`) and
activate only when the active pack carries those tables.

**Every pack gets a trend.** `/api/analytics/timeseries` also returns a
`generic` block built by `analytics.generic_monthly(data)`: the pack's own
monthly table (the one whose `report_month` / `month` column holds `YYYY-MM`
values — `report_month` preferred, then the table with more rows) with one
measure per numeric column (`sum`, or `mean` for rate-like names). When the
pack holds a lapse count and an exposure column, an extra **annualised
lapse rate** measure (`12 × lapses ÷ exposure`) is prepended and chosen as
the default, matching the dashboard's annualised-lapse-rate KPI. The user
portal renders this as a "Monthly trend from this pack" card with a measure
selector, chart and month table; the fixed-schema driver / scenario / agent
cards stay hidden. For packs with **no** monthly table, `requirements`
(required vs. present vs. missing tables, their key columns and each table's
purpose via `analytics.TABLE_PURPOSE`) is returned and the UI renders an
explicit "what this view needs" panel — not an error — with three routes:
switch the Scenario selector to a compatible sales pack, upload the four
tables through Data centre, or use the pack's own Dashboard metrics.

## 8. Security notes

- Sessions are HttpOnly cookies whose value is an HMAC-signed
  `user_id|expires` payload (`auth.issue_session_token`). The token is
  also written to the `sessions` table so logout revokes it immediately
  on the issuing instance; other serverless instances verify the
  signature and load the user directly, so a cookie survives cold starts
  and per-instance `/tmp` databases. `/api/admin/*` requires the admin
  role; the admin UI is a separate page with no cross-links from the
  user portal.
- API keys (system and user) are encrypted with a machine-local key
  (`.onti.key`) and never returned unmasked by any API.
- **Credential normalisation.** A key pasted from e-mail, a spreadsheet
  or `.env` routinely carries wrapper quotes, a `Bearer` prefix or a
  trailing newline. Sent verbatim, the provider answers
  `{"code":30014,"message":"Token is invalid."}` even though the key is
  valid. `llm_client.normalize_api_key` therefore strips whitespace,
  quotes (straight, back-tick, typographic), a leading `Bearer` scheme
  and collapses known placeholders (`sk-...`, `your-api-key`, …) to an
  empty string; `normalize_base_url` trims spaces and trailing slashes.
  Both run at **every boundary** — `create_config` / `update_config`,
  the per-user equivalents, `resolve_test_config` (including legacy rows
  written before normalisation existed) and `ConfiguredLLM.__init__`,
  the last gate before the `Authorization` header is built. Dirty values
  are never persisted, and an empty key sends no `Authorization` header
  at all.
- **Provider errors are translated.** `llm_client.describe_provider_error`
  maps the provider's status/body into `error` (human cause), `hint`
  (next step) and `provider_error` (raw body, 300 chars, for support),
  plus `provider_code`. The model form renders all three, so the admin
  never has to read raw provider JSON.
- Formula evaluation runs in a sandboxed expression parser; arbitrary
  Python is rejected at save time.
- All state-changing endpoints require confirmation in the UI and
  write to the `audit` table.

## 9. Testing

```bat
python -m py_compile main.py modules\*.py   :: syntax gate
python tests\verify_t1.py ... verify_t6.py  :: layer checks (no server)
python tests\verify_model_key.py            :: key paste hygiene + error text
python tests\smoke_http.py                  :: end-to-end HTTP matrix
python tests\verify_t7.py                   :: upload sheets/mapping + evidence
python tests\verify_t8.py                   :: batch import (sheets/CSV/folder/JSON)
python tests\verify_t9.py                   :: lapse pack end to end
python tests\verify_login.py                :: 24 checks — auth flows
python tests\verify_model_endpoints.py      :: 65 checks — model CRUD + diagnostics
python tests\verify_ontology_value.py       :: 14 scenarios; writes the value report
```

The smoke suite covers the anonymous / user / admin cookie matrix,
SSE answers, report generation, uploads and the admin API surface.
It activates the built-in sample pack for its pack-specific assertions
and restores the previously active pack afterwards.

> **Testing note.** When driving the app from Electron-based automation
> browsers, the console may log a single `net::ERR_ABORTED` for
> `/api/ask-stream` after an answer completes. This is an Electron
> fetch-streaming quirk, not an application error: the chunked SSE
> framing was verified byte-complete with a raw HTTP client, and
> answers always render fully in regular browsers.

`verify_t7.py` runs against the live server and covers the data-import
path end to end: workbook
sheet listing and default first-sheet pick, explicit sheet selection,
the unknown-sheet 4xx error, manual column-mapping overrides
(force-map and force-ignore → unmapped), a sheet + mapping commit
round-trip (dataset deleted afterwards), and AI-proposal evidence
storage and retrieval. `verify_t8.py` covers batch import:
one workbook splitting into one item per sheet, name-matched items
routing to existing tables and unmatched items becoming new tables, a
new-table commit creating the table plus its entity/attributes and
syncing the graph, several CSVs committing in one request, data-type
JSON import, ontology-definition JSON rejected with a pack-import
hint, an empty item flagged not-ready in preview and refused at commit
with nothing written, and self-cleanup that restores the pack. Both
tests build their `lapse_experience_synthetic_demo` fixture pack on
demand from the real `data/Lapse_mockdata_v3 - Scenario Demo.xlsx`
via `tests/_seed_synthetic.py` (the pack ships in neither the database
nor the seed) and remove it again when they created it.

---

### 9.1 Cross-scenario "With vs Without ontology" comparison (2026-10-29)

Six real scenarios across two ontology packs were exercised end-to-end
via `POST /api/ask/compare` to quantify what the ontology layer adds
over a data-grounded baseline (same uploaded tables, no entity
definitions, no metric formulas, no graph).

**Test matrix** (each question answered twice by the same model — once
grounded in the full ontology, once grounded in raw uploaded tables):

| #   | Pack           | Question                                                |
| --- | -------------- | ------------------------------------------------------- |
| 1   | 6 · VNB        | What is the VNB in April?                               |
| 2   | 6 · VNB        | How does VNB in April compare to the previous month?    |
| 3   | 6 · VNB        | Why is there such a movement in VNB during April?       |
| 4   | 23 · Lapse Exp | What is the lapse experience in September?              |
| 5   | 23 · Lapse Exp | How does September lapse compare to the previous month? |
| 6   | 23 · Lapse Exp | Why is there such a movement in lapse during September? |

**Results (all 6 scenarios completed successfully):**

| Dimension                                         |               With Ontology                |     Without Ontology     |
| ------------------------------------------------- | :----------------------------------------: | :----------------------: |
| Answer source                                     | **Rules engine (6/6, 100% deterministic)** | LLM AI (6/6, stochastic) |
| Structured tables in answer                       |                    5/6                     |           6/6            |
| Impact-chain graph nodes                          |        59 total across 6 scenarios         |          **0**           |
| Impact-chain graph edges                          |                  50 total                  |          **0**           |
| Matched ontology concepts                         |               4–6 per answer               |          **—**           |
| Metric definitions used                           |                7–9 per pack                |          **0**           |
| Admits data limitation ("cannot determine cause") |           **0/3** why-questions            | 2/3 lapse why-questions  |

**Key findings.**

1. **Rule engine hit rate = 100%**. All six questions hit a compiled
   rule template. Output is fully deterministic — re-running produces
   byte-identical answers. Without-ontology answers are stochastic LLM
   generations; any two runs will differ in wording.

2. **Impact-chain graphs are ontology-exclusive.** Across 6 scenarios
   the ontology side visualised **59 graph nodes / 50 edges** that
   matched concepts like `TotalVNB`, `AgentProductivity`,
   `ActiveAgentCount`, `WeightedVNBMargin`, etc. The baseline side has
   no graph and no matched concepts — it treats every column literally.

3. **Lapse why-question is the clearest contrast.** With ontology:
   cohort-level breakdown (WP PY1 Agency +3.0 pp, WP PY1 Worksite
   Marketing +2.0 pp…) plus a **financial bridge table** showing CSM,
   Insurance Service Result, and EV impact in $M by framework
   (IFRS17 / EV). Without ontology the model replies
   *"I cannot determine a definitive cause … the uploaded tables
   contain lapse outcomes but do not include any explanatory
   variables"* — correctly identifying that the raw tables lack what
   the ontology's `metric_formulas`, `graph_nodes` and
   `graph_edges` provide.

4. **Rule-sort bug fix discovered during testing.** The VNB pack had
   `vnb_gap` (`sort=4`) preceding `mom_attribution` (`sort=27`). The
   regex pattern `why.*vnb` in `vnb_gap` captured *"Why is there such
   a movement in VNB"* before `mom_attribution`'s `movement` pattern
   could run. Fix: raised `mom_attribution` to `sort=1`, dropped
   `vnb_gap` to `sort=28`. Post-fix the why-question now returns a
   proper attribution table — *Active-agent effect +$0.045M,
   Productivity +$0.042M, Mix/margin +$0.012M, per-channel breakdown*
   — and 20 graph nodes instead of a 173-char gap paragraph.

5. **Same data, different reasoning.** Both sides ground in the same
   uploaded tables (12 tables for VNB, 4 for Lapse). The difference
   comes from the ontology layer adding: metric formulas (8–9 per
   pack), entity-to-table mappings, graph edges that connect drivers
   to outcomes, and rule templates that compose them into answers.

**Regression suites after all changes:**
`tests/verify_login.py` 24/24 ✅ · `tests/verify_model_endpoints.py`
65/65 ✅. No pre-existing regressions introduced.

### 9.2 Deep-analysis validation on the v4 pack (2026-10-29)

The 9.1 run measured *grounding counts*, not *reasoning depth*, and it
used a workbook that carried a pre-written `NARRATIVE DRIVER` column —
which the baseline model simply parroted, making the two sides look
equivalent. The v4 workbook (`BPTablesTest_v4_clean.xlsx`) drops that
column: six sheets, pure relational data, 3,552 rows. The pack
(`bp-planning-v4`) was rebuilt from it and the deterministic reasoning
layer added.

**Test suite:** `tests/verify_ontology_value.py` — 14 scenarios across
six dimensions (level, movement, mix, cohort, trend, coverage), each
answered twice through `POST /api/ask/compare` by the same model.

**Scored rubric (0–5 per axis):** business depth, logical rigour, data
linkage, explainability.

| Axis           | With ontology  |   Plain LLM   |   Delta    |
| -------------- | :------------: | :-----------: | :--------: |
| Business depth |    4.14 / 5    |   2.14 / 5    |   +2.00    |
| Logical rigour |    5.00 / 5    |   0.43 / 5    |   +4.57    |
| Data linkage   |    5.00 / 5    |   3.14 / 5    |   +1.86    |
| Explainability |    5.00 / 5    |   1.00 / 5    |   +4.00    |
| **Total**      | **19.14 / 20** | **6.71 / 20** | **+12.43** |

Every scenario scored 19–20 for the ontology side; the baseline ranged
1–10. The numbers are reproduced per question in
[ONTOLOGY_VALUE_REPORT.md](ONTOLOGY_VALUE_REPORT.md) and stored raw in
`docs/ontology_value_report.json`.

**What produced the gap.** The ontology side carries, on every answer:
the exact per-segment factor bridge (residual $0.000M), the mix-weighted
margin proven against the channel's implied VNB/APE margin, three
agent-cohort tables from a second join, seven statistical tests (OLS
trend + R², CV, z-score, momentum, HHI, driver correlation,
reconciliation), a numbered reasoning trace naming each method and table,
and an annotated impact chain from the graph. The baseline, given the
same raw tables, could restate the headline figure — correctly, at
2,907 chars in its longest answer — but never joined a second table,
never decomposed the move and never reconciled anything.

**Why the earlier comparison was not fair.** With the `NARRATIVE DRIVER`
column present, the baseline's answer was essentially a copy of the
human-written explanation, so it read more fluently than the ontology
answer at that time. Removing the column also required no ontology
change: the pack's column keys, relations and formulas are schema-level,
so explanatory text never had to be modelled.
