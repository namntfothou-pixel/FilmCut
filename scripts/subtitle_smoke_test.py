"""Opt-in real Whisper/MCP transcription test using a supplied speech recording.

Unlike the regular smoke test, this can download the configured small Whisper
model on first use. No speech fixture or model is downloaded by this script
except through faster-whisper's normal model loader.
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from engine.ffmpeg import run_ffmpeg
from engine.media import probe_media
from engine.subtitle import read_srt


async def run_test(speech: Path, language: str, root: Path):
    source = root / "sources"
    source.mkdir()
    video = source / "speech.mp4"
    run_ffmpeg(["-n", "-f", "lavfi", "-i", "color=c=black:s=320x240:r=24", "-i", str(speech.resolve()),
                "-map", "0:v:0", "-map", "1:a:0", "-shortest", "-c:v", "libx264", "-threads", "1",
                "-c:a", "aac", str(video)])
    info = probe_media(video)
    params = StdioServerParameters(command=sys.executable, args=[str(REPOSITORY / "mcp_server.py")],
        cwd=str(REPOSITORY), env={**os.environ, "FILMCUT_PROJECTS_ROOT": str(root / "projects")})
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            async def call(name, arguments):
                result = await session.call_tool(name, arguments, read_timeout_seconds=timedelta(seconds=1800))
                assert not result.isError and result.structuredContent["success"], result
                return result.structuredContent["data"]
            created = await call("create_project", {"name": "SubtitleSmoke", "source_folder": str(source)})
            project = Path(created["project_path"])
            metadata = created["project"]
            metadata["resolution"] = {"width": 320, "height": 240}
            (project / "project.json").write_text(json.dumps(metadata), encoding="utf-8")
            # The initial timeline has no clips yet; update its canvas to match.
            timeline = json.loads((project / "timeline.json").read_text(encoding="utf-8"))
            timeline["width"], timeline["height"] = 320, 240
            (project / "timeline.json").write_text(json.dumps(timeline), encoding="utf-8")
            await call("add_clip", {"project": "SubtitleSmoke", "source": str(video), "source_in": 0,
                                   "source_out": info["duration"], "position": 0})
            transcript = await call("generate_subtitles", {"project": "SubtitleSmoke", "language": language})
            assert transcript["segments"], "No speech segments detected; supply a clear speech recording"
            assert read_srt(Path(transcript["srt_path"]))
            rendered = await call("render_preview", {"project": "SubtitleSmoke", "burn_subtitles": True})
            output = Path(rendered["preview_path"])
            run_ffmpeg(["-i", str(output), "-f", "null", "-"])
            assert (await call("ping", {}))["status"] == "ok"
            return {"status": "PASS", "model": transcript["model"], "language": language,
                    "segments": transcript["segments"], "srt_path": transcript["srt_path"],
                    "preview_path": str(output), "metadata": rendered["metadata"], "full_decode": "PASS"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("speech", type=Path, help="Existing audio or video recording containing clear speech")
    parser.add_argument("--language", choices=["en", "vi"], default="en")
    parser.add_argument("--output-root", type=Path, help="Retain results in a new, empty directory")
    options = parser.parse_args()
    if not options.speech.is_file():
        parser.error("Speech recording must exist")
    if options.output_root:
        root = options.output_root.resolve()
        root.mkdir()  # Never silently overwrite an earlier test's artifacts.
        print(json.dumps(asyncio.run(run_test(options.speech, options.language, root)), ensure_ascii=False, indent=2))
    else:
        with tempfile.TemporaryDirectory(prefix="filmcut-subtitles-") as temporary:
            result = asyncio.run(run_test(options.speech, options.language, Path(temporary)))
            result["artifacts_retained"] = False
            print(json.dumps(result, ensure_ascii=False, indent=2))
