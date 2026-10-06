"""Inspectable boundary recommendations; J/L cuts remain audio operations."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class EditRecommendation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    from_clip: str = Field(min_length=1)
    to_clip: str = Field(min_length=1)
    type: Literal['hard_cut', 'crossfade', 'j_cut', 'l_cut', 'fade_to_black']
    duration: float = Field(ge=0, allow_inf_nan=False)
    enabled: bool = True
    reason: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def valid_duration(self):
        if self.from_clip == self.to_clip:
            raise ValueError('Boundary must join different clips')
        if (self.type == 'hard_cut' and self.duration != 0) or (self.type != 'hard_cut' and self.duration <= 0):
            raise ValueError('Hard cuts require duration 0; other recommendations require positive duration')
        return self


class EditRefinementPlan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: Literal[1] = 1
    project: str
    timeline_fingerprint: str
    decisions: list[EditRecommendation]
    before_duration: float = Field(ge=0, allow_inf_nan=False)
    after_duration: float = Field(ge=0, allow_inf_nan=False)
    warnings: list[str] = Field(default_factory=list)
    policy: str

    @model_validator(mode='after')
    def unique_boundaries(self):
        boundaries = [(d.from_clip, d.to_clip) for d in self.decisions]
        if len(set(boundaries)) != len(boundaries):
            raise ValueError('Only one recommendation per boundary')
        return self
