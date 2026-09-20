#!/usr/bin/env python3
"""Fixed assembly of the four current, human-approved narrative segments."""

from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import canonical_layout
import segment_plan
import segment_render

SCHEMA = "haru.segment_assembly.v1"
OUTPUT_PATH = "output/final.pre-loudnorm.mp4"
RECEIPT_PATH = "quality-review/segments/assembly.json"
METHOD_COPY = "ffmpeg_concat_copy"
METHOD_REENCODE = "ffmpeg_deterministic_reencode"

RECEIPT_KEYS = frozenset(
    {
        "schema",
        "project",
        "output",
        "segments",
        "source_receipts",
        "transition_policy",
        "audio_policy",
        "policy_sha256",
        "method",
        "target_profile",
        "source_stream_profiles",
        "output_stream_profile",
        "output_sha256",
        "bytes",
        "duration_seconds",
        "decode_evidence",
    }
)
SEGMENT_KEYS = frozenset(
    {
        "segment_id",
        "ordinal",
        "definition_sha256",
        "dependency_sha256",
        "video_sha256",
        "bytes",
        "duration_seconds",
    }
)
SOURCE_RECEIPT_KEYS = frozenset(
    {
        "segment_id",
        "render_sha256",
        "evidence_sha256",
        "review_sha256",
        "stream_profile_sha256",
    }
)
DECODE_EVIDENCE_KEYS = frozenset(
    {
        "full_decode_clean",
        "packet_dts_monotonic",
        "frame_pts_monotonic",
        "duration_within_frame_tolerance",
        "frame_tolerance_seconds",
    }
)


class AssemblyError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_at(directory_fd, name):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    try:
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def regular_file_at(directory_fd, name):
    try:
        return stat.S_ISREG(
            os.stat(name, dir_fd=directory_fd, follow_symlinks=False).st_mode
        )
    except FileNotFoundError:
        return False


def create_temp_at(directory_fd):
    for _ in range(4):
        name = f".assembly-{secrets.token_hex(16)}.tmp"
        try:
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            return descriptor, name
        except FileExistsError:
            continue
    raise AssemblyError("assembly_output_invalid", "cannot allocate receipt temporary")


def _direct(project, relative):
    p = canonical_layout.direct_path(project / relative, project)
    return p if p is not None and not p.is_symlink() and p.is_file() else None


def _receipt_digests(project, segment_id):
    result = {"segment_id": segment_id}
    for key, name in (
        ("render_sha256", "render.json"),
        ("evidence_sha256", "evidence.json"),
        ("review_sha256", "review.json"),
    ):
        p = _direct(project, str(segment_render.review_dir(segment_id) / name))
        if p is None:
            raise AssemblyError(
                "segment_receipt_missing", f"{segment_id} {name} is missing"
            )
        result[key] = sha256(p)
    return result


def capture_approved_snapshot(project):
    plan = segment_plan.validate(project)
    if plan.get("mode") != "segmented":
        raise AssemblyError("segment_plan_invalid", "current segment plan required")
    lifecycle = segment_render.apply_lifecycle(plan, project)
    records = lifecycle.get("segments")
    if (
        not isinstance(records, list)
        or [r.get("segment_id") for r in records] != list(segment_plan.SEGMENTS)
        or lifecycle.get("next_actionable_segment") is not None
        or any(r.get("status") != "approved" for r in records)
    ):
        raise AssemblyError(
            "segments_not_approved", "all four current segments must be approved"
        )
    tuples = []
    receipts = []
    stream_profiles = []
    videos = []
    for ordinal, record in enumerate(records, 1):
        current = segment_render.current_render(project, plan, record)
        if current is None:
            raise AssemblyError("segment_stale", f"{record['segment_id']} is stale")
        render, video, _ = current
        tuples.append(
            {
                "segment_id": record["segment_id"],
                "ordinal": ordinal,
                "definition_sha256": record["definition_sha256"],
                "dependency_sha256": record["dependency_sha256"],
                "video_sha256": render["video_sha256"],
                "bytes": render["bytes"],
                "duration_seconds": render["duration_seconds"],
            }
        )
        receipt = _receipt_digests(project, record["segment_id"])
        receipt["stream_profile_sha256"] = segment_plan._canonical_digest(
            render["stream_profile"]
        )
        receipts.append(receipt)
        stream_profiles.append(
            {"segment_id": record["segment_id"], "profile": render["stream_profile"]}
        )
        videos.append(video)
    return {
        "plan_sha256": plan["sha256"],
        "policy": plan["assembly"],
        "policy_sha256": plan["assembly_policy_sha256"],
        "segments": tuples,
        "source_receipts": receipts,
        "stream_profiles": stream_profiles,
        "videos": videos,
    }


def _run(command, runner=None):
    return (runner or subprocess.run)(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        text=True,
    )


def _json(command, code, runner=None):
    r = _run(command, runner)
    if r.returncode:
        raise AssemblyError(code, (r.stderr or code)[-500:])
    try:
        value = json.loads(r.stdout)
    except (json.JSONDecodeError, TypeError) as e:
        raise AssemblyError(code, "invalid probe JSON") from e
    if not isinstance(value, dict):
        raise AssemblyError(code, "invalid probe object")
    return value


def probe_media(path, runner=None):
    value = _json(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_data_hash",
            "sha256",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        "media_probe_failed",
        runner,
    )
    streams = value.get("streams")
    media_format = value.get("format")
    if not isinstance(streams, list) or not isinstance(media_format, dict):
        raise AssemblyError("media_probe_failed", "streams/format missing")
    fields = {
        "video": (
            "index",
            "codec_type",
            "codec_name",
            "codec_tag_string",
            "extradata_hash",
            "time_base",
            "start_time",
            "width",
            "height",
            "pix_fmt",
            "field_order",
            "sample_aspect_ratio",
            "r_frame_rate",
            "avg_frame_rate",
            "color_range",
            "color_space",
            "color_transfer",
            "color_primaries",
        ),
        "audio": (
            "index",
            "codec_type",
            "codec_name",
            "codec_tag_string",
            "extradata_hash",
            "time_base",
            "start_time",
            "sample_rate",
            "channels",
            "channel_layout",
            "sample_fmt",
        ),
    }
    normalized = [
        {key: stream.get(key) for key in fields.get(stream.get("codec_type"), ())}
        for stream in streams
        if isinstance(stream, dict)
    ]
    videos = [stream for stream in normalized if stream.get("codec_type") == "video"]
    audio = [stream for stream in normalized if stream.get("codec_type") == "audio"]
    try:
        duration = float(media_format["duration"])
    except (KeyError, TypeError, ValueError) as error:
        raise AssemblyError("media_probe_failed", "duration missing") from error
    if (
        len(normalized) != len(streams)
        or len(normalized) != 2
        or len(videos) != 1
        or len(audio) != 1
        or not math.isfinite(duration)
        or duration <= 0
    ):
        raise AssemblyError("media_probe_failed", "invalid audio/video profile")
    return {"streams": normalized, "duration_seconds": duration}


def _fraction(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+/[1-9][0-9]*", value):
        return None
    a, b = map(int, value.split("/"))
    return (a, b) if a else None


def derive_target(profile):
    try:
        profile = segment_render.normalize_stream_profile(profile)
    except segment_render.SegmentError as error:
        raise AssemblyError(
            "unsupported_stream_profile", "unsafe first stream profile"
        ) from error
    video = next(
        stream for stream in profile["streams"] if stream.get("codec_type") == "video"
    )
    audio = next(
        (
            stream
            for stream in profile["streams"]
            if stream.get("codec_type") == "audio"
        ),
        None,
    )
    fps = video.get("frame_rate")
    pixel_format = video.get("pix_fmt")
    if (
        not isinstance(video.get("width"), int)
        or not isinstance(video.get("height"), int)
        or not isinstance(pixel_format, str)
        or not re.fullmatch(r"[A-Za-z0-9_]+", pixel_format)
        or not _fraction(fps)
    ):
        raise AssemblyError("unsupported_stream_profile", "unsafe first video profile")
    target = {
        "container": "mp4",
        "video": {
            "codec_name": "h264",
            "width": video["width"],
            "height": video["height"],
            "pix_fmt": pixel_format,
            "frame_rate": fps,
        },
        "audio": None,
    }
    if audio:
        try:
            sample_rate = int(audio.get("sample_rate"))
            channels = int(audio.get("channels"))
        except (TypeError, ValueError) as error:
            raise AssemblyError(
                "unsupported_stream_profile", "unsafe first audio profile"
            ) from error
        channel_layout = audio.get("channel_layout") or {
            1: "mono",
            2: "stereo",
            6: "5.1",
            8: "7.1",
        }.get(channels)
        if (
            not 0 < sample_rate <= 384000
            or not 0 < channels <= 8
            or not isinstance(channel_layout, str)
            or not re.fullmatch(r"[A-Za-z0-9_.()]+", channel_layout)
        ):
            raise AssemblyError(
                "unsupported_stream_profile", "unsafe first audio profile"
            )
        target["audio"] = {
            "codec_name": "aac",
            "sample_rate": sample_rate,
            "channels": channels,
            "channel_layout": channel_layout,
        }
    return target


def assembly_command(manifest, output, method, target, sources=None):
    common = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
    ]
    if method == METHOD_COPY:
        return common + [
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(manifest),
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-map",
            "0",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
        ]
    if method != METHOD_REENCODE or not isinstance(sources, list) or len(sources) != 4:
        raise AssemblyError(
            "assembly_policy_invalid", "fixed four-source reencode required"
        )
    video = target["video"]
    audio = target["audio"]
    if not isinstance(audio, dict):
        raise AssemblyError("unsupported_stream_profile", "audio stream required")
    command = list(common)
    for source in sources:
        command.extend(["-i", str(source)])
    filters = []
    concat_inputs = []
    for index in range(4):
        filters.append(
            f"[{index}:v:0]"
            f"scale={video['width']}:{video['height']}:flags=lanczos,"
            f"fps={video['frame_rate']},format={video['pix_fmt']},"
            f"setsar=1,setpts=PTS-STARTPTS[v{index}]"
        )
        filters.append(
            f"[{index}:a:0]"
            f"aresample={audio['sample_rate']}:async=0:first_pts=0,"
            f"aformat=sample_rates={audio['sample_rate']}:"
            f"channel_layouts={audio['channel_layout']},"
            f"asetpts=PTS-STARTPTS[a{index}]"
        )
        concat_inputs.append(f"[v{index}][a{index}]")
    filters.append("".join(concat_inputs) + "concat=n=4:v=1:a=1[video][audio]")
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[video]",
            "-map",
            "[audio]",
            "-map_metadata",
            "-1",
            "-map_chapters",
            "-1",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            video["pix_fmt"],
            "-r",
            video["frame_rate"],
            "-threads",
            "1",
            "-x264-params",
            "threads=1:lookahead_threads=1:sliced_threads=0",
            "-fflags",
            "+bitexact",
            "-flags:v",
            "+bitexact",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ar",
            str(audio["sample_rate"]),
            "-ac",
            str(audio["channels"]),
            "-flags:a",
            "+bitexact",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    return command


def decode_clean(path, runner=None):
    r = _run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-map",
            "0",
            "-f",
            "null",
            "-",
        ],
        runner,
    )
    return r.returncode == 0 and not (r.stderr or "").strip()


def _monotonic(rows, field, required_streams):
    seen = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get(field) in (None, "N/A"):
            continue
        try:
            stamp = float(row[field])
        except (TypeError, ValueError):
            return False
        if not math.isfinite(stamp):
            return False
        stream = row.get("stream_index")
        if stream in seen and stamp + 0.000001 < seen[stream]:
            return False
        seen[stream] = stamp
    return set(required_streams).issubset(seen)


def timestamp_evidence(path, profile, runner=None):
    packets = _json(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_packets",
            "-show_entries",
            "packet=stream_index,dts_time",
            "-of",
            "json",
            str(path),
        ],
        "timestamp_probe_failed",
        runner,
    ).get("packets")
    frames = _json(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_frames",
            "-show_entries",
            "frame=stream_index,best_effort_timestamp_time",
            "-of",
            "json",
            str(path),
        ],
        "timestamp_probe_failed",
        runner,
    ).get("frames")
    streams = profile["streams"]
    packet_streams = [stream["index"] for stream in streams]
    video_streams = [
        stream["index"] for stream in streams if stream["codec_type"] == "video"
    ]
    return {
        "packet_dts_monotonic": _monotonic(packets, "dts_time", packet_streams),
        "frame_pts_monotonic": _monotonic(
            frames, "best_effort_timestamp_time", video_streams
        ),
    }


def _same(a, b):
    return all(
        a.get(k) == b.get(k)
        for k in (
            "plan_sha256",
            "policy",
            "policy_sha256",
            "segments",
            "source_receipts",
        )
    )


def assemble(project_path, runner=None):
    project = segment_render.direct_directory(project_path)
    output = project / OUTPUT_PATH
    receipt_path = project / RECEIPT_PATH
    existing = current_receipt(project)
    if existing is not None:
        return existing[0]
    for path in (output, receipt_path):
        direct = canonical_layout.direct_path(path, project)
        if (
            direct is None
            or direct.is_symlink()
            or (direct.exists() and not direct.is_file())
        ):
            raise AssemblyError(
                "assembly_output_invalid", "assembly paths must be direct regular files"
            )
    had_output = output.is_file()
    output.parent.mkdir(parents=True, exist_ok=True)
    before = capture_approved_snapshot(project)
    media_profiles = [probe_media(v, runner) for v in before["videos"]]
    profiles = [segment_render.normalize_stream_profile(p) for p in media_profiles]
    if [
        {"segment_id": segment_id, "profile": profile}
        for segment_id, profile in zip(segment_plan.SEGMENTS, profiles)
    ] != before["stream_profiles"]:
        raise AssemblyError(
            "segment_stream_changed", "segment streams changed before assembly"
        )
    compatible = all(p["streams"] == profiles[0]["streams"] for p in profiles[1:])
    method = METHOD_COPY if compatible else METHOD_REENCODE
    target = derive_target(profiles[0])
    try:
        receipt_parent_fd = canonical_layout.open_direct_directory_fd(
            receipt_path.parent, project
        )
    except OSError as error:
        raise AssemblyError(
            "assembly_output_invalid", "assembly receipt parent is unsafe"
        ) from error
    stale_receipt_sha256 = (
        sha256_at(receipt_parent_fd, receipt_path.name)
        if regular_file_at(receipt_parent_fd, receipt_path.name)
        else None
    )
    manifest = candidate = receipt_tmp = None
    completed = False
    try:
        with tempfile.NamedTemporaryFile("w", dir=output.parent, delete=False) as f:
            manifest = Path(f.name)
            for video in before["videos"]:
                f.write("file '" + str(video).replace("'", "'\\''") + "'\n")
            f.flush()
            os.fsync(f.fileno())
        fd, name = tempfile.mkstemp(suffix=".mp4", dir=output.parent)
        os.close(fd)
        candidate = Path(name)
        candidate.unlink()
        result = _run(
            assembly_command(manifest, candidate, method, target, before["videos"]),
            runner,
        )
        if result.returncode or not candidate.is_file() or candidate.is_symlink():
            raise AssemblyError(
                "assembly_failed", (result.stderr or "no output")[-500:]
            )
        out_profile = probe_media(candidate, runner)
        timestamps = timestamp_evidence(candidate, out_profile, runner)
        frac = _fraction(target["video"]["frame_rate"])
        tolerance = frac[1] / frac[0]
        expected = sum(float(s["duration_seconds"]) for s in before["segments"])
        if not decode_clean(candidate, runner):
            raise AssemblyError("assembly_decode_failed", "decode failed")
        if abs(out_profile["duration_seconds"] - expected) > tolerance + 0.000001:
            raise AssemblyError(
                "assembly_duration_failed", "duration outside one frame"
            )
        if not all(timestamps.values()):
            raise AssemblyError("assembly_timestamp_failed", "timestamps not monotonic")
        if not _same(before, capture_approved_snapshot(project)):
            raise AssemblyError("segment_changed", "source changed")
        receipt = {
            "schema": SCHEMA,
            "project": project.name,
            "output": OUTPUT_PATH,
            "segments": before["segments"],
            "source_receipts": before["source_receipts"],
            "transition_policy": before["policy"]["transition_policy"],
            "audio_policy": before["policy"]["audio_policy"],
            "policy_sha256": before["policy_sha256"],
            "method": method,
            "target_profile": target,
            "source_stream_profiles": before["stream_profiles"],
            "output_stream_profile": {
                **segment_render.normalize_stream_profile(out_profile),
                "duration_seconds": out_profile["duration_seconds"],
            },
            "output_sha256": sha256(candidate),
            "bytes": candidate.stat().st_size,
            "duration_seconds": out_profile["duration_seconds"],
            "decode_evidence": {
                "full_decode_clean": True,
                **timestamps,
                "duration_within_frame_tolerance": True,
                "frame_tolerance_seconds": tolerance,
            },
        }
        receipt_descriptor, receipt_tmp = create_temp_at(receipt_parent_fd)
        with os.fdopen(receipt_descriptor, "w") as f:
            json.dump(receipt, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        if not _same(before, capture_approved_snapshot(project)):
            raise AssemblyError("segment_changed", "source changed before promotion")
        os.replace(candidate, output)
        candidate = None
        if not _same(before, capture_approved_snapshot(project)):
            output.unlink(missing_ok=True)
            raise AssemblyError(
                "segment_changed", "source changed before receipt promotion"
            )
        if stale_receipt_sha256 is not None:
            if (
                not regular_file_at(receipt_parent_fd, receipt_path.name)
                or sha256_at(receipt_parent_fd, receipt_path.name)
                != stale_receipt_sha256
            ):
                raise AssemblyError(
                    "segment_changed", "assembly receipt changed before promotion"
                )
            archived_name = (
                f"{receipt_path.name}.superseded-{stale_receipt_sha256[:12]}"
            )
            if regular_file_at(receipt_parent_fd, archived_name):
                if sha256_at(receipt_parent_fd, archived_name) != stale_receipt_sha256:
                    raise AssemblyError(
                        "assembly_archive_invalid",
                        "superseded assembly receipt path is unsafe",
                    )
                os.unlink(receipt_path.name, dir_fd=receipt_parent_fd)
            else:
                try:
                    os.stat(
                        archived_name,
                        dir_fd=receipt_parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    raise AssemblyError(
                        "assembly_archive_invalid",
                        "superseded assembly receipt path is unsafe",
                    )
                os.replace(
                    receipt_path.name,
                    archived_name,
                    src_dir_fd=receipt_parent_fd,
                    dst_dir_fd=receipt_parent_fd,
                )
        os.replace(
            receipt_tmp,
            receipt_path.name,
            src_dir_fd=receipt_parent_fd,
            dst_dir_fd=receipt_parent_fd,
        )
        receipt_tmp = None
        completed = True
        return receipt
    finally:
        if (
            not completed
            and not had_output
            and (output.exists() or output.is_symlink())
        ):
            try:
                output.unlink()
            except OSError:
                pass
        for p in (manifest, candidate):
            if p:
                try:
                    p.unlink()
                except OSError:
                    pass
        if receipt_tmp:
            try:
                os.unlink(receipt_tmp, dir_fd=receipt_parent_fd)
            except OSError:
                pass
        os.close(receipt_parent_fd)


def output_stream_profile_current(receipt):
    try:
        profile = receipt["output_stream_profile"]
        normalized = segment_render.normalize_stream_profile(profile)
        streams = profile["streams"]
        duration = profile["duration_seconds"]
        receipt_duration = receipt["duration_seconds"]
        target = receipt["target_profile"]
        target_video = target["video"]
        target_audio = target["audio"]
        video, audio = normalized["streams"]
        if (
            not isinstance(profile, dict)
            or set(profile) != {"streams", "duration_seconds"}
            or normalized["streams"] != streams
            or not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or not math.isfinite(duration)
            or duration <= 0
            or not isinstance(receipt_duration, (int, float))
            or isinstance(receipt_duration, bool)
            or not math.isfinite(receipt_duration)
            or not math.isclose(
                duration, receipt_duration, rel_tol=0.0, abs_tol=0.000001
            )
            or target.get("container") != "mp4"
            or not isinstance(target_video, dict)
            or not isinstance(target_audio, dict)
        ):
            return False
        if receipt.get("method") == METHOD_COPY:
            source_streams = receipt["source_stream_profiles"][0]["profile"]["streams"]
            return streams == source_streams
        return all(
            video.get(key) == target_video.get(key)
            for key in ("codec_name", "width", "height", "pix_fmt", "frame_rate")
        ) and all(
            audio.get(key) == target_audio.get(key)
            for key in (
                "codec_name",
                "sample_rate",
                "channels",
                "channel_layout",
            )
        )
    except (KeyError, TypeError, ValueError, segment_render.SegmentError):
        return False


def frame_tolerance_current(receipt, evidence):
    try:
        numerator, denominator = _fraction(
            receipt["target_profile"]["video"]["frame_rate"]
        )
        expected = denominator / numerator
        actual = evidence["frame_tolerance_seconds"]
        return (
            isinstance(actual, (int, float))
            and not isinstance(actual, bool)
            and math.isfinite(actual)
            and actual > 0
            and math.isclose(actual, expected, rel_tol=0.0, abs_tol=0.000001)
        )
    except (KeyError, TypeError, ValueError):
        return False


def stream_policy_current(receipt):
    try:
        entries = receipt["source_stream_profiles"]
        if (
            not isinstance(entries, list)
            or len(entries) != len(segment_plan.SEGMENTS)
            or not all(isinstance(entry, dict) for entry in entries)
            or [entry.get("segment_id") for entry in entries]
            != list(segment_plan.SEGMENTS)
        ):
            return False
        profiles = [entry.get("profile") for entry in entries]
        normalized = [
            segment_render.normalize_stream_profile(profile) for profile in profiles
        ]
        if normalized != profiles:
            return False
        compatible = all(
            profile["streams"] == profiles[0]["streams"] for profile in profiles[1:]
        )
        expected_method = METHOD_COPY if compatible else METHOD_REENCODE
        return receipt.get("method") == expected_method and receipt.get(
            "target_profile"
        ) == derive_target(profiles[0])
    except (
        AssemblyError,
        KeyError,
        TypeError,
        ValueError,
        segment_render.SegmentError,
    ):
        return False


def segment_tuples_shape_ok(segments):
    if not isinstance(segments, list) or len(segments) != len(segment_plan.SEGMENTS):
        return False
    for ordinal, (segment_id, segment) in enumerate(
        zip(segment_plan.SEGMENTS, segments, strict=True), 1
    ):
        if (
            not isinstance(segment, dict)
            or set(segment) != SEGMENT_KEYS
            or segment.get("segment_id") != segment_id
            or segment.get("ordinal") != ordinal
            or any(
                not canonical_layout.valid_sha256(segment.get(key))
                for key in (
                    "definition_sha256",
                    "dependency_sha256",
                    "video_sha256",
                )
            )
            or not isinstance(segment.get("bytes"), int)
            or isinstance(segment.get("bytes"), bool)
            or segment["bytes"] <= 0
            or not isinstance(segment.get("duration_seconds"), (int, float))
            or isinstance(segment.get("duration_seconds"), bool)
            or not math.isfinite(segment["duration_seconds"])
            or segment["duration_seconds"] <= 0
        ):
            return False
    return True


def source_receipts_shape_ok(receipt):
    source_receipts = receipt.get("source_receipts")
    profiles = receipt.get("source_stream_profiles")
    if (
        not isinstance(source_receipts, list)
        or not isinstance(profiles, list)
        or len(source_receipts) != len(segment_plan.SEGMENTS)
        or len(profiles) != len(segment_plan.SEGMENTS)
    ):
        return False
    for segment_id, source, profile_entry in zip(
        segment_plan.SEGMENTS, source_receipts, profiles, strict=True
    ):
        if (
            not isinstance(source, dict)
            or set(source) != SOURCE_RECEIPT_KEYS
            or source.get("segment_id") != segment_id
            or any(
                not canonical_layout.valid_sha256(source.get(key))
                for key in SOURCE_RECEIPT_KEYS - {"segment_id"}
            )
            or not isinstance(profile_entry, dict)
            or set(profile_entry) != {"segment_id", "profile"}
            or profile_entry.get("segment_id") != segment_id
            or source.get("stream_profile_sha256")
            != segment_plan._canonical_digest(profile_entry.get("profile"))
        ):
            return False
    return True


def receipt_shape_ok(project, receipt, require_output=True):
    try:
        evidence = receipt["decode_evidence"]
        segments = receipt["segments"]
        ok = (
            isinstance(receipt, dict)
            and set(receipt) == RECEIPT_KEYS
            and isinstance(evidence, dict)
            and set(evidence) == DECODE_EVIDENCE_KEYS
            and segment_tuples_shape_ok(segments)
            and receipt.get("schema") == SCHEMA
            and receipt.get("project") == project.name
            and receipt.get("output") == OUTPUT_PATH
            and receipt.get("transition_policy") == segment_plan.TRANSITION_POLICY
            and receipt.get("audio_policy") == segment_plan.AUDIO_POLICY
            and receipt.get("method") in {METHOD_COPY, METHOD_REENCODE}
            and canonical_layout.valid_sha256(receipt.get("policy_sha256"))
            and canonical_layout.valid_sha256(receipt.get("output_sha256"))
            and isinstance(receipt.get("bytes"), int)
            and not isinstance(receipt.get("bytes"), bool)
            and receipt["bytes"] > 0
            and isinstance(receipt.get("duration_seconds"), (int, float))
            and not isinstance(receipt.get("duration_seconds"), bool)
            and receipt["duration_seconds"] > 0
            and math.isfinite(receipt["duration_seconds"])
            and all(
                evidence.get(k) is True
                for k in (
                    "full_decode_clean",
                    "packet_dts_monotonic",
                    "frame_pts_monotonic",
                    "duration_within_frame_tolerance",
                )
            )
            and isinstance(evidence.get("frame_tolerance_seconds"), (int, float))
            and not isinstance(evidence.get("frame_tolerance_seconds"), bool)
            and math.isfinite(evidence["frame_tolerance_seconds"])
            and evidence["frame_tolerance_seconds"] > 0
            and frame_tolerance_current(receipt, evidence)
            and source_receipts_shape_ok(receipt)
            and stream_policy_current(receipt)
            and output_stream_profile_current(receipt)
        )
        if not ok:
            return False
        if require_output:
            output = _direct(project, OUTPUT_PATH)
            return (
                output is not None
                and sha256(output) == receipt["output_sha256"]
                and output.stat().st_size == receipt["bytes"]
            )
        return True
    except (KeyError, OSError, TypeError, ValueError):
        return False


def read_receipt(project):
    path = _direct(project, RECEIPT_PATH)
    if path is None:
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return (value, path) if isinstance(value, dict) else None


def current_receipt(project, expected_sha256=None):
    loaded = read_receipt(project)
    if loaded is None:
        return None
    receipt, path = loaded
    try:
        snapshot = capture_approved_snapshot(project)
    except (AssemblyError, OSError):
        return None
    return (
        loaded
        if (expected_sha256 is None or sha256(path) == expected_sha256)
        and receipt_shape_ok(project, receipt)
        and receipt.get("segments") == snapshot["segments"]
        and receipt.get("source_receipts") == snapshot["source_receipts"]
        and receipt.get("source_stream_profiles") == snapshot["stream_profiles"]
        and receipt.get("policy_sha256") == snapshot["policy_sha256"]
        else None
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", type=Path)
    args = parser.parse_args(argv)
    try:
        receipt = assemble(args.project)
    except (AssemblyError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "status": "failed",
                    "code": getattr(exc, "code", "assembly_internal_error"),
                }
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(receipt, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
