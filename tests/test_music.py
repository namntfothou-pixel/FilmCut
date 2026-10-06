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
from engine.timeline import load_timeline, save_timeline, validate_timeline
from schemas.timeline import MusicClip, MusicTrack, VideoClip, VideoTrack
from services.music_service import add_music, remove_music, update_music
from services.project_service import create_project


@pytest.fixture(scope="module")
def music_sources(tmp_path_factory):
    root = tmp_path_factory.mktemp("music sources ü ' spaces")
    video, music = root / "dialogue.mp4", root / "short BGM.wav"
    run_ffmpeg(["-n", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=24", "-f", "lavfi", "-i",
                "sine=frequency=480:sample_rate=48000", "-t", "3", "-c:v", "mpeg4", "-threads", "1",
                "-c:a", "aac", str(video)])
    run_ffmpeg(["-n", "-f", "lavfi", "-i", "sine=frequency=960:sample_rate=44100", "-t", "0.5",
                "-af", "volume=4", "-c:a", "pcm_f32le", str(music)])
    return video, music


@pytest.fixture
def music_project(tmp_path, music_sources):
    video, _ = music_sources
    result = create_project("Music Test", video.parent, projects_root=tmp_path / "projects")
    folder = result.project_path
    metadata = result.project.model_dump(mode="json")
    metadata["resolution"] = {"width": 320, "height": 240}
    (folder / "project.json").write_text(json.dumps(metadata))
    timeline = load_timeline(folder)
    timeline.width, timeline.height = 320, 240
    timeline.video_tracks = [VideoTrack(id="video", clips=[VideoClip(id="dialogue", source=str(video), source_out=3)])]
    save_timeline(folder, timeline)
    return folder


def decode_audio(path, *, stereo=False):
    result = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-vn", "-ac", "2",
                             "-ar", "48000", "-f", "f32le", "-"], shell=False, check=True,
                            capture_output=True, timeout=30)
    samples = array.array("f")
    samples.frombytes(result.stdout)
    if sys.byteorder != "little":
        samples.byteswap()
    # Inspect actual stereo samples for clipping. For frequency analysis average
    # channels explicitly; FFmpeg's default mono downmix can add 3 dB.
    return samples if stereo else array.array("f", ((samples[index] + samples[index + 1]) / 2 for index in range(0, len(samples), 2)))


def amplitude(samples, frequency, start, length=0.1):
    segment = samples[round(start * 48000):round((start + length) * 48000)]
    assert len(segment) > 0
    real = sum(value * math.cos(2 * math.pi * frequency * index / 48000) for index, value in enumerate(segment))
    imag = sum(value * math.sin(2 * math.pi * frequency * index / 48000) for index, value in enumerate(segment))
    return 2 * math.hypot(real, imag) / len(segment)


def test_source_audio_plus_looping_bgm_real_preview(music_project, music_sources):
    video, music = music_sources
    original = [path.read_bytes() for path in music_sources]
    baseline_path = render_timeline(music_project)
    baseline = decode_audio(baseline_path)
    added = add_music(music_project, str(music), timeline_start=0.5, volume_db=-6,
                      fade_in=0.25, fade_out=0.25, loop=True)
    before = (music_project / "timeline.json").read_bytes()
    output = render_timeline(music_project)
    info = probe_media(output)
    assert info["video_codec"] == "h264" and info["audio_codec"] == "aac"
    assert info["sample_rate"] == 48000
    assert (info["width"], info["height"], info["fps"]) == (320, 240, 24)
    assert info["duration"] == pytest.approx(3, abs=0.07)
    samples = decode_audio(output)
    assert amplitude(samples, 480, 1.5) == pytest.approx(amplitude(baseline, 480, 1.5), rel=0.1)
    assert amplitude(samples, 960, 0.2) < 0.005  # Timeline placement.
    assert amplitude(samples, 960, 1.5) > 0.1  # Short source has repeated.
    assert amplitude(samples, 960, 2.5) > 0.1  # Loop continues near video end.
    assert amplitude(samples, 960, 0.51, 0.04) < amplitude(samples, 960, 1.5) * 0.3
    assert amplitude(samples, 960, 2.95, 0.025) < amplitude(samples, 960, 1.5) * 0.3
    assert max(abs(sample) for sample in samples) < 1
    assert (music_project / "timeline.json").read_bytes() == before
    assert [path.read_bytes() for path in music_sources] == original
    assert Path(added["backup_path"]).is_file()
    assert not list((music_project / "cache").iterdir())


def test_nonloop_stop_db_gain_and_disabled_music(music_project, music_sources):
    _, music = music_sources
    result = add_music(music_project, str(music), timeline_start=0.25, source_in=0.1, source_out=0.4, volume_db=-6)
    samples = decode_audio(render_timeline(music_project))
    initial = amplitude(samples, 960, 0.3)
    assert initial > 0.1
    assert amplitude(samples, 960, 1.5) < 0.005
    update_music(music_project, result["music_id"], volume_db=-12)
    quieter = decode_audio(render_timeline(music_project))
    assert amplitude(quieter, 960, 0.3) == pytest.approx(initial * 10 ** (-6 / 20), rel=0.1)
    update_music(music_project, result["music_id"], enabled=False)
    disabled = decode_audio(render_timeline(music_project))
    assert amplitude(disabled, 960, 0.3) < 0.005
    assert amplitude(disabled, 480, 0.3) > 0.05


def test_high_gain_mix_is_peak_limited(music_project, music_sources):
    add_music(music_project, str(music_sources[1]), volume_db=24, loop=True)
    samples = decode_audio(render_timeline(music_project), stereo=True)
    assert max(abs(sample) for sample in samples) < 1.0
    assert sum(abs(sample) > 0.99 for sample in samples) == 0


def test_music_is_cut_and_faded_at_video_end(music_project, music_sources):
    add_music(music_project, str(music_sources[1]), timeline_start=2.8, source_out=0.5,
              fade_in=0.2, fade_out=0.2, volume_db=-6)
    output = render_timeline(music_project)
    assert probe_media(output)["duration"] == pytest.approx(3, abs=0.07)
    samples = decode_audio(output)
    assert amplitude(samples, 960, 2.98, 0.015) < amplitude(samples, 960, 2.88, 0.03)


def test_loop_repeats_selected_trim_segment(music_project, tmp_path):
    # Distinct tones in the file; only the latter half is selected for looping.
    file = tmp_path / "two tones.wav"
    run_ffmpeg(["-n", "-f", "lavfi", "-i", "sine=frequency=720:duration=0.5:sample_rate=48000",
                "-f", "lavfi", "-i", "sine=frequency=1440:duration=0.5:sample_rate=48000",
                "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[a]", "-map", "[a]", "-c:a", "pcm_f32le", str(file)])
    add_music(music_project, str(file), source_in=0.5, source_out=1, volume_db=0, loop=True)
    samples = decode_audio(render_timeline(music_project))
    assert amplitude(samples, 1440, 2.2) > 0.05
    assert amplitude(samples, 720, 2.2) < 0.005


def test_music_crud_history_and_canonical_fields(music_project, music_sources):
    previous = (music_project / "timeline.json").read_bytes()
    added = add_music(music_project, str(music_sources[1]), loop=True, fade_in=0.3, fade_out=0.3)
    assert Path(added["backup_path"]).read_bytes() == previous
    item = added["music"]
    assert set(item) == {"id", "file", "timeline_start", "end", "source_in", "source_out", "volume_db", "fade_in", "fade_out", "loop", "enabled"}
    assert item['end'] is None
    assert item["source_out"] == pytest.approx(0.5, abs=0.001)
    previous = (music_project / "timeline.json").read_bytes()
    updated = update_music(music_project, added["music_id"], volume_db=-24, enabled=False)
    assert updated["music"]["volume_db"] == -24 and not updated["music"]["enabled"]
    assert Path(updated["backup_path"]).read_bytes() == previous
    remove_music(music_project, added["music_id"])
    assert not load_timeline(music_project).music_tracks[0].clips
    assert music_sources[1].is_file()


@pytest.mark.parametrize("changes", [{"volume_db": float("nan")}, {"volume_db": 100}, {"fade_in": -1},
    {"source_out": 0}, {"timeline_start": -1}, {"fade_in": 1}])
def test_invalid_music_values(changes):
    values = {"id": "music", "file": "track.wav", "source_out": 0.5, **changes}
    with pytest.raises(ValidationError):
        MusicClip(**values)


def test_invalid_source_and_invalid_update_preserve_timeline(music_project, music_sources, tmp_path):
    previous = (music_project / "timeline.json").read_bytes()
    invalid = tmp_path / "broken.wav"
    invalid.write_bytes(b"not music")
    with pytest.raises(Exception) as caught:
        add_music(music_project, str(invalid))
    assert hasattr(caught.value, "to_dict")
    assert (music_project / "timeline.json").read_bytes() == previous
    added = add_music(music_project, str(music_sources[1]))
    previous = (music_project / "timeline.json").read_bytes()
    with pytest.raises(Exception):
        update_music(music_project, added["music_id"], source_out=2)
    assert (music_project / "timeline.json").read_bytes() == previous


def test_legacy_music_fields_and_loop_overlap():
    legacy = MusicClip(id="old", source="track.wav", source_out=2, volume=0.5, speed=1)
    assert legacy.file == "track.wav" and legacy.volume_db == pytest.approx(-6.0206, abs=0.0001)
    assert "source" not in legacy.model_dump() and "volume" not in legacy.model_dump()
    with pytest.raises(ValidationError, match="prohibited overlap"):
        from schemas.timeline import Timeline
        Timeline(project="Example", video_tracks=[VideoTrack(id="video", clips=[VideoClip(id="v", source="v.mp4", source_out=10)])],
                 music_tracks=[MusicTrack(id="music", clips=[MusicClip(id="a", file="a.wav", source_out=1, loop=True),
                                                            MusicClip(id="b", file="b.wav", source_out=1, timeline_start=2)])])


def test_missing_music_file_has_precise_validation_location(music_project):
    timeline = load_timeline(music_project)
    timeline.music_tracks = [MusicTrack(id="music", clips=[MusicClip(id="missing", file="missing.wav", source_out=1)])]
    result = validate_timeline(timeline, base_dir=music_project)
    assert not result.valid
    assert result.errors[0].location == ["music_tracks", 0, "clips", 0, "file"]


def test_failed_music_render_preserves_previous_preview_and_cleans_cache(music_project, music_sources, monkeypatch):
    from engine.ffmpeg import FFmpegError
    from engine.render import RenderError
    add_music(music_project, str(music_sources[1]), loop=True)
    preview = music_project / "preview" / "preview.mp4"
    preview.write_bytes(b"previous valid preview")

    def fail(arguments):
        Path(arguments[-1]).write_bytes(b"partial music output")
        raise FFmpegError("ffmpeg_failed", "simulated audio encoder failure", stderr="music diagnostic")

    monkeypatch.setattr("engine.audio.run_ffmpeg", fail)
    with pytest.raises(RenderError) as caught:
        render_timeline(music_project)
    assert caught.value.details["stderr"] == "music diagnostic"
    assert preview.read_bytes() == b"previous valid preview"
    assert not list((music_project / "cache").iterdir())


def test_update_cannot_change_music_identity(music_project, music_sources):
    from engine.timeline import TimelineError
    added = add_music(music_project, str(music_sources[1]))
    previous = (music_project / "timeline.json").read_bytes()
    with pytest.raises(TimelineError) as caught:
        update_music(music_project, added["music_id"], id="replacement")
    assert caught.value.code == "invalid_music_update"
    assert (music_project / "timeline.json").read_bytes() == previous
