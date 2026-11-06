# Onti — Technical Documentation

> Audience: developers and integrators. For usage see
> `docs/USER_GUIDE.md`and`docs/ADMIN_GUIDE.md` ; for setup see the
> root `README.md` .

---

## 1. Architecture

```text
Browser (vanilla JS)
  │  fetch / SSE
  ▼
FastAPI app (main.py)        ← app assembly + exception handlers only
  │
  ├── routes/                ← one APIRouter module per feature area
  │     (auth, packs, config, graph, analytics, ask, reports,
  │      admin_packs, admin_data, admin_models, admin_users,
  │      graph_studio, my_models; shared helpers in routes/common.py)
  ├── common/logger.py       ← AppLogger (dependency-free console logger)
  ├── common/exceptions.py   ← UploadError — status-carrying I/O failure shared
  │                              by importer, admin upload and batch paths
  ├── common/io_utils.py     ← Pure file parsers (CSV / XLSX / XLS / JSON →
  │                              pandas frames, workbook item expansion)
  ├── common/values.py       ← Dependency-free value helpers (is_num,
  │                              month_label) shared by analytics / charts
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
  ├── modules/self_iterate.py ← dual-loop engine: critic, inner loop,
  │                             harvest → proposals, evolution stats
  ├── modules/constants.py   ← operational literals (truncation, snippet,
  │                            report/import limits) — no magic numbers
  ├── modules/run_log.py     ← append-only run/audit trail (JSONL)
  └── modules/db.py          ← SQLite schema, settings, audit, crypto
        ▼
      data/onti.db (SQLite, WAL; /tmp/onti-data on read-only hosts)
```

No external services: one Python process, one SQLite file. The
frontend is server-rendered Jinja2 plus four static JS files
( `common.js`,`graph.js`,`app.js`,`admin.js` ) — no build step.

`modules/db.py:_resolve_data_dir` picks the writable data directory:
`ONTI_DATA_DIR`when set,`/tmp/onti-data`when`VERCEL` is present
(the deployment itself is read-only), otherwise the bundled `data/`

folder. On first connection the bundled `data/onti.db` is copied into
that writable directory, so a fresh serverless instance starts with the
same seeded packs, accounts and report metadata. Files under
`data/reports/` that are not present in the writable directory are read
from the bundled copy ( `report_builder._abs` ). The master key
(`db._machine_key`) resolves in this order: `ONTI_SECRET_KEY`, then a
`data/.onti.key` file (writable directory first, bundled copy second),
then — on an ephemeral host only — a key derived from the shared storage
credential (`BLOB_READ_WRITE_TOKEN` or `ONTI_SNAPSHOT_TOKEN`), so every
instance signs and validates the same session cookies. A randomly
generated per-instance key is the last resort and is logged as a warning,
because it makes cookies and encrypted model keys instance-local.

`db.EPHEMERAL_STORAGE` marks hosts whose filesystem is *wiped between
instances* — it is true only when the `VERCEL` environment variable is
present, or when `ONTI_EPHEMERAL=1` is set explicitly. It is **not**
derived from the data-directory path: a Docker container points
`ONTI_DATA_DIR` at its mounted volume and is therefore fully persistent.
On an ephemeral host writes live only for that instance, so
`/api/auth/status`reports`ephemeral: true` ; `modules/snapshot.py`
mirrors accounts and the database to an external store (see below).
Without a snapshot backend, accounts stay per-instance **and
self-registration is refused** (`503`, see the auth contract): it is
better to say so than to create an account the next instance cannot see.

**Snapshot sync ( `modules/snapshot.py` ).** Two backends, selected by
environment: `BLOB_READ_WRITE_TOKEN` → Vercel Blob (REST, snapshot stored
under an unguessable prefix derived from the signing key because Blob
URLs are public), or `ONTI_SNAPSHOT_URL` + `ONTI_SNAPSHOT_TOKEN` → any
HTTP endpoint ( `PUT` to upload, `GET` to download). On cold start
`db._prepare_data_dir` restores the latest whole-database snapshot before
falling back to the bundled database; uploads happen after every account
mutation (`snapshot.request_sync`, burst-collapsed to one upload per 5 s)
and on a schedule via `GET /api/internal/snapshot-sync` (wired as a daily
Vercel Cron, authenticated by `CRON_SECRET` when set). All failures are
logged and swallowed, so a store outage never breaks a request; with no
backend configured every call is a no-op.

**Account records (the durability fix).** A whole-database snapshot is
last-writer-wins: an old instance uploading late could erase an account a
newer instance had just created. Accounts therefore have their own
record, `shared/accounts/<sha256(username.lower().strip())>.json` (no
dependence on the master key, so rotating it never orphans accounts),
written *before* the transaction that changed the account commits:

* **Self-registration** — `auth.register_account`; a failed upload raises
  `503` and rolls the transaction back, so no phantom account survives.
* **Admin create** — `routes/admin_users`, inside the same transaction.
* **Role / enable / password** — `auth.set_user_role`,
  `set_user_enabled` and `reset_password` (best effort; failures are
  logged and do not abort the administrative change).
* **Sign-in on a foreign instance** — `auth._register_login_row` inserts
  the stored record and the normal password check follows.
* **Password reset on another instance** — `auth._refresh_login_row`
  adopts the newer hash when the local one rejects the password.
* **Delete** — not exposed by the API; `snapshot.delete_account` exists
  for operators.

`snapshot.restore_accounts` runs at startup on ephemeral hosts and lists
the blob prefix (HTTP backends adopt lazily at sign-in). Each record
carries the PBKDF2 hash, role, enabled flag and timestamps — never a
plaintext password — and a record for one account can never remove
another.

## 2. Data model (SQLite)

Core tables (selection):

* `users` — accounts, PBKDF2 hashes, roles, e-mail address + verified flag,
  `password_changed_at` (rotation clock), `mfa_secret` (base32 TOTP key),
  `mfa_enabled` (flag, sign-in branches on this)
* `password_history` — one row per password ever set, kept 24 months;
  reuse is rejected by hash comparison
* `email_otp` — mail challenges: purpose, address, `SHA-256(id:code)`,
  payload for the verify step, TTL, attempts, `consumed_at`
* `sessions` — session tokens (signed + stored)
* `revoked_sessions` — tokens revoked by logout (checked before use)
* `ontology_packs` — one row per ontology (see origin note below)
* `entities`, `attributes`, `relations` — the ontology schema
* `graph_nodes`, `graph_edges` — impact graph (auto-generated if empty)
* `metric_formulas`, `question_rules` — computation + rules engine
* `tables`, `datasets` — logical data tables and versioned sources
* `question_bank` — canonical questions
* `user_questions` — per-user private question lists
* `ask_history` — one row per answered question (see memory note)
* `studio_history` — Graph studio generation history log
* `model_configs` — admin-registered models (keys encrypted)
* `user_model_configs` — per-user Model centre entries (keys encrypted)
* `llm_calls` — token metering: caller, model, success, latency
* `audit` — append-only write log
* `app_settings` — key/value store: user model + pack selections
* `self_iter_sessions` — inner-loop runs: scores, rounds, improved flag
* `self_iter_findings` — outer-loop ontology gaps (open/proposed)

Four columns deserve detail:

* `ontology_packs.origin` — provenance string (Graph studio
  generation with table list and username, JSON import, or seed) shown
  as the 🧾 badge in the admin pack bar.
* `ontology_packs.version` — optimistic-lock counter bumped by every
  pack write (element upsert/delete, graph sync, import, restore).
  Write endpoints accept an `expected_version` parameter and answer
  HTTP 409 when another session already moved the pack forward, so
  concurrent admin edits never overwrite each other.
*`ontology_proposals.evidence` — the JSON snapshot of*what the model
  was shown* when it drafted the proposal: target table key/label,
  dataset id, the table's column specs and the sample rows
  ( `modules/induction.py:_build_evidence` ). Returned by
`list_proposals`alongside`rationale` so the admin review queue can
  render the "Proposing against" panel.
* `ask_history` — user, pack, question, answer excerpt, source,
  matched flag and token count. Powers the Recent asks chips, the
  History view and the AI's cross-session memory.

`studio_history` records every Graph studio action per user: pack,
action ( `ontology`/`preview`/`data-upload`/`ontology-upload` /
`report` ), title and detail (for reports, the report uid). It powers
the user-facing Generation history card.

Migrations run in `modules/db.py:init_db`— e.g.`ontology_packs`

gains `origin`and`version`,`ask_history`gains`answer` , and
`ontology_proposals`gains`evidence`, all via idempotent`ALTER TABLE`

guards, so older databases upgrade in place.

## 3. Identity, provenance and memory

### 3.1 Pack origin

Every write path that creates a pack records `origin` :

* Graph studio: `Generated from data in Graph studio (tables: t1,
  t2) by <username>`(plus`+ AI enrichment` when requested);
* Admin import: `Imported from JSON file by <admin>`;
* seed packs carry a descriptive origin.

The value is surfaced as the 🧾 badge in the admin pack bar.

The built-in sample pack ( `vnb-ape-sample` ) is auto-upgraded by
`seed.upgrade_sample_pack` on every startup (idempotent, flagged
`sample_pack_upgraded_v2:{pack_id}` ): it adds the five monthly
data-table entities (VNB Monthly, Channel Monthly, Agent Monthly,
Product Mix Monthly, Scenario Analysis) with attributes and relations
(10 entities / 13 relations in total, one relation per table-to-table
link such as channel aggregates VNB, agent rolls up to channel), and
repairs a degraded impact graph (only auto-generated entity nodes with
wiped values) by rebuilding the rich KPI chain — 26 nodes / 30 edges
including the table-level nodes X1–X8.

### 3.2 Graph studio provenance

* `GET /api/graph-studio/sources` annotates every ontology table with
  its source dataset ( `dataset_name`,`dataset_file`,`dataset_kind` ,
`dataset_updated`from the newest`datasets` row).
* Every graph node carries `source {kind, detail}`; the UI provenance
  panel and the per-view "Sources used" list render it.
* Views: `lineage`(default),`schema`,  `columns` — built by
`ontology_service.build_studio_graph` .

### 3.3 Per-user scenario packs

`app_settings["user_pack:{uid}"]` stores the pack a user picked in
the header dropdown. `routes/common.py:user_pack_id` resolves it with fallback
to the admin-activated pack, and every user-facing read (graph,
dashboard, analytics, Ask, Graph studio, reports, memory) goes
through `resolve_pack`.`GET /api/packs` lists the visible packs with
`is_active`/`selected` flags, origin and data-source summaries;
`POST /api/my/pack`sets the selection,`{"pack_id": null}` resets it
to the system default.

### 3.4 Ask memory (short + long term)

`routes/ask.py:ask_stream`assembles the LLM`history` from:

1. the current session's turns (client-supplied, short-term);
2. the four most recent `ask_history` answers for this user + pack
   (long-term, cross-session), truncated to 600 chars each
   ( `constants.MEMORY_ANSWER_CHARS` ).

After the stream finishes, the question, answer excerpt, source,
matched flag and token count are inserted into `ask_history` .
`GET /api/ask/history` returns the last 30 asks per active pack.

**Rules-engine month inheritance.** `ontology_service.answer_question`

also accepts the same `history` ; when the current question names no
month ( `_extract_month_prefixes` finds none), the engine walks the
history from newest to oldest and borrows the month from the most
recent user turn that names one, so follow-ups like *"How does it
compare to the previous month?"* stay on the month asked about before.
Inside rule templates, `month_context(question, rows)` resolves that
month against the data (a year-less name such as "Sep" picks the most
recent matching month), `filter_by_month(rows, month)` selects its
rows, `month_movement(rows, tgt, prev)` aggregates both months and
ranks the biggest cohort movers, and `attr_for(question)` returns the
driver attribution for that month. `/api/ask/compare` builds the same
memory before calling `compare_answers` , so the comparison's
with-ontology side inherits the month identically.

### 3.5 With / without ontology comparison

`POST /api/ask/compare`(body`{question, mode?, history?}` ) answers
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

* **With ontology** runs the normal pipeline synchronously
  ( `qa_service.compare_answers` ) in three stages:
  1. `deep_analysis.analyze()` — the deterministic reasoning layer (see
     §3.5.1) computes every figure from the pack's own tables;

  2. `rule_answer` — the rules engine renders its canonical answer when
     a rule matches;

  3. the two are concatenated (rule answer first, then the analysis
     body) and `retrieve_subgraph` builds the impact chain.

`source`is`rules+analysis`,`rules`, or`analysis` accordingly. The
  analysis alone is enough to answer, so the grounded side still works
  when no model is configured. `analysis.depth` carries the counters
  quoted in `value` (steps, statistical tests, findings, tables joined,
  dimensions, reconciliation).

* **Second wheel (`narrative`).** When a model is configured, the same
  model is called a second time with
`llm.build_synthesis_prompt(analysis, ontology_context, rules_answer)` .
  In that prompt the model is a **narrator only**: it must reuse the
  pre-computed numbers verbatim and explain the reasoning chain. This is
  the "rule engine + LLM" joint mode; it never recomputes, so the
  deterministic evidence stays authoritative.
* **Without ontology** calls the same model with
 `llm.build_baseline_prompt(ontology_service.build_table_profiles(pack))`

  — the **real uploaded data tables are included** so the model can
  reason over company figures, but the ontology layer (entity
  definitions, metric formulas, graph edges, impact chain) is
  deliberately stripped. `graph`is always`null` because there is
  nothing to trace. Grounding reports the same data tables the
  ontology side saw ( `tables`,`table_count`) with`metric_count: 0` .
  No conversation history is forwarded, so ontology-grounded context
  cannot leak across.
* Only the grounded answer is written to `ask_history`; the baseline is
  not stored. The endpoint runs the two blocking model calls via
`asyncio.to_thread` , so the event loop stays free.
* When no model is configured the grounded side still answers through
  the rules engine and the baseline side explains that it needs a model
  ( `source: "unavailable"` ).
* **Portal rendering.** `static/js/app.js:renderCompareCard` renders
  exactly two columns — the grounded answer (with its impact chain) and
  the baseline answer. No explanatory banner or counter strip is drawn:
  the `value` object stays in the response for API clients and for
`tests/verify_ontology_value.py` , which scores the gap from it.

#### 3.5.1 Deterministic reasoning layer ( `modules/deep_analysis.py` )

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
4. **Cross-check dimensions** — join `mix`→`product` for the weighted
   margin and split the change into mix vs product-margin effects; join
`agent_monthly`→`agent_master` for churn-band, certification and
   tenure cohorts.
5. **Statistics** — OLS slope + R², coefficient of variation, z-score of
   the move into the target month, 3-vs-3 momentum, Herfindahl
   concentration, driver correlation and the reconciliation residual.
6. **Explain** — render a focus-aware report (the section matching the
   question type leads), a ranked finding list, a numbered reasoning
   trace and an annotated impact chain walked over the pack's graph.

`detect_focus(question)`selects one of`level`,`movement`,`mix` ,
`cohort`,`trend` ; follow-up questions inherit the month named earlier
in the conversation, mirroring the rules engine's own memory.

`POST /api/ask-stream` uses the same layer: a rule hit is followed by the
analysis body, and AI mode swaps the plain system prompt for
`llm.build_synthesis_prompt(...)` , which forbids the model from
recomputing.

### 3.6 Global ontology mode

A second, system-wide control decides whether the ontology is used at
all. It is a **client-side preference**, not a server setting: both
portals render the same **Use ontology ON / OFF** control from
`Onti.ontology`in`static/js/common.js` , which persists the choice in
`localStorage`under`onti.useOntology`(default`"on"` ) and broadcasts
an `onti:ontology` DOM event on every change.

| Key     | `onti.useOntology`                                           |
| ------- | ------------------------------------------------------------ |
| Default | ON                                                           |
| Scope   | Per browser; both portals on the same origin share it        |
| Toggle  | `Onti.ontology.mount()`in the header of`app.js`and`admin.js` |
| Read    | `Onti.ontology.enabled()`                                    |

With the switch OFF, no data is sent anywhere different — the front end
simply refuses to render ontology-derived output and says so:

* `loadDashboard` replaces KPI cards with an off panel and hides
`#dashGraphCard`;`loadAnalytics`and`loadGraphStudio` render the same
  panel instead of charts; the admin `loaders.graph` does likewise.
* `askCore`/`compareCore`delegate to`askWithoutOntology`, which calls
`POST /api/ask/compare`and renders only its`without_ontology` side
  (badge “Without ontology”, no impact chain).
* `POST /api/reports`accepts`use_ontology: false`; the server removes
  the `evidence`,`charts`,`analytics`and`ontology` sections (and
  falls back to `["summary"]` if that empties the list, so
`report_builder.build_report` never re-applies its default section
  set), and suffixes the title with “— No ontology (text-only)”.
  Reports therefore still generate and download.
* A dismissible orange `.onto-off-banner` is inserted under the topbar
  by `Onti.ontology.renderBanner()` , and every off panel carries a
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

| Block                | Content                               | Cap      |
| -------------------- | ------------------------------------- | -------- |
| Column profile       | type + sum/avg/min/max or value list  | ≤ 48     |
| Month coverage       | month columns normalised to `YYYY-MM` | ≤ 36     |
| Per-month aggregates | month × numeric-column sums/averages  | ≤ 36 × 8 |
| Sample rows          | 3 verbatim rows for format demo       | 3        |

Everything else (entities, relations, formulas, graph edges) is unchanged.
Callers: `qa_service.stream_answer`/`compare_answers` and
`report_builder.build_report` — all pass no arguments, so all three share
the same complete grounding. `compute_pack` itself always had every row; the
truncation existed only in the prompt layer.

Monthly experience tables are therefore fully covered: the model can list
every month and quote per-month totals (e.g. Aug 55 / Sep 63 lapses) without
ever seeing a "rows omitted" note.

### 3.8 Self-iterating agent — dual-loop architecture

Opt-in self-improvement for answers *and* the ontology, implemented by
`modules/self_iterate.py` (engine) and `routes/self_iterate.py` (HTTP).

**Feature gate.** Per-user setting `self_iter:<user_id>` in
`app_settings`, default **off** (`"0"`). `GET /api/self-iterate/status`
returns `{"available", "enabled", "stats"}`; `PUT
/api/self-iterate/toggle` persists the switch and writes an audit row.
Every endpoint re-checks the flag server-side — the UI switch is only a
convenience, never the boundary.

**Inner loop** (`run_inner_loop`, max `MAX_ROUNDS = 3` rounds):

1. *Answer* — mirrors the standard grounded pipeline: rules engine
   first (`qa_service.rule_answer`), then the deterministic deep-analysis
   layer (`deep_analysis.analyze`), else the model with the ontology
   system prompt.
2. *Critique* — `critique()` is a **deterministic** scorer (no model
   needed): rule match +25, analysis +10, reasoning depth ≤ +10, graph
   evidence +10/+5, metrics +10, tables +5, substance ≤ +15 (max 90).
   Missing components become machine-readable `issues`
   (`missing_rule`, `shallow_analysis`, `coverage_gap`, `metric_gap`,
   `thin_answer`) with human-readable `hints`.
3. *Refine* — while the score is below `PASS_SCORE = 70` and a model is
   available, `_refine_prompt()` appends the draft plus its critique to
   the system prompt and the model re-answers; the best-scoring draft
   wins. The full `iterations` trace (round, answer, score, issues) is
   returned for the UI's score bars.

**Outer loop** (human-gated evolution):

* `record_session()` persists every run in `self_iter_sessions`
  (scores, rounds, improved flag) and writes the gap-kind issues as rows
  in `self_iter_findings` (`open`).
* `harvest()` turns open findings into additive `node` proposals in the
  existing `ontology_proposals` queue (key `si_<kind>_<slug>`, deduped
  against pending proposals and existing graph keys) and marks the
  findings `proposed`. An administrator approves through
  `induction.approve_proposal` — the same validated `upsert_*` write
  path as every other ontology change, with a pack-version snapshot.
* `stats()` aggregates evolution metrics (sessions, average
  initial/final score, improved share, average rounds, finding counts).

**HTTP surface** (`routes/self_iterate.py`):

| Endpoint                         | Meaning                           |
| -------------------------------- | --------------------------------- |
| `GET /api/self-iterate/status`   | availability + toggle + stats     |
| `PUT /api/self-iterate/toggle`   | flip the per-user switch          |
| `POST /api/ask/compare3`         | three-way comparison payload      |
| `GET /api/self-iterate/findings` | findings of the caller's pack     |
| `POST /api/self-iterate/harvest` | open findings → pending proposals |

`POST /api/ask/compare3` runs the inner loop and `qa_service.compare_answers`
concurrently (`asyncio.gather` + `asyncio.to_thread`), returning
`self_iterated` (answer, iterations, scores), `with_ontology`,
`without_ontology` and a `value` strip (score lift, rounds, chain
nodes/edges, metrics used, answer chars). It answers **403** while the
caller's switch is off.

**Frontend.** `static/js/common.js` gains an `Onti.selfIter` helper that
mounts the header switch, keeps state server-side and dispatches
`onti:selfiter`. `static/js/app.js` relabels the compare toggle
(◐ Compare ×3), renders the three-column result with the iteration
trace and a quantitative strip, and adds **⟳ Insights** — a modal with
evolution stats, the findings table and the harvest button. Styles:
`.cmp-grid-3`, `.si-trace`, `.si-bar`, `.si-value` in
`static/css/app.css`.

**Tests.** `tests/verify_self_iterate.py` runs against a throwaway
`ONTI_DATA_DIR` and covers the toggle default/round-trip, the critic,
proposal keys, session recording, harvest + dedup, approval
materialisation, the stubbed-model inner loop (refinement, rules-only
single round, round cap) and the full HTTP flow including the 403 gate
and the end-to-end pack evolution.

## 4. Models — system, user, resolution

* System models: `model_configs` (admin CRUD, encrypted keys).
* User models: `user_model_configs` (owner-scoped CRUD via
`/api/my/models*` , encrypted keys, duplicate names rejected 409).
* Selection: `app_settings["user_model:{uid}"]`stores`sys:N`,
`my:N`or empty.`routes/common.py:_effective_model_cfg` resolves it at ask
  time with fallback to the system default.
* `.env` registration (`llm_client.append_env_model`): writes a
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

* Testing: `POST /api/my/models/test`accepts`config_id: "my:N"`
  (resolves the user's stored config server-side) or ad-hoc fields.
* Oversight: `GET /api/admin/user-models` returns every user-created
  model with masked keys and owner username.

### 4.1 Connection-test contract

`POST /api/admin/models/test`and`POST /api/my/models/test` share one
response shape. A test that *ran* always answers **HTTP 200**, whatever
the verdict — a negative result is a successful request, not a client
error (encoding it as 400 used to make every failed test look like a
malformed request in the server log and pushed the UI through its
exception path, where the actionable hint is lost). 4xx is reserved for
genuinely bad requests: unknown `config_id` (404), missing
`base_url`/`model` (422).

Response fields:

* `ok`—`true` when connected. The single field the UI branches on.
* `error` — human-readable cause; never raw provider JSON.
* `hint` — the next concrete step.
* `provider_error` — the gateway's own body, or
`(empty body, HTTP 401)` when it answers silently.
* `provider_code` — provider error code when the body carries one
  (e.g. `30014` ).
* `category` — machine-readable class, see below.
* `retryable`—`true` when repeating the test could succeed.
* `latency_ms` — round-trip time of the probe.

Transport exceptions ( `httpx.RequestError` ) are classified by walking
the whole `__cause__`/`__context__` chain — an
`ssl.SSLCertVerificationError` sits several layers below the
`httpx.ConnectError` that surfaces:

* `tls_certificate` — "The gateway's TLS certificate could not be
  verified"; not retryable.
* `tls_handshake` — "The TLS handshake with the gateway failed"; not
  retryable.
* `dns` — "The gateway's host name could not be resolved"; not
  retryable.
* `protocol` — "The provider closed the connection without a valid
  reply"; not retryable.
* `connection_refused` — "The gateway refused the connection"; not
  retryable.
* `timeout` — "The provider did not answer in time"; retryable.
* `unreachable` — "The provider could not be reached"; retryable.

HTTP-level failures carry `auth`(401/403/30014),`model_not_found`

(404), `rate_limited`(429),`provider_error`(5xx) and`http_error`

(any other status). Only `rate_limited`,`provider_error` and
`408`/`425` are retryable.

The distinct messages matter: lumping a certificate failure into "could
not be reached" sent operators hunting for a network fault when the
only wrong setting was **Verify SSL certificate**.

The probe sends one `POST {base_url}/chat/completions` with a 5-token
`ping`and **waits as long as the model's own`timeout` allows** — a
busy gateway can legitimately queue a trivial prompt for over a minute,
and a shorter hard-coded ceiling turned that into a false "did not
answer in time". Only the *connect* phase is capped (10 s), so a dead
host or closed port still fails fast.

## 5. Ontology generation from data (Graph studio)

`POST /api/graph-studio/infer` (dry run) and
`POST /api/graph-studio/create-ontology` (transactional create) both
accept `ai_enrich: bool` :

1. Structural inference (`ontology_service.infer_ontology`): each
   table → entity; each column → typed attribute; `*_id` columns
   matching another table → ONE_TO_MANY relations.
2. AI enrichment (`routes/graph_studio.py:_ai_enrich_ontology`): sends columns
and
   sample rows to the effective model, expects strict JSON; fills
   empty entity descriptions and proposes up to eight starter
   questions. Failures degrade silently to the structural result.
3. Create: new pack, schema copied, data rows copied, impact graph
   written, origin recorded, audit entry written; activation is
   automatic.

`create-ontology` additionally records the creation in
`studio_history` , lists the exact source tables in the pack's origin,
and **generates a full report automatically**
( `routes/common.py:_auto_pack_report` ): the response carries the
report `uid` , and the title follows
`<name> | Scenario: <pack> | Data: <sources>` .
The report is linked from Graph studio's Generation history card
( `GET /api/graph-studio/history` ).

### 5.1 Data import and AI proposals ( `importer.py`,`induction.py` )

`POST /api/admin/data/upload` carries the whole import in one endpoint
and one shared pipeline ( `importer.analyse_upload` , used by both the
preview and the commit):

* **Sheet pick** — `importer.list_sheets` enumerates an workbook's tabs
  (via `pandas.ExcelFile`);`_resolve_sheet` maps a requested sheet
  name, a 0-based index, or the default (first sheet) to
`pandas.read_excel(sheet_name=…)` . An unknown sheet raises an
`UploadError` (4xx) that names the available sheets. Non-workbook
  files return no sheets and are read as a single table.
* **Column mapping** — `guess_column_mapping` matches headers to the
  table's non-derived columns by exact key then case-folded substring
  (length ≥ 3). The caller may override per header via the `mapping`

  form field (JSON `header → column key`);`apply_mapping_override`

  re-validates every override and maps `__ignore__` / blank / unknown
  targets to "not imported", so they drop into `unmapped_headers` .
* **Preview vs commit** — `preview_upload` returns the report (valid /
  invalid counts, headers, sheets, `instance_columns` , mapping,
  unmapped headers) without writing. `commit_upload` re-runs the same
  analysis, refuses to write when any row is invalid, and stores the
  valid rows as a new activated dataset ( `admin_service.create_dataset` ).
* **AI proposals + evidence** — a successful commit schedules
`induction.induce_from_table` in the background. It sends the table's
  columns and sample rows to the model, which returns strict JSON
  elements each carrying a `rationale`;`_build_evidence` also stores
  the exact table/columns/sample rows it saw in
`ontology_proposals.evidence` , so the admin review queue can show the
  basis for each proposal.

**Data-type JSON.** `parse_data_file`also accepts`.json` (registered
in `config.ALLOWED_UPLOAD_EXT`).`_read_json_records` decodes the bytes
across common encodings and normalises a top-level list, or a
`{"records" | "data" | "rows": [...]}` wrapper, into rows; a bare
object becomes a single row. A JSON file whose keys look like an
ontology definition ( `entities`/`node_types` ) is rejected with an
`UploadError` pointing at the pack-import path instead.

**Batch import** — `POST /api/admin/data/batch-upload` (form fields
`pack_id`,`commit`,`plan`, one or more`files` ) reuses the same
single-table pipeline for every unit of work. One *item* is a sheet of
a workbook (via `importer.list_items` ) or the whole of a CSV/JSON file.

* **Expansion** — `list_items` splits each uploaded file into items;
  folder uploads keep the browser's relative paths and `_basename` /
`_stem` normalise the names before matching.
* **Routing** — `analyse_batch` resolves each item's target table in
  order of precedence: an explicit `plan`entry (`target` = an existing
  table key or the `__new__`sentinel), else`admin_service.find_table_by_name`

  (exact key, then `ontology_service._key_tokens` set equality, then
  token intersection), else a new table inferred by `_infer_new_table` .
  Items matching no table become `__new__` proposals the admin
  confirms or re-routes in the review UI.
* **New tables** — `admin_service.create_table` inserts the
`pack_tables`row and its`pack_columns`wholesale;`commit_batch`

  first calls `ontology_service.upsert_entity`/`upsert_attribute`

  per column and runs `sync_visual_graph` , so a batch-created table is
  a first-class part of the ontology, not just a data table.
* **Preview vs commit** — `analyse_batch(with_rows=False)` powers the
  preview and reports per item `{file, item, table key, mapping,
  valid/invalid counts, skip}`;`commit_batch` refuses the whole
  request ( `UploadError` , 4xx, nothing written) when any non-skipped
  item is empty or invalid, otherwise writes one activated dataset per
  item and returns `items`plus the`created_tables` it built.

## 6. Reports

`modules/report_builder.py` renders the requested sections into
Markdown, HTML (print-friendly, base64 charts) and PDF. Section
numbering adjusts to what is present. Notable sections:

* `ontology` — pack nodes/links rendered via
`report_charts.render_chain_graph`into`charts/ontology_graph.png` ;
* `analytics`—`analytics.mom_attribution` (headcount / productivity
  / mix effects vs. prior month) and `analytics.agent_stats`

  (per-channel registered/active/avg productivity) tables;
* AI interpretation is appended only when a model is configured.

`POST /api/reports/sample` answers the pack's recommended questions
with the rules engine ( `raise_on_template_error=False` ), then builds a
report with all sections — no AI cost. Its title carries the scenario
and data sources (`Sample Analytics Report | Scenario: <pack> |
Data: <sources>`).`GET /api/reports/{uid}/view` serves the HTML
inline.

`GET /api/dashboard` assembles the user-portal dashboard in one call:
the resolved pack (name, description, origin), its data sources (kind
and row count), KPI cards and the monthly timeseries. **KPI cards are
driven by the pack's own `metric_formulas`** — each formula whose input
table has rows becomes one card, formatted by its `fmt`/`decimals` —
so any ontology shows its own metrics (VNB/APE, IFRS 17 CSM / ISR / EV,
lapse rates, …) instead of a fixed VNB/APE key list. The response also
returns `formula_count` , letting the UI tell "no formulas declared"
apart from "formulas declared but their tables are still empty". When
the pack carries a compatible monthly series, `analytics.company_series`

adds latest-month APE / VNB / margin / MoM cards. On first call per
user + pack it also generates a full
**"Full Analytics Report"** via `_ensure_auto_report` (idempotent,
flagged `auto_report:<user>:<pack>`in`app_settings` ) and returns it
as `auto_report` — the dashboard banner links to
`/api/reports/{uid}/view` .
`GET /api/graph-studio/history` returns the caller's 30
newest `studio_history`rows.`GET /api/admin/user-settings` joins
users with their pack and model selections for the admin support
table.

`GET /api/search/insight?q=` (Ontology workspace instant insight)
returns the search chips, the matched graph nodes ( `node_keys` ), a
1-hop neighbourhood sub-graph ( `subgraph.nodes/links` ) and a generated
analysis card ( `routes/graph.py:_insight_analysis` ) describing the top match —
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

Endpoints: `/api/analytics/timeseries`,`/attribution`,`/scenario` ,
`/agent-stats` . The driver-attribution, scenario and agent views read the
built-in VNB/APE monthly schema ( `analytics.REQUIRED_TABLES` :
`vnb_monthly`,`channel_monthly`,`agent_monthly`,`agent_master` ) and
activate only when the active pack carries those tables.

**Every pack gets a trend.** `/api/analytics/timeseries` also returns a
`generic`block built by`analytics.generic_monthly(data)` : the pack's own
monthly table (the one whose `report_month`/`month`column holds`YYYY-MM`

values — `report_month` preferred, then the table with more rows) with one
measure per numeric column ( `sum`, or`mean` for rate-like names). When the
pack holds a lapse count and an exposure column, an extra **annualised
lapse rate** measure ( `12 × lapses ÷ exposure` ) is prepended and chosen as
the default, matching the dashboard's annualised-lapse-rate KPI. The user
portal renders this as a "Monthly trend from this pack" card with a measure
selector, chart and month table; the fixed-schema driver / scenario / agent
cards stay hidden. For packs with **no** monthly table, `requirements`

(required vs. present vs. missing tables, their key columns and each table's
purpose via `analytics.TABLE_PURPOSE` ) is returned and the UI renders an
explicit "what this view needs" panel — not an error — with three routes:
switch the Scenario selector to a compatible sales pack, upload the four
tables through Data centre, or use the pack's own Dashboard metrics.

## 8. Security, deployment and performance

* **Two-factor authentication.** `modules/auth.py` owns the credential and
  password policy, `modules/otp.py` the challenges, `modules/mailer.py` the
  transport. `POST /api/auth/signup/start` validates the address and the
  password, hashes the password into the challenge payload and mails a code
  — no `users` row exists yet. `/signup/verify` consumes the code and calls
  `auth.register_hashed`, which derives the username from the address
  (`alice@example.com` → `alice`, `-2` on collision) and writes the account
  plus its first `password_history` row in one transaction.
  `POST /api/auth/signin/start` checks the password only and returns
  `otp_required`; `/signin/verify` consumes the code and only then calls
  `auth.start_session`, so no cookie exists between the two steps. Legacy
  accounts without an address get `bind_required` and a 15-minute
  HMAC-signed token; `/api/auth/bind/start|verify` attach and confirm the
  address before the session is issued.
* **Password policy and rotation.** `auth.validate_password_policy` enforces
  length ≥ 10, at least three of four character classes, and
  `auth.password_reused` compares against `password_history` inside a
  24-month window. `auth.password_state` exposes
  `password_days_left`, `password_warning` (≤ 14 days) and
  `password_expired`; `/api/auth/me` returns it, the portal renders a banner
  and `/account/password` performs the rotation. Administrators resetting a
  password go through the same validation.
* **Verification challenges.** `modules/otp.py` stores
  `SHA-256(challenge_id:code)` (never the code), enforces a 10-minute TTL, a
  60-second resend cooldown, five attempts and single use. A resend retires
  the previous challenge first, so an older e-mail stops working. Because
  the instance that sends a code may not be the one that verifies it, each
  challenge is mirrored to `shared/otp/<sha256-of-challenge>.json` in the
  same durable store as accounts; `snapshot.delete_otp` removes the mirror
  on success or supersede. Attempts are committed before the 400 is raised,
  so a retry cannot reset the budget (the request wrapper would otherwise
  roll the increment back).
* **Mail transport.** `modules/mailer.py` prefers `RESEND_API_KEY`, falls
  back to `SMTP_HOST` (+ `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`,
  `SMTP_SECURITY`), and answers 503 when neither is configured — never a
  silent success. `ONTI_MAIL_ECHO=1` prints the code and returns
  `debug_code` for local development only.
* Sessions are HttpOnly cookies whose value is an HMAC-signed JSON
  payload (`auth.issue_session_token`). The payload holds the user id,
  the expiry and a random nonce (so two logins in the same second never
  collide on the `sessions` primary key), plus the identity claims
  `un`/`dn`/`r` (username, display name, role). Legacy
  `user_id|expires|nonce` payloads are still accepted by
  `auth.verify_session_claims`, which returns the claims and backs the
  older `auth.verify_session_token` tuple API. The token is also written
  to `sessions` and, on logout, to `revoked_sessions`, so logout revokes
  it immediately; other serverless instances verify the signature and
  check the revocation table before loading the user row.
* **Cross-instance accounts and sessions.** On ephemeral storage
  (`db.EPHEMERAL_STORAGE`, i.e. the Vercel runtime) each instance copies
  the bundled database into its own `/tmp`, so a row written by
  self-registration exists on one instance only. Two mechanisms keep the
  visitor signed in:
  1. `auth.register_account` writes the per-account record before the
     transaction commits and fails with `503` if it cannot, so a
     successful registration is always durable.
  2. `auth.user_by_token` resolves an ephemeral-host session **by the
     username claim** (`_register_login_row`), adopting the durable
     record when this instance has not seen the account. If neither the
     database nor the store knows the username the session is refused —
     the token is never turned into a synthetic user, so a removed
     account cannot be impersonated. A persistent host keeps the strict
     row check and always rejects a disabled account.
* **Credentials across instances.** `authenticate` adopts a missing row
  from the account store, and when the local PBKDF2 hash rejects the
  password it re-reads the stored record (`_refresh_login_row`): an
  administrator who reset a password or disabled an account on another
  instance is therefore honoured at the next sign-in.
* `/api/auth/status` publishes `ephemeral`, `snapshot_sync`,
  `accounts_persist` (true when the host is persistent *or* a durable
  account store is configured), `mail_configured` and the password policy.
  `POST /api/auth/signup/start` returns the challenge shape
  (`challenge_id`, `email_masked`, `expires_in`, `provider`); on an
  ephemeral host without a store it answers `503` with an actionable
  message instead of creating a phantom account, and the sign-in page hides
  the registration switch. Unknown usernames return the stable
  `Invalid username or password.` message; the per-instance demo hint was
  removed now that accounts are durable.
* `/api/admin/*` requires the admin role; the admin UI is a separate page
  with no cross-links from the user portal.
* API keys (system and user) are encrypted with a machine-local key
  ( `.onti.key` ) and never returned unmasked by any API.
* **Credential normalisation.** A key pasted from e-mail, a spreadsheet
  or `.env`routinely carries wrapper quotes, a`Bearer` prefix or a
  trailing newline. Sent verbatim, the provider answers
`{"code":30014,"message":"Token is invalid."}` even though the key is
  valid. `llm_client.normalize_api_key` therefore strips whitespace,
  quotes (straight, back-tick, typographic), a leading `Bearer` scheme
  and collapses known placeholders ( `sk-...`,`your-api-key` , …) to an
  empty string; `normalize_base_url` trims spaces and trailing slashes.
  Both run at **every boundary** — `create_config`/`update_config` ,
  the per-user equivalents, `resolve_test_config` (including legacy rows
  written before normalisation existed) and `ConfiguredLLM.__init__` ,
  the last gate before the `Authorization` header is built. Dirty values
  are never persisted, and an empty key sends no `Authorization` header
  at all.
* **Provider errors are translated.** `llm_client.describe_provider_error`
  maps the provider's status/body into `error`(human cause),`hint`

  (next step) and `provider_error` (raw body, 300 chars, for support),
  plus `provider_code` . The model form renders all three, so the admin
  never has to read raw provider JSON.
* Formula evaluation runs in a sandboxed expression parser; arbitrary
  Python is rejected at save time.
* All state-changing endpoints require confirmation in the UI and
  write to the `audit` table.

### 8.1 Deployment topology (Vercel vs Docker)

* **Vercel.** Vercel detects the FastAPI `app` in `main.py` and routes
  every path to that single function. `vercel.json` must therefore
  **not** contain a catch-all `rewrites` entry: rewriting to
  `/main.py` replaces the request path with the file name, so every
  route answers `{"detail":"Not Found"}`. The file only sets
  `functions.main.py.maxDuration` and the daily snapshot cron;
  `/static` is promoted to the CDN by the framework integration.
* **Docker.** `ONTI_DATA_DIR=/data` on the `onti-data` volume makes the
  host persistent (`ephemeral: false`), so no snapshot backend is
  needed; set `ONTI_SECRET_KEY` when running several replicas.
* **Configuration switching.** `ONTI_CONFIG` selects another config
  file and `ONTI_ENV=docker` deep-merges `config/config.<env>.json`
  over `config/config.json`; secrets stay in environment variables
  named by the `snapshot` block.

### 8.2 Data-processing performance

CSV/XLSX ingestion stays on pandas: the workbook path relies on the
`openpyxl` engine (`.xlsx` ) and the legacy `.xls` reader, which
polars does not cover without an extra calamine dependency, and polars
would add tens of megabytes to the 500 MB serverless bundle. The hot
paths were instead made vectorised — question-list import uses
`Series.str.strip` plus `to_dict("records")` rather than `iterrows` —
while per-row validation keeps an explicit loop because it must report
the row number and field-level error for every rejected row.

## 9. Testing

```bat
python -m py_compile main.py ^
  (Get-ChildItem routes\*.py, modules\*.py ^
  | ForEach-Object { $_.FullName })   :: syntax gate
python tests\verify_t1.py ... verify_t6.py  :: layer checks (no server)
python tests\verify_model_key.py            :: key paste hygiene + error text
python tests\smoke_http.py                  :: end-to-end HTTP matrix
python tests\verify_t7.py                   :: upload sheets/mapping + evidence
python tests\verify_t8.py                   :: batch import (sheets/CSV/folder/JSON)
python tests\verify_t9.py                   :: lapse pack end to end
python tests\verify_giteam.py               :: regular team account vs admin routes
python tests\verify_permissions.py          :: registration everywhere + ontology
                                            :: creation rights & admin visibility
python tests\verify_login.py                :: 30 checks — sign-in gate + 2FA contract
python tests\verify_auth2fa.py              :: 83 checks — offline, throwaway DB
python tests\verify_model_endpoints.py      :: 65 checks — model CRUD + diagnostics
python tests\verify_ontology_value.py       :: 14 scenarios; writes the value report
```

Tests that talk to the server need the same database the server uses, so run
them against `python main.py`on`127.0.0.1:8010` (not against a container
whose volume is a separate copy). For a container, use the HTTP-only scripts
( `smoke_http.py`,`verify_giteam.py` ) and the checks in
README → Deploying with Docker. `verify_ontology_value.py` needs a live model;
when the gateway is slow it reports `SKIP` instead of failing, because that is
an environment condition, not an application defect.

### Run log

`tests\run_all.py`executes every`tests\*.py`script — except`_` -prefixed
helper modules such as `_seed_synthetic.py` , which tests import rather than
execute — (or a filtered subset) as a subprocess and appends one JSON line per
run to `data/run_log.jsonl`through`modules/run_log.py:record_run` :

| Field                       | Meaning                                 |
| --------------------------- | --------------------------------------- |
| `run_id`                    | unique identifier of the run            |
| `started_at`, `finished_at` | ISO-8601 start / finish                 |
| `duration_ms`               | wall-clock duration of the run          |
| `actor`, `host`             | OS user and machine that ran the script |
| `script`                    | `tests/<name>.py`                       |
| `status`                    | `PASS`/`FAIL`; 900 s timeout = `FAIL`   |
| `summary`                   | last non-empty output line              |
| `details`                   | free-form extras (exit code, filters)   |

The path and the retention cap come from the `runtime` block of
`config/config.json`(`run_log`,`run_log_max_entries` ); older lines are
trimmed when the cap is exceeded. `python -m modules.run_log` prints the
ten most recent entries.

Operational literals (truncation widths, snippet sizes, report/import
limits) are centralised in `modules/constants.py` ; deployment values
(port, session, upload size, run-log path) live in `config/config.json`

and reach the code through `modules/config.py` .

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

### 9.1 Cross-scenario "With vs Without ontology" comparison

Six real scenarios across two ontology packs were exercised end-to-end
via `POST /api/ask/compare` to quantify what the ontology layer adds
over a data-grounded baseline (same uploaded tables, no entity
definitions, no metric formulas, no graph).

**Test matrix** (each question answered twice by the same model — once
grounded in the full ontology, once grounded in raw uploaded tables):

| #   | Pack       | Question                                         |
| --- | ---------- | ------------------------------------------------ |
| 1   | 6 · VNB    | What is the VNB in April?                        |
| 2   | 6 · VNB    | How does April VNB compare to the prior month?   |
| 3   | 6 · VNB    | Why is there such a movement in April VNB?       |
| 4   | 23 · Lapse | What is the lapse experience in September?       |
| 5   | 23 · Lapse | How does September lapse compare to prior month? |
| 6   | 23 · Lapse | Why such a movement in September lapse?          |

**Results (all 6 scenarios completed successfully):**

| Dimension             | With Ontology            | Without Ontology    |
| --------------------- | ------------------------ | ------------------- |
| Answer source         | Rules 6/6, deterministic | LLM 6/6, stochastic |
| Structured tables     | 5/6                      | 6/6                 |
| Impact-chain nodes    | 59 across scenarios      | **0**               |
| Impact-chain edges    | 50 total                 | **0**               |
| Matched concepts      | 4–6 per answer           | —                   |
| Metrics used          | 7–9 per pack             | **0**               |
| Cannot-determine rate | **0/3** why-questions    | 2/3 lapse whys      |

**Key findings.**

1. **Rule engine hit rate = 100%**. All six questions hit a compiled
   rule template. Output is fully deterministic — re-running produces
   byte-identical answers. Without-ontology answers are stochastic LLM
   generations; any two runs will differ in wording.

2. **Impact-chain graphs are ontology-exclusive.** Across 6 scenarios
   the ontology side visualised **59 graph nodes / 50 edges** that
   matched concepts like `TotalVNB`,`AgentProductivity` ,
`ActiveAgentCount`,`WeightedVNBMargin` , etc. The baseline side has
   no graph and no matched concepts — it treats every column literally.

3. **Lapse why-question is the clearest contrast.** With ontology:
   cohort-level breakdown (WP PY1 Agency +3.0 pp, WP PY1 Worksite
   Marketing +2.0 pp…) plus a **financial bridge table** showing CSM,
   Insurance Service Result, and EV impact in $M by framework
   (IFRS17 / EV). Without ontology the model replies
   *"I cannot determine a definitive cause … the uploaded tables
   contain lapse outcomes but do not include any explanatory
   variables"* — correctly identifying that the raw tables lack what
   the ontology's `metric_formulas`,`graph_nodes` and
`graph_edges` provide.

4. **Rule-sort bug fix discovered during testing.** The VNB pack had
`vnb_gap`(`sort=4`) preceding`mom_attribution`(`sort=27` ). The
   regex pattern `why.*vnb`in`vnb_gap` captured*"Why is there such
   a movement in VNB"* before `mom_attribution`'s`movement` pattern
   could run. Fix: raised `mom_attribution`to`sort=1` , dropped
`vnb_gap`to`sort=28` . Post-fix the why-question now returns a
   proper attribution table — *Active-agent effect +$0.045M,
   Productivity +$0.042M, Mix/margin +$0.012M, per-channel breakdown*
   — and 20 graph nodes instead of a 173-char gap paragraph.

5. **Same data, different reasoning.** Both sides ground in the same
   uploaded tables (12 tables for VNB, 4 for Lapse). The difference
   comes from the ontology layer adding: metric formulas (8–9 per
   pack), entity-to-table mappings, graph edges that connect drivers
   to outcomes, and rule templates that compose them into answers.

**Regression suites after all changes:**
`tests/verify_login.py` 30/30 ✅ · `tests/verify_auth2fa.py` 83/83 ✅ ·
`tests/verify_model_endpoints.py`

65/65 ✅. No pre-existing regressions introduced.

### 9.2 Deep-analysis validation on the v4 pack

The 9.1 run measured *grounding counts*, not *reasoning depth*, and it
used a workbook that carried a pre-written `NARRATIVE DRIVER` column —
which the baseline model simply parroted, making the two sides look
equivalent. The v4 workbook ( `BPTablesTest_v4_clean.xlsx` ) drops that
column: six sheets, pure relational data, 3, 552 rows. The pack
( `bp-planning-v4` ) was rebuilt from it and the deterministic reasoning
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
`docs/ontology_value_report.json` .

**What produced the gap.** The ontology side carries, on every answer:
the exact per-segment factor bridge (residual $0.000M), the mix-weighted
margin proven against the channel's implied VNB/APE margin, three
agent-cohort tables from a second join, seven statistical tests (OLS
trend + R², CV, z-score, momentum, HHI, driver correlation,
reconciliation), a numbered reasoning trace naming each method and table,
and an annotated impact chain from the graph. The baseline, given the
same raw tables, could restate the headline figure — correctly, at
2, 907 chars in its longest answer — but never joined a second table,
never decomposed the move and never reconciled anything.

**Why the earlier comparison was not fair.** With the `NARRATIVE DRIVER`

column present, the baseline's answer was essentially a copy of the
human-written explanation, so it read more fluently than the ontology
answer at that time. Removing the column also required no ontology
change: the pack's column keys, relations and formulas are schema-level,
so explanatory text never had to be modelled.
