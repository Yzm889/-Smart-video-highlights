/* 一帧成片 · 桌面版 v2 —— 应用骨架逻辑
   固定工作站布局（菜单栏/工具栏/三栏编辑器/状态栏），调本地服务既有接口。
   浏览器打开此页 = 桌面能力自动降级（无原生对话框、无本地路径直连）。 */
'use strict';

const $ = id => document.getElementById(id);

/* ── 脚本初始化错误出口（已知坑 #3）──
   顶层大量 $('id').addEventListener(...) 直接绑定：只要某个 id 在 HTML 里不存在，
   就会抛 TypeError，导致该行之后**全部顶层代码静默死亡**——界面不报错、按钮全没反应，
   主人只能看到「点了没反应」。这里把漏网的初始化异常变成可见提示 + 控制台堆栈。
   配合 tests/test_desktop_dom_ids.py 的 CI 静态对账，双保险。 */
let _bootErrors = 0;
function _bootFail(msg){
  _bootErrors++;
  try { console.error('[FrameCut 启动错误]', msg); } catch(e) {}
  try {
    const w = document.createElement('div');
    w.style.cssText = 'position:fixed;z-index:99999;left:12px;right:12px;bottom:12px;'
      + 'padding:10px 14px;background:#7a1f1f;color:#fff;border-radius:8px;'
      + 'font:13px/1.5 system-ui,sans-serif;box-shadow:0 4px 16px rgba(0,0,0,.35)';
    w.setAttribute('role', 'alert');
    w.textContent = '脚本初始化出错（部分按钮可能无反应）：' + msg
      + '　请截图反馈，并按 Ctrl+R 重载。';
    document.body.appendChild(w);
  } catch(e) {}
}
window.addEventListener('error', ev => {
  if(!_bootErrors) _bootFail(ev.message || '未知脚本错误');
  else try { console.error('[FrameCut 运行错误]', ev.message); } catch(e) {}
});
window.addEventListener('unhandledrejection', ev => {
  const r = ev.reason;
  try { console.error('[FrameCut 未处理的异步错误]', r); } catch(e) {}
  if(!_bootErrors) _bootFail('异步操作失败：' + (r && r.message ? r.message : String(r)));
});

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
let _draftTimer = null;       // 草稿写入 debounce 句柄

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

/* ── 运行期提示卡（v1.4.65） ─────────────────────────────────────────
   后端在 PROGRESS[run].notices 里挂「必须让用户当场处理」的问题（目前是
   「台词模型只认英文」）。这里是唯一的展示入口：所有 /api/progress 轮询
   拿到 p 之后调 uiNotices(p) 即可，不在各条流水线里各写一遍弹窗。
   为什么不用 toast：toast 2.6 秒必消失、最多 3 条还会被后续挤掉，而这条
   要求用户去改设置，必须停到他处理或主动关掉。 */
const _fcNoticeSeen = new Set();   // 同 code+model 只弹一次（本次会话）
const _fcNoticeQ = [];
let _fcNoticeOn = false;

const _FC_LANG_CN = {zh:'中文', en:'英语', ja:'日语', ko:'韩语', yue:'粤语',
                     ru:'俄语', fr:'法语', de:'德语', es:'西班牙语', auto:'自动判别'};
function _fcLangName(code){
  if(!code) return '非英文';
  return _FC_LANG_CN[String(code).toLowerCase()] || String(code);
}

function uiNotices(p){
  const list = (p && p.notices) || [];
  for(const n of list){
    if(!n || !n.code) continue;
    const key = n.code + '|' + (n.model || '');
    if(_fcNoticeSeen.has(key)) continue;
    _fcNoticeSeen.add(key);
    _fcNoticeQ.push(n);
  }
  _fcNoticePump();
}
function _fcNoticePump(){
  if(_fcNoticeOn || !_fcNoticeQ.length) return;
  const el = $('fcNotice');
  if(!el) return;
  const n = _fcNoticeQ.shift();
  _fcNoticeOn = true;
  if(n.code === 'asr_model_en_only') _fcRenderAsrModelNotice(n, el);
  else if(n.code === 'asr_model_broken') _fcRenderModelIssueNotice(n, el);
  else { _fcNoticeOn = false; _fcNoticePump(); return; }   // 未知 code 不弹，别拦路
  el.style.display = 'block';
}
function _fcNoticeHide(){
  const el = $('fcNotice');
  if(el) el.style.display = 'none';
  _fcNoticeOn = false;
  setTimeout(_fcNoticePump, 160);
}
function _fcRenderAsrModelNotice(n, el){
  const install = n.reason === 'install';       // 装完模型就提示 vs 跑到一半才发现
  const lang = _fcLangName(n.detected || n.lang);
  const name = _esc(n.model || '当前模型');
  const alt = _esc(n.used || 'base');
  document.getElementById('fcNoticeTitle').textContent = install
    ? '这个台词模型只认英文，中文片会整段变英文'
    : '台词模型只认英文，中文会被转成英文乱码';
  document.getElementById('fcNoticeBody').innerHTML = install
    ? '<b>' + name + '</b> 是英文专用模型（没有中文词表）。用它识别中文/日语，'
      + '会输出一串看着像模像样的英文（「Thank you, you know」这类），不是偶发故障。<br>'
      + '要识别中文请选多语言模型 <b>' + alt + '</b>（已下载，可直接切换）。'
    : '<b>' + name + '</b> 是英文专用模型（没有中文词表）。这次识别到的是 <b>' + lang
      + '语音</b>，它会把中文硬转成英文（「Thank you, you know」这类）。<br>'
      + '本次已自动改用 <b>' + alt + '</b> 继续识别，结果不受影响；'
      + '换成多语言模型之后就不用每次兜底了。';
  const fix = document.getElementById('fcNoticeFix');
  if(n.used){
    fix.style.display = '';
    fix.className = 'mini-btn primary';
    fix.textContent = '换成 ' + n.used;
    fix.onclick = async () => {
      fix.disabled = true;
      const ok = await _fcSwitchWhisperModel(n.used);
      fix.disabled = false;
      if(ok){
        _fcNoticeHide();
        toast('✅ 台词模型已换成 ' + n.used + '，下次识别直接生效', 'ok');
      }
    };
  } else {
    fix.style.display = 'none';
  }
  document.getElementById('fcNoticeMore').onclick = () => { _fcNoticeHide(); openSettings('secWhisper'); };
  document.getElementById('fcNoticeClose').onclick = () => _fcNoticeHide();
}
/* v1.4.81-ux：模型文件有误的弹窗。不写「失败」两个字就完事，必须给出
   ①已下部分还在 ②点哪里续传 ③镜像/代理排查 ④手动放置 ⑤临时换模型。 */
function _fcRenderModelIssueNotice(n, el){
  const name = _esc(n.model || '当前模型');
  const why = _esc(n.reason || '文件不完整');
  document.getElementById('fcNoticeTitle').textContent = '台词模型 ' + name + ' 文件有误';
  document.getElementById('fcNoticeBody').innerHTML =
      '<b>出了什么问题</b>：' + why + '。常见于下载中断、镜像返回不完整或手动拷贝模型。<br>'
    + '<b>已下载的部分还在</b>：下载器支持断点续传，<b>不会从头重下</b>。<br>'
    + '<b>按顺序试</b>：<br>'
    + '① 点下面「重新下载（续传）」——只补缺的部分；<br>'
    + '② 反复失败就去「🌐 国内下载镜像」检查代理是否开着，或切直连；<br>'
    + '③ 也可手动把完整模型放进 <code>models/whisper/</code>，回来点「✓ 校验」；<br>'
    + '④ 急着用先换成已就绪的多语言模型 <b>' + _esc(n.used || 'base') + '</b>。';
  const fix = document.getElementById('fcNoticeFix');
  fix.style.display = '';
  fix.className = 'mini-btn primary';
  fix.textContent = '重新下载（续传）';
  fix.onclick = async () => {
    fix.disabled = true;
    try{
      _fcNoticeHide();
      openSettings('secWhisper');
      const sel = $('setWhisperModel');
      if(sel && n.model && [...sel.options].some(o => o.value === n.model)) sel.value = n.model;
      _wspRefreshCards();
      await _wspStartTask(false);
    } finally { fix.disabled = false; }
  };
  document.getElementById('fcNoticeMore').onclick = () => { _fcNoticeHide(); openSettings('secWhisper'); };
  document.getElementById('fcNoticeClose').onclick = () => _fcNoticeHide();
}
async function _fcSwitchWhisperModel(model){
  try{
    const r = await fetch('/api/ai/config', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({whisper: {model: model}})}).then(x => x.json());
    if(!r.ok) throw new Error(r.error || '保存失败');
    renderWhisperOptions();
    return true;
  }catch(e){
    toast('切换失败：' + e.message, 'bad');
    return false;
  }
}

/* ── 降级提示坞（v1.4.67） ─────────────────────────────────────────
   后端把所有「兜底/降级」写进 PROGRESS[run].warnings，这里常驻展示。
   为什么单独一块：网页版 static/app.js 的 renderWarnings 早就有了，桌面版
   一直没接——于是「画面分析失败/弹幕没用上/封面没出」这些事只落进日志，
   用户看到的是分数照出、结果照有，只是悄悄变差了。 */
const _uiWarnSeen = new Set();     // 同一条只进坞一次（跨任务也去重）
let _uiWarnItems = [];
let _warnDockOpen = false;

function uiWarnings(p){
  const ws = (p && p.warnings) || [];
  let added = false;
  for(const w of ws){
    const t = String(w || '').trim();
    if(!t || _uiWarnSeen.has(t)) continue;
    _uiWarnSeen.add(t);
    _uiWarnItems.push(t);
    added = true;
  }
  if(!added) return;
  if(!_warnDockOpen){ _warnDockOpen = true; }   // 新提示一律先展开，别让人错过
  _warnDockRender();
}
function _warnDockRender(){
  const dock = $('warnDock'), list = $('warnDockList'), n = $('warnDockN');
  if(!dock || !list || !n) return;
  if(!_uiWarnItems.length){ dock.style.display = 'none'; return; }
  dock.style.display = 'block';
  n.textContent = '⚠ 本次 ' + _uiWarnItems.length + ' 条提示';
  list.innerHTML = _uiWarnItems.map(t => '<li>' + _esc(t) + '</li>').join('');
  list.style.display = _warnDockOpen ? 'block' : 'none';
  if($('warnDockTg')) $('warnDockTg').textContent = _warnDockOpen ? '收起' : '展开';
}
function _warnDockBind(){
  const tg = $('warnDockTg'), clr = $('warnDockClr');
  if(tg) tg.addEventListener('click', () => { _warnDockOpen = !_warnDockOpen; _warnDockRender(); });
  if(clr) clr.addEventListener('click', () => {
    _uiWarnItems = []; _uiWarnSeen.clear(); _warnDockOpen = false; _warnDockRender();
  });
}
function uiProgressHints(p){ uiNotices(p); uiWarnings(p); }

/* ── 统一进度轮询（v1.4.69） ────────────────────────────────────────
   以前 7 条流水线各写一遍 setInterval + fetch + 完成/失败判断，结果是：
   · 有的漏接 uiProgressHints（降级提示就丢了）
   · 有的用 setInterval，请求慢时会重叠发
   · 取消/关闭只能各自 clearInterval，容易漏成幽灵轮询
   这里统一成「一次请求 → 判断 → 再排下一次」，并保证 notices/warnings
   只在一个地方处理。onDone 可以抛错，抛了就 reject（给「结束了但没数据」
   这种情形用）。 */
function stopHandle(h){
  if(!h) return;
  if(typeof h.stop === 'function') h.stop();
  else clearInterval(h);           // 兼容历史 setInterval 句柄
}
function pollRun(runid, o){
  o = o || {};
  const iv = Math.max(300, o.interval || 1200);
  const t0 = Date.now();
  const limit = Math.max(0, o.timeout || 0);      // 0 = 不限时
  let stopped = false, timer = null, resolveFn = null, rejectFn = null;
  const promise = new Promise((res, rej) => { resolveFn = res; rejectFn = rej; });
  const handle = {
    promise: promise,
    stop: function(){
      if(stopped) return;
      stopped = true;
      if(timer) clearTimeout(timer);
      timer = null;
      try{ o.onStop && o.onStop(); }catch(e){ /* 清理失败不影响主流程 */ }
    }
  };
  const next = () => { if(!stopped) timer = setTimeout(tick, iv); };
  async function tick(){
    if(stopped) return;
    let p;
    try{
      // o.url 可指向别的状态接口（模型下载等）；默认就是任务进度
      p = await fetch(o.url || ('/api/progress?run=' + encodeURIComponent(runid)),
                      {cache: 'no-store'}).then(x => x.json());
    }catch(e){
      if(limit && Date.now() - t0 > limit){
        handle.stop();
        rejectFn(new Error('等待进度超时'));
        return;
      }
      next(); return;              // 网络抖动：下次再试，不打断流水线
    }
    if(stopped) return;
    uiProgressHints(p);            // 唯一入口：notices + warnings 都不会漏
    try{ o.onTick && o.onTick(p); }catch(e){ console.warn('[pollRun] onTick:', e); }
    if(o.isError ? o.isError(p) : !!p.error){
      handle.stop();
      const err = new Error((p && p.error) || '任务失败');
      err.progress = p;
      try{ o.onError && o.onError(p, err); }catch(e){ /* 回调异常不二次抛 */ }
      rejectFn(err);
      return;
    }
    if(o.isDone ? o.isDone(p) : !!p.done){
      let thrown = null;
      try{ if(o.onDone) await o.onDone(p); }catch(e){ thrown = e; }
      handle.stop();
      if(thrown) rejectFn(thrown); else resolveFn(p);
      return;
    }
    next();
  }
  tick();
  return handle;
}
_warnDockBind();     // 脚本在 body 末尾，此时 #warnDock 已存在
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
  _setEntryActive('tbNarrate');   // 编辑器/解说页都是「解说」入口的领地
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

/* ── 桌面模式初始化：桥重试 60s + 模式徽章永远可见
   【主人实测踩中】EXE 里桥未连上时页面静默退化成上传模式——
   选片变上传 5GB。现在：初始化持续重试；模式显示在状态栏；选片前先确保桥就绪 */
let _desktopInited = false;
function _setModeBadge(){
  const b = $('sbMode');
  if(!b) return;
  if(_isDesktop){ b.textContent = '● 桌面直连'; b.className = 'mode-ok'; }
  else if(window.pywebview){ b.textContent = '● 桥连接中…'; b.className = 'mode-warn'; }
  else { b.textContent = '● 浏览器模式（原生选片不可用）'; b.className = 'mode-warn'; }
}
function initDesktop(){
  if(_desktopInited) return;
  const api = window.pywebview && window.pywebview.api;
  if(!api || !api.get_desktop_info){ _setModeBadge(); return; }
  api.get_desktop_info().then(info => {
    if(!info || !info.token){ _setModeBadge(); return; }
    _token = info.token;
    _isDesktop = true;
    _setModeBadge();
  }).catch(e => { _setModeBadge(); console.warn('桌面初始化失败', e); });
}
window.addEventListener('pywebviewready', initDesktop);
if(window.pywebview) initDesktop();
let _initTries = 0;
const _initRetry = setInterval(() => {
  if(_isDesktop){ clearInterval(_initRetry); _setModeBadge(); return; }
  if(window.pywebview && window.pywebview.api) initDesktop();
  if(++_initTries > 120) clearInterval(_initRetry);
}, 500);
setTimeout(_setModeBadge, 100);
async function _ensureBridge(){
  if(_isDesktop) return true;
  if(window.pywebview && window.pywebview.api){
    initDesktop();
    for(let i = 0; i < 6 && !_isDesktop; i++){ await new Promise(r => setTimeout(r, 500)); }
  }
  return _isDesktop && !!(window.pywebview && window.pywebview.api);
}

/* ── 桥调用失败的统一出口：绝不许静默吞 ──
   v1.4.7 教训：DesktopApi.open_folder 因模块漏导入必然抛 NameError，而这里当时
   写的是 `open_folder(...).catch(() => {})`，异常被吃掉，主人点「打开文件夹」
   看到的就是「点了没反应」，坏了很久都没人发现。凡是主人主动点击触发的桥调用，
   失败必须看得见。 */
function bridgeFail(action, e){
  const msg = (e && e.message) ? e.message : String(e || '未知错误');
  try { console.warn('[bridge]', action, e); } catch(_) {}
  toast(action + '失败：' + msg, 'bad');
}
/* 打开成片所在文件夹（桌面版专属）。返回是否成功，便于调用方决定要不要提示。 */
async function openFolder(rel){
  if(!(window.pywebview && window.pywebview.api)){
    toast('成片在 webui_output 目录');
    return false;
  }
  try {
    await window.pywebview.api.open_folder(rel);
    return true;
  } catch(e){
    bridgeFail('打开文件夹', e);
    return false;
  }
}

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

/* 【2026-11-11 主人拍板】环境检查改为按需：就绪时不占首屏（自动收起），
   没就绪才自动展开；顶栏「环境检查」按钮随时可开/收（再点一次收起）。 */
let _envOpen = false;    // 用户主动点开了环境卡（点收起/再点按钮 = 关）
let _envReady = null;    // null=检测中 / true / false

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
  _envReady = coreOk;
  if(!_envOpen){
    // 就绪 = 不弹（解说页直接可用）；没就绪 = 自动展开，别让用户猜
    $('envCard').style.display = coreOk ? 'none' : '';
  }
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

/* ── 剧情稿 / 片名 草稿自动保存 ──
   【主人实测痛点】长篇剧情稿（可能上千字）此前没有任何持久化：刷新页面、关窗口、
   或误按一次 Ctrl+N 就当场蒸发且无法撤销。这里把「项目级输入」存进 localStorage，
   下次打开自动回填；只有用户在 newProject() 里明确确认后才真正清除。

   进草稿的三项，其余刻意排除（宁少勿滥）：
   - npPlot  剧情稿/自写解说稿，最长最贵 → 必存
   - npName  片名，是「查剧情」的关键字，重做同一影片时每次都要重打 → 必存
   - npScript（复选框）→ 必存，而且**必须与 npPlot 同生共死**：它决定同一段
     npPlot 文本的语义（勾上 = 这是写好的一字不改的完整解说稿；不勾 = 这是
     剧情梗概，由 AI 自己写解说）。只回填文字不回填勾选，恢复出来的就是一份
     自相矛盾的半份状态：主人明明写好了整篇稿子，却被静默当成剧情稿重写，
     「文字不改」的承诺当场失效。
     （注意：npScript 不在 ADV_IDS、也不在 ADV_CHECKS 里 —— 那是 npCut/npBgm，
       与 advSave() 无关，所以它此前没有任何持久化，不是一个「已存在的真相来源」。）
   排除：coverTitle / coverSub —— 每次点「生成封面」都会被 _projName 覆盖 / 清空；
        musicQ —— 每次打开音乐库都被清空。这三者是会话瞬态，存了反而会盖掉用户新输入。
   排除：setXxx 走 ai_config 后端持久化，cloneName / presetNameInput 是模态输入框。
   注意坑 3：所有 DOM 取用一律守卫式（const el = $('x'); if(el) ...），
   本文件顶层任何一次裸取不存在的 id 都会让它之后的顶层代码静默死亡。 */
const DRAFT_KEY = 'framecut_desktop_draft_v1';
const DRAFT_TEXT_IDS = ['npPlot', 'npName'];
const DRAFT_CHECKS = ['npScript'];
const DRAFT_DEBOUNCE_MS = 400;

/* v1.4.37：视频/字幕持久化——主人同一影片反复重做 5+ 次，每次开窗口都要
   重新选视频、重新导入字幕。现在 localStorage 记住上次的，新建项目页一键复用。
   只存桌面版本地路径（isLocalPath=true）；浏览器上传的 upload_id 会过期，不存。 */
const LAST_VIDEO_KEY = 'framecut_desktop_last_video_v1';
const LAST_SRT_KEY = 'framecut_desktop_last_srt_v1';

function lastVideoSave(){
  try {
    if(_video && _video.isLocalPath){
      localStorage.setItem(LAST_VIDEO_KEY, JSON.stringify({name: _video.name, path: _video.path}));
    }
  } catch(e){}
}
function lastVideoRead(){
  try { return JSON.parse(localStorage.getItem(LAST_VIDEO_KEY) || 'null') || null; }
  catch(e){ return null; }
}
function lastVideoClear(){
  try { localStorage.removeItem(LAST_VIDEO_KEY); } catch(e){}
}

function lastSrtSave(){
  try {
    if(_srt && _srt.name && _srt.text){
      localStorage.setItem(LAST_SRT_KEY, JSON.stringify({name: _srt.name, text: _srt.text}));
    }
  } catch(e){}
}
function lastSrtRead(){
  try { return JSON.parse(localStorage.getItem(LAST_SRT_KEY) || 'null') || null; }
  catch(e){ return null; }
}
function lastSrtClear(){
  try { localStorage.removeItem(LAST_SRT_KEY); } catch(e){}
}

/* v1.4.51：配乐持久化——视频/字幕记住了，但选中的 BGM 每次开窗还要重新选。
   与视频/字幕同一规则：只存桌面版本地路径（isLocalPath=true）；
   浏览器上传的 upload_id 会过期、base64 太大，都不存。 */
const LAST_MUSIC_KEY = 'framecut_desktop_last_music_v1';
function lastMusicSave(){
  try {
    if(_music && _music.isLocalPath && _music.path){
      localStorage.setItem(LAST_MUSIC_KEY, JSON.stringify({name: _music.name, path: _music.path}));
    }
  } catch(e){}
}
function lastMusicRead(){
  try { return JSON.parse(localStorage.getItem(LAST_MUSIC_KEY) || 'null') || null; }
  catch(e){ return null; }
}
function lastMusicClear(){
  try { localStorage.removeItem(LAST_MUSIC_KEY); } catch(e){}
}
/* 复用上次的配乐：本地路径形态直接可用（合成时带 desktop_token） */
function reuseLastMusic(){
  const lm = lastMusicRead();
  if(!lm) return false;
  _music = {name: lm.name, path: lm.path, isLocalPath: true};
  setBtnIcon('tbMusic', 'i-music', _music.name.slice(0, 14));
  const bg = $('npBgm');
  if(bg) bg.checked = true;
  return true;
}

/* 新建项目页渲染复用按钮（有历史记录时才显示） */
function _renderLastMedia(){
  const lv = lastVideoRead();
  const ls = lastSrtRead();
  const lm = lastMusicRead();
  const slot = $('npLastMedia');
  if(!slot) return;
  let html = '';
  if(lv){
    html += '<button class="mini-btn" id="npReuseVideo" title="复用上次的视频">'
          + '<svg class="ic"><use href="#i-folder"/></svg>📹 上次视频：' + _esc(lv.name) + '</button>';
  }
  if(ls){
    html += '<button class="mini-btn" id="npReuseSrt" title="复用上次的字幕">'
          + '<svg class="ic"><use href="#i-subtitle"/></svg>📝 上次字幕：' + _esc(ls.name) + '</button>';
  }
  if(lm){
    html += '<button class="mini-btn" id="npReuseMusic" title="复用上次的配乐">'
          + '<svg class="ic"><use href="#i-music"/></svg>🎵 上次配乐：' + _esc(lm.name) + '</button>';
  }
  // v1.4.51：三样都在时给「一键全部复用」——重做同一部片子的最后一步也省掉
  if(lv && ls && lm){
    html += '<button class="mini-btn" id="npReuseAll" title="视频+字幕+配乐一次复用">'
          + '<svg class="ic"><use href="#i-folder"/></svg>📦 一键复用上次全部</button>';
  }
  slot.innerHTML = html;
  if(lv){
    const b = $('npReuseVideo');
    if(b) b.addEventListener('click', () => {
      acceptVideo(lv.name, lv.path);
      toast('✅ 已复用上次视频：' + lv.name, 'ok');
    });
  }
  if(ls){
    const b = $('npReuseSrt');
    if(b) b.addEventListener('click', () => {
      _confirmSrtNames().then(ok => {   // v1.4.47：复用也是导入，同样提醒
        if(!ok) return;
        _srt = {name: ls.name, text: ls.text};
        _syncSrtUi();
        toast('✅ 已复用上次字幕：' + ls.name, 'ok');
      });
    });
  }
  if(lm){
    const b = $('npReuseMusic');
    if(b) b.addEventListener('click', () => {
      if(reuseLastMusic()) toast('✅ 已复用上次配乐：' + lm.name + '（已勾选 BGM）', 'ok');
    });
  }
  if(lv && ls && lm){
    const b = $('npReuseAll');
    if(b) b.addEventListener('click', () => {
      let n = 0;
      if(lv){ acceptVideo(lv.name, lv.path); n++; }
      _srt = {name: ls.name, text: ls.text}; _syncSrtUi(); n++;   // 一键复用免弹窗（主动选择即已知提醒）
      if(reuseLastMusic()) n++;
      toast('✅ 已复用上次全部：视频 + 字幕 + 配乐（' + n + ' 项）', 'ok');
    });
  }
}
function draftRead(){
  try { return JSON.parse(localStorage.getItem(DRAFT_KEY) || 'null') || null; }
  catch(e){ return null; }
}
function draftSnapshot(){
  const v = {};
  DRAFT_TEXT_IDS.forEach(id => { const el = $(id); if(el) v[id] = el.value; });
  // 复选框不能走 el.value（永远得到 'on'）：按 checked 存布尔。
  DRAFT_CHECKS.forEach(id => { const el = $(id); if(el) v[id] = !!el.checked; });
  return v;
}
/* 「有没有内容」只看**文本**字段。只勾了复选框、稿子却空着，不算有内容 ——
   否则一个纸一样白的窗口点「新建」都会弹确认框，把空白零打扰的手感毁掉。
   （只勾复选框没意义：那个勾只有在有稿子时才有语义，单独恢复一个勾等于没事找事。） */
function draftHasContent(v){
  return !!v && DRAFT_TEXT_IDS.some(id => typeof v[id] === 'string' && v[id].trim() !== '');
}
function draftSave(){
  const v = draftSnapshot();
  try {
    // 用户把稿子自己清空了 → 草稿跟着删，免得下次打开又冒出来
    if(draftHasContent(v)) localStorage.setItem(DRAFT_KEY, JSON.stringify(v));
    else localStorage.removeItem(DRAFT_KEY);
  } catch(e){}
}
function draftClear(){
  // 先把还没落盘的 debounce 取消：否则清空后 400ms 内那个定时器又会把旧稿写回去
  if(_draftTimer){ clearTimeout(_draftTimer); _draftTimer = null; }
  try { localStorage.removeItem(DRAFT_KEY); } catch(e){}
}
function draftRestore(){
  const v = draftRead();
  if(!draftHasContent(v)) return false;
  DRAFT_TEXT_IDS.forEach(id => {
    const el = $(id);
    if(el && typeof v[id] === 'string') el.value = v[id];
  });
  // 复选框必须跟文本一起回填：只回填文字不回填勾选，恢复出来的是半份状态
  // —— 主人写好整篇稿子（勾了「完整解说稿」）会被静默当成剧情稿重写。
  DRAFT_CHECKS.forEach(id => {
    const el = $(id);
    if(el && typeof v[id] === 'boolean') el.checked = v[id];
  });
  if(typeof updateNewProj === 'function') updateNewProj();
  return true;
}
/* newProject 用：返回「当前框里有没有会被清掉的内容」，没有就返回 null，
   好让「完全空白时点新建」保持零打扰（一键清空的老手感不丢）。 */
function draftPending(){
  const v = draftSnapshot();
  return draftHasContent(v) ? v : null;
}
/* 输入即存：debounce 400ms，停止敲字后就落盘，切换视图/下次打开都在。
   用 'input' 而不是 'change'——textarea 的 change 要等失焦才触发，
   主人写了一千字直接关窗口就白写了。 */
function draftScheduleSave(){
  if(_draftTimer) clearTimeout(_draftTimer);
  _draftTimer = setTimeout(() => { _draftTimer = null; draftSave(); }, DRAFT_DEBOUNCE_MS);
}
DRAFT_TEXT_IDS.forEach(id => {
  const el = $(id);
  if(el) el.addEventListener('input', draftScheduleSave);
});
// 复选框绑 'change'（checkbox 不发 input 事件）。注意 newProjectReset() 里的
// `checked = false` 是**程序赋值**，不会触发 change，所以清空后不会反而把草稿写回去。
DRAFT_CHECKS.forEach(id => {
  const el = $(id);
  if(el) el.addEventListener('change', draftScheduleSave);
});
/* 页面加载即回填草稿。放在这里（updateNewProj 已定义、且在任何异步任务之前），
   保证刷新/重开窗口后剧情稿和片名都还在；回填不算「用户输入」，故不触发 draftSave。
   注意：状态栏文字统一在文件末尾的「启动」段落地（这里若也 setStatus，
   会被末尾那句 setStatus('就绪') 立刻盖掉）。 */
const _draftRestored = draftRestore();
_renderLastMedia();   // v1.4.37：有上次视频/字幕时显示一键复用按钮

$('npPick').addEventListener('click', async () => {
  if(await _ensureBridge()){
    try {
      const r = await window.pywebview.api.pick_video();
      if(r && r.path) acceptVideo(r.name || r.path.split(/[\\/]/).pop(), r.path);
    } catch(e){ toast('选择失败：' + e.message, 'bad'); }
  } else {
    // 浏览器模式兜底：标准文件选择 + 分片上传（与网页版同一套后端），不再是死胡同
    _browserFileInput('video/*', async f => {
      const label = $('npPickLabel');
      if(f.size > 300 * 1024 * 1024){
        toast('⚠ ' + _fmtMB(f.size) + ' 的大片在浏览器上传又慢又可能失败——建议双击 FrameCut.exe，本地直连秒选不上传', 'bad');
      }
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
  async function sendChunk(idx, blob, attempt){
    const ctl = new AbortController();
    const killer = setTimeout(() => ctl.abort(), 60000);   // 【实测踩中】单片挂起无超时 → 5GB 上传卡死在 26%
    try {
      const fd = new FormData();
      fd.append('upload_id', uid);
      fd.append('idx', String(idx));
      fd.append('chunk', blob);
      const r = await fetch('/api/upload/chunk', {method:'POST', body: fd, signal: ctl.signal}).then(x => x.json());
      if(!r.ok) throw new Error(r.error || ('分片 ' + idx + ' 失败'));
      return true;
    } catch(e){
      if(attempt < 2) return await sendChunk(idx, blob, attempt + 1);   // 重试 2 次
      throw e;
    } finally {
      clearTimeout(killer);
    }
  }
  async function worker(){
    while(next < total){
      const idx = next++;
      const blob = file.slice(idx * CHUNK, Math.min(file.size, (idx + 1) * CHUNK));
      await sendChunk(idx, blob, 0);
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
  lastVideoSave();   // v1.4.37：记住上次视频，下次一键复用
  $('npPick').classList.add('has');
  $('npPickLabel').textContent = '✅ ' + name;
  $('projName').textContent = name.replace(/\.[^.]+$/, '');
  updateNewProj();
}
$('npSrt').addEventListener('click', importSrt);
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

/* ── 画面理解档位：切换即解释差异，避免"点了不知道等多久" ── */
const TIER_TIP = {
  speed: '极速：只看台词附近的画面，没有台词的打斗/对视/吻戏不会进候选池——只适合先调文案和配音（画面索引 10 秒）。',
  balanced: '平衡：全片每 12 秒看一帧，无台词高光镜头也会进候选池；实测画面索引约 1 分钟，整条约 11 分钟。',
  quality: '质量：每 8 秒看一帧 + 每帧 5 个字段 + 更严的相似帧复用，动作节奏快的片子选它；画面索引约 3 分钟。',
  ultra: '极致：每 5 秒一帧、动作匹配权重加倍，最接近"解说与画面严丝合缝"；画面索引约 8 分钟。'
};
document.addEventListener('change', (e) => {
  if (e.target && e.target.id === 'npTier') {
    const t = e.target.value;
    if ($('npTierHint')) $('npTierHint').textContent = TIER_TIP[t] || '';
  }
});

/* ── 高级设置（生成与合成共用一套参数，改动自动记忆） ── */
const ADV_KEY = 'framecut_desktop_adv_v1';
// 会被「记住」的字段。ADV_IDS 里都是 select / text，取值用 .value；
// ADV_CHECKS 是复选框，必须用 .checked —— 用 .value 往返会把它们永远存成 'on'。
// npTier / npCut / npBgm 原来不在这个表里（只有 _advParams() 读它们）：
// 用户在「新建项目」选的画面理解档位、精剪、BGM 每次开窗口都被忘掉，
// 静默退回 index.html 里写死的默认值（档位回 balanced、精剪回开、BGM 回关）。
const ADV_IDS = ['advTarget','advRes','advColor','advTrans','advQual','advBurn',
  'advFontSize','advColor1','advOutline','advOutlineW','advAlign','advMarginV','advLayers','advVoice',
  'npTier'];
const ADV_CHECKS = ['npCut','npBgm'];
const ADV_ALL = ADV_IDS.concat(ADV_CHECKS);
function _advRead(id){
  const el = $(id);
  if(!el) return undefined;
  return el.type === 'checkbox' ? !!el.checked : el.value;
}
function _advWrite(id, val){
  const el = $(id);
  if(!el) return;
  if(el.type === 'checkbox') el.checked = !!val; else el.value = val;
}
function _advTierHint(){
  const t = $('npTier') ? $('npTier').value : '';
  if($('npTierHint')) $('npTierHint').textContent = TIER_TIP[t] || '';
}
function _advParams(){
  return {
    maxSeg: 25, autoCut: $('npCut').checked,
    // 画面理解档位：决定是否覆盖无台词镜头、每帧让模型写多少字
    narrTier: ($('npTier') ? $('npTier').value : 'balanced'),
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
  ADV_ALL.forEach(id => { const val = _advRead(id); if(val !== undefined) v[id] = val; });
  try { localStorage.setItem(ADV_KEY, JSON.stringify(v)); } catch(e){}
}
function advLoad(){
  let v = null;
  try { v = JSON.parse(localStorage.getItem(ADV_KEY) || 'null'); } catch(e){}
  if(!v) return;
  ADV_ALL.forEach(id => { if(v[id] !== undefined) _advWrite(id, v[id]); });
  _advTierHint();               // 档位提示文字要跟着恢复后的值走，否则与下拉不一致
}
$('npAdvToggle').addEventListener('click', () => {
  const b = $('npAdvBody');
  const show = b.style.display === 'none';
  b.style.display = show ? 'block' : 'none';
  $('npAdvArrow').textContent = show ? '收起 ▴' : '展开 ▾';
});
ADV_ALL.forEach(id => { const el = $(id); if(el) el.addEventListener('change', advSave); });
advLoad();

/* ── 参数预设（同一影片反复重做 5+ 次、每次 30+ 字段的痛点）──
   只做命名保存 / 一键套用 / 删除，复用 ADV_ALL 与 advSave，
   不动 _advParams() 与任何后端接口；纯 localStorage，无新依赖。 */
const PRESET_KEY = 'framecut_param_presets';
function presetAll(){
  try { return JSON.parse(localStorage.getItem(PRESET_KEY) || '{}') || {}; }
  catch(e){ return {}; }
}
function presetWrite(all){
  try { localStorage.setItem(PRESET_KEY, JSON.stringify(all)); return true; }
  catch(e){ toast('预设保存失败：本地存储不可用', 'bad'); return false; }
}
function presetCurrent(){
  const v = {};
  ADV_ALL.forEach(id => { const val = _advRead(id); if(val !== undefined) v[id] = val; });
  return v;
}
function presetFill(){
  const sel = $('presetSel'), all = presetAll();
  const keep = sel.value;
  sel.innerHTML = '<option value="">（上次使用的参数）</option>';
  Object.keys(all).sort().forEach(name => {
    const o = document.createElement('option');
    o.value = name; o.textContent = name;
    sel.appendChild(o);
  });
  if(all[keep] !== undefined) sel.value = keep;
}
function presetHint(msg){
  const h = $('presetHint');
  if(h){ h.textContent = msg || ''; }
}
/* 页面内模态替代 window.prompt / window.confirm：
   pywebview(WebView2) 的原生 JS 对话框可能被压到窗口后或静默失败，
   主人只会看到「点了没反应」（v1.4.1 已踩过）。返回 Promise<string|null>：
   确定 → 输入值（确认模式下为 'ok'），取消/Esc → null。 */
let _presetModalResolve = null;
function presetModal(mode, title, defaultName, detail){
  const mask = $('presetMask'), input = $('presetNameInput'), body = $('presetModalBody');
  const isInput = mode === 'input';
  $('presetModalTitle').textContent = title;
  body.style.display = isInput ? 'block' : 'none';
  const msg = $('presetModalMsg');
  if(msg){
    msg.style.display = isInput ? 'none' : 'block';
    msg.textContent = isInput ? '' : (detail || '');
  }
  input.value = defaultName || '';
  mask.style.display = 'flex';
  if(isInput){ setTimeout(() => { input.focus(); input.select(); }, 0); }
  return new Promise(resolve => {
    _presetModalResolve = resolve;
  });
}
/* 统一确认框：替代全项目剩余的原生 confirm()（同类静默失败风险）。
   用法：if(!await uiConfirm('标题', '补充说明', '确定删除')) return; */
async function uiConfirm(title, detail, okText){
  const ok = $('presetModalOk'), prev = ok ? ok.textContent : '';
  if(ok && okText) ok.textContent = okText;
  try {
    return !!(await presetModal('confirm', title, '', detail || ''));
  } finally {
    if(ok && okText) ok.textContent = prev;
  }
}
function presetModalClose(result){
  const mask = $('presetMask');
  if(mask.style.display === 'none') return;
  mask.style.display = 'none';
  const fn = _presetModalResolve;
  _presetModalResolve = null;
  if(fn) fn(result);
}
$('presetModalOk').addEventListener('click', () => {
  const input = $('presetNameInput');
  const body = $('presetModalBody');
  const val = body.style.display === 'none' ? 'ok' : (input.value || '').trim();
  if(!val){ input.focus(); return; }
  presetModalClose(val);
});
$('presetModalCancel').addEventListener('click', () => presetModalClose(null));
$('presetMask').addEventListener('click', e => {
  if(e.target === $('presetMask')) presetModalClose(null);
});
$('presetNameInput').addEventListener('keydown', e => {
  if(e.key === 'Enter'){ e.preventDefault(); $('presetModalOk').click(); }
});
$('presetSave').addEventListener('click', async () => {
  const name = await presetModal('input', '给这套参数起个名字', '');
  if(!name) return;
  const all = presetAll();
  const overwrite = all[name] !== undefined;
  all[name] = presetCurrent();
  if(!presetWrite(all)) return;
  presetFill();
  $('presetSel').value = name;
  presetHint((overwrite ? '已更新' : '已保存') + '「' + name + '」· 含 '
    + Object.keys(all[name]).length + ' 个参数');
  toast((overwrite ? '已更新' : '已保存') + '预设：' + name, 'ok');
});
$('presetSel').addEventListener('change', () => {
  const name = $('presetSel').value, all = presetAll();
  if(!name || !all[name]){ presetHint(''); return; }
  const v = all[name];
  ADV_ALL.forEach(id => { if(v[id] !== undefined) _advWrite(id, v[id]); });
  _advTierHint();                  // 与 advLoad 同理：提示要跟上下拉
  advSave();                       // 套用后同步「上次参数」，重开窗口仍是这套
  presetHint('已套用「' + name + '」· 下次生成即生效');
  toast('已套用预设：' + name, 'ok');
});
$('presetDel').addEventListener('click', async () => {
  const name = $('presetSel').value, all = presetAll();
  if(!name || !all[name]){ toast('请先选择一个预设', 'bad'); return; }
  const yes = await presetModal('confirm', '删除预设「' + name + '」？', '');
  if(!yes) return;
  delete all[name];
  if(!presetWrite(all)) return;
  presetFill();
  $('presetSel').value = '';
  presetHint('已删除「' + name + '」');
  toast('已删除预设：' + name, 'ok');
});
presetFill();

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
  stopHandle(_pollTimer);
  _pollTimer = pollRun(runid, {
    interval: 1200,
    onTick: p => {
      $('gmFill').style.width = Math.min(100, p.pct || 0) + '%';
      // 【v1.4.52 配音失败可见化】首轮失败段实时可见（tts_failures 已随 progress 下发）
      if(p.tts_failures && p.tts_failures.length){
        const badIdx = {};
        p.tts_failures.forEach(f2 => { badIdx[f2.idx] = 1; });
        $('gmMeta').textContent = (p.phase || '处理中') + ' · ' + (p.pct || 0) + '%'
          + ' · ⚠ 首轮失败 ' + Object.keys(badIdx).length + ' 段（重试中，最终结果见调整页标记）';
      } else {
        $('gmMeta').textContent = (p.phase || '处理中') + ' · ' + (p.pct || 0) + '%';
      }
    },
    onError: p => {
      $('genMask').style.display = 'none';
      $('npGo').disabled = false;
      toast('生成失败：' + p.error, 'bad');
    },
    isDone: p => !!(p.done && p.tts_list && p.tts_list.length),
    onDone: async p => {
      _runDir = p.run_dir || '';
      _videoFile = p.video_file || '';
      $('genMask').style.display = 'none';
      await loadAdjustItems(_runDir);
      openEditor();
      // 【v1.4.52】完成提示带上失败汇总（宁可不静默：哪几段曾经失败，调整页有 ⚠ 标记）
      const nf = (p.tts_failures || []).length;
      toast('✅ 已生成 ' + p.tts_list.length + ' 段配音与画面方案'
        + (nf ? '（⚠ ' + nf + ' 条失败记录，详情见调整页 ⚠ 标记）' : ''));
    }
  }).promise.catch(() => { /* 失败已在 onError 里提示 */ });
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
  stopHandle(_pollTimer); _pollTimer = null;
  $('genMask').style.display = 'none';
  $('npGo').disabled = false;
  toast('已取消生成');
});
$('sbCancel').addEventListener('click', async () => {
  if(!_pollRunId) return;
  try { await fetch('/api/cancel', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({runid: _pollRunId})}); } catch(e){ /* 尽力取消 */ }
  stopHandle(_pollTimer); _pollTimer = null;
  $('sbCancel').style.display = 'none';
  const btn = $('tbCompose'); btn.disabled = false;
  setStatus('已取消合成', 'warn');
  toast('已取消合成');
});

/* ── 加载调整数据 ── */
/* 【v1.4.46 成片时间轴】_composed = compose 落盘的成片方案（/api/tts_state 的
   composed 节）。有它：预览直接播成片 final.mp4、时间轴按成片累计坐标、段画面
   范围用 plan 的 src_start/src_end（真剪实际范围）。没有：回落源片坐标（旧任务
   兼容，行为同 v1.4.45 之前）。 */
let _composed = null;
function _outMode(){ return !!(_composed && _composed.segments && _composed.segments.length); }
function _tlTotal(){ return _outMode() ? Math.max(1, _composed.total || 0) : Math.max(10, _vdur || 0); }
/* 拖拽/检查器改了段长后重排成片坐标：累计各段 (video_end-video_start)。
   这是「下一次合成的近似预览」（合成端还有 ±15% 微变速与念完钳制，最终以成片为准） */
function _relayout(){
  if(!_outMode()) return;
  let cur = 0;
  _items.forEach(it => {
    const len = Math.max(0.1, (it.video_end || 0) - (it.video_start || 0));
    it.out_start = Math.round(cur * 100) / 100;
    it.out_end = Math.round((cur + len) * 100) / 100;
    cur += len;
  });
  _composed.total = Math.round(cur * 100) / 100;
}
/* ── v1.4.77 失败分类码 → 动作建议：光知道「失败了」没用，得告诉用户下一步干嘛 ── */
const TTS_FAIL_HINTS = {
  vram: '（显存不足：关掉占显卡的程序后重试该段）',
  asset: '（模型文件缺失：到「设置 → 模型中心」检查配音模型）',
  crash: '（引擎崩溃：到设置页点「重试引擎」后重配该段）',
  timeout: '（合成超时：显卡可能被别的程序拖慢，重试该段；反复出现请关占卡程序）',
  engine: '（引擎报错：重试该段；反复失败请查看 tts2 日志）'
};
function _ttsFailHint(code){
  return TTS_FAIL_HINTS[code] || '';
}

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
  _composed = (st.composed && st.composed.segments && st.composed.segments.length) ? st.composed : null;
  // 【v1.4.52 配音失败可见化】首轮失败段（idx→error）标记到条目上，列表打 ⚠
  const _failMap = {};
  (st.tts_failures || []).forEach(f2 => {
    if(f2 && f2.idx !== undefined && !_failMap[f2.idx]) _failMap[f2.idx] = f2;
  });  if(_composed){
    const byIdx = {};
    _composed.segments.forEach(sg => { byIdx[sg.index] = sg; });
    _items.forEach(it => {
      const sg = byIdx[it.index];
      if(!sg) return;
      it.video_start = sg.src_start;      // 真剪实际范围（合成端提交的就是它）
      it.video_end = sg.src_end;
      it.out_start = sg.out_start;        // 成片坐标（预览/时间轴用）
      it.out_end = sg.out_end;
      if(!it.duration) it.duration = sg.duration || 0;
    });
  } else {
    _items.forEach(it => { delete it.out_start; delete it.out_end; });
  }
  _items.forEach(it => {
    const f2 = _failMap[it.index];
    it._ttsFail = f2 ? ((f2.error || '配音失败') + _ttsFailHint(f2.code)) : '';
  });
  _sel = -1;
  renderCaps(); ensureNarrElements(); renderTimeline(); setupPreview();
  _proxyFile = ''; _stopProxyPoll();
  ensureProxy();
  $('capCount').textContent = '· ' + _items.length + ' 段';
  $('tbCompose').disabled = false;
  $('stEngine').textContent = _engineSummary();
}

/* ── 4K 预览代理（720p 副本，合成始终用原片） ── */
let _proxyFile = '', _proxyTimer = null;
function _stopProxyPoll(){ if(_proxyTimer){ clearInterval(_proxyTimer); _proxyTimer = null; } }
function _applyProxyFile(f){
  if(!f || f === _proxyFile) return;
  _proxyFile = f;
  const v = $('pvVideo');
  if(!v) return;
  v.pause();                                   // 只在暂停态热切换，避免播放中换源
  v.src = '/media/' + _runDir.replace(/\\/g, '/') + '/' + f;
}
function ensureProxy(){
  if(!_runDir) return;
  if(_outMode()) return;   // 成片模式：预览已直读 final.mp4，无需 720p 代理
  const ask = () => fetch('/api/preview_proxy?run_dir=' + encodeURIComponent(_runDir)
    + '&name=' + encodeURIComponent((_videoFile || '').split('/').pop()))
    .then(r => r.json()).catch(() => null);
  ask().then(res => {
    if(!res || !res.ok) return;                            // 异常：静默用原片
    if(res.mode === 'original') return;                    // ≤720p：原片足够
    if(res.ready && res.file){ _applyProxyFile(res.file); return; }
    if(_proxyTimer) clearInterval(_proxyTimer);
    let n = 0;
    _proxyTimer = setInterval(() => {                      // 后台转码中，4s 轮询（上限 10 分钟）
      if(++n > 150){ _stopProxyPoll(); return; }
      ask().then(r2 => {
        if(r2 && r2.ready && r2.file){ _stopProxyPoll(); _applyProxyFile(r2.file); }
      }).catch(() => {});
    }, 4000);
  });
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
    // 成片模式：解说段时间显示用成片坐标；引用段显示源片区间（原声在源片的位置）
    const vs = (it.quote || !_outMode() || it.out_start === undefined)
      ? (it.video_start || 0) : it.out_start;
    const sel = i === _sel ? ' sel' : '';
    const skip = it._skip ? ' skip' : '';
    if(it.quote){
      return '<div class="cap-row' + sel + skip + '" data-i="' + i + '"><div class="row-top">'
        + '<span class="cap-time" style="color:#4ec9b0">' + _fmt(vs) + '</span>'
        + '<span class="cap-quote">🎬 原声引用 ' + (+vs).toFixed(1) + '-' + (+(it.video_end||0)).toFixed(1) + 's</span>'
        + '<button class="cap-del" data-del="' + i + '">✕</button></div></div>';
    }
    return '<div class="cap-row' + sel + skip + '" data-i="' + i + '"><div class="row-top">'
      + '<span class="cap-time"' + (it._ttsFail
          ? ' style="color:var(--bad,#f48771)" title="⚠ 配音曾失败：' + _esc(it._ttsFail) + '（重试结果以成片为准）"'
          : '')
      + '>' + (it._ttsFail ? '⚠ ' : '') + _fmt(vs) + '</span>'
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
  // 成片模式按成片坐标定位预览；源片模式按源片坐标
  seekTo(_outMode() && it.out_start !== undefined ? it.out_start : (it.video_start || 0));
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
      + '<div class="ins-row"><label>画面范围（秒，源片坐标）</label><div class="ins-pair">'
      + '<input type="number" id="inVs" value="' + (+it.video_start).toFixed(1) + '" step="0.5">'
      + '<input type="number" id="inVe" value="' + (+(it.video_end||0)).toFixed(1) + '" step="0.5"></div></div>'
      + '<div class="ins-row"><label>段长 / 配音（不足会红，合成端自动钳回）</label>'
      + '<span class="rv" id="inLenNote"></span></div>'
      + '<button class="ins-quote-btn" id="inQuoteToggle">🎬 设为原声引用（保留原声，不配解说）</button>';
  }
  const bind = (id, fn) => { const el = $(id); if(el) el.addEventListener('input', () => fn(parseFloat(el.value))); };
  const _lenNote = () => {
    const el = $('inLenNote');
    if(!el) return;
    const len = Math.max(0, (it.video_end || 0) - (it.video_start || 0));
    const need = (it.duration || 0) + 0.3;
    el.textContent = len.toFixed(1) + 's / 配音 ' + (it.duration || 0).toFixed(1) + 's';
    el.style.color = (!it.quote && len + 0.01 < need) ? 'var(--bad, #f48771)' : 'var(--ok, #4ec9b0)';
  };
  bind('inNv', v => { it.narr_volume = v; const s = $('inNvV'); if(s) s.textContent = v + '%';
    const el = document.getElementById('narr-' + i); if(el){ try { el.volume = Math.max(0, Math.min(1, v/100)); } catch(e){} } });
  bind('inOv', v => { it.orig_volume = v; const s = $('inOvV'); if(s) s.textContent = v + '%'; });
  bind('inVs', v => {
    // 起点右移不得把段压到配音以下（主人红线：段长 ≥ 配音）
    const minLen = Math.max(0.5, (it.duration || 0) + 0.3);
    it.video_start = Math.max(0, v);
    if((it.video_end || 0) - it.video_start < minLen){
      it.video_end = Math.round((it.video_start + minLen) * 10) / 10;
      const el = $('inVe'); if(el) el.value = (+it.video_end).toFixed(1);
    }
    _relayout(); renderTimeline(); _lenNote();
  });
  bind('inVe', v => {
    const minLen = Math.max(0.5, (it.duration || 0) + 0.3);
    it.video_end = Math.max(v, it.video_start + minLen);
    const el = $('inVe'); if(el) el.value = (+it.video_end).toFixed(1);
    _relayout(); renderTimeline(); _lenNote();
  });
  bind('inText', v => { it.text = v; renderCaps(); });
  _lenNote();
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
  const total = Math.max(10, Math.ceil(_tlTotal()));
  inner.style.width = Math.max(600, total * _pxPerSec) + 'px';
  const zv = $('tlZoomVal');
  if(zv) zv.textContent = Math.round(_pxPerSec) + 'px/s';
  let ruler = '';
  const step = total > 600 ? 60 : total > 180 ? 30 : total > 60 ? 10 : 5;
  for(let t = 0; t <= total; t += step){
    ruler += '<span style="left:' + (t / total * 100) + '%">' + _fmt(t) + '</span>';
  }
  $('tlRuler').innerHTML = ruler;
  // 【v1.4.46 成片模式】色块按成片累计坐标排布（每段一块，段长=下一次合成的近似
  // 预览，拖拽后 _relayout 重排）；无 composed 时回落源片绝对时间（旧任务兼容）。
  let html = '';
  _items.forEach((it, i) => {
    let left, w;
    if(_outMode()){
      const o0 = (it.out_start !== undefined) ? it.out_start : (it.video_start || 0);
      const od = Math.max(0.1, (it.out_end !== undefined ? it.out_end : o0 + 0.1) - o0);
      left = o0 / total * 100;
      w = Math.min(od, Math.max(0.1, total - o0)) / total * 100;
    } else {
      const vs = Math.max(0, it.video_start || 0);
      const vd = Math.max(0.1, (it.video_end || 0) - vs);
      left = vs / total * 100;
      w = Math.min(vd, total - vs) / total * 100;
    }
    // 主人红线（v1.4.44）：段长必须 ≥ 配音时长——短了红框警示（合成端也会钳回）
    const short = (!it.quote && !it._skip
      && ((it.video_end || 0) - (it.video_start || 0)) < ((it.duration || 0) + 0.3 - 0.01));
    const cls = (it.quote ? ' quote' : '') + (it._skip ? ' skip' : '')
      + (short ? ' short' : '') + (i === _sel ? ' sel' : '');
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
    const total = _tlTotal();
    const srcBound = Math.max(10, _vdur || 0);   // 源片坐标钳制界（拖拽改的是 src 范围）
    // 主人红线：段长下限 = 配音时长 + 0.3s 气口（低于即被钳回，成片端同样钳）
    const minLen = Math.max(0.5, (it.duration || 0) + 0.3);
    const b = track.querySelector('.tl-block[data-i="' + drag.i + '"]');
    if(drag.mode === 'L'){
      const ns = Math.round(snap(Math.min(drag.end - minLen, Math.max(0, drag.start + dt)), drag.snap) * 10) / 10;
      it.video_start = Math.min(ns, it.video_end - minLen);
    } else if(drag.mode === 'R'){
      const ne = Math.round(snap(Math.max(drag.start + minLen, Math.min(srcBound, drag.end + dt)), drag.snap) * 10) / 10;
      it.video_end = Math.max(ne, it.video_start + minLen);
    } else {
      const vd = drag.end - drag.start;
      const ns = Math.round(Math.max(0, Math.min(snap(drag.start + dt, drag.snap), srcBound - vd)) * 10) / 10;
      it.video_start = ns;
      it.video_end = Math.round((ns + vd) * 10) / 10;
    }
    const vs = it.video_start, vd = Math.max(0.1, it.video_end - vs);
    if(_outMode()){
      _relayout();
      const o0 = it.out_start || 0, od = Math.max(0.1, (it.out_end || 0) - o0);
      b.style.left = (o0 / total * 100) + '%';
      b.style.width = (Math.min(od, Math.max(0.1, total - o0)) / total * 100) + '%';
    } else {
      b.style.left = (vs / total * 100) + '%';
      b.style.width = (Math.min(vd, total - vs) / total * 100) + '%';
    }
  });
  window.addEventListener('mouseup', () => {
    if(!drag) return;
    const it = _items[drag.i];
    if(drag.moved && it){
      _relayout();
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
  const v = $('pvVideo');
  // 【v1.4.46 成片模式】播放头是成片时间：换算回源片切点（画面/配音按同一比例分割）
  let vs, ve, tSrc, tCut;
  if(_outMode()){
    vs = (it.out_start !== undefined) ? it.out_start : (it.video_start || 0);
    ve = (it.out_end !== undefined) ? it.out_end : vs + 0.1;
    tCut = (v && isFinite(v.currentTime)) ? v.currentTime : (vs + ve) / 2;
    if(!(tCut > vs + 0.4 && tCut < ve - 0.4)){
      toast('先把播放头停在这段内要分割的位置（离两端至少 0.4s）', 'bad');
      return;
    }
    const oLen = Math.max(0.1, ve - vs), sLen = Math.max(0.1, (it.video_end || 0) - (it.video_start || 0));
    tSrc = Math.round(((it.video_start || 0) + (tCut - vs) / oLen * sLen) * 100) / 100;
  } else {
    vs = it.video_start || 0; ve = it.video_end || 0;
    tSrc = tCut = (v && isFinite(v.currentTime)) ? v.currentTime : (vs + ve) / 2;
    if(!(tCut > vs + 0.4 && tCut < ve - 0.4)){
      toast('先把播放头停在段内要分割的位置（离两端至少 0.4s）', 'bad');
      return;
    }
  }
  if(ve - vs < 1.6){ toast('这段画面太短（<1.6s），不值得分割', 'bad'); return; }
  const veSrc = it.video_end || 0;   // 分割前该段源片终点（拆给新段）
  toast('正在切割配音…');
  try {
    const r = await fetch('/api/audio_split', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({run_dir: _runDir, name: it.audio, frac: (tCut - vs) / (ve - vs)})});
    const out = await r.json();
    if(!out.ok) throw new Error(out.error || '切割失败');
    const txt = (it.text || '').trim();
    let cut = Math.round(txt.length * ((tCut - vs) / (ve - vs)));
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
    it.video_end = tSrc;
    it.audio = out.a1;
    it.duration = out.d1;
    if(t1){ it.text = t1; it.audio_text = t1; }
    it.fade_out = 0;
    let mx = -1;
    _items.forEach(x => { const n = parseInt(x && x.index); if(!isNaN(n) && n > mx) mx = n; });
    _items.splice(i + 1, 0, {index: mx + 1, text: t2, audio_text: t2, audio: out.a2, duration: out.d2,
      video_start: tSrc, video_end: veSrc, audio_offset: 0,
      orig_volume: it.orig_volume, narr_volume: it.narr_volume,
      fade_in: 0, fade_out: 0, isBroll: false, quote: false});
    _sel = i + 1;
    _relayout();
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
    const total = _tlTotal() || 0;
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
  const total = _tlTotal() || v.duration;
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
  // 【v1.4.46 成片模式】final.mp4 已混好音、烧好字幕——预览只做播放头与段指示，
  // 不再叠字幕/放配音（那是源片坐标预览的机制，成片里都烧进去了）。
  if(_outMode()){
    let seg = -1;
    for(let i = 0; i < _items.length; i++){
      const it = _items[i];
      if(it._skip) continue;
      const o0 = (it.out_start !== undefined) ? it.out_start : (it.video_start || 0);
      const o1 = (it.out_end !== undefined) ? it.out_end : o0 + 0.1;
      if(o1 > o0 && t >= o0 && t < o1){ seg = i; break; }
    }
    _curSeg = seg;
    const sb = $('stSeg');
    if(sb && seg >= 0) sb.textContent = '段 ' + (seg + 1) + '/' + _items.length;
    updatePlayhead();
    return;
  }
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
  if(!v) return;
  // 【v1.4.46 成片模式】预览直读成片 final.mp4（混音+烧字幕都在里面）；
  // 无 composed 时回落源片/proxy（旧任务兼容）。
  let want = null;
  if(_outMode() && _composed.final){
    want = '/media/' + _runDir.replace(/\\/g, '/') + '/' + _composed.final;
  } else if(_runDir && _videoFile){
    want = '/media/' + _videoFile;
  }
  if(!want) return;
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
    const bound = _outMode()
      ? (_items[i].out_start !== undefined ? _items[i].out_start : (_items[i].video_start || 0))
      : (_items[i].video_start || 0);
    if(bound > t){ pos = i; break; }
  }
  // 成片模式：新段源片窗口取「前一段源片终点」顺延（成片坐标由 _relayout 重排）
  let ns = Math.round(t * 10) / 10, ne = Math.round((t + 5) * 10) / 10;
  if(_outMode()){
    const prev = _items[pos - 1];
    ns = prev ? (prev.video_end || 0) : 0;
    ne = Math.min(Math.max(10, _vdur || 0), ns + 5);
    if(ne - ns < 0.5){ ns = Math.max(0, ne - 5); }
  }
  let maxIdx = 0;
  _items.forEach(x => { if((x.index || 0) > maxIdx) maxIdx = x.index; });
  _pushUndo();
  _items.splice(pos, 0, {index: maxIdx + 1, text: '', audio: '', audio_text: '', duration: 0,
    video_start: ns, video_end: ne,
    narr_volume: null, isBroll: false, quote: false});
  _relayout();
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
    if($('presetMask') && $('presetMask').style.display !== 'none'){
      presetModalClose(null); return;
    }
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

/* ── 新建/导出/打开文件夹 ──
   新建项目 = 清空当前项目（用户点「新建」时期望的确实是清空这个手感，不删）。
   但剧情稿是主人最贵的一份输入，此前是无条件静默清空、误按 Ctrl+N 就没了。
   现在只有「框里真有内容」时才停下来问一句；完全空白时直接清，零打扰。
   注意用 uiConfirm 而不是 window.confirm —— pywebview/WebView2 的原生对话框
   会被压到窗口后或静默失败（v1.4.1 事故），且 tests/test_preset_no_native_dialog.py
   全文禁止 confirm(/prompt(/alert(。 */
async function newProject(){
  const draft = draftPending();
  if(draft){
    const n = (draft.npPlot || '').trim().length;
    const keep = await uiConfirm('新建项目会清空当前剧情稿',
      '当前剧情稿 ' + n + ' 字、片名「' + ((draft.npName || '').trim() || '未填')
      + '」都会被清掉，且无法撤销。\n想留就先复制一下，或点「取消」继续留在本项目。',
      '清空并新建');
    if(!keep) return;                 // 用户反悔：一个字都不许动
  }
  draftClear();                       // 到这一步才真正销毁草稿
  newProjectReset();
}
function newProjectReset(){
  _pushUndo();
  _items = []; _runDir = ''; _videoFile = ''; _vdur = 0; _sel = -1; _curSeg = -1;
  _narrEls = {};
  document.querySelectorAll('audio[id^="narr-"]').forEach(a => a.remove());
  _video = null;
  _srt = null;
  $('projName').textContent = '未打开项目';
  $('tbGenerate').disabled = false;
  $('tbCompose').disabled = true;
  showEditor(false);
  switchView('media');
  $('npGo').disabled = false;
  $('npPickLabel').textContent = '从电脑选择视频';
  $('npPick').classList.remove('has');
  $('npSrtLabel').textContent = '导入字幕（SRT / ASS / VTT）——靠台词找画面，成片质量的命脉';
  setBtnIcon('tbSrt', 'i-subtitle', '字幕');
  _renderLastMedia();   // v1.4.37：新建项目后刷新复用按钮（视频/字幕保留，跨项目复用）
  const ep = $('exportPanel'); if(ep) ep.style.display = 'none';
  const vs = $('videoShell'); if(vs) vs.style.display = '';
  const pn = $('npName'); if(pn) pn.value = '';   // 片名与剧情稿一起清，否则「查剧情」还留着旧片名
  $('npPlot').value = '';
  if($('npScript')) $('npScript').checked = false;
  if(typeof updateNewProj === 'function') updateNewProj();
  setStatus('就绪');
}
function openOutFolder(){
  if(_runDir) openFolder(_runDir);
  else toast('成片在 webui_output 目录');
}
$('tbCompose').addEventListener('click', composeAll);
$('tbGenerate').addEventListener('click', () => {
  if(!_video || !$('npPlot').value.trim()){ switchView('story'); return; }
  $('npGo').click();
});

/* ── SRT 导入（桌面：原生对话框 + 本地直读） ── */
/* 【v1.4.47 人名译名提醒（主人 2026-11-12 拍板）】解说稿与字幕的人名必须是
   同一套译名：稿子写「柯蒂斯」、字幕写「科特」，同一个角色对不上号，语义匹配
   抓不住 → 画面配到别人的对话上（主人实测：稿子讲柯蒂斯与弗兰克对话，画面
   还是弗兰克和唐尼）。字幕导入的三个入口统一在导入前弹窗提醒。
   【v1.4.48 文案修正（主人实测教训）】不要建议用户「把字幕交给 AI 改」——
   网页版 LLM 重写长结构化文件必然偷懒：合并字幕条（216→64 条、单条 51 行/
   120s）、丢时间精度、甚至自创格式（[00:15] 台词式），主人两份 AI 改的字幕
   全废。正确姿势：字幕保持原样导入，让 AI 只改解说稿的人名（稿子短，安全）。
   文案内联在函数里（拖拽夹具按块抽取，别拆出去）。 */
function _confirmSrtNames(){
  return uiConfirm('导入前提醒：人名译名要对齐',
    '解说稿和字幕里的人名必须是同一套译名。\n'
    + '例如稿子写「柯蒂斯」、字幕写「科特」——同一个角色对不上号，\n'
    + '生成时画面就会配到别的对话上（画面配错的常见原因）。\n\n'
    + '建议：字幕保持原样导入（别让 AI 重排字幕——它会合并字幕条、\n'
    + '丢时间精度），让 AI 只改解说稿：把稿子里的人名改成字幕里的\n'
    + '叫法（稿子短，改起来安全），再回来导入。',
    '已对齐，继续导入');
}
let _srt = null;   // {name, text}【v1.4.50 补回：v1.4.49 重构时误删——严格模式下
                   // 给未声明变量赋值直接抛 ReferenceError，导入链全炸
                   // （「字幕导入失败：_srt is not defined」主人实测）】
/* 【v1.4.49 编码自适应】字幕站点下载的 srt 大量是 GBK/GB2312（Netflix 官方
   简体实测就是 GBK），旧实现 FileReader 固定 utf-8 → 官方中文字幕直接乱码。
   改为 arrayBuffer + 解码器链：BOM(uft-16) → utf-8 严格（fatal，坏字节即失败）
   → gbk（WebView2/Chromium 内置）→ utf-8 兜底。与后端 parse_subtitle_file
   的顺序一致。 */
function _decodeSubtitleBytes(buf){
  const bytes = new Uint8Array(buf);
  if(bytes.length >= 2 && bytes[0] === 0xFF && bytes[1] === 0xFE){
    try { return new TextDecoder('utf-16le').decode(buf); } catch(e){}
  }
  if(bytes.length >= 2 && bytes[0] === 0xFE && bytes[1] === 0xFF){
    try { return new TextDecoder('utf-16be').decode(buf); } catch(e){}
  }
  try { return new TextDecoder('utf-8', {fatal: true}).decode(buf); } catch(e){}
  try { return new TextDecoder('gbk').decode(buf); } catch(e){}
  try { return new TextDecoder('utf-8').decode(buf); } catch(e){}
  return null;
}
/* 导入体检（只提醒不阻断）：SRT 里「一条 ≥3 行文本」的大块，多半是 AI 重排
   时把多条字幕合并了——台词定位会变粗（主人实测 216 条被 DeepSeek 合并成
   64 条，画面配不准）。 */
function _warnMergedBlocks(text){
  try {
    const lines = String(text || '').split(/\r?\n/);
    const tsRe = /^\s*\d{1,2}:\d{1,2}:\d{1,2}[.,]\d{1,3}\s*-->/;
    let merged = 0, i = 0;
    while(i < lines.length){
      if(/^\s*\d+\s*$/.test(lines[i]) && i + 1 < lines.length && tsRe.test(lines[i + 1])){
        let j = i + 2, c = 0;
        while(j < lines.length && lines[j].trim()){ c++; j++; }
        if(c >= 3) merged++;
        i = j + 1;
      } else {
        i++;
      }
    }
    if(merged >= 3){
      toast('⚠ 这份字幕有 ' + merged + ' 条疑似被合并的大块，台词定位会变粗——建议用原版字幕', 'bad');
    }
  } catch(e){ /* 体检失败不阻断导入 */ }
}
function _loadSrtFile(file){
  const done = (text) => {
    _srt = {name: file.name, text: text};
    lastSrtSave();   // v1.4.37：记住上次字幕，下次一键复用
    _syncSrtUi();
    _warnMergedBlocks(text);
    toast('✅ 字幕已导入：' + file.name + '（生成时按台词找画面）', 'ok');
  };
  if(file.arrayBuffer){
    file.arrayBuffer().then(buf => {
      const text = _decodeSubtitleBytes(buf);
      if(text === null){ toast('字幕读取失败：无法识别的编码', 'bad'); return; }
      done(text);
    }).catch(() => toast('字幕读取失败', 'bad'));
    return;
  }
  const r = new FileReader();   // 老浏览器兜底（无 arrayBuffer 时维持旧行为）
  r.onload = () => done(String(r.result));
  r.onerror = () => toast('字幕读取失败', 'bad');
  r.readAsText(file, 'utf-8');
}
function _syncSrtUi(){
  setBtnIcon('tbSrt', 'i-subtitle', '字幕 ✓');
  const lbl = $('npSrtLabel');
  if(lbl) lbl.innerHTML = '✅ 已导入 <b>' + _esc((_srt && _srt.name) || '') + '</b> ——生成时按台词找画面（点此可换）';
}
async function importSrt(){
  if(!(await _confirmSrtNames())) return;   // v1.4.47：导入前人名译名提醒
  if(await _ensureBridge()){
    toast('正在打开文件选择框…（如未出现，请看任务栏）');
    window.pywebview.api.pick_srt().then(r => {
      if(!r || !r.path) return null;
      const name = r.name || r.path.split(/[\/]/).pop();
      return window.pywebview.api.read_text_file(r.path).then(t => {
        if(!t.ok) throw new Error(t.error || '读取失败');
        _srt = {name: name, text: t.text};
        lastSrtSave();   // v1.4.37：记住上次字幕
        _syncSrtUi();
        toast('✅ 字幕已导入：' + name + '（生成时按台词找画面）', 'ok');
      });
    }).catch(e => toast('字幕导入失败：' + e.message, 'bad'));
    return;
  }
  _browserFileInput('.srt,.ass,.ssa,.vtt', f => _loadSrtFile(f));
}

/* ── 稿件/字幕拖入窗口（EXE/浏览器都行，绕开原生对话框） ── */
/* 【2026-11-10 加】主人反馈「稿子拖不进去」：以前只认 ['srt','ass','ssa','vtt']，
   把写好的 .txt/.md 稿子拖进来**一点反应都没有**（连提示都没有），看起来像坏了。
   现在：
     - 字幕类 → 走原来的 _loadSrtFile（按台词找画面），仍要求在新项目页；
     - 文稿类（txt/md/markdown/text）→ 直接回填剧情稿框 npPlot；
     - .docx 明确告诉用户「请另存为 txt」——不静默吞掉。
       为什么不做 docx：python-docx 虽在本机开发环境装了，但既不在
       requirements.txt 里也不在 FrameCut.spec 的打包清单里，打包版没有它。
       为拖一个稿子去加一条新运行时依赖，不符合本仓「不乱做」的约束。 */
const _SCRIPT_EXT = ['txt', 'md', 'markdown', 'text'];
function _loadScriptIntoPlot(file){
  const r = new FileReader();
  r.onload = () => {
    const txt = String(r.result || '');
    const el = $('npPlot');
    if(!el) return;
    if(!txt.trim()){ toast('这个文件是空的：' + file.name, 'bad'); return; }
    // 已有内容时不静默覆盖——同一窗口里主人可能刚手打过一段
    const cur = (el.value || '').trim();
    const put = () => {
      el.value = txt;
      // 与 T2 草稿层联动：立刻存草稿，并刷新「已填/未填」的按钮状态
      if(typeof draftScheduleSave === 'function') draftScheduleSave();
      if(typeof updateNewProj === 'function') updateNewProj();
      toast('✅ 稿件已导入：' + file.name + '（' + txt.length + ' 字）', 'ok');
      if($('newProj') && $('newProj').style.display === 'none') switchView('story');
    };
    if(cur && cur !== txt.trim()){
      uiConfirm('导入稿件会覆盖当前的剧情稿',
        '框里已有 ' + cur.length + ' 字。导入后会被这个文件（' + txt.length + ' 字）替换，且无法撤销。',
        '覆盖').then(ok => { if(ok) put(); });
    } else {
      put();
    }
  };
  r.onerror = () => toast('稿件读取失败：' + file.name, 'bad');
  r.readAsText(file, 'utf-8');
}
document.addEventListener('dragover', e => { e.preventDefault(); });
document.addEventListener('drop', e => {
  e.preventDefault();
  const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
  if(!f) return;
  const ext = (f.name.split('.').pop() || '').toLowerCase();
  if(['srt', 'ass', 'ssa', 'vtt'].indexOf(ext) >= 0){
    if($('newProj').style.display === 'none'){ toast('先点「新建项目」回到引导页再导入字幕', 'bad'); return; }
    _confirmSrtNames().then(ok => { if(ok) _loadSrtFile(f); });   // v1.4.47：人名译名提醒
    return;
  }
  if(_SCRIPT_EXT.indexOf(ext) >= 0){ _loadScriptIntoPlot(f); return; }
  if(ext === 'docx'){
    toast('暂不支持 .docx：请在 Word 里「另存为」纯文本 (.txt) 再拖进来', 'bad');
    return;
  }
  if(['mp4', 'mov', 'mkv', 'avi', 'webm', 'flv', 'm4v', 'wmv'].indexOf(ext) >= 0){
    toast('视频请用「从电脑选择视频」按钮（或拖到视频框里）', 'bad');
    return;
  }
  toast('认不出这个文件类型：.' + ext + '（支持字幕 srt/ass/ssa/vtt、稿件 txt/md）', 'bad');
});

/* ── 配乐（桌面：本地路径直连） ── */
let _music = null;   // {name, path, isLocalPath}
async function pickLocalMusic(){
  if(!await _ensureBridge()){
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
    lastMusicSave();   // v1.4.51：记住上次配乐（仅本地路径形态）
    setBtnIcon('tbMusic', 'i-music', _music.name.slice(0, 14));
    const bg = $('npBgm'); if(bg) bg.checked = true;
    toast('配乐已选：' + _music.name);
  }).catch(e => toast('选乐失败：' + e.message, 'bad'));
}

/* ── 设置对话框 ── */
/* ── 设置页分区导航（v1.4.15）──
   原来 20+ 控件平铺一屏、没有主次也没有引导；收成 3 页，默认落在「配音」。
   环境卡的「去设置」会带 secVoice / secModelCenter 进来，必须顺手切到对应页，
   否则 scrollIntoView 会滚向一个被隐藏的元素，用户什么都看不到。 */
function showSettingsPane(name){
  document.querySelectorAll('[data-set-tab]').forEach(b =>
    b.classList.toggle('active', b.dataset.setTab === name));
  document.querySelectorAll('[data-set-pane]').forEach(p =>
    p.style.display = (p.dataset.setPane === name) ? '' : 'none');
  try{ localStorage.setItem('framecut_set_tab', name); }catch(e){}
}
(function initSettingsTabs(){
  const tabs = document.querySelectorAll('[data-set-tab]');
  if(!tabs.length) return;
  tabs.forEach(b => b.addEventListener('click', () => showSettingsPane(b.dataset.setTab)));
  let last = 'voice';
  try{ last = localStorage.getItem('framecut_set_tab') || 'voice'; }catch(e){}
  if(!document.querySelector('[data-set-pane="' + last + '"]')) last = 'voice';
  showSettingsPane(last);
})();

function openSettings(sec){
  $('settingsMask').style.display = 'flex';
  if(sec === 'secVoice') showSettingsPane('voice');
  else if(sec === 'secModelCenter') showSettingsPane('models');
  // v1.4.65：Whisper 台词模型也在「模型中心」那一栏，切过去才滚得到；
  // 否则弹窗停在「配音·音色」，用户点了「去设置」却看不到要改的那个下拉。
  else if(sec === 'secWhisper') showSettingsPane('models');
  if(sec){
    setTimeout(() => {
      const el = $(sec);
      if(el) el.scrollIntoView({block: 'start'});
    }, 60);
  }
  fetch('/api/ai/config').then(r => r.json()).then(d => {
    const c = d.config || {};
    $('setVisBase').value = (c.vision || {}).base_url || '';
    // 【2026-11-10 修数据丢失】以前只读 c.vision.model，而运行时真正生效的是 c.vlm.model。
    // 主人真机上 vision.model="" 而 vlm.model 才是实际值 → 输入框显示空白 →
    // 保存时 desktop.js 下面 `const _vm = setVisModel.value` 取到空 →
    // payload.vlm 不带 model 键 → handler.py:1176 的 `elif k in cur: del cur[k]` 把它删掉。
    // 于是「下拉里选好了看图模型，下次一点保存就没了」，反复出现、用户看不出原因。
    // 这里改成「vision 优先、回退 vlm」，输入框始终显示实际生效的模型名。
    $('setVisModel').value = (c.vision || {}).model || (c.vlm || {}).model || '';
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
  // v1.4.76 情感接线：模式/强度/句间气口随 tts_local 一起存（后端白名单已收）
  const _emoEl = $('setTtsEmotion'), _eaEl = $('setTtsEmoAlpha'), _gapEl = $('setTtsGap');
  const payload = {vision: {}, tts: {}, local: {}, tts_local: {
    engine: $('setTtsEngine').value || 'auto',
    voice: $('setTtsVoice').value || '',
    cosy_voice: $('setCosyVoice').value || '',
    rate: $('setTtsRate').value.trim() || '+0%',
    // 音色描述：后端全链都读（tts_engines.py:1099 造音色 / :1111 铸参考音），
    // 但以前 ①设置页没有输入框 ②/api/ai/config 白名单不存 → 永远等于 DEFAULT_INSTRUCT。
    // 允许空串（= 用默认音色），所以不能用 `|| ''` 之外的花样，直接送。
    instruct: (($('setTtsInstruct') || {}).value || '').trim(),
    emotion: _emoEl ? (_emoEl.value || 'auto') : 'auto',
    emotion_alpha: _eaEl ? (+_eaEl.value || 0.6) : 0.6,
    interval_silence: _gapEl ? (Math.round(+_gapEl.value) || 0) : 200
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
  // 【键名统一】运行时读的是 ai_config.vlm，设置页以前只写 vision →
  // 用户填了模型名、状态仍是「VLM 未启用」。这里两份都写，以 vlm 为准。
  const _vm = ($('setVisModel').value || '').trim();
  const _vb = (($('setVisBase').value || $('setVlmBase').value || '').trim()
               || 'http://localhost:11434').replace(/\/v1\/?$/, '');
  if(_vm || $('setVlmBase').value.trim()){
    payload.vlm = {enabled: true, mode: 'ollama', base_url: _vb};
    if(_vm) payload.vlm.model = _vm;
  }
  // 【v1.4.65 修】以前设置页压根没把 Whisper 下拉存进配置：用户选了 base、
  // 点保存，ai_config.whisper.model 仍是默认 distil-large-v3 —— 下拉框只是
  // 「下载哪个模型」的选择器。这就是「换了模型但识别结果没变」的根源。
  const _wm = (($('setWhisperModel') || {}).value || '').trim();
  if(_wm) payload.whisper = {model: _wm};
  try {
    const r = await fetch('/api/ai/config', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    const out = await r.json();
    if(!out.ok) throw new Error(out.error || '保存失败');
    $('settingsMask').style.display = 'none';
    toast('✅ 设置已保存');
    if(_wm && _wspEnOnly[_wm]){
      toast('⚠ ' + _wm + ' 只支持英文：识别中文会输出英文乱码，建议换成 ' + _wspSuggest, 'bad');
    }
  } catch(e){ toast('保存失败：' + e.message, 'bad'); }
});

/* ── 进度数字通用格式化（Whisper / 配音共用，v1.4.81-ux） ──
   主人原话：「进度也不知道，速度也不知道，就干等」。所有长任务至少给出
   % / 已下·总量 / 速度 / 预计剩余 / 已耗时，不再只给一根条或「已等 N 分钟」。 */
function _fmtDur(sec){
  sec = Math.max(0, Math.round(Number(sec) || 0));
  if(sec < 60) return sec + ' 秒';
  const m = Math.floor(sec / 60), s = sec % 60;
  if(m < 60) return m + ' 分' + (s ? ' ' + s + ' 秒' : '');
  return Math.floor(m / 60) + ' 小时 ' + (m % 60) + ' 分';
}
function _speedMB(v){
  // '2.0 MB/s' / '800 KB/s' / 数字(MB/s) → MB/s
  if(typeof v === 'number') return v > 0 ? v : 0;
  const m = /^([\d.]+)\s*(MB|KB|GB)\/s/i.exec(String(v || ''));
  if(!m) return 0;
  const n = parseFloat(m[1]) || 0, u = m[2].toUpperCase();
  return u === 'KB' ? n / 1024 : (u === 'GB' ? n * 1024 : n);
}
function _fmtMB(mb){
  const n = Number(mb) || 0;
  return n >= 1024 ? (n / 1024).toFixed(2) + ' GB' : Math.round(n) + ' MB';
}
function _progressMeta(pct, done, total, speed, extra, label){
  const bits = [(label || '进度') + ' ' + (pct || 0) + '%'];
  if(done !== undefined && done !== null && total) bits.push(_fmtMB(done) + ' / ' + _fmtMB(total));
  const sp = _speedMB(speed);
  if(speed) bits.push(typeof speed === 'number' ? (speed + ' MB/s') : String(speed));
  if(sp > 0 && total && done !== undefined && done !== null && total > done){
    bits.push('预计剩余 ' + _fmtDur((total - done) / sp));
  }
  if(extra) bits.push(extra);
  return bits.join(' · ');
}

/* ── Whisper 台词模型区：下载格 / 选择格（v1.4.81-ux） ──
   主人反馈：下载与选择挤一行、状态只写在 <option> 里、文件全不全没人告诉。
   拆成两个格子：① 状态+动作+进度 ② 选择+完整性结论+解决办法。
   校验证据落 localStorage；没有证据时只写「待校验」，绝不凭目录存在就写「完整无误」。 */
let _wspLastStatus = null;
let _wspVerify = {};
try{ _wspVerify = JSON.parse(localStorage.getItem('framecut_wsp_verify') || '{}') || {}; }catch(e){}
function _wspSaveVerify(model, ok, msg){
  _wspVerify[model] = {ok: !!ok, ts: Date.now(), msg: String(msg || '').slice(0, 200)};
  try{ localStorage.setItem('framecut_wsp_verify', JSON.stringify(_wspVerify)); }catch(e){}
}
function _wspSetBadge(id, text, cls){
  const el = $(id); if(!el) return;
  el.textContent = text; el.className = 'env-badge ' + cls;
}
function _wspDownloaded(s, m){
  const v = (s && s.valid_models && s.valid_models[m]) || {};
  return !!(v.downloaded || (s && s.available && s.available.indexOf(m) >= 0));
}
function _wspShowIssue(model, reason){
  uiNotices({notices: [{code: 'asr_model_broken', model: model,
                        reason: reason || '模型文件不完整', used: _wspSuggest}]});
}
function _wspRefreshCards(s){
  if(s) _wspLastStatus = s;
  const sel = $('setWhisperModel');
  const m = sel ? sel.value : '';
  if(!m) return;
  const st = _wspLastStatus || {};
  const dl = _wspDownloaded(st, m);
  const rec = _wspVerify[m];
  const modelEl = $('wspDlModel');
  if(modelEl) modelEl.textContent = '选中模型：' + m;
  const needResume = !!(rec && rec.ok === false && dl);
  if(st.downloading) _wspSetBadge('wspDlBadge', '下载中 ' + (st.download_pct || 0) + '%', 'wait');
  else if(needResume) _wspSetBadge('wspDlBadge', '需续传', 'bad');
  else _wspSetBadge('wspDlBadge', dl ? '已下载' : '未下载', dl ? 'ok' : 'wait');
  const btn = $('btnWhisperDl');
  if(btn) btn.innerHTML = needResume
    ? '<svg class="ic"><use href="#i-download"/></svg>继续下载（续传）'
    : (dl ? '<svg class="ic"><use href="#i-download"/></svg>校验 / 续传'
          : '<svg class="ic"><use href="#i-download"/></svg>下载选中模型');
  const hint = $('wspVerifyHint');
  if(!dl){
    _wspSetBadge('wspVerifyBadge', '未下载', 'wait');
    if(hint) hint.textContent = '这个模型还没下载：先在左边格子点下载，完成后回来校验。';
  } else if(rec && rec.ok === true){
    _wspSetBadge('wspVerifyBadge', '完整无误', 'ok');
    if(hint) hint.textContent = '已通过加载校验（' + new Date(rec.ts).toLocaleString()
      + '）：模型可正常使用。选中后记得点「保存设置」才会生效。';
  } else if(rec && rec.ok === false){
    _wspSetBadge('wspVerifyBadge', '模型有误', 'bad');
    if(hint) hint.textContent = '上次校验/下载未通过：' + (rec.msg || '文件不完整')
      + '。已下部分保留，点「继续下载（续传）」即可，不会从头重下。';
    _wspShowIssue(m, rec.msg);
  } else if(st.ready === true && st.selected === m){
    // 后端 whisper_model_ready() 对「当前生效模型」做过真实文件检查（权重+配置齐全），
    // 这算正面证据，可以直接给结论；其余模型没这个检查，不许跟着沾光。
    _wspSetBadge('wspVerifyBadge', '完整无误', 'ok');
    if(hint) hint.textContent = '后端已确认权重与配置齐全（当前生效模型）。换模型后记得选中并「保存设置」才会生效。';
    _wspSaveVerify(m, true, '后端就绪检查通过');
  } else {
    _wspSetBadge('wspVerifyBadge', '待校验', 'wait');
    if(hint) hint.textContent = '权重文件已就位，但还没做过加载校验；点右侧「✓ 校验」确认可用。';
  }
}

async function _wspStartTask(verifyOnly){
  const model = $('setWhisperModel').value;
  const stEl = $('whisperDlStatus');
  if(verifyOnly && !_wspDownloaded(_wspLastStatus, model)){
    stEl.textContent = '这个模型还没下载：先在左边格子点下载，完成后再校验。';
    _wspSetBadge('wspVerifyBadge', '未下载', 'wait');
    return;
  }
  const t0 = Date.now();
  try {
    const r = await fetch('/api/whisper/download', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({model: model})});
    const out = await r.json();
    if(!out.ok && !/开始|已存在/.test(out.message || out.error || '')) throw new Error(out.error || out.message || '失败');
    stEl.textContent = verifyOnly ? '正在加载校验模型…' : (out.message || '已开始下载');
    // 新一轮下载/校验开始后，上一次的「完整无误」就不再成立，先撤成「校验中…」
    _wspSetBadge('wspVerifyBadge', '校验中…', 'wait');
    // 点击即亮进度条，不等第一次轮询（后端状态在线程里才置位，有首帧 idle 竞态）
    _pullBar('whisperPullWrap', 'whisperPullFill', 'whisperPullMeta', true, 0);
    _wspSetBadge('wspDlBadge', '启动中 0%', 'wait');
    let ticks = 0, seenRunning = false;
    pollRun(model, {
      url: '/api/whisper/status', interval: 2000,
      isError: () => false,                       // 状态接口没有 error 语义
      // 【修】/api/whisper/status 返回扁平字段；以前误读成嵌套的 download 子对象、
      // 结果恒为空，于是进度条被藏、文案被清空、isDone 首帧即真、onDone 也不刷新下拉。
      onTick: s2 => {
        if(s2.downloading) seenRunning = true;
        const booting = ticks < 2 && !s2.downloading && s2.download_ok == null;
        _pullBar('whisperPullWrap', 'whisperPullFill', 'whisperPullMeta',
                 s2.downloading, s2.download_pct, s2.dl_mb_done, s2.dl_mb_total, s2.dl_speed);
        // 前两帧后端可能仍是 idle，别把刚点亮的进度条又藏回去（EXE 后端未热更时有竞态）
        if(booting){
          const bw = $('whisperPullWrap');
          if(bw && bw.style.display === 'none'){
            bw.style.display = 'block';
            $('whisperPullFill').style.width = '0%';
            $('whisperPullMeta').textContent = '正在启动…';
          }
        } else {
          // 数字行：% / 已下·总量 / 速度 / 预计剩余 / 已耗时——不再让人干等
          const meta = $('whisperPullMeta');
          if(meta) meta.textContent = _progressMeta(s2.download_pct, s2.dl_mb_done,
            s2.dl_mb_total, s2.dl_speed, '已耗时 ' + _fmtDur((Date.now() - t0) / 1000));
        }
        _wspSetBadge('wspDlBadge', s2.downloading ? ('下载中 ' + (s2.download_pct || 0) + '%')
          : (booting ? '启动中' : (s2.download_ok === true ? '已完成' : (s2.download_ok === false ? '失败·可续传' : '准备中'))),
          (s2.downloading || booting) ? 'wait' : (s2.download_ok === false ? 'bad' : 'ok'));
        stEl.textContent = s2.downloading ? ('下载中 ' + (s2.download_pct || 0) + '%')
          : (s2.download_ok === true ? '✅ ' + (s2.download_msg || '完成')
             : (s2.download_ok === false ? '❌ ' + (s2.download_msg || '失败') + ' —— 已下部分保留，点「继续下载（续传）」即可，不会从头重下'
                : (s2.download_msg || '准备中…')));
      },
      // 至少轮询 2 次且不能停在「还没开始」：必须见过 running、拿到终态，或等满 6 次宽限
      isDone: s2 => (++ticks >= 2 && !s2.downloading && (seenRunning || s2.download_ok === true || s2.download_ok === false || ticks >= 6)),
      onDone: async s2 => {
        _pullBar('whisperPullWrap', 'whisperPullFill', 'whisperPullMeta', false);  // 终态收起
        if(s2.download_ok === false){
          _wspSaveVerify(model, false, s2.download_msg || '下载/校验失败');
          _wspSetBadge('wspDlBadge', '失败·可续传', 'bad');
          stEl.textContent = '❌ ' + (s2.download_msg || '失败') + ' —— 已下部分保留，点「继续下载（续传）」继续，不会从头重下';
          await renderWhisperOptions();
          _wspShowIssue(model, s2.download_msg);
          return;
        }
        if(s2.download_ok !== true) return;
        _wspSaveVerify(model, true, s2.download_msg || '加载校验通过');
        stEl.textContent = '✅ ' + (s2.download_msg || '完成') + ' —— 选中后记得点「保存设置」才会生效';
        await renderWhisperOptions();
        // 刚装完的模型只认英文：立刻提示，别等用户剪完一条片才发现台词全是英文
        if(_wspEnOnly[model]){
          uiNotices({notices: [{code: 'asr_model_en_only', model: model,
                                used: _wspSuggest, reason: 'install', lang: 'auto'}]});
        }
      }
    }).promise.catch(() => { /* 下载状态拉取失败：保持上一次文案即可 */ });
  } catch(e){ stEl.textContent = '❌ ' + e.message; }
}
$('btnWhisperDl').addEventListener('click', () => _wspStartTask(false));
if($('btnWhisperVerify')) $('btnWhisperVerify').addEventListener('click', () => _wspStartTask(true));
let _wspEnOnly = {};        // 模型 → 是否英文专用（后端 whisper_is_en_only 判定，前端不自己猜）
let _wspSuggest = 'base';   // 后端推荐的多语言替代（已下载的优先）

/* 常用清单（主人反馈 2026-11-14：12 个模型挑花眼、不知道下哪个）——
   下拉只列这几个，按推荐度排序；清单外未下载的不显示，
   但后端 _WHISPER_KNOWN 全表保留：手动放置的模型照样能被识别、出现在「已下载」区。 */
const _WSP_POPULAR = [
  {k: 'small',           tag: '★推荐 · 中文可用'},
  {k: 'medium',          tag: '中文可用 · 更准更慢'},
  {k: 'large-v3',        tag: '中文可用 · 最准 · 约3GB'},
  {k: 'distil-large-v3', tag: '仅英文 · 英文片首选'},
  {k: 'base',            tag: '最快 · 效果一般'}
];
function _wspPopIdx(k){ return _WSP_POPULAR.findIndex(m => m.k === k); }

function renderWhisperOptions(){
  return fetch('/api/whisper/status').then(r => r.json()).then(s => {
    const sel = $('setWhisperModel');
    if(!sel) return;
    const valid = s.valid_models || {};
    _wspEnOnly = {};
    Object.keys(valid).forEach(k => { if(valid[k] && valid[k].en_only) _wspEnOnly[k] = 1; });
    _wspSuggest = s.suggest_multilingual || 'base';
    // 只显示：已下载的（按推荐度排前）+ 清单里未下载的前几个推荐 + 当前生效模型兜底。
    // 清单外且未下载的（tiny / distil-small / large-v2 / distil-medium / large / distil-large-v2）不再列出。
    const downloaded = Object.keys(valid).filter(k => valid[k] && valid[k].downloaded)
      .sort((a, b) => _wspPopIdx(a) - _wspPopIdx(b));
    const popUndl = _WSP_POPULAR.map(m => m.k).filter(k => valid[k] && !valid[k].downloaded);
    const order = downloaded.concat(popUndl);
    const cur = s.selected || '';
    if(cur && valid[cur] && !order.includes(cur)) order.push(cur);   // 当前生效模型不在清单也必须显示，否则用户以为配置丢了
    sel.innerHTML = order.map(k => {
      const dl = !!(valid[k] && valid[k].downloaded);
      const pop = _WSP_POPULAR.find(m => m.k === k);
      const tag = pop ? (' · ' + pop.tag) : (_wspEnOnly[k] ? ' · 仅英文' : '');
      return '<option value="' + k + '"' + (dl ? ' data-dl="1"' : '') + '>'
        + k + (dl ? ' ✓' : '（未下载）') + tag + '</option>';
    }).join('');
    // 回显「当前真正生效的模型」：旧实现无条件选 large-v3（只要它下载过），
    // 于是下拉显示的和实际在跑的模型不是一个，用户以为换了其实没换。
    if(cur && [...sel.options].some(o => o.value === cur)) sel.value = cur;
    _whisperLangHint();
    _wspRefreshCards(s);
  }).catch(() => {});
}

/* 设置页里的「只认英文」红字提醒：选到 distil-* 立刻可见，不用等到跑出英文乱码 */
function _whisperLangHint(){
  const el = $('whisperLangHint');
  const sel = $('setWhisperModel');
  if(!el || !sel) return;
  const m = sel.value || '';
  if(!_wspEnOnly[m]){ el.style.display = 'none'; el.textContent = ''; return; }
  el.style.display = '';
  el.innerHTML = '⚠ <b>' + _esc(m) + '</b> 是英文专用模型，只能识别英文——'
    + '中文/日语等会被硬转成英文乱码（例如「Thank you, you know」这类英文幻觉）。'
    + '多语种请选 <b>large-v3 / medium / small / base</b>（推荐 <b>' + _esc(_wspSuggest) + '</b>）。';
}
if($('setWhisperModel')) $('setWhisperModel').addEventListener('change', () => {
  _whisperLangHint();
  _wspRefreshCards();          // 选完立刻给「完整无误 / 模型有误 / 待校验」结论
});

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
      // 【不再假装已选】未启用时下拉首项给「（未启用）」并选中它。
      // 以前默认显示「MiniCPM-V 4.5（已装）」，看着像已经在用，实际 enabled=false。
      if(!V.enabled){
        const sel = $('setVlmPick');
        const o = document.createElement('option');
        o.value = ''; o.textContent = '（未启用 · 选一个即生效）';
        sel.insertBefore(o, sel.firstChild);
        sel.value = '';
        $('mcHint').textContent = '看图模型当前未启用；上面选一个已装模型会立即启用';
      } else if(!$('mcHint').textContent){
        $('mcHint').textContent = '当前看图模型：' + (V.model || '');
      }
      _pullBar('vlmPullWrap', 'vlmPullFill', 'vlmPullMeta', V.pulling, V.pull_pct, V.pull_mb_done, V.pull_mb_total, V.pull_speed);
    });
    const jobT = _envGet('/api/tts2/model/status').then(S => {
      if(!S) return;
      _renderTts2ModelCards(S);
    });
    await Promise.all([jobL, jobV, jobT]);
  } catch(e){ /* 静默，下次刷新 */ }
}

/* ── v1.4.78 模型中心·配音权重下载 / v1.4.79 一键安装 ──
   两卡（IndexTTS2 / Qwen3-TTS）：四态徽标 + 进度 + 取消。
   Qwen3-TTS 环境可一键安装（缺权重会先下载再装环境）；IndexTTS-2 权重可下、
   环境按 README 手动（依赖面重，不收编），按钮如实分步。 */
let _tts2DlHandle = null;
let _tts2TaskStart = 0;          // 点击/接上任务的时间，用来算「已耗时 / 预计剩余」
let _tts2Failed = null;          // {engine, op}：失败后按钮改「继续（续传）」，别让人以为要重下
function _renderTts2ModelCards(S){
  const map = {qwen3tts: ['tts2BadgeQwen', 'tts2LabelQwen', 'btnTts2DlQwen'],
               indextts2: ['tts2BadgeIdx', 'tts2LabelIdx', 'btnTts2DlIdx']};
  const running = !!S.running;
  Object.keys(map).forEach(eng => {
    const st = (S.engines || {})[eng] || {};
    const badge = $(map[eng][0]), label = $(map[eng][1]), btn = $(map[eng][2]);
    if(!badge || !label || !btn) return;
    let badgeCls, badgeText, btnText, btnOn = true, act = 'download';
    const installing = st.env_installable;
    if(running && S.engine === eng){
      badgeCls = 'wait';
      badgeText = (S.op === 'install') ? '安装中' : '下载中';
      btnText = '⏹ 取消'; act = 'cancel';
    } else if(_tts2Failed && _tts2Failed.engine === eng){
      // 失败后不要让用户以为要从头再下 5GB：直接给「继续（续传）」
      badgeCls = 'bad'; badgeText = '未完成·可续传';
      btnText = '▶ 继续（断点续传）';
      act = (_tts2Failed.op === 'install') ? 'install' : 'download';
    } else if(st.ready){
      badgeCls = 'ok'; badgeText = '已就绪'; btnText = '✅ 已就绪'; btnOn = false;
    } else if(st.weights){
      badgeCls = 'wait'; badgeText = '权重已下载';
      if(installing){
        btnText = '⚙ 安装运行环境'; act = 'install';
      } else {
        btnText = '⚙ 需手动装环境'; btnOn = false; act = 'manual';
      }
    } else {
      badgeCls = 'bad'; badgeText = '未下载';
      btnText = installing ? '⬇ 一键安装（权重+环境）' : '⬇ 下载权重';
      act = installing ? 'install' : 'download';
    }
    badge.textContent = badgeText;
    badge.className = 'env-badge ' + badgeCls;
    btn.textContent = btnText;
    btn.disabled = !btnOn || (running && S.engine !== eng);
    btn.dataset.eng = eng;
    btn.dataset.act = act;
    let suffix = '';
    if(!st.ready && st.weights){
      suffix = installing ? ' · 权重已在，点右侧「安装运行环境」即可（venv+PyTorch，约 2.6GB）'
                          : ' · 权重已在；运行环境较重，请按 README 手动安装（uv）';
    }
    label.textContent = (st.label || '') + '（' + (st.size_hint || '') + '）' + suffix;
  });
  const barWrap = $('tts2PullWrap');
  if(barWrap){
    barWrap.style.display = running ? '' : 'none';
    if(running){
      const pct = Math.min(100, S.pct || 0);
      $('tts2PullFill').style.width = pct + '%';
      // v1.4.81-ux：主人反馈「等了 3 分钟，进度/速度都不知道，就干等」。
      // 下载阶段有真实字节数就用字节算 ETA；安装阶段只有按耗时爬行的阶段进度，
      // 必须写明「按耗时估算」，不能冒充下载字节进度。
      const hasBytes = Number(S.mb_total) > 0 && Number(S.mb_done) > 0;
      const elapsed = (Date.now() - (_tts2TaskStart || Date.now())) / 1000;
      let extra = '已耗时 ' + _fmtDur(elapsed);
      if(!hasBytes && pct > 0 && pct < 100){
        extra += ' · 预计剩余 ' + _fmtDur(elapsed * (100 - pct) / pct);
      }
      $('tts2PullMeta').textContent = _progressMeta(pct,
        hasBytes ? S.mb_done : null, hasBytes ? S.mb_total : null,
        hasBytes ? (S.speed_mb_s || 0) : null, extra,
        hasBytes ? '下载进度' : '阶段进度（按耗时估算）')
        + ' · ' + (S.msg || '处理中…')
        + '（点「⏹ 取消」；已下/已装部分保留，续传不会从头重下）';
    }
  }
  // 设置页开着时发现任务在跑 → 自动接上轮询（关掉再开不丢进度）
  if(running && !_tts2DlHandle){
    if(!_tts2TaskStart) _tts2TaskStart = Date.now();
    _pollTts2Model();
  }
}
function _pollTts2Model(){
  if(_tts2DlHandle) _tts2DlHandle.stop();
  let ticks = 0, seenRunning = false;
  _tts2DlHandle = pollRun('tts2dl', {
    url: '/api/tts2/model/status', interval: 2000,
    isError: () => false,
    // 至少轮询 2 次且不能停在「还没开始」：必须见过 running、拿到终态，或等满 6 次宽限
    isDone: p => (++ticks >= 2 && !p.running && (seenRunning || p.ok === true || p.ok === false || ticks >= 6)),
    onTick: S => {
      if(S.running) seenRunning = true;
      _renderTts2ModelCards(S);
      // 前两帧别把刚点亮的进度条又藏回去
      if(ticks < 2){
        const bw = $('tts2PullWrap');
        if(bw && bw.style.display === 'none'){
          bw.style.display = '';
          $('tts2PullFill').style.width = '0%';
          $('tts2PullMeta').textContent = '正在启动…';
        }
      }
    },
    onDone: async S => {
      _tts2DlHandle = null;
      if(S.ok === true){
        _tts2Failed = null;
        // 一键安装与仅下权重的成功语义不同，提示必须分开（别让用户以为装好了环境）
        toast(S.op === 'install'
          ? '✅ 安装完成：权重与运行环境都就绪，可回配音页试听'
          : ('✅ 权重下载完成' + (S.engine === 'qwen3tts'
            ? '（点「⚙ 安装运行环境」补齐 venv + PyTorch）' : '')), 'ok');
      } else {
        // 失败要回答三件事：已下的还在吗 / 点哪里继续 / 会不会从头重下
        _tts2Failed = {engine: S.engine || '', op: S.op || 'download'};
        toast((S.msg || '任务未完成') + ' —— 已下/已装部分保留，点「▶ 继续（断点续传）」接着跑，不会从头重下', 'bad');
      }
      fillModelCenter(); fillTtsSettings();
    }
  });
}
$('secTts2Models').addEventListener('click', async ev => {
  const b = ev.target.closest('.mini-btn');
  if(!b || !b.dataset.eng) return;
  const eng = b.dataset.eng;
  const act = b.dataset.act || 'download';
  if(act === 'manual') return;              // 手动指引按钮不动作
  if(act === 'cancel'){
    const r = await fetch('/api/tts2/model/cancel', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: '{}'})
      .then(x => x.json()).catch(() => null);
    toast(r && r.ok ? '已请求取消（几秒内生效，已完成的部分保留）'
                    : ((r && (r.error || r.message)) || '取消失败'), 'bad');
    return;
  }
  const url = (act === 'install') ? '/api/tts2/env/install' : '/api/tts2/model/download';
  const r = await fetch(url, {method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({engine: eng})}).then(x => x.json()).catch(() => null);
  if(r && r.ok){
    toast(r.message || '已开始', 'ok');
    _tts2TaskStart = Date.now();
    _tts2Failed = null;
    // 点击即亮进度条，别让用户只看到一句「开始下载」却不知道有没有在跑
    const bw = $('tts2PullWrap');
    if(bw){
      bw.style.display = '';
      $('tts2PullFill').style.width = '0%';
      $('tts2PullMeta').textContent = r.message || '正在启动…';
    }
    _pollTts2Model();
    // 【修】成功时不在这里刷模型中心：它异步拉到的首帧 idle 会把刚点亮的进度条又藏回去，
    // 进度刷新由 _pollTts2Model 的 onTick 负责，不额外抢一帧。
  } else {
    toast('发起失败：' + ((r && (r.error || r.message)) || '未知'), 'bad');
    fillModelCenter();
  }
});
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
  if(!m) return;                 // 选「（未启用）」不写配置
  const V = _envData.vlm;
  const installed = ((V && V.installed) || []).some(n => (n || '').split(':')[0] === m.split(':')[0]);
  await fetch('/api/ai/config', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({vlm: {enabled: true, mode: 'ollama', base_url: 'http://localhost:11434/v1', model: m}})});
  toast('看图模型已切换为 ' + m + (installed ? '' : '，开始自动拉取…'));
  if(!installed){ $('btnPullVlm').click(); }
  else { refreshEnv(); fillModelCenter(); }
});
/* ── 硬件一键配置：按显存推荐四件套，配音引擎不抢现成的 ── */
let _hwRec = null;
$('btnHwApply').addEventListener('click', async () => {
  const b = $('btnHwApply'), h = $('hwHint');
  if(!_hwRec){
    b.disabled = true; h.textContent = '检测硬件中…';
    try {
      const hw = await fetch('/api/hardware').then(r => r.json());
      const rec = hw.recommendations || {};
      _hwRec = rec;
      const ups = (hw.upgrades || []).map(u => u.slot).join('、');
      h.innerHTML = '档位：<b>' + _esc(hw.tier || '?') + '</b>（' + _esc(String(hw.gpu || '无独显')) + ' / ' + hw.gpu_vram_gb + 'GB 显存）'
        + '<br>推荐：写稿 ' + _esc(rec.text || '') + ' · 看图 ' + _esc(rec.vlm || '') + ' · 台词 ' + _esc(rec.whisper || '')
        + ' · 离线配音 ' + _esc(rec.tts || '')
        + (hw.current && hw.current.tts_engine === 'cosyvoice' ? '<br>配音引擎保持你的 CosyVoice 不动' : '')
        + (ups ? '<br>可升级：' + _esc(ups) : '');
      b.textContent = '应用此配置';
    } catch(e){ h.textContent = '检测失败：' + e.message; }
    b.disabled = false;
    return;
  }
  // 应用
  b.disabled = true; b.textContent = '应用中…';
  try {
    const rec = _hwRec;
    const payload = {
      local: {enabled: true, base_url: 'http://localhost:11434/v1', model: rec.text},
      vlm: {enabled: !!rec.vlm, mode: 'ollama', base_url: 'http://localhost:11434', model: rec.vlm},
      whisper: {model: rec.whisper}
    };
    // 【实测踩中】配音引擎守卫必须读当下配置（页面缓存可能未就绪，实测把主人的
    // CosyVoice 覆盖成了 sherpa）。
    // 下面这个列表 = 「可以安全覆盖」的取值，不是「要排除」的取值——命中就把
    // tts_local 改成 sherpa。只有 auto/sapi/空 才算「用户没做过显式选择」，
    // 把这类配置升级到离线 sherpa 是纯赚。
    // 【2026-11-10】白名单修好后 qwen3tts / indextts2 也是「用户显式选过的自然引擎」，
    // 与 cosyvoice/chattts/edge 同类，一律不覆盖（已拍板第⑦条：硬件一键配置不得接管
    // 在用的自然配音引擎）。它们不在这里，就是被保护。
    const cfgNow = await fetch('/api/ai/config').then(x => x.json()).catch(() => null);
    const curEng = ((cfgNow && cfgNow.config && cfgNow.config.tts_local) || {}).engine || 'auto';
    const curCosy = cfgNow && cfgNow.cosyvoice_installed;
    if(['auto', 'sapi', ''].indexOf(curEng) >= 0 && rec.tts && !curCosy){
      payload.tts_local = {engine: 'sherpa', sherpa_model: rec.tts};
    }
    const r = await fetch('/api/ai/config', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(payload)}).then(x => x.json());
    if(!r.ok) throw new Error(r.error || '保存失败');
    // 未安装的自动开始拉取（进度条已有）
    const L = _envData.local;
    const localInstalled = ((L && L.installed) || []).some(n => (n || '').split(':')[0] === (rec.text || '').split(':')[0]);
    if(!localInstalled){ fetch('/api/local/pull', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({model: rec.text})}); }
    const V = _envData.vlm;
    const vlmInstalled = ((V && V.installed) || []).some(n => (n || '').split(':')[0] === (rec.vlm || '').split(':')[0]);
    if(rec.vlm && !vlmInstalled){ fetch('/api/vlm/pull', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({model: rec.vlm})}); }
    const W = _envData.wsp;
    if(W && !W.ready && W.selected !== rec.whisper){ fetch('/api/whisper/download', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({model: rec.whisper})}); }
    toast('✅ 已按本机配置（' + (rec.text || '') + ' 等），缺的模型开始自动拉取', 'ok');
    fillModelCenter(); refreshEnv();
    _hwRec = null; b.textContent = '按本机一键配置';
  } catch(e){ toast('应用失败：' + e.message, 'bad'); }
  b.disabled = false;
});

// 设置打开期间轮询拉取进度（2.5s，仅在对话框可见时）
setInterval(() => {
  if($('settingsMask').style.display === 'none' || !$('settingsMask').style.display) return;
  fillModelCenter();
}, 2500);

/* ── 配音状态栏文案（纯函数，便于 monkeypatch / 单元测试直测） ──
   【2026-11-10 修】这里以前只看 sherpa/cosyvoice（上一代引擎），完全没看 v.tts2。
   主人换成 Qwen3-TTS/IndexTTS2 之后 sherpa/cosyvoice 都是 false，于是
   明明两个新引擎都就绪，状态栏却写「⚠ 还没装本地引擎：…建议在下方装离线模型」——
   把「已就绪」说成「没装」，正好就是主人报的「选了引擎还提示没有配置」。

   顺带把语义拆开：「已选 X」是配置，「X 就绪」是能力，两者不等价。
   选了一个没就绪的引擎时要说清「会换用另一个本地引擎」，而不是笼统地说没装。 */
function _ttsStateHint(v, cfg){
  v = v || {}; cfg = cfg || {};
  const st = [];
  if(v.degrade_msg) st.push('⚠ ' + v.degrade_msg);
  if(v.edge_dead) st.push('⚠ 微软服务不可用：' + v.edge_dead);
  if(v.degrade_msg || v.edge_dead || (cfg.engine || 'auto') === 'sapi') return st;

  const t2 = v.tts2 || {};
  const t2any = !!(t2.qwen3tts || t2.indextts2);
  const legacy = !!(v.sherpa_installed || v.cosyvoice_installed);
  if(!t2any && !legacy){
    st.push('⚠ 还没装本地引擎：微软服务不稳时会退回机械音，建议在下方装离线模型');
    return st;
  }
  const eng = (cfg.engine || 'auto');
  // auto 的真实顺序是 _auto_order() = ['indextts2','qwen3tts']（克隆优先）
  const name = (eng === 'indextts2') ? 'IndexTTS2'
             : (eng === 'qwen3tts')  ? 'Qwen3-TTS'
             : (t2.indextts2 ? 'IndexTTS2' : 'Qwen3-TTS');
  const want = (eng === 'indextts2') ? t2.indextts2
             : (eng === 'qwen3tts')  ? t2.qwen3tts
             : (t2.indextts2 || t2.qwen3tts);
  if(want){
    st.push('✓ 本地配音就绪（' + name + '），联网服务只作兜底');
  } else if(t2any){
    st.push('⚠ 已选 ' + name + '，但它的引擎资产没就绪：合成时会自动换用另一个本地引擎');
  } else {
    st.push('✓ 本地引擎优先（' + name + ' 之外的老引擎），联网服务只作兜底');
  }
  return st;
}

/* ── 设置：配音引擎安装（缺哪个装哪个，装完自动可用） ── */
function renderTtsInstalls(v){
  const box = $('ttsInstalls');
  if(!box || !v) return;
  const st = v.tts2 || {};
  const rows = [
    ['Qwen3-TTS · VoiceDesign', st.qwen3tts, '纯文字描述造音色，不用录参考音，离线运行', 'qwen3tts'],
    ['IndexTTS2', st.indextts2, '8 维情感 + 时长控制，自带示例音色', 'indextts2']
  ];
  // v1.4.84 引导行：先告诉用户「现在能不能跑、用的是谁」，再说缺什么
  const _usable = st.qwen3tts || st.indextts2;
  const _lead = _usable
    ? ('✅ 现在就能配音：「自动」模式会用已装的 ' + (st.indextts2 ? 'IndexTTS2' : 'Qwen3-TTS')
       + '，另一个装不装都不影响当前使用。')
    : '两个配音模型都未装：到下方「模型中心」装一个即可（推荐 Qwen3-TTS，可一键装权重+运行环境）。';
  box.innerHTML = '<div class="set-row"><span class="env-badge '
      + (_usable ? 'ok' : 'bad') + '" style="flex:none">' + (_usable ? '可配音' : '待安装')
      + '</span><span class="hint" style="flex:1">' + _lead + '</span></div>'
    + rows.map(r =>
      '<div class="set-row"><span class="env-badge ' + (r[1] ? 'ok' : 'bad') + '">'
      + (r[1] ? '已装' : '未装') + '</span><span class="hint" style="flex:1">'
      + r[0] + ' · ' + r[2] + '</span>'
      + (r[1] ? '' : '<button type="button" class="mini-btn" data-go-tts2="'
        + r[3] + '">⬇ 去下载</button>')
      + '</div>').join('')
    + _vramRow(st)
    + '<div class="hint">引擎资产在 ' + _esc(st.dir || '本机用户数据目录')
    + '，升级成品不会丢失。显示「未装」缺的是权重或运行环境：到「模型中心」的'
    + '「配音模型权重与环境」区，Qwen3-TTS 可一键安装（权重 + 运行环境），'
    + 'IndexTTS-2 权重可下载、运行环境请按 README 手动装。</div>';
}
/* 引导闭环：voice 页「未装」徽标 → 一键跳到模型中心的配音权重区（v1.4.78） */
$('ttsInstalls').addEventListener('click', ev => {
  const b = ev.target.closest('[data-go-tts2]');
  if(!b) return;
  showSettingsPane('models');
  const sec = $('secTts2Models');
  if(sec) sec.scrollIntoView({behavior: 'smooth', block: 'start'});
});

/* 显存一行：12GB 卡上最容易「看着装好了却合成不动」的地方，
   直接把它摊开给用户看（Ollama 的 qwen3:8b 能吃掉 9.6GB）。 */
function _vramRow(st){
  const vr = (st && st.vram) || {};
  if(!vr.total) return '';
  const free = vr.free || 0, tight = free < 5200;
  const ol = (st.ollama || []);
  return '<div class="set-row"><span class="env-badge ' + (tight ? 'bad' : 'ok') + '">'
    + (tight ? '偏紧' : '充足') + '</span><span class="hint" style="flex:1">'
    + '显存 ' + vr.used + ' / ' + vr.total + ' MB（空闲 ' + free + ' MB）'
    + (ol.length ? ' · Ollama 正占用 ' + _esc(ol.join('、')) + '，点试听会自动腾出' : '')
    + '</span></div>';
}
let _setupTimer = null;
/* 死代码清理（2026-11-10）：_pollTtsSetup 一直没有调用者，而且它等的是
   `#ttsInstalls .mini-btn`——renderTtsInstalls() 从不渲染按钮（只渲染状态徽标 + 显存行）。
   更关键的是后端根本没有 qwen3tts/indextts2 的安装接口：/api/tts/install 走的是
   edge-tts，/api/tts/model/download 只有 sherpa 的 melo-zh / piper-huayan。
   换言之这里曾经想做「缺哪个装哪个」，但装的两个引擎都不在可安装清单里，
   按钮即使渲染出来也无接口可调。真正的修法需要先有后端安装器（见 AI_CHANGELOG），
   在那之前不留一个装作能装的空壳。_setupTimer 保留给以后真接了安装接口时用。 */

/* ── 设置：配音引擎与音色 ── */
// auto 的真实顺序是 _auto_order() = ['indextts2','qwen3tts']（克隆优先，整片同一把嗓子），
// 标签以前写成「Qwen3-TTS 优先」，与代码相反，用户按标签理解就会选错引擎。
// 【2026-11-10】qwen3tts 这一项原来标「VoiceDesign（文字造音色）」——修完 P1-3 后
// 它也是克隆（Qwen3 只负责按描述造那一条参考音，逐句合成由 IndexTTS2 克隆完成），
// 标签再写「文字造音色」会让人以为还是逐句现造。按实际行为改。
const TTS_ENGINES = [['auto','自动（IndexTTS2 克隆优先）'],
  ['qwen3tts','Qwen3 按文字描述造音色 → IndexTTS2 克隆（无需录音）'],
  ['indextts2','IndexTTS2（情感 / 时长可控）']];
/* ── v1.4.84 引导随实际状态走 ── 主人实测抓的引导矛盾：IndexTTS2 未装时，
   状态区写「未装」、下拉却写「自动（IndexTTS2 克隆优先）」——用户没法判断
   现在会用谁、要不要装。三个纯函数把文案变成实际就绪状态的函数。 */
function _autoEngineLabel(st){
  st = st || {};
  if (st.indextts2) return '自动（IndexTTS2 克隆优先）';
  if (st.qwen3tts) return '自动（IndexTTS2 未装 · 当前用 Qwen3-TTS 配音）';
  return '自动（配音模型未装 · 到模型中心一键安装）';
}
function _qwenEngineLabel(st){
  st = st || {};
  return st.indextts2
    ? 'Qwen3 按文字描述造音色 → IndexTTS2 克隆（无需录音）'
    : 'Qwen3-TTS · VoiceDesign（IndexTTS2 未装时由它直接配音）';
}
function _autoEngineHint(st){
  st = st || {};
  if (st.indextts2 && st.qwen3tts)
    return '保持「自动」即可：优先 IndexTTS2 克隆音色（整片同一把嗓子），Qwen3-TTS 负责造参考音、并在克隆失败时兜底。试听只合成一句，不跑全片。';
  if (st.qwen3tts)
    return '现在就能跑：IndexTTS2 未装，「自动」会直接用 Qwen3-TTS 配音；到模型中心装好 IndexTTS2 后无需任何设置，自动恢复「克隆优先」。';
  if (st.indextts2)
    return 'Qwen3-TTS 未装：「自动」会用 IndexTTS2（自带示例音色）配音。';
  return '两个配音模型都未装：到下方「模型中心」装 Qwen3-TTS（可一键装权重+运行环境）后即可配音。';
}
async function fillTtsSettings(){
  try {
    const v = await fetch('/api/tts/voices').then(r => r.json());
    const cfg = v.cfg || {};
    const st2 = v.tts2 || {};
    const _engines = [
      ['auto', _autoEngineLabel(st2)],
      ['qwen3tts', _qwenEngineLabel(st2)],
      ['indextts2', 'IndexTTS2（情感 / 时长可控）']];
    $('setTtsEngine').innerHTML = _engines.map(e =>
      '<option value="' + e[0] + '">' + _esc(e[1]) + '</option>').join('');
    $('setTtsEngine').value = cfg.engine || 'auto';
    const _engHint = $('ttsEngineHint');
    if(_engHint) _engHint.textContent = _autoEngineHint(st2);
    $('setTtsVoice').innerHTML = (v.voices || []).map(n =>
      '<option value="' + _esc(n) + '">' + _esc(n) + '</option>').join('');
    $('setTtsVoice').value = cfg.voice || 'zh-CN-XiaoxiaoNeural';
    $('setTtsRate').value = cfg.rate || '+0%';
    const _ins = $('setTtsInstruct');
    if(_ins) _ins.value = cfg.instruct || '';
    // v1.4.76 情感接线回填：模式/强度/气口 + 情绪试听按钮（词表来自后端 EMO_VECTORS）
    const _emo = $('setTtsEmotion');
    if(_emo) _emo.value = cfg.emotion || 'auto';
    const _ea = $('setTtsEmoAlpha');
    if(_ea){
      _ea.value = (cfg.emotion_alpha != null ? cfg.emotion_alpha : 0.6);
      const _eav = $('setTtsEmoAlphaVal');
      if(_eav) _eav.textContent = (+_ea.value).toFixed(2);
    }
    const _gap = $('setTtsGap');
    if(_gap) _gap.value = (cfg.interval_silence != null ? cfg.interval_silence : 200);
    renderEmoPreview(v.emo_words || []);
    const cv = await fetch('/api/tts/cosyvoice/voices', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: '{}'}).then(r => r.json()).catch(() => null);
    $('setCosyVoice').innerHTML = ((cv && cv.voices) || []).map(x =>
      '<option value="' + _esc(x.name) + '">' + _esc(x.name) + (x.custom ? '（克隆）' : '') + '</option>').join('');
    $('setCosyVoice').value = cfg.cosy_voice || '中文女';
    $('ttsTestHint').textContent = v.label || '';
    $('ttsStateHint').textContent = _ttsStateHint(v, cfg).join('   ');
    renderTtsInstalls(v);
  } catch(e){ $('ttsTestHint').textContent = '读取配音配置失败：' + e.message; }
}
$('btnTtsTest').addEventListener('click', async () => {
  const b = $('btnTtsTest'), hint = $('ttsTestHint');
  if(b.dataset.busy === '1') return;       // 连点不叠加请求
  b.dataset.busy = '1';
  b.disabled = true; b.textContent = '⏳ 合成中…';
  const ctrl = new AbortController();
  // 后端各段超时是「加载 150s/360s + 合成 240s」，前端要留得比它长，
  // 否则真因还没传回来，前端就先 abort 成一句「试听超时」。
  const kill = setTimeout(() => ctrl.abort(), 360000);
  const ticks = [
    setTimeout(() => { if(b.dataset.busy === '1') hint.textContent = '正在加载配音模型（首次约半分钟）…'; }, 8000),
    setTimeout(() => { if(b.dataset.busy === '1') hint.textContent = '仍在合成；若选的是 IndexTTS2，首次加载约 3 分钟…'; }, 45000),
    setTimeout(() => { if(b.dataset.busy === '1') hint.textContent = '还在算：显卡可能被别的程序占着（设置页「引擎状态」可看显存）…'; }, 120000)
  ];
  try {
    const r = await fetch('/api/tts/test', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({text: '这是一段配音试听，听听这个声音合不合适。'}),
      signal: ctrl.signal});
    const out = await r.json();
    if(!out.ok) throw new Error(out.error || out.message || '试听失败');
    hint.textContent = '当前引擎：' + (out.engine || '?') + ' · ' + (out.message || '');
    if(out.file) new Audio('/media/' + out.file).play().catch(() => {});
  } catch(e){
    hint.textContent = '❌ ' + (e.name === 'AbortError'
      ? '试听超时（6 分钟）。到设置页看「引擎状态」里的显存占用，关掉占显卡的程序再试'
      : e.message);
  } finally {
    clearTimeout(kill); ticks.forEach(clearTimeout);
    b.dataset.busy = ''; b.disabled = false; b.textContent = '🧪 试听';
  }
});
$('btnTtsReset').addEventListener('click', async () => {
  const b = $('btnTtsReset');
  b.disabled = true;
  try {
    const r = await fetch('/api/tts_reset').then(r => r.json());
    if(!r.ok) throw new Error(r.error || '重置失败');
    toast('✅ 配音引擎已重置，下次合成会重新选择最优引擎', 'ok');
    await fillTtsSettings();
  } catch(e){ toast('重置失败：' + e.message, 'bad'); }
  b.disabled = false;
});

/* ── v1.4.76 情绪试听：每个情绪词一个小按钮，点它听固定示例句在该情绪下的效果 ──
   词表来自 /api/tts/voices 的 emo_words（后端 EMO_VECTORS 的键，单一来源）；
   向量本体留在服务端，前端只传 emo_name。 */
const EMO_SAMPLE_TEXT = '他推开门，看到了桌上那封信。';
function renderEmoPreview(words){
  const row = $('emoPreviewRow');
  if(!row) return;
  row.innerHTML = (words || []).map(w =>
    '<button type="button" class="mini-btn emo-btn" data-emo="' + _esc(w) + '">'
    + _esc(w) + '</button>').join('');
}
$('emoPreviewRow').addEventListener('click', async ev => {
  const b = ev.target.closest('.emo-btn');
  if(!b || b.dataset.busy === '1') return;
  const emo = b.dataset.emo;
  b.dataset.busy = '1';
  b.disabled = true;
  const hint = $('emoPreviewHint');
  hint.textContent = '⏳ 正在合成「' + emo + '」试听…（首次约半分钟起，别关设置窗）';
  const ctrl = new AbortController();
  // 与 btnTtsTest 同参：后端加载+合成超时上限 360s，前端留得比它长
  const kill = setTimeout(() => ctrl.abort(), 360000);
  try {
    const r = await fetch('/api/tts/test', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({text: EMO_SAMPLE_TEXT, emo_name: emo}),
      signal: ctrl.signal});
    const out = await r.json();
    if(!out.ok) throw new Error(out.error || out.message || '试听失败');
    hint.textContent = '✅「' + emo + '」' + (out.message || '');
    if(out.file) new Audio('/media/' + out.file).play().catch(() => {});
  } catch(e){
    hint.textContent = '❌「' + emo + '」'
      + (e.name === 'AbortError' ? '试听超时（6 分钟）。可到「引擎状态」看显存占用' : e.message);
  } finally {
    clearTimeout(kill);
    b.dataset.busy = '';
    b.disabled = false;
  }
});
$('setTtsEmoAlpha').addEventListener('input', () => {
  const _eav = $('setTtsEmoAlphaVal');
  if(_eav) _eav.textContent = (+$('setTtsEmoAlpha').value).toFixed(2);
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
      // v1.4.59：直播切片记录换入口——「编辑」走的是解说编辑器必报错，
      // 换成「↻ 恢复候选」重进切片候选编辑器续调。
      const isLive = h.kind === 'live_clip';
      return '<div class="hist-row">'
        + (h.cover ? '<img class="hist-cover" src="/media/' + _esc(h.cover) + '" alt="">'
                   : '<div class="hist-cover ph">' + (isLive ? '✂' : '🎬') + '</div>')
        + '<div class="rc-name">' + _esc(title)
        + (isLive ? ' <span class="env-badge ok">✂ 直播切片</span>' : '') + miss
        + '<small>' + _esc(h.time || '') + (h.duration ? ' · ' + h.duration + 's' : '') + '</small></div>'
        + '<div class="hist-acts">'
        + (h.missing ? '' : '<button class="mini-btn" data-play="' + i + '">▶ 播放</button>'
                        + (isLive
                          ? '<button class="mini-btn" data-lc="' + i + '" title="恢复候选高光，重新调整后导出">↻ 恢复候选</button>'
                          : '<button class="mini-btn" data-edit="' + i + '" title="回编辑器改词重配">✏️ 编辑</button>')
                        + '<button class="mini-btn" data-folder="' + i + '" title="打开所在文件夹"><svg class="ic"><use href="#i-folder"/></svg></button>')
        + '<button class="mini-btn" data-del="' + i + '" title="删除这条记录"><svg class="ic"><use href="#i-trash"/></svg></button></div></div>';
    }).join('');
    $('histList').querySelectorAll('[data-play]').forEach(b => b.addEventListener('click', () => {
      const h = _histItems[+b.dataset.play];
      $('histMask').style.display = 'none';
      openExport(h.file);
    }));
    $('histList').querySelectorAll('[data-edit]').forEach(b => b.addEventListener('click', async () => {
      const h = _histItems[+b.dataset.edit];
      const runDir = (h.file || '').split('/')[0];
      $('histMask').style.display = 'none';
      try {
        toast('载入项目…');
        await loadAdjustItems(runDir);
        openEditor();
      } catch(e){ toast('该任务不支持编辑器打开：' + e.message, 'bad'); }
    }));
    // v1.4.59：直播切片记录「↻ 恢复候选」——读阶段①草稿重进候选编辑器续调
    $('histList').querySelectorAll('[data-lc]').forEach(b => b.addEventListener('click', async () => {
      const h = _histItems[+b.dataset.lc];
      const runDir = (h.file || '').split('/')[0];
      $('histMask').style.display = 'none';
      try {
        toast('恢复候选…');
        const r = await fetch('/api/live_clip_draft?run_dir=' + encodeURIComponent(runDir)).then(x => x.json());
        if(!r.ok) throw new Error(r.error || '草稿不存在');
        _lcRunDir = runDir;
        _lcVdur = r.vdur || 0;
        _lcCandidates = r.clips || [];
        _lcVideoFile = r.video_file || '';
        const sel = $('lcAspect');
        if(sel && r.aspect_default) sel.value = r.aspect_default;
        showLiveClip(true);
        renderLcEditor();
        toast('✅ 已恢复候选：' + _lcCandidates.length + ' 条（可调整后重新导出）', 'ok');
      } catch(e){ toast('恢复候选失败：' + e.message, 'bad'); }
    }));
    $('histList').querySelectorAll('[data-folder]').forEach(b => b.addEventListener('click', () => {
      const h = _histItems[+b.dataset.folder];
      openFolder(h.file);
    }));
    $('histList').querySelectorAll('[data-del]').forEach(b => b.addEventListener('click', async () => {
      const h = _histItems[+b.dataset.del];
      if(!await uiConfirm('删除这条历史记录？', '只删记录，不动磁盘上的其他文件。', '删除')) return;
      try {
        await fetch('/api/history/delete', {method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({file: h.file})});
        openHistory();
      } catch(e){ toast('删除失败：' + e.message, 'bad'); }
    }));
  }).catch(e => { $('histList').innerHTML = '<div class="ins-empty">加载失败：' + _esc(e.message) + '</div>'; });
}
$('histClose').addEventListener('click', () => $('histMask').style.display = 'none');
$('histClear').addEventListener('click', async () => {
  if(!_histItems.length){ toast('没有可清空的记录', 'bad'); return; }
  // v1.4.36：清空全部历史记录 + 删除磁盘上所有成片（正在生成的任务目录会保留，后端 _active_run_dirs 兜底）。
  if(!await uiConfirm('清空全部生成记录？', '将删除全部 ' + _histItems.length + ' 条记录，并删除磁盘上对应的成片文件。正在生成中的任务目录会保留。此操作不可恢复。', '清空')) return;
  try {
    const r = await fetch('/api/history/clear', {method: 'POST'}).then(x => x.json());
    if(!r.ok) throw new Error(r.error || '清空失败');
    toast('✅ 已清空 ' + _histItems.length + ' 条记录及对应成片', 'ok');
    openHistory();
  } catch(e){ toast('清空失败：' + e.message, 'bad'); }
});

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
  if(!await _ensureBridge()){
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
    openFolder(_lastExport);
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
    _storeAutoFill(d && d.auto_clean);
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
      const rel = b.dataset.delRel;
      const name = (rel || '').split('/').pop();
      // v1.4.36：成片目录删除会同步清理历史记录（后端 _purge_history_prefix），确认文案必须明示。
      const isFilm = /^webui_output\/[^/]+$/.test(rel || '');
      const extra = isFilm ? '该目录的成片及对应历史记录将被一并删除。' : '';
      if(!await uiConfirm('删除该项？', '目标：' + (name || rel) + (extra ? '\n' + extra : '') + '\n此操作不可恢复。', '删除')) return;
      b.disabled = true;
      try {
        const r = await fetch('/api/storage/delete', {method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({path: rel})}).then(x => x.json());
        if(!r.ok) throw new Error(r.error || '删除失败');
        if(isFilm && r.purged) toast('✅ 成片已删除，同步清理 ' + r.purged + ' 条历史记录', 'ok');
        loadStorage();
      } catch(e){ toast('删除失败：' + e.message, 'bad'); b.disabled = false; }
    }));
  }).catch(e => { $('storeList').innerHTML = '<div class="ins-empty">加载失败：' + _esc(e.message) + '</div>'; });
}
/* ── v1.4.68 自动清理策略 ─────────────────────────────────────────
   默认关闭。清理是破坏性操作，只允许清「任务残留/临时文件」；有历史记录的
   成片目录永远不进自动清理范围（删了历史页整排「文件已丢失」）。 */
function _storeAutoFill(c){
  if(!c) return;
  $('storeAuto').checked = !!c.auto_clean;
  $('storeDays').value = c.keep_days || 14;
  $('storeMax').value = c.keep_max || 60;
  _storeAutoHint(c);
}
function _storeAutoHint(c){
  const el = $('storeAutoHint');
  if(!el) return;
  el.textContent = c.auto_clean
    ? '启动时自动清理：保留最近 ' + c.keep_days + ' 天 / 最多 ' + c.keep_max
      + ' 个；只清任务残留与临时文件，成片不动'
    : '只清理「任务残留 / 临时文件」；有历史记录的成片永远不会被自动删。';
}
async function _storeAutoSave(){
  const body = {storage: {
    auto_clean: !!$('storeAuto').checked,
    keep_days: Math.max(1, Math.min(3650, parseInt($('storeDays').value, 10) || 14)),
    keep_max: Math.max(2, Math.min(5000, parseInt($('storeMax').value, 10) || 60))
  }};
  try{
    const r = await fetch('/api/ai/config', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(body)}).then(x => x.json());
    if(!r.ok) throw new Error(r.error || '保存失败');
    $('storeDays').value = body.storage.keep_days;
    $('storeMax').value = body.storage.keep_max;
    _storeAutoHint(body.storage);
    toast('✅ 清理策略已保存', 'ok');
  }catch(e){ toast('保存失败：' + e.message, 'bad'); }
}
['storeAuto', 'storeDays', 'storeMax'].forEach(id => {
  const el = $(id);
  if(el) el.addEventListener('change', _storeAutoSave);
});
$('storeAutoNow').addEventListener('click', async () => {
  const btn = $('storeAutoNow');
  btn.disabled = true;
  try{
    // 先 dry-run：把「会删哪些、能省多少」摆给用户看，再动手
    const pre = await fetch('/api/storage/autoclean', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({dry_run: true})}).then(x => x.json());
    if(!pre.ok) throw new Error(pre.error || '预检失败');
    const items = pre.removed || [];
    if(!items.length){ toast('按当前策略没有可清理的残留'); return; }
    const names = items.slice(0, 5).map(x => (x.rel || '').split('/').pop()).join('、');
    if(!await uiConfirm('清理 ' + items.length + ' 项历史残留？',
      '预计释放 ' + _fmtSize(pre.freed) + '\n包括：' + names + (items.length > 5 ? ' 等' : '')
      + '\n\n只删任务残留与临时文件，有历史记录的成片不会被删。', '开始清理')) return;
    const r = await fetch('/api/storage/autoclean', {method:'POST',
      headers:{'Content-Type':'application/json'}, body: JSON.stringify({dry_run: false})}).then(x => x.json());
    if(!r.ok) throw new Error(r.error || '清理失败');
    toast('✅ 已清理 ' + (r.removed || []).length + ' 项，释放 ' + _fmtSize(r.freed), 'ok');
    loadStorage();
  }catch(e){ toast('清理失败：' + e.message, 'bad'); }
  finally{ btn.disabled = false; }
});
$('storeClean').addEventListener('click', async () => {
  if(!await uiConfirm('清理所有「可回收」项？', '模型权重需重新下载，默认不动。', '开始清理')) return;
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
  const sc = $('sbCancel'); if(sc) sc.style.display = '';
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
    const sc = $('sbCancel'); if(sc) sc.style.display = 'none';
    setStatus('合成失败', 'bad');
    toast('合成失败：' + e.message, 'bad');
  }
}
function pollTask(runid){
  // 等一条「会产出音频」的任务；结束但没音频算失败（以前是 setInterval + 手写 resolve）
  return pollRun(runid, {
    interval: 1200,
    isDone: p => !!p.done,
    onDone: p => { if(!p.audio) throw new Error('任务结束但未返回音频'); }
  }).promise;
}
function pollCompose(runid){
  stopHandle(_pollTimer);
  _pollRunId = runid;
  const sc = $('sbCancel'); if(sc) sc.style.display = '';
  setStatus('合成中…');
  _pollTimer = pollRun(runid, {
    interval: 1200,
    onTick: p => {
      const c = $('sbCancel'); if(c) c.style.display = 'none';
    },
    onError: p => {
      setStatus('合成失败', 'bad');
      toast('合成失败：' + p.error, 'bad');
    },
    isDone: p => !!(p.done && p.file),
    onDone: async p => {
      setStatus('✅ 成片已生成', 'ok');
      // 【v1.4.46】合成后刷新调整数据：新的 composed_plan（成片坐标）与预览源，
      // 用户点「返回编辑」时看到的就是新成片的时间轴。
      try { await loadAdjustItems(_runDir); } catch(e){ /* 尽力刷新 */ }
      openExport(p.file);
    }
  }).promise.catch(() => { /* 失败已在 onError 里提示 */ });
}
function openExport(file){
  _lastExport = file;
  const pv = $('pvVideo'); if(pv) pv.pause();
  switchView('export');
  $('exPlayer').src = '/media/' + file;
  $('exPath').textContent = file;
  const srt = $('exSrt');
  if(srt){
    const runRel = file.split('/').slice(0, -1).join('/');
    srt.href = '/media/' + runRel + '/narr.srt';
    srt.style.display = (_advParams().subtitle.burn ? 'none' : 'inline-block');
  }
  const btn = $('tbCompose');
  btn.disabled = false;
}
$('exFolder').addEventListener('click', () => {
  if(_lastExport) openFolder(_lastExport);
  else toast('成片在 webui_output 目录');
});
$('exBack').addEventListener('click', () => switchView('adjust'));
$('exNew').addEventListener('click', () => newProject());

/* ── 新版本自检（候选 B，非阻塞）──
   启动 2 秒后才查一次；后端 3s 超时且断网静默，所以这里任何异常都不打扰主人。
   只提示、不自动下载、不改配置。 */
setTimeout(() => {
  fetch('/api/version_check').then(r => r.json()).then(v => {
    // 顶栏版本胶囊从 APP_VERSION 动态回填：HTML 里的静态文字只是无网兜底，
    // 版本写两处必然漂移（评测 2026-11-11：发版忘改 HTML 胶囊就会说谎）。
    const pill = document.querySelector('.menu-title');
    if(pill && v && v.current) pill.textContent = v.current;
    if(v && v.ok && v.has_update){
      toast('发现新版本 ' + v.latest + '（当前 ' + v.current + '），可在 GitHub Releases 下载', 'ok');
    }
  }).catch(() => {});
}, 2000);

/* ── 启动 ── */
setStatus(_draftRestored ? '就绪 · 已恢复上次未提交的剧情稿' : '就绪');
_setEntryActive('tbNarrate');


/* ── 直播切片（两阶段：①自动分析出候选 → ②用户删改候选/圈段 → 导出）──
   与解说链路「先生成配音 → 确认 → 再合成」同款两阶段模式：
   阶段①调 /api/live_clip 只做分析落草稿（不出片）；候选渲染成可编辑清单，
   每段可删、起止秒数可改、可手动圈段；阶段②调 /api/live_clip/export 出片。 */
let _lcVideo = null;          // {name, path} 本地直连 或 {name, upload_id} 浏览器上传
let _lcDanmakuText = '';      // v1.4.61：弹幕 XML 原文（直传 /api/live_clip，不落盘）
let _lcPollTimer = null;
let _lcPollRunId = null;      // v1.4.55：取消按钮凭它发 /api/cancel
let _lcAspectUsed = 'blur';   // v1.4.57：导出实际用的画幅（结果预览框比例跟着切）
let _lcFromEditor = false;

/* v1.4.57：切片参数记忆（与解说链路参数记忆对齐）——lcN/lcMin/lcMax/lcAsr/
   lcAspect/lcLang 每次从 DOM 现读，重开窗口全归默认又要重调一遍。 */
const LC_KEY = 'framecut_desktop_lc_v1';
function lcSaveParams(){
  try {
    localStorage.setItem(LC_KEY, JSON.stringify({
      n: $('lcN').value, min: $('lcMin').value, max: $('lcMax').value,
      asr: $('lcAsr').checked,
      aspect: $('lcAspect') ? $('lcAspect').value : 'blur',
      lang: $('lcLang') ? $('lcLang').value : 'auto',
    }));
  } catch(e){}
}
function lcRestoreParams(){
  try {
    const v = JSON.parse(localStorage.getItem(LC_KEY) || 'null');
    if(!v) return;
    const set = (id, val) => { const el = $(id); if(el && val !== undefined && val !== null) el.value = val; };
    set('lcN', v.n); set('lcMin', v.min); set('lcMax', v.max);
    set('lcAspect', v.aspect); set('lcLang', v.lang);
    if($('lcAsr') && v.asr !== undefined) $('lcAsr').checked = !!v.asr;
  } catch(e){}
}
let _lcRunDir = '';           // 阶段①草稿所在任务目录名（阶段②凭它导出）
let _lcVdur = 0;              // 源片时长（秒），编辑器边界钳制用
let _lcCandidates = [];       // 阶段①候选段（[{start,end,dur,score,peak_t,reasons}]）
let _lcVideoFile = '';        // v1.4.58：run_dir 内源片文件名（候选试看用；空=直读形态不可试看）
let _lcPreviewV = null;       // v1.4.58：试看用的悬浮 video（惰性创建）
let _lcPrevStop = null;       // v1.4.58：试看到这个时刻自动暂停

/* v1.4.58 候选试看：从段起点播到段终点自动暂停——导出前先看内容，别盲改数字 */
function lcPreviewSeg(seg){
  if(!_lcRunDir || !_lcVideoFile){ toast('该任务没有可预览的源片（本机路径直读形态不支持试看）', 'bad'); return; }
  if(!_lcPreviewV){
    _lcPreviewV = document.createElement('video');
    _lcPreviewV.style.cssText = 'position:fixed;right:16px;bottom:16px;width:min(420px,42vw);'
      + 'z-index:500;border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,.45);background:#000';
    _lcPreviewV.controls = true;
    document.body.appendChild(_lcPreviewV);
    _lcPreviewV.addEventListener('timeupdate', () => {
      if(_lcPrevStop !== null && _lcPreviewV.currentTime >= _lcPrevStop){
        _lcPreviewV.pause();
        _lcPrevStop = null;
      }
    });
  }
  const src = '/media/' + _lcRunDir.replace(/\\/g, '/') + '/' + _lcVideoFile;
  if(_lcPreviewV.dataset.src !== src){
    _lcPreviewV.dataset.src = src;
    _lcPreviewV.src = src;
    _lcPreviewV.load();
  }
  _lcPrevStop = Math.max(seg.start + 0.2, seg.end - 0.05);
  try { _lcPreviewV.currentTime = Math.max(0, seg.start + 0.05); } catch(e){}
  const pp = _lcPreviewV.play();
  if(pp && pp.catch) pp.catch(e => toast('试看失败：' + e.message, 'bad'));
}

function showLiveClip(on){
  const lc = $('liveClip'), np = $('newProj'), ed = $('editor');
  if(on) _lcFromEditor = !!(ed && ed.style.display !== 'none');
  if(lc) lc.style.display = on ? 'flex' : 'none';
  if(on){
    if(np) np.style.display = 'none';
    if(ed) ed.style.display = 'none';
    lcRestoreParams();   // v1.4.57：回填上次的切片参数
    lcSyncFocus();       // v1.4.62：画幅可能是记忆恢复的 crop，焦点行要同步显隐
  } else {
    if(_lcFromEditor && ed){ ed.style.display = 'flex'; }
    else if(np) np.style.display = 'flex';
  }
  if(!on && _lcPollTimer){ stopHandle(_lcPollTimer); _lcPollTimer = null; }
  updateLiveClip();
  _setEntryActive(on ? 'tbLiveClip' : 'tbNarrate');
}

function updateLiveClip(){
  if(!$('lcGo')) return;
  const busy = $('lcProg').style.display !== 'none';
  const ready = !!(_lcVideo || ($('lcPath').value || '').trim());
  $('lcGo').disabled = !ready || busy;
  $('lcGo').textContent = '① 分析高光（先出候选，不会直接出片）';
  $('lcHint').textContent = busy ? '分析进行中（转写+逐帧+评分），候选出来后可自由调整'
    : (ready ? '① 先自动分析出候选高光 → ② 你删改确认后才会导出成片'
             : '选择录像或填写本地路径后开始');
}

/* ── v1.4.62 crop 画幅水平焦点 ──────────────────────────────────────────
   范文「视觉中心」的轻量版：竖屏硬裁默认居中会把偏在角落的人脸截掉。
   滑条 0~100% → 提交时转成 0~1（后端 _clamp_focus 再钳一次），视频链与
   封面共用同一个值；缩略图上用 CSS 变量画一条焦点竖线做即时预览。 */
function lcFocusVal(){
  const el = $('lcFocus');
  return el ? (parseInt(el.value, 10) || 0) / 100 : 0.5;
}
function lcSyncFocus(){
  const isCrop = !!($('lcAspect') && $('lcAspect').value === 'crop');
  const row = $('lcFocusRow');
  if(row) row.style.display = isCrop ? 'flex' : 'none';
  const f = lcFocusVal();
  const lab = $('lcFocusLabel');
  if(lab) lab.textContent = Math.round(f * 100) + '%'
    + (Math.abs(f - 0.5) < 0.005 ? '（居中）' : (f < 0.5 ? '（偏左）' : '（偏右）'));
  if(document.documentElement && document.documentElement.style){
    document.documentElement.style.setProperty('--lc-focus', String(f));
    if(document.body && document.body.classList){
      document.body.classList.toggle('lc-crop-on', isCrop);
    }
  }
}
$('lcAspect').addEventListener('change', lcSyncFocus);
$('lcFocus').addEventListener('input', lcSyncFocus);

function _lcVideoObj(){
  if(_lcVideo){
    return _lcVideo.upload_id ? {name: _lcVideo.name, upload_id: _lcVideo.upload_id}
                              : {name: _lcVideo.name, path: _lcVideo.path, desktop_token: _token};
  }
  return null;
}

/* ── v1.4.61 弹幕热度信号 ─────────────────────────────────────────────────
   范文（切片助手）的核心优势：按每个高光事件的弹幕数量排序——观众用弹幕
   直接投票，比音量/画面运动更贴近「哪里好看」。我们只吸收信号，不做录播
   工具链（自动录弹幕/一键开播）。XML 文本直传后端解析，不落盘、不入库。
   桌面桥复用 pick_srt（其对话框含「所有文件 (*.*)」可选 .xml）+ read_text_file；
   浏览器走 FileReader 同款解码链（v1.4.49 的 _decodeSubtitleBytes：UTF-8
   严格 → GBK → 兜底），避免 GBK 弹幕在读取那一步就乱码。 */
function _lcDanmakuUi(){
  const lbl = $('lcDanmakuLabel'), clr = $('lcDanmakuClear');
  if(_lcDanmakuText){
    const kb = _lcDanmakuText.length / 1024;
    const size = kb >= 1 ? Math.round(kb) + ' KB' : '<1 KB';   // 短文件别显示「0 KB」
    lbl.textContent = '✅ 已载入弹幕 XML（' + size
      + '）——高光排序将跟随观众热度';
    clr.style.display = '';
  } else {
    lbl.textContent = 'B 站直播弹幕 XML 让高光排序跟随观众热度';
    clr.style.display = 'none';
  }
}
function _lcDanmakuLoaded(text, name){
  if(!text || !String(text).trim()){ toast('弹幕 XML 是空的（未载入）', 'bad'); return; }
  _lcDanmakuText = String(text);
  _lcDanmakuUi();
  toast('✅ 弹幕已载入：' + name + '（分析时按弹幕热度排序）', 'ok');
}
$('lcDanmakuClear').addEventListener('click', () => {
  _lcDanmakuText = '';
  _lcDanmakuUi();
  toast('已清除弹幕（高光排序回到音量+画面+台词）');
});
$('lcDanmaku').addEventListener('click', async () => {
  if(await _ensureBridge()){
    window.pywebview.api.pick_srt().then(r => {
      if(!r || !r.path) return null;
      return window.pywebview.api.read_text_file(r.path).then(t => {
        if(!t.ok) throw new Error(t.error || '读取失败');
        return {text: t.text, name: r.name || r.path.split(/[\\/]/).pop()};
      });
    }).then(r => {
      if(r) _lcDanmakuLoaded(r.text, r.name);
    }).catch(e => toast('弹幕 XML 读取失败：' + e.message, 'bad'));
  } else {
    _browserFileInput('.xml,.txt', async f => {
      try {
        const text = _decodeSubtitleBytes(await f.arrayBuffer());
        if(!text) throw new Error('解码失败');
        _lcDanmakuLoaded(text, f.name);
      } catch(e){ toast('弹幕 XML 读取失败：' + e.message, 'bad'); }
    });
  }
});

$('tbLiveClip').addEventListener('click', () => showLiveClip(true));
$('lcBack').addEventListener('click', () => showLiveClip(false));
$('lcPath').addEventListener('input', updateLiveClip);

$('lcPick').addEventListener('click', async () => {
  if(await _ensureBridge()){
    try {
      const r = await window.pywebview.api.pick_video();
      if(r && r.path){
        _lcVideo = {name: r.name || r.path.split(/[\\/]/).pop(), path: r.path};
        $('lcPickLabel').textContent = '✅ ' + _lcVideo.name;
        updateLiveClip();
      }
    } catch(e){ toast('选择失败：' + e.message, 'bad'); }
  } else {
    _browserFileInput('video/*', async f => {
      try {
        $('lcPickLabel').textContent = '⬆ 上传中 0%…';
        const uid = await _chunkUpload(f, done => {
          $('lcPickLabel').textContent = '⬆ 上传中 ' + Math.round(done * 100 / f.size) + '%';
        });
        _lcVideo = {name: f.name, upload_id: uid};
        $('lcPickLabel').textContent = '✅ ' + f.name;
        updateLiveClip();
        toast('✅ 录像已上传', 'ok');
      } catch(e){
        $('lcPickLabel').textContent = '选择直播录像（OBS 录制 / 平台回放）';
        toast('上传失败：' + e.message, 'bad');
      }
    });
  }
});

/* 阶段①：分析（不出片，落草稿） */
$('lcGo').addEventListener('click', async () => {
  lcSaveParams();   // v1.4.57：参数随提交记忆
  const vo = _lcVideoObj();
  const body = {
    n_clips: parseInt($('lcN').value, 10) || 5,
    min_sec: parseInt($('lcMin').value, 10) || 20,
    max_sec: parseInt($('lcMax').value, 10) || 60,
    use_asr: $('lcAsr').checked,
    aspect: $('lcAspect') ? $('lcAspect').value : 'blur',
    asr_language: $('lcLang') ? $('lcLang').value : 'auto',
  };
  if(_lcDanmakuText) body.danmaku_text = _lcDanmakuText;   // v1.4.61 弹幕热度信号
  if(vo){ body.video = vo; }
  else {
    const p = ($('lcPath').value || '').trim();
    if(!p){ toast('先选择录像或填写本地路径', 'bad'); return; }
    body.src_path = p;
    body.desktop_token = _token || '';
  }
  $('lcProg').style.display = 'block';
  $('lcFill').style.width = '0%';
  $('lcMeta').textContent = '提交中…';
  $('lcResults').style.display = 'none';
  $('lcEditor').style.display = 'none';
  if($('lcCancel')) $('lcCancel').style.display = '';   // v1.4.55：分析可取消
  updateLiveClip();
  try {
    const r = await fetch('/api/live_clip', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}).then(x => x.json());
    if(!r.ok) throw new Error(r.error || '提交失败');
    _lcPollRunId = r.runid;
    stopHandle(_lcPollTimer);
    _lcPollTimer = pollRun(r.runid, {
      interval: 1500,
      onTick: p => {
        $('lcFill').style.width = Math.min(100, p.pct || 0) + '%';
        $('lcMeta').textContent = (p.phase || '处理中') + ' · ' + (p.pct || 0) + '%';
      },
      onError: p => {
        _lcPollTimer = null; _lcPollRunId = null;
        $('lcProg').style.display = 'none';
        if($('lcCancel')) $('lcCancel').style.display = 'none';
        updateLiveClip();
        // v1.4.55：用户主动取消不算失败，轻提示即可
        if(/取消/.test(p.error || '')) toast('已取消分析');
        else toast('分析失败：' + p.error, 'bad');
      },
      isDone: p => !!(p.done && p.draft_saved),
      onDone: p => {
        _lcPollTimer = null; _lcPollRunId = null;
        $('lcProg').style.display = 'none';
        if($('lcCancel')) $('lcCancel').style.display = 'none';
        _lcRunDir = p.run_dir || '';
        _lcVdur = p.vdur || 0;
        _lcCandidates = p.clips || [];
        _lcVideoFile = p.video_file || '';   // v1.4.58：候选试看用的源片名
        updateLiveClip();
        renderLcEditor();
        toast('✅ 候选高光已出：' + _lcCandidates.length + ' 条，可调整后导出', 'ok');
      }
    }).promise.catch(() => { /* 已在 onError 提示 */ });
  } catch(e){
    $('lcProg').style.display = 'none';
    if($('lcCancel')) $('lcCancel').style.display = 'none';
    updateLiveClip();
    toast('分析失败：' + e.message, 'bad');
  }
});

/* v1.4.55：取消分析——后端 analyze_highlights 已接 RUN_PROCS 协作取消
   （ffmpeg/帧分析/ASR 都会在下一个检查点抛 AbortError），这里只管发信号+收尾 */
$('lcCancel').addEventListener('click', async () => {
  if(!_lcPollRunId) return;
  try {
    await fetch('/api/cancel', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({runid: _lcPollRunId})});
  } catch(e){ /* 尽力取消 */ }
  stopHandle(_lcPollTimer); _lcPollTimer = null; _lcPollRunId = null;
  $('lcProg').style.display = 'none';
  if($('lcCancel')) $('lcCancel').style.display = 'none';
  updateLiveClip();
  toast('正在取消分析…（当前步骤收尾后即停）');
});

/* ── 候选编辑器 ── */
function renderLcEditor(){
  const box = $('lcEditor');
  box.style.display = 'flex';
  box.innerHTML = '';
  const head = document.createElement('div');
  head.className = 'lc-results-head';
  head.textContent = '候选高光 ' + _lcCandidates.length + ' 条 —— 可删除 / 改起止秒数 / 手动圈段，确认后导出';
  box.appendChild(head);
  _lcCandidates.forEach((c, i) => box.appendChild(_lcRow(c, i)));
  const tip = document.createElement('div');
  tip.className = 'lc-seg-why';
  tip.textContent = '缩略图 = 每段峰值时刻的画面；确认导出后还会按峰值重新挑封面';
  box.appendChild(tip);
  const acts = document.createElement('div');
  acts.className = 'lc-foot';
  const bAdd = document.createElement('button');
  bAdd.className = 'mini-btn';
  bAdd.textContent = '＋ 手动圈一段';
  bAdd.addEventListener('click', () => {
    _lcCandidates.push({start: 0, end: Math.min(30, Math.max(5, _lcVdur)), dur: 0,
                        score: '', reasons: ['手动指定'], peak_t: 0});
    renderLcEditor();
  });
  const bRe = document.createElement('button');
  bRe.className = 'mini-btn';
  bRe.textContent = '↻ 重新分析';
  bRe.addEventListener('click', () => { $('lcEditor').style.display = 'none'; $('lcGo').click(); });
  const bGo = document.createElement('button');
  bGo.className = 'btn primary';
  bGo.textContent = '✂ 导出切片（' + _lcCandidates.length + ' 条）';
  bGo.addEventListener('click', lcExport);
  // v1.4.64：带着本任务进精剪时间轴（复用已转写台词，不重跑 Whisper）
  const bPrecise = document.createElement('button');
  bPrecise.className = 'mini-btn lcp-entry-btn';
  bPrecise.textContent = '🎬 进精剪时间轴';
  bPrecise.title = '不导出独立切片，改在时间轴上自己剪/删/延长/排序/调音量';
  bPrecise.addEventListener('click', () => lcpAnalyze({reuseRun: _lcRunDir || undefined}));
  acts.appendChild(bAdd);
  acts.appendChild(bRe);
  acts.appendChild(bPrecise);
  acts.appendChild(bGo);
  box.appendChild(acts);
}

function _lcFmtT(v){
  const t = Math.max(0, Number(v) || 0);
  return String(Math.floor(t / 60)).padStart(2, '0') + ':'
    + String(Math.floor(t % 60)).padStart(2, '0');
}

function _lcRow(c, i){
  const row = document.createElement('div');
  row.className = 'lc-seg';
  const n = document.createElement('span');
  n.className = 'lc-seg-n';
  n.textContent = '#' + (i + 1);
  const mkInput = (cls, val) => {
    const inp = document.createElement('input');
    inp.type = 'number';
    inp.className = cls;
    inp.step = '0.1';
    inp.min = '0';
    inp.max = String(Math.max(1, _lcVdur));
    inp.value = Number(val).toFixed(1);
    return inp;
  };
  const lS = document.createElement('label');
  lS.append('起(秒)', mkInput('lc-s', c.start));
  const lE = document.createElement('label');
  lE.append('止(秒)', mkInput('lc-e', c.end));
  const dur = document.createElement('span');
  dur.className = 'lc-seg-dur';
  const why = document.createElement('div');
  why.className = 'lc-seg-why';
  const refreshDur = () => {
    const a = parseFloat(row.querySelector('.lc-s').value) || 0;
    const b = parseFloat(row.querySelector('.lc-e').value) || 0;
    const d = Math.max(0, b - a);
    dur.textContent = '时长 ' + d.toFixed(1) + ' 秒' + (d < 3 ? '（≥3 秒才能导出）' : '');
    dur.style.color = d < 3 ? 'var(--bad)' : 'var(--muted)';
    c.start = Math.min(a, Math.max(0, _lcVdur));
    c.end = Math.min(b, Math.max(0, _lcVdur));
    why.textContent = (c.reasons && c.reasons.length ? c.reasons.join('；') : '手动指定')
      + (c.score !== '' && c.score !== undefined ? '  ·  分 ' + c.score : '');
  };
  lS.querySelector('input').addEventListener('input', refreshDur);
  lE.querySelector('input').addEventListener('input', refreshDur);
  const bDel = document.createElement('button');
  bDel.className = 'mini-btn lc-del';
  bDel.textContent = '删除';
  bDel.addEventListener('click', () => {
    const k = _lcCandidates.indexOf(c);
    if(k >= 0) _lcCandidates.splice(k, 1);
    renderLcEditor();
  });
  const bPrev = document.createElement('button');   // v1.4.58 候选试看
  bPrev.className = 'mini-btn';
  bPrev.textContent = '▶ 试看';
  bPrev.addEventListener('click', () => lcPreviewSeg(c));
  if (c.thumb) {
    // v1.4.62：缩略图上叠一条焦点竖线（纯 CSS 定位，位置由 --lc-focus 统一驱动）
    const wrap = document.createElement('div');
    wrap.className = 'lc-thumb-wrap';
    const img = document.createElement('img');
    img.className = 'lc-thumb';
    img.src = '/media/' + c.thumb;
    img.alt = '峰值画面';
    const mark = document.createElement('div');
    mark.className = 'lc-focus-mark';
    wrap.append(img, mark);
    row.appendChild(wrap);
  }
  row.append(n, lS, lE, dur, bPrev, bDel, why);
  /* v1.4.61 候选台词明细：范文「事件下方字幕列表可翻看」——展开看这段在
     说什么，不选中可收起。lines 随 draft 持久化，「恢复候选」后照样能翻。 */
  const bLines = document.createElement('button');
  bLines.className = 'mini-btn lc-lines-btn';
  bLines.textContent = '💬 台词';
  const linesBox = document.createElement('div');
  linesBox.className = 'lc-lines';
  linesBox.style.display = 'none';
  const _lines = Array.isArray(c.lines) ? c.lines : [];
  if(_lines.length){
    _lines.forEach(s => {
      const it = document.createElement('div');
      it.className = 'lc-line';
      it.textContent = _lcFmtT(s.start) + '  ' + String(s.text || '');
      linesBox.appendChild(it);
    });
  } else {
    const empty = document.createElement('div');
    empty.className = 'lc-line';
    empty.textContent = '（这段没有台词记录：可能未启用转写，或录像为纯音乐/静默）';
    linesBox.appendChild(empty);
  }
  bLines.addEventListener('click', () => {
    const open = linesBox.style.display !== 'none';
    linesBox.style.display = open ? 'none' : 'block';
    bLines.textContent = open ? '💬 台词' : '💬 收起台词';
  });
  row.append(bLines, linesBox);
  refreshDur();
  return row;
}

function _lcCollectSegments(){
  const rows = Array.from($('lcEditor').querySelectorAll('.lc-seg'));
  const segs = [];
  rows.forEach(r => {
    const a = parseFloat(r.querySelector('.lc-s').value);
    const b = parseFloat(r.querySelector('.lc-e').value);
    if(isFinite(a) && isFinite(b) && b - a >= 3) segs.push({start: a, end: b});
  });
  return segs;
}

/* 阶段②：按调整后的清单导出 */
async function lcExport(){
  const segs = _lcCollectSegments();
  if(!segs.length){ toast('没有可导出的段落：每段至少 3 秒', 'bad'); return; }
  if(!_lcRunDir){ toast('草稿丢失，请重新分析', 'bad'); return; }
  const body = {run_dir: _lcRunDir, segments: segs,
                aspect: $('lcAspect') ? $('lcAspect').value : 'blur',
                focus: lcFocusVal()};   // v1.4.62：crop 水平焦点随导出提交
  _lcAspectUsed = body.aspect;   // v1.4.57：结果预览框比例跟着实际画幅切
  $('lcProg').style.display = 'block';
  $('lcFill').style.width = '0%';
  $('lcMeta').textContent = '导出中…';
  $('lcEditor').style.display = 'none';
  $('lcResults').style.display = 'none';
  updateLiveClip();
  try {
    const r = await fetch('/api/live_clip/export', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}).then(x => x.json());
    if(!r.ok) throw new Error(r.error || '提交失败');
    stopHandle(_lcPollTimer);
    _lcPollTimer = pollRun(r.runid, {
      interval: 1500,
      onTick: p => {
        $('lcFill').style.width = Math.min(100, p.pct || 0) + '%';
        $('lcMeta').textContent = (p.phase || '导出中') + ' · ' + (p.pct || 0) + '%';
      },
      onError: p => {
        _lcPollTimer = null;
        $('lcProg').style.display = 'none';
        renderLcEditor();
        toast('导出失败：' + p.error, 'bad');
      },
      isDone: p => !!(p.done && p.clips && p.clips.length),
      onDone: p => {
        _lcPollTimer = null;
        $('lcProg').style.display = 'none';
        renderLiveClips(p);
      }
    }).promise.catch(() => { /* 已在 onError 提示 */ });
  } catch(e){
    $('lcProg').style.display = 'none';
    renderLcEditor();
    toast('导出失败：' + e.message, 'bad');
  }
}

function renderLiveClips(p){
  const box = $('lcResults');
  box.style.display = 'flex';
  box.innerHTML = '';
  const head = document.createElement('div');
  head.className = 'lc-results-head';
  head.textContent = '✅ 导出 ' + p.clips.length + ' 条（点开预览，原声保留）';
  box.appendChild(head);
  p.clips.forEach((c, i) => {
    const item = document.createElement('div');
    item.className = 'lc-item';
    const v = document.createElement('video');
    v.className = 'lc-video';
    v.controls = true;
    v.preload = 'metadata';
    // v1.4.57：预览框比例跟着实际输出画幅（旧实现写死 16/9，竖屏预览大黑边）
    v.style.aspectRatio = (_lcAspectUsed === 'blur' || _lcAspectUsed === 'crop') ? '9 / 16' : '16 / 9';
    v.src = '/media/' + c.rel;                       // rel 是 ASCII 路径（/media/ 不做 URL 解码）
    if (c.cover) v.poster = '/media/' + c.cover;     // 自动封面：高光峰值附近挑的最佳帧
    const meta = document.createElement('div');
    meta.className = 'lc-item-meta';
    meta.innerHTML = '<b>#' + (i + 1) + '</b> 第' + Math.round(c.start) + '秒 ~ 第' + Math.round(c.end)
      + '秒（' + Math.round(c.dur) + '秒）'
      + (c.cover ? '<div class="lc-cover-tag">🖼 已生成封面</div>' : '')
      + '<div class="lc-why">' + (c.reasons || []).map(x => '· ' + x).join('<br>') + '</div>';
    if(c.cover){
      // v1.4.60 换封面：指定源片时刻重抽一帧（默认段中点），按导出画幅裁切
      const cRow = document.createElement('div');
      cRow.className = 'lc-why';
      const inp = document.createElement('input');
      inp.type = 'number';
      inp.step = '0.5';
      inp.min = '0';
      inp.value = Number(c.start + Math.max(1, c.dur) / 2).toFixed(1);
      inp.title = '封面取自源片的这个时刻（秒）';
      inp.style.width = '92px';
      const bC = document.createElement('button');
      bC.className = 'mini-btn';
      bC.textContent = '🖼 换封面（此时刻）';
      bC.addEventListener('click', async () => {
        try {
          bC.disabled = true;
          const rr = await fetch('/api/live_clip_cover', {method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({run_dir: _lcRunDir, cover: c.cover,
                                  t: parseFloat(inp.value) || 0, aspect: _lcAspectUsed,
                                  focus: lcFocusVal()})}).then(x => x.json());
          if(!rr.ok) throw new Error(rr.error || '换封面失败');
          v.poster = '/media/' + rr.cover + '?t=' + Date.now();   // 时间戳防缓存
          toast('✅ 封面已更换', 'ok');
        } catch(e){ toast('换封面失败：' + e.message, 'bad'); }
        finally { bC.disabled = false; }
      });
      cRow.append(inp, bC);
      meta.appendChild(cRow);
    }
    item.appendChild(v);
    item.appendChild(meta);
    box.appendChild(item);
  });
  const foot = document.createElement('div');
  foot.className = 'lc-foot';
  const bBack = document.createElement('button');
  bBack.className = 'mini-btn';
  bBack.textContent = '← 回到候选调整';
  bBack.addEventListener('click', () => { box.style.display = 'none'; renderLcEditor(); });
  const bFolder = document.createElement('button');
  bFolder.className = 'mini-btn';
  bFolder.textContent = '📁 打开切片文件夹';
  bFolder.addEventListener('click', () => {
    fetch('/api/reveal', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({file: p.clips[0].rel})});
  });
  const bReport = document.createElement('button');
  bReport.className = 'mini-btn';
  bReport.textContent = '📄 查看报告';
  bReport.addEventListener('click', async () => {
    try {
      const t = await fetch('/media/' + p.report_txt).then(x => x.text());
      const pre = box.querySelector('.lc-report') || document.createElement('div');
      pre.className = 'lc-report';
      pre.textContent = t;
      if(!box.contains(pre)) box.appendChild(pre);
    } catch(e){ toast('报告读取失败：' + e.message, 'bad'); }
  });
  foot.appendChild(bBack);
  foot.appendChild(bFolder);
  foot.appendChild(bReport);
  box.appendChild(foot);
  toast('✅ 导出完成，共 ' + p.clips.length + ' 条', 'ok');
}

/* ── 顶栏三入口（解说 / 直播切片 / 环境检查）：视图切换 + 激活态 ──
   解说是主线：默认落在解说页；直播切片/环境检查是并列入口。
   环境卡平时收起（就绪时不占首屏），按钮可开/收。 */
function _setEntryActive(id){
  ['tbNarrate', 'tbLiveClip', 'tbEnvCheck'].forEach(b => {
    const el = $(b);
    if(el) el.classList.toggle('active', b === id);
  });
}

function showNewProj(){
  const lc = $('liveClip'), np = $('newProj');
  if(lc) lc.style.display = 'none';
  if(np) np.style.display = 'flex';
  if(_lcPollTimer){ stopHandle(_lcPollTimer); _lcPollTimer = null; }
  updateLiveClip();
  _setEntryActive('tbNarrate');
}

$('tbNarrate').addEventListener('click', () => {
  // 已在解说工作台（编辑器）就不动；否则回到解说页
  if($('editor').style.display !== 'none') return;
  showNewProj();
});

$('tbEnvCheck').addEventListener('click', () => {
  const card = $('envCard');
  const opening = card.style.display === 'none';
  if(opening){
    _envOpen = true;
    if($('editor').style.display !== 'none') showEditor(false);
    showNewProj();
    card.style.display = '';
    refreshEnv();
    _setEntryActive('tbEnvCheck');
  } else {
    _envOpen = false;
    card.style.display = 'none';
    _setEntryActive('tbNarrate');
  }
});

$('envHide').addEventListener('click', () => {
  _envOpen = false;
  $('envCard').style.display = 'none';
  _setEntryActive('tbNarrate');
});

/* ══════════════════════════════════════════════════════════════════
   直播精剪工作台 v1.4.64
   ──────────────────────────────────────────────────────────────────
   产品语义（与「快速出片」分开）：
     · 快速出片 = 自动挑 5 条 20~60 秒，直接给成片；
     · 精剪 = 只在全片每个高能点打注释（评分+台词），用户自己在时间轴上
       剪切 / 删除 / 前后延长 / 拖拽改顺序 / 调每段音量，最后拼成一条成片。
   时间轴的**先后就是成片顺序**（不是源片时间），所以拖拽换位=改叙事顺序。
   所有编辑状态按 run_dir 存 localStorage，关掉应用再回来还能续剪。
   ══════════════════════════════════════════════════════════════════ */
let _lcpRunDir = '', _lcpVdur = 0, _lcpWin = 2, _lcpVideoFile = '', _lcpSrcName = '';
let _lcpPoints = [], _lcpCurve = [], _lcpAsr = [];
let _lcpSegs = [], _lcpSel = -1;
let _lcpPx = 8, _lcpSort = 'time', _lcpQ = '';
let _lcpVideo = null, _lcpStopAt = null, _lcpVideoBound = false;
let _lcpUndoStk = [], _lcpTitles = {};
let _lcpPoll = null, _lcpBusy = false, _lcpSaveT = null;
let _lcpAspectUsed = 'blur';
let _lcpRaf = null, _lcpDrag = null, _lcpLastReq = {};
const LCP_UNDO_MAX = 40;

function _lcpFmt(t){
  t = Math.max(0, Number(t) || 0);
  const h = Math.floor(t / 3600), m = Math.floor(t % 3600 / 60), s = Math.floor(t % 60);
  return (h ? String(h).padStart(2, '0') + ':' : '') + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}
function _lcpDur(t){
  const v = Math.max(0, Number(t) || 0);
  return v < 60 ? v.toFixed(1) + 's'
    : Math.floor(v / 60) + 'm' + String(Math.round(v % 60)).padStart(2, '0') + 's';
}
function _lcpClamp(v, a, b){ return Math.min(b, Math.max(a, v)); }
function _lcpOverlap(s, pt){
  return Math.min(s.end, pt.end) - Math.max(s.start, pt.start);
}
function _lcpKey(){ return 'framecut_lcp_v1_' + _lcpRunDir; }

/* ── 存档 / 撤销 ───────────────────────────────────────────────── */
function lcpSaveNow(){
  try{
    localStorage.setItem(_lcpKey(), JSON.stringify({
      vdur: _lcpVdur,
      aspect: $('lcpAspect') ? $('lcpAspect').value : 'blur',
      px: _lcpPx, sel: _lcpSel, titles: _lcpTitles,
      segs: _lcpSegs.map(s => ({start: +(+s.start).toFixed(2), end: +(+s.end).toFixed(2),
                                volume: +(+s.volume).toFixed(3)})),
    }));
  }catch(e){}
}
function lcpSave(){
  if(_lcpSaveT) clearTimeout(_lcpSaveT);
  _lcpSaveT = setTimeout(lcpSaveNow, 400);
}
function lcpSyncUndoBtn(){
  const b = $('lcpUndo');
  if(b) b.disabled = !_lcpUndoStk.length;
}
function lcpUndoPushSnap(snap){
  _lcpUndoStk.push(snap || JSON.stringify(_lcpSegs));
  if(_lcpUndoStk.length > LCP_UNDO_MAX) _lcpUndoStk.shift();
  lcpSyncUndoBtn();
}
function lcpUndoPush(){ lcpUndoPushSnap(JSON.stringify(_lcpSegs)); }
function lcpUndo(){
  if(!_lcpUndoStk.length) return;
  const snap = _lcpUndoStk.pop();
  try{ _lcpSegs = JSON.parse(snap); }catch(e){ return; }
  if(_lcpSel >= _lcpSegs.length) _lcpSel = _lcpSegs.length - 1;
  lcpRenderAll(); lcpSave(); lcpSyncUndoBtn();
}

/* ── 视图切换 ──────────────────────────────────────────────────── */
function lcpOpen(){
  const me = $('lcPrecise'), lc = $('liveClip'), np = $('newProj'), ed = $('editor');
  if(me) me.style.display = 'flex';
  // 工作台底部整条是时间轴：降级提示坞默认会压在上面（实拍截图里挡住了前几段），
  // 打开工作台时把它抬到时间轴之上，并先收起来，需要时再展开。
  document.body.classList.add('lcp-on');
  _warnDockOpen = false; _warnDockRender();
  if(lc) lc.style.display = 'none';
  if(np) np.style.display = 'none';
  if(ed) ed.style.display = 'none';
  _lcpBindVideo();
}
function lcpClose(){
  const me = $('lcPrecise');
  if(me) me.style.display = 'none';
  document.body.classList.remove('lcp-on');
  if(_lcpVideo){ try{ _lcpVideo.pause(); }catch(e){} }
  // v1.4.69：关掉工作台就把轮询停掉，否则后台还在拉进度、跑完还往隐藏的
  // overlay 上写结果（以前靠「反正看不见」蒙过去，白烧请求）
  stopHandle(_lcpPoll); _lcpPoll = null; _lcpBusy = false;
  showLiveClip(true);
}

/* ── 分析（阶段①）─────────────────────────────────────────────── */
function lcpOverlay(title, meta, pct){
  const ov = $('lcpOverlay');
  if(!ov) return;
  ov.style.display = 'flex';
  ov.classList.remove('wide');
  const card = ov.querySelector('.lcp-ov-card');
  if(card){
    const old = card.querySelector('.lcp-ov-video');
    if(old) old.remove();
    const pre = card.querySelector('.lcp-ov-report');
    if(pre) pre.remove();
    const rv = card.querySelector('.lcp-rv');     // v1.4.73 导出前总览
    if(rv) rv.remove();
  }
  $('lcpOvTitle').textContent = title || '';
  $('lcpOvMeta').textContent = meta || '';
  $('lcpOvFill').style.width = _lcpClamp(pct || 0, 0, 100) + '%';
  const bar = ov.querySelector('.lcp-ov-bar');
  if(bar) bar.style.display = '';        // 每次复原，避免「确认框」把进度条永久藏掉
  $('lcpOvActs').innerHTML = '';
}
function lcpOverlayHide(){
  const ov = $('lcpOverlay');
  if(ov) ov.style.display = 'none';
}
function lcpOverlayFail(msg, retryFn){
  lcpOverlay('出错了', msg || '未知错误', 0);
  const acts = $('lcpOvActs');
  const b = document.createElement('button');
  b.className = 'mini-btn';
  b.textContent = '← 返回直播切片';
  b.addEventListener('click', () => { lcpOverlayHide(); lcpClose(); });
  acts.appendChild(b);
  if(typeof retryFn === 'function'){
    const b2 = document.createElement('button');
    b2.className = 'mini-btn';
    b2.textContent = '重试';
    b2.addEventListener('click', retryFn);
    acts.appendChild(b2);
  }
}

async function lcpAnalyze(opts){
  opts = opts || {};
  _lcpLastReq = opts;
  const body = {
    use_asr: $('lcAsr') ? $('lcAsr').checked : true,
    asr_language: $('lcLang') ? $('lcLang').value : 'auto',
    max_points: parseInt(($('lcPoints') || {}).value, 10) || 24,
    point_max_sec: 40,
  };
  if(_lcDanmakuText) body.danmaku_text = _lcDanmakuText;
  if(opts.reuseRun) body.reuse_run = opts.reuseRun;
  else {
    const vo = _lcVideoObj();
    if(vo) body.video = vo;
    else {
      const p = (($('lcPath') || {}).value || '').trim();
      if(!p){ toast('先选择录像或填写本地路径', 'bad'); return; }
      body.src_path = p;
      body.desktop_token = _token || '';
    }
  }
  lcpOpen();
  _lcpBusy = true;
  _lcpRunDir = ''; _lcpSegs = []; _lcpSel = -1;
  _lcpPoints = []; _lcpCurve = []; _lcpAsr = []; _lcpUndoStk = []; _lcpTitles = {};
  // 换源片前必须把上一支视频卸掉：否则分析期间旧画面还在播，用户会以为分析没开始
  if(_lcpVideo){
    try{ _lcpVideo.pause(); }catch(e){}
    _lcpVideo.removeAttribute('src');
    _lcpVideo.dataset.src = '';
    try{ _lcpVideo.load(); }catch(e){}
  }
  lcpSyncUndoBtn();
  if($('lcpNotes')) $('lcpNotes').innerHTML = '';
  if($('lcpClips')) $('lcpClips').innerHTML = '';
  if($('lcpRuler')) $('lcpRuler').innerHTML = '';
  if($('lcpInsp')){ $('lcpInsp').style.display = 'none'; $('lcpInsp').innerHTML = ''; }
  if($('lcpSrc')) $('lcpSrc').textContent = '';
  if($('lcpCurSeg')) $('lcpCurSeg').textContent = '未选中片段';
  lcpOverlay('正在分析全片高能点…',
    '音量 + 画面动作 + 台词密度' + (_lcDanmakuText ? ' + 弹幕热度' : '') + ' 综合评分', 0);
  try{
    const r = await fetch('/api/live_clip_timeline', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}).then(x => x.json());
    if(!r.ok) throw new Error(r.error || '提交失败');
    stopHandle(_lcpPoll);
    _lcpPoll = pollRun(r.runid, {
      interval: 1200,
      onTick: p => {
        $('lcpOvFill').style.width = _lcpClamp(p.pct || 0, 0, 100) + '%';
        $('lcpOvMeta').textContent = (p.phase || '处理中') + ' · ' + (p.pct || 0) + '%';
      },
      onError: p => {
        _lcpPoll = null; _lcpBusy = false;
        lcpOverlayFail(p.error, () => lcpAnalyze(_lcpLastReq || {}));
      },
      isDone: p => !!(p.done && p.draft_saved),
      onDone: p => {
        _lcpPoll = null; _lcpBusy = false;
        lcpApplyAnalysis(p);
      }
    }).promise.catch(() => { /* 已在 onError 提示 */ });
  }catch(e){
    _lcpBusy = false;
    lcpOverlayFail(e.message);
  }
}

function lcpApplyAnalysis(p){
  _lcpRunDir = p.run_dir || '';
  _lcpVdur = Number(p.vdur) || 0;
  _lcpWin = Number(p.window) || 2;
  _lcpVideoFile = p.video_file || '';
  _lcpPoints = (p.points || []).slice();
  _lcpCurve = p.curve || [];
  _lcpAsr = p.asr || [];
  const pick = (_lcVideo && _lcVideo.name) || (($('lcPath') || {}).value || '').split(/[\\/]/).pop();
  _lcpSrcName = pick || '源录像';
  $('lcpSrc').textContent = _lcpSrcName + ' · ' + _lcpFmt(_lcpVdur)
    + (_lcpPoints.length ? ' · ' + _lcpPoints.length + ' 个高能点' : '');
  // 初始时间轴 = 全部高能点（按时间）。用户从这条「完整高光带」上删/剪，
  // 而不是面对一条空时间轴不知道从哪下手。
  let segs = _lcpPoints.map(pt => ({start: pt.start, end: pt.end, volume: 1}));
  let restored = false;
  try{
    const saved = JSON.parse(localStorage.getItem(_lcpKey()) || 'null');
    if(saved && Array.isArray(saved.segs) && saved.segs.length
       && Math.abs((saved.vdur || 0) - _lcpVdur) < 1.5){
      segs = saved.segs.map(s => ({start: +s.start, end: +s.end,
                                   volume: s.volume == null ? 1 : +s.volume}))
        .filter(s => isFinite(s.start) && isFinite(s.end) && s.end > s.start);
      restored = segs.length > 0;
      if(saved.titles) _lcpTitles = saved.titles;
      if(saved.px){ _lcpPx = _lcpClamp(+saved.px, 3, 48); $('lcpZoom').value = _lcpPx; }
    }
  }catch(e){}
  _lcpSegs = segs;
  _lcpSel = _lcpSegs.length ? 0 : -1;
  _lcpUndoStk = []; lcpSyncUndoBtn();
  if(!_lcpVideoFile){
    toast('该任务源片不在任务目录内，预览不可用（仍可编辑与导出）', 'bad');
  }
  _lcpBindVideo();
  lcpRenderAll();
  if(_lcpSel >= 0) lcpSelect(_lcpSel, false);   // 同步底部「当前片段」与设置面板
  lcpOverlayHide();
  toast(restored
    ? '✅ 已恢复上次的剪辑（' + _lcpSegs.length + ' 段）'
    : '✅ 已标记 ' + _lcpPoints.length + ' 个高能点，开始精剪吧', 'ok');
}

/* ── 预览播放器 ────────────────────────────────────────────────── */
function lcpSrcUrl(){
  if(!_lcpRunDir || !_lcpVideoFile) return '';
  return encodeURI('/media/' + _lcpRunDir.replace(/\\/g, '/') + '/' + _lcpVideoFile);
}
function _lcpBindVideo(){
  if(!_lcpVideo) _lcpVideo = $('lcpVideo');
  if(!_lcpVideo) return;
  if(!_lcpVideoBound){
    _lcpVideoBound = true;
    _lcpVideo.addEventListener('timeupdate', lcpOnTime);
    _lcpVideo.addEventListener('seeked', lcpOnTime);
    _lcpVideo.addEventListener('loadedmetadata', lcpOnTime);
    _lcpVideo.addEventListener('play', () => { if($('lcpPlay')) $('lcpPlay').textContent = '⏸'; });
    _lcpVideo.addEventListener('pause', () => { if($('lcpPlay')) $('lcpPlay').textContent = '▶'; });
    _lcpVideo.addEventListener('error', () => {
      if(_lcpVideoFile) toast('源片预览加载失败（/media 不可达或浏览器不支持该编码）', 'bad');
    });
  }
  const src = lcpSrcUrl();
  if(src && _lcpVideo.dataset.src !== src){
    _lcpVideo.dataset.src = src;
    _lcpVideo.src = src;
    _lcpVideo.load();
  }
}
function lcpSeek(t, play, stopAt){
  if(!_lcpVideo || !_lcpVideoFile) return;
  const v = _lcpClamp(Number(t) || 0, 0, Math.max(0, _lcpVdur - 0.05));
  try{ _lcpVideo.currentTime = v; }catch(e){}
  _lcpStopAt = (stopAt == null ? null : Math.max(v, Number(stopAt)));
  if(play){ const pp = _lcpVideo.play(); if(pp && pp.catch) pp.catch(() => {}); }
}
function lcpTogglePlay(){
  if(!_lcpVideo || !_lcpVideoFile) return;
  if(_lcpVideo.paused){ _lcpStopAt = null; const pp = _lcpVideo.play(); if(pp && pp.catch) pp.catch(() => {}); }
  else _lcpVideo.pause();
}
function lcpOnTime(){
  if(!_lcpVideo) return;
  const t = _lcpVideo.currentTime || 0;
  if($('lcpTc')) $('lcpTc').textContent = _lcpFmt(t) + ' / ' + _lcpFmt(_lcpVdur);
  if(_lcpStopAt != null && t >= _lcpStopAt - 0.03){
    try{ _lcpVideo.pause(); }catch(e){}
    _lcpStopAt = null;
  }
  const head = $('lcpOvHead');
  if(head && _lcpVdur > 0) head.style.left = (t / _lcpVdur * 100) + '%';
  lcpUpdateSeqHead(t);
}
function lcpUpdateSeqHead(t){
  const head = $('lcpSeqHead'), clips = $('lcpClips');
  if(!head || !clips) return;
  const offs = _lcpOffsets();
  let x = 0, idx = -1;
  for(let i = 0; i < _lcpSegs.length; i++){
    const s = _lcpSegs[i];
    if(t >= s.start - 0.001 && t <= s.end + 0.001){
      x = offs[i] + (t - s.start) * _lcpPx;
      idx = i;
      break;
    }
  }
  if(idx >= 0){ head.style.display = 'block'; head.style.left = Math.round(x) + 'px'; }
  else head.style.display = 'none';
  Array.from(clips.children).forEach((el, i) => el.classList.toggle('playing', i === idx));
}

/* ── 时间轴几何 ────────────────────────────────────────────────── */
function _lcpSegW(s){ return Math.max(28, Math.max(0, s.end - s.start) * _lcpPx); }
function _lcpOffsets(){
  const out = [];
  let x = 0;
  for(const s of _lcpSegs){ out.push(x); x += _lcpSegW(s) + 2; }
  return out;
}
function _lcpTickStep(){
  const cands = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
  for(const c of cands){ if(c * _lcpPx >= 64) return c; }
  return 900;
}
function _lcpViewW(){
  const seq = $('lcpSeq');
  return seq ? Math.max(120, seq.clientWidth - 2) : 600;
}
function lcpApplyGeometry(){
  if(_lcpRaf) return;
  _lcpRaf = requestAnimationFrame(() => { _lcpRaf = null; lcpApplyGeometryNow(); });
}
function lcpApplyGeometryNow(){
  const clips = $('lcpClips'), inner = $('lcpSeqInner');
  if(!clips || !inner) return;
  const offs = _lcpOffsets();
  const total = offs.length
    ? offs[offs.length - 1] + _lcpSegW(_lcpSegs[_lcpSegs.length - 1]) + 10 : 0;
  inner.style.width = Math.max(_lcpViewW(), total) + 'px';
  Array.from(clips.children).forEach((el, i) => {
    const s = _lcpSegs[i];
    if(!s) return;
    el.style.left = offs[i] + 'px';
    el.style.width = _lcpSegW(s) + 'px';
    const tt = el.querySelector('.lcp-clip-t');
    if(tt) tt.textContent = _lcpFmt(s.start) + '–' + _lcpFmt(s.end);
  });
  lcpUpdateTotals();
}

/* ── 渲染：时间轴 ──────────────────────────────────────────────── */
function lcpRenderSeq(){
  const clips = $('lcpClips'), ruler = $('lcpRuler'), inner = $('lcpSeqInner');
  if(!clips || !ruler || !inner) return;
  const offs = _lcpOffsets();
  const total = offs.length
    ? offs[offs.length - 1] + _lcpSegW(_lcpSegs[_lcpSegs.length - 1]) + 10 : 0;
  inner.style.width = Math.max(_lcpViewW(), total) + 'px';
  // 标尺按「成片时长」（各段之和）排，不是源片时长：时间轴是可编辑序列，
  // 用源片长度画刻度会在末尾拖出一大截无法点击的空白。
  const seqDur = _lcpSegs.reduce((a, s) => a + Math.max(0, s.end - s.start), 0);
  ruler.innerHTML = '';
  const step = _lcpTickStep();
  for(let t = 0; t <= seqDur + 0.01; t += step){
    const d = document.createElement('div');
    d.className = 'lcp-tick';
    d.style.left = Math.round(t * _lcpPx) + 'px';
    d.textContent = _lcpFmt(t);
    ruler.appendChild(d);
  }
  clips.innerHTML = '';
  _lcpSegs.forEach((s, i) => {
    const el = document.createElement('div');
    el.className = 'lcp-clip' + (i === _lcpSel ? ' sel' : '')
      + (s.volume > 1.001 ? ' vol-up' : '') + (s.volume <= 0.001 ? ' vol-zero' : '');
    el.style.left = offs[i] + 'px';
    el.style.width = _lcpSegW(s) + 'px';
    el.dataset.i = i;
    const n = document.createElement('div');
    n.className = 'lcp-clip-n';
    n.textContent = '#' + (i + 1);
    const tt = document.createElement('div');
    tt.className = 'lcp-clip-t';
    tt.textContent = _lcpFmt(s.start) + '–' + _lcpFmt(s.end);
    const v = document.createElement('div');
    v.className = 'lcp-clip-v';
    v.textContent = s.volume <= 0.001 ? '🔇' : ('🔊' + Math.round(s.volume * 100) + '%');
    const hl = document.createElement('div');
    hl.className = 'h l'; hl.dataset.h = 'l';
    const hr = document.createElement('div');
    hr.className = 'h r'; hr.dataset.h = 'r';
    el.append(n, tt, v, hl, hr);
    _lcpBindClip(el, i);
    clips.appendChild(el);
  });
  lcpUpdateSeqHead(_lcpVideo ? (_lcpVideo.currentTime || 0) : 0);
}
function _lcpBindClip(el, i){
  el.addEventListener('pointerdown', ev => {
    if(ev.button !== 0) return;
    const h = ev.target && ev.target.dataset ? ev.target.dataset.h : '';
    if(h){ _lcpStartTrim(ev, i, h); return; }
    _lcpStartDrag(ev, el, i);
  });
}
function _lcpStartDrag(ev, el, i){
  ev.preventDefault();
  const startX = ev.clientX;
  let moved = false, target = i;
  const rects = Array.from($('lcpClips').children).map(c => {
    const r = c.getBoundingClientRect();
    return r.left + r.width / 2;
  });
  const onMove = e => {
    const dx = e.clientX - startX;
    if(!moved && Math.abs(dx) > 4) moved = true;
    if(!moved) return;
    el.classList.add('dragging');
    el.style.transform = 'translateX(' + dx + 'px)';
    let k = 0;
    for(let j = 0; j < rects.length; j++){ if(e.clientX > rects[j]) k = j + 1; }
    target = _lcpClamp(k, 0, _lcpSegs.length - 1);
    Array.from($('lcpClips').children).forEach((c, j) => c.classList.toggle('sel', j === target));
  };
  const onUp = () => {
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', onUp);
    el.classList.remove('dragging');
    el.style.transform = '';
    if(!moved){ lcpSelect(i, true); return; }
    let dst = target;
    if(dst > i) dst -= 1;
    if(dst === i){ lcpRenderSeq(); return; }
    lcpUndoPush();
    const seg = _lcpSegs.splice(i, 1)[0];
    _lcpSegs.splice(_lcpClamp(dst, 0, _lcpSegs.length), 0, seg);
    _lcpSel = _lcpClamp(dst, 0, _lcpSegs.length - 1);
    lcpRenderAll(); lcpSave();
    toast('已移动到第 ' + (_lcpSel + 1) + ' 位');
  };
  window.addEventListener('pointermove', onMove);
  window.addEventListener('pointerup', onUp);
}
function _lcpStartTrim(ev, i, which){
  ev.preventDefault();
  ev.stopPropagation();
  const s = _lcpSegs[i];
  const startX = ev.clientX;
  const s0 = s.start, e0 = s.end;
  const snap = JSON.stringify(_lcpSegs);
  let dirty = false;
  lcpSelect(i, false);
  const onMove = e => {
    const dx = e.clientX - startX;
    if(!dirty && Math.abs(dx) > 2) dirty = true;
    if(!dirty) return;
    const dt = dx / _lcpPx;
    if(which === 'l') s.start = _lcpClamp(s0 + dt, 0, e0 - 2);
    else s.end = _lcpClamp(e0 + dt, s0 + 2, _lcpVdur);
    lcpApplyGeometry();
    lcpRenderInspector();
  };
  const onUp = () => {
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', onUp);
    if(dirty){
      lcpUndoPushSnap(snap);
      lcpRenderAll(); lcpSave();
    }
  };
  window.addEventListener('pointermove', onMove);
  window.addEventListener('pointerup', onUp);
}

/* ── 渲染：注释列表 ────────────────────────────────────────────── */
function _lcpPtText(pt){
  if(pt.text) return pt.text;
  return (pt.lines || []).map(x => x.text || '').join(' ').trim();
}
function _lcpPointFor(seg){
  let best = null, bestOl = 0;
  for(const pt of _lcpPoints){
    const ol = _lcpOverlap(seg, pt);
    if(ol > bestOl){ best = pt; bestOl = ol; }
  }
  return best;
}
function _lcpPointKey(seg){
  const pt = _lcpPointFor(seg);
  if(pt) return 'p' + Math.round(pt.start * 10);
  return 't' + Math.round(seg.start * 10);
}
function _lcpLineOk(text){
  // 与后端 _line_weight 同一口径：整条台词是同一个单元在复读才算噪音
  //（『呃呃』『纵纵』『对对对』）。早先按「不同字数 ≤2」判，会把
  //『好的』『卧槽666』这种真实短句也滤掉，前后端口径不一致。
  const s = String(text || '').replace(/[^\u4e00-\u9fa5A-Za-z0-9]/g, '');
  const n = s.length;
  if(n <= 1) return false;
  for(let L = 1; L <= (n >> 1); L++){
    if(n % L) continue;
    const rep = n / L;
    if(L > 1 && rep < 3) continue;          // 多字单元要重复 3 次以上才算幻觉
    let same = true;
    for(let i = L; i < n; i++){ if(s[i] !== s[i - L]){ same = false; break; } }
    if(same) return false;
  }
  return true;
}
function _lcpLinesIn(start, end){
  return (_lcpAsr || []).filter(x => Number(x.end) > start && Number(x.start) < end)
    .filter(x => _lcpLineOk(x.text))
    .slice(0, 200);
}
function lcpRenderNotes(){
  const box = $('lcpNotes');
  if(!box) return;
  box.innerHTML = '';
  const q = (_lcpQ || '').trim().toLowerCase();
  let list = _lcpPoints.map((pt, i) => ({pt, i}));
  if(q){
    list = list.filter(o => (_lcpPtText(o.pt) + ' ' + (o.pt.reasons || []).join(' ')).toLowerCase().indexOf(q) >= 0);
  }
  if(_lcpSort === 'score') list.sort((a, b) => (b.pt.pct || 0) - (a.pt.pct || 0) || a.pt.start - b.pt.start);
  else list.sort((a, b) => a.pt.start - b.pt.start);
  if($('lcpNoteCount')) $('lcpNoteCount').textContent = '· ' + _lcpPoints.length + ' 个';
  if(!list.length){
    const e = document.createElement('div');
    e.className = 'lcp-note';
    e.style.cursor = 'default';
    e.textContent = q ? '没有匹配的注释' : '没有注释（录像可能整段平淡）';
    box.appendChild(e);
    return;
  }
  list.forEach(({pt, i}) => {
    const usedIdx = _lcpSegs.findIndex(s => _lcpOverlap(s, pt) / Math.max(1e-6, pt.dur) >= 0.4);
    const el = document.createElement('div');
    el.className = 'lcp-note' + (usedIdx >= 0 && usedIdx === _lcpSel ? ' on' : '')
      + (usedIdx < 0 ? ' dim' : '');
    const top = document.createElement('div');
    top.className = 'lcp-note-top';
    const id = document.createElement('span');
    id.className = 'lcp-note-id';
    id.textContent = '#' + (i + 1);
    const tr = document.createElement('span');
    tr.textContent = _lcpFmt(pt.start) + '–' + _lcpFmt(pt.end);
    const sc = document.createElement('span');
    sc.className = 'lcp-note-sc' + ((pt.pct || 0) >= 95 ? '' : ((pt.pct || 0) >= 85 ? ' s2' : ' s3'));
    sc.textContent = '高分 ' + (pt.pct || 0);
    sc.title = '全片分位：' + (pt.pct || 0) + '（越高越炸）';
    top.append(id, tr, sc);
    const tx = document.createElement('div');
    tx.className = 'lcp-note-tx';
    const tt = _lcpPtText(pt);
    tx.textContent = tt || (pt.noisy ? '（这段像音乐/掌声，没有清晰台词）' : '（这段没有台词记录）');
    el.append(top, tx);
    const acts = document.createElement('div');
    acts.className = 'lcp-note-act';
    if(usedIdx >= 0){
      const u = document.createElement('span');
      u.className = 'lcp-note-used';
      u.textContent = '✓ 已在时间轴 #' + (usedIdx + 1);
      acts.appendChild(u);
    } else {
      const b = document.createElement('button');
      b.className = 'mini-btn';
      b.textContent = '＋ 加入时间轴';
      b.addEventListener('click', ev => {
        ev.stopPropagation();
        lcpUndoPush();
        const idx = _lcpSegs.filter(s => s.start < pt.start).length;
        _lcpSegs.splice(idx, 0, {start: pt.start, end: pt.end, volume: 1});
        _lcpSel = idx;
        lcpRenderAll(); lcpSave();
        toast('已加入时间轴第 ' + (idx + 1) + ' 位', 'ok');
      });
      acts.appendChild(b);
    }
    el.appendChild(acts);
    el.addEventListener('click', ev => {
      if(ev.target && ev.target.closest && ev.target.closest('button')) return;
      if(usedIdx >= 0) lcpSelect(usedIdx, true);
      else {
        if(_lcpVideo) _lcpVideo.volume = 1;
        lcpSeek(pt.start + 0.03, true, pt.end);
        if($('lcpCurSeg')) $('lcpCurSeg').textContent = '注释 #' + (i + 1) + '（未加入时间轴）';
        lcpRenderNotes();
        el.classList.add('on');
      }
    });
    box.appendChild(el);
  });
}

/* ── 渲染：片段设置 ────────────────────────────────────────────── */
function lcpRenderInspector(){
  const box = $('lcpInsp');
  if(!box) return;
  const s = _lcpSegs[_lcpSel];
  if(!s){ box.style.display = 'none'; box.innerHTML = ''; return; }
  box.style.display = 'flex';
  box.innerHTML = '';
  const h = document.createElement('div');
  h.className = 'lcp-insp-h';
  h.innerHTML = '片段 #' + (_lcpSel + 1) + ' <span>' + _lcpDur(s.end - s.start) + '</span>';
  box.appendChild(h);
  const r1 = document.createElement('div');
  r1.className = 'lcp-insp-row';
  const mkNum = (label, val, on) => {
    const w = document.createElement('label');
    w.style.display = 'flex';
    w.style.alignItems = 'center';
    w.style.gap = '4px';
    w.append(label);
    const inp = document.createElement('input');
    inp.type = 'number';
    inp.step = '0.1';
    inp.min = '0';
    inp.max = String(Math.round(_lcpVdur));
    inp.value = (+val).toFixed(1);
    inp.addEventListener('change', () => {
      const snap = JSON.stringify(_lcpSegs);
      let v = parseFloat(inp.value);
      if(!isFinite(v)) v = val;
      on(v);
      lcpUndoPushSnap(snap);
      lcpRenderAll(); lcpSave();
    });
    w.appendChild(inp);
    return w;
  };
  r1.append(
    mkNum('起(秒)', s.start, v => { s.start = _lcpClamp(v, 0, s.end - 2); }),
    mkNum('止(秒)', s.end, v => { s.end = _lcpClamp(v, s.start + 2, _lcpVdur); })
  );
  const sp = document.createElement('span');
  sp.style.color = 'var(--dim)';
  sp.textContent = '时长 ' + _lcpDur(s.end - s.start);
  r1.appendChild(sp);
  box.appendChild(r1);

  const r2 = document.createElement('div');
  r2.className = 'lcp-insp-row';
  const lab = document.createElement('span');
  lab.textContent = '音量';
  const rg = document.createElement('input');
  rg.type = 'range'; rg.min = '0'; rg.max = '200'; rg.step = '5';
  rg.value = String(Math.round(s.volume * 100));
  const pct = document.createElement('b');
  pct.textContent = Math.round(s.volume * 100) + '%';
  pct.style.minWidth = '42px';
  let volSnap = null;
  rg.addEventListener('pointerdown', () => { volSnap = JSON.stringify(_lcpSegs); });
  rg.addEventListener('input', () => {
    s.volume = _lcpClamp((parseInt(rg.value, 10) || 0) / 100, 0, 2);
    pct.textContent = Math.round(s.volume * 100) + '%';
    const clip = $('lcpClips') ? $('lcpClips').children[_lcpSel] : null;
    if(clip){
      clip.classList.toggle('vol-up', s.volume > 1.001);
      clip.classList.toggle('vol-zero', s.volume <= 0.001);
      const v = clip.querySelector('.lcp-clip-v');
      if(v) v.textContent = s.volume <= 0.001 ? '🔇' : ('🔊' + Math.round(s.volume * 100) + '%');
    }
    lcpSave();
  });
  rg.addEventListener('change', () => {
    if(volSnap){ lcpUndoPushSnap(volSnap); volSnap = null; }
    lcpSave();
  });
  const bMute = document.createElement('button');
  bMute.className = 'mini-btn';
  bMute.textContent = '静音';
  bMute.addEventListener('click', () => {
    lcpUndoPush();
    s.volume = s.volume > 0 ? 0 : 1;
    lcpRenderAll(); lcpSave();
  });
  const bOrig = document.createElement('button');
  bOrig.className = 'mini-btn';
  bOrig.textContent = '原声 100%';
  bOrig.addEventListener('click', () => {
    lcpUndoPush();
    s.volume = 1;
    lcpRenderAll(); lcpSave();
  });
  r2.append(lab, rg, pct, bMute, bOrig);
  box.appendChild(r2);

  const r3 = document.createElement('div');
  r3.className = 'lcp-insp-acts';
  const bPlay = document.createElement('button');
  bPlay.className = 'mini-btn';
  bPlay.textContent = '▶ 只播这段';
  bPlay.addEventListener('click', () => lcpSelect(_lcpSel, true));
  const bMerge = document.createElement('button');
  bMerge.className = 'mini-btn';
  bMerge.textContent = '⇥ 合并下一段';
  bMerge.title = '把这段和它后面那段连成一段（M）——中间的空隙也会并进来';
  bMerge.disabled = _lcpSel >= _lcpSegs.length - 1;
  bMerge.addEventListener('click', lcpMerge);
  const bSplit = document.createElement('button');
  bSplit.className = 'mini-btn';
  bSplit.textContent = '✂ 在播放头分割';
  bSplit.addEventListener('click', lcpSplit);
  const bDel = document.createElement('button');
  bDel.className = 'mini-btn';
  bDel.textContent = '🗑 删除';
  bDel.addEventListener('click', lcpDelete);
  const bSum = document.createElement('button');
  bSum.className = 'mini-btn';
  bSum.textContent = '✨ AI 摘要';
  bSum.addEventListener('click', () => lcpSummary(_lcpSel, bSum));
  r3.append(bPlay, bMerge, bSplit, bDel, bSum);
  box.appendChild(r3);

  const title = _lcpTitles[_lcpPointKey(s)];
  if(title){
    const d = document.createElement('div');
    d.className = 'lcp-insp-row';
    d.style.color = 'var(--acc2)';
    d.textContent = '摘要：' + title;
    box.appendChild(d);
  }
  if(s.volume > 1.001){
    const n = document.createElement('div');
    n.className = 'lcp-insp-row';
    n.style.color = 'var(--dim)';
    n.textContent = '预览最多 100%，超过 100% 的增益在导出时生效';
    box.appendChild(n);
  }
}

/* ── 操作：选中 / 分割 / 删除 / 摘要 ───────────────────────────── */
function lcpSelect(i, seek){
  _lcpSel = (i >= 0 && i < _lcpSegs.length) ? i : -1;
  lcpRenderSeq();
  lcpRenderInspector();
  lcpRenderNotes();
  const s = _lcpSegs[_lcpSel];
  if(s && seek){
    if(_lcpVideo) _lcpVideo.volume = Math.min(1, Math.max(0, s.volume));
    lcpSeek(s.start + 0.03, true, s.end);
  }
  if($('lcpCurSeg')){
    $('lcpCurSeg').textContent = s
      ? ('片段 #' + (_lcpSel + 1) + '  ' + _lcpFmt(s.start) + '–' + _lcpFmt(s.end)
         + '  ' + _lcpDur(s.end - s.start) + '  ·  音量 ' + Math.round(s.volume * 100) + '%')
      : '未选中片段';
  }
  const seq = $('lcpSeq'), el = $('lcpClips') ? $('lcpClips').children[_lcpSel] : null;
  if(seq && el){
    const L = el.offsetLeft, R = L + el.offsetWidth;
    if(L < seq.scrollLeft + 20) seq.scrollLeft = Math.max(0, L - 20);
    else if(R > seq.scrollLeft + seq.clientWidth - 20) seq.scrollLeft = R - seq.clientWidth + 20;
  }
}
/* ── 时间轴整理（v1.4.72） ────────────────────────────────────────
   纯函数放前面，node 里能直接跑（见 tests/test_precise_timeline_ops.py）。
   为什么需要：同一支片反复调时，时间轴很快变成一堆碎段——想并回去、想按
   时间重排、想把全片音量统一压到 30%，以前只能一段段点。 */
function lcpMergeSegs(segs, i){
  const arr = (segs || []).map(s => ({
    start: +s.start, end: +s.end,
    volume: (s.volume === undefined || s.volume === null) ? 1 : +s.volume
  }));
  if(!arr.length || i < 0 || i >= arr.length - 1) return null;
  const a = arr[i], b = arr[i + 1];
  const merged = {
    start: Math.min(a.start, b.start),
    end: Math.max(a.end, b.end),
    volume: a.volume              // 音量跟随「被选中的那段」，可预期
  };
  if(merged.end - merged.start < 0.1) return null;
  arr.splice(i, 2, merged);
  return {segs: arr, merged: merged};
}
function lcpSortSegsByStart(segs){
  return (segs || []).map(s => Object.assign({}, s))
    .sort((x, y) => (x.start - y.start) || (x.end - y.end));
}

function lcpMerge(){
  if(_lcpSel < 0){ toast('先选中一个片段', 'bad'); return; }
  const r = lcpMergeSegs(_lcpSegs, _lcpSel);
  if(!r){ toast('最后一段没有下一段可合并', 'bad'); return; }
  lcpUndoPush();
  const dur = r.merged.end - r.merged.start;
  _lcpSegs = r.segs;
  lcpRenderAll(); lcpSave();
  toast('已与下一段合并，当前段 ' + _lcpDur(dur));
}

function lcpResort(){
  if(_lcpSegs.length < 2){ toast('至少两段才需要重排'); return; }
  const before = _lcpSegs.map(s => s.start).join(',');
  const sorted = lcpSortSegsByStart(_lcpSegs);
  if(sorted.map(s => s.start).join(',') === before){ toast('已经按源片时间排好了'); return; }
  lcpUndoPush();
  _lcpSegs = sorted;
  _lcpSel = -1;
  lcpRenderAll(); lcpSave();
  toast('已按源片时间重排（成片顺序随之改变）');
}

/* 全部音量：返工时最常见的诉求是「整体压到 X%」，一段段拉滑条太慢。
   滑条只改显示，点「套用」才落盘——避免拖动过程中把 20 多段反复重绘。 */
function lcpAllVolShow(){
  if($('lcpVolAllV')) $('lcpVolAllV').textContent = $('lcpVolAll').value + '%';
}
function lcpApplyAllVolume(){
  if(!_lcpSegs.length){ toast('时间轴是空的', 'bad'); return; }
  const v = _lcpClamp((parseInt($('lcpVolAll').value, 10) || 0) / 100, 0, 2);
  lcpUndoPush();
  _lcpSegs.forEach(s => { s.volume = v; });
  lcpRenderAll(); lcpSave();
  toast('全部 ' + _lcpSegs.length + ' 段音量 → ' + Math.round(v * 100) + '%');
}

function lcpSplit(){
  if(_lcpSel < 0){ toast('先选中一个片段', 'bad'); return; }
  const t = _lcpVideo ? (_lcpVideo.currentTime || 0) : 0;
  const s = _lcpSegs[_lcpSel];
  if(!(t > s.start + 1 && t < s.end - 1)){
    toast('把播放头放到片段中间再分割（距两端至少 1 秒）', 'bad');
    return;
  }
  lcpUndoPush();
  const a = {start: s.start, end: +t.toFixed(2), volume: s.volume};
  const b = {start: +t.toFixed(2), end: s.end, volume: s.volume};
  _lcpSegs.splice(_lcpSel, 1, a, b);
  _lcpSel = _lcpSel + 1;
  lcpRenderAll(); lcpSave();
  toast('已分割为两段（' + _lcpDur(a.end - a.start) + ' + ' + _lcpDur(b.end - b.start) + '）');
}
function lcpDelete(){
  if(_lcpSel < 0){ toast('先选中一个片段', 'bad'); return; }
  lcpUndoPush();
  const gone = _lcpSegs.splice(_lcpSel, 1)[0];
  if(_lcpSel >= _lcpSegs.length) _lcpSel = _lcpSegs.length - 1;
  lcpRenderAll(); lcpSave();
  toast('已删除 ' + _lcpFmt(gone.start) + '–' + _lcpFmt(gone.end) + ' 这段');
}
async function lcpSummary(i, btn){
  const s = _lcpSegs[i];
  if(!s) return;
  const pt = _lcpPointFor(s);
  let lines = (pt && pt.lines && pt.lines.length) ? pt.lines : _lcpLinesIn(s.start, s.end);
  lines = lines.filter(x => x && String(x.text || '').trim());
  if(!lines.length){ toast('这段没有台词可总结（未启用转写或整段无语音）', 'bad'); return; }
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = '总结中…';
  try{
    const r = await fetch('/api/live_clip/summary', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({lines})}).then(x => x.json());
    if(!r.ok || !r.title) throw new Error(r.error || '模型没有返回标题');
    _lcpTitles[_lcpPointKey(s)] = r.title;
    lcpSave();
    lcpRenderInspector();
    lcpRenderNotes();
    toast('✅ 摘要：' + r.title, 'ok');
  }catch(e){
    toast('摘要失败：' + e.message, 'bad');
  }finally{
    btn.disabled = false;
    btn.textContent = old;
  }
}

/* ── 渲染：热度概览 ────────────────────────────────────────────── */
function lcpDrawOverview(){
  const cv = $('lcpCurve'), wrap = $('lcpOverview');
  if(!cv || !wrap) return;
  const w = wrap.clientWidth || 600, h = wrap.clientHeight || 56;
  const dpr = window.devicePixelRatio || 1;
  cv.width = Math.max(1, Math.round(w * dpr));
  cv.height = Math.max(1, Math.round(h * dpr));
  const c = cv.getContext('2d');
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, w, h);
  if(_lcpVdur <= 0) return;
  // 已选区间底色（先画，别盖住曲线）
  c.fillStyle = 'rgba(63,208,164,.07)';
  _lcpSegs.forEach(s => {
    c.fillRect(s.start / _lcpVdur * w, 0,
      Math.max(1, (s.end - s.start) / _lcpVdur * w), h);
  });
  c.strokeStyle = 'rgba(255,255,255,.05)';
  c.lineWidth = 1;
  for(let i = 1; i < 4; i++){
    const y = h * i / 4;
    c.beginPath(); c.moveTo(0, y); c.lineTo(w, y); c.stroke();
  }
  if(_lcpCurve.length){
    const n = _lcpCurve.length;
    const X = i => (i / Math.max(1, n - 1)) * w;
    const Y = v => h - _lcpClamp(v, 0, 1) * h * 0.9 - 2;
    const grad = c.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, 'rgba(61,126,255,.50)');
    grad.addColorStop(1, 'rgba(61,126,255,.04)');
    c.beginPath();
    c.moveTo(0, h);
    for(let i = 0; i < n; i++) c.lineTo(X(i), Y(_lcpCurve[i]));
    c.lineTo(w, h);
    c.closePath();
    c.fillStyle = grad;
    c.fill();
    c.beginPath();
    for(let i = 0; i < n; i++){
      if(i) c.lineTo(X(i), Y(_lcpCurve[i]));
      else c.moveTo(X(i), Y(_lcpCurve[i]));
    }
    c.strokeStyle = '#6f9cff';
    c.lineWidth = 1.3;
    c.stroke();
  }
  _lcpPoints.forEach(pt => {
    const x = _lcpClamp((pt.peak_t || pt.start) / _lcpVdur * w, 0, w - 2);
    c.fillStyle = (pt.pct || 0) >= 95 ? '#3fd0a4' : ((pt.pct || 0) >= 85 ? '#f5c451' : '#6f9cff');
    c.globalAlpha = 0.9;
    c.fillRect(x - 1, h - 11, 2, 9);
    c.globalAlpha = 1;
  });
}

/* ── 汇总 / 导出 ──────────────────────────────────────────────── */
function lcpUpdateTotals(){
  const tot = _lcpSegs.reduce((a, s) => a + Math.max(0, s.end - s.start), 0);
  if($('lcpTotal')) $('lcpTotal').textContent = _lcpSegs.length + ' 段 · ' + _lcpDur(tot);
  if($('lcpTlHint')){
    $('lcpTlHint').textContent = _lcpSegs.length
      ? '拖片段改顺序 · 拖两端改起止 · 空格播放'
      : '时间轴是空的：从右侧注释点「＋ 加入时间轴」';
  }
  if($('lcpExport')) $('lcpExport').disabled = !_lcpSegs.length || _lcpBusy;
}
function lcpRenderAll(){
  lcpRenderSeq();
  lcpRenderNotes();
  lcpRenderInspector();
  lcpDrawOverview();
  lcpUpdateTotals();
  lcpSyncUndoBtn();
}

/* ── 导出前总览（v1.4.73） ────────────────────────────────────────
   导出一次要几十秒到几分钟，导完才发现「整片 12 分钟」「有 3 处画面重复」
   代价太大。这里先把账算清楚——纯函数，node 里可以直接跑。 */
function lcpExportSummary(segs){
  const raw = segs || [];
  const arr = [], bad = [];
  raw.forEach((s, i) => {
    const a = +((s || {}).start), b = +((s || {}).end);
    if(!isFinite(a) || !isFinite(b) || b - a < 0.05){ bad.push(i + 1); return; }
    arr.push({start: a, end: b, volume: (s.volume === undefined || s.volume === null) ? 1 : +s.volume});
  });
  const n = arr.length;
  if(!n) return {n: 0, total: 0, max: 0, min: 0, vmin: 0, vmax: 0,
                 overlaps: [], tooShort: [], muted: [], overCount: false, bad: bad};
  const durs = arr.map(s => s.end - s.start);
  const vols = arr.map(s => s.volume);
  const overlaps = [];
  for(let i = 0; i < n; i++){
    for(let j = i + 1; j < n; j++){
      const ov = Math.min(arr[i].end, arr[j].end) - Math.max(arr[i].start, arr[j].start);
      if(ov > 0.5) overlaps.push([i + 1, j + 1, Math.round(ov * 10) / 10]);
    }
  }
  const tooShort = [], muted = [];
  arr.forEach((s, i) => {
    if(s.end - s.start < 1.0) tooShort.push(i + 1);
    if(s.volume <= 0.001) muted.push(i + 1);
  });
  return {
    n: n,
    total: durs.reduce((a, b) => a + b, 0),
    max: Math.max.apply(null, durs),
    min: Math.min.apply(null, durs),
    vmin: Math.min.apply(null, vols),
    vmax: Math.max.apply(null, vols),
    overlaps: overlaps, tooShort: tooShort, muted: muted,
    overCount: n > 60, bad: bad
  };
}

function lcpExportReview(){
  if(_lcpBusy){ toast('正在导出，先等这次跑完', 'bad'); return; }
  const sum = lcpExportSummary(_lcpSegs);
  if(!sum.n){ toast('时间轴是空的', 'bad'); return; }
  const rows = [];
  rows.push('<div class="lcp-rv-hero">' + sum.n + ' 段 · 成片 <b>' + _lcpDur(sum.total) + '</b></div>');
  rows.push('<div class="lcp-rv-sub">最长 ' + _lcpDur(sum.max) + ' · 最短 ' + _lcpDur(sum.min)
    + ' · 音量 ' + Math.round(sum.vmin * 100) + '%~' + Math.round(sum.vmax * 100) + '%</div>');
  const warn = [];
  if(sum.bad.length){
    warn.push('有 ' + sum.bad.length + ' 段时间非法（起止为空或结束早于开始）：#' + sum.bad.slice(0, 6).join('、'));
  }
  if(sum.overCount) warn.push('段数 ' + sum.n + ' 超过 60，后端会拒绝导出——先合并几段再导');
  if(sum.overlaps.length){
    warn.push('有 ' + sum.overlaps.length + ' 处画面重叠：同一段素材会在成片里出现两次（'
      + sum.overlaps.slice(0, 3).map(o => '#' + o[0] + '↔#' + o[1]).join('、')
      + (sum.overlaps.length > 3 ? ' 等' : '') + '）');
  }
  if(sum.tooShort.length){
    warn.push('有 ' + sum.tooShort.length + ' 段不足 1 秒（#' + sum.tooShort.slice(0, 6).join('、') + '），会一闪而过');
  }
  if(sum.muted.length){
    warn.push('有 ' + sum.muted.length + ' 段是静音（#' + sum.muted.slice(0, 6).join('、') + '）');
  }
  if(warn.length){
    rows.push('<ul class="lcp-rv-warn">' + warn.map(t => '<li>' + _esc(t) + '</li>').join('') + '</ul>');
  } else {
    rows.push('<div class="lcp-rv-ok">✓ 没有发现明显问题，可以导出</div>');
  }

  lcpOverlay('导出前确认', '', 100);
  // 这是「确认」不是「进度」：把 100% 的进度条藏掉，别让人以为已经导完了
  const _bar = $('lcpOverlay').querySelector('.lcp-ov-bar');
  if(_bar) _bar.style.display = 'none';
  const card = $('lcpOverlay').querySelector('.lcp-ov-card');
  const box = document.createElement('div');
  box.className = 'lcp-rv';
  box.innerHTML = rows.join('');
  card.insertBefore(box, $('lcpOvActs'));

  const acts = $('lcpOvActs');
  const bBack = document.createElement('button');
  bBack.className = 'mini-btn';
  bBack.textContent = '返回调整';
  bBack.addEventListener('click', lcpOverlayHide);
  const bGo = document.createElement('button');
  bGo.className = 'mini-btn primary';
  bGo.id = 'lcpExportGoBtn';
  bGo.textContent = '确认导出';
  bGo.disabled = !!(sum.overCount || sum.bad.length);
  if(bGo.disabled) bGo.title = '先修掉上面的问题再导出';
  bGo.addEventListener('click', lcpExportGo);
  acts.append(bBack, bGo);
}

async function lcpExportGo(){
  if(!_lcpSegs.length){ toast('时间轴是空的', 'bad'); return; }
  if(_lcpBusy) return;
  const segs = _lcpSegs.map(s => ({
    start: +(+s.start).toFixed(2), end: +(+s.end).toFixed(2),
    volume: +_lcpClamp(+s.volume, 0, 2).toFixed(3),
  }));
  _lcpAspectUsed = $('lcpAspect') ? $('lcpAspect').value : 'blur';
  lcpSaveNow();
  _lcpBusy = true;
  lcpUpdateTotals();
  lcpOverlay('正在导出精剪成片…',
    '逐段渲染（各自音量）→ 按时间轴顺序拼接', 3);
  try{
    const r = await fetch('/api/live_clip_timeline/export', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({run_dir: _lcpRunDir, segments: segs,
                            aspect: _lcpAspectUsed, focus: 0.5})}).then(x => x.json());
    if(!r.ok) throw new Error(r.error || '提交失败');
    stopHandle(_lcpPoll);
    _lcpPoll = pollRun(r.runid, {
      interval: 1200,
      onTick: p => {
        $('lcpOvFill').style.width = _lcpClamp(p.pct || 0, 0, 100) + '%';
        $('lcpOvMeta').textContent = (p.phase || '导出中') + ' · ' + (p.pct || 0) + '%';
      },
      onError: p => {
        _lcpPoll = null; _lcpBusy = false;
        lcpUpdateTotals();
        lcpOverlayFail(p.error);
      },
      isDone: p => !!(p.done && p.reel),
      onDone: p => {
        _lcpPoll = null; _lcpBusy = false;
        lcpUpdateTotals();
        lcpShowResult(p);
      }
    }).promise.catch(() => { /* 已在 onError 提示 */ });
  }catch(e){
    _lcpBusy = false;
    lcpUpdateTotals();
    lcpOverlayFail(e.message);
  }
}
function lcpExportNow(){ lcpExportReview(); }   // 入口统一先看总览

function lcpShowResult(p){
  const ov = $('lcpOverlay');
  lcpOverlay('✅ 精剪成片完成',
    p.reel.n + ' 段 · ' + _lcpDur(p.reel.dur) + ' · ' + p.reel.file, 100);
  ov.classList.add('wide');
  const card = ov.querySelector('.lcp-ov-card');
  const v = document.createElement('video');
  v.className = 'lcp-ov-video';
  v.controls = true;
  v.preload = 'metadata';
  v.style.aspectRatio = (_lcpAspectUsed === 'blur' || _lcpAspectUsed === 'crop') ? '9 / 16' : '16 / 9';
  v.src = encodeURI('/media/' + p.reel.rel);
  card.insertBefore(v, $('lcpOvActs'));
  const acts = $('lcpOvActs');
  const bFolder = document.createElement('button');
  bFolder.className = 'mini-btn';
  bFolder.textContent = '📁 打开所在文件夹';
  bFolder.addEventListener('click', () => {
    fetch('/api/reveal', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({file: p.reel.rel})});
  });
  const bReport = document.createElement('button');
  bReport.className = 'mini-btn';
  bReport.textContent = '📄 查看报告';
  bReport.addEventListener('click', async () => {
    try{
      const t = await fetch(encodeURI('/media/' + p.report_txt)).then(x => x.text());
      let pre = card.querySelector('.lcp-ov-report');
      if(!pre){
        pre = document.createElement('pre');
        pre.className = 'lcp-ov-report';
        pre.style.cssText = 'max-height:150px;overflow:auto;font-size:11px;color:var(--dim);'
          + 'background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px;'
          + 'white-space:pre-wrap;margin:0';
        card.appendChild(pre);
      }
      pre.textContent = t;
    }catch(e){ toast('报告读取失败：' + e.message, 'bad'); }
  });
  const bBack = document.createElement('button');
  bBack.className = 'mini-btn';
  bBack.textContent = '↩ 继续编辑';
  bBack.addEventListener('click', lcpOverlayHide);
  acts.append(bFolder, bReport, bBack);
  toast('✅ 精剪成片完成：' + p.reel.file, 'ok');
}

/* ── 事件绑定 ──────────────────────────────────────────────────── */
if($('lcpBack')) $('lcpBack').addEventListener('click', lcpClose);
if($('lcpExport')) $('lcpExport').addEventListener('click', lcpExportNow);
if($('lcpPlay')) $('lcpPlay').addEventListener('click', lcpTogglePlay);
if($('lcpSplit')) $('lcpSplit').addEventListener('click', lcpSplit);
if($('lcpDel')) $('lcpDel').addEventListener('click', lcpDelete);
if($('lcpUndo')) $('lcpUndo').addEventListener('click', lcpUndo);
if($('lcpResort')) $('lcpResort').addEventListener('click', lcpResort);
if($('lcpVolAll')){
  $('lcpVolAll').addEventListener('input', lcpAllVolShow);
  $('lcpVolAll').addEventListener('change', lcpAllVolShow);
}
if($('lcpVolApply')) $('lcpVolApply').addEventListener('click', lcpApplyAllVolume);
if($('lcpReset')) $('lcpReset').addEventListener('click', () => {
  if(!_lcpPoints.length) return;
  lcpUndoPush();
  _lcpSegs = _lcpPoints.map(pt => ({start: pt.start, end: pt.end, volume: 1}));
  _lcpSel = _lcpSegs.length ? 0 : -1;
  lcpRenderAll(); lcpSave();
  toast('已重置为分析出的全部高能点');
});
if($('lcpZoom')) $('lcpZoom').addEventListener('input', () => {
  _lcpPx = _lcpClamp(parseFloat($('lcpZoom').value) || 8, 3, 48);
  lcpRenderSeq();
  lcpUpdateSeqHead(_lcpVideo ? (_lcpVideo.currentTime || 0) : 0);
  lcpSave();
});
if($('lcpSort')) $('lcpSort').addEventListener('change', () => {
  _lcpSort = $('lcpSort').value;
  lcpRenderNotes();
});
if($('lcpSearch')) $('lcpSearch').addEventListener('input', () => {
  _lcpQ = $('lcpSearch').value;
  lcpRenderNotes();
});
if($('lcpOverview')) $('lcpOverview').addEventListener('pointerdown', ev => {
  const box = $('lcpOverview');
  const r = box.getBoundingClientRect();
  const toT = cx => _lcpClamp((cx - r.left) / Math.max(1, r.width) * _lcpVdur, 0, _lcpVdur);
  lcpSeek(toT(ev.clientX), false);
  const mv = e2 => { if(_lcpVideo) { try{ _lcpVideo.currentTime = toT(e2.clientX); }catch(e){} } };
  const up = () => {
    window.removeEventListener('pointermove', mv);
    window.removeEventListener('pointerup', up);
  };
  window.addEventListener('pointermove', mv);
  window.addEventListener('pointerup', up);
});
if($('lcPreciseGo')) $('lcPreciseGo').addEventListener('click', () => lcpAnalyze());
if(window.ResizeObserver && $('lcpOverview')){
  try{ new ResizeObserver(() => lcpDrawOverview()).observe($('lcpOverview')); }catch(e){}
}

document.addEventListener('keydown', ev => {
  const me = $('lcPrecise');
  if(!me || me.style.display === 'none') return;
  const tag = (ev.target && ev.target.tagName) || '';
  if(/INPUT|TEXTAREA|SELECT/.test(tag)) return;
  if(ev.code === 'Space'){ ev.preventDefault(); lcpTogglePlay(); }
  else if(ev.key === 'Delete' || ev.key === 'Backspace'){ ev.preventDefault(); lcpDelete(); }
  else if(ev.key === 's' || ev.key === 'S'){ ev.preventDefault(); lcpSplit(); }
  else if(ev.key === 'm' || ev.key === 'M'){ ev.preventDefault(); lcpMerge(); }
  else if((ev.ctrlKey || ev.metaKey) && (ev.key === 'z' || ev.key === 'Z')){ ev.preventDefault(); lcpUndo(); }
  else if(ev.key === 'ArrowLeft'){ ev.preventDefault(); lcpSeek((_lcpVideo ? _lcpVideo.currentTime : 0) - 1, false); }
  else if(ev.key === 'ArrowRight'){ ev.preventDefault(); lcpSeek((_lcpVideo ? _lcpVideo.currentTime : 0) + 1, false); }
});
