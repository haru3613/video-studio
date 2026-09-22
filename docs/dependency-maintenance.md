# Dependency maintenance

Dependabot checks monthly. Routine version updates have a total open-PR ceiling
of five: Rust 1, Python 1, JavaScript 2, and GitHub Actions 1. Minor/patch updates
are grouped by ecosystem. Remotion has its own group because every direct
`remotion` and `@remotion/*` package must stay on the same exact version.
Remotion security updates also have a separate family group.

Major upgrades are manual maintenance work. `sha2` is pre-1.0, so its minor
upgrades also need explicit API migration review. Node typings stay on the
supported Node 22 line; React upgrades must include matching `react-dom`.

Routine version updates for the optional G2P packages (`torch`, `transformers`,
`onnxruntime`) are held until an update can be checked against a real G2PW model
and a reproducible optional environment. The normal source CI does not install
that environment, so a green core check is not evidence of model compatibility.

These are **version-update** rules. Dependabot security updates stay enabled;
GitHub's version-PR ceiling does not limit security PRs. See the
[official options reference](https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-options-reference)
and [version-update filtering](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/manage-your-dependency-security/controlling-dependencies-updated).

## Initial backlog review — 2026-09-22

The original bot PRs were based on the initial release before imported-media
preparation merged. Accepted changes are consolidated on current `main` and
must pass fresh macOS/Linux checks before merge; old CI results are historical.

| Original PRs | Disposition | Reason |
| --- | --- | --- |
| #2, #3, #5 | Adopt in consolidated update; close originals as superseded | Reviewed pinned Actions updates; original CI passed on both hosted runners. New workflow retains SHA pins, read-only permissions and Node 22. |
| #4, #6, #8 | Adopt in consolidated update; close originals as superseded | `clap` 4.6.7, `uuid` 1.26.1 and `thiserror` 2.0.20 fit existing manifest constraints. Regenerate/test the combined dependency set. |
| #11, #13 | Replace with synchronized update; close originals as superseded | Each single-package update leaves mixed Remotion versions. Upgrade captions, CLI, media and core together to 4.0.525, and render landscape/portrait smoke clips. |
| #7 | Close; defer API migration | `sha2` 0.11 fails compilation: digest output no longer satisfies the `LowerHex` use in the source fingerprint. |
| #9 | Close; defer API migration | `rmcp` 3.4 fails compilation: `server_info` is now optional where the code reads `.name`. |
| #15 | Close; defer API migration | Python MCP 2 removes the currently imported `mcp.server.fastmcp` API; test collection fails on both platforms. |
| #18 | Close; defer coordinated React upgrade | React 19 with React DOM 18 produces `npm ci` peer-dependency resolution failures on both platforms. |
| #10 | Close; keep supported runtime alignment | Node 26 typings exceed the Node 22 CI/runtime baseline. A runtime-support change must precede this major type upgrade. |
| #16 | Close; defer compiler migration | TypeScript 7 is a major compiler upgrade. Passing the small template typecheck alone does not establish the migration's value or compatibility. |
| #12, #14, #17 | Close; require optional-model validation | Only optional G2P requirements change. Core CI passes without importing or executing the proposed model runtimes. |

Closing a deferred update does not declare its version unsafe. It means this
repository has not completed the needed migration or workload verification.
Deferred versions can be reconsidered together with the relevant code/tests.
