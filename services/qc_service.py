"""Final timeline QC and guarded MP4 export using FilmCut's existing renderer."""

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from engine.audio import AudioError, probe_audio
from engine.media import MediaError, probe_media
from engine.render import RenderError, render_timeline
from engine.subtitle import SubtitleError, read_srt
from engine.timeline import TimelineError, _native_path, _project_context, timeline_lock, validate_timeline
from schemas.timeline import Timeline

BLACK_EVENT = re.compile(r"black_start:([0-9.]+)\s+black_end:([0-9.]+)\s+black_duration:([0-9.]+)")


class QCError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code, self.details = code, details or {}

    def to_dict(self):
        return {"code": self.code, "message": str(self), "details": self.details}


class ExportError(QCError):
    pass


def _check(name, status, summary, details=None):
    return {"name": name, "status": status, "summary": summary, "details": details or {}}


def _save_report(path: Path, report: dict):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                prefix=".qc-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        temporary.replace(path)
    except (OSError, ValueError) as exc:
        raise QCError("qc_report_write_failed", f"Cannot save QC report {path}: {exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _diagnostic(video: Path, filter_spec: str, *, timeout=300) -> str:
    command = ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", "-loglevel", "info",
               "-i", str(video), "-map", "0:a:0" if filter_spec.startswith("astats") else "0:v:0",
               "-af" if filter_spec.startswith("astats") else "-vf", filter_spec,
               "-f", "null", "-"]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=timeout, check=False, shell=False)
    except FileNotFoundError as exc:
        raise QCError("ffmpeg_missing", "FFmpeg is required for media QC") from exc
    except subprocess.TimeoutExpired as exc:
        raise QCError("qc_analysis_timeout", f"FFmpeg QC analysis exceeded {timeout} seconds") from exc
    except OSError as exc:
        raise QCError("qc_analysis_failed", str(exc)) from exc
    if completed.returncode:
        raise QCError("qc_analysis_failed", f"FFmpeg QC filter failed: {completed.stderr[-2000:]}",
                      {"returncode": completed.returncode})
    return completed.stderr


def _black_intervals(preview: Path):
    output = _diagnostic(preview, "blackdetect=d=0.04:pix_th=0.10:pic_th=0.95")
    return [{"start": float(a), "end": float(b), "duration": float(c)}
            for a, b, c in BLACK_EVENT.findall(output)]


def _audio_peak_db(preview: Path):
    output = _diagnostic(preview, "astats=metadata=0:reset=0")
    values = re.findall(r"Peak level dB:\s*(-?(?:inf|[0-9]+(?:\.[0-9]+)?))", output, flags=re.IGNORECASE)
    numeric = [float(value) for value in values if "inf" not in value.lower()]
    if not numeric:
        if re.search(r"Peak level dB:\s*-?inf", output, flags=re.IGNORECASE):
            return None
        raise QCError("audio_analysis_unavailable", "FFmpeg did not report an audio peak level")
    # astats prints both per-channel and Overall results. The maximum is the
    # conservative level to compare against digital full scale.
    return max(numeric)


def _resolve(value: str, folder: Path):
    path = _native_path(value)
    return (path if path.is_absolute() else folder / path).resolve(strict=True)


def _intentional_black(timeline: Timeline, interval: dict, tolerance: float):
    for track in timeline.video_tracks:
        clips = {clip.id: clip for clip in track.clips}
        for transition in track.transitions:
            if transition.type != "fade_to_black":
                continue
            left, right = clips.get(transition.from_clip), clips.get(transition.to_clip)
            if left is None or right is None:
                continue
            start = left.timeline_end - transition.duration
            end = right.timeline_start + transition.duration
            if interval["start"] >= start - tolerance and interval["end"] <= end + tolerance:
                return True
    return False


def qc_project(project):
    """Render and inspect the current timeline; persist qc_report.json every run."""
    folder, metadata = _project_context(project)
    report_path = folder / "qc_report.json"
    timeline_path = folder / "timeline.json"
    checks = []
    timeline = None
    timeline_bytes = None
    duration = 0.0
    try:
        timeline_bytes = timeline_path.read_bytes()
        raw = json.loads(timeline_bytes)
        timeline = Timeline.model_validate(raw)
        validation = validate_timeline(timeline, base_dir=folder)
        if timeline.project != metadata.name:
            validation = validation.model_copy(update={"valid": False})
            errors = [*validation.errors]
            from schemas.timeline import TimelineIssue
            errors.append(TimelineIssue(code="project_mismatch", location=["project"],
                                        message="Timeline project does not match project.json"))
            validation = validation.model_copy(update={"errors": errors})
        duration = timeline.duration
        media_errors = [e.model_dump(mode="json") for e in validation.errors
                        if e.code in {"missing_source", "unsupported_path"}]
        checks.append(_check("missing_media", "fail" if media_errors else "pass",
            f"{len(media_errors)} missing or unsupported media references.", {"issues": media_errors}))
        checks.append(_check("timeline_validity", "pass" if validation.valid else "fail",
            "Timeline validation passed." if validation.valid else "Timeline contains invalid intent or references.",
            {"issues": [e.model_dump(mode="json") for e in validation.errors]}))
    except (OSError, ValueError, TypeError, ValidationError, TimelineError) as exc:
        details = exc.to_dict() if callable(getattr(exc, "to_dict", None)) else {"message": str(exc)}
        checks.extend([
            _check("missing_media", "fail", "Cannot verify media because timeline.json could not be loaded.", details),
            _check("timeline_validity", "fail", "Timeline JSON or schema is invalid.", details),
        ])

    report = {"version": 1, "project": metadata.name, "checked_at": datetime.now(timezone.utc).isoformat(),
              "passed": False, "checks": checks, "errors": [], "warnings": [],
              "timeline_path": str(timeline_path), "timeline_sha256": hashlib.sha256(timeline_bytes).hexdigest()
              if timeline_bytes is not None else None,
              "timeline_duration": duration, "preview_path": None, "preview_sha256": None}

    if timeline is not None:
        source_errors, silent_video, audio_inputs_present = [], [], False
        checked_video, checked_audio = set(), set()
        for track in timeline.video_tracks:
            for clip in track.clips:
                try:
                    path = _resolve(clip.source, folder)
                    if str(path).casefold() not in checked_video:
                        checked_video.add(str(path).casefold())
                        info = probe_media(path)
                        if clip.enabled and info["has_audio"]:
                            audio_inputs_present = True
                        elif clip.enabled:
                            silent_video.append(str(path))
                except (OSError, ValueError, RuntimeError, MediaError) as exc:
                    source_errors.append({"file": clip.source, "clip_id": clip.id,
                                          "code": getattr(exc, "code", "broken_source"), "message": str(exc)})
        for collection in ("audio_tracks", "music_tracks", "sfx_tracks"):
            for track in getattr(timeline, collection):
                for clip in track.clips:
                    value = clip.source if collection == "audio_tracks" else clip.file
                    try:
                        path = _resolve(value, folder)
                        if str(path).casefold() not in checked_audio:
                            checked_audio.add(str(path).casefold())
                            probe_audio(path)
                        if clip.enabled and (collection == "audio_tracks" or clip.volume_db > -120):
                            audio_inputs_present = True
                    except (OSError, ValueError, RuntimeError, AudioError, MediaError) as exc:
                        source_errors.append({"file": value, "clip_id": clip.id,
                                              "code": getattr(exc, "code", "broken_source"), "message": str(exc)})
        checks.append(_check("broken_source_references", "fail" if source_errors else "pass",
            f"{len(source_errors)} broken source references.", {"issues": source_errors}))
        if silent_video:
            report["warnings"].append({"check": "missing_audio", "message":
                "Some video sources have no audio stream; this is allowed when other dialogue or sound is present.",
                "sources": silent_video})
        report["audio_inputs_present"] = audio_inputs_present

        settings_ok = (timeline.width == metadata.resolution.width and timeline.height == metadata.resolution.height
                       and math.isclose(timeline.fps, metadata.fps, rel_tol=1e-9, abs_tol=1e-9))
        checks.extend([
            _check("resolution", "pass" if (timeline.width, timeline.height) ==
                   (metadata.resolution.width, metadata.resolution.height) else "fail",
                   f"Timeline {timeline.width}x{timeline.height}; project {metadata.resolution.width}x{metadata.resolution.height}."),
            _check("fps", "pass" if math.isclose(timeline.fps, metadata.fps, rel_tol=1e-9, abs_tol=1e-9) else "fail",
                   f"Timeline {timeline.fps:g} fps; project {metadata.fps:g} fps."),
        ])

        cue_issues, beyond_issues = [], []
        for track in timeline.subtitle_tracks:
            cues = track.cues
            if track.file is not None:
                try:
                    cues = read_srt(_resolve(track.file, folder))
                except (OSError, ValueError, RuntimeError, SubtitleError) as exc:
                    cue_issues.append({"track_id": track.id, "code": getattr(exc, "code", "invalid_subtitle"),
                                       "message": str(exc)})
                    continue
            for index, cue in enumerate(cues):
                start = cue.get("start") if isinstance(cue, dict) else cue.timeline_start
                end = cue.get("end") if isinstance(cue, dict) else cue.timeline_end
                if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
                    cue_issues.append({"track_id": track.id, "cue": index + 1, "code": "invalid_timing",
                                       "start": start, "end": end})
                elif end > duration + 1e-6:
                    beyond_issues.append({"track_id": track.id, "cue": index + 1,
                                          "code": "beyond_timeline", "start": start, "end": end,
                                          "timeline_duration": duration})
        checks.append(_check("subtitle_timing", "fail" if cue_issues else "pass",
            f"{len(cue_issues)} subtitle parse or interval errors.", {"issues": cue_issues}))
        checks.append(_check("subtitle_beyond_timeline", "fail" if beyond_issues else "pass",
            f"{len(beyond_issues)} subtitle cues end after the video timeline.", {"issues": beyond_issues}))

        if validation.valid and settings_ok and duration > 0:
            try:
                preview = render_timeline(folder)
                output_info = probe_media(preview)
                report["preview_path"] = str(preview)
                report["preview_sha256"] = hashlib.sha256(preview.read_bytes()).hexdigest()
                report["output_metadata"] = output_info
                checks.append(_check("rendered_output", "pass", "Fresh preview rendered and ffprobe-readable.", output_info))
                output_matches = (output_info["width"] == metadata.resolution.width
                    and output_info["height"] == metadata.resolution.height
                    and math.isclose(output_info["fps"], metadata.fps, rel_tol=1e-5)
                    and output_info["video_codec"] == "h264" and output_info["audio_codec"] == "aac"
                    and output_info["sample_rate"] == metadata.audio_sample_rate)
                checks.append(_check("output_format", "pass" if output_matches else "fail",
                    "Output matches project resolution/FPS with H.264/AAC." if output_matches else
                    "Output codec, sample rate, resolution, or FPS differs from project settings.", output_info))
                duration_ok = abs(output_info["duration"] - duration) <= 1 / metadata.fps + 0.03
                checks.append(_check("output_duration", "pass" if duration_ok else "fail",
                    f"Rendered {output_info['duration']:.3f}s; timeline {duration:.3f}s.", output_info))
                if not output_info["has_audio"]:
                    checks.append(_check("missing_audio", "fail", "Rendered output has no audio stream."))
                    checks.append(_check("audio_clipping", "skipped", "Cannot inspect peak level without an audio stream."))
                else:
                    try:
                        peak_db = _audio_peak_db(preview)
                        has_signal = peak_db is not None
                        missing_audio = not audio_inputs_present or not has_signal
                        checks.append(_check("missing_audio", "fail" if missing_audio else "pass",
                            "No audible dialogue or sound source is present in the rendered program." if missing_audio else
                            "Rendered program contains audio from a source or sound track.",
                            {"audio_inputs_present": audio_inputs_present, "peak_dbfs": peak_db}))
                        clipping = has_signal and peak_db >= -0.1
                        checks.append(_check("audio_clipping", "fail" if clipping else "pass",
                            "Rendered audio reaches digital full scale; inspect for clipping." if clipping else
                            ("Rendered audio has headroom." if has_signal else "Silent audio has no clipping."),
                            {"peak_dbfs": peak_db, "threshold_dbfs": -0.1}))
                    except QCError as exc:
                        checks.append(_check("missing_audio", "pass", "Rendered output contains an audio stream; signal analysis failed."))
                        checks.append(_check("audio_clipping", "fail", str(exc), exc.to_dict()))
                try:
                    black = _black_intervals(preview)
                    tolerance = 1 / metadata.fps + 0.03
                    unexpected = [event for event in black if not _intentional_black(timeline, event, tolerance)]
                    checks.append(_check("unexpected_black_frames", "fail" if unexpected else "pass",
                        f"Found {len(unexpected)} unexpected black intervals.",
                        {"intervals": unexpected, "intentional_fades": len(black) - len(unexpected)}))
                except QCError as exc:
                    checks.append(_check("unexpected_black_frames", "fail", str(exc), exc.to_dict()))
            except (RenderError, MediaError, OSError, ValueError, RuntimeError, QCError) as exc:
                details = exc.to_dict() if callable(getattr(exc, "to_dict", None)) else {"message": str(exc)}
                checks.append(_check("rendered_output", "fail", str(exc), details))
        else:
            checks.append(_check("rendered_output", "skipped",
                "Cannot render until timeline references, project settings, and duration are valid."))

    try:
        timeline_stable = timeline_bytes is not None and timeline_path.read_bytes() == timeline_bytes
    except OSError:
        timeline_stable = False
    checks.append(_check("timeline_stability", "pass" if timeline_stable else "fail",
        "timeline.json stayed unchanged during QC." if timeline_stable else
        "timeline.json changed or became unavailable while QC ran."))

    failed = [item for item in checks if item["status"] == "fail"]
    required_checks = ("missing_media", "broken_source_references", "timeline_validity",
                       "unexpected_black_frames", "audio_clipping", "missing_audio",
                       "subtitle_timing", "subtitle_beyond_timeline", "resolution", "fps",
                       "output_duration", "output_format", "rendered_output", "timeline_stability")
    present = {item["name"] for item in checks}
    checks.extend(_check(name, "skipped", "Not assessed because an earlier project, timeline, or render check failed.")
                  for name in required_checks if name not in present)
    report["errors"] = [{"check": item["name"], "summary": item["summary"], **item["details"]}
                         for item in failed]
    report["passed"] = not failed
    report["report_path"] = str(report_path)
    report["checks"] = checks
    _save_report(report_path, report)
    return report


def export_final(project, *, force: bool = False):
    """Export the QC-checked render; `force` bypasses findings, never encoding checks."""
    if not isinstance(force, bool):
        raise ExportError("invalid_force", "force must be a boolean")
    folder, metadata = _project_context(project)
    report = qc_project(folder)
    if not report["passed"] and not force:
        raise ExportError("qc_failed", "Final export blocked because QC did not pass.",
                          {"qc_report_path": report["report_path"], "errors": report["errors"]})
    candidate = Path(report["preview_path"]) if report["preview_path"] else None
    if candidate is None or not candidate.is_file():
        raise ExportError("render_unavailable", "No valid rendered preview is available for final export.",
                          {"qc_report_path": report["report_path"]})
    try:
        with timeline_lock(folder):
            if hashlib.sha256((folder / "timeline.json").read_bytes()).hexdigest() != report["timeline_sha256"]:
                raise ExportError("timeline_changed", "Timeline changed after QC; rerun QC before exporting.")
            if hashlib.sha256(candidate.read_bytes()).hexdigest() != report["preview_sha256"]:
                raise ExportError("preview_changed", "Rendered preview changed after QC; rerun QC before exporting.")
            output = folder / "output"
            output.mkdir(exist_ok=True)
            if output.resolve().parent != folder:
                raise ExportError("invalid_output_directory", "Final export directory must stay inside its project.")
            destination = output / f"{metadata.name}_FINAL.mp4"
            timeline = Timeline.model_validate_json((folder / "timeline.json").read_text(encoding="utf-8"))
            references = []
            for collection in ("video_tracks", "audio_tracks", "music_tracks", "sfx_tracks"):
                for track in getattr(timeline, collection):
                    for clip in track.clips:
                        references.append(clip.source if collection in ("video_tracks", "audio_tracks") else clip.file)
            references.extend(track.file for track in timeline.subtitle_tracks if track.file is not None)
            destination_resolved = destination.resolve(strict=False)
            def conflicts_with_source(value):
                source = _native_path(value)
                if not source.is_absolute():
                    source = folder / source
                return source.resolve(strict=False) == destination_resolved
            if any(conflicts_with_source(value) for value in references):
                raise ExportError("final_output_source_conflict", "Final export path is referenced by timeline media; refusing to replace source content.",
                                  {"export_path": str(destination)})
            info = probe_media(candidate)
            if (info["video_codec"] != "h264" or info["audio_codec"] != "aac"
                    or info["width"] != metadata.resolution.width or info["height"] != metadata.resolution.height
                    or not math.isclose(info["fps"], metadata.fps, rel_tol=1e-5)
                    or info["sample_rate"] != metadata.audio_sample_rate):
                raise ExportError("invalid_final_format", "Rendered candidate does not meet the required H.264/AAC project format.", info)
            temporary = output / f".{metadata.name}-{uuid4().hex}.tmp.mp4"
            try:
                shutil.copyfile(candidate, temporary)
                if hashlib.sha256(temporary.read_bytes()).hexdigest() != report["preview_sha256"]:
                    raise ExportError("preview_changed", "Preview changed while copying; final file was not installed.")
                final_info = probe_media(temporary)
                if (final_info["video_codec"] != "h264" or final_info["audio_codec"] != "aac"
                        or final_info["width"] != metadata.resolution.width or final_info["height"] != metadata.resolution.height
                        or not math.isclose(final_info["fps"], metadata.fps, rel_tol=1e-5)
                        or final_info["sample_rate"] != metadata.audio_sample_rate
                        or abs(final_info["duration"] - report["timeline_duration"]) > 1 / metadata.fps + 0.03):
                    raise ExportError("invalid_final_output", "Final MP4 failed ffprobe verification.", final_info)
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        return {"success": True, "project": metadata.name, "export_path": str(destination),
                "qc_passed": report["passed"], "forced": force, "qc_report_path": report["report_path"],
                "metadata": probe_media(destination)}
    except ExportError:
        raise
    except (OSError, MediaError, TimelineError, ValueError, RuntimeError) as exc:
        details = exc.to_dict() if callable(getattr(exc, "to_dict", None)) else {"message": str(exc)}
        raise ExportError("export_failed", f"Final export failed: {exc}", details) from exc
