# Adding an on-prem database via Tailscale

End state: a database physically inside the client's office (SQL Server, MySQL, or Postgres) is an Airbyte source, reached by Airbyte-on-the-VPS over the tailnet — with **zero** public exposure of the database. The author's live deployment runs an on-prem SQL Server ERP (42 tables) exactly this way.

## How the connection actually flows

Keep the two paths separate (see [`../../../shared-references/remote-control-model.md`](../../../shared-references/remote-control-model.md)):

| Path | Who | Over what |
|---|---|---|
| **Runtime data flow** | Airbyte on the VPS → on-prem DB port (1433/3306/5432) | Tailscale, on schedule |
| **Agent management** | Claude → VPS / on-prem host via SSH | Tailscale SSH, on demand |

Airbyte runs on the VPS. The on-prem host joins the **same tailnet**. So Airbyte dials the DB at the on-prem host's **Tailscale hostname** on the DB port, as if they were on one LAN. The DB never gets a public port. This is principle 2 doing its job.

## Preflight — confirm the tailnet path

The on-prem host should already be in the tailnet from Phase 1 ([`../../create-mds/references/tailscale-onprem.md`](../../create-mds/references/tailscale-onprem.md) Step E). If not, do that first. Then verify, **from the VPS** (the machine Airbyte runs on):

```bash
ssh deploy@<client>-mds

# 1. Is the on-prem host in the tailnet and online?
tailscale status | grep <onprem-host-name>     # expect a 100.x address + "active"/idle (not "offline")

# 2. Can the VPS reach the DB port over the tailnet?
nc -zv <onprem-host-name> 1433      # SQL Server  (3306 MySQL, 5432 Postgres)
# expect: "succeeded" / "open"
```

If `nc` hangs or refuses, the data path is broken — **fix it before touching Airbyte**. The usual cause is the on-prem firewall (below), not Airbyte.

## On-prem firewall requirement

Most office firewalls block all incoming traffic by default. The on-prem DB host must **allow the Tailscale interface** to reach the DB port. This is the one manual firewall touch on the on-prem side (and the only place the user changes a firewall in this whole flow).

- **Windows** (typical for SQL Server): add an inbound rule allowing TCP 1433 on the `Tailscale` network adapter / the `100.64.0.0/10` CGNAT range. Do **not** open 1433 to the public internet — scope the rule to the Tailscale interface only.
  ```powershell
  New-NetFirewallRule -DisplayName "Airbyte via Tailscale (SQL Server)" `
    -Direction Inbound -Protocol TCP -LocalPort 1433 `
    -RemoteAddress 100.64.0.0/10 -Action Allow
  ```
- **Linux** (MySQL/Postgres): allow the DB port only on the `tailscale0` interface, mirroring the VPS pattern:
  ```bash
  sudo ufw allow in on tailscale0 to any port 5432 proto tcp   # or 3306
  ```

After the rule, re-run `nc -zv <onprem-host-name> <port>` from the VPS — it should now succeed.

## Create a read-only DB user for Airbyte

Never give Airbyte the DB admin account. Provision a dedicated **read-only** user (least privilege — principle 7-friendly):

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

Store the password in the agent's secrets store, never in the marker or the client repo.

## Configure the Airbyte source

Create the generic DB source via the public API (token + mechanics in [`airbyte-api-gotchas.md`](airbyte-api-gotchas.md)). The one rule that matters here:

> **Host = the Tailscale hostname, NOT a public IP and NOT the LAN IP.** Use `<onprem-host-name>` (the MagicDNS name) or its `100.x` tailnet IP. The office LAN IP (`192.168.x`) is meaningless to the VPS; a public IP would defeat the whole design.

Sketch of the `configuration` for a SQL Server source (confirm exact field names against the live spec — see the catalog):

```jsonc
{
  "sourceType": "mssql",
  "host": "<onprem-host-name>",     // Tailscale hostname — NOT 192.168.x, NOT public IP
  "port": 1433,
  "database": "<erp_db>",
  "username": "airbyte_ro",
  "password": "<secret>",
  "schemas": ["dbo"],
  "ssl_method": { "ssl_method": "unencrypted" }  // tailnet is already encrypted end-to-end
}
```

Then create the BigQuery destination/connection and trigger a sync per the worked example in `airbyte-api-gotchas.md`. Land the data in `raw_<source>` (e.g. `raw_sql_server`).

## Sync mode choice

Start at **Full Refresh | Overwrite** — safe, deterministic, no cursor risk. This is what the author's 42-table SQL Server ERP runs on (daily 07:30 UTC, dataset `raw_sql_server`). Move individual large tables to incremental/CDC only once the volume justifies the extra setup:

- **SQL Server CDC** needs SQL Server Agent running and CDC enabled on the tables.
- **Postgres CDC** needs `wal_level=logical` + a replication slot.
- **MySQL CDC** needs binlog enabled.

CDC adds load on the on-prem box and more failure modes over the tailnet — don't reach for it until Full Refresh is too slow.

## Common gotchas

- **On-prem Windows host shows `offline` after a reboot** → Tailscale wasn't installed as a service. It must run as a daemon, not the interactive app. In an elevated PowerShell: `& "C:\Program Files\Tailscale\tailscale.exe" up`; confirm `Get-Service Tailscale` is `Running`. (Same note as Phase 1.)
- **`nc -zv` from the VPS hangs/refuses** → on-prem firewall is blocking the DB port on the Tailscale interface. Add the inbound rule above, scoped to the tailnet range — never to the public internet.
- **Airbyte connects but reads no tables** → the `airbyte_ro` user lacks `SELECT`/`db_datareader`, or is scoped to the wrong schema/database. Re-grant; check `schemas` in the source config.
- **Used the LAN or public IP as host** → works from inside the office, fails from the VPS (LAN IP) or exposes the DB (public IP). Always the Tailscale hostname.
- **MagicDNS name not resolving from the VPS** → fall back to the `100.x` tailnet IP from `tailscale status`; check MagicDNS is enabled in the tailnet admin.
- **Sync works manually but not on schedule** → the on-prem host or its Tailscale daemon sleeps/powers down outside office hours. An always-on box (or wake schedule) is required for unattended nightly syncs.

## Marker state

```jsonc
{
  "stack": { "sources": ["...existing", "sql_server_onprem_tailscale"] },
  "decisions": {
    "tailnet_hostname_onprem": "<onprem-host-name>"
  },
  "history": [
    { "date": "2026-05-29", "skill": "add-source", "source": "sql_server_onprem_tailscale", "outcome": "ok", "via": "airbyte_tailscale" }
  ]
}
```

The `airbyte_ro` password lives in the secrets store; the Airbyte source config (with the secret redacted/referenced) is exported and committed per principle 4.
