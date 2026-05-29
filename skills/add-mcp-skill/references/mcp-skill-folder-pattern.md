# MCP skill folder pattern

The opinionated layout for every skill registered with the MCP server. Four files, each with one purpose. Used by both `create-mds` Phase 3 (the first skill) and `add-mcp-skill` (every subsequent skill).

## The four files

```
mcp-skills/<skill-name>/
├── descriptor.json    ← machine-readable boundary + limits
├── context.md         ← business glossary the LLM reads first
├── schema.md          ← per-table column documentation
└── examples.sql       ← canonical queries (3-5)
```

Why four files instead of one? Because the agent loads them at different points:

- `descriptor.json` is read by the server (not the agent) to enforce table access.
- `context.md` is the first thing the agent reads when the skill is selected — sets domain framing.
- `schema.md` is referenced when the agent is composing SQL — column lookups.
- `examples.sql` is the pattern bank the agent matches against.

Separating these keeps each file scannable and lets the agent load only what it needs (saves context tokens).

## `descriptor.json` — the contract

```jsonc
{
  "name": "sales",
  "description": "Sales analytics for orders, revenue, customers, and channels.",
  "version": "0.1.0",
  "tables": [
    "<project>.analytics.dim_customers",
    "<project>.analytics.fact_orders",
    "<project>.analytics.revenue_monthly"
  ],
  "max_query_bytes": 2147483648,
  "max_rows": 1000,
  "default_currency": "EUR",
  "fiscal_year_start_month": 1
}
```

Required keys:

| Key | Type | Meaning |
|---|---|---|
| `name` | string | Unique skill identifier. Lowercase, dashes or underscores. Used as URL slug and in `list_skills()`. |
| `description` | string | One-sentence summary shown in `list_skills()`. |
| `version` | string | Semver. Increment when changing tables, columns, or query patterns. |
| `tables` | string[] | Fully-qualified BigQuery table names the agent may query. Enforced by the server. |
| `max_query_bytes` | integer | Maximum `maximumBytesBilled` for any query under this skill. Server-enforced. |
| `max_rows` | integer | Maximum row count returned per query. Server-enforced; LIMIT injected if absent. |

Optional but recommended:

| Key | Meaning |
|---|---|
| `default_currency` | Default monetary unit. Agent uses this when formatting answers. |
| `fiscal_year_start_month` | 1-12. Most clients: 1. UK or non-calendar fiscal: varies. |
| `time_zone` | IANA TZ name. Defaults to `Europe/Madrid` for ES clients. |
| `excluded_columns` | per-table column names the agent should NEVER include in output (PII, secrets). |

**The `tables` array is the security boundary.** The MCP server's `run_bq_query` tool parses the SQL (or uses a BQ dry-run) and rejects any query that references a table not in this list. This is how the same MCP server can safely expose multiple skills with different scopes — a user querying `sales` cannot accidentally (or maliciously) join into HR tables.

## `context.md` — the business glossary

The LLM reads this entire file when the skill is selected. Keep it scannable (100-300 lines is typical).

Required sections:

```markdown
# <Skill name> — business context

## What "<domain>" means here
[paragraph: scope of the domain, what's IN, what's OUT]

## Key terms and definitions
[definitions of domain-specific terms — channel, active customer, fiscal year, etc.]

## Time period conventions
[how "today", "last month", "YTD" should be computed for this domain]

## Common questions and where to find them
[2-5 typical questions with hints about which table/column answers each]

## What this skill does NOT cover
[explicit out-of-scope: directs the agent to give up gracefully]
```

Optional sections:

- Currency/units conventions (multi-currency, decimal precision)
- Data freshness (when is data updated, is "today" actually "yesterday" because of sync delay)
- Privacy notes ("never include `email` in output for this skill")
- Known data quality issues ("orders before 2023 have NULL country")

**Quality bar**: written as if onboarding a new junior analyst. If they'd ask a clarifying question after reading, the doc isn't done yet.

## `schema.md` — per-table column documentation

One section per table in `descriptor.json.tables`. Each section has:

1. The full table name as a heading
2. A one-line description
3. A column table with name, type, description
4. A "gotchas" subsection
5. (Optional) Common joins for this table

Template:

```markdown
## `<project>.analytics.fact_orders`

One row per order. The atomic event table.

| Column | Type | Description |
|---|---|---|
| `order_id` | STRING | Primary key. Format: `ord_<8 hex>`. |
| `customer_id` | STRING | FK to dim_customers. NOT NULL. |
| `order_date` | DATE | Date the order was placed. Use this for time filtering. |
| ... | ... | ... |

Gotchas:
- `gross_amount` and `net_amount` are NEVER negative — returns are separate rows...
- `order_date` is Madrid TZ. Underlying `order_at` is UTC.

Common joins:
- `fact_orders LEFT JOIN dim_customers USING (customer_id)`
```

**Quality bar**: every column the agent will reference appears here. Every gotcha that bit a human analyst is listed. Joins are explicit (the LLM will copy them).

## `examples.sql` — the pattern bank

3-5 canonical queries. Each preceded by a comment block stating the natural-language question.

Template:

```sql
-- ============================================================
-- Q: What was last month's total revenue?
-- ============================================================
SELECT ...
FROM `<project>.analytics.revenue_monthly`
WHERE month = DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 1 MONTH), MONTH);

-- ============================================================
-- Q: Top 10 customers by lifetime value
-- ============================================================
SELECT ...
ORDER BY ltv_eur DESC
LIMIT 10;
```

What makes good examples:

- **Span the typical question types** for the domain (single-period agg, period-over-period, top-N, distribution).
- **Match the columns documented in `schema.md`** — drift between files breaks the agent's reasoning.
- **Use the conventions from `context.md`** (e.g. always filter `status='completed'` for "sales").
- **Show pattern reuse** — the agent should be able to swap "last month" for "last quarter" without rewriting the query shape.

What to avoid:

- Overly clever SQL (window functions stacked 3-deep). The agent should pattern-match, not parse.
- Hardcoded literal dates in examples (`WHERE month = '2026-04-01'`). Use `CURRENT_DATE()` and arithmetic.
- Cross-skill joins. Each example stays within the skill's `tables` allowlist.

## Optional fifth file: `runbooks.md`

For mature skills, a fifth file documenting "what to do when the agent's answer disagrees with the user's expectation". Common patterns:

- "Sales by channel shows 'web' lower than expected" → check if `channel_code='web'` is being filtered correctly (web orders ≠ web-fulfilled orders).
- "Revenue YoY is off" → confirm both years exclude returns the same way.

This is the institutional knowledge that accumulates over time. Optional in v0.3.0; recommended once a skill has been in production for a few weeks.

## Skill iteration loop

The point of the four-file structure is that **iteration is cheap**. When the agent gives a wrong answer:

1. Identify which file caused the wrong reasoning (usually `context.md` — a missing definition).
2. Edit that file. One paragraph is often enough.
3. Restart the MCP container (or wait for the watcher to pick up the change).
4. Re-ask the same question. Verify the agent's reasoning now uses the new context.
5. Commit the diff to the client repo.

Over months, the four files become the **operational memory** of the analytics team. Every misunderstanding becomes a permanent improvement. The skill ages well rather than rotting.

## Multi-skill rules

When the deployment has multiple skills:

- **Each skill is independent.** No cross-skill file references — each `<skill>/context.md` is self-contained.
- **Tables can overlap.** Two skills can both list `dim_customers` if both need it. The server allows the union of allowlists when one query references one skill at a time.
- **Names must be unique.** `sales`, `finance`, `marketing` — no collisions across the directory.
- **Don't make mega-skills.** A skill that covers "sales + finance + operations" loses the routing benefit (the agent can't selectively load one domain's context). Prefer narrow, deep skills.

Rule of thumb: a skill should fit on a whiteboard. If you can't explain its scope in 30 seconds, it's two skills.

## Reference deployment

`pol-cc/skills-sapiens` is the original production deployment of this pattern (private repo). Its first active skill `analitica-comercial` covers `raw_sql_server.VistaInformeAlbaranesClientes` plus four ERP tables, and has been iterated over months — every "the agent got X wrong" became a `context.md` edit. The pattern works; note that skills-sapiens uses a slightly different on-disk layout (see next section).

## Reconciliation with the skills-sapiens reference

The production reference uses a slightly different on-disk layout than the four-file pattern above:

```
skills/<skill>/
├── SKILL.md                    ← YAML frontmatter (name, description) — entry point, not descriptor.json
├── context.md
├── examples.sql
└── sources/
    └── <source>/
        ├── schema.md           ← per-source column docs
        └── semantics.md        ← per-source business meaning / column richness
```

Two differences:

- **`SKILL.md` with YAML frontmatter** is the entry point instead of `descriptor.json`. The frontmatter carries `name` + `description` (same shape Claude Code skills use).
- **Per-source `sources/<source>/schema.md` + `semantics.md`** instead of a single flat `schema.md` — one folder per underlying data source, with richer per-source column documentation split across schema and semantics.

The trade-off:

| | `descriptor.json` (this repo) | `SKILL.md` frontmatter (skills-sapiens) |
|---|---|---|
| Table allowlist | **Machine-enforceable** — the server reads `tables[]` and rejects out-of-scope queries | Not machine-read for enforcement; scope is documented in prose |
| Readability | JSON, terse | More human-readable, fits the Claude Code skill convention |
| Per-source detail | Single `schema.md` | Per-source `schema.md` + `semantics.md` — richer column docs |

Both are valid. **This repo standardizes on `descriptor.json`** because the enforceable table allowlist is the security boundary (see above), and machine-enforcement beats prose for that job. A deployment may adopt the skills-sapiens `SKILL.md` + per-source layout if it prefers human-readable entry points and richer per-source semantics — just be aware the table allowlist then lives in prose, so enforcement must come from the BQ service account's IAM scope rather than the descriptor. The write tools (see [`mcp-github-writeback.md`](mcp-github-writeback.md)) address the skills-sapiens layout's logical keys (`context`, `examples`, `sources/<name>/schema`, `sources/<name>/semantics`).
