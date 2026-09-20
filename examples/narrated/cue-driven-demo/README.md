# Cue-driven narrated demo source

This is a reproducible 24-second source fixture for the narrated Remotion
template. Its topic, copy, and vector graphics are original. It has no network
assets, paid provider output, private references, downloaded fonts, or committed
media.

The fixture uses a generated 220 Hz technical synchronization tone so the real
Remotion render contains an audio stream. The tone is not speech or narration.
The committed `render_plan.json` therefore uses the existing
`haru.render_plan.v1` contract with `engine: remotion`,
`lane_contract: manual.v1`, and an explicit
`skip_pronunciation_gate: true`. This is permitted for rendering the example;
it does not satisfy narrated publication. Video Studio should continue to block
publication until a user supplies narration and the normal pronunciation,
loudness, QA, and approval receipts exist.

Installing this template and rendering the fixture verifies this source package.
It does not by itself prove the full Video Studio CLI/MCP workflow.

## Assemble in a disposable directory

Run from the repository root:

```sh
demo_root="$(mktemp -d)"
cp -R examples/narrated/cue-driven-demo/. "$demo_root/"
cp -R templates/narrated/remotion "$demo_root/remotion"
cp "$demo_root/composition-content.json" "$demo_root/remotion/src/content.json"
mkdir -p "$demo_root/output" "$demo_root/remotion/public/assets"

(
  cd "$demo_root/remotion"
  npm ci
  npm test
  npm run check
)

ffmpeg -y -hide_banner -loglevel error \
  -f lavfi -i "sine=frequency=220:sample_rate=48000:duration=24" \
  -filter:a "volume=0.025" \
  "$demo_root/remotion/public/assets/demo-tone.wav"
```

## Render the real Remotion compositions

```sh
(
  cd "$demo_root/remotion"
  npx --no-install remotion render src/index.ts NarratedLandscape \
    "$demo_root/output/landscape.mp4" --concurrency=1
  npx --no-install remotion render src/index.ts NarratedPortrait \
    "$demo_root/output/portrait.mp4" --concurrency=1
  npx --no-install remotion still src/index.ts NarratedCover \
    "$demo_root/output/cover.png"
)

ffprobe -v error -show_entries stream=codec_type,width,height \
  -show_entries format=duration -of json "$demo_root/output/landscape.mp4"
ffmpeg -hide_banner -nostats -v error \
  -i "$demo_root/output/landscape.mp4" -f null -
```

The production render must still go through Video Studio's MCP
`render-project` runner with `--tools-root media-tools`. The direct commands
above are bounded template QA and intentionally write only inside the disposable
directory.

To use real narration, copy a user-owned track into
`remotion/public/assets/`, update `composition-content.json`, and replace the
technical labels with captions aligned to that track. Remove the render skip
only after the canonical narration and its real pronunciation evidence exist.
