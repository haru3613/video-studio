import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import overview_catalog


def project(source, name):
    return {"source": source, "name": name, "updated_at": "2026-09-12T00:00:00+00:00"}


def group(group_id, members, primary=None, **extra):
    primary = primary or members[0]
    value = {
        "id": group_id,
        "title": group_id,
        "type": "video",
        "collection": "Test",
        "primary": primary,
        "members": members,
        "cover": None,
        "reason": "test",
    }
    value.update(extra)
    return value


class OverviewCatalogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.roots = {"studio": base / "studio", "media": base / "media"}
        for root in self.roots.values():
            root.mkdir()
        self.catalog = base / "catalog.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write_catalog(self, groups, schema="haru.dashboard_catalog.v1"):
        self.catalog.write_text(json.dumps({"schema": schema, "groups": groups}), encoding="utf-8")

    def test_six_versions_become_one_explicit_group(self):
        members = [{"source": "studio", "name": f"version-{i}", "label": str(i)} for i in range(6)]
        self.write_catalog([group("one-project", members)])
        result = overview_catalog.build_overview([project("studio", item["name"]) for item in members], self.roots, self.catalog)
        self.assertEqual(result["source_count"], 6)
        self.assertEqual(result["counts"], {"video": 1, "library": 0, "experiment": 0, "unclassified": 0})
        self.assertEqual(len(result["groups"]), 1)
        self.assertEqual(len(result["groups"][0]["members"]), 6)
        self.assertEqual(result["groups"][0]["primary"]["name"], "version-0")
        self.assertEqual(result["groups"][0]["keywords"], [])

    def test_keywords_are_preserved_and_malformed_keywords_degrade(self):
        member = {"source": "studio", "name": "birthrate", "label": "main"}
        self.write_catalog([group("birthrate", [member], keywords=["少子化", "出生率", "Mina"])])
        result = overview_catalog.build_overview([project("studio", "birthrate")], self.roots, self.catalog)
        self.assertEqual(result["groups"][0]["keywords"], ["少子化", "出生率", "Mina"])
        self.write_catalog([group("birthrate", [member], keywords="少子化")])
        result = overview_catalog.build_overview([project("studio", "birthrate")], self.roots, self.catalog)
        self.assertEqual(result["counts"]["unclassified"], 1)
        self.write_catalog([group("birthrate", [member], keywords=["少子化", 1])])
        result = overview_catalog.build_overview([project("studio", "birthrate")], self.roots, self.catalog)
        self.assertEqual(result["counts"]["unclassified"], 1)

    def test_shipped_catalog_contains_no_operator_projects_and_preserves_discovery(self):
        shipped = json.loads(overview_catalog.DEFAULT_CATALOG.read_text(encoding="utf-8"))
        self.assertEqual(shipped["groups"], [])
        cards = [project("studio", "first-project"), project("studio", "second-project")]
        result = overview_catalog.build_overview(cards, self.roots)
        self.assertEqual(result["source_count"], 2)
        self.assertEqual(result["counts"]["unclassified"], 2)

    def test_unknown_and_cross_source_same_name_are_standalone_once(self):
        known = {"source": "studio", "name": "same", "label": "known"}
        self.write_catalog([group("known", [known])])
        projects = [project("studio", "same"), project("media", "same"), project("studio", "new")]
        result = overview_catalog.build_overview(projects, self.roots, self.catalog)
        self.assertEqual(result["source_count"], 3)
        self.assertEqual([(g["type"], g["primary"]["source"], g["primary"]["name"]) for g in result["groups"]], [("video", "studio", "same"), ("unclassified", "media", "same"), ("unclassified", "studio", "new")])

    def test_missing_primary_does_not_substitute_another_member(self):
        primary = {"source": "studio", "name": "missing", "label": "selected"}
        member = {"source": "studio", "name": "present", "label": "other"}
        self.write_catalog([group("selected", [primary, member], primary=primary)])
        result = overview_catalog.build_overview([project("studio", "present")], self.roots, self.catalog)
        shelf = result["groups"][0]
        self.assertIsNone(shelf["primary"])
        self.assertEqual([card["name"] for card in shelf["members"]], ["present"])
        self.assertTrue(any("primary missing" in warning for warning in result["warnings"]))

    def test_missing_member_keeps_group_and_warns(self):
        present = {"source": "studio", "name": "present", "label": "main"}
        absent = {"source": "studio", "name": "absent", "label": "old"}
        self.write_catalog([group("partial", [present, absent])])
        result = overview_catalog.build_overview([project("studio", "present")], self.roots, self.catalog)
        self.assertEqual(len(result["groups"]), 1)
        self.assertEqual(len(result["groups"][0]["members"]), 1)
        self.assertTrue(any("member missing" in warning for warning in result["warnings"]))

    def test_all_missing_members_omit_group_and_leave_discovered_unknowns(self):
        absent = {"source": "studio", "name": "absent", "label": "old"}
        self.write_catalog([group("gone", [absent])])
        result = overview_catalog.build_overview([project("studio", "new")], self.roots, self.catalog)
        self.assertEqual([item["id"] for item in result["groups"]], ["unclassified-studio-new"])
        self.assertTrue(any("omitted" in warning for warning in result["warnings"]))

    def test_bad_schema_and_conflicting_membership_degrade_without_loss(self):
        projects = [project("studio", "a"), project("studio", "b")]
        self.write_catalog([], schema="wrong")
        bad = overview_catalog.build_overview(projects, self.roots, self.catalog)
        self.assertEqual(bad["counts"]["unclassified"], 2)
        entry = {"source": "studio", "name": "a", "label": "a"}
        self.write_catalog([group("first", [entry]), group("second", [entry])])
        conflict = overview_catalog.build_overview(projects, self.roots, self.catalog)
        self.assertEqual(conflict["counts"]["unclassified"], 2)
        self.assertTrue(any("ignored" in warning for warning in conflict["warnings"]))

    def test_malformed_unhashable_and_nul_catalog_values_degrade_without_error(self):
        cards = [project("studio", "a")]
        member = {"source": ["studio"], "name": "a", "label": "bad"}
        self.write_catalog([group("bad-source", [member])])
        result = overview_catalog.build_overview(cards, self.roots, self.catalog)
        self.assertEqual(result["counts"]["unclassified"], 1)
        member = {"source": "studio", "name": "a", "label": "bad"}
        self.write_catalog([group("bad-type", [member], type=["video"])])
        result = overview_catalog.build_overview(cards, self.roots, self.catalog)
        self.assertEqual(result["counts"]["unclassified"], 1)
        member = {"source": "studio", "name": "a\x00bad", "label": "bad"}
        self.write_catalog([group("bad-nul", [member])])
        result = overview_catalog.build_overview(cards, self.roots, self.catalog)
        self.assertEqual(result["counts"]["unclassified"], 1)

    def test_cover_blocks_traversal_symlink_and_non_image_and_quotes_url(self):
        name = 'quoted " project'
        (self.roots["studio"] / name / "output").mkdir(parents=True)
        cover = self.roots["studio"] / name / "output" / 'cover "1.png'
        cover.write_bytes(b"image")
        member = {"source": "studio", "name": name, "label": "q"}
        self.write_catalog([group("quoted", [member], cover='/media/studio/quoted%20%22%20project/output/cover%20%221.png')])
        result = overview_catalog.build_overview([project("studio", name)], self.roots, self.catalog)
        self.assertIn("%22", result["groups"][0]["primary"]["url"])
        self.assertEqual(result["groups"][0]["cover"], '/media/studio/quoted%20%22%20project/output/cover%20%221.png')

        self.write_catalog([group("quoted", [member], cover="/media/studio/quoted%20%22%20project/../secret.png")])
        self.assertIsNone(overview_catalog.build_overview([project("studio", name)], self.roots, self.catalog)["groups"][0]["cover"])
        self.write_catalog([group("quoted", [member], cover='/media/studio/quoted%20%22%20project/output/nope.txt')])
        self.assertIsNone(overview_catalog.build_overview([project("studio", name)], self.roots, self.catalog)["groups"][0]["cover"])
        target = Path(self.tmp.name) / "outside.png"
        target.write_bytes(b"image")
        link = self.roots["studio"] / name / "output" / "link.png"
        try:
            os.symlink(target, link)
        except (NotImplementedError, OSError):
            self.skipTest("symlinks unavailable")
        self.write_catalog([group("quoted", [member], cover='/media/studio/quoted%20%22%20project/output/link.png')])
        self.assertIsNone(overview_catalog.build_overview([project("studio", name)], self.roots, self.catalog)["groups"][0]["cover"])

    def test_cover_handles_malformed_url_hidden_path_and_repeated_component(self):
        name = "same"
        (self.roots["studio"] / name / name).mkdir(parents=True)
        (self.roots["studio"] / name / name / "cover.png").write_bytes(b"image")
        member = {"source": "studio", "name": name, "label": "same"}
        self.write_catalog([group("same", [member], cover="/media/studio/same/same/cover.png")])
        result = overview_catalog.build_overview([project("studio", name)], self.roots, self.catalog)
        self.assertEqual(result["groups"][0]["cover"], "/media/studio/same/same/cover.png")
        self.write_catalog([group("same", [member], cover="http://[bad")])
        self.assertIsNone(overview_catalog.build_overview([project("studio", name)], self.roots, self.catalog)["groups"][0]["cover"])
        self.write_catalog([group("same", [member], cover="/media/studio/same/.hidden/cover.png")])
        self.assertIsNone(overview_catalog.build_overview([project("studio", name)], self.roots, self.catalog)["groups"][0]["cover"])


if __name__ == "__main__":
    unittest.main()
