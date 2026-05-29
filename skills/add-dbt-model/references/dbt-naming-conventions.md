# dbt naming conventions

The opinionated naming used by every `agentic-data-engineer` deployment. Following these conventions lets the agent reason about models by inspection — a `stg_factorial_employees` table is obviously a Factorial-HR staging model for the `employees` raw table, no documentation needed.

## File and model name patterns

| Layer | File name pattern | Example |
|---|---|---|
| Staging | `stg_<source>_<table>.sql` | `stg_factorial_employees.sql` |
| Intermediate | `int_<concept>.sql` (no source prefix) | `int_orders_with_customer.sql` |
| Marts (dimension) | `dim_<entity>.sql` | `dim_customers.sql` |
| Marts (fact) | `fact_<event>.sql` | `fact_orders.sql` |
| Marts (summary / report) | `<domain>_<grain>.sql` (no `fact_`/`dim_` prefix) | `revenue_monthly.sql`, `cohort_retention.sql` |

The same name is used for the file and the dbt model (and therefore the BigQuery table). `stg_factorial_employees.sql` becomes `<project>.analytics_staging.stg_factorial_employees`.

## Directory structure

```
models/
├── staging/
│   ├── factorial/
│   │   ├── _factorial__sources.yml          ← raw source declarations
│   │   ├── _factorial__models.yml           ← staging model tests/docs
│   │   ├── stg_factorial_employees.sql
│   │   ├── stg_factorial_contracts.sql
│   │   └── stg_factorial_leaves.sql
│   ├── sql_server/
│   │   ├── _sql_server__sources.yml
│   │   ├── stg_sql_server_orders.sql
│   │   └── stg_sql_server_customers.sql
│   └── google_ads/
│       └── stg_google_ads_campaigns.sql
├── intermediate/
│   ├── _intermediate__models.yml
│   ├── int_orders_with_customer.sql
│   └── int_employees_with_contract.sql
└── marts/
    ├── _marts__models.yml
    ├── dim_customers.sql
    ├── dim_employees.sql
    ├── fact_orders.sql
    └── revenue_monthly.sql
```

Rules:

- **Staging is partitioned by source**: one subfolder per source (`factorial/`, `sql_server/`, `google_ads/`). One staging model per important raw table.
- **Intermediate is flat**: no subfolders. Intermediate models are reusable across marts; folders would over-categorize them.
- **Marts is mostly flat**, but big projects can introduce subject-area subfolders (`marts/finance/`, `marts/sales/`) once you have 15+ marts. Avoid earlier.
- **YAML files use double-underscore convention** (`_factorial__sources.yml`). The leading underscore makes them sort to the top in file listings. The doubled underscore separates the prefix-context from the suffix-type.

## Column naming

Inside the SQL:

- **Snake case** always, never camelCase or PascalCase.
- **Primary keys**: `<entity>_id` (e.g. `employee_id`, `order_id`). Avoid bare `id`.
- **Foreign keys**: same as the primary key of the referenced entity (`customer_id` in `fact_orders` refers to `dim_customers.customer_id`).
- **Timestamps**: `<event>_at` for points in time (`created_at`, `updated_at`, `loaded_at`). `<event>_date` for date-only values.
- **Booleans**: `is_<predicate>` or `has_<thing>` (`is_active`, `has_contract`). Never `<thing>_flag` or `<thing>_bool`.
- **Counts**: `<thing>_count` (`order_count`, `employee_count`).
- **Amounts**: `<thing>_amount` for money (`order_amount`, `salary_amount`). Add `_eur` / `_usd` suffix if multi-currency.
- **Rates / percentages**: `<thing>_pct` for stored percentages (`margin_pct`). Distinguish from raw ratio: `margin_ratio` is 0.0-1.0, `margin_pct` is 0-100.

## SQL style inside models

Required:

- **CTEs** (`WITH ... AS (...)`) over deeply nested subqueries. Each CTE has a clear purpose.
- **Explicit column lists** in `SELECT` — never `SELECT *` in marts. Acceptable in staging only when "passthrough rename" is the model's intent.
- **One CTE per concern**: source → cleanup → cast → output. Don't pack everything into one giant SELECT.
- **Lowercase keywords** (`select`, `from`, `where`, `with`) — easier to scan when the lines mix code and identifiers. dbt's `sqlfluff` formatter defaults to this.
- **Trailing commas** in column lists (one column per line). Easier diffs.

Avoid:

- `SELECT *` in marts (breaks when upstream adds a column you don't want).
- Hardcoded BigQuery project IDs (`\`my-project.foo.bar\``) — use `{{ ref('...') }}` or `{{ source('...', '...') }}`.
- Logic in marts that duplicates intermediate work — extract to an intermediate model.

## Canonical staging model shape

Every staging model follows roughly this pattern:

```sql
{{ config(materialized='view') }}

with source as (
    select * from {{ source('factorial', 'employees') }}
),

renamed as (
    select
        -- Identifiers
        id                          as employee_id,
        company_id                  as company_id,

        -- Attributes
        first_name                  as first_name,
        last_name                   as last_name,
        cast(birthday as date)      as birth_date,
        cast(hired_on as date)      as hired_date,
        cast(terminated_on as date) as terminated_date,

        -- Metadata
        _dlt_load_id                as load_id     -- dlt's opaque load-id string; join _dlt_loads for the timestamp. (Airbyte legacy: _airbyte_extracted_at as loaded_at)
    from source
    where id is not null     -- drop garbage rows from raw
)

select * from renamed
```

Key points:

- One CTE (`source`) just pulls the raw, no logic.
- One CTE (`renamed`) does all transformations.
- Casts are explicit even when types look right — protects against schema drift.
- The load-metadata column is carried through for downstream freshness. dlt's `_dlt_load_id` is an opaque load-id *string*, not a timestamp — carry it as `load_id` and join the `_dlt_loads` table when you need the wall-clock `loaded_at`. (Airbyte legacy: `_airbyte_extracted_at` is already a timestamp, so alias it straight to `loaded_at`.)
- The final `select * from renamed` is the only `*` allowed — it's selecting from a CTE you just defined, not from the warehouse.

## Canonical mart model shape

```sql
{{ config(materialized='table') }}

with orders as (
    select * from {{ ref('stg_shopify_orders') }}
),

customers as (
    select * from {{ ref('dim_customers') }}
),

joined as (
    select
        o.order_id,
        o.customer_id,
        c.customer_name,
        c.customer_country,
        o.order_date,
        o.order_amount,
        o.line_count
    from orders o
    left join customers c using (customer_id)
)

select * from joined
```

Key points:

- One CTE per input model. Each `ref()` appears exactly once.
- The join lives in its own CTE.
- Final `select` is the deliverable shape.

## Tests pattern

Every model gets at minimum two tests via the `schema.yml` next to it:

```yaml
version: 2

models:
  - name: dim_customers
    description: "One row per customer. Source of truth for customer attributes."
    columns:
      - name: customer_id
        description: "Primary key."
        tests:
          - not_null
          - unique
      - name: customer_country
        tests:
          - accepted_values:
              values: ["ES", "FR", "IT", "PT", "DE", "OTHER"]
```

Minimum bar:

- Every primary key: `not_null` + `unique`
- Every foreign key: `not_null` (uniqueness depends on the model)
- Every "enum" column: `accepted_values`
- Every monetary column: optional `dbt_utils.expression_is_true` with `>= 0` (if applicable)

Tests catch ~80% of upstream regressions for ~5 lines of YAML per column. Cheap insurance.
