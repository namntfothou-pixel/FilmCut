import asyncio
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from analysis.script_provider import LocalScriptParser
from mcp_server import build_server
from schemas.script import SceneRequirement, ScriptBreakdown
from services.analysis_service import AnalysisError
from services.project_service import create_project
from services.script_service import analyze_script, get_script_breakdown

SCRIPT = (Path(__file__).parent / 'fixtures' / 'short_drama.txt').read_text(encoding='utf-8')


@pytest.fixture
def project(tmp_path):
    result = create_project('Drama', tmp_path, projects_root=tmp_path / 'projects')
    assert result.success
    return result.project_path


def test_short_drama_breakdown_and_unchanged_timeline(project):
    before = {p: p.read_bytes() for p in project.rglob('*') if p.is_file()}
    result = analyze_script(project, SCRIPT)
    scenes = result['breakdown']['requirements']
    assert len(scenes) == 2
    first, second = scenes
    assert first['scene_id'] == 'scene_001' and second['story_order'] == 2
    assert set(first) == set(SceneRequirement.model_fields)
    assert first['characters'] == ['MAI', 'AN']
    assert first['location'] == "MAI'S KITCHEN - NIGHT"
    assert first['action'] == 'Mai finds a sealed letter beside two cold cups of tea.'
    assert first['emotion'] == 'anxious'
    assert first['preferred_shot_size'] == 'medium'
    assert first['estimated_duration'] == 12
    assert 'MAI: Anh đã hứa sẽ quay lại.' in first['dialogue']
    assert 'AN: I kept my promise.' in first['dialogue']
    assert first['continuity_requirements'] == ["Mai holds the letter in her left hand."]
    assert 'MAI (quietly)' in first['notes']
    assert second['estimated_duration'] > 0
    assert any('Rough duration estimate' in note for note in second['notes'])
    saved = project / 'analysis' / 'script_breakdown.json'
    assert result['breakdown_path'] == str(saved)
    assert 'Vào nhà đi.' in saved.read_text(encoding='utf-8')
    assert get_script_breakdown(project) == result
    assert all(p.read_bytes() == content for p, content in before.items())
    assert set(p for p in project.rglob('*') if p.is_file()) == set(before) | {saved}


def test_shot_script_explicit_fields():
    result = LocalScriptParser().analyze('''SHOT 1 - Establishing
Location: A café
Characters: Mai, An
Action: Mai enters.
Dialogue: Mai: Xin chào.
Preferred shot size: wide
Continuity requirements: Same red coat.
Estimated duration: 4.5s

SHOT 2 - Reaction
Location: A café
Action: An looks up.
''')
    first, second = result.requirements
    assert first.estimated_duration == 4.5
    assert first.preferred_shot_size == 'wide'
    assert first.continuity_requirements == ['Same red coat.']
    assert second.emotion is None and second.dialogue is None
    assert second.characters == []


def test_consecutive_speakers_and_screenplay_directions():
    scene = LocalScriptParser().analyze('INT. ROOM - DAY\nMAI\nHello.\nAN\nHi.\nCUT TO:\n').requirements[0]
    assert scene.characters == ['MAI', 'AN']
    assert scene.dialogue == 'MAI: Hello.\nAN: Hi.'
    assert 'CUT TO:' in scene.notes


@pytest.mark.parametrize('heading', ['INT./EXT. CAR - DAY', 'EXT. ROAD - DAY', 'SCENE 1 - Arrival', 'CẢNH 1 - Đêm'])
def test_heading_formats(heading):
    assert len(LocalScriptParser().analyze(heading + '\nAction: A door closes.').requirements) == 1


@pytest.mark.parametrize('script,code', [('', 'invalid_script'), ('  ', 'invalid_script'),
    ('x' * 1_000_001, 'invalid_script'), ('An unstructured paragraph.', 'unsupported_script_format'),
    ('SHOT 1\nDuration: five', 'unsupported_script_format')])
def test_invalid_input_preserves_prior_breakdown(project, script, code):
    analyze_script(project, SCRIPT)
    saved = project / 'analysis' / 'script_breakdown.json'
    before = saved.read_bytes()
    with pytest.raises(AnalysisError) as error:
        analyze_script(project, script)
    assert error.value.code == code
    assert saved.read_bytes() == before


@pytest.mark.parametrize('field,value', [('estimated_duration', -1), ('estimated_duration', float('nan')),
    ('story_order', 0), ('story_order', True), ('characters', 'Mai'), ('scene_id', '')])
def test_invalid_requirement(field, value):
    scene = LocalScriptParser().analyze(SCRIPT).requirements[0].model_dump()
    with pytest.raises(ValidationError):
        SceneRequirement.model_validate({**scene, field: value})


@pytest.mark.parametrize('change', ['duplicate', 'order', 'empty', 'version'])
def test_bad_breakdown_rejected(project, change):
    value = LocalScriptParser().analyze(SCRIPT).model_dump()
    if change == 'duplicate':
        value['requirements'][1]['scene_id'] = value['requirements'][0]['scene_id']
    elif change == 'order':
        value['requirements'].reverse()
    elif change == 'version':
        value['version'] = 'unsupported'
    else:
        value['requirements'] = []
    with pytest.raises(AnalysisError) as error:
        analyze_script(project, SCRIPT, breakdown=value)
    assert error.value.code == 'invalid_script_breakdown'
    assert not (project / 'analysis' / 'script_breakdown.json').exists()


def test_provider_interface_and_failure(project):
    provider = Mock()
    provider.analyze.return_value = LocalScriptParser().analyze(SCRIPT)
    result = analyze_script(project, 'Free prose understood by a future model.', provider=provider)
    provider.analyze.assert_called_once_with('Free prose understood by a future model.')
    provider.analyze.side_effect = TimeoutError('model timeout')
    with pytest.raises(AnalysisError) as error:
        analyze_script(project, SCRIPT, provider=provider)
    assert error.value.code == 'script_analysis_failed'
    assert get_script_breakdown(project) == result


def test_missing_corrupt_and_write_failures(project, monkeypatch):
    with pytest.raises(AnalysisError) as error:
        get_script_breakdown(project)
    assert error.value.code == 'script_breakdown_not_found'
    result = analyze_script(project, SCRIPT)
    saved = Path(result['breakdown_path'])
    before = saved.read_bytes()
    def fail(*args):
        raise PermissionError('locked')
    monkeypatch.setattr(Path, 'replace', fail)
    with pytest.raises(AnalysisError) as error:
        analyze_script(project, SCRIPT)
    assert error.value.code == 'script_write_failed'
    assert saved.read_bytes() == before
    assert list(saved.parent.iterdir()) == [saved]
    saved.write_text('broken')
    with pytest.raises(AnalysisError) as error:
        get_script_breakdown(project)
    assert error.value.code == 'script_read_failed'


def test_mcp_script_tools(project):
    server = build_server(project.parent)
    async def exercise():
        async def call(name, args):
            result = await server.call_tool(name, args)
            return result[1] if isinstance(result, tuple) else result
        created = await call('analyze_script', {'project': 'Drama', 'script': SCRIPT})
        assert created['success']
        read = await call('get_script_breakdown', {'project': 'Drama'})
        assert read == created
        failed = await call('analyze_script', {'project': 'Drama', 'script': ''})
        assert failed['error']['code'] == 'invalid_script'
        assert (await call('ping', {}))['success']
    asyncio.run(exercise())
