-- Example queries for the sales skill (EXAMPLE). The LLM uses these as patterns
-- when composing answers. Keep every table/column consistent with schema.md.

-- ============================================================
-- Q: What was total revenue last month?
-- ============================================================
SELECT
  SUM(net_revenue_eur) AS revenue_eur,
  SUM(order_count)     AS orders
FROM `example-client-mds-prod.analytics.revenue_monthly`
WHERE month = DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 1 MONTH), MONTH);

-- ============================================================
-- Q: How does this month compare to the same month last year?
-- ============================================================
WITH current_month AS (
  SELECT SUM(net_revenue_eur) AS revenue
  FROM `example-client-mds-prod.analytics.revenue_monthly`
  WHERE month = DATE_TRUNC(CURRENT_DATE(), MONTH)
),
last_year_same_month AS (
  SELECT SUM(net_revenue_eur) AS revenue
  FROM `example-client-mds-prod.analytics.revenue_monthly`
  WHERE month = DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 1 YEAR), MONTH)
)
SELECT
  current_month.revenue AS this_month,
  last_year_same_month.revenue AS year_ago,
  SAFE_DIVIDE(current_month.revenue - last_year_same_month.revenue,
              last_year_same_month.revenue) AS yoy_change
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
FROM `example-client-mds-prod.analytics.dim_customers`
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
FROM `example-client-mds-prod.analytics.revenue_monthly`
WHERE month >= DATE_TRUNC(CURRENT_DATE(), YEAR)
GROUP BY channel_code
ORDER BY revenue DESC;

-- ============================================================
-- Q: Orders today (ops sanity check)
-- ============================================================
SELECT
  channel_code,
  COUNT(*)         AS orders,
  SUM(net_amount)  AS net_revenue
FROM `example-client-mds-prod.analytics.fact_orders`
WHERE order_date = CURRENT_DATE() AND status = 'completed'
GROUP BY channel_code
ORDER BY net_revenue DESC;
