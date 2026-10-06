"""Create local projects without modifying source media or existing projects."""

import json
import os
import shutil
from pathlib import Path, PureWindowsPath

from schemas.project import Project, ProjectError, ProjectResult, validate_project_name
from schemas.timeline import Timeline

PROJECTS_ROOT = Path(__file__).resolve().parents[1] / "projects"
PROJECT_DIRECTORIES = ("cache", "analysis", "subtitles", "preview", "output")


def _failure(code: str, message: str) -> ProjectResult:
    return ProjectResult(success=False, error=ProjectError(code=code, message=message))


def get_project(name: str, *, projects_root: str | Path | None = None) -> ProjectResult:
    """Load project metadata by its validated name without changing any files."""
    try:
        validate_project_name(name)
    except ValueError as exc:
        return _failure("invalid_project_name", str(exc))
    try:
        root = Path(projects_root) if projects_root is not None else PROJECTS_ROOT
        if os.name != "nt" and PureWindowsPath(str(root)).drive:
            return _failure("unsupported_path", "Windows project roots require a Windows host.")
        target = root.expanduser().resolve() / name
        metadata = Project.model_validate_json((target / "project.json").read_text(encoding="utf-8"))
        if metadata.name != name:
            return _failure("project_mismatch", "Project name does not match its metadata.")
        return ProjectResult(success=True, project=metadata, project_path=target)
    except FileNotFoundError:
        return _failure("project_not_found", f"Project not found: {name}")
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        return _failure("project_read_failed", f"Cannot read project: {exc}")


def create_project(
    name: str, source_folder: str | Path, *, projects_root: str | Path | None = None
) -> ProjectResult:
    """Create metadata and empty indexes; return a structured result on failure.

    Native Windows drive and UNC paths are handled by pathlib on Windows.
    Foreign Windows paths on POSIX are rejected rather than misinterpreted as
    relative paths. The optional root supports isolated tests and local storage.
    """
    try:
        validate_project_name(name)
    except ValueError as exc:
        return _failure("invalid_project_name", str(exc))

    try:
        if not isinstance(source_folder, (str, Path)) or not str(source_folder).strip():
            return _failure("invalid_source_folder", "Source folder must be a nonempty path.")
        if os.name != "nt" and PureWindowsPath(str(source_folder)).drive:
            return _failure("unsupported_path", "Windows drive and UNC paths require a Windows host.")
        source = Path(source_folder).expanduser().resolve(strict=True)
        if not source.is_dir():
            return _failure("invalid_source_folder", "Source path must be a directory.")
        # Opening a directory iterator also detects inaccessible source directories.
        with os.scandir(source) as entries:
            next(entries, None)
    except (OSError, ValueError, RuntimeError) as exc:
        return _failure("invalid_source_folder", f"Cannot access source folder: {exc}")

    target = None
    created = False
    try:
        root = Path(projects_root) if projects_root is not None else PROJECTS_ROOT
        if os.name != "nt" and PureWindowsPath(str(root)).drive:
            return _failure("unsupported_path", "Windows project roots require a Windows host.")
        root = root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        target = root / name
        # Exclusive creation also protects existing files, directories and symlinks.
        try:
            target.mkdir()
        except FileExistsError:
            return _failure("project_exists", f"Project already exists: {target}")
        created = True
        project = Project(name=name, source_folder=str(source))
        for directory in PROJECT_DIRECTORIES:
            (target / directory).mkdir()
        documents = {
            "project.json": project.model_dump(mode="json"),
            "source_index.json": {"version": 1, "sources": []},
            "timeline.json": Timeline(
                project=project.name, fps=project.fps,
                width=project.resolution.width, height=project.resolution.height,
            ).model_dump(mode="json"),
        }
        for filename, document in documents.items():
            with (target / filename).open("x", encoding="utf-8") as handle:
                json.dump(document, handle, ensure_ascii=False, indent=2, allow_nan=False)
                handle.write("\n")
        return ProjectResult(success=True, project=project, project_path=target)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        message = f"Cannot create project: {exc}"
        if created and target is not None:
            try:
                shutil.rmtree(target)
            except OSError as cleanup_error:
                message += f"; incomplete project remains at {target}: {cleanup_error}"
        return _failure("project_creation_failed", message)
