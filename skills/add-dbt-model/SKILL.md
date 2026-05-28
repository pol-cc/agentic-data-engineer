---
name: add-dbt-model
description: "Add a new dbt model (staging, intermediate, or marts) to an existing MDS deployment. Invoke when the user wants to transform raw data, build an analytics table, or expose a new metric in BigQuery."
---

# add-dbt-model

> **Status**: v0.2.0 — conventions and decision tree references written; the step-by-step playbook for invoking from an existing MDS is still skeletal.

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

Templates (still to be written):
- `templates/staging.sql.template`
- `templates/marts.sql.template`
