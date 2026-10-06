"""Manual BGM/SFX preparation and dialogue-preserving peak-limited mixing."""

import json
import math
import subprocess
from pathlib import Path
import os

from engine.ffmpeg import FFmpegError, run_ffmpeg
from engine.media import _existing_path
from engine.timeline import _native_path


class AudioError(FFmpegError):
    pass


def validate_clip_audio_source(clip, folder: Path) -> dict:
    from engine.media import probe_media
    source = _native_path(clip.source)
    source = source if source.is_absolute() else folder / source
    video = probe_media(source)
    if not video["has_audio"]:
        raise AudioError("missing_source_audio", "This video has no source audio to offset")
    metadata = probe_audio(source)
    if clip.resolved_audio_source_out > metadata["duration"] + 1e-6:
        raise AudioError("insufficient_audio_handle", "audio_source_out exceeds the source audio duration")
    return metadata


def probe_audio(file: str | Path) -> dict:
    path = _existing_path(file)
    try:
        result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                                 "format=duration:stream=duration,codec_name,sample_rate", "-of", "json", str(path)],
                                shell=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                check=False, timeout=30)
    except FileNotFoundError as exc:
        raise AudioError("ffprobe_missing", "ffprobe is not available on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioError("audio_probe_timeout", "Audio probe exceeded 30 seconds") from exc
    except OSError as exc:
        raise AudioError("audio_probe_failed", str(exc)) from exc
    if result.returncode:
        raise AudioError("invalid_audio", "ffprobe rejected the audio file", stderr=result.stderr, returncode=result.returncode)
    try:
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        value = stream.get("duration")
        if value in (None, "N/A"):
            value = data.get("format", {}).get("duration")
        duration = float(value)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("Audio duration must be finite and positive")
        return {"path": str(path), "duration": duration, "audio_codec": stream.get("codec_name", "")}
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError) as exc:
        raise AudioError("invalid_audio", f"Missing or invalid audio stream: {exc}") from exc


def resolve_music(item, folder: Path) -> Path:
    path = _native_path(item.file)
    return path if path.is_absolute() else folder / path


def validate_music_source(item, folder: Path) -> dict:
    metadata = probe_audio(resolve_music(item, folder))
    if item.source_out > metadata["duration"] + 1e-6:
        raise AudioError("music_trim_out_of_range", "Music source_out exceeds its source duration")
    return metadata


def validate_sfx_source(item, folder: Path) -> dict:
    metadata = probe_audio(resolve_music(item, folder))
    end = metadata["duration"] if item.source_out is None else item.source_out
    if item.source_in >= end or end > metadata["duration"] + 1e-6:
        raise AudioError("sfx_trim_out_of_range", "SFX source trim must lie inside its audio duration")
    duration = end - item.source_in
    if item.fade_in + item.fade_out > duration:
        raise AudioError("invalid_sfx_fades", "SFX fades exceed its remaining source duration")
    return {**metadata, "playback_duration": duration}


def _gain_fade_filters(duration, volume_db, fade_in, fade_out):
    total = fade_in + fade_out
    factor = min(1, duration / total) if total else 1
    fade_in, fade_out = fade_in * factor, fade_out * factor
    filters = ["asetpts=PTS-STARTPTS", f"atrim=duration={duration:.12g}", f"volume={volume_db:.12g}dB"]
    if fade_in:
        filters.append(f"afade=t=in:st=0:d={fade_in:.12g}")
    if fade_out:
        filters.append(f"afade=t=out:st={duration - fade_out:.12g}:d={fade_out:.12g}")
    return filters


def prepare_music(item, folder: Path, video_duration: float, workspace: Path, index: int) -> Path | None:
    """Trim once, repeat only that interval, then apply gain and endpoint fades."""
    if not item.enabled or item.timeline_start >= video_duration:
        return None
    info = validate_music_source(item, folder)
    duration = video_duration - item.timeline_start if item.loop else min(item.duration, video_duration - item.timeline_start)
    if item.end is not None:
        duration = min(duration, item.end - item.timeline_start)
    segment = workspace / f"music-{index}-segment.wav"
    prepared = workspace / f"music-{index}-prepared.wav"
    run_ffmpeg(["-n", "-ss", f"{item.source_in:.12g}", "-i", info["path"], "-map", "0:a:0",
                "-vn", "-t", f"{item.duration:.12g}", "-af", "asetpts=PTS-STARTPTS,aresample=48000",
                "-ac", "2", "-c:a", "pcm_f32le", str(segment)])
    arguments = ["-n"]
    if item.loop:
        arguments += ["-stream_loop", "-1"]
    arguments += ["-i", str(segment)]
    # If the timeline truncates the music, shorten both fades proportionally.
    filters = _gain_fade_filters(duration, item.volume_db, item.fade_in, item.fade_out)
    run_ffmpeg([*arguments, "-map", "0:a:0", "-af", ",".join(filters), "-t", f"{duration:.12g}",
                "-ar", "48000", "-ac", "2", "-c:a", "pcm_f32le", str(prepared)])
    return prepared


def prepare_sfx(item, folder: Path, video_duration: float, workspace: Path, index: int) -> Path | None:
    if not item.enabled or item.timeline_time >= video_duration:
        return None
    info = validate_sfx_source(item, folder)
    duration = min(info["playback_duration"], video_duration - item.timeline_time)
    output = workspace / f"sfx-{index}-prepared.wav"
    filters = ["aresample=48000", *_gain_fade_filters(duration, item.volume_db, item.fade_in, item.fade_out)]
    run_ffmpeg(["-n", "-ss", f"{item.source_in:.12g}", "-i", info["path"], "-map", "0:a:0", "-vn",
                "-af", ",".join(filters), "-t", f"{duration:.12g}", "-ar", "48000", "-ac", "2",
                "-c:a", "pcm_f32le", str(output)])
    return output


def mix_audio(video: Path, music_items: list, sfx_items: list, folder: Path, duration: float, workspace: Path, output: Path) -> bool:
    """Mix source dialogue at unity gain; limit peaks without automatic gain boost.

    Returns False if no additional audio plays. Retain the original video then.
    Float PCM intermediates preserve gain until the final limiter, and H.264
    video is stream-copied. Fades apply only to added audio, never to dialogue.
    """
    prepared = []
    for index, item in enumerate(music_items):
        path = prepare_music(item, folder, duration, workspace, index)
        if path is not None:
            prepared.append((item.timeline_start, path))
    for index, item in enumerate(sfx_items):
        path = prepare_sfx(item, folder, duration, workspace, index)
        if path is not None:
            prepared.append((item.timeline_time, path))
    if not prepared:
        return False
    arguments = ["-n", "-i", str(video)]
    filters = ["[0:a:0]asetpts=PTS-STARTPTS[dialogue]"]
    inputs = ["[dialogue]"]
    for index, (timestamp, path) in enumerate(prepared, 1):
        arguments += ["-i", str(path)]
        delay_samples = round(timestamp * 48000)
        filters.append(f"[{index}:a:0]adelay={delay_samples}S:all=1[m{index}]")
        inputs.append(f"[m{index}]")
    filters.append("".join(inputs) + f"amix=inputs={len(inputs)}:duration=first:dropout_transition=0:normalize=0,"
                   f"alimiter=limit=0.8:level=false:latency=true,atrim=duration={duration:.12g},asetpts=PTS-STARTPTS[mix]")
    run_ffmpeg([*arguments, "-filter_complex_threads", "1", "-filter_complex", ";".join(filters),
                "-map", "0:v:0", "-map", "[mix]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-ar", "48000", "-ac", "2", "-t", f"{duration:.12g}", "-movflags", "+faststart", str(output)])
    return True


def mix_music(video: Path, items: list, folder: Path, duration: float, workspace: Path, output: Path) -> bool:
    """Compatibility helper for callers mixing only music."""
    return mix_audio(video, items, [], folder, duration, workspace, output)


def prepare_dialogue(clip, folder: Path, duration: float, workspace: Path, index: int,
                     *, origin: float = 0, fade_in: float = 0, fade_out: float = 0) -> tuple | None:
    """Extract one source interval exactly once, with sample-based placement."""
    from engine.media import probe_media
    source = _native_path(clip.source)
    source = source if source.is_absolute() else folder / source
    if not probe_media(source)["has_audio"]:
        if clip.has_audio_offset:
            raise AudioError("missing_source_audio", "Offset clip has no source dialogue")
        return None
    metadata = validate_clip_audio_source(clip, folder) if clip.has_audio_offset else probe_audio(source)
    start = clip.resolved_audio_timeline_start - origin
    source_in = clip.resolved_audio_source_in
    length = clip.audio_duration
    if start < 0:
        source_in -= start * clip.speed
        length += start
        start = 0
    length = min(length, duration - start)
    if length <= 0 or clip.volume == 0 or source_in >= metadata["duration"]:
        return None
    if clip.speed != 1:
        raise AudioError("unsupported_speed", "Source-audio rendering currently requires speed 1")
    output = workspace / f"dialogue-{index}-prepared.wav"
    # Trim resampled PCM by sample count instead of input seeking. Container
    # packet timestamps (e.g. Matroska's millisecond timebase) must not shift
    # source samples and create phase errors between aligned source intervals.
    first_sample = round(source_in * 48000)
    sample_count = round(length * 48000)
    filters = ["aresample=48000", "aformat=channel_layouts=stereo",
               f"atrim=start_sample={first_sample}:end_sample={first_sample + sample_count}",
               "asetpts=N/SR/TB", f"volume={clip.volume:.12g}", "apad", f"atrim=duration={length:.12g}"]
    # Default audio retains synchronized transition envelopes. Explicit source
    # audio is independent of visual effects and has no implicit boundary fades.
    if not clip.has_audio_offset:
        if fade_in:
            filters.append(f"afade=t=in:st=0:d={fade_in:.12g}")
        if fade_out:
            filters.append(f"afade=t=out:st={clip.audio_duration - fade_out:.12g}:d={fade_out:.12g}")
    run_ffmpeg(["-n", "-i", str(source), "-map", "0:a:0", "-vn",
                "-af", ",".join(filters), "-t", f"{length:.12g}", "-ar", "48000", "-ac", "2",
                "-c:a", "pcm_f32le", str(output)])
    return start, output


def replace_dialogue(video: Path, clips: list, transitions: list, folder: Path, duration: float,
                     workspace: Path, output: Path, *, origin: float = 0) -> bool:
    """Replace embedded audio with independently scheduled source events.

    The video input's audio is never included in this graph. Every enabled clip
    contributes at most one event, so offset dialogue cannot play twice.
    """
    incoming = {item.to_clip: item.overlap for item in transitions}
    outgoing = {item.from_clip: item.overlap for item in transitions}
    events = []
    covered = {}
    # Explicit edits own their handles. A stable clip order resolves shared
    # source samples between two explicit edits without summing them twice.
    order = sorted(enumerate(clips), key=lambda item: (not item[1].has_audio_offset, item[0]))
    for index, clip in order:
        if not clip.enabled:
            continue
        event = prepare_dialogue(clip, folder, duration, workspace, index, origin=origin,
                                 fade_in=incoming.get(clip.id, 0), fade_out=outgoing.get(clip.id, 0))
        if event is not None:
            start, path = event
            source = _native_path(clip.source)
            source = source if source.is_absolute() else folder / source
            mapping = round((clip.resolved_audio_source_in - (clip.resolved_audio_timeline_start - origin)) * 48000)
            key = (os.path.normcase(str(source.resolve())), mapping)
            first = round(start * 48000)
            last = round(min(duration, clip.audio_timeline_end - origin) * 48000)
            masks = [(max(first, previous_first), min(last, previous_last))
                     for previous_first, previous_last, explicit in covered.get(key, [])
                     if (explicit or clip.has_audio_offset) and previous_last > first and previous_first < last]
            covered.setdefault(key, []).append((first, last, clip.has_audio_offset))
            if masks:
                # Default/default visual crossfade envelopes already sum to one.
                # Deduplicate only overlaps involving independently edited audio.
                spans = []
                for begin, end in sorted(masks):
                    if spans and begin <= spans[-1][1]:
                        spans[-1] = (spans[-1][0], max(end, spans[-1][1]))
                    else:
                        spans.append((begin, end))
                if len(spans) == 1 and spans[0] == (first, last):
                    continue
                condition = "+".join(f"gte(n,{begin - first})*lt(n,{end - first})" for begin, end in spans)
                masked = workspace / f"dialogue-{index}-deduplicated.wav"
                run_ffmpeg(["-n", "-i", str(path), "-af", f"aeval=exprs='if({condition},0,val(ch))':c=same",
                            "-c:a", "pcm_f32le", str(masked)])
                path = masked
            events.append((start, path))
    if not events:
        return False  # The caller prepared a silent video candidate.
    arguments = ["-n", "-i", str(video), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    filters = [f"[1:a:0]atrim=duration={duration:.12g},asetpts=PTS-STARTPTS[clock]"]
    labels = ["[clock]"]
    for index, (timestamp, path) in enumerate(events, 2):
        arguments += ["-i", str(path)]
        filters.append(f"[{index}:a:0]adelay={round(timestamp * 48000)}S:all=1[d{index}]")
        labels.append(f"[d{index}]")
    filters.append("".join(labels) + f"amix=inputs={len(labels)}:duration=first:dropout_transition=0:normalize=0,"
                   f"alimiter=limit=0.8:level=false:latency=true,atrim=duration={duration:.12g},asetpts=PTS-STARTPTS[dialogue]")
    run_ffmpeg([*arguments, "-filter_complex_threads", "1", "-filter_complex", ";".join(filters),
                "-map", "0:v:0", "-map", "[dialogue]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                "-ar", "48000", "-ac", "2", "-t", f"{duration:.12g}", "-movflags", "+faststart", str(output)])
    return True
