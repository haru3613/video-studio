#!/usr/bin/env python3
"""Authenticated Streamable HTTP gateway for the installed Video Studio MCP.

The HTTP process is a policy enforcing adapter.  It does not execute pipeline
commands itself.  Each authenticated MCP session gets an SDK ClientSession to
the promoted, owner-controlled ``video-studio-mcp`` stdio server.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import contextlib
import hashlib
import json
import math
import os
import re
import stat
import sys
import time
import tomllib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
import jwt
import uvicorn
from jwt import PyJWK
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.routes import create_protected_resource_routes
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl
from starlette.routing import Route

try:
    from . import artifact_intake
except ImportError:  # direct script execution
    import artifact_intake


READ_SCOPE = "studio:read"
EXECUTE_SCOPE = "studio:execute"
REVIEW_SCOPE = "studio:review"
ALL_SCOPES = (READ_SCOPE, EXECUTE_SCOPE, REVIEW_SCOPE)

# This allowlist is deliberately closed.  Publish, upload, thumbnail replacement,
# publish approval, and runtime trust operations never enter the HTTP surface.
TOOL_SCOPES: dict[str, str] = {
    "workspace_info": READ_SCOPE,
    "project_list": READ_SCOPE,
    "create": EXECUTE_SCOPE,
    "select": READ_SCOPE,
    "status": READ_SCOPE,
    "artifact_index": READ_SCOPE,
    "record_selection": EXECUTE_SCOPE,
    "lease_claim": EXECUTE_SCOPE,
    "lease_renew": EXECUTE_SCOPE,
    "lease_status": READ_SCOPE,
    "lease_release": EXECUTE_SCOPE,
    "run_next": EXECUTE_SCOPE,
    "verify": READ_SCOPE,
    "delivery_status": READ_SCOPE,
    "export_delivery": EXECUTE_SCOPE,
    "job_status": READ_SCOPE,
    "job_logs": READ_SCOPE,
    "job_cancel": EXECUTE_SCOPE,
    "job_resume": EXECUTE_SCOPE,
    "review_feedback": READ_SCOPE,
    "review_add": REVIEW_SCOPE,
    "review_resolve": REVIEW_SCOPE,
    "artifact_stage": EXECUTE_SCOPE,
    "artifact_import": EXECUTE_SCOPE,
    "produce_staged_artifact": EXECUTE_SCOPE,
    "visual_qa": REVIEW_SCOPE,
    "pronunciation_review": REVIEW_SCOPE,
}

PUBLISHING_TOOLS = frozenset(
    {
        "approve_publish",
        "prepare_publish_approval",
        "prepare_publish",
        "publish",
        "replace_thumbnail",
        "reconcile_upload",
    }
)

RUNNERS_REQUIRING_MEDIA_TOOLS = frozenset(
    {
        "render-project",
        "generate-cover",
        "analyze-pronunciation",
        "confirm-pronunciation",
        "generate-narration",
    }
)

# Operator-owned provider/runtime settings needed by the reviewed core runners.
# The SDK's baseline remains authoritative for process basics; everything else
# is dropped rather than inheriting the server's entire environment.
BACKEND_ENV_ALLOWLIST = (
    "VIDEO_STUDIO_DELIVERY_ROOT",
    "ELEVENLABS_API_KEY",
    "ELEVENLABS_API_KEY_PATH",
    "VIDEO_STUDIO_TTS_VOICE_ID",
    "VIDEO_STUDIO_TTS_MODEL",
    "VIDEO_STUDIO_TTS_MAX_CREDITS",
    "VIDEO_STUDIO_TTS_SPEND_JOURNAL",
    "VIDEO_STUDIO_TTS_PYTHON",
    "HARU_TTS_PYTHON",
    "VIDEO_STUDIO_G2PW_PYTHON",
    "VIDEO_STUDIO_G2PW_MODEL_DIR",
    "VIDEO_STUDIO_G2PW_BERT_MODEL",
    "HARU_G2PW_PYTHON",
    "HARU_G2PW_MODEL_DIR",
    "HARU_G2PW_BERT_MODEL",
    "VIDEO_STUDIO_VOICE_RULES_DIR",
    "VIDEO_STUDIO_COVER_ASSET_DIR",
    "VIDEO_STUDIO_CHROMIUM",
)

ASYMMETRIC_JWT_ALGORITHMS = frozenset(
    {
        "RS256",
        "RS384",
        "RS512",
        "PS256",
        "PS384",
        "PS512",
        "ES256",
        "ES384",
        "ES512",
        "EdDSA",
    }
)

_SLUG = re.compile(r"^[a-z](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_COMMENT_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STAGE_ID = re.compile(r"^[0-9a-f]{32}$")
_PUBLIC_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
INLINE_TEXT_LIMIT = 1024 * 1024
STAGED_PRODUCE_TARGETS = frozenset(
    {
        "script-proposal.md",
        "sources.md",
        "issue_brief.md",
        "storyboard-final-timed.json",
        "editorial-contract.json",
        "claims.json",
        "publish-metadata.json",
    }
)
NO_PROJECT_TOOLS = frozenset({"workspace_info", "project_list"})
ROOT_ARGUMENTS = frozenset(
    {"workspace_root", "projects_root", "project_root", "tools_root", "source_file"}
)


class ConfigurationError(ValueError):
    """The operator-owned configuration is invalid."""


class GatewayPolicyError(ValueError):
    """An HTTP tool call crossed the configured trust boundary."""


@dataclass(frozen=True)
class HttpMcpConfig:
    host: str
    port: int
    resource_url: str
    issuer: str
    audience: str
    jwks_url: str
    algorithms: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    workspace_root: Path
    projects_root: Path
    media_tools_root: Path
    backend_command: Path
    jwks_cache_seconds: int = 300
    jwks_min_refresh_seconds: int = 10
    jwks_max_keys: int = 32
    jwks_max_bytes: int = 262_144
    clock_skew_seconds: int = 30
    max_request_body_size: int = 2_097_152
    session_idle_timeout_seconds: int = 900
    max_sessions: int = 100


class BackendSession(Protocol):
    async def list_tools(self) -> types.ListToolsResult: ...

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> types.CallToolResult: ...


@dataclass
class GatewaySession:
    backend: BackendSession
    tools: dict[str, types.Tool]
    public_tools: dict[str, types.Tool]


BackendConnector = Callable[[HttpMcpConfig], contextlib.AbstractAsyncContextManager[BackendSession]]


def _require_mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{name} must be a TOML table")
    return value


def _require_string(table: dict[str, Any], key: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{key} must be a non-empty string")
    return value.strip()


def _require_int(table: dict[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigurationError(f"{key} must be an integer from {low} through {high}")
    return value


def _https_url(value: str, name: str, *, allow_path: bool = True) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (not allow_path and parsed.path not in ("", "/"))
    ):
        raise ConfigurationError(f"{name} must be an HTTPS URL without credentials or a fragment")
    return value.rstrip("/") if parsed.path in ("", "/") else value


def _origin(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme not in ("https", "http")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError("allowed_origins entries must be URL origins")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ConfigurationError("plain HTTP origins are allowed only for loopback")
    return f"{parsed.scheme}://{parsed.netloc}"


def _secure_config_file(path: Path) -> Path:
    if not path.is_absolute():
        raise ConfigurationError("config path must be absolute")
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ConfigurationError("config file is not readable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ConfigurationError("config file must be a regular file, not a symlink")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ConfigurationError("config file must be owned by the server user")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ConfigurationError("config file must not be group- or world-writable")
    return path.resolve(strict=True)


def _configured_directory(value: str, name: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise ConfigurationError(f"{name} must be an absolute path without '..'")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ConfigurationError(f"{name} must be an existing directory") from error
    if stat.S_ISLNK(metadata.st_mode) or not resolved.is_dir():
        raise ConfigurationError(f"{name} must be a real directory, not a symlink")
    return resolved


def _trusted_executable(path: Path) -> Path:
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ConfigurationError(
            "the promoted ~/.local/share/video-studio/bin/video-studio-mcp executable is required"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ConfigurationError("video-studio-mcp must be a regular file, not a symlink")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise ConfigurationError("video-studio-mcp must be owned by the server user")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or not os.access(resolved, os.X_OK):
        raise ConfigurationError("video-studio-mcp permissions are unsafe or not executable")
    return resolved


def load_config(path: Path, *, repo_root: Path | None = None) -> HttpMcpConfig:
    """Load and validate the fixed trust configuration.

    The backend executable and bundled media tools are intentionally not TOML
    options.  A remote caller and a configuration typo cannot select a new
    executable trust root.
    """

    config_path = _secure_config_file(path)
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError("config file is not valid UTF-8 TOML") from error

    server = _require_mapping(raw.get("server"), "server")
    oauth = _require_mapping(raw.get("oauth"), "oauth")
    paths = _require_mapping(raw.get("paths"), "paths")

    host = _require_string(server, "host")
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ConfigurationError("server.host must be loopback")
    port = _require_int(server, "port", 8765, 1, 65535)

    resource_url = _https_url(_require_string(server, "resource_url"), "resource_url")
    resource = urlparse(resource_url)
    if not resource.path or resource.path == "/" or resource.query:
        raise ConfigurationError("resource_url must include a query-free MCP endpoint path")

    origins_value = server.get("allowed_origins", [])
    if not isinstance(origins_value, list) or not all(isinstance(item, str) for item in origins_value):
        raise ConfigurationError("allowed_origins must be an array of origins")
    allowed_origins = tuple(dict.fromkeys(_origin(item) for item in origins_value))

    hosts_value = server.get("allowed_hosts", [])
    if not isinstance(hosts_value, list) or not all(
        isinstance(item, str) and item and "/" not in item and "@" not in item
        for item in hosts_value
    ):
        raise ConfigurationError("allowed_hosts must be an array of Host header values")
    derived_hosts = [resource.netloc, "127.0.0.1:*", "[::1]:*", "localhost:*"]
    allowed_hosts = tuple(dict.fromkeys([*derived_hosts, *hosts_value]))

    issuer = _https_url(_require_string(oauth, "issuer"), "issuer")
    audience = _require_string(oauth, "audience")
    jwks_url = _https_url(_require_string(oauth, "jwks_url"), "jwks_url")
    algorithms_value = oauth.get("algorithms", ["RS256"])
    if not isinstance(algorithms_value, list) or not algorithms_value:
        raise ConfigurationError("algorithms must be a non-empty array")
    algorithms = tuple(dict.fromkeys(algorithms_value))
    if not all(isinstance(item, str) and item in ASYMMETRIC_JWT_ALGORITHMS for item in algorithms):
        raise ConfigurationError("algorithms may contain only supported asymmetric JWT algorithms")

    workspace_root = _configured_directory(_require_string(paths, "workspace_root"), "workspace_root")
    projects_root = _configured_directory(str(workspace_root / "projects"), "workspace projects directory")
    _configured_directory(str(workspace_root / "inbox"), "workspace inbox directory")
    _configured_directory(str(workspace_root / ".video-studio"), "workspace state directory")
    manifest_path = workspace_root / "workspace.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ConfigurationError("workspace_root must contain a direct workspace.json")
    try:
        workspace_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ConfigurationError("workspace.json is not valid UTF-8 JSON") from error
    if not isinstance(workspace_manifest, dict) or workspace_manifest.get("schema") != "video_studio.workspace.v1":
        raise ConfigurationError("workspace.json has an unsupported schema")
    try:
        artifact_intake.workspace_info(workspace_root)
    except (artifact_intake.IntakeError, OSError, TypeError, ValueError) as error:
        raise ConfigurationError("workspace_root is not a valid Video Studio workspace") from error

    root = (repo_root or Path(__file__).resolve().parents[1]).resolve(strict=True)
    media_tools_root = _configured_directory(str(root / "media-tools"), "bundled media-tools root")
    backend_command = _trusted_executable(
        Path.home() / ".local/share/video-studio/bin/video-studio-mcp"
    )

    cache_seconds = _require_int(oauth, "jwks_cache_seconds", 300, 30, 86_400)
    min_refresh = _require_int(oauth, "jwks_min_refresh_seconds", 10, 1, 300)
    if min_refresh > cache_seconds:
        raise ConfigurationError("jwks_min_refresh_seconds cannot exceed jwks_cache_seconds")

    return HttpMcpConfig(
        host=host,
        port=port,
        resource_url=resource_url,
        issuer=issuer,
        audience=audience,
        jwks_url=jwks_url,
        algorithms=algorithms,
        allowed_origins=allowed_origins,
        allowed_hosts=allowed_hosts,
        workspace_root=workspace_root,
        projects_root=projects_root,
        media_tools_root=media_tools_root,
        backend_command=backend_command,
        jwks_cache_seconds=cache_seconds,
        jwks_min_refresh_seconds=min_refresh,
        jwks_max_keys=_require_int(oauth, "jwks_max_keys", 32, 1, 128),
        jwks_max_bytes=_require_int(oauth, "jwks_max_bytes", 262_144, 4_096, 1_048_576),
        clock_skew_seconds=_require_int(oauth, "clock_skew_seconds", 30, 0, 300),
        max_request_body_size=_require_int(
            server, "max_request_body_size", 2_097_152, 16_384, 4_194_304
        ),
        session_idle_timeout_seconds=_require_int(
            server, "session_idle_timeout_seconds", 900, 30, 86_400
        ),
        max_sessions=_require_int(server, "max_sessions", 100, 1, 10_000),
    )


class JwksTokenVerifier(TokenVerifier):
    """Validate OAuth access-token JWTs with a bounded, fail-closed JWKS cache."""

    def __init__(self, config: HttpMcpConfig, http_client: httpx.AsyncClient | None = None):
        self.config = config
        self._client = http_client or httpx.AsyncClient(timeout=5.0, follow_redirects=False)
        self._owns_client = http_client is None
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at = 0.0
        self._last_attempt = 0.0
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _download(self) -> dict[str, PyJWK]:
        content = bytearray()
        async with self._client.stream(
            "GET", self.config.jwks_url, headers={"Accept": "application/json"}
        ) as response:
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared and int(declared) > self.config.jwks_max_bytes:
                raise ValueError("JWKS document is too large")
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > self.config.jwks_max_bytes:
                    raise ValueError("JWKS document is too large")
                content.extend(chunk)
        document = json.loads(content)
        entries = document.get("keys") if isinstance(document, dict) else None
        if not isinstance(entries, list) or not entries or len(entries) > self.config.jwks_max_keys:
            raise ValueError("JWKS key set is empty or exceeds the configured bound")

        keys: dict[str, PyJWK] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("JWKS contains a malformed key")
            kid = entry.get("kid")
            if not isinstance(kid, str) or not kid or len(kid) > 256 or kid in keys:
                raise ValueError("JWKS keys require unique bounded kid values")
            if entry.get("use") not in (None, "sig"):
                continue
            key_ops = entry.get("key_ops")
            if key_ops is not None and (
                not isinstance(key_ops, list) or "verify" not in key_ops
            ):
                continue
            key = PyJWK.from_dict(entry)
            if key.algorithm_name not in self.config.algorithms:
                continue
            keys[kid] = key
        if not keys:
            raise ValueError("JWKS contains no allowed signature keys")
        return keys

    async def _refresh(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self._keys and now - self._fetched_at < self.config.jwks_cache_seconds:
            return
        async with self._lock:
            now = time.monotonic()
            if not force and self._keys and now - self._fetched_at < self.config.jwks_cache_seconds:
                return
            if force and now - self._last_attempt < self.config.jwks_min_refresh_seconds:
                return
            self._last_attempt = now
            new_keys = await self._download()
            self._keys = new_keys
            self._fetched_at = time.monotonic()

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            algorithm = header.get("alg")
            if (
                not isinstance(kid, str)
                or not kid
                or not isinstance(algorithm, str)
                or algorithm not in self.config.algorithms
            ):
                return None

            await self._refresh()
            key = self._keys.get(kid)
            if key is None:
                await self._refresh(force=True)
                key = self._keys.get(kid)
            if key is None or key.algorithm_name != algorithm:
                return None

            claims = jwt.decode(
                token,
                key=key.key,
                algorithms=[algorithm],
                issuer=self.config.issuer,
                audience=self.config.audience,
                leeway=self.config.clock_skew_seconds,
                options={"require": ["exp", "iss", "aud"]},
            )
            if not isinstance(claims, dict):
                return None

            client_id = claims.get("client_id") or claims.get("azp") or claims.get("sub")
            subject = claims.get("sub")
            if not isinstance(client_id, str) or not client_id or len(client_id) > 512:
                return None
            if subject is not None and (not isinstance(subject, str) or not subject or len(subject) > 512):
                return None

            raw_scopes = claims.get("scope", claims.get("scp", ""))
            if isinstance(raw_scopes, str):
                scopes = raw_scopes.split()
            elif isinstance(raw_scopes, list) and all(isinstance(item, str) for item in raw_scopes):
                scopes = raw_scopes
            else:
                return None
            if len(scopes) > 64 or any(not item or len(item) > 256 for item in scopes):
                return None
            scopes = list(dict.fromkeys(scopes))

            audience = claims.get("aud")
            resources = [audience] if isinstance(audience, str) else audience
            resource = self.config.resource_url if self.config.resource_url in (resources or []) else None
            return AccessToken(
                token=token,
                client_id=client_id,
                scopes=scopes,
                expires_at=int(claims["exp"]),
                resource=resource,
                subject=subject,
                claims={"iss": claims["iss"]},
            )
        except (jwt.PyJWTError, httpx.HTTPError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None


@contextlib.asynccontextmanager
async def stdio_backend(config: HttpMcpConfig) -> AsyncIterator[BackendSession]:
    environment = get_default_environment()
    for name in BACKEND_ENV_ALLOWLIST:
        value = os.environ.get(name)
        if value and not value.startswith("()"):
            environment[name] = value
    parameters = StdioServerParameters(
        command=str(config.backend_command), args=[], env=environment
    )
    async with stdio_client(parameters, errlog=sys.stderr) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def _token_from_request(server: FastMCP[Any]) -> AccessToken:
    try:
        request = server._mcp_server.request_context.request  # noqa: SLF001 - SDK request context
    except LookupError as error:
        raise GatewayPolicyError("authenticated request context is unavailable") from error
    user = request.scope.get("user") if request is not None else None
    if not isinstance(user, AuthenticatedUser):
        raise GatewayPolicyError("authentication is required")
    return user.access_token


def _principal_owner(token: AccessToken) -> str:
    issuer = str((token.claims or {}).get("iss", ""))
    identity = {"iss": issuer, "sub": token.subject or "", "client_id": token.client_id}
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"http:{digest[:40]}"


def _principal_idempotency_key(token: AccessToken, value: object) -> str:
    if not isinstance(value, str) or not _IDEMPOTENCY_KEY.fullmatch(value):
        raise GatewayPolicyError("idempotency_key must be a bounded opaque identifier")
    principal = _principal_owner(token).removeprefix("http:")
    key_digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:40]
    return f"http_{principal}_{key_digest}"


def _require_no_symlink_components(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise GatewayPolicyError("path is outside the configured root") from error
    current = root
    for part in relative.parts:
        if part in ("", ".", ".."):
            raise GatewayPolicyError("path contains an unsafe component")
        current = current / part
        try:
            if current.is_symlink():
                raise GatewayPolicyError("symlink paths are not accepted over HTTP")
        except OSError as error:
            raise GatewayPolicyError("path cannot be inspected") from error


def _project_path(value: object, config: HttpMcpConfig) -> Path:
    if not isinstance(value, str) or not value:
        raise GatewayPolicyError("project_root must be a non-empty absolute path")
    raw = Path(value)
    if not raw.is_absolute() or ".." in raw.parts:
        raise GatewayPolicyError("project_root must be absolute and cannot contain '..'")
    _require_no_symlink_components(config.projects_root, raw)
    try:
        resolved = raw.resolve(strict=True)
    except OSError as error:
        raise GatewayPolicyError("project_root must be an existing project directory") from error
    if not resolved.is_dir() or resolved.parent != config.projects_root:
        raise GatewayPolicyError("project_root must be a direct child of the configured projects root")
    return resolved


def _project_id_path(value: object, config: HttpMcpConfig) -> Path:
    if not isinstance(value, str) or not _SLUG.fullmatch(value):
        raise GatewayPolicyError("project_id must be a canonical lowercase slug")
    try:
        project = artifact_intake.resolve_project(config.workspace_root, value)
    except (artifact_intake.IntakeError, OSError, TypeError, ValueError) as error:
        raise GatewayPolicyError("project_id does not name a direct workspace project") from error
    return _project_path(str(project), config)


def _projects_path(value: object, config: HttpMcpConfig) -> Path:
    if not isinstance(value, str) or not value:
        raise GatewayPolicyError("projects_root must be supplied")
    raw = Path(value)
    if not raw.is_absolute() or ".." in raw.parts or raw.is_symlink():
        raise GatewayPolicyError("projects_root is not the configured projects root")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as error:
        raise GatewayPolicyError("projects_root is unavailable") from error
    if resolved != config.projects_root:
        raise GatewayPolicyError("projects_root is not the configured projects root")
    return resolved


def _staged_source(value: object, project: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise GatewayPolicyError("source_file must be supplied")
    raw = Path(value)
    staging = project / ".hvp" / "staging"
    if not raw.is_absolute() or ".." in raw.parts:
        raise GatewayPolicyError("source_file must be an absolute staged path")
    _require_no_symlink_components(staging, raw)
    try:
        resolved = raw.resolve(strict=True)
    except OSError as error:
        raise GatewayPolicyError("source_file must be an existing staged file") from error
    if not resolved.is_file() or not resolved.is_relative_to(staging):
        raise GatewayPolicyError("source_file must be inside the project's staging directory")
    return resolved


def _safe_artifact(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise GatewayPolicyError("artifact must be a project-relative path")
    path = Path(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise GatewayPolicyError("artifact must be a safe project-relative path")
    return value


def _safe_inbox_reference(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or "\\" in value
        or "\x00" in value
    ):
        raise GatewayPolicyError("inbox_path must be a bounded relative path")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise GatewayPolicyError("inbox_path must be a bounded relative path")
    return value


def _public_input_schema(name: str, backend_schema: dict[str, Any]) -> dict[str, Any]:
    """Project the stdio schema into the fixed-workspace HTTP contract."""

    schema = copy.deepcopy(backend_schema)
    properties = schema.setdefault("properties", {})
    required = list(schema.get("required", []))

    project_schema = properties.pop("project_root", None)
    for field in ROOT_ARGUMENTS:
        properties.pop(field, None)
        required = [item for item in required if item != field]

    if name in {"create", "select"}:
        properties.pop("project", None)
        required = [item for item in required if item != "project"]
        project_schema = {"type": "string"}

    if name not in NO_PROJECT_TOOLS:
        public_project = copy.deepcopy(project_schema or {"type": "string"})
        public_project.update(
            {
                "type": "string",
                "pattern": _SLUG.pattern,
                "description": "Project identifier in the server's configured workspace.",
            }
        )
        properties["project_id"] = public_project
        if "project_id" not in required:
            required.append("project_id")

    if name == "artifact_stage":
        properties.setdefault("inbox_path", {"type": ["string", "null"]})
        properties.setdefault("inline_text", {"type": ["string", "null"]})
        properties["inbox_path"].update({"maxLength": 1024})
        properties["inline_text"].update({"maxLength": INLINE_TEXT_LIMIT})
        schema["oneOf"] = [
            {"required": ["inbox_path"], "not": {"required": ["inline_text"]}},
            {"required": ["inline_text"], "not": {"required": ["inbox_path"]}},
        ]
    if name in {"artifact_import", "produce_staged_artifact"}:
        properties.setdefault("stage_id", {"type": "string"})
        properties["stage_id"].update({"pattern": _STAGE_ID.pattern})

    schema["required"] = required
    schema["properties"] = properties
    schema["additionalProperties"] = False
    return schema


def _public_tool(name: str, tool: types.Tool, scope: str) -> types.Tool:
    description = (tool.description or "").rstrip()
    return tool.model_copy(
        update={
            "description": f"{description} Requires OAuth scope `{scope}`.",
            "inputSchema": _public_input_schema(name, tool.inputSchema),
        }
    )


def _backend_arguments(
    name: str,
    arguments: dict[str, Any],
    config: HttpMcpConfig,
) -> dict[str, Any]:
    if any(field in arguments for field in ROOT_ARGUMENTS):
        raise GatewayPolicyError("server filesystem roots are not accepted over HTTP")
    converted = dict(arguments)
    project_id = converted.pop("project_id", None)
    if name in NO_PROJECT_TOOLS:
        if project_id is not None:
            raise GatewayPolicyError("this tool does not accept project_id")
        converted["workspace_root"] = str(config.workspace_root)
        return converted

    if name == "create":
        if not isinstance(project_id, str) or not _SLUG.fullmatch(project_id):
            raise GatewayPolicyError("project_id must be a canonical lowercase slug")
        converted["projects_root"] = str(config.projects_root)
        converted["project"] = project_id
        return converted
    if name == "select":
        project = _project_id_path(project_id, config)
        converted["projects_root"] = str(config.projects_root)
        converted["project"] = project.name
        return converted

    converted["project_root"] = str(_project_id_path(project_id, config))
    return converted


def enforce_arguments(
    tool_name: str, arguments: dict[str, Any], config: HttpMcpConfig, token: AccessToken
) -> dict[str, Any]:
    """Copy and constrain backend arguments before they cross into stdio."""

    constrained = dict(arguments)
    if "projects_root" in constrained:
        constrained["projects_root"] = str(_projects_path(constrained["projects_root"], config))

    project: Path | None = None
    if "project_root" in constrained:
        project = _project_path(constrained["project_root"], config)
        constrained["project_root"] = str(project)

    if tool_name in {"create", "select"}:
        project_name = constrained.get("project")
        if not isinstance(project_name, str) or not _SLUG.fullmatch(project_name):
            raise GatewayPolicyError("project must be a canonical lowercase slug")

    # The HTTP lease identity is derived exclusively from authenticated claims.
    # The field stays in the wire schema for stdio/HTTP compatibility, but no
    # caller-selected value reaches the lease authority.
    if "owner" in constrained:
        constrained["owner"] = _principal_owner(token)

    if tool_name == "run_next":
        runner = constrained.get("runner")
        if not isinstance(runner, str) or not runner:
            raise GatewayPolicyError("runner must be supplied")
        supplied_tools = constrained.get("tools_root")
        if runner in RUNNERS_REQUIRING_MEDIA_TOOLS:
            if supplied_tools is not None:
                if not isinstance(supplied_tools, str):
                    raise GatewayPolicyError("tools_root must be a path string")
                try:
                    supplied = Path(supplied_tools).resolve(strict=True)
                except OSError as error:
                    raise GatewayPolicyError("tools_root is unavailable") from error
                if Path(supplied_tools).is_symlink() or supplied != config.media_tools_root:
                    raise GatewayPolicyError("tools_root cannot select caller-controlled tools")
            constrained["tools_root"] = str(config.media_tools_root)
        elif supplied_tools is not None:
            raise GatewayPolicyError("this runner does not accept tools_root over HTTP")

    if tool_name == "produce_artifact":
        if project is None:
            raise GatewayPolicyError("project_root is required")
        constrained["source_file"] = str(_staged_source(constrained.get("source_file"), project))
        constrained["artifact"] = _safe_artifact(constrained.get("artifact"))

    if tool_name == "export_delivery" and "destination" in constrained:
        raise GatewayPolicyError(
            "export destination is server-configured and cannot be supplied over HTTP"
        )

    if tool_name in {"job_status", "job_logs", "job_cancel", "job_resume"}:
        job_id = constrained.get("job_id")
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            raise GatewayPolicyError("job_id must be exactly 32 lowercase hexadecimal characters")
    if tool_name == "job_logs" and "max_bytes" in constrained:
        max_bytes = constrained["max_bytes"]
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or not 1 <= max_bytes <= 65_536
        ):
            raise GatewayPolicyError("max_bytes must be an integer from 1 through 65536")

    if tool_name == "review_resolve":
        comment_id = constrained.get("comment_id")
        package_id = constrained.get("expected_package_id")
        asset_sha256 = constrained.get("expected_asset_sha256")
        if not isinstance(comment_id, str) or not _COMMENT_ID.fullmatch(comment_id):
            raise GatewayPolicyError("comment_id must be a canonical lowercase UUID")
        if not isinstance(package_id, str) or not _SHA256.fullmatch(package_id):
            raise GatewayPolicyError("expected_package_id must be a lowercase SHA-256 digest")
        if not isinstance(asset_sha256, str) or not _SHA256.fullmatch(asset_sha256):
            raise GatewayPolicyError("expected_asset_sha256 must be a lowercase SHA-256 digest")
        if constrained.get("status") not in {"open", "resolved"}:
            raise GatewayPolicyError("review status must be open or resolved")
        constrained["idempotency_key"] = _principal_idempotency_key(
            token, constrained.get("idempotency_key")
        )

    if tool_name == "review_add":
        client_id = constrained.get("client_id")
        package_id = constrained.get("package_id")
        asset_id = constrained.get("asset_id")
        asset_sha256 = constrained.get("asset_sha256")
        body = constrained.get("body")
        timestamp = constrained.get("timestamp_seconds")
        if not isinstance(client_id, str) or not _COMMENT_ID.fullmatch(client_id):
            raise GatewayPolicyError("client_id must be a canonical lowercase UUID")
        if not isinstance(package_id, str) or not _SHA256.fullmatch(package_id):
            raise GatewayPolicyError("package_id must be a lowercase SHA-256 digest")
        if not isinstance(asset_id, str) or not _PUBLIC_IDENTIFIER.fullmatch(asset_id):
            raise GatewayPolicyError("asset_id must be a bounded identifier")
        if not isinstance(asset_sha256, str) or not _SHA256.fullmatch(asset_sha256):
            raise GatewayPolicyError("asset_sha256 must be a lowercase SHA-256 digest")
        if not isinstance(body, str) or not body.strip() or len(body) > 5000:
            raise GatewayPolicyError("review body must contain 1 to 5000 characters")
        if timestamp is not None and (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or not math.isfinite(timestamp)
        ):
            raise GatewayPolicyError("timestamp_seconds must be finite or null")
        constrained["idempotency_key"] = _principal_idempotency_key(
            token, constrained.get("idempotency_key")
        )

    if tool_name == "artifact_stage":
        inbox = constrained.get("inbox_path")
        inline = constrained.get("inline_text")
        if (inbox is None) == (inline is None):
            raise GatewayPolicyError("artifact_stage requires exactly one input source")
        if inbox is not None:
            constrained["inbox_path"] = _safe_inbox_reference(inbox)
        if inline is not None:
            if not isinstance(inline, str) or not inline or "\x00" in inline:
                raise GatewayPolicyError("inline_text must be non-empty UTF-8 text")
            if len(inline.encode("utf-8")) > INLINE_TEXT_LIMIT:
                raise GatewayPolicyError("inline_text exceeds the 1 MiB limit")

    if tool_name in {"artifact_import", "produce_staged_artifact"}:
        stage_id = constrained.get("stage_id")
        if not isinstance(stage_id, str) or not _STAGE_ID.fullmatch(stage_id):
            raise GatewayPolicyError("stage_id must be exactly 32 lowercase hexadecimal characters")
    if tool_name == "produce_staged_artifact":
        artifact = _safe_artifact(constrained.get("artifact"))
        if artifact not in STAGED_PRODUCE_TARGETS:
            raise GatewayPolicyError("artifact is not an agent-authorable staged target")
        constrained["artifact"] = artifact

    return constrained


def _sanitize_public_value(
    value: Any,
    config: HttpMcpConfig,
    *,
    key: str | None = None,
) -> Any:
    if isinstance(value, dict):
        return {
            field: _sanitize_public_value(item, config, key=str(field))
            for field, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_public_value(item, config) for item in value]
    if not isinstance(value, str):
        return value

    workspace_text = str(config.workspace_root)
    projects_text = str(config.projects_root)
    media_tools_text = str(config.media_tools_root)
    backend_text = str(config.backend_command)
    if value == workspace_text:
        return "workspace"
    if value == projects_text:
        return "projects"
    if value.startswith(projects_text + os.sep):
        relative = value[len(projects_text + os.sep) :]
        return relative.replace(os.sep, "/")
    if value.startswith(workspace_text + os.sep):
        relative = value[len(workspace_text + os.sep) :]
        return relative.replace(os.sep, "/")
    if value == media_tools_text or value.startswith(media_tools_text + os.sep):
        return "bundled-media-tools"
    if value == backend_text:
        return "installed-video-studio-mcp"

    path_key = key is not None and any(
        marker in key.lower()
        for marker in ("path", "root", "destination", "executable", "command")
    )
    if path_key and Path(value).is_absolute():
        return "server-managed"
    return (
        value.replace(workspace_text, "<workspace>")
        .replace(media_tools_text, "<bundled-media-tools>")
        .replace(backend_text, "<installed-runtime>")
    )


def _sanitize_public_result(
    result: types.CallToolResult,
    config: HttpMcpConfig,
) -> types.CallToolResult:
    structured = (
        _sanitize_public_value(result.structuredContent, config)
        if result.structuredContent is not None
        else None
    )
    content: list[types.ContentBlock] = []
    for block in result.content:
        if isinstance(block, types.TextContent):
            try:
                parsed = json.loads(block.text)
            except (json.JSONDecodeError, TypeError):
                text = (
                    block.text.replace(str(config.workspace_root), "<workspace>")
                    .replace(str(config.media_tools_root), "<bundled-media-tools>")
                    .replace(str(config.backend_command), "<installed-runtime>")
                )
            else:
                text = json.dumps(
                    _sanitize_public_value(parsed, config),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            content.append(block.model_copy(update={"text": text}))
        else:
            content.append(block)
    return result.model_copy(
        update={
            "content": content,
            "structuredContent": structured,
            "meta": _sanitize_public_value(result.meta, config)
            if result.meta is not None
            else None,
        }
    )


def _replace_protected_resource_metadata(app: Any, config: HttpMcpConfig) -> None:
    """Advertise all per-tool scopes while global auth requires only a token."""

    replacement = create_protected_resource_routes(
        resource_url=AnyHttpUrl(config.resource_url),
        authorization_servers=[AnyHttpUrl(config.issuer)],
        scopes_supported=list(ALL_SCOPES),
        resource_name="Video Studio MCP",
    )[0]
    for index, route in enumerate(app.routes):
        if isinstance(route, Route) and route.path == replacement.path:
            app.routes[index] = replacement
            return
    app.routes.append(replacement)


def build_app(
    config: HttpMcpConfig,
    *,
    verifier: TokenVerifier | None = None,
    backend_connector: BackendConnector = stdio_backend,
) -> Any:
    """Build the authenticated official Streamable HTTP ASGI application."""

    token_verifier = verifier or JwksTokenVerifier(config)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastMCP[Any]) -> AsyncIterator[GatewaySession]:
        async with backend_connector(config) as backend:
            listed = await backend.list_tools()
            tools = {tool.name: tool for tool in listed.tools}
            missing = sorted(set(TOOL_SCOPES) - set(tools))
            if missing:
                raise RuntimeError(f"stable video-studio-mcp is missing required tools: {', '.join(missing)}")
            # The allowlist remains authoritative even if a future backend adds
            # a publish or runtime-management tool.
            public_tools = {
                name: _public_tool(name, tools[name], TOOL_SCOPES[name])
                for name in TOOL_SCOPES
            }
            yield GatewaySession(
                backend=backend,
                tools=tools,
                public_tools=public_tools,
            )

    parsed_resource = urlparse(config.resource_url)
    server = FastMCP(
        "video-studio-http",
        instructions=(
            "Authenticated self-hosted Video Studio. Use the configured workspace only. "
            "Lease owner fields are bound to the OAuth principal. Publishing and runtime "
            "approval are local-operator-only and are not exposed here."
        ),
        token_verifier=token_verifier,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(config.issuer),
            resource_server_url=AnyHttpUrl(config.resource_url),
            required_scopes=[],
            validate_token_resource=False,
        ),
        host=config.host,
        port=config.port,
        streamable_http_path=parsed_resource.path,
        json_response=True,
        stateless_http=False,
        max_request_body_size=config.max_request_body_size,
        session_idle_timeout=config.session_idle_timeout_seconds,
        max_sessions=config.max_sessions,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(config.allowed_hosts),
            allowed_origins=list(config.allowed_origins),
        ),
        lifespan=lifespan,
    )

    @server._mcp_server.list_tools()  # noqa: SLF001 - low-level SDK preserves backend schemas
    async def list_tools() -> list[types.Tool]:
        token = _token_from_request(server)
        state: GatewaySession = server._mcp_server.request_context.lifespan_context  # noqa: SLF001
        available = []
        for name, required_scope in TOOL_SCOPES.items():
            if required_scope not in token.scopes:
                continue
            available.append(state.public_tools[name])
        return available

    @server._mcp_server.call_tool()  # noqa: SLF001 - return CallToolResult unchanged
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        token = _token_from_request(server)
        required_scope = TOOL_SCOPES.get(name)
        if required_scope is None or name in PUBLISHING_TOOLS:
            raise GatewayPolicyError("tool is not exposed by the HTTP gateway")
        if required_scope not in token.scopes:
            raise GatewayPolicyError(f"insufficient OAuth scope; required: {required_scope}")
        if not isinstance(arguments, dict):
            raise GatewayPolicyError("tool arguments must be an object")
        state: GatewaySession = server._mcp_server.request_context.lifespan_context  # noqa: SLF001
        if name not in state.public_tools:
            raise GatewayPolicyError("tool is not exposed by the HTTP gateway")
        backend_arguments = _backend_arguments(name, arguments, config)
        constrained = enforce_arguments(name, backend_arguments, config, token)
        result = await state.backend.call_tool(name, constrained)
        return _sanitize_public_result(result, config)

    app = server.streamable_http_app()
    _replace_protected_resource_metadata(app, config)

    if isinstance(token_verifier, JwksTokenVerifier):
        sdk_lifespan = app.router.lifespan_context

        @contextlib.asynccontextmanager
        async def application_lifespan(application: Any) -> AsyncIterator[None]:
            try:
                async with sdk_lifespan(application):
                    yield
            finally:
                await token_verifier.aclose()

        app.router.lifespan_context = application_lifespan

    # Let embedding tests and controlled launchers close an owned JWKS client.
    app.state.video_studio_server = server
    app.state.token_verifier = token_verifier
    return app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the authenticated Video Studio HTTP MCP")
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="absolute path to the owner-controlled TOML configuration",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(args.config)
        app = build_app(config)
    except ConfigurationError as error:
        print(f"video-studio-http: {error}", file=sys.stderr)
        return 2
    uvicorn.run(
        app,
        host=config.host,
        port=config.port,
        log_config=None,
        access_log=False,
        server_header=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
