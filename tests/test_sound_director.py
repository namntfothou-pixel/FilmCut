import asyncio
import hashlib
import json
import math
import shutil
import struct
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from analysis.provider import SuppliedAnalysisProvider
from engine.ffmpeg import run_ffmpeg
from engine.media import probe_media, scan_folder, write_source_index
from engine.render import render_timeline
from engine.timeline import load_timeline, save_timeline
from mcp_server import build_server
from schemas.sound import MusicIntent, SFXIntent
from schemas.timeline import MusicClip, MusicTrack, VideoClip, VideoTrack
from services.analysis_service import AnalysisError, analyze_source
from services.project_service import create_project
from services.script_service import analyze_script
from services.sound_service import plan_music, plan_sfx, apply_music_plan, apply_sfx_plan


@pytest.fixture(scope='module')
def assets(tmp_path_factory):
    root = tmp_path_factory.mktemp('sound director assets')
    video = root / 'video.mp4'
    run_ffmpeg(['-n', '-f', 'lavfi', '-i', 'color=c=red:s=160x120:r=24', '-f', 'lavfi', '-i',
        'sine=frequency=440:sample_rate=48000', '-t', '2', '-c:v', 'mpeg4', '-threads', '1', '-c:a', 'aac', str(video)])
    for kind in ['music', 'sfx']:
        (root / kind).mkdir()
    for kind, name, frequency, duration in [('music', 'tense', 960, .3), ('music', 'warm', 1440, .35), ('sfx', 'impact', 1760, .15)]:
        run_ffmpeg(['-n', '-f', 'lavfi', '-i', f'sine=frequency={frequency}:sample_rate=48000',
                    '-t', str(duration), '-c:a', 'pcm_s16le', str(root / kind / f'{name}.wav')])
    (root / 'music' / 'library.json').write_text(json.dumps({'version': 1, 'items': [
        {'id': 'tense', 'file': 'tense.wav', 'tags': ['tense', 'suspense']},
        {'id': 'warm', 'file': 'warm.wav', 'tags': ['hopeful', 'warm']}]}))
    (root / 'sfx' / 'library.json').write_text(json.dumps({'version': 1, 'items': [
        {'id': 'impact', 'file': 'impact.wav', 'tags': ['body', 'impact', 'concrete', 'heavy', 'wall']}]}))
    return root


@pytest.fixture
def setup(tmp_path, assets):
    result = create_project('Sound', assets, projects_root=tmp_path / 'projects')
    folder = result.project_path
    meta = json.loads((folder / 'project.json').read_text())
    meta['resolution'] = {'width': 160, 'height': 120}
    (folder / 'project.json').write_text(json.dumps(meta))
    timeline = load_timeline(folder)
    timeline.width, timeline.height = 160, 120
    timeline.video_tracks = [VideoTrack(id='video', clips=[VideoClip(id='clip', source=str(assets / 'video.mp4'), source_out=2)])]
    save_timeline(folder, timeline)
    scanned = scan_folder(assets)
    write_source_index(folder, scanned)
    source = scanned['sources'][0]
    observations = dict(source_id=source['id'], characters=['Mai'], location='room', shot_size='medium',
        camera_angle=None, camera_motion=None, action='body hits concrete wall', emotion='anxious',
        dialogue='Please stop.', visual_quality='sharp', continuity_notes=[], usable_start=0,
        usable_end=2, problems=[], description=None)
    analyze_source(folder, source['id'], provider=SuppliedAnalysisProvider(observations))
    analyze_script(folder, 'SCENE 1\nEmotion: anxious\nAction: body hits concrete wall\nDialogue: Please stop.\nDuration: 2')
    music, sfx = tmp_path / 'music', tmp_path / 'sfx'
    shutil.copytree(assets / 'music', music)
    shutil.copytree(assets / 'sfx', sfx)
    return folder, music, sfx


def test_plans_do_not_edit_or_generate_audio(setup):
    folder, music, sfx = setup
    before = (folder / 'timeline.json').read_bytes()
    original = {p: p.read_bytes() for root in [music, sfx] for p in root.rglob('*') if p.is_file()}
    planned = plan_music(folder, library_root=music)
    decision = planned['plan']['music'][0]
    assert decision['mood'] == 'tense' and decision['energy'] == .6
    assert decision['start'] == 0 and decision['end'] == 2
    assert decision['asset_id'] == 'tense' and decision['match_score'] == 1
    assert decision['ducking']['enabled']
    sounds = plan_sfx(folder, library_root=sfx)
    event = sounds['plan']['sfx'][0]
    assert event['event'] == 'body hits concrete wall'
    assert {'body', 'impact', 'concrete', 'heavy'} <= set(event['tags'])
    assert event['timing'] == 'approximate' and event['timestamp'] == 0
    assert event['asset_id'] == 'impact'
    assert any('approximate' in warning for warning in sounds['plan']['warnings'])
    assert sounds['plan']['music'] == planned['plan']['music']
    assert (folder / 'timeline.json').read_bytes() == before
    assert original == {p: p.read_bytes() for root in [music, sfx] for p in root.rglob('*') if p.is_file()}
    assert not list((folder / 'cache').iterdir())


def exact_plans(folder, music, sfx):
    plan_music(folder, library_root=music, intents=[
        dict(mood='tense', energy=.6, start=0, end=1, recommended_tags=['tense'],
             ducking={'enabled': True}, fade_in=.1, fade_out=.1),
        dict(mood='hopeful', energy=.4, start=1, end=2, recommended_tags=['warm'],
             ducking={'enabled': False}, fade_in=.1, fade_out=.1)])
    return plan_sfx(folder, library_root=sfx, intents=[
        dict(event='body hits concrete wall', timestamp=.6, tags=['body', 'impact', 'concrete', 'heavy'], intensity=.8)])


def samples(path, start, length=.1):
    result = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-ss', str(start), '-i', str(path),
        '-t', str(length), '-vn', '-ac', '1', '-ar', '48000', '-f', 'f32le', '-'], capture_output=True, check=True, timeout=30)
    return struct.unpack(f'<{len(result.stdout)//4}f', result.stdout)


def tone(values, frequency):
    n = len(values)
    return 2 / n * math.hypot(sum(v * math.cos(2 * math.pi * frequency * i / 48000) for i, v in enumerate(values)),
                              sum(v * math.sin(2 * math.pi * frequency * i / 48000) for i, v in enumerate(values)))


def test_real_preview_music_boundaries_dialogue_and_exact_sfx(setup, assets):
    folder, music, sfx = setup
    hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in assets.rglob('*') if p.is_file()}
    baseline = render_timeline(folder)
    dialogue = tone(samples(baseline, .4), 440)
    plan = exact_plans(folder, music, sfx)
    before = (folder / 'timeline.json').read_bytes()
    applied = apply_music_plan(folder, library_root=music)
    assert Path(applied['backup_path']).read_bytes() == before
    assert apply_sfx_plan(folder, library_root=sfx)['inserted'] == 1
    timeline = load_timeline(folder)
    assert [m.end for m in timeline.music_tracks[-1].clips] == [1, 2]
    assert [m.volume_db for m in timeline.music_tracks[-1].clips] == [-24, -18]
    assert timeline.sfx_tracks[-1].clips[0].timeline_time == .6
    assert 'sound-director-' in (folder / 'timeline.json').read_text()
    output = render_timeline(folder)
    actual = probe_media(output)
    assert actual['video_codec'] == 'h264' and actual['audio_codec'] == 'aac'
    assert actual['sample_rate'] == 48000 and abs(actual['duration'] - 2) < .06
    early, late, impact, before_impact = samples(output, .4), samples(output, 1.4), samples(output, .62), samples(output, .45)
    assert tone(early, 440) == pytest.approx(dialogue, rel=.2)
    assert tone(early, 960) > .004
    assert tone(late, 960) < tone(early, 960) * .1
    assert tone(late, 1440) > .008
    assert tone(early, 1440) < tone(late, 1440) * .1
    assert tone(impact, 1760) > .015
    assert tone(before_impact, 1760) < tone(impact, 1760) * .1
    assert hashes == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in assets.rglob('*') if p.is_file()}
    assert not list((folder / 'cache').iterdir())


def test_application_is_idempotent_and_preserves_manual_audio(setup):
    folder, music, sfx = setup
    timeline = load_timeline(folder)
    timeline.music_tracks = [MusicTrack(id='manual', clips=[MusicClip(id='manual-bed',
        file=str(music / 'warm.wav'), source_out=.35, volume_db=-60, loop=True)])]
    save_timeline(folder, timeline)
    exact_plans(folder, music, sfx)
    apply_music_plan(folder, library_root=music)
    apply_sfx_plan(folder, library_root=sfx)
    before = (folder / 'timeline.json').read_bytes()
    history = list((folder / 'timeline_history').iterdir())
    assert not apply_music_plan(folder, library_root=music)['changed']
    assert not apply_sfx_plan(folder, library_root=sfx)['changed']
    assert (folder / 'timeline.json').read_bytes() == before
    assert list((folder / 'timeline_history').iterdir()) == history
    assert load_timeline(folder).music_tracks[0].clips[0].id == 'manual-bed'


def test_stale_plan_and_missing_asset_are_atomic(setup):
    folder, music, sfx = setup
    exact_plans(folder, music, sfx)
    (music / 'tense.wav').unlink()
    before = (folder / 'timeline.json').read_bytes()
    with pytest.raises(AnalysisError) as error:
        apply_music_plan(folder, library_root=music)
    assert error.value.code == 'unresolved_sound_asset'
    assert (folder / 'timeline.json').read_bytes() == before
    timeline = load_timeline(folder)
    timeline.video_tracks[0].clips[0].source_out = 1.5
    save_timeline(folder, timeline)
    with pytest.raises(AnalysisError) as error:
        apply_sfx_plan(folder, library_root=sfx)
    assert error.value.code == 'stale_sound_plan'


def test_empty_library_unresolved_and_invalid_cues(setup):
    folder, music, sfx = setup
    (music / 'library.json').write_text('{"version":1,"items":[]}')
    assert plan_music(folder, library_root=music)['plan']['music'][0]['file'] is None
    with pytest.raises(AnalysisError):
        apply_music_plan(folder, library_root=music)
    for cue in [dict(event='impact', timestamp=2, tags=['impact'], intensity=.5),
                dict(event='impact', timestamp=-1, tags=['impact'], intensity=.5)]:
        with pytest.raises(AnalysisError):
            plan_sfx(folder, library_root=sfx, intents=[cue])


@pytest.mark.parametrize('changes', [{'end':0}, {'fade_in':2}, {'energy':float('nan')}])
def test_music_intent_validation(changes):
    data = dict(mood='tense', energy=.5, start=0, end=1, recommended_tags=['tense'], fade_in=.1, fade_out=.1)
    with pytest.raises(ValidationError):
        MusicIntent(**{**data, **changes})


@pytest.mark.parametrize('changes', [{'timestamp':-1}, {'intensity':2}, {'tags':[]}])
def test_sfx_intent_validation(changes):
    data = dict(event='impact', timestamp=.5, tags=['impact'], intensity=.5)
    with pytest.raises(ValidationError):
        SFXIntent(**{**data, **changes})


def test_mcp_plan_apply_tools(setup):
    folder, music, sfx = setup
    server = build_server(folder.parent, sfx, music_library_root=music)
    async def exercise():
        async def call(name):
            raw = await server.call_tool(name, {'project':'Sound'})
            return raw[1] if isinstance(raw, tuple) else raw
        for name in ['plan_music','plan_sfx','apply_music_plan','apply_sfx_plan']:
            result = await call(name)
            assert result['success'], result
        assert Path((await call('render_preview'))['data']['preview_path']).is_file()
    asyncio.run(exercise())


def test_catalog_paths_and_changed_tags_are_rejected(setup):
    folder, music, sfx = setup
    exact_plans(folder, music, sfx)
    catalog_path = music / 'library.json'
    original = catalog_path.read_text()
    catalog = json.loads(original)
    catalog['items'][0]['tags'] = ['unrelated']
    catalog_path.write_text(json.dumps(catalog))
    with pytest.raises(AnalysisError) as error:
        apply_music_plan(folder, library_root=music)
    assert error.value.code == 'stale_sound_asset'
    catalog['items'][0]['file'] = '../escape.wav'
    catalog_path.write_text(json.dumps(catalog))
    with pytest.raises(AnalysisError) as error:
        plan_music(folder, library_root=music)
    assert error.value.code == 'invalid_music_library'


def test_failed_plan_save_preserves_previous_plan(setup, monkeypatch):
    folder, music, sfx = setup
    exact_plans(folder, music, sfx)
    path = folder / 'analysis' / 'sound_plan.json'
    before = path.read_bytes()
    def fail(*args):
        raise PermissionError('plan locked')
    monkeypatch.setattr(Path, 'replace', fail)
    with pytest.raises(AnalysisError):
        plan_sfx(folder, library_root=sfx, intents=[])
    assert path.read_bytes() == before
    assert not list(path.parent.glob('*.tmp'))


def test_exact_12_42_second_cue_is_visible_in_timeline(setup):
    folder, music, sfx = setup
    timeline = load_timeline(folder)
    source = timeline.video_tracks[0].clips[0].source
    timeline.video_tracks[0].clips = [VideoClip(id=f'clip-{i}', source=source, source_out=2,
        timeline_start=i * 2) for i in range(7)]
    save_timeline(folder, timeline)
    plan_sfx(folder, library_root=sfx, intents=[dict(event='body hits concrete wall', timestamp=12.42,
        tags=['body', 'impact', 'concrete', 'heavy'], intensity=.9)])
    apply_sfx_plan(folder, library_root=sfx)
    item = load_timeline(folder).sfx_tracks[-1].clips[0]
    assert item.timeline_time == 12.42
    assert {'body', 'impact', 'concrete', 'heavy'} <= set(item.tags)


@pytest.mark.parametrize('changes', [{'end':0}, {'end':.1,'fade_in':.2}, {'end':2,'loop':False}])
def test_music_clip_endpoint_validation(changes):
    values = dict(id='cue', file='a.wav', source_out=1, loop=True)
    with pytest.raises(ValidationError):
        MusicClip(**{**values, **changes})
