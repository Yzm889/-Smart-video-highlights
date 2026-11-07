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
let _pollRunId = '';         // 当前生成任务（供取消）
let _lastExport = '';         // 最近一次合成成片（供打开文件夹）
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
  let wrap = document.getElementById('toastWrap');
  if(!wrap){
    wrap = document.createElement('div');
    wrap.id = 'toastWrap';
    wrap.className = 'toast-wrap';
    document.body.appendChild(wrap);
  }
  while(wrap.children.length >= 3) wrap.removeChild(wrap.firstChild);
  const t = document.createElement('div');
  t.className = 'toast' + (kind === 'bad' ? ' bad' : (kind === 'ok' || /✅/.test(msg) ? ' ok' : ''));
  t.setAttribute('role', 'alert');
  t.textContent = msg;
  wrap.appendChild(t);
  setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 260); }, 2600);
}
function setBtnIcon(id, icon, text){
  const b = $(id);
  if(b) b.innerHTML = '<svg class="ic"><use href="#' + icon + '"/></svg>' + text;
}
function setStatus(s, kind){
  const el = $('stState');
  el.textContent = '● ' + s;
  el.className = kind === 'bad' ? 'bad' : (kind === 'ok' ? 'ok' : (kind === 'warn' ? 'warn' : ''));
  el.id = 'stState';
}

/* ── 视图切换 ── */
function showEditor(on){
  $('editor').style.display = on ? 'flex' : 'none';
  $('newProj').style.display = on ? 'none' : 'flex';
}
function switchView(v){
  /* adjust = 编辑器（预览+时间轴）；export = 导出面板替换预览区；
     其余 = 新建项目卡。保留函数名以兼容调用点。 */
  const inExport = v === 'export';
  const vs = $('videoShell'), tl = $('tlCard'), ep = $('exportPanel');
  if(vs) vs.style.display = inExport ? 'none' : '';
  if(tl) tl.style.display = inExport ? 'none' : '';
  if(ep) ep.style.display = inExport ? 'flex' : 'none';
  if(v === 'adjust' || inExport){ showEditor(true); return; }
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
document.querySelectorAll('.menu').forEach(m => {   // 已展开时划过即切换（桌面软件惯例）
  m.addEventListener('mouseenter', () => {
    if(document.querySelector('.menu.open') && !m.classList.contains('open')){
      document.querySelectorAll('.menu').forEach(x => x.classList.remove('open'));
      m.classList.add('open');
    }
  });
});
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
  history(){ openHistory(); },
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
    '解说轨：' + (_narrOn ? '✓ 开' : '禁音');
  document.querySelector('[data-act="tgorig"]').textContent =
    '原声轨：' + (_origOn ? '✓ 开' : '禁音');
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

/* ── 第一步：环境检查（装机引导） ──
   渐进渲染：各接口独立落地、各自刷新——快行先显示，不被慢探测（模型 ping 可达 10s+）拖住。
   【曾犯的错】v1 把整卡渲染压在 Promise.all 后面，冷启动时"检查中…"卡半分钟还锁死重试。 */
const _envData = {local: null, wsp: null, vlm: null, ai: null};
let _envActive = false;   // 有拉取/下载/安装任务时 2.5s 加密轮询
let _envAutoEnabled = false;   // 已自动打开「启用本地模型」开关（一次）

function _envGet(u){
  const c = new AbortController();
  const k = setTimeout(() => c.abort(), 15000);   // 挂死的探测不能拖死整卡
  return fetch(u, {signal: c.signal}).then(r => r.json())
    .catch(() => null).finally(() => clearTimeout(k));
}

function renderEnv(){
  const box = $('envRows');
  if(!box || $('newProj').style.display === 'none') return;
  const L = _envData.local, W = _envData.wsp, V = _envData.vlm, A = _envData.ai;
  const okB = t => '<span class="env-badge ok">✓ ' + t + '</span>';
  const noB = t => '<span class="env-badge bad">' + t + '</span>';
  const wB  = t => '<span class="env-badge wait">' + t + '</span>';
  const chk = t => '<span class="env-badge wait">检测中</span>' + (t || '');
  const btn = (id, label, ghost) => '<button class="env-btn' + (ghost ? ' ghost' : '') + '" data-env="' + id + '">' + label + '</button>';
  const rows = [];
  const lm = (L && L.model) || 'qwen3:8b';

  // ① Ollama 引擎：服务真实状态（与配置解耦，直探 11434）
  //   【实测踩中】旧版拿配置 ping 当服务状态——EXE 无配置快照时 enabled=false，
  //   Ollama 明明在跑也显示未就绪，还会误触 1.5GB 重装（已装环境重装必失败）。
  const ollamaUp = !!(L && L.ollama_running);
  if(L && L.ollama_installing){
    rows.push('<div class="env-row"><div class="en-name">Ollama ' + (L.ollama_install_phase || '处理中') + '<small>'
      + (L.ollama_install_msg || '') + '</small></div>' + wB((L.ollama_install_pct || 0) + '%') + '</div>');
  } else {
    rows.push('<div class="env-row"><div class="en-name">Ollama 本地引擎<small>AI 运行时（免费 · 全程本地）</small></div>'
      + (!L ? chk() : (ollamaUp ? okB('运行中')
        : ((L && L.ollama_installed) ? noB('已安装·未运行') + btn('install', '启动')
                                     : noB('未安装') + btn('install', '安装（约1.5GB）')))) + '</div>');
  }

  // ② 文字大脑（写解说稿，必装）：看模型是否真在 Ollama 里，不依赖配置开关
  let txtOk = false;
  if(L && L.pulling){
    rows.push('<div class="env-row"><div class="en-name">文字大脑 ' + (L.pull_model || lm) + '<small>' + (L.pull_msg || '拉取中') + '</small></div>' + wB((L.pull_pct || 0) + '%') + '</div>');
  } else if(!L){
    rows.push('<div class="env-row"><div class="en-name">文字大脑 ' + lm + '<small>写解说稿的核心（约 4.7GB，一次下载）</small></div>' + chk() + '</div>');
  } else {
    txtOk = (L.installed || []).some(m => (m || '').split(':')[0] === lm.split(':')[0]);
    const sub = txtOk ? (L.enabled ? '写解说稿的核心' : '写解说稿的核心 · 已自动启用本地模型')
                      : (ollamaUp ? '约 4.7GB，一次下载' : '先启动上方 Ollama');
    rows.push('<div class="env-row"><div class="en-name">文字大脑 ' + lm + '<small>' + sub + '</small></div>'
      + (txtOk ? okB('就绪') : (ollamaUp ? noB('未拉取') + btn('pulltext', '拉取') : wB('等待'))) + '</div>');
    // 模型已在而配置开关没开（EXE 首跑无配置快照）：自动打开，否则生成链路不走本地
    if(txtOk && L.enabled === false && !_envAutoEnabled){
      _envAutoEnabled = true;
      fetch('/api/ai/config', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({local: {enabled: true, base_url: 'http://localhost:11434/v1', model: lm}})})
        .catch(() => { _envAutoEnabled = false; });
    }
  }

  // ③ 看图模型（可选）
  if(V && V.pulling){
    rows.push('<div class="env-row"><div class="en-name">看图模型 ' + (V.pull_model || V.model || 'minicpm-v') + '<small>拉取中</small></div>' + wB((V.pull_pct || 0) + '%') + '</div>');
  } else {
    const vOk = !!(V && V.ready);
    const vOpt = !!(V && V.enabled === false);   // 明确关闭才算"可选"，超时/未启用保持检测中
    rows.push('<div class="env-row"><div class="en-name">看图模型 ' + ((V && V.model) || 'minicpm-v') + '<small>可选：AI 亲眼看画面，理解更准</small></div>'
      + (!V ? chk() : (vOk ? okB('就绪') : (vOpt ? '<span class="env-badge wait">可选</span>' + btn('pullvlm', '拉取', true)
                                              : noB('未就绪') + btn('pullvlm', '拉取', true)))) + '</div>');
  }

  // ④ Whisper 台词识别
  if(W && W.downloading){
    rows.push('<div class="env-row"><div class="en-name">台词识别 ' + (W.download_model || 'large-v3') + '<small>听懂台词，时间轴零偏差</small></div>' + wB((W.download_pct || 0) + '%') + '</div>');
  } else {
    const wOk = !!(W && W.ready);
    rows.push('<div class="env-row"><div class="en-name">台词识别 ' + ((W && W.selected) || 'large-v3') + '<small>听懂台词（2.9GB，一次下载）</small></div>'
      + (!W ? chk() : (wOk ? okB('就绪') : noB('未下载') + btn('dlwsp', '下载'))) + '</div>');
  }

  // ⑤ 配音引擎（信息展示；引擎安装/音色在设置里）
  const vc = A && A.voice;
  rows.push('<div class="env-row"><div class="en-name">配音引擎<small>' + ((vc && vc.ready && vc.label) ? vc.label : ((vc && vc.reason) || '检测中')) + '</small></div>'
    + (!A ? chk() : (vc ? (vc.ready ? okB('就绪') : noB('未就绪')) : wB('…'))) + '</div>');

  const coreOk = ollamaUp && txtOk && !!(W && W.ready);
  rows.unshift('<div class="env-row" style="border-color:' + (coreOk ? 'var(--ok)' : 'var(--warn)') + '">'
    + '<div class="en-name" style="font-size:13px">' + (coreOk
      ? '✅ 环境就绪——选片，开始创作'
      : '⏳ 还差以下组件（点按钮自动完成，约需几分钟）') + '</div></div>');
  box.innerHTML = rows.join('');
  _envActive = !!(L && L.pulling) || !!(W && W.downloading) || !!(V && V.pulling) || !!(L && L.ollama_installing);
}

function refreshEnv(){
  if($('newProj').style.display === 'none') return;
  _envGet('/api/local/status').then(j => { if(j){ _envData.local = j; renderEnv(); } });
  _envGet('/api/whisper/status').then(j => { if(j){ _envData.wsp = j; renderEnv(); } });
  _envGet('/api/vlm/status').then(j => { if(j){ _envData.vlm = j; renderEnv(); } });
  _envGet('/api/ai_status').then(j => { if(j){ _envData.ai = j; renderEnv(); } });
}

$('envRows').addEventListener('click', async e => {   // 事件委托：重渲染不丢按钮
  const b = e.target.closest('[data-env]');
  if(!b) return;
  const act = b.dataset.env;
  b.disabled = true; b.textContent = '⏳ 处理中…';
  const post = (u, d) => fetch(u, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(d || {})});
  try {
    if(act === 'install'){ await post('/api/ollama/install'); }
    if(act === 'pulltext'){ await post('/api/local/pull', {model: (_envData.local && _envData.local.model) || 'qwen3:8b'}); }
    if(act === 'pullvlm'){ await post('/api/vlm/pull', {model: (_envData.vlm && _envData.vlm.model) || 'minicpm-v'}); }
    if(act === 'dlwsp'){ await post('/api/whisper/download', {model: (_envData.wsp && _envData.wsp.selected) || 'large-v3'}); }
    setTimeout(refreshEnv, 2000);
  } catch(err){ toast('操作失败：' + err.message, 'bad'); refreshEnv(); }
});
$('envRefresh').addEventListener('click', refreshEnv);
setInterval(() => {                       // 拉取/下载中 2.5s 加密刷新
  if(!_envActive || $('newProj').style.display === 'none') return;
  refreshEnv();
}, 2500);
setInterval(refreshEnv, 15000);           // 平时 15s 慢巡检（探测有成本）
setTimeout(refreshEnv, 400);

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
$('npBgm').addEventListener('change', () => {
  if($('npBgm').checked && !_music && window.pywebview && window.pywebview.api) pickLocalMusic();
});

/* ── 高级设置（生成与合成共用一套参数，改动自动记忆） ── */
const ADV_KEY = 'framecut_desktop_adv_v1';
const ADV_IDS = ['advTarget','advRes','advColor','advTrans','advQual','advBurn',
  'advFontSize','advColor1','advOutline','advOutlineW','advAlign','advMarginV','advLayers','advVoice'];
function _advParams(){
  return {
    maxSeg: 25, autoCut: $('npCut').checked,
    targetSec: parseFloat($('advTarget').value) || 0,   // 0 = 全片（后端回落视频时长）
    narr_style: 'movie', detail_level: 'balanced', genre: 'auto',
    subtitle: {fontSize: +$('advFontSize').value || 22, color: $('advColor1').value || '#FFFFFF',
               outlineColor: $('advOutline').value || '#000000',
               outlineWidth: ($('advOutlineW').value === '' ? 2 : +$('advOutlineW').value),
               alignment: +$('advAlign').value || 2, marginV: +$('advMarginV').value || 50,
               burn: $('advBurn').value !== '0'},
    audio: {layers: $('advLayers').value === '1' ? true : ($('advLayers').value === '2' ? 2 : false),
            exportVoice: $('advVoice').value === '1'},
    resolution: $('advRes').value || 'original', fit: 'auto',
    colorStyle: $('advColor').value || 'native',
    qualityTier: $('advQual').value || 'balanced',
    transition: $('advTrans').value || 'hard'
  };
}
function advSave(){
  const v = {};
  ADV_IDS.forEach(id => { const el = $(id); if(el) v[id] = el.value; });
  try { localStorage.setItem(ADV_KEY, JSON.stringify(v)); } catch(e){}
}
function advLoad(){
  let v = null;
  try { v = JSON.parse(localStorage.getItem(ADV_KEY) || 'null'); } catch(e){}
  if(!v) return;
  ADV_IDS.forEach(id => { const el = $(id); if(el && v[id] !== undefined) el.value = v[id]; });
}
$('npAdvToggle').addEventListener('click', () => {
  const b = $('npAdvBody');
  const show = b.style.display === 'none';
  b.style.display = show ? 'block' : 'none';
  $('npAdvArrow').textContent = show ? '收起 ▴' : '展开 ▾';
});
ADV_IDS.forEach(id => { const el = $(id); if(el) el.addEventListener('change', advSave); });
advLoad();

/* ── 查剧情（与网页版同一条 /api/deepseek_plot 链，仅加 UI 入口） ── */
$('npPlotLookup').addEventListener('click', async () => {
  const title = $('npName').value.trim();
  if(!title){ toast('先填片名再查剧情（或在剧情框直接写）', 'bad'); return; }
  const b = $('npPlotLookup');
  b.disabled = true; b.textContent = '⏳ 查询中…';
  try {
    const r = await fetch('/api/deepseek_plot', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({title: title, which: 'nar'})});
    const out = await r.json();
    if(!out.ok) throw new Error(out.error || '查询失败');
    $('npPlot').value = (out.plot || '').trim();
    updateNewProj();
    toast('✅ 剧情已填入，可修改后开始生成');
  } catch(e){ toast('查剧情失败：' + e.message, 'bad'); }
  b.disabled = false; b.textContent = '🔎 查剧情';
});

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
    params: _advParams(),
  };
  if(_srt) body.subtitle_file = {name: _srt.name, text: _srt.text};
  if(_music && $('npBgm').checked) body.music = {name: _music.name, path: _music.path, desktop_token: _token};
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
  _pollRunId = runid;
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
$('gmCancel').addEventListener('click', async () => {
  if(_pollRunId){
    try { await fetch('/api/cancel', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({runid: _pollRunId})}); } catch(e){ /* 尽力取消 */ }
  }
  if(_pollTimer) clearInterval(_pollTimer);
  $('genMask').style.display = 'none';
  $('npGo').disabled = false;
  toast('已取消生成');
});

/* ── 加载调整数据 ── */
async function loadAdjustItems(runDir){
  _runDir = runDir;   // 恢复项目/最近任务进来时也要有 _runDir（否则合成守卫拦截）
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
  $('tbCompose').disabled = false;
  $('stEngine').textContent = _engineSummary();
}
function _engineSummary(){
  const parts = [];
  if(_envData.local && _envData.local.model) parts.push('大脑 ' + _envData.local.model);
  const v = _envData.ai && _envData.ai.voice;
  if(v && v.ready && v.label) parts.push(v.label);
  return parts.join(' · ');
}

/* ── 字幕面板（左） ── */
function renderCaps(){
  const list = $('capList');
  if(!_items.length){
    list.innerHTML = '<div class="empty-state">生成后，字幕段会出现在这里<small>也可播放到任意位置点「指针处添加」</small></div>';
    return;
  }
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
    ta._snap = JSON.stringify(_items);
    ta.addEventListener('change', () => {
      if(ta._snap && ta._snap !== JSON.stringify(_items)){
        _undoStack.push(ta._snap);
        if(_undoStack.length > 50) _undoStack.shift();
        _redoStack = [];
      }
      scheduleStatus();
    });
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
      !_items[i]._skip && _clean(it.text) !== _clean(it.audio_text || '')).length;
    setStatus(n ? n + ' 段改词待重配（合成时自动）' : '就绪', n ? 'warn' : '');
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
  if(!it){
    body.innerHTML = '<div class="empty-state">点选一段字幕<small>在这里调音量、改画面范围、设原声引用</small></div>';
    return;
  }
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
let _pxPerSec = 12;   // 缩放：4~80
function renderTimeline(){
  const inner = $('tlInner');
  const total = Math.max(10, Math.ceil(_vdur));
  inner.style.width = Math.max(600, total * _pxPerSec) + 'px';
  const zv = $('tlZoomVal');
  if(zv) zv.textContent = Math.round(_pxPerSec) + 'px/s';
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
    const tip = _esc(_clean(it.text).slice(0, 60)) || (it.quote ? '原声引用' : '');
    html += '<div class="tl-block' + cls + '" data-i="' + i + '" title="' + tip + '" style="left:' + left + '%;width:'
      + w + '%;background:' + (it.quote ? '#10b981' : TL_COLORS[i % TL_COLORS.length]) + '">'
      + '<span>' + (it.quote ? '原声' : '#' + (i + 1)) + '</span></div>';
    cum += vd;
  });
  $('tlTrack').innerHTML = html;
  $('tlTrack').querySelectorAll('.tl-block').forEach(b => {
    b.addEventListener('click', () => selectSeg(parseInt(b.dataset.i, 10)));
  });
  updatePlayhead();
}
(function(){
  const sc = $('tlScroll');
  let scrubbing = false;
  const seekAt = e => {
    const rect = sc.getBoundingClientRect();
    const x = e.clientX - rect.left + sc.scrollLeft;
    const total = Math.max(10, _vdur || 0);
    const t = Math.max(0, Math.min(total, x / _pxPerSec));
    const v = $('pvVideo');
    if(v && v.duration) v.currentTime = Math.min(t, Math.max(0, v.duration - 0.05));
  };
  sc.addEventListener('mousedown', e => {
    if(e.target.closest('.tl-block')) return;   // 色块点击仍是选中
    scrubbing = true;
    seekAt(e);
  });
  window.addEventListener('mousemove', e => { if(scrubbing) seekAt(e); });
  window.addEventListener('mouseup', () => scrubbing = false);
  const zm = dir => {
    _pxPerSec = Math.max(4, Math.min(80, _pxPerSec * (dir > 0 ? 1.4 : 1 / 1.4)));
    renderTimeline();
  };
  const zi = $('tlZoomIn'), zo = $('tlZoomOut');
  if(zi) zi.addEventListener('click', () => zm(1));
  if(zo) zo.addEventListener('click', () => zm(-1));
})();
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
  setBtnIcon('tbNarr', 'i-mic', _narrOn ? '解说轨' : '解说轨✕');
  _items.forEach((it, i) => applyNarrVolume(i));
  const el = _narrElFor(_curSeg);
  if(el) el.muted = !_narrOn;
  refreshMenus();
}
function toggleOrigTrack(){
  _origOn = !_origOn;
  const b = $('tbOrig');
  b.classList.toggle('on', _origOn);
  setBtnIcon('tbOrig', 'i-volume', _origOn ? '原声轨' : '原声轨✕');
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
  if(e.key === 'Escape'){
    ['settingsMask', 'histMask'].forEach(id => {
      const m = $(id);
      if(m && m.style.display !== 'none') m.style.display = 'none';
    });
    return;
  }
  if(inField) return;
  if(e.code === 'Space'){ e.preventDefault(); togglePlay(); }
  else if(e.key === 'ArrowLeft'){ const v = $('pvVideo'); if(v) v.currentTime = Math.max(0, v.currentTime - 1); }
  else if(e.key === 'ArrowRight'){ const v = $('pvVideo'); if(v) v.currentTime += 1; }
  else if(e.key === 'Delete' && _sel >= 0){ delSelected(); }
  else if(e.ctrlKey && e.key === 'z'){ e.preventDefault(); undoAdjust(); }
  else if(e.ctrlKey && e.key === 'y'){ e.preventDefault(); redoAdjust(); }
});

$('capAdd').addEventListener('click', addCaptionAtPlayhead);
$('capDel').addEventListener('click', delSelected);

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
$('tbCompose').addEventListener('click', composeAll);
$('tbGenerate').addEventListener('click', () => {
  if(!_video || !$('npPlot').value.trim()){ switchView('story'); return; }
  $('npGo').click();
});

/* ── SRT 导入（桌面：原生对话框 + 本地直读） ── */
let _srt = null;   // {name, text}
function importSrt(){
  if(!(window.pywebview && window.pywebview.api)){ toast('请运行桌面版 EXE', 'bad'); return; }
  window.pywebview.api.pick_srt().then(r => {
    if(!r || !r.path) return null;
    const name = r.name || r.path.split(/[\/]/).pop();
    return window.pywebview.api.read_text_file(r.path).then(t => {
      if(!t.ok) throw new Error(t.error || '读取失败');
      _srt = {name: name, text: t.text};
      setBtnIcon('tbSrt', 'i-subtitle', '字幕 ✓');
      toast('字幕已导入：' + name);
    });
  }).catch(e => toast('字幕导入失败：' + e.message, 'bad'));
}

/* ── 配乐（桌面：本地路径直连） ── */
let _music = null;   // {name, path, isLocalPath}
function pickLocalMusic(){
  if(!(window.pywebview && window.pywebview.api)) return;
  window.pywebview.api.pick_music().then(r => {
    if(!r || !r.path) return;
    _music = {name: r.name, path: r.path, isLocalPath: true};
    setBtnIcon('tbMusic', 'i-music', _music.name.slice(0, 14));
    const bg = $('npBgm'); if(bg) bg.checked = true;
    toast('配乐已选：' + _music.name);
  }).catch(e => toast('选乐失败：' + e.message, 'bad'));
}

/* ── 设置对话框 ── */
function openSettings(){
  $('settingsMask').style.display = 'flex';
  fetch('/api/ai/config').then(r => r.json()).then(d => {
    const c = d.config || {};
    $('setVisBase').value = (c.vision || {}).base_url || '';
    $('setVisModel').value = (c.vision || {}).model || '';
    $('setVisKey').value = '';   // 已配置的不回显，留空=不修改
    $('setVisKey').placeholder = (c.vision || {}).api_key ? '已配置（不改请留空）' : '未配置';
    $('setLocalBase').value = (c.local || {}).base_url || '';
    $('setLocalModel').value = (c.local || {}).model || '';
    renderWhisperOptions();
    fillTtsSettings();
  }).catch(e => toast('读取配置失败：' + e.message, 'bad'));
}
$('tbSettings').addEventListener('click', openSettings);
$('tbSrt').addEventListener('click', importSrt);
$('tbMusic').addEventListener('click', pickLocalMusic);
$('settingsClose').addEventListener('click', () => $('settingsMask').style.display = 'none');
$('settingsSave').addEventListener('click', async () => {
  const payload = {vision: {}, tts: {}, local: {}, tts_local: {
    engine: $('setTtsEngine').value || 'auto',
    voice: $('setTtsVoice').value || '',
    cosy_voice: $('setCosyVoice').value || '',
    rate: $('setTtsRate').value.trim() || '+0%'
  }};
  ['VisBase', 'VisModel'].forEach(f => {
    const el = $('set' + f);
    if(el && el.value.trim()) payload.vision[f.replace('Vis', '').toLowerCase()] = el.value.trim();
  });
  payload.vision.base_url = $('setVisBase').value.trim();
  payload.vision.model = $('setVisModel').value.trim();
  if($('setVisKey').value.trim()) payload.vision.api_key = $('setVisKey').value.trim();
  payload.local = {enabled: true, base_url: $('setLocalBase').value.trim(), model: $('setLocalModel').value.trim()};
  try {
    const r = await fetch('/api/ai/config', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    const out = await r.json();
    if(!out.ok) throw new Error(out.error || '保存失败');
    $('settingsMask').style.display = 'none';
    toast('✅ 设置已保存');
  } catch(e){ toast('保存失败：' + e.message, 'bad'); }
});

/* ── Whisper 模型下载（设置对话框内） ── */
$('btnWhisperDl').addEventListener('click', async () => {
  const model = $('setWhisperModel').value;
  const stEl = $('whisperDlStatus');
  try {
    const r = await fetch('/api/whisper/download', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({model: model})});
    const out = await r.json();
    if(!out.ok && !/开始|已存在/.test(out.message || out.error || '')) throw new Error(out.error || out.message || '失败');
    stEl.textContent = out.message || '已开始下载';
    const iv = setInterval(async () => {
      const s = await fetch('/api/whisper/status').then(x => x.json()).catch(() => null);
      if(!s) return;
      const d = s.download || {};
      stEl.textContent = d.downloading ? ('下载中 ' + (d.download_pct || 0) + '%') : (d.download_ok ? '✅ ' + (d.download_msg || '完成') : (d.download_msg || ''));
      if(!d.downloading){ clearInterval(iv); if(d.download_ok) renderWhisperOptions(); }
    }, 2000);
  } catch(e){ stEl.textContent = '❌ ' + e.message; }
});
function renderWhisperOptions(){
  fetch('/api/whisper/status').then(r => r.json()).then(s => {
    const sel = $('setWhisperModel');
    if(!sel) return;
    const valid = s.valid_models || {};
    sel.innerHTML = Object.keys(valid).map(k =>
      '<option value="' + k + '"' + (valid[k].downloaded ? ' data-dl="1"' : '') + '>' +
      k + (valid[k].downloaded ? ' ✓' : '（未下载）') + '</option>').join('');
    if([...sel.options].some(o => o.value === 'large-v3' && o.dataset.dl)) sel.value = 'large-v3';
  }).catch(() => {});
}

/* ── 设置：配音引擎与音色 ── */
const TTS_ENGINES = [['auto','自动（按可用性选最优）'],
  ['cosyvoice','CosyVoice（质量最高 · 支持克隆音色）'],
  ['edge','edge-tts（微软 · 需联网）'],
  ['chattts','ChatTTS'],
  ['sherpa','离线模型（sherpa）'],
  ['sapi','系统 SAPI（机械音兜底）']];
async function fillTtsSettings(){
  try {
    const v = await fetch('/api/tts/voices').then(r => r.json());
    const cfg = v.cfg || {};
    $('setTtsEngine').innerHTML = TTS_ENGINES.map(e =>
      '<option value="' + e[0] + '">' + e[1] + '</option>').join('');
    $('setTtsEngine').value = cfg.engine || 'auto';
    $('setTtsVoice').innerHTML = (v.voices || []).map(n =>
      '<option value="' + _esc(n) + '">' + _esc(n) + '</option>').join('');
    $('setTtsVoice').value = cfg.voice || 'zh-CN-XiaoxiaoNeural';
    $('setTtsRate').value = cfg.rate || '+0%';
    const cv = await fetch('/api/tts/cosyvoice/voices', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: '{}'}).then(r => r.json()).catch(() => null);
    $('setCosyVoice').innerHTML = ((cv && cv.voices) || []).map(x =>
      '<option value="' + _esc(x.name) + '">' + _esc(x.name) + (x.custom ? '（克隆）' : '') + '</option>').join('');
    $('setCosyVoice').value = cfg.cosy_voice || '中文女';
    $('ttsTestHint').textContent = v.label || '';
  } catch(e){ $('ttsTestHint').textContent = '读取配音配置失败：' + e.message; }
}
$('btnTtsTest').addEventListener('click', async () => {
  const b = $('btnTtsTest');
  b.disabled = true; b.textContent = '⏳ 合成中…';
  try {
    const r = await fetch('/api/tts/test', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({text: '这是一段配音试听，听听这个声音合不合适。'})});
    const out = await r.json();
    if(!out.ok) throw new Error(out.error || out.message || '试听失败');
    $('ttsTestHint').textContent = '当前引擎：' + (out.engine || '?') + ' · ' + (out.message || '');
    if(out.file) new Audio('/media/' + out.file).play().catch(() => {});
  } catch(e){ $('ttsTestHint').textContent = '❌ ' + e.message; }
  b.disabled = false; b.textContent = '🧪 试听';
});

/* ── 最近项目（关窗不丢：配音方案都在服务器上） ── */
async function loadRecent(){
  const card = $('recentCard');
  try {
    const r = await fetch('/api/tts_recent').then(x => x.json());
    const list = (r && r.list) || [];
    card.style.display = list.length ? 'block' : 'none';
    $('recentList').innerHTML = list.slice(0, 6).map(t => {
      const name = (t.movie || '').trim() || t.run_dir;
      return '<div class="recent-row" data-run="' + _esc(t.run_dir) + '">'
        + '<div class="rc-name">' + _esc(name)
        + '<small>' + _esc(t.time || '') + ' · ' + (t.tts_count || 0) + ' 段配音</small></div>'
        + '<span class="rc-go">继续 ›</span></div>';
    }).join('');
    $('recentList').querySelectorAll('.recent-row').forEach(row => {
      row.addEventListener('click', async () => {
        try {
          toast('恢复中…');
          await loadAdjustItems(row.dataset.run);
          openEditor();
          toast('✅ 已恢复，可继续调整或直接合成');
        } catch(e){ toast('恢复失败：' + e.message, 'bad'); }
      });
    });
  } catch(e){ card.style.display = 'none'; }
}
$('recentRefresh').addEventListener('click', loadRecent);
loadRecent();

/* ── 成片历史 ── */
let _histItems = [];
function openHistory(){
  $('histMask').style.display = 'flex';
  $('histList').innerHTML = '<div class="ins-empty">加载中…</div>';
  fetch('/api/history').then(r => r.json()).then(d => {
    _histItems = (d && d.history) || [];
    if(!_histItems.length){
      $('histList').innerHTML = '<div class="ins-empty">还没有成片——生成并合成后会出现在这里</div>';
      return;
    }
    $('histList').innerHTML = _histItems.map((h, i) => {
      const rel = h.file || '';
      const name = rel.split('/').pop() || rel;
      const cap = Array.isArray(h.captions) ? h.captions.join(' ') : (h.captions || '');
      const title = String(cap).trim().slice(0, 42) || name;
      const miss = h.missing ? ' <span class="env-badge bad">文件丢失</span>' : '';
      return '<div class="hist-row">'
        + (h.cover ? '<img class="hist-cover" src="/media/' + _esc(h.cover) + '" alt="">'
                   : '<div class="hist-cover ph">🎬</div>')
        + '<div class="rc-name">' + _esc(title) + miss
        + '<small>' + _esc(h.time || '') + (h.duration ? ' · ' + h.duration + 's' : '') + '</small></div>'
        + '<div class="hist-acts">'
        + (h.missing ? '' : '<button class="mini-btn" data-play="' + i + '">▶ 播放</button>'
                        + '<button class="mini-btn" data-folder="' + i + '" title="打开所在文件夹"><svg class="ic"><use href="#i-folder"/></svg></button>')
        + '<button class="mini-btn" data-del="' + i + '" title="删除这条记录"><svg class="ic"><use href="#i-trash"/></svg></button></div></div>';
    }).join('');
    $('histList').querySelectorAll('[data-play]').forEach(b => b.addEventListener('click', () => {
      const h = _histItems[+b.dataset.play];
      $('histMask').style.display = 'none';
      openExport(h.file);
    }));
    $('histList').querySelectorAll('[data-folder]').forEach(b => b.addEventListener('click', () => {
      const h = _histItems[+b.dataset.folder];
      if(window.pywebview && window.pywebview.api)
        window.pywebview.api.open_folder(h.file).catch(() => {});
      else toast('成片在 webui_output 目录');
    }));
    $('histList').querySelectorAll('[data-del]').forEach(b => b.addEventListener('click', async () => {
      const h = _histItems[+b.dataset.del];
      if(!confirm('删除这条历史记录？（只删记录，不动磁盘上的其他文件）')) return;
      try {
        await fetch('/api/history/delete', {method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({file: h.file})});
        openHistory();
      } catch(e){ toast('删除失败：' + e.message, 'bad'); }
    }));
  }).catch(e => { $('histList').innerHTML = '<div class="ins-empty">加载失败：' + _esc(e.message) + '</div>'; });
}
$('histClose').addEventListener('click', () => $('histMask').style.display = 'none');

/* ── 合成（含改词自动重配） ── */
async function composeAll(){
  if(!_runDir || !_items.length){ toast('请先生成或打开一个项目', 'bad'); return; }
  const btn = $('tbCompose');
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
    return it && !it.quote && x.text.trim() && _clean(x.text) !== _clean(it.audio_text || '');
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
    const body = { run_dir: _runDir, items: items, skip: skip, params: _advParams() };
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
}
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
  _lastExport = file;
  const pv = $('pvVideo'); if(pv) pv.pause();
  switchView('export');
  $('exPlayer').src = '/media/' + file;
  $('exPath').textContent = file;
  const btn = $('tbCompose');
  btn.disabled = false;
}
$('exFolder').addEventListener('click', () => {
  if(window.pywebview && window.pywebview.api && _lastExport){
    window.pywebview.api.open_folder(_lastExport).catch(() => {});
  } else toast('成片在 webui_output 目录');
});
$('exBack').addEventListener('click', () => switchView('adjust'));
$('exNew').addEventListener('click', () => newProject());

/* ── 启动 ── */
setStatus('就绪');
