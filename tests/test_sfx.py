import array
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from engine.ffmpeg import run_ffmpeg
from engine.media import probe_media
from engine.render import render_timeline
from engine.timeline import TimelineError, load_timeline, save_timeline, validate_timeline
from schemas.timeline import SFXClip, SFXTrack, VideoClip, VideoTrack
from services.project_service import create_project
from services.sfx_service import add_sfx, remove_sfx, update_sfx, list_sfx_library, search_sfx_by_tags
from services.music_service import add_music


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    root = tmp_path_factory.mktemp("SFX sources ü spaces")
    video = root / "dialogue.mp4"
    run_ffmpeg(["-n", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=24", "-f", "lavfi", "-i",
                "sine=frequency=480:sample_rate=48000", "-t", "3", "-c:v", "mpeg4", "-threads", "1",
                "-c:a", "aac", str(video)])
    effects = []
    for frequency in (1080, 1440, 1920, 960):
        file = root / f"effect {frequency}.wav"
        run_ffmpeg(["-n", "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=44100",
                    "-t", "0.6", "-c:a", "pcm_f32le", str(file)])
        effects.append(file)
    return video, effects


@pytest.fixture
def project(tmp_path, sources):
    video, _ = sources
    result = create_project("SFX Test", video.parent, projects_root=tmp_path / "projects")
    folder = result.project_path
    metadata = result.project.model_dump(mode="json")
    metadata["resolution"] = {"width": 320, "height": 240}
    (folder / "project.json").write_text(json.dumps(metadata), encoding="utf-8")
    timeline = load_timeline(folder)
    timeline.width, timeline.height = 320, 240
    timeline.video_tracks = [VideoTrack(id="video", clips=[VideoClip(id="dialogue", source=str(video), source_out=3)])]
    save_timeline(folder, timeline)
    return folder


def decode(path, stereo=False):
    result = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-vn", "-ac", "2",
                             "-ar", "48000", "-f", "f32le", "-"], shell=False, check=True,
                            capture_output=True, timeout=30)
    samples = array.array("f")
    samples.frombytes(result.stdout)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples if stereo else array.array("f", ((samples[i] + samples[i + 1]) / 2 for i in range(0, len(samples), 2)))


def amplitude(samples, frequency, start, length=0.1):
    segment = samples[round(start * 48000):round((start + length) * 48000)]
    assert segment
    real = sum(value * math.cos(2 * math.pi * frequency * i / 48000) for i, value in enumerate(segment))
    imag = sum(value * math.sin(2 * math.pi * frequency * i / 48000) for i, value in enumerate(segment))
    return 2 * math.hypot(real, imag) / len(segment)


def test_three_simultaneous_effects_preserve_dialogue_and_bgm(project, sources):
    video, effects = sources
    original = [path.read_bytes() for path in (video, *effects)]
    add_music(project, str(effects[3]), loop=True, volume_db=-12)
    baseline = decode(render_timeline(project))
    for effect in effects[:3]:
        add_sfx(project, str(effect), timeline_time=1.2345, volume_db=-6, tags=["impact", "heavy"])
    timeline_bytes = (project / "timeline.json").read_bytes()
    output = render_timeline(project)
    info = probe_media(output)  # Real ffprobe, not mocked metadata.
    assert (info["video_codec"], info["audio_codec"], info["sample_rate"]) == ("h264", "aac", 48000)
    assert (info["width"], info["height"], info["fps"]) == (320, 240, 24)
    assert info["duration"] == pytest.approx(3, abs=0.07)
    samples = decode(output)
    for frequency in (1080, 1440, 1920):
        assert amplitude(samples, frequency, 1.05) < 0.003
        assert amplitude(samples, frequency, 1.4) > 0.035
        assert amplitude(samples, frequency, 2.05) < 0.003
    for frequency in (480, 960):
        assert amplitude(samples, frequency, 1.4) == pytest.approx(amplitude(baseline, frequency, 1.4), rel=0.15)
    assert max(abs(value) for value in decode(output, stereo=True)) < 1
    assert (project / "timeline.json").read_bytes() == timeline_bytes
    assert [path.read_bytes() for path in (video, *effects)] == original
    assert not list((project / "cache").iterdir())


def test_sfx_gain_fades_disabled_and_timeline_end(project, sources):
    added = add_sfx(project, str(sources[1][0]), timeline_time=0.25, source_in=0.1,
                    fade_in=0.1, fade_out=0.1)
    samples = decode(render_timeline(project))
    peak = amplitude(samples, 1080, 0.4)
    assert peak > 0.07
    assert amplitude(samples, 1080, 0.255, 0.025) < peak * 0.4
    assert amplitude(samples, 1080, 0.72, 0.025) < peak * 0.4
    update_sfx(project, added["sfx_id"], volume_db=-6)
    quieter = decode(render_timeline(project))
    assert amplitude(quieter, 1080, 0.4) == pytest.approx(peak * 10 ** (-6 / 20), rel=0.1)
    update_sfx(project, added["sfx_id"], enabled=False)
    assert amplitude(decode(render_timeline(project)), 1080, 0.4) < 0.003
    update_sfx(project, added["sfx_id"], enabled=True, timeline_time=2.8, fade_in=0.2, fade_out=0.2)
    output = render_timeline(project)
    samples = decode(output)
    assert amplitude(samples, 1080, 2.985, 0.01) < amplitude(samples, 1080, 2.88, 0.025)
    assert probe_media(output)["duration"] == pytest.approx(3, abs=0.07)


def test_simultaneous_loud_effects_are_peak_limited(project, sources):
    for file in sources[1][:3]:
        add_sfx(project, str(file), timeline_time=0.25, volume_db=24)
    samples = decode(render_timeline(project), stereo=True)
    assert max(abs(value) for value in samples) < 1


def test_source_in_selects_remaining_audio(project, tmp_path):
    file = tmp_path / "two tones.wav"
    run_ffmpeg(["-n", "-f", "lavfi", "-i", "sine=frequency=720:duration=0.3:sample_rate=48000",
                "-f", "lavfi", "-i", "sine=frequency=1440:duration=0.3:sample_rate=48000",
                "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[a]", "-map", "[a]", "-c:a", "pcm_f32le", str(file)])
    add_sfx(project, str(file), timeline_time=0.5, source_in=0.3)
    samples = decode(render_timeline(project))
    assert amplitude(samples, 1440, 0.6) > 0.07
    assert amplitude(samples, 720, 0.6) < 0.003
    assert amplitude(samples, 1440, 1.2) < 0.003


def test_timestamp_is_rounded_to_audio_sample(project, sources, monkeypatch):
    from engine import audio
    captured = []
    original = audio.run_ffmpeg
    def record(arguments):
        if "-filter_complex" in arguments:
            captured.append(arguments[arguments.index("-filter_complex") + 1])
        return original(arguments)
    monkeypatch.setattr(audio, "run_ffmpeg", record)
    add_sfx(project, str(sources[1][0]), timeline_time=1.2345)
    render_timeline(project)
    assert "adelay=59256S:all=1" in captured[0]


def test_crud_exact_history_and_overlap(project, sources):
    before = (project / "timeline.json").read_bytes()
    added = add_sfx(project, str(sources[1][0]), 0.5, tags=[" Impact ", "heavy", "IMPACT"])
    assert added["sfx"]["tags"] == ["impact", "heavy"]
    assert Path(added["backup_path"]).read_bytes() == before
    add_sfx(project, str(sources[1][1]), 0.5)
    timeline = load_timeline(project)
    assert len(timeline.sfx_tracks[0].clips) == 2
    assert validate_timeline(timeline, base_dir=project).valid
    before = (project / "timeline.json").read_bytes()
    changed = update_sfx(project, added["sfx_id"], timeline_time=0, volume_db=0, enabled=False, tags=[])
    assert Path(changed["backup_path"]).read_bytes() == before
    assert changed["sfx"]["timeline_time"] == 0 and not changed["sfx"]["enabled"] and changed["sfx"]["tags"] == []
    before = (project / "timeline.json").read_bytes()
    removed = remove_sfx(project, added["sfx_id"])
    assert Path(removed["backup_path"]).read_bytes() == before
    assert len(load_timeline(project).sfx_tracks[0].clips) == 1
    assert sources[1][0].is_file()


@pytest.mark.parametrize("changes", [{"timeline_time": -1}, {"source_in": 2}, {"fade_in": 2},
    {"volume_db": float("nan")}, {"tags": [""]}, {"id": "replacement"}])
def test_invalid_updates_preserve_history(project, sources, changes):
    added = add_sfx(project, str(sources[1][0]), 0)
    before = (project / "timeline.json").read_bytes()
    history = set((project / "timeline_history").iterdir())
    with pytest.raises(Exception) as caught:
        update_sfx(project, added["sfx_id"], **changes)
    assert hasattr(caught.value, "to_dict")
    assert (project / "timeline.json").read_bytes() == before
    assert set((project / "timeline_history").iterdir()) == history


def test_invalid_audio_and_missing_event(project, tmp_path):
    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"not audio")
    before = (project / "timeline.json").read_bytes()
    with pytest.raises(Exception) as caught:
        add_sfx(project, str(broken), 0)
    assert caught.value.to_dict()["code"] == "invalid_audio"
    with pytest.raises(TimelineError, match="not found"):
        remove_sfx(project, "missing")
    assert (project / "timeline.json").read_bytes() == before


def test_legacy_sfx_and_missing_file_location(project):
    legacy = SFXClip(id="old", source="old.wav", source_out=1, timeline_start=0.5, volume=0.5, speed=1)
    assert legacy.timeline_time == 0.5 and legacy.volume_db == pytest.approx(-6.0206, abs=0.0001)
    with pytest.raises(ValidationError):
        SFXClip(id="bad", file="x.wav", source_in=1, source_out=0.5)
    timeline = load_timeline(project)
    timeline.sfx_tracks = [SFXTrack(id="sfx", clips=[legacy])]
    result = validate_timeline(timeline, base_dir=project)
    assert not result.valid and result.errors[0].location == ["sfx_tracks", 0, "clips", 0, "file"]


@pytest.fixture
def library(tmp_path, sources):
    root = tmp_path / "library"
    (root / "audio").mkdir(parents=True)
    (root / "audio" / "impact_concrete_03.wav").write_bytes(sources[1][0].read_bytes())
    (root / "audio" / "whoosh.wav").write_bytes(sources[1][1].read_bytes())
    items = [dict(id="impact", file="audio/impact_concrete_03.wav", tags=["impact", "body", "wall", "concrete", "heavy"]),
             dict(id="whoosh", file="audio/whoosh.wav", tags=["whoosh", "heavy"]),
             dict(id="missing", file="audio/missing.wav", tags=["impact"])]
    (root / "library.json").write_text(json.dumps(dict(version=1, items=items)), encoding="utf-8")
    return root


def test_library_and_case_insensitive_all_any_search(library):
    catalog = list_sfx_library(library_root=library)
    assert len(catalog["items"]) == 3 and len(catalog["errors"]) == 1
    assert catalog["errors"][0]["code"] == "missing_sfx_asset"
    result = search_sfx_by_tags([" IMPACT ", "HEAVY", "impact"], library_root=library)
    assert [item["id"] for item in result["items"]] == ["impact"]
    assert Path(result["items"][0]["file"]).is_file()
    assert len(search_sfx_by_tags(["impact", "whoosh"], match_all=False, library_root=library)["items"]) == 2
    assert not search_sfx_by_tags(["unknown"], library_root=library)["items"]
    with pytest.raises(TimelineError) as caught:
        search_sfx_by_tags([], library_root=library)
    assert caught.value.code == "invalid_tag_query"


@pytest.mark.parametrize("file", ["../outside.wav", "D:\\outside.wav", "/outside.wav", "audio/../../outside.wav"])
def test_catalog_paths_cannot_escape_library(library, file):
    (library / "library.json").write_text(json.dumps(dict(version=1, items=[dict(id="bad", file=file)])))
    with pytest.raises(TimelineError) as caught:
        list_sfx_library(library_root=library)
    assert caught.value.code == "invalid_sfx_library"


def test_malformed_catalog(library):
    (library / "library.json").write_text("not json")
    with pytest.raises(TimelineError):
        list_sfx_library(library_root=library)


def test_sfx_encoder_failure_keeps_preview_and_cleans_cache(project, sources, monkeypatch):
    from engine.ffmpeg import FFmpegError
    from engine.render import RenderError
    add_sfx(project, str(sources[1][0]), 0.5)
    preview = project / "preview" / "preview.mp4"
    preview.write_bytes(b"previous preview")
    def fail(arguments):
        Path(arguments[-1]).write_bytes(b"partial output")
        raise FFmpegError("ffmpeg_failed", "SFX preparation failed", stderr="useful diagnostic")
    monkeypatch.setattr("engine.audio.run_ffmpeg", fail)
    with pytest.raises(RenderError) as caught:
        render_timeline(project)
    assert caught.value.details["stderr"] == "useful diagnostic"
    assert preview.read_bytes() == b"previous preview"
    assert not list((project / "cache").iterdir())
