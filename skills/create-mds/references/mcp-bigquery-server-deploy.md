# MCP server deploy

End state: an authenticated MCP server running as a Docker container, behind Traefik with TLS, exposing BigQuery read tools to AI clients. Total time: ~30 minutes (most of it building the image and waiting for first auth flow).

## Architecture recap

See [`mcp-server-architecture.md`](mcp-server-architecture.md) for the design decisions. Quick reminder:

- **Implementation**: TypeScript with `@modelcontextprotocol/sdk`
- **Transport**: HTTP/SSE (for claude.ai compatibility)
- **Auth**: GitHub OAuth 2.1 + username allowlist
- **BigQuery access**: dedicated read-only service account
- **Skills storage**: directory mounted from the VPS host

## Preflight

```bash
ssh deploy@<client>-mds

# Phase 3 prerequisites that should already be done:
ls /home/deploy/secrets/bq-mcp-reader.json     # BQ read-only SA key
docker ps | grep traefik                        # Traefik running
dig +short mcp.<client-domain>.com              # DNS resolves to this VPS
curl -I https://mcp.<client-domain>.com/        # TLS works (Traefik returns 404, that's fine)

# Confirm GitHub OAuth app credentials are available
test -n "$GITHUB_OAUTH_CLIENT_ID" || echo "[need] GITHUB_OAUTH_CLIENT_ID"
test -n "$GITHUB_OAUTH_CLIENT_SECRET" || echo "[need] GITHUB_OAUTH_CLIENT_SECRET"
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
├── package.json                 dependencies: @modelcontextprotocol/sdk, express, @google-cloud/bigquery, octokit, jose
├── tsconfig.json
├── Dockerfile
├── src/
│   ├── index.ts                 entry: starts express, mounts MCP transport, mounts OAuth routes
│   ├── auth.ts                  GitHub OAuth 2.1 flow + allowlist check
│   ├── mcp.ts                   MCP tool/resource registration
│   ├── tools/
│   │   ├── list_skills.ts       reads /skills/ directory, returns skill metadata
│   │   ├── get_skill_context.ts reads context.md, schema.md, examples.sql for a named skill
│   │   └── run_bq_query.ts      executes a SQL query against the BQ project, scoped by descriptor.json
│   └── lib/
│       ├── bigquery.ts          BQ client wrapper with byte cap, row cap, read-only enforcement
│       └── skill-loader.ts      filesystem walker that validates skill folder structure
└── README.md
```

Key implementation notes (for whoever writes the skeleton):

- **`run_bq_query` MUST**:
  - Parse the SQL with a real SQL parser (or use BQ's `dry_run` job to detect referenced tables) and reject any query touching a table not in the current skill's `descriptor.json` allowlist.
  - Enforce `maximumBytesBilled` on the job to the value in `descriptor.json` (default 2 GiB).
  - Enforce `MAX_ROWS_RETURNED` (default 1000).
  - Reject any statement that isn't `SELECT` — no DML, no DDL, no procedures.
- **OAuth flow**:
  - `/auth/github/login` redirects to GitHub
  - `/auth/github/callback` exchanges the code, fetches the user's GitHub login, checks against `MCP_ALLOWED_USERS`, issues a session cookie (JWT signed with a server-side secret).
  - Every MCP request requires the session cookie.
- **Skill discovery**:
  - On startup, scan `/skills/` for subdirectories with a valid `descriptor.json`.
  - Watch the directory (or reload on container restart) so new skills appear without code changes.

## Step B — Dockerfile

```dockerfile
# Multi-stage build for small image
FROM node:22-alpine AS builder
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY tsconfig.json ./
COPY src ./src
RUN npm run build

FROM node:22-alpine
WORKDIR /app
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/dist ./dist
COPY package.json ./
EXPOSE 3000
CMD ["node", "dist/index.js"]
```

Build:

```bash
cd /home/deploy/mcp-server
docker build -t mcp-server:latest .
```

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
      - PORT=3000

      # BigQuery
      - GCP_PROJECT=<client>-mds-prod
      - BQ_LOCATION=EU
      - GOOGLE_APPLICATION_CREDENTIALS=/secrets/bq-mcp-reader.json

      # Query limits (defaults; per-skill descriptor.json can override)
      - DEFAULT_MAX_QUERY_BYTES=2147483648
      - DEFAULT_MAX_ROWS=1000

      # Auth — GitHub OAuth
      - GITHUB_OAUTH_CLIENT_ID=${GITHUB_OAUTH_CLIENT_ID}
      - GITHUB_OAUTH_CLIENT_SECRET=${GITHUB_OAUTH_CLIENT_SECRET}
      - MCP_ALLOWED_USERS=["acme-cto","acme-data-analyst"]
      - SESSION_SECRET=${SESSION_SECRET}   # generate: openssl rand -hex 32

      # Public URL (used for OAuth callback)
      - PUBLIC_URL=https://mcp.<client-domain>.com

    volumes:
      - /home/deploy/secrets/bq-mcp-reader.json:/secrets/bq-mcp-reader.json:ro
      - /home/deploy/mcp-skills:/skills:ro
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.mcp.rule=Host(`mcp.<client-domain>.com`)"
      - "traefik.http.routers.mcp.entrypoints=websecure"
      - "traefik.http.routers.mcp.tls.certresolver=letsencrypt"
      - "traefik.http.services.mcp.loadbalancer.server.port=3000"

networks:
  traefik-net:
    external: true
EOF
```

Replace `<client>` and `<client-domain>.com` with actual values. Customize `MCP_ALLOWED_USERS` with the GitHub logins captured in Phase 3 Step 5.

## Step D — Generate the session secret and set env

```bash
# Generate a server-side secret used to sign session JWTs
export SESSION_SECRET=$(openssl rand -hex 32)

# Capture into a .env file (not committed)
cat > /home/deploy/mcp-server/.env <<EOF
GITHUB_OAUTH_CLIENT_ID=$GITHUB_OAUTH_CLIENT_ID
GITHUB_OAUTH_CLIENT_SECRET=$GITHUB_OAUTH_CLIENT_SECRET
SESSION_SECRET=$SESSION_SECRET
EOF
chmod 600 /home/deploy/mcp-server/.env
```

Docker Compose will load this automatically when starting from this directory.

## Step E — Create the skills directory

```bash
mkdir -p /home/deploy/mcp-skills
# Initially empty. The first skill is added in the next step.
```

## Step F — Bring up the container

```bash
cd /home/deploy/mcp-server
docker compose up -d

# Logs
docker logs mcp 2>&1 | tail -30
```

Expected log output:

```
[mcp] starting on port 3000
[mcp] connected to BigQuery project <client>-mds-prod (EU)
[mcp] GitHub OAuth configured, callback URL: https://mcp.<client>-mds.com/auth/github/callback
[mcp] allowlist: [acme-cto, acme-data-analyst]
[mcp] loaded 0 skills from /skills
[mcp] ready
```

If any line says `error` or `failed to start`: stop, fix, retry. Common issues at the bottom of this file.

## Step G — Verify TLS + OAuth from a browser

From the user's laptop browser:

1. Visit `https://mcp.<client-domain>.com/`
2. Expected: a landing page or simple "MCP server is running. Authenticate with GitHub to use." prompt.
3. Click "Sign in with GitHub" (or navigate to `/auth/github/login`).
4. Approve the OAuth app on GitHub.
5. Redirected back to the server, which sets a session cookie.
6. Visit `https://mcp.<client-domain>.com/healthz` — should return `{ ok: true, user: "your-github-login" }`.

If allowlist check fails:

```
{ error: "github_user_not_allowed", login: "some-other-user" }
```

Fix by editing `MCP_ALLOWED_USERS` in `docker-compose.yml` and restarting the container.

## Step H — Verify the MCP transport

claude.ai connects via the MCP protocol over Server-Sent Events. To verify the transport works:

```bash
# From the user's laptop, with the session cookie from the browser:
COOKIE="<paste the session cookie value>"
curl -N \
  -H "Cookie: mcp_session=$COOKIE" \
  -H "Accept: text/event-stream" \
  https://mcp.<client-domain>.com/mcp/sse
```

Expected: an open SSE stream that periodically sends heartbeats. Close with Ctrl+C.

If 401: cookie expired or invalid. Re-auth via browser.
If 404: SSE endpoint isn't wired. Check the MCP server code.
If 502 from Traefik: container can't be reached on `traefik-net`. Confirm `docker network inspect traefik-net` shows the `mcp` container.

## Step I — Bring up first skill (next reference)

Continue with [`mcp-first-skill-bootstrap.md`](mcp-first-skill-bootstrap.md) to scaffold the first skill folder. Without at least one skill, the MCP server runs but `list_skills()` returns empty.

## Common gotchas

- **Container loops on restart** → check `docker logs mcp` for the error. Usually a missing env var or bad credentials path.
- **`GOOGLE_APPLICATION_CREDENTIALS` not found** → the volume mount path inside the container must match the env var value (here: `/secrets/bq-mcp-reader.json`).
- **GitHub OAuth callback returns "redirect_uri mismatch"** → the URL configured in the GitHub OAuth app settings must match `PUBLIC_URL + /auth/github/callback` exactly. No trailing slash difference.
- **claude.ai connector "fails to connect"** → check that the MCP URL in claude.ai settings ends in `/mcp` (not just the bare hostname). Different MCP servers use different paths.
- **First query times out** → BigQuery cold start on a new project + `maximumBytesBilled` cap rejects oversized queries. Look at the BQ console job history for the exact error.
- **Session expires too often** → adjust the session TTL (default in the skeleton: 24h). Long sessions are fine for the PYME profile; rotate manually if a user is offboarded.

## Marker state after this step

```jsonc
{
  "stack": {
    "mcp": true
  },
  "decisions": {
    "mcp_endpoint": "https://mcp.<client-domain>.com/mcp",
    "mcp_auth": "github_oauth",
    "mcp_allowed_users": ["acme-cto", "acme-data-analyst"],
    "mcp_bq_service_account": "mcp-reader@<client>-mds-prod.iam.gserviceaccount.com",
    "mcp_server_path_on_vps": "/home/deploy/mcp-server",
    "mcp_skills_path_on_vps": "/home/deploy/mcp-skills"
  }
}
```
