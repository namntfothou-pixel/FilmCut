"""Analyze scripts and store requirements without touching timeline or media."""

import json
import tempfile
from pathlib import Path

from analysis.script_provider import LocalScriptParser, ScriptAnalysisProvider, ScriptParseError
from engine.media import _existing_path
from schemas.project import Project
from schemas.script import ScriptBreakdown
from services.analysis_service import AnalysisError

MAX_SCRIPT_CHARACTERS = 1_000_000


def _folder(project):
    try:
        folder = _existing_path(project, directory=True)
        Project.model_validate_json((folder / "project.json").read_text(encoding="utf-8"))
        return folder
    except Exception as exc:
        raise AnalysisError("invalid_project", f"Cannot load script project: {exc}") from exc


def _validate(value):
    try:
        return ScriptBreakdown.model_validate(value.model_dump() if isinstance(value, ScriptBreakdown) else value)
    except (ValueError, TypeError) as exc:
        raise AnalysisError("invalid_script_breakdown", str(exc)) from exc


def analyze_script(project: str | Path, script: str, *, provider: ScriptAnalysisProvider | None = None,
                   breakdown: dict | None = None) -> dict:
    folder = _folder(project)
    if not isinstance(script, str) or not script.strip() or len(script) > MAX_SCRIPT_CHARACTERS:
        raise AnalysisError("invalid_script", f"Provide nonempty script text up to {MAX_SCRIPT_CHARACTERS} characters")
    if breakdown is None:
        try:
            breakdown = (provider if provider is not None else LocalScriptParser()).analyze(script)
        except ScriptParseError as exc:
            raise AnalysisError("unsupported_script_format", str(exc)) from exc
        except Exception as exc:
            raise AnalysisError("script_analysis_failed", f"Script analyzer failed: {exc}") from exc
    validated = _validate(breakdown)
    destination = folder / "analysis" / "script_breakdown.json"
    temporary = None
    try:
        destination.parent.mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                         prefix=".script-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(validated.model_dump_json(indent=2) + "\n")
        temporary.replace(destination)
    except OSError as exc:
        raise AnalysisError("script_write_failed", str(exc)) from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return {"breakdown": validated.model_dump(mode="json"), "breakdown_path": str(destination)}


def get_script_breakdown(project: str | Path) -> dict:
    path = _folder(project) / "analysis" / "script_breakdown.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AnalysisError("script_breakdown_not_found", "Analyze a script first") from exc
    except (OSError, ValueError) as exc:
        raise AnalysisError("script_read_failed", str(exc)) from exc
    return {"breakdown": _validate(value).model_dump(mode="json"), "breakdown_path": str(path)}
