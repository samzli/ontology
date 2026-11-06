# Onti — Ontology Governance Console · Administrator Guide

> This guide is for **administrators**. The admin console ( `/admin` ,
> deep-navy page) is for governance only: accounts, ontologies, data
> oversight, canonical questions, models and usage monitoring. The
> end-user perspective lives in `docs/USER_GUIDE.md` ; architecture in
> `docs/TECHNICAL.md` ; the ontology JSON file format in
> `docs/ONTOLOGY_JSON_REFERENCE.md`; setup in the root`README.md` .

---

## 1. The governance mindset

The two portals have strictly separated responsibilities:

* **User portal** (`/app`, light page) — users ask questions, upload
  data, build ontologies in Graph studio and generate reports.
* **Admin console** (`/admin`, navy page) — administrators govern:
  accounts and access, oversight of every ontology and dataset, the
  canonical question list, model configuration and token spend.

The portals stay separate: the admin console header has a one-way
**← User portal** button back to `/app` , while the user portal has no
link into `/admin`. The default administrator (`admin` /
`Onti@2026` — change the password after first login) can enter both;
self-registered users only ever see `/app` .

## 2. Console map

1. **System dashboard** — pack/user/model/report statistics, recent
   activity, security signals, database usage, AI tokens (30 days)
   with breakdowns by model and by user.
2. **Users & audit** — create accounts, promote/demote, enable/
   disable, reset passwords, toggle self-registration, review the
   audit feed.
3. **Ontology governance** — packs, health check, entities,
   attributes, relations, graph nodes/edges, formulas, rules.
4. **Data oversight** — tables and datasets across packs; upload new
   data for any pack (pick the workbook sheet, review and correct the
   auto-guessed column mapping, preview with zero writes, then commit
   as a versioned dataset). **Batch import** brings in many files, a
   whole folder, or every sheet of a workbook at once — each item kept
   separately and routed to its table (§4.6). The active dataset's rows
   are fully browsable: 50 rows per page with **‹ Prev / Next ›** and a
   *Rows x–y of z* counter, so you can inspect every record.
5. **Canonical questions** — the organisation-wide question list
   (bulk import from TXT / CSV / XLSX).
6. **Knowledge graph** — Schema / Impact / Instance layers plus the
   one-click **Sync impact graph from ontology** action.
7. **Models & usage**— system model endpoints,**User-created
   models**oversight table, the**User portal settings** table (each
   user's current scenario pack and model), token usage.
8. **AI proposals**— review queue for AI-generated**ontology
   additions** (entities, graph nodes/edges, metric formulas). A
   proposal never edits or deletes existing content and never touches
   application code: approving one only adds the described element to
   the pack, and nothing is applied until you approve it. Two sources
   feed the same queue: the upload-time induction pass, and the
   **self-iterating agent** — when users opt in with the ⟳ Self-iterate
   switch, its outer loop harvests recurring answer gaps into `node`
   proposals (keys prefixed `si_`, rationale links the finding) so the
   ontology grows toward what people actually ask. Approving an `si_`
   proposal runs the same validated write path and version snapshot as
   any other change. Rejected proposals are simply not applied — the
   underlying finding stays recorded and is not re-proposed
   automatically.
9. **All reports** — every user's reports with HTML / PDF / Markdown
   downloads.

## 3. Ontology provenance — always know where a pack came from

Every pack carries an **origin**record, shown as the**🧾 badge**
next to the selected pack in Ontology governance and visible to users
in Graph studio:

* **Generated in Graph studio** — "Generated from data in Graph
  studio (tables: …) by \<username> \[+ AI enrichment]" — the exact
  tables the user picked are listed.
* **Imported from JSON** — "Imported from JSON file by \<admin>".
* Built-in seed packs carry their own descriptive origin.

Use the badge when a user asks *"where does this ontology come
from?"* — the answer is on screen, no database digging.

## 4. Supporting users

### 4.1 A user's own model fails

Users register private models in their **Model centre**. In
**Models & usage → User-created models** you see every entry with
owner, name, model id, base URL and a **masked** key (never the
secret). Ask the user to press **Test** in their Model centre and
share the error message; the base URL shown here is usually enough to
spot typos or wrong paths.

### 4.2 Reviewing a user-built ontology

Packs created in Graph studio appear immediately in Ontology
governance with their origin badge (and with an auto-generated
analytics report in All reports). To review one:

1. Select it in the pack bar (no need to activate);
2. Run **🩺 Health check** — red items block trustworthy answers;
3. Open **Knowledge graph** to compare Schema and Impact layers; if
   they diverge, press **⟳ Sync impact graph from ontology**;
4. Browse the copied datasets under **Data oversight**.

Activate it only when it passes; the currently active pack is what
every user sees.

### 4.3 Reviewing reports

**All reports** lists every user's reports. Download the HTML for the
full layout with charts — including the optional **ontology graph**
and **performance analytics** sections — when a user forwards a
number you cannot reproduce. Report titles state the scenario pack
and the data sources behind them
( `<title> | Scenario: <pack> | Data: <sources>` ).

> **A user reports an empty Performance analytics page.** The page first
> checks the user's active pack for a monthly table. If the pack declares
> one (a `report_month`/`month`column of`YYYY-MM` values), the page
> auto-renders a **Monthly trend from this pack** card — no action needed,
> and this is expected for any pack, including a lapse experience study.
> The empty state appears only when the pack has **no** monthly table: the
> driver / scenario / agent cards need the built-in VNB/APE schema
> ( `vnb_monthly`,`channel_monthly`,`agent_monthly`,`agent_master` ). In
> that case the page itself lists which tables are missing and how to fix
> it. Point the user either at the **Scenario** selector (switch to a pack
> that has the schema) or at **Data centre** (upload a workbook whose sheet
> names match those tables). The pack's own metrics on the Dashboard are
> always the authoritative numbers.

### 4.4 Reproducing a user's setup

When a user forwards a screenshot or a number you cannot reproduce,
open **Models & usage → User portal settings**. The table shows, for
every account: the **scenario pack** they are working in (a badge
distinguishes the system default from a user-selected pack), the
**model** their answers use, and their last sign-in. Switch to the
same pack on your side (or select the pack in Ontology governance)
to see exactly what they see; reports and dashboards are scoped per
scenario.

### 4.5 Importing data and mapping columns

**Data oversight → pick a table → ⬆ Upload data.** The flow is
deliberately preview-first: nothing is written until you commit.

1. **Drop a `.csv`/`.xlsx`/`.xls`/`.json`** (max 10 MB) and give
   the dataset a name.
2. **One workbook, many tabs.** A `.xlsx` with several sheet tabs is a
   single upload — you do **not** upload one file per table. After the
   file is read, a **Workbook sheet** selector appears listing every
   tab; choose the one holding the table you want. Only that sheet is
   read, and the first sheet is used by default. To bring in a second
   tab, upload the same file again and pick the other sheet — or bring
   in every tab at once with **batch import** (see §4.6).
3. **Check the column mapping.** The platform auto-maps your headers to
   the target table's columns by exact name and by close
   (case/substring) match. The panel lists every uploaded header with
   its current target: a header mapped to **— ignore —** is *not*
   imported. If a header was unmatched (counted in the **unmapped**
   badge) or guessed wrongly, pick the right column from its dropdown —
   the preview re-runs automatically. Headers that match nothing in the
   target table are shown as unmapped and simply ignored, so a wide
   workbook never produces a hard error; only genuinely required
   columns ( `*` ) must be mapped.
4. **Read the preview.** It reports valid / invalid rows, how many
   columns were read, how many stayed unmapped, and the exact errors
   per Excel row. A single invalid row blocks the commit — fix the file
   first.
5. **Commit.**Press**✓ Looks good — commit import**; the dataset is
   stored and activated, metrics recompute from it, and the AI induction
   pass runs in the background (review the result in **AI proposals**,
   console map item 8).

Every write is versioned: the previous dataset stays and can be
re-activated from the Datasets list at any time.

> **Match column types to the data.** Every target column has a data
> type ( `INTEGER`,`FLOAT`,`TEXT` ) and the preview validates each cell
> against it. A column typed `INTEGER` rejects decimal values — e.g. an
> `exposure` column that holds fractional policy-months such as
> `534.483826`must be`FLOAT` , or every row with a decimal fails with
> "is not an integer". When a large share of rows is rejected with type
> errors, fix the column type in Ontology governance (Attributes) and
> re-upload; changing the type does not touch the already-stored data.

**Upload performance.** Imports scale linearly and are fast: a 96-row
sheet commits in a fraction of a second, and validating 20, 000 rows
takes under a second. Rows are written in one batched transaction, so
if a very large workbook feels slow the time goes into reading the
file and the background AI induction pass — not the database write.

### 4.6 Batch import — many files, a folder, or every sheet at once

The **Batch import**area of the same**⬆ Upload data** card brings in
a lot at once without losing track of what is what. Nothing is written
until you commit, exactly like the single-file flow.

1. **Pick the input.** **＋ Select files…** accepts many `.csv`,
`.xlsx`/`.xls`and`.json` files at once; **📁 Select folder…**
   uploads every supported file inside a folder (and its subfolders) in
   one go. A folder may mix formats — that is fine.
2. **Every file is split into items.**A workbook becomes**one item
   per sheet**; a `.csv`or data`.json` becomes a single item. So a
   5-sheet workbook appears as 5 separate, clearly named rows — nothing
   is merged.
3. **Each item is routed to a table.** By name: a sheet called
`Monthly_Experience`routes to the table keyed`experience` , and a
`region_sales.csv`routes to a table keyed`region_sales` . When no
   table matches, the item is offered as **＋ Create new table** — on
   commit the platform creates the table, its columns, a matching
   ontology entity and attributes, and syncs the impact graph. Change
   the target from the dropdown for any item.
4. **Check the mapping per item.** Every row has its own expandable
   column-mapping editor, with the same **— ignore —** / `*` required
   rules as §4.5. Adjust a target or a mapping and the whole preview
   re-runs.
5. **Commit.** **✓ Import all …** writes every item as its own dataset
   (activated) and creates any missing tables. As in the single-file
   flow, **one unusable item aborts the whole batch — nothing is
   written** — so a partially-bad folder never leaves a half-imported
   state. Untick **skip** on an item you do not want to import.

Data-shaped `.json`(an array of records, or`{"records" | "data" |
"rows": […]}`) imports like a table. A`.json` that is an *ontology*
definition (it contains `entities` ) is rejected here with a pointer to
**Import pack** — that is the right entry for ontology files.

## 5. Models and token spend

Onti is **provider-neutral**. Any OpenAI-compatible endpoint works —
OpenAI, SiliconFlow, DeepSeek direct, Deloitte Nova (D. Nova), Azure
OpenAI, Together, Ollama-OpenAI, vLLM — there is **no hardcoded list**
in the backend and adding a new provider never requires a code change.
The UI only asks for three values: base URL, model id, API key (plus
the optional **Verify SSL certificate** toggle).

* **System models** (Models & usage): add or disable any
  OpenAI-compatible endpoint, set the default, test connections. The
  default is what every user gets unless they pick otherwise.
* **User-created models**: private per user; you can see but not use
  them. Users register their own in the **Model centre** of the user
  portal — each entry is scoped to its owner only.
* **Token metering**: every AI answer writes a row (caller, model,
  success, latency, tokens). The System dashboard summarises the last
  30 days — totals, average latency, failures — by model and by user.
  A failing endpoint shows up as a failure spike; disable it there.

### Provider-specific configuration examples

These are the three gateways currently deployed in the team — copy the
fields verbatim. Other OpenAI-compatible gateways (Azure, Ollama, vLLM,
Together, …) only need their own base URL + model id + API key; the
form fields are the same for every provider.

| Field          | SiliconFlow                     |
| -------------- | ------------------------------- |
| **Base URL**   | `https://api.siliconflow.cn/v1` |
| **Model id**   | `deepseek-ai/DeepSeek-V4-Flash` |
| **API key**    | `sk-...`                        |
| **Verify SSL** | ✅ keep on                      |

| Field          | Deloitte Nova (D. Nova)           |
| -------------- | --------------------------------- |
| **Base URL**   | `https://nova.deloitte.com.cn/v1` |
| **Model id**   | `DeepSeek-V4-Flash`               |
| **API key**    | `ak-...`                          |
| **Verify SSL** | ❌**uncheck** (self-signed cert)  |

| Field          | Azure OpenAI                                              |
| -------------- | --------------------------------------------------------- |
| **Base URL**   | `https://<reg>.openai.azure.com/openai/deployments/<dep>` |
| **Model id**   | `gpt-4o-mini` (your deployment name)                      |
| **API key**    | `...`                                                     |
| **Verify SSL** | ✅ keep on                                                |

> **Important — model ids differ across gateways.** DeepSeek V4 Flash
> is `deepseek-ai/DeepSeek-V4-Flash` on SiliconFlow but
> `DeepSeek-V4-Flash` on the Nova gateway. Always press **Test
> connection** before setting a model as default — the button validates
> the exact model id that gateway accepts. A wrong model id fails with
> a clear "model not found" style error instead of a silent crash.

### Model setup checklist — pitfalls and validation

Five rules cover every configuration mistake seen in practice. They
apply identically to system models (this console) and to user models
(Model centre in the user portal):

**The Base URL must be the full API prefix, not the bare host.**
The platform sends requests to `{base_url}/chat/completions` , so the
URL must include the provider's API prefix — for most gateways that
is `/v1`.`https://nova.deloitte.com.cn/v1` works; the bare host
`https://nova.deloitte.com.cn` does **not** (Nova's gateway answers
*405 Not Allowed* because it only exposes routes under `/v1` ), and
neither do invented paths such as `/del/v1`or`/api/v1` . Trailing
slashes are harmless ( `/v1/`,`/v1///` are normalised away), but the
platform deliberately **never auto-appends `/v1`** — only a minority
of gateways use that prefix (Azure and most on-prem servers do not),
so guessing would break more setups than it fixes. If in doubt, copy
the base URL verbatim from the provider's API documentation.
**Model ids are case-sensitive and gateway-specific.** The same
underlying model has different ids on different gateways (see the
table above), and ids must match the provider's catalogue exactly,
including capitalisation and hyphens.
**Self-signed gateways need Verify SSL off** — see the next section.
**Paste the API key clean** — quotes, `Bearer` prefixes and stray
whitespace are stripped automatically; placeholder text such as
`sk-...` is treated as "no key" and never stored (details in
*Updating an API key* below).
**Always press Test connection before Save and before setting a
default.** The test calls the exact URL + model id + key combination
and translates the provider's answer into an actionable message, so
every mistake above surfaces immediately instead of at the next
user question. The test waits up to the model's own **Timeout** —
a busy gateway can take a minute or more for a one-word prompt, so
a red "did not answer in time" means *raise the Timeout*, not
"the endpoint is down". Every other red message names a setting to
fix.

Symptom → cause → fix:

**Symptom:** "The provider returned HTTP 405"
**Cause:** Base URL missing the API prefix (bare host, wrong path)
**Fix:** Use the full prefix, e.g. `https://nova.deloitte.com.cn/v1`

**Symptom:** "The provider could not find this model"
**Cause:** Model id wrong for this gateway (case, prefix, hyphens)
**Fix:** Copy the exact id from the provider's catalogue; re-test

**Symptom:** "The provider rejected the API key" (401/403/30014)
**Cause:** Key revoked/expired, or pasted with quotes/Bearer prefix
**Fix:** Re-issue or re-paste the bare key; placeholders never stored

**Symptom:** "The gateway's TLS certificate could not be verified"
**Cause:** Self-signed or untrusted cert (e.g. Nova, on-prem)
**Fix:** Uncheck **Verify SSL certificate** on that model's config

**Symptom:** "The TLS handshake with the gateway failed"
**Cause:** TLS version/cipher mismatch, or `http://` vs `https://`
**Fix:** Check the scheme and whether an internal proxy is expected

**Symptom:** "The gateway's host name could not be resolved"
**Cause:** DNS failure — URL typo, no VPN, or missing hosts entry
**Fix:** Check the URL character by character; test from server host

**Symptom:** "The provider closed the connection without a valid reply"
**Cause:** Host answered, but not with a chat-completions response
**Fix:** Base URL must be the full API prefix; confirm model id exists

**Symptom:** "The gateway refused the connection"
**Cause:** Nothing listening on that host/port, or a proxy blocks it
**Fix:** Check scheme and port; test from the server host

**Symptom:** "The provider did not answer in time"
**Cause:** Slow or queueing gateway; short timeout
**Fix:** Raise the model's **Timeout**, then retry — transient

**Symptom:** "The provider could not be reached"
**Cause:** Network path blocked; cause not otherwise classifiable
**Fix:** Check the base URL, network and any corporate proxy

> **How a test result is reported.** Every *completed* connection test
> answers **HTTP 200**. The verdict lives in the payload: `ok: true` for
> a green result, `ok: false`plus`error`(what went wrong),`hint`
> (what to do), `provider_error` (the gateway's own wording) and
> `category`/`retryable`.`retryable: true` marks a transient failure
> (timeout, rate limit, 5xx) where pressing **Test** again is
> worthwhile; `retryable: false` marks a configuration fault that will
> fail identically every time until it is fixed. HTTP 4xx is reserved
> for genuinely malformed requests — an unknown `config_id` (404) or a
> missing field (422) — never for a negative test verdict.

### Adding a new provider (no code change needed)

1. Open **Models & usage → Add model**.
2. Paste the three fields from the provider's API docs — any OpenAI-
   compatible base URL works.
3. If the gateway uses a self-signed TLS certificate, **uncheck**
   *Verify SSL certificate*. Default is ON.
4. Press **Test connection**. If it turns green, press **Save**.
5. Set as default if this is the primary gateway for the team.

### Self-signed gateways (Nova, on-prem proxies)

Gateways that present a self-signed TLS certificate — Deloitte Nova
( `https://nova.deloitte.com.cn/v1` ), internal enterprise proxies, some
on-prem deployments — require the **Verify SSL certificate** toggle
to be unchecked. Each model configuration (admin and per-user) carries
an individual toggle; it is stored per-model in the database so each
gateway can be configured independently.

From `.env` , the same flag can be set with any of these variables
(applied once at first startup, when the model table is empty):

```bash
NOVA_VERIFY_SSL=false      # canonical Nova-specific name
LLM_VERIFY_SSL=false       # generic
SKIP_SSL_VERIFY=1          # standalone trigger — presence alone means skip
```

### Updating an API key

1. Open **Edit model**; the key field is empty and shows
`stored: sk-s****tlhn — leave blank to keep it` . A blank field
   **keeps the current key** — you only paste a key when rotating it.
2. Paste the key exactly as the provider issued it. Surrounding quotes,
   a `Bearer` prefix, a line break or trailing spaces are removed
   automatically before the key is saved and sent, so a key copied out
   of an e-mail or spreadsheet still works. Placeholder text such as
`sk-...` is treated as "no key" and is never stored.
3. Click **Test connection** before saving. A rejection now reads
   **"The provider rejected the API key (code 30014 — Token is
   invalid.)"** with a hint underneath, and the provider's original
   wording under **Provider said:**. If the hint asks you to remove
   quotes, the key itself is fine and only the paste was dirty.

A pasted key that *looks* correct can still fail if the provider
revoked or expired it. **Test connection** distinguishes the two: a
normalised-but-wrong key is rejected the same way, while a dirty paste
of a good key now succeeds.

> **Users comparing answers with and without the ontology.** The yellow
> **◐ Compare: with / without ontology** toggle in the Ask bar is a
> demo aid: each question is answered twice — once grounded in the
> ontology (with its impact chain) and once by the same model given only
> the raw uploaded tables, with the ontology layer stripped (no entities,
> no metric formulas, no joins, no chain). Budget **two model calls per
> question** while it is on; the rules-engine side is free, so canonical
> questions still cost nothing. When no model is configured the grounded
> side still answers through the rules engine and the page states that the
> non-ontology side needs a model — that is expected, not an error. If
> users report the comparison halves look identical, check that a
> default model is enabled under Models & usage; without it only the
> rules side can run.
>
> **The global "Use ontology" ON / OFF switch.** Both the user portal and
> this console carry the same switch in the top bar. It is a **per-browser
> preference stored in localStorage** ( `onti.useOntology` ) — not a
> server-side role, licence or data permission, and it does not alter any
> stored data. ON is the default. While it is OFF the portal runs in
> text-only demo mode: KPI cards, graphs, performance charts and impact
> chains are replaced by an "Ontology is OFF" panel, Ask answers from the
> uploaded data tables with no ontology layer, and reports still generate but
> drop their graph, chart and analytics sections (the title is suffixed
> "— No ontology (text-only)"). In this console, Knowledge graph shows the
> same off panel and hides its Schema / Impact / Instance layers, while the
> governance lists — entities, relations, AI proposals — remain fully
> usable. If a user says "my dashboard has no numbers", first check whether
> the switch in their header reads **OFF**; a single click on **ON**
> restores every view.
>
> **Why some answers are instant and others take seconds.** A question
> that matches a canonical rule (Ontology governance → Rules) is
> answered deterministically with **no model call at all**; anything
> else waits for the AI model, which takes seconds. The built-in VNB /
> APE pack ships with 31 rules, which is why its demo questions feel
> instant; a freshly generated pack starts with fewer, so more
> questions go to the model. The Actuarial Lapse Experience pack now
> carries 18 rules including data-driven ones (**Data coverage** —
> lists every month in the data; **Monthly lapse experience** — the
> per-month lapse table; **Trend analysis**and**Lapse variance** —
> month-on-month comparison and movement drivers), and all adapt
> automatically to whatever dataset is activated. A second, trimmed
> copy of the lapse study (3 rules over the synthetic demo extract)
> exists for demo recordings; both versions stay available in the
> scenario selector. The remedy for slow topics is not a faster
> model but more rules: watch what users actually ask (token metering
> rows show latency per answer) and add rules for the frequent ones.
> Grounded AI answers are also much faster than before — the
> knowledge-base context sent to the model is now a compact profile per
> table (column profiles, full month coverage, per-month aggregates, a
> 3-row sample) instead of hundreds of raw rows.

## 6. Safety rails

* **Version snapshots** — Ontology governance → select a pack →
  **🕘 History**→**📸 Save snapshot**; restore any time (the restore
  itself auto-saves a `before-restore` snapshot). 20 snapshots per
  pack.
* **Import preview** — importing a pack JSON runs a dry-run first;
  blocking issues keep the Import button disabled, so a green preview
  never fails at commit time. Three file shapes are accepted
  ( `onti-pack/v1` exports, BP2 raw definitions, relationship-graph
  ontologies) and validation errors name the exact item and field.
  Field-by-field requirements: see
  [ONTOLOGY_JSON_REFERENCE.md](ONTOLOGY_JSON_REFERENCE.md).
* **Concurrent editing protection** — every pack has a version number
  (badge in the pack bar). If two administrators edit the same pack,
  the second save is rejected with a clear "modified by another
  admin" message instead of silently overwriting; refresh the page
  and re-apply the edit.
* **AI proposal evidence** — every element the AI proposes (Admin →
  **AI proposals**) carries a **rationale**plus a**Proposing
  against** panel showing the exact table, its columns and the sample
  rows the model saw. Read this evidence before you **Approve** or
  **Reject** — nothing is applied until you approve.
* **What a proposal changes** — each card spells it out in plain
  language ("What approving this will do — …"): an *entity* proposal
  adds an entity (and its attributes), a *relation / edge* proposal
  adds a relationship between two existing nodes, a *formula*
  proposal adds a metric formula. Approving never edits or removes
  existing ontology content and never changes application code; the
  raw `{…}` payload is kept under a collapsed **Technical payload**
  twisty for audit only.
* **Audit log** — every write operation (packs, elements, models,
  users, datasets, activations) is recorded with actor and detail.

## 7. Recommended routines

| When              | Do                                              |
| ----------------- | ----------------------------------------------- |
| New user ontology | Health check → review graph → decide activation |
| New model added   | Test connection; watch failures for a day       |
| Weekly            | Skim audit feed and token-by-user for anomalies |
| Before big edits  | Save a pack snapshot (History)                  |
| Monthly           | Review canonical questions; prune unused ones   |

## 8. FAQ

**Q:** A user says the AI answered wrongly.
Check the badge in their screenshot: blue = model answer (reference
only). If a rule should have matched instead, add or fix the rule
under Ontology governance → Rules, then run the Health check.

**Q:** The impact graph does not match what I edited in the schema.
They are two layers of the same ontology. Press **⟳ Sync impact graph
from ontology** in Knowledge graph.

**Q:** Can I see a user's API key?
No — keys are stored encrypted and only ever displayed masked, to
anyone, including administrators.

**Q:** Users complain some questions are instant and others slow.
Check the badge on the answer: a rules badge means a canonical rule
answered it instantly (no model call); a model badge means the AI was
called, which takes seconds. If the same kind of question keeps going
to the model, add a rule for it under Ontology governance → Rules.

**Q:** Do rules understand follow-up questions like "How does it
compare to the previous month?"?
Yes. When the current question names no month, the rules engine
inherits the most recent month mentioned earlier in the conversation
(recent session turns plus the user's four most recent stored answers
for the pack). Rules built on the `month_context`/`month_movement`

helpers — such as the lapse pack's *comparison* and *movement* rules —
then answer for that inherited month. This applies to the Ask view,
the Dashboard chat and the with/without ontology comparison alike.

**Q:** How do I give a new colleague access?
Users & audit → create the account (or enable self-registration) and
share the user portal URL: `http://onti.local:8010/app` after the
hosts mapping in the README, otherwise `http://localhost:8010/app` .
Never share a `localhost` or `onti.local` address with a colleague: those
names only resolve on the machine running the server. For remote access
use the deployment domain ( `https://…/app` ).

Every account is now an **e-mail account**, and sign-in is two-factor:
the colleague needs the password *and* a code delivered to that address,
so the deployment must have `RESEND_API_KEY` or `SMTP_HOST` set. When
creating an account in **Users & audit** you supply the address; the
username is derived from it automatically. The same password policy as
self-registration applies (≥ 10 characters, 3 of 4 character classes, no
reuse in 24 months), and the account is prompted to rotate every 90 days.
Legacy accounts without an address are asked to bind one on first
sign-in — the session is not issued until the code comes back.

### Two second factors

Every account picks **one** of two second factors after the password is
verified:

| Factor        | When to use                      | Config required                                  |
| ------------- | -------------------------------- | ------------------------------------------------ |
| E-mail OTP    | Default — works with any mailbox | `RESEND_API_KEY` or `SMTP_HOST`                  |
| Authenticator | No network, no e-mail, no delay  | `pyotp` + `qrcode` (already in requirements.txt) |

The Authenticator flow is `GET /api/auth/mfa/bind` (returns the QR SVG +
a short-lived `mfa_token`) → user scans the QR with Google / Microsoft /
1Password Authenticator → `POST /api/auth/mfa/bind/verify` with one fresh
code and the `mfa_token`. After that, sign-in on the account takes
`/api/auth/signin/mfa/verify` instead of the e-mail OTP endpoint. The
account page (`/account/password`) exposes a switch to enable or disable
MFA; disabling falls back to the e-mail OTP on the next sign-in.

### Shared team accounts

Four **shared workspace accounts** are seeded out of the box — no personal
e-mail bound at seed time, so the team picks its own second factor on
first sign-in. The seeder at `scripts/seed_preset_accounts.py` is
idempotent and safe to re-run after schema upgrades:

```bash
python scripts/seed_preset_accounts.py
```

| Username       | Purpose                                            |
| -------------- | ---------------------------------------------------|
| `dais_hk_life` | Deloitte AIS — Hong Kong Life Insurance Team       |
| `dais_hk_gi`   | Deloitte AIS — Hong Kong General Insurance Team    |
| `dais_ml_life` | Deloitte AIS — Mainland Life Insurance Team        |
| `dais_ml_gi`   | Deloitte AIS — Mainland General Insurance Team     |

**Initial password for all four:** `OntiDeloitte@2026`. Password policy
(10+ chars, ≥3/4 classes, no reuse in 24 months, 90-day rotation) applies
from day one. After the first sign-in each team chooses either the
Authenticator app MFA path (no network, recommended) or binds a shared
mailbox for the e-mail OTP.

Duplicate self-registrations against any of these usernames (or any
existing account) are rejected with a friendly HTTP 409 `"already registered"`
— the front-end surfaces "Use your existing account instead" instead of
a generic error.

Self-registration works on every host. On Vercel the function filesystem
is per-instance (`/api/auth/status`→`ephemeral: true`), so configure a
durable store — **Vercel Database Blob (new, recommended)** via
`BLOB_STORE_ID` (the `vercel-blob` SDK authenticates with Vercel's OIDC
automatically once the Blob store is linked to the project; no manual
token needed), or the legacy REST API via `BLOB_READ_WRITE_TOKEN`, or
your own HTTP endpoint via `ONTI_SNAPSHOT_URL` + `ONTI_SNAPSHOT_TOKEN`.
Every account is then saved as its own record
(`shared/accounts/<sha256-of-username>.json`) before the registration
returns; a cold start re-hydrates missing accounts, and a sign-in on an
instance that has never seen the account adopts the record on demand. A
session also survives instance switches because all instances share the
master key (`ONTI_SECRET_KEY`; when it is absent the key is derived
from the storage credential). Without a store, self-registration is
refused with `503` and the sign-in page hides the switch, so nobody is
promised an account that cannot survive. The Docker deployment keeps the
volume, so everything persists there (`ephemeral: false`) — use it
(README → Deploying with Docker) for a real multi-user workspace.
**Pre-provision accounts before deploying** when no store is configured.
Serverless instances then start from the committed `data/onti.db`:
create the accounts you want to hand out on a local or Docker instance,
then commit the database and redeploy. The bundled database ships only
the real accounts — the test suite deletes the throw-away logins it
creates, so `data/onti.db` never accumulates synthetic users.
On Vercel make sure **Deployment Protection → Vercel Authentication** is
disabled, otherwise only members of your Vercel team can open the app.
Mind the URL: `vercel.com/<team>/<project>/<url>` is a dashboard path that
always answers 404; the app lives on its own domain.

**Q:** A team (e.g. GI) wants to build an ontology demo — do they need
admin accounts?
No. **Building ontologies is a user-portal feature**: every route in
`routes/graph_studio.py` (sources, build, upload, infer, create-ontology,
history) requires only `auth.current_user` , so a regular account can
upload data, generate an ontology, ask questions and produce reports.
Admin rights are only for governance (accounts, oversight, models,
activation) — keep them for the project owner.

Recommended setup:

1. Keep self-registration off on shared deployments.
2. Create **one regular account per person** (or one shared demo account
   for a short demo) in **Users & audit**.
3. Share `<domain>/app`— not`/admin`.
4. On Vercel, accounts created at runtime need a durable store
   ( `BLOB_READ_WRITE_TOKEN` or `ONTI_SNAPSHOT_URL` ). With one, they are
   written per account and reachable from every instance. Without one,
   registration is refused ( `503` ); create accounts on a local or
   Docker instance, commit the updated `data/onti.db` , and push — the
   deployment then starts every instance with those accounts.

A demo account ( `giteam`/`GiDemo@2026`, role`user` ) ships in the
committed database for exactly this purpose; rename it or replace it with
per-person accounts before a long-lived rollout.
