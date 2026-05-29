# Health checks — one section per layer

The exact commands `verify-pipeline` runs, what passes, and how to read amber/red. **Read-only.** Nothing here mutates state — no restarts, no re-syncs, no `dbt run`. If a check is red, the skill stops at "here's what's wrong" and hands off to [`troubleshoot`](../../troubleshoot/SKILL.md).

All remote commands run over Tailscale SSH per the [remote-control model](../../../shared-references/remote-control-model.md): one `ssh deploy@<client>-mds "..."` per Bash call, stateless between calls. Add `-o ConnectTimeout=5` so a dead host fails fast instead of hanging the tool.

## Read the marker first

```bash
if [ ! -f .agentic-data-engineer.json ]; then
  echo "[abort] not a managed MDS deployment"
  exit 1
fi
```

Pull the values every check below substitutes in:

```bash
CLIENT=$(jq -r '.client' .agentic-data-engineer.json)
BQ_PROJECT=$(jq -r '.decisions.bq_project_id' .agentic-data-engineer.json)
GREEN=$(jq -r '.decisions.freshness_thresholds.green_hours // 26' .agentic-data-engineer.json)
AMBER=$(jq -r '.decisions.freshness_thresholds.amber_hours // 50' .agentic-data-engineer.json)
DBT_LOGS=$(jq -r '.decisions.dbt_logs_path // "/home/deploy/dbt/logs"' .agentic-data-engineer.json)
HAS_MCP=$(jq -r '.stack.mcp // false' .agentic-data-engineer.json)
```

**Traffic-light convention, used everywhere below:**

| Light | Meaning |
|---|---|
| green | within `green_hours` (default 26) / all `success` / reachable |
| amber | older than green but within `amber_hours` (default 50), or a soft warning (e.g. integrity drift, GA4 lag) |
| red | older than `amber_hours`, a `failed`/`error` state, or unreachable |

---

## 1. Tailscale reachability

The first gate. If the VPS is unreachable, every later check is unknowable, not green.

```bash
ssh -o ConnectTimeout=5 deploy@<client>-mds "tailscale status"
```

- **green** — command returns and the VPS line shows the tailnet; all expected nodes (VPS, on-prem host if any) are listed without `offline`.
- **amber** — VPS reachable but a *non-critical* node (e.g. the on-prem Windows host) shows `offline`. Runtime syncs from that source will fail next cycle, but the rest of the stack is fine. The on-prem-offline-after-reboot case is a known gotcha — see [common-failures](../../troubleshoot/references/common-failures.md).
- **red** — SSH times out or `tailscale status` errors. The VPS is down, its Tailscale daemon is down, or the laptop is off the tailnet. Stop the sweep here; mark every downstream layer "unknown (VPS unreachable)" and hand to `troubleshoot`.

---

## 2. Airbyte — last sync per connection

Airbyte's public API lives at `http://localhost:8000/api/public/v1/` **on the VPS** (not `/api/v1/`, which is internal — see [airbyte-install](../../create-mds/references/airbyte-install.md)). Reach it through the SSH session. OAuth2 token first, then job history.

Token (client_id/secret live in the secrets store, referenced by the marker — load them into the SSH env, never echo them):

```bash
ssh deploy@<client>-mds 'bash -s' <<'EOF'
set -euo pipefail
BASE=http://localhost:8000/api/public/v1
TOKEN=$(curl -s -X POST $BASE/applications/token \
  -H "Content-Type: application/json" \
  -d "{\"client_id\":\"$AIRBYTE_CLIENT_ID\",\"client_secret\":\"$AIRBYTE_CLIENT_SECRET\",\"grant_type\":\"client_credentials\"}" \
  | jq -r .access_token)

# List connections, then the most recent job for each
for CID in $(curl -s -H "Authorization: Bearer $TOKEN" "$BASE/connections" | jq -r '.data[].connectionId'); do
  curl -s -H "Authorization: Bearer $TOKEN" "$BASE/jobs?connectionId=$CID&limit=1" \
    | jq -r --arg cid "$CID" '.data[0] | "\($cid)\t\(.status)\t\(.jobType)\t\(.lastUpdatedAt)"'
done
EOF
```

`$AIRBYTE_CLIENT_ID` / `$AIRBYTE_CLIENT_SECRET` must be present in the remote shell. Inject them from the agent secrets store at call time; do not commit or print them.

Interpret per connection, using `lastUpdatedAt` of the most recent `succeeded` job vs now:

- **green** — most recent job `succeeded` and finished within `green_hours`.
- **amber** — most recent `succeeded` job is older than `green_hours` but within `amber_hours`, OR the latest job is still `running` (a long sync in progress is not a failure).
- **red** — latest job `failed` or `cancelled`, or no `succeeded` job within `amber_hours`. A 403 on the token call is itself red — usually a rotated `client_id/secret` the marker no longer matches (see common-failures).

> The default green/amber thresholds (26/50h) give a daily sync a full extra day of slack before it goes red — a single missed night is amber, two is red.

---

## 3. BigQuery raw freshness

Per raw dataset, read the newest `last_modified_time` from `__TABLES__`. Use the `bq` CLI from the laptop (gcloud authed in Phase 1); no SSH needed for BQ checks.

```bash
bq query --use_legacy_sql=false --format=prettyjson "
  SELECT table_id,
         TIMESTAMP_MILLIS(last_modified_time) AS last_mod,
         TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), TIMESTAMP_MILLIS(last_modified_time), HOUR) AS age_hours,
         row_count
  FROM \`${BQ_PROJECT}.<raw_dataset>.__TABLES__\`
  ORDER BY last_modified_time ASC
"
```

Read the **oldest** table per dataset (worst case sets the light):

- **green** — `age_hours <= green_hours` for every table.
- **amber** — oldest table between `green_hours` and `amber_hours`. For GA4 specifically, a missing *today's* table is **expected** (Google exports yesterday's data with a lag) — treat GA4's expected-latest as "yesterday", not "today", before deciding the light.
- **red** — oldest table older than `amber_hours`, or a raw table is missing entirely.

---

## 4. BigQuery raw-vs-staging integrity

Catch the silent regression where staging built from incomplete raw, or a join dropped rows. Compare row counts raw vs the matching `stg_` model.

```bash
bq query --use_legacy_sql=false "
  WITH raw AS (
    SELECT '<table>' AS name, COUNT(*) AS n FROM \`${BQ_PROJECT}.<raw_dataset>.<table>\`
  ),
  stg AS (
    SELECT '<table>' AS name, COUNT(*) AS n FROM \`${BQ_PROJECT}.<staging_dataset>.stg_<table>\`
  )
  SELECT raw.name,
         raw.n AS raw_rows,
         stg.n AS stg_rows,
         SAFE_DIVIDE(ABS(raw.n - stg.n), raw.n) AS drift
  FROM raw JOIN stg USING (name)
"
```

Threshold: `decisions.integrity_threshold` (default **0.5%** = `0.005`). Some drift is legitimate — staging may dedup or filter test rows — so this is a *warning*, not a hard gate.

- **green** — `drift <= integrity_threshold` for every pair.
- **amber** — any pair exceeds the threshold. Surface it as a warning with both counts; the operator decides whether it's expected dedup or a real loss.
- **red** — a `stg_` table has **zero** rows while raw is non-empty (staging definitely broke), or a `stg_` model is missing.

---

## 5. dbt run freshness

The last cron run wrote `target/run_results.json` and a timestamped log. Read both over SSH; don't re-run dbt.

```bash
# Parse the last run for any non-success node
ssh deploy@<client>-mds \
  "jq -r '.metadata.generated_at, (.results[] | select(.status != \"success\") | \"\(.status)\t\(.unique_id)\")' \
   /home/deploy/dbt/<project>/target/run_results.json"
```

`generated_at` is the run's finish time (UTC). The cron default is `0 11 * * *` (11:00 UTC) — see [dbt-cron-scheduling](../../create-mds/references/dbt-cron-scheduling.md).

- **green** — `generated_at` within `green_hours` AND the `jq` body printed nothing (every node `success`).
- **amber** — last run within `green_hours`/`amber_hours` but some nodes are `warn` (e.g. a `dbt test` warning), or the run is older than `green_hours` but younger than `amber_hours`.
- **red** — any node `error`/`fail`, or `run_results.json` older than `amber_hours`, or the file is absent.

Confirm against the day's log when a node is non-success:

```bash
ssh deploy@<client>-mds "tail -40 ${DBT_LOGS}/dbt_run_\$(date -u +%Y-%m-%d).log"
```

(`\$(date ...)` is escaped so it evaluates on the **VPS**, giving the VPS's UTC date — not the laptop's.) Expect `Completed successfully` and `PASS=N WARN=0 ERROR=0`.

---

## 6. MCP health (only if `stack.mcp == true`)

Skip entirely when the marker says no MCP. Two independent signals: the container, and the public endpoint.

Container (over SSH):

```bash
ssh deploy@<client>-mds "docker ps --filter name=mcp --format '{{.Names}}\t{{.Status}}'"
ssh deploy@<client>-mds "docker logs --tail 30 mcp"     # only if Status is not Up
```

Public HTTPS endpoint (from the laptop — claude.ai reaches it publicly, not over Tailscale):

```bash
MCP_URL=$(jq -r '.decisions.mcp_endpoint' .agentic-data-engineer.json)
curl -s -o /dev/null -w '%{http_code}\n' -I "$MCP_URL"
```

- **green** — container `Status` is `Up`, AND the endpoint returns **401 or 406** (auth-required: the server is alive and correctly refusing an unauthenticated request — see [phase-3-agentic-layer](../../create-mds/references/phase-3-agentic-layer.md) Step 6).
- **amber** — container `Up` but a TLS cert nearing expiry, or a write-tools-enabled server (`mcp_write_tools == true`) whose logs show a recent `_sync_to_origin` failure.
- **red** — container not `Up`/restarting, **502** from the endpoint (Traefik up, container down), or a connection timeout.

> A **200** from the bare `/mcp` is *not* what you want — the endpoint should demand auth. 401/406 is the healthy state.

---

## Full sweep — assemble the one-page report

Run the six checks in order, short-circuiting only at the Tailscale gate (step 1 red ⇒ downstream "unknown"). Collect each layer's light, the supporting timestamp/figure, and any warning string. Then render the report per [report-format](report-format.md).

Suggested order and what each contributes to the report:

| # | Layer | Feeds report field |
|---|---|---|
| 1 | Tailscale | reachability light; gates the rest |
| 2 | Airbyte | per-connection light + last-sync timestamp |
| 3 | BQ raw freshness | per-dataset light + oldest-table age |
| 4 | BQ integrity | drift warnings (raw vs staging) |
| 5 | dbt run | run light + `generated_at` + failing nodes |
| 6 | MCP | container + endpoint light (omit row if no MCP) |

Global verdict rule:

- **all green** → "Pipeline healthy."
- **any amber, no red** → "Pipeline healthy with warnings — <one-line summary>."
- **any red** → "Pipeline degraded — <layer> failing. Run troubleshoot." Do **not** propose or apply a fix here; that is `troubleshoot`'s job.

## Common gotchas

- **SSH hangs on a dead VPS.** Always `-o ConnectTimeout=5`. The public-IP fallback path is firewalled off after Phase 1 and will otherwise eat the full tool timeout.
- **`$(date)` evaluates on the laptop.** For the dbt log filename you want the VPS's UTC date — single-quote or escape `\$(date -u +%Y-%m-%d)` so it runs remotely.
- **GA4 "missing today" looks red but isn't.** Google exports yesterday's table with a lag. Anchor GA4 freshness to yesterday; mark amber (informational) at most.
- **Airbyte token expires in 180s.** Fetch it inside the same heredoc that uses it; don't cache it across Bash calls.
- **Integrity drift is a warning, not a failure.** Legit dedup/filtering in staging produces small drift. Only zero-vs-nonzero is a hard red.
- **MCP 200 on `/mcp` is wrong, not right.** The healthy unauthenticated response is 401/406.

## Marker state

`verify-pipeline` **reads** the marker and **never writes** it. The fields it consumes:

```jsonc
{
  "client": "<client>",
  "stack": { "mcp": true },
  "decisions": {
    "bq_project_id": "<client>-mds-prod",
    "freshness_thresholds": { "green_hours": 26, "amber_hours": 50 },
    "integrity_threshold": 0.005,
    "dbt_logs_path": "/home/deploy/dbt/logs",
    "mcp_endpoint": "https://mcp.<client-domain>.com/mcp",
    "mcp_write_tools": true
  }
}
```
