// MoneyPrinterTurbo 时间线编辑器（Streamlit components.v2 组件）。
//
// Python 端通过 data 传入工程（片段、字幕、素材库、媒体 URL 和界面文案），
// 编辑状态保存在组件根节点上：Streamlit 每次 rerun 都会重新调用本函数，
// 只有 data.rev 变化（重新载入或放弃修改）时才重置编辑状态。保存与渲染通过
// setTriggerValue("action", …) 交回 Python，拖动过程中不触发 rerun。
//
// 预览以旁白音频为主时钟：根据时间线位置找到当前片段，切换静音视频的
// 素材并对齐播放位置，字幕作为叠加层实时显示，因此改动无需渲染即可预览。

const MIN_CLIP = 0.3;
const MIN_SUB = 0.2;
const HISTORY_LIMIT = 100;

const ICONS = {
  play: '<path d="M8 5v14l11-7z" fill="currentColor" stroke="none"/>',
  pause: '<rect x="6" y="5" width="4" height="14" fill="currentColor" stroke="none"/><rect x="14" y="5" width="4" height="14" fill="currentColor" stroke="none"/>',
  back: '<path d="M11 7l-6 5 6 5zM19 7l-6 5 6 5z" fill="currentColor" stroke="none"/>',
  fwd: '<path d="M5 7l6 5-6 5zM13 7l6 5-6 5z" fill="currentColor" stroke="none"/>',
  split: '<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M20 4L8.1 15.9M14.5 14.5L20 20M8.1 8.1L12 12"/>',
  trash: '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
  text: '<path d="M5 6V4h14v2M12 4v16M9 20h6"/>',
  undo: '<path d="M9 14L4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 0 12h-3"/>',
  redo: '<path d="M15 14l5-5-5-5"/><path d="M20 9H10a6 6 0 0 0 0 12h3"/>',
  zoomOut: '<circle cx="11" cy="11" r="7"/><path d="M8 11h6M20 20l-4-4"/>',
  zoomIn: '<circle cx="11" cy="11" r="7"/><path d="M8 11h6M11 8v6M20 20l-4-4"/>',
  music: '<path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/>',
};

function icon(name, size = 20) {
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function fmt(seconds) {
  const s = Math.max(0, seconds);
  const m = Math.floor(s / 60);
  const r = s - m * 60;
  return `${m}:${r < 10 ? "0" : ""}${r.toFixed(1)}`;
}

function tickLabel(seconds) {
  if (seconds === 0) return "0s";
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const r = Math.round(seconds - m * 60);
  return r ? `${m}:${r < 10 ? "0" : ""}${r}` : `${m}m`;
}

function uid(prefix) {
  return prefix + Math.random().toString(36).slice(2, 12);
}

const clone = (value) => JSON.parse(JSON.stringify(value));
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const clipLen = (c) => (c.out - c.in) / (c.speed || 1);

class TimelineEditor {
  constructor(root, data, setTriggerValue) {
    this.root = root;
    this.setTriggerValue = setTriggerValue;
    this.rev = data.rev;
    this.data = data;
    this.L = data.labels || {};
    this.clips = clone(data.clips || []);
    this.subs = clone(data.subtitles || []);
    this.subsEnabled = data.subtitlesEnabled !== false;
    this.sel = null; // {kind: "sub"|"clip", id}
    this.zoom = 40; // px per second
    this.time = 0;
    this.playing = false;
    this.dirty = false;
    this.undoStack = [];
    this.redoStack = [];
    this.drag = null;
    this.build();
    this.renderAll();
    this.seek(0);
    this.raf = requestAnimationFrame(() => this.tick());
  }

  update(data) {
    // 素材库可能新增（上传），文案和音乐设置可能变化；编辑中的片段和字幕保持不变。
    this.data = data;
    this.L = data.labels || this.L;
    if ((data.musicUrl || "") !== this.musicUrl) this.setMusicSource(data.musicUrl);
    this.voice.volume = clamp(data.voiceVolume ?? 1, 0, 1);
    this.music.volume = clamp(data.musicVolume ?? 0.2, 0, 1);
    this.renderSide();
    this.renderTracks();
    this.renderLibrary();
    this.renderButtons();
  }

  destroy() {
    cancelAnimationFrame(this.raf);
    this.voice.pause();
    this.music.pause();
    this.video.pause();
  }

  // ----------------------------------------------------------- DOM skeleton
  build() {
    const L = this.L;
    const [w, h] = this.frameSize();
    this.root.innerHTML = `
      <div class="mpt-ed" tabindex="0">
        <div class="mpt-ed__top">
          <div class="mpt-ed__stage">
            <div class="mpt-ed__frame" style="width:${w}px;height:${h}px">
              <video muted playsinline preload="auto"></video>
              <div class="mpt-ed__caption"></div>
            </div>
          </div>
          <div class="mpt-ed__side"></div>
        </div>
        <div class="mpt-ed__panel">
          <div class="mpt-ed__toolbar">
            <div class="mpt-ed__group">
              <button class="mpt-ed__icon" data-act="split" title="${esc(L.split)}" aria-label="${esc(L.split)}">${icon("split")}</button>
              <button class="mpt-ed__icon" data-act="addsub" title="${esc(L.addSubtitle)}" aria-label="${esc(L.addSubtitle)}">${icon("text")}</button>
              <button class="mpt-ed__icon" data-act="delete" title="${esc(L.delete)}" aria-label="${esc(L.delete)}">${icon("trash")}</button>
              <span class="mpt-ed__divider"></span>
              <button class="mpt-ed__icon" data-act="undo" title="${esc(L.undo)}" aria-label="${esc(L.undo)}">${icon("undo")}</button>
              <button class="mpt-ed__icon" data-act="redo" title="${esc(L.redo)}" aria-label="${esc(L.redo)}">${icon("redo")}</button>
            </div>
            <div class="mpt-ed__group mpt-ed__group--center">
              <button class="mpt-ed__icon mpt-ed__icon--small" data-act="back" aria-label="-5s">${icon("back", 16)}</button>
              <button class="mpt-ed__icon mpt-ed__icon--play" data-act="play" aria-label="${esc(L.play)}">${icon("play", 22)}</button>
              <button class="mpt-ed__icon mpt-ed__icon--small" data-act="fwd" aria-label="+5s">${icon("fwd", 16)}</button>
              <span class="mpt-ed__time"></span>
            </div>
            <div class="mpt-ed__group">
              <button class="mpt-ed__icon" data-act="zoomout" aria-label="${esc(L.zoom)} -">${icon("zoomOut")}</button>
              <input class="mpt-ed__zoom" type="range" min="10" max="240" step="1" aria-label="${esc(L.zoom)}">
              <button class="mpt-ed__icon" data-act="zoomin" aria-label="${esc(L.zoom)} +">${icon("zoomIn")}</button>
            </div>
          </div>
          <div class="mpt-ed__scroller">
            <div class="mpt-ed__content">
              <div class="mpt-ed__ruler"></div>
              <div class="mpt-ed__track mpt-ed__track--subs"></div>
              <div class="mpt-ed__track mpt-ed__track--music"></div>
              <div class="mpt-ed__track mpt-ed__track--clips"></div>
              <div class="mpt-ed__playhead"></div>
            </div>
          </div>
          <p class="mpt-ed__libtitle">${esc(L.library)}</p>
          <div class="mpt-ed__library"></div>
        </div>
      </div>`;
    const q = (s) => this.root.querySelector(s);
    this.el = q(".mpt-ed");
    this.video = q("video");
    this.caption = q(".mpt-ed__caption");
    this.side = q(".mpt-ed__side");
    this.scroller = q(".mpt-ed__scroller");
    this.content = q(".mpt-ed__content");
    this.ruler = q(".mpt-ed__ruler");
    this.subTrack = q(".mpt-ed__track--subs");
    this.musicTrack = q(".mpt-ed__track--music");
    this.clipTrack = q(".mpt-ed__track--clips");
    this.playhead = q(".mpt-ed__playhead");
    this.timeLabel = q(".mpt-ed__time");
    this.zoomInput = q(".mpt-ed__zoom");
    this.library = q(".mpt-ed__library");
    this.playBtn = q('[data-act="play"]');
    this.zoomInput.value = this.zoom;

    this.voice = new Audio(this.data.voiceUrl || "");
    this.voice.preload = "auto";
    this.voice.volume = clamp(this.data.voiceVolume ?? 1, 0, 1);
    this.voice.addEventListener("ended", () => this.pause());
    this.music = new Audio();
    this.music.loop = true;
    this.music.volume = clamp(this.data.musicVolume ?? 0.2, 0, 1);
    this.setMusicSource(this.data.musicUrl);

    this.el.addEventListener("click", (e) => this.onClick(e));
    this.el.addEventListener("keydown", (e) => this.onKey(e));
    this.zoomInput.addEventListener("input", () => this.setZoom(+this.zoomInput.value));
    this.content.addEventListener("pointerdown", (e) => this.onPointerDown(e));
    this.content.addEventListener("pointermove", (e) => this.onPointerMove(e));
    this.content.addEventListener("pointerup", (e) => this.onPointerUp(e));
    this.content.addEventListener("pointercancel", (e) => this.onPointerUp(e));
  }

  frameSize() {
    const aspect = this.data.aspect || "9:16";
    if (aspect === "16:9") return [560, 315];
    if (aspect === "1:1") return [360, 360];
    return [216, 384];
  }

  setMusicSource(url) {
    this.musicUrl = url || "";
    this.music.pause();
    if (url) {
      this.music.src = url;
    } else {
      this.music.removeAttribute("src");
    }
  }

  // ------------------------------------------------------------- model utils
  snapshot() {
    return JSON.stringify({ clips: this.clips, subs: this.subs, subsEnabled: this.subsEnabled });
  }

  commit(mutator) {
    const before = this.snapshot();
    mutator();
    if (this.snapshot() !== before) {
      this.undoStack.push(before);
      if (this.undoStack.length > HISTORY_LIMIT) this.undoStack.shift();
      this.redoStack = [];
      this.dirty = true;
    }
    this.renderAll();
  }

  restore(snapshot) {
    const state = JSON.parse(snapshot);
    this.clips = state.clips;
    this.subs = state.subs;
    this.subsEnabled = state.subsEnabled;
    this.dirty = true;
    if (this.sel && !this.findSel()) this.sel = null;
    this.renderAll();
  }

  undo() {
    if (!this.undoStack.length) return;
    this.redoStack.push(this.snapshot());
    this.restore(this.undoStack.pop());
  }

  redo() {
    if (!this.redoStack.length) return;
    this.undoStack.push(this.snapshot());
    this.restore(this.redoStack.pop());
  }

  duration() {
    return Math.max(0.1, this.data.duration || 0.1);
  }

  clipLayout() {
    let t = 0;
    return this.clips.map((c) => {
      const item = { clip: c, start: t, end: t + clipLen(c) };
      t = item.end;
      return item;
    });
  }

  // 与服务端渲染一致：片段总长不足旁白时循环片段。
  clipAtTime(t) {
    const layout = this.clipLayout();
    const total = layout.length ? layout[layout.length - 1].end : 0;
    if (!total) return null;
    const local = t < total ? t : t % total;
    const item = layout.find((x) => local >= x.start && local < x.end) || layout[layout.length - 1];
    return { ...item, offset: local - item.start };
  }

  subAtTime(t) {
    return this.subs.find((s) => t >= s.start && t < s.end) || null;
  }

  findSel() {
    if (!this.sel) return null;
    const list = this.sel.kind === "sub" ? this.subs : this.clips;
    return list.find((x) => x.id === this.sel.id) || null;
  }

  // ---------------------------------------------------------------- playback
  play() {
    if (this.time >= this.duration() - 0.05) this.seek(0);
    this.playing = true;
    this.voice.currentTime = this.time;
    this.voice.play().catch(() => {});
    if (this.music.getAttribute("src")) {
      this.music.currentTime = this.music.duration ? this.time % this.music.duration : 0;
      this.music.play().catch(() => {});
    }
    this.syncVideo(true);
    this.playBtn.innerHTML = icon("pause", 22);
  }

  pause() {
    this.playing = false;
    this.voice.pause();
    this.music.pause();
    this.video.pause();
    this.playBtn.innerHTML = icon("play", 22);
  }

  seek(t) {
    this.time = clamp(t, 0, this.duration());
    if (this.playing) {
      this.voice.currentTime = this.time;
      if (this.music.duration) this.music.currentTime = this.time % this.music.duration;
    }
    this.syncVideo(true);
    this.renderPlayhead();
  }

  syncVideo(force) {
    const hit = this.clipAtTime(this.time);
    if (!hit) return;
    const source = this.data.sources[hit.clip.source];
    if (!source || !source.url) return;
    const target = hit.clip.in + hit.offset * (hit.clip.speed || 1);
    if (this.video.dataset.src !== source.url) {
      this.video.dataset.src = source.url;
      this.video.src = source.url;
      this.video.addEventListener("loadedmetadata", () => {
        this.video.currentTime = target;
        if (this.playing) this.video.play().catch(() => {});
      }, { once: true });
      return;
    }
    this.video.playbackRate = hit.clip.speed || 1;
    if (force || Math.abs(this.video.currentTime - target) > 0.3) {
      this.video.currentTime = target;
    }
    if (this.playing && this.video.paused) this.video.play().catch(() => {});
    if (!this.playing && !this.video.paused) this.video.pause();
  }

  tick() {
    if (!this.root.isConnected) return;
    if (this.playing) {
      this.time = this.voice.currentTime;
      if (this.time >= this.duration()) this.pause();
      this.syncVideo(false);
      this.renderPlayhead();
    }
    const sub = this.subsEnabled ? this.subAtTime(this.time) : null;
    const text = sub ? sub.text : "";
    if (this.caption.textContent !== text) this.caption.textContent = text;
    this.raf = requestAnimationFrame(() => this.tick());
  }

  // ---------------------------------------------------------------- rendering
  renderAll() {
    this.renderTracks();
    this.renderSide();
    this.renderLibrary();
    this.renderButtons();
  }

  px(seconds) {
    return seconds * this.zoom;
  }

  renderTracks() {
    const duration = this.duration();
    const layout = this.clipLayout();
    const clipTotal = layout.length ? layout[layout.length - 1].end : 0;
    const span = Math.max(duration, clipTotal) + 2;
    this.content.style.width = `${this.px(span)}px`;

    // 刻度：根据缩放选择主刻度间隔，保持标签间距约 80px 以上。
    const steps = [1, 2, 5, 10, 15, 20, 30, 60, 120];
    const major = steps.find((s) => this.px(s) >= 80) || 120;
    const minor = major / 4;
    let ruler = "";
    for (let t = 0; t <= span; t += minor) {
      const isMajor = Math.abs(t / major - Math.round(t / major)) < 1e-6;
      ruler += `<span class="mpt-ed__tick mpt-ed__tick--${isMajor ? "major" : "minor"}" style="left:${this.px(t)}px"></span>`;
      if (isMajor) {
        // 0 刻度标签左对齐，避免居中后被裁掉一半。
        const shift = t === 0 ? "transform:none;" : "";
        ruler += `<span class="mpt-ed__ticklabel" style="left:${this.px(t)}px;${shift}">${tickLabel(Math.round(t))}</span>`;
      }
    }
    ruler += `<span class="mpt-ed__beyond" style="left:${this.px(duration)}px;right:0"></span>`;
    this.ruler.innerHTML = ruler;

    const sel = this.sel || {};
    this.subTrack.setAttribute("aria-label", this.L.subtitlesTrack || "");
    this.subTrack.innerHTML = this.subs.map((s) => `
        <div class="mpt-ed__sub${sel.kind === "sub" && sel.id === s.id ? " mpt-ed__sub--sel" : ""}"
             data-kind="sub" data-id="${esc(s.id)}" title="${esc(s.text)}"
             style="left:${this.px(s.start)}px;width:${Math.max(6, this.px(s.end - s.start))}px">
          <i class="mpt-ed__handle mpt-ed__handle--l" data-edge="l"></i>
          <span>${esc(s.text)}</span>
          <i class="mpt-ed__handle mpt-ed__handle--r" data-edge="r"></i>
        </div>`).join("");

    const musicOn = !!this.data.musicLabel;
    this.musicTrack.innerHTML = `
      <div class="mpt-ed__music${musicOn ? "" : " mpt-ed__music--off"}" style="width:${this.px(duration)}px">
        ${icon("music", 16)}<span>${esc(this.data.musicLabel || this.L.noMusic)}</span>
      </div>`;

    this.clipTrack.setAttribute("aria-label", this.L.clipsTrack || "");
    let clipsHtml = "";
    for (const item of layout) {
      const c = item.clip;
      const source = this.data.sources[c.source] || {};
      const thumb = (this.data.thumbs || {})[c.id] || source.thumb || "";
      clipsHtml += `
        <div class="mpt-ed__clip${sel.kind === "clip" && sel.id === c.id ? " mpt-ed__clip--sel" : ""}${this.drag && this.drag.kind === "clip-move" && this.drag.id === c.id ? " mpt-ed__clip--drag" : ""}"
             data-kind="clip" data-id="${esc(c.id)}" title="${esc(source.label || "")}"
             style="left:${this.px(item.start)}px;width:${Math.max(6, this.px(item.end - item.start))}px;${thumb ? `background-image:url('${thumb}')` : ""}">
          <i class="mpt-ed__handle mpt-ed__handle--l" data-edge="l"></i>
          <span class="mpt-ed__cliplabel">${fmt(item.end - item.start)}</span>
          <i class="mpt-ed__handle mpt-ed__handle--r" data-edge="r"></i>
        </div>`;
    }
    if (clipTotal && clipTotal < duration) {
      const width = this.px(duration - clipTotal);
      // 区域太窄时只保留斜纹，说明放到悬停提示里。
      const label = width >= 90 ? esc(this.L.clipsRepeat) : "";
      clipsHtml += `<div class="mpt-ed__repeat" title="${esc(this.L.clipsRepeat)}" style="left:${this.px(clipTotal)}px;width:${width}px">${label}</div>`;
    }
    clipsHtml += `<div class="mpt-ed__insert"></div>`;
    this.clipTrack.innerHTML = clipsHtml;
    this.insertMarker = this.clipTrack.querySelector(".mpt-ed__insert");
    this.renderPlayhead();
  }

  renderPlayhead() {
    this.playhead.style.left = `${this.px(this.time)}px`;
    this.timeLabel.textContent = `${fmt(this.time)} / ${fmt(this.duration())}`;
    if (this.playing) {
      const x = this.px(this.time);
      const view = this.scroller;
      if (x < view.scrollLeft || x > view.scrollLeft + view.clientWidth - 40) {
        view.scrollLeft = Math.max(0, x - 80);
      }
    }
  }

  renderButtons() {
    const q = (a) => this.root.querySelector(`[data-act="${a}"]`);
    q("undo").disabled = !this.undoStack.length;
    q("redo").disabled = !this.redoStack.length;
    q("delete").disabled = !this.sel;
    const busy = !!this.data.rendering;
    this.root.querySelectorAll('[data-act="save"],[data-act="render"]').forEach((b) => (b.disabled = busy));
  }

  renderSide() {
    const L = this.L;
    const item = this.findSel();
    let body = "";
    if (item && this.sel.kind === "sub") {
      body = `
        <h3>${esc(L.subtitlesTrack)}</h3>
        <label class="mpt-ed__field"><span>${esc(L.text)}</span>
          <textarea data-field="text">${esc(item.text)}</textarea></label>
        <div class="mpt-ed__row">
          <label class="mpt-ed__field"><span>${esc(L.start)}</span><input type="number" step="0.1" min="0" data-field="start" value="${item.start.toFixed(2)}"></label>
          <label class="mpt-ed__field"><span>${esc(L.end)}</span><input type="number" step="0.1" min="0" data-field="end" value="${item.end.toFixed(2)}"></label>
        </div>
        <div class="mpt-ed__actions" style="margin-top:0">
          <button class="mpt-ed__btn" data-act="split">${icon("split", 16)}${esc(L.split)}</button>
          <button class="mpt-ed__btn mpt-ed__btn--danger" data-act="delete">${icon("trash", 16)}${esc(L.delete)}</button>
        </div>`;
    } else if (item && this.sel.kind === "clip") {
      const source = this.data.sources[item.source] || {};
      const options = Object.entries(this.data.sources).map(([id, s]) =>
        `<option value="${esc(id)}"${id === item.source ? " selected" : ""}>${esc(s.label)} · ${fmt(s.duration)}</option>`).join("");
      const speeds = [0.5, 0.75, 1, 1.25, 1.5, 2].map((v) =>
        `<option value="${v}"${Math.abs(v - (item.speed || 1)) < 1e-3 ? " selected" : ""}>${v}×</option>`).join("");
      body = `
        <h3>${esc(L.clipsTrack)} · ${esc(source.label || "")}</h3>
        <div class="mpt-ed__row">
          <label class="mpt-ed__field"><span>${esc(L.sourceIn)}</span><input type="number" step="0.1" min="0" data-field="in" value="${item.in.toFixed(2)}"></label>
          <label class="mpt-ed__field"><span>${esc(L.sourceOut)}</span><input type="number" step="0.1" min="0" data-field="out" value="${item.out.toFixed(2)}"></label>
        </div>
        <div class="mpt-ed__row">
          <label class="mpt-ed__field"><span>${esc(L.speed)}</span><select data-field="speed">${speeds}</select></label>
          <label class="mpt-ed__field"><span>${esc(L.replace)}</span><select data-field="source">${options}</select></label>
        </div>
        <div class="mpt-ed__actions" style="margin-top:0">
          <button class="mpt-ed__btn" data-act="split">${icon("split", 16)}${esc(L.split)}</button>
          <button class="mpt-ed__btn" data-act="duplicate">${esc(L.duplicate)}</button>
          <button class="mpt-ed__btn mpt-ed__btn--danger" data-act="delete">${icon("trash", 16)}${esc(L.delete)}</button>
        </div>`;
    } else {
      body = `<h3>${esc(L.title)}</h3><p class="mpt-ed__hint">${esc(L.hint)}</p>`;
    }
    const status = this.data.rendering ? L.rendering : this.dirty ? L.unsaved : "";
    this.side.innerHTML = `
      ${body}
      <label class="mpt-ed__field" style="flex-direction:row;align-items:center;gap:8px">
        <input type="checkbox" data-field="subsEnabled" style="width:18px;min-height:18px;accent-color:#17181C"${this.subsEnabled ? " checked" : ""}>
        <span style="font-weight:500">${esc(L.showSubtitles)}</span>
      </label>
      <div class="mpt-ed__actions">
        <button class="mpt-ed__btn" data-act="save">${esc(L.save)}</button>
        <button class="mpt-ed__btn mpt-ed__btn--accent" data-act="render">${esc(L.render)}</button>
      </div>
      <div class="mpt-ed__status">${esc(status)}</div>`;
    this.side.querySelectorAll("[data-field]").forEach((input) => {
      input.addEventListener("change", () => this.onField(input));
      if (input.tagName === "TEXTAREA") {
        input.addEventListener("focus", () => { this.textBefore = this.snapshot(); });
        input.addEventListener("input", () => this.onTextInput(input));
      }
    });
    this.renderButtons();
  }

  renderLibrary() {
    const L = this.L;
    this.library.innerHTML = Object.entries(this.data.sources || {}).map(([id, s]) => `
      <div class="mpt-ed__source">
        ${s.thumb ? `<img src="${s.thumb}" alt="">` : `<img alt="">`}
        <div><b title="${esc(s.label)}">${esc(s.label)}</b>
          <button class="mpt-ed__btn" data-act="append" data-source="${esc(id)}">${esc(L.add)}</button></div>
      </div>`).join("");
  }

  setZoom(value) {
    const center = (this.scroller.scrollLeft + this.scroller.clientWidth / 2) / this.zoom;
    this.zoom = clamp(value, 10, 240);
    this.zoomInput.value = this.zoom;
    this.renderTracks();
    this.scroller.scrollLeft = Math.max(0, center * this.zoom - this.scroller.clientWidth / 2);
  }

  // ----------------------------------------------------------------- editing
  onField(input) {
    const item = this.findSel();
    const field = input.dataset.field;
    if (field === "subsEnabled") {
      this.commit(() => { this.subsEnabled = input.checked; });
      return;
    }
    if (!item) return;
    if (field === "text") {
      // 输入时已实时写入文字；失焦时把聚焦前的快照记为一个撤销点。
      item.text = input.value.slice(0, 500);
      const before = this.textBefore;
      this.textBefore = null;
      if (before && before !== this.snapshot()) {
        this.undoStack.push(before);
        if (this.undoStack.length > HISTORY_LIMIT) this.undoStack.shift();
        this.redoStack = [];
        this.dirty = true;
      }
      this.renderAll();
      return;
    }
    this.commit(() => {
      if (field === "start") {
        item.start = clamp(+input.value || 0, 0, item.end - MIN_SUB);
      } else if (field === "end") {
        item.end = clamp(+input.value || 0, item.start + MIN_SUB, this.duration());
      } else if (field === "in") {
        item.in = clamp(+input.value || 0, 0, item.out - MIN_CLIP);
      } else if (field === "out") {
        const max = (this.data.sources[item.source] || {}).duration || item.out;
        item.out = clamp(+input.value || 0, item.in + MIN_CLIP, max);
      } else if (field === "speed") {
        item.speed = clamp(+input.value || 1, 0.25, 4);
      } else if (field === "source") {
        const source = this.data.sources[input.value];
        if (source) {
          const length = item.out - item.in;
          item.source = input.value;
          item.in = 0;
          item.out = Math.min(source.duration, Math.max(MIN_CLIP, length));
        }
      }
    });
  }

  onTextInput(input) {
    // 输入过程中实时更新字幕块和预览；失焦时 change 事件再记录一次撤销点。
    const item = this.findSel();
    if (!item || this.sel.kind !== "sub") return;
    item.text = input.value.slice(0, 500);
    this.dirty = true;
    const block = this.subTrack.querySelector(`[data-id="${CSS.escape(item.id)}"] span`);
    if (block) block.textContent = item.text;
  }

  split() {
    const t = this.time;
    if (this.sel && this.sel.kind === "sub") {
      const s = this.findSel();
      if (s && t > s.start + MIN_SUB && t < s.end - MIN_SUB) {
        this.commit(() => {
          const second = { id: uid("t"), start: t, end: s.end, text: s.text };
          s.end = t;
          this.subs.splice(this.subs.indexOf(s) + 1, 0, second);
        });
      }
      return;
    }
    const hit = this.clipAtTime(t);
    if (!hit || t >= this.clipLayout().slice(-1)[0].end) return;
    const c = hit.clip;
    const cut = c.in + hit.offset * (c.speed || 1);
    if (cut - c.in < MIN_CLIP || c.out - cut < MIN_CLIP) return;
    this.commit(() => {
      const second = { ...c, id: uid("c"), in: cut };
      c.out = cut;
      this.clips.splice(this.clips.indexOf(c) + 1, 0, second);
      this.sel = { kind: "clip", id: second.id };
    });
  }

  addSubtitle() {
    const start = clamp(this.time, 0, this.duration() - MIN_SUB);
    const next = this.subs.filter((s) => s.start > start).sort((a, b) => a.start - b.start)[0];
    const end = Math.min(this.duration(), start + 2, next ? next.start : Infinity);
    if (end - start < MIN_SUB) return;
    const sub = { id: uid("t"), start, end, text: this.L.newSubtitle || "New subtitle" };
    this.commit(() => {
      this.subs.push(sub);
      this.subs.sort((a, b) => a.start - b.start);
      this.sel = { kind: "sub", id: sub.id };
    });
  }

  removeSelected() {
    const item = this.findSel();
    if (!item) return;
    this.commit(() => {
      if (this.sel.kind === "sub") {
        this.subs = this.subs.filter((s) => s !== item);
      } else if (this.clips.length > 1) {
        this.clips = this.clips.filter((c) => c !== item);
      }
      this.sel = null;
    });
  }

  duplicateSelected() {
    const item = this.findSel();
    if (!item || this.sel.kind !== "clip") return;
    this.commit(() => {
      const copy = { ...item, id: uid("c") };
      this.clips.splice(this.clips.indexOf(item) + 1, 0, copy);
      this.sel = { kind: "clip", id: copy.id };
    });
  }

  appendSource(sourceId) {
    const source = this.data.sources[sourceId];
    if (!source) return;
    this.commit(() => {
      const clip = { id: uid("c"), source: sourceId, in: 0, out: Math.min(source.duration, 5), speed: 1 };
      this.clips.push(clip);
      this.sel = { kind: "clip", id: clip.id };
    });
  }

  emit(type) {
    this.pause();
    const edits = {
      clips: this.clips.map(({ id, source, in: i, out, speed }) => ({ id, source, in: i, out, speed })),
      subtitles: this.subs.map(({ id, start, end, text }) => ({ id, start, end, text })),
      subtitles_enabled: this.subsEnabled,
    };
    this.dirty = false;
    this.setTriggerValue("action", { type, edits, nonce: Date.now() });
    this.renderSide();
  }

  onClick(e) {
    const button = e.target.closest("[data-act]");
    if (!button || button.disabled) return;
    const act = button.dataset.act;
    if (act === "play") this.playing ? this.pause() : this.play();
    else if (act === "back") this.seek(this.time - 5);
    else if (act === "fwd") this.seek(this.time + 5);
    else if (act === "zoomin") this.setZoom(this.zoom * 1.4);
    else if (act === "zoomout") this.setZoom(this.zoom / 1.4);
    else if (act === "split") this.split();
    else if (act === "addsub") this.addSubtitle();
    else if (act === "delete") this.removeSelected();
    else if (act === "duplicate") this.duplicateSelected();
    else if (act === "undo") this.undo();
    else if (act === "redo") this.redo();
    else if (act === "append") this.appendSource(button.dataset.source);
    else if (act === "save" || act === "render") this.emit(act);
  }

  onKey(e) {
    const typing = ["INPUT", "TEXTAREA", "SELECT"].includes((e.composedPath()[0] || {}).tagName);
    if (typing) return;
    if (e.key === " ") { e.preventDefault(); this.playing ? this.pause() : this.play(); }
    else if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); this.removeSelected(); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") { e.preventDefault(); e.shiftKey ? this.redo() : this.undo(); }
    else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "y") { e.preventDefault(); this.redo(); }
    else if (e.key === "s" || e.key === "S") { this.split(); }
    else if (e.key === "ArrowLeft") { this.seek(this.time - (e.shiftKey ? 5 : 0.5)); }
    else if (e.key === "ArrowRight") { this.seek(this.time + (e.shiftKey ? 5 : 0.5)); }
  }

  // ------------------------------------------------------------ pointer drag
  timeAt(e) {
    const rect = this.content.getBoundingClientRect();
    return (e.clientX - rect.left) / this.zoom;
  }

  onPointerDown(e) {
    if (e.button !== 0) return;
    this.el.focus({ preventScroll: true });
    const block = e.target.closest("[data-kind]");
    const t = this.timeAt(e);
    if (!block) {
      // 标尺或空白处：定位播放头，按住可拖动浏览。
      this.drag = { kind: "scrub" };
      this.sel = null;
      this.seek(t);
      this.renderSide();
      this.renderTracks();
      this.content.setPointerCapture(e.pointerId);
      return;
    }
    const kind = block.dataset.kind;
    const id = block.dataset.id;
    const edge = e.target.dataset.edge || "";
    const list = kind === "sub" ? this.subs : this.clips;
    const item = list.find((x) => x.id === id);
    if (!item) return;
    this.sel = { kind, id };
    this.drag = {
      kind: kind === "sub" ? (edge ? `sub-${edge}` : "sub-move") : (edge ? `clip-${edge}` : "clip-move"),
      id, startT: t, startX: e.clientX, before: this.snapshot(), orig: { ...item }, moved: false,
    };
    this.content.setPointerCapture(e.pointerId);
    this.renderTracks();
    this.renderSide();
  }

  onPointerMove(e) {
    const d = this.drag;
    if (!d) return;
    const t = this.timeAt(e);
    if (d.kind === "scrub") { this.seek(t); return; }
    if (!d.moved && Math.abs(e.clientX - d.startX) < 3) return;
    d.moved = true;
    const delta = t - d.startT;
    const o = d.orig;
    if (d.kind.startsWith("sub")) {
      const s = this.subs.find((x) => x.id === d.id);
      if (!s) return;
      const length = o.end - o.start;
      if (d.kind === "sub-move") {
        s.start = clamp(o.start + delta, 0, this.duration() - length);
        s.end = s.start + length;
      } else if (d.kind === "sub-l") {
        s.start = clamp(o.start + delta, 0, o.end - MIN_SUB);
      } else {
        s.end = clamp(o.end + delta, o.start + MIN_SUB, this.duration());
      }
      this.renderTracks();
      return;
    }
    const c = this.clips.find((x) => x.id === d.id);
    if (!c) return;
    const sourceDuration = (this.data.sources[c.source] || {}).duration || o.out;
    if (d.kind === "clip-l") {
      c.in = clamp(o.in + delta * (o.speed || 1), 0, o.out - MIN_CLIP);
      this.renderTracks();
    } else if (d.kind === "clip-r") {
      c.out = clamp(o.out + delta * (o.speed || 1), o.in + MIN_CLIP, sourceDuration);
      this.renderTracks();
    } else {
      // 拖动排序：按指针位置计算插入点并显示标记。
      // 以去掉被拖片段后的排列计算各片段中点，指针越过中点即插到其后。
      const rest = this.clips.filter((x) => x.id !== c.id);
      let index = rest.length;
      let x = 0;
      let cursor = 0;
      for (let i = 0; i < rest.length; i++) {
        const length = clipLen(rest[i]);
        if (t < cursor + length / 2) { index = i; break; }
        cursor += length;
      }
      for (let i = 0; i < index; i++) x += clipLen(rest[i]);
      d.target = index;
      this.insertMarker.style.display = "block";
      this.insertMarker.style.left = `${this.px(x) - 1}px`;
      const block = this.clipTrack.querySelector(`[data-id="${CSS.escape(c.id)}"]`);
      if (block) block.classList.add("mpt-ed__clip--drag");
    }
  }

  onPointerUp(e) {
    const d = this.drag;
    this.drag = null;
    if (!d) return;
    try { this.content.releasePointerCapture(e.pointerId); } catch (_) { /* already released */ }
    if (d.kind === "scrub") return;
    if (!d.moved) {
      // 单击块：选中并把播放头移到点击处，方便立即分割。
      this.seek(this.timeAt(e));
      this.renderTracks();
      this.renderSide();
      return;
    }
    if (d.kind === "clip-move" && d.target !== undefined) {
      const c = this.clips.find((x) => x.id === d.id);
      const rest = this.clips.filter((x) => x.id !== d.id);
      rest.splice(d.target, 0, c);
      this.clips = rest;
    }
    if (this.snapshot() !== d.before) {
      this.undoStack.push(d.before);
      if (this.undoStack.length > HISTORY_LIMIT) this.undoStack.shift();
      this.redoStack = [];
      this.dirty = true;
      if (d.kind.startsWith("sub")) this.subs.sort((a, b) => a.start - b.start);
    }
    this.renderAll();
  }
}

export default function (component) {
  const { data, parentElement, setTriggerValue } = component;
  if (!data) return;
  let editor = parentElement.__mptEditor;
  if (!editor || editor.rev !== data.rev || !editor.root.isConnected) {
    if (editor) editor.destroy();
    let host = parentElement.querySelector(".mpt-ed-host");
    if (!host) {
      host = document.createElement("div");
      host.className = "mpt-ed-host";
      parentElement.appendChild(host);
    }
    editor = new TimelineEditor(host, data, setTriggerValue);
    parentElement.__mptEditor = editor;
  } else {
    editor.setTriggerValue = setTriggerValue;
    editor.update(data);
  }
}
