# Install from source

Video Studio is self-hosted. Installing it builds the current checked-out
source on your computer; there is no hosted account or prebuilt binary release.
The installer promotes a content-addressed local runtime, so the CLI and MCP
server do not run from a checkout that might later change branches.

## Before you start

The supported, recorded checks cover macOS arm64 and Ubuntu 24.04 x86_64. Have
these tools available before installing:

- Git
- [uv](https://docs.astral.sh/uv/)
- Rust and rustup (the repository selects its pinned toolchain)
- Node.js 22 or newer
- FFmpeg and FFprobe
- a local Chrome or Chromium browser

`scripts/install` creates a Python 3.11 environment with `uv`, builds the Rust
programs, and runs the dependency doctor. It needs a clean, committed checkout.
On macOS, Swift is only required for the repository's full source-verification
suite, not the normal install.

## Install

```sh
git clone https://github.com/haru3613/video-studio.git
cd video-studio
scripts/install
```

The installer prints the two launchers it installed:

```text
~/.local/share/video-studio/bin/video-studio
~/.local/share/video-studio/bin/video-studio-mcp
```

Use them immediately by adding the directory to the current shell:

```sh
export PATH="$HOME/.local/share/video-studio/bin:$PATH"
```

To make this persistent, add that same `export` line to your shell's startup
file, such as `~/.zshrc` or `~/.bashrc`, then open a new shell. You can also
invoke the two absolute paths above directly; do not point an MCP client at a
binary under `pipeline/target/` in a mutable checkout.

## Create local working directories

Projects, review state, staged inputs, and renders belong in a workspace outside
the source checkout. Choose one explicitly, then initialize it:

```sh
export VIDEO_STUDIO_WORKSPACE="$HOME/VideoStudio"
video-studio workspace init --workspace "$VIDEO_STUDIO_WORKSPACE"
```

Set the export directory in the environment that launches the CLI or MCP server:

```sh
export VIDEO_STUDIO_DELIVERY_ROOT="$VIDEO_STUDIO_WORKSPACE/exports"
```

Workspace initialization creates `projects/`, `inbox/`, `exports/`, and private
state under that one root. The delivery directory must be an absolute path.
Video Studio does not upload or publish from this setting; it is where a
verified delivery bundle is written when you explicitly export it.

Now check the installed runtime, tools, and initialized workspace:

```sh
video-studio doctor
```

Look for `code: doctor_ready`. If `data.checks.workspace` is false, confirm the
workspace path and rerun initialization. Running this check before creating a
workspace returns `doctor_blocked` even when the software is installed.

You are ready to follow the [first video walkthrough](quickstart.md), or to
connect a local agent through [stdio MCP](mcp-quickstart.md).

## If installation or the first check stops

| Message or check | What to do |
| --- | --- |
| `Install uv` or `Install Rust` | Install the missing prerequisite, reopen the terminal, then run `scripts/install` again. |
| `Install requires clean committed source` | Use a fresh clone for installation, or review and commit your intentional source edits. Keep project media outside the checkout. |
| `data.checks.workspace` is false | Set `VIDEO_STUDIO_WORKSPACE` and initialize that workspace before running `video-studio doctor`. |
| `node`, `ffmpeg`, or `ffprobe` is false | The installed workflow checks standard tool locations: `/usr/bin`, `/usr/local/bin`, `/opt/homebrew/bin`. A tool available only through a shell's version manager may need an installation in one of those locations. |

The first install downloads/builds dependencies; completion time depends on the
machine and cache. The installer finishes by printing both launcher paths.

## Maintainers and contributors

For a complete source check rather than a normal installation, run:

```sh
scripts/verify
```

It runs formatting, Rust checks and tests, Python tests, media checks, and on
macOS the native signing-helper tests. It is not required for a user install and
does not enable providers, publishing, or a cloud service.
