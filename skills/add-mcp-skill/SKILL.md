---
name: add-mcp-skill
description: "Add a new BigQuery-backed skill to the MCP server: a tool exposed to AI agents plus the .md context files that describe the underlying data. Invoke when the user wants AI agents (claude.ai, Claude Code, Cursor) to query a new domain of the warehouse."
---

# add-mcp-skill

> **Status**: v0.3.0 — folder-pattern reference written (the most important component). Step-by-step playbook for adding a skill to an existing MCP server still skeletal; the template skeleton (`templates/mcp-skill-skeleton/`) is forthcoming.

## What this skill does

Extends the client's MCP server with a new "skill" — a callable tool plus its context. After this skill runs, an AI agent connected to the MCP server can answer natural-language questions about the new data domain, generate the right SQL, and execute it against BigQuery.

The pattern mirrors `pol-cc/skills-sapiens` (the reference MCP deployment): one MCP server, multiple skills, each scoped to a domain (sales, finance, marketing, etc.).

## Preflight

```bash
if [ ! -f .agentic-data-engineer.json ]; then
  echo "[abort] not a managed MDS deployment"
  exit 1
fi

jq -e '.stack.mcp == true' .agentic-data-engineer.json > /dev/null || {
  echo "[abort] this MDS doesn't have an MCP server"
  echo "run Phase 3 of create-mds first"
  exit 1
}
```

## Anatomy of an MCP skill

Each skill in the MCP server is a folder under `mcp-server/skills/<skill-name>/`:

```
<skill-name>/
├── descriptor.json    declares which BQ datasets/tables this skill can read
├── context.md         business context the LLM needs to write correct SQL
├── schema.md          per-table column documentation, gotchas, joins
└── examples.sql       example queries (the LLM learns the pattern)
```

The MCP server exposes one generic `run_bq_query` tool and uses the per-skill files as **the context** the calling agent loads before composing a query.

## Playbook outline

**Phase A — Define the skill scope**

Ask the user:

1. What domain? (sales, finance, marketing, operations, HR, etc.)
2. Which BigQuery tables/datasets are in scope?
3. What kinds of questions should the skill answer?

**Phase B — Write the skill files**

1. `descriptor.json` — declare allowed tables, max bytes per query, max rows.
2. `context.md` — business glossary: what is a "customer" in this client's world, how are channels classified, etc.
3. `schema.md` — for each table, the meaningful columns + gotchas (e.g. "amount is signed for refunds", "vendor_code is NULL for off-catalog").
4. `examples.sql` — three to five canonical queries the LLM can pattern-match against.

See [`templates/mcp-skill-skeleton/`](templates/mcp-skill-skeleton/) (to be written).

**Phase C — Deploy**

1. Commit the new skill folder to the client repo.
2. On the VPS, pull the change and restart the MCP container.
3. Verify the skill is listed via `list_skills()` from an MCP client.

**Phase D — Verify**

Connect to the MCP server from claude.ai or Claude Code and ask a representative question. Confirm the LLM produces correct SQL grounded in the context files.

## References

Folder pattern (complete):
- [`references/mcp-skill-folder-pattern.md`](references/mcp-skill-folder-pattern.md) — the four-file structure (descriptor.json + context.md + schema.md + examples.sql), quality bars per file, iteration loop, multi-skill rules

Background (in create-mds Phase 3):
- [`../create-mds/references/mcp-server-architecture.md`](../create-mds/references/mcp-server-architecture.md) — how the MCP server uses these files

Still to be written:
- `templates/mcp-skill-skeleton/` — copy-paste starter (descriptor.json + context.md + schema.md + examples.sql with sensible placeholders)
- `references/skills-sapiens-reference.md` — annotated walkthrough of the production reference deployment's first skill
