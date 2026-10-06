import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from analysis.provider import SuppliedAnalysisProvider
from engine import media, render
from engine.ffmpeg import run_ffmpeg
from engine.timeline import load_timeline, _project_context
from mcp_server import build_server
from services.analysis_service import AnalysisError, analyze_source
from services.project_service import create_project
from services.rough_cut_service import build_rough_cut, _plan
from services.script_service import analyze_script


@pytest.fixture(scope='module')
def footage(tmp_path_factory):
    root = tmp_path_factory.mktemp('rough footage')
    for name, color in [('a', 'red'), ('b', 'blue'), ('c', 'green')]:
        run_ffmpeg(['-n', '-f', 'lavfi', '-i', f'color=c={color}:s=160x120:r=24',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000',
                    '-t', '2', '-c:v', 'mpeg4', '-threads', '1', '-c:a', 'aac', str(root / f'{name}.mp4')])
    return root


@pytest.fixture
def project(tmp_path, footage):
    result = create_project('Rough', footage, projects_root=tmp_path / 'projects')
    folder = result.project_path
    metadata_path = folder / 'project.json'
    metadata = json.loads(metadata_path.read_text())
    metadata['resolution'] = {'width': 160, 'height': 120}
    metadata_path.write_text(json.dumps(metadata))
    scan = media.scan_folder(footage)
    media.write_source_index(folder, scan)
    for source in scan['sources']:
        observations = dict(source_id=source['id'], characters=['Mai'], location='room', shot_size='medium',
            camera_angle=None, camera_motion=None, action='Mai opens letter', emotion=None, dialogue=None,
            visual_quality='sharp' if source['filename'] != 'c.mp4' else 'blurry',
            continuity_notes=[], usable_start=.25, usable_end=1.75, problems=[], description=None)
        analyze_source(folder, source['id'], provider=SuppliedAnalysisProvider(observations))
    analyze_script(folder, 'SCENE 1\nAction: Mai opens letter\nDuration: 1\n\nSCENE 2\nAction: Mai opens letter\nDuration: 1')
    return folder


def test_synthetic_end_to_end_report_history_and_preview(project, footage):
    before_sources = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in footage.iterdir()}
    old_timeline = (project / 'timeline.json').read_bytes()
    result = build_rough_cut(project)
    report = result['report']
    assert report['duration'] == 2 and report['frames'] == 48
    assert [scene['scene_id'] for scene in report['decisions']] == ['scene_001', 'scene_002']
    selected = [s['selections'][0] for s in report['decisions']]
    assert selected[0]['source_id'] != selected[1]['source_id']
    assert all(s['components']['visual_quality']['score'] == 1 for s in selected)
    assert all(s['source_in'] == .25 and s['source_out'] == 1.25 for s in selected)
    assert not any(s['reused_source'] for s in selected)
    timeline = load_timeline(project)
    assert len(timeline.video_tracks[0].clips) == 2
    assert all(t.type == 'cut' and t.duration == 0 for t in timeline.video_tracks[0].transitions)
    assert not timeline.music_tracks and not timeline.sfx_tracks and not timeline.subtitle_tracks
    assert Path(report['backup_path']).read_bytes() == old_timeline
    assert report['timeline_sha256'] == hashlib.sha256((project / 'timeline.json').read_bytes()).hexdigest()
    assert json.loads(Path(result['report_path']).read_text()) == report
    actual = media.probe_media(result['preview_path'])
    assert actual['video_codec'] == 'h264' and actual['audio_codec'] == 'aac'
    assert actual['sample_rate'] == 48000 and actual['fps'] == 24
    assert abs(actual['duration'] - 2) < .08
    assert before_sources == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in footage.iterdir()}
    assert not list((project / 'cache').iterdir())
    assert not (project / '.timeline.lock').exists()


def test_fill_long_scene_with_unused_sources_before_repeating(project):
    analyze_script(project, 'SCENE 1\nAction: Mai opens letter\nDuration: 5')
    folder, metadata = _project_context(project)
    timeline, report = _plan(folder, metadata)
    selections = report['decisions'][0]['selections']
    assert len(selections) == 4
    assert len({s['source_id'] for s in selections[:3]}) == 3
    assert [s['reused_source'] for s in selections] == [False, False, False, True]
    assert [s['duration'] for s in selections] == [1.5, 1.5, 1.5, .5]
    assert timeline.duration == 5


def test_round_duration_and_usable_bounds_inward(project):
    analyze_script(project, 'SCENE 1\nAction: Mai opens letter\nDuration: 0.31')
    for path in (project / 'analysis').glob('*.json'):
        if path.name == 'script_breakdown.json':
            continue
        data = json.loads(path.read_text())
        data.update(usable_start=.26, usable_end=1.74)
        path.write_text(json.dumps(data))
    folder, metadata = _project_context(project)
    timeline, report = _plan(folder, metadata)
    clip = timeline.video_tracks[0].clips[0]
    assert clip.source_in == pytest.approx(7 / 24)
    assert clip.source_out <= 1.74
    assert report['frames'] == 7
    assert abs(report['duration'] - .31) <= 1 / 48


def test_no_eligible_sources_preserves_existing_files(project):
    for path in (project / 'analysis').glob('*.json'):
        if path.name != 'script_breakdown.json':
            path.unlink()
    before = {p: p.read_bytes() for p in project.rglob('*') if p.is_file()}
    with pytest.raises(AnalysisError) as error:
        build_rough_cut(project)
    assert error.value.code == 'no_eligible_source'
    assert before == {p: p.read_bytes() for p in project.rglob('*') if p.is_file()}


def test_no_story_evidence_does_not_select_on_quality_alone(project):
    analyze_script(project, 'SCENE 1\nAction: Horse gallops\nDuration: 1')
    with pytest.raises(AnalysisError, match='No valid analyzed source'):
        build_rough_cut(project)


def test_unknown_or_stale_ranges_are_skipped(project):
    index = json.loads((project / 'source_index.json').read_text())
    for i, record in enumerate(index['sources']):
        path = project / 'analysis' / f"{record['id']}.json"
        data = json.loads(path.read_text())
        if i == 0:
            data.update(usable_start=None, usable_end=None)
        elif i == 1:
            data['usable_end'] = 3
            record['duration'] = 4  # Stale index must not authorize an impossible media trim.
        path.write_text(json.dumps(data))
    (project / 'source_index.json').write_text(json.dumps(index))
    folder, metadata = _project_context(project)
    timeline, report = _plan(folder, metadata)
    assert {w['code'] for w in report['warnings']} == {'unknown_usable_range', 'stale_usable_range'}
    assert report['decisions'][1]['selections'][0]['reused_source']


def test_render_failure_preserves_timeline_preview_report(project, monkeypatch):
    (project / 'preview' / 'preview.mp4').write_bytes(b'previous preview')
    (project / 'analysis' / 'rough_cut_report.json').write_text('{"previous": true}')
    before = {p: p.read_bytes() for p in project.rglob('*') if p.is_file()}
    def fail(*args):
        raise render.RenderError('test_render_failed', 'Synthetic render failure')
    monkeypatch.setattr(render, 'render_timeline', fail)
    with pytest.raises(render.RenderError):
        build_rough_cut(project)
    assert before == {p: p.read_bytes() for p in project.rglob('*') if p.is_file()}
    assert not list((project / 'cache').iterdir())


def test_publish_failure_rolls_back_timeline_and_report(project, monkeypatch):
    old_timeline = (project / 'timeline.json').read_bytes()
    (project / 'preview' / 'preview.mp4').write_bytes(b'old preview')
    old_report = project / 'analysis' / 'rough_cut_report.json'
    old_report.write_text('{"previous": true}')
    replace = Path.replace
    def fail_preview(self, destination):
        if Path(destination) == project / 'preview' / 'preview.mp4':
            raise PermissionError('preview locked')
        return replace(self, destination)
    monkeypatch.setattr(Path, 'replace', fail_preview)
    with pytest.raises(AnalysisError) as error:
        build_rough_cut(project)
    assert error.value.code == 'rough_cut_publish_failed'
    assert (project / 'timeline.json').read_bytes() == old_timeline
    assert (project / 'preview' / 'preview.mp4').read_bytes() == b'old preview'
    assert old_report.read_text() == '{"previous": true}'
    assert not (project / '.timeline.lock').exists()


def test_mcp_rough_cut(project):
    server = build_server(project.parent)
    async def exercise():
        raw = await server.call_tool('build_rough_cut', {'project': 'Rough'})
        result = raw[1] if isinstance(raw, tuple) else raw
        assert result['success'], result
        assert Path(result['data']['preview_path']).is_file()
        assert result['data']['report']['duration'] == 2
        raw = await server.call_tool('build_rough_cut', {'project': 'missing'})
        error = raw[1] if isinstance(raw, tuple) else raw
        assert error['error']['code'] == 'project_not_found'
    asyncio.run(exercise())
