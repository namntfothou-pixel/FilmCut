# FilmCut development rules

1. `timeline.json` is the source of truth.
2. Editing must be non-destructive.
3. AI makes editing decisions; FFmpeg executes them.
4. Never build one giant FFmpeg command directly from an AI response.
5. All paths must support Windows absolute paths.
6. Preview and final exports are separate.
7. Every feature must have error handling.
8. Run tests after every implementation phase.
9. Do not build a GUI yet.
10. Automatic rough cuts are allowed through explicit `build_rough_cut` or `auto_edit_project` requests, with validated sources, timeline history, an edit-decision report, and a preview.
11. Final exports require a passing saved QC report unless the user explicitly sets `force=true`.

Use Python 3.11+ and the repository-local `.venv`. Keep dependencies inside
that virtual environment. Keep source media unchanged and generated files out
of Git. Project creation, media analysis, and timeline intent models are
implemented, along with single-track video preview rendering and a FastMCP
stdio adapter with non-destructive video-clip editing and timeline history.
Manual music and SFX editing, a tagged local SFX catalog, and BGM/SFX mixing are
implemented. Local faster-whisper subtitle generation, UTF-8 SRT timeline
references, and optional plain preview burn-in are implemented. Cut, crossfade,
and fade-to-black transitions with synchronized A/V overlap are implemented.
J-cuts and L-cuts use independent source-audio fields, never visual transition
types. Offset dialogue replaces embedded audio and is mixed once per enabled
clip; preserve video frame timing. Do not add flashy transitions. Final export
is explicit and QC-gated. Whisper defaults to multilingual tiny on
CPU/int8, loads only on transcription requests, and supports a local model path.
Do not download large models by default or add advanced subtitle styling.
Sound Director may propose existing local audio assets and apply saved plans
only through explicit plan/apply tools. Mark inferred SFX timing approximate;
never claim precise event detection from untimed action prose.
MCP stdout is reserved for protocol messages; log to stderr.

Semantic source analysis lives in `analysis/`, `schemas/source_analysis.py`, and
`services/analysis_service.py`, separately from deterministic rendering. Keep
model providers abstract and injectable. Save observations under each project's
`analysis/` directory. Analysis and ranking never edit timelines; only the
explicit rough-cut and refinement apply services may turn their results into a timeline. Keep their
selection policy separate from the deterministic renderer. Rough cuts do not
add music or SFX; separate Sound Director apply operations may add them.
Refinement plans remain inspectable before application. Keep hard cuts as the
default, preserve manual edits, and never represent J/L cuts as visual effects.
Do not shift existing timed audio/subtitles silently when adding overlap.

High-level orchestration delegates to existing services. An explicit
`auto_edit_project` request authorizes the conservative default refinement and
Sound Director plan/apply steps; retain inspectable plans and stage snapshots.
Record stage failures and block dependent stages. Never silently invent semantic
analysis, skip missing sound assets/transcription, or export final automatically.
QC and final export are explicit user requests. `export_final` must render and
verify H.264/AAC before atomically publishing to `output/`; retain the QC report
and keep preview/export artifacts separate.
