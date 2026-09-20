import json

import pytest

from workspace import initialize


def test_workspace_is_idempotent_and_contains_no_provider_configuration(tmp_path):
    root = tmp_path / "work"
    first = initialize(root)
    assert initialize(root) == first
    assert (root / "projects").is_dir()
    assert (root / "inbox").is_dir()
    assert (root / "exports").is_dir()
    assert set(json.loads((root / "workspace.json").read_text())) == {"schema", "workspace_id"}


def test_workspace_refuses_redirected_private_state_without_writing(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".video-studio").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        initialize(root)
    assert list(outside.iterdir()) == []
    assert not (root / "workspace.json").exists()


def test_workspace_refuses_unknown_existing_version(tmp_path):
    (tmp_path / "workspace.json").write_text('{"schema":"future.v2"}')
    with pytest.raises(ValueError, match="unsupported"):
        initialize(tmp_path)
