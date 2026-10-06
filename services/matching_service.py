"""Read-only candidate ranking from saved script requirements and analyses."""

import json
from pathlib import Path

from analysis.matching import ranking_policy, score_source
from engine.media import _existing_path
from schemas.script import ScriptBreakdown
from schemas.source_analysis import SourceAnalysis
from services.analysis_service import AnalysisError, get_source_analysis
from services.script_service import _folder, get_script_breakdown


def _load(project, limit):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise AnalysisError('invalid_candidate_limit', 'limit must be an integer between 1 and 100')
    folder = _folder(project)
    breakdown = ScriptBreakdown.model_validate(get_script_breakdown(folder)['breakdown'])
    try:
        index = json.loads((folder / 'source_index.json').read_text(encoding='utf-8'))
        records = index['sources']
        if not isinstance(records, list) or any(not isinstance(item, dict) or not isinstance(item.get('id'), str) for item in records):
            raise ValueError('Expected indexed source objects with string IDs')
        if len({item['id'] for item in records}) != len(records):
            raise ValueError('Duplicate source IDs in source index')
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise AnalysisError('invalid_source_index', str(exc)) from exc
    sources, warnings = [], []
    for record in sorted(records, key=lambda item: item['id']):
        try:
            saved = get_source_analysis(folder, record['id'])
            source = SourceAnalysis.model_validate(saved['analysis'])
            path = _existing_path(record.get('path', ''))
            sources.append((source, str(path)))
        except Exception as exc:
            error = exc.to_dict() if callable(getattr(exc, 'to_dict', None)) else {'code': 'invalid_source', 'message': str(exc)}
            warnings.append({'source_id': record['id'], **error})
    return breakdown, sources, warnings


def _rank(scene, sources, limit):
    candidates = [{**score_source(scene, source), 'path': path} for source, path in sources]
    candidates.sort(key=lambda item: (-item['score'], item['source_id']))
    return {'scene_id': scene.scene_id, 'candidates': candidates[:limit], 'total_candidates': len(candidates)}


def find_candidates_for_scene(project: str | Path, scene_id: str, limit: int = 5) -> dict:
    breakdown, sources, warnings = _load(project, limit)
    scene = next((item for item in breakdown.requirements if item.scene_id == scene_id), None)
    if scene is None:
        raise AnalysisError('scene_not_found', f'Scene {scene_id!r} is not in the saved script breakdown')
    return {**_rank(scene, sources, limit), 'warnings': warnings, 'scoring': ranking_policy()}


def rank_sources_for_script(project: str | Path, limit: int = 5) -> dict:
    breakdown, sources, warnings = _load(project, limit)
    return {'scenes': {scene.scene_id: _rank(scene, sources, limit) for scene in breakdown.requirements},
            'warnings': warnings, 'scoring': ranking_policy()}
