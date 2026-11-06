/* 一帧成片 · 桌面版逻辑（调本地服务既有接口；浏览器打开此页时桌面能力自动降级） */
'use strict';

/* ── 状态 ── */
let _video = null;            // {name, path, isLocalPath} 或 File
let _token = '';              // 桌面会话令牌
let _isDesktop = false;
let _runDir = '';             // 当前任务目录
let _items = [];              // 调整段（含 _skip 标记）
let _videoFile = '';          // 源片在 run_dir 里的相对路径
let _vdur = 0;
let _selected = -1;
let _pollTimer = null;
let _audioText = {};          // index → 该段音频对应的文字（改词检测基线）

const $ = id => document.getElementById(id);

/* ── 视图切换 ── */
function showView(v){
  document.querySelectorAll('.view').forEach(el => el.classList.remove('active'));
  const el = $('view-' + v);
  if(el) el.classList.add('active');
  document.querySelectorAll('.rail-step').forEach(b => {
    b.classList.toggle('active', b.dataset.view === v);
    const n = {media:1, story:2, adjust:3, export:4}[b.dataset.view];
    b.classList.toggle('done', n < ({media:1, story:2, adjust:3, export:4}[v] || 1));
  });
}
document.querySelectorAll('.rail-step').forEach(b =>
  b.addEventListener('click', () => {
    if(b.dataset.view === 'adjust' && !_runDir){ toast('先生成配音与方案'); return; }
    if(b.dataset.view === 'export' && !_runDir){ toast('还没有可导出的任务'); return; }
    showView(b.dataset.view);
  }));

function toast(msg, kind){
  const t = document.createElement('div');
  t.className = 'toast' + (kind ? ' ' + kind : '');
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 300); }, 2600);
}

/* ── 桌面模式初始化 ── */
function initDesktop(){
  const api = window.pywebview && window.pywebview.api;
  if(!api || !api.get_desktop_info) return;
  api.get_desktop_info().then(info => {
    if(!info || !info.token) return;
    _token = info.token;
    _isDesktop = true;
    $('verTag').textContent = info.version || '桌面版';
  }).catch(e => console.warn('桌面初始化失败', e));
}
window.addEventListener('pywebviewready', initDesktop);
if(window.pywebview) initDesktop();

/* ── 视图 1：选素材 ── */
$('pickCard').addEventListener('click', () => {
  if(_isDesktop && window.pywebview && window.pywebview.api){
    window.pywebview.api.pick_video().then(r => {
      if(r && r.path) setVideo({name: r.name || r.path.split(/[\\/]/).pop(), path: r.path, isLocalPath: true});
    }).catch(e => toast('选择失败：' + e.message, 'bad'));
  } else {
    toast('请在浏览器版中使用文件选择，或运行桌面版 EXE', 'bad');
  }
});
$('pickCard').addEventListener('keydown', e => { if(e.key === 'Enter' || e.key === ' ') $('pickCard').click(); });

function setVideo(v){
  _video = v;
  $('pickTitle').textContent = '✅ ' + v.name;
  $('pickMeta').textContent = v.isLocalPath ? '本地直连 · 免上传 · 点此可重新选择' : '已加载 · 点此可重新选择';
  $('videoNameField').style.display = 'block';
  $('btnGenerate').disabled = false;
  updateGenHint();
  showView('story');
}
function updateGenHint(){
  const plot = $('plotText').value.trim();
  const isScript = $('optScript').checked;
  $('btnGenerate').disabled = !(_video && plot);
  $('genHint').textContent = !_video ? '先选择视频'
    : !plot ? '写下剧情或粘贴完整解说稿'
    : isScript ? '将按稿找画面配音（文字不改）' : '将按剧情写解说、挑画面';
}
$('plotText').addEventListener('input', updateGenHint);
$('optScript').addEventListener('change', updateGenHint);

/* ── 视图 2：生成 ── */
$('btnGenerate').addEventListener('click', async () => {
  const plot = $('plotText').value.trim();
  const isScript = $('optScript').checked;
  if(!_video || !plot) return;
  const btn = $('btnGenerate');
  btn.disabled = true;
  const vobj = _video.isLocalPath
    ? {name: _video.name, path: _video.path, desktop_token: _token}
    : null;
  const body = {
    video: vobj || _video,
    plot: isScript ? '' : plot,
    script: isScript ? plot : undefined,
    params: {
      maxSeg: 25, autoCut: $('optCut').checked, targetSec: 600,
      narr_style: 'movie', detail_level: 'balanced', genre: 'auto',
      subtitle: {fontSize: 22, color: '#FFFFFF', outlineColor: '#000000',
                 outlineWidth: 2, alignment: 2, marginV: 50},
      audio: {layers: false, exportVoice: false},
      resolution: 'original', fit: 'auto', colorStyle: 'native',
      qualityTier: 'balanced', transition: 'hard'
    }
  };
  if($('optBgm').checked){ /* 配乐在调整页可再补；v1 用默认不选 */ }
  try {
    const r = await fetch('/api/movie_tts', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const out = await r.json();
    if(!out.ok) throw new Error(out.error || '提交失败');
    $('genHint').textContent = '已提交，正在生成（首次加载模型较慢）…';
    showView('adjust');
    pollTTS(out.runid);
  } catch(e){
    btn.disabled = false;
    toast('提交失败：' + e.message, 'bad');
  }
});

let _runid = '';
function pollTTS(runid){
  _runid = runid;
  $('genProgress').style.display = 'block';
  showView('adjust');
  if(_pollTimer) clearInterval(_pollTimer);
  _pollTimer = setInterval(async () => {
    try {
      const r = await fetch('/api/progress?run=' + runid);
      const p = await r.json();
      $('pfill').style.width = Math.min(100, p.pct || 0) + '%';
      $('ppct').textContent = (p.pct || 0) + '%';
      $('pphase').textContent = p.phase || '处理中…';
      if(p.error){
        clearInterval(_pollTimer);
        $('pphase').textContent = '❌ ' + p.error;
        toast('生成失败：' + p.error, 'bad');
        return;
      }
      if(p.done && p.tts_list && p.tts_list.length){
        clearInterval(_pollTimer);
        _runDir = p.run_dir || '';
        _videoFile = p.video_file || '';
        await loadAdjustItems(_runDir);
        $('genProgress').style.display = 'none';
        toast('✅ 配音与画面方案已生成（' + p.tts_list.length + ' 段）');
      }
    } catch(e){ /* 瞬时网络抖动忽略，下轮再取 */ }
  }, 1200);
}

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
  renderCaps();
  renderTimeline();
  setupPreview();
  showView('adjust');
}

/* ── 视图 3：调整 ── */
function _esc(s){
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function _clean(s){
  return String(s || '').replace(/\{(?:情绪|停顿|慢|快|高音|低音|大声|小声)[^}]*\}/g, '')
    .replace(/\{\/(?:情绪|停顿|慢|快|高音|低音|大声|小声)\}/g, '').replace(/\s+/g, ' ').trim();
}
function _fmt(sec){
  const m = Math.floor((sec || 0) / 60), s = Math.floor((sec || 0) % 60);
  return (m < 10 ? '0' : '') + m + ':' + (s < 10 ? '0' : '') + s;
}

function renderCaps(){
  const list = $('capList');
  list.innerHTML = _items.map((it, i) => {
    const vs = it.video_start || 0;
    if(it.quote){
      return '<div class="cap-row" data-i="' + i + '"><div class="row-top">'
        + '<span class="cap-time" style="color:#34d399;background:rgba(16,185,129,.1)">' + _fmt(vs) + '</span>'
        + '<span class="cap-quote">🎬 原声引用 ' + (+vs).toFixed(1) + '-' + (+(it.video_end||0)).toFixed(1) + 's · 保留原片声音（右键时间轴可恢复）</span>'
        + '<button class="cap-del" data-del="' + i + '">✕</button></div></div>';
    }
    return '<div class="cap-row" data-i="' + i + '"><div class="row-top">'
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
    ta.addEventListener('change', () => {
      const i = parseInt(ta.dataset.edit, 10);
      if(_items[i]) _items[i].text = ta.value.trim();
    });
  });
  list.querySelectorAll('[data-del]').forEach(b => {
    b.addEventListener('click', () => {
      const i = parseInt(b.dataset.del, 10);
      _items.splice(i, 1);
      if(_selected === i) _selected = -1;
      renderCaps(); renderTimeline();
    });
  });
}

function selectSeg(i){
  _selected = i;
  const it = _items[i];
  if(!it) return;
  document.querySelectorAll('.cap-row').forEach((r, k) =>
    r.classList.toggle('sel', k === i));
  document.querySelectorAll('.tl-block').forEach((b, k) =>
    b.classList.toggle('sel', k === i));
  seekTo(it.video_start || 0);
}

function seekTo(t){
  const v = $('pvVideo');
  if(v && v.readyState >= 1){ try { v.currentTime = t; } catch(e){} }
}

/* ── 时间轴 ── */
const TL_COLORS = ['#6c8cff','#8b5cf6','#ec4899','#f59e0b','#10b981','#06b6d4','#ef4444','#84cc16'];
function renderTimeline(){
  const inner = $('tlInner');
  const total = Math.max(10, Math.ceil(_vdur));
  inner.style.width = Math.max(600, total * 10) + 'px';
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
    const cls = it.quote ? ' tl-quote' : '';
    html += '<div class="tl-block' + cls + '" data-i="' + i + '" style="left:' + left + '%;width:'
      + w + '%;background:' + (it.quote ? '#10b981' : TL_COLORS[i % TL_COLORS.length]) + '">'
      + '<span>' + (it.quote ? '🎬' : '#' + (i + 1)) + '</span></div>';
    cum += vd;
  });
  $('tlTrack').innerHTML = html;
  $('tlTrack').querySelectorAll('.tl-block').forEach(b => {
    b.addEventListener('click', () => selectSeg(parseInt(b.dataset.i, 10)));
  });
}

/* ── 预览：叠加字幕 + 音轨开关 ── */
let _narrOn = true, _origOn = true;
$('tglNarr').addEventListener('click', function(){
  _narrOn = !_narrOn;
  this.classList.toggle('on', _narrOn);
  this.textContent = (_narrOn ? '🎙 解说轨：开' : '🎙 解说轨：禁音');
  syncPreviewAudio();
});
$('tglOrig').addEventListener('click', function(){
  _origOn = !_origOn;
  this.classList.toggle('on', _origOn);
  this.textContent = (_origOn ? '🎬 原声轨：开' : '🎬 原声轨：禁音');
  syncPreviewAudio();
});
function syncPreviewAudio(){
  const v = $('pvVideo');
  if(v) v.muted = !_origOn;
  applyNarrVolume();
}
function _curNarrEl(){
  return document.getElementById('narr-' + _playSeg);
}
let _playSeg = -1;
function applyNarrVolume(){
  const el = _curNarrEl();
  if(!el) return;
  const it = _items[_playSeg];
  const nv = it ? it.narr_volume : null;
  try { el.volume = (nv === undefined || nv === null) ? 1 : Math.max(0, Math.min(1, nv / 100)); } catch(e){}
  el.muted = !_narrOn;
}
function setupPreview(){
  const v = $('pvVideo');
  if(!v || !(_runDir && _videoFile)) return;
  const want = '/media/' + _videoFile;
  if(v.dataset.src !== want){ v.src = want; v.dataset.src = want; }
  v.onloadedmetadata = () => { _vdur = Math.max(_vdur, v.duration || 0); renderTimeline(); };
  v.ontimeupdate = () => {
    const t = v.currentTime || 0;
    // 当前段
    let seg = -1;
    for(let i = 0; i < _items.length; i++){
      const vs = _items[i].video_start || 0, ve = _items[i].video_end || 0;
      if(ve > vs && t >= vs && t < ve){ seg = i; break; }
    }
    if(seg !== _playSeg){
      const old = document.getElementById('narr-' + _playSeg);
      if(old){ try { old.pause(); } catch(e){} }
      _playSeg = seg;
      if(seg >= 0){
        const el = document.getElementById('narr-' + seg);
        if(el && _items[seg].audio && !_items[seg].quote){
          try { el.currentTime = 0; } catch(e){}
          const pp = el.play();
          if(pp && pp.catch) pp.catch(() => {});
        }
        applyNarrVolume();
      }
    }
    // 叠字幕
    const ov = $('subOverlay');
    if(!ov) return;
    const it = seg >= 0 ? _items[seg] : null;
    const txt = (it && !it.quote) ? _clean(it.text) : '';
    ov.textContent = txt;
    ov.style.display = txt ? 'block' : 'none';
  };
}

/* ── 配音轨音频元素：随段渲染（预载） ── */
function ensureNarrElements(){
  _items.forEach((it, i) => {
    if(it.quote || !it.audio) return;
    if(!document.getElementById('narr-' + i)){
      const a = document.createElement('audio');
      a.id = 'narr-' + i;
      a.preload = 'auto';
      a.src = '/media/' + it.audio;
      a.style.display = 'none';
      document.body.appendChild(a);
    }
  });
}

/* ── ➕ 加字幕 / ✕ 删除 ── */
$('btnAddCap').addEventListener('click', () => {
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
  const ta = document.querySelector('[data-edit="' + pos + '"]');
  if(ta){ ta.focus(); ta.scrollIntoView({block: 'nearest'}); }
});

let _undoStack = [], _redoStack = [];
function _pushUndo(){ _undoStack.push(JSON.stringify(_items)); if(_undoStack.length > 50) _undoStack.shift(); _redoStack = []; }
function undoAdjust(){
  if(!_undoStack.length) return;
  _redoStack.push(JSON.stringify(_items));
  _applyItems(JSON.parse(_undoStack.pop()));
}
function redoAdjust(){
  if(!_redoStack.length) return;
  _undoStack.push(JSON.stringify(_items));
  _applyItems(JSON.parse(_redoStack.pop()));
}
function _applyItems(items){
  _items = items;
  renderCaps(); ensureNarrElements(); renderTimeline();
}
document.addEventListener('keydown', e => {
  if(e.ctrlKey && e.key === 'z' && !$('plotText').contains(document.activeElement)){ e.preventDefault(); undoAdjust(); }
  if(e.ctrlKey && (e.key === 'y' || (e.shiftKey && e.key === 'Z'))){ e.preventDefault(); redoAdjust(); }
});

/* ── 合成 ── */
$('btnCompose').addEventListener('click', async () => {
  const btn = $('btnCompose');
  btn.disabled = true;
  btn.textContent = '⏳ 检测改词…';
  // 改词段自动重配（audio_text ≠ text → /api/tts_single）
  const skip = [], items = [];
  _items.forEach((it, i) => {
    if(it._skip){ skip.push(it.index); return; }
    items.push({
      index: it.index, text: it.text, audio: it.audio,
      video_start: it.video_start, video_end: it.video_end,
      audio_offset: 0, orig_volume: 0, narr_volume: it.narr_volume,
      isBroll: false, quote: it.quote
    });
  });
  const changed = items.filter(x => {
    const it = _items.find(z => z.index === x.index);
    return it && (x.text.trim() !== (it.audio_text || '').trim()) && x.text.trim();
  });
  try {
    for(const c of changed){
      const pos = _items.findIndex(z => z.index === c.index);
      btn.textContent = '⏳ 重配改词段…';
      const r = await fetch('/api/tts_single', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({text: c.text, run_dir: _runDir, index: c.index})});
      const out = await r.json();
      if(!out.ok) throw new Error(out.error || '重配失败');
      const res = await pollTask(out.runid, m => {});
      if(res.error) throw new Error(res.error);
      c.audio = res.audio;
      const it = _items.find(z => z.index === c.index);
      if(it) it.audio_text = c.text;
    }
    btn.textContent = '⏳ 合成中…';
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
    btn.disabled = false; btn.textContent = '🎬 确认配音，合成视频';
    toast('合成失败：' + e.message, 'bad');
  }
});

function pollCompose(runid){
  showView('export');
  $('exDone').style.display = 'none';
  $('exErr').style.display = 'none';
  $('exProgress').style.display = 'block';
  if(_pollTimer) clearInterval(_pollTimer);
  _pollTimer = setInterval(async () => {
    try {
      const r = await fetch('/api/progress?run=' + runid);
      const p = await r.json();
      $('exFill').style.width = Math.min(100, p.pct || 0) + '%';
      $('exPct').textContent = (p.pct || 0) + '%';
      $('exPhase').textContent = p.phase || '合成中…';
      if(p.error){
        clearInterval(_pollTimer);
        $('exProgress').style.display = 'none';
        const er = $('exErr'); er.style.display = 'block';
        er.textContent = '❌ ' + p.error;
        return;
      }
      if(p.done && p.file){
        clearInterval(_pollTimer);
        $('exProgress').style.display = 'none';
        $('exDone').style.display = 'block';
        $('exPlayer').src = '/media/' + p.file;
        $('exPath').textContent = p.file;
        if(window.pywebview && window.pywebview.api)
          window.pywebview.api.open_folder(p.file).catch(() => {});
      }
    } catch(e){ /* 抖动忽略 */ }
  }, 1200);
}

function pollTask(runid, onP){
  return new Promise((resolve, reject) => {
    const iv = setInterval(async () => {
      try {
        const r = await fetch('/api/progress?run=' + runid);
        const p = await r.json();
        if(onP) onP(p);
        if(p.error){ clearInterval(iv); reject(new Error(p.error)); return; }
        if(p.done && p.audio){ clearInterval(iv); resolve(p); return; }
        if(p.done){ clearInterval(iv); reject(new Error('配音任务结束但未返回音频')); return; }
      } catch(e){ /* 抖动重试 */ }
    }, 1200);
  });
}

/* ── 打开文件夹（桌面能力；网页无 pywebview 时提示路径） ── */
$('btnOpenFolder').addEventListener('click', () => {
  const rel = $('exPath').textContent;
  if(window.pywebview && window.pywebview.api){
    window.pywebview.api.open_folder(rel).catch(() => {});
  } else {
    toast('文件在 webui_output / ' + rel);
  }
});

/* ── 轻提示 ── */
function toastStyle(){
  const t = document.createElement('style');
  t.textContent = '.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);' +
    'background:#262c3a;color:var(--txt);padding:10px 20px;border-radius:9px;font-size:13.5px;' +
    'z-index:999;box-shadow:0 8px 24px rgba(0,0,0,.5);border:1px solid var(--line2);transition:opacity .3s}' +
    '.toast.bad{border-color:var(--bad);color:#fca5a5}';
  document.head.appendChild(t);
}
toastStyle();
