"""Mocked semantic providers: no inference, network or renderer required."""

import asyncio
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from analysis.provider import SuppliedAnalysisProvider
from mcp_server import build_server
from schemas.source_analysis import SourceAnalysis
from services import analysis_service as service
from services.project_service import create_project


@pytest.fixture
def project(tmp_path):
    sources = tmp_path / "footage tiếng Việt"
    sources.mkdir()
    video = sources / "source 01.mp4"
    video.write_bytes(b"mock video; analysis tests never decode media")
    result = create_project("Semantic", sources, projects_root=tmp_path / "projects")
    assert result.success
    index = {"version": 1, "sources": [{"id": "source-01", "path": str(video),
             "duration": 5, "width": 1920, "height": 1080, "fps": 24}]}
    (result.project_path / "source_index.json").write_text(json.dumps(index), encoding="utf-8")
    return result.project_path, video


@pytest.fixture
def observations():
    return dict(source_id="source-01", characters=["Mai"], location="Hà Nội",
                shot_size="medium", camera_angle="eye level", camera_motion="static",
                action="Mai opens a door", emotion="curious", dialogue="Xin chào!",
                visual_quality="Sharp image; slight flicker near the end",
                continuity_notes=["Door opens left; Mai wears a blue shirt"],
                usable_start=0.25, usable_end=4.5, problems=["End-frame flicker"],
                description="Mai bước vào phòng.")


def test_provider_contract_storage_and_no_editing(project, observations):
    folder, video = project
    before = {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    provider = Mock()
    provider.analyze.return_value = SourceAnalysis(**observations)
    result = service.analyze_source(folder, "source-01", provider=provider)
    context = provider.analyze.call_args.args[0]
    assert (context.source_id, context.path, context.duration, context.width,
            context.height, context.fps) == ("source-01", video, 5, 1920, 1080, 24)
    provider.analyze.assert_called_once()
    saved = folder / "analysis" / "source-01.json"
    assert result["analysis_path"] == str(saved)
    assert result["analysis"] == observations
    assert "Hà Nội" in saved.read_text(encoding="utf-8")
    assert '\n  "source_id"' in saved.read_text(encoding="utf-8")
    assert service.get_source_analysis(folder, "source-01") == result
    assert all(p.read_bytes() == contents for p, contents in before.items())
    assert video.read_bytes().startswith(b"mock video")
    assert set(p for p in folder.rglob("*") if p.is_file()) == set(before) | {saved}


@pytest.mark.parametrize("changes", [
    {"usable_start": -1}, {"usable_end": float("nan")}, {"usable_start": float("inf")},
    {"usable_end": 0.25}, {"usable_start": None}, {"characters": "Mai"},
    {"source_id": "../escape"}, {"source_id": "D:\\escape"}, {"unexpected": "field"},
])
def test_schema_rejects_invalid_data(observations, changes):
    with pytest.raises(ValidationError):
        SourceAnalysis(**{**observations, **changes})


def test_unknown_observations_are_explicit(observations):
    value = {key: ([] if isinstance(item, list) else None) for key, item in observations.items()}
    value["source_id"] = "source-01"
    assert SourceAnalysis(**value).usable_end is None
    with pytest.raises(ValidationError):
        SourceAnalysis(source_id="source-01")


@pytest.mark.parametrize("changes", [{"source_id": "another-source"}, {"usable_end": 6},
                                      {"usable_start": 4.5}, {"description": {"bad": 1}}])
def test_invalid_provider_output_preserves_previous_analysis(project, observations, changes):
    folder, _ = project
    service.analyze_source(folder, "source-01", provider=SuppliedAnalysisProvider(observations))
    saved = folder / "analysis" / "source-01.json"
    before = saved.read_bytes()
    with pytest.raises(service.AnalysisError) as error:
        service.analyze_source(folder, "source-01", provider=SuppliedAnalysisProvider({**observations, **changes}))
    assert error.value.code == "invalid_source_analysis"
    assert saved.read_bytes() == before
    assert not list(saved.parent.glob("*.tmp"))


def test_provider_failure_and_missing_configuration(project):
    folder, _ = project
    with pytest.raises(service.AnalysisError) as error:
        service.analyze_source(folder, "source-01")
    assert error.value.code == "analysis_provider_not_configured"
    provider = Mock()
    provider.analyze.side_effect = TimeoutError("model timed out")
    with pytest.raises(service.AnalysisError) as error:
        service.analyze_source(folder, "source-01", provider=provider)
    assert error.value.to_dict() == {"code": "analysis_provider_failed",
                                   "message": "Source analysis provider failed: model timed out"}
    assert not list((folder / "analysis").iterdir())


def test_source_lookup_failures_do_not_call_provider(project, observations):
    folder, video = project
    provider = Mock()
    for source_id, code in [("../outside", "invalid_source_id"), ("missing", "source_not_found")]:
        with pytest.raises(service.AnalysisError) as error:
            service.analyze_source(folder, source_id, provider=provider)
        assert error.value.code == code
    video.unlink()
    with pytest.raises(service.AnalysisError) as error:
        service.analyze_source(folder, "source-01", provider=provider)
    assert error.value.code == "source_unavailable"
    provider.analyze.assert_not_called()


def test_missing_corrupt_and_mismatched_saved_analysis(project, observations):
    folder, _ = project
    with pytest.raises(service.AnalysisError) as error:
        service.get_source_analysis(folder, "source-01")
    assert error.value.code == "analysis_not_found"
    saved = folder / "analysis" / "source-01.json"
    saved.write_text("broken JSON")
    with pytest.raises(service.AnalysisError) as error:
        service.get_source_analysis(folder, "source-01")
    assert error.value.code == "analysis_read_failed"
    saved.write_text(json.dumps({**observations, "source_id": "other"}))
    with pytest.raises(service.AnalysisError) as error:
        service.get_source_analysis(folder, "source-01")
    assert error.value.code == "invalid_source_analysis"


def test_atomic_write_failure_preserves_saved_analysis(project, observations, monkeypatch):
    folder, _ = project
    result = service.analyze_source(folder, "source-01", provider=SuppliedAnalysisProvider(observations))
    saved = Path(result["analysis_path"])
    before = saved.read_bytes()
    def fail(*args):
        raise PermissionError("destination locked")
    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(service.AnalysisError) as error:
        service.analyze_source(folder, "source-01", provider=SuppliedAnalysisProvider(observations))
    assert error.value.code == "analysis_write_failed"
    assert saved.read_bytes() == before
    assert list(saved.parent.iterdir()) == [saved]


@pytest.mark.parametrize("contents", ["broken", '{"sources": {}}', '{"sources": [null]}'])
def test_corrupt_index_is_structured(project, contents):
    folder, _ = project
    (folder / "source_index.json").write_text(contents)
    with pytest.raises(service.AnalysisError) as error:
        service.get_source_analysis(folder, "source-01")
    assert error.value.code == "analysis_context_invalid"


def test_mcp_injected_and_supplied_provider_and_error_recovery(project, observations):
    folder, _ = project
    provider = Mock()
    provider.analyze.return_value = observations
    server = build_server(folder.parent, analysis_provider=provider)
    async def exercise():
        args = {"project": "Semantic", "source_id": "source-01"}
        async def call(name, arguments):
            raw = await server.call_tool(name, arguments)
            return raw[1] if isinstance(raw, tuple) else raw
        assert (await call("analyze_source", args))["data"]["analysis"] == observations
        assert (await call("get_source_analysis", args))["data"]["analysis"] == observations
        changed = {**observations, "description": "Updated observation"}
        assert (await call("analyze_source", {**args, "analysis": changed}))["success"]
        assert provider.analyze.call_count == 1
        bad = await call("analyze_source", {**args, "analysis": {}})
        assert bad["error"]["code"] == "invalid_source_analysis"
        provider.analyze.side_effect = RuntimeError("offline")
        assert (await call("analyze_source", args))["error"]["code"] == "analysis_provider_failed"
        assert (await call("get_source_analysis", args))["data"]["analysis"] == changed
        assert (await call("ping", {}))["success"]
    asyncio.run(exercise())
