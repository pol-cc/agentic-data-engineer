---
name: add-source
description: "Add a new data source (Airbyte connector, BigQuery native transfer, or on-prem database via Tailscale) to an existing Modern Data Stack. Invoke when the user wants to integrate a new SaaS, database, or Google service into the warehouse."
---

# add-source

> **Status**: v0.0.1 — skeleton. Playbook in development.

## What this skill does

Extends an existing MDS deployment with a new data source. Three source types are supported:

| Type | When | How |
|---|---|---|
| **Airbyte connector** | The source has an existing Airbyte connector (Factorial, Shopify, HubSpot, Stripe, MySQL, Postgres, etc.) | Configure via Airbyte API, sync into a new `raw_<source>` BigQuery dataset |
| **BigQuery native transfer** | The source is a Google service with first-party BQ export (GA4, Google Ads, Search Console) | Configure via `bq` CLI / BigQuery Data Transfer Service |
| **On-prem database** | The source is a database physically inside the client's premises (SQL Server, MySQL, Postgres) | Reach via Tailscale, configure as a generic database connector in Airbyte |

The skill picks the right path based on what the user describes and what Airbyte's connector catalog covers.

## Preflight (always run first)

Read the marker:

```bash
if [ ! -f .agentic-data-engineer.json ]; then
  echo "[abort] this directory is not a managed MDS deployment"
  echo "run 'create-mds' first if you need a new stack"
  exit 1
fi
```

Confirm the source is not already configured by checking `.stack.sources` in the marker.

## Playbook outline

**Phase A — Decide the integration path**

1. Ask the user what source to add.
2. Match against Airbyte's connector catalog (see `references/airbyte-connectors-catalog.md`).
3. Determine: Airbyte connector available? Native BQ transfer? On-prem via Tailscale?

**Phase B — Configure the connection**

For Airbyte (most common):

1. Get an Airbyte API token via OAuth2 (see `references/airbyte-api-gotchas.md`).
2. Create the source (`POST /sources`).
3. Create the destination if not yet present (BigQuery, scoped to the right project + dataset).
4. Create the connection with the right sync mode and schedule.
5. Trigger an initial sync (`POST /jobs`).

For native BQ transfer:

1. Authenticate the source service against the BigQuery project.
2. Create the data transfer config via `bq mk --transfer_config` or the BigQuery Data Transfer API.
3. Schedule daily/hourly as appropriate.

For on-prem via Tailscale:

1. Verify the on-prem host is in the tailnet (`tailscale status`).
2. Confirm Airbyte can reach the host on the database port from inside the VPS.
3. Configure the Airbyte source with the Tailscale hostname (not a public IP).

**Phase C — Verify**

1. Wait for the first sync to complete.
2. Confirm the `raw_<source>` dataset exists in BigQuery and has data.
3. Update the marker:

```jsonc
{
  "stack": {
    "sources": [...existing, "<new_source>"]
  },
  "history": [..., {"date": "...", "skill": "add-source", "source": "<new_source>", "outcome": "ok"}]
}
```

4. Commit to the client repo.

## References

- [`references/airbyte-api-gotchas.md`](references/airbyte-api-gotchas.md) — *to be written* — API v2 served under `/api/public/v1/`, OAuth2 token flow, common 403/500 traps
- [`references/airbyte-connectors-catalog.md`](references/airbyte-connectors-catalog.md) — *to be written* — when to pick which connector, common gotchas per source
- [`references/bq-native-transfer.md`](references/bq-native-transfer.md) — *to be written* — GA4, Google Ads, Search Console native transfer setup
- [`references/on-prem-tailscale.md`](references/on-prem-tailscale.md) — *to be written* — reaching SQL Server / MySQL / Postgres behind NAT via Tailscale
