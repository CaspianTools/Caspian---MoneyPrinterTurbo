"""时间线编辑器后端：字幕解析、编辑校验与工程初始化。"""

import json
import os

import pytest

from app.services import editor


def _project(tmp_path, duration=12.0):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"")
    return {
        "version": editor.PROJECT_VERSION,
        "task_id": "t",
        "video_index": 1,
        "duration": duration,
        "sources": {"s1": {"path": str(source), "label": "clip.mp4", "duration": 8.0}},
        "clips": [{"id": "c1", "source": "s1", "in": 0.0, "out": 3.0, "speed": 1.0}],
        "original_clips": [{"id": "c1", "source": "s1", "in": 0.0, "out": 3.0, "speed": 1.0}],
        "subtitles": [],
        "subtitles_enabled": True,
        "music": {"mode": editor.MUSIC_KEEP, "song": "", "volume": 0.2},
        "voice_volume": 1.0,
    }


def test_srt_round_trip_keeps_text_and_timing():
    text = (
        "1\n00:00:00,000 --> 00:00:01,500\nHello\n\n"
        "2\n00:00:01,500 --> 00:01:02,250\nTwo\nlines\n"
    )
    subtitles = editor.parse_srt(text)
    assert subtitles == [
        {"start": 0.0, "end": 1.5, "text": "Hello"},
        {"start": 1.5, "end": 62.25, "text": "Two\nlines"},
    ]
    assert editor.parse_srt(editor.format_srt(subtitles)) == subtitles


def test_parse_srt_skips_malformed_and_empty_blocks():
    text = "1\nnot a time\nX\n\n2\n00:00:02,000 --> 00:00:01,000\nbackwards\n\n3\n00:00:03,000 --> 00:00:04,000\n\n"
    assert editor.parse_srt(text) == []


def test_validate_edits_rejects_unknown_sources(tmp_path):
    project = _project(tmp_path)
    with pytest.raises(editor.EditorError):
        editor.validate_edits(
            project, {"clips": [{"source": "/etc/passwd", "in": 0, "out": 2}]}
        )


def test_validate_edits_clamps_and_drops_invalid_items(tmp_path):
    project = _project(tmp_path)
    result = editor.validate_edits(
        project,
        {
            "clips": [
                {"id": "a", "source": "s1", "in": -5, "out": 99, "speed": 10},
                {"id": "b", "source": "s1", "in": 1.0, "out": 1.1},
            ],
            "subtitles": [
                {"id": "x", "start": 5, "end": 6, "text": "  keep  "},
                {"id": "y", "start": 1, "end": 1.05, "text": "too short"},
                {"id": "z", "start": 2, "end": 3, "text": "   "},
                {"id": "w", "start": -1, "end": 50, "text": "clamped"},
            ],
            "voice_volume": 9,
        },
    )
    assert result["clips"] == [
        {"id": "a", "source": "s1", "in": 0.0, "out": 8.0, "speed": 4.0}
    ]
    assert [(s["start"], s["end"], s["text"]) for s in result["subtitles"]] == [
        (0.0, 12.0, "clamped"),
        (5.0, 6.0, "keep"),
    ]
    assert result["voice_volume"] == 2.0
    # 服务端字段不能被浏览器提交覆盖。
    assert result["sources"] == project["sources"]
    assert result["task_id"] == "t"


def test_validate_edits_requires_a_clip(tmp_path):
    with pytest.raises(editor.EditorError):
        editor.validate_edits(_project(tmp_path), {"clips": []})


def test_validate_edits_checks_song_exists(tmp_path, monkeypatch):
    songs = tmp_path / "songs"
    songs.mkdir()
    (songs / "a.mp3").write_bytes(b"")
    monkeypatch.setattr(editor.utils, "song_dir", lambda: str(songs))
    project = _project(tmp_path)
    clips = project["clips"]
    ok = editor.validate_edits(
        project, {"clips": clips, "music": {"mode": "song", "song": "../songs/a.mp3", "volume": 0.5}}
    )
    assert ok["music"] == {
        "mode": "song", "song": "a.mp3", "volume": 0.5,
        "start": 0.0, "end": 12.0, "offset": 0.0,
    }
    with pytest.raises(editor.EditorError):
        editor.validate_edits(
            project, {"clips": clips, "music": {"mode": "song", "song": "missing.mp3"}}
        )


def test_initial_clips_use_recorded_segments_and_skip_slivers(tmp_path, monkeypatch):
    source = tmp_path / "material.mp4"
    source.write_bytes(b"")
    monkeypatch.setattr(editor, "probe_duration", lambda path: 10.0)
    payload = {
        "params": {"video_clip_duration": 3},
        "video_segments": {
            "1": [
                {"source": str(source), "in": 0, "out": 3, "speed": 1.0},
                {"source": str(source), "in": 3, "out": 3.04, "speed": 1.0},
            ]
        },
    }
    sources = {}
    clips = editor._initial_clips("task", 1, payload, sources)
    assert [(c["in"], c["out"]) for c in clips] == [(0.0, 3.0)]
    assert sources[clips[0]["source"]]["path"] == os.path.abspath(source)


def test_initial_clips_split_combined_track_for_old_tasks(tmp_path, monkeypatch):
    monkeypatch.setattr(editor.utils, "task_dir", lambda task_id="": str(tmp_path))
    (tmp_path / "combined-1.mp4").write_bytes(b"")
    monkeypatch.setattr(editor, "probe_duration", lambda path: 7.5)
    sources = {}
    clips = editor._initial_clips("task", 1, {"params": {"video_clip_duration": 3}}, sources)
    assert [(c["in"], c["out"]) for c in clips] == [(0.0, 3.0), (3.0, 6.0), (6.0, 7.5)]
    assert len(sources) == 1


def test_save_and_load_project_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(editor.utils, "task_dir", lambda task_id="": str(tmp_path))
    project = _project(tmp_path)
    editor.save_project("t", 1, project)
    with open(tmp_path / "editor-1.json", encoding="utf-8") as file:
        assert json.load(file)["clips"] == project["clips"]
    loaded = editor.load_project("t", 1)
    assert loaded["clips"] == project["clips"]


def test_validate_edits_clamps_music_region(tmp_path):
    project = _project(tmp_path, duration=10.0)
    clips = project["clips"]
    moved = editor.validate_edits(
        project,
        {"clips": clips, "music": {"mode": "keep", "start": 2, "end": 99, "offset": -3}},
    )
    assert (moved["music"]["start"], moved["music"]["end"], moved["music"]["offset"]) == (2.0, 10.0, 0.0)
    # 过短的区间回退为整条时间线。
    tiny = editor.validate_edits(
        project, {"clips": clips, "music": {"mode": "keep", "start": 4, "end": 4.1}}
    )
    assert (tiny["music"]["start"], tiny["music"]["end"]) == (0.0, 10.0)


def test_music_file_prefers_recorded_original(tmp_path, monkeypatch):
    monkeypatch.setattr(editor.utils, "task_dir", lambda task_id="": str(tmp_path))
    songs = tmp_path / "songs"
    songs.mkdir()
    (songs / "preset.mp3").write_bytes(b"")
    monkeypatch.setattr(editor.utils, "song_dir", lambda: str(songs))
    recorded = tmp_path / "random-pick.mp3"
    recorded.write_bytes(b"")
    payload = {"params": {"bgm_type": "random"}, "bgm_files": {"1": str(recorded)}}
    (tmp_path / "script.json").write_text(json.dumps(payload), encoding="utf-8")

    assert editor.music_file("t", 1, {"mode": "keep"}) == str(recorded)
    assert editor.music_file("t", 2, {"mode": "keep"}) == ""
    assert editor.music_file("t", 1, {"mode": "none"}) == ""
    assert editor.music_file("t", 1, {"mode": "song", "song": "preset.mp3"}) == str(songs / "preset.mp3")


def test_search_stock_hides_download_urls(tmp_path, monkeypatch):
    from app.config import config
    from app.models.schema import MaterialInfo
    from app.services import material

    monkeypatch.setattr(editor.utils, "task_dir", lambda task_id="": str(tmp_path))
    (tmp_path / "script.json").write_text(json.dumps({"params": {"video_subject": "x"}}), encoding="utf-8")
    monkeypatch.setitem(config.app, "pexels_api_keys", "key")
    monkeypatch.setitem(config.app, "pixabay_api_keys", "")

    def fake_search(search_term, minimum_duration, video_aspect):
        item = MaterialInfo()
        item.url = "https://videos.example/secret-token.mp4"
        item.duration = 7
        item.source_info = {"preview_image": "https://images.example/a.jpg", "creator": {"name": "Ann"}}
        return [item]

    monkeypatch.setattr(material, "search_videos_pexels", fake_search)
    assert editor.stock_providers() == ["pexels"]
    results = editor.search_stock("t", "  ocean   waves ", "pexels")
    assert results == [{
        "id": results[0]["id"], "provider": "pexels", "duration": 7.0,
        "thumb": "https://images.example/a.jpg", "page": "", "creator": "Ann",
    }]
    assert "secret-token" not in json.dumps(results)
    with pytest.raises(editor.EditorError):
        editor.search_stock("t", "ocean", "pixabay")


def test_add_stock_source_rejects_unknown_results(tmp_path):
    with pytest.raises(editor.EditorError):
        editor.add_stock_source("t", _project(tmp_path), "r-not-a-result")
