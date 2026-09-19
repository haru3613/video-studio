#!/usr/bin/env python3
"""Render a short window of the cut for a human to approve before the full render.

The editorial preview gate exists so nobody spends a twenty-minute render on a
cut that is wrong. It is the last gate before `render-project`, and it is the
only one whose verdict a machine cannot supply: everything else here is measured,
this one is watched.

Until now the preview was produced by hand, which meant the gate was only as
reliable as someone remembering how. Re-timing a project against a new narration
invalidates the previous verdict by design -- the review is bound to the
editorial contract's digest -- so a hand-made step sat in the middle of a loop
the rest of this pipeline automates.

Two actions, deliberately separate:

  * `render`  -- produce the window and a contact sheet, bound to the contract
  * `review`  -- record a named human's verdict on THAT window

Splitting them is the point. The renderer cannot approve its own output, and the
verdict names the bytes it was given, so it cannot survive the next re-time.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PREVIEW_REQUEST_SCHEMA = "haru.editorial_preview_request.v1"
PREVIEW_SCHEMA = "haru.editorial_preview.v1"
REVIEW_SCHEMA = "haru.editorial_preview_review.v1"
# editorial_contract.py refuses a preview outside this band: short enough that
# reviewing it is cheap, long enough to show rhythm rather than a single shot.
PREVIEW_MIN_SECONDS = 60.0
PREVIEW_MAX_SECONDS = 90.0
PREVIEW_DURATION_TOLERANCE_SECONDS = 1.0
PREVIEW_DIRECTORY = "quality-review/editorial-preview"


class PreviewError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def direct_file(path: Path) -> Path:
    if path.is_symlink() or not path.is_file():
        raise PreviewError("invalid_path", f"expected a direct file: {path.name}")
    return path.resolve(strict=True)


def direct_directory(path: Path) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise PreviewError("invalid_path", "expected a direct directory")
    return path.resolve(strict=True)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreviewError("invalid_artifact", str(exc)) from exc
    if not isinstance(value, dict):
        raise PreviewError("invalid_artifact", f"{path.name} must be an object")
    return value


def replace_file_atomically(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def write_json_atomically(path: Path, value: dict) -> None:
    replace_file_atomically(
        path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def tool_path() -> str:
    return os.pathsep.join(
        [*os.get_exec_path(), "/opt/homebrew/bin", "/usr/local/bin"]
    )


def binary(name: str, override: str) -> str:
    configured = os.environ.get(override)
    if configured:
        return configured
    found = shutil.which(name, path=tool_path())
    if not found:
        raise PreviewError("runtime_unavailable", f"{name} is unavailable")
    return found


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [binary("ffprobe", "HARU_FFPROBE"), "-v", "error", "-show_entries",
         "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=False, timeout=120,
    )
    try:
        return round(float(result.stdout.strip()), 3)
    except ValueError as exc:
        raise PreviewError("preview_unmeasurable", f"cannot measure {path.name}") from exc


def render_preview(project_path: Path) -> dict:
    project = direct_directory(project_path)
    state = direct_directory(project / ".hvp")
    staging = direct_directory(state / "staging")

    request_path = direct_file(staging / "editorial-preview-request.json")
    request = read_json(request_path)
    start = request.get("start_seconds")
    duration = request.get("duration_seconds")
    if not (
        request.get("schema") == PREVIEW_REQUEST_SCHEMA
        and isinstance(start, (int, float)) and not isinstance(start, bool) and start >= 0
        and isinstance(duration, (int, float)) and not isinstance(duration, bool)
        and PREVIEW_MIN_SECONDS <= duration <= PREVIEW_MAX_SECONDS
    ):
        raise PreviewError(
            "invalid_preview_request",
            f"preview must name a start and a duration between "
            f"{PREVIEW_MIN_SECONDS:.0f} and {PREVIEW_MAX_SECONDS:.0f} seconds",
        )

    plan = read_json(direct_file(project / "render_plan.json"))
    contract_path = direct_file(project / "editorial-contract.json")
    contract_digest = sha256(contract_path)
    if plan.get("editorial_contract_sha256") != contract_digest:
        # The same binding render-project demands. Failing here says which
        # artifact is stale, minutes before the full render would have.
        raise PreviewError(
            "render_plan_stale",
            "render plan is not bound to the current editorial contract "
            "(run retime-visuals)",
        )
    composition = plan.get("composition")
    remotion_value = plan.get("remotion_dir")
    fps = plan.get("fps", 30)
    if not (
        isinstance(composition, str) and composition
        and isinstance(remotion_value, str) and remotion_value
        and isinstance(fps, int) and not isinstance(fps, bool) and 1 <= fps <= 120
    ):
        raise PreviewError("invalid_artifact", "render plan does not describe a composition")
    remotion = direct_directory(project / remotion_value)
    if not remotion.is_relative_to(project):
        raise PreviewError("invalid_path", "remotion_dir escapes the project")

    first = round(start * fps)
    last = first + round(duration * fps) - 1
    preview_dir = project / PREVIEW_DIRECTORY
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview = preview_dir / "preview.mp4"
    if preview.is_symlink():
        raise PreviewError("invalid_path", "preview.mp4 must not be a symlink")

    with tempfile.TemporaryDirectory(dir=preview_dir) as work:
        raw = Path(work) / "preview.mp4"
        result = subprocess.run(
            [binary("npx", "HARU_NPX"), "remotion", "render", composition, str(raw),
             f"--frames={first}-{last}", "--concurrency=1"],
            cwd=str(remotion), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, check=False,
            env={
                "HOME": str(direct_directory(state)),
                "LANG": "C.UTF-8",
                "npm_config_offline": "true",
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            },
        )
        if result.returncode != 0 or not raw.is_file():
            tail = (result.stderr or result.stdout or "no output").strip()[-800:]
            raise PreviewError("preview_render_failed", f"remotion render failed: {tail}")
        measured = probe_duration(raw)
        if abs(measured - duration) > PREVIEW_DURATION_TOLERANCE_SECONDS:
            raise PreviewError(
                "preview_duration_mismatch",
                f"asked for {duration:.3f}s and got {measured:.3f}s",
            )
        replace_file_atomically(preview, raw.read_bytes())

        sheet = preview_dir / "contact-sheet.png"
        subprocess.run(
            [binary("ffmpeg", "HARU_FFMPEG"), "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(preview), "-vf", "fps=1/6,scale=320:-1,tile=5x3",
             "-frames:v", "1", str(Path(work) / "sheet.png")],
            check=False, capture_output=True, timeout=300,
        )
        contact_sheet = Path(work) / "sheet.png"
        if contact_sheet.is_file():
            replace_file_atomically(sheet, contact_sheet.read_bytes())

    receipt = {
        "schema": PREVIEW_SCHEMA,
        "project": project.name,
        "status": "complete",
        "preview": f"{PREVIEW_DIRECTORY}/preview.mp4",
        "preview_sha256": sha256(preview),
        "start_seconds": float(start),
        "duration_seconds": probe_duration(preview),
        "frames": {"first": first, "last": last, "fps": fps},
        "editorial_contract_sha256": contract_digest,
        "rendered_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        # The verdict is a separate act by a named human. This receipt only says
        # what was produced and what it was produced from.
        "requires_human_verdict": True,
    }
    write_json_atomically(state / "editorial-preview.json", receipt)
    return receipt


def review_preview(project_path: Path, reviewed_by: str, verdict: str, notes: str) -> dict:
    """Record a human's verdict on the preview that was actually rendered.

    Bound to the preview's bytes and the contract's digest, so approving one cut
    can never stand in for another -- which is the whole reason a re-time
    invalidates the previous verdict.
    """
    project = direct_directory(project_path)
    state = direct_directory(project / ".hvp")
    if not reviewed_by.strip() or verdict not in {"pass", "fail"} or not notes.strip():
        raise PreviewError("invalid_review", "a named reviewer, a verdict and notes are required")

    receipt = read_json(direct_file(state / "editorial-preview.json"))
    preview = direct_file(project / f"{PREVIEW_DIRECTORY}/preview.mp4")
    contract_digest = sha256(direct_file(project / "editorial-contract.json"))
    if (
        receipt.get("schema") != PREVIEW_SCHEMA
        or receipt.get("preview_sha256") != sha256(preview)
        or receipt.get("editorial_contract_sha256") != contract_digest
    ):
        raise PreviewError(
            "preview_stale",
            "the rendered preview is not the current cut; render it again first",
        )

    review = {
        "schema": REVIEW_SCHEMA,
        "project": project.name,
        "preview": receipt["preview"],
        "preview_sha256": receipt["preview_sha256"],
        "duration_seconds": receipt["duration_seconds"],
        "editorial_contract_sha256": contract_digest,
        "verdict": verdict,
        "reviewed_by": reviewed_by.strip(),
        "reviewed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "notes": notes.strip(),
    }
    write_json_atomically(project / f"{PREVIEW_DIRECTORY}/review.json", review)
    return review


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    render_parser = subparsers.add_parser("render")
    render_parser.add_argument("project", type=Path)
    review_parser = subparsers.add_parser("review")
    review_parser.add_argument("project", type=Path)
    review_parser.add_argument("--reviewed-by", required=True)
    review_parser.add_argument("--verdict", required=True, choices=["pass", "fail"])
    review_parser.add_argument("--notes", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "render":
            value = render_preview(args.project)
        else:
            value = review_preview(args.project, args.reviewed_by, args.verdict, args.notes)
    except PreviewError as exc:
        print(json.dumps(
            {"schema_version": 1, "outcome": "error", "code": exc.code, "data": None}
        ))
        return 2
    print(json.dumps(value, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
