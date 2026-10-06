# FilmCut

A Windows-first local video editing engine, intended for future control by
Codex through MCP. Project creation, media analysis, timeline intent, and single-track video preview
rendering are implemented, with a local FastMCP stdio server.

## Requirements

- Python 3.11+
- FFmpeg and ffprobe available on PATH
- Git

## Windows setup (PowerShell)

Run from the FilmCut repository root:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -c requirements-lock.txt
.\.venv\Scripts\python.exe -m pytest
```

Activation is optional; calling the virtual environment's Python directly avoids
PowerShell execution-policy changes. Check `py -3 --version` is 3.11 or newer.

For Linux cloud development:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -c requirements-lock.txt
.venv/bin/python -m pytest
```

## Architecture

- `engine/`: media, FFmpeg execution, timeline, audio, subtitles, and rendering.
- `schemas/`: Pydantic project and timeline models describing editing intent.
- `services/`: project orchestration.
- `mcp_server.py`: local FastMCP stdio server.
- `assets/music/` and `assets/sfx/`: local asset directories.
- `projects/`: local project data, including future `timeline.json` files.
- `tests/`: imports, project management, media analysis, timeline validation,
  real FFmpeg rendering, and MCP stdio integration tests.

Pydantic, the MCP Python SDK, and pytest are installed for development.
FFmpeg and ffprobe are external executables, not Python packages.
`faster-whisper` will be added during a later transcription phase.

Follow the permanent development rules in `AGENTS.md`.

For cloud-to-Windows transfer, follow `TRANSFER_WINDOWS.md`. Never copy a Linux
`.venv` to Windows; create it locally. Dependency constraints record the tested
cloud package versions; Windows-specific SDK dependencies are resolved by pip.
Native Windows validation remains required. Stop before MCP configuration until
the Windows source checkout and virtual environment have been verified.

## Project creation

```python
from services.project_service import create_project

result = create_project("My Film", r"C:\Videos\Source")
if result.success:
    print(result.project_path)
else:
    print(result.error.code, result.error.message)
```

Run this example on Windows with an existing source directory. On Linux, supply
a native Linux directory instead. Windows drive and UNC paths are supported by
native `pathlib` on Windows and rejected on Linux rather than interpreted as
relative paths. An optional `projects_root` keyword overrides the default
repository `projects/` directory, for example in temporary-directory tests.

Each project contains `project.json`, `source_index.json`, `timeline.json`, and
the directories `cache/`, `analysis/`, `subtitles/`, `preview/`, and `output/`.
Metadata defaults to 1920×1080, 24 fps, 48000 Hz, and project version `1.0`, with
a UTC creation timestamp and resolved source directory. The source index and
timeline start empty; creation does not scan, copy, or edit media.

Invalid names, unavailable sources, existing projects, and filesystem failures
return a `ProjectResult` with `success=False` and a structured error. Existing
projects are never overwritten. Failed creation attempts remove the newly
created project; if cleanup fails, the error reports the remaining directory.

## Media analysis

```python
from engine.media import MediaError, probe_media, scan_folder, write_source_index

try:
    metadata = probe_media(r"C:\Videos\Source\clip.mp4")
    scan = scan_folder(r"C:\Videos\Source")
    write_source_index(result.project_path, scan)
except MediaError as error:
    print(error.to_dict())
```

`probe_media` returns ID, absolute path, filename, duration in seconds, width,
height, fps, video codec, audio presence, audio codec, and audio sample rate.
IDs are stable for the same resolved path. Unknown numeric metadata is zero;
silent videos have an empty audio codec and sample rate zero. The first video
stream (excluding cover art) and first audio stream are selected.

`scan_folder` recursively discovers `.mp4`, `.mov`, `.mkv`, and `.webm` files,
matching extensions without case. It returns `sources` and per-file `errors`;
invalid videos do not prevent valid videos from being indexed. Folder-access
failures and missing ffprobe raise structured `MediaError` exceptions. Directory
symlinks are not followed. ffprobe runs without a shell, with a 30-second timeout,
and emits selected metadata only; Python never loads video content into memory.

`write_source_index(project_folder, scan_result)` atomically replaces only the
existing project's `source_index.json`, storing version, sources, and errors.
It leaves the timeline and source media unchanged. Tests generate three short
synthetic videos with FFmpeg and do not require real footage.

## Timeline format and persistence

`timeline.json` is the editing source of truth. All times are seconds; no fields
contain FFmpeg commands or filters. Version 1 stores the project name, fps,
width, height, and typed `video_tracks`, `audio_tracks`, `music_tracks`,
`sfx_tracks`, and `subtitle_tracks`. Each track has a unique ID and its own
clips (or subtitle cues). IDs are unique throughout the timeline.

Video, dialogue, music, and SFX clips use `id`, `source`, `source_in`,
`source_out`, `timeline_start`, `enabled`, `volume`, and `speed`. Trim intervals
are half-open; duration is `(source_out - source_in) / speed`. Volume is a
nonnegative linear gain (1 is unchanged); speed must be finite and positive.
Disabled clips retain their intent but do not participate in overlap checks.
All source files, including those on disabled clips, must exist. Relative
sources resolve against the project directory during load and save.

Video, dialogue, and music clips cannot overlap within a track. Overlaps across
tracks and within SFX tracks are allowed. Subtitle cues contain text and
timeline start/end times; enabled cues cannot overlap within a subtitle track.
Touching intervals are allowed, with a 1e-9-second comparison tolerance.

Video transitions contain an ID, `from_clip`, `to_clip`, `kind` (crossfade,
dissolve, or wipe), and positive duration. They describe a blend at the boundary
of two touching, enabled clips on the same track, without shifting clip placement.
Duration cannot exceed either clip. A clip's incoming and outgoing transition
durations cannot together exceed its duration; duplicate boundary transitions
are rejected. Transition rendering is reserved for a later phase.

```python
from engine.timeline import (
    create_empty_timeline, load_timeline, save_timeline, validate_timeline,
)

project_folder = result.project_path  # From a successful create_project call.
timeline = create_empty_timeline(project_folder)
validation = validate_timeline(timeline, base_dir=project_folder)
if validation.valid:
    save_timeline(project_folder, timeline)
timeline = load_timeline(project_folder)
```

`create_empty_timeline` returns a model without writing. It accepts a Project
model, project name in the default projects root, or project-directory Path.
Load/save accept the same forms; pass a directory Path for custom project roots.
Standalone validation returns structured issues and resolves relative sources
against `base_dir` (cwd by default). Load/save raise serializable `TimelineError`
exceptions on failure. Saving revalidates mutated models, checks project identity,
and atomically replaces only `timeline.json` with indented UTF-8 JSON.

New projects initialize this complete format. Earlier `{version, tracks}`
placeholders are rejected without modification; automatic migration is not
implemented. Windows drive and UNC paths are preserved in JSON and checked
using native pathlib on Windows. On Linux they report `unsupported_path`.

## Video preview rendering

```python
from engine.render import RenderError, render_timeline

try:
    preview_path = render_timeline(project_folder)
except RenderError as error:
    print(error.to_dict())
```

`render_timeline(project)` reads the saved timeline, normalizes each enabled
video clip, concatenates the intermediates, verifies the MP4 with ffprobe, and
atomically installs `preview/preview.mp4`. Project resolution and fps are used;
timeline settings must match. Output is H.264/yuv420p video and stereo AAC at
48000 Hz. Display aspect ratio is preserved with square pixels and black
letterboxing/pillarboxing. Silent clips receive a silent audio stream. Original
clip audio volume is honored. AAC padding is trimmed before concatenation.

This phase supports exactly one enabled video track with normal-speed clips
placed contiguously from time zero. Gaps, overlapping tracks, speed changes,
transitions, and active separate audio/music/SFX/subtitle tracks return explicit
errors. Odd project dimensions are rejected because this H.264/yuv420p output
requires even dimensions. Trim endpoints must be within the source duration.
Frame quantization can change duration by approximately one output frame.

`render_clip(project, clip)` and `render_video_track(project, track)` return
cached MP4 files for reuse; successful returned files are intentionally retained.
Temporary intermediates use isolated subdirectories in the project's cache and
are cleaned on success or failure. Only this run's files are removed; unrelated
cache files and prior previews are preserved on failure. A preview cannot replace
its own source media. Final exports remain separate and are not implemented.

`engine/ffmpeg.py` provides argument-vector execution, aspect normalization,
encoding settings, and concatenation of prepared files. Every subprocess uses
`shell=False`; FFmpeg runs noninteractively with a 300-second per-command timeout.
Failures expose stderr and exit status through structured errors.

## Local MCP server

The official MCP Python SDK's FastMCP exposes the initial tools: `ping`,
`create_project`, `get_project`, `analyze_folder`, `get_timeline`,
`create_timeline`, and `render_preview`. Project arguments are project names
within FilmCut's projects root. All tools return structured content with
`success`, `data`, and `error`; project errors do not terminate the stdio process.
SDK argument-validation failures are MCP tool errors and also leave it running.
Logging goes to stderr, while stdout carries only MCP protocol messages.

Tools delegate to existing project, media, timeline, and render services.
`analyze_folder` indexes the project's configured source folder and reports
per-file errors plus `complete=false` on a partial scan. `create_timeline` is
idempotent: it preserves an existing valid timeline and only initializes a
missing file. Invalid existing timelines return errors without being reset.
`render_preview` uses the saved timeline and retains the current render limits.
Five non-destructive editing tools are also available: `add_clip`, `remove_clip`,
`trim_clip`, `move_clip`, and `set_clip_speed`.

Run the standalone official-SDK client test from the repository root:

```powershell
.\.venv\Scripts\python.exe .\scripts\mcp_smoke_test.py
```

It launches a real server process, performs initialization and tool discovery,
calls all twelve tools, tests recovery after project and argument errors, and
renders synthetic media through MCP. Its temporary project storage is isolated
using `FILMCUT_PROJECTS_ROOT`; real projects are untouched. On Linux use
`.venv/bin/python scripts/mcp_smoke_test.py`.

To produce the exact Windows Codex configuration, run this on the Windows
machine from the FilmCut folder:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows_mcp_config.ps1
```

This prints a `[mcp_servers.filmcut]` TOML section with paths inspected using
`Resolve-Path`, including the local `.venv\Scripts\python.exe`, server script,
and project cwd. Missing files cause errors; no paths are guessed and no settings
are modified. The execution-policy option applies only to this process.
Merge the printed section into the Codex configuration file, preserving other
settings (normally `$env:USERPROFILE\.codex\config.toml`; use `$env:CODEX_HOME`
when configured). Then verify the actual configured launch with:

```powershell
codex mcp get filmcut --json
$FilmCutCodexConfig = if ($env:CODEX_HOME) {
    Join-Path $env:CODEX_HOME 'config.toml'
} else {
    Join-Path $env:USERPROFILE '.codex\config.toml'
}
.\.venv\Scripts\python.exe .\scripts\mcp_smoke_test.py --codex-config $FilmCutCodexConfig
```

The smoke test reads the actual `command`, `args`, `cwd`, and optional `env` from
the `filmcut` configuration and launches them. Only project storage is overridden
to keep the test isolated. A `codex mcp get` result alone verifies registration,
not a successful server connection. Restart the Codex session after configuration.

## Non-destructive timeline editing

All editing tools modify only `timeline.json`; they do not render or modify
source/output media. `position` is a timestamp in seconds, matching
`timeline_start`, not a clip index. Other clips keep their placements. Removal
may leave a gap; longer trims or slower playback may create a prohibited overlap,
in which case the edit is rejected without modifying the saved timeline.

- `add_clip(project, source, source_in, source_out, position)` adds a video clip
  with a generated ID. It uses the sole video track or creates one; multiple video
  tracks are ambiguous because this API has no track argument.
- `remove_clip(project, clip_id)` removes the matching video entry. A clip
  referenced by a transition cannot be removed through this API.
- `trim_clip(project, clip_id, source_in, source_out)` changes source trim times
  while preserving the timeline start, volume, and speed.
- `move_clip(project, clip_id, position)` changes the timeline start in seconds.
- `set_clip_speed(project, clip_id, speed)` records finite, positive playback
  speed, changing the computed duration while preserving other clip placements.

Add and trim operations probe sources and require trim endpoints within known
media duration. Every edit revalidates the entire timeline and saves atomically.
Results include a concise summary, clip ID/state, timeline path, previous-version
backup path, and revision identifier. Errors use the existing structured MCP
envelope; failed edits do not create committed versions.

Before every replacement, including direct `save_timeline` calls, the exact
previous timeline bytes are written into `timeline_history/<UTC-time>-<UUID>.json`
inside the project. Backup writes are flushed before installing the new timeline.
The schema's `version=1` remains the format version; timestamped snapshots provide
editing history. A backup failure prevents the edit. No backup is needed when
creating a previously missing timeline. No automatic history pruning is performed.

A project `.timeline.lock` serializes complete read/modify/save operations and
direct saves. Concurrent requests receive `timeline_busy` and can retry. A crash
may leave a stale lock; confirm no writer is running before removing it manually.
Snapshots can be inspected or supplied to the existing validated save API to
restore intent; no separate undo/restore MCP tool is added in this phase.

Preview rendering still requires one contiguous track at speed 1. Editing gaps
or speed changes is supported as intent; rendering those cases remains deferred.
Music editing is not implemented.
