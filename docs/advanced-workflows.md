# Advanced local workflows

These workflows are useful after you have made a first project. They are kept
separate from the getting-started path because they affect durable workspace
state or require operator-managed infrastructure.

## Retry a failed render

Use the job ID returned by render submission to inspect a failed run:

```sh
video-studio job status --project-root "$HOME/VideoStudio/projects/my-video" \
  --job-id '<job_id>'
video-studio job logs --project-root "$HOME/VideoStudio/projects/my-video" \
  --job-id '<job_id>'
```

If inputs did not change, resume that exact failed job with the active project
lease:

```sh
video-studio job resume --project-root "$HOME/VideoStudio/projects/my-video" \
  --job-id '<job_id>' --owner local --lease-id '<lease_id>' \
  --idempotency-key resume-my-video-1
```

For `ENOSPC` / “no space left on device”, first free space for render frames and
build caches, then resume. Do not delete project state to clear a failed job.
If you corrected narration, subtitles, scenes, or media, prepare and
render with new idempotency keys. Video Studio creates a new job for the new
input revision and retains the earlier job history and final until a verified
replacement is promoted. See [user media](user-media.md#retry-only-the-failed-work)
for the precise behavior.

## Keep or release a lease

For a longer session, renew your current lease before it expires:

```sh
video-studio lease renew --project-root "$HOME/VideoStudio/projects/my-video" \
  --owner local --lease-id '<lease_id>' --ttl-seconds 3600 \
  --idempotency-key renew-my-video-1
```

After it expires, claim a new lease with a new idempotency key and use the new
ID for later mutations. Another active owner's lease is a reason to wait or
coordinate, not to delete project state.

Release your lease before installing a new runtime:

```sh
video-studio lease release --project-root "$HOME/VideoStudio/projects/my-video" \
  --owner local --lease-id '<lease_id>' --idempotency-key release-my-video-1
```

A runtime upgrade can invalidate the old mutation capability. Releasing first
avoids leaving a project held until the old lease expires.

## Review and export

Start the local review UI:

```sh
video-studio ui --workspace "$HOME/VideoStudio"
```

Open the displayed localhost address and enter the one-time code from the
owner-only code file. It expires after five minutes and is consumed once.
Comments are attached to the media version being reviewed; resolving one is not
an editorial acceptance or publishing approval.

Before exporting, ensure `VIDEO_STUDIO_DELIVERY_ROOT` names an existing absolute
directory in the environment that starts Video Studio. Check delivery status,
then export with the active lease and a fresh idempotency key:

```sh
video-studio delivery status \
  --project-root "$HOME/VideoStudio/projects/my-video"
video-studio delivery export \
  --project-root "$HOME/VideoStudio/projects/my-video" \
  --owner local --lease-id '<lease_id>' \
  --idempotency-key export-my-video-1
```

Export creates an immutable local bundle. It does not upload, schedule, or
publish a video.

## Backup and restore

Back up an idle workspace to an existing destination:

```sh
mkdir -p "$HOME/VideoStudioBackups"
video-studio workspace backup \
  --workspace "$HOME/VideoStudio" \
  --destination "$HOME/VideoStudioBackups"
```

Backup refuses active render jobs and takes a workspace-wide barrier. Restore
requires a new empty destination and verifies the backup manifest and file
hashes:

```sh
video-studio workspace restore \
  --backup "$HOME/VideoStudioBackups/<backup-directory>" \
  --destination "$HOME/VideoStudioRestored"
```

Restored workspaces invalidate leases, publish approval, template trust, and
path-bound signatures. Credentials, OAuth material, browser sessions, signing
keys, provider responses, and external attestation records are deliberately not
included; the operator backs those up separately.

## Custom templates and remote access

The bundled template is trusted by the installed workflow. Custom renderer code
requires a local owner approval through `scripts/trust-template`; MCP clients
cannot grant that trust. For a private server, follow [HTTP MCP](http-mcp.md).
It is an operator-managed service for one trusted workspace, not a sandbox for
untrusted Node or Remotion code.

## Existing projects and compatibility

The core retains the historical `haru.*` artifact schema names. New public
interfaces use `video_studio.*`; these names do not require an original Haru
account, channel, voice, or configuration.

Current render receipts bind the complete input revision. Older receipts remain
readable but require a new render before technical delivery. The preparer also
preserves custom or older template code; `template_conflict` requires a fresh
project or an explicit template migration, rather than silently replacing code.
