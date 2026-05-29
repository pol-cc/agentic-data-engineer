# Traefik + TLS setup

End state: Traefik running on the VPS as a Docker container, serving TLS on port 443 for the MCP endpoint, auto-renewing certificates via Let's Encrypt. Total time: ~20 minutes (5 active, 15 waiting for ACME).

> **Skip this whole step if the client declined the MCP.** Phase 3 (and therefore the public TLS endpoint, the domain, and Traefik) is opt-in. A Phase-2 deployment exposes no public port and needs no reverse proxy. Only set up Traefik once the client has opted into the MCP serving layer — see [`phase-3-agentic-layer.md`](phase-3-agentic-layer.md) for the opt-in gate.

## Why Traefik (not nginx, not Caddy)

Traefik is chosen for three reasons:

1. **Docker label-driven config.** Containers register routes via labels (`traefik.http.routers.mcp.rule=Host(...)`). No separate Traefik config files to edit when adding a new service. The MCP container declares its own route.
2. **Built-in Let's Encrypt.** ACME flow is configured once at the Traefik level; new containers get TLS automatically.
3. **Active project.** Caddy is fine but smaller community. Nginx is heavier-touch for the same outcome.

> If the deployment already has Nginx or Caddy from prior work, use that — no need to add Traefik. The label patterns in Step E adapt directly to Caddy v2 and to a manual Nginx site config. Traefik is the v0.3.0 *default*, not a requirement.

## Preflight

```bash
ssh deploy@<client>-mds

# Confirm Docker is installed (from Phase 1 Airbyte step)
docker --version

# Confirm DNS resolves to this VPS
dig +short mcp.<client-domain>.com
# Expected: VPS public IPv4 (the same IP returned by `curl -s ifconfig.me` on the VPS)

# Confirm ports 80/443 are reachable on the VPS public IP
# (Run from anywhere with internet access, not from the VPS):
nc -zv <VPS_PUBLIC_IP> 80
nc -zv <VPS_PUBLIC_IP> 443
# Expected: connection refused (no service yet — but reachable)
# If timeout: Hostinger or upstream firewall is blocking. Open them.
```

If DNS doesn't resolve yet, stop and wait. Traefik will fail Let's Encrypt's HTTP-01 challenge if the domain doesn't reach this VPS.

## Step A — Open ports 80 and 443 on the VPS firewall

In Phase 1 we closed everything except Tailscale. Open 80 and 443 to the world:

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw status verbose
```

These are the only public ports the deployment will have. SSH and Airbyte remain Tailscale-only.

## Step B — Create the Traefik docker-compose

```bash
mkdir -p /home/deploy/traefik
cd /home/deploy/traefik

# Create a Docker network that Traefik and downstream services will share
docker network create traefik-net

cat > docker-compose.yml <<'EOF'
services:
  traefik:
    image: traefik:v3.5
    container_name: traefik
    restart: unless-stopped
    networks:
      - traefik-net
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - ./letsencrypt:/letsencrypt
      - ./traefik.yml:/traefik.yml:ro
    command:
      - --configfile=/traefik.yml

networks:
  traefik-net:
    external: true
EOF
```

## Step C — Write the Traefik static config

```bash
cat > /home/deploy/traefik/traefik.yml <<'EOF'
# Traefik static configuration

api:
  # Dashboard disabled in production — Tailscale users can SSH-tunnel to localhost if needed.
  dashboard: false

entryPoints:
  web:
    address: ":80"
    # Auto-redirect HTTP to HTTPS
    http:
      redirections:
        entryPoint:
          to: websecure
          scheme: https
          permanent: true
  websecure:
    address: ":443"
    http:
      tls:
        certResolver: letsencrypt

providers:
  docker:
    exposedByDefault: false   # Containers must opt-in with labels
    network: traefik-net

certificatesResolvers:
  letsencrypt:
    acme:
      email: <user-email>     # REPLACE with the user's email for cert expiry notices
      storage: /letsencrypt/acme.json
      httpChallenge:
        entryPoint: web

log:
  level: INFO
  format: json
EOF

# Initialize the ACME storage with strict perms (Traefik refuses to start otherwise)
mkdir -p /home/deploy/traefik/letsencrypt
touch /home/deploy/traefik/letsencrypt/acme.json
chmod 600 /home/deploy/traefik/letsencrypt/acme.json
```

Replace `<user-email>` with the user's actual email. Let's Encrypt sends renewal-expiry warnings (rarely fires because Traefik auto-renews 30 days before expiry).

## Step D — Start Traefik

```bash
cd /home/deploy/traefik
docker compose up -d

# Verify
docker ps | grep traefik
docker logs traefik 2>&1 | tail -20
```

At this point Traefik is running but has no routes. Hitting the domain returns a 404.

## Step E — Optional smoke test: a temporary "hello" service

To confirm Traefik is correctly wired before deploying the MCP container, deploy a tiny test service:

```bash
docker run -d \
  --name whoami-test \
  --network traefik-net \
  --label "traefik.enable=true" \
  --label "traefik.http.routers.whoami.rule=Host(\`mcp.<client-domain>.com\`)" \
  --label "traefik.http.routers.whoami.entrypoints=websecure" \
  --label "traefik.http.routers.whoami.tls.certresolver=letsencrypt" \
  --label "traefik.http.services.whoami.loadbalancer.server.port=80" \
  traefik/whoami
```

Wait ~30 seconds for Let's Encrypt to issue the cert, then test:

```bash
curl -I https://mcp.<client-domain>.com/
# Expected: HTTP/2 200, no TLS warnings
```

If the cert fails to issue:

```bash
docker logs traefik 2>&1 | grep -i acme
# Common errors: DNS not yet propagated, port 80 not actually reachable from internet,
# rate limit hit (5 certs/week per registered domain on Let's Encrypt).
```

Once verified, **remove the test container** before deploying the real MCP:

```bash
docker stop whoami-test
docker rm whoami-test
```

The cert stays in `letsencrypt/acme.json` for the domain — issued once, reused by the next container with the same Host rule.

## Step F — Auto-restart on VPS reboot

The compose stack restarts on its own (the `restart: unless-stopped` policy). Confirm:

```bash
sudo systemctl enable docker     # docker daemon starts at boot (usually already on)
docker ps                        # traefik container should be Running after a reboot
```

## Step G — Save Traefik config to the client repo

```bash
# On the user's laptop, in the client repo
mkdir -p infra/traefik
scp deploy@<client>-mds:/home/deploy/traefik/docker-compose.yml infra/traefik/
scp deploy@<client>-mds:/home/deploy/traefik/traefik.yml infra/traefik/

# Add to .gitignore: letsencrypt/ — never commit the cert storage
cat >> .gitignore <<'EOF'

# Traefik cert storage (contains private keys)
infra/traefik/letsencrypt/
EOF

git add infra/traefik/docker-compose.yml infra/traefik/traefik.yml .gitignore
git commit -m "Phase 3: Traefik TLS reverse proxy config"
```

## Common gotchas

- **`acme.json` permissions too open** → Traefik refuses to start. Always `chmod 600`.
- **Let's Encrypt rate limit** → 5 certs per registered domain per week. If you hit it during testing, use `acme-staging-v02.api.letsencrypt.org/directory` as `caServer` in traefik.yml until things stabilize, then switch back to production.
- **DNS hasn't propagated yet** → ACME challenge fails. Wait, retry. Use `https://dnschecker.org/` to confirm visibility from multiple resolvers.
- **Cloudflare proxying enabled on the DNS record** → the orange cloud must be OFF for ACME to work directly. Or use the DNS challenge instead (more setup, but works with Cloudflare proxy on).
- **Container behind Traefik must be on the same Docker network** → if you forget `--network traefik-net`, Traefik can't reach the container and returns 502. Easy to miss.

## Marker state after this step

```jsonc
{
  "decisions": {
    "traefik_used": true,
    "traefik_version": "v3.5",
    "mcp_domain": "mcp.<client-domain>.com",
    "letsencrypt_email": "<user-email>"
  }
}
```
