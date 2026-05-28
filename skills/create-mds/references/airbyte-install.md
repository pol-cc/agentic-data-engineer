# Airbyte OSS install on the VPS

End state: Airbyte OSS running on the VPS, accessible from inside the tailnet, with a working OAuth2 API. Total time: ~15-25 minutes (most of it Airbyte's initial container pull and Kind cluster boot).

## Why `abctl` (Airbyte's CLI installer)

Airbyte OSS today is deployed as Kubernetes manifests. `abctl` ships a Kind-based Kubernetes-in-Docker that runs entirely on a single VPS — no real Kubernetes cluster required. This is the path Airbyte's docs themselves recommend for self-hosted single-node deployments. Tested at Airbyte v2.x on Ubuntu 24.04.

Alternative: the legacy `docker-compose` Airbyte. **Not recommended** — it has fewer features, the API surface differs, and Airbyte is removing support.

## Preflight

```bash
ssh deploy@<client>-mds

# Confirm Tailscale works and we're reachable
tailscale status

# Confirm at least 8 GB RAM and 30 GB free disk
free -h
df -h /
```

If RAM < 7 GB usable or free disk < 30 GB, **stop**. KVM 2 should give 8 GB / 100 GB — investigate before continuing.

## Step A — Install Docker

`abctl` requires Docker.

```bash
# Official Docker convenience script
curl -fsSL https://get.docker.com | sh

# Allow deploy user to run docker without sudo
sudo usermod -aG docker deploy
# Important: log out and back in for the group change to take effect:
exit
```

Reconnect:

```bash
ssh deploy@<client>-mds
docker run --rm hello-world      # should print the greeting
```

## Step B — Install `abctl`

```bash
curl -LsSf https://get.airbyte.com | sh
# Verify
abctl version
```

This installs `abctl` to `~/.airbyte/bin/abctl`. Add it to PATH if needed:

```bash
echo 'export PATH="$HOME/.airbyte/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

## Step C — Install Airbyte locally (Kind in Docker)

```bash
# This is the long-running step — pulls ~5 GB of images, boots a Kind cluster,
# applies Airbyte's Helm chart. 10-20 min depending on VPS network.
abctl local install
```

What `abctl` does behind the scenes:

1. Pulls Kind, creates a single-node Kubernetes cluster inside Docker.
2. Pulls Airbyte container images.
3. Applies the Airbyte Helm chart with sensible defaults.
4. Exposes the Airbyte UI on `http://localhost:8000` on the VPS.

Watch the output for "Installation completed" — and check:

```bash
abctl local status     # all components should be "Running"
docker ps              # Kind container should be up
kubectl --kubeconfig $HOME/.airbyte/abctl/kubeconfig.yaml get pods -A   # all pods Ready
```

## Step D — Get the auto-generated admin credentials

```bash
abctl local credentials
```

This prints something like:

```
Username: airbyte
Password: <random_string>
Client-Id: <client_id>
Client-Secret: <client_secret>
```

Capture all four into the agent's secrets store. The `Client-Id` / `Client-Secret` are the OAuth2 credentials for the public API.

> **Why not the username/password directly?** The Airbyte UI uses basic auth with the username/password; the **API** uses OAuth2 client credentials. Different surfaces.

## Step E — Smoke-test the OAuth2 API

The first source of confusion: Airbyte's **public API v2** is served under `/api/public/v1/`. That's not a typo. The v1 path under `/api/v1/` is the **internal** API — don't use it. Calling the internal API often returns 403 or 500 unhelpfully.

Get a token:

```bash
curl -X POST http://localhost:8000/api/public/v1/applications/token \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": "'$AIRBYTE_CLIENT_ID'",
    "client_secret": "'$AIRBYTE_CLIENT_SECRET'",
    "grant_type": "client_credentials"
  }'
```

Expected response:

```json
{ "access_token": "<jwt>", "token_type": "Bearer", "expires_in": 180 }
```

If 401: client_id/secret wrong, regenerate with `abctl local credentials`.
If 404: you're on the wrong path. Confirm it's `/api/public/v1/`.
If connection refused: port 8000 not bound on localhost. Check `abctl local status`.

Test that the token works by listing workspaces:

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/public/v1/applications/token \
  -H "Content-Type: application/json" \
  -d "{\"client_id\": \"$AIRBYTE_CLIENT_ID\", \"client_secret\": \"$AIRBYTE_CLIENT_SECRET\", \"grant_type\": \"client_credentials\"}" | jq -r .access_token)

curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/public/v1/workspaces
```

You should see at least one default workspace.

## Step F — (Optional, dev convenience) SSH tunnel for the UI

The Airbyte UI runs on port 8000 of the VPS, not exposed publicly. To browse it from the user's laptop:

```bash
# From the laptop, in a separate terminal:
ssh -L 8000:localhost:8000 deploy@<client>-mds
```

Then open `http://localhost:8000` in the laptop's browser. Log in with the username/password from Step D.

This tunnel only needs to be open when the user wants to inspect the UI. The agent does **not** use the UI — it uses the API over Tailscale SSH.

## Step G — Persistence and reboot survival

Airbyte's data lives in Docker volumes managed by `abctl`. To survive VPS reboots:

```bash
# Set up a systemd unit that restarts the Kind container
sudo tee /etc/systemd/system/airbyte.service > /dev/null <<'EOF'
[Unit]
Description=Airbyte (via abctl Kind cluster)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/home/deploy/.airbyte/bin/abctl local start
ExecStop=/home/deploy/.airbyte/bin/abctl local stop
User=deploy
TimeoutStartSec=600

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable airbyte
```

Test it:

```bash
sudo systemctl restart airbyte
abctl local status
```

After reboot, Airbyte should come back up within ~2 minutes.

## Common gotchas

- **"Connection refused" on port 8000**: `abctl local install` finished but the port isn't bound. Check `abctl local status`; sometimes Kind needs `abctl local start` again.
- **API returns 403 with valid token**: you might be hitting `/api/v1/` (internal) instead of `/api/public/v1/`. Recheck the path.
- **OOM during install on tight VPS**: KVM 1 (4 GB RAM) is too small. Need KVM 2 minimum.
- **`abctl: command not found` after install**: PATH not updated. Re-source `~/.bashrc` or use full path `~/.airbyte/bin/abctl`.
- **Stale Kind cluster after a botched install**: `abctl local uninstall --persisted` clears everything; re-run `abctl local install`.

## Marker state after this step

```jsonc
{
  "decisions": {
    "airbyte_version": "<output of abctl version>",
    "airbyte_api_base": "http://localhost:8000/api/public/v1/",
    "airbyte_client_id_ref": "secrets/<client>-airbyte-credentials.json"
  }
}
```

The actual `client_id` and `client_secret` are stored OUTSIDE the marker, in the secrets folder. The marker holds only a reference to where they live.
