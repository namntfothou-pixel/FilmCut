"""Validated project metadata and structured service results."""

import re
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


def validate_project_name(name: str) -> str:
    """Require a single directory name safe on both Windows and POSIX."""
    if not isinstance(name, str) or not name or len(name) > 100:
        raise ValueError("Project name must contain between 1 and 100 characters.")
    if name != name.strip() or name.endswith(".") or name in {".", ".."}:
        raise ValueError("Project name cannot have surrounding whitespace or trailing dots.")
    if re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', name):
        raise ValueError("Project name contains forbidden filename characters.")
    stem = name.split(".")[0].rstrip(" ").upper()
    if stem in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} or re.fullmatch(
        r"(?:COM|LPT)[1-9¹²³]", stem
    ):
        raise ValueError("Project name is reserved by Windows.")
    return name


class Resolution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: int = Field(default=1920, gt=0)
    height: int = Field(default=1080, gt=0)


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    source_folder: str = Field(min_length=1)
    created_time: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolution: Resolution = Field(default_factory=Resolution)
    fps: float = Field(default=24, gt=0, allow_inf_nan=False)
    audio_sample_rate: int = Field(default=48000, gt=0)
    project_version: str = "1.0"

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        return validate_project_name(value)


class ProjectError(BaseModel):
    code: str
    message: str


class ProjectResult(BaseModel):
    success: bool
    project: Project | None = None
    project_path: Path | None = None
    error: ProjectError | None = None
