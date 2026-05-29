# BigQuery project + service account setup

End state: a fresh GCP project named `<client>-mds-prod` (or as the user named it) with the BigQuery API enabled, billing attached, a **budget alert** in place, and a **write** service account that dlt uses to load `raw_*` datasets. Total time: ~10 minutes; ~3 minutes active.

> **Read/write service-account split.** This step creates the **write** SA (`dlt-writer`) used by dlt loads and (by default) dbt. The MCP server gets its own **read-only** SA, created separately in Phase 3 — agents querying the warehouse should never hold write credentials. This reference only sets up the write side; the MCP worker handles the read-only SA in [`mcp-bigquery-server-deploy.md`](mcp-bigquery-server-deploy.md). The split note appears again at Step F.

## Preflight

- The user has a Google account.
- The user runs `gcloud` on their laptop, or they've granted Claude an active `gcloud auth` session.
- The user has a billing account in Google Cloud (free trial counts, but the agent should confirm).

If `gcloud` is not installed on the user's laptop:

```bash
# macOS:
brew install --cask google-cloud-sdk
# Windows: download installer from https://cloud.google.com/sdk/docs/install
# Linux: see https://cloud.google.com/sdk/docs/install#deb
```

## Step A — Authenticate gcloud (user ceremony)

```bash
gcloud auth login
```

This opens a browser, the user signs in with their Google account, and approves access. ~30 seconds.

```bash
gcloud auth application-default login
```

Same browser flow, but stores credentials for application code (used by the BigQuery client library — important for testing). ~30 seconds.

Verify:

```bash
gcloud auth list           # should show the active account
gcloud projects list       # should list any existing projects (or none)
```

## Step B — Create the project

Project IDs are globally unique across all of Google Cloud. Pick a pattern like `<client>-mds-prod` and append a random suffix if taken.

```bash
PROJECT_ID="<client>-mds-prod"

gcloud projects create $PROJECT_ID \
  --name="<Client> MDS Production" \
  --set-as-default

# Verify
gcloud config get-value project       # should print $PROJECT_ID
```

If creation fails with `ALREADY_EXISTS`:

```bash
PROJECT_ID="<client>-mds-prod-$(openssl rand -hex 3)"
gcloud projects create $PROJECT_ID --name="<Client> MDS Production" --set-as-default
```

## Step C — Link billing

A project without billing can't run BigQuery jobs.

```bash
# List billing accounts visible to the user
gcloud billing accounts list

# Pick the one to use (capture its ID)
BILLING_ACCOUNT_ID="<from above>"

gcloud billing projects link $PROJECT_ID \
  --billing-account=$BILLING_ACCOUNT_ID
```

If the user has no billing account yet, this is a manual ceremony:

1. Send them to https://console.cloud.google.com/billing
2. They create a billing account (requires a credit card; new accounts get $300 free credit).
3. Re-run `gcloud billing accounts list`, then the link command.

> **PYME-friendly note**: BigQuery's free tier (10 GB storage + 1 TB queries/month) means most starter PYMEs see **$0 actual charges**. The billing card is required to enable the API but rarely charged.

## Step C2 — Set a budget alert (the runaway-query safety net)

BigQuery charges by **bytes scanned**, not by row count or time. A single unbounded query against a large table — a missing partition filter, a `SELECT *` over history, a bad join — can scan terabytes and cost real money in seconds, with no warning. A budget alert is the cheap insurance against that. Set one **before** any data lands.

```bash
# The budget lives on the BILLING ACCOUNT, scoped to this project.
# Requires the Cloud Billing Budget API.
gcloud services enable billingbudgets.googleapis.com --project=$PROJECT_ID

# Create a monthly budget with alert thresholds at 50% / 90% / 100%.
# Verify the exact flag names against `gcloud billing budgets create --help`
# at execution time — the Budget API surface has changed across gcloud versions.
gcloud billing budgets create \
  --billing-account=$BILLING_ACCOUNT_ID \
  --display-name="<client> MDS monthly budget" \
  --budget-amount=20EUR \
  --filter-projects="projects/$PROJECT_ID" \
  --threshold-rule=percent=0.5 \
  --threshold-rule=percent=0.9 \
  --threshold-rule=percent=1.0

# Verify
gcloud billing budgets list --billing-account=$BILLING_ACCOUNT_ID
```

Pick a budget amount the client is comfortable being alerted at — `20EUR/month` is a sane default for a starter PYME that expects to stay inside the free tier (the alert fires long before a real bill). Alerts go to the billing account's admins by email; for headless notification, attach a Pub/Sub topic (`--all-updates-rule-pubsub-topic=`) the agent can poll later.

> **Budget alerts notify; they do not cap.** A budget alert emails when spend crosses a threshold — it does **not** stop queries. Pair it with the **bytes-cap mindset**: every dbt mart is partitioned + clustered (see [`add-dbt-model`](../../add-dbt-model/SKILL.md)) so queries prune to a partition, and the read-only MCP SA can carry a `maximum_bytes_billed` ceiling (Phase 3). The alert is the backstop, not the brake.

## Step D — Enable the BigQuery API

```bash
gcloud services enable bigquery.googleapis.com --project=$PROJECT_ID

# Verify
gcloud services list --enabled --project=$PROJECT_ID | grep bigquery
```

Also enable the BigQuery Data Transfer Service API — needed later if the user wants GA4 or Google Ads native transfers (Phase 1.5 or `add-source` for those):

```bash
gcloud services enable bigquerydatatransfer.googleapis.com --project=$PROJECT_ID
```

## Step E — Set the default dataset location

BigQuery datasets are pinned to a location at creation. **Pick a location based on the client's country before creating any datasets**:

| Client country | Recommended location | Reason |
|---|---|---|
| EU member state | `EU` (multi-region) | GDPR; lower egress to clients |
| US | `US` (multi-region) | Lowest latency for US users |
| Asia-Pacific | `asia-southeast1` or similar single region | Latency |

```bash
# This is a session default; explicit per-dataset overrides at creation time
gcloud config set bigquery/default_dataset_location EU
```

## Step F — Create the WRITE service account (for dlt)

This is the **write** account: dlt uses it to create `raw_*` datasets and write rows (including dlt's own `_dlt_*` state tables). dbt reuses it by default (see [`dbt-profiles-bigquery.md`](dbt-profiles-bigquery.md)).

```bash
SA_NAME="dlt-writer"

gcloud iam service-accounts create $SA_NAME \
  --display-name="dlt → BigQuery writer" \
  --project=$PROJECT_ID

SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
echo $SA_EMAIL    # capture this for the dlt + dbt configs later
```

Assign minimum roles needed:

| Role | Why |
|---|---|
| `roles/bigquery.dataEditor` | Create datasets, create/update tables, write rows (raw + `_dlt_*` state) |
| `roles/bigquery.jobUser` | Run load/query jobs |

```bash
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/bigquery.dataEditor"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/bigquery.jobUser"
```

> **Why not `roles/bigquery.admin`?** Principle of least privilege. The write account does not need to delete datasets, manage IAM, or read everything. If a stricter policy is desired, scope further with dataset-level grants.

> **Read/write split — the read-only SA is NOT created here.** When Phase 3 stands up the MCP server, it gets a **separate** read-only service account (`mcp-reader@…`) with only `roles/bigquery.dataViewer` + `roles/bigquery.jobUser`, optionally a `maximum_bytes_billed` ceiling, and **no write role at all**. Agents querying the warehouse must never hold the write key. That SA is the MCP worker's responsibility — see [`mcp-bigquery-server-deploy.md`](mcp-bigquery-server-deploy.md). Keep the two keys physically separate on the box (`bq-dlt.json` vs the MCP's own key).

## Step G — Download the service account key

```bash
mkdir -p ~/.config/agentic-data-engineer/secrets
KEY_PATH=~/.config/agentic-data-engineer/secrets/<client>-bq-dlt.json

gcloud iam service-accounts keys create $KEY_PATH \
  --iam-account=$SA_EMAIL

chmod 600 $KEY_PATH
echo "Service account key saved to: $KEY_PATH"
```

**This is a credential. Never commit it to the client repo.** The marker file references the path; the file itself lives in the agent's secrets folder on the user's laptop and on the VPS.

## Step H — Securely copy the key to the VPS

dlt authenticates to BigQuery with this key.

```bash
# From the user's laptop
scp $KEY_PATH deploy@<client>-mds:/home/deploy/secrets/bq-dlt.json

# On the VPS:
ssh deploy@<client>-mds
mkdir -p /home/deploy/secrets
chmod 700 /home/deploy/secrets
chmod 600 /home/deploy/secrets/bq-dlt.json
```

dlt picks this up via Application Default Credentials when the pipeline (and the systemd unit) export `GOOGLE_APPLICATION_CREDENTIALS=/home/deploy/secrets/bq-dlt.json` — see [`dlt-on-vps-install.md`](dlt-on-vps-install.md) and [`orchestration-systemd.md`](orchestration-systemd.md). dbt's `profiles.yml` references the same key file (Phase 2).

## Step I — Smoke-test with `bq`

From the user's laptop:

```bash
bq ls --project_id=$PROJECT_ID
# Expected: empty list (no datasets yet, but the project responds)

bq query --use_legacy_sql=false --project_id=$PROJECT_ID \
  "SELECT 1 AS test"
# Expected: a 1-row result. Confirms billing and API are live.
```

If the smoke test fails:

- **`Access Denied: Project not found`** → billing not linked.
- **`Permission denied`** → API not enabled.
- **`Quota exceeded`** → unlikely on day 1, but check the free tier dashboard.

## Step J — (Optional) Pre-create the raw dataset

dlt creates the `raw_<source>` dataset on first load, but pre-creating it lets the agent verify location and access early:

```bash
SOURCE_KEY=factorial_hr    # whatever the first source is
bq mk \
  --location=EU \
  --description="Raw data from $SOURCE_KEY via dlt" \
  $PROJECT_ID:raw_$SOURCE_KEY
```

(The `add-source` skill / dlt will create the dataset if it doesn't exist.)

## Marker state after this step

```jsonc
{
  "decisions": {
    "bq_project_id": "<client>-mds-prod",
    "bq_location": "EU",
    "bq_budget_alert": true,
    "bq_write_sa": "dlt-writer@<client>-mds-prod.iam.gserviceaccount.com",
    "bq_write_sa_key_ref": "secrets/<client>-bq-dlt.json"
    // The read-only MCP SA is added to the marker in Phase 3, not here.
  }
}
```

## Common gotchas

- **"Billing account is not active"** → user's billing account is in setup or suspended. Check at https://console.cloud.google.com/billing.
- **Free-tier confusion** → free tier is per-billing-account, not per-project. A user with 5 projects on one billing account shares the same 1 TB/month of queries.
- **Dataset location can never be changed** → if EU/US is wrong, the dataset must be recreated. Choose right the first time.
- **Service account key creation disabled** → some orgs disable key creation in Org Policy. If so, fall back to workload identity federation (advanced; not in v0.1 scope).
