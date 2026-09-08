# Homepage

![Homepage](../screenshots/homepage.png)

Dashboard UI for the home lab, powered by [gethomepage/homepage](https://gethomepage.dev).

**Local URL:** `https://<NAS_IP>:3000` (HTTPS + Authelia SSO via Nginx)
**External URL:** `https://<NAS_HOST>` (port 443, via Synology Reverse Proxy → Nginx → Homepage)

## File Structure

```
homepage/
├── docker-compose.yml    ← homepage + nginx services
├── nginx/
│   └── nginx.conf        ← Nginx reverse proxy + Authelia forward-auth config
└── config/
    ├── settings.yaml     ← theme, layout
    ├── widgets.yaml      ← top bar: datetime, search, resources
    ├── services.yaml     ← service cards
    ├── bookmarks.yaml    ← bookmark links (Quick Access / NAS Admin / Network / Dev Tools / Reference)
    └── docker.yaml       ← docker socket config
```

## Setup

Environment variables live in `homepage/.env`, which is **generated** — never
edit it by hand. Values come from the encrypted vault via this stack's
`secrets.manifest.yaml`:

```bash
make edit-vault   # change a value (sops decrypts on read, re-encrypts on save)
make secrets      # regenerate homepage/.env from vault + manifest
./scripts/deploy.sh
```

See [`../secrets/README.md`](../secrets/README.md) for the full workflow.

## HTTPS + Authelia SSO

Access to the homepage is protected by Nginx with TLS and Authelia SSO forward-auth.

| Item | Detail |
|------|--------|
| Authentication | Authelia SSO forward-auth — managed by the `auth/` stack |
| Port layout | Nginx listens on **443 SSL**, exposed on host port **3000**; homepage is internal-only |
| TLS certificate | Synology default cert mounted from `/usr/syno/etc/certificate/system/default/` — uses `RSA-cert.pem` / `RSA-privkey.pem` |

The `auth/` stack must be running before starting homepage (provides `auth_net` network and Authelia endpoint).

### Host Validation

`HOMEPAGE_ALLOWED_HOSTS` must include every hostname:port combination that clients use to reach the container. The current value covers:

| Entry | When used |
|---|---|
| `<NAS_HOST>` | Browser on HTTPS default port (no port suffix in Host header) |
| `<NAS_HOST>:443` | Clients that include the explicit port |
| `<NAS_HOST>:3000` | Direct LAN HTTPS access |
| `192.168.50.200:3000` | Local IP access |

### SSL Certificate Auto-Renewal

The Nginx container reads the cert at startup and caches it in memory. When Synology renews the cert (every 90 days), Nginx must be reloaded to pick it up. Set up a **Synology Task Scheduler** monthly task:

```bash
# DSM → Control Panel → Task Scheduler → Scheduled Task → User-defined script
# Schedule: Monthly, day 1, 03:00 | User: root
docker exec homepage-nginx nginx -s reload
```

## Secrets Injection

Credentials are never hardcoded in config files. They flow through two layers:

1. Docker Compose reads variables from the generated `homepage/.env` via `env_file: .env` and injects `HOMEPAGE_VAR_*` container env vars directly.
2. `services.yaml` references them as `{{HOMEPAGE_VAR_*}}` — Homepage interpolates these at runtime.

### Key Variables (in the generated `homepage/.env`)

| Variable | Purpose |
|---|---|
| `HOMEPAGE_VAR_NAS_URL` | DSM API base URL — use **HTTP port 5000** (`http://192.168.x.x:5000`), not HTTPS, to avoid SSL cert mismatch on IP address |
| `HOMEPAGE_VAR_DDNS_BASE_HTTP` | External base for services that don't support HTTPS (e.g. ping checks) |
| `HOMEPAGE_VAR_DDNS_BASE_HTTPS` | External base for clickable service links (HTTPS via Synology Reverse Proxy) |
| `HOMEPAGE_ALLOWED_HOSTS` | Comma-separated list of allowed hostnames — must include bare `<NAS_HOST>` (no port) for standard HTTPS access |
| `HOMEPAGE_VAR_N8N_HTTPS` | Clickable link for the n8n card. **Changed 2026-09-08** — the DSM reverse proxy moved from an `n8n.<NAS_HOST>` subdomain on :443 to `<NAS_HOST>:15678` |
| `HOMEPAGE_VAR_N8N_HTTP` | Ping target for the n8n card. Stays `http://<NAS_IP>:5678` — LAN-direct, never through the reverse proxy, so the RP change did not touch it |

> Every card that points at a stack carries this HTTP/HTTPS pair: the HTTPS one
> is the link a browser follows, the HTTP one is the LAN address the ping check
> hits. Change a stack's reverse proxy entry and only the HTTPS half needs
> updating — in the vault, not in `.env`.

## n8n Webhook Ingress

This stack's Nginx also fronts n8n's webhook paths. Telegram registers webhooks
only on ports 80, 88, 443 and 8443, and :443 is the only one forwarded at the
router — homepage already owns it through the DSM reverse proxy. So instead of
giving n8n its own entry, three prefix locations here forward to it:

| Path | Destination |
|---|---|
| `/webhook/` | `host.docker.internal:5678` |
| `/webhook-test/` | `host.docker.internal:5678` |
| `/webhook-waiting/` | `host.docker.internal:5678` |

Prefix locations take precedence over `location /`, so these bypass basic auth —
deliberately: a webhook has to be callable by Telegram, and the path already
carries n8n's per-workflow UUID. n8n's own basic auth never covered these paths.

`extra_hosts: host.docker.internal:host-gateway` on the `nginx` service is what
makes the hop work; n8n runs in a separate compose project, so its container
name does not resolve from here.

**This couples the two stacks: if homepage is down, the n8n bot stops receiving
messages.** Forwarding port 8443 at the router would let n8n have its own
reverse proxy entry and these three locations could be deleted.

## Configuration

All config files in `config/` are hot-reloaded — no container restart needed after edits.

| File | Purpose |
|---|---|
| `settings.yaml` | Theme, layout, title |
| `widgets.yaml` | Top bar widgets (clock, search, system resources) |
| `services.yaml` | Service cards with API widgets |
| `bookmarks.yaml` | Quick-access links |
| `docker.yaml` | Docker socket connection for container status widgets |

