#!/usr/bin/env python3
"""MCP-owned pronunciation analysis, probe, and human-review workflow."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


PLAN_SCHEMA = "haru.pronunciation_plan.v1"
OVERRIDES_SCHEMA = "haru.pronunciation_overrides.v1"
ANALYSIS_SCHEMA = "haru.pronunciation_analysis.v1"
PROBE_REQUEST_SCHEMA = "haru.pronunciation_probe_request.v1"
PROBE_SCHEMA = "haru.pronunciation_probe.v1"
CONFIRMATION_SCHEMA = "haru.pronunciation_confirmation.v1"
REVIEW_SCHEMA = "haru.pronunciation_review.v1"
NARRATION_REQUEST_SCHEMA = "haru.narration_generation_request.v1"
NARRATION_SCHEMA = "haru.narration_generation.v4"
PROMOTION_REQUEST_SCHEMA = "haru.narration_promotion_request.v1"
PROMOTION_SCHEMA = "haru.narration_promotion.v1"
PRONUNCIATION_APPROVAL_SCHEMA = "haru.pronunciation_approval.v1"

# A genuinely spoken cue can have zero duration in isolation -- a lone
# trailing punctuation cue, measured on a human-approved shipped take
# (projects/ai-cyber-eval-escape-2026, 1 zero-duration cue in 211). A
# truncated take instead collapses a whole CLUSTER of cues onto one shared
# timestamp (measured on a rejected single-request take,
# projects/prepay-card-shop-fraud-taiwan-2026/audio/
# narration-final-1x-single-take-candidate.srt: 240/406 cues share exactly
# one end timestamp, 570.321s). A ratio alone would let a truncation that
# drops only the last few percent of a long script slip under it, so the
# gate keys on cluster size instead: it passes the isolated artifact and
# catches a truncation dropping as few as this many cues.
NARRATION_ALIGNMENT_ZERO_DURATION_EPSILON_SECONDS = 0.02
NARRATION_ALIGNMENT_ZERO_DURATION_CLUSTER_MAX_CUES = 3
# Tolerance between the SRT's last cue end and the ffprobe-measured audio
# duration; beyond this, the transcript claims text the audio never plays.
# This alone does not catch a truncation cluster: a provider that truncates
# generation and re-times the audio to match can make the last cue line up
# with the (shorter) audio exactly -- the cluster check above is what catches
# that case.
NARRATION_ALIGNMENT_END_TOLERANCE_SECONDS = 0.5

# Accepted 1x-audio pace band, in non-whitespace script characters per second
# of narration, as a backstop behind the alignment-integrity gate above.
# Canonical sectioned generation measured 3.78 chars/sec. The rejected
# single-request take was
# not actually spoken 2x fast: it silently truncated generation at ~43% of
# the script while its alignment claimed full coverage (see the gate above);
# dividing the full script's char count by that half-length audio duration
# is what produced the apparent 8.11 chars/sec.
NARRATION_PACE_MIN_CHARS_PER_SECOND = 2.8
NARRATION_PACE_MAX_CHARS_PER_SECOND = 5.5
NARRATION_PACE_BASIS = "non_whitespace_characters.v1"

# Report-only threshold for inter-section loudness drift (each section is an
# independent TTS request, so level can drift across a seam). This is a first
# estimate, not a validated band like the pace gate above -- it has no
# human-accepted-vs-rejected baseline yet. Record the spread and tune this
# number against a take the human accepts before ever promoting it to a hard
# WorkflowError gate.
NARRATION_SEAM_SPREAD_WARN_DB = 3.0
# ffmpeg reports "mean_volume: -inf dB" for a genuinely silent section (a real
# defect worth surfacing, not a parser failure). -inf is not valid JSON, so it
# is recorded as this large-but-finite sentinel instead of being dropped or
# raised as a parse error.
NARRATION_SILENT_SECTION_DB = -120.0

# Pinned to match generate_sectioned_narration.py's own --target-chars/--max-chars
# defaults; passed explicitly so the receipt's recorded values are always accurate
# even if the script's defaults change later. A staged request may override both
# (section_target_chars / section_max_chars): every seam is a prosody restart, so
# fewer, longer sections is the lever for seams that read as restarts.
NARRATION_SECTION_TARGET_CHARS = 300
NARRATION_SECTION_MAX_CHARS = 520
# eleven_v3 advertises maximum_text_length_per_request=5000, but a measured 4513-char
# request silently truncated after voicing 1754 chars while still returning alignment
# claiming full coverage. The documented limit is not a reliability guarantee, so the
# override ceiling sits well under the one length observed to fail.
NARRATION_SECTION_CHARS_CEILING = 1500
NARRATION_SECTION_CHARS_FLOOR = 100

# The SRT's timeline is re-derived from speech-to-text on the produced audio,
# because eleven_v3's own character alignment drifts inside long requests (on a
# 375.92s section the cue at t=291s pointed two cues behind what was being
# said). This is the fraction of a section's characters that must anchor to the
# transcript before that re-derived timeline is trustworthy. It mirrors the
# child's own floor in narration/stt_align.py: the gate is restated here rather
# than assumed, because a receipt must not depend on the child having checked.
NARRATION_SRT_MIN_ANCHOR_COVERAGE = 0.80


class WorkflowError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowError("invalid_pronunciation_artifact", str(exc)) from exc
    if not isinstance(value, dict):
        raise WorkflowError("invalid_pronunciation_artifact", f"{path.name} must be an object")
    return value


def write_json_atomically(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def replace_file_atomically(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def direct_directory(path: Path) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise WorkflowError("invalid_path", "expected a direct directory")
    return path.resolve(strict=True)


def direct_file(path: Path) -> Path:
    if path.is_symlink() or not path.is_file():
        raise WorkflowError("invalid_path", "expected a direct file")
    return path.resolve(strict=True)


def executable_from_env(name: str, fallback: str) -> Path:
    aliases = {
        "HARU_TTS_PYTHON": "VIDEO_STUDIO_TTS_PYTHON",
        "HARU_G2PW_PYTHON": "VIDEO_STUDIO_G2PW_PYTHON",
        "HARU_FFMPEG": "VIDEO_STUDIO_FFMPEG",
        "HARU_FFPROBE": "VIDEO_STUDIO_FFPROBE",
    }
    configured = os.environ.get(aliases.get(name, name)) or os.environ.get(name)
    candidate = (
        Path(configured).expanduser()
        if configured
        else Path(
            shutil.which(
                fallback,
                path=os.pathsep.join(
                    [*os.get_exec_path(), "/opt/homebrew/bin", "/usr/local/bin"]
                ),
            )
            or fallback
        )
    )
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise WorkflowError("runtime_unavailable", f"{name} is unavailable") from exc
    if not resolved.is_file():
        raise WorkflowError("runtime_unavailable", f"{name} is unavailable")
    return candidate.absolute()


def pronunciation_failure_code(output: str) -> str:
    if "BUDGET STOP" in output:
        return "pronunciation_probe_budget_exceeded"
    if "key not found" in output or "key file empty" in output:
        return "pronunciation_probe_credentials_unavailable"
    if match := re.search(r"ERROR HTTP (\d{3})", output):
        return f"pronunciation_provider_http_{match.group(1)}"
    if "timed out" in output.lower() or "timeout" in output.lower():
        return "pronunciation_provider_timeout"
    return "pronunciation_probe_failed"


def narration_file(project: Path) -> Path:
    candidates = [project / "narration.txt", project / "script/narration.txt"]
    found = [path for path in candidates if path.is_file() and not path.is_symlink()]
    if len(found) != 1:
        raise WorkflowError("narration_unavailable", "expected exactly one canonical narration")
    return found[0].resolve(strict=True)


def ensure_state(project: Path) -> tuple[Path, Path]:
    state = project / ".hvp"
    staging = state / "staging"
    for path in (state, staging):
        if path.is_symlink():
            raise WorkflowError("invalid_path", "state directories cannot be symlinks")
        path.mkdir(exist_ok=True)
    return direct_directory(state), direct_directory(staging)


def current_plan(project: Path) -> tuple[Path, dict, str, str]:
    source = narration_file(project)
    source_sha = sha256(source)
    plan_path = direct_file(project / "pronunciation-plan.json")
    plan = read_json(plan_path)
    plan_sha = sha256(plan_path)
    analysis = read_json(direct_file(project / ".hvp/pronunciation-analysis.json"))
    if not (
        plan.get("schema") == PLAN_SCHEMA
        and plan.get("source", {}).get("sha256") == source_sha
        and analysis.get("schema") == ANALYSIS_SCHEMA
        and analysis.get("status") == "complete"
        and analysis.get("source_sha256") == source_sha
        and analysis.get("output_sha256") == plan_sha
    ):
        raise WorkflowError("pronunciation_plan_stale", "pronunciation plan is stale")
    return plan_path, plan, plan_sha, source_sha


def valid_probe_receipt(
    project: Path, receipt: dict, source_sha: str, plan_sha: str, request_sha: str
) -> bool:
    if not (
        receipt.get("schema") == PROBE_SCHEMA
        and receipt.get("status") == "complete"
        and receipt.get("source_sha256") == source_sha
        and receipt.get("plan_sha256") == plan_sha
        and receipt.get("request_sha256") == request_sha
        and isinstance(receipt.get("confirmation"), dict)
        and isinstance(receipt.get("audio"), list)
        and receipt["audio"]
    ):
        return False
    artifacts = [receipt["confirmation"], *receipt["audio"]]
    if receipt.get("mode") == "ab":
        if not isinstance(receipt.get("g2p_validation"), dict):
            return False
        artifacts.append(receipt["g2p_validation"])
    for item in artifacts:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            return False
        path = project / item["path"]
        try:
            path = direct_file(path)
            path.relative_to(project)
        except (WorkflowError, ValueError):
            return False
        if item.get("sha256") != sha256(path):
            return False
    return True


def valid_effective_pronunciation_rules(value: object) -> bool:
    return (
        isinstance(value, dict)
        and value.get("reported_by") == "child_take_report"
        and isinstance(value.get("fixes"), dict)
        and bool(value["fixes"])
    )


def valid_narration_receipt(
    project: Path, receipt: dict, source_sha: str, probe_sha: str,
    review_sha: str, request_sha: str,
) -> bool:
    credits = receipt.get("credits_spent")
    max_credits = receipt.get("max_credits")
    artifacts = receipt.get("artifacts")
    if not (
        receipt.get("schema") == NARRATION_SCHEMA
        and receipt.get("project") == project.name
        and receipt.get("status") == "complete"
        and receipt.get("source_sha256") == source_sha
        and receipt.get("probe_sha256") == probe_sha
        and receipt.get("review_sha256") == review_sha
        and receipt.get("request_sha256") == request_sha
        and isinstance(credits, int)
        and isinstance(max_credits, int)
        and credits <= max_credits
        and isinstance(artifacts, dict)
    ):
        return False
    alignment_check = receipt.get("alignment_check")
    if not (
        receipt.get("pronunciation_mechanism") == "inline_respelling"
        and receipt.get("pronunciation_mechanism_source") in {"from_receipt", "derived_from_take"}
        and isinstance(receipt.get("narration_pace_chars_per_second"), (int, float))
        and receipt.get("narration_pace_basis") == NARRATION_PACE_BASIS
        and isinstance(receipt.get("narration_duration_seconds"), (int, float))
        and isinstance(receipt.get("section_count"), int)
        and isinstance(receipt.get("sections"), list)
        # A receipt whose alignment gate never ran, or ran and failed, must
        # never be treated as reusable -- that check is this whole path's
        # premise, not an optional decoration on the receipt.
        and isinstance(alignment_check, dict)
        and alignment_check.get("status") == "pass"
        # A cached receipt must not be reusable unless its SRT timeline was
        # re-derived from the audio. A receipt written before that gate existed
        # describes a take timed by the provider's drifting alignment.
        and isinstance(receipt.get("srt_alignment"), dict)
        and receipt["srt_alignment"].get("source") == "stt_forced"
        and isinstance(receipt.get("seam_check"), dict)
        and isinstance(receipt.get("approved_pronunciation_terms"), list)
        and valid_effective_pronunciation_rules(receipt.get("effective_pronunciation_rules"))
    ):
        return False
    for item in artifacts.values():
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            return False
        path = project / item["path"]
        try:
            path = direct_file(path)
            path.relative_to(project)
        except (WorkflowError, ValueError):
            return False
        if item.get("sha256") != sha256(path):
            return False
    return bool(receipt.get("artifacts"))


def run_planner(
    tools: Path,
    source: Path,
    output: Path,
    overrides: Path | None = None,
) -> dict:
    planner = direct_file(tools / "narration/g2p_plan.py")
    runtime = Path.home() / ".cache/video-studio/g2pw"
    os.environ.setdefault("HARU_G2PW_PYTHON", str(runtime / ".venv/bin/python"))
    python = executable_from_env("HARU_G2PW_PYTHON", "python3")
    arguments = [
        str(python),
        str(planner),
        "--text-file",
        str(source),
        "--out",
        str(output),
    ]
    if overrides is not None:
        arguments.extend(["--overrides", str(direct_file(overrides))])
    environment = os.environ.copy()
    environment.setdefault("HARU_G2PW_MODEL_DIR", str(runtime / "G2PWModel"))
    environment.setdefault("HARU_G2PW_BERT_MODEL", str(runtime / "bert-base-chinese"))
    result = subprocess.run(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
        check=False,
    )
    if result.returncode != 0:
        raise WorkflowError("g2p_analysis_failed", "g2pW planner failed")
    plan = read_json(direct_file(output))
    source_sha = sha256(source)
    if plan.get("schema") != PLAN_SCHEMA or plan.get("source", {}).get("sha256") != source_sha:
        raise WorkflowError("g2p_analysis_failed", "planner returned an unbound plan")
    return plan


def analyze(project_path: Path, tools_path: Path) -> dict:
    project = direct_directory(project_path)
    tools = direct_directory(tools_path)
    source = narration_file(project)
    state, staging = ensure_state(project)
    staged = staging / "pronunciation-plan.json"
    overrides = project / "pronunciation-overrides.json"
    run_planner(
        tools,
        source,
        staged,
        direct_file(overrides) if overrides.exists() else None,
    )
    source_sha = sha256(source)
    output = project / "pronunciation-plan.json"
    os.replace(staged, output)
    receipt = {
        "schema": ANALYSIS_SCHEMA,
        "project": project.name,
        "status": "complete",
        "source": str(source.relative_to(project)),
        "source_sha256": source_sha,
        "output": "pronunciation-plan.json",
        "output_sha256": sha256(output),
    }
    write_json_atomically(state / "pronunciation-analysis.json", receipt)
    return receipt


def confirm(project_path: Path, tools_path: Path) -> dict:
    project = direct_directory(project_path)
    tools = direct_directory(tools_path)
    state, staging = ensure_state(project)
    _, _, plan_sha, source_sha = current_plan(project)
    source = narration_file(project)
    request_path = direct_file(staging / "pronunciation-probe-request.json")
    request = read_json(request_path)
    mode = request.get("mode", "classify")
    variants = request.get("variants")
    if mode == "ab" and isinstance(variants, list):
        terms = [item.get("term") for item in variants if isinstance(item, dict)]
    else:
        terms = request.get("terms")
    passes = request.get("passes")
    max_credits = request.get("max_credits")
    approved_by = request.get("spending_approved_by")
    if not (
        request.get("schema") == PROBE_REQUEST_SCHEMA
        and mode in {"classify", "ab"}
        and isinstance(terms, list)
        and 1 <= len(terms) <= 20
        and len(terms) == len(set(terms))
        and all(isinstance(term, str) and 1 <= len(term) <= 32 for term in terms)
        and isinstance(passes, int)
        and 1 <= passes <= 3
        and request.get("model") == "eleven_v3"
        and isinstance(max_credits, int)
        and 1 <= max_credits <= 5000
        and isinstance(approved_by, str)
        and bool(approved_by.strip())
        and (
            mode == "classify"
            or (
                passes == 1
                and isinstance(request.get("voice"), str)
                and 1 <= len(request["voice"]) <= 128
                and isinstance(variants, list)
                and len(variants) == len(terms)
                and all(
                    isinstance(item, dict)
                    and item.get("term") in terms
                    and isinstance(item.get("spoken"), str)
                    and len(item["spoken"]) == len(item["term"])
                    and "=" not in item["term"]
                    and "=" not in item["spoken"]
                    for item in variants
                )
            )
        )
    ):
        raise WorkflowError("invalid_probe_request", "invalid pronunciation probe request")
    estimated_credits = passes * sum(len(term) + 32 for term in terms)
    if mode == "ab":
        estimated_credits *= 2
    if estimated_credits > max_credits:
        raise WorkflowError("probe_budget_exceeded", "probe estimate exceeds approved credits")

    request_sha = sha256(request_path)
    existing_path = state / "pronunciation-probes.json"
    if existing_path.is_file() and not existing_path.is_symlink():
        existing = read_json(existing_path)
        if valid_probe_receipt(project, existing, source_sha, plan_sha, request_sha):
            return existing

    job_digest = hashlib.sha256(f"{plan_sha}:{request_sha}".encode()).hexdigest()
    probes_root = state / "pronunciation-probes"
    if probes_root.is_symlink():
        raise WorkflowError("invalid_path", "probe output cannot be a symlink")
    probes_root.mkdir(exist_ok=True)
    direct_directory(probes_root)
    out = probes_root / job_digest[:16]
    if out.is_symlink():
        raise WorkflowError("invalid_path", "probe output cannot be a symlink")
    out.mkdir(parents=True, exist_ok=True)
    g2p_validation = None
    if mode == "ab":
        overrides_path = out / "g2p-overrides.json"
        write_json_atomically(
            overrides_path,
            {"schema": OVERRIDES_SCHEMA, "terms": variants},
        )
        validation_path = out / "g2p-validation.json"
        validation = run_planner(
            tools,
            source,
            validation_path,
            overrides_path,
        )
        review_items = [
            item
            for item in validation.get("review_items", [])
            if isinstance(item, dict) and item.get("reason") == "project_override"
        ]
        for variant in variants:
            matches = [
                item
                for item in review_items
                if item.get("term") == variant["term"]
                and item.get("spoken") == variant["spoken"]
            ]
            if not matches or not all(item.get("g2p_match") is True for item in matches):
                raise WorkflowError(
                    "g2p_variant_mismatch",
                    "A/B variant does not preserve the contextual G2P reading",
                )
        g2p_validation = {
            "path": str(validation_path.relative_to(project)),
            "sha256": sha256(validation_path),
        }
    confirmer = direct_file(tools / "narration/confirm_pronunciation.py")
    python = executable_from_env("HARU_TTS_PYTHON", "python3.11")
    arguments = [str(python), str(confirmer)]
    for term in terms:
        arguments.extend(["--term", term])
    arguments.extend(["--out-dir", str(out), "--model", "eleven_v3"])
    if mode == "ab":
        for variant in variants:
            arguments.extend(["--candidate", f"{variant['term']}={variant['spoken']}"])
        arguments.extend(["--ab", "--force-budget", "--voice", request["voice"]])
    else:
        arguments.extend(["--passes", str(passes), "--no-fix"])
    result = subprocess.run(
        arguments,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise WorkflowError(
            pronunciation_failure_code(f"{result.stdout}\n{result.stderr}"),
            "pronunciation confirmer failed",
        )
    confirmation_path = direct_file(out / "confirmation.json")
    confirmation = read_json(confirmation_path)
    credits_spent = confirmation.get("credits_spent")
    audio_paths = (
        [out / "probe-a-original.mp3", out / "probe-b-g2p.mp3"]
        if mode == "ab"
        else sorted(out.glob("probe-terms-*.mp3"))
    )
    valid_confirmation = (
        confirmation.get("schema") == CONFIRMATION_SCHEMA
        and confirmation.get("terms") == terms
        and isinstance(credits_spent, int)
        and 0 <= credits_spent <= max_credits
        and len(audio_paths) == (2 if mode == "ab" else passes)
        and all(path.is_file() and not path.is_symlink() for path in audio_paths)
    )
    if mode == "ab":
        valid_confirmation = bool(
            valid_confirmation
            and confirmation.get("mode") == "ab"
            and confirmation.get("voice") == request["voice"]
            and confirmation.get("model") == "eleven_v3"
            and confirmation.get("fixes")
            == {item["term"]: item["spoken"] for item in variants}
        )
    if not valid_confirmation:
        raise WorkflowError("pronunciation_probe_failed", "invalid probe result")
    receipt = {
        "schema": PROBE_SCHEMA,
        "project": project.name,
        "status": "complete",
        "mode": mode,
        "source_sha256": source_sha,
        "plan_sha256": plan_sha,
        "request_sha256": request_sha,
        "spending_approved_by": approved_by.strip(),
        "estimated_credits": estimated_credits,
        "max_credits": max_credits,
        "credits_spent": credits_spent,
        **(
            {
                "voice": request["voice"],
                "variants": variants,
                # confirm_pronunciation.py's --ab path always bakes the
                # respelling into the literal probe text it sends to TTS; it
                # has no pronunciation-dictionary option at all.
                "mechanism": "inline_respelling",
            }
            if mode == "ab"
            else {}
        ),
        **({"g2p_validation": g2p_validation} if g2p_validation else {}),
        "confirmation": {
            "path": str(confirmation_path.relative_to(project)),
            "sha256": sha256(confirmation_path),
        },
        "audio": (
            [
                {
                    "label": label,
                    "path": str(path.relative_to(project)),
                    "sha256": sha256(path),
                }
                for label, path in zip(("a", "b"), audio_paths)
            ]
            if mode == "ab"
            else [
                {"path": str(path.relative_to(project)), "sha256": sha256(path)}
                for path in audio_paths
            ]
        ),
    }
    write_json_atomically(existing_path, receipt)
    return receipt


def current_probe(project: Path) -> tuple[dict, str, str, str]:
    _, _, plan_sha, source_sha = current_plan(project)
    request_path = direct_file(project / ".hvp/staging/pronunciation-probe-request.json")
    request_sha = sha256(request_path)
    receipt_path = direct_file(project / ".hvp/pronunciation-probes.json")
    receipt = read_json(receipt_path)
    if not valid_probe_receipt(project, receipt, source_sha, plan_sha, request_sha):
        raise WorkflowError("pronunciation_probe_stale", "pronunciation probe is stale")
    return receipt, sha256(receipt_path), plan_sha, source_sha


def review(project_path: Path, reviewed_by: str, verdict: str, notes: str) -> dict:
    project = direct_directory(project_path)
    if not reviewed_by.strip() or verdict not in {"pass", "fail"} or not notes.strip():
        raise WorkflowError("invalid_pronunciation_review", "review fields are invalid")
    _, probe_sha, plan_sha, source_sha = current_probe(project)
    receipt = {
        "schema": REVIEW_SCHEMA,
        "project": project.name,
        "verdict": verdict,
        "reviewed_by": reviewed_by.strip(),
        "reviewed_at": dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "notes": notes.strip(),
        "source_sha256": source_sha,
        "plan_sha256": plan_sha,
        "probe_sha256": probe_sha,
    }
    write_json_atomically(project / ".hvp/pronunciation-review.json", receipt)
    return receipt


def _srt_seconds(value: str) -> float:
    hours, minutes, rest = value.split(":")
    seconds, millis = rest.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000


def _srt_time(value: float) -> str:
    millis = max(0, round(value * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    seconds, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def retime_srt(source: Path, output: Path, speed: float) -> None:
    lines = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if " --> " in line:
            start, end = line.split(" --> ", 1)
            line = f"{_srt_time(_srt_seconds(start) / speed)} --> {_srt_time(_srt_seconds(end) / speed)}"
        lines.append(line)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _srt_cues(path: Path) -> list[tuple[float, float, str]]:
    cues = []
    start = end = None
    text_lines: list[str] = []
    for line in [*path.read_text(encoding="utf-8").splitlines(), ""]:
        if " --> " in line:
            if start is not None:
                cues.append((start, end, "\n".join(text_lines).strip()))
            raw_start, raw_end = line.split(" --> ", 1)
            start, end = _srt_seconds(raw_start.strip()), _srt_seconds(raw_end.strip())
            text_lines = []
        elif line.strip() and start is not None:
            text_lines.append(line)
    if start is not None:
        cues.append((start, end, "\n".join(text_lines).strip()))
    return cues


def _largest_zero_duration_cluster(
    cues: list[tuple[float, float, str]],
) -> tuple[int, list[str]]:
    """Largest group of zero-duration cues sharing one end timestamp.

    A truncated take collapses many alignment entries onto the cutoff point;
    an isolated punctuation-only cue does not. Grouping by shared timestamp is
    what tells them apart -- a raw zero-duration count cannot.
    """
    groups: dict[float, list[str]] = {}
    for start, end, text in cues:
        if end - start <= NARRATION_ALIGNMENT_ZERO_DURATION_EPSILON_SECONDS:
            groups.setdefault(end, []).append(text)
    if not groups:
        return 0, []
    texts = max(groups.values(), key=len)
    return len(texts), texts


def ffprobe_duration_seconds(ffprobe: Path, path: Path) -> float:
    """Measure a media file's true duration directly from its bytes via a
    lightweight format-only probe (no decode), rather than trusting any
    self-reported metadata."""
    result = subprocess.run(
        [str(ffprobe), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        tail = (result.stderr or result.stdout or "").strip()[-500:]
        raise WorkflowError(
            "narration_generation_failed",
            f"ffprobe duration unparseable for {path.name}: {tail}",
        ) from None


def ffmpeg_mean_volume_db(ffmpeg: Path, path: Path) -> float:
    """Decode a section and measure its mean loudness. Reserved for the
    report-only seam check -- the merged file only needs the cheap duration
    probe above."""
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
    )
    match = re.search(r"mean_volume:\s*(-inf|-?[\d.]+) dB", result.stderr)
    if not match:
        tail = (result.stderr or "").strip()[-500:]
        raise WorkflowError(
            "narration_generation_failed",
            f"ffmpeg volumedetect unparseable for {path.name}: {tail}",
        )
    value = match.group(1)
    return NARRATION_SILENT_SECTION_DB if value == "-inf" else float(value)


def validated_srt_alignment(take: dict, section_count: int) -> dict:
    """The take's SRT-alignment report, or refuse to write a receipt.

    eleven_v3's own character alignment was measured to drift inside long
    requests -- on a 375.92s section, the cue at t=291s pointed two cues behind
    what was being said, while every word was present and correct. The SRT is a
    shipped artifact AND pins the cue-driven visual cuts, so a take whose
    timeline came from the provider cannot be accepted here.

    The child re-derives the timeline from speech-to-text on the audio it just
    produced and reports how it went. A checkout too old to report that cannot
    generate narration through this runner: unprovable is the right outcome,
    not "probably fine".
    """
    report = take.get("srt_alignment")
    if not isinstance(report, dict) or report.get("source") != "stt_forced":
        raise WorkflowError(
            "narration_generation_failed",
            "narration take does not report an STT-derived SRT timeline "
            "(srt_alignment.source != 'stt_forced'); the provider's own "
            "alignment drifts and cannot place a subtitle or a cue-pinned cut",
        )
    sections = report.get("sections")
    measured = report.get("sections_measured")
    coverage = report.get("min_anchor_coverage")
    delta = report.get("provider_delta_max_seconds")
    if not (
        # `type(...) is int` rather than isinstance: bool is an int subclass, so
        # {"sections": true} would otherwise satisfy > 0 and == 1 and land in a
        # durable receipt verbatim.
        type(sections) is int
        and type(measured) is int
        # Tied to the take's own section list, not just to each other: a child
        # that measured 1 of 12 and reported "1 of 1" would otherwise produce a
        # receipt claiming a fully-measured timeline for a 12-section take.
        and sections == section_count
        and measured == sections
        and isinstance(coverage, (int, float))
        and not isinstance(coverage, bool)
        # Bounded, not just numeric. This also rejects NaN and infinity, which
        # matters because NaN passes every comparison it is given -- "measured
        # and fine" is exactly what an unguarded NaN would look like against the
        # threshold below, and it is not valid JSON for the Rust reader either.
        and 0.0 <= coverage <= 1.0
        # The child reports null when the provider's timeline could not be
        # compared. Absence of evidence must not arrive as a confident 0.0, and
        # the receipt claims this discipline, so the receipt enforces it.
        and (delta is None or (
            isinstance(delta, (int, float))
            and not isinstance(delta, bool)
            and math.isfinite(delta)
            and delta >= 0.0
        ))
    ):
        # min_anchor_coverage is null when any section went unmeasured. Comparing
        # that against a threshold would raise, so it is rejected by shape first
        # -- "not measured" must never read as "measured and fine".
        raise WorkflowError(
            "narration_generation_failed",
            "narration take does not report a well-formed SRT alignment for "
            "every section",
        )
    if coverage < NARRATION_SRT_MIN_ANCHOR_COVERAGE:
        raise WorkflowError(
            "narration_srt_alignment_unreliable",
            f"only {coverage:.1%} of one section's characters anchored to the "
            f"transcript (minimum {NARRATION_SRT_MIN_ANCHOR_COVERAGE:.0%}); the "
            f"re-derived timeline is not trustworthy and the provider's is the "
            f"thing measured to drift",
        )
    # The receipt states the number it was judged against, rather than pointing
    # at a constant that may have moved since.
    return {**report, "min_anchor_coverage_required": NARRATION_SRT_MIN_ANCHOR_COVERAGE}


def check_alignment_integrity(label: str, srt_path: Path, audio_duration_seconds: float) -> dict:
    cues = _srt_cues(srt_path)
    zero_duration = sum(
        1 for start, end, _ in cues
        if end - start <= NARRATION_ALIGNMENT_ZERO_DURATION_EPSILON_SECONDS
    )
    cluster_size, cluster_texts = _largest_zero_duration_cluster(cues)
    last_cue_end = max((end for _, end, _ in cues), default=0.0)
    coverage_ok = (
        abs(last_cue_end - audio_duration_seconds) <= NARRATION_ALIGNMENT_END_TOLERANCE_SECONDS
    )
    truncated = cluster_size >= NARRATION_ALIGNMENT_ZERO_DURATION_CLUSTER_MAX_CUES
    ok = coverage_ok and not truncated
    return {
        "label": label,
        "status": "pass" if ok else "fail",
        "cue_count": len(cues),
        "zero_duration_cue_count": zero_duration,
        "largest_zero_duration_cluster": cluster_size,
        "zero_duration_cluster_threshold": NARRATION_ALIGNMENT_ZERO_DURATION_CLUSTER_MAX_CUES,
        "zero_duration_cluster_cue_texts": cluster_texts,
        "last_cue_end_seconds": last_cue_end,
        "audio_duration_seconds": audio_duration_seconds,
    }


def probe_mechanism(project: Path, probe: dict) -> tuple[str | None, str, dict | None]:
    """Return (mechanism, provenance, evidence) for an approved ab probe.

    Prefers the receipt's own `mechanism` field (provenance="from_receipt",
    no evidence needed -- it's already probe-receipt-bound). Probe receipts
    written before that field existed predate it, not invalidate it: derive
    the mechanism from the b-clip's own take.json and probe text file the
    confirmer already wrote (provenance "derived_from_take") rather than
    force a fresh paid probe and human re-approval when the artifacts on disk
    still prove what was tested. Those two files aren't covered by the probe
    receipt's own digests, so `evidence` carries their sha256 for the caller
    to bind into the narration receipt -- otherwise the derivation would be
    unauditable after the fact.
    """
    recorded = probe.get("mechanism")
    if isinstance(recorded, str) and recorded:
        return recorded, "from_receipt", None
    audio = probe.get("audio")
    variants = probe.get("variants")
    if not isinstance(audio, list) or not isinstance(variants, list):
        return None, "undetermined", None
    b_entry = next(
        (item for item in audio if isinstance(item, dict) and item.get("label") == "b"),
        None,
    )
    if not isinstance(b_entry, dict) or not isinstance(b_entry.get("path"), str):
        return None, "undetermined", None
    try:
        b_audio = direct_file(project / b_entry["path"])
    except WorkflowError:
        return None, "undetermined", None
    take_path = b_audio.with_suffix(".take.json")
    text_path = b_audio.with_suffix(".txt")
    if not (
        take_path.is_file() and not take_path.is_symlink()
        and text_path.is_file() and not text_path.is_symlink()
    ):
        return None, "undetermined", None
    evidence = {
        "take_path": str(take_path.relative_to(project)),
        "take_sha256": sha256(take_path),
        "text_path": str(text_path.relative_to(project)),
        "text_sha256": sha256(text_path),
    }
    take = read_json(direct_file(take_path))
    if take.get("pronunciation_dictionary"):
        return "pronunciation_dictionary", "derived_from_take", evidence
    text = text_path.read_text(encoding="utf-8")
    if all(
        isinstance(item, dict) and isinstance(item.get("spoken"), str) and item["spoken"] in text
        for item in variants
    ):
        return "inline_respelling", "derived_from_take", evidence
    return None, "undetermined", evidence


def generate_narration(project_path: Path, tools_path: Path) -> dict:
    project = direct_directory(project_path)
    tools = direct_directory(tools_path)
    state, staging = ensure_state(project)
    probe, probe_sha, plan_sha, source_sha = current_probe(project)
    review_path = direct_file(state / "pronunciation-review.json")
    review = read_json(review_path)
    review_sha = sha256(review_path)
    if not (
        review.get("schema") == REVIEW_SCHEMA
        and review.get("verdict") == "pass"
        and review.get("source_sha256") == source_sha
        and review.get("plan_sha256") == plan_sha
        and review.get("probe_sha256") == probe_sha
        and probe.get("mode") == "ab"
        and isinstance(probe.get("voice"), str)
        and isinstance(probe.get("variants"), list)
        and probe["variants"]
    ):
        raise WorkflowError("pronunciation_review_stale", "approved G2P review is required")
    mechanism, mechanism_source, mechanism_evidence = probe_mechanism(project, probe)
    if mechanism != "inline_respelling":
        raise WorkflowError(
            "pronunciation_mechanism_mismatch",
            f"approved probe mechanism ({mechanism or 'undetermined'}) does not "
            "match the sectioned generator's inline respelling",
        )

    request_path = direct_file(staging / "narration-generation-request.json")
    request = read_json(request_path)
    max_credits = request.get("max_credits")
    approved_by = request.get("spending_approved_by")
    if not (
        request.get("schema") == NARRATION_REQUEST_SCHEMA
        and isinstance(max_credits, int)
        and 1 <= max_credits <= 12000
        and isinstance(approved_by, str)
        and approved_by.strip()
    ):
        raise WorkflowError("invalid_narration_request", "invalid narration generation request")
    target_chars = request.get("section_target_chars", NARRATION_SECTION_TARGET_CHARS)
    max_chars = request.get("section_max_chars", NARRATION_SECTION_MAX_CHARS)
    if not (
        isinstance(target_chars, int)
        and isinstance(max_chars, int)
        and NARRATION_SECTION_CHARS_FLOOR <= target_chars <= max_chars
        and max_chars <= NARRATION_SECTION_CHARS_CEILING
    ):
        raise WorkflowError(
            "invalid_narration_request",
            "section sizing must satisfy "
            f"{NARRATION_SECTION_CHARS_FLOOR} <= section_target_chars <= "
            f"section_max_chars <= {NARRATION_SECTION_CHARS_CEILING}",
        )
    source = narration_file(project)
    source_text = source.read_text(encoding="utf-8").strip()
    tts_source_sha = hashlib.sha256(source_text.encode()).hexdigest()
    estimated_credits = len(source_text)
    if estimated_credits > max_credits:
        raise WorkflowError("narration_budget_exceeded", "narration estimate exceeds approved credits")
    request_sha = sha256(request_path)
    receipt_path = state / "narration-generation.json"
    if receipt_path.is_file() and not receipt_path.is_symlink():
        existing = read_json(receipt_path)
        if valid_narration_receipt(
            project, existing, source_sha, probe_sha, review_sha, request_sha
        ):
            return existing

    job_sha = hashlib.sha256(
        f"{NARRATION_SCHEMA}:{source_sha}:{probe_sha}:{review_sha}:{request_sha}".encode()
    ).hexdigest()
    candidate = staging / "narration-candidates" / job_sha[:16]
    if candidate.is_symlink():
        raise WorkflowError("invalid_path", "candidate output cannot be a symlink")
    candidate.mkdir(parents=True, exist_ok=True)
    overrides = candidate / "pronunciation-overrides.json"
    write_json_atomically(
        overrides,
        {"schema": OVERRIDES_SCHEMA, "terms": probe["variants"]},
    )
    generator = direct_file(tools / "narration/generate_sectioned_narration.py")
    # Pre-flight, before any spend: a haru-media-tools checkout without this
    # module cannot re-derive the SRT timeline, and that take would be refused
    # after the generation was paid for.
    direct_file(tools / "narration/stt_align.py")
    python = executable_from_env("HARU_TTS_PYTHON", "python3.11")
    out_base = candidate / "narration-g2p-1x"
    sections_dir = candidate / "narration-g2p-1x-sections"
    arguments = [
        str(python), str(generator), "--text-file", str(source),
        "--out-base", str(out_base), "--sections-dir", str(sections_dir),
        "--voice", probe["voice"], "--pronunciation-overrides", str(overrides),
        "--target-chars", str(target_chars),
        "--max-chars", str(max_chars),
        "--force-budget",
    ]
    environment = os.environ.copy()
    environment["TTS_BUDGET_OK"] = dt.date.today().isoformat()
    environment["PATH"] = os.pathsep.join(
        [environment.get("PATH", ""), "/opt/homebrew/bin", "/usr/local/bin"]
    )
    result = subprocess.run(
        arguments, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        env=environment, check=False,
    )
    if result.returncode != 0:
        tail = (result.stderr or result.stdout or "").strip()[-500:]
        raise WorkflowError(
            "narration_generation_failed",
            f"sectioned narration generator failed: {tail}",
        )
    take_path = direct_file(Path(f"{out_base}.take.json"))
    take = read_json(take_path)
    credit_report = take.get("run_credit_report")
    raw_credits = credit_report.get("actual_credits") if isinstance(credit_report, dict) else None
    # generate_sectioned_narration.py's credit_report() reports actual_credits
    # as None when every section was already cached -- nothing was newly
    # generated this run, not a malformed take. Without this, any post-spend
    # failure (a missing ffmpeg, a failed atempo, an alignment/pace raise)
    # would brick every re-run forever: sections stay cached, the receipt is
    # never written, and the next attempt hits this same "invalid" read. The
    # pre-spend budget check above already bounds what a run could cost, so
    # treating a fully cached re-run as 0 newly-spent credits is safe.
    credits_spent = 0 if raw_credits is None else raw_credits
    take_problems = [
        field for field, ok in (
            ("source_sha256", take.get("source_sha256") == tts_source_sha),
            ("voice", take.get("voice") == probe["voice"]),
            ("model", take.get("model") == "eleven_v3"),
            ("run_credit_report.actual_credits", isinstance(credits_spent, int)),
        )
        if not ok
    ]
    if not take_problems and not (0 <= credits_spent <= max_credits):
        take_problems.append("run_credit_report.actual_credits exceeds max_credits")
    if take_problems:
        raise WorkflowError(
            "narration_generation_failed",
            f"invalid narration take, bad field(s): {', '.join(take_problems)}",
        )

    # The receipt must state the rules actually applied, not just what this
    # runner passed in -- the child merges a voice-level fix table on top of
    # the probe's variants before speaking. Only the child can report that
    # merged set; a receipt this runner reconstructed would bind to whatever
    # local file currently happens to define the voice layer, which is a
    # different claim in every checkout and provably wrong in some of them.
    # An old haru-media-tools checkout that doesn't report it can't generate
    # narration here -- unprovable is the correct outcome, not "probably fine".
    child_reported_fixes = take.get("effective_pronunciation_fixes")
    if not isinstance(child_reported_fixes, dict) or not child_reported_fixes:
        raise WorkflowError(
            "narration_generation_failed",
            "narration take does not report which pronunciation rules were "
            "applied (effective_pronunciation_fixes); the receipt cannot "
            "state what was spoken",
        )
    missing = [
        variant["term"] for variant in probe["variants"]
        if child_reported_fixes.get(variant["term"]) != variant["spoken"]
    ]
    if missing:
        raise WorkflowError(
            "narration_generation_failed",
            f"narration take's applied fixes are missing approved probe "
            f"variant(s): {', '.join(missing)}",
        )
    effective_pronunciation_rules = {
        "reported_by": "child_take_report",
        "fixes": child_reported_fixes,
    }

    section_manifest = take.get("sections")
    if not isinstance(section_manifest, list) or not section_manifest:
        raise WorkflowError("narration_generation_failed", "narration take is missing sections")
    sections_receipt = []
    chars_cumulative = 0
    for item in section_manifest:
        if not (
            isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and isinstance(item.get("chars"), int)
            and item["chars"] > 0
        ):
            raise WorkflowError("narration_generation_failed", "narration take section entry is invalid")
        chars_cumulative += item["chars"]
        sections_receipt.append(
            {
                "id": item["id"],
                "chars": item["chars"],
                # Cumulative char counts through this section, NOT source text
                # offsets. "chars" is the length of what was sent to the
                # provider, i.e. AFTER inline respelling -- an IPA respelling
                # turns one CJK char into several -- and split_text() also drops
                # the "\n\n" between groups. Neither lands on a source index.
                "chars_cumulative_through_section": chars_cumulative,
            }
        )
    # After the section list is validated, so the alignment report can be tied
    # to the take's real section count rather than to its own self-report.
    srt_alignment = validated_srt_alignment(take, len(section_manifest))

    one_x_audio = direct_file(Path(f"{out_base}.mp3"))
    one_x_srt = direct_file(Path(f"{out_base}.srt"))
    sections_dir = direct_directory(sections_dir)
    ffmpeg = executable_from_env("HARU_FFMPEG", "ffmpeg")
    ffprobe = executable_from_env("HARU_FFPROBE", "ffprobe")

    # Never trust the provider's self-reported duration/alignment: measure the
    # actual audio bytes ourselves. This is what catches a take that silently
    # truncated generation while still claiming full coverage.
    measured_duration = ffprobe_duration_seconds(ffprobe, one_x_audio)
    merged_alignment = check_alignment_integrity("merged", one_x_srt, measured_duration)

    seam_levels = {}
    section_alignments = []
    for item in sections_receipt:
        section_id = item["id"]
        section_audio = direct_file(sections_dir / f"{section_id}.mp3")
        section_srt = direct_file(sections_dir / f"{section_id}.srt")
        # Bind the receipt's max_chars to the take rather than to the argv we
        # sent, so a child that ignores or reinterprets the flag cannot produce
        # a receipt claiming a sizing it did not use. Check the .txt, which is
        # the pre-respelling section text split_text bounded -- never the take's
        # "chars", which is measured on what the child sent the provider, after
        # inline respelling, and respelling is not length-preserving.
        section_source = direct_file(sections_dir / f"{section_id}.txt")
        if len(section_source.read_text(encoding="utf-8")) > max_chars:
            raise WorkflowError(
                "narration_generation_failed",
                f"{section_id} exceeds the requested max_chars",
            )
        section_duration = ffprobe_duration_seconds(ffprobe, section_audio)
        seam_levels[section_id] = ffmpeg_mean_volume_db(ffmpeg, section_audio)
        section_alignments.append(
            check_alignment_integrity(section_id, section_srt, section_duration)
        )

    alignment_results = [merged_alignment, *section_alignments]
    if any(item["status"] == "fail" for item in alignment_results):
        raise WorkflowError(
            "narration_alignment_truncated",
            "narration alignment claims coverage the audio does not have "
            f"(failing: {[item['label'] for item in alignment_results if item['status'] == 'fail']})",
        )

    pace_character_count = sum(not character.isspace() for character in source_text)
    narration_pace = pace_character_count / measured_duration
    if not (
        NARRATION_PACE_MIN_CHARS_PER_SECOND
        <= narration_pace
        <= NARRATION_PACE_MAX_CHARS_PER_SECOND
    ):
        raise WorkflowError(
            "narration_pace_out_of_range",
            f"narration pace {narration_pace:.2f} chars/sec is outside the "
            f"accepted {NARRATION_PACE_MIN_CHARS_PER_SECOND}-"
            f"{NARRATION_PACE_MAX_CHARS_PER_SECOND} chars/sec band",
        )

    seam_spread = max(seam_levels.values()) - min(seam_levels.values())
    seam_status = "warn" if seam_spread > NARRATION_SEAM_SPREAD_WARN_DB else "pass"

    audio = candidate / "narration-g2p-listen-1p25.mp3"
    speed_result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y", "-i",
         str(one_x_audio), "-filter:a", "atempo=1.25", "-c:a", "libmp3lame",
         "-b:a", "128k", str(audio)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, check=False,
    )
    if speed_result.returncode != 0:
        raise WorkflowError("narration_generation_failed", "audio tempo conversion failed")
    audio = direct_file(audio)
    srt = candidate / "narration-g2p-listen-1p25.srt"
    retime_srt(one_x_srt, srt, 1.25)
    srt = direct_file(srt)

    artifacts = {}
    for name, path in {
        "audio": audio,
        "srt": srt,
        "audio_1x": one_x_audio,
        "srt_1x": one_x_srt,
        "take": take_path,
        "overrides": overrides,
    }.items():
        artifacts[name] = {
            "path": str(path.relative_to(project)),
            "sha256": sha256(path),
        }
    receipt = {
        "schema": NARRATION_SCHEMA,
        "project": project.name,
        "status": "complete",
        "source_sha256": source_sha,
        "plan_sha256": plan_sha,
        "probe_sha256": probe_sha,
        "review_sha256": review_sha,
        "request_sha256": request_sha,
        "voice": probe["voice"],
        "model": "eleven_v3",
        "generation_mode": "sectioned",
        "section_count": len(sections_receipt),
        "sections": sections_receipt,
        "gap_seconds": take.get("gap_seconds"),
        "target_chars": target_chars,
        "max_chars": max_chars,
        "pronunciation_mechanism": mechanism,
        "pronunciation_mechanism_source": mechanism_source,
        "pronunciation_mechanism_evidence": mechanism_evidence,
        # The approved probe's variants only -- NOT what generation actually
        # applied. See effective_pronunciation_rules below for that.
        "approved_pronunciation_terms": probe["variants"],
        "effective_pronunciation_rules": effective_pronunciation_rules,
        "narration_duration_seconds": measured_duration,
        "narration_pace_chars_per_second": narration_pace,
        "narration_pace_basis": NARRATION_PACE_BASIS,
        "narration_pace_character_count": pace_character_count,
        "narration_pace_band": {
            "min_chars_per_second": NARRATION_PACE_MIN_CHARS_PER_SECOND,
            "max_chars_per_second": NARRATION_PACE_MAX_CHARS_PER_SECOND,
        },
        "alignment_check": {
            "status": "pass",
            "merged": merged_alignment,
            "sections": section_alignments,
        },
        # Where the SRT's timestamps came from, and how far the provider's own
        # alignment was from the audio. That distance used to be invisible -- a
        # drifting alignment reads exactly like a correct one until it is
        # measured against the audio -- which is how a take shipped with cues
        # pointing two cues behind what was actually being said. The gates above
        # cannot see it: they compare the SRT's edges to the audio's length, and
        # a timeline can drift several seconds in the middle while still
        # starting and ending in the right place.
        "srt_alignment": srt_alignment,
        "seam_check": {
            "status": seam_status,
            "levels_db": seam_levels,
            "spread_db": seam_spread,
            "warn_above_db": NARRATION_SEAM_SPREAD_WARN_DB,
        },
        "checks": {
            "pronunciation_mechanism": "pass",
            "alignment_integrity": "pass",
            "srt_alignment_source": "pass",
            "narration_pace": "pass",
            "seam_continuity": seam_status,
            "budget": "pass",
        },
        "speed": 1.25,
        "spending_approved_by": approved_by.strip(),
        "estimated_credits": estimated_credits,
        "max_credits": max_credits,
        "credits_spent": credits_spent,
        "artifacts": artifacts,
    }
    write_json_atomically(receipt_path, receipt)
    candidates_root = candidate.parent
    for stale in candidates_root.iterdir():
        if stale != candidate and not stale.is_symlink() and stale.is_dir():
            shutil.rmtree(stale)
    return receipt


def promote_narration(project_path: Path) -> dict:
    """Make an accepted narration candidate the project's canonical narration.

    This is the only SANCTIONED path from `.hvp/staging/narration-candidates/`
    to `narration-final.mp3`. It is not yet the only possible one: nothing
    downstream reads the promotion receipt. render_project_worker.py checks the
    pronunciation stamp beside the canonical audio and nothing else, and that
    stamp needs only a matching `sha256` and an empty `warnings` list -- both of
    which a hand-written file supplies. So the bindings below are what makes a
    promotion trustworthy, not what makes it mandatory. Closing that is HVP-44.

    Promotion is refused unless the same audio is named by all three of:
      - the staged request, which carries the HUMAN's acceptance and the sha256
        of what they listened to;
      - the narration receipt, which carries the machine gates it passed;
      - the bytes on disk right now.
    A human can only accept audio they heard, and only audio that passed the
    gates can be promoted, and neither can be swapped afterwards.

    The pronunciation approval stamp beside the canonical audio is rewritten
    from the same request, bound to the new sha256. It is a claim about
    particular bytes, so carrying the previous take's stamp forward would make
    it a lie about this one.
    """
    project = direct_directory(project_path)
    state, staging = ensure_state(project)

    request_path = direct_file(staging / "narration-promotion-request.json")
    request = read_json(request_path)
    accepted_by = request.get("accepted_by")
    audio_sha = request.get("audio_sha256")
    candidate_rel = request.get("candidate_audio")
    accepted_issues = request.get("accepted_issues", [])
    if not (
        request.get("schema") == PROMOTION_REQUEST_SCHEMA
        and isinstance(accepted_by, str)
        and accepted_by.strip()
        and isinstance(audio_sha, str)
        and re.fullmatch(r"[0-9a-f]{64}", audio_sha)
        and isinstance(candidate_rel, str)
        and candidate_rel
        and isinstance(accepted_issues, list)
        and all(
            isinstance(item, dict)
            and isinstance(item.get("text"), str)
            and isinstance(item.get("note"), str)
            for item in accepted_issues
        )
    ):
        raise WorkflowError("invalid_promotion_request", "invalid narration promotion request")

    # Re-derive the whole chain the generator validated, and put the receipt
    # through the SAME validator the generator uses to decide a receipt is
    # reusable. A weaker check here would let a hand-written receipt stand in
    # for the gates -- and a take that failed the truncation gate leaves a
    # plausible-looking MP3 on disk with no receipt, so "repair the missing
    # receipt" is the natural next move for a confused agent, not a contrived
    # one. This is the line between "the audio the operator accepted" and "audio".
    probe, probe_sha, plan_sha, source_sha = current_probe(project)
    review_path = direct_file(state / "pronunciation-review.json")
    review = read_json(review_path)
    review_sha = sha256(review_path)
    if not (
        review.get("schema") == REVIEW_SCHEMA
        and review.get("verdict") == "pass"
        and review.get("source_sha256") == source_sha
        and review.get("plan_sha256") == plan_sha
        and review.get("probe_sha256") == probe_sha
    ):
        raise WorkflowError(
            "pronunciation_review_stale",
            "the pronunciation review backing this narration is not the current one",
        )
    generation_request_sha = sha256(
        direct_file(staging / "narration-generation-request.json")
    )
    receipt_path = direct_file(state / "narration-generation.json")
    receipt = read_json(receipt_path)
    if not valid_narration_receipt(
        project, receipt, source_sha, probe_sha, review_sha, generation_request_sha
    ):
        raise WorkflowError(
            "narration_receipt_stale",
            "narration receipt is missing, does not describe the current script, "
            "review and request, or does not record passing gates",
        )
    artifacts = receipt["artifacts"]
    audio_entry = artifacts.get("audio")
    srt_entry = artifacts.get("srt")
    if not (
        isinstance(audio_entry, dict)
        and isinstance(srt_entry, dict)
        and isinstance(audio_entry.get("sha256"), str)
        and isinstance(srt_entry.get("path"), str)
        and isinstance(srt_entry.get("sha256"), str)
        and audio_entry.get("path") == candidate_rel
    ):
        raise WorkflowError(
            "promotion_candidate_mismatch",
            "the accepted candidate is not the audio this receipt describes",
        )

    candidate_audio = direct_file(project / candidate_rel)
    candidate_srt = direct_file(project / srt_entry["path"])
    for path in (candidate_audio, candidate_srt):
        try:
            path.relative_to(project)
        except ValueError as exc:
            raise WorkflowError("invalid_path", "candidate must live inside the project") from exc
    measured_audio_sha = sha256(candidate_audio)
    if not (
        measured_audio_sha == audio_sha == audio_entry.get("sha256")
        and sha256(candidate_srt) == srt_entry.get("sha256")
    ):
        raise WorkflowError(
            "promotion_candidate_mismatch",
            "the bytes on disk are not the ones accepted and receipted",
        )

    final_audio = project / "narration-final.mp3"
    final_srt = project / "narration-final.srt"
    stamp_path = project / "narration-final.mp3.pron-ok.json"
    # Before the short-circuit, not after: a symlinked canonical target must be
    # refused on every path, or re-running promotion is a way to keep one that a
    # fresh promotion would have rejected.
    for path in (final_audio, final_srt, stamp_path):
        if path.is_symlink():
            raise WorkflowError("invalid_path", "canonical narration must not be a symlink")

    promotion_path = state / "narration-promotion.json"
    if promotion_path.is_file() and not promotion_path.is_symlink():
        existing = read_json(promotion_path)
        existing_artifacts = existing.get("artifacts")
        if (
            existing.get("schema") == PROMOTION_SCHEMA
            and existing.get("audio_sha256") == measured_audio_sha
            and existing.get("request_sha256") == sha256(request_path)
            # Tied to the bytes directly, not only through the receipt's own two
            # fields agreeing with each other: a receipt whose audio_sha256 and
            # artifacts.audio.sha256 disagree would otherwise short-circuit while
            # the canonical file is still the previous take.
            and sha256(final_audio) == measured_audio_sha
            # Every artifact the returned receipt claims, not just the audio.
            # Reporting a promotion that is only two-thirds in effect is the
            # same class of lie as reporting one that never happened.
            and isinstance(existing_artifacts, dict)
            and all(
                (project / name).is_file()
                and isinstance(existing_artifacts.get(key), dict)
                and sha256(project / name) == existing_artifacts[key].get("sha256")
                for key, name in (
                    ("audio", "narration-final.mp3"),
                    ("srt", "narration-final.srt"),
                    ("pronunciation_stamp", "narration-final.mp3.pron-ok.json"),
                )
            )
        ):
            return existing
    previous_srt_sha = (
        sha256(final_srt) if final_srt.is_file() and not final_srt.is_symlink() else None
    )
    # The digests were measured before this read, so check the payload we are
    # about to write rather than the file after writing it. Checking afterwards
    # would still catch a candidate that changed in between, but only by leaving
    # canonical bytes that neither the stamp nor the receipt describes -- a
    # corrupt project AND a raise, where this is just a raise.
    # No test covers this: the race cannot be produced on demand from a
    # subprocess, and a monkeypatched one would only test the mock.
    audio_payload = candidate_audio.read_bytes()
    srt_payload = candidate_srt.read_bytes()
    if (
        hashlib.sha256(audio_payload).hexdigest() != measured_audio_sha
        or hashlib.sha256(srt_payload).hexdigest() != srt_entry["sha256"]
    ):
        raise WorkflowError(
            "promotion_candidate_mismatch",
            "the candidate changed while it was being promoted",
        )
    replace_file_atomically(final_audio, audio_payload)
    replace_file_atomically(final_srt, srt_payload)
    write_json_atomically(
        stamp_path,
        {
            "schema": PRONUNCIATION_APPROVAL_SCHEMA,
            "status": "pass",
            "sha256": measured_audio_sha,
            "warnings": [],
            "accepted_issues": [
                {**item, "disposition": "accepted_by_human"} for item in accepted_issues
            ],
            "approved_by": accepted_by.strip(),
            "approved_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        },
    )

    promotion = {
        "schema": PROMOTION_SCHEMA,
        "project": project.name,
        "status": "complete",
        "accepted_by": accepted_by.strip(),
        "promoted_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "candidate_audio": candidate_rel,
        "candidate_srt": srt_entry["path"],
        "audio_sha256": measured_audio_sha,
        "srt_sha256": sha256(candidate_srt),
        "request_sha256": sha256(request_path),
        "narration_receipt_sha256": sha256(receipt_path),
        "source_sha256": receipt["source_sha256"],
        # Carried onto the promotion so the canonical narration can be traced to
        # the gates it passed without re-reading the generation receipt, which a
        # later run will overwrite.
        "narration_duration_seconds": receipt.get("narration_duration_seconds"),
        "srt_alignment": receipt.get("srt_alignment"),
        # A timed storyboard built against the previous narration would cut the
        # visuals on the old cue times with the new audio, and every gate would
        # still pass. Retired here and named, so the re-timing is a visible step
        # rather than something the operator has to remember.
        "retired_timed_storyboard": invalidate_timed_storyboard(
            project, previous_srt_sha, sha256(final_srt)
        ),
        "artifacts": {
            "audio": {"path": "narration-final.mp3", "sha256": measured_audio_sha},
            "srt": {"path": "narration-final.srt", "sha256": sha256(final_srt)},
            "pronunciation_stamp": {
                "path": "narration-final.mp3.pron-ok.json",
                "sha256": sha256(stamp_path),
            },
        },
    }
    write_json_atomically(promotion_path, promotion)
    return promotion


def invalidate_timed_storyboard(project: Path, previous_srt_sha: str | None,
                                promoted_srt_sha: str) -> list[str]:
    """Retire a timed storyboard built against a different narration timeline.

    time_storyboard.py derives storyboard-final-timed.json from
    narration-final.srt and records no digest of it, and the layout gate accepts
    the validation file on `ok: true` alone. So promoting a new narration under
    a timed storyboard leaves the render cutting visuals on the old cue times
    with the new audio, and every gate still passes -- exactly the silent
    wrongness this runner exists to prevent, one step downstream.

    Renaming rather than deleting: the old timings are still the best starting
    point for re-timing, and nothing should quietly destroy them.
    """
    if previous_srt_sha == promoted_srt_sha:
        return []
    retired = []
    for name in ("storyboard-final-timed.json", "storyboard-final-timed-validation.json"):
        path = project / name
        if path.is_file() and not path.is_symlink():
            os.replace(path, project / f"{name}.stale-narration-{promoted_srt_sha[:12]}")
            retired.append(name)
    return retired


def validate_current_review(project_path: Path) -> dict:
    project = direct_directory(project_path)
    if not (project / "pronunciation-plan.json").exists():
        return {
            "required": False,
            "ok": True,
            "code": "pronunciation_review_not_required",
            "files": [],
        }
    try:
        _, probe_sha, plan_sha, source_sha = current_probe(project)
        review_path = direct_file(project / ".hvp/pronunciation-review.json")
        receipt = read_json(review_path)
    except WorkflowError:
        return {
            "required": True,
            "ok": False,
            "code": "pronunciation_review_stale",
            "files": [],
        }
    bound = bool(
        receipt.get("schema") == REVIEW_SCHEMA
        and receipt.get("project") == project.name
        and receipt.get("source_sha256") == source_sha
        and receipt.get("plan_sha256") == plan_sha
        and receipt.get("probe_sha256") == probe_sha
        and isinstance(receipt.get("reviewed_by"), str)
        and bool(receipt["reviewed_by"].strip())
        and isinstance(receipt.get("notes"), str)
        and bool(receipt["notes"].strip())
    )
    verdict = receipt.get("verdict") if bound else None
    return {
        "required": True,
        "ok": verdict == "pass",
        "code": (
            "pronunciation_review_current"
            if verdict == "pass"
            else "pronunciation_review_failed"
            if verdict == "fail"
            else "pronunciation_review_stale"
        ),
        "files": [
            "pronunciation-plan.json",
            ".hvp/pronunciation-probes.json",
            ".hvp/pronunciation-review.json",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    analyze_parser = subparsers.add_parser("analyze")
    analyze_parser.add_argument("project", type=Path)
    analyze_parser.add_argument("tools_root", type=Path)
    confirm_parser = subparsers.add_parser("confirm")
    confirm_parser.add_argument("project", type=Path)
    confirm_parser.add_argument("tools_root", type=Path)
    generate_parser = subparsers.add_parser("generate-narration")
    generate_parser.add_argument("project", type=Path)
    generate_parser.add_argument("tools_root", type=Path)
    promote_parser = subparsers.add_parser("promote-narration")
    promote_parser.add_argument("project", type=Path)
    review_parser = subparsers.add_parser("review")
    review_parser.add_argument("project", type=Path)
    review_parser.add_argument("--reviewed-by", required=True)
    review_parser.add_argument("--verdict", required=True, choices=["pass", "fail"])
    review_parser.add_argument("--notes", required=True)
    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("project", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "analyze":
            value = analyze(args.project, args.tools_root)
        elif args.action == "confirm":
            value = confirm(args.project, args.tools_root)
        elif args.action == "review":
            value = review(args.project, args.reviewed_by, args.verdict, args.notes)
        elif args.action == "generate-narration":
            value = generate_narration(args.project, args.tools_root)
        elif args.action == "promote-narration":
            value = promote_narration(args.project)
        else:
            value = validate_current_review(args.project)
    except WorkflowError as exc:
        print(
            json.dumps(
                {"schema_version": 1, "outcome": "error", "code": exc.code, "data": None}
            )
        )
        return 2
    print(json.dumps(value, ensure_ascii=False))
    return 0 if args.action != "check" or value["ok"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
