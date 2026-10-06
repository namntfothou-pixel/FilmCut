"""Verify that the scaffold and its required dependencies can be imported."""

import importlib

import pytest


@pytest.mark.parametrize(
    "module_name",
    [
        "pydantic",
        "mcp.server.fastmcp",
        "mcp_server",
        "engine.media",
        "engine.ffmpeg",
        "engine.timeline",
        "engine.audio",
        "engine.subtitle",
        "engine.render",
        "schemas.project",
        "schemas.timeline",
        "services.project_service",
    ],
)
def test_import(module_name):
    importlib.import_module(module_name)
