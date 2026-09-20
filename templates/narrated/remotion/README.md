# Narrated Remotion template

This is a local, self-hosted Remotion source template for a cue-driven narrated
video. It registers:

- `NarratedLandscape` at 1920x1080
- `NarratedPortrait` at 1080x1920
- `NarratedCover` at 1280x720

All motion is frame-driven. Captions use the Remotion `Caption` JSON shape,
and every visual event starts on an exact caption cue. The template uses system
fonts and original HTML/SVG graphics; it downloads no media or fonts at render
time.

Dependencies retain their own licenses. In particular, Remotion packages are
governed by the Remotion License recorded in `package-lock.json`.

The source is one component of Video Studio. Installing and rendering this
folder proves the Remotion template, but does not prove the full CLI/MCP
workflow, final loudness mix, QA receipts, or publication readiness.

## Install and inspect

Node 22 or newer is required.

```sh
npm ci
npm test
npm run check
```

Rendering requires an installed local Chrome or Chromium. The template checks
standard macOS and Linux locations and refuses to download a browser during a
render. Set `VIDEO_STUDIO_CHROMIUM` when the executable lives elsewhere.

Generate the clearly labelled technical tone described in
`public/assets/README.md`, then render locally:

```sh
npm run render:landscape
npm run render:portrait
npm run render:cover
```

Replace `src/content.json` with project content that passes
`tests/validate-content.mjs`. Put local media in `public/assets/` and refer to
it by a relative path such as `assets/narration.wav`. Remote URLs and paths
outside `public/` are rejected.

For real narration, set `media.narration.kind` to
`user_supplied_narration`. A user-imported track still needs the Studio's
separate pronunciation, loudness, and publication evidence. Never relabel the
technical tone as narration and never create a pronunciation receipt for it.
