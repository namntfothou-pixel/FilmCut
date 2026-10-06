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
Video clips also have independent `audio_source_in`, `audio_source_out`, and
`audio_timeline_start` fields. Null values follow the corresponding video
fields; explicit values control source dialogue separately.
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
`set_j_cut`, `set_l_cut`, and `reset_audio_offset` independently edit source
audio timing without changing visual transitions or video placement.
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
calls all thirty-six tools, tests recovery after project and argument errors, and
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

Default dialogue follows the visual blend interval with linear audio
crossfades, including while the picture fades through black. Explicit source
audio timing overrides operate independently and bypass those implicit fades.
Blends must last at least one output frame and fit both clips.

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

## Independent dialogue timing: J-cuts and L-cuts

J/L edits are source-audio operations. They never add a visual transition type,
shift video trims, move video clips, or change video duration. VideoClip stores:

| Field | Null/default behavior | Explicit behavior |
| --- | --- | --- |
| `audio_source_in` | Follow source_in | Select the first audio source time |
| `audio_source_out` | Follow source_out | Select the last audio source time |
| `audio_timeline_start` | Follow timeline_start | Place the selected audio interval on the timeline |

All times are seconds; source_out must exceed source_in, timestamps must be
finite/nonnegative, and explicit audio trims must fit the source audio stream.
Overlapping source-audio events are allowed. Clip `volume` applies once to its
selected audio. Disabled clips contribute neither picture nor source audio.
The existing normal-speed render limit still applies.

- `set_j_cut(project, clip_id, duration)` leads an incoming clip's dialogue by
  duration seconds, selecting source_in - duration * speed and placing it at
  timeline_start - duration. It preserves the currently selected audio end.
  A preceding enabled clip, source pre-roll, and nonnegative timeline placement
  are required. Repeating the operation sets an absolute lead; it does not
  accumulate offsets.
- `set_l_cut(project, clip_id, duration)` sets outgoing dialogue's timeline end
  to video timeline_end + duration, retaining its current audio source/start.
  For synchronized/J-cut audio this selects source_out + duration * speed.
  A following enabled clip, source post-roll, and enough remaining picture time
  are required. Repeating it sets an absolute extension.
- `reset_audio_offset(project, clip_id)` sets all three fields to null, restoring
  dialogue that follows the video's trim/placement and default transition fades.

Starting from synchronized audio, J and L can combine on a middle clip while
preserving lip sync throughout the visible picture. For a clip using source
1–3 seconds at timeline 2–4 seconds, a 0.5-second J-cut records audio source
0.5–3 at timeline 1.5; adding a 0.5-second L-cut extends the audio source end
to 3.5 and its timeline end to 4.5. The mapping remains:

`audio timeline time = audio_timeline_start + (source time - audio_source_in) / speed`

Thus source time 1 still plays at timeline time 2 and source time 3 at timeline
time 4. A J-cut uses earlier source sound, and an L-cut uses later source sound;
neither repeats the visible trim to create an artificial overlap. Arbitrary
independent audio fields may also be authored directly in timeline.json.

All three tools use validated timeline transactions and exact prior-JSON
backups. Missing audio, insufficient handles, invalid offsets, or invalid
boundaries return structured errors without writing a timeline revision. A
visual transition change leaves explicit audio timestamps fixed; review those
placements or reset/reapply the audio edits when changing picture placement.
Regenerate subtitles after changing dialogue timing.

When any enabled clip has an explicit audio field, the renderer normalizes and
joins the picture with silent audio, then rebuilds dialogue from the original
source intervals. The final dialogue graph never includes the picture input's
embedded audio. Each enabled source clip contributes exactly one event; clips
without overrides retain their synchronized transition fade envelopes. Explicit
events receive no fades from visual transitions. Float PCM intermediates,
48000 Hz sample delays, a non-normalizing mix, and a final peak limiter preserve
levels and protect overlaps from clipping. BGM/SFX mix onto that dialogue once.
When adjacent clips refer to the same recording and map the same source samples
to the same output samples, shared J/L handles are deduplicated with sample-based
masks. Explicit audio edits own shared regions ahead of default audio; stable
clip order resolves overlap between two explicit edits. Normal complementary
crossfade envelopes between unedited clips remain intact. Distinct sources or
different source-time mappings continue to mix normally.

Placement rounds to the nearest audio sample, independently of the video frame
clock. Audio is bounded by the final picture duration, and isolated render_clip
projects a clip's global audio event into its local picture window. Subtitle
transcription uses the same independently rendered dialogue, without BGM/SFX.
Source trims use resampled PCM sample counts, avoiding packet timestamp/seek
rounding that could shift source audio or create phase errors in overlaps.

Real tests use different tones before, during, and after each source's picture
trim. They verify the source-to-timeline equations, audible overlap windows,
unchanged dialogue gain, no default-dialogue duplication, sample-delay rounding,
peak limiting, unchanged hashes for every rendered video frame, and ffprobe
format/duration. Cache cleanup and prior-preview preservation are tested too.

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

## Phase 2: semantic source analysis

Semantic observations are separate from ffprobe's technical source index and
from the deterministic FFmpeg renderer. This phase provides a validated data
contract, provider interface, persistence service, and two MCP tools. It does
not select footage or automatically edit a movie. No model SDK, credentials,
network calls, or model downloads are required.

1. Run `analyze_folder(project)` to populate `source_index.json`.
2. Use its source IDs with `analyze_source(project, source_id, analysis=None)`.
   Supply an observation dictionary in `analysis`, or inject an implementation
   of `AnalysisProvider` into `build_server(analysis_provider=...)`.
   Without either, the tool returns `analysis_provider_not_configured`.
3. Read the saved result with `get_source_analysis(project, source_id)`.

Each successful analysis writes readable UTF-8 JSON to
`projects/<project>/analysis/<source_id>.json`. Reanalysis atomically replaces
that source's previous observations only after validation. Provider, validation,
and write failures preserve the previous file. Reading saved observations does
not invoke a model or require the source media to remain online; it does require
the project's source index. Analysis never modifies source media, the index,
`timeline.json`, or rendered output.

`SourceAnalysis` contains all of these required keys:

| Fields | Meaning/type |
| --- | --- |
| `source_id` | Exact ID from `source_index.json` |
| `characters` | List of observed character labels |
| `location`, `shot_size`, `camera_angle`, `camera_motion` | Descriptive text or `null` |
| `action`, `emotion`, `dialogue`, `visual_quality` | Descriptive text or `null`; dialogue is observed text, not a generated script |
| `continuity_notes`, `problems` | Lists of textual observations |
| `usable_start`, `usable_end` | Source-relative seconds within the indexed duration, or both `null` when no usable range is established |
| `description` | Overall description or `null` |

Unknown observations should remain `null`; empty lists indicate no reported
items. Text can be English, Vietnamese, or another language. Usability is
advisory metadata, never an automatic trim. The schema rejects unknown fields,
invalid intervals, non-finite timestamps, and unsafe source IDs. The service
also rejects mismatched IDs and intervals exceeding indexed duration. Rerun
`analyze_folder` and analysis if the underlying footage changes.

A future local or remote multimodal adapter implements this interface:

```python
from analysis.provider import SourceContext
from schemas.source_analysis import SourceAnalysis
from services.analysis_service import analyze_source

class MyMultimodalProvider:
    def analyze(self, source: SourceContext) -> SourceAnalysis | dict:
        # source carries an absolute native Path, ID, duration, dimensions, fps.
        # Your adapter handles sampling, model invocation, credentials, timeouts.
        # Ask for the contract exposed by SourceAnalysis.model_json_schema().
        # Return observations; do not change the source or timeline.
        raise NotImplementedError("Connect your chosen model here")

# Once the adapter is implemented:
# analyze_source(project_path, source_id, provider=MyMultimodalProvider())
```

MCP callers can instead supply observations from their own multimodal workflow;
`SuppliedAnalysisProvider` routes them through identical service validation.
The architecture does not claim any real model has analyzed footage yet.
Mocked tests cover the contract, Unicode persistence, unavailable providers,
invalid model responses, source lookups, atomic write failures, unchanged
project/media files, and MCP error recovery. The stdio smoke test also saves
and retrieves supplied mock observations.

## Script breakdown

`analyze_script(project, script, breakdown=None)` accepts script **text**, then
saves `projects/<project>/analysis/script_breakdown.json` as readable UTF-8 JSON.
`get_script_breakdown(project)` reads and validates it. Both MCP tools use the
script service; they never create or change a timeline, source index, or media.
A successful rerun atomically replaces the prior breakdown. Invalid input,
provider failures, and failed saves preserve the prior file.

The document contains `version: "1.0"` and an ordered `requirements` list.
Each `SceneRequirement` has `scene_id`, `story_order`, `characters`, `location`,
`action`, `emotion`, `dialogue`, `preferred_shot_size`,
`continuity_requirements`, `estimated_duration`, and `notes`. Characters,
continuity requirements, and notes are string lists. Unknown textual fields are
null. Durations are positive finite seconds. Scene IDs must be unique and
story order must be consecutive, starting at 1.

The offline parser supports `INT.`/`EXT.` screenplay headings and numbered
`SCENE`, `SHOT`, or `CẢNH` headings. Each heading starts one requirement, in
script order, with a stable positional ID such as `scene_001`. Scene and shot
headings are flat: use one heading per intended requirement rather than nested
scene/shot headings. Speaker names can be uppercase cues on their own lines or
`NAME: dialogue`. Separate action after dialogue with a blank line. Explicit
English field labels are supported: `Characters` (comma-separated), `Location`,
`Action`, `Emotion`, `Dialogue`, `Shot size` / `Preferred shot size`,
`Continuity` / `Continuity requirements`, `Duration` / `Estimated duration`,
and `Notes`. Field values can contain Vietnamese or other Unicode text.

This parser extracts formatting and explicit text; it does not infer unspoken
emotions or identify characters from action prose. Untagged prose outside
speaker blocks becomes action. Parenthetical delivery directions and screenplay
transitions are retained as notes. Unheaded free-form prose returns a useful
format error. A duration label accepts seconds (e.g. `Duration: 12 seconds`);
otherwise a rough reading estimate is recorded with its formula in notes for
review. This estimate is advisory and does not schedule clips.

The runnable example is `tests/fixtures/short_drama.txt`, a two-scene drama with
English action and Vietnamese dialogue. It exercises explicit and estimated
duration, continuity, shot preference, and dialogue extraction.

For semantic interpretation of arbitrary scripts, implement
`analysis.script_provider.ScriptAnalysisProvider.analyze(script)` returning a
`ScriptBreakdown` or compatible dictionary. Pass it to
`build_server(script_provider=...)` or
`services.script_service.analyze_script(..., provider=...)`. Adapters own model
selection and timeouts. An MCP client can also supply a model-produced
`breakdown` dictionary alongside the original script, using the same validation
and persistence path. `ScriptBreakdown.model_json_schema()` exposes the contract.
No provider or model is loaded by default. Script input is limited to one
million characters; no file paths are implicitly opened as script text.

## Explainable source matching

After saving a script breakdown and source analyses, use:

- `find_candidates_for_scene(project, scene_id, limit=5)`
- `rank_sources_for_script(project, limit=5)`

These MCP tools and matching services read existing metadata and return ranked
videos. They do not invoke a model, decode video, write files, select footage,
or modify `timeline.json`. `limit` is 1–100 candidates **per scene**. The script
result contains a `scenes` mapping in story order. Each scene returns
`candidates` and `total_candidates`. Each candidate contains `source_id`, its
native absolute `path`, a score in [0, 1], and all component explanations.
Equal scores sort by source ID so repeated calls are deterministic. Short or
poor matches remain visible with low scores; they are not silently excluded.

The versioned policy `lexical-v1` uses this formula:

`score = sum(active weight × component score) / sum(active weights)`

| Component | Weight | Rule |
| --- | ---: | --- |
| Characters | 0.20 | Fraction of required names present, with exact Unicode-normalized, case-insensitive matching; extra characters are allowed |
| Action | 0.20 | Fraction of required content tokens found in source action |
| Emotion | 0.10 | Required-token coverage against source emotion |
| Location | 0.10 | Required-token coverage against source location |
| Shot size | 0.10 | Same size/alias = 1; neighboring size = 0.5; other sizes = 0 |
| Visual quality | 0.10 | Explicit keyword baseline, minus penalties for reported problems |
| Continuity | 0.10 | Average best source-note token coverage for each continuity requirement |
| Usable duration | 0.10 | `min((usable_end - usable_start) / estimated_duration, 1)` |

Missing scene preferences deactivate their components and their weights. Quality
and duration always participate. Missing source text or an unknown usable range
scores zero for that component. For example, with all preferences active, a
source matching everything except one of two required characters scores 0.90:
`0.20 × 0.5 + 0.80 × 1`. A half-length usable interval loses 0.05 more.
These values are suitability heuristics, **not confidence probabilities**.

Text rules retain Unicode accents, case-fold and split words, and remove the
small English stop-word list in `analysis/matching.py`. Each text component
exposes matched/missing tokens and its rule. A mismatch in explicit negation
markers (`no`, `not`, `never`, `without`, `không`, `chưa`, `chẳng`) forces that
text comparison to zero. This is a conservative lexical guard, not grammatical
or semantic understanding. It does not detect every contradiction, resolve
pronouns, translate, or recognize synonyms. Shared vocabulary can still match
opposite actions; review the evidence before editing.

Shot-size aliases and their size ordering are explicit in `SHOT_GROUPS` in
`analysis/matching.py` (extreme wide through extreme close-up). Unrecognized
labels require exact normalized matching. Continuity compares each requirement
to individual source notes; it does not establish continuity between selected
clips or scenes.

Quality uses the reported `visual_quality` text, without pixel measurements:
missing = 0; recognized negative words or negation = 0.25; recognized positive
words = 1; otherwise = 0.5. Negative evidence takes precedence. Subtract 0.1 per
distinct nonempty `problems` entry, capped at 0.5, and clamp at zero. Keyword
lists are explicit in the scorer, and the response reports recognized words,
baseline, and penalty. Mixed descriptions such as “sharp but blurry” therefore
receive the conservative negative baseline. Non-English or unfamiliar quality
terms may use the unrecognized baseline; normalize upstream observations if a
shared vocabulary is desired.

Every component reports its score, configured weight, active status, required
and observed evidence, explanation, and normalized contribution. The response
also includes the policy version, weights, formula, limitations, and tie rule.
Sources with missing/corrupt analyses, invalid metadata, or unavailable files
are skipped with structured per-source `warnings`. An empty index yields empty
candidate lists. Invalid script/index data, unknown scene IDs, and invalid
limits return structured errors. Reanalyze changed footage before matching;
ranking trusts the saved metadata and does not re-probe content.

## Auto Rough Cut

Call `build_rough_cut(project)` after analyzing the script, indexing footage,
and saving source analyses. This is an explicit edit operation: it replaces the
current timeline with a fresh video-only rough cut, keeps the exact prior
`timeline.json` in `timeline_history/`, and renders
`projects/<project>/preview/preview.mp4`. Existing music, SFX, subtitles, and
manual edits remain in the backup, not in the new rough cut. Original footage
is never changed. No model invocation or new FFmpeg implementation is involved.

Selection follows this deterministic policy:

1. Read all saved scenes in story order and all valid indexed source analyses.
   Probe candidate media again; exclude corrupt/unavailable sources, unknown
   usable ranges, and ranges exceeding the current media duration.
2. Score candidates using the existing eight-component matcher. If a scene has
   story preferences, require some positive evidence in at least one of its
   character/action/emotion/location/shot-size/continuity components. Quality
   and duration alone cannot qualify footage for such a scene. This is a
   permissive lexical eligibility rule, not proof of semantic suitability.
3. Prefer unused eligible sources, then descending match score, then visual
   quality as a tie-breaker, then source ID. Reuse occurs only when no unused
   eligible source remains for that scene. Canonical file paths identify reuse,
   so different IDs pointing to the same file do not bypass the rule.
4. Round usable bounds inward to project-frame boundaries. Start each selected
   clip at its usable start, trim to the remaining scene duration, and use more
   candidates if necessary. When all eligible sources have already been used,
   repeat the best candidate's usable range and explicitly report that reuse.
5. Round each estimated duration to the nearest output frame (half up, minimum
   one frame). Lay clips contiguously at speed 1 with original source audio and
   explicit zero-duration cut transitions. No music, SFX, subtitles, J/L cuts,
   or visual blend effects are added automatically.

No eligible source for any scene fails the whole build before changing the
project timeline. Builds exceeding 1,000 clips are rejected to bound accidental
repetition from unrealistic durations. Scene duration is fulfilled to the
reported frame-rounded target; repetition may therefore be visible and should
be reviewed before further editing.

The service validates and renders a staged saved timeline in a temporary
project under the real project's cache. Only after successful rendering does it
publish the new canonical timeline, decision report, and preview while holding
the project timeline lock. The existing renderer handles normalization and
output verification. Normal render failures preserve the prior timeline,
report, and preview. Publication errors restore the old timeline/report, and
return any rollback error explicitly. A history snapshot can remain after a
failed publication. This is not a crash-atomic multi-file filesystem transaction;
`timeline.json` remains authoritative.

The return value includes `timeline_path`, `preview_path`, `report_path`, and
`report`. The same report is saved as
`projects/<project>/analysis/rough_cut_report.json`. It contains scene order,
requested and rounded durations, each selected clip's source ID and interval,
placement, component scores, selection reason, reuse reason, skipped-source
warnings, the scoring/selection policies, preview metadata, and backup path.
A timeline SHA-256 ties the report to the exact saved edit; subsequent manual
edits make the report historical until a new rough cut is built.

Tests generate small colored videos with audio tones and exercise the full
script → analysis → ranking → selection → saved timeline → preview pipeline.
They verify duration/codecs with ffprobe, source hashes, story order, distinct
high-quality choices, required reuse, range validation, and failure recovery.

## Sound Director

Four MCP tools keep planning separate from timeline execution:

- `plan_music(project, intents=None)`
- `plan_sfx(project, intents=None)`
- `apply_music_plan(project)`
- `apply_sfx_plan(project)`

Planning reads the script breakdown, valid source analyses, current video
clips, and existing tagged local libraries. It writes a validated `SoundPlan`
to `projects/<project>/analysis/sound_plan.json` and returns it. Planning never
edits the timeline, creates audio, invokes a model, or downloads files.
Application reads the saved plan, revalidates its assets, and saves visible
`MusicClip`/`SFXClip` entries through the shared timeline lock, validation, and
exact-JSON history. Render afterward with `render_preview(project)`.

Music decisions contain `mood`, energy [0,1], `start`, `end`,
`recommended_tags`, `ducking`, `fade_in`, and `fade_out`. SFX decisions contain
`event`, `timestamp`, `tags`, and intensity [0,1]. Every decision also records an
ID, selected asset ID/file, tag-coverage score, and explanation. SFX decisions
state whether timing is `exact` or `approximate`.

The default planner uses small, explicit rules in
`analysis/sound_director.py`. Anxious/scared emotions suggest tense/suspense;
anger suggests intense action; sadness suggests reflective music; happiness or
relief suggests hopeful/warm; otherwise ambient/neutral. Energy describes
intent, while asset selection uses tags. Rough-cut report associations are used
only when they still match clip/source trim and placement. Otherwise scene
association is inferred through the existing lexical matcher and flagged for
review. This is an offline heuristic director, not a claim that a model has
watched or acoustically analyzed footage.

Default SFX rules recognize body impacts against walls, doors opening/closing,
footsteps, and breaking glass in English action descriptions. Untimed prose
cannot establish exactly when an event happens in a shot. Default cues therefore
use shot onset and report approximate timing. Supply exact intents from a user
or a future model/provider for synchronized work, for example:

```json
{
  "event": "body hits concrete wall",
  "timestamp": 12.42,
  "tags": ["body", "impact", "concrete", "heavy"],
  "intensity": 0.9,
  "timing": "exact"
}
```

Pass a list of these objects as `intents` to `plan_sfx`. Music intents use the
music fields above and optional `ducking` of
`{"enabled": true, "attenuation_db": -6, "mode": "cue_gain"}`.
`MusicIntent.model_json_schema()` and `SFXIntent.model_json_schema()` expose
provider-neutral contracts: any future model can return these intents, while
FilmCut still chooses and validates the local files itself. Supplied intents
replace that section of the plan and do not invent media paths. Timestamps must
lie inside the current video duration; music cues must not overlap.

Music uses `assets/music/library.json`, with the same version-1 entry format as
the SFX catalog: `id`, relative `file`, `tags`, optional `description`. Existing
SFX assets use `assets/sfx/library.json`. Catalog paths stay inside their
library directory. Assets must exist and contain a valid audio stream.
Matching scores required-tag coverage, accepts a positive partial match, and
breaks ties by asset ID; explanations show matched tags. Missing/broken files
are reported. No matching file produces an unresolved decision. Apply rejects
unresolved or unavailable decisions before modifying any timeline.
The shipped catalogs are empty: register your existing files and tags before
using them. Override roots with `FILMCUT_MUSIC_LIBRARY` and
`FILMCUT_SFX_LIBRARY`, or inject roots into `build_server` for tests.

Ducking is explicit **cue-wide gain attenuation**, not a dynamic sidechain or
word-level dialogue detector. Dialogue-bearing or conservatively audio-bearing
clips enable it by default. Application uses a -18 dB music base plus the
configured attenuation (default -6 dB), recorded as `volume_db: -24` in the
music entry. Original source audio gain is preserved. Music loops within each
planned cue and fades at its endpoints. `MusicClip.end` is an optional absolute
timeline endpoint; legacy music with `end: null` keeps its prior behavior.
The deterministic audio engine only gained this bounded playback endpoint.

SFX intensity maps to `-30 + 24 × intensity` dB, with zero intensity muted at
-120 dB. Events are trimmed to video end and receive short click-prevention
fades. Their file, timestamp, trim, tags, gain, and enabled state are visible in
`timeline.json`. The existing mixer sums simultaneous events and limits peaks.
Automatic entries use the reserved `sound-director-` ID namespace. Application
replaces only that kind of director-owned entries; manual tracks and the other
sound kind survive. Reapplying an unchanged plan is idempotent and creates no
extra events or history entry. Explicit reapplication overwrites manual edits
to director-owned entries; use separate manual IDs for lasting overrides.

The plan fingerprints all non-director timeline content. Video or manual-audio
changes require replanning. Music and SFX can be planned together and applied
in either order, because their own inserted entries do not invalidate the
shared plan. Replanning after a changed timeline starts a fresh SoundPlan.
Each section retains its own current warnings. Applying never silently fills
an unresolved cue from an unrelated asset, and changed library tag matches
require replanning.

Tests use synthetic existing audio files and footage. They verify that planning
changes no timeline/audio files, both apply tools back up edits, repeated apply
is idempotent, manual music survives, stale/missing assets fail atomically,
and a literal 12.42-second SFX entry appears in the timeline. A real preview
test checks music endpoint timing and SFX placement through spectral tone
measurements, preserves dialogue level, and verifies H.264/AAC/48 kHz output
with ffprobe. The stdio smoke test exercises all four tools and renders the mix.
