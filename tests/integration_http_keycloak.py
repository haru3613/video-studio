#!/usr/bin/env python3
"""Isolated real-Keycloak integration check for the HTTP MCP gateway.

This script creates and removes task-labelled Keycloak/Nginx containers and a
throwaway realm. By default it uses a temporary fake stdio MCP backend for the
authorization matrix. With ``--context`` it probes and uses an isolated
installed candidate only when that backend exposes the complete required tool
surface. It never invokes the user's original Haru runtime.
"""

from __future__ import annotations

import asyncio
import argparse
import base64
import contextlib
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import httpx
import jwt
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.client.streamable_http import streamable_http_client

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.http_mcp import (
    EXECUTE_SCOPE,
    READ_SCOPE,
    REVIEW_SCOPE,
    HttpMcpConfig,
    JwksTokenVerifier,
    TOOL_SCOPES,
    build_app,
)
from tools.workspace import initialize as initialize_workspace

if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))
from test_http_mcp import backend_schema


KEYCLOAK_IMAGE = (
    "quay.io/keycloak/keycloak:26.7.4@"
    "sha256:82a77884f3af238beab1e7afd63b5f530e1b5c0590bd7aa60b40a40463e29b2c"
)
NGINX_IMAGE = (
    "nginx:1.27.5-alpine@"
    "sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"
)


class OAuthFlowError(RuntimeError):
    pass


class LoginFormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.action: str | None = None
        self.fields: dict[str, str] = {}
        self._in_login_form = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "form" and attributes.get("id") == "kc-form-login":
            self._in_login_form = True
            self.action = attributes.get("action")
        elif tag == "input" and self._in_login_form:
            name = attributes.get("name")
            if name:
                self.fields[name] = attributes.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form" and self._in_login_form:
            self._in_login_form = False


def run_docker(arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *arguments],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and result.returncode != 0:
        operation = arguments[0] if arguments else "command"
        raise RuntimeError(f"docker {operation} failed with exit code {result.returncode}")
    return result


class KeycloakFixture:
    def __init__(self, resource_url: str, redirect_uri: str, ca_path: Path) -> None:
        marker = f"{os.getpid()}-{uuid.uuid4().hex[:10]}"
        self.container = f"video-studio-http-keycloak-e2e-{marker}"
        self.realm = f"video-studio-e2e-{uuid.uuid4().hex[:12]}"
        self.resource_url = resource_url
        self.external_origin = resource_url.removesuffix("/mcp")
        self.redirect_uri = redirect_uri
        self.ca_path = ca_path
        self.admin_user = f"e2e-admin-{uuid.uuid4().hex[:8]}"
        self.admin_password = secrets.token_urlsafe(32)
        self.client_id = f"video-studio-agent-{uuid.uuid4().hex[:8]}"
        self.client_secret = secrets.token_urlsafe(32)
        self.wrong_audience_client_id = f"wrong-audience-{uuid.uuid4().hex[:8]}"
        self.wrong_audience_secret = secrets.token_urlsafe(32)
        self.public_client_id = f"video-studio-browser-{uuid.uuid4().hex[:8]}"
        self.test_username = f"e2e-user-{uuid.uuid4().hex[:8]}"
        self.test_password = secrets.token_urlsafe(28)
        self.internal_base_url = ""
        self.issuer = ""
        self.jwks_url = ""

    def start(self) -> None:
        run_docker(
            [
                "run",
                "--rm",
                "--detach",
                "--name",
                self.container,
                "--publish",
                "127.0.0.1::8080",
                "--env",
                f"KC_BOOTSTRAP_ADMIN_USERNAME={self.admin_user}",
                "--env",
                f"KC_BOOTSTRAP_ADMIN_PASSWORD={self.admin_password}",
                "--env",
                f"KC_HOSTNAME={self.external_origin}",
                "--env",
                "KC_PROXY_HEADERS=xforwarded",
                KEYCLOAK_IMAGE,
                "start-dev",
            ]
        )
        port_output = run_docker(["port", self.container, "8080/tcp"]).stdout.strip()
        if not port_output.startswith("127.0.0.1:"):
            raise RuntimeError("Keycloak did not bind an isolated loopback port")
        port = int(port_output.rsplit(":", 1)[1])
        self.internal_base_url = f"http://127.0.0.1:{port}"
        self.issuer = f"{self.external_origin}/realms/{self.realm}"
        self.jwks_url = f"{self.issuer}/protocol/openid-connect/certs"
        self._wait_until_ready()
        self._configure_realm()

    def stop(self) -> None:
        # The exact task-labelled container is the only destructive target.
        run_docker(["rm", "--force", self.container], check=False)

    def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + 180
        discovery = (
            f"{self.internal_base_url}/realms/master/.well-known/openid-configuration"
        )
        last_status = "not reachable"
        with httpx.Client(timeout=3.0) as client:
            while time.monotonic() < deadline:
                container_state = run_docker(
                    ["inspect", "--format", "{{.State.Status}}", self.container], check=False
                )
                if container_state.returncode != 0 or container_state.stdout.strip() == "exited":
                    raise RuntimeError("Keycloak container exited before becoming ready")
                try:
                    response = client.get(discovery)
                    last_status = str(response.status_code)
                    if response.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(1)
        raise RuntimeError(f"Keycloak did not become ready; last HTTP status: {last_status}")

    def _admin_token(self, client: httpx.Client) -> str:
        response = client.post(
            f"{self.internal_base_url}/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": self.admin_user,
                "password": self.admin_password,
            },
        )
        response.raise_for_status()
        token = response.json().get("access_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("Keycloak admin token response was malformed")
        return token

    @staticmethod
    def _expect(response: httpx.Response, expected: tuple[int, ...] = (201, 204)) -> None:
        if response.status_code not in expected:
            raise RuntimeError(f"Keycloak admin request failed with HTTP {response.status_code}")

    def _configure_realm(self) -> None:
        with httpx.Client(timeout=10.0) as client:
            admin_token = self._admin_token(client)
            headers = {"Authorization": f"Bearer {admin_token}"}
            self._expect(
                client.post(
                    f"{self.internal_base_url}/admin/realms",
                    headers=headers,
                    json={
                        "realm": self.realm,
                        "enabled": True,
                        "accessTokenLifespan": 120,
                        "sslRequired": "none",
                    },
                )
            )

            scope_ids: dict[str, str] = {}
            for scope in (READ_SCOPE, EXECUTE_SCOPE, REVIEW_SCOPE):
                response = client.post(
                    f"{self.internal_base_url}/admin/realms/{self.realm}/client-scopes",
                    headers=headers,
                    json={
                        "name": scope,
                        "protocol": "openid-connect",
                        "attributes": {
                            "include.in.token.scope": "true",
                            "display.on.consent.screen": "false",
                        },
                    },
                )
                self._expect(response)
                location = response.headers.get("location", "")
                scope_id = location.rsplit("/", 1)[-1]
                if not scope_id:
                    raise RuntimeError("Keycloak did not return a client-scope identifier")
                scope_ids[scope] = scope_id

            primary_uuid = self._create_client(
                client,
                headers,
                client_id=self.client_id,
                client_secret=self.client_secret,
                include_audience=True,
            )
            wrong_uuid = self._create_client(
                client,
                headers,
                client_id=self.wrong_audience_client_id,
                client_secret=self.wrong_audience_secret,
                include_audience=False,
            )
            public_uuid = self._create_public_client(client, headers)
            for client_uuid in (primary_uuid, wrong_uuid, public_uuid):
                for scope_id in scope_ids.values():
                    self._expect(
                        client.put(
                            f"{self.internal_base_url}/admin/realms/{self.realm}/clients/"
                            f"{client_uuid}/optional-client-scopes/{scope_id}",
                            headers=headers,
                        ),
                        expected=(204,),
                    )
            self._expect(
                client.post(
                    f"{self.internal_base_url}/admin/realms/{self.realm}/users",
                    headers=headers,
                    json={
                        "username": self.test_username,
                        "email": f"{self.test_username}@example.invalid",
                        "firstName": "Video",
                        "lastName": "Studio",
                        "enabled": True,
                        "emailVerified": True,
                        "credentials": [
                            {
                                "type": "password",
                                "value": self.test_password,
                                "temporary": False,
                            }
                        ],
                    },
                )
            )

    def _create_client(
        self,
        client: httpx.Client,
        headers: dict[str, str],
        *,
        client_id: str,
        client_secret: str,
        include_audience: bool,
    ) -> str:
        protocol_mappers = []
        if include_audience:
            protocol_mappers.append(
                {
                    "name": "video-studio-resource-audience",
                    "protocol": "openid-connect",
                    "protocolMapper": "oidc-audience-mapper",
                    "config": {
                        "included.custom.audience": self.resource_url,
                        "access.token.claim": "true",
                        "id.token.claim": "false",
                    },
                }
            )
        response = client.post(
            f"{self.internal_base_url}/admin/realms/{self.realm}/clients",
            headers=headers,
            json={
                "clientId": client_id,
                "secret": client_secret,
                "enabled": True,
                "protocol": "openid-connect",
                "publicClient": False,
                "serviceAccountsEnabled": True,
                "standardFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "fullScopeAllowed": False,
                "protocolMappers": protocol_mappers,
            },
        )
        self._expect(response)
        client_uuid = response.headers.get("location", "").rsplit("/", 1)[-1]
        if not client_uuid:
            raise RuntimeError("Keycloak did not return a client identifier")
        return client_uuid

    def _create_public_client(
        self, client: httpx.Client, headers: dict[str, str]
    ) -> str:
        response = client.post(
            f"{self.internal_base_url}/admin/realms/{self.realm}/clients",
            headers=headers,
            json={
                "clientId": self.public_client_id,
                "enabled": True,
                "protocol": "openid-connect",
                "publicClient": True,
                "serviceAccountsEnabled": False,
                "standardFlowEnabled": True,
                "implicitFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "fullScopeAllowed": False,
                "redirectUris": [self.redirect_uri],
                "webOrigins": [],
                "attributes": {"pkce.code.challenge.method": "S256"},
                "protocolMappers": [
                    {
                        "name": "video-studio-resource-audience",
                        "protocol": "openid-connect",
                        "protocolMapper": "oidc-audience-mapper",
                        "config": {
                            "included.custom.audience": self.resource_url,
                            "access.token.claim": "true",
                            "id.token.claim": "false",
                        },
                    }
                ],
            },
        )
        self._expect(response)
        client_uuid = response.headers.get("location", "").rsplit("/", 1)[-1]
        if not client_uuid:
            raise RuntimeError("Keycloak did not return a public-client identifier")
        return client_uuid

    def token(self, scopes: tuple[str, ...], *, correct_audience: bool = True) -> str:
        client_id = self.client_id if correct_audience else self.wrong_audience_client_id
        client_secret = self.client_secret if correct_audience else self.wrong_audience_secret
        response = httpx.post(
            f"{self.internal_base_url}/realms/{self.realm}/protocol/openid-connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
                "scope": " ".join(scopes),
            },
            timeout=10.0,
        )
        response.raise_for_status()
        token = response.json().get("access_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError("Keycloak access-token response was malformed")
        return token

    def browser_authorization_redirect(
        self,
        authorization_endpoint: str,
        *,
        state: str,
        verifier: str,
        scopes: tuple[str, ...],
    ) -> str:
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        parameters = {
            "response_type": "code",
            "client_id": self.public_client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(("openid", *scopes)),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        with httpx.Client(
            timeout=10.0,
            follow_redirects=False,
            verify=str(self.ca_path),
            trust_env=False,
        ) as browser:
            login_page = browser.get(f"{authorization_endpoint}?{urlencode(parameters)}")
            if login_page.status_code != 200:
                raise OAuthFlowError(
                    f"authorization endpoint returned HTTP {login_page.status_code}"
                )
            parser = LoginFormParser()
            parser.feed(login_page.text)
            if not parser.action:
                raise OAuthFlowError("Keycloak login form was not present")
            form = dict(parser.fields)
            form["username"] = self.test_username
            form["password"] = self.test_password
            form.setdefault("credentialId", "")
            signed_in = browser.post(
                urljoin(str(login_page.url), parser.action), data=form
            )
            if signed_in.status_code not in (302, 303):
                explanation = " ".join(
                    re.sub(r"<[^>]+>", " ", signed_in.text).split()
                )[-500:]
                raise OAuthFlowError(
                    f"temporary-user login returned HTTP {signed_in.status_code}; "
                    f"fields={sorted(form)} cookies="
                    f"{[(cookie.name, cookie.secure, cookie.domain, cookie.path) for cookie in browser.cookies.jar]}: "
                    f"{explanation}"
                )
            location = signed_in.headers.get("location")
            if not location:
                raise OAuthFlowError("Keycloak login did not return a callback redirect")
            return location

    def parse_callback(self, location: str, *, expected_state: str) -> str:
        expected = urlparse(self.redirect_uri)
        callback = urlparse(location)
        if (
            callback.scheme,
            callback.hostname,
            callback.port,
            callback.path,
        ) != (
            expected.scheme,
            expected.hostname,
            expected.port,
            expected.path,
        ):
            raise OAuthFlowError(
                "authorization callback did not use the exact redirect URI: "
                f"got={(callback.scheme, callback.hostname, callback.port, callback.path)!r} "
                f"expected={(expected.scheme, expected.hostname, expected.port, expected.path)!r}"
            )
        parameters = parse_qs(callback.query)
        if parameters.get("state") != [expected_state]:
            raise OAuthFlowError("authorization callback state did not match")
        if parameters.get("iss") not in (None, [self.issuer]):
            raise OAuthFlowError("authorization callback issuer did not match")
        code = parameters.get("code", [""])[0]
        if not code:
            raise OAuthFlowError("authorization callback omitted the code")
        return code

    def exchange_code(
        self,
        token_endpoint: str,
        *,
        code: str,
        verifier: str,
        redirect_uri: str | None = None,
    ) -> httpx.Response:
        return httpx.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "client_id": self.public_client_id,
                "redirect_uri": redirect_uri or self.redirect_uri,
                "code": code,
                "code_verifier": verifier,
            },
            timeout=10.0,
            verify=str(self.ca_path),
            trust_env=False,
        )

    def verify_discovery(self) -> None:
        response = httpx.get(
            f"{self.issuer}/.well-known/openid-configuration",
            timeout=10.0,
            verify=str(self.ca_path),
            trust_env=False,
        )
        response.raise_for_status()
        document = response.json()
        if document.get("issuer") != self.issuer or document.get("jwks_uri") != self.jwks_url:
            raise RuntimeError("Keycloak discovery did not match the configured issuer/JWKS")


def unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def write_test_certificates(directory: Path) -> Path:
    now = datetime.now(UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Video Studio E2E Test CA")])
    ca_certificate = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=False,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    server_certificate = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )

    ca_path = directory / "test-ca.pem"
    certificate_path = directory / "server.pem"
    key_path = directory / "server-key.pem"
    ca_path.write_bytes(ca_certificate.public_bytes(serialization.Encoding.PEM))
    certificate_path.write_bytes(server_certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return ca_path


class TlsReverseProxy:
    def __init__(
        self,
        directory: Path,
        tls_port: int,
        gateway_port: int,
        keycloak_port: int,
    ) -> None:
        marker = f"{os.getpid()}-{uuid.uuid4().hex[:10]}"
        self.container = f"video-studio-http-nginx-e2e-{marker}"
        self.directory = directory
        self.tls_port = tls_port
        self.gateway_port = gateway_port
        self.keycloak_port = keycloak_port

    def start(self) -> None:
        config = self.directory / "nginx.conf"
        config.write_text(
            "\n".join(
                [
                    "events {}",
                    "http {",
                    "  access_log off;",
                    "  server {",
                    "    listen 8443 ssl;",
                    "    server_name localhost;",
                    "    ssl_certificate /certs/server.pem;",
                    "    ssl_certificate_key /certs/server-key.pem;",
                    "    ssl_protocols TLSv1.2 TLSv1.3;",
                    "    location /mcp {",
                    f"      proxy_pass http://host.docker.internal:{self.gateway_port};",
                    "      proxy_http_version 1.1;",
                    "      proxy_set_header Host $http_host;",
                    "      proxy_set_header X-Forwarded-Proto https;",
                    "      proxy_buffering off;",
                    "    }",
                    "    location /.well-known/oauth-protected-resource {",
                    f"      proxy_pass http://host.docker.internal:{self.gateway_port};",
                    "      proxy_http_version 1.1;",
                    "      proxy_set_header Host $http_host;",
                    "      proxy_set_header X-Forwarded-Proto https;",
                    "      proxy_buffering off;",
                    "    }",
                    "    location / {",
                    f"      proxy_pass http://host.docker.internal:{self.keycloak_port};",
                    "      proxy_http_version 1.1;",
                    "      proxy_set_header Host $http_host;",
                    "      proxy_set_header X-Forwarded-Host $http_host;",
                    "      proxy_set_header X-Forwarded-Proto https;",
                    f"      proxy_set_header X-Forwarded-Port {self.tls_port};",
                    "      proxy_buffering off;",
                    "    }",
                    "  }",
                    "}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        run_docker(
            [
                "run",
                "--rm",
                "--detach",
                "--name",
                self.container,
                "--publish",
                f"127.0.0.1:{self.tls_port}:8443",
                "--add-host",
                "host.docker.internal:host-gateway",
                "--volume",
                f"{self.directory}:/certs:ro",
                "--volume",
                f"{config}:/etc/nginx/nginx.conf:ro",
                NGINX_IMAGE,
            ]
        )

    def stop(self) -> None:
        run_docker(["rm", "--force", self.container], check=False)


@contextlib.asynccontextmanager
async def serve_gateway(app: Any) -> AsyncIterator[int]:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    port = int(listener.getsockname()[1])
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="warning",
            access_log=False,
            server_header=False,
            proxy_headers=False,
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            if task.done():
                await task
                raise RuntimeError("gateway server stopped before startup")
            if time.monotonic() >= deadline:
                raise RuntimeError("gateway server did not start")
            await asyncio.sleep(0.05)
        yield port
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=15)


def write_fake_backend(directory: Path) -> Path:
    executable = directory / "fake-video-studio-mcp"
    tool_names = sorted(TOOL_SCOPES)
    schemas = {name: backend_schema(name) for name in tool_names}
    source = f"""#!{sys.executable}
import anyio
import json
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

TOOLS = {tool_names!r}
SCHEMAS = json.loads({json.dumps(schemas)!r})
server = Server("video-studio-http-e2e-backend")

@server.list_tools()
async def list_tools():
    return [types.Tool(name=name, description=f"Fake {{name}}", inputSchema=SCHEMAS[name]) for name in TOOLS]

@server.call_tool()
async def call_tool(name, arguments):
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps({{"tool": name, "arguments": arguments}}))],
        structuredContent={{"tool": name, "arguments": arguments}},
        _meta={{"backend": "isolated-keycloak-e2e"}},
    )

async def main():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())

anyio.run(main)
"""
    executable.write_text(source, encoding="utf-8")
    executable.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return executable


def gateway_config(
    fixture: KeycloakFixture,
    workspace: Path,
    backend: Path,
    media_tools_root: Path,
) -> HttpMcpConfig:
    projects = workspace / "projects"
    return HttpMcpConfig(
        host="127.0.0.1",
        port=8765,
        resource_url=fixture.resource_url,
        issuer=fixture.issuer,
        audience=fixture.resource_url,
        jwks_url=fixture.jwks_url,
        algorithms=("RS256",),
        allowed_origins=(fixture.resource_url.removesuffix("/mcp"),),
        allowed_hosts=(urlparse(fixture.resource_url).netloc,),
        workspace_root=workspace.resolve(),
        projects_root=projects.resolve(),
        media_tools_root=media_tools_root.resolve(strict=True),
        backend_command=backend.resolve(),
        jwks_cache_seconds=30,
        jwks_min_refresh_seconds=1,
        session_idle_timeout_seconds=60,
        max_sessions=8,
    )


async def backend_tools(backend: Path, *, backend_home: Path | None = None) -> set[str]:
    environment = get_default_environment()
    if backend_home is not None:
        environment["HOME"] = str(backend_home)
    parameters = StdioServerParameters(
        command=str(backend),
        args=[],
        env=environment,
    )
    with open(os.devnull, "w", encoding="utf-8") as diagnostics:
        async with stdio_client(parameters, errlog=diagnostics) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return {tool.name for tool in (await session.list_tools()).tools}


def isolated_candidate(context_path: Path) -> tuple[Path, Path, str, Path, Path, Path]:
    try:
        context = json.loads(context_path.read_text(encoding="utf-8"))
        home = Path(context["home"]).resolve(strict=True)
        workspace = Path(context["workspace"]).resolve(strict=True)
        project = Path(context["project"]).resolve(strict=True)
        source = Path(context["source"]).resolve(strict=True)
        qa_root = Path(context["qa"]).resolve(strict=True)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("isolated candidate context is invalid") from error
    backend = home / ".local/share/video-studio/bin/video-studio-mcp"
    if backend.is_symlink() or not backend.is_file() or not os.access(backend, os.X_OK):
        raise RuntimeError("isolated candidate backend is not installed")
    if project.parent != workspace / "projects":
        raise RuntimeError("isolated candidate project is outside its workspace")
    original = Path.home() / ".local/bin/video-studio-mcp"
    try:
        if original.exists() and backend.samefile(original):
            raise RuntimeError("refusing to invoke the original Haru runtime")
    except OSError as error:
        raise RuntimeError("could not verify isolated backend identity") from error
    return backend.resolve(), workspace, project.name, home, source, qa_root


@contextlib.asynccontextmanager
async def sdk_session(
    resource_url: str, token: str, ca_path: Path
) -> AsyncIterator[ClientSession]:
    origin = resource_url.removesuffix("/mcp")
    headers = {"Authorization": f"Bearer {token}", "Origin": origin}
    async with httpx.AsyncClient(
        verify=str(ca_path),
        headers=headers,
        timeout=httpx.Timeout(120.0, connect=10.0),
    ) as client:
        async with streamable_http_client(
            resource_url, http_client=client
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def raw_post(
    resource_url: str, token: str, ca_path: Path, *, origin: str | None = None
) -> httpx.Response:
    async with httpx.AsyncClient(verify=str(ca_path)) as client:
        return await client.post(
            resource_url,
            json={},
            headers={
                "Authorization": f"Bearer {token}",
                "Origin": origin or resource_url.removesuffix("/mcp"),
            },
        )


def _redacted(value: str, token: str) -> str:
    return value.replace(token, "<redacted-token>")


def claude_mcp_healthcheck(
    cli: Path,
    *,
    resource_url: str,
    token: str,
    ca_path: Path,
    root: Path,
) -> str:
    try:
        executable = cli.resolve(strict=True)
    except OSError as error:
        raise RuntimeError("Claude Code executable is unavailable") from error
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError("Claude Code executable is not executable")

    client_root = root / "claude-client"
    home = client_root / "home"
    config_dir = client_root / "config"
    cwd = client_root / "cwd"
    for directory in (client_root, home, config_dir, cwd):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    server_name = "video-studio-e2e"
    config_path = client_root / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    server_name: {
                        "type": "http",
                        "url": resource_url,
                        "headers": {"Authorization": f"Bearer {token}"},
                    }
                }
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    config_path.chmod(0o600)
    environment = {
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(config_dir),
        "NODE_EXTRA_CA_CERTS": str(ca_path),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "TERM": "dumb",
        "NO_COLOR": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }

    def invoke(*arguments: str) -> tuple[int, str]:
        completed = subprocess.run(
            [str(executable), *arguments],
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=45,
            check=False,
        )
        combined = _redacted(completed.stdout + "\n" + completed.stderr, token)
        return completed.returncode, combined

    def connected_line(output: str) -> bool:
        plain = re.sub(r"\x1b\[[0-9;]*m", "", output)
        health_lines = [
            line.strip()
            for line in plain.splitlines()
            if line.strip()
            and re.search(r"[✓✔]\s*Connected\b", line, re.IGNORECASE)
        ]
        return len(health_lines) == 1 and server_name in health_lines[0]

    try:
        version_code, version_output = invoke("--version")
        if version_code != 0:
            raise RuntimeError("Claude Code version probe failed")
        version = next(
            (line.strip() for line in version_output.splitlines() if line.strip()),
            "unknown-version",
        )
        common = (
            "--strict-mcp-config",
            "--mcp-config",
            str(config_path),
            "mcp",
        )
        list_code, list_output = invoke(*common, "list")
        scope = "strict inline config"
        if list_code != 0 or not connected_line(list_output):
            add_code, _add_output = invoke(
                "mcp",
                "add",
                "--transport",
                "http",
                "--scope",
                "user",
                server_name,
                resource_url,
            )
            settings_path = config_dir / ".claude.json"
            if add_code != 0 or settings_path.is_symlink() or not settings_path.is_file():
                raise RuntimeError("Claude Code isolated MCP configuration failed")
            try:
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
                servers = settings["mcpServers"]
                server = servers[server_name]
            except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
                raise RuntimeError("Claude Code isolated MCP settings were malformed") from error
            if (
                set(servers) != {server_name}
                or server.get("type") != "http"
                or server.get("url") != resource_url
            ):
                raise RuntimeError("Claude Code isolated MCP settings contained unexpected servers")
            server["headers"] = {"Authorization": f"Bearer {token}"}
            temporary = settings_path.with_name(".claude.json.tmp")
            temporary.write_text(
                json.dumps(settings, separators=(",", ":")), encoding="utf-8"
            )
            temporary.chmod(0o600)
            os.replace(temporary, settings_path)
            settings_path.chmod(0o600)
            list_code, list_output = invoke("mcp", "list")
            if list_code != 0 or not connected_line(list_output):
                raise RuntimeError("Claude Code isolated user-scope MCP health check failed")
            common = ("mcp",)
            scope = "isolated user-scope fallback"
        get_code, get_output = invoke(*common, "get", server_name)
        get_plain = re.sub(r"\x1b\[[0-9;]*m", "", get_output)
        if get_code != 0 or server_name not in get_plain or resource_url not in get_plain:
            raise RuntimeError("Claude Code strict MCP get did not return the isolated server")
        return f"{version} ({scope})"
    finally:
        for sensitive in (
            config_path,
            config_dir / ".claude.json",
            config_dir / ".claude.json.tmp",
        ):
            try:
                sensitive.unlink()
            except FileNotFoundError:
                pass


def structured_result(result: Any, operation: str) -> dict[str, Any]:
    if result.isError or not isinstance(result.structuredContent, dict):
        raise RuntimeError(f"{operation} returned an MCP error")
    return result.structuredContent


def nested_string(value: Any, key: str) -> str | None:
    if isinstance(value, dict):
        candidate = value.get(key)
        if isinstance(candidate, str):
            return candidate
        for item in value.values():
            found = nested_string(item, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = nested_string(item, key)
            if found is not None:
                return found
    return None


async def render_and_export(
    session: ClientSession,
    project_id: str,
    *,
    existing_job_id: str | None = None,
) -> None:
    suffix = uuid.uuid4().hex[:12]
    claim = structured_result(
        await session.call_tool(
            "lease_claim",
            {
                "schema_version": 1,
                "project_id": project_id,
                "owner": "authenticated",
                "ttl_seconds": 600,
                "idempotency_key": f"http-e2e-claim-{suffix}",
            },
        ),
        "lease_claim",
    )
    lease_id = nested_string(claim, "lease_id")
    if lease_id is None:
        raise RuntimeError("lease_claim omitted lease_id")

    try:
        if existing_job_id is None:
            started = structured_result(
                await session.call_tool(
                    "run_next",
                    {
                        "schema_version": 1,
                        "project_id": project_id,
                        "owner": "authenticated",
                        "lease_id": lease_id,
                        "runner": "render-project",
                        "idempotency_key": f"http-e2e-render-{suffix}",
                    },
                ),
                "run_next render-project",
            )
            if started.get("outcome") != "ok" or started.get("code") != "gate_started":
                raise RuntimeError("fresh render submission did not return gate_started")
            job_id = nested_string(started, "job_id")
        else:
            job_id = existing_job_id
        if job_id is None or not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise RuntimeError("render submission omitted canonical job_id")

        deadline = time.monotonic() + 300
        final_job: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            envelope = structured_result(
                await session.call_tool(
                    "job_status",
                    {
                        "schema_version": 1,
                        "project_id": project_id,
                        "job_id": job_id,
                    },
                ),
                "job_status",
            )
            status = nested_string(envelope, "status")
            if status == "succeeded":
                final_job = envelope
                break
            if status in {"failed", "cancelled", "interrupted"}:
                raise RuntimeError(f"render job reached terminal status {status}")
            await asyncio.sleep(1)
        if final_job is None:
            raise RuntimeError("render job did not finish within 300 seconds")

        delivery = structured_result(
            await session.call_tool(
                "delivery_status",
                {"schema_version": 1, "project_id": project_id},
            ),
            "delivery_status",
        )
        if delivery.get("outcome") != "ok":
            raise RuntimeError("delivery_status was not ready after successful render")
        exported = structured_result(
            await session.call_tool(
                "export_delivery",
                {
                    "schema_version": 1,
                    "project_id": project_id,
                    "owner": "authenticated",
                    "lease_id": lease_id,
                    "idempotency_key": f"http-e2e-export-{suffix}",
                    "diagnostic": False,
                },
            ),
            "export_delivery",
        )
        if exported.get("outcome") != "ok":
            raise RuntimeError("export_delivery did not complete")
        if existing_job_id is None:
            print(f"PASS: authenticated render submission {job_id} succeeded and exported")
        else:
            print(f"PASS: authenticated read/export of existing render job {job_id}")
    finally:
        released = await session.call_tool(
            "lease_release",
            {
                "schema_version": 1,
                "project_id": project_id,
                "owner": "authenticated",
                "lease_id": lease_id,
                "idempotency_key": f"http-e2e-release-{suffix}",
            },
        )
        if released.isError:
            raise RuntimeError("lease_release failed")


async def authorization_code_token(
    fixture: KeycloakFixture, config: HttpMcpConfig, ca_path: Path
) -> str:
    metadata_url = (
        f"{config.resource_url.removesuffix('/mcp')}"
        "/.well-known/oauth-protected-resource/mcp"
    )
    async with httpx.AsyncClient(verify=str(ca_path), trust_env=False, timeout=10.0) as client:
        metadata_response = await client.get(metadata_url)
        metadata_response.raise_for_status()
        metadata = metadata_response.json()
        if metadata.get("resource") != config.resource_url:
            raise OAuthFlowError("protected-resource metadata named a different resource")
        if metadata.get("authorization_servers") != [fixture.issuer]:
            raise OAuthFlowError("protected-resource metadata named a different issuer")
        if set(metadata.get("scopes_supported", [])) != {
            READ_SCOPE,
            EXECUTE_SCOPE,
            REVIEW_SCOPE,
        }:
            raise OAuthFlowError("protected-resource metadata scopes were incomplete")

        discovery_response = await client.get(
            f"{fixture.issuer}/.well-known/openid-configuration"
        )
        discovery_response.raise_for_status()
        discovery = discovery_response.json()
    authorization_endpoint = discovery.get("authorization_endpoint")
    token_endpoint = discovery.get("token_endpoint")
    if not isinstance(authorization_endpoint, str) or not isinstance(token_endpoint, str):
        raise OAuthFlowError("OIDC discovery omitted authorization or token endpoint")

    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(32)
    location = await asyncio.to_thread(
        fixture.browser_authorization_redirect,
        authorization_endpoint,
        state=state,
        verifier=verifier,
        scopes=(READ_SCOPE, EXECUTE_SCOPE),
    )
    try:
        fixture.parse_callback(location, expected_state=f"wrong-{state}")
    except OAuthFlowError:
        pass
    else:
        raise OAuthFlowError("callback state mismatch was not rejected")
    code = fixture.parse_callback(location, expected_state=state)
    exchange = await asyncio.to_thread(
        fixture.exchange_code,
        token_endpoint,
        code=code,
        verifier=verifier,
    )
    if exchange.status_code != 200:
        raise OAuthFlowError(f"valid PKCE exchange returned HTTP {exchange.status_code}")
    token = exchange.json().get("access_token")
    if not isinstance(token, str) or not token:
        raise OAuthFlowError("PKCE token exchange omitted the access token")
    replay = await asyncio.to_thread(
        fixture.exchange_code,
        token_endpoint,
        code=code,
        verifier=verifier,
    )
    if replay.status_code < 400:
        raise OAuthFlowError("authorization code could be used twice")

    mismatch_verifier = secrets.token_urlsafe(64)
    mismatch_state = secrets.token_urlsafe(32)
    mismatch_location = await asyncio.to_thread(
        fixture.browser_authorization_redirect,
        authorization_endpoint,
        state=mismatch_state,
        verifier=mismatch_verifier,
        scopes=(READ_SCOPE,),
    )
    mismatch_code = fixture.parse_callback(mismatch_location, expected_state=mismatch_state)
    verifier_rejection = await asyncio.to_thread(
        fixture.exchange_code,
        token_endpoint,
        code=mismatch_code,
        verifier=secrets.token_urlsafe(64),
    )
    if verifier_rejection.status_code < 400:
        raise OAuthFlowError("mismatched PKCE verifier was accepted")

    redirect_verifier = secrets.token_urlsafe(64)
    redirect_state = secrets.token_urlsafe(32)
    redirect_location = await asyncio.to_thread(
        fixture.browser_authorization_redirect,
        authorization_endpoint,
        state=redirect_state,
        verifier=redirect_verifier,
        scopes=(READ_SCOPE,),
    )
    redirect_code = fixture.parse_callback(redirect_location, expected_state=redirect_state)
    wrong_redirect = fixture.redirect_uri.replace("/callback", "/wrong-callback")
    redirect_rejection = await asyncio.to_thread(
        fixture.exchange_code,
        token_endpoint,
        code=redirect_code,
        verifier=redirect_verifier,
        redirect_uri=wrong_redirect,
    )
    if redirect_rejection.status_code < 400:
        raise OAuthFlowError("mismatched redirect URI was accepted")
    return token


async def exercise_gateway(
    fixture: KeycloakFixture,
    config: HttpMcpConfig,
    ca_path: Path,
    *,
    project_id: str,
    fake_backend: bool,
    render_project: bool,
    existing_job_id: str | None,
    claude_cli: Path | None,
    client_root: Path,
) -> None:
    read_execute_token = await authorization_code_token(fixture, config, ca_path)
    review_only_token = fixture.token((REVIEW_SCOPE,))
    wrong_audience_token = fixture.token((READ_SCOPE,), correct_audience=False)

    decoded = jwt.decode(read_execute_token, options={"verify_signature": False})
    audience = decoded.get("aud")
    audiences = [audience] if isinstance(audience, str) else audience
    if decoded.get("iss") != fixture.issuer or config.resource_url not in (audiences or []):
        raise RuntimeError("Keycloak did not issue the expected issuer/audience claims")
    token_scopes = set(str(decoded.get("scope", "")).split())
    if not {READ_SCOPE, EXECUTE_SCOPE}.issubset(token_scopes):
        raise RuntimeError("Keycloak did not issue the requested Video Studio scopes")
    if claude_cli is not None:
        version = await asyncio.to_thread(
            claude_mcp_healthcheck,
            claude_cli,
            resource_url=config.resource_url,
            token=read_execute_token,
            ca_path=ca_path,
            root=client_root,
        )
        print(f"PASS: Claude Code {version} strict isolated MCP health check")

    async with sdk_session(config.resource_url, read_execute_token, ca_path) as session:
        tools = await session.list_tools()
        names = {tool.name for tool in tools.tools}
        expected = {
            name
            for name, scope in TOOL_SCOPES.items()
            if scope in {READ_SCOPE, EXECUTE_SCOPE}
        }
        if names != expected or "visual_qa" in names:
            raise RuntimeError("scope-filtered tool discovery was incorrect")
        if names.intersection(
            {
                "publish",
                "approve_publish",
                "prepare_publish_approval",
                "prepare_publish",
                "replace_thumbnail",
                "reconcile_upload",
            }
        ):
            raise RuntimeError("a publishing tool escaped onto the HTTP surface")

        workspace = await session.call_tool("workspace_info", {"schema_version": 1})
        projects = await session.call_tool("project_list", {"schema_version": 1})
        status = await session.call_tool("status", {"schema_version": 1, "project_id": project_id})
        if workspace.isError or projects.isError:
            raise RuntimeError("fixed-workspace catalog calls failed")
        if status.isError or status.structuredContent is None:
            raise RuntimeError("SDK status call through the stdio backend failed")
        serialized = json.dumps(
            [workspace.structuredContent, projects.structuredContent, status.structuredContent]
        )
        if str(config.workspace_root) in serialized:
            raise RuntimeError("server workspace path leaked through HTTP")

        if fake_backend:
            lease = await session.call_tool(
                "lease_claim",
                {
                    "schema_version": 1,
                    "project_id": project_id,
                    "owner": "caller-impersonation-attempt",
                    "ttl_seconds": 60,
                    "idempotency_key": "keycloak-e2e-lease",
                },
            )
            forwarded_owner = (lease.structuredContent or {}).get("arguments", {}).get("owner")
            if lease.isError or not str(forwarded_owner).startswith("http:"):
                raise RuntimeError("authenticated principal was not bound to lease owner")
            staged = await session.call_tool(
                "artifact_stage",
                {
                    "schema_version": 1,
                    "project_id": project_id,
                    "owner": "ignored",
                    "lease_id": "lease",
                    "role": "script_notes",
                    "inline_text": "Authenticated inline note",
                    "idempotency_key": "keycloak-e2e-stage",
                },
            )
            if staged.isError:
                raise RuntimeError("public inline intake call failed")
        if render_project:
            if fake_backend:
                raise RuntimeError("render mode requires an isolated installed backend")
            await render_and_export(
                session,
                project_id,
                existing_job_id=existing_job_id,
            )

    async with sdk_session(config.resource_url, review_only_token, ca_path) as session:
        names = {tool.name for tool in (await session.list_tools()).tools}
        expected_review = {
            name for name, scope in TOOL_SCOPES.items() if scope == REVIEW_SCOPE
        }
        if names != expected_review:
            raise RuntimeError("review-only token saw tools outside its scope")
        blocked = await session.call_tool(
            "status", {"schema_version": 1, "project_id": project_id}
        )
        if not blocked.isError or "studio:read" not in blocked.content[0].text:
            raise RuntimeError("review-only token was not blocked from read tools")
        render = await session.call_tool(
            "run_next",
            {
                "schema_version": 1,
                "project_id": project_id,
                "owner": "ignored",
                "lease_id": "lease",
                "runner": "render-project",
                "idempotency_key": "forbidden-render",
            },
        )
        if not render.isError or "studio:execute" not in render.content[0].text:
            raise RuntimeError("review-only token could execute a renderer")
        if fake_backend:
            added = await session.call_tool(
                "review_add",
                {
                    "schema_version": 1,
                    "project_id": project_id,
                    "client_id": "12345678-1234-4234-8234-123456789abc",
                    "package_id": "a" * 64,
                    "asset_id": "video-current",
                    "asset_sha256": "b" * 64,
                    "timestamp_seconds": 0,
                    "body": "Authenticated review note",
                    "idempotency_key": "keycloak-e2e-review",
                },
            )
            if added.isError:
                raise RuntimeError("review-scope comment call failed")

    wrong_audience = await raw_post(config.resource_url, wrong_audience_token, ca_path)
    if wrong_audience.status_code != 401:
        raise RuntimeError("wrong-audience Keycloak token was accepted")
    challenge = wrong_audience.headers.get("www-authenticate", "")
    if "resource_metadata=" not in challenge:
        raise RuntimeError("OAuth challenge omitted protected-resource metadata")

    wrong_origin = await raw_post(
        config.resource_url, read_execute_token, ca_path, origin="https://evil.example"
    )
    if wrong_origin.status_code != 403:
        raise RuntimeError("disallowed Origin was accepted")

    try:
        async with httpx.AsyncClient(verify=True, trust_env=False, timeout=5.0) as client:
            await client.get(
                f"{config.resource_url.removesuffix('/mcp')}"
                "/.well-known/oauth-protected-resource/mcp"
            )
    except httpx.TransportError:
        pass
    else:
        raise RuntimeError("TLS endpoint was accepted without the temporary trusted CA")


async def run_tls_integration(
    fixture: KeycloakFixture,
    config: HttpMcpConfig,
    ca_path: Path,
    certificate_directory: Path,
    tls_port: int,
    *,
    project_id: str,
    fake_backend: bool,
    render_project: bool,
    existing_job_id: str | None,
    claude_cli: Path | None,
) -> None:
    jwks_client = httpx.AsyncClient(
        verify=str(ca_path), trust_env=False, timeout=5.0, follow_redirects=False
    )
    verifier = JwksTokenVerifier(config, http_client=jwks_client)
    app = build_app(config, verifier=verifier)
    # SDK setup configures process logging. Keep authorization codes, session
    # codes, tokens, and query strings out of integration output.
    for logger_name in ("httpx", "httpcore", "mcp"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    keycloak_port = int(urlparse(fixture.internal_base_url).port or 0)
    try:
        async with serve_gateway(app) as upstream_port:
            proxy = TlsReverseProxy(
                certificate_directory, tls_port, upstream_port, keycloak_port
            )
            try:
                await asyncio.to_thread(proxy.start)
                metadata_url = (
                    f"{config.resource_url.removesuffix('/mcp')}"
                    "/.well-known/oauth-protected-resource/mcp"
                )
                deadline = time.monotonic() + 30
                async with httpx.AsyncClient(
                    verify=str(ca_path), trust_env=False, timeout=3.0
                ) as client:
                    while True:
                        try:
                            response = await client.get(metadata_url)
                            if response.status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        if time.monotonic() >= deadline:
                            raise RuntimeError("TLS reverse proxy did not become ready")
                        await asyncio.sleep(0.2)
                fixture.verify_discovery()
                await exercise_gateway(
                    fixture,
                    config,
                    ca_path,
                    project_id=project_id,
                    fake_backend=fake_backend,
                    render_project=render_project,
                    existing_job_id=existing_job_id,
                    claude_cli=claude_cli,
                    client_root=certificate_directory.parent,
                )
            finally:
                await asyncio.to_thread(proxy.stop)
    finally:
        await jwks_client.aclose()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run isolated Keycloak/TLS authentication against fake or installed MCP backend"
    )
    parser.add_argument(
        "--context",
        type=Path,
        help="isolated E2E context JSON whose HOME contains an installed candidate",
    )
    parser.add_argument(
        "--render-project",
        metavar="PROJECT_ID",
        help="submit/poll one real render and export; requires --context and a released project lease",
    )
    parser.add_argument(
        "--existing-job-id",
        help="poll/export a previously accepted canonical render job without submitting another",
    )
    parser.add_argument(
        "--claude-cli",
        type=Path,
        help="run no-inference Claude Code strict MCP list/get with isolated config",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.existing_job_id is not None and (
        args.render_project is None
        or re.fullmatch(r"[0-9a-f]{32}", args.existing_job_id) is None
    ):
        print("PENDING: --existing-job-id requires render mode and a canonical job ID", file=sys.stderr)
        return 2
    tls_port = unused_loopback_port()
    callback_port = unused_loopback_port()
    resource_url = f"https://localhost:{tls_port}/mcp"
    redirect_uri = f"http://127.0.0.1:{callback_port}/callback"
    with tempfile.TemporaryDirectory(prefix="video-studio-http-keycloak-") as temporary:
        root = Path(temporary)
        certificate_directory = root / "certificates"
        certificate_directory.mkdir()
        ca_path = write_test_certificates(certificate_directory)
        if args.context is not None:
            try:
                backend, workspace, project_id, backend_home, source_root, qa_root = isolated_candidate(
                    args.context.resolve(strict=True)
                )
                if args.render_project is not None and args.render_project != project_id:
                    raise RuntimeError("--render-project must name the isolated context project")
                os.environ["HOME"] = str(backend_home)
                if args.render_project is not None:
                    delivery_root = qa_root / "exports"
                    if delivery_root.is_symlink():
                        raise RuntimeError("isolated delivery root is a symlink")
                    delivery_root.mkdir(mode=0o700, parents=True, exist_ok=True)
                    os.environ["VIDEO_STUDIO_DELIVERY_ROOT"] = str(delivery_root)
                names = asyncio.run(backend_tools(backend, backend_home=backend_home))
            except (RuntimeError, OSError, httpx.HTTPError) as error:
                print(f"PENDING: isolated backend unavailable: {error}", file=sys.stderr)
                return 2
            missing = sorted(set(TOOL_SCOPES) - names)
            if missing:
                print(
                    "PENDING: isolated backend missing required HTTP tools: "
                    + ", ".join(missing),
                    file=sys.stderr,
                )
                return 2
            fake_backend = False
            media_tools_root = source_root / "media-tools"
        else:
            if args.render_project is not None:
                print("PENDING: --render-project requires --context", file=sys.stderr)
                return 2
            workspace = root / "workspace"
            initialize_workspace(workspace)
            project_id = "e2e-project"
            (workspace / "projects" / project_id).mkdir()
            backend = write_fake_backend(root)
            fake_backend = True
            media_tools_root = REPO_ROOT / "media-tools"
        print(f"Starting isolated {KEYCLOAK_IMAGE} authentication check", flush=True)
        fixture = KeycloakFixture(resource_url, redirect_uri, ca_path)
        try:
            fixture.start()
            config = gateway_config(
                fixture, workspace, backend, media_tools_root
            )
            asyncio.run(
                run_tls_integration(
                    fixture,
                    config,
                    ca_path,
                    certificate_directory,
                    tls_port,
                    project_id=project_id,
                    fake_backend=fake_backend,
                    render_project=args.render_project is not None,
                    existing_job_id=args.existing_job_id,
                    claude_cli=args.claude_cli,
                )
            )
            print("PASS: Keycloak authorization-code + PKCE S256 and exact callback checks")
            print("PASS: trusted-CA TLS reverse proxy and official SDK HTTP-to-stdio MCP")
            print("PASS: issuer, audience, scope, Origin, metadata, and principal binding checks")
            print(
                "PASS: "
                + ("isolated installed core backend" if not fake_backend else "current fake backend contract")
            )
            return 0
        except (RuntimeError, OSError, subprocess.SubprocessError, httpx.HTTPError) as error:
            print(f"FAIL: isolated Keycloak integration check: {error}", file=sys.stderr)
            return 1
        finally:
            fixture.stop()


if __name__ == "__main__":
    raise SystemExit(main())
