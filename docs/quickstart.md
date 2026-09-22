# Make your first video

This walkthrough ends with a video you can play, revise, and export. It uses an
original sample generated on your machine, so you can try the workflow before
preparing your own assets. The sample voice is explicitly selected eSpeak NG
speech for testing. It is not a recommended production voice.

First complete [installation](installation.md), including the PATH setup. For
this sample only, install [eSpeak NG](https://github.com/espeak-ng/espeak-ng):
`brew install espeak-ng` on macOS, or `sudo apt-get install espeak-ng` on Ubuntu.
Your own audio/SRT workflow does not need eSpeak NG.

Run the following commands from your cloned `video-studio` repository, in the
same terminal. Keep that checkout clean; inputs and finished media live outside it.

## 1. Create sample inputs and a workspace

```sh
STUDIO_SOURCE="$PWD"
STUDIO_INPUTS="$HOME/VideoStudioInputs/first-video"
export VIDEO_STUDIO_WORKSPACE="$HOME/VideoStudio"
STUDIO_PROJECT="$VIDEO_STUDIO_WORKSPACE/projects/first-video"

python3 "$STUDIO_SOURCE/examples/quickstart/make_inputs.py" \
  --demo-voice --output "$STUDIO_INPUTS"
video-studio workspace init --workspace "$VIDEO_STUDIO_WORKSPACE"
export VIDEO_STUDIO_DELIVERY_ROOT="$VIDEO_STUDIO_WORKSPACE/exports"
```

The sample directory contains `voice.wav`, `captions.srt`, `project.json`, and
provenance information. The generator measures each spoken section instead of
speeding up the voice to fit a preset duration. It refuses to overwrite an
existing sample; use another empty directory if you want a fresh copy.

If you already have narration and captions, use the input format in
[bring your own media](user-media.md), then continue below with your spec path.

## 2. Prepare the project

```sh
video-studio create --projects-root "$VIDEO_STUDIO_WORKSPACE/projects" \
  --project first-video --idempotency-key create-first-video

video-studio --json lease claim --project-root "$STUDIO_PROJECT" \
  --owner local --ttl-seconds 3600 --idempotency-key lease-first-video \
  > "$STUDIO_INPUTS/lease.json"
cat "$STUDIO_INPUTS/lease.json"
```

Look for `code: lease_claimed`. The lease lets the tool coordinate changes to
this project. Save its returned ID for the rest of the walkthrough:

```sh
STUDIO_LEASE_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["data"]["lease_id"])' "$STUDIO_INPUTS/lease.json")"

video-studio prepare --project-root "$STUDIO_PROJECT" \
  --spec "$STUDIO_INPUTS/project.json" --owner local \
  --lease-id "$STUDIO_LEASE_ID" --idempotency-key prepare-first-video-1

(cd "$STUDIO_PROJECT/remotion" && npm ci --ignore-scripts)
```

`project_prepared` means the inputs and local template are ready. The audio and
SRT are copied without retiming. This step makes no provider request.

## 3. Render and check progress

```sh
video-studio --json run --project-root "$STUDIO_PROJECT" \
  --owner local --lease-id "$STUDIO_LEASE_ID" \
  --runner render-project --tools-root "$STUDIO_SOURCE/media-tools" \
  --idempotency-key render-first-video-1 > "$STUDIO_INPUTS/render.json"
cat "$STUDIO_INPUTS/render.json"
```

Look for `code: gate_started`. The render is now running in the background.
Save its job ID and check it:

```sh
STUDIO_JOB_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["data"]["data"]["job_id"])' "$STUDIO_INPUTS/render.json")"
video-studio job status --project-root "$STUDIO_PROJECT" --job-id "$STUDIO_JOB_ID"
```

Repeat the status command until `data.status` is `succeeded`. The video is then
at `$STUDIO_PROJECT/output/final.mp4`. If the state is `failed`, read the error:

```sh
video-studio job logs --project-root "$STUDIO_PROJECT" --job-id "$STUDIO_JOB_ID"
```

Fix the reported problem before continuing. See [retry a render](advanced-workflows.md)
for unchanged-input resume and corrected-input rerenders. A submitted job is
not a finished video; `succeeded` is the result to wait for.

## 4. Watch it and leave feedback

```sh
video-studio ui --workspace "$VIDEO_STUDIO_WORKSPACE"
```

The terminal prints a localhost URL and the path to a one-time code file. Open
the URL, open that file locally to copy the code, and sign in within five minutes.
Keep the terminal running while you review. If the default port is occupied,
add `--port 8791` and use the displayed address.

Open `first-video`, play the current video, and leave a timestamped comment on
something you want changed. The current review UI uses Traditional Chinese;
the comment action is **加入留言** (add comment). For example: “Change the first heading to One step,
done well.” The comment stays tied to the video version you watched.

Return to the terminal and press **Ctrl+C** to stop the review server before
continuing this terminal walkthrough. Your comments and media remain stored.
An agent can now retrieve the feedback with:

```sh
video-studio review list --project-root "$STUDIO_PROJECT"
```

## 5. Make one change

For a small first revision, change the sample's first scene heading:

```sh
python3 - "$STUDIO_INPUTS/project.json" <<'PY'
import json
import sys
from pathlib import Path
path = Path(sys.argv[1])
spec = json.loads(path.read_text())
spec["scenes"][0]["heading"] = "One step, done well"
path.write_text(json.dumps(spec, indent=2) + "\n")
PY

video-studio prepare --project-root "$STUDIO_PROJECT" \
  --spec "$STUDIO_INPUTS/project.json" --owner local \
  --lease-id "$STUDIO_LEASE_ID" --idempotency-key prepare-first-video-2

video-studio --json run --project-root "$STUDIO_PROJECT" \
  --owner local --lease-id "$STUDIO_LEASE_ID" \
  --runner render-project --tools-root "$STUDIO_SOURCE/media-tools" \
  --idempotency-key render-first-video-2 > "$STUDIO_INPUTS/render-2.json"

STUDIO_JOB_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["data"]["data"]["job_id"])' "$STUDIO_INPUTS/render-2.json")"
video-studio job status --project-root "$STUDIO_PROJECT" --job-id "$STUDIO_JOB_ID"
```

Again, wait for `succeeded`, then reopen the review UI to inspect the change.
Stop the UI with Ctrl+C when you return to the terminal. Use a new idempotency
key for each changed request; reusing a key replays the earlier request.
The preceding finished video remains available if a retake fails.

For later revisions, give the agent your feedback and let it update the spec.
You can replace image/video assets and text through the same preparation path.

## 6. Export and release the project

```sh
video-studio delivery status --project-root "$STUDIO_PROJECT"

video-studio delivery export --project-root "$STUDIO_PROJECT" \
  --owner local --lease-id "$STUDIO_LEASE_ID" \
  --idempotency-key export-first-video

video-studio lease release --project-root "$STUDIO_PROJECT" \
  --owner local --lease-id "$STUDIO_LEASE_ID" \
  --idempotency-key release-first-video
```

Successful technical checks return `delivery_technical_ready`; a successful
export returns `delivery_exported` and its `data.bundle_path`. That directory
contains the immutable delivery bundle, including the MP4 and SRT. The working
video remains in the project's `output/final.mp4`.

If a delivery check reports a blocker, follow its message and rerun the check
before exporting. Technical readiness does not approve the narration, facts,
or publication; review those yourself. Export is local and does not upload to
a video platform.

The lease above lasts an hour. Release it before updating the installed runtime.
For longer sessions, see [lease and recovery notes](advanced-workflows.md).

Next, [replace the sample with your own media](user-media.md) or
[connect the workflow to your MCP client](mcp-quickstart.md).
