"""Local transcription, UTF-8 SRT interchange, and plain subtitle burn-in."""

import math
import os
import re
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field

from engine.ffmpeg import encoding_arguments, run_ffmpeg
from schemas.timeline import SubtitleCue


class SubtitleError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

    def to_dict(self):
        return {"code": self.code, "message": str(self)}


class WhisperSettings(BaseModel):
    model: str = Field(default="tiny", min_length=1)
    device: str = "cpu"
    compute_type: str = "int8"
    cpu_threads: int = Field(default=2, ge=1)
    download_root: str = str(Path(__file__).resolve().parents[1] / ".cache" / "whisper")
    local_files_only: bool = False

    @classmethod
    def from_environment(cls):
        mapping = {"model": "MODEL", "device": "DEVICE", "compute_type": "COMPUTE_TYPE",
                   "cpu_threads": "THREADS", "download_root": "CACHE", "local_files_only": "LOCAL_ONLY"}
        try:
            return cls(**{key: os.environ[f"FILMCUT_WHISPER_{suffix}"] for key, suffix in mapping.items()
                          if f"FILMCUT_WHISPER_{suffix}" in os.environ})
        except ValueError as exc:
            raise SubtitleError("invalid_whisper_configuration", str(exc)) from exc


def normalize_language(language: str) -> str:
    languages = {"en": "en", "english": "en", "vi": "vi", "vietnamese": "vi"}
    try:
        return languages[language.strip().casefold()]
    except (KeyError, AttributeError) as exc:
        raise SubtitleError("unsupported_language", "Use en/English or vi/Vietnamese") from exc


@lru_cache(maxsize=1)
def _load_model(model, device, compute_type, cpu_threads, download_root, local_files_only):
    # Import and download only on an explicit transcription request, never at startup.
    try:
        from faster_whisper import WhisperModel
        return WhisperModel(model, device=device, compute_type=compute_type, cpu_threads=cpu_threads,
                            download_root=download_root, local_files_only=local_files_only)
    except ImportError as exc:
        raise SubtitleError("whisper_missing", "Install requirements.txt inside the FilmCut .venv") from exc
    except Exception as exc:
        raise SubtitleError("whisper_model_unavailable", f"Cannot load Whisper model {model!r}: {exc}. "
                            "Check network/cache access or configure FILMCUT_WHISPER_MODEL with a local model directory.") from exc


def transcribe_audio(audio: Path, language: str, *, settings: WhisperSettings | None = None) -> dict:
    language = normalize_language(language)
    settings = settings or WhisperSettings.from_environment()
    if language == "vi" and settings.model.endswith(".en"):
        raise SubtitleError("unsupported_model_language", "Vietnamese requires a multilingual model, e.g. tiny or base")
    model = _load_model(**settings.model_dump())
    try:
        segments, info = model.transcribe(str(audio), language=language, task="transcribe", beam_size=5,
                                          vad_filter=True, condition_on_previous_text=False)
        cues = []
        for segment in segments:  # Inference is lazy; consume within the error boundary.
            text = segment.text.strip()
            if not text:
                continue
            start, end = float(segment.start), float(segment.end)
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
                raise ValueError("Whisper returned an invalid segment interval")
            if cues and start < cues[-1].timeline_end:
                raise ValueError("Whisper returned overlapping segment intervals")
            cues.append(SubtitleCue(id=f"segment-{len(cues) + 1}", text=text, timeline_start=start, timeline_end=end))
        return {"language": language, "model": settings.model, "duration": float(info.duration),
                "segments": [{"start": cue.timeline_start, "end": cue.timeline_end, "text": cue.text} for cue in cues]}
    except SubtitleError:
        raise
    except Exception as exc:
        raise SubtitleError("transcription_failed", f"Whisper transcription failed: {exc}") from exc


def format_timestamp(seconds: float) -> str:
    if not math.isfinite(seconds) or seconds < 0:
        raise SubtitleError("invalid_srt_time", "SRT time must be finite and nonnegative")
    milliseconds = math.floor(seconds * 1000 + 0.5)
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    whole_seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def format_srt(segments: list[dict]) -> str:
    blocks, previous_end = [], 0
    try:
        for index, segment in enumerate(segments, 1):
            start, end = float(segment["start"]), float(segment["end"])
            text = segment["text"].replace("\r\n", "\n").replace("\r", "\n").strip()
            if not text or re.search(r"\n[ \t]*\n", text) or "\x00" in text:
                raise ValueError("SRT text cannot be empty or contain blank lines/NUL")
            if not math.isfinite(end) or end <= start or start < previous_end:
                raise ValueError("SRT segments must be ordered, positive-length, and non-overlapping")
            start_string, end_string = format_timestamp(start), format_timestamp(end)
            if start_string == end_string:
                raise ValueError("SRT segment must last at least one rounded millisecond")
            blocks.append(f"{index}\n{start_string} --> {end_string}\n{text}\n\n")
            previous_end = end
        return "".join(blocks)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise SubtitleError("invalid_srt", str(exc)) from exc


_TIMING = re.compile(r"^(\d{2,}):([0-5]\d):([0-5]\d),(\d{3}) --> (\d{2,}):([0-5]\d):([0-5]\d),(\d{3})$")


def parse_srt(text: str) -> list[dict]:
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    segments = []
    try:
        for index, block in enumerate(re.split(r"\n[ \t]*\n", text), 1):
            lines = block.splitlines()
            match = _TIMING.fullmatch(lines[1]) if len(lines) >= 3 else None
            if not match or lines[0].strip() != str(index):
                raise ValueError("Expected numbered SRT blocks with HH:MM:SS,mmm timestamps")
            values = list(map(int, match.groups()))
            times = [values[offset] * 3600 + values[offset + 1] * 60 + values[offset + 2] + values[offset + 3] / 1000
                     for offset in (0, 4)]
            segments.append({"start": times[0], "end": times[1], "text": "\n".join(lines[2:])})
        format_srt(segments)
        return segments
    except (ValueError, IndexError) as exc:
        raise SubtitleError("invalid_srt", str(exc)) from exc


def read_srt(path: Path) -> list[dict]:
    try:
        return parse_srt(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError) as exc:
        raise SubtitleError("subtitle_read_failed", f"Cannot read UTF-8 SRT {path}: {exc}") from exc


def burn_subtitles(video: Path, track, folder: Path, workspace: Path, output: Path) -> bool:
    if track.file is not None:
        from engine.timeline import _native_path
        file = _native_path(track.file)
        segments = read_srt(file if file.is_absolute() else folder / file)
    else:
        segments = [{"start": cue.timeline_start, "end": cue.timeline_end, "text": cue.text}
                    for cue in track.cues if cue.enabled]
    if not segments:
        return False
    # A fixed local filename avoids FFmpeg filter escaping bugs for Windows
    # drive letters, apostrophes, spaces, and non-ASCII project paths.
    (workspace / "captions.srt").write_text(format_srt(segments), encoding="utf-8")
    run_ffmpeg(["-n", "-i", str(video.resolve()), "-map", "0:v:0", "-map", "0:a:0", "-filter_threads", "1",
                "-vf", "subtitles=filename=captions.srt:charenc=UTF-8", *encoding_arguments(),
                "-c:a", "copy", str(output.resolve())], cwd=workspace)
    return True
