# dbt install on the VPS

End state: a Python venv at `/home/deploy/dbt-env/` containing `dbt-core` and `dbt-bigquery`, ready to be invoked as the `dbt build` stage of the linear pipeline script. Total time: ~5 minutes.

## Why a venv, not system Python

Two reasons:

1. **Isolation**: dbt has tight version pinning on `jinja2`, `click`, `protobuf`, etc. System Python may have conflicting versions if other tools (n8n, custom scripts) are installed on the VPS.
2. **Upgradability**: replacing the venv when dbt upgrades is a one-line operation (`rm -rf` + recreate). System packages are messier.

Avoid `pipx` for dbt — it works for the CLI but the `dbt-bigquery` adapter must be in the same env as `dbt-core`. The venv path is cleaner.

## Preflight

```bash
ssh deploy@<client>-mds

# Confirm Python 3.10+ is available (dbt 1.11+ requires 3.10 minimum)
python3 --version
# Ubuntu 24.04 ships 3.12 — fine.

# Confirm pip and venv module
python3 -m pip --version
python3 -m venv --help > /dev/null && echo "venv module present"

# Install if missing (rare on 24.04 but safe to ensure)
sudo apt-get install -y python3-venv python3-pip
```

## Step A — Create the venv

```bash
# Living at /home/deploy/dbt-env/ — outside the dbt project dir so the project can be wiped and recreated
python3 -m venv /home/deploy/dbt-env

# Verify
ls /home/deploy/dbt-env/bin/   # should show: activate, pip, python, etc.
```

## Step B — Install dbt-core and dbt-bigquery

```bash
# Activate the venv (or use the absolute pip path if scripting non-interactively)
source /home/deploy/dbt-env/bin/activate

# Upgrade pip first (avoids resolver warnings)
pip install --upgrade pip

# Install dbt — pin to a known-good minor version, allow patches
pip install "dbt-core>=1.11,<1.12" "dbt-bigquery>=1.11,<1.12"
```

> **Why this pin range?** dbt-core and dbt-bigquery are released as parallel versions, sometimes with breaking adapter changes. Pinning a minor range lets the agent stay on a tested combination while still receiving patch updates. The pinned range can be bumped via `pip install -U` once a new minor is validated.

> **Alternative**: if a specific Phase 1 deployment is known to use newer dbt features, override with the user's confirmation. Defaults stay safe.

## Step C — Verify

```bash
dbt --version
```

Expected:

```
Core:
  - installed: 1.11.x
  - latest:    1.11.x  (or newer; warning is informational)
Plugins:
  - bigquery: 1.11.x
```

If `dbt --version` is not found:

- Did you activate the venv? Try `/home/deploy/dbt-env/bin/dbt --version` with the absolute path.
- Was the install successful? `pip list | grep dbt` should show both packages.

## Step D — Bash convenience (optional)

For interactive sessions over SSH, add a convenience alias:

```bash
echo 'alias dbt-activate="source /home/deploy/dbt-env/bin/activate"' >> ~/.bashrc
```

The linear pipeline script invokes dbt by absolute path (`/home/deploy/dbt-env/bin/dbt build`), so it doesn't depend on shell activation. See [`orchestration-systemd.md`](orchestration-systemd.md). (The [cron alternative](dbt-cron-scheduling.md) uses the same absolute path.)

## Common gotchas

- **`error: externally-managed-environment`** on system pip: that's why we use a venv. Don't `pip install --break-system-packages`.
- **`Cython.Compiler.Errors.CompileError`** during install: an outdated `setuptools`/`wheel`. Run `pip install --upgrade pip setuptools wheel` first.
- **`protobuf` version conflict warnings**: dbt usually pins this. If pip can't resolve, try a clean venv (rm -rf the existing one).
- **`dbt-core` install completes but `dbt-bigquery` fails on `pyarrow`**: usually a missing build toolchain on minimal Ubuntu. `sudo apt-get install -y build-essential libpython3-dev` before retry.

## Marker state after this step

```jsonc
{
  "decisions": {
    "dbt_venv_path": "/home/deploy/dbt-env",
    "dbt_core_version": "<output of dbt --version Core line>",
    "dbt_bigquery_version": "<output of bigquery plugin line>"
  }
}
```
