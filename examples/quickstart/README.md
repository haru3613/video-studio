# Quickstart input pack

This generator creates the small input pack used by the README's first-video
walkthrough. It makes inputs only; it never creates a Video Studio workspace,
claims a lease, writes `.hvp` state, or renders a video.

The sample uses deliberately mechanical eSpeak NG speech. You must opt in to
that voice explicitly:

```sh
python3 examples/quickstart/make_inputs.py \
  --output "$HOME/VideoStudioInputs/quickstart" \
  --demo-voice
```

The output directory must be outside the repository and new or empty. No part
of its path may be a symlink. The command requires `espeak-ng`, `ffmpeg`, and
`ffprobe` on `PATH`, makes no network or API calls, and writes:

- `voice.wav`: 48 kHz mono PCM demo narration
- `captions.srt`: captions timed from the generated sentence WAVs
- `project.json`: a `video_studio.project_spec.v1` file with relative inputs
- `PROVENANCE.md`: source text and the review limitations of the sample

Pass `project.json` to `video-studio prepare` as shown in the main quickstart.
For real work, use your own reviewed narration and subtitles. This demo voice,
its pronunciation, and its generated caption alignment are not approved for
publication.
