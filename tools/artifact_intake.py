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
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import workspace_barrier

STAGE_SCHEMA = "video_studio.artifact_stage.v1"
STORED_STAGE_SCHEMA = "video_studio.artifact_stage.v2"
IMPORT_SCHEMA = "video_studio.artifact_import.v1"
MANIFEST_SCHEMA = "video-studio.import_manifest.v1"
STAGE_ID = re.compile(r"[0-9a-f]{32}")
REQUEST_ID = STAGE_ID
PROJECT_ID = re.compile(r"[a-z](?:[a-z0-9-]{0,62}[a-z0-9])?")
INLINE_LIMIT = 1024 * 1024
STAGE_TTL_SECONDS = 24 * 60 * 60
PROJECT_STAGE_QUOTA = 100 * 1024 * 1024
LOCAL_OPERATOR = "local-operator"

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


def _owner(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(ord(character) < 0x20 for character in value)
    ):
        raise IntakeError("invalid_owner")
    return value


def _stage_binding(workspace: Path, project: Path, owner: str) -> tuple[str, str, str]:
    try:
        workspace_id = json.loads(
            (workspace / "workspace.json").read_text(encoding="utf-8")
        )["workspace_id"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise IntakeError("invalid_workspace") from error
    return workspace_id, project.name, _owner(owner)


def _stage_id(
    workspace_id: str,
    project_scope: str,
    owner: str,
    role: str,
    extension: str,
    digest: str,
) -> str:
    binding = "\0".join(
        (workspace_id, project_scope, owner, role, extension, digest)
    ).encode()
    return hashlib.sha256(binding).hexdigest()[:32]


def _public_stage(value: dict) -> dict:
    return {
        "schema": STAGE_SCHEMA,
        "stage_id": value["stage_id"],
        "role": value["role"],
        "extension": value["extension"],
        "sha256": value["sha256"],
        "bytes": value["bytes"],
        "blob": value["blob"],
    }


def _cleanup_expired_stages(
    workspace: Path, project: Path, root: Path, now: int
) -> None:
    for directory in root.iterdir():
        if (
            STAGE_ID.fullmatch(directory.name) is None
            or directory.is_symlink()
            or not directory.is_dir()
        ):
            continue
        manifest = directory / "manifest.json"
        blob = directory / "blob"
        if (
            manifest.is_symlink()
            or blob.is_symlink()
            or not manifest.is_file()
            or not blob.is_file()
        ):
            continue
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        expires_at = value.get("expires_at") if isinstance(value, dict) else None
        if not (
            isinstance(value, dict)
            and isinstance(expires_at, int)
            and not isinstance(expires_at, bool)
            and expires_at <= now
        ):
            continue
        try:
            verified = _read_stage_manifest(
                workspace,
                project,
                directory.name,
                value.get("owner"),
                now,
                allow_expired=True,
            )
        except IntakeError:
            continue
        if verified["expires_at"] <= now:
            os.chmod(directory, 0o700)
            shutil.rmtree(directory)


def _staged_bytes(root: Path) -> int:
    total = 0
    for directory in root.iterdir():
        if STAGE_ID.fullmatch(directory.name) is None:
            continue
        if directory.is_symlink() or not directory.is_dir():
            raise IntakeError("stage_invalid")
        blob = directory / "blob"
        if blob.is_symlink() or not blob.is_file():
            raise IntakeError("stage_invalid")
        total += blob.stat().st_size
    return total


def _stage_bytes(
    workspace: Path,
    project: Path,
    owner: str,
    role: str,
    extension: str,
    payload: bytes,
) -> dict:
    _validate_payload(role, extension, payload)
    digest = hashlib.sha256(payload).hexdigest()
    workspace_id, project_scope, owner = _stage_binding(workspace, project, owner)
    stage_id = _stage_id(
        workspace_id, project_scope, owner, role, extension, digest
    )
    root = _stage_root(project)
    destination = root / stage_id
    now = int(time.time())
    manifest_value = {
        "schema": STORED_STAGE_SCHEMA,
        "stage_id": stage_id,
        "workspace_id": workspace_id,
        "project_scope": project_scope,
        "owner": owner,
        "role": role,
        "extension": extension,
        "sha256": digest,
        "bytes": len(payload),
        "blob": f".hvp/staging/intake/{stage_id}/blob",
        "created_at": now,
        "expires_at": now + STAGE_TTL_SECONDS,
    }
    lock = project / ".hvp/intake.lock"
    with lock.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _cleanup_expired_stages(workspace, project, root, now)
        if destination.exists():
            return _public_stage(
                _read_stage_manifest(workspace, project, stage_id, owner, now)
            )
        if _staged_bytes(root) + len(payload) > PROJECT_STAGE_QUOTA:
            raise IntakeError("stage_quota_exceeded")
        return _commit_stage(project, root, destination, manifest_value, payload)


def _commit_stage(
    project: Path,
    root: Path,
    destination: Path,
    manifest_value: dict,
    payload: bytes,
) -> dict:
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
    return _public_stage(manifest_value)


def _read_stage_manifest(
    workspace: Path,
    project: Path,
    stage_id: str,
    owner: str,
    now: int | None = None,
    *,
    allow_expired: bool = False,
) -> dict:
    if STAGE_ID.fullmatch(stage_id) is None:
        raise IntakeError("invalid_stage_id")
    workspace_id, project_scope, owner = _stage_binding(workspace, project, owner)
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
    if not isinstance(value, dict) or value.get("schema") != STORED_STAGE_SCHEMA:
        raise IntakeError("stage_invalid")
    if value.get("stage_id") != stage_id:
        raise IntakeError("stage_invalid")
    if (
        value.get("workspace_id") != workspace_id
        or value.get("project_scope") != project_scope
    ):
        raise IntakeError("stage_invalid")
    if value.get("owner") != owner:
        raise IntakeError("stage_owner_mismatch")
    if value.get("blob") != f".hvp/staging/intake/{stage_id}/blob":
        raise IntakeError("stage_invalid")
    if not isinstance(value.get("sha256"), str) or re.fullmatch(
        r"[0-9a-f]{64}", value["sha256"]
    ) is None:
        raise IntakeError("stage_invalid")
    if not isinstance(value.get("bytes"), int) or isinstance(value.get("bytes"), bool):
        raise IntakeError("stage_invalid")
    created_at = value.get("created_at")
    expires_at = value.get("expires_at")
    if (
        not isinstance(created_at, int)
        or isinstance(created_at, bool)
        or not isinstance(expires_at, int)
        or isinstance(expires_at, bool)
        or expires_at != created_at + STAGE_TTL_SECONDS
    ):
        raise IntakeError("stage_invalid")
    if not allow_expired and expires_at <= (int(time.time()) if now is None else now):
        raise IntakeError("stage_expired")
    _extensions, limit, _kind = _validate_role(value.get("role"))
    payload = _read_bounded(blob, limit)
    if hashlib.sha256(payload).hexdigest() != value.get("sha256") or len(payload) != value.get("bytes"):
        raise IntakeError("stage_invalid")
    _validate_payload(value.get("role"), value.get("extension"), payload)
    expected_id = _stage_id(
        workspace_id,
        project_scope,
        owner,
        value["role"],
        value["extension"],
        value["sha256"],
    )
    if expected_id != stage_id:
        raise IntakeError("stage_invalid")
    return value


def stage_inbox(
    workspace_value: Path,
    project_value: Path,
    role: str,
    relative: str,
    owner: str = LOCAL_OPERATOR,
) -> dict:
    workspace = _workspace(workspace_value)
    project = _project(workspace, project_value)
    with workspace_barrier.mutation_barrier(project):
        source = _relative_file(workspace / "inbox", relative)
        extensions, limit, _kind = _validate_role(role)
        extension = source.suffix.lower()
        size = source.stat().st_size
        if extension not in extensions or size > limit:
            raise IntakeError(
                "artifact_too_large"
                if size > limit
                else "artifact_type_invalid"
            )
        if size > PROJECT_STAGE_QUOTA:
            raise IntakeError("stage_quota_exceeded")
        return _stage_bytes(
            workspace, project, owner, role, extension, _read_bounded(source, limit)
        )


def stage_text(
    workspace_value: Path,
    project_value: Path,
    role: str,
    text: str,
    owner: str = LOCAL_OPERATOR,
) -> dict:
    workspace = _workspace(workspace_value)
    project = _project(workspace, project_value)
    with workspace_barrier.mutation_barrier(project):
        if role not in INLINE_ROLES:
            raise IntakeError("inline_role_invalid")
        payload = text.encode("utf-8")
        if len(payload) > INLINE_LIMIT:
            raise IntakeError("artifact_too_large")
        return _stage_bytes(workspace, project, owner, role, INLINE_ROLES[role], payload)


def stage_inline_request(
    workspace_value: Path,
    project_value: Path,
    role: str,
    request_id: str,
    owner: str = LOCAL_OPERATOR,
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
        staged = _stage_bytes(
            workspace, project, owner, role, INLINE_ROLES[role], payload
        )
        request.unlink()
        return staged


def import_stage(
    workspace_value: Path,
    project_value: Path,
    stage_id: str,
    owner: str = LOCAL_OPERATOR,
) -> dict:
    workspace = _workspace(workspace_value)
    project = _project(workspace, project_value)
    with workspace_barrier.mutation_barrier(project):
        lock = project / ".hvp/intake.lock"
        with lock.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            stage = _read_stage_manifest(workspace, project, stage_id, owner)
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


def resolve_stage(
    workspace_value: Path,
    project_value: Path,
    stage_id: str,
    owner: str = LOCAL_OPERATOR,
) -> dict:
    workspace = _workspace(workspace_value)
    project = _project(workspace, project_value)
    return _public_stage(_read_stage_manifest(workspace, project, stage_id, owner))


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
    stage_action = action in {"stage-inbox", "stage-text", "stage-inline-file"}
    valid = stage_action and len(argv) in {6, 7}
    valid = valid or (action in {"import", "resolve"} and len(argv) in {5, 6})
    if not valid:
        print(json.dumps(envelope(None, "error", "invalid_input"), separators=(",", ":")))
        return 2
    project = Path(argv[3])
    owner_index = 6 if stage_action else 5
    owner = argv[owner_index] if len(argv) > owner_index else LOCAL_OPERATOR
    try:
        if action == "stage-inbox":
            data = stage_inbox(Path(argv[2]), project, argv[4], argv[5], owner)
            code = "artifact_staged"
        elif action == "stage-text":
            data = stage_text(Path(argv[2]), project, argv[4], argv[5], owner)
            code = "artifact_staged"
        elif action == "stage-inline-file":
            data = stage_inline_request(Path(argv[2]), project, argv[4], argv[5], owner)
            code = "artifact_staged"
        elif action == "import":
            data = import_stage(Path(argv[2]), project, argv[4], owner)
            code = "artifact_imported"
        else:
            data = resolve_stage(Path(argv[2]), project, argv[4], owner)
            code = "artifact_staged"
        response, exit_code = envelope(project.resolve(), "ok", code, data), 0
    except IntakeError as error:
        outcome = (
            "blocked"
            if error.code
            in {"stage_conflict", "import_conflict", "stage_quota_exceeded"}
            else "error"
        )
        exit_code = 3 if outcome == "blocked" else 2
        response = envelope(project, outcome, error.code)
    except (OSError, TypeError, ValueError):
        response, exit_code = envelope(project, "error", "invalid_input"), 2
    print(json.dumps(response, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
