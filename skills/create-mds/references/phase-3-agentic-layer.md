# Phase 3 — Agentic Layer Playbook (MCP server)

This is the orchestrator for Phase 3 of `create-mds`. Phase 1 and Phase 2 must be complete. By the end of Phase 3 the deployment has a **public, authenticated MCP server** that exposes the BigQuery warehouse to AI agents (claude.ai, Claude Code, Cursor, any MCP client) via a stable URL.

This is the phase that **turns the MDS into an agentic platform** — moving from passive reporting to natural-language query and incremental knowledge curation.

> **Phase 3 is opt-in but recommended — the agentic cherry on top, not a requirement.** A deployment is a valid, valuable Modern Data Stack at the **end of Phase 2** (raw ingested by dlt + dbt marts, queryable from the BQ console and any BI tool). Phase 3 is the heaviest, most exposed piece in the whole stack — a public server, a custom domain, TLS, OAuth, and an AI agent pointed at the warehouse — so it is **activated only for clients who want the agentic query layer.** Recommend it hard (it's where the stack's option value lives — see [the bottom of this file](#what-gets-lost-if-phase-3-is-skipped)); never impose it. If the client declines, stop after Phase 2 cleanly and record `"mcp": false` — the build is done.

> **Security framing — this layer exposes externally-ingested data to an AI agent.** The MCP layer is the one place where data that arrived from outside the org (synced via dlt) gets handed to an LLM that may also hold write capability. That makes it the highest-leverage attack surface in the stack. Three postures are baked into the steps below and are **not optional** once MCP is opted in: a **dedicated read-only BigQuery service account** (not the dlt writer), **write tools OFF by default** (and PR-not-push when on), and treating **all synced data as untrusted** in any context an agent reads it (prompt-injection vector). Read [`mcp-server-architecture.md`](mcp-server-architecture.md#security-model) for the rationale before deploying.

End state of Phase 3 (when opted in):

- An MCP server container running on the VPS behind Traefik with TLS
- A public HTTPS endpoint at `mcp.<client-domain>.com/mcp` (or similar)
- GitHub OAuth 2.1 protecting the endpoint with an allowlist of authorized users
- A **dedicated read-only** BigQuery service account scoped to read access on the analytics dataset (NOT the dlt writer SA)
- **Write tools OFF** (the default) — read-only query layer. If the client opts into write tools, they open a PR for human review, never push to `main` (see Step 0)
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

**Opt-in gate.** Phase 3 is opt-in. Before running any step below, confirm the client actually wants the agentic query layer:

```
The MDS is complete and working at the end of Phase 2 — raw data ingested,
dbt marts built, queryable from BigQuery and any BI tool.

Phase 3 adds the agentic layer: a public, authenticated MCP server so you (and
claude.ai / Claude Code / Cursor) can ask the warehouse questions in natural
language. It's the recommended cherry on top, but it's the heaviest, most-exposed
part of the stack (public server, your own domain, TLS, GitHub OAuth, an AI agent
pointed at your data).

Want to add it now? You can also stop here and add it later — invoke me again any time.
```

If the client declines: record `"mcp": false` in the marker (it's the default — see Step 10), report that the build is complete at Phase 2, and stop. Do not provision a domain, Traefik, or any server.

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
| Should the MCP also accept WRITE operations on the client repo? | Lets agents propose edits to `context.md` etc. — but as a **PR for human review**, never a direct push | **Default `no` (off) — opt-in later.** Write tools are the most dangerous, most complex surface; ship read-only first. Turn on only when the client explicitly wants chat-driven skill curation. When on, edits open a PR — see [`../../add-mcp-skill/references/mcp-github-writeback.md`](../../add-mcp-skill/references/mcp-github-writeback.md). |

If the user doesn't have a custom domain: this is a manual ceremony. Send them to Namecheap, Cloudflare, Hostinger Domains, or Porkbun. Wait until they have the domain.

> **Default the write-tools answer to "no".** Read-only is the lean, safe baseline: the agent can query the warehouse but cannot mutate the repo at all. Write tools are a strict add-on the client opts into later (re-run with `add-mcp-skill` or re-invoke Phase 3) — never the default on a fresh build.

> **Prompt-injection is a real vector here.** The data the agent reads (rows from `run_bq_query`, and the skill `.md` files) originated outside the org — it was synced in by dlt from external systems. A crafted string in that data ("ignore previous instructions and …") can try to steer the agent. With write tools OFF this is contained to read scope; with write tools ON it becomes a path to repo mutation, which is exactly why the write path must be PR-not-push (a human reviews every change). **Treat all synced data as untrusted in any context the agent reads it.** This is the central reason write tools are off by default.

---

## Step 1 — Architecture and component selection

Detailed instructions: [`mcp-server-architecture.md`](mcp-server-architecture.md).

Key decisions made by this playbook:

- **Implementation language**: FastMCP (Python) — the proven choice in the reference deployment, with `GitHubProvider` auth built in and a compact `server.py`. TypeScript with `@modelcontextprotocol/sdk` is a valid alternative.
- **Transport**: Streamable HTTP (remote MCP). Required for claude.ai compatibility — stdio is local-only.
- **Auth**: GitHub OAuth via FastMCP's `GitHubProvider` with a username allowlist (`ALLOWED_GITHUB_USERS`). Reasoning: the user already has a GitHub account, no password DB to manage, allowlist via env var.
- **Hosting**: container on the same VPS as the dlt + dbt pipeline, exposed via Traefik with Let's Encrypt TLS.
- **Storage**: stateless container. All state (skill files, allowlist, BQ creds) is mounted from the host or env vars.

Don't continue until the user agrees to these defaults (or specifies overrides — but v0.7.0 only documents the defaults; overrides are user-responsibility).

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

1. Open ports 80 and 443 on the VPS firewall (only these — the pipeline / SSH stay Tailscale-only).
2. Pull Traefik container, configure with file provider for static routes.
3. Configure Let's Encrypt ACME for automatic TLS.
4. Define the `mcp` router that will forward to the MCP container.

Verification: `curl -I https://mcp.<client-domain>.com/` returns a TLS cert from Let's Encrypt (and a 502 because the MCP container isn't up yet — that's expected at this stage).

---

## Step 4 — Create the dedicated read-only BigQuery service account

The MCP server reads from BigQuery with its **own, read-only identity** — **NOT** the dlt writer service account from Phase 1. This is non-negotiable: the server hands query results to an LLM, so its credential must be unable to write, delete, or read anything outside the analytics layer.

The default is **one read-only SA scoped to the analytics dataset**:

- `roles/bigquery.dataViewer` granted **on the analytics dataset only** (not project-wide) — the agent can read marts, nothing else
- `roles/bigquery.jobUser` at project level (required to run any query job)

```bash
# On the user's laptop, with gcloud authed (Phase 1 setup)
PROJECT_ID="<client>-mds-prod"
DATASET="analytics"                 # the dbt marts dataset from Phase 2
SA_NAME="mcp-reader"

gcloud iam service-accounts create $SA_NAME \
  --display-name="MCP server BigQuery reader (read-only)" \
  --project=$PROJECT_ID

SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# dataViewer scoped to the analytics DATASET (least privilege — not project-wide).
# Granted at the dataset level so the agent cannot read raw/staging or other datasets.
bq update --dataset \
  --source <(bq show --format=prettyjson "${PROJECT_ID}:${DATASET}" \
    | jq --arg sa "$SA_EMAIL" \
        '.access += [{"role":"READER","userByEmail":$sa}]') \
  "${PROJECT_ID}:${DATASET}"
# (Equivalent: edit the dataset's access list to add the SA as READER.)

# jobUser at project level — needed to run query jobs (does NOT grant data access)
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

> **Why a dedicated read-only SA (not the dlt writer)?** Principle of least privilege, sharpened by the threat model: this credential is reachable by an AI agent that reads untrusted, externally-synced data. It must be **unable** to write or delete in BigQuery, and **unable** to read raw/staging or any non-analytics dataset. If the MCP code or container is ever compromised — or the agent is steered by a prompt-injection payload in the data — the blast radius is "read the marts", nothing more. dlt keeps writing with its separate, more-privileged identity, which is never mounted into this container.

> **Hardening OPTION (not the default): per-skill service accounts.** A deployment that wants tighter isolation can give each MCP skill its own read-only SA scoped to just that skill's tables, and select the credential per skill. That's a documented option for high-sensitivity domains — **not** the default. Do **not** create N service accounts on a fresh build; one read-only SA scoped to the analytics dataset is the lean baseline. Add per-skill SAs only when a client explicitly needs that separation.

---

## Step 5 — Create the GitHub OAuth app

GitHub OAuth gates access to the MCP server. The user creates the OAuth app at https://github.com/settings/developers — manual ceremony.

Settings:

- **Application name**: `<client> MDS — MCP server`
- **Homepage URL**: `https://mcp.<client-domain>.com`
- **Authorization callback URL**: the callback `GitHubProvider` derives from `PUBLIC_BASE_URL` (confirm the exact path against the FastMCP version in use; it must match this setting exactly).

Generate a client secret (single-use display — capture immediately).

Capture into agent secrets:

- `GITHUB_CLIENT_ID`
- `GITHUB_CLIENT_SECRET`

Plus an allowlist of GitHub usernames that the MCP server will accept:

```bash
# ALLOWED_GITHUB_USERS env var — comma-separated; empty = any authenticated GitHub user
ALLOWED_GITHUB_USERS=acme-cto,acme-data-analyst
```

If — and only if — write tools are enabled (Step 0; **off by default**), also capture a **fine-grained PAT** scoped to the client repo only, with the permissions the write path needs to **open a PR** (`contents:write` to push a branch + `pull_requests:write` to open the PR) — supplied as `GITHUB_TOKEN`, rotated ~every 90 days. The write tools create a branch and open a PR for human review; they do **not** push to `main`. Exact mechanism and scopes: [`../../add-mcp-skill/references/mcp-github-writeback.md`](../../add-mcp-skill/references/mcp-github-writeback.md). Skip this entirely for the default read-only deployment.

> **Why GitHub OAuth specifically?** Three reasons: (1) the user already has GitHub credentials, no new password DB. (2) The MCP server reads/writes the client repo — same identity that's authorized for the repo is authorized for the MCP. (3) OAuth flow is standard; FastMCP's `GitHubProvider` handles it natively.

---

## Step 6 — Deploy the MCP server

Detailed instructions: [`mcp-bigquery-server-deploy.md`](mcp-bigquery-server-deploy.md).

Summary:

1. Clone the MCP server source (FastMCP / Python) into `/home/deploy/mcp-server/` on the VPS, and clone the client repo (which holds the skill folders) as a live clone at `/root/<client>-mds`.
   - The template skeleton lives at [`add-mcp-skill/templates/mcp-skeleton/`](../../add-mcp-skill/templates/mcp-skeleton/) — start from it. `pol-cc/skills-sapiens` remains the reference production deployment if you want a fuller example to crib from.
2. Build the Docker image (`python:3.12-slim`, `pip install -r requirements.txt`).
3. Run as a Docker container with:
   - Mount the **read-only** SA key `/home/deploy/secrets/bq-mcp-reader.json` read-only (the dlt writer key is never mounted here)
   - Mount the client-repo clone at `/repo` (read-only is enough for the default read-only deployment; the write tools, when enabled, need it read-write to stage a branch)
   - Env vars: `GCP_PROJECT`, `BQ_LOCATION`, `GCP_CREDS_PATH`, `GITHUB_CLIENT_ID/SECRET`, `ALLOWED_GITHUB_USERS`, `MAX_BYTES_BILLED`, `MAX_ROWS`, `PUBLIC_BASE_URL`. Write tools are **off by default** — set `GITHUB_TOKEN` (and the write flag) **only** if the client opted in at Step 0.
4. Register the container with Traefik via Docker labels.
5. Confirm TLS + that the `/mcp` endpoint demands auth.

Verification: `curl -I https://mcp.<client-domain>.com/mcp` returns a valid Let's Encrypt cert and a 401/406 (auth required), not a 502.

---

## Step 7 — Bootstrap the first skill

Detailed instructions: [`mcp-first-skill-bootstrap.md`](mcp-first-skill-bootstrap.md) and [`../../add-mcp-skill/references/mcp-skill-folder-pattern.md`](../../add-mcp-skill/references/mcp-skill-folder-pattern.md).

Summary:

1. Pick the domain (from Step 0 user input — e.g. `sales`).
2. Identify the BigQuery tables in scope (the marts the user already has).
3. Create the skill folder inside the client-repo live clone at `/root/<client>-mds/skills/<domain>/` (mounted into the container at `/repo/skills/`):
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

The skill folders already live **inside** the client repo (the VPS clone at `/repo` is that repo) — a local edit + push (or, if write tools are enabled, a merged PR) commits them by definition. What still needs committing is the **MCP server source** (`server.py`, `Dockerfile`, `requirements.txt`, `docker-compose.yml`, `deploy.sh`):

```bash
# On the user's laptop, in the client repo
# Pull the MCP server source from the VPS (the skills/ folders are already in the repo)
mkdir -p mcp-server
scp -r deploy@<client>-mds:/home/deploy/mcp-server/server.py \
       deploy@<client>-mds:/home/deploy/mcp-server/Dockerfile \
       deploy@<client>-mds:/home/deploy/mcp-server/requirements.txt \
       deploy@<client>-mds:/home/deploy/mcp-server/docker-compose.yml \
       mcp-server/

git add mcp-server/ skills/
git commit -m "Phase 3: deploy FastMCP server with first skill (<domain>)"
```

Secrets stay out of the repo:

- BigQuery service account key → only in `/home/deploy/secrets/` on VPS
- GitHub OAuth client secret and the write-tools PAT (`GITHUB_TOKEN`) → only in the container `.env`, captured locally in `~/.config/agentic-data-engineer/secrets/`

The repo includes `mcp-server/.env.example` showing the env-var shape with placeholder values.

---

## Step 10 — Update the marker

**Default (Phase 3 not opted in).** MCP is opt-in, so the default marker state is simply:

```jsonc
{
  "stack": {
    "mcp": false
  }
}
```

`"mcp": false` is the correct, complete end state for a deployment that stopped after Phase 2. Nothing else in this step applies — the build is done.

**When Phase 3 is opted in and deployed:**

```jsonc
{
  "stack": {
    "mcp": true
  },
  "decisions": {
    "mcp_endpoint": "https://mcp.<client-domain>.com/mcp",
    "mcp_domain": "mcp.<client-domain>.com",
    "mcp_impl": "fastmcp_python",
    "mcp_auth": "github_oauth",
    "mcp_allowed_users": ["acme-cto", "acme-data-analyst"],
    "mcp_write_tools": false,
    "mcp_bq_service_account": "mcp-reader@<project>.iam.gserviceaccount.com",
    "mcp_bq_read_scope": "dataset:analytics",
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

`"mcp_write_tools": false` is the default even when MCP is deployed — read-only is the baseline. Flip it to `true` only when the client opted into the PR-not-push write path (Step 0). Commit.

---

## Step 11 — Hand off

Report to the user:

- MCP endpoint URL
- GitHub usernames on the allowlist
- First skill registered + domain it covers
- claude.ai now has the connector
- "To add a new skill (e.g. finance, marketing), invoke me with 'add a finance skill to the MCP'. I'll use the `add-mcp-skill` skill."
- "Use `verify-pipeline` to confirm the MCP server's health alongside the dlt + dbt pipeline."

Phase 3 is complete. The MDS is now a full agentic platform.

---

## Write tools — the "propose a context.md edit from chat" feature (OFF by default)

Write tools let an authenticated agent **propose** an edit to a skill's markdown from chat. They are **off by default** — the lean, safe baseline is read-only — and are a strict opt-in (Step 0). They are the most dangerous, most complex surface in Phase 3, because they connect an LLM (reading untrusted, externally-synced data) to a repo that drives the agent's own behavior.

Two tools:

- `append_to_section(skill, file_key, section, text)` — append under an existing heading.
- `replace_in_file(skill, file_key, old, new)` — correct an existing definition.

When enabled, a user chatting with claude.ai can say "the way we count active customers is wrong — it should exclude returns" and the agent edits the relevant `context.md` section. Critically, the server does **NOT push to `main`** — it **creates a branch and opens a pull request** (via the GitHub API / `gh`) for a human to review and merge. The fix lands when a person approves it, not the instant the agent writes it. This is the guardrail that makes the prompt-injection vector survivable: even if injected data steers the agent into a malicious edit, the change is just a PR sitting for review, not a live mutation.

The full mechanism — fine-grained PAT scoped to this repo, branch creation, PR open via the GitHub API, the path-traversal safety model (the tools can only touch files inside an existing `skills/<skill>/` folder; they CANNOT create new skill folders or change `descriptor.json` allowlists) — is documented in [`../../add-mcp-skill/references/mcp-github-writeback.md`](../../add-mcp-skill/references/mcp-github-writeback.md) (owned and hardened separately). Defer to that doc for exact branch/PR specifics.

Enable by setting `GITHUB_TOKEN` plus the write-enable flag (Step 6) **only if the client opted in**. Default behavior — `GITHUB_TOKEN` unset — is read-only. Record the choice in the marker as `mcp_write_tools` (default `false`).

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

Skipping Phase 3 is a legitimate end state, not a half-finished build — the MDS still works for traditional BI (dashboards, scheduled reports, manual SQL via the BQ console). What the client *forgoes* by not opting in (the case for recommending it):

- Natural-language exploration of the warehouse from chat
- Per-skill "this is what these tables mean" curation that improves over time
- The compounding benefit of MCP — every new client app (a Slack bot, a custom dashboard, a future agent) connects to the same server with the same skills

Phase 3 is the **option value** of the architecture. It's the bet that AI agents will become a primary interface to data, not just an occasional tool. PYME deployments that don't yet have analyst capacity benefit most from this phase — it gives the founder a query interface they didn't otherwise have.
