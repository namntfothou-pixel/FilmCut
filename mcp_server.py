"""Local FilmCut FastMCP stdio adapter; stdout is reserved for MCP messages."""

import logging
import os
from functools import wraps
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from engine import media, render, timeline
from services import project_service, timeline_service, music_service, sfx_service, subtitle_service

logger = logging.getLogger("filmcut.mcp")


class ToolFailure(Exception):
    def __init__(self, error):
        super().__init__(error["message"])
        self.error = error

    def to_dict(self):
        return self.error


def _success(data):
    return {"success": True, "data": data, "error": None}


def _guard(function):
    """Contain project failures, including unexpected service exceptions."""
    @wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception as exc:
            if callable(getattr(exc, "to_dict", None)):
                error = exc.to_dict()
            else:
                logger.exception("FilmCut tool %s failed", function.__name__)
                error = {"code": "internal_error", "message": f"{function.__name__} failed: {exc}"}
            return {"success": False, "data": None, "error": error}
    return guarded


def build_server(projects_root: Path | None = None, sfx_library_root: Path | None = None) -> FastMCP:
    """Allow an isolated project root for tests, using the same existing services."""
    server = FastMCP("FilmCut", log_level="WARNING", instructions=(
        "Local video editing engine. Project arguments are project names. "
        "timeline.json is the source of truth. create_timeline never resets an existing timeline."
    ))

    def require_project(name):
        result = project_service.get_project(name, projects_root=projects_root)
        if not result.success:
            raise ToolFailure(result.error.model_dump(mode="json"))
        return result

    @server.tool(structured_output=True)
    @_guard
    def ping() -> dict[str, Any]:
        """Check FilmCut MCP connectivity."""
        return _success({"server": "FilmCut", "transport": "stdio", "status": "ok"})

    @server.tool(structured_output=True)
    @_guard
    def create_project(name: str, source_folder: str) -> dict[str, Any]:
        """Create a project without overwriting existing projects or source media."""
        result = project_service.create_project(name, source_folder, projects_root=projects_root)
        if not result.success:
            raise ToolFailure(result.error.model_dump(mode="json"))
        return _success({"project": result.project.model_dump(mode="json"), "project_path": str(result.project_path)})

    @server.tool(structured_output=True)
    @_guard
    def get_project(name: str) -> dict[str, Any]:
        """Read project metadata by project name."""
        result = require_project(name)
        return _success({"project": result.project.model_dump(mode="json"), "project_path": str(result.project_path)})

    @server.tool(structured_output=True)
    @_guard
    def analyze_folder(project: str) -> dict[str, Any]:
        """Analyze a project's configured source folder and save its source index."""
        result = require_project(project)
        scan = media.scan_folder(result.project.source_folder)
        path = media.write_source_index(result.project_path, scan)
        return _success({**scan, "complete": not scan["errors"], "source_index_path": str(path)})

    @server.tool(structured_output=True)
    @_guard
    def get_timeline(project: str) -> dict[str, Any]:
        """Load and validate the project's saved editing intent."""
        result = require_project(project)
        saved = timeline.load_timeline(result.project_path)
        return _success({"timeline": saved.model_dump(mode="json")})

    @server.tool(structured_output=True)
    @_guard
    def create_timeline(project: str) -> dict[str, Any]:
        """Initialize a missing timeline; preserve and return an existing timeline."""
        result = require_project(project)
        path = result.project_path / "timeline.json"
        if path.exists() or path.is_symlink():
            saved = timeline.load_timeline(result.project_path)
            created = False
        else:
            saved = timeline.create_empty_timeline(result.project)
            timeline.save_timeline(result.project_path, saved)
            created = True
        return _success({"timeline": saved.model_dump(mode="json"), "created": created})

    @server.tool(structured_output=True)
    @_guard
    def render_preview(project: str, burn_subtitles: bool | None = None) -> dict[str, Any]:
        """Render preview; optionally burn the enabled subtitle track with plain styling."""
        result = require_project(project)
        path = render.render_timeline(result.project_path, burn_subtitles=burn_subtitles)
        return _success({"preview_path": str(path), "metadata": media.probe_media(path)})

    @server.tool(structured_output=True)
    @_guard
    def add_clip(project: str, source: str, source_in: float, source_out: float, position: float) -> dict[str, Any]:
        """Add a video clip at position in seconds; preserve other positions."""
        result = require_project(project)
        return _success(timeline_service.add_clip(result.project_path, source, source_in, source_out, position))

    @server.tool(structured_output=True)
    @_guard
    def remove_clip(project: str, clip_id: str) -> dict[str, Any]:
        """Remove a video timeline entry without changing any media files."""
        result = require_project(project)
        return _success(timeline_service.remove_clip(result.project_path, clip_id))

    @server.tool(structured_output=True)
    @_guard
    def trim_clip(project: str, clip_id: str, source_in: float, source_out: float) -> dict[str, Any]:
        """Update source trim times; validate and back up before saving."""
        result = require_project(project)
        return _success(timeline_service.trim_clip(result.project_path, clip_id, source_in, source_out))

    @server.tool(structured_output=True)
    @_guard
    def move_clip(project: str, clip_id: str, position: float) -> dict[str, Any]:
        """Move a video clip to seconds; reject prohibited overlaps."""
        result = require_project(project)
        return _success(timeline_service.move_clip(result.project_path, clip_id, position))

    @server.tool(structured_output=True)
    @_guard
    def set_clip_speed(project: str, clip_id: str, speed: float) -> dict[str, Any]:
        """Set positive playback speed in timeline intent; do not render media."""
        result = require_project(project)
        return _success(timeline_service.set_clip_speed(result.project_path, clip_id, speed))

    @server.tool(structured_output=True)
    @_guard
    def add_music(project: str, file: str, timeline_start: float = 0, source_in: float = 0,
                  source_out: float | None = None, volume_db: float = -18, fade_in: float = 0,
                  fade_out: float = 0, loop: bool = False, enabled: bool = True) -> dict[str, Any]:
        """Manually add BGM; loop repeats its selected interval to video end."""
        result = require_project(project)
        return _success(music_service.add_music(result.project_path, file, timeline_start, source_in,
                                               source_out, volume_db, fade_in, fade_out, loop, enabled))

    @server.tool(structured_output=True)
    @_guard
    def remove_music(project: str, music_id: str) -> dict[str, Any]:
        """Remove music intent without deleting source media."""
        result = require_project(project)
        return _success(music_service.remove_music(result.project_path, music_id))

    @server.tool(structured_output=True)
    @_guard
    def update_music(project: str, music_id: str, file: str | None = None,
                     timeline_start: float | None = None, source_in: float | None = None,
                     source_out: float | None = None, volume_db: float | None = None,
                     fade_in: float | None = None, fade_out: float | None = None,
                     loop: bool | None = None, enabled: bool | None = None) -> dict[str, Any]:
        """Update only supplied music fields; validate, back up and save."""
        result = require_project(project)
        return _success(music_service.update_music(result.project_path, music_id, file=file,
            timeline_start=timeline_start, source_in=source_in, source_out=source_out,
            volume_db=volume_db, fade_in=fade_in, fade_out=fade_out, loop=loop, enabled=enabled))

    @server.tool(structured_output=True)
    @_guard
    def add_sfx(project: str, file: str, timeline_time: float, source_in: float = 0,
                volume_db: float = 0, fade_in: float = 0, fade_out: float = 0,
                enabled: bool = True, tags: list[str] | None = None) -> dict[str, Any]:
        """Manually place an SFX event at seconds; simultaneous events are allowed."""
        result = require_project(project)
        return _success(sfx_service.add_sfx(result.project_path, file, timeline_time, source_in,
                                          volume_db, fade_in, fade_out, enabled, tags))

    @server.tool(structured_output=True)
    @_guard
    def remove_sfx(project: str, sfx_id: str) -> dict[str, Any]:
        """Remove an event without deleting its audio file."""
        result = require_project(project)
        return _success(sfx_service.remove_sfx(result.project_path, sfx_id))

    @server.tool(structured_output=True)
    @_guard
    def update_sfx(project: str, sfx_id: str, file: str | None = None,
                   timeline_time: float | None = None, source_in: float | None = None,
                   volume_db: float | None = None, fade_in: float | None = None,
                   fade_out: float | None = None, enabled: bool | None = None,
                   tags: list[str] | None = None) -> dict[str, Any]:
        """Update supplied SFX fields with validation and automatic timeline history."""
        result = require_project(project)
        return _success(sfx_service.update_sfx(result.project_path, sfx_id, file=file,
            timeline_time=timeline_time, source_in=source_in, volume_db=volume_db,
            fade_in=fade_in, fade_out=fade_out, enabled=enabled, tags=tags))

    @server.tool(structured_output=True)
    @_guard
    def list_sfx_library() -> dict[str, Any]:
        """List the local catalog with normalized tags and asset availability."""
        return _success(sfx_service.list_sfx_library(library_root=sfx_library_root))

    @server.tool(structured_output=True)
    @_guard
    def search_sfx_by_tags(tags: list[str], match_all: bool = True) -> dict[str, Any]:
        """Search available SFX by all tags (default) or any tag, without AI."""
        return _success(sfx_service.search_sfx_by_tags(tags, match_all, library_root=sfx_library_root))

    @server.tool(structured_output=True)
    @_guard
    def generate_subtitles(project: str, language: str) -> dict[str, Any]:
        """Transcribe rendered dialogue locally (en/vi), save UTF-8 SRT and timeline reference."""
        result = require_project(project)
        return _success(subtitle_service.generate_subtitles(result.project_path, language))

    return server


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)  # Logging goes to stderr.
    root = os.environ.get("FILMCUT_PROJECTS_ROOT")
    library = os.environ.get("FILMCUT_SFX_LIBRARY")
    build_server(Path(root) if root else None, Path(library) if library else None).run(transport="stdio")
