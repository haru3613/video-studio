from __future__ import annotations

import contextlib
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from tools.http_mcp import (
    ALL_SCOPES,
    EXECUTE_SCOPE,
    READ_SCOPE,
    REVIEW_SCOPE,
    TOOL_SCOPES,
    HttpMcpConfig,
    JwksTokenVerifier,
    build_app,
    load_config,
)
import tools.http_mcp as http_mcp
from tools.workspace import initialize as initialize_workspace


def backend_schema(name: str) -> dict[str, Any]:
    fields: dict[str, dict[str, Any]] = {
        "schema_version": {"type": "integer"},
        "workspace_root": {"type": "string"},
        "projects_root": {"type": "string"},
        "project_root": {"type": "string"},
        "project": {"type": "string"},
        "idempotency_key": {"type": "string"},
        "owner": {"type": "string"},
        "lease_id": {"type": "string"},
        "ttl_seconds": {"type": "integer"},
        "job_id": {"type": "string"},
        "max_bytes": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "diagnostic": {"type": "boolean", "default": False},
        "runner": {"type": "string"},
        "tools_root": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "comment_id": {"type": "string"},
        "status": {"type": "string"},
        "expected_package_id": {"type": "string"},
        "expected_asset_sha256": {"type": "string"},
        "client_id": {"type": "string"},
        "package_id": {"type": "string"},
        "asset_id": {"type": "string"},
        "asset_sha256": {"type": "string"},
        "timestamp_seconds": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "body": {"type": "string"},
        "role": {"type": "string"},
        "inbox_path": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "inline_text": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "stage_id": {"type": "string"},
        "artifact": {"type": "string"},
        "produced_by": {"type": "string"},
        "cron_run_id": {"type": "string"},
        "candidate_id": {"type": "string"},
        "chosen_by": {"type": "string"},
        "chosen_at": {"type": "integer"},
        "action": {"type": "string"},
        "reviewed_by": {"type": "string"},
        "verdict": {"type": "string"},
        "notes": {"type": "string"},
    }
    tool_fields: dict[str, list[str]] = {
        "workspace_info": ["workspace_root"],
        "project_list": ["workspace_root"],
        "create": ["projects_root", "project", "idempotency_key"],
        "select": ["projects_root", "project"],
        "status": ["project_root"],
        "artifact_index": ["project_root"],
        "record_selection": [
            "project_root",
            "cron_run_id",
            "candidate_id",
            "chosen_by",
            "chosen_at",
            "idempotency_key",
        ],
        "lease_claim": ["project_root", "owner", "ttl_seconds", "idempotency_key"],
        "lease_renew": [
            "project_root",
            "owner",
            "lease_id",
            "ttl_seconds",
            "idempotency_key",
        ],
        "lease_status": ["project_root"],
        "lease_release": ["project_root", "owner", "lease_id", "idempotency_key"],
        "run_next": [
            "project_root",
            "owner",
            "lease_id",
            "runner",
            "tools_root",
            "idempotency_key",
        ],
        "verify": ["project_root"],
        "delivery_status": ["project_root"],
        "export_delivery": [
            "project_root",
            "owner",
            "lease_id",
            "idempotency_key",
            "diagnostic",
        ],
        "job_status": ["project_root", "job_id"],
        "job_logs": ["project_root", "job_id", "max_bytes"],
        "job_cancel": ["project_root", "owner", "lease_id", "job_id", "idempotency_key"],
        "job_resume": ["project_root", "owner", "lease_id", "job_id", "idempotency_key"],
        "review_feedback": ["project_root"],
        "review_add": [
            "project_root",
            "client_id",
            "package_id",
            "asset_id",
            "asset_sha256",
            "timestamp_seconds",
            "body",
            "idempotency_key",
        ],
        "review_resolve": [
            "project_root",
            "comment_id",
            "status",
            "expected_package_id",
            "expected_asset_sha256",
            "idempotency_key",
        ],
        "artifact_stage": [
            "project_root",
            "owner",
            "lease_id",
            "role",
            "inbox_path",
            "inline_text",
            "idempotency_key",
        ],
        "artifact_import": [
            "project_root",
            "owner",
            "lease_id",
            "stage_id",
            "idempotency_key",
        ],
        "produce_staged_artifact": [
            "project_root",
            "owner",
            "lease_id",
            "stage_id",
            "artifact",
            "produced_by",
            "idempotency_key",
        ],
        "visual_qa": ["project_root", "owner", "lease_id", "action", "idempotency_key"],
        "pronunciation_review": [
            "project_root",
            "owner",
            "lease_id",
            "reviewed_by",
            "verdict",
            "notes",
            "idempotency_key",
        ],
    }
    selected = ["schema_version", *tool_fields[name]]
    optional = {"tools_root", "max_bytes", "diagnostic", "inbox_path", "inline_text"}
    return {
        "type": "object",
        "properties": {field: fields[field] for field in selected},
        "required": [field for field in selected if field not in optional],
        "additionalProperties": False,
    }


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.tools = [
            types.Tool(
                name=name,
                description=f"Backend {name}",
                inputSchema=backend_schema(name),
            )
            for name in TOOL_SCOPES
        ]

    async def list_tools(self) -> types.ListToolsResult:
        return types.ListToolsResult(tools=self.tools)

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> types.CallToolResult:
        copied = dict(arguments or {})
        self.calls.append((name, copied))
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=json.dumps({"tool": name, "arguments": copied}),
                )
            ],
            structuredContent={"tool": name, "arguments": copied},
            _meta={"backend": "fake"},
        )


@contextlib.asynccontextmanager
async def fake_connector(backend: FakeBackend, _: HttpMcpConfig) -> AsyncIterator[FakeBackend]:
    yield backend


@pytest.fixture(scope="module")
def signing_material() -> tuple[Any, dict[str, Any]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": "test-key", "alg": "RS256", "use": "sig"})
    return private_key, {"keys": [jwk]}


@pytest.fixture
def gateway_config(tmp_path: Path) -> HttpMcpConfig:
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace)
    projects = workspace / "projects"
    media_tools = tmp_path / "repo" / "media-tools"
    media_tools.mkdir(parents=True)
    return HttpMcpConfig(
        host="127.0.0.1",
        port=8765,
        resource_url="https://studio.example/mcp",
        issuer="https://issuer.example",
        audience="https://studio.example/mcp",
        jwks_url="https://issuer.example/jwks",
        algorithms=("RS256",),
        allowed_origins=("https://studio.example",),
        allowed_hosts=("studio.example",),
        workspace_root=workspace.resolve(),
        projects_root=projects.resolve(),
        media_tools_root=media_tools.resolve(),
        backend_command=Path("/unused/video-studio-mcp"),
        jwks_cache_seconds=300,
        jwks_min_refresh_seconds=30,
    )


def issue_token(
    signing_material: tuple[Any, dict[str, Any]],
    *,
    issuer: str = "https://issuer.example",
    audience: str = "https://studio.example/mcp",
    scopes: tuple[str, ...] = ALL_SCOPES,
    kid: str = "test-key",
) -> str:
    private_key, _ = signing_material
    now = int(time.time())
    return jwt.encode(
        {
            "iss": issuer,
            "aud": audience,
            "sub": "operator-123",
            "client_id": "agent-client",
            "scope": " ".join(scopes),
            "iat": now,
            "exp": now + 300,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": kid},
    )


def test_config_requires_initialized_fixed_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    initialize_workspace(workspace)
    fake_home = tmp_path / "home"
    backend = fake_home / ".local/share/video-studio/bin/video-studio-mcp"
    backend.parent.mkdir(parents=True)
    backend.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    backend.chmod(0o700)
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    config_file = tmp_path / "http.toml"
    config_file.write_text(
        "\n".join(
            [
                "[server]",
                'host = "127.0.0.1"',
                "port = 8765",
                'resource_url = "https://studio.example/mcp"',
                'allowed_origins = ["https://studio.example"]',
                "[oauth]",
                'issuer = "https://issuer.example"',
                'audience = "https://studio.example/mcp"',
                'jwks_url = "https://issuer.example/jwks"',
                'algorithms = ["RS256"]',
                "[paths]",
                f'workspace_root = "{workspace}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    config_file.chmod(0o600)

    loaded = load_config(config_file, repo_root=Path(__file__).resolve().parents[1])
    assert loaded.workspace_root == workspace.resolve()
    assert loaded.projects_root == (workspace / "projects").resolve()
    assert loaded.backend_command == backend.resolve()

    (workspace / "inbox").rmdir()
    with pytest.raises(http_mcp.ConfigurationError, match="inbox"):
        load_config(config_file, repo_root=Path(__file__).resolve().parents[1])


def verifier_for(
    gateway_config: HttpMcpConfig,
    signing_material: tuple[Any, dict[str, Any]],
    request_count: list[int] | None = None,
) -> JwksTokenVerifier:
    _, document = signing_material

    def handler(request: httpx.Request) -> httpx.Response:
        if request_count is not None:
            request_count.append(1)
        assert str(request.url) == gateway_config.jwks_url
        return httpx.Response(200, json=document)

    return JwksTokenVerifier(
        gateway_config,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def make_app(
    config: HttpMcpConfig,
    signing_material: tuple[Any, dict[str, Any]],
    backend: FakeBackend,
    request_count: list[int] | None = None,
):
    verifier = verifier_for(config, signing_material, request_count)

    @contextlib.asynccontextmanager
    async def connector(selected: HttpMcpConfig) -> AsyncIterator[FakeBackend]:
        async with fake_connector(backend, selected) as session:
            yield session

    return build_app(config, verifier=verifier, backend_connector=connector)


@contextlib.asynccontextmanager
async def raw_client(app: Any, token: str, *, origin: str = "https://studio.example"):
    headers = {"Authorization": f"Bearer {token}", "Origin": origin}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://studio.example",
            headers=headers,
        ) as client:
            yield client


@contextlib.asynccontextmanager
async def mcp_session(app: Any, token: str):
    async with raw_client(app, token) as client:
        async with streamable_http_client(
            "https://studio.example/mcp", http_client=client
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


@pytest.mark.asyncio
async def test_real_streamable_http_session_preserves_schema_and_structured_result(
    gateway_config: HttpMcpConfig, signing_material: tuple[Any, dict[str, Any]]
) -> None:
    backend = FakeBackend()
    app = make_app(gateway_config, signing_material, backend)
    token = issue_token(signing_material, scopes=(READ_SCOPE,))
    project = gateway_config.projects_root / "demo"
    project.mkdir()

    async with mcp_session(app, token) as session:
        listed = await session.list_tools()
        names = {tool.name for tool in listed.tools}
        assert names == {name for name, scope in TOOL_SCOPES.items() if scope == READ_SCOPE}
        assert not names.intersection(
            {
                "publish",
                "approve_publish",
                "prepare_publish_approval",
                "prepare_publish",
                "replace_thumbnail",
                "reconcile_upload",
            }
        )
        status = next(tool for tool in listed.tools if tool.name == "status")
        assert status.inputSchema["additionalProperties"] is False
        assert "project_id" in status.inputSchema["properties"]
        assert "project_root" not in status.inputSchema["properties"]
        assert READ_SCOPE in (status.description or "")

        result = await session.call_tool(
            "status", {"schema_version": 1, "project_id": "demo"}
        )
        assert result.isError is False
        assert result.structuredContent == {
            "tool": "status",
            "arguments": {"schema_version": 1, "project_root": "demo"},
        }
        assert result.meta == {"backend": "fake"}
        assert isinstance(result.content[0], types.TextContent)
        assert str(gateway_config.workspace_root) not in result.content[0].text
        assert json.loads(result.content[0].text)["arguments"]["project_root"] == "demo"

        hidden = await session.call_tool("publish", {})
        assert hidden.isError is True
        assert "not exposed" in hidden.content[0].text
        assert [name for name, _ in backend.calls] == ["status"]


@pytest.mark.asyncio
async def test_public_tool_schemas_never_expose_server_filesystem_fields(
    gateway_config: HttpMcpConfig, signing_material: tuple[Any, dict[str, Any]]
) -> None:
    backend = FakeBackend()
    app = make_app(gateway_config, signing_material, backend)
    token = issue_token(signing_material)

    async with mcp_session(app, token) as session:
        tools = (await session.list_tools()).tools

    forbidden = {"workspace_root", "projects_root", "project_root", "tools_root", "source_file"}
    assert {"workspace_info", "project_list", "artifact_stage", "artifact_import"}.issubset(
        {tool.name for tool in tools}
    )
    for tool in tools:
        properties = tool.inputSchema.get("properties", {})
        assert forbidden.isdisjoint(properties), tool.name
        if tool.name not in {"workspace_info", "project_list"}:
            assert properties["project_id"]["pattern"] == http_mcp._SLUG.pattern
    export_schema = next(tool.inputSchema for tool in tools if tool.name == "export_delivery")
    assert export_schema["properties"]["diagnostic"] == {
        "type": "boolean",
        "default": False,
    }
    review_schema = next(tool.inputSchema for tool in tools if tool.name == "review_resolve")
    assert "expected_package_id" in review_schema["properties"]
    add_schema = next(tool.inputSchema for tool in tools if tool.name == "review_add")
    assert {"owner", "lease_id", "reviewed_by", "verdict", "approval"}.isdisjoint(
        add_schema["properties"]
    )
    assert "produce_artifact" not in {tool.name for tool in tools}


@pytest.mark.asyncio
async def test_workspace_catalog_calls_use_fixed_root_and_redact_backend_paths(
    gateway_config: HttpMcpConfig, signing_material: tuple[Any, dict[str, Any]]
) -> None:
    backend = FakeBackend()
    app = make_app(gateway_config, signing_material, backend)
    token = issue_token(signing_material, scopes=(READ_SCOPE,))

    async with mcp_session(app, token) as session:
        info = await session.call_tool("workspace_info", {"schema_version": 1})
        projects = await session.call_tool("project_list", {"schema_version": 1})

    assert info.isError is False
    assert projects.isError is False
    assert [name for name, _ in backend.calls] == ["workspace_info", "project_list"]
    assert all(
        arguments["workspace_root"] == str(gateway_config.workspace_root)
        for _, arguments in backend.calls
    )
    assert info.structuredContent["arguments"]["workspace_root"] == "workspace"
    assert str(gateway_config.workspace_root) not in info.content[0].text
    assert str(gateway_config.workspace_root) not in projects.content[0].text


@pytest.mark.asyncio
async def test_project_creation_uses_public_slug_and_fixed_projects_root(
    gateway_config: HttpMcpConfig, signing_material: tuple[Any, dict[str, Any]]
) -> None:
    backend = FakeBackend()
    app = make_app(gateway_config, signing_material, backend)
    token = issue_token(signing_material, scopes=(EXECUTE_SCOPE,))

    async with mcp_session(app, token) as session:
        invalid = await session.call_tool(
            "create",
            {"schema_version": 1, "project_id": "../escape", "idempotency_key": "create-1"},
        )
        created = await session.call_tool(
            "create",
            {"schema_version": 1, "project_id": "new-project", "idempotency_key": "create-2"},
        )

    assert invalid.isError is True
    assert created.isError is False
    assert backend.calls == [
        (
            "create",
            {
                "schema_version": 1,
                "idempotency_key": "create-2",
                "projects_root": str(gateway_config.projects_root),
                "project": "new-project",
            },
        )
    ]
    assert str(gateway_config.projects_root) not in created.content[0].text


@pytest.mark.asyncio
async def test_public_intake_translates_project_id_and_accepts_only_bounded_sources(
    gateway_config: HttpMcpConfig, signing_material: tuple[Any, dict[str, Any]]
) -> None:
    project = gateway_config.projects_root / "demo"
    project.mkdir()
    backend = FakeBackend()
    app = make_app(gateway_config, signing_material, backend)
    token = issue_token(signing_material, scopes=(EXECUTE_SCOPE,))
    base = {
        "schema_version": 1,
        "project_id": "demo",
        "owner": "caller-owner",
        "lease_id": "lease",
        "role": "script_notes",
        "idempotency_key": "stage-1",
    }

    async with mcp_session(app, token) as session:
        inline = await session.call_tool(
            "artifact_stage", {**base, "inline_text": "A bounded production note"}
        )
        inbox = await session.call_tool(
            "artifact_stage",
            {
                **base,
                "role": "reference_image",
                "inbox_path": "references/cover.png",
                "idempotency_key": "stage-2",
            },
        )
        both = await session.call_tool(
            "artifact_stage",
            {**base, "inline_text": "note", "inbox_path": "note.txt"},
        )
        absolute = await session.call_tool(
            "artifact_stage",
            {**base, "inbox_path": "/etc/passwd"},
        )
        traversal = await session.call_tool(
            "artifact_stage",
            {**base, "inbox_path": "../outside.png"},
        )

    assert inline.isError is False
    assert inbox.isError is False
    assert both.isError is True
    assert absolute.isError is True
    assert traversal.isError is True
    assert [name for name, _ in backend.calls] == ["artifact_stage", "artifact_stage"]
    for _, arguments in backend.calls:
        assert arguments["project_root"] == str(project.resolve())
        assert arguments["owner"].startswith("http:")
        assert "project_id" not in arguments
    with pytest.raises(http_mcp.GatewayPolicyError, match="1 MiB"):
        http_mcp.enforce_arguments(
            "artifact_stage",
            {
                "schema_version": 1,
                "project_root": str(project),
                "role": "script_notes",
                "inline_text": "界" * 350_000,
            },
            gateway_config,
            None,  # no owner field is present, so no token data is consulted
        )


@pytest.mark.asyncio
async def test_stage_ids_are_opaque_and_old_absolute_producer_is_hidden(
    gateway_config: HttpMcpConfig, signing_material: tuple[Any, dict[str, Any]]
) -> None:
    project = gateway_config.projects_root / "demo"
    project.mkdir()
    backend = FakeBackend()
    app = make_app(gateway_config, signing_material, backend)
    token = issue_token(signing_material, scopes=(EXECUTE_SCOPE,))
    common = {
        "schema_version": 1,
        "project_id": "demo",
        "owner": "ignored",
        "lease_id": "lease",
        "idempotency_key": "intake-1",
    }

    async with mcp_session(app, token) as session:
        bad = await session.call_tool(
            "artifact_import", {**common, "stage_id": "../staging/blob"}
        )
        imported = await session.call_tool(
            "artifact_import", {**common, "stage_id": "a" * 32}
        )
        produced = await session.call_tool(
            "produce_staged_artifact",
            {
                **common,
                "stage_id": "a" * 32,
                "artifact": "script-proposal.md",
                "produced_by": "agent",
            },
        )
        forbidden = await session.call_tool(
            "produce_staged_artifact",
            {
                **common,
                "stage_id": "a" * 32,
                "artifact": "output/final.mp4",
                "produced_by": "agent",
            },
        )
        hidden = await session.call_tool(
            "produce_artifact",
            {
                **common,
                "source_file": "/server/project/.hvp/staging/blob",
                "artifact": "script-proposal.md",
                "produced_by": "agent",
            },
        )

    assert bad.isError is True
    assert imported.isError is False
    assert produced.isError is False
    assert forbidden.isError is True
    assert hidden.isError is True
    assert [name for name, _ in backend.calls] == [
        "artifact_import",
        "produce_staged_artifact",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("issuer", "audience"),
    [
        ("https://wrong-issuer.example", "https://studio.example/mcp"),
        ("https://issuer.example", "https://other.example/mcp"),
    ],
)
async def test_bad_issuer_or_audience_gets_oauth_challenge(
    gateway_config: HttpMcpConfig,
    signing_material: tuple[Any, dict[str, Any]],
    issuer: str,
    audience: str,
) -> None:
    app = make_app(gateway_config, signing_material, FakeBackend())
    token = issue_token(signing_material, issuer=issuer, audience=audience)
    async with raw_client(app, token) as client:
        response = await client.post("/mcp", json={})
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert 'error="invalid_token"' in challenge
    assert 'resource_metadata="https://studio.example/.well-known/oauth-protected-resource/mcp"' in challenge


@pytest.mark.asyncio
async def test_bad_signature_is_rejected(
    gateway_config: HttpMcpConfig, signing_material: tuple[Any, dict[str, Any]]
) -> None:
    other_material = (rsa.generate_private_key(public_exponent=65537, key_size=2048), signing_material[1])
    token = issue_token(other_material)
    app = make_app(gateway_config, signing_material, FakeBackend())
    async with raw_client(app, token) as client:
        response = await client.post("/mcp", json={})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_per_tool_scope_blocks_call_before_backend(
    gateway_config: HttpMcpConfig, signing_material: tuple[Any, dict[str, Any]]
) -> None:
    backend = FakeBackend()
    app = make_app(gateway_config, signing_material, backend)
    token = issue_token(signing_material, scopes=(REVIEW_SCOPE,))
    project = gateway_config.projects_root / "demo"
    project.mkdir()

    async with mcp_session(app, token) as session:
        result = await session.call_tool(
            "status", {"schema_version": 1, "project_id": "demo"}
        )
    assert result.isError is True
    assert "required: studio:read" in result.content[0].text
    assert backend.calls == []


@pytest.mark.asyncio
async def test_disallowed_origin_is_rejected_before_protocol(
    gateway_config: HttpMcpConfig, signing_material: tuple[Any, dict[str, Any]]
) -> None:
    app = make_app(gateway_config, signing_material, FakeBackend())
    token = issue_token(signing_material)
    async with raw_client(app, token, origin="https://evil.example") as client:
        response = await client.post("/mcp", json={})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_workspace_traversal_never_reaches_backend(
    gateway_config: HttpMcpConfig, signing_material: tuple[Any, dict[str, Any]]
) -> None:
    backend = FakeBackend()
    app = make_app(gateway_config, signing_material, backend)
    token = issue_token(signing_material, scopes=(READ_SCOPE,))

    async with mcp_session(app, token) as session:
        result = await session.call_tool(
            "status",
            {"schema_version": 1, "project_id": "../outside"},
        )
    assert result.isError is True
    assert "Input validation error" in result.content[0].text
    assert backend.calls == []


@pytest.mark.asyncio
async def test_project_symlink_escape_never_reaches_backend(
    gateway_config: HttpMcpConfig, signing_material: tuple[Any, dict[str, Any]], tmp_path: Path
) -> None:
    outside = tmp_path / "outside-project"
    outside.mkdir()
    linked = gateway_config.projects_root / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    backend = FakeBackend()
    app = make_app(gateway_config, signing_material, backend)
    token = issue_token(signing_material, scopes=(READ_SCOPE,))

    async with mcp_session(app, token) as session:
        result = await session.call_tool(
            "status", {"schema_version": 1, "project_id": "linked"}
        )
    assert result.isError is True
    assert "direct workspace project" in result.content[0].text
    assert backend.calls == []


@pytest.mark.asyncio
async def test_lease_owner_is_derived_from_authenticated_principal(
    gateway_config: HttpMcpConfig, signing_material: tuple[Any, dict[str, Any]]
) -> None:
    project = gateway_config.projects_root / "demo"
    project.mkdir()
    backend = FakeBackend()
    app = make_app(gateway_config, signing_material, backend)
    token = issue_token(signing_material, scopes=(EXECUTE_SCOPE,))

    async with mcp_session(app, token) as session:
        result = await session.call_tool(
            "lease_claim",
            {
                "schema_version": 1,
                "project_id": "demo",
                "owner": "pretend-to-be-someone-else",
                "ttl_seconds": 60,
                "idempotency_key": "claim-1",
            },
        )
    assert result.isError is False
    forwarded_owner = backend.calls[0][1]["owner"]
    assert forwarded_owner.startswith("http:")
    assert forwarded_owner != "pretend-to-be-someone-else"


@pytest.mark.asyncio
async def test_caller_cannot_select_tools_root(
    gateway_config: HttpMcpConfig, signing_material: tuple[Any, dict[str, Any]], tmp_path: Path
) -> None:
    project = gateway_config.projects_root / "demo"
    project.mkdir()
    malicious_tools = tmp_path / "malicious-tools"
    malicious_tools.mkdir()
    backend = FakeBackend()
    app = make_app(gateway_config, signing_material, backend)
    token = issue_token(signing_material, scopes=(EXECUTE_SCOPE,))

    async with mcp_session(app, token) as session:
        result = await session.call_tool(
            "run_next",
            {
                "schema_version": 1,
                "project_id": "demo",
                "owner": "ignored",
                "lease_id": "lease",
                "runner": "render-project",
                "tools_root": str(malicious_tools),
                "idempotency_key": "run-1",
            },
        )
    assert result.isError is True
    assert "Input validation error" in result.content[0].text
    assert backend.calls == []


@pytest.mark.asyncio
async def test_delivery_and_job_arguments_stay_inside_server_policy(
    gateway_config: HttpMcpConfig, signing_material: tuple[Any, dict[str, Any]]
) -> None:
    project = gateway_config.projects_root / "demo"
    project.mkdir()
    backend = FakeBackend()
    app = make_app(gateway_config, signing_material, backend)
    token = issue_token(signing_material, scopes=(READ_SCOPE, EXECUTE_SCOPE))

    async with mcp_session(app, token) as session:
        destination = await session.call_tool(
            "export_delivery",
            {
                "schema_version": 1,
                "project_id": "demo",
                "owner": "ignored",
                "lease_id": "lease",
                "idempotency_key": "export-1",
                "destination": "/tmp/caller-selected",
            },
        )
        bad_job = await session.call_tool(
            "job_status",
            {
                "schema_version": 1,
                "project_id": "demo",
                "job_id": "../another-project",
            },
        )
        oversized_log = await session.call_tool(
            "job_logs",
            {
                "schema_version": 1,
                "project_id": "demo",
                "job_id": "a" * 32,
                "max_bytes": 65_537,
            },
        )
        valid_export = await session.call_tool(
            "export_delivery",
            {
                "schema_version": 1,
                "project_id": "demo",
                "owner": "ignored",
                "lease_id": "lease",
                "idempotency_key": "export-2",
            },
        )
        valid_log = await session.call_tool(
            "job_logs",
            {
                "schema_version": 1,
                "project_id": "demo",
                "job_id": "b" * 32,
                "max_bytes": 65_536,
            },
        )

    assert destination.isError is True
    assert "Input validation error" in destination.content[0].text
    assert bad_job.isError is True
    assert "32 lowercase hexadecimal" in bad_job.content[0].text
    assert oversized_log.isError is True
    assert "1 through 65536" in oversized_log.content[0].text
    assert valid_export.isError is False
    assert valid_log.isError is False
    assert [name for name, _ in backend.calls] == ["export_delivery", "job_logs"]
    assert backend.calls[0][1]["owner"].startswith("http:")
    assert "destination" not in backend.calls[0][1]


@pytest.mark.asyncio
async def test_review_mutations_need_review_scope_and_no_execute_lease(
    gateway_config: HttpMcpConfig, signing_material: tuple[Any, dict[str, Any]]
) -> None:
    project = gateway_config.projects_root / "demo"
    project.mkdir()
    backend = FakeBackend()
    app = make_app(gateway_config, signing_material, backend)
    token = issue_token(signing_material, scopes=(REVIEW_SCOPE,))
    valid = {
        "schema_version": 1,
        "project_id": "demo",
        "comment_id": "12345678-1234-1234-1234-123456789abc",
        "status": "resolved",
        "expected_package_id": "a" * 64,
        "expected_asset_sha256": "b" * 64,
        "idempotency_key": "resolve-1",
    }

    async with mcp_session(app, token) as session:
        bad_uuid = await session.call_tool(
            "review_resolve", {**valid, "comment_id": "12345678-1234-1234-1234-123456789ABC"}
        )
        bad_digest = await session.call_tool(
            "review_resolve", {**valid, "expected_asset_sha256": "../asset"}
        )
        bad_status = await session.call_tool(
            "review_resolve", {**valid, "status": "approved"}
        )
        bad_body = await session.call_tool(
            "review_add",
            {
                "schema_version": 1,
                "project_id": "demo",
                "client_id": "12345678-1234-1234-1234-123456789abc",
                "package_id": "a" * 64,
                "asset_id": "video-current",
                "asset_sha256": "b" * 64,
                "timestamp_seconds": 0,
                "body": "x" * 5001,
                "idempotency_key": "add-1",
            },
        )
        bad_key = await session.call_tool(
            "review_add",
            {
                "schema_version": 1,
                "project_id": "demo",
                "client_id": "12345678-1234-1234-1234-123456789abc",
                "package_id": "a" * 64,
                "asset_id": "video-current",
                "asset_sha256": "b" * 64,
                "timestamp_seconds": 0,
                "body": "Please tighten this cut.",
                "idempotency_key": "not allowed",
            },
        )
        added = await session.call_tool(
            "review_add",
            {
                "schema_version": 1,
                "project_id": "demo",
                "client_id": "12345678-1234-1234-1234-123456789abc",
                "package_id": "a" * 64,
                "asset_id": "video-current",
                "asset_sha256": "b" * 64,
                "timestamp_seconds": 0,
                "body": "Please tighten this cut.",
                "idempotency_key": "add-2",
            },
        )
        accepted = await session.call_tool("review_resolve", valid)
        render = await session.call_tool(
            "run_next",
            {
                "schema_version": 1,
                "project_id": "demo",
                "owner": "ignored",
                "lease_id": "lease",
                "runner": "render-project",
                "idempotency_key": "render-1",
            },
        )

    assert bad_uuid.isError is True
    assert bad_digest.isError is True
    assert bad_status.isError is True
    assert bad_body.isError is True
    assert bad_key.isError is True
    assert added.isError is False
    assert accepted.isError is False
    assert render.isError is True
    assert [name for name, _ in backend.calls] == ["review_add", "review_resolve"]
    assert "owner" not in backend.calls[0][1]
    assert "lease_id" not in backend.calls[0][1]
    assert backend.calls[0][1]["idempotency_key"].startswith("http_")
    assert backend.calls[0][1]["idempotency_key"] != "add-2"
    assert "owner" not in backend.calls[1][1]


@pytest.mark.asyncio
async def test_unknown_kid_rotation_attempt_is_rate_bounded(
    gateway_config: HttpMcpConfig, signing_material: tuple[Any, dict[str, Any]]
) -> None:
    requests: list[int] = []
    verifier = verifier_for(gateway_config, signing_material, requests)
    unknown = issue_token(signing_material, kid="rotated-but-not-published")
    assert await verifier.verify_token(unknown) is None
    assert await verifier.verify_token(unknown) is None
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_stdio_backend_forwards_only_safe_baseline_and_operator_allowlist(
    gateway_config: HttpMcpConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed = {
        "VIDEO_STUDIO_DELIVERY_ROOT": "/operator/exports",
        "ELEVENLABS_API_KEY_PATH": "/operator/credentials/elevenlabs",
        "VIDEO_STUDIO_TTS_VOICE_ID": "voice-id",
        "VIDEO_STUDIO_G2PW_MODEL_DIR": "/operator/models/g2pw",
        "VIDEO_STUDIO_G2PW_PYTHON": "/operator/bin/g2p-python",
        "VIDEO_STUDIO_COVER_ASSET_DIR": "/operator/cover-assets",
        "VIDEO_STUDIO_CHROMIUM": "/operator/bin/chromium",
    }
    for name, value in allowed.items():
        monkeypatch.setenv(name, value)
    rejected = {
        "PYTHONPATH": "/untrusted/python",
        "BASH_ENV": "/untrusted/shell-hook",
        "AWS_SECRET_ACCESS_KEY": "not-forwarded",
        "UNRELATED_PROVIDER_TOKEN": "not-forwarded",
    }
    for name, value in rejected.items():
        monkeypatch.setenv(name, value)

    captured: list[Any] = []

    @contextlib.asynccontextmanager
    async def fake_stdio(parameters: Any, errlog: Any = None):
        captured.append(parameters)
        yield object(), object()

    class FakeSession:
        def __init__(self, read: Any, write: Any):
            self.initialized = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: Any):
            return None

        async def initialize(self):
            self.initialized = True

    monkeypatch.setattr(http_mcp, "stdio_client", fake_stdio)
    monkeypatch.setattr(http_mcp, "ClientSession", FakeSession)

    async with http_mcp.stdio_backend(gateway_config) as session:
        assert session.initialized is True

    assert len(captured) == 1
    parameters = captured[0]
    assert parameters.command == str(gateway_config.backend_command)
    assert parameters.args == []
    assert parameters.env is not None
    for name, value in allowed.items():
        assert parameters.env[name] == value
    for name in rejected:
        assert name not in parameters.env
    assert parameters.env.get("HOME") == str(Path.home())
    assert "PATH" in parameters.env


@pytest.mark.asyncio
async def test_protected_resource_metadata_advertises_per_tool_scopes(
    gateway_config: HttpMcpConfig, signing_material: tuple[Any, dict[str, Any]]
) -> None:
    app = make_app(gateway_config, signing_material, FakeBackend())
    token = issue_token(signing_material)
    async with raw_client(app, token) as client:
        response = await client.get("/.well-known/oauth-protected-resource/mcp")
    assert response.status_code == 200
    document = response.json()
    assert document["resource"] == gateway_config.resource_url
    assert set(document["scopes_supported"]) == set(ALL_SCOPES)
