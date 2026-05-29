# dlt rest_api source — the default lane for SaaS APIs

End state: a SaaS API lands in `raw_<source>` in the warehouse via a **dlt pipeline the agent wrote and ran itself**. The agent writes *config*, not extraction code — dlt's `rest_api` source turns a declarative spec into a paginated, incremental, typed load. This is the workhorse of `add-source`, replacing Airbyte for all but the gnarliest sources.

> **Why this is the default, not Airbyte:** `python load.py` gives the agent a stack trace or a row count *immediately*, at the same layer it acts on. No Temporal, no worker pods, no job polling, no Docker log spelunking. Short feedback loop = the right loop for an agent. (See [`SKILL.md`](../SKILL.md#why-dlt-is-the-default-the-agent-native-rationale).)

## Install

```bash
pip install "dlt[bigquery]"     # extras per destination: dlt[duckdb], dlt[postgres], dlt[snowflake]
```

The `[bigquery]` extra pulls the BigQuery destination. Swapping warehouses is a one-line change in `dlt.pipeline(destination=...)` — this is what keeps the warehouse escape-hatch (principle 7) open.

## The shape of a pipeline

Three pieces: the **source** (declarative `rest_api` config), the **pipeline** (where it loads), and the **run** (which returns `load_info`).

```python
import dlt
from dlt.sources.rest_api import rest_api_source

source = rest_api_source({
    "client": {
        "base_url": "https://api.example.com/v2/",
        "auth": { "type": "bearer", "token": dlt.secrets["sources.example.api_token"] },
        "paginator": "auto",
    },
    "resources": [
        {
            "name": "orders",
            "endpoint": {
                "path": "orders",
                "params": { "per_page": 100 },
                "paginator": { "type": "page_number", "base_page": 1, "total_path": "meta.total_pages" },
            },
            "primary_key": "id",
            "write_disposition": "merge",
            "incremental": {
                "cursor_path": "updated_at",
                "initial_value": "2024-01-01T00:00:00Z",
            },
        },
    ],
})

pipeline = dlt.pipeline(
    pipeline_name="example",
    destination="bigquery",
    dataset_name="raw_example",
)

load_info = pipeline.run(source)
print(load_info)
```

That is the whole extraction layer. No HTTP loop, no pagination bookkeeping, no schema DDL — dlt infers the schema and writes it.

## The `client` block — auth + base_url + default paginator

| Field | What | Notes |
|---|---|---|
| `base_url` | API root | Resource `path`s are relative to it. |
| `auth` | How to authenticate | `type`: `bearer` (token), `api_key` (name + location header/query), `oauth2` (client-credentials flow). Pull the secret from `dlt.secrets[...]` — never inline. |
| `paginator` | Default paginator for all resources | `"auto"` lets dlt detect; override per-resource when needed. |

Auth shapes (verify exact key names against the installed dlt version):

| `type` | Keys |
|---|---|
| `bearer` | `token` |
| `api_key` | `api_key`, `name` (header/param name), `location` (`"header"` or `"query"`) |
| `oauth2` | `client_id`, `client_secret`, `token_url`, `scopes` (client-credentials) |

## The `resources` list — one entry per stream

Each resource maps to a table in `raw_<source>`.

| Field | What | Choices |
|---|---|---|
| `name` | Table name in the dataset | snake_case, stable |
| `endpoint.path` | Path under `base_url` | e.g. `"orders"` |
| `endpoint.params` | Query params | static (e.g. `per_page`) or templated from incremental |
| `endpoint.paginator` | Per-resource paginator | overrides the client default |
| `primary_key` | Dedup key for `merge` | required when `write_disposition: "merge"` |
| `write_disposition` | How rows land | `"append"` / `"replace"` / `"merge"` |
| `incremental` | Cursor for incremental loads | `cursor_path` + `initial_value` |

### Paginator types

Pick by how the API signals "next page" (verify names against the installed version):

| Paginator | API signals next page via |
|---|---|
| `json_link` | A URL in the response body (`next` field) |
| `header_link` | RFC-5988 `Link:` header (GitHub-style) |
| `offset` | `offset` + `limit` params; stops at a total count |
| `page_number` | A page counter; `total_path` or empty-page detection |
| `cursor` | An opaque cursor token echoed forward |
| `"auto"` | dlt guesses — fine to start, pin it explicitly once you know |

> **Paginator is the #1 silent-gap source.** A wrong `total_path`, an off-by-one `base_page`, or `"auto"` mis-detecting on a paginated endpoint can stop the load after page 1 — *no error*, just missing rows. This is exactly why **reconciliation is mandatory** (below).

### write_disposition

| Value | Effect | Use when |
|---|---|---|
| `replace` | Drop + reload the table each run | Small dimension tables; no reliable cursor yet. **Safe default — start here.** |
| `append` | Add new rows, keep old | Immutable event streams |
| `merge` | Upsert on `primary_key` | Mutable records with an `updated_at` cursor; incremental |

Start every resource at `replace` (deterministic, no cursor bugs). Move to `merge` + incremental only once you've confirmed the cursor behaves — and **only with reconciliation watching**.

### Incremental

```python
"incremental": { "cursor_path": "updated_at", "initial_value": "2024-01-01T00:00:00Z" }
```

dlt stores the high-water mark in the **destination** (`_dlt_pipeline_state`) so the next run resumes from it — even on a fresh VPS. Equivalent imperative form for non-`rest_api` resources: `dlt.sources.incremental("updated_at", initial_value=...)`.

> **The cursor is the second silent-gap source.** Pick a column that is **monotonic and server-set** (`updated_at`, a sequence id) — not a client clock, not a field the source can backfill in the past. A cursor that occasionally moves backwards leaves a permanent hole. Reconcile.

## Credentials

Never inline secrets. Two options:

`.dlt/secrets.toml` (gitignored):
```toml
[sources.example]
api_token = "<TOKEN>"

[destination.bigquery]
location = "EU"
[destination.bigquery.credentials]
project_id = "<BQ_PROJECT_ID>"
private_key = "<KEY>"
client_email = "<SA_EMAIL>"
```

Or env vars (better for the VPS / cron):
```bash
export SOURCES__EXAMPLE__API_TOKEN="<TOKEN>"
export DESTINATION__BIGQUERY__CREDENTIALS='{"project_id":"...","private_key":"...","client_email":"..."}'
```

Double underscore `__` is the section separator. The pipeline script is committed (principle 4); `.dlt/secrets.toml` is **not**.

## The run + reconcile loop (mandatory)

```bash
python load.py          # writes raw_example, prints load_info
python reconcile.py      # source count vs raw_example, freshness, gaps — MUST pass
```

`load_info` tells you the load *ran*; it does **not** tell you the data is *complete*. Only reconciliation does. After every run:

1. **Load status** — `_dlt_loads` shows the latest load `status = 0` (succeeded).
2. **Row count** — source-reported count (API `total`, or a `COUNT` against the source) vs `SELECT COUNT(*) FROM raw_example.orders`.
3. **Freshness** — `MAX(updated_at)` in the destination is recent enough.
4. **Sequence gaps** — for id/sequence streams, no holes in the primary key range.

Full SQL/Python for these checks is in [`dlt-state-and-reconstruction.md`](dlt-state-and-reconstruction.md#mandatory-reconciliation-checks). Use [`../templates/reconcile.py.template`](../templates/reconcile.py.template) as the starter.

> **A source is not "done" until reconciliation passes.** This is not optional polish — it is what makes dlt safe. A green `load_info` over a silent gap is the failure this whole skill is built to prevent.

## When to drop to the escape hatch

dlt's `rest_api` handles the vast majority of SaaS APIs. Reach for a **maintained connector** (Airbyte standalone / Singer tap — [`airbyte-api-gotchas.md`](airbyte-api-gotchas.md)) only when:

- The API has pathological pagination/auth a `rest_api` config can't express cleanly.
- A high-quality maintained connector already handles a notoriously fiddly source (and reimplementing it in dlt is wasted effort).
- The source needs CDC/log-based capture dlt's REST source doesn't offer.

That is a **per-source** decision, not a platform switch. Default stays dlt.

## Common gotchas

- **Green load, missing rows.** The single most dangerous outcome. A wrong paginator or cursor silently truncates. *Always* reconcile — `load_info` is not proof of completeness.
- **`"auto"` paginator guessed wrong.** Fine for a first probe; pin the explicit paginator type once you see the response shape, or it may stop after page 1.
- **Incremental cursor moves backwards / is client-set.** Picks up the same rows or skips rows. Use a server-set monotonic column; reconcile the boundary.
- **`merge` without a `primary_key`.** dlt can't upsert; you get duplicates or an error. Set `primary_key` for every `merge` resource.
- **Secret inlined in the script.** It then lands in Git. Pull from `dlt.secrets[...]` / env vars; gitignore `.dlt/secrets.toml`.
- **Rate-limit 429 mid-load.** Looks like a partial failure. Add backoff in the client config; re-run (dlt resumes from state, doesn't double-load merged rows).
- **Schema drift surprises.** dlt auto-evolves the schema (new columns appear). Usually desirable; if a column type flips, dlt may create a variant column — check `_dlt_*` and the table schema after a drift.
- **Wrong dataset.** `dataset_name` must be `raw_<source>` — one dataset per source, tables inside. Don't make `raw_example_orders`.

## Marker state

`add-source` appends the source key to `.stack.sources` and adds a `history` entry on success:

```jsonc
{
  "stack": { "sources": ["...existing", "example"] },
  "history": [
    { "date": "2026-05-29", "skill": "add-source", "source": "example", "outcome": "ok", "via": "dlt" }
  ]
}
```

The pipeline script (`load.py`) and `reconcile.py` are committed to the client repo (principle 4); secrets stay in the secrets store / `.dlt/secrets.toml` (gitignored), never in the marker.
