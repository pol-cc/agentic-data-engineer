# dlt state, VPS reconstruction & mandatory reconciliation

End state: you understand **where dlt keeps its state**, why that makes the VPS a *cattle* box you can throw away and rebuild without data loss, and exactly **which reconciliation checks every load must pass**. This is the reference that makes dlt *safe*. Read it before trusting any dlt load.

## Where dlt state lives

dlt splits state across two places — and the important half is in the warehouse, not on the box:

| Where | What | Survives VPS loss? |
|---|---|---|
| **Destination warehouse** (the `raw_<source>` dataset) | `_dlt_pipeline_state` (incremental cursors / high-water marks), `_dlt_loads` (one row per load, with status), `_dlt_version` (schema versions) | **Yes** — it's in BigQuery |
| **Local working dir** (`~/.dlt/pipelines/<name>/` on the box) | A local cache/mirror of the same state + working files for the in-progress load | No — but it's only a cache |

The local working dir is a **performance cache**, not the source of truth. On a fresh box, dlt rehydrates incremental state **from the destination** `_dlt_pipeline_state` on the next run.

### The three warehouse tables

In `raw_<source>` you'll find:

- **`_dlt_loads`** — one row per `pipeline.run()`. Columns include `load_id`, `status` (`0` = succeeded), `inserted_at`. This is your "did the load complete?" ledger.
- **`_dlt_pipeline_state`** — the serialized incremental cursors (the `updated_at` high-water mark per resource). This is what a fresh box reads to resume without gaps.
- **`_dlt_version`** — schema + dlt version history for the dataset.

Every loaded table also carries a `_dlt_load_id` column linking each row back to the load that wrote it — useful for reconciliation and for surgically removing a bad load.

## Why this makes the VPS reconstructible (cattle, not pet)

> **The disaster-recovery test:** the VPS is wiped. You provision a fresh box, `pip install dlt`, re-clone the committed pipeline scripts, restore the secrets, and run `python load.py`. dlt reads `_dlt_pipeline_state` **from the warehouse**, sees the last cursor, and resumes from exactly where it left off — **no gap, no double-load.**

This is the central reason dlt is the default over Airbyte:

| | dlt | Airbyte OSS |
|---|---|---|
| Incremental state lives… | in the **destination warehouse** | on the **box** (local DB/volume) |
| Lose the VPS → | fresh box restores cursors from the warehouse, resumes cleanly | **state is gone** — you either re-sync from scratch (cost/time) or risk a gap |
| The box is… | **cattle** — disposable, rebuildable from Git + warehouse | a **pet** — its local state is irreplaceable |

The committed artifacts (`load.py`, `reconcile.py`, `.dlt/config.toml` minus secrets — principle 4) plus the warehouse state plus the secrets store are **everything** needed to rebuild. Nothing critical lives only on the box. That is the whole point of cattle-not-pet (and of principle 7's open escape hatch — your data and your cursors are in standard warehouse tables, not locked in a tool).

> **Caveat to verify per version:** dlt's exact state-restore behavior on a fresh box depends on the dlt version and that the destination state tables are intact. Before relying on it for a real DR drill, **verify against the installed dlt version** — do a dry rebuild on a throwaway box and confirm the cursor resumes.

## Why reconciliation is MANDATORY, not optional

> **dlt fails silently.** A wrong paginator, a non-monotonic incremental cursor, or a mis-typed `cursor_path` does **not** raise — `pipeline.run()` returns a clean `load_info` and `_dlt_loads.status = 0`. The load "succeeded." It just **dropped half the rows**. Without reconciliation, you cannot tell a complete load from a gappy one. **This makes an unreconciled dlt pipeline *more* dangerous than Airbyte** — Airbyte at least tends to fail loudly.

So the rule for this entire skill:

> **No source is "done" until reconciliation passes. Run it after the first load and on every scheduled run. A failed reconcile must alert — never pass quietly.**

`load_info` proves the load *ran*. Only reconciliation proves the data is *complete*.

## Mandatory reconciliation checks

Run all of these after every load. The [`../templates/reconcile.py.template`](../templates/reconcile.py.template) bundles them.

### 1. Load status — did dlt itself report success?

```sql
-- In the raw_<source> dataset. Latest load must be status 0 (succeeded).
SELECT load_id, status, inserted_at
FROM `raw_example._dlt_loads`
ORDER BY inserted_at DESC
LIMIT 1;
```

If `status != 0`, stop — the load errored. (Necessary, not sufficient: a `status = 0` load can still be gappy. The next checks catch that.)

### 2. Row count — source vs destination (the gap catcher)

The check that catches silent truncation. Compare the source-reported count to the destination count:

```python
# reconcile.py (sketch — verify client APIs against your installed versions)
import dlt
from google.cloud import bigquery

bq = bigquery.Client()

def dest_count(table: str) -> int:
    q = f"SELECT COUNT(*) AS n FROM `raw_example.{table}`"
    return list(bq.query(q))[0].n

# source_count: for a DB, SELECT COUNT(*) over the tailnet; for an API, the `total` field.
def source_count(table: str) -> int:
    ...  # SELECT COUNT(*) FROM <table>  (DB)  /  GET ...?per_page=1 -> meta.total  (API)

for table in ["orders", "customers"]:
    s, d = source_count(table), dest_count(table)
    tol = 0  # exact for replace/full; allow a small tolerance for in-flight rows on incremental
    if abs(s - d) > tol:
        raise SystemExit(f"[RECONCILE FAIL] {table}: source={s} dest={d} — DATA GAP")
    print(f"[ok] {table}: source={s} dest={d}")
```

- **`replace` / full loads** → counts must match **exactly** (tolerance 0).
- **`merge` / incremental** → allow a small tolerance only for rows written between the two counts; investigate anything larger.

### 3. Freshness — is the newest row recent enough?

```sql
SELECT MAX(updated_at) AS newest
FROM `raw_example.orders`;
-- Compare to expected cadence. For a daily source, "newest" should be within ~1 day.
-- For time-lagged sources (e.g. GA4-style yesterday-only exports) compare to YESTERDAY, not today.
```

A stale `MAX(updated_at)` means the incremental cursor stopped advancing — a gap forming in real time.

### 4. Sequence gaps — holes in an id/sequence stream

```sql
-- For a monotonic integer primary key, expected count == max - min + 1.
SELECT
  MIN(id) AS lo,
  MAX(id) AS hi,
  COUNT(*) AS n,
  (MAX(id) - MIN(id) + 1) - COUNT(*) AS missing
FROM `raw_example.orders`;
-- missing > 0  → holes in the sequence → rows dropped. Investigate the paginator/cursor.
```

(Only valid where the source guarantees a dense, monotonic id. For sparse/UUID keys, lean on checks 2 and 3.)

### Putting it together

`reconcile.py` should exit **non-zero** on any failure so cron/orchestration treats a gap as a failed run and alerts. Wire it as `python load.py && python reconcile.py` — the reconcile gates the load. A green load over a silent gap is the exact failure this skill exists to prevent.

## Common gotchas

- **Trusting `load_info` / `status = 0` as proof of completeness.** It only proves the load ran. Always run checks 2–4.
- **Reconciling only on the first load.** A cursor can break weeks later (source changes a timestamp's behavior). Reconcile **every** scheduled run, not just setup.
- **Wiping `~/.dlt/` to "fix" a pipeline.** That only clears the local cache — state still lives in the warehouse. Re-running rehydrates from `_dlt_pipeline_state`; to truly reset, you must also clear the destination state tables (rarely what you want).
- **Deleting a bad load.** Use the `_dlt_load_id` column to filter/remove just the rows from the offending `load_id` rather than nuking the table.
- **Count mismatch on `merge` flagged as failure.** A small, explainable delta from in-flight rows is normal on incremental; a *large* one is a real gap. Set tolerance deliberately, don't blanket-ignore.
- **No alert on reconcile failure.** A failing `reconcile.py` that nobody sees is the same as no reconcile. It must exit non-zero and the orchestrator must surface it (see `verify-pipeline`).
- **Assuming DR works without testing it.** Verify the fresh-box state restore against your installed dlt version before relying on it in an emergency.
