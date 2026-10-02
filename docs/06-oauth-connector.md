# 06 — OAuth connector: give it to accountants

Goal: an accountant pastes **one URL** into Claude (or ChatGPT), signs in with **Google**, and gets their own
isolated workspace. You control who gets in with an invite list.

## Big picture: how the login works
MCP remote connectors use **OAuth 2.1** (the same standard as "Sign in with Google" buttons):

```
Accountant's Claude ──(1) discovers /.well-known/oauth-* ──► contador-mcp (OAuth proxy)
                    ──(2) registers itself (Dynamic Client Registration, only claude.ai/chatgpt/localhost callbacks)
                    ──(3) /authorize + PKCE ──► consent screen ("Allow Claude to access contador-mcp?")
                                             ──► Google login ──► /auth/callback
                    ◄─(4) code ── (5) /token ──► contador-mcp issues ITS OWN short-lived token
                    ──(6) MCP calls with that token ──► email verified? invited? ──► tenant + role ──► tools
```
Key ideas:
- **PKCE** proves that the app finishing the login is the one that started it (stops stolen codes).
- **Proxy pattern**: Google doesn't support dynamic registration, so contador-mcp acts as the authorization
  server for MCP clients and delegates the *identity* check to Google. Google tokens never reach Claude.
- **Consent screen** (on our server) prevents a malicious app from silently reusing a previous approval.
- **Identity ≠ access**: Google proves *who* you are; `members.json` / allowlist decides *what* you can do.

## Option A — 15-minute pilot from your laptop (no domain, no VM)
Good to show it to 2–3 colleagues. The server runs on your machine and a free Cloudflare tunnel gives it a
public HTTPS URL.

```bash
pip install -e ".[pdf]"
cloudflared tunnel --url http://localhost:8000      # prints https://<random>.trycloudflare.com
```
Then do the Google setup below using that URL, and run:
```bash
export CONTADOR_AUTH_MODE=oauth CONTADOR_OAUTH_PROVIDER=google \
       CONTADOR_PUBLIC_URL=https://<random>.trycloudflare.com \
       CONTADOR_OAUTH_CLIENT_ID=... CONTADOR_OAUTH_CLIENT_SECRET=... \
       CONTADOR_ALLOWED_EMAILS=colega1@gmail.com,colega2@gmail.com \
       CONTADOR_SUPPORT_CONTACT=vos@gmail.com CONTADOR_OPERATOR_NAME="Contador IA (beta)"
contador-mcp --transport http --port 8000
```
Caveats: the URL changes every time the tunnel restarts (you'd update Google + tell colleagues) and the
service is down when your laptop sleeps. For anything longer than a demo, use Option B.

## Option B — stable beta on a VM (recommended)
Follow [`05-deployment.md`](05-deployment.md) (Docker + Caddy HTTPS on your domain) with the OAuth block of
`deploy/.env.example`.

## Google Cloud setup (≈10 minutes, free)
1. Go to <https://console.cloud.google.com/> → create a project (e.g. "contador-mcp").
2. **APIs & Services → OAuth consent screen** (Google Auth Platform → Branding/Audience):
   - User type **External**. App name, support email, and your **privacy policy URL**:
     `https://<your-domain>/privacidad` (the server already serves it).
   - Scopes: only `openid` and `.../auth/userinfo.email` (non-sensitive → no Google review needed).
   - Audience: keep it in **Testing** and add your colleagues as **test users** (up to 100). That is a
     second gate on top of our own invite list. Publish later when you open the beta.
3. **Credentials → Create credentials → OAuth client ID → Web application**:
   - Authorized redirect URI: **`https://<your-domain>/auth/callback`** (exactly; for the tunnel use the
     trycloudflare URL).
   - Copy the client ID and secret into `CONTADOR_OAUTH_CLIENT_ID` / `CONTADOR_OAUTH_CLIENT_SECRET`.

## Managing access
| Want | Do |
|---|---|
| Let a colleague try it in their own sandbox | add their email to `CONTADOR_ALLOWED_EMAILS` (restart) **or** `contador-mcp --invite colega@gmail.com` (no restart) |
| Put several people of one estudio in the same workspace | `contador-mcp --invite ana@estudio.com --tenant estudio-perez --role admin` and `--role member` / `--role viewer` for the rest |
| Remove someone | `contador-mcp --revoke colega@gmail.com` (effective immediately) |
| Everyone from a domain | `CONTADOR_ALLOWED_DOMAINS=estudioperez.com.ar` |
| Open beta | `CONTADOR_ACCESS_POLICY=open` (any verified Google account gets a personal sandbox) |

In Docker: `docker compose -f deploy/docker-compose.yml exec contador contador-mcp --invite colega@gmail.com`.

Roles: `viewer` = read only · `member` = read + load data · `admin` = + delete knowledge docs + audit log.

## Message to send to your colleagues (copy & paste)
> Hola! Estoy probando un asistente de IA para estudios contables: arma el calendario de vencimientos
> por CUIT, checklists de inicio y cierre de mes, calcula posición de IVA, F.931, SAC, anticipos y Convenio
> Multilateral, y responde consultas normativas citando la fuente.
>
> Para probarlo (5 min):
> 1. En Claude (claude.ai o la app): Configuración → Conectores → *Agregar conector personalizado*.
> 2. Pegá esta URL: **https://&lt;tu-dominio&gt;/mcp**
> 3. Tocá *Conectar* e ingresá con tu cuenta de Google (te invité con *tu-email*).
> 4. Probá: "Registrá a mi cliente ACME SA, CUIT 30-71234567-1, sociedad RI con empleados. ¿Qué vence
>    esta semana?"
>
> Es una beta: usá datos de prueba o anonimizados, y verificá fechas y alícuotas contra ARCA.
> Instrucciones y privacidad: https://&lt;tu-dominio&gt;/

## Troubleshooting
| Symptom | Cause / fix |
|---|---|
| "no está invitada" when connecting | Email not in members/allowlist → `--invite` |
| Google error `redirect_uri_mismatch` | The redirect URI in Google must be exactly `<CONTADOR_PUBLIC_URL>/auth/callback` |
| Google "access blocked: app not verified / not a test user" | Add the email as a test user in the consent screen (Testing mode) |
| Connector asks to log in again after a restart | `CONTADOR_OAUTH_SIGNING_KEY` changed or `/data` isn't persistent |
| ChatGPT can't register | Check its callback is in `CONTADOR_OAUTH_REDIRECT_URIS` (defaults include chatgpt.com) |
