#!/usr/bin/env python3
"""Anti-drift tests for the canonical layout declaration.

The declaration is only worth having if it cannot quietly disagree with the gate
it claims to describe. Two directions, both tested:

* **Over-claiming** — the declaration lists something the gate does not want. A
  project that genuinely satisfies the gate would then still show outstanding
  artifacts.
* **Under-claiming** — the gate requires something the declaration omits. A
  producer following the declaration would build a project that still blocks,
  which is exactly the trap this whole ticket exists to remove.

Without both, `canonical_layout.py` becomes a second mirror: a description of
the gate written by reading the gate, agreeing with it by construction and
proving nothing.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from runtime_test_fixture import RuntimePolicyCase
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_status  # noqa: E402
import canonical_layout as layout  # noqa: E402
import render_self_eval  # noqa: E402
from test_agent_status import make_ready_project  # noqa: E402


def required_gates(root: Path) -> set:
    """Gate names the status builder treats as required, read from the builder."""
    empty = root / "projects" / "nothing"
    empty.mkdir(parents=True, exist_ok=True)
    status, _ = agent_status.build(empty, root)
    return {d["stage"] for d in status["blocker_details"]}


class CanonicalLayoutTest(RuntimePolicyCase):
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()

    def tearDown(self):
        self._tmp.cleanup()

    def contract_is_accepted(self, lane, profile, runtime_contract=...):
        """Run the project-contract artifact validator over one lane/profile pair."""
        artifact = layout._artifact("project-contract.json")
        value = {
            "schema": "haru.project_contract.v1",
            "lane_contract": lane,
            "publish_target": {
                "youtube_channel_id": "UCaaaaaaaaaaaaaaaaaaaaaa"
            },
        }
        if profile is not None:
            value["production_profile"] = profile
        if runtime_contract is ...:
            runtime_contract = {
                "schema": layout.PROJECT_RUNTIME_CONTRACT_SCHEMA,
                "runtime": "haru.runtime.v1",
                "evaluator": "haru.evaluator.v1",
                "artifact": "haru.artifact.v1",
            }
        if runtime_contract is not None:
            value["runtime_contract"] = runtime_contract
        path = self.root / "project-contract.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        try:
            layout._validate_source(path, artifact)
            return True
        except ValueError:
            return False

    def test_a_produced_contract_cannot_drop_the_runtime_compatibility_block(self):
        """Producing project-contract.json is the one moment the block can be
        lost: an agent rewrites the lane fields and silently drops what `create`
        wrote, and the runtime then refuses to serve the project with nothing
        pointing at why. Presence and shape are checked here; which versions are
        servable is the runtime's call, not this module's."""
        self.assertFalse(self.contract_is_accepted("manual.v1", None, runtime_contract=None))
        self.assertFalse(
            self.contract_is_accepted("manual.v1", None, runtime_contract={"runtime": "haru.runtime.v1"})
        )
        self.assertFalse(
            self.contract_is_accepted(
                "manual.v1",
                None,
                runtime_contract={
                    "schema": layout.PROJECT_RUNTIME_CONTRACT_SCHEMA,
                    "runtime": "haru.runtime.v1",
                    "evaluator": "  ",
                    "artifact": "haru.artifact.v1",
                },
            )
        )
        # An unknown version is still well-formed: refusing it here would put the
        # compatibility decision in two places.
        self.assertTrue(
            self.contract_is_accepted(
                "manual.v1",
                None,
                runtime_contract={
                    "schema": layout.PROJECT_RUNTIME_CONTRACT_SCHEMA,
                    "runtime": "haru.runtime.v99",
                    "evaluator": "haru.evaluator.v99",
                    "artifact": "haru.artifact.v99",
                },
            )
        )

    def test_a_lane_that_names_a_presenter_pins_it(self):
        """Each presenter lane accepts only its own profile, and every lane rejects junk."""
        self.assertTrue(self.contract_is_accepted("social_issue_longform.v1", "host_longform.v1"))
        self.assertTrue(self.contract_is_accepted("tech_longform.v1", "technical_host.v1"))

        # Crossing the two is the mistake worth catching: both halves are individually
        # valid, so nothing else in the pipeline would notice.
        self.assertFalse(self.contract_is_accepted("social_issue_longform.v1", "technical_host.v1"))
        self.assertFalse(self.contract_is_accepted("tech_longform.v1", "host_longform.v1"))

        self.assertFalse(self.contract_is_accepted("tech_longform.v1", None))
        self.assertFalse(self.contract_is_accepted("not_a_lane.v1", "technical_host.v1"))

    def test_the_manual_lane_must_not_claim_a_presenter(self):
        """Only pinned lanes are gated at render, so a manual project carrying a
        profile would read as presenter-bound while skipping the editorial gate."""
        self.assertTrue(self.contract_is_accepted("manual.v1", None))
        self.assertFalse(self.contract_is_accepted("manual.v1", "technical_host.v1"))
        self.assertFalse(self.contract_is_accepted("manual.v1", "host_longform.v1"))

    def test_every_pinned_lane_pins_a_profile_the_contract_validator_knows(self):
        """A lane pinned to a profile no validator recognises would fail closed forever."""
        import editorial_contract

        for lane, contract in agent_status.LANE_CONTRACTS.items():
            profile = contract.get("production_profile")
            if profile is None:
                continue
            with self.subTest(lane=lane):
                self.assertIn(profile, editorial_contract.PROFILES)
                self.assertTrue(self.contract_is_accepted(lane, profile))

    def test_declaration_does_not_over_claim(self):
        """A project the gate accepts must leave nothing outstanding."""
        project = make_ready_project(self.root)
        status, _ = agent_status.build(project, self.root)
        self.assertEqual(status["blockers"], [], "fixture must be genuinely ready")

        pending = layout.outstanding(project)
        self.assertEqual(
            [a.path for a in pending],
            [],
            "the declaration lists artifacts a passing project does not have",
        )

    def test_declaration_does_not_under_claim(self):
        """Every required gate must be reachable through a declared artifact."""
        declared = {a.gate for a in layout.ARTIFACTS}
        needed = required_gates(self.root)

        # Gates satisfied by other gates' artifacts rather than one of their own.
        derived = {
            "cover",           # produced into output/ by the render stage
            "loudness",        # measured from the render result
            "video_binding",   # relationship between render and review
            "review_freshness",
            "publish_target",  # validated from project-contract.json
        }
        uncovered = needed - declared - derived
        self.assertEqual(
            uncovered,
            set(),
            f"required gates with no declared artifact and no derivation: {uncovered}",
        )

    def test_scaffold_never_produces_a_passing_project(self):
        """Placeholders must not be mistakable for work.

        A scaffold that satisfied gates would be the worst possible outcome:
        green on an empty project.
        """
        project = self.root / "projects" / "scaffolded"
        layout.scaffold(project)
        status, _ = agent_status.build(project, self.root)
        self.assertNotEqual(status["overall_status"], "ready_for_human_upload_approval")
        self.assertTrue(status["blockers"])

    def test_no_individual_gate_passes_on_a_placeholder(self):
        """Per-gate, not aggregate — the distinction this test exists for.

        `test_scaffold_never_produces_a_passing_project` asserts only that the
        project as a whole is not ready and has blockers. That stayed green
        while `proposal` and `publish_pack` both reported `pass` on a freshly
        scaffolded project, because each only asks whether a non-empty file
        exists and a placeholder is a non-empty file.

        A green gate on a stub is worse than a missing one: it tells a producer
        that stage is done.
        """
        project = self.root / "projects" / "scaffolded"
        layout.scaffold(project)
        status, _ = agent_status.build(project, self.root)

        passing = sorted(g for g, v in status["stages"].items() if v["status"] == "pass")
        self.assertEqual(
            passing, [], f"these gates pass on placeholders alone: {passing}"
        )

    def test_scaffold_then_checklist_reports_everything_outstanding(self):
        project = self.root / "projects" / "scaffolded"
        layout.scaffold(project)
        pending = {a.path for a in layout.outstanding(project)}
        expected = {a.path for a in layout.ARTIFACTS if a.kind != "dir"}
        self.assertEqual(pending, expected)

    def test_every_release_artifact_names_its_repo_owned_producer(self):
        missing = [artifact.path for artifact in layout.ARTIFACTS if not artifact.producer]
        self.assertEqual(missing, [])

    def test_self_eval_current_and_history_paths_are_declared_without_scaffolding_receipts(self):
        """Declare the lane without manufacturing authority-looking JSON."""
        declared = {
            artifact.path: artifact
            for artifact in layout.ARTIFACTS
        }
        result = declared[layout.RENDER_SELF_EVAL_RESULT]
        self.assertEqual(result.gate, "render_self_eval")
        self.assertEqual(result.producer, "scripts/render-self-eval evaluate")
        self.assertEqual(
            layout.RENDER_SELF_EVAL_RESULT,
            render_self_eval.RESULT_PATH,
            "the layout and engine must name the same current projection",
        )
        self.assertEqual(
            layout.RENDER_SELF_EVAL_ROOT,
            render_self_eval.ROOT,
            "the layout and engine must name the same history root",
        )
        self.assertIn(
            layout.RENDER_SELF_EVAL_RESULT,
            layout.GENERATED_NEVER_SCAFFOLD,
        )
        for history in (
            layout.RENDER_SELF_EVAL_ATTEMPTS,
            layout.RENDER_SELF_EVAL_ORPHANS,
            layout.RETIRED_PRE_SELF_EVAL_ROOT,
            layout.APPROVAL_HISTORY_ROOT,
        ):
            self.assertNotIn(
                history,
                declared,
                f"{history} is runtime-allocated immutable history, not a scaffold target",
            )

        project = self.root / "projects" / "scaffolded-self-eval"
        layout.scaffold(project)
        self.assertFalse((project / layout.RENDER_SELF_EVAL_RESULT).exists())
        self.assertFalse((project / layout.RENDER_SELF_EVAL_ATTEMPTS).exists())
        self.assertFalse((project / layout.RETIRED_PRE_SELF_EVAL_ROOT).exists())
        self.assertIn(
            layout.RENDER_SELF_EVAL_RESULT,
            {artifact.path for artifact in layout.outstanding(project)},
        )

    def test_self_eval_current_projection_cannot_use_the_generic_producer(self):
        project = self.root / "projects" / "cannot-forge-self-eval"
        project.mkdir(parents=True)
        source = self.root / "forged.json"
        source.write_text('{"schema":"haru.render_self_eval.v1"}')

        with self.assertRaisesRegex(
            ValueError, "generated artifacts must use their dedicated producer"
        ):
            layout.produce(
                project,
                layout.RENDER_SELF_EVAL_RESULT,
                source,
                produced_by="caller",
            )

    def test_produce_validates_then_atomically_writes_a_digest_receipt(self):
        project = self.root / "projects" / "produced"
        project.mkdir(parents=True)
        source = self.root / "proposal.md"
        source.write_text("# Real proposal\n\nThis came from the research stage.\n")

        receipt = layout.produce(
            project,
            "script-proposal.md",
            source,
            produced_by="codex",
        )

        target = project / "script-proposal.md"
        receipt_path = project / ".hvp/producer-receipts/script-proposal.md.json"
        self.assertEqual(target.read_text(), source.read_text())
        self.assertEqual(receipt["schema"], "haru.producer_receipt.v1")
        self.assertEqual(receipt["artifact"], "script-proposal.md")
        self.assertEqual(receipt["output_sha256"], layout.sha256(target))
        self.assertEqual(json.loads(receipt_path.read_text()), receipt)
        self.assertFalse(any(path.name.endswith(".tmp") for path in project.rglob("*")))

        source.write_text(layout.TODO_MARKER)
        with self.assertRaises(ValueError):
            layout.produce(
                project,
                "script-proposal.md",
                source,
                produced_by="codex",
            )
        self.assertIn("Real proposal", target.read_text())

    def test_produce_accepts_optional_segment_plan_without_scaffolding_it(self):
        project = self.root / "projects" / "segmented"
        project.mkdir(parents=True)
        source = self.root / "segment-plan.json"
        plan = {
            "schema": "haru.segment_plan.v1",
            "segments": [
                {"segment_id": segment_id}
                for segment_id in ("qi", "cheng", "zhuan", "he")
            ],
        }
        source.write_text(json.dumps(plan), encoding="utf-8")

        receipt = layout.produce(
            project,
            "segment-plan.json",
            source,
            produced_by="test",
        )

        self.assertEqual(receipt["artifact"], "segment-plan.json")
        self.assertEqual(receipt["gate"], "segments")
        self.assertEqual(
            json.loads((project / "segment-plan.json").read_text(encoding="utf-8")),
            plan,
        )
        self.assertNotIn("segment-plan.json", {artifact.path for artifact in layout.ARTIFACTS})
        layout.scaffold(project)
        self.assertNotIn(
            "segment-plan.json",
            {artifact.path for artifact in layout.outstanding(project)},
        )

        source.write_text('{"schema":"wrong","segments":[]}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "segment plan schema"):
            layout.produce(
                project,
                "segment-plan.json",
                source,
                produced_by="test",
                force=True,
            )


    def test_produce_replaces_scaffolded_empty_binary_without_force(self):
        project = self.root / "projects" / "produced-binary"
        layout.scaffold(project)
        source = self.root / "narration.mp3"
        source.write_bytes(b"real narration")

        layout.produce(
            project,
            "narration-final.mp3",
            source,
            produced_by="test",
        )

        self.assertEqual(
            (project / "narration-final.mp3").read_bytes(),
            b"real narration",
        )

    def test_produce_refuses_symlinked_receipt_state_before_artifact_commit(self):
        project = self.root / "projects" / "linked-state"
        project.mkdir(parents=True)
        outside = self.root / "outside-state"
        outside.mkdir()
        (project / ".hvp").symlink_to(outside, target_is_directory=True)
        source = self.root / "proposal.md"
        source.write_text("# Real proposal\n")

        with self.assertRaises(ValueError):
            layout.produce(
                project,
                "script-proposal.md",
                source,
                produced_by="test",
            )

        self.assertFalse((project / "script-proposal.md").exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_produce_commits_the_exact_bytes_that_were_validated(self):
        project = self.root / "projects" / "snapshot-source"
        project.mkdir(parents=True)
        source = self.root / "proposal.md"
        source.write_text("# Valid proposal\n")
        validate = layout._validate_source

        def swap_after_validation(path, artifact):
            payload = validate(path, artifact)
            path.write_text(layout.TODO_MARKER)
            return payload

        with mock.patch.object(
            layout, "_validate_source", side_effect=swap_after_validation
        ):
            layout.produce(
                project,
                "script-proposal.md",
                source,
                produced_by="test",
            )

        self.assertEqual(
            (project / "script-proposal.md").read_text(),
            "# Valid proposal\n",
        )

    def test_real_project_layout_snapshot_is_not_gate_reflected(self):
        fixture = json.loads(
            (
                Path(__file__).resolve().parent
                / "fixtures/scam-industry-chain-taiwan-2026-layout.json"
            ).read_text()
        )
        self.assertEqual(fixture["schema"], "haru.real_project_layout_fixture.v1")
        self.assertEqual(
            fixture["source_project"], "scam-industry-chain-taiwan-2026"
        )
        observed = set(fixture["observed_artifacts"])
        self.assertTrue(
            {
                "claims.json",
                "narration-final.mp3",
                "output/final.mp4",
                "quality-review/final-v3/prep.json",
                "remotion/src/Episode.tsx",
            }
            <= observed
        )
        self.assertNotIn(layout.TODO_MARKER, json.dumps(fixture))

    def test_scaffold_is_idempotent_and_never_clobbers_real_work(self):
        project = make_ready_project(self.root)
        proposal = project / "script-proposal.md"
        before = proposal.read_text(encoding="utf-8")
        layout.scaffold(project)
        self.assertEqual(proposal.read_text(encoding="utf-8"), before)

        status, _ = agent_status.build(project, self.root)
        self.assertEqual(status["blockers"], [], "scaffold must not break a ready project")

    def test_generated_artifacts_are_never_scaffolded(self):
        """A stub pipeline_status.json would be read as real state.

        A real project was found carrying a 2026-07-05 status claiming
        ready_with_warnings — a value this code no longer emits — while
        recomputing gave nine blockers.
        """
        project = self.root / "projects" / "scaffolded"
        layout.scaffold(project)
        for generated in layout.GENERATED_NEVER_SCAFFOLD:
            self.assertFalse(
                (project / generated).exists(),
                f"{generated} must never be scaffolded",
            )

    def test_never_scaffold_covers_everything_the_tooling_writes(self):
        """Derive the generated set from the *writer*, not from the list.

        `test_generated_artifacts_are_never_scaffolded` iterates
        GENERATED_NEVER_SCAFFOLD, so it can only catch an entry that is listed
        and scaffolded anyway — never a *missing* entry. Deleting
        artifact_manifest.json from the tuple left it green, which is the same
        second-mirror failure this whole module exists to avoid.

        So ask the tool instead. The project starts *empty* — not scaffolded,
        not a ready fixture — so what appears is derived purely from the
        writer's own behaviour, with no input from this module. Seeding it any
        other way reintroduces the circularity: a scaffolded project already
        contains whatever the tuple currently permits.

        Scope: `agent_status --write` only. `publish/publish-approval.json` has
        its own writer and its own tests; covering it here would mean driving a
        full approval, which those tests already do.
        """
        project = self.root / "projects" / "written"
        project.mkdir(parents=True)
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent / "agent_status.py"),
             str(project), "--workspace", str(self.root), "--write"],
            check=True, capture_output=True,
        )
        produced = {p.name for p in project.iterdir()}
        self.assertTrue(produced, "the writer produced nothing — test would pass vacuously")

        missing = produced - set(layout.GENERATED_NEVER_SCAFFOLD)
        self.assertEqual(
            missing,
            set(),
            f"tooling writes these but scaffold would stub them: {sorted(missing)}",
        )

    def test_checklist_never_reports_done_on_something_the_gate_blocks(self):
        """The checklist and the gate must agree, or the checklist lies.

        Both cases below were found by review on the merged version, where
        `outstanding()` used plain `is_file()` and swallowed UnicodeDecodeError
        as "somebody produced it for real". Each read `[x] done` in the
        checklist while `agent_status.build()` blocked the project — and
        docs/canonical-layout.md sells the checklist exit code as a script gate,
        so the disagreement was reachable, not theoretical.
        """
        outside = self.root / "outside.md"
        outside.write_text("content living outside the project\n", encoding="utf-8")

        # Each artifact is paired with the gate that actually reads it. Pairing
        # them wrongly makes the test vacuous: an undecodable script-proposal.md
        # does not block, because the proposal gate only checks the file exists.
        cases = (
            ("symlinked", "script-proposal.md", "proposal",
             lambda p: (p.unlink(), p.symlink_to(outside))),
            ("undecodable", "claims.json", "sources",
             lambda p: p.write_bytes(b"\xff\xfe not utf-8 \x00")),
        )
        for name, path, gate, corrupt in cases:
            with self.subTest(case=name):
                project = make_ready_project(self.root, slug=f"corrupt-{name}")
                corrupt(project / path)

                status, _ = agent_status.build(project, self.root)
                self.assertIn(
                    gate,
                    {d["stage"] for d in status["blocker_details"]},
                    "fixture did not actually break the gate — test would be vacuous",
                )
                self.assertIn(
                    path,
                    {a.path for a in layout.outstanding(project)},
                    "checklist reports done on a project the gate blocks",
                )

        self.assertEqual(
            outside.read_text(encoding="utf-8"),
            "content living outside the project\n",
            "the symlink target must not have been touched",
        )

    def test_scaffold_refuses_to_write_through_a_symlink(self):
        """`--force` must not clobber a file outside the project."""
        outside = self.root / "precious.md"
        outside.write_text("do not overwrite me\n", encoding="utf-8")

        project = self.root / "projects" / "linked"
        project.mkdir(parents=True)
        (project / "script-proposal.md").symlink_to(outside)

        with self.assertRaises(ValueError):
            layout.scaffold(project, force=True)
        self.assertEqual(outside.read_text(encoding="utf-8"), "do not overwrite me\n")

    def test_checklist_exit_state_matches_outstanding(self):
        project = self.root / "projects" / "scaffolded"
        layout.scaffold(project)
        self.assertIn(layout.TODO_MARKER, (project / "claims.json").read_text(encoding="utf-8"))
        text = layout.checklist(project)
        self.assertIn("script-proposal.md", text)
        self.assertIn("[ ]", text)

        ready = make_ready_project(self.root, slug="ready")
        self.assertNotIn("[ ]", layout.checklist(ready))


if __name__ == "__main__":
    unittest.main()
