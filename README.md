# Video Studio

Self-hosted video-production workflows for people and AI agents. Run the same
workflow from a CLI, a local stdio MCP server, or an authenticated HTTP MCP
server on your own machine. Video Studio does not provide a cloud service.

This is a development release candidate. Implementation and code review are
complete; see [validation](docs/validation.md) for tested workflows and limits.
Licensed under [Apache-2.0](LICENSE), copyright 2026 haru3613.

## What it does

- Plans and validates project artifacts, narration, subtitles, and cue-driven
  storyboards using the existing production contracts.
- Renders Remotion projects with FFmpeg verification and final audio mixing.
- Keeps durable background jobs, input snapshots, cancellation, retries, and
  version-bound results. Failed retakes preserve the previous final.
- Provides a local review UI with playback, comparisons, timestamped comments,
  and feedback that CLI/MCP agents can read and resolve.
- Separates technical delivery from pronunciation review, editorial judgment,
  human approval, and platform publishing.
- Includes optional ElevenLabs narration, Mandarin pronunciation tooling,
  reference-analysis import, YouTube workflows, and macOS signed approval code.
  These require the operator's own configuration and are not automatically enabled.

The core retains the original `haru.*` artifact schemas as a compatibility
contract. They are format names; no original channel, voice, credentials, or
production media are supplied. New public interfaces use `video_studio.*`.

New render receipts bind the complete render-input revision. Historical receipts
without that binding remain readable but are treated as stale; re-render the
project before a new technical delivery. They are never silently upgraded.

## Prerequisites

The current verification targets are macOS arm64 and Ubuntu 24.04 x86_64.
Platform support is only confirmed by the recorded checks, not this target list.

Install Git, [uv](https://docs.astral.sh/uv/), Rust/rustup (the repo pins its
toolchain), Node.js 22+, FFmpeg/FFprobe, and a local Chrome/Chromium browser.
On macOS, source verification also requires Swift 6.2 or newer (for example,
Xcode 26.2) for the native helper tests.
The optional `--speech` demo also needs [eSpeak NG](https://github.com/espeak-ng/espeak-ng).
No paid model account is needed for the example.

```sh
git clone https://github.com/haru3613/video-studio.git
cd video-studio
scripts/setup
scripts/verify
scripts/install
```

Installation requires clean committed source. It builds and activates an
immutable, content-addressed **self-built** runtime. This is source integrity,
not a claim of an independently signed official release. An unrelated existing
launcher is never overwritten. The CLI and stdio server are installed in:

```text
~/.local/share/video-studio/bin/video-studio
~/.local/share/video-studio/bin/video-studio-mcp
```

Add that directory to PATH, or use the full paths. The existing private Haru
launcher at `~/.local/bin/video-studio-mcp` is not replaced.

## First project

The primary path is **your own narration audio and matching SRT subtitles**.
No TTS account or API key is required. Follow the [user media walkthrough](docs/user-media.md)
to create a project, prepare its scene/data spec with `video-studio prepare`,
and render through the CLI. Images, video clips, diagrams, and background music
are selected in JSON; no template source edits are needed.

Speech generation is optional and requires an [explicit provider choice](docs/narration-providers.md).
The included ElevenLabs adapter is one choice, not a default. A provider can be
supplied by an installed adapter package; Video Studio provides no hosted TTS.

For a dependency-only smoke test, `scripts/prepare-example PROJECT --speech`
explicitly generates mechanical eSpeak NG demo speech. Without `--speech`, it
creates a labelled synchronization tone. Both are test material. See the
[template guide](templates/narrated/remotion/README.md) for the example workflow.

Accepted render work is not completed work. Use the returned job ID with
`video-studio job status` (or `logs`, `cancel`, `resume`). If you fix inputs after
a failed render, submit a new render request: it creates a new job for the new
revision and preserves the earlier history and final.

Every operation is available through `video-studio call TOOL --input request.json`;
`video-studio tools` lists the exact typed schemas.

## Review and export

```sh
video-studio ui --workspace "$HOME/VideoStudio"
```

Open the displayed localhost address and enter the one-time code from the
owner-only code file. The code expires after five minutes and is consumed once.
For a private server, use an SSH tunnel to this loopback UI. Comments remain
bound to the reviewed media version; resolving a comment is not a publication
approval.

`video-studio delivery status --project-root "$HOME/VideoStudio/projects/demo"`
performs fresh technical checks (`delivery_status` over MCP). `export_delivery` exports an
immutable bundle and requires a valid lease and idempotency key. Set
`VIDEO_STUDIO_DELIVERY_ROOT` to an existing absolute output directory in the
operator environment before launching CLI/MCP. Export manifests explicitly
record content checks that were not performed. The source tree has no publishing
channel or signing key configured, and upload remains held.

## Workspace backup and restore

Backup and restore are local administrator commands. They run from the active
installed source closure with the managed Python environment and are not exposed
as MCP or HTTP tools.

```sh
mkdir -p "$HOME/VideoStudioBackups"
video-studio workspace backup \
  --workspace "$HOME/VideoStudio" \
  --destination "$HOME/VideoStudioBackups"

video-studio workspace restore \
  --backup "$HOME/VideoStudioBackups/<backup-directory>" \
  --destination "$HOME/VideoStudioRestored"
```

Backup refuses active render jobs and takes the workspace-wide barrier so CLI,
MCP, intake, and review writes cannot cross the snapshot. Restore verifies the
manifest and file hashes and requires a new empty destination. Terminal job
history remains readable; unfinished jobs become interrupted with their prior
process authority revoked. Restore invalidates leases, publish approval,
template trust, and path-bound signatures, and rekeys review storage for the
new canonical path. Provider credentials, OAuth material,
browser/UI sessions, signing keys, and external attestation ledgers are excluded
and must be recovered separately by the operator.

## MCP

For local stdio, configure your MCP client to execute
`~/.local/share/video-studio/bin/video-studio-mcp`. Expand `~` if your client
requires an absolute path. No HTTP listener or OAuth service is needed.

For a private HTTP server, follow [HTTP deployment](docs/http-mcp.md). The
implementation uses OAuth JWT validation and Streamable HTTP, with a tested
self-hosted Keycloak reference. Run your own TLS proxy and authorization server;
there is no Video Studio account, subscription, or hosted control plane.

HTTP requests cannot choose arbitrary workspaces, executables, or export roots.
Publishing and runtime trust operations are not exposed over HTTP. The server
is a single operator's trusted workspace, not a sandbox for untrusted users'
Node/Remotion code. Custom templates require local owner approval through
`scripts/trust-template`; clients cannot grant themselves code trust.

## Development and limits

```sh
scripts/verify
uv run --locked python tests/integration_http_keycloak.py
```

The second command requires Docker and creates only disposable, task-specific
Keycloak/Nginx containers. It does not contact production accounts or call paid
media APIs. The first command runs source, Rust, Python, media, and (on macOS)
Swift fake-key checks; it is not publication permission.

See [contributing](CONTRIBUTING.md), [third-party notices](THIRD_PARTY_NOTICES.md),
[implementation scope](docs/implementation-plan.md), and
[source provenance](docs/source-import.json), and [security reporting](SECURITY.md).
Generated media, credentials,
provider responses, browser sessions, and private operator data stay outside
this repository. Video Studio source is licensed under Apache-2.0; third-party
dependencies, including Remotion, retain their own license terms.
