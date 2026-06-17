# Tailscale — VPS and on-prem joining

End state: the VPS reachable via a stable Tailscale hostname, all public ports closed except for Tailscale itself. Optionally, on-prem source databases reachable from the VPS without exposing them to the internet.

## Why Tailscale (recap)

See [`shared-references/ai-native-principles.md`](../../../shared-references/ai-native-principles.md#2-tailscale-as-first-class-network-layer) — principle 2.

## Preflight

- The VPS exists and is reachable via `ssh deploy@<public_ip>` (previous step).
- The user has a Tailscale account (signup at https://login.tailscale.com/start if not).

## Step A — Generate an auth key (user ceremony)

Tailscale needs a one-time key to register the VPS. The user does this manually:

1. Go to https://login.tailscale.com/admin/settings/keys
2. Click "Generate auth key".
3. Settings:
   - **Reusable**: NO (one-shot, safer)
   - **Ephemeral**: NO (the VPS should persist)
   - **Pre-approved**: YES (so the device joins without manual approval in the admin UI)
   - **Tags**: `tag:mds-vps` (creates an ACL tag — see Step D)
   - **Expiration**: 90 days (default)
4. Copy the key (`tskey-auth-...`).
5. Paste it back to the agent.

The agent stores it in `~/.config/agentic-data-engineer/secrets/<client>-tailscale-authkey.txt` (chmod 600), **never** committed.

## Step B — Install Tailscale on the VPS

```bash
ssh deploy@<public_ip>

# Install Tailscale (official one-liner)
curl -fsSL https://tailscale.com/install.sh | sudo sh

# Join the tailnet with a stable hostname
sudo tailscale up \
  --authkey=<tskey-auth-...> \
  --hostname=<client>-mds \
  --ssh \
  --advertise-tags=tag:mds-vps

# Verify membership
tailscale status
tailscale ip -4    # prints the tailnet IP (usually 100.x.x.x)
```

> **`--ssh`** enables Tailscale's built-in SSH server. This means the user can `ssh deploy@<client>-mds` from any machine in the tailnet (including their laptop) without managing keys — Tailscale handles the auth. The traditional OpenSSH keypair (`~/.ssh/<client>_vps`) is still useful as a fallback. Record `"vps_tailscale_ssh": true` in the marker so a fresh session knows the keyless path is the default and only reaches for the key file when the tailnet itself is down.

## Step C — Install Tailscale on the user's laptop

If not yet installed:

- **Windows**: download installer from https://tailscale.com/download/windows
- **macOS**: `brew install --cask tailscale` or https://tailscale.com/download/mac
- **Linux**: `curl -fsSL https://tailscale.com/install.sh | sh`

Then `tailscale up` and complete the OAuth dance once.

Verify from the laptop:

```bash
ssh deploy@<client>-mds        # connects via tailnet
tailscale ping <client>-mds    # < 50 ms typically
```

If `ssh deploy@<client>-mds` works without a public IP, **Tailscale is fully operational**.

## Step D — Close public SSH (firewall lockdown)

Now that Tailscale works, public port 22 must be closed. The only allowed public traffic is whatever Tailscale needs (UDP 41641 outbound — handled automatically).

```bash
ssh deploy@<client>-mds   # via Tailscale from now on

# Install ufw if not present
sudo apt-get install -y ufw

# Allow SSH only from Tailscale interface
sudo ufw allow in on tailscale0 to any port 22 proto tcp
sudo ufw default deny incoming
sudo ufw default allow outgoing

# Enable. UFW will warn that we're closing existing SSH — confirm:
sudo ufw --force enable

sudo ufw status verbose
```

After this, `ssh deploy@<public_ip>` from a machine NOT in the tailnet should fail. `ssh deploy@<client>-mds` from inside the tailnet should still work.

**Verification check** (the agent runs this from the user's laptop):

```bash
# Should succeed:
ssh -o ConnectTimeout=5 deploy@<client>-mds "echo ok"

# Should fail with timeout if you can simulate (e.g., via mobile hotspot):
# ssh -o ConnectTimeout=5 deploy@<public_ip> "echo ok"
```

## Step E (Optional) — Add an on-prem source to the tailnet

If the user has a database on-prem (e.g. SQL Server on a Windows server inside their office), add that server to the same tailnet.

### Windows on-prem

1. User downloads and installs https://tailscale.com/download/windows on the on-prem server.
2. User runs Tailscale, logs in to the same account. The server appears in the admin console.
3. User sets the hostname for clarity (e.g., `acme-erp-server`).
4. Important: **Tailscale on Windows needs to run as a service so it survives reboot**, not as the user's interactive app. After install, in PowerShell as Administrator:
   ```powershell
   & "C:\Program Files\Tailscale\tailscale.exe" up
   ```
   This registers the device as a daemon. Confirm with `Get-Service Tailscale` shows `Running`.

### Linux on-prem

```bash
curl -fsSL https://tailscale.com/install.sh | sudo sh
sudo tailscale up --hostname=<onprem-host-name>
```

### Verify VPS → on-prem reachability

```bash
ssh deploy@<client>-mds
ping <onprem-host-name>
nc -zv <onprem-host-name> 1433   # if SQL Server on default port
```

If the on-prem database accepts connections from `100.x.x.x` (its tailnet IP), the dlt `sql_database` source running on the VPS will be able to reach it over the tailnet (see [`add-source`](../../add-source/SKILL.md)). (If you ran the Airbyte alternative instead, the same reachability lets Airbyte's generic database connector reach it.)

> **Gotcha**: many on-prem firewalls block all incoming traffic by default. The on-prem database's firewall must allow the Tailscale interface — typically `Tailscale Network Adapter` on Windows. This is the only manual firewall touch on the on-prem side.

## Step F — ACLs (optional but recommended)

Tailscale ACLs limit which devices can reach which. For a fresh tailnet, the default ACL allows all-to-all, which is fine for a one-person PYME but tightens easily:

```jsonc
// In the Tailscale admin → Access Controls
{
  "tagOwners": {
    "tag:mds-vps":         ["autogroup:admin"],
    "tag:mds-onprem":      ["autogroup:admin"]
  },
  "acls": [
    // Admins can reach everything
    {"action": "accept", "src": ["autogroup:admin"], "dst": ["*:*"]},
    // The VPS can reach on-prem databases on common DB ports
    {"action": "accept", "src": ["tag:mds-vps"], "dst": ["tag:mds-onprem:1433,3306,5432"]}
  ]
}
```

If the user is solo, skip this step. Add later when a team joins.

## Marker state after this step

```jsonc
{
  "decisions": {
    "tailscale_used": true,
    "tailnet_hostname_vps": "<client>-mds",
    "tailnet_hostname_onprem": "<onprem-host-name or null>",
    "vps_tailscale_ssh": true
  }
}
```

The public IP is no longer the primary access path — it's emergency-only. All subsequent steps use the tailnet hostname.
