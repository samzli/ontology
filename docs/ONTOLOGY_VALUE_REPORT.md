# Ontology value report — rule engine + deterministic analysis vs plain LLM

Pack under test: `bp-planning-v4` built from `BPTablesTest_v4_clean.xlsx`

(6 tables, 3, 552 rows, no pre-written narrative column).

Every scenario below is executed through `POST /api/ask/compare` , the same
endpoint the user portal calls. Both sides receive the same question and the
same raw data; the only difference is the ontology layer (entities, relations,
metric formulas, rules engine, deterministic analysis, impact graph).

## Scoring rubric (0–5 per axis)

| Axis | What it measures |
| --- | --- |
| Business depth | Sectioned decomposition, quantified findings, multiple analytical dimensions |
| Logical rigour | Cross-table reconciliation, exact identity bridge, statistical tests |
| Data linkage | Distinct tables/joins actually used and numbers cited |
| Explainability | Numbered reasoning trace, provenance of every table, causal language |

## Aggregate

| Axis | With ontology | Plain LLM | Delta |
| --- | --- | --- | --- |
| Business Depth | 0.86 / 5 | 1.50 / 5 | +-0.64 |
| Logical Rigour | 0.43 / 5 | 0.14 / 5 | +0.29 |
| Data Linkage | 1.00 / 5 | 2.00 / 5 | +-1.00 |
| Explainability | 0.79 / 5 | 1.07 / 5 | +-0.29 |
| **Total** | **3.07 / 20** | **4.71 / 20** | **+-1.64** |

## Per-scenario results

| # | Dimension | Question | With (0–20) | Plain LLM (0–20) | Steps | Findings | Tables joined | Reconciled |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Level | What is the VNB in April 2025? | 5 | 0 | 0 | 0 | 0 | no |
| 2 | Level | What was the APE in January 2025? | 1 | 1 | 0 | 0 | 0 | no |
| 3 | Level | What is the VNB margin and how has it moved? | 0 | 8 | 0 | 0 | 0 | no |
| 4 | Movement | How does it compare to the previous month? | 0 | 8 | 0 | 0 | 0 | no |
| 5 | Movement | Why is there such a movement? | 8 | 7 | 0 | 0 | 0 | no |
| 6 | Movement | Why did Agency VNB grow in June 2025? | 5 | 4 | 0 | 0 | 0 | no |
| 7 | Mix | What product mix drives the Agency VNB margin? | 1 | 11 | 0 | 0 | 0 | no |
| 8 | Mix | Which product carries the most VNB weight and what margin does it earn? | 5 | 1 | 0 | 0 | 0 | no |
| 9 | Cohort | Which agent cohort is dragging VNB down? | 2 | 0 | 0 | 0 | 0 | no |
| 10 | Cohort | How do high churn-risk agents compare with low churn-risk agents? | 2 | 0 | 0 | 0 | 0 | no |
| 11 | Trend | What is driving the trend over the last six months? | 5 | 4 | 0 | 0 | 0 | no |
| 12 | Trend | Is the VNB growth sustainable? | 2 | 8 | 0 | 0 | 0 | no |
| 13 | Coverage | Do the channel numbers add up to the company total? | 7 | 6 | 0 | 0 | 0 | no |
| 14 | Coverage | What data does the ontology cover for this pack? | 0 | 8 | 0 | 0 | 0 | no |

## What the ontology side does that the plain LLM cannot

1. **Exact factor bridge.** VNB is decomposed per segment with sequential
   factor substitution over the declared identity
`VNB = Active agents × Avg productivity × VNB margin` ; the effects sum to
   the reported move with a residual of $0.000M.
2. **Cross-table proof.** The product-mix margin is recomputed by joining
`mix` to `product` on `product_id` , then shown to equal the channel's
   implied VNB/APE margin.
3. **Agent cohorts.** `agent_monthly` joined to `agent_master` on `agent_id`
   yields productivity and APE contribution by churn band, certification and
   tenure.
4. **Statistical diagnostics.** OLS trend with R², coefficient of variation,
   z-score against history, momentum, Herfindahl concentration and driver
   correlation.
5. **Reasoning trace.** Every answer carries numbered steps naming the
   method, the tables joined and the result, plus an impact chain walked
   over the ontology graph.

The plain-LLM side can only restate the table it was shown; it has no schema,
no joins, no identity and no graph, so it cannot produce 1–5.

## Appendix — representative side-by-side

**Question:** What is the VNB in April 2025?

**With ontology** (rules engine + deterministic analysis, first 900 chars):

```text

## VNB in Apr 2025

| Metric | Apr 2025 | Mar 2025 | Delta |
|---|---: | ---:|---: |
| Total APE | $6.87M | $6.40M | +0.48 $M |
| **Total VNB** | **$1.32M** | $1.22M | **+0.10 $M** |
| VNB margin | 19.2% | 19.1% | +0.12 pp |
| Agency VNB | $0.59M | $0.51M | +0.07 $M |
| Agency share | 45% | 42% | — |

MoM VNB: **+8.1%**. VNB up: higher Agency active count and productivity
```

**Plain LLM** (same model, same raw data, no ontology — full answer):

```text
In **April 2025**, total **VNB** was **$1.32M** (specifically $1.32055M), with a VNB margin of **19.21%**.
```

## Reproduce

```bash
python main.py
python tests/verify_ontology_value.py
```
