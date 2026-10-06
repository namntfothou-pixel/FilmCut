"""Plan from metadata/catalogs; apply only reviewed saved sound decisions."""

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path

from analysis.matching import score_source
from analysis.sound_director import music_intent, sfx_intents
from engine.audio import probe_audio, validate_music_source, validate_sfx_source
from engine.media import probe_media
from engine.timeline import _project_context, load_timeline, timeline_lock, save_locked_timeline
from schemas.sound import SoundPlan, MusicIntent, SFXIntent, MusicDecision, SFXDecision
from schemas.timeline import MusicClip, MusicTrack, SFXClip, SFXTrack
from services.analysis_service import AnalysisError
from services.matching_service import _load
from services.music_library_service import list_music_library
from services.sfx_service import list_sfx_library

PREFIX = 'sound-director-'


def _fingerprint(timeline):
    data = timeline.model_dump(mode='json')
    for collection in ('music_tracks', 'sfx_tracks'):
        retained = []
        for track in data[collection]:
            track['clips'] = [c for c in track['clips'] if not c['id'].startswith(PREFIX)]
            if track['clips'] or not track['id'].startswith(PREFIX):
                retained.append(track)
        data[collection] = retained
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _read_plan(folder):
    try:
        return SoundPlan.model_validate_json((folder / 'analysis' / 'sound_plan.json').read_text(encoding='utf-8'))
    except FileNotFoundError as exc:
        raise AnalysisError('sound_plan_not_found', 'Plan music or SFX first') from exc
    except (OSError, ValueError) as exc:
        raise AnalysisError('invalid_sound_plan', str(exc)) from exc


def _catalog(kind, root):
    catalog = list_music_library(library_root=root) if kind == 'music' else list_sfx_library(library_root=root)
    assets, warnings = [], [error['message'] for error in catalog['errors']]
    for entry in catalog['items']:
        if not entry['available']:
            continue
        try:
            info = probe_audio(entry['file'])
            assets.append({**entry, 'duration': info['duration']})
        except Exception as exc:
            if getattr(exc, 'code', '') == 'ffprobe_missing':
                raise
            warnings.append(f"Skipped invalid audio {entry['id']}: {exc}")
    return assets, warnings


def _choose(tags, assets):
    scored = [(len(set(tags) & set(asset['tags'])) / len(set(tags)), asset) for asset in assets] if tags else []
    scored.sort(key=lambda pair: (-pair[0], pair[1]['id']))
    return scored[0] if scored and scored[0][0] > 0 else (0, None)


def _contexts(folder, timeline):
    if len(timeline.video_tracks) != 1 or timeline.duration <= 0:
        raise AnalysisError('invalid_sound_timeline', 'Sound Director requires one nonempty video track')
    breakdown, sources, skipped = _load(folder, 100)
    warnings = [f"{entry['source_id']}: {entry['message']}" for entry in skipped]
    by_path = {os.path.normcase(str(Path(path).resolve())): analysis for analysis, path in sources}
    associations = {}
    report_path = folder / 'analysis' / 'rough_cut_report.json'
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding='utf-8'))
            for decision in report['decisions']:
                for selection in decision['selections']:
                    associations[selection['clip_id']] = (decision['scene_id'], selection)
        except (OSError, ValueError, KeyError, TypeError):
            warnings.append('Rough-cut report cannot be read; scene mapping uses lexical matching.')
    scenes = {s.scene_id: s for s in breakdown.requirements}
    result = []
    for clip in sorted((c for c in timeline.video_tracks[0].clips if c.enabled), key=lambda c: c.timeline_start):
        path = Path(clip.source)
        path = path if path.is_absolute() else folder / path
        analysis = by_path.get(os.path.normcase(str(path.resolve())))
        if analysis is None:
            warnings.append(f'No valid source analysis for {clip.id}; no automatic cues for this clip.')
            continue
        mapped = associations.get(clip.id)
        scene = None
        if mapped and mapped[0] in scenes and mapped[1].get('source_id') == analysis.source_id and all(
            math.isclose(mapped[1].get(key, -1), getattr(clip, key), abs_tol=1e-9)
            for key in ('source_in', 'source_out', 'timeline_start')):
            scene = scenes[mapped[0]]
        if scene is None:
            scene = max(breakdown.requirements, key=lambda s: score_source(s, analysis)['score'])
            warnings.append(f'{clip.id}: scene mapping inferred lexically as {scene.scene_id}; review intent.')
        result.append((scene, analysis, clip, bool(probe_media(path)['has_audio'])))
    return result, warnings


def _save_plan(folder, plan):
    destination = folder / 'analysis' / 'sound_plan.json'
    temporary = None
    try:
        destination.parent.mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=destination.parent,
                prefix='.sound-plan-', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(plan.model_dump_json(indent=2) + '\n')
        temporary.replace(destination)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return {'plan': plan.model_dump(mode='json'), 'plan_path': str(destination)}


def _plan(project, kind, root, intents):
    folder, metadata = _project_context(project)
    with timeline_lock(folder):
        timeline = load_timeline(folder)
        if timeline.duration <= 0:
            raise AnalysisError('invalid_sound_timeline', 'Create a nonempty video timeline first')
        binding = _fingerprint(timeline)
        path = folder / 'analysis' / 'sound_plan.json'
        plan = _read_plan(folder) if path.exists() else SoundPlan(project=metadata.name, timeline_fingerprint=binding)
        if plan.timeline_fingerprint != binding:
            plan = SoundPlan(project=metadata.name, timeline_fingerprint=binding)
        assets, warnings = _catalog(kind, root)
        if intents is None:
            contexts, context_warnings = _contexts(folder, timeline)
            warnings += context_warnings
            if kind == 'music':
                intents = []
                for i, (scene, analysis, clip, has_audio) in enumerate(contexts):
                    cue = music_intent(scene, analysis, clip)
                    # Music boundaries follow the incoming picture during blends.
                    cue.end = min(cue.end, contexts[i + 1][2].timeline_start) if i + 1 < len(contexts) else cue.end
                    if cue.end <= cue.start:
                        continue
                    cue.fade_in = cue.fade_out = min(.25, (cue.end - cue.start) / 2)
                    cue.ducking.enabled = cue.ducking.enabled or has_audio
                    intents.append(cue.model_dump())
            else:
                intents = [cue.model_dump() for scene, analysis, clip, _ in contexts for cue in sfx_intents(scene, analysis, clip)]
        decisions = []
        for i, data in enumerate(intents):
            cue = (MusicIntent if kind == 'music' else SFXIntent).model_validate(data)
            if (cue.end > timeline.duration + 1e-9 if kind == 'music' else cue.timestamp >= timeline.duration):
                raise AnalysisError('sound_cue_out_of_range', 'Sound cue must lie inside current video duration')
            tags = cue.recommended_tags if kind == 'music' else cue.tags
            score, asset = _choose(tags, assets)
            reason = (f"Matched {sorted(set(tags) & set(asset['tags']))}: tag coverage {score:.3f}; asset ID breaks ties."
                      if asset else 'No available library asset matches these tags; unresolved, never substitute an invented file.')
            cls = MusicDecision if kind == 'music' else SFXDecision
            decisions.append(cls(**cue.model_dump(), id=f'{PREFIX}{kind}-{i:04d}',
                file=asset['file'] if asset else None, asset_id=asset['id'] if asset else None,
                match_score=score, reason=reason))
            if asset is None:
                warnings.append(f'Unresolved {kind} cue {i}.')
            if kind == 'sfx' and cue.timing == 'approximate':
                warnings.append(f'{cue.event}: approximate shot-onset timing; provide an exact cue for precise sync.')
        setattr(plan, kind, decisions)
        setattr(plan, f'{kind}_warnings', warnings)
        plan.warnings = list(dict.fromkeys([*plan.music_warnings, *plan.sfx_warnings]))
        plan = SoundPlan.model_validate(plan.model_dump())
        return _save_plan(folder, plan)


def _apply(project, kind, root):
    folder, metadata = _project_context(project)
    with timeline_lock(folder):
        timeline = load_timeline(folder)
        plan = _read_plan(folder)
        if plan.project != metadata.name or plan.timeline_fingerprint != _fingerprint(timeline):
            raise AnalysisError('stale_sound_plan', 'Timeline changed since planning; regenerate the sound plan')
        assets, _ = _catalog(kind, root)
        catalog = {asset['id']: asset for asset in assets}
        items = []
        for decision in getattr(plan, kind):
            asset = catalog.get(decision.asset_id)
            if asset is None or decision.file != asset['file']:
                raise AnalysisError('unresolved_sound_asset', 'Every applied cue must reference an available asset in the current library')
            tags = decision.recommended_tags if kind == 'music' else decision.tags
            if not math.isclose(_choose(tags, [asset])[0], decision.match_score, abs_tol=1e-9):
                raise AnalysisError('stale_sound_asset', 'Asset tags changed since planning; regenerate the plan')
            if not decision.id.startswith(f'{PREFIX}{kind}-'):
                raise AnalysisError('invalid_sound_decision_id', 'Sound decision IDs must use the reserved director namespace')
            if kind == 'music':
                if decision.end > timeline.duration + 1e-9:
                    raise AnalysisError('sound_cue_out_of_range', 'Music cue exceeds video duration')
                volume = -18 + (decision.ducking.attenuation_db if decision.ducking.enabled else 0)
                item = MusicClip(id=decision.id, file=asset['file'], timeline_start=decision.start, end=decision.end,
                    source_out=min(asset['duration'], decision.end - decision.start), loop=True,
                    volume_db=volume, fade_in=decision.fade_in, fade_out=decision.fade_out)
                validate_music_source(item, folder)
            else:
                if decision.timestamp >= timeline.duration:
                    raise AnalysisError('sound_cue_out_of_range', 'SFX cue exceeds video duration')
                length = min(asset['duration'], timeline.duration - decision.timestamp)
                item = SFXClip(id=decision.id, file=asset['file'], timeline_time=decision.timestamp,
                    source_out=length, tags=decision.tags, volume_db=-120 if decision.intensity == 0 else -30 + 24 * decision.intensity,
                    fade_in=min(.01, length / 2), fade_out=min(.01, length / 2))
                validate_sfx_source(item, folder)
            items.append(item)
        collection = 'music_tracks' if kind == 'music' else 'sfx_tracks'
        track_cls = MusicTrack if kind == 'music' else SFXTrack
        candidate = timeline.model_copy(deep=True)
        tracks = []
        for track in getattr(candidate, collection):
            track.clips = [c for c in track.clips if not c.id.startswith(f'{PREFIX}{kind}-')]
            if track.clips or track.id != f'{PREFIX}{kind}':
                tracks.append(track)
        if items:
            tracks.append(track_cls(id=f'{PREFIX}{kind}', clips=items))
        setattr(candidate, collection, tracks)
        if candidate.model_dump() == timeline.model_dump():
            return {'changed': False, 'inserted': len(items), 'timeline_path': str(folder / 'timeline.json'), 'backup_path': None}
        path, backup = save_locked_timeline(folder, metadata, candidate)
        return {'changed': True, 'inserted': len(items), 'timeline_path': str(path),
                'backup_path': str(backup) if backup else None, 'events': [item.model_dump(mode='json') for item in items]}


def _guard_call(function, *args):
    try:
        return function(*args)
    except Exception as exc:
        if callable(getattr(exc, 'to_dict', None)):
            raise
        raise AnalysisError('sound_director_failed', str(exc)) from exc


def plan_music(project, *, library_root=None, intents=None):
    return _guard_call(_plan, project, 'music', library_root, intents)


def plan_sfx(project, *, library_root=None, intents=None):
    return _guard_call(_plan, project, 'sfx', library_root, intents)


def apply_music_plan(project, *, library_root=None):
    return _guard_call(_apply, project, 'music', library_root)


def apply_sfx_plan(project, *, library_root=None):
    return _guard_call(_apply, project, 'sfx', library_root)
