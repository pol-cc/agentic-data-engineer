# MCP server architecture

Background reading for understanding what we're deploying in Phase 3 and why. No commands here — this file is conceptual. Concrete deployment lives in [`mcp-bigquery-server-deploy.md`](mcp-bigquery-server-deploy.md).

## What an MCP server is

The Model Context Protocol (MCP) is Anthropic's open standard for connecting AI agents to external tools and context. An **MCP server** is a process that exposes:

- **Tools** — callable functions the agent can invoke (e.g. `run_bq_query(sql)`)
- **Resources** — readable content the agent can fetch (e.g. `mcp-skills://sales/schema.md`)
- **Prompts** — pre-canned prompt templates (rarely used in the BQ-backed pattern)

An **MCP client** is anything that consumes these — claude.ai, Claude Code, Cursor, custom apps using the SDKs. The agent inside the client decides which tools to call and which resources to load, based on the user's natural-language request.

For the agentic-data-engineer stack, the MCP server is the **agentic layer**: a single endpoint that exposes the BigQuery warehouse to every compliant AI client. One server, many consumers.

## Why a server (instead of Claude Code-native skills)

Claude Code already supports skills locally (the `.claude/skills/` pattern that this repo uses). Why not just put the BQ querying knowledge in Claude Code skills?

Three reasons:

1. **Multi-client reach.** A Claude Code skill helps the user in their terminal. An MCP server helps anyone with an MCP client — claude.ai web, Cursor, custom apps, future agents. One deploy, multiple surfaces.
2. **Persistent context curation.** MCP skills are stored on the server. When the user refines a `context.md` ("our 'active customer' definition changed"), every future query benefits. Claude Code skills live in the user's filesystem and don't sync across users.
3. **Authentication separation.** The MCP server can authorize *who* queries the warehouse independently of *what* tools they have. The skills-as-Claude-Code pattern has no auth — you have access to everything the client has.

The two patterns coexist. Claude Code skills drive the *evolution* of the system (this repo). The MCP server drives the *use* of the system (the warehouse). They don't compete.

## The BigQuery-backed pattern

The reference design used here (taken from the `skills-sapiens` reference deployment) is:

```
                                                       ┌────────────────┐
                                            ┌────────► │   BigQuery     │
                                            │          │  (analytics_*) │
┌─────────────────────┐  HTTPS+GitHubOAuth ┌───────────┴┐                │
│  AI client          │ ────────────────► │  MCP server ├──── run_bq_query
│  (claude.ai,        │ ◄──────────────── │ (FastMCP)   │ ─── list_skills
│   Claude Code,      │    JSON results   │  on VPS     │ ─── get_skill_context
│   Cursor, etc)      │                   └──────┬──────┘ ─── append_to_section ┐ write
└─────────────────────┘                          │        ─── replace_in_file   ┘ tools
                                                 │                                │
                                                 ▼                                │ commit + push
                                       /repo  (live git clone, rw)  ◄─────────────┘
                                       └── skills/
                                           ├── sales/
                                           │   ├── descriptor.json
                                           │   ├── context.md
                                           │   ├── schema.md
                                           │   └── examples.sql
                                           ├── finance/
                                           │   └── ...
                                           └── operations/
                                               └── ...
                                                 │ git push origin main
                                                 ▼
                                       origin/main  (GitHub — source of truth)
```

The MCP server is **generic** — it doesn't hardcode any client's business logic. The per-skill folders are **specific** — they teach the agent what the warehouse looks like for *this* deployment.

When an agent connects, it:

1. Calls `list_skills()` to discover available domains.
2. Calls `get_skill_context(skill_name)` to load the `context.md`, `schema.md`, and `examples.sql` for the domain it cares about.
3. Composes a SQL query against the BigQuery tables declared in `descriptor.json`.
4. Calls `run_bq_query(sql)` to execute. The server enforces a max-bytes cap (default 2 GiB) and only allows reads.
5. Returns rows to the agent, which synthesizes the answer for the user.

## Why GitHub OAuth for auth

The MCP server is public-facing (it must be, to connect to claude.ai which doesn't speak Tailscale). It needs auth. Options considered:

| Auth method | Pros | Cons |
|---|---|---|
| **GitHub OAuth 2.1** ✓ chosen | User already has GitHub. No password DB. Allowlist by username. Same identity that owns the client repo. | OAuth flow has slight UX friction on first connect. |
| API key / bearer token | Simplest. Just a string. | Secret distribution problem. Rotation is manual. Anyone who has the key has full access — no per-user revocation. |
| OAuth via Auth0 / Clerk / etc. | Most features (MFA, social, etc.) | Vendor lock-in, monthly cost, principle 7 violation. |
| Mutual TLS | Tightest security | Painful UX. Every client needs a cert. claude.ai doesn't support it today. |

GitHub OAuth wins for this profile (PYME, single-digit users, integrates with existing devops).

> **The allowlist matters.** GitHub OAuth just confirms identity — the server enforces *which* identities are allowed. The `ALLOWED_GITHUB_USERS` env var is a comma-separated list of logins; `_authorize_request()` reads the `login` claim from the access token and checks it. **An empty allowlist means any authenticated GitHub user** — so set an explicit list, especially when write tools are enabled. Rotate by editing the env var and restarting the container.

## Why a container on the existing VPS

We deploy the MCP server as a Docker container on the same VPS that hosts Airbyte and dbt. Alternatives:

| Hosting | Pros | Cons |
|---|---|---|
| **Same VPS, Docker container** ✓ | Zero extra infrastructure. Shares Tailscale. Cheap. | If the VPS dies, MCP and pipeline both go down. |
| Separate VPS for MCP | Isolation. | Doubles the monthly cost. |
| Serverless (Cloud Run, Fly.io, Vercel Functions) | Auto-scales. No VPS management. | Cold start latency hurts the chat UX. Vendor lock-in. Egress charges if querying BQ from another region. |
| Anthropic's hosting (future managed MCP) | Zero ops. | Doesn't exist yet for self-defined servers as of v0.3.0. |

The same-VPS choice gives Phase 3 a near-zero marginal cost. The MCP container is small (~50 MB image, ~100 MB RAM running) and idles cheaply.

## Why FastMCP (Python)

The reference deployment (`skills-sapiens`) is built on **FastMCP** in Python, and that's the default we recommend for Phase 3:

- **Proven in production.** The `skills-sapiens` server runs FastMCP with Streamable HTTP behind Traefik — this is a deployed, iterated-on design, not a sketch.
- **Auth is built in.** FastMCP ships `GitHubProvider`, which handles the GitHub OAuth handshake and token verification. No hand-rolled OAuth routes or session-cookie plumbing — the allowlist check is a few lines reading the `login` claim.
- **Compact code.** The BigQuery client (`google.cloud.bigquery`) plus the git-subprocess calls for the write tools fit in a single small `server.py`. Less surface area to maintain.
- **Streamable HTTP transport** is first-class, which is what claude.ai's remote connectors speak.

The trade-off is image size: a `python:3.12-slim` image lands around 150-200 MB versus ~50 MB for Node/Alpine. For a container that idles cheaply on the existing VPS, that's a non-issue.

TypeScript with `@modelcontextprotocol/sdk` remains a fully valid alternative — swap the framework, keep the rest (principle 7, escape hatch).

## Skill folder pattern: descriptor + context + schema + examples

Every skill is four files. Each has one job. The agent loads all four when the skill is selected.

**`descriptor.json`** — machine-readable. Declares the allowed BigQuery tables and query limits. The MCP server uses this to **enforce** that a query against this skill cannot touch tables outside scope (no SQL injection escape via cross-dataset joins).

```jsonc
{
  "name": "sales",
  "description": "Sales analytics: orders, revenue, customers, channels.",
  "tables": [
    "<project>.analytics.dim_customers",
    "<project>.analytics.fact_orders",
    "<project>.analytics.revenue_monthly"
  ],
  "max_query_bytes": 2147483648,
  "max_rows": 1000
}
```

**`context.md`** — human-readable. Business glossary. What does "customer" mean here? What's a "channel"? What's the difference between gross and net revenue? The LLM reads this before writing SQL.

**`schema.md`** — per-table column documentation. For each table: meaningful columns, types, gotchas, joins. Think of it as the SQL-style equivalent of dbt's `schema.yml` but written for an LLM consumer.

**`examples.sql`** — 3-5 canonical queries the LLM can pattern-match against. Coverage: "total revenue by month", "top N customers by lifetime value", "channel mix this quarter vs last".

This four-file pattern is documented in detail at [`../../add-mcp-skill/references/mcp-skill-folder-pattern.md`](../../add-mcp-skill/references/mcp-skill-folder-pattern.md). When `add-mcp-skill` is invoked, it scaffolds these four files for the new domain.

## How skills evolve

The most important property of the MCP server in production: **skills improve over time, in place**.

A typical week:

- The user asks claude.ai a question. The agent writes a query. The query is wrong because `context.md` is missing the definition of "active customer".
- The user notices, points out the error.
- In the same chat (or in a separate Claude Code session), the user (or the agent, with the `append_to_section` / `replace_in_file` write tools) updates `context.md` to clarify the definition. The edit is committed + pushed to `main` automatically — see [`../../add-mcp-skill/references/mcp-github-writeback.md`](../../add-mcp-skill/references/mcp-github-writeback.md).
- Next time anyone asks a similar question, the answer is correct.

The skill folder is a **versioned, curated knowledge base** that gets sharper every time it's used. Git history shows the evolution. PRs can be used for changes that need review.

This is the loop that justifies Phase 3. Without it, the warehouse is passive; with it, the warehouse learns.

## Write tools — first-class, not deferred

The server exposes **two write tools** — `append_to_section` and `replace_in_file` — that let an authenticated agent edit a skill's markdown and have the change committed + pushed to the client repo's `main` branch automatically. This is in production in `skills-sapiens` and is a core part of the value, not an optional extra.

The value is the **iteration loop**: when claude.ai gets an answer wrong because `context.md` is missing a definition, the user corrects it *in the same chat*, the agent patches the file, and the fix is live for everyone on the next query — no SSH, no redeploy. This is what makes the warehouse *learn* rather than stay passive.

Safety comes from **tool scoping**, not filesystem perms: the tools can only touch files inside an existing `skills/<skill>/` folder (a path-traversal guard enforces this). They cannot create new skill folders, change `descriptor.json` allowlists, or touch server code.

Full mechanism — the fine-grained PAT, `claude-bot` identity + co-author trailer, `_sync_to_origin()` self-healing, `_commit_and_push()` rollback, the safety model, and how it coexists with `deploy.sh` — is documented in [`../../add-mcp-skill/references/mcp-github-writeback.md`](../../add-mcp-skill/references/mcp-github-writeback.md). To run read-only, omit the PAT.

## What this Phase 3 deliberately does NOT do

To keep scope manageable in v0.3.0:

- **No multi-tenant MCP.** One MCP server serves one client deployment. A "host multiple clients on one MCP server" pattern is possible but adds complexity.
- **No audit log.** Queries are not persisted by the server. BigQuery's own audit logs cover usage (`INFORMATION_SCHEMA.JOBS_BY_PROJECT`); write-tool edits are recorded as commits in `git log`. A future `add-mcp-audit-log` skill could add server-side logging if compliance demands it.
- **No rate limiting.** GitHub OAuth + small allowlist + BQ's own quotas are considered sufficient at PYME scale. Add a rate limiter if abuse becomes a concern.

These are conscious omissions, not oversights. Add them later when the use case demands.
