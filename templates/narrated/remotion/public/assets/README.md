# Local assets

Put user-owned narration, music, effects, images, and clips here. No media is
bundled with the template.

The committed starter content points at `demo-tone.wav`. Generate that file
only for technical template QA:

```sh
ffmpeg -y -f lavfi -i "sine=frequency=220:sample_rate=48000:duration=24" \
  -filter:a "volume=0.025" public/assets/demo-tone.wav
```

This signal is a synthetic synchronization tone. It is not speech, narration,
music, a pronunciation sample, or evidence that the pronunciation gate passed.
