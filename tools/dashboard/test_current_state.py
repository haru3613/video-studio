import json

import server


def test_saved_ready_state_cannot_override_missing_real_artifacts(tmp_path):
    project = tmp_path / "projects/demo"
    project.mkdir(parents=True)
    (project / "project-contract.json").write_text(json.dumps({"schema": "haru.project_contract.v1", "lane_contract": "manual.v1"}))
    (project / "pipeline_status.json").write_text(json.dumps({"overall_status": "publish_approved", "blockers": []}))
    status, _ = server.current_pipeline(project)
    assert status["overall_status"] == "in_progress"
    assert status["blockers"]


def test_legacy_status_is_not_publication_evidence(tmp_path):
    (tmp_path / "pipeline_status.json").write_text('{"overall_status":"publish_approved","stages":{"qa":{"status":"pass"}}}')
    status, artifacts = server.current_pipeline(tmp_path)
    assert status["overall_status"] == "unverified"
    assert status["stages"] == {}
    assert artifacts is None
