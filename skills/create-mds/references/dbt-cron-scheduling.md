# dbt cron scheduling

> **ALTERNATIVE ORCHESTRATION (documented escape) — the default is a single linear script + systemd timers.** The default stack runs **one linear script** (`dlt load → dbt build → reconcile`) fired by a **systemd timer** ([`orchestration-systemd.md`](orchestration-systemd.md)). That design is more agent-native (`systemctl status` + `journalctl -u` beat a mute crontab for observability) and it **eliminates the race condition this file spends its first section guarding against** — because dlt and dbt run sequentially in one process, dbt can't start before ingestion finishes. **Use cron only when** you've inherited an existing crontab, the host has no systemd (a rare container/minimal base), or you deliberately schedule dbt independently of ingestion (e.g. the Airbyte alternative, where the two genuinely *are* separate jobs and the offset-tuning below applies). For the dlt default, **do not use cron — use the systemd timer.** The content below remains correct if you do run cron.

End state: a cron job on the VPS that runs `dbt run && dbt test` daily, writes timestamped logs to a discoverable path, and survives reboots. Total time: ~10 minutes.

## Picking the schedule time

The schedule is **the most operationally consequential decision** when dbt is scheduled *separately* from ingestion (the Airbyte-alternative path). If dbt runs before sources finish, downstream tables are built from incomplete raw data — a silent regression nobody notices until someone looks at yesterday's report. (The dlt default sidesteps this entirely; see the banner above.)

Reference timing (UTC) for the Airbyte/cron alternative path:

| Component | Default schedule | Typical finish |
|---|---|---|
| Airbyte (SQL Server, Factorial, etc.) | 07:30 UTC | 08:15 UTC for medium tenants |
| BQ Data Transfer (Google Ads) | Variable | Anywhere from 14:00 to 18:00 UTC |
| BQ Export (GA4) | Automatic | 07:00–10:30 UTC, creates *yesterday's* table |
| **dbt run** | **11:00 UTC** (recommended) | ~30 min depending on model count |

**Recommended for this alternative path: `0 11 * * *` (11:00 UTC every day).** This:

- Gives Airbyte 3h+ of slack after the 07:30 trigger
- Catches GA4 on most days
- Misses Google Ads on the day-of (so Ads marts are a day behind — usually acceptable)

If the client has Google Ads as a critical daily source, push to `0 18 * * *`. The trade-off: dbt runs once a day, so the marts are always one cycle behind the latest data.

## Preflight

```bash
ssh deploy@<client>-mds

# Confirm dbt project + profiles + debug pass
cd /home/deploy/dbt/<client>-mds
/home/deploy/dbt-env/bin/dbt debug > /dev/null
```

## Step A — Write the wrapper script

```bash
mkdir -p /home/deploy/dbt/logs

cat > /home/deploy/dbt/run_dbt.sh <<'EOF'
#!/usr/bin/env bash
# Wrapper invoked by cron. Activates the venv, runs dbt, captures a timestamped log.
# Exit code mirrors dbt's: 0 on success, non-zero on failure.

set -uo pipefail

PROJECT_DIR="/home/deploy/dbt/<client>-mds"
DBT_BIN="/home/deploy/dbt-env/bin/dbt"
LOG_DIR="/home/deploy/dbt/logs"
LOG_FILE="$LOG_DIR/dbt_run_$(date -u +%Y-%m-%d).log"

mkdir -p "$LOG_DIR"
cd "$PROJECT_DIR" || exit 1

echo "===== dbt run started at $(date -u +%Y-%m-%dT%H:%M:%SZ) =====" >> "$LOG_FILE"

# Run models, then tests. If run fails, skip tests (no point testing broken models).
"$DBT_BIN" run >> "$LOG_FILE" 2>&1
RUN_RC=$?

if [ $RUN_RC -eq 0 ]; then
  echo "===== dbt test started at $(date -u +%Y-%m-%dT%H:%M:%SZ) =====" >> "$LOG_FILE"
  "$DBT_BIN" test >> "$LOG_FILE" 2>&1
  TEST_RC=$?
else
  echo "===== dbt run failed (rc=$RUN_RC), skipping test =====" >> "$LOG_FILE"
  TEST_RC=0   # Don't double-count; the run failure is the headline
fi

echo "===== dbt finished at $(date -u +%Y-%m-%dT%H:%M:%SZ), run_rc=$RUN_RC test_rc=$TEST_RC =====" >> "$LOG_FILE"
exit $RUN_RC
EOF

# Replace placeholder
sed -i "s|<client>-mds|$ACTUAL_PROJECT_DIR_NAME|g" /home/deploy/dbt/run_dbt.sh

chmod +x /home/deploy/dbt/run_dbt.sh
```

## Step B — Manual test

Run the script once manually before scheduling:

```bash
/home/deploy/dbt/run_dbt.sh
echo "exit: $?"

# Inspect the log
LATEST_LOG=$(ls -t /home/deploy/dbt/logs/dbt_run_*.log | head -1)
echo "log: $LATEST_LOG"
tail -50 "$LATEST_LOG"
```

If exit is 0 and the log shows `Completed successfully`, proceed. If not, fix before scheduling.

## Step C — Install the crontab entry

```bash
# As deploy user
crontab -e
```

Add this line (replace 11 with the chosen hour if different):

```
0 11 * * * /home/deploy/dbt/run_dbt.sh > /dev/null 2>&1
```

Save and exit. Verify:

```bash
crontab -l
```

Notes on the cron line:

- **`> /dev/null 2>&1`**: silences cron's mail. The wrapper writes its own log, so cron mail is redundant.
- **No `bash` prefix**: the script has a shebang and is executable.
- **No env vars**: the wrapper uses absolute paths everywhere, so cron's stripped PATH doesn't matter.

## Step D — Commit the wrapper to the client repo

The script is part of the deployment's reproducible state:

```bash
# On the user's laptop, in the client repo
mkdir -p dbt
scp deploy@<client>-mds:/home/deploy/dbt/run_dbt.sh dbt/run_dbt.sh

# Also commit the cron schedule as a separate text file (so it's auditable in Git)
cat > dbt/crontab.txt <<'EOF'
# Cron schedule for dbt on the VPS. Installed under the `deploy` user.
# To install: `crontab dbt/crontab.txt`

0 11 * * * /home/deploy/dbt/run_dbt.sh > /dev/null 2>&1
EOF

git add dbt/run_dbt.sh dbt/crontab.txt
git commit -m "Phase 2: dbt cron schedule"
```

## Step E — Verify (within 24 hours)

The next morning (after the first scheduled run), check:

```bash
ssh deploy@<client>-mds
LOG=$(ls -t /home/deploy/dbt/logs/ | head -1)
tail -30 /home/deploy/dbt/logs/$LOG
```

Expected to see:

- `Completed successfully` from `dbt run`
- `PASS=N WARN=0 ERROR=0` from `dbt test`

If `ERROR > 0`, [`troubleshoot`](../../troubleshoot/SKILL.md) walks through diagnosis.

## Log rotation

Logs accumulate one per day. Over time the `logs/` folder grows. A simple rotation:

```bash
# Append to crontab
crontab -e

# Add: weekly cleanup, keep last 30 days
0 6 * * 0 find /home/deploy/dbt/logs -name 'dbt_run_*.log' -mtime +30 -delete
```

## Common gotchas

- **Cron runs but nothing happens, no log**: check `/var/log/syslog | grep CRON` — usually a permissions or path issue. Solve by making sure the script is executable and uses absolute paths only.
- **Cron runs once and then never again**: a long-running `dbt run` (> 1h) overlaps the next day's schedule. dbt usually finishes in < 30 min for a starter project; if it grows, push the schedule earlier or split into incremental models.
- **First run fails with "Permission denied" reading the BQ key**: the wrapper runs as `deploy` user; if you accidentally changed the key file owner to root, fix with `sudo chown deploy:deploy /home/deploy/secrets/bq-dlt.json`.
- **Schedule drift between UTC and local time**: cron uses the system TZ. Confirm with `timedatectl`. The default on Hostinger Ubuntu images is UTC — verify before assuming.

## Marker state after this step

```jsonc
{
  "stack": {
    "orchestration": "cron"
  },
  "decisions": {
    "dbt_cron_schedule": "0 11 * * *",
    "dbt_cron_user": "deploy",
    "dbt_runner_script": "/home/deploy/dbt/run_dbt.sh",
    "dbt_logs_path": "/home/deploy/dbt/logs"
  }
}
```
