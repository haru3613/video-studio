#!/usr/bin/env python3
"""Import a compact, digest-bound OpenMontage reference analysis."""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path


EVIDENCE_FILES = (
    "haru_reference_analysis.json",
    "scenes.json",
    "video_analysis_brief.json",
    "reference_audio.json",
)
ROLL_TYPES = {
    "a_roll_full",
    "b_roll_pure",
    "b_roll_with_presenter_pip",
}


def digest(path):
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def load_object(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def write_json(path, value):
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


def selected_reference(project):
    declaration_path = project / "reference-videos.json"
    declaration = load_object(declaration_path)
    if declaration.get("schema") != "haru.reference_videos.v1":
        raise ValueError("reference-videos.json has an unknown schema")
    references = declaration.get("references")
    if not isinstance(references, list):
        raise ValueError("reference-videos.json references must be a list")
    selected = [
        item
        for item in references
        if isinstance(item, dict) and item.get("selected") is True
    ]
    if len(selected) != 1:
        raise ValueError("reference-videos.json must select exactly one reference")
    reference = selected[0]
    reference_id = reference.get("id")
    if not isinstance(reference_id, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9_-]{0,63}", reference_id
    ):
        raise ValueError("selected reference id is invalid")
    if not str(reference.get("classification_reviewed_by") or "").strip():
        raise ValueError("classification_reviewed_by is required")
    return declaration_path, reference


def analysis_directory(reference):
    locator = reference.get("analysis")
    if not isinstance(locator, dict):
        raise ValueError("selected reference has no analysis locator")
    if locator.get("provider") != "openmontage":
        raise ValueError("selected reference analysis provider must be openmontage")
    if locator.get("root_env") != "OPENMONTAGE_ROOT":
        raise ValueError("OpenMontage analysis must use OPENMONTAGE_ROOT")
    root_value = os.environ.get("OPENMONTAGE_ROOT")
    if not root_value:
        raise ValueError("OPENMONTAGE_ROOT is not set")
    relative = locator.get("relative_path")
    if not isinstance(relative, str) or not relative.strip():
        raise ValueError("OpenMontage analysis relative_path is required")
    root = Path(root_value).expanduser().resolve(strict=True)
    candidate = root / relative
    if candidate.is_symlink():
        raise ValueError("OpenMontage analysis directory cannot be a symlink")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root) or not resolved.is_dir():
        raise ValueError("OpenMontage analysis must stay inside OPENMONTAGE_ROOT")
    return resolved


def import_analysis(project):
    project_input = Path(project).expanduser()
    if project_input.is_symlink() or not project_input.is_dir():
        raise ValueError("project must be a direct directory")
    project = project_input.resolve(strict=True)
    declaration_path, reference = selected_reference(project)
    source_dir = analysis_directory(reference)

    source_paths = []
    payloads = {}
    for name in EVIDENCE_FILES:
        path = source_dir / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"OpenMontage artifact is missing or unsafe: {name}")
        source_paths.append(path)
        payloads[name] = load_object(path)

    curated = payloads["haru_reference_analysis.json"]
    if curated.get("schema") != "haru.reference_video_analysis.v1":
        raise ValueError("OpenMontage curated analysis has an unknown schema")
    source = curated.get("source")
    if not isinstance(source, dict) or source.get("url") != reference.get("source_url"):
        raise ValueError("reference source URL does not match OpenMontage analysis")
    roll = curated.get("roll_mix_estimate")
    categories = roll.get("categories") if isinstance(roll, dict) else None
    if not isinstance(categories, list) or len(categories) != len(ROLL_TYPES) or {
        item.get("id") for item in categories if isinstance(item, dict)
    } != ROLL_TYPES:
        raise ValueError("OpenMontage roll taxonomy is incomplete")
    rules = curated.get("reusable_workflow_rules")
    if not isinstance(rules, list) or not rules or not all(
        isinstance(rule, str) and rule.strip() for rule in rules
    ):
        raise ValueError("OpenMontage editing rules are missing")
    chapters = curated.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise ValueError("OpenMontage section boundaries are missing")

    brief = payloads["video_analysis_brief.json"]
    brief_structure = brief.get("structure_analysis")
    brief_scenes = (
        brief_structure.get("scenes") if isinstance(brief_structure, dict) else None
    )
    if not isinstance(brief_scenes, list):
        raise ValueError("OpenMontage motion analysis is missing")
    unknown_count = sum(
        1
        for scene in brief_scenes
        if isinstance(scene, dict) and scene.get("motion_type") == "unknown"
    )
    provenance = curated.get("analysis_provenance")
    limitations = curated.get("known_limitations")
    duration = source.get("duration_seconds")
    output = {
        "schema": "haru.reference_video_analysis.v1",
        "reference_id": reference["id"],
        "source_snapshot": source,
        "structure": {
            "sections": chapters,
            "beat_boundaries": [
                {"at_seconds": item.get("start_seconds"), "label": item.get("label")}
                for item in chapters
                if isinstance(item, dict)
            ],
        },
        "cut_cadence": curated.get("editing_metrics"),
        "shot_groups": curated.get("five_aspect_shot_groups"),
        "roll_mix": {
            **roll,
            "categories": [
                {**item, "roll_type": item["id"]}
                for item in categories
                if isinstance(item, dict)
            ],
        },
        "b_roll_taxonomy": curated.get("b_roll_taxonomy") or [],
        "editing_rules": [
            {"id": f"{reference['id']}-rule-{index:02d}", "text": text}
            for index, text in enumerate(rules, 1)
        ],
        "methodology": {
            "openmontage_revision": (
                provenance.get("openmontage_revision")
                if isinstance(provenance, dict)
                else None
            ),
            "interval_sample_seconds": (
                provenance.get("interval_sample_seconds")
                if isinstance(provenance, dict)
                else None
            ),
            "classification_reviewed_by": reference[
                "classification_reviewed_by"
            ].strip(),
            "long_video_local_fallback": bool(
                isinstance(duration, (int, float)) and duration > 600
            ),
            "motion_classification": {
                "scene_count": len(brief_scenes),
                "unknown_count": unknown_count,
            },
        },
        "confidence": {
            "roll_mix": roll.get("confidence"),
            "expected_error_percentage_points": roll.get(
                "expected_error_percentage_points"
            ),
        },
        "limitations": limitations if isinstance(limitations, list) else [],
        "presentation_field_contract": {
            "card": "viewer-facing copy",
            "visual_intent": "art and edit instruction",
        },
        "evidence": {
            "declaration_sha256": digest(declaration_path),
            "artifacts": [
                {
                    "name": path.name,
                    "sha256": digest(path),
                    "bytes": path.stat().st_size,
                }
                for path in source_paths
            ],
        },
    }

    state = project / ".hvp"
    if state.is_symlink():
        raise ValueError("project state cannot be a symlink")
    target = project / "reference-analysis.json"
    write_json(target, output)
    receipt = {
        "schema": "haru.producer_receipt.v1",
        "project": project.name,
        "artifact": target.name,
        "gate": "reference_analysis",
        "producer": "scripts/import-openmontage-reference",
        "produced_by": os.environ.get("HARU_AGENT_NAME", "codex"),
        "produced_at": dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "source_sha256": output["evidence"]["declaration_sha256"],
        "output_sha256": digest(target),
        "bytes": target.stat().st_size,
    }
    write_json(project / ".hvp/producer-receipts/reference-analysis.json", receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(
        description="Import a compact OpenMontage reference analysis"
    )
    parser.add_argument("project")
    args = parser.parse_args()
    try:
        receipt = import_analysis(args.project)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
