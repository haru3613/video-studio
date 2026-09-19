import unittest

import visual_timeline


def timed_storyboard():
    return {
        "schema": "haru.storyboard_timed.v1",
        "visual_timeline_contract": "cue_driven.v1",
        "scenes": [
            {
                "scene_id": "voice",
                "start_seconds": 182.108,
                "end_seconds": 227.462,
                "visual_events": [
                    {
                        "event_id": "voice-step-1",
                        "cue": "從零到一的第一段路是聲音",
                        "visual_state": "voice.step.1",
                        "presenter_state": "talking",
                        "start_seconds": 182.108,
                        "end_seconds": 197.185,
                    },
                    {
                        "event_id": "voice-step-2",
                        "cue": "第二步，做文字層的發音預檢",
                        "visual_state": "voice.step.2",
                        "presenter_state": "hidden",
                        "start_seconds": 197.185,
                        "end_seconds": 205.431,
                    },
                    {
                        "event_id": "voice-step-3",
                        "cue": "第三步，要聽語氣和斷句",
                        "visual_state": "voice.step.3",
                        "presenter_state": "hidden",
                        "start_seconds": 205.431,
                        "end_seconds": 219.082,
                    },
                    {
                        "event_id": "voice-step-4",
                        "cue": "第四步，定稿用 eleven_v3",
                        "visual_state": "voice.step.4",
                        "presenter_state": "hidden",
                        "start_seconds": 219.082,
                        "end_seconds": 227.462,
                    },
                ],
            }
        ],
    }


class VisualTimelineTest(unittest.TestCase):
    def test_cue_driven_timeline_keeps_212_seconds_on_step_three(self):
        storyboard = timed_storyboard()

        self.assertTrue(visual_timeline.valid_timed_storyboard(storyboard))
        active = next(
            event
            for event in storyboard["scenes"][0]["visual_events"]
            if event["start_seconds"] <= 212 < event["end_seconds"]
        )
        self.assertEqual(active["event_id"], "voice-step-3")

    def test_fixed_semantic_cadence_is_rejected(self):
        storyboard = timed_storyboard()
        storyboard["scenes"][0]["visual_events"][2]["focus_period_seconds"] = 8

        checks = {
            check["name"]: check
            for check in visual_timeline.validate_timed_storyboard(storyboard)
        }

        self.assertFalse(checks["no_semantic_timer"]["passed"])

    def test_presenter_state_is_required(self):
        storyboard = timed_storyboard()
        del storyboard["scenes"][0]["visual_events"][0]["presenter_state"]

        checks = {
            check["name"]: check
            for check in visual_timeline.validate_timed_storyboard(storyboard)
        }

        self.assertFalse(checks["presenter_states_explicit"]["passed"])


if __name__ == "__main__":
    unittest.main()
