import json
from pathlib import Path

import pytest

import local_delivery


def test_blocked_project_can_export_only_explicit_diagnostics(tmp_path):
    project = tmp_path / "projects/demo"
    project.mkdir(parents=True)
    project = project.resolve()
    destination = tmp_path / "exports"
    destination.mkdir()
    with pytest.raises(local_delivery.DeliveryError, match="blockers"):
        local_delivery.export_delivery(project, destination=destination, idempotency_key="normal")
    result = local_delivery.export_diagnostics(project, destination=destination, idempotency_key="debug")
    assert result["status"] == "diagnostic_only"
    assert result["delivery_ready"] is False
    report = json.loads((Path(result["bundle_path"]) / "diagnostic.json").read_text())
    assert report["technical_status"]["blockers"]
    assert str(project) not in json.dumps(report)
    assert not (Path(result["bundle_path"]) / "video/final.mp4").exists()
    retry = local_delivery.export_diagnostics(project, destination=destination, idempotency_key="debug")
    assert retry["reused"] is True
    assert not (project / "publish").exists()


def test_tampered_diagnostic_bundle_is_never_reused(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    destination = tmp_path / "exports"
    destination.mkdir()
    result = local_delivery.export_diagnostics(project.resolve(), destination=destination, idempotency_key="debug")
    (Path(result["bundle_path"]) / "diagnostic.json").write_text('{"status":"pass"}')
    with pytest.raises(local_delivery.DeliveryError, match="differs"):
        local_delivery.export_diagnostics(project.resolve(), destination=destination, idempotency_key="debug")
