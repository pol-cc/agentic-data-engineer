# Choosing and configuring Airbyte connectors

End state: the right integration path is chosen for the source the user named, and — if it's an Airbyte connector — its `configuration` block is filled with the correct fields. This reference is the decision layer; `airbyte-api-gotchas.md` is the mechanics.

## First decision — Airbyte, BQ native, or generic DB?

Before reaching for a connector, route the source into one of three lanes:

| The source is… | Path | Why |
|---|---|---|
| A **Google service** (GA4, Google Ads, Search Console) | **BigQuery native transfer** — NOT Airbyte | First-party, more reliable, free, saves an Airbyte connector slot. See [`bq-native-transfer.md`](bq-native-transfer.md). |
| A **database physically in the client's office** (SQL Server, MySQL, Postgres on-prem) | **Generic DB connector in Airbyte, reached over Tailscale** | The DB stays private; Airbyte on the VPS dials its Tailscale hostname. See [`on-prem-tailscale.md`](on-prem-tailscale.md). |
| **Anything else** — a SaaS API, or a cloud-hosted DB | **Airbyte connector** (this file) | Standard ELT path. |

> **The rule worth memorizing:** Google services go through BigQuery's native transfers, never Airbyte. Everything else (SaaS APIs, databases) uses Airbyte. The author's real deployment runs Google Ads + GA4 natively and SQL Server + Factorial HR through Airbyte for exactly this reason.

## Common PYME source catalog

Connector availability and quirks for the sources that come up most. "Auth" is what the user must supply; "Gotchas" are the ones that bite during config; "Default sync" is where to start (tune later).

| Source | Connector? | Auth | Gotchas | Default sync |
|---|---|---|---|---|
| **Shopify** | Yes (native) | OAuth, or Admin API access token (`shpat_…`) | Token needs the right API scopes (orders, products, customers); rate-limited — large stores sync slowly | Full Refresh \| Overwrite, then incremental on `orders`/`customers` |
| **Stripe** | Yes (native) | Restricted API key (`rk_…`, read-only) | Use a **restricted** key, not the secret key; `client_secret`/account-level data needs extra scopes | Incremental on events; Full Refresh for small dimension streams |
| **HubSpot** | Yes (native) | OAuth (preferred) or Private App token | Private App must have the right CRM scopes enabled; OAuth needs a human consent ceremony | Incremental where supported |
| **Salesforce** | Yes (native) | OAuth (consent ceremony) | Sandbox vs production endpoint; API call limits on lower editions; BULK API for big objects | Incremental |
| **Factorial HR** | Yes | API key | EU-hosted; map all needed HR objects; small data volumes | **Full Refresh \| Overwrite** (the author's deployment: 27 tables, daily) |
| **MySQL** (cloud-hosted) | Yes | DB creds (read-only user) | Needs network reachability; for CDC, binlog enabled; SSL settings | Full Refresh \| Overwrite, then CDC if available |
| **Postgres** (cloud-hosted) | Yes | DB creds (read-only user) | CDC needs logical replication (`wal_level=logical`) + a replication slot; SSL mode | Full Refresh \| Overwrite, then CDC |
| **SQL Server** (cloud-hosted) | Yes | DB creds (read-only user) | CDC needs SQL Server Agent + CDC enabled on tables; named-instance vs port | Full Refresh \| Overwrite |
| **Google Sheets** | Yes | OAuth / service account | Share the sheet with the service account email; header row assumptions | Full Refresh \| Overwrite |
| **Notion / Airtable** | Yes | API key / integration token | Integration must be invited to the workspace/base | Full Refresh \| Overwrite |
| **GA4** | **No — use BQ native Export** | — | Do not look for an Airbyte connector | n/a — see `bq-native-transfer.md` |
| **Google Ads** | **No — use BQ Data Transfer Service** | — | Do not look for an Airbyte connector | n/a — see `bq-native-transfer.md` |
| **Search Console** | **No — use BQ Data Transfer** | — | Do not look for an Airbyte connector | n/a — see `bq-native-transfer.md` |

Connector availability moves between releases. If a source isn't listed, it likely still has a connector — check the running instance (below) before concluding it doesn't.

## Looking up a connector's exact config schema

Never guess the `configuration` field names — a wrong field returns a 422 and a confusing message. Get the schema from the running Airbyte instance.

Conceptually, the flow is:

1. List the available **source definitions** on this instance to find the connector (its definition ID and exact name).
2. Fetch that definition's **specification** — the JSON Schema of its `configuration` (required fields, enums, secret fields, auth sub-objects).
3. Fill the `configuration` block in `POST /sources` to match that schema.

Use the public API on the VPS (token from `airbyte-api-gotchas.md`). If a `source-definitions` / specification endpoint isn't present at the running version, fall back to the connector's published docs for that version — but treat the live spec as ground truth when available. **Do not hardcode field names from memory across connector versions; they drift.**

A pragmatic shortcut when iterating: configure the source once in the UI over an SSH tunnel ([`../../create-mds/references/airbyte-install.md`](../../create-mds/references/airbyte-install.md) Step F), then `GET /sources` to read back the exact `configuration` shape the UI produced, and reproduce it via the API for the committed config. (Per principle 4, the API/committed form is the source of truth — the UI is only a schema-discovery aid here.)

In practice, the read-back shortcut looks like this on the VPS (token from `airbyte-api-gotchas.md`):

```bash
# After configuring the source once in the UI, dump its exact configuration:
curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/public/v1/sources \
  | jq '.data[] | select(.name=="acme-shopify") | .configuration'
```

Copy that block (with secrets redacted) into the committed config, and reproduce it verbatim in future `POST /sources` calls. This sidesteps guessing field names entirely.

## Auth types — what the user must hand you

| Auth type | Sources | Headless? | What to collect |
|---|---|---|---|
| **API key / token** | Stripe, Factorial, Notion, Airtable, Shopify (Admin token) | Yes | A scoped, read-only key. Stash in the secrets store. |
| **DB credentials** | MySQL, Postgres, SQL Server | Yes | Host, port, db, a read-only DB user. |
| **OAuth** | HubSpot, Salesforce, Google Sheets, Shopify (OAuth flow) | **No — human ceremony** | Drive the consent URL, capture the token. |

For OAuth connectors, the consent screen is principle 1's documented exception: the agent generates the authorization URL, the user clicks and approves in a browser, and the resulting refresh/access token is pasted back. After that the connector refreshes on its own. Prefer a non-OAuth auth method when a connector offers one (e.g. HubSpot Private App token, Shopify Admin API token) to stay fully headless.

## Sync mode choice

| Mode | When | Trade-off |
|---|---|---|
| **Full Refresh \| Overwrite** | **Default for every new source.** Small/medium tables, or when you don't yet trust the incremental cursor | Re-reads everything each run; simple, deterministic, no cursor bugs |
| **Full Refresh \| Append** | Rarely — you want history snapshots of small tables | Grows unbounded; usually a dbt concern instead |
| **Incremental \| Append** | Large tables with a reliable cursor (`updated_at`, monotonic id) | Faster, cheaper; needs a trustworthy cursor column |
| **Incremental \| Append + Dedup (CDC)** | Big, frequently-changing DB tables; the source supports CDC | Lowest load on the source; most setup (binlog/WAL/CDC features) |

Start every source at **Full Refresh \| Overwrite** (principle: safe first, tune later). Move a table to incremental only once you've confirmed its cursor behaves and the volume justifies it.

## Naming convention

Raw data lands in `raw_<source>` BigQuery datasets — one dataset per source: `raw_shopify`, `raw_stripe`, `raw_factorial`, `raw_sql_server`. The connection's destination/namespace setting controls this. Keep `<source>` short, lowercase, snake_case, and stable — dbt staging models and the marker's `.stack.sources` reference it.

## Common gotchas

- **Looking for an Airbyte connector for a Google service** → stop; those are BQ native transfers. This is the single most common wrong turn.
- **Using a full-power API key where a restricted/read-only one exists** (Stripe secret key, DB superuser) → violates least privilege. Always provision a scoped, read-only credential for Airbyte.
- **OAuth connectors need a human consent ceremony** → Salesforce/HubSpot/Google-Sheets-via-OAuth can't be fully headless. Generate the consent URL, have the user approve, capture the token (principle 1's documented exception).
- **Connector version drift** → a `configuration` that worked last quarter can 422 after a connector upgrade. Re-fetch the spec from the running instance.
- **One dataset per source, not per table** → don't create `raw_shopify_orders`, `raw_shopify_customers`. It's `raw_shopify` with tables inside.
- **Rate limits look like failures** → a slow or partially-failing first sync on Shopify/Salesforce is often throttling, not misconfig. Check the job logs before re-creating the source.

## Marker state

`add-source` appends the source key to `.stack.sources` and adds a `history` entry — see the marker example in [`airbyte-api-gotchas.md`](airbyte-api-gotchas.md#marker-state-after-this-step). The chosen connector and sync mode belong in the committed Airbyte config (exported YAML), not the marker.
