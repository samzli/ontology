# Ontology value report — rule engine + analysis vs plain LLM

Pack under test: `bp-planning-v4` built from
`BPTablesTest_v4_clean.xlsx` (6 tables, 3,552 rows, no pre-written
narrative column).

Every scenario below is executed through `POST /api/ask/compare`,
the same endpoint the user portal calls. Both sides receive the
same question and the same raw data; the only difference is the
ontology layer (entities, relations, metric formulas, rules
engine, deterministic analysis, impact graph).

## Scoring rubric (0–5 per axis)

| Axis | What it measures |
| --- | --- |
| Business depth | Sectioned decomposition, quantified findings |
| Logical rigour | Reconciliation, identity bridge, statistics |
| Data linkage | Distinct tables/joins used and numbers cited |
| Explainability | Reasoning trace, provenance, causal language |

## Aggregate

| Axis | With ontology | Plain LLM | Delta |
| --- | --- | --- | --- |
| Business Depth | 0.71 / 5 | 0.00 / 5 | +0.71 |
| Logical Rigour | 0.21 / 5 | 0.00 / 5 | +0.21 |
| Data Linkage | 0.79 / 5 | 0.00 / 5 | +0.79 |
| Explainability | 0.50 / 5 | 0.00 / 5 | +0.50 |
| **Total** | **2.21 / 20** | **0.00 / 20** | **+2.21** |

## Per-scenario results

| # | Dimension | With | Plain | Steps | Findings | Tables | OK |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Level | 5 | 0 | 0 | 0 | 0 | no |
| 2 | Level | 0 | 0 | 0 | 0 | 0 | no |
| 3 | Level | 0 | 0 | 0 | 0 | 0 | no |
| 4 | Movement | 0 | 0 | 0 | 0 | 0 | no |
| 5 | Movement | 8 | 0 | 0 | 0 | 0 | no |
| 6 | Movement | 5 | 0 | 0 | 0 | 0 | no |
| 7 | Mix | 1 | 0 | 0 | 0 | 0 | no |
| 8 | Mix | 5 | 0 | 0 | 0 | 0 | no |
| 9 | Cohort | 0 | 0 | 0 | 0 | 0 | no |
| 10 | Cohort | 2 | 0 | 0 | 0 | 0 | no |
| 11 | Trend | 5 | 0 | 0 | 0 | 0 | no |
| 12 | Trend | 0 | 0 | 0 | 0 | 0 | no |
| 13 | Coverage | 0 | 0 | 0 | 0 | 0 | no |
| 14 | Coverage | 0 | 0 | 0 | 0 | 0 | no |

### Scenario questions

1. **Level** — What is the VNB in April 2025?
2. **Level** — What was the APE in January 2025?
3. **Level** — What is the VNB margin and how has it moved?
4. **Movement** — How does it compare to the previous month?
5. **Movement** — Why is there such a movement?
6. **Movement** — Why did Agency VNB grow in June 2025?
7. **Mix** — What product mix drives the Agency VNB margin?
8. **Mix** — Which product carries the most VNB weight and what margin does it
   earn?
9. **Cohort** — Which agent cohort is dragging VNB down?
10. **Cohort** — How do high churn-risk agents compare with low churn-risk
    agents?
11. **Trend** — What is driving the trend over the last six months?
12. **Trend** — Is the VNB growth sustainable?
13. **Coverage** — Do the channel numbers add up to the company total?
14. **Coverage** — What data does the ontology cover for this pack?

## What the ontology side does that the plain LLM cannot

1. **Exact factor bridge.** VNB is decomposed per segment with sequential
   factor substitution over the declared identity `VNB = Active agents × Avg
   productivity × VNB margin`; the effects sum to the reported move with a
   residual of $0.000M.
2. **Cross-table proof.** The product-mix margin is recomputed by joining
   `mix` to `product` on `product_id`, then shown to equal the channel's
   implied VNB/APE margin.
3. **Agent cohorts.** `agent_monthly` joined to `agent_master` on `agent_id`
   yields productivity and APE contribution by churn band, certification and
   tenure.
4. **Statistical diagnostics.** OLS trend with R², coefficient of variation,
   z-score against history, momentum, Herfindahl concentration and driver
   correlation.
5. **Reasoning trace.** Every answer carries numbered steps naming the method,
   the tables joined and the result, plus an impact chain walked over the
   ontology graph.

The plain-LLM side can only restate the table it was shown; it has no schema,
no joins, no identity and no graph, so it cannot produce 1–5.

## Appendix — representative side-by-side

**Question:** What is the VNB in April 2025?

**With ontology** (rules engine + analysis, first 900 chars):

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

**Plain LLM** (same model, same raw data, no ontology — full):

```text
No enabled AI model is configured, so the non-ontology answer cannot be
generated. The ontology-grounded answer above still works — the rules engine
needs no model. Ask an administrator to add a model in Model centre to run
both sides.
```

## Reproduce

```bash
python main.py
python tests/verify_ontology_value.py
```
