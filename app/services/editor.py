"""
成片时间线编辑器的后端：把已完成任务的产物还原成可编辑工程，保存编辑并重新渲染。

一条成片由两步产物组成：``combined-N.mp4`` 是只有画面的素材轨，
``video.generate_video`` 再叠加旁白、字幕和背景音乐得到 ``final-N.mp4``。
编辑器只重做这两步：字幕和音乐的修改只需要重跑第二步；片段的增删、排序、
裁剪才需要先用 FFmpeg 重建素材轨。文案、关键词和配音全部复用原任务产物，
因此重新渲染不会再调用大模型或 TTS，也不会产生额外的 API 费用。

工程文件保存在任务目录的 ``editor-N.json``；浏览器只拿到素材的 id，
真实文件路径始终由服务端维护，编辑请求中的未知 id 会被拒绝。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import uuid
from typing import Any, Callable

from loguru import logger

from app.models.schema import VideoAspect, VideoFitMode, VideoParams
from app.services import task_artifacts
from app.services import video as video_service
from app.utils import utils

PROJECT_VERSION = 1
EDITOR_DIR_NAME = "editor"
MIN_CLIP_SECONDS = 0.3
MIN_SUBTITLE_SECONDS = 0.2
MIN_MUSIC_SECONDS = 0.5
MAX_SUBTITLE_CHARS = 500
MAX_CLIPS = 400
MAX_SUBTITLES = 2000
MUSIC_KEEP = "keep"
MUSIC_NONE = "none"
MUSIC_SONG = "song"
_SRT_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")
_DURATION = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_render_lock = threading.Lock()


class EditorError(Exception):
    """编辑器可展示给用户的错误。"""


# ---------------------------------------------------------------------------
# 路径与探测
# ---------------------------------------------------------------------------


def _task_path(task_id: str, *parts: str) -> str:
    return os.path.join(utils.task_dir(task_id), *parts)


def editor_dir(task_id: str) -> str:
    directory = _task_path(task_id, EDITOR_DIR_NAME)
    os.makedirs(directory, exist_ok=True)
    return directory


def project_file(task_id: str, video_index: int) -> str:
    return _task_path(task_id, f"editor-{int(video_index)}.json")


def edited_video_file(task_id: str, video_index: int) -> str:
    return _task_path(task_id, f"final-{int(video_index)}-edited.mp4")


def _ffmpeg() -> str:
    return video_service.get_ffmpeg_binary()


def probe_duration(file_path: str) -> float:
    """用 ``ffmpeg -i`` 读取媒体时长；只依赖 FFmpeg，不要求 ffprobe。"""
    result = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-i", file_path],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    match = _DURATION.search(result.stderr or "")
    if not match:
        raise EditorError(f"cannot read media duration: {os.path.basename(file_path)}")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _run_ffmpeg(arguments: list[str]) -> None:
    result = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise EditorError(f"ffmpeg failed: {(result.stderr or '').strip()[-400:]}")


def available_video_indexes(task_id: str) -> list[int]:
    """返回可编辑的成片序号：成片、素材轨和旁白都在才可编辑。"""
    if not os.path.isfile(_task_path(task_id, "audio.mp3")):
        return []
    indexes = []
    for index in range(1, 51):
        if os.path.isfile(_task_path(task_id, f"final-{index}.mp4")) and os.path.isfile(
            _task_path(task_id, f"combined-{index}.mp4")
        ):
            indexes.append(index)
    return indexes


# ---------------------------------------------------------------------------
# 字幕
# ---------------------------------------------------------------------------


def _parse_srt_time(value: str) -> float:
    match = _SRT_TIME.search(value)
    if not match:
        raise ValueError(f"invalid srt time: {value}")
    hours, minutes, seconds, millis = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis.ljust(3, "0")) / 1000


def parse_srt(text: str) -> list[dict[str, Any]]:
    subtitles = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        lines = [line for line in block.splitlines() if line.strip()]
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        start_text, end_text = lines[timing_index].split("-->", 1)
        try:
            start, end = _parse_srt_time(start_text), _parse_srt_time(end_text)
        except ValueError:
            continue
        content = "\n".join(lines[timing_index + 1 :]).strip()
        if content and end > start:
            subtitles.append({"start": round(start, 3), "end": round(end, 3), "text": content})
    return subtitles


def _format_srt_time(seconds: float) -> str:
    millis = int(round(max(0.0, seconds) * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def format_srt(subtitles: list[dict[str, Any]]) -> str:
    blocks = []
    ordered = sorted(subtitles, key=lambda item: item["start"])
    for number, item in enumerate(ordered, start=1):
        blocks.append(
            f"{number}\n{_format_srt_time(item['start'])} --> "
            f"{_format_srt_time(item['end'])}\n{item['text']}\n"
        )
    return "\n".join(blocks)


# ---------------------------------------------------------------------------
# 工程：创建、读取、校验、保存
# ---------------------------------------------------------------------------


def _new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def _source_id(file_path: str) -> str:
    return "s" + hashlib.sha1(os.path.abspath(file_path).encode("utf-8")).hexdigest()[:12]


def _load_task_params(task_id: str) -> dict[str, Any]:
    try:
        with open(_task_path(task_id, "script.json"), encoding="utf-8") as file:
            payload = json.load(file)
    except (OSError, ValueError) as exc:
        raise EditorError(f"task settings are missing: {exc}") from exc
    params = payload.get("params") if isinstance(payload, dict) else None
    if not isinstance(params, dict):
        raise EditorError("task settings are missing")
    return payload


def _register_source(sources: dict, file_path: str, label: str) -> str:
    source_id = _source_id(file_path)
    if source_id not in sources:
        sources[source_id] = {
            "path": os.path.abspath(file_path),
            "label": label,
            "duration": round(probe_duration(file_path), 3),
        }
    return source_id


def _initial_clips(task_id: str, video_index: int, script_payload: dict, sources: dict) -> list:
    """优先使用合成时记录的真实片段；旧任务没有记录时按片段时长等分素材轨。"""
    combined = _task_path(task_id, f"combined-{video_index}.mp4")
    segments = (script_payload.get("video_segments") or {}).get(str(video_index))
    clips = []
    if isinstance(segments, list) and segments:
        try:
            for segment in segments:
                # 合成时为安全余量多拼的尾段可能只有几十毫秒，不作为可编辑片段。
                if float(segment["out"]) - float(segment["in"]) < MIN_CLIP_SECONDS:
                    continue
                source_path = segment["source"]
                if not os.path.isfile(source_path):
                    raise FileNotFoundError(source_path)
                source_id = _register_source(
                    sources, source_path, os.path.basename(source_path)
                )
                clips.append(
                    {
                        "id": _new_id("c"),
                        "source": source_id,
                        "in": round(float(segment["in"]), 3),
                        "out": round(float(segment["out"]), 3),
                        "speed": float(segment.get("speed") or 1.0),
                    }
                )
            return clips
        except (KeyError, TypeError, ValueError, OSError, EditorError) as exc:
            logger.warning(f"editor: recorded segments unusable, splitting instead: {exc}")
            clips = []

    source_id = _register_source(sources, combined, "Original video")
    total = sources[source_id]["duration"]
    params = script_payload.get("params") or {}
    step = max(1.0, float(params.get("video_clip_duration") or 5))
    start = 0.0
    while start < total - 0.05:
        end = min(total, start + step)
        clips.append(
            {"id": _new_id("c"), "source": source_id, "in": round(start, 3),
             "out": round(end, 3), "speed": 1.0}
        )
        start = end
    return clips


def create_project(task_id: str, video_index: int) -> dict[str, Any]:
    if video_index not in available_video_indexes(task_id):
        raise EditorError("this video cannot be edited")
    script_payload = _load_task_params(task_id)
    params = script_payload["params"]
    sources: dict[str, dict] = {}
    clips = _initial_clips(task_id, video_index, script_payload, sources)
    # 原始素材轨也作为素材库条目，方便把剪掉的段落找回来。
    _register_source(sources, _task_path(task_id, f"combined-{video_index}.mp4"), "Original video")

    subtitles = []
    subtitle_path = _task_path(task_id, "subtitle.srt")
    if os.path.isfile(subtitle_path):
        with open(subtitle_path, encoding="utf-8", errors="replace") as file:
            subtitles = parse_srt(file.read())
    for item in subtitles:
        item["id"] = _new_id("t")

    project = {
        "version": PROJECT_VERSION,
        "task_id": task_id,
        "video_index": int(video_index),
        "duration": round(probe_duration(_task_path(task_id, "audio.mp3")), 3),
        "sources": sources,
        "clips": clips,
        "original_clips": copy.deepcopy(clips),
        "subtitles": subtitles,
        "subtitles_enabled": bool(params.get("subtitle_enabled", True)),
        "music": {
            "mode": MUSIC_KEEP,
            "song": "",
            "volume": float(params.get("bgm_volume") if params.get("bgm_volume") is not None else 0.2),
            # 音乐在时间线上的区间，以及从歌曲的第几秒开始播放。
            "start": 0.0,
            "end": round(probe_duration(_task_path(task_id, "audio.mp3")), 3),
            "offset": 0.0,
        },
        "voice_volume": float(params.get("voice_volume") or 1.0),
    }
    return project


def load_project(task_id: str, video_index: int) -> dict[str, Any]:
    path = project_file(task_id, video_index)
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as file:
                project = json.load(file)
            if project.get("version") == PROJECT_VERSION:
                # 只信任仍然存在的素材文件。
                project["sources"] = {
                    key: value
                    for key, value in (project.get("sources") or {}).items()
                    if isinstance(value, dict) and os.path.isfile(str(value.get("path", "")))
                }
                return validate_edits(project, project)
        except (OSError, ValueError, EditorError) as exc:
            logger.warning(f"editor: saved project unusable, rebuilding: {exc}")
    return create_project(task_id, video_index)


def _number(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise EditorError(f"invalid {name}") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise EditorError(f"invalid {name}")
    return number


def validate_edits(project: dict[str, Any], edits: dict[str, Any]) -> dict[str, Any]:
    """
    把浏览器提交的编辑合并进服务端工程。

    只接受片段、字幕、音乐和音量；素材路径、任务 id 等字段始终取自服务端。
    片段必须引用已登记的素材，入点/出点会被夹在素材时长之内。
    """
    sources = project["sources"]
    result = copy.deepcopy(project)

    clips = []
    for raw in (edits.get("clips") or [])[:MAX_CLIPS]:
        if not isinstance(raw, dict) or raw.get("source") not in sources:
            raise EditorError("a clip refers to an unknown source")
        source_duration = sources[raw["source"]]["duration"]
        start = min(max(0.0, _number(raw.get("in"), "clip start")), source_duration)
        end = min(max(0.0, _number(raw.get("out"), "clip end")), source_duration)
        if end - start < MIN_CLIP_SECONDS:
            continue
        speed = min(4.0, max(0.25, _number(raw.get("speed", 1.0), "clip speed")))
        clips.append(
            {"id": str(raw.get("id") or _new_id("c"))[:40], "source": raw["source"],
             "in": round(start, 3), "out": round(end, 3), "speed": speed}
        )
    if not clips:
        raise EditorError("the timeline needs at least one clip")
    result["clips"] = clips

    duration = project["duration"]
    subtitles = []
    for raw in (edits.get("subtitles") or [])[:MAX_SUBTITLES]:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()[:MAX_SUBTITLE_CHARS]
        start = min(max(0.0, _number(raw.get("start"), "subtitle start")), duration)
        end = min(max(0.0, _number(raw.get("end"), "subtitle end")), duration)
        if not text or end - start < MIN_SUBTITLE_SECONDS:
            continue
        subtitles.append(
            {"id": str(raw.get("id") or _new_id("t"))[:40], "start": round(start, 3),
             "end": round(end, 3), "text": text}
        )
    result["subtitles"] = sorted(subtitles, key=lambda item: item["start"])
    result["subtitles_enabled"] = bool(edits.get("subtitles_enabled", project.get("subtitles_enabled", True)))

    music = edits.get("music") or project.get("music") or {}
    mode = music.get("mode") if music.get("mode") in (MUSIC_KEEP, MUSIC_NONE, MUSIC_SONG) else MUSIC_KEEP
    song = os.path.basename(str(music.get("song") or ""))
    if mode == MUSIC_SONG and not (song and os.path.isfile(os.path.join(utils.song_dir(), song))):
        raise EditorError("the selected song does not exist")
    previous = project.get("music") or {}
    start = min(max(0.0, _number(music.get("start", previous.get("start", 0.0)), "music start")), duration)
    end = min(
        max(0.0, _number(music.get("end", previous.get("end", duration)), "music end")), duration
    )
    if end - start < MIN_MUSIC_SECONDS:
        start, end = 0.0, duration
    result["music"] = {
        "mode": mode,
        "song": song if mode == MUSIC_SONG else "",
        "volume": min(2.0, max(0.0, _number(music.get("volume", 0.2), "music volume"))),
        "start": round(start, 3),
        "end": round(end, 3),
        "offset": round(
            max(0.0, _number(music.get("offset", previous.get("offset", 0.0)), "music offset")), 3
        ),
    }
    result["voice_volume"] = min(
        2.0, max(0.0, _number(edits.get("voice_volume", project.get("voice_volume", 1.0)), "voice volume"))
    )
    return result


def save_project(task_id: str, video_index: int, project: dict[str, Any]) -> None:
    path = project_file(task_id, video_index)
    temporary = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(project, file, ensure_ascii=False, indent=1)
    os.replace(temporary, path)


def reset_project(task_id: str, video_index: int) -> dict[str, Any]:
    path = project_file(task_id, video_index)
    if os.path.isfile(path):
        os.remove(path)
    return create_project(task_id, video_index)


def add_source(task_id: str, video_index: int, project: dict, file_name: str, data: bytes) -> dict:
    """把用户上传的视频加入素材库，返回更新后的工程。"""
    extension = os.path.splitext(file_name)[1].lower()
    if extension not in {".mp4", ".mov", ".m4v", ".webm", ".mkv"}:
        raise EditorError("unsupported video file")
    uploads = os.path.join(editor_dir(task_id), "uploads")
    os.makedirs(uploads, exist_ok=True)
    digest = hashlib.sha1(data).hexdigest()[:12]
    target = os.path.join(uploads, f"{digest}{extension}")
    if not os.path.isfile(target):
        with open(target, "wb") as file:
            file.write(data)
    result = copy.deepcopy(project)
    try:
        _register_source(result["sources"], target, os.path.basename(file_name)[:80])
    except EditorError:
        os.remove(target)
        raise
    return result


def list_songs() -> list[str]:
    directory = utils.song_dir()
    if not os.path.isdir(directory):
        return []
    return sorted(
        name for name in os.listdir(directory)
        if os.path.splitext(name)[1].lower() in {".mp3", ".wav", ".m4a", ".ogg", ".flac"}
    )


# ---------------------------------------------------------------------------
# 预览素材：低分辨率代理与缩略图
# ---------------------------------------------------------------------------


def preview_file(task_id: str, source: dict) -> str:
    """生成 360p、无音轨的预览代理，供浏览器流畅切换和拖动。

    优先输出 VP9/WebM：开源 Chromium 等不含 H.264 解码器的浏览器也能播放；
    FFmpeg 不支持 VP9 时退回 H.264/MP4。返回值的扩展名决定 MIME 类型。
    """
    key = hashlib.sha1(source["path"].encode("utf-8")).hexdigest()[:16]
    scale = "scale='if(gt(iw,ih),-2,360)':'if(gt(iw,ih),360,-2)'"
    attempts = (
        (".webm", ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8",
                   "-row-mt", "1", "-b:v", "0", "-crf", "40", "-g", "15"]),
        (".mp4", ["-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-g", "15",
                  "-movflags", "+faststart"]),
    )
    for extension, _codec in attempts:
        cached = os.path.join(editor_dir(task_id), f"proxy-{key}{extension}")
        if os.path.isfile(cached):
            return cached
    last_error = None
    for extension, codec in attempts:
        target = os.path.join(editor_dir(task_id), f"proxy-{key}{extension}")
        staged = f"{target}.{uuid.uuid4().hex}{extension}"
        try:
            _run_ffmpeg(["-i", source["path"], "-an", "-vf", scale, *codec, staged])
            os.replace(staged, target)
            return target
        except EditorError as exc:
            last_error = exc
            if os.path.isfile(staged):
                os.remove(staged)
    raise last_error


def thumbnail_file(task_id: str, source: dict, at_seconds: float) -> str:
    at_seconds = max(0.0, min(float(at_seconds), max(0.0, source["duration"] - 0.05)))
    key = hashlib.sha1(f"{source['path']}|{at_seconds:.2f}".encode("utf-8")).hexdigest()[:16]
    target = os.path.join(editor_dir(task_id), f"thumb-{key}.jpg")
    if not os.path.isfile(target):
        _run_ffmpeg([
            "-ss", f"{at_seconds:.2f}", "-i", source["path"], "-frames:v", "1",
            "-vf", "scale=-2:96", "-q:v", "6", target,
        ])
    return target


# ---------------------------------------------------------------------------
# 背景音乐
# ---------------------------------------------------------------------------


def music_file(task_id: str, video_index: int, music: dict) -> str:
    """
    返回当前音乐设置对应的音频文件；无法确定时返回空字符串。

    “保留原音乐”依次查找：配乐服务生成的文件、合成时记录的实际文件
    （script.json 的 bgm_files）、任务指定的曲库歌曲。旧任务的随机音乐没有
    记录，无法找回原曲，此时音乐条不能移动，渲染沿用原始随机规则。
    """
    if music.get("mode") == MUSIC_NONE:
        return ""
    if music.get("mode") == MUSIC_SONG:
        path = os.path.join(utils.song_dir(), os.path.basename(music.get("song") or ""))
        return path if os.path.isfile(path) else ""
    task_dir = utils.task_dir(task_id)
    for name in sorted(os.listdir(task_dir)):
        if name.startswith((f"sonilo-bgm-{video_index}.", f"elevenlabs-bgm-{video_index}.")):
            return os.path.join(task_dir, name)
    try:
        payload = _load_task_params(task_id)
    except EditorError:
        return ""
    recorded = (payload.get("bgm_files") or {}).get(str(video_index))
    if isinstance(recorded, str) and os.path.isfile(recorded):
        return recorded
    params = payload["params"]
    if params.get("bgm_type") == "preset" and params.get("bgm_file"):
        path = os.path.join(utils.song_dir(), os.path.basename(params["bgm_file"]))
        if os.path.isfile(path):
            return path
    return ""


def _place_music(source: str, music: dict, duration: float, output: str) -> None:
    """生成与成片等长的音乐轨：区间前后静音，歌曲从 offset 开始，不够长则循环。"""
    start = float(music.get("start", 0.0))
    length = max(MIN_MUSIC_SECONDS, float(music.get("end", duration)) - start)
    fade = min(1.0, length / 2)
    audio_filter = (
        f"atrim=start={float(music.get('offset', 0.0)):.3f}:duration={length:.3f},"
        f"asetpts=PTS-STARTPTS,afade=t=out:st={length - fade:.3f}:d={fade:.3f},"
        f"adelay={int(round(start * 1000))}:all=1,apad=whole_dur={duration:.3f}"
    )
    # 输出 WAV：VBR MP3 中的长段静音会让时长和定位估算失准。
    _run_ffmpeg([
        "-stream_loop", "-1", "-i", source, "-vn", "-af", audio_filter,
        "-ac", "2", "-ar", "44100", "-t", f"{duration:.3f}", output,
    ])


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def _clips_unchanged(project: dict) -> bool:
    def signature(clips):
        return [(c["source"], c["in"], c["out"], c.get("speed", 1.0)) for c in clips]

    return signature(project["clips"]) == signature(project.get("original_clips") or [])


def _canvas_filter(width: int, height: int, fit_mode: str, speed: float) -> str:
    if fit_mode == VideoFitMode.contain.value:
        scale = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    else:
        scale = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        )
    speed_filter = f"setpts=PTS/{speed:.4f}," if abs(speed - 1.0) > 1e-3 else ""
    return f"{speed_filter}{scale},fps=30,setsar=1,format=yuv420p"


def _build_visual_track(
    task_id: str, project: dict, params: VideoParams, progress: Callable[[float, str], None]
) -> str:
    """按时间线顺序裁剪、缩放并拼接片段；总长不足旁白时循环片段补足。"""
    if _clips_unchanged(project):
        return _task_path(task_id, f"combined-{project['video_index']}.mp4")

    width, height = VideoAspect(params.video_aspect).to_resolution()
    fit_mode = VideoFitMode(params.video_fit_mode or VideoFitMode.cover).value
    work_dir = editor_dir(task_id)
    target_duration = project["duration"]
    clip_files: list[str] = []
    rendered: dict[str, str] = {}
    try:
        ordered = list(project["clips"])
        total = sum((c["out"] - c["in"]) / c.get("speed", 1.0) for c in ordered)
        sequence = list(ordered)
        while total < target_duration and ordered:
            for clip in ordered:
                sequence.append(clip)
                total += (clip["out"] - clip["in"]) / clip.get("speed", 1.0)
                if total >= target_duration:
                    break
        for number, clip in enumerate(sequence, start=1):
            progress(0.05 + 0.5 * number / len(sequence), f"clip {number}/{len(sequence)}")
            cache_key = f"{clip['source']}|{clip['in']}|{clip['out']}|{clip.get('speed', 1.0)}"
            if cache_key in rendered:
                clip_files.append(rendered[cache_key])
                continue
            source = project["sources"][clip["source"]]
            with tempfile.NamedTemporaryFile(
                dir=work_dir, prefix="edit-clip-", suffix=".mp4", delete=False
            ) as temporary:
                clip_file = temporary.name
            _run_ffmpeg([
                "-ss", f"{clip['in']:.3f}", "-to", f"{clip['out']:.3f}", "-i", source["path"],
                "-an", "-vf", _canvas_filter(width, height, fit_mode, clip.get("speed", 1.0)),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", clip_file,
            ])
            rendered[cache_key] = clip_file
            clip_files.append(clip_file)

        output = os.path.join(work_dir, f"combined-{project['video_index']}-edited.mp4")
        progress(0.6, "joining clips")
        video_service.concat_video_clips_with_ffmpeg(
            clip_files=clip_files,
            output_file=output,
            threads=max(1, int(params.n_threads or 2)),
            output_dir=work_dir,
            max_duration=target_duration,
        )
        return output
    finally:
        video_service.delete_files(list(rendered.values()))


def render_project(
    task_id: str,
    video_index: int,
    edits: dict[str, Any],
    progress: Callable[[float, str], None] | None = None,
) -> str:
    """校验并保存编辑，然后输出 ``final-N-edited.mp4``；原成片保持不变。"""
    progress = progress or (lambda _fraction, _message: None)
    if not _render_lock.acquire(blocking=False):
        raise EditorError("another edit is already rendering")
    try:
        project = validate_edits(load_project(task_id, video_index), edits)
        save_project(task_id, video_index, project)
        payload = _load_task_params(task_id)
        params = VideoParams(**payload["params"])
        params.voice_volume = project["voice_volume"]
        params.subtitle_enabled = bool(project["subtitles_enabled"] and project["subtitles"])

        progress(0.02, "preparing clips")
        visual_track = _build_visual_track(task_id, project, params, progress)

        subtitle_path = ""
        if params.subtitle_enabled:
            subtitle_path = os.path.join(editor_dir(task_id), f"subtitle-{video_index}-edited.srt")
            with open(subtitle_path, "w", encoding="utf-8") as file:
                file.write(format_srt(project["subtitles"]))

        bgm_override = None
        music = project["music"]
        source = music_file(task_id, video_index, music)
        if music["mode"] == MUSIC_NONE:
            params.bgm_volume = 0.0
            bgm_override = ""
        elif source:
            # 已知音乐文件时按时间线区间预先排好整条音乐轨，渲染层不再循环。
            progress(0.65, "placing music")
            bgm_override = os.path.join(editor_dir(task_id), f"music-{video_index}-edited.wav")
            _place_music(source, music, project["duration"], bgm_override)
            params.bgm_type = params.bgm_type or "preset"
            params.bgm_volume = music["volume"]
        else:
            params.bgm_volume = music["volume"]

        progress(0.7, "adding voice, subtitles and music")
        output = edited_video_file(task_id, video_index)
        staged = os.path.join(editor_dir(task_id), f"render-{uuid.uuid4().hex}.mp4")
        video_service.generate_video(
            video_path=visual_track,
            audio_path=_task_path(task_id, "audio.mp3"),
            subtitle_path=subtitle_path,
            output_file=staged,
            params=params,
            bgm_file_override=bgm_override,
        )
        os.replace(staged, output)
        task_artifacts.patch_script_data(
            task_id, edited_videos={str(video_index): os.path.basename(output)}
        )
        progress(1.0, "done")
        logger.info(f"editor: rendered {output}")
        return output
    finally:
        _render_lock.release()
