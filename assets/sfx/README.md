# Local SFX library

Place your own audio files in `audio/` (subdirectories are allowed). Edit
`library.json` to catalog them using a unique id, relative file, tags, and optional
description. See `library.example.json` for the `impact_concrete_03.wav` example
and its impact/body/wall/concrete/heavy tags. This example is not active and no
sound file with that name is bundled. The active catalog starts empty.

Catalog file paths stay inside this directory. Absolute paths, parent traversal,
and symlinks escaping the library are rejected. Tags are trimmed, case-insensitive,
and deduplicated. Missing catalog audio is reported as unavailable; unindexed
files are not automatically added or assigned tags.

`list_sfx_library()` reads the catalog; `search_sfx_by_tags(tags, match_all=True)`
searches it without AI. Search returns only available files, matching all tags by
default (or any tag with match_all=False). Add effects to a project using the
returned absolute file path. Timeline edits never change this catalog or audio.

Audio files are ignored by Git; catalog metadata and the directory placeholder
are tracked. Back up audio files separately when moving the library to another
machine. `FILMCUT_SFX_LIBRARY` can override the default library directory for
testing or a local external catalog.
