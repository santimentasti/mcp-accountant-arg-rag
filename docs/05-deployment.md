# 05 — Deployment (generic VM / container host)

## About "instacloud.com"
`instacloud.com` could not be reached from the build sandbox, and the only "Instacloud" found is
**Stanford University IT's internal VM service** (Linux/Windows VMs with root, patching and nightly
snapshots, only for Stanford faculty/staff). Per the design decision, the deploy is **portable**: a hardened
Docker image + Compose stack that runs on any Linux VM with root (an Instacloud-style VM, a VPS, Hetzner,
DigitalOcean, AWS Lightsail / Local Zone Buenos Aires, etc.) or any container platform (Fly.io, Cloud Run,
Railway) with a persistent volume.

**Data residency tip:** for Ley 25.326 the simplest story is hosting in Argentina (e.g. AWS Local Zone
Buenos Aires or a local provider) or in an adequate jurisdiction, with a processing agreement.

## Architecture
```
Internet ──443──► Caddy (TLS, HSTS, body limit) ──internal network──► contador-mcp :8000 ──► /data volume
                                                                      (non-root, read-only FS)   (encrypted)
```

## Step by step on a Linux VM
1. **Harden the VM**: automatic security updates, SSH keys only, firewall allowing only 22/80/443
   (`ufw allow OpenSSH && ufw allow 80,443/tcp && ufw enable`).
2. **Encrypted disk for data**: use the provider's encrypted volumes (KMS) or LUKS on a dedicated disk,
   and point Docker's volume storage (or a bind mount) to it.
3. **Install Docker** (+ compose plugin) and clone the repo.
4. **Point DNS** for your domain (e.g. `mcp.tu-estudio.com.ar`) to the VM.
5. **Configure**:
   ```bash
   cp deploy/.env.example deploy/.env      # set DOMAIN and auth mode
   mkdir -p deploy/secrets
   # one token per person; scopes: read | read,write | read,write,admin
   docker run --rm --entrypoint contador-mcp contador-mcp:latest --gen-token estudio-perez \
       --scopes contador:read,contador:write
   # copy the printed JSON {sha256: {...}} entries into deploy/secrets/tokens.json (hashes only)
   chmod 600 deploy/secrets/tokens.json
   ```
   Hand each person their token through a secure channel (password manager), never by email/WhatsApp.
6. **Start**: `docker compose -f deploy/docker-compose.yml up -d --build`
   (behind a corporate proxy with its own CA: `docker build --secret id=pip_ca,src=ca.pem .`).
7. **Check**: `curl https://mcp.tu-estudio.com.ar/health` → `ok`; a request without token → `401`.

## Connecting clients
- **Claude Code**:
  `claude mcp add --transport http contador https://mcp.tu-estudio.com.ar/mcp --header "Authorization: Bearer cmcp_…"`
- **Other MCP clients** supporting streamable HTTP + bearer header: same URL and header.
- **claude.ai / ChatGPT custom connectors**: use `CONTADOR_AUTH_MODE=oauth` (Google sign-in) — see
  [`06-oauth-connector.md`](06-oauth-connector.md).

## JWT mode (recommended for teams)
Configure your IdP (Auth0, Supabase, WorkOS, Keycloak…) to issue access tokens with:
`iss` = your issuer, `aud` = `contador-mcp`, a `tenant` claim (the estudio id, `[A-Za-z0-9_-]`) and
`scope` containing `contador:read` / `contador:write` / `contador:admin`. Then set in `deploy/.env`:
`CONTADOR_AUTH_MODE=jwt`, `CONTADOR_JWT_JWKS_URI`, `CONTADOR_JWT_ISSUER`, `CONTADOR_JWT_AUDIENCE`.
Issuer and audience are mandatory: without them a token minted for another app of the same IdP would work.

## Backups
```bash
# consistent online backup of every tenant DB, encrypted with age (or gpg) before leaving the VM
docker compose -f deploy/docker-compose.yml exec contador sh -c \
  'for d in /data/tenants/*/; do python -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); s.backup(sqlite3.connect(sys.argv[2]))" "$d/contador.sqlite3" "/tmp/$(basename $d).bak"; done'
docker compose -f deploy/docker-compose.yml cp contador:/tmp/. ./backup/ && tar c backup | age -r <PUBKEY> > backup-$(date +%F).tar.age && rm -rf backup
```
Test restores quarterly. Keep backups for the tax prescription period.

## Updating
`git pull && docker compose -f deploy/docker-compose.yml up -d --build` — run `pip-audit` in CI and
rebuild monthly even without code changes, to pick up patched dependencies and base image.
