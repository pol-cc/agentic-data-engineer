# Incremental models and BigQuery cost control

BigQuery bills by **bytes scanned**, not by rows or by time. That single fact drives every materialization decision in this skill. A query that re-reads a 50 GB fact table every day costs ~30x a query that reads only today's 1.5 GB partition — for the same result. This reference explains when to go incremental, how to partition and cluster, and how it ties into the deployment's cost guardrails.

## The default rule (memorize this)

| Layer | Materialization | Why |
|---|---|---|
| **staging** | `view` | Stores no data; cheap to rebuild. Reads cost only when a downstream model selects from it. |
| **dimensions** (`dim_*`) | `table` | Small, slowly-changing, fully recomputable. A full refresh is cheap because the table is small. |
| **small reports** (`<domain>_<grain>`) | `table` | Same — if the aggregate is a few thousand rows, recomputing it is trivial. |
| **facts / events** (`fact_*`) | `incremental` | Large, append-mostly, grows forever. Full-refresh `table` re-scans the entire history every run — the surprise-bill risk this whole change exists to prevent. |

> The agent-native default flipped facts from `table` to `incremental` precisely because an SMB on the BigQuery free tier (1 TB scanned/month) can blow through it with a single growing `fact_orders` rebuilt nightly. Incremental keeps the daily scan proportional to the daily data, not to all history.

## When to go incremental

Reach for `incremental` when **all** of these hold:

- The model is a **fact / event grain** — one row per thing that happened, append-mostly (rows are rarely updated after their event date).
- The table is **large or growing unbounded** — a full refresh scans more than you want to pay for, or is trending that way.
- Rows can be placed on a **timeline by a date column** — `order_date`, `event_date`, a `loaded_at` watermark. This is what lets you process "just the recent window".

Stay on `table` when the model is small and fully recomputable (dimensions, small aggregates). **Do not reach for incremental prematurely** — full-refresh tables are simpler and correct by default. Incremental adds operating complexity (look-back windows, late-arriving data, the occasional `--full-refresh`). Switch only when the cost or runtime is a real problem. This mirrors the guidance in [`staging-vs-marts.md`](staging-vs-marts.md) ("Don't reach for incremental immediately").

## The BigQuery-efficient incremental pattern

Use the `insert_overwrite` strategy with `partition_by`. This is the pattern in [`../templates/marts_incremental.sql.template`](../templates/marts_incremental.sql.template):

```sql
{{
    config(
        materialized='incremental',
        incremental_strategy='insert_overwrite',
        partition_by={'field': 'order_date', 'data_type': 'date'},
        cluster_by=['customer_id'],
        on_schema_change='append_new_columns'
    )
}}
```

Why this specific combination:

- **`insert_overwrite`** — on an incremental run, dbt replaces *whole partitions* that the new batch covers, rather than merging row by row. It is atomic and idempotent: re-running the same day overwrites the same partitions, so you never get duplicates. On BigQuery only the touched partitions are scanned and rewritten.
- **`partition_by` (date)** — physically splits the table by day. This is what makes `insert_overwrite` and the `is_incremental()` filter touch a small slice instead of the whole table. The partition field must be a `DATE` (or a timestamp/datetime with `'granularity': 'day'`).
- **`cluster_by`** — sorts rows within each partition by the columns people filter and join on most (e.g. `customer_id`). Cuts bytes scanned further on selective queries. Pick 1-4 columns, highest-cardinality / most-filtered first.
- **`on_schema_change='append_new_columns'`** — if an upstream column appears, add it to the table instead of failing the run (existing rows get NULL).

### The `is_incremental()` guard

```sql
{% if is_incremental() %}
where order_date >= (
    select date_sub(max(order_date), interval 3 day) from {{ this }}
)
{% endif %}
```

This block runs **only on incremental runs** (not the first build, not `--full-refresh`). It limits the rows pulled from upstream to a recent look-back window, which is what bounds the per-run bytes scanned. The look-back (3 days above) re-loads recent partitions so **late-arriving rows** land correctly; `insert_overwrite` makes re-loading a partition idempotent. Tune the window to your source's lateness — widen it if rows commonly arrive days late.

### Partition / cluster choice cheat-sheet

- **Partition column**: the date the event happened and the date people filter dashboards by. Usually `<event>_date`. Must never be NULL (NULL rows land in a phantom partition `insert_overwrite` can't manage — test it `not_null`, see [`../templates/schema.yml.template`](../templates/schema.yml.template)).
- **Cluster columns**: the dimension keys most queries filter or join on (`customer_id`, `store_id`). Skip clustering on free-text or near-unique columns.
- **Granularity**: day is the right default for SMB volumes. Only go monthly if a day's data is tiny *and* you have years of history (BigQuery caps a table at ~4000 partitions ≈ 10 years of daily).

## Cost guardrails this ties into

Incremental models are the per-model lever. They sit inside two deployment-wide guardrails set up earlier in the stack:

1. **GCP budget alert** — `create-mds` Phase 1 attaches billing to the project (see [`../../create-mds/references/bigquery-project-setup.md`](../../create-mds/references/bigquery-project-setup.md)). A billing budget + alert (e.g. warn at the client's monthly ceiling from the discovery step) is the backstop that emails before a runaway query becomes a runaway bill. Incremental models keep you well under that ceiling; the alert catches the day something goes wrong.
2. **`maximum_bytes_billed`** — the MCP query server caps every interactive query at `MAX_BYTES_BILLED` (default 2 GiB; see [`../../create-mds/references/mcp-bigquery-server-deploy.md`](../../create-mds/references/mcp-bigquery-server-deploy.md)). That protects *ad-hoc reads*. For dbt runs themselves you can set the same cap project-wide in `dbt_project.yml` or per-model config:

   ```yaml
   # dbt_project.yml — reject any model build that would scan more than 20 GB
   models:
     +maximum_bytes_billed: 21474836480   # 20 GiB, tune per deployment
   ```

   A build that would exceed the cap fails loudly instead of silently billing. Pair it with `dbt run --dry-run`-style review of `--select state:modified` on large models.

Together: incremental keeps the scan small, `maximum_bytes_billed` is the per-query circuit breaker, the budget alert is the account-level backstop.

## Cross-database convention: light, not a framework

The default warehouse is BigQuery, but the templates avoid gratuitous BigQuery-only syntax so a future migration (the agent's job, if it ever happens) stays cheap. This is a **light convention, not a mandatory framework**:

- **Prefer dbt's cross-db macros** over native BigQuery functions where a clean equivalent exists:
  - `{{ dbt.date_trunc('month', 'order_date') }}` instead of `date_trunc(order_date, MONTH)`
  - `{{ dbt.datediff('start_at', 'end_at', 'day') }}` instead of `date_diff(...)`
  - `{{ dbt.safe_cast('raw_col', api.Column.translate_type('numeric')) }}` for casts that shouldn't blow up on bad data
  - `dbt_utils` helpers (`dbt_utils.star`, `dbt_utils.union_relations`, `dbt_utils.recency`) for the common patterns.
- **Flatten nested structs / arrays early** — do the BigQuery-specific `unnest()` / struct navigation in the **staging** layer so marts work on flat, portable columns. Don't let nested BigQuery types leak into marts.

What we deliberately **do not** do:

- **No `adapter.dispatch` macro framework.** Writing per-adapter macro variants for 80 models to stay theoretically portable is overkill for an SMB stack that runs on one warehouse. In an agent-native stack, if the warehouse ever changes, the agent does the migration — mechanically, model by model — far cheaper than maintaining a dispatch layer forever. The convention above keeps 90% of the portability for ~0% of the ongoing cost; the dispatch framework buys the last 10% at high permanent cost. Skip it.

## Operating notes

- **First build is a full scan** — the very first `dbt run` of an incremental model reads all of upstream once (there's no `{{ this }}` yet). Expect that; it's a one-time cost.
- **`dbt run --full-refresh --select fact_orders`** rebuilds from scratch — use it after changing the partition column, the grain, or fixing historical data. It re-scans everything, so do it deliberately.
- **Schema changes** are handled by `on_schema_change='append_new_columns'` for additive changes; a removed/retyped column needs a `--full-refresh`.
- **Duplicates** on an incremental model almost always mean the look-back window re-loaded rows that the `insert_overwrite` partition boundary didn't fully cover — confirm the partition column is the same column the `where` filters on.
