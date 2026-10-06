import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from mcp_server import build_server
from services import project_service

REPOSITORY = Path(__file__).resolve().parents[1]


def test_standalone_stdio_smoke_test():
    result = subprocess.run([sys.executable, str(REPOSITORY / "scripts" / "mcp_smoke_test.py")],
                            cwd=REPOSITORY, capture_output=True, text=True, timeout=90, shell=False)
    assert result.returncode == 0, result.stderr + result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "PASS"
    assert payload["preview_render"] == "PASS"
    assert len(payload["tools"]) == 15
    assert payload["timeline_editing"] == "PASS"
    assert payload["music_mixing"] == "PASS"


def test_unexpected_service_failure_is_contained(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("simulated service error")
    monkeypatch.setattr(project_service, "get_project", fail)
    server = build_server(tmp_path)

    async def exercise():
        result = await server.call_tool("get_project", {"name": "Example"})
        # FastMCP internally returns content plus a structured dictionary.
        payload = result[1] if isinstance(result, tuple) else result
        assert payload["success"] is False
        assert payload["error"]["code"] == "internal_error"
        assert "simulated service error" in payload["error"]["message"]
        alive = await server.call_tool("ping", {})
        payload = alive[1] if isinstance(alive, tuple) else alive
        assert payload["success"]
    asyncio.run(exercise())


def test_get_project_service(tmp_path):
    result = project_service.create_project("Example", tmp_path, projects_root=tmp_path / "projects")
    loaded = project_service.get_project("Example", projects_root=tmp_path / "projects")
    assert loaded.success and loaded.project == result.project
    assert loaded.project_path == result.project_path
    assert project_service.get_project("../bad", projects_root=tmp_path).error.code == "invalid_project_name"
    assert project_service.get_project("Missing", projects_root=tmp_path).error.code == "project_not_found"
    (result.project_path / "project.json").write_text("invalid json")
    assert project_service.get_project("Example", projects_root=tmp_path / "projects").error.code == "project_read_failed"


def test_stdio_using_actual_configured_paths(tmp_path):
    configuration = tmp_path / "config.toml"
    # JSON string quoting also produces valid TOML basic path strings.
    configuration.write_text(
        '[mcp_servers.filmcut]\n'
        f'command = {json.dumps(sys.executable)}\n'
        f'args = [{json.dumps(str(REPOSITORY / "mcp_server.py"))}]\n'
        f'cwd = {json.dumps(str(REPOSITORY))}\n', encoding="utf-8")
    result = subprocess.run([sys.executable, str(REPOSITORY / "scripts" / "mcp_smoke_test.py"),
                             "--codex-config", str(configuration)], cwd=REPOSITORY,
                            capture_output=True, text=True, timeout=90, shell=False)
    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads(result.stdout)["status"] == "PASS"
