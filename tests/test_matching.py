import asyncio
import json
from copy import deepcopy

import pytest

from analysis.matching import WEIGHTS, score_source
from analysis.provider import SuppliedAnalysisProvider
from mcp_server import build_server
from schemas.script import SceneRequirement
from schemas.source_analysis import SourceAnalysis
from services.analysis_service import AnalysisError, analyze_source
from services.matching_service import find_candidates_for_scene, rank_sources_for_script
from services.project_service import create_project
from services.script_service import analyze_script


@pytest.fixture
def scene():
    return SceneRequirement(scene_id='scene_04', story_order=1, characters=['Mai', 'An'],
        location='Kitchen', action='Mai opens letter', emotion='anxious', dialogue=None,
        preferred_shot_size='medium', continuity_requirements=['red coat left hand'],
        estimated_duration=10, notes=[])


@pytest.fixture
def source():
    return SourceAnalysis(source_id='source_032', characters=['Mai', 'An'], location='Kitchen',
        shot_size='MS', camera_angle=None, camera_motion=None, action='Mai opens letter',
        emotion='anxious', dialogue=None, visual_quality='Sharp and clear',
        continuity_notes=['red coat left hand'], usable_start=1, usable_end=11,
        problems=[], description=None)


def test_perfect_score_and_component_math(scene, source):
    result = score_source(scene, source)
    assert result['score'] == pytest.approx(1)
    assert set(result['components']) == set(WEIGHTS)
    assert sum(WEIGHTS.values()) == pytest.approx(1)
    assert sum(c['contribution'] for c in result['components'].values()) == pytest.approx(result['score'])
    assert all(c['score'] == 1 for c in result['components'].values())
    json.dumps(result, allow_nan=False)


def test_partial_score_is_explainable(scene, source):
    source.characters = ['Mai']
    source.action = 'Mai waits'
    source.emotion = 'happy'
    source.location = 'Garden'
    source.shot_size = 'MCU'
    source.continuity_notes = ['red coat right hand']
    source.usable_end = 6
    result = score_source(scene, source)
    expected = .2 * .5 + .2 / 3 + .1 * 0 + .1 * 0 + .1 * .5 + .1 + .1 * .75 + .1 * .5
    assert result['score'] == pytest.approx(expected)
    assert result['components']['characters']['explanation']['missing'] == ['an']
    assert result['components']['usable_duration']['explanation']['shortfall_seconds'] == 5
    assert result['components']['action']['explanation']['missing_tokens'] == ['letter', 'opens']


@pytest.mark.parametrize('field,value', [('action', None), ('emotion', None), ('location', None)])
def test_missing_source_evidence_scores_zero(scene, source, field, value):
    setattr(source, field, value)
    result = score_source(scene, source)
    assert result['components'][field]['score'] == 0
    assert result['components'][field]['active']


def test_unspecified_requirements_do_not_dilute_other_components(scene, source):
    scene.characters = []
    scene.action = scene.emotion = scene.location = scene.preferred_shot_size = None
    scene.continuity_requirements = []
    result = score_source(scene, source)
    assert result['score'] == pytest.approx(1)
    assert result['active_weight_total'] == pytest.approx(.2)
    assert result['components']['visual_quality']['contribution'] == pytest.approx(.5)


def test_unknown_duration_and_quality_are_not_perfect(scene, source):
    source.usable_start = source.usable_end = None
    source.visual_quality = None
    result = score_source(scene, source)
    assert result['components']['usable_duration']['score'] == 0
    assert result['components']['visual_quality']['score'] == 0


@pytest.mark.parametrize('quality,problems,expected', [
    ('sharp', [], 1), ('sharp but blurry', [], .25), ('not sharp', [], .25),
    ('unmapped prose', [], .5), ('clear', ['artifact', 'artifact'], .9),
    ('poor', ['blur', 'noise', 'artifact'], 0), (None, [], 0),
])
def test_quality_rules(scene, source, quality, problems, expected):
    source.visual_quality, source.problems = quality, problems
    assert score_source(scene, source)['components']['visual_quality']['score'] == pytest.approx(expected)


def test_unicode_normalization_negation_and_shot_aliases(scene, source):
    scene.characters, source.characters = ['MAI'], ['mai']
    scene.action, source.action = 'Mở cửa', 'MỞ CỬA'
    scene.preferred_shot_size, source.shot_size = 'close-up', 'CU'
    result = score_source(scene, source)
    assert result['components']['action']['score'] == 1
    assert result['components']['shot_size']['score'] == 1
    source.action = 'không mở cửa'
    result = score_source(scene, source)
    assert result['components']['action']['score'] == 0
    assert result['components']['action']['explanation']['negation_mismatch']


@pytest.fixture
def project(tmp_path, scene, source):
    result = create_project('Candidates', tmp_path, projects_root=tmp_path / 'projects')
    assert result.success
    folder = result.project_path
    second_scene = scene.model_copy(update={'scene_id': 'scene_05', 'story_order': 2})
    analyze_script(folder, 'Supplied test script', breakdown={
        'requirements': [scene.model_dump(), second_scene.model_dump()]})
    records = []
    for source_id in ['source_032', 'source_017', 'source_045']:
        path = tmp_path / f'{source_id}.mp4'
        path.write_bytes(b'mock media; matching does not decode')
        records.append({'id': source_id, 'path': str(path), 'duration': 20, 'width': 100, 'height': 100, 'fps': 24})
    (folder / 'source_index.json').write_text(json.dumps({'sources': records}), encoding='utf-8')
    for record in records:
        data = source.model_dump()
        data['source_id'] = record['id']
        if record['id'] == 'source_017':
            data['characters'] = ['Mai']
        elif record['id'] == 'source_045':
            data['action'] = 'Someone runs away'
        analyze_source(folder, record['id'], provider=SuppliedAnalysisProvider(data))
    return folder


def test_rank_order_script_limit_and_read_only(project):
    before = {p: p.read_bytes() for p in project.parent.parent.rglob('*') if p.is_file()}
    result = find_candidates_for_scene(project, 'scene_04')
    assert [c['source_id'] for c in result['candidates']] == ['source_032', 'source_017', 'source_045']
    assert result['warnings'] == []
    assert result['scoring']['version'] == 'lexical-v1'
    ranked = rank_sources_for_script(project, limit=2)
    assert list(ranked['scenes']) == ['scene_04', 'scene_05']
    assert ranked['scenes']['scene_04']['candidates'] == result['candidates'][:2]
    assert ranked['scenes']['scene_04']['total_candidates'] == 3
    after = {p: p.read_bytes() for p in project.parent.parent.rglob('*') if p.is_file()}
    assert before == after


def test_ties_use_source_id_not_index_order(project):
    path = project / 'analysis' / 'source_017.json'
    document = json.loads((project / 'analysis' / 'source_032.json').read_text())
    document['source_id'] = 'source_017'
    path.write_text(json.dumps(document))
    assert [c['source_id'] for c in find_candidates_for_scene(project, 'scene_04')['candidates']][:2] == ['source_017', 'source_032']


def test_missing_corrupt_and_unavailable_sources_are_reported(project):
    (project / 'analysis' / 'source_017.json').unlink()
    (project / 'analysis' / 'source_045.json').write_text('bad json')
    result = find_candidates_for_scene(project, 'scene_04')
    assert len(result['candidates']) == 1
    assert {w['code'] for w in result['warnings']} == {'analysis_not_found', 'analysis_read_failed'}
    (project.parent.parent / 'source_032.mp4').unlink()
    result = rank_sources_for_script(project)
    assert not result['scenes']['scene_04']['candidates']
    assert len(result['warnings']) == 3


@pytest.mark.parametrize('limit', [0, -1, 101, True, 2.5])
def test_invalid_limit(project, limit):
    with pytest.raises(AnalysisError) as error:
        rank_sources_for_script(project, limit)
    assert error.value.code == 'invalid_candidate_limit'


def test_missing_scene_and_bad_index(project):
    with pytest.raises(AnalysisError) as error:
        find_candidates_for_scene(project, 'missing')
    assert error.value.code == 'scene_not_found'
    (project / 'source_index.json').write_text('[]')
    with pytest.raises(AnalysisError) as error:
        rank_sources_for_script(project)
    assert error.value.code == 'invalid_source_index'


def test_empty_index_returns_empty_rankings(project):
    (project / 'source_index.json').write_text('{"sources": []}')
    result = rank_sources_for_script(project)
    assert result['warnings'] == []
    assert all(not value['candidates'] for value in result['scenes'].values())


def test_mcp_ranking_and_error_recovery(project):
    server = build_server(project.parent)
    async def exercise():
        async def call(name, args):
            raw = await server.call_tool(name, args)
            return raw[1] if isinstance(raw, tuple) else raw
        found = await call('find_candidates_for_scene', {'project': 'Candidates', 'scene_id': 'scene_04'})
        assert found['success']
        assert found['data']['candidates'][0]['source_id'] == 'source_032'
        assert (await call('rank_sources_for_script', {'project': 'Candidates'}))['success']
        bad = await call('find_candidates_for_scene', {'project': 'Candidates', 'scene_id': 'unknown'})
        assert bad['error']['code'] == 'scene_not_found'
        assert (await call('ping', {}))['success']
    asyncio.run(exercise())
