# Authenticated HTTP MCP

Video Studio's HTTP mode is a self-hosted policy gateway in front of the
promoted `~/.local/share/video-studio/bin/video-studio-mcp` stdio server. It uses the MCP Python
SDK's Streamable HTTP transport. It does not offer a hosted service and it does
not accept an executable path from a request or from TOML.

## Trust boundary

The process binds only to loopback. For a private server, terminate HTTPS in a
reverse proxy and forward to loopback. The configured public `resource_url`,
OAuth issuer, audience, JWKS URL, allowed Origins, workspace, and projects
directory are fixed for the lifetime of the process. The config file must be an
owner-controlled regular file and must not be group- or world-writable.

JWT access tokens require a configured asymmetric signature algorithm and valid
`iss`, `aud`, and `exp` claims. JWKS refresh, document size, and key count are
bounded. Invalid bearer requests receive an RFC 6750 challenge pointing to the
RFC 9728 protected-resource metadata document.

The HTTP tool scopes are:

- `studio:read`: `select`, `status`, `artifact_index`, `lease_status`, `verify`,
  `delivery_status`, `job_status`, and `job_logs`
- `studio:execute`: `create`, `record_selection`, lease mutation, `run_next`,
  `produce_artifact`, `export_delivery`, `job_cancel`, and `job_resume`
- `studio:review`: `visual_qa`, `pronunciation_review`, and `review_resolve`

`review_feedback` is also available under `studio:read`. Resolving or reopening
a note requires its canonical UUID plus the exact current review-package and
asset SHA-256 digests. It only changes note status; it does not record technical
QA, human acceptance, or publishing approval.

Publishing, upload reconciliation, publish approval, thumbnail replacement,
and runtime trust management are absent from the HTTP tool list. They remain
local operator actions.

Every `project_root` must be an existing direct child of the configured
`<workspace>/projects` directory. Symlink and traversal paths are rejected.
`projects_root` must equal that exact directory. Runners that need media tools
receive the repository's bundled `media-tools` path; callers cannot select a
tools directory or executable. `produce_artifact` accepts only an existing
regular file below the selected project's `.hvp/staging/` directory.
Delivery exports use only the operator-configured
`VIDEO_STUDIO_DELIVERY_ROOT`; HTTP requests have no destination field. Job
operations accept only the canonical 32-character lowercase hexadecimal job
identifier, and log reads are capped at 65,536 bytes.

The wire schemas retain the stdio `owner` field for compatibility, but the
gateway replaces it with a stable digest of the authenticated issuer, subject,
and OAuth client. A token cannot claim or operate another principal's lease.
The MCP SDK also binds each stateful HTTP session to the credential principal
that initialized it.

## Run

Create the workspace directories and install/promote the stable stdio runtime
before starting the gateway:

```sh
mkdir -p /srv/video-studio/workspace/projects
mkdir -p /srv/video-studio/exports
cp config/http.example.toml /secure/path/video-studio-http.toml
chmod 600 /secure/path/video-studio-http.toml
export VIDEO_STUDIO_DELIVERY_ROOT=/srv/video-studio/exports
scripts/video-studio-http --config /secure/path/video-studio-http.toml
```

The OAuth authorization server must issue access-token JWTs for the configured
audience and scopes. The gateway is a resource server; it does not host login,
client registration, or token issuance.

The stdio backend inherits the MCP SDK's restricted process baseline plus an
explicit allowlist used by reviewed runners: the delivery root, ElevenLabs
key/key-file and TTS settings, G2P model/Python settings, voice-rules directory,
cover asset directory, and Chromium path. Request fields never become
environment variables. Other parent values such as `PYTHONPATH`, `BASH_ENV`,
shell startup hooks, and unrelated secrets are not forwarded. Core runners
apply a second, operation-specific environment boundary before invoking media
or provider processes.

## Isolated Keycloak integration check

Maintainers with Docker can run the real authorization-server check:

```sh
uv run python tests/integration_http_keycloak.py
```

The check starts `quay.io/keycloak/keycloak:26.7.4` pinned to the tested image
digest and `nginx:1.27.5-alpine` pinned to its tested digest. Both use uniquely
named throwaway containers and random loopback ports. It creates a temporary
realm, test user, resource audience mapper, optional `studio:*` client scopes,
confidential service-account clients, and a public client restricted to PKCE
S256 and one exact loopback redirect URI.

The test discovers the authorization server from the HTTPS protected-resource
metadata, reads OIDC discovery, navigates the real Keycloak login form over a
trusted-test-CA TLS reverse proxy, checks callback state and issuer, exchanges
the one-time authorization code with its verifier, and rejects a mismatched
verifier and redirect URI. It uses that access token with the official MCP SDK
to initialize Streamable HTTP, list scoped tools, call the isolated fake stdio
backend, verify structured results and principal binding, and reject wrong
audience, scope, Origin, and an untrusted TLS certificate.

The login is deterministic HTTP form navigation rather than Playwright. It
submits the same Keycloak HTML form, cookies, redirect, and token exchange a
browser performs; no direct password grant is used for the public client. It
does not prove a particular external agent's browser-launch/callback adapter or
a production CA/reverse-proxy configuration. The script removes only its two
task-labelled containers in `finally` blocks and never reads, replaces, or
invokes an installed Video Studio runtime.
