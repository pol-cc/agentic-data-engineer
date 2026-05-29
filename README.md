# agentic-data-engineer

> **This is not a data stack. It's the data engineer.** A [Claude Code](https://claude.com/claude-code) skillpack that turns a blank session into the data engineer a small business could never afford to hire — and a capable one for larger companies too. Point it at a new client and it builds and operates a cheap, powerful Modern Data Stack end-to-end, headless, then steps away until you call it again.

`agentic-data-engineer` is a **skillpack** — skills, playbooks, and templates — not an application and **not the stack itself**. It is the codified knowledge an AI agent reads to *do data-engineering work on your behalf*: discover what you already have, provision what you don't, wire the pipeline, transform the data, and expose it to AI. Once the stack is up it runs by itself (cron, Airbyte schedules, dbt); the engineer returns only when invoked — to add a source, build a model, expose a new domain to AI, or debug.

**Opinionated, but not dogmatic.** It ships a strong default stack *because without an opinion you can't move forward*. But it asks what you already run before assuming anything, adapts to your reality (your existing VPS, warehouse, or VPN win over its defaults), and — crucially — treats its own playbooks as a **floor, not a ceiling**: when it hits terrain the docs don't map, it reasons from first principles, improvises, and keeps going rather than dead-ending on its own documentation.

**100% headless, Claude Code in control.** Every lifecycle operation runs from a terminal session over Tailscale SSH — provisioning, configuration, syncs, transforms, troubleshooting. The only human steps are the ones that legally require a human: signups, OAuth consent, payment. UIs are an inspection layer, never the only way.

## What it builds (the *output* — the repo is the engineer that builds it)

A complete data stack a PYME can afford:

```
DATA SOURCES                INTEGRATION         WAREHOUSE          TRANSFORM          AGENTIC LAYER (opt-in)
────────────                ───────────         ─────────          ─────────          ──────────────────────

On-prem databases ──┐       dlt                                   dbt (VPS)          MCP server
  (via Tailscale)   │      (Python lib,         BigQuery            staging/             (BigQuery-backed)
                    │       on VPS)      ──>    raw_* datasets ──>  marts/        ──>  Skills + .md
SaaS APIs ──────────┤       + reconciliation                        (incremental)       context, callable
  (Factorial, etc.) │                                                                   from any MCP client
                    │
Google services ────┘      BQ native           analytics_*       one linear script
  (GA4, Ads)               transfers     ──>   datasets          dlt load → dbt build
                                                                  (systemd timer)
```

Cost: **~$5-10/month** for a lean dlt build (BigQuery free tier + a small VPS + Tailscale free). Realistically more with heavy use or the Airbyte alternative — see the honest breakdown in [`shared-references/stack-rationale.md`](shared-references/stack-rationale.md). No hard vendor lock-in: ingestion and transformation are portable; the warehouse is a pragmatic managed default with a documented migration path.

## Design principles

This stack is **opinionated by default but adaptive in execution — it recommends strongly and imposes nothing.** The trade-offs are explicit in [`shared-references/ai-native-principles.md`](shared-references/ai-native-principles.md). Headlines:

1. **100% headless from Claude Code** — every lifecycle operation works from a terminal session. UIs are an inspection layer, never the only way.
2. **Tailscale as first-class network layer** — zero public ports, on-prem databases reachable from the VPS, Claude reaches the VPS the same way.
3. **Freemium-first opinionated stack** — dlt + BigQuery + dbt-core + Tailscale + Hostinger VPS, orchestrated by one linear script on a systemd timer. Real costs near $10/month for a lean starter PYME.
4. **GitHub-native ops** — every reproducible piece of the system lives in a GitHub repo. UI-only state is forbidden.
5. **Marker-driven idempotence** — re-running a skill never duplicates work. A `.agentic-data-engineer.json` file in the client repo records what exists.
6. **Observable from agent** — every component exposes logs/status via API or terminal so the agent can troubleshoot without a human screen.
7. **Escape hatches always open** — every component is portable. No lock-in is a design promise.
8. **Recommend strongly, impose nothing** — the agent discovers what you already have *before* provisioning, and your existing VPS / warehouse / VPN wins over the defaults. Every major choice is surfaced as `Default · Alternatives · When to deviate`. See [`shared-references/discovery-and-adaptation.md`](shared-references/discovery-and-adaptation.md).

## Skills

Each skill is invocable independently. Claude picks the right one from natural language.

| Skill | When to invoke |
|---|---|
| [`create-mds`](skills/create-mds/) | Build a Modern Data Stack from scratch on a new VPS |
| [`add-source`](skills/add-source/) | Add a new data source (Airbyte connector or BQ native transfer) to an existing MDS |
| [`add-dbt-model`](skills/add-dbt-model/) | Add a staging, intermediate, or marts model to the dbt project |
| [`add-mcp-skill`](skills/add-mcp-skill/) | Add a new BigQuery-backed skill to the MCP server |
| [`verify-pipeline`](skills/verify-pipeline/) | Run a health check across sources, warehouse, transforms |
| [`troubleshoot`](skills/troubleshoot/) | Diagnose pipeline issues with the agent reading logs across the stack |

## Install

This repo is a Claude Code **plugin** (and its own single-plugin **marketplace**), so once installed the engineer's skills are available in *every* project — including a brand-new empty client folder.

```text
# In Claude Code — add this repo as a marketplace, then install the plugin:
/plugin marketplace add pol-cc/agentic-data-engineer
/plugin install agentic-data-engineer@pol-cc
```

Or try it without installing (loads for one session):

```bash
git clone https://github.com/pol-cc/agentic-data-engineer.git
claude --plugin-dir ./agentic-data-engineer
```

Plugin skills are namespaced (e.g. `/agentic-data-engineer:create-mds`), but you rarely type that — the engineer **picks the right skill from what you ask**.

## What a session feels like

Open a fresh Claude Code in an **empty folder** for a new client and just say what you need:

> *"I need to build the data stack for a new client — a small bakery chain with a Shopify store and a Factorial HR account. Let's start."*

The engineer takes over. It **asks the few things it needs** ("do you already have a VPS? a cloud account? where's the data coming from?") rather than interrogating you, guides you through the human-only steps (signups, OAuth) with the exact links, and provisions everything else headlessly over Tailscale SSH. When the stack is up, it writes a `CLAUDE.md` and a `.agentic-data-engineer.json` state marker into the folder — so the next session in that folder resumes as this client's data engineer, already knowing what's built.

You'll need accounts at: [Google Cloud](https://cloud.google.com) (BigQuery), [Hostinger](https://hostinger.com/vps) or a similar VPS provider, [Tailscale](https://tailscale.com), and [GitHub](https://github.com) — though if you already have any of these, the engineer adapts and reuses them instead.

## Status

**v0.7.0 — stack refactored to a leaner, agent-native default: dlt + BigQuery + dbt + systemd.** After a deep engineering review, the default ingestion moved from Airbyte OSS to **dlt** (a Python library: short feedback loop, state in the warehouse so the VPS is disposable) with **mandatory post-load reconciliation**, and orchestration moved from cron to **one linear script on a systemd timer** (kills the load-vs-transform race by construction). BigQuery stays (serving concurrency for the MCP + compute offload), with active cost control (incremental marts + bytes caps + a budget alert). The MCP layer is now **opt-in and hardened** (read-only service account; write tools off by default, PR-not-push). **Airbyte + cron remain as documented alternatives** for inherited or data-team-scale deployments. The repo is a Claude Code plugin + marketplace; `create-mds` writes a per-client `CLAUDE.md`. All six skills have working references:

- **`create-mds`** — end-to-end: discovery-and-adapt (Step 0) → raw layer (Phase 1, Tailscale + VPS + Airbyte + BigQuery) → dbt transforms on cron (Phase 2) → public MCP server with GitHub OAuth, BigQuery read tools, and **write tools** that let an AI client edit skill docs and push to `main` from chat (Phase 3).
- **`add-source`** — Airbyte API, connector catalog, BQ native transfers, on-prem via Tailscale.
- **`add-dbt-model`** — naming conventions, staging-vs-marts decision tree, and copy-paste templates (staging, marts, schema, sources).
- **`add-mcp-skill`** — the four-file folder pattern, the GitHub write-back mechanism, and a **runnable FastMCP server skeleton** (`templates/mcp-skeleton/`).
- **`verify-pipeline`** — read-only health checks per layer + report format.
- **`troubleshoot`** — ordered diagnostic flow + a catalog of known failure modes.

Two pieces of connective tissue every skill relies on: [`shared-references/remote-control-model.md`](shared-references/remote-control-model.md) (how the agent drives the VPS and on-prem hosts headlessly over Tailscale SSH) and [`shared-references/discovery-and-adaptation.md`](shared-references/discovery-and-adaptation.md) (ask-first, adapt to what the user already has — principle 8).

Still thin: alternative-stack playbooks (Snowflake / WireGuard / AWS) are supported at the adaptation level, not yet with full parallel playbooks. See each `skills/<name>/SKILL.md` header for individual status.

## License

MIT. See [LICENSE](LICENSE).

## Author

Built by [Pol Cribcasals](https://github.com/pol-cc) — distilling patterns from production MDS deployments at PYMEs.

Contributions, issues, and discussions are welcome.
