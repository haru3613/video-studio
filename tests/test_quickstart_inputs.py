import importlib.util
import json
from pathlib import Path
import shutil
import sys

import pytest

import artifact_intake
import project_prepare
from workspace import initialize


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "examples/quickstart/make_inputs.py"
SPEC = importlib.util.spec_from_file_location("quickstart_inputs", SCRIPT)
quickstart_inputs = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(quickstart_inputs)

TOOLS = all(shutil.which(name) for name in ("espeak-ng", "ffmpeg", "ffprobe"))


def test_refuses_nonempty_and_symlinked_outputs(tmp_path):
    with pytest.raises(quickstart_inputs.QuickstartError, match="explicit"):
        quickstart_inputs.generate(tmp_path / "implicit", demo_voice=False)

    with pytest.raises(quickstart_inputs.QuickstartError, match="outside"):
        quickstart_inputs.prepare_output(ROOT / "generated-quickstart")

    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(quickstart_inputs.QuickstartError, match="must be empty"):
        quickstart_inputs.prepare_output(nonempty)
    assert (nonempty / "keep.txt").read_text(encoding="utf-8") == "keep"

    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(quickstart_inputs.QuickstartError, match="symlink"):
        quickstart_inputs.prepare_output(linked)

    ancestor = tmp_path / "ancestor"
    ancestor.symlink_to(real, target_is_directory=True)
    with pytest.raises(quickstart_inputs.QuickstartError, match="symlink"):
        quickstart_inputs.prepare_output(ancestor / "child")


@pytest.mark.skipif(not TOOLS, reason="espeak-ng, ffmpeg and ffprobe are required")
def test_generated_pack_prepares_with_the_public_project_contract(tmp_path):
    inputs = tmp_path / "inputs"
    result = quickstart_inputs.generate(inputs, demo_voice=True)
    source_spec = json.loads((inputs / "project.json").read_text(encoding="utf-8"))

    assert 15 <= result["duration_seconds"] <= 25
    assert set(path.name for path in inputs.iterdir()) == {
        "voice.wav",
        "captions.srt",
        "project.json",
        "PROVENANCE.md",
    }
    assert [scene["visual"]["kind"] for scene in source_spec["scenes"]] == [
        "cards",
        "steps",
        "signal",
    ]

    workspace = (tmp_path / "workspace").resolve()
    initialize(workspace)
    project = workspace / "projects/quickstart"
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
    inbox = workspace / "inbox"
    shutil.copy2(inputs / "voice.wav", inbox / "voice.wav")
    voice_stage = artifact_intake.stage_inbox(
        workspace, project, "reference_audio", "voice.wav", "quickstart-owner"
    )
    captions_stage = artifact_intake.stage_text(
        workspace,
        project,
        "subtitle",
        (inputs / "captions.srt").read_text(encoding="utf-8"),
        "quickstart-owner",
    )
    stage_ids = {"voice": voice_stage["stage_id"], "captions": captions_stage["stage_id"]}
    staged_spec = dict(source_spec)
    staged_spec["assets"] = [
        {"id": asset["id"], "kind": asset["kind"], "stage_id": stage_ids[asset["id"]]}
        for asset in source_spec["assets"]
    ]
    (project / "project-spec.json").write_text(
        json.dumps(staged_spec), encoding="utf-8"
    )

    prepared = project_prepare.prepare(project, "quickstart-owner")

    assert prepared["duration_seconds"] == result["duration_seconds"]
    assert prepared["asset_count"] == 2
    assert (project / "remotion/src/content.json").is_file()
