"""Standalone official-SDK stdio test, isolated from real FilmCut projects."""

import asyncio
import argparse
import json
import os
import sys
import tempfile
import tomllib
from datetime import timedelta
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from engine.ffmpeg import run_ffmpeg
TOOLS = {"ping", "create_project", "get_project", "analyze_folder", "get_timeline", "create_timeline", "render_preview",
         "add_clip", "remove_clip", "trim_clip", "move_clip", "set_clip_speed"}


async def run_smoke_test(configuration=None):
    with tempfile.TemporaryDirectory(prefix="filmcut-mcp-") as temporary:
        root = Path(temporary)
        source = root / "source media"
        source.mkdir()
        video = source / "synthetic.mp4"
        run_ffmpeg(["-n", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=24", "-t", "0.5",
                    "-c:v", "mpeg4", "-threads", "1", str(video)])
        (source / "broken.mov").write_bytes(b"invalid media")
        settings = configuration or {"command": sys.executable,
                                     "args": [str(REPOSITORY / "mcp_server.py")], "cwd": str(REPOSITORY)}
        params = StdioServerParameters(command=settings["command"], args=settings["args"],
                                       cwd=settings["cwd"], env={**os.environ, **settings.get("env", {}),
                                       "FILMCUT_PROJECTS_ROOT": str(root / "projects")})
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as session:
                await session.initialize()
                tools = {tool.name for tool in (await session.list_tools()).tools}
                assert tools == TOOLS, tools

                async def call(name, arguments=None, *, success=True):
                    result = await session.call_tool(name, arguments or {}, read_timeout_seconds=timedelta(seconds=90))
                    assert not result.isError, result
                    payload = result.structuredContent
                    assert isinstance(payload, dict), result
                    json.dumps(payload, allow_nan=False)
                    assert payload["success"] is success, payload
                    if not success:
                        assert payload["error"]["code"] and payload["error"]["message"], payload
                    return payload

                await call("ping")
                await call("get_project", {"name": "Missing"}, success=False)
                await call("create_project", {"name": "../invalid", "source_folder": str(source)}, success=False)
                await call("create_project", {"name": "Missing Source", "source_folder": str(root / "missing")}, success=False)
                await call("create_project", {"name": "Smoke", "source_folder": str(source)})
                duplicate = await call("create_project", {"name": "Smoke", "source_folder": str(source)}, success=False)
                assert duplicate["error"]["code"] == "project_exists"
                details = await call("get_project", {"name": "Smoke"})
                project = Path(details["data"]["project_path"])
                analyzed = await call("analyze_folder", {"project": "Smoke"})
                assert len(analyzed["data"]["sources"]) == 1
                assert len(analyzed["data"]["errors"]) == 1 and not analyzed["data"]["complete"]
                assert (project / "source_index.json").is_file()
                await call("get_timeline", {"project": "Smoke"})
                created = await call("create_timeline", {"project": "Smoke"})
                assert not created["data"]["created"]
                await call("render_preview", {"project": "Smoke"}, success=False)
                await call("ping")  # A project error has not terminated the process.
                added = await call("add_clip", {"project": "Smoke", "source": str(video), "source_in": 0,
                                                "source_out": 0.5, "position": 0})
                clip_id = added["data"]["clip_id"]
                assert Path(added["data"]["backup_path"]).is_file()
                await call("trim_clip", {"project": "Smoke", "clip_id": clip_id, "source_in": 0.1, "source_out": 0.4})
                await call("move_clip", {"project": "Smoke", "clip_id": clip_id, "position": 1})
                await call("set_clip_speed", {"project": "Smoke", "clip_id": clip_id, "speed": 2})
                await call("set_clip_speed", {"project": "Smoke", "clip_id": clip_id, "speed": 0}, success=False)
                await call("set_clip_speed", {"project": "Smoke", "clip_id": clip_id, "speed": 1})
                await call("move_clip", {"project": "Smoke", "clip_id": clip_id, "position": 0})
                await call("trim_clip", {"project": "Smoke", "clip_id": clip_id, "source_in": 0, "source_out": 0.5})
                await call("add_clip", {"project": "Smoke", "source": str(video), "source_in": 0,
                                        "source_out": 0.5, "position": 0}, success=False)
                before = (project / "timeline.json").read_bytes()
                await call("create_timeline", {"project": "Smoke"})
                assert (project / "timeline.json").read_bytes() == before
                rendered = await call("render_preview", {"project": "Smoke"})
                data = rendered["data"]
                assert Path(data["preview_path"]).is_file()
                assert data["metadata"]["video_codec"] == "h264"
                assert data["metadata"]["audio_codec"] == "aac"
                assert data["metadata"]["sample_rate"] == 48000
                assert abs(data["metadata"]["duration"] - 0.5) < 0.07
                await call("remove_clip", {"project": "Smoke", "clip_id": clip_id})
                removed = await call("get_timeline", {"project": "Smoke"})
                assert not removed["data"]["timeline"]["video_tracks"][0]["clips"]
                # Missing timeline initialization is also tested independently.
                (project / "timeline.json").unlink()
                recreated = await call("create_timeline", {"project": "Smoke"})
                assert recreated["data"]["created"]
                malformed = await session.call_tool("get_project", {})
                assert malformed.isError  # SDK argument validation error, process stays alive.
                await call("ping")
                return {"status": "PASS", "transport": "stdio", "tools": sorted(tools),
                        "project_error_recovery": "PASS", "preview_render": "PASS", "timeline_editing": "PASS"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-config", type=Path, help="Read and test mcp_servers.filmcut from this Codex config.toml")
    options = parser.parse_args()
    configuration = None
    if options.codex_config:
        with options.codex_config.open("rb") as handle:
            configuration = tomllib.load(handle)["mcp_servers"]["filmcut"]
        for value in (configuration["command"], configuration["cwd"], *configuration["args"]):
            if not Path(value).is_absolute() or not Path(value).exists():
                raise ValueError(f"Expected an existing absolute FilmCut configuration path: {value}")
    print(json.dumps(asyncio.run(run_smoke_test(configuration)), indent=2))
