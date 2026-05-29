# Common failures — the catalog

One entry per known failure mode. Each follows the same template: **symptom → confirm (command) → root cause → proposed fix (command) → prevention.** The fix is always something you **propose and the user confirms** — `troubleshoot` never auto-mutates. Commands run over Tailscale SSH per the [remote-control model](../../../shared-references/remote-control-model.md).

Reach this catalog from the [diagnostic flow](diagnostic-flow.md), which tells you *which* entry the evidence points at.

---

## Tailscale on-prem host offline

- **Symptom** — Airbyte syncs from the on-prem source fail; the on-prem node shows `offline` in `tailscale status` / the admin console. The VPS itself is reachable.
- **Confirm**
  ```bash
  ssh deploy@<client>-mds "tailscale status | grep -i offline"
  ssh deploy@<client>-mds "nc -zv <onprem-host> 1433"   # times out when offline
  ```
- **Root cause** — Tailscale on the on-prem Windows box stopped, usually **after a Windows reboot** that didn't bring the service back, or the node key expired. Very common on PYME office servers.
- **Proposed fix** — bring Tailscale back up on the on-prem host. If it has a management path (Tailscale SSH or OpenSSH, options a/b in the [remote-control model on-prem section](../../../shared-references/remote-control-model.md#on-prem-host-control)), the agent can do it headlessly:
  ```bash
  ssh user@<onprem-host> 'powershell -Command "& \"C:\Program Files\Tailscale\tailscale.exe\" up"'
  ```
  If there is no management path yet (the realistic day-1 state, option c), print the line for the user to run **in PowerShell as Administrator** on the box:
  ```powershell
  & "C:\Program Files\Tailscale\tailscale.exe" up
  ```
- **Prevention** — set Tailscale to start at boot on the on-prem host, and enable a management path (`tailscale set --ssh` or OpenSSH Server) so future restarts are headless.

---

## Airbyte abctl killed by OOM

- **Symptom** — `abctl local status` shows components not `Running`; the Airbyte UI/API is unreachable on :8000; syncs stopped.
- **Confirm**
  ```bash
  ssh deploy@<client>-mds "free -h && abctl local status"
  ssh deploy@<client>-mds "sudo dmesg -T | grep -i 'killed process' | tail -5"   # OOM killer evidence
  ```
- **Root cause** — the VPS ran out of RAM and the kernel OOM-killer reaped an Airbyte/Kind pod. Typically a too-small plan (KVM 1 / 4 GB) or another process spiking memory. Airbyte OSS wants 8 GB.
- **Proposed fix** — restart the controller, then address the size if it recurs:
  ```bash
  ssh deploy@<client>-mds "abctl local start"   # propose; mutating
  ssh deploy@<client>-mds "abctl local status"  # confirm components Running
  ```
  If OOM recurs, propose upgrading the Hostinger plan to KVM 2 (8 GB) — a manual ceremony in the panel.
- **Prevention** — KVM 2 minimum (the [airbyte-install](../../create-mds/references/airbyte-install.md) preflight enforces 8 GB / 30 GB free); add swap as a cushion; the `airbyte.service` systemd unit auto-restarts after reboots.

---

## Airbyte API 403 after credential rotation

- **Symptom** — every Airbyte API call returns **403/401**; `verify-pipeline` marks Airbyte red on the token step. Syncs may still run (the scheduler uses internal auth) — only the API is locked out.
- **Confirm**
  ```bash
  ssh deploy@<client>-mds 'curl -s -o /dev/null -w "%{http_code}\n" -X POST \
    http://localhost:8000/api/public/v1/applications/token \
    -H "Content-Type: application/json" \
    -d "{\"client_id\":\"$AIRBYTE_CLIENT_ID\",\"client_secret\":\"$AIRBYTE_CLIENT_SECRET\",\"grant_type\":\"client_credentials\"}"'
  # 403/401 = bad credentials; 404 = wrong path (/api/v1/ instead of /api/public/v1/)
  ```
- **Root cause** — the `client_id`/`client_secret` were regenerated (e.g. `abctl local credentials` re-run, or a reinstall) and the agent's secrets store / marker reference still points at the old pair.
- **Proposed fix** — read the current credentials and update the secrets store the marker references:
  ```bash
  ssh deploy@<client>-mds "abctl local credentials"   # read-only; prints current Client-Id/Secret
  ```
  Then propose writing the new pair into `secrets/<client>-airbyte-credentials.json` (the path in `decisions.airbyte_client_id_ref`). The marker itself only holds the *reference*, so usually no marker edit is needed.
- **Prevention** — never re-run `abctl local credentials` casually; if you must rotate, update the secrets store in the same change. Confirm the path is `/api/public/v1/`, not the internal `/api/v1/`.

---

## Silent data gap (dlt incremental cursor mis-set)

- **Symptom** — the destination is **missing rows the source has**, but **nothing errored**: the dlt run reported success, `_dlt_loads` shows the load completed, freshness is green. The gap surfaces only as a reconciliation mismatch (`verify-pipeline` section 4b/4c) or a user noticing "where are last week's orders?". **This is dlt's signature failure mode** — a Python library that silently skips rather than crashing.
- **Confirm**
  ```bash
  # Sequence gap in the destination (cheapest proof — a gap IS a missing row)
  bq query --use_legacy_sql=false "
    SELECT MIN(id) AS lo, MAX(id) AS hi, COUNT(DISTINCT id) AS present,
           (MAX(id)-MIN(id)+1)-COUNT(DISTINCT id) AS missing
    FROM \`<project>.<raw_dataset>.<table>\`"
  # The cursor dlt actually used (stored in pipeline state)
  bq query --use_legacy_sql=false "
    SELECT * FROM \`<project>.<raw_dataset>._dlt_pipeline_state\` ORDER BY _dlt_load_id DESC LIMIT 1"
  ```
  Compare the destination window to the source for the same cursor range (see [verify-pipeline reconciliation](../../verify-pipeline/references/health-checks.md#4b-source-vs-destination-row-count--the-core-reconciliation)). `missing > 0` or `dest < source` confirms the gap.
- **Root cause** — the incremental cursor is set wrong, so dlt's high-watermark skips rows. The usual culprits: (a) cursor column **not monotonic** (a mutable `updated_at` that goes backwards, or ties at the same second that the `>` filter drops); (b) wrong cursor column entirely (chose `created_at` but the source backfills old rows); (c) `initial_value` set past existing data, so the first load never picked up history; (d) source timezone vs cursor timezone mismatch shifting the boundary. A paginator that stops early (next-page token misread) produces the same gap symptom — check that too.
- **Proposed fix** — backfill the gap, then correct the cursor. Backfill by re-loading the affected window with `write_disposition="merge"` (so re-loaded rows dedupe on the primary key, not duplicate), or for a bounded gap a one-off full refresh of the table:
  ```bash
  # Propose (mutating): re-run the dlt pipeline for the source with a corrected/earlier cursor.
  ssh deploy@<client>-mds 'bash -s' <<'EOF'
  set -euo pipefail
  cd ~/dlt && source .venv/bin/activate
  # edit the source's incremental(cursor_path=..., initial_value=...) to a safe earlier value first
  python pipelines/<source>_pipeline.py
  EOF
  ```
  Then re-run reconciliation to confirm `missing = 0`. Choosing a strictly-increasing cursor (an append-only `id` or an immutable `created_at`) is the durable fix; use `last_value_func` / a lag window if ties at the boundary are the cause. `# verify against the installed dlt version` for the exact incremental config keys.
- **Prevention** — **reconciliation after every load is the prevention** (`verify-pipeline` runs it; `add-source` runs it at first load). Prefer a monotonic cursor; add a small overlap (re-pull a safety margin and let `merge` dedupe) rather than a razor-edge `>` boundary; keep a sequence/gap test on any table with a monotonic key.

---

## dlt load partial / `_dlt_loads` shows failed

- **Symptom** — a dlt run died mid-write; the destination holds a *partial* package. `_dlt_loads` latest `status != 0` (or the latest `load_id` is in the data table but absent/incomplete in `_dlt_loads`). Often follows an OOM, a dropped source connection, or a BQ error mid-load.
- **Confirm**
  ```bash
  bq query --use_legacy_sql=false "
    SELECT load_id, schema_name, status,
           TIMESTAMP_MILLIS(CAST(inserted_at AS INT64)) AS loaded_at
    FROM \`<project>.<raw_dataset>._dlt_loads\` ORDER BY inserted_at DESC LIMIT 5"
  ssh deploy@<client>-mds "journalctl -u dlt-<source>.service --since '24 hours ago' --no-pager | tail -60"
  ```
  A non-`0` latest `status` confirms the package aborted. (`# verify against the installed dlt version` — older versions may encode status differently.)
- **Root cause** — the load was interrupted before dlt finalized the package: VPS OOM-killed the Python process, the source connection dropped, BigQuery rejected a batch (schema/quota), or the systemd unit was stopped mid-run. dlt's staging model means a partial package can leave the data table ahead of a completed `_dlt_loads` row.
- **Proposed fix** — dlt is designed to recover by **re-running**: a fresh run resumes/normalizes the pending package or supersedes it (incremental state means it won't double-count merge-keyed rows). Address the root cause first (free RAM, fix the source/BQ error), then:
  ```bash
  ssh deploy@<client>-mds 'bash -s' <<'EOF'
  set -euo pipefail
  cd ~/dlt && source .venv/bin/activate
  python pipelines/<source>_pipeline.py   # propose; mutating — re-runs the load
  EOF
  ```
  After it completes, confirm `_dlt_loads` latest `status = 0` and re-run reconciliation. If a partial package left orphan rows that `merge` won't reconcile, propose a full refresh of that one table.
- **Prevention** — give the dlt runner enough RAM (it's lighter than Airbyte, but a huge full-refresh still spikes); keep loads incremental so a re-run is cheap; the systemd unit's restart policy retries transient drops; reconciliation catches a partial that slipped through.

---

## Reconciliation mismatch source vs destination

- **Symptom** — `verify-pipeline` ingest reconciliation (section 4) is **red/amber**: source row count and destination row count disagree beyond `reconciliation_tolerance`. The load itself may look fine (status 0, fresh) — the mismatch is the *only* signal.
- **Confirm**
  ```bash
  # Destination
  bq query --use_legacy_sql=false "SELECT COUNT(*) FROM \`<project>.<raw_dataset>.<table>\`"
  # Source (SQL example over the tailnet from the VPS)
  ssh deploy@<client>-mds "sqlcmd -S <onprem-host> -d <db> -Q 'SET NOCOUNT ON; SELECT COUNT(*) FROM <schema>.<table>'"
  ```
  For an incremental table, compare the **same cursor window** on both sides, not lifetime totals (the destination keeps history the source may have purged — see [reconciliation 4b](../../verify-pipeline/references/health-checks.md#4b-source-vs-destination-row-count--the-core-reconciliation)).
- **Root cause** — decide by **sign**:
  - `dest < source` (rows missing) → an ingest gap: usually the [silent cursor gap](#silent-data-gap-dlt-incremental-cursor-mis-set) above, or a [partial load](#dlt-load-partial--_dlt_loads-shows-failed). The serious case.
  - `dest > source` (more rows in dest) → either legitimate (the source hard-deleted rows the warehouse retains — expected for an append/history table), or duplicates from a misconfigured `write_disposition` (`append` where `merge` was intended, so re-runs pile up).
  - tiny delta that flickers → rows mutating mid-count (source written while you counted); re-count and confirm it settles.
- **Proposed fix** — for `dest < source`, follow the cursor-gap or partial-load fix. For `dest > source` from duplicates, propose switching the resource to `write_disposition="merge"` with the right primary key and a dedupe/full-refresh of the affected table:
  ```bash
  # Inspect duplication before proposing a fix (read-only)
  bq query --use_legacy_sql=false "
    SELECT <pk>, COUNT(*) c FROM \`<project>.<raw_dataset>.<table>\`
    GROUP BY <pk> HAVING c > 1 ORDER BY c DESC LIMIT 20"
  ```
  For an expected `dest > source` (source purges, warehouse retains), it's **not a failure** — document it and set `reconciliation_tolerance` or anchor the check to the cursor window so it stops flagging.
- **Prevention** — pick `merge` + a stable primary key for any mutable source; reconcile on the cursor window for incremental tables; record known-divergent tables (purge-at-source) in the marker so reconciliation expects the gap.

---

## systemd timer didn't fire (dlt load never ran)

- **Symptom** — no new dlt load this cycle: `_dlt_loads` has no recent `load_id`, raw tables stale though the source has new data, reconciliation/freshness amber-to-red. The dlt-on-VPS stack runs loads from a **systemd timer** (not cron — see [create-mds dlt scheduling]).
- **Confirm**
  ```bash
  ssh deploy@<client>-mds "systemctl list-timers 'dlt-*' --all --no-pager"          # NEXT/LAST columns
  ssh deploy@<client>-mds "systemctl status dlt-<source>.timer dlt-<source>.service --no-pager"
  ssh deploy@<client>-mds "journalctl -u dlt-<source>.service --since '2 days ago' --no-pager | tail -80"
  ssh deploy@<client>-mds "timedatectl | grep -E 'Time zone|System clock'"          # TZ + sync
  ```
  `LAST` far in the past, a `disabled`/`dead` timer, or a `failed` service unit confirms it.
- **Root cause** — the timer is disabled or was never `enable --now`'d; the service unit `failed` (bad venv path, missing creds, the partial-load case above) and a non-`Restart=on-failure` policy left it stopped; an `OnCalendar=` expression that doesn't match the intended time (timer schedules are in the system TZ — confirm the VPS is UTC); or the box was down at the scheduled instant and `Persistent=true` wasn't set to catch up.
- **Proposed fix** — depending on what's confirmed (each mutating — propose first):
  ```bash
  ssh deploy@<client>-mds "sudo systemctl enable --now dlt-<source>.timer"           # timer was off
  ssh deploy@<client>-mds "sudo systemctl start dlt-<source>.service"                # run the load now to catch up
  ssh deploy@<client>-mds "sudo systemctl reset-failed dlt-<source>.service"         # clear a failed state, then start
  ```
  If `OnCalendar=` is wrong, propose editing the timer unit and `daemon-reload`. To recover this cycle's data, propose a manual `systemctl start` of the service (or the pipeline script directly), then re-run reconciliation.
- **Prevention** — `Persistent=true` on the timer so a missed window runs at next boot; `Restart=on-failure` on the service; the timer + service units are committed in the client repo (auditable, reinstallable); confirm the VPS TZ is UTC at setup. The systemd journal is the audit trail — `verify-pipeline`'s ingestion check reads `_dlt_loads`, which catches a missed fire as stale.

---

## BigQuery free-tier quota exceeded

- **Symptom** — dbt or MCP queries fail with `quotaExceeded` / `Quota exceeded`; new data stops appearing even though Airbyte succeeded.
- **Confirm**
  ```bash
  bq query --use_legacy_sql=false "
    SELECT job_id, error_result.reason AS reason, total_bytes_processed, creation_time
    FROM \`<project>\`.\`region-EU\`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
    WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 48 HOUR)
      AND error_result.reason LIKE '%quota%'
    ORDER BY creation_time DESC LIMIT 20
  "
  ```
- **Root cause** — the project crossed the BigQuery free-tier monthly limit (1 TB query / 10 GB storage), or a custom quota cap, often from a full-refresh sync or an unfiltered query scanning huge tables.
- **Proposed fix** — short term, wait for the monthly reset or raise the quota in the GCP console (billing decision — **ask the user**, it can cost money). Structurally, propose reducing scan volume:
  ```bash
  # Find the heaviest recent queries to target for optimization
  bq query --use_legacy_sql=false "
    SELECT job_id, total_bytes_processed/1e9 AS gb, query
    FROM \`<project>\`.\`region-EU\`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
    WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
    ORDER BY total_bytes_processed DESC LIMIT 10
  "
  ```
- **Prevention** — incremental dbt models instead of full rebuilds, partition + cluster large tables, set `MAX_BYTES_BILLED` on the MCP server (Phase 3), and avoid `SELECT *` on raw.

---

## dbt staging ran before Airbyte finished (race condition)

- **Symptom** — `stg_` tables built from partial data: row counts well below raw, or a `stg_` model errored on a relation Airbyte hadn't finished writing. Yesterday's report looks wrong.
- **Confirm**
  ```bash
  # Compare the dbt run finish time to the Airbyte sync finish time for the same source
  ssh deploy@<client>-mds "jq -r .metadata.generated_at <project>/target/run_results.json"
  # then compare against the connection's last job lastUpdatedAt from the Airbyte API (see diagnostic-flow Step 3)
  ```
  If dbt's `generated_at` is **before** the sync's `lastUpdatedAt`, staging built mid-sync.
- **Root cause** — the dbt cron fired while a sync was still running. The default `0 11 * * *` schedule exists precisely to give Airbyte (07:30 trigger) 3h+ of slack — see [dbt-cron-scheduling](../../create-mds/references/dbt-cron-scheduling.md). A race means a sync ran late or the schedule was moved too early.
- **Proposed fix** — re-run dbt now that the sync is complete (mutating — confirm first), and propose keeping/restoring the 11:00 UTC schedule:
  ```bash
  ssh deploy@<client>-mds "/home/deploy/dbt/run_dbt.sh"   # propose; rebuilds marts
  ```
  If the source genuinely finishes later, propose pushing the cron (e.g. `0 18 * * *`) rather than racing.
- **Prevention** — keep the 11:00 UTC default unless a source is provably slower; for tight coupling, consider an Airbyte webhook / `dbt source freshness` gate before `dbt run`.

---

## GA4 export missing today's table

- **Symptom** — the GA4 raw dataset has no table for *today*; a freshness check flags GA4 amber/red.
- **Confirm**
  ```bash
  bq query --use_legacy_sql=false "
    SELECT table_id FROM \`<project>.analytics_<ga4_property>.__TABLES__\`
    ORDER BY table_id DESC LIMIT 3
  "
  # Latest is events_<yesterday> or events_intraday_<today> — NOT a finalized events_<today>
  ```
- **Root cause** — **not a failure.** Google's GA4 BigQuery export creates *yesterday's* finalized table with a lag (typically completing 07:00–10:30 UTC); today's data only exists as `events_intraday_*` until finalized. Time-aware checks must anchor GA4 freshness to **yesterday**.
- **Proposed fix** — **none.** Explain to the user that GA4 is a day behind by design and confirm the latest table is yesterday's. Only escalate if *yesterday's* table is also missing past mid-morning UTC.
- **Prevention** — bake the day-behind expectation into freshness checks (verify-pipeline already treats GA4-today-missing as expected); don't schedule GA4-dependent marts to demand same-day data.

---

## MCP container down / 502

- **Symptom** — the public MCP endpoint returns **502**; claude.ai can't reach the connector.
- **Confirm**
  ```bash
  ssh deploy@<client>-mds "docker ps -a --filter name=mcp --format '{{.Names}}\t{{.Status}}'"
  ssh deploy@<client>-mds "docker logs --tail 50 mcp"
  curl -s -o /dev/null -w '%{http_code}\n' -I "$(jq -r .decisions.mcp_endpoint .agentic-data-engineer.json)"
  ```
- **Root cause** — the MCP container exited or is crash-looping (bad env var, missing BQ key mount, OOM), while Traefik is still up — so Traefik answers but has no backend (502).
- **Proposed fix** — read the logs to find why it died, then propose a restart:
  ```bash
  ssh deploy@<client>-mds "docker restart mcp"   # propose; mutating
  ssh deploy@<client>-mds "docker ps --filter name=mcp --format '{{.Status}}'"
  ```
  If the logs show a config error, fix the `.env`/mount and propose re-creating the container rather than a bare restart.
- **Prevention** — `restart: unless-stopped` in the compose file; the verify-pipeline MCP check catches a 502 early.

---

## MCP write-tools PR failing (PAT expired or wrong scope)

- **Symptom** — write-tool calls (`append_to_section` / `replace_in_file`) error; MCP logs show `_sync_to_origin`, branch-push, or `pr_open_failed` auth errors. Read queries still work. (Write tools are **off by default** — this only applies when `mcp_write_tools == true`. The tools open a PR, never push to `main`.)
- **Confirm**
  ```bash
  ssh deploy@<client>-mds "docker logs --tail 80 mcp | grep -iE 'push|sync_to_origin|pr_open_failed|denied|401|403|422'"
  jq -r '.decisions.mcp_write_tools' .agentic-data-engineer.json   # confirm write tools are enabled
  ```
- **Root cause** — the fine-grained PAT (`GITHUB_TOKEN`) expired, was rotated, or is missing a scope. The PR posture needs **both** `contents:write` (push the `claude-bot/edit-<ts>` branch) **and** `pull_requests:write` (open the PR). A 403 on push = missing/expired `contents:write`; a 403 on PR-open = missing `pull_requests:write`; a 422 = a PR for that branch already exists or the branch matched base. PATs rotate ~every 90 days per [phase-3-agentic-layer](../../create-mds/references/phase-3-agentic-layer.md).
- **Proposed fix** — the user generates a fresh fine-grained PAT with both scopes (manual ceremony on GitHub), then propose updating the container env and recreating it:
  ```bash
  # After the user supplies the new PAT into the container .env (GITHUB_TOKEN):
  ssh deploy@<client>-mds "cd /home/deploy/mcp-server && docker compose up -d --force-recreate mcp"   # propose
  ```
- **Prevention** — calendar the PAT rotation; grant exactly `contents:write` + `pull_requests:write` on the one repo (least privilege); keep the token only in the container `.env` (never the repo); if write tools aren't needed, run read-only (`MCP_WRITE_TOOLS=off`, the default — no PAT at all). See [mcp-github-writeback: Security posture](../../add-mcp-skill/references/mcp-github-writeback.md#security-posture).

---

## dbt cron never fired (no log today)

- **Symptom** — no `dbt_run_<today>.log`; `run_results.json` stale; marts a day stale though Airbyte is fresh.
- **Confirm**
  ```bash
  ssh deploy@<client>-mds "systemctl is-active cron && crontab -l | grep run_dbt"
  ssh deploy@<client>-mds "grep CRON /var/log/syslog | tail -10"
  ssh deploy@<client>-mds "timedatectl | grep 'Time zone'"   # confirm UTC; schedule is in system TZ
  ```
- **Root cause** — cron daemon inactive, the crontab entry missing/edited away, the wrapper not executable, or a TZ mismatch firing the job at an unexpected hour. See [dbt-cron-scheduling](../../create-mds/references/dbt-cron-scheduling.md) gotchas.
- **Proposed fix** — depending on what's confirmed, propose one of:
  ```bash
  ssh deploy@<client>-mds "sudo systemctl enable --now cron"          # daemon was down
  ssh deploy@<client>-mds "chmod +x /home/deploy/dbt/run_dbt.sh"      # not executable
  ssh deploy@<client>-mds "crontab /home/deploy/dbt/crontab.txt"      # entry missing — reinstall from repo copy
  ```
  And to recover today's marts, propose a manual `run_dbt.sh` run.
- **Prevention** — the crontab is committed as `dbt/crontab.txt` in the client repo (auditable, reinstallable); confirm the VPS TZ is UTC at setup.

---

## VPS disk full

- **Symptom** — assorted failures across layers: Airbyte sync errors, dbt write failures, container crashes — all downstream of a full root filesystem.
- **Confirm**
  ```bash
  ssh deploy@<client>-mds "df -h / && du -sh /var/lib/docker /home/deploy/dbt/logs 2>/dev/null | sort -h"
  ```
- **Root cause** — Docker image/layer accumulation, unrotated dbt logs, or Airbyte temp data filled `/`.
- **Proposed fix** — propose reclaiming space (each is mutating — confirm):
  ```bash
  ssh deploy@<client>-mds "docker system prune -f"                                   # unused images/containers
  ssh deploy@<client>-mds "find /home/deploy/dbt/logs -name 'dbt_run_*.log' -mtime +30 -delete"  # old logs
  ```
- **Prevention** — the weekly log-rotation cron from [dbt-cron-scheduling](../../create-mds/references/dbt-cron-scheduling.md); KVM 2's 100 GB disk; periodic `docker system prune` if image churn is high.

---

## Quick index

| Failure | First confirm | Diagnostic step |
|---|---|---|
| Tailscale on-prem offline | `tailscale status \| grep offline` | 1 |
| abctl OOM | `dmesg \| grep 'killed process'` | 2 |
| VPS disk full | `df -h /` | 2 |
| systemd timer didn't fire (dlt) | `systemctl list-timers 'dlt-*'` — LAST stale | 2 → 3 |
| Airbyte 403 | token call returns 403 | 3 |
| dlt partial load | `_dlt_loads` latest `status != 0` | 3 → 4 |
| Silent data gap (cursor mis-set) | sequence `missing > 0`, or dest < source | 4 |
| Reconciliation mismatch src vs dest | source/dest count delta beyond tolerance | 4 |
| dbt race condition | dbt `generated_at` < sync time | 3 → 5 |
| BQ quota exceeded | `JOBS_BY_PROJECT` reason `quota` | 4 |
| GA4 missing today (non-error) | latest table = yesterday | 4 |
| dbt cron never fired | no log today | 5 |
| MCP 502 / down | endpoint 502, container exited | 6 |
| MCP PAT expired | logs: branch push / PR-open denied | 6 |
