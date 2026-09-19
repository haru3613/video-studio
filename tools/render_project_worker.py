#!/usr/bin/python3 -I
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))

import render_contract
import editorial_contract
import pronunciation_workflow
import segment_assembly
import segment_plan
import template_trust


def emit(project, outcome, code, data=None):
    print(
        json.dumps(
            {
                "schema_version": 1,
                "outcome": outcome,
                "code": code,
                "project": str(project) if project else None,
                "data": data,
            },
            separators=(",", ":"),
        )
    )


def direct_directory(path):
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("directory")
    return path.resolve(strict=True)


def direct_file(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("file")
    return path.resolve(strict=True)


def safe_relative(root, value, kind):
    path = Path(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise ValueError("relative path")
    candidate = root.joinpath(*path.parts)
    current = root
    for part in path.parts:
        current = current / part
        if current.exists() or current.is_symlink():
            if current.is_symlink():
                raise ValueError("symlink")
    if kind == "directory":
        return direct_directory(candidate)
    if kind == "file":
        return direct_file(candidate)
    parent = direct_directory(candidate.parent)
    return parent / candidate.name


def write_json_atomically(path, value):
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as handle:
        json.dump(value, handle, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render_main(argv):
    validate_only = len(argv) == 4 and argv[1] == "--validate"
    if not validate_only and len(argv) != 3:
        emit(None, "error", "invalid_input")
        return 2
    project_arg = argv[2] if validate_only else argv[1]
    tools_arg = argv[3] if validate_only else argv[2]
    try:
        project = direct_directory(project_arg)
        tools_root = direct_directory(tools_arg)
        plan_path = direct_file(project / "render_plan.json")
        plan = json.loads(plan_path.read_text())
        if (
            plan.get("schema") != "haru.render_plan.v1"
            or plan.get("engine") != "remotion"
        ):
            raise ValueError("schema")
        segment_summary = segment_plan.validate(project)
        if segment_summary.get("mode") == "invalid_segment_plan":
            raise ValueError("segment plan")
        segmented = segment_summary.get("mode") == "segmented"
        composition = None
        remotion = None
        if not segmented:
            template_trust.authorize(project)
            composition = plan.get("composition")
            if not isinstance(composition, str) or not re.fullmatch(
                r"[A-Za-z][A-Za-z0-9_-]{0,127}", composition
            ):
                raise ValueError("composition")
            remotion = safe_relative(project, plan.get("remotion_dir", ""), "directory")
            safe_relative(remotion, "package.json", "file")
            node_modules = safe_relative(remotion, "node_modules", "directory")
            remotion_binary = remotion / "node_modules/.bin/remotion"
            resolved_binary = remotion_binary.resolve(strict=True)
            if (
                not resolved_binary.is_file()
                or node_modules not in resolved_binary.parents
            ):
                raise ValueError("remotion binary")
        output_value = plan.get("output")
        if output_value != "output/final.mp4":
            raise ValueError("output")
        output = safe_relative(project, output_value, "output")
        expected = plan.get("expected_duration")
        if isinstance(expected, bool):
            raise ValueError("expected")
        if isinstance(expected, (int, float)):
            if expected <= 0:
                raise ValueError("expected")
            expected_arg = str(expected)
        elif isinstance(expected, str):
            expected_arg = str(safe_relative(project, expected, "file"))
        else:
            raise ValueError("expected")
        concurrency = plan.get("concurrency", 1)
        if (
            isinstance(concurrency, bool)
            or not isinstance(concurrency, int)
            or not 1 <= concurrency <= 8
        ):
            raise ValueError("concurrency")
        narration = plan.get("narration")
        narration_text = plan.get("narration_text")
        skip_pronunciation = plan.get("skip_pronunciation_gate", False)
        audio_mix = plan.get("audio_mix")
        if narration is not None:
            narration = safe_relative(project, narration, "file")
            if narration_text is None:
                raise ValueError("narration text")
            narration_text = safe_relative(project, narration_text, "file")
            pronunciation = json.loads(
                direct_file(Path(str(narration) + ".pron-ok.json")).read_text()
            )
            narration_digest = sha256_file(narration)
            if (
                pronunciation.get("sha256") != narration_digest
                or pronunciation.get("warnings") != []
            ):
                raise ValueError("pronunciation receipt")
            if not segmented:
                # Remotion reads narration through staticFile(), which resolves
                # under remotion/public/. Segmented assembly does not read it.
                static_copy = remotion / "public" / narration.name
                if static_copy.is_symlink() or (
                    static_copy.is_file()
                    and sha256_file(static_copy) != narration_digest
                ):
                    raise ValueError("renderer narration copy")
        elif not skip_pronunciation:
            raise ValueError("pronunciation")
        if not isinstance(skip_pronunciation, bool):
            raise ValueError("pronunciation")
        renderer = safe_relative(tools_root, "video/render_and_verify.sh", "file")
        mixer = direct_file(Path(__file__).resolve().parents[1] / "tools/mix_final.py")
        if audio_mix is not None:
            validated_mix = subprocess.run(
                [str(mixer), "--validate-plan", str(plan_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if validated_mix.returncode != 0:
                raise ValueError("audio mix")
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        emit(locals().get("project"), "error", "invalid_input")
        return 2
    pronunciation_review = pronunciation_workflow.validate_current_review(project)
    if pronunciation_review["required"] and not pronunciation_review["ok"]:
        emit(
            project,
            "blocked",
            pronunciation_review["code"],
            pronunciation_review,
        )
        return 3
    editorial_digest = None
    project_contract_path = project / "project-contract.json"
    if project_contract_path.exists() or project_contract_path.is_symlink():
        try:
            project_contract = json.loads(
                direct_file(project_contract_path).read_text()
            )
        except (OSError, ValueError, json.JSONDecodeError, TypeError):
            emit(project, "error", "invalid_input")
            return 2
        # Trigger on the LANE, not on the declared profile: a lane whose profile is
        # missing or unrecognised must still be gated, and that is exactly the case
        # where the gate matters most. Keying on the profile let it fail open.
        project_lane = project_contract.get("lane_contract")
        pinned_profile = editorial_contract.LANE_PROFILES.get(project_lane)
        if pinned_profile is not None:
            editorial_path = project / "editorial-contract.json"
            try:
                editorial_digest = sha256_file(direct_file(editorial_path))
            except (OSError, ValueError):
                editorial_digest = None
            editorial = editorial_contract.validate_project(
                project, require_preview=True
            )
            declared = project_contract.get("production_profile")
            if declared != pinned_profile:
                editorial["ok"] = False
                editorial["problems"].append(
                    f"{project_lane} requires production_profile {pinned_profile}"
                )
            if editorial.get("profile") != declared:
                editorial["ok"] = False
                editorial["problems"].append(
                    "editorial contract production_profile does not match the project contract"
                )
            if (
                plan.get("editorial_contract") != "editorial-contract.json"
                or plan.get("editorial_contract_sha256") != editorial_digest
            ):
                editorial["ok"] = False
                editorial["problems"].append(
                    "render plan is not bound to the editorial contract"
                )
            if not editorial["ok"]:
                emit(project, "blocked", "editorial_contract_failed", editorial)
                return 3
    if validate_only:
        emit(project, "ok", "render_plan_valid")
        return 0

    marker = Path(str(output) + ".render-result")
    log = Path(str(output) + ".render.log")
    lock = Path(str(output) + ".rendering")
    premix = output.with_name(output.name.removesuffix(".mp4") + ".pre-loudnorm.mp4")
    premix_raw = premix.with_name(premix.name.removesuffix(".mp4") + ".raw.mp4")
    assembly_receipt_path = project / segment_assembly.RECEIPT_PATH
    occupied = [output, premix_raw, marker, log, lock]
    if not segmented:
        occupied.append(premix)
    if any(path.exists() or path.is_symlink() for path in occupied):
        emit(project, "blocked", "output_exists")
        return 3
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(descriptor, b'{"schema":"haru.render_lock.v1","status":"running"}\n')
        os.fsync(descriptor)
        os.close(descriptor)
        state = project / ".hvp"
        if state.is_symlink():
            raise OSError("state symlink")
        state.mkdir(exist_ok=True)
        state = direct_directory(state)
        runtime_home = state / "runtime-home"
        if runtime_home.is_symlink():
            raise OSError("runtime home symlink")
        runtime_home.mkdir(exist_ok=True)
        runtime_home = direct_directory(runtime_home)
        arguments = [
            str(renderer),
            str(remotion),
            composition,
            str(premix),
            expected_arg,
            str(concurrency),
        ]
        if narration is not None:
            # The isolated worker HOME intentionally has no STT dependencies.
            # The canonical receipt above proves these exact bytes already passed.
            arguments.append("--skip-pronunciation-gate")
        elif skip_pronunciation:
            arguments.append("--skip-pronunciation-gate")
        environment = {
            "HOME": str(runtime_home),
            "LANG": "C.UTF-8",
            "npm_config_offline": "true",
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
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
            environment["VIDEO_STUDIO_CHROMIUM"] = str(
                browser_path.resolve(strict=True)
            )
        assembly_digest = None
        assembly_value = None
        if segmented:
            current_assembly = segment_assembly.current_receipt(project)
            if current_assembly is None:
                emit(project, "blocked", "segment_assembly_required")
                return 3
            assembly_value = current_assembly[0]
            log.touch(exist_ok=False)
            if current_assembly[0].get("output_sha256") != sha256_file(premix):
                raise ValueError("assembly output digest")
            assembly_digest = sha256_file(current_assembly[1])
        else:
            # The portable supervisor owns detachment; this worker stays synchronous so
            # one terminal marker represents the whole render and mix.
            with log.open("xb") as render_log:
                result = subprocess.run(
                    arguments,
                    stdin=subprocess.DEVNULL,
                    stdout=render_log,
                    stderr=subprocess.STDOUT,
                    env=environment,
                    check=False,
                )
            if result.returncode != 0 or not premix.is_file() or premix.is_symlink():
                failure = {
                    "schema": "haru.render_result.v1",
                    "status": "failed",
                    "project": project.name,
                    "output": output_value,
                    "exit_code": result.returncode,
                }
                write_json_atomically(marker, failure)
                emit(project, "error", "render_failed", failure)
                return 4
        premix_digest = sha256_file(premix)
        mix_arguments = [
            str(mixer),
            str(premix),
            str(output),
            expected_arg,
            str(renderer),
        ]
        if audio_mix:
            mix_arguments.append(str(plan_path))
        with log.open("ab") as mix_log:
            mixed = subprocess.run(
                mix_arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=mix_log,
                env=environment,
                check=False,
                text=True,
            )
        try:
            mix = json.loads(mixed.stdout)
        except (json.JSONDecodeError, TypeError):
            mix = None
        if (
            mixed.returncode != 0
            or not output.is_file()
            or output.is_symlink()
            or not render_contract.valid_mix_receipt(mix)
            or mix["input_sha256"] != premix_digest
            or bool(mix.get("audio_mix")) != bool(audio_mix)
        ):
            try:
                output.unlink()
            except OSError:
                pass
            failure = {
                "schema": "haru.render_result.v1",
                "status": "failed",
                "project": project.name,
                "output": output_value,
                "exit_code": mixed.returncode,
            }
            write_json_atomically(marker, failure)
            emit(project, "error", "mix_failed", failure)
            return 4
        if segmented:
            current_assembly = segment_assembly.current_receipt(
                project, expected_sha256=assembly_digest
            )
            if (
                current_assembly is None
                or current_assembly[0] != assembly_value
                or current_assembly[0].get("output_sha256") != mix["input_sha256"]
            ):
                try:
                    output.unlink()
                except OSError:
                    pass
                raise ValueError("assembly changed during final mix")
        digest = sha256_file(output)
        if mix["sha256"] != digest or mix["bytes"] != output.stat().st_size:
            output.unlink()
            raise OSError("mix digest")
        success = {
            "schema": "haru.render_result.v1",
            "status": "render_complete",
            "render_input_revision": render_contract.render_input_revision(project),
            "project": project.name,
            "output": output_value,
            "video_sha256": digest,
            "bytes": output.stat().st_size,
            "duration_seconds": mix["duration_seconds"],
            "loudness_lufs": mix["loudness_lufs"],
            "true_peak_dbfs": mix["true_peak_dbfs"],
            "loudness_range_lu": mix["loudness_range_lu"],
            "mix": {
                "schema": mix["schema"],
                "method": mix["method"],
                "normalization_type": mix["normalization_type"],
                "input_sha256": mix["input_sha256"],
                "target": mix["target"],
            },
        }
        if segmented:
            success["assembly"] = {
                "schema": segment_assembly.SCHEMA,
                "path": segment_assembly.RECEIPT_PATH,
                "sha256": assembly_digest,
            }
        if mix.get("audio_mix"):
            success["mix"]["audio_mix"] = mix["audio_mix"]
        if narration is not None:
            success["narration_sha256"] = narration_digest
        if editorial_digest is not None:
            success["editorial_contract_sha256"] = editorial_digest
        write_json_atomically(marker, success)
        if not segmented:
            try:
                premix.unlink()
            except OSError:
                pass
        emit(project, "ok", "render_complete", success)
        return 0
    except (KeyError, OSError, TypeError, ValueError):
        try:
            output.unlink()
        except (NameError, OSError):
            pass
        emit(project, "error", "internal_error")
        return 5
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def job_main(argv):
    """Render one fenced snapshot and ask the durable coordinator to commit it."""
    if len(argv) != 6:
        emit(None, "error", "invalid_input")
        return 2
    try:
        import portable_jobs

        canonical_project = direct_directory(argv[2])
        tools_root = direct_directory(argv[3])
        job_id = argv[4]
        epoch = int(argv[5])
        job = portable_jobs.worker_context(canonical_project, job_id, epoch)
        if (
            not job
            or Path(job["tools_root"]) != tools_root
            or job.get("snapshot_root") is None
        ):
            emit(canonical_project, "blocked", "job_fenced")
            return 3
        snapshot = direct_directory(job["snapshot_root"])
        code = render_main([argv[0], str(snapshot), str(tools_root)])
        if code != 0:
            portable_jobs.worker_failed(canonical_project, job_id, epoch, code)
            return code
        if not portable_jobs.promote_candidate(canonical_project, job_id, epoch):
            # A cancelled, resumed or otherwise stale worker may retain its
            # candidate for diagnosis, but it never writes canonical output.
            emit(canonical_project, "blocked", "job_fenced")
            return 3
        emit(
            canonical_project,
            "ok",
            "render_promoted",
            {"job_id": job_id, "epoch": epoch},
        )
        return 0
    except (OSError, TypeError, ValueError):
        try:
            portable_jobs.worker_failed(canonical_project, job_id, epoch, 5)
        except (NameError, OSError, TypeError, ValueError):
            pass
        emit(locals().get("canonical_project"), "error", "internal_error")
        return 5


def main(argv):
    if len(argv) > 1 and argv[1] == "--job":
        return job_main(argv)
    return render_main(argv)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
