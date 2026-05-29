# Discovery and adaptation — ask first, then build

The step every build or expand skill runs **before** it provisions anything. This is principle 8 ([recommend strongly, impose nothing](ai-native-principles.md#8-recommend-strongly-impose-nothing)) made operational.

The mental model: a senior data engineer has strong opinions ("I'd use BigQuery") but the first thing they do on a new engagement is ask what you already run, and adapt if you already run Snowflake. They recommend hard; they impose nothing. The default stack ([`stack-rationale.md`](stack-rationale.md)) is the recommendation. This file is where the user's reality overrides it.

---

## Why discovery first

Skipping discovery means imposing a rebuild on a user who already has half the stack. That is the failure mode of a dogmatic playbook: it provisions a fresh Hostinger VPS next to the perfectly good Hetzner box the user already pays for, or it stands up BigQuery for a shop that already runs Snowflake and just wants a pipeline into it.

Discovery costs five minutes of questions. Getting it wrong costs a teardown, a confused user, and a deployment that fights the infrastructure it lives in. Ask first.

The output of discovery is twofold:

1. An **adapted plan** — which later steps are *provisioned fresh* vs *validated-and-reused* vs *skipped entirely*.
2. A set of **recorded decisions** written to the marker (see below) so re-runs and other skills inherit them.

---

## The discovery questions

Ask these before gathering build inputs. Confirm answers explicitly. Default to the recommended stack only where the user has no existing piece and no preference.

| # | Question | Why it matters | Possible answers |
|---|---|---|---|
| 1 | Do you have a VPS / server already? | Reuse beats re-provision | Provider + specs (vCPU/RAM/disk) / none |
| 2 | Do you have a cloud account, and a preference? | Honor existing billing + IAM | GCP / AWS / Azure / none / no preference |
| 3 | Do you have a data warehouse already? | Don't impose BigQuery on a Snowflake shop | BigQuery / Snowflake / Postgres / Redshift / none |
| 4 | Is there a VPN or network path to on-prem sources? | Skip Tailscale if reachability exists | Tailscale / WireGuard / IPsec / nothing / no on-prem sources |
| 5 | On-prem source constraints? | Shapes the reachability + management plan | OS, firewall control (yes/no), who admins the box |
| 6 | Existing GitHub org / Git host? | Where deployment state lives | GitHub org / GitLab / Gitea / none |
| 7 | Budget ceiling? | Bounds the recommendation | $/month the user will tolerate |
| 8 | Team size? | Affects free-tier sizing + access model | solo / small team / N seats |

Questions 1–4 are the ones that most often flip the plan. Spend the time there.

---

## How each answer changes the plan

Map every answer to a concrete adaptation. "Validate-and-reuse" means run the existing component through the relevant step's preflight check instead of the provisioning path.

| Discovery answer | Adaptation |
|---|---|
| **Already has a VPS** (any provider) | Skip Hostinger provisioning. Validate the existing box: SSH access, specs adequate for Airbyte + dbt + MCP (~2 vCPU / 8 GB), Docker installable. Record `"vps": "reused_existing"`. |
| **No VPS** | Provision per the default ([`vps-hostinger-bootstrap.md`](../skills/create-mds/references/vps-hostinger-bootstrap.md)). This is the well-trodden path. |
| **Prefers / already has Snowflake** | Swap the warehouse adapter: dbt targets Snowflake (`dbt-snowflake`), the MCP read tool points at Snowflake, the Airbyte destination is Snowflake. **Flag the deltas honestly** — this repo's playbooks are BigQuery-shaped; Snowflake is supported at the adaptation level, not yet with a full parallel playbook. |
| **Prefers / already has Postgres** | dbt targets Postgres, MCP reads Postgres. Same honesty caveat as Snowflake. Note Postgres-on-VPS scaling limits (~50M rows on cheap hardware) if relevant. |
| **AWS / Azure preference** | Honor where playbooks allow (VPS can live on EC2/Lightsail/Azure VM; warehouse maps to Redshift/Synapse if the user already has it). BigQuery still recommended if there is no existing warehouse and no hard cloud constraint — defend it, then follow the user. |
| **Already uses WireGuard** | Skip Tailscale. Document the reachability assumptions the rest of the stack needs: the VPS must reach on-prem DB ports over the existing tunnel, and the Claude Code laptop needs a path to manage the VPS. Record `"network_layer": "existing_wireguard"`. |
| **Already uses IPsec / traditional VPN** | Same as WireGuard — skip Tailscale, document reachability, note that Tailscale SSH conveniences (keyless, MagicDNS) won't apply; management SSH falls back to keys. |
| **Tailscale already in use** | Reuse the tailnet. Join the new VPS to it; skip account setup. |
| **No on-prem sources** | Skip the on-prem / Tailscale-to-DB parts entirely (Step E in the network setup). SaaS-only deployments don't need any on-prem reachability story. |
| **Existing GitHub org** | Create the client repo under it; skip account/org setup. GitLab/Gitea → identical workflow, retarget the remote. |
| **Tight budget** | Lean harder on free tiers; flag any choice that crosses the ceiling before proceeding. |

When an adaptation lands the user off the default stack, **say so plainly** (see "Honesty about current coverage").

---

## The default recommendation framing

For every decision point, present it the same way. The default is a recommendation the agent **defends with reasons**, not a rail it enforces:

```
Default: X (because …)
Alternatives: Y, Z
When to deviate: <the conditions under which Y or Z is the better call>
```

Worked example — the warehouse decision:

```
Default: BigQuery (real free tier carries most PYMEs forever; no idle-warehouse
         cost; native GA4 / Google Ads ingestion saves connector slots)
Alternatives: Snowflake, Postgres-on-VPS, DuckDB
When to deviate: you already run Snowflake/Postgres (target it, don't migrate);
         revenue justifies Snowflake's $800+/mo minimum; you want zero cloud
         dependency (DuckDB).
```

The agent states the default, gives the reasons, and asks. If the user picks an alternative, the agent proceeds with it — no relitigating. The opinion is in the recommendation, not in a refusal to adapt.

The full set of defaults-with-reasons lives in [`stack-rationale.md`](stack-rationale.md); this step is where those reasons get *offered to the user* rather than silently assumed.

---

## Recording decisions in the marker

Discovery answers and the chosen (possibly non-default) components go into `.agentic-data-engineer.json` under `decisions`, so re-runs and other skills respect them (principle 5 — marker-driven idempotence). A deployment that adapted away from the defaults looks like this:

```jsonc
{
  "skill_version": "0.5.0",
  "created_at": "2026-05-29",
  "stack": {
    "sources": ["sql_server_onprem"],
    "warehouse": "snowflake",
    "transform": "dbt_vps",
    "orchestration": "cron",
    "mcp": true
  },
  "decisions": {
    "warehouse": "snowflake",          // adapted away from BigQuery default
    "network_layer": "existing_wireguard", // user already had WireGuard; Tailscale skipped
    "vps": "reused_existing",          // validated the user's Hetzner box, did not provision
    "vps_provider": "hetzner",
    "vps_hostname": "client-data-1",
    "cloud_preference": "aws",
    "github_org": "client-co",
    "discovery_date": "2026-05-29"
  },
  "history": [
    {"date": "2026-05-29", "skill": "create-mds", "phase": 1, "outcome": "ok", "note": "non-default: snowflake + wireguard + reused vps"}
  ]
}
```

On re-run, the agent reads these `decisions` and does **not** re-ask the discovery questions or try to re-impose the defaults. Other skills (`add-source`, `add-dbt-model`, the MCP skills) read the same fields and target Snowflake/WireGuard/the existing VPS automatically.

---

## Honesty about current coverage

Be transparent with the user about where the deployment is on a well-trodden path vs being adapted.

The v0.x playbooks are **deepest for the default stack**: BigQuery, Airbyte OSS, Tailscale, Hostinger, dbt-core, cron, FastMCP. Those paths have full, tested references.

Alternative paths are supported at the **decision-and-adaptation level** and via the [escape hatches](ai-native-principles.md#7-escape-hatches-always-open) (dbt is plain SQL, Airbyte configs are portable YAML, MCP is an open protocol). But some alternative playbooks are **thinner today**:

| Path | Coverage today |
|---|---|
| BigQuery / Airbyte / Tailscale / Hostinger | Full playbooks. Well-trodden. |
| Reuse an existing VPS (any provider) | Solid — validation is provider-agnostic; only provisioning is Hostinger-specific. |
| Snowflake / Postgres warehouse | Adaptation-level. dbt and MCP retarget cleanly; expect to fill gaps the BigQuery references assume. |
| WireGuard / IPsec instead of Tailscale | Adaptation-level. Reachability is yours to document; the playbooks assume Tailscale conveniences. |
| AWS / Azure VPS or warehouse | Adaptation-level. No dedicated playbook yet. |

Do not fabricate parity that does not exist. When adapting onto a thin path, tell the user: "This is supported, but the BigQuery path is the tested one — I'll flag where I'm extrapolating." That honesty is part of the recommendation discipline, not a weakness in it.

---

## When the playbook doesn't cover it

Discovery adapts to *known* alternatives (the table above). But sometimes you hit something neither the default nor any documented alternative covers — a niche source with no Airbyte connector, an on-prem system with an odd protocol, a client constraint the stack wasn't designed for.

The rule is principle 8's: **don't dead-end.** Concretely:

1. **Name the gap plainly** to the user: "The skillpack doesn't cover X. Here's what I'd do instead."
2. **Reason from the principles, not the playbook.** Is the alternative headless? Tailscale-reachable? Free-tier-friendly? Observable from the agent? Portable? A solution that honors the [eight principles](ai-native-principles.md) is in-spirit even when it's not in the docs.
3. **Propose, get a yes, implement.** It is fine to read external docs, try an approach, and iterate. The engineer researches what it doesn't know.
4. **Record and write back.** Put the choice in the marker (`decisions`), and if it's reusable, add a reference or note to the skillpack so the next client inherits the scar tissue.

The skillpack is the floor of what the engineer knows, not the ceiling.

---

## Common gotchas

- **Skipping discovery on a re-run.** If the marker already has `decisions`, do NOT re-ask — read them. Discovery is a first-deployment step; re-runs inherit the recorded choices.
- **Assuming "reuse" means "no checks."** A reused VPS still gets a preflight: specs, SSH access, Docker. "Validate-and-reuse" is not "trust blindly."
- **Treating a cloud preference as a warehouse choice.** "We use AWS" does not mean "we have Redshift." Ask question 3 separately from question 2.
- **Letting an existing VPN go undocumented.** If you skip Tailscale for the user's WireGuard, you inherit responsibility for stating exactly which reachability the rest of the stack assumes (VPS → on-prem DB port; laptop → VPS management). Write it down or the next skill will be blind.
- **Over-defending the default.** Defend the recommendation once, with reasons. If the user still wants the alternative, proceed. Relitigating is imposing, which violates principle 8.
- **Silent adaptation.** Never quietly swap the stack and move on. Every deviation from the default is announced, recorded in the marker, and flagged for coverage honesty.
- **No-preference is not no-answer.** If the user genuinely has nothing and no preference, *that* is when you confidently deploy the full default stack — that's the path it's built for.
