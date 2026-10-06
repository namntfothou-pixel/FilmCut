"""Preview rendering: video trims, normalization and sequential concatenation."""

import math
import tempfile
from pathlib import Path
from uuid import uuid4

from engine.ffmpeg import FFmpegError, concatenate_normalized, encoding_arguments, normalization_filter, run_ffmpeg, transition_normalized
from engine.audio import mix_audio, resolve_music
from engine.subtitle import SubtitleError, burn_subtitles as burn_subtitle_track
from engine.media import MediaError, probe_media
from engine.timeline import TimelineError, _native_path, _project_context, load_timeline
from schemas.timeline import VideoClip, VideoTrack


class RenderError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.details = code, details or {}

    def to_dict(self):
        return {"code": self.code, "message": str(self), "details": self.details}


def _context(project):
    try:
        folder, metadata = _project_context(project)
        normalization_filter(metadata.resolution.width, metadata.resolution.height, metadata.fps)
        cache = folder / "cache"
        cache.mkdir(exist_ok=True)
        # Do not write/delete scratch data via a cache symlink outside this project.
        if cache.resolve().parent != folder:
            raise RenderError("invalid_cache", "Project cache must be inside its project directory.")
        return folder, metadata, cache
    except (TimelineError, FFmpegError) as exc:
        raise RenderError(exc.code, str(exc), exc.to_dict()) from exc
    except OSError as exc:
        raise RenderError("filesystem_error", str(exc)) from exc


def _verify(path, metadata, duration):
    info = probe_media(path)
    tolerance = 1 / metadata.fps + 0.03
    if (info["width"] != metadata.resolution.width or info["height"] != metadata.resolution.height
            or not math.isclose(info["fps"], metadata.fps, rel_tol=1e-5)
            or info["video_codec"] != "h264" or info["audio_codec"] != "aac"
            or info["sample_rate"] != 48000 or abs(info["duration"] - duration) > tolerance):
        raise RenderError("invalid_output", "Rendered output failed codec, format or duration verification.", info)


def _render_clip(folder, metadata, clip, destination):
    clip = VideoClip.model_validate(clip.model_dump() if isinstance(clip, VideoClip) else clip)
    if not clip.enabled:
        raise RenderError("disabled_clip", "Cannot render a disabled clip independently.")
    if clip.speed != 1:
        raise RenderError("unsupported_speed", "This phase supports normal-speed clips only.")
    source = _native_path(clip.source)
    if not source.is_absolute():
        source = folder / source
    source = source.resolve(strict=True)
    info = probe_media(source)
    if info["duration"] <= 0 or clip.source_out > info["duration"] + 1e-6:
        raise RenderError("trim_out_of_range", "Clip trim extends beyond the source duration.",
                          {"clip": clip.id, "source_duration": info["duration"]})
    duration = clip.duration
    arguments = ["-n", "-ss", f"{clip.source_in:.12g}", "-i", str(source)]
    if not info["has_audio"]:
        arguments += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    vf = ("setpts=PTS-STARTPTS," + normalization_filter(metadata.resolution.width, metadata.resolution.height, metadata.fps)
          + f",tpad=stop_mode=clone:stop_duration={1 / metadata.fps:.12g}")
    af = (f"asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,"
          f"volume={clip.volume:.12g},apad,atrim=duration={duration:.12g}")
    arguments += ["-map", "0:V:0", "-map", "0:a:0" if info["has_audio"] else "1:a:0",
                  "-filter_threads", "1", "-vf", vf, "-af", af, "-t", f"{duration:.12g}",
                  *encoding_arguments(), str(destination)]
    run_ffmpeg(arguments)
    _verify(destination, metadata, duration)


def _active_clips(track):
    track = VideoTrack.model_validate(track.model_dump() if isinstance(track, VideoTrack) else track)
    clips = sorted((clip for clip in track.clips if clip.enabled), key=lambda clip: clip.timeline_start)
    if not clips:
        raise RenderError("empty_track", "Video track has no enabled clips.")
    end = 0.0
    boundaries = {(item.from_clip, item.to_clip): item for item in track.transitions}
    previous = None
    for clip in clips:
        transition = boundaries.get((previous.id, clip.id)) if previous else None
        overlap = transition.overlap if transition else 0
        if not math.isclose(clip.timeline_start, end - overlap, rel_tol=0, abs_tol=1e-9):
            raise RenderError("unsupported_placement", "Clips must be contiguous from timeline time zero; gaps and overlaps are not supported.")
        end = clip.timeline_end
        previous = clip
    return clips


def _render_track(folder, metadata, cache, track, destination):
    clips = _active_clips(track)
    with tempfile.TemporaryDirectory(prefix="render-", dir=cache) as workspace:
        files = []
        for index, clip in enumerate(clips):
            output = Path(workspace) / f"clip-{index:04d}.mp4"
            _render_clip(folder, metadata, clip, output)
            files.append(output)
        boundaries = {(item.from_clip, item.to_clip): item for item in track.transitions}
        if any(item.overlap for item in track.transitions):
            joined, duration = files[0], clips[0].duration
            for index in range(1, len(files)):
                transition = boundaries.get((clips[index - 1].id, clips[index].id))
                kind, overlap = (transition.type, transition.overlap) if transition else ("cut", 0)
                output = Path(workspace) / f"joined-{index:04d}.mp4"
                transition_normalized(joined, files[index], duration, clips[index].duration,
                                      kind, overlap, output, fps=metadata.fps)
                duration += clips[index].duration - overlap
                _verify(output, metadata, duration)
                joined = output
            joined.replace(destination)
        else:
            concatenate_normalized(files, [clip.duration for clip in clips], destination, fps=metadata.fps)
        _verify(destination, metadata, clips[-1].timeline_end)


def _error(exc):
    if isinstance(exc, RenderError):
        return exc
    if isinstance(exc, (FFmpegError, MediaError, TimelineError, SubtitleError)):
        return RenderError(exc.code, str(exc), exc.to_dict())
    return RenderError("render_failed", str(exc))


def _cleanup_output(destination, error):
    if destination is not None:
        try:
            destination.unlink(missing_ok=True)
        except OSError as exc:
            error.details["cleanup_error"] = str(exc)
            error.details["remaining_file"] = str(destination)


def render_clip(project, clip: VideoClip | dict) -> Path:
    """Return a normalized cached MP4. Successful cached results are retained."""
    destination = None
    try:
        folder, metadata, cache = _context(project)
        destination = cache / f"clip-{uuid4().hex}.mp4"
        _render_clip(folder, metadata, clip, destination)
        return destination
    except (RenderError, FFmpegError, MediaError, TimelineError, OSError, ValueError, RuntimeError) as exc:
        error = _error(exc)
        _cleanup_output(destination, error)
        if error is exc:
            raise
        raise error from exc


def render_video_track(project, track: VideoTrack | dict) -> Path:
    """Return a concatenated cached MP4; remove only this render's intermediates."""
    destination = None
    try:
        folder, metadata, cache = _context(project)
        destination = cache / f"track-{uuid4().hex}.mp4"
        _render_track(folder, metadata, cache, track, destination)
        return destination
    except (RenderError, FFmpegError, MediaError, TimelineError, OSError, ValueError, RuntimeError) as exc:
        error = _error(exc)
        _cleanup_output(destination, error)
        if error is exc:
            raise
        raise error from exc


def _video_track(timeline, metadata):
    if (timeline.width, timeline.height, timeline.fps) != (metadata.resolution.width, metadata.resolution.height, metadata.fps):
        raise RenderError("settings_mismatch", "Timeline resolution/FPS must match project.json.")
    if any(clip.enabled for track in timeline.audio_tracks for clip in track.clips):
        raise RenderError("unsupported_tracks", "Separate dialogue tracks are not rendered yet.")
    tracks = [track for track in timeline.video_tracks if any(clip.enabled for clip in track.clips)]
    if len(tracks) != 1:
        raise RenderError("unsupported_track_count", "Exactly one nonempty video track is required.")
    return tracks[0]


def render_dialogue_audio(project, timeline, workspace: Path) -> Path:
    """Render a timeline snapshot's dialogue to mono 16 kHz PCM for transcription."""
    try:
        folder, metadata, cache = _context(project)
        track = _video_track(timeline, metadata)
        video, audio = workspace / "dialogue.mp4", workspace / "dialogue.wav"
        _render_track(folder, metadata, cache, track, video)
        run_ffmpeg(["-n", "-i", str(video), "-map", "0:a:0", "-vn", "-ar", "16000", "-ac", "1",
                    "-c:a", "pcm_s16le", str(audio)])
        return audio
    except (RenderError, FFmpegError, MediaError, TimelineError, OSError, ValueError, RuntimeError) as exc:
        if isinstance(exc, RenderError):
            raise
        raise _error(exc) from exc


def render_timeline(project, *, burn_subtitles: bool | None = None) -> Path:
    """Render saved timeline.json into preview/preview.mp4 after verification."""
    try:
        folder, metadata, cache = _context(project)
        timeline = load_timeline(folder)
        track = _video_track(timeline, metadata)
        subtitles = [track for track in timeline.subtitle_tracks if track.enabled
                     and (burn_subtitles is True or (burn_subtitles is None and track.burn_in))]
        if len(subtitles) > 1:
            raise RenderError("ambiguous_subtitle_track", "Enable burn-in for one subtitle track at a time.")
        preview = folder / "preview"
        preview.mkdir(exist_ok=True)
        if preview.resolve().parent != folder:
            raise RenderError("invalid_preview", "Preview directory must be inside its project directory.")
        destination = preview / "preview.mp4"
        for clip in _active_clips(track):
            source = _native_path(clip.source)
            source = source if source.is_absolute() else folder / source
            if source.resolve() == destination.resolve():
                raise RenderError("source_output_conflict", "Preview output cannot overwrite source media.")
        music = [item for track in timeline.music_tracks for item in track.clips]
        sfx = [item for track in timeline.sfx_tracks for item in track.clips]
        for item in [*music, *sfx]:
            if item.enabled and resolve_music(item, folder).resolve() == destination.resolve():
                raise RenderError("source_output_conflict", "Preview output cannot overwrite audio source media.")
        with tempfile.TemporaryDirectory(prefix="preview-", dir=cache) as workspace:
            workspace = Path(workspace)
            candidate = workspace / "preview.mp4"
            _render_track(folder, metadata, cache, track, candidate)
            duration = _active_clips(track)[-1].timeline_end
            mixed = workspace / "mixed.mp4"
            if mix_audio(candidate, music, sfx, folder, duration, workspace, mixed):
                _verify(mixed, metadata, duration)
                candidate = mixed
            if subtitles:
                captioned = workspace / "captioned.mp4"
                if burn_subtitle_track(candidate, subtitles[0], folder, workspace, captioned):
                    _verify(captioned, metadata, duration)
                    candidate = captioned
            candidate.replace(destination)
        return destination
    except (RenderError, FFmpegError, MediaError, TimelineError, SubtitleError, OSError, ValueError, RuntimeError) as exc:
        if isinstance(exc, RenderError):
            raise
        raise _error(exc) from exc
