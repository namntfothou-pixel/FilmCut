"""Non-destructive video-clip editing with validated timeline snapshots."""

from uuid import uuid4
import math

from engine.media import probe_media
from engine.audio import validate_clip_audio_source
from engine.timeline import (
    TimelineError, _native_path, _project_context, load_timeline,
    save_locked_timeline, timeline_lock,
)
from schemas.timeline import Transition, VideoClip, VideoTrack


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
        if any(transition.type != "cut" and clip_id in (transition.from_clip, transition.to_clip) for transition in track.transitions):
            raise TimelineError("clip_has_transitions", "Cannot remove a clip referenced by a transition")
        track.clips.pop(index)
        track.transitions = [item for item in track.transitions if clip_id not in (item.from_clip, item.to_clip)]
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


def set_transition(project, clip_id: str, transition_type: str, duration: float):
    """Set the outgoing boundary and ripple later video positions by overlap delta."""
    details = {}
    def change(timeline, folder):
        track, _, left = _find_clip(timeline, clip_id)
        active = sorted((clip for clip in track.clips if clip.enabled), key=lambda clip: clip.timeline_start)
        if not left.enabled or active.index(left) == len(active) - 1:
            raise TimelineError("no_transition_boundary", "Choose an enabled clip with a following enabled clip")
        index = active.index(left)
        right = active[index + 1]
        old = next((item for item in track.transitions if item.from_clip == left.id), None)
        transition = Transition(id=old.id if old else f"transition-{uuid4().hex}", from_clip=left.id,
                                to_clip=right.id, type=transition_type, duration=duration)
        old_overlap = old.overlap if old else 0
        if not abs(right.timeline_start - (left.timeline_end - old_overlap)) <= 1e-9:
            raise TimelineError("noncontiguous_boundary", "A transition cannot bridge an existing timeline gap")
        if transition.duration > min(left.duration, right.duration):
            raise TimelineError("transition_too_long", "Transition exceeds a participating clip duration")
        if 0 < transition.duration < 1 / timeline.fps - 1e-9:
            raise TimelineError("transition_too_short", "Video transitions must last at least one output frame")
        previous_duration = timeline.duration
        delta = old_overlap - transition.overlap
        shifted = []
        for clip in active[index + 1:]:
            old_start = clip.timeline_start
            clip.timeline_start += delta
            if delta:
                shifted.append({"clip_id": clip.id, "before": old_start, "after": clip.timeline_start})
        track.transitions = [item for item in track.transitions if item.from_clip != left.id] + [transition]
        details.update(transition=transition.model_dump(mode="json"), shifted_clips=shifted,
                       previous_duration=previous_duration, duration=timeline.duration)
        return left, f"Set {transition.type} after {left.id} ({transition.duration:g}s); video duration {timeline.duration:g}s."
    return {**_edit(project, "set_transition", change), **details}


def _set_audio_offset(project, clip_id, duration, *, lead):
    def change(timeline, folder):
        if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
            raise TimelineError("invalid_audio_offset", "Audio offset duration must be finite and positive")
        track, index, clip = _find_clip(timeline, clip_id)
        active = sorted((item for item in track.clips if item.enabled), key=lambda item: item.timeline_start)
        if not clip.enabled or (lead and active.index(clip) == 0) or (not lead and active.index(clip) == len(active) - 1):
            raise TimelineError("no_audio_boundary", "J-cut needs preceding picture; L-cut needs following picture")
        data = clip.model_dump()
        if lead:
            source_in = clip.source_in - duration * clip.speed
            start = clip.timeline_start - duration
            if source_in < 0 or start < 0:
                raise TimelineError("insufficient_audio_handle", "J-cut exceeds source pre-roll or available preceding timeline time")
            data.update(audio_source_in=source_in, audio_source_out=clip.resolved_audio_source_out,
                        audio_timeline_start=start)
        else:
            if clip.timeline_end + duration > track.duration + 1e-9:
                raise TimelineError("insufficient_timeline_handle", "L-cut exceeds the remaining picture duration")
            data.update(audio_source_in=clip.resolved_audio_source_in,
                        audio_source_out=clip.resolved_audio_source_in + (
                            clip.timeline_end + duration - clip.resolved_audio_timeline_start) * clip.speed,
                        audio_timeline_start=clip.resolved_audio_timeline_start)
        changed = VideoClip.model_validate(data)
        validate_clip_audio_source(changed, folder)
        track.clips[index] = changed
        label = "J-cut" if lead else "L-cut"
        return changed, (f"Set {label} on {clip.id} ({duration:g}s); source audio "
                         f"{changed.resolved_audio_source_in:g}–{changed.resolved_audio_source_out:g}s "
                         f"at timeline {changed.resolved_audio_timeline_start:g}s; video timing preserved.")
    return _edit(project, "set_j_cut" if lead else "set_l_cut", change)


def set_j_cut(project, clip_id: str, duration: float):
    return _set_audio_offset(project, clip_id, duration, lead=True)


def set_l_cut(project, clip_id: str, duration: float):
    return _set_audio_offset(project, clip_id, duration, lead=False)


def reset_audio_offset(project, clip_id: str):
    def change(timeline, folder):
        track, index, clip = _find_clip(timeline, clip_id)
        changed = VideoClip.model_validate({**clip.model_dump(), "audio_source_in": None,
                                           "audio_source_out": None, "audio_timeline_start": None})
        track.clips[index] = changed
        return changed, f"Reset {clip.id} source audio to follow its video trim and placement."
    return _edit(project, "reset_audio_offset", change)
