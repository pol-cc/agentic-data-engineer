# Stack Rationale

Why each piece of the stack is what it is. Every choice has alternatives — this file explains the trade-offs so a skill (or a human reading the repo) can defend the decision or override it consciously.

Read every section below as a **decision point, not a commandment.** Each component is a *default with reasons* — a strong recommendation the agent defends, paired with the alternatives it considered and the conditions under which it would deviate. The opinion is real and worth defending (that is the project's value), but it is offered, not imposed (principle 8 — recommend strongly, impose nothing).

**The discovery step overrides these defaults.** Before any deployment, the agent asks what the user already has and adapts; an existing VPS, warehouse, VPN, or cloud preference wins over the recommendation here. See [`discovery-and-adaptation.md`](discovery-and-adaptation.md) for how that override works and gets recorded.

---

## Tailscale — the network backbone

**Chosen because**: zero-config mesh VPN with free tier sized for a PYME (3 users, 100 devices), works on every OS, and gives every host a stable hostname inside the tailnet. The on-prem reachability story is unmatched — a SQL Server in a small company's office becomes reachable from a cloud VPS without firewall changes, port forwarding, or IT involvement.

**Alternatives considered**:
- *WireGuard (raw)* — more work to set up, no built-in DNS, no SSO. Tailscale is WireGuard under the hood with the boring parts solved.
- *Traditional VPN (OpenVPN, IPsec)* — heavier, requires a concentrator, no zero-config story.
- *Cloudflare Tunnels* — good for exposing services to the internet, but doesn't solve the on-prem-database-to-cloud-VPS direction.
- *Public IPs + firewall rules* — fragile, requires ISP cooperation, fails entirely behind CG-NAT.

**When to reconsider**: if the client outgrows Tailscale's free tier and the cost approaches Hostinger VPS spend, evaluate WireGuard (principle 7 — escape hatch is open).

---

## Hostinger VPS — the compute host

**Chosen because**: KVM 2 plan at ~$5-8/month gives 2 vCPU, 8 GB RAM, 100 GB NVMe — enough headroom for Airbyte OSS, dbt, and an MCP server container with comfort. Hostinger's API and CLI allow headless provisioning.

**Alternatives considered**:
- *Hetzner Cloud* — better price/performance, but no managed snapshots in the cheapest tier. Equivalent choice if the client prefers EU billing or has Hetzner credits.
- *DigitalOcean* — similar tier, slightly more expensive ($12/mo for equivalent specs). Pick if the client values DO's ecosystem (Spaces, managed Postgres) for later expansion.
- *AWS Lightsail / Lambda / Fargate* — more expensive, more vendor lock-in.
- *Self-hosted on-prem* — viable for clients with existing infra, but excluded from the default path because the goal is no-IT-team-needed deployment.

**Implication**: `create-mds` defaults to Hostinger but `references/vps-providers.md` (to be written) will document equivalent setup commands for Hetzner and DigitalOcean.

---

## Airbyte OSS — the integration engine

**Chosen because**: open-source, self-hostable, broadest connector catalog of any open-source ELT tool (300+), exposes a full REST API for headless ops (principle 6). The `abctl` CLI installs it cleanly on a Linux VPS via a Kind-based Kubernetes-in-Docker.

**Alternatives considered**:
- *Fivetran* — best-in-class managed, but $500+/month minimum and the job state lives only in their UI. Violates principles 3 and 6.
- *Meltano* — open-source, but smaller connector catalog and the dbt integration story overlaps with `add-dbt-model` here.
- *dlt (Python lib)* — code-first, no UI, more flexible. Worth a skill of its own one day for clients who want code-only sources. For now Airbyte's coverage wins.
- *Custom Python scripts* — fine for one-off sources, doesn't scale to 5+ sources without becoming a maintenance burden.

**Gotchas baked into the skills**: Airbyte's public API is served under `/api/public/v1/` (not `/api/v2/`), uses OAuth2 client credentials, and has a few endpoints that 403/500 if you use the wrong base path. The `references/airbyte-api-gotchas.md` file (in `skills/add-source/`) documents these.

---

## BigQuery — the warehouse

**Chosen because**: real free tier (10 GB storage + 1 TB queries/month) carries most PYMEs forever, native integrations with GA4 and Google Ads (no Airbyte needed for them — saves connector slots), separation of storage and compute means you don't pay for "idle warehouse" like Snowflake, and the SQL dialect is close to ANSI.

**Alternatives considered**:
- *Snowflake* — more powerful but $800+/month minimum spend kills the PYME story. Reconsider when client revenue justifies it.
- *DuckDB on VPS* — fascinating, costs nothing, but no SaaS-style scaling story and no obvious MCP integration path. Worth a skill variant later.
- *Postgres on VPS* — fine for small warehouses, breaks at 50M+ rows on cheap VPS hardware.
- *ClickHouse* — excellent analytics performance, more ops overhead. Pick if the client has heavy real-time analytics needs.

**Implication**: every skill assumes BigQuery as the warehouse. A future `create-mds-duckdb` or `create-mds-postgres` variant could swap it out — they'd be parallel skills, not modifications of `create-mds`.

---

## dbt-core — the transformation layer

**Chosen because**: industry standard, open-source, the BigQuery adapter is mature, the model-test-doc story is unmatched, and `dbt run` is trivially schedulable from cron. The Python venv + cron pattern is light and observable (principle 6).

**Alternatives considered**:
- *dbt Cloud* — paid layer on top of dbt-core. Buys you a scheduler, IDE, and CI — all of which we get from cron + GitHub Actions + VS Code at zero cost.
- *SQLMesh* — newer, better at versioning and audits. Worth tracking; would be a viable swap if the team grows.
- *Hand-written SQL + a Python orchestrator* — gives you nothing dbt doesn't already give you. Skip.

---

## MCP server — the agentic layer

**Chosen because**: Model Context Protocol is the open standard for connecting tools to AI agents. A BigQuery-backed MCP server gives any compliant agent (claude.ai, Claude Code, Cursor, custom) the ability to query the warehouse, read the `.md` context files in the GitHub repo, and propose edits to them. This is the layer that turns the MDS into a queryable knowledge base, not just a reporting engine.

**Alternatives considered**:
- *Custom REST API for the warehouse* — works, but every new agent client needs custom integration. MCP gives portability for free.
- *Embedding the BigQuery client directly into the agent* — only works for one agent at a time. MCP is the multi-client story.
- *No agentic layer* — leaves the MDS as a passive warehouse. Defensible but throws away the differentiator.

**Implication**: the MCP server is **Phase 3** of `create-mds`. Phase 1 and 2 produce a valid MDS without it; Phase 3 makes it agentic.

---

## GitHub — version control and ops surface

**Chosen because**: the default. Every dev knows it. Free private repos. Actions for CI. Issues for bug tracking. PRs for changes. The `.agentic-data-engineer.json` marker, the dbt project, and the Airbyte configs all live here (principle 4).

**Alternatives**: GitLab, Gitea, Codeberg — all viable, identical workflow. The skills target GitHub by default and don't depend on Actions for core function (cron on the VPS does the heavy lifting).

---

## What is NOT in the default stack

Things that show up in nearby projects but are explicitly excluded from the default `create-mds` path:

- **Looker / Metabase / Superset / Tableau** — BI layer. Out of scope. The agent and the MCP cover most ad-hoc reporting needs; a Next.js dashboard or any BI tool plugs into BigQuery if the client wants one.
- **Kafka / Redpanda / streaming** — overkill for PYME data volumes. Batch is fine.
- **Spark / Beam / Dataflow** — same. BigQuery handles the transformation budget for under-100GB warehouses without breaking a sweat.
- **Kubernetes (beyond what `abctl` runs locally for Airbyte)** — operational overhead that doesn't pay for itself at PYME scale.

These can be added by client-specific skills, but `create-mds` won't pull them in.
