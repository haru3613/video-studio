import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import editorial_contract


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class MinaLongformEditorialContractTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.project = Path(self.directory.name) / "mina-longform"
        self.project.mkdir()
        (self.project / "project-contract.json").write_text(
            json.dumps(
                {
                    "schema": "haru.project_contract.v1",
                    "lane_contract": "social_issue_longform.v1",
                    "production_profile": "mina_longform.v1",
                }
            )
        )
        self.assets = self.project / "assets"
        self.assets.mkdir()
        for name in ("mina.mp4", "broll.mp4", "broll-2.mp4", "motion.mp4", "preview.mp4"):
            (self.assets / name).write_bytes(b"\x00\x00\x00\x18ftypmp42" + name.encode())
        (self.assets / "motion-source.tsx").write_text("export default motionScene;\n")
        (self.assets / "motion.mp4.motion-canvas.json").write_text(
            json.dumps(
                {
                    "schema": "haru.motion_canvas_render.v1",
                    "engine": "motion_canvas",
                    "engine_version": "3.17.2",
                    "design_id": "system-path",
                    "semantic_purpose": "Explain the system path.",
                    "cue_ids": ["system-motion"],
                    "output": "assets/motion.mp4",
                    "output_sha256": sha256(self.assets / "motion.mp4"),
                    "source": "assets/motion-source.tsx",
                    "source_sha256": sha256(self.assets / "motion-source.tsx"),
                }
            )
        )
        self.write_source_receipt("broll.mp4", ["evidence"])
        self.write_source_receipt("broll-2.mp4", ["evidence-pip", "evidence-close"])

        events = [
            ("host-open", 0.0, 18.0, "talking"),
            ("evidence", 18.0, 50.0, "hidden"),
            ("evidence-pip", 50.0, 65.0, "listening"),
            ("system-motion", 65.0, 95.0, "hidden"),
            ("evidence-close", 95.0, 100.0, "hidden"),
        ]
        storyboard = {
            "schema": "haru.storyboard_timed.v1",
            "visual_timeline_contract": "cue_driven.v1",
            "audio_duration_seconds": 100.0,
            "scenes": [
                {
                    "scene_id": "s01",
                    "start_seconds": 0.0,
                    "end_seconds": 100.0,
                    "visual_events": [
                        {
                            "event_id": event_id,
                            "start_seconds": start,
                            "end_seconds": end,
                            "cue": event_id,
                            "visual_state": event_id,
                            "presenter_state": presenter,
                        }
                        for event_id, start, end, presenter in events
                    ],
                }
            ],
        }
        self.storyboard = self.project / "storyboard-final-timed.json"
        self.storyboard.write_text(json.dumps(storyboard))
        self.contract = {
            "schema": "haru.editorial_contract.v1",
            "project": self.project.name,
            "production_profile": "mina_longform.v1",
            "storyboard_sha256": sha256(self.storyboard),
            "shots": [
                self.shot("host-open", 0, 18, "aroll_full", "assets/mina.mp4"),
                self.shot("evidence", 18, 50, "broll_full", "assets/broll.mp4"),
                {
                    **self.shot("evidence-pip", 50, 65, "broll_pip", "assets/broll-2.mp4"),
                    "presenter_asset_path": "assets/mina.mp4",
                    "presenter_asset_sha256": sha256(self.assets / "mina.mp4"),
                    "presenter_id": "mina",
                },
                {
                    **self.shot("system-motion", 65, 95, "motion_graphics", "assets/motion.mp4"),
                    "engine": "motion_canvas",
                    "asset_role": "motion_canvas",
                    "producer_receipt_path": "assets/motion.mp4.motion-canvas.json",
                    "producer_receipt_sha256": sha256(
                        self.assets / "motion.mp4.motion-canvas.json"
                    ),
                },
                self.shot("evidence-close", 95, 100, "broll_full", "assets/broll-2.mp4"),
            ],
        }

    def tearDown(self):
        self.directory.cleanup()

    def shot(self, event_id, start, end, composition, path):
        asset = self.project / path
        role = "mina_aroll" if composition == "aroll_full" else "broll"
        shot = {
            "event_id": event_id,
            "start_seconds": start,
            "end_seconds": end,
            "composition": composition,
            "asset_path": path,
            "asset_sha256": sha256(asset),
            "asset_role": role,
        }
        if composition == "aroll_full":
            shot["presenter_id"] = "mina"
        if composition in {"broll_full", "broll_pip"}:
            shot["source_receipt_path"] = f"{path}.source.json"
            shot["source_receipt_sha256"] = sha256(
                self.project / f"{path}.source.json"
            )
        return shot

    def retime(self, event_id, start, end):
        """Move one shot and its storyboard event together.

        Shot timing must match the storyboard to within 2ms, so a test that
        changes a duration has to change both or it fails for the wrong reason.
        """
        storyboard = json.loads(self.storyboard.read_text())
        for event in storyboard["scenes"][0]["visual_events"]:
            if event["event_id"] == event_id:
                event["start_seconds"], event["end_seconds"] = start, end
        self.storyboard.write_text(json.dumps(storyboard))
        self.contract["storyboard_sha256"] = sha256(self.storyboard)
        for shot in self.contract["shots"]:
            if shot["event_id"] == event_id:
                shot["start_seconds"], shot["end_seconds"] = start, end

    def write_source_receipt(self, name, cue_ids):
        asset = self.assets / name
        (self.assets / f"{name}.source.json").write_text(
            json.dumps(
                {
                    "schema": "haru.broll_source.v1",
                    "output": f"assets/{name}",
                    "output_sha256": sha256(asset),
                    "source_kind": "stock",
                    "source_url": f"https://videos.example/{name}",
                    "provider": "Example Stock",
                    "search_query": "cyber security operations footage",
                    "acquired_at": "2026-08-04T01:00:00Z",
                    "license_or_usage_basis": "fixture license",
                    "semantic_purpose": "Show the system context named by the cue.",
                    "cue_ids": cue_ids,
                    "brand_characters": [],
                }
            )
        )

    def write_contract(self):
        path = self.project / "editorial-contract.json"
        path.write_text(json.dumps(self.contract))
        return path

    def write_preview_review(self, contract_path):
        preview = self.assets / "preview.mp4"
        review = self.project / "quality-review/editorial-preview/review.json"
        review.parent.mkdir(parents=True)
        review.write_text(
            json.dumps(
                {
                    "schema": "haru.editorial_preview_review.v1",
                    "project": self.project.name,
                    "preview": "assets/preview.mp4",
                    "preview_sha256": sha256(preview),
                    "duration_seconds": 75.0,
                    "editorial_contract_sha256": sha256(contract_path),
                    "verdict": "pass",
                    "reviewed_by": "codex",
                }
            )
        )

    def test_complete_mina_longform_contract_passes(self):
        contract_path = self.write_contract()
        self.write_preview_review(contract_path)

        result = editorial_contract.validate_project(self.project, require_preview=True)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["ratios"], {"aroll": 0.18, "broll": 0.52, "motion_graphics": 0.3})

    def test_dynamic_presentation_without_real_shot_types_is_rejected(self):
        self.contract["shots"] = [
            {
                **self.shot("host-open", 0, 18, "motion_graphics", "assets/motion.mp4"),
                "engine": "remotion",
            }
        ]
        self.write_contract()

        result = editorial_contract.validate_project(self.project, require_preview=False)

        self.assertFalse(result["ok"])
        self.assertIn("storyboard events and editorial shots must match exactly", result["problems"])
        self.assertIn("motion_graphics requires engine motion_canvas and asset_role motion_canvas", result["problems"])
        self.assertIn("A-roll ratio must be between 0.15 and 0.20", result["problems"])
        self.assertIn("B-roll ratio must be at least 0.45", result["problems"])

    def test_text_label_cannot_substitute_for_mina_video(self):
        self.contract["shots"][0].pop("asset_path")
        self.contract["shots"][0].pop("asset_sha256")
        self.write_contract()

        result = editorial_contract.validate_project(self.project, require_preview=False)

        self.assertFalse(result["ok"])
        self.assertIn("host-open: direct digest-bound video asset is required", result["problems"])

    def test_preview_must_be_digest_bound_and_60_to_90_seconds(self):
        contract_path = self.write_contract()
        self.write_preview_review(contract_path)
        review = self.project / "quality-review/editorial-preview/review.json"
        value = json.loads(review.read_text())
        value["duration_seconds"] = 45.0
        review.write_text(json.dumps(value))

        result = editorial_contract.validate_project(self.project, require_preview=True)

        self.assertFalse(result["ok"])
        self.assertIn("editorial preview duration must be between 60 and 90 seconds", result["problems"])

    def test_motion_graphics_requires_digest_bound_motion_canvas_receipt(self):
        self.contract["shots"][3].pop("producer_receipt_path")
        self.write_contract()

        result = editorial_contract.validate_project(self.project, require_preview=False)

        self.assertFalse(result["ok"])
        self.assertIn("system-motion: valid Motion Canvas producer receipt is required", result["problems"])

    def test_motion_receipt_requires_a_semantic_design_identity(self):
        receipt = self.assets / "motion.mp4.motion-canvas.json"
        value = json.loads(receipt.read_text())
        value.pop("design_id")
        receipt.write_text(json.dumps(value))
        self.write_contract()

        result = editorial_contract.validate_project(self.project, require_preview=False)

        self.assertFalse(result["ok"])
        self.assertIn("system-motion: valid Motion Canvas producer receipt is required", result["problems"])

    def test_motion_graphics_must_be_at_least_a_quarter_of_runtime(self):
        """The only motion-graphics floor that measures anything.

        `time_storyboard.py` used to carry a second one that required half the
        scenes to declare `source: "motion_graphics"` — a card-template field
        whose vocabulary does not even contain that plural, and which nothing
        verifies against the shots. It was removed. This is the rule that
        survives, so it needs a test proving it can actually fail: it counts
        seconds, against digest-bound assets, on every presenter-pinned lane.
        """
        # 30s of 100s is 30%. Give 10s of it to the B-roll shot that follows.
        self.retime("system-motion", 65, 85)
        self.retime("evidence-close", 85, 100)
        self.write_contract()

        result = editorial_contract.validate_project(self.project, require_preview=False)

        self.assertFalse(result["ok"])
        # Exact, not assertIn: the retiming introduces no other violation, so an
        # unrelated regression cannot keep this test green for the wrong reason.
        self.assertEqual(result["problems"], ["Motion Canvas ratio must be at least 0.25"])
        self.assertEqual(result["ratios"]["motion_graphics"], 0.20)

    def test_broll_requires_digest_bound_source_receipt(self):
        self.contract["shots"][1].pop("source_receipt_path")
        self.write_contract()

        result = editorial_contract.validate_project(self.project, require_preview=False)

        self.assertFalse(result["ok"])
        self.assertIn("evidence: valid B-roll source receipt is required", result["problems"])

    def test_broll_source_receipt_cannot_change_after_contract_is_written(self):
        self.write_contract()
        receipt = self.assets / "broll.mp4.source.json"
        value = json.loads(receipt.read_text())
        value["semantic_purpose"] = "Changed after contract approval."
        receipt.write_text(json.dumps(value))

        result = editorial_contract.validate_project(self.project, require_preview=False)

        self.assertFalse(result["ok"])
        self.assertIn("evidence: valid B-roll source receipt is required", result["problems"])

    def test_presenter_identity_is_mina_and_broll_cannot_feature_haru(self):
        self.contract["shots"][0]["presenter_id"] = "haru"
        receipt = self.assets / "broll.mp4.source.json"
        value = json.loads(receipt.read_text())
        value["brand_characters"] = ["haru"]
        receipt.write_text(json.dumps(value))
        self.write_contract()

        result = editorial_contract.validate_project(self.project, require_preview=False)

        self.assertFalse(result["ok"])
        self.assertIn("host-open: presenter_id must be mina", result["problems"])
        self.assertIn("evidence: valid B-roll source receipt is required", result["problems"])

    def switch_to_haru_tech(self):
        """Move the whole fixture onto the haru_tech.v1 profile."""
        self.contract["production_profile"] = "haru_tech.v1"
        for shot in self.contract["shots"]:
            if shot.get("asset_role") == "mina_aroll":
                shot["asset_role"] = "haru_aroll"
            if shot.get("presenter_id") == "mina":
                shot["presenter_id"] = "haru"

    def test_haru_tech_profile_passes_with_its_own_presenter(self):
        self.switch_to_haru_tech()
        contract_path = self.write_contract()
        self.write_preview_review(contract_path)

        result = editorial_contract.validate_project(self.project, require_preview=True)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["profile"], "haru_tech.v1")
        # Same editorial arithmetic as Mina — only the presenter changed.
        self.assertEqual(result["ratios"], {"aroll": 0.18, "broll": 0.52, "motion_graphics": 0.3})

    def test_haru_tech_profile_rejects_the_mina_presenter(self):
        """The presenter check must bind to the declared profile, not to a default."""
        self.switch_to_haru_tech()
        self.contract["shots"][0]["asset_role"] = "mina_aroll"
        self.contract["shots"][0]["presenter_id"] = "mina"
        self.write_contract()

        result = editorial_contract.validate_project(self.project, require_preview=False)

        self.assertFalse(result["ok"])
        self.assertIn("host-open: asset_role must be haru_aroll", result["problems"])
        self.assertIn("host-open: presenter_id must be haru", result["problems"])

    def rebind_receipt(self, name, **changes):
        """Edit a source receipt AND re-bind its digest.

        Without the re-bind the tamper check fires first and the receipt is rejected
        for having changed, so whatever the test meant to vary is never reached.
        """
        receipt = self.assets / f"{name}.source.json"
        value = json.loads(receipt.read_text())
        value.update(changes)
        receipt.write_text(json.dumps(value))
        for shot in self.contract["shots"]:
            if shot.get("source_receipt_path") == f"assets/{name}.source.json":
                shot["source_receipt_sha256"] = sha256(receipt)

    def test_haru_tech_broll_may_feature_its_own_presenter(self):
        self.switch_to_haru_tech()
        self.rebind_receipt("broll.mp4", brand_characters=["haru"])
        contract_path = self.write_contract()
        self.write_preview_review(contract_path)

        result = editorial_contract.validate_project(self.project, require_preview=True)

        self.assertTrue(result["ok"], result)

    def test_haru_tech_broll_cannot_feature_the_other_presenter(self):
        """The brand-character check must follow the profile, not a hardcoded name.

        The receipt digest is re-bound so `brand_characters` is the only thing that
        differs from the passing case above — otherwise the tamper check rejects the
        receipt first and this asserts nothing about the presenter at all.
        """
        self.switch_to_haru_tech()
        self.rebind_receipt("broll.mp4", brand_characters=["mina"])
        self.write_contract()

        result = editorial_contract.validate_project(self.project, require_preview=False)

        self.assertFalse(result["ok"])
        self.assertIn("evidence: valid B-roll source receipt is required", result["problems"])

    def test_unknown_profile_is_rejected_and_still_reports_other_problems(self):
        self.contract["production_profile"] = "not_a_profile.v1"
        self.contract["shots"][0].pop("asset_path")
        self.contract["shots"][0].pop("asset_sha256")
        self.write_contract()

        result = editorial_contract.validate_project(self.project, require_preview=False)

        self.assertFalse(result["ok"])
        self.assertIn(
            "production_profile must be one of ['haru_tech.v1', 'mina_longform.v1']",
            result["problems"],
        )
        # An unusable profile must not swallow the rest of the report.
        self.assertIn("host-open: direct digest-bound video asset is required", result["problems"])

    def test_creator_owned_broll_cannot_replace_live_sourcing(self):
        for name in ("broll.mp4", "broll-2.mp4"):
            receipt = self.assets / f"{name}.source.json"
            value = json.loads(receipt.read_text())
            value.update(
                {
                    "source_kind": "creator_owned",
                    "approved_by": "harvey",
                    "source_reference": f"Haru Media Library/{name}",
                    "usage_reason": "Approved creator-owned context footage.",
                }
            )
            receipt.write_text(json.dumps(value))
        self.write_contract()

        result = editorial_contract.validate_project(self.project, require_preview=False)

        self.assertFalse(result["ok"])
        self.assertIn("At least 70% of B-roll must come from external live sourcing", result["problems"])

    def test_reused_broll_bytes_fail_diversity_even_under_different_cues(self):
        receipt = self.assets / "broll.mp4.source.json"
        value = json.loads(receipt.read_text())
        value["cue_ids"] = ["evidence", "evidence-pip", "evidence-close"]
        receipt.write_text(json.dumps(value))
        for shot in (self.contract["shots"][2], self.contract["shots"][4]):
            shot["asset_path"] = "assets/broll.mp4"
            shot["asset_sha256"] = sha256(self.assets / "broll.mp4")
            shot["source_receipt_path"] = "assets/broll.mp4.source.json"
        self.write_contract()

        result = editorial_contract.validate_project(self.project, require_preview=False)

        self.assertFalse(result["ok"])
        self.assertIn("B-roll needs at least 2 unique source assets", result["problems"])
        self.assertIn("B-roll asset reuse cannot exceed 45 seconds", result["problems"])

    def test_long_aroll_and_motion_reuse_require_distinct_assets_and_designs(self):
        timings = {
            "host-open": (0, 46),
            "evidence": (46, 110),
            "evidence-pip": (110, 140),
            "system-motion": (140, 204),
            "evidence-close": (204, 255),
        }
        storyboard = json.loads(self.storyboard.read_text())
        storyboard["audio_duration_seconds"] = 255
        for event in storyboard["scenes"][0]["visual_events"]:
            event["start_seconds"], event["end_seconds"] = timings[event["event_id"]]
        self.storyboard.write_text(json.dumps(storyboard))
        self.contract["storyboard_sha256"] = sha256(self.storyboard)
        for shot in self.contract["shots"]:
            shot["start_seconds"], shot["end_seconds"] = timings[shot["event_id"]]
        self.write_contract()

        result = editorial_contract.validate_project(self.project, require_preview=False)

        self.assertFalse(result["ok"])
        self.assertIn("A-roll needs at least 2 unique source assets", result["problems"])
        self.assertIn("A-roll asset reuse cannot exceed 45 seconds", result["problems"])
        self.assertIn("Motion design needs at least 2 unique source assets", result["problems"])
        self.assertIn("Motion design needs at least 2 unique semantic designs", result["problems"])


if __name__ == "__main__":
    unittest.main()
