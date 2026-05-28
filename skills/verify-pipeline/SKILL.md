---
name: verify-pipeline
description: "Run a full health check across the MDS pipeline: Airbyte sync status, BigQuery freshness per source, dbt model freshness, MCP server health, and raw-vs-staging row count integrity. Invoke when the user wants to confirm the pipeline is healthy or asks 'is everything working?'"
---

# verify-pipeline

> **Status**: v0.0.1 — skeleton. Playbook in development.

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
| **Airbyte** | `GET /api/public/v1/jobs?status=succeeded` for each connection | Last successful sync within `freshness_thresholds.green_hours` (default 26h) |
| **BigQuery raw** | `__TABLES__` modification time per raw dataset | Updated within green_hours |
| **BigQuery integrity** | Row count `raw.<table>` vs `staging.stg_<table>` | Difference within 0.5% (or configured threshold) |
| **dbt** | `target/run_results.json` from last cron run via SSH | All models `success`, run completed within green_hours |
| **MCP** (if configured) | `GET /health` on the MCP server endpoint | Returns 200 |

## Output

A markdown report with: per-source traffic-light status, last successful sync timestamp, dbt model freshness, integrity warnings, and a one-line global verdict.

The skill **never auto-fixes**. If a check fails, it points the user at [`troubleshoot`](../troubleshoot/SKILL.md).

## References

- [`../create-mds/references/airbyte-install.md`](../create-mds/references/airbyte-install.md) — for the API auth flow
- [`../../shared-references/ai-native-principles.md`](../../shared-references/ai-native-principles.md) — principle 6 on observability
