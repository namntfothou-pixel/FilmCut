"""Reusable FFmpeg execution and normalization helpers; never invoke a shell."""

import math
import subprocess
from pathlib import Path


class FFmpegError(Exception):
    def __init__(self, code: str, message: str, *, stderr: str = "", returncode=None):
        super().__init__(message)
        self.code, self.stderr, self.returncode = code, stderr[-8000:], returncode

    def to_dict(self):
        return {"code": self.code, "message": str(self), "stderr": self.stderr,
                "returncode": self.returncode}


def run_ffmpeg(arguments: list[str], *, timeout: float = 300, cwd: Path | None = None) -> None:
    """Run an argument vector, surfacing FFmpeg diagnostics and exit status."""
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", *arguments]
    try:
        result = subprocess.run(command, shell=False, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout, check=False, cwd=cwd)
    except FileNotFoundError as exc:
        raise FFmpegError("ffmpeg_missing", "FFmpeg is not available on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        stderr = exc.stderr or ""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        raise FFmpegError("ffmpeg_timeout", f"FFmpeg exceeded {timeout} seconds.", stderr=stderr) from exc
    except OSError as exc:
        raise FFmpegError("ffmpeg_start_failed", str(exc)) from exc
    if result.returncode:
        raise FFmpegError("ffmpeg_failed", f"FFmpeg exited with code {result.returncode}: {result.stderr[-2000:]}",
                          stderr=result.stderr, returncode=result.returncode)


def normalization_filter(width: int, height: int, fps: float) -> str:
    """Convert display aspect to square pixels, then fit and pad to the canvas."""
    if width <= 0 or height <= 0 or width % 2 or height % 2:
        raise FFmpegError("invalid_resolution", "H.264 yuv420p requires positive even dimensions.")
    if not math.isfinite(fps) or fps <= 0:
        raise FFmpegError("invalid_fps", "FPS must be finite and positive.")
    return (
        "scale=w='max(2,trunc(iw*sar/2)*2)':h=ih,setsar=1,"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={fps:.12g},format=yuv420p"
    )


def encoding_arguments() -> list[str]:
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k", "-movflags", "+faststart",
            "-threads", "2"]


def concatenate_normalized(clips: list[Path], durations: list[float], output: Path,
                           *, fps: float) -> None:
    """Join prepared clips; filter fragments refer only to numeric input indices.

    Reset timestamps and trim AAC padding before joining. This small graph only
    concatenates normalized intermediates, never interprets raw editing intent.
    """
    if not clips or len(clips) != len(durations) or any(not math.isfinite(d) or d <= 0 for d in durations):
        raise FFmpegError("invalid_concat", "Expected normalized clips with positive durations.")
    arguments = ["-n"]
    filters, inputs = [], []
    for index, (clip, duration) in enumerate(zip(clips, durations)):
        arguments += ["-i", str(clip)]
        filters += [f"[{index}:v:0]setpts=PTS-STARTPTS[v{index}]",
                    f"[{index}:a:0]atrim=duration={duration:.12g},asetpts=PTS-STARTPTS[a{index}]"]
        inputs.append(f"[v{index}][a{index}]")
    filters.append("".join(inputs) + f"concat=n={len(clips)}:v=1:a=1[v][a]")
    arguments += ["-filter_complex_threads", "1", "-filter_complex", ";".join(filters),
                  "-map", "[v]", "-map", "[a]", "-r", f"{fps:.12g}",
                  "-t", f"{sum(durations):.12g}", *encoding_arguments(), str(output)]
    run_ffmpeg(arguments)
