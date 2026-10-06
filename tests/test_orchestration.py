import asyncio
import hashlib
import json
import wave
from pathlib import Path

import pytest

from engine.ffmpeg import run_ffmpeg
from engine.media import probe_media
from engine.timeline import load_timeline
from mcp_server import build_server
from services import orchestration_service as workflow
from services.analysis_service import AnalysisError
from services.project_service import create_project


@pytest.fixture
def project(tmp_path):
    sources = tmp_path / "source files"
    sources.mkdir()
    result = create_project("Auto", sources, projects_root=tmp_path / "projects")
    assert result.success
    metadata = result.project.model_dump(mode="json")
    metadata["resolution"] = {"width": 160, "height": 120}
    (result.project_path / "project.json").write_text(json.dumps(metadata), encoding="utf-8")
    return result.project_path


def fake_pipeline(monkeypatch, failed_stage=None):
    calls = []
    def action(name):
        def invoke(*args, **kwargs):
            calls.append(name)
            if name == failed_stage:
                raise AnalysisError("simulated_failure", f"Failure in {name}")
            return {"summary": f"Ran {name}"}
        return invoke
    targets = [
        (workflow, "_sources"), (workflow, "_script"),
        (workflow.matching_service, "rank_sources_for_script"),
        (workflow.rough_cut_service, "build_rough_cut"),
        (workflow.refinement_service, "plan_edit_refinement"),
        (workflow.refinement_service, "apply_edit_refinement"),
        (workflow.sound_service, "plan_music"), (workflow.sound_service, "apply_music_plan"),
        (workflow.sound_service, "plan_sfx"), (workflow.sound_service, "apply_sfx_plan"),
        (workflow.subtitle_service, "generate_subtitles"), (workflow.render, "render_timeline"),
    ]
    for name, (module, attr) in zip(workflow.STAGES, targets):
        monkeypatch.setattr(module, attr, action(name))
    return calls


@pytest.mark.parametrize("failed", workflow.STAGES)
def test_every_stage_failure_is_identifiable_and_checkpointed(project, monkeypatch, failed):
    calls = fake_pipeline(monkeypatch, failed)
    report = workflow.auto_edit_project(project)
    position = workflow.STAGES.index(failed)
    assert calls == list(workflow.STAGES[:position + 1])
    assert not report["success"] and report["status"] == "failed"
    assert report["failed_stage"] == failed and report["preview_path"] is None
    assert report["error"]["code"] == "simulated_failure"
    assert report["error"]["stage"] == failed
    assert [s["status"] for s in report["stages"]] == ["completed"] * position + ["failed"] + ["blocked"] * (11-position)
    for stage in report["stages"]:
        assert stage["logs"]
        if stage["status"] == "completed":
            assert stage["artifacts"] and stage["errors"] == []
            assert Path(stage["artifacts"][-1]).is_file()
        else:
            assert stage["errors"]
    assert json.loads(Path(report["report_path"]).read_text()) == report
    assert json.loads((project / "analysis" / "auto_edit_report.json").read_text()) == report
    assert not (project / ".auto-edit.lock").exists()
    assert list((project / "output").iterdir()) == []


def test_invalid_settings_are_reported(project):
    (project / "auto_edit.json").write_text('{"language":"unsupported"}')
    report = workflow.auto_edit_project(project)
    assert report["failed_stage"] == "analyze_sources"
    assert "language" in report["error"]["message"]


def test_workflow_lock_and_report_failure_are_structured(project, monkeypatch):
    lock = project / ".auto-edit.lock"
    lock.write_text("existing run")
    with pytest.raises(AnalysisError, match="already running") as error:
        workflow.auto_edit_project(project)
    assert error.value.code == "workflow_busy"
    assert lock.read_text() == "existing run"
    lock.unlink()
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(workflow, "_write", fail)
    with pytest.raises(AnalysisError) as error:
        workflow.auto_edit_project(project)
    assert error.value.code == "workflow_report_failed"
    assert not lock.exists()


def test_mcp_failure_keeps_report_and_process_alive(project, monkeypatch):
    fake_pipeline(monkeypatch, "apply_sfx")
    server = build_server(project.parent)
    async def exercise():
        result = await server.call_tool("auto_edit_project", {"project": "Auto"})
        payload = result[1]
        assert payload["success"] is False
        assert payload["data"]["failed_stage"] == "apply_sfx"
        assert payload["error"]["stage"] == "apply_sfx"
        assert (await server.call_tool("ping", {}))[1]["success"]
    asyncio.run(exercise())


class Provider:
    def __init__(self):
        self.calls = []
    def analyze(self, context):
        self.calls.append(context.source_id)
        return dict(source_id=context.source_id, characters=["Mai"], location="room",
            shot_size="medium", camera_angle=None, camera_motion=None,
            action="body hits concrete wall", emotion=None, dialogue="Hello",
            visual_quality="sharp", continuity_notes=[], usable_start=0,
            usable_end=context.duration, problems=[], description="Mai in room")


def real_inputs(project):
    sources = Path(json.loads((project / "project.json").read_text())["source_folder"])
    video = sources / "01.mp4"
    run_ffmpeg(["-n", "-f", "lavfi", "-i", "color=c=blue:s=160x120:r=24",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "2",
        "-c:v", "mpeg4", "-threads", "1", "-c:a", "aac", str(video)])
    roots = []
    for kind, frequency, tags in [("music", 880, ["ambient", "neutral"]),
                                 ("sfx", 1320, ["body", "impact", "wall", "concrete", "heavy"])]:
        root = project.parent / kind
        root.mkdir()
        run_ffmpeg(["-n", "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000",
                    "-t", "0.2", "-c:a", "pcm_s16le", str(root / "sound.wav")])
        (root / "library.json").write_text(json.dumps({"version": 1, "items": [
            {"id": kind, "file": "sound.wav", "tags": tags}]}))
        roots.append(root)
    (project / "script.txt").write_text("SCENE 1\nCharacters: Mai\nLocation: room\n"
        "Action: body hits concrete wall\nDialogue: Hello\nDuration: 1", encoding="utf-8")
    (project / "auto_edit.json").write_text('{"language":"vi"}')
    return video, roots


def test_real_complete_workflow_and_reuse(project, monkeypatch):
    video, (music, sfx) = real_inputs(project)
    original = hashlib.sha256(video.read_bytes()).hexdigest()
    provider = Provider()
    def transcribe(audio, language):
        assert language == "vi"
        with wave.open(str(audio), "rb") as handle:
            assert handle.getframerate() == 16000
            assert handle.getnframes() / 16000 == pytest.approx(1, abs=.06)
        return {"language": language, "model": "test-double", "duration": 1,
                "segments": [{"start": .1, "end": .9, "text": "Xin chào Việt Nam!"}]}
    monkeypatch.setattr(workflow.subtitle_service, "transcribe_audio", transcribe)
    report = workflow.auto_edit_project(project, analysis_provider=provider,
                                        music_library_root=music, sfx_library_root=sfx)
    assert report["success"], report
    assert [s["name"] for s in report["stages"]] == list(workflow.STAGES)
    assert all(s["status"] == "completed" and s["logs"] and s["artifacts"] for s in report["stages"])
    for stage in report["stages"]:
        assert all(Path(p).exists() for p in stage["artifacts"])
        snapshot = Path(stage["artifacts"][-1])
        assert json.loads(snapshot.read_text()) == stage["result"]
    assert provider.calls and report["stages"][0]["result"]["analyzed"] == 1
    preview = probe_media(report["preview_path"])
    assert preview["duration"] == pytest.approx(1, abs=.07)
    assert (preview["video_codec"], preview["audio_codec"], preview["sample_rate"]) == ("h264", "aac", 48000)
    timeline = load_timeline(project)
    assert timeline.music_tracks and timeline.sfx_tracks and timeline.subtitle_tracks
    assert "Xin chào Việt Nam!" in Path(report["stages"][10]["result"]["srt_path"]).read_text(encoding="utf-8")
    assert hashlib.sha256(video.read_bytes()).hexdigest() == original
    assert list((project / "output").iterdir()) == []
    assert list((project / "cache").iterdir()) == []
    saved_report = Path(report["report_path"]).read_bytes()
    (project / "script.txt").unlink()
    second = workflow.auto_edit_project(project, music_library_root=music, sfx_library_root=sfx)
    assert second["success"], second
    assert second["stages"][0]["result"] == {"source_index_path": str(project / "source_index.json"),
        "analyzed": 0, "reused": 1, "warnings": []}
    assert "Revalidating" in second["stages"][1]["logs"][1]
    assert second["run_id"] != report["run_id"]
    assert Path(report["report_path"]).read_bytes() == saved_report
    assert list((project / "output").iterdir()) == []
