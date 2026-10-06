"""Local SFX catalog metadata and normalized, human-authored tags."""

from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def normalize_tags(tags: list[str]) -> list[str]:
    if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag.strip() for tag in tags):
        raise ValueError("Tags must be a list of nonempty strings")
    return list(dict.fromkeys(tag.strip().casefold() for tag in tags))


class SFXLibraryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1)
    file: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    description: str = ""

    @field_validator("tags")
    @classmethod
    def valid_tags(cls, value):
        return normalize_tags(value)

    @field_validator("file")
    @classmethod
    def relative_file(cls, value):
        windows = PureWindowsPath(value)
        posix = PurePosixPath(value.replace("\\", "/"))
        if windows.drive or windows.root or posix.is_absolute() or ".." in posix.parts or ":" in value or "\x00" in value:
            raise ValueError("Catalog files must be relative paths inside the SFX library")
        return posix.as_posix()


class SFXLibrary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    items: list[SFXLibraryEntry] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_items(self):
        for attribute in ("id", "file"):
            values = [getattr(item, attribute).casefold() for item in self.items]
            if len(values) != len(set(values)):
                raise ValueError(f"Duplicate SFX library {attribute}")
        return self
