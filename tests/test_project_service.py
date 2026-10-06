import json
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from schemas.project import Project
from schemas.timeline import Timeline
from services.project_service import PROJECT_DIRECTORIES, create_project


def test_create_temporary_project(tmp_path):
    source = tmp_path / "source media ü"
    source.mkdir()
    media = source / "original.mp4"
    media.write_bytes(b"unchanged source")
    result = create_project("Test Project", source, projects_root=tmp_path / "projects")

    assert result.success and result.error is None
    project_path = tmp_path / "projects" / "Test Project"
    assert result.project_path == project_path
    assert {p.name for p in project_path.iterdir()} == {
        "project.json", "source_index.json", "timeline.json", *PROJECT_DIRECTORIES
    }
    assert all((project_path / name).is_dir() for name in PROJECT_DIRECTORIES)
    metadata = json.loads((project_path / "project.json").read_text(encoding="utf-8"))
    assert metadata["name"] == "Test Project"
    assert metadata["source_folder"] == str(source.resolve())
    assert metadata["resolution"] == {"width": 1920, "height": 1080}
    assert metadata["fps"] == 24
    assert metadata["audio_sample_rate"] == 48000
    assert metadata["project_version"] == "1.0"
    assert datetime.fromisoformat(metadata["created_time"].replace("Z", "+00:00")).tzinfo
    assert Project.model_validate(metadata) == result.project
    assert Timeline.model_validate_json((project_path / "timeline.json").read_text()) == Timeline(project="Test Project")
    assert json.loads((project_path / "source_index.json").read_text()) == {"version": 1, "sources": []}
    assert media.read_bytes() == b"unchanged source"
    assert result.model_dump(mode="json")["project_path"] == str(project_path)


@pytest.mark.parametrize("name", ["", ".", "..", "../escape", "a/b", "a\\b", "C:\\bad", "bad:",
    "bad?", "bad*", "bad|", "bad<", 'bad"', "bad\x00", "bad\n", " name", "name ", "name.",
    "CON", "con.txt", "NUL", "PRN", "AUX", "COM1", "LPT9.txt", "COM¹", "CONOUT$", "a" * 101])
def test_invalid_names_do_not_create_files(tmp_path, name):
    root = tmp_path / "projects"
    result = create_project(name, tmp_path, projects_root=root)
    assert not result.success
    assert result.error.code == "invalid_project_name"
    assert not root.exists()


@pytest.mark.parametrize("kind", ["missing", "file", "empty"])
def test_invalid_source(tmp_path, kind):
    source = tmp_path / "source"
    if kind == "file":
        source.write_text("not a directory")
    result = create_project("Example", "" if kind == "empty" else source, projects_root=tmp_path / "projects")
    assert not result.success
    assert result.error.code == "invalid_source_folder"
    assert not (tmp_path / "projects").exists()


@pytest.mark.parametrize("kind", ["directory", "file"])
def test_existing_project_preserved(tmp_path, kind):
    root = tmp_path / "projects"
    root.mkdir()
    target = root / "Example"
    if kind == "directory":
        target.mkdir()
        sentinel = target / "timeline.json"
    else:
        sentinel = target
    sentinel.write_bytes(b"existing user data")
    result = create_project("Example", tmp_path, projects_root=root)
    assert not result.success and result.error.code == "project_exists"
    assert sentinel.read_bytes() == b"existing user data"


def test_write_failure_rolls_back_only_new_project(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    existing = root / "existing.txt"
    existing.write_text("keep")
    original_open = Path.open

    def failing_open(path, *args, **kwargs):
        if path.name == "timeline.json":
            raise PermissionError("simulated denied write")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)
    result = create_project("Example", tmp_path, projects_root=root)
    assert not result.success and result.error.code == "project_creation_failed"
    assert not (root / "Example").exists()
    assert existing.read_text() == "keep"


def test_project_schema_preserves_windows_path():
    source = r"C:\Users\Editor\Source Media"
    assert Project(name="Example", source_folder=source).model_dump()["source_folder"] == source


@pytest.mark.skipif(__import__("os").name == "nt", reason="Foreign-path behavior on POSIX")
@pytest.mark.parametrize("source", [r"C:\Videos", r"\\server\share\Videos"])
def test_windows_paths_not_misinterpreted_on_linux(tmp_path, source):
    result = create_project("Example", source, projects_root=tmp_path / "projects")
    assert not result.success and result.error.code == "unsupported_path"


@pytest.mark.parametrize("changes", [{"name": "../bad"}, {"fps": 0}, {"audio_sample_rate": -1},
    {"resolution": {"width": 0, "height": 1080}}])
def test_schema_rejects_invalid_metadata(changes):
    values = {"name": "Example", "source_folder": r"C:\Videos"}
    values.update(changes)
    with pytest.raises(ValidationError):
        Project(**values)
