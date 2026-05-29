# Sales — business context (EXAMPLE skill)

> This is the sample skill shipped with the MCP skeleton so the server works out
> of the box. Replace the `example-client-mds-prod` project, the table names, and
> every domain definition below with the real client's. Then rename the folder
> (and `descriptor.json.name`) from `example-sales` to your domain.

## What "sales" means here

For this client, sales refers to direct-to-customer orders placed through any
channel. Internal transfers between group companies, refurbished/returned-then-
resold items, and B2B reseller wholesale are EXCLUDED from sales analytics.

## Channels

- **Retail**: physical stores. `channel_code IN ('store_madrid', 'store_barcelona', ...)`.
- **Web**: online store. `channel_code = 'web'`. Note: web orders are sometimes
  fulfilled from a retail store — `channel_code` is the placement channel, not
  fulfillment.
- **Phone**: phone-in orders handled by customer service. Small share (~3%).
- **Marketplace**: third-party platforms (Amazon, etc.) — currently empty.

## "Active customer"

A customer is **active** if they placed at least one order in the trailing 90
days. The flag is `dim_customers.is_active_90d`. ALWAYS use this column — never
compute it inline from `fact_orders`.

## Net vs gross

- `gross_amount` = sum of line item prices BEFORE discounts and taxes.
- `net_amount` = gross MINUS discounts, BEFORE taxes (the figure that matches
  the P&L).
- Sales reports always use `net_amount` unless explicitly asked about gross.

## Currency

All amounts are in EUR (`default_currency` in descriptor.json). Foreign-currency
transactions are converted at order time using the daily ECB rate.

## Time period conventions

- **Today**: the current date in Madrid time (Europe/Madrid).
- **"This month"**: from the 1st of the current month to today, inclusive.
- **"Last month"**: full previous calendar month.
- **"YTD"**: from January 1st of the current year to today.
- **Fiscal year**: matches the calendar year (starts January).

## Common questions and where to find them

- "Revenue this month" → `revenue_monthly`, filter `month = DATE_TRUNC(CURRENT_DATE(), MONTH)`.
- "Top 10 customers by lifetime value" → `dim_customers` ordered by `ltv_eur` DESC.
- "Channel mix this quarter" → `revenue_monthly` grouped by `channel_code`.

## What this skill does NOT cover

- Marketing attribution (which campaign brought the customer) — a `marketing` skill.
- Inventory and stock levels — an `operations` skill.
- Customer support tickets — outside this MDS.
