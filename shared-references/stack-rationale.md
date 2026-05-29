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

**Chosen because**: KVM 2 plan at ~$5-8/month gives 2 vCPU, 8 GB RAM, 100 GB NVMe — comfortable for dlt + dbt + an (optional) MCP server container. With the dlt default (no Airbyte Kubernetes-in-Docker), the box is lean and even a smaller tier works; KVM 2 stays the safe recommendation. The VPS is **disposable** — durable state lives in BigQuery (`_dlt_*` cursors) and the client repo, so a rebuild loses nothing. Hostinger's API and CLI allow headless provisioning.

**Alternatives considered**:
- *Hetzner Cloud* — better price/performance, but no managed snapshots in the cheapest tier. Equivalent choice if the client prefers EU billing or has Hetzner credits.
- *DigitalOcean* — similar tier, slightly more expensive ($12/mo for equivalent specs). Pick if the client values DO's ecosystem (Spaces, managed Postgres) for later expansion.
- *AWS Lightsail / Lambda / Fargate* — more expensive, more vendor lock-in.
- *Self-hosted on-prem* — viable for clients with existing infra, but excluded from the default path because the goal is no-IT-team-needed deployment.

**Implication**: `create-mds` defaults to Hostinger but `references/vps-providers.md` (to be written) will document equivalent setup commands for Hetzner and DigitalOcean.

---

## dlt — the integration engine (default)

**Chosen because** (the agent-native case): dlt (data load tool) is a **Python library, not a platform**. The agent writes a pipeline config, runs `python load.py`, and gets a stack trace or a row count immediately — on the same layer it acts. That short feedback loop is exactly where an agent is strong. dlt also persists its incremental state (cursors) to the **destination warehouse** (`_dlt_*` tables), not to a local DB on the box — so a lost VPS is rebuilt from the repo with cursors intact ("cattle, not pet"). And dlt loads to BigQuery / DuckDB / Postgres / Snowflake by changing one config line, which keeps the warehouse escape-hatch genuinely open (principle 7). For DBs (the on-prem-via-Tailscale case), dlt's `sql_database` source with the Arrow/ConnectorX backend is as good as any.

**The one caveat, designed around**: dlt's failure mode is *insidious* — a mis-set incremental cursor or paginator leaves **silent data gaps** rather than crashing. So **reconciliation after every load is mandatory** (row-count source-vs-destination, freshness, gap checks). That turns the silent failure loud and is what makes dlt safe. See `skills/add-source/references/dlt-state-and-reconstruction.md`.

**Alternatives considered**:
- *Airbyte OSS* — battle-tested connectors and a 300+ catalog, but it's a heavy Kubernetes-in-Docker control plane (Temporal + workers) the agent operates through a job API, not a script it runs and reads. Keeps state on the box (breaks the cattle-not-pet property) and needs a bigger, pricier VPS. **Kept as a documented escape hatch** (`skills/create-mds/references/airbyte-install.md`) for inherited deployments, data-team scale, or a single SaaS source that defeats a dlt config. Its public API is under `/api/public/v1/` (not `/api/v2/`) with OAuth2 — documented in `skills/add-source/references/airbyte-api-gotchas.md`.
- *Singer taps* — run a maintained tap standalone as a subprocess (dlt can ingest from it) for the one gnarly SaaS API. Per-source escape, not a platform.
- *Fivetran* — best-in-class managed, but $500+/month minimum and job state only in their UI. Violates principles 3 and 6.
- *Meltano* — open-source, but heavier than dlt for the agent loop and overlaps dbt.

**The rule**: dlt by default; a maintained connector (Airbyte standalone / Singer) per source, as a documented exception — principle 8 applied to the ingestion layer.

**Google services exception**: GA4, Google Ads, Search Console use BigQuery's **native transfers**, never dlt or Airbyte — more reliable and it saves the integration layer entirely for those sources.

---

## BigQuery — the warehouse

**Chosen because**: real free tier (10 GB storage + 1 TB queries/month) carries most PYMEs far, native integrations with GA4 and Google Ads (no integration tool needed for them), separation of storage and compute means you don't pay for "idle warehouse" like Snowflake, and the SQL dialect is close to ANSI. But the two *strongest* reasons are agent-native:
- **Serving concurrency for the MCP.** The MCP layer serves potentially concurrent reads from AI clients while loads and transforms run. BigQuery (serverless) gives unlimited concurrent reads with zero thought. DuckDB is a single-file store (multi-reader OR one-writer) — you'd hit contention and have to architect a read replica / Parquet snapshot. This is a *present* need, not hypothetical.
- **Compute offload at scale.** dbt push-down means transformation compute runs on Google's servers, not your cheap VPS. At millions of rows it's irrelevant; at x100 (hundreds of millions) the elastic compute keeps a small box from drowning in marts.
- **Zero infra to keep alive.** Serverless = nothing for the agent to provision, patch, or monitor; no 3am page. For an agent-operated stack, that's worth a lot.

**The honest caveat (cost, not infra)**: BigQuery bills by bytes scanned. A full-refresh dbt model on growing tables, or a careless query, can eat the free tier and start billing. This is handled actively, not ignored: `maximum_bytes_billed` on every query (the MCP enforces it), **incremental + partition + cluster by default** for fact marts (`skills/add-dbt-model/references/incremental-and-cost.md`), and a **GCP budget alert** set during `create-mds`. The "self-hosted / no lock-in" promise is therefore softened honestly: the *transformation layer* is portable (dlt + dbt), the *warehouse* is a pragmatic managed default with a documented migration path — not literally self-hosted.

**Alternatives considered**:
- *Snowflake* — more powerful but $800+/month minimum spend kills the PYME story. Reconsider when client revenue justifies it.
- *DuckDB on VPS* — fascinating, costs nothing, but no SaaS-style scaling story and no obvious MCP integration path. Worth a skill variant later.
- *Postgres on VPS* — fine for small warehouses, breaks at 50M+ rows on cheap VPS hardware.
- *ClickHouse* — excellent analytics performance, more ops overhead. Pick if the client has heavy real-time analytics needs.

**Implication**: every skill assumes BigQuery as the warehouse. A future `create-mds-duckdb` or `create-mds-postgres` variant could swap it out — they'd be parallel skills, not modifications of `create-mds`.

---

## dbt-core — the transformation layer

**Chosen because**: industry standard, open-source, the BigQuery adapter is mature, the model-test-doc story is unmatched, and `dbt build` slots straight into the linear pipeline script (`dlt load → dbt build → reconcile`) fired by a systemd timer — no separate scheduler. The Python venv + systemd-timer pattern is light and observable via `journalctl` (principle 6); a `crontab` is the documented alternative.

**Alternatives considered**:
- *dbt Cloud* — paid layer on top of dbt-core. Buys you a scheduler, IDE, and CI — all of which we get from the systemd timer + GitHub Actions + VS Code at zero cost.
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

**Chosen because**: the default. Every dev knows it. Free private repos. Actions for CI. Issues for bug tracking. PRs for changes. The `.agentic-data-engineer.json` marker, the dbt project, and the dlt pipeline configs all live here (principle 4).

**Alternatives**: GitLab, Gitea, Codeberg — all viable, identical workflow. The skills target GitHub by default and don't depend on Actions for core function (the systemd-timer linear script on the VPS does the heavy lifting).

---

## What is NOT in the default stack

Things that show up in nearby projects but are explicitly excluded from the default `create-mds` path:

- **Looker / Metabase / Superset / Tableau** — BI layer. Out of scope. The agent and the MCP cover most ad-hoc reporting needs; a Next.js dashboard or any BI tool plugs into BigQuery if the client wants one.
- **Kafka / Redpanda / streaming** — overkill for PYME data volumes. Batch is fine.
- **Spark / Beam / Dataflow** — same. BigQuery handles the transformation budget for under-100GB warehouses without breaking a sweat.
- **Kubernetes** — operational overhead that doesn't pay for itself at PYME scale. (The Airbyte alternative drags in a local Kubernetes-in-Docker via `abctl`; the dlt default avoids it entirely.)

These can be added by client-specific skills, but `create-mds` won't pull them in.
