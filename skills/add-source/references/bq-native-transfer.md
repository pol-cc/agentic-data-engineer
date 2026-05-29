# BigQuery native transfers for Google services

End state: a Google service (GA4, Google Ads, Search Console) lands daily in BigQuery **without dlt and without an Airbyte connector**. These use Google's first-party exports / BigQuery Data Transfer Service (DTS) — managed, scheduled, and free, run by Google rather than on the VPS.

> **The architectural rule:** Google services use BigQuery's native transfers — **never dlt, never Airbyte**. This is more reliable than scraping the same data through any third-party connector, it's free, and Google runs it. This is the dedicated lane for the three Google sources; everything else defaults to dlt ([`dlt-rest-api-source.md`](dlt-rest-api-source.md)). The author's live deployment runs Google Ads + GA4 this way.

## Preflight

- The BQ project exists with billing linked and the BigQuery API enabled ([`../../create-mds/references/bigquery-project-setup.md`](../../create-mds/references/bigquery-project-setup.md)).
- The **BigQuery Data Transfer Service API** is enabled (Step D of that reference enables `bigquerydatatransfer.googleapis.com`). Re-confirm:
  ```bash
  gcloud services enable bigquerydatatransfer.googleapis.com --project=$BQ_PROJECT_ID
  ```
- `gcloud` / `bq` are authenticated as a user with access to both the BQ project and the source Google service (or a service account with DTS permissions).

## The three Google sources

| Source | Mechanism | Where it's configured | Dataset |
|---|---|---|---|
| **GA4** | BigQuery **Export** (a GA4 feature, not DTS) | GA4 Admin console (human ceremony) | `analytics_<propertyId>` (Google-chosen) |
| **Google Ads** | BigQuery **Data Transfer Service** | `bq mk --transfer_config` or console | `raw_google_ads` |
| **Search Console** | BigQuery **Data Transfer Service** | `bq mk --transfer_config` or console | `raw_search_console` |

Note GA4 is the odd one out: it is **not** DTS. It's a native export configured inside Analytics itself.

## GA4 — BigQuery Export (human ceremony)

GA4's BigQuery link is enabled from the **Analytics admin UI**, not from `bq`. This is an unavoidable human ceremony (principle 1's documented exception). The agent guides; the user clicks.

1. Send the user to **GA4 Admin → Property → Product links → BigQuery links → Link**.
2. They select the BQ project (`$BQ_PROJECT_ID`), pick the data location (match the warehouse — `EU`/`US`), and choose **Daily** export (the free tier; "Streaming" is billable and usually unnecessary for a PYME).
3. They confirm. GA4 grants its export service account access to the project automatically.

Google then creates a dataset named **`analytics_<propertyId>`** (e.g. `analytics_318472901`) — the property ID, not a name you choose. Tables appear as `events_YYYYMMDD`.

Verify the link landed (after the first daily run):

```bash
bq ls --project_id=$BQ_PROJECT_ID | grep analytics_
bq ls --project_id=$BQ_PROJECT_ID analytics_<propertyId>   # expect events_YYYYMMDD
```

> **Freshness is time-aware.** GA4's daily export creates **yesterday's** table (`events_<yesterday>`) each morning — there is intraday lag. A freshness check must compare against *yesterday's* date, not today's, or it will always look "stale." Today's events arrive tomorrow.

## Google Ads — Data Transfer Service via `bq mk`

Google Ads *is* headless via DTS. The command shape:

```bash
bq mk --transfer_config \
  --project_id=$BQ_PROJECT_ID \
  --data_source=google_ads \
  --target_dataset=raw_google_ads \
  --display_name="Google Ads → raw_google_ads" \
  --schedule="every 24 hours" \
  --params='{
    "customer_id": "<10-digit-ads-customer-id-no-dashes>"
  }'
```

- Pre-create the target dataset first (DTS does not always create it): `bq mk --location=EU $BQ_PROJECT_ID:raw_google_ads`.
- `data_source=google_ads` is the DTS source key.
- `customer_id` is the Google Ads account ID **without dashes** (`1234567890`, not `123-456-7890`). For a manager (MCC) account, the customer ID is the client account you want, not the MCC.
- First run triggers an **OAuth authorization ceremony**: `bq mk --transfer_config` prints a URL; the user opens it, grants the Ads scope, pastes back the auth code. After that, DTS refreshes on its own.

Confirm and inspect:

```bash
bq ls --transfer_config --project_id=$BQ_PROJECT_ID --transfer_location=EU
# Backfill a date range if needed:
bq mk --transfer_run --start_time=<RFC3339> --end_time=<RFC3339> <transfer_config_resource_name>
```

## Search Console — Data Transfer Service

Same DTS pattern; `data_source=search_console`. Pre-create `raw_search_console`, then:

```bash
bq mk --transfer_config \
  --project_id=$BQ_PROJECT_ID \
  --data_source=search_console \
  --target_dataset=raw_search_console \
  --display_name="Search Console → raw_search_console" \
  --schedule="every 24 hours" \
  --params='{ "site_url": "https://www.acme.com/" }'
```

`site_url` must exactly match a verified property in Search Console (trailing slash, `https://`, and `www` matter — or use the `sc-domain:acme.com` form for a domain property). Same first-run OAuth ceremony as Google Ads.

## Schedule

All three are **daily, Google-managed**. You don't put these in the VPS's cron — DTS and GA4 Export run on Google's infrastructure on their own daily cadence. The agent's only scheduling job is to confirm the cadence is "Daily," not "Streaming"/intraday, to stay on the free tier.

## Dataset naming

| Source | Dataset | Chosen by |
|---|---|---|
| GA4 | `analytics_<propertyId>` | Google (fixed, property ID) |
| Google Ads | `raw_google_ads` | You (`--target_dataset`) |
| Search Console | `raw_search_console` | You (`--target_dataset`) |

GA4 breaks the `raw_<source>` convention because Google names it — that's expected. dbt staging models should reference `analytics_<propertyId>` explicitly for the GA4 source.

## Common gotchas

- **GA4 freshness false alarm** → checking for *today's* `events_` table always fails; yesterday's is the newest. Make freshness checks date-aware.
- **GA4 export not appearing** → it can take up to ~24 h after linking for the first daily table; intraday/streaming is a separate (billable) toggle. Don't re-link in a panic.
- **`raw_google_ads` not created** → pre-create the dataset with `bq mk` before the transfer config; DTS won't always make it.
- **`customer_id` with dashes** → DTS rejects or returns no data. Strip dashes.
- **DTS API not enabled** → `bq mk --transfer_config` fails; enable `bigquerydatatransfer.googleapis.com`.
- **Dataset location mismatch** → the target dataset's location must match where DTS/GA4 writes; a US/EU mismatch fails the transfer. Match the warehouse location set in Phase 1.
- **OAuth ceremony skipped** → the transfer config exists but never runs because the first-run authorization wasn't completed. Re-run and complete the URL step.

## Marker state

These are still `add-source` outcomes — record the source key in `.stack.sources` and a `history` entry. Because the transfer config lives in Google (not the VPS), note enough to re-find it:

```jsonc
{
  "stack": { "sources": ["...existing", "google_ads", "ga4"] },
  "decisions": {
    "ga4_dataset": "analytics_<propertyId>",
    "google_ads_dataset": "raw_google_ads"
  },
  "history": [
    { "date": "2026-05-29", "skill": "add-source", "source": "google_ads", "outcome": "ok", "via": "bq_dts" },
    { "date": "2026-05-29", "skill": "add-source", "source": "ga4", "outcome": "ok", "via": "bq_export" }
  ]
}
```

Per principle 4, also commit the `bq mk --transfer_config` command(s) to the client repo (e.g. a `transfers/` runbook) so the native transfers are reproducible from Git, not UI-only.
