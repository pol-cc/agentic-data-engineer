---
name: create-mds
description: "Build a Modern Data Stack (Tailscale + Airbyte + BigQuery + dbt + MCP) from scratch on a new VPS for a small or medium business. Invoke when the user wants to bootstrap data integration end-to-end."
---

# create-mds

> **Status**: v0.1.0 — Phase 1 playbook complete; Phase 2 and Phase 3 still to be written. See [`shared-references/ai-native-principles.md`](../../shared-references/ai-native-principles.md) for the design philosophy this skill must honor.

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

**Status: planned.** Will be a separate playbook section invoked after Phase 1 completes or on demand.

Outline:

1. Install dbt-core + dbt-bigquery in a Python venv on the VPS.
2. Scaffold the dbt project structure (`staging/`, `intermediate/`, `marts/`) following [`add-dbt-model`](../add-dbt-model/SKILL.md) conventions.
3. Configure `profiles.yml` for the BigQuery service account.
4. Schedule `dbt run` via cron with logging to a discoverable path.
5. Commit the dbt project to the client repo.

## Phase 3 — Agentic layer (MCP server)

**Status: planned.** Optional. Skipped if the user declines.

Outline:

1. Scaffold an MCP server from the template (see [`add-mcp-skill`](../add-mcp-skill/SKILL.md) for the per-skill pattern).
2. Deploy as a container on the VPS behind Traefik with TLS.
3. Configure OAuth/auth for the MCP endpoint.
4. Register the first skill (`run_bq_query` plus a context `.md` file).
5. Commit the MCP server config to the client repo.

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

Cross-cutting:
- [`../../shared-references/ai-native-principles.md`](../../shared-references/ai-native-principles.md)
- [`../../shared-references/stack-rationale.md`](../../shared-references/stack-rationale.md)
