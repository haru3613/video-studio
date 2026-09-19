#!/usr/bin/env python3
"""Create a conservative YouTube publish pack from project manifests."""

import argparse
import json
import re
import stat
import sys
import tempfile
from pathlib import Path

# The isolated shell entrypoint omits the script directory from sys.path.
# Resolve sibling modules from this verified source directory once.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import canonical_layout  # noqa: E402
import agent_status  # noqa: E402


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def chapter_lines(project):
    storyboard = project / "storyboard-final-timed-readable.md"
    if not storyboard.exists():
        return []
    rows = []
    for line in storyboard.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| s"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 6:
            continue
        scene, time_range, _dur, _source, _register, intent = cells[:6]
        start = time_range.split("-", 1)[0]
        label = intent.split("；", 1)[0].split(":", 1)[0]
        rows.append(f"- {start} {label}")
    return rows


def source_lines(project):
    claims = read_json(project / "claims.json", {})
    lines = []
    seen = set()
    for claim in claims.get("claims", []):
        name = claim.get("source_name")
        url = claim.get("source_url")
        source_type = claim.get("source_type")
        if not name or not url:
            continue
        key = (name, url)
        if key in seen:
            continue
        seen.add(key)
        caveat = " (survey/estimate)" if source_type == "survey_non_official" else ""
        if source_type and ("blocked" in source_type or "snippet" in source_type):
            caveat = " (needs manual source lock before public publish)"
        lines.append(f"- {name}{caveat}: {url}")
    return lines


def build_pack(project, workspace):
    metadata = read_json(project / "publish-metadata.json")
    canonical_layout.validate_publish_metadata(metadata, project.name)
    project_contract = read_json(project / "project-contract.json")
    target, target_error = agent_status.canonical_publish_target(project_contract)
    if target is None:
        raise ValueError(target_error)
    status, _artifacts = agent_status.build(project, workspace)
    canonical = status["canonical_artifacts"]
    chapters = chapter_lines(project)
    sources = source_lines(project)
    warnings = status["warnings"]

    final_video = canonical.get("final_video") or {}
    cover = canonical.get("cover") or {}
    review = canonical.get("quality_review") or {}

    lines = [
        "# YouTube Publish Pack",
        "",
        f"Project: `{project.name}`",
        f"Target channel: `{target['youtube_channel_id']}`",
        f"Visibility: `{target['visibility']}`",
        f"<!-- haru.publish_target_sha256: {agent_status.publish_target_sha256(target)} -->",
        "",
        "## Title",
        "",
        metadata["title"].strip(),
        "",
        "## Thumbnail",
        "",
        f"- File: `{cover.get('path', '')}`",
        f"- Text: `{metadata['thumbnail_text'].strip()}`",
        "",
        "## Description",
        "",
        metadata["description"].strip(),
        "",
        "## Chapters",
        "",
        *(chapters or ["- Chapters not provided."]),
        "",
        "## Source Statement",
        "",
        metadata["source_statement"].strip(),
        "",
        "## Sources",
        "",
        *(sources or ["- See source statement above."]),
        "",
        "## Hashtags",
        "",
        " ".join(metadata["hashtags"]),
        "",
        "## Final Artifacts",
        "",
        f"- Final video: `{final_video.get('path', '')}`",
        f"- Final video sha256: `{final_video.get('sha256', '')}`",
        f"- Final thumbnail: `{cover.get('path', '')}`",
        f"- QA review: `{review.get('path', '')}`",
        "",
        "## Upload Gate",
        "",
        "- Public upload, scheduling, and metadata changes require the operator confirmation.",
        "- Do not publish if source-lock warnings remain unresolved.",
    ]
    if warnings:
        lines.extend(["", "## Source-Lock Warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)
    lines.append("")
    return "\n".join(lines)


def write_pack(project, pack):
    destination = project / "youtube-publish-pack.md"
    try:
        existing_mode = destination.lstat().st_mode
    except FileNotFoundError:
        existing_mode = 0
    mode = stat.S_IMODE(existing_mode) if stat.S_ISREG(existing_mode) else 0o644
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=project,
            prefix=".youtube-publish-pack.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(pack)
        temporary.chmod(mode)
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Create a YouTube publish pack.")
    parser.add_argument("project", help="Project slug under projects/ or a project path")
    parser.add_argument("--workspace", default=Path(__file__).resolve().parents[1], type=Path)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    workspace = args.workspace.resolve()
    project = agent_status.resolve_project(args.project, workspace)
    if not project.exists():
        raise SystemExit(f"project not found: {project}")
    pack = build_pack(project, workspace)
    if args.write:
        write_pack(project, pack)
    print(pack)


if __name__ == "__main__":
    main()
