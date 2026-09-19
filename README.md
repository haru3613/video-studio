# Video Studio

A self-hosted video-production workflow for people and AI agents.

This repository is being prepared for an open-source release. It currently
contains project documentation and a preparation plan; the executable workflow
has not been imported yet.

The planned first version provides a complete CLI and a self-hosted MCP server
with stdio and authenticated HTTP transports. Both use the same workflow core
for planning, narration, storyboarding, rendering, quality checks, and export.
A local review dashboard and a small, redistributable example will demonstrate
the workflow. Video Studio does not provide a managed cloud service.

See [the preparation plan](docs/open-source-plan.md) for the project scope and
[the implementation plan](docs/implementation-plan.md) for interfaces,
architecture, delivery stages, and acceptance criteria.

Run the documentation-stage repository checks with:

```sh
scripts/verify
```

Runtime installation instructions will be added when an independently
installable version is available. Provider credentials, production media,
channel configuration, and operator state belong outside this repository.

License selection is pending. No open-source license has been granted yet.
Third-party software and example assets will retain their applicable licenses.
