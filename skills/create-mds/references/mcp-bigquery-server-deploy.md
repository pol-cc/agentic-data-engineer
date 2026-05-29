# MCP server deploy

End state: an authenticated MCP server running as a Docker container, behind Traefik with TLS, exposing BigQuery read tools to AI clients. Total time: ~30 minutes (most of it building the image and waiting for first auth flow).

## Architecture recap

See [`mcp-server-architecture.md`](mcp-server-architecture.md) for the design decisions. Quick reminder:

- **Implementation**: FastMCP (Python). TypeScript with `@modelcontextprotocol/sdk` is a valid alternative — swap the framework, keep the rest.
- **Transport**: Streamable HTTP (for claude.ai compatibility)
- **Auth**: GitHub OAuth via FastMCP's `GitHubProvider` + username allowlist (`ALLOWED_GITHUB_USERS`)
- **BigQuery access**: dedicated read-only service account, with byte + row caps
- **Write tools**: `append_to_section` + `replace_in_file` commit + push skill-doc edits to the repo `main` (see [`../../add-mcp-skill/references/mcp-github-writeback.md`](../../add-mcp-skill/references/mcp-github-writeback.md))
- **Skills storage**: the client repo, cloned live on the VPS and mounted into the container at `/repo` read-write

## Preflight

```bash
ssh deploy@<client>-mds

# Phase 3 prerequisites that should already be done:
ls /home/deploy/secrets/bq-mcp-reader.json     # BQ read-only SA key
docker ps | grep traefik                        # Traefik running
dig +short mcp.<client-domain>.com              # DNS resolves to this VPS
curl -I https://mcp.<client-domain>.com/        # TLS works (Traefik returns 404, that's fine)

# Confirm GitHub OAuth app credentials are available
test -n "$GITHUB_CLIENT_ID" || echo "[need] GITHUB_CLIENT_ID"
test -n "$GITHUB_CLIENT_SECRET" || echo "[need] GITHUB_CLIENT_SECRET"

# If write tools are enabled, the fine-grained PAT must be available too
test -n "$GITHUB_TOKEN" || echo "[need] GITHUB_TOKEN (only if write tools enabled — see mcp-github-writeback.md)"
```

If any prerequisite is missing, complete it before continuing — none of the steps below skip safely.

## Step A — Clone or scaffold the MCP server source

For v0.3.0 there is no canonical template yet under [`../../add-mcp-skill/templates/mcp-skeleton/`](../../add-mcp-skill/templates/mcp-skeleton/) (planned). Two paths:

### Path 1: use a reference deployment as the starting point

```bash
mkdir -p /home/deploy/mcp-server
cd /home/deploy/mcp-server

# Example: clone the skills-sapiens reference (private repo — request access from pol-cc)
# OR clone a public starter template if/when one is published.
git clone <reference-mcp-server-url> .
```

### Path 2: bootstrap a minimal MCP server from scratch

The minimal shape, summarized — the actual implementation should live in a dedicated repo or template skeleton:

```
mcp-server/
├── requirements.txt             fastmcp, google-cloud-bigquery  (git is a system dep in the image)
├── Dockerfile
├── server.py                    entry: FastMCP app, GitHubProvider auth, read tool + 2 write tools
├── deploy.sh                    preflight (clean tree + HEAD==origin/main) then fetch/reset/rebuild
├── docker-compose.yml
└── README.md
```

The whole server is one `server.py` — FastMCP keeps it compact. Realistic shape:

```python
import os, subprocess
from fastmcp import FastMCP
from fastmcp.server.auth.providers.github import GitHubProvider
from google.cloud import bigquery

# --- inbound auth: GitHub OAuth + username allowlist -----------------------
auth = GitHubProvider(
    client_id=os.environ["GITHUB_CLIENT_ID"],
    client_secret=os.environ["GITHUB_CLIENT_SECRET"],
    base_url=os.environ["PUBLIC_BASE_URL"],          # OAuth callback base
)
ALLOWED = [u for u in os.environ.get("ALLOWED_GITHUB_USERS", "").split(",") if u]

def _authorize_request():
    # read the `login` claim from the verified access token
    login = _current_token_login()                   # via FastMCP auth context
    if ALLOWED and login not in ALLOWED:
        raise PermissionError(f"github_user_not_allowed: {login}")
    return login

mcp = FastMCP("client-mds", auth=auth)

# --- BigQuery read tool (byte + row caps, SELECT only) ---------------------
bq = bigquery.Client(project=os.environ["GCP_PROJECT"])
MAX_BYTES_BILLED = int(os.environ.get("MAX_BYTES_BILLED", 2 * 1024**3))   # 2 GiB
MAX_ROWS         = int(os.environ.get("MAX_ROWS", 1000))

@mcp.tool
def run_bq_query(skill: str, sql: str) -> list[dict]:
    """Run a read-only SELECT scoped to the skill's descriptor.json allowlist."""
    _authorize_request()
    _assert_select_only(sql)
    _assert_tables_in_allowlist(skill, sql)          # dry-run referenced-tables check
    job = bq.query(sql, job_config=bigquery.QueryJobConfig(
        maximum_bytes_billed=MAX_BYTES_BILLED, dry_run=False))
    return [dict(r) for r in job.result(max_results=MAX_ROWS)]

# --- write tools: edit skill docs, commit + push to repo main --------------
# Full mechanism (PAT, _git_setup, _sync_to_origin, _commit_and_push,
# path-traversal guard) is documented in mcp-github-writeback.md.

@mcp.tool
def append_to_section(skill: str, file_key: str, section: str, text: str) -> str:
    login = _authorize_request()
    path = _resolve_skill_file(skill, file_key)      # guarded; stays inside skills/<skill>/
    _sync_to_origin()                                # fetch + hard-reset to origin/main
    _append_under_heading(path, section, text)
    return _commit_and_push(f"docs({skill}): append to {section}", login, path)

@mcp.tool
def replace_in_file(skill: str, file_key: str, old: str, new: str) -> str:
    login = _authorize_request()
    path = _resolve_skill_file(skill, file_key)
    _sync_to_origin()
    _replace_substring(path, old, new)
    return _commit_and_push(f"docs({skill}): replace text", login, path)

if __name__ == "__main__":
    _git_setup()                                     # safe.directory, identity, PAT remote URL
    mcp.run(transport="streamable-http", host="0.0.0.0", port=8000)
```

Key implementation notes (for whoever writes the skeleton):

- **`run_bq_query` MUST**:
  - Use a BQ `dry_run` job (or a real SQL parser) to detect referenced tables and reject any query touching a table not in the current skill's `descriptor.json` allowlist.
  - Enforce `maximum_bytes_billed` on the job from `MAX_BYTES_BILLED` (default 2 GiB).
  - Cap returned rows at `MAX_ROWS` (default 1000).
  - Reject any statement that isn't `SELECT` — no DML, no DDL, no procedures.
- **Inbound auth** (`GitHubProvider`):
  - FastMCP's `GitHubProvider` handles the GitHub OAuth handshake and token verification — no hand-rolled `/auth/...` routes.
  - `_authorize_request()` reads the `login` claim from the verified access token and checks it against `ALLOWED_GITHUB_USERS` (comma-separated env; **empty = any authenticated GitHub user**).
- **Write tools** (`append_to_section`, `replace_in_file`):
  - On startup `_git_setup()` configures `safe.directory`, the `claude-bot` identity, and bakes the `GITHUB_TOKEN` PAT into the origin remote URL.
  - Before every write, `_sync_to_origin()` refuses a dirty tree, fetches, and hard-resets to `origin/main` (self-healing against non-fast-forward).
  - `_commit_and_push()` commits with a `Co-Authored-By: <caller_login>` trailer and pushes to `main`; on push failure it rolls back the local commit.
  - `_resolve_skill_file()` maps logical keys to paths and **rejects anything escaping `skills/<skill>/`** — safety is by tool scoping, not filesystem perms.
  - Full detail: [`../../add-mcp-skill/references/mcp-github-writeback.md`](../../add-mcp-skill/references/mcp-github-writeback.md).
- **Skill discovery**:
  - On startup, scan the cloned repo's `skills/` for subdirectories with a valid `descriptor.json`.
  - Skill `.md` files are read live off the mounted clone, so a chat-driven write (or a human push) is picked up without a rebuild.

## Step B — Dockerfile

```dockerfile
FROM python:3.12-slim

# git is required at runtime — the write tools shell out to it (clone is mounted at /repo)
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py ./

EXPOSE 8000
CMD ["python", "server.py"]
```

`requirements.txt`:

```
fastmcp
google-cloud-bigquery
```

Build:

```bash
cd /home/deploy/mcp-server
docker build -t mcp-server:latest .
```

> The Python image is larger than a Node/Alpine one (~150-200 MB vs ~50 MB), but the trade is worth it: FastMCP ships `GitHubProvider` and Streamable HTTP, and the BQ + git-subprocess code is compact (one `server.py`). If image size matters more than ergonomics, TypeScript with `@modelcontextprotocol/sdk` is the documented alternative.

## Step C — Compose with env vars

```bash
cd /home/deploy/mcp-server

cat > docker-compose.yml <<'EOF'
services:
  mcp:
    image: mcp-server:latest
    container_name: mcp
    restart: unless-stopped
    networks:
      - traefik-net
    environment:
      # Public base URL (FastMCP uses this for the OAuth callback)
      - PUBLIC_BASE_URL=https://mcp.<client-domain>.com

      # BigQuery
      - GCP_PROJECT=<client>-mds-prod
      - BQ_LOCATION=EU
      - GCP_CREDS_PATH=/secrets/bq-mcp-reader.json   # read by the BQ client

      # Read caps (defaults; per-skill descriptor.json can tighten)
      - MAX_BYTES_BILLED=2147483648                  # 2 GiB
      - MAX_ROWS=1000

      # Inbound auth — GitHub OAuth via FastMCP GitHubProvider
      - GITHUB_CLIENT_ID=${GITHUB_CLIENT_ID}
      - GITHUB_CLIENT_SECRET=${GITHUB_CLIENT_SECRET}
      - ALLOWED_GITHUB_USERS=acme-cto,acme-data-analyst   # comma-separated; empty = any authed GitHub user

      # Write tools — fine-grained PAT (contents:write on this repo only).
      # Omit to run read-only. See mcp-github-writeback.md.
      - GITHUB_TOKEN=${GITHUB_TOKEN}

    volumes:
      - /home/deploy/secrets/bq-mcp-reader.json:/secrets/bq-mcp-reader.json:ro
      - /root/<client>-mds:/repo:rw                  # live git clone of the client repo (skills live here)
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.mcp.rule=Host(`mcp.<client-domain>.com`)"
      - "traefik.http.routers.mcp.entrypoints=websecure"
      - "traefik.http.routers.mcp.tls.certresolver=letsencrypt"
      - "traefik.http.services.mcp.loadbalancer.server.port=8000"

networks:
  traefik-net:
    external: true
EOF
```

Replace `<client>` and `<client-domain>.com` with actual values. Customize `ALLOWED_GITHUB_USERS` with the GitHub logins captured in Phase 3 Step 5.

> **`/repo` is mounted `rw`** because the write tools commit + push from inside the container. Safety is enforced by tool scoping (the path-traversal guard in `_resolve_skill_file`), not by the mount being read-only — see [`../../add-mcp-skill/references/mcp-github-writeback.md`](../../add-mcp-skill/references/mcp-github-writeback.md). To run **read-only**, simply omit `GITHUB_TOKEN`; the read tool and inbound OAuth still work.

## Step D — Set the env file

FastMCP's `GitHubProvider` verifies tokens itself, so there is no server-side session secret to generate. Just capture the secrets into a `.env` (not committed):

```bash
cat > /home/deploy/mcp-server/.env <<EOF
GITHUB_CLIENT_ID=$GITHUB_CLIENT_ID
GITHUB_CLIENT_SECRET=$GITHUB_CLIENT_SECRET

# Write tools (omit to run read-only). Fine-grained PAT, contents:write on this repo.
# Rotate ~every 90 days: edit this line and restart the container — no re-clone.
GITHUB_TOKEN=$GITHUB_TOKEN
EOF
chmod 600 /home/deploy/mcp-server/.env
```

Docker Compose will load this automatically when starting from this directory.

## Step E — Clone the client repo on the VPS

The skills live in the client repo. The VPS holds a **live clone** that the container mounts at `/repo`.

```bash
# Clone the client repo (skills/<name>/ folders live inside it)
git clone https://github.com/<owner>/<client>-mds.git /root/<client>-mds
# Initially the skills/ dir may be empty; the first skill is added in the next step.
```

`origin/main` is the source of truth; both human pushes (then `deploy.sh`) and chat-driven write tools converge here. See [`../../add-mcp-skill/references/mcp-github-writeback.md`](../../add-mcp-skill/references/mcp-github-writeback.md).

## Step F — Bring up the container

```bash
cd /home/deploy/mcp-server
docker compose up -d

# Logs
docker logs mcp 2>&1 | tail -30
```

Expected log output:

```
[mcp] git_setup: safe.directory=/repo, identity=claude-bot, origin=x-access-token@github.com/<owner>/<client>-mds
[mcp] connected to BigQuery project <client>-mds-prod (EU)
[mcp] GitHubProvider configured, callback base: https://mcp.<client-domain>.com
[mcp] allowlist: [acme-cto, acme-data-analyst]
[mcp] write tools: enabled  (GITHUB_TOKEN present)
[mcp] loaded 0 skills from /repo/skills
[mcp] FastMCP running (streamable-http) on 0.0.0.0:8000
```

(If `GITHUB_TOKEN` is unset you'll see `write tools: disabled (read-only)` and the `git_setup` line is skipped.)

If any line says `error` or `failed to start`: stop, fix, retry. Common issues at the bottom of this file.

## Step G — Verify TLS + OAuth

`GitHubProvider` drives the OAuth handshake as part of the MCP connection — there is no separate browser login page or session cookie to set. To confirm TLS + that the server is up:

```bash
# TLS terminates at Traefik; the MCP endpoint answers on /mcp
curl -I https://mcp.<client-domain>.com/mcp
# expect a valid Let's Encrypt cert and a 401/406 (auth required) — NOT a 502
```

A 502 means Traefik can't reach the container — confirm `docker network inspect traefik-net` lists `mcp`. A valid cert plus a 401 (rather than 502) means the server is reachable and demanding auth, which is correct.

The real auth check happens when a client connects (next step). If the connecting GitHub user isn't on the allowlist, `_authorize_request()` raises:

```
github_user_not_allowed: some-other-user
```

Fix by editing `ALLOWED_GITHUB_USERS` in `docker-compose.yml` (or `.env`) and restarting the container.

## Step H — Verify the MCP transport

claude.ai connects over **Streamable HTTP** at `/mcp`. The fastest end-to-end check is to add it as a connector (Step 8 of the Phase 3 playbook) and ask it to `list_skills()`. For a quick liveness probe from the laptop:

```bash
curl -I https://mcp.<client-domain>.com/mcp
```

- **401 / 406** (auth required): server is up; the OAuth handshake happens during the MCP connect from a real client. Expected.
- **404**: the `/mcp` path isn't mounted. Check `mcp.run(transport="streamable-http", ...)` and the Traefik router rule.
- **502 from Traefik**: container unreachable on `traefik-net`. Confirm the `mcp` container is attached.

## Step I — Bring up first skill (next reference)

Continue with [`mcp-first-skill-bootstrap.md`](mcp-first-skill-bootstrap.md) to scaffold the first skill folder. Without at least one skill, the MCP server runs but `list_skills()` returns empty.

## Common gotchas

- **Container loops on restart** → check `docker logs mcp` for the error. Usually a missing env var or bad credentials path.
- **BQ credentials not found** → the volume mount path inside the container must match `GCP_CREDS_PATH` (here: `/secrets/bq-mcp-reader.json`).
- **GitHub OAuth callback returns "redirect_uri mismatch"** → the callback configured in the GitHub OAuth app must match the URL `GitHubProvider` derives from `PUBLIC_BASE_URL`. Confirm `PUBLIC_BASE_URL` has no trailing slash and matches the OAuth app's callback exactly.
- **claude.ai connector "fails to connect"** → check that the MCP URL in claude.ai settings ends in `/mcp` (not just the bare hostname).
- **First query times out** → BigQuery cold start on a new project + the `MAX_BYTES_BILLED` cap rejects oversized queries. Look at the BQ console job history for the exact error.
- **`dubious ownership in repository at '/repo'`** → `_git_setup()`'s `safe.directory` config didn't run; the container UID differs from the host owner of the clone. See [`../../add-mcp-skill/references/mcp-github-writeback.md`](../../add-mcp-skill/references/mcp-github-writeback.md).
- **Write tool 403 on push** → `GITHUB_TOKEN` missing, expired, or lacking `contents:write`. Rotate in `.env` and restart.

## Marker state after this step

```jsonc
{
  "stack": {
    "mcp": true
  },
  "decisions": {
    "mcp_endpoint": "https://mcp.<client-domain>.com/mcp",
    "mcp_impl": "fastmcp_python",
    "mcp_auth": "github_oauth",
    "mcp_allowed_users": ["acme-cto", "acme-data-analyst"],
    "mcp_write_tools": true,
    "mcp_bq_service_account": "mcp-reader@<client>-mds-prod.iam.gserviceaccount.com",
    "mcp_server_path_on_vps": "/home/deploy/mcp-server",
    "mcp_repo_clone_on_vps": "/root/<client>-mds"
  }
}
```

(Set `"mcp_write_tools": false` when `GITHUB_TOKEN` is omitted and the server runs read-only.)
