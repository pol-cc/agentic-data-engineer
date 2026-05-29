# MCP server skeleton

A copy-paste starter for the BigQuery-backed MCP server: GitHub OAuth auth,
read-only BigQuery query tool scoped by per-skill table allowlists, and two
GitHub write-back tools that let an authenticated agent edit skill docs from
chat and push the change to `main`.

It ships with one working sample skill (`skills/example-sales/`) so the server
returns something useful the moment it boots.

> This is a STARTER. The safety-critical logic in `server.py` (SELECT-only,
> table allowlist, path-traversal guard, sync-before-write, rollback-on-push)
> is real and correct, but sections marked `# TODO: harden` and the exact
> FastMCP auth-context accessor (`# verify against installed FastMCP version`)
> must be reviewed against the installed library before production use.

## Contents

```
mcp-skeleton/
├── server.py              FastMCP app: run_bq_query + list_skills + get_skill_context
│                          + append_to_section + replace_in_file (write tools)
├── requirements.txt       fastmcp, google-cloud-bigquery, pyyaml
├── Dockerfile             python:3.12-slim + git (system dep for write tools)
├── docker-compose.yml     service + Traefik labels + env vars + volume mounts
├── .env.example           every env var, with required/optional notes
├── deploy.sh              human/code deploy path: preflight + fetch/reset + rebuild
├── README.md              this file
└── skills/
    └── example-sales/     sample skill (descriptor.json + context.md + schema.md + examples.sql)
```

## How to use it

### 1. Copy into the client repo

The skills live in the client's private MDS repo; the server reads them off a
live clone mounted at `/repo`. Copy this skeleton into that repo (e.g. under
`mcp-server/`), then rename/edit `skills/example-sales/` for a real domain — or
add new skills with the `add-mcp-skill` workflow.

### 2. Fill the environment

```bash
cp .env.example .env
# edit .env: PUBLIC_BASE_URL, GCP_PROJECT, GITHUB_CLIENT_ID/SECRET,
# ALLOWED_GITHUB_USERS, and (only for write tools) GITHUB_TOKEN
chmod 600 .env
```

Also edit the `<PLACEHOLDER>` values in `docker-compose.yml` (the Traefik
`Host(...)` rule, the BQ creds mount path, and the `/repo` clone path).

Required: `PUBLIC_BASE_URL`, `GCP_PROJECT`, `GITHUB_CLIENT_ID`,
`GITHUB_CLIENT_SECRET`, and the BigQuery SA key mounted at the path in
`GOOGLE_APPLICATION_CREDENTIALS`.

Optional: `MAX_BYTES_BILLED`, `MAX_ROWS`, `ALLOWED_GITHUB_USERS` (empty = any
authenticated GitHub user). `GITHUB_TOKEN` is required **only if write tools are
enabled** — omit it to run read-only.

### 3. Deploy (on the VPS)

Prerequisites: Traefik running on the external `traefik-net` network, DNS for
`mcp.<client-domain>.com` pointing at the VPS, the BQ read-only SA key on disk,
and a live clone of the client repo at the path mounted to `/repo`.

```bash
docker compose build
docker compose up -d
docker logs mcp 2>&1 | tail -20
```

Expect log lines for the BigQuery connection, the allowlist, and `write tools:
enabled` (or `disabled (read-only)` when `GITHUB_TOKEN` is absent).

For subsequent **code** changes (server.py / Dockerfile / compose), commit + push
to `main`, then on the VPS run `./deploy.sh` (it refuses a dirty tree or a local
HEAD that differs from `origin/main`, then fetch/reset/rebuilds).

**Content-only** skill-doc edits made via the write tools do NOT need a rebuild —
they push to `origin/main` and the container reads them live off the mounted clone.

## The skills folder

Each subfolder of `skills/` with a valid `descriptor.json` is a skill. The
four-file pattern (`descriptor.json` + `context.md` + `schema.md` +
`examples.sql`) is documented in
`add-mcp-skill/references/mcp-skill-folder-pattern.md`. The `tables[]` array in
`descriptor.json` is the **security boundary** — `run_bq_query` rejects any
query that references a table not in the selected skill's allowlist.

## Safety model (read before changing server.py)

- **SELECT-only.** Every query must be a single read-only SELECT — no DML/DDL/
  procedures/multi-statement.
- **Table allowlist.** A BigQuery dry-run reports referenced tables; anything
  outside the skill's `descriptor.json` allowlist is rejected.
- **Caps.** `maximum_bytes_billed` + `MAX_ROWS` on every query.
- **Write tools are narrow.** They edit prose files inside an *existing* skill
  folder only — they cannot create skills, touch `descriptor.json`, or write
  outside `skills/<skill>/`. The path-traversal guard enforces this even though
  `/repo` is mounted read-write.
- **Sync before write, rollback on push fail.** Before any edit the server
  refuses a dirty tree and hard-resets to `origin/main`; a failed push rolls the
  local commit back.

See `add-mcp-skill/references/mcp-github-writeback.md` for the full write-back
mechanism and `create-mds/references/mcp-bigquery-server-deploy.md` for the
end-to-end deploy walkthrough.
