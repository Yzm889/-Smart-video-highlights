/* 一帧成片 · 桌面版 v2 —— 应用骨架逻辑
   固定工作站布局（菜单栏/工具栏/三栏编辑器/状态栏），调本地服务既有接口。
   浏览器打开此页 = 桌面能力自动降级（无原生对话框、无本地路径直连）。 */
'use strict';

const $ = id => document.getElementById(id);

/* ── 状态 ── */
let _video = null;            // {name, path, isLocalPath}
let _token = '';
let _isDesktop = false;
let _runDir = '', _videoFile = '', _vdur = 0;
let _items = [];              // 字幕段
let _sel = -1;                // 选中的数组下标
let _narrOn = true, _origOn = true;
let _pollTimer = null;
let _undoStack = [], _redoStack = [];
let _projName = '';

/* ── 小工具 ── */
const _esc = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
const _clean = s => String(s || '')
  .replace(/\{(?:情绪|停顿|慢|快|高音|低音|大声|小声)[^}]*\}/g, '')
  .replace(/\{\/(?:情绪|停顿|慢|快|高音|低音|大声|小声)\}/g, '').replace(/\s+/g, ' ').trim();
const _fmt = sec => {
  const m = Math.floor((sec || 0) / 60), s = Math.floor((sec || 0) % 60);
  return (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
};
function toast(msg, kind){
  let t = document.getElementById('dtoast');
  if(!t){
    t = document.createElement('div');
    t.id = 'dtoast';
    t.style.cssText = 'position:fixed;bottom:40px;left:50%;transform:translateX(-50%);' +
      'background:#2d2d2d;color:var(--txt);padding:9px 20px;border-radius:6px;font-size:13px;' +
      'z-index:500;border:1px solid var(--line);box-shadow:0 6px 18px rgba(0,0,0,.5);transition:opacity .3s';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.style.borderColor = kind === 'bad' ? 'var(--bad)' : 'var(--line)';
  t.style.opacity = '1';
  clearTimeout(t._h);
  t._h = setTimeout(() => t.style.opacity = '0', 2600);
}
function setStatus(s, kind){
  const el = $('stState');
  el.textContent = '● ' + s;
  el.className = kind === 'bad' ? 'bad' : (kind === 'ok' ? 'ok' : '');
  el.id = 'stState';
}

/* ── 视图切换 ── */
function showEditor(on){
  $('editor').style.display = on ? 'flex' : 'none';
  $('newProj').style.display = on ? 'none' : 'flex';
}
function switchView(v){
  /* 新架构无独立 view 分区：adjust/export = 显示编辑器三栏；
     media/story = 显示新建项目卡。保留函数名以兼容调用点。 */
  if(v === 'adjust'){ showEditor(true); return; }
  if(v === 'export'){ showEditor(true); return; }
  showEditor(false);
}

/* ── 菜单栏 ── */
document.querySelectorAll('.menu').forEach(m => {
  m.querySelector(':scope > span').addEventListener('click', e => {
    e.stopPropagation();
    const was = m.classList.contains('open');
    document.querySelectorAll('.menu').forEach(x => x.classList.remove('open'));
    if(!was) m.classList.add('open');
  });
});
document.addEventListener('click', () =>
  document.querySelectorAll('.menu').forEach(x => x.classList.remove('open')));
const _acts = {
  new(){ newProject(); },
  openfolder(){ openOutFolder(); },
  exit(){ window.close && window.close(); },
  undo(){ undoAdjust(); },
  redo(){ redoAdjust(); },
  addcap(){ addCaptionAtPlayhead(); },
  delcap(){ delSelected(); },
  tgnarr(){ toggleNarrTrack(); },
  tgorig(){ toggleOrigTrack(); },
  about(){ toast('一帧成片 · 本地免费 AI 解说剪辑 · 桌面版'); }
};
document.querySelectorAll('.mi').forEach(mi => {
  mi.addEventListener('click', () => {
    const fn = _acts[mi.dataset.act];
    if(fn) fn();
  });
});
function refreshMenus(){
  document.querySelector('[data-act="tgnarr"]').textContent =
    '解说轨：' + (_narrOn ? '开' : '禁音');
  document.querySelector('[data-act="tgorig"]').textContent =
    '原声轨：' + (_origOn ? '开' : '禁音');
}

/* ── 桌面模式初始化 ── */
let _desktopInited = false;
function initDesktop(){
  if(_desktopInited) return;
  const api = window.pywebview && window.pywebview.api;
  if(!api || !api.get_desktop_info) return;
  _desktopInited = true;
  api.get_desktop_info().then(info => {
    if(!info || !info.token) return;
    _token = info.token;
    _isDesktop = true;
  }).catch(e => console.warn('桌面初始化失败', e));
}
window.addEventListener('pywebviewready', initDesktop);
if(window.pywebview) initDesktop();

/* ── 新建项目 ── */
function updateNewProj(){
  const has = !!_video;
  const plot = $('npPlot').value.trim();
  $('npGo').disabled = !(has && plot);
  $('npHint').textContent = !_video ? '先选择视频'
    : !plot ? '写下剧情，或勾选「完整解说稿」'
    : $('npScript').checked ? '将按稿找画面配音（文字不改）' : 'AI 按剧情写解说、挑画面';
}
$('npPick').addEventListener('click', async () => {
  if(_isDesktop && window.pywebview && window.pywebview.api){
    try {
      const r = await window.pywebview.api.pick_video();
      if(r && r.path) acceptVideo(r.name || r.path.split(/[\\/]/).pop(), r.path);
    } catch(e){ toast('选择失败：' + e.message, 'bad'); }
  } else {
    toast('请运行桌面版 EXE 使用原生选片', 'bad');
  }
});
function acceptVideo(name, path){
  _video = {name, path, isLocalPath: true};
  $('npPick').classList.add('has');
  $('npPickLabel').textContent = '✅ ' + name;
  $('projName').textContent = name.replace(/\.[^.]+$/, '');
  updateNewProj();
}
$('npPlot').addEventListener('input', updateNewProj);
$('npScript').addEventListener('change', updateNewProj);

$('npGo').addEventListener('click', async () => {
  const btn = $('npGo');
  const plot = $('npPlot').value.trim();
  const isScript = $('npScript').checked;
  if(!_video || !plot) return;
  btn.disabled = true;
  _projName = ($('npName').value || _video.name).replace(/\.[^.]+$/, '');
  $('projName').textContent = _projName;
  $('genMask').style.display = 'flex';
  $('gmTitle').textContent = '正在生成（首次加载模型较慢）…';
  const body = {
    video: {name: _video.name, path: _video.path, desktop_token: _token},
    plot: isScript ? '' : plot,
    script: isScript ? plot : undefined,
    params: {
      maxSeg: 25, autoCut: $('npCut').checked, targetSec: 600,
      narr_style: 'movie', detail_level: 'balanced', genre: 'auto',
      subtitle: {fontSize: 22, color: '#FFFFFF', outlineColor: '#000000',
                 outlineWidth: 2, alignment: 2, marginV: 50},
      audio: {layers: false, exportVoice: false},
      resolution: 'original', fit: 'auto', colorStyle: 'native',
      qualityTier: 'balanced', transition: 'hard'
    }
  };
  try {
    const r = await fetch('/api/movie_tts', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const out = await r.json();
    if(!out.ok) throw new Error(out.error || '提交失败');
    pollTTS(out.runid);
  } catch(e){
    $('genMask').style.display = 'none';
    btn.disabled = false;
    toast('提交失败：' + e.message, 'bad');
  }
});
function pollTTS(runid){
  _runDir = runid;
  if(_pollTimer) clearInterval(_pollTimer);
  _pollTimer = setInterval(async () => {
    try {
      const r = await fetch('/api/progress?run=' + runid);
      const p = await r.json();
      $('gmFill').style.width = Math.min(100, p.pct || 0) + '%';
      $('gmMeta').textContent = (p.phase || '处理中') + ' · ' + (p.pct || 0) + '%';
      if(p.error){
        clearInterval(_pollTimer);
        $('genMask').style.display = 'none';
        $('npGo').disabled = false;
        toast('生成失败：' + p.error, 'bad');
        return;
      }
      if(p.done && p.tts_list && p.tts_list.length){
        clearInterval(_pollTimer);
        _runDir = p.run_dir || '';
        _videoFile = p.video_file || '';
        $('genMask').style.display = 'none';
        await loadAdjustItems(_runDir);
        openEditor();
        toast('✅ 已生成 ' + p.tts_list.length + ' 段配音与画面方案');
      }
    } catch(e){ /* 抖动忽略 */ }
  }, 1200);
}
function openEditor(){
  showEditor(true);
  switchView('adjust');
}

/* ── 加载调整数据 ── */
async function loadAdjustItems(runDir){
  const r = await fetch('/api/tts_state?run_dir=' + encodeURIComponent(runDir));
  const st = await r.json();
  if(!st.ok) throw new Error(st.error || '读取方案失败');
  _items = st.tts_list.map(it => ({
    index: it.index, text: it.text || '', audio: it.audio || '',
    audio_text: it.text || '', duration: it.duration || 0,
    video_start: it.video_start || 0, video_end: it.video_end || 0,
    narr_volume: null, isBroll: false, quote: false
  }));
  _vdur = st.video_duration || 0;
  if(st.video_path) _videoFile = runDir.replace(/\\/g, '/') + '/' + st.video_path;
  _sel = -1;
  renderCaps(); ensureNarrElements(); renderTimeline(); setupPreview();
  $('capCount').textContent = '· ' + _items.length + ' 段';
}

/* ── 字幕面板（左） ── */
function renderCaps(){
  const list = $('capList');
  list.innerHTML = _items.map((it, i) => {
    const vs = it.video_start || 0;
    const sel = i === _sel ? ' sel' : '';
    const skip = it._skip ? ' skip' : '';
    if(it.quote){
      return '<div class="cap-row' + sel + skip + '" data-i="' + i + '"><div class="row-top">'
        + '<span class="cap-time" style="color:#4ec9b0">' + _fmt(vs) + '</span>'
        + '<span class="cap-quote">🎬 原声引用 ' + (+vs).toFixed(1) + '-' + (+(it.video_end||0)).toFixed(1) + 's</span>'
        + '<button class="cap-del" data-del="' + i + '">✕</button></div></div>';
    }
    return '<div class="cap-row' + sel + skip + '" data-i="' + i + '"><div class="row-top">'
      + '<span class="cap-time">' + _fmt(vs) + '</span>'
      + '<textarea rows="2" data-edit="' + i + '">' + _esc(_clean(it.text)) + '</textarea>'
      + '<button class="cap-del" data-del="' + i + '" title="删除此段">✕</button></div></div>';
  }).join('');
  list.querySelectorAll('.cap-row').forEach(row => {
    row.addEventListener('click', e => {
      if(e.target.closest('textarea, button')) return;
      selectSeg(parseInt(row.dataset.i, 10));
    });
  });
  list.querySelectorAll('[data-edit]').forEach(ta => {
    ta.addEventListener('input', () => {
      const i = parseInt(ta.dataset.edit, 10);
      if(_items[i]) _items[i].text = ta.value;
    });
    ta.addEventListener('keydown', e => e.stopPropagation());
    ta.addEventListener('change', () => scheduleStatus());
  });
  list.querySelectorAll('[data-del]').forEach(b => {
    b.addEventListener('click', () => {
      const i = parseInt(b.dataset.del, 10);
      _pushUndo();
      _items.splice(i, 1);
      _sel = -1;
      renderCaps(); ensureNarrElements(); renderTimeline();
    });
  });
}
let _statusTimer = null;
function scheduleStatus(){
  clearTimeout(_statusTimer);
  _statusTimer = setTimeout(() => {
    const n = _items.filter((it, i) =>
      !_items[i]._skip && _clean(it.text) !== (it.audio_text || '').trim()).length;
    $('stState').textContent = n ? '● ' + n + ' 段改词待重配（合成时自动）' : '● 就绪';
  }, 300);
}

/* ── 选中与检查器 ── */
function selectSeg(i){
  _sel = i;
  const it = _items[i];
  if(!it) return;
  document.querySelectorAll('.cap-row').forEach((r, k) =>
    r.classList.toggle('sel', k === i));
  document.querySelectorAll('.tl-block').forEach((b, k) =>
    b.classList.toggle('sel', k === i));
  seekTo(it.video_start || 0);
  renderInspector(i);
  // 右列滚动到选中行
  const row = document.querySelector('.cap-row[data-i="' + i + '"]');
  if(row) row.scrollIntoView({block: 'nearest'});
  $('stSeg').textContent = '段 ' + (i + 1) + '/' + _items.length;
}
function renderInspector(i){
  const it = _items[i];
  const body = $('insBody');
  if(!it){ body.innerHTML = '<div class="ins-empty">点选一段字幕</div>'; return; }
  if(it.quote){
    body.innerHTML = '<div class="ins-empty" style="color:var(--ok)">🎬 原声引用段<br>保留原片声音</div>'
      + '<div class="ins-sep"></div>'
      + '<div class="ins-row"><label>画面范围（秒）</label><div class="ins-pair">'
      + '<input type="number" id="inVs" value="' + (+it.video_start).toFixed(1) + '" step="0.5">'
      + '<input type="number" id="inVe" value="' + (+(it.video_end||0)).toFixed(1) + '" step="0.5"></div></div>'
      + '<button class="ins-quote-btn on" id="inQuoteToggle">🗣 恢复为解说段</button>';
  } else {
    const nv = (it.narr_volume === undefined || it.narr_volume === null) ? 100 : it.narr_volume;
    const ov = (it.orig_volume !== undefined) ? it.orig_volume : 0;
    body.innerHTML = '<div class="ins-row"><label>字幕文字</label>'
      + '<input type="text" id="inText" value="' + _esc(_clean(it.text)) + '"></div>'
      + '<div class="ins-row"><label>🎙 解说音量 %（100 = 原始）</label><div class="ins-range">'
      + '<input type="range" id="inNv" min="0" max="200" step="5" value="' + nv + '">'
      + '<span class="rv" id="inNvV">' + nv + '%</span></div></div>'
      + '<div class="ins-row"><label>🎬 原声音量 %（解说时压低）</label><div class="ins-range">'
      + '<input type="range" id="inOv" min="0" max="100" step="5" value="' + ov + '">'
      + '<span class="rv" id="inOvV">' + ov + '%</span></div></div>'
      + '<div class="ins-sep"></div>'
      + '<div class="ins-row"><label>画面范围（秒）</label><div class="ins-pair">'
      + '<input type="number" id="inVs" value="' + (+it.video_start).toFixed(1) + '" step="0.5">'
      + '<input type="number" id="inVe" value="' + (+(it.video_end||0)).toFixed(1) + '" step="0.5"></div></div>'
      + '<button class="ins-quote-btn" id="inQuoteToggle">🎬 设为原声引用（保留原声，不配解说）</button>';
  }
  const bind = (id, fn) => { const el = $(id); if(el) el.addEventListener('input', () => fn(parseFloat(el.value))); };
  bind('inNv', v => { it.narr_volume = v; const s = $('inNvV'); if(s) s.textContent = v + '%';
    const el = document.getElementById('narr-' + i); if(el){ try { el.volume = Math.max(0, Math.min(1, v/100)); } catch(e){} } });
  bind('inOv', v => { it.orig_volume = v; const s = $('inOvV'); if(s) s.textContent = v + '%'; });
  bind('inVs', v => { it.video_start = v; renderTimeline(); });
  bind('inVe', v => { it.video_end = v; renderTimeline(); });
  bind('inText', v => { it.text = v; renderCaps(); });
  const qt = $('inQuoteToggle');
  if(qt) qt.addEventListener('click', () => {
    _pushUndo();
    it.quote = !it.quote;
    renderInspector(i); renderCaps(); renderTimeline();
  });
}

/* ── 时间轴 ── */
const TL_COLORS = ['#569cd6','#c586c0','#ce9178','#dcdcaa','#4ec9b0','#9cdcfe','#f48771','#6a9955'];
function renderTimeline(){
  const inner = $('tlInner');
  const total = Math.max(10, Math.ceil(_vdur));
  inner.style.width = Math.max(600, total * 12) + 'px';
  let ruler = '';
  const step = total > 600 ? 60 : total > 180 ? 30 : total > 60 ? 10 : 5;
  for(let t = 0; t <= total; t += step){
    ruler += '<span style="left:' + (t / total * 100) + '%">' + _fmt(t) + '</span>';
  }
  $('tlRuler').innerHTML = ruler;
  let html = '';
  let cum = 0;
  _items.forEach((it, i) => {
    const vd = Math.max(0.1, (it.video_end || 0) - (it.video_start || 0));
    const left = cum / total * 100, w = vd / total * 100;
    const cls = (it.quote ? ' quote' : '') + (it._skip ? ' skip' : '') + (i === _sel ? ' sel' : '');
    html += '<div class="tl-block' + cls + '" data-i="' + i + '" style="left:' + left + '%;width:'
      + w + '%;background:' + (it.quote ? '#10b981' : TL_COLORS[i % TL_COLORS.length]) + '">'
      + '<span>' + (it.quote ? '🎬' : '#' + (i + 1)) + '</span></div>';
    cum += vd;
  });
  $('tlTrack').innerHTML = html;
  $('tlTrack').querySelectorAll('.tl-block').forEach(b => {
    b.addEventListener('click', () => selectSeg(parseInt(b.dataset.i, 10)));
  });
  updatePlayhead();
}
function updatePlayhead(){
  const v = $('pvVideo');
  const ph = $('tlPlayhead');
  if(!v || !ph || !v.duration) return;
  const total = Math.max(10, _vdur || v.duration);
  ph.style.left = Math.min(100, (v.currentTime / total) * 100) + '%';
  $('stTime').textContent = _fmt(v.currentTime) + '.' + Math.floor((v.currentTime % 1) * 10);
}

/* ── 预览：配音轨 + 闪避镜像 + 叠字幕 ── */
let _narrEls = {};
function ensureNarrElements(){
  _items.forEach((it, i) => {
    if(it.quote || !it.audio) return;
    const key = i + '|' + it.audio;
    if(!_narrEls[key]){
      const a = document.createElement('audio');
      a.preload = 'auto';
      a.src = '/media/' + it.audio;
      a.style.display = 'none';
      document.body.appendChild(a);
      _narrEls[key] = a;
    }
  });
}
function _narrElFor(i){
  const it = _items[i];
  if(!it || it.quote || !it.audio) return null;
  const key = i + '|' + it.audio;
  return _narrEls[key] || null;
}
function applyNarrVolume(i){
  const el = _narrElFor(i);
  if(!el) return;
  const it = _items[i];
  const nv = it ? it.narr_volume : null;
  try { el.volume = (nv === undefined || nv === null) ? 1 : Math.max(0, Math.min(1, nv / 100)); } catch(e){}
  el.muted = !_narrOn;
}
let _curSeg = -1;
function previewTick(){
  const v = $('pvVideo');
  if(!v || v.paused) return;
  const t = v.currentTime || 0;
  let seg = -1;
  for(let i = 0; i < _items.length; i++){
    const it = _items[i];
    if(it.quote || it._skip) continue;
    const vs = it.video_start || 0, ve = it.video_end || 0;
    if(ve > vs && t >= vs && t < ve){ seg = i; break; }
  }
  if(seg !== _curSeg){
    const old = _narrElFor(_curSeg);
    if(old){ try { old.pause(); old.currentTime = 0; } catch(e){} }
    _curSeg = seg;
    if(seg >= 0){
      const el = _narrElFor(seg);
      if(el){
        try { el.currentTime = 0; } catch(e){}
        applyNarrVolume(seg);
        const pp = el.play();
        if(pp && pp.catch) pp.catch(() => {});
      }
    }
    if(seg >= 0 && seg !== _sel){ /* 播放经过时轻指示，不抢选中态 */ }
    const sb = $('stSeg');
    if(sb && seg >= 0) sb.textContent = '段 ' + (seg + 1) + '/' + _items.length;
  }
  // 叠字幕
  const ov = $('subOverlay');
  if(!ov) return;
  const it = seg >= 0 ? _items[seg] : null;
  const txt = (it && !it.quote) ? _clean(it.text) : '';
  ov.textContent = txt;
  ov.style.display = txt ? 'block' : 'none';
  updatePlayhead();
}
function setupPreview(){
  const v = $('pvVideo');
  if(!v || !(_runDir && _videoFile)) return;
  const want = '/media/' + _videoFile;
  if(v.dataset.src !== want){
    v.src = want;
    v.dataset.src = want;
    v.load();
  }
  if(!v.dataset.tickBound){
    v.dataset.tickBound = '1';
    v.addEventListener('timeupdate', previewTick);
    v.addEventListener('seeked', previewTick);
  }
  ensureNarrElements();
}

/* ── 播放控制（空格/按钮） ── */
function togglePlay(){
  const v = $('pvVideo');
  if(!v) return;
  if(v.paused){
    _curSeg = -1;
    const pp = v.play();
    if(pp && pp.catch) pp.catch(e => toast('播放失败：' + e.message, 'bad'));
  } else {
    v.pause();
    const el = _narrElFor(_curSeg);
    if(el){ try { el.pause(); } catch(e){} }
    _curSeg = -1;
  }
}

/* ── 音轨开关 ── */
function toggleNarrTrack(){
  _narrOn = !_narrOn;
  const b = $('tbNarr');
  b.classList.toggle('on', _narrOn);
  b.textContent = _narrOn ? '🎙 解说轨' : '🎙 解说轨✕';
  _items.forEach((it, i) => applyNarrVolume(i));
  const el = _narrElFor(_curSeg);
  if(el) el.muted = !_narrOn;
  refreshMenus();
}
function toggleOrigTrack(){
  _origOn = !_origOn;
  const b = $('tbOrig');
  b.classList.toggle('on', _origOn);
  b.textContent = _origOn ? '🎬 原声轨' : '🎬 原声轨✕';
  const v = $('pvVideo');
  if(v) v.muted = !_origOn;
  refreshMenus();
}
$('tbNarr').addEventListener('click', toggleNarrTrack);
$('tbOrig').addEventListener('click', toggleOrigTrack);

/* ── 撤销/重做 ── */
function _pushUndo(){ _undoStack.push(JSON.stringify(_items)); if(_undoStack.length > 50) _undoStack.shift(); _redoStack = []; }
function undoAdjust(){
  if(!_undoStack.length) return;
  _redoStack.push(JSON.stringify(_items));
  _items = JSON.parse(_undoStack.pop());
  _sel = -1;
  renderCaps(); ensureNarrElements(); renderTimeline();
}
function redoAdjust(){
  if(!_redoStack.length) return;
  _undoStack.push(JSON.stringify(_items));
  _items = JSON.parse(_redoStack.pop());
  renderCaps(); ensureNarrElements(); renderTimeline();
}

/* ── 加字幕 / 删除 ── */
function addCaptionAtPlayhead(){
  const v = $('pvVideo');
  const t = v ? (v.currentTime || 0) : 0;
  let pos = _items.length;
  for(let i = 0; i < _items.length; i++){
    if((_items[i].video_start || 0) > t){ pos = i; break; }
  }
  let maxIdx = 0;
  _items.forEach(x => { if((x.index || 0) > maxIdx) maxIdx = x.index; });
  _pushUndo();
  _items.splice(pos, 0, {index: maxIdx + 1, text: '', audio: '', audio_text: '', duration: 0,
    video_start: Math.round(t * 10) / 10, video_end: Math.round((t + 5) * 10) / 10,
    narr_volume: null, isBroll: false, quote: false});
  renderCaps(); ensureNarrElements(); renderTimeline();
  selectSeg(pos);
  const ta = document.querySelector('[data-edit="' + pos + '"]');
  if(ta){ ta.focus(); }
}
function delSelected(){
  if(_sel < 0 || _sel >= _items.length) return;
  _pushUndo();
  _items.splice(_sel, 1);
  _sel = -1;
  renderCaps(); ensureNarrElements(); renderTimeline();
}

/* ── 键盘 ── */
document.addEventListener('keydown', e => {
  const inField = /INPUT|TEXTAREA/.test((e.target.tagName || ''));
  if(e.ctrlKey && e.key === 'n'){ e.preventDefault(); newProject(); return; }
  if(inField) return;
  if(e.code === 'Space'){ e.preventDefault(); togglePlay(); }
  else if(e.key === 'ArrowLeft'){ const v = $('pvVideo'); if(v) v.currentTime = Math.max(0, v.currentTime - 1); }
  else if(e.key === 'ArrowRight'){ const v = $('pvVideo'); if(v) v.currentTime += 1; }
  else if(e.key === 'Delete' && _sel >= 0){ delSelected(); }
  else if(e.ctrlKey && e.key === 'z'){ e.preventDefault(); undoAdjust(); }
  else if(e.ctrlKey && e.key === 'y'){ e.preventDefault(); redoAdjust(); }
});

/* ── 新建/导出/打开文件夹 ── */
function newProject(){
  _pushUndo();
  _items = []; _runDir = ''; _videoFile = ''; _vdur = 0; _sel = -1; _curSeg = -1;
  _narrEls = {};
  document.querySelectorAll('audio[id^="narr-"]').forEach(a => a.remove());
  _video = null;
  $('projName').textContent = '未打开项目';
  $('tbGenerate').disabled = false;
  $('tbCompose').disabled = true;
  showEditor(false);
  switchView('media');
  $('npGo').disabled = false;
  $('npPickLabel').textContent = '从电脑选择视频';
  $('npPick').classList.remove('has');
  const ep = $('exportPanel'); if(ep) ep.style.display = 'none';
  const vs = $('videoShell'); if(vs) vs.style.display = '';
  $('npPlot').value = '';
  setStatus('就绪');
}
function openOutFolder(){
  if(window.pywebview && window.pywebview.api && _runDir){
    window.pywebview.api.open_folder(_runDir).catch(() => {});
  } else {
    toast('成片在 webui_output 目录');
  }
}
$('tbCompose').addEventListener('click', () => { if(_items.length) switchView('adjust'); });
$('tbGenerate').addEventListener('click', () => {
  if(!_video || !$('npPlot').value.trim()){ switchView('story'); return; }
  $('npGo').click();
});

/* ── 合成（含改词自动重配） ── */
$('btnCompose').addEventListener('click', async () => {
  const btn = $('btnCompose');
  btn.disabled = true;
  const skip = [], items = [];
  _items.forEach((it, i) => {
    if(it._skip){ skip.push(it.index); return; }
    items.push({index: it.index, text: it.text, audio: it.audio,
      video_start: it.video_start, video_end: it.video_end,
      audio_offset: 0, orig_volume: (it.orig_volume !== undefined ? it.orig_volume : 0),
      narr_volume: (it.narr_volume !== undefined ? it.narr_volume : null),
      isBroll: false, quote: !!it.quote});
  });
  // 改词段自动重配
  const changed = items.filter(x => {
    const it = _items.find(z => z.index === x.index);
    return it && !it.quote && x.text.trim() && x.text.trim() !== (it.audio_text || '').trim();
  });
  try {
    for(const c of changed){
      setStatus('重配改词段…', 'warn');
      const r = await fetch('/api/tts_single', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({text: c.text, run_dir: _runDir, index: c.index})});
      const out = await r.json();
      if(!out.ok) throw new Error(out.error || '重配失败');
      const res = await pollTask(out.runid);
      if(res.error) throw new Error(res.error);
      c.audio = res.audio;
      const it = _items.find(z => z.index === c.index);
      if(it) it.audio_text = c.text;
    }
    setStatus('合成中…');
    const body = { run_dir: _runDir, items: items, skip: skip, params: {
      subtitle: {fontSize: 22, color: '#FFFFFF', outlineColor: '#000000',
                 outlineWidth: 2, alignment: 2, marginV: 50},
      audio: {layers: false, exportVoice: false},
      resolution: 'original', fit: 'auto', colorStyle: 'native',
      qualityTier: 'balanced', transition: 'hard' } };
    const r = await fetch('/api/movie_compose', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const out = await r.json();
    if(!out.ok) throw new Error(out.error || '合成失败');
    pollCompose(out.runid);
  } catch(e){
    btn.disabled = false;
    setStatus('合成失败', 'bad');
    toast('合成失败：' + e.message, 'bad');
  }
});
function pollTask(runid){
  return new Promise((resolve, reject) => {
    const iv = setInterval(async () => {
      try {
        const r = await fetch('/api/progress?run=' + runid);
        const p = await r.json();
        if(p.error){ clearInterval(iv); reject(new Error(p.error)); return; }
        if(p.done && p.audio){ clearInterval(iv); resolve(p); return; }
        if(p.done){ clearInterval(iv); reject(new Error('任务结束但未返回音频')); return; }
      } catch(e){ /* 重试 */ }
    }, 1200);
  });
}
function pollCompose(runid){
  if(_pollTimer) clearInterval(_pollTimer);
  setStatus('合成中…');
  _pollTimer = setInterval(async () => {
    try {
      const r = await fetch('/api/progress?run=' + runid);
      const p = await r.json();
      if(p.error){
        clearInterval(_pollTimer);
        setStatus('合成失败', 'bad');
        toast('合成失败：' + p.error, 'bad');
        return;
      }
      if(p.done && p.file){
        clearInterval(_pollTimer);
        setStatus('✅ 成片已生成', 'ok');
        openExport(p.file);
      }
    } catch(e){ /* 抖动 */ }
  }, 1200);
}
function openExport(file){
  switchView('export');
  $('exportPanel').style.display = 'block';   // 导出视图替换预览区
  $('videoShell').style.display = 'none';
  $('exPlayer').src = '/media/' + file;
  $('exPath').textContent = file;
  if(window.pywebview && window.pywebview.api)
    window.pywebview.api.open_folder(file).catch(() => {});
  const btn = $('btnCompose');
  btn.disabled = false;
  btn.textContent = '🎬 合成视频';
}

/* ── 启动 ── */
setStatus('就绪');
