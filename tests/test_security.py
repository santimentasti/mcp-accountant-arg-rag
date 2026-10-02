"""Security tests: file access guard, tenant isolation, scopes, auth over HTTP, error masking, audit."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import replace

import pytest
from fastmcp import Client
from fastmcp.client.auth import BearerAuth
from fastmcp.exceptions import ToolError

from conftest import ACME, TODAY

from contador_mcp.security import (
    SCOPE_READ, SCOPE_WRITE, HashedStaticTokenVerifier, SecurityConfig, generate_token, hash_token,
    resolve_import_path,
)
from contador_mcp.server import create_server
from contador_mcp.service import ContadorService


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------------------- file guard
@pytest.fixture
def guard(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "ok.csv").write_text("a")
    (tmp_path / "secret.csv").write_text("s")
    return SecurityConfig(import_dirs=(inbox.resolve(),)), inbox, tmp_path


def test_path_guard_allows_inbox_file(guard):
    cfg, inbox, _ = guard
    assert resolve_import_path(cfg, str(inbox / "ok.csv")).name == "ok.csv"
    assert resolve_import_path(cfg, "ok.csv").name == "ok.csv"  # relative -> inbox


@pytest.mark.parametrize("bad", ["../secret.csv", "/etc/passwd", "~/.ssh/id_rsa", "ok.csv\x00.txt"])
def test_path_guard_blocks_traversal_and_outside(guard, bad):
    cfg, *_ = guard
    with pytest.raises(PermissionError):
        resolve_import_path(cfg, bad)


def test_path_guard_blocks_symlink_escape_hidden_and_extension(guard):
    cfg, inbox, root = guard
    os.symlink(root / "secret.csv", inbox / "link.csv")
    with pytest.raises(PermissionError):
        resolve_import_path(cfg, str(inbox / "link.csv"))
    (inbox / ".hidden.csv").write_text("x")
    with pytest.raises(PermissionError):
        resolve_import_path(cfg, str(inbox / ".hidden.csv"))
    (inbox / "x.sh").write_text("x")
    with pytest.raises(PermissionError):
        resolve_import_path(cfg, str(inbox / "x.sh"))


def test_file_paths_disabled_mode(guard):
    cfg, inbox, _ = guard
    with pytest.raises(PermissionError, match="deshabilitada"):
        resolve_import_path(replace(cfg, allow_file_paths=False), str(inbox / "ok.csv"))


def test_kb_tool_cannot_read_arbitrary_files(acme, tmp_path):
    secret = tmp_path / "id_rsa.txt"
    secret.write_text("-----BEGIN PRIVATE KEY-----")
    with pytest.raises(PermissionError):
        acme.knowledge_add_document("x", file_path=str(secret))
    assert not acme.knowledge_search("PRIVATE KEY")["resultados"]


# ----------------------------------------------------------------------------- tokens
def test_static_tokens_are_hashed_and_verified():
    token, digest = generate_token()
    assert token.startswith("cmcp_") and digest == hash_token(token) and token not in digest
    v = HashedStaticTokenVerifier({digest: {"tenant": "est1", "scopes": [SCOPE_READ], "name": "ana"}})
    ok = run(v.verify_token(token))
    assert ok.claims["tenant"] == "est1" and ok.scopes == [SCOPE_READ]
    assert run(v.verify_token(token + "x")) is None


def test_static_tokens_config_rejects_plaintext(monkeypatch, tmp_path):
    from contador_mcp.security import load_security_config

    monkeypatch.setenv("CONTADOR_AUTH_MODE", "static")
    monkeypatch.setenv("CONTADOR_STATIC_TOKENS", json.dumps({"not-a-hash": {"tenant": "x"}}))
    with pytest.raises(ValueError, match="SHA-256"):
        load_security_config(tmp_path)


def test_jwt_mode_requires_issuer_and_audience(monkeypatch, tmp_path):
    from contador_mcp.security import load_security_config

    monkeypatch.setenv("CONTADOR_AUTH_MODE", "jwt")
    monkeypatch.setenv("CONTADOR_JWT_JWKS_URI", "https://idp.example/.well-known/jwks.json")
    with pytest.raises(ValueError, match="ISSUER"):
        load_security_config(tmp_path)


# ----------------------------------------------------------------------------- error masking & audit
def test_internal_errors_are_masked_but_user_errors_are_clear(settings):
    svc = ContadorService(settings, today=TODAY)
    mcp = create_server(service=svc)

    def boom(*a, **k):
        raise RuntimeError("secret internal path /srv/db.sqlite3")

    svc.client_list = boom

    async def scenario():
        async with Client(mcp) as c:
            with pytest.raises(ToolError) as e:
                await c.call_tool("client_list", {})
            assert "secret" not in str(e.value) and "Error interno" in str(e.value)
            with pytest.raises(ToolError, match="no registrado"):
                await c.call_tool("client_get", {"cuit": ACME})

    run(scenario())
    log = svc.audit_log()
    assert {r["tool"] for r in log} == {"client_list", "client_get"}
    assert {r["outcome"] for r in log} == {"error_interno", "error"}


def test_audit_redacts_payloads(acme):
    mcp = create_server(service=acme)

    async def scenario():
        async with Client(mcp) as c:
            await c.call_tool("client_add_note", {"cuit": ACME, "note": "Sueldo del gerente: 9.999.999"})

    run(scenario())
    entry = acme.audit_log(tool="client_add_note")[0]
    assert entry["args"]["cuit"] == ACME
    assert "9.999.999" not in json.dumps(entry)


def test_db_file_permissions(acme):
    mode = os.stat(acme.settings.db_path).st_mode & 0o777
    assert mode == 0o600
    assert os.stat(acme.settings.data_dir).st_mode & 0o777 == 0o700


# ----------------------------------------------------------------------------- real HTTP: auth, scopes, tenants
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def http_server(tmp_path):
    tokens = {}
    conf = {}
    for name, tenant, scopes in [("ana", "estudio-a", [SCOPE_READ, SCOPE_WRITE]),
                                 ("beto", "estudio-b", [SCOPE_READ, SCOPE_WRITE]),
                                 ("junior", "estudio-a", [SCOPE_READ])]:
        tok, dig = generate_token()
        tokens[name] = tok
        conf[dig] = {"tenant": tenant, "scopes": scopes, "name": name}
    port = _free_port()
    env = {**os.environ, "CONTADOR_AUTH_MODE": "static", "CONTADOR_STATIC_TOKENS": json.dumps(conf),
           "CONTADOR_DENSE_MODEL": "none"}
    proc = subprocess.Popen([sys.executable, "-m", "contador_mcp.server", "--transport", "http", "--port", str(port),
                             "--data-dir", str(tmp_path)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    url = f"http://127.0.0.1:{port}/mcp"
    for _ in range(60):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.2)
    yield url, tokens, tmp_path
    proc.terminate()
    proc.wait(timeout=10)


def test_http_requires_auth_and_isolates_tenants(http_server):
    url, tokens, data_dir = http_server

    async def scenario():
        with pytest.raises(Exception):
            async with Client(url) as c:  # no token
                await c.list_tools()
        with pytest.raises(Exception):
            async with Client(url, auth=BearerAuth("cmcp_wrong")) as c:
                await c.list_tools()

        async with Client(url, auth=BearerAuth(tokens["ana"])) as a:
            me = (await a.call_tool("whoami", {})).data
            assert me["tenant"] == "estudio-a" and me["lectura_archivos_servidor"] is False
            await a.call_tool("client_register", {"cuit": ACME, "razon_social": "ACME SA", "tipo_persona": "juridica",
                                                  "condicion_iva": "responsable_inscripto", "iibb_regimen": "local"})
            await a.call_tool("client_add_note", {"cuit": ACME, "note": "dato confidencial zeta-42"})
            with pytest.raises(ToolError, match="deshabilitada"):
                await a.call_tool("import_mis_comprobantes", {"cuit": ACME, "direccion": "recibida",
                                                              "file_path": "/etc/passwd"})
            with pytest.raises(ToolError, match="admin"):
                await a.call_tool("audit_log", {})

        async with Client(url, auth=BearerAuth(tokens["beto"])) as b:  # other estudio sees nothing
            assert (await b.call_tool("client_list", {})).data == []
            hits = (await b.call_tool("knowledge_search", {"query": "zeta-42 confidencial", "cuit": ACME})).data
            assert not any("zeta" in r["texto"] for r in hits["resultados"])

        async with Client(url, auth=BearerAuth(tokens["junior"])) as j:  # same estudio, read-only
            assert len((await j.call_tool("client_list", {})).data) == 1
            with pytest.raises(ToolError, match="contador:write"):
                await j.call_tool("client_add_note", {"cuit": ACME, "note": "x"})

    run(scenario())
    assert (data_dir / "tenants" / "estudio-a" / "contador.sqlite3").exists()
    assert (data_dir / "tenants" / "estudio-b" / "contador.sqlite3").exists()


def test_cli_refuses_public_http_without_auth(tmp_path):
    env = {**os.environ, "CONTADOR_AUTH_MODE": "none", "CONTADOR_DENSE_MODEL": "none"}
    r = subprocess.run([sys.executable, "-m", "contador_mcp.server", "--transport", "http", "--host", "0.0.0.0",
                        "--data-dir", str(tmp_path)], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode != 0 and "requiere autenticación" in r.stderr


def test_http_jwt_mode_validates_issuer_audience_and_tenant(tmp_path):
    from fastmcp.server.auth.providers.jwt import RSAKeyPair

    key = RSAKeyPair.generate()
    iss, aud = "https://idp.estudio.example", "contador-mcp"
    port = _free_port()
    env = {**os.environ, "CONTADOR_AUTH_MODE": "jwt", "CONTADOR_JWT_PUBLIC_KEY": key.public_key,
           "CONTADOR_JWT_ISSUER": iss, "CONTADOR_JWT_AUDIENCE": aud, "CONTADOR_DENSE_MODEL": "none"}
    proc = subprocess.Popen([sys.executable, "-m", "contador_mcp.server", "--transport", "http", "--port", str(port),
                             "--data-dir", str(tmp_path)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}/mcp"
    for _ in range(60):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.2)
    good = key.create_token(subject="ana@estudio", issuer=iss, audience=aud, scopes=[SCOPE_READ],
                            additional_claims={"tenant": "estudio-jwt"})
    wrong_aud = key.create_token(issuer=iss, audience="otra-app", additional_claims={"tenant": "x"})
    no_tenant = key.create_token(issuer=iss, audience=aud, scopes=[SCOPE_READ])
    forged = RSAKeyPair.generate().create_token(issuer=iss, audience=aud, additional_claims={"tenant": "estudio-jwt"})

    async def scenario():
        async with Client(url, auth=BearerAuth(good)) as c:
            me = (await c.call_tool("whoami", {})).data
            assert me["tenant"] == "estudio-jwt" and me["usuario"] == "ana@estudio"
        for bad in (wrong_aud, forged):
            with pytest.raises(Exception):
                async with Client(url, auth=BearerAuth(bad)) as c:
                    await c.list_tools()
        async with Client(url, auth=BearerAuth(no_tenant)) as c:
            with pytest.raises(ToolError, match="tenant"):
                await c.call_tool("client_list", {})

    try:
        run(scenario())
    finally:
        proc.terminate()
        proc.wait(timeout=10)
