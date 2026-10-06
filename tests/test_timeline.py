import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from engine.timeline import (
    TimelineError, create_empty_timeline, load_timeline, save_timeline, validate_timeline,
)
from schemas.project import Project, Resolution
from schemas.timeline import (
    AudioClip, AudioTrack, MusicClip, MusicTrack, SFXClip, SFXTrack, SubtitleCue,
    SubtitleTrack, Timeline, Transition, VideoClip, VideoTrack,
)
from services.project_service import create_project


@pytest.fixture
def project(tmp_path):
    source = tmp_path / "sources"
    source.mkdir()
    (source / "clip.mp4").write_bytes(b"source contents; no decoding required")
    result = create_project("Timeline Test", source, projects_root=tmp_path / "projects")
    assert result.success
    return result.project_path, source / "clip.mp4"


def clip(source, **changes):
    fields = dict(id="clip1", source=str(source), source_in=1, source_out=5, timeline_start=0)
    fields.update(changes)
    return VideoClip(**fields)


def test_empty_timeline_uses_project_settings():
    metadata = Project(name="Example", source_folder=r"C:\Videos", fps=30,
                       resolution=Resolution(width=1280, height=720))
    timeline = create_empty_timeline(metadata)
    assert (timeline.project, timeline.fps, timeline.width, timeline.height) == ("Example", 30, 1280, 720)
    assert all(not getattr(timeline, name) for name in (
        "video_tracks", "audio_tracks", "music_tracks", "sfx_tracks", "subtitle_tracks"))
    assert validate_timeline(timeline).valid


def test_new_project_has_valid_full_timeline(project):
    directory, _ = project
    assert load_timeline(directory) == create_empty_timeline(directory)


def test_all_models_roundtrip(project):
    directory, source = project
    timeline = Timeline(
        project="Timeline Test", video_tracks=[VideoTrack(id="video", clips=[clip(source)])],
        audio_tracks=[AudioTrack(id="dialogue", clips=[AudioClip(id="speech", source=str(source), source_out=2)])],
        music_tracks=[MusicTrack(id="music", clips=[MusicClip(id="song", source=str(source), source_out=4, volume=0.3)])],
        sfx_tracks=[SFXTrack(id="sfx", clips=[SFXClip(id="sound", source=str(source), source_out=1)])],
        subtitle_tracks=[SubtitleTrack(id="subtitles", language="en", cues=[
            SubtitleCue(id="caption", text="Hello ü", timeline_start=0, timeline_end=2)])],
    )
    destination = save_timeline(directory, timeline)
    contents = destination.read_text(encoding="utf-8")
    assert '\n  "project"' in contents and contents.endswith("\n") and "Hello ü" in contents
    assert load_timeline(directory) == timeline
    assert Timeline.model_validate_json(contents) == timeline
    assert source.read_bytes() == b"source contents; no decoding required"
    assert "duration" not in timeline.video_tracks[0].clips[0].model_dump()


@pytest.mark.parametrize("changes", [
    {"source_out": 1}, {"source_out": 0.5}, {"source_in": -1}, {"source_out": -1},
    {"timeline_start": -1}, {"speed": 0}, {"speed": -1}, {"speed": float("nan")},
    {"speed": float("inf")}, {"volume": -1}, {"timeline_start": float("nan")},
])
def test_invalid_clip_values_rejected(project, changes):
    with pytest.raises(ValidationError):
        clip(project[1], **changes)


@pytest.mark.parametrize("changes", [{"width": 0}, {"height": -1}, {"width": 1.5},
    {"fps": 0}, {"fps": float("inf")}, {"version": 2}, {"filter_complex": "forbidden"}])
def test_invalid_timeline_values(changes):
    result = validate_timeline({"project": "Example", **changes})
    assert not result.valid and result.errors
    assert result.model_dump(mode="json")["errors"][0]["location"]


def test_missing_source_and_directory_source(project):
    directory, source = project
    for path in (source.with_name("missing.mp4"), source.parent):
        timeline = Timeline(project="Timeline Test", video_tracks=[VideoTrack(id="video", clips=[clip(path)])])
        result = validate_timeline(timeline)
        assert not result.valid and result.errors[0].code == "missing_source"
        previous = (directory / "timeline.json").read_bytes()
        with pytest.raises(TimelineError) as caught:
            save_timeline(directory, timeline)
        assert caught.value.to_dict()["code"] == "invalid_timeline"
        assert (directory / "timeline.json").read_bytes() == previous


def test_disabled_clip_still_requires_existing_source(project):
    timeline = Timeline(project="Timeline Test", video_tracks=[VideoTrack(id="video", clips=[
        clip(project[1].with_name("missing"), enabled=False)])])
    assert not validate_timeline(timeline).valid


def test_relative_sources_resolve_from_project_folder(project, monkeypatch):
    directory, source = project
    relative = os.path.relpath(source, directory)
    timeline = Timeline(project="Timeline Test", video_tracks=[VideoTrack(id="video", clips=[clip(relative)])])
    monkeypatch.chdir(source.parent)
    save_timeline(directory, timeline)
    assert load_timeline(directory) == timeline


@pytest.mark.parametrize("track_class,clip_class,field", [
    (VideoTrack, VideoClip, "video_tracks"), (AudioTrack, AudioClip, "audio_tracks"),
    (MusicTrack, MusicClip, "music_tracks"),
])
def test_same_track_overlaps_rejected(project, track_class, clip_class, field):
    first = (MusicClip(id="clip1", file=str(project[1]), source_in=1, source_out=3)
             if clip_class is MusicClip else clip_class(**clip(project[1], speed=2).model_dump()))
    second = clip_class(**clip(project[1], id="clip2", timeline_start=1.9).model_dump())
    with pytest.raises(ValidationError, match="prohibited overlap"):
        Timeline(project="Timeline Test", **{field: [track_class(id="track", clips=[first, second])]})


def test_touching_clips_speed_and_disabled_overlap(project):
    first = clip(project[1], speed=2)
    assert first.duration == 2 and first.timeline_end == 2
    second = clip(project[1], id="clip2", timeline_start=2)
    disabled = clip(project[1], id="clip3", enabled=False)
    timeline = Timeline(project="Timeline Test", video_tracks=[VideoTrack(id="video", clips=[second, first, disabled])])
    assert validate_timeline(timeline).valid


def test_cross_track_and_sfx_overlaps_allowed(project):
    source = project[1]
    timeline = Timeline(project="Timeline Test", video_tracks=[
        VideoTrack(id="video1", clips=[clip(source)]),
        VideoTrack(id="video2", clips=[clip(source, id="clip2")])],
        sfx_tracks=[SFXTrack(id="sfx", clips=[
            SFXClip(id="fx1", source=str(source), source_out=2),
            SFXClip(id="fx2", source=str(source), source_out=2)])])
    assert validate_timeline(timeline).valid


def test_subtitle_overlap_rejected():
    with pytest.raises(ValidationError, match="prohibited overlap"):
        Timeline(project="Example", subtitle_tracks=[SubtitleTrack(id="sub", cues=[
            SubtitleCue(id="a", text="First", timeline_start=0, timeline_end=2),
            SubtitleCue(id="b", text="Second", timeline_start=1, timeline_end=3)])])


def test_transition_serialization(project):
    source = project[1]
    transition = Transition(id="transition", from_clip="clip1", to_clip="clip2", duration=0.5)
    timeline = Timeline(project="Timeline Test", video_tracks=[VideoTrack(id="video", clips=[
        clip(source), clip(source, id="clip2", timeline_start=4)], transitions=[transition])])
    save_timeline(project[0], timeline)
    assert load_timeline(project[0]) == timeline


@pytest.mark.parametrize("changes", [{"to_clip": "missing"}, {"from_clip": "clip2"}, {"duration": 5}])
def test_invalid_transitions(project, changes):
    values = dict(id="transition", from_clip="clip1", to_clip="clip2", duration=1)
    values.update(changes)
    with pytest.raises(ValidationError):
        Timeline(project="Timeline Test", video_tracks=[VideoTrack(id="video", clips=[
            clip(project[1]), clip(project[1], id="clip2", timeline_start=4)],
            transitions=[Transition(**values)])])


def test_duplicate_identifiers(project):
    with pytest.raises(ValidationError, match="duplicate"):
        Timeline(project="Timeline Test", video_tracks=[VideoTrack(id="video", clips=[
            clip(project[1]), clip(project[1], timeline_start=4)])])


def test_mutated_model_is_revalidated(project):
    timeline = Timeline(project="Timeline Test", video_tracks=[VideoTrack(id="video", clips=[clip(project[1])])])
    timeline.video_tracks[0].clips[0].speed = 0
    assert not validate_timeline(timeline).valid


def test_project_mismatch_is_not_saved(project):
    with pytest.raises(TimelineError) as caught:
        save_timeline(project[0], Timeline(project="Other"))
    assert caught.value.code == "project_mismatch"


def test_malformed_json_and_legacy_placeholder_not_modified(project):
    directory, _ = project
    for text in ("{broken", '{"version": 1, "tracks": []}'):
        path = directory / "timeline.json"
        path.write_text(text)
        with pytest.raises(TimelineError):
            load_timeline(directory)
        assert path.read_text() == text


def test_failed_replace_preserves_timeline(project, monkeypatch):
    directory, _ = project
    original = (directory / "timeline.json").read_bytes()

    def deny(*args, **kwargs):
        raise PermissionError("simulated denial")

    monkeypatch.setattr(Path, "replace", deny)
    with pytest.raises(TimelineError) as caught:
        save_timeline(directory, create_empty_timeline(directory))
    assert caught.value.code == "timeline_write_failed"
    assert (directory / "timeline.json").read_bytes() == original
    assert not list(directory.glob(".timeline-*.tmp"))


@pytest.mark.skipif(os.name == "nt", reason="foreign Windows paths on POSIX")
def test_windows_path_preserved_and_reported():
    source = r"C:\Source Media\clip.mp4"
    timeline = Timeline(project="Example", video_tracks=[VideoTrack(id="video", clips=[clip(source)])])
    assert json.loads(timeline.model_dump_json())["video_tracks"][0]["clips"][0]["source"] == source
    assert validate_timeline(timeline).errors[0].code == "unsupported_path"


@pytest.mark.parametrize("kind", ["gap", "duplicate", "consumed"])
def test_transition_boundary_conflicts(project, kind):
    source = project[1]
    clips = [clip(source), clip(source, id="clip2", timeline_start=4)]
    transitions = [Transition(id="t1", from_clip="clip1", to_clip="clip2", duration=3)]
    if kind == "gap":
        clips[1].timeline_start = 5
    elif kind == "duplicate":
        transitions.append(Transition(id="t2", from_clip="clip1", to_clip="clip2", duration=0.5))
    else:
        clips.append(clip(source, id="clip3", timeline_start=8))
        transitions.append(Transition(id="t2", from_clip="clip2", to_clip="clip3", duration=3))
    with pytest.raises(ValidationError):
        Timeline(project="Timeline Test", video_tracks=[VideoTrack(id="video", clips=clips, transitions=transitions)])


def test_invalid_project_reports_structured_error(tmp_path):
    with pytest.raises(TimelineError) as caught:
        load_timeline(tmp_path / "missing")
    assert caught.value.to_dict()["code"] == "invalid_project"


@pytest.mark.parametrize("start,end", [(-1, 2), (2, 2), (3, 2)])
def test_invalid_subtitle_timing(start, end):
    with pytest.raises(ValidationError):
        SubtitleCue(id="cue", text="Caption", timeline_start=start, timeline_end=end)
