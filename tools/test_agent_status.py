#!/usr/bin/env python3
import hashlib
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from runtime_test_fixture import RuntimePolicyCase
import zlib

import agent_status
import canonical_layout
import final_quality_authority
import render_self_eval
import render_contract
import self_eval_fixture
import visual_qa_sample


def self_eval_roots(root):
    """Point the external self-eval authority at this test's tmpdir.

    The ledger and attestation roots are read from the environment on every
    call, so setting them per fixture keeps each project's authority inside the
    directory the test owns. After the tmpdir is removed the variables point at
    nothing, which the engine reads as "no anchor" — the correct failure, not a
    silent fallback to a real user directory.
    """
    state = Path(root) / ".self-eval-state"
    attestations = Path(root) / ".self-eval-attestations"
    for path in (state, attestations):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.chmod(0o700)
    os.environ["HARU_VIDEO_STUDIO_SELF_EVAL_STATE_ROOT"] = str(state)
    os.environ["HARU_VIDEO_STUDIO_SELF_EVAL_ATTESTATION_ROOT"] = str(attestations)
    return state, attestations


def seal_self_eval(project, root, *, status="pass", attempt=1):
    """Seal one genuine, ledger-anchored self-eval state for a fixture project.

    Goes through the engine's real prepare/commit path, so a fixture cannot
    manufacture a current pass the external ledger does not anchor — which is
    the property the downstream gates are being tested for.

    `human_intervention_required` only exists at ordinal 3, so pass
    `attempt=3` for it; the engine refuses the inconsistent combination rather
    than inventing a state the lifecycle cannot reach.
    """
    self_eval_roots(root)
    return self_eval_fixture.seal(project, status=status, attempt=attempt)


def write_png(path, width=1280, height=720):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\0" + b"\0" * (width * 3) for _ in range(height))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def write_visual_qa_receipts(project, video):
    sampling = project / visual_qa_sample.OUTPUT_DIR
    sampling.mkdir(parents=True, exist_ok=True)
    sheet = sampling / visual_qa_sample.SHEET_NAME
    index = sampling / visual_qa_sample.INDEX_NAME
    sample = sampling / visual_qa_sample.SAMPLE_NAME
    review = sampling / visual_qa_sample.REVIEW_NAME
    write_png(sheet, 1920, 1080)
    storyboard = json.loads(
        (project / "storyboard-final-timed.json").read_text(encoding="utf-8")
    )
    cues = visual_qa_sample.parse_srt(
        (project / "narration-final.srt").read_text(encoding="utf-8")
    )
    editorial_path = project / "editorial-contract.json"
    editorial = (
        json.loads(editorial_path.read_text(encoding="utf-8"))
        if editorial_path.is_file()
        else None
    )
    frames = visual_qa_sample.plan_samples(
        storyboard["scenes"],
        cues,
        visual_motif=storyboard.get("visual_motif"),
        editorial_shots=editorial.get("shots") if editorial else None,
    )
    index.write_text(
        visual_qa_sample.index_markdown(project, frames), encoding="utf-8"
    )

    def entry(path):
        return {
            "path": str(path.relative_to(project)),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        }

    # The v2 receipts bind the sealed self-eval by digest. The fixture reads the
    # promoted bytes directly rather than asking visual_qa_sample for the ref, so
    # it stays an independent witness instead of a mirror of the code under test.
    self_eval_path = project / canonical_layout.RENDER_SELF_EVAL_RESULT
    receipt = {
        "schema": visual_qa_sample.SAMPLE_SCHEMA,
        "project": project.name,
        "created_at": "2026-07-29T00:00:00+00:00",
        "inputs": {
            "final_video": entry(video),
            "storyboard": entry(project / "storyboard-final-timed.json"),
            "storyboard_validation": entry(
                project / "storyboard-final-timed-validation.json"
            ),
            "srt": entry(project / "narration-final.srt"),
            "render_self_eval": entry(self_eval_path),
        },
        "outputs": {
            "contact_sheet": {
                **entry(sheet),
                "columns": 4,
                "rows": (len(frames) + 3) // 4,
            },
            "index": entry(index),
        },
        "frames": frames,
    }
    if editorial:
        receipt["inputs"]["editorial_contract"] = entry(editorial_path)
    sample.write_text(json.dumps(receipt), encoding="utf-8")
    review.write_text(
        json.dumps(
            {
                "schema": visual_qa_sample.REVIEW_SCHEMA,
                "project": project.name,
                "reviewed_by": "fixture",
                "reviewed_at": "2026-07-29T00:00:00+00:00",
                "verdict": "pass",
                "notes": "Fixture human review covers every sampled frame.",
                "final_video_sha256": receipt["inputs"]["final_video"]["sha256"],
                "contact_sheet_sha256": receipt["outputs"]["contact_sheet"]["sha256"],
                "sample_sha256": hashlib.sha256(sample.read_bytes()).hexdigest(),
                "render_self_eval": entry(self_eval_path),
            }
        ),
        encoding="utf-8",
    )

    record_final_quality_fixture(project)


def record_final_quality_fixture(project):
    # Test-only machine producer. Production status never mints this record;
    # final-quality-review does so only after its real scans complete.
    def entry(name):
        path = project / name
        return {"path": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}

    if all((project / name).is_file() for name in final_quality_authority.OUTPUT_PATHS.values()):
        final_quality_authority._record(project,
            {key: entry(name) for key, name in final_quality_authority.INPUT_PATHS.items()},
            {key: entry(name) for key, name in final_quality_authority.OUTPUT_PATHS.items()})


def bind_render_revision(project):
    revision = render_contract.render_input_revision(project)
    for marker in (project / "output").glob("*.mp4.render-result"):
        value = json.loads(marker.read_text(encoding="utf-8"))
        value["render_input_revision"] = revision
        marker.write_text(json.dumps(value), encoding="utf-8")


def make_ready_project(root, slug="demo", self_eval_status="pass", self_eval_attempt=1):
    # macOS exposes TemporaryDirectory below `/var`, a symlink to
    # `/private/var`. Production callers use canonical project paths; keep the
    # test's protected ledger namespace identical to downstream validators.
    root = Path(root).resolve()
    project = root / "projects" / slug
    (project / "output").mkdir(parents=True)
    review_dir = project / "quality-review" / "final-v1"
    review_dir.mkdir(parents=True)
    (project / "project-contract.json").write_text(
        json.dumps(
            {
                "schema": "haru.project_contract.v1",
                "lane_contract": "manual.v1",
                "selection": {"chosen_by": "fixture", "chosen_at": 1},
                "publish_target": {
                    "youtube_channel_id": "UCaaaaaaaaaaaaaaaaaaaaaa"
                },
                "runtime_contract": {
                    "schema": "haru.project_runtime_contract.v1",
                    "runtime": "haru.runtime.v1",
                    "evaluator": "haru.evaluator.v1",
                    "artifact": "haru.artifact.v1",
                },
            }
        ),
        encoding="utf-8",
    )
    (project / "script-proposal.md").write_text("proposal", encoding="utf-8")
    (project / "sources.md").write_text("sources", encoding="utf-8")
    (project / "claims.json").write_text(
        json.dumps(
            {
                "claims": [
                    {
                        "id": "C001",
                        "source_type": "official",
                        "source_name": "Official source",
                        "source_url": "https://example.test/source",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    audio = project / "narration-final.mp3"
    audio.write_bytes(b"audio")
    audio.with_suffix(".srt").write_text(
        """1
00:00:00,000 --> 00:00:10,800
cold open

2
00:00:10,800 --> 00:00:30,000
evidence

3
00:00:30,000 --> 00:00:42,000
presenter and evidence

4
00:00:42,000 --> 00:01:00,000
callback tail
""",
        encoding="utf-8",
    )
    Path(str(audio) + ".pron-ok.json").write_text(
        json.dumps({"sha256": hashlib.sha256(b"audio").hexdigest(), "warnings": []}),
        encoding="utf-8",
    )
    (project / "storyboard-final-timed-validation.json").write_text(
        json.dumps(
            {
                "schema": "haru.storyboard_validation.v1",
                "ok": True,
                "checks": [{"status": "pass"}],
            }
        ),
        encoding="utf-8",
    )
    (project / "storyboard-final-timed.json").write_text(
        json.dumps(
            {
                "schema": "haru.storyboard_timed.v1",
                "project": slug,
                "srt": "narration-final.srt",
                "visual_timeline_contract": "cue_driven.v1",
                "audio_duration_seconds": 60,
                "scenes": [
                    {
                        "scene_id": "s01",
                        "section": "cold-open",
                        "start_seconds": 0,
                        "end_seconds": 30,
                        "card_type": "ConceptCard",
                        "card": ["cold"],
                        "visual_events": [
                            {"event_id": "host-open", "start_seconds": 0, "end_seconds": 10.8, "cue": "host", "visual_state": "host", "presenter_state": "talking"},
                            {"event_id": "evidence", "start_seconds": 10.8, "end_seconds": 30, "cue": "evidence", "visual_state": "evidence", "presenter_state": "hidden"},
                        ],
                    },
                    {
                        "scene_id": "s02",
                        "section": "leopard-tail",
                        "start_seconds": 30,
                        "end_seconds": 60,
                        "motif": True,
                        "visual_events": [
                            {"event_id": "evidence-pip", "start_seconds": 30, "end_seconds": 42, "cue": "pip", "visual_state": "pip", "presenter_state": "listening"},
                            {"event_id": "system-motion", "start_seconds": 42, "end_seconds": 60, "cue": "motion", "visual_state": "motion", "presenter_state": "hidden"},
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    # HVP-33 defines the self-eval gate on exactly output/final.mp4, so the
    # fixture's canonical final lives there. A revisioned name would leave the
    # gate evaluating a file the rest of the project never uses.
    video = project / "output" / "final.mp4"
    video.write_bytes(b"video")
    video_sha = hashlib.sha256(b"video").hexdigest()
    Path(str(video) + ".render-result").write_text(
        json.dumps(
            {
                "schema": "haru.render_result.v1",
                "status": "pass",
                "project": slug,
                "output": str(video.relative_to(project)),
                "video_sha256": video_sha,
                "bytes": video.stat().st_size,
                "duration_seconds": 60,
                "loudness_lufs": -14.2,
                "true_peak_dbfs": -1.0,
                "loudness_range_lu": 5.3,
                "mix": {
                    "schema": "haru.final_mix.v1",
                    "method": "ffmpeg_loudnorm_two_pass",
                    "normalization_type": "linear",
                    "input_sha256": "1" * 64,
                    "target": {
                        "integrated_lufs": -14.0,
                        "true_peak_dbfs": -1.0,
                        "loudness_range_lu": 5.3,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    (review_dir / "prep.json").write_text(
        json.dumps(
            {
                "video_sha256": video_sha,
                "metadata": {"duration": 60},
                "audio": {"has_audio": True, "clipping": False, "too_quiet": False},
            }
        ),
        encoding="utf-8",
    )
    (review_dir / "review.json").write_text(
        json.dumps(
            {
                "video": str(video.relative_to(project)),
                "video_sha256": video_sha,
                "publish_readiness": "ship",
                "critical_issues": [],
                "warnings": [],
                "checks": [
                    {"name": name, "status": "pass"}
                    for name in agent_status.LONGFORM_QA_CHECKS
                ],
            }
        ),
        encoding="utf-8",
    )
    write_png(project / "output" / "cover.png")
    (project / "publish-metadata.json").write_text(
        json.dumps(
            {
                "schema": "haru.publish_metadata.v1",
                "project": slug,
                "title": "Fixture title",
                "description": "Fixture description.",
                "thumbnail_text": "Fixture thumbnail",
                "hashtags": ["#Fixture"],
                "source_statement": "Fixture source statement.",
                "made_for_kids": False,
                "category_id": "22",
            }
        ),
        encoding="utf-8",
    )
    target = {
        "youtube_channel_id": "UCaaaaaaaaaaaaaaaaaaaaaa",
        "visibility": agent_status.PUBLISH_VISIBILITY,
    }
    (project / "youtube-publish-pack.md").write_text(
        "publish\n"
        f"<!-- haru.publish_target_sha256: {agent_status.publish_target_sha256(target)} -->\n",
        encoding="utf-8",
    )
    (project / "issue_brief.md").write_text("- Duration target: 55-70 seconds\n", encoding="utf-8")
    (project / "artifact_manifest.json").write_text(
        json.dumps(
            {
                "schema": "haru.artifact_manifest.v1",
                "project": slug,
                "canonical": {
                    "final_video": {
                        "path": str(video),
                        "sha256": video_sha,
                        "bytes": video.stat().st_size,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    bind_render_revision(project)
    seal_self_eval(
        project, root, status=self_eval_status, attempt=self_eval_attempt
    )
    write_visual_qa_receipts(project, video)
    return project


def write_mina_editorial_fixture(project):
    project_contract_path = project / "project-contract.json"
    project_contract = json.loads(project_contract_path.read_text(encoding="utf-8"))
    project_contract["lane_contract"] = "social_issue_longform.v1"
    project_contract["production_profile"] = "host_longform.v1"
    project_contract_path.write_text(
        json.dumps(project_contract), encoding="utf-8"
    )
    assets = project / "assets"
    assets.mkdir(exist_ok=True)
    paths = {}
    for name in ("mina.mp4", "broll.mp4", "motion.mp4"):
        path = assets / name
        path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + name.encode())
        paths[name] = path
    motion_source = assets / "motion-source.tsx"
    motion_source.write_text("export default motionScene;\n", encoding="utf-8")
    motion_receipt = assets / "motion.mp4.motion-canvas.json"
    motion_receipt.write_text(
        json.dumps(
            {
                "schema": "haru.motion_canvas_render.v1",
                "engine": "motion_canvas",
                "engine_version": "fixture",
                "design_id": "system-path",
                "semantic_purpose": "Explain the system path.",
                "cue_ids": ["system-motion"],
                "output": "assets/motion.mp4",
                "output_sha256": hashlib.sha256(paths["motion.mp4"].read_bytes()).hexdigest(),
                "source": "assets/motion-source.tsx",
                "source_sha256": hashlib.sha256(motion_source.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    broll_receipt = assets / "broll.mp4.source.json"
    broll_receipt.write_text(
        json.dumps(
            {
                "schema": "haru.broll_source.v1",
                "output": "assets/broll.mp4",
                "output_sha256": hashlib.sha256(paths["broll.mp4"].read_bytes()).hexdigest(),
                "source_kind": "stock",
                "source_url": "https://videos.example/security-operations",
                "provider": "Example Stock",
                "search_query": "cyber security operations footage",
                "acquired_at": "2026-08-04T01:00:00Z",
                "license_or_usage_basis": "fixture license",
                "semantic_purpose": "Show the system context named by the cue.",
                "cue_ids": ["evidence", "evidence-pip"],
                "brand_characters": [],
            }
        ),
        encoding="utf-8",
    )
    storyboard = project / "storyboard-final-timed.json"
    contract = {
        "schema": "haru.editorial_contract.v1",
        "project": project.name,
        "production_profile": "host_longform.v1",
        "storyboard_sha256": hashlib.sha256(storyboard.read_bytes()).hexdigest(),
        "shots": [
            {"event_id": "host-open", "start_seconds": 0, "end_seconds": 10.8, "composition": "aroll_full", "asset_role": "host_aroll", "presenter_id": "host", "asset_path": "assets/mina.mp4", "asset_sha256": hashlib.sha256(paths["mina.mp4"].read_bytes()).hexdigest()},
            {"event_id": "evidence", "start_seconds": 10.8, "end_seconds": 30, "composition": "broll_full", "asset_role": "broll", "asset_path": "assets/broll.mp4", "asset_sha256": hashlib.sha256(paths["broll.mp4"].read_bytes()).hexdigest(), "source_receipt_path": "assets/broll.mp4.source.json", "source_receipt_sha256": hashlib.sha256(broll_receipt.read_bytes()).hexdigest()},
            {"event_id": "evidence-pip", "start_seconds": 30, "end_seconds": 42, "composition": "broll_pip", "asset_role": "broll", "asset_path": "assets/broll.mp4", "asset_sha256": hashlib.sha256(paths["broll.mp4"].read_bytes()).hexdigest(), "source_receipt_path": "assets/broll.mp4.source.json", "source_receipt_sha256": hashlib.sha256(broll_receipt.read_bytes()).hexdigest(), "presenter_id": "host", "presenter_asset_path": "assets/mina.mp4", "presenter_asset_sha256": hashlib.sha256(paths["mina.mp4"].read_bytes()).hexdigest()},
            {"event_id": "system-motion", "start_seconds": 42, "end_seconds": 60, "composition": "motion_graphics", "asset_role": "motion_canvas", "asset_path": "assets/motion.mp4", "asset_sha256": hashlib.sha256(paths["motion.mp4"].read_bytes()).hexdigest(), "engine": "motion_canvas", "producer_receipt_path": "assets/motion.mp4.motion-canvas.json", "producer_receipt_sha256": hashlib.sha256(motion_receipt.read_bytes()).hexdigest()},
        ],
    }
    contract_path = project / "editorial-contract.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    preview_dir = project / "quality-review/editorial-preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview = preview_dir / "preview.mp4"
    preview.write_bytes(b"\x00\x00\x00\x18ftypmp42preview")
    (preview_dir / "review.json").write_text(
        json.dumps(
            {
                "schema": "haru.editorial_preview_review.v1",
                "project": project.name,
                "preview": "quality-review/editorial-preview/preview.mp4",
                "preview_sha256": hashlib.sha256(preview.read_bytes()).hexdigest(),
                "duration_seconds": 75,
                "editorial_contract_sha256": hashlib.sha256(contract_path.read_bytes()).hexdigest(),
                "verdict": "pass",
                "reviewed_by": "fixture",
            }
        ),
        encoding="utf-8",
    )
    editorial_digest = hashlib.sha256(contract_path.read_bytes()).hexdigest()
    for marker in (project / "output").glob("*.mp4.render-result"):
        value = json.loads(marker.read_text(encoding="utf-8"))
        value["editorial_contract_sha256"] = editorial_digest
        marker.write_text(json.dumps(value), encoding="utf-8")
    bind_render_revision(project)
    # Adding the editorial contract and rebinding the marker changes the self-eval
    # identity, so the attempt sealed by make_ready_project is now history. Seal
    # the new identity before rewriting the v2 receipts that bind it.
    video = project / "output" / "final.mp4"
    if video.is_file():
        seal_self_eval(project, project.parents[1], attempt=2)
        write_visual_qa_receipts(project, video)


class AgentStatusTest(RuntimePolicyCase):
    def test_unissued_or_rewritten_final_qa_cannot_pass_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_ready_project(root)
            initial, _ = agent_status.build(project, root)
            self.assertEqual(initial["stages"]["qa"]["status"], "pass")
            path = project / "quality-review/final-v1/review.json"
            receipt = json.loads(path.read_text())
            receipt["unissued_note"] = "hand-written replacement, still claims every check passed"
            path.write_text(json.dumps(receipt))
            changed, _ = agent_status.build(project, root)
            self.assertNotEqual(changed["stages"]["qa"]["status"], "pass")
            self.assertIn("qa_not_passed", {b["code"] for b in changed["blocker_details"]})


    def test_embedded_audio_delivery_explains_canonical_gap_without_fake_stamp_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            (project / "narration-final.mp3").unlink()
            (project / "narration-final.mp3.pron-ok.json").unlink()
            legacy = project / "narration-final-v3-yu-sectioned-v5.mp3"
            legacy.write_bytes(b"legacy audio")
            legacy.with_suffix(".srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nold take\n")
            Path(str(legacy) + ".pron-ok.json").write_text(json.dumps({
                "sha256": hashlib.sha256(legacy.read_bytes()).hexdigest(), "warnings": []
            }))
            (project / "render_plan.json").write_text(json.dumps({"skip_pronunciation_gate": True}))
            status, _ = agent_status.build(project, root)
            self.assertEqual(status["stages"]["tts"]["status"], "missing")
            self.assertNotIn("pronunciation warnings must be a list", status["warnings"])
            self.assertTrue(any("derive-narration" in action for action in status["next_actions"]))
            self.assertNotEqual(status["overall_status"], "ready_for_human_upload_approval")

    def test_completed_visual_review_names_final_quality_runner_for_missing_legacy_qa(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            for path in project.glob("quality-review/final-v*/review.json"):
                path.unlink()
            status, _ = agent_status.build(project, root)
            self.assertIn("run_next with runner final-quality-review", status["next_actions"])


    def test_social_issue_contract_requires_human_selection_before_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            (project / "project-contract.json").write_text(
                json.dumps(
                    {
                        "schema": "haru.project_contract.v1",
                        "lane_contract": "social_issue_longform.v1",
                    }
                ),
                encoding="utf-8",
            )

            status, _ = agent_status.build(project, root)

            self.assertEqual(status["stages"]["proposal"]["status"], "pass")
            self.assertEqual(status["stages"]["selection"]["status"], "missing")
            self.assertIn(
                "selection_not_passed",
                {item["code"] for item in status["blocker_details"]},
            )

    def test_social_issue_longform_requires_mina_editorial_contract_before_render_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root, "mina-required")
            (project / "project-contract.json").write_text(
                json.dumps(
                    {
                        "schema": "haru.project_contract.v1",
                        "lane_contract": "social_issue_longform.v1",
                    }
                ),
                encoding="utf-8",
            )
            (project / ".hvp").mkdir()
            (project / ".hvp" / "selection.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "cron_run_id": "topic-run",
                        "candidate_id": "candidate",
                        "chosen_by": "harvey",
                        "chosen_at": 1,
                        "project_slug": project.name,
                    }
                ),
                encoding="utf-8",
            )

            status, _ = agent_status.build(project, root)

            self.assertEqual(status["stages"]["editorial"]["status"], "missing")
            self.assertEqual(status["stages"]["render"]["status"], "missing")
            self.assertIn("editorial", status["required_stages"])

    def test_social_issue_rejects_receipt_ids_the_canonical_writer_cannot_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            (project / "project-contract.json").write_text(
                json.dumps(
                    {
                        "schema": "haru.project_contract.v1",
                        "lane_contract": "social_issue_longform.v1",
                    }
                ),
                encoding="utf-8",
            )
            (project / ".hvp").mkdir()
            (project / ".hvp" / "selection.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "cron_run_id": "bad id with spaces",
                        "candidate_id": "candidate/../../escape",
                        "chosen_by": "not-valid!",
                        "chosen_at": 1785254400,
                        "project_slug": "demo",
                    }
                ),
                encoding="utf-8",
            )

            status, _ = agent_status.build(project, root)

            self.assertEqual(status["stages"]["selection"]["status"], "missing")
            self.assertIn(
                "selection_not_passed",
                {item["code"] for item in status["blocker_details"]},
            )

    def test_review_cannot_shorten_the_lane_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            (project / "project-contract.json").write_text(
                json.dumps(
                    {
                        "schema": "haru.project_contract.v1",
                        "lane_contract": "manual.v1",
                        "selection": {"chosen_by": "harvey", "chosen_at": 1785254400},
                    }
                ),
                encoding="utf-8",
            )
            review = project / "quality-review" / "final-v1" / "review.json"
            review_data = json.loads(review.read_text(encoding="utf-8"))
            review_data["required_checks"] = ["visual_spot_check"]
            review_data["checks"] = [{"name": "visual_spot_check", "status": "pass"}]
            review.write_text(json.dumps(review_data), encoding="utf-8")

            status, _ = agent_status.build(project, root)

            self.assertEqual(status["stages"]["qa"]["status"], "warn")
            self.assertIn(
                "qa_not_passed",
                {item["code"] for item in status["blocker_details"]},
            )

    def test_review_cannot_self_report_visual_sampling_without_receipts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            shutil.rmtree(project / visual_qa_sample.OUTPUT_DIR)
            review = project / "quality-review" / "final-v1" / "review.json"
            review_data = json.loads(review.read_text(encoding="utf-8"))
            review_data["checks"].append(
                {"name": "visual_sampling", "status": "pass"}
            )
            review.write_text(json.dumps(review_data), encoding="utf-8")

            status, _ = agent_status.build(project, root)

            self.assertEqual(status["stages"]["qa"]["status"], "warn")
            self.assertIn(
                "visual_sampling",
                status["stages"]["qa"]["notes"][0]["required_checks"],
            )

    def test_visual_review_is_stale_when_contact_sheet_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            sheet = (
                project
                / visual_qa_sample.OUTPUT_DIR
                / visual_qa_sample.SHEET_NAME
            )
            sheet.write_bytes(sheet.read_bytes() + b"changed")

            status, _ = agent_status.build(project, root)

            self.assertEqual(status["stages"]["qa"]["status"], "warn")

    def test_check_exits_zero_only_for_ready_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)

            result = subprocess.run(
                [
                    sys.executable,
                    str(self.installed_tool("agent_status.py")),
                    str(project),
                    "--workspace",
                    str(root),
                    "--check",
                ],
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout)["overall_status"],
                "ready_for_human_upload_approval",
            )

    def test_check_exits_nonzero_for_failing_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "projects" / "empty"
            project.mkdir(parents=True)

            result = subprocess.run(
                [
                    sys.executable,
                    str(self.installed_tool("agent_status.py")),
                    str(project),
                    "--workspace",
                    str(root),
                    "--check",
                ],
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(json.loads(result.stdout)["overall_status"], "in_progress")

    def test_review_video_must_stay_inside_current_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            foreign = root / "projects" / "other" / "output" / "other-final.mp4"
            foreign.parent.mkdir(parents=True)
            foreign.write_bytes(b"video")
            Path(str(foreign) + ".render-result").write_text(
                json.dumps({"status": "pass", "duration_seconds": 60}),
                encoding="utf-8",
            )
            review = project / "quality-review" / "final-v1" / "review.json"
            data = json.loads(review.read_text(encoding="utf-8"))
            data.update(video=str(foreign), video_sha256=hashlib.sha256(b"video").hexdigest())
            review.write_text(json.dumps(data), encoding="utf-8")

            status, _ = agent_status.build(project, root)

            self.assertIn("video_binding_not_passed", {item["code"] for item in status["blocker_details"]})

    def test_outside_review_path_is_rejected_before_reading_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            foreign_directory = root / "projects" / "other"
            foreign_directory.mkdir(parents=True)
            review = project / "quality-review" / "final-v1" / "review.json"
            data = json.loads(review.read_text(encoding="utf-8"))
            data["video"] = str(foreign_directory)
            review.write_text(json.dumps(data), encoding="utf-8")

            status, artifacts = agent_status.build(project, root)

            self.assertIsNone(artifacts["canonical"]["final_video"])
            self.assertIn("video_binding_not_passed", {item["code"] for item in status["blocker_details"]})

    def test_duration_evidence_must_agree_within_half_a_second(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            prep = project / "quality-review" / "final-v1" / "prep.json"
            data = json.loads(prep.read_text(encoding="utf-8"))
            data["metadata"]["duration"] = 61
            prep.write_text(json.dumps(data), encoding="utf-8")

            status, _ = agent_status.build(project, root)

            self.assertNotEqual(status["stages"]["duration"]["status"], "pass")
            self.assertIn("duration_not_passed", {item["code"] for item in status["blocker_details"]})

    def test_malformed_prep_is_a_blocker_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            prep = project / "quality-review" / "final-v1" / "prep.json"
            prep.write_text(json.dumps({"metadata": None, "audio": None}), encoding="utf-8")

            status, _ = agent_status.build(project, root)

            codes = {item["code"] for item in status["blocker_details"]}
            self.assertIn("duration_not_passed", codes)
            self.assertIn("loudness_not_passed", codes)

    def test_duration_must_fit_issue_brief_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            (project / "issue_brief.md").write_text(
                "- Length: 61-70 seconds\n",
                encoding="utf-8",
            )

            status, _ = agent_status.build(project, root)

            self.assertNotEqual(status["stages"]["duration"]["status"], "pass")

    def test_duration_requires_a_parseable_issue_brief_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            (project / "issue_brief.md").unlink()

            status, _ = agent_status.build(project, root)

            self.assertNotEqual(status["stages"]["duration"]["status"], "pass")

    def test_boolean_duration_is_not_numeric_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            (project / "issue_brief.md").write_text("- Length: 0-2 seconds\n", encoding="utf-8")
            render = project / "output" / "final.mp4.render-result"
            render_data = json.loads(render.read_text(encoding="utf-8"))
            render_data["duration_seconds"] = True
            render.write_text(json.dumps(render_data), encoding="utf-8")
            prep = project / "quality-review" / "final-v1" / "prep.json"
            prep_data = json.loads(prep.read_text(encoding="utf-8"))
            prep_data["metadata"]["duration"] = True
            prep.write_text(json.dumps(prep_data), encoding="utf-8")

            status, _ = agent_status.build(project, root)

            self.assertNotEqual(status["stages"]["duration"]["status"], "pass")

    def test_listed_storyboard_and_review_checks_must_all_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            storyboard = project / "storyboard-final-timed-validation.json"
            storyboard.write_text(
                json.dumps({"ok": True, "checks": [{"status": "fail"}]}),
                encoding="utf-8",
            )
            review = project / "quality-review" / "final-v1" / "review.json"
            review_data = json.loads(review.read_text(encoding="utf-8"))
            review_data["checks"] = [{"status": "warn"}]
            review.write_text(json.dumps(review_data), encoding="utf-8")

            status, _ = agent_status.build(project, root)

            self.assertNotEqual(status["stages"]["storyboard"]["status"], "pass")
            self.assertEqual(status["stages"]["qa"]["status"], "warn")
            self.assertIn("storyboard_not_passed", {item["code"] for item in status["blocker_details"]})
            self.assertIn("qa_not_passed", {item["code"] for item in status["blocker_details"]})

    def test_storyboard_ok_must_be_boolean_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            storyboard = project / "storyboard-final-timed-validation.json"
            storyboard.write_text(
                json.dumps({"ok": "false", "checks": [{"status": "pass"}]}),
                encoding="utf-8",
            )

            status, _ = agent_status.build(project, root)

            self.assertNotEqual(status["stages"]["storyboard"]["status"], "pass")

    def test_review_requires_lane_specific_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            review = project / "quality-review" / "final-v1" / "review.json"
            review_data = json.loads(review.read_text(encoding="utf-8"))
            review_data["checks"] = [{"name": "unrelated_smoke_test", "status": "pass"}]
            review.write_text(json.dumps(review_data), encoding="utf-8")

            status, _ = agent_status.build(project, root)

            self.assertEqual(status["stages"]["qa"]["status"], "warn")
            self.assertIn("qa_not_passed", {item["code"] for item in status["blocker_details"]})

    def test_review_requires_an_explicit_lane_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            (project / "project-contract.json").unlink()

            status, _ = agent_status.build(project, root)

            self.assertEqual(status["stages"]["qa"]["status"], "warn")
            self.assertEqual(status["stages"]["selection"]["status"], "missing")

    def test_review_check_boolean_fields_must_be_boolean_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            review = project / "quality-review" / "final-v1" / "review.json"
            review_data = json.loads(review.read_text(encoding="utf-8"))
            review_data["checks"][0]["ok"] = "false"
            review.write_text(json.dumps(review_data), encoding="utf-8")

            status, _ = agent_status.build(project, root)

            self.assertIn("qa_not_passed", {item["code"] for item in status["blocker_details"]})

    def test_optional_review_check_does_not_become_a_required_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            review = project / "quality-review" / "final-v1" / "review.json"
            review_data = json.loads(review.read_text(encoding="utf-8"))
            review_data["checks"].append({"name": "optional_inventory", "status": "warn"})
            review.write_text(json.dumps(review_data), encoding="utf-8")

            # The fixture producer may report an optional warning; it still
            # authenticates these exact changed bytes as its issued evidence.
            record_final_quality_fixture(project)
            status, _ = agent_status.build(project, root)

            self.assertEqual(status["stages"]["qa"]["status"], "pass")

    def test_review_requires_explicit_empty_issue_lists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            review = project / "quality-review" / "final-v1" / "review.json"
            review_data = json.loads(review.read_text(encoding="utf-8"))
            review_data.pop("critical_issues")
            review_data.pop("warnings")
            review.write_text(json.dumps(review_data), encoding="utf-8")

            status, _ = agent_status.build(project, root)

            self.assertEqual(status["stages"]["qa"]["status"], "warn")

    def test_explicit_check_failure_overrides_ok_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            review = project / "quality-review" / "final-v1" / "review.json"
            review_data = json.loads(review.read_text(encoding="utf-8"))
            review_data["checks"][0].update(status="fail", ok=True)
            review.write_text(json.dumps(review_data), encoding="utf-8")

            status, _ = agent_status.build(project, root)

            self.assertEqual(status["stages"]["qa"]["status"], "warn")

    def test_review_sha_is_checked_against_current_video_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            video = project / "output" / "final.mp4"
            video.write_bytes(b"mutated")

            status, artifacts = agent_status.build(project, root)

            self.assertEqual(status["overall_status"], "in_progress")
            self.assertIn("video_binding_not_passed", {item["code"] for item in status["blocker_details"]})
            self.assertEqual(
                artifacts["canonical"]["final_video"]["sha256"],
                hashlib.sha256(b"mutated").hexdigest(),
            )

    def test_video_replacement_during_hashing_blocks_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            video = project / "output" / "final.mp4"
            original_sha256 = agent_status.sha256
            replaced = False

            def replacing_sha256(path):
                nonlocal replaced
                digest = original_sha256(path)
                if path == video and not replaced:
                    replacement = video.with_suffix(".replacement")
                    replacement.write_bytes(b"other")
                    os.replace(replacement, video)
                    replaced = True
                return digest

            agent_status.sha256 = replacing_sha256
            try:
                status, _ = agent_status.build(project, root)
            finally:
                agent_status.sha256 = original_sha256

            self.assertIn("video_binding_not_passed", {item["code"] for item in status["blocker_details"]})

    def test_video_replacement_after_initial_stable_hash_blocks_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            video = project / "output" / "final.mp4"
            original_stable_sha256 = agent_status.stable_sha256
            replaced = False

            def replacing_stable_sha256(path):
                nonlocal replaced
                digest = original_stable_sha256(path)
                if path == video and not replaced:
                    replacement = video.with_suffix(".replacement")
                    replacement.write_bytes(b"other")
                    os.replace(replacement, video)
                    replaced = True
                return digest

            agent_status.stable_sha256 = replacing_stable_sha256
            try:
                status, _ = agent_status.build(project, root)
            finally:
                agent_status.stable_sha256 = original_stable_sha256

            self.assertIn("video_binding_not_passed", {item["code"] for item in status["blocker_details"]})

    def test_render_evidence_sha_must_match_current_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            video = project / "output" / "final.mp4"
            video.write_bytes(b"rerendered")
            current_sha = hashlib.sha256(b"rerendered").hexdigest()
            for path in [
                project / "quality-review" / "final-v1" / "review.json",
                project / "quality-review" / "final-v1" / "prep.json",
            ]:
                data = json.loads(path.read_text(encoding="utf-8"))
                data["video_sha256"] = current_sha
                path.write_text(json.dumps(data), encoding="utf-8")
            manifest = project / "artifact_manifest.json"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["canonical"]["final_video"]["sha256"] = current_sha
            data["canonical"]["final_video"]["bytes"] = video.stat().st_size
            manifest.write_text(json.dumps(data), encoding="utf-8")

            status, _ = agent_status.build(project, root)

            self.assertIn("render_not_passed", {item["code"] for item in status["blocker_details"]})

    def test_unreadable_render_evidence_emits_blocker_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            render = project / "output" / "final.mp4.render-result"
            original_mode = stat.S_IMODE(render.stat().st_mode)
            render.chmod(0)
            try:
                status, _ = agent_status.build(project, root)
            finally:
                render.chmod(original_mode)

            self.assertIn("render_not_passed", {item["code"] for item in status["blocker_details"]})

    def test_wrong_review_sha_blocks_unchanged_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            review = project / "quality-review" / "final-v1" / "review.json"
            data = json.loads(review.read_text(encoding="utf-8"))
            data["video_sha256"] = "0" * 64
            review.write_text(json.dumps(data), encoding="utf-8")

            status, _ = agent_status.build(project, root)

            self.assertIn("video_binding_not_passed", {item["code"] for item in status["blocker_details"]})

    def test_existing_manifest_must_match_selected_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            project = make_ready_project(root)
            video = project / "output" / "final.mp4"
            (project / "artifact_manifest.json").write_text(
                json.dumps(
                    {
                        "schema": "haru.artifact_manifest.v1",
                        "project": "demo",
                        "canonical": {
                            "final_video": {
                                "path": str(video.relative_to(root)),
                                "sha256": "0" * 64,
                                "bytes": video.stat().st_size,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            status, _ = agent_status.build(project, root)

            self.assertEqual(status["overall_status"], "in_progress")
            self.assertIn("manifest_binding_not_passed", {item["code"] for item in status["blocker_details"]})

    def test_manifest_is_required_and_bound_to_current_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            manifest = project / "artifact_manifest.json"
            manifest.unlink()

            missing, _ = agent_status.build(project, root)
            self.assertIn("manifest_binding_not_passed", {item["code"] for item in missing["blocker_details"]})

            project = make_ready_project(root, "other-demo")
            manifest = project / "artifact_manifest.json"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["project"] = "different-project"
            manifest.write_text(json.dumps(data), encoding="utf-8")

            wrong_project, _ = agent_status.build(project, root)
            self.assertIn(
                "manifest_binding_not_passed",
                {item["code"] for item in wrong_project["blocker_details"]},
            )

    def test_manifest_byte_count_must_match_current_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            manifest = project / "artifact_manifest.json"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["canonical"]["final_video"]["bytes"] += 1
            manifest.write_text(json.dumps(data), encoding="utf-8")

            status, _ = agent_status.build(project, root)

            self.assertIn("manifest_binding_not_passed", {item["code"] for item in status["blocker_details"]})

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_manifest_path_cannot_use_an_outside_symlink_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            video = project / "output" / "final.mp4"
            alias = root / "outside-alias.mp4"
            os.symlink(video, alias)
            manifest = project / "artifact_manifest.json"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["canonical"]["final_video"]["path"] = str(alias)
            manifest.write_text(json.dumps(data), encoding="utf-8")

            status, _ = agent_status.build(project, root)

            self.assertIn("manifest_binding_not_passed", {item["code"] for item in status["blocker_details"]})

            internal_alias = project / "output" / "internal-alias.mp4"
            os.symlink(video, internal_alias)
            data["canonical"]["final_video"]["path"] = str(internal_alias)
            manifest.write_text(json.dumps(data), encoding="utf-8")

            internal_status, _ = agent_status.build(project, root)
            self.assertIn(
                "manifest_binding_not_passed",
                {item["code"] for item in internal_status["blocker_details"]},
            )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_malformed_manifest_paths_emit_blockers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            manifest = project / "artifact_manifest.json"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            loop = project / "output" / "loop.mp4"
            os.symlink(loop, loop)
            data["canonical"]["final_video"]["path"] = str(loop)
            manifest.write_text(json.dumps(data), encoding="utf-8")

            loop_status, _ = agent_status.build(project, root)
            self.assertIn(
                "manifest_binding_not_passed",
                {item["code"] for item in loop_status["blocker_details"]},
            )

            data["canonical"]["final_video"]["path"] = "output/bad\u0000path.mp4"
            manifest.write_text(json.dumps(data), encoding="utf-8")
            nul_status, _ = agent_status.build(project, root)
            self.assertIn(
                "manifest_binding_not_passed",
                {item["code"] for item in nul_status["blocker_details"]},
            )

    def test_invalid_existing_manifest_blocks_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            (project / "artifact_manifest.json").write_text("{broken", encoding="utf-8")

            status, _ = agent_status.build(project, root)

            self.assertIn("manifest_binding_not_passed", {item["code"] for item in status["blocker_details"]})

    def test_non_utf8_manifest_emits_a_blocker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            (project / "artifact_manifest.json").write_bytes(b"\xff")

            status, _ = agent_status.build(project, root)

            self.assertIn("manifest_binding_not_passed", {item["code"] for item in status["blocker_details"]})

    def test_newer_final_render_requires_a_new_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            review = project / "quality-review" / "final-v1" / "review.json"
            reviewed = project / "output" / "final.mp4"
            os.utime(reviewed, (100, 100))
            newer = project / "output" / "demo-final-v2.mp4"
            newer.write_bytes(b"new video")
            os.utime(newer, (101, 101))
            os.utime(review, (102, 102))

            status, _ = agent_status.build(project, root)

            self.assertEqual(status["overall_status"], "in_progress")
            self.assertIn("review_freshness_not_passed", {item["code"] for item in status["blocker_details"]})

    def test_newest_malformed_review_invalidates_older_valid_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            newer_review = project / "quality-review" / "final-v2" / "review.json"
            newer_review.parent.mkdir()
            newer_review.write_text("{broken", encoding="utf-8")
            os.utime(newer_review, (100, 100))
            os.utime(project / "quality-review" / "final-v1" / "review.json", (200, 200))

            status, _ = agent_status.build(project, root)

            self.assertEqual(status["overall_status"], "in_progress")
            self.assertIn("qa_not_passed", {item["code"] for item in status["blocker_details"]})

    def test_empty_higher_revision_review_invalidates_older_valid_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            newer_review = project / "quality-review" / "final-v2" / "review.json"
            newer_review.parent.mkdir()
            newer_review.touch()

            status, _ = agent_status.build(project, root)

            self.assertIn("qa_not_passed", {item["code"] for item in status["blocker_details"]})

    def test_newer_unversioned_review_invalidates_older_valid_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            newer_review = project / "quality-review" / "final-latest" / "review.json"
            newer_review.parent.mkdir()
            newer_review.write_text("{broken", encoding="utf-8")
            timestamp = 100
            os.utime(newer_review, (timestamp, timestamp))
            os.utime(project / "quality-review" / "final-v1" / "review.json", (timestamp, timestamp))

            status, _ = agent_status.build(project, root)

            self.assertIn("qa_not_passed", {item["code"] for item in status["blocker_details"]})

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_higher_revision_review_symlink_invalidates_older_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            newer_review = project / "quality-review" / "final-v2" / "review.json"
            newer_review.parent.mkdir()
            os.symlink(project / "quality-review" / "final-v1" / "review.json", newer_review)

            status, _ = agent_status.build(project, root)

            self.assertIn("qa_not_passed", {item["code"] for item in status["blocker_details"]})

    def test_higher_final_revision_invalidates_review_even_with_older_mtime(self):
        """Revision beats mtime — provable only on a legacy revisioned project.

        HVP-33 pins the canonical final to `output/final.mp4`, which carries no
        revision, so the revision comparison can only ever fire between legacy
        revisioned candidates. Removing the canonical final is what keeps this
        non-vacuous: leaving it in place lets its own mtime trip the freshness
        gate, and the revision rule this test names would never be exercised.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            canonical = project / "output" / "final.mp4"
            canonical_marker = Path(str(canonical) + ".render-result")
            reviewed = project / "output" / "demo-final-v1.mp4"
            reviewed.write_bytes(canonical.read_bytes())
            marker = json.loads(canonical_marker.read_text(encoding="utf-8"))
            marker["output"] = "output/demo-final-v1.mp4"
            Path(str(reviewed) + ".render-result").write_text(
                json.dumps(marker), encoding="utf-8"
            )
            review = project / "quality-review" / "final-v1" / "review.json"
            review_data = json.loads(review.read_text(encoding="utf-8"))
            review_data["video"] = "output/demo-final-v1.mp4"
            review.write_text(json.dumps(review_data), encoding="utf-8")
            canonical.unlink()
            canonical_marker.unlink()
            newer = project / "output" / "demo-final-v2.mp4"
            newer.write_bytes(b"new video")
            os.utime(reviewed, (200, 200))
            os.utime(newer, (100, 100))

            status, _ = agent_status.build(project, root)

            self.assertIn("review_freshness_not_passed", {item["code"] for item in status["blocker_details"]})

    def test_empty_higher_final_revision_invalidates_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            (project / "output" / "demo-final-v2.mp4").touch()

            status, _ = agent_status.build(project, root)

            self.assertIn("review_freshness_not_passed", {item["code"] for item in status["blocker_details"]})

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_higher_final_revision_symlink_invalidates_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            os.symlink(
                project / "output" / "final.mp4",
                project / "output" / "demo-final-v2.mp4",
            )

            status, _ = agent_status.build(project, root)

            self.assertIn("review_freshness_not_passed", {item["code"] for item in status["blocker_details"]})

    def test_pronunciation_warning_blocks_tts_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            stamp = project / "narration-final.mp3.pron-ok.json"
            stamp.write_text(json.dumps({"status": "WARN"}), encoding="utf-8")

            status, _ = agent_status.build(project, root)

            self.assertEqual(status["stages"]["tts"]["status"], "warn")
            self.assertIn("tts_not_passed", {item["code"] for item in status["blocker_details"]})

    def test_pending_pronunciation_stamp_blocks_tts_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            stamp = project / "narration-final.mp3.pron-ok.json"
            data = json.loads(stamp.read_text(encoding="utf-8"))
            data["status"] = "pending"
            stamp.write_text(json.dumps(data), encoding="utf-8")

            status, _ = agent_status.build(project, root)

            self.assertNotEqual(status["stages"]["tts"]["status"], "pass")

    def test_pronunciation_warnings_must_be_a_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            stamp = project / "narration-final.mp3.pron-ok.json"
            data = json.loads(stamp.read_text(encoding="utf-8"))
            data["warnings"] = {}
            stamp.write_text(json.dumps(data), encoding="utf-8")

            status, _ = agent_status.build(project, root)

            self.assertNotEqual(status["stages"]["tts"]["status"], "pass")

    def test_pronunciation_stamp_sha_must_match_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            stamp = project / "narration-final.mp3.pron-ok.json"
            stamp.write_text(json.dumps({"sha256": "0" * 64}), encoding="utf-8")

            status, _ = agent_status.build(project, root)

            self.assertNotEqual(status["stages"]["tts"]["status"], "pass")

    def test_g2p_plan_requires_a_current_human_probe_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            (project / "narration.txt").write_text("銀行行動", encoding="utf-8")
            (project / "pronunciation-plan.json").write_text(
                json.dumps(
                    {
                        "schema": "haru.pronunciation_plan.v1",
                        "source": {
                            "sha256": hashlib.sha256("銀行行動".encode()).hexdigest()
                        },
                    }
                ),
                encoding="utf-8",
            )

            status, _ = agent_status.build(project, root)

            self.assertEqual(status["stages"]["tts"]["status"], "warn")
            self.assertIn(
                "pronunciation_review_stale",
                status["stages"]["tts"]["warnings"],
            )

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_required_artifact_symlink_cannot_escape_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            audio = project / "narration-final.mp3"
            outside = root / "outside.mp3"
            outside.write_bytes(audio.read_bytes())
            audio.unlink()
            os.symlink(outside, audio)

            status, _ = agent_status.build(project, root)

            self.assertNotEqual(status["stages"]["tts"]["status"], "pass")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_required_artifact_must_not_be_an_internal_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            audio = project / "narration-final.mp3"
            target = project / "audio-bytes.mp3"
            audio.rename(target)
            os.symlink(target, audio)

            status, _ = agent_status.build(project, root)

            self.assertNotEqual(status["stages"]["tts"]["status"], "pass")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_required_artifact_cannot_hide_behind_a_symlinked_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            alias = project / "alias-output"
            os.symlink(project / "output", alias)
            aliased_video = alias / "final.mp4"
            review = project / "quality-review" / "final-v1" / "review.json"
            review_data = json.loads(review.read_text(encoding="utf-8"))
            review_data["video"] = str(aliased_video)
            review.write_text(json.dumps(review_data), encoding="utf-8")
            manifest = project / "artifact_manifest.json"
            manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
            manifest_data["canonical"]["final_video"]["path"] = str(aliased_video)
            manifest.write_text(json.dumps(manifest_data), encoding="utf-8")

            status, _ = agent_status.build(project, root)

            self.assertIn("video_binding_not_passed", {item["code"] for item in status["blocker_details"]})

    def test_invalid_pronunciation_stamp_blocks_tts_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            stamp = project / "narration-final.mp3.pron-ok.json"
            stamp.write_text("{broken", encoding="utf-8")

            status, _ = agent_status.build(project, root)

            self.assertNotEqual(status["stages"]["tts"]["status"], "pass")

    def test_cover_must_be_1280_by_720_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            write_png(project / "output" / "cover.png", 640, 360)

            status, _ = agent_status.build(project, root)

            self.assertEqual(status["stages"]["cover"]["status"], "warn")
            self.assertIn("cover_not_passed", {item["code"] for item in status["blocker_details"]})

    def test_truncated_png_header_is_not_a_valid_cover(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            cover = project / "output" / "cover.png"
            cover.write_bytes(cover.read_bytes()[:24])

            status, _ = agent_status.build(project, root)

            self.assertNotEqual(status["stages"]["cover"]["status"], "pass")

    def test_png_without_image_data_is_not_a_valid_cover(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            cover = project / "output" / "cover.png"
            data = cover.read_bytes()
            cover.write_bytes(data[:33] + data[-12:])

            status, _ = agent_status.build(project, root)

            self.assertNotEqual(status["stages"]["cover"]["status"], "pass")

    def test_png_with_corrupt_image_data_is_not_a_valid_cover(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            cover = project / "output" / "cover.png"
            data = cover.read_bytes()
            corrupt = b"not-zlib"
            idat = (
                struct.pack(">I", len(corrupt))
                + b"IDAT"
                + corrupt
                + struct.pack(">I", zlib.crc32(b"IDAT" + corrupt))
            )
            cover.write_bytes(data[:33] + idat + data[-12:])

            status, _ = agent_status.build(project, root)

            self.assertNotEqual(status["stages"]["cover"]["status"], "pass")

    def test_loudness_prep_must_bind_current_video_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            prep = project / "quality-review" / "final-v1" / "prep.json"
            data = json.loads(prep.read_text(encoding="utf-8"))
            data["video_sha256"] = "0" * 64
            prep.write_text(json.dumps(data), encoding="utf-8")

            status, _ = agent_status.build(project, root)

            self.assertNotEqual(status["stages"]["loudness"]["status"], "pass")
            self.assertIn("loudness_not_passed", {item["code"] for item in status["blocker_details"]})

    def test_render_requires_digest_bound_two_pass_final_mix_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            marker = project / "output" / "final.mp4.render-result"
            original = json.loads(marker.read_text(encoding="utf-8"))

            for missing in ("mix", "true_peak_dbfs", "loudness_range_lu"):
                data = dict(original)
                data.pop(missing)
                marker.write_text(json.dumps(data), encoding="utf-8")
                status, _ = agent_status.build(project, root)
                self.assertNotEqual(status["stages"]["render"]["status"], "pass", missing)

            flattened = json.loads(json.dumps(original))
            flattened["loudness_range_lu"] = 0
            flattened["mix"]["target"]["loudness_range_lu"] = 20
            marker.write_text(json.dumps(flattened), encoding="utf-8")
            status, _ = agent_status.build(project, root)
            self.assertNotEqual(status["stages"]["render"]["status"], "pass", "flattened LRA")

    def test_loudness_requires_a_measurement_near_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            result = project / "output" / "final.mp4.render-result"
            data = json.loads(result.read_text(encoding="utf-8"))
            data.pop("loudness_lufs")
            result.write_text(json.dumps(data), encoding="utf-8")

            missing, _ = agent_status.build(project, root)
            self.assertNotEqual(missing["stages"]["loudness"]["status"], "pass")

            data["loudness_lufs"] = -8
            result.write_text(json.dumps(data), encoding="utf-8")
            out_of_range, _ = agent_status.build(project, root)
            self.assertNotEqual(out_of_range["stages"]["loudness"]["status"], "pass")

    def test_missing_required_artifacts_emit_machine_readable_blockers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "projects" / "empty"
            project.mkdir(parents=True)

            status, _ = agent_status.build(project, root)

            self.assertEqual(status["overall_status"], "in_progress")
            self.assertIn("proposal_not_passed", {item["code"] for item in status["blocker_details"]})
            self.assertIn("render_not_passed", {item["code"] for item in status["blocker_details"]})
            self.assertTrue(status["blockers"])

    def test_claims_require_nonempty_source_evidence_and_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            (project / "sources.md").write_text("", encoding="utf-8")

            empty_sources, _ = agent_status.build(project, root)
            self.assertIn("sources_not_passed", {item["code"] for item in empty_sources["blocker_details"]})

            (project / "sources.md").write_text("sources", encoding="utf-8")
            claims = project / "claims.json"
            claims.write_text(
                json.dumps(
                    {
                        "claims": [
                            {
                                "id": "C001",
                                "source_type": "official",
                                "source_name": "Official source",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            missing_link, _ = agent_status.build(project, root)
            self.assertIn("sources_not_passed", {item["code"] for item in missing_link["blocker_details"]})

    def test_claims_report_until_one_carries_domain_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            claims = project / "claims.json"

            def write(source_types):
                claims.write_text(
                    json.dumps(
                        {
                            "claims": [
                                {
                                    "id": f"C{index:03d}",
                                    "source_type": source_type,
                                    "source_name": "Source",
                                    "source_url": "https://example.invalid/1",
                                }
                                for index, source_type in enumerate(source_types)
                            ]
                        }
                    ),
                    encoding="utf-8",
                )

            def domain_notes(reported):
                return [n for n in reported["notes"] if "domain evidence" in n]

            # Institutional citations alone satisfy every other rule and still
            # leave the episode describing its subject from the outside.
            write(["statute", "news", "government_dataset"])
            summary, reported = agent_status.claim_summary(project)
            self.assertEqual(summary["domain_claims"], 0)
            self.assertEqual(len(domain_notes(reported)), 1)
            # Reported without blocking: the citations themselves are sound.
            self.assertEqual(reported["status"], "pass")
            self.assertEqual(reported["warnings"], [])

            write(["statute", "news", "price_listing"])
            summary, reported = agent_status.claim_summary(project)
            self.assertEqual(summary["domain_claims"], 1)
            self.assertEqual(domain_notes(reported), [])

    def test_malformed_claims_and_review_video_emit_blockers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = make_ready_project(root)
            (project / "claims.json").write_text('{"claims": 1}', encoding="utf-8")
            review = project / "quality-review" / "final-v1" / "review.json"
            review_data = json.loads(review.read_text(encoding="utf-8"))
            review_data["video"] = []
            review.write_text(json.dumps(review_data), encoding="utf-8")

            status, _ = agent_status.build(project, root)

            codes = {item["code"] for item in status["blocker_details"]}
            self.assertIn("sources_not_passed", codes)
            self.assertIn("video_binding_not_passed", codes)

    def test_stale_manifest_without_video_is_a_blocker_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "projects" / "empty"
            project.mkdir(parents=True)
            (project / "artifact_manifest.json").write_text(
                json.dumps(
                    {
                        "canonical": {
                            "final_video": {
                                "path": "projects/empty/output/missing-final.mp4",
                                "sha256": "0" * 64,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            status, _ = agent_status.build(project, root)

            self.assertIn("manifest_binding_not_passed", {item["code"] for item in status["blocker_details"]})

    def test_all_required_gates_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status, _ = agent_status.build(make_ready_project(root), root)

            self.assertEqual(status["overall_status"], "ready_for_human_upload_approval")
            self.assertEqual(status["blockers"], [])
            self.assertEqual(status["stages"]["duration"]["status"], "pass")
            self.assertEqual(status["stages"]["loudness"]["status"], "pass")

    def test_builds_resumable_status_from_project_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "projects" / "demo"
            (project / "output").mkdir(parents=True)
            (project / "quality-review" / "final-v1").mkdir(parents=True)

            (project / "script-proposal.md").write_text("proposal", encoding="utf-8")
            (project / "sources.md").write_text("sources", encoding="utf-8")
            (project / "claims.json").write_text(
                json.dumps(
                    {
                        "claims": [
                            {"id": "C001", "source_type": "official"},
                            {"id": "C002", "source_type": "official_search_snippet_page_blocked_by_bot_protection"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (project / "narration-final-v3-yu-sectioned-v5.mp3").write_bytes(b"audio")
            (project / "narration-final-v3-yu-sectioned-v5.srt").write_text("1\n", encoding="utf-8")
            (project / "narration-final-v3-yu-sectioned-v5.mp3.pron-ok.json").write_text("{}", encoding="utf-8")
            (project / "storyboard-final-timed-validation.json").write_text('{"ok": true}', encoding="utf-8")

            video = project / "output" / "demo-final-v1.mp4"
            video.write_bytes(b"video")
            sha = hashlib.sha256(b"video").hexdigest()
            (project / "output" / "demo-final-v1.mp4.render-result").write_text("PASS", encoding="utf-8")
            review = project / "quality-review" / "final-v1" / "review.json"
            review.write_text(
                json.dumps({"video": str(video), "video_sha256": sha, "publish_readiness": "ship", "critical_issues": []}),
                encoding="utf-8",
            )

            status, artifacts = agent_status.build(project, root)

            self.assertEqual(status["overall_status"], "in_progress")
            self.assertEqual(status["stages"]["tts"]["status"], "warn")
            self.assertEqual(status["stages"]["render"]["status"], "missing")
            self.assertEqual(status["stages"]["sources"]["status"], "warn")
            self.assertIn(
                "complete publish-metadata.json and generate a target-bound youtube-publish-pack.md",
                status["next_actions"],
            )
            self.assertEqual(artifacts["canonical"]["final_video"]["sha256"], sha)

    @unittest.skipUnless(os.name == "posix", "symlink test requires POSIX")
    def test_publish_pack_replaces_symlink_without_overwriting_its_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            sentinel = root / "sentinel"
            sentinel.write_text("keep", encoding="utf-8")
            destination = project / "youtube-publish-pack.md"
            destination.symlink_to(sentinel)
            (project / "publish-metadata.json").write_text(
                json.dumps(
                    {
                        "schema": "haru.publish_metadata.v1",
                        "project": project.name,
                        "title": "Fixture title",
                        "description": "Fixture description.",
                        "thumbnail_text": "Fixture thumbnail",
                        "hashtags": ["#Fixture"],
                        "source_statement": "Fixture source statement.",
                        "made_for_kids": False,
                        "category_id": "22",
                    }
                ),
                encoding="utf-8",
            )
            (project / "project-contract.json").write_text(
                json.dumps(
                    {
                        "schema": "haru.project_contract.v1",
                        "publish_target": {
                            "youtube_channel_id": "UCaaaaaaaaaaaaaaaaaaaaaa"
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(self.installed_tool("make_publish_pack.py")),
                    str(project),
                    "--workspace",
                    str(root),
                    "--write",
                ],
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertFalse(destination.is_symlink())
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o644)

class RenderSelfEvalGateTest(RuntimePolicyCase):
    """The HVP-33 gate as agent status reports it.

    Everything here goes through `agent_status.build`, because the whole point of
    the gate is what a reader downstream of it sees: which stage blocks, which
    action is named, what the manifest declares, and which refs an approval is
    allowed to bind.
    """

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def build(self, project):
        return agent_status.build(project, self.root)

    def codes(self, status):
        return {item["code"] for item in status["blocker_details"]}

    def test_gate_sits_between_render_and_qa_and_passes_on_a_sealed_project(self):
        project = make_ready_project(self.root)

        status, _ = self.build(project)

        required = status["required_stages"]
        self.assertEqual(
            required.index("render") + 1,
            required.index("render_self_eval"),
            "self-eval must be the gate immediately after render",
        )
        self.assertLess(
            required.index("render_self_eval"),
            required.index("qa"),
            "whole-video QA must come after the deterministic gate",
        )
        self.assertEqual(status["stages"]["render_self_eval"]["status"], "pass")
        self.assertEqual(status["blockers"], [], "fixture must be genuinely ready")

    def test_status_manifest_and_approval_refs_agree_about_the_same_bytes(self):
        """Three readers, one answer.

        The stage, the artifact manifest and the approval-intent ref are read by
        three different consumers (`status`, `artifact_index`, `approve_publish`).
        A disagreement between them is how an approval ends up bound to bytes the
        gate never cleared, so parity is asserted directly rather than assumed.
        """
        project = make_ready_project(self.root)
        promoted = project / canonical_layout.RENDER_SELF_EVAL_RESULT

        status, artifacts = self.build(project)

        ref = status["approval_intent_refs"]["render_self_eval"]
        declared = artifacts["canonical"]["render_self_eval"]
        self.assertEqual(ref["path"], render_self_eval.RESULT_PATH)
        self.assertEqual(ref["sha256"], hashlib.sha256(promoted.read_bytes()).hexdigest())
        self.assertEqual(ref["bytes"], promoted.stat().st_size)
        self.assertEqual(declared["sha256"], ref["sha256"])
        self.assertEqual(declared["bytes"], ref["bytes"])
        self.assertEqual(
            status["stages"]["render_self_eval"]["files"],
            [render_self_eval.RESULT_PATH],
        )

    def test_a_tampered_current_result_fails_closed_through_every_downstream_gate(self):
        """One extra byte in the projection must not degrade quietly.

        The project tree is an audit mirror, not the authority: editing it breaks
        the external anchor, so the gate blocks, sampling refuses, and no ref is
        offered for an approval to bind.
        """
        project = make_ready_project(self.root)
        promoted = project / canonical_layout.RENDER_SELF_EVAL_RESULT
        promoted.write_bytes(promoted.read_bytes() + b" ")

        status, _ = self.build(project)

        self.assertEqual(status["stages"]["render_self_eval"]["status"], "missing")
        self.assertIn("render_self_eval_not_passed", self.codes(status))
        self.assertIn("self_eval_unavailable", self.codes(status))
        self.assertIsNone(status["approval_intent_refs"]["render_self_eval"])
        self.assertIsNone(status["approval_intent_refs"]["visual_qa_review"])
        self.assertEqual(
            status["stages"]["qa"]["notes"][1]["code"], "self_eval_not_current"
        )

    def test_a_deleted_external_ledger_is_not_recoverable_from_the_project_tree(self):
        """The anti-substitution property, stated as a test.

        Every project byte is still exactly as the runtime sealed it. Only the
        external authority is gone, and that alone must revoke the pass —
        otherwise a copied or rolled-back project tree is a valid pass.
        """
        project = make_ready_project(self.root)
        before, _ = self.build(project)
        self.assertEqual(before["stages"]["render_self_eval"]["status"], "pass")

        shutil.rmtree(self.root / ".self-eval-state")

        status, _ = self.build(project)

        self.assertEqual(status["stages"]["render_self_eval"]["status"], "missing")
        self.assertIn("self_eval_unavailable", self.codes(status))
        self.assertIsNone(status["approval_intent_refs"]["render_self_eval"])

    def test_each_non_pass_state_names_itself_and_its_own_next_action(self):
        cases = (
            ("needs_human", 1, "self_eval_needs_human", "submit a render-self-eval-review vision verdict for the current attempt"),
            ("fail", 1, "self_eval_fail", "fix the source, render again, then reevaluate"),
            ("human_intervention_required", 3, "self_eval_human_intervention_required", "human intervention required: three self-evaluation attempts failed"),
        )
        for state, attempt, blocker_code, expected_action in cases:
            with self.subTest(state=state):
                project = make_ready_project(
                    self.root,
                    slug=f"state-{state.replace('_', '-')}",
                    self_eval_status=state,
                    self_eval_attempt=attempt,
                )

                status, _ = self.build(project)

                self.assertEqual(status["stages"]["render_self_eval"]["status"], "missing")
                self.assertIn(blocker_code, self.codes(status))
                self.assertEqual(
                    status["stages"]["render_self_eval"]["notes"][0]["status"], state
                )
                # The engine fixes one action per state; status must quote it
                # rather than fall back to the generic "run the evaluator".
                self.assertEqual(
                    status["stages"]["render_self_eval"]["notes"][0]["next_action"],
                    expected_action,
                )
                self.assertIn(expected_action, status["next_actions"])
                self.assertNotIn(
                    "run structure-aware visual sampling and record the human verdict",
                    status["next_actions"],
                    "sampling cannot be the next action while the gate blocks it",
                )

    def test_a_legacy_project_without_a_render_is_not_told_to_self_evaluate(self):
        """Ordering, not just presence.

        A project with no finished render is blocked on the self-eval gate — it
        has to be, the gate is required — but the *action* must still be "render",
        because self-evaluating bytes that do not exist is not a step anyone can
        take.
        """
        project = self.root / "projects" / "legacy"
        (project / "output").mkdir(parents=True)

        status, _ = self.build(project)

        self.assertEqual(status["stages"]["render"]["status"], "missing")
        self.assertIn("render_self_eval_not_passed", self.codes(status))
        self.assertIn(
            "run render-project and write the final render-result marker",
            status["next_actions"],
        )
        self.assertNotIn(
            agent_status.SELF_EVAL_NEXT_ACTIONS[None], status["next_actions"]
        )

    def test_the_gate_blocks_release_even_when_every_other_gate_passes(self):
        """The gate has to be load-bearing, not decorative.

        Deleting only the self-eval projection leaves a project that passed every
        pre-HVP-33 gate. If it still reported ready, the whole ticket would be a
        no-op.
        """
        project = make_ready_project(self.root)
        (project / canonical_layout.RENDER_SELF_EVAL_RESULT).unlink()

        status, _ = self.build(project)

        self.assertEqual(status["overall_status"], "in_progress")
        self.assertIn("render_self_eval_not_passed", self.codes(status))
        self.assertIn(
            canonical_layout.RENDER_SELF_EVAL_RESULT,
            [
                path
                for item in status["blocker_details"]
                for path in item["expected_artifacts"]
            ],
            "the blocker must name the artifact a producer needs",
        )



if __name__ == "__main__":
    unittest.main()
