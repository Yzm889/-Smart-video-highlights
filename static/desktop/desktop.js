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
  storage(){ openStorage(); },
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

/* ── 升级导入横幅（新包首跑、EXE 内） ── */
setTimeout(async () => {
  if(!(_isDesktop && window.pywebview && window.pywebview.api)) return;
  try {
    const hc = await window.pywebview.api.has_config();
    if(!hc.ok || hc.has || localStorage.getItem('framecut_mig_dismissed')) return;
    const b = $('migBanner');
    if(b) b.style.display = 'block';
  } catch(e){ /* 静默 */ }
}, 900);
$('migGo').addEventListener('click', async () => {
  try {
    const r = await window.pywebview.api.pick_folder();
    if(!r || !r.path) return;
    $('migGo').disabled = true; $('migGo').textContent = '迁移中…';
    const out = await window.pywebview.api.migrate_from(r.path);
    if(!out.ok){ toast('导入失败：' + out.error, 'bad'); }
    else {
      $('migBanner').style.display = 'none';
      localStorage.setItem('framecut_mig_dismissed', '1');
      toast('✅ 导入完成：' + (out.moved.join('、') || '无内容') + (out.notes.length ? '（' + out.notes.join('；') + '）' : ''), 'ok');
      setTimeout(() => location.reload(), 1800);   // 带着配置重启服务上下文
    }
  } catch(e){ toast('导入失败：' + e.message, 'bad'); }
  $('migGo').disabled = false; $('migGo').textContent = '选择旧版本目录导入';
});
$('migSkip').addEventListener('click', () => {
  localStorage.setItem('framecut_mig_dismissed', '1');
  $('migBanner').style.display = 'none';
});

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
  const bar = (pct, done, total, speed) => {
    let meta = pct + '%';
    if(done !== undefined && done !== null && total) meta = done + 'MB / ' + total + 'MB · ' + pct + '%';
    if(speed) meta += ' · ' + speed;
    return '<div class="pull-wrap" style="flex:1;min-width:120px"><div class="pbar slim"><i style="width:' + pct + '%"></i></div>'
      + '<div class="pbar-meta">' + meta + '</div></div>';
  };
  const btn = (id, label, ghost) => '<button class="env-btn' + (ghost ? ' ghost' : '') + '" data-env="' + id + '">' + label + '</button>';
  const rows = [];
  const lm = (L && L.model) || 'qwen3:8b';

  // 【主人拍板】第一页只看状态：装引擎/选模型收进设置「模型中心」，未就绪行给「去设置 ›」
  const gotoBtn = sec => '<button class="env-btn ghost" data-goto="' + sec + '">去设置 ›</button>';
  const ollamaUp = !!(L && L.ollama_running);
  if(L && L.ollama_installing){
    rows.push('<div class="env-row"><div class="en-name">Ollama ' + (L.ollama_install_phase || '处理中') + '<small>'
      + (L.ollama_install_msg || '') + '</small></div>' + wB((L.ollama_install_pct || 0) + '%') + '</div>');
  } else {
    rows.push('<div class="env-row"><div class="en-name">Ollama 本地引擎<small>AI 运行时（免费 · 全程本地）</small></div>'
      + (!L ? chk() : (ollamaUp ? okB('运行中')
        : (L.ollama_installed ? noB('已安装·未运行') + gotoBtn('secModelCenter')
                              : noB('未安装') + gotoBtn('secModelCenter')))) + '</div>');
  }

  // ② 文字大脑：识别本机已装的全部模型（不止当前用的）
  let txtOk = false;
  if(L && L.pulling){
    rows.push('<div class="env-row"><div class="en-name">文字大脑 ' + (L.pull_model || lm) + '<small>' + (L.pull_msg || '拉取中') + '</small></div>'
      + bar(L.pull_pct || 0, L.pull_mb_done, L.pull_mb_total, L.pull_speed) + '</div>');
  } else if(!L){
    rows.push('<div class="env-row"><div class="en-name">文字大脑<small>写解说稿的核心</small></div>' + chk() + '</div>');
  } else {
    txtOk = (L.installed || []).some(m => (m || '').split(':')[0] === lm.split(':')[0]);
    const others = (L.installed || []).filter(m => m && (m.split(':')[0] !== lm.split(':')[0]));
    const sub = txtOk
      ? ('当前 ' + lm + (others.length ? ' · 本机已装 ' + others.length + ' 个模型，可在设置更换' : ''))
      : '本机未装「' + lm + '」，去设置里选型号或拉取';
    rows.push('<div class="env-row"><div class="en-name">文字大脑 ' + lm + '<small>' + sub + '</small></div>'
      + (txtOk ? okB('就绪') : noB('未就绪') + gotoBtn('secModelCenter')) + '</div>');
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
    rows.push('<div class="env-row"><div class="en-name">看图模型 ' + (V.pull_model || V.model || 'minicpm-v') + '<small>拉取中</small></div>'
      + bar(V.pull_pct || 0, V.pull_mb_done, V.pull_mb_total, V.pull_speed) + '</div>');
  } else {
    const vOk = !!(V && V.ready);
    const vOpt = !!(V && V.enabled === false);   // 明确关闭才算"可选"，超时/未启用保持检测中
    rows.push('<div class="env-row"><div class="en-name">看图模型 ' + ((V && V.model) || 'minicpm-v') + '<small>可选：AI 亲眼看画面，理解更准</small></div>'
      + (!V ? chk() : (vOk ? okB('就绪') : wB('可选') + gotoBtn('secModelCenter'))) + '</div>');
  }

  // ④ Whisper 台词识别
  if(W && W.downloading){
    rows.push('<div class="env-row"><div class="en-name">台词识别 ' + (W.download_model || 'large-v3') + '<small>听懂台词，时间轴零偏差</small></div>'
      + bar(W.download_pct || 0, W.dl_mb_done, W.dl_mb_total, W.dl_speed) + '</div>');
  } else {
    const wOk = !!(W && W.ready);
    rows.push('<div class="env-row"><div class="en-name">台词识别 ' + ((W && W.selected) || 'large-v3') + '<small>听懂台词（2.9GB，一次下载）</small></div>'
      + (!W ? chk() : (wOk ? okB('就绪') : noB('未下载') + gotoBtn('secWhisper'))) + '</div>');
  }

  // ⑤ 配音引擎（信息展示；引擎安装/音色在设置里）
  const vc = A && A.voice;
  rows.push('<div class="env-row"><div class="en-name">配音引擎<small>' + ((vc && vc.ready && vc.label) ? vc.label : ((vc && vc.reason) || '检测中')) + '</small></div>'
    + (!A ? chk() : (vc ? (vc.ready ? okB('就绪') : noB('未就绪') + btn('gotoVoice', '去设置 ›', true)) : wB('…'))) + '</div>');

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

$('envRows').addEventListener('click', e => {   // 只负责「去设置」跳转，安装动作全在设置里
  const b = e.target.closest('[data-goto]');
  if(b){ openSettings(b.dataset.goto); return; }
  const v = e.target.closest('[data-env="gotoVoice"]');
  if(v){ openSettings('secVoice'); }
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
    // 浏览器模式兜底：标准文件选择 + 分片上传（与网页版同一套后端），不再是死胡同
    _browserFileInput('video/*', async f => {
      const label = $('npPickLabel');
      try {
        label.textContent = '⬆ 上传中 0%…';
        const uid = await _chunkUpload(f, done => {
          label.textContent = '⬆ 上传中 ' + Math.round(done * 100 / f.size) + '%（' + _fmtMB(done) + ' / ' + _fmtMB(f.size) + '）';
        });
        _video = {name: f.name, upload_id: uid, isLocalPath: false};
        $('npPick').classList.add('has');
        label.textContent = '✅ ' + f.name;
        $('projName').textContent = f.name.replace(/\.[^.]+$/, '');
        updateNewProj();
        toast('✅ 视频已上传，可以填写剧情开始生成', 'ok');
      } catch(e){ label.textContent = '从电脑选择视频'; toast('上传失败：' + e.message, 'bad'); }
    });
  }
});
let _npFileInput = null;
function _browserFileInput(accept, cb){
  // 浏览器模式的兜底选择器（EXE 里走 pywebview 原生对话框）
  if(!_npFileInput){
    _npFileInput = document.createElement('input');
    _npFileInput.type = 'file';
    _npFileInput.style.display = 'none';
    document.body.appendChild(_npFileInput);
  }
  _npFileInput.accept = accept;
  _npFileInput.onchange = () => {
    const f = _npFileInput.files && _npFileInput.files[0];
    _npFileInput.value = '';
    if(f) cb(f);
  };
  _npFileInput.click();
}
function _fileB64(file){
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(',')[1] || '');
    r.onerror = () => reject(new Error('读取失败'));
    r.readAsDataURL(file);
  });
}
function _fmtMB(n){ return (n / 1024 / 1024).toFixed(1) + 'MB'; }

/* ── 分片上传（web 同一套 /api/upload/*，4MB/片 3 并发） ── */
async function _chunkUpload(file, onProg){
  const r0 = await fetch('/api/upload/init', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({name: file.name, size: file.size})}).then(x => x.json());
  if(!r0.ok) throw new Error(r0.error || '上传初始化失败');
  const uid = r0.upload_id;
  const CHUNK = 4 * 1024 * 1024, CONC = 3;
  const total = Math.max(1, Math.ceil(file.size / CHUNK));
  let next = 0, doneBytes = 0;
  async function worker(){
    while(next < total){
      const idx = next++;
      const blob = file.slice(idx * CHUNK, Math.min(file.size, (idx + 1) * CHUNK));
      const fd = new FormData();
      fd.append('upload_id', uid);
      fd.append('idx', String(idx));
      fd.append('chunk', blob);
      const r = await fetch('/api/upload/chunk', {method:'POST', body: fd}).then(x => x.json());
      if(!r.ok) throw new Error(r.error || ('分片 ' + idx + ' 失败'));
      doneBytes += blob.size;
      if(onProg) onProg(doneBytes);
    }
  }
  await Promise.all([worker(), worker(), worker()]);
  const rd = await fetch('/api/upload/done', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({upload_id: uid, name: file.name, chunks: total})}).then(x => x.json());
  if(!rd.ok) throw new Error(rd.error || '上传收尾失败');
  return uid;
}

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

function _musicPayload(){
  if(!_music) return undefined;
  return (_music.upload_id ? {name: _music.name, upload_id: _music.upload_id}
        : (_music.data ? {name: _music.name, data: _music.data}
           : {name: _music.name, path: _music.path, desktop_token: _token}));
}

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
    video: (_video.upload_id ? {name: _video.name, upload_id: _video.upload_id}
                             : {name: _video.name, path: _video.path, desktop_token: _token}),
    plot: isScript ? '' : plot,
    script: isScript ? plot : undefined,
    params: _advParams(),
  };
  if(_srt) body.subtitle_file = {name: _srt.name, text: _srt.text};
  if(_music && $('npBgm').checked) body.music = _musicPayload();
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
  // PR 式：色块按视频绝对时间定位（累计排布会让裁剪/拖动没有物理意义）
  let html = '';
  _items.forEach((it, i) => {
    const vs = Math.max(0, it.video_start || 0);
    const vd = Math.max(0.1, (it.video_end || 0) - vs);
    const left = vs / total * 100, w = Math.min(vd, total - vs) / total * 100;
    const cls = (it.quote ? ' quote' : '') + (it._skip ? ' skip' : '') + (i === _sel ? ' sel' : '');
    const tip = _esc(_clean(it.text).slice(0, 60)) || (it.quote ? '原声引用' : '');
    html += '<div class="tl-block' + cls + '" data-i="' + i + '" title="' + tip + '" style="left:' + left + '%;width:'
      + w + '%;background:' + (it.quote ? '#10b981' : TL_COLORS[i % TL_COLORS.length]) + '">'
      + '<i class="tl-h hL" data-h="L"></i><i class="tl-h hR" data-h="R"></i>'
      + '<span>' + (it.quote ? '原声' : '#' + (i + 1)) + '</span>'
      + '<canvas class="tl-wave" data-wv="' + i + '"></canvas>'
      + '</div>';
  });
  $('tlTrack').innerHTML = html;
  $('tlTrack').querySelectorAll('.tl-block').forEach(b => {
    b.addEventListener('click', () => selectSeg(parseInt(b.dataset.i, 10)));
    b.addEventListener('contextmenu', e => {
      e.preventDefault();
      selectSeg(parseInt(b.dataset.i, 10));
      openTlMenu(e.clientX, e.clientY, parseInt(b.dataset.i, 10));
    });
  });
  requestAnimationFrame(_drawWaves);   // 布局稳定后再量宽（首帧 rect 可能为 0）
  setTimeout(_drawWaves, 350);
  updatePlayhead();
}

/* ── 时间轴拖拽：边缘裁剪 / 整体平移 / 磁吸（快照在首次移动时入栈） ── */
(function(){
  const track = $('tlTrack');
  let drag = null;
  function snapList(i){
    const arr = [0, Math.max(10, _vdur)];
    _items.forEach((it, k) => {
      if(k === i) return;
      arr.push(it.video_start || 0, it.video_end || 0);
    });
    return arr;
  }
  function snap(t, list){
    const tol = 8 / _pxPerSec;   // 8px 内吸附
    for(const s of list){ if(Math.abs(t - s) <= tol) return s; }
    return t;
  }
  track.addEventListener('mousedown', e => {
    const h = e.target.closest('.tl-h');
    const b = e.target.closest('.tl-block');
    if(!b) return;
    const i = parseInt(b.dataset.i, 10);
    const it = _items[i];
    if(!it) return;
    drag = {i: i, mode: h ? h.dataset.h : 'move', x0: e.clientX,
            start: it.video_start || 0, end: it.video_end || 0,
            snap: snapList(i), moved: false};
    e.preventDefault();
    e.stopPropagation();
  });
  window.addEventListener('mousemove', e => {
    if(!drag) return;
    const it = _items[drag.i];
    if(!it) return;
    if(!drag.moved && Math.abs(e.clientX - drag.x0) <= 3) return;
    if(!drag.moved){
      drag.moved = true;
      _undoStack.push(JSON.stringify(_items));
      if(_undoStack.length > 50) _undoStack.shift();
      _redoStack = [];
    }
    const dt = (e.clientX - drag.x0) / _pxPerSec;
    const total = Math.max(10, _vdur);
    const b = track.querySelector('.tl-block[data-i="' + drag.i + '"]');
    if(drag.mode === 'L'){
      const ns = Math.round(snap(Math.min(drag.end - 0.5, Math.max(0, drag.start + dt)), drag.snap) * 10) / 10;
      it.video_start = Math.min(ns, it.video_end - 0.5);
    } else if(drag.mode === 'R'){
      const ne = Math.round(snap(Math.max(drag.start + 0.5, Math.min(total, drag.end + dt)), drag.snap) * 10) / 10;
      it.video_end = Math.max(ne, it.video_start + 0.5);
    } else {
      const vd = drag.end - drag.start;
      const ns = Math.round(Math.max(0, Math.min(snap(drag.start + dt, drag.snap), total - vd)) * 10) / 10;
      it.video_start = ns;
      it.video_end = Math.round((ns + vd) * 10) / 10;
    }
    const vs = it.video_start, vd = Math.max(0.1, it.video_end - vs);
    b.style.left = (vs / total * 100) + '%';
    b.style.width = (Math.min(vd, total - vs) / total * 100) + '%';
  });
  window.addEventListener('mouseup', () => {
    if(!drag) return;
    const it = _items[drag.i];
    if(drag.moved && it){
      renderTimeline();
      if(_sel === drag.i) renderInspector(_sel);
      ensureNarrElements();
    }
    drag = null;
  });
})();

/* ── 时间轴右键菜单：分割 / 淡入淡出 / 跳过 / 原声引用 ── */
let _tlMenu = null;
function openTlMenu(x, y, i){
  closeTlMenu();
  const it = _items[i];
  if(!it) return;
  const items = [];
  if(it.quote){
    items.push('<div class="mi" data-m="unquote">恢复为解说段</div>');
  } else {
    items.push('<div class="mi" data-m="split">从播放头分割（一段拆两段）</div>');
    items.push('<div class="mi" data-m="fadein">淡入 ' + (it.fade_in ? '× 清除（当前 ' + it.fade_in + 's）' : '0.5s') + '</div>');
    items.push('<div class="mi" data-m="fadeout">淡出 ' + (it.fade_out ? '× 清除（当前 ' + it.fade_out + 's）' : '0.5s') + '</div>');
    items.push('<div class="mi" data-m="skip">' + (it._skip ? '取消跳过（恢复进合成）' : '跳过此段（不进合成）') + '</div>');
    items.push('<div class="mi" data-m="quote">设为原声引用（保留原声，不配解说）</div>');
  }
  items.push('<div class="mi" data-m="del">删除此段</div>');
  _tlMenu = document.createElement('div');
  _tlMenu.className = 'menu-dd tl-menu';
  _tlMenu.style.cssText = 'display:block;position:fixed;left:' + Math.min(x, window.innerWidth - 260)
    + 'px;top:' + Math.min(y, window.innerHeight - 200) + 'px;z-index:400';
  _tlMenu.innerHTML = items.join('');
  document.body.appendChild(_tlMenu);
  _tlMenu.addEventListener('click', async e => {
    const mi = e.target.closest('.mi');
    if(!mi) return;
    const m = mi.dataset.m;
    closeTlMenu();
    const it2 = _items[i];
    if(!it2) return;
    if(m === 'del'){ _sel = i; delSelected(); return; }
    if(m === 'unquote' || m === 'quote'){
      _pushUndo();
      it2.quote = !it2.quote;
      renderInspector(i); renderCaps(); renderTimeline();
      return;
    }
    if(m === 'skip'){
      _pushUndo();
      it2._skip = !it2._skip;
      renderCaps(); renderTimeline();
      scheduleStatus();
      toast(it2._skip ? '已跳过：此段不进合成' : '已恢复：此段进合成');
      return;
    }
    if(m === 'fadein'){
      _pushUndo();
      it2.fade_in = it2.fade_in ? 0 : 0.5;
      renderTimeline();
      toast(it2.fade_in ? '已加淡入 0.5s（合成时生效）' : '已清除淡入');
      return;
    }
    if(m === 'fadeout'){
      _pushUndo();
      it2.fade_out = it2.fade_out ? 0 : 0.5;
      renderTimeline();
      toast(it2.fade_out ? '已加淡出 0.5s（合成时生效）' : '已清除淡出');
      return;
    }
    if(m === 'split'){ await splitSegAtPlayhead(i); }
  });
  setTimeout(() => document.addEventListener('click', closeTlMenu, {once: true}), 0);
}
function closeTlMenu(){
  if(_tlMenu){ _tlMenu.remove(); _tlMenu = null; }
}

/* ── 分割：配音按比例切两段（/api/audio_split），文字向最近标点断开 ── */
async function splitSegAtPlayhead(i){
  const it = _items[i];
  if(!it) return;
  if(it.quote){ toast('原声引用段没有配音，直接拖拽调范围即可', 'bad'); return; }
  if(!it.audio){ toast('该段没有配音文件，无法分割', 'bad'); return; }
  const vs = it.video_start || 0, ve = it.video_end || 0;
  if(ve - vs < 1.6){ toast('这段画面太短（<1.6s），不值得分割', 'bad'); return; }
  const v = $('pvVideo');
  const t = (v && isFinite(v.currentTime)) ? v.currentTime : (vs + ve) / 2;
  if(!(t > vs + 0.4 && t < ve - 0.4)){
    toast('先把播放头停在段内要分割的位置（离两端至少 0.4s）', 'bad');
    return;
  }
  toast('正在切割配音…');
  try {
    const r = await fetch('/api/audio_split', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({run_dir: _runDir, name: it.audio, frac: (t - vs) / (ve - vs)})});
    const out = await r.json();
    if(!out.ok) throw new Error(out.error || '切割失败');
    const txt = (it.text || '').trim();
    let cut = Math.round(txt.length * ((t - vs) / (ve - vs)));
    const WIN = Math.max(2, Math.floor(txt.length * 0.2));
    let best = -1;
    for(let k = Math.max(1, cut - WIN); k <= Math.min(txt.length - 1, cut + WIN); k++){
      if('，。！？；、：,.!? '.indexOf(txt.charAt(k)) >= 0){
        if(best < 0 || Math.abs(k - cut) < Math.abs(best - cut)) best = k;
      }
    }
    if(best >= 0) cut = best + 1;
    const t1 = txt.slice(0, cut).trim(), t2 = txt.slice(cut).trim();
    _pushUndo();
    it.video_end = t;
    it.audio = out.a1;
    it.duration = out.d1;
    if(t1){ it.text = t1; it.audio_text = t1; }
    it.fade_out = 0;
    let mx = -1;
    _items.forEach(x => { const n = parseInt(x && x.index); if(!isNaN(n) && n > mx) mx = n; });
    _items.splice(i + 1, 0, {index: mx + 1, text: t2, audio_text: t2, audio: out.a2, duration: out.d2,
      video_start: t, video_end: ve, audio_offset: 0,
      orig_volume: it.orig_volume, narr_volume: it.narr_volume,
      fade_in: 0, fade_out: 0, isBroll: false, quote: false});
    _sel = i + 1;
    renderCaps(); ensureNarrElements(); renderTimeline(); selectSeg(_sel);
    toast('✅ 已分割：一段拆两段（画面/配音/文字各自独立）');
  } catch(e){ toast('分割失败：' + e.message, 'bad'); }
}

/* ── 色块内配音波形（/api/audio_peaks，懒加载缓存） ── */
let _wavesDrawn = {};
function _drawWaves(){
  $('tlTrack').querySelectorAll('canvas.tl-wave').forEach(cv => {
    const i = parseInt(cv.dataset.wv, 10);
    const it = _items[i];
    if(!it || it.quote || !it.audio) { cv.style.display = 'none'; return; }
    const w = Math.max(20, Math.round(cv.parentElement.getBoundingClientRect().width));
    cv.width = w; cv.height = 34;
    cv.style.display = 'block';
    if(it._peaks){ _paintWave(cv, it._peaks); return; }
    if(_wavesDrawn[it.audio]) return;   // 已在请求中
    _wavesDrawn[it.audio] = true;
    fetch('/api/audio_peaks?run_dir=' + encodeURIComponent(_runDir)
      + '&name=' + encodeURIComponent(it.audio) + '&points=64')
      .then(r => r.json()).then(d => {
        if(d && d.peaks){
          _items.forEach(x => { if(x.audio === it.audio) x._peaks = d.peaks; });
          $('tlTrack').querySelectorAll('canvas[data-wv]').forEach(c2 => {
            const j = parseInt(c2.dataset.wv, 10);
            if(_items[j] && _items[j].audio === it.audio) _paintWave(c2, d.peaks);
          });
        }
      }).catch(() => {});
  });
}
function _paintWave(cv, peaks){
  const ctx = cv.getContext('2d');
  const w = cv.width, h = cv.height;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = 'rgba(255,255,255,.35)';
  const n = peaks.length;
  for(let k = 0; k < n; k++){
    const bh = Math.max(1, Math.min(1, Math.abs(peaks[k])) * h * 0.9);
    ctx.fillRect(k / n * w, (h - bh) / 2, Math.max(1, w / n - 1), bh);
  }
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
function seekTo(t){
  const v = $('pvVideo');
  if(!v || !isFinite(t)) return;
  try { v.currentTime = Math.max(0, Math.min(t, (v.duration || t) - 0.05)); } catch(e){}
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
  if(!(window.pywebview && window.pywebview.api)){
    _browserFileInput('.srt,.ass,.ssa,.vtt', f => {
      const r = new FileReader();
      r.onload = () => {
        _srt = {name: f.name, text: String(r.result)};
        setBtnIcon('tbSrt', 'i-subtitle', '字幕 ✓');
        toast('✅ 字幕已导入：' + f.name, 'ok');
      };
      r.readAsText(f, 'utf-8');
    });
    return;
  }
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
  if(!(window.pywebview && window.pywebview.api)){
    _browserFileInput('audio/*', async f => {
      try {
        if(f.size > 8 * 1024 * 1024){
          $('tbMusic').textContent = '⬆ 上传中…';
          const uid = await _chunkUpload(f);
          _music = {name: f.name, upload_id: uid, isLocalPath: false};
        } else {
          const b64 = await _fileB64(f);
          _music = {name: f.name, data: b64, isLocalPath: false};
        }
        setBtnIcon('tbMusic', 'i-music', _music.name.slice(0, 14));
        const bg = $('npBgm'); if(bg) bg.checked = true;
        toast('✅ 配乐已选：' + _music.name, 'ok');
      } catch(e){ toast('配乐失败：' + e.message, 'bad'); }
    });
    return;
  }
  window.pywebview.api.pick_music().then(r => {
    if(!r || !r.path) return;
    _music = {name: r.name, path: r.path, isLocalPath: true};
    setBtnIcon('tbMusic', 'i-music', _music.name.slice(0, 14));
    const bg = $('npBgm'); if(bg) bg.checked = true;
    toast('配乐已选：' + _music.name);
  }).catch(e => toast('选乐失败：' + e.message, 'bad'));
}

/* ── 设置对话框 ── */
function openSettings(sec){
  $('settingsMask').style.display = 'flex';
  if(sec){
    setTimeout(() => {
      const el = $(sec);
      if(el) el.scrollIntoView({block: 'start'});
    }, 60);
  }
  fetch('/api/ai/config').then(r => r.json()).then(d => {
    const c = d.config || {};
    $('setVisBase').value = (c.vision || {}).base_url || '';
    $('setVisModel').value = (c.vision || {}).model || '';
    $('setVisKey').value = '';   // 已配置的不回显，留空=不修改
    $('setVisKey').placeholder = (c.vision || {}).api_key ? '已配置（不改请留空）' : '未配置';
    $('setLocalBase').value = (c.local || {}).base_url || '';
    $('setVlmBase').value = (c.vlm || {}).base_url || '';
    renderWhisperOptions();
    fillTtsSettings();
    fillModelCenter();
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
  // 空地址不落盘（实测泄漏过 vlm.base_url='' → vlm_ping 建出 http:///api/chat 挂死）
  if($('setLocalBase').value.trim()) payload.local.base_url = $('setLocalBase').value.trim();
  if($('setVlmBase').value.trim()) payload.vlm = {enabled: true, mode: 'ollama', base_url: $('setVlmBase').value.trim()};
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
      _pullBar('whisperPullWrap', 'whisperPullFill', 'whisperPullMeta', d.downloading, d.download_pct, d.dl_mb_done, d.dl_mb_total, d.dl_speed);
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

/* ── 设置：模型中心（与网页版同一份候选目录） ── */
const LOCAL_MODEL_CATALOG = [
  {tag: 'qwen3:8b',          label: '⭐ qwen3:8b',   size: '≈5.0GB'},
  {tag: 'qwen2.5:7b',        label: 'qwen2.5:7b',    size: '≈4.7GB'},
  {tag: 'qwen2.5:3b',        label: 'qwen2.5:3b',    size: '≈1.9GB'},
  {tag: 'qwen3:14b-q4_K_M',  label: 'qwen3:14b',     size: '≈9.3GB'},
  {tag: 'qwen2.5:14b',       label: 'qwen2.5:14b',   size: '≈8.4GB'}
];
const VLM_MODEL_CATALOG = [
  {tag: 'minicpm-v4.5',  label: '⭐ MiniCPM-V 4.5', size: '≈6.1GB'},
  {tag: 'qwen3-vl:8b',   label: 'qwen3-vl:8b',     size: '≈6.1GB'}
];
function _fillModelPick(sel, catalog, current, installed, pulling){
  const names = installed || [];
  const opts = [];
  catalog.forEach(m => {
    const inst = names.some(n => (n || '').split(':')[0] === m.tag.split(':')[0]);
    opts.push('<option value="' + m.tag + '"' + (m.tag === current ? ' selected' : '') + '>'
      + m.label + (inst ? '（已装）' : '（' + m.size + '）') + '</option>');
  });
  names.forEach(n => {
    if(!catalog.some(m => m.tag.split(':')[0] === (n || '').split(':')[0]) && n !== current){
      opts.push('<option value="' + _esc(n) + '"' + (n === current ? ' selected' : '') + '>' + _esc(n) + '（已装）</option>');
    }
  });
  sel.innerHTML = opts.join('');
  sel.dataset.pulling = pulling ? '1' : '';
}
function _pullBar(wrapId, fillId, metaId, active, pct, done, total, speed){
  const wrap = $(wrapId);
  if(!wrap) return;
  if(!active){ wrap.style.display = 'none'; return; }
  wrap.style.display = 'block';
  $(fillId).style.width = (pct || 0) + '%';
  let meta = (pct || 0) + '%';
  if(done !== undefined && done !== null && total) meta = done + 'MB / ' + total + 'MB · ' + (pct || 0) + '%';
  if(speed) meta += ' · ' + speed;
  $(metaId).textContent = meta;
}
async function fillModelCenter(){
  try {
    // 【实测踩中】本地填充不许等在慢速的 vlm 探测后面（vlm/status 可达 10s+）——
    // 两个探测并行，各自的行各自落地
    const jobL = _envGet('/api/local/status').then(L => {
      if(!L) return;
      _fillModelPick($('setLocalPick'), LOCAL_MODEL_CATALOG, L.model || 'qwen3:8b', L.installed, L.pulling);
      _pullBar('localPullWrap', 'localPullFill', 'localPullMeta', L.pulling, L.pull_pct, L.pull_mb_done, L.pull_mb_total, L.pull_speed);
      $('ollamaState').textContent = L.ollama_running ? '✓ 运行中'
        : (L.ollama_installed ? '已安装·未运行（点右侧启动）' : '未安装（首次约 1.5GB 下载）');
      $('mcHint').textContent = L.pulling
        ? ('拉取中 ' + (L.pull_model || '') + ' ' + (L.pull_pct || 0) + '%…')
        : '选已安装的立即生效；选未安装的会自动开始拉取';
    });
    const jobV = _envGet('/api/vlm/status').then(V => {
      if(!V) return;
      _fillModelPick($('setVlmPick'), VLM_MODEL_CATALOG, V.model || 'minicpm-v4.5', V.installed, V.pulling);
      _pullBar('vlmPullWrap', 'vlmPullFill', 'vlmPullMeta', V.pulling, V.pull_pct, V.pull_mb_done, V.pull_mb_total, V.pull_speed);
    });
    await Promise.all([jobL, jobV]);
  } catch(e){ /* 静默，下次刷新 */ }
}
$('btnPullLocal').addEventListener('click', async () => {
  const m = $('setLocalPick').value;
  const r = await fetch('/api/local/pull', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({model: m})}).then(x => x.json()).catch(() => null);
  toast(r && r.ok ? ('开始拉取 ' + m) : ('拉取失败：' + ((r && (r.error || r.message)) || '未知')));
  fillModelCenter();
});
$('btnPullVlm').addEventListener('click', async () => {
  const m = $('setVlmPick').value;
  const r = await fetch('/api/vlm/pull', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({model: m})}).then(x => x.json()).catch(() => null);
  toast(r && r.ok ? ('开始拉取 ' + m) : ('拉取失败：' + ((r && (r.error || r.message)) || '未知')));
  fillModelCenter();
});
$('btnOllamaSetup').addEventListener('click', async () => {
  const b = $('btnOllamaSetup');
  b.disabled = true;
  try { await fetch('/api/ollama/install', {method:'POST', headers:{'Content-Type':'application/json'}, body: '{}'}); } catch(e){}
  toast('Ollama 处理中，状态会自动刷新');
  setTimeout(() => { b.disabled = false; fillModelCenter(); refreshEnv(); }, 2500);
});
// 两个下拉：选择即保存；未安装自动拉取（与网页版 pickLocalModel 语义一致）
$('setLocalPick').addEventListener('change', async () => {
  const m = $('setLocalPick').value;
  const L = _envData.local;
  const installed = ((L && L.installed) || []).some(n => (n || '').split(':')[0] === m.split(':')[0]);
  await fetch('/api/ai/config', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({local: {enabled: true, model: m}})});
  toast('文字模型已切换为 ' + m + (installed ? '' : '，开始自动拉取…'));
  if(!installed){ $('btnPullLocal').click(); }
  else { refreshEnv(); fillModelCenter(); }
});
$('setVlmPick').addEventListener('change', async () => {
  const m = $('setVlmPick').value;
  const V = _envData.vlm;
  const installed = ((V && V.installed) || []).some(n => (n || '').split(':')[0] === m.split(':')[0]);
  await fetch('/api/ai/config', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({vlm: {enabled: true, mode: 'ollama', base_url: 'http://localhost:11434/v1', model: m}})});
  toast('看图模型已切换为 ' + m + (installed ? '' : '，开始自动拉取…'));
  if(!installed){ $('btnPullVlm').click(); }
  else { refreshEnv(); fillModelCenter(); }
});
// 设置打开期间轮询拉取进度（2.5s，仅在对话框可见时）
setInterval(() => {
  if($('settingsMask').style.display === 'none' || !$('settingsMask').style.display) return;
  fillModelCenter();
}, 2500);

/* ── 设置：配音引擎安装（缺哪个装哪个，装完自动可用） ── */
function renderTtsInstalls(v){
  const box = $('ttsInstalls');
  if(!box || !v) return;
  const parts = [];
  if(!v.cosyvoice_installed){
    parts.push('<button class="mini-btn" data-ttsi="cosyvoice">装 CosyVoice（推荐）</button>');
  }
  if(!v.edge_installed){
    parts.push('<button class="mini-btn" data-ttsi="edge">装 edge-tts</button>');
  }
  if(v.sherpa_models && v.sherpa_models.length){
    v.sherpa_models.forEach(m => {
      if(!m.ready) parts.push('<button class="mini-btn" data-ttsi="sherpa" data-m="' + m.key + '">下载离线模型 ' + m.label + '</button>');
    });
  }
  box.innerHTML = parts.join('') || '<span class="hint">引擎都齐了 ✓</span>';
  box.querySelectorAll('[data-ttsi]').forEach(b => b.addEventListener('click', async () => {
    b.disabled = true; b.textContent = '安装中…';
    const k = b.dataset.ttsi;
    try {
      if(k === 'cosyvoice'){ await fetch('/api/tts/cosyvoice/install', {method:'POST', headers:{'Content-Type':'application/json'}, body: '{}'}); }
      if(k === 'edge'){ await fetch('/api/tts/install', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({pkg: 'edge-tts'})}); }
      if(k === 'sherpa'){ await fetch('/api/tts/model/download', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({model: b.dataset.m})}); }
      toast('已开始安装（后台进行，装完自动可用；CosyVoice 首次较长）');
      setTimeout(() => { fillTtsSettings(); }, 3000);
    } catch(e){ toast('安装失败：' + e.message, 'bad'); b.disabled = false; }
  }));
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
    renderTtsInstalls(v);
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

/* ── 声音克隆（本地 CosyVoice，参考音频不出机器） ── */
let _cloneFile = null;
function openClone(){
  _cloneFile = null;
  $('cloneName').value = '';
  $('clonePickLabel').textContent = '选择音频文件';
  $('cloneGo').disabled = true;
  $('cloneMask').style.display = 'flex';
}
$('btnCloneVoice').addEventListener('click', openClone);
$('cloneClose').addEventListener('click', () => $('cloneMask').style.display = 'none');
$('clonePick').addEventListener('click', async () => {
  if(!(window.pywebview && window.pywebview.api)){
    _browserFileInput('audio/*', async f => {
      try {
        const b64 = await _fileB64(f);
        _cloneFile = {path: null, name: f.name, b64: b64};
        $('clonePickLabel').textContent = '✅ ' + f.name;
        $('cloneGo').disabled = !$('cloneName').value.trim();
      } catch(e){ toast('读取失败：' + e.message, 'bad'); }
    });
    return;
  }
  try {
    const r = await window.pywebview.api.pick_music();
    if(!r || !r.path) return;
    _cloneFile = {path: r.path, name: r.name};
    $('clonePickLabel').textContent = '✅ ' + r.name;
    $('cloneGo').disabled = !$('cloneName').value.trim();
  } catch(e){ toast('选择失败：' + e.message, 'bad'); }
});
$('cloneName').addEventListener('input', () => { $('cloneGo').disabled = !_cloneFile || !$('cloneName').value.trim(); });
$('cloneGo').addEventListener('click', async () => {
  if(!_cloneFile) return;
  const b = $('cloneGo');
  b.disabled = true; b.textContent = '克隆中…';
  try {
    let b64 = _cloneFile.b64;
    if(!b64){
      const rd = await window.pywebview.api.read_file_b64(_cloneFile.path);
      if(!rd.ok) throw new Error(rd.error || '读取失败');
      b64 = rd.b64;
    }
    const ext = (_cloneFile.name.split('.').pop() || 'wav').toLowerCase();
    const r = await fetch('/api/tts/cosyvoice/add_voice', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name: $('cloneName').value.trim(),
                            audio: 'data:audio/' + ext + ';base64,' + b64})});
    const out = await r.json();
    if(!out.ok) throw new Error(out.error || '克隆失败');
    $('cloneMask').style.display = 'none';
    toast('✅ 音色「' + $('cloneName').value.trim() + '」已克隆，可在音色列表选择', 'ok');
    fillTtsSettings();
  } catch(e){ toast('克隆失败：' + e.message, 'bad'); }
  b.disabled = false; b.textContent = '开始克隆';
});

/* ── 免费音乐库（搜索 / 试听 / 使用） ── */
function openMusicLib(){
  $('musicMask').style.display = 'flex';
  $('musicQ').value = '';
  loadMusicLib('');
}
$('tbMusicLib').addEventListener('click', openMusicLib);
$('musicClose').addEventListener('click', () => { $('musicMask').style.display = 'none'; $('musicAudio') && $('musicAudio').pause(); });
function loadMusicLib(q){
  $('musicList').innerHTML = '<div class="ins-empty">搜索中…</div>';
  fetch('/api/music/search?q=' + encodeURIComponent(q || '')).then(r => r.json()).then(d => {
    const list = (d && d.results) || [];
    if(!list.length){ $('musicList').innerHTML = '<div class="ins-empty">没有匹配的曲目</div>'; return; }
    $('musicList').innerHTML = list.map(t =>
      '<div class="hist-row" data-id="' + _esc(t.id) + '">'
      + '<div class="rc-name">' + _esc(t.title)
      + '<small>' + _esc(t.genre || '') + (t.bpm ? ' · ' + t.bpm + 'BPM' : '') + (t.cached ? ' · 已缓存' : '') + '</small></div>'
      + '<div class="hist-acts">'
      + '<button class="mini-btn" data-prev="' + _esc(t.id) + '">▶ 试听</button>'
      + '<button class="mini-btn" data-use="' + _esc(t.id) + '" data-title="' + _esc(t.title) + '">＋ 使用</button>'
      + '</div></div>').join('');
    let cur = null;
    $('musicList').querySelectorAll('[data-prev]').forEach(b => b.addEventListener('click', () => {
      if(cur){ cur.pause(); }
      cur = new Audio('/music_lib/' + b.dataset.prev + '.mp3');
      cur.play().catch(() => toast('试听失败（首次使用会先下载）', 'bad'));
    }));
    $('musicList').querySelectorAll('[data-use]').forEach(b => b.addEventListener('click', async () => {
      b.disabled = true; b.textContent = '下载中…';
      try {
        const r = await fetch('/api/music/use?id=' + encodeURIComponent(b.dataset.use));
        const out = await r.json();
        if(!out.ok) throw new Error(out.error || '下载失败');
        if(window.pywebview && window.pywebview.api && out.path){
          _music = {name: out.file, path: out.path, isLocalPath: true};
        } else {
          _music = {name: out.file, catalogId: b.dataset.use, isLocalPath: false};
        }
        setBtnIcon('tbMusic', 'i-music', _music.name.slice(0, 14));
        const bg = $('npBgm'); if(bg) bg.checked = true;
        $('musicMask').style.display = 'none';
        toast('✅ 已设为配乐：' + out.file, 'ok');
      } catch(e){ toast('使用失败：' + e.message, 'bad'); b.disabled = false; b.textContent = '＋ 使用'; }
    }));
  }).catch(e => { $('musicList').innerHTML = '<div class="ins-empty">加载失败：' + _esc(e.message) + '</div>'; });
}
$('musicGo').addEventListener('click', () => loadMusicLib($('musicQ').value.trim()));
$('musicQ').addEventListener('keydown', e => { if(e.key === 'Enter') loadMusicLib($('musicQ').value.trim()); });

/* ── 生成封面（导出面板入口，AI 自动选帧） ── */
$('exCover').addEventListener('click', () => {
  if(!_lastExport){ toast('先合成出成片', 'bad'); return; }
  $('coverTitle').value = _projName || '';
  $('coverSub').value = '';
  $('coverMask').style.display = 'flex';
});
$('coverClose').addEventListener('click', () => $('coverMask').style.display = 'none');
$('coverGo').addEventListener('click', async () => {
  const b = $('coverGo');
  b.disabled = true; b.textContent = '生成中…';
  try {
    const r = await fetch('/api/cover', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({file: _lastExport, title: $('coverTitle').value.trim(),
                            sub: $('coverSub').value.trim(), style: 0})});
    const out = await r.json();
    if(!out.ok) throw new Error(out.error || '生成失败');
    $('coverHint').innerHTML = '✅ 已生成：<b>/media/' + _esc(out.cover) + '</b>（与成片同目录）';
    if(window.pywebview && window.pywebview.api)
      window.pywebview.api.open_folder(_lastExport).catch(() => {});
    toast('✅ 封面已生成 cover.jpg', 'ok');
  } catch(e){ toast('封面失败：' + e.message, 'bad'); }
  b.disabled = false; b.textContent = '生成';
});

/* ── 存储管理（总览 / 单项删 / 一键清理） ── */
function _fmtSize(n){
  if(!n) return '0';
  if(n < 1024 * 1024) return (n / 1024).toFixed(0) + 'KB';
  if(n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(0) + 'MB';
  return (n / 1024 / 1024 / 1024).toFixed(2) + 'GB';
}
function openStorage(){
  $('storeMask').style.display = 'flex';
  loadStorage();
}
$('storeClose').addEventListener('click', () => $('storeMask').style.display = 'none');
$('storeRefresh').addEventListener('click', loadStorage);
let _storeGroups = [];
function loadStorage(){
  $('storeList').innerHTML = '<div class="ins-empty">扫描中…</div>';
  fetch('/api/storage').then(r => r.json()).then(d => {
    _storeGroups = (d && d.groups) || [];
    $('storeSummary').textContent = '项目共 ' + _fmtSize(d.total_bytes)
      + ' · 可回收约 ' + _fmtSize(d.reclaimable_bytes) + ' · 磁盘剩余 ' + _fmtSize(d.free_bytes);
    const rows = [];
    _storeGroups.forEach(g => {
      if(!g.total) return;
      rows.push('<div class="rc-name" style="padding:6px 4px 2px"> <b>' + _esc(g.label) + '</b>'
        + '<small>' + _fmtSize(g.total) + (g.deletable ? ' · 可清理' : ' · 保留') + '</small></div>');
      (g.items || []).slice(0, 12).forEach(it => {
        rows.push('<div class="hist-row"><div class="rc-name">' + _esc(it.name)
          + '<small>' + _fmtSize(it.size) + '</small></div>'
          + (g.deletable ? '<div class="hist-acts"><button class="mini-btn" data-del-rel="' + _esc(it.rel) + '">🗑</button></div>' : '')
          + '</div>');
      });
    });
    $('storeList').innerHTML = rows.join('') || '<div class="ins-empty">很干净，没有占用</div>';
    $('storeList').querySelectorAll('[data-del-rel]').forEach(b => b.addEventListener('click', async () => {
      b.disabled = true;
      try {
        await fetch('/api/storage/delete', {method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({path: b.dataset.delRel})});
        loadStorage();
      } catch(e){ toast('删除失败：' + e.message, 'bad'); b.disabled = false; }
    }));
  }).catch(e => { $('storeList').innerHTML = '<div class="ins-empty">加载失败：' + _esc(e.message) + '</div>'; });
}
$('storeClean').addEventListener('click', async () => {
  if(!confirm('清理所有「可回收」项？（模型权重需重新下载，默认不动）')) return;
  const b = $('storeClean');
  b.disabled = true; b.textContent = '清理中…';
  let n = 0;
  try {
    for(const g of _storeGroups){
      if(!g.deletable || g.tier === 'review') continue;   // 模型档(review)不动
      for(const it of (g.items || [])){
        try {
          await fetch('/api/storage/delete', {method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({path: it.rel})});
          n++;
        } catch(e){ /* 逐项尽力 */ }
      }
    }
    toast('✅ 已清理 ' + n + ' 项', 'ok');
  } finally {
    b.disabled = false; b.textContent = '一键清理可回收';
    loadStorage();
  }
});

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
      fade_in: (it.fade_in || 0), fade_out: (it.fade_out || 0),
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
    if(_music && $('npBgm').checked){
      body.music = _musicPayload();   // 配乐在合成时落盘
    }
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
