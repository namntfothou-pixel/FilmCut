import json
import shutil
import subprocess

import pytest

from engine.media import MediaError, probe_media, scan_folder, write_source_index
from services.project_service import create_project


def generate_video(path, size, fps, audio, codec):
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
               "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}"]
    if audio:
        command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    command += ["-t", "1", "-c:v", codec, "-threads", "1", "-pix_fmt", "yuv420p"]
    if audio:
        command += ["-c:a", "aac"]
    subprocess.run(command + [str(path)], check=True, capture_output=True, timeout=30)


@pytest.fixture(scope="module")
def videos(tmp_path_factory):
    root = tmp_path_factory.mktemp("synthetic media ü")
    nested = root / "nested"
    nested.mkdir()
    specs = [(root / "audio.MP4", "160x120", 24, True, "mpeg4"),
             (nested / "silent.mov", "320x180", 30, False, "mpeg4"),
             (nested / "silent.webm", "128x96", 25, False, "libvpx-vp9")]
    for path, size, fps, audio, codec in specs:
        generate_video(path, size, fps, audio, codec)
    (root / "ignore.txt").write_text("not a video")
    return root, specs


def test_recursive_video_detection(videos):
    root, specs = videos
    result = scan_folder(root)
    assert result["errors"] == []
    assert {item["filename"] for item in result["sources"]} == {spec[0].name for spec in specs}
    assert len({item["id"] for item in result["sources"]}) == 3
    assert scan_folder(root) == result


@pytest.mark.parametrize("index", [0, 1, 2])
def test_probe_metadata(videos, index):
    _, specs = videos
    path, size, fps, audio, codec = specs[index]
    data = probe_media(path)
    assert set(data) == {"id", "path", "filename", "duration", "width", "height", "fps",
                         "video_codec", "has_audio", "audio_codec", "sample_rate"}
    assert data["path"] == str(path.resolve())
    assert data["filename"] == path.name
    assert data["duration"] == pytest.approx(1, abs=0.1)
    assert (data["width"], data["height"]) == tuple(map(int, size.split("x")))
    assert data["fps"] == pytest.approx(fps)
    assert data["video_codec"] == ("vp9" if codec == "libvpx-vp9" else codec)
    assert data["has_audio"] is audio
    assert data["audio_codec"] == ("aac" if audio else "")
    assert data["sample_rate"] == (48000 if audio else 0)
    assert probe_media(path)["id"] == data["id"]


def test_mkv_supported(videos, tmp_path):
    _, specs = videos
    target = tmp_path / "remux.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-n", "-i", str(specs[0][0]),
                    "-c", "copy", str(target)], check=True, capture_output=True, timeout=30)
    assert probe_media(target)["width"] == 160


def test_invalid_video_and_partial_scan(tmp_path, videos):
    invalid = tmp_path / "broken.mp4"
    invalid.write_bytes(b"not a video")
    with pytest.raises(MediaError) as caught:
        probe_media(invalid)
    assert caught.value.to_dict()["code"] == "invalid_video"
    _, specs = videos
    # Reuse one of the three synthetic videos; no real footage is needed.
    shutil.copyfile(specs[1][0], tmp_path / "valid.mov")
    result = scan_folder(tmp_path)
    assert len(result["sources"]) == 1
    assert result["errors"][0]["code"] == "invalid_video"


def test_empty_folder(tmp_path):
    assert scan_folder(tmp_path) == {"sources": [], "errors": []}


@pytest.mark.parametrize("function", [probe_media, scan_folder])
def test_missing_path(tmp_path, function):
    with pytest.raises(MediaError) as caught:
        function(tmp_path / "missing.mp4")
    assert caught.value.code == "invalid_path"


def test_write_index(videos, tmp_path):
    root, _ = videos
    project = create_project("Media Test", root, projects_root=tmp_path / "projects")
    assert project.success
    timeline_before = (project.project_path / "timeline.json").read_bytes()
    result = scan_folder(root)
    destination = write_source_index(project.project_path, result)
    assert destination == project.project_path / "source_index.json"
    assert json.loads(destination.read_text(encoding="utf-8")) == {"version": 1, **result}
    assert write_source_index(project.project_path, result) == destination
    assert (project.project_path / "timeline.json").read_bytes() == timeline_before


def test_index_replace_failure_preserves_previous_file(videos, tmp_path, monkeypatch):
    root, _ = videos
    project = create_project("Failure Test", root, projects_root=tmp_path / "projects")
    destination = project.project_path / "source_index.json"
    previous = destination.read_bytes()

    def deny_replace(*args, **kwargs):
        raise PermissionError("simulated denial")

    monkeypatch.setattr(type(destination), "replace", deny_replace)
    with pytest.raises(MediaError) as caught:
        write_source_index(project.project_path, {"sources": [], "errors": []})
    assert caught.value.code == "index_write_failed"
    assert destination.read_bytes() == previous
    assert not list(project.project_path.glob(".source_index-*.tmp"))


@pytest.mark.parametrize("failure,code", [(FileNotFoundError(), "ffprobe_missing"),
    (subprocess.TimeoutExpired("ffprobe", 30), "probe_timeout")])
def test_probe_process_errors(videos, monkeypatch, failure, code):
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(MediaError) as caught:
        probe_media(videos[1][0][0])
    assert caught.value.code == code


def test_audio_only_file_rejected(tmp_path):
    path = tmp_path / "audio.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-n", "-f", "lavfi", "-i",
                    "sine=frequency=440", "-t", "0.2", "-c:a", "aac", str(path)],
                   check=True, capture_output=True, timeout=30)
    with pytest.raises(MediaError) as caught:
        probe_media(path)
    assert caught.value.code == "invalid_video"
