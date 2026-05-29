# Adding an on-prem database via Tailscale

End state: a database physically inside the client's office (SQL Server, MySQL, or Postgres) is loaded by a **dlt `sql_database` pipeline running on the VPS**, reaching the DB over the tailnet — with **zero** public exposure of the database. The author's live deployment runs an on-prem SQL Server ERP (42 tables) exactly this way.

> **This file = the network + DB-access setup (unchanged).** Reachability, firewall, and the read-only DB user are the same as they always were. The **connector layer is now dlt**, not Airbyte — the actual pipeline shape (connection string, backend, incremental) lives in [`dlt-sql-database-source.md`](dlt-sql-database-source.md). (Running Airbyte for an on-prem DB is still possible as an escape hatch — [`airbyte-api-gotchas.md`](airbyte-api-gotchas.md) — but dlt is the default.)

## How the connection actually flows

Keep the two paths separate (see [`../../../shared-references/remote-control-model.md`](../../../shared-references/remote-control-model.md)):

| Path | Who | Over what |
|---|---|---|
| **Runtime data flow** | The dlt pipeline on the VPS → on-prem DB port (1433/3306/5432) | Tailscale, on schedule |
| **Agent management** | Claude → VPS / on-prem host via SSH | Tailscale SSH, on demand |

The dlt pipeline runs on the VPS (on cron). The on-prem host joins the **same tailnet**. So the pipeline's connection string dials the DB at the on-prem host's **Tailscale hostname** on the DB port, as if they were on one LAN. The DB never gets a public port. This is principle 2 doing its job.

## Preflight — confirm the tailnet path

The on-prem host should already be in the tailnet from Phase 1 ([`../../create-mds/references/tailscale-onprem.md`](../../create-mds/references/tailscale-onprem.md) Step E). If not, do that first. Then verify, **from the VPS** (the machine the dlt pipeline runs on):

```bash
ssh deploy@<client>-mds

# 1. Is the on-prem host in the tailnet and online?
tailscale status | grep <onprem-host-name>     # expect a 100.x address + "active"/idle (not "offline")

# 2. Can the VPS reach the DB port over the tailnet?
nc -zv <onprem-host-name> 1433      # SQL Server  (3306 MySQL, 5432 Postgres)
# expect: "succeeded" / "open"
```

If `nc` hangs or refuses, the data path is broken — **fix it before touching the dlt pipeline**. The usual cause is the on-prem firewall (below), not the pipeline.

## On-prem firewall requirement

Most office firewalls block all incoming traffic by default. The on-prem DB host must **allow the Tailscale interface** to reach the DB port. This is the one manual firewall touch on the on-prem side (and the only place the user changes a firewall in this whole flow).

- **Windows** (typical for SQL Server): add an inbound rule allowing TCP 1433 on the `Tailscale` network adapter / the `100.64.0.0/10` CGNAT range. Do **not** open 1433 to the public internet — scope the rule to the Tailscale interface only.
  ```powershell
  New-NetFirewallRule -DisplayName "MDS ingest via Tailscale (SQL Server)" `
    -Direction Inbound -Protocol TCP -LocalPort 1433 `
    -RemoteAddress 100.64.0.0/10 -Action Allow
  ```
- **Linux** (MySQL/Postgres): allow the DB port only on the `tailscale0` interface, mirroring the VPS pattern:
  ```bash
  sudo ufw allow in on tailscale0 to any port 5432 proto tcp   # or 3306
  ```

After the rule, re-run `nc -zv <onprem-host-name> <port>` from the VPS — it should now succeed.

## Create a read-only DB user

Never give the ingest pipeline the DB admin account. Provision a dedicated **read-only** user (least privilege — principle 7-friendly). dlt only ever `SELECT`s, so a pure read-only grant is sufficient. (The user is named `airbyte_ro` for continuity with existing deployments; `dlt_ro` is equally fine for new ones — pick one and keep it consistent.)

**SQL Server:**
```sql
CREATE LOGIN airbyte_ro WITH PASSWORD = '<strong-secret>';
CREATE USER airbyte_ro FOR LOGIN airbyte_ro;
ALTER ROLE db_datareader ADD MEMBER airbyte_ro;   -- read-only on all tables
```

**Postgres:**
```sql
CREATE USER airbyte_ro WITH PASSWORD '<strong-secret>';
GRANT CONNECT ON DATABASE <db> TO airbyte_ro;
GRANT USAGE ON SCHEMA public TO airbyte_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO airbyte_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO airbyte_ro;
```

**MySQL:**
```sql
CREATE USER 'airbyte_ro'@'%' IDENTIFIED BY '<strong-secret>';
GRANT SELECT ON <db>.* TO 'airbyte_ro'@'%';
```

Store the password in the agent's secrets store / `.dlt/secrets.toml` (gitignored), never in the marker or committed to the client repo.

## Configure the dlt source (connector layer)

The pipeline itself — connection string, backend, reflection, incremental — lives in [`dlt-sql-database-source.md`](dlt-sql-database-source.md). The one rule that the network setup enforces here:

> **Host = the Tailscale hostname, NOT a public IP and NOT the LAN IP.** Use `<onprem-host-name>` (the MagicDNS name) or its `100.x` tailnet IP in the dlt connection string. The office LAN IP (`192.168.x`) is meaningless to the VPS; a public IP would defeat the whole design.

The dlt `sql_database` source takes a SQLAlchemy connection string built from exactly these pieces:

```
mssql+pymssql://airbyte_ro:<secret>@<onprem-host-name>:1433/<erp_db>
#               read-only user        Tailscale hostname  DB port  database
```

dlt reflects the tables, lands them in `raw_<source>` (e.g. `raw_sql_server`), and persists incremental state to the warehouse. Build it from [`../templates/dlt_sql_database_source.py.template`](../templates/dlt_sql_database_source.py.template), run `python load.py` on the VPS, then **reconcile** (mandatory — next section).

## Load mode choice

Start at a **full `replace` load** — safe, deterministic, no cursor risk. This is what the author's 42-table SQL Server ERP runs on (daily 07:30 UTC, dataset `raw_sql_server`). Move individual large tables to dlt incremental (cursor column, `merge`) only once the volume justifies it and the cursor is proven — **with reconciliation watching** ([`dlt-state-and-reconstruction.md`](dlt-state-and-reconstruction.md)).

If a table truly needs log-based CDC (rare for a PYME ERP), that's a signal to consider the maintained-connector escape hatch for *that table* ([`airbyte-api-gotchas.md`](airbyte-api-gotchas.md)) — dlt's `sql_database` source is cursor-based, not log-based. CDC also adds load on the on-prem box and more failure modes over the tailnet, so don't reach for it until a full/incremental load is too slow.

## Reconcile (mandatory)

A DB source has no excuse to skip reconciliation — you can `SELECT COUNT(*)` on both sides. After every load, confirm source-vs-destination row counts, freshness, and sequence gaps per [`dlt-state-and-reconstruction.md`](dlt-state-and-reconstruction.md#mandatory-reconciliation-checks). dlt fails silently on a bad cursor; the count check is what catches the gap. The source is not done until reconcile passes.

## Common gotchas

- **On-prem Windows host shows `offline` after a reboot** → Tailscale wasn't installed as a service. It must run as a daemon, not the interactive app. In an elevated PowerShell: `& "C:\Program Files\Tailscale\tailscale.exe" up`; confirm `Get-Service Tailscale` is `Running`. (Same note as Phase 1.)
- **`nc -zv` from the VPS hangs/refuses** → on-prem firewall is blocking the DB port on the Tailscale interface. Add the inbound rule above, scoped to the tailnet range — never to the public internet. Fix this *before* touching dlt: the pipeline can't load a DB it can't reach.
- **dlt connects but reflects no tables** → the read-only user lacks `SELECT`/`db_datareader`, or is scoped to the wrong schema/database. Re-grant; scope `table_names`/schema in the dlt source.
- **Used the LAN or public IP as host** → works from inside the office, fails from the VPS (LAN IP) or exposes the DB (public IP). Always the Tailscale hostname in the connection string.
- **MagicDNS name not resolving from the VPS** → fall back to the `100.x` tailnet IP from `tailscale status`; check MagicDNS is enabled in the tailnet admin.
- **Load works manually but not on schedule** → the on-prem host or its Tailscale daemon sleeps/powers down outside office hours. An always-on box (or wake schedule) is required for unattended nightly `python load.py` runs.

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

The read-only DB password lives in the secrets store / `.dlt/secrets.toml` (gitignored); the dlt pipeline script and `reconcile.py` (with the host as the Tailscale name and the secret referenced, not inlined) are committed per principle 4.
