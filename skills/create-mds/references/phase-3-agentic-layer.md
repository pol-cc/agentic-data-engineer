# Phase 3 — Agentic Layer Playbook (MCP server)

This is the orchestrator for Phase 3 of `create-mds`. Phase 1 and Phase 2 must be complete. By the end of Phase 3 the deployment has a **public, authenticated MCP server** that exposes the BigQuery warehouse to AI agents (claude.ai, Claude Code, Cursor, any MCP client) via a stable URL.

This is the phase that **turns the MDS into an agentic platform** — moving from passive reporting to natural-language query and incremental knowledge curation.

End state of Phase 3:

- An MCP server container running on the VPS behind Traefik with TLS
- A public HTTPS endpoint at `mcp.<client-domain>.com/mcp` (or similar)
- GitHub OAuth 2.1 protecting the endpoint with an allowlist of authorized users
- A BigQuery service account scoped to read access on `analytics_*` datasets
- One "skill" registered (`descriptor.json` + `context.md` + `schema.md` + `examples.sql`) covering a domain the user picks (typically sales or finance — whatever has the cleanest marts ready)
- The MCP server code and skill files committed to the client repo
- The marker file updated; `verify-pipeline` includes the MCP health check

Estimated wall-clock time: **2-3 hours** for a first deployment, mostly waiting on DNS propagation and Let's Encrypt issuance. Active human attention: ~30 minutes (GitHub OAuth app creation and domain registration if needed).

---

## Preflight

```bash
# Confirm Phase 1 + 2 complete
if [ ! -f .agentic-data-engineer.json ]; then
  echo "[abort] not a managed MDS deployment"
  exit 1
fi

# Phase 2 must be present
jq -e '.stack.transform == "dbt_vps"' .agentic-data-engineer.json > /dev/null || {
  echo "[abort] MDS is at Phase 1 only — run create-mds Phase 2 first"
  echo "the MCP server is only useful with at least one mart to query"
  exit 1
}

# Confirm MCP hasn't already been set up
if jq -e '.stack.mcp == true' .agentic-data-engineer.json > /dev/null; then
  echo "[abort] MCP server already deployed for this MDS"
  echo "use add-mcp-skill to add skills, or troubleshoot to debug"
  exit 1
fi

# Confirm at least one mart exists (otherwise the agent has nothing useful to query)
# This is a soft check — proceed but warn if no marts
```

Capture from the marker for later:

- `decisions.bq_project_id` → MCP server reads from here
- `decisions.bq_location` → for BQ client config
- `decisions.github_repo` → where the MCP code and skills live

---

## Step 0 — User input

Ask the user:

| Question | Used for | Example |
|---|---|---|
| Custom domain for the MCP endpoint | DNS + Traefik | `mcp.acme-bakery.com` (the user must own this) |
| GitHub usernames allowed to use the MCP | Auth allowlist | `["acme-cto", "acme-data-analyst"]` |
| Which BigQuery domain to expose first | Bootstrap the first skill | `sales` (and we'll target `analytics.dim_customers`, `analytics.fact_orders`, etc.) |
| Should the MCP also accept WRITE operations on the client repo? | Enables agents to update context.md files via commits | `yes` (recommended — see "Write tools" below) / `no` |

If the user doesn't have a custom domain: this is a manual ceremony. Send them to Namecheap, Cloudflare, Hostinger Domains, or Porkbun. Wait until they have the domain.

---

## Step 1 — Architecture and component selection

Detailed instructions: [`mcp-server-architecture.md`](mcp-server-architecture.md).

Key decisions made by this playbook:

- **Implementation language**: TypeScript using `@modelcontextprotocol/sdk` (Anthropic's official SDK). Reasoning: best documentation, most examples, fastest startup, smallest container.
- **Transport**: HTTP/SSE (remote MCP). Required for claude.ai compatibility — stdio is local-only.
- **Auth**: GitHub OAuth 2.1 with allowlist. Reasoning: the user already has a GitHub account, no password DB to manage, allowlist via env var.
- **Hosting**: container on the same VPS as Airbyte/dbt, exposed via Traefik with Let's Encrypt TLS.
- **Storage**: stateless container. All state (skill files, allowlist, BQ creds) is mounted from the host or env vars.

Don't continue until the user agrees to these defaults (or specifies overrides — but v0.3.0 only documents the defaults; overrides are user-responsibility).

---

## Step 2 — Set up DNS

```bash
# The user creates an A record pointing to the VPS public IP.
# (Note: Tailscale doesn't help here — the MCP server must be reachable from claude.ai, which is on the public internet.)
```

For the user's DNS provider:

| Record type | Name | Value |
|---|---|---|
| A | `mcp` (or full `mcp.acme-bakery.com`) | VPS public IPv4 |

Wait for propagation:

```bash
dig +short mcp.<client-domain>.com
# expected: VPS public IPv4
```

Propagation usually < 5 min for fresh records, can take up to 1h with some providers.

> **Why not Tailscale for the MCP endpoint?** Tailscale would block the connection from claude.ai (which is not on the tailnet). The MCP endpoint is the one public-facing service this stack has. Traefik + Let's Encrypt + GitHub OAuth provide layered defense.

---

## Step 3 — Install Traefik on the VPS

Detailed instructions: [`traefik-tls-setup.md`](traefik-tls-setup.md).

Summary:

1. Open ports 80 and 443 on the VPS firewall (only these — Airbyte stays Tailscale-only).
2. Pull Traefik container, configure with file provider for static routes.
3. Configure Let's Encrypt ACME for automatic TLS.
4. Define the `mcp` router that will forward to the MCP container.

Verification: `curl -I https://mcp.<client-domain>.com/` returns a TLS cert from Let's Encrypt (and a 502 because the MCP container isn't up yet — that's expected at this stage).

---

## Step 4 — Create the BigQuery read-only service account

The MCP server reads from BigQuery. Reusing the Airbyte writer service account is overprivileged.

```bash
# On the user's laptop, with gcloud authed (Phase 1 setup)
PROJECT_ID="<client>-mds-prod"
SA_NAME="mcp-reader"

gcloud iam service-accounts create $SA_NAME \
  --display-name="MCP server BigQuery reader" \
  --project=$PROJECT_ID

SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# Bindings: bigquery.dataViewer at project level (read all data),
#          bigquery.jobUser (run queries)
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/bigquery.dataViewer"

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/bigquery.jobUser"

# Generate key
KEY_PATH=~/.config/agentic-data-engineer/secrets/<client>-bq-mcp-reader.json
gcloud iam service-accounts keys create $KEY_PATH \
  --iam-account=$SA_EMAIL
chmod 600 $KEY_PATH

# Transfer to VPS
scp $KEY_PATH deploy@<client>-mds:/home/deploy/secrets/bq-mcp-reader.json
ssh deploy@<client>-mds "chmod 600 /home/deploy/secrets/bq-mcp-reader.json"
```

> **Why a separate service account?** Principle of least privilege. The MCP server should be unable to write to BigQuery. If the MCP code or container is ever compromised, the attacker can read all data but cannot modify or delete it. Airbyte continues to write with its separate, more-privileged identity.

---

## Step 5 — Create the GitHub OAuth app

GitHub OAuth gates access to the MCP server. The user creates the OAuth app at https://github.com/settings/developers — manual ceremony.

Settings:

- **Application name**: `<client> MDS — MCP server`
- **Homepage URL**: `https://mcp.<client-domain>.com`
- **Authorization callback URL**: `https://mcp.<client-domain>.com/auth/github/callback`

Generate a client secret (single-use display — capture immediately).

Capture into agent secrets:

- `GITHUB_OAUTH_CLIENT_ID`
- `GITHUB_OAUTH_CLIENT_SECRET`

Plus an allowlist of GitHub usernames that the MCP server will accept:

```jsonc
// MCP_ALLOWED_USERS env var (JSON array)
["acme-cto", "acme-data-analyst"]
```

> **Why GitHub OAuth specifically?** Three reasons: (1) the user already has GitHub credentials, no new password DB. (2) The MCP server reads/writes the client repo — same identity that's authorized for the repo is authorized for the MCP. (3) OAuth flow is standard; many MCP client libraries handle it natively.

---

## Step 6 — Deploy the MCP server

Detailed instructions: [`mcp-bigquery-server-deploy.md`](mcp-bigquery-server-deploy.md).

Summary:

1. Clone the MCP server source code into `/home/deploy/mcp-server/` on the VPS.
   - For v0.3.0: the template skeleton lives at [`add-mcp-skill/templates/mcp-skeleton/`](../../add-mcp-skill/templates/mcp-skeleton/) (to be written). Until that template exists, point at the user's choice of starter — `pol-cc/skills-sapiens` is the reference deployment.
2. Build the Docker image.
3. Run as a Docker container with:
   - Mount `/home/deploy/secrets/bq-mcp-reader.json` read-only
   - Mount `/home/deploy/mcp-skills/` (skill files) read-write
   - Env vars for `GCP_PROJECT`, `BQ_LOCATION`, `GITHUB_OAUTH_CLIENT_ID/SECRET`, `MCP_ALLOWED_USERS`, `MAX_QUERY_BYTES`
4. Register the container with Traefik via Docker labels.
5. Confirm TLS handshake + GitHub OAuth login from a browser.

Verification: visit `https://mcp.<client-domain>.com/` in a browser, complete GitHub OAuth, see a basic landing page or 200 response.

---

## Step 7 — Bootstrap the first skill

Detailed instructions: [`mcp-first-skill-bootstrap.md`](mcp-first-skill-bootstrap.md) and [`../../add-mcp-skill/references/mcp-skill-folder-pattern.md`](../../add-mcp-skill/references/mcp-skill-folder-pattern.md).

Summary:

1. Pick the domain (from Step 0 user input — e.g. `sales`).
2. Identify the BigQuery tables in scope (the marts the user already has).
3. Create `/home/deploy/mcp-skills/<domain>/`:
   - `descriptor.json` — allowed tables, query limits
   - `context.md` — business glossary for this domain
   - `schema.md` — per-table column docs and gotchas
   - `examples.sql` — 3-5 canonical queries
4. Restart the MCP container to pick up the new skill.
5. Verify the skill appears in `list_skills()` from an MCP client.

---

## Step 8 — Connect from claude.ai

The user adds the MCP server to claude.ai as a remote connector. Manual ceremony.

1. In claude.ai → Settings → Connectors → "Add custom connector".
2. Name: `<client> MDS`.
3. URL: `https://mcp.<client-domain>.com/mcp`.
4. Complete GitHub OAuth flow when prompted.
5. Test by asking claude.ai: "What skills do you have available from the <client> MDS connector?" — it should list the domains.

Then a real test:

> "Using the <client> MDS, what was last month's revenue compared to the previous month?"

claude.ai loads the `sales` skill (or whatever the first skill is), composes a SQL query from `context.md` + `schema.md` + `examples.sql`, runs it via the MCP server, returns a synthesized answer with the row data.

---

## Step 9 — Commit MCP code and skills to the client repo

```bash
# On the user's laptop, in the client repo
# Pull the MCP server source (already on the VPS) and the skill files
mkdir -p mcp-server mcp-skills
scp -r deploy@<client>-mds:/home/deploy/mcp-server/* mcp-server/
scp -r deploy@<client>-mds:/home/deploy/mcp-skills/* mcp-skills/

git add mcp-server/ mcp-skills/
git commit -m "Phase 3: deploy MCP server with first skill (<domain>)"
```

Secrets stay out of the repo:

- BigQuery service account key → only in `/home/deploy/secrets/` on VPS
- GitHub OAuth client secret → only in container env var, captured locally in `~/.config/agentic-data-engineer/secrets/`

The repo includes `mcp-server/.env.example` showing the env-var shape with placeholder values.

---

## Step 10 — Update the marker

```jsonc
{
  "stack": {
    "mcp": true
  },
  "decisions": {
    "mcp_endpoint": "https://mcp.<client-domain>.com/mcp",
    "mcp_domain": "mcp.<client-domain>.com",
    "mcp_auth": "github_oauth",
    "mcp_allowed_users": ["acme-cto", "acme-data-analyst"],
    "mcp_bq_service_account": "mcp-reader@<project>.iam.gserviceaccount.com",
    "mcp_first_skill": "<domain>",
    "traefik_used": true,
    "github_oauth_client_id_ref": "secrets/<client>-mcp-github-oauth.json"
  },
  "history": [
    ...,
    {"date": "<today>", "skill": "create-mds", "phase": 3, "outcome": "ok"}
  ]
}
```

Commit.

---

## Step 11 — Hand off

Report to the user:

- MCP endpoint URL
- GitHub usernames on the allowlist
- First skill registered + domain it covers
- claude.ai now has the connector
- "To add a new skill (e.g. finance, marketing), invoke me with 'add a finance skill to the MCP'. I'll use the `add-mcp-skill` skill."
- "Use `verify-pipeline` to confirm the MCP server's health alongside Airbyte and dbt."

Phase 3 is complete. The MDS is now a full agentic platform.

---

## Write tools — the "edit context.md from chat" feature

The reference deployment (`skills-sapiens`) implements **write tools** that let an authenticated agent commit changes to the client repo. Specifically:

- `propose_skill_edit(skill_name, file, content_diff)` — proposes a change to a `context.md` / `schema.md` file, opens a PR on the client repo.
- Approval is per-call, prompted to the user in chat. No silent writes.

This means a user chatting with claude.ai can say "the way we count active customers is wrong — it should exclude returns" and the agent can:

1. Find the relevant `context.md` section
2. Propose the edit
3. Open a PR
4. The user reviews on GitHub

Phase 3 v0.3.0 documents this pattern but does not auto-enable it. Enabling write tools requires extra OAuth scope and PR review discipline. Future skill (`enable-mcp-write-tools`) will cover the upgrade path. Until then: read-only MCP is safer for v0.3.0.

---

## Idempotence guarantees

If Phase 3 is interrupted:

- **DNS exists but Traefik not installed** → resume at Step 3
- **Traefik up but TLS cert not issued** → check ACME logs; common cause is firewall blocking port 80 inbound
- **MCP container deployed but no skill** → resume at Step 7
- **Skill exists but not connected to claude.ai** → resume at Step 8

Each reference's preflight catches the state mismatch.

---

## What gets lost if Phase 3 is skipped

A deployment can stop at Phase 2 forever — the MDS still works for traditional BI (dashboards, scheduled reports, manual SQL via the BQ console). What you lose:

- Natural-language exploration of the warehouse from chat
- Per-skill "this is what these tables mean" curation that improves over time
- The compounding benefit of MCP — every new client app (a Slack bot, a custom dashboard, a future agent) connects to the same server with the same skills

Phase 3 is the **option value** of the architecture. It's the bet that AI agents will become a primary interface to data, not just an occasional tool. PYME deployments that don't yet have analyst capacity benefit most from this phase — it gives the founder a query interface they didn't otherwise have.
