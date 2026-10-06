"""Plan an explained rough cut, render it, then publish with timeline history."""

import hashlib
import json
import math
import os
import shutil
import tempfile
from pathlib import Path

from analysis.matching import ranking_policy, score_source
from engine import media, render
from engine.timeline import _project_context, create_empty_timeline, save_locked_timeline, timeline_lock, validate_timeline
from schemas.timeline import VideoClip, VideoTrack, Transition
from services.analysis_service import AnalysisError
from services.matching_service import _load

MAX_CLIPS = 1000
NARRATIVE_COMPONENTS = ('characters', 'action', 'emotion', 'location', 'shot_size', 'continuity')


def _plan(folder, metadata):
    breakdown, sources, warnings = _load(folder, 100)  # Loads all sources; this is not a top-100 shortlist.
    available = []
    for analysis, path in sources:
        try:
            if Path(path).resolve() == (folder / 'preview' / 'preview.mp4').resolve():
                raise AnalysisError('source_output_conflict', 'Preview output cannot also be source footage')
            if analysis.usable_start is None:
                raise AnalysisError('unknown_usable_range', 'Source has no established usable interval')
            actual = media.probe_media(path)
            if analysis.usable_end > actual['duration'] + 1e-6:
                raise AnalysisError('stale_usable_range', 'Usable interval exceeds current media duration; reanalyze source')
            start = math.ceil(analysis.usable_start * metadata.fps - 1e-9)
            end = math.floor(analysis.usable_end * metadata.fps + 1e-9)
            if end <= start:
                raise AnalysisError('usable_range_too_short', 'Usable interval contains no complete output frame')
            available.append((analysis, path, start, end))
        except Exception as exc:
            if getattr(exc, 'code', None) == 'ffprobe_missing':
                raise
            error = exc.to_dict() if callable(getattr(exc, 'to_dict', None)) else {'code': 'invalid_source', 'message': str(exc)}
            warnings.append({'source_id': analysis.source_id, **error})
    timeline = create_empty_timeline(metadata)
    clips, decisions, used = [], [], set()
    total_frames = 0
    for scene in breakdown.requirements:
        ranked = []
        for analysis, path, start, end in available:
            candidate = score_source(scene, analysis)
            active_story = [candidate['components'][name] for name in NARRATIVE_COMPONENTS
                            if candidate['components'][name]['active']]
            # Do not fill a narrative scene using duration/quality evidence alone.
            if active_story and not any(item['score'] > 0 for item in active_story):
                continue
            ranked.append((candidate, path, start, end))
        ranked.sort(key=lambda item: (-item[0]['score'],
            -item[0]['components']['visual_quality']['score'], item[0]['source_id']))
        if not ranked:
            raise AnalysisError('no_eligible_source', f'No valid analyzed source has usable frames and matching story evidence for {scene.scene_id}')
        target_frames = max(1, math.floor(scene.estimated_duration * metadata.fps + .5))
        remaining = target_frames
        selected = []
        while remaining:
            if len(clips) >= MAX_CLIPS:
                raise AnalysisError('rough_cut_too_large', f'Rough cut exceeds {MAX_CLIPS} clips; revise scene durations or usable intervals')
            unused = [item for item in ranked if os.path.normcase(str(Path(item[1]).resolve())) not in used]
            candidate, path, start, end = (unused or ranked)[0]
            repeated = not unused
            count = min(remaining, end - start)
            clip = VideoClip(id=f'rough-{len(clips) + 1:04d}', source=path,
                source_in=start / metadata.fps, source_out=(start + count) / metadata.fps,
                timeline_start=total_frames / metadata.fps)
            clips.append(clip)
            used.add(os.path.normcase(str(Path(path).resolve())))
            selected.append({'clip_id': clip.id, 'source_id': candidate['source_id'],
                'source_in': clip.source_in, 'source_out': clip.source_out,
                'timeline_start': clip.timeline_start, 'duration': count / metadata.fps,
                'reused_source': repeated, 'score': candidate['score'], 'components': candidate['components'],
                'reason': ('No unused eligible source remains; reuse the highest-ranked eligible source.' if repeated
                           else 'Highest-ranked unused eligible source; quality breaks score ties.'),
                'duration_reason': 'Use the start of the frame-aligned usable interval, trimmed to remaining scene duration.'})
            total_frames += count
            remaining -= count
        decisions.append({'scene_id': scene.scene_id, 'story_order': scene.story_order,
            'requested_duration': scene.estimated_duration, 'duration': target_frames / metadata.fps,
            'duration_rounding': target_frames / metadata.fps - scene.estimated_duration,
            'eligible_sources': len(ranked), 'selections': selected})
    transitions = [Transition(id=f'rough-cut-{i}', from_clip=left.id, to_clip=right.id, type='cut', duration=0)
                   for i, (left, right) in enumerate(zip(clips, clips[1:]), 1)]
    timeline.video_tracks = [VideoTrack(id='rough-video', clips=clips, transitions=transitions)]
    validation = validate_timeline(timeline, base_dir=folder)
    if not validation.valid:
        raise AnalysisError('invalid_rough_cut', str(validation.model_dump(mode='json')))
    report = {'version': '1.0', 'project': metadata.name, 'duration': total_frames / metadata.fps,
        'frames': total_frames, 'fps': metadata.fps, 'decisions': decisions, 'warnings': warnings,
        'scoring': ranking_policy(), 'selection_policy':
        'Story order; unused eligible sources first, then score descending, quality descending, source ID ascending. '
        'At least one specified story component must match. Repeat only when unused eligible sources are exhausted. '
        'Usable bounds round inward to output frames; scene durations round to nearest frame (minimum one).'}
    return timeline, report


def _build_rough_cut(project: str | Path) -> dict:
    folder, metadata = _project_context(project)
    with timeline_lock(folder):
        timeline, report = _plan(folder, metadata)
        cache, preview, analysis = folder / 'cache', folder / 'preview', folder / 'analysis'
        for directory in (cache, preview, analysis):
            directory.mkdir(exist_ok=True)
            if directory.is_symlink() or directory.resolve().parent != folder:
                raise AnalysisError('invalid_project_directory', f'{directory.name} must be a real project directory')
        timeline_path = folder / 'timeline.json'
        report_path = analysis / 'rough_cut_report.json'
        preview_path = preview / 'preview.mp4'
        with tempfile.TemporaryDirectory(prefix='rough-cut-', dir=cache) as temporary:
            stage = Path(temporary)
            (stage / 'project.json').write_text(metadata.model_dump_json(indent=2), encoding='utf-8')
            timeline_bytes = (timeline.model_dump_json(indent=2) + '\n').encode('utf-8')
            (stage / 'timeline.json').write_bytes(timeline_bytes)
            # The existing renderer reads this saved timeline; no FFmpeg logic is duplicated.
            rendered = render.render_timeline(stage)
            preview_metadata = media.probe_media(rendered)
            report.update(timeline_sha256=hashlib.sha256(timeline_bytes).hexdigest(),
                          timeline_path=str(timeline_path), preview_path=str(preview_path),
                          preview_metadata=preview_metadata)
            # Store canonical output identity, not the soon-to-be-deleted staging path.
            report['preview_metadata'] = {**preview_metadata, 'path': str(preview_path)}
            report['preview_metadata'].pop('id', None)
            old_timeline, old_report = stage / 'previous-timeline.json', stage / 'previous-report.json'
            if timeline_path.exists():
                shutil.copyfile(timeline_path, old_timeline)
            if report_path.exists():
                shutil.copyfile(report_path, old_report)
            saved = False
            report_published = False
            try:
                _, backup = save_locked_timeline(folder, metadata, timeline)
                saved = True
                report['backup_path'] = str(backup) if backup else None
                staged_report = stage / 'report.json'
                staged_report.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
                staged_report.replace(report_path)
                report_published = True
                rendered.replace(preview_path)  # Atomic last step; previous preview survives failures.
            except Exception as exc:
                rollback_errors = []
                for changed, original, destination in ((saved, old_timeline, timeline_path),
                                                       (report_published, old_report, report_path)):
                    if changed:
                        try:
                            if original.exists():
                                original.replace(destination)
                            else:
                                destination.unlink(missing_ok=True)
                        except OSError as rollback:
                            rollback_errors.append(str(rollback))
                raise AnalysisError('rough_cut_publish_failed', f'{exc}; rollback errors: {rollback_errors}') from exc
        return {'timeline_path': str(timeline_path), 'preview_path': str(preview_path),
                'report_path': str(report_path), 'report': report}


def build_rough_cut(project: str | Path) -> dict:
    """Rebuild the project's rough cut and preview, preserving the prior timeline."""
    try:
        return _build_rough_cut(project)
    except Exception as exc:
        if callable(getattr(exc, 'to_dict', None)):
            raise
        raise AnalysisError('rough_cut_failed', str(exc)) from exc
