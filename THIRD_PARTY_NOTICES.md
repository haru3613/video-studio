# Third-party software and source provenance

Video Studio contains original workflow code extracted from the owner's
private Studio and media-tools repositories. Exact commits and the reviewed
source allowlists are recorded in [source-import.json](docs/source-import.json)
and [media-tools/SOURCE_PROVENANCE.json](media-tools/SOURCE_PROVENANCE.json).
The original Video Studio source is licensed under Apache-2.0, copyright 2026
haru3613. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Dependencies retain their own licenses. Rust dependencies are locked in
`pipeline/Cargo.lock`, Python dependencies in `uv.lock`, and the example's
Node dependencies in `templates/narrated/remotion/package-lock.json`.

- **Remotion** is source-available under the Remotion License, not an OSI
  open-source dependency. Installing this workflow does not grant a downstream
  Remotion license. See the [official license FAQ](https://www.remotion.dev/docs/license/faq).
- **FFmpeg** is installed by the operator. Its LGPL/GPL obligations depend on
  the actual build. This source repository does not bundle an FFmpeg binary.
  See [FFmpeg legal information](https://ffmpeg.org/legal.html).
- **eSpeak NG** is an optional locally installed speech tool used to read the
  original demo text. No engine binary, voice data, or generated audio is
  included in this source repository. See its
  [source and license](https://github.com/espeak-ng/espeak-ng).
- **Model Context Protocol SDKs**, FastAPI, Uvicorn, React and other libraries
  are installed through their locked package managers, under their respective
  licenses. Package-manager metadata and upstream notices remain authoritative.
- **System fonts and Chrome/Chromium** are supplied by the operator. The
  template contains original vector graphics and does not fetch remote fonts.
- **Optional G2P models and provider voices** are not included. Their licenses,
  model acquisition, usage limits, and service terms are the operator's concern.

A future binary or container release must audit the actual bundled dependency
closure and carry the required source/notices. This source-only preparation
does not imply every possible distribution mode has been cleared.
