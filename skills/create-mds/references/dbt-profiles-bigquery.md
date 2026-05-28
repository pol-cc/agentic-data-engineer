# dbt profiles for BigQuery

End state: `/home/deploy/.dbt/profiles.yml` configured with the service account from Phase 1, `dbt debug` passing. Total time: ~5 minutes.

## Where profiles.yml lives

dbt looks for `profiles.yml` in this order:

1. `$DBT_PROFILES_DIR/profiles.yml` if the env var is set
2. `~/.dbt/profiles.yml`
3. (in the project root, only if explicitly invoked with `--profiles-dir .`)

**Use `~/.dbt/profiles.yml`** (the default). Reasons:

- Keeps credentials separate from the dbt project (which is committed to the client repo).
- One profiles.yml can hold multiple profiles if the same VPS hosts multiple deployments.
- Standard convention; less magic for future maintainers.

## Preflight

```bash
ssh deploy@<client>-mds

# Confirm the service account key was copied during Phase 1
ls -la /home/deploy/secrets/bq-airbyte.json || {
  echo "[abort] BigQuery service account key missing — re-run bigquery-project-setup.md Step H"
  exit 1
}

# Confirm dbt is installed
/home/deploy/dbt-env/bin/dbt --version > /dev/null
```

## Step A — Decide whether to share or split service accounts

Phase 1 created **one** service account (`airbyte-writer@...`) with `bigquery.dataEditor` + `bigquery.jobUser`. Two options:

| Option | Pros | Cons |
|---|---|---|
| **Reuse the same service account for dbt** | One key file to manage; same role set is enough | Slightly larger blast radius if the key leaks |
| **Create a separate `dbt-runner` service account** | Cleaner separation, can scope roles tighter | More IAM bindings to track |

**Default for v0.1: reuse.** Reasoning: PYME deployments don't justify the extra IAM management overhead. The Airbyte and dbt accounts both need write access to `<project>` — the work is similar. Revisit if a security audit demands separation.

## Step B — Create `~/.dbt/profiles.yml`

```bash
mkdir -p /home/deploy/.dbt
chmod 700 /home/deploy/.dbt

cat > /home/deploy/.dbt/profiles.yml <<'EOF'
<client>_mds:
  target: prod
  outputs:
    prod:
      type: bigquery
      method: service-account
      keyfile: /home/deploy/secrets/bq-airbyte.json
      project: <client>-mds-prod
      dataset: analytics
      location: EU              # or US / asia-southeast1 / etc. — must match the project's location
      threads: 4
      timeout_seconds: 600
      priority: interactive
EOF

chmod 600 /home/deploy/.dbt/profiles.yml
```

Replace the placeholders (`<client>_mds`, `<client>-mds-prod`, `EU`) with the actual values captured from the marker:

- Profile name (`<client>_mds`) **must match** the `profile:` line in `dbt_project.yml`.
- `project` must match `decisions.bq_project_id`.
- `location` must match `decisions.bq_location`.

## Step C — Commit a sanitized example to the client repo

`profiles.yml` itself never goes in the repo (contains a credential path). But `profiles.yml.example` does — so a fresh checkout knows the shape:

```bash
# Inside the client repo (on the user's laptop)
cat > dbt/profiles.yml.example <<'EOF'
# Template — copy to ~/.dbt/profiles.yml on the machine that runs dbt,
# fill in the placeholders, and DO NOT commit the filled version.

<client>_mds:
  target: prod
  outputs:
    prod:
      type: bigquery
      method: service-account
      keyfile: /path/to/service-account.json   # NOT in this repo
      project: <client>-mds-prod
      dataset: analytics
      location: EU
      threads: 4
      timeout_seconds: 600
      priority: interactive
EOF

git add dbt/profiles.yml.example
```

## Step D — Run `dbt debug`

```bash
cd /home/deploy/dbt/<client>-mds
/home/deploy/dbt-env/bin/dbt debug
```

Expected output ends with:

```
Configuration:
  profiles.yml file [OK found and valid]
  dbt_project.yml file [OK found and valid]

Required dependencies:
 - git [OK found]

Connection:
  method: service-account
  database: <client>-mds-prod
  schema: analytics
  location: EU
  priority: interactive
  ...
  Connection test: [OK connection ok]

All checks passed!
```

If any line shows `[ERROR]`:

| Error | Cause | Fix |
|---|---|---|
| profiles.yml [ERROR not found] | Wrong path or wrong filename | Confirm `/home/deploy/.dbt/profiles.yml` exists and is readable by the `deploy` user |
| profiles.yml [ERROR invalid] | YAML syntax error | Run `python3 -c "import yaml; yaml.safe_load(open('/home/deploy/.dbt/profiles.yml'))"` for a clear error message |
| Connection test [ERROR not ok] | Service account can't auth or lacks roles | Re-check key file path; confirm `gcloud projects get-iam-policy <project>` shows the bindings |
| `dataset 'analytics' was not found` | Dataset hasn't been created | Either create it manually `bq mk --location=EU <project>:analytics` or trust the first `dbt run` to create it (newer dbt-bigquery versions create on demand) |

## Step E — Quick sanity check: query a raw table

```bash
cd /home/deploy/dbt/<client>-mds
/home/deploy/dbt-env/bin/dbt run-operation log_query \
  --args "{sql: 'SELECT COUNT(*) AS n FROM \`<client>-mds-prod.raw_<source>.<a_table>\`'}"
```

> dbt doesn't have a built-in "run arbitrary query" command. If `log_query` macro isn't available, just use `bq query` instead, or write a one-off model file. The point: verify the service account can read `raw_*` (it has `dataEditor`, which includes read on its own project).

## Marker state after this step

```jsonc
{
  "decisions": {
    "dbt_profiles_path_on_vps": "/home/deploy/.dbt/profiles.yml",
    "dbt_profile_name": "<client>_mds",
    "dbt_target_dataset": "analytics"
  }
}
```
