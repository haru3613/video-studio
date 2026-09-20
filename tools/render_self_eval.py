#!/usr/bin/env python3
"""Render self-evaluation: a repo-owned structural gate on the final mix.

Where this sits: after `output/final.mp4` plus its render marker, and before the
HVP-21 whole-video human QA. It never replaces a human authority; it produces
bounded, digest-bound evidence about one exact candidate, seals a deterministic
verdict when the render is structurally broken, and otherwise hands a reviewer a
clean evaluation to accept or refuse.

Three properties are load-bearing, and every one of them is a security property:

* Bytes, not pathnames, are the subject. Every identity input is opened
  component-by-component with `O_NOFOLLOW`, hashed from the held descriptor, and
  copied into a runtime-private directory before any tool runs. ffmpeg only ever
  sees the private snapshot, under a fixed relative argv. A pathname swapped
  mid-run cannot change what was analyzed, and cannot make a clean digest stand
  next to evidence from a different file.
* Attempts are immutable and finite. Three ordinals, create-once files, and no
  fourth attempt. A deterministic failure can never be talked into a review.
* The deciding state is external. The project tree is an audit mirror; the
  protected ledger in `self_eval_authority` is authority, and every reviewer
  verdict consumes a one-time external attestation. A writable project cannot
  fabricate a pass, and restoring an older project tree does not restore one.

V1 mutates no media: `allowed_repairs` is empty and `automatic_fix_applied` is
null. A structural failure is fixed at the source and rendered again.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import canonical_layout  # noqa: E402
import editorial_contract  # noqa: E402
import render_contract  # noqa: E402
import segment_plan  # noqa: E402
import self_eval_authority as authority  # noqa: E402

RESULT_SCHEMA = "haru.render_self_eval.v1"
IDENTITY_SCHEMA = "haru.render_self_eval_identity.v1"
POLICY_SCHEMA = "haru.render_self_eval_boundary_policy.v1"
PLAN_SCHEMA = "haru.render_self_eval_boundary_plan.v1"
COMMANDS_SCHEMA = "haru.render_self_eval_commands.v1"
FACTS_SCHEMA = "haru.render_self_eval_facts.v1"
INDEX_SCHEMA = "haru.render_self_eval_evidence_index.v1"
EVALUATION_SCHEMA = "haru.render_self_eval_attempt.v1"
UNAVAILABLE_SCHEMA = "haru.render_self_eval_vision_unavailable.v1"
REVIEW_SCHEMA = "haru.render_self_eval_review.v1"
OUTCOME_SCHEMA = "haru.render_self_eval_outcome.v1"
RETIREMENT_SCHEMA = "haru.render_self_eval_retirement.v1"
VISION_INTENT_SCHEMA = "haru.self_eval_vision_review_intent.v1"
HUMAN_INTENT_SCHEMA = "haru.self_eval_human_review_intent.v1"

ALGORITHM = "haru.render_self_eval.v2"
HUMAN_CAPABILITY = "human_structural_attestation.v1"

ROOT = "quality-review/render-self-eval"
RESULT_PATH = f"{ROOT}/render-self-eval.json"
POLICY_PATH = f"{ROOT}/boundary-policy.json"
CURRENT_PLAN_PATH = f"{ROOT}/boundary-plan.json"
CURRENT_REVIEW_PATH = f"{ROOT}/review.json"
ATTEMPTS_PATH = f"{ROOT}/attempts"
ORPHANS_PATH = f"{ROOT}/orphans"
MAX_ATTEMPTS = 3

STATUS_NEEDS_HUMAN = "needs_human"
STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_HUMAN_INTERVENTION = "human_intervention_required"
STATUSES = (STATUS_NEEDS_HUMAN, STATUS_PASS, STATUS_FAIL, STATUS_HUMAN_INTERVENTION)

CANDIDATE = "output/final.mp4"
MARKER = "output/final.mp4.render-result"
STORYBOARD = "storyboard-final-timed.json"
VALIDATION = "storyboard-final-timed-validation.json"
SRT = "narration-final.srt"
EDITORIAL = "editorial-contract.json"
ASSEMBLY = "quality-review/segments/assembly.json"
INPUT_ORDER = (CANDIDATE, MARKER, STORYBOARD, VALIDATION, SRT, EDITORIAL, ASSEMBLY)
ALWAYS_PRESENT = (CANDIDATE, MARKER, STORYBOARD, VALIDATION, SRT)
SNAPSHOT_NAMES = {
    CANDIDATE: "candidate.mp4",
    MARKER: "candidate.render-result",
    STORYBOARD: "storyboard.json",
    VALIDATION: "storyboard-validation.json",
    SRT: "narration.srt",
    EDITORIAL: "editorial-contract.json",
    ASSEMBLY: "assembly.json",
}

RETIREMENT_SOURCES = (
    "quality-review/visual-sampling/visual-qa-sample.json",
    "quality-review/visual-sampling/visual-qa-contact-sheet.png",
    "quality-review/visual-sampling/visual-qa-index.md",
    "quality-review/visual-sampling/visual-qa-review.json",
    "publish/publish-approval.json",
)
RETIRED_VISUAL_DIR = "quality-review/visual-sampling/retired-pre-self-eval"
APPROVAL_HISTORY_DIR = "publish/approval-history"
VISUAL_TOMBSTONE = f"{RETIRED_VISUAL_DIR}/retirement.json"
APPROVAL_TOMBSTONE = f"{APPROVAL_HISTORY_DIR}/self-eval-cutover.json"

INSPIRED_BY = {
    "repository": "https://github.com/browser-use/video-use",
    "commit": "92c2b34e44c205cbc2acae7f6ca7c1c219d5dd66",
    "helper_path": "helpers/timeline_view.py",
    "helper_sha256": "69aee88e4204f86127740cca9de6a6eaa75a558df1bb07745dd62f69a3c2e9cf",
}

# One repo constant. `boundary-policy.json` is exactly its canonical bytes, and
# its digest is inside the attempt identity: changing any threshold changes every
# identity, so no evaluation is ever compared across policies.
POLICY = {
    "schema": POLICY_SCHEMA,
    "algorithm": "haru.render_self_eval_policy.v2",
    "frames_per_window": 10,
    "boundary_before_seconds": 1.5,
    "boundary_after_seconds": 1.5,
    "edge_window_seconds": 2.0,
    "midpoint_fractions": [1 / 3, 2 / 3],
    "max_windows": 256,
    "max_total_evidence_bytes": 536870912,
    "sampling": {
        "mode": "half_open_bin_center_with_decoded_tail",
        "bins": 10,
        "rounding_decimals": 6,
        "tail_frame_strategy": "clamp_to_last_decoded_pts_within_window",
    },
    "video_tail_coverage": {
        "required_end": "latest_timed_scene_or_visual_event_end",
        "final_frame_end": "last_decoded_pts_plus_one_frame",
        "tolerance_seconds": 0.002,
    },
    "geometry": {
        "video_map": "0:v:0",
        "audio_map": "0:a:0",
        "frame_width": 320,
        "filmstrip_tile": "10x1",
        "waveform_size": "1280x240",
        "composite_width": 3200,
        "composite_stack": "vstack",
    },
    "black_detection": {
        "filter": "blackdetect",
        "pixel_threshold": 0.10,
        "picture_threshold": 0.98,
        "minimum_duration_frames": 0.5,
    },
    "edge_black_detection": {
        "window_seconds": 2.0,
        "coverage_fraction": 0.90,
    },
    "frame_gap_detection": {
        "gap_frames": 1.5,
        "tolerance_seconds": 0.002,
    },
    "audio_discontinuity": {
        "sample_rate": 48000,
        "sample_format": "s16le",
        "channels": 1,
        "window_seconds": 0.020,
        "jump_threshold": 0.50,
        "full_scale": 32768,
    },
    "tool_provenance": {
        "algorithm": ALGORITHM,
        "inspired_by": dict(INSPIRED_BY),
    },
}
POLICY_BYTES = authority.canonical_bytes(POLICY)
POLICY_SHA256 = authority.canonical_digest(POLICY)

# The container duration is driven by the narration mix, so it lands within a
# frame of the authored duration but is not bit-identical to it. One frame plus
# 50ms is wide enough for a correct render and far too narrow to hide a
# truncated or doubled one.
DURATION_TOLERANCE_SECONDS = 0.05

NEXT_ACTIONS = {
    STATUS_NEEDS_HUMAN: (
        "submit a render-self-eval-review vision verdict for the current attempt"
    ),
    STATUS_PASS: "proceed to whole-video visual QA (HVP-21)",
    STATUS_FAIL: "fix the source, render again, then reevaluate",
    STATUS_HUMAN_INTERVENTION: (
        "human intervention required: three self-evaluation attempts failed"
    ),
}
REQUIRED_ACTIONS = {
    STATUS_NEEDS_HUMAN: "await_reviewer_verdict",
    STATUS_PASS: "continue_to_visual_qa",
    STATUS_FAIL: "repair_source_and_rerender",
    STATUS_HUMAN_INTERVENTION: "escalate_to_human",
}
TRANSITION_STATUS = {
    "evaluate_pending": STATUS_NEEDS_HUMAN,
    "vision_unavailable": STATUS_NEEDS_HUMAN,
    "evaluate_fail": STATUS_FAIL,
    "vision_fail": STATUS_FAIL,
    "human_fail": STATUS_FAIL,
    "vision_pass": STATUS_PASS,
    "human_pass": STATUS_PASS,
}
FINDING_KEYS = {"timestamp_seconds", "boundary_id", "category", "severity", "message"}
REVIEW_INPUT_KEYS = {
    "reviewer_kind",
    "verdict",
    "reviewed_by",
    "provider",
    "model",
    "capability",
    "notes",
    "findings",
}
RESULT_KEYS = {
    "schema",
    "project",
    "status",
    "attempt",
    "max_attempts",
    "attempt_identity",
    "inputs",
    "boundary_policy",
    "boundary_plan",
    "evaluation",
    "evidence_index",
    "review",
    "outcome",
    "tool",
    "findings",
    "verdict",
    "remediation",
    "next_action",
    "updated_at",
}
EVALUATION_KEYS = {
    "schema",
    "project",
    "attempt",
    "attempt_identity",
    "inputs",
    "boundary_policy",
    "boundary_plan",
    "commands",
    "facts",
    "evidence_index",
    "tool",
    "checks",
    "findings",
    "status",
    "remediation",
    "evaluated_at",
}
OUTCOME_KEYS = {
    "schema",
    "project",
    "attempt",
    "attempt_identity",
    "source",
    "verdict",
    "evaluation",
    "evidence_index",
    "review",
    "findings",
    "authority",
    "sealed_at",
}
REVIEW_KEYS = {
    "schema",
    "project",
    "attempt",
    "attempt_identity",
    "evaluation",
    "evidence_index",
    "vision_unavailable",
    "reviewer_kind",
    "verdict",
    "reviewed_by",
    "provider",
    "model",
    "capability",
    "notes",
    "findings",
    "review_intent_sha256",
    "authority",
    "reviewed_at",
}
UNAVAILABLE_KEYS = {
    "schema",
    "project",
    "attempt",
    "attempt_identity",
    "evaluation",
    "evidence_index",
    "reviewed_by",
    "provider",
    "model",
    "capability",
    "result",
    "notes",
    "recorded_at",
    "authority",
}
WINDOW_KEYS = {
    "window_id",
    "kind",
    "start_seconds",
    "end_seconds",
    "boundary_seconds",
    "authored_ids",
    "srt_labels",
    "sample_times_seconds",
}
PLAN_KEYS = {
    "schema",
    "project",
    "attempt_identity",
    "inputs",
    "boundary_policy",
    "duration_seconds",
    "fps",
    "windows",
    "created_at",
}
COMMANDS_KEYS = {"schema", "project", "attempt_identity", "commands"}
COMMAND_KEYS = {
    "ordinal",
    "purpose",
    "argv",
    "exit_code",
    "stdout_sha256",
    "stderr_sha256",
    "started_at",
    "finished_at",
}
FACTS_KEYS = {"schema", "project", "attempt_identity", "media", "windows"}
MEDIA_KEYS = {
    "duration_seconds",
    "fps",
    "video_streams",
    "audio_streams",
    "full_decode_clean",
    "frame_pts_monotonic",
    "packet_dts_monotonic",
}
WINDOW_FACT_KEYS = {
    "window_id",
    "frame_timestamps_seconds",
    "black_intervals",
    "audio_jump",
}
BLACK_INTERVAL_KEYS = {"start_seconds", "end_seconds", "duration_seconds"}
AUDIO_JUMP_KEYS = {"value", "timestamp_seconds"}
INDEX_KEYS = {
    "schema",
    "project",
    "attempt_identity",
    "entries",
    "total_available_bytes",
}
AVAILABLE_ENTRY_KEYS = {
    "path",
    "kind",
    "availability",
    "sha256",
    "bytes",
    "window_id",
}
UNAVAILABLE_ENTRY_KEYS = {
    "kind",
    "availability",
    "reason",
    "window_id",
}
CHECK_KEYS = {"check_id", "status", "scope", "details"}

BLACK_LINE = re.compile(
    r"black_start:(?P<start>[0-9.]+)\s+black_end:(?P<end>[0-9.]+)\s+"
    r"black_duration:(?P<duration>[0-9.]+)"
)
SRT_STAMP = re.compile(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})")


class SelfEvalError(ValueError):
    """Any refusal. Callers surface the message; nothing is half-applied."""


AuthorityError = authority.AuthorityError


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _round(value) -> float:
    return round(float(value), 6)


def _number(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def attempt_dir(attempt: int) -> str:
    return f"{ATTEMPTS_PATH}/attempt-{attempt:02d}"


def _project_dir(project) -> Path:
    path = Path(project)
    resolved = Path(os.path.abspath(path))
    if path.is_symlink() or not resolved.is_dir():
        raise SelfEvalError("project must be a direct existing directory")
    return resolved


def _ref(path: str, payload: bytes) -> dict:
    return {
        "path": path,
        "sha256": authority.hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }


def _read_project_json(project: Path, relative: str):
    try:
        fd = authority.open_project_file_fd(project, relative)
    except AuthorityError:
        return None
    try:
        payload = authority.read_fd(fd)
    except AuthorityError:
        return None
    finally:
        os.close(fd)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


# --------------------------------------------------------------------------
# which inputs form this project's identity
# --------------------------------------------------------------------------


def editorial_required(project: Path) -> bool:
    """`editorial_contract.LANE_PROFILES` is the one place this mapping lives."""
    contract = _read_project_json(project, "project-contract.json")
    if contract is None:
        # No lane authority to read. Demanding the presenter contract is the
        # only safe direction: silently treating it as absent would drop a real
        # authority input out of the identity.
        return True
    lane = contract.get("lane_contract")
    pinned = editorial_contract.LANE_PROFILES
    if (
        contract.get("schema") != "haru.project_contract.v1"
        or lane not in pinned
        or contract.get("production_profile") != pinned[lane]
    ):
        raise SelfEvalError("project contract lane authority is invalid")
    return pinned[lane] is not None


def assembly_required(project: Path) -> bool:
    mode = segment_plan.validate(project).get("mode")
    if mode == "segmented":
        return True
    if mode == "legacy_non_segmented":
        return False
    raise SelfEvalError("segment plan is invalid; self-evaluation cannot bind it")


def segment_seams(project: Path) -> list:
    """Interior HVP-52 segment seams, in seconds, from the validated plan."""
    plan = segment_plan.validate(project)
    if plan.get("mode") != "segmented":
        return []
    seams = []
    for record in plan.get("segments", [])[1:]:
        selector = record.get("selector") or {}
        fps = selector.get("fps")
        start_frame = selector.get("start_frame")
        if not isinstance(fps, int) or fps <= 0 or not isinstance(start_frame, int):
            raise SelfEvalError("segment selector is invalid")
        seams.append((_round(start_frame / fps), f"seam:{record.get('segment_id')}"))
    return seams


# --------------------------------------------------------------------------
# snapshot: bytes are the subject, pathnames are only a stale check
# --------------------------------------------------------------------------


def _held_root_is_current(project: Path, root_fd: int) -> bool:
    try:
        current = os.stat(project, follow_symlinks=False)
        held = os.fstat(root_fd)
    except OSError:
        return False
    return (
        stat.S_ISDIR(current.st_mode)
        and current.st_dev == held.st_dev
        and current.st_ino == held.st_ino
    )


def _require_held_root(project: Path, root_fd: int) -> None:
    if not _held_root_is_current(project, root_fd):
        raise SelfEvalError("project root changed during the operation")


class Snapshot:
    """Fd-held, byte-exact copies of every identity input, plus the identity."""

    def __init__(self, project: Path):
        self.project = project
        self.root_fd = authority.open_project_fd(project)
        self.directory = Path(tempfile.mkdtemp(prefix=".haru-self-eval-"))
        os.chmod(self.directory, 0o700)
        self.work = self.directory / "work"
        self.stage = self.directory / "stage"
        self.work.mkdir(mode=0o700)
        self.stage.mkdir(mode=0o700)
        self.entries = []
        self.present = {}
        self.expected_authority = {}
        try:
            self._capture()
            self.identity = authority.canonical_digest(self.preimage())
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        root_fd = getattr(self, "root_fd", -1)
        if root_fd is not None and root_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(root_fd)
            self.root_fd = -1
        shutil.rmtree(self.directory, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False

    def _expected(self) -> dict:
        expected = {relative: True for relative in ALWAYS_PRESENT}
        expected[EDITORIAL] = editorial_required(self.project)
        expected[ASSEMBLY] = assembly_required(self.project)
        return expected

    def _capture(self) -> None:
        expected = self._expected()
        self.expected_authority = expected
        for relative in INPUT_ORDER:
            wanted = expected[relative]
            try:
                fd = authority.open_relative_file_fd(self.root_fd, relative)
            except AuthorityError as error:
                if wanted:
                    raise SelfEvalError(
                        f"required identity input is missing: {relative}"
                    ) from error
                self.entries.append({"path": relative, "state": "absent"})
                continue
            try:
                if not wanted:
                    raise SelfEvalError(
                        f"unexpected authority for this lane: {relative}"
                    )
                digest, size = self._snapshot(fd, relative)
            finally:
                os.close(fd)
            self.entries.append(
                {
                    "path": relative,
                    "state": "present",
                    "sha256": digest,
                    "bytes": size,
                }
            )
            self.present[relative] = digest

    def _snapshot(self, fd: int, relative: str) -> tuple:
        """One pass: the digest and the private copy come from the same read."""
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SelfEvalError(f"identity input is not a regular file: {relative}")
        digest = authority.hashlib.sha256()
        total = 0
        os.lseek(fd, 0, os.SEEK_SET)
        target = self.directory / SNAPSHOT_NAMES[relative]
        with target.open("wb") as handle:
            while True:
                block = os.read(fd, authority.CHUNK)
                if not block:
                    break
                digest.update(block)
                handle.write(block)
                total += len(block)
            handle.flush()
            os.fsync(handle.fileno())
        if os.fstat(fd).st_size != total or total == 0:
            raise SelfEvalError(f"identity input changed while snapshotting: {relative}")
        return digest.hexdigest(), total

    def path(self, relative: str) -> Path:
        return self.directory / SNAPSHOT_NAMES[relative]

    def text(self, relative: str) -> str:
        try:
            return self.path(relative).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise SelfEvalError(f"identity input is not UTF-8 text: {relative}") from error

    def json(self, relative: str):
        try:
            value = json.loads(self.text(relative))
        except json.JSONDecodeError as error:
            raise SelfEvalError(f"identity input is not JSON: {relative}") from error
        if not isinstance(value, dict):
            raise SelfEvalError(f"identity input is not a JSON object: {relative}")
        return value

    def preimage(self) -> dict:
        return {
            "schema": IDENTITY_SCHEMA,
            "inputs": self.entries,
            "boundary_policy_sha256": POLICY_SHA256,
        }

    def stale(self) -> bool:
        """Have the project pathnames moved off the snapshotted bytes?"""
        if not _held_root_is_current(self.project, self.root_fd):
            return True
        try:
            if self._expected() != self.expected_authority:
                return True
        except (SelfEvalError, OSError, ValueError):
            return True
        for entry in self.entries:
            relative = entry["path"]
            try:
                fd = authority.open_relative_file_fd(self.root_fd, relative)
            except AuthorityError:
                if entry["state"] == "absent":
                    continue
                return True
            try:
                digest, size = authority.hash_fd(fd)
            except AuthorityError:
                return True
            finally:
                os.close(fd)
            if entry["state"] == "absent":
                return True
            if digest != entry["sha256"] or size != entry["bytes"]:
                return True
        return False


def identity_bytes(project) -> bytes:
    """Exact identity preimage bytes for one project. Cross-language fixture."""
    with Snapshot(_project_dir(project)) as snapshot:
        return authority.canonical_bytes(snapshot.preimage())


# --------------------------------------------------------------------------
# recorded tool runs
# --------------------------------------------------------------------------


class Runner:
    """Every tool run, recorded. Fixed argv, relative paths, no caller input."""

    def __init__(self, cwd: Path):
        self.cwd = cwd
        self.commands = []

    def run(self, purpose: str, argv: list) -> subprocess.CompletedProcess:
        started = now_iso()
        try:
            completed = subprocess.run(
                argv,
                cwd=self.cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError as error:
            raise SelfEvalError(f"{argv[0]} is unavailable") from error
        self.commands.append(
            {
                "ordinal": len(self.commands) + 1,
                "purpose": purpose,
                "argv": list(argv),
                "exit_code": completed.returncode,
                "stdout_sha256": authority.hashlib.sha256(completed.stdout).hexdigest(),
                "stderr_sha256": authority.hashlib.sha256(completed.stderr).hexdigest(),
                "started_at": started,
                "finished_at": now_iso(),
            }
        )
        return completed

    def probe_json(self, purpose: str, argv: list):
        completed = self.run(purpose, argv)
        if completed.returncode != 0:
            return None
        try:
            value = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def version(self, executable: str) -> str:
        completed = self.run(f"{executable}_version", [executable, "-version"])
        first = completed.stdout.decode("utf-8", "replace").splitlines()
        return first[0].strip() if first else "unknown"


# --------------------------------------------------------------------------
# media facts
# --------------------------------------------------------------------------


def _fps_from_rate(value):
    if not isinstance(value, str) or "/" not in value:
        return None
    numerator, denominator = value.split("/", 1)
    try:
        top, bottom = int(numerator), int(denominator)
    except ValueError:
        return None
    if top <= 0 or bottom <= 0:
        return None
    return top / bottom


def probe_media(runner: Runner) -> dict:
    value = runner.probe_json(
        "probe_streams",
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            "candidate.mp4",
        ],
    )
    streams = value.get("streams") if isinstance(value, dict) else None
    container = value.get("format") if isinstance(value, dict) else None
    if not isinstance(streams, list) or not isinstance(container, dict):
        return {
            "probed": False,
            "duration_seconds": None,
            "fps": None,
            "video_streams": 0,
            "audio_streams": 0,
        }
    video = [s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"]
    audio = [s for s in streams if isinstance(s, dict) and s.get("codec_type") == "audio"]
    try:
        duration = float(container.get("duration"))
    except (TypeError, ValueError):
        duration = None
    if duration is not None and not math.isfinite(duration):
        duration = None
    fps = _fps_from_rate(video[0].get("r_frame_rate")) if video else None
    return {
        "probed": True,
        "duration_seconds": duration,
        "fps": fps,
        "video_streams": len(video),
        "audio_streams": len(audio),
    }


def probe_frame_timestamps(runner: Runner) -> list | None:
    value = runner.probe_json(
        "probe_frames",
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "frame=best_effort_timestamp_time",
            "-of",
            "json",
            "candidate.mp4",
        ],
    )
    frames = value.get("frames") if isinstance(value, dict) else None
    if not isinstance(frames, list):
        return None
    stamps = []
    for frame in frames:
        raw = frame.get("best_effort_timestamp_time") if isinstance(frame, dict) else None
        if raw in (None, "N/A"):
            continue
        try:
            stamp = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(stamp):
            return None
        stamps.append(stamp)
    return stamps


def probe_packet_monotonic(runner: Runner) -> bool:
    value = runner.probe_json(
        "probe_packets",
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "packet=stream_index,dts_time",
            "-of",
            "json",
            "candidate.mp4",
        ],
    )
    packets = value.get("packets") if isinstance(value, dict) else None
    if not isinstance(packets, list) or not packets:
        return False
    seen = {}
    for packet in packets:
        if not isinstance(packet, dict) or packet.get("dts_time") in (None, "N/A"):
            continue
        try:
            stamp = float(packet["dts_time"])
        except (TypeError, ValueError):
            return False
        if not math.isfinite(stamp):
            return False
        index = packet.get("stream_index")
        if index in seen and stamp + 1e-6 < seen[index]:
            return False
        seen[index] = stamp
    return bool(seen)


def decode_clean(runner: Runner) -> bool:
    completed = runner.run(
        "decode_full",
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "candidate.mp4",
            "-map",
            "0",
            "-f",
            "null",
            "-",
        ],
    )
    return completed.returncode == 0 and not completed.stderr.strip()


def detect_black(runner: Runner, fps: float) -> list:
    minimum = POLICY["black_detection"]["minimum_duration_frames"] / fps
    completed = runner.run(
        "black_detect",
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "info",
            "-i",
            "candidate.mp4",
            "-map",
            POLICY["geometry"]["video_map"],
            "-vf",
            "blackdetect=pix_th={0:.2f}:pic_th={1:.2f}:d={2:.6f}".format(
                POLICY["black_detection"]["pixel_threshold"],
                POLICY["black_detection"]["picture_threshold"],
                minimum,
            ),
            "-f",
            "null",
            "-",
        ],
    )
    intervals = []
    for match in BLACK_LINE.finditer(completed.stderr.decode("utf-8", "replace")):
        start = float(match.group("start"))
        end = float(match.group("end"))
        if not math.isfinite(start) or not math.isfinite(end) or end < start:
            continue
        intervals.append(
            {
                "start_seconds": _round(start),
                "end_seconds": _round(end),
                "duration_seconds": _round(end - start),
            }
        )
    intervals.sort(key=lambda item: (item["start_seconds"], item["end_seconds"]))
    return intervals


def extract_audio(runner: Runner, snapshot: Snapshot) -> Path | None:
    policy = POLICY["audio_discontinuity"]
    completed = runner.run(
        "audio_extract",
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            "candidate.mp4",
            "-map",
            POLICY["geometry"]["audio_map"],
            "-ac",
            str(policy["channels"]),
            "-ar",
            str(policy["sample_rate"]),
            "-f",
            policy["sample_format"],
            "work/audio.raw",
        ],
    )
    target = snapshot.work / "audio.raw"
    if completed.returncode != 0 or not target.is_file() or target.stat().st_size <= 0:
        return None
    return target


# --------------------------------------------------------------------------
# boundary plan
# --------------------------------------------------------------------------


def parse_srt_labels(text: str) -> list:
    """Cue text for window labels only.

    Deliberately lenient, and deliberately not the segment-plan cue parser:
    that one is cue *authority* and must refuse a malformed timeline, while a
    label is context for a reviewer. A broken cue block costs a label, never a
    verdict.
    """
    cues = []
    for index, block in enumerate(re.split(r"\r?\n\r?\n+", text.strip()), 1):
        lines = block.splitlines()
        if len(lines) < 2 or "-->" not in lines[1]:
            continue
        stamps = SRT_STAMP.findall(lines[1])
        if len(stamps) != 2:
            continue
        bounds = []
        for hours, minutes, seconds, millis in stamps:
            if int(minutes) >= 60 or int(seconds) >= 60:
                bounds = []
                break
            bounds.append(
                int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis) / 1000
            )
        if len(bounds) != 2 or bounds[1] <= bounds[0]:
            continue
        try:
            cue_index = int(lines[0].strip())
        except ValueError:
            cue_index = index
        cues.append(
            {
                "cue_index": cue_index,
                "start_seconds": _round(bounds[0]),
                "end_seconds": _round(bounds[1]),
                "text": "\n".join(lines[2:]).strip(),
            }
        )
    return cues


def authored_boundaries(storyboard: dict, seams: list, duration: float) -> list:
    marks: dict = {}

    def add(value, identifier: str) -> None:
        if not _number(value):
            return
        seconds = _round(value)
        if seconds <= 0 or seconds >= duration:
            return
        marks.setdefault(seconds, set()).add(identifier)

    scenes = storyboard.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise SelfEvalError("timed storyboard has no scenes")
    for scene in scenes:
        if not isinstance(scene, dict):
            raise SelfEvalError("timed storyboard scene is invalid")
        scene_id = scene.get("scene_id")
        scene_start = scene.get("start_seconds")
        if not isinstance(scene_id, str) or not scene_id or not _number(scene_start):
            raise SelfEvalError("timed storyboard scene identity or timing is invalid")
        add(scene_start, f"scene:{scene_id}")
        previous = None
        for event in scene.get("visual_events") or []:
            if not isinstance(event, dict):
                continue
            event_id = event.get("event_id")
            start = event.get("start_seconds")
            if not isinstance(event_id, str) or not event_id or not _number(start):
                continue
            if float(start) > float(scene_start):
                add(start, f"event:{event_id}")
            if previous is not None and any(
                previous.get(key) != event.get(key)
                for key in ("visual_state", "presenter_state", "overlay")
            ):
                add(start, f"state:{event_id}")
            previous = event
    for seconds, identifier in seams:
        add(seconds, identifier)
    return sorted(marks.items())


def _window(
    window_id: str,
    kind: str,
    start: float,
    end: float,
    boundary: float,
    authored_ids: list,
    cues: list,
    frame_timestamps: list | None = None,
) -> dict:
    start = _round(max(0.0, start))
    end = _round(end)
    if end <= start:
        raise SelfEvalError(f"window {window_id} has no positive duration")
    bins = POLICY["sampling"]["bins"]
    samples = [_round(start + (index + 0.5) * (end - start) / bins) for index in range(bins)]
    # A muxed narration postroll can make the container extend a few frames
    # beyond the visual stream. Keep the plan truthful by recording the final
    # decoded frame timestamp, rather than asking ffmpeg for a nonexistent one.
    # Do not substitute a frame outside this window: that remains unavailable
    # evidence and is judged as a real missing visual tail below.
    if (
        POLICY["sampling"]["tail_frame_strategy"]
        == "clamp_to_last_decoded_pts_within_window"
        and frame_timestamps
    ):
        last = frame_timestamps[-1]
        if start <= last < end:
            samples = [_round(min(sample, last)) for sample in samples]
    labels = [
        {
            "cue_index": cue["cue_index"],
            "start_seconds": cue["start_seconds"],
            "end_seconds": cue["end_seconds"],
            "text": cue["text"],
        }
        for cue in cues
        if cue["end_seconds"] > start and cue["start_seconds"] < end
    ]
    return {
        "window_id": window_id,
        "kind": kind,
        "start_seconds": start,
        "end_seconds": end,
        "boundary_seconds": _round(boundary),
        "authored_ids": sorted(authored_ids),
        "srt_labels": labels,
        "sample_times_seconds": samples,
    }


def plan_windows(
    storyboard: dict,
    cues: list,
    seams: list,
    duration: float,
    frame_timestamps: list | None = None,
) -> list:
    if not _number(duration) or duration <= 0:
        raise SelfEvalError("candidate duration is not a positive number")
    edge = min(POLICY["edge_window_seconds"], duration)
    half = POLICY["edge_window_seconds"] / 2
    windows = [
        _window("edge-head", "edge_head", 0.0, edge, 0.0, [], cues, frame_timestamps),
        _window(
            "edge-tail",
            "edge_tail",
            max(0.0, duration - POLICY["edge_window_seconds"]),
            duration,
            duration,
            [],
            cues,
            frame_timestamps,
        ),
    ]
    for ordinal, fraction in enumerate(POLICY["midpoint_fractions"], 1):
        centre = duration * fraction
        windows.append(
            _window(
                f"midpoint-{ordinal}",
                "midpoint",
                max(0.0, centre - half),
                min(duration, centre + half),
                centre,
                [],
                cues,
                frame_timestamps,
            )
        )
    for ordinal, (seconds, identifiers) in enumerate(
        authored_boundaries(storyboard, seams, duration), 1
    ):
        windows.append(
            _window(
                f"authored-{ordinal:03d}",
                "authored",
                seconds - POLICY["boundary_before_seconds"],
                min(duration, seconds + POLICY["boundary_after_seconds"]),
                seconds,
                sorted(identifiers),
                cues,
                frame_timestamps,
            )
        )
    windows.sort(key=lambda window: (window["start_seconds"], window["end_seconds"], window["window_id"]))
    return windows


def authored_visual_end(storyboard: dict) -> float | None:
    """The latest visual time the timed storyboard requires the video to cover."""
    ends = []
    scenes = storyboard.get("scenes") if isinstance(storyboard, dict) else None
    if not isinstance(scenes, list):
        return None
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        if _number(scene.get("end_seconds")):
            ends.append(float(scene["end_seconds"]))
        events = scene.get("visual_events")
        if isinstance(events, list):
            for event in events:
                if isinstance(event, dict) and _number(event.get("end_seconds")):
                    ends.append(float(event["end_seconds"]))
    return _round(max(ends)) if ends else None


# --------------------------------------------------------------------------
# evidence capture
# --------------------------------------------------------------------------


class Stager:
    """Private mirror of the attempt directory; refs are taken from these bytes."""

    def __init__(self, root: Path):
        self.root = root
        self.refs = {}

    def _target(self, relative: str) -> Path:
        parts = authority.relative_parts(relative)
        target = self.root.joinpath(*parts)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return target

    def add_bytes(self, project_path: str, relative: str, payload: bytes) -> dict:
        target = self._target(relative)
        with target.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        ref = _ref(project_path, payload)
        self.refs[project_path] = ref
        return ref

    def adopt(self, project_path: str, relative: str, source: Path) -> dict:
        target = self._target(relative)
        os.replace(source, target)
        payload = target.read_bytes()
        ref = _ref(project_path, payload)
        self.refs[project_path] = ref
        return ref


class Budget:
    def __init__(self):
        self.total = 0
        self.exceeded = False

    def admit(self, size: int) -> bool:
        if self.total + size > POLICY["max_total_evidence_bytes"]:
            self.exceeded = True
            return False
        self.total += size
        return True


def _entry_available(path: str, kind: str, ref: dict, window_id) -> dict:
    return {
        "path": path,
        "kind": kind,
        "availability": "available",
        "sha256": ref["sha256"],
        "bytes": ref["bytes"],
        "window_id": window_id,
    }


def _entry_unavailable(kind: str, reason: str, window_id) -> dict:
    return {
        "kind": kind,
        "availability": "unavailable",
        "reason": reason,
        "window_id": window_id,
    }


def capture_window(
    runner: Runner,
    snapshot: Snapshot,
    stager: Stager,
    budget: Budget,
    attempt: int,
    window: dict,
    has_video: bool,
    has_audio: bool,
) -> list:
    """Filmstrip, waveform, labels, composite for one window. Never partial."""
    window_id = window["window_id"]
    base = f"{attempt_dir(attempt)}/windows/{window_id}"
    staged = f"windows/{window_id}"
    scratch = snapshot.work / window_id
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    entries = []
    geometry = POLICY["geometry"]

    labels = authority.canonical_bytes(
        {
            "schema": "haru.render_self_eval_labels.v1",
            "window_id": window_id,
            "kind": window["kind"],
            "start_seconds": window["start_seconds"],
            "end_seconds": window["end_seconds"],
            "boundary_seconds": window["boundary_seconds"],
            "authored_ids": window["authored_ids"],
            "srt_labels": window["srt_labels"],
            "sample_times_seconds": window["sample_times_seconds"],
        }
    )

    def index(kind: str, name: str, source: Path | None, reason: str | None) -> None:
        if source is None:
            entries.append(_entry_unavailable(kind, reason or "capture failed", window_id))
            return
        size = source.stat().st_size
        if size <= 0:
            source.unlink(missing_ok=True)
            entries.append(_entry_unavailable(kind, "tool produced no bytes", window_id))
            return
        if not budget.admit(size):
            # Delete the just-staged artifact rather than keep evidence the
            # index cannot account for; the attempt seals as budget-exceeded.
            source.unlink(missing_ok=True)
            entries.append(
                _entry_unavailable(kind, "evidence_budget_exceeded", window_id)
            )
            return
        ref = stager.adopt(f"{base}/{name}", f"{staged}/{name}", source)
        entries.append(_entry_available(f"{base}/{name}", kind, ref, window_id))

    labels_path = scratch / "labels.json"
    labels_path.write_bytes(labels)
    index("labels", "labels.json", labels_path, None)

    filmstrip = None
    if has_video:
        frames = []
        for ordinal, seconds in enumerate(window["sample_times_seconds"], 1):
            name = f"{window_id}/f-{ordinal:02d}.png"
            completed = runner.run(
                "window_frame",
                [
                    "ffmpeg",
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-ss",
                    f"{seconds:.6f}",
                    "-i",
                    "candidate.mp4",
                    "-map",
                    geometry["video_map"],
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale={geometry['frame_width']}:-2",
                    f"work/{name}",
                ],
            )
            produced = snapshot.work / name
            if completed.returncode == 0 and produced.is_file() and produced.stat().st_size > 0:
                frames.append(produced)
        if len(frames) == POLICY["frames_per_window"]:
            for ordinal, produced in enumerate(frames, 1):
                os.replace(produced, scratch / f"tile-{ordinal:02d}.png")
            completed = runner.run(
                "window_filmstrip",
                [
                    "ffmpeg",
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-framerate",
                    "1",
                    "-start_number",
                    "1",
                    "-i",
                    f"work/{window_id}/tile-%02d.png",
                    "-frames:v",
                    "1",
                    "-vf",
                    f"tile={geometry['filmstrip_tile']}:padding=4:margin=4:color=black",
                    f"work/{window_id}/filmstrip.png",
                ],
            )
            candidate = scratch / "filmstrip.png"
            if completed.returncode == 0 and candidate.is_file():
                filmstrip = candidate
    index(
        "filmstrip",
        "filmstrip.png",
        filmstrip,
        None if has_video else "candidate has no video stream",
    )

    waveform = None
    if has_audio:
        completed = runner.run(
            "window_waveform",
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                f"{window['start_seconds']:.6f}",
                "-t",
                f"{window['end_seconds'] - window['start_seconds']:.6f}",
                "-i",
                "candidate.mp4",
                "-filter_complex",
                f"[{geometry['audio_map']}]"
                f"showwavespic=s={geometry['waveform_size']}:colors=white"
                "[wave]",
                "-map",
                "[wave]",
                "-frames:v",
                "1",
                f"work/{window_id}/waveform.png",
            ],
        )
        candidate = scratch / "waveform.png"
        if completed.returncode == 0 and candidate.is_file():
            waveform = candidate
    index(
        "waveform",
        "waveform.png",
        waveform,
        None if has_audio else "candidate has no audio stream",
    )

    composite = None
    filmstrip_staged = stager.root / staged / "filmstrip.png"
    waveform_staged = stager.root / staged / "waveform.png"
    if filmstrip_staged.is_file() and waveform_staged.is_file():
        completed = runner.run(
            "window_composite",
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                f"stage/{staged}/filmstrip.png",
                "-i",
                f"stage/{staged}/waveform.png",
                "-filter_complex",
                "[0:v]scale={0}:-2[strip];[1:v]scale={0}:{1}[wave];"
                "[strip][wave]{2}=inputs=2".format(
                    geometry["composite_width"],
                    geometry["waveform_size"].split("x")[1],
                    geometry["composite_stack"],
                ),
                "-frames:v",
                "1",
                f"work/{window_id}/composite.png",
            ],
        )
        candidate = scratch / "composite.png"
        if completed.returncode == 0 and candidate.is_file():
            composite = candidate
    index(
        "composite",
        "composite.png",
        composite,
        "filmstrip or waveform is unavailable",
    )
    return entries


# --------------------------------------------------------------------------
# deterministic checks
# --------------------------------------------------------------------------


def _finding(timestamp, boundary_id, category, severity, message) -> dict:
    return {
        "timestamp_seconds": _round(timestamp),
        "boundary_id": boundary_id,
        "category": category,
        "severity": severity,
        "message": message,
    }


def sort_findings(findings: list) -> list:
    return sorted(
        findings,
        key=lambda finding: (
            finding["timestamp_seconds"],
            finding["boundary_id"],
            finding["category"],
            finding["severity"],
            finding["message"],
        ),
    )


def window_frame_facts(stamps: list, window: dict) -> list:
    return [
        _round(stamp)
        for stamp in stamps
        if window["start_seconds"] <= stamp < window["end_seconds"]
    ]


def frame_gap_findings(window: dict, stamps: list, fps: float) -> list:
    limit = POLICY["frame_gap_detection"]["gap_frames"] / fps + POLICY[
        "frame_gap_detection"
    ]["tolerance_seconds"]
    findings = []
    if not stamps:
        return [
            _finding(
                window["start_seconds"],
                window["window_id"],
                "frame_gap",
                "fail",
                "required window has no frame timestamps",
            )
        ]
    for previous, current in zip(stamps, stamps[1:]):
        if current - previous > limit:
            findings.append(
                _finding(
                    current,
                    window["window_id"],
                    "frame_gap",
                    "fail",
                    f"frame gap of {current - previous:.6f}s exceeds {limit:.6f}s",
                )
            )
    return findings


def window_black(intervals: list, window: dict) -> list:
    clipped = []
    for interval in intervals:
        start = max(interval["start_seconds"], window["start_seconds"])
        end = min(interval["end_seconds"], window["end_seconds"])
        if end > start:
            clipped.append(
                {
                    "start_seconds": _round(interval["start_seconds"]),
                    "end_seconds": _round(interval["end_seconds"]),
                    "duration_seconds": _round(
                        interval["end_seconds"] - interval["start_seconds"]
                    ),
                }
            )
    return clipped


def edge_black_covered(intervals: list, window: dict) -> float:
    span = window["end_seconds"] - window["start_seconds"]
    if span <= 0:
        return 0.0
    covered = 0.0
    for interval in intervals:
        start = max(interval["start_seconds"], window["start_seconds"])
        end = min(interval["end_seconds"], window["end_seconds"])
        if end > start:
            covered += end - start
    return covered / span


def audio_jump(raw: Path | None, window: dict) -> dict | None:
    """Largest normalized adjacent jump within +/-20ms of the boundary."""
    if raw is None:
        return None
    policy = POLICY["audio_discontinuity"]
    rate = policy["sample_rate"]
    boundary = window["boundary_seconds"]
    first = max(0, int(round((boundary - policy["window_seconds"]) * rate)) - 1)
    last = int(round((boundary + policy["window_seconds"]) * rate)) + 1
    count = last - first + 1
    if count <= 1:
        return None
    try:
        size = raw.stat().st_size
        with raw.open("rb") as handle:
            handle.seek(first * 2)
            payload = handle.read(count * 2)
    except OSError:
        return None
    if len(payload) < 4 or first * 2 >= size:
        return None
    samples = memoryview(payload).cast("h") if len(payload) % 2 == 0 else None
    if samples is None:
        samples = memoryview(payload[: len(payload) - 1]).cast("h")
    window_start_index = int(round(window["start_seconds"] * rate))
    worst = None
    for offset in range(1, len(samples)):
        value = abs(samples[offset] - samples[offset - 1]) / policy["full_scale"]
        if worst is None or value > worst[0]:
            worst = (value, first + offset)
    if worst is None:
        return None
    value, index = worst
    return {
        "value": _round(value),
        "timestamp_seconds": _round(
            window["start_seconds"] + (index - window_start_index) / rate
        ),
    }


# --------------------------------------------------------------------------
# artifact assembly
# --------------------------------------------------------------------------


def _receipt_check_passes(check) -> bool:
    if check is True:
        return True
    if not isinstance(check, dict):
        return False
    status = check.get("status")
    if ("ok" in check and check["ok"] is not True) or (
        "passed" in check and check["passed"] is not True
    ):
        return False
    return status == "pass" or (
        not status
        and (check.get("ok") is True or check.get("passed") is True)
    )


def tool_provenance(runner: Runner) -> dict:
    return {
        "algorithm": ALGORITHM,
        "ffmpeg_version": runner.version("ffmpeg"),
        "ffprobe_version": runner.version("ffprobe"),
        "inspired_by": dict(INSPIRED_BY),
    }


def remediation(status: str) -> dict:
    return {
        "allowed_repairs": [],
        "automatic_fix_applied": None,
        "required_action": REQUIRED_ACTIONS[status],
    }


def _check(check_id: str, status: str, scope: str, details: str) -> dict:
    return {"check_id": check_id, "status": status, "scope": scope, "details": details}


class Evaluation:
    """One attempt's private, complete artifact set, ready to promote."""

    def __init__(self, project: Path, snapshot: Snapshot, attempt: int):
        self.project = project
        self.snapshot = snapshot
        self.attempt = attempt
        self.stager = Stager(snapshot.stage)
        self.refs = {}
        self.findings = []
        self.checks = []
        self.status = "clean"
        self.evaluation = None
        self.outcome = None

    # -- assembly ------------------------------------------------------
    def build(self) -> None:
        project = self.project
        snapshot = self.snapshot
        attempt = self.attempt
        directory = attempt_dir(attempt)
        runner = Runner(snapshot.directory)
        tool = tool_provenance(runner)

        storyboard = snapshot.json(STORYBOARD)
        validation = snapshot.json(VALIDATION)
        validation_checks = validation.get("checks")
        storyboard_authority_ok = bool(
            storyboard.get("schema") == "haru.storyboard_timed.v1"
            and validation.get("schema") == "haru.storyboard_validation.v1"
            and validation.get("ok") is True
            and isinstance(validation_checks, list)
            and validation_checks
            and all(
                _receipt_check_passes(check)
                for check in validation_checks
            )
        )
        self.checks.append(
            _check(
                "storyboard_authority",
                "pass" if storyboard_authority_ok else "fail",
                "storyboard",
                "timed storyboard validation is current and passes"
                if storyboard_authority_ok
                else "timed storyboard or its validation is invalid",
            )
        )
        if not storyboard_authority_ok:
            self._fail(
                _finding(
                    0.0,
                    "storyboard",
                    "storyboard_validation_invalid",
                    "fail",
                    "timed storyboard validation does not pass",
                )
            )
        marker = render_contract.parse_render_result(snapshot.path(MARKER))
        cues = parse_srt_labels(snapshot.text(SRT))
        media = probe_media(runner)

        fps = storyboard.get("fps")
        if not isinstance(fps, int) or isinstance(fps, bool) or fps <= 0:
            fps = media["fps"]
        if not _number(fps) or fps <= 0:
            raise SelfEvalError("frame rate authority is unavailable")
        fps = float(fps)

        planned_duration = storyboard.get("duration_seconds")
        if not _number(planned_duration) or planned_duration <= 0:
            planned_duration = marker.get("duration_seconds")
        if not _number(planned_duration) or planned_duration <= 0:
            raise SelfEvalError("candidate duration authority is unavailable")
        planned_duration = _round(planned_duration)

        has_video = media["video_streams"] >= 1
        has_audio = media["audio_streams"] >= 1
        stamps = probe_frame_timestamps(runner) if has_video else []
        frames_ok = stamps is not None and all(
            later >= earlier for earlier, later in zip(stamps or [], (stamps or [])[1:])
        )
        windows = plan_windows(
            storyboard,
            cues,
            segment_seams(project),
            planned_duration,
            stamps if frames_ok else None,
        )
        if len(windows) > POLICY["max_windows"]:
            self._fail(
                _finding(
                    0.0,
                    "attempt",
                    "evidence_budget_exceeded",
                    "fail",
                    f"{len(windows)} windows exceeds the {POLICY['max_windows']} cap",
                )
            )
            windows = []

        plan = {
            "schema": PLAN_SCHEMA,
            "project": project.name,
            "attempt_identity": snapshot.identity,
            "inputs": snapshot.entries,
            "boundary_policy": _ref(POLICY_PATH, POLICY_BYTES),
            "duration_seconds": planned_duration,
            "fps": fps,
            "windows": windows,
            "created_at": now_iso(),
        }
        plan_bytes = authority.canonical_bytes(plan)
        plan_ref = self.stager.add_bytes(
            f"{directory}/boundary-plan.json", "boundary-plan.json", plan_bytes
        )

        decoded = decode_clean(runner) if media["probed"] else False
        packets_ok = probe_packet_monotonic(runner) if media["probed"] else False
        black = detect_black(runner, fps) if has_video else []
        raw_audio = extract_audio(runner, snapshot) if has_audio else None

        budget = Budget()
        entries = []
        window_facts = []
        for window in windows:
            entries.extend(
                capture_window(
                    runner,
                    snapshot,
                    self.stager,
                    budget,
                    attempt,
                    window,
                    has_video,
                    has_audio,
                )
            )
            in_window = window_frame_facts(stamps or [], window)
            window_facts.append(
                {
                    "window_id": window["window_id"],
                    "frame_timestamps_seconds": in_window,
                    "black_intervals": window_black(black, window),
                    "audio_jump": audio_jump(raw_audio, window),
                }
            )

        facts = {
            "schema": FACTS_SCHEMA,
            "project": project.name,
            "attempt_identity": snapshot.identity,
            "media": {
                "duration_seconds": (
                    _round(media["duration_seconds"])
                    if _number(media["duration_seconds"])
                    else None
                ),
                "fps": fps,
                "video_streams": media["video_streams"],
                "audio_streams": media["audio_streams"],
                "full_decode_clean": decoded,
                "frame_pts_monotonic": bool(frames_ok),
                "packet_dts_monotonic": bool(packets_ok),
            },
            "windows": window_facts,
        }
        facts_bytes = authority.canonical_bytes(facts)
        commands = {
            "schema": COMMANDS_SCHEMA,
            "project": project.name,
            "attempt_identity": snapshot.identity,
            "commands": runner.commands,
        }
        commands_bytes = authority.canonical_bytes(commands)
        metadata_bytes = len(facts_bytes) + len(commands_bytes)
        while (
            budget.total + metadata_bytes
            > POLICY["max_total_evidence_bytes"]
        ):
            removable = next(
                (
                    index
                    for index in range(len(entries) - 1, -1, -1)
                    if entries[index]["availability"] == "available"
                ),
                None,
            )
            if removable is None:
                raise SelfEvalError(
                    "required commands and facts exceed the evidence budget"
                )
            removed = entries[removable]
            staged_relative = removed["path"][len(directory) + 1 :]
            (self.stager.root / staged_relative).unlink(missing_ok=True)
            budget.total -= removed["bytes"]
            budget.exceeded = True
            entries[removable] = _entry_unavailable(
                removed["kind"],
                "evidence_budget_exceeded",
                removed["window_id"],
            )
        budget.admit(metadata_bytes)
        facts_ref = self.stager.add_bytes(
            f"{directory}/facts.json", "facts.json", facts_bytes
        )
        commands_ref = self.stager.add_bytes(
            f"{directory}/commands.json",
            "commands.json",
            commands_bytes,
        )
        entries.insert(
            0, _entry_available(commands_ref["path"], "commands", commands_ref, None)
        )
        entries.insert(1, _entry_available(facts_ref["path"], "facts", facts_ref, None))
        index = {
            "schema": INDEX_SCHEMA,
            "project": project.name,
            "attempt_identity": snapshot.identity,
            "entries": entries,
            "total_available_bytes": sum(
                entry["bytes"] for entry in entries if entry["availability"] == "available"
            ),
        }
        index_ref = self.stager.add_bytes(
            f"{directory}/evidence-index.json",
            "evidence-index.json",
            authority.canonical_bytes(index),
        )

        self._judge(
            marker=marker,
            media=media,
            planned_duration=planned_duration,
            required_video_end=(
                authored_visual_end(storyboard)
                if POLICY["video_tail_coverage"]["required_end"]
                == "latest_timed_scene_or_visual_event_end"
                else None
            ),
            fps=fps,
            windows=windows,
            window_facts=window_facts,
            black=black,
            stamps=stamps,
            decoded=decoded,
            packets_ok=packets_ok,
            frames_ok=frames_ok,
            entries=entries,
            budget=budget,
        )

        if snapshot.stale():
            raise SelfEvalError("identity inputs changed during evaluation")

        evaluation = {
            "schema": EVALUATION_SCHEMA,
            "project": project.name,
            "attempt": attempt,
            "attempt_identity": snapshot.identity,
            "inputs": snapshot.entries,
            "boundary_policy": _ref(POLICY_PATH, POLICY_BYTES),
            "boundary_plan": plan_ref,
            "commands": commands_ref,
            "facts": facts_ref,
            "evidence_index": index_ref,
            "tool": tool,
            "checks": self.checks,
            "findings": sort_findings(self.findings),
            "status": self.status,
            "remediation": remediation(
                STATUS_FAIL if self.status == "fail" else STATUS_NEEDS_HUMAN
            ),
            "evaluated_at": now_iso(),
        }
        self.evaluation = evaluation
        self.plan = plan
        self.tool = tool
        self.evaluation_ref = self.stager.add_bytes(
            f"{directory}/evaluation.json",
            "evaluation.json",
            authority.canonical_bytes(evaluation),
        )
        self.index_ref = index_ref
        self.plan_ref = plan_ref

    # -- verdicts ------------------------------------------------------
    def _fail(self, finding: dict) -> None:
        self.findings.append(finding)
        self.status = "fail"

    def _judge(self, **facts) -> None:
        marker = facts["marker"]
        media = facts["media"]
        fps = facts["fps"]
        windows = facts["windows"]
        project = self.project
        snapshot = self.snapshot

        candidate_entry = next(
            entry for entry in snapshot.entries if entry["path"] == CANDIDATE
        )
        contract_marker = dict(marker)
        if contract_marker.get("status") in ("render_complete", "PASS"):
            contract_marker["status"] = "pass"
        marker_ok = (
            render_contract.final_mix_passes(
                contract_marker,
                project,
                project / CANDIDATE,
                snapshot.present.get(CANDIDATE),
            )
            and marker.get("bytes") == candidate_entry["bytes"]
        )
        self.checks.append(
            _check(
                "render_marker_current",
                "pass" if marker_ok else "fail",
                "candidate",
                "render marker binds the exact candidate bytes and current inputs"
                if marker_ok
                else "render marker is stale, failed, malformed, or bound elsewhere",
            )
        )
        if not marker_ok:
            self._fail(
                _finding(
                    0.0,
                    "candidate",
                    "render_marker_invalid",
                    "fail",
                    "output/final.mp4.render-result does not certify this candidate",
                )
            )

        profile_ok = media["video_streams"] == 1 and media["audio_streams"] == 1
        self.checks.append(
            _check(
                "stream_profile",
                "pass" if profile_ok else "fail",
                "candidate",
                f"{media['video_streams']} video and {media['audio_streams']} audio streams",
            )
        )
        if not profile_ok:
            self._fail(
                _finding(
                    0.0,
                    "candidate",
                    "stream_profile_invalid",
                    "fail",
                    "candidate must carry exactly one video and one audio stream",
                )
            )

        self.checks.append(
            _check(
                "full_decode",
                "pass" if facts["decoded"] else "fail",
                "candidate",
                "full decode is clean" if facts["decoded"] else "full decode reported errors",
            )
        )
        if not facts["decoded"]:
            self._fail(
                _finding(
                    0.0,
                    "candidate",
                    "decode_failed",
                    "fail",
                    "ffmpeg could not decode the candidate cleanly",
                )
            )

        monotonic = bool(facts["packets_ok"] and facts["frames_ok"])
        self.checks.append(
            _check(
                "timestamp_monotonic",
                "pass" if monotonic else "fail",
                "candidate",
                "packet DTS and frame PTS are finite and monotonic"
                if monotonic
                else "packet or frame timestamps are non-finite or out of order",
            )
        )
        if not monotonic:
            self._fail(
                _finding(
                    0.0,
                    "candidate",
                    "timestamp_nonmonotonic",
                    "fail",
                    "candidate timestamps are not finite and monotonic",
                )
            )

        probed = media["duration_seconds"]
        tolerance = 1.0 / fps + DURATION_TOLERANCE_SECONDS
        duration_ok = _number(probed) and abs(probed - facts["planned_duration"]) <= tolerance
        self.checks.append(
            _check(
                "duration_profile",
                "pass" if duration_ok else "fail",
                "candidate",
                f"probed duration {probed} against authored {facts['planned_duration']}"
                f" within {tolerance:.6f}s",
            )
        )
        if not duration_ok:
            self._fail(
                _finding(
                    0.0,
                    "candidate",
                    "duration_mismatch",
                    "fail",
                    "candidate duration does not match the authored duration",
                )
            )

        required_video_end = facts["required_video_end"]
        tail_policy = POLICY["video_tail_coverage"]
        last_frame_end = (
            facts["stamps"][-1] + 1.0 / fps
            if tail_policy["final_frame_end"] == "last_decoded_pts_plus_one_frame"
            and facts["stamps"]
            else None
        )
        video_tail_ok = (
            _number(required_video_end)
            and _number(last_frame_end)
            and last_frame_end + tail_policy["tolerance_seconds"] >= required_video_end
        )
        self.checks.append(
            _check(
                "video_tail_coverage",
                "pass" if video_tail_ok else "fail",
                "candidate",
                f"decoded video ends at {last_frame_end} against authored visual end "
                f"{required_video_end}"
                if video_tail_ok
                else "decoded video does not cover the authored visual tail",
            )
        )
        if not video_tail_ok:
            self._fail(
                _finding(
                    last_frame_end or 0.0,
                    "candidate",
                    "video_tail_truncated",
                    "fail",
                    "decoded video ends before the authored visual tail",
                )
            )

        gaps = []
        for window, window_fact in zip(windows, facts["window_facts"]):
            gaps.extend(
                frame_gap_findings(window, window_fact["frame_timestamps_seconds"], fps)
            )
        self.checks.append(
            _check(
                "frame_continuity",
                "pass" if not gaps else "fail",
                "windows",
                f"{len(gaps)} frame gaps across {len(windows)} windows",
            )
        )
        for finding in gaps:
            self._fail(finding)

        flashes = []
        for window in windows:
            if window["kind"] != "authored":
                continue
            for interval in facts["black"]:
                if (
                    interval["end_seconds"] > window["start_seconds"]
                    and interval["start_seconds"] < window["end_seconds"]
                ):
                    flashes.append(
                        _finding(
                            interval["start_seconds"],
                            window["window_id"],
                            "black_flash",
                            "fail",
                            "black interval inside an authored boundary window",
                        )
                    )
        self.checks.append(
            _check(
                "authored_black_flash",
                "pass" if not flashes else "fail",
                "authored_windows",
                f"{len(flashes)} black intervals inside authored windows",
            )
        )
        for finding in flashes:
            self._fail(finding)

        edges = []
        for window in windows:
            if window["kind"] not in ("edge_head", "edge_tail"):
                continue
            coverage = edge_black_covered(facts["black"], window)
            if coverage >= POLICY["edge_black_detection"]["coverage_fraction"]:
                edges.append(
                    _finding(
                        window["start_seconds"],
                        window["window_id"],
                        "invalid_edge_black",
                        "fail",
                        f"black covers {coverage:.3f} of the edge window",
                    )
                )
        self.checks.append(
            _check(
                "edge_black",
                "pass" if not edges else "fail",
                "edge_windows",
                f"{len(edges)} structurally black edge windows",
            )
        )
        for finding in edges:
            self._fail(finding)

        jumps = []
        threshold = POLICY["audio_discontinuity"]["jump_threshold"]
        for window, window_fact in zip(windows, facts["window_facts"]):
            if window["kind"] != "authored":
                continue
            jump = window_fact["audio_jump"]
            if jump is not None and jump["value"] >= threshold:
                jumps.append(
                    _finding(
                        jump["timestamp_seconds"],
                        window["window_id"],
                        "audio_discontinuity",
                        "fail",
                        f"normalized adjacent sample jump {jump['value']:.6f}",
                    )
                )
        self.checks.append(
            _check(
                "audio_continuity",
                "pass" if not jumps else "fail",
                "authored_windows",
                f"{len(jumps)} audio discontinuities at authored boundaries",
            )
        )
        for finding in jumps:
            self._fail(finding)

        missing = [
            entry for entry in facts["entries"] if entry["availability"] == "unavailable"
        ]
        self.checks.append(
            _check(
                "evidence_complete",
                "pass" if not missing else "unavailable",
                "windows",
                f"{len(missing)} required artifacts are unavailable",
            )
        )
        for entry in missing:
            self._fail(
                _finding(
                    0.0,
                    entry["window_id"] or "attempt",
                    "evidence_unavailable",
                    "fail",
                    f"{entry['kind']} unavailable: {entry['reason']}",
                )
            )

        budget = facts["budget"]
        self.checks.append(
            _check(
                "evidence_budget",
                "fail" if budget.exceeded else "pass",
                "attempt",
                f"{budget.total} of {POLICY['max_total_evidence_bytes']} bytes indexed",
            )
        )
        if budget.exceeded:
            self._fail(
                _finding(
                    0.0,
                    "attempt",
                    "evidence_budget_exceeded",
                    "fail",
                    "indexed evidence exceeded the policy byte cap",
                )
            )

    # -- sealing -------------------------------------------------------
    def seal_deterministic(self) -> dict:
        outcome = {
            "schema": OUTCOME_SCHEMA,
            "project": self.project.name,
            "attempt": self.attempt,
            "attempt_identity": self.snapshot.identity,
            "source": "deterministic",
            "verdict": "fail",
            "evaluation": self.evaluation_ref,
            "evidence_index": self.index_ref,
            "review": None,
            "findings": sort_findings(self.findings),
            "authority": None,
            "sealed_at": now_iso(),
        }
        self.outcome = outcome
        self.outcome_ref = self.stager.add_bytes(
            f"{attempt_dir(self.attempt)}/outcome.json",
            "outcome.json",
            authority.canonical_bytes(outcome),
        )
        return outcome


# --------------------------------------------------------------------------
# current projection
# --------------------------------------------------------------------------


def build_result(
    project: Path,
    *,
    status: str,
    attempt: int,
    identity: str,
    inputs: list,
    plan_ref: dict,
    evaluation_ref: dict,
    index_ref: dict,
    review_ref,
    outcome_ref,
    tool: dict,
    findings: list,
) -> dict:
    return {
        "schema": RESULT_SCHEMA,
        "project": project.name,
        "status": status,
        "attempt": attempt,
        "max_attempts": MAX_ATTEMPTS,
        "attempt_identity": identity,
        "inputs": inputs,
        "boundary_policy": _ref(POLICY_PATH, POLICY_BYTES),
        "boundary_plan": plan_ref,
        "evaluation": evaluation_ref,
        "evidence_index": index_ref,
        "review": review_ref,
        "outcome": outcome_ref,
        "tool": tool,
        "findings": sort_findings(findings),
        "verdict": status,
        "remediation": remediation(status),
        "next_action": NEXT_ACTIONS[status],
        "updated_at": now_iso(),
    }


# --------------------------------------------------------------------------
# promotion: held directory descriptors, renameat, nothing caller-supplied
# --------------------------------------------------------------------------


def _mkdir_at(dir_fd: int, name: str, mode: int = 0o755) -> None:
    with contextlib.suppress(FileExistsError):
        os.mkdir(name, mode, dir_fd=dir_fd)


def _ensure_dirs(project: Path, relative: str, project_fd: int | None = None) -> None:
    """Create components below a held project descriptor, never a fresh path."""
    parts = authority.relative_parts(relative)
    descriptor = (
        authority.open_project_fd(project)
        if project_fd is None
        else os.dup(project_fd)
    )
    try:
        for part in parts:
            _mkdir_at(descriptor, part)
            child = authority.open_dir_at(descriptor, part)
            os.close(descriptor)
            descriptor = child
    finally:
        os.close(descriptor)


def _copy_tree(source: Path, dir_fd: int) -> None:
    for entry in sorted(os.listdir(source)):
        child = source / entry
        if child.is_dir() and not child.is_symlink():
            _mkdir_at(dir_fd, entry)
            nested = authority.open_dir_at(dir_fd, entry)
            try:
                _copy_tree(child, nested)
                authority.fsync_dir(nested)
            finally:
                os.close(nested)
            continue
        payload = child.read_bytes()
        authority.write_leaf_at(dir_fd, entry, payload, exclusive=True)


class Promotion:
    """Stage inside the project, then renameat into a nonexistent attempt dir."""

    def __init__(
        self, project: Path, transaction_id: str, project_fd: int | None = None
    ):
        self.project = project
        self.transaction_id = transaction_id
        held = (
            authority.open_project_fd(project)
            if project_fd is None
            else os.dup(project_fd)
        )
        try:
            _ensure_dirs(project, ATTEMPTS_PATH, held)
            self.root_fd = authority.open_relative_dir_fd(held, ROOT)
            self.attempts_fd = authority.open_relative_dir_fd(held, ATTEMPTS_PATH)
        finally:
            os.close(held)

    def close(self) -> None:
        for name in ("attempts_fd", "root_fd"):
            fd = getattr(self, name, -1)
            if fd is not None and fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
                setattr(self, name, -1)

    def promote_attempt(self, attempt: int, staged: Path) -> None:
        target = f"attempt-{attempt:02d}"
        if not any(staged.iterdir()):
            return
        if self._merge_existing(target, staged):
            return
        staging = f".staging-{self.transaction_id[:16]}"
        with contextlib.suppress(FileNotFoundError):
            shutil.rmtree(
                Path(self.project) / ATTEMPTS_PATH / staging, ignore_errors=True
            )
        _mkdir_at(self.attempts_fd, staging, 0o700)
        staging_fd = authority.open_dir_at(self.attempts_fd, staging)
        try:
            _copy_tree(staged, staging_fd)
            authority.fsync_dir(staging_fd)
        finally:
            os.close(staging_fd)
        os.rename(
            staging,
            target,
            src_dir_fd=self.attempts_fd,
            dst_dir_fd=self.attempts_fd,
        )
        authority.fsync_dir(self.attempts_fd)

    def _merge_existing(self, target: str, staged: Path) -> bool:
        """Append optional create-once files to an immutable attempt.

        The attempt directory itself is promoted into a nonexistent name during
        evaluation. Review and outcome are later transitions, so they are added
        fd-relatively under the already-held attempt directory. Existing equal
        bytes are an idempotent replay; differing bytes are never overwritten.
        """
        try:
            existing_fd = authority.open_dir_at(self.attempts_fd, target)
        except AuthorityError:
            return False

        def merge(source: Path, directory_fd: int) -> None:
            for entry in sorted(os.listdir(source)):
                child = source / entry
                if child.is_dir():
                    _mkdir_at(directory_fd, entry, 0o700)
                    child_fd = authority.open_dir_at(directory_fd, entry)
                    try:
                        merge(child, child_fd)
                        authority.fsync_dir(child_fd)
                    finally:
                        os.close(child_fd)
                    continue
                payload = child.read_bytes()
                try:
                    fd = authority.open_file_at(directory_fd, entry)
                except AuthorityError:
                    authority.write_leaf_at(
                        directory_fd, entry, payload, exclusive=True
                    )
                    continue
                try:
                    if authority.read_fd(fd) != payload:
                        raise SelfEvalError(
                            f"attempt artifact differs from sealed history: {entry}"
                        )
                finally:
                    os.close(fd)

        try:
            merge(staged, existing_fd)
            authority.fsync_dir(existing_fd)
            return True
        finally:
            os.close(existing_fd)

    def write_current(self, name: str, payload: bytes) -> None:
        authority.atomic_leaf_at(self.root_fd, name, payload)

    def remove_current(self, name: str) -> None:
        with contextlib.suppress(FileNotFoundError, OSError):
            os.unlink(name, dir_fd=self.root_fd)
        authority.fsync_dir(self.root_fd)


# --------------------------------------------------------------------------
# retirement barrier and quarantine
# --------------------------------------------------------------------------


def retirement_plan(project: Path, project_fd: int | None = None) -> dict:
    entries = []
    for source in RETIREMENT_SOURCES:
        fd = None
        try:
            fd = (
                authority.open_project_file_fd(project, source)
                if project_fd is None
                else authority.open_relative_file_fd(project_fd, source)
            )
            payload = authority.read_fd(fd)
        except AuthorityError:
            continue
        finally:
            if fd is not None:
                os.close(fd)
        digest = authority.hashlib.sha256(payload).hexdigest()
        size = len(payload)
        basename = source.rsplit("/", 1)[1]
        if source.startswith("publish/"):
            try:
                approval = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                approval = {}
            identifier = (
                approval.get("approval_intent_sha256")
                if isinstance(approval, dict)
                else None
            )
            if not authority.is_sha256(identifier):
                identifier = digest
            destination = f"{APPROVAL_HISTORY_DIR}/{identifier}.pre-self-eval.json"
        else:
            destination = f"{RETIRED_VISUAL_DIR}/{digest}-{basename}"
        entries.append(
            {
                "source": source,
                "destination": destination,
                "sha256": digest,
                "bytes": size,
            }
        )
    return {"entries": entries}


def _move_within_project(
    project: Path,
    source: str,
    destination: str,
    project_fd: int | None = None,
) -> None:
    source_parts = authority.relative_parts(source)
    destination_parts = authority.relative_parts(destination)
    _ensure_dirs(
        project, str(Path(*destination_parts[:-1])), project_fd
    )
    source_relative = (
        str(Path(*source_parts[:-1])) if len(source_parts) > 1 else ""
    )
    source_fd = (
        authority.open_project_dir_fd(project, source_relative)
        if project_fd is None
        else authority.open_relative_dir_fd(project_fd, source_relative)
    )
    try:
        destination_relative = str(Path(*destination_parts[:-1]))
        destination_fd = (
            authority.open_project_dir_fd(project, destination_relative)
            if project_fd is None
            else authority.open_relative_dir_fd(
                project_fd, destination_relative
            )
        )
        try:
            try:
                os.link(
                    source_parts[-1],
                    destination_parts[-1],
                    src_dir_fd=source_fd,
                    dst_dir_fd=destination_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                # Crash after the link but before unlink: only accept the exact
                # same inode bytes. A preplaced destination is never replaced.
                source_stat = os.stat(
                    source_parts[-1], dir_fd=source_fd, follow_symlinks=False
                )
                destination_stat = os.stat(
                    destination_parts[-1],
                    dir_fd=destination_fd,
                    follow_symlinks=False,
                )
                if (
                    source_stat.st_dev != destination_stat.st_dev
                    or source_stat.st_ino != destination_stat.st_ino
                ):
                    raise SelfEvalError(
                        f"retirement destination already exists: {destination}"
                    )
            authority.fsync_dir(destination_fd)
            os.unlink(source_parts[-1], dir_fd=source_fd)
        finally:
            os.close(destination_fd)
        authority.fsync_dir(source_fd)
    finally:
        os.close(source_fd)


def retire(
    project: Path,
    plan: dict,
    transaction_id: str,
    project_fd: int | None = None,
) -> None:
    """Idempotently retire pre-self-eval HVP-21/HVP-28 authority."""
    if project_fd is not None:
        _require_held_root(project, project_fd)
    entries = plan.get("entries") or []
    groups = {"visual": [], "approval": []}
    for entry in entries:
        source = entry["source"]
        try:
            fd = (
                authority.open_project_file_fd(project, source)
                if project_fd is None
                else authority.open_relative_file_fd(project_fd, source)
            )
        except AuthorityError:
            fd = None
        if fd is not None:
            try:
                digest, size = authority.hash_fd(fd)
            finally:
                os.close(fd)
            if digest != entry["sha256"] or size != entry["bytes"]:
                raise SelfEvalError(
                    f"retirement source changed since the plan: {source}"
                )
            _move_within_project(
                project, source, entry["destination"], project_fd
            )
        else:
            try:
                destination_fd = (
                    authority.open_project_file_fd(
                        project, entry["destination"]
                    )
                    if project_fd is None
                    else authority.open_relative_file_fd(
                        project_fd, entry["destination"]
                    )
                )
            except AuthorityError as error:
                raise SelfEvalError(
                    f"retirement source and destination are unavailable: {source}"
                ) from error
            try:
                digest, size = authority.hash_fd(destination_fd)
            finally:
                os.close(destination_fd)
            if digest != entry["sha256"] or size != entry["bytes"]:
                raise SelfEvalError(
                    f"retirement destination changed since the plan: "
                    f"{entry['destination']}"
                )
        groups[
            "approval" if source.startswith("publish/") else "visual"
        ].append(entry)

    for group, tombstone in (
        ("visual", VISUAL_TOMBSTONE),
        ("approval", APPROVAL_TOMBSTONE),
    ):
        if not groups[group]:
            continue
        payload = authority.canonical_bytes(
            {
                "schema": RETIREMENT_SCHEMA,
                "project": project.name,
                "transaction_id": transaction_id,
                "entries": groups[group],
                "retired_at": now_iso(),
            }
        )
        parts = authority.relative_parts(tombstone)
        tombstone_relative = str(Path(*parts[:-1]))
        _ensure_dirs(project, tombstone_relative, project_fd)
        dir_fd = (
            authority.open_project_dir_fd(project, tombstone_relative)
            if project_fd is None
            else authority.open_relative_dir_fd(
                project_fd, tombstone_relative
            )
        )


        try:
            try:
                authority.write_leaf_at(
                    dir_fd, parts[-1], payload, exclusive=True
                )
            except FileExistsError:
                # The same prepared transaction may retry after its moves. The
                # create-once tombstone already records the cutover.
                pass
        finally:
            os.close(dir_fd)
def _optional_project_ref(project: Path, relative: str):
    parts = authority.relative_parts(relative)
    root_fd = authority.open_project_fd(project)
    try:
        parent = authority.open_relative_dir_fd(
            root_fd, str(Path(*parts[:-1]))
        )
    finally:
        os.close(root_fd)
    try:
        try:
            info = os.stat(
                parts[-1], dir_fd=parent, follow_symlinks=False
            )
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise SelfEvalError(
                f"optional project receipt is not a regular file: {relative}"
            )
    finally:
        os.close(parent)
    return _project_ref(project, relative)


def quarantine(
    project: Path,
    transaction_id: str,
    refs: list,
    project_fd: int | None = None,
) -> None:
    target = f"{ORPHANS_PATH}/{transaction_id}"
    _ensure_dirs(project, target, project_fd)
    for ref in refs:
        try:
            descriptor = (
                authority.open_project_file_fd(project, ref["path"])
                if project_fd is None
                else authority.open_relative_file_fd(project_fd, ref["path"])
            )
            os.close(descriptor)
        except AuthorityError:
            continue
        digest = authority.hashlib.sha256(ref["path"].encode("utf-8")).hexdigest()[:12]
        basename = ref["path"].rsplit("/", 1)[-1]
        with contextlib.suppress(SelfEvalError, AuthorityError, OSError):
            _move_within_project(
                project,
                ref["path"],
                f"{target}/{digest}-{basename}",
                project_fd,
            )


# --------------------------------------------------------------------------
# current-state validation
# --------------------------------------------------------------------------


def _project_ref(project: Path, relative: str) -> dict:
    fd = authority.open_project_file_fd(project, relative)
    try:
        digest, size = authority.hash_fd(fd)
    finally:
        os.close(fd)
    return {"path": relative, "sha256": digest, "bytes": size}


def _load_ref(project: Path, ref: dict) -> dict:
    if not authority.is_ref(ref):
        raise SelfEvalError("reference is malformed")
    fd = authority.open_project_file_fd(project, ref["path"])
    try:
        payload = authority.read_fd(fd)
    finally:
        os.close(fd)
    if (
        authority.hashlib.sha256(payload).hexdigest() != ref["sha256"]
        or len(payload) != ref["bytes"]
    ):
        raise SelfEvalError(f"reference does not match its bytes: {ref['path']}")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SelfEvalError(f"referenced artifact is malformed: {ref['path']}") from error
    if not isinstance(value, dict):
        raise SelfEvalError(f"referenced artifact is not an object: {ref['path']}")
    return value


def _valid_findings(findings) -> bool:
    if not isinstance(findings, list):
        return False
    for finding in findings:
        if (
            not isinstance(finding, dict)
            or set(finding) != FINDING_KEYS
            or not _number(finding.get("timestamp_seconds"))
            or finding["timestamp_seconds"] < 0
            or not all(
                isinstance(finding.get(key), str) and finding[key]
                for key in ("boundary_id", "category", "severity", "message")
            )
        ):
            return False
    return findings == sort_findings(findings)


def _valid_inputs(inputs) -> bool:
    if not isinstance(inputs, list) or len(inputs) != len(INPUT_ORDER):
        return False
    for entry, relative in zip(inputs, INPUT_ORDER):
        if not isinstance(entry, dict) or entry.get("path") != relative:
            return False
        if entry.get("state") == "absent":
            if set(entry) != {"path", "state"} or relative in ALWAYS_PRESENT:
                return False
        elif entry.get("state") == "present":
            if (
                set(entry) != {"path", "state", "sha256", "bytes"}
                or not authority.is_sha256(entry.get("sha256"))
                or not isinstance(entry.get("bytes"), int)
                or isinstance(entry["bytes"], bool)
                or entry["bytes"] <= 0
            ):
                return False
        else:
            return False
    return True


def _inputs_current(project: Path, inputs: list) -> bool:
    for entry in inputs:
        try:
            actual = _project_ref(project, entry["path"])
        except AuthorityError:
            if entry["state"] == "absent":
                continue
            return False
        if entry["state"] == "absent":
            return False
        if actual["sha256"] != entry["sha256"] or actual["bytes"] != entry["bytes"]:
            return False
    return True
def _valid_plan(plan: dict, result: dict) -> bool:
    windows = plan.get("windows") if isinstance(plan, dict) else None
    if (
        set(plan) != PLAN_KEYS
        or plan.get("schema") != PLAN_SCHEMA
        or plan.get("project") != result["project"]
        or plan.get("attempt_identity") != result["attempt_identity"]
        or plan.get("inputs") != result["inputs"]
        or plan.get("boundary_policy") != result["boundary_policy"]
        or not _number(plan.get("duration_seconds"))
        or plan["duration_seconds"] <= 0
        or not _number(plan.get("fps"))
        or plan["fps"] <= 0
        or not isinstance(plan.get("created_at"), str)
        or not isinstance(windows, list)
        or len(windows) > POLICY["max_windows"]
    ):
        return False
    keys = []
    for window in windows:
        if (
            not isinstance(window, dict)
            or set(window) != WINDOW_KEYS
            or not isinstance(window.get("window_id"), str)
            or not window["window_id"]
            or not isinstance(window.get("kind"), str)
            or not window["kind"]
            or not all(
                _number(window.get(name))
                for name in ("start_seconds", "end_seconds", "boundary_seconds")
            )
            or window["start_seconds"] < 0
            or window["end_seconds"] <= window["start_seconds"]
            or window["end_seconds"] > plan["duration_seconds"] + 0.000001
            or not isinstance(window.get("authored_ids"), list)
            or window["authored_ids"] != sorted(set(window["authored_ids"]))
            or not all(
                isinstance(identifier, str) and identifier
                for identifier in window["authored_ids"]
            )
            or not isinstance(window.get("sample_times_seconds"), list)
            or len(window["sample_times_seconds"]) != POLICY["frames_per_window"]
            or not all(_number(value) for value in window["sample_times_seconds"])
            or window["sample_times_seconds"]
            != sorted(window["sample_times_seconds"])
            or any(
                value < window["start_seconds"] or value >= window["end_seconds"]
                for value in window["sample_times_seconds"]
            )
            or not isinstance(window.get("srt_labels"), list)
        ):
            return False
        for label in window["srt_labels"]:
            if (
                not isinstance(label, dict)
                or set(label)
                != {"cue_index", "start_seconds", "end_seconds", "text"}
                or not isinstance(label.get("cue_index"), int)
                or isinstance(label["cue_index"], bool)
                or not _number(label.get("start_seconds"))
                or not _number(label.get("end_seconds"))
                or label["end_seconds"] <= label["start_seconds"]
                or not isinstance(label.get("text"), str)
            ):
                return False
        keys.append(
            (
                window["start_seconds"],
                window["end_seconds"],
                window["window_id"],
            )
        )
    return keys == sorted(keys) and len(keys) == len(set(keys))


def _valid_commands(project: Path, ref: dict, result: dict) -> bool:
    try:
        value = _load_ref(project, ref)
    except (SelfEvalError, AuthorityError):
        return False
    commands = value.get("commands") if isinstance(value, dict) else None
    if (
        set(value) != COMMANDS_KEYS
        or value.get("schema") != COMMANDS_SCHEMA
        or value.get("project") != result["project"]
        or value.get("attempt_identity") != result["attempt_identity"]
        or not isinstance(commands, list)
    ):
        return False
    for ordinal, command in enumerate(commands, 1):
        if (
            not isinstance(command, dict)
            or set(command) != COMMAND_KEYS
            or command.get("ordinal") != ordinal
            or not isinstance(command.get("purpose"), str)
            or not command["purpose"]
            or not isinstance(command.get("argv"), list)
            or not command["argv"]
            or not all(isinstance(argument, str) for argument in command["argv"])
            or command["argv"][0] not in ("ffmpeg", "ffprobe")
            or not isinstance(command.get("exit_code"), int)
            or isinstance(command["exit_code"], bool)
            or not authority.is_sha256(command.get("stdout_sha256"))
            or not authority.is_sha256(command.get("stderr_sha256"))
            or not isinstance(command.get("started_at"), str)
            or not isinstance(command.get("finished_at"), str)
        ):
            return False
    return True


def _valid_facts(project: Path, ref: dict, result: dict) -> bool:
    try:
        value = _load_ref(project, ref)
    except (SelfEvalError, AuthorityError):
        return False
    media = value.get("media") if isinstance(value, dict) else None
    windows = value.get("windows") if isinstance(value, dict) else None
    if (
        set(value) != FACTS_KEYS
        or value.get("schema") != FACTS_SCHEMA
        or value.get("project") != result["project"]
        or value.get("attempt_identity") != result["attempt_identity"]
        or not isinstance(media, dict)
        or set(media) != MEDIA_KEYS
        or (
            media["duration_seconds"] is not None
            and (
                not _number(media["duration_seconds"])
                or media["duration_seconds"] <= 0
            )
        )
        or not _number(media.get("fps"))
        or media["fps"] <= 0
        or any(
            not isinstance(media.get(name), int)
            or isinstance(media[name], bool)
            or media[name] < 0
            for name in ("video_streams", "audio_streams")
        )
        or any(
            not isinstance(media.get(name), bool)
            for name in (
                "full_decode_clean",
                "frame_pts_monotonic",
                "packet_dts_monotonic",
            )
        )
        or not isinstance(windows, list)
    ):
        return False
    seen = set()
    for window in windows:
        if (
            not isinstance(window, dict)
            or set(window) != WINDOW_FACT_KEYS
            or not isinstance(window.get("window_id"), str)
            or not window["window_id"]
            or window["window_id"] in seen
            or not isinstance(window.get("frame_timestamps_seconds"), list)
            or not all(_number(stamp) for stamp in window["frame_timestamps_seconds"])
            or window["frame_timestamps_seconds"]
            != sorted(window["frame_timestamps_seconds"])
            or not isinstance(window.get("black_intervals"), list)
        ):
            return False
        seen.add(window["window_id"])
        for interval in window["black_intervals"]:
            if (
                not isinstance(interval, dict)
                or set(interval) != BLACK_INTERVAL_KEYS
                or not all(_number(interval.get(name)) for name in BLACK_INTERVAL_KEYS)
                or interval["end_seconds"] < interval["start_seconds"]
            ):
                return False
        jump = window["audio_jump"]
        if jump is not None and (
            not isinstance(jump, dict)
            or set(jump) != AUDIO_JUMP_KEYS
            or not all(_number(jump.get(name)) for name in AUDIO_JUMP_KEYS)
        ):
            return False
    return True


def _valid_index(project: Path, value: dict, result: dict) -> bool:
    entries = value.get("entries") if isinstance(value, dict) else None
    if (
        set(value) != INDEX_KEYS
        or value.get("schema") != INDEX_SCHEMA
        or value.get("project") != result["project"]
        or value.get("attempt_identity") != result["attempt_identity"]
        or not isinstance(entries, list)
        or not isinstance(value.get("total_available_bytes"), int)
        or isinstance(value["total_available_bytes"], bool)
        or value["total_available_bytes"] < 0
    ):
        return False
    paths = set()
    total = 0
    for entry in entries:
        if not isinstance(entry, dict):
            return False
        if entry.get("availability") == "available":
            if (
                set(entry) != AVAILABLE_ENTRY_KEYS
                or not authority.is_ref(
                    {
                        "path": entry.get("path"),
                        "sha256": entry.get("sha256"),
                        "bytes": entry.get("bytes"),
                    }
                )
                or entry["path"] == result["evidence_index"]["path"]
                or entry["path"] in paths
                or not isinstance(entry.get("kind"), str)
                or not entry["kind"]
                or (
                    entry.get("window_id") is not None
                    and not isinstance(entry["window_id"], str)
                )
            ):
                return False
            paths.add(entry["path"])
            total += entry["bytes"]
            try:
                actual = _project_ref(project, entry["path"])
            except AuthorityError:
                return False
            if (
                actual["sha256"] != entry["sha256"]
                or actual["bytes"] != entry["bytes"]
            ):
                return False
        elif entry.get("availability") == "unavailable":
            if (
                set(entry) != UNAVAILABLE_ENTRY_KEYS
                or not isinstance(entry.get("kind"), str)
                or not entry["kind"]
                or not isinstance(entry.get("reason"), str)
                or not entry["reason"]
                or (
                    entry.get("window_id") is not None
                    and not isinstance(entry["window_id"], str)
                )
            ):
                return False
        else:
            return False
    return (
        total == value["total_available_bytes"]
        and total <= POLICY["max_total_evidence_bytes"]
    )


def _validate_immutable_history(project: Path, chain) -> None:
    for record in chain.records:
        for immutable_ref in record["project_refs"]:
            if not immutable_ref["path"].startswith(f"{ATTEMPTS_PATH}/"):
                continue
            try:
                actual_ref = _project_ref(project, immutable_ref["path"])
            except AuthorityError as error:
                raise SelfEvalError(
                    "immutable attempt history is missing"
                ) from error
            if actual_ref != immutable_ref:
                raise SelfEvalError(
                    "immutable attempt history differs from the protected ledger"
                )


def _failed_outcome_count(project: Path, before_attempt: int) -> int:
    failed = 0
    for ordinal in range(1, before_attempt):
        try:
            outcome_ref = _project_ref(
                project, f"{attempt_dir(ordinal)}/outcome.json"
            )
            outcome = _load_ref(project, outcome_ref)
        except (SelfEvalError, AuthorityError):
            continue
        if (
            outcome.get("schema") == OUTCOME_SCHEMA
            and outcome.get("attempt") == ordinal
            and outcome.get("verdict") == "fail"
        ):
            failed += 1
    return failed


def _validated_current(project: Path, chain=None) -> dict:
    """One read of the current projection; everything else derives from it."""
    if chain is None:
        with authority.locked(project, project.name) as namespace:
            chain = authority.validated_chain(namespace)
    fd = authority.open_project_file_fd(project, RESULT_PATH)
    try:
        payload = authority.read_fd(fd)
    finally:
        os.close(fd)
    ref = {
        "path": RESULT_PATH,
        "sha256": authority.hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }
    try:
        result = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SelfEvalError("current self-eval result is malformed") from error
    if (
        not isinstance(result, dict)
        or set(result) != RESULT_KEYS
        or result.get("schema") != RESULT_SCHEMA
        or result.get("project") != project.name
        or result.get("status") not in STATUSES
        or result.get("verdict") != result.get("status")
        or not isinstance(result.get("attempt"), int)
        or isinstance(result["attempt"], bool)
        or not 1 <= result["attempt"] <= MAX_ATTEMPTS
        or result.get("max_attempts") != MAX_ATTEMPTS
        or not authority.is_sha256(result.get("attempt_identity"))
        or not _valid_inputs(result.get("inputs"))
        or not _valid_findings(result.get("findings"))
        or result.get("next_action") != NEXT_ACTIONS[result["status"]]
        or result.get("remediation") != remediation(result["status"])
        or not isinstance(result.get("tool"), dict)
        or result["tool"].get("algorithm") != ALGORITHM
        or result["tool"].get("inspired_by") != INSPIRED_BY
        or set(result["tool"]) != {"algorithm", "ffmpeg_version", "ffprobe_version", "inspired_by"}
        or not isinstance(result.get("updated_at"), str)
    ):
        raise SelfEvalError("current self-eval result is invalid")

    status = result["status"]
    attempt = result["attempt"]
    identity = result["attempt_identity"]
    if result["boundary_policy"] != _ref(POLICY_PATH, POLICY_BYTES):
        raise SelfEvalError("current boundary policy reference is not the repo policy")
    policy = _load_ref(project, result["boundary_policy"])
    if authority.canonical_bytes(policy) != POLICY_BYTES:
        raise SelfEvalError("boundary policy bytes are not the repo policy")

    evaluation = _load_ref(project, result["evaluation"])
    checks = evaluation.get("checks") if isinstance(evaluation, dict) else None
    if (
        set(evaluation) != EVALUATION_KEYS
        or evaluation.get("schema") != EVALUATION_SCHEMA
        or evaluation.get("project") != project.name
        or evaluation.get("attempt") != attempt
        or evaluation.get("attempt_identity") != identity
        or evaluation.get("inputs") != result["inputs"]
        or evaluation.get("evidence_index") != result["evidence_index"]
        or evaluation.get("tool") != result["tool"]
        or evaluation.get("status") not in ("clean", "fail")
        or not _valid_findings(evaluation.get("findings"))
        or evaluation.get("boundary_policy") != result["boundary_policy"]
        or evaluation.get("boundary_plan")
        != {
            **result["boundary_plan"],
            "path": f"{attempt_dir(attempt)}/boundary-plan.json",
        }
        or evaluation.get("remediation")
        != remediation(
            STATUS_FAIL
            if evaluation.get("status") == "fail"
            else STATUS_NEEDS_HUMAN
        )
        or not isinstance(evaluation.get("evaluated_at"), str)
        or not isinstance(checks, list)
        or not checks
        or any(
            not isinstance(check, dict)
            or set(check) != CHECK_KEYS
            or not isinstance(check.get("check_id"), str)
            or not check["check_id"]
            or check.get("status") not in ("pass", "fail", "unavailable")
            or not isinstance(check.get("scope"), str)
            or not check["scope"]
            or not isinstance(check.get("details"), str)
            for check in checks
        )
        or not _valid_commands(project, evaluation.get("commands"), result)
        or not _valid_facts(project, evaluation.get("facts"), result)
    ):
        raise SelfEvalError("current evaluation does not bind this result")
    if result["evaluation"]["path"] != f"{attempt_dir(attempt)}/evaluation.json":
        raise SelfEvalError("current evaluation is not this attempt's evaluation")
    if result["evidence_index"]["path"] != f"{attempt_dir(attempt)}/evidence-index.json":
        raise SelfEvalError("current evidence index is not this attempt's index")
    plan = _load_ref(project, result["boundary_plan"])
    attempt_plan = _load_ref(project, evaluation["boundary_plan"])
    if (
        result["boundary_plan"]["path"] != CURRENT_PLAN_PATH
        or plan != attempt_plan
        or not _valid_plan(plan, result)
    ):
        raise SelfEvalError("current boundary plan does not bind this result")
    index = _load_ref(project, result["evidence_index"])
    if not _valid_index(project, index, result):
        raise SelfEvalError("current evidence index does not bind this result")

    deterministic_fail = evaluation["status"] == "fail"
    if status == STATUS_NEEDS_HUMAN and deterministic_fail:
        raise SelfEvalError("a deterministic failure can never be pending review")
    if status in (STATUS_PASS,) and deterministic_fail:
        raise SelfEvalError("a deterministic failure can never pass")

    review = result["review"]
    outcome = result["outcome"]
    current_review = _optional_project_ref(project, CURRENT_REVIEW_PATH)
    if status == STATUS_NEEDS_HUMAN:
        if review is not None or outcome is not None or current_review is not None:
            raise SelfEvalError(
                "a pending state carries no review, outcome, or review projection"
            )
    else:
        if outcome is None:
            raise SelfEvalError("a terminal state must carry a sealed outcome")
        sealed = _load_ref(project, outcome)
        if outcome["path"] != f"{attempt_dir(attempt)}/outcome.json":
            raise SelfEvalError("outcome is not this attempt's outcome")
        expected_verdict = "pass" if status == STATUS_PASS else "fail"
        if (
            set(sealed) != OUTCOME_KEYS
            or sealed.get("schema") != OUTCOME_SCHEMA
            or sealed.get("project") != project.name
            or sealed.get("attempt") != attempt
            or sealed.get("attempt_identity") != identity
            or sealed.get("verdict") != expected_verdict
            or sealed.get("evaluation") != result["evaluation"]
            or sealed.get("evidence_index") != result["evidence_index"]
            or not _valid_findings(sealed.get("findings"))
            or sealed.get("source") not in ("deterministic", "vision", "human_fallback")
        ):
            raise SelfEvalError("sealed outcome does not bind this result")
        if sealed["source"] == "deterministic":
            if (
                sealed["review"] is not None
                or sealed["authority"] is not None
                or review is not None
                or current_review is not None
                or expected_verdict != "fail"
                or not deterministic_fail
            ):
                raise SelfEvalError("deterministic outcome shape is invalid")
        else:
            if review is None or sealed["review"] != review:
                raise SelfEvalError("reviewed outcome must bind its review")
            recorded = _load_ref(project, review)
            if review["path"] != f"{attempt_dir(attempt)}/review.json":
                raise SelfEvalError("review is not this attempt's review")
            if (
                current_review is None
                or current_review["sha256"] != review["sha256"]
                or current_review["bytes"] != review["bytes"]
            ):
                raise SelfEvalError(
                    "current review projection does not equal the attempt review"
                )
            if not _valid_review(project, recorded, result, sealed):
                raise SelfEvalError("recorded review does not bind this result")
            if sealed["authority"] != recorded["authority"]:
                raise SelfEvalError("outcome authority must equal the review authority")
            if not authority.verify_consumed(
                recorded["authority"], project.name, project
            ):
                raise SelfEvalError(
                    "review authority is not a consumed attestation"
                )

    states = {
        entry["path"]: entry["state"] for entry in result["inputs"]
    }
    try:
        authority_presence_current = (
            states[EDITORIAL]
            == ("present" if editorial_required(project) else "absent")
            and states[ASSEMBLY]
            == ("present" if assembly_required(project) else "absent")
        )
    except (SelfEvalError, OSError, ValueError):
        authority_presence_current = False
    if not authority_presence_current:
        raise SelfEvalError(
            "current self-eval inputs do not match lane or segment authority"
        )

    # Immutable history, and no fourth attempt.
    _validate_immutable_history(project, chain)
    for ordinal in range(1, MAX_ATTEMPTS + 1):
        try:
            fd = authority.open_project_file_fd(
                project, f"{attempt_dir(ordinal)}/evaluation.json"
            )
        except AuthorityError:
            if ordinal <= attempt:
                raise SelfEvalError(f"attempt {ordinal} history is missing")
            continue
        else:
            os.close(fd)
    failed_outcomes = 0
    for ordinal in range(1, MAX_ATTEMPTS + 1):
        try:
            outcome_ref = _project_ref(
                project, f"{attempt_dir(ordinal)}/outcome.json"
            )
        except AuthorityError:
            continue
        outcome_value = _load_ref(project, outcome_ref)
        if (
            outcome_value.get("schema") == OUTCOME_SCHEMA
            and outcome_value.get("attempt") == ordinal
            and outcome_value.get("verdict") == "fail"
        ):
            failed_outcomes += 1
    if status == STATUS_HUMAN_INTERVENTION and (
        attempt != MAX_ATTEMPTS or failed_outcomes != MAX_ATTEMPTS
    ):
        raise SelfEvalError(
            "human intervention requires exactly three sealed failed attempts"
        )
    if status == STATUS_FAIL and attempt >= MAX_ATTEMPTS:
        raise SelfEvalError("attempt three cannot report a plain failure")

    if not _inputs_current(project, result["inputs"]):
        raise SelfEvalError("current self-eval result no longer describes the project")
    marker = render_contract.parse_render_result(project / MARKER)
    candidate = next(entry for entry in result["inputs"] if entry["path"] == CANDIDATE)
    if (
        not render_contract.final_mix_passes(
            marker, project, project / CANDIDATE, candidate["sha256"]
        )
        or marker.get("bytes") != candidate["bytes"]
    ):
        raise SelfEvalError("candidate render marker no longer certifies this candidate")

    anchor = _anchor(project, chain)
    if anchor is None:
        raise SelfEvalError("no external ledger anchor for this project")
    if (
        anchor["attempt"] != attempt
        or anchor["attempt_identity"] != identity
        or TRANSITION_STATUS[anchor["transition"]]
        != (STATUS_FAIL if status == STATUS_HUMAN_INTERVENTION else status)
        or ref not in anchor["project_refs"]
    ):
        raise SelfEvalError("current self-eval result is not the anchored state")
    if status == STATUS_HUMAN_INTERVENTION and anchor["transition"] not in (
        "evaluate_fail",
        "vision_fail",
        "human_fail",
    ):
        raise SelfEvalError("human intervention must anchor a sealed failure")
    if anchor["transition"] == "vision_unavailable":
        unavailable_ref = next(
            (
                project_ref
                for project_ref in anchor["project_refs"]
                if project_ref["path"]
                == f"{attempt_dir(attempt)}/vision-unavailable.json"
            ),
            None,
        )
        if unavailable_ref is None:
            raise SelfEvalError(
                "vision-unavailable transition does not anchor its receipt"
            )
        unavailable = _load_ref(project, unavailable_ref)
        if not _valid_unavailable(project, unavailable, result):
            raise SelfEvalError(
                "vision-unavailable receipt is not backed by protected authority"
            )
    result["__ref__"] = ref
    return result


def _valid_review(project: Path, recorded: dict, result: dict, sealed: dict) -> bool:
    attempt = result["attempt"]
    if (
        not isinstance(recorded, dict)
        or set(recorded) != REVIEW_KEYS
        or recorded.get("schema") != REVIEW_SCHEMA
        or recorded.get("project") != project.name
        or recorded.get("attempt") != attempt
        or recorded.get("attempt_identity") != result["attempt_identity"]
        or recorded.get("evaluation") != result["evaluation"]
        or recorded.get("evidence_index") != result["evidence_index"]
        or recorded.get("reviewer_kind") not in ("vision", "human_fallback")
        or recorded.get("verdict") != sealed["verdict"]
        or recorded.get("reviewer_kind") != sealed["source"]
        or not isinstance(recorded.get("reviewed_by"), str)
        or not recorded["reviewed_by"]
        or not all(
            isinstance(recorded.get(key), str)
            for key in ("provider", "model", "capability", "notes")
        )
        or not _valid_findings(recorded.get("findings"))
        or not authority.is_sha256(recorded.get("review_intent_sha256"))
    ):
        return False
    if recorded["reviewer_kind"] == "vision":
        if recorded["vision_unavailable"] is not None or not all(
            recorded[key] for key in ("provider", "model", "capability")
        ):
            return False
        expected = _vision_intent(project, result, recorded)
    else:
        if (
            recorded["capability"] != HUMAN_CAPABILITY
            or not authority.is_ref(recorded["vision_unavailable"])
        ):
            return False
        try:
            unavailable = _load_ref(project, recorded["vision_unavailable"])
        except (SelfEvalError, AuthorityError):
            return False
        if not _valid_unavailable(project, unavailable, result):
            return False
        expected = _human_intent(
            project, result, recorded, recorded["vision_unavailable"]
        )
    if authority.canonical_digest(expected) != recorded["review_intent_sha256"]:
        return False
    return bool(
        (recorded["verdict"] == "pass" and not recorded["findings"])
        or (recorded["verdict"] == "fail" and recorded["findings"])
    )


def _valid_unavailable(project: Path, value: dict, result: dict) -> bool:
    if (
        not isinstance(value, dict)
        or set(value) != UNAVAILABLE_KEYS
        or value.get("schema") != UNAVAILABLE_SCHEMA
        or value.get("project") != project.name
        or value.get("attempt") != result["attempt"]
        or value.get("attempt_identity") != result["attempt_identity"]
        or value.get("evaluation") != result["evaluation"]
        or value.get("evidence_index") != result["evidence_index"]
        or value.get("result") != "unavailable"
        or not isinstance(value.get("reviewed_by"), str)
        or not value["reviewed_by"]
        or not all(
            isinstance(value.get(key), str)
            for key in ("provider", "model", "capability", "notes")
        )
        or not all(value[key] for key in ("provider", "model", "capability"))
        or not isinstance(value.get("recorded_at"), str)
        or not isinstance(value.get("authority"), dict)
        or not authority.is_vision_attestation_schema(
            value["authority"].get("schema")
        )
        or not authority.verify_consumed(value["authority"], project.name, project)
    ):
        return False
    intent = _vision_intent(
        project,
        result,
        {
            "reviewer_kind": "vision",
            "verdict": "unavailable",
            "reviewed_by": value["reviewed_by"],
            "provider": value["provider"],
            "model": value["model"],
            "capability": value["capability"],
            "notes": value["notes"],
            "findings": [],
        },
    )
    return (
        authority.canonical_digest(intent)
        == value["authority"].get("review_intent_sha256")
    )


def _anchor(project: Path, chain=None):
    if chain is not None:
        return chain.latest
    return authority.latest_record(project, project.name)


def validate_current(project) -> dict:
    """The validated current `haru.render_self_eval.v1`, or a refusal."""
    directory = _project_dir(project)
    try:
        with authority.locked(directory, directory.name) as namespace:
            chain = authority.validated_chain(namespace)
            result = _validated_current(directory, chain)
    except AuthorityError as error:
        raise SelfEvalError(str(error)) from error
    return {key: value for key, value in result.items() if key != "__ref__"}


def current_pass(project):
    """{"ref": ..., "result": ...} for a validated current pass, else None."""
    try:
        directory = _project_dir(project)
        with authority.locked(directory, directory.name) as namespace:
            chain = authority.validated_chain(namespace)
            result = _validated_current(directory, chain)
    except (SelfEvalError, AuthorityError, OSError):
        return None
    if result["status"] != STATUS_PASS:
        return None
    ref = result.pop("__ref__")
    return {"ref": ref, "result": result}


def current_pass_ref(project):
    passed = current_pass(project)
    return passed["ref"] if passed else None


def current_status(project):
    try:
        directory = _project_dir(project)
        with authority.locked(directory, directory.name) as namespace:
            chain = authority.validated_chain(namespace)
            return _validated_current(directory, chain)["status"]
    except (SelfEvalError, AuthorityError, OSError):
        return None


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------


def _history(chain) -> dict:
    attempts = {}
    for record in chain.records:
        attempts.setdefault(record["attempt"], record["attempt_identity"])
    return attempts


def _recover_authority(project: Path, pending: dict):
    """Recover authority only from exact promoted bytes plus the consumed leaf."""
    suffix = (
        "/vision-unavailable.json"
        if pending["transition"] == "vision_unavailable"
        else "/review.json"
    )
    receipt_ref = next(
        (
            ref
            for ref in pending["expected_project_refs"]
            if ref["path"].endswith(suffix)
        ),
        None,
    )
    if receipt_ref is None:
        return None
    try:
        receipt = _load_ref(project, receipt_ref)
    except (SelfEvalError, AuthorityError):
        return None
    consumed = receipt.get("authority")
    expected_schema = (
        authority.VISION_ATTESTATION_SCHEMA
        if pending["transition"].startswith("vision_")
        else authority.HUMAN_ATTESTATION_SCHEMA
    )
    if (
        receipt.get("attempt") != pending["attempt"]
        or receipt.get("attempt_identity") != pending["attempt_identity"]
        or not isinstance(consumed, dict)
        or not authority.same_attestation_family(
            consumed.get("schema"), expected_schema
        )
        or not authority.verify_consumed(consumed, project.name, project)
    ):
        return None
    if (
        receipt.get("schema") == REVIEW_SCHEMA
        and receipt.get("review_intent_sha256")
        != consumed.get("review_intent_sha256")
    ):
        return None
    return consumed


def _open_transaction(project: Path, project_fd: int | None = None):
    namespace = authority.Namespace(project, project.name)
    try:
        chain = authority.validated_chain(namespace)
        chain = authority.recover(
            namespace,
            chain,
            retire=lambda plan, transaction_id: retire(
                project, plan, transaction_id, project_fd
            ),
            quarantine=lambda identifier, refs: quarantine(
                project, identifier, refs, project_fd
            ),
            recovered_authority=lambda pending: _recover_authority(
                project, pending
            ),
        )
        return namespace, chain
    except BaseException:
        namespace.close()
        raise


def _promote(
    project: Path,
    transaction_id: str,
    attempt: int,
    staged: Path,
    current: dict,
    remove: tuple = (),
    project_fd: int | None = None,
) -> None:
    if project_fd is not None:
        _require_held_root(project, project_fd)
    promotion = Promotion(project, transaction_id, project_fd)
    try:
        promotion.promote_attempt(attempt, staged)
        for name in remove:
            promotion.remove_current(name)
        for name, payload in current.items():
            promotion.write_current(name, payload)
        if project_fd is not None:
            _require_held_root(project, project_fd)
    finally:
        promotion.close()


def evaluate(project) -> dict:
    """Evaluate the current candidate, or return the current state unchanged."""
    directory = _project_dir(project)
    with Snapshot(directory) as snapshot:
        namespace, chain = _open_transaction(directory, snapshot.root_fd)
        try:
            _validate_immutable_history(directory, chain)
            history = _history(chain)
            if chain.latest is not None and chain.latest["attempt_identity"] == snapshot.identity:
                # Unchanged inputs never create an attempt and never rewrite the
                # projection: byte-identical output is exactly what "reused" means.
                current = _validated_current(directory, chain)
                return {
                    key: value
                    for key, value in current.items()
                    if key != "__ref__"
                }
            if snapshot.identity in history.values():
                raise SelfEvalError(
                    "this candidate already consumed an earlier attempt ordinal"
                )
            attempt = (max(history) if history else 0) + 1
            if attempt > MAX_ATTEMPTS:
                raise SelfEvalError(
                    "three self-evaluation attempts are sealed; no fourth attempt exists"
                )

            evaluation = Evaluation(directory, snapshot, attempt)
            evaluation.build()
            deterministic_fail = evaluation.status == "fail"
            if (
                deterministic_fail
                and attempt == MAX_ATTEMPTS
                and _failed_outcome_count(directory, attempt)
                != MAX_ATTEMPTS - 1
            ):
                raise SelfEvalError(
                    "attempt three can fail only after two sealed failed attempts"
                )
            outcome_ref = None
            findings = list(evaluation.findings)
            if deterministic_fail:
                evaluation.seal_deterministic()
                outcome_ref = evaluation.outcome_ref
                status = (
                    STATUS_HUMAN_INTERVENTION
                    if attempt == MAX_ATTEMPTS
                    else STATUS_FAIL
                )
                transition = "evaluate_fail"
            else:
                status = STATUS_NEEDS_HUMAN
                transition = "evaluate_pending"

            plan_bytes = authority.canonical_bytes(evaluation.plan)
            current_plan_ref = _ref(CURRENT_PLAN_PATH, plan_bytes)
            result = build_result(
                directory,
                status=status,
                attempt=attempt,
                identity=snapshot.identity,
                inputs=snapshot.entries,
                plan_ref=current_plan_ref,
                evaluation_ref=evaluation.evaluation_ref,
                index_ref=evaluation.index_ref,
                review_ref=None,
                outcome_ref=outcome_ref,
                tool=evaluation.tool,
                findings=findings,
            )
            result_bytes = authority.canonical_bytes(result)
            current = {
                "boundary-policy.json": POLICY_BYTES,
                "boundary-plan.json": plan_bytes,
                "render-self-eval.json": result_bytes,
            }
            refs = list(evaluation.stager.refs.values()) + [
                _ref(POLICY_PATH, POLICY_BYTES),
                current_plan_ref,
                _ref(RESULT_PATH, result_bytes),
            ]
            pending = authority.prepare(
                namespace,
                chain,
                transition=transition,
                attempt=attempt,
                attempt_identity=snapshot.identity,
                expected_refs=refs,
                retirement=(
                    retirement_plan(directory, snapshot.root_fd)
                    if chain.generation == 0
                    else None
                ),
            )
            authority.commit(
                namespace,
                chain,
                pending,
                authority=None,
                retire=lambda plan: retire(
                    directory,
                    plan,
                    pending["transaction_id"],
                    snapshot.root_fd,
                ),
                promote=lambda: _promote(
                    directory,
                    pending["transaction_id"],
                    attempt,
                    snapshot.stage,
                    current,
                    remove=("review.json",),
                    project_fd=snapshot.root_fd,
                ),
            )
        finally:
            namespace.close()
    return validate_current(directory)


def _validate_review_input(review_input) -> dict:
    if not isinstance(review_input, dict) or set(review_input) != REVIEW_INPUT_KEYS:
        raise SelfEvalError("review input keys are invalid")
    kind = review_input["reviewer_kind"]
    verdict = review_input["verdict"]
    if kind not in ("vision", "human_fallback"):
        raise SelfEvalError("reviewer_kind must be vision or human_fallback")
    allowed = ("pass", "fail", "unavailable") if kind == "vision" else ("pass", "fail")
    if verdict not in allowed:
        raise SelfEvalError("verdict is not allowed for this reviewer kind")
    for key in ("reviewed_by", "provider", "model", "capability", "notes"):
        if not isinstance(review_input[key], str):
            raise SelfEvalError(f"{key} must be a string")
    if not review_input["reviewed_by"]:
        raise SelfEvalError("reviewed_by is required")
    if kind == "vision":
        if not all(review_input[key] for key in ("provider", "model", "capability")):
            raise SelfEvalError("vision review provenance is required")
        if review_input["capability"] == HUMAN_CAPABILITY:
            raise SelfEvalError("vision review cannot claim the human capability")
    elif review_input["capability"] != HUMAN_CAPABILITY:
        raise SelfEvalError(f"human fallback capability must be {HUMAN_CAPABILITY}")
    if not _valid_findings(review_input["findings"]):
        raise SelfEvalError("findings are malformed or unsorted")
    if verdict == "pass" and review_input["findings"]:
        raise SelfEvalError("a pass verdict carries no findings")
    if verdict == "fail" and not review_input["findings"]:
        raise SelfEvalError("a fail verdict must cite at least one finding")
    if verdict == "unavailable" and review_input["findings"]:
        raise SelfEvalError("an unavailable verdict carries no findings")
    return dict(review_input)


def _intent_common(project: Path, result: dict, review_input: dict) -> dict:
    return {
        "project_id": project.name,
        "attempt": result["attempt"],
        "attempt_identity": result["attempt_identity"],
        "evaluation": result["evaluation"],
        "evidence_index": result["evidence_index"],
        "reviewer_kind": review_input["reviewer_kind"],
        "verdict": review_input["verdict"],
        "reviewed_by": review_input["reviewed_by"],
        "provider": review_input["provider"],
        "model": review_input["model"],
        "capability": review_input["capability"],
        "notes": review_input["notes"],
        "findings": sort_findings(review_input["findings"]),
    }


def _vision_intent(project: Path, result: dict, review_input: dict) -> dict:
    return {"schema": VISION_INTENT_SCHEMA, **_intent_common(project, result, review_input)}


def _human_intent(project: Path, result: dict, review_input: dict, unavailable_ref: dict) -> dict:
    return {
        "schema": HUMAN_INTENT_SCHEMA,
        "vision_unavailable": unavailable_ref,
        **_intent_common(project, result, review_input),
    }


def record_review(project, review_input, attestation_ref) -> dict:
    """Record one reviewer verdict, backed by a one-time external attestation."""
    directory = _project_dir(project)
    review_input = _validate_review_input(review_input)
    if not isinstance(attestation_ref, str) or not authority.ATTESTATION_REF.fullmatch(
        attestation_ref
    ):
        raise SelfEvalError("attestation_ref is malformed")
    operation_root_fd = authority.open_project_fd(directory)
    try:
        namespace, chain = _open_transaction(directory, operation_root_fd)
    except BaseException:
        os.close(operation_root_fd)
        raise
    try:
        result = _validated_current(directory, chain)
        result.pop("__ref__")
        attempt = result["attempt"]
        identity = result["attempt_identity"]
        directory_path = attempt_dir(attempt)
        kind = review_input["reviewer_kind"]
        verdict = review_input["verdict"]

        unavailable_ref = None
        existing_unavailable = None
        try:
            existing_unavailable = _project_ref(
                directory, f"{directory_path}/vision-unavailable.json"
            )
        except AuthorityError:
            pass

        if kind == "human_fallback":
            if existing_unavailable is None:
                raise SelfEvalError(
                    "human fallback requires the current attempt's "
                    "vision-unavailable receipt"
                )
            unavailable_ref = existing_unavailable
            recorded_unavailable = _load_ref(directory, unavailable_ref)
            if not _valid_unavailable(directory, recorded_unavailable, result):
                raise SelfEvalError(
                    "human fallback requires the current attempt's "
                    "vision-unavailable receipt"
                )
            intent = _human_intent(directory, result, review_input, unavailable_ref)
            schema = authority.HUMAN_ATTESTATION_SCHEMA
        else:
            intent = _vision_intent(directory, result, review_input)
            schema = authority.VISION_ATTESTATION_SCHEMA
        intent_sha256 = authority.canonical_digest(intent)
        if (
            verdict == "fail"
            and attempt == MAX_ATTEMPTS
            and _failed_outcome_count(directory, attempt)
            != MAX_ATTEMPTS - 1
        ):
            raise SelfEvalError(
                "attempt three can fail only after two sealed failed attempts"
            )

        if _replay(directory, attempt, kind, verdict, intent_sha256):
            current = _validated_current(directory, chain)
            return {
                key: value
                for key, value in current.items()
                if key != "__ref__"
            }
        if result["status"] != STATUS_NEEDS_HUMAN:
            raise SelfEvalError(
                "only a current clean evaluation pending review can be reviewed"
            )
        if kind == "vision" and existing_unavailable is not None:
            raise SelfEvalError(
                "vision is unavailable for this attempt; only human_fallback may seal it"
            )

        consumed = authority.consume(
            chain, schema, attestation_ref, directory.name, intent_sha256
        )

        stager = Stager(Path(tempfile.mkdtemp(prefix=".haru-self-eval-review-")))
        try:
            refs = []
            removals = ()
            if verdict == "unavailable":
                receipt = {
                    "schema": UNAVAILABLE_SCHEMA,
                    "project": directory.name,
                    "attempt": attempt,
                    "attempt_identity": identity,
                    "evaluation": result["evaluation"],
                    "evidence_index": result["evidence_index"],
                    "reviewed_by": review_input["reviewed_by"],
                    "provider": review_input["provider"],
                    "model": review_input["model"],
                    "capability": review_input["capability"],
                    "result": "unavailable",
                    "notes": review_input["notes"],
                    "recorded_at": now_iso(),
                    "authority": consumed,
                }
                stager.add_bytes(
                    f"{directory_path}/vision-unavailable.json",
                    "vision-unavailable.json",
                    authority.canonical_bytes(receipt),
                )
                status = STATUS_NEEDS_HUMAN
                transition = "vision_unavailable"
                review_ref = None
                outcome_ref = None
                findings = result["findings"]
                removals = ("review.json",)
            else:
                review = {
                    "schema": REVIEW_SCHEMA,
                    "project": directory.name,
                    "attempt": attempt,
                    "attempt_identity": identity,
                    "evaluation": result["evaluation"],
                    "evidence_index": result["evidence_index"],
                    "vision_unavailable": unavailable_ref,
                    "reviewer_kind": kind,
                    "verdict": verdict,
                    "reviewed_by": review_input["reviewed_by"],
                    "provider": review_input["provider"],
                    "model": review_input["model"],
                    "capability": review_input["capability"],
                    "notes": review_input["notes"],
                    "findings": sort_findings(review_input["findings"]),
                    "review_intent_sha256": intent_sha256,
                    "authority": consumed,
                    "reviewed_at": now_iso(),
                }
                review_bytes = authority.canonical_bytes(review)
                review_ref = stager.add_bytes(
                    f"{directory_path}/review.json", "review.json", review_bytes
                )
                outcome = {
                    "schema": OUTCOME_SCHEMA,
                    "project": directory.name,
                    "attempt": attempt,
                    "attempt_identity": identity,
                    "source": kind,
                    "verdict": verdict,
                    "evaluation": result["evaluation"],
                    "evidence_index": result["evidence_index"],
                    "review": review_ref,
                    "findings": sort_findings(
                        result["findings"] + review_input["findings"]
                    ),
                    "authority": consumed,
                    "sealed_at": now_iso(),
                }
                outcome_ref = stager.add_bytes(
                    f"{directory_path}/outcome.json",
                    "outcome.json",
                    authority.canonical_bytes(outcome),
                )
                findings = outcome["findings"]
                if verdict == "pass":
                    status = STATUS_PASS
                elif attempt == MAX_ATTEMPTS:
                    status = STATUS_HUMAN_INTERVENTION
                else:
                    status = STATUS_FAIL
                transition = f"{'vision' if kind == 'vision' else 'human'}_{verdict}"

            current_review_bytes = (
                None if review_ref is None else (stager.root / "review.json").read_bytes()
            )
            result_object = build_result(
                directory,
                status=status,
                attempt=attempt,
                identity=identity,
                inputs=result["inputs"],
                plan_ref=result["boundary_plan"],
                evaluation_ref=result["evaluation"],
                index_ref=result["evidence_index"],
                review_ref=review_ref,
                outcome_ref=outcome_ref,
                tool=result["tool"],
                findings=findings,
            )
            result_bytes = authority.canonical_bytes(result_object)
            current = {"render-self-eval.json": result_bytes}
            refs = list(stager.refs.values()) + [_ref(RESULT_PATH, result_bytes)]
            if current_review_bytes is not None:
                current["review.json"] = current_review_bytes
                refs.append(_ref(CURRENT_REVIEW_PATH, current_review_bytes))
            pending = authority.prepare(
                namespace,
                chain,
                transition=transition,
                attempt=attempt,
                attempt_identity=identity,
                expected_refs=refs,
                retirement=(
                    retirement_plan(directory, operation_root_fd)
                    if chain.generation == 0
                    else None
                ),
            )
            authority.commit(
                namespace,
                chain,
                pending,
                authority=consumed,
                retire=lambda plan: retire(
                    directory,
                    plan,
                    pending["transaction_id"],
                    operation_root_fd,
                ),
                promote=lambda: _promote(
                    directory,
                    pending["transaction_id"],
                    attempt,
                    stager.root,
                    current,
                    remove=removals,
                    project_fd=operation_root_fd,
                ),
            )
        finally:
            shutil.rmtree(stager.root, ignore_errors=True)
    finally:
        if operation_root_fd >= 0:
            os.close(operation_root_fd)
        namespace.close()
    return validate_current(directory)


def _replay(project: Path, attempt: int, kind: str, verdict: str, intent_sha256: str) -> bool:
    """Has this exact review intent already been recorded? Never consume twice."""
    name = (
        "vision-unavailable.json"
        if verdict == "unavailable"
        else "review.json"
    )
    relative = f"{attempt_dir(attempt)}/{name}"
    try:
        fd = authority.open_project_file_fd(project, relative)
    except AuthorityError:
        return False
    try:
        payload = authority.read_fd(fd)
    finally:
        os.close(fd)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(value, dict):
        return False
    if verdict == "unavailable":
        recorded = value.get("authority")
        return bool(
            isinstance(recorded, dict)
            and recorded.get("review_intent_sha256") == intent_sha256
        )
    return bool(
        value.get("review_intent_sha256") == intent_sha256
        and value.get("reviewer_kind") == kind
        and value.get("verdict") == verdict
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _emit(project: Path) -> int:
    """stdout is the exact current bytes: a caller can hash what it read."""
    fd = authority.open_project_file_fd(project, RESULT_PATH)
    try:
        payload = authority.read_fd(fd)
    finally:
        os.close(fd)
    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("evaluate", "validate"):
        sub = subparsers.add_parser(name)
        sub.add_argument("project", type=Path)
    review = subparsers.add_parser("review")
    review.add_argument("project", type=Path)
    review.add_argument("--review-json", required=True)
    review.add_argument("--attestation-ref", required=True)
    args = parser.parse_args(argv)

    try:
        project = _project_dir(args.project)
        if args.command == "evaluate":
            evaluate(project)
        elif args.command == "validate":
            validate_current(project)
        else:
            try:
                review_input = json.loads(args.review_json)
            except json.JSONDecodeError as error:
                raise SelfEvalError("review JSON is malformed") from error
            record_review(project, review_input, args.attestation_ref)
        return _emit(project)
    except (SelfEvalError, AuthorityError, OSError) as error:
        sys.stdout.write(
            json.dumps({"error": str(error)}, ensure_ascii=False, sort_keys=True) + "\n"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
