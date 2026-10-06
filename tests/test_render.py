import hashlib
import json
import struct
import subprocess
from pathlib import Path

import pytest

from engine.ffmpeg import FFmpegError, run_ffmpeg
from engine.media import probe_media
from engine.render import RenderError, render_clip, render_timeline, render_video_track
from engine.timeline import load_timeline, save_timeline
from schemas.timeline import AudioClip, AudioTrack, VideoClip, VideoTrack
from services.project_service import create_project


@pytest.fixture(scope="module")
def render_sources(tmp_path_factory):
    root = tmp_path_factory.mktemp("render sources ü ' spaces")
    wide = root / "wide red.mp4"
    tall = root / "tall blue.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-n", "-f", "lavfi", "-i",
                    "color=c=red:s=320x180:r=30", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=44100", "-t", "2", "-c:v", "mpeg4",
                    "-threads", "1", "-c:a", "aac", str(wide)], check=True, capture_output=True, timeout=30)
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-n", "-f", "lavfi", "-i",
                    "color=c=blue:s=120x240:r=15", "-t", "2", "-c:v", "mpeg4",
                    "-threads", "1", str(tall)], check=True, capture_output=True, timeout=30)
    return wide, tall


@pytest.fixture
def render_project(tmp_path, render_sources):
    result = create_project("Render Test", render_sources[0].parent, projects_root=tmp_path / "projects")
    folder = result.project_path
    metadata_path = folder / "project.json"
    metadata = json.loads(metadata_path.read_text())
    metadata["resolution"] = {"width": 320, "height": 240}
    metadata_path.write_text(json.dumps(metadata))
    timeline = load_timeline(folder)
    timeline.width, timeline.height = 320, 240
    timeline.video_tracks = [VideoTrack(id="video", clips=[
        VideoClip(id="wide", source=str(render_sources[0]), source_in=0.5, source_out=1.5),
        VideoClip(id="tall", source=str(render_sources[1]), source_in=0.5, source_out=1.5, timeline_start=1),
        VideoClip(id="disabled", source=str(render_sources[0]), source_out=1, enabled=False)])]
    save_timeline(folder, timeline)
    return folder


def frame(path, time):
    result = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", str(time), "-i", str(path),
                             "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-threads", "1", "-"],
                            check=True, capture_output=True, timeout=30)
    assert len(result.stdout) == 320 * 240 * 3
    return result.stdout


def pixel(frame_bytes, x, y):
    offset = (y * 320 + x) * 3
    return tuple(frame_bytes[offset:offset + 3])


def audio_energy(path, start):
    result = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", str(start), "-i", str(path),
                             "-t", "0.2", "-vn", "-ac", "1", "-f", "s16le", "-"],
                            check=True, capture_output=True, timeout=30)
    samples = struct.unpack(f"<{len(result.stdout) // 2}h", result.stdout)
    assert samples
    return sum(abs(sample) for sample in samples) / len(samples)


def test_end_to_end_preview(render_project, render_sources):
    original_hashes = [hashlib.sha256(path.read_bytes()).digest() for path in render_sources]
    timeline_before = (render_project / "timeline.json").read_bytes()
    sentinel = render_project / "cache" / "unrelated.txt"
    sentinel.write_text("keep this")
    output = render_timeline(render_project)
    assert output == render_project / "preview" / "preview.mp4"
    info = probe_media(output)
    assert (info["width"], info["height"], info["fps"]) == (320, 240, 24)
    assert info["video_codec"] == "h264" and info["audio_codec"] == "aac"
    assert info["sample_rate"] == 48000
    assert info["duration"] == pytest.approx(2, abs=1 / 24 + 0.03)
    first, second = frame(output, 0.3), frame(output, 1.3)
    assert max(pixel(first, 160, 10)) < 10  # Wide source: letterbox.
    assert pixel(first, 160, 120)[0] > 200
    assert max(pixel(second, 10, 120)) < 10  # Tall source: pillarbox.
    assert pixel(second, 160, 120)[2] > 200
    assert audio_energy(output, 0.3) > 100
    assert audio_energy(output, 1.3) < 5
    assert (render_project / "timeline.json").read_bytes() == timeline_before
    assert [hashlib.sha256(path.read_bytes()).digest() for path in render_sources] == original_hashes
    assert list((render_project / "cache").iterdir()) == [sentinel]
    assert not list((render_project / "output").iterdir())
    # A subsequent preview can replace the previous one safely.
    assert render_timeline(render_project) == output


def test_render_clip_and_video_track(render_project):
    track = load_timeline(render_project).video_tracks[0]
    single = render_clip(render_project, track.clips[1])
    assert single.parent == render_project / "cache"
    assert probe_media(single)["duration"] == pytest.approx(1, abs=0.05)
    joined = render_video_track(render_project, track)
    assert joined.parent == render_project / "cache"
    assert probe_media(joined)["duration"] == pytest.approx(2, abs=0.08)
    assert set((render_project / "cache").iterdir()) == {single, joined}


@pytest.mark.parametrize("change,code", [("speed", "unsupported_speed"), ("gap", "unsupported_placement"),
    ("audio", "unsupported_tracks"), ("multi", "unsupported_track_count"),
    ("empty", "unsupported_track_count"), ("settings", "settings_mismatch")])
def test_unsupported_intent_is_explicit(render_project, change, code):
    timeline = load_timeline(render_project)
    if change == "speed":
        timeline.video_tracks[0].clips[0].speed = 2
        timeline.video_tracks[0].clips[1].timeline_start = 0.5
    elif change == "gap":
        timeline.video_tracks[0].clips[1].timeline_start = 2
    elif change == "audio":
        timeline.audio_tracks = [AudioTrack(id="dialogue", clips=[AudioClip(id="speech", source=timeline.video_tracks[0].clips[0].source, source_out=1)])]
    elif change == "multi":
        timeline.video_tracks.append(VideoTrack(id="other", clips=[VideoClip(id="other-clip", source=timeline.video_tracks[0].clips[0].source, source_out=1)]))
    elif change == "empty":
        timeline.video_tracks.clear()
    else:
        timeline.width = 640
    save_timeline(render_project, timeline)
    with pytest.raises(RenderError) as caught:
        render_timeline(render_project)
    assert caught.value.code == code
    assert not list((render_project / "cache").iterdir())


def test_failed_ffmpeg_keeps_previous_preview_and_cleans_scratch(render_project, monkeypatch):
    preview = render_project / "preview" / "preview.mp4"
    preview.write_bytes(b"previous preview")
    sentinel = render_project / "cache" / "keep.txt"
    sentinel.write_text("keep")

    def fail(arguments):
        Path(arguments[-1]).write_bytes(b"partial output")
        raise FFmpegError("ffmpeg_failed", "encoder failed", stderr="useful diagnostic", returncode=1)

    monkeypatch.setattr("engine.render.run_ffmpeg", fail)
    with pytest.raises(RenderError) as caught:
        render_timeline(render_project)
    assert caught.value.to_dict()["details"]["stderr"] == "useful diagnostic"
    assert preview.read_bytes() == b"previous preview"
    assert list((render_project / "cache").iterdir()) == [sentinel]


def test_invalid_trim_cleans_partial_output(render_project):
    source = load_timeline(render_project).video_tracks[0].clips[0].source
    with pytest.raises(RenderError) as caught:
        render_clip(render_project, VideoClip(id="bad", source=source, source_out=20))
    assert caught.value.code == "trim_out_of_range"
    assert not list((render_project / "cache").iterdir())


def test_ffmpeg_runner_never_uses_shell_and_reports_stderr(monkeypatch):
    def fake_run(command, **kwargs):
        assert kwargs["shell"] is False
        assert command[:2] == ["ffmpeg", "-hide_banner"]
        return subprocess.CompletedProcess(command, 1, stderr="encoder diagnostic")
    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(FFmpegError) as caught:
        run_ffmpeg(["-n", "file with spaces.mp4"])
    assert caught.value.stderr == "encoder diagnostic"
    assert caught.value.returncode == 1


def test_transitions_are_rejected(render_project):
    from schemas.timeline import Transition
    timeline = load_timeline(render_project)
    timeline.video_tracks[0].transitions = [Transition(id="blend", from_clip="wide", to_clip="tall", duration=0.2)]
    save_timeline(render_project, timeline)
    with pytest.raises(RenderError) as caught:
        render_timeline(render_project)
    assert caught.value.code == "unsupported_transitions"


@pytest.mark.parametrize("failure,code", [(FileNotFoundError(), "ffmpeg_missing"),
    (subprocess.TimeoutExpired("ffmpeg", 300, stderr=b"timed out diagnostic"), "ffmpeg_timeout")])
def test_runner_start_errors(monkeypatch, failure, code):
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(FFmpegError) as caught:
        run_ffmpeg(["-n", "output.mp4"])
    assert caught.value.code == code


def test_preview_cannot_overwrite_source(render_project, render_sources):
    import shutil
    preview = render_project / "preview" / "preview.mp4"
    shutil.copyfile(render_sources[0], preview)
    before = preview.read_bytes()
    timeline = load_timeline(render_project)
    timeline.video_tracks[0].clips[0].source = str(preview)
    save_timeline(render_project, timeline)
    with pytest.raises(RenderError) as caught:
        render_timeline(render_project)
    assert caught.value.code == "source_output_conflict"
    assert preview.read_bytes() == before
