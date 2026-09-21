#!/usr/bin/python3 -I
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


TARGET_I = -14.0
TARGET_TP = -1.0
# AAC raised a real -2.0 dBTP encode to -0.06 dBTP; keep the release gate strict.
ENCODE_TP = -3.0
TARGET_TOLERANCE = 1.0
# AAC quantization can move peaks a little during a corrective re-encode.  Keep
# this guard below the final release ceiling instead of adding a peak limiter.
CORRECTION_PEAK_GUARD = 0.25
MAX_CORRECTION_DB = 2.0
AUDIO_MIX_SCHEMA = "haru.audio_mix.v1"


def fail(message):
    print(message, file=sys.stderr)
    return 1


def direct_file(value):
    path = Path(value)
    if path.is_symlink() or not path.is_file():
        raise ValueError("file")
    return path.resolve(strict=True)


def output_path(value):
    path = Path(value)
    if path.exists() or path.is_symlink():
        raise ValueError("output exists")
    parent = path.parent.resolve(strict=True)
    if not parent.is_dir():
        raise ValueError("output parent")
    return parent / path.name


def stable_sha256(path):
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    identity = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
    )
    if identity(before) != identity(after):
        raise RuntimeError("media changed while hashing")
    return digest.hexdigest()


def number(value, minimum, maximum):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and minimum <= value <= maximum
    )


def project_file(project, value):
    path = Path(value)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError("audio path")
    current = project
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise ValueError("audio symlink")
    resolved = direct_file(current)
    resolved.relative_to(project)
    return resolved


def load_audio_mix(plan_path):
    plan_path = direct_file(plan_path)
    plan = json.loads(plan_path.read_text())
    value = plan.get("audio_mix")
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or value.get("schema") != AUDIO_MIX_SCHEMA
        or set(value) - {"schema", "background_music", "sound_effects"}
    ):
        raise ValueError("audio mix")
    project = plan_path.parent
    background = value.get("background_music")
    if background is not None:
        if (
            not isinstance(background, dict)
            or set(background) - {"path", "gain_db", "loop", "fade_in_seconds", "fade_out_seconds"}
            or not isinstance(background.get("path"), str)
            or not number(background.get("gain_db", -24), -60, 0)
            or not isinstance(background.get("loop", True), bool)
            or not number(background.get("fade_in_seconds", 2), 0, 30)
            or not number(background.get("fade_out_seconds", 3), 0, 30)
        ):
            raise ValueError("background music")
        background = {
            **background,
            "gain_db": background.get("gain_db", -24),
            "loop": background.get("loop", True),
            "fade_in_seconds": background.get("fade_in_seconds", 2),
            "fade_out_seconds": background.get("fade_out_seconds", 3),
            "file": project_file(project, background["path"]),
        }
    effects = value.get("sound_effects", [])
    if not isinstance(effects, list) or len(effects) > 64:
        raise ValueError("sound effects")
    resolved_effects = []
    for effect in effects:
        if (
            not isinstance(effect, dict)
            # at_scene records WHICH storyboard scene this effect marks, so a
            # narration re-time can move it instead of leaving a chapter whoosh
            # on the previous take's clock. retime-visuals resolves it to
            # start_seconds; the mixer still plays the resolved seconds.
            or set(effect) - {"path", "start_seconds", "gain_db", "at_scene"}
            or not isinstance(effect.get("at_scene", ""), str)
            or not isinstance(effect.get("path"), str)
            or not number(effect.get("start_seconds"), 0, 21600)
            or not number(effect.get("gain_db", -6), -60, 6)
        ):
            raise ValueError("sound effect")
        resolved_effects.append(
            {
                **effect,
                "gain_db": effect.get("gain_db", -6),
                "file": project_file(project, effect["path"]),
            }
        )
    if background is None and not resolved_effects:
        raise ValueError("empty audio mix")
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return {
        "plan_sha256": hashlib.sha256(canonical).hexdigest(),
        "background_music": background,
        "sound_effects": resolved_effects,
    }


def run(command):
    return subprocess.run(command, capture_output=True, text=True, check=False)


def loudnorm_measure(ffmpeg, media):
    result = run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(media),
            "-map",
            "0:a:0",
            "-af",
            f"loudnorm=I={TARGET_I}:TP={ENCODE_TP}:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        ]
    )
    if result.returncode != 0:
        raise RuntimeError("loudness measurement failed")
    matches = re.findall(r'\{\s*"input_i".*?\}', result.stderr, re.DOTALL)
    if not matches:
        raise RuntimeError("loudness measurement missing")
    data = json.loads(matches[-1])
    for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"):
        value = float(data[key])
        if not math.isfinite(value):
            raise RuntimeError(f"invalid {key}")
        data[key] = value
    return data


def bounded_gain_correction(measured):
    """Return the transparent gain needed to meet the loudness gate safely.

    loudnorm can select dynamic normalization for high-crest narration.  Its
    reported second-pass result can then miss the requested integrated target
    even after a conservative encoder peak target.  A small measured gain is
    transparent; it is only allowed when the decoded true-peak headroom proves
    it can remain below the strict final ceiling without limiting.
    """
    correction = TARGET_I - measured["input_i"]
    if abs(correction) <= TARGET_TOLERANCE:
        return 0.0
    if abs(correction) > MAX_CORRECTION_DB:
        raise RuntimeError(
            "target loudness not reached "
            f"(measured_i={measured['input_i']:.2f} LUFS, "
            f"measured_tp={measured['input_tp']:.2f} dBTP, "
            f"required_gain={correction:.2f} dB exceeds "
            f"{MAX_CORRECTION_DB:.2f} dB bound)"
        )
    if correction > 0:
        headroom = TARGET_TP - CORRECTION_PEAK_GUARD - measured["input_tp"]
        if correction > headroom:
            raise RuntimeError(
                "target loudness not reached "
                f"(measured_i={measured['input_i']:.2f} LUFS, "
                f"measured_tp={measured['input_tp']:.2f} dBTP, "
                f"required_gain={correction:.2f} dB exceeds "
                f"safe_peak_headroom={max(0.0, headroom):.2f} dB)"
            )
    return correction


def encode_gain_correction(ffmpeg, source, output, correction):
    result = run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-nostats",
            "-i",
            str(source),
            "-map",
            "0",
            "-map_metadata",
            "0",
            "-c",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-filter:a:0",
            f"volume={correction:.6f}dB",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    if result.returncode != 0 or not output.is_file():
        raise RuntimeError("bounded loudness correction failed")


def media_duration(ffprobe, media):
    result = run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(media),
        ]
    )
    duration = float(result.stdout.strip())
    if result.returncode != 0 or not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("duration unavailable")
    return duration


def compose_audio(ffmpeg, ffprobe, source, audio_mix, output):
    duration = media_duration(ffprobe, source)
    command = [ffmpeg, "-y", "-hide_banner", "-nostats", "-i", str(source)]
    filters = []
    mix_inputs = ["[narration]"]
    background = audio_mix["background_music"]
    effects = audio_mix["sound_effects"]
    assets = []
    input_index = 1
    if background:
        before = stable_sha256(background["file"])
        assets.append((background["file"], before))
        if background["loop"]:
            command.extend(["-stream_loop", "-1"])
        command.extend(["-i", str(background["file"])])
        filters.append(
            "[0:a:0]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            "asplit=2[narration][narration_key]"
        )
        fade_out_start = max(0, duration - background["fade_out_seconds"])
        filters.append(
            f"[{input_index}:a:0]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"apad,atrim=0:{duration},asetpts=PTS-STARTPTS,volume={background['gain_db']}dB,"
            f"afade=t=in:st=0:d={background['fade_in_seconds']},"
            f"afade=t=out:st={fade_out_start}:d={background['fade_out_seconds']}[bgm]"
        )
        filters.append(
            "[bgm][narration_key]sidechaincompress="
            "threshold=0.02:ratio=8:attack=20:release=600[bgm_ducked]"
        )
        mix_inputs.append("[bgm_ducked]")
        input_index += 1
    else:
        filters.append(
            "[0:a:0]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[narration]"
        )
    for index, effect in enumerate(effects):
        before = stable_sha256(effect["file"])
        assets.append((effect["file"], before))
        command.extend(["-i", str(effect["file"])])
        delay = round(effect["start_seconds"] * 1000)
        label = f"sfx{index}"
        filters.append(
            f"[{input_index}:a:0]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"volume={effect['gain_db']}dB,adelay={delay}:all=1,apad,atrim=0:{duration}[{label}]"
        )
        mix_inputs.append(f"[{label}]")
        input_index += 1
    filters.append(
        "".join(mix_inputs)
        + f"amix=inputs={len(mix_inputs)}:duration=first:dropout_transition=0:normalize=0[mixed]"
    )
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "0:v:0",
            "-map",
            "[mixed]",
            "-map_metadata",
            "0",
            "-c:v",
            "copy",
            "-c:a",
            "flac",
            "-t",
            str(duration),
            str(output),
        ]
    )
    result = run(command)
    if result.returncode != 0 or not output.is_file():
        raise RuntimeError("audio composition failed")
    for path, digest in assets:
        if stable_sha256(path) != digest:
            raise RuntimeError("audio asset changed during mix")
    return {
        "schema": AUDIO_MIX_SCHEMA,
        "plan_sha256": audio_mix["plan_sha256"],
        "ducking": "sidechaincompress.v1" if background else None,
        "background_music": (
            {"path": background["path"], "sha256": assets[0][1]} if background else None
        ),
        "sound_effects": [
            {
                "path": effect["path"],
                "sha256": assets[index + (1 if background else 0)][1],
                "start_seconds": effect["start_seconds"],
            }
            for index, effect in enumerate(effects)
        ],
    }


def main(argv):
    if len(argv) == 3 and argv[1] == "--validate-plan":
        try:
            load_audio_mix(argv[2])
            return 0
        except (OSError, ValueError, KeyError, json.JSONDecodeError, TypeError):
            return fail("invalid audio mix plan")
    if len(argv) not in (5, 6):
        return fail("usage: mix_final.py <pre-mix.mp4> <final.mp4> <expected> <verifier> [render-plan]")
    temporary = None
    correction_temporary = None
    composed = None
    try:
        source = direct_file(argv[1])
        output = output_path(argv[2])
        verifier = direct_file(argv[4])
        if source == output:
            raise ValueError("same path")
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        if not ffmpeg or not ffprobe:
            raise RuntimeError("ffmpeg unavailable")

        input_sha256 = stable_sha256(source)
        audio_mix = load_audio_mix(argv[5]) if len(argv) == 6 else None
        mix_source = source
        audio_mix_receipt = None
        if audio_mix:
            with tempfile.NamedTemporaryFile(
                dir=output.parent,
                prefix=f".{output.stem}.layers-",
                suffix=".mkv",
                delete=False,
            ) as handle:
                composed = Path(handle.name)
            audio_mix_receipt = compose_audio(
                ffmpeg, ffprobe, source, audio_mix, composed
            )
            mix_source = composed
        first = loudnorm_measure(ffmpeg, mix_source)
        target_lra = min(50.0, max(1.0, first["input_lra"]))
        loudnorm = (
            f"loudnorm=I={TARGET_I}:TP={ENCODE_TP}:LRA={target_lra}:"
            f"measured_I={first['input_i']}:measured_TP={first['input_tp']}:"
            f"measured_LRA={first['input_lra']}:measured_thresh={first['input_thresh']}:"
            f"offset={first['target_offset']}:linear=true:print_format=json"
        )
        with tempfile.NamedTemporaryFile(
            dir=output.parent, prefix=f".{output.stem}.mixing-", suffix=".mp4", delete=False
        ) as handle:
            temporary = Path(handle.name)
        mixed = run(
            [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-nostats",
                "-i",
                str(mix_source),
                "-map",
                "0",
                "-map_metadata",
                "0",
                "-c",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-filter:a:0",
                loudnorm,
                "-movflags",
                "+faststart",
                str(temporary),
            ]
        )
        if mixed.returncode != 0:
            raise RuntimeError("two-pass loudnorm failed")
        second_matches = re.findall(r'\{\s*"input_i".*?\}', mixed.stderr, re.DOTALL)
        if not second_matches:
            raise RuntimeError("second-pass evidence missing")
        normalization_type = json.loads(second_matches[-1]).get("normalization_type")
        if normalization_type not in {"linear", "dynamic"}:
            raise RuntimeError("second-pass normalization type missing")

        measured = loudnorm_measure(ffmpeg, temporary)
        correction_db = bounded_gain_correction(measured)
        if correction_db:
            with tempfile.NamedTemporaryFile(
                dir=output.parent, prefix=f".{output.stem}.correcting-", suffix=".mp4", delete=False
            ) as handle:
                correction_temporary = Path(handle.name)
            encode_gain_correction(ffmpeg, temporary, correction_temporary, correction_db)
            temporary.unlink()
            temporary = correction_temporary
            correction_temporary = None
            measured = loudnorm_measure(ffmpeg, temporary)
            if abs(measured["input_i"] - TARGET_I) > TARGET_TOLERANCE:
                raise RuntimeError(
                    "target loudness not reached after bounded correction "
                    f"(measured_i={measured['input_i']:.2f} LUFS, "
                    f"measured_tp={measured['input_tp']:.2f} dBTP, "
                    f"applied_gain={correction_db:.2f} dB)"
                )
        if measured["input_tp"] > TARGET_TP:
            raise RuntimeError(
                f"true peak {measured['input_tp']:.2f} dBTP exceeds {TARGET_TP:.2f} dBTP"
            )
        if first["input_lra"] - measured["input_lra"] > 3.0:
            raise RuntimeError("loudness range was flattened")

        for command in (
            [str(verifier), "--verify-only", str(temporary), argv[3]],
            [
                str(verifier),
                "--loudness-gate-only",
                str(temporary),
                str(TARGET_I),
                str(TARGET_TOLERANCE),
            ],
        ):
            verified = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            if verified.returncode != 0:
                raise RuntimeError("final verification failed")

        probe = run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(temporary),
            ]
        )
        duration = float(probe.stdout.strip())
        if probe.returncode != 0 or not math.isfinite(duration):
            raise RuntimeError("duration unavailable")
        if stable_sha256(source) != input_sha256:
            raise RuntimeError("pre-mix changed during final mix")

        output_sha256 = stable_sha256(temporary)
        output_bytes = temporary.stat().st_size
        os.replace(temporary, output)
        temporary = None
        receipt = {
            "schema": "haru.final_mix.v1",
            "status": "mix_complete",
            "method": "ffmpeg_loudnorm_two_pass",
            "normalization_type": normalization_type,
            "input_sha256": input_sha256,
            "sha256": output_sha256,
            "bytes": output_bytes,
            "duration_seconds": duration,
            "loudness_lufs": measured["input_i"],
            "true_peak_dbfs": measured["input_tp"],
            "loudness_range_lu": measured["input_lra"],
            "target": {
                "integrated_lufs": TARGET_I,
                "true_peak_dbfs": TARGET_TP,
                "encoder_true_peak_dbfs": ENCODE_TP,
                "loudness_range_lu": target_lra,
            },
        }
        if audio_mix_receipt:
            receipt["audio_mix"] = audio_mix_receipt
        print(json.dumps(receipt, separators=(",", ":")))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError, RuntimeError) as error:
        return fail(f"mix_final: {error}")
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        if correction_temporary is not None:
            try:
                correction_temporary.unlink()
            except OSError:
                pass
        if composed is not None:
            try:
                composed.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
