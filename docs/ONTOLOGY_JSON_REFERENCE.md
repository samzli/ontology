# Ontology JSON format reference

This page answers the question: *"I want to generate a JSON ontology
file myself — must it follow a particular format?"*

Short answer: **no single strict schema is forced on you.** The import
dialog accepts three shapes, auto-detected on upload. In every shape
only a handful of fields are required; everything else is optional
with sensible defaults, and unknown extra fields are ignored. This
page lists exactly what is required, what is optional, and what the
validator checks, so you can hand-write or generate a file with
confidence.

## Which shape will my file be detected as?

| Detection (first match wins)                                 | Shape               |
| ------------------------------------------------------------ | ------------------- |
| `format` field equals `onti-pack/v1`                         | onti-pack/v1 export |
| First entity is an object with `id` and no `key`             | BP2 raw ontology    |
| Entities are plain strings, or a `relationships` list exists | Relationship-graph  |
| Anything else (e.g. `key`-style entities, no marker)         | onti-pack/v1        |

Notes:

- A file with an **unknown `format` marker is still accepted** as long
  as it contains recognisable content (`entities`, `attributes`,
  `relations` / `relationships`, `graph_nodes`, `metric_formulas` or
  `question_rules`). The marker alone never blocks an import.
- Top-level must be a **JSON object** (not an array).

## Shared top-level fields (all shapes)

| Field         | Required | Default             | Notes                                                |
| ------------- | -------- | ------------------- | ---------------------------------------------------- |
| `slug`        | yes*     | derived from `id`   | 2-49 chars: lowercase letters, digits, `-`, `_` only |
| `name`        | no       | the slug, humanised | Display name in the pack bar                         |
| `description` | no       | `""`                | Short description shown in admin                     |
| `narrative`   | no       | `""`                | Long-form domain story used by the AI answer layer   |

\* A slug is required either in the file or via the **Slug override**
box in the import dialog. If both are given the override wins. A
duplicate slug is rejected with HTTP 409.

## Shape 1: onti-pack/v1 (native export format)

This is what **Export JSON** produces. Entities use `key`.

### entities

| Field         | Required | Default | Notes                                |
| ------------- | -------- | ------- | ------------------------------------ |
| `key`         | yes      | -       | Unique within the pack               |
| `label_en`    | yes      | -       | Display label                        |
| `layer`       | no       | `3`     | 1 = top concept, 2 = mid, 3 = detail |
| `description` | no       | `""`    |                                      |
| `sort`        | no       | `0`     | Ordering hint                        |

### attributes

| Field         | Required | Default  | Notes                             |
| ------------- | -------- | -------- | --------------------------------- |
| `entity_key`  | yes      | -        | Must match an imported entity key |
| `key`         | yes      | -        | Unique within the pack            |
| `label_en`    | yes      | -        |                                   |
| `data_type`   | no       | `STRING` | `STRING`, `INTEGER` or `FLOAT`    |
| `unit`        | no       | `""`     | e.g. `USD`, `%`                   |
| `enum_values` | no       | `""`     | Comma-separated allowed values    |
| `required`    | no       | `0`      | `1` marks a mandatory field       |
| `is_unique`   | no       | `0`      | `1` marks a natural key           |
| `description` | no       | `""`     |                                   |
| `sort`        | no       | `0`      |                                   |

### relations

| Field                         | Required | Default        | Notes                                                      |
| ----------------------------- | -------- | -------------- | ---------------------------------------------------------- |
| `key`                         | yes      | -              | Unique within the pack                                     |
| `source_key`                  | yes      | -              | Must match an entity key                                   |
| `target_key`                  | yes      | -              | Must match an entity key                                   |
| `label_en`                    | no       | `""`           | Verb phrase shown on graphs                                |
| `cardinality`                 | no       | `MANY_TO_MANY` | `ONE_TO_ONE`, `ONE_TO_MANY`, `MANY_TO_ONE`, `MANY_TO_MANY` |
| `source_attr` / `target_attr` | no       | `""`           | Join attribute keys                                        |
| `description`                 | no       | `""`           |                                                            |
| `sort`                        | no       | `0`            |                                                            |

### Optional blocks

- `graph_nodes` — impact-graph nodes. Required: `key`. Optional:
  `label`, `grp` (default `financial`), `value`, `description`,
  `aliases`, `is_core`, `sort`.
- `graph_edges` — required: `key`, `source_key`, `target_key`; source
  may be a comma-separated list. Optional: `edge_type` (default
  `STRUCTURAL`), `label`, `description`, `sort`. Every referenced node
  must exist in `graph_nodes`.
- `metric_formulas` — required: `key`, `expression` (safe sandbox
  DSL). Optional: `label`, `unit`, `fmt` (default `number`),
  `decimals` (default `2`), `sort`.
- `question_rules` — required: `key`, `template`. Optional:
  `category`, `patterns`, `require_groups`, `guards`, `evidence_nodes`,
  `path_steps`, `sort`.
- `tables` — required: `key` per table and per column. Optional:
  `label`, `description`, `sheet`, `columns`.
- `question_bank` — required: `question`.
- `analysis` — declarative analytics spec used by
  `modules/deep_analysis.py`; omit it and the engine infers roles from
  the ontology. Optional keys:

  | Key | Meaning |
  | --- | --- |
  | `metric_table` / `metric_column` | company-level metric series |
  | `metric_second_column` | base measure (APE), used for the ratio |
  | `metric_label` / `second_label` | display names |
  | `month_column` | month column on the metric table (`YYYY-MM` values) |
  | `driver_table` / `driver_month_column` | segment-level driver table |
  | `segment_column` / `segment_label_column` | segment id + display name |
  | `segment_metric` | per-segment measure reconciled to the total |
  | `identity` | `{target, factors:[{label, column, kind, scale}]}` — the multiplicative identity decomposed per segment |
  | `mix` | `{mix_table, product_table, weight_column, margin_column, product_key, channel}` — cross-table mix proof |
  | `agent` | `{monthly_table, master_table, productivity_column, ape_column, status_column, attributes, join_date_column}` — cohort analysis |
  | `dimensions` | list of dimension names quoted in the answer |

  The spec is stored in `app_settings['analysis:{pack_id}']` on import,
  returned by pack export, and deleted with the pack.

## Shape 2: BP2 raw ontology

Same structure but with `id`-style field names. The importer converts
it on the fly:

| BP2 field                         | Converted to  |
| --------------------------------- | ------------- |
| entity `id`                       | `key`         |
| attribute `id`                    | `key`         |
| attribute `owned_class`           | `entity_key`  |
| attribute `unique`                | `is_unique`   |
| relation `id`                     | `key`         |
| relation `source_class`           | `source_key`  |
| relation `target_class`           | `target_key`  |
| relation `source_class_attribute` | `source_attr` |
| relation `target_class_attribute` | `target_attr` |

`label_en`, `layer`, `description`, `data_type`, `cardinality` etc.
carry over unchanged. `label_en` defaults to the `id` if omitted.
Blocks such as `graph_nodes`, `graph_edges`, `metric_formulas`,
`question_rules` and `tables` pass through untouched. Missing `id`
fields raise an error naming the exact item, e.g.
`entity #3: missing 'id' (or 'key') field.`

## Shape 3: relationship-graph ontology

The most relaxed shape — handy when your JSON was produced by another
tool or an LLM. Entities are **plain name strings**; relationships use
`from` / `to` / `type`.

```json
{
  "id": "my_domain_demo",
  "description": "Minimal relationship-graph ontology",
  "entities": ["Product", "Channel", "Observation"],
  "deferred_entities": ["Agent"],
  "relationships": [
    {
      "type": "has_product",
      "from": "Observation",
      "to": "Product",
      "cardinality": "many_to_one",
      "keys": ["product"],
      "time_rule": "REPORT_MONTH"
    }
  ],
  "tables": {
    "experience": {
      "sheets": ["Monthly_Experience"],
      "fields": {
        "month": ["REPORT_MONTH", "REPORT_MTH"],
        "product": ["PLANGROUP"]
      }
    }
  },
  "definitions": { "A/E": "Actual-to-expected ratio" },
  "open_decisions": ["Which lapse definition is authoritative?"]
}
```

Field notes:

| Field               | Required | Notes                                                                                                                                                                                                              |
| ------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `id` (or `slug`)    | yes*     | Slugified into the pack slug                                                                                                                                                                                       |
| `entities`          | no       | Strings, or objects with `id` / `name`                                                                                                                                                                             |
| `deferred_entities` | no       | Imported with a "deferred" note                                                                                                                                                                                    |
| `relationships`     | no       | Required per item: `type`, `from`, `to`; optional `cardinality` (`many_to_one` etc., auto-uppercased), `keys` (join column list), `time_rule`, `rule` — the latter three are preserved in the relation description |
| `tables`            | no       | Dict of `{key: {sheets, fields}}`; `fields` maps logical names to physical column names and later powers automatic column mapping on data upload                                                                   |
| `definitions`       | no       | Merged into the pack narrative as a glossary                                                                                                                                                                       |
| `open_decisions`    | no       | Merged into the narrative                                                                                                                                                                                          |

Entity names, relationship types and table keys are slugified
(spaces/punctuation become `_`, letters lowercased).

## Minimal examples

Smallest valid onti-pack/v1 file:

```json
{
  "format": "onti-pack/v1",
  "slug": "tiny_demo",
  "name": "Tiny Demo",
  "entities": [{ "key": "policy", "label_en": "Policy" }]
}
```

Equivalent BP2 file:

```json
{
  "slug": "tiny_demo_bp2",
  "entities": [{ "id": "policy" }]
}
```

Equivalent relationship-graph file:

```json
{ "slug": "tiny_demo_graph", "entities": ["Policy"] }
```

## What the validator checks (and common errors)

| Check                                  | Error you will see                                    |
| -------------------------------------- | ----------------------------------------------------- |
| Top-level is a JSON object             | `Invalid pack file: expected a JSON object...`        |
| Slug present + matches the slug rules  | `Invalid pack slug.`                                  |
| Slug not already used                  | `Pack slug 'x' already exists.` (HTTP 409)            |
| Required fields per block (see tables) | `entity: missing fields ['label_en']`                 |
| Keys unique within the pack            | `... keys must be unique within the pack.` (HTTP 422) |
| Attributes reference existing entities | listed as a blocking issue in the preview             |
| Graph edges reference existing nodes   | `edge x references unknown nodes ['y']`               |

Tips:

1. **Preview first** — selecting a file in the import dialog runs a
   dry-run and shows counts, blocking issues (red) and warnings
   (amber) before anything is written. A green preview never fails at
   commit time.
2. **Fix-by-message** — validation errors name the exact item number
   and missing field, so a hand-edited file is easy to correct.
3. **Start minimal** — entities + relations alone already produce a
   working knowledge graph; attributes, tables and formulas can be
   added later in the admin console or via re-import (as a new slug).
4. **Re-importing an edited pack** — imports always create a new pack.
   To replace an existing one, delete it first or use a new slug.
