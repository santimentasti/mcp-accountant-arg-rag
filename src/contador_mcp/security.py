"""Security layer: authentication, tenant isolation, authorization, audit, safe file access.

Threat model and rationale: see SECURITY.md.
"""

from __future__ import annotations

import base64
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
ROLE_SCOPES = {
    "viewer": frozenset({SCOPE_READ}),
    "member": frozenset({SCOPE_READ, SCOPE_WRITE}),
    "admin": frozenset({SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN}),
}
# Where MCP clients may receive the OAuth code. Restricting this blocks phishing via rogue client registration.
DEFAULT_REDIRECT_URIS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "https://chatgpt.com/connector_platform_oauth_redirect",
    "http://localhost:*",
    "http://127.0.0.1:*",
)


@dataclass(frozen=True)
class OAuthConfig:
    provider: str  # google | oidc | custom
    public_url: str
    client_id: str
    client_secret: str | None
    signing_key: str
    oidc_config_url: str | None = None
    authorize_url: str | None = None
    token_url: str | None = None
    jwks_uri: str | None = None
    public_key: str | None = None
    issuer: str | None = None
    audience: str | None = None
    allowed_redirect_uris: tuple[str, ...] = DEFAULT_REDIRECT_URIS
    access_policy: str = "invite"  # invite | open
    allowed_emails: frozenset[str] = frozenset()
    allowed_domains: frozenset[str] = frozenset()
    admin_emails: frozenset[str] = frozenset()
    members_file: Path | None = None
    support_contact: str = "el administrador del servicio"
    operator_name: str = "contador-mcp"


@dataclass(frozen=True)
class SecurityConfig:
    auth_mode: str = "none"  # none | static | jwt | oauth
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
    oauth: OAuthConfig | None = None

    @property
    def auth_enabled(self) -> bool:
        return self.auth_mode in ("static", "jwt", "oauth")


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
    if mode not in ("none", "static", "jwt", "oauth"):
        raise ValueError("CONTADOR_AUTH_MODE debe ser none, static, jwt u oauth")
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
    oauth = load_oauth_config(data_dir) if mode == "oauth" else None
    return SecurityConfig(
        oauth=oauth,
        auth_mode=mode, static_tokens=static,
        jwt_jwks_uri=env.get("CONTADOR_JWT_JWKS_URI"), jwt_public_key=env.get("CONTADOR_JWT_PUBLIC_KEY"),
        jwt_issuer=env.get("CONTADOR_JWT_ISSUER"), jwt_audience=env.get("CONTADOR_JWT_AUDIENCE"),
        jwt_algorithm=env.get("CONTADOR_JWT_ALGORITHM"),
        tenant_claim=env.get("CONTADOR_TENANT_CLAIM", "tenant"),
        import_dirs=import_dirs, allow_file_paths=allow_files,
        rate_limit_rps=float(env.get("CONTADOR_RATE_LIMIT_RPS", "5")),
        rate_limit_burst=int(env.get("CONTADOR_RATE_LIMIT_BURST", "30")),
    )


def _csv(value: str | None) -> frozenset[str]:
    return frozenset(x.strip().lower() for x in (value or "").split(",") if x.strip())


def _load_or_create_signing_key(data_dir: Path) -> str:
    """Stable secret for FastMCP-issued tokens and encrypted OAuth state; survives restarts."""
    env_key = os.environ.get("CONTADOR_OAUTH_SIGNING_KEY")
    if env_key:
        if len(env_key) < 32:
            raise ValueError("CONTADOR_OAUTH_SIGNING_KEY debe tener al menos 32 caracteres")
        return env_key
    path = data_dir / "oauth-signing.key"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    key = secrets.token_urlsafe(48)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key)
    return key


def load_oauth_config(data_dir: Path) -> OAuthConfig:
    env = os.environ
    provider = env.get("CONTADOR_OAUTH_PROVIDER", "google").strip().lower()
    if provider not in ("google", "oidc", "custom"):
        raise ValueError("CONTADOR_OAUTH_PROVIDER debe ser google, oidc o custom")
    public_url = env.get("CONTADOR_PUBLIC_URL", "").rstrip("/")
    if not public_url:
        raise ValueError("Modo oauth requiere CONTADOR_PUBLIC_URL (ej. https://mcp.tu-dominio.com.ar)")
    if not public_url.startswith("https://") and not re.match(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$", public_url):
        raise ValueError("CONTADOR_PUBLIC_URL debe ser https:// (http solo para localhost)")
    client_id = env.get("CONTADOR_OAUTH_CLIENT_ID")
    if not client_id:
        raise ValueError("Modo oauth requiere CONTADOR_OAUTH_CLIENT_ID (y normalmente CONTADOR_OAUTH_CLIENT_SECRET)")
    policy = env.get("CONTADOR_ACCESS_POLICY", "invite").strip().lower()
    if policy not in ("invite", "open"):
        raise ValueError("CONTADOR_ACCESS_POLICY debe ser invite u open")
    cfg = OAuthConfig(
        provider=provider, public_url=public_url, client_id=client_id,
        client_secret=env.get("CONTADOR_OAUTH_CLIENT_SECRET"),
        signing_key=_load_or_create_signing_key(data_dir),
        oidc_config_url=env.get("CONTADOR_OIDC_CONFIG_URL"),
        authorize_url=env.get("CONTADOR_OAUTH_AUTHORIZE_URL"), token_url=env.get("CONTADOR_OAUTH_TOKEN_URL"),
        jwks_uri=env.get("CONTADOR_OAUTH_JWKS_URI"), public_key=env.get("CONTADOR_OAUTH_PUBLIC_KEY"),
        issuer=env.get("CONTADOR_OAUTH_ISSUER"), audience=env.get("CONTADOR_OAUTH_AUDIENCE"),
        allowed_redirect_uris=tuple(x.strip() for x in env["CONTADOR_OAUTH_REDIRECT_URIS"].split(",") if x.strip())
        if env.get("CONTADOR_OAUTH_REDIRECT_URIS") else DEFAULT_REDIRECT_URIS,
        access_policy=policy,
        allowed_emails=_csv(env.get("CONTADOR_ALLOWED_EMAILS")),
        allowed_domains=_csv(env.get("CONTADOR_ALLOWED_DOMAINS")),
        admin_emails=_csv(env.get("CONTADOR_ADMIN_EMAILS")),
        members_file=data_dir / "members.json",
        support_contact=env.get("CONTADOR_SUPPORT_CONTACT", "el administrador del servicio"),
        operator_name=env.get("CONTADOR_OPERATOR_NAME", "contador-mcp"),
    )
    if provider == "oidc" and not cfg.oidc_config_url:
        raise ValueError("Proveedor oidc requiere CONTADOR_OIDC_CONFIG_URL (.../.well-known/openid-configuration)")
    if provider == "custom" and not (cfg.authorize_url and cfg.token_url and (cfg.jwks_uri or cfg.public_key)
                                     and cfg.issuer):
        raise ValueError("Proveedor custom requiere AUTHORIZE_URL, TOKEN_URL, JWKS_URI o PUBLIC_KEY e ISSUER")
    return cfg


# --------------------------------------------------------------------------- membership (who may use the beta)
class MembershipPolicy:
    """Maps a verified email to (tenant, role).

    Order: members.json (explicit estudio + role) > allowlist/domains/open policy (personal workspace) > deny.
    members.json is re-read when it changes, so invitations need no restart:
      {"members": {"ana@estudio.com": {"tenant": "estudio-perez", "role": "admin"}}}
    """

    def __init__(self, cfg: OAuthConfig):
        self.cfg = cfg
        self._mtime: float | None = None
        self._members: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()

    def _members_now(self) -> dict[str, dict[str, str]]:
        path = self.cfg.members_file
        if not path or not path.exists():
            return {}
        with self._lock:
            mtime = path.stat().st_mtime
            if mtime != self._mtime:
                data = json.loads(path.read_text(encoding="utf-8") or "{}")
                self._members = {k.lower(): v for k, v in (data.get("members") or {}).items()}
                self._mtime = mtime
            return self._members

    @staticmethod
    def personal_tenant(email: str) -> str:
        # No PII in directory names: a stable hash of the email.
        return "u-" + hashlib.sha256(email.encode("utf-8")).hexdigest()[:16]

    def resolve(self, email: str | None, verified: bool | None) -> Principal:
        if not email:
            raise PermissionError("El proveedor de identidad no informó un email")
        if verified is False or (isinstance(verified, str) and verified.lower() != "true"):
            raise PermissionError("El email de la cuenta no está verificado")
        email = email.strip().lower()
        member = self._members_now().get(email)
        if member:
            tenant, role = member.get("tenant", ""), member.get("role", "member")
            if not _TENANT_RE.match(tenant) or role not in ROLE_SCOPES:
                raise PermissionError("Membresía mal configurada; contactar al administrador")
        elif (self.cfg.access_policy == "open" or email in self.cfg.allowed_emails
              or email.rsplit("@", 1)[-1] in self.cfg.allowed_domains or email in self.cfg.admin_emails):
            tenant, role = self.personal_tenant(email), "admin"  # their own sandbox estudio
        else:
            raise PermissionError(f"La cuenta {email} no está invitada a esta beta. "
                                  f"Pedí acceso a {self.cfg.support_contact}.")
        scopes = ROLE_SCOPES["admin" if email in self.cfg.admin_emails else role]
        return Principal(tenant, email, scopes)


def update_members(path: Path, email: str, tenant: str | None = None, role: str = "member",
                   revoke: bool = False) -> dict:
    email = email.strip().lower()
    if "@" not in email:
        raise ValueError("Email inválido")
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"members": {}}
    members = data.setdefault("members", {})
    if revoke:
        members.pop(email, None)
    else:
        tenant = tenant or MembershipPolicy.personal_tenant(email)
        if not _TENANT_RE.match(tenant) or role not in ROLE_SCOPES:
            raise ValueError("tenant o rol inválido (roles: viewer, member, admin)")
        members[email] = {"tenant": tenant, "role": role}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return data


def build_oauth_provider(cfg: OAuthConfig, data_dir: Path):
    from cryptography.fernet import Fernet
    from key_value.aio.stores.filetree import (
        FileTreeStore, FileTreeV1CollectionSanitizationStrategy, FileTreeV1KeySanitizationStrategy,
    )
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

    # Client registrations + upstream tokens live encrypted inside our data volume (persist across restarts).
    storage_dir = data_dir / "oauth-state"
    storage_dir.mkdir(parents=True, exist_ok=True)
    harden_permissions(storage_dir)
    fernet_key = base64.urlsafe_b64encode(hashlib.sha256(("storage:" + cfg.signing_key).encode()).digest())
    storage = FernetEncryptionWrapper(
        key_value=FileTreeStore(
            data_directory=storage_dir,
            key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(storage_dir),
            collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(storage_dir),
        ),
        fernet=Fernet(fernet_key), raise_on_decryption_error=False,
    )
    common = dict(base_url=cfg.public_url, allowed_client_redirect_uris=list(cfg.allowed_redirect_uris),
                  client_storage=storage, jwt_signing_key=cfg.signing_key, require_authorization_consent=True)
    if cfg.provider == "google":
        from fastmcp.server.auth.providers.google import GoogleProvider

        return GoogleProvider(client_id=cfg.client_id, client_secret=cfg.client_secret,
                              required_scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"],
                              **common)
    if cfg.provider == "oidc":
        from fastmcp.server.auth.oidc_proxy import OIDCProxy

        return OIDCProxy(config_url=cfg.oidc_config_url, client_id=cfg.client_id, client_secret=cfg.client_secret,
                         audience=cfg.audience, required_scopes=["openid", "email"], **common)
    from fastmcp.server.auth.oauth_proxy import OAuthProxy
    from fastmcp.server.auth.providers.jwt import JWTVerifier

    verifier = JWTVerifier(public_key=cfg.public_key, jwks_uri=cfg.jwks_uri, issuer=cfg.issuer,
                           audience=cfg.audience, ssrf_safe=bool(cfg.jwks_uri))
    return OAuthProxy(upstream_authorization_endpoint=cfg.authorize_url, upstream_token_endpoint=cfg.token_url,
                      upstream_client_id=cfg.client_id, upstream_client_secret=cfg.client_secret,
                      token_verifier=verifier, valid_scopes=["openid", "email"], **common)


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


def build_auth_provider(cfg: SecurityConfig, data_dir: Path | None = None):
    if cfg.auth_mode == "oauth":
        if cfg.oauth is None or data_dir is None:
            raise ValueError("Configuración oauth incompleta")
        return build_oauth_provider(cfg.oauth, data_dir)
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
    if cfg.auth_mode == "oauth":
        return _membership_for(cfg).resolve(claims.get("email"), claims.get("email_verified"))
    tenant = claims.get(cfg.tenant_claim) or claims.get("tenant")
    if not isinstance(tenant, str) or not _TENANT_RE.match(tenant):
        raise PermissionError(f"El token no tiene un claim '{cfg.tenant_claim}' válido (tenant)")
    scopes = set(token.scopes or [])
    if "scope" in claims and isinstance(claims["scope"], str):
        scopes |= set(claims["scope"].split())
    return Principal(tenant, str(claims.get("sub") or token.client_id), frozenset(scopes))


_POLICIES: dict[int, MembershipPolicy] = {}


def _membership_for(cfg: SecurityConfig) -> MembershipPolicy:
    assert cfg.oauth is not None  # nosec B101 - guarded by auth_mode == "oauth"
    key = id(cfg.oauth)
    if key not in _POLICIES:
        _POLICIES[key] = MembershipPolicy(cfg.oauth)
    return _POLICIES[key]


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

    async def on_list_tools(self, context: MiddlewareContext, call_next):
        try:
            current_principal(self.cfg)  # uninvited users get a clear message at connect time
        except PermissionError as exc:
            from mcp.shared.exceptions import MCPError
            from mcp.types import INVALID_REQUEST

            raise MCPError(INVALID_REQUEST, str(exc)) from None
        return await call_next(context)

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
