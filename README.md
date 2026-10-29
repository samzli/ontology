# Onti — Insurance Ontology Workspace

Onti turns your insurance data and methodology into a
**question-answering, graph-grounded analytics platform** — without
writing code. Define an ontology (entities, attributes, relations,
formulas, rules), import CSV / Excel / JSON data (one file at a time,
many files, a whole folder, or every sheet of a workbook at once), and
users get:

- **Deterministic answers** from auditable rules when their question
  matches;
- **AI answers** from any OpenAI-compatible model, grounded strictly in
  the active ontology and data (Graph-RAG style);
- a **dashboard** with KPI cards and the full knowledge graph, plus an
  inline impact chain under every answer;
- one-click **analytics reports in HTML, PDF and Markdown** with
  server-rendered charts.

Onti is **not hard-coded to any metric, product, or model**. It ships
with one built-in **VNB / APE sample pack** so you can try it
immediately; everything you see is data that an administrator can
replace or extend from the admin console — by drawing the graph in
ontology governance, by uploading a raw ontology JSON file (BP2,
relationship-graph or `onti-pack/v1` format), by exporting /
importing a `.onti-pack.json` pack on another machine, or — fastest
of all — by letting users generate an ontology straight from their
uploaded tables in Graph studio
(see [3.1](#31-generate-an-ontology-from-your-data)).

- **Backend:** Python 3.10+ · FastAPI · SQLite (zero external services)
- **Frontend:** server-rendered Jinja2 pages + vanilla HTML/CSS/JS — no
  build step, no CDN
- **Auth:** local accounts, PBKDF2 hashes, signed HttpOnly session cookies,
  role-based portals
- **Default URL:** <http://localhost:8010> — or `http://onti.local:8010`
  after the one-line hosts mapping below

---

## 1. Quick start (Windows)

```bat
:: 1. (optional) create a virtual environment
python -m venv .venv && .venv\Scripts\activate

:: 2a. either double-click
start_server.bat

:: 2b. or run manually
pip install -r requirements.txt
python main.py
```

`start_server.bat` installs missing dependencies, then starts the
server. On first launch the database is created automatically under
`data/onti.db` and the built-in sample pack is seeded.

Open one of the two portals (the root URL `/` forwards to the user
portal):

- **User portal** — <http://localhost:8010/app>
- **Admin console** — <http://localhost:8010/admin>

### A named local domain instead of localhost

Prefer a real name? Map `onti.local` (named after the insurance
ontology workspace) to your machine — one line, administrator rights
required:

1. Open `C:\Windows\System32\drivers\etc\hosts` as administrator and
   append:

   ```text
   127.0.0.1  onti.local
   ```

2. Flush DNS (`ipconfig /flushdns`) and browse to:

   - **User portal** — <http://onti.local:8010/app>
   - **Admin console** — <http://onti.local:8010/admin>

The server already listens on `0.0.0.0`, so colleagues on the same
network can also reach it via your IP: `http://<your-ip>:8010/app`.
For a production domain, put Onti behind any reverse proxy (Caddy,
nginx, IIS) and point the proxy at port 8010 — no code change needed.

A default administrator is pre-provisioned so you can sign in
immediately. The credentials are **not displayed on the web pages** (to
avoid exposing them to anyone who reaches the site); they are only shown
in the local server console and recorded here for the operator:

| Username | Password    |
| -------- | ----------- |
| `admin`  | `Onti@2026` |

> **Change the default password after first login** (Admin console →
> Users & audit → reset password). You can also create additional
> accounts from there or enable self-registration.
>
> **Port already in use?** `start_server.bat` detects this and stops
> with a clear message — close the other Onti window first (or end the
> Python process), then run it again.

Behind a restricted network, install with a mirror:

```bat
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn
```

### AI models are optional

All rules-engine, graph, library, import and PDF/HTML/Markdown features
work **without any model configured**. AI mode, AI enrichment and AI
report interpretation only become available after at least one model is
configured — either by an administrator in **Admin → Models & usage**
(system-wide) or by each user in their own **Model centre**
(user-private, see [Per-user Model centre](#per-user-model-centre)).

On first start, a `.env` with the generic `LLM_API_KEY` /
`LLM_BASE_URL` / `LLM_MODEL_NAME` variables (see `.env.example`; the
legacy `SILICONFLOW_*` names are still accepted as a fallback) is
migrated **once** into an encrypted default model configuration. Any
OpenAI-compatible endpoint works — OpenAI, DeepSeek, Moonshot, Together,
Ollama, vLLM and similar. You can delete or replace the configuration
afterwards in Models & usage.

#### Self-signed gateways (Deloitte Nova, on-prem proxies)

Gateways that present a self-signed TLS certificate — Deloitte Nova
(`https://nova.deloitte.com.cn/v1`), internal enterprise proxies, and
some on-prem deployments — require SSL verification to be disabled.
Every model configuration (admin and per-user) carries an individual
**Verify SSL certificate** toggle that default to "on"; when connecting
to such a gateway, simply **uncheck it**. The flag is stored per-model
in the database, so each gateway can be configured independently.

From `.env`, the same flag can be set with any of:

```bash
NOVA_VERIFY_SSL=false      # canonical Nova-specific name
LLM_VERIFY_SSL=false       # generic
SKIP_SSL_VERIFY=1          # alternative trigger
```

> **Important — base URLs and model ids are gateway-specific.**
> The Base URL must be the provider's full API prefix — the platform
> calls `{base_url}/chat/completions`, so `https://nova.deloitte.com.cn/v1`
> works while the bare host `https://nova.deloitte.com.cn` returns *405
> Not Allowed* (Nova only serves routes under `/v1`). Trailing slashes
> are normalised away, but `/v1` is never auto-appended (Azure and
> on-prem gateways use other prefixes), so copy the URL verbatim from
> the provider's docs. Model ids differ too: DeepSeek V4 Flash goes by
> `deepseek-ai/DeepSeek-V4-Flash` on SiliconFlow but
> `DeepSeek-V4-Flash` (case-sensitive) on the Nova gateway. Always test
> a newly added model with the **Test** button before setting it as
> default — it validates the exact URL + model id + key combination.
> The full pitfall checklist and a symptom → cause → fix table live in
> [docs/ADMIN_GUIDE.md](docs/ADMIN_GUIDE.md) §5 ("Model setup
> checklist"). A test always answers with a verdict — `ok: true`, or
> `ok: false` plus the cause and the fix — never with a 4xx; the
> translation of SSL, DNS, timeout and dropped-socket failures is
> specified in [docs/TECHNICAL.md](docs/TECHNICAL.md) §4.1.

---

## 2. Portals — who does what

Onti serves **two independent web entries**, each with its own URL and
sign-in page. The split is deliberately simple:

- **Users use and build.** In the user portal they ask questions,
  upload data, turn their data into ontologies, and produce reports.
- **Admins govern.** In the admin console they manage accounts and
  access, oversee everyone's knowledge bases, configure the available
  models, and monitor usage. They do not answer questions there.

There is deliberately no navigation link between the two consoles — the
two roles have separate URLs so the boundary stays obvious:

| Portal          | URL                           | Who               |
| --------------- | ----------------------------- | ----------------- |
| **User portal** | <http://localhost:8010/app>   | all accounts      |
| **Admin**       | <http://localhost:8010/admin> | `admin` role only |

The two sign-in pages are visually distinct (light user page vs. dark
navy admin page). Visiting `/admin` while signed out redirects to
`/admin/login`; a regular (non-admin) account is redirected back to
`/app?forbidden=1`. The default administrator (`admin` / `Onti@2026`)
can enter either portal; self-registered users only see the user
portal.

### User portal — `/app` (use & build)

- **Dashboard** (default) — a **scenario banner** naming the active
  scenario pack, its description and every underlying data source
  (kind + row count), real **KPI cards** driven by that pack's own
  metric formulas — a VNB/APE pack shows VNB and margin, a lapse-study
  pack shows its lapse rates and IFRS 17 CSM / ISR / EV closing values
  — each card carries a **ⓘ What does this mean?** twisty with its
  formula and source table — the full knowledge graph rendered in-page, and a
  quick question box. A layer switcher toggles between **Impact
  graph** (default, coloured by edge type) and **Class
  relationships** (entities → relations with cardinality labels).
  A **Full analytics report** for the scenario (multi-section,
  chart-rich) is generated automatically the first time the dashboard
  is opened and one click away from the banner.
- **Scenario switcher** (header) — a dropdown of every pack the
  account can access. Switching updates the dashboard, graphs,
  analytics, Ask and Graph studio in place; the choice is remembered
  per user and **System default** returns to the admin-activated
  pack.
- **Performance analytics** — adapts to the active pack. When the pack
  declares its own monthly table (a `report_month` / `month` column of
  `YYYY-MM` values) the page draws a **Monthly trend from this pack** card
  automatically — pick any numeric column from the dropdown; with a lapse
  count and an exposure column the default is the annualised lapse rate
  (`12 × lapses ÷ exposure`), matching the dashboard KPI. The full
  VNB/APE views — monthly trend chart, MoM driver attribution, target
  back-solve scenario and agent-salesforce health — activate when the
  pack carries the built-in monthly sales sheets (`vnb_monthly`,
  `channel_monthly`, `agent_monthly`, `agent_master`); a pack with no
  monthly table gets a message listing which of those tables are
  required, present and missing.
- **Ask the ontology** — Auto / Rules engine / AI mode switch. Answers
  stream over SSE with a source badge (green rules, amber no-match,
  blue AI). Every answer is followed by an inline **Impact chain**
  graph card. A "🤖 Model: … · change" button shows the active model
  and jumps to the dedicated **Model centre** view where every user
  picks any enabled model for their own answers (remembered per user)
  and registers personal models.
  The AI grounding block is **profile-first and never truncated**: every
  table is summarised over its full active dataset (column profiles,
  complete month coverage, per-month aggregates, a 3-row sample), so
  questions like *"what months are in the data?"* or *"compare Sep to
  Aug"* answer from all months, not the first 100 rows — and the smaller
  prompt makes AI answers noticeably faster.
  Suggestion chips come in four groups: **✨ Recommended for you**
  (generated from the ontology), **★ My questions** (your own saved
  list; remove any chip with its × button), **🕘 Recent asks** (your
  last questions against this ontology — click to re-ask) and the
  administrator-curated **Standard questions**. Use **★ Save question**
  to keep the typed question, or **📋 Batch** to paste a whole list —
  each line is answered in sequence and later questions use earlier
  answers as memory.
- **With / without ontology comparison** — the yellow
  **◐ Compare: with / without ontology** toggle answers each question
  twice in one card: the ontology-grounded answer with its impact chain
  on one side, and the same model given **only the raw data tables** —
  no entity definitions, no metric formulas, no joins, no chain — on the
  other. The card shows the two answers exactly as produced; the
  quantitative gap is returned by the API (`value`) and measured in
  [docs/ONTOLOGY_VALUE_REPORT.md](docs/ONTOLOGY_VALUE_REPORT.md). Built
  for client demos; costs two model calls per question.
- **Global ontology ON / OFF switch** — a header toggle in **both the user portal and the admin console**.
  OFF is a deliberate **text-only demo mode**: KPI cards, knowledge graphs, performance charts
  and impact chains are hidden behind an explicit "Ontology is OFF" panel, Ask answers from
  the uploaded data tables but with no ontology layer, and reports still generate but as text-only. ON is the default and restores
  every view instantly. The toggle is per-browser in localStorage; it does not change any server-side data.
- **Data centre** — browse the underlying data tables (each shows its
  source file and upload time) and read their rows; tables with more
  than a dozen rows offer **View full dataset**, a paged browser
  (50 rows per page) over the complete active dataset. To preview your
  own `.csv` / `.xlsx` against the ontology without storing anything,
  use **Graph studio → 📤 Graph from file**.
- **Ontology workspace** — build / manage the active ontology
  (entities, attributes, graph nodes/edges, formulas, rules) and a
  full-text ontology search box. Every search returns an
  **Instant insight** card: what the top match is, which tables back
  it, the affected graph neighbourhood (matched nodes + one hop)
  rendered inline, and a written analysis.
- **Model centre** (sidebar) — a dedicated page to switch which AI
  model answers your questions (system models plus your own) and to
  register personal OpenAI-compatible endpoints.
- **Graph studio** — two things in one place: (1) **✨ Build an
  ontology from your data** — pick knowledge-base tables, preview the
  inferred entities/attributes/relations (optionally **AI-enriched**:
  the model writes entity descriptions and suggests starter questions),
  then create and activate the new ontology with one click (its data is
  copied, so you can ask it questions and report on it immediately — a
  full analytics report is generated automatically); (2) explore
  read-only graphs of your sources — **data lineage** (default), schema,
  column mapping, or an uploaded CSV/XLSX/JSON file.
  Every table chip shows its source file and upload time; every node
  carries a provenance record. A **Generation history** card lists
  everything produced here — ontology creations, previews, data and
  ontology-JSON uploads, and the auto-generated reports — newest
  first, each labelled by type, with one-click **Switch to it** for
  past ontologies and **View** links for reports.
- **History** — your current session's questions and answers, with
  one-click Re-ask. Ask history is also kept across sessions and used
  as conversational memory for AI follow-ups.
- **Knowledge library** — read-only browsing of entities, attributes,
  relations, formulas and imported data tables.
- **My reports** — generate a report (sections now include the
  **ontology knowledge graph** and **performance analytics**), open it
  in the browser with **🌐 View**, download HTML / PDF / Markdown, or
  delete. **⚡ Sample report** builds a full demo report from the
  recommended questions in one click. Reports generated from the
  current scenario are named `<title> | Scenario: <pack> | Data:
  <sources>` so every number can be traced to its data; creating an
  ontology in Graph studio also generates a report automatically.

### Admin portal — `/admin` (governance)

1. **Users & audit** — create users, promote/demote, enable/disable,
   reset passwords, toggle self-registration, review the audit feed.
2. **System dashboard** — pack/user/model/report statistics, recent
   activity, security signals, database usage, **total AI tokens in
   the last 30 days**, and token-consumption tables **by model** and
   **by user**.
3. **Ontology governance** — pack management (New ontology wizard with
   a getting-started checklist / duplicate / export / import / reset /
   activate / delete), a **🧾 origin badge** stating exactly where each
   pack came from (which user created it in Graph studio and from which
   tables, or which admin imported it), a **Health check** (one-click
   structural integrity report), and seven CRUD tabs: entities,
   attributes, relations, graph nodes, edges, metric formulas, question
   rules. Supports deep links: `/admin?view=onto`, `/admin?view=data`,
   etc.
4. **Data oversight** — see and supplement the data imported into any
   ontology table: upload `.csv` / `.xlsx` / `.json`, pick the workbook
   sheet, review and correct the automatic column mapping, per-row
   validation, preview with zero writes, then commit as a versioned
   dataset. **Batch import** accepts many files, a whole folder, or every
   sheet of a workbook at once — each item kept separate and routed to
   its table, with unmatched items creating new tables automatically.
5. **Canonical questions** — the organisation-wide question list
   (categories, search, add / delete, bulk import from TXT / CSV /
   XLSX). It powers the **Standard questions** chips in the user
   portal; users additionally keep their own private questions.
6. **Knowledge graph** — three layers of the same ontology:
   **Schema** (entities & relations), **Impact** (the curated graph
   users see), and **Instance** (actual data rows, with search and
   entity filters). If the schema and impact graphs ever differ,
   **Sync impact graph from ontology** rebuilds the impact graph from
   the entities and relations in one click.
7. **Models & usage** — configure the OpenAI-compatible endpoints
   users may choose from, review the **User-created models** table
   (every model a user registered in their Model centre — owner, name,
   model, base URL, masked key) and the **User portal settings** table
   (which scenario pack and model each user currently works with,
   marked default or user-selected) so support can reproduce any
   setup. Keys are encrypted at rest and shown masked. Every AI answer
   records token usage for the dashboard above.
8. **AI proposals** — review queue for AI-generated **ontology
   additions** (entities, nodes/edges, formulas). Each card states in
   plain language what approving it will do; approving only adds the
   element, never edits existing content and never touches code.
9. **All reports** — all users' reports with the three download
   formats and delete.

All dangerous actions require confirmation; all write operations are
recorded in the audit log.

---

## 2.1. Knowledge graph — visualisation reference

The dashboard, the inline impact chain, the admin Knowledge graph view
and the **Open large view** modal all share the same SVG renderer
(`static/js/graph.js`). A pack can either declare the visual graph
explicitly (`graph_nodes` / `graph_edges` tables) **or** leave them
empty — the engine will then **auto-generate** the graph from the
ontology's entities and relations (`ontology_service._auto_generate_graph`):

- Layer → group mapping: layer 1/2 → `financial`, 3 → `operational`,
  4 → `mix`, 5 → `cross`.
- Cardinality → edge-type mapping: `ONE_TO_ONE` / `ONE_TO_MANY` /
  `MANY_TO_ONE` → `STRUCTURAL`; `MANY_TO_MANY` → `CROSS_LINK`.

### Edge-type styling

Every edge is rendered with a colour + dash + width from a fixed
palette; the legend shows a line sample for each type present:

| Edge type     | Colour    | Dash     | Meaning                               |
| ------------- | --------- | -------- | ------------------------------------- |
| `STRUCTURAL`  | `#5B8DEF` | solid    | Formula-derived identity relationship |
| `OPERATIONAL` | `#0E7C5F` | `6,3`    | Driver to component relationship      |
| `MIX`         | `#B45309` | `3,3`    | Weighting to blended margin           |
| `CROSS_LINK`  | `#7C3AED` | `10,4,2` | Cross-cutting multi-node dependency   |
| `RELATION`    | `#2563EB` | solid    | Entity relationship (class layer)     |

### Interactions

- **Hover a node** → connected edges and neighbour nodes stay
  highlighted, all others dim. Tooltips show label, value, description.
- **Hover an edge** → it turns red; tooltip shows label, type,
  description.
- **Edge labels** render with a white outline so they stay readable
  where they cross lines and nodes.
- **Click a node / edge** → fires `onNodeClick` / `onEdgeClick`.
- **Edge-type legend** is appended below the group legend when more
  than one edge type is present.

### Two graph layers (toggle on the Dashboard)

- **Impact graph** (`GET /api/graph`) — `graph_nodes` / `graph_edges`
  (or auto-generated). The operational view used by Q&A, reports and
  analytics.
- **Class relationships** (`GET /api/class-graph`) — `entities` +
  `relations` only. Useful for reviewing the schema itself.

The class-relationship layer is lazy-loaded on first switch and cached
for the session.

### Three graph layers in Admin → Knowledge graph

The admin Knowledge graph view presents the same ontology three ways:

- **Schema layer** — entities and the relations between them. This is
  the structural truth; other layers are derived from it.
- **Impact graph** — the curated `graph_nodes` / `graph_edges` view
  that every user sees on their dashboard.
- **Instance layer** — actual data rows (searchable, node-capped).

Because the impact graph can historically diverge from the schema
(e.g. after hand edits), the view carries a **Sync impact graph from
ontology** action (`POST /api/admin/ontology/sync-graph`) that
rebuilds `graph_nodes` / `graph_edges` from the current entities and
relations. Ontologies generated from data write both layers at once,
so they start in sync.

### Graph intelligence tools (Open large view toolbar)

The **Open large view** modal carries a toolbar with analysis tools
backed by server-side graph algorithms:

| Tool        | Endpoint                | What it does                      |
| ----------- | ----------------------- | --------------------------------- |
| Path finder | `GET /api/graph/path`   | BFS shortest directed path.       |
| Impact      | `GET /api/graph/impact` | N-hop up/downstream reachability. |
| Cycle check | `GET /api/graph/cycles` | DFS cycle detection.              |

Results render as a level-by-level chip list while the rest of the
graph dims. **Reset** restores the full view.

### Ontology health check

A one-click structural integrity report (`GET /api/admin/ontology/health`)
with a colour score — **green** (healthy), **amber** (needs attention)
or **red** (critical errors) — across nine checks:

- Entities have attributes (error/warn) — entities with no attributes.
- Entities connected (warn) — entities never used in any relation.
- Aliases resolve uniquely (warn) — aliases mapping to multiple nodes.
- No isolated nodes (warn) — graph nodes with no edges.
- Single connected web (warn) — more than one connected component.
- Formulas compile (error) — expressions failing the sandbox parser.
- Rules answerable (warn) — rules without patterns or answer templates.
- Relations labelled (warn) — relations falling back to key as label.
- Attributes declare units (warn) — numeric attributes without a unit.

Each failed check lists the exact offending keys as clickable chips.

---

## 3. Building your own ontology (no code)

### 3.1 Generate an ontology from your data

The fastest path for a user who already has tables:

1. **Upload data** in the user portal's **Data centre** (CSV / XLSX).
2. Open **Graph studio → ✨ Build an ontology from your data**.
3. Tick the tables to include and give the new ontology a name.
4. Press **🔍 Preview inferred ontology**. Nothing is written yet:
   each table becomes an **entity**, each column an **attribute**
   (number / boolean / date / string detected from real values), and
   `*_id` columns that match another table become ONE_TO_MANY
   **relations**. The preview lists every entity and relation and
   draws the inferred schema graph. Tick **AI enrichment** first and
   the configured model also writes a one-line description for each
   entity and proposes up to eight starter questions (shown in the
   preview; saved into the new pack's question bank on create).
5. Press **✓ Create & activate ontology**. The platform creates a new
   pack, copies the picked tables' columns **and their current data
   rows** into it, writes a matching impact graph, activates it, and
   **generates a full analytics report automatically** (titled with
   the scenario and the exact source tables). You land back on Ask and
   can question the ontology immediately; it also powers analytics,
   the dashboard graph and reports. The creation, the report and any
   previews or uploads are recorded in Graph studio's **Generation
   history**.

Inference rules live in
`ontology_service.infer_ontology`; endpoints:

- `POST /api/graph-studio/infer` — dry-run preview (counts, entities,
  relations, schema graph, optional `ai_questions`);
- `POST /api/graph-studio/create-ontology` — create, copy data and
  activate in one transaction (rolled back if validation fails). Both
  accept `ai_enrich: true`; created packs record their origin
  ("Generated from data in Graph studio (tables: …) by \<user>") which
  is displayed as the 🧾 badge in the admin console.

### Quickstart — create a brand-new ontology in ~10 minutes

1. **Admin console → Ontology governance → New ontology.** A two-step
   wizard: Step 1 — name, slug, description. Step 2 — start **blank**
   or **copy an existing pack**.
2. A **getting-started checklist** appears; every step is a clickable
   jump: entities → relations → data → graph → health → activate.
3. **Add entities** (`Ontology governance → Entities → Add`): one row
   per business concept. Layer 1–2 = financial, 3 = operational,
   4 = mix, 5 = cross.
4. **Add attributes** for each entity — typed, with units.
5. **Add relations** between entities with cardinality — the class
   graph and the auto-generated impact graph are built from these.
6. **Upload data** (`Data oversight` in admin, or **Data centre** in
   the user portal): drop a `.csv` / `.xlsx` / `.json`, pick the
   **workbook sheet** when the file has several tabs, review the
   **column mapping** (unmatched headers are shown as `unmapped` and
   ignored — correct any wrong target yourself), preview, then commit
   as a versioned dataset. **Batch import** in the same card brings in
   many files, a whole folder, or every sheet of a workbook at once —
   each file/sheet becomes its own row, matched by name to a table or
   creating a new one, so a mixed folder never has to be split by hand.
7. **Run the health check** — fix any red/amber items it lists.
8. **Activate the pack** (select it in the pack bar → Activate). Users
   see the new ontology immediately, no restart.

> Prefer a file? **Import pack** accepts a raw ontology JSON (BP2,
> relationship-graph or `onti-pack/v1`) — see
> [Importing an ontology JSON file](#importing-an-ontology-json-file).

### The seven building blocks

1. Define **entities** (business concepts), their **attributes**
   (typed, with units), and **relations** between entities.
2. Add **graph nodes / edges** to declare the causal chain shown under
   answers; edges may use multi-source keys. (Leave empty and the graph
   auto-generates from entities + relations.)
3. Add **metric formulas** using the safe expression language:
   `rows()`, `where()`, `lookup()`, `cell()`, `sum()`, `avg()`,
   `min()`, `max()`, `count()`, `wavg()`, `round()`, `abs()`,
   `money_k()`. Arbitrary Python, imports and attribute access are
   rejected at save time.
4. Add **question rules**: regex patterns, AND groups, guards, a
   Markdown answer template (Jinja sandbox), and evidence nodes.
5. **Canonical questions**: paste a list of questions
   (TXT/CSV/XLSX) — duplicates are skipped and unmatched user
   questions get clickable Top-3 suggestions. Users can additionally
   keep their own private questions from the Ask view.
6. Activate the pack — every user immediately asks against the new
   ontology. No restart needed.

Packs are portable: **Export JSON** produces a single self-contained
file; **Import pack** restores it on another machine and validates all
computed metrics before activation.

### Importing an ontology JSON file

The import dialog accepts **any JSON file** in one of three shapes,
auto-detected on the server (`admin_service._normalize_pack_payload`):

- **`onti-pack/v1`** — detected by `format: "onti-pack/v1"`. Used
  as-is; this is the shape produced by **Export JSON**.
- **BP2 raw ontology** — detected by `entities` whose first item
  carries `id` but no `key`. Converted on the fly by
  `_convert_bp2_to_v1`. Field mapping: entity `id`→`key`; attribute
  `id`→`key`, `owned_class`→`entity_key`, `unique`→`is_unique`;
  relation `id`→`key`, `source_class`→`source_key`,
  `target_class`→`target_key`, `source_class_attribute`→`source_attr`,
  `target_class_attribute`→`target_attr`. Other blocks
  (`graph_nodes`, `graph_edges`, `metric_formulas`, `question_rules`,
  `tables`) pass through untouched.
- **Relationship-graph ontology** — detected by `entities` given as
  plain name strings and/or a `relationships` list with
  `from`/`to`/`type`. Converted by
  `_convert_relationship_graph_to_v1`: entity names are slugified into
  keys; `relationships` become relations with the cardinality mapped
  (`many_to_one` → `MANY_TO_ONE`, etc.) and join keys / time rules /
  business rules preserved in the relation description;
  `deferred_entities` are imported with a "deferred" note; the
  `tables` dict (sheets + logical-field-to-physical-column mapping)
  becomes pack tables with columns; `definitions` and
  `open_decisions` are merged into the pack narrative. An unknown
  `format` marker does not block the import as long as the file
  contains recognisable ontology content.

The built-in sample schema `data/ontology_BP2.json` is one example of a
BP2 raw file — drag it into the import dialog to provision a minimal
5-entity / 30-attribute / 5-relation pack. `data/lapse_demo.json`
shows the relationship-graph shape (6 active + 2 deferred entities,
5 relationships, 4 source tables). You can also drop in any other
raw or `onti-pack/v1` JSON file; the dashboard graph is then
auto-generated from the entities and relations
(see [2.1](#21-knowledge-graph--visualisation-reference)).

> Hand-writing or generating your own ontology JSON? The
> field-by-field requirements for all three shapes, with minimal
> examples, live in
> [docs/ONTOLOGY_JSON_REFERENCE.md](docs/ONTOLOGY_JSON_REFERENCE.md).

### Import preview — look before you commit

Selecting a file in the import dialog **immediately runs a dry-run**
(`POST /api/admin/packs/import/preview`) before anything is written:

- shows the pack name / slug and a chip row of counts (entities,
  attributes, relations, graph nodes/edges, formulas, rules, tables,
  questions);
- **blocking issues** (duplicate slug, missing required fields,
  attributes referencing unknown entities, edges referencing unknown
  nodes) render in red and the Import button stays disabled;
- advisory warnings (e.g. "no data tables") render in amber.

The preview reuses the same validation as the real import, so a green
preview never fails at commit time. When validation does fail, the
error names the exact item and field (e.g. `attribute 'cov_amount':
missing 'owned_class' (owning entity) field`), and the API always
returns a 4xx status with that message — malformed files never
produce an opaque 500.

### Concurrent editing protection (optimistic locking)

Every pack carries a `version` number, shown as a badge in the admin
pack bar. Each save, delete, graph sync and import bumps it. When two
administrators edit the same pack, the second save is rejected with
HTTP 409 and the UI shows "This pack was modified by another admin.
Please refresh the page." — the first writer's changes are never
silently overwritten. Refreshing reloads the new version and the edit
can be re-applied.

### Version snapshots & one-click rollback

**Ontology governance → select a pack → History** opens the version
panel:

- **Save snapshot now** stores a complete `onti-pack/v1` snapshot —
  entities, graph, formulas, rules, question bank and data rows; the 20
  most recent per pack are kept.
- **Restore** replaces the pack with the snapshot. Before overwriting,
  the current state is auto-saved as `before-restore` on the new pack
  record, so a rollback is itself reversible.

### Help centre and documentation

Both portals mount a floating **?** button. The panel opens an
English quick-start with two tabs: **How to use** and **FAQ**. The
same content in long form lives in:

- [docs/USER_GUIDE.md](docs/USER_GUIDE.md) — end-user guide (ask,
  upload, Graph studio, Model centre, reports);
- [docs/ADMIN_GUIDE.md](docs/ADMIN_GUIDE.md) — administrator guide
  (governance, oversight, support workflows);
- [docs/ONTOLOGY_JSON_REFERENCE.md](docs/ONTOLOGY_JSON_REFERENCE.md) —
  ontology JSON file format: the three accepted shapes, required vs
  optional fields, minimal examples and validator error messages;
- [docs/TECHNICAL.md](docs/TECHNICAL.md) — architecture, data model
  and HTTP API reference;
- [docs/ONTOLOGY_VALUE_REPORT.md](docs/ONTOLOGY_VALUE_REPORT.md) —
  measured with-vs-without-ontology comparison (14 scenarios, scored).

### Graph studio — user-built graphs with provenance

The **Graph studio** view (`/app` → 🕸️) has two jobs. The top card,
**✨ Build an ontology from your data**, performs the full
data → ontology → activate loop described in
[3.1](#31-generate-an-ontology-from-your-data). Below it, **Explore
graphs of your sources** lets any user build compact read-only
knowledge graphs on demand in **three analysis views**:

1. **Data lineage** (default) — entities plus the data tables, with
   one edge per table showing which entity it feeds. The owner is
   taken from the table's explicit entity mapping, or inferred from
   the entity whose attributes match the most columns (edge labelled
   "feeds (inferred)"). This is the opening view because it answers
   the first question users have: *which of my tables feeds which
   concept?*
2. **Schema** — entities and their relations only. With the built-in
   pack this is 10 entities and 13 relations (the five monthly
   data-table entities included).
3. **Column mapping** — entities, tables and one node per **unique
   column concept**. A column shared by several tables (e.g.
   `channel_id` present in seven tables, or `prod` / `productivity`
   variants) is aggregated into a single node whose value shows
   "N table(s)"; this removes the duplicate nodes that made earlier
   graphs unreadable. Tick "Include columns with no ontology
   attribute match" to also see unmatched columns.

Every table chip in the picker names its **source dataset** — the
uploaded file, its kind and upload time — so the graph and the data
can never be confused ("where does this table come from?"). For the
lineage and column-mapping views you can narrow the graph to specific
tables with the checkboxes (Select all / Clear). A compact layout
kicks in automatically for larger graphs.

**Graph from file** — upload a `.csv` / `.xlsx` data file or an
ontology `.json` (BP2 / `onti-pack/v1`). Columns are matched against
the active ontology's attributes, so the graph shows how the new
content connects to existing concepts. Nothing is written to the
database — it is an in-memory preview.

**Provenance everywhere** — every node carries a `source` record
(kind + detail). Clicking a node shows exactly which entity, table,
column (with all source tables and raw column names) or uploaded file
it came from, and a **Sources used** panel lists every origin behind
the current graph.

Endpoints: `GET /api/graph-studio/sources`,
`POST /api/graph-studio/build` (`view` ∈ `schema` | `lineage` |
`columns`, `table_ids`, `include_unmatched`),
`POST /api/graph-studio/upload`, plus the ontology-generation pair
`POST /api/graph-studio/infer` and
`POST /api/graph-studio/create-ontology`.

### Personal question lists and smart recommendations

Every user owns a private question list, separate from the
administrator-curated canonical questions:

- **★ Save question** in the Ask view stores the current question; it
  reappears as a **★ My questions** chip (click × to remove).
- **✨ Recommended for you** chips are derived from the active
  ontology — metric formulas, numeric attributes and entities — and
  de-duplicated against the canonical list. They refresh automatically
  when the active pack changes.
- Endpoints: `GET/POST /api/my-questions`,
  `DELETE /api/my-questions/{id}`,
  `GET /api/recommended-questions`. Lists are scoped to the active
  pack and the signed-in user.

### Ask history, batch questions and long-term memory

Every question answered against the active pack is recorded in the
`ask_history` table (question, answer excerpt, source, latency,
tokens) — visible to the user as the **🕘 Recent asks** chips and the
History view, and usable as **long-term memory** by the AI:

- **Short-term memory** — each AI request carries the current session's
  last turns plus the four most recent stored answers, so follow-ups
  like *"and last month?"* resolve correctly. The rules engine uses the
  same memory: when a follow-up names no month (e.g. *"How does it
  compare to the previous month?"*, *"Why is there such a movement?"*),
  it inherits the month from the most recent question that named one
  and answers with the month-on-month comparison, biggest cohort movers
  and the financial bridge for that month.
- **Batch questions** — **📋 Batch** in the Ask view accepts a list
  (one question per line, max 20) and answers them in sequence; later
  questions see earlier answers, and everything lands in history. When
  finished the questions can be saved to the personal list in one
  click.
- **Sample report** — **⚡ Sample report** in My reports answers the
  ontology's recommended questions with the deterministic rules engine
  (no AI wait, no token cost) and renders the full report — summary,
  Q&A, evidence, KPIs, charts, ontology graph, analytics, appendix —
  in one click. `POST /api/reports/sample`.
- Endpoint: `GET /api/ask/history` — the caller's 30 most recent asks
  for the active pack plus their saved questions.

### Per-user Model centre

Users can pick and even register models themselves — no `.env` editing
required. Everything lives in the dedicated **Model centre** view
(sidebar); the Ask view shows a "🤖 Model: … · change" button that
jumps there:

- The **System models** list shows every enabled system model
  (prefix `sys:`); the **My models** list shows the user's own
  registrations (prefix `my:`). The choice is stored per user and
  only affects that user's answers; **Default** falls back to the
  system default model.
- **＋ Add my model** registers any OpenAI-compatible endpoint
  (display name, base URL, API key, model name, temperature,
  max tokens, timeout), with one-click **Test**, edit and delete.
  Entries are private to the account and reappear the next time the
  user signs in.
- Ticking **Also register in the server .env file** appends a
  self-contained `# >>> onti model: <slug> <<<` block with
  `ONTI_MODEL_<SLUG>_*` variables to the project `.env` (existing blocks
  with the same slug are replaced) — handy for deployments that rebuild
  the database. The database stays the source of truth; the `.env` block
  is documentation, not configuration.
- Oversight: administrators see every user-created model (keys masked)
  in **Admin → Models & usage → User-created models**.
- Endpoints: `GET /api/models`, `POST /api/models/select`,
  `GET/POST /api/my/models`, `PUT/DELETE /api/my/models/{id}`,
  `POST /api/my/models/test` (accepts `my:N` to test a saved entry).

### Formula impact analysis

When an admin opens a formula for editing, the form loads an
**impact panel** (`GET /api/formulas/{key}/impact`) listing everything
that depends on the formula — transitively:

- downstream formulas that call it;
- graph nodes whose `value` references it;
- question rules whose template mentions it.

Green = nothing depends on it; amber = lists the affected items.

### Ontology search

The user portal has a **Search the ontology** box on the Ontology
workspace page (`GET /api/search?q=`). It does a case-insensitive
substring match across entity/attribute/relation labels, keys and
descriptions, plus formula expressions and rule templates, ranked by
field weight. Results render as kind-labelled chips.

---

## 4. Reports

Click **Generate report from session** (user portal) after asking
questions, or use the admin Reports area. You choose the title, scope
and sections:

- executive summary, Q&A record (mode, latency, evidence), ontology
  evidence & impact chain;
- KPI tables, chart analysis, appendix & methodology, optional AI
  business interpretation (skipped when no model is configured);
- **Ontology knowledge graph** — the pack's class/impact graph rendered
  into the report, so readers see the structure behind the numbers;
- **Performance analytics** — the month-on-month VNB driver
  decomposition (headcount / productivity / product-mix effects) and
  the per-channel salesforce table, identical to the analytics view.

Reports generated from a scenario carry a self-describing title:
`<title> | Scenario: <pack name> | Data: <data sources>` — the same
scenario banner shown on the dashboard — so a downloaded file always
states which ontology and which data it is based on. Creating an
ontology in Graph studio generates such a report automatically (no
extra click); it appears in **My reports** and in Graph studio's
Generation history.

Outputs are written to `data/reports/<report-id>/`:

| Format       | Characteristics                                        |
| ------------ | ------------------------------------------------------ |
| **HTML**     | Self-contained file, charts as base64, print-friendly. |
| **PDF**      | A4 layout, cover, KPI cards, repeating headers.        |
| **Markdown** | Clean Markdown with linked chart references.           |

Each report card offers **🌐 View** (opens the HTML inline), the three
downloads and delete. Empty sections are omitted and numbering adjusts
to the content.

---

## 5. Models & usage — use any model

Every model configuration is a standard OpenAI-compatible Chat
Completions endpoint:

```HTTP
POST {base_url}/chat/completions
Authorization: Bearer {api_key}
{"model": "...", "messages": [...], "temperature": ..., "max_tokens": ...}
```

Examples: OpenAI, DeepSeek, Moonshot, Together, SiliconFlow, and local
servers such as Ollama, vLLM, LM Studio. The client reads the current
default configuration for each ask, so switching the default takes
effect immediately.

### Token metering

Every AI answer is logged to the `llm_calls` table with caller, model,
success flag, latency and token count. Streamed requests send
`stream_options.include_usage` so the provider reports prompt +
completion tokens; providers that omit usage fall back to a word-count
estimate. The admin **System dashboard** summarises the last 30 days
(total tokens/calls, average latency, failures) with breakdowns
**by model** and **by user**.

### AI ontology proposals (propose → review → apply)

When data changes, the platform can extend the ontology itself — always
with a human approval gate:

1. **Trigger** — every successful data-commit runs an induction pass in
   the background, or start one manually with **Analyze with AI** in
   Data oversight. No model configured or the switch off → the step is
   silently skipped.
2. **Draft** — `modules/induction.py` sends the model the table's
   columns, sample rows and existing ontology keys. The model returns
   strict JSON proposing additive elements only, each with a short
   `rationale` stating the column(s)/value(s) that justify it.
3. **Review** — proposals land as `pending` rows in **Admin → AI
   proposals**, each showing a **Proposing against** panel — the exact
   table, the columns and the sample rows the model saw (stored in
   `ontology_proposals.evidence`) — so you know what a proposal is based
   on before you approve or reject it. Approve, reject, or **Approve
   all** (processed in dependency order).
4. **Apply** — approving runs the same validated `upsert_*` path as
   the manual builder, so an invalid proposal is rejected with a
   reason.

Related admin endpoints: `GET /api/admin/proposals`,
`POST /api/admin/proposals/{id}/approve|reject`,
`POST /api/admin/packs/{pack_id}/proposals/approve-all`,
`POST /api/admin/data/tables/{table_id}/analyze`.

---

## 6. Configuration

`config/config.json` holds only deployment-level settings:

```json
{
  "system_name": "Onti",
  "version": "2.1.0",
  "description": "Onti — Insurance Ontology Workspace: a data-driven insurance ontology agent platform with rule-based and AI Q&A, knowledge graph studio, data lineage, analytics reporting and per-user model management.",
  "server": { "host": "0.0.0.0", "port": 8010 },
  "security": {
    "session_days": 7,
    "allow_registration": true,
    "upload_max_mb": 10,
    "allowed_upload_ext": [".csv", ".xlsx", ".xls", ".json", ".txt"]
  }
}
```

Models, answer modes, packs and users are managed in the application /
database, not in files.

### Data & backups

Locally, everything stateful lives in the `data/` folder. On a serverless
host there is no writable `data/`; the app then uses `/tmp/onti-data`
(or the directory named by `ONTI_DATA_DIR`) — see
[Deploying to Vercel](#deploying-to-vercel-serverless).

- `onti.db` (+ `-wal` / `-shm`) — users, ontology packs, datasets,
  reports metadata, audit.
- `ontology_BP2.json` — built-in sample schema in raw BP2 format;
  used by the sample pack seed and as a ready-to-import example.
- `motor_insurance_ontology.json` — a second sample schema (motor
  claims domain) demonstrating that any BP2-format JSON can be imported.
- `.onti.key` — machine-local key used to encrypt model API keys and
  to sign session cookies.
- `reports/` — generated report files.
- `*.csv|xlsx` uploads are parsed and stored as rows in the database.

To back up: stop the server, copy the whole `data/` folder (or the
directory named by `ONTI_DATA_DIR`).

### Deploying to Vercel (serverless)

The repository is linked to Vercel, so every push to the connected
branch triggers a deployment. Vercel detects the FastAPI `app` in
`main.py` and deploys the whole site as **one function** (Fluid compute);
`vercel.json` sets `maxDuration` to 60 s, 1 GB memory and the files the
function reads at runtime (`data/`, `templates/`, `static/`, `config/`,
`modules/`, `common/`). No separate static build is involved: FastAPI
serves the Jinja pages and `/static` assets itself.

**Why the deployment used to fail.** Vercel mounts the deployment
read-only, so SQLite could not create its `-wal` sidecar next to
`data/onti.db`; startup died in `db.init_db` →
`get_conn` → `PRAGMA journal_mode = WAL` with
`sqlite3.OperationalError: unable to open database file`, and every
request returned HTTP 500 `FUNCTION_INVOCATION_FAILED`.

**Writable storage.** `modules/db.py` detects `VERCEL` and switches to
`/tmp/onti-data` (`ONTI_DATA_DIR` overrides the location on any host):

- On the first request of each instance the bundled, pre-seeded
  `data/onti.db` is copied to the writable directory, so every visitor
  sees the same packs, KPI data, demo reports and pre-provisioned
  accounts — including `admin` / `Onti@2026`.
- Reports that were generated before the deployment are read from the
  bundled `data/reports/` folder when they are absent from the writable
  directory.
- Writes made **inside the app** (new registrations, uploaded data,
  generated reports, model configs) live as long as the function instance
  and are not shared between instances. For durable multi-user writes,
  run the app on a VM/container with a persistent disk, or point
  `ONTI_DATA_DIR` at a mounted persistent volume.
- Session cookies are signed with the bundled key and also validated
  statelessly, so a login survives cold starts and requests served by
  another instance.

**Let any account in.** Production deployments are public, but if
**Deployment Protection → Vercel Authentication** is enabled, visitors are
asked to sign in with a Vercel account and colleagues outside the Vercel
team cannot open the app. In Vercel: *Project → Settings → Deployment
Protection* → disable **Vercel Authentication** for the production domain
(or hand out a Protection Bypass link). Inside the app, users can then open
`/app`, sign in with a pre-provisioned account, or self-register
(`allow_registration` is on by default); admins add accounts in
**Admin → Users & audit**.

**Environment variables.** Set `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL_NAME`
in *Project → Settings → Environment Variables* and redeploy. They are
read from `os.environ` on first startup and imported once into the
encrypted model configuration (the same bootstrap also accepts
`LLM_VERIFY_SSL=false` / `SKIP_SSL_VERIFY=1`). AI features stay optional —
the rules engine, graph, imports and reports work without a model.

**Health check.** `GET /health` returns JSON without authentication and
is the quickest way to confirm a deployment is alive.

---

## 7. HTTP API (summary)

All endpoints except `/health`, `/login` and `/api/auth/*` require the
`onti_session` cookie; `/api/admin/*` additionally requires the admin
role.

- **POST** `/api/auth/register`, `/login`, `/logout` — accounts &
  sessions.
- **GET** `/api/config`, `/api/questions`, `/api/graph`,
  `/api/class-graph`, `/api/library`, `/api/search?q=`,
  `/api/search/insight?q=` (chips + affected sub-graph + written
  analysis), `/api/formulas/{key}/impact` — user portal reads.
- **GET** `/api/models`, **POST** `/api/models/select` — per-user
  model picker for the Model centre (enabled configs only, no keys
  exposed).
- **GET/POST** `/api/my/models`, **PUT/DELETE** `/api/my/models/{id}`,
  **POST** `/api/my/models/test` — the personal Model centre
  (user-private configs, optional `.env` registration).
- **GET** `/api/recommended-questions`, **GET/POST**
  `/api/my-questions`, **DELETE** `/api/my-questions/{id}` — smart
  suggestions and the caller's private question list.
- **GET** `/api/ask/history` — the caller's recent asks (memory) for
  the active pack.
- **GET** `/api/packs` and **POST** `/api/my/pack` — the scenario pack
  list for the header dropdown and the per-user selection (`null`
  resets to the admin-activated pack); **GET** `/api/dashboard` —
  scenario banner, KPI cards and timeseries for the resolved pack;
  **GET** `/api/graph-studio/history` — the caller's generation
  history (ontologies, previews, uploads, reports).
- **GET** `/api/graph-studio/sources`, **POST**
  `/api/graph-studio/build`, **POST** `/api/graph-studio/upload`,
  **POST** `/api/graph-studio/infer`, **POST**
  `/api/graph-studio/create-ontology` — source graphs (upload is
  in-memory only) and data → ontology generation with preview.
- **GET** `/api/graph/path`, `/api/graph/impact`, `/api/graph/cycles`
  — graph intelligence (path, N-hop impact, cycle detection).
- **POST** `/api/ask-stream` — SSE answer stream
  (`meta` → `token*` → `done` / `error`); the `done` event carries
  the `tokens` consumed.
- **POST** `/api/ask/compare` — the same question answered twice:
  `with_ontology` (rules / grounded AI, with its impact chain) and
  `without_ontology` (same model, raw data tables only, no graph), plus
  a `value` summary of what the ontology contributed.
- **GET** `/api/analytics/timeseries`, `/attribution`, `/scenario`,
  `/agent-stats` — monthly analytics.
- **POST/GET/DELETE** `/api/reports...` — generate, list, download,
  delete; **POST** `/api/reports/sample` — one-click demo report;
  **GET** `/api/reports/{uid}/view` — open the HTML inline.
- **GET/POST/PUT/DELETE** `/api/admin/...` — packs, ontology elements,
  data, datasets, questions, instance graph, visual graph, models,
  users, settings, audit; **GET** `/api/admin/user-models` — oversight
  of all user-created models (keys masked); **GET**
  `/api/admin/user-settings` — each user's current scenario pack and
  model selection for support.
- **POST** `/api/admin/ontology/sync-graph`, **GET**
  `/api/admin/visual-graph` — rebuild the impact graph from
  entities/relations; read any pack's impact graph.
- **GET/POST** `/api/admin/proposals`, `/approve`, `/reject` — AI
  proposal review queue.
- **POST** `/api/admin/packs/{id}/proposals/approve-all`,
  `/api/admin/data/tables/{id}/analyze` — approve all; run induction.
- **GET** `/api/admin/ontology/health?pack_id=` — structural
  integrity report.
- **GET/POST** `/api/admin/packs/{id}/versions`, restore — pack
  version snapshots.
- **POST** `/api/admin/packs/import/preview` — import dry-run.

`/api/ask-stream` body:

```json
{ "question": "What is the total VNB?", "mode": "auto", "history": [] }
```

`mode` is `auto` (rules first, AI fallback), `rules`, or `ai`.

---

## 8. Tests / verification

With the server running on port 8010:

```bat
:: layer-level checks (no server needed)
python tests\verify_t1.py
python tests\verify_t2.py
python tests\verify_t3.py
python tests\verify_t4.py
python tests\verify_t5.py
python tests\verify_t6.py

:: authentication & account flows (freshly re-seeded DB recommended)
python tests\verify_login.py               :: 24 checks — login, register, session, lockout

:: model endpoint contract (needs the live provider configured)
python tests\verify_model_endpoints.py     :: 65 checks — create/test/edit/default/delete, self-signed diagnostics, CRUD, audit

:: model key handling (offline, no provider call)
python tests\verify_model_key.py           :: 68 checks — paste hygiene, persistence, error mapping

:: end-to-end HTTP smoke
python tests\smoke_http.py

:: data import: sheets, column mapping + AI-proposal evidence
python tests\verify_t7.py

:: batch import: many files / every sheet / a folder
python tests\verify_t8.py

:: actuarial lapse pack: import -> metrics -> dashboard -> analytics
python tests\verify_t9.py

:: ontology value: 14 scenarios, with vs without ontology, scored report
python tests\verify_ontology_value.py      :: writes docs\ONTOLOGY_VALUE_REPORT.md
```

The layer checks (`verify_t1` … `verify_t6`) are **workspace-tolerant**:
they pin the built-in sample pack by slug rather than assuming it is the
active pack, use per-run fixture names, and assert structural /
consistency invariants instead of a fixed row count. `verify_t3` additionally
asserts the sample pack's historical v1 numbers only while that demo
snapshot is still in place; on a workspace whose demo rows were replaced it
prints `SKIP v1 parity` and runs the behavioural checks instead.

Coverage:

- `verify_login.py` exercises the full auth matrix — admin login, wrong
  password, unknown user, empty/short/invalid username, short/long
  password, disabled-account 403, freshbot lifecycle, register →
  session-cookie → /me → logout → 401, and verifies that login failures
  surface the real error text ("Invalid username or password") rather
  than the session-expiry wording.
- `verify_model_endpoints.py` drives every model CRUD path on both
  system-wide and per-user scopes, probes a live working gateway, an
  unreachable endpoint, a self-signed certificate, and the
  certificate-off toggle, verifies that raw API keys are never
  serialised back, and confirms that test failures are HTTP 200 with
  `ok:false` plus an actionable hint (not 4xx).
- The smoke suite covers the anonymous / user / admin cookie matrix,
  default-admin login, all read APIs, SSE rules answers with graph
  evidence, unmatched suggestions, report generation with all three
  formats, CSV upload validation and admin question/model endpoints.

`verify_t7.py` needs the server running with the synthetic-demo pack
seeded and exercises the administrator import path end to end:
multi-sheet workbook selection (default first sheet, explicit pick,
unknown-sheet error), manual column-mapping overrides, a sheet+mapping
commit round-trip, and AI-proposal evidence storage/retrieval.

`verify_t8.py` covers **batch import** end to end: one workbook expanded
into one item per sheet, name-matched sheets routed to their tables,
unmatched sheets creating new tables (with entity, attributes and graph
sync), several CSVs and a data-shaped JSON imported together, an
ontology-shaped JSON rejected with a pointer to **Import pack**, and an
unusable item aborting the whole batch without writing anything. It
needs the same synthetic-demo pack and cleans up every table it creates.

To reset to a clean state (re-seeds the sample pack and re-provisions
the default admin):

```bat
:: stop the server first
del data\onti.db data\onti.db-wal data\onti.db-shm
python -m modules.db
```

---

## 9. Project layout

The codebase follows a **thin HTTP layer, fat service layer** split:
`main.py` owns only the FastAPI surface; all business logic lives in
`modules/`.

```text
Code/
├─ main.py              # FastAPI app: pages, routes, SSE only.
├─ start_server.bat
├─ requirements.txt
├─ vercel.json          # serverless build/route config (all paths → main.py)
├─ config/config.json   # port, session, upload settings
├─ modules/
│  ├─ db.py             # schema, connections, settings, audit, crypto
│  ├─ seed.py           # idempotent built-in sample pack
│  ├─ auth.py           # accounts, PBKDF2, sessions, roles
│  ├─ ontology_service.py  # rule engine, formulas, graph, search,
│  │                      # health check, formula impact analysis,
│  │                      # graph-studio builder with provenance
│  ├─ qa_service.py     # Q&A pipeline: rules first, LLM fallback
│  ├─ analytics.py      # time series, MoM attribution, back-solve
│  ├─ llm_client.py     # OpenAI-compatible client + model CRUD
│  │                      # + per-user model resolution
│  ├─ admin_service.py  # packs, ontology CRUD, datasets, audit,
│  │                      # import (BP2 / onti-pack), versions
│  ├─ importer.py       # CSV/XLSX/JSON parse (sheet pick), single + batch
│  │                      # import, column mapping, validation
│  ├─ induction.py      # AI ontology induction (pending proposals + evidence)
│  ├─ report_builder.py # MD/HTML/PDF assembly
│  └─ report_charts.py  # matplotlib charts + chain graph
├─ data/
│  ├─ onti.db                      # SQLite database (auto-created)
│  └─ reports/                     # generated report files
├─ templates/           # login, app, admin pages
└─ static/              # app.css, common.js, graph.js, app.js, admin.js
```

### How a question flows through the modules

1. `main.py:ask_stream` validates the request, loads the active pack +
   the caller's effective model config (per-user pick, else default),
   and calls `qa_service.stream_answer(...)`.
2. `qa_service` asks `ontology_service.answer_question` (rules engine).
   On a hit it yields `meta` → `token` → `done` with the impact-chain
   subgraph.
3. On a miss (auto / AI mode), `qa_service` calls the configured LLM
   with a system prompt built from the ontology, proxies tokens, and
   attaches a post-hoc subgraph on `done`.
4. `main.py` wraps each event dict as `data: {...}\n\n` and streams it.

The sample pack's instance data is read at seed time from the first
`.xlsx` workbook found in the project root or `data/`. Set the
`ONTI_DATA_WORKBOOK` environment variable to point at a specific file.
Workbooks that include monthly sheets enable the **Performance
analytics** view; otherwise the pack falls back to a single-month
snapshot.
