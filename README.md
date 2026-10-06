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
`faster-whisper` provides local transcription. Whisper model weights load only
on explicit subtitle generation, with a small configurable multilingual default.

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

Video and dialogue clips use `id`, `source`, `source_in`,
`source_out`, `timeline_start`, `enabled`, `volume`, and `speed`. Trim intervals
are half-open; duration is `(source_out - source_in) / speed`. Volume is a
nonnegative linear gain (1 is unchanged); speed must be finite and positive.
Disabled clips retain their intent but do not participate in overlap checks.
All source files, including those on disabled clips, must exist. Relative
sources resolve against the project directory during load and save.
Music and SFX use decibel gain and their own fields documented below.

Video clips may overlap only at an explicitly defined transition boundary.
Dialogue and music clips cannot overlap within a track. Overlaps across
tracks and within SFX tracks are allowed. Subtitle tracks reference a UTF-8 SRT
`file` or contain inline cues, with `enabled` and `burn_in` flags. Inline cues
contain text and timeline start/end times; enabled cues cannot overlap within a track.
Touching intervals are allowed, with a 1e-9-second comparison tolerance.

Video transitions contain an ID, `from_clip`, `to_clip`, `type` (`cut`,
`crossfade`, or `fade_to_black`), and `duration`. Cuts use duration 0; blends
require positive duration and matching overlap between adjacent enabled clips.
Duration cannot exceed either clip, and a clip's incoming/outgoing overlaps
cannot together exceed its duration. Duplicate boundary transitions are rejected.
The transition tool and rendering behavior are documented below.

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
placed contiguously from time zero, allowing declared transition overlaps.
Gaps, overlapping video tracks, speed changes, and active separate dialogue audio tracks return explicit
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
`set_transition(project, clip_id, transition_type, duration)` edits the boundary
after an enabled video clip, with automatic timeline history.
Five non-destructive video editing tools are also available: `add_clip`, `remove_clip`,
`trim_clip`, `move_clip`, and `set_clip_speed`, plus three manual music tools:
`add_music`, `remove_music`, and `update_music`. Five SFX tools are available:
`add_sfx`, `remove_sfx`, `update_sfx`, `list_sfx_library`, and `search_sfx_by_tags`.
`generate_subtitles(project, language)` generates English/Vietnamese captions.
`render_preview(project, burn_subtitles=None)` optionally burns one enabled
subtitle track. Omitted burn_subtitles honors the timeline's burn_in flags;
False omits burn-in, and True uses the enabled track.

Run the standalone official-SDK client test from the repository root:

```powershell
.\.venv\Scripts\python.exe .\scripts\mcp_smoke_test.py
```

It launches a real server process, performs initialization and tool discovery,
calls all twenty-two tools, tests recovery after project and argument errors, and
renders synthetic media through MCP. Its temporary project storage is isolated
using `FILMCUT_PROJECTS_ROOT` and `FILMCUT_SFX_LIBRARY`; real projects and the
local asset catalog are untouched. On Linux use
`.venv/bin/python scripts/mcp_smoke_test.py`.
The regular smoke test checks subtitle error recovery without downloading or
loading a model. Real transcription has a separate opt-in smoke test below.

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
  referenced by a blending transition cannot be removed through this API.
  Set the boundary to cut first; zero-duration cut records are cleaned on removal.
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
Manual music editing is described below; automatic music selection is not implemented.

## Simple video transitions

`set_transition(project, clip_id, transition_type, duration)` applies to the
boundary after clip_id and its next enabled clip on the same track. An enabled
successor is required; the tool rejects gaps, disabled clips, and terminal clips.
Only `cut`, `crossfade`, and `fade_to_black` are supported.

- `cut` switches directly to the next clip and requires duration 0.
- `crossfade` blends outgoing/incoming pictures over the supplied duration.
- `fade_to_black` fades the outgoing picture to black in the first half and
  fades the incoming picture from black in the second half of that overlap.

Both blends overlap video and original dialogue audio for exactly the same
interval. Audio uses linear crossfades, including while the picture fades
through black; there is no separate audio offset. J-cuts and L-cuts are not
implemented. Blends must last at least one output frame and fit both clips.

The tool stores a record in the video track's `transitions` list:

```json
{
  "id": "transition-<id>",
  "from_clip": "clip-a",
  "to_clip": "clip-b",
  "type": "crossfade",
  "duration": 0.5
}
```

It shifts subsequent enabled video clips by the change in overlap and stores
their actual timeline_start values. Changing a 0.5-second crossfade to a cut
restores those 0.5 seconds. Other tracks retain their absolute timestamps;
review SFX/music placements and regenerate captions after changing transitions.
The structured result includes the transition, shifted clip positions, previous
and new total video duration, summary, and exact prior-timeline backup. Failed
validation leaves the timeline/history unchanged.

For a contiguous track, total duration is **sum of clip durations minus sum of
transition overlaps**. Two 3-second clips with a 0.5-second blend render to
5.5 seconds. `Timeline.duration` and `VideoTrack.duration` use stored actual
clip endpoints, including cumulative overlaps. Blends must link adjacent clips;
arbitrary overlaps and overlapping incoming/outgoing blend windows are rejected.
Existing trim/move/speed edits must preserve any declared boundary constraints.

The renderer first normalizes each clip, then joins boundaries using small,
deterministic two-input FFmpeg graphs with synchronized xfade/acrossfade.
Cut-only timelines retain the normal concatenation path. Mixed BGM/SFX and
subtitle timing use the shortened rendered clock. Each intermediate and final
MP4 is verified; cache cleanup and preservation of prior previews on failure
remain in place. Frame quantization may alter duration by about one output frame.

Earlier intent-only records using `kind: "crossfade"` and touching clips migrate
to overlap positions in memory when loaded. Loading does not rewrite the file;
the next save stores canonical `type` fields and backs up the original JSON.
New records always require correctly placed overlaps. Flashy transitions are
not implemented.

## Manual music and BGM mixing

Music items contain `id`, `file`, `timeline_start`, `source_in`, `source_out`,
`volume_db`, `fade_in`, `fade_out`, `loop`, and `enabled`. Times are seconds;
gain is decibels in the range -120 to +60, default -18. Canonical JSON uses
`file` and `volume_db`. Earlier `source`/linear `volume` fields are accepted and
converted when saving; legacy music speed must be 1. Source files are never changed.

- `add_music(project, file, timeline_start=0, source_in=0, source_out=None,
  volume_db=-18, fade_in=0, fade_out=0, loop=False, enabled=True)` adds a manually
  supplied file. Omitted source_out defaults to its probed audio duration. The
  service uses the sole music track or creates one; multiple tracks require
  explicit timeline authoring because this tool has no track argument.
- `remove_music(project, music_id)` removes only its timeline entry.
- `update_music(project, music_id, ...)` accepts any of those optional music
  fields and changes only supplied values, including zero and False.

All three tools use the shared locked, validated transaction with timeline
backups and structured summaries. Unsupported/corrupt audio and out-of-range
trims return errors without changing the saved timeline. Music files can use any
audio format readable by the installed ffprobe/FFmpeg, including WAV and MP3.

A loop repeats the selected source interval until video end, starting at
timeline_start. Non-looping music ends after that interval. Both are cut at video
end. Fades apply once to the actual playback start/end, rather than resetting at
loop boundaries. Fades must fit non-loop playback; when the video truncates the
music, fade lengths are shortened proportionally to fit the remaining duration.
Enabled looping music occupies the remainder of the video for same-track
overlap validation. Music on separate tracks can mix concurrently.

The audio engine prepares floating-point PCM intermediates in the render's
isolated project cache directory. The original dialogue remains at unity gain;
there is no automatic ducking or amix normalization. A final peak limiter caps
the mix at 0.8 before AAC encoding, without automatic gain boost, reserving
headroom for codec overshoot. High mix levels may trigger dynamic attenuation;
the tests inspect decoded stereo samples and confirm no clipping for the stress
fixtures. Audio is stereo AAC at 48000 Hz; H.264 video is copied without another
video encode. Disabled or out-of-timeline music does not contribute to output.

Real render tests measure source and BGM frequencies, loop persistence, selected
source intervals, gains, fades, silence when disabled, timeline-end trims, and
decoded output peaks. No AI music selection is implemented.

## Manual SFX library and events

The local library lives in `assets/sfx/`: `library.json` is the active versioned
tag catalog, `audio/` holds user-supplied sounds, and `library.example.json`
shows `impact_concrete_03.wav` with `impact`, `body`, `wall`, `concrete`, and
`heavy` tags. The active catalog starts empty; the example does not include an
audio file. See [the library instructions](assets/sfx/README.md) for adding files.
Audio assets are ignored by Git and should be backed up separately. Set
`FILMCUT_SFX_LIBRARY` to an absolute directory to use a different local catalog.

Each event contains `id`, `file`, `timeline_time`, `source_in`, `volume_db`,
`fade_in`, `fade_out`, `enabled`, and `tags`. Times are seconds; gain defaults to
0 dB and accepts -120 through +60 dB. Tags are trimmed, case-insensitive, and
deduplicated. Events play from source_in to the file's end, without looping,
and are trimmed at video end. An optional `source_out` preserves older manually
trimmed SFX entries. Earlier `source`, `timeline_start`, and linear `volume`
fields migrate to canonical fields when saving; legacy speed must be 1.

- `add_sfx(project, file, timeline_time, source_in=0, volume_db=0,
  fade_in=0, fade_out=0, enabled=True, tags=None)` adds a manually supplied sound
  to the sole SFX track, creating that track if needed.
- `remove_sfx(project, sfx_id)` removes its event and preserves the sound file.
- `update_sfx(project, sfx_id, ...)` changes supplied event fields while retaining
  the ID. Zero, False, and an empty tag list are accepted updates.
- `list_sfx_library()` returns catalog entries with absolute paths, tags,
  availability, and missing-file diagnostics. Catalog paths must stay inside
  the library. Invalid catalogs return structured errors.
- `search_sfx_by_tags(tags, match_all=True)` returns available catalog entries
  matching all requested tags; set match_all=False for any-tag matching.

Every edit uses validated, locked timeline persistence with an exact backup of
the previous `timeline.json` and a concise change summary. Missing/corrupt
audio, invalid timestamps, source offsets, or fades leave the timeline unchanged.
Fades must fit the remaining source audio; when video end truncates the event,
fade lengths shrink proportionally to fit. Relative event files resolve against
the project directory; Windows absolute paths are supported on Windows.

SFX events may overlap on the same track or across tracks. They mix together
with dialogue and BGM through the shared floating-point audio engine and one
final limiter. Placement rounds to the nearest 48000 Hz audio sample rather than
a video frame or millisecond. AAC encoding can introduce transient smearing.
Disabled events and events at or beyond video end do not play. Rendering does
not modify source files or the saved timeline.

Real tests render three simultaneous tones with dialogue and looping BGM,
verify timestamps, trims, gain, fades, clipping protection, cache cleanup, and
H.264/AAC/48000 Hz output with ffprobe. No AI SFX detection is implemented.

## Local automatic subtitles

`generate_subtitles(project, language)` accepts `en`/`English` or
`vi`/`Vietnamese`. It renders the saved video's trims and original dialogue
volume before BGM/SFX mixing, extracts mono 16 kHz PCM in the project cache,
then runs faster-whisper locally with voice activity detection. Segment times
use the rendered timeline clock, not the original source clock. The current
contiguous, normal-speed, single-video-track render limits still apply.

The structured result includes `segments` with `start`, `end`, and `text`, the
model/language, `srt_path`, `transcript_path`, a timeline reference, and backup
path. UTF-8 SRT and readable transcript JSON are saved under
`projects/<project>/subtitles/` with unique revision filenames. No speech
produces an empty SRT and segment list. Generation preserves the preview and
source media and cleans its own temporary files on success or failure.

The generated timeline track uses a relative SRT reference:

```json
{
  "id": "generated-subtitles-<revision>",
  "language": "vi",
  "file": "subtitles/subtitles-vi-<revision>.srt",
  "enabled": true,
  "burn_in": false,
  "cues": []
}
```

Every SRT reference is validated on timeline load/save. Regenerating a language
updates its generated track and backs up the previous timeline; old subtitle
artifacts remain available to history. Results are rejected if timeline.json or
project.json changed during inference, so a long transcription cannot overwrite
new edits. Regenerate captions after changing video trims or placements.

Preview burn-in is optional: pass `burn_subtitles=True` to `render_preview`,
or set the desired track's `burn_in` to true in timeline.json. Only one enabled
subtitle track can burn at a time. FFmpeg must include the `subtitles` filter
(libass); default plain styling and a system font with Vietnamese glyphs are
used. Burn-in preserves the mixed AAC audio and installs the verified preview
atomically. Failure leaves the previous preview intact. Advanced styling is
not implemented.

Whisper settings are environment variables inherited by the MCP process:

| Variable | Default | Purpose |
| --- | --- | --- |
| `FILMCUT_WHISPER_MODEL` | `tiny` | Multilingual model name or local CTranslate2 model directory |
| `FILMCUT_WHISPER_DEVICE` | `cpu` | Inference device |
| `FILMCUT_WHISPER_COMPUTE_TYPE` | `int8` | Inference precision |
| `FILMCUT_WHISPER_THREADS` | `2` | CPU threads |
| `FILMCUT_WHISPER_CACHE` | `<repo>/.cache/whisper` | Model download/cache directory |
| `FILMCUT_WHISPER_LOCAL_ONLY` | `false` | Require already cached/local weights, with no download |

The multilingual tiny weights are roughly 75 MB and download from Hugging Face
only on the first explicit transcription request. Installation, MCP startup,
tool discovery, and regular tests do not download models. Cached/local weights
work offline. A blocked download returns `whisper_model_unavailable` with
configuration guidance. Do not use an English-only `.en` model for Vietnamese.
Tiny has limited accuracy; a larger multilingual model can be configured
explicitly when desired. Review automatic captions before export.

Run the **real transcription** smoke test with an existing clear speech file:

```powershell
& '.\.venv\Scripts\python.exe' '.\scripts\subtitle_smoke_test.py' '<absolute path to speech.wav>' --language vi
```

Use `--language en` for English and `--output-root <new-directory>` to retain
artifacts. This test launches the MCP server, generates subtitles using the
configured real model, checks nonempty segments and SRT, renders a captioned
preview, probes it, and fully decodes it. It may download tiny on first use;
ordinary pytest uses inference test doubles, with real FFmpeg extraction and
burn-in, to remain deterministic and network independent.
