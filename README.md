# Video Studio

**Make narrated videos with your AI agent.**

Bring your voice, captions, images, and clips. Render a video on your own
machine, review it in your browser, and send changes back to your agent.
Use the CLI or connect through MCP.

[Make your first video](docs/quickstart.md) · [Connect your agent](docs/mcp-quickstart.md) · [Use your own media](docs/user-media.md)

<!-- Showcase: insert inspected silent sample preview and local review screenshot. -->

Video Studio is self-hosted. Your files and render jobs live on your machine
or private server; there is no Video Studio cloud account. Speech generation
is optional, and you choose the voice or provider.

## From your files to a finished video

| You bring | You get |
| --- | --- |
| Narration audio and matching SRT subtitles | A rendered MP4 with captions |
| Images, video clips, or the included diagram scenes | Landscape or portrait video from a JSON project spec |
| Feedback on a specific moment | A revision your agent can render and you can compare in the review page |

The bundled template suits narrated explainers, short lessons, and walkthroughs
built from prepared media. Choose scenes and change their text, colors, and
assets in JSON. Custom Remotion code can extend the visuals.

If a render fails, the previous finished video stays available. Fix the input
and submit a new render in the same project; job history and logs remain there
to help you find what went wrong.

## Make, review, revise

1. **Make a first cut.** Give your agent the narration, captions, and visual
   assets, or operate the CLI yourself.
2. Open the local review page. Watch the video and leave a comment at the moment
   you want changed.
3. Have the agent read the feedback, update the project, and render the next
   version. Export the MP4 and SRT when you're happy with the result.

After connecting the tools, a starting request can be:

> Use Video Studio to make a landscape explainer from `voice.wav`,
> `captions.srt`, and the images in `assets/`. Keep the supplied voice and use
> the bundled template. Show me the result in the local review page, then read
> my timestamped feedback before making the next version.

## Choose how you work

| Entry point | Use it when… | Start here |
| --- | --- | --- |
| CLI | You work in a terminal, or your coding agent can run shell commands. | [First video walkthrough](docs/quickstart.md) |
| Local MCP | You want Video Studio tools inside your MCP client. | [stdio setup and first task](docs/mcp-quickstart.md) |
| Private HTTP MCP | You run the workflow on your own server. | [Authenticated deployment](docs/http-mcp.md) |

All three use the same projects, render jobs, and review feedback.

## Try it with a sample

The [first video walkthrough](docs/quickstart.md) includes a small original
sample you can create locally. It takes you through preparing inputs, rendering,
reviewing, making one change, and exporting a local bundle.

The sample uses explicitly selected eSpeak NG test speech. For your own
productions, import your preferred audio and SRT; no TTS API key is needed for
that path. [Optional providers](docs/narration-providers.md) include an
ElevenLabs adapter and an interface for installed provider packages.

Start with the [installation guide](docs/installation.md). This is currently a
source-built release: it needs Git, uv, Rust/rustup, Node.js 22+, FFmpeg, and
Chrome/Chromium. Current test targets are macOS arm64 and Ubuntu 24.04 x86_64.

## Project status

Video Studio is an early release for people comfortable using a coding agent
or terminal. The browser UI is for reviewing video and leaving feedback;
project changes are made through the agent, CLI, or project files.

Local rendering, review, corrected-input retries, and export have been exercised.
See [validation evidence](docs/validation.md) and the
[CI runs](https://github.com/haru3613/video-studio/actions/workflows/verify.yml)
for the tested workflows. Technical checks cover decodability, timing, and
render binding; you still review the content and pronunciation.

## Guides and development

- [Bring your own voice and visuals](docs/user-media.md)
- [Install and troubleshoot](docs/installation.md)
- [Connect an MCP client](docs/mcp-quickstart.md)
- [Back up a workspace and understand workflow guarantees](docs/advanced-workflows.md)
- [Customize the Remotion template](templates/narrated/remotion/README.md)
- [Contribute](CONTRIBUTING.md) · [Report a security issue](SECURITY.md)

For source changes, run `scripts/verify`. The optional authenticated-HTTP
integration test is documented in [HTTP deployment](docs/http-mcp.md).

Video Studio source is licensed under [Apache-2.0](LICENSE), copyright 2026
haru3613. Dependencies, including Remotion, retain their own terms; see
[third-party notices](THIRD_PARTY_NOTICES.md). Provider credentials and generated
production media belong outside the source repository.
