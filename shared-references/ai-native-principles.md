# AI-Native Principles

These are the eight design principles that hold the `agentic-data-engineer` skill set together. Every skill in this repo must honor them. When the principles conflict with a tool choice, the tool loses — we change the tool, not the principle.

The stack is **opinionated by default but adaptive in execution — recommend strongly, impose nothing.** The default stack below is a strong recommendation the agent defends; how it gets deployed always bends to what the user already has (principle 8).

Read this file before writing or modifying any skill.

---

## 1. 100% headless from Claude Code

**Every lifecycle operation of the Modern Data Stack must be executable from a Claude Code session — without leaving the terminal.**

This includes: provisioning a VPS, installing dlt, creating a BigQuery project, configuring service accounts, registering Tailscale nodes, configuring a systemd timer (cron as an alternative), deploying dbt, exposing an MCP server, and inspecting any of it later for troubleshooting.

The only allowed exceptions are **authentication ceremonies that require a human** — OAuth consent screens, MFA prompts, VPS provider signup, payment confirmation. When such a ceremony is unavoidable, the skill must:

1. Generate the exact URL the user clicks
2. Tell the user precisely what to confirm and what to copy back
3. Resume execution once the secret is returned

**Implication for skill design**: every `references/*.md` file must include the exact terminal commands or API calls Claude will execute. "Click the X button in the dashboard" is not acceptable except inside a documented OAuth ceremony.

If an operation cannot be made headless without losing essential functionality, we replace the component.

---

## 2. Tailscale as first-class network layer

**Tailscale is not an "access option" — it is the network backbone of the stack.** Every machine that runs a component of the MDS is part of a Tailscale tailnet: the VPS, the Claude Code laptop, and any on-prem servers hosting source databases.

This decision resolves three concerns at once:

| Concern | How Tailscale resolves it |
|---|---|
| Security | The VPS exposes zero public ports. The dlt/dbt linear script, its logs, and the MCP server are reachable only inside the tailnet. |
| On-prem connectivity | Legacy ERPs and SQL Server installations behind a NAT become reachable from the VPS as if they were on the same LAN — no firewall changes, no traditional VPN. |
| Multi-host uniformity | Claude reaches the VPS, the laptop, and the on-prem server with the same `ssh user@machine-name` — no jump hosts, no IP hardcoding. |

Public ingress (when needed — e.g. the MCP server exposed to claude.ai) is added via a reverse proxy with TLS (Traefik, Caddy), explicitly and per-service, on top of the tailnet baseline.

---

## 3. Freemium-first, opinionated stack

**Every component in the default stack must have a real free tier or be open-source self-hostable.** The goal is a working MDS for under **$10/month** for a starter PYME.

| Component | Tier used | Cost |
|---|---|---|
| BigQuery | 10 GB storage + 1 TB queries/month free | $0 for most PYMEs |
| dlt | Python library on the VPS | $0 |
| dbt-core | Open source | $0 |
| Tailscale | Free plan (3 users, 100 devices) | $0 |
| Hostinger VPS (KVM 2) | Entry tier | ~$5-8/mo |
| GitHub | Free for public repos / unlimited private | $0 |
| **Total** | | **~$5-10/mo lean** |

This is the headline number. It is the differentiator against Fivetran ($500+/mo), dbt Cloud ($100+/mo), Snowflake ($800+/mo minimum spend).

**Implication**: when a skill proposes a tool not in this list, the burden of proof is on the tool. Justify the cost, justify the lock-in.

---

## 4. GitHub-native ops

**Every reproducible piece of the system lives in a GitHub repo owned by the client.** This includes:

- dlt pipeline configs and the linear pipeline script (Airbyte connection configs exported as YAML — alternative)
- dbt project (models, tests, profiles.yml.example)
- MCP server skills and context `.md` files
- The systemd timer/service units (or a `crontab.txt` — alternative)
- The `.agentic-data-engineer.json` marker (see principle 5)
- Runbooks and architectural decision records (ADRs)

**UI-only state is forbidden.** If a critical setting only exists "in the Airbyte UI" or "in the BigQuery console", it must be exported and committed, or the skill must regenerate it deterministically from committed configs.

This gives the client: version history, PRs for changes, CI for dbt tests, multi-developer collaboration, and disaster recovery. It also gives Claude a stable source of truth to reason about — `git log` and a config file beat scraping a dashboard every time.

---

## 5. Marker-driven idempotence

**Every skill operation must be safely re-runnable.** Re-invoking a skill never duplicates work, never overwrites uncommitted changes, never leaves the system in an inconsistent state.

This is enforced by a `.agentic-data-engineer.json` file at the root of every client repo. It records:

```jsonc
{
  "skill_version": "0.7.0",
  "created_at": "2026-05-28",
  "stack": {
    "sources": ["sql_server_onprem_tailscale", "factorial_hr"],
    "ingestion": "dlt",
    "warehouse": "bigquery",
    "transform": "dbt_vps",
    "orchestration": "systemd_timer",
    "mcp": true
  },
  "decisions": {
    "vps_provider": "hostinger",
    "vps_hostname": "client-mds-1",
    "bq_project_id": "client-mds-prod",
    "tailscale_used": true,
    "github_repo": "user/client-mds"
  },
  "history": [
    {"date": "2026-05-28", "skill": "create-mds", "outcome": "ok"},
    {"date": "2026-05-30", "skill": "add-source", "source": "factorial_hr", "outcome": "ok"}
  ]
}
```

Before doing anything, every skill reads this file:

- **Marker missing** → skill is in "create" mode (only `create-mds` should proceed)
- **Marker present, operation already recorded** → skill is in "verify" mode (re-run is a no-op + confirmation)
- **Marker present, operation new** → skill is in "evolve" mode (extend the existing system)

The marker is committed to the client repo. It is human-readable and Claude can update it surgically when an operation completes.

---

## 6. Observable from agent

**Every component of the stack must expose its state via API or terminal — never UI-only.** Claude can troubleshoot the pipeline without a human looking at any screen.

| Component | Observability surface |
|---|---|
| dlt | Post-load reconciliation (row-count source-vs-destination, freshness, gap checks) + the `_dlt_*` state tables in the warehouse + the linear script's stdout/stderr |
| Linear pipeline script | `journalctl -u <unit>` and `systemctl status` for the systemd timer; per-run log files over SSH |
| BigQuery | `INFORMATION_SCHEMA.JOBS_BY_PROJECT`, `__TABLES__` (freshness), `bq` CLI |
| dbt | Run artifacts (`target/run_results.json`, `target/manifest.json`) and log files (`logs/dbt.log`) over SSH |
| MCP server | Container logs over SSH (`docker logs` or `journalctl`) plus an HTTP health endpoint |
| Tailscale | `tailscale status` over SSH on any node |
| Airbyte (alternative) | REST API under `/api/public/v1/` — `GET /jobs`, `GET /connections`, `POST /jobs` to trigger |

**Tools that hide state behind a closed dashboard fail this principle.** This is why Fivetran is out (job state opaque without a UI session) and dlt is in — its state lives in the warehouse (`_dlt_*`) and every load is reconciled, so the agent sees completeness without a screen. (Airbyte OSS, the documented alternative, passes too via its full REST API.)

---

## 7. Escape hatches always open

**The stack is opinionated, not coercive.** Every component must be replaceable by the client without rewriting the rest of the system.

| Component | Why portable |
|---|---|
| dbt models | Plain SQL — portable to any modern warehouse |
| Airbyte configs | YAML, exportable — restorable to Fivetran or to a re-installed Airbyte |
| BigQuery datasets | Standard SQL, plus `bq extract` to GCS, plus federated queries from other warehouses |
| MCP server | Speaks the open Model Context Protocol — any compliant client (claude.ai, Claude Code, Cursor, custom) works |
| Tailscale | Optional — could be replaced by WireGuard or a traditional VPN if the client outgrows Tailscale's free tier |

This is a **marketing promise as much as a design constraint**. The pitch is the opposite of Fivetran/Snowflake/dbt Cloud: opinionated where it saves you time, open where you might want to leave.

If you ever find a component that locks the client in, propose its replacement before shipping.

---

## 8. Recommend strongly, impose nothing

**The agent discovers what the user already has BEFORE provisioning anything.** A senior data engineer has strong opinions ("I'd use BigQuery") but asks what you already run first, and adapts if you already run Snowflake. They recommend hard; they impose nothing.

Every build or expand skill runs a **discovery-and-adaptation step first** (see [`discovery-and-adaptation.md`](discovery-and-adaptation.md)). The user's existing infrastructure wins over the defaults:

| The user already has… | The default loses to it |
|---|---|
| An existing VPS (any provider) | Validate and reuse it — don't provision a Hostinger box. |
| An existing warehouse (Snowflake, Postgres) | Target it — don't impose BigQuery. |
| A cloud preference (AWS, Azure) | Honor it where the playbooks can. |
| An existing VPN (WireGuard, IPsec) | Document its reachability and skip Tailscale. |

Every major choice is **surfaced as a decision with a default + alternatives**, never silently assumed. The agent presents `Default: X (because…) · Alternatives: Y, Z · When to deviate`, defends the recommendation with reasons — and then proceeds with the user's choice. The default is a recommendation, not a rail.

This is not a contradiction of principles 2 and 3 — the opinion stays. It is the discipline that the opinion is *offered and defended*, not *forced*. When the user picks a non-default, that is an [escape hatch](#7-escape-hatches-always-open) exercised early instead of late; record it in the marker (principle 5) so re-runs and other skills respect it.

**The playbooks are your starting knowledge, not your boundary.** The skillpack documents the well-trodden path; it cannot anticipate every client. When you hit terrain the references don't map — a niche source with no connector, a constraint the default stack doesn't handle, a place where the opinionated choice is plainly wrong here — **do not dead-end with *"the docs don't cover this."*** Name the gap, reason from these principles (is the alternative still headless? Tailscale-reachable? free-tier? observable? portable?), propose it, get a yes, and proceed with the engineer's judgment. Then, if it's reusable, write the learning back so the next deployment inherits it. Dead-ending on the documentation is itself a form of dogmatism — the exact thing this principle exists to prevent. The skillpack is the floor of what the engineer knows, never the ceiling.

---

## How to read these in practice

When writing a skill, ask yourself for each step:

1. **Can Claude execute this without the user leaving the terminal?** (principle 1)
2. **Does this assume Tailscale, or does it punch a hole in the network?** (principle 2)
3. **Does this require a paid tier when a free option exists?** (principle 3)
4. **Is the resulting state committed to Git, or only in a UI?** (principle 4)
5. **What happens if I re-run this skill tomorrow? Does the marker know?** (principle 5)
6. **How does Claude see whether this step succeeded — via API or by trusting?** (principle 6)
7. **If the client wants to replace this tool in two years, what hurts?** (principle 7)
8. **Did I ask what the user already has — and, when the docs fell short, did I improvise from the principles rather than dead-end?** (principle 8)

A skill that answers these cleanly is shippable. A skill that doesn't gets refined or rejected.
