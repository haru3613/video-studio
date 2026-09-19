# Dependency and redistribution audit

**Audit date:** 2026-09-20 (Asia/Taipei)
**Worktree base:** `8ea6c3f9158a5d3c9e74f7b56814428a4320843c`, with the current uncommitted OSS candidate changes inspected
**Release form assessed:** source only; no dependency, media, model, browser, FFmpeg, or native executable binaries bundled

## Outcome

**License gate resolved.** The owner selected Apache-2.0 with copyright 2026
haru3613 on 2026-09-20. Root LICENSE/NOTICE and Python, Rust and template package
metadata now record that grant. The Node lockfile change affects only this
project's license metadata; all audited dependency versions and integrity
values remain unchanged. The inventory records the updated lockfile hash.

Remotion is a separate downstream-use condition, not a reason the repository's
own source cannot be published. Its official FAQ states that Remotion is
source-available rather than OSI open source and that use is governed by the
proprietary Remotion License. It also classifies `npx remotion render` and
similar programmatic rendering as automation, with eligibility and commercial
terms depending on the operator. See the
[official Remotion License FAQ](https://www.remotion.dev/docs/license/faq) and
[license text](https://www.remotion.dev/license). The project must keep this
prominent so users do not infer that the future Video Studio license also
licenses Remotion.

No dependency versions were changed and no automated fix command was run.

## Exact inventory

The machine-readable inventory is
[dependency-inventory.json](dependency-inventory.json). It records every exact
package/version found in the three candidate lockfiles, its runtime/build/dev
classification, declared or resolved license metadata, source metadata, lockfile
SHA-256, and audit evidence.

| Ecosystem | Locked packages | Classification |
| --- | ---: | --- |
| Python `uv.lock` | 44 external | 37 runtime, 7 dev |
| Rust `pipeline/Cargo.lock` | 125 external | 114 runtime, 7 build, 4 dev |
| Node `package-lock.json` | 286 unique name/version pairs | 283 runtime or platform-optional, 3 dev |

“Runtime” for Node includes platform-specific optional packages present in the
lock; one machine installs only the matching subset. Rust “build” includes
proc-macro and build-time closure required to compile the source.

License metadata sources were:

- Python: exact-version PyPI JSON metadata, including PEP 639
  `license_expression` where published. An example of the authoritative format
  is [FastAPI 0.141.1 metadata](https://pypi.org/pypi/fastapi/0.141.1/json).
- Rust: `cargo metadata --locked`, reading the exact downloaded crate
  manifests selected by `Cargo.lock`.
- Node: exact `package-lock.json` metadata, then the installed locked package
  metadata/license file for Remotion entries that say
  `SEE LICENSE IN LICENSE.md`.
- `transformers==5.14.1`: PyPI omitted a license field, so the inventory
  resolves it to Apache-2.0 from the
  [official v5.14.1 LICENSE](https://github.com/huggingface/transformers/blob/v5.14.1/LICENSE).

## Redistribution findings

### Repository license

The initial audit's pending-license blocker is resolved by the owner's
Apache-2.0 decision. Third-party packages retain their own terms.

### Source-only warnings

1. **Remotion packages are source-available.**
   The runtime closure contains Remotion License packages including
   `remotion`, `@remotion/cli`, renderer, player, bundler, web-renderer,
   studio-protocol, and media-parser. These are not covered by an OSI license.

2. **Eight Remotion package records have no package-level license assertion.**
   `@remotion/media@4.0.506` and seven platform compositor packages omit both
   the lockfile license field and a packaged license file. The compositor
   packages are optional binary packages. They are not bundled in this
   source-only release. Treat them as `NOASSERTION` for any future binary,
   offline cache, container, installer, or vendored `node_modules` release
   until upstream licensing is confirmed.

3. **Weak-copyleft and attribution licenses occur in installed closures.**
   The Node runtime includes MPL-2.0 Mediabunny and encoder packages and
   CC-BY-4.0 `caniuse-lite`; Python includes MPL-2.0 `certifi`. The Rust
   runtime's `r-efi` offers permissive MIT/Apache alternatives in addition to
   LGPL. A binary or vendored dependency release needs license texts,
   attribution, and file-level/source obligations assessed for the actual
   shipped subset.

4. **Optional G2P transitive dependencies are not locked.**
   `media-tools/requirements-g2p.txt` pins five direct packages but is not a
   complete hashed lock. The five direct pins were inventoried and audited;
   their transitive closure and model weights were not. Do not describe the
   optional G2P install as reproducible or redistribution-cleared.

5. **Operator tools are outside the source grant.**
   FFmpeg/FFprobe, Chrome/Chromium, eSpeak NG, provider voices, and G2P model
   weights are supplied separately. FFmpeg explains that the applicable LGPL
   or GPL obligations depend on build configuration; see
   [FFmpeg legal information](https://ffmpeg.org/legal.html). Codec patent
   permissions are separate from copyright licenses, as Remotion also notes.

No tracked archive, media, shared-library, executable, or WebAssembly extension
matched the audited binary patterns. This supports the source-only boundary; it
does not cover generated GitHub release assets that do not yet exist.

## Vulnerability evidence

Zero findings below means no advisory matched the exact versions within the
stated coverage. It is not proof that the code is vulnerability-free.

| Scope | Command/tool | Result |
| --- | --- | --- |
| Python runtime | `uv export --locked --no-dev --no-emit-project`, then PyPA `pip-audit --disable-pip` | 36 current-platform dependencies checked; 0 known vulnerabilities |
| Python conditional | Inventory comparison | Windows-only `pywin32` is locked but was not audited on this macOS run |
| Optional G2P | `pip-audit --disable-pip --no-deps -r media-tools/requirements-g2p.txt` | 5 direct pins checked; 0 known vulnerabilities; transitive closure excluded |
| Rust all locked | `cargo-audit 0.22.2 --json` | 0 vulnerabilities and 0 warnings; RustSec DB commit `d5c17953a895cf19e8d3ce66eaa42b6fcfe1fb16`, updated 2026-09-19 |
| Node official npm audit | `npm ci --ignore-scripts`, then npm 10.9.8 `npm audit` with runtime-only and all scopes | **Tool error:** npm registry returned HTTP 400 “Invalid package tree” plus endpoint-retirement notice; no conclusion |
| Node fallback | OSV `POST /v1/querybatch` over exact lock versions | runtime/platform-optional: 283 checked, 0 matches; all: 286 checked, 0 matches |

`pip-audit` is the PyPA auditing tool described on its
[official PyPI page](https://pypi.org/project/pip-audit/). `cargo-audit`
checked the RustSec advisory database. The Node fallback used OSV's documented
batch API; OSV describes itself as an aggregator of version-aware advisory
databases and documents `querybatch` in its
[official API documentation](https://osv.dev/docs/). The npm failure is retained
as a coverage gap rather than converted into a pass; npm documents the audit
mechanism in the [npm audit CLI reference](https://docs.npmjs.com/cli/v10/commands/npm-audit/).

## Commands and evidence boundary

Commands were run read-only against dependency state on 2026-09-20:

```sh
uv tree --locked --no-dev
uv export --locked --no-dev --no-emit-project
uvx --from pip-audit pip-audit --disable-pip -r <locked-export>
uvx --from pip-audit pip-audit --disable-pip --no-deps \
  -r media-tools/requirements-g2p.txt

cargo metadata --locked --format-version 1
cargo tree --locked --edges normal,build
cargo-audit 0.22.2 --json

npm ci --ignore-scripts                 # disposable /tmp copy
npm audit --omit=dev --json             # failed: registry HTTP 400
npm audit --json                        # failed: registry HTTP 400
OSV POST /v1/querybatch                 # exact Node lock versions
```

Package installation and audit tooling lived under `/tmp` or user package
caches. No installed dependency tree or generated binary was added to the
repository.

## Required decisions before publication

1. Completed: owner selected Apache-2.0, copyright 2026 haru3613.
2. Keep the Remotion notice and downstream-use warning visible in the README and
   notices.
3. Keep the release source-only. Before any binary/container/offline-cache
   distribution, repeat the audit on the exact artifact and resolve all
   `NOASSERTION` Remotion packages.
4. Either produce a complete optional-G2P lock with model provenance or continue
   to label G2P as externally supplied and outside the reproducible base install.
5. Re-run npm's official audit when its registry endpoint accepts the lock; do
   not treat the OSV fallback as equivalent npm-tool evidence.
