import json
from pathlib import Path
import sys
import tempfile
import unittest
from runtime_test_fixture import RuntimePolicyCase

sys.path.insert(0, str(Path(__file__).resolve().parent))

import make_publish_pack


def write_metadata(project_path, **overrides):
    value = {
        "schema": "haru.publish_metadata.v1",
        "project": project_path.name,
        "title": "Mina 睡前故事會｜拇指姑娘",
        "description": "一個睡前童話故事。",
        "thumbnail_text": "小小的人，大大的夢",
        "hashtags": ["#Mina睡前故事會", "#拇指姑娘"],
        "source_statement": "改寫自公版經典童話《拇指姑娘》。",
        "made_for_kids": True,
        "category_id": "22",
    }
    value.update(overrides)
    (project_path / "publish-metadata.json").write_text(
        json.dumps(value, ensure_ascii=False), encoding="utf-8"
    )



def write_contract(project_path):
    (project_path / "project-contract.json").write_text(
        json.dumps(
            {
                "schema": "haru.project_contract.v1",
                "lane_contract": "manual.v1",
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

class PublishPackMetadataTest(RuntimePolicyCase):
    def test_project_metadata_drives_pack_without_cross_project_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            project = workspace / "projects" / "mina-thumbelina"
            project.mkdir(parents=True)
            write_contract(project)
            write_metadata(project)

            pack = make_publish_pack.build_pack(project, workspace)

            self.assertIn("Mina 睡前故事會｜拇指姑娘", pack)
            self.assertIn("一個睡前童話故事。", pack)
            self.assertIn("小小的人，大大的夢", pack)
            self.assertIn("#Mina睡前故事會 #拇指姑娘", pack)
            self.assertIn("改寫自公版經典童話《拇指姑娘》。", pack)
            self.assertNotIn("TODO", pack)
            for leaked in ("年輕人", "房價", "少子化", "人生不該像押注"):
                self.assertNotIn(leaked, pack)

    def test_missing_todo_or_wrong_project_metadata_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            project = workspace / "projects" / "mina-thumbelina"
            project.mkdir(parents=True)
            write_contract(project)
            cases = (
                {"title": ""},
                {"description": "TODO: write this"},
                {"project": "another-project"},
                {"hashtags": []},
                {"source_statement": None},
                {"made_for_kids": "yes"},
                {"category_id": "people"},
            )
            for invalid in cases:
                with self.subTest(invalid=invalid):
                    write_metadata(project, **invalid)
                    with self.assertRaises(ValueError):
                        make_publish_pack.build_pack(project, workspace)


if __name__ == "__main__":
    unittest.main()
