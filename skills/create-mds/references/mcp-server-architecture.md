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
┌─────────────────────┐    HTTPS+OAuth     ┌───────────┴┐                │
│  AI client          │ ────────────────► │  MCP server ├──── run_bq_query
│  (claude.ai,        │ ◄──────────────── │ (container) │ ─── list_skills
│   Claude Code,      │    JSON results   │  on VPS     │ ─── get_skill_context
│   Cursor, etc)      │                   └──────┬──────┘ ─── (optional) propose_edit
└─────────────────────┘                          │
                                                 │
                                                 ▼
                                       /home/deploy/mcp-skills/
                                       ├── sales/
                                       │   ├── descriptor.json
                                       │   ├── context.md
                                       │   ├── schema.md
                                       │   └── examples.sql
                                       ├── finance/
                                       │   └── ...
                                       └── operations/
                                           └── ...
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

> **The allowlist matters.** GitHub OAuth just confirms identity — the server enforces *which* identities are allowed. Without the allowlist, anyone with a GitHub account could connect. The `MCP_ALLOWED_USERS` env var is a simple JSON array; rotate by editing and restarting the container.

## Why a container on the existing VPS

We deploy the MCP server as a Docker container on the same VPS that hosts Airbyte and dbt. Alternatives:

| Hosting | Pros | Cons |
|---|---|---|
| **Same VPS, Docker container** ✓ | Zero extra infrastructure. Shares Tailscale. Cheap. | If the VPS dies, MCP and pipeline both go down. |
| Separate VPS for MCP | Isolation. | Doubles the monthly cost. |
| Serverless (Cloud Run, Fly.io, Vercel Functions) | Auto-scales. No VPS management. | Cold start latency hurts the chat UX. Vendor lock-in. Egress charges if querying BQ from another region. |
| Anthropic's hosting (future managed MCP) | Zero ops. | Doesn't exist yet for self-defined servers as of v0.3.0. |

The same-VPS choice gives Phase 3 a near-zero marginal cost. The MCP container is small (~50 MB image, ~100 MB RAM running) and idles cheaply.

## Why TypeScript with `@modelcontextprotocol/sdk`

The MCP SDK is available in Python and TypeScript. For Phase 3 we recommend **TypeScript**:

- Larger ecosystem for HTTP servers and OAuth flows on Node.
- Anthropic's "remote MCP" examples are TS-first.
- The container is smaller (~50 MB vs ~200 MB for Python).
- The BigQuery Node SDK is mature.

For users who prefer Python: it works equally well. Swap the framework, keep the rest. This is principle 7 (escape hatch).

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
- In the same chat (or in a separate Claude Code session), the user (or the agent, with the `propose_edit` write tool) updates `context.md` to clarify the definition.
- Next time anyone asks a similar question, the answer is correct.

The skill folder is a **versioned, curated knowledge base** that gets sharper every time it's used. Git history shows the evolution. PRs can be used for changes that need review.

This is the loop that justifies Phase 3. Without it, the warehouse is passive; with it, the warehouse learns.

## What this Phase 3 deliberately does NOT do

To keep scope manageable in v0.3.0:

- **No write tools enabled by default.** `propose_edit` is documented but disabled. Read-only MCP is the v0.3.0 default. Enabling writes requires additional OAuth scope and PR review discipline — future work.
- **No multi-tenant MCP.** One MCP server serves one client deployment. A "host multiple clients on one MCP server" pattern is possible but adds complexity.
- **No audit log.** Queries are not persisted by the server. BigQuery's own audit logs cover usage (`INFORMATION_SCHEMA.JOBS_BY_PROJECT`). A future `add-mcp-audit-log` skill could add server-side logging if compliance demands it.
- **No rate limiting.** GitHub OAuth + small allowlist + BQ's own quotas are considered sufficient at PYME scale. Add `rate-limiter-flexible` (Node) or equivalent if abuse becomes a concern.

These are conscious omissions, not oversights. Add them later when the use case demands.
