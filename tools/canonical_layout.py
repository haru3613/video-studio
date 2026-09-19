#!/usr/bin/env python3
"""The canonical project layout, declared once.

Before this file, the layout `agent_status.py` gates on existed in exactly two
places: scattered through the gate checks themselves, and re-implemented inside
a test fixture. Nothing told a *producer* what to make. `hvp create` produced an
empty directory, the gate then demanded a dozen specific files, and the only
things that had ever satisfied it were fixtures written by reading the gate.
That is not verification, it is a mirror.

So: one declaration, consumed by three callers.

* ``scaffold`` writes the skeleton, so a new project starts in the shape the
  gate expects.
* ``checklist`` renders it for a human or an agent, so "what is still missing"
  is answerable without reading gate source.
* The test suite builds a project *from this declaration* and asserts the gate
  reports zero blockers. If the two ever drift, that test fails — which is the
  only thing keeping this file honest.

Placeholders are deliberately **invalid**, never plausible-but-empty. A stub
that parses is worse than an absent file: a stale `pipeline_status.json`
claiming `ready_with_warnings` was found in a real project, from a schema
version that no longer exists, and anything trusting it would have read a
finished video that was nine gates from done.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import editorial_contract  # noqa: E402

SCHEMA = "haru.canonical_layout.v1"

# Marker every scaffolded placeholder carries. The gate must reject any artifact
# still containing it; `scaffold` is a starting point, never a way to go green.
TODO_MARKER = "HVP_TODO_REPLACE_ME"

# The project-side runtime compatibility block. `hvp create` writes it, the Rust
# runtime decides which versions it is able to serve, and this module only
# refuses to let a producer drop it: a project contract promoted without one
# leaves the project unservable, and no runtime ever infers it back.
PROJECT_RUNTIME_CONTRACT_SCHEMA = "haru.project_runtime_contract.v1"
PROJECT_RUNTIME_CONTRACT_FIELDS = ("runtime", "evaluator", "artifact")

# The render self-evaluation lane (HVP-33), declared here for the same reason
# everything else in this file is: `agent_status` gates on it, the checklist
# reports it, and the docs quote it. `tools/render_self_eval.py` owns the
# behaviour and re-declares `ROOT`/`RESULT_PATH` for its own callers; this
# module cannot import it (that module imports this one), so the two are kept
# honest by an equality assertion in tools/test_canonical_layout.py rather than
# by hope.
#
# Only the current projection is a declared artifact. The attempt history is
# create-once immutable state whose directory names are allocated at runtime, so
# there is nothing static to declare and nothing a producer should ever write:
# the paths below exist so every reader names them identically.
RENDER_SELF_EVAL_ROOT = "quality-review/render-self-eval"
RENDER_SELF_EVAL_RESULT = f"{RENDER_SELF_EVAL_ROOT}/render-self-eval.json"
RENDER_SELF_EVAL_BOUNDARY_POLICY = f"{RENDER_SELF_EVAL_ROOT}/boundary-policy.json"
RENDER_SELF_EVAL_BOUNDARY_PLAN = f"{RENDER_SELF_EVAL_ROOT}/boundary-plan.json"
RENDER_SELF_EVAL_REVIEW = f"{RENDER_SELF_EVAL_ROOT}/review.json"
RENDER_SELF_EVAL_ATTEMPTS = f"{RENDER_SELF_EVAL_ROOT}/attempts"
RENDER_SELF_EVAL_ORPHANS = f"{RENDER_SELF_EVAL_ROOT}/orphans"

# Where the first anchored self-eval transaction retires the pre-cutover HVP-21
# v1 and HVP-28 v2 receipts. Retirement preserves audit history and is never
# reversed, so these are history roots, not artifacts a project must hold.
RETIRED_PRE_SELF_EVAL_ROOT = "quality-review/visual-sampling/retired-pre-self-eval"
RETIRED_PRE_SELF_EVAL_TOMBSTONE = f"{RETIRED_PRE_SELF_EVAL_ROOT}/retirement.json"
APPROVAL_HISTORY_ROOT = "publish/approval-history"
APPROVAL_CUTOVER_TOMBSTONE = f"{APPROVAL_HISTORY_ROOT}/self-eval-cutover.json"

YOUTUBE_CHANNEL_ID = re.compile(r"^UC[A-Za-z0-9_-]{22}$")


def validate_publish_target(value):
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("youtube_channel_id"), str)
        or not YOUTUBE_CHANNEL_ID.fullmatch(value["youtube_channel_id"])
    ):
        raise ValueError(
            "project contract must carry a valid publish_target.youtube_channel_id"
        )
    return value


def validate_runtime_contract(value):
    if (
        not isinstance(value, dict)
        or value.get("schema") != PROJECT_RUNTIME_CONTRACT_SCHEMA
    ):
        raise ValueError(
            "project contract must carry the runtime_contract block written by "
            "hvp create; it is never inferred"
        )
    for field in PROJECT_RUNTIME_CONTRACT_FIELDS:
        named = value.get(field)
        if not isinstance(named, str) or not named.strip():
            raise ValueError(f"runtime_contract {field} version is required")
    return value


def direct_path(path, project):
    """Resolve `path` only if no component inside `project` is a symlink.

    The single definition of "this path is really inside this project".
    `agent_status.project_file` delegates here rather than keeping its own copy:
    the checklist once used plain `is_file()` while the gate walked for
    symlinks, so a symlinked `script-proposal.md` read `[x] done` in the
    checklist while the gate blocked it as missing. Two implementations of one
    rule is exactly the drift this module exists to prevent, and the module
    that declares the layout is the one that owns the rule.

    Lives here, not in agent_status, because agent_status already imports this
    module — the other direction is a cycle.
    """
    try:
        project_path = Path(os.path.abspath(project))
        candidate = Path(os.path.abspath(path))
        relative = candidate.relative_to(project_path)
        current = project_path
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                return None
        if not candidate.resolve().is_relative_to(project_path.resolve()):
            return None
        return candidate
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def open_direct_directory_fd(path, project):
    """Open a project directory component-by-component without following symlinks."""
    candidate = direct_path(path, project)
    if candidate is None or not candidate.is_dir():
        raise OSError("directory must be direct and contained by the project")
    project_path = Path(os.path.abspath(project))
    relative = candidate.relative_to(project_path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(project_path, flags)
    try:
        for part in relative.parts:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class Artifact:
    __slots__ = ("path", "gate", "purpose", "kind", "producer", "profiles")

    def __init__(
        self,
        path: str,
        gate: str,
        purpose: str,
        kind: str = "text",
        producer: str = "scripts/hvp-produce",
        profiles=(),
    ):
        self.path = path
        self.gate = gate
        self.purpose = purpose
        self.kind = kind  # text | json | dir | binary
        self.producer = producer
        self.profiles = tuple(profiles)

    def as_dict(self):
        return {
            "path": self.path,
            "gate": self.gate,
            "purpose": self.purpose,
            "kind": self.kind,
            "producer": self.producer,
            "profiles": list(self.profiles),
        }


# Ordered roughly by production sequence, so the checklist reads as a workflow.
ARTIFACTS = [
    Artifact(
        "project-contract.json",
        "selection",
        "Versioned lane contract. Social-issue longform additionally "
        "requires the immutable .hvp/selection.json receipt.",
        kind="json",
    ),
    Artifact(
        "script-proposal.md",
        "proposal",
        "The approved script. Written after Harvey picks the topic.",
    ),
    Artifact(
        "sources.md",
        "sources",
        "Human-readable source list backing every factual claim.",
    ),
    Artifact(
        "claims.json",
        "sources",
        "Machine-checkable claims with source_type/source_name/source_url. "
        "A claim whose source needs manual locking blocks the gate.",
        kind="json",
    ),
    Artifact(
        "narration-final.mp3",
        "tts",
        "Final narration audio. Draft takes do not satisfy this.",
        kind="binary",
    ),
    Artifact("narration-final.srt", "tts", "Subtitles for the final narration."),
    Artifact(
        "narration-final.mp3.pron-ok.json",
        "tts",
        "Pronunciation check. Must carry the audio's sha256 and an empty "
        "warnings list; any warning fails the gate.",
        kind="json",
    ),
    Artifact(
        "storyboard-final-timed.json",
        "storyboard",
        "Scene timings and visual structure used for render and visual QA.",
        kind="json",
    ),
    Artifact(
        "storyboard-final-timed-validation.json",
        "storyboard",
        "Timed storyboard validation output.",
        kind="json",
    ),
    Artifact(
        "editorial-contract.json",
        "editorial",
        "Cue-complete Mina A-roll, B-roll/PIP, and Motion Canvas shot contract.",
        kind="json",
        profiles=("mina_longform.v1",),
    ),
    Artifact(
        "quality-review/editorial-preview/review.json",
        "editorial_preview",
        "Digest-bound pass receipt for the 60-90 second editorial preview.",
        kind="json",
        profiles=("mina_longform.v1",),
    ),
    Artifact(
        "output/",
        "render",
        "Rendered video and cover live here.",
        kind="dir",
        producer="scripts/render-project + scripts/hvp-produce output/cover.png",
    ),
    Artifact(
        RENDER_SELF_EVAL_RESULT,
        "render_self_eval",
        "Current digest-bound render self-evaluation state for the exact "
        "output/final.mp4 bytes. Sealed by the runner and anchored in the "
        "external authority ledger; never hand-written.",
        kind="json",
        producer="scripts/render-self-eval evaluate",
    ),
    Artifact(
        "quality-review/",
        "qa",
        "One directory per reviewed render, holding prep.json and review.json.",
        kind="dir",
        producer="scripts/hvp-produce + scripts/visual-qa-sample",
    ),
    Artifact(
        "issue_brief.md",
        "duration",
        "Carries the duration target the render is measured against.",
    ),
    Artifact(
        "publish-metadata.json",
        "publish_pack",
        "Project-owned YouTube title, description, thumbnail text, "
        "hashtags, and source statement.",
        kind="json",
    ),
    Artifact(
        "youtube-publish-pack.md",
        "publish_pack",
        "Generated by scripts/make-publish-pack; do not hand-write.",
        producer="scripts/make-publish-pack",
    ),
    Artifact(
        "artifact_manifest.json",
        "manifest_binding",
        "Declares the canonical final video and its digest. Must agree "
        "with the bytes on disk.",
        kind="json",
        producer="scripts/agent-status --write",
    ),
]

# Written by tooling during the run, never scaffolded — a stub here would be
# read as real state. See the module docstring.
#
# `artifact_manifest.json` is declared in ARTIFACTS above (the manifest_binding
# gate needs it, and the checklist must show it) but is emitted by
# `agent_status.py --write` alongside pipeline_status.json. Scaffolding it would
# tell a producer to hand-write a file the tooling generates — the exact
# confusion this module exists to remove.
#
# The render self-eval result is the sharpest case. It is declared above (the
# render_self_eval gate needs it and the checklist must show it) but it is only
# ever sealed by the runner against an external authority ledger. A placeholder
# there would be a project-tree claim with no anchor behind it, which is exactly
# the substitution the ledger exists to refuse — so the scaffold must not create
# one and `produce` must refuse to promote one.
GENERATED_NEVER_SCAFFOLD = (
    "pipeline_status.json",
    "publish/publish-approval.json",
    "artifact_manifest.json",
    RENDER_SELF_EVAL_RESULT,
)


def placeholder(artifact: Artifact) -> str:
    if artifact.kind == "json":
        return (
            json.dumps(
                {
                    "_todo": TODO_MARKER,
                    "_purpose": artifact.purpose,
                    "_gate": artifact.gate,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    return f"{TODO_MARKER}\n\ngate: {artifact.gate}\npurpose: {artifact.purpose}\n"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomically(path: Path, value: dict) -> None:
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


def _artifact(path: str) -> Artifact:
    matches = [artifact for artifact in ARTIFACTS if artifact.path == path]
    if len(matches) == 1 and matches[0].kind != "dir":
        artifact = matches[0]
    elif re.fullmatch(r"quality-review/final-v\d+/(?:prep|review)\.json", path):
        artifact = Artifact(
            path,
            "qa",
            "Digest-bound final quality evidence.",
            kind="json",
        )
    elif path == "output/cover.png":
        artifact = Artifact(path, "cover", "Canonical 1280x720 cover.", kind="binary")
    elif path == "segment-plan.json":
        artifact = Artifact(
            path,
            "segments",
            "Optional canonical four-act segment plan.",
            kind="json",
        )
    else:
        raise ValueError("artifact must be one declared canonical file")
    if artifact.path in GENERATED_NEVER_SCAFFOLD:
        raise ValueError("generated artifacts must use their dedicated producer")
    return artifact


def _validate_source(source: Path, artifact: Artifact) -> bytes:
    if source.is_symlink() or not source.is_file() or source.stat().st_size <= 0:
        raise ValueError("source must be a non-empty direct file")
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise ValueError("source cannot be read") from exc
    if not payload:
        raise ValueError("source must be a non-empty direct file")
    if artifact.kind == "binary":
        return payload
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("source must be UTF-8 text") from exc
    if not text.strip() or TODO_MARKER in text:
        raise ValueError("source is empty or still contains the scaffold marker")
    if artifact.kind == "json":
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("source must be valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("canonical JSON artifacts must be objects")
        if artifact.path == "project-contract.json":
            lane = value.get("lane_contract")
            pinned = editorial_contract.LANE_PROFILES
            if (
                value.get("schema") != "haru.project_contract.v1"
                or lane not in pinned
                or value.get("production_profile") != pinned[lane]
            ):
                raise ValueError(
                    "project contract schema, lane, or production profile is invalid"
                )
            validate_runtime_contract(value.get("runtime_contract"))
            validate_publish_target(value.get("publish_target"))
        if artifact.path == "claims.json":
            claims = value.get("claims")
            if (
                not isinstance(claims, list)
                or not claims
                or any(
                    not isinstance(claim, dict)
                    or any(
                        not isinstance(claim.get(field), str)
                        or not claim[field].strip()
                        for field in ("id", "source_name", "source_type", "source_url")
                    )
                    for claim in claims
                )
            ):
                raise ValueError("claims must carry source identity and URL")
        if artifact.path.endswith(".pron-ok.json") and (
            not valid_sha256(value.get("sha256"))
            or not isinstance(value.get("warnings"), list)
        ):
            raise ValueError("pronunciation receipt is invalid")
        if artifact.path.endswith("storyboard-final-timed-validation.json") and (
            value.get("ok") is not True or not isinstance(value.get("checks"), list)
        ):
            raise ValueError("storyboard validation is invalid")
        if artifact.path == "publish-metadata.json":
            validate_publish_metadata(value)
        if artifact.path.endswith("storyboard-final-timed.json") and not isinstance(
            value.get("scenes"), list
        ):
            raise ValueError("timed storyboard must contain scenes")
        if artifact.path == "segment-plan.json" and (
            value.get("schema") != "haru.segment_plan.v1"
            or [
                segment.get("segment_id")
                for segment in value.get("segments", [])
                if isinstance(segment, dict)
            ]
            != ["qi", "cheng", "zhuan", "he"]
        ):
            raise ValueError("segment plan schema and ordered segment IDs are invalid")
        if artifact.path.endswith("/prep.json"):
            metadata = value.get("metadata")
            audio = value.get("audio")
            if (
                not isinstance(metadata, dict)
                or not number(metadata.get("duration"))
                or metadata["duration"] <= 0
                or not isinstance(audio, dict)
            ):
                raise ValueError("quality prep contract is invalid")
        if artifact.path.endswith("/review.json") and (
            not valid_sha256(value.get("video_sha256"))
            or value.get("publish_readiness") not in {"ship", "hold"}
            or not isinstance(value.get("critical_issues"), list)
            or not isinstance(value.get("warnings"), list)
            or not isinstance(value.get("checks"), list)
        ):
            raise ValueError("quality review contract is invalid")
    elif artifact.path == "narration-final.srt" and "-->" not in text:
        raise ValueError("SRT contains no timed cues")
    elif artifact.path == "issue_brief.md" and not re.search(
        r"(?im)Duration target\s*:\s*\d+(?:\.\d+)?\s*[-–]\s*\d+(?:\.\d+)?\s*seconds?",
        text,
    ):
        raise ValueError("issue brief has no duration target")
    return payload


def validate_publish_metadata(value, project_name=None):
    if not isinstance(value, dict) or value.get("schema") != "haru.publish_metadata.v1":
        raise ValueError("publish metadata schema is invalid")
    if project_name is not None and value.get("project") != project_name:
        raise ValueError("publish metadata project does not match directory")
    for field in (
        "project",
        "title",
        "description",
        "thumbnail_text",
        "source_statement",
    ):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ValueError(f"publish metadata {field} is required")
    hashtags = value.get("hashtags")
    if (
        not isinstance(hashtags, list)
        or not hashtags
        or any(
            not isinstance(tag, str) or not re.fullmatch(r"#[^\s#]+", tag)
            for tag in hashtags
        )
    ):
        raise ValueError("publish metadata hashtags are invalid")
    if not isinstance(value.get("made_for_kids"), bool):
        raise ValueError("publish metadata made_for_kids is required")
    if not isinstance(value.get("category_id"), str) or not re.fullmatch(
        r"\d+", value["category_id"]
    ):
        raise ValueError("publish metadata category_id is invalid")
    if re.search(r"\bTODO\b|HVP_TODO_REPLACE_ME", json.dumps(value), re.IGNORECASE):
        raise ValueError("publish metadata still contains TODO")
    return value


def valid_sha256(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def produce(
    project: Path,
    artifact_path: str,
    source: Path,
    *,
    produced_by: str,
    force: bool = False,
) -> dict:
    """Validate and atomically commit one agent-authored canonical artifact."""
    project_input = Path(project)
    if project_input.is_symlink() or not project_input.is_dir():
        raise ValueError("project must be a direct directory")
    project = project_input.resolve()
    if not produced_by.strip():
        raise ValueError("produced_by is required")
    artifact = _artifact(artifact_path)
    source = Path(source)
    payload = _validate_source(source, artifact)
    state = direct_path(project / ".hvp", project)
    if state is None or state.is_symlink():
        raise ValueError("project state must be a direct path")
    state.mkdir(exist_ok=True)
    receipt_dir = direct_path(state / "producer-receipts", project)
    if receipt_dir is None or receipt_dir.is_symlink():
        raise ValueError("producer receipt directory must be a direct path")
    receipt_dir.mkdir(exist_ok=True)
    if not receipt_dir.is_dir():
        raise ValueError("producer receipt directory is invalid")
    target = direct_path(project / artifact.path, project)
    if target is None or target.is_symlink():
        raise ValueError("artifact path is not a direct project path")
    if target.exists() and not force:
        existing = target.read_bytes()
        if existing and existing != payload and TODO_MARKER.encode() not in existing:
            raise ValueError(
                "artifact already contains real work; use --force to replace it"
            )

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("wb", dir=target.parent, delete=False) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, target)

    digest = sha256(target)
    receipt = {
        "schema": "haru.producer_receipt.v1",
        "project": project.name,
        "artifact": artifact.path,
        "gate": artifact.gate,
        "producer": artifact.producer,
        "produced_by": produced_by.strip(),
        "produced_at": dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "source_sha256": digest,
        "output_sha256": digest,
        "bytes": target.stat().st_size,
    }
    receipt_name = artifact.path.replace("/", "__") + ".json"
    _write_json_atomically(receipt_dir / receipt_name, receipt)
    return receipt


def scaffold(project: Path, *, force: bool = False) -> list:
    """Create the canonical skeleton. Returns the paths written."""
    written = []
    project.mkdir(parents=True, exist_ok=True)
    # Existing projects have lane authority. Do not add a profile-specific
    # placeholder the lane forbids: after HVP-33, merely creating an unexpected
    # editorial input correctly stales the externally anchored self-eval pass.
    # A brand-new project has no contract yet, so it retains the historical full
    # skeleton and lets the checklist explain every available lane artifact.
    active_profile_known = False
    active_profile = None
    contract_path = direct_path(project / "project-contract.json", project)
    if contract_path is not None and contract_path.is_file():
        try:
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            contract = None
        lane = contract.get("lane_contract") if isinstance(contract, dict) else None
        if (
            isinstance(contract, dict)
            and contract.get("schema") == "haru.project_contract.v1"
            and lane in editorial_contract.LANE_PROFILES
        ):
            active_profile_known = True
            active_profile = editorial_contract.LANE_PROFILES[lane]
    for artifact in ARTIFACTS:
        if (
            artifact.profiles
            and active_profile_known
            and active_profile not in artifact.profiles
        ):
            continue
        if artifact.path in GENERATED_NEVER_SCAFFOLD:
            continue
        target = project / artifact.path
        if (
            target.is_symlink()
            or direct_path(target, project) is None
            and target.exists()
        ):
            # Writing through it would clobber a file outside the project —
            # `--force` on a stray symlinked artifact would overwrite the
            # target, not the link. Refuse loudly; this is a trust boundary.
            raise ValueError(
                f"{artifact.path} is reached through a symlink; refusing to write. "
                "Remove the link first — the gate will not read it either."
            )
        if artifact.kind == "dir":
            target.mkdir(parents=True, exist_ok=True)
            written.append(artifact.path)
            continue
        if target.exists() and not force:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if artifact.kind == "binary":
            # An empty file, so the gate reports "missing" rather than reading a
            # placeholder as audio.
            target.write_bytes(b"")
        else:
            target.write_text(placeholder(artifact), encoding="utf-8")
        written.append(artifact.path)
    return written


def outstanding(project: Path) -> list:
    """Artifacts still absent, empty, unreachable, or carrying the marker.

    "Unreachable" matters: anything reached through a symlink is outstanding,
    because the gate refuses to read it. Reporting it done would tell a
    producer they are finished on a project that cannot pass.
    """
    pending = []
    contract = direct_path(project / "project-contract.json", project)
    try:
        value = json.loads(contract.read_text(encoding="utf-8")) if contract else None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        value = None
    known_profile = None
    if isinstance(value, dict) and value.get("schema") == "haru.project_contract.v1":
        if value.get("lane_contract") == "manual.v1":
            known_profile = ""
        elif (
            value.get("lane_contract") == "social_issue_longform.v1"
            and value.get("production_profile") == "mina_longform.v1"
        ):
            known_profile = "mina_longform.v1"
    for artifact in ARTIFACTS:
        if (
            artifact.profiles
            and known_profile is not None
            and known_profile not in artifact.profiles
        ):
            continue
        safe = direct_path(project / artifact.path, project)
        if safe is None:
            pending.append(artifact)
            continue
        if artifact.kind == "dir":
            if not safe.is_dir():
                pending.append(artifact)
            continue
        if not safe.is_file() or safe.stat().st_size == 0:
            pending.append(artifact)
            continue
        try:
            if TODO_MARKER in safe.read_text(encoding="utf-8"):
                pending.append(artifact)
        except (OSError, UnicodeDecodeError):
            # Only the binary artifact is legitimately undecodable. For a text
            # or json artifact, unreadable bytes are a broken file, not proof
            # somebody produced it — the previous `pass` here reported a
            # truncated claims.json as done while the gate blocked it.
            if artifact.kind != "binary":
                pending.append(artifact)
    return pending


def checklist(project: Path) -> str:
    pending = {a.path for a in outstanding(project)}
    lines = [f"# Canonical artifacts — {project.name}", ""]
    for artifact in ARTIFACTS:
        mark = " " if artifact.path in pending else "x"
        lines.append(
            f"- [{mark}] `{artifact.path}` ({artifact.gate}) — {artifact.purpose}"
        )
    lines += ["", "Generated by tooling, never hand-written:"]
    lines += [f"- `{p}`" for p in GENERATED_NEVER_SCAFFOLD]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Canonical project layout.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scaffold", help="Write the canonical skeleton into a project")
    p.add_argument("project", type=Path)
    p.add_argument("--force", action="store_true", help="Overwrite existing files")

    p = sub.add_parser("checklist", help="Show which canonical artifacts remain")
    p.add_argument("project", type=Path)

    p = sub.add_parser(
        "produce",
        help="Validate and atomically commit one agent-authored canonical artifact",
    )
    p.add_argument("project", type=Path)
    p.add_argument("artifact")
    p.add_argument("--from", dest="source", type=Path, required=True)
    p.add_argument("--produced-by", required=True)
    p.add_argument("--force", action="store_true")

    sub.add_parser("declare", help="Emit the layout declaration as JSON")

    args = parser.parse_args()
    if args.command == "scaffold":
        written = scaffold(args.project, force=args.force)
        print(
            json.dumps(
                {"schema": SCHEMA, "project": str(args.project), "written": written},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "checklist":
        sys.stdout.write(checklist(args.project))
        return 0 if not outstanding(args.project) else 1
    if args.command == "produce":
        receipt = produce(
            args.project,
            args.artifact,
            args.source,
            produced_by=args.produced_by,
            force=args.force,
        )
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return 0
    print(
        json.dumps(
            {
                "schema": SCHEMA,
                "todo_marker": TODO_MARKER,
                "artifacts": [a.as_dict() for a in ARTIFACTS],
                "generated_never_scaffold": list(GENERATED_NEVER_SCAFFOLD),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
