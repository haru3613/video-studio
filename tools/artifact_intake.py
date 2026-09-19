#!/usr/bin/env python3
"""Stage bounded inbox/text inputs and import them into fixed project roles."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import workspace_barrier

STAGE_SCHEMA = "video_studio.artifact_stage.v1"
IMPORT_SCHEMA = "video_studio.artifact_import.v1"
MANIFEST_SCHEMA = "video-studio.import_manifest.v1"
STAGE_ID = re.compile(r"[0-9a-f]{32}")
REQUEST_ID = STAGE_ID
PROJECT_ID = re.compile(r"[a-z](?:[a-z0-9-]{0,62}[a-z0-9])?")
INLINE_LIMIT = 1024 * 1024

ROLE_RULES = {
    "source_video": ({".mp4", ".mov", ".webm"}, 512 * 1024 * 1024, "media"),
    "reference_image": ({".png", ".jpg", ".jpeg", ".webp"}, 50 * 1024 * 1024, "media"),
    "reference_audio": ({".mp3", ".wav", ".m4a", ".aac", ".flac"}, 200 * 1024 * 1024, "media"),
    "background_music": ({".mp3", ".wav", ".m4a", ".aac", ".flac"}, 200 * 1024 * 1024, "media"),
    "sound_effect": ({".mp3", ".wav", ".m4a", ".aac", ".flac"}, 50 * 1024 * 1024, "media"),
    "subtitle": ({".srt", ".vtt"}, 10 * 1024 * 1024, "text"),
    "script_notes": ({".txt", ".md"}, 10 * 1024 * 1024, "text"),
    "storyboard_data": ({".json"}, 10 * 1024 * 1024, "json"),
    "metadata": ({".json"}, 10 * 1024 * 1024, "json"),
}
INLINE_ROLES = {
    "subtitle": ".srt",
    "script_notes": ".txt",
    "storyboard_data": ".json",
    "metadata": ".json",
}


class IntakeError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _direct_directory(value: Path) -> Path:
    path = Path(value)
    if path.is_symlink() or not path.is_dir():
        raise IntakeError("invalid_path")
    return path.resolve(strict=True)


def _workspace(value: Path) -> Path:
    root = _direct_directory(value)
    manifest = root / "workspace.json"
    projects = root / "projects"
    inbox = root / "inbox"
    if (
        manifest.is_symlink()
        or not manifest.is_file()
        or projects.is_symlink()
        or not projects.is_dir()
        or inbox.is_symlink()
        or not inbox.is_dir()
    ):
        raise IntakeError("invalid_workspace")
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise IntakeError("invalid_workspace") from error
    if not isinstance(value, dict) or value.get("schema") != "video_studio.workspace.v1":
        raise IntakeError("invalid_workspace")
    try:
        uuid.UUID(value["workspace_id"])
    except (KeyError, TypeError, ValueError) as error:
        raise IntakeError("invalid_workspace") from error
    return root


def _project(workspace: Path, value: Path) -> Path:
    project = _direct_directory(value)
    try:
        relative = project.relative_to((workspace / "projects").resolve(strict=True))
    except ValueError as error:
        raise IntakeError("invalid_project") from error
    if len(relative.parts) != 1:
        raise IntakeError("invalid_project")
    return project


def resolve_project(workspace_value: Path, project_id: str) -> Path:
    workspace = _workspace(workspace_value)
    if not isinstance(project_id, str) or PROJECT_ID.fullmatch(project_id) is None:
        raise IntakeError("invalid_project")
    candidate = workspace / "projects" / project_id
    if candidate.is_symlink() or not candidate.is_dir():
        raise IntakeError("project_not_found")
    return _project(workspace, candidate)


def workspace_info(workspace_value: Path) -> dict:
    workspace = _workspace(workspace_value)
    manifest = json.loads((workspace / "workspace.json").read_text(encoding="utf-8"))
    projects = []
    for entry in sorted((workspace / "projects").iterdir(), key=lambda path: path.name):
        if (
            PROJECT_ID.fullmatch(entry.name)
            and not entry.is_symlink()
            and entry.is_dir()
        ):
            projects.append(
                {
                    "project_id": entry.name,
                    "contract_present": (entry / "project-contract.json").is_file(),
                }
            )
    return {
        "schema": "video-studio.workspace_catalog.v1",
        "workspace_id": manifest["workspace_id"],
        "projects": projects,
    }


def _relative_file(root: Path, value: str) -> Path:
    relative = Path(value)
    if (
        not value
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise IntakeError("invalid_inbox_path")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise IntakeError("invalid_inbox_path")
    if not current.is_file():
        raise IntakeError("invalid_inbox_path")
    try:
        current.resolve(strict=True).relative_to(root.resolve(strict=True))
    except ValueError as error:
        raise IntakeError("invalid_inbox_path") from error
    return current


def _validate_role(role: str) -> tuple[set[str], int, str]:
    try:
        return ROLE_RULES[role]
    except (KeyError, TypeError) as error:
        raise IntakeError("unsupported_role") from error


def _read_bounded(path: Path, limit: int) -> bytes:
    with path.open("rb") as handle:
        payload = handle.read(limit + 1)
    if len(payload) > limit:
        raise IntakeError("artifact_too_large")
    return payload


def _validate_payload(role: str, extension: str, payload: bytes) -> None:
    extensions, _limit, kind = _validate_role(role)
    if extension not in extensions or not payload:
        raise IntakeError("artifact_type_invalid")
    if kind in {"text", "json"}:
        try:
            text = payload.decode("utf-8")
        except UnicodeError as error:
            raise IntakeError("artifact_type_invalid") from error
        if "\x00" in text:
            raise IntakeError("artifact_type_invalid")
        if kind == "json":
            try:
                value = json.loads(text)
            except json.JSONDecodeError as error:
                raise IntakeError("artifact_type_invalid") from error
            if not isinstance(value, (dict, list)):
                raise IntakeError("artifact_type_invalid")
        return
    valid = False
    if extension == ".png":
        valid = payload.startswith(b"\x89PNG\r\n\x1a\n")
    elif extension in {".jpg", ".jpeg"}:
        valid = payload.startswith(b"\xff\xd8\xff")
    elif extension == ".webp":
        valid = payload.startswith(b"RIFF") and payload[8:12] == b"WEBP"
    elif extension in {".mp4", ".mov", ".m4a"}:
        valid = len(payload) >= 12 and payload[4:8] == b"ftyp"
    elif extension == ".webm":
        valid = payload.startswith(b"\x1aE\xdf\xa3")
    elif extension == ".wav":
        valid = payload.startswith(b"RIFF") and payload[8:12] == b"WAVE"
    elif extension == ".flac":
        valid = payload.startswith(b"fLaC")
    elif extension == ".mp3":
        valid = payload.startswith(b"ID3") or payload.startswith((b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"))
    elif extension == ".aac":
        valid = len(payload) >= 2 and payload[0] == 0xFF and payload[1] & 0xF6 == 0xF0
    if not valid:
        raise IntakeError("artifact_type_invalid")


def _stage_root(project: Path) -> Path:
    state = project / ".hvp"
    if state.is_symlink():
        raise IntakeError("invalid_project")
    state.mkdir(mode=0o700, exist_ok=True)
    staging = state / "staging"
    intake = staging / "intake"
    for path in (staging, intake):
        if path.is_symlink():
            raise IntakeError("invalid_project")
        path.mkdir(mode=0o700, exist_ok=True)
    return _direct_directory(intake)


def _stage_bytes(project: Path, role: str, extension: str, payload: bytes) -> dict:
    _validate_payload(role, extension, payload)
    digest = hashlib.sha256(payload).hexdigest()
    stage_id = hashlib.sha256(f"{role}\0{extension}\0{digest}".encode()).hexdigest()[:32]
    root = _stage_root(project)
    destination = root / stage_id
    manifest_value = {
        "schema": STAGE_SCHEMA,
        "stage_id": stage_id,
        "role": role,
        "extension": extension,
        "sha256": digest,
        "bytes": len(payload),
        "blob": f".hvp/staging/intake/{stage_id}/blob",
    }
    lock = project / ".hvp/intake.lock"
    with lock.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return _commit_stage(project, root, destination, stage_id, manifest_value, payload)


def _commit_stage(
    project: Path,
    root: Path,
    destination: Path,
    stage_id: str,
    manifest_value: dict,
    payload: bytes,
) -> dict:
    if destination.exists():
        existing = _read_stage(project, stage_id)
        if existing != manifest_value:
            raise IntakeError("stage_conflict")
        return existing
    temporary = Path(tempfile.mkdtemp(prefix=".intake-", dir=root))
    try:
        blob = temporary / "blob"
        with blob.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        manifest = temporary / "manifest.json"
        manifest.write_text(json.dumps(manifest_value, separators=(",", ":")) + "\n")
        with manifest.open("rb") as handle:
            os.fsync(handle.fileno())
        os.chmod(blob, 0o444)
        os.chmod(manifest, 0o444)
        os.replace(temporary, destination)
        os.chmod(destination, 0o555)
        descriptor = os.open(root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return manifest_value


def _read_stage(project: Path, stage_id: str) -> dict:
    if STAGE_ID.fullmatch(stage_id) is None:
        raise IntakeError("invalid_stage_id")
    root = project / ".hvp/staging/intake"
    if root.is_symlink() or not root.is_dir():
        raise IntakeError("stage_not_found")
    root = root.resolve(strict=True)
    directory = root / stage_id
    if directory.is_symlink() or not directory.is_dir():
        raise IntakeError("stage_not_found")
    manifest = directory / "manifest.json"
    blob = directory / "blob"
    if manifest.is_symlink() or blob.is_symlink() or not manifest.is_file() or not blob.is_file():
        raise IntakeError("stage_invalid")
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise IntakeError("stage_invalid") from error
    if value.get("schema") != STAGE_SCHEMA or value.get("stage_id") != stage_id:
        raise IntakeError("stage_invalid")
    if value.get("blob") != f".hvp/staging/intake/{stage_id}/blob":
        raise IntakeError("stage_invalid")
    if not isinstance(value.get("sha256"), str) or re.fullmatch(
        r"[0-9a-f]{64}", value["sha256"]
    ) is None:
        raise IntakeError("stage_invalid")
    if not isinstance(value.get("bytes"), int) or isinstance(value.get("bytes"), bool):
        raise IntakeError("stage_invalid")
    _extensions, limit, _kind = _validate_role(value.get("role"))
    payload = _read_bounded(blob, limit)
    if hashlib.sha256(payload).hexdigest() != value.get("sha256") or len(payload) != value.get("bytes"):
        raise IntakeError("stage_invalid")
    _validate_payload(value.get("role"), value.get("extension"), payload)
    return value


def stage_inbox(workspace_value: Path, project_value: Path, role: str, relative: str) -> dict:
    workspace = _workspace(workspace_value)
    project = _project(workspace, project_value)
    with workspace_barrier.mutation_barrier(project):
        source = _relative_file(workspace / "inbox", relative)
        extensions, limit, _kind = _validate_role(role)
        extension = source.suffix.lower()
        if extension not in extensions or source.stat().st_size > limit:
            raise IntakeError(
                "artifact_too_large"
                if source.stat().st_size > limit
                else "artifact_type_invalid"
            )
        return _stage_bytes(project, role, extension, _read_bounded(source, limit))


def stage_text(workspace_value: Path, project_value: Path, role: str, text: str) -> dict:
    workspace = _workspace(workspace_value)
    project = _project(workspace, project_value)
    with workspace_barrier.mutation_barrier(project):
        if role not in INLINE_ROLES:
            raise IntakeError("inline_role_invalid")
        payload = text.encode("utf-8")
        if len(payload) > INLINE_LIMIT:
            raise IntakeError("artifact_too_large")
        return _stage_bytes(project, role, INLINE_ROLES[role], payload)


def stage_inline_request(
    workspace_value: Path, project_value: Path, role: str, request_id: str
) -> dict:
    workspace = _workspace(workspace_value)
    project = _project(workspace, project_value)
    if REQUEST_ID.fullmatch(request_id) is None:
        raise IntakeError("invalid_request_id")
    if role not in INLINE_ROLES:
        raise IntakeError("inline_role_invalid")
    state = project / ".hvp"
    if state.is_symlink() or not state.is_dir():
        raise IntakeError("inline_request_not_found")
    request_root = project / ".hvp/intake-requests"
    if request_root.is_symlink() or not request_root.is_dir():
        raise IntakeError("inline_request_not_found")
    request = request_root / f"{request_id}.txt"
    if request.is_symlink() or not request.is_file():
        raise IntakeError("inline_request_not_found")
    with workspace_barrier.mutation_barrier(project):
        payload = _read_bounded(request, INLINE_LIMIT)
        try:
            payload.decode("utf-8")
        except UnicodeError as error:
            raise IntakeError("artifact_type_invalid") from error
        staged = _stage_bytes(project, role, INLINE_ROLES[role], payload)
        request.unlink()
        return staged


def import_stage(workspace_value: Path, project_value: Path, stage_id: str) -> dict:
    workspace = _workspace(workspace_value)
    project = _project(workspace, project_value)
    with workspace_barrier.mutation_barrier(project):
        stage = _read_stage(project, stage_id)
        role = stage["role"]
        extension = stage["extension"]
        # Every import stays in this non-canonical namespace. It can inform later
        # production, but can never replace narration, final video or approvals.
        relative = Path("imports") / role / f"{stage_id}{extension}"
        imports = project / "imports"
        if imports.is_symlink():
            raise IntakeError("import_conflict")
        imports.mkdir(exist_ok=True)
        role_root = imports / role
        if role_root.is_symlink():
            raise IntakeError("import_conflict")
        role_root.mkdir(exist_ok=True)
        try:
            role_root.resolve(strict=True).relative_to(project)
        except (OSError, ValueError) as error:
            raise IntakeError("import_conflict") from error
        target = role_root / f"{stage_id}{extension}"
        if target.is_symlink():
            raise IntakeError("import_conflict")
        source = project / stage["blob"]
        lock = project / ".hvp/intake.lock"
        with lock.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            if target.exists():
                if (
                    not target.is_file()
                    or hashlib.sha256(target.read_bytes()).hexdigest()
                    != stage["sha256"]
                ):
                    raise IntakeError("import_conflict")
            else:
                with source.open("rb") as input_stream:
                    with target.open("xb") as output:
                        shutil.copyfileobj(input_stream, output)
                        output.flush()
                        os.fsync(output.fileno())
                os.chmod(target, 0o444)
            manifest_path = project / "imports/manifest.json"
            if manifest_path.is_symlink():
                raise IntakeError("import_conflict")
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as error:
                    raise IntakeError("import_conflict") from error
                if manifest.get("schema") != MANIFEST_SCHEMA or not isinstance(
                    manifest.get("artifacts"), list
                ):
                    raise IntakeError("import_conflict")
            else:
                manifest = {"schema": MANIFEST_SCHEMA, "artifacts": []}
            record = {
                "stage_id": stage_id,
                "role": role,
                "path": relative.as_posix(),
                "sha256": stage["sha256"],
                "bytes": stage["bytes"],
            }
            existing = [
                item
                for item in manifest["artifacts"]
                if item.get("stage_id") == stage_id
            ]
            if existing and existing != [record]:
                raise IntakeError("import_conflict")
            if not existing:
                manifest["artifacts"].append(record)
                temporary = manifest_path.with_name(
                    f".{manifest_path.name}.{os.getpid()}.tmp"
                )
                with temporary.open("x", encoding="utf-8") as output:
                    output.write(json.dumps(manifest, separators=(",", ":")) + "\n")
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, manifest_path)
                descriptor = os.open(manifest_path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        return {"schema": IMPORT_SCHEMA, **record}


def resolve_stage(workspace_value: Path, project_value: Path, stage_id: str) -> dict:
    workspace = _workspace(workspace_value)
    project = _project(workspace, project_value)
    return _read_stage(project, stage_id)


def envelope(project, outcome, code, data=None):
    return {
        "schema_version": 1,
        "outcome": outcome,
        "code": code,
        "project": str(project) if project else None,
        "data": data,
    }


def main(argv):
    action = argv[1] if len(argv) > 1 else ""
    valid = action in {"stage-inbox", "stage-text", "stage-inline-file"} and len(argv) == 6
    valid = valid or (action in {"import", "resolve"} and len(argv) == 5)
    if not valid:
        print(json.dumps(envelope(None, "error", "invalid_input"), separators=(",", ":")))
        return 2
    project = Path(argv[3])
    try:
        if action == "stage-inbox":
            data = stage_inbox(Path(argv[2]), project, argv[4], argv[5])
            code = "artifact_staged"
        elif action == "stage-text":
            data = stage_text(Path(argv[2]), project, argv[4], argv[5])
            code = "artifact_staged"
        elif action == "stage-inline-file":
            data = stage_inline_request(Path(argv[2]), project, argv[4], argv[5])
            code = "artifact_staged"
        elif action == "import":
            data = import_stage(Path(argv[2]), project, argv[4])
            code = "artifact_imported"
        else:
            data = resolve_stage(Path(argv[2]), project, argv[4])
            code = "artifact_staged"
        response, exit_code = envelope(project.resolve(), "ok", code, data), 0
    except IntakeError as error:
        outcome = "blocked" if error.code in {"stage_conflict", "import_conflict"} else "error"
        exit_code = 3 if outcome == "blocked" else 2
        response = envelope(project, outcome, error.code)
    except (OSError, TypeError, ValueError):
        response, exit_code = envelope(project, "error", "invalid_input"), 2
    print(json.dumps(response, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
