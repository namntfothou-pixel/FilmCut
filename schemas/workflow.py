"""Inspectable orchestration settings and stage reports, independent of rendering."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AutoEditSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    script_file: str | None = Field(default=None, min_length=1)
    language: Literal["en", "vi"] = "en"


class WorkflowStage(BaseModel):
    name: str
    status: Literal["pending", "running", "completed", "failed", "blocked"] = "pending"
    logs: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None
    result: dict[str, Any] | None = None


class WorkflowReport(BaseModel):
    run_id: str
    project: str
    status: Literal["running", "completed", "failed"] = "running"
    success: bool = False
    started_at: str
    finished_at: str | None = None
    failed_stage: str | None = None
    error: dict[str, Any] | None = None
    report_path: str
    preview_path: str | None = None
    stages: list[WorkflowStage]
