---
name: troubleshoot
description: "Diagnose pipeline issues by reading logs and state across Airbyte, dbt, BigQuery, the VPS, and Tailscale. Invoke when verify-pipeline reports a failure, the user says 'something's broken', or a sync hasn't run."
---

# troubleshoot

> **Status**: v0.9.0 — references written; diagnostic playbook operational. Now covers the **dlt-era ingest failure modes** — the silent data gap (a mis-set incremental cursor that skips rows without crashing), a partial load (`_dlt_loads` status non-zero), a source-vs-destination reconciliation mismatch, and a systemd timer that didn't fire — alongside the original Airbyte/dbt/BQ/MCP modes.

## What this skill does

Walks through the most common failure modes of the MDS in order of likelihood, gathering evidence from each layer. The agent reasons over the evidence to identify the cause and proposes (but does not execute) a fix. The user confirms before any change is applied.

## Preflight

```bash
if [ ! -f .agentic-data-engineer.json ]; then
  echo "[abort] not a managed MDS deployment"
  exit 1
fi
```

## Standard diagnostic flow

Run checks in this order — earlier failures often explain later ones:

**1. Tailscale reachability**

```bash
ssh -i ~/.ssh/deploy_<client> root@<vps-tailscale-hostname> "tailscale status"
```

If unreachable: the VPS is offline, Tailscale on the VPS is down, or Tailscale on the laptop is down.

**2. VPS processes and timers**

```bash
ssh ... "uptime && free -h && df -h /"                         # load, RAM, disk
ssh ... "systemctl list-timers 'dlt-*' 'dbt-*' --all --no-pager"  # default: dlt + dbt timers (NEXT/LAST)
ssh ... "journalctl -u dlt-<source>.service -u dbt-run.service --since '2 days ago' --no-pager | tail -40"
ssh ... "docker ps --format '{{.Names}}\t{{.Status}}'"         # MCP, etc.
# If stack.ingest == airbyte (alternative path):
ssh ... "abctl local status"                                   # Airbyte controller — airbyte path only
ssh ... "systemctl is-active cron"                             # cron daemon — only if dbt runs on cron
```

**3. Ingest jobs**

Default (dlt): no ingest API — read the `dlt-<source>.service` journal (Step 2) and the `_dlt_loads` status + reconciliation in Step 4. If `stack.ingest == airbyte` (alternative path), get a token then list recent jobs:

```bash
# Airbyte path only — get token, then list recent jobs
curl ... /api/public/v1/jobs?limit=20
```

Look for: `failed` status, `cancelled`, or jobs that haven't started in 24h+.

**4. BigQuery state**

```bash
bq query --use_legacy_sql=false "
  SELECT table_name, TIMESTAMP_MILLIS(last_modified_time) AS last_mod
  FROM \`<project>.<dataset>.__TABLES__\`
  ORDER BY last_modified_time DESC
"
```

**5. dbt last run**

```bash
ssh ... "cat /root/dbt/<project>/target/run_results.json | jq '.results[] | select(.status != \"success\")'"
```

## Common failure modes

Catalogued in `references/common-failures.md`:

- **Silent data gap** — a dlt incremental cursor mis-set skips rows without erroring; only reconciliation (source vs destination) catches it. The dlt stack's signature failure.
- **dlt partial load** — `_dlt_loads` latest `status != 0`; the load died mid-write, leaving a partial package. Re-run to recover.
- **Reconciliation mismatch source vs destination** — counts disagree beyond tolerance; diagnose by sign (dest < source = gap; dest > source = duplicates or source purge).
- **systemd timer didn't fire** — the dlt load never ran (timer disabled, service failed, wrong `OnCalendar`, or no `Persistent=true`).
- Tailscale on on-prem server marked offline (Windows reboot, service stopped)
- Airbyte `abctl` controller killed by OOM on small VPS (when `stack.ingest == "airbyte"`)
- Airbyte API 403 because the client_id/client_secret was rotated and the marker still references the old one
- BigQuery quota exceeded (free tier crossed)
- dbt failed because `stg_*` ran before the ingest load completed (race condition — fix is reschedule)
- GA4 export missing today's table (Google delay, not an error)
- MCP write-tools PR failing (PAT expired or missing `pull_requests:write`) — write tools are off by default and open PRs, not pushes

## Output

A markdown summary: which layer failed, the evidence, the proposed fix, and a yes/no question for the user. Nothing is changed until the user confirms.

## References

- [`references/diagnostic-flow.md`](references/diagnostic-flow.md) — the ordered diagnostic walk (Tailscale → VPS/timers → ingest [dlt by default, Airbyte on the alternative path] → BQ → dbt → MCP) with exact SSH commands and the propose-then-confirm contract
- [`references/common-failures.md`](references/common-failures.md) — catalog of known failure modes (symptom → confirm → root cause → proposed fix → prevention)
- [`../../shared-references/remote-control-model.md`](../../shared-references/remote-control-model.md) — how the agent reaches the VPS and on-prem hosts over Tailscale SSH
- [`../../shared-references/ai-native-principles.md`](../../shared-references/ai-native-principles.md) — principle 6 on observability
