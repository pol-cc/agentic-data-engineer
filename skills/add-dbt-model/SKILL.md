---
name: add-dbt-model
description: "Add a new dbt model (staging, intermediate, or marts) to an existing MDS deployment. Invoke when the user wants to transform raw data, build an analytics table, or expose a new metric in BigQuery."
---

# add-dbt-model

> **Status**: v0.7.0 (pre-test) — conventions + templates complete, with **incremental-by-default for fact/event marts** on BigQuery (cost control). Conventions and decision-tree references written; copy-paste templates (staging, marts dimension, marts incremental fact, schema, sources) included plus the `incremental-and-cost.md` reference; the Phase A–D playbook (classify → write → run/verify → commit) is in place.

## What this skill does

Adds a new dbt model to the client's dbt project. Decides where the model belongs (staging / intermediate / marts) based on what the user describes, writes the SQL, adds tests, and runs it once to verify before committing.

## Preflight

```bash
if [ ! -f .agentic-data-engineer.json ]; then
  echo "[abort] not a managed MDS deployment"
  exit 1
fi

# Confirm dbt is configured in this stack
jq -e '.stack.transform == "dbt_vps"' .agentic-data-engineer.json > /dev/null || {
  echo "[abort] this MDS doesn't have dbt configured"
  echo "Phase 2 of create-mds adds dbt — run that first"
  exit 1
}
```

## Playbook outline

**Phase A — Classify the model**

Ask the user what they want to compute. Then decide:

| Layer | When |
|---|---|
| **staging** (`stg_<source>_<table>`) | One-to-one with a raw source table, applies cleanup (rename columns, cast types, filter junk rows). One staging model per raw table. |
| **intermediate** (`int_<concept>`) | Reusable logic that several marts will consume (e.g. `int_orders_with_customer`). Not exposed to end users. |
| **marts** (analytics-ready, plain names like `orders`, `revenue_monthly`) | The deliverable. End users and BI tools query these. |

See `references/staging-vs-marts.md` and `references/dbt-naming-conventions.md`.

**Materialization is a cost decision on BigQuery (bills by bytes scanned).** Default by layer: staging = `view`; dimensions / small reports = `table`; **fact/event marts = `incremental`** (partitioned + clustered, `insert_overwrite`). A full-refresh `table` on a growing fact re-scans all history every run — the surprise-bill risk this default prevents. See `references/incremental-and-cost.md`.

**Phase B — Write the model**

1. SSH to the VPS, locate the dbt project.
2. Create the SQL file in the right folder.
3. Write the `SELECT` with explicit column lists (never `SELECT *` in marts).
4. Add a `schema.yml` entry with tests (`not_null`, `unique` where applicable, `accepted_values` for known categories).

**Phase C — Run and verify**

1. `dbt run --select <model>` in the venv on the VPS.
2. `dbt test --select <model>`.
3. Spot-check the resulting table in BigQuery (`bq query` or `SELECT * LIMIT 10`).

**Phase D — Commit**

1. Push to the client repo.
2. Update marker history.

## References

Conventions (complete):
- [`references/dbt-naming-conventions.md`](references/dbt-naming-conventions.md) — file/model/column naming, SQL style, tests pattern, canonical model shapes
- [`references/staging-vs-marts.md`](references/staging-vs-marts.md) — decision tree for which layer a model belongs in
- [`references/incremental-and-cost.md`](references/incremental-and-cost.md) — BigQuery cost control: when to go incremental, partition/cluster choice, `maximum_bytes_billed`, the GCP budget alert backstop, and the light cross-db convention (prefer dbt macros, flatten structs early — no `adapter.dispatch` framework)

Templates (complete) — copy into the client's dbt project and fill the `<PLACEHOLDER>` markers:
- [`templates/staging.sql.template`](templates/staging.sql.template) — canonical staging model: `view` materialization, `source` + `renamed` CTEs, explicit casts, ingest-tool-agnostic load metadata (dlt's `_dlt_load_id` → `load_id`, join `_dlt_loads` for the timestamp; Airbyte legacy `_airbyte_extracted_at` → `loaded_at`), `where <pk> is not null`
- [`templates/marts.sql.template`](templates/marts.sql.template) — DIMENSION / small-report mart: `table` materialization, one CTE per `ref()` input, a `joined` CTE, explicit final select. Heavily commented on when to use `table` (dim) vs `incremental` (fact)
- [`templates/marts_incremental.sql.template`](templates/marts_incremental.sql.template) — FACT / event mart (BigQuery default): `incremental` + `insert_overwrite` + `partition_by` + `cluster_by` + `on_schema_change`, with the `is_incremental()` look-back guard
- [`templates/schema.yml.template`](templates/schema.yml.template) — model docs + tests (`not_null`/`unique` PK, `not_null` FK, `accepted_values` enum, optional monetary check) plus optional incremental-fact guards (partition `not_null`, recency, row-count)
- [`templates/sources.yml.template`](templates/sources.yml.template) — raw `sources:` declaration (database `<project>`, schema `raw_<source>`, freshness warn 26h / error 50h, `loaded_at_field`)
