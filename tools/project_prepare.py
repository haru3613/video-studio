#!/usr/bin/env python3
"""Prepare a bundled narrated project from owner-bound inputs; never synthesize speech."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import artifact_intake
import template_trust
from workspace_barrier import mutation_barrier

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "video_studio.project_spec.v1"
RESULT = "video_studio.project_preparation.v1"
ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
TIME = re.compile(r"^(\d{2,}):(\d{2}):(\d{2})[,.](\d{3})$")
ROLES = {
    "audio": {"reference_audio", "background_music", "sound_effect"},
    "subtitle": {"subtitle"},
    "image": {"reference_image"},
    "video": {"source_video"},
}
PALETTE = {
    "background": "#0A0F1C",
    "surface": "#141C2F",
    "ink": "#F4F7FF",
    "muted": "#97A4BE",
    "accent": "#62E6C6",
    "accentWarm": "#FFB45C",
}


class PrepareError(ValueError):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def require(condition, code, message):
    if not condition:
        raise PrepareError(code, message)


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def direct(project, relative):
    path = project
    for part in Path(relative).parts:
        require(
            part not in ("..", ".") and not Path(part).is_absolute(),
            "path_invalid",
            "unsafe project path",
        )
        path /= part
        require(
            not path.is_symlink(), "path_invalid", "project paths must not use symlinks"
        )
    return path


def probe(path):
    binary = shutil.which("ffprobe")
    require(binary is not None, "media_tool_unavailable", "ffprobe is required")
    try:
        run = subprocess.run(
            [
                binary,
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            check=False,
        )
        value = json.loads(run.stdout)
        require(
            run.returncode == 0 and isinstance(value.get("streams"), list),
            "media_invalid",
            "media cannot be probed",
        )
        return value
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError) as error:
        raise PrepareError("media_invalid", "media cannot be probed") from error


def timestamp(text):
    match = TIME.fullmatch(text.strip())
    require(match is not None, "captions_invalid", "captions require SRT timestamps")
    h, m, s, ms = map(int, match.groups())
    require(m < 60 and s < 60, "captions_invalid", "invalid SRT time")
    return ((h * 60 + m) * 60 + s) * 1000 + ms


def parse_srt(path, duration_ms):
    try:
        text = (
            path.read_text(encoding="utf-8-sig")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
        )
    except UnicodeError as error:
        raise PrepareError("captions_invalid", "captions must be UTF-8 SRT") from error
    captions, last_end = [], 0
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        if lines and lines[0].strip().isdigit():
            lines = lines[1:]
        require(
            len(lines) >= 2 and "-->" in lines[0],
            "captions_invalid",
            "each SRT cue needs time and text",
        )
        times = lines[0].split("-->")
        require(len(times) == 2, "captions_invalid", "invalid SRT cue time")
        start, end = timestamp(times[0]), timestamp(times[1])
        body = "\n".join(lines[1:]).strip()
        require(
            body and len(body) <= 2000 and last_end <= start < end <= duration_ms,
            "captions_invalid",
            "SRT cues must be non-overlapping and fit the audio",
        )
        captions.append(
            {
                "text": body,
                "startMs": start,
                "endMs": end,
                "timestampMs": None,
                "confidence": None,
            }
        )
        last_end = end
    require(
        0 < len(captions) <= 10000, "captions_invalid", "provide 1 to 10000 SRT cues"
    )
    return captions


def text(value, field, limit=240):
    require(
        isinstance(value, str) and 0 < len(value.strip()) <= limit,
        "spec_invalid",
        f"{field} requires non-empty text",
    )
    return value.strip()


def milliseconds(value):
    require(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value),
        "spec_invalid",
        "scene time must be finite",
    )
    return round(value * 1000)


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def active_jobs(project):
    database = direct(project, ".hvp/jobs.sqlite3")
    if not database.exists():
        return False
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    try:
        return (
            connection.execute(
                "SELECT 1 FROM jobs WHERE status IN ('queued','running','cancel_requested','promoting') LIMIT 1"
            ).fetchone()
            is not None
        )
    finally:
        connection.close()


def materialize(project, owner, spec, spec_digest):
    require(
        isinstance(spec, dict) and spec.get("schema") == SCHEMA,
        "spec_invalid",
        "expected video_studio.project_spec.v1",
    )
    require(
        set(spec)
        <= {
            "schema",
            "title",
            "kicker",
            "format",
            "narration",
            "assets",
            "scenes",
            "palette",
            "background_music",
        },
        "spec_invalid",
        "unknown project spec field",
    )
    title = text(spec.get("title"), "title")
    format_name = spec.get("format", "landscape")
    require(
        format_name in ("landscape", "portrait"),
        "spec_invalid",
        "format must be landscape or portrait",
    )
    narration = spec.get("narration")
    require(
        isinstance(narration, dict) and narration.get("mode") == "import",
        "narration_source_required",
        "choose import with your audio and SRT; TTS and demo generation are separate explicit actions",
    )
    require(
        set(narration) <= {"mode", "audio", "captions", "provider"},
        "spec_invalid",
        "unknown narration field",
    )
    assets = spec.get("assets")
    require(
        isinstance(assets, list) and 2 <= len(assets) <= 32,
        "spec_invalid",
        "provide audio/subtitles and up to 32 staged assets",
    )
    workspace = project.parent.parent
    resolved, probes = {}, {}
    for item in assets:
        require(
            isinstance(item, dict) and set(item) == {"id", "kind", "stage_id"},
            "spec_invalid",
            "asset needs id, kind and owner-bound stage_id",
        )
        asset_id, kind = item.get("id"), item.get("kind")
        require(
            isinstance(asset_id, str)
            and ID.fullmatch(asset_id)
            and asset_id not in resolved
            and kind in ROLES,
            "spec_invalid",
            "asset IDs must be unique safe names with supported kinds",
        )
        stage = artifact_intake.resolve_stage(
            workspace, project, item["stage_id"], owner
        )
        require(
            stage["role"] in ROLES[kind],
            "asset_kind_mismatch",
            "staged media role does not match its declared kind",
        )
        source = direct(project, stage["blob"])
        require(
            source.is_file() and sha(source) == stage["sha256"],
            "asset_changed",
            "staged asset changed",
        )
        value = {
            "id": asset_id,
            "kind": kind,
            "source": source,
            "sha256": stage["sha256"],
            "extension": stage["extension"],
            "stage_id": item["stage_id"],
        }
        value["path"] = f"assets/{asset_id}-{stage['sha256'][:16]}{stage['extension']}"
        if kind != "subtitle":
            metadata = probe(source)
            wanted = "audio" if kind == "audio" else "video"
            require(
                any(s.get("codec_type") == wanted for s in metadata["streams"]),
                "asset_kind_mismatch",
                "media stream does not match its declared kind",
            )
            probes[asset_id] = metadata
        resolved[asset_id] = value

    def get(asset_id, kind):
        require(
            isinstance(asset_id, str)
            and asset_id in resolved
            and resolved[asset_id]["kind"] == kind,
            "asset_missing",
            f"{kind} asset reference is missing or has the wrong kind",
        )
        return resolved[asset_id]

    voice = get(narration.get("audio"), "audio")
    subtitles = get(narration.get("captions"), "subtitle")
    require(
        subtitles["extension"] == ".srt",
        "captions_invalid",
        "this template accepts UTF-8 SRT subtitles",
    )
    try:
        duration_ms = round(float(probes[voice["id"]]["format"]["duration"]) * 1000)
    except (KeyError, TypeError, ValueError) as error:
        raise PrepareError(
            "audio_duration_invalid", "audio duration is unavailable"
        ) from error
    require(
        0 < duration_ms <= 3600000,
        "audio_duration_invalid",
        "audio must be positive and at most one hour",
    )
    captions = parse_srt(subtitles["source"], duration_ms)
    palette = dict(PALETTE)
    palette_input = spec.get("palette", {})
    require(
        isinstance(palette_input, dict) and set(palette_input) <= set(PALETTE),
        "spec_invalid",
        "unknown palette field",
    )
    require(
        all(isinstance(v, str) and COLOR.fullmatch(v) for v in palette_input.values()),
        "spec_invalid",
        "palette values must be six-digit hex colors",
    )
    palette.update(palette_input)
    source_scenes = spec.get("scenes")
    if source_scenes is None:
        source_scenes = [
            {
                "id": f"cue-{i+1}",
                "start_seconds": (0 if i == 0 else cue["startMs"]) / 1000,
                "end_seconds": (
                    captions[i + 1]["startMs"] if i + 1 < len(captions) else duration_ms
                )
                / 1000,
                "heading": cue["text"][:120],
                "visual": {"kind": "signal"},
            }
            for i, cue in enumerate(captions)
        ]
    require(
        isinstance(source_scenes, list) and 1 <= len(source_scenes) <= 128,
        "spec_invalid",
        "provide 1 to 128 scenes",
    )
    scenes, storyboard, cursor, scene_ids = [], [], 0, set()
    for scene in source_scenes:
        require(
            isinstance(scene, dict)
            and set(scene)
            <= {"id", "start_seconds", "end_seconds", "heading", "text", "visual"},
            "spec_invalid",
            "unknown scene field",
        )
        scene_id = scene.get("id")
        require(
            isinstance(scene_id, str)
            and ID.fullmatch(scene_id)
            and scene_id not in scene_ids,
            "spec_invalid",
            "scene IDs must be unique safe names",
        )
        scene_ids.add(scene_id)
        start, end = milliseconds(scene.get("start_seconds")), milliseconds(
            scene.get("end_seconds")
        )
        require(
            start == cursor and start < end <= duration_ms,
            "scene_timing_invalid",
            "scenes must cover the audio without gaps or overlaps",
        )
        visual = scene.get("visual", {"kind": "signal"})
        require(
            isinstance(visual, dict)
            and set(visual) <= {"kind", "asset", "labels", "fit", "active_index"},
            "spec_invalid",
            "unknown visual field",
        )
        kind = visual.get("kind")
        require(
            kind in ("signal", "cards", "steps", "image", "video"),
            "visual_invalid",
            "unsupported visual kind",
        )
        data = {"kind": kind}
        if kind in ("image", "video"):
            media = get(visual.get("asset"), kind)
            fit = visual.get("fit", "contain")
            require(
                fit in ("contain", "cover"),
                "visual_invalid",
                "fit must be contain or cover",
            )
            data.update(path=media["path"], fit=fit)
            if kind == "video":
                try:
                    clip_ms = round(
                        float(probes[media["id"]]["format"]["duration"]) * 1000
                    )
                except (KeyError, ValueError, TypeError) as error:
                    raise PrepareError(
                        "media_invalid", "video duration is unavailable"
                    ) from error
                require(
                    clip_ms >= end - start,
                    "video_too_short",
                    "video asset is shorter than its scene; trim the scene or supply a longer clip",
                )
        if "labels" in visual:
            labels = visual["labels"]
            require(
                isinstance(labels, list) and len(labels) == 3,
                "visual_invalid",
                "cards and steps need three labels",
            )
            data["labels"] = [text(label, "visual label", 60) for label in labels]
        active = visual.get("active_index", 1)
        require(
            isinstance(active, int)
            and not isinstance(active, bool)
            and 0 <= active <= 2,
            "visual_invalid",
            "active_index must be 0, 1 or 2",
        )
        data["activeIndex"] = active
        if "text" in scene:
            data["text"] = text(scene["text"], "scene text", 500)
        heading = text(scene.get("heading"), "heading", 180)
        points = (
            [start]
            + [c["startMs"] for c in captions if start < c["startMs"] < end]
            + [end]
        )
        events = []
        story_events = []
        for n, (a, b) in enumerate(zip(points, points[1:])):
            cue = next(
                (c for c in reversed(captions) if c["startMs"] <= a), captions[0]
            )
            event_id = f"{scene_id}-{n+1}"
            events.append(
                {
                    "eventId": event_id,
                    "cue": cue["text"],
                    "visualState": "imported." + kind,
                    "presenterState": "hidden",
                    "startMs": a,
                    "endMs": b,
                    "visual": data,
                }
            )
            story_events.append(
                {
                    "event_id": event_id,
                    "cue": cue["text"],
                    "visual_state": "imported." + kind,
                    "presenter_state": "hidden",
                    "start_seconds": a / 1000,
                    "end_seconds": b / 1000,
                    "start_frame": round(a * 0.03),
                    "end_frame": round(b * 0.03),
                    "duration_frames": round(b * 0.03) - round(a * 0.03),
                }
            )
        scenes.append(
            {
                "sceneId": scene_id,
                "label": heading,
                "startMs": start,
                "endMs": end,
                "events": events,
            }
        )
        storyboard.append(
            {
                "scene_id": scene_id,
                "section": heading,
                "start_seconds": start / 1000,
                "end_seconds": end / 1000,
                "start_frame": round(start * 0.03),
                "end_frame": round(end * 0.03),
                "duration_frames": round(end * 0.03) - round(start * 0.03),
                "visual_events": story_events,
            }
        )
        cursor = end
    require(
        cursor == duration_ms,
        "scene_timing_invalid",
        "last scene must end at the supplied audio duration",
    )
    background = None
    if spec.get("background_music") is not None:
        music = spec["background_music"]
        require(
            isinstance(music, dict) and set(music) <= {"asset", "gain_db"},
            "spec_invalid",
            "invalid background music",
        )
        gain = music.get("gain_db", -28)
        require(
            isinstance(gain, (int, float))
            and not isinstance(gain, bool)
            and math.isfinite(gain)
            and -60 <= gain <= 0,
            "spec_invalid",
            "music gain_db must be between -60 and 0",
        )
        track = get(music.get("asset"), "audio")
        background = {
            "path": track["path"],
            "kind": "local_audio",
            "label": "User-supplied background music",
            "volume": 10 ** (gain / 20),
        }
    content = {
        "schema": "video_studio.narrated_content.v1",
        "visual_timeline_contract": "cue_driven.v1",
        "title": title,
        "kicker": text(spec.get("kicker", title), "kicker"),
        "fps": 30,
        "durationMs": duration_ms,
        "palette": palette,
        "media": {
            "narration": {
                "path": voice["path"],
                "kind": "user_supplied_narration",
                "label": "Imported user-selected audio; pronunciation unreviewed",
            },
            "backgroundMusic": background,
            "soundEffects": [],
        },
        "captions": captions,
        "scenes": scenes,
    }
    provider = narration.get("provider", "user-supplied")
    require(
        isinstance(provider, str) and 0 < len(provider) <= 120,
        "spec_invalid",
        "provider label must be bounded text",
    )
    files = {
        "remotion/src/content.json": json_bytes(content),
        "composition-content.json": json_bytes(content),
        "narration-final.srt": subtitles["source"].read_bytes(),
        "cues.json": json_bytes(
            [
                {
                    "start": c["startMs"] / 1000,
                    "end": c["endMs"] / 1000,
                    "text": c["text"],
                }
                for c in captions
            ]
        ),
        "storyboard-final-timed.json": json_bytes(
            {
                "schema": "haru.storyboard_timed.v1",
                "project": project.name,
                "srt": "narration-final.srt",
                "visual_timeline_contract": "cue_driven.v1",
                "timing_basis": "User-supplied subtitles and authored scenes; semantic alignment unreviewed",
                "duration_seconds": duration_ms / 1000,
                "scene_count": len(scenes),
                "scenes": storyboard,
            }
        ),
        "render_plan.json": json_bytes(
            {
                "schema": "haru.render_plan.v1",
                "engine": "remotion",
                "remotion_dir": "remotion",
                "composition": (
                    "NarratedPortrait"
                    if format_name == "portrait"
                    else "NarratedLandscape"
                ),
                "output": "output/final.mp4",
                "expected_duration": duration_ms / 1000,
                "concurrency": 1,
                "skip_pronunciation_gate": True,
            }
        ),
        "script-proposal.md": (
            "# " + title + "\n\n" + "\n\n".join(c["text"] for c in captions) + "\n"
        ).encode(),
        "narration-source.json": json_bytes(
            {
                "schema": "video_studio.narration_source.v1",
                "mode": "import",
                "provider": provider,
                "audio_sha256": voice["sha256"],
                "captions_sha256": subtitles["sha256"],
                "pronunciation_reviewed": False,
                "retimed": False,
            }
        ),
        "authoring/review-package.json": json_bytes(
            {
                "schema": "haru.review_package.v1",
                "title": title,
                "assets": [
                    {
                        "id": "video-current",
                        "kind": "video",
                        "label": "Current video",
                        "role": "current",
                        "path": "output/final.mp4",
                    },
                    {
                        "id": "audio-current",
                        "kind": "audio",
                        "label": "Imported narration (unreviewed)",
                        "role": "current",
                        "path": "remotion/public/" + voice["path"],
                    },
                ],
                "changes": [],
                "chapters": [
                    {"title": s["label"], "seconds": s["startMs"] / 1000}
                    for s in scenes
                ],
            }
        ),
    }
    media_files = {
        "remotion/public/" + a["path"]: a
        for a in resolved.values()
        if a["kind"] != "subtitle"
    }
    return files, media_files, duration_ms


def prepare(project_value, owner):
    project = Path(project_value)
    require(
        project.is_absolute()
        and not project.is_symlink()
        and project.is_dir()
        and project.resolve() == project,
        "project_invalid",
        "use a canonical project path",
    )
    state = direct(project, ".hvp")
    require(
        state.is_dir(),
        "project_invalid",
        "create the project through the CLI/MCP first",
    )
    contract = direct(project, "project-contract.json")
    try:
        contract_raw = contract.read_bytes()
        value = json.loads(contract_raw)
    except (OSError, ValueError) as error:
        raise PrepareError(
            "project_invalid", "project contract is unavailable"
        ) from error
    require(
        isinstance(value.get("runtime_contract"), dict),
        "project_invalid",
        "project needs a runtime contract",
    )
    require(
        value.get("lane_contract") in (None, "HVP_TODO_REPLACE_ME", "manual.v1")
        and value.get("production_profile") in (None, "HVP_TODO_REPLACE_ME"),
        "project_profile_incompatible",
        "preparation supports the manual local-delivery profile",
    )
    spec_path = direct(project, "project-spec.json")
    require(
        spec_path.is_file() and spec_path.stat().st_size <= 1024 * 1024,
        "spec_invalid",
        "project-spec.json must be at most 1 MiB",
    )
    raw = spec_path.read_bytes()
    spec_digest = hashlib.sha256(raw).hexdigest()
    try:
        spec = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise PrepareError("spec_invalid", "project spec is not valid JSON") from error
    with mutation_barrier(project):
        lock = direct(project, ".hvp/project-prepare.lock")
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            require(
                not active_jobs(project),
                "project_busy",
                "wait for or cancel the active render before preparing new inputs",
            )
            bundle = ROOT / "templates/narrated/remotion"
            remotion = direct(project, "remotion")
            fresh = not remotion.exists()
            if not fresh:
                require(
                    remotion.is_dir()
                    and template_trust.code_digest(remotion)[0]
                    == template_trust.code_digest(bundle)[0],
                    "template_conflict",
                    "existing custom or older template code is preserved; use a fresh project or migrate it explicitly",
                )
            files, media, duration_ms = materialize(project, owner, spec, spec_digest)
            if (
                value.get("lane_contract") != "manual.v1"
                or value.get("production_profile") is not None
            ):
                # `create` scaffolds an unselected contract. Importing a narrated
                # spec explicitly selects local manual delivery, never a presenter
                # profile or editorial/publication approval.
                value.update(lane_contract="manual.v1", production_profile=None)
                files["project-contract.json"] = json_bytes(value)
            for relative in [*files, *media]:
                destination = direct(project, relative)
                require(
                    not destination.exists() or destination.is_file(),
                    "path_invalid",
                    "a prepared file target is not a regular file",
                )
            require(
                spec_path.read_bytes() == raw,
                "spec_changed",
                "project spec changed while preparing",
            )
            with tempfile.TemporaryDirectory(prefix="prepare-", dir=state) as temporary:
                temporary = Path(temporary)
                stage = temporary / "new"
                stage.mkdir()
                saved = temporary / "old"
                saved.mkdir()
                committed = []
                created_dirs = []
                if fresh:
                    shutil.copytree(
                        bundle,
                        stage / "remotion",
                        ignore=shutil.ignore_patterns(
                            "node_modules", "output", ".cache"
                        ),
                        copy_function=shutil.copyfile,
                    )
                    # The release is read-only; the project's copy must be editable.
                    for directory in [
                        stage / "remotion",
                        *(stage / "remotion").rglob("*"),
                    ]:
                        if directory.is_dir():
                            directory.chmod(0o755)
                for relative, payload in files.items():
                    target = stage / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(payload)
                for relative, asset in media.items():
                    target = stage / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(asset["source"], target)
                    require(
                        sha(target) == asset["sha256"],
                        "asset_changed",
                        "staged media changed during copy",
                    )
                require(
                    spec_path.read_bytes() == raw,
                    "spec_changed",
                    "project spec changed while preparing",
                )
                require(
                    contract.read_bytes() == contract_raw,
                    "project_changed",
                    "project contract changed while preparing",
                )
                try:
                    if fresh:
                        os.replace(stage / "remotion", remotion)
                        committed.append(("remotion", False, True))
                    for relative in [*files, *media]:
                        if fresh and relative.startswith("remotion/"):
                            continue
                        destination = direct(project, relative)
                        parent = destination.parent
                        missing = []
                        while not parent.exists():
                            missing.append(parent)
                            parent = parent.parent
                        for directory in reversed(missing):
                            directory.mkdir()
                            created_dirs.append(directory)
                        existed = destination.exists()
                        if existed:
                            backup = saved / relative
                            backup.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(destination, backup)
                        os.replace(stage / relative, destination)
                        committed.append((relative, existed, False))
                except BaseException:
                    for relative, existed, is_dir in reversed(committed):
                        target = project / relative
                        if is_dir:
                            shutil.rmtree(target)
                        elif existed:
                            os.replace(saved / relative, target)
                        else:
                            target.unlink(missing_ok=True)
                    for directory in reversed(created_dirs):
                        try:
                            directory.rmdir()
                        except OSError:
                            pass
                    raise
            return {
                "schema": RESULT,
                "project": project.name,
                "spec_sha256": spec_digest,
                "duration_seconds": duration_ms / 1000,
                "narration_source": "import",
                "asset_count": len(spec["assets"]),
                "template": "narrated",
                "requires_dependency_install": not (
                    remotion / "node_modules/.bin/remotion"
                ).exists(),
            }


def main(argv):
    if len(argv) != 3:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "outcome": "error",
                    "code": "invalid_input",
                    "project": None,
                    "data": None,
                }
            )
        )
        return 2
    project = Path(argv[1])
    try:
        result = prepare(project, argv[2])
        response = {
            "schema_version": 1,
            "outcome": "ok",
            "code": "project_prepared",
            "project": str(project),
            "data": result,
        }
        status = 0
    except PrepareError as error:
        response = {
            "schema_version": 1,
            "outcome": "blocked",
            "code": error.code,
            "project": str(project),
            "data": {"message": error.message},
        }
        status = 3
    except artifact_intake.IntakeError as error:
        response = {
            "schema_version": 1,
            "outcome": "blocked",
            "code": error.code,
            "project": str(project),
            "data": {"message": "Restage the asset for this project and lease owner."},
        }
        status = 3
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        response = {
            "schema_version": 1,
            "outcome": "error",
            "code": "prepare_failed",
            "project": str(project),
            "data": {
                "message": "Could not prepare inputs; inspect the project and stage inputs."
            },
        }
        status = 2
    print(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
