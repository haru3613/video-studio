#!/usr/bin/env python3
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import render_contract


class RenderContractTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.project = Path(self.temporary.name) / "demo"
        (self.project / "output").mkdir(parents=True)
        self.video = self.project / "output/final.mp4"
        self.video.write_bytes(b"final")
        self.digest = hashlib.sha256(b"final").hexdigest()
        self.marker = {
            "schema": "haru.render_result.v1",
            "status": "render_complete",
            "project": "demo",
            "output": "output/final.mp4",
            "video_sha256": self.digest,
            "bytes": 5,
            "duration_seconds": 1,
            "loudness_lufs": -14,
            "true_peak_dbfs": -1,
            "loudness_range_lu": 5,
            "mix": {
                "schema": "haru.final_mix.v1",
                "method": "ffmpeg_loudnorm_two_pass",
                "normalization_type": "linear",
                "input_sha256": "a" * 64,
                "target": {
                    "integrated_lufs": -14.0,
                    "true_peak_dbfs": -1.0,
                    "loudness_range_lu": 5,
                },
            },
        }
        self.marker["render_input_revision"] = render_contract.render_input_revision(
            self.project
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_valid_result_is_digest_bound_and_fail_closed(self):
        self.assertTrue(
            render_contract.valid_final_result(self.project, self.marker, self.video)
        )
        self.marker["mix"]["target"]["loudness_range_lu"] = 20
        self.assertFalse(
            render_contract.valid_final_result(self.project, self.marker, self.video)
        )

    def test_parser_normalizes_success_without_accepting_invalid_json(self):
        receipt = Path(self.temporary.name) / "receipt.json"
        receipt.write_text(json.dumps(self.marker))
        self.assertEqual(render_contract.parse_render_result(receipt)["status"], "pass")
        receipt.write_text("{broken")
        self.assertEqual(
            render_contract.parse_render_result(receipt)["status"], "unknown"
        )

    def test_optional_audio_mix_receipt_must_bind_its_assets(self):
        self.marker["mix"]["audio_mix"] = {
            "schema": "haru.audio_mix.v1",
            "plan_sha256": "b" * 64,
            "ducking": "sidechaincompress.v1",
            "background_music": {"path": "audio/bgm.wav", "sha256": "c" * 64},
            "sound_effects": [],
        }
        self.assertTrue(
            render_contract.valid_final_result(self.project, self.marker, self.video)
        )
        self.marker["mix"]["audio_mix"]["background_music"]["sha256"] = "broken"
        self.assertFalse(
            render_contract.valid_final_result(self.project, self.marker, self.video)
        )
        self.marker["mix"]["audio_mix"]["sound_effects"] = {"not": "a list"}
        self.assertFalse(
            render_contract.valid_final_result(self.project, self.marker, self.video)
        )


if __name__ == "__main__":
    unittest.main()


def test_a_render_from_a_superseded_take_is_not_complete(tmp_path):
    """The last instance of this session's recurring failure.

    The marker records which narration and which contract were rendered, and
    nothing compared them to the files on disk. So a render from a previous take
    stayed "complete" forever: render-project returned the old marker instantly
    and never re-rendered, while the narration, the cut and the duration had all
    moved on. Measured on a real project -- a marker claiming 957.6s came back
    for a 1093.7s narration.
    """
    project = tmp_path / "demo"
    (project / "output").mkdir(parents=True)
    video = project / "output/final.mp4"
    video.write_bytes(b"rendered video")
    narration = project / "narration-final.mp3"
    narration.write_bytes(b"the take that was rendered")
    contract = project / "editorial-contract.json"
    contract.write_text('{"schema": "haru.editorial_contract.v1"}', encoding="utf-8")

    marker = {
        "schema": "haru.render_result.v1",
        "status": "render_complete",
        "project": "demo",
        "output": "output/final.mp4",
        "video_sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
        "bytes": video.stat().st_size,
        "narration_sha256": hashlib.sha256(narration.read_bytes()).hexdigest(),
        "editorial_contract_sha256": hashlib.sha256(contract.read_bytes()).hexdigest(),
        "duration_seconds": 957.6,
        "loudness_lufs": -14.0,
        "true_peak_dbfs": -2.0,
        "loudness_range_lu": 3.2,
        "mix": {
            "schema": "haru.final_mix.v1",
            "method": "ffmpeg_loudnorm_two_pass",
            "normalization_type": "dynamic",
            "input_sha256": "a" * 64,
            "target": {
                "integrated_lufs": -14.0,
                "true_peak_dbfs": -1.0,
                "loudness_range_lu": 3.8,
            },
        },
    }
    marker["render_input_revision"] = render_contract.render_input_revision(project)
    assert render_contract.valid_final_result(project, marker, video) is True

    # A new narration take: same video on disk, same marker, different project.
    narration.write_bytes(b"the take that was approved after it")
    assert render_contract.valid_final_result(project, marker, video) is False

    # And the same for a re-timed cut.
    narration.write_bytes(b"the take that was rendered")
    contract.write_text(
        '{"schema": "haru.editorial_contract.v1", "shots": []}', encoding="utf-8"
    )
    assert render_contract.valid_final_result(project, marker, video) is False


def test_a_marker_without_those_digests_is_still_judged_on_what_it_has(tmp_path):
    # Lanes with no editorial contract record none, and demanding one would
    # refuse every render outside the longform profile.
    project = tmp_path / "demo"
    (project / "output").mkdir(parents=True)
    video = project / "output/final.mp4"
    video.write_bytes(b"rendered video")
    marker = {
        "schema": "haru.render_result.v1",
        "status": "render_complete",
        "project": "demo",
        "output": "output/final.mp4",
        "video_sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
        "bytes": video.stat().st_size,
        "duration_seconds": 10.0,
        "loudness_lufs": -14.0,
        "true_peak_dbfs": -2.0,
        "loudness_range_lu": 3.2,
        "mix": {
            "schema": "haru.final_mix.v1",
            "method": "ffmpeg_loudnorm_two_pass",
            "normalization_type": "dynamic",
            "input_sha256": "a" * 64,
            "target": {
                "integrated_lufs": -14.0,
                "true_peak_dbfs": -1.0,
                "loudness_range_lu": 3.8,
            },
        },
    }
    marker["render_input_revision"] = render_contract.render_input_revision(project)
    assert render_contract.valid_final_result(project, marker, video) is True


def test_a_current_render_is_never_superseded(tmp_path):
    # render_project happens to ask valid_final_result first, so this clause is
    # redundant there. It is not redundant here: a predicate named "superseded"
    # that answers True for the render you are looking at is a trap for the next
    # caller, who will not know the order matters.
    project = tmp_path / "demo"
    (project / "output").mkdir(parents=True)
    video = project / "output/final.mp4"
    video.write_bytes(b"rendered video")
    narration = project / "narration-final.mp3"
    narration.write_bytes(b"the current take")
    marker = {
        "schema": "haru.render_result.v1",
        "status": "render_complete",
        "project": "demo",
        "output": "output/final.mp4",
        "video_sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
        "bytes": video.stat().st_size,
        "narration_sha256": hashlib.sha256(narration.read_bytes()).hexdigest(),
        "duration_seconds": 10.0,
        "loudness_lufs": -14.0,
        "true_peak_dbfs": -2.0,
        "loudness_range_lu": 3.2,
        "mix": {
            "schema": "haru.final_mix.v1",
            "method": "ffmpeg_loudnorm_two_pass",
            "normalization_type": "dynamic",
            "input_sha256": "a" * 64,
            "target": {
                "integrated_lufs": -14.0,
                "true_peak_dbfs": -1.0,
                "loudness_range_lu": 3.8,
            },
        },
    }
    marker["render_input_revision"] = render_contract.render_input_revision(project)
    assert render_contract.valid_final_result(project, marker, video) is True
    assert render_contract.superseded_final_result(project, marker, video) is False

    narration.write_bytes(b"a later take")
    assert render_contract.valid_final_result(project, marker, video) is False
    assert render_contract.superseded_final_result(project, marker, video) is True


def test_full_revision_tracks_visual_and_audio_inputs_but_not_generated_state(tmp_path):
    project = tmp_path / "demo"
    (project / "remotion/src").mkdir(parents=True)
    (project / "remotion/node_modules/.cache").mkdir(parents=True)
    (project / "audio").mkdir()
    (project / "output").mkdir()
    (project / "quality-review").mkdir()
    content = project / "remotion/src/content.json"
    content.write_text('{"title":"first"}', encoding="utf-8")
    music = project / "audio/bed.wav"
    music.write_bytes(b"music-one")
    initial = render_contract.render_input_revision(project)

    (project / "output/cover.png").write_bytes(b"generated cover")
    (project / "quality-review/review.json").write_text("{}", encoding="utf-8")
    (project / "remotion/node_modules/.cache/compiler.bin").write_bytes(b"cache")
    assert render_contract.render_input_revision(project) == initial

    output_source = project / "output/final-title.json"
    output_source.write_text('{"title":"input"}', encoding="utf-8")
    assert render_contract.render_input_revision(project) != initial
    output_source.unlink()
    assert render_contract.render_input_revision(project) == initial

    content.write_text('{"title":"second"}', encoding="utf-8")
    visual_revision = render_contract.render_input_revision(project)
    assert visual_revision != initial
    content.write_text('{"title":"first"}', encoding="utf-8")
    assert render_contract.render_input_revision(project) == initial

    music.write_bytes(b"music-two")
    assert render_contract.render_input_revision(project) != initial


def test_remotion_content_change_makes_a_playable_final_superseded(tmp_path):
    project = tmp_path / "demo"
    (project / "remotion/src").mkdir(parents=True)
    (project / "output").mkdir()
    content = project / "remotion/src/content.json"
    content.write_text('{"title":"first"}', encoding="utf-8")
    video = project / "output/final.mp4"
    video.write_bytes(b"rendered video")
    marker = {
        "schema": "haru.render_result.v1",
        "status": "render_complete",
        "render_input_revision": render_contract.render_input_revision(project),
        "project": "demo",
        "output": "output/final.mp4",
        "video_sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
        "bytes": video.stat().st_size,
        "duration_seconds": 10.0,
        "loudness_lufs": -14.0,
        "true_peak_dbfs": -2.0,
        "loudness_range_lu": 3.2,
        "mix": {
            "schema": "haru.final_mix.v1",
            "method": "ffmpeg_loudnorm_two_pass",
            "normalization_type": "dynamic",
            "input_sha256": "a" * 64,
            "target": {
                "integrated_lufs": -14.0,
                "true_peak_dbfs": -1.0,
                "loudness_range_lu": 3.8,
            },
        },
    }
    assert render_contract.valid_final_result(project, marker, video)
    content.write_text('{"title":"changed"}', encoding="utf-8")
    assert not render_contract.valid_final_result(project, marker, video)
    assert render_contract.superseded_final_result(project, marker, video)

    content.write_text('{"title":"first"}', encoding="utf-8")
    legacy = dict(marker)
    legacy.pop("render_input_revision")
    assert not render_contract.valid_final_result(project, legacy, video)
    assert render_contract.superseded_final_result(project, legacy, video)
