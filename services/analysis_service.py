"""Validate and atomically persist semantic source analysis, without editing."""

import json
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from analysis.provider import AnalysisProvider, SourceContext
from engine.media import _existing_path
from schemas.project import Project
from schemas.source_analysis import SourceAnalysis, SourceId


class AnalysisError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def to_dict(self) -> dict:
        return {"code": self.code, "message": str(self)}


class _IndexedSource(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: SourceId
    path: str = Field(min_length=1)
    duration: float = Field(gt=0, allow_inf_nan=False)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: float = Field(ge=0, allow_inf_nan=False)


def _context(project: str | Path, source_id: str) -> tuple[Path, _IndexedSource]:
    try:
        TypeAdapter(SourceId).validate_python(source_id)
    except ValueError as exc:
        raise AnalysisError("invalid_source_id", str(exc)) from exc
    try:
        folder = _existing_path(project, directory=True)
        Project.model_validate_json((folder / "project.json").read_text(encoding="utf-8"))
        document = json.loads((folder / "source_index.json").read_text(encoding="utf-8"))
        sources = document["sources"]
        if not isinstance(sources, list) or any(not isinstance(item, dict) for item in sources):
            raise ValueError("source_index.json must contain a sources list of objects")
        matches = [item for item in sources if item.get("id") == source_id]
        if not matches:
            raise AnalysisError("source_not_found", "Source ID is not indexed; run analyze_folder first")
        if len(matches) != 1:
            raise ValueError("Duplicate source ID in source_index.json")
        return folder, _IndexedSource.model_validate(matches[0])
    except AnalysisError:
        raise
    except Exception as exc:
        raise AnalysisError("analysis_context_invalid", f"Cannot load project source index: {exc}") from exc


def _validate(value, source: _IndexedSource) -> SourceAnalysis:
    try:
        # Revalidate model instances too, including instances constructed without validation.
        analysis = SourceAnalysis.model_validate(value.model_dump() if isinstance(value, SourceAnalysis) else value)
        if analysis.source_id != source.id:
            raise ValueError("Analysis source_id does not match the requested source")
        if analysis.usable_end is not None and analysis.usable_end > source.duration:
            raise ValueError("usable_end exceeds indexed source duration")
        return analysis
    except (ValueError, TypeError) as exc:
        raise AnalysisError("invalid_source_analysis", str(exc)) from exc


def analyze_source(project: str | Path, source_id: str, *, provider: AnalysisProvider | None = None) -> dict:
    folder, source = _context(project, source_id)
    if provider is None:
        raise AnalysisError("analysis_provider_not_configured", "Supply semantic observations or inject an AnalysisProvider; no default model is configured")
    try:
        path = _existing_path(source.path)
    except Exception as exc:
        raise AnalysisError("source_unavailable", str(exc)) from exc
    context = SourceContext(source.id, path, source.duration, source.width, source.height, source.fps)
    try:
        observations = provider.analyze(context)
    except Exception as exc:
        raise AnalysisError("analysis_provider_failed", f"Source analysis provider failed: {exc}") from exc
    analysis = _validate(observations, source)
    destination = folder / "analysis" / f"{source.id}.json"
    temporary = None
    try:
        destination.parent.mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                         prefix=".analysis-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(analysis.model_dump_json(indent=2) + "\n")
        temporary.replace(destination)
    except OSError as exc:
        raise AnalysisError("analysis_write_failed", f"Cannot save source analysis: {exc}") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # Preserve the structured write failure if scratch cleanup fails.
    return {"analysis": analysis.model_dump(mode="json"), "analysis_path": str(destination)}


def get_source_analysis(project: str | Path, source_id: str) -> dict:
    folder, source = _context(project, source_id)
    path = folder / "analysis" / f"{source.id}.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AnalysisError("analysis_not_found", f"No saved semantic analysis for source {source_id}") from exc
    except (OSError, ValueError) as exc:
        raise AnalysisError("analysis_read_failed", f"Cannot read source analysis: {exc}") from exc
    analysis = _validate(document, source)
    return {"analysis": analysis.model_dump(mode="json"), "analysis_path": str(path)}
