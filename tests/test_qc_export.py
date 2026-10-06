import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from engine.ffmpeg import run_ffmpeg
from engine.media import probe_media
from engine.timeline import load_timeline, save_timeline
from mcp_server import build_server
from schemas.timeline import SubtitleTrack, VideoClip, VideoTrack
from services.project_service import create_project
from services.qc_service import ExportError, export_final, qc_project


@pytest.fixture
def project(tmp_path):
    source_folder = tmp_path / "sources ü"
    source_folder.mkdir()
    result = create_project("QC Demo", source_folder, projects_root=tmp_path / "projects")
    folder = result.project_path
    metadata = result.project.model_dump(mode="json")
    metadata["resolution"] = {"width": 160, "height": 120}
    (folder / "project.json").write_text(json.dumps(metadata), encoding="utf-8")
    return folder


def video(path, *, color="blue", audio=True, volume=1):
    args = ["-n", "-f", "lavfi", "-i", f"color=c={color}:s=160x120:r=24"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000,volume={volume}"]
    args += ["-t", "1", "-c:v", "mpeg4", "-threads", "1"]
    if audio:
        args += ["-c:a", "pcm_s16le"]
    args += [str(path)]
    run_ffmpeg(args)


def set_source(project, path):
    timeline = load_timeline(project)
    timeline.width, timeline.height = 160, 120
    timeline.video_tracks = [VideoTrack(id="video", clips=[VideoClip(id="shot", source=str(path), source_out=1)])]
    save_timeline(project, timeline)


def check(report, name):
    return next(item for item in report["checks"] if item["name"] == name)


def test_complete_qc_and_final_export(project):
    source = Path(json.loads((project / "project.json").read_text())["source_folder"]) / "shot.mp4"
    video(source)
    set_source(project, source)
    report = qc_project(project)
    assert report["passed"], report
    assert Path(report["report_path"]) == project / "qc_report.json"
    assert json.loads(Path(report["report_path"]).read_text()) == report
    for name in ("missing_media", "broken_source_references", "timeline_validity", "unexpected_black_frames",
                 "audio_clipping", "missing_audio", "subtitle_timing", "subtitle_beyond_timeline",
                 "resolution", "fps", "output_duration"):
        assert check(report, name)["status"] == "pass", check(report, name)
    exported = export_final(project)
    path = project / "output" / "QC Demo_FINAL.mp4"
    assert Path(exported["export_path"]) == path and path.is_file()
    assert exported["qc_passed"] and not exported["forced"]
    info = probe_media(path)
    assert (info["width"], info["height"], info["fps"]) == (160, 120, 24)
    assert info["video_codec"] == "h264" and info["audio_codec"] == "aac"
    assert info["duration"] == pytest.approx(1, abs=.07)
    assert not list((project / "output").glob("*.tmp.mp4"))


def test_black_frames_block_export_unless_forced(project):
    source = Path(json.loads((project / "project.json").read_text())["source_folder"]) / "black.mp4"
    video(source, color="black")
    set_source(project, source)
    report = qc_project(project)
    assert not report["passed"]
    assert check(report, "unexpected_black_frames")["status"] == "fail"
    with pytest.raises(ExportError) as caught:
        export_final(project)
    assert caught.value.code == "qc_failed"
    assert caught.value.details["qc_report_path"] == str(project / "qc_report.json")
    assert not list((project / "output").glob("*_FINAL.mp4"))
    forced = export_final(project, force=True)
    assert forced["forced"] and not forced["qc_passed"]
    assert Path(forced["export_path"]).is_file()


def test_missing_program_audio_is_reported_even_if_mp4_has_silent_aac(project):
    source = Path(json.loads((project / "project.json").read_text())["source_folder"]) / "silent.mp4"
    video(source, audio=False)
    set_source(project, source)
    report = qc_project(project)
    assert check(report, "missing_audio")["status"] == "fail"
    assert any(item["check"] == "missing_audio" for item in report["warnings"])


def test_audio_clipping_threshold_gates_export(project, monkeypatch):
    source = Path(json.loads((project / "project.json").read_text())["source_folder"]) / "shot.mp4"
    video(source)
    set_source(project, source)
    monkeypatch.setattr("services.qc_service._audio_peak_db", lambda _: 0.0)
    report = qc_project(project)
    assert not report["passed"]
    assert check(report, "audio_clipping")["status"] == "fail"
    with pytest.raises(ExportError) as caught:
        export_final(project)
    assert caught.value.code == "qc_failed"
    assert export_final(project, force=True)["forced"]


def test_export_never_replaces_a_timeline_source(project):
    destination = project / "output" / "QC Demo_FINAL.mp4"
    destination.parent.mkdir(exist_ok=True)
    video(destination)
    original = destination.read_bytes()
    set_source(project, destination)
    with pytest.raises(ExportError) as caught:
        export_final(project)
    assert caught.value.code == "final_output_source_conflict"
    assert destination.read_bytes() == original


def test_qc_rejects_timeline_changed_during_preview_render(project, monkeypatch):
    source = Path(json.loads((project / "project.json").read_text())["source_folder"]) / "shot.mp4"
    video(source)
    set_source(project, source)
    from services import qc_service
    render = qc_service.render_timeline

    def render_then_change(folder):
        output = render(folder)
        timeline = folder / "timeline.json"
        timeline.write_bytes(timeline.read_bytes() + b"\n")
        return output

    monkeypatch.setattr(qc_service, "render_timeline", render_then_change)
    report = qc_project(project)
    assert not report["passed"]
    assert check(report, "timeline_stability")["status"] == "fail"


def test_missing_media_and_broken_media_have_distinct_findings(project):
    broken = project / "broken.mp4"
    broken.write_bytes(b"not a video")
    set_source(project, broken)
    report = qc_project(project)
    assert check(report, "missing_media")["status"] == "pass"
    assert check(report, "broken_source_references")["status"] == "fail"

    missing = project / "not-here.mp4"
    data = json.loads((project / "timeline.json").read_text(encoding="utf-8"))
    data["video_tracks"][0]["clips"][0]["source"] = str(missing)
    (project / "timeline.json").write_text(json.dumps(data), encoding="utf-8")
    report = qc_project(project)
    assert not report["passed"]
    assert check(report, "missing_media")["status"] == "fail"
    assert check(report, "timeline_validity")["status"] == "fail"
    assert check(report, "output_duration")["status"] == "skipped"
    with pytest.raises(ExportError, match="QC did not pass"):
        export_final(project)


def test_subtitle_timing_and_format_mismatch_are_reported(project):
    source = Path(json.loads((project / "project.json").read_text())["source_folder"]) / "shot.mp4"
    video(source)
    set_source(project, source)
    srt = project / "subtitles" / "late.srt"
    srt.write_text("1\n00:00:01,100 --> 00:00:01,500\nToo late\n\n", encoding="utf-8")
    timeline = load_timeline(project)
    timeline.subtitle_tracks = [SubtitleTrack(id="sub", language="en", file=str(srt))]
    save_timeline(project, timeline)
    report = qc_project(project)
    assert check(report, "subtitle_beyond_timeline")["status"] == "fail"
    assert check(report, "subtitle_beyond_timeline")["details"]["issues"][0]["code"] == "beyond_timeline"
    with pytest.raises(ExportError) as caught:
        export_final(project)
    assert caught.value.code == "qc_failed"

    timeline = load_timeline(project)
    timeline.subtitle_tracks = []
    timeline.width = 128
    timeline.fps = 30
    (project / "timeline.json").write_text(timeline.model_dump_json(indent=2), encoding="utf-8")
    report = qc_project(project)
    assert check(report, "resolution")["status"] == "fail"
    assert check(report, "fps")["status"] == "fail"
    assert check(report, "rendered_output")["status"] == "skipped"


def test_mcp_qc_and_export_tools(project):
    source = Path(json.loads((project / "project.json").read_text())["source_folder"]) / "shot.mp4"
    video(source)
    set_source(project, source)
    server = build_server(project.parent)

    async def exercise():
        qc = await server.call_tool("qc_project", {"project": "QC Demo"})
        payload = qc[1]
        assert payload["success"] and payload["data"]["passed"]
        export = await server.call_tool("export_final", {"project": "QC Demo"})
        result = export[1]
        assert result["success"] and Path(result["data"]["export_path"]).is_file()
        assert (await server.call_tool("ping", {}))[1]["success"]
    asyncio.run(exercise())
