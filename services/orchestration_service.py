"""Explicit, fail-fast orchestration of existing FilmCut services. Preview only."""

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from engine import media, render
from engine.timeline import _native_path, _project_context
from schemas.workflow import AutoEditSettings, WorkflowReport, WorkflowStage
from services import (analysis_service, script_service, matching_service,
                      rough_cut_service, refinement_service, sound_service, subtitle_service)
from services.analysis_service import AnalysisError

STAGES = (
    "analyze_sources", "analyze_script", "rank_sources", "build_rough_cut",
    "plan_edit_refinement", "apply_edit_refinement", "plan_music", "apply_music",
    "plan_sfx", "apply_sfx", "generate_subtitles", "render_preview",
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _write(path, document):
    """Write human-readable UTF-8 journal files atomically."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".workflow-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(document, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        temporary.replace(path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def _artifacts(value):
    paths = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("_path") and isinstance(item, str):
                paths.append(item)
            else:
                paths.extend(_artifacts(item))
    elif isinstance(value, list):
        for item in value:
            paths.extend(_artifacts(item))
    return list(dict.fromkeys(paths))


def _sources(folder, metadata, provider, stage):
    index = folder / "source_index.json"
    document = json.loads(index.read_text(encoding="utf-8")) if index.exists() else {"sources": []}
    if not isinstance(document, dict) or not isinstance(document.get("sources"), list):
        raise AnalysisError("invalid_source_index", "source_index.json must contain a sources list")
    if not document["sources"]:
        document = media.scan_folder(metadata.source_folder)
        media.write_source_index(folder, document)
        stage.logs.append(f"Scanned source folder: {len(document['sources'])} videos.")
    stage.artifacts.append(str(index))
    for error in document.get("errors", []):
        stage.logs.append(f"Source scan warning: {error}")
    if not document["sources"]:
        raise AnalysisError("no_video_sources", "No valid videos found in the source folder")
    analyzed, reused = 0, 0
    for source in document["sources"]:
        try:
            result = analysis_service.get_source_analysis(folder, source["id"])
            reused += 1
        except AnalysisError as exc:
            if exc.code != "analysis_not_found":
                raise
            result = analysis_service.analyze_source(folder, source["id"], provider=provider)
            analyzed += 1
        stage.artifacts.append(result["analysis_path"])
        stage.logs.append(f"Validated semantic analysis for {source['id']}.")
    stage.logs.append(f"Analyzed {analyzed} missing sources; reused {reused} saved analyses.")
    return {"source_index_path": str(index), "analyzed": analyzed, "reused": reused,
            "warnings": document.get("errors", [])}


def _script(folder, settings, provider, stage):
    path = _native_path(settings.script_file) if settings.script_file else folder / "script.txt"
    if not path.is_absolute():
        path = folder / path
    if settings.script_file is not None or path.exists():
        with path.open(encoding="utf-8-sig") as handle:
            script = handle.read(script_service.MAX_SCRIPT_CHARACTERS + 1)
        stage.logs.append(f"Analyzing script: {path}")
        return script_service.analyze_script(folder, script, provider=provider)
    try:
        existing = script_service.get_script_breakdown(folder)
    except AnalysisError as exc:
        if exc.code == "script_breakdown_not_found":
            raise AnalysisError("script_input_missing", "Provide project/script.txt, auto_edit.json script_file, or a saved script breakdown") from exc
        raise
    stage.logs.append("Revalidating the saved structured script breakdown; no original script supplied.")
    return script_service.analyze_script(folder, json.dumps(existing["breakdown"], ensure_ascii=False),
                                         breakdown=existing["breakdown"])


def auto_edit_project(project, *, analysis_provider=None, script_provider=None,
                      music_library_root=None, sfx_library_root=None):
    """Run all twelve stages, checkpointing outputs and preserving partial work.

    Calling this operation authorizes the conservative default refinement plan.
    Custom approved plans remain available through the separate plan/apply tools.
    No final export is called. Missing providers, assets, or models are errors,
    never invented analysis or silently skipped stages.
    """
    folder, metadata = _project_context(project)
    analysis = folder / "analysis"
    analysis.mkdir(exist_ok=True)
    if analysis.is_symlink():
        raise AnalysisError("invalid_analysis_directory", "Workflow reports must stay inside the project")
    runs = analysis / "workflow_runs"
    runs.mkdir(exist_ok=True)
    if runs.is_symlink():
        raise AnalysisError("invalid_workflow_directory", "Workflow runs must stay inside the project")
    lock = folder / ".auto-edit.lock"
    try:
        handle = lock.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise AnalysisError("workflow_busy", "An auto-edit workflow is already running; inspect its report before retrying") from exc
    try:
        with handle:
            run_id = uuid4().hex
            report = WorkflowReport(run_id=run_id, project=metadata.name, started_at=_now(),
                report_path=str(runs / f"{run_id}.json"), stages=[WorkflowStage(name=name) for name in STAGES])
            handle.write(run_id)
        def checkpoint():
            try:
                _write(Path(report.report_path), report.model_dump(mode="json"))
                _write(analysis / "auto_edit_report.json", report.model_dump(mode="json"))
            except (OSError, ValueError) as exc:
                raise AnalysisError("workflow_report_failed", f"Cannot checkpoint workflow {run_id}: {exc}") from exc

        checkpoint()
        settings = None
        def sources(stage):
            nonlocal settings
            config = folder / "auto_edit.json"
            settings = AutoEditSettings.model_validate_json(config.read_text(encoding="utf-8")) if config.exists() else AutoEditSettings()
            return _sources(folder, metadata, analysis_provider, stage)

        def rank(stage):
            result = matching_service.rank_sources_for_script(folder)
            path = analysis / "source_rankings.json"
            _write(path, result)
            return {**result, "rankings_path": str(path)}

        def preview(stage):
            path = render.render_timeline(folder)
            return {"preview_path": str(path), "metadata": media.probe_media(path)}

        actions = (
            sources,
            lambda stage: _script(folder, settings, script_provider, stage),
            rank,
            lambda stage: rough_cut_service.build_rough_cut(folder),
            lambda stage: refinement_service.plan_edit_refinement(folder),
            lambda stage: refinement_service.apply_edit_refinement(folder),
            lambda stage: sound_service.plan_music(folder, library_root=music_library_root),
            lambda stage: sound_service.apply_music_plan(folder, library_root=music_library_root),
            lambda stage: sound_service.plan_sfx(folder, library_root=sfx_library_root),
            lambda stage: sound_service.apply_sfx_plan(folder, library_root=sfx_library_root),
            lambda stage: subtitle_service.generate_subtitles(folder, settings.language),
            preview,
        )
        for stage, action in zip(report.stages, actions):
            stage.status, stage.started_at = "running", _now()
            stage.logs.append(f"Starting {stage.name}.")
            checkpoint()
            try:
                result = action(stage)
                stage.result = result
                stage.artifacts = list(dict.fromkeys([*stage.artifacts, *_artifacts(result)]))
                # Capture immutable per-run output snapshots: shared plan files can
                # be revised by later stages or a subsequent workflow invocation.
                snapshot = runs / f"{run_id}-{stage.name}.json"
                _write(snapshot, result)
                stage.artifacts.append(str(snapshot))
                stage.logs.append(result.get("summary", f"Completed {stage.name}."))
                for warning in result.get("warnings", []):
                    stage.logs.append(f"Warning: {warning}")
                for warning in result.get("plan", {}).get("warnings", []):
                    stage.logs.append(f"Plan warning: {warning}")
                if stage.name == "build_rough_cut":
                    stage.logs.append("Rough-cut preview is intermediate; the final workflow preview is rendered in stage 12.")
                if stage.name == "plan_edit_refinement":
                    stage.logs.append("Using the conservative default refinement policy authorized by auto_edit_project.")
                stage.status = "completed"
            except Exception as exc:
                error = exc.to_dict() if callable(getattr(exc, "to_dict", None)) else {"code": "stage_failed", "message": str(exc)}
                error = {**error, "stage": stage.name}
                stage.status, stage.errors = "failed", [error]
                stage.logs.append(f"Failed: {error['message']}")
                report.failed_stage, report.error, report.status = stage.name, error, "failed"
                for pending in report.stages:
                    if pending.status == "pending":
                        pending.status = "blocked"
                        pending.logs.append(f"Not run because {stage.name} failed.")
                        pending.errors.append({"code": "stage_blocked", "message": f"Blocked by {stage.name}", "stage": stage.name})
            stage.finished_at = _now()
            checkpoint()
            if stage.status == "failed":
                break
        if report.failed_stage is None:
            report.status, report.success = "completed", True
            report.preview_path = report.stages[-1].result["preview_path"]
        report.finished_at = _now()
        checkpoint()
        return report.model_dump(mode="json")
    finally:
        lock.unlink(missing_ok=True)
