# dbt project scaffold

End state: a dbt project directory on the VPS, structured to match the conventions in [`add-dbt-model`](../../add-dbt-model/SKILL.md), ready to receive staging models in Step 4 of Phase 2. Total time: ~5 minutes.

## Why scaffold by hand instead of `dbt init`

`dbt init` is interactive and asks for profile setup, target choice, etc. — we can do all of that headlessly and with a stricter directory structure than dbt's default. Scaffolding by hand also lets us pre-bake the agent's conventions (staging subfolders by source, materialization defaults, etc.) without needing to edit `dbt_project.yml` afterwards.

## Preflight

```bash
ssh deploy@<client>-mds

# Confirm dbt is installed
/home/deploy/dbt-env/bin/dbt --version > /dev/null || {
  echo "[abort] dbt not installed — run dbt-on-vps-install.md first"
  exit 1
}

# Confirm the project doesn't already exist
PROJECT_PATH="/home/deploy/dbt/<client>-mds"
if [ -d "$PROJECT_PATH" ]; then
  echo "[abort] dbt project already exists at $PROJECT_PATH"
  exit 1
fi
```

## Step A — Create directory layout

```bash
PROJECT_PATH="/home/deploy/dbt/<client>-mds"
mkdir -p $PROJECT_PATH/models/staging
mkdir -p $PROJECT_PATH/models/intermediate
mkdir -p $PROJECT_PATH/models/marts
mkdir -p $PROJECT_PATH/tests
mkdir -p $PROJECT_PATH/seeds
mkdir -p $PROJECT_PATH/macros
mkdir -p $PROJECT_PATH/snapshots
mkdir -p $PROJECT_PATH/analyses
```

## Step B — Write `dbt_project.yml`

```bash
cat > $PROJECT_PATH/dbt_project.yml <<'EOF'
name: '<client>_mds'
version: '0.1.0'
config-version: 2

profile: '<client>_mds'

# Paths
model-paths: ["models"]
analysis-paths: ["analyses"]
test-paths: ["tests"]
seed-paths: ["seeds"]
macro-paths: ["macros"]
snapshot-paths: ["snapshots"]

target-path: "target"
clean-targets:
  - "target"
  - "dbt_packages"

# Models config
# Layered defaults: staging is cheap to rebuild (views), marts persist (tables).
# Override per-model with {{ config(materialized='X') }} at the top of any .sql file.
models:
  <client>_mds:
    staging:
      +materialized: view
      +schema: staging      # writes to analytics_staging dataset
    intermediate:
      +materialized: table
      +schema: intermediate # writes to analytics_intermediate
    marts:
      +materialized: table
      # default schema = profile target dataset (analytics)
EOF
```

Replace `<client>` with the actual sanitized client name (kebab-case, no underscores in YAML — but dbt project names use underscores so `acme_bakery` not `acme-bakery`).

> **Schema strategy**: by default dbt writes everything to the target dataset. The `+schema` config above creates dataset suffixes so staging models land in `<project>.analytics_staging`, intermediate in `<project>.analytics_intermediate`, and marts in `<project>.analytics`. This keeps the namespace clean and lets you grant different IAM roles to end users (only analytics, not staging or intermediate).
>
> The "analytics_" prefix comes from the target dataset name in profiles.yml + the `+schema` suffix concatenated by the BigQuery adapter. To override, see the dbt docs on `generate_schema_name`.

## Step C — Write `.gitignore`

```bash
cat > $PROJECT_PATH/.gitignore <<'EOF'
target/
dbt_packages/
logs/

# Local dev artifacts
.user.yml
EOF
```

## Step D — Optional: `packages.yml`

If the client will use dbt utility packages (date helpers, generic tests, etc.), add a packages.yml at the project root and run `dbt deps` once. Skip in v0.7.0 unless the user asks; most starters don't need it.

```yaml
# Example only — don't include by default
packages:
  - package: dbt-labs/dbt_utils
    version: [">=1.1.0", "<2.0.0"]
```

## Step E — Source `.yml` declaration files

For each raw source the deployment has, declare it to dbt. This lets staging models reference `source('<source_name>', '<table>')` instead of hardcoding `\`<project>.raw_<source>.<table>\``, and enables freshness checks.

For each source, create `models/staging/<source>/_<source>__sources.yml`:

```yaml
version: 2

sources:
  - name: factorial
    database: <project>
    schema: raw_factorial
    description: "Factorial HR data, loaded by the dlt pipeline daily"
    freshness:
      warn_after: {count: 26, period: hour}
      error_after: {count: 50, period: hour}
    loaded_at_field: _dlt_load_id    # dlt stamps each row with its load id; join to _dlt_loads for the timestamp
    # (If you ran the Airbyte alternative instead, the column is _airbyte_extracted_at.)
    tables:
      - name: employees
      - name: contracts
      - name: leaves
      # ... list the raw tables important for this deployment
```

The list of `tables:` doesn't need to be exhaustive — only the ones we plan to model.

## Step F — Smoke-test the project structure

```bash
cd $PROJECT_PATH
/home/deploy/dbt-env/bin/dbt parse
```

`dbt parse` validates the project YAML and source configurations without running any SQL. If this fails, the project YAML has a syntax error.

Expected output: `Project '<client>_mds' loaded successfully.`

(Note: `dbt parse` requires `profiles.yml` to exist. If you scaffold the project before configuring profiles, this check fails — that's expected. See [`dbt-profiles-bigquery.md`](dbt-profiles-bigquery.md) next.)

## Marker state after this step

```jsonc
{
  "decisions": {
    "dbt_project_path_on_vps": "/home/deploy/dbt/<client>-mds",
    "dbt_project_name": "<client>_mds",
    "dbt_schema_strategy": "suffix",
    "dbt_target_dataset_base": "analytics"
  }
}
```
