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

## MCP write-tools push failing (PAT expired)

- **Symptom** — write-tool calls (`append_to_section` / `replace_in_file`) error; MCP logs show `_sync_to_origin` or `git push` auth failures. Read queries still work.
- **Confirm**
  ```bash
  ssh deploy@<client>-mds "docker logs --tail 80 mcp | grep -iE 'push|sync_to_origin|denied|401|403'"
  jq -r '.decisions.mcp_write_tools' .agentic-data-engineer.json   # confirm write tools are enabled
  ```
- **Root cause** — the fine-grained PAT (`GITHUB_TOKEN`, `contents:write` on the client repo) expired or was rotated. PATs are rotated ~every 90 days per [phase-3-agentic-layer](../../create-mds/references/phase-3-agentic-layer.md).
- **Proposed fix** — the user generates a fresh fine-grained PAT (manual ceremony on GitHub), then propose updating the container env and recreating it:
  ```bash
  # After the user supplies the new PAT into the container .env (GITHUB_TOKEN):
  ssh deploy@<client>-mds "cd /home/deploy/mcp-server && docker compose up -d --force-recreate mcp"   # propose
  ```
- **Prevention** — calendar the PAT rotation; keep the token only in the container `.env` (never the repo); if write tools aren't needed, run read-only (omit `GITHUB_TOKEN`).

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
| Airbyte 403 | token call returns 403 | 3 |
| dbt race condition | dbt `generated_at` < sync time | 3 → 5 |
| BQ quota exceeded | `JOBS_BY_PROJECT` reason `quota` | 4 |
| GA4 missing today (non-error) | latest table = yesterday | 4 |
| dbt cron never fired | no log today | 5 |
| MCP 502 / down | endpoint 502, container exited | 6 |
| MCP PAT expired | logs: push denied | 6 |
