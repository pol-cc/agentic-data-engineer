---
name: troubleshoot
description: "Diagnose pipeline issues by reading logs and state across Airbyte, dbt, BigQuery, the VPS, and Tailscale. Invoke when verify-pipeline reports a failure, the user says 'something's broken', or a sync hasn't run."
---

# troubleshoot

> **Status**: v0.5.0 — references written; diagnostic playbook operational.

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

**2. VPS processes**

```bash
ssh ... "docker ps -a | grep airbyte"        # Airbyte containers
ssh ... "abctl local status"                  # Airbyte controller
ssh ... "systemctl status cron"               # cron daemon
ssh ... "tail -100 /root/dbt/logs/dbt_run_$(date -I).log"   # last dbt run
```

**3. Airbyte API**

```bash
# Get token, then list recent jobs
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

To be documented in `references/common-failures.md`:

- Tailscale on on-prem server marked offline (Windows reboot, service stopped)
- Airbyte `abctl` controller killed by OOM on small VPS
- Airbyte API 403 because the client_id/client_secret was rotated and the marker still references the old one
- BigQuery quota exceeded (free tier crossed)
- dbt failed because `stg_*` ran before the Airbyte sync completed (race condition — fix is reschedule)
- GA4 export missing today's table (Google delay, not an error)

## Output

A markdown summary: which layer failed, the evidence, the proposed fix, and a yes/no question for the user. Nothing is changed until the user confirms.

## References

- [`references/diagnostic-flow.md`](references/diagnostic-flow.md) — the ordered diagnostic walk (Tailscale → VPS → Airbyte → BQ → dbt → MCP) with exact SSH commands and the propose-then-confirm contract
- [`references/common-failures.md`](references/common-failures.md) — catalog of known failure modes (symptom → confirm → root cause → proposed fix → prevention)
- [`../../shared-references/remote-control-model.md`](../../shared-references/remote-control-model.md) — how the agent reaches the VPS and on-prem hosts over Tailscale SSH
- [`../../shared-references/ai-native-principles.md`](../../shared-references/ai-native-principles.md) — principle 6 on observability
