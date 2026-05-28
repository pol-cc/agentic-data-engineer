# agentic-data-engineer

> An AI data engineer for small businesses. Skill-based, not app-based. Appears when called, builds your Modern Data Stack, and steps away.

`agentic-data-engineer` is a [Claude Code](https://claude.com/claude-code) skill collection that builds and evolves a **Modern Data Stack (MDS)** for small and medium businesses — end-to-end, headless, from a Claude Code session.

It is not a SaaS, not a runtime, not a daemon. It is **a body of knowledge an AI agent reads to do data engineering work on your behalf** — bootstrapping a VPS, configuring Airbyte, wiring BigQuery, scaffolding dbt, deploying an MCP server. Once the system is up, it runs by itself (cron, Airbyte schedules, dbt). The skill comes back only when invoked.

## What it builds

A complete data stack a PYME can afford:

```
DATA SOURCES                INTEGRATION         WAREHOUSE          TRANSFORM          AGENTIC LAYER
────────────                ───────────         ─────────          ─────────          ─────────────

On-prem databases ──┐                                             dbt (VPS)          MCP server
  (via Tailscale)   │      Airbyte OSS         BigQuery            staging/             (BigQuery-backed)
                    │      (on VPS)     ──>    raw_* datasets ──>  marts/        ──>  Skills + .md
SaaS APIs ──────────┤                                                                   context, callable
  (Factorial, etc.) │                                                                   from any MCP client
                    │
Google services ────┘      BQ native           analytics_*
  (GA4, Ads)               transfers     ──>   datasets
```

Cost: **~$5-10/month**. No vendor lock-in: every component is open-source or has a real free tier.

## Design principles

This stack is opinionated. The trade-offs are explicit in [`shared-references/ai-native-principles.md`](shared-references/ai-native-principles.md). Headlines:

1. **100% headless from Claude Code** — every lifecycle operation works from a terminal session. UIs are an inspection layer, never the only way.
2. **Tailscale as first-class network layer** — zero public ports, on-prem databases reachable from the VPS, Claude reaches the VPS the same way.
3. **Freemium-first opinionated stack** — BigQuery + Airbyte OSS + dbt-core + Tailscale + Hostinger VPS = real costs under $10/month for a starter PYME.
4. **GitHub-native ops** — every reproducible piece of the system lives in a GitHub repo. UI-only state is forbidden.
5. **Marker-driven idempotence** — re-running a skill never duplicates work. A `.agentic-data-engineer.json` file in the client repo records what exists.
6. **Observable from agent** — every component exposes logs/status via API or terminal so the agent can troubleshoot without a human screen.
7. **Escape hatches always open** — every component is portable. No lock-in is a design promise.

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

## Quick start

```bash
# Clone this repo so Claude Code can read the skills
git clone https://github.com/pol-cc/agentic-data-engineer.git
cd agentic-data-engineer

# Open Claude Code in this folder and ask:
# > "Build me a Modern Data Stack for a small bakery chain. We have a Shopify store and a Factorial HR account."
#
# Claude will pick the `create-mds` skill, ask the questions it needs, and walk you through.
```

You need accounts at: [Google Cloud](https://cloud.google.com) (BigQuery), [Hostinger](https://hostinger.com/vps) or similar VPS provider, [Tailscale](https://tailscale.com), and a [GitHub](https://github.com) account for the client repo.

## Status

**v0.3.0 — `create-mds` complete (all three phases).** The skill drives a deployment end-to-end: from zero through raw layer (Phase 1, Tailscale + VPS + Airbyte + BigQuery), through dbt transforms on cron (Phase 2), to a public MCP server with GitHub OAuth and the first per-domain skill (Phase 3). The `add-dbt-model` and `add-mcp-skill` skills have their core convention references written.

The remaining three skills (`add-source`, `verify-pipeline`, `troubleshoot`) still have skeletal SKILL.md scaffolds; their `references/` are next. See each `skills/<name>/SKILL.md` header for individual status.

Next development priority: `add-source` references (Airbyte API + BQ native transfer + on-prem via Tailscale).

## License

MIT. See [LICENSE](LICENSE).

## Author

Built by [Pol Cribcasals](https://github.com/pol-cc) — distilling patterns from production MDS deployments at PYMEs.

Contributions, issues, and discussions are welcome.
