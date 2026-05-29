# Report format — the one-page health report

The markdown `verify-pipeline` emits after a [full sweep](health-checks.md#full-sweep--assemble-the-one-page-report). One screen, skimmable, traffic-light first. The global verdict is the **first line** so a busy operator reads one sentence and stops if it's green.

## Conventions

- Lights as emoji so they scan in any client: `🟢` green, `🟡` amber, `🔴` red, `⚪` unknown (skipped or VPS unreachable).
- Timestamps always **UTC, ISO-8601** (`2026-05-29T11:04Z`) — the whole stack runs on UTC.
- Ages in whole hours, derived from `green_hours`/`amber_hours` in the marker.
- **Read-only.** The emitted report never contains a command that mutates state. The footer points at `troubleshoot`; it does not pre-write a fix.
- Omit the MCP row entirely when `stack.mcp` is false — don't show a row that doesn't apply.

## Template

```markdown
# MDS health — <client> — <UTC timestamp>

**Verdict: <light> <one-line global verdict>**

| Layer | Status | Detail |
|---|---|---|
| Tailscale | <light> | VPS reachable; <N> nodes online<, on-prem offline> |
| Ingestion | <light> | <N>/<M> sources loaded fresh; oldest load <ts> (<age>h ago) |
| BQ raw freshness | <light> | oldest table <dataset>.<table> <age>h ago |
| Ingest reconciliation | <light> | <N>/<M> sources reconcile; <gap detail or "no gaps"> |
| BQ integrity | <light> | <N> pairs within threshold<; drift on ...> |
| dbt run | <light> | last run <ts> (<age>h ago); <all success | N failing> |
| MCP | <light> | container <Up/down>; endpoint <code> |

**Last sync per connection**

| Connection | Last success (UTC) | Age | Status |
|---|---|---|---|
| <name> | <ts> | <age>h | <light> |

**dbt freshness**
Last cron run <ts> (<age>h ago), schedule 0 11 * * *. <All N models success | listing of failing nodes>.

**Ingest reconciliation**
<All sources reconcile (source = destination, no gaps). | One bullet per mismatch: <source>.<table> source = A rows, dest = B rows (missing M); or sequence gap: ids/days <list>.>

**Integrity warnings**
<None. | One bullet per pair over threshold: raw.X = A rows, stg_X = B rows (drift D%).>

---
<If any red/amber:> One or more checks are degraded. This skill does not auto-fix — run **troubleshoot** to diagnose <layer>.
<If all green:> No action needed.
```

## Filled example — healthy with one warning

```markdown
# MDS health — acme-bakery — 2026-05-29T11:42Z

**Verdict: 🟡 Pipeline healthy with warnings — GA4 a day behind (expected), orders integrity drift 1.2%.**

| Layer | Status | Detail |
|---|---|---|
| Tailscale | 🟢 | VPS reachable; 2 nodes online |
| Ingestion | 🟢 | 3/3 dlt sources loaded fresh; oldest load 2026-05-29T07:48Z (4h ago) |
| BQ raw freshness | 🟡 | oldest table raw_ga4.events 28h ago (GA4 export lag — expected) |
| Ingest reconciliation | 🟢 | 3/3 sources reconcile; no sequence gaps |
| BQ integrity | 🟡 | 2/3 pairs within threshold; drift on stg_orders |
| dbt run | 🟢 | last run 2026-05-29T11:31Z (0h ago); all 14 models success |
| MCP | 🟢 | container Up 6 days; endpoint 401 (auth required) |

**Last load per source**

| Source | Last load (UTC) | Age | Status |
|---|---|---|---|
| sqlserver_erp | 2026-05-29T07:48Z | 4h | 🟢 |
| factorial_hr | 2026-05-29T07:52Z | 4h | 🟢 |
| ga4_export | 2026-05-28T08:10Z | 28h | 🟡 |

**dbt freshness**
Last cron run 2026-05-29T11:31Z (0h ago), schedule 0 11 * * *. All 14 models success, PASS=22 WARN=0 ERROR=0.

**Ingest reconciliation**
All sources reconcile: source row counts match destination within tolerance; the orders id sequence has no gaps. (The stg_orders shortfall below is a *transform*-layer drift, not an ingest gap — raw_erp.orders reconciles 1:1 with the ERP source.)

**Integrity warnings**
- raw_erp.orders = 18,402 rows, stg_orders = 18,180 rows (drift 1.2%, threshold 0.5%). Confirm whether staging's dedup explains the 222-row gap.

---
One or more checks are degraded. This skill does not auto-fix — run **troubleshoot** to look at the stg_orders integrity drift. The GA4 amber is the normal export lag and needs no action.
```

## Filled example — all green

```markdown
# MDS health — acme-bakery — 2026-05-30T11:40Z

**Verdict: 🟢 Pipeline healthy.**

| Layer | Status | Detail |
|---|---|---|
| Tailscale | 🟢 | VPS reachable; 2 nodes online |
| Ingestion | 🟢 | 3/3 dlt sources loaded fresh; oldest load 2026-05-30T07:50Z (4h ago) |
| BQ raw freshness | 🟢 | oldest table raw_ga4.events 4h ago |
| Ingest reconciliation | 🟢 | 3/3 sources reconcile; no sequence gaps |
| BQ integrity | 🟢 | 3/3 pairs within threshold |
| dbt run | 🟢 | last run 2026-05-30T11:30Z (0h ago); all 14 models success |
| MCP | 🟢 | container Up 7 days; endpoint 401 (auth required) |

---
No action needed.
```

## Filled example — VPS unreachable (red gate)

When the Tailscale gate is red, everything downstream is **unknown**, not red — be honest that the sweep couldn't run.

```markdown
# MDS health — acme-bakery — 2026-05-29T11:42Z

**Verdict: 🔴 Pipeline degraded — VPS unreachable over Tailscale. Could not assess downstream layers.**

| Layer | Status | Detail |
|---|---|---|
| Tailscale | 🔴 | SSH to acme-mds timed out (ConnectTimeout 5s) |
| Ingestion | ⚪ | unknown — VPS unreachable (`_dlt_loads` reachable via BQ, but on-prem source counts are not) |
| BQ raw freshness | 🟢 | oldest table raw_ga4.events 4h ago (BQ is independent of the VPS) |
| Ingest reconciliation | ⚪ | partial — destination counts/`_dlt_loads`/sequence gaps readable via BQ, but the source side needs the VPS/on-prem path |
| BQ integrity | ⚪ | unknown — staging counts unread |
| dbt run | ⚪ | unknown — run_results.json on the VPS |
| MCP | ⚪ | unknown — VPS unreachable |

---
The VPS could not be reached. BigQuery is hosted by Google and still reports fresh, so data through the last load is intact — but the dlt runner, dbt, and the MCP server are all on the unreachable VPS, and source-side reconciliation needs the VPS→on-prem path. Run **troubleshoot** starting at Tailscale reachability.
```

## Notes on rendering

- Keep the body under ~30 lines so it fits one screen. Per-connection and integrity detail tables are optional — drop them when there's a single connection and zero drift.
- Never paste raw run_results.json or full job JSON into the report. Summarize; the operator can ask `troubleshoot` for the raw evidence.
- The verdict sentence is the contract: green ⇒ stop, amber ⇒ informational, red ⇒ go to `troubleshoot`.
