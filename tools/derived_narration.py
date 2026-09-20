#!/usr/bin/env python3
"""Prepare and promote digest-bound tempo derivations of canonical narration."""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

# The MCP runner uses `/usr/bin/python3 -I -S`; isolated mode intentionally
# omits the script directory from sys.path, so add only this owned tools dir.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pronunciation_workflow import (
    PRONUNCIATION_APPROVAL_SCHEMA,
    WorkflowError,
    direct_directory,
    direct_file,
    executable_from_env,
    read_json,
    replace_file_atomically,
    sha256,
    write_json_atomically,
)


REQUEST_SCHEMA = "haru.narration_derivation_request.v1"
DERIVATION_SCHEMA = "haru.narration_derivation.v1"
ACCEPTANCE_SCHEMA = "haru.narration_derivation_acceptance.v1"
PROMOTION_SCHEMA = "haru.narration_derivation_promotion.v1"
REQUEST_PATH = Path(".hvp/staging/narration-derivation-request.json")
ACCEPTANCE_PATH = Path(".hvp/staging/narration-derivation-acceptance.json")
DERIVATIONS_ROOT = Path(".hvp/staging/narration-derivations")
PROMOTION_PATH = Path(".hvp/narration-derivation-promotion.json")
TRANSACTION_PATH = Path(".hvp/narration-derivation-transaction.json")
TRANSACTION_SCHEMA = "haru.narration_derivation_transaction.v1"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
SRT_TIME_RE = re.compile(
    r"^(\d{2,}):(\d{2}):(\d{2}),(\d{3}) --> "
    r"(\d{2,}):(\d{2}):(\d{2}),(\d{3})$"
)


def _contained_direct(path: Path, project: Path, *, kind: str) -> Path:
    """Require a direct path below project; no symlink component is accepted."""
    try:
        relative = Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(project)))
    except ValueError as exc:
        raise WorkflowError("invalid_path", f"{kind} must live inside the project") from exc
    current = project
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise WorkflowError("invalid_path", f"{kind} cannot cross a symlink")
    candidate = direct_file(path) if kind == "file" else direct_directory(path)
    try:
        candidate.relative_to(project)
    except ValueError as exc:
        raise WorkflowError("invalid_path", f"{kind} must live inside the project") from exc
    return candidate


def _ensure_direct_directory(path: Path, project: Path) -> Path:
    """Create a directory only after checking every existing parent component."""
    try:
        relative = Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(project)))
    except ValueError as exc:
        raise WorkflowError("invalid_path", "state directory must live inside the project") from exc
    current = project
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise WorkflowError("invalid_path", "state directories cannot cross a symlink")
        if current.exists() and not current.is_dir():
            raise WorkflowError("invalid_path", "state path must be a directory")
        current.mkdir(exist_ok=True)
    return _contained_direct(path, project, kind="directory")


def _artifact(path: Path, project: Path) -> dict:
    return {"path": str(path.relative_to(project)), "sha256": sha256(path)}


def _canonical_inputs(project: Path) -> tuple[dict, dict]:
    audio = _contained_direct(project / "narration-final.mp3", project, kind="file")
    srt = _contained_direct(project / "narration-final.srt", project, kind="file")
    stamp_path = _contained_direct(
        project / "narration-final.mp3.pron-ok.json", project, kind="file"
    )
    stamp = read_json(stamp_path)
    audio_sha = sha256(audio)
    if not (
        stamp.get("schema") == PRONUNCIATION_APPROVAL_SCHEMA
        and stamp.get("status") == "pass"
        and stamp.get("warnings") == []
        and stamp.get("sha256") == audio_sha
    ):
        raise WorkflowError(
            "pronunciation_approval_stale",
            "canonical narration does not have a matching passing pronunciation stamp",
        )
    _parse_srt(srt.read_text(encoding="utf-8"))
    return {
        "audio": _artifact(audio, project),
        "srt": _artifact(srt, project),
        "pronunciation_stamp": _artifact(stamp_path, project),
    }, stamp


def _seconds(match: re.Match[str], start: int) -> float:
    hours, minutes, seconds, millis = (int(match.group(start + offset)) for offset in range(4))
    if minutes >= 60 or seconds >= 60:
        raise WorkflowError("invalid_srt", "SRT timestamp is out of range")
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


def _format_time(value: float) -> str:
    millis = max(0, round(value * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    seconds, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _parse_srt(text: str) -> list[tuple[str, float, float, list[str]]]:
    blocks = re.split(r"\r?\n\s*\r?\n", text.strip()) if text.strip() else []
    cues: list[tuple[str, float, float, list[str]]] = []
    previous_start = -1.0
    for block in blocks:
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].strip():
            raise WorkflowError("invalid_srt", "SRT cue is malformed")
        match = SRT_TIME_RE.fullmatch(lines[1].strip())
        if match is None:
            raise WorkflowError("invalid_srt", "SRT timestamp is malformed")
        start, end = _seconds(match, 1), _seconds(match, 5)
        if end < start or start < previous_start or not any(line.strip() for line in lines[2:]):
            raise WorkflowError("invalid_srt", "SRT cue timing or text is invalid")
        cues.append((lines[0].strip(), start, end, lines[2:]))
        previous_start = start
    if not cues:
        raise WorkflowError("invalid_srt", "SRT must contain at least one cue")
    return cues


def _retimed_srt_payload(source: Path, tempo: float) -> bytes:
    cues = _parse_srt(source.read_text(encoding="utf-8"))
    blocks = [
        "\n".join(
            [index, f"{_format_time(start / tempo)} --> {_format_time(end / tempo)}", *text]
        )
        for index, start, end, text in cues
    ]
    payload = ("\n\n".join(blocks) + "\n").encode("utf-8")
    _parse_srt(payload.decode("utf-8"))
    return payload


def _duration(ffprobe: Path, path: Path) -> float:
    result = subprocess.run(
        [str(ffprobe), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        duration = float(result.stdout.strip())
    except ValueError:
        duration = math.nan
    if result.returncode != 0 or not math.isfinite(duration) or duration <= 0:
        raise WorkflowError("media_probe_failed", f"could not measure {path.name}")
    return duration


def _decode(ffmpeg: Path, path: Path) -> None:
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-i", str(path), "-f", "null", "-"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise WorkflowError("media_decode_failed", f"could not fully decode {path.name}")


def _tempo_request(project: Path) -> tuple[Path, dict, float, str]:
    path = _contained_direct(project / REQUEST_PATH, project, kind="file")
    request = read_json(path)
    tempo = request.get("tempo")
    if (
        request.get("schema") != REQUEST_SCHEMA
        or isinstance(tempo, bool)
        or not isinstance(tempo, (int, float))
        or not math.isfinite(float(tempo))
        or not 0.5 <= float(tempo) <= 2.0
        or not isinstance(request.get("source_audio_sha256"), str)
        or not SHA256_RE.fullmatch(request["source_audio_sha256"])
        or not isinstance(request.get("source_srt_sha256"), str)
        or not SHA256_RE.fullmatch(request["source_srt_sha256"])
    ):
        raise WorkflowError(
            "invalid_derivation_request",
            "request must bind current audio/SRT digests and a finite tempo from 0.5 to 2.0",
        )
    return path, request, float(tempo), sha256(path)


def _request_matches_source(request: dict, source: dict) -> bool:
    audio = source.get("audio") if isinstance(source, dict) else None
    srt = source.get("srt") if isinstance(source, dict) else None
    return bool(
        isinstance(audio, dict)
        and isinstance(srt, dict)
        and request.get("source_audio_sha256") == audio.get("sha256")
        and request.get("source_srt_sha256") == srt.get("sha256")
    )


def _source_paths_fixed(source: object) -> bool:
    return bool(
        isinstance(source, dict)
        and isinstance(source.get("audio"), dict)
        and source["audio"].get("path") == "narration-final.mp3"
        and isinstance(source.get("srt"), dict)
        and source["srt"].get("path") == "narration-final.srt"
        and isinstance(source.get("pronunciation_stamp"), dict)
        and source["pronunciation_stamp"].get("path")
        == "narration-final.mp3.pron-ok.json"
        and all(
            isinstance(source[key].get("sha256"), str)
            and SHA256_RE.fullmatch(source[key]["sha256"])
            for key in ("audio", "srt", "pronunciation_stamp")
        )
    )


def _receipt_artifacts_current(project: Path, receipt: dict) -> bool:
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, dict):
        return False
    request_sha = receipt.get("request_sha256")
    if not isinstance(request_sha, str) or not SHA256_RE.fullmatch(request_sha):
        return False
    expected = {
        "audio": str(DERIVATIONS_ROOT / request_sha / "narration.mp3"),
        "srt": str(DERIVATIONS_ROOT / request_sha / "narration.srt"),
    }
    for key in ("audio", "srt"):
        entry = artifacts.get(key)
        if not (
            isinstance(entry, dict)
            and isinstance(entry.get("path"), str)
            and entry.get("path") == expected[key]
            and SHA256_RE.fullmatch(str(entry.get("sha256")))
        ):
            return False
        try:
            path = _contained_direct(project / entry["path"], project, kind="file")
        except WorkflowError:
            return False
        if sha256(path) != entry["sha256"]:
            return False
    return True


def prepare(project_path: Path) -> dict:
    """Build a non-canonical audio/SRT tempo candidate and digest receipt."""
    project = direct_directory(project_path)
    request_path, request, tempo, request_sha = _tempo_request(project)
    source, _stamp = _canonical_inputs(project)
    if not _request_matches_source(request, source):
        raise WorkflowError(
            "derivation_source_mismatch",
            "derivation request does not bind the current canonical narration",
        )
    root = _ensure_direct_directory(project / DERIVATIONS_ROOT, project)
    candidate_dir = _ensure_direct_directory(root / request_sha, project)
    receipt_path = candidate_dir / "derivation.json"
    if receipt_path.is_symlink():
        raise WorkflowError("invalid_path", "derivation receipt cannot be a symlink")
    if receipt_path.is_file():
        existing = read_json(receipt_path)
        if (
            existing.get("schema") == DERIVATION_SCHEMA
            and existing.get("status") == "complete"
            and existing.get("project") == project.name
            and existing.get("request_sha256") == request_sha
            and existing.get("tempo") == tempo
            and existing.get("source") == source
            and existing.get("decode_status") == "pass"
            and _receipt_artifacts_current(project, existing)
        ):
            return existing

    ffmpeg = executable_from_env("HARU_FFMPEG", "ffmpeg")
    ffprobe = executable_from_env("HARU_FFPROBE", "ffprobe")
    source_audio = project / source["audio"]["path"]
    source_srt = project / source["srt"]["path"]
    audio = candidate_dir / "narration.mp3"
    srt = candidate_dir / "narration.srt"
    for path in (audio, srt):
        if path.is_symlink():
            raise WorkflowError("invalid_path", "candidate artifact cannot be a symlink")

    with tempfile.NamedTemporaryFile(suffix=".mp3", dir=candidate_dir, delete=False) as handle:
        temporary_audio = Path(handle.name)
    try:
        result = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i", str(source_audio),
             "-filter:a", f"atempo={tempo:.12g}", "-map_metadata", "-1",
             "-c:a", "libmp3lame", "-b:a", "128k", str(temporary_audio)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise WorkflowError("tempo_derivation_failed", "ffmpeg atempo transform failed")
        temporary_audio = direct_file(temporary_audio)
        source_duration = _duration(ffprobe, source_audio)
        candidate_duration = _duration(ffprobe, temporary_audio)
        _decode(ffmpeg, temporary_audio)
        expected_duration = source_duration / tempo
        tolerance = max(0.15, expected_duration * 0.03)
        if abs(candidate_duration - expected_duration) > tolerance:
            raise WorkflowError("tempo_derivation_failed", "derived audio duration does not match requested tempo")
        os.replace(temporary_audio, audio)
    finally:
        if temporary_audio.exists() and not temporary_audio.is_symlink():
            temporary_audio.unlink()
    replace_file_atomically(srt, _retimed_srt_payload(source_srt, tempo))

    # Bind the receipt to one stable source/request snapshot. If an editor or
    # another runner changed any canonical input while ffmpeg was working, the
    # candidate remains staging-only and no trustworthy receipt is minted.
    current_source, _current_stamp = _canonical_inputs(project)
    if sha256(request_path) != request_sha or current_source != source:
        raise WorkflowError(
            "derivation_source_changed",
            "derivation request or canonical narration changed during prepare",
        )

    receipt = {
        "schema": DERIVATION_SCHEMA,
        "status": "complete",
        "project": project.name,
        "tempo": tempo,
        "request_sha256": request_sha,
        "request": {"path": str(request_path.relative_to(project)), "sha256": request_sha},
        "source": source,
        "source_duration_seconds": source_duration,
        "candidate_duration_seconds": candidate_duration,
        "expected_duration_seconds": expected_duration,
        "decode_status": "pass",
        "decode": {"status": "pass", "whole_file": True},
        "artifacts": {
            "audio": _artifact(direct_file(audio), project),
            "srt": _artifact(direct_file(srt), project),
        },
    }
    write_json_atomically(receipt_path, receipt)
    return receipt


def _load_derivation_for_acceptance(
    project: Path, *, require_current_source: bool = True
) -> tuple[Path, dict, Path, dict, str]:
    acceptance_path = _contained_direct(project / ACCEPTANCE_PATH, project, kind="file")
    acceptance = read_json(acceptance_path)
    accepted_by = acceptance.get("accepted_by")
    candidate_rel = acceptance.get("candidate_audio")
    audio_sha = acceptance.get("audio_sha256")
    receipt_sha = acceptance.get("derivation_receipt_sha256")
    accepted_issues = acceptance.get("accepted_issues", [])
    if not (
        acceptance.get("schema") == ACCEPTANCE_SCHEMA
        and isinstance(accepted_by, str) and accepted_by.strip()
        and isinstance(candidate_rel, str) and candidate_rel
        and isinstance(audio_sha, str) and SHA256_RE.fullmatch(audio_sha)
        and isinstance(receipt_sha, str) and SHA256_RE.fullmatch(receipt_sha)
        and isinstance(accepted_issues, list)
        and all(isinstance(item, dict) and isinstance(item.get("text"), str)
                and isinstance(item.get("note"), str) for item in accepted_issues)
    ):
        raise WorkflowError("invalid_derivation_acceptance", "invalid narration derivation acceptance")
    request_path, request, tempo, request_sha = _tempo_request(project)
    expected_receipt = project / DERIVATIONS_ROOT / request_sha / "derivation.json"
    receipt_path = _contained_direct(expected_receipt, project, kind="file")
    receipt = read_json(receipt_path)
    if sha256(receipt_path) != receipt_sha:
        raise WorkflowError("derivation_receipt_stale", "accepted derivation receipt digest does not match")
    source = receipt.get("source")
    if not (
        receipt.get("schema") == DERIVATION_SCHEMA
        and receipt.get("status") == "complete"
        and receipt.get("project") == project.name
        and receipt.get("tempo") == tempo
        and receipt.get("request_sha256") == request_sha
        and _request_matches_source(request, source)
        and _source_paths_fixed(source)
        and receipt.get("decode_status") == "pass"
        and _receipt_artifacts_current(project, receipt)
    ):
        raise WorkflowError("derivation_receipt_stale", "derivation no longer describes its request and candidates")
    audio_entry = receipt["artifacts"]["audio"]
    if candidate_rel != audio_entry["path"] or audio_sha != audio_entry["sha256"]:
        raise WorkflowError("derivation_candidate_mismatch", "acceptance does not name the receipted candidate")
    # A completed replay is judged against the promoted canonical bytes below.
    # On a first promotion, the canonical source must still be the exact source
    # snapshot from prepare; accepting an old candidate after a newer narration
    # landed would otherwise roll the project backwards.
    promotion_path = project / PROMOTION_PATH
    existing = read_json(promotion_path) if promotion_path.is_file() and not promotion_path.is_symlink() else None
    if require_current_source and not (
        isinstance(existing, dict)
        and _canonical_promotion_current(
            project, existing, sha256(acceptance_path), receipt_sha, audio_sha
        )
    ):
        current_source, _stamp = _canonical_inputs(project)
        if source != current_source:
            raise WorkflowError(
                "derivation_receipt_stale",
                "derivation no longer describes the current canonical source",
            )
    return acceptance_path, acceptance, receipt_path, receipt, sha256(request_path)


def _canonical_promotion_current(project: Path, promotion: dict, acceptance_sha: str,
                                 receipt_sha: str, audio_sha: str) -> bool:
    artifacts = promotion.get("artifacts")
    if not (
        promotion.get("schema") == PROMOTION_SCHEMA
        and promotion.get("status") == "complete"
        and promotion.get("request_sha256") == acceptance_sha
        and promotion.get("derivation_receipt_sha256") == receipt_sha
        and promotion.get("audio_sha256") == audio_sha
        and isinstance(artifacts, dict)
    ):
        return False
    for key, name in (
        ("audio", "narration-final.mp3"),
        ("srt", "narration-final.srt"),
        ("pronunciation_stamp", "narration-final.mp3.pron-ok.json"),
    ):
        entry = artifacts.get(key)
        path = project / name
        if path.is_symlink() or not path.is_file() or not isinstance(entry, dict):
            return False
        if entry.get("path") != name or entry.get("sha256") != sha256(path):
            return False
    return True


def _json_payload(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


@contextlib.contextmanager
def _promotion_lock(project: Path):
    state = _ensure_direct_directory(project / ".hvp", project)
    lock_path = state / "narration-derivation.lock"
    if lock_path.is_symlink():
        raise WorkflowError("invalid_path", "promotion lock cannot be a symlink")
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise WorkflowError("invalid_path", "promotion lock must be a regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _failure_point(name: str) -> None:
    """Internal transaction boundary; tests replace this in their own process."""
    del name


def _path_digest(path: Path) -> str | None:
    if path.is_symlink():
        raise WorkflowError("invalid_path", f"{path.name} cannot be a symlink")
    if not path.exists():
        return None
    if not path.is_file():
        raise WorkflowError("invalid_path", f"{path.name} must be a regular file")
    return sha256(path)


def _copy_if_missing_or_equal(source: Path, target: Path, expected_sha: str) -> None:
    source_sha = _path_digest(source)
    target_sha = _path_digest(target)
    if target_sha is not None:
        if target_sha != expected_sha:
            raise WorkflowError(
                "narration_archive_conflict",
                f"archive {target.name} already contains different bytes",
            )
        return
    if source_sha != expected_sha:
        raise WorkflowError("derivation_source_changed", f"{source.name} changed before archival")
    replace_file_atomically(target, source.read_bytes())
    if sha256(target) != expected_sha:
        raise WorkflowError("narration_archive_failed", f"archive {target.name} is invalid")


CANONICAL_NAMES = {
    "audio": "narration-final.mp3",
    "srt": "narration-final.srt",
    "pronunciation_stamp": "narration-final.mp3.pron-ok.json",
}
RETIRE_NAMES = (
    "storyboard-final-timed.json",
    "storyboard-final-timed-validation.json",
)


def _canonical_entries(transaction: dict) -> list[tuple[str, dict, dict, dict]]:
    old = transaction.get("source")
    new = transaction.get("canonical")
    archives = transaction.get("archives")
    if not (
        _source_paths_fixed(old)
        and isinstance(new, dict)
        and isinstance(archives, dict)
    ):
        raise WorkflowError("invalid_derivation_transaction", "transaction canonical map is invalid")
    result = []
    suffix = old["audio"]["sha256"][:12]
    for key, name in CANONICAL_NAMES.items():
        new_entry = new.get(key)
        archive = archives.get(key)
        expected_archive = f"{name}.superseded-{suffix}"
        if not (
            isinstance(new_entry, dict)
            and new_entry.get("path") == name
            and isinstance(new_entry.get("sha256"), str)
            and SHA256_RE.fullmatch(new_entry["sha256"])
            and isinstance(archive, dict)
            and archive.get("path") == expected_archive
            and archive.get("sha256") == old[key]["sha256"]
        ):
            raise WorkflowError("invalid_derivation_transaction", "transaction paths are not fixed")
        result.append((key, old[key], new_entry, archive))
    return result


def _retirement_entries(project: Path, new_srt_sha: str) -> list[dict]:
    result = []
    suffix = new_srt_sha[:12]
    for name in RETIRE_NAMES:
        source = project / name
        target = project / f"{name}.stale-narration-{suffix}"
        source_sha, target_sha = _path_digest(source), _path_digest(target)
        if target_sha is not None and source_sha is not None and target_sha != source_sha:
            raise WorkflowError("narration_retirement_conflict", f"{target.name} already differs")
        if source_sha is not None:
            result.append({
                "source": name,
                "target": target.name,
                "sha256": source_sha,
                "target_preexisting": target_sha is not None,
            })
    return result


def _build_transaction(
    project: Path,
    acceptance_path: Path,
    acceptance: dict,
    receipt_path: Path,
    receipt: dict,
    derivation_request_sha: str,
    *,
    approved_at: str | None = None,
    retirements: list[dict] | None = None,
) -> dict:
    acceptance_sha = sha256(acceptance_path)
    receipt_sha = sha256(receipt_path)
    approved_at = approved_at or dt.datetime.now().astimezone().isoformat(timespec="seconds")
    stamp = {
        "schema": PRONUNCIATION_APPROVAL_SCHEMA,
        "status": "pass",
        "sha256": receipt["artifacts"]["audio"]["sha256"],
        "warnings": [],
        "accepted_issues": [
            {**item, "disposition": "accepted_by_human"}
            for item in acceptance.get("accepted_issues", [])
        ],
        "approved_by": acceptance["accepted_by"].strip(),
        "approved_at": approved_at,
        "derived_from": {
            "schema": DERIVATION_SCHEMA,
            "receipt_sha256": receipt_sha,
            "tempo": receipt["tempo"],
            "source_audio_sha256": receipt["source"]["audio"]["sha256"],
            "source_pronunciation_stamp_sha256": receipt["source"]["pronunciation_stamp"]["sha256"],
            "acceptance_sha256": acceptance_sha,
        },
    }
    stamp_sha = hashlib.sha256(_json_payload(stamp)).hexdigest()
    canonical = {
        "audio": {"path": CANONICAL_NAMES["audio"], "sha256": receipt["artifacts"]["audio"]["sha256"]},
        "srt": {"path": CANONICAL_NAMES["srt"], "sha256": receipt["artifacts"]["srt"]["sha256"]},
        "pronunciation_stamp": {"path": CANONICAL_NAMES["pronunciation_stamp"], "sha256": stamp_sha},
    }
    old_suffix = receipt["source"]["audio"]["sha256"][:12]
    archives = {
        key: {"path": f"{name}.superseded-{old_suffix}", "sha256": receipt["source"][key]["sha256"]}
        for key, name in CANONICAL_NAMES.items()
    }
    if retirements is None:
        retirements = _retirement_entries(project, canonical["srt"]["sha256"])
    archived_paths = [archives[key]["path"] for key in CANONICAL_NAMES]
    promotion = {
        "schema": PROMOTION_SCHEMA,
        "status": "complete",
        "project": project.name,
        "accepted_by": acceptance["accepted_by"].strip(),
        "promoted_at": approved_at,
        "tempo": receipt["tempo"],
        "audio_sha256": canonical["audio"]["sha256"],
        "srt_sha256": canonical["srt"]["sha256"],
        "request_sha256": acceptance_sha,
        "acceptance_request_sha256": acceptance_sha,
        "derivation_request_sha256": derivation_request_sha,
        "derivation_receipt_sha256": receipt_sha,
        "archived_superseded": archived_paths,
        "retired_timed_storyboard": [item["source"] for item in retirements],
        "artifacts": canonical,
    }
    return {
        "schema": TRANSACTION_SCHEMA,
        "status": "in_progress",
        "project": project.name,
        "acceptance": {"path": str(ACCEPTANCE_PATH), "sha256": acceptance_sha},
        "derivation_receipt": {
            "path": str(receipt_path.relative_to(project)),
            "sha256": receipt_sha,
        },
        "derivation_request_sha256": derivation_request_sha,
        "source": receipt["source"],
        "candidate": receipt["artifacts"],
        "canonical": canonical,
        "archives": archives,
        "stamp": stamp,
        "retirements": retirements,
        "promotion": promotion,
    }


def _validate_transaction_shape(project: Path, transaction: dict) -> None:
    if not (
        transaction.get("schema") == TRANSACTION_SCHEMA
        and transaction.get("status") == "in_progress"
        and transaction.get("project") == project.name
        and isinstance(transaction.get("acceptance"), dict)
        and transaction["acceptance"].get("path") == str(ACCEPTANCE_PATH)
        and isinstance(transaction["acceptance"].get("sha256"), str)
        and SHA256_RE.fullmatch(transaction["acceptance"]["sha256"])
        and isinstance(transaction.get("derivation_receipt"), dict)
        and isinstance(transaction.get("stamp"), dict)
        and isinstance(transaction.get("promotion"), dict)
        and isinstance(transaction.get("retirements"), list)
    ):
        raise WorkflowError("invalid_derivation_transaction", "promotion journal is malformed")
    _canonical_entries(transaction)
    suffix = transaction["canonical"]["srt"]["sha256"][:12]
    seen = set()
    for item in transaction["retirements"]:
        if not (
            isinstance(item, dict)
            and item.get("source") in RETIRE_NAMES
            and item.get("target") == f"{item['source']}.stale-narration-{suffix}"
            and isinstance(item.get("sha256"), str)
            and SHA256_RE.fullmatch(item["sha256"])
            and isinstance(item.get("target_preexisting"), bool)
            and item["source"] not in seen
        ):
            raise WorkflowError("invalid_derivation_transaction", "retirement path is not fixed")
        seen.add(item["source"])
    # An in-progress transaction may be before or after each retirement move,
    # so either the source or its fixed target can exist. In both states the
    # journal must name it; omission would let a forged journal skip invalidating
    # a storyboard that still carries the old cue clock.
    for name in RETIRE_NAMES:
        target = project / f"{name}.stale-narration-{suffix}"
        present = (
            _path_digest(project / name) is not None
            or _path_digest(target) is not None
        )
        if present and name not in seen:
            raise WorkflowError("invalid_derivation_transaction", "journal omits a timed storyboard")


def _validate_transaction_evidence(project: Path, transaction: dict) -> tuple[dict, dict]:
    _validate_transaction_shape(project, transaction)
    receipt_entry = transaction["derivation_receipt"]
    request_path, request, _tempo, request_sha = _tempo_request(project)
    expected_receipt_path = DERIVATIONS_ROOT / request_sha / "derivation.json"
    if receipt_entry.get("path") != str(expected_receipt_path):
        raise WorkflowError("invalid_derivation_transaction", "journal receipt path is not fixed")
    receipt_path = _contained_direct(project / expected_receipt_path, project, kind="file")
    receipt = read_json(receipt_path)
    if (
        sha256(receipt_path) != receipt_entry.get("sha256")
        or receipt.get("source") != transaction.get("source")
        or receipt.get("artifacts") != transaction.get("candidate")
        or receipt.get("request_sha256") != request_sha
        or not _request_matches_source(request, receipt.get("source", {}))
    ):
        raise WorkflowError("transaction_evidence_changed", "derivation receipt changed during promotion")
    try:
        acceptance_path, acceptance, loaded_receipt_path, loaded_receipt, loaded_request_sha = (
            _load_derivation_for_acceptance(project, require_current_source=False)
        )
    except WorkflowError as exc:
        raise WorkflowError("transaction_evidence_changed", str(exc)) from exc
    if (
        sha256(acceptance_path) != transaction["acceptance"]["sha256"]
        or loaded_receipt_path != receipt_path
        or loaded_receipt != receipt
        or loaded_request_sha != transaction.get("derivation_request_sha256")
    ):
        raise WorkflowError("transaction_evidence_changed", "acceptance changed during promotion")
    rebuilt = _build_transaction(
        project,
        acceptance_path,
        acceptance,
        receipt_path,
        receipt,
        loaded_request_sha,
        approved_at=transaction["stamp"].get("approved_at"),
        retirements=transaction["retirements"],
    )
    if rebuilt != transaction:
        raise WorkflowError("invalid_derivation_transaction", "journal intent cannot be reconstructed")
    return acceptance, receipt


def _rollback_transaction(project: Path, transaction: dict) -> None:
    """Restore only known installed/missing leaves; preserve unknown user edits."""
    _validate_transaction_shape(project, transaction)
    entries = _canonical_entries(transaction)
    restorable = True
    for _key, old, new, archive in entries:
        canonical = project / old["path"]
        current = _path_digest(canonical)
        if current is None or current == new["sha256"]:
            archive_path = project / archive["path"]
            if _path_digest(archive_path) != old["sha256"]:
                restorable = False
                continue
            replace_file_atomically(canonical, archive_path.read_bytes())
    for item in reversed(transaction["retirements"]):
        source, target = project / item["source"], project / item["target"]
        source_sha, target_sha = _path_digest(source), _path_digest(target)
        if source_sha is None and target_sha == item["sha256"]:
            if item["target_preexisting"]:
                replace_file_atomically(source, target.read_bytes())
            else:
                os.replace(target, source)
        elif source_sha not in {None, item["sha256"]}:
            restorable = False
    promotion_path = project / PROMOTION_PATH
    intended_promotion_sha = hashlib.sha256(
        _json_payload(transaction["promotion"])
    ).hexdigest()
    if _path_digest(promotion_path) == intended_promotion_sha:
        promotion_path.unlink()
    journal = project / TRANSACTION_PATH
    if restorable and journal.is_file() and not journal.is_symlink():
        journal.unlink()


def _assert_known_canonical(project: Path, transaction: dict) -> None:
    unknown = []
    for key, old, new, _archive in _canonical_entries(transaction):
        current = _path_digest(project / old["path"])
        if current not in {None, old["sha256"], new["sha256"]}:
            unknown.append(key)
    if unknown:
        _rollback_transaction(project, transaction)
        raise WorkflowError(
            "derivation_source_changed",
            f"canonical narration changed during promotion: {', '.join(unknown)}",
        )


def _execute_transaction(project: Path, transaction: dict) -> dict:
    # Until this succeeds, every digest in the journal is attacker-controlled.
    # Never use an evidence-invalid journal as rollback authority.
    _validate_transaction_evidence(project, transaction)
    _assert_known_canonical(project, transaction)

    for key, old, _new, archive in _canonical_entries(transaction):
        _validate_transaction_evidence(project, transaction)
        _assert_known_canonical(project, transaction)
        _copy_if_missing_or_equal(
            project / old["path"], project / archive["path"], old["sha256"]
        )
        _failure_point(f"archive-{key}")

    stamp_payload = _json_payload(transaction["stamp"])
    payloads = {
        "audio": (project / transaction["candidate"]["audio"]["path"]).read_bytes(),
        "srt": (project / transaction["candidate"]["srt"]["path"]).read_bytes(),
        "pronunciation_stamp": stamp_payload,
    }
    for key, old, new, _archive in _canonical_entries(transaction):
        _validate_transaction_evidence(project, transaction)
        _assert_known_canonical(project, transaction)
        payload = payloads[key]
        if hashlib.sha256(payload).hexdigest() != new["sha256"]:
            _rollback_transaction(project, transaction)
            raise WorkflowError("invalid_derivation_transaction", "intended canonical digest is invalid")
        target = project / new["path"]
        if _path_digest(target) != new["sha256"]:
            replace_file_atomically(target, payload)
        _failure_point(f"install-{key}")

    for item in transaction["retirements"]:
        _validate_transaction_evidence(project, transaction)
        _assert_known_canonical(project, transaction)
        source, target = project / item["source"], project / item["target"]
        source_sha, target_sha = _path_digest(source), _path_digest(target)
        if target_sha is not None and target_sha != item["sha256"]:
            _rollback_transaction(project, transaction)
            raise WorkflowError("narration_retirement_conflict", f"{target.name} changed")
        if source_sha is not None:
            if source_sha != item["sha256"]:
                _rollback_transaction(project, transaction)
                raise WorkflowError("narration_retirement_conflict", f"{source.name} changed")
            os.replace(source, target)
        _failure_point(f"retire-{item['source']}")

    _validate_transaction_evidence(project, transaction)
    _assert_known_canonical(project, transaction)
    promotion_path = project / PROMOTION_PATH
    if promotion_path.is_symlink():
        _rollback_transaction(project, transaction)
        raise WorkflowError("invalid_path", "promotion receipt cannot be a symlink")
    write_json_atomically(promotion_path, transaction["promotion"])
    _failure_point("promotion-receipt")
    _validate_transaction_evidence(project, transaction)
    journal = project / TRANSACTION_PATH
    if journal.is_symlink() or not journal.is_file():
        raise WorkflowError("invalid_derivation_transaction", "promotion journal disappeared")
    journal.unlink()
    return transaction["promotion"]


def promote(project_path: Path) -> dict:
    """Promote exactly the human-accepted derivation to canonical narration."""
    project = direct_directory(project_path)
    with _promotion_lock(project):
        journal_path = project / TRANSACTION_PATH
        if journal_path.is_symlink():
            raise WorkflowError("invalid_path", "promotion journal cannot be a symlink")
        if journal_path.is_file():
            return _execute_transaction(project, read_json(journal_path))

        acceptance_path, acceptance, receipt_path, receipt, derivation_request_sha = (
            _load_derivation_for_acceptance(project)
        )
        acceptance_sha = sha256(acceptance_path)
        receipt_sha = sha256(receipt_path)
        promotion_path = project / PROMOTION_PATH
        if promotion_path.is_symlink():
            raise WorkflowError("invalid_path", "promotion receipt cannot be a symlink")
        if promotion_path.is_file():
            existing = read_json(promotion_path)
            if _canonical_promotion_current(
                project,
                existing,
                acceptance_sha,
                receipt_sha,
                receipt["artifacts"]["audio"]["sha256"],
            ):
                return existing

        current_source, _stamp = _canonical_inputs(project)
        if current_source != receipt["source"]:
            raise WorkflowError("derivation_receipt_stale", "canonical narration changed")
        transaction = _build_transaction(
            project,
            acceptance_path,
            acceptance,
            receipt_path,
            receipt,
            derivation_request_sha,
        )
        _validate_transaction_evidence(project, transaction)
        _assert_known_canonical(project, transaction)
        write_json_atomically(journal_path, transaction)
        _failure_point("journal")
        return _execute_transaction(project, transaction)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("prepare", "promote"):
        command = subparsers.add_parser(action)
        command.add_argument("project", type=Path)
    args = parser.parse_args(argv)
    try:
        value = prepare(args.project) if args.action == "prepare" else promote(args.project)
    except (WorkflowError, OSError, UnicodeDecodeError) as exc:
        code = exc.code if isinstance(exc, WorkflowError) else "narration_derivation_failed"
        print(json.dumps({"schema_version": 1, "outcome": "error", "code": code, "data": None}))
        return 2
    print(json.dumps(value, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
