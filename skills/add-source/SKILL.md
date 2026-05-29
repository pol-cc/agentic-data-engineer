---
name: add-source
description: "Add a new data source (dlt pipeline, BigQuery native transfer, or on-prem database via Tailscale) to an existing Modern Data Stack. Invoke when the user wants to integrate a new SaaS, database, or Google service into the warehouse."
---

# add-source

> **Status**: v0.7.0 — dlt default; Airbyte/Singer documented escape.

## What this skill does

Extends an existing MDS deployment with a new data source. The **default ingestion tool is dlt** (data load tool) — a Python library the agent drives directly. Three lanes:

| Lane | When | How |
|---|---|---|
| **dlt pipeline** (default) | A SaaS API, or a cloud-hosted DB. Anything with an HTTP/REST API or a SQLAlchemy-reachable database. | Write a dlt pipeline script, `python load.py`, reconcile. See [`references/dlt-rest-api-source.md`](references/dlt-rest-api-source.md). |
| **BigQuery native transfer** | The source is a Google service with first-party BQ export (GA4, Google Ads, Search Console) | `bq` CLI / BigQuery Data Transfer Service — never dlt, never Airbyte. See [`references/bq-native-transfer.md`](references/bq-native-transfer.md). |
| **On-prem database via Tailscale** | A database physically inside the client's premises (SQL Server, MySQL, Postgres) | dlt `sql_database` source over the tailnet. See [`references/dlt-sql-database-source.md`](references/dlt-sql-database-source.md). |

**Escape hatch (not the default):** when a SaaS API is too gnarly for a dlt `rest_api` config — pathological pagination, undocumented auth, a connector someone else already maintains better — fall back to a **maintained connector** (Airbyte standalone or a Singer tap) for *that one source*. Per source, not as a platform. See [`references/airbyte-api-gotchas.md`](references/airbyte-api-gotchas.md).

### Why dlt is the default (the agent-native rationale)

- **Short feedback loop.** dlt is a Python library. The agent writes a pipeline script, runs `python load.py`, and gets a stack trace or a row count back **immediately** — same layer it acts on. Airbyte forces the agent to operate an opaque control plane (Temporal, workers, job polling, Docker log spelunking) — the worst possible loop for an agent.
- **Cattle, not pet.** dlt persists incremental state to the **destination warehouse** (`_dlt_pipeline_state`, `_dlt_loads`, `_dlt_version` tables in the dataset), not in a local DB on the box. Lose the VPS, and a fresh box restores its cursors *from the warehouse* — no data gaps. Airbyte keeps state on the box, which breaks disaster recovery. See [`references/dlt-state-and-reconstruction.md`](references/dlt-state-and-reconstruction.md).
- **Warehouse-agnostic.** dlt loads to BigQuery / DuckDB / Postgres / Snowflake by changing one config line — keeping the warehouse escape-hatch (principle 7) genuinely open.

### The non-negotiable: reconcile after every load

> **dlt's failure mode is insidious.** A mis-set incremental cursor or a wrong paginator does **not** crash — it silently leaves **DATA GAPS**. A pipeline that "succeeded" can be quietly dropping half the rows. This makes reconciliation **mandatory, not optional**: without it, dlt is *more* dangerous than Airbyte.

**Every new source REQUIRES a reconciliation check** after its first load and on every scheduled run: row-count source-vs-destination, freshness, and sequence-gap checks. The source is not "done" until reconciliation passes. See [`references/dlt-state-and-reconstruction.md`](references/dlt-state-and-reconstruction.md) and the [`templates/reconcile.py.template`](templates/reconcile.py.template).

## Preflight (always run first)

Read the marker:

```bash
if [ ! -f .agentic-data-engineer.json ]; then
  echo "[abort] this directory is not a managed MDS deployment"
  echo "run 'create-mds' first if you need a new stack"
  exit 1
fi
```

Confirm the source is not already configured by checking `.stack.sources` in the marker.

## Playbook outline

**Phase A — Decide the lane**

1. Ask the user what source to add.
2. Route it:
   - **Google service** (GA4, Google Ads, Search Console) → BQ native transfer. Stop; do not use dlt or Airbyte.
   - **On-prem database** behind the office NAT → dlt `sql_database` over Tailscale.
   - **Anything else** (SaaS API, cloud DB) → **dlt** (default). Only fall back to the Airbyte/Singer escape hatch if the API defeats a dlt `rest_api` config.

**Phase B — Build the dlt pipeline (default lane)**

1. `pip install "dlt[bigquery]"` (extra per destination: `dlt[duckdb]`, `dlt[postgres]`).
2. Write the pipeline from the template:
   - REST API → [`templates/dlt_rest_api_source.py.template`](templates/dlt_rest_api_source.py.template) (config-driven: `client`, `resources`, `paginator`, `incremental`, `write_disposition`).
   - On-prem DB → [`templates/dlt_sql_database_source.py.template`](templates/dlt_sql_database_source.py.template).
3. Put credentials in `.dlt/secrets.toml` or env vars (`DESTINATION__BIGQUERY__CREDENTIALS`, `SOURCES__...`). **Never commit secrets.**
4. Run it: `python load.py`. Read `load_info`; fix the stack trace; re-run. Land in `raw_<source>`.

**Phase C — Reconcile (mandatory, not optional)**

1. Run [`templates/reconcile.py.template`](templates/reconcile.py.template) against the new source: source count vs `SELECT COUNT(*)` in `raw_<source>`, freshness, sequence-gap.
2. Check `_dlt_loads` shows status `succeeded` for the load.
3. **Do not mark the source done until reconciliation passes.** A silent gap here becomes a wrong dashboard later.

**Phase D — Schedule + verify**

1. Add the `python load.py && python reconcile.py` invocation to the VPS cron (per `verify-pipeline` / orchestration conventions). The reconcile step gates the load: a failed reconcile must alert, not pass quietly.
2. Update the marker:

```jsonc
{
  "stack": {
    "sources": [...existing, "<new_source>"]
  },
  "history": [..., {"date": "...", "skill": "add-source", "source": "<new_source>", "outcome": "ok", "via": "dlt"}]
}
```

3. Commit the pipeline script + reconcile script to the client repo (principle 4). **Secrets stay out of Git.**

## References

- [`references/dlt-rest-api-source.md`](references/dlt-rest-api-source.md) — **default for SaaS APIs.** dlt `rest_api` source: config shape, auth types, paginators, incremental, `write_disposition`, worked example, the run+reconcile loop.
- [`references/dlt-sql-database-source.md`](references/dlt-sql-database-source.md) — **default for on-prem/cloud DBs.** dlt `sql_database` source over Tailscale: connection string with the tailnet hostname, backend, incremental, read-only DB user.
- [`references/dlt-state-and-reconstruction.md`](references/dlt-state-and-reconstruction.md) — where dlt state lives (`_dlt_*` tables + local working dir), why the VPS is reconstructible (cattle-not-pet), and the **mandatory** reconciliation checks with concrete SQL/Python.
- [`references/bq-native-transfer.md`](references/bq-native-transfer.md) — **the lane for Google services only** (GA4, Google Ads, Search Console). Native, no dlt, no Airbyte.
- [`references/on-prem-tailscale.md`](references/on-prem-tailscale.md) — Tailscale reachability + read-only DB user for an on-prem DB; pairs with the dlt `sql_database` source.
- [`references/airbyte-api-gotchas.md`](references/airbyte-api-gotchas.md) — **ESCAPE HATCH.** Maintained connector (Airbyte standalone / Singer tap) for the one SaaS source dlt can't tame. API facts still true if you run Airbyte.
- [`references/airbyte-connectors-catalog.md`](references/airbyte-connectors-catalog.md) — per-source auth + gotchas + sync-mode choice, useful when the escape hatch is in play.
- [`../../shared-references/remote-control-model.md`](../../shared-references/remote-control-model.md) — how the agent reaches the VPS over Tailscale SSH to run `python load.py` and the on-prem DB over the tailnet.

## Templates

- [`templates/dlt_rest_api_source.py.template`](templates/dlt_rest_api_source.py.template) — copy-paste REST API pipeline starter (`<PLACEHOLDER>` markers).
- [`templates/dlt_sql_database_source.py.template`](templates/dlt_sql_database_source.py.template) — copy-paste on-prem DB pipeline starter.
- [`templates/reconcile.py.template`](templates/reconcile.py.template) — the post-load reconciliation check. Run after **every** load.
