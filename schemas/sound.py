"""Auditable sound intent and selected existing assets; no generated audio."""

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator
from schemas.sfx import SFXLibrary, normalize_tags

Time = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Unit = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


class SoundModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Ducking(SoundModel):
    enabled: bool = False
    attenuation_db: float = Field(default=-6, ge=-60, le=0, allow_inf_nan=False)
    mode: Literal['cue_gain'] = 'cue_gain'


class MusicIntent(SoundModel):
    mood: str = Field(min_length=1)
    energy: Unit
    start: Time
    end: Time
    recommended_tags: list[str]
    ducking: Ducking = Field(default_factory=Ducking)
    fade_in: Time = 0.25
    fade_out: Time = 0.25

    @field_validator('recommended_tags')
    @classmethod
    def tags(cls, value):
        return normalize_tags(value)

    @model_validator(mode='after')
    def interval(self):
        if self.end <= self.start or self.fade_in + self.fade_out > self.end - self.start + 1e-9:
            raise ValueError('Music requires end > start and fades within the cue')
        return self


class SFXIntent(SoundModel):
    event: str = Field(min_length=1)
    timestamp: Time
    tags: list[str]
    intensity: Unit
    timing: Literal['exact', 'approximate'] = 'exact'

    @field_validator('tags')
    @classmethod
    def valid_tags(cls, value):
        result = normalize_tags(value)
        if not result:
            raise ValueError('SFX requires at least one tag')
        return result


class MusicDecision(MusicIntent):
    id: str
    file: str | None = None
    asset_id: str | None = None
    match_score: Unit = 0
    reason: str


class SFXDecision(SFXIntent):
    id: str
    file: str | None = None
    asset_id: str | None = None
    match_score: Unit = 0
    reason: str


class SoundPlan(SoundModel):
    version: Literal[1] = 1
    project: str
    timeline_fingerprint: str
    music: list[MusicDecision] = Field(default_factory=list)
    sfx: list[SFXDecision] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    music_warnings: list[str] = Field(default_factory=list)
    sfx_warnings: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def ids(self):
        values = [item.id for item in [*self.music, *self.sfx]]
        if len(set(values)) != len(values):
            raise ValueError('Sound decision IDs must be unique')
        ordered = sorted(self.music, key=lambda cue: cue.start)
        if any(right.start < left.end - 1e-9 for left, right in zip(ordered, ordered[1:])):
            raise ValueError('Planned music cues must not overlap')
        return self


# Both catalogs use the same validated relative files, tags, and uniqueness rules.
MusicLibrary = SFXLibrary
