import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("editorial_preview.py")

pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe not available",
)


def run(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True,
    )


FAKE_NPX = """#!/bin/sh
# Stands in for `npx remotion render <composition> <out> --frames=a-b`.
out=""
for arg in "$@"; do
  case "$arg" in
    --*) ;;
    remotion|render) ;;
    *) [ -z "$out" ] && continue || true ;;
  esac
done
out="$4"
# The runner spawns npx with a fixed, isolated env, so the knob is a file
# in the project (cwd is the remotion dir) rather than a variable.
seconds=70
[ -f ../preview-seconds ] && seconds="$(cat ../preview-seconds)"
ffmpeg -hide_banner -loglevel error -y -f lavfi \
  -i "color=c=black:s=64x36:d=$seconds" -r 30 "$out"
"""


def _project(root, *, contract_digest=None):
    project = Path(root) / "demo"
    (project / ".hvp/staging").mkdir(parents=True)
    (project / "remotion").mkdir(parents=True)

    contract = {"schema": "haru.editorial_contract.v1", "shots": []}
    contract_path = project / "editorial-contract.json"
    contract_path.write_text(json.dumps(contract, ensure_ascii=False), encoding="utf-8")
    digest = contract_digest or hashlib.sha256(contract_path.read_bytes()).hexdigest()

    (project / "render_plan.json").write_text(json.dumps({
        "schema": "haru.render_plan.v1", "engine": "remotion", "remotion_dir": "remotion",
        "composition": "Demo", "fps": 30, "editorial_contract": "editorial-contract.json",
        "editorial_contract_sha256": digest,
    }), encoding="utf-8")
    (project / ".hvp/staging/editorial-preview-request.json").write_text(json.dumps({
        "schema": "haru.editorial_preview_request.v1",
        "start_seconds": 120.0, "duration_seconds": 70.0,
    }), encoding="utf-8")

    npx = Path(root) / "fake-npx"
    npx.write_text(FAKE_NPX, encoding="utf-8")
    npx.chmod(0o755)
    return project, npx


def _render(project, npx, **env):
    import os
    return subprocess.run(
        [sys.executable, str(SCRIPT), "render", str(project)],
        capture_output=True, text=True,
        env={**os.environ, "HARU_NPX": str(npx), **env},
    )


def test_render_produces_a_preview_bound_to_the_contract():
    with tempfile.TemporaryDirectory() as tmp:
        project, npx = _project(tmp)
        result = _render(project, npx)
        assert result.returncode == 0, result.stdout + result.stderr
        receipt = json.loads(result.stdout)

        preview = project / "quality-review/editorial-preview/preview.mp4"
        assert preview.is_file()
        assert receipt["preview_sha256"] == hashlib.sha256(preview.read_bytes()).hexdigest()
        assert receipt["editorial_contract_sha256"] == hashlib.sha256(
            (project / "editorial-contract.json").read_bytes()).hexdigest()
        # 120s at 30fps, 70s long.
        assert receipt["frames"] == {"first": 3600, "last": 5699, "fps": 30}
        assert receipt["requires_human_verdict"] is True


def test_a_preview_of_a_stale_cut_is_refused_before_rendering():
    # The same binding render-project demands, checked minutes earlier and with
    # a message that names the stale artifact.
    with tempfile.TemporaryDirectory() as tmp:
        project, npx = _project(tmp, contract_digest="stale")
        result = _render(project, npx)
        assert result.returncode == 2, result.stdout
        assert json.loads(result.stdout)["code"] == "render_plan_stale"
        assert not (project / "quality-review/editorial-preview/preview.mp4").exists()


def test_a_window_outside_the_reviewable_band_is_refused():
    for duration in (45.0, 120.0, "70", True):
        with tempfile.TemporaryDirectory() as tmp:
            project, npx = _project(tmp)
            request = project / ".hvp/staging/editorial-preview-request.json"
            payload = json.loads(request.read_text())
            payload["duration_seconds"] = duration
            request.write_text(json.dumps(payload), encoding="utf-8")

            result = _render(project, npx)
            assert result.returncode == 2, (duration, result.stdout)
            assert json.loads(result.stdout)["code"] == "invalid_preview_request"


def test_a_preview_that_is_not_the_window_asked_for_is_refused():
    # A renderer that quietly produced a different span would put the wrong cut
    # in front of the reviewer, and the verdict would name bytes nobody chose.
    with tempfile.TemporaryDirectory() as tmp:
        project, npx = _project(tmp)
        (project / "preview-seconds").write_text("30", encoding="utf-8")
        result = _render(project, npx)
        assert result.returncode == 2, result.stdout
        assert json.loads(result.stdout)["code"] == "preview_duration_mismatch"


def test_the_verdict_names_the_preview_it_was_given():
    with tempfile.TemporaryDirectory() as tmp:
        project, npx = _project(tmp)
        assert _render(project, npx).returncode == 0

        result = run("review", project, "--reviewed-by", "harvey",
                     "--verdict", "pass", "--notes", "looks right")
        assert result.returncode == 0, result.stderr
        review = json.loads(result.stdout)
        assert review["schema"] == "haru.editorial_preview_review.v1"
        assert review["verdict"] == "pass"
        assert review["reviewed_by"] == "harvey"
        preview = project / "quality-review/editorial-preview/preview.mp4"
        assert review["preview_sha256"] == hashlib.sha256(preview.read_bytes()).hexdigest()
        assert (project / "quality-review/editorial-preview/review.json").is_file()


def test_a_verdict_cannot_be_recorded_against_a_cut_that_has_moved_on():
    # This is why a re-time invalidates the previous approval: the verdict names
    # bytes, and re-timing produces different ones.
    with tempfile.TemporaryDirectory() as tmp:
        project, npx = _project(tmp)
        assert _render(project, npx).returncode == 0
        contract = project / "editorial-contract.json"
        contract.write_text(json.dumps({"schema": "haru.editorial_contract.v1",
                                        "shots": [{"event_id": "new"}]}), encoding="utf-8")

        result = run("review", project, "--reviewed-by", "harvey",
                     "--verdict", "pass", "--notes", "still fine?")
        assert result.returncode == 2, result.stdout
        assert json.loads(result.stdout)["code"] == "preview_stale"
        assert not (project / "quality-review/editorial-preview/review.json").exists()


def test_a_verdict_needs_a_named_reviewer_and_notes():
    with tempfile.TemporaryDirectory() as tmp:
        project, npx = _project(tmp)
        assert _render(project, npx).returncode == 0
        for reviewer, notes in ((" ", "ok"), ("harvey", "  ")):
            result = run("review", project, "--reviewed-by", reviewer,
                         "--verdict", "pass", "--notes", notes)
            assert result.returncode == 2, result.stdout
            assert json.loads(result.stdout)["code"] == "invalid_review"
