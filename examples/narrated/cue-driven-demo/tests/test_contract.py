from __future__ import annotations

import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


def read(name: str):
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


class NarratedDemoContractTest(unittest.TestCase):
    def test_render_plan_uses_the_existing_worker_contract(self):
        plan = read("render_plan.json")
        self.assertEqual(plan["schema"], "haru.render_plan.v1")
        self.assertEqual(plan["engine"], "remotion")
        self.assertEqual(plan["remotion_dir"], "remotion")
        self.assertEqual(plan["composition"], "NarratedLandscape")
        self.assertEqual(plan["output"], "output/final.mp4")
        self.assertEqual(plan["expected_duration"], 24)
        self.assertIs(plan["skip_pronunciation_gate"], True)
        self.assertEqual(read("project-contract.json")["lane_contract"], "manual.v1")
        self.assertEqual(list(ROOT.rglob("*.pron-ok.json")), [])

    def test_content_captions_cues_and_storyboard_share_one_timeline(self):
        content = read("composition-content.json")
        captions = read("captions.json")
        cues = read("cues.json")
        storyboard = read("storyboard-final-timed.json")

        self.assertEqual(content["schema"], "video_studio.narrated_content.v1")
        self.assertEqual(content["visual_timeline_contract"], "cue_driven.v1")
        self.assertEqual(content["captions"], captions)
        self.assertEqual(storyboard["schema"], "haru.storyboard_timed.v1")
        self.assertEqual(storyboard["visual_timeline_contract"], "cue_driven.v1")
        self.assertEqual(storyboard["duration_seconds"] * 1000, content["durationMs"])
        self.assertEqual(
            [(cue["start"] * 1000, cue["end"] * 1000, cue["text"]) for cue in cues],
            [(cue["startMs"], cue["endMs"], cue["text"]) for cue in captions],
        )

        content_events = [
            (
                event["eventId"],
                event["cue"],
                event["visualState"],
                event["presenterState"],
                event["startMs"],
                event["endMs"],
            )
            for scene in content["scenes"]
            for event in scene["events"]
        ]
        storyboard_events = [
            (
                event["event_id"],
                event["cue"],
                event["visual_state"],
                event["presenter_state"],
                event["start_seconds"] * 1000,
                event["end_seconds"] * 1000,
            )
            for scene in storyboard["scenes"]
            for event in scene["visual_events"]
        ]
        self.assertEqual(content_events, storyboard_events)

    def test_demo_audio_is_explicitly_not_speech(self):
        narration = read("composition-content.json")["media"]["narration"]
        self.assertEqual(narration["kind"], "synthetic_tone_not_speech")
        self.assertRegex(narration["label"].lower(), r"not narration|not speech")
        self.assertFalse((ROOT / "remotion/public/assets/demo-tone.wav").exists())


if __name__ == "__main__":
    unittest.main()
