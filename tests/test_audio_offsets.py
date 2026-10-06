import array
import asyncio
import json
import math
import subprocess
import sys
import wave
from pathlib import Path

import pytest
from pydantic import ValidationError

from engine.ffmpeg import FFmpegError, run_ffmpeg
from engine.media import probe_media
from engine.render import RenderError, render_clip, render_dialogue_audio, render_timeline
from engine.timeline import TimelineError, load_timeline, save_timeline, validate_timeline
from mcp_server import build_server
from schemas.timeline import VideoClip, VideoTrack
from services.project_service import create_project
from services.timeline_service import set_j_cut, set_l_cut, reset_audio_offset, set_transition
from services.music_service import add_music
from services.sfx_service import add_sfx


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    root = tmp_path_factory.mktemp("audio handles ü")
    files = []
    for color, frequencies in (("red", (660, 440, 330)), ("blue", (880, 1320, 1760)), ("green", (220, 660, 1100))):
        file = root / f"{color} dialogue.mkv"
        arguments = ["-n", "-f", "lavfi", "-i", f"color=c={color}:s=160x120:r=24"]
        for frequency, duration in zip(frequencies, (1, 2, 1)):
            arguments += ["-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000:duration={duration}"]
        run_ffmpeg([*arguments, "-filter_complex_threads", "1", "-filter_complex", "[1:a][2:a][3:a]concat=n=3:v=0:a=1[a]",
                    "-map", "0:v", "-map", "[a]", "-t", "4", "-c:v", "ffv1", "-threads", "1", "-c:a", "pcm_s16le", str(file)])
        files.append(file)
    return files


@pytest.fixture
def project(tmp_path, sources):
    result = create_project("Audio Cuts", sources[0].parent, projects_root=tmp_path / "projects ü ' spaces")
    folder = result.project_path
    metadata = result.project.model_dump(mode="json")
    metadata["resolution"] = {"width": 320, "height": 240}
    (folder / "project.json").write_text(json.dumps(metadata), encoding="utf-8")
    timeline = load_timeline(folder)
    timeline.width, timeline.height = 320, 240
    timeline.video_tracks = [VideoTrack(id="video", clips=[VideoClip(id=color, source=str(file), source_in=1,
        source_out=3, timeline_start=index * 2) for index, (color, file) in enumerate(zip(("red", "blue", "green"), sources))])]
    save_timeline(folder, timeline)
    return folder


def decode(path, *, stereo=False, mono=False):
    result = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-vn", "-ac", "1" if mono else "2", "-ar", "48000",
        "-f", "f32le", "-"], check=True, capture_output=True, timeout=30, shell=False)
    samples = array.array("f")
    samples.frombytes(result.stdout)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples if stereo or mono else array.array("f", ((samples[i] + samples[i + 1]) / 2 for i in range(0, len(samples), 2)))


def amplitude(samples, frequency, start, length=0.1):
    segment = samples[round(start * 48000):round((start + length) * 48000)]
    assert segment
    real = sum(value * math.cos(2 * math.pi * frequency * i / 48000) for i, value in enumerate(segment))
    imag = sum(value * math.sin(2 * math.pi * frequency * i / 48000) for i, value in enumerate(segment))
    return 2 * math.hypot(real, imag) / len(segment)


def frame_hashes(path):
    result = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-map", "0:v:0", "-f", "framemd5", "-"],
                            check=True, capture_output=True, timeout=30, shell=False, text=True)
    return [line for line in result.stdout.splitlines() if line and not line.startswith("#")]


def test_j_cut_reads_pre_roll_without_changing_video_or_doubling_dialogue(project, sources):
    baseline_path = render_timeline(project)
    baseline_frames, baseline = frame_hashes(baseline_path), decode(baseline_path)
    original = [file.read_bytes() for file in sources]
    prior = (project / "timeline.json").read_bytes()
    result = set_j_cut(project, "blue", 0.5)
    assert result["clip"]["audio_source_in"] == 0.5 and result["clip"]["audio_source_out"] == 3
    assert result["clip"]["audio_timeline_start"] == 1.5
    assert Path(result["backup_path"]).read_bytes() == prior
    saved = (project / "timeline.json").read_bytes()
    output = render_timeline(project)
    samples = decode(output)
    assert amplitude(samples, 880, 1.3) < 0.004
    assert amplitude(samples, 880, 1.7) > 0.07
    assert amplitude(samples, 440, 1.7) == pytest.approx(amplitude(baseline, 440, 1.7), rel=0.1)
    assert amplitude(samples, 1320, 2.2) == pytest.approx(amplitude(baseline, 1320, 2.2), rel=0.1)
    assert amplitude(samples, 880, 2.2) < 0.004
    assert frame_hashes(output) == baseline_frames and len(baseline_frames) == 144
    assert probe_media(output)["duration"] == pytest.approx(6, abs=0.05)
    assert not load_timeline(project).video_tracks[0].transitions
    assert (project / "timeline.json").read_bytes() == saved
    assert [file.read_bytes() for file in sources] == original
    assert not list((project / "cache").iterdir())


def test_l_cut_reads_post_roll_and_sums_only_one_event_per_clip(project):
    baseline = decode(render_timeline(project))
    set_l_cut(project, "red", 0.5)
    output = render_timeline(project)
    samples = decode(output)
    assert amplitude(samples, 330, 1.8) < 0.004
    assert amplitude(samples, 330, 2.2) > 0.07
    assert amplitude(samples, 330, 2.7) < 0.004
    assert amplitude(samples, 440, 2.2) < 0.004  # No repeated outgoing main dialogue.
    assert amplitude(samples, 1320, 2.2) == pytest.approx(amplitude(baseline, 1320, 2.2), rel=0.1)
    assert amplitude(samples, 440, 0.5) == pytest.approx(amplitude(baseline, 440, 0.5), rel=0.1)
    assert max(abs(value) for value in decode(output, stereo=True)) < 1
    info = probe_media(output)
    assert (info["video_codec"], info["audio_codec"], info["sample_rate"], info["fps"]) == ("h264", "aac", 48000, 24)


def test_combined_j_and_l_keep_source_to_timeline_sync_mathematically(project):
    set_j_cut(project, "blue", 0.5)
    set_l_cut(project, "blue", 0.5)
    timeline = load_timeline(project)
    clip = timeline.video_tracks[0].clips[1]
    assert clip.audio_duration == 3 and clip.audio_timeline_end == 4.5
    assert clip.resolved_audio_timeline_start + (clip.source_in - clip.resolved_audio_source_in) / clip.speed == clip.timeline_start
    assert clip.resolved_audio_timeline_start + (clip.source_out - clip.resolved_audio_source_in) / clip.speed == clip.timeline_end
    samples = decode(render_timeline(project))
    assert amplitude(samples, 880, 1.7) > 0.07
    assert amplitude(samples, 1320, 2.5) > 0.07
    assert amplitude(samples, 1760, 4.2) > 0.07 and amplitude(samples, 660, 4.2) > 0.07
    assert amplitude(samples, 1760, 4.7) < 0.004
    assert timeline.duration == 6


def test_reset_restores_default_audio_with_history(project):
    baseline = decode(render_timeline(project))
    set_j_cut(project, "blue", 0.5)
    set_l_cut(project, "red", 0.5)
    for clip_id in ("red", "blue"):
        before = (project / "timeline.json").read_bytes()
        result = reset_audio_offset(project, clip_id)
        assert Path(result["backup_path"]).read_bytes() == before
        assert all(result["clip"][field] is None for field in ("audio_source_in", "audio_source_out", "audio_timeline_start"))
    restored = decode(render_timeline(project))
    assert amplitude(restored, 880, 1.7) < 0.004 and amplitude(restored, 330, 2.2) < 0.004
    assert amplitude(restored, 1320, 2.5) == pytest.approx(amplitude(baseline, 1320, 2.5), rel=0.1)


@pytest.mark.parametrize("operation,duration", [("j", -1), ("l", 0), ("j", float("nan")), ("l", float("inf")),
                                                ("j", 1.5), ("l", 1.5)])
def test_invalid_offsets_preserve_timeline_and_history(project, operation, duration):
    before = (project / "timeline.json").read_bytes()
    history = set((project / "timeline_history").iterdir())
    with pytest.raises(Exception) as caught:
        (set_j_cut if operation == "j" else set_l_cut)(project, "blue", duration)
    assert hasattr(caught.value, "to_dict")
    assert (project / "timeline.json").read_bytes() == before
    assert set((project / "timeline_history").iterdir()) == history


def test_first_last_and_missing_clip_errors(project):
    for operation, clip in ((set_j_cut, "red"), (set_l_cut, "green")):
        with pytest.raises(TimelineError) as caught:
            operation(project, clip, 0.5)
        assert caught.value.code == "no_audio_boundary"
    with pytest.raises(TimelineError) as caught:
        reset_audio_offset(project, "missing")
    assert caught.value.code == "clip_not_found"


@pytest.mark.parametrize("values", [{"audio_source_in": -1}, {"audio_source_in": 3, "audio_source_out": 2},
    {"audio_timeline_start": -1}, {"audio_source_out": float("inf")}])
def test_invalid_independent_audio_schema(values):
    with pytest.raises(ValidationError):
        VideoClip(id="clip", source="file.mp4", source_in=1, source_out=3, **values)


def test_manual_independent_timing_and_source_validation(project):
    timeline = load_timeline(project)
    clip = timeline.video_tracks[0].clips[1]
    clip.audio_source_in, clip.audio_source_out, clip.audio_timeline_start = 0, 1, 0.25
    save_timeline(project, timeline)
    samples = decode(render_timeline(project))
    assert amplitude(samples, 880, 0.1) < 0.004
    assert amplitude(samples, 880, 0.5) > 0.07
    assert amplitude(samples, 1320, 2.5) < 0.004  # The default embedded dialogue is gone.
    clip.audio_source_out = 5
    validation = validate_timeline(timeline, base_dir=project)
    assert not validation.valid and validation.errors[0].code == "insufficient_audio_handle"


def test_visual_transition_unchanged_and_later_audio_offsets_not_shifted(project):
    set_j_cut(project, "blue", 0.5)
    before = load_timeline(project).video_tracks[0].clips[1]
    set_transition(project, "red", "crossfade", 0.5)
    timeline = load_timeline(project)
    assert timeline.video_tracks[0].clips[1].audio_timeline_start == before.audio_timeline_start
    assert timeline.video_tracks[0].transitions[0].type == "crossfade"
    assert probe_media(render_timeline(project))["duration"] == pytest.approx(5.5, abs=0.07)


def test_audio_offsets_with_bgm_sfx_and_transcription(project, tmp_path):
    set_j_cut(project, "blue", 0.5)
    tone = tmp_path / "extra.wav"
    run_ffmpeg(["-n", "-f", "lavfi", "-i", "sine=frequency=2200:sample_rate=48000:duration=0.5",
                "-c:a", "pcm_f32le", str(tone)])
    add_music(project, str(tone), loop=True, volume_db=-18)
    add_sfx(project, str(tone), timeline_time=4.5, volume_db=-6)
    samples = decode(render_timeline(project))
    assert amplitude(samples, 880, 1.7) > 0.07
    assert amplitude(samples, 2200, 4.6) > amplitude(samples, 2200, 4.2) * 3
    audio = render_dialogue_audio(project, load_timeline(project), tmp_path)
    with wave.open(str(audio), "rb") as stream:
        assert (stream.getframerate(), stream.getnchannels()) == (16000, 1)
        assert stream.getnframes() / 16000 == pytest.approx(6, abs=0.03)
    dialogue = decode(audio, mono=True)
    assert amplitude(dialogue, 880, 1.7) > 0.07
    assert amplitude(dialogue, 2200, 4.6) < 0.004


def test_isolated_clip_projects_independent_audio_into_local_time(project):
    set_j_cut(project, "blue", 0.5)
    clip = load_timeline(project).video_tracks[0].clips[1]
    output = render_clip(project, clip)
    samples = decode(output)
    assert probe_media(output)["duration"] == pytest.approx(2, abs=0.05)
    assert amplitude(samples, 1320, 0.3) > 0.07
    assert amplitude(samples, 880, 0.3) < 0.004  # Pre-roll lies outside this isolated picture.


def test_dialogue_failure_keeps_preview_and_cleans_scratch(project, monkeypatch):
    set_j_cut(project, "blue", 0.5)
    preview = project / "preview" / "preview.mp4"
    preview.write_bytes(b"old preview")
    def fail(*args, **kwargs):
        raise FFmpegError("ffmpeg_failed", "dialogue mixer failed", stderr="dialogue diagnostic")
    monkeypatch.setattr("engine.render.replace_dialogue", fail)
    with pytest.raises(RenderError) as caught:
        render_timeline(project)
    assert caught.value.details["stderr"] == "dialogue diagnostic"
    assert preview.read_bytes() == b"old preview"
    assert not list((project / "cache").iterdir())


def test_mcp_audio_offset_tools_and_recovery(project):
    server = build_server(project.parent)
    async def exercise():
        for name, clip in (("set_j_cut", "blue"), ("set_l_cut", "red")):
            result = await server.call_tool(name, {"project": "Audio Cuts", "clip_id": clip, "duration": 0.5})
            assert result[1]["success"] and result[1]["data"]["clip"]["audio_source_in"] is not None
        result = await server.call_tool("reset_audio_offset", {"project": "Audio Cuts", "clip_id": "blue"})
        assert result[1]["success"] and result[1]["data"]["clip"]["audio_source_in"] is None
        result = await server.call_tool("set_l_cut", {"project": "Audio Cuts", "clip_id": "red", "duration": 10})
        assert not result[1]["success"] and result[1]["error"]["code"]
        assert (await server.call_tool("ping", {}))[1]["success"]
    asyncio.run(exercise())


def test_fractional_sample_placement_and_no_embedded_audio_in_mixer(project, monkeypatch):
    from engine import audio
    captured = []
    original = audio.run_ffmpeg
    def record(arguments):
        if "-filter_complex" in arguments:
            captured.append(arguments[arguments.index("-filter_complex") + 1])
        return original(arguments)
    monkeypatch.setattr(audio, "run_ffmpeg", record)
    set_j_cut(project, "blue", 0.12345)
    samples = decode(render_timeline(project))
    assert "adelay=90074S:all=1" in captured[0]  # round((2 - .12345) * 48000)
    assert "amix=inputs=4" in captured[0]  # Three source events plus one silent clock.
    assert "[0:a" not in captured[0]
    assert amplitude(samples, 880, 1.92, 0.04) > 0.07


def test_missing_source_audio_and_disabled_clip_return_errors(project, tmp_path):
    silent = tmp_path / "silent.mkv"
    run_ffmpeg(["-n", "-f", "lavfi", "-i", "color=c=black:s=160x120:r=24", "-t", "4",
                "-c:v", "ffv1", "-threads", "1", str(silent)])
    timeline = load_timeline(project)
    timeline.video_tracks[0].clips[1].source = str(silent)
    save_timeline(project, timeline)
    before = (project / "timeline.json").read_bytes()
    with pytest.raises(Exception) as caught:
        set_j_cut(project, "blue", 0.5)
    assert caught.value.to_dict()["code"] == "missing_source_audio"
    assert (project / "timeline.json").read_bytes() == before
    timeline.video_tracks[0].clips[1].enabled = False
    save_timeline(project, timeline)
    with pytest.raises(TimelineError) as caught:
        set_j_cut(project, "blue", 0.5)
    assert caught.value.code == "no_audio_boundary"


def test_loud_dialogue_overlap_is_peak_limited(project):
    timeline = load_timeline(project)
    for clip in timeline.video_tracks[0].clips:
        clip.volume = 8
    save_timeline(project, timeline)
    set_j_cut(project, "blue", 0.5)
    samples = decode(render_timeline(project), stereo=True)
    assert max(abs(value) for value in samples) < 1


@pytest.fixture(scope="module")
def continuous_source(tmp_path_factory):
    file = tmp_path_factory.mktemp("continuous dialogue") / "same recording.mkv"
    run_ffmpeg(["-n", "-f", "lavfi", "-i", "color=c=black:s=160x120:r=24", "-f", "lavfi", "-i",
        "sine=frequency=440:sample_rate=48000", "-t", "6", "-c:v", "ffv1", "-threads", "1", "-c:a", "pcm_s16le", str(file)])
    return file


@pytest.mark.parametrize("operations", ["j", "l", "both"])
def test_shared_same_recording_handles_are_not_doubled(project, continuous_source, operations):
    timeline = load_timeline(project)
    timeline.video_tracks[0].clips = [
        VideoClip(id="red", source=str(continuous_source), source_in=1, source_out=3),
        VideoClip(id="blue", source=str(continuous_source), source_in=3, source_out=5, timeline_start=2)]
    save_timeline(project, timeline)
    baseline = decode(render_timeline(project))
    if operations in ("j", "both"):
        set_j_cut(project, "blue", 0.5)
    if operations in ("l", "both"):
        set_l_cut(project, "red", 0.5)
    output = render_timeline(project)
    samples = decode(output)
    for timestamp in (0.5, 1.7, 2.2, 2.7):
        assert amplitude(samples, 440, timestamp) == pytest.approx(amplitude(baseline, 440, timestamp), rel=0.1)
    assert probe_media(output)["duration"] == pytest.approx(4, abs=0.05)


def test_default_crossfade_of_aligned_source_audio_retains_complementary_gains(project, continuous_source):
    timeline = load_timeline(project)
    track = timeline.video_tracks[0]
    track.clips[0].source, track.clips[0].source_in, track.clips[0].source_out = str(continuous_source), 1, 3
    track.clips[1].source, track.clips[1].source_in, track.clips[1].source_out = str(continuous_source), 2.5, 4.5
    save_timeline(project, timeline)
    set_transition(project, "red", "crossfade", 0.5)
    baseline = decode(render_timeline(project))
    set_j_cut(project, "green", 0.5)
    rebuilt = decode(render_timeline(project))
    # Complementary fades of the same source sum to unity; compare the known
    # single-source level, avoiding packet-seek phase artifacts in the old path.
    assert amplitude(rebuilt, 440, 1.7) == pytest.approx(amplitude(baseline, 440, 0.5), rel=0.1)


def test_l_cut_preserves_manual_start_and_sets_exact_trailing_endpoint(project):
    timeline = load_timeline(project)
    clip = timeline.video_tracks[0].clips[1]
    clip.audio_source_in, clip.audio_source_out, clip.audio_timeline_start = 1, 3, 2.25
    save_timeline(project, timeline)
    result = set_l_cut(project, "blue", 0.5)
    changed = load_timeline(project).video_tracks[0].clips[1]
    assert changed.resolved_audio_source_in == 1 and changed.resolved_audio_timeline_start == 2.25
    assert changed.audio_timeline_end == changed.timeline_end + 0.5 == 4.5
    assert result["clip"]["audio_source_out"] == 3.25
