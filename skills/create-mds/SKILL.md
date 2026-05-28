---
name: create-mds
description: "Build a Modern Data Stack (Tailscale + Airbyte + BigQuery + dbt + MCP) from scratch on a new VPS for a small or medium business. Invoke when the user wants to bootstrap data integration end-to-end."
---

# create-mds

> **Status**: v0.3.0 — Phase 1, Phase 2, and Phase 3 playbooks complete. See [`shared-references/ai-native-principles.md`](../../shared-references/ai-native-principles.md) for the design philosophy this skill must honor.

## What this skill does

Builds a complete Modern Data Stack for a PYME from zero — no existing infrastructure assumed. End state:

- A VPS running Airbyte OSS + dbt + an MCP server, joined to a Tailscale tailnet
- A BigQuery project with a service account and raw datasets ready to receive data
- A GitHub repo holding the dbt project, Airbyte config exports, MCP context, and the `.agentic-data-engineer.json` marker
- One or more data sources actively syncing
- A cron job running `dbt run` daily
- (Optional Phase 3) An MCP server exposing the warehouse to AI agents

The skill is invoked **once per deployment**. Subsequent additions (new sources, new models, new MCP skills) use other skills in this repo.

## Preflight (always run first)

Before doing anything, check whether the current directory is already a client repo with a marker file:

```bash
if [ -f .agentic-data-engineer.json ]; then
  echo "[abort] this directory is already a managed MDS deployment"
  echo "see: $(jq -r .decisions.github_repo .agentic-data-engineer.json)"
  echo "use 'add-source', 'add-dbt-model', or 'troubleshoot' instead"
  exit 1
fi
```

If the marker exists, **do not proceed**. Tell the user which skill to use instead.

## Phase 1 — Raw layer (Tailscale + VPS + Airbyte + BigQuery)

**Status: complete (v0.1.0).** Full playbook in [`references/phase-1-raw-layer.md`](references/phase-1-raw-layer.md). That file is the orchestrator the agent reads to drive Phase 1 end-to-end.

Outline:

1. Gather user input: company name, primary data sources, VPS provider preference, GCP billing account.
2. Provision the VPS — [`references/vps-hostinger-bootstrap.md`](references/vps-hostinger-bootstrap.md).
3. Join the VPS to a Tailscale tailnet, optionally on-prem hosts — [`references/tailscale-onprem.md`](references/tailscale-onprem.md).
4. Create the BigQuery project and service account — [`references/bigquery-project-setup.md`](references/bigquery-project-setup.md).
5. Install Airbyte OSS via `abctl` — [`references/airbyte-install.md`](references/airbyte-install.md).
6. Wire the first source (delegates to [`add-source`](../add-source/SKILL.md)).
7. Initialize the client GitHub repo, write the marker, commit the initial state.

## Phase 2 — Transform layer (dbt)

**Status: complete (v0.2.0).** Full playbook in [`references/phase-2-transform-layer.md`](references/phase-2-transform-layer.md). Invoked after Phase 1 succeeds, or independently if the user already has Phase 1 done and wants to add dbt.

Outline:

1. Install dbt-core + dbt-bigquery in a Python venv on the VPS — [`references/dbt-on-vps-install.md`](references/dbt-on-vps-install.md).
2. Scaffold the dbt project structure following the [`add-dbt-model`](../add-dbt-model/SKILL.md) conventions — [`references/dbt-project-scaffold.md`](references/dbt-project-scaffold.md).
3. Configure `profiles.yml` for the BigQuery service account — [`references/dbt-profiles-bigquery.md`](references/dbt-profiles-bigquery.md).
4. Bootstrap staging models for each existing source (delegates to [`add-dbt-model`](../add-dbt-model/SKILL.md)).
5. Schedule `dbt run` via cron — [`references/dbt-cron-scheduling.md`](references/dbt-cron-scheduling.md).
6. Commit the dbt project to the client repo.

## Phase 3 — Agentic layer (MCP server)

**Status: complete (v0.3.0).** Full playbook in [`references/phase-3-agentic-layer.md`](references/phase-3-agentic-layer.md). Optional — can be skipped or deferred. When invoked, turns the warehouse into an agentic platform queryable by any MCP-compatible client (claude.ai, Claude Code, Cursor, future agents).

Outline:

1. User input: domain name for MCP endpoint, allowlist of GitHub users, first skill domain, write-tools yes/no.
2. DNS A record → VPS public IP.
3. Install Traefik on the VPS for TLS reverse proxy — [`references/traefik-tls-setup.md`](references/traefik-tls-setup.md).
4. Create a read-only BigQuery service account for the MCP server.
5. Create a GitHub OAuth app (manual ceremony).
6. Deploy the MCP server as a Docker container — [`references/mcp-bigquery-server-deploy.md`](references/mcp-bigquery-server-deploy.md).
7. Bootstrap the first skill (folder with descriptor.json + context.md + schema.md + examples.sql) — [`references/mcp-first-skill-bootstrap.md`](references/mcp-first-skill-bootstrap.md).
8. Connect from claude.ai (manual ceremony).
9. Commit MCP code and skills to the client repo.

Conceptual background: [`references/mcp-server-architecture.md`](references/mcp-server-architecture.md).

## Outputs

When this skill completes successfully:

- A client GitHub repo at the URL the user chose
- A VPS running the chosen components
- `.agentic-data-engineer.json` marker committed with the deployment's full state
- A 24h verification window where the user runs `verify-pipeline` to confirm syncs and (if Phase 2 ran) the first `dbt run`

## References

Phase 1 (complete):
- [`references/phase-1-raw-layer.md`](references/phase-1-raw-layer.md) — orchestrator, step-by-step
- [`references/vps-hostinger-bootstrap.md`](references/vps-hostinger-bootstrap.md)
- [`references/tailscale-onprem.md`](references/tailscale-onprem.md)
- [`references/airbyte-install.md`](references/airbyte-install.md)
- [`references/bigquery-project-setup.md`](references/bigquery-project-setup.md)

Phase 2 (complete):
- [`references/phase-2-transform-layer.md`](references/phase-2-transform-layer.md) — orchestrator
- [`references/dbt-on-vps-install.md`](references/dbt-on-vps-install.md)
- [`references/dbt-project-scaffold.md`](references/dbt-project-scaffold.md)
- [`references/dbt-profiles-bigquery.md`](references/dbt-profiles-bigquery.md)
- [`references/dbt-cron-scheduling.md`](references/dbt-cron-scheduling.md)
- [`../add-dbt-model/references/dbt-naming-conventions.md`](../add-dbt-model/references/dbt-naming-conventions.md)
- [`../add-dbt-model/references/staging-vs-marts.md`](../add-dbt-model/references/staging-vs-marts.md)

Phase 3 (complete):
- [`references/phase-3-agentic-layer.md`](references/phase-3-agentic-layer.md) — orchestrator
- [`references/mcp-server-architecture.md`](references/mcp-server-architecture.md) — design decisions
- [`references/traefik-tls-setup.md`](references/traefik-tls-setup.md)
- [`references/mcp-bigquery-server-deploy.md`](references/mcp-bigquery-server-deploy.md)
- [`references/mcp-first-skill-bootstrap.md`](references/mcp-first-skill-bootstrap.md)
- [`../add-mcp-skill/references/mcp-skill-folder-pattern.md`](../add-mcp-skill/references/mcp-skill-folder-pattern.md)

Cross-cutting:
- [`../../shared-references/ai-native-principles.md`](../../shared-references/ai-native-principles.md)
- [`../../shared-references/stack-rationale.md`](../../shared-references/stack-rationale.md)
