import asyncio
import hashlib
import json
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.ffmpeg import FFmpegError, run_ffmpeg
from engine.media import probe_media
from engine.render import RenderError, render_timeline
from engine.subtitle import (SubtitleError, WhisperSettings, format_srt, format_timestamp,
                             parse_srt, read_srt, transcribe_audio)
from engine.timeline import TimelineError, load_timeline, save_timeline, validate_timeline
from mcp_server import build_server
from schemas.timeline import SubtitleCue, SubtitleTrack, VideoClip, VideoTrack
from services.project_service import create_project
from services.subtitle_service import generate_subtitles


@pytest.mark.parametrize("time,expected", [(0, "00:00:00,000"), (1.234, "00:00:01,234"),
    (59.9996, "00:01:00,000"), (3599.9996, "01:00:00,000"), (360000, "100:00:00,000")])
def test_srt_timestamp_rounding_and_carry(time, expected):
    assert format_timestamp(time) == expected


def test_bilingual_srt_roundtrip_utf8(tmp_path):
    segments = [{"start": 0.125, "end": 1.25, "text": "Hello world."},
                {"start": 1.25, "end": 3.5, "text": "Xin chào Việt Nam!\nĐây là phụ đề."}]
    text = format_srt(segments)
    assert text.startswith("1\n00:00:00,125 --> 00:00:01,250\nHello world.\n\n2\n")
    assert text.endswith("Đây là phụ đề.\n\n")
    file = tmp_path / "Vietnamese.srt"
    file.write_text(text, encoding="utf-8")
    assert read_srt(file) == segments
    assert parse_srt("\ufeff" + text.replace("\n", "\r\n")) == segments
    assert format_srt([]) == "" and parse_srt("") == []


@pytest.mark.parametrize("segments", [[dict(start=-1, end=1, text="Hi")], [dict(start=1, end=1, text="Hi")],
    [dict(start=0, end=float("nan"), text="Hi")], [dict(start=0, end=1, text=" ")],
    [dict(start=0, end=1, text="Hi\n\nthere")], [dict(start=0, end=0.0001, text="Hi")],
    [dict(start=0, end=2, text="One"), dict(start=1, end=3, text="Overlap")]])
def test_invalid_srt_rejected(segments):
    with pytest.raises(SubtitleError):
        format_srt(segments)


@pytest.mark.parametrize("text", ["garbage", "1\n00:60:00,000 --> 00:60:01,000\nHi",
    "2\n00:00:00,000 --> 00:00:01,000\nHi", "1\n00:00:02,000 --> 00:00:01,000\nHi"])
def test_invalid_srt_file_rejected(text):
    with pytest.raises(SubtitleError):
        parse_srt(text)


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    root = tmp_path_factory.mktemp("subtitle source")
    video = root / "dialogue.mp4"
    run_ffmpeg(["-n", "-f", "lavfi", "-i", "color=c=black:s=320x240:r=24", "-f", "lavfi", "-i",
                "sine=frequency=480:sample_rate=48000", "-t", "2", "-c:v", "mpeg4", "-threads", "1",
                "-c:a", "aac", str(video)])
    return video


@pytest.fixture
def project(tmp_path, source):
    result = create_project("Subtitles", source.parent, projects_root=tmp_path / "projects ü ' spaces")
    folder = result.project_path
    data = result.project.model_dump(mode="json")
    data["resolution"] = {"width": 320, "height": 240}
    (folder / "project.json").write_text(json.dumps(data), encoding="utf-8")
    timeline = load_timeline(folder)
    timeline.width, timeline.height = 320, 240
    timeline.video_tracks = [VideoTrack(id="video", clips=[VideoClip(id="clip", source=str(source),
                            source_in=0.25, source_out=1.75)])]
    save_timeline(folder, timeline)
    return folder


@pytest.fixture
def fake_transcriber(monkeypatch):
    calls = []
    def transcribe(audio, language):
        with wave.open(str(audio), "rb") as stream:
            assert (stream.getframerate(), stream.getnchannels(), stream.getsampwidth()) == (16000, 1, 2)
            assert stream.getnframes() / 16000 == pytest.approx(1.5, abs=0.05)
        calls.append(language)
        return {"language": language, "model": "test-double", "duration": 1.5,
                "segments": [{"start": 0.2, "end": 1.2, "text": "Xin chào Việt Nam!" if language == "vi" else "Hello world!"}]}
    monkeypatch.setattr("services.subtitle_service.transcribe_audio", transcribe)
    return calls


def test_generation_persistence_reference_history_and_regeneration(project, source, fake_transcriber):
    original_hash = hashlib.sha256(source.read_bytes()).digest()
    previous = (project / "timeline.json").read_bytes()
    preview = project / "preview" / "preview.mp4"
    preview.write_bytes(b"previous preview remains")
    result = generate_subtitles(project, "Vietnamese")
    assert fake_transcriber == ["vi"]
    assert Path(result["srt_path"]).parent == project / "subtitles"
    assert "Xin chào Việt Nam!" in Path(result["srt_path"]).read_text(encoding="utf-8")
    assert json.loads(Path(result["transcript_path"]).read_text(encoding="utf-8"))["segments"] == result["segments"]
    assert Path(result["backup_path"]).read_bytes() == previous
    saved = load_timeline(project)
    assert saved.subtitle_tracks[0].file == result["subtitle_track"]["file"]
    assert not saved.subtitle_tracks[0].burn_in
    result2 = generate_subtitles(project, "vi")
    assert result2["srt_path"] != result["srt_path"] and Path(result["srt_path"]).is_file()
    assert len(load_timeline(project).subtitle_tracks) == 1
    assert preview.read_bytes() == b"previous preview remains"
    assert hashlib.sha256(source.read_bytes()).digest() == original_hash
    assert not list((project / "cache").iterdir())


def frame(video, timestamp):
    result = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-ss", str(timestamp), "-i", str(video),
                             "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-threads", "1", "-"],
                            check=True, capture_output=True, timeout=30, shell=False)
    return result.stdout


def test_optional_burn_in_real_preview_preserves_audio(project, fake_transcriber):
    result = generate_subtitles(project, "vi")
    before = (project / "timeline.json").read_bytes()
    preview = render_timeline(project)
    baseline = frame(preview, 0.6)
    assert max(baseline) < 10
    preview = render_timeline(project, burn_subtitles=True)
    assert max(frame(preview, 0.6)) > 200  # Subtitles are visible in real frames.
    assert max(frame(preview, 1.4)) < 10  # Subtitle disappears at its endpoint.
    info = probe_media(preview)
    assert (info["video_codec"], info["audio_codec"], info["sample_rate"]) == ("h264", "aac", 48000)
    assert info["duration"] == pytest.approx(1.5, abs=0.07)
    assert (project / "timeline.json").read_bytes() == before
    assert Path(result["srt_path"]).is_file() and not list((project / "cache").iterdir())


def test_inline_burn_in_flag_and_disabled_track(project):
    timeline = load_timeline(project)
    timeline.subtitle_tracks = [SubtitleTrack(id="inline", burn_in=True, cues=[
        SubtitleCue(id="line", text="English subtitle", timeline_start=0, timeline_end=1)])]
    save_timeline(project, timeline)
    assert max(frame(render_timeline(project), 0.5)) > 200
    assert max(frame(render_timeline(project, burn_subtitles=False), 0.5)) < 10
    timeline.subtitle_tracks[0].enabled = False
    save_timeline(project, timeline)
    assert max(frame(render_timeline(project, burn_subtitles=True), 0.5)) < 10


def test_missing_and_corrupt_srt_validation(project):
    timeline = load_timeline(project)
    timeline.subtitle_tracks = [SubtitleTrack(id="file", file="subtitles/missing.srt")]
    result = validate_timeline(timeline, base_dir=project)
    assert not result.valid and result.errors[0].location == ["subtitle_tracks", 0, "file"]
    file = project / "subtitles" / "broken.srt"
    file.write_bytes(b"\xff\xff")
    timeline.subtitle_tracks[0].file = str(file)
    assert not validate_timeline(timeline, base_dir=project).valid


def test_stale_transcription_does_not_overwrite_timeline(project, fake_transcriber, monkeypatch):
    from services import subtitle_service
    original = subtitle_service.transcribe_audio
    def changed(audio, language):
        result = original(audio, language)
        timeline = load_timeline(project)
        timeline.video_tracks[0].clips[0].volume = 0.5
        save_timeline(project, timeline)
        return result
    monkeypatch.setattr(subtitle_service, "transcribe_audio", changed)
    with pytest.raises(SubtitleError) as caught:
        generate_subtitles(project, "en")
    assert caught.value.code == "timeline_changed"
    assert load_timeline(project).video_tracks[0].clips[0].volume == 0.5
    assert not list((project / "subtitles").iterdir())
    assert not list((project / "cache").iterdir())


def test_failed_inference_preserves_timeline_and_artifacts(project, monkeypatch):
    previous = (project / "timeline.json").read_bytes()
    def fail(*args):
        raise SubtitleError("transcription_failed", "Simulated inference failure")
    monkeypatch.setattr("services.subtitle_service.transcribe_audio", fail)
    with pytest.raises(SubtitleError):
        generate_subtitles(project, "en")
    assert (project / "timeline.json").read_bytes() == previous
    assert not list((project / "cache").iterdir())
    assert not list((project / "subtitles").iterdir())


def test_failed_save_removes_only_new_artifacts(project, fake_transcriber, monkeypatch):
    prior = project / "subtitles" / "prior.srt"
    prior.write_text("", encoding="utf-8")
    def fail(*args):
        raise TimelineError("timeline_write_failed", "Simulated persistence failure")
    monkeypatch.setattr("services.subtitle_service.save_locked_timeline", fail)
    with pytest.raises(TimelineError):
        generate_subtitles(project, "en")
    assert list((project / "subtitles").iterdir()) == [prior]


def test_failed_burn_preserves_preview(project, fake_transcriber, monkeypatch):
    generate_subtitles(project, "en")
    preview = project / "preview" / "preview.mp4"
    preview.write_bytes(b"old preview")
    def fail(*args, **kwargs):
        raise FFmpegError("ffmpeg_failed", "libass unavailable", stderr="subtitle filter diagnostic")
    monkeypatch.setattr("engine.subtitle.run_ffmpeg", fail)
    with pytest.raises(RenderError) as caught:
        render_timeline(project, burn_subtitles=True)
    assert caught.value.details["stderr"] == "subtitle filter diagnostic"
    assert preview.read_bytes() == b"old preview" and not list((project / "cache").iterdir())


@pytest.mark.parametrize("language", ["en", "vi"])
def test_whisper_adapter_handles_lazy_sdk_segments(language, monkeypatch, tmp_path):
    def transcribe(path, **options):
        assert options["language"] == language and options["task"] == "transcribe" and options["vad_filter"]
        return iter([SimpleNamespace(start=0.1, end=1.2, text=" Xin chào! ")]), SimpleNamespace(duration=2)
    monkeypatch.setattr("engine.subtitle._load_model", lambda **kwargs: SimpleNamespace(transcribe=transcribe))
    result = transcribe_audio(tmp_path / "audio.wav", language)
    assert result["segments"] == [dict(start=0.1, end=1.2, text="Xin chào!")]


def test_whisper_lazy_error_and_configuration(monkeypatch, tmp_path):
    def transcribe(path, **options):
        def segments():
            raise RuntimeError("lazy inference failed")
            yield
        return segments(), SimpleNamespace(duration=1)
    monkeypatch.setattr("engine.subtitle._load_model", lambda **kwargs: SimpleNamespace(transcribe=transcribe))
    with pytest.raises(SubtitleError, match="lazy inference failed"):
        transcribe_audio(tmp_path / "audio.wav", "en")
    with pytest.raises(SubtitleError, match="multilingual"):
        transcribe_audio(tmp_path / "audio.wav", "vi", settings=WhisperSettings(model="tiny.en"))
    monkeypatch.setenv("FILMCUT_WHISPER_THREADS", "0")
    with pytest.raises(SubtitleError):
        WhisperSettings.from_environment()


def test_subtitle_mcp_success_and_error_recovery(project, fake_transcriber):
    server = build_server(project.parent)
    async def exercise():
        success = await server.call_tool("generate_subtitles", {"project": "Subtitles", "language": "vi"})
        data = success[1]
        assert data["success"] and data["data"]["segments"]
        failed = await server.call_tool("generate_subtitles", {"project": "Subtitles", "language": "invalid"})
        assert not failed[1]["success"] and failed[1]["error"]["code"] == "unsupported_language"
        assert (await server.call_tool("ping", {}))[1]["success"]
    asyncio.run(exercise())
