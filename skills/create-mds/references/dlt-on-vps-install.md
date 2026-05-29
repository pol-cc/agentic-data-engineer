# dlt install on the VPS

End state: a Python venv at `/home/deploy/dlt-env/` containing `dlt` with the BigQuery extra, plus a `.dlt/` config + secrets directory wired to the Phase 1 service account. This is the ingestion engine the linear pipeline script invokes. Total time: ~5 minutes.

## What dlt is (and why it's the default)

dlt ([data load tool](https://dlthub.com)) is a **Python library**, not a platform. The agent writes a small pipeline script and runs `python load.py` — it gets a stack trace or a row count back immediately, in the same layer it acts on. There is no control plane to operate, no Docker, no Kubernetes, no job-polling API. This is the short feedback loop an agent works best with (principle 1, principle 6).

The killer property for a disposable VPS: **dlt persists incremental state to the destination warehouse** (`_dlt_loads`, `_dlt_pipeline_state`, `_dlt_version` tables inside each `raw_<src>` dataset), not on the box. Lose the VPS and a fresh one rebuilt from the client repo restores its cursors *from BigQuery* — no data gaps, no re-backfill. **Cattle, not pet.** The full state-and-reconstruction detail lives in [`../../add-source/references/dlt-state-and-reconstruction.md`](../../add-source/references/dlt-state-and-reconstruction.md).

## Why a venv, not system Python

Same reasoning as dbt ([`dbt-on-vps-install.md`](dbt-on-vps-install.md)):

1. **Isolation**: dlt pulls a tree of deps (`pyarrow`, `google-cloud-bigquery`, paginator/auth libs). Keep them off system Python.
2. **Upgradability**: replace the venv wholesale (`rm -rf` + recreate) on a dlt bump.

> **One venv or two?** dlt and dbt can share a single venv if their pins don't conflict — but the default is **two separate venvs** (`dlt-env`, `dbt-env`). Each upgrades independently and a dependency clash in one never breaks the other. The linear pipeline script (see [`orchestration-systemd.md`](orchestration-systemd.md)) calls each by absolute path, so two venvs cost nothing operationally.

## Preflight

```bash
ssh deploy@<client>-mds

# Confirm Python 3.10+ (dlt supports 3.9+, but match the dbt floor)
python3 --version
# Ubuntu 24.04 ships 3.12 — fine.

# Confirm pip and venv module
python3 -m pip --version
python3 -m venv --help > /dev/null && echo "venv module present"

# Install if missing
sudo apt-get install -y python3-venv python3-pip
```

## Step A — Create the venv

```bash
# Living at /home/deploy/dlt-env/ — outside any pipeline dir so it can be wiped and recreated
python3 -m venv /home/deploy/dlt-env

# Verify
ls /home/deploy/dlt-env/bin/   # should show: activate, pip, python, etc.
```

## Step B — Install dlt with the BigQuery extra

```bash
# Activate (or use the absolute pip path if scripting non-interactively)
source /home/deploy/dlt-env/bin/activate

# Upgrade pip first
pip install --upgrade pip

# Install dlt with the BigQuery destination extra.
# Pin a known-good minor; allow patches. Verify the current range against
# https://dlthub.com/docs at execution time — dlt ships frequently.
pip install "dlt[bigquery]>=1.0,<2.0"
```

The `[bigquery]` extra pulls the `google-cloud-bigquery` client and `pyarrow`. For other destinations the extra changes (`dlt[duckdb]`, `dlt[postgres]`, `dlt[snowflake]`) — keeping the warehouse escape hatch open (principle 7).

> **Source extras are per-source, installed when wiring that source.** The REST API helper (`dlt[bigquery]` already includes `requests`-based sources via `dlt.sources.rest_api`); an on-prem DB needs `pip install "dlt[sql_database]"` plus a driver (`pymssql`, `pymysql`, `psycopg2-binary`). Those land in [`add-source`](../../add-source/SKILL.md), not here.

## Step C — Verify

```bash
dlt --version
dlt --help        # confirms the CLI is on PATH inside the venv
python -c "import dlt; print(dlt.__version__)"
```

Expected: a version line (e.g. `dlt 1.x.x`). If `dlt: command not found`, you didn't activate the venv — use the absolute path `/home/deploy/dlt-env/bin/dlt`.

## Step D — Lay out the `.dlt/` config + secrets directory

dlt reads configuration and credentials from a `.dlt/` directory. It searches the **current working directory** first, then the user home (`~/.dlt/`). The pipeline script always `cd`s into the pipeline dir, so put `.dlt/` **inside the pipeline project root** the client repo mirrors.

```bash
# The pipeline project root (mirrored in the client repo; created in Phase 1 Step 5)
PIPE_DIR="/home/deploy/dlt/<client>-mds"
mkdir -p "$PIPE_DIR/.dlt"
chmod 700 "$PIPE_DIR/.dlt"
```

Two files live in `.dlt/`:

- **`config.toml`** — non-secret pipeline config (dataset names, destination type). **Committed** to the client repo.
- **`secrets.toml`** — the BigQuery service-account credentials and any source API keys. **Never committed** (gitignored).

`config.toml` (committed):

```toml
# .dlt/config.toml — non-secret. Safe to commit.
[runtime]
log_level = "INFO"

[destination.bigquery]
location = "EU"          # must match decisions.bq_location
```

`secrets.toml` (NOT committed):

```toml
# .dlt/secrets.toml — credentials. chmod 600. NEVER commit.
[destination.bigquery.credentials]
project_id = "<client>-mds-prod"
private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
client_email = "dlt-writer@<client>-mds-prod.iam.gserviceaccount.com"
```

```bash
chmod 600 "$PIPE_DIR/.dlt/secrets.toml"
```

> **Secrets via env vars instead of `secrets.toml`.** dlt also reads credentials from environment variables, which suits systemd (set them in the unit's `EnvironmentFile=`). The mapping is the dotted path upper-cased with `__`: `DESTINATION__BIGQUERY__CREDENTIALS__PROJECT_ID`, `..._CLIENT_EMAIL`, `..._PRIVATE_KEY`. Or point dlt at the existing key file by exporting `GOOGLE_APPLICATION_CREDENTIALS=/home/deploy/secrets/bq-dlt.json` (the SA key copied in Phase 1) and dlt picks it up via Application Default Credentials. **Prefer the keyfile-via-ADC path** — it reuses the credential already on the box and keeps the private key out of TOML. See [`orchestration-systemd.md`](orchestration-systemd.md) for wiring it into the unit.

## Step E — Bash convenience (optional)

```bash
echo 'alias dlt-activate="source /home/deploy/dlt-env/bin/activate"' >> ~/.bashrc
```

The systemd service uses the absolute path (`/home/deploy/dlt-env/bin/python`), so it never depends on shell activation. See [`orchestration-systemd.md`](orchestration-systemd.md).

## Common gotchas

- **`error: externally-managed-environment`** on system pip: that's why we use a venv. Don't `--break-system-packages`.
- **`dlt-bigquery` install fails on `pyarrow`**: missing build toolchain on minimal Ubuntu. `sudo apt-get install -y build-essential libpython3-dev` before retry.
- **`private_key` newlines mangled in `secrets.toml`**: the key must keep its literal `\n` escapes inside the TOML string, or use the keyfile-via-ADC path (Step D note) and avoid pasting the key into TOML at all.
- **dlt can't find credentials**: it searched `./.dlt/` from the wrong CWD. The script must `cd` into the pipeline dir before running, or set `GOOGLE_APPLICATION_CREDENTIALS`. Confirm with `dlt pipeline <name> info` from inside the pipeline dir.
- **`.dlt/secrets.toml` committed by accident**: it holds a credential. Confirm `.gitignore` excludes `.dlt/secrets.toml` (Phase 1 Step 6 adds it). If it leaked, rotate the SA key.

## Marker state after this step

```jsonc
{
  "decisions": {
    "dlt_venv_path": "/home/deploy/dlt-env",
    "dlt_version": "<output of dlt --version>",
    "dlt_pipeline_dir": "/home/deploy/dlt/<client>-mds",
    "dlt_secrets_mode": "keyfile_adc"   // or "secrets_toml" / "env"
  }
}
```
