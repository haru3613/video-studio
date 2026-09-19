#!/usr/bin/env python3
"""Video Studio story pipeline v0.

This v0 does not call external model APIs. It turns a structured visual analysis
JSON into a complete story packet and emits prompts for local Codex/Claude
handoff. The output is designed to feed the video generation step and the
`video-quality-reviewer/scripts/validate.py --spec` gate.
"""
import argparse
import json
from pathlib import Path


JUDGES = [
    {"id": "prompt_match", "checks": ["prompt_adherence", "expected_story_beats", "must_not_happen"]},
    {"id": "temporal_consistency", "checks": ["subject_consistency", "object_permanence", "scene_continuity"]},
    {"id": "physics_causality", "checks": ["physics", "causal_logic", "motion_quality"]},
    {"id": "short_form_payoff", "checks": ["hook", "pacing", "payoff_readability", "loopability"]},
]


def _text_blob(*values):
    parts = []
    for value in values:
        if isinstance(value, list):
            parts.extend(str(item) for item in value)
        elif isinstance(value, dict):
            parts.append(json.dumps(value, ensure_ascii=False))
        elif value is not None:
            parts.append(str(value))
    return " ".join(parts).casefold()


def _contains(blob, *needles):
    return any(needle.casefold() in blob for needle in needles)


def _pick_subject(items, *needles, default="subject"):
    for item in items:
        if _contains(str(item), *needles):
            return item
    return items[0] if items else default


def _dedupe(items):
    seen = set()
    out = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def build_prompt_pack():
    """Return the model handoff prompts for the full story chain."""
    return {
        "visual_analysis": """You are a visual story analyst.

Analyze the image without creating a story yet.

Return JSON only.

Schema:
{
  "literal_scene": "",
  "main_subjects": [],
  "important_props": [],
  "location": "",
  "visible_actions": [],
  "body_language": [],
  "implied_emotions": [],
  "visual_tension": "",
  "production_constraints": [],
  "things_not_to_invent": []
}

Rules:
- Only describe what is visually supported.
- Do not identify real people.
- Do not create a plot yet.
- Mention uncertainty explicitly.
""",
        "story_atoms": """You are a short-form story architect.

Using the visual analysis, extract story atoms.

Return JSON only.

Schema:
{
  "protagonist": "",
  "mystery_or_opponent": "",
  "arena": "",
  "forbidden_zone": "",
  "desire": "",
  "fear_or_limitation": "",
  "core_tension": "",
  "comic_contrast": "",
  "best_story_engine": ""
}

Visual analysis:
{visual_analysis}
""",
        "angle_generation": """You are a short video concept generator.

Generate 8 possible short-form story angles from the image.

Each angle must be based on:
- the visible image
- a clear genre lens
- a simple short-video hook
- a punchline
- low production complexity

Return JSON only.

Schema:
{
  "angles": [
    {
      "title": "",
      "genre_lens": "",
      "one_line_premise": "",
      "hook_caption": "",
      "core_joke": "",
      "ending_punchline": "",
      "risk": "",
      "production_difficulty": 1
    }
  ]
}

Story atoms:
{story_atoms}
""",
        "angle_scoring": """You are a ruthless short-form creative director.

Score each concept from 1 to 10.

Criteria:
- hook_clarity
- visual_fit
- comedy_strength
- emotional_readability
- caption_potential
- production_feasibility
- replay_value

Return JSON only.

Schema:
{
  "scores": [
    {
      "title": "",
      "hook_clarity": 0,
      "visual_fit": 0,
      "comedy_strength": 0,
      "emotional_readability": 0,
      "caption_potential": 0,
      "production_feasibility": 0,
      "replay_value": 0,
      "total": 0,
      "reason": ""
    }
  ],
  "winner": {
    "title": "",
    "reason": ""
  }
}

Concepts:
{angles}
""",
        "storyboard": """You are a short-form video director.

Turn the winning concept into a 10-12 second vertical pet-cam short.

Return JSON only.

Schema:
{
  "title": "",
  "duration_seconds": 12,
  "structure": [
    {
      "time_range": "0-2s",
      "visual": "",
      "caption": "",
      "narration": "",
      "sound_effect": "",
      "purpose": "hook|setup|turn|button"
    }
  ],
  "final_caption_script": [],
  "video_generation_prompt": "",
  "negative_prompt": "",
  "validation_spec": {
    "must_include": [],
    "must_not_happen": [],
    "continuity_rules": [],
    "comedy_rules": []
  }
}

Winning concept:
{selected_angle}

Story atoms:
{story_atoms}
""",
        "feasibility_judge": """You are a short-form AI video feasibility judge.

Evaluate whether this story concept is visually supported by the source image.

Reject concepts that:
- require large action not implied by the image
- introduce too many new characters
- require complex physics
- require facial expressions that animals cannot reliably show
- require precise object interaction that AI video models may fail
- ignore the strongest visual tension in the image

Return JSON only.

Schema:
{
  "is_supported": true,
  "unsupported_parts": [],
  "suggested_simplification": ""
}

Concept:
{concept}
""",
	    }


def build_local_handoff(has_visual_analysis):
    status = (
        "A `story_packet.json` has already been generated; use this file to review or revise the chosen concept."
        if has_visual_analysis
        else "Run the visual analysis prompt first, write `visual_analysis.json`, then rerun `story_pipeline.py`."
    )
    return f"""# Local Codex/Claude Handoff

This project does not assume Gemini, Qwen, or any hosted story model. Use the
local Codex or Claude session as the reasoning layer, and keep the handoff file
based.

Current status: {status}

## Step 1: Visual analysis

Give Codex or Claude the source image/frame and the `visual_analysis` prompt
from `prompt_pack.json`.

Write the response as JSON only to:

```text
visual_analysis.json
```

The visual analysis must describe only what is visible. Do not write the plot in
this step.

## Step 2: Story packet

Run:

```bash
python3 workspace/video-studio/tools/story_pipeline.py \\
  --visual-analysis visual_analysis.json \\
  --source-image path/to/source.png \\
  --out workspace/video-studio/tmp/story-run
```

Review:

```text
story_packet.json
video_generation_prompt.txt
video_validator_spec.json
```

## Step 3: Optional Claude/Codex refinement

If the first story packet is too predictable, give Codex or Claude:

```text
story_packet.json
prompt_pack.json angle_generation / angle_scoring / storyboard prompts
```

Ask for replacement JSON only, then manually rerun or update the packet.

## Step 4: Generated-video validation

After video generation, run the local validator:

```bash
python3 \\
  workspace/skills/video-quality-reviewer/scripts/validate.py \\
  --video path/to/output.mp4 \\
  --prompt workspace/video-studio/tmp/story-run/video_generation_prompt.txt \\
  --spec workspace/video-studio/tmp/story-run/video_validator_spec.json \\
  --out workspace/video-studio/tmp/story-run/video-validation
```

Then use Codex or Claude to answer the judge prompts in:

```text
video-validation/judge_prompts.json
```

Save each answer as a judge JSON and pass it back with `--judge-report`.
"""


def extract_story_atoms(visual_analysis):
    subjects = visual_analysis.get("main_subjects") or []
    props = visual_analysis.get("important_props") or []
    blob = _text_blob(visual_analysis)

    protagonist = _pick_subject(subjects, "cat", "貓", default="main subject")
    opponent = _pick_subject(subjects + props, "duck", "小黃鴨", "rubber duck", default="important object")

    if _contains(blob, "bathtub", "bath", "浴缸"):
        arena = "bathtub"
    else:
        arena = visual_analysis.get("location") or "visible location"

    forbidden_zone = "water" if _contains(blob, "water", "水") else "risky area"

    if _contains(blob, "duck", "rubber duck", "小黃鴨") and _contains(blob, "water", "水"):
        desire = "understand or inspect the suspicious duck"
        fear = "touching water"
        tension = "curiosity versus fear of water"
        contrast = "a harmless toy duck is treated as a serious threat"
        engine = "small_event_big_stakes"
    else:
        desire = "understand what is happening in the scene"
        fear = "crossing the visible risk or social boundary"
        tension = visual_analysis.get("visual_tension") or "desire versus hesitation"
        contrast = "a small ordinary event is interpreted too seriously"
        engine = "genre_reframe"

    return {
        "protagonist": protagonist,
        "mystery_or_opponent": opponent,
        "arena": arena,
        "forbidden_zone": forbidden_zone,
        "desire": desire,
        "fear_or_limitation": fear,
        "core_tension": tension,
        "comic_contrast": contrast,
        "best_story_engine": engine,
    }


def generate_angles(story_atoms, visual_analysis):
    blob = _text_blob(story_atoms, visual_analysis)
    if _contains(blob, "cat", "black cat") and _contains(blob, "duck", "rubber duck") and _contains(blob, "water", "bathtub"):
        return {
            "angles": [
                {
                    "title": "浴缸浮屍案",
                    "genre_lens": "fake crime documentary",
                    "one_line_premise": "A black cat investigates a suspicious yellow duck floating in the bathtub.",
                    "hook_caption": "我家貓今天發現了一具浮屍。",
                    "core_joke": "The cat treats a harmless bath toy like crime-scene evidence.",
                    "ending_punchline": "The detective concludes the case is actually a bath trap and retreats.",
                    "risk": "Needs captions and small cautious movement only.",
                    "production_difficulty": 2,
                    "required_motion": "small",
                },
                {
                    "title": "洗澡陰謀論",
                    "genre_lens": "conspiracy thriller",
                    "one_line_premise": "The cat suspects the duck is bait in a larger bath-time operation.",
                    "hook_caption": "牠發現事情不單純。",
                    "core_joke": "A normal rubber duck becomes evidence of a coordinated bath conspiracy.",
                    "ending_punchline": "The cat withdraws before the human trap activates.",
                    "risk": "Caption voice must carry the paranoia.",
                    "production_difficulty": 2,
                    "required_motion": "small",
                },
                {
                    "title": "浴缸風險評估",
                    "genre_lens": "corporate risk report",
                    "one_line_premise": "The cat performs a formal risk assessment of one floating duck.",
                    "hook_caption": "今日風險項目：黃色漂浮物。",
                    "core_joke": "A tiny hesitation becomes a boardroom-grade safety decision.",
                    "ending_punchline": "Recommendation: do not enter the water market.",
                    "risk": "May feel clever rather than emotional if captions are too dry.",
                    "production_difficulty": 1,
                    "required_motion": "none",
                },
                {
                    "title": "水生鴨類觀察紀錄",
                    "genre_lens": "nature documentary",
                    "one_line_premise": "A black cat observes the rare bathtub duck from a safe distance.",
                    "hook_caption": "黑貓第一次觀察水生鴨類。",
                    "core_joke": "An ordinary toy is narrated like rare wildlife.",
                    "ending_punchline": "The researcher refuses fieldwork because the habitat is wet.",
                    "risk": "Lower surprise; relies on narration texture.",
                    "production_difficulty": 1,
                    "required_motion": "none",
                },
                {
                    "title": "黃色嫌犯偵訊",
                    "genre_lens": "courtroom interrogation",
                    "one_line_premise": "The cat silently interrogates a duck that refuses to answer.",
                    "hook_caption": "嫌犯全程保持沉默。",
                    "core_joke": "The duck's blank face becomes suspiciously calm.",
                    "ending_punchline": "The detective cannot proceed because evidence is surrounded by water.",
                    "risk": "Needs close captions; less visually active.",
                    "production_difficulty": 2,
                    "required_motion": "small",
                },
                {
                    "title": "黃色水中裝置拆除",
                    "genre_lens": "spy bomb-disposal scene",
                    "one_line_premise": "The cat treats the rubber duck as a suspicious device in the tub.",
                    "hook_caption": "任務：解除黃色水中裝置。",
                    "core_joke": "A bath toy gets the full spy-thriller treatment.",
                    "ending_punchline": "The agent aborts because the device is protected by water.",
                    "risk": "Could imply mechanical interaction that i2v may not execute.",
                    "production_difficulty": 4,
                    "required_motion": "small",
                },
                {
                    "title": "詛咒小黃鴨",
                    "genre_lens": "micro horror",
                    "one_line_premise": "The cat believes the duck is a cursed object floating too calmly.",
                    "hook_caption": "它漂得太安靜了。",
                    "core_joke": "A harmless duck is framed as a cursed artifact.",
                    "ending_punchline": "The cat leaves the curse for the humans.",
                    "risk": "Tone can become too dark for pet-cam comedy.",
                    "production_difficulty": 3,
                    "required_motion": "small",
                },
                {
                    "title": "浴缸怪獸大戰",
                    "genre_lens": "monster movie",
                    "one_line_premise": "The cat imagines a huge battle inside the bathtub.",
                    "hook_caption": "牠以為浴缸裡有怪獸。",
                    "core_joke": "The smallest toy gets inflated into an action sequence.",
                    "ending_punchline": "The battle never happens because the hero refuses to get wet.",
                    "risk": "Requires fantasy stakes and may tempt large action.",
                    "production_difficulty": 8,
                    "required_motion": "large",
                },
            ]
        }

    protagonist = story_atoms["protagonist"]
    opponent = story_atoms["mystery_or_opponent"]
    return {
        "angles": [
            {
                "title": "小事件大陣仗",
                "genre_lens": "fake crisis report",
                "one_line_premise": f"{protagonist} treats {opponent} like a serious incident.",
                "hook_caption": "事情看起來不單純。",
                "core_joke": "A tiny visible event is inflated into a serious crisis.",
                "ending_punchline": "The subject chooses self-preservation over investigation.",
                "risk": "Needs a clear visible tension from the source image.",
                "production_difficulty": 2,
                "required_motion": "small",
            },
            {
                "title": "錯誤職業化",
                "genre_lens": "professional roleplay",
                "one_line_premise": f"{protagonist} behaves like an inspector assigned to {opponent}.",
                "hook_caption": "今日值班：我。",
                "core_joke": "The subject's posture is misread as professional competence.",
                "ending_punchline": "The job is abandoned at the first inconvenience.",
                "risk": "Caption voice must do most of the comedy.",
                "production_difficulty": 2,
                "required_motion": "small",
            },
        ]
    }


def _score_angle(angle, story_atoms, visual_analysis):
    angle_blob = _text_blob(angle)
    context_blob = _text_blob(story_atoms, visual_analysis)
    blob = _text_blob(angle, story_atoms)
    difficulty = int(angle.get("production_difficulty") or 5)
    large_motion = (
        str(angle.get("required_motion", "")).casefold() == "large"
        or _contains(angle_blob, "large action", "fight", "battle", "explodes", "magic", "fantasy", "怪獸", "大戰")
    )
    detective = _contains(angle_blob, "crime", "detective", "浮屍", "嫌犯", "偵探")
    duck_fit = _contains(angle_blob, "duck", "小黃鴨") and _contains(context_blob, "cat", "貓")

    hook = 9 if detective else 7
    visual_fit = 9 if duck_fit else 6
    comedy = 8 if detective else 7
    emotional = 8 if _contains(blob, "suspicious", "cautious", "retreats", "陰謀", "保命") else 6
    caption = 9 if detective or _contains(blob, "report", "documentary", "偵訊", "風險") else 7
    feasibility = max(1, 11 - difficulty)
    replay = 8 if _contains(blob, "retreat", "trap", "回", "案件") else 6

    if large_motion:
        visual_fit -= 2
        comedy -= 1
        feasibility -= 3
        replay -= 1
    if difficulty <= 2 and _contains(blob, "small", "none", "cautious"):
        feasibility += 1
    if _contains(context_blob, "water") and story_atoms.get("forbidden_zone") == "water":
        emotional += 1

    scores = {
        "title": angle["title"],
        "hook_clarity": _clamp(hook),
        "visual_fit": _clamp(visual_fit),
        "comedy_strength": _clamp(comedy),
        "emotional_readability": _clamp(emotional),
        "caption_potential": _clamp(caption),
        "production_feasibility": _clamp(feasibility),
        "replay_value": _clamp(replay),
    }
    scores["total"] = sum(value for key, value in scores.items() if key != "title")
    scores["reason"] = _score_reason(angle, large_motion, detective)
    return scores


def _clamp(value):
    return max(1, min(10, int(value)))


def _score_reason(angle, large_motion, detective):
    if large_motion:
        return "Penalized because it asks for large or fantasy motion that source-image i2v is likely to break."
    if detective:
        return "Wins because the image already looks like cautious inspection, and captions can inflate the stakes without complex motion."
    return "Usable if captions create the reframe while keeping the visible action small."


def score_angles(angles, story_atoms, visual_analysis):
    scores = [_score_angle(angle, story_atoms, visual_analysis) for angle in angles.get("angles", [])]
    winner = max(scores, key=lambda item: item["total"]) if scores else {"title": "", "reason": ""}
    return {
        "scores": scores,
        "winner": {
            "title": winner["title"],
            "reason": winner.get("reason", ""),
        },
    }


def select_angle(angles, scored):
    title = (scored.get("winner") or {}).get("title")
    for angle in angles.get("angles", []):
        if angle.get("title") == title:
            return angle
    return (angles.get("angles") or [{}])[0]


def build_storyboard(selected_angle, story_atoms, visual_analysis):
    if selected_angle.get("title") == "浴缸浮屍案":
        structure = [
            {
                "time_range": "0-2s",
                "visual": "Black cat on the bathtub rim, staring down at the yellow rubber duck in shallow water.",
                "caption": "我家貓今天發現了一具浮屍。",
                "narration": "",
                "sound_effect": "soft crime-documentary sting",
                "purpose": "hook",
            },
            {
                "time_range": "2-6s",
                "visual": "The cat leans in cautiously but keeps all paws dry; the duck floats calmly.",
                "caption": "嫌犯：黃色，會漂浮，表情過於冷靜。",
                "narration": "",
                "sound_effect": "tiny water ambience",
                "purpose": "setup",
            },
            {
                "time_range": "6-9s",
                "visual": "The cat pauses, reassesses the water around the evidence, and pulls back.",
                "caption": "探長分析三秒後發現：這不是命案。",
                "narration": "",
                "sound_effect": "small record scratch or low whoosh",
                "purpose": "turn",
            },
            {
                "time_range": "9-12s",
                "visual": "The duck remains floating safely while the cat retreats with a deadpan look.",
                "caption": "這是一場洗澡陰謀。案件未結。",
                "narration": "",
                "sound_effect": "button hit, then loopable bathroom ambience",
                "purpose": "button",
            },
        ]
    else:
        structure = [
            {
                "time_range": "0-2s",
                "visual": f"{story_atoms['protagonist']} notices {story_atoms['mystery_or_opponent']}.",
                "caption": selected_angle.get("hook_caption", "事情看起來不單純。"),
                "narration": "",
                "sound_effect": "small hook sting",
                "purpose": "hook",
            },
            {
                "time_range": "2-6s",
                "visual": "Hold on the ordinary scene and let the subject hesitate.",
                "caption": selected_angle.get("core_joke", "牠開始評估現場。"),
                "narration": "",
                "sound_effect": "ambient room tone",
                "purpose": "setup",
            },
            {
                "time_range": "6-9s",
                "visual": "The subject makes a tiny choice that reframes the setup.",
                "caption": "三秒後，牠做出判斷。",
                "narration": "",
                "sound_effect": "small turn cue",
                "purpose": "turn",
            },
            {
                "time_range": "9-12s",
                "visual": "End on a deadpan pause that can loop back to the opening.",
                "caption": selected_angle.get("ending_punchline", "先保命。"),
                "narration": "",
                "sound_effect": "button hit",
                "purpose": "button",
            },
        ]

    captions = [beat["caption"] for beat in structure if beat.get("caption")]
    video_prompt = build_video_generation_prompt(selected_angle, story_atoms, visual_analysis)
    negative_prompt = (
        "No text rendered in the video, no subtitles generated by the model, no human hands, "
        "no extra animals, no fantasy effects, no camera cuts, no cat deformation, no duck color change."
    )
    return {
        "title": selected_angle.get("title", "story"),
        "duration_seconds": 12,
        "structure": structure,
        "final_caption_script": captions,
        "video_generation_prompt": video_prompt,
        "negative_prompt": negative_prompt,
        "validation_spec": build_story_validation_spec(selected_angle, story_atoms, visual_analysis, video_prompt),
    }


def build_video_generation_prompt(selected_angle, story_atoms, visual_analysis):
    if selected_angle.get("title") == "浴缸浮屍案":
        return (
            "Vertical 9:16 photorealistic candid pet-camera footage in a real bathroom. "
            "Fixed phone camera, no cuts, no camera movement, no text in the generated image. "
            "A black cat stands on the bathtub rim and cautiously inspects a yellow rubber duck "
            "floating in shallow bath water. Keep the action small and physically plausible: "
            "the cat leans in, pauses, then retreats from the water; the duck stays floating. "
            "The serious detective-documentary tone is created later by captions and sound design, "
            "not by adding props, humans, police tape, or fantasy visuals."
        )
    return (
        "Vertical 9:16 photorealistic candid pet-camera footage. Fixed camera, no cuts, no text. "
        f"{story_atoms['protagonist']} notices {story_atoms['mystery_or_opponent']} in {story_atoms['arena']}. "
        "Keep the motion small, believable, and grounded in the visible source image."
    )


def build_story_validation_spec(selected_angle, story_atoms, visual_analysis, video_prompt):
    subjects = _dedupe([
        story_atoms.get("protagonist"),
        story_atoms.get("mystery_or_opponent"),
        story_atoms.get("arena"),
        story_atoms.get("forbidden_zone"),
    ])
    must_not = [
        "cat jumps into water",
        "rubber duck changes color or identity",
        "extra animals appear",
        "humans enter the frame",
        "bathroom changes into another location",
        "fantasy, monster, police, or crime-scene props appear",
    ]
    return {
        "version": "0.1",
        "source": "video_studio_story_pipeline",
        "prompt": video_prompt,
        "subjects": subjects,
        "expected_story_beats": [
            {"time_window": "00:00-00:02", "beat": "Hook establishes the cat inspecting the duck."},
            {"time_window": "00:02-00:06", "beat": "The scene remains ordinary and physically plausible."},
            {"time_window": "00:06-00:09", "beat": "The comedy turn reframes the duck/water situation."},
            {"time_window": "00:09-00:12", "beat": "The final caption/button lands and loops cleanly."},
        ],
        "must_include": [
            "black cat on bathtub edge",
            "yellow rubber duck floating in water",
            "cat appears cautious and curious",
        ],
        "must_not_happen": must_not,
        "physical_constraints": [
            "The duck should float naturally and not teleport.",
            "The cat should not deform, morph, or move like a human.",
            "Water contact or splash should not appear unless the body visibly causes it.",
            "All motion should remain small enough for a real pet-camera clip.",
        ],
        "continuity_constraints": [
            "The cat remains the same black cat throughout.",
            "The duck remains the same yellow rubber duck throughout.",
            "The bathroom, tub rim, and water level remain spatially stable.",
        ],
        "critical_failure_conditions": [
            "cat jumps into water",
            "main comic turn is not readable",
            "model adds literal crime-scene props instead of keeping the image pet-cam realistic",
            "duck disappears, teleports, or changes color",
            "cat face/body visibly mutates",
            "text appears inside generated video frames",
        ],
        "comedy_rules": [
            "tone should feel like a serious detective documentary applied to a trivial pet-cam event",
            "the cat should behave seriously while the situation remains visually ordinary",
            "captions should reframe the scene, not describe the obvious",
        ],
        "judges": JUDGES,
    }


def run_pipeline_from_analysis(visual_analysis, source_image=None):
    atoms = extract_story_atoms(visual_analysis)
    angles = generate_angles(atoms, visual_analysis)
    scored = score_angles(angles, atoms, visual_analysis)
    selected = select_angle(angles, scored)
    storyboard = build_storyboard(selected, atoms, visual_analysis)
    concept = {
        "title": selected.get("title", ""),
        "genre": selected.get("genre_lens", ""),
        "premise": selected.get("one_line_premise", ""),
    }
    return {
        "version": "0.1",
        "source_image": source_image,
        "visual_analysis": visual_analysis,
        "story_atoms": atoms,
        "angles": angles,
        "angle_scores": scored,
        "selected_angle": selected,
        "concept": concept,
        "short_script": {
            "duration": storyboard["duration_seconds"],
            "captions": storyboard["final_caption_script"],
        },
        "storyboard": storyboard,
        "video_generation_prompt": storyboard["video_generation_prompt"],
        "negative_prompt": storyboard["negative_prompt"],
        "video_validator_spec": storyboard["validation_spec"],
    }


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--visual-analysis", help="Path to visual_analysis.json.")
    ap.add_argument("--source-image", default=None)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    prompt_pack = build_prompt_pack()
    _write_json(out_dir / "prompt_pack.json", prompt_pack)
    (out_dir / "local_handoff.md").write_text(
        build_local_handoff(has_visual_analysis=bool(args.visual_analysis)) + "\n",
        encoding="utf-8",
    )

    if not args.visual_analysis:
        _write_json(
            out_dir / "story_pipeline_status.json",
            {
                "status": "needs_visual_analysis",
                "next_step": "Run the visual_analysis prompt from prompt_pack.json on the source image, then rerun with --visual-analysis.",
            },
        )
        print(json.dumps({"status": "needs_visual_analysis", "out": str(out_dir)}, ensure_ascii=False))
        return

    visual_analysis = _read_json(args.visual_analysis)
    packet = run_pipeline_from_analysis(visual_analysis, source_image=args.source_image)
    _write_json(out_dir / "story_packet.json", packet)
    _write_json(out_dir / "video_validator_spec.json", packet["video_validator_spec"])
    (out_dir / "video_generation_prompt.txt").write_text(packet["video_generation_prompt"] + "\n", encoding="utf-8")
    (out_dir / "negative_prompt.txt").write_text(packet["negative_prompt"] + "\n", encoding="utf-8")
    print(json.dumps(
        {
            "status": "story_ready",
            "selected_angle": packet["selected_angle"]["title"],
            "out": str(out_dir),
        },
        ensure_ascii=False,
    ))


if __name__ == "__main__":
    main()
