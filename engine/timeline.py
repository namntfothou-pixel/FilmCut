"""Timeline validation and readable, atomic local JSON persistence."""

import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from uuid import uuid4

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


@contextmanager
def timeline_lock(folder: Path):
    """Serialize local writes and complete read/modify/write operations."""
    lock = folder / ".timeline.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise TimelineError("timeline_busy", "Another timeline write is in progress. Retry after it finishes; inspect a stale .timeline.lock before removing it.") from exc
    except OSError as exc:
        raise TimelineError("timeline_lock_failed", str(exc)) from exc
    try:
        os.close(descriptor)
        yield
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise TimelineError("timeline_lock_cleanup_failed", f"Timeline lock could not be removed: {exc}") from exc


def save_locked_timeline(folder: Path, metadata: Project, timeline: Timeline | dict) -> tuple[Path, Path | None]:
    """Save under timeline_lock, backing up the exact previous JSON first."""
    parsed = _validated(timeline, folder, metadata)
    temporary = None
    backup = None
    try:
        destination = folder / "timeline.json"
        if destination.exists():
            history = folder / "timeline_history"
            if history.is_symlink():
                raise ValueError("timeline_history cannot be a symlink")
            history.mkdir(exist_ok=True)
            previous = destination.read_bytes()
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            backup = history / f"{stamp}-{uuid4().hex}.json"
            with backup.open("xb") as handle:
                handle.write(previous)
                handle.flush()
                os.fsync(handle.fileno())
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder,
                                         prefix=".timeline-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(parsed.model_dump_json(indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
        return destination, backup
    except (OSError, ValueError) as exc:
        if backup is not None:
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                pass
        raise TimelineError("timeline_write_failed", str(exc)) from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def save_timeline(project: Project | str | Path, timeline: Timeline | dict) -> Path:
    """Validate, snapshot the previous timeline, then atomically replace it."""
    folder, metadata = _project_context(project)
    with timeline_lock(folder):
        return save_locked_timeline(folder, metadata, timeline)[0]
