"""Manual SFX events and deterministic local tag catalog queries."""

from pathlib import Path
from uuid import uuid4

from engine.audio import validate_sfx_source
from engine.timeline import TimelineError
from schemas.sfx import SFXLibrary, normalize_tags
from schemas.timeline import SFXClip, SFXTrack
from services.timeline_service import _edit

SFX_LIBRARY_ROOT = Path(__file__).resolve().parents[1] / "assets" / "sfx"


def list_sfx_library(*, library_root: Path | None = None):
    try:
        root = (library_root if library_root is not None else SFX_LIBRARY_ROOT).resolve(strict=True)
        catalog = SFXLibrary.model_validate_json((root / "library.json").read_text(encoding="utf-8"))
        items, errors = [], []
        for entry in sorted(catalog.items, key=lambda item: (Path(item.file).name.casefold(), item.file)):
            path = (root / entry.file).resolve()
            if not path.is_relative_to(root):
                raise ValueError(f"SFX catalog file escapes library: {entry.file}")
            available = path.is_file()
            items.append({**entry.model_dump(mode="json"), "file": str(path),
                          "relative_file": entry.file, "available": available})
            if not available:
                errors.append({"code": "missing_sfx_asset", "message": f"Missing SFX: {entry.file}", "id": entry.id})
        return {"library_root": str(root), "items": items, "errors": errors}
    except (OSError, ValueError, RuntimeError) as exc:
        raise TimelineError("invalid_sfx_library", str(exc)) from exc


def search_sfx_by_tags(tags: list[str], match_all: bool = True, *, library_root: Path | None = None):
    try:
        query = normalize_tags(tags)
        if not query:
            raise ValueError("Supply at least one tag")
    except ValueError as exc:
        raise TimelineError("invalid_tag_query", str(exc)) from exc
    catalog = list_sfx_library(library_root=library_root)
    wanted = set(query)
    return {"tags": query, "match_all": match_all, "items": [item for item in catalog["items"]
            if item["available"] and (wanted <= set(item["tags"]) if match_all else bool(wanted & set(item["tags"])))]}


def _find(timeline, sfx_id):
    for track in timeline.sfx_tracks:
        for index, item in enumerate(track.clips):
            if item.id == sfx_id:
                return track, index, item
    raise TimelineError("sfx_not_found", f"SFX event not found: {sfx_id}")


def _sfx_edit(project, operation, change):
    result = _edit(project, operation, change)
    result["sfx_id"] = result.pop("clip_id")
    result["sfx"] = result.pop("clip")
    return result


def add_sfx(project, file: str, timeline_time: float, source_in: float = 0, volume_db: float = 0,
            fade_in: float = 0, fade_out: float = 0, enabled: bool = True, tags: list[str] | None = None):
    def change(timeline, folder):
        item = SFXClip(id=f"sfx-{uuid4().hex}", file=file, timeline_time=timeline_time, source_in=source_in,
                       volume_db=volume_db, fade_in=fade_in, fade_out=fade_out, enabled=enabled,
                       tags=[] if tags is None else tags)
        validate_sfx_source(item, folder)
        if len(timeline.sfx_tracks) > 1:
            raise TimelineError("ambiguous_sfx_track", "add_sfx requires zero or one SFX track")
        if not timeline.sfx_tracks:
            timeline.sfx_tracks.append(SFXTrack(id=f"sfx-track-{uuid4().hex}"))
        timeline.sfx_tracks[0].clips.append(item)
        return item, f"Added SFX {item.id} at {item.timeline_time:g}s, {item.volume_db:g} dB."
    return _sfx_edit(project, "add_sfx", change)


def remove_sfx(project, sfx_id: str):
    def change(timeline, folder):
        track, index, item = _find(timeline, sfx_id)
        track.clips.pop(index)
        return item, f"Removed SFX {item.id}; audio preserved."
    return _sfx_edit(project, "remove_sfx", change)


def update_sfx(project, sfx_id: str, **changes):
    allowed = {"file", "timeline_time", "source_in", "volume_db", "fade_in", "fade_out", "enabled", "tags"}
    if set(changes) - allowed:
        raise TimelineError("invalid_sfx_update", "Only editable SFX fields may be updated")
    def change(timeline, folder):
        track, index, item = _find(timeline, sfx_id)
        updated = SFXClip.model_validate({**item.model_dump(), **{key: value for key, value in changes.items() if value is not None}})
        validate_sfx_source(updated, folder)
        track.clips[index] = updated
        return updated, f"Updated SFX {item.id} at {updated.timeline_time:g}s, {updated.volume_db:g} dB."
    return _sfx_edit(project, "update_sfx", change)
