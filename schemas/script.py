"""Story requirements describe intent, independent of source clips and rendering."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SceneRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
    story_order: int = Field(ge=1, strict=True)
    characters: list[str]
    location: str | None
    action: str | None
    emotion: str | None
    dialogue: str | None
    preferred_shot_size: str | None
    continuity_requirements: list[str]
    estimated_duration: float = Field(gt=0, allow_inf_nan=False)
    notes: list[str]


class ScriptBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = "1.0"
    requirements: list[SceneRequirement] = Field(min_length=1)

    @model_validator(mode="after")
    def ordered_unique_scenes(self):
        if self.version != "1.0":
            raise ValueError("Unsupported script breakdown version")
        if len({item.scene_id for item in self.requirements}) != len(self.requirements):
            raise ValueError("Scene IDs must be unique")
        if [item.story_order for item in self.requirements] != list(range(1, len(self.requirements) + 1)):
            raise ValueError("Requirements must be in consecutive story order starting at 1")
        return self
