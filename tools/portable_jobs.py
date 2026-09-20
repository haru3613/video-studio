#!/usr/bin/env python3
"""Durable, cross-platform supervision for detached render workers.

The database and every candidate live inside the project.  A worker renders a
snapshot and can only ask this module to commit it; epoch, source revision,
snapshot digest, process identity and candidate verification are checked again
at that boundary.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import render_contract


SCHEMA = "video-studio.jobs.v1"
JOB_SCHEMA = "haru.render_job.v2"
ACTIVE = ("queued", "running", "cancel_requested", "promoting")
TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")
MUTABLE_OUTPUTS = {
    "output/final.mp4",
    "output/final.mp4.render-result",
    "output/final.mp4.render.log",
    "output/final.mp4.rendering",
    "output/final.pre-loudnorm.raw.mp4",
}
_CHILDREN = {}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _direct_directory(value: Path) -> Path:
    path = Path(value)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("directory")
    return path.resolve(strict=True)


def _state(project: Path) -> Path:
    state = project / ".hvp"
    if state.is_symlink():
        raise ValueError("state")
    state.mkdir(mode=0o700, exist_ok=True)
    state = _direct_directory(state)
    try:
        os.chmod(state, 0o700)
    except OSError:
        pass
    return state


def _connect(project: Path) -> sqlite3.Connection:
    database = _state(project) / "jobs.sqlite3"
    connection = sqlite3.connect(database, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
          job_id TEXT PRIMARY KEY,
          kind TEXT NOT NULL CHECK(kind = 'render'),
          project TEXT NOT NULL,
          tools_root TEXT NOT NULL,
          worker TEXT NOT NULL,
          status TEXT NOT NULL,
          epoch INTEGER NOT NULL,
          revision TEXT NOT NULL,
          snapshot_digest TEXT,
          snapshot_root TEXT,
          candidate_root TEXT,
          pid INTEGER,
          pid_token TEXT,
          process_group INTEGER,
          exit_code INTEGER,
          error_code TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_render
          ON jobs(project) WHERE status IN
          ('queued','running','cancel_requested','promoting');
        """
    )
    try:
        os.chmod(database, 0o600)
    except OSError:
        pass
    return connection


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def _projection(project: Path, job: dict) -> None:
    value = {
        "schema": JOB_SCHEMA,
        "job_id": job["job_id"],
        "project": project.name,
        "status": job["status"],
        "launcher": "portable-python",
        "epoch": job["epoch"],
        "revision": job["revision"],
        "pid": job.get("pid"),
        "log": str(_job_root(project, job["job_id"]) / "worker.log"),
        "output": "output/final.mp4",
        "updated_at": job["updated_at"],
    }
    _atomic_json(_state(project) / "render-job.json", value)


def _atomic_json(path: Path, value: dict) -> None:
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
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _job_root(project: Path, job_id: str) -> Path:
    return project / "output/.staging" / job_id


def _attempt_root(project: Path, job_id: str, epoch: int) -> Path:
    return _job_root(project, job_id) / f"attempt-{epoch}"


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _generated_cache(relative: str) -> bool:
    # Webpack writes compiler caches while rendering. Do not admit a caller's
    # prebuilt cache into a snapshot, or treat fresh compiler output as source.
    parts = relative.split("/")
    return any(
        parts[index : index + 2] == ["node_modules", ".cache"]
        for index in range(len(parts) - 1)
    )


def _copy_excluded(relative: str) -> bool:
    return (
        _generated_cache(relative)
        or relative == ".hvp"
        or relative.startswith(".hvp/")
        or relative == "output/versions"
        or relative.startswith("output/versions/")
        or relative == "output/.staging"
        or relative.startswith("output/.staging/")
        or relative in MUTABLE_OUTPUTS
        or ".superseded-" in relative
    )


def _validate_links(project: Path) -> None:
    for root, directories, files in os.walk(project, followlinks=False):
        root_path = Path(root)
        relative_root = _relative(root_path, project) if root_path != project else ""
        directories[:] = [
            name
            for name in directories
            if not _copy_excluded("/".join(filter(None, (relative_root, name))))
        ]
        for name in [*directories, *files]:
            path = root_path / name
            relative = "/".join(filter(None, (relative_root, name)))
            if _copy_excluded(relative) or not path.is_symlink():
                continue
            target = os.readlink(path)
            if os.path.isabs(target):
                raise ValueError("absolute symlink in project inputs")
            try:
                path.resolve(strict=True).relative_to(project)
            except (OSError, RuntimeError, ValueError) as error:
                raise ValueError("escaping symlink in project inputs") from error


def _digest_tree(root: Path) -> str:
    return render_contract.render_input_revision(root)


def project_revision(project: Path) -> str:
    project = _direct_directory(project)
    _validate_links(project)
    return _digest_tree(project)


def _snapshot(project: Path, job_id: str, epoch: int) -> tuple[Path, str]:
    attempt = _attempt_root(project, job_id, epoch)
    if attempt.exists() or attempt.is_symlink():
        raise ValueError("attempt already exists")
    attempt.mkdir(parents=True)
    snapshot = attempt / "snapshot" / project.name

    def ignore(directory: str, names: list[str]) -> set[str]:
        directory_path = Path(directory)
        relative_root = (
            _relative(directory_path, project) if directory_path != project else ""
        )
        return {
            name
            for name in names
            if _copy_excluded("/".join(filter(None, (relative_root, name))))
        }

    _validate_links(project)
    shutil.copytree(project, snapshot, symlinks=True, ignore=ignore)
    import template_trust

    template_trust.copy_authority(project, snapshot)
    # A non-segmented worker refuses a left-over premix. A segmented worker
    # needs the assembly premix and its receipt, so preserve it only when the
    # canonical segment plan is present.
    if not (snapshot / "segment-plan.json").is_file():
        try:
            (snapshot / "output/final.pre-loudnorm.mp4").unlink()
        except FileNotFoundError:
            pass
    snapshot_digest = _digest_tree(snapshot)
    source_template = _template_authorization(project)
    snapshot_template = _template_authorization(snapshot)
    if source_template.get("template_digest") != snapshot_template.get(
        "template_digest"
    ):
        raise ValueError("template changed while snapshotting")
    _atomic_json(
        attempt / "snapshot.json",
        {
            "schema": "video-studio.render_snapshot.v1",
            "job_id": job_id,
            "epoch": epoch,
            "snapshot_digest": snapshot_digest,
            "template_digest": snapshot_template.get("template_digest"),
        },
    )
    return snapshot, snapshot_digest


def _template_authorization(project: Path) -> dict:
    # A validated segmented plan reuses already rendered segment media and does
    # not execute the project's Remotion source in the final assembly worker.
    if (project / "segment-plan.json").is_file():
        return {"template_digest": None, "mode": "not_applicable"}
    import template_trust

    return template_trust.authorize(project)


def _parse_linux_proc_stat(value: str) -> tuple[str, str] | None:
    # The comm field is parenthesized and may itself contain spaces or closing
    # parentheses. Split only after its final delimiter so field 22 remains the
    # process start time used to fence PID reuse.
    closing = value.rfind(")")
    if closing < 0:
        return None
    fields = value[closing + 1 :].split()
    if len(fields) <= 19 or len(fields[0]) != 1 or not fields[19].isdigit():
        return None
    return fields[0], fields[19]


def _process_token(pid: int) -> str | None:
    child = _CHILDREN.get(pid)
    if child is not None and child.poll() is not None:
        if _CHILDREN.get(pid) is child:
            _CHILDREN.pop(pid, None)
        return None
    if sys.platform.startswith("linux"):
        try:
            parsed = _parse_linux_proc_stat(
                Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            )
            if parsed is None:
                return None
            state, started_at = parsed
            if state in {"Z", "X", "x"}:
                child = _CHILDREN.get(pid)
                if child is not None and child.poll() is not None:
                    if _CHILDREN.get(pid) is child:
                        _CHILDREN.pop(pid, None)
                return None
            return f"linux:{started_at}"
        except OSError:
            return None
    try:
        result = subprocess.run(
            ["/bin/ps", "-ww", "-o", "lstart=", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    value = result.stdout.strip()
    return f"ps:{value}" if result.returncode == 0 and value else None


def _same_process(job: dict) -> bool:
    pid = job.get("pid")
    token = job.get("pid_token")
    group = job.get("process_group")
    if not isinstance(pid, int) or not token or group != pid:
        return False
    if _process_token(pid) != token:
        process = _CHILDREN.pop(pid, None)
        if process is not None:
            process.poll()
        return False
    try:
        return os.getpgid(pid) == group
    except (OSError, ProcessLookupError):
        return False


def _spawn(project: Path, job: dict) -> None:
    root = _job_root(project, job["job_id"])
    root.mkdir(parents=True, exist_ok=True)
    log = root / "worker.log"
    runtime_home = root / "runtime-home"
    runtime_home.mkdir(exist_ok=True)
    environment = {
        "HOME": str(runtime_home),
        "LANG": "C.UTF-8",
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "PYTHONUTF8": "1",
    }
    browser = os.environ.get("VIDEO_STUDIO_CHROMIUM")
    if browser:
        browser_path = Path(browser)
        if (
            not browser_path.is_absolute()
            or browser_path.is_symlink()
            or not browser_path.is_file()
            or not os.access(browser_path, os.X_OK)
        ):
            raise ValueError("trusted browser path")
        environment["VIDEO_STUDIO_CHROMIUM"] = str(browser_path.resolve(strict=True))
    with log.open("ab", buffering=0) as output:
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                job["worker"],
                "--job",
                str(project),
                job["tools_root"],
                job["job_id"],
                str(job["epoch"]),
            ],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            env=environment,
            start_new_session=True,
            close_fds=True,
        )
    _CHILDREN[process.pid] = process


def _wait_for_claim(project: Path, job_id: str, timeout: float = 2.0) -> dict:
    deadline = time.monotonic() + timeout
    job = get_job(project, job_id, refresh=False)
    while job and job["status"] == "queued" and time.monotonic() < deadline:
        time.sleep(0.02)
        job = get_job(project, job_id, refresh=False)
    return job


def submit(
    project_value: Path, tools_value: Path, worker_value: Path
) -> tuple[dict, bool]:
    project = _direct_directory(project_value)
    tools = _direct_directory(tools_value)
    worker = Path(worker_value).resolve(strict=True)
    if worker.is_symlink() or not worker.is_file():
        raise ValueError("worker")
    revision = project_revision(project)
    connection = _connect(project)
    try:
        connection.execute("BEGIN IMMEDIATE")
        active = connection.execute(
            "SELECT * FROM jobs WHERE project=? AND status IN (?,?,?,?) ORDER BY created_at DESC LIMIT 1",
            (str(project), *ACTIVE),
        ).fetchone()
        if active is not None:
            connection.execute("COMMIT")
            job = _row(active)
            _projection(project, job)
            return job, False
        job_id = uuid.uuid4().hex
        now = _now()
        connection.execute(
            """INSERT INTO jobs
               (job_id,kind,project,tools_root,worker,status,epoch,revision,created_at,updated_at)
               VALUES (?,?,?,?,?,'queued',1,?,?,?)""",
            (
                job_id,
                "render",
                str(project),
                str(tools),
                str(worker),
                revision,
                now,
                now,
            ),
        )
        connection.execute("COMMIT")
        try:
            snapshot, snapshot_digest = _snapshot(project, job_id, 1)
            if snapshot_digest != revision or project_revision(project) != revision:
                raise ValueError("project changed while snapshotting")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE jobs SET snapshot_root=?,candidate_root=?,snapshot_digest=?,updated_at=? WHERE job_id=? AND epoch=1 AND status='queued'",
                (
                    str(snapshot),
                    str(snapshot / "output"),
                    snapshot_digest,
                    _now(),
                    job_id,
                ),
            )
            connection.execute("COMMIT")
            job = get_job(project, job_id, refresh=False)
            _spawn(project, job)
            job = _wait_for_claim(project, job_id)
        except Exception:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE jobs SET status='failed',error_code='job_start_failed',updated_at=? WHERE job_id=? AND status='queued'",
                (_now(), job_id),
            )
            connection.execute("COMMIT")
            raise
        _projection(project, job)
        return job, True
    finally:
        connection.close()


def get_job(
    project_value: Path, job_id: str | None = None, refresh: bool = True
) -> dict | None:
    project = _direct_directory(project_value)
    connection = _connect(project)
    try:
        if job_id is None:
            row = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        job = _row(row)
        if not job:
            return None
        if (
            refresh
            and job["status"] in ("running", "cancel_requested", "promoting")
            and not _same_process(job)
        ):
            status = (
                "cancelled" if job["status"] == "cancel_requested" else "interrupted"
            )
            if job["status"] == "promoting":
                status = _recover_promotion(project, job)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE jobs SET status=?,error_code=?,updated_at=? WHERE job_id=? AND epoch=? AND status=?",
                (
                    status,
                    None if status == "succeeded" else f"worker_{status}",
                    _now(),
                    job["job_id"],
                    job["epoch"],
                    job["status"],
                ),
            )
            connection.execute("COMMIT")
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id=?", (job["job_id"],)
            ).fetchone()
            job = _row(row)
        _projection(project, job)
        return job
    finally:
        connection.close()


def worker_context(project_value: Path, job_id: str, epoch: int) -> dict | None:
    project = _direct_directory(project_value)
    pid = os.getpid()
    token = _process_token(pid)
    if token is None or os.getpgrp() != pid:
        return None
    connection = _connect(project)
    try:
        connection.execute("BEGIN IMMEDIATE")
        changed = connection.execute(
            """UPDATE jobs SET status='running',pid=?,pid_token=?,process_group=?,updated_at=?
               WHERE job_id=? AND epoch=? AND status='queued'""",
            (pid, token, pid, _now(), job_id, epoch),
        ).rowcount
        row = connection.execute(
            "SELECT * FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        connection.execute("COMMIT")
        job = _row(row)
        if changed != 1 or not job:
            return None
        _projection(project, job)
        return job
    finally:
        connection.close()


def worker_failed(project_value: Path, job_id: str, epoch: int, exit_code: int) -> None:
    project = _direct_directory(project_value)
    connection = _connect(project)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT status FROM jobs WHERE job_id=? AND epoch=?", (job_id, epoch)
        ).fetchone()
        if row is not None:
            status = "cancelled" if row["status"] == "cancel_requested" else "failed"
            connection.execute(
                "UPDATE jobs SET status=?,exit_code=?,error_code=?,updated_at=? WHERE job_id=? AND epoch=? AND status IN ('running','cancel_requested')",
                (status, exit_code, "render_worker_failed", _now(), job_id, epoch),
            )
        connection.execute("COMMIT")
        job = get_job(project, job_id, refresh=False)
        if job:
            _projection(project, job)
    finally:
        connection.close()


def _validated_candidate(project: Path, job: dict) -> tuple[Path, Path, dict]:
    snapshot = _direct_directory(Path(job["snapshot_root"]))
    expected_attempt = _attempt_root(project, job["job_id"], job["epoch"]).resolve()
    snapshot.relative_to(expected_attempt)
    if _digest_tree(snapshot) != job["snapshot_digest"]:
        raise ValueError("snapshot changed")
    snapshot_manifest = json.loads(
        (
            _attempt_root(project, job["job_id"], job["epoch"]) / "snapshot.json"
        ).read_text(encoding="utf-8")
    )
    expected_template = snapshot_manifest.get("template_digest")
    if (
        _template_authorization(snapshot).get("template_digest") != expected_template
        or _template_authorization(project).get("template_digest") != expected_template
    ):
        raise ValueError("template authorization changed")
    video = snapshot / "output/final.mp4"
    marker_path = Path(str(video) + ".render-result")
    if (
        video.is_symlink()
        or marker_path.is_symlink()
        or not video.is_file()
        or not marker_path.is_file()
    ):
        raise ValueError("candidate missing")
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    if marker.get(render_contract.RENDER_INPUT_REVISION_FIELD) != job[
        "revision"
    ] or not render_contract.valid_final_result(snapshot, marker, video):
        raise ValueError("candidate invalid")
    return video, marker_path, marker


def _preserve_previous(output: Path, job_id: str) -> dict:
    marker_path = Path(str(output) + ".render-result")
    if not output.is_file() or output.is_symlink():
        return {}
    from version_archive import preserve_video

    preserve_video(output)
    stamp = job_id[:12]
    if marker_path.is_file() and not marker_path.is_symlink():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            stamp = str(
                marker.get("narration_sha256") or marker.get("video_sha256") or stamp
            )[:12]
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    backups = {}
    for source in (output, marker_path, Path(str(output) + ".render.log")):
        if not source.is_file() or source.is_symlink():
            continue
        target = source.with_name(f"{source.name}.superseded-{stamp}")
        if target.exists():
            target = source.with_name(f"{source.name}.superseded-{stamp}-{job_id[:8]}")
        if source == output:
            os.link(source, target)
        else:
            shutil.copy2(source, target)
        with target.open("rb") as handle:
            os.fsync(handle.fileno())
        backups[source.name] = str(target)
    return backups


def _restore_promotion(project: Path, manifest: dict) -> None:
    output_dir = _direct_directory(project / "output")
    backups = manifest.get("backups")
    if not isinstance(backups, dict):
        raise ValueError("promotion backups")
    for canonical_name in (
        "final.mp4",
        "final.mp4.render-result",
        "final.mp4.render.log",
    ):
        canonical = output_dir / canonical_name
        backup_value = backups.get(canonical_name)
        if isinstance(backup_value, str):
            backup = Path(backup_value)
            backup.resolve(strict=True).relative_to(output_dir)
            if backup.is_symlink() or not backup.is_file():
                raise ValueError("promotion backup")
            os.replace(backup, canonical)
        else:
            try:
                if canonical.is_symlink() or canonical.is_file():
                    canonical.unlink()
            except FileNotFoundError:
                pass
    _fsync_directory(output_dir)


def _promote_files(
    video: Path,
    marker_path: Path,
    output: Path,
    canonical_marker: Path,
) -> None:
    os.replace(video, output)
    os.replace(marker_path, canonical_marker)
    candidate_log = Path(str(video) + ".render.log")
    if candidate_log.is_file() and not candidate_log.is_symlink():
        os.replace(candidate_log, Path(str(output) + ".render.log"))
    _fsync_directory(output.parent)


def _recover_promotion(project: Path, job: dict) -> str:
    manifest_path = _job_root(project, job["job_id"]) / "promotion.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = manifest["candidate_sha256"]
        expected_marker = manifest.get("candidate_marker_sha256")
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return "interrupted"
    output = project / "output/final.mp4"
    marker_path = Path(str(output) + ".render-result")
    try:
        import render_contract

        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if (
            _sha256(output) == expected
            and _sha256(marker_path) == expected_marker
            and marker.get("video_sha256") == expected
            and marker.get(render_contract.RENDER_INPUT_REVISION_FIELD)
            == job["revision"]
            and render_contract.valid_final_result(project, marker, output)
        ):
            return "succeeded"
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        pass
    try:
        _restore_promotion(project, manifest)
    except (OSError, RuntimeError, ValueError):
        return "interrupted"
    return "interrupted"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_promotion_valid(
    project: Path,
    job: dict,
    manifest: dict,
    output: Path,
    marker_path: Path,
) -> bool:
    try:
        if (
            output.is_symlink()
            or marker_path.is_symlink()
            or not output.is_file()
            or not marker_path.is_file()
            or project_revision(project) != job["revision"]
            or _sha256(output) != manifest["candidate_sha256"]
            or _sha256(marker_path) != manifest["candidate_marker_sha256"]
        ):
            return False
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        return bool(
            marker.get("video_sha256") == manifest["candidate_sha256"]
            and marker.get(render_contract.RENDER_INPUT_REVISION_FIELD)
            == job["revision"]
            and render_contract.valid_final_result(project, marker, output)
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def promote_candidate(project_value: Path, job_id: str, epoch: int) -> bool:
    project = _direct_directory(project_value)
    connection = _connect(project)
    promotion_manifest = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        job = _row(row)
        if not job or job["epoch"] != epoch or job["status"] != "running":
            connection.execute("ROLLBACK")
            return False
        if project_revision(project) != job["revision"]:
            connection.execute(
                "UPDATE jobs SET status='interrupted',error_code='project_revision_changed',updated_at=? WHERE job_id=? AND epoch=?",
                (_now(), job_id, epoch),
            )
            connection.execute("COMMIT")
            return False
        video, marker_path, marker = _validated_candidate(project, job)
        output_dir = _direct_directory(project / "output")
        if video.stat().st_dev != output_dir.stat().st_dev:
            raise ValueError("candidate filesystem")
        connection.execute(
            "UPDATE jobs SET status='promoting',updated_at=? WHERE job_id=? AND epoch=? AND status='running'",
            (_now(), job_id, epoch),
        )
        connection.execute("COMMIT")

        output = output_dir / "final.mp4"
        canonical_marker = Path(str(output) + ".render-result")
        backups = _preserve_previous(output, job_id)
        promotion_manifest = {
            "schema": "video-studio.pending_promotion.v1",
            "job_id": job_id,
            "epoch": epoch,
            "candidate_sha256": marker["video_sha256"],
            "candidate_marker_sha256": _sha256(marker_path),
            "backups": backups,
        }
        _atomic_json(_job_root(project, job_id) / "promotion.json", promotion_manifest)
        for candidate in (video, marker_path):
            with candidate.open("rb") as handle:
                os.fsync(handle.fileno())
        _promote_files(video, marker_path, output, canonical_marker)

        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        current = _row(row)
        if (
            not current
            or current["epoch"] != epoch
            or current["status"] != "promoting"
            or not _canonical_promotion_valid(
                project, current, promotion_manifest, output, canonical_marker
            )
        ):
            error_code = (
                "project_revision_changed"
                if current and project_revision(project) != current["revision"]
                else "promotion_binding_changed"
            )
            _restore_promotion(project, promotion_manifest)
            connection.execute(
                "UPDATE jobs SET status='interrupted',exit_code=NULL,error_code=?,updated_at=? WHERE job_id=? AND epoch=? AND status='promoting'",
                (error_code, _now(), job_id, epoch),
            )
            connection.execute("COMMIT")
            current = get_job(project, job_id, refresh=False)
            if current:
                _projection(project, current)
            return False
        changed = connection.execute(
            "UPDATE jobs SET status='succeeded',exit_code=0,error_code=NULL,updated_at=? WHERE job_id=? AND epoch=? AND status='promoting'",
            (_now(), job_id, epoch),
        ).rowcount
        connection.execute("COMMIT")
        job = get_job(project, job_id, refresh=False)
        if job:
            _projection(project, job)
        return changed == 1
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        if promotion_manifest is not None:
            try:
                _restore_promotion(project, promotion_manifest)
            except (OSError, RuntimeError, ValueError):
                pass
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE jobs SET status='failed',error_code='promotion_failed',updated_at=? WHERE job_id=? AND epoch=? AND status IN ('running','promoting')",
                (_now(), job_id, epoch),
            )
            connection.execute("COMMIT")
        except sqlite3.Error:
            pass
        return False
    finally:
        connection.close()


def cancel(project_value: Path, job_id: str) -> dict:
    project = _direct_directory(project_value)
    connection = _connect(project)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        job = _row(row)
        if not job:
            connection.execute("ROLLBACK")
            raise ValueError("job")
        if job["status"] in TERMINAL:
            connection.execute("COMMIT")
            return job
        if job["status"] == "promoting":
            connection.execute("COMMIT")
            return job
        connection.execute(
            "UPDATE jobs SET status='cancel_requested',updated_at=? WHERE job_id=? AND status IN ('queued','running')",
            (_now(), job_id),
        )
        connection.execute("COMMIT")
        job = get_job(project, job_id, refresh=False)
        if _same_process(job):
            os.killpg(job["process_group"], signal.SIGTERM)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and _same_process(job):
            time.sleep(0.02)
        process = _CHILDREN.pop(job.get("pid"), None)
        if process is not None:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        job = get_job(project, job_id, refresh=True)
        _projection(project, job)
        return job
    finally:
        connection.close()


def resume(project_value: Path, job_id: str) -> dict:
    project = _direct_directory(project_value)
    connection = _connect(project)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        job = _row(row)
        if not job or job["status"] not in TERMINAL or job["status"] == "succeeded":
            connection.execute("ROLLBACK")
            raise ValueError("job not resumable")
        if project_revision(project) != job["revision"]:
            connection.execute("ROLLBACK")
            raise ValueError("project revision changed")
        epoch = job["epoch"] + 1
        connection.execute(
            """UPDATE jobs SET status='queued',epoch=?,snapshot_digest=NULL,snapshot_root=NULL,
               candidate_root=NULL,pid=NULL,pid_token=NULL,process_group=NULL,exit_code=NULL,
               error_code=NULL,updated_at=? WHERE job_id=? AND epoch=?""",
            (epoch, _now(), job_id, job["epoch"]),
        )
        connection.execute("COMMIT")
        snapshot, snapshot_digest = _snapshot(project, job_id, epoch)
        if (
            snapshot_digest != job["revision"]
            or project_revision(project) != job["revision"]
        ):
            raise ValueError("project changed while snapshotting")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE jobs SET snapshot_root=?,candidate_root=?,snapshot_digest=?,updated_at=? WHERE job_id=? AND epoch=? AND status='queued'",
            (
                str(snapshot),
                str(snapshot / "output"),
                snapshot_digest,
                _now(),
                job_id,
                epoch,
            ),
        )
        connection.execute("COMMIT")
        job = get_job(project, job_id, refresh=False)
        _spawn(project, job)
        job = _wait_for_claim(project, job_id)
        _projection(project, job)
        return job
    finally:
        connection.close()
