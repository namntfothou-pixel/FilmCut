"""Bounded ffprobe metadata inspection and non-destructive source indexing."""

import json
import math
import os
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path, PureWindowsPath
from uuid import NAMESPACE_URL, uuid5

SUPPORTED_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm"})
PROBE_TIMEOUT = 30


class MediaError(Exception):
    """An error that callers can serialize for MCP or other interfaces."""

    def __init__(self, code: str, message: str, path: str | Path = ""):
        super().__init__(message)
        self.code = code
        self.path = str(path)

    def to_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "path": self.path}


def _existing_path(value: str | Path, *, directory: bool = False) -> Path:
    try:
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise ValueError("A nonempty local path is required.")
        if os.name != "nt" and PureWindowsPath(str(value)).drive:
            raise MediaError("unsupported_path", "Windows drive and UNC paths require Windows.", value)
        path = Path(value).expanduser().resolve(strict=True)
        if not (path.is_dir() if directory else path.is_file()):
            raise ValueError("Expected a directory." if directory else "Expected a regular file.")
        return path
    except (OSError, ValueError, RuntimeError) as exc:
        raise MediaError("invalid_path", str(exc), value) from exc


def _number(value, *, rational: bool = False) -> float:
    try:
        number = float(Fraction(str(value))) if rational else float(value)
        return number if math.isfinite(number) and number >= 0 else 0.0
    except (ValueError, TypeError, ZeroDivisionError, OverflowError):
        return 0.0


def probe_media(path: str | Path) -> dict:
    """Return the requested metadata dictionary or raise a structured MediaError.

    Select the first non-cover-art video stream and first audio stream. Unknown
    numeric metadata is zero. ffprobe reads metadata, never emits frames or
    packets; Python captures only its small selected JSON metadata output.
    """
    source = _existing_path(path)
    if source.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise MediaError("unsupported_format", "Supported formats: mp4, mov, mkv, webm.", source)
    command = [
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,duration,sample_rate:stream_disposition=attached_pic",
        "-of", "json", str(source),
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=PROBE_TIMEOUT,
                                   check=False, shell=False)
    except FileNotFoundError as exc:
        raise MediaError("ffprobe_missing", "ffprobe is not available on PATH.", source) from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError("probe_timeout", "ffprobe exceeded its 30-second timeout.", source) from exc
    except OSError as exc:
        raise MediaError("probe_failed", str(exc), source) from exc
    if completed.returncode != 0:
        raise MediaError("invalid_video", completed.stderr.strip()[:2000] or "ffprobe rejected the file.", source)
    try:
        data = json.loads(completed.stdout)
        streams = data.get("streams", [])
        video = next((stream for stream in streams if stream.get("codec_type") == "video"
                      and not stream.get("disposition", {}).get("attached_pic", 0)), None)
        if video is None:
            raise MediaError("invalid_video", "No video stream found.", source)
        audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
        width, height = int(video.get("width", 0)), int(video.get("height", 0))
        if width <= 0 or height <= 0:
            raise ValueError("Video has no valid resolution.")
        duration = _number(data.get("format", {}).get("duration")) or _number(video.get("duration"))
        fps = _number(video.get("avg_frame_rate"), rational=True) or _number(video.get("r_frame_rate"), rational=True)
        return {
            "id": str(uuid5(NAMESPACE_URL, os.path.normcase(str(source)))),
            "path": str(source), "filename": source.name, "duration": duration,
            "width": width, "height": height, "fps": fps,
            "video_codec": video.get("codec_name", ""), "has_audio": audio is not None,
            "audio_codec": audio.get("codec_name", "") if audio else "",
            "sample_rate": int(_number(audio.get("sample_rate"))) if audio else 0,
        }
    except (ValueError, TypeError, AttributeError, KeyError) as exc:
        raise MediaError("invalid_metadata", f"Invalid ffprobe metadata: {exc}", source) from exc


def scan_folder(folder: str | Path) -> dict:
    """Recursively return {sources, errors}; corrupt videos do not hide good ones.

    Directory traversal failures raise MediaError. Symlink directories are not
    followed, preventing cycles. File extensions are matched without case.
    """
    root = _existing_path(folder, directory=True)
    sources, errors = [], []

    def walk_error(exc):
        raise MediaError("scan_failed", str(exc), exc.filename or root) from exc

    for current, directories, filenames in os.walk(root, onerror=walk_error, followlinks=False):
        directories.sort()
        for filename in sorted(filenames):
            path = Path(current) / filename
            if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            try:
                sources.append(probe_media(path))
            except MediaError as exc:
                if exc.code == "ffprobe_missing":
                    raise
                errors.append(exc.to_dict())
    return {"sources": sources, "errors": errors}


def write_source_index(project_folder: str | Path, scan_result: dict) -> Path:
    """Atomically write a scan result to an existing project's source_index.json.

    The result includes per-file errors, so a partial scan is explicit. This
    operation does not modify project metadata, timeline, or source media.
    """
    project = _existing_path(project_folder, directory=True)
    if not (project / "project.json").is_file() or not (project / "timeline.json").is_file():
        raise MediaError("invalid_project", "Expected an initialized FilmCut project.", project)
    temporary = None
    try:
        if not isinstance(scan_result, dict) or not isinstance(scan_result.get("sources"), list) or not isinstance(scan_result.get("errors"), list):
            raise ValueError("Expected a scan_folder result with sources and errors lists.")
        document = {"version": 1, "sources": scan_result["sources"], "errors": scan_result["errors"]}
        contents = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=project,
                                         prefix=".source_index-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(contents)
        destination = project / "source_index.json"
        temporary.replace(destination)
        return destination
    except (OSError, ValueError, TypeError) as exc:
        raise MediaError("index_write_failed", str(exc), project) from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # Preserve the original write error; this is only a scratch file.
