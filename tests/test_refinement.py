import asyncio
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from analysis.provider import SuppliedAnalysisProvider
from engine.ffmpeg import run_ffmpeg
from engine.media import scan_folder, write_source_index, probe_media
from engine.render import render_timeline
from engine.timeline import load_timeline, save_timeline
from schemas.timeline import MusicTrack, MusicClip, VideoClip, VideoTrack
from mcp_server import build_server
from services.analysis_service import AnalysisError, analyze_source
from services.project_service import create_project
from services.script_service import analyze_script
from services.refinement_service import plan_edit_refinement, apply_edit_refinement


@pytest.fixture(scope='module')
def footage(tmp_path_factory):
    folder = tmp_path_factory.mktemp('refinement footage')
    for i, color in enumerate(['red', 'blue', 'green']):
        run_ffmpeg(['-n','-f','lavfi','-i',f'color=c={color}:s=160x120:r=24','-f','lavfi','-i',
            f'sine=frequency={440 + i * 440}:sample_rate=48000','-t','3','-c:v','mpeg4','-threads','1','-c:a','aac',str(folder / f'{i}.mp4')])
    return folder


@pytest.fixture
def project(tmp_path, footage):
    result = create_project('Refine', footage, projects_root=tmp_path / 'projects')
    folder = result.project_path
    meta = json.loads((folder / 'project.json').read_text())
    meta['resolution'] = dict(width=160,height=120)
    (folder / 'project.json').write_text(json.dumps(meta))
    scan = scan_folder(footage)
    write_source_index(folder, scan)
    timeline = load_timeline(folder)
    timeline.width, timeline.height = 160,120
    clips = []
    decisions = []
    for i, source in enumerate(scan['sources']):
        color = ['red','blue','green'][i]
        analyze_source(folder, source['id'], provider=SuppliedAnalysisProvider(dict(source_id=source['id'],
            characters=['Mai'], location='room', shot_size='medium', camera_angle=None,camera_motion=None,
            action=f'{color} room', emotion=None,dialogue=None, visual_quality='sharp',continuity_notes=[],
            usable_start=.5,usable_end=2,problems=[],description=None)))
        clips.append(VideoClip(id=f'clip-{i}', source=source['path'],source_in=.5,source_out=2,timeline_start=i*1.5))
        decisions.append(dict(scene_id=f'scene_{i+1:03d}',selections=[dict(clip_id=f'clip-{i}',source_id=source['id'],
            source_in=.5,source_out=2,timeline_start=i*1.5)]))
    timeline.video_tracks = [VideoTrack(id='video',clips=clips)]
    save_timeline(folder,timeline)
    (folder / 'analysis' / 'rough_cut_report.json').write_text(json.dumps(dict(decisions=decisions)))
    analyze_script(folder, '\n\n'.join(f'SCENE {i+1}\nAction: {color} room\nDuration: 1.5' for i,color in enumerate(['red','blue','green'])))
    return folder


def set_note(folder, index, note):
    path = folder / 'analysis' / 'script_breakdown.json'
    data = json.loads(path.read_text())
    data['requirements'][index]['notes'].append(note)
    path.write_text(json.dumps(data))


def set_dialogue(folder, index):
    index_data = json.loads((folder / 'source_index.json').read_text())
    path = folder / 'analysis' / f"{index_data['sources'][index]['id']}.json"
    data = json.loads(path.read_text())
    data['dialogue'] = 'Please listen.'
    path.write_text(json.dumps(data))


def frames(path):
    result = subprocess.run(['ffmpeg','-v','error','-i',str(path),'-an','-f','framemd5','-'],
        capture_output=True,check=True,timeout=30)
    return [line for line in result.stdout.splitlines() if not line.startswith(b'#')]


def test_default_is_hard_cut_and_planning_is_read_only(project):
    before = (project / 'timeline.json').read_bytes()
    plan = plan_edit_refinement(project)
    assert all(d['type']=='hard_cut' for d in plan['plan']['decisions'])
    assert plan['plan']['after_duration'] == 4.5
    assert (project / 'timeline.json').read_bytes()==before
    applied=apply_edit_refinement(project)
    assert Path(applied['backup_path']).read_bytes()==before
    assert all(t.type=='cut' for t in load_timeline(project).video_tracks[0].transitions)
    assert not list((project / 'cache').iterdir())


@pytest.mark.parametrize('kind,note', [('crossfade','Memory montage; dissolve'),('fade_to_black','Next day; clear time jump')])
def test_real_before_after_visual_previews(project,kind,note):
    before=render_timeline(project)
    before_copy=project / 'preview' / 'before.mp4'
    shutil.copyfile(before,before_copy)
    original=(project / 'timeline.json').read_bytes()
    set_note(project,1,note)
    plan=plan_edit_refinement(project)['plan']
    assert plan['decisions'][0]['type']==kind
    assert plan['decisions'][1]['type']=='hard_cut'
    assert plan['after_duration'] < plan['before_duration']
    applied=apply_edit_refinement(project)
    assert Path(applied['backup_path']).read_bytes()==original
    after=render_timeline(project)
    assert probe_media(before_copy)['duration']==pytest.approx(4.5,abs=.07)
    info=probe_media(after)
    assert info['video_codec']=='h264' and info['audio_codec']=='aac'
    assert info['duration']==pytest.approx(plan['after_duration'],abs=.07)
    assert load_timeline(project).video_tracks[0].transitions[0].type==kind


@pytest.mark.parametrize('kind,index,note', [('j_cut',1,'J-cut: incoming dialogue over reaction'),('l_cut',0,'L-cut: outgoing dialogue over reaction')])
def test_real_dialogue_before_after_preserves_video_frames(project,kind,index,note):
    before=render_timeline(project)
    before_frames=frames(before)
    set_dialogue(project,index)
    set_note(project,index,note)
    timeline_bytes=(project / 'timeline.json').read_bytes()
    source_hashes={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(load_timeline(project).video_tracks[0].clips[0].source).parent.glob('*.mp4')}
    plan=plan_edit_refinement(project)['plan']
    assert plan['decisions'][0]['type']==kind
    assert plan['after_duration']==4.5
    assert any('untimed' in w for w in plan['warnings'])
    apply_edit_refinement(project)
    timeline=load_timeline(project)
    clip=timeline.video_tracks[0].clips[1 if kind=='j_cut' else 0]
    assert clip.has_audio_offset
    if kind=='j_cut':
        assert clip.audio_source_in==pytest.approx(.3)
        assert clip.audio_timeline_start==pytest.approx(1.3)
    else:
        assert clip.audio_source_out==pytest.approx(2.2)
    after=render_timeline(project)
    assert frames(after)==before_frames
    assert probe_media(after)['duration']==pytest.approx(4.5,abs=.07)
    assert source_hashes=={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in source_hashes}


def test_budget_and_no_adjacent_effects(project):
    recommendations=[dict(from_clip=f'clip-{i}',to_clip=f'clip-{i+1}',type='crossfade',duration=.25,reason='montage') for i in range(2)]
    before=(project / 'timeline.json').read_bytes()
    with pytest.raises(AnalysisError) as error:
        plan_edit_refinement(project,recommendations=recommendations)
    assert error.value.code=='too_many_refinements'
    assert (project / 'timeline.json').read_bytes()==before


def test_short_audio_handles_and_existing_offsets_are_preserved(project):
    timeline=load_timeline(project)
    timeline.video_tracks[0].clips[1].source_in=0
    timeline.video_tracks[0].clips[1].source_out=1.5
    save_timeline(project,timeline)
    set_dialogue(project,1)
    set_note(project,1,'J-cut')
    plan=plan_edit_refinement(project)['plan']
    assert plan['decisions'][0]['type']=='hard_cut'
    timeline=load_timeline(project)
    timeline.video_tracks[0].clips[0].audio_source_in=.5
    timeline.video_tracks[0].clips[0].audio_source_out=2.1
    timeline.video_tracks[0].clips[0].audio_timeline_start=0
    save_timeline(project,timeline)
    assert not plan_edit_refinement(project)['plan']['decisions'][0]['enabled']


def test_stale_plan_and_disabled_recommendations(project):
    plan_edit_refinement(project,recommendations=[dict(from_clip='clip-0',to_clip='clip-1',type='j_cut',duration=.2,
        enabled=False,reason='Not approved')])
    assert not apply_edit_refinement(project)['changed']
    timeline=load_timeline(project)
    timeline.video_tracks[0].clips[0].volume=.5
    save_timeline(project,timeline)
    with pytest.raises(AnalysisError) as error:
        apply_edit_refinement(project)
    assert error.value.code=='stale_refinement_plan'


def test_visual_shifts_do_not_desynchronize_timed_audio(project):
    timeline=load_timeline(project)
    file=timeline.video_tracks[0].clips[0].source
    timeline.music_tracks=[MusicTrack(id='music',clips=[MusicClip(id='bed',file=file,source_out=3,loop=True)])]
    save_timeline(project,timeline)
    set_note(project,1,'Next day')
    assert plan_edit_refinement(project)['plan']['decisions'][0]['type']=='hard_cut'
    before=(project / 'timeline.json').read_bytes()
    with pytest.raises(AnalysisError) as error:
        plan_edit_refinement(project,recommendations=[dict(from_clip='clip-0',to_clip='clip-1',type='fade_to_black',duration=.25,reason='Time break')])
    assert error.value.code=='unsafe_timing_change'
    assert (project / 'timeline.json').read_bytes()==before


def test_invalid_plan_apply_is_atomic(project):
    plan_edit_refinement(project)
    path=project / 'analysis' / 'edit_refinement_plan.json'
    data=json.loads(path.read_text())
    data['decisions'][1]['to_clip']='missing'
    path.write_text(json.dumps(data))
    before=(project / 'timeline.json').read_bytes()
    with pytest.raises(AnalysisError):
        apply_edit_refinement(project)
    assert (project / 'timeline.json').read_bytes()==before
    assert not (project / '.timeline.lock').exists()


def test_mcp_refinement_tools(project):
    server=build_server(project.parent)
    async def exercise():
        for name in ['plan_edit_refinement','apply_edit_refinement']:
            raw=await server.call_tool(name,{'project':'Refine'})
            result=raw[1] if isinstance(raw,tuple) else raw
            assert result['success'],result
    asyncio.run(exercise())
