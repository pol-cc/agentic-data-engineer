# Bootstrapping the first MCP skill

End state: one skill folder registered with the MCP server, the agent can answer natural-language questions about a domain of the warehouse. Total time: ~20-30 minutes (mostly writing context.md prose).

## Picking the domain

For the first skill, pick the domain with the **cleanest, smallest set of marts already in place**. Reasoning: the first skill is also the user's first taste of the agentic interface — a confident, correct answer to a simple question wins them over. A skill on a half-finished mart layer disappoints.

Typical first-skill choices, by ease:

| Domain | When easy | When hard |
|---|---|---|
| **sales** | Single fact_orders mart, dim_customers, one or two summary marts (revenue_monthly, top_customers) | Multi-currency, multi-channel attribution still in flight |
| **finance** | A clean P&L mart from dbt | Multi-company consolidation, exchange rates, accruals |
| **operations** | Simple agg of order processing times, error rates | Heavy domain logic (SLAs, multi-tier categorization) |
| **HR / employees** | Headcount, contracts, leave from Factorial | Compensation analytics (privacy-sensitive) |

Default for v0.3.0: **start with `sales`** if there's a fact_orders mart. Otherwise pick whatever the user has most ready.

## Preflight

```bash
ssh deploy@<client>-mds

# Confirm MCP server is up
docker ps | grep "^.*mcp\s"

# Confirm /home/deploy/mcp-skills exists and is empty (first skill)
ls /home/deploy/mcp-skills/
# Expected: empty
```

## Step A — Create the skill folder

```bash
SKILL=sales      # or whichever domain
mkdir -p /home/deploy/mcp-skills/$SKILL
cd /home/deploy/mcp-skills/$SKILL
```

The detailed folder pattern lives at [`../../add-mcp-skill/references/mcp-skill-folder-pattern.md`](../../add-mcp-skill/references/mcp-skill-folder-pattern.md). Brief summary: four files, each with one purpose.

## Step B — Write `descriptor.json`

```bash
cat > descriptor.json <<'EOF'
{
  "name": "sales",
  "description": "Sales analytics for orders, revenue, customers, and channels.",
  "version": "0.1.0",
  "tables": [
    "<client>-mds-prod.analytics.dim_customers",
    "<client>-mds-prod.analytics.fact_orders",
    "<client>-mds-prod.analytics.revenue_monthly"
  ],
  "max_query_bytes": 2147483648,
  "max_rows": 1000,
  "default_currency": "EUR",
  "fiscal_year_start_month": 1
}
EOF
```

Replace placeholders. The `tables` array is the **enforcement boundary** — the MCP server's `run_bq_query` tool will reject queries that reference any table not in this list. Pick the smallest set the agent needs.

Don't include staging or intermediate tables. The MCP exposes deliverable layer (marts) only. If the agent needs raw data, the skill is too low-level — add an intermediate mart first.

## Step C — Write `context.md` (the business glossary)

This is the most important file. It tells the LLM what concepts mean in this client's world. Bad `context.md` = confidently wrong queries. Good `context.md` = the agent reasons like a real analyst.

```bash
cat > context.md <<'EOF'
# Sales — business context

## What "sales" means here

For <client>, sales refers to direct-to-customer orders placed through any channel. Internal transfers between companies in the group, refurbished/returned-then-resold items, and B2B reseller wholesale are EXCLUDED from sales analytics.

## Channels

- **Retail**: physical stores. Identified by `channel_code IN ('store_madrid', 'store_barcelona', ...)`. See `dim_customers.channel` for the full list.
- **Web**: orders placed through the online store. `channel_code = 'web'`. Note: web orders are sometimes fulfilled from a retail store — the `channel_code` is the placement channel, not fulfillment.
- **Phone**: phone-in orders, handled by customer service. Small share (~3%).
- **Marketplace**: third-party platforms (Amazon, etc.) — currently empty, planned for 2026 Q3.

## "Active customer"

A customer is **active** if they placed at least one order in the trailing 90 days. The flag is `dim_customers.is_active_90d`. Always use this column — never compute it inline from `fact_orders`.

## Currency

All amounts in `fact_orders.gross_amount` and `net_amount` are in EUR. Multi-currency support is on the roadmap; until then, foreign-currency transactions are converted at order time using the daily ECB rate.

## Net vs gross

- `gross_amount` = sum of line item prices BEFORE discounts and taxes
- `net_amount` = gross MINUS discounts, BEFORE taxes (this is the figure that matches the P&L)
- Sales reports always use `net_amount` unless explicitly asked about gross.

## Time period conventions

- **Today**: assume the current date in Madrid time (Europe/Madrid).
- **"This month"**: from the 1st of the current month to today, inclusive.
- **"Last month"**: full previous calendar month.
- **"YTD"**: from January 1st of the current year to today.
- **Fiscal year**: matches calendar year. Starts January.

## Common questions and where to find them

- "Revenue this month": `revenue_monthly` table, filter `month = DATE_TRUNC(CURRENT_DATE(), MONTH)`.
- "Top 10 customers by lifetime value": `dim_customers` ordered by `ltv_eur` DESC.
- "Channel mix this quarter": `fact_orders` grouped by `channel_code`, aggregated to current quarter.

## What this skill does NOT cover

- Marketing attribution (which campaign brought the customer) — that's `marketing` skill (not yet built).
- Inventory and stock levels — that's `operations` skill.
- Customer support tickets — outside this MDS.
EOF
```

**Quality bar for `context.md`**:

- Reads like an analyst's onboarding doc.
- Defines every term that's domain-specific.
- Explicitly flags "always use column X, never compute inline" pitfalls.
- Notes any common mistakes (web vs fulfillment channel, gross vs net).
- Says what the skill does NOT cover, so the agent knows when to give up gracefully.

Length: 100-300 lines is typical. The agent loads this entire file on every relevant query — keep it focused.

## Step D — Write `schema.md` (per-table column docs)

```bash
cat > schema.md <<'EOF'
# Sales schema reference

## `<client>-mds-prod.analytics.dim_customers`

One row per customer. Source of truth for customer attributes.

| Column | Type | Description |
|---|---|---|
| `customer_id` | STRING | Primary key. Stable across renames. Format: `cus_<8 hex>`. |
| `customer_name` | STRING | Display name. May be NULL for guest-checkout customers. |
| `customer_country` | STRING | ISO 3166-1 alpha-2 country code. |
| `channel` | STRING | Origination channel: see `context.md` for valid values. |
| `created_at` | TIMESTAMP | First order date. |
| `is_active_90d` | BOOLEAN | TRUE if any order in trailing 90 days. ALWAYS use this column for "active" status. |
| `ltv_eur` | FLOAT64 | Lifetime value in EUR, gross. Updated daily. |

Gotchas:
- ~5% of rows have NULL `customer_name` (guest checkouts). Don't filter them out unless asked.
- `country` is NULL for legacy customers (pre-2023). Treat as 'UNKNOWN' if reporting.

## `<client>-mds-prod.analytics.fact_orders`

One row per order. The atomic event table.

| Column | Type | Description |
|---|---|---|
| `order_id` | STRING | Primary key. Format: `ord_<8 hex>`. |
| `customer_id` | STRING | FK to dim_customers. NOT NULL. |
| `order_date` | DATE | Date the order was placed. Use this for time filtering. |
| `channel_code` | STRING | Channel of placement: store_*, web, phone, marketplace. |
| `gross_amount` | FLOAT64 | Sum of line item prices BEFORE discounts/taxes. EUR. |
| `discount_amount` | FLOAT64 | Total discounts applied. Always >= 0. |
| `net_amount` | FLOAT64 | gross_amount - discount_amount. The figure used in P&L. |
| `line_count` | INT64 | Number of distinct SKUs in the order. |
| `status` | STRING | One of: completed, returned, refunded, partial_refund. |

Gotchas:
- `gross_amount` and `net_amount` are NEVER negative — returns are separate rows with `status='returned'` and the original order_id has `status='completed'`. To compute "net sales" excluding returns, filter `status='completed'`.
- `order_date` is the placement date in Madrid TZ. The underlying timestamp `order_at` is in UTC if you need precision.

## `<client>-mds-prod.analytics.revenue_monthly`

Pre-aggregated mart. One row per (month, channel_code).

| Column | Type | Description |
|---|---|---|
| `month` | DATE | First day of the month. |
| `channel_code` | STRING | Channel. |
| `net_revenue_eur` | FLOAT64 | Sum of completed orders' net_amount for this month/channel. |
| `order_count` | INT64 | Distinct order_id count. |
| `customer_count` | INT64 | Distinct customer_id count for orders in this month. |

Use this mart for "revenue by month" questions — it's smaller and faster than aggregating `fact_orders`.

## Common joins

- Customer attributes on an order: `fact_orders LEFT JOIN dim_customers USING (customer_id)`.
- Don't join `revenue_monthly` to anything — it's a leaf mart.
EOF
```

**Quality bar for `schema.md`**:

- Every column the agent will query has a row.
- Every gotcha that has bitten a human analyst is listed.
- Joins are explicit (the LLM will copy them).

## Step E — Write `examples.sql`

```bash
cat > examples.sql <<'EOF'
-- Example queries for the sales skill. The LLM uses these as patterns when composing answers.

-- ============================================================
-- Q: What was total revenue last month?
-- ============================================================
SELECT
  SUM(net_revenue_eur) AS revenue_eur,
  SUM(order_count) AS orders
FROM `<client>-mds-prod.analytics.revenue_monthly`
WHERE month = DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 1 MONTH), MONTH);

-- ============================================================
-- Q: How does this month compare to the same month last year?
-- ============================================================
WITH current_month AS (
  SELECT SUM(net_revenue_eur) AS revenue
  FROM `<client>-mds-prod.analytics.revenue_monthly`
  WHERE month = DATE_TRUNC(CURRENT_DATE(), MONTH)
),
last_year_same_month AS (
  SELECT SUM(net_revenue_eur) AS revenue
  FROM `<client>-mds-prod.analytics.revenue_monthly`
  WHERE month = DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 1 YEAR), MONTH)
)
SELECT
  current_month.revenue AS this_month,
  last_year_same_month.revenue AS year_ago,
  SAFE_DIVIDE(current_month.revenue - last_year_same_month.revenue, last_year_same_month.revenue) AS yoy_change
FROM current_month, last_year_same_month;

-- ============================================================
-- Q: Top 10 customers by lifetime value
-- ============================================================
SELECT
  customer_id,
  customer_name,
  customer_country,
  ROUND(ltv_eur, 2) AS ltv_eur,
  is_active_90d
FROM `<client>-mds-prod.analytics.dim_customers`
WHERE customer_name IS NOT NULL    -- exclude guests for this view
ORDER BY ltv_eur DESC
LIMIT 10;

-- ============================================================
-- Q: Channel mix this year (YTD)
-- ============================================================
SELECT
  channel_code,
  SUM(net_revenue_eur) AS revenue,
  SAFE_DIVIDE(SUM(net_revenue_eur), SUM(SUM(net_revenue_eur)) OVER ()) AS share
FROM `<client>-mds-prod.analytics.revenue_monthly`
WHERE month >= DATE_TRUNC(CURRENT_DATE(), YEAR)
GROUP BY channel_code
ORDER BY revenue DESC;

-- ============================================================
-- Q: Orders today (for ops sanity check)
-- ============================================================
SELECT
  channel_code,
  COUNT(*) AS orders,
  SUM(net_amount) AS net_revenue
FROM `<client>-mds-prod.analytics.fact_orders`
WHERE order_date = CURRENT_DATE() AND status = 'completed'
GROUP BY channel_code
ORDER BY net_revenue DESC;
EOF
```

**Quality bar for `examples.sql`**:

- 3-5 queries that span the typical question types in this domain.
- Comments above each query stating the natural-language question it answers.
- Pattern-match worthy: the LLM should be able to swap in user-specified filters/aggregations.
- Use the same tables and columns documented in `schema.md` — consistency between files is enforced by review, not code.

## Step F — Restart the MCP container to pick up the skill

The skill loader watches the `/skills` directory but may need a restart depending on implementation.

```bash
cd /home/deploy/mcp-server
docker compose restart mcp

# Verify loaded
docker logs mcp 2>&1 | grep -i "loaded.*skill"
# Expected: "[mcp] loaded 1 skill from /skills: sales"
```

## Step G — Verify from claude.ai

In claude.ai:

1. With the MCP connector already added (Phase 3 Step 8), start a new chat.
2. Ask: "Using the <client> MDS connector, what was last month's total revenue?"
3. Expected: claude.ai calls `list_skills`, sees `sales`, calls `get_skill_context('sales')`, reads `examples.sql`, adapts the "last month revenue" query with current dates, calls `run_bq_query(sql)`, gets the number, replies with prose + the figure.
4. Verify the answer matches what you'd get by running the same SQL directly in `bq query`.

If the agent picks the wrong table or computes wrongly: the issue is in `context.md` or `schema.md`. Add the missing nuance, restart, retry. **This is the iteration loop the MCP server exists for.**

## Step H — Commit to the client repo

```bash
# On the user's laptop, in the client repo
mkdir -p mcp-skills/<SKILL>
scp -r deploy@<client>-mds:/home/deploy/mcp-skills/<SKILL>/* mcp-skills/<SKILL>/

git add mcp-skills/<SKILL>/
git commit -m "Phase 3: bootstrap MCP skill: <SKILL>"
```

The skill files are now versioned. Future edits (the "improve context.md when the agent gets something wrong" loop) happen via PR, and the user can review the change history.

## Common gotchas

- **Agent doesn't pick the right query pattern** → `examples.sql` is too narrow. Add more variety.
- **Agent returns "I don't know"** when it should be able to answer → `context.md` is too vague. Be explicit: "to compute X, query Y, with filter Z".
- **Agent runs a query that exceeds the bytes cap** → either raise the cap in `descriptor.json` (carefully — costs money) or add a hint in `context.md` that points the agent at a smaller pre-aggregated mart for the typical question.
- **Skill folder exists but `list_skills()` doesn't return it** → check the MCP server log; usually a missing/invalid `descriptor.json`. The JSON must be valid and `name` must be unique.

## What the second skill looks like

Phase 3 Step 7 (this reference) creates the first skill. Subsequent skills use the `add-mcp-skill` skill — same pattern, different domain. The structure under `/home/deploy/mcp-skills/` grows:

```
mcp-skills/
├── sales/
│   ├── descriptor.json
│   ├── context.md
│   ├── schema.md
│   └── examples.sql
├── finance/    ← added later via add-mcp-skill
│   └── ...
└── operations/ ← added later
    └── ...
```

Each skill is independent. The MCP server treats them as independent units; the user explicitly chooses which to ask about.
