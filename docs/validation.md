# Validation evidence

Status: implementation candidate under active integration and review.
No production account, original Haru runtime, or public channel was modified.

The source was extracted from Studio commit
`18723997707e500d48c6c752cf56970fa51700c1` and media-tools commit
`6bd0414736dbb63a6dad8ec55b86116317d50119`. Subsequent work generalizes operator
configuration and installation while preserving the production schemas and
quality gates. New local delivery is explicitly separate from publication.

Recorded slice evidence (not a substitute for the final integrated candidate):

- Real immutable self-build installation, verification, corrupt/stale runtime
  refusal, and collision refusal for unrelated existing launchers.
- Original Rust workflow regression suites and complete typed CLI/MCP surface.
- Cross-platform process supervision tests using real FFmpeg: client exit,
  cancellation, input snapshots, failed retakes, epoch fencing, and interrupted
  promotion recovery.
- Local technical delivery and exports with real media probes, full decode,
  digest rechecks, and source-change refusal.
- Real Keycloak authorization-code/PKCE, callback checks, trusted-CA Nginx TLS,
  and official SDK HTTP-to-stdio protocol. Its backend was an isolated fixture;
  this does not by itself prove a full real rendering workflow over HTTP.
- Real Remotion landscape/portrait/cover template renders and clean decode.
  Earlier adapter tests using a synthetic renderer are not counted as Remotion
  execution evidence.
- Real UI-created review-store fixtures, current/stale media checks, idempotent
  note resolution, and no upgrade of comments into approval.
- Swift key/auth tests use fakes. No hardware enrollment or human-presence
  signing was performed. Paid TTS/STT and actual YouTube uploads remain untested.

Still required before completion:

- Final `scripts/verify` receipt for the integrated tree and Linux CI.
- Clean committed installation plus actual CLI/MCP example render, review and
  export in an isolated workspace.
- Browser interaction and screenshot evidence for login, playback, comments,
  stale-version handling, and feedback retrieval.
- Redacted secret scan of the candidate and Git history, dependency/license
  inventory, and the requested independent Standards and Spec code reviews.
- Owner's license selection and the final public-release readiness audit.

Local test logs and screenshots are deliberately kept outside the repository.
This document will be updated with exact candidate commits and results rather
than treating test configuration or implementation intent as proof.
