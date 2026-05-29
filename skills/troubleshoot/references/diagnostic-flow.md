# Diagnostic flow — the ordered walk

The walk `troubleshoot` follows when something is broken. **Earlier failures explain later ones**, so go in order and stop drilling once a step explains the symptom. The discipline throughout: **gather evidence → reason over it → PROPOSE a fix → get the user's confirmation before mutating anything.** This skill never auto-fixes.

All remote commands run over Tailscale SSH per the [remote-control model](../../../shared-references/remote-control-model.md): one `ssh deploy@<client>-mds "..."` per Bash call, stateless between calls, `-o ConnectTimeout=5` so a dead host fails fast.

## Read the marker first

```bash
if [ ! -f .agentic-data-engineer.json ]; then
  echo "[abort] not a managed MDS deployment"
  exit 1
fi
```

Pull the values the steps below substitute in:

```bash
CLIENT=$(jq -r '.client' .agentic-data-engineer.json)
BQ_PROJECT=$(jq -r '.decisions.bq_project_id' .agentic-data-engineer.json)
DBT_LOGS=$(jq -r '.decisions.dbt_logs_path // "/home/deploy/dbt/logs"' .agentic-data-engineer.json)
DBT_PROJECT_DIR=$(jq -r '.decisions.dbt_runner_script // "/home/deploy/dbt/run_dbt.sh"' .agentic-data-engineer.json)
HAS_MCP=$(jq -r '.stack.mcp // false' .agentic-data-engineer.json)
ONPREM=$(jq -r '.decisions.onprem_host // empty' .agentic-data-engineer.json)
```

If `verify-pipeline` already ran, start at the layer it flagged — but still confirm the steps **above** that layer are green, because a red lower layer is the more likely root cause.

---

## Step 1 — Tailscale reachability

Is the VPS reachable at all? Nothing downstream is knowable until this is green.

```bash
ssh -o ConnectTimeout=5 deploy@<client>-mds "tailscale status"
```

**What a failure implies:**

| Symptom | Implies |
|---|---|
| SSH times out / `Connection refused` | VPS down, Tailscale daemon down on the VPS, or laptop off the tailnet. Check the [admin console](https://login.tailscale.com/admin/machines) and the Hostinger panel. |
| SSH works, but an on-prem node shows `offline` | The VPS is fine; the on-prem source is unreachable. Runtime syncs from it will fail next cycle. See [common-failures: Tailscale on-prem offline](common-failures.md#tailscale-on-prem-host-offline). |
| `Permission denied (publickey)` | Wrong `-i ~/.ssh/<client>_vps`, or expecting Tailscale SSH but the VPS wasn't brought up with `--ssh`. |

If the VPS itself is unreachable, **stop the walk** — the fix is at the network layer (propose: power-cycle via Hostinger panel, or have the user run `tailscale up` on the box if they have console access). Don't guess at Airbyte/dbt state you can't read.

---

## Step 2 — VPS processes and resources

The VPS answers SSH — now is the machine healthy and are the daemons up?

```bash
ssh deploy@<client>-mds "uptime && free -h && df -h /"          # load, RAM, disk
# Default stack: the dlt + dbt systemd timers — are they enabled and did they last fire?
ssh deploy@<client>-mds "systemctl list-timers 'dlt-*' 'dbt-*' --all --no-pager"   # NEXT/LAST per timer
ssh deploy@<client>-mds "journalctl -u dlt-<source>.service -u dbt-run.service --since '2 days ago' --no-pager | tail -40"
ssh deploy@<client>-mds "docker ps --format '{{.Names}}\t{{.Status}}'"  # all containers (MCP, etc.)
# If stack.ingest == airbyte (alternative path): the Airbyte controller + the dbt cron daemon
ssh deploy@<client>-mds "abctl local status"                    # Airbyte controller — airbyte path only
ssh deploy@<client>-mds "systemctl is-active cron"              # cron daemon — only if dbt runs on cron
```

**What a failure implies:**

| Symptom | Implies |
|---|---|
| `df -h /` near 100% | Disk full — Docker logs, dlt/dbt logs (or Airbyte image pulls on that path) filled the disk. Common silent cause of later failures. |
| `free -h` shows little free, high swap | Memory pressure — a dlt full-refresh spike, or the [abctl OOM](common-failures.md#airbyte-abctl-killed-by-oom) story on the Airbyte path. |
| a `dlt-*`/`dbt-*` timer `LAST` far in the past, or a `dead`/`disabled` timer | The scheduled load/run isn't firing — see [systemd timer didn't fire](common-failures.md#systemd-timer-didnt-fire-dlt-load-never-ran). |
| a `dlt-*`/`dbt-*` service unit `failed` | The load/run crashed — read its journal in the relevant step below. |
| a container in `Restarting`/`Exited` | That service is crash-looping — read its logs in the relevant step below. |
| `abctl local status` components not `Running` (airbyte path) | Airbyte controller is down — restart needed (propose, don't run). |
| `cron` inactive (only when dbt runs on cron) | dbt will never run on schedule — on the default systemd-timer path, check the `dbt-*` timer above instead. |

Check the kernel OOM killer if memory looked tight:

```bash
ssh deploy@<client>-mds "sudo dmesg -T | grep -i 'killed process' | tail -5"
```

---

## Step 3 — Ingest jobs

**Default stack (dlt).** There is no ingest API — the load is the `dlt-<source>.service` driven by its timer (Step 2). Read its state and the dlt bookkeeping in BigQuery (Step 4, `_dlt_loads`); the silent-gap reconciliation is the load-correctness signal, not a job status board. Confirm the last run from the journal:

```bash
ssh deploy@<client>-mds "systemctl status dlt-<source>.timer dlt-<source>.service --no-pager"
ssh deploy@<client>-mds "journalctl -u dlt-<source>.service --since '2 days ago' --no-pager | tail -80"
```

A `failed` unit or a `LAST` far in the past routes to [systemd timer didn't fire](common-failures.md#systemd-timer-didnt-fire-dlt-load-never-ran) or [dlt partial load](common-failures.md#dlt-load-partial--_dlt_loads-shows-failed). Then go to Step 4 for the `_dlt_loads` status and the reconciliation.

**If `stack.ingest == airbyte` (alternative path):** Airbyte's public API is at `http://localhost:8000/api/public/v1/` **on the VPS** (not `/api/v1/`, the internal API — see [airbyte-install](../../create-mds/references/airbyte-install.md)). Token, then recent jobs.

```bash
ssh deploy@<client>-mds 'bash -s' <<'EOF'
set -euo pipefail
BASE=http://localhost:8000/api/public/v1
TOKEN=$(curl -s -X POST $BASE/applications/token \
  -H "Content-Type: application/json" \
  -d "{\"client_id\":\"$AIRBYTE_CLIENT_ID\",\"client_secret\":\"$AIRBYTE_CLIENT_SECRET\",\"grant_type\":\"client_credentials\"}" \
  | jq -r .access_token)
echo "token: ${TOKEN:0:12}..."

for CID in $(curl -s -H "Authorization: Bearer $TOKEN" "$BASE/connections" | jq -r '.data[].connectionId'); do
  curl -s -H "Authorization: Bearer $TOKEN" "$BASE/jobs?connectionId=$CID&limit=3" \
    | jq -r --arg cid "$CID" '.data[] | "\($cid)\t\(.status)\t\(.jobType)\t\(.startTime)\t\(.lastUpdatedAt)"'
done
EOF
```

`$AIRBYTE_CLIENT_ID` / `$AIRBYTE_CLIENT_SECRET` must be in the remote shell — inject from the agent secrets store at call time, never print or commit them.

**What a failure implies:**

| Symptom | Implies |
|---|---|
| token call returns **403/401** | Rotated `client_id/secret` the marker no longer matches. See [common-failures: Airbyte 403](common-failures.md#airbyte-api-403-after-credential-rotation). |
| token call **404** | Wrong path — you're on `/api/v1/`. Use `/api/public/v1/`. |
| `connection refused` on :8000 | Port not bound — `abctl` controller down (back to Step 2). |
| latest job `failed`/`cancelled` | Read the job's logs (UI tunnel, or the API job-detail endpoint) for the connector error — bad on-prem credentials, source unreachable, schema change. |
| latest job `running` for hours | A stuck/slow sync; not a failure yet, but flag it. |
| no jobs in 24h+ | The connection's schedule isn't firing, or it's disabled. |

If a sync from an on-prem source failed, re-confirm the runtime data path (this is Airbyte's path, **not** Claude's — distinct connection):

```bash
ssh deploy@<client>-mds "nc -zv <onprem-host> 1433"   # 3306 MySQL / 5432 Postgres
```

---

## Step 4 — BigQuery state

Is raw data landing, and is staging built from complete raw?

```bash
# Freshness per raw dataset
bq query --use_legacy_sql=false "
  SELECT table_id,
         TIMESTAMP_MILLIS(last_modified_time) AS last_mod,
         TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), TIMESTAMP_MILLIS(last_modified_time), HOUR) AS age_hours,
         row_count
  FROM \`${BQ_PROJECT}.<raw_dataset>.__TABLES__\`
  ORDER BY last_modified_time ASC
"
```

Recent job errors and bytes billed (catches quota/cost issues):

```bash
bq query --use_legacy_sql=false "
  SELECT job_id, user_email, state, error_result.reason AS err,
         total_bytes_processed, creation_time
  FROM \`${BQ_PROJECT}\`.\`region-EU\`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
  WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)
    AND (state != 'DONE' OR error_result IS NOT NULL)
  ORDER BY creation_time DESC
  LIMIT 20
"
```

(Use the project's actual region — `region-EU` or `region-US` per `decisions.bq_location`.)

dlt load state + a cheap completeness check (the dlt stack's silent-gap detector — freshness alone won't catch it):

```bash
# dlt bookkeeping: did the last load complete? (status 0 = ok)
bq query --use_legacy_sql=false "
  SELECT load_id, schema_name, status,
         TIMESTAMP_MILLIS(CAST(inserted_at AS INT64)) AS loaded_at
  FROM \`${BQ_PROJECT}.<raw_dataset>._dlt_loads\` ORDER BY inserted_at DESC LIMIT 5"

# Sequence-gap on a monotonic key — a gap IS a missing row (no source round-trip needed)
bq query --use_legacy_sql=false "
  SELECT (MAX(id)-MIN(id)+1)-COUNT(DISTINCT id) AS missing
  FROM \`${BQ_PROJECT}.<raw_dataset>.<table>\`"
```

For the full source-vs-destination reconciliation (and the incremental cursor-window comparison), use [verify-pipeline section 4](../../verify-pipeline/references/health-checks.md#4-ingest-reconciliation-the-dlt-gap-nobody-had) — `troubleshoot` reuses those queries; this step just routes to the right failure entry.

**What a failure implies:**

| Symptom | Implies |
|---|---|
| raw table fresh, `stg_` stale/empty | dbt didn't run or failed — drill into Step 5. |
| raw table stale | ingestion didn't deliver — back to Step 3 (dlt `_dlt_loads` / Airbyte jobs). |
| raw fresh but **fewer rows than the source** | the silent gap — a dlt cursor/paginator skipped rows without erroring. Reconcile source vs destination. See [silent data gap](common-failures.md#silent-data-gap-dlt-incremental-cursor-mis-set) and [reconciliation mismatch](common-failures.md#reconciliation-mismatch-source-vs-destination). |
| `_dlt_loads` latest `status != 0` | a dlt load aborted mid-write — partial package. See [dlt partial load](common-failures.md#dlt-load-partial--_dlt_loads-shows-failed). |
| `JOBS_BY_PROJECT` shows `quotaExceeded` | [BQ free-tier quota exceeded](common-failures.md#bigquery-free-tier-quota-exceeded). |
| GA4 today's table missing | Expected Google export lag — **not** an error. See [common-failures: GA4 export lag](common-failures.md#ga4-export-missing-todays-table). |
| `stg_` rows << raw rows | Possible race condition — staging ran mid-load. See [common-failures: dbt race](common-failures.md#dbt-staging-ran-before-the-dlt-load-finished-race-condition). |

---

## Step 5 — dbt last run

What did the last scheduled run do? On the default stack the `dbt-run.service` is driven by a systemd timer; on the Airbyte/cron alternative it's a cron entry. Read `run_results.json` and the day's log — don't re-run dbt as a diagnostic (that mutates the warehouse).

```bash
# Non-success nodes from the last run
ssh deploy@<client>-mds \
  "jq -r '.metadata.generated_at, (.results[] | select(.status != \"success\") | \"\(.status)\t\(.unique_id)\t\(.message)\")' \
   ${DBT_PROJECT_DIR%/run_dbt.sh}/<project>/target/run_results.json"

# The day's log (date evaluated on the VPS, UTC)
ssh deploy@<client>-mds "tail -60 ${DBT_LOGS}/dbt_run_\$(date -u +%Y-%m-%d).log"
```

The default schedule fires after the dlt load completes (the systemd timer's `OnCalendar=`); the Airbyte/cron alternative defaults to `0 11 * * *` (11:00 UTC) — see [dbt-cron-scheduling](../../create-mds/references/dbt-cron-scheduling.md).

**What a failure implies:**

| Symptom | Implies |
|---|---|
| `run_results.json` absent / log missing for today | The `dbt-run.timer` didn't fire (Step 2: timer `dead`/`disabled`), or — on the cron alternative — `cron` inactive; or the wrapper errored before writing. Check `journalctl -u dbt-run.service` (systemd) or `grep CRON /var/log/syslog` (cron). |
| node `error`: compilation / missing relation | A `stg_` model references a raw table the dlt load hasn't created (Airbyte on the alternative path) — likely the race condition or a failed load (Steps 3-4). |
| node `error`: `Permission denied` reading BQ key | Key file owner changed; wrapper runs as `deploy`. |
| `dbt test` `fail`/`warn` only | Data-quality issue, not a pipeline outage — surface but don't treat as down. |
| run started but never finished | A long run overlapped the next schedule, or the box was OOM-killed mid-run (Step 2). |

---

## Step 6 — MCP server (only if `stack.mcp == true`)

Skip when there's no MCP. Container, then the public endpoint.

```bash
ssh deploy@<client>-mds "docker ps --filter name=mcp --format '{{.Names}}\t{{.Status}}'"
ssh deploy@<client>-mds "docker logs --tail 50 mcp"

MCP_URL=$(jq -r '.decisions.mcp_endpoint' .agentic-data-engineer.json)
curl -s -o /dev/null -w '%{http_code}\n' -I "$MCP_URL"     # from the laptop (public path)
```

**What a failure implies:**

| Symptom | Implies |
|---|---|
| endpoint **502** | Traefik up, MCP container down/crash-looping — read `docker logs mcp`. |
| endpoint **timeout** | Traefik down, ports 80/443 closed, or DNS broke. |
| endpoint **200** on `/mcp` | Auth is mis-wired — the endpoint should demand auth (401/406), not serve openly. |
| logs: BQ `403`/auth error | `mcp-reader` service-account key missing or unmounted. |
| logs: `_sync_to_origin` / push failed (write tools) | The fine-grained PAT (`GITHUB_TOKEN`) expired/rotated. See `mcp_write_tools` in the marker and [phase-3-agentic-layer](../../create-mds/references/phase-3-agentic-layer.md). |
| TLS cert error | Let's Encrypt renewal failed — check Traefik ACME logs. |

---

## Reasoning and the propose-then-confirm contract

Once a step explains the symptom, **stop drilling** and reason out loud:

1. **Evidence** — the exact command output that points at the cause (quote the line, don't paraphrase).
2. **Root cause** — name it, and say which earlier step (if any) it traces back to.
3. **Proposed fix** — the **exact** command(s) you would run, copy-pasteable, with what each does.
4. **Confirmation question** — a yes/no. **Run nothing that changes state until the user says yes.**

Examples of the boundary:

- Reading `docker logs`, `run_results.json`, `__TABLES__`, `tailscale status`, `JOBS_BY_PROJECT` — **read-only, run freely.**
- `abctl local start`, `docker restart mcp`, `crontab -e`, re-running a sync, `tailscale up`, editing the marker — **mutating, propose first.**

When unsure whether a command mutates, treat it as mutating and ask.

## Common gotchas

- **SSH hangs on a dead VPS.** Always `-o ConnectTimeout=5`.
- **`$(date)` evaluates on the laptop.** Escape `\$(date -u +%Y-%m-%d)` so the dbt log filename resolves on the VPS in UTC.
- **Airbyte token expires in 180s.** Fetch and use it inside the same heredoc; don't carry it across Bash calls.
- **Don't re-run dbt to "see if it works."** That rebuilds tables and masks the original failure's state. Read `run_results.json` instead.
- **A red lower layer fakes a red upper layer.** A failed sync (Step 3) looks like a dbt error (Step 5). Always confirm the lower steps before blaming the upper one.
- **The runtime path is Airbyte's, not Claude's.** If `nc -zv <onprem>` from the VPS fails but Claude can SSH the VPS fine, the *data* path is broken, not the *management* path — different connections (see remote-control-model).

## Marker state

`troubleshoot` **reads** the marker to locate paths/endpoints. It does **not** write the marker as part of diagnosis. If a confirmed fix changes deployment state (e.g. rotated Airbyte credentials, a new cron schedule), the relevant `create-mds`/`add-source` step updates the marker — `troubleshoot` hands back to it rather than editing the marker itself.
