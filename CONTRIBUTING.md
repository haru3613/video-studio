# Contributing

This repository is currently a private release candidate. Changes should have a
clear user-visible purpose and preserve the shared CLI/MCP workflow behavior.

Run `scripts/setup`, then `scripts/verify`. Keep provider calls out of automated
unit tests. The Keycloak integration test uses disposable local services. A
paid-provider smoke test requires an explicit budget and must be reported
separately from offline contract tests.

Use a focused branch and pull request. Explain the behavior, affected workflow,
verification commands and results, and any unverified platform or service.
Changes to runtime authority, leases, job promotion, or artifact/review binding
need regression coverage for failure and recovery, not just a successful path.

Import original code through an explicit allowlist with source commit and
redistribution provenance. Preserve third-party notices. Do not submit generated
media, credentials, account configuration, real provider responses, browser
state, or personal production projects.

The `haru.*` schemas intentionally preserve compatibility. Introduce a version
and migration story when changing their meaning; do not silently rename or
weaken an existing evidence gate. Review feedback, technical QA, and publishing
approval remain different concepts.
