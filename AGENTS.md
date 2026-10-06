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
10. Do not implement AI source selection yet.

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
clip; preserve video frame timing. Do not add flashy transitions. Final exports are not
implemented. Whisper defaults to multilingual tiny on
CPU/int8, loads only on transcription requests, and supports a local model path.
Do not download large models by default or add advanced subtitle styling.
Do not implement automatic music selection or AI SFX detection.
MCP stdout is reserved for protocol messages; log to stderr.
