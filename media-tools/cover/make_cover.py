#!/usr/bin/env python3
"""Render a neutral 1280x720 thumbnail with a user-supplied subject image."""

from __future__ import annotations

import argparse
import html as html_escape
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "cover_template.html"
ACCENTS = {"amber", "teal", "coral", "violet"}
TOP_BASE_PX = 80
BOTTOM_BASE_PX = 96
FIT_MAX_WIDTH_PX = 1180
FIT_FLOOR_PX = 52
_SLOT_RE = re.compile(r"[A-Z]{2,}_[A-Z][A-Z_]*")
KNOWN_SLOTS = {
    "SUBJECT_SRC",
    "KICKER_HTML",
    "TOP_HTML",
    "BOTTOM_HTML",
    "SUBTITLE_HTML",
    "TOP_SIZE",
    "BOTTOM_SIZE",
}


def find_chromium(explicit: str | None = None) -> str:
    configured = explicit or os.getenv("VIDEO_STUDIO_CHROMIUM")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"configured Chromium executable does not exist: {path}")
        return str(path)
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "chrome",
    ):
        found = shutil.which(name)
        if found:
            return found
    for candidate in (
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
    ):
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError(
        "Chromium/Chrome not found; pass --browser or set VIDEO_STUDIO_CHROMIUM"
    )


def resolve_subject(
    expression: str,
    *,
    asset: str | None,
    asset_dir: str | None,
) -> Path:
    if asset:
        candidate = Path(asset).expanduser()
        if not candidate.is_file():
            raise FileNotFoundError(f"subject image does not exist: {candidate}")
        return candidate.resolve()
    configured = asset_dir or os.getenv("VIDEO_STUDIO_COVER_ASSET_DIR")
    if not configured:
        raise FileNotFoundError(
            "pass --asset/--asset-dir or set VIDEO_STUDIO_COVER_ASSET_DIR"
        )
    root = Path(configured).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"cover asset directory does not exist: {root}")
    names = (
        f"{expression}-cut.png",
        f"subject-{expression}-cut.png",
        f"{expression}.png",
    )
    for name in names:
        candidate = root / name
        if candidate.is_file():
            return candidate.resolve()
    available = sorted(path.name for path in root.glob("*.png"))
    raise FileNotFoundError(
        f"no subject image for expression {expression!r} in {root}; "
        f"available: {available or '(none)'}"
    )


def _rich(text: str) -> str:
    escaped = html_escape.escape(text, quote=False)
    escaped = escaped.replace("\\n", "<br>").replace("\n", "<br>")
    return re.sub(r"\*\*(.+?)\*\*", r'<span class="hl">\1</span>', escaped)


def _visual_units(line: str) -> float:
    return sum(
        1.0 if unicodedata.east_asian_width(char) in ("W", "F") else 0.5
        for char in line
    )


def fit_size(
    text: str,
    base_px: int,
    max_width_px: int = FIT_MAX_WIDTH_PX,
    floor_px: int = FIT_FLOOR_PX,
) -> str:
    cleaned = text.replace("**", "")
    lines = re.split(r"\\n|\n", cleaned)
    units = max((_visual_units(line.strip()) for line in lines), default=0.0)
    if units <= 0 or units * base_px <= max_width_px:
        return f"{base_px}px"
    return f"{max(floor_px, int(max_width_px / units))}px"


def build_cover_html(
    template: str,
    *,
    accent: str,
    subject_src: str,
    kicker: str,
    top: str,
    bottom: str,
    subtitle: str,
    top_size: str | None = None,
    bottom_size: str | None = None,
) -> str:
    """Fill the fixed template in one pass."""
    if accent not in ACCENTS:
        raise RuntimeError(f"unsupported accent: {accent}")
    unknown = set(_SLOT_RE.findall(template)) - KNOWN_SLOTS
    if unknown:
        raise RuntimeError(f"cover template has unknown slots: {sorted(unknown)}")
    top_size = top_size or fit_size(top, TOP_BASE_PX)
    bottom_size = bottom_size or fit_size(bottom, BOTTOM_BASE_PX)
    substitutions = {
        "--ACCENT": f"--{accent}",
        "SUBJECT_SRC": subject_src,
        "KICKER_HTML": _rich(kicker),
        "TOP_HTML": _rich(top),
        "BOTTOM_HTML": _rich(bottom),
        "SUBTITLE_HTML": _rich(subtitle),
        "TOP_SIZE": top_size,
        "BOTTOM_SIZE": bottom_size,
    }
    pattern = re.compile(
        "|".join(re.escape(key) for key in sorted(substitutions, key=len, reverse=True))
    )
    return pattern.sub(lambda match: substitutions[match.group(0)], template)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kicker", default="")
    parser.add_argument("--top", default="")
    parser.add_argument("--bottom", required=True)
    parser.add_argument("--subtitle", default="")
    parser.add_argument("--expression", default="default")
    parser.add_argument("--accent", default="amber", choices=sorted(ACCENTS))
    parser.add_argument("--top-size")
    parser.add_argument("--bottom-size")
    parser.add_argument("--asset", help="exact subject PNG path")
    parser.add_argument("--asset-dir", help="directory containing expression PNGs")
    parser.add_argument("--browser", help="Chrome/Chromium executable")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    for name, value in (("--top-size", args.top_size), ("--bottom-size", args.bottom_size)):
        if value is not None and not re.fullmatch(r"\d{1,3}px", value):
            parser.error(f"{name} must look like '80px'")

    try:
        subject = resolve_subject(
            args.expression, asset=args.asset, asset_dir=args.asset_dir
        )
        browser = find_chromium(args.browser)
        html = build_cover_html(
            TEMPLATE.read_text(encoding="utf-8"),
            accent=args.accent,
            subject_src=html_escape.escape(subject.as_uri(), quote=True),
            kicker=args.kicker,
            top=args.top,
            bottom=args.bottom,
            subtitle=args.subtitle,
            top_size=args.top_size,
            bottom_size=args.bottom_size,
        )
    except (FileNotFoundError, RuntimeError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

    output = Path(args.out).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".html", dir=output.parent, delete=False, encoding="utf-8"
    ) as handle:
        handle.write(html)
        temporary = Path(handle.name)
    command = [
        browser,
        "--headless",
        "--disable-gpu",
        "--hide-scrollbars",
        "--force-device-scale-factor=1",
        "--window-size=1280,720",
        "--virtual-time-budget=6000",
        f"--screenshot={output}",
        temporary.as_uri(),
    ]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        command.insert(1, "--no-sandbox")
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    finally:
        temporary.unlink(missing_ok=True)
    if completed.returncode != 0 or not output.is_file():
        print(
            f"ERROR: browser did not render thumbnail: {completed.stderr[-500:]}",
            file=sys.stderr,
        )
        return 1
    print(f"OK {output} ({output.stat().st_size // 1024} KB, 1280x720)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
