# Phase 2 — Transform Layer Playbook (dbt)

This is the orchestrator for Phase 2 of `create-mds`. Phase 1 must be complete. By the end of Phase 2 the deployment has a working dbt project on the VPS that runs daily on cron, transforming `raw_*` data into clean `analytics` tables.

End state of Phase 2:

- dbt-core + dbt-bigquery installed in a Python venv on the VPS
- A dbt project at a stable path on the VPS, structured as `staging / intermediate / marts`
- `profiles.yml` pointing at the BigQuery service account already in place (from Phase 1)
- At least one `staging` model for each source already wired
- A cron job running `dbt run` daily at a scheduled time after Airbyte syncs complete
- Logs written to a discoverable path the agent can tail
- The dbt project committed to the client GitHub repo

Estimated wall-clock time: **30-60 minutes**, ~15 minutes active.

---

## Preflight

```bash
# Confirm we are in a Phase-1-complete deployment
if [ ! -f .agentic-data-engineer.json ]; then
  echo "[abort] not a managed MDS deployment — run create-mds Phase 1 first"
  exit 1
fi

# Confirm Phase 2 hasn't already run
if jq -e '.stack.transform == "dbt_vps"' .agentic-data-engineer.json > /dev/null; then
  echo "[abort] dbt is already configured for this MDS"
  echo "use add-dbt-model to add models, or troubleshoot to debug"
  exit 1
fi

# Confirm at least one source exists (else there is nothing to transform)
SOURCE_COUNT=$(jq '.stack.sources | length' .agentic-data-engineer.json)
if [ "$SOURCE_COUNT" -lt 1 ]; then
  echo "[abort] no sources configured yet — invoke add-source first"
  exit 1
fi
```

If preflight passes, capture from the marker:

- `decisions.vps_hostname_tailnet` → SSH target
- `decisions.bq_project_id` → dbt target project
- `decisions.bq_location` → dbt target location (e.g. `EU`)
- `decisions.bq_service_account_email` → for `profiles.yml`
- `stack.sources` → which raw datasets the agent should create staging models for

---

## Step 1 — Install dbt on the VPS

Detailed instructions: [`dbt-on-vps-install.md`](dbt-on-vps-install.md).

Summary:

1. SSH to the VPS as `deploy`.
2. Install Python 3.12+ and `python3-venv` (Ubuntu 24.04 ships 3.12).
3. Create `/home/deploy/dbt-env/` venv.
4. Install `dbt-core` and `dbt-bigquery` pinned to a known-good version.
5. Verify `dbt --version` reports both core and bigquery adapter.

Verification: `dbt --version` from the venv prints two lines (core + bigquery).

---

## Step 2 — Scaffold the dbt project

Detailed instructions: [`dbt-project-scaffold.md`](dbt-project-scaffold.md).

Summary:

1. Create `/home/deploy/dbt/<client>-mds/` as the dbt project root.
2. Write `dbt_project.yml` with sensible defaults:
   - Profile name: `<client>_mds`
   - Model paths: `models/`, with subdirs `staging/`, `intermediate/`, `marts/`
   - Materialization defaults: staging → `view`, intermediate → `table` (small), marts → `table`
3. Create directory skeleton (`models/{staging,intermediate,marts}/`, `tests/`, `seeds/`, `macros/`).
4. Add a `.gitignore` excluding `target/`, `dbt_packages/`, `logs/`.

Verification: `ls -la /home/deploy/dbt/<client>-mds/` shows the expected layout.

---

## Step 3 — Configure `profiles.yml`

Detailed instructions: [`dbt-profiles-bigquery.md`](dbt-profiles-bigquery.md).

Summary:

1. Create `/home/deploy/.dbt/profiles.yml` (note: lives outside the project root, in `~/.dbt/`).
2. Configure the BigQuery target:
   - Auth method: `service-account` with the key file from Phase 1
   - Project: `decisions.bq_project_id`
   - Dataset: `analytics` (the dbt-managed dataset)
   - Location: `decisions.bq_location`
   - Threads: 4 (sensible for KVM 2 sized VPS)
3. Run `dbt debug` from the project root — should pass all checks.

Verification: `dbt debug` returns "All checks passed!".

---

## Step 4 — Bootstrap staging models for existing sources

For each source already configured (from `stack.sources` in the marker), create one `stg_<source>_<table>` model per important table. The agent doesn't need to model every raw table — it asks the user which tables matter for this client's reporting.

For each chosen table:

1. Create `models/staging/<source>/stg_<source>_<table>.sql`.
2. Apply the standard staging shape: `SELECT` with explicit casts and renamed columns.
3. Add a `schema.yml` next to the SQL with `not_null` tests on the primary key.

This delegates to [`add-dbt-model`](../../add-dbt-model/SKILL.md) for each model — same skill, called with `staging` mode for each table. Phase 2 batches it for the initial set.

> **Don't create marts in Phase 2.** Phase 2 only stands up the dbt infrastructure plus a clean staging layer. Marts are domain-specific and should be added one at a time afterwards via `add-dbt-model` once the user knows what they want to measure.

Verification: `dbt run --select staging` succeeds. All staging tables appear in `<project>.analytics_staging` (or whatever schema the staging config writes to).

---

## Step 5 — Schedule cron

Detailed instructions: [`dbt-cron-scheduling.md`](dbt-cron-scheduling.md).

Summary:

1. Write `/home/deploy/dbt/run_dbt.sh` — a wrapper script that activates the venv, runs `dbt run` and `dbt test`, writes timestamped logs.
2. Make it executable.
3. Add a crontab entry as `deploy` user.

**Pick the schedule carefully** based on when sources finish:

- Airbyte default sync: 07:30 UTC, takes 30-60 min per source.
- Google Ads Data Transfer: variable, often ~10:00-17:00 UTC.
- GA4 Export: variable, often ~07:00-10:30 UTC for the previous day.

A safe default: **11:00 UTC** (13:00 Madrid time). This gives Airbyte syncs ~3h of slack and catches GA4 most days. If Google Ads is critical and runs later, push to 18:00 UTC and accept the day-of latency.

> **Common race condition**: if Airbyte hasn't finished when dbt runs, staging models built from incomplete raw data corrupt downstream marts for a day. The marker stores the chosen schedule so [`troubleshoot`](../../troubleshoot/SKILL.md) can detect this pattern.

Verification: trigger the cron job manually (`/home/deploy/dbt/run_dbt.sh`), confirm log file written, confirm tables updated.

---

## Step 6 — Commit dbt project to the client repo

The dbt project files (NOT the profiles.yml, which holds the path to the credentials) must live in the client repo:

```bash
# On the user's laptop, in the client repo
git clone deploy@<client>-mds:/home/deploy/dbt/<client>-mds dbt
# (or use rsync, scp, or have the agent generate the files locally and push from there)

# Add to client repo, commit
git add dbt/
git commit -m "Phase 2: scaffold dbt project"
```

Two things stay OUTSIDE the repo:

- `profiles.yml` — refers to a credential path. Lives in `/home/deploy/.dbt/` only.
- `target/` and `dbt_packages/` — gitignored. Regenerated on each `dbt deps` / `dbt run`.

A `profiles.yml.example` (with placeholder values) goes IN the repo so a fresh checkout can be made functional.

---

## Step 7 — Update the marker

```jsonc
{
  "stack": {
    "transform": "dbt_vps",
    "orchestration": "cron"
  },
  "decisions": {
    "dbt_project_path_on_vps": "/home/deploy/dbt/<client>-mds",
    "dbt_venv_path": "/home/deploy/dbt-env",
    "dbt_profiles_path_on_vps": "/home/deploy/.dbt/profiles.yml",
    "dbt_target_dataset": "analytics",
    "dbt_cron_schedule": "0 11 * * *",
    "dbt_cron_user": "deploy"
  },
  "history": [
    ...,
    {"date": "<today>", "skill": "create-mds", "phase": 2, "outcome": "ok"}
  ]
}
```

Commit the updated marker.

---

## Step 8 — Hand off

Tell the user:

- dbt project lives at `/home/deploy/dbt/<client>-mds/` on the VPS, mirrored in the client repo
- Cron runs daily at 11:00 UTC (or whatever schedule was chosen)
- First successful run timestamp + table counts in the staging layer
- "To add a mart, invoke me with 'create a mart for X'. I'll use the `add-dbt-model` skill."
- "Use `verify-pipeline` to confirm dbt freshness alongside Airbyte's."

Phase 2 is complete.

---

## Idempotence guarantees

If Phase 2 is interrupted:

- **dbt venv exists but project doesn't** → resume at Step 2.
- **Project scaffold exists but profiles.yml missing** → resume at Step 3.
- **Profiles configured but no models** → resume at Step 4.
- **Models exist but no cron** → resume at Step 5.

Each step's reference file defines its own preflight to make this safe.
