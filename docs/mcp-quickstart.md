# Connect an agent with local stdio MCP

Use this when your AI coding agent supports MCP and you want it to work in your
own Video Studio workspace. The local stdio server and the CLI use the same
durable projects, jobs, review notes, and retry behavior. Choose the CLI when an
agent can run shell commands; choose MCP when you prefer typed tools in the
agent's tool list.

This guide configures a local process. It starts no HTTP listener, needs no
OAuth service, and sends no project data to a Video Studio cloud service.

## 1. Install and initialize a workspace

Follow the [installation guide](installation.md) first, then prepare the operator-owned directories:

```sh
export VIDEO_STUDIO_WORKSPACE="$HOME/VideoStudio"
video-studio workspace init --workspace "$VIDEO_STUDIO_WORKSPACE"
export VIDEO_STUDIO_DELIVERY_ROOT="$VIDEO_STUDIO_WORKSPACE/exports"
```

The executable is installed at the following absolute path after installation.
Replace `/Users/you` with your actual home directory; some MCP clients do not
expand `~`.

```text
/Users/you/.local/share/video-studio/bin/video-studio-mcp
```

## 2. Add a stdio server entry

Most MCP clients use an object shaped like this. Put the equivalent entry in
your client's MCP settings, using real absolute paths.

```json
{
  "mcpServers": {
    "video-studio": {
      "command": "/Users/you/.local/share/video-studio/bin/video-studio-mcp",
      "env": {
        "VIDEO_STUDIO_WORKSPACE": "/Users/you/VideoStudio",
        "VIDEO_STUDIO_DELIVERY_ROOT": "/Users/you/VideoStudio/exports"
      }
    }
  }
}
```

`VIDEO_STUDIO_WORKSPACE` records the intended operator workspace for local
commands. Stdio tool calls still name an absolute `workspace_root` or
`project_root` explicitly, so an agent's request is auditable. The delivery root
is only used when a workflow explicitly exports a delivery bundle. Do not put
provider keys, browser profiles, or unrelated shell configuration in this MCP
entry.

Restart or reload your MCP client after changing its settings. The server writes
MCP protocol data only to standard output; diagnostics go to standard error.

## 3. Make the first read-only call

Ask your agent:

> Call `workspace_info` with `{ "schema_version": 1, "workspace_root":
> "/Users/you/VideoStudio" }`. Tell me the workspace ID and project count. Do
> not create or change anything.

Replace the example path before sending it. A newly initialized workspace should
return an `ok` result with code `workspace_info` and data shaped like:

```json
{
  "schema": "video_studio.workspace_info.v1",
  "workspace_id": "…",
  "project_count": 0
}
```

That confirms the agent can see the tool and the correct workspace. From there,
give it a bounded production task, for example: “Create a project called
`my-video`, prepare it from my narration audio and matching SRT, render it, and
report the returned job ID.” Follow the [first-video walkthrough](quickstart.md) for a complete sample,
or the [own-media spec](user-media.md) for your own files.

## CLI remains available

MCP is only a connection method. A shell-capable agent can use the same workflow
without MCP configuration:

```sh
video-studio workspace info --workspace "$HOME/VideoStudio"
video-studio tools
```

`video-studio tools` prints the exact typed tool surface of the installed MCP
server. `video-studio call TOOL --input request.json` is available when an agent
needs to call a tool directly from a shell.

## Private HTTP deployment

For a server you operate yourself, use the authenticated Streamable HTTP
deployment guide: [HTTP MCP](http-mcp.md). It binds the service to one configured
workspace, requires your own TLS proxy and OAuth issuer, and accepts only the
operator-configured export root. The reference configuration is tested with
Keycloak; other MCP-client and identity-provider combinations require your own
compatibility check.
