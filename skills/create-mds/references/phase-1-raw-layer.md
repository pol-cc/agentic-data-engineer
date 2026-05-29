# Phase 1 — Raw Layer Playbook

This is the orchestrator for Phase 1 of `create-mds`. It produces a working MDS up to the **raw layer**: data from at least one source landing in BigQuery as `raw_<source>` datasets, synced daily. No dbt, no MCP yet — those are Phase 2 and 3.

End state of Phase 1:

- A Hostinger VPS provisioned and hardened
- A Tailscale tailnet with at least the VPS joined; optionally on-prem source hosts
- Airbyte OSS installed on the VPS, reachable only via Tailscale, API accessible
- A BigQuery project with a service account configured
- At least one Airbyte connection live: source → `raw_<source>` dataset
- A client GitHub repo committed with `.agentic-data-engineer.json` marker, config exports, and a README

Estimated wall-clock time: **2-4 hours** for a first deployment, most of which is waiting for VPS provisioning and Airbyte initial install. Active human attention needed: ~30 minutes (OAuth prompts and credential capture).

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
5. Create a service account `airbyte-writer@<project>.iam.gserviceaccount.com` with roles:
   - `roles/bigquery.dataEditor` (write to raw datasets)
   - `roles/bigquery.jobUser` (run queries for sync overhead)
6. Generate and download the service account JSON key.
7. **Securely transfer the JSON to the VPS** via SSH (never to the public repo).

Verification: `bq ls --project_id=<client>-mds-prod` works from the user's laptop.

---

## Step 4 — Install Airbyte OSS

Detailed instructions: [`airbyte-install.md`](airbyte-install.md).

Summary:

1. On the VPS (over Tailscale SSH), install Docker.
2. Install `abctl` (Airbyte's CLI installer).
3. Run `abctl local install` — this brings up Airbyte as Kind-based Kubernetes-in-Docker. Takes ~10-15 min.
4. Set the admin credentials with `abctl local credentials`.
5. Enable the OAuth2 API by creating an application; capture `client_id` and `client_secret`.
6. Smoke-test the API: `curl /api/public/v1/applications/token` returns a JWT.

Verification: `abctl local status` shows all components running. The API responds at `http://localhost:8000/api/public/v1/` from inside the VPS (port not exposed publicly — Tailscale + SSH tunnel for local browser if needed).

---

## Step 5 — Wire the first source

Detailed instructions: [`../../add-source/SKILL.md`](../../add-source/SKILL.md). Phase 1 invokes the `add-source` skill internally with the first source the user named in Step 0.

Summary:

1. Look up the source in Airbyte's connector catalog.
2. Get user credentials for the source (OAuth ceremony for SaaS sources; database creds for on-prem).
3. Configure Airbyte source via API.
4. Configure BigQuery destination via API, pointing at `raw_<source>` dataset in the new GCP project (the dataset will be created on first sync).
5. Configure the connection with sync mode `Full Refresh Overwrite` (start safe; can tune later) and schedule daily at a time chosen by the user (default 07:30 UTC).
6. Trigger an initial sync. Watch it complete.
7. Verify the data landed: `bq query "SELECT COUNT(*) FROM <project>.raw_<source>.<a_table>"`.

---

## Step 6 — Create the client repo

1. On the user's laptop, create a new GitHub repo (private by default) named as the user chose in Step 0.
2. Initialize it locally with:
   - `README.md` — auto-generated brief describing the deployment
   - `.agentic-data-engineer.json` — the marker file with Phase 1 state
   - `airbyte-configs/` — exported connection config(s) as YAML
   - `infra/` — VPS hostname, Tailscale notes, BigQuery project ID (no secrets)
   - `.gitignore` — exclude `secrets/`, `*.json` credentials, etc.
3. Push to GitHub.

The marker file looks like this after Phase 1:

```jsonc
{
  "skill_version": "0.1.0",
  "created_at": "<today>",
  "stack": {
    "sources": ["<first_source>"],
    "warehouse": "bigquery",
    "transform": null,
    "orchestration": null,
    "mcp": false
  },
  "decisions": {
    "warehouse": "bigquery",
    "network_layer": "tailscale",
    "vps": "provisioned_fresh",
    "vps_provider": "hostinger",
    "vps_hostname": "<acme>-mds-vps",
    "vps_region": "<region>",
    "tailnet": "<user-tailnet>",
    "bq_project_id": "<acme>-mds-prod",
    "bq_location": "EU",
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
- GCP project ID
- GitHub client repo URL
- First sync timestamp + row counts
- "Run `verify-pipeline` tomorrow morning after the scheduled sync to confirm continuity."
- "When you want Phase 2 (dbt), invoke me with 'set up dbt for this MDS'. When you want Phase 3 (MCP), invoke me with 'set up the MCP server'."

Phase 1 is complete.

---

## Idempotence guarantees

If Phase 1 is interrupted (network failure, user cancels mid-way), re-invoking `create-mds` checks the marker:

- **No marker** → fresh start. Begins at Step 0.
- **Marker exists with Phase 1 partial** → resumes from the first unfinished step. Each step has its own preflight check (does the VPS exist? is Tailscale joined? is BQ project created? is Airbyte installed?).
- **Marker exists with Phase 1 complete** → refuses to run. User should invoke `add-source` or another evolution skill.

Each sub-reference (`vps-hostinger-bootstrap.md`, etc.) defines its own preflight.
