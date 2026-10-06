import json
from pathlib import Path

import pytest

from engine.ffmpeg import run_ffmpeg
from engine.timeline import TimelineError, load_timeline, save_timeline, timeline_lock
from services import timeline_service as edits
from services.project_service import create_project


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    root = tmp_path_factory.mktemp("editing source ü")
    path = root / "video.mp4"
    run_ffmpeg(["-n", "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=24", "-t", "3",
                "-c:v", "mpeg4", "-threads", "1", str(path)])
    return path


@pytest.fixture
def project(tmp_path, source):
    return create_project("Edit Test", source.parent, projects_root=tmp_path / "projects").project_path


def history(project):
    return sorted((project / "timeline_history").glob("*.json"))


def test_all_operations_snapshot_exact_previous_timeline(project, source):
    source_before = source.read_bytes()
    preview = project / "preview" / "preview.mp4"
    preview.write_bytes(b"untouched render")
    original = (project / "timeline.json").read_bytes()
    added = edits.add_clip(project, str(source), 0, 2, 0)
    clip_id = added["clip_id"]
    assert Path(added["backup_path"]).read_bytes() == original
    actions = [
        lambda: edits.trim_clip(project, clip_id, 0.25, 1.25),
        lambda: edits.move_clip(project, clip_id, 4),
        lambda: edits.set_clip_speed(project, clip_id, 2),
        lambda: edits.remove_clip(project, clip_id),
    ]
    for action in actions:
        previous = (project / "timeline.json").read_bytes()
        result = action()
        assert result["summary"] and result["revision"]
        assert Path(result["backup_path"]).read_bytes() == previous
        assert json.loads((project / "timeline.json").read_text())["version"] == 1
        json.dumps(result, allow_nan=False)
    assert len(history(project)) == 5
    assert not load_timeline(project).video_tracks[0].clips
    assert source.read_bytes() == source_before
    assert preview.read_bytes() == b"untouched render"
    assert not (project / ".timeline.lock").exists()


def test_positions_are_seconds_and_other_clips_do_not_ripple(project, source):
    first = edits.add_clip(project, str(source), 0, 1, 0)["clip_id"]
    second = edits.add_clip(project, str(source), 0, 1, 5.5)["clip_id"]
    edits.move_clip(project, first, 2.25)
    edits.trim_clip(project, first, 0, 0.5)
    edits.set_clip_speed(project, first, 2)
    clips = {clip.id: clip for clip in load_timeline(project).video_tracks[0].clips}
    assert clips[first].timeline_start == 2.25 and clips[first].duration == 0.25
    assert clips[second].timeline_start == 5.5
    edits.remove_clip(project, first)
    assert load_timeline(project).video_tracks[0].clips[0].timeline_start == 5.5


@pytest.mark.parametrize("operation", ["add", "move", "trim", "speed"])
def test_overlaps_rejected_without_backup_or_write(project, source, operation):
    first = edits.add_clip(project, str(source), 0, 1, 0)["clip_id"]
    second = edits.add_clip(project, str(source), 0, 1, 1)["clip_id"]
    previous, count = (project / "timeline.json").read_bytes(), len(history(project))
    actions = {
        "add": lambda: edits.add_clip(project, str(source), 0, 1, 0.5),
        "move": lambda: edits.move_clip(project, second, 0.5),
        "trim": lambda: edits.trim_clip(project, first, 0, 2),
        "speed": lambda: edits.set_clip_speed(project, first, 0.5),
    }
    with pytest.raises(TimelineError):
        actions[operation]()
    assert (project / "timeline.json").read_bytes() == previous
    assert len(history(project)) == count
    assert not (project / ".timeline.lock").exists()


@pytest.mark.parametrize("operation", ["trim", "move", "speed", "remove"])
def test_missing_clip_structured_error(project, operation):
    actions = {
        "trim": lambda: edits.trim_clip(project, "missing", 0, 1),
        "move": lambda: edits.move_clip(project, "missing", 1),
        "speed": lambda: edits.set_clip_speed(project, "missing", 2),
        "remove": lambda: edits.remove_clip(project, "missing"),
    }
    with pytest.raises(TimelineError) as caught:
        actions[operation]()
    assert caught.value.to_dict()["code"] == "clip_not_found"
    assert not history(project)


@pytest.mark.parametrize("case", ["negative_position", "bad_trim", "outside_source", "missing_source", "zero_speed", "nan_speed"])
def test_invalid_edits_leave_existing_timeline_unchanged(project, source, case):
    clip_id = edits.add_clip(project, str(source), 0, 1, 0)["clip_id"]
    previous, count = (project / "timeline.json").read_bytes(), len(history(project))
    actions = {
        "negative_position": lambda: edits.move_clip(project, clip_id, -1),
        "bad_trim": lambda: edits.trim_clip(project, clip_id, 1, 1),
        "outside_source": lambda: edits.trim_clip(project, clip_id, 0, 10),
        "missing_source": lambda: edits.add_clip(project, str(source.with_name("missing.mp4")), 0, 1, 2),
        "zero_speed": lambda: edits.set_clip_speed(project, clip_id, 0),
        "nan_speed": lambda: edits.set_clip_speed(project, clip_id, float("nan")),
    }
    with pytest.raises(Exception) as caught:
        actions[case]()
    assert callable(getattr(caught.value, "to_dict", None))
    assert (project / "timeline.json").read_bytes() == previous
    assert len(history(project)) == count


def test_failed_replace_and_failed_backup_do_not_commit(project, source, monkeypatch):
    previous = (project / "timeline.json").read_bytes()

    def deny_replace(*args, **kwargs):
        raise PermissionError("simulated atomic replace failure")

    monkeypatch.setattr(Path, "replace", deny_replace)
    with pytest.raises(TimelineError):
        edits.add_clip(project, str(source), 0, 1, 0)
    assert (project / "timeline.json").read_bytes() == previous
    assert not history(project)
    assert not list(project.glob(".timeline-*.tmp"))
    monkeypatch.undo()
    # A file at the history directory path prevents backups: no save is allowed.
    (project / "timeline_history").rmdir()
    (project / "timeline_history").write_bytes(b"keep this unrelated file")
    with pytest.raises(TimelineError):
        edits.add_clip(project, str(source), 0, 1, 0)
    assert (project / "timeline.json").read_bytes() == previous
    assert (project / "timeline_history").read_bytes() == b"keep this unrelated file"


def test_busy_lock_prevents_lost_update(project, source):
    previous = (project / "timeline.json").read_bytes()
    with timeline_lock(project):
        with pytest.raises(TimelineError) as caught:
            edits.add_clip(project, str(source), 0, 1, 0)
        assert caught.value.code == "timeline_busy"
        assert (project / ".timeline.lock").exists()
    assert (project / "timeline.json").read_bytes() == previous
    assert not history(project)


def test_every_direct_save_also_has_history(project):
    timeline = load_timeline(project)
    previous = (project / "timeline.json").read_bytes()
    save_timeline(project, timeline)
    assert len(history(project)) == 1
    assert history(project)[0].read_bytes() == previous
