# Make a video with your own narration and assets

Supply a finished voice recording and matching UTF-8 SRT subtitles. They can
come from a recording session, a local model, or any TTS service you choose.
Preparation copies their bytes unchanged: it does not synthesize speech,
stretch the recording, or invent subtitle timing. No provider key is needed.
The final render applies the project's program loudness policy; source audio
is retained unchanged. Pronunciation and semantic alignment still need review.

## CLI

For a complete first run, follow the [first video walkthrough](quickstart.md).
It includes installation, job polling, local review, revisions, export, and
lease release. This page documents the input spec for your own material.

Start with an installed `video-studio` and an external workspace:

```sh
video-studio workspace init --workspace "$HOME/VideoStudio"
video-studio create --projects-root "$HOME/VideoStudio/projects" \
  --project my-video --idempotency-key create-my-video
video-studio lease claim --project-root "$HOME/VideoStudio/projects/my-video" \
  --owner local --ttl-seconds 3600 --idempotency-key lease-my-video
```

Save this as `project.json` beside `voice.wav` and `captions.srt`:

```json
{
  "schema": "video_studio.project_spec.v1",
  "title": "Finish one small step",
  "narration": {"mode": "import", "audio": "voice", "captions": "captions"},
  "assets": [
    {"id": "voice", "kind": "audio", "file": "voice.wav"},
    {"id": "captions", "kind": "subtitle", "file": "captions.srt"}
  ]
}
```

Then prepare and render, using the lease ID returned above:

```sh
video-studio prepare --project-root "$HOME/VideoStudio/projects/my-video" \
  --spec ./project.json --owner local --lease-id '<lease_id>' \
  --idempotency-key prepare-my-video-1
(cd "$HOME/VideoStudio/projects/my-video/remotion" && npm ci --ignore-scripts)
video-studio run --project-root "$HOME/VideoStudio/projects/my-video" \
  --owner local --lease-id '<lease_id>' --runner render-project \
  --tools-root '/absolute/path/to/video-studio/media-tools' \
  --idempotency-key render-my-video-1
```

The render response contains a job ID. Check it with
`video-studio job status --project-root "$HOME/VideoStudio/projects/my-video" --job-id '<job_id>'`
until `data.status` is `succeeded`, then open
`video-studio ui --workspace "$HOME/VideoStudio"`.
The [review and export steps](quickstart.md#4-watch-it-and-leave-feedback)
show the rest of the flow. When adapting them here, keep using your `my-video`
project path and current lease. Technical delivery does not mean editorial or
publication approval.

Relative `file` paths are resolved against the spec's directory. The CLI copies
local regular files into the workspace inbox, stages them for the lease owner,
and deletes only its temporary copies. Original files are never modified.
The spec is limited to 1 MiB and 32 assets. Use a new idempotency key when you
change inputs; repeating a key is a replay of that request.

## Choose the visuals from data

Without `scenes`, each caption starts a simple signal diagram. For authored
visuals, add contiguous scenes from zero to the exact audio duration in seconds.
For example, with a ten-second recording:

```json
"scenes": [
  {"id": "first", "start_seconds": 0, "end_seconds": 4,
   "heading": "Choose a small action",
   "visual": {"kind": "cards", "labels": ["Task", "Next step", "Done"], "active_index": 1}},
  {"id": "second", "start_seconds": 4, "end_seconds": 10,
   "heading": "Make progress visible", "text": "One action you can finish today.",
   "visual": {"kind": "image", "asset": "illustration", "fit": "contain"}}
]
```

Add the referenced image to `assets`, for example
`{"id":"illustration","kind":"image","file":"illustration.png"}`.
Supported visual kinds are `signal`, `cards`, `steps`, `image`, and `video`.
Cards and steps accept three labels and `active_index` from 0 to 2. Images and
videos accept `contain` or `cover`. Video clips start at their scene start,
continue across caption changes, and are muted so they do not compete with the
narration. Supply clips long enough for the scene; automatic looping is absent.

Optional settings include `format` (`landscape` or `portrait`), `kicker`,
`palette` (six-digit hex colors), and explicit background music:
`"background_music":{"asset":"music","gain_db":-28}`. Add music as an audio
asset. No background music is added by default. This gain is a fixed level,
not speech-aware ducking; review the balance before delivery.

Authored visuals remain on screen through pauses, including a scene's leading
silence. Caption cues record provenance; importing SRT does not certify semantic
alignment of your scene choices.

Subtitles may include silent gaps. Overlapping cues or cues past the end of the
recording are rejected. The template hides subtitles during gaps. Changing
subtitle timing requires you to supply corrected SRT, not a forced voice speed.

Run `prepare` again with a new key to replace data/media and regenerate the
scene plan. It preserves prior renders and job history. Preparation only
updates the bundled template; custom or older template code is preserved and
returns `template_conflict` rather than being overwritten. Use a fresh project
or explicitly migrate that code.

## MCP and private servers

Use `artifact_stage` to stage audio, subtitles, images, and videos from the
operator's workspace inbox. Set each spec asset to `{id, kind, stage_id}`.
Stage the JSON as metadata, promote it with `produce_staged_artifact` to
`project-spec.json`, then invoke `run_next` with `runner: "prepare-project"`.
All calls require the project's current lease and owner. `prepare-project`
accepts no arbitrary command or tools root. This is the same preparation path
used by the CLI, including authenticated HTTP MCP.

HTTP clients must use server-side inbox paths or existing stage IDs. Client
machine `file` paths are a convenience of the local CLI, not remote file access.
No Video Studio upload host, cloud account, or hosted TTS is provided.

## Retry only the failed work

- If inputs are unchanged, use `job resume` with the failed job ID.
- If you corrected audio, subtitles, or scene assets, prepare with a new key,
  then run `render-project` with a new key. A failed job with an older input
  revision creates a new job; it cannot prevent the corrected render.
- Resuming an old job after inputs change returns
  `job_resume_revision_changed`. Cancelled/interrupted jobs still require an
  explicit action; they are not silently restarted.

Old finals remain available until a verified replacement is promoted. Job logs
include the render/mixing diagnostic, so an audio failure is actionable.

For optional speech generation and the installed-provider interface, see
[narration providers](narration-providers.md). Mechanical demo speech is an
explicit test choice and is not the default production voice.
