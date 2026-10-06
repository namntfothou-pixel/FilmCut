"""Manual music CRUD using the shared validated, backed-up editing transaction."""

from uuid import uuid4

from engine.audio import probe_audio, validate_music_source
from engine.timeline import TimelineError, _native_path
from schemas.timeline import MusicClip, MusicTrack
from services.timeline_service import _edit


def _find(timeline, music_id):
    for track in timeline.music_tracks:
        for index, item in enumerate(track.clips):
            if item.id == music_id:
                return track, index, item
    raise TimelineError("music_not_found", f"Music item not found: {music_id}")


def _music_edit(project, operation, change):
    result = _edit(project, operation, change)
    result["music_id"] = result.pop("clip_id")
    result["music"] = result.pop("clip")
    return result


def add_music(project, file: str, timeline_start: float = 0, source_in: float = 0,
              source_out: float | None = None, volume_db: float = -18, fade_in: float = 0,
              fade_out: float = 0, loop: bool = False, enabled: bool = True):
    def change(timeline, folder):
        path = _native_path(file)
        info = probe_audio(path if path.is_absolute() else folder / path)
        item = MusicClip(id=f"music-{uuid4().hex}", file=file, timeline_start=timeline_start,
                         source_in=source_in, source_out=info["duration"] if source_out is None else source_out,
                         volume_db=volume_db, fade_in=fade_in, fade_out=fade_out, loop=loop, enabled=enabled)
        validate_music_source(item, folder)
        if len(timeline.music_tracks) > 1:
            raise TimelineError("ambiguous_music_track", "add_music requires zero or one music track")
        if not timeline.music_tracks:
            timeline.music_tracks.append(MusicTrack(id=f"music-track-{uuid4().hex}"))
        timeline.music_tracks[0].clips.append(item)
        return item, f"Added music {item.id} at {item.timeline_start:g}s, {item.volume_db:g} dB."
    return _music_edit(project, "add_music", change)


def remove_music(project, music_id: str):
    def change(timeline, folder):
        track, index, item = _find(timeline, music_id)
        track.clips.pop(index)
        return item, f"Removed music {item.id}; source file preserved."
    return _music_edit(project, "remove_music", change)


def update_music(project, music_id: str, **changes):
    allowed = {"file", "timeline_start", "source_in", "source_out", "volume_db", "fade_in", "fade_out", "loop", "enabled"}
    if set(changes) - allowed:
        raise TimelineError("invalid_music_update", "Only editable music fields may be updated")
    def change(timeline, folder):
        track, index, item = _find(timeline, music_id)
        updated = MusicClip.model_validate({**item.model_dump(), **{key: value for key, value in changes.items() if value is not None}})
        validate_music_source(updated, folder)
        track.clips[index] = updated
        return updated, f"Updated music {item.id} at {updated.timeline_start:g}s, {updated.volume_db:g} dB."
    return _music_edit(project, "update_music", change)
