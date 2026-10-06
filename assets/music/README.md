# Local music library

Place your existing audio files here and register them in `library.json`.
The Sound Director never generates or downloads audio. Paths must be relative
and stay inside this directory. IDs and paths must be unique. Example entry:

```json
{"id":"tense_bed","file":"audio/tense_bed.wav","tags":["tense","suspense"],"description":"Quiet suspense bed"}
```

Tags drive matching. An empty catalog yields unresolved decisions, not invented
files. Use `FILMCUT_MUSIC_LIBRARY` to select a different local catalog directory.
