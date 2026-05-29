---
name: create-mds
description: "Build a Modern Data Stack (Tailscale + dlt + BigQuery + dbt-core + systemd timers + optional MCP) from scratch on a new VPS for a small or medium business. Invoke when the user wants to bootstrap data integration end-to-end."
---

# create-mds

> **Status**: v0.7.0 — default stack is **Tailscale + dlt + BigQuery + dbt-core + a single linear script on systemd timers + (opt-in) MCP**, on a small disposable VPS. Phase 1, Phase 2, and Phase 3 playbooks complete, with a discovery-and-adapt step (Step 0) that asks what the user already has before provisioning. Airbyte OSS + cron are kept as documented alternatives, not the default. See [`shared-references/ai-native-principles.md`](../../shared-references/ai-native-principles.md) for the design philosophy this skill must honor, and [`shared-references/discovery-and-adaptation.md`](../../shared-references/discovery-and-adaptation.md) for the ask-first discipline.

## What this skill does

Builds a complete Modern Data Stack for a PYME from zero — no existing infrastructure assumed. End state:

- A small disposable VPS joined to a Tailscale tailnet, running dlt + dbt in Python venvs
- A BigQuery project with a **write** service account, a **budget alert**, and `raw_<source>` datasets receiving data
- A GitHub repo holding the dlt pipeline + reconcile scripts, the dbt project, the systemd units, the per-client `CLAUDE.md`, and the `.agentic-data-engineer.json` marker
- One or more data sources loading via dlt, each **reconciled** (row-count / freshness / sequence-gap — mandatory)
- A **single linear pipeline script** (`dlt load → dbt build → reconcile`) fired daily by a **systemd timer**
- (Optional Phase 3, **opt-in but recommended**) An MCP server exposing the warehouse to AI agents over a read-only service account

### Why this default (the agent-native rationale)

An agent works best with a **short feedback loop** (write a script, run it, read the result), things it can **run+read by CLI**, **loud failures**, and a system **reconstructible from the repo**. dlt is a Python library — `python load.py` returns a row count or a stack trace immediately, no control plane to operate. dlt persists incremental state to the **destination** warehouse (`_dlt_*` tables in BigQuery), so a lost VPS is rebuilt from the repo with cursors intact — **cattle, not pet**. And because dlt and dbt run as one sequential script, the old Airbyte-cron-vs-dbt-cron race condition is **gone by construction**; systemd timers replace cron because `systemctl status` + `journalctl -u` are far better agent-observability surfaces than a mute crontab.

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

## Phase 1 — Raw layer (Tailscale + VPS + BigQuery + dlt)

**Status: complete (v0.7.0 — dlt default).** Full playbook in [`references/phase-1-raw-layer.md`](references/phase-1-raw-layer.md). That file is the orchestrator the agent reads to drive Phase 1 end-to-end.

Outline:

1. Discover-and-adapt, then gather build inputs: company name, primary data sources, VPS preference, GCP billing account.
2. Provision the small disposable VPS — [`references/vps-hostinger-bootstrap.md`](references/vps-hostinger-bootstrap.md).
3. Join the VPS to a Tailscale tailnet, optionally on-prem hosts — [`references/tailscale-onprem.md`](references/tailscale-onprem.md).
4. Create the BigQuery project, the **write** service account, and a **budget alert** — [`references/bigquery-project-setup.md`](references/bigquery-project-setup.md).
5. Install dlt in a Python venv on the VPS — [`references/dlt-on-vps-install.md`](references/dlt-on-vps-install.md).
6. Write the first dlt pipeline (`dlt load → raw_<src>`) and **reconcile** (mandatory) — delegates to [`add-source`](../add-source/SKILL.md).
7. Initialize the client GitHub repo (dlt scripts + per-client `CLAUDE.md`), write the marker, commit the initial state.

> *Alternative ingestion (documented escape, not default):* Airbyte OSS via `abctl` — [`references/airbyte-install.md`](references/airbyte-install.md). Battle-tested but heavier and less agent-native; use when already committed or at data-team scale.

## Phase 2 — Transform layer (dbt) + orchestration

**Status: complete (v0.7.0 — systemd default).** Full playbook in [`references/phase-2-transform-layer.md`](references/phase-2-transform-layer.md). Invoked after Phase 1 succeeds, or independently if the user already has Phase 1 done and wants to add dbt.

Outline:

1. Install dbt-core + dbt-bigquery in a Python venv on the VPS — [`references/dbt-on-vps-install.md`](references/dbt-on-vps-install.md).
2. Scaffold the dbt project structure following the [`add-dbt-model`](../add-dbt-model/SKILL.md) conventions — [`references/dbt-project-scaffold.md`](references/dbt-project-scaffold.md).
3. Configure `profiles.yml` for the BigQuery write service account — [`references/dbt-profiles-bigquery.md`](references/dbt-profiles-bigquery.md).
4. Bootstrap staging models for each existing source; marts (added later) default to incremental + partition + cluster — delegates to [`add-dbt-model`](../add-dbt-model/SKILL.md).
5. Wire `dbt build` as the middle stage of the **single linear pipeline script** (`dlt load → dbt build → reconcile`) and fire it with a **systemd timer** — [`references/orchestration-systemd.md`](references/orchestration-systemd.md). One sequential script means the Airbyte-vs-dbt race condition is gone by construction.
6. Commit the dbt project + systemd units to the client repo.

> *Alternative orchestration (documented escape, not default):* schedule `dbt run` on its own cron — [`references/dbt-cron-scheduling.md`](references/dbt-cron-scheduling.md). Only for inherited crontabs, no-systemd hosts, or the Airbyte path where ingestion and transform genuinely are separate jobs.

## Phase 3 — Agentic layer (MCP server)

**Status: complete.** Full playbook in [`references/phase-3-agentic-layer.md`](references/phase-3-agentic-layer.md). **Opt-in but recommended** — can be skipped or deferred, but it's what turns the warehouse into an agentic platform queryable by any MCP-compatible client (claude.ai, Claude Code, Cursor, future agents). The MCP server queries BigQuery through a **separate read-only service account** (never the dlt write key — see the read/write split in [`references/bigquery-project-setup.md`](references/bigquery-project-setup.md)).

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
- A 24h verification window where the user runs `verify-pipeline` to confirm the timer fired and (if Phase 2 ran) the first `dlt load → dbt build → reconcile` succeeded

## References

Phase 1 (complete):
- [`references/phase-1-raw-layer.md`](references/phase-1-raw-layer.md) — orchestrator, step-by-step
- [`references/vps-hostinger-bootstrap.md`](references/vps-hostinger-bootstrap.md)
- [`references/tailscale-onprem.md`](references/tailscale-onprem.md)
- [`references/bigquery-project-setup.md`](references/bigquery-project-setup.md) — write SA + budget alert + read/write split note
- [`references/dlt-on-vps-install.md`](references/dlt-on-vps-install.md) — **default ingestion engine**
- [`../add-source/SKILL.md`](../add-source/SKILL.md) — dlt source detail (first pipeline + reconcile)
- [`references/airbyte-install.md`](references/airbyte-install.md) — *alternative ingestion (documented escape)*

Phase 2 (complete):
- [`references/phase-2-transform-layer.md`](references/phase-2-transform-layer.md) — orchestrator
- [`references/dbt-on-vps-install.md`](references/dbt-on-vps-install.md)
- [`references/dbt-project-scaffold.md`](references/dbt-project-scaffold.md)
- [`references/dbt-profiles-bigquery.md`](references/dbt-profiles-bigquery.md)
- [`references/orchestration-systemd.md`](references/orchestration-systemd.md) — **default orchestration: linear script + systemd timer**
- [`references/dbt-cron-scheduling.md`](references/dbt-cron-scheduling.md) — *alternative orchestration (documented escape)*
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
