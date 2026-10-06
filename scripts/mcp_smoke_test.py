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
         "add_clip", "remove_clip", "trim_clip", "move_clip", "set_clip_speed",
         "add_music", "remove_music", "update_music", "add_sfx", "remove_sfx", "update_sfx",
         "list_sfx_library", "search_sfx_by_tags", "generate_subtitles", "set_transition",
         "set_j_cut", "set_l_cut", "reset_audio_offset", "analyze_source", "get_source_analysis",
         "analyze_script", "get_script_breakdown", "find_candidates_for_scene", "rank_sources_for_script"}


async def run_smoke_test(configuration=None):
    with tempfile.TemporaryDirectory(prefix="filmcut-mcp-") as temporary:
        root = Path(temporary)
        source = root / "source media"
        source.mkdir()
        video = source / "synthetic.mp4"
        run_ffmpeg(["-n", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=24", "-f", "lavfi", "-i",
                    "sine=frequency=480:sample_rate=48000", "-t", "2",
                    "-c:v", "mpeg4", "-threads", "1", "-c:a", "aac", str(video)])
        music = source / "BGM.wav"
        run_ffmpeg(["-n", "-f", "lavfi", "-i", "sine=frequency=960:sample_rate=44100", "-t", "0.2",
                    "-c:a", "pcm_s16le", str(music)])
        (source / "broken.mov").write_bytes(b"invalid media")
        library = root / "sfx library"
        (library / "audio").mkdir(parents=True)
        effect = library / "audio" / "impact_concrete_03.wav"
        run_ffmpeg(["-n", "-f", "lavfi", "-i", "sine=frequency=1440:sample_rate=48000", "-t", "0.2",
                    "-c:a", "pcm_s16le", str(effect)])
        (library / "library.json").write_text(json.dumps({"version": 1, "items": [
            {"id": "impact_concrete_03", "file": "audio/impact_concrete_03.wav",
             "tags": ["impact", "body", "wall", "concrete", "heavy"]}]}), encoding="utf-8")
        settings = configuration or {"command": sys.executable,
                                     "args": [str(REPOSITORY / "mcp_server.py")], "cwd": str(REPOSITORY)}
        params = StdioServerParameters(command=settings["command"], args=settings["args"],
                                       cwd=settings["cwd"], env={**os.environ, **settings.get("env", {}),
                                       "FILMCUT_PROJECTS_ROOT": str(root / "projects"),
                                       "FILMCUT_SFX_LIBRARY": str(library)})
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
                source_id = analyzed["data"]["sources"][0]["id"]
                source_args = {"project": "Smoke", "source_id": source_id}
                missing_provider = await call("analyze_source", source_args, success=False)
                assert missing_provider["error"]["code"] == "analysis_provider_not_configured"
                observations = {"source_id": source_id, "characters": [], "location": None,
                    "shot_size": None, "camera_angle": None, "camera_motion": None,
                    "action": None, "emotion": None, "dialogue": None, "visual_quality": None,
                    "continuity_notes": [], "usable_start": 0, "usable_end": 1,
                    "problems": [], "description": "Synthetic test pattern; supplied mock observations"}
                before_analysis = (project / "timeline.json").read_bytes()
                await call("analyze_source", {**source_args, "analysis": observations})
                saved_analysis = await call("get_source_analysis", source_args)
                assert saved_analysis["data"]["analysis"] == observations
                assert (project / "timeline.json").read_bytes() == before_analysis
                script_result = await call("analyze_script", {"project": "Smoke", "script":
                    "INT. ROOM - NIGHT\nMai opens the letter.\nMAI: You came back.\n\nEXT. GATE - NIGHT\nAN: I promised."})
                assert len(script_result["data"]["breakdown"]["requirements"]) == 2
                assert (await call("get_script_breakdown", {"project": "Smoke"}))["data"] == script_result["data"]
                candidates = await call("find_candidates_for_scene", {"project": "Smoke", "scene_id": "scene_001"})
                assert candidates["data"]["candidates"][0]["source_id"] == source_id
                ranked = await call("rank_sources_for_script", {"project": "Smoke"})
                assert len(ranked["data"]["scenes"]) == 2
                await call("find_candidates_for_scene", {"project": "Smoke", "scene_id": "missing"}, success=False)
                await call("analyze_script", {"project": "Smoke", "script": ""}, success=False)
                assert (project / "timeline.json").read_bytes() == before_analysis
                await call("get_timeline", {"project": "Smoke"})
                # No network/model downloads in the general server smoke test.
                subtitle_error = await call("generate_subtitles", {"project": "Smoke", "language": "invalid"}, success=False)
                assert subtitle_error["error"]["code"] == "unsupported_language"
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
                added_music = await call("add_music", {"project": "Smoke", "file": str(music), "loop": True,
                                                       "volume_db": -12, "fade_in": 0.05, "fade_out": 0.05})
                music_id = added_music["data"]["music_id"]
                await call("update_music", {"project": "Smoke", "music_id": music_id, "volume_db": -18})
                await call("update_music", {"project": "Smoke", "music_id": music_id, "fade_in": -1}, success=False)
                catalog = await call("list_sfx_library")
                assert len(catalog["data"]["items"]) == 1
                found = await call("search_sfx_by_tags", {"tags": ["IMPACT", "concrete", "heavy"]})
                assert found["data"]["items"][0]["id"] == "impact_concrete_03"
                await call("search_sfx_by_tags", {"tags": []}, success=False)
                effects = []
                for _ in range(3):
                    added_sfx = await call("add_sfx", {"project": "Smoke", "file": str(effect),
                        "timeline_time": 0.1, "volume_db": -12, "fade_in": 0.01, "fade_out": 0.01,
                        "tags": ["impact", "concrete"]})
                    effects.append(added_sfx["data"]["sfx_id"])
                await call("update_sfx", {"project": "Smoke", "sfx_id": effects[0], "volume_db": -18})
                await call("update_sfx", {"project": "Smoke", "sfx_id": effects[0], "timeline_time": -1}, success=False)
                await call("remove_sfx", {"project": "Smoke", "sfx_id": "missing"}, success=False)
                rendered = await call("render_preview", {"project": "Smoke"})
                data = rendered["data"]
                assert Path(data["preview_path"]).is_file()
                assert data["metadata"]["video_codec"] == "h264"
                assert data["metadata"]["audio_codec"] == "aac"
                assert data["metadata"]["sample_rate"] == 48000
                assert abs(data["metadata"]["duration"] - 0.5) < 0.07
                following = await call("add_clip", {"project": "Smoke", "source": str(video), "source_in": 0.25,
                                                    "source_out": 0.75, "position": 0.5})
                for kind in ("crossfade", "fade_to_black"):
                    changed = await call("set_transition", {"project": "Smoke", "clip_id": clip_id,
                                                            "transition_type": kind, "duration": 0.125})
                    assert changed["data"]["duration"] == 0.875
                    rendered = await call("render_preview", {"project": "Smoke"})
                    assert abs(rendered["data"]["metadata"]["duration"] - 0.875) < 0.07
                await call("set_transition", {"project": "Smoke", "clip_id": clip_id,
                                              "transition_type": "wipe", "duration": 0.125}, success=False)
                await call("set_transition", {"project": "Smoke", "clip_id": clip_id,
                                              "transition_type": "cut", "duration": 0})
                await call("set_j_cut", {"project": "Smoke", "clip_id": following["data"]["clip_id"], "duration": 0.1})
                await call("set_l_cut", {"project": "Smoke", "clip_id": clip_id, "duration": 0.1})
                offset_render = await call("render_preview", {"project": "Smoke"})
                assert abs(offset_render["data"]["metadata"]["duration"] - 1) < 0.07
                await call("set_j_cut", {"project": "Smoke", "clip_id": following["data"]["clip_id"], "duration": 10}, success=False)
                for id_to_reset in (clip_id, following["data"]["clip_id"]):
                    await call("reset_audio_offset", {"project": "Smoke", "clip_id": id_to_reset})
                await call("remove_clip", {"project": "Smoke", "clip_id": following["data"]["clip_id"]})
                for sfx_id in effects:
                    await call("remove_sfx", {"project": "Smoke", "sfx_id": sfx_id})
                await call("remove_music", {"project": "Smoke", "music_id": music_id})
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
                        "project_error_recovery": "PASS", "preview_render": "PASS", "timeline_editing": "PASS",
                        "music_mixing": "PASS", "sfx_mixing": "PASS", "sfx_library": "PASS",
                        "subtitle_error_recovery": "PASS", "transitions": "PASS", "audio_offsets": "PASS"}


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
