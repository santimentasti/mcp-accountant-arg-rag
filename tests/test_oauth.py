"""End-to-end OAuth 2.1 flow as claude.ai would run it, against a fake upstream identity provider.

discovery -> dynamic client registration -> /authorize (PKCE) -> consent screen -> upstream IdP login ->
callback -> code -> /token -> MCP calls with the issued token -> membership policy (invite list).
"""

import asyncio
import base64
import hashlib
import html
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.auth import BearerAuth
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import RSAKeyPair
from starlette.applications import Starlette
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from conftest import ACME

ISS = "https://fake-idp.test"
AUD = "fake-client"
CLIENT_CB = "http://localhost:33418/callback"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(port: int) -> None:
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError(f"port {port} never opened")


class FakeIdP:
    """Minimal OAuth server: logs in whoever `current_user` is (stands in for Google)."""

    def __init__(self):
        self.key = RSAKeyPair.generate()
        self.current_user = {"email": "nobody@test", "email_verified": True}
        self.codes: dict[str, dict] = {}
        self.port = _free_port()

        async def authorize(request):
            q = request.query_params
            code = secrets.token_urlsafe(16)
            self.codes[code] = dict(self.current_user)
            return RedirectResponse(f"{q['redirect_uri']}?{urlencode({'code': code, 'state': q['state']})}", 302)

        async def token(request):
            form = await request.form()
            user = self.codes.pop(form.get("code"), None)
            if not user:
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            at = self.key.create_token(subject=user["email"], issuer=ISS, audience=AUD, scopes=["openid", "email"],
                                       additional_claims=user)
            return JSONResponse({"access_token": at, "token_type": "Bearer", "expires_in": 3600})

        app = Starlette(routes=[Route("/authorize", authorize), Route("/token", token, methods=["POST"])])
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))
        threading.Thread(target=self.server.run, daemon=True).start()
        _wait_port(self.port)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    data_dir = tmp_path_factory.mktemp("oauth-data")
    idp = FakeIdP()
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "CONTADOR_AUTH_MODE": "oauth", "CONTADOR_OAUTH_PROVIDER": "custom",
           "CONTADOR_PUBLIC_URL": base, "CONTADOR_OAUTH_CLIENT_ID": AUD, "CONTADOR_OAUTH_CLIENT_SECRET": "s3cret",
           "CONTADOR_OAUTH_AUTHORIZE_URL": f"{idp.base}/authorize", "CONTADOR_OAUTH_TOKEN_URL": f"{idp.base}/token",
           "CONTADOR_OAUTH_PUBLIC_KEY": idp.key.public_key, "CONTADOR_OAUTH_ISSUER": ISS,
           "CONTADOR_OAUTH_AUDIENCE": AUD, "CONTADOR_ALLOWED_EMAILS": "ana@estudio.com.ar",
           "CONTADOR_SUPPORT_CONTACT": "santi@example.com", "CONTADOR_OPERATOR_NAME": "Beta Contadores",
           "CONTADOR_DENSE_MODEL": "none"}
    cli = [sys.executable, "-m", "contador_mcp.server", "--data-dir", str(data_dir)]
    subprocess.run(cli + ["--invite", "beto@estudioperez.com", "--tenant", "estudio-perez", "--role", "viewer"],
                   env=env, check=True, capture_output=True)
    proc = subprocess.Popen(cli + ["--transport", "http", "--port", str(port)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    _wait_port(port)
    yield {"base": base, "idp": idp, "env": env, "cli": cli, "data_dir": data_dir}
    proc.terminate()
    proc.wait(timeout=10)
    idp.server.should_exit = True


def oauth_login(base: str, idp: FakeIdP, email: str, verified: bool = True) -> str:
    """Drive the browser part of the flow headlessly and return the MCP access token."""
    idp.current_user = {"email": email, "email_verified": verified}
    with httpx.Client(follow_redirects=False, timeout=10) as http:
        prm = http.get(f"{base}/.well-known/oauth-protected-resource/mcp").json()
        assert prm["authorization_servers"]
        meta = http.get(f"{base}/.well-known/oauth-authorization-server").json()
        assert "S256" in meta["code_challenge_methods_supported"]
        reg = http.post(meta["registration_endpoint"], json={
            "redirect_uris": [CLIENT_CB], "token_endpoint_auth_method": "none", "client_name": "Test MCP client",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
        assert reg.status_code == 201, reg.text
        client_id = reg.json()["client_id"]

        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        url = meta["authorization_endpoint"] + "?" + urlencode({
            "response_type": "code", "client_id": client_id, "redirect_uri": CLIENT_CB, "state": "st-123",
            "code_challenge": challenge, "code_challenge_method": "S256", "resource": f"{base}/mcp"})
        code = None
        for _ in range(10):
            r = http.get(url)
            if r.status_code == 200 and "<form" in r.text:  # consent screen: the user clicks "Allow"
                form = r.text
                action = html.unescape(re.search(r'<form[^>]*action="([^"]*)"', form).group(1))
                fields = {m.group(1): html.unescape(m.group(2)) for m in
                          re.finditer(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', form)}
                fields["action"] = "approve"
                r = http.post(httpx.URL(url).join(action), data=fields)
            assert r.status_code in (302, 303, 307), (r.status_code, r.text[:300])
            url = str(httpx.URL(url).join(r.headers["location"]))
            if url.startswith(CLIENT_CB):
                q = parse_qs(urlparse(url).query)
                assert q["state"] == ["st-123"]
                code = q["code"][0]
                break
        assert code, "never reached the client callback"
        tok = http.post(meta["token_endpoint"], data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": CLIENT_CB, "client_id": client_id,
            "code_verifier": verifier})
        assert tok.status_code == 200, tok.text
        return tok.json()["access_token"]


def run(coro):
    return asyncio.run(coro)


def test_invited_email_gets_personal_workspace(stack):
    token = oauth_login(stack["base"], stack["idp"], "ana@estudio.com.ar")

    async def scenario():
        async with Client(f"{stack['base']}/mcp", auth=BearerAuth(token)) as c:
            me = (await c.call_tool("whoami", {})).data
            assert me["usuario"] == "ana@estudio.com.ar" and me["tenant"].startswith("u-")
            assert "contador:admin" in me["scopes"] and me["autenticacion"] == "oauth"
            await c.call_tool("client_register", {"cuit": ACME, "razon_social": "ACME SA", "tipo_persona": "juridica",
                                                  "condicion_iva": "responsable_inscripto", "iibb_regimen": "local"})
            assert len((await c.call_tool("client_list", {})).data) == 1

    run(scenario())


def test_members_file_maps_estudio_and_role(stack):
    token = oauth_login(stack["base"], stack["idp"], "Beto@EstudioPerez.com")  # case-insensitive

    async def scenario():
        async with Client(f"{stack['base']}/mcp", auth=BearerAuth(token)) as c:
            me = (await c.call_tool("whoami", {})).data
            assert me["tenant"] == "estudio-perez" and me["scopes"] == ["contador:read"]
            assert (await c.call_tool("client_list", {})).data == []  # does not see ana's workspace
            with pytest.raises(ToolError, match="contador:write"):
                await c.call_tool("client_add_note", {"cuit": ACME, "note": "x"})

    run(scenario())


def test_uninvited_and_unverified_are_rejected_then_invite_without_restart(stack):
    token = oauth_login(stack["base"], stack["idp"], "eve@gmail.com")

    async def denied():
        with pytest.raises(Exception, match="no está invitada"):
            async with Client(f"{stack['base']}/mcp", auth=BearerAuth(token)) as c:
                await c.list_tools()

    run(denied())

    unverified = oauth_login(stack["base"], stack["idp"], "ana@estudio.com.ar", verified=False)

    async def not_verified():
        with pytest.raises(Exception, match="no está verificado"):
            async with Client(f"{stack['base']}/mcp", auth=BearerAuth(unverified)) as c:
                await c.call_tool("whoami", {})

    run(not_verified())

    subprocess.run(stack["cli"] + ["--invite", "eve@gmail.com"], env=stack["env"], check=True, capture_output=True)

    async def now_ok():
        async with Client(f"{stack['base']}/mcp", auth=BearerAuth(token)) as c:
            assert (await c.call_tool("whoami", {})).data["usuario"] == "eve@gmail.com"

    run(now_ok())


def test_security_of_oauth_endpoints(stack):
    base = stack["base"]
    with httpx.Client(timeout=10) as http:
        meta = http.get(f"{base}/.well-known/oauth-authorization-server").json()
        evil = http.post(meta["registration_endpoint"], json={
            "redirect_uris": ["https://evil.example/cb"], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code"], "response_types": ["code"]})
        assert evil.status_code == 400  # only claude.ai / chatgpt / localhost callbacks may register
        assert http.post(f"{base}/mcp", json={}).status_code == 401
        assert http.post(f"{base}/mcp", json={}, headers={"Authorization": "Bearer forged.jwt.token"}).status_code == 401
        landing = http.get(f"{base}/")
        assert landing.status_code == 200 and f"{base}/mcp" in landing.text and "Beta Contadores" in landing.text
        assert "default-src 'none'" in landing.headers["content-security-policy"]
        assert http.get(f"{base}/privacidad").status_code == 200
    state = stack["data_dir"] / "oauth-state"
    assert state.is_dir() and oct(state.stat().st_mode & 0o777) == "0o700"
    assert oct((stack["data_dir"] / "oauth-signing.key").stat().st_mode & 0o777) == "0o600"
    # OAuth state is encrypted at rest: no plaintext emails or tokens on disk
    blob = b"".join(p.read_bytes() for p in state.rglob("*") if p.is_file())
    assert b"ana@estudio.com.ar" not in blob and b"eyJ" not in blob
    members = json.loads((stack["data_dir"] / "members.json").read_text())
    assert members["members"]["beto@estudioperez.com"] == {"tenant": "estudio-perez", "role": "viewer"}


def test_google_provider_hands_off_to_google(tmp_path):
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = {**os.environ, "CONTADOR_AUTH_MODE": "oauth", "CONTADOR_OAUTH_PROVIDER": "google",
           "CONTADOR_PUBLIC_URL": base, "CONTADOR_OAUTH_CLIENT_ID": "123-abc.apps.googleusercontent.com",
           "CONTADOR_OAUTH_CLIENT_SECRET": "x", "CONTADOR_DENSE_MODEL": "none"}
    proc = subprocess.Popen([sys.executable, "-m", "contador_mcp.server", "--transport", "http", "--port", str(port),
                             "--data-dir", str(tmp_path)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        _wait_port(port)
        with httpx.Client(follow_redirects=False, timeout=10) as http:
            meta = http.get(f"{base}/.well-known/oauth-authorization-server").json()
            cid = http.post(meta["registration_endpoint"], json={
                "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"], "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}).json()["client_id"]
            url = meta["authorization_endpoint"] + "?" + urlencode({
                "response_type": "code", "client_id": cid, "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                "state": "s", "code_challenge": "x" * 43, "code_challenge_method": "S256"})
            for _ in range(5):
                r = http.get(url)
                if r.status_code == 200 and "<form" in r.text:
                    action = html.unescape(re.search(r'<form[^>]*action="([^"]*)"', r.text).group(1))
                    fields = {m.group(1): html.unescape(m.group(2)) for m in
                              re.finditer(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', r.text)}
                    r = http.post(httpx.URL(url).join(action), data={**fields, "action": "approve"})
                url = str(httpx.URL(url).join(r.headers["location"]))
                if "accounts.google.com" in url:
                    break
        q = parse_qs(urlparse(url).query)
        assert urlparse(url).hostname == "accounts.google.com"
        assert q["client_id"] == ["123-abc.apps.googleusercontent.com"]
        assert q["redirect_uri"] == [f"{base}/auth/callback"]
        assert "email" in q["scope"][0] and q["code_challenge_method"] == ["S256"]
    finally:
        proc.terminate()
        proc.wait(timeout=10)
