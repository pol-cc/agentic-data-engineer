# agentic-data-engineer — Claude Code briefing

You are reading this because the user opened a Claude Code session inside `agentic-data-engineer`. This repo is a **skill collection**, not an application. There is no app to run, no service to start, no tests to execute.

## What this repo is

A library of skills (`skills/<name>/SKILL.md`) that an AI agent uses to build and evolve a **Modern Data Stack** for small and medium businesses. The stack is opinionated: Tailscale + Airbyte OSS + BigQuery + dbt + MCP, deployed on a VPS, fully driveable from a Claude Code session.

The full design philosophy lives in [`shared-references/ai-native-principles.md`](shared-references/ai-native-principles.md). **Read it before doing anything substantive.**

## How the user invokes skills

Skills are picked automatically by Claude from the user's natural language. The user does NOT type `/create-mds` — they say things like:

- "Build me an MDS for a small e-commerce" → invoke `skills/create-mds`
- "Add Shopify as a data source" → invoke `skills/add-source`
- "Create a staging model for orders" → invoke `skills/add-dbt-model`

Each skill is a self-contained playbook with a `SKILL.md` entry point, optional `references/` (read on demand), optional `scripts/` (deterministic helpers), and optional `templates/` (files to copy into the client repo).

Cross-cutting knowledge every skill relies on lives in `shared-references/`:
- [`shared-references/discovery-and-adaptation.md`](shared-references/discovery-and-adaptation.md) — **run this first** on any build/expand task. Discover what the user already has (VPS, warehouse, VPN, cloud) and adapt before provisioning. The stack is opinionated by default but you ask first and adapt (principle 8 — recommend strongly, impose nothing).
- [`shared-references/remote-control-model.md`](shared-references/remote-control-model.md) — read before running any remote command. How the agent drives the VPS and on-prem hosts over Tailscale SSH.

## What you should NOT do in this repo

- **Do not build an application.** Skills produce artifacts in the user's *own* client repo, not here.
- **Do not commit client data.** This repo holds knowledge; client configs, credentials, and per-deployment artifacts live in the client's own private repo.
- **Do not invent skills.** If the user asks for something not covered by an existing skill, propose adding one — don't improvise across skills.

## What you SHOULD do

- When the user asks for work that matches an existing skill, **read that skill's `SKILL.md` first**, then proceed.
- When developing this repo (writing or refining skills), follow the format used by Anthropic's official skills: YAML front-matter with `name` + `description`, terse operational body, references for deep context, scripts for repeatable commands.
- Keep skill bodies short. **Optimize for routing, not documentation.** Long-form knowledge belongs in `references/` files cited from the skill body.

## Current status

This repo is at **v0.0.1 (skeleton)**. Skills exist with proper front-matter but their playbooks are still being written. Check each `SKILL.md` header for its individual status.

Development priority: **Phase 1 (`create-mds`)** first — the rest follow.
