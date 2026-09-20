#!/usr/bin/env python3
"""Off-project completion proof for the fixed final-quality runner.

Project receipts remain inspectable evidence, but they are not authority: the
status reader accepts them only when this owner-only state binds their exact
bytes to the exact mechanical inputs used by the runner.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
from pathlib import Path

import self_eval_authority as authority


SCHEMA = "haru.final_quality_authority.v1"
DIRECTORY = "final-quality"
LEAF = "current.json"
INPUT_PATHS = {
    "final_video": "output/final.mp4",
    "render_result": "output/final.mp4.render-result",
    "render_self_eval": "quality-review/render-self-eval/render-self-eval.json",
    "visual_qa_review": "quality-review/visual-sampling/visual-qa-review.json",
}
OUTPUT_PATHS = {
    "prep": "quality-review/final-v1/prep.json",
    "review": "quality-review/final-v1/review.json",
}
KEYS = {
    "schema",
    "project_id",
    "project_root",
    "project_path_sha256",
    "recorded_at",
    "inputs",
    "outputs",
}


class FinalQualityAuthorityError(ValueError):
    """Protected final-quality state is missing, unsafe, or inconsistent."""


def _project(project) -> Path:
    root = Path(os.path.abspath(os.fspath(project)))
    if root.is_symlink() or not root.is_dir():
        raise FinalQualityAuthorityError("project root must be a direct directory")
    return root


def _refs(value, expected_paths: dict) -> bool:
    return bool(
        isinstance(value, dict)
        and set(value) == set(expected_paths)
        and all(
            authority.is_ref(value.get(name))
            and value[name]["path"] == path
            and value[name]["bytes"] > 0
            for name, path in expected_paths.items()
        )
    )


def _all_refs(inputs: dict, outputs: dict) -> list:
    return [
        *(inputs[name] for name in INPUT_PATHS),
        *(outputs[name] for name in OUTPUT_PATHS),
    ]


def _owner_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return bool(
        not path.is_symlink()
        and stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid()
        and stat.S_IMODE(info.st_mode) == 0o700
    )


def _open_proof_dir(namespace, *, create: bool) -> int:
    try:
        info = os.stat(DIRECTORY, dir_fd=namespace.fd, follow_symlinks=False)
    except FileNotFoundError:
        if not create:
            raise FinalQualityAuthorityError("final-quality authority is missing")
        try:
            os.mkdir(DIRECTORY, 0o700, dir_fd=namespace.fd)
        except OSError as exc:
            raise FinalQualityAuthorityError(
                "cannot create final-quality authority"
            ) from exc
        info = os.stat(DIRECTORY, dir_fd=namespace.fd, follow_symlinks=False)
    except OSError as exc:
        raise FinalQualityAuthorityError(
            "final-quality authority is unavailable"
        ) from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise FinalQualityAuthorityError(
            "final-quality authority permissions are invalid"
        )
    try:
        return authority.open_dir_at(namespace.fd, DIRECTORY)
    except authority.AuthorityError as exc:
        raise FinalQualityAuthorityError(str(exc)) from exc


def _value(project: Path, inputs: dict, outputs: dict, recorded_at: str) -> dict:
    return {
        "schema": SCHEMA,
        "project_id": project.name,
        "project_root": str(project),
        "project_path_sha256": authority.project_path_sha256(project),
        "recorded_at": recorded_at,
        "inputs": inputs,
        "outputs": outputs,
    }


def _record(
    project, inputs: dict, outputs: dict, recorded_at: str | None = None
) -> dict:
    """Low-level runner/fixture writer; ordinary callers must use validate()."""
    root = _project(project)
    if not _refs(inputs, INPUT_PATHS) or not _refs(outputs, OUTPUT_PATHS):
        raise FinalQualityAuthorityError("final-quality refs are malformed")
    refs = _all_refs(inputs, outputs)
    try:
        with authority.locked(root, root.name) as namespace:
            if authority.verify_refs(root, refs) != "match":
                raise FinalQualityAuthorityError("final-quality refs are not current")
            proof = _value(root, inputs, outputs, recorded_at or authority.now_iso())
            descriptor = _open_proof_dir(namespace, create=True)
            try:
                authority.atomic_leaf_at(
                    descriptor, LEAF, authority.canonical_bytes(proof)
                )
            finally:
                os.close(descriptor)
            if authority.verify_refs(root, refs) != "match":
                raise FinalQualityAuthorityError(
                    "final-quality refs changed during recording"
                )
    except authority.AuthorityError as exc:
        raise FinalQualityAuthorityError(str(exc)) from exc
    return proof


def _read_locked(project: Path) -> dict:
    state = authority.state_root()
    if not state.is_absolute() or not _owner_directory(state):
        raise FinalQualityAuthorityError("protected state root is unavailable")
    namespace_path = state / authority.project_path_sha256(project)
    proof_path = namespace_path / DIRECTORY
    if not _owner_directory(namespace_path) or not _owner_directory(proof_path):
        raise FinalQualityAuthorityError("final-quality authority is unavailable")
    namespace_fd = os.open(namespace_path, authority.DIR_FLAGS)
    lock_fd = proof_fd = directory_fd = -1
    try:
        lock_fd = os.open("lock", os.O_RDONLY | authority.NOFOLLOW, dir_fd=namespace_fd)
        info = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise FinalQualityAuthorityError("protected authority lock is invalid")
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        directory_fd = authority.open_dir_at(namespace_fd, DIRECTORY)
        proof_fd = authority.open_protected_file_at(directory_fd, LEAF)
        payload = authority.read_fd(proof_fd)
        value = json.loads(
            payload.decode("utf-8"), object_pairs_hook=authority._strict_object
        )
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        authority.AuthorityError,
    ) as exc:
        raise FinalQualityAuthorityError(
            "final-quality authority is malformed"
        ) from exc
    finally:
        if lock_fd >= 0:
            with contextlib.suppress(OSError):
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        for descriptor in (proof_fd, directory_fd, lock_fd, namespace_fd):
            if descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
    if not isinstance(value, dict):
        raise FinalQualityAuthorityError("final-quality authority is malformed")
    return value


def validate(project, expected_inputs: dict, expected_outputs: dict) -> bool:
    """Return whether the protected proof binds these exact live project refs."""
    try:
        root = _project(project)
        if not _refs(expected_inputs, INPUT_PATHS) or not _refs(
            expected_outputs, OUTPUT_PATHS
        ):
            return False
        refs = _all_refs(expected_inputs, expected_outputs)
        if authority.verify_refs(root, refs) != "match":
            return False
        value = _read_locked(root)
        expected = _value(
            root, expected_inputs, expected_outputs, value.get("recorded_at")
        )
        if (
            set(value) != KEYS
            or not isinstance(value.get("recorded_at"), str)
            or authority.UTC_TIMESTAMP.fullmatch(value["recorded_at"]) is None
            or value != expected
        ):
            return False
        return authority.verify_refs(root, refs) == "match"
    except (OSError, TypeError, ValueError, authority.AuthorityError):
        return False
