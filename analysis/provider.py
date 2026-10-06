"""Implement this interface to integrate a local or remote multimodal model."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from schemas.source_analysis import SourceAnalysis


@dataclass(frozen=True)
class SourceContext:
    source_id: str
    path: Path
    duration: float
    width: int
    height: int
    fps: float


class AnalysisProvider(Protocol):
    def analyze(self, source: SourceContext) -> SourceAnalysis | dict:
        """Return observations conforming to SourceAnalysis.model_json_schema().

        Adapters own frame/audio sampling, model configuration, timeouts and
        credentials. Never modify the source. No model is selected or loaded
        by FilmCut's analysis service. Unknown observations must remain null.
        """
        ...


@dataclass(frozen=True)
class SuppliedAnalysisProvider:
    """Accept observations supplied by a caller, including a future MCP client."""

    analysis: dict

    def analyze(self, source: SourceContext) -> dict:
        return self.analysis
