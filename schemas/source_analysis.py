"""Provider-neutral semantic observations; never rendering instructions."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, model_validator

SourceId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")]
Seconds = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class SourceAnalysis(BaseModel):
    """Source-relative seconds; null means unknown, not an invented observation.

    Lists can be empty. Both usable bounds are null when no usable interval is
    established. Free-text observations support any language and provider.
    """

    model_config = ConfigDict(extra="forbid")

    source_id: SourceId
    characters: list[str]
    location: str | None
    shot_size: str | None
    camera_angle: str | None
    camera_motion: str | None
    action: str | None
    emotion: str | None
    dialogue: str | None
    visual_quality: str | None
    continuity_notes: list[str]
    usable_start: Seconds | None
    usable_end: Seconds | None
    problems: list[str]
    description: str | None

    @model_validator(mode="after")
    def usable_interval(self):
        if (self.usable_start is None) != (self.usable_end is None):
            raise ValueError("Provide both usable bounds or set both to null")
        if self.usable_start is not None and self.usable_end <= self.usable_start:
            raise ValueError("usable_end must exceed usable_start")
        return self
