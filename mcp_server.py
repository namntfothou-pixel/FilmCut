"""Local FilmCut FastMCP stdio adapter; stdout is reserved for MCP messages."""

import logging
import os
from functools import wraps
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from engine import media, render, timeline
from services import project_service, timeline_service, music_service, sfx_service, subtitle_service
from services import analysis_service
from services import script_service
from services import matching_service
from services import rough_cut_service
from services import sound_service
from services import refinement_service
from services import orchestration_service
from services import qc_service
from analysis.provider import AnalysisProvider, SuppliedAnalysisProvider
from analysis.script_provider import ScriptAnalysisProvider

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


def build_server(projects_root: Path | None = None, sfx_library_root: Path | None = None,
                 *, analysis_provider: AnalysisProvider | None = None,
                 script_provider: ScriptAnalysisProvider | None = None,
                 music_library_root: Path | None = None) -> FastMCP:
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
    def auto_edit_project(project: str) -> dict[str, Any]:
        """Run analysis through a subtitled preview with stage reports; never export final.

        Uses saved semantic analysis or the injected provider, script.txt or a
        saved breakdown, tagged local sound libraries, and configured Whisper.
        Calling this authorizes conservative default refinement and timeline edits.
        Failure preserves completed work and identifies blocked downstream stages.
        """
        result = require_project(project)
        report = orchestration_service.auto_edit_project(result.project_path,
            analysis_provider=analysis_provider, script_provider=script_provider,
            music_library_root=music_library_root, sfx_library_root=sfx_library_root)
        return {"success": report["success"], "data": report, "error": report["error"]}

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
    def analyze_source(project: str, source_id: str, analysis: dict[str, Any] | None = None) -> dict[str, Any]:
        """Save validated semantic observations or request the configured analysis provider.

        Supply all SourceAnalysis fields: source_id, characters, location,
        shot_size, camera_angle, camera_motion, action, emotion, dialogue,
        visual_quality, continuity_notes, usable_start, usable_end, problems,
        description. Characters/notes/problems are string lists; unknown text
        and both usable bounds may be null. Bounds are source-relative seconds.
        This never modifies the timeline or renders media.
        """
        result = require_project(project)
        provider = SuppliedAnalysisProvider(analysis) if analysis is not None else analysis_provider
        return _success(analysis_service.analyze_source(result.project_path, source_id, provider=provider))

    @server.tool(structured_output=True)
    @_guard
    def get_source_analysis(project: str, source_id: str) -> dict[str, Any]:
        """Read saved semantic source metadata without invoking a model."""
        result = require_project(project)
        return _success(analysis_service.get_source_analysis(result.project_path, source_id))

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
    def qc_project(project: str) -> dict[str, Any]:
        """Run timeline, media, subtitle, black-frame, audio-peak and output QC; save qc_report.json."""
        result = require_project(project)
        return _success(qc_service.qc_project(result.project_path))

    @server.tool(structured_output=True)
    @_guard
    def export_final(project: str, force: bool = False) -> dict[str, Any]:
        """QC and export H.264/AAC MP4. QC must pass unless force is true."""
        result = require_project(project)
        return _success(qc_service.export_final(result.project_path, force=force))

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
                  fade_out: float = 0, loop: bool = False, enabled: bool = True, end: float | None = None) -> dict[str, Any]:
        """Manually add BGM; loop to video end or optional explicit end timestamp."""
        result = require_project(project)
        return _success(music_service.add_music(result.project_path, file, timeline_start, source_in,
                                               source_out, volume_db, fade_in, fade_out, loop, enabled, end))

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
                     loop: bool | None = None, enabled: bool | None = None, end: float | None = None) -> dict[str, Any]:
        """Update only supplied music fields; validate, back up and save."""
        result = require_project(project)
        return _success(music_service.update_music(result.project_path, music_id, file=file,
            timeline_start=timeline_start, source_in=source_in, source_out=source_out,
            volume_db=volume_db, fade_in=fade_in, fade_out=fade_out, loop=loop, enabled=enabled, end=end))

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

    @server.tool(structured_output=True)
    @_guard
    def set_transition(project: str, clip_id: str, transition_type: str, duration: float) -> dict[str, Any]:
        """Set cut/crossfade/fade_to_black after a clip; shift later video positions and back up the timeline."""
        result = require_project(project)
        return _success(timeline_service.set_transition(result.project_path, clip_id, transition_type, duration))

    @server.tool(structured_output=True)
    @_guard
    def set_j_cut(project: str, clip_id: str, duration: float) -> dict[str, Any]:
        """Lead incoming source dialogue by duration seconds using pre-roll; preserve video and lip sync."""
        result = require_project(project)
        return _success(timeline_service.set_j_cut(result.project_path, clip_id, duration))

    @server.tool(structured_output=True)
    @_guard
    def set_l_cut(project: str, clip_id: str, duration: float) -> dict[str, Any]:
        """Extend outgoing source dialogue into following picture using post-roll; preserve video timing."""
        result = require_project(project)
        return _success(timeline_service.set_l_cut(result.project_path, clip_id, duration))

    @server.tool(structured_output=True)
    @_guard
    def reset_audio_offset(project: str, clip_id: str) -> dict[str, Any]:
        """Reset the three independent audio fields to follow the video, with timeline history."""
        result = require_project(project)
        return _success(timeline_service.reset_audio_offset(result.project_path, clip_id))

    @server.tool(structured_output=True)
    @_guard
    def analyze_script(project: str, script: str, breakdown: dict[str, Any] | None = None) -> dict[str, Any]:
        """Convert script text to scene requirements and save script_breakdown.json.

        Offline parser accepts INT./EXT., SCENE 1, SHOT 1, or CẢNH 1 headings,
        uppercase speaker cues, and explicit Characters/Location/Action/Emotion/
        Dialogue/Shot size/Continuity/Duration/Notes labels. Unknown semantics
        remain null. Optionally supply a ScriptBreakdown object (version and
        requirements) from a model. Does not create or modify a timeline.
        """
        result = require_project(project)
        return _success(script_service.analyze_script(result.project_path, script,
            provider=script_provider, breakdown=breakdown))

    @server.tool(structured_output=True)
    @_guard
    def get_script_breakdown(project: str) -> dict[str, Any]:
        """Read validated scene requirements without invoking a script analyzer."""
        result = require_project(project)
        return _success(script_service.get_script_breakdown(result.project_path))

    @server.tool(structured_output=True)
    @_guard
    def find_candidates_for_scene(project: str, scene_id: str, limit: int = 5) -> dict[str, Any]:
        """Rank indexed sources for a saved scene using eight explained lexical scores.

        Returns component evidence, weights, contributions, and skipped-source
        warnings. Scores are suitability heuristics, not probabilities. Does not
        select footage, invoke a model, or modify the timeline.
        """
        result = require_project(project)
        return _success(matching_service.find_candidates_for_scene(result.project_path, scene_id, limit))

    @server.tool(structured_output=True)
    @_guard
    def rank_sources_for_script(project: str, limit: int = 5) -> dict[str, Any]:
        """Return explained candidates for every saved scene in story order; no editing."""
        result = require_project(project)
        return _success(matching_service.rank_sources_for_script(result.project_path, limit))

    @server.tool(structured_output=True)
    @_guard
    def build_rough_cut(project: str) -> dict[str, Any]:
        """Build a video-only rough cut from saved script/analyses and render a preview.

        Follow story order, prefer ranked unused sources, use frame-aligned
        usable intervals, and fill scene durations. Cut transitions only.
        Replaces the timeline after successful staged rendering, with history.
        Returns an edit-decision report; no automatic music, SFX or subtitles.
        """
        result = require_project(project)
        return _success(rough_cut_service.build_rough_cut(result.project_path))

    @server.tool(structured_output=True)
    @_guard
    def plan_music(project: str, intents: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Save music intent and tag-matched local files in SoundPlan; no timeline changes.

        Optional provider-neutral intents use mood, energy, start/end, recommended_tags,
        ducking {enabled, attenuation_db, mode: cue_gain}, fade_in/out.
        Ducking is a static gain reduction for the cue, not inferred word timing.
        """
        result = require_project(project)
        return _success(sound_service.plan_music(result.project_path, library_root=music_library_root, intents=intents))

    @server.tool(structured_output=True)
    @_guard
    def apply_music_plan(project: str) -> dict[str, Any]:
        """Validate saved plan/assets and apply visible music entries with timeline history."""
        result = require_project(project)
        return _success(sound_service.apply_music_plan(result.project_path, library_root=music_library_root))

    @server.tool(structured_output=True)
    @_guard
    def plan_sfx(project: str, intents: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Save SFX decisions choosing existing tagged files. No audio generation.

        Optional intents: event, timestamp, tags, intensity [0,1], timing exact/approximate.
        Default action rules mark shot-onset timing approximate; supply exact
        timestamped intents for precise sync. Does not modify timeline.json.
        """
        result = require_project(project)
        return _success(sound_service.plan_sfx(result.project_path, library_root=sfx_library_root, intents=intents))

    @server.tool(structured_output=True)
    @_guard
    def apply_sfx_plan(project: str) -> dict[str, Any]:
        """Validate saved plan/assets and apply visible SFX entries with timeline history."""
        result = require_project(project)
        return _success(sound_service.apply_sfx_plan(result.project_path, library_root=sfx_library_root))

    @server.tool(structured_output=True)
    @_guard
    def plan_edit_refinement(project: str, recommendations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Save an inspectable conservative boundary plan; timeline remains unchanged.

        Defaults to hard cuts. Narrative notes justify sparse blends; dialogue
        flow and source handles justify J/L audio edits. Optional recommendations:
        from_clip, to_clip, type (hard_cut/crossfade/j_cut/l_cut/fade_to_black),
        duration, enabled, reason, evidence. Uses existing edit validation.
        """
        result = require_project(project)
        return _success(refinement_service.plan_edit_refinement(result.project_path, recommendations=recommendations))

    @server.tool(structured_output=True)
    @_guard
    def apply_edit_refinement(project: str) -> dict[str, Any]:
        """Apply the saved inspected plan atomically with timeline history; render preview afterward."""
        result = require_project(project)
        return _success(refinement_service.apply_edit_refinement(result.project_path))

    return server


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)  # Logging goes to stderr.
    root = os.environ.get("FILMCUT_PROJECTS_ROOT")
    library = os.environ.get("FILMCUT_SFX_LIBRARY")
    music = os.environ.get("FILMCUT_MUSIC_LIBRARY")
    build_server(Path(root) if root else None, Path(library) if library else None,
                 music_library_root=Path(music) if music else None).run(transport="stdio")
