"""
成片编辑器页面：时间线组件、音乐与音量设置、片段上传，以及后台重新渲染。

页面状态保存在 ``st.session_state``：``editor_task_id`` 非空时主页面改为渲染
编辑器。工程读写和渲染都由 ``app.services.editor`` 完成，这里只负责界面、
媒体 URL 注册和后台任务状态展示。
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from typing import Callable

import streamlit as st
from loguru import logger
from streamlit.runtime import get_instance as get_streamlit_runtime

from app.services import editor as editor_service

_ASSET_DIR = Path(__file__).with_name("editor")
_timeline = st.components.v2.component(
    "mpt_timeline",
    css=(_ASSET_DIR / "timeline.css").read_text(encoding="utf-8"),
    js=(_ASSET_DIR / "timeline.js").read_text(encoding="utf-8"),
)

# 后台渲染状态按 (任务, 成片序号) 记录；渲染线程只写这里，不访问会话状态。
_render_jobs: dict[tuple[str, int], dict] = {}
_render_jobs_lock = threading.Lock()

_JS_LABEL_KEYS = {
    "title": "Editor Title",
    "hint": "Editor Hint",
    "play": "Editor Play",
    "split": "Editor Split",
    "delete": "Editor Delete",
    "addSubtitle": "Editor Add Subtitle",
    "undo": "Editor Undo",
    "redo": "Editor Redo",
    "zoom": "Editor Zoom",
    "subtitlesTrack": "Editor Subtitles Track",
    "clipsTrack": "Editor Clips Track",
    "library": "Editor Library",
    "add": "Editor Add To Timeline",
    "text": "Editor Text",
    "start": "Editor Start",
    "end": "Editor End",
    "sourceIn": "Editor In",
    "sourceOut": "Editor Out",
    "speed": "Editor Speed",
    "replace": "Editor Replace",
    "duplicate": "Editor Duplicate",
    "newSubtitle": "Editor New Subtitle",
    "showSubtitles": "Editor Show Subtitles",
    "clipsRepeat": "Editor Clips Repeat",
    "unsaved": "Editor Unsaved",
    "rendering": "Editor Rendering",
    "save": "Editor Save",
    "render": "Editor Render",
    "noMusic": "Editor Music None",
    "musicOffset": "Editor Music Offset",
    "musicLocked": "Editor Music Locked",
    "musicKeep": "Editor Music Keep",
    "musicSong": "Editor Music Song",
    "musicVolume": "Editor Music Volume",
    "voiceVolume": "Editor Voice Volume",
    "tools": "Editor Tools",
    "tabMedia": "Editor Tab Media",
    "tabStock": "Editor Tab Stock",
    "tabAudio": "Editor Tab Audio",
    "tabText": "Editor Tab Text",
    "mediaHint": "Editor Media Hint",
    "stockSearch": "Editor Stock Search",
    "stockButton": "Editor Stock Button",
    "stockSearching": "Editor Stock Searching",
    "stockDownloading": "Editor Stock Downloading",
    "stockEmpty": "Editor Stock Empty",
    "stockNoKey": "Editor Stock No Key",
    "stockHint": "Editor Stock Hint",
    "stockError": "Editor Stock Error",
    "useStock": "Editor Stock Use",
    "replaceStock": "Editor Replace Stock",
    "moveLeft": "Editor Move Left",
    "moveRight": "Editor Move Right",
    "resetMusic": "Editor Reset Music",
    "removeMusic": "Editor Remove Music",
    "addSubtitleHere": "Editor Add Subtitle Here",
    "playFromHere": "Editor Play From Here",
    "editText": "Editor Edit Text",
}


def open_editor(task_id: str) -> None:
    st.session_state["editor_task_id"] = task_id
    st.session_state.pop("editor_project", None)


def close_editor() -> None:
    st.session_state.pop("editor_task_id", None)
    st.session_state.pop("editor_project", None)


def is_editable(task_id: str) -> bool:
    try:
        return bool(editor_service.available_video_indexes(task_id))
    except Exception:
        return False


def _media_url(path: str, mimetype: str) -> str:
    """把本地文件注册到 Streamlit 媒体服务，返回浏览器可访问的 URL。"""
    coordinates = "mpt_editor." + hashlib.sha1(path.encode("utf-8")).hexdigest()[:16]
    return get_streamlit_runtime().media_file_mgr.add(path, mimetype, coordinates)


def _job(task_id: str, video_index: int) -> dict | None:
    with _render_jobs_lock:
        job = _render_jobs.get((task_id, video_index))
        return dict(job) if job else None


def _start_render(task_id: str, video_index: int, edits: dict) -> None:
    key = (task_id, video_index)
    with _render_jobs_lock:
        if _render_jobs.get(key, {}).get("state") == "running":
            return
        _render_jobs[key] = {"state": "running", "progress": 0.0, "message": "", "error": ""}

    def report(fraction: float, message: str) -> None:
        with _render_jobs_lock:
            _render_jobs[key].update(progress=max(0.0, min(1.0, fraction)), message=message)

    def run() -> None:
        try:
            output = editor_service.render_project(task_id, video_index, edits, report)
            with _render_jobs_lock:
                _render_jobs[key].update(state="done", progress=1.0, output=output)
        except Exception as exc:
            logger.exception(f"editor render failed: task_id={task_id}, index={video_index}")
            with _render_jobs_lock:
                _render_jobs[key].update(state="failed", error=str(exc))

    threading.Thread(target=run, name=f"editor-render-{task_id}", daemon=True).start()


def _project_state(task_id: str, video_index: int) -> dict:
    """会话内缓存工程；rev 变化时时间线组件才会丢弃未保存的修改。"""
    cached = st.session_state.get("editor_project")
    if (
        not cached
        or cached["task_id"] != task_id
        or cached["video_index"] != video_index
    ):
        project = editor_service.load_project(task_id, video_index)
        cached = {
            "task_id": task_id,
            "video_index": video_index,
            "project": project,
            "rev": int(st.session_state.get("editor_rev", 0)) + 1,
        }
        st.session_state["editor_rev"] = cached["rev"]
        st.session_state["editor_project"] = cached
    return cached


def _music_preview(task_id: str, video_index: int, project: dict, tr: Callable) -> dict:
    """返回音乐轨的预览数据；只有找得到音乐文件时音乐条才能拖动。"""
    music = project["music"]
    duration = project["duration"]
    region = {
        "start": float(music.get("start", 0.0)),
        "end": float(music.get("end", duration)),
        "offset": float(music.get("offset", 0.0)),
    }
    preview = {"musicUrl": "", "musicLabel": "", "musicDraggable": False,
               "musicLength": 0.0, "musicRegion": region}
    if music["mode"] == editor_service.MUSIC_NONE:
        return preview
    path = editor_service.music_file(task_id, video_index, music)
    if path:
        label = music["song"] if music["mode"] == editor_service.MUSIC_SONG else tr("Editor Music Keep")
        preview.update(
            musicUrl=_media_url(path, "audio/mpeg"),
            musicLabel=label,
            musicDraggable=True,
            musicLength=editor_service.probe_duration(path),
        )
        return preview
    params = editor_service._load_task_params(task_id)["params"]
    if params.get("bgm_type") and float(params.get("bgm_volume") or 0) > 0:
        # 旧任务的随机音乐：会保留，但原曲未知，无法预览或移动。
        preview["musicLabel"] = tr("Editor Music Keep")
    return preview


def _component_data(task_id: str, state: dict, tr: Callable) -> dict:
    project = state["project"]
    sources = {}
    terms, keyword_by_file = editor_service.task_keywords(task_id)
    with st.spinner(tr("Editor Loading")):
        for source_id, source in project["sources"].items():
            proxy = editor_service.preview_file(task_id, source)
            thumb = editor_service.thumbnail_file(task_id, source, source["duration"] / 2)
            sources[source_id] = {
                "label": source["label"],
                "duration": source["duration"],
                "url": _media_url(
                    proxy, "video/webm" if proxy.endswith(".webm") else "video/mp4"
                ),
                "thumb": _media_url(thumb, "image/jpeg"),
                # 片段原来的搜索关键词，单击片段时用来预填在线素材搜索。
                "keyword": keyword_by_file.get(os.path.basename(source["path"]), ""),
            }
        thumbs = {}
        for clip in project["clips"][:200]:
            source = project["sources"][clip["source"]]
            thumb = editor_service.thumbnail_file(task_id, source, (clip["in"] + clip["out"]) / 2)
            thumbs[clip["id"]] = _media_url(thumb, "image/jpeg")
    music_preview = _music_preview(task_id, state["video_index"], project, tr)
    job = _job(task_id, state["video_index"])
    return {
        "rev": state["rev"],
        "duration": project["duration"],
        "aspect": str(editor_service._load_task_params(task_id)["params"].get("video_aspect") or "9:16"),
        "voiceUrl": _media_url(os.path.join(editor_service.utils.task_dir(task_id), "audio.mp3"), "audio/mpeg"),
        "voiceVolume": project["voice_volume"],
        **music_preview,
        "music": project["music"],
        "songs": editor_service.list_songs(),
        "stock": {
            **st.session_state.get("editor_stock", {}),
            "providers": editor_service.stock_providers(),
            "terms": terms,
        },
        "stockPick": st.session_state.get("editor_stock_pick"),
        "subtitlesEnabled": project["subtitles_enabled"],
        "sources": sources,
        "thumbs": thumbs,
        "clips": project["clips"],
        "subtitles": project["subtitles"],
        "labels": {name: tr(key) for name, key in _JS_LABEL_KEYS.items()},
        "rendering": bool(job and job["state"] == "running"),
    }


def _render_upload(task_id: str, state: dict, tr: Callable) -> None:
    """导入本地片段到素材库；文件较大，留在 Streamlit 原生上传控件中处理。"""
    key = f"{task_id}_{state['video_index']}_{state['rev']}"
    with st.expander(tr("Editor Upload Clip"), icon=":material/upload:"):
        upload = st.file_uploader(
            tr("Editor Upload Clip"),
            type=["mp4", "mov", "m4v", "webm", "mkv"],
            key=f"editor_upload_{key}",
            label_visibility="collapsed",
        )
    if upload is not None:
        marker = f"{upload.name}:{upload.size}"
        if st.session_state.get("editor_last_upload") != marker:
            try:
                state["project"] = editor_service.add_source(
                    task_id, state["video_index"], state["project"], upload.name, upload.getvalue()
                )
                st.session_state["editor_last_upload"] = marker
                st.rerun()
            except editor_service.EditorError as exc:
                st.error(str(exc))


def _handle_action(task_id: str, video_index: int, state: dict, action: dict, tr: Callable) -> bool:
    """处理时间线组件发来的请求；返回 True 表示需要立即刷新页面。"""
    kind = action.get("type")
    edits = dict(action.get("edits") or {})
    nonce = str(action.get("nonce") or "")
    if kind == "stock_search":
        stock = {"query": str(action.get("query") or "")[:100], "nonce": nonce, "results": [], "error": ""}
        try:
            with st.spinner(tr("Editor Stock Searching")):
                stock["results"] = editor_service.search_stock(
                    task_id, stock["query"], str(action.get("provider") or "")
                )
        except Exception as exc:
            logger.warning(f"editor stock search failed: {exc}")
            stock["error"] = str(exc)[:200]
        st.session_state["editor_stock"] = stock
        return True
    if kind == "stock_pick":
        try:
            with st.spinner(tr("Editor Stock Downloading")):
                project, source_id = editor_service.add_stock_source(
                    task_id, state["project"], str(action.get("result") or "")
                )
            state["project"] = project
            st.session_state["editor_stock_pick"] = {
                "nonce": nonce, "source": source_id, "clipId": action.get("clip"),
            }
        except Exception as exc:
            logger.warning(f"editor stock download failed: {exc}")
            stock = dict(st.session_state.get("editor_stock", {}))
            stock.update(error=str(exc)[:200], nonce=nonce)
            st.session_state["editor_stock"] = stock
        return True
    try:
        merged = editor_service.validate_edits(state["project"], edits)
    except editor_service.EditorError as exc:
        st.error(str(exc))
        return False
    if kind == "music":
        # 只更新会话中的音乐设置，刷新后预览新的音乐文件；不写盘。
        state["project"]["music"] = merged["music"]
        state["project"]["voice_volume"] = merged["voice_volume"]
        return True
    state["project"] = merged
    editor_service.save_project(task_id, video_index, merged)
    if kind == "render":
        _start_render(task_id, video_index, edits)
    else:
        st.toast(tr("Editor Saved"))
    return False


@st.fragment(run_every="1s")
def _render_job_progress(task_id: str, video_index: int, tr: Callable) -> None:
    """只在渲染进行中轮询进度；结束后整页刷新，切回静态结果。"""
    job = _job(task_id, video_index)
    if not job or job["state"] != "running":
        st.rerun(scope="app")
    st.progress(job["progress"], text=f"{tr('Editor Rendering')} {job['message']}")


def _render_job_status(task_id: str, video_index: int, tr: Callable) -> None:
    job = _job(task_id, video_index)
    if job and job["state"] == "running":
        _render_job_progress(task_id, video_index, tr)
        return
    if job and job["state"] == "failed":
        st.error(tr("Editor Render Failed").format(error=job["error"]))
    elif job and job["state"] == "done":
        st.success(tr("Editor Render Done"))
    output = editor_service.edited_video_file(task_id, video_index)
    if not os.path.isfile(output):
        return
    with st.container(border=True, key="editor_result_card"):
        st.markdown(f"**{tr('Editor Edited Result')}**")
        columns = st.columns([1, 1])
        with columns[0]:
            st.video(output)
        with columns[1]:
            with open(output, "rb") as file:
                st.download_button(
                    tr("Editor Download Edited"),
                    data=file,
                    file_name=os.path.basename(output),
                    mime="video/mp4",
                    icon=":material/download:",
                    on_click="ignore",
                    key=f"editor_download_{task_id}_{video_index}",
                )


def render_editor(tr: Callable) -> None:
    task_id = st.session_state.get("editor_task_id", "")
    header = st.columns([1, 3, 1.4, 1.2], vertical_alignment="center")
    if header[0].button(tr("Editor Back"), icon=":material/arrow_back:", key="editor_back"):
        close_editor()
        st.rerun()
    header[1].markdown(f"<div class='mpt-page-heading'><h2>{tr('Editor Title')}</h2></div>", unsafe_allow_html=True)

    indexes = editor_service.available_video_indexes(task_id) if task_id else []
    if not indexes:
        st.warning(tr("Editor Not Available"))
        return
    video_index = header[2].selectbox(
        tr("Editor Video Select"), indexes, key=f"editor_video_index_{task_id}",
        format_func=lambda value: f"#{value}",
        label_visibility="collapsed" if len(indexes) == 1 else "visible",
        disabled=len(indexes) == 1,
    )
    if header[3].button(tr("Editor Reset"), icon=":material/restart_alt:", key="editor_reset"):
        project = editor_service.reset_project(task_id, video_index)
        st.session_state["editor_rev"] = int(st.session_state.get("editor_rev", 0)) + 1
        st.session_state["editor_project"] = {
            "task_id": task_id, "video_index": video_index,
            "project": project, "rev": st.session_state["editor_rev"],
        }

    try:
        state = _project_state(task_id, video_index)
        data = _component_data(task_id, state, tr)
    except editor_service.EditorError as exc:
        st.error(str(exc))
        return

    result = _timeline(
        key=f"editor_timeline_{task_id}_{video_index}",
        data=data,
        on_action_change=lambda: None,
    )
    action = getattr(result, "action", None)
    if isinstance(action, dict) and action.get("nonce") != st.session_state.get("editor_last_nonce"):
        st.session_state["editor_last_nonce"] = action.get("nonce")
        if _handle_action(task_id, video_index, state, action, tr):
            st.rerun()

    _render_upload(task_id, state, tr)
    _render_job_status(task_id, video_index, tr)
