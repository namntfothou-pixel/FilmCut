"""Metadata-based recommendations, previewable plans and atomic timeline edits."""

import hashlib
import json
import math
import tempfile
from pathlib import Path

from analysis.matching import normalize
from engine.audio import probe_audio
from engine.timeline import _project_context, _native_path, load_timeline, save_locked_timeline, timeline_lock
from schemas.refinement import EditRecommendation, EditRefinementPlan
from services.analysis_service import AnalysisError
from services.sound_service import _contexts
from services import timeline_service

POLICY = ('Hard cuts default. At most max(1, floor(boundaries/3)) enabled non-hard recommendations; '
          'no neighboring non-hard recommendations. Blends require explicit narrative cues and no dialogue. '
          'J/L cuts require dialogue flow evidence and real audio handles. Existing effects/audio edits are preserved. '
          'Timing from untimed dialogue is provisional; listen before applying.')


def _hash(timeline):
    return hashlib.sha256(json.dumps(timeline.model_dump(mode='json'), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _clips(timeline):
    if len(timeline.video_tracks) != 1:
        raise AnalysisError('invalid_refinement_timeline', 'Refinement requires one video track')
    return sorted((c for c in timeline.video_tracks[0].clips if c.enabled), key=lambda c: c.timeline_start)


def _budget(timeline, decisions):
    clips = _clips(timeline)
    positions = {(a.id, b.id): i for i, (a, b) in enumerate(zip(clips, clips[1:]))}
    nonhard = []
    for decision in decisions:
        boundary = (decision.from_clip, decision.to_clip)
        if boundary not in positions:
            raise AnalysisError('invalid_refinement_boundary', 'Recommendations must reference adjacent enabled clips')
        if decision.enabled and decision.type != 'hard_cut':
            nonhard.append(positions[boundary])
    if len(nonhard) > max(1, len(positions) // 3) or any(b - a == 1 for a, b in zip(sorted(nonhard), sorted(nonhard)[1:])):
        raise AnalysisError('too_many_refinements', 'Keep hard cuts dominant; non-hard recommendations must be sparse and non-adjacent')


def _simulate(folder, metadata, timeline, decisions):
    """Reuse existing edit services on a temporary timeline, then save once canonically."""
    _budget(timeline, decisions)
    candidate = timeline.model_copy(deep=True)
    original_paths = []
    for collection in ('video_tracks', 'audio_tracks', 'music_tracks', 'sfx_tracks', 'subtitle_tracks'):
        for track in getattr(candidate, collection):
            items = [track] if collection == 'subtitle_tracks' else track.clips
            for item in items:
                field = 'file' if collection in ('music_tracks', 'sfx_tracks', 'subtitle_tracks') else 'source'
                value = getattr(item, field)
                if value is not None:
                    path = _native_path(value)
                    original_paths.append((collection, item.id, field, value))
                    setattr(item, field, str(path if path.is_absolute() else folder / path))
    cache = folder / 'cache'
    cache.mkdir(exist_ok=True)
    if cache.is_symlink():
        raise AnalysisError('invalid_cache', 'Refinement cache must stay inside its project')
    changes = []
    with tempfile.TemporaryDirectory(prefix='refinement-', dir=cache) as directory:
        stage = Path(directory)
        (stage / 'project.json').write_text(metadata.model_dump_json(), encoding='utf-8')
        (stage / 'timeline.json').write_text(candidate.model_dump_json(indent=2), encoding='utf-8')
        positions = {(a.id, b.id): i for i, (a, b) in enumerate(zip(_clips(timeline), _clips(timeline)[1:]))}
        for decision in sorted((d for d in decisions if d.enabled), key=lambda d: positions[(d.from_clip, d.to_clip)]):
            current = load_timeline(stage)
            track = current.video_tracks[0]
            left = next(c for c in track.clips if c.id == decision.from_clip)
            right = next(c for c in track.clips if c.id == decision.to_clip)
            old = next((t for t in track.transitions if t.from_clip == left.id), None)
            if decision.type in ('hard_cut', 'crossfade', 'fade_to_black'):
                kind = 'cut' if decision.type == 'hard_cut' else decision.type
                overlap = 0 if old is None else old.overlap
                if abs(decision.duration - overlap) > 1e-9:
                    timed = any(getattr(current, k) for k in ('audio_tracks', 'music_tracks', 'sfx_tracks', 'subtitle_tracks'))
                    if timed or any(c.has_audio_offset for c in track.clips):
                        raise AnalysisError('unsafe_timing_change', 'Visual overlap would shift existing audio/subtitles. Refine picture before sound, or choose J/L cuts.')
                if kind != 'cut' and not math.isclose(decision.duration * current.fps, round(decision.duration * current.fps), abs_tol=1e-7):
                    raise AnalysisError('unaligned_transition', 'Visual transition duration must align to output frames')
                if old is not None and old.type == kind and old.duration == decision.duration:
                    continue
                change = timeline_service.set_transition(stage, left.id, kind, decision.duration)
            else:
                target = right if decision.type == 'j_cut' else left
                if target.has_audio_offset or (old is not None and old.type != 'cut'):
                    raise AnalysisError('existing_boundary_edit', 'Reset existing audio offsets/blend before requesting a new dialogue edit')
                operation = timeline_service.set_j_cut if decision.type == 'j_cut' else timeline_service.set_l_cut
                change = operation(stage, target.id, decision.duration)
            changes.append({'from_clip': left.id, 'to_clip': right.id, 'type': decision.type,
                            'duration': decision.duration, 'summary': change['summary']})
        candidate = load_timeline(stage)
    for collection, identifier, field, value in original_paths:
        for track in getattr(candidate, collection):
            items = [track] if collection == 'subtitle_tracks' else track.clips
            for item in items:
                if item.id == identifier:
                    setattr(item, field, value)
    return candidate, changes


def _automatic(folder, timeline):
    contexts, warnings = _contexts(folder, timeline)
    by_id = {clip.id: (scene, analysis, audio) for scene, analysis, clip, audio in contexts}
    clips = _clips(timeline)
    decisions = []
    used, previous_nonhard = 0, -2
    limit = max(1, (len(clips) - 1) // 3)
    supplemental = any(getattr(timeline, k) for k in ('audio_tracks', 'music_tracks', 'sfx_tracks', 'subtitle_tracks'))
    any_offset = any(c.has_audio_offset for c in clips)
    for i, (left, right) in enumerate(zip(clips, clips[1:])):
        decision = EditRecommendation(from_clip=left.id, to_clip=right.id, type='hard_cut', duration=0,
            reason='Hard cut preserves pace and picture timing; no strong narrative or dialogue-flow cue.')
        existing = next((t for t in timeline.video_tracks[0].transitions if t.from_clip == left.id), None)
        if (existing and existing.type != 'cut') or left.has_audio_offset or right.has_audio_offset:
            decision.enabled = False
            decision.reason = 'Preserve existing manual visual/audio boundary edit.'
            decisions.append(decision)
            continue
        if left.id not in by_id or right.id not in by_id:
            decision.evidence = ['Missing valid analysis for one side; retain hard cut.']
            decisions.append(decision)
            continue
        ls, la, lhas = by_id[left.id]
        rs, ra, rhas = by_id[right.id]
        ldialogue, rdialogue = bool(la.dialogue or ls.dialogue), bool(ra.dialogue or rs.dialogue)
        text = normalize(' '.join([*rs.notes, *rs.continuity_requirements, rs.location or '']))
        lead = normalize(' '.join([*rs.notes, la.action or '', *ls.notes]))
        trail = normalize(' '.join([*ls.notes, ra.action or '', *rs.notes]))
        allowed = used < limit and i > previous_nonhard + 1
        kind, duration, reason = 'hard_cut', 0, decision.reason
        frame_cap = math.floor(min(left.duration, right.duration) * .25 * timeline.fps + 1e-9)
        if allowed and not supplemental and not any_offset and not ldialogue and not rdialogue and frame_cap:
            if any(term in text for term in ('next day', 'hours later', 'time jump', 'scene break', 'fade to black', 'fade out')):
                kind = 'fade_to_black'
                duration = min(frame_cap, max(1, math.floor(.35 * timeline.fps))) / timeline.fps
                reason = 'Explicit scene/time-break cue; a short fade marks the break.'
            elif any(term in text for term in ('crossfade', 'dissolve', 'dream sequence', 'memory montage', 'montage')):
                kind = 'crossfade'
                duration = min(frame_cap, max(1, math.floor(.25 * timeline.fps))) / timeline.fps
                reason = 'Explicit dissolve/montage cue with no reported dialogue at the boundary.'
        if allowed and kind == 'hard_cut':
            try:
                if rdialogue and rhas and (('j cut' in lead) or (not ldialogue and any(t in lead for t in ('reaction', 'listens', 'listening')))):
                    path = _native_path(right.source)
                    audio = probe_audio(path if path.is_absolute() else folder / path)
                    if right.source_out > audio['duration'] + 1e-6:
                        raise ValueError('Incoming trim exceeds actual source audio')
                    kind = 'j_cut'
                    duration = min(.2, right.source_in / right.speed, right.timeline_start, left.duration / 2)
                    reason = 'Incoming dialogue over a reaction/listening shot; lead speech while preserving picture.'
                elif ldialogue and lhas and (('l cut' in trail) or (not rdialogue and any(t in trail for t in ('reaction', 'listens', 'listening')))):
                    path = _native_path(left.source)
                    audio = probe_audio(path if path.is_absolute() else folder / path)
                    kind = 'l_cut'
                    duration = min(.2, (audio['duration'] - left.source_out) / left.speed, right.duration / 2)
                    reason = 'Outgoing dialogue continues over a reaction/listening shot; preserve picture.'
                if kind in ('j_cut', 'l_cut') and duration <= 1 / 48000:
                    kind, duration, reason = 'hard_cut', 0, 'Insufficient real source-audio handle for a dialogue overlap.'
            except Exception as exc:
                kind, duration = 'hard_cut', 0
                warnings.append(f'Audio-handle check failed at {left.id}: {exc}')
        if kind != 'hard_cut':
            used += 1
            previous_nonhard = i
            if kind in ('j_cut', 'l_cut'):
                warnings.append(f'{left.id}/{right.id}: dialogue metadata is untimed; overlap is a provisional listening recommendation, not word-level speech alignment.')
        decision.type, decision.duration, decision.reason = kind, duration, reason
        decision.evidence = [f'Scenes: {ls.scene_id} -> {rs.scene_id}', f'Dialogue metadata: outgoing={ldialogue}, incoming={rdialogue}',
                             f'Incoming narrative notes: {text}', f'Sparse-effect budget: {limit}']
        if supplemental and any(t in text for t in ('montage', 'dissolve', 'time jump', 'next day', 'fade to black')):
            warnings.append(f'{left.id}: retain hard cut because a blend would shift existing timed sound/subtitles.')
        decisions.append(EditRecommendation.model_validate(decision.model_dump()))
    return decisions, warnings


def _write_plan(folder, plan):
    path = folder / 'analysis' / 'edit_refinement_plan.json'
    temporary = None
    try:
        path.parent.mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, prefix='.refinement-', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(plan.model_dump_json(indent=2) + '\n')
        temporary.replace(path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return {'plan': plan.model_dump(mode='json'), 'plan_path': str(path)}


def _plan(project, recommendations):
    folder, metadata = _project_context(project)
    with timeline_lock(folder):
        timeline = load_timeline(folder)
        if not _clips(timeline):
            raise AnalysisError('invalid_refinement_timeline', 'Create a nonempty rough cut first')
        if recommendations is None:
            decisions, warnings = _automatic(folder, timeline)
        else:
            decisions = [EditRecommendation.model_validate(item) for item in recommendations]
            warnings = ['Supplied recommendations must be reviewed for narrative/dialogue suitability.']
        # Validation includes IDs, budgets, actual handles and the resulting timeline.
        skeleton = EditRefinementPlan(project=metadata.name, timeline_fingerprint=_hash(timeline), decisions=decisions,
            before_duration=timeline.duration, after_duration=timeline.duration, warnings=warnings, policy=POLICY)
        candidate, _ = _simulate(folder, metadata, timeline, skeleton.decisions)
        skeleton.after_duration = candidate.duration
        return _write_plan(folder, skeleton)


def _apply(project):
    folder, metadata = _project_context(project)
    with timeline_lock(folder):
        timeline = load_timeline(folder)
        path = folder / 'analysis' / 'edit_refinement_plan.json'
        if not path.is_file():
            raise AnalysisError('refinement_plan_not_found', 'Plan edit refinement first')
        plan = EditRefinementPlan.model_validate_json(path.read_text(encoding='utf-8'))
        if plan.project != metadata.name or plan.timeline_fingerprint != _hash(timeline):
            raise AnalysisError('stale_refinement_plan', 'Timeline changed since planning; create a fresh refinement plan')
        candidate, changes = _simulate(folder, metadata, timeline, plan.decisions)
        if not math.isclose(plan.before_duration, timeline.duration, abs_tol=1e-9) or not math.isclose(plan.after_duration, candidate.duration, abs_tol=1e-9):
            raise AnalysisError('refinement_prediction_mismatch', 'Recommendations changed after planning; regenerate the inspected plan with updated recommendations')
        if candidate.model_dump() == timeline.model_dump():
            return {'changed': False, 'changes': [], 'timeline_path': str(folder / 'timeline.json'), 'backup_path': None}
        saved, backup = save_locked_timeline(folder, metadata, candidate)
        return {'changed': True, 'changes': changes, 'before_duration': timeline.duration, 'after_duration': candidate.duration,
                'timeline_path': str(saved), 'backup_path': str(backup) if backup else None,
                'preview_stale': True, 'sound_plan_stale': True}


def _guard(function, *args):
    try:
        return function(*args)
    except Exception as exc:
        if callable(getattr(exc, 'to_dict', None)):
            raise
        raise AnalysisError('edit_refinement_failed', str(exc)) from exc


def plan_edit_refinement(project, *, recommendations=None):
    return _guard(_plan, project, recommendations)


def apply_edit_refinement(project):
    return _guard(_apply, project)
