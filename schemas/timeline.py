"""Editing intent in seconds; no FFmpeg commands or filter syntax."""

import math
from typing import Annotated, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from schemas.project import validate_project_name
from schemas.sfx import normalize_tags

Timestamp = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Identifier = Annotated[str, Field(min_length=1)]


class IntentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AudioClip(IntentModel):
    id: Identifier
    source: Identifier
    source_in: Timestamp = 0
    source_out: Positive
    timeline_start: Timestamp = 0
    enabled: bool = True
    volume: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 1
    speed: Positive = 1

    @model_validator(mode="after")
    def valid_interval(self):
        if self.source_out <= self.source_in:
            raise ValueError("source_out must be greater than source_in")
        if not self.source.strip():
            raise ValueError("source must be a nonempty path")
        if not math.isfinite(self.timeline_end):
            raise ValueError("computed timeline end must be finite")
        return self

    @property
    def duration(self) -> float:
        return (self.source_out - self.source_in) / self.speed

    @property
    def timeline_end(self) -> float:
        return self.timeline_start + self.duration


class VideoClip(AudioClip):
    """Video trim, placement, playback rate, and original audio volume."""

    audio_source_in: Timestamp | None = None
    audio_source_out: Positive | None = None
    audio_timeline_start: Timestamp | None = None

    @property
    def has_audio_offset(self):
        return any(value is not None for value in (self.audio_source_in, self.audio_source_out, self.audio_timeline_start))

    @property
    def resolved_audio_source_in(self):
        return self.source_in if self.audio_source_in is None else self.audio_source_in

    @property
    def resolved_audio_source_out(self):
        return self.source_out if self.audio_source_out is None else self.audio_source_out

    @property
    def resolved_audio_timeline_start(self):
        return self.timeline_start if self.audio_timeline_start is None else self.audio_timeline_start

    @property
    def audio_duration(self):
        return (self.resolved_audio_source_out - self.resolved_audio_source_in) / self.speed

    @property
    def audio_timeline_end(self):
        return self.resolved_audio_timeline_start + self.audio_duration

    @model_validator(mode="after")
    def valid_audio_interval(self):
        if self.resolved_audio_source_out <= self.resolved_audio_source_in:
            raise ValueError("audio_source_out must exceed audio_source_in")
        if not math.isfinite(self.audio_timeline_end):
            raise ValueError("Computed source-audio end must be finite")
        return self


class MusicClip(IntentModel):
    """Manual music intent; a loop repeats the selected source interval."""

    id: Identifier
    file: Identifier = Field(validation_alias=AliasChoices("file", "source"))
    timeline_start: Timestamp = 0
    source_in: Timestamp = 0
    source_out: Positive
    volume_db: Annotated[float, Field(ge=-120, le=60, allow_inf_nan=False)] = -18
    fade_in: Timestamp = 0
    fade_out: Timestamp = 0
    loop: bool = False
    enabled: bool = True

    @model_validator(mode="before")
    @classmethod
    def legacy_fields(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            if "volume" in value:
                gain = value.pop("volume")
                if "volume_db" in value or not isinstance(gain, (int, float)) or not math.isfinite(gain) or gain < 0:
                    raise ValueError("Invalid or conflicting legacy music volume")
                value["volume_db"] = 20 * math.log10(gain) if gain else -120
            if "speed" in value and value.pop("speed") != 1:
                raise ValueError("Music speed changes are not supported")
        return value

    @model_validator(mode="after")
    def valid_interval(self):
        if self.source_out <= self.source_in or not self.file.strip():
            raise ValueError("Music requires a nonempty file and source_out > source_in")
        if not math.isfinite(self.timeline_end):
            raise ValueError("Music timeline end must be finite")
        if not self.loop and self.fade_in + self.fade_out > self.duration:
            raise ValueError("Music fades exceed its playback duration")
        return self

    @property
    def source(self):
        return self.file

    @property
    def duration(self):
        return self.source_out - self.source_in

    @property
    def timeline_end(self):
        return self.timeline_start + self.duration


class SFXClip(IntentModel):
    """An overlapping, timestamped SFX event; never a rendering instruction."""

    id: Identifier
    file: Identifier = Field(validation_alias=AliasChoices("file", "source"))
    timeline_time: Timestamp = Field(default=0, validation_alias=AliasChoices("timeline_time", "timeline_start"))
    source_in: Timestamp = 0
    source_out: Positive | None = None  # Preserve earlier explicitly trimmed SFX.
    volume_db: Annotated[float, Field(ge=-120, le=60, allow_inf_nan=False)] = 0
    fade_in: Timestamp = 0
    fade_out: Timestamp = 0
    enabled: bool = True
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def legacy_fields(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            if "volume" in value:
                gain = value.pop("volume")
                if "volume_db" in value or not isinstance(gain, (int, float)) or not math.isfinite(gain) or gain < 0:
                    raise ValueError("Invalid or conflicting legacy SFX volume")
                value["volume_db"] = 20 * math.log10(gain) if gain else -120
            if "speed" in value and value.pop("speed") != 1:
                raise ValueError("SFX speed changes are not supported")
        return value

    @field_validator("tags")
    @classmethod
    def valid_tags(cls, value):
        return normalize_tags(value)

    @model_validator(mode="after")
    def valid_source(self):
        if not self.file.strip():
            raise ValueError("SFX requires a nonempty file")
        if self.source_out is not None and self.source_out <= self.source_in:
            raise ValueError("SFX source_out must exceed source_in")
        return self

    @property
    def source(self):
        return self.file

    @property
    def timeline_start(self):
        return self.timeline_time


class Transition(IntentModel):
    id: Identifier
    from_clip: Identifier
    to_clip: Identifier
    type: Literal["cut", "crossfade", "fade_to_black"] = Field(
        default="crossfade", validation_alias=AliasChoices("type", "kind"))
    duration: Timestamp

    @model_validator(mode="after")
    def valid_duration(self):
        if (self.type == "cut" and self.duration != 0) or (self.type != "cut" and self.duration <= 0):
            raise ValueError("Cuts require duration 0; crossfade/fade_to_black require a positive duration")
        return self

    @property
    def overlap(self):
        return self.duration


class VideoTrack(IntentModel):
    id: Identifier
    clips: list[VideoClip] = Field(default_factory=list)
    transitions: list[Transition] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_boundaries(cls, value):
        # The earlier intent-only schema serialized `kind` with touching clips.
        # Convert that exact legacy representation to actual overlap positions;
        # new `type` records always require explicit, correct placement.
        if not isinstance(value, dict) or not value.get("transitions") or not all(
                isinstance(item, dict) and "kind" in item and "type" not in item for item in value["transitions"]):
            return value
        value = dict(value)
        clips = [VideoClip.model_validate(item).model_copy(deep=True) for item in value.get("clips", [])]
        transitions = [Transition.model_validate(item) for item in value["transitions"]]
        active = sorted((clip for clip in clips if clip.enabled), key=lambda clip: clip.timeline_start)
        boundaries = {(item.from_clip, item.to_clip): item for item in transitions}
        original_starts = {clip.id: clip.timeline_start for clip in active}
        overlap = 0
        for index, clip in enumerate(active):
            if index:
                left = active[index - 1]
                transition = boundaries.get((left.id, clip.id))
                if transition:
                    if not math.isclose(original_starts[left.id] + left.duration, original_starts[clip.id], abs_tol=1e-9, rel_tol=0):
                        raise ValueError("Legacy transition clips must touch before migration")
                    overlap += transition.overlap
            clip.timeline_start -= overlap
        value["clips"], value["transitions"] = clips, transitions
        return value

    @property
    def duration(self):
        return max((clip.timeline_end for clip in self.clips if clip.enabled), default=0)

    @model_validator(mode="after")
    def valid_transitions(self):
        active = sorted((clip for clip in self.clips if clip.enabled), key=lambda clip: clip.timeline_start)
        adjacent = {(left.id, right.id): (left, right) for left, right in zip(active, active[1:])}
        boundaries, usage = {}, {}
        for transition in self.transitions:
            boundary = (transition.from_clip, transition.to_clip)
            if boundary not in adjacent:
                raise ValueError("transition must reference adjacent enabled clips in timeline order")
            if boundary in boundaries:
                raise ValueError("multiple transitions at the same boundary")
            boundaries[boundary] = transition
            left, right = adjacent[boundary]
            if transition.duration > min(left.duration, right.duration):
                raise ValueError("transition duration exceeds a participating clip")
            if not math.isclose(right.timeline_start, left.timeline_end - transition.overlap, rel_tol=0, abs_tol=1e-9):
                raise ValueError("transition placement must match its overlap duration")
            for clip in (left, right):
                usage[clip.id] = usage.get(clip.id, 0) + transition.overlap
                if usage[clip.id] > clip.duration + 1e-9:
                    raise ValueError("transitions consume overlapping portions of a clip")
        for left, right in zip(active, active[1:]):
            if right.timeline_start < left.timeline_end - 1e-9 and (left.id, right.id) not in boundaries:
                raise ValueError(f"prohibited overlap: {left.id} and {right.id}")
        return self


class AudioTrack(IntentModel):
    id: Identifier
    clips: list[AudioClip] = Field(default_factory=list)


class MusicTrack(IntentModel):
    id: Identifier
    clips: list[MusicClip] = Field(default_factory=list)


class SFXTrack(IntentModel):
    id: Identifier
    clips: list[SFXClip] = Field(default_factory=list)


class SubtitleCue(IntentModel):
    id: Identifier
    text: Annotated[str, Field(min_length=1)]
    timeline_start: Timestamp
    timeline_end: Positive
    enabled: bool = True

    @model_validator(mode="after")
    def valid_interval(self):
        if self.timeline_end <= self.timeline_start:
            raise ValueError("subtitle end must be greater than start")
        return self


class SubtitleTrack(IntentModel):
    id: Identifier
    language: str = "und"
    file: Identifier | None = None
    enabled: bool = True
    burn_in: bool = False
    cues: list[SubtitleCue] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_content(self):
        if self.file is not None and (not self.file.strip() or self.cues):
            raise ValueError("Subtitle track must use a nonempty SRT file or inline cues, not both")
        return self


class Timeline(IntentModel):
    project: Identifier
    version: Literal[1] = 1
    fps: Positive = 24
    width: Annotated[int, Field(gt=0, strict=True)] = 1920
    height: Annotated[int, Field(gt=0, strict=True)] = 1080
    video_tracks: list[VideoTrack] = Field(default_factory=list)
    audio_tracks: list[AudioTrack] = Field(default_factory=list)
    music_tracks: list[MusicTrack] = Field(default_factory=list)
    sfx_tracks: list[SFXTrack] = Field(default_factory=list)
    subtitle_tracks: list[SubtitleTrack] = Field(default_factory=list)

    @property
    def duration(self):
        return max((track.duration for track in self.video_tracks), default=0)

    @model_validator(mode="after")
    def valid_structure(self):
        validate_project_name(self.project)
        seen = set()

        def unique(identifier):
            if identifier in seen:
                raise ValueError(f"duplicate timeline identifier: {identifier}")
            seen.add(identifier)

        video_end = self.duration

        def no_overlaps(items):
            active = sorted((item for item in items if item.enabled), key=lambda item: item.timeline_start)
            for previous, current in zip(active, active[1:]):
                end = max(previous.timeline_start, video_end) if isinstance(previous, MusicClip) and previous.loop else previous.timeline_end
                if current.timeline_start < end - 1e-9:
                    raise ValueError(f"prohibited overlap: {previous.id} and {current.id}")

        for track in [*self.video_tracks, *self.audio_tracks, *self.music_tracks, *self.sfx_tracks, *self.subtitle_tracks]:
            unique(track.id)
            items = track.cues if isinstance(track, SubtitleTrack) else track.clips
            for item in items:
                unique(item.id)
            if not isinstance(track, (SFXTrack, VideoTrack)):
                no_overlaps(items)
            if isinstance(track, VideoTrack):
                for transition in track.transitions:
                    unique(transition.id)
        return self


class TimelineIssue(IntentModel):
    code: str
    location: list[str | int] = Field(default_factory=list)
    message: str


class TimelineValidation(IntentModel):
    valid: bool
    errors: list[TimelineIssue] = Field(default_factory=list)
