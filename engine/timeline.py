"""Timeline validation and readable, atomic local JSON persistence."""

import json
import os
import tempfile
from pathlib import Path, PureWindowsPath

from pydantic import ValidationError

from schemas.project import Project, validate_project_name
from schemas.timeline import Timeline, TimelineIssue, TimelineValidation
from services.project_service import PROJECTS_ROOT


class TimelineError(Exception):
    def __init__(self, code, message, issues=None):
        super().__init__(message)
        self.code = code
        self.issues = issues or []

    def to_dict(self):
        return {"code": self.code, "message": str(self),
                "errors": [issue.model_dump() for issue in self.issues]}


def _native_path(value) -> Path:
    if os.name != "nt" and PureWindowsPath(str(value)).drive:
        raise ValueError("Windows drive and UNC paths require a Windows host")
    return Path(value).expanduser()


def _project_context(project: Project | str | Path) -> tuple[Path, Project]:
    try:
        if isinstance(project, Project):
            folder = PROJECTS_ROOT / validate_project_name(project.name)
        elif isinstance(project, str) and not any(char in project for char in "/\\:"):
            folder = PROJECTS_ROOT / validate_project_name(project)
        else:
            folder = _native_path(project)
        folder = folder.resolve(strict=True)
        metadata = Project.model_validate_json((folder / "project.json").read_text(encoding="utf-8"))
        if isinstance(project, Project) and metadata.name != project.name:
            raise ValueError("Project identity does not match project.json")
        return folder, metadata
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        raise TimelineError("invalid_project", str(exc)) from exc


def create_empty_timeline(project: Project | str | Path) -> Timeline:
    """Return an empty timeline; persistence is explicit through save_timeline."""
    metadata = project if isinstance(project, Project) else _project_context(project)[1]
    try:
        return Timeline(project=metadata.name, fps=metadata.fps,
                        width=metadata.resolution.width, height=metadata.resolution.height)
    except ValidationError as exc:
        raise TimelineError("invalid_project", str(exc)) from exc


def validate_timeline(timeline: Timeline | dict, *, base_dir: str | Path | None = None) -> TimelineValidation:
    """Check intent plus every source, including disabled clips.

    Relative sources resolve against base_dir, or cwd for standalone validation.
    On POSIX, Windows paths report unsupported_path rather than being reinterpreted.
    """
    try:
        data = timeline.model_dump() if isinstance(timeline, Timeline) else timeline
        parsed = Timeline.model_validate(data)
    except ValidationError as exc:
        issues = [TimelineIssue(code=error["type"], location=list(error["loc"]), message=error["msg"])
                  for error in exc.errors()]
        return TimelineValidation(valid=False, errors=issues)
    errors = []
    try:
        root = _native_path(base_dir) if base_dir is not None else Path.cwd()
    except (ValueError, TypeError) as exc:
        return TimelineValidation(valid=False, errors=[TimelineIssue(code="invalid_base_dir", message=str(exc))])
    for collection in ("video_tracks", "audio_tracks", "music_tracks", "sfx_tracks"):
        for track_index, track in enumerate(getattr(parsed, collection)):
            for clip_index, clip in enumerate(track.clips):
                location = [collection, track_index, "clips", clip_index, "source"]
                try:
                    path = _native_path(clip.source)
                    if not path.is_absolute():
                        path = root / path
                    if not path.resolve(strict=True).is_file():
                        raise FileNotFoundError("Source is not a regular file")
                except (OSError, ValueError, RuntimeError) as exc:
                    code = "unsupported_path" if os.name != "nt" and PureWindowsPath(clip.source).drive else "missing_source"
                    errors.append(TimelineIssue(code=code, location=location, message=str(exc)))
    return TimelineValidation(valid=not errors, errors=errors)


def _validated(timeline, folder, metadata) -> Timeline:
    validation = validate_timeline(timeline, base_dir=folder)
    if not validation.valid:
        raise TimelineError("invalid_timeline", "Timeline validation failed", validation.errors)
    parsed = Timeline.model_validate(timeline.model_dump() if isinstance(timeline, Timeline) else timeline)
    if parsed.project != metadata.name:
        raise TimelineError("project_mismatch", "Timeline belongs to a different project")
    return parsed


def load_timeline(project: Project | str | Path) -> Timeline:
    folder, metadata = _project_context(project)
    try:
        data = json.loads((folder / "timeline.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TimelineError("timeline_read_failed", str(exc)) from exc
    return _validated(data, folder, metadata)


def save_timeline(project: Project | str | Path, timeline: Timeline | dict) -> Path:
    folder, metadata = _project_context(project)
    parsed = _validated(timeline, folder, metadata)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder,
                                         prefix=".timeline-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(parsed.model_dump_json(indent=2) + "\n")
        destination = folder / "timeline.json"
        temporary.replace(destination)
        return destination
    except (OSError, ValueError) as exc:
        raise TimelineError("timeline_write_failed", str(exc)) from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
