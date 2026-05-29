# The remote-control model — how Claude drives the machines

How a Claude Code session actually operates the VPS and on-prem hosts it provisions. This is the single reference for "how does the agent run a command over there?" — other playbooks link here instead of re-explaining it.

## Mental model

**Claude drives remote machines over Tailscale SSH, one command per Bash tool call.**

Every machine in the stack is a node in the tailnet (principle 2 — see [`ai-native-principles.md`](ai-native-principles.md#2-tailscale-as-first-class-network-layer)). The Claude Code laptop is also a node. So from its Bash tool, Claude reaches any other node with a plain `ssh user@host "command"` — no jump host, no public IP, no VPN client to script.

The one rule that shapes everything below: **the Bash tool is stateless between calls.** Shell state — `cd`, exported variables, an activated venv — does NOT persist from one Bash call to the next. Each call that targets the VPS is a fresh `ssh deploy@<host> "..."` that opens a connection, runs one command, and closes. Treat every remote command as self-contained.

### Runtime data flow ≠ agent management

Keep these two paths separate in your head — they confuse people constantly:

| Path | Who acts | When | Over what |
|---|---|---|---|
| **Runtime data flow** | Airbyte (running on the VPS) | On schedule (cron) | Tailscale, VPS → on-prem DB port (1433/3306/5432) |
| **Agent management** | Claude (from its Bash tool) | On demand, when you ask | Tailscale SSH, laptop → VPS / on-prem host |

Claude is **not** in the runtime data path. When a sync runs at 03:00, Airbyte on the VPS reaches the on-prem SQL Server itself; Claude is asleep. Claude's job is to *manage* — install, configure, inspect, troubleshoot — not to ferry rows. Don't conflate "Airbyte can reach the DB" with "Claude can manage the box the DB runs on." Those are different connections, set up separately (see the on-prem section below).

---

## VPS access

### The three reachability paths

The VPS answers at three addresses. Prefer them in this order:

| # | Path | Example | When |
|---|---|---|---|
| 1 | **Tailscale hostname** | `ssh -i ~/.ssh/<client>_vps deploy@acme-mds` | **Default. Always start here.** Stable, survives IP changes. |
| 2 | **Tailscale IP** | `ssh -i ~/.ssh/<client>_vps deploy@100.88.139.128` | When MagicDNS name resolution is flaky but the tailnet is up. |
| 3 | **Public IP** | `ssh -i ~/.ssh/<client>_vps deploy@<public_ip>` | Emergency only — e.g. Tailscale daemon down on the VPS. After Phase 1 firewall lockdown ([`tailscale-onprem.md` Step D](../skills/create-mds/references/tailscale-onprem.md)) this path is **closed** to non-tailnet machines. |

The repo convention (set in Phase 1, see [`vps-hostinger-bootstrap.md`](../skills/create-mds/references/vps-hostinger-bootstrap.md)) is a **`deploy` user** with a per-client key at **`~/.ssh/<client>_vps`**. Throughout the playbooks, `<client>-mds` is the Tailscale hostname and `deploy@<client>-mds` is the canonical target.

> Note: the author's own live deployments happen to use `root` with `~/.ssh/deploy_hostinger`. Ignore that — the repo standard is the unprivileged `deploy` user with a per-client key. Use `sudo` for privileged steps.

### Default: the plain `ssh` CLI from the Bash tool

This is the model 95% of the time. One remote command per Bash call:

```bash
# Inspect Airbyte job state
ssh deploy@<client>-mds "abctl local status"

# Check disk before an install
ssh deploy@<client>-mds "df -h /"

# Tail a dbt log
ssh deploy@<client>-mds "tail -n 50 ~/dbt/logs/dbt.log"
```

Add `-i ~/.ssh/<client>_vps` if the key isn't your default identity, and `-o ConnectTimeout=5` to fail fast on a dead host instead of hanging the tool call.

### Tailscale SSH — the keyless path

In Phase 1 the VPS joined the tailnet with `tailscale up --ssh` ([`tailscale-onprem.md` Step B](../skills/create-mds/references/tailscale-onprem.md)). That turns on Tailscale's built-in SSH server. From any tailnet node — including the Claude Code laptop — you can then connect **without managing a keypair at all**:

```bash
# No -i flag, no key file. Tailscale proves identity via the tailnet.
ssh deploy@<client>-mds "tailscale status"
```

Tailscale handles auth and identity (and logs the session in the admin console). The traditional `~/.ssh/<client>_vps` keypair stays as a **fallback** for when the tailnet itself is the thing you're debugging. Prefer Tailscale SSH for day-to-day; keep the key for emergencies.

### Escape hatch: paramiko / Python

The `ssh` CLI is one-shot and stateless. When you genuinely need a **persistent connection**, an **interactive shell**, or **programmatic error handling** across many dependent steps, drop down to a Python script using `paramiko` (the Python SSH library). Real-world `sistema_integracio` notes describe this as "Claude Code can connect to the VPS via SSH (paramiko/Python)."

Use it sparingly, and only as a committed helper in a skill's `scripts/` folder — not ad hoc. Good fits: a migration that must hold one transaction open across steps, or a loop that branches on the exit code of each remote command.

```python
# scripts/example_remote.py — escape hatch, not the default
import paramiko

client = paramiko.SSHClient()
client.load_system_host_keys()
client.connect("acme-mds", username="deploy", key_filename="/home/user/.ssh/acme_vps")

for cmd in ["abctl local status", "docker ps --format '{{.Names}}'"]:
    _, stdout, stderr = client.exec_command(cmd)
    code = stdout.channel.recv_exit_status()
    print(cmd, "->", code, stdout.read().decode())
    if code != 0:
        raise SystemExit(f"{cmd} failed: {stderr.read().decode()}")

client.close()
```

**Default to the `ssh` CLI. Reach for paramiko only when the CLI's statelessness actually blocks you.**

---

## On-prem host control

The on-prem machine (typically a Windows server in the client's office running SQL Server) has two separate connections to the stack. Don't mix them up.

### The runtime data path (Airbyte, not Claude)

Set up in [`tailscale-onprem.md` Step E](../skills/create-mds/references/tailscale-onprem.md). The on-prem host joins the tailnet; the VPS reaches its database over the tailnet on the DB port:

```bash
# Run FROM the VPS — this is what Airbyte does at sync time
ssh deploy@<client>-mds "nc -zv <onprem-host> 1433"   # SQL Server
#                                                3306   # MySQL
#                                                5432   # Postgres
```

Once the on-prem DB accepts connections from the VPS's tailnet IP, Airbyte's generic database connector handles syncs on schedule. **Claude is not in this path.** It only verifies the path works at setup time.

### The management path (Claude → on-prem host)

Sometimes Claude needs to operate the on-prem box *itself* — most commonly to restart Tailscale when the host shows offline in the admin console. The real fix is, in **PowerShell as Administrator** on that machine:

```powershell
& "C:\Program Files\Tailscale\tailscale.exe" up
```

For Claude to run that **headlessly** (principle 1), the on-prem host needs an access path. Three options, best first:

| # | Option | How Claude reaches it | Reality |
|---|---|---|---|
| a | **Tailscale SSH on the on-prem host** | `ssh user@<onprem-tailnet-host> 'powershell -Command "..."'` | Cleanest. Keyless, like the VPS. Requires the host to run Tailscale with SSH enabled. |
| b | **OpenSSH Server on Windows** | Standard `ssh user@<onprem-host>` over the tailnet | Windows 10/11/Server can enable the OpenSSH Server optional feature; then it's a normal SSH target. |
| c | **Manual ceremony** | Claude prints the exact PowerShell line; the user runs it on the box | The AI-Native "human ceremony" fallback (allowed by principle 1). The goal is to *minimize* it. |

**Be honest about day 1:** most PYME on-prem Windows servers ship with no SSH at all, so **option (c) is the realistic starting state.** That's acceptable — but setting up (a) or (b) is a worthwhile one-time improvement that gets the on-prem host to near-headless, so future restarts and diagnostics don't need a human at the keyboard.

Enabling option (a) on the Windows host (one-time, PowerShell as Administrator):

```powershell
& "C:\Program Files\Tailscale\tailscale.exe" set --ssh
```

Enabling option (b) on the Windows host (one-time, PowerShell as Administrator):

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service -Name sshd -StartupType 'Automatic'
```

After either, Claude can drive the on-prem host the same way it drives the VPS:

```bash
# Restart Tailscale on the on-prem Windows host, headlessly
ssh user@acme-erp-server 'powershell -Command "& \"C:\Program Files\Tailscale\tailscale.exe\" up"'
```

---

## Quoting and multi-line commands over SSH

`ssh host "command"` passes the command string through **two** shells: your local shell quotes it, then the remote shell parses it. Nested quotes, `$variables`, and multi-line scripts are where this bites. Rules of thumb:

- **Single-quote the remote command** when you do NOT want your local shell to expand anything: `ssh deploy@host 'echo $HOME'` prints the *remote* `$HOME`.
- **Double-quote** when you DO want local expansion: `ssh deploy@host "echo $LOCAL_VAR"` substitutes the local variable before sending.
- **For anything multi-line, stop hand-quoting — pipe a heredoc to a remote shell.**

### Example 1 — remote variable, not local

```bash
# Wrong: local shell eats $(hostname) before ssh runs
ssh deploy@<client>-mds "echo running on $(hostname)"   # prints the LAPTOP hostname

# Right: single quotes defer to the remote shell
ssh deploy@<client>-mds 'echo running on $(hostname)'   # prints the VPS hostname
```

### Example 2 — multi-line script via heredoc (the safe default for anything non-trivial)

```bash
ssh deploy@<client>-mds 'bash -s' <<'EOF'
set -euo pipefail
cd ~/dbt
source ../.venv/bin/activate
dbt run --select staging
dbt test --select staging
EOF
```

The quoted `<<'EOF'` (note the quotes around `EOF`) means the local shell does **no** expansion — the script reaches the VPS verbatim, and `bash -s` executes it as one session, so `cd` and `source` actually carry across the lines. This is how you get multi-step state in a single Bash tool call despite the stateless model.

### Example 3 — passing one local value into a remote heredoc

```bash
# Unquoted EOF + escaped remote $ vars = local CLIENT expands, remote $f does not
CLIENT="acme"
ssh deploy@<client>-mds 'bash -s' <<EOF
for f in /opt/$CLIENT/*.log; do echo "found \$f"; done
EOF
```

Here `$CLIENT` expands locally (no quotes on `EOF`), while `\$f` is escaped so it stays a remote variable. When in doubt, prefer the quoted-heredoc form (Example 2) and inject values as positional args instead of inline interpolation.

---

## Common gotchas

- **Shell state doesn't persist between Bash calls.** A `cd ~/dbt` in one `ssh` call is gone by the next. Either chain with `&&` in one command, or use a heredoc (`bash -s`) to run the whole sequence in one connection.
- **`$(...)` and `$VAR` expand locally inside double quotes.** You meant the remote value. Single-quote, or escape with `\$`.
- **Hung tool call on a dead host.** Always pass `-o ConnectTimeout=5`; the public-IP path may be firewalled-off and will otherwise hang the full tool timeout.
- **First connection prompts for host-key acceptance** and stalls a non-interactive Bash call. For automation use `-o StrictHostKeyChecking=accept-new` (not `no` — that's insecure).
- **Windows command quoting is doubly nested.** `ssh ... 'powershell -Command "..."'` needs the inner `"` escaped as `\"` and the path `"C:\Program Files\..."` escaped again. Verify with a trivial `powershell -Command "echo ok"` before the real command.
- **Tailscale hostname doesn't resolve.** MagicDNS may be off or the node expired. Fall back to the tailnet IP (`tailscale ip -4` on the host), then re-check the admin console.
- **`Permission denied (publickey)`.** Either the wrong `-i ~/.ssh/<client>_vps`, or you're expecting Tailscale SSH but the VPS wasn't brought up with `--ssh`. Re-run `tailscale up --ssh` per [`tailscale-onprem.md`](../skills/create-mds/references/tailscale-onprem.md).

---

## Closing — why this model

This is principle 1 ([100% headless](ai-native-principles.md#1-100-headless-from-claude-code)) and principle 2 ([Tailscale-first](ai-native-principles.md#2-tailscale-as-first-class-network-layer)) made concrete. Headless means Claude operates every box from its Bash tool with no human at a keyboard — which is exactly why getting the on-prem host onto path (a) or (b) matters, and why path (c) is a fallback to minimize rather than a destination. Tailscale-first means the *same* `ssh user@host` reaches the VPS, the laptop, and the on-prem server with no IP hardcoding and no public ports. When a machine can't be driven this way, that's a gap to close, not a workflow to accept.
