"""Non-destructive video-clip editing with validated timeline snapshots."""

from uuid import uuid4

from engine.media import probe_media
from engine.timeline import (
    TimelineError, _native_path, _project_context, load_timeline,
    save_locked_timeline, timeline_lock,
)
from schemas.timeline import VideoClip, VideoTrack


def _find_clip(timeline, clip_id):
    for track in timeline.video_tracks:
        for index, clip in enumerate(track.clips):
            if clip.id == clip_id:
                return track, index, clip
    raise TimelineError("clip_not_found", f"Video clip not found: {clip_id}")


def _validate_trim(clip, folder):
    source = _native_path(clip.source)
    if not source.is_absolute():
        source = folder / source
    info = probe_media(source)
    if info["duration"] <= 0 or clip.source_out > info["duration"] + 1e-6:
        raise TimelineError("trim_out_of_range", "Clip source_out exceeds the media duration or its duration is unknown")


def _edit(project, operation, change):
    try:
        folder, metadata = _project_context(project)
        with timeline_lock(folder):
            candidate = load_timeline(folder).model_copy(deep=True)
            clip, summary = change(candidate, folder)
            path, backup = save_locked_timeline(folder, metadata, candidate)
            return {
                "operation": operation, "project": metadata.name,
                "clip_id": clip.id, "clip": clip.model_dump(mode="json"),
                "summary": summary, "timeline_path": str(path),
                "backup_path": str(backup) if backup else None,
                "revision": backup.stem if backup else None,
            }
    except TimelineError:
        raise
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        raise TimelineError("invalid_edit", str(exc)) from exc


def add_clip(project, source: str, source_in: float, source_out: float, position: float):
    """Add a video clip at a timestamp in seconds without shifting other clips."""
    def change(timeline, folder):
        if len(timeline.video_tracks) > 1:
            raise TimelineError("ambiguous_track", "add_clip requires zero or one video track")
        clip = VideoClip(id=f"clip-{uuid4().hex}", source=source, source_in=source_in,
                         source_out=source_out, timeline_start=position)
        _validate_trim(clip, folder)
        if not timeline.video_tracks:
            timeline.video_tracks.append(VideoTrack(id=f"video-{uuid4().hex}"))
        timeline.video_tracks[0].clips.append(clip)
        return clip, f"Added {clip.id} at {clip.timeline_start:g}s."
    return _edit(project, "add_clip", change)


def remove_clip(project, clip_id: str):
    """Remove only the clip's timeline entry; never delete its source file."""
    def change(timeline, folder):
        track, index, clip = _find_clip(timeline, clip_id)
        if any(clip_id in (transition.from_clip, transition.to_clip) for transition in track.transitions):
            raise TimelineError("clip_has_transitions", "Cannot remove a clip referenced by a transition")
        track.clips.pop(index)
        return clip, f"Removed {clip.id}; other clip positions preserved."
    return _edit(project, "remove_clip", change)


def trim_clip(project, clip_id: str, source_in: float, source_out: float):
    def change(timeline, folder):
        track, index, clip = _find_clip(timeline, clip_id)
        changed = VideoClip.model_validate({**clip.model_dump(), "source_in": source_in, "source_out": source_out})
        _validate_trim(changed, folder)
        track.clips[index] = changed
        return changed, f"Trimmed {clip.id} to source {changed.source_in:g}–{changed.source_out:g}s."
    return _edit(project, "trim_clip", change)


def move_clip(project, clip_id: str, position: float):
    def change(timeline, folder):
        track, index, clip = _find_clip(timeline, clip_id)
        changed = VideoClip.model_validate({**clip.model_dump(), "timeline_start": position})
        track.clips[index] = changed
        return changed, f"Moved {clip.id} from {clip.timeline_start:g}s to {changed.timeline_start:g}s."
    return _edit(project, "move_clip", change)


def set_clip_speed(project, clip_id: str, speed: float):
    def change(timeline, folder):
        track, index, clip = _find_clip(timeline, clip_id)
        changed = VideoClip.model_validate({**clip.model_dump(), "speed": speed})
        track.clips[index] = changed
        return changed, f"Set {clip.id} speed to {changed.speed:g}x (duration {changed.duration:g}s)."
    return _edit(project, "set_clip_speed", change)
