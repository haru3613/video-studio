import unittest

import time_storyboard
import visual_timeline


class TimeStoryboardVisualEventsTest(unittest.TestCase):
    def test_resolves_visual_events_from_narration_cues(self):
        cues = [
            {"start": 182.108, "end": 197.185, "text": "從零到一的第一段路是聲音"},
            {"start": 197.185, "end": 205.431, "text": "第二步，做文字層的發音預檢"},
            {"start": 205.431, "end": 219.082, "text": "第三步，要聽語氣和斷句"},
            {"start": 219.082, "end": 227.462, "text": "第四步，定稿用 eleven_v3"},
        ]
        scene = {
            "scene_id": "voice",
            "cue_index": 0,
            "start_seconds": 182.108,
            "end_seconds": 227.462,
            "visual_events": [
                {
                    "event_id": f"voice-step-{index}",
                    "marker": cue["text"],
                    "visual_state": f"voice.step.{index}",
                    "presenter_state": "talking" if index == 1 else "hidden",
                }
                for index, cue in enumerate(cues, 1)
            ],
        }

        unmatched = time_storyboard.resolve_visual_events([scene], cues, 30)
        storyboard = {
            "schema": "haru.storyboard_timed.v1",
            "visual_timeline_contract": "cue_driven.v1",
            "scenes": [scene],
        }

        self.assertEqual(unmatched, [])
        self.assertTrue(visual_timeline.valid_timed_storyboard(storyboard))
        active = next(
            event
            for event in scene["visual_events"]
            if event["start_seconds"] <= 212 < event["end_seconds"]
        )
        self.assertEqual(active["event_id"], "voice-step-3")


if __name__ == "__main__":
    unittest.main()
