"""Generate immutable SRT/transcript artifacts and reference them in the timeline."""

import json
import tempfile
from pathlib import Path
from uuid import uuid4

from engine.render import render_dialogue_audio
from engine.subtitle import SubtitleError, format_srt, normalize_language, transcribe_audio
from engine.timeline import _project_context, load_timeline, save_locked_timeline, timeline_lock
from schemas.timeline import SubtitleTrack


def generate_subtitles(project, language: str):
    language = normalize_language(language)
    folder, metadata = _project_context(project)
    timeline_path = folder / "timeline.json"
    artifacts = []
    committed = False
    try:
        # Do not hold the timeline lock during rendering/model inference. Reject
        # stale results instead of overwriting edits made during transcription.
        with timeline_lock(folder):
            before = timeline_path.read_bytes()
            metadata_before = (folder / "project.json").read_bytes()
            timeline = load_timeline(folder)
        cache = folder / "cache"
        cache.mkdir(exist_ok=True)
        if cache.resolve().parent != folder:
            raise SubtitleError("invalid_cache", "Transcription cache must stay inside its project")
        with tempfile.TemporaryDirectory(prefix="transcription-", dir=cache) as temporary:
            audio = render_dialogue_audio(folder, timeline, Path(temporary))
            transcript = transcribe_audio(audio, language)
        text = format_srt(transcript["segments"])
        with timeline_lock(folder):
            if timeline_path.read_bytes() != before or (folder / "project.json").read_bytes() != metadata_before:
                raise SubtitleError("timeline_changed", "Project changed during transcription; regenerate subtitles for the current timeline")
            target = folder / "subtitles"
            target.mkdir(exist_ok=True)
            if target.resolve().parent != folder:
                raise SubtitleError("invalid_subtitle_directory", "Subtitles must stay inside their project")
            revision = uuid4().hex
            srt = target / f"subtitles-{language}-{revision}.srt"
            manifest = srt.with_suffix(".json")
            for path, content in ((srt, text), (manifest, json.dumps(transcript, ensure_ascii=False, indent=2) + "\n")):
                with path.open("x", encoding="utf-8", newline="\n") as handle:
                    artifacts.append(path)
                    handle.write(content)
            # Re-generation replaces only this service's generated language track,
            # preserving old artifacts and references in timeline history.
            previous = next((item for item in timeline.subtitle_tracks if item.language == language
                             and item.id.startswith("generated-subtitles-") and item.file is not None), None)
            track = SubtitleTrack(id=previous.id if previous else f"generated-subtitles-{revision}",
                                  language=language, file=srt.relative_to(folder).as_posix(),
                                  enabled=previous.enabled if previous else True,
                                  burn_in=previous.burn_in if previous else False)
            if previous:
                timeline.subtitle_tracks[timeline.subtitle_tracks.index(previous)] = track
            else:
                timeline.subtitle_tracks.append(track)
            saved, backup = save_locked_timeline(folder, metadata, timeline)
            committed = True
        return {"project": metadata.name, **transcript, "srt_path": str(srt), "transcript_path": str(manifest),
                "subtitle_track": track.model_dump(mode="json"), "timeline_path": str(saved),
                "backup_path": str(backup) if backup else None,
                "summary": f"Generated {len(transcript['segments'])} {language} subtitle segments from rendered dialogue."}
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        raise SubtitleError("subtitle_generation_failed", str(exc)) from exc
    finally:
        if not committed:
            for path in artifacts:
                path.unlink(missing_ok=True)
