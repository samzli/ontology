# Ontology value report — rule engine + deterministic analysis vs plain LLM

Pack under test: `bp-planning-v4` built from `BPTablesTest_v4_clean.xlsx` (6 tables, 3,552 rows, no pre-written narrative column).

Every scenario below is executed through `POST /api/ask/compare`, the same endpoint the user portal calls. Both sides receive the same question and the same raw data; the only difference is the ontology layer (entities, relations, metric formulas, rules engine, deterministic analysis, impact graph).

## Scoring rubric (0–5 per axis)

| Axis | What it measures |
|---|---|
| Business depth | Sectioned decomposition, quantified findings, multiple analytical dimensions |
| Logical rigour | Cross-table reconciliation, exact identity bridge, statistical tests |
| Data linkage | Distinct tables/joins actually used and numbers cited |
| Explainability | Numbered reasoning trace, provenance of every table, causal language |

## Aggregate

| Axis | With ontology | Plain LLM | Delta |
|---|---|---|---|
| Business Depth | 4.14 / 5 | 2.14 / 5 | +2.00 |
| Logical Rigour | 5.00 / 5 | 0.43 / 5 | +4.57 |
| Data Linkage | 5.00 / 5 | 3.14 / 5 | +1.86 |
| Explainability | 5.00 / 5 | 1.00 / 5 | +4.00 |
| **Total** | **19.14 / 20** | **6.71 / 20** | **+12.43** |

## Per-scenario results

| # | Dimension | Question | With (0–20) | Plain LLM (0–20) | Steps | Findings | Tables joined | Reconciled |
|---|---|---|---|---|---|---|---|---|
| 1 | Level | What is the VNB in April 2025? | 19 | 1 | 6 | 13 | 4 | yes |
| 2 | Level | What was the APE in January 2025? | 19 | 8 | 5 | 9 | 4 | yes |
| 3 | Level | What is the VNB margin and how has it moved? | 19 | 8 | 5 | 9 | 4 | yes |
| 4 | Movement | How does it compare to the previous month? | 19 | 8 | 5 | 9 | 4 | yes |
| 5 | Movement | Why is there such a movement? | 20 | 10 | 5 | 9 | 4 | yes |
| 6 | Movement | Why did Agency VNB grow in June 2025? | 20 | 8 | 6 | 13 | 4 | yes |
| 7 | Mix | What product mix drives the Agency VNB margin? | 19 | 8 | 6 | 13 | 4 | yes |
| 8 | Mix | Which product carries the most VNB weight and what margin does it earn? | 19 | 4 | 6 | 13 | 4 | yes |
| 9 | Cohort | Which agent cohort is dragging VNB down? | 19 | 4 | 6 | 13 | 4 | yes |
| 10 | Cohort | How do high churn-risk agents compare with low churn-risk agents? | 19 | 1 | 6 | 13 | 4 | yes |
| 11 | Trend | What is driving the trend over the last six months? | 19 | 8 | 6 | 13 | 4 | yes |
| 12 | Trend | Is the VNB growth sustainable? | 19 | 8 | 6 | 13 | 4 | yes |
| 13 | Coverage | Do the channel numbers add up to the company total? | 19 | 10 | 6 | 13 | 4 | yes |
| 14 | Coverage | What data does the ontology cover for this pack? | 19 | 8 | 6 | 13 | 4 | yes |

## What the ontology side does that the plain LLM cannot

1. **Exact factor bridge.** VNB is decomposed per segment with sequential factor substitution over the declared identity `VNB = Active agents × Avg productivity × VNB margin`; the effects sum to the reported move with a residual of $0.000M.
2. **Cross-table proof.** The product-mix margin is recomputed by joining `mix` to `product` on `product_id`, then shown to equal the channel's implied VNB/APE margin.
3. **Agent cohorts.** `agent_monthly` joined to `agent_master` on `agent_id` yields productivity and APE contribution by churn band, certification and tenure.
4. **Statistical diagnostics.** OLS trend with R², coefficient of variation, z-score against history, momentum, Herfindahl concentration and driver correlation.
5. **Reasoning trace.** Every answer carries numbered steps naming the method, the tables joined and the result, plus an impact chain walked over the ontology graph.

The plain-LLM side can only restate the table it was shown; it has no schema, no joins, no identity and no graph, so it cannot produce 1–5.

## Appendix — representative side-by-side

**Question:** What is the VNB in April 2025?

**With ontology** (rules engine + deterministic analysis, first 900 chars):

```text
**VNB in Apr 2025: $1.32M on $6.87M APE (margin 19.2%).**

| Channel | Active agents | Avg productivity ($k) | APE ($M) | VNB ($M) | VNB margin |
|---|---|---|---|---|---|
| Agency | 365 | 9.00 | 3.285 | 0.589 | 17.9% |
| Banca | 178 | 13.00 | 2.314 | 0.504 | 21.8% |
| Brokerage | 70 | 18.20 | 1.274 | 0.227 | 17.8% |

**Where the number sits.** VNB is spread across 3 segments; **Agency** contributes $0.589M (44.6%), which is where any explanation has to start.

### Monthly trajectory

| Month | VNB | Change | Change % |
|---|---|---|---|
| Jan 2025 | $1.207M |  |  |
| Feb 2025 | $1.253M | +$0.046M | +3.8% |
| Mar 2025 | $1.221M | -$0.032M | -2.5% |
| Apr 2025 | $1.321M | +$0.099M | +8.1% |
| May 2025 | $1.281M | -$0.039M | -3.0% |
| Jun 2025 | $1.448M | +$0.167M | +13.0% |


### Why it moved — exact driver bridge

The ontology identity is applied per segment, so the effects add up to the
```

**Plain LLM** (same model, same raw data, no ontology — full answer):

```text
Based on the uploaded data (**VNB Monthly (company)** table, which is the authoritative source for company-level figures), the **VNB in April 2025 (2025-04) was $1.32M** (specifically $1.32055M), on total APE of $6.87M, giving a VNB margin of **19.21%**.

This is the total company number. If you are asking for VNB by channel for that month, I can pull those from the `channel_monthly` table, but the company total is $1.32055M.
```

## Reproduce

```bash
python main.py
python tests/verify_ontology_value.py
```
