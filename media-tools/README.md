# Bundled media tools

These are the portable local adapters used when Video Studio receives
`--tools-root media-tools`. They do not provide a hosted service and do not
contain credentials, voices, identity artwork, model weights, provider logs, or
generated media.

Install the small runtime dependency:

```sh
python3 -m pip install -r media-tools/requirements.txt
```

FFmpeg and FFprobe must be available on `PATH`. Remotion rendering uses the
project-local executable through `npx --no-install`.

Narration requires an explicit voice, an ElevenLabs credential, and an approved
credit limit:

```sh
export VIDEO_STUDIO_TTS_VOICE_ID=your_voice_id
export ELEVENLABS_API_KEY_PATH=/secure/path/to/key
export VIDEO_STUDIO_TTS_MAX_CREDITS=5000
```

`ELEVENLABS_API_KEY` may be used instead of the file path. Never configure
both. Every paid request requires a credit cap. When both CLI and environment
limits are provided, the smaller limit applies. `--force-budget` is retained
only for caller compatibility and never bypasses that cap. A persistent spend
journal prevents ambiguous provider outcomes from being retried and charged
again; see [SPEND_JOURNAL.md](narration/SPEND_JOURNAL.md).

Thumbnail rendering requires a subject image directory and Chrome/Chromium:

```sh
export VIDEO_STUDIO_COVER_ASSET_DIR=/path/to/png-assets
export VIDEO_STUDIO_CHROMIUM=/path/to/chromium  # optional when auto-detected
```

Files may be named `<expression>-cut.png`, `subject-<expression>-cut.png`, or
`<expression>.png`. No sample identity is bundled.

Mandarin G2P analysis is optional. Install
`requirements-g2p.txt`, obtain the models under their own licenses, and point
to local directories with `VIDEO_STUDIO_G2PW_MODEL_DIR` and
`VIDEO_STUDIO_G2PW_BERT_MODEL`. The planner remains offline and fails closed
when either model is unavailable.

Run the bounded offline suite with:

```sh
python3 -m unittest discover -s media-tools/tests -p 'test_*.py'
```
