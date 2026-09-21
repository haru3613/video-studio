import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import artifact_intake
import project_prepare
from workspace import initialize


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

pytestmark = pytest.mark.skipif(
    not FFMPEG or not FFPROBE, reason="ffmpeg and ffprobe are required"
)


def write_audio(path: Path, seconds: float = 2.0) -> bytes:
    subprocess.run(
        [
            FFMPEG,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r=48000:cl=mono:d={seconds}",
            "-c:a",
            "pcm_s16le",
            str(path),
        ],
        check=True,
    )
    return path.read_bytes()


def srt(end: str = "00:00:02,000") -> bytes:
    return f"1\n00:00:00,000 --> {end}\nImported words\n".encode()


@pytest.fixture
def prepared_project(tmp_path):
    workspace = (tmp_path / "workspace").resolve()
    initialize(workspace)
    project = workspace / "projects" / "narrated"
    (project / ".hvp").mkdir(parents=True)
    (project / "output").mkdir()
    (project / "project-contract.json").write_text(
        json.dumps(
            {
                "schema": "haru.project_contract.v1",
                "lane_contract": "manual.v1",
                "runtime_contract": {"schema": "haru.runtime.v1"},
            }
        ),
        encoding="utf-8",
    )
    return workspace, project


def stage_inputs(workspace: Path, project: Path, owner: str, *, audio: bytes, captions: bytes):
    inbox = workspace / "inbox"
    (inbox / "voice.wav").write_bytes(audio)
    audio_stage = artifact_intake.stage_inbox(
        workspace, project, "reference_audio", "voice.wav", owner
    )
    caption_stage = artifact_intake.stage_text(
        workspace, project, "subtitle", captions.decode("utf-8"), owner
    )
    return audio_stage, caption_stage


def spec(audio_stage: dict, caption_stage: dict, *, scenes=None):
    value = {
        "schema": project_prepare.SCHEMA,
        "title": "Imported narration",
        "format": "landscape",
        "narration": {
            "mode": "import",
            "audio": "voice",
            "captions": "captions",
            "provider": "self-recorded",
        },
        "assets": [
            {"id": "voice", "kind": "audio", "stage_id": audio_stage["stage_id"]},
            {
                "id": "captions",
                "kind": "subtitle",
                "stage_id": caption_stage["stage_id"],
            },
        ],
    }
    if scenes is not None:
        value["scenes"] = scenes
    return value


def write_spec(project: Path, value: dict):
    (project / "project-spec.json").write_text(
        json.dumps(value, ensure_ascii=False), encoding="utf-8"
    )


def prepared_audio(project: Path) -> Path:
    content = json.loads((project / "remotion/src/content.json").read_text())
    return project / "remotion/public" / content["media"]["narration"]["path"]


def test_prepare_preserves_imported_audio_and_srt_without_approvals(prepared_project):
    workspace, project = prepared_project
    audio = write_audio(workspace / "inbox" / "source.wav")
    captions = srt()
    audio_stage, caption_stage = stage_inputs(
        workspace, project, "lease-owner", audio=audio, captions=captions
    )
    previous_final = project / "output/final.mp4"
    previous_marker = Path(str(previous_final) + ".render-result")
    previous_final.write_bytes(b"prior playable bytes")
    previous_marker.write_text('{"prior":true}\n', encoding="utf-8")
    write_spec(project, spec(audio_stage, caption_stage))

    result = project_prepare.prepare(project, "lease-owner")

    assert result["narration_source"] == "import"
    assert prepared_audio(project).read_bytes() == audio
    assert (project / "narration-final.srt").read_bytes() == captions
    source = json.loads((project / "narration-source.json").read_text())
    assert source == {
        "schema": "video_studio.narration_source.v1",
        "mode": "import",
        "provider": "self-recorded",
        "audio_sha256": hashlib.sha256(audio).hexdigest(),
        "captions_sha256": hashlib.sha256(captions).hexdigest(),
        "pronunciation_reviewed": False,
        "retimed": False,
    }
    assert previous_final.read_bytes() == b"prior playable bytes"
    assert previous_marker.read_text(encoding="utf-8") == '{"prior":true}\n'
    assert not (project / "narration-final.mp3.pron-ok.json").exists()
    assert not (project / "publish").exists()
    assert not (project / "quality-review").exists()


def test_scene_gap_rejects_without_mutating_existing_project(prepared_project):
    workspace, project = prepared_project
    audio = write_audio(workspace / "inbox" / "source.wav")
    audio_stage, caption_stage = stage_inputs(
        workspace, project, "lease-owner", audio=audio, captions=srt()
    )
    sentinel = project / "existing.txt"
    sentinel.write_text("preserve me", encoding="utf-8")
    write_spec(
        project,
        spec(
            audio_stage,
            caption_stage,
            scenes=[
                {
                    "id": "one",
                    "start_seconds": 0,
                    "end_seconds": 0.5,
                    "heading": "One",
                },
                {
                    "id": "two",
                    "start_seconds": 0.75,
                    "end_seconds": 2,
                    "heading": "Two",
                },
            ],
        ),
    )

    with pytest.raises(project_prepare.PrepareError, match="scenes must cover"):
        project_prepare.prepare(project, "lease-owner")

    assert sentinel.read_text(encoding="utf-8") == "preserve me"
    assert not (project / "remotion").exists()
    assert not (project / "narration-final.srt").exists()


def test_reprepare_new_staged_assets_replaces_inputs_but_keeps_prior_final(prepared_project):
    workspace, project = prepared_project
    first_audio = write_audio(workspace / "inbox" / "source.wav")
    first_audio_stage, first_caption_stage = stage_inputs(
        workspace, project, "lease-owner", audio=first_audio, captions=srt()
    )
    write_spec(project, spec(first_audio_stage, first_caption_stage))
    project_prepare.prepare(project, "lease-owner")
    previous_final = project / "output/final.mp4"
    previous_final.write_bytes(b"first render remains available")

    second_audio = write_audio(workspace / "inbox" / "replacement.wav", seconds=1.5)
    (workspace / "inbox" / "voice.wav").write_bytes(second_audio)
    second_audio_stage = artifact_intake.stage_inbox(
        workspace, project, "reference_audio", "voice.wav", "lease-owner"
    )
    second_caption_stage = artifact_intake.stage_text(
        workspace,
        project,
        "subtitle",
        "1\n00:00:00,000 --> 00:00:01,500\nReplacement words\n",
        "lease-owner",
    )
    write_spec(project, spec(second_audio_stage, second_caption_stage))

    project_prepare.prepare(project, "lease-owner")

    assert prepared_audio(project).read_bytes() == second_audio
    assert (project / "narration-final.srt").read_text(encoding="utf-8").endswith(
        "Replacement words\n"
    )
    assert previous_final.read_bytes() == b"first render remains available"


def test_invalid_spec_and_staging_boundaries_do_not_mutate_project(prepared_project):
    workspace, project = prepared_project
    audio = write_audio(workspace / "inbox" / "source.wav")
    audio_stage, caption_stage = stage_inputs(
        workspace, project, "owner-a", audio=audio, captions=srt()
    )
    spec_path = project / "project-spec.json"
    spec_path.write_text("{invalid", encoding="utf-8")
    before = {path.relative_to(project): path.read_bytes() for path in project.rglob("*") if path.is_file()}

    with pytest.raises(project_prepare.PrepareError, match="project spec"):
        project_prepare.prepare(project, "owner-a")
    assert {
        path.relative_to(project): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
    } == before

    write_spec(project, spec(audio_stage, caption_stage))
    with pytest.raises(artifact_intake.IntakeError, match="stage_owner_mismatch"):
        project_prepare.prepare(project, "owner-b")
    assert not (project / "remotion").exists()

    manifest = (
        project / audio_stage["blob"]
    ).with_name("manifest.json")
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["created_at"] = 0
    value["expires_at"] = artifact_intake.STAGE_TTL_SECONDS
    os.chmod(manifest.parent, 0o755)
    os.chmod(manifest, 0o644)
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(artifact_intake.IntakeError, match="stage_expired"):
        project_prepare.prepare(project, "owner-a")
    assert not (project / "remotion").exists()


def test_symlinked_project_spec_cannot_escape_canonical_project(prepared_project, tmp_path):
    _workspace, project = prepared_project
    outside = tmp_path / "outside-spec.json"
    outside.write_text('{"schema":"video_studio.project_spec.v1"}', encoding="utf-8")
    (project / "project-spec.json").symlink_to(outside)

    with pytest.raises(project_prepare.PrepareError, match="must not use symlinks"):
        project_prepare.prepare(project, "lease-owner")

    assert outside.read_text(encoding="utf-8") == '{"schema":"video_studio.project_spec.v1"}'
    assert not (project / "remotion").exists()


def test_subtitle_gaps_preserve_times_and_hold_authored_visual(prepared_project):
    workspace, project = prepared_project
    audio = write_audio(workspace / "inbox/source.wav")
    captions = b"1\n00:00:00,250 --> 00:00:00,750\nFirst\n\n2\n00:00:01,000 --> 00:00:01,500\nSecond\n"
    voice, subs = stage_inputs(workspace, project, "owner", audio=audio, captions=captions)
    write_spec(project, spec(voice, subs))
    project_prepare.prepare(project, "owner")
    content = json.loads((project / "remotion/src/content.json").read_text())
    assert [(c["startMs"], c["endMs"]) for c in content["captions"]] == [(250, 750), (1000, 1500)]
    assert content["scenes"][0]["startMs"] == 0
    assert content["scenes"][-1]["endMs"] == 2000
    assert (project / "narration-final.srt").read_bytes() == captions


def test_short_video_is_rejected_before_project_materialization(prepared_project):
    workspace, project = prepared_project
    audio = write_audio(workspace / "inbox/source.wav")
    voice, subs = stage_inputs(workspace, project, "owner", audio=audio, captions=srt())
    clip = workspace / "inbox/short.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x64:r=30", "-t", "1.933", "-c:v", "libx264", str(clip)], check=True)
    staged = artifact_intake.stage_inbox(workspace, project, "source_video", "short.mp4", "owner")
    value = spec(voice, subs, scenes=[{"id":"clip", "start_seconds":0, "end_seconds":2, "heading":"Footage", "visual":{"kind":"video", "asset":"clip"}}])
    value["assets"].append({"id":"clip", "kind":"video", "stage_id":staged["stage_id"]})
    write_spec(project, value)
    with pytest.raises(project_prepare.PrepareError, match="shorter than its scene"):
        project_prepare.prepare(project, "owner")
    assert not (project / "remotion").exists()
