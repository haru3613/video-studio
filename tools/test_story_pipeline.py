#!/usr/bin/env python3
"""Unit tests for the Video Studio story pipeline.

Run: python3 -m pytest workspace/video-studio/tools/test_story_pipeline.py
"""
import importlib.util
import json
import os


_spec = importlib.util.spec_from_file_location(
    "story_pipeline",
    os.path.join(os.path.dirname(__file__), "story_pipeline.py"),
)
story_pipeline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(story_pipeline)


DUCK_VISUAL_ANALYSIS = {
    "literal_scene": "A black cat stands on a bathtub rim and looks down at a yellow rubber duck floating in water.",
    "main_subjects": ["black cat", "yellow rubber duck"],
    "important_props": ["bathtub", "water", "rubber duck"],
    "location": "bathroom, bathtub",
    "visible_actions": ["cat is looking down at the duck"],
    "body_language": ["cautious", "curious", "suspicious"],
    "implied_emotions": ["curious", "cautious", "suspicious"],
    "visual_tension": "The cat is interested in the duck but the duck is inside water.",
    "production_constraints": [
        "fixed pet-camera style",
        "small movement is suitable",
        "large action scenes would feel unnatural",
    ],
    "things_not_to_invent": ["extra cats", "humans in frame", "sharks", "magic"],
}


def test_duck_visual_analysis_generates_story_atoms():
    atoms = story_pipeline.extract_story_atoms(DUCK_VISUAL_ANALYSIS)

    assert atoms["protagonist"] == "black cat"
    assert atoms["mystery_or_opponent"] == "yellow rubber duck"
    assert atoms["arena"] == "bathtub"
    assert atoms["forbidden_zone"] == "water"
    assert "curiosity" in atoms["core_tension"].lower()
    assert "water" in atoms["fear_or_limitation"].lower()


def test_duck_pipeline_selects_crime_documentary_angle():
    packet = story_pipeline.run_pipeline_from_analysis(
        DUCK_VISUAL_ANALYSIS,
        source_image="uni-black-peering-at-rubber-duck-in-bathtub.png",
    )

    titles = [angle["title"] for angle in packet["angles"]["angles"]]
    assert "浴缸浮屍案" in titles
    assert packet["selected_angle"]["title"] == "浴缸浮屍案"
    assert packet["concept"]["genre"] == "fake crime documentary"
    assert packet["storyboard"]["duration_seconds"] == 12
    assert len(packet["storyboard"]["structure"]) == 4
    assert packet["storyboard"]["structure"][0]["purpose"] == "hook"
    assert packet["storyboard"]["structure"][-1]["purpose"] == "button"


def test_packet_contains_video_validator_spec_compatible_with_validator_v0():
    packet = story_pipeline.run_pipeline_from_analysis(DUCK_VISUAL_ANALYSIS)
    spec = packet["video_validator_spec"]

    assert spec["version"] == "0.1"
    assert "black cat" in spec["subjects"]
    assert "yellow rubber duck" in spec["subjects"]
    assert any("serious detective" in rule for rule in spec["comedy_rules"])
    assert any("cat jumps into water" in item for item in spec["critical_failure_conditions"])
    assert [judge["id"] for judge in spec["judges"]] == [
        "prompt_match",
        "temporal_consistency",
        "physics_causality",
        "short_form_payoff",
    ]


def test_scoring_penalizes_high_action_or_unsupported_angles():
    atoms = story_pipeline.extract_story_atoms(DUCK_VISUAL_ANALYSIS)
    angles = {
        "angles": [
            {
                "title": "浴缸浮屍案",
                "genre_lens": "fake crime documentary",
                "one_line_premise": "A black cat investigates a suspicious duck in the bathtub.",
                "hook_caption": "我家貓今天發現了一具浮屍。",
                "core_joke": "A toy duck is treated like a crime scene.",
                "ending_punchline": "The detective retreats when the case looks like a bath trap.",
                "risk": "Needs captions and small cat movement only.",
                "production_difficulty": 2,
                "required_motion": "small",
            },
            {
                "title": "浴缸怪獸大戰",
                "genre_lens": "monster movie",
                "one_line_premise": "The cat jumps into the bathtub to fight a sea monster.",
                "hook_caption": "牠以為浴缸裡有怪獸。",
                "core_joke": "A huge fantasy battle erupts in the bathroom.",
                "ending_punchline": "The monster explodes into bubbles.",
                "risk": "Requires large action, water combat, and fantasy effects.",
                "production_difficulty": 9,
                "required_motion": "large",
            },
        ]
    }

    scored = story_pipeline.score_angles(angles, atoms, DUCK_VISUAL_ANALYSIS)
    totals = {item["title"]: item["total"] for item in scored["scores"]}

    assert totals["浴缸浮屍案"] > totals["浴缸怪獸大戰"]
    assert scored["winner"]["title"] == "浴缸浮屍案"


def test_visual_analysis_constraints_do_not_penalize_small_angle():
    atoms = story_pipeline.extract_story_atoms(DUCK_VISUAL_ANALYSIS)
    angles = {
        "angles": [
            {
                "title": "浴缸浮屍案",
                "genre_lens": "fake crime documentary",
                "one_line_premise": "A black cat investigates a suspicious duck in the bathtub.",
                "hook_caption": "我家貓今天發現了一具浮屍。",
                "core_joke": "A toy duck is treated like a crime scene.",
                "ending_punchline": "The detective retreats when the case looks like a bath trap.",
                "risk": "Needs captions and small cat movement only.",
                "production_difficulty": 2,
                "required_motion": "small",
            }
        ]
    }

    scored = story_pipeline.score_angles(angles, atoms, DUCK_VISUAL_ANALYSIS)

    assert "Penalized" not in scored["scores"][0]["reason"]
    assert scored["scores"][0]["production_feasibility"] >= 9


def test_prompt_pack_contains_model_handoff_prompts():
    prompt_pack = story_pipeline.build_prompt_pack()

    assert set(prompt_pack) == {
        "visual_analysis",
        "story_atoms",
        "angle_generation",
        "angle_scoring",
        "storyboard",
        "feasibility_judge",
    }
    assert all("Return JSON only" in prompt for prompt in prompt_pack.values())
    assert "Do not create a plot yet" in prompt_pack["visual_analysis"]


def test_cli_writes_story_packet_and_prompt_pack(tmp_path):
    visual_path = tmp_path / "visual_analysis.json"
    out_dir = tmp_path / "story"
    visual_path.write_text(json.dumps(DUCK_VISUAL_ANALYSIS), encoding="utf-8")

    story_pipeline.main([
        "--visual-analysis",
        str(visual_path),
        "--source-image",
        "uni-black-peering-at-rubber-duck-in-bathtub.png",
        "--out",
        str(out_dir),
    ])

    packet = json.loads((out_dir / "story_packet.json").read_text(encoding="utf-8"))
    prompt_pack = json.loads((out_dir / "prompt_pack.json").read_text(encoding="utf-8"))
    handoff = (out_dir / "local_handoff.md").read_text(encoding="utf-8")

    assert packet["selected_angle"]["title"] == "浴缸浮屍案"
    assert packet["video_generation_prompt"]
    assert "visual_analysis" in prompt_pack
    assert "Codex or Claude" in handoff
    assert "visual_analysis.json" in handoff
