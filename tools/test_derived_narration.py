import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


SCRIPT = Path(__file__).with_name("derived_narration.py")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(action: str, project: Path):
    return subprocess.run(
        [sys.executable, "-I", "-S", str(SCRIPT), action, str(project)],
        capture_output=True,
        text=True,
    )


def _interrupt_promotion(project: Path, boundary: str):
    code = (
        "import importlib.util, os, pathlib, sys\n"
        "spec=importlib.util.spec_from_file_location('derived_narration_interrupted', sys.argv[1])\n"
        "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)\n"
        "wanted=sys.argv[2]\n"
        "def interrupt(name):\n"
        "    if name == wanted: os._exit(91)\n"
        "module._failure_point=interrupt\n"
        "module.promote(pathlib.Path(sys.argv[3]))\n"
    )
    return subprocess.run(
        [sys.executable, "-I", "-S", "-c", code, str(SCRIPT), boundary, str(project)],
        capture_output=True,
        text=True,
    )


def _make_project(root: Path, *, tempo=1.25) -> Path:
    project = root / "demo"
    (project / ".hvp/staging").mkdir(parents=True)
    audio = project / "narration-final.mp3"
    subprocess.run(
        [
            shutil.which("ffmpeg") or "ffmpeg",
            "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=2.4",
            "-c:a", "libmp3lame", "-b:a", "96k", str(audio),
        ],
        check=True,
    )
    (project / "narration-final.srt").write_text(
        "1\n00:00:00,000 --> 00:00:01,200\n第一句\n\n"
        "2\n00:00:01,200 --> 00:00:02,400\n第二句\n",
        encoding="utf-8",
    )
    (project / "narration-final.mp3.pron-ok.json").write_text(
        json.dumps(
            {
                "schema": "haru.pronunciation_approval.v1",
                "status": "pass",
                "sha256": _sha(audio),
                "warnings": [],
                "approved_by": "harvey",
            }
        ),
        encoding="utf-8",
    )
    srt = project / "narration-final.srt"
    (project / ".hvp/staging/narration-derivation-request.json").write_text(
        json.dumps(
            {
                "schema": "haru.narration_derivation_request.v1",
                "tempo": tempo,
                "source_audio_sha256": _sha(audio),
                "source_srt_sha256": _sha(srt),
            }
        ),
        encoding="utf-8",
    )
    # These should be retired because their cue clock belongs to the old SRT.
    (project / "storyboard-final-timed.json").write_text("{}", encoding="utf-8")
    (project / "storyboard-final-timed-validation.json").write_text("{}", encoding="utf-8")
    return project


def _prepare(project: Path) -> dict:
    result = _run("prepare", project)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def _accept(project: Path, receipt: dict) -> Path:
    receipt_path = (
        project
        / ".hvp/staging/narration-derivations"
        / receipt["request_sha256"]
        / "derivation.json"
    )
    acceptance = project / ".hvp/staging/narration-derivation-acceptance.json"
    acceptance.write_text(
        json.dumps(
            {
                "schema": "haru.narration_derivation_acceptance.v1",
                "accepted_by": "harvey",
                "candidate_audio": receipt["artifacts"]["audio"]["path"],
                "audio_sha256": receipt["artifacts"]["audio"]["sha256"],
                "derivation_receipt_sha256": _sha(receipt_path),
                "accepted_issues": [],
            }
        ),
        encoding="utf-8",
    )
    return acceptance


def test_prepare_uses_real_atempo_and_retimes_srt_without_canonical_writes():
    with tempfile.TemporaryDirectory() as tmp:
        project = _make_project(Path(tmp), tempo=1.25)
        before = {
            name: _sha(project / name)
            for name in (
                "narration-final.mp3",
                "narration-final.srt",
                "narration-final.mp3.pron-ok.json",
            )
        }
        receipt = _prepare(project)

        assert receipt["schema"] == "haru.narration_derivation.v1"
        assert receipt["decode_status"] == "pass"
        assert receipt["artifacts"]["audio"]["path"].endswith("/narration.mp3")
        assert receipt["artifacts"]["srt"]["path"].endswith("/narration.srt")
        assert math.isclose(
            receipt["candidate_duration_seconds"],
            receipt["source_duration_seconds"] / 1.25,
            rel_tol=0.03,
            abs_tol=0.15,
        )
        srt = (project / receipt["artifacts"]["srt"]["path"]).read_text()
        assert "00:00:00,960" in srt
        assert "00:00:01,920" in srt
        assert before == {name: _sha(project / name) for name in before}


def test_promote_refuses_missing_human_acceptance():
    with tempfile.TemporaryDirectory() as tmp:
        project = _make_project(Path(tmp))
        _prepare(project)
        result = _run("promote", project)
        assert result.returncode == 2
        assert json.loads(result.stdout)["code"] == "invalid_path"


def test_promote_refuses_stale_source_and_changed_candidate():
    with tempfile.TemporaryDirectory() as tmp:
        project = _make_project(Path(tmp))
        receipt = _prepare(project)
        _accept(project, receipt)
        (project / "narration-final.srt").write_text(
            "1\n00:00:00,000 --> 00:00:02,400\nchanged\n", encoding="utf-8"
        )
        stale = _run("promote", project)
        assert stale.returncode == 2
        assert json.loads(stale.stdout)["code"] == "derivation_receipt_stale"

    with tempfile.TemporaryDirectory() as tmp:
        project = _make_project(Path(tmp))
        receipt = _prepare(project)
        _accept(project, receipt)
        (project / receipt["artifacts"]["audio"]["path"]).write_bytes(b"changed")
        stale = _run("promote", project)
        assert stale.returncode == 2
        assert json.loads(stale.stdout)["code"] == "derivation_receipt_stale"


def test_symlinks_and_nonfinite_tempo_fail_closed():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project = _make_project(root)
        outside = root / "outside.mp3"
        outside.write_bytes((project / "narration-final.mp3").read_bytes())
        (project / "narration-final.mp3").unlink()
        (project / "narration-final.mp3").symlink_to(outside)
        result = _run("prepare", project)
        assert result.returncode == 2
        assert json.loads(result.stdout)["code"] == "invalid_path"

    for tempo in (float("nan"), float("inf"), 0.49, 2.01, True):
        with tempfile.TemporaryDirectory() as tmp:
            project = _make_project(Path(tmp), tempo=tempo)
            result = _run("prepare", project)
            assert result.returncode == 2, tempo
            assert json.loads(result.stdout)["code"] == "invalid_derivation_request"


def test_promotion_is_digest_bound_archives_old_bytes_and_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        project = _make_project(Path(tmp), tempo=1.25)
        old_audio_sha = _sha(project / "narration-final.mp3")
        receipt = _prepare(project)
        acceptance = _accept(project, receipt)

        first = _run("promote", project)
        second = _run("promote", project)
        assert first.returncode == 0 and second.returncode == 0, second.stdout + second.stderr
        first_value, second_value = json.loads(first.stdout), json.loads(second.stdout)
        assert first_value == second_value
        assert first_value["schema"] == "haru.narration_derivation_promotion.v1"
        assert first_value["request_sha256"] == _sha(acceptance)
        assert first_value["audio_sha256"] == _sha(project / "narration-final.mp3")
        assert first_value["audio_sha256"] == receipt["artifacts"]["audio"]["sha256"]
        assert len(first_value["archived_superseded"]) == 3
        assert (project / f"narration-final.mp3.superseded-{old_audio_sha[:12]}").is_file()
        assert first_value["retired_timed_storyboard"] == [
            "storyboard-final-timed.json",
            "storyboard-final-timed-validation.json",
        ]

        stamp = json.loads((project / "narration-final.mp3.pron-ok.json").read_text())
        assert stamp["sha256"] == first_value["audio_sha256"]
        assert stamp["approved_by"] == "harvey"
        assert stamp["derived_from"]["acceptance_sha256"] == _sha(acceptance)

        # The original request remains bound to the old canonical source. A
        # careless prepare retry after promotion therefore cannot speed the
        # already-derived audio a second time.
        repeated_prepare = _run("prepare", project)
        assert repeated_prepare.returncode == 2
        assert json.loads(repeated_prepare.stdout)["code"] == "derivation_source_mismatch"


def test_promotion_refuses_a_symlinked_candidate_after_acceptance():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project = _make_project(root)
        receipt = _prepare(project)
        _accept(project, receipt)
        candidate = project / receipt["artifacts"]["audio"]["path"]
        outside = root / "candidate.mp3"
        outside.write_bytes(candidate.read_bytes())
        candidate.unlink()
        candidate.symlink_to(outside)
        result = _run("promote", project)
        assert result.returncode == 2
        assert json.loads(result.stdout)["code"] == "derivation_receipt_stale"


def test_every_transaction_boundary_recovers_by_finishing_forward():
    boundaries = (
        "journal",
        "archive-audio",
        "archive-srt",
        "archive-pronunciation_stamp",
        "install-audio",
        "install-srt",
        "install-pronunciation_stamp",
        "retire-storyboard-final-timed.json",
        "retire-storyboard-final-timed-validation.json",
        "promotion-receipt",
    )
    for boundary in boundaries:
        with tempfile.TemporaryDirectory() as tmp:
            project = _make_project(Path(tmp))
            receipt = _prepare(project)
            _accept(project, receipt)
            interrupted = _interrupt_promotion(project, boundary)
            assert interrupted.returncode == 91, boundary
            assert (project / ".hvp/narration-derivation-transaction.json").is_file()

            recovered = _run("promote", project)
            assert recovered.returncode == 0, (boundary, recovered.stdout, recovered.stderr)
            promotion = json.loads(recovered.stdout)
            assert _sha(project / "narration-final.mp3") == promotion["audio_sha256"]
            assert _sha(project / "narration-final.srt") == promotion["srt_sha256"]
            assert not (project / ".hvp/narration-derivation-transaction.json").exists()


def test_changed_evidence_is_untouched_until_restored_then_recovery_finishes():
    for mutation in ("acceptance", "candidate"):
        with tempfile.TemporaryDirectory() as tmp:
            project = _make_project(Path(tmp))
            original = {
                name: (project / name).read_bytes()
                for name in (
                    "narration-final.mp3",
                    "narration-final.srt",
                    "narration-final.mp3.pron-ok.json",
                )
            }
            receipt = _prepare(project)
            acceptance = _accept(project, receipt)
            acceptance_payload = acceptance.read_bytes()
            candidate = project / receipt["artifacts"]["srt"]["path"]
            candidate_payload = candidate.read_bytes()
            interrupted = _interrupt_promotion(project, "install-audio")
            assert interrupted.returncode == 91
            if mutation == "acceptance":
                value = json.loads(acceptance.read_text())
                value["accepted_by"] = "someone-else"
                acceptance.write_text(json.dumps(value), encoding="utf-8")
            else:
                candidate.write_text("changed after interruption", encoding="utf-8")

            recovered = _run("promote", project)
            assert recovered.returncode == 2, mutation
            assert json.loads(recovered.stdout)["code"] == "transaction_evidence_changed"
            assert (project / ".hvp/narration-derivation-transaction.json").is_file()
            # Evidence failure cannot authorize rollback: preserve the exact
            # partially installed state until the fixed evidence is restored.
            assert (project / "narration-final.mp3").read_bytes() != original["narration-final.mp3"]
            assert (project / "narration-final.srt").read_bytes() == original["narration-final.srt"]
            if mutation == "acceptance":
                acceptance.write_bytes(acceptance_payload)
            else:
                candidate.write_bytes(candidate_payload)
            finished = _run("promote", project)
            assert finished.returncode == 0, (mutation, finished.stdout, finished.stderr)
            assert not (project / ".hvp/narration-derivation-transaction.json").exists()


def test_recovery_rejects_forged_journal_and_preserves_unknown_source_edit():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project = _make_project(root)
        receipt = _prepare(project)
        _accept(project, receipt)
        interrupted = _interrupt_promotion(project, "journal")
        assert interrupted.returncode == 91
        journal_path = project / ".hvp/narration-derivation-transaction.json"
        journal = json.loads(journal_path.read_text())
        outside = root / "must-not-touch"
        outside.write_bytes(b"private")
        journal["archives"]["audio"]["path"] = "../must-not-touch"
        journal_path.write_text(json.dumps(journal), encoding="utf-8")
        rejected = _run("promote", project)
        assert rejected.returncode == 2
        assert json.loads(rejected.stdout)["code"] == "invalid_derivation_transaction"
        assert outside.read_bytes() == b"private"

    with tempfile.TemporaryDirectory() as tmp:
        project = _make_project(Path(tmp))
        receipt = _prepare(project)
        _accept(project, receipt)
        interrupted = _interrupt_promotion(project, "archive-audio")
        assert interrupted.returncode == 91
        newer_srt = b"1\n00:00:00,000 --> 00:00:02,400\nnewer user edit\n"
        (project / "narration-final.srt").write_bytes(newer_srt)
        rejected = _run("promote", project)
        assert rejected.returncode == 2
        assert json.loads(rejected.stdout)["code"] == "derivation_source_changed"
        assert (project / "narration-final.srt").read_bytes() == newer_srt
        assert not (project / ".hvp/narration-derivation-transaction.json").exists()


def test_shape_valid_evidence_invalid_journal_cannot_authorize_rollback():
    with tempfile.TemporaryDirectory() as tmp:
        project = _make_project(Path(tmp))
        receipt = _prepare(project)
        _accept(project, receipt)
        assert _interrupt_promotion(project, "journal").returncode == 91
        journal_path = project / ".hvp/narration-derivation-transaction.json"
        journal = json.loads(journal_path.read_text())
        canonical_before = {
            name: (project / name).read_bytes()
            for name in (
                "narration-final.mp3",
                "narration-final.srt",
                "narration-final.mp3.pron-ok.json",
            )
        }
        forged_old = {
            "audio": b"forged old audio",
            "srt": b"forged old srt",
            "pronunciation_stamp": b"forged old stamp",
        }
        suffix = hashlib.sha256(forged_old["audio"]).hexdigest()[:12]
        names = {
            "audio": "narration-final.mp3",
            "srt": "narration-final.srt",
            "pronunciation_stamp": "narration-final.mp3.pron-ok.json",
        }
        for key, name in names.items():
            old_sha = hashlib.sha256(forged_old[key]).hexdigest()
            journal["source"][key]["sha256"] = old_sha
            journal["canonical"][key]["sha256"] = hashlib.sha256(
                canonical_before[name]
            ).hexdigest()
            journal["archives"][key] = {
                "path": f"{name}.superseded-{suffix}",
                "sha256": old_sha,
            }
            (project / journal["archives"][key]["path"]).write_bytes(forged_old[key])
        forged_retirement_suffix = journal["canonical"]["srt"]["sha256"][:12]
        for item in journal["retirements"]:
            item["target"] = (
                f"{item['source']}.stale-narration-{forged_retirement_suffix}"
            )
        journal_path.write_text(json.dumps(journal), encoding="utf-8")

        rejected = _run("promote", project)
        assert rejected.returncode == 2
        assert json.loads(rejected.stdout)["code"] == "transaction_evidence_changed"
        assert journal_path.is_file()
        assert all(
            (project / name).read_bytes() == payload
            for name, payload in canonical_before.items()
        )


if __name__ == "__main__":
    tests = sorted(
        (name, value)
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    )
    for _name, test in tests:
        test()
    print(f"{len(tests)} derived narration tests passed")
