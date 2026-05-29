# dlt sql_database source — on-prem & cloud databases

End state: a database (SQL Server, MySQL, Postgres) lands in `raw_<source>` via a **dlt `sql_database` pipeline**, reached over the tailnet for on-prem hosts. dlt reflects the tables from a SQLAlchemy connection string — no per-table extraction code. This replaces the Airbyte generic-DB connector as the default DB lane.

> The Tailscale reachability and read-only-user setup are unchanged from the Airbyte days — see [`on-prem-tailscale.md`](on-prem-tailscale.md). **Only the connector layer changes: dlt instead of Airbyte.** Reachability, firewall, and least-privilege DB user are identical.

## How the connection flows

Keep the two paths separate (see [`../../../shared-references/remote-control-model.md`](../../../shared-references/remote-control-model.md)):

| Path | Who | Over what |
|---|---|---|
| **Runtime data flow** | The dlt pipeline (running on the VPS, on cron) → on-prem DB port (1433/3306/5432) | Tailscale, on schedule |
| **Agent management** | Claude → VPS via SSH to run `python load.py` | Tailscale SSH, on demand |

The dlt pipeline runs on the VPS. The on-prem host is in the **same tailnet**. So the pipeline's connection string points at the on-prem host's **Tailscale hostname** on the DB port — as if they were one LAN. The DB never gets a public port. Principle 2 doing its job.

> **Claude is not in the runtime data path.** At 03:00 the cron runs `python load.py` on the VPS, which dials the on-prem DB over the tailnet; Claude is asleep. Claude's job is to *write, run, and reconcile* the pipeline — not to ferry rows.

## Install

```bash
pip install "dlt[bigquery]"
pip install "sqlalchemy" "pymssql"        # SQL Server   (or psycopg2 for Postgres, pymysql for MySQL)
# for speed, optionally: pip install "connectorx" or "pyarrow"
```

## The shape of a sql_database pipeline

```python
import dlt
from dlt.sources.sql_database import sql_database

# Connection string points at the TAILSCALE hostname, not a LAN or public IP.
# SQL Server:  mssql+pymssql://airbyte_ro:<pw>@<onprem-host-name>:1433/<db>
# Postgres:    postgresql://airbyte_ro:<pw>@<onprem-host-name>:5432/<db>
# MySQL:       mysql+pymysql://airbyte_ro:<pw>@<onprem-host-name>:3306/<db>
source = sql_database(
    credentials=dlt.secrets["sources.sql_database.credentials"],
    backend="pyarrow",          # or "connectorx" for large pulls; "sqlalchemy" is the safe default
    # table_names=["orders", "customers"],   # omit to reflect all readable tables
)

pipeline = dlt.pipeline(
    pipeline_name="sql_server",
    destination="bigquery",
    dataset_name="raw_sql_server",
)

load_info = pipeline.run(source)
print(load_info)
```

dlt **reflects** the schema from the live DB — you don't declare columns. By default it reads every table the read-only user can `SELECT`; pin `table_names=[...]` to scope it.

## Connection string — the one rule that matters

> **Host = the Tailscale hostname (or its `100.x` tailnet IP), NOT the office LAN IP (`192.168.x`) and NOT a public IP.** The LAN IP is meaningless to the VPS; a public IP would defeat the whole zero-exposure design. Use the MagicDNS name `<onprem-host-name>`.

Put the credentials in `.dlt/secrets.toml` (gitignored) so the host/password never hit the script or Git:

```toml
[sources.sql_database.credentials]
drivername = "mssql+pymssql"
host = "<onprem-host-name>"      # Tailscale hostname — NOT 192.168.x, NOT public IP
port = 1433
database = "<erp_db>"
username = "airbyte_ro"
password = "<secret>"
```

Or a single env var:
```bash
export SOURCES__SQL_DATABASE__CREDENTIALS="mssql+pymssql://airbyte_ro:<pw>@<onprem-host-name>:1433/<erp_db>"
```

## Read-only DB user

Same least-privilege user as before — never give the pipeline the DB admin account. Full `CREATE`/`GRANT` per engine (SQL Server `db_datareader`, Postgres `SELECT`, MySQL `SELECT`) is in [`on-prem-tailscale.md`](on-prem-tailscale.md#create-a-read-only-db-user). dlt only ever `SELECT`s; the read-only grant is sufficient. The user is still named `airbyte_ro` in existing deployments — keep the name for continuity, or rename to `dlt_ro` for new ones.

## Backend choice

| `backend` | When | Trade-off |
|---|---|---|
| `sqlalchemy` | Default; correctness-first; any engine | Slowest; one row at a time |
| `pyarrow` | Most loads; columnar, typed | Faster; needs `pyarrow` installed |
| `connectorx` | Large pulls (millions of rows) | Fastest; needs `connectorx`; fewer type-mapping knobs |

Start with `pyarrow`. Drop to `sqlalchemy` if a type maps wrong; reach for `connectorx` only when volume demands it.

## Incremental

```python
from dlt.sources.sql_database import sql_database

source = sql_database(backend="pyarrow").with_resources("orders")
source.orders.apply_hints(
    incremental=dlt.sources.incremental("updated_at", initial_value="2024-01-01")
)
```

Pick an `updated_at` / monotonic sequence column per table. dlt stores the high-water mark in the **destination** (`_dlt_pipeline_state`), so a fresh VPS resumes from the warehouse with no gap (see [`dlt-state-and-reconstruction.md`](dlt-state-and-reconstruction.md)). Start at full load (`write_disposition="replace"`); move a table to incremental only once the cursor is proven — **with reconciliation watching** (next section).

> **The same silent-gap risk as REST.** A bad cursor column (one the source updates retroactively, or a non-monotonic timestamp) leaves rows behind with no error. Reconcile every load.

## Run + reconcile loop (mandatory)

```bash
python load.py        # reflects tables, writes raw_sql_server, prints load_info
python reconcile.py    # source COUNT(*) vs raw_sql_server, freshness, gaps — MUST pass
```

For a DB source, reconciliation is straightforward and there's no excuse to skip it — you can `SELECT COUNT(*)` on **both** sides:

- **Row count**: `SELECT COUNT(*) FROM <table>` on the on-prem DB (over the tailnet) vs `SELECT COUNT(*) FROM raw_sql_server.<table>` in BigQuery.
- **Freshness**: `MAX(updated_at)` matches between source and destination.
- **Sequence gaps**: no holes in the primary-key range for incremental tables.

Concrete SQL/Python: [`dlt-state-and-reconstruction.md`](dlt-state-and-reconstruction.md#mandatory-reconciliation-checks). Starter: [`../templates/reconcile.py.template`](../templates/reconcile.py.template). **The source is not done until reconciliation passes.**

## Common gotchas

- **Used the LAN or public IP as host.** Works from inside the office (LAN IP), fails from the VPS, or exposes the DB (public IP). Always the Tailscale hostname. (See [`on-prem-tailscale.md`](on-prem-tailscale.md).)
- **`nc -zv <host> <port>` from the VPS hangs/refuses.** The runtime path is broken — on-prem firewall blocking the DB port on the Tailscale interface. Fix reachability *before* touching dlt; the pipeline can't connect to a DB it can't reach.
- **Driver not installed.** `mssql+pymssql` needs `pymssql`, Postgres `psycopg2`, MySQL `pymysql`. A missing driver is a clear ImportError on `python load.py` — the short feedback loop catches it instantly.
- **Reflects too many tables.** Omitting `table_names` pulls every readable table; on a 200-table ERP that's a slow first load. Pin the tables you actually need.
- **Incremental cursor not monotonic.** Same silent-gap trap as REST. Confirm the column only moves forward; reconcile the boundary.
- **On-prem box sleeps at night.** Manual run works, the 03:00 cron finds the host offline. The on-prem host (and its Tailscale daemon) must be always-on for unattended syncs.
- **Type mapping surprises with `connectorx`.** It's fast but maps fewer edge-case types cleanly. If a `DECIMAL`/`DATETIME` lands wrong, fall back to `pyarrow` or `sqlalchemy`.

## Marker state

```jsonc
{
  "stack": { "sources": ["...existing", "sql_server_onprem_tailscale"] },
  "decisions": {
    "tailnet_hostname_onprem": "<onprem-host-name>"
  },
  "history": [
    { "date": "2026-05-29", "skill": "add-source", "source": "sql_server_onprem_tailscale", "outcome": "ok", "via": "dlt_sql_database" }
  ]
}
```

The DB password lives in the secrets store / `.dlt/secrets.toml` (gitignored); the pipeline script and `reconcile.py` are committed per principle 4, with the host as the Tailscale name and the secret referenced, not inlined.
