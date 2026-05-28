# Staging vs intermediate vs marts — decision tree

Where does a new model belong? This is the most common question when invoking `add-dbt-model`. Use this decision tree.

## The three layers, in one paragraph each

**Staging** is the "polish raw" layer. One staging model per important raw table. Mechanical work: rename columns, cast types, drop garbage rows. No joins (with rare exceptions). No business logic. The output looks like the raw input, just cleaner. Materialized as **views** (cheap to rebuild because they don't store data).

**Intermediate** is the "build reusable joins" layer. Used when multiple downstream marts need the same join (e.g., orders joined with customer attributes, used by `fact_orders`, `revenue_monthly`, `cohort_retention` alike). Materialized as **tables** when the join is expensive; **views** when it's cheap. Never queried by humans directly — only by other models.

**Marts** are the "deliverable" layer. The tables humans and dashboards query. Business logic lives here: revenue calculations, cohort definitions, churn flags. Marts join staging and intermediate models freely. Materialized as **tables** (queryable fast, results stable through the day).

## Decision tree

Ask these questions in order. Stop at the first "yes".

```
1. Is the model a 1:1 cleanup of a raw source table (rename, cast, filter junk)?
   YES → staging        (models/staging/<source>/stg_<source>_<table>.sql)
   NO  → continue

2. Will the model be consumed by 2+ other models, and never by a human directly?
   YES → intermediate   (models/intermediate/int_<concept>.sql)
   NO  → continue

3. Is the model a deliverable — humans, dashboards, or external systems will query it?
   YES → continue with mart sub-decision below
   NO  → STOP. You may not need a new model. Could this be a CTE inside an existing model?
```

### Mart sub-decision

Within `marts/`, pick the right name pattern:

```
Is the model a dimension (slowly-changing entity attributes — customers, products, employees, stores)?
   YES → dim_<entity>.sql

Is the model a fact (one row per event — orders, page views, payroll runs, sensor readings)?
   YES → fact_<event>.sql

Is the model a summary or report (pre-aggregated for a specific report — revenue by month, cohort retention table)?
   YES → <domain>_<grain>.sql       (e.g. revenue_monthly, cohort_retention, customer_health_summary)

Otherwise:
   Use a descriptive name in snake_case without dim_/fact_/mart_ prefix.
```

## Common edge cases

### "It's a join but only one mart uses it"

Don't extract to intermediate. Keep the join inline in the mart's SQL as a CTE. Intermediate models are only worth it when the same logic appears in 2+ places — premature extraction creates a layer of indirection no one wants.

Move it to intermediate only when:
- A second mart actually needs the same join, OR
- The join is expensive enough that materializing it once saves significant query time, OR
- The join is the same across deployments (a reusable pattern), so it's worth standardizing.

### "It's pre-aggregated raw data"

If a raw table is already aggregated (e.g. `raw_google_ads.daily_campaign_stats`), the staging model is still a cleanup-only model. Do NOT do the aggregation here. Aggregation is mart business — `marts/ads_performance_daily.sql` consumes the staging view.

Reasoning: staging models stay opinion-free so they can be reused if you ever add a different "view" of the same source.

### "I want to denormalize for fast queries"

That's a mart concern. Denormalization belongs in marts (or intermediate if multiple marts share the denorm shape).

### "I have a slowly-changing dimension (SCD2)"

Use dbt's `snapshots/` directory, not `models/`. Snapshots are a different materialization with built-in version tracking. Outside scope of v0.1; covered in [future] `references/dbt-snapshots.md`.

### "I have an incremental model"

Both intermediate and mart layers can be incremental (`{{ config(materialized='incremental') }}`). Use incremental when:
- The model is rebuilt daily, AND
- A full rebuild takes longer than the data freshness window allows, AND
- New rows can be identified by a watermark column (created_at, loaded_at).

Don't reach for incremental immediately. Full-refresh tables are simpler and correct by default. Switch only when the daily run time becomes a real problem.

## Anti-patterns to refuse

When the user asks for a model that violates the layering, push back:

| User says | Refuse and propose |
|---|---|
| "Add a join in the staging model for X" | "Staging stays per-source. Let's create an intermediate model or do the join in the mart." |
| "Add business logic in the staging model" | "Staging should mirror raw structure. The business logic belongs in a mart." |
| "Create a mart that joins 8 staging models" | "Let's extract the joining into one or two intermediates first — easier to test and reuse." |
| "Query the raw table directly from a mart" | "Always go through staging. Even if it feels redundant today, you'll thank yourself when the raw schema shifts." |
| "Skip tests on this column" | "At minimum, every PK gets not_null + unique. Anything else is optional but tests are cheap." |

## Quick reference: the 5-line summary

```
Cleanup raw 1:1?              → staging
Reusable join for 2+ marts?   → intermediate
Humans query it?              → mart
   Entity attributes?           → dim_*
   Event rows?                  → fact_*
   Pre-aggregated report?       → <domain>_<grain>
```
