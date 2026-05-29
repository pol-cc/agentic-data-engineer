# Orchestration — one linear script + systemd timers

End state: a single linear entrypoint script on the VPS that runs **dlt load → dbt build → reconcile** in sequence, fired daily by a systemd `.timer`, inspectable with `systemctl status` and `journalctl -u`. Total time: ~15 minutes.

This replaces cron. Cron is documented as the alternative in [`dbt-cron-scheduling.md`](dbt-cron-scheduling.md) — use it only when you've inherited a crontab or a no-systemd host.

## Why one linear script (the race condition dies by construction)

The old Airbyte+cron stack ran **two independent cron jobs**: Airbyte synced on its schedule, dbt ran on a *separate* schedule chosen to fire "long enough after" the syncs. That gap was a guess, and the gap was the bug — if a sync ran late, dbt built marts from incomplete raw data and silently corrupted yesterday's report. The old playbook documented this as a headline gotcha.

With dlt, ingestion is a Python call the agent owns, so **ingestion and transformation become ONE script, run sequentially**:

```
dlt load   →   dbt build   →   reconcile
   ↓ (only if exit 0)  ↓ (only if exit 0)
```

dbt cannot start until `dlt load` returned success. There is no second schedule to misalign, no slack window to guess. **The race condition is eliminated by construction, not by tuning a cron offset.** This is the single biggest operational win of the dlt switch.

## Why systemd timers over cron (agent observability)

| | cron | systemd timer |
|---|---|---|
| "Did it run? did it pass?" | `grep CRON /var/log/syslog`, then hunt for the job's own log | `systemctl status <unit>` — last run, exit code, next run, all in one read |
| Read the run's output | find and `tail` the script's hand-rolled log file | `journalctl -u <unit>` — structured, timestamped, rotated by journald |
| Missed run while box was off | silently skipped | `Persistent=true` runs it on next boot |
| Stop it overlapping itself | hand-rolled lockfile | `Type=oneshot` — the next trigger won't start until this one exits |
| Cap memory | not built in | `MemoryMax=` |

cron is a mute black box; an agent has to scrape syslog and trust a hand-rolled log. systemd gives the agent **two CLI surfaces it can run and read** (`systemctl status`, `journalctl -u`) — exactly the observability principle 6 demands.

## System vs `--user` units

Two ways to install:

- **System unit** (`/etc/systemd/system/`, run via `sudo systemctl`): survives without a login session, runs as a specified `User=deploy`. **Default — robust for an unattended VPS.**
- **`--user` unit** (`~/.config/systemd/user/`, run via `systemctl --user`): no sudo, but needs lingering enabled (`sudo loginctl enable-linger deploy`) to run when `deploy` isn't logged in.

This reference uses the **system unit** path. The `--user` variant is identical minus `sudo` and with `systemctl --user` / `journalctl --user -u`.

## Preflight

```bash
ssh deploy@<client>-mds

# Both venvs present
/home/deploy/dlt-env/bin/dlt --version > /dev/null && echo "dlt ok"
/home/deploy/dbt-env/bin/dbt --version > /dev/null && echo "dbt ok"

# dbt connects, pipeline dir exists
test -d /home/deploy/dlt/<client>-mds && echo "pipeline dir ok"
cd /home/deploy/dbt/<client>-mds && /home/deploy/dbt-env/bin/dbt debug > /dev/null && echo "dbt debug ok"
```

## Step A — Write the linear entrypoint script

The pipeline lives in the dlt pipeline dir (mirrored in the client repo). `run_pipeline.sh` is the single command systemd fires.

```bash
PIPE_DIR="/home/deploy/dlt/<client>-mds"
DBT_DIR="/home/deploy/dbt/<client>-mds"
mkdir -p "$PIPE_DIR/logs"

cat > "$PIPE_DIR/run_pipeline.sh" <<'EOF'
#!/usr/bin/env bash
# The single linear MDS pipeline: dlt load -> dbt build -> reconcile.
# Run by the systemd .timer. Sequential and deterministic: each stage runs
# only if the previous one exited 0. Exit non-zero on ANY failure so the
# systemd unit goes 'failed' and `systemctl status` shows it loudly.

set -euo pipefail

PIPE_DIR="/home/deploy/dlt/<client>-mds"
DBT_DIR="/home/deploy/dbt/<client>-mds"
DLT_PY="/home/deploy/dlt-env/bin/python"
DBT_BIN="/home/deploy/dbt-env/bin/dbt"

# Reuse the BQ service-account key already on the box (ADC).
export GOOGLE_APPLICATION_CREDENTIALS="/home/deploy/secrets/bq-dlt.json"

echo "=== pipeline start $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# 1) INGEST — dlt loads every source into raw_<src>. State lives in BigQuery.
cd "$PIPE_DIR"
echo "--- dlt load ---"
"$DLT_PY" load.py

# 2) TRANSFORM — dbt build runs models AND tests together; fails on either.
cd "$DBT_DIR"
echo "--- dbt build ---"
"$DBT_BIN" build

# 3) RECONCILE — mandatory. dlt fails silently with DATA GAPS; this catches them.
cd "$PIPE_DIR"
echo "--- reconcile ---"
"$DLT_PY" reconcile.py

echo "=== pipeline ok $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
EOF

# Replace the placeholder with the real project dir name
sed -i "s|<client>-mds|$ACTUAL_PROJECT_DIR_NAME|g" "$PIPE_DIR/run_pipeline.sh"
chmod +x "$PIPE_DIR/run_pipeline.sh"
```

Notes that make this agent-friendly:

- **`set -euo pipefail`** + the sequential layout = the script aborts at the first failing stage with a non-zero exit. systemd marks the unit `failed`; `systemctl status` shows it. No silent partial runs.
- **`dbt build`, not `dbt run` + `dbt test`** — `build` interleaves models and their tests in DAG order, so a model is tested right after it's built and a failing test stops downstream models. One command, loud failure.
- **`reconcile.py` runs last and gates success.** dlt's failure mode is silent data gaps (see [`../../add-source/SKILL.md`](../../add-source/SKILL.md)). Reconciliation row-count / freshness / sequence-gap checks are mandatory; a failed reconcile must exit non-zero so the run is marked `failed`, not quietly "ok".
- **No `> logfile 2>&1` redirection** — let stdout/stderr flow to the journal. `journalctl -u` is the log now. (A `logs/` dir is still created for any tool that writes its own files, e.g. dbt's `target/`.)

> **Python entrypoint alternative.** Instead of `run_pipeline.sh`, the agent can write a single `run_pipeline.py` that imports the dlt sources, calls `pipeline.run(...)`, then `subprocess.run([dbt_bin, "build"], check=True)`, then the reconcile checks — raising on any failure. Same contract (sequential, non-zero on failure). Prefer the Python entry when the agent wants programmatic control over the dlt `load_info` between stages; prefer the bash wrapper for simplicity. Either is fine; the systemd unit just points `ExecStart=` at it.

## Step B — Manual test before scheduling

```bash
/home/deploy/dlt/<client>-mds/run_pipeline.sh
echo "exit: $?"
```

Exit 0 and the tail showing `=== pipeline ok ... ===` means it's safe to schedule. A non-zero exit must be fixed first — don't schedule a broken pipeline.

## Step C — Write the systemd `.service` (Type=oneshot)

```bash
sudo tee /etc/systemd/system/mds-pipeline.service > /dev/null <<'EOF'
[Unit]
Description=MDS pipeline (dlt load -> dbt build -> reconcile)
# Network must be up for BigQuery + any source APIs
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=deploy
Group=deploy
WorkingDirectory=/home/deploy/dlt/<client>-mds
ExecStart=/home/deploy/dlt/<client>-mds/run_pipeline.sh

# Reuse the on-box BQ service-account key (ADC). Or use EnvironmentFile= for
# dlt env-var credentials (DESTINATION__BIGQUERY__CREDENTIALS__*).
Environment=GOOGLE_APPLICATION_CREDENTIALS=/home/deploy/secrets/bq-dlt.json

# Cap memory so a runaway load can't OOM the box. Tune to the VPS (KVM 2 = 8 GB).
MemoryMax=4G

# A long backfill shouldn't be killed prematurely; a hung run shouldn't run forever.
TimeoutStartSec=3600
EOF
```

`Type=oneshot` is the right type for a run-to-completion job: systemd considers the unit "active (exited)" only after the script returns 0, and `failed` if it returns non-zero — which is exactly the status an agent reads.

## Step D — Write the `.timer`

```bash
sudo tee /etc/systemd/system/mds-pipeline.timer > /dev/null <<'EOF'
[Unit]
Description=Run the MDS pipeline daily

[Timer]
# Daily at 09:00 UTC. Pick a time after source APIs have yesterday's data ready.
# Format: DayOfWeek Year-Month-Day Hour:Minute:Second
OnCalendar=*-*-* 09:00:00
# If the box was off at 09:00, run as soon as it's back up (no missed days).
Persistent=true
# Smear start by up to 5 min so many timers don't all fire on the dot.
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
EOF
```

> **Choosing `OnCalendar=`.** With one linear script there is no Airbyte-vs-dbt offset to tune — pick a single time *after* the slowest source has yesterday's data available. 09:00 UTC is a safe default for SaaS APIs. If a source publishes late (some ad platforms finalize mid-afternoon), push to `*-*-* 18:00:00` and accept day-of latency. The whole pipeline (load + build + reconcile) is one unit, so this is the only schedule decision.

## Step E — Enable and verify

```bash
sudo systemctl daemon-reload

# Enable the TIMER (not the service — the timer triggers the service).
sudo systemctl enable --now mds-pipeline.timer

# Confirm it's scheduled and see the next fire time
systemctl status mds-pipeline.timer
systemctl list-timers mds-pipeline.timer

# Fire the pipeline once now, on demand, without waiting for the timer:
sudo systemctl start mds-pipeline.service

# Read how that run went — last exit code + summary:
systemctl status mds-pipeline.service

# Read the full output of the run (this is the new "log"):
journalctl -u mds-pipeline.service --no-pager -n 100

# Follow a run live:
journalctl -u mds-pipeline.service -f
```

A healthy unit shows `Active: inactive (dead)` between runs (oneshot is only active while running) and the timer shows a future `Trigger:`. After a run, `systemctl status mds-pipeline.service` shows the last result; a failure shows `failed` with the non-zero exit code — the agent's signal to read `journalctl -u` and fix.

## Step F — Log retention

journald rotates automatically, but cap it so logs can't fill the disk:

```bash
# Cap the persistent journal at 200 MB (system-wide)
sudo sed -i 's/^#\?SystemMaxUse=.*/SystemMaxUse=200M/' /etc/systemd/journald.conf
sudo systemctl restart systemd-journald

# Inspect current journal disk usage anytime
journalctl --disk-usage
```

The pipeline writes no per-day log files of its own (the journal is the log), so there is no `find … -delete` cleanup cron to maintain like the old stack had.

## Step G — Commit to the client repo

The pipeline is reproducible state (principle 4). Mirror it in the client repo:

```bash
# On the user's laptop, in the client repo
scp deploy@<client>-mds:/home/deploy/dlt/<client>-mds/run_pipeline.sh pipeline/run_pipeline.sh

# Commit the systemd units as auditable text (the agent reinstalls from these on a rebuild)
scp deploy@<client>-mds:/etc/systemd/system/mds-pipeline.service systemd/mds-pipeline.service
scp deploy@<client>-mds:/etc/systemd/system/mds-pipeline.timer   systemd/mds-pipeline.timer

git add pipeline/run_pipeline.sh systemd/mds-pipeline.service systemd/mds-pipeline.timer
git commit -m "Phase 2: linear pipeline + systemd timer"
```

On a fresh VPS, the rebuild re-`scp`s these units back, `daemon-reload`, `enable --now` — and dlt's warehouse-resident state means the first run resumes from the last cursor with no gap. **Cattle, not pet.**

## Common gotchas

- **Unit doesn't run, no error.** You enabled the `.service` instead of the `.timer`. Enable the **timer**: `sudo systemctl enable --now mds-pipeline.timer`. The service is triggered *by* the timer, not enabled directly.
- **`status` shows `failed (Result: exit-code)`.** The script returned non-zero — that's the system working. Read `journalctl -u mds-pipeline.service -n 100` to see which stage (dlt / dbt / reconcile) failed.
- **`Permission denied` reading the BQ key.** The unit runs as `deploy`; if the key got chowned to root, `sudo chown deploy:deploy /home/deploy/secrets/bq-dlt.json`.
- **Timer fires but BigQuery calls fail at boot.** Missing `After=network-online.target` / `Wants=network-online.target` — the unit ran before the network came up. Both lines are in Step C.
- **Run killed mid-backfill.** `MemoryMax=` too low for a big first load, or `TimeoutStartSec=` too short. Raise both for the initial backfill, then lower for steady state.
- **`journalctl` empty for the unit.** You read the wrong unit name, or logs went to a redirected file (don't redirect — Step A note). Confirm the unit name with `systemctl list-units 'mds-*'`.

## Marker state after this step

```jsonc
{
  "stack": {
    "orchestration": "systemd_timer"
  },
  "decisions": {
    "orchestration": "systemd_timer",
    "pipeline_entrypoint": "/home/deploy/dlt/<client>-mds/run_pipeline.sh",
    "systemd_service": "mds-pipeline.service",
    "systemd_timer": "mds-pipeline.timer",
    "pipeline_schedule": "*-*-* 09:00:00",
    "pipeline_user": "deploy"
  },
  "history": [
    ...,
    {"date": "<today>", "skill": "create-mds", "phase": 2, "step": "orchestration", "outcome": "ok"}
  ]
}
```
