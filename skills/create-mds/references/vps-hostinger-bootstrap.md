# VPS bootstrap — Hostinger KVM 2

End state: a fresh Ubuntu 24.04 VPS, hardened, reachable via SSH, ready to receive Tailscale (next step). Total time: ~10 minutes wall-clock, ~5 minutes active.

## Why Hostinger KVM 2

See [`shared-references/stack-rationale.md`](../../../shared-references/stack-rationale.md#hostinger-vps--the-compute-host). The KVM 2 plan gives 2 vCPU, 8 GB RAM, 100 GB NVMe for ~$5-8/month — comfortable for dlt + dbt + (optional) MCP at PYME data volumes. With the dlt default (no Airbyte Kubernetes-in-Docker), even a smaller box can work; KVM 2 stays the safe recommendation and leaves headroom for the MCP server. The VPS is **disposable** — its durable state lives in BigQuery (`_dlt_*` cursors) and the client repo, so a rebuild loses nothing. Hostinger's API allows headless provisioning.

For Hetzner Cloud or DigitalOcean equivalents, swap the API calls accordingly — the rest of this playbook is identical.

## Preflight

Before this step:

- The user has a Hostinger account (created manually if needed — signup at https://hostinger.com/vps).
- The user is logged in to Hostinger's web console at least once (some accounts require a first-login captcha that can't be automated).

## Step A — Obtain a Hostinger API token (user ceremony)

The user does this once, manually:

1. Go to https://hpanel.hostinger.com/profile/api
2. Click "Generate token". Scope: "Full access" or limit to "VPS" if available.
3. Copy the token (shown only once).
4. Paste it back to the agent.

The agent stores the token in `~/.config/agentic-data-engineer/secrets/hostinger.token` (chmod 600), **never** in the client repo.

## Step B — Generate an SSH keypair (agent)

```bash
mkdir -p ~/.ssh
ssh-keygen -t ed25519 -f ~/.ssh/<client>_vps -N "" -C "<client>-mds-deploy"
```

This is the keypair the agent will use to reach the VPS. The private half stays on the user's laptop only; the public half is uploaded during provisioning.

## Step C — Provision the VPS via Hostinger API

> **Hostinger's public VPS API** lives under `https://developers.hostinger.com/api/vps/v1/`. The current endpoints and exact payload schema must be verified against their docs at the time of execution — Hostinger has rotated paths in the past. The flow below is the conceptual shape; map it to the live spec.

Steps:

1. List available plans, find KVM 2 ID:
   ```bash
   curl -H "Authorization: Bearer $HOSTINGER_TOKEN" \
     https://developers.hostinger.com/api/vps/v1/catalog/plans
   ```

2. List available OS templates, find Ubuntu 24.04 LTS:
   ```bash
   curl -H "Authorization: Bearer $HOSTINGER_TOKEN" \
     https://developers.hostinger.com/api/vps/v1/catalog/templates
   ```

3. List available datacenters, pick one close to the client:
   ```bash
   curl -H "Authorization: Bearer $HOSTINGER_TOKEN" \
     https://developers.hostinger.com/api/vps/v1/catalog/datacenters
   ```

4. Upload the SSH public key:
   ```bash
   curl -X POST -H "Authorization: Bearer $HOSTINGER_TOKEN" \
     -H "Content-Type: application/json" \
     -d "{\"name\": \"<client>-deploy\", \"key\": \"$(cat ~/.ssh/<client>_vps.pub)\"}" \
     https://developers.hostinger.com/api/vps/v1/public-keys
   ```

5. Create the VPS, attaching the SSH key:
   ```bash
   curl -X POST -H "Authorization: Bearer $HOSTINGER_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{
       "plan_id": "<kvm_2_plan_id>",
       "template_id": "<ubuntu_24_04_template_id>",
       "datacenter_id": "<chosen_dc_id>",
       "hostname": "<client>-mds",
       "ssh_keys": ["<ssh_key_id>"],
       "password": "<generated_strong_password>"
     }' \
     https://developers.hostinger.com/api/vps/v1/virtual-machines
   ```

6. Poll the VPS status until it reports `running` and an IP is assigned. This takes 3-5 minutes:
   ```bash
   curl -H "Authorization: Bearer $HOSTINGER_TOKEN" \
     https://developers.hostinger.com/api/vps/v1/virtual-machines/<vm_id>
   ```

Once the VPS is `running` with an IP, record:

- VPS ID
- Public IPv4
- The strong password (paste into `~/.config/agentic-data-engineer/secrets/<client>-vps.password`, chmod 600)

## Step D — First SSH and harden

```bash
ssh -i ~/.ssh/<client>_vps root@<public_ip>
```

On first connect, accept the host key. Then run the hardening script:

```bash
# Update everything
apt-get update && apt-get -y upgrade
apt-get -y install unattended-upgrades

# Enable auto security updates
dpkg-reconfigure -plow unattended-upgrades   # (skip — non-interactive equivalent below)
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

# Create a deploy user
adduser --disabled-password --gecos "" deploy
usermod -aG sudo deploy
echo "deploy ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/deploy
mkdir -p /home/deploy/.ssh
cp /root/.ssh/authorized_keys /home/deploy/.ssh/
chown -R deploy:deploy /home/deploy/.ssh
chmod 700 /home/deploy/.ssh
chmod 600 /home/deploy/.ssh/authorized_keys

# Disable root SSH login (use deploy from now on)
sed -i 's/^PermitRootLogin yes/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/^#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart sshd

# Install useful base tools
apt-get -y install curl jq htop tmux git
```

## Step E — Verify

From the user's laptop:

```bash
# Should fail (root is disabled):
ssh -i ~/.ssh/<client>_vps root@<public_ip>      # expected: Permission denied

# Should succeed:
ssh -i ~/.ssh/<client>_vps deploy@<public_ip>
```

If the `deploy` SSH works, this step is done. **Do NOT close port 22 to the public internet yet** — Tailscale isn't installed. That happens in the next reference, [`tailscale-onprem.md`](tailscale-onprem.md), once the VPS is reachable through Tailscale.

## Cleanup on failure

If provisioning fails partway:

```bash
# Delete the half-created VPS
curl -X DELETE -H "Authorization: Bearer $HOSTINGER_TOKEN" \
  https://developers.hostinger.com/api/vps/v1/virtual-machines/<vm_id>
```

Re-run the playbook from Step C.

## What state to record in the marker

After this step succeeds, the agent updates `.agentic-data-engineer.json` (which doesn't exist yet — it's created at the end of Phase 1) with the planned values:

```jsonc
{
  "decisions": {
    "vps_provider": "hostinger",
    "vps_id": "<hostinger_vm_id>",
    "vps_public_ip": "<public_ip>",
    "vps_user": "deploy"
  }
}
```

Tailscale hostname is added in the next step.
