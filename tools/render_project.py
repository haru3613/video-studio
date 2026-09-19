#!/usr/bin/env python3
"""Start or resume the one supported detached HVP render lifecycle."""

from __future__ import annotations

import json
import os
import subprocess
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import canonical_layout  # noqa: E402
import portable_jobs  # noqa: E402
import render_contract  # noqa: E402
import segment_assembly  # noqa: E402


JOB_SCHEMA = portable_jobs.JOB_SCHEMA


def direct_directory(path: Path) -> Path:
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("directory")
    return path.resolve(strict=True)


def direct_file(path: Path) -> Path:
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("file")
    return path.resolve(strict=True)


def direct_project_path(project: Path, relative: str, kind: str) -> Path | None:
    candidate = project
    for part in Path(relative).parts:
        candidate /= part
        if candidate.is_symlink():
            return None
    try:
        candidate.resolve(strict=True).relative_to(project.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return None
    if kind == "file" and not candidate.is_file():
        return None
    if kind == "directory" and not candidate.is_dir():
        return None
    return candidate


def read_json(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def write_json_atomically(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(value, handle, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def envelope(project: Path, outcome: str, code: str, data=None) -> dict:
    return {
        "schema_version": 1,
        "outcome": outcome,
        "code": code,
        "project": str(project),
        "data": data,
    }


def valid_final_result(project: Path, marker: dict, video: Path) -> bool:
    direct_video = direct_project_path(project, "output/final.mp4", "file")
    direct_marker = direct_project_path(
        project, "output/final.mp4.render-result", "file"
    )
    return bool(
        direct_video == video
        and direct_marker is not None
        and render_contract.valid_final_result(project, marker, video)
    )


def valid_job(project: Path, job: dict | None) -> bool:
    if not isinstance(job, dict):
        return False
    return bool(
        job.get("schema") == JOB_SCHEMA
        and job.get("project") == project.name
        and job.get("status")
        in {
            "queued",
            "running",
            "cancel_requested",
            "promoting",
            "succeeded",
            "failed",
            "cancelled",
            "interrupted",
        }
        and job.get("launcher") == "portable-python"
        and isinstance(job.get("job_id"), str)
        and isinstance(job.get("epoch"), int)
        and render_contract.valid_sha256(job.get("revision"), lowercase=True)
        and job.get("output") == "output/final.mp4"
    )


def cleanup_job(project: Path, job: dict | None) -> None:
    # Durable records are evidence and are never deleted as cleanup.  This
    # compatibility function intentionally only removes an old malformed
    # projection when the state directory itself is direct and safe.
    if valid_job(project, job):
        return
    state = direct_project_path(project, ".hvp", "directory")
    projection = state / "render-job.json" if state is not None else None
    if projection is not None and projection.is_file() and not projection.is_symlink():
        try:
            projection.unlink()
        except OSError:
            pass


def validate_plan(project: Path, tools: Path, worker: Path) -> None:
    result = subprocess.run(
        [worker, "--validate", project, tools],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        response = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        response = None
    if (
        result.returncode != 0
        or not isinstance(response, dict)
        or response.get("outcome") != "ok"
        or response.get("code") != "render_plan_valid"
    ):
        raise ValueError("render plan")


def start_job(project: Path, tools: Path) -> dict:
    repo = Path(__file__).resolve().parents[1]
    worker = direct_file(repo / "tools/render_project_worker.py")
    validate_plan(project, tools, worker)
    job, _created = portable_jobs.submit(project, tools, worker)
    return {
        "schema": JOB_SCHEMA,
        "job_id": job["job_id"],
        "project": project.name,
        "status": job["status"],
        "launcher": "portable-python",
        "epoch": job["epoch"],
        "revision": job["revision"],
        "pid": job.get("pid"),
        "log": str(project / "output/.staging" / job["job_id"] / "worker.log"),
        "output": "output/final.mp4",
        "started_at": job["created_at"],
    }


def superseded_paths(output: Path, preserve_assembly: bool = False) -> tuple:
    """Files the worker treats as evidence that this final render already ran."""
    stem = output.name.removesuffix(".mp4")
    paths = [
        output,
        output.with_name(f"{stem}.pre-loudnorm.raw.mp4"),
        Path(str(output) + ".render-result"),
        Path(str(output) + ".render.log"),
        Path(str(output) + ".rendering"),
    ]
    if not preserve_assembly:
        paths.extend(
            [
                output.with_name(f"{stem}.pre-loudnorm.mp4"),
                output.parent.parent / "quality-review/segments/assembly.json",
            ]
        )
    return tuple(paths)


def retire_superseded(
    project: Path, marker: dict, output: Path, marker_path: Path
) -> list:
    """Move a superseded render aside, keeping it readable.

    Suffixed with the narration it was rendered against, so a retired file says
    which version it is rather than merely that it is old.
    """
    stamp = str(marker.get("narration_sha256") or "unknown")[:12]
    preserve_assembly = segment_assembly.current_receipt(project) is not None
    moves = []
    descriptors = []
    try:
        for path in superseded_paths(output, preserve_assembly):
            direct_parent = canonical_layout.direct_path(path.parent, project)
            if direct_parent is None:
                raise ValueError("unsafe superseded render parent")
            if not direct_parent.exists():
                continue
            if not direct_parent.is_dir():
                raise ValueError("unsafe superseded render parent")
            try:
                descriptor = canonical_layout.open_direct_directory_fd(
                    path.parent, project
                )
            except OSError as error:
                raise ValueError("unsafe superseded render parent") from error
            descriptors.append(descriptor)
            try:
                source_stat = os.stat(
                    path.name, dir_fd=descriptor, follow_symlinks=False
                )
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(source_stat.st_mode):
                raise ValueError("unsafe superseded render path")
            target_name = f"{path.name}.superseded-{stamp}"
            try:
                target_stat = os.stat(
                    target_name, dir_fd=descriptor, follow_symlinks=False
                )
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISREG(target_stat.st_mode):
                    raise ValueError("unsafe superseded render target")
            moves.append((descriptor, path.name, target_name))
        for descriptor, source_name, target_name in moves:
            os.replace(
                source_name,
                target_name,
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
            )
        return [target_name for _, _, target_name in moves]
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def run(project_value, tools_value) -> tuple[dict, int]:
    project = direct_directory(Path(project_value))
    tools = direct_directory(Path(tools_value))
    output = project / "output/final.mp4"
    marker_path = Path(str(output) + ".render-result")
    marker_present = marker_path.exists() or marker_path.is_symlink()
    direct_marker = direct_project_path(
        project, "output/final.mp4.render-result", "file"
    )
    marker = read_json(direct_marker) if direct_marker else None
    if marker_present and marker is None:
        return envelope(project, "error", "render_result_invalid"), 4
    job_path = project / ".hvp/render-job.json"
    job_present = job_path.exists() or job_path.is_symlink()
    direct_job = direct_project_path(project, ".hvp/render-job.json", "file")
    job = read_json(direct_job) if direct_job else None
    if job_present and (job is None or not valid_job(project, job)):
        return envelope(project, "error", "render_job_invalid"), 4

    if marker is not None:
        if valid_final_result(project, marker, output):
            return envelope(project, "ok", "render_complete", marker), 0
        if marker.get("status") == "failed":
            return envelope(project, "error", "render_failed", marker), 4
        if render_contract.superseded_final_result(project, marker, output):
            # Keep the last playable version in place while the detached retry
            # renders in staging. Promotion preserves it as superseded only
            # after the new candidate passes every original worker gate.
            durable = portable_jobs.get_job(project)
            if durable is not None:
                if durable["status"] in portable_jobs.ACTIVE:
                    receipt = {
                        "schema": JOB_SCHEMA,
                        "job_id": durable["job_id"],
                        "project": project.name,
                        "status": durable["status"],
                        "launcher": "portable-python",
                        "epoch": durable["epoch"],
                        "revision": durable["revision"],
                        "pid": durable.get("pid"),
                        "log": str(
                            project
                            / "output/.staging"
                            / durable["job_id"]
                            / "worker.log"
                        ),
                        "output": "output/final.mp4",
                        "started_at": durable["created_at"],
                    }
                    return envelope(project, "ok", "render_running", receipt), 0
                if durable["status"] == "failed":
                    return envelope(project, "error", "render_failed", durable), 4
                if durable["status"] in {"cancelled", "interrupted"}:
                    return envelope(project, "error", "render_aborted", durable), 4
                if durable["status"] != "succeeded":
                    return envelope(
                        project, "error", "render_result_invalid", durable
                    ), 4
            receipt = start_job(project, tools)
            return envelope(
                project,
                "ok",
                "render_started",
                {**receipt, "previous_final_preserved": True},
            ), 0
        return envelope(project, "error", "render_result_invalid", marker), 4

    durable = portable_jobs.get_job(project)
    if durable is not None:
        job = {
            "schema": JOB_SCHEMA,
            "job_id": durable["job_id"],
            "project": project.name,
            "status": durable["status"],
            "launcher": "portable-python",
            "epoch": durable["epoch"],
            "revision": durable["revision"],
            "pid": durable.get("pid"),
            "log": str(project / "output/.staging" / durable["job_id"] / "worker.log"),
            "output": "output/final.mp4",
            "started_at": durable["created_at"],
        }
        if durable["status"] in portable_jobs.ACTIVE:
            return envelope(project, "ok", "render_running", job), 0
        if durable["status"] == "succeeded":
            # A succeeded row without a valid canonical marker is evidence of a
            # broken promotion, never permission to start over.
            return envelope(project, "error", "render_result_invalid", job), 4
        if durable["status"] == "failed":
            return envelope(project, "error", "render_failed", job), 4
        return envelope(project, "error", "render_aborted", job), 4

    if output.exists() or output.is_symlink():
        return envelope(project, "blocked", "output_exists"), 3
    receipt = start_job(project, tools)
    return envelope(project, "ok", "render_started", receipt), 0


def job_status(project_value, job_id=None) -> tuple[dict, int]:
    project = direct_directory(Path(project_value))
    job = portable_jobs.get_job(project, job_id)
    if job is None:
        return envelope(project, "error", "job_not_found"), 2
    return envelope(project, "ok", "job_status", job), 0


def cancel_job(project_value, job_id) -> tuple[dict, int]:
    project = direct_directory(Path(project_value))
    job = portable_jobs.cancel(project, job_id)
    return envelope(project, "ok", "job_cancelled", job), 0


def resume_job(project_value, job_id) -> tuple[dict, int]:
    project = direct_directory(Path(project_value))
    job = portable_jobs.resume(project, job_id)
    return envelope(project, "ok", "job_resumed", job), 0


def main(argv: list[str]) -> int:
    action = "run"
    if len(argv) > 1 and argv[1] in {"--status", "--cancel", "--resume"}:
        action = argv[1][2:]
    valid = (
        (action == "run" and len(argv) == 3)
        or (action == "status" and len(argv) in {3, 4})
        or (action in {"cancel", "resume"} and len(argv) == 4)
    )
    if not valid:
        print(
            json.dumps(
                envelope(Path("."), "error", "invalid_input"), separators=(",", ":")
            )
        )
        return 2
    try:
        if action == "run":
            response, exit_code = run(argv[1], argv[2])
        elif action == "status":
            response, exit_code = job_status(
                argv[2], argv[3] if len(argv) == 4 else None
            )
        elif action == "cancel":
            response, exit_code = cancel_job(argv[2], argv[3])
        else:
            response, exit_code = resume_job(argv[2], argv[3])
    except (OSError, TypeError, ValueError):
        project_arg = argv[1] if action == "run" else argv[2]
        response, exit_code = envelope(Path(project_arg), "error", "invalid_input"), 2
    print(json.dumps(response, separators=(",", ":")))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
