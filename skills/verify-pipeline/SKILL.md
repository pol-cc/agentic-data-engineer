---
name: verify-pipeline
description: "Run a full health check across the MDS pipeline: ingestion (dlt/Airbyte) load status, BigQuery freshness per source, ingest reconciliation (source-vs-destination row counts), dbt model freshness, MCP server health, and raw-vs-staging row count integrity. Invoke when the user wants to confirm the pipeline is healthy or asks 'is everything working?'"
---

# verify-pipeline

> **Status**: v0.9.0 — references written; read-only health check operational. **Ingest reconciliation is now a first-class layer** (source-vs-destination row counts, dlt `_dlt_loads` freshness, sequence/gap checks) — mandatory after every dlt load to catch the silent data gap a mis-set incremental cursor leaves without crashing.

## What this skill does

Runs deterministic checks across every layer of the MDS and produces a one-page report. Read-only — never modifies state. Safe to invoke at any time.

## Preflight

```bash
if [ ! -f .agentic-data-engineer.json ]; then
  echo "[abort] not a managed MDS deployment"
  exit 1
fi
```

## Checks performed

| Layer | Check | Pass criterion |
|---|---|---|
| **Tailscale** | `tailscale status` on the VPS via SSH | VPS reachable, all nodes online |
| **Ingestion** (dlt/Airbyte) | dlt `_dlt_loads` last-load status + age per source (or Airbyte `GET /jobs` when `stack.ingest == "airbyte"`) | Latest load completed within `freshness_thresholds.green_hours` (default 26h) |
| **BigQuery raw** | `__TABLES__` modification time per raw dataset | Updated within green_hours |
| **Ingest reconciliation** | Source-vs-destination row count per source; dlt `_dlt_loads` status; sequence/gap check on monotonic keys | Destination matches source within `reconciliation_tolerance` (default 0); no sequence gaps; latest `_dlt_loads.status = 0` |
| **BigQuery integrity** | Row count `raw.<table>` vs `staging.stg_<table>` | Difference within 0.5% (or configured threshold) |
| **dbt** | `target/run_results.json` from last cron run via SSH | All models `success`, run completed within green_hours |
| **MCP** (if configured) | `GET /health` on the MCP server endpoint | Returns 200 |

**Reconciliation is the ingest-layer check dbt tests don't cover.** dbt tests validate the *transform* (raw → staging → marts); reconciliation validates the *ingest* (source → raw). It is mandatory after every dlt load because dlt's failure mode is silent — a mis-set incremental cursor or broken paginator leaves a data gap without crashing, so freshness looks green while rows are missing. Only counting source against destination catches it. See [`references/health-checks.md`](references/health-checks.md) section 4.

## Output

A markdown report with: per-source traffic-light status, last successful sync timestamp, dbt model freshness, integrity warnings, and a one-line global verdict.

The skill **never auto-fixes**. If a check fails, it points the user at [`troubleshoot`](../troubleshoot/SKILL.md).

## References

- [`references/health-checks.md`](references/health-checks.md) — the exact command(s) per layer, pass criteria, amber/red interpretation, and the full-sweep procedure
- [`references/report-format.md`](references/report-format.md) — the markdown report template the skill emits, with filled examples
- [`../../shared-references/remote-control-model.md`](../../shared-references/remote-control-model.md) — how the agent reaches the VPS over Tailscale SSH
- [`../create-mds/references/airbyte-install.md`](../create-mds/references/airbyte-install.md) — for the API auth flow
- [`../../shared-references/ai-native-principles.md`](../../shared-references/ai-native-principles.md) — principle 6 on observability
