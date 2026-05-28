# Glossary

Terms used across the skills and references. Read this if a word feels overloaded.

---

**Agent / agentic** — In this repo, "the agent" means an LLM-driven runtime (Claude Code, claude.ai, Codex, any MCP-compatible client) executing a skill on the user's behalf. "Agentic" describes work driven by such a runtime as opposed to a fixed application.

**Client repo** — The private GitHub repo that holds a specific deployment of the MDS for one company. It contains the `.agentic-data-engineer.json` marker, the dbt project, exported Airbyte configs, MCP context files, and any deployment-specific scripts. *Different from the repo you are reading*, which holds the agnostic playbook.

**dbt model** — A SQL `SELECT` statement (with metadata) that dbt materializes as a table or view in BigQuery. Models are organized as `staging/` (cleanup of raw data), `intermediate/` (re-usable joins), and `marts/` (analytics-ready tables).

**ELT** — Extract, Load, Transform. The data is extracted from sources, loaded raw into BigQuery (the "L" step), then transformed inside BigQuery via dbt. Opposed to ETL where transformation happens before loading.

**Headless** — Operable from a terminal session without a graphical UI. The whole MDS is headless in this repo; UI dashboards exist but are inspection layers, never the only path.

**Marker file** — The `.agentic-data-engineer.json` file at the root of every client repo. Records what the skills have built so re-runs are idempotent (principle 5).

**MCP (Model Context Protocol)** — Anthropic's open protocol for exposing tools and context to AI agents. An MCP server runs as a process (typically a container on the VPS), exposes tools (e.g. `run_bq_query`) and resources (e.g. `.md` files), and any compliant client (claude.ai, Claude Code) can connect.

**MDS (Modern Data Stack)** — The collective name for the stack this repo builds: an integration tool (Airbyte) + a cloud warehouse (BigQuery) + a transformation layer (dbt) + (optionally) a BI/agentic layer (MCP server). The "modern" refers to the cloud-native, decoupled-storage-from-compute generation that emerged around 2018-2020.

**Native transfer** — When Google Cloud handles the data movement itself (GA4 → BigQuery, Google Ads → BigQuery). No Airbyte connector needed; the transfer is configured in the BigQuery console or via the `bq` CLI.

**On-prem source** — A database physically located on the client's premises (typically an old ERP, a SQL Server, a MySQL). Reached via Tailscale from the VPS; never exposed to the public internet.

**PYME** — Spanish/Catalan acronym for "Pequeña Y Mediana Empresa" (small and medium business). Used as the default audience profile in this repo: typically 10-200 employees, no dedicated data team, $5-50/month budget for the data stack.

**Raw dataset** — A BigQuery dataset (prefixed `raw_*` by convention) that holds untransformed data as the integration tool delivered it. Never queried directly by users — staging models clean it up first.

**Skill** — A markdown playbook (`SKILL.md`) plus optional supporting files (references, scripts, templates) that teaches an AI agent how to do one specific task. The unit of distribution in this repo.

**Source / connector** — A configured data source (e.g. "Factorial HR account #1234") inside Airbyte. A *connector* is the reusable adapter Airbyte ships (e.g. "Factorial HR connector"); a *source* is an instance of a connector with credentials and a specific configuration.

**Sync** — One execution of an Airbyte connection (source → destination). Triggered by Airbyte's scheduler or manually via the API.

**Tailnet** — A Tailscale-managed mesh network. Every machine in a client's MDS deployment (VPS, laptop, on-prem servers) joins one tailnet and gets a stable hostname inside it.

**VPS** — Virtual Private Server. The cloud machine that hosts Airbyte, dbt cron, and the MCP server. Defaults to Hostinger KVM 2 in this repo; any equivalent works.

**Warehouse** — The cloud database that holds all integrated data. BigQuery in this repo by default.
