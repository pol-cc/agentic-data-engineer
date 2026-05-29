# Sales schema reference (EXAMPLE skill)

## `example-client-mds-prod.analytics.dim_customers`

One row per customer. Source of truth for customer attributes.

| Column | Type | Description |
|---|---|---|
| `customer_id` | STRING | Primary key. Stable across renames. Format: `cus_<8 hex>`. |
| `customer_name` | STRING | Display name. May be NULL for guest-checkout customers. |
| `customer_country` | STRING | ISO 3166-1 alpha-2 country code. |
| `channel` | STRING | Origination channel: see `context.md` for valid values. |
| `created_at` | TIMESTAMP | First order date. |
| `is_active_90d` | BOOLEAN | TRUE if any order in trailing 90 days. ALWAYS use this for "active" status. |
| `ltv_eur` | FLOAT64 | Lifetime value in EUR, gross. Updated daily. |

Gotchas:
- ~5% of rows have NULL `customer_name` (guest checkouts). Don't filter them out unless asked.
- `customer_country` is NULL for legacy customers (pre-2023). Treat as 'UNKNOWN' if reporting.

## `example-client-mds-prod.analytics.fact_orders`

One row per order. The atomic event table.

| Column | Type | Description |
|---|---|---|
| `order_id` | STRING | Primary key. Format: `ord_<8 hex>`. |
| `customer_id` | STRING | FK to dim_customers. NOT NULL. |
| `order_date` | DATE | Date the order was placed (Madrid TZ). Use this for time filtering. |
| `channel_code` | STRING | Channel of placement: store_*, web, phone, marketplace. |
| `gross_amount` | FLOAT64 | Sum of line item prices BEFORE discounts/taxes. EUR. |
| `discount_amount` | FLOAT64 | Total discounts applied. Always >= 0. |
| `net_amount` | FLOAT64 | gross_amount - discount_amount. The figure used in the P&L. |
| `line_count` | INT64 | Number of distinct SKUs in the order. |
| `status` | STRING | One of: completed, returned, refunded, partial_refund. |

Gotchas:
- `gross_amount` and `net_amount` are NEVER negative — returns are separate rows
  with `status='returned'`. To compute "net sales" excluding returns, filter
  `status = 'completed'`.
- `order_date` is the placement date in Madrid TZ. The underlying `order_at`
  timestamp is in UTC if you need precision.

## `example-client-mds-prod.analytics.revenue_monthly`

Pre-aggregated mart. One row per (month, channel_code).

| Column | Type | Description |
|---|---|---|
| `month` | DATE | First day of the month. |
| `channel_code` | STRING | Channel. |
| `net_revenue_eur` | FLOAT64 | Sum of completed orders' net_amount for this month/channel. |
| `order_count` | INT64 | Distinct order_id count. |
| `customer_count` | INT64 | Distinct customer_id count for orders in this month. |

Use this mart for "revenue by month" questions — smaller and faster than
aggregating `fact_orders`.

## Common joins

- Customer attributes on an order: `fact_orders LEFT JOIN dim_customers USING (customer_id)`.
- Don't join `revenue_monthly` to anything — it's a leaf mart.
