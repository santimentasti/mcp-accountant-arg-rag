"""Security layer: authentication, tenant isolation, authorization, audit, safe file access.

Threat model and rationale: see SECURITY.md.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from fastmcp.exceptions import ToolError
from fastmcp.server.auth.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware.middleware import Middleware, MiddlewareContext

log = logging.getLogger("contador_mcp.security")

SCOPE_READ = "contador:read"
SCOPE_WRITE = "contador:write"
SCOPE_ADMIN = "contador:admin"
LOCAL_TENANT = "local"
_TENANT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

ALLOWED_IMPORT_SUFFIXES = {".csv", ".txt", ".md", ".markdown", ".pdf"}
MAX_IMPORT_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGES = 500

# Errors whose message is safe and useful to show to the user/LLM.
EXPECTED_ERRORS: tuple[type[BaseException], ...] = (ValueError, LookupError, FileNotFoundError, PermissionError)


# --------------------------------------------------------------------------- config
@dataclass(frozen=True)
class SecurityConfig:
    auth_mode: str = "none"  # none | static | jwt
    static_tokens: dict[str, dict[str, Any]] = field(default_factory=dict)  # sha256(token) -> {tenant, scopes, name}
    jwt_jwks_uri: str | None = None
    jwt_public_key: str | None = None
    jwt_issuer: str | None = None
    jwt_audience: str | None = None
    jwt_algorithm: str | None = None
    tenant_claim: str = "tenant"
    import_dirs: tuple[Path, ...] = ()
    allow_file_paths: bool = True
    rate_limit_rps: float = 5.0
    rate_limit_burst: int = 30
    max_response_bytes: int = 1_000_000

    @property
    def auth_enabled(self) -> bool:
        return self.auth_mode in ("static", "jwt")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> tuple[str, str]:
    """Return (token, sha256) — the token is shown once, only the hash is stored."""
    token = "cmcp_" + secrets.token_urlsafe(32)
    return token, hash_token(token)


def _parse_static_tokens(raw: str) -> dict[str, dict[str, Any]]:
    """JSON: {"<sha256>": {"tenant": "estudio-perez", "scopes": ["contador:read", ...], "name": "Ana"}}."""
    data = json.loads(raw)
    out: dict[str, dict[str, Any]] = {}
    for digest, info in data.items():
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Las claves de CONTADOR_STATIC_TOKENS deben ser hashes SHA-256 (no tokens en claro)")
        tenant = info.get("tenant")
        if not tenant or not _TENANT_RE.match(tenant):
            raise ValueError(f"tenant inválido en token {digest[:8]}…")
        out[digest] = {"tenant": tenant, "scopes": list(info.get("scopes") or [SCOPE_READ]),
                       "name": info.get("name", tenant)}
    return out


def load_security_config(data_dir: Path, transport: str = "stdio") -> SecurityConfig:
    env = os.environ
    mode = env.get("CONTADOR_AUTH_MODE", "none").strip().lower()
    if mode not in ("none", "static", "jwt"):
        raise ValueError("CONTADOR_AUTH_MODE debe ser none, static o jwt")
    static: dict[str, dict[str, Any]] = {}
    if mode == "static":
        raw = env.get("CONTADOR_STATIC_TOKENS")
        path = env.get("CONTADOR_STATIC_TOKENS_FILE")
        if path:
            raw = Path(path).read_text(encoding="utf-8")
        if not raw:
            raise ValueError("Modo static requiere CONTADOR_STATIC_TOKENS o CONTADOR_STATIC_TOKENS_FILE")
        static = _parse_static_tokens(raw)
    if mode == "jwt" and not (env.get("CONTADOR_JWT_JWKS_URI") or env.get("CONTADOR_JWT_PUBLIC_KEY")):
        raise ValueError("Modo jwt requiere CONTADOR_JWT_JWKS_URI o CONTADOR_JWT_PUBLIC_KEY")
    if mode == "jwt" and not (env.get("CONTADOR_JWT_ISSUER") and env.get("CONTADOR_JWT_AUDIENCE")):
        raise ValueError("Modo jwt requiere CONTADOR_JWT_ISSUER y CONTADOR_JWT_AUDIENCE (evita aceptar tokens de otra app)")

    dirs_env = env.get("CONTADOR_IMPORT_DIRS")
    import_dirs = tuple(Path(p).expanduser().resolve() for p in dirs_env.split(os.pathsep) if p) if dirs_env \
        else ((data_dir / "inbox").resolve(),)
    # Over HTTP the server's filesystem is not the user's: file paths are off unless explicitly enabled.
    allow_files = env.get("CONTADOR_ALLOW_FILE_PATHS", "1" if transport == "stdio" else "0") in ("1", "true", "yes")
    return SecurityConfig(
        auth_mode=mode, static_tokens=static,
        jwt_jwks_uri=env.get("CONTADOR_JWT_JWKS_URI"), jwt_public_key=env.get("CONTADOR_JWT_PUBLIC_KEY"),
        jwt_issuer=env.get("CONTADOR_JWT_ISSUER"), jwt_audience=env.get("CONTADOR_JWT_AUDIENCE"),
        jwt_algorithm=env.get("CONTADOR_JWT_ALGORITHM"),
        tenant_claim=env.get("CONTADOR_TENANT_CLAIM", "tenant"),
        import_dirs=import_dirs, allow_file_paths=allow_files,
        rate_limit_rps=float(env.get("CONTADOR_RATE_LIMIT_RPS", "5")),
        rate_limit_burst=int(env.get("CONTADOR_RATE_LIMIT_BURST", "30")),
    )


# --------------------------------------------------------------------------- authentication
class HashedStaticTokenVerifier(TokenVerifier):
    """Bearer tokens compared by SHA-256 in constant time; plaintext tokens are never stored."""

    def __init__(self, tokens: dict[str, dict[str, Any]]):
        super().__init__()
        self._tokens = tokens

    async def verify_token(self, token: str) -> AccessToken | None:
        digest = hash_token(token)
        match = None
        for known, info in self._tokens.items():  # iterate all: no early exit timing signal
            if hmac.compare_digest(known, digest):
                match = info
        if not match:
            return None
        return AccessToken(token=token, client_id=match["name"], scopes=match["scopes"],
                           claims={"tenant": match["tenant"], "sub": match["name"]})


def build_auth_provider(cfg: SecurityConfig):
    if cfg.auth_mode == "static":
        return HashedStaticTokenVerifier(cfg.static_tokens)
    if cfg.auth_mode == "jwt":
        from fastmcp.server.auth.providers.jwt import JWTVerifier

        return JWTVerifier(public_key=cfg.jwt_public_key, jwks_uri=cfg.jwt_jwks_uri, issuer=cfg.jwt_issuer,
                           audience=cfg.jwt_audience, algorithm=cfg.jwt_algorithm, ssrf_safe=True)
    return None


# --------------------------------------------------------------------------- principal / tenant
@dataclass(frozen=True)
class Principal:
    tenant: str
    subject: str
    scopes: frozenset[str]


def current_principal(cfg: SecurityConfig) -> Principal:
    if not cfg.auth_enabled:
        return Principal(LOCAL_TENANT, "local-user", frozenset({SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN}))
    token = get_access_token()
    if token is None:
        raise PermissionError("No autenticado")
    claims = token.claims or {}
    tenant = claims.get(cfg.tenant_claim) or claims.get("tenant")
    if not isinstance(tenant, str) or not _TENANT_RE.match(tenant):
        raise PermissionError(f"El token no tiene un claim '{cfg.tenant_claim}' válido (tenant)")
    scopes = set(token.scopes or [])
    if "scope" in claims and isinstance(claims["scope"], str):
        scopes |= set(claims["scope"].split())
    return Principal(tenant, str(claims.get("sub") or token.client_id), frozenset(scopes))


def tenant_dir(base: Path, tenant: str) -> Path:
    if not _TENANT_RE.match(tenant):
        raise PermissionError("tenant inválido")
    path = (base / "tenants" / tenant).resolve()
    if base.resolve() not in path.parents:
        raise PermissionError("tenant inválido")
    return path


class TenantRegistry:
    """One isolated ContadorService (own SQLite + KB) per tenant, created lazily."""

    def __init__(self, cfg: SecurityConfig, factory: Callable[[str], Any]):
        self.cfg = cfg
        self._factory = factory
        self._services: dict[str, Any] = {}
        self._lock = threading.Lock()

    def get(self, tenant: str) -> Any:
        with self._lock:
            if tenant not in self._services:
                self._services[tenant] = self._factory(tenant)
            return self._services[tenant]

    def current(self) -> Any:
        return self.get(current_principal(self.cfg).tenant)


# --------------------------------------------------------------------------- safe file access
def resolve_import_path(cfg: SecurityConfig, raw: str) -> Path:
    """Only regular, non-hidden files with an allowed extension inside an allowed directory."""
    if not cfg.allow_file_paths:
        raise PermissionError("La lectura de archivos del servidor está deshabilitada en este despliegue: "
                              "enviar el contenido como texto (csv_text / content).")
    if "\x00" in raw:
        raise PermissionError("Ruta inválida")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = cfg.import_dirs[0] / path
    resolved = path.resolve()  # follows symlinks: a link pointing outside is rejected below
    if not any(d == resolved.parent or d in resolved.parents for d in cfg.import_dirs):
        allowed = ", ".join(str(d) for d in cfg.import_dirs)
        raise PermissionError(f"Ruta fuera de los directorios permitidos ({allowed}). Configurar CONTADOR_IMPORT_DIRS.")
    if any(part.startswith(".") for part in resolved.parts[1:]):
        raise PermissionError("No se permiten archivos o directorios ocultos")
    if resolved.suffix.lower() not in ALLOWED_IMPORT_SUFFIXES:
        raise PermissionError(f"Extensión no permitida: {resolved.suffix} ({', '.join(sorted(ALLOWED_IMPORT_SUFFIXES))})")
    if not resolved.is_file():
        raise FileNotFoundError(f"No existe el archivo: {resolved.name}")
    if resolved.stat().st_size > MAX_IMPORT_BYTES:
        raise PermissionError(f"Archivo demasiado grande (máx. {MAX_IMPORT_BYTES // (1024 * 1024)} MB)")
    return resolved


def harden_permissions(path: Path) -> None:
    """Data dirs 0700, files 0600 (best effort; no-op on platforms without POSIX modes)."""
    try:
        if path.is_dir():
            os.chmod(path, 0o700)
        elif path.exists():
            os.chmod(path, 0o600)
    except OSError:  # pragma: no cover
        pass


# --------------------------------------------------------------------------- middleware
def _redact_args(args: dict[str, Any]) -> dict[str, Any]:
    """Audit keeps *what* was touched (tool, CUIT, period), not the payload (CSV, notes, amounts)."""
    keep = {"cuit", "month", "periodo", "obligacion", "direccion", "document_id", "concepto", "days", "tipo"}
    out: dict[str, Any] = {}
    for k, v in (args or {}).items():
        if k in keep:
            out[k] = v
        elif isinstance(v, str):
            out[k] = f"<str len={len(v)}>"
        else:
            out[k] = f"<{type(v).__name__}>"
    return out


class SecurityMiddleware(Middleware):
    """Authorization by scope, audit trail per tenant, and safe error messages."""

    def __init__(self, cfg: SecurityConfig, registry: TenantRegistry, write_tools: set[str], admin_tools: set[str]):
        self.cfg = cfg
        self.registry = registry
        self.write_tools = write_tools
        self.admin_tools = admin_tools

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        name = context.message.name
        args = context.message.arguments or {}
        try:
            principal = current_principal(self.cfg)
        except PermissionError as exc:
            raise ToolError(str(exc)) from None
        needed = SCOPE_ADMIN if name in self.admin_tools else SCOPE_WRITE if name in self.write_tools else SCOPE_READ
        # admin implies write implies read
        granted = set(principal.scopes)
        if SCOPE_ADMIN in granted:
            granted |= {SCOPE_WRITE, SCOPE_READ}
        if SCOPE_WRITE in granted:
            granted |= {SCOPE_READ}
        start = time.perf_counter()
        outcome = "ok"
        try:
            if needed not in granted:
                outcome = "denegado"
                raise ToolError(f"Permiso insuficiente: '{name}' requiere el scope {needed}")
            return await call_next(context)
        except ToolError as exc:
            if outcome == "ok":
                outcome = "error"
            cause = exc.__cause__
            if isinstance(cause, EXPECTED_ERRORS):
                raise ToolError(str(cause)) from None
            if cause is not None and not isinstance(cause, ToolError):
                outcome = "error_interno"
                raise ToolError(f"Error interno al ejecutar '{name}'. Ver el log del servidor.") from None
            raise
        except EXPECTED_ERRORS as exc:
            outcome = "error"
            raise ToolError(str(exc)) from None
        except Exception:
            outcome = "error_interno"
            log.exception("Error interno en %s", name)
            raise ToolError(f"Error interno al ejecutar '{name}'. Ver el log del servidor.") from None
        finally:
            try:
                svc = self.registry.get(principal.tenant)
                svc.audit(principal.subject, name, _redact_args(args), outcome,
                          round((time.perf_counter() - start) * 1000))
            except Exception:  # auditing must never break the request
                log.exception("No se pudo registrar auditoría")
