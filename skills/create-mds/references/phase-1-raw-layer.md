# Phase 1 — Raw Layer Playbook

This is the orchestrator for Phase 1 of `create-mds`. It produces a working MDS up to the **raw layer**: data from at least one source landing in BigQuery as `raw_<source>` datasets via **dlt**, loaded daily. No dbt, no MCP yet — those are Phase 2 and 3.

The default ingestion engine is **dlt** ([data load tool](https://dlthub.com)) — a Python library the agent drives directly with `python load.py`, not a platform. Airbyte OSS remains documented as an [alternative ingestion path](airbyte-install.md) for teams already committed to it, but it is **not the default**. Why dlt: a short feedback loop (write a script, run it, read the row count or stack trace immediately), and warehouse-resident state — dlt persists its incremental cursors to the *destination* (`_dlt_*` tables in BigQuery), so a lost VPS is rebuilt from the repo with cursors intact. **Cattle, not pet.**

End state of Phase 1:

- A Hostinger VPS provisioned and hardened
- A Tailscale tailnet with at least the VPS joined; optionally on-prem source hosts
- A BigQuery project with a write service account, a **budget alert**, and the BigQuery API enabled
- dlt installed in a venv on the VPS, reachable only via Tailscale
- At least one dlt pipeline live and **reconciled**: source → `raw_<source>` dataset, with state in BigQuery's `_dlt_*` tables
- A client GitHub repo committed with `.agentic-data-engineer.json` marker, the dlt pipeline + reconcile scripts, a per-client `CLAUDE.md`, and a README

Estimated wall-clock time: **1.5-3 hours** for a first deployment, most of which is waiting for VPS provisioning. Active human attention needed: ~30 minutes (OAuth prompts and credential capture). Dropping Airbyte's Kubernetes-in-Docker install removes the slowest, heaviest step of the old playbook.

---

## Step 0 — Discover, adapt, then gather build inputs

Do **not** start provisioning. Phase 1 begins by discovering what the user already has and adapting the plan to it (principle 8 — recommend strongly, impose nothing). Only after the plan is adapted do you gather the concrete build inputs.

### 0a — Run the discovery questions

Run the discovery-and-adaptation step first: [`../../../shared-references/discovery-and-adaptation.md`](../../../shared-references/discovery-and-adaptation.md). Ask what infrastructure already exists (VPS? warehouse? VPN / on-prem reachability? cloud preference? GitHub org? budget? team size?), present each major component as `Default: X (because…) · Alternatives · When to deviate`, and let the user's existing setup win over the defaults.

The adapted plan determines, for each of the steps below, whether it is **provisioned fresh**, **validated-and-reused**, or **skipped**:

| If the user already has… | Effect on the steps below |
|---|---|
| An existing VPS (any provider) | **Step 1 validates-and-reuses** it (SSH, specs, Docker) instead of provisioning Hostinger. Record `"vps": "reused_existing"`. |
| An existing VPN / WireGuard / IPsec | **Step 2 is skipped or trimmed** — document the existing reachability instead of joining Tailscale. Record `"network_layer"`. |
| No on-prem sources | The on-prem parts of **Step 2** are skipped entirely. |
| An existing warehouse (Snowflake/Postgres) | **Step 3 targets it**, not BigQuery. Flag the BigQuery-shaped deltas honestly (see the discovery reference). |
| An existing GitHub org | **Step 6** creates the repo under it; skip org setup. |

When you adapt onto a thinner alternative path, tell the user plainly (the discovery reference's coverage table is the source of truth for what is well-trodden vs adapted).

### 0b — Gather the build inputs

Once the plan is adapted, ask the user the questions in this table for the components that are still being provisioned fresh. Confirm answers explicitly. All answers, plus the discovery decisions, go into the marker file at the end.

| Question | Used for | Example answer |
|---|---|---|
| Company name (short, kebab-case) | VPS hostname, GCP project ID, repo name | `acme-bakery` |
| Country / billing region | VPS region, BigQuery dataset location | Spain → VPS in Frankfurt, BQ in EU |
| Primary data sources (this Phase 1 wires the first one) | Decides which connector to set up first | `Factorial HR`, `Shopify`, `SQL Server on-prem` |
| Does the user have a Hostinger account already? | Skip signup if yes (and skip entirely if reusing a VPS) | Yes / No |
| Does the user have a Tailscale account already? | Skip signup if yes (and skip entirely if reusing a VPN) | Yes / No |
| Does the user have a Google Cloud account? | Skip signup if yes (and skip if targeting another warehouse) | Yes / No |
| Does the user have a GitHub account? | Required, fail early if not | Yes |
| Client repo name | Where the deployment state lives | `<user>/acme-mds` |

If the user lacks an account needed by a fresh-provision step, **stop and ask them to create it** — these are the AI-Native exception ceremonies (principle 1). Provide the signup URLs (skip any whose component the user already has):

- https://hostinger.com/vps — needed for the VPS (skip if reusing a VPS)
- https://login.tailscale.com/start — needed for the tailnet (skip if reusing a VPN)
- https://console.cloud.google.com/ — needed for BigQuery (skip if targeting another warehouse)
- https://github.com/signup — needed for the client repo

Wait for confirmation. Then continue.

---

## Step 1 — Provision the VPS

Detailed instructions: [`vps-hostinger-bootstrap.md`](vps-hostinger-bootstrap.md).

Summary of what happens:

1. User obtains a Hostinger API token (manual, ~3 min — OAuth ceremony).
2. The agent provisions a KVM 2 VPS in the chosen region via Hostinger API.
3. The agent generates an SSH keypair locally, uploads the public half during provisioning.
4. The agent SSHes into the new VPS (over public IP for this one step), runs the hardening script:
   - Update packages, install `unattended-upgrades`
   - Create a `deploy` user with sudo, disable root SSH password login
   - Open only port 22 (closes everything else; the rest goes through Tailscale)
5. Capture VPS public IP and root password into a local `.secrets/` file (never committed).

Verification: `ssh deploy@<vps-ip>` from the user's laptop succeeds.

---

## Step 2 — Join Tailscale

Detailed instructions: [`tailscale-onprem.md`](tailscale-onprem.md).

Summary:

1. User obtains a Tailscale OAuth client or generates an auth key (manual, ~2 min).
2. Install Tailscale on the VPS, join the tailnet, set a stable hostname (e.g. `acme-mds-vps`).
3. **Lock the VPS firewall**: now that Tailscale works, close port 22 to public internet. Only Tailscale IPs allowed.
4. Verify Claude can `ssh deploy@acme-mds-vps` from the user's laptop (the laptop must also be on the tailnet — install Tailscale there if not yet).

(Optional, if the user has an on-prem source) Install Tailscale on the on-prem Windows / Linux server hosting the source database. Verify ping from VPS to on-prem host via the tailnet.

Verification: `ssh deploy@<vps-tailscale-hostname>` works; the VPS has no open public ports except what Tailscale needs.

---

## Step 3 — Set up BigQuery

Detailed instructions: [`bigquery-project-setup.md`](bigquery-project-setup.md).

Summary:

1. User runs `gcloud auth login` once on their laptop (OAuth ceremony, ~1 min).
2. The agent creates a new GCP project named `<client>-mds-prod` (or whatever the user prefers).
3. Enable billing on the project (requires manual click if it's the user's first project — OAuth ceremony).
4. Enable the BigQuery API.
5. **Create a budget alert** (`gcloud billing budgets create`) — a monthly amount with threshold alerts so a runaway query bill is caught early. BigQuery's bytes-scanned model means one bad query can cost real money; the alert is the safety net.
6. Create a **write** service account `dlt-writer@<project>.iam.gserviceaccount.com` with roles:
   - `roles/bigquery.dataEditor` (write to raw datasets, including dlt's `_dlt_*` state tables)
   - `roles/bigquery.jobUser` (run load/query jobs)
   - (A separate **read-only** SA for the MCP server is created in Phase 3 — see the read/write split note in [`bigquery-project-setup.md`](bigquery-project-setup.md).)
7. Generate and download the service account JSON key.
8. **Securely transfer the JSON to the VPS** via SSH (never to the public repo) — dlt reads it via `GOOGLE_APPLICATION_CREDENTIALS`.

Verification: `bq ls --project_id=<client>-mds-prod` works from the user's laptop; `gcloud billing budgets list --billing-account=<id>` shows the new budget.

---

## Step 4 — Install dlt on the VPS

Detailed instructions: [`dlt-on-vps-install.md`](dlt-on-vps-install.md).

Summary:

1. On the VPS (over Tailscale SSH), confirm Python 3.10+ and `python3-venv`.
2. Create a venv at `/home/deploy/dlt-env/`.
3. `pip install "dlt[bigquery]"` (pin a known-good minor; verify the range against dlt's docs at execution time).
4. Verify `dlt --version` and `python -c "import dlt"`.
5. Lay out the pipeline dir `/home/deploy/dlt/<client>-mds/` with a `.dlt/` config dir. Wire credentials via the on-box service-account key (`GOOGLE_APPLICATION_CREDENTIALS`) — reuses the credential from Step 3 and keeps the private key out of TOML.

Verification: `dlt --version` reports a version; `.dlt/` exists in the pipeline dir; the SA key is readable by `deploy`.

No Docker, no Kubernetes, no control plane — dlt is a library. This step is minutes, not the ~15 min Airbyte's Kind cluster took.

---

## Step 5 — Write the first dlt pipeline and reconcile

Detailed instructions: [`../../add-source/SKILL.md`](../../add-source/SKILL.md). Phase 1 invokes the `add-source` skill internally with the first source the user named in Step 0; that skill owns the dlt source detail (REST API config, paginators, incremental, on-prem `sql_database`). Do not duplicate it here.

Summary:

1. Pick the lane for the first source (per `add-source`): a **dlt pipeline** for a SaaS API or cloud DB (default), a **BigQuery native transfer** for a Google service (GA4 / Google Ads), or a **dlt `sql_database`** source for an on-prem DB over the tailnet.
2. Get user credentials for the source (OAuth ceremony for SaaS; read-only DB creds for on-prem). Put them in `.dlt/secrets.toml` or env vars — **never committed**.
3. Write `load.py` from the `add-source` template: `dlt.pipeline(destination="bigquery", dataset_name="raw_<source>").run(source)`. Start with `write_disposition="replace"` (safe), tune to `merge`/incremental once a cursor is chosen.
4. Run it: `python load.py`. Read `load_info`; fix the stack trace; re-run until rows land in `raw_<source>`. dlt creates the dataset and its `_dlt_loads` / `_dlt_pipeline_state` tables automatically.
5. **Reconcile (mandatory, not optional).** dlt's failure mode is *silent data gaps*, not crashes — a mis-set cursor or wrong paginator drops rows without erroring. Run the reconciliation check (`reconcile.py` from the `add-source` template): source row count vs `SELECT COUNT(*)` in `raw_<source>`, freshness, and sequence-gap. **The source is not done until reconciliation passes.**
6. Confirm the load succeeded in state: `bq query "SELECT * FROM <project>.raw_<source>._dlt_loads ORDER BY inserted_at DESC LIMIT 5"` shows status `succeeded`.

> **Why reconciliation is non-negotiable.** Without it, dlt is *more* dangerous than Airbyte: a pipeline that "ran successfully" can be quietly dropping half the rows, and nobody notices until a dashboard is wrong. The reconcile check is what makes dlt safe. It runs again on every scheduled run in Phase 2's linear pipeline (see [`orchestration-systemd.md`](orchestration-systemd.md)).

---

## Step 6 — Create the client repo

1. On the user's laptop, create a new GitHub repo (private by default) named as the user chose in Step 0.
2. Initialize it locally with:
   - `README.md` — auto-generated brief describing the deployment
   - `.agentic-data-engineer.json` — the marker file with Phase 1 state
   - `CLAUDE.md` — written from [`../templates/client-CLAUDE.md.template`](../templates/client-CLAUDE.md.template), with the `<...>` placeholders filled in for this deployment. This re-activates the data-engineer posture in any future Claude Code session opened in this folder (strong posture, not a cage — see the template).
   - `pipeline/` — the dlt `load.py`, `reconcile.py`, and `.dlt/config.toml` (NOT `.dlt/secrets.toml`)
   - `infra/` — VPS hostname, Tailscale notes, BigQuery project ID (no secrets)
   - `.gitignore` — exclude `secrets/`, `*.json` credentials, **`.dlt/secrets.toml`**, etc.
3. Push to GitHub.

> The dlt pipeline scripts are the reproducible heart of the raw layer (principle 4). Together with the warehouse-resident `_dlt_*` state, a fresh VPS checked out from this repo reloads from the last cursor with no gap — the cattle-not-pet property. Secrets stay out of Git: the SA key lives in `/home/deploy/secrets/` on the box and the agent's secrets folder on the laptop.

> **Why a per-client `CLAUDE.md`?** When the skillpack is installed as a Claude Code plugin, its skills are available globally and picked by description — but nothing pins a session to *this* client. The per-client `CLAUDE.md` lives in the client repo and loads automatically when a session opens there, so the next time you (or anyone) work in this folder, Claude resumes as this deployment's data engineer with the marker state in hand. The skillpack provides the knowledge; this file provides the local, persistent role.

The marker file looks like this after Phase 1:

```jsonc
{
  "skill_version": "0.7.0",
  "created_at": "<today>",
  "stack": {
    "sources": ["<first_source>"],
    "ingestion": "dlt",
    "warehouse": "bigquery",
    "transform": null,
    "orchestration": null,
    "mcp": false
  },
  "decisions": {
    "ingestion": "dlt",
    "warehouse": "bigquery",
    "network_layer": "tailscale",
    "vps": "provisioned_fresh",
    "vps_provider": "hostinger",
    "vps_hostname": "<acme>-mds-vps",
    "vps_region": "<region>",
    "tailnet": "<user-tailnet>",
    "bq_project_id": "<acme>-mds-prod",
    "bq_location": "EU",
    "bq_budget_alert": true,
    "bq_write_sa": "dlt-writer@<acme>-mds-prod.iam.gserviceaccount.com",
    "dlt_venv_path": "/home/deploy/dlt-env",
    "dlt_pipeline_dir": "/home/deploy/dlt/<acme>-mds",
    "github_repo": "<user>/<acme>-mds"
  },
  "history": [
    {"date": "<today>", "skill": "create-mds", "phase": 1, "outcome": "ok"}
  ]
}
```

---

## Step 7 — Hand off

Report to the user:

- VPS hostname (tailnet name)
- GCP project ID + that a budget alert is in place
- GitHub client repo URL
- First dlt load timestamp + row counts, and that reconciliation passed
- "Run `verify-pipeline` after the next scheduled run to confirm continuity (scheduling lands in Phase 2 — until then the load is manual)."
- "When you want Phase 2 (dbt + the systemd-timed pipeline), invoke me with 'set up dbt for this MDS'. When you want Phase 3 (MCP — opt-in but recommended), invoke me with 'set up the MCP server'."

Phase 1 is complete.

---

## Idempotence guarantees

If Phase 1 is interrupted (network failure, user cancels mid-way), re-invoking `create-mds` checks the marker:

- **No marker** → fresh start. Begins at Step 0.
- **Marker exists with Phase 1 partial** → resumes from the first unfinished step. Each step has its own preflight check (does the VPS exist? is Tailscale joined? is BQ project created? is dlt installed? did the first pipeline load + reconcile?).
- **Marker exists with Phase 1 complete** → refuses to run. User should invoke `add-source` or another evolution skill.

Each sub-reference (`vps-hostinger-bootstrap.md`, etc.) defines its own preflight.
