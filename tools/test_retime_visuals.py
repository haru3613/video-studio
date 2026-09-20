import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("retime_visuals.py")

# time_storyboard derives the last scene's end from the real audio, so the
# fixture needs an mp3 ffprobe can actually measure.
pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe not available",
)


def run(project):
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(project)], capture_output=True, text=True,
    )


def _srt(cues) -> str:
    def stamp(seconds):
        ms = round(seconds * 1000)
        h, ms = divmod(ms, 3_600_000)
        m, ms = divmod(ms, 60_000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    return "".join(
        f"{i}\n{stamp(start)} --> {stamp(end)}\n{text}\n\n"
        for i, (start, end, text) in enumerate(cues, 1)
    )


def _silent_mp3(path, seconds):
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={seconds}",
        "-c:a", "libmp3lame", "-b:a", "64k", str(path),
    ], check=True)


def _project(root, *, cue_shift=0.0):
    """A project whose narration cues can be moved, as a re-take moves them."""
    project = Path(root) / "demo"
    (project / ".hvp").mkdir(parents=True)
    (project / "remotion/public/data").mkdir(parents=True)

    cues = [
        (0.0 + cue_shift, 2.0 + cue_shift, "第一句話"),
        (2.0 + cue_shift, 4.0 + cue_shift, "第二句話"),
        (4.0 + cue_shift, 6.0 + cue_shift, "第三句話"),
    ]
    (project / "narration-final.srt").write_text(_srt(cues), encoding="utf-8")
    _silent_mp3(project / "narration-final.mp3", cues[-1][1])
    # The timing gate refuses a storyboard for narration nothing approved.
    (project / "narration-final.mp3.pron-ok.json").write_text(
        json.dumps({"schema": "haru.pronunciation_approval.v1", "warnings": []}),
        encoding="utf-8")
    (project / "storyboard-scenes.json").write_text(json.dumps({
        "project": "demo", "fps": 30, "visual_timeline_contract": "cue_driven.v1",
        "scenes": [
            {"scene_id": "sc-one", "marker": "第一句話", "source": "motion_graphics",
             "visual_events": [
                 {"event_id": "ev-one", "marker": "第一句話",
                  "visual_state": "one", "presenter_state": "talking"},
                 {"event_id": "ev-two", "marker": "第二句話",
                  "visual_state": "two", "presenter_state": "listening"}]},
            {"scene_id": "sc-two", "marker": "第三句話", "source": "motion_graphics",
             "visual_events": [
                 {"event_id": "ev-three", "marker": "第三句話",
                  "visual_state": "three", "presenter_state": "talking"}]},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    (project / "editorial-contract.json").write_text(json.dumps({
        "schema": "haru.editorial_contract.v1", "project": "demo",
        "production_profile": "host_longform.v1", "storyboard_sha256": "stale",
        "shots": [
            {"event_id": "ev-one", "start_seconds": 99.0, "end_seconds": 99.5,
             "asset_path": "a.mp4", "composition": "broll_full"},
            {"event_id": "ev-two", "start_seconds": 99.0, "end_seconds": 99.5,
             "asset_path": "a.mp4", "composition": "broll_full"},
            {"event_id": "ev-three", "start_seconds": 99.0, "end_seconds": 99.5,
             "asset_path": "a.mp4", "composition": "broll_full"},
        ],
    }, ensure_ascii=False), encoding="utf-8")
    (project / "render_plan.json").write_text(json.dumps({
        "audio_mix": {"sound_effects": [
            {"path": "sfx.wav", "start_seconds": 999.0, "at_scene": "sc-two"},
            {"path": "hand.wav", "start_seconds": 1.5},
        ]},
    }), encoding="utf-8")
    (project / "remotion/public/data/asset-durations.json").write_text(
        json.dumps({"a.mp4": 30.0}), encoding="utf-8")
    # Remotion reads narration through staticFile(), i.e. its own copy.
    (project / "remotion/public/narration-final.mp3").write_bytes(b"the previous take")
    for name in ("editorial-contract.json", "storyboard-final-timed.json", "cues.json"):
        (project / "remotion/public/data" / name).write_text("{}", encoding="utf-8")
    return project


def test_retime_moves_shots_onto_their_own_events():
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(tmp)
        result = run(project)
        assert result.returncode == 0, result.stdout + result.stderr
        receipt = json.loads(result.stdout)

        assert receipt["schema"] == "haru.visual_retime.v1"
        assert receipt["editorial"]["shots"] == 3
        assert receipt["editorial"]["shots_moved"] == 3

        storyboard = json.loads((project / "storyboard-final-timed.json").read_text())
        events = {event["event_id"]: event
                  for scene in storyboard["scenes"] for event in scene["visual_events"]}
        contract = json.loads((project / "editorial-contract.json").read_text())
        for shot in contract["shots"]:
            event = events[shot["event_id"]]
            # Exact, not rounded again: editorial_contract.py compares to 0.002s.
            assert shot["start_seconds"] == event["start_seconds"]
            assert shot["end_seconds"] == event["end_seconds"]
        # And re-bound to the storyboard it was timed against.
        assert contract["storyboard_sha256"] != "stale"


def test_anchored_sound_effects_follow_their_scene_and_others_do_not_move():
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(tmp)
        assert run(project).returncode == 0
        plan = json.loads((project / "render_plan.json").read_text())
        anchored, hand_placed = plan["audio_mix"]["sound_effects"]
        storyboard = json.loads((project / "storyboard-final-timed.json").read_text())
        scene_two = next(s for s in storyboard["scenes"] if s["scene_id"] == "sc-two")

        assert anchored["start_seconds"] == scene_two["start_seconds"]
        # No anchor means no rule to follow -- a hand-placed hit stays put.
        assert hand_placed["start_seconds"] == 1.5


def test_the_renderer_reads_the_re_timed_files_not_the_previous_ones():
    # Remotion imports remotion/public/data/*, and nothing else writes there.
    # A re-time that stopped at the project root would leave the render cutting
    # the old timeline while every gate, which reads the root, reported green.
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(tmp)
        assert run(project).returncode == 0
        data = project / "remotion/public/data"

        assert (data / "editorial-contract.json").read_bytes() == (
            project / "editorial-contract.json").read_bytes()
        assert (data / "storyboard-final-timed.json").read_bytes() == (
            project / "storyboard-final-timed.json").read_bytes()
        cues = json.loads((data / "cues.json").read_text())
        assert [cue["text"] for cue in cues] == ["第一句話", "第二句話", "第三句話"]


def test_a_narration_retake_moves_everything_again():
    # The point of the runner: the same project, a narration that now starts
    # 10s later, and every derived time follows without anyone editing them.
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(tmp)
        assert run(project).returncode == 0
        first = json.loads((project / "editorial-contract.json").read_text())

        # A slower re-take: the same words, the same order, spread out. The
        # opening still lands at zero, which is what makes everything after it
        # move by a different amount rather than a constant offset.
        (project / "narration-final.srt").write_text(
            _srt([(0.0, 2.0, "第一句話"), (5.0, 7.0, "第二句話"), (10.0, 12.0, "第三句話")]),
            encoding="utf-8")
        _silent_mp3(project / "narration-final.mp3", 12.0)
        assert run(project).returncode == 0
        second = json.loads((project / "editorial-contract.json").read_text())

        by_id = lambda contract: {s["event_id"]: s["start_seconds"] for s in contract["shots"]}
        assert by_id(first) == {"ev-one": 0.0, "ev-two": 2.0, "ev-three": 4.0}
        assert by_id(second) == {"ev-one": 0.0, "ev-two": 5.0, "ev-three": 10.0}
        plan = json.loads((project / "render_plan.json").read_text())
        assert plan["audio_mix"]["sound_effects"][0]["start_seconds"] == 10.0


def test_rerunning_an_unchanged_project_changes_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(tmp)
        assert run(project).returncode == 0
        before = (project / "editorial-contract.json").read_bytes()
        second = json.loads(run(project).stdout)
        assert second["editorial"]["shots_moved"] == 0
        assert second["sound_effects"]["moved"] == 0
        assert (project / "editorial-contract.json").read_bytes() == before


def test_a_shot_whose_event_vanished_is_refused():
    # The cut and the narration have genuinely diverged; guessing a time for a
    # shot with no event would put a picture on an arbitrary moment.
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(tmp)
        contract_path = project / "editorial-contract.json"
        contract = json.loads(contract_path.read_text())
        contract["shots"].append({"event_id": "ev-gone", "start_seconds": 1.0,
                                  "end_seconds": 2.0, "asset_path": "a.mp4"})
        contract_path.write_text(json.dumps(contract, ensure_ascii=False), encoding="utf-8")

        result = run(project)
        assert result.returncode == 2, result.stdout
        assert json.loads(result.stdout)["code"] == "editorial_events_unmatched"


def test_an_event_no_shot_covers_is_refused():
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(tmp)
        contract_path = project / "editorial-contract.json"
        contract = json.loads(contract_path.read_text())
        contract["shots"] = [s for s in contract["shots"] if s["event_id"] != "ev-two"]
        contract_path.write_text(json.dumps(contract, ensure_ascii=False), encoding="utf-8")

        result = run(project)
        assert result.returncode == 2, result.stdout
        assert json.loads(result.stdout)["code"] == "editorial_events_unmatched"


def test_a_sound_effect_anchored_to_an_unknown_scene_is_refused():
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(tmp)
        plan_path = project / "render_plan.json"
        plan = json.loads(plan_path.read_text())
        plan["audio_mix"]["sound_effects"][0]["at_scene"] = "sc-nonexistent"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")

        result = run(project)
        assert result.returncode == 2, result.stdout
        assert json.loads(result.stdout)["code"] == "sound_effect_anchor_unmatched"


def test_a_marker_the_narration_no_longer_says_stops_the_retime():
    # time_storyboard cannot place a scene whose opening words are gone, and a
    # storyboard missing a scene must not be handed on as if it were timed.
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(tmp)
        (project / "narration-final.srt").write_text(
            _srt([(0.0, 2.0, "第一句話"), (2.0, 4.0, "第二句話"), (4.0, 6.0, "完全不同")]),
            encoding="utf-8")
        result = run(project)
        assert result.returncode == 2, result.stdout
        assert json.loads(result.stdout)["code"] == "storyboard_timing_failed"


def test_a_project_without_a_renderer_data_directory_still_completes():
    # ai-cyber-eval-escape-2026 imports the contract from the project root, so
    # there is nothing to copy. Refusing would leave that layout re-timed but
    # receiptless, and failing identically on every retry.
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(tmp)
        shutil.rmtree(project / "remotion")
        result = run(project)
        assert result.returncode == 0, result.stdout + result.stderr
        receipt = json.loads(result.stdout)

        assert receipt["render_inputs"] == {
            "synced": False, "reason": "project has no remotion/public/data",
        }
        # Nothing is claimed about files this layout does not have.
        assert "render_cues" not in receipt["artifacts"]
        assert receipt["editorial"]["shots_moved"] == 3


def test_an_asset_path_cannot_reach_outside_the_project():
    # asset_path comes out of the contract, which is project data rather than
    # something this runner authored, so it gets the same containment the other
    # tools in this repo give that field.
    for escape in ("../outside.mp4", "/etc/hosts", "sub/../../outside.mp4"):
        with tempfile.TemporaryDirectory() as tmp:
            project = _project(tmp)
            (Path(tmp) / "outside.mp4").write_bytes(b"not mine")
            contract_path = project / "editorial-contract.json"
            contract = json.loads(contract_path.read_text())
            contract["shots"][0]["asset_path"] = escape
            contract_path.write_text(json.dumps(contract, ensure_ascii=False), encoding="utf-8")

            result = run(project)
            assert result.returncode == 2, (escape, result.stdout)
            assert json.loads(result.stdout)["code"] == "invalid_path"
            durations = json.loads(
                (project / "remotion/public/data/asset-durations.json").read_text())
            assert escape not in durations


def test_a_failed_retime_does_not_leave_a_receipt_claiming_success():
    # The receipt is the only record of which narration the visuals are timed
    # against. A stale one over a half-re-timed project is worse than none.
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(tmp)
        assert run(project).returncode == 0
        assert (project / ".hvp/visual-retime.json").is_file()

        contract_path = project / "editorial-contract.json"
        contract = json.loads(contract_path.read_text())
        contract["shots"].append({"event_id": "ev-gone", "start_seconds": 1.0,
                                  "end_seconds": 2.0, "asset_path": "a.mp4"})
        contract_path.write_text(json.dumps(contract, ensure_ascii=False), encoding="utf-8")

        assert run(project).returncode == 2
        assert not (project / ".hvp/visual-retime.json").exists()


def test_rewriting_a_file_keeps_it_readable():
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(tmp)
        contract = project / "editorial-contract.json"
        contract.chmod(0o644)
        assert run(project).returncode == 0
        assert contract.stat().st_mode & 0o077 == 0o044


def test_an_asset_path_cannot_cross_a_symlink():
    # Resolving and then checking containment misses a symlink that points back
    # INSIDE the project: the resolved path is contained, but the contract still
    # reached the file by a name that is not what it says it is. Refusing each
    # component is what editorial_contract.py does with the same field.
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(tmp)
        (project / "real").mkdir()
        (project / "real/clip.mp4").write_bytes(b"clip")
        (project / "link").symlink_to(project / "real")

        contract_path = project / "editorial-contract.json"
        contract = json.loads(contract_path.read_text())
        contract["shots"][0]["asset_path"] = "link/clip.mp4"
        contract_path.write_text(json.dumps(contract, ensure_ascii=False), encoding="utf-8")

        result = run(project)
        assert result.returncode == 2, result.stdout
        assert json.loads(result.stdout)["code"] == "invalid_path"


def test_it_runs_where_the_runner_runs_it_not_only_in_a_shell():
    # The MCP runner is spawned with a minimal PATH that has no
    # /opt/homebrew/bin, so ffprobe is absent. The first real run through MCP
    # failed with an opaque storyboard_timing_failed while the identical command
    # worked from a shell, which is the kind of gap only this test closes.
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(tmp)
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(project)],
            capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(project)},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)["status"] == "complete"


def test_the_render_plan_is_rebound_to_the_re_timed_contract():
    # render_project_worker refuses a plan whose editorial_contract_sha256 does
    # not equal the contract on disk, and nothing else updates it -- so a
    # re-time that moved every shot but left this behind blocks the render with
    # a message about binding rather than about timing.
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(tmp)
        plan_path = project / "render_plan.json"
        plan = json.loads(plan_path.read_text())
        plan["editorial_contract_sha256"] = "stale"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")

        receipt = json.loads(run(project).stdout)
        assert receipt["render_plan_binding"] == "rebound"
        contract_digest = hashlib.sha256(
            (project / "editorial-contract.json").read_bytes()).hexdigest()
        assert json.loads(plan_path.read_text())["editorial_contract_sha256"] == contract_digest

        # Idempotent: a second run has nothing to rebind.
        assert json.loads(run(project).stdout)["render_plan_binding"] == "unchanged"


def test_the_renderer_gets_the_canonical_narration_not_the_previous_take():
    """The failure a human caught by watching, which no gate had caught.

    Remotion reads narration through staticFile(), which resolves under
    remotion/public/ and nowhere else. Promoting a new take replaces the
    canonical file and leaves that copy behind, so the render comes out as the
    new cut carrying the previous narration -- with everything green, because
    every gate reads the canonical one.
    """
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(tmp)
        static_copy = project / "remotion/public/narration-final.mp3"
        assert static_copy.read_bytes() != (project / "narration-final.mp3").read_bytes()

        receipt = json.loads(run(project).stdout)
        assert receipt["render_inputs"]["static_copies_resynced"] == ["narration-final.mp3"]
        assert static_copy.read_bytes() == (project / "narration-final.mp3").read_bytes()

        # Idempotent: nothing to resync once they agree.
        assert json.loads(run(project).stdout)["render_inputs"]["static_copies_resynced"] == []


def test_a_static_copy_the_project_never_had_is_not_invented():
    # Presence under remotion/public is what says the composition reads it.
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(tmp)
        (project / "remotion/public/narration-final.mp3").unlink()
        assert run(project).returncode == 0
        assert not (project / "remotion/public/narration-final.mp3").exists()
