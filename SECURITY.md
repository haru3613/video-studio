# Security reporting

Report suspected vulnerabilities privately through [GitHub private vulnerability
reporting](https://github.com/haru3613/video-studio/security/advisories/new).
Do not open a public issue for an undisclosed vulnerability. Include the affected
commit, CLI or MCP operation, a minimal reproduction and the observed impact.
Never attach credentials, access tokens, personal media, browser profiles or
unredacted provider responses.

During experimental development, only the current main branch is supported.
HTTP MCP is intended for a single operator's trusted workspace. Custom Remotion
source executes with that operator's permissions; this project does not sandbox
untrusted templates or provide multi-tenant isolation.
