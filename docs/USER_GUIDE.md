# Onti — Insurance Ontology Workspace · User Guide

> This guide is for **every user** — no coding knowledge required.
> Each step includes the exact UI path. For administration see
> `docs/ADMIN_GUIDE.md`; for architecture and APIs see
> `docs/TECHNICAL.md`; for setup see `README.md` in the project root.

---

## 1. What is this platform? (30-second overview)

Onti organises an insurance business's **metrics, data and drivers**
into a single "knowledge graph". You can:

- View key metrics & the graph — User portal → Dashboard
- Ask questions in plain language — User portal → Ask
- Save and re-use your own questions — Ask → ★ Save question
- Upload my own data — User portal → Data centre
- Turn uploaded data into a new ontology — User portal → Graph studio
- Generate a report in one click — User portal → My reports
- Govern users, models, questions and data — Admin console

**Two portals, one rule of thumb.** The **user portal** (`/app`, light
page) is where people *use and build*: ask, upload, create ontologies,
report. The **admin console** (`/admin`, deep-navy page) is where
administrators *govern*: accounts and access, the canonical question
list, the available models, token usage, and oversight of every
ontology. There is no link between the two by design — change the URL
if you land in the wrong one.

---

## 2. Regular user: 5-minute start

### 2.1 Read the dashboard

- The **scenario banner** on top names the scenario you are looking
  at — the active ontology (pack), its description and the data
  sources behind it (each source shows its kind and row count). You
  always know what scenario and data the numbers are based on. The
  banner also carries a **Full analytics report** button: a complete,
  multi-section report for this scenario is generated automatically
  the first time you open the dashboard and one click opens it
  (View / HTML / PDF / Markdown are also in My reports).
- The **KPI cards** show the numbers the active scenario's ontology
  declares. A pack defines its own metrics, so a VNB/APE pack shows
  VNB and margin, while an actuarial pack (e.g. a lapse experience
  study) shows annualised lapse rate, A/E ratio and the IFRS 17
  **CSM**, **ISR** and **EV** closing values. A card appears as soon
  as the table it reads has rows. If the grid looks empty, the note
  under it states how many metrics the pack declares and points to
  the **Data centre** (§4) to load the matching tables — this is a
  data gap, not a bug.

  > **What each KPI means.** Every card is one *metric formula* the
  > pack declares: the label names the metric and the number is
  > computed from the tables you loaded, never invented. Open **ⓘ What
  > does this mean?** on a card to see its exact formula and the table
  > it reads. The metrics a lapse-experience study (Actuarial Lapse
  > Experience) shows are:
  >
  > - **Annualised lapse rate** — 12 × actual lapses ÷ exposure, i.e.
  >   how many policies lapse per year.
  > - **Lapse variance (ppt)** — actual minus expected lapse rate, in
  >   percentage points.
  > - **A/E ratio** — actual ÷ expected; 1.00 means exactly on
  >   assumption.
  > - **Average assumption** — the exposure-weighted assumed lapse
  >   rate, shown for reference.
  > - **CSM / ISR / EV (closing)** — IFRS 17 Contractual Service
  >   Margin, Insurance Service Result and Embedded Value closing
  >   balances (in $M).

- The **scenario dropdown** in the header lists every pack you can
  access. Pick another one and the dashboard, graphs, analytics, Ask
  and Graph studio all switch with it — nothing to reconfigure.
  Choose **System default** to return to the administrator-activated
  pack. Your choice is remembered the next time you sign in.
- The **knowledge graph** in the middle: each node is a metric or
  driver. Click **Open large view** for two extra tools —
  **Find a path** (pick two nodes → Find, to see how they influence
  each other) and **Impact analysis** (pick a node → Analyse, to see
  what it drives downstream or what drives it upstream).
- The legend maps colours / dash patterns to relationship types: blue
  solid = structural, green dash = operational driver, brown dots =
  weighting, purple long-dash = cross-link.

### 2.1.1 Turn the ontology on and off

The header carries a **Use ontology ON / OFF** switch, present in both the
user portal and the admin console. It is the master control for whether the
active ontology is used at all, and it is remembered in your browser.

**ON (the default).** Everything works as described in this guide: KPI cards,
the knowledge graph, insights, performance analytics, impact chains, grounded
answers and full reports.

**OFF — text-only mode.** Every ontology-derived visual is hidden and an
orange banner at the top of every page says **“Ontology is OFF.”** Nothing is
broken; this is the deliberate demonstration state:

| Area                           | What you see with the ontology OFF                                                                                                                                                       |
| ------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Dashboard                      | KPI cards and the knowledge graph are replaced by a short “Ontology is OFF — text-only mode” panel                                                                                       |
| Performance analytics          | The same panel — no trend chart, no driver attribution                                                                                                                                   |
| Knowledge graph / Graph studio | A panel explaining that graph views need the ontology                                                                                                                                    |
| Ask                            | Answers are grounded in the uploaded data tables but with **no ontology layer** — no metric formulas, no impact chain, no entity linking. A “Without ontology” badge marks every answer. |
| My reports                     | Reports are still generated and downloadable, but **text-only**: the graph, evidence, chart and analytics sections are removed and the title is suffixed “— No ontology (text-only)”     |

Turn the switch back to **ON** — or press **Turn ontology back on** in the
banner or panel — to restore every visual immediately. This is the fastest
way to show a client the difference the ontology makes across the whole
product, not just in one answer.

### 2.2 Ask questions

Type directly into the **Ask the ontology** box, e.g.:

- `What is the total VNB this year?`
- `Which channel contributes the most APE?`

Answers are grounded in your **entire active dataset** — the assistant
sees a complete profile of every table (all months, per-month totals,
value ranges), not a truncated sample. Asking *"what months are there
in the monthly experience data?"* lists every month in the data, and
*"how does Sep compare to Aug?"* is answered with real totals for both
months. For the Actuarial Lapse Experience pack two instant rules cover
exactly these questions (**Data coverage** and **Monthly lapse
experience**), so they answer immediately without a model call.

The "🤖 Model: … · change" button at the top of the Ask view shows the
model currently answering your questions and jumps to the
**Model centre** (see section 2.9 below), where you can try any
enabled model (e.g. fast vs. reasoning), including models you register
yourself. Your choice is remembered and only affects your own answers.

Four kinds of suggestion chips sit above the question box:

- **✨ Recommended for you** — questions generated from the active
  ontology (its metrics, numbers and entities);
- **★ My questions** — questions you saved yourself. Type a question,
  press **★ Save question**, and it stays here; click the × on a chip
  to remove it;
- **🕘 Recent asks** — the last questions you asked this ontology,
  across sessions. Click one to re-ask it;
- **Standard questions** — the organisation-wide list curated by your
  administrator.

Click any chip to ask it immediately.

### 2.2.1 Compare answers with and without the ontology

The yellow **◐ Compare: with / without ontology** button next to the
Auto / Rules engine / AI mode switch turns every question into a
side-by-side demonstration of what the ontology adds. Press it once —
the button stays highlighted and **Send** becomes **Compare** — then
ask a question as usual.

One question is answered twice and shown in a single card:

- **🟢 With ontology** — the rules engine answer when a canonical rule
  matches, **plus** the deterministic deep analysis: the exact factor
  bridge for the month, the product-mix proof, the agent-cohort tables,
  the statistical diagnostics and a numbered reasoning trace. Every
  figure is computed from the pack's own tables, so the answer is
  reproducible and audit-ready, and it carries its **🔗 Impact chain**.
  If a model is configured, a short **AI narration** of the same
  pre-computed analysis is shown underneath — the model explains the
  reasoning but never recalculates it.
- **⚪ Without ontology** — the same model, asked the same question
  grounded in the **same uploaded data tables** (so it still sees the
  company's real figures) but **without any ontology layer**: no
  metric formulas, no entity definitions, no joins, no impact chain.
  It works with literal column names and can restate what the table
  shows, but cannot decompose the number into drivers, join a second
  table or reason across concepts. There is deliberately **no impact
  chain**, because there is nothing to trace.

The card contains only the two answers (and the ontology side's impact
chain) — no explanatory banner or counter strip is added, so you can
judge the two outputs on their own content. The same counts are still
available programmatically from `POST /api/ask/compare` (`value`), and a
reproducible measurement of the gap is in
[ONTOLOGY_VALUE_REPORT.md](ONTOLOGY_VALUE_REPORT.md).

This is the mode to use in a client demo: same model, same question,
and the only difference is the ontology. The ontology side is
deterministic and needs no model, so it answers even when no model is
configured; the narration and the non-ontology side each cost one model
call.

Press the yellow button again to return to normal single answers.

### 2.2.2 Batch questions and memory

Press **📋 Batch** to paste a whole list of questions (one per line,
up to 20). Each is asked in sequence, and later questions use the
earlier answers as **memory** — so you can ask a broad question first
and then follow up with *"and last month?"*. Every batch question and
answer is stored in your history, and you can save the whole list to
**★ My questions** with one click. When the batch finishes, use
**📄 Generate report from session** or **⚡ Sample report** to package
the results.

The **badge colour** next to an answer tells you its source:

| Badge | Meaning                                     | Trust level       |
| ----- | ------------------------------------------- | ----------------- |
| Green | Deterministic rule (configured by an admin) | Highest — cite it |
| Amber | No matching rule                            | Only suggestions  |
| Blue  | AI model answer                             | Reference only    |

Every answer includes an **impact chain graph** showing which factors
make up the number — handy for screenshots when presenting to
leadership.

### 2.3 Bring in your own data

The **Data centre** lists the underlying data tables that feed the
active ontology — every table shows its source file, kind and upload
time, and you can open any table to read its rows. Tables with more
than a dozen rows also show a **View full dataset** button: it opens a
paged browser (50 rows per page, with **‹ Prev / Next ›** controls and
a running *Rows x–y of z* counter) so you can read every record, not
just the preview.

Two things you can do with *your own* file:

- **Preview it — read-only.** Open **Graph studio** and press
  **📤 Graph from file**, then choose a `.csv`, `.xlsx` or ontology
  `.json`. The platform lines the file's columns up against the
  ontology and draws its graph. **Nothing is stored** and the ontology
  is not changed — a safe way to see how your data fits.
- **Turn it into a real ontology.** Open **Graph studio → ✨ Build an
  ontology from your data** (section 2.7), or ask your administrator to
  import the file — see [ADMIN_GUIDE.md](ADMIN_GUIDE.md), section 4.5.

> Imports are **safe**: every dataset keeps its full version history
> and never overwrites existing data. The administrator chooses which
> version is active.

### 2.4 Generate a report

Open **Reports** → choose the Q&A scope and the sections →
**Generate** → download as **Markdown / HTML / PDF**, or click
**🌐 View** to read it right in the browser. Reports built from the
current scenario carry a self-describing title — `<title> | Scenario:
<pack> | Data: <sources>` — so a downloaded file always states what
scenario and data it is based on.

Two extra section checkboxes make reports self-contained:

- **Ontology knowledge graph** — the ontology graph itself, so readers
  see the structure behind the numbers;
- **Performance analytics** — the month-on-month driver decomposition
  (headcount / productivity / product mix) and the per-channel
  salesforce table, identical to the Performance analytics view.

> **What Performance analytics shows — and why it can be empty.**
> The page adapts to the active scenario. **When the pack declares its own
> monthly table** — any table with a `report_month` / `month` column of
> `YYYY-MM` values — the page renders a **Monthly trend from this pack**
> card automatically: pick any numeric column from the dropdown and the
> chart and month table redraw. If the pack also holds a lapse count and
> an exposure column, the default metric is the **annualised lapse rate**
> (12 × lapses ÷ exposure) — the same figure as the dashboard's
> *Annualised lapse rate* KPI, so the trend and the headline agree.
>
> **When the pack has no monthly table at all**, the page reads the fixed
> monthly sales schema: `vnb_monthly`, `channel_monthly`, `agent_monthly`
> and `agent_master`, each carrying a `month` column. This is **not an
> error**: the page then lists the tables it requires, the tables your
> pack already has and the ones still missing. Three routes to fill it:

1. **Switch scenario** — choose a pack that carries the monthly schema
   from the **Scenario** selector at the top of the page (the built-in
   VNB / APE sample pack is one). The view fills in automatically.
2. **Upload the tables** — open **Data centre** and upload a workbook
   containing the four tables below; sheets are matched to tables by
   name, so keep the sheet names identical to the table names. The key
   columns are what let the views group by month, channel and agent.
3. **Your data may not be VNB / APE at all** — then the
   driver-attribution, scenario and agent views do not apply, but the
   **Monthly trend from this pack** card still works: it is built from
   whatever monthly table your pack declares. Read the metrics the active
   pack declares on the **Dashboard**, and explore how they connect in the
   **Knowledge graph** or **Graph studio**.

| Required table    | Key columns            | What it powers                                        |
| ----------------- | ---------------------- | ----------------------------------------------------- |
| `vnb_monthly`     | `month`                | Company trend chart and the month-on-month VNB bridge |
| `channel_monthly` | `month, channel_id`    | Channel breakdown and the driver attribution          |
| `agent_monthly`   | `month, channel_id`    | Agent productivity table                              |
| `agent_master`    | `agent_id, channel_id` | Agent names and reporting lines for the agent view    |

No questions yet? Press **⚡ Sample report** — the platform answers
the ontology's recommended questions instantly with the rules engine
(no AI wait, no token cost) and renders a complete demo report.

### 2.5 Use the floating help button

A round **?** button sits in the bottom-right corner of every page.
Click it for a built-in quick-start and FAQ — no need to open this
document.

### 2.6 Search the ontology — instant insight

Open **Ontology workspace** and use the **Search the ontology** box at
the top. Type any term — for example `vnb`, `agent`, `premium`,
`margin` — and the platform instantly finds matching entities,
attributes, formulas and rules across the whole active ontology.
Results show the kind and name as chips, so you can locate a concept
without browsing every table.

Below the chips an **Instant insight** card appears for every search:

- **What it is** — the top match's kind, description and key facts
  (attributes, source tables, relations, formulas using it);
- **Affected graph neighbourhood** — the matched nodes plus one hop
  rendered as a small graph, so you immediately see what drives and
  what depends on the concept;
- **Written analysis** — a short paragraph explaining the match and
  how to use it.

### 2.7 Build your own graph in Graph studio

Open **Graph studio** from the sidebar. It builds one of three small,
focused graphs instead of one huge diagram:

1. **Data lineage** (shown automatically) — which data tables feed
   which entities, so you can immediately see how your uploaded data
   connects to the ontology. An edge marked "feeds (inferred)" means
   the platform guessed the link from matching column names.
2. **Schema** — the ontology's entities and how they relate.
3. **Column mapping** — one node per unique column concept. A column
   that exists in several tables (for example Channel ID in seven
   tables) appears only once, marked with the number of source
   tables. Uncheck **Include columns with no ontology attribute
   match** if you only want mapped columns.

The table picker names every table's **source** — the uploaded file,
its kind and upload time — so you always know where each table came
from. Tick the tables you want (or use **Select all** / **Clear**),
then press **Build graph**.

- **Click any node** — the **Node provenance** panel shows exactly
  which entity, table or column(s) it comes from; aggregated columns
  list every source table and raw column name.
- **Graph from file** — upload a `.csv`, `.xlsx` or ontology `.json`
  file to preview its graph instantly. Columns are matched to the
  active ontology and the connections are drawn for you. The file is
  not stored; it is a safe preview.

Below the graphs, the **Generation history** card lists everything
produced in Graph studio — ontologies created, previews, data and
ontology-JSON uploads, and the reports generated along the way —
newest first, each labelled by type. Past ontology entries offer
**Switch to it** to make that scenario active again; report entries
link straight to the report in the browser.

### 2.8 Turn your data into a new ontology

The top card in **Graph studio — ✨ Build an ontology from your data**
creates a brand-new, working ontology without any manual modelling:

1. Upload your tables first (**Data centre**, CSV/XLSX).
2. In Graph studio, tick the tables to include and type a name.
3. Optional: tick **AI enrichment** to let the configured model write
   a one-line description for each entity and propose up to eight
   starter questions (they are added to the new ontology).
4. Click **🔍 Preview inferred ontology**. Each table becomes an
   entity, each column a typed attribute, and `*_id` columns pointing
   at another table become relations. Review the counts, lists,
   AI-suggested questions and preview graph — nothing is saved yet.
5. Click **✓ Create & activate ontology** and confirm. The platform
   creates the ontology, copies the data rows in, and activates it.
6. You return to **Ask** automatically — the new ontology answers
   immediately, and its graph and data feed the dashboard, analytics
   and reports. A **full analytics report is generated
   automatically** (titled with the scenario and the source tables)
   and lands in My reports; the creation and the report appear in
   Graph studio's Generation history.

Your previous ontology is not deleted; administrators can re-activate
any pack from **Admin → Ontology governance**.

### 2.9 Model centre — add your own AI model

You are not limited to the models your administrator configured. Open
the **Model centre** view from the sidebar (the "🤖 Model: … · change"
button in the Ask view jumps there):

1. Pick a model: the **System models** list contains every endpoint the
   administrator enabled; **Default** follows the
   administrator-configured model. Selecting one switches your answers
   to it immediately.
2. To register your own, press **＋ Add my model** and fill in a
   display name, the **Base URL** of any OpenAI-compatible endpoint
   (OpenAI, Azure OpenAI, DeepSeek, SiliconFlow, Deloitte Nova
   (D.Nova), Ollama, vLLM, …), your **API key** and the **model id**;
   temperature, max tokens and timeout have sensible defaults.
   Paste the key exactly as issued — surrounding quotes, a `Bearer`
   prefix, a line break or trailing spaces are removed automatically,
   and placeholder text such as `sk-...` is treated as "no key".
   **The Base URL must be the provider's full API prefix, not the bare
   host**: the platform calls `{base_url}/chat/completions`, so for
   Nova it is `https://nova.deloitte.com.cn/v1` — the bare
   `https://nova.deloitte.com.cn` fails with *405 Not Allowed*, and
   `/v1` is never appended for you (other gateways use different
   prefixes). Trailing slashes don't matter; a missing prefix does.
   When in doubt, copy the URL from the provider's API documentation.
3. If the gateway uses a self-signed TLS certificate — Deloitte Nova
   (`https://nova.deloitte.com.cn/v1`) is one — **uncheck** *Verify
   SSL certificate*. Every other gateway keeps it on.
4. Press **Test** before **Save**. A wrong model id is the most
   common mistake: e.g. DeepSeek V4 Flash is
   `deepseek-ai/DeepSeek-V4-Flash` on SiliconFlow but
   `DeepSeek-V4-Flash` on the Nova gateway. The **Test** button catches
   this and tells you "model not found" instead of a silent crash.
   Every test returns a verdict rather than an error page: green with a
   latency, or red with the cause, a suggested fix and the gateway's own
   wording underneath. If the message says **The gateway's TLS
   certificate could not be verified**, clear *Verify SSL certificate*
   and test again; if it is a timeout, press Test once more before
   changing anything.
5. Press **Save model** — it is stored for your account only and
   reappears under **My models** the next time you sign in. No `.env`
   file editing is ever needed.
6. Optionally tick "Also register in the server .env file" so the
   deployment keeps a copy of the endpoint outside the database.

Your administrators can see that a model exists (owner, name, base
URL — never your key) to support you if an endpoint misbehaves.

---

## 3. Administrator: create a brand-new ontology

> No code required — roughly 10 minutes. All UI paths are in the
> **Admin console**, which is for governance: accounts, ontologies,
> canonical questions, models and usage monitoring. Day-to-day
> questions and data-driven ontology creation happen in the user
> portal.

### Step 1 — Create the ontology

Go to **Ontology governance → New ontology**:

- Step 1: fill in the name, slug (lowercase letters/digits) and
  description;
- Step 2: choose **Start from blank** or **Copy an existing ontology**;
- After creation a **getting-started checklist** pops up with 6
  clickable steps — just follow them.

### Step 2 — Build the schema (Entities → Attributes → Relations)

1. **Entities**: add business concepts, e.g. Policy, Agent;
2. **Attributes**: add typed fields (with units) to each concept;
3. **Relations**: connect concepts to each other;
4. The impact graph is **generated automatically** from the above — no
   manual drawing required. If the Schema and Impact graphs ever look
   different, open **Knowledge graph → ⟳ Sync impact graph from
   ontology** to realign them in one click.

> When editing a **formula**, an **impact panel** appears automatically
> and shows everything that depends on it (other formulas, graph nodes,
> question rules). Green = safe to change; amber = lists the blast
> radius so you know what will be affected before you save.

### Step 3 — Upload data

**Data oversight → pick a table → ⬆ Upload data**:

1. Drop a `.csv` / `.xlsx` / `.json` (max 10 MB) and give the dataset a
   name.
2. Multi-sheet workbook? Pick the **Workbook sheet** holding the table
   you want — the first sheet is read by default, and the preview
   re-runs for the sheet you choose. One workbook is one upload; a
   second tab is the same file uploaded again with the other sheet
   picked.
3. Review the **Column mapping** panel. The platform maps your headers
   to the table's columns automatically; a header set to **— ignore —**
   is not imported. Correct any wrong target yourself and the preview
   re-runs. `*` marks a required column — all of them must be mapped.
   Unmatched headers are simply shown as **unmapped** and ignored, so a
   wide workbook does not fail.
4. The preview lists valid / invalid rows and any per-row errors — a
   single bad row blocks the import.
5. Press **✓ Looks good — commit import**.

**Many files at once.** The same card has a **Batch import** area:
**＋ Select files…** or **📁 Select folder…**. Every file you pick is
split into its own row — a workbook becomes one row per sheet, a `.csv`
or data `.json` one row — and each row is routed to a table by name (an
unmatched one offers to **create a new table**). Adjust a target or a
mapping per row and press **✓ Import all …**. As with a single file,
one unusable item blocks the whole batch — nothing is imported until
every row is clean.

See [ADMIN_GUIDE.md](ADMIN_GUIDE.md), sections 4.5 and 4.6, for the
same flow in detail.

### Step 4 — Health check

Press **Health check** for a one-click 9-item report:

- red = must fix (e.g. a formula fails to compile);
- amber = should fix (e.g. an entity without attributes, an isolated
  node);
- green = all clear.

The report lists the exact offending items — click each to fix it.

### Step 5 — Activate

Select the pack in the dropdown → **Activate**. Users see the new
ontology immediately; no restart is needed.

---

## 4. Administrator: two safety features

### Version snapshots & one-click rollback

- Location: **Ontology governance → select a pack → History**;
- Before a big edit, press **Save snapshot now**;
- If something breaks, click **Restore** on the relevant version — the
  current state is first auto-saved as `before-restore`, so a rollback
  can itself be undone;
- Each pack keeps the 20 most recent snapshots.

### Import preview (look before you commit)

- Location: **Import pack** — choosing a JSON file triggers an
  automatic preview:
  - it shows how many entities / attributes / relations / formulas /
    rules / tables will be imported;
  - blocking issues (duplicate slug, missing fields, broken
    references) are shown in red and **disable the Import button**;
  - advisory warnings (e.g. "no data tables") are shown in amber;
- The file is only written once the preview passes;
- Three file shapes are supported: `onti-pack` exports from this
  platform, raw ontology definition JSON
  (e.g. the built-in `data/ontology_BP2.json`), or a
  relationship-graph ontology where entities are plain name strings
  and relationships use `from`/`to`/`type`
  (e.g. `data/lapse_demo.json` — its source tables are registered
  automatically);
- If a file fails validation, the error names the exact item and
  missing field, so it is easy to fix and re-import;
- Full field-by-field format requirements (all three shapes, with
  minimal examples): see
  [ONTOLOGY_JSON_REFERENCE.md](ONTOLOGY_JSON_REFERENCE.md).

### Models, questions and usage oversight

The rest of the admin console is governance, not modelling:

- **Models & usage** — add/disable OpenAI-compatible model endpoints,
  set the system default, and review the last 30 days of AI usage:
  total calls, tokens, average latency and failures, broken down by
  model and by user.
- **Canonical questions** — the organisation-wide standard questions
  shown to every user. Users' own saved questions stay private and are
  managed by the users themselves.
- **Users** — create accounts, reset passwords and control access.
- **Data oversight** — review tables and datasets across packs.
- **All reports** — view reports generated by all users.

---

## 5. Frequently asked questions

**Q:** The answer says "no matching rule".
The current ontology does not cover that question. Try one of the
suggested questions, or ask your administrator to add a rule / upload
relevant data.

**Q:** The assistant says it can only see part of my data (e.g. "only
January").
That was an old limitation — answers are now grounded in the **full
active dataset** of every table (all months, per-month totals, value
ranges), never a truncated sample. If you still see a partial-data
answer, refresh the page and re-ask; the Data centre preview stays
paged for readability, but the assistant always reads everything.

**Q:** Where do the numbers come from?
From the data tables in the Data centre plus transparent formulas —
never invented by the model. Hover a graph node to see its formula
source.

**Q:** What if I make a mistake?
Data uploads are versioned and never overwrite anything. If you create
an ontology in Graph studio, the previous one is kept — ask an admin
to re-activate it from Ontology governance. Admins can additionally
roll back packs via History.

**Q:** What do the graph edges mean?
See the legend in section 2.1; hover any node or edge for the full
label and description.

**Q:** Does the AI remember my earlier questions?
Yes. Each AI answer carries the recent session turns plus your four
most recent stored answers for this ontology, so follow-ups understand
context. The 🕘 Recent asks chips and the History view show everything
you asked; nothing is shared between users.

**Q:** Can I ask follow-up questions like "How does it compare to the
previous month?" or "Why is there such a movement?"
Yes. After a month-specific question (e.g. *"What is the lapse
experience in Sep?"*), the rules engine inherits that month for
follow-ups: *compare* questions answer with the month-on-month table
for the same month, and *why / movement* questions list the biggest
cohort movers, overlapping business events (where the pack carries an
events table) and the financial bridge attributed to that month. This
works in the Ask view, the Dashboard chat and the with/without
ontology comparison.

**Q:** Can I see the full dataset behind a table, not just a preview?
Yes — Data centre → any table card with more than a dozen rows shows
**View full dataset**, which pages through the complete table 50 rows
at a time. The assistant always reads the full dataset regardless of
what the preview shows.

**Q:** Where does a table or graph node come from?
Graph studio lists the source file, kind and upload time on every
table chip, and the Node provenance panel names the exact table,
column or file behind any node. Reports include the ontology graph and
its sources as well.

**Q:** How do I compare results across different ontologies?
Use the **scenario dropdown** in the header. Each option is a scenario
pack with its own data, graph and reports; switching updates the
dashboard and every other view immediately. Every report states which
scenario and data sources it was built from, and Graph studio's
Generation history lists the ontologies you created before.

---

## 6. Where to get help

1. Click the **?** button in the bottom-right corner for the in-app
   help panel;
2. Read this guide;
3. If still stuck, contact your platform administrator — every action
   is recorded in the audit log for easy troubleshooting.
