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

**MDS (Modern Data Stack)** — The collective name for the stack this repo builds: an integration tool (dlt by default) + a cloud warehouse (BigQuery) + a transformation layer (dbt) + (optionally) an agentic layer (MCP server). The "modern" refers to the cloud-native, decoupled-storage-from-compute generation that emerged around 2018-2020.

**dlt (data load tool)** — The default ingestion tool: a Python library (not a platform) that extracts from sources and loads raw into the warehouse. Chosen for its short agent feedback loop and because it persists incremental state to the destination warehouse (`_dlt_*` tables), making the VPS disposable. Its insidious failure mode (silent data gaps from a mis-set cursor) is why reconciliation is mandatory.

**Reconciliation** — Mandatory post-load checks that the ingested data is complete: row-count source-vs-destination, freshness, and sequence/gap detection. Turns dlt's silent failure mode loud. Lives in the linear pipeline script and is verified by `verify-pipeline`.

**systemd timer** — The default orchestration mechanism (replacing cron). A `.timer` unit fires the linear pipeline script (`dlt load → dbt build → reconcile`); `systemctl status` and `journalctl -u` give the agent far better observability than a mute crontab.

**Cattle, not pet** — The property that the VPS is disposable: all durable state lives in BigQuery (`_dlt_*` cursors) and the client repo, so a lost box is rebuilt by the agent in minutes with no data gaps.

**Native transfer** — When Google Cloud handles the data movement itself (GA4 → BigQuery, Google Ads → BigQuery). No integration tool needed — not even a dlt pipeline (nor an Airbyte connector); the transfer is configured in the BigQuery console or via the `bq` CLI.

**On-prem source** — A database physically located on the client's premises (typically an old ERP, a SQL Server, a MySQL). Reached via Tailscale from the VPS (dlt's `sql_database` source connects over the tailnet); never exposed to the public internet.

**PYME** — Spanish/Catalan acronym for "Pequeña Y Mediana Empresa" (small and medium business). Used as the default audience profile in this repo: typically 10-200 employees, no dedicated data team, $5-50/month budget for the data stack.

**Raw dataset** — A BigQuery dataset (prefixed `raw_*` by convention) that holds untransformed data as the integration tool (dlt by default; Airbyte as an alternative) delivered it. Never queried directly by users — staging models clean it up first.

**Skill** — A markdown playbook (`SKILL.md`) plus optional supporting files (references, scripts, templates) that teaches an AI agent how to do one specific task. The unit of distribution in this repo.

**Source / connector** — A configured data source (e.g. "Factorial HR account #1234"). By default a source is a **dlt source/pipeline** — a Python definition (credentials + config) that extracts from the system and loads raw into the warehouse. *(Alternative: an Airbyte source — an instance of a reusable Airbyte connector — for inherited or data-team-scale deployments.)*

**Sync** — One run of a source → destination load. By default, one execution of a **dlt pipeline** (`python load.py`), fired by the systemd-timer linear script. *(Alternative: one execution of an Airbyte connection, triggered by Airbyte's scheduler or its API.)*

**Tailnet** — A Tailscale-managed mesh network. Every machine in a client's MDS deployment (VPS, laptop, on-prem servers) joins one tailnet and gets a stable hostname inside it.

**VPS** — Virtual Private Server. The cloud machine that hosts the dlt + dbt linear pipeline script (on a systemd timer) and, optionally, the MCP server (Airbyte optional, as an alternative integration layer). Defaults to Hostinger KVM 2 in this repo; any equivalent works. Disposable — durable state lives in BigQuery and the client repo.

**Warehouse** — The cloud database that holds all integrated data. BigQuery in this repo by default.
