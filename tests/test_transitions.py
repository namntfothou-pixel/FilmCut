import array
import asyncio
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from engine.ffmpeg import FFmpegError, run_ffmpeg
from engine.media import probe_media
from engine.render import RenderError, render_dialogue_audio, render_timeline
from engine.timeline import TimelineError, load_timeline, save_timeline, validate_timeline
from mcp_server import build_server
from schemas.timeline import Transition, VideoClip, VideoTrack
from services.project_service import create_project
from services.timeline_service import remove_clip, set_transition
from services.music_service import add_music
from services.sfx_service import add_sfx


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    root = tmp_path_factory.mktemp("transition sources ü")
    files = []
    for color, frequency in (("red", 480), ("blue", 960), ("green", None)):
        file = root / f"{color} source.mp4"
        arguments = ["-n", "-f", "lavfi", "-i", f"color=c={color}:s=320x240:r=24"]
        if frequency:
            arguments += ["-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000"]
        run_ffmpeg([*arguments, "-t", "2.5", "-c:v", "mpeg4", "-threads", "1", "-c:a", "aac", str(file)])
        files.append(file)
    return files


@pytest.fixture
def project(tmp_path, sources):
    result = create_project("Transitions", sources[0].parent, projects_root=tmp_path / "projects ü ' spaces")
    folder = result.project_path
    metadata = result.project.model_dump(mode="json")
    metadata["resolution"] = {"width": 320, "height": 240}
    (folder / "project.json").write_text(json.dumps(metadata), encoding="utf-8")
    timeline = load_timeline(folder)
    timeline.width, timeline.height = 320, 240
    timeline.video_tracks = [VideoTrack(id="video", clips=[VideoClip(id=color, source=str(file),
        source_in=0.25, source_out=2.25, timeline_start=index * 2) for index, (color, file) in enumerate(zip(("red", "blue", "green"), sources))])]
    save_timeline(folder, timeline)
    return folder


def color_at(video, timestamp):
    result = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", str(timestamp), "-i", str(video),
        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-threads", "1", "-"],
        shell=False, check=True, capture_output=True, timeout=30)
    assert len(result.stdout) == 320 * 240 * 3
    offset = (120 * 320 + 160) * 3
    return tuple(result.stdout[offset:offset + 3])


def decode_audio(file):
    result = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(file), "-vn", "-ac", "2", "-ar", "48000",
        "-f", "f32le", "-"], shell=False, check=True, capture_output=True, timeout=30)
    samples = array.array("f")
    samples.frombytes(result.stdout)
    if sys.byteorder != "little":
        samples.byteswap()
    return array.array("f", ((samples[i] + samples[i + 1]) / 2 for i in range(0, len(samples), 2)))


def amplitude(samples, frequency, start, length=0.08):
    segment = samples[round(start * 48000):round((start + length) * 48000)]
    assert segment
    real = sum(value * math.cos(2 * math.pi * frequency * i / 48000) for i, value in enumerate(segment))
    imag = sum(value * math.sin(2 * math.pi * frequency * i / 48000) for i, value in enumerate(segment))
    return 2 * math.hypot(real, imag) / len(segment)


@pytest.mark.parametrize("kind,duration,expected", [("cut", 0, 6), ("crossfade", 0.5, 5.5), ("fade_to_black", 0.5, 5.5)])
def test_real_transition_frames_audio_and_duration(project, sources, kind, duration, expected):
    original = [file.read_bytes() for file in sources]
    result = set_transition(project, "red", kind, duration)
    before = (project / "timeline.json").read_bytes()
    output = render_timeline(project)
    metadata = probe_media(output)
    assert metadata["duration"] == pytest.approx(expected, abs=0.07)
    assert (metadata["width"], metadata["height"], metadata["fps"]) == (320, 240, 24)
    assert (metadata["video_codec"], metadata["audio_codec"], metadata["sample_rate"]) == ("h264", "aac", 48000)
    assert result["duration"] == expected == load_timeline(project).duration
    assert color_at(output, 0.5)[0] > 220 and color_at(output, 2.3)[2] > 220
    if kind == "cut":
        assert color_at(output, 1.9)[0] > 220
        assert color_at(output, 2.1)[2] > 220
    elif kind == "crossfade":
        red, green, blue = color_at(output, 1.75)
        assert 90 < red < 170 and 90 < blue < 170 and green < 15
    else:
        assert max(color_at(output, 1.75)) < 30
        assert color_at(output, 1.55)[0] > 100 and color_at(output, 1.95)[2] > 100
    samples = decode_audio(output)
    assert amplitude(samples, 480, 1.2) > 0.07
    assert amplitude(samples, 960, 1.2) < 0.005
    assert amplitude(samples, 480, 2.2) < 0.005
    assert amplitude(samples, 960, 2.2) > 0.07
    if kind != "cut":
        assert amplitude(samples, 480, 1.72) > 0.025
        assert amplitude(samples, 960, 1.72) > 0.025
    assert amplitude(samples, 960, expected - 0.5) < 0.005  # Third clip has no audio.
    run_ffmpeg(["-i", str(output), "-f", "null", "-"])
    assert (project / "timeline.json").read_bytes() == before
    assert [file.read_bytes() for file in sources] == original
    assert not list((project / "cache").iterdir())


def test_mixed_chain_uses_cumulative_overlap_for_all_audio(project, tmp_path):
    set_transition(project, "red", "crossfade", 0.5)
    set_transition(project, "blue", "fade_to_black", 0.25)
    timeline = load_timeline(project)
    assert [clip.timeline_start for clip in timeline.video_tracks[0].clips] == [0, 1.5, 3.25]
    assert timeline.duration == 5.25
    tone = tmp_path / "BGM and SFX.wav"
    run_ffmpeg(["-n", "-f", "lavfi", "-i", "sine=frequency=1920:sample_rate=48000", "-t", "0.5",
                "-c:a", "pcm_f32le", str(tone)])
    add_music(project, str(tone), loop=True, volume_db=-18)
    add_sfx(project, str(tone), timeline_time=4.75, volume_db=-6)
    output = render_timeline(project)
    assert probe_media(output)["duration"] == pytest.approx(5.25, abs=0.07)
    samples = decode_audio(output)
    assert amplitude(samples, 1920, 4.85) > amplitude(samples, 1920, 4.5) * 3
    assert color_at(output, 4)[1] > 110
    assert not list((project / "cache").iterdir())


@pytest.mark.parametrize("duration", [0.3, 2, 1 / 24])
def test_fractional_full_clip_and_single_frame_overlap(project, duration):
    set_transition(project, "red", "crossfade", duration)
    output = render_timeline(project)
    assert load_timeline(project).duration == pytest.approx(6 - duration)
    assert probe_media(output)["duration"] == pytest.approx(6 - duration, abs=1 / 24 + 0.03)


def test_dialogue_transcription_render_uses_transition_duration(project, tmp_path):
    set_transition(project, "red", "crossfade", 0.5)
    audio = render_dialogue_audio(project, load_timeline(project), tmp_path)
    import wave
    with wave.open(str(audio), "rb") as stream:
        assert stream.getnframes() / stream.getframerate() == pytest.approx(5.5, abs=0.03)


def test_set_update_and_cut_restore_positions_with_exact_backups(project, sources):
    previous = (project / "timeline.json").read_bytes()
    added = set_transition(project, "red", "crossfade", 0.5)
    assert Path(added["backup_path"]).read_bytes() == previous
    assert len(added["shifted_clips"]) == 2
    assert added["transition"]["type"] == "crossfade" and "kind" not in added["transition"]
    previous = (project / "timeline.json").read_bytes()
    changed = set_transition(project, "red", "fade_to_black", 0.25)
    assert changed["transition"]["id"] == added["transition"]["id"]
    assert Path(changed["backup_path"]).read_bytes() == previous
    restored = set_transition(project, "red", "cut", 0)
    assert restored["duration"] == 6
    assert [clip.timeline_start for clip in load_timeline(project).video_tracks[0].clips] == [0, 2, 4]
    assert len(load_timeline(project).video_tracks[0].transitions) == 1
    remove_clip(project, "green")  # Unrelated to the boundary.
    remove_clip(project, "blue")  # A zero-duration cut must not block removal.
    assert not load_timeline(project).video_tracks[0].transitions
    assert all(file.is_file() for file in sources)


@pytest.mark.parametrize("kind,duration", [("wipe", 0.5), ("crossfade", 0), ("cut", 0.5),
    ("crossfade", -1), ("fade_to_black", float("nan")), ("crossfade", 3), ("crossfade", 0.001)])
def test_invalid_edits_do_not_write_or_backup(project, kind, duration):
    previous = (project / "timeline.json").read_bytes()
    count = len(list((project / "timeline_history").iterdir()))
    with pytest.raises(TimelineError):
        set_transition(project, "red", kind, duration)
    assert (project / "timeline.json").read_bytes() == previous
    assert len(list((project / "timeline_history").iterdir())) == count


def test_incoming_and_outgoing_overlap_cannot_consume_same_frames(project):
    set_transition(project, "red", "crossfade", 1.25)
    previous = (project / "timeline.json").read_bytes()
    with pytest.raises(TimelineError, match="validation"):
        set_transition(project, "blue", "fade_to_black", 1)
    assert (project / "timeline.json").read_bytes() == previous


def test_missing_last_disabled_and_gap_boundaries(project):
    with pytest.raises(TimelineError) as caught:
        set_transition(project, "missing", "cut", 0)
    assert caught.value.code == "clip_not_found"
    with pytest.raises(TimelineError) as caught:
        set_transition(project, "green", "crossfade", 0.5)
    assert caught.value.code == "no_transition_boundary"
    timeline = load_timeline(project)
    timeline.video_tracks[0].clips[1].enabled = False
    save_timeline(project, timeline)
    with pytest.raises(TimelineError) as caught:
        set_transition(project, "blue", "crossfade", 0.5)
    assert caught.value.code == "no_transition_boundary"
    with pytest.raises(TimelineError) as caught:
        set_transition(project, "red", "crossfade", 0.5)
    assert caught.value.code == "noncontiguous_boundary"


def test_overlap_requires_explicit_correct_boundary(project):
    timeline = load_timeline(project)
    timeline.video_tracks[0].clips[1].timeline_start = 1.5
    assert not validate_timeline(timeline, base_dir=project).valid
    timeline.video_tracks[0].transitions = [Transition(id="boundary", from_clip="red", to_clip="blue",
                                                     type="crossfade", duration=0.5)]
    assert validate_timeline(timeline, base_dir=project).valid
    timeline.video_tracks[0].transitions[0].to_clip = "green"
    assert not validate_timeline(timeline, base_dir=project).valid


def test_legacy_kind_boundary_migrates_without_rewriting_during_load(project):
    path = project / "timeline.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["video_tracks"][0]["transitions"] = [dict(id="old", from_clip="red", to_clip="blue", kind="crossfade", duration=0.5)]
    path.write_text(json.dumps(data), encoding="utf-8")
    before = path.read_bytes()
    timeline = load_timeline(project)
    assert [clip.timeline_start for clip in timeline.video_tracks[0].clips] == [0, 1.5, 3.5]
    assert timeline.duration == 5.5 and path.read_bytes() == before
    result = set_transition(project, "red", "cut", 0)
    assert Path(result["backup_path"]).read_bytes() == before
    assert result["duration"] == 6


def test_join_failure_preserves_previous_preview_and_cache(project, monkeypatch):
    set_transition(project, "red", "crossfade", 0.5)
    preview = project / "preview" / "preview.mp4"
    preview.write_bytes(b"previous preview")
    sentinel = project / "cache" / "keep.txt"
    sentinel.write_text("keep")
    def fail(*args, **kwargs):
        raise FFmpegError("ffmpeg_failed", "transition encoder failed", stderr="useful transition diagnostic")
    monkeypatch.setattr("engine.render.transition_normalized", fail)
    with pytest.raises(RenderError) as caught:
        render_timeline(project)
    assert caught.value.details["stderr"] == "useful transition diagnostic"
    assert preview.read_bytes() == b"previous preview"
    assert list((project / "cache").iterdir()) == [sentinel]


def test_transition_mcp_structured_results_and_error_recovery(project):
    server = build_server(project.parent)
    async def exercise():
        result = await server.call_tool("set_transition", dict(project="Transitions", clip_id="red",
                                      transition_type="crossfade", duration=0.5))
        assert result[1]["success"] and result[1]["data"]["duration"] == 5.5
        result = await server.call_tool("set_transition", dict(project="Transitions", clip_id="red",
                                      transition_type="flashy", duration=1))
        assert not result[1]["success"] and result[1]["error"]["message"]
        assert (await server.call_tool("ping", {}))[1]["success"]
    asyncio.run(exercise())
