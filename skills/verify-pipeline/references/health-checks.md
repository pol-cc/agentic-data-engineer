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
INGEST=$(jq -r '.stack.ingest // "dlt"' .agentic-data-engineer.json)   # "dlt" (default) | "airbyte"
RECON_TOL=$(jq -r '.decisions.reconciliation_tolerance // 0' .agentic-data-engineer.json)  # rows allowed to differ
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

## 2. Ingestion — last load per source

> **Default stack is dlt.** For the dlt stack, the per-source "last load" light comes from **`_dlt_loads` in the destination dataset** (status + age) — see section 4a, which doubles as both the load-freshness and reconciliation signal. Run 4a here and skip the Airbyte API block below. The Airbyte flow in this section applies only when `stack.ingest == "airbyte"`.

When `stack.ingest == "airbyte"`: Airbyte's public API lives at `http://localhost:8000/api/public/v1/` **on the VPS** (not `/api/v1/`, which is internal — see [airbyte-install](../../create-mds/references/airbyte-install.md)). Reach it through the SSH session. OAuth2 token first, then job history.

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

## 4. Ingest reconciliation (the dlt gap nobody had)

**This is the layer that covers the INGEST step itself** — did every row that exists at the source actually land in the destination? It is distinct from section 5 (raw-vs-staging integrity), which is a *transform-layer* check: section 5 trusts raw and compares it to staging; section 4 does not trust raw and compares it to the **source**.

Why this is mandatory under dlt. The default ingestion is [dlt](../../add-source/references/dlt-state-and-reconstruction.md) (a Python library), and its worst failure mode is **insidious**: a mis-set incremental cursor or a broken paginator does **not** crash — the pipeline reports success while silently **skipping rows**, leaving a data gap. `pipeline.run()` returns a clean `load_info`; `_dlt_loads` shows the load succeeded; freshness looks fine. The only way to catch it is to **count the source and count the destination and compare.** dbt tests cover the transform layer; nothing covers ingest until this check. Run it **after every load** — `verify-pipeline` does it as part of the sweep, and `add-source` runs it once at first load.

> When `stack.ingest == "airbyte"`, dlt-specific sub-checks (4a freshness via `_dlt_loads`) don't apply — anchor freshness to section 3 / the Airbyte job step (section 2) instead. The source-vs-destination row count (4b) and the sequence-gap check (4c) apply to **any** ingestion tool.

### 4a. dlt load state and freshness — `_dlt_loads`

dlt writes bookkeeping tables into the **destination dataset** alongside the data: `_dlt_loads` (one row per load package), `_dlt_pipeline_state`, `_dlt_version`. Read the last load per dataset — its status and its age:

```bash
bq query --use_legacy_sql=false --format=prettyjson "
  SELECT load_id,
         schema_name,
         status,                                            -- 0 = completed
         TIMESTAMP_MILLIS(CAST(inserted_at AS INT64)) AS loaded_at,
         TIMESTAMP_DIFF(CURRENT_TIMESTAMP(),
                        TIMESTAMP_MILLIS(CAST(inserted_at AS INT64)), HOUR) AS age_hours
  FROM \`${BQ_PROJECT}.<raw_dataset>._dlt_loads\`
  ORDER BY inserted_at DESC
  LIMIT 5
"
```

(`_dlt_loads.status`: `0` = the load package completed. A non-zero/missing latest status means the load aborted partway — see 4d. Column types vary by dlt version; `inserted_at` may already be a TIMESTAMP — `# verify against the installed dlt version`.)

- **green** — latest `status = 0` AND `age_hours <= green_hours`.
- **amber** — latest load older than `green_hours` but within `amber_hours`, or no new load this cycle while the schedule expected one.
- **red** — latest `status != 0` (load aborted), `_dlt_loads` absent (dlt never wrote to this dataset), or `age_hours > amber_hours`.

### 4b. Source-vs-destination row count — the core reconciliation

Per dlt source/table, count what's at the **source** and what's in the **destination**, and compare. The exact source count depends on the source type:

```bash
# Destination side (BigQuery) — count the landed table for the cursor window
bq query --use_legacy_sql=false "
  SELECT COUNT(*) AS dest_rows
  FROM \`${BQ_PROJECT}.<raw_dataset>.<table>\`
"
```

```bash
# Source side — examples by source type (read-only COUNT against the source).
# SQL source (SQL Server / Postgres / MySQL) over the tailnet, from the VPS:
ssh deploy@<client>-mds "sqlcmd -S <onprem-host> -d <db> -Q 'SET NOCOUNT ON; SELECT COUNT(*) FROM <schema>.<table>'"
# REST API source — the provider's count/total field, or a HEAD/meta endpoint:
ssh deploy@<client>-mds "curl -s -H \"Authorization: Bearer \$API_TOKEN\" '<api>/<resource>?per_page=1' | jq '.total // .meta.total_count'"
```

Compare for an **append/full** table: `dest_rows` should equal `source_rows` (within `reconciliation_tolerance`, default **0** — ingest should be exact, unlike the transform-layer drift in section 5).

For an **incremental** table you cannot compare lifetime totals (the destination accumulates history the source may have purged). Compare the **same cursor window** on both sides instead — count source rows with `WHERE <cursor_col> > <last_load_high_watermark>` and destination rows loaded in the last package:

```bash
# Source rows newer than the last successful cursor value (the dlt high-watermark)
ssh deploy@<client>-mds "sqlcmd -S <onprem-host> -d <db> -Q \
  \"SET NOCOUNT ON; SELECT COUNT(*) FROM <schema>.<table> WHERE <cursor_col> > '<last_watermark>'\""
# Destination rows from the most recent load package (join data table to _dlt_loads by load_id)
bq query --use_legacy_sql=false "
  SELECT COUNT(*) AS loaded_this_run
  FROM \`${BQ_PROJECT}.<raw_dataset>.<table>\`
  WHERE _dlt_load_id = (
    SELECT load_id FROM \`${BQ_PROJECT}.<raw_dataset>._dlt_loads\`
    WHERE status = 0 ORDER BY inserted_at DESC LIMIT 1
  )
"
```

(Recover `<last_watermark>` from `_dlt_pipeline_state` — dlt stores the incremental cursor's last value there — or from the prior run's `load_info`. `# verify against the installed dlt version` for the state JSON shape.)

- **green** — `|source_rows - dest_rows| <= reconciliation_tolerance` (0 by default). The window reconciles exactly.
- **amber** — a small, explainable difference (e.g. a few rows the source mutated mid-count, or a known soft-delete). Surface both counts and the delta; a human confirms it's benign.
- **red** — `dest_rows < source_rows` beyond tolerance. **This is the silent-gap signal** — the destination is missing rows the source has. Most likely a mis-set incremental cursor or a paginator that stopped early. Do **not** dismiss it; hand to [troubleshoot: silent data gap](../../troubleshoot/references/common-failures.md#silent-data-gap-dlt-incremental-cursor-mis-set).

### 4c. Sequence / gap check — when there's a monotonic key

If the source table has a monotonic key (auto-increment `id`, a gapless invoice number, a daily date partition), a **gap in the sequence** in the destination is direct proof of dropped rows — no source round-trip needed:

```bash
# Integer/auto-increment key: count distinct vs (max - min + 1); any shortfall = gaps
bq query --use_legacy_sql=false "
  SELECT MIN(id) AS lo, MAX(id) AS hi,
         COUNT(DISTINCT id) AS present,
         (MAX(id) - MIN(id) + 1) - COUNT(DISTINCT id) AS missing
  FROM \`${BQ_PROJECT}.<raw_dataset>.<table>\`
"
```

```bash
# Date-partitioned source: list calendar days with no rows in the loaded range
bq query --use_legacy_sql=false "
  SELECT day
  FROM UNNEST(GENERATE_DATE_ARRAY(
         (SELECT MIN(DATE(<ts_col>)) FROM \`${BQ_PROJECT}.<raw_dataset>.<table>\`),
         (SELECT MAX(DATE(<ts_col>)) FROM \`${BQ_PROJECT}.<raw_dataset>.<table>\`))) AS day
  WHERE day NOT IN (SELECT DISTINCT DATE(<ts_col>) FROM \`${BQ_PROJECT}.<raw_dataset>.<table>\`)
  ORDER BY day
"
```

- **green** — `missing = 0` (integer key) / the date query returns no rows (no missing days).
- **amber** — a handful of gaps that map to a known cause (source soft-deletes, a genuine non-business day with zero events). Surface the gap list; a human confirms.
- **red** — gaps that line up with a load boundary or a cursor window — the smoking gun of an incremental-cursor gap. Hand to troubleshoot.

> Sequence checks are the cheapest reconciliation (one destination-only query, no source access) and the most damning when red — a gap *is* a missing row. Prefer 4c whenever the table has a usable monotonic key; fall back to the 4b count when it doesn't.

### 4d. Partial load — `_dlt_loads` shows an aborted package

If 4a flagged a non-zero latest `status`, the load died mid-write — the destination holds a *partial* package. Confirm and surface it; the fix is in troubleshoot:

```bash
bq query --use_legacy_sql=false "
  SELECT load_id, schema_name, status,
         TIMESTAMP_MILLIS(CAST(inserted_at AS INT64)) AS loaded_at
  FROM \`${BQ_PROJECT}.<raw_dataset>._dlt_loads\`
  ORDER BY inserted_at DESC LIMIT 3
"
```

A non-`0` latest status (or a `load_id` present in the data table but missing/incomplete in `_dlt_loads`) is **red** — see [troubleshoot: dlt partial load](../../troubleshoot/references/common-failures.md#dlt-load-partial--_dlt_loads-shows-failed).

---

## 5. BigQuery raw-vs-staging integrity

Catch the silent regression where staging built from incomplete raw, or a join dropped rows. Compare row counts raw vs the matching `stg_` model. **This trusts raw** (section 4 already reconciled raw against the source) and asks the narrower question: did the transform preserve the rows?

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

## 6. dbt run freshness

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

## 7. MCP health (only if `stack.mcp == true`)

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
- **amber** — container `Up` but a TLS cert nearing expiry, or a write-tools-enabled server (`mcp_write_tools == true`) whose logs show a recent `_sync_to_origin` / branch-push / PR-open failure (write tools are off by default — only check this when the marker says they're on).
- **red** — container not `Up`/restarting, **502** from the endpoint (Traefik up, container down), or a connection timeout.

> A **200** from the bare `/mcp` is *not* what you want — the endpoint should demand auth. 401/406 is the healthy state.

---

## Full sweep — assemble the one-page report

Run the checks in order, short-circuiting only at the Tailscale gate (step 1 red ⇒ downstream "unknown"). Collect each layer's light, the supporting timestamp/figure, and any warning string. Then render the report per [report-format](report-format.md).

Suggested order and what each contributes to the report:

| # | Layer | Feeds report field |
|---|---|---|
| 1 | Tailscale | reachability light; gates the rest |
| 2 | Ingestion (dlt/Airbyte) | per-source last-load light + timestamp |
| 3 | BQ raw freshness | per-dataset light + oldest-table age |
| 4 | Ingest reconciliation | source-vs-dest row delta, `_dlt_loads` status, sequence gaps |
| 5 | BQ integrity | drift warnings (raw vs staging) |
| 6 | dbt run | run light + `generated_at` + failing nodes |
| 7 | MCP | container + endpoint light (omit row if no MCP) |

Step 2 is the dlt **load** signal (`_dlt_loads` per source, section 4a) for the default dlt stack, or the Airbyte job step (section 2) when `stack.ingest == "airbyte"`. Step 4 (reconciliation) is the **mandatory** ingest-layer check — never skip it for the dlt stack; a green freshness with a red reconciliation is exactly the silent-gap case dlt is prone to.

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
- **A dlt load can succeed and still leave a gap.** `_dlt_loads.status = 0` and fresh data are NOT proof of completeness — a mis-set cursor skips rows silently. Only the source-vs-destination count (4b) or the sequence check (4c) catches it. Never treat freshness as a substitute for reconciliation.
- **Reconciliation tolerance is 0 by default, unlike integrity drift.** Ingest should be exact (every source row lands); transform may legitimately dedup. Don't copy the 0.5% integrity threshold onto reconciliation.
- **Incremental tables: compare the window, not the lifetime total.** The destination keeps history the source purged, so lifetime counts will diverge legitimately. Reconcile rows newer than the dlt high-watermark, not `COUNT(*)` vs `COUNT(*)`.
- **`_dlt_*` column types vary by dlt version.** `inserted_at`/`status` may be stored differently across releases — `# verify against the installed dlt version` before trusting the exact cast.
- **MCP 200 on `/mcp` is wrong, not right.** The healthy unauthenticated response is 401/406.

## Marker state

`verify-pipeline` **reads** the marker and **never writes** it. The fields it consumes:

```jsonc
{
  "client": "<client>",
  "stack": { "mcp": true, "ingest": "dlt" },     // "dlt" (default) | "airbyte"
  "decisions": {
    "bq_project_id": "<client>-mds-prod",
    "freshness_thresholds": { "green_hours": 26, "amber_hours": 50 },
    "integrity_threshold": 0.005,                // transform layer (raw vs staging)
    "reconciliation_tolerance": 0,               // ingest layer (source vs dest) — exact by default
    "dbt_logs_path": "/home/deploy/dbt/logs",
    "mcp_endpoint": "https://mcp.<client-domain>.com/mcp",
    "mcp_write_tools": false                     // OFF by default; only check the write path when true
  }
}
```
