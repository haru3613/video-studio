# Security reporting

Video Studio is a pre-release, private candidate. Existing collaborators can
report a suspected vulnerability through a private repository issue. Include
the candidate commit, affected CLI or MCP operation, a minimal reproduction,
and the observed impact. Never attach credentials, access tokens, personal
media, browser profiles, or unredacted provider responses.

Before this repository becomes public, the maintainer must enable GitHub
private vulnerability reporting and replace this private-candidate procedure
with its verified reporting link. A public issue is not an appropriate place
for an undisclosed vulnerability.

Only the current reviewed candidate is supported during development. HTTP MCP
is intended for a single operator's trusted workspace. Custom Remotion source
executes with that operator's permissions; this release does not sandbox
untrusted templates or provide multi-tenant isolation.
