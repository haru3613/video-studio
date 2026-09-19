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

- `studio:read`: `workspace_info`, `project_list`, `select`, `status`,
  `artifact_index`, `lease_status`, `verify`, `delivery_status`, `job_status`,
  and `job_logs`
- `studio:execute`: `create`, `record_selection`, lease mutation, `run_next`,
  `artifact_stage`, `artifact_import`, `produce_staged_artifact`,
  `export_delivery`, `job_cancel`, and `job_resume`
- `studio:review`: `visual_qa`, `pronunciation_review`, `review_add`, and
  `review_resolve`

`review_feedback` is also available under `studio:read` and returns the current
package ID plus typed assets. Adding a note requires a canonical client UUID,
current package and asset digests, a bounded body, and a valid media timestamp.
Resolving or reopening requires the comment UUID and the original package and
asset digests saved with that comment. Historical notes remain resolvable after
a rerender, including when their old media is no longer available. Review mutations use `studio:review`; they do not grant execute scope,
run renderers, or record technical QA, human acceptance, or publishing
approval.
Because review mutations intentionally do not require an execute lease, the
gateway namespaces their idempotency keys with the authenticated OAuth
principal before forwarding them. One principal cannot replay another
principal's review receipt by guessing its client-supplied key.

Publishing, upload reconciliation, publish approval, thumbnail replacement,
and runtime trust management are absent from the HTTP tool list. They remain
local operator actions.

HTTP tools accept a `project_id`, never `workspace_root`, `projects_root`,
`project_root`, `tools_root`, or a staging path. The gateway resolves each ID
to an existing direct child of its one configured workspace and rejects
traversal, case drift, and symlinks. Runners that need media tools receive the
repository's bundled `media-tools` path; callers cannot select a tools
directory or executable. The older absolute-path stdio contracts remain local
and are absent from HTTP schemas.

Remote intake uses `artifact_stage` with exactly one of `inbox_path` or
`inline_text`. `inbox_path` is relative to the operator-mounted workspace
`inbox/`; inline UTF-8 text is limited to 1 MiB and restricted to text/JSON
roles. The returned opaque stage ID can be passed to `artifact_import` for the
fixed non-canonical imports namespace, or to `produce_staged_artifact` for one
of seven reviewed text/JSON targets. Media, subtitles, code, final narration,
render output, QA, approval, and publishing receipts cannot be promoted through
this intake path. No SSH or manual write into a project's staging directory is
needed.
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

Initialize the workspace and install/promote the stable stdio runtime before
starting the gateway:

```sh
scripts/init-workspace /srv/video-studio/workspace
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

Default mode uses a temporary backend generated from the current HTTP
contract. It proves the Keycloak authorization matrix, PKCE callback exchange,
TLS trust, scope-filtered 27-tool HTTP surface, fixed-workspace translation,
inline intake, review-only mutation access, and execute-scope denial without
depending on an installed candidate.

After installing a candidate into an isolated E2E HOME, run the same matrix
against its real promoted stdio server:

```sh
uv run python tests/integration_http_keycloak.py \
  --context /tmp/video-studio-e2e-context.json
```

Real mode reads the isolated HOME, workspace, and project from that JSON and
probes the MCP tool surface before starting Docker. It refuses the original
Haru runtime and exits with a `PENDING` receipt when the candidate lacks a
required HTTP backend tool. It performs no render, provider, publishing, or
paid call.

For a release candidate whose demo project is ready and whose existing lease
has been released, maintainers can additionally exercise the authenticated
render/job/delivery path:

```sh
uv run python tests/integration_http_keycloak.py \
  --context /tmp/video-studio-e2e-context.json \
  --render-project demo
```

This mode derives the delivery root from the context's isolated QA directory,
claims a principal-bound project lease, submits `render-project`, polls the
opaque job for at most five minutes, checks delivery status, exports once, and
releases the lease. It still makes no provider, publishing, or paid call.

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
to initialize Streamable HTTP, list scoped tools, call the temporary stdio
backend or selected isolated candidate, verify structured results and principal
binding, and reject wrong audience, scope, Origin, and an untrusted TLS
certificate.

The login is deterministic HTTP form navigation rather than Playwright. It
submits the same Keycloak HTML form, cookies, redirect, and token exchange a
browser performs; no direct password grant is used for the public client. It
does not prove a particular external agent's browser-launch/callback adapter or
a production CA/reverse-proxy configuration. The script removes only its two
task-labelled containers in `finally` blocks. Fake mode never invokes an
installed runtime; real mode invokes only the candidate beneath the isolated
HOME supplied by the context file.
