# Validation evidence

The implementation and requested code review are complete. This remains a
source-only release candidate. The owner has selected Apache-2.0, copyright
2026 haru3613; LICENSE, NOTICE and package metadata record that decision.
No production account, original Haru runtime, or public channel was modified.

## Candidate and source checks

The workflow evidence below was collected on executable candidate `0f609a0`
(and `d163877` for the final HTTP run). The licensing follow-up adds Apache-2.0
package metadata and LICENSE/NOTICE to the installed runtime closure without
changing workflow logic. Source provenance is recorded in
[source-import.json](source-import.json) and the media-tools provenance file.

On macOS arm64, `scripts/verify` passed with:

- 129 Rust tests, with rustfmt and clippy (`-D warnings`).
- 701 Python tests plus 83 subtests.
- 18 Swift fake-key tests.
- Python compilation, shell syntax, dashboard JavaScript checks and diff checks.

Two existing FastAPI/Starlette deprecation warnings remain. They do not change
the passing result. Native macOS source verification requires Swift 6.2+;
CI selects Xcode 26.2 explicitly.

[GitHub CI on bb97530](https://github.com/haru3613/video-studio/actions/runs/35459096385)
passed on macOS 15 arm64 and Ubuntu 24.04 x86_64, including template install,
content tests, TypeScript checks and example-contract tests. The later backup
projection fix passed its 10-test focused suite and the full local suite above.
The PR's current checks remain the authoritative receipt for its exact head.

## Installed workflows

Clean committed installation was exercised in a separate runtime HOME, with
shared compiler/package caches but no copied operator credentials. This is not
a claim of an uncached new-machine installation. The managed Python environment
and immutable self-built runtime were provisioned by `scripts/install`.
The installed CLI worked outside the source checkout; inherited legacy runtime
and launcher overrides did not touch their poisoned test destinations.

| Workflow | Observed evidence |
| --- | --- |
| CLI and stdio MCP | Created a canonical project, claimed a lease, submitted a real Remotion render, polled durable state and released the lease. Installed doctor passed; the exact surface contains 34 tools. |
| Real video | Original 24-second example text was spoken locally with eSpeak NG; the landscape Remotion composition rendered, passed FFmpeg full decode and subtitle timing checks. Separate landscape, portrait and cover template renders were checked. |
| Authenticated HTTP MCP | On `d163877`, fresh `run_next` returned `outcome=ok`, `code=gate_started`; job `8ce54f7cabb9431094a8af03a534e4f6` reached `succeeded`. Delivery and immutable export passed, and the lease was released. |
| HTTP authorization | Disposable Keycloak authorization-code/PKCE S256 and trusted-test-CA TLS checks passed, including issuer, audience, scope, Origin, principal binding, wrong verifier/redirect and untrusted-CA rejection. |
| Agent client | Claude Code 2.1.260 connected to exactly one authenticated test MCP server using isolated HOME/config/cwd and the test CA. This was a health check, without model inference or an agent-driven browser OAuth callback. No real client settings were changed. |
| Browser review | Chrome exercised one-time login, playback, seeking and a timestamped comment. After rerender, the old comment played its exact archived video and could be marked resolved. No JavaScript errors or external requests were observed. |
| Agent feedback and delivery | Installed CLI retrieved the old comment as resolved, stale and still playable; technical status and export passed for the new final. |
| Backup and restore | Installed CLI backed up 87 real workspace files and databases, restored into a new path, verified identical final-video bytes, and retrieved completed job history. Active-job refusal and restored process-authority invalidation have regression coverage. |

The final HTTP render revision was
`e7761161f8b2b1bbc1b4e4e799f8745420f6aa929538e86949e6119695917bab`;
the exported bundle was
`85cf5dea4eb0e853de05d16a369e24467d68131855565ec3cc97b99bd0fe6033`.
Temporary Keycloak/Nginx containers and token-bearing Claude configuration were
removed. Logs, screenshots, generated audio/video and backup files remain
outside this repository.

## Review and audit

Both independent review axes passed after their findings were repaired; see
[code-review.md](code-review.md). Actual workflow runs also exposed and repaired
compiler-cache invalidation, unused-scaffold delivery checks, strict retake
response validation, stale backup projections and Linux zombie detection.
Earlier failing runs are not counted as successful submission evidence.

Gitleaks 8.30.1 scanned the source candidate and reachable Git history with
redaction enabled and reported no secrets. The additional presence-only audit
matched one deliberately fake log-redaction fixture. Author and committer
metadata use GitHub noreply addresses. A clean scan is evidence, not a guarantee
that a repository can contain no secret.

[Dependency audit](dependency-audit.md) records exact locked packages, licenses,
and vulnerability coverage. Python/Rust audits and the Node OSV fallback found
no matching known vulnerabilities. npm's official audit endpoint failed; its
failure and the optional G2P transitive-closure gap remain explicitly recorded.
Remotion retains its separate license terms.

## Unverified or deliberately unconfigured

- Paid TTS/STT, actual YouTube uploads, hardware key enrollment and human-presence
  signing were not performed. Provider tests are offline; Swift key tests use fakes.
- Technical delivery does not assert factual accuracy, pronunciation approval,
  human visual acceptance or publication authority.
- Linux CI checks source/contracts and portable process behavior. The full real
  Remotion/browser workflow above ran on macOS, not on a Linux browser host.
- Optional G2P models and transitive packages are externally supplied. Binary,
  container, offline-cache and vendored-dependency distribution are not cleared
  by this source-only audit.
- The owner approved Apache-2.0 with copyright 2026 haru3613. Public repository
  visibility, private vulnerability reporting and branch protection are checked
  separately from source/runtime verification.
