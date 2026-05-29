# Driving the Airbyte API headlessly

End state: Claude can create sources, destinations, and connections and trigger syncs on the VPS's Airbyte OSS instance entirely over the REST API — no UI clicks. This is the workhorse of `add-source`.

All calls run **on the VPS**, against `http://localhost:8000/api/public/v1/`. Port 8000 is bound to localhost only and is **not** public. Claude reaches it by SSH-ing into the VPS and running `curl` there — see [`../../../shared-references/remote-control-model.md`](../../../shared-references/remote-control-model.md) for the `ssh deploy@<client>-mds "..."` pattern. Every command below is what runs *inside* that SSH session.

## The `/api/public/v1/` path trap

Airbyte serves **two** APIs on port 8000:

| Path | Which API | Use it? |
|---|---|---|
| `/api/public/v1/` | **Public API v2** (OAuth2, documented, stable) | **Yes — always.** |
| `/api/v1/` | Internal API (UI's private backend) | **No.** Returns 403/500 when misused; schemas change without notice. |

The naming is genuinely confusing: the **public** API is at `/api/**public**/v1/`, the **internal** one is at `/api/v1/`. If you see a 403 or 500 on a request that looks correct, the first thing to check is whether `public` is in the path.

## OAuth2 token flow

Auth is OAuth2 **client credentials**. The `Client-Id` / `Client-Secret` come from `abctl local credentials` (captured during install — see [`../../create-mds/references/airbyte-install.md`](../../create-mds/references/airbyte-install.md) Step D). They live in the agent's secrets store, never in the marker.

POST them to the token endpoint to get a short-lived JWT:

```bash
curl -s -X POST http://localhost:8000/api/public/v1/applications/token \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": "'"$AIRBYTE_CLIENT_ID"'",
    "client_secret": "'"$AIRBYTE_CLIENT_SECRET"'",
    "grant_type": "client_credentials"
  }'
```

Response:

```json
{ "access_token": "<jwt>", "token_type": "Bearer", "expires_in": 180 }
```

`expires_in` is **~180 seconds**. This is short. Do not fetch a token at the start of a long playbook and assume it survives — see token expiry below.

### Copy-pasteable token fetch (run on the VPS)

This one-liner grabs a fresh token into `$TOKEN`. Run it immediately before the call that needs it:

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/public/v1/applications/token \
  -H "Content-Type: application/json" \
  -d "{\"client_id\":\"$AIRBYTE_CLIENT_ID\",\"client_secret\":\"$AIRBYTE_CLIENT_SECRET\",\"grant_type\":\"client_credentials\"}" \
  | jq -r .access_token)

# sanity check — should be a long dotted string, not "null"
[ "$TOKEN" != "null" ] && [ -n "$TOKEN" ] && echo "token ok" || echo "token FAILED"
```

Every subsequent call carries `-H "Authorization: Bearer $TOKEN"`.

### Token expiry handling

Because the Bash tool is **stateless between calls** (each remote command is a fresh `ssh deploy@<host> "..."`), `$TOKEN` does **not** survive from one Claude Bash call to the next anyway. The safe pattern is:

**Fetch the token in the same SSH command that uses it.** Bundle the token fetch and the API call into one remote shell invocation so the JWT is alive for the whole ~180 s window:

```bash
ssh deploy@<client>-mds '
  TOKEN=$(curl -s -X POST http://localhost:8000/api/public/v1/applications/token \
    -H "Content-Type: application/json" \
    -d "{\"client_id\":\"'"$AIRBYTE_CLIENT_ID"'\",\"client_secret\":\"'"$AIRBYTE_CLIENT_SECRET"'\",\"grant_type\":\"client_credentials\"}" \
    | jq -r .access_token)
  curl -s -H "Authorization: Bearer $TOKEN" \
    http://localhost:8000/api/public/v1/workspaces | jq .
'
```

If a multi-step operation (create source → create connection) runs long, re-fetch the token before each step rather than reusing a stale one. A token-fetch costs nothing; a 401 mid-operation costs a confusing retry.

## Core endpoints

All relative to `http://localhost:8000/api/public/v1/`.

| Method + path | Purpose |
|---|---|
| `POST /applications/token` | Exchange client credentials for a JWT |
| `GET /workspaces` | List workspaces (you need the `workspaceId` for most creates) |
| `GET /sources` | List configured sources |
| `POST /sources` | Create a source |
| `GET /destinations` | List configured destinations |
| `POST /destinations` | Create a destination |
| `GET /connections` | List connections |
| `POST /connections` | Create a connection (binds a source to a destination) |
| `GET /jobs?connectionId=<id>` | List sync/reset jobs for a connection (status, freshness) |
| `POST /jobs` | Trigger a job — body `{"connectionId":"<id>","jobType":"sync"}` |

Do **not** assume endpoints beyond these. If you need something else (e.g. listing connector definitions / their config schema), reach for it conceptually and verify against Airbyte's live API docs at the running version — don't hardcode an invented path.

## Worked example — source → destination → connection → sync

The full happy path for an Airbyte-driven source. IDs returned by each step feed the next, so capture them. Run each block inside one SSH session (token-fresh).

**0. Get the workspace ID** (needed by the creates):

```bash
WORKSPACE_ID=$(curl -s -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/api/public/v1/workspaces | jq -r '.data[0].workspaceId')
```

**1. Create the source.** The `configuration` block is connector-specific — its exact shape comes from the connector's config schema (see `airbyte-connectors-catalog.md`). Example for a generic Postgres-style source:

```bash
SOURCE_ID=$(curl -s -X POST http://localhost:8000/api/public/v1/sources \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "workspaceId": "'"$WORKSPACE_ID"'",
    "name": "acme-shopify",
    "configuration": {
      "sourceType": "shopify",
      "shop": "acme.myshopify.com",
      "credentials": { "auth_method": "api_password", "api_password": "'"$SHOPIFY_TOKEN"'" }
    }
  }' | jq -r .sourceId)
```

**2. Create the BigQuery destination** — *only if it does not already exist*. Check first with `GET /destinations`; reuse the existing one if found (see gotcha below). The BQ credential JSON path is the key copied to the VPS in [`../../create-mds/references/bigquery-project-setup.md`](../../create-mds/references/bigquery-project-setup.md) Step H.

```bash
DEST_ID=$(curl -s -X POST http://localhost:8000/api/public/v1/destinations \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "workspaceId": "'"$WORKSPACE_ID"'",
    "name": "bigquery-raw",
    "configuration": {
      "destinationType": "bigquery",
      "project_id": "'"$BQ_PROJECT_ID"'",
      "dataset_id": "raw_shopify",
      "dataset_location": "EU",
      "loading_method": { "method": "Standard" },
      "credentials_json": '"$(jq -Rs . < /home/deploy/secrets/bq-airbyte.json)"'
    }
  }' | jq -r .destinationId)
```

**3. Create the connection.** Start with `Full Refresh | Overwrite` (safe default — see naming/sync-mode rules in the catalog). Schedule daily:

```bash
CONNECTION_ID=$(curl -s -X POST http://localhost:8000/api/public/v1/connections \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "sourceId": "'"$SOURCE_ID"'",
    "destinationId": "'"$DEST_ID"'",
    "name": "shopify-to-bq",
    "namespaceDefinition": "destination",
    "schedule": { "scheduleType": "cron", "cronExpression": "0 30 7 * * ?" },
    "configurations": { "streams": [] }
  }' | jq -r .connectionId)
```

(An empty `streams: []` typically means "all streams"; to pin specific streams and sync modes, populate it. Confirm against the live schema.)

**4. Trigger the first sync:**

```bash
curl -s -X POST http://localhost:8000/api/public/v1/jobs \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"connectionId":"'"$CONNECTION_ID"'","jobType":"sync"}' | jq .
```

**5. Poll for completion:**

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://localhost:8000/api/public/v1/jobs?connectionId=$CONNECTION_ID" \
  | jq '.data[0] | {jobId, status, jobType}'
```

Loop until `status` is `succeeded` (or `failed` / `cancelled`). Then verify the data landed in BigQuery (`bq ls`, `__TABLES__`).

## Common gotchas

- **403 / 500 on a correct-looking call** → you're on `/api/v1/` (internal) not `/api/public/v1/` (public). Re-check the path for the word `public`.
- **401 mid-operation** → the JWT expired (~180 s). Re-fetch and retry. Don't reuse a token across separate Claude Bash calls; the shell is stateless, so the variable is gone anyway.
- **`access_token` is `null`** → wrong `client_id`/`client_secret`, or they were rotated. Re-run `abctl local credentials` on the VPS to get the current pair and update the secrets store. Credentials can rotate on a reinstall/upgrade.
- **Destination already exists** → creating a second BigQuery destination scoped to the same project is fine but messy. Prefer **one BigQuery destination per project**, with each connection writing into its own `raw_<source>` dataset via the connection's namespace/dataset setting. `GET /destinations` first and reuse the existing `destinationId`.
- **`jq` not installed on the VPS** → `sudo apt-get install -y jq`. The token one-liners depend on it.
- **`curl: connection refused` on :8000** → Airbyte isn't running. `abctl local status`; if components are down, `abctl local start`.
- **Connector config rejected (422)** → the `configuration` block doesn't match the connector's schema. Look the schema up (catalog reference) rather than guessing field names; Airbyte's validation messages name the offending field.
- **Cron expression format** → Airbyte uses Quartz-style 6/7-field cron (`0 30 7 * * ?`), **not** standard 5-field crontab. `0 30 7 * * ?` = daily 07:30. Mixing the two formats silently schedules wrong.

## Marker state after this step

`add-source` doesn't add Airbyte connection IDs to the marker `decisions` (they live in Airbyte and are re-discoverable via `GET /connections`). It records the *source* in `.stack.sources` and a `history` entry on success:

```jsonc
{
  "stack": { "sources": ["...existing", "shopify"] },
  "history": [
    { "date": "2026-05-29", "skill": "add-source", "source": "shopify", "outcome": "ok" }
  ]
}
```

Per principle 4, the authoritative Airbyte config should also be exported (via `GET /sources`, `/destinations`, `/connections`) and committed to the client repo as versioned YAML — connection state must never live UI-only.
