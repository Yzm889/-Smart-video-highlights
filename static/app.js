
const ITEMS = [];
const $ = id => document.getElementById(id);
const drop = $('drop'), fi = $('fileInput');
drop.addEventListener('click', () => fi.click());
fi.addEventListener('change', e => { for (const f of fi.files) handle(f); fi.value=''; });
drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('dragover'); });
drop.addEventListener('dragleave', () => drop.classList.remove('dragover'));
drop.addEventListener('drop', e => { e.preventDefault(); drop.classList.remove('dragover'); for (const f of e.dataTransfer.files) handle(f); });
$('defDur').addEventListener('change', () => { clearEmpty(); });

// ---- remember last-used settings across sessions (localStorage) ----
(function(){
  const KEY = 'springStudio.settings';
  // 追加记忆的控件：强卡点 / 短片解说 / 联网解说 参数（刷新后不丢失）
  const EXT = ['bcStrength','bcSensitivity','bcMinClip','bcBeatSync','bcKeepAudio','bcTransition',
               'narMaxSeg','movieMaxSeg','narTheme','narReq','narMode','movieMode','narBgm','movieBgm'];
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) || '{}');
    if (saved.res && $('res').querySelector('option[value="'+saved.res+'"]')) $('res').value = saved.res;
    if (saved.fps) $('fps').value = saved.fps;
    if (saved.trans) $('trans').value = saved.trans;
    if (saved.beatStep) $('beatStep').value = saved.beatStep;
    if (saved.hardCutSel) $('hardCutSel').value = saved.hardCutSel;
    if (saved.defDur) $('defDur').value = saved.defDur;
    if ($('aiCap')) $('aiCap').checked = !!saved.aiCap;
    EXT.forEach(id => {
      const el = $(id); if (!el) return;
      const v = saved[id];
      if (v === undefined || v === null) return;
      if (el.type === 'checkbox') el.checked = !!v;
      else el.value = v;
      if (id === 'bcSensitivity' && $('bcSensitivityText')) $('bcSensitivityText').textContent = v;
    });
  } catch(e){}
  function save(){
    const o = { res:$('res').value, fps:$('fps').value, trans:$('trans').value, beatStep:$('beatStep').value, hardCutSel:$('hardCutSel').value, defDur:$('defDur').value, aiCap: $('aiCap')?$('aiCap').checked:false };
    EXT.forEach(id => { const el=$(id); if (el) o[id] = el.type==='checkbox' ? el.checked : el.value; });
    try { localStorage.setItem(KEY, JSON.stringify(o)); } catch(e){}
  }
  const ids = ['res','fps','trans','beatStep','hardCutSel','defDur','aiCap'].concat(EXT);
  ids.forEach(id => { const el=$(id); if(el) el.addEventListener('change', save); });
})();

// ---- background music upload ----
let MUSIC = null;
const musDrop = $('musDrop'), mi = $('musInput');
function setMusic(file){
  if (!file) { MUSIC = null; musDrop.textContent = '把 mp3/wav 音乐拖到这里，或点此选择背景音乐'; $('musInfo2').style.display='none'; updateMusicHint(); return; }
  MUSIC = { name:file.name, file };
  musDrop.textContent = '🎵 已选：' + file.name;
  $('musInfo').style.display = file ? 'none' : '';
  $('musInfo2').style.display = 'block';
  $('musInfo2').textContent = '已选音乐，合成时自动分析节拍并对齐素材切换点。';
  updateMusicHint();
}
function updateMusicHint(){
  const hint = $('bcMusicHint');
  if (!hint) return;
  if (MUSIC) {
    hint.innerHTML = '✅ 已选背景音乐：<b>' + escapeHtml(MUSIC.name) + '</b>，可以开始强卡点了。';
    hint.style.background = '#ecfdf5';
    hint.style.borderColor = '#a7f3d0';
    hint.style.color = '#065f46';
  } else {
    hint.innerHTML = '🎵 未选择背景音乐：强卡点需要一首音乐来对齐鼓点。 <a href="javascript:void(0)" class="jump-link" onclick="goStep(\'musicCard\')">🎵 去选背景音乐</a> 或 <a href="javascript:void(0)" class="jump-link" onclick="goStep(\'libCard\')">🔎 去免费曲库搜一首</a>';
    hint.style.background = '';
    hint.style.borderColor = '';
    hint.style.color = '';
  }
}
function updateBuildModeHint(){
  const hint = $('buildModeHint');
  if (!hint) return;
  const eco = $('eco'), aiCap = $('aiCap');
  const ecoOn = eco ? eco.checked : true;
  const aiOn = aiCap ? aiCap.checked : false;
  if (ecoOn) {
    hint.innerHTML = '💡 <b>省流模式</b>：文案用离线模板、不调付费API，几乎不花钱。想让文案更聪明可启用 <a href="javascript:void(0)" class="jump-link" onclick="jumpToAISection(\'local\')">🖥 本地模型</a>（免费离线）或 <a href="javascript:void(0)" class="jump-link" onclick="jumpToAISection(\'cloud\')">🌐 云端API</a>（花少量钱）。';
    hint.style.background = '';
    hint.style.borderColor = '';
    hint.style.color = '';
  } else if (aiOn) {
    hint.innerHTML = '⚠️ <b>真AI模式</b>：将调用 DeepSeek 看图写文案 + 小米 MiMo 配音，会消耗API余额。请确保已配置 <a href="javascript:void(0)" class="jump-link" onclick="jumpToAISection(\'cloud\')">🌐 云端API（视觉+TTS）</a>，否则生成前会弹窗提醒。';
    hint.style.background = '#fffbeb';
    hint.style.borderColor = '#fde68a';
    hint.style.color = '#92400e';
  } else {
    hint.innerHTML = 'ℹ️ <b>基础合成模式</b>：不生成AI文案、不配音，只做素材拼接+转场+配乐。如需AI文案请勾选上方「按画面生成中文文案」。';
    hint.style.background = '#f0f9ff';
    hint.style.borderColor = '#bae6fd';
    hint.style.color = '#075985';
  }
}
musDrop.addEventListener('click', () => mi.click());
mi.addEventListener('change', e => { if (mi.files.length) setMusic(mi.files[0]); mi.value=''; });
musDrop.addEventListener('dragover', e => { e.preventDefault(); musDrop.classList.add('over'); });
musDrop.addEventListener('dragleave', () => musDrop.classList.remove('over'));
musDrop.addEventListener('drop', e => { e.preventDefault(); musDrop.classList.remove('over'); if (e.dataTransfer.files.length) setMusic(e.dataTransfer.files[0]); });

// ---- free 踩点 music library ----
let PREV_AUDIO = null;
function doSearch(){
  const q = ($('musSearch').value || '').trim();
  const box = $('musResults');
  box.innerHTML = '搜索中…';
  fetch('/api/music/search?q=' + encodeURIComponent(q)).then(r => r.json()).then(res => {
    if (!res.ok) { box.innerHTML = '❌ 搜索失败'; return; }
    if (!res.results.length) { box.innerHTML = '<div class="hint">未找到，换个词（如 电子/轻快/hip）。</div>'; return; }
    box.innerHTML = '';
    for (const t of res.results){
      const d = document.createElement('div');
      d.className = 'mres' + (t.cached ? ' done' : '');
      d.innerHTML = `<div class="info"><div class="t">${t.title}</div>
        <div class="m">${t.genre} · BPM~${t.bpm} · ${t.length!==null?'时长'+t.length+'s':'待缓存'} · ${t.license}</div></div>
        <audio preload="none"></audio>
        <button class="btn mini ghost" onclick="previewMusic('${t.id}','/music_lib/${t.id}.mp3',this)">▶ 预览</button>
        <button class="btn mini ghost" onclick="useMusic('${t.id}','${t.title}')">＋ 使用</button>`;
      box.appendChild(d);
    }
  }).catch(() => box.innerHTML = '❌ 搜索失败（服务未运行？）');
}
function previewMusic(id, url, btn){
  const audio = btn.parentElement.querySelector('audio');
  if (PREV_AUDIO && PREV_AUDIO !== audio){ PREV_AUDIO.pause(); PREV_AUDIO.currentTime=0; }
  if (audio.paused){
    audio.src = url + '?t=' + Date.now();
    audio.currentTime = 0;
    audio.play().then(()=>{ btn.textContent='⏸ 停止'; btn.dataset.on=1; }).catch(()=>{ btn.textContent='▶ 预览'; btn.dataset.on=0; });
    PREV_AUDIO = audio;
  } else { audio.pause(); audio.currentTime=0; btn.textContent='▶ 预览'; btn.dataset.on=0; PREV_AUDIO=null; }
}
function useMusic(id, title){
  fetch('/api/music/use?id=' + encodeURIComponent(id)).then(r=>r.json()).then(res=>{
    if (!res.ok) { alert('❌ ' + (res.error||'载入失败')); return; }
    MUSIC = { name:title, catalogId:id, url:res.url };
    musDrop.textContent = '🎵 已选（曲库）：' + title;
    $('musInfo').style.display = 'none';
    $('musInfo2').style.display = 'block';
    $('musInfo2').textContent = '已选曲库音乐，合成时自动分析节拍并对齐素材切换点。';
    updateMusicHint();
    // refresh result list to mark cached
    doSearch();
  }).catch(()=>alert('❌ 载入失败'));
}

// ---- AI config (vision & tts are two independent channels) ----
function loadAIConfig(){
  fetch('/api/ai/config').then(r=>r.json()).then(res=>{
    if(!res.ok) return;
    const c = res.config||{};
    const vis = c.vision||{}, tts = c.tts||{};
    if(vis.base_url) $('visBase').value = vis.base_url;
    if(vis.api_key) $('visKey').value = vis.api_key;
    if(vis.model) $('visModel').value = vis.model;
    if(tts.base_url) $('ttsBase').value = tts.base_url;
    if(tts.api_key) $('ttsKey').value = tts.api_key;
    if(tts.model) $('ttsModel').value = tts.model;
    if(tts.voice) $('ttsVoice').value = tts.voice;
    if(tts.provider) $('ttsProvider').value = tts.provider;
    const loc = c.local || {};
    if(loc.base_url) $('localBase').value = loc.base_url;
    if(loc.model) $('localModel').value = loc.model;
    if($('localEnabled')) $('localEnabled').checked = (loc.enabled !== false);
    const wsp = c.whisper || {};
    if(wsp.model && $('whisperModel')) $('whisperModel').value = wsp.model;
    const vlm = c.vlm || {};
    if($('vlmEnabled')) $('vlmEnabled').checked = (vlm.enabled === true);
    if(vlm.base_url) $('vlmBase').value = vlm.base_url;
    if(vlm.model) $('vlmModel').value = vlm.model;
    if(vlm.mode) $('vlmMode').value = vlm.mode;
    const mir = c.mirror || {};
    if($('mirUseMirror')) $('mirUseMirror').value = (mir.use_hf_mirror === false) ? '0' : '1';
    if(mir.hf_endpoint) $('mirHf').value = mir.hf_endpoint;
    if($('mirProxy')) $('mirProxy').value = mir.ollama_proxy || '';
    ttsProviderHint();
    const st = [];
    if(res.vision_available) st.push('视觉✅'); else st.push('视觉(离线)');
    if(res.tts_available) st.push('配音✅'); else st.push('配音未配');
    $('aiStatus').textContent = st.join(' · ');
  }).catch(()=>{});
}
function ttsProviderHint(){
  const p = $('ttsProvider').value;
  const optional = (p==='dashscope' || p==='mimo');
  $('ttsBase').placeholder = p==='dashscope' ? '通义可留空' : (p==='mimo' ? 'MiMo可留空' : '例 api.openai.com/v1');
  $('ttsModel').placeholder = p==='dashscope' ? '例 qwen-audio-turbo' : (p==='mimo' ? '例 mimo-v2.5-tts' : '例 tts-1');
}
function saveAIConfig(){
  const body = {
    vision: { base_url: $('visBase').value.trim(), api_key: $('visKey').value.trim(), model: $('visModel').value.trim() },
    tts:    { provider: $('ttsProvider').value, base_url: $('ttsBase').value.trim(), api_key: $('ttsKey').value.trim(), model: $('ttsModel').value.trim(), voice: $('ttsVoice').value.trim() },
    local:  { enabled: $('localEnabled') ? $('localEnabled').checked : true, base_url: $('localBase').value.trim(), model: $('localModel').value.trim() },
    whisper: { model: $('whisperModel') ? $('whisperModel').value.trim() : 'base' },
    vlm:    { enabled: $('vlmEnabled') ? $('vlmEnabled').checked : false, mode: $('vlmMode') ? $('vlmMode').value.trim() : 'ollama',
              base_url: $('vlmBase') ? $('vlmBase').value.trim() : '', model: $('vlmModel') ? $('vlmModel').value.trim() : '' },
  };
  fetch('/api/ai/config', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) })
    .then(r=>r.json()).then(res=>{
      const st=[];
      if(res.ok){
        st.push(res.vision_available?'视觉✅':'视觉(离线)');
        st.push(res.tts_available?'配音✅':'配音未配');
        $('aiStatus').textContent = '✅ 已保存 · ' + st.join(' · ');
        $('aiStatus').style.background = '#ecfdf5';
        $('aiStatus').style.border = '1px solid #a7f3d0';
        $('aiStatus').style.borderRadius = '8px';
        $('aiStatus').style.padding = '6px 10px';
        setTimeout(()=>{ $('aiStatus').style.background=''; $('aiStatus').style.border=''; $('aiStatus').style.borderRadius=''; $('aiStatus').style.padding=''; },3000);
        loadAiStatus();  // 刷新顶部状态条
      } else $('aiStatus').textContent = '❌ '+res.error;
    }).catch(()=>$('aiStatus').textContent='❌ 保存失败');
  }
function saveMirror(){
  const body = { mirror: {
    use_hf_mirror: $('mirUseMirror') ? ($('mirUseMirror').value === '1') : true,
    hf_endpoint: $('mirHf') ? $('mirHf').value.trim() : 'https://hf-mirror.com',
    ollama_proxy: $('mirProxy') ? $('mirProxy').value.trim() : '',
  } };
  fetch('/api/ai/config', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) })
    .then(r=>r.json()).then(res=>{ if(!res.ok) console.warn('镜像配置保存失败', res.error); }).catch(()=>{});
}
function testAI(ch){
  const btn = ch==='vision' ? $('testVis') : $('testTts');
  const old = btn.textContent;
  btn.textContent = '⏳ 测试中…'; btn.disabled = true;
  const setRes = (c, ok, msg) => { const el = $(c==='vision'?'visTestRes':'ttsTestRes'); if(el) el.textContent = (ok?'✅ ':'❌ ')+msg; };
  setRes(ch, false, '测试中…');
  fetch('/api/ai/test?ch='+ch).then(r=>r.json()).then(res=>{
    if(!res.ok){ setRes(ch,false,res.error||'失败'); return; }
    // the endpoint tests both; show the requested channel prominently, both updated
    for (const c of ['vision','tts']){
      if(res[c]) setRes(c, res[c].test_ok, res[c].message || '');
    }
  }).catch(()=>setRes(ch,false,'请求失败'));
  btn.textContent = old; btn.disabled = false;
}
function testLocal(){
  const btn = $('testLocal'); if(!btn) return;
  const old = btn.textContent;
  btn.textContent = '⏳ 测试中…'; btn.disabled = true;
  const el = $('localTestRes');
  if(el) el.textContent = '测试中…';
  fetch('/api/local/test').then(r=>r.json()).then(res=>{
    if(el) el.textContent = (res.test_ok ? '✅ ' : '❌ ') + (res.message || (res.test_ok ? '本地模型可达' : '不可达'));
  }).catch(()=>{ if(el) el.textContent = '❌ 请求失败'; });
  btn.textContent = old; btn.disabled = false;
}
// ---- Whisper 模型：选择 / 预下载 / 状态轮询 ----
function saveWhisper(){
  fetch('/api/ai/config', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ whisper: { model: $('whisperModel').value.trim() } }) })
    .then(()=>loadWhisperStatus()).catch(()=>{});
}
function downloadWhisper(){
  const btn = $('dlWhisper'); if(btn){ btn.textContent='⏳ 下载中…'; btn.disabled=true; }
  fetch('/api/whisper/download', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ model: $('whisperModel').value.trim() }) })
    .then(r=>r.json()).then(res=>{
      if($('whisperStatus')) $('whisperStatus').textContent = (res.ok?'⏳ ':'❌ ') + (res.message||'');
      loadWhisperStatus(); // 进入轮询
    }).catch(()=>{ if(btn){ btn.textContent='⬇ 下载/预载模型'; btn.disabled=false; } });
}
function loadWhisperStatus(){
  fetch('/api/whisper/status').then(r=>r.json()).then(res=>{
    const el = $('whisperStatus'); if(!el) return;
    const dir = res.models_dir || '';
    let s = '当前：' + res.selected + ' · ' + (res.ready ? '✅ 已就绪' : '⬜ 未下载');
    if(res.downloading) s = '⏳ 下载中（' + (res.download_model||'') + '）：' + (res.download_msg||'');
    else if(res.download_ok === false) s = '❌ 上次下载失败：' + (res.download_msg||'');
    el.textContent = s;
    // 模型目录与可用模型提示
    const cb = $('cmdWhisper'); if(cb){
      cb.textContent = '模型目录：' + dir + (res.available && res.available.length ? '　已缓存：' + res.available.join(', ') : '');
    }
    const btn = $('dlWhisper');
    if(btn){ btn.disabled = !!res.downloading; btn.textContent = res.downloading ? '⏳ 下载中…' : '⬇ 下载/预载模型'; }
    // 自动续轮询：下载中持续刷新
    if(res.downloading) setTimeout(loadWhisperStatus, 2000);
  }).catch(()=>{});
}

// ---- 本地视觉理解 VLM：启用保存 / 拉取 / 检测轮询 ----
function saveVlm(){
  fetch('/api/ai/config', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ vlm: {
      enabled: $('vlmEnabled').checked, mode: $('vlmMode').value.trim(),
      base_url: $('vlmBase').value.trim(), model: $('vlmModel').value.trim() } }) })
    .then(()=>loadVlmStatus()).catch(()=>{});
}
function testVlm(){
  const btn = $('testVlm'); if(btn){ btn.textContent='⏳ 检测中…'; btn.disabled=true; }
  fetch('/api/vlm/status').then(r=>r.json()).then(res=>{
    const el = $('vlmStatus'); if(el) el.textContent = (res.ready ? '✅ ' : '❌ ') + (res.message||'');
    if(btn){ btn.textContent='🧪 测试/检测'; btn.disabled=false; }
  }).catch(()=>{ if(btn){ btn.textContent='🧪 测试/检测'; btn.disabled=false; } });
}
function vlmUpdateBar(res){
  const bar = $('vlmPullBar'), fill = $('vlmPullFill'), pct = $('vlmPullPct');
  if(!bar) return;
  const p = Math.max(0, Math.min(100, Number(res.pull_pct) || 0));
  if(res.pulling){
    bar.style.display = 'block';
    if(fill) fill.style.width = p + '%';
    if(pct){ pct.style.display = 'block';
      pct.textContent = (res.pull_model || '') + '　' + p + '%　' + (res.pull_msg || '拉取中…'); }
  } else {
    bar.style.display = 'none';
    if(pct) pct.style.display = 'none';
  }
}
function loadVlmStatus(){
  fetch('/api/vlm/status').then(r=>r.json()).then(res=>{
    const el = $('vlmStatus'); if(!el) return;
    let s = '启用：' + (res.enabled ? '是' : '否') + ' · ' + (res.ready ? '✅ 模型已就绪' : '⬜ 未就绪');
    let isErr = false;
    if(res.ready){
      if(res.pulling) s += '（后台仍在拉取官方版，不影响使用）';
      else if(res.message) s += '　' + res.message;
    } else if(res.pulling){
      s = '⏳ 拉取中（' + (res.pull_model || '') + '）：' + (res.pull_msg || '拉取中…');
    } else if(res.pull_ok === false){
      s = '❌ 上次拉取失败：' + (res.pull_msg || '');
      isErr = true;
    } else if(res.message){
      s += '　' + res.message;
    }
    el.textContent = s;
    el.style.color = isErr ? '#c0392b' : '';
    vlmUpdateBar(res);
    const pbtn = $('pullVlm');
    if(pbtn){ pbtn.disabled = !!res.pulling; pbtn.textContent = res.pulling ? '⏳ 拉取中…' : '📥 拉取模型'; }
    const cb = $('cmdVlm'); if(cb) cb.textContent = '提示：终端执行  ollama pull ' + (res.model || 'qwen2.5vl:latest') + '  （也可点下方自动拉取）';
    if(res.pulling) setTimeout(loadVlmStatus, 2000);
    // 同步顶栏芯片
    const chip = $('aiVlm'); if(chip){ chip.className = 'aichip ' + (res.ready ? 'ok' : 'no');
      chip.textContent = res.ready ? '📷 视觉理解 已就绪' : '📷 视觉理解 未部署'; }
  }).catch(()=>{});
}
// ---- 本地模型（文字解说）：网页内一键拉取 + 状态轮询 ----
function pullLocalModel(){
  const btn = $('pullLocal'); if(btn){ btn.textContent='⏳ 拉取中…'; btn.disabled=true; }
  const bar = $('localPullBar'), pct = $('localPullPct');
  if(bar) bar.style.display = 'block';
  if($('localPullFill')) $('localPullFill').style.width = '0%';
  if(pct){ pct.style.display = 'block'; pct.textContent = ($('localModel').value.trim() || 'qwen2.5:latest') + '　0%　准备中…'; }
  fetch('/api/local/pull', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ model: $('localModel').value.trim() }) })
    .then(r=>r.json()).then(res=>{
      if(!res.ok) $('localPullRes').textContent = '❌ ' + (res.message || res.error || '拉取未启动');
      loadLocalStatus();
    }).catch(()=>{ if(btn){ btn.textContent='📥 拉取模型'; btn.disabled=false; } });
}
function loadLocalStatus(){
  fetch('/api/local/status').then(r=>r.json()).then(res=>{
    const el = $('localPullRes'); if(!el) return;
    let s = '启用：' + (res.enabled ? '是' : '否') + ' · ' + (res.ready ? '✅ 模型已就绪' : '⬜ 未就绪');
    let isErr = false;
    // 已就绪优先显示 ✅；后台拉取仅作次要提示，不再盖掉「已就绪」
    if(res.ready){
      if(res.pulling) s += '（后台仍在拉取官方版，不影响使用）';
      else if(res.message) s += '　' + res.message;
    } else if(res.pulling){
      s = '⏳ 拉取中（' + (res.pull_model||'') + '）：' + (res.pull_msg||'拉取中…');
    } else if(res.pull_ok === false){
      s = '❌ 上次拉取失败：' + (res.pull_msg||'');
      isErr = true;
    } else if(res.message){
      s += '　' + res.message;
    }
    el.textContent = s;
    el.style.color = isErr ? '#c0392b' : '';
    const bar = $('localPullBar'), fill = $('localPullFill'), pct = $('localPullPct');
    if(bar){
      const p = Math.max(0, Math.min(100, Number(res.pull_pct) || 0));
      if(res.pulling){
        bar.style.display = 'block';
        if(fill) fill.style.width = p + '%';
        if(pct){ pct.style.display = 'block'; pct.textContent = (res.pull_model||'') + '　' + p + '%　' + (res.pull_msg||'拉取中…'); }
      } else {
        bar.style.display = 'none';
        if(pct) pct.style.display = 'none';
      }
    }
    const btn = $('pullLocal');
    if(btn){ btn.disabled = !!res.pulling; btn.textContent = res.pulling ? '⏳ 拉取中…' : '📥 拉取模型'; }
    if(res.pulling) setTimeout(loadLocalStatus, 2000);
    const chip = $('aiLocal'); if(chip){ chip.className = 'aichip ' + (res.ready ? 'ok' : 'no');
      chip.textContent = res.ready ? '🖥 本地模型 已就绪' : '🖥 本地模型 未部署'; }
  }).catch(()=>{});
}
// ---- 本地视觉理解 VLM：网页内一键拉取（按钮此前未接，这里补上）----
function pullVlm(){
  const btn = $('pullVlm'); if(btn){ btn.textContent='⏳ 拉取中…'; btn.disabled=true; }
  const bar = $('vlmPullBar'), pct = $('vlmPullPct');
  if(bar) bar.style.display = 'block';
  if($('vlmPullFill')) $('vlmPullFill').style.width = '0%';
  if(pct){ pct.style.display = 'block'; pct.textContent = ($('vlmModel').value.trim() || 'qwen2.5vl:latest') + '　0%　准备中…'; }
  fetch('/api/vlm/pull', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ model: $('vlmModel').value.trim() }) })
    .then(r=>r.json()).then(res=>{
      if(!res.ok) $('vlmStatus').textContent = '❌ ' + (res.message || res.error || '拉取未启动');
      loadVlmStatus();
    }).catch(()=>{ if(btn){ btn.textContent='📥 拉取模型'; btn.disabled=false; } });
}


function copyText(id){
  const el = $(id); if(!el) return;
  const t = el.textContent || el.innerText || '';
  if(navigator.clipboard) navigator.clipboard.writeText(t).then(()=>{
    const b = el.parentElement && el.parentElement.querySelector('.cp');
    if(b){ const o = b.textContent; b.textContent = '✅ 已复制'; setTimeout(()=>b.textContent = o, 1200); }
  }).catch(()=>{});
}
// 自动探测可用的 Ollama 安装包镜像（后端并发探测，过滤证书不安全/打不开的），渲染可用列表并标推荐。
function scanOllamaMirrors(){
  const box = $('ollamaMirrors'), btn = $('btnScanMirror');
  if(box) box.innerHTML = '<span class="hint">⏳ 正在探测可用镜像（约 6 秒）…</span>';
  if(btn){ btn.disabled = true; btn.textContent = '🔍 检测中…'; }
  fetch('/api/mirror/scan', {method:'POST'}).then(r=>r.json()).then(res=>{
    if(!res.ok || !res.result){ if(box) box.innerHTML = '<span class="hint">❌ 检测失败：'+(res.error||'未知错误')+'</span>'; return; }
    const list = res.result.mirrors || [], best = res.result.best;
    if(!list.length){ if(box) box.innerHTML = '<span class="hint">未配置镜像候选。</span>'; return; }
    try { localStorage.setItem('ollamaBest', best || ''); } catch(e){}
    const okList = list.filter(m=>m.ok);
    const official = '<div class="hint" style="margin-top:8px;">镜像若仍打不开，可直接去官网下载：<a class="open" href="https://ollama.com/download" target="_blank">https://ollama.com/download</a>（下 Windows 版 OllamaSetup.exe）。</div>';
    if(!okList.length){
      if(box) box.innerHTML = '<span class="hint">⚠️ 当前网络下所有候选镜像都打不开（可能无外网/被墙）。<b>请直接去官网下载</b>：<a class="open" href="https://ollama.com/download" target="_blank">https://ollama.com/download</a>（下 Windows 版 OllamaSetup.exe），或稍后重试点「🔍 重新检测可用镜像」。</span>';
      return;
    }
    if(box) box.innerHTML = okList.map((m,i)=>{
      const rec = (m.base === best) ? ' recommend' : '';
      const tag = (m.base === best) ? '<span class="badge ok">✅ 推荐</span>' : '<span class="badge ok">可用</span>';
      const cid = 'mir' + i;
      return `<div class="mirroritem${rec}">
        ${tag}
        <code id="${cid}">${m.url}</code>
        <a class="open" target="_blank" href="${m.url}">↗ 打开</a>
        <button class="btn mini ghost cp" onclick="copyText('${cid}')">📋 复制</button>
        <div class="note">${m.note||''}</div>
      </div>`;
    }).join('') + official;
  }).catch(()=>{ if(box) box.innerHTML = '<span class="hint">❌ 检测请求失败，请重试。</span>'; })
    .finally(()=>{ if(btn){ btn.disabled = false; btn.textContent = '🔍 重新检测可用镜像'; } });
}
function loadHistory(){
  fetch('/api/history').then(r=>r.json()).then(res=>{
    const box = $('historyList');
    if(!res.ok || !res.history || !res.history.length){ box.innerHTML='<div class="hint">还没有生成记录。</div>'; return; }
    box.innerHTML = '';
    res.history.forEach(h => {
      const d = document.createElement('div');
      d.className = 'item';
      const secs = h.duration || 0;
      const tag = [h.music?'🎵':'', h.voice?'🗣️':'', (h.w||'')+'x'+(h.h||'')].filter(Boolean).join(' ');
      d.innerHTML = `<div class="meta"><div class="name">🕘 ${h.time||''} · ${secs}s ${tag}</div>
        <div class="kind">${(h.captions&&h.captions.length)?'文案:'+h.captions.join(' / '):''}</div></div>
        <a class="btn mini ghost" href="/media/${h.file}" download="spring-${h.time||''}.mp4">⬇ 下载</a>
        <button class="btn mini del">🗑 删除</button>`;
      d.querySelector('a').addEventListener('click', (e)=>{ e.preventDefault(); const a=e.currentTarget; a.href='/media/'+h.file+'?t='+Date.now(); a.click(); });
      d.querySelector('button.del').addEventListener('click', () => deleteHistory(h.file));
      box.appendChild(d);
    });
  }).catch(()=>{});
}
function deleteHistory(file){
  if(!confirm('确定删除这条生成记录及其成片文件？')) return;
  fetch('/api/history/delete', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({file})}).then(r=>r.json()).then(res=>{
    if(res.ok){ loadHistory(); } else { alert('删除失败：' + (res.error||'')); }
  }).catch(()=>alert('删除失败'));
}
function clearHistory(){
  if(!confirm('确定清空全部生成记录与成片文件？此操作不可恢复。')) return;
  fetch('/api/history/clear', {method:'POST'}).then(r=>r.json()).then(res=>{
    if(res.ok){ loadHistory(); } else { alert('清空失败'); }
  }).catch(()=>alert('清空失败'));
}
loadAIConfig();
loadHistory();
loadAiStatus();
scanOllamaMirrors();   // 页面加载即自动探测可用 Ollama 安装镜像，免人工替换
loadWhisperStatus();
loadVlmStatus();

function clearEmpty(){ $('emptyHint').style.display = ITEMS.length ? 'none' : ''; }

function isVideo(name){ return /\.(mp4|mov|webm|avi|mkv|m4v)$/i.test(name); }

function handle(file){
  if (!(file.type.startsWith('image/') || file.type.startsWith('video/') || isVideo(file.name))) return;
  const id = 'it' + Date.now() + Math.random().toString(36).slice(2,6);
  const dur = Math.max(1, parseInt($('defDur').value) || 3);
  const it = { id, name:file.name, kind: isVideo(file.name) ? 'video' : 'image', dur, url:URL.createObjectURL(file), file };
  ITEMS.push(it);
  render();
}

function render(){
  const box = $('items'); box.innerHTML = '';
  clearEmpty();
  ITEMS.forEach((it, i) => {
    const d = document.createElement('div'); d.className = 'item';
    const kindTxt = it.kind==='video' ? '🎬 视频' : '🖼️ 图片';
    d.innerHTML = `
      <img class="thumb" src="${it.url}" alt="">
      <div class="meta"><div class="name">${it.name}</div><div class="kind">${kindTxt}</div></div>
      <span class="lbl">时长</span><input type="number" value="${it.dur}" min="1" max="120" data-i="${i}">
      <button class="btn mini ghost" data-up="${i}" title="上移">↑</button>
      <button class="btn mini ghost" data-dn="${i}" title="下移">↓</button>
      <button class="btn mini danger" data-del="${i}">删除</button>`;
    box.appendChild(d);
  });
  box.querySelectorAll('input[type=number]').forEach(inp => inp.onchange = e => { ITEMS[+e.target.dataset.i].dur = Math.max(1, parseInt(e.target.value)||1); });
  box.querySelectorAll('[data-up]').forEach(b => b.onclick = () => move(+b.dataset.up, -1));
  box.querySelectorAll('[data-dn]').forEach(b => b.onclick = () => move(+b.dataset.dn, 1));
  box.querySelectorAll('[data-del]').forEach(b => b.onclick = () => { ITEMS.splice(+b.dataset.del, 1); render(); });
}
function move(i, dir){
  const j = i + dir; if (j < 0 || j >= ITEMS.length) return;
  [ITEMS[i], ITEMS[j]] = [ITEMS[j], ITEMS[i]]; render();
}

function toB64(u8){
  // chunked base64 to avoid the "maximum call stack" crash on large files
  const CHUNK = 0x8000; let bin = '';
  for (let i = 0; i < u8.length; i += CHUNK){
    bin += String.fromCharCode.apply(null, u8.subarray(i, i + CHUNK));
  }
  return btoa(bin);
}

let _tickTimer = null;
let _currentRunid = null;
function setBar(p){ const b=$('bar').querySelector('i'); $('bar').style.display='block'; b.style.width=Math.min(100,Math.max(1,p))+'%'; }
function stopBar(){ clearInterval(_tickTimer); $('bar').style.display='none'; }

// ---- AI 配置就绪状态 + 生成前置引导（避免静默用免费模板） ----
let _aiStatus = null;
let _pendingGen = null;
let _pfTarget = 'cloud';  // preflight 弹窗「去配置」跳转目标
function goPreflightConfig(){
  closePreflight();
  jumpToAISection(_pfTarget);
}
function loadAiStatus(){
  fetch('/api/ai_status').then(r=>r.json()).then(s=>{
    _aiStatus = s;
    const chat = $('aiChat'), vis = $('aiVision');
    if (chat){ chat.className = 'aichip ' + (s.chat ? 'ok' : 'no');
      chat.textContent = s.chat ? '🤖 真AI(LLM) 已配置' : '🤖 真AI(LLM) 未配置'; }
    if (vis){ vis.className = 'aichip ' + (s.vision ? 'ok' : 'no');
      vis.textContent = s.vision ? '👁 画面描述 已配置' : '👁 画面描述 未配置'; }
    const loc = $('aiLocal');
    if (loc){ loc.className = 'aichip ' + (s.local ? 'ok' : 'no');
      loc.textContent = s.local ? '🖥 本地模型 已就绪' : '🖥 本地模型 未部署'; }
    const vlm = $('aiVlm');
    if (vlm){ vlm.className = 'aichip ' + (s.vlm_ready ? 'ok' : 'no');
      vlm.textContent = s.vlm_ready ? '📷 视觉理解 已就绪' : (s.vlm_enabled ? '📷 视觉理解 未就绪' : '📷 视觉理解 未部署'); }
    const wsp = $('aiWhisper');
    if (wsp){ wsp.className = 'aichip ' + (s.whisper_ready ? 'ok' : 'no');
      wsp.textContent = s.whisper_ready ? ('🎙 Whisper ' + s.whisper_model) : ('🎙 Whisper ' + s.whisper_model + ' 未下载'); }
    renderNarrGuide(s);
    loadLocalStatus(); loadVlmStatus();
  }).catch(()=>{});
}

// 解说模型引导：提示 qwen2.5vl 的局限 + 引导部署「文字模型」写剧情解说
function renderNarrGuide(s){
  const ng = $('narrGuide');
  if (!ng) return;
  const g = s.narr_guide || {};
  const esc = (x) => String(x||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  if (g.local_ok){
    ng.className = 'narr-guide ok';
    ng.innerHTML = '✅ 解说稿由本地<b>文字模型</b> <code>'+esc(g.local_model)+'</code> 生成（剧情解说质量最佳）。'
      + (g.weak_vlm ? ' <span class="dim">视觉模型 '+esc(g.vlm_model)+' 只负责看懂画面。</span>' : '');
  } else if (g.weak_vlm || (s.vlm_enabled && !s.chat)){
    ng.className = 'narr-guide warn';
    ng.innerHTML = '⚠️ 当前视觉模型 <b>'+esc(g.vlm_model||'?')+'</b> 是「看图模型」：只负责识别画面，<b>写剧情解说词很弱</b>。'
      + '想要《赌圣》式剧情解说，请部署一个<b>文字模型</b>（写稿主力），并到「🤖 AI 配置 → ③ 本地模型」把模型填成它：'
      + ' <code>'+(g.recommend||'ollama pull qwen2.5:14b')+'</code>'
      + (g.installed && g.installed.length ? ' <span class="dim">已装：'+g.installed.join('、')+'</span>' : '');
  } else if (s.vlm_ready || s.local){
    ng.className = 'narr-guide';
    ng.innerHTML = 'ℹ️ 解说稿生成时自动使用可用的本地模型（视觉模型看懂画面 + 文字模型写剧情解说）。';
  } else {
    ng.className = 'narr-guide';
    ng.innerHTML = '💡 解说需要本地模型：视觉模型看懂画面、文字模型写剧情解说。见下方「🤖 AI 配置 → 路径 A」一键部署。';
  }
}
// task: 'narrate'|'movie'|'instruct'|'build'|'beatcut'；返回 Promise<true> 表示允许继续
function preflight(task){
  return new Promise(resolve => {
    const decide = (s) => {
      _aiStatus = s;
      let missing = null, explicit = false;
      const narSel = $('narMode'), movSel = $('movieMode'), eco = $('eco'), aiCap = $('aiCap');
      if (task === 'narrate' && narSel && narSel.value === 'ai' && !s.chat){ missing = '真AI 解说(LLM)'; explicit = true; }
      else if (task === 'movie' && movSel && movSel.value === 'ai' && !s.chat){ missing = '真AI 解说(LLM)'; explicit = true; }
      else if (task === 'build' && eco && !eco.checked && !s.any_ai){ missing = '真AI(DeepSeek/MiMo)'; explicit = true; }
      else if (task === 'build' && aiCap && aiCap.checked && !s.vision){ missing = '画面描述(Vision)'; explicit = true; }
      else if (task === 'instruct' && !s.chat){ missing = '真AI 解说(LLM)'; explicit = false; }
      if (missing){
        _pendingGen = resolve;  // 点「仍用免费生成」时 resolve(true)
        // 根据缺少的配置类型决定「去配置」跳转到云端还是本地
        _pfTarget = (explicit && (missing.indexOf('LLM') >= 0 || missing.indexOf('Vision') >= 0 || missing.indexOf('真AI') >= 0)) ? 'cloud' : 'local';
        $('pfTitle').textContent = (explicit ? '已选真AI 但未配置 ' : '尚未配置 ') + missing + ' API';
        $('pfMsg').innerHTML = explicit
          ? '你选择了「<b>真AI</b>」模式，但 <b>' + missing + ' API</b> 还没配置，无法生成。<br>请先去「🤖 AI 配置」填好 Key，或把模式切回「省流(免费)」再生成。'
          : '你还没有配置 <b>' + missing + ' API</b>。直接点「生成」会用 <b>本地离线模式</b>：本地 faster-whisper 识别真实台词 + SAPI 免费配音（不调任何付费接口，有显卡会用 GPU 加速）。<br>想让免费模式解说词更聪明：在「🤖 AI 配置 → ③ 本地模型」部署本机 Ollama+qwen 离线改写；或在「⑤ 本地视觉理解 VLM」部署 qwen2.5vl，省流模式会<b>逐段看画面</b>生成真解说（仍不花一分钱）；或点「仍用本地离线生成」继续。';
        $('preflight').style.display = 'flex';
      } else {
        resolve(true);
      }
    };
    if (_aiStatus) decide(_aiStatus);
    else fetch('/api/ai_status').then(r=>r.json()).then(decide).catch(()=>resolve(true));
  });
}
function closePreflight(){ $('preflight').style.display = 'none'; }
function continueGen(){ const f = _pendingGen; _pendingGen = null; closePreflight(); if (f) f(true); }
function setModeBadge(id, mode){
  const el = $(id); if (!el) return;
  if (mode === 'ai'){ el.className = 'modebadge ai'; el.textContent = '🤖 真AI 生成'; }
  else if (mode === 'vlm'){ el.className = 'modebadge vlm'; el.textContent = '📷 本地视觉理解'; }
  else if (mode === 'local'){ el.className = 'modebadge local'; el.textContent = '🖥 本地模型'; }
  else if (mode === 'free'){ el.className = 'modebadge free'; el.textContent = '🖥 本地离线'; }
  else { el.className = 'modebadge'; el.textContent = ''; }
}

// ---- 全局进度条 + 悬浮预览（P2：进度/预览 UI 增强） ----
let _gStart = 0;
function gStart(label){
  _gStart = Date.now();
  $('gprogLabel').textContent = label || '处理中…';
  $('gprogPct').textContent = '0%';
  $('gprogPhase').textContent = '';
  $('gprogFill').style.width = '3%';
  $('gprog').classList.add('show');
}
function gSet(pct, phase){
  pct = Math.min(99, Math.max(1, pct || 0));
  $('gprogFill').style.width = pct + '%';
  $('gprogPct').textContent = Math.floor(pct) + '%';
  const el = (Date.now() - _gStart) / 1000;
  let txt = phase || '';
  if (pct > 3){
    const eta = Math.max(0, el / pct * (100 - pct));
    txt += (txt ? ' · ' : '') + '预计还需 ' + Math.ceil(eta) + 's';
  }
  $('gprogPhase').textContent = txt;
}
function gDone(){
  $('gprogFill').style.width = '100%';
  $('gprogPct').textContent = '100%';
  $('gprogPhase').textContent = '完成 ✓';
  setTimeout(() => $('gprog').classList.remove('show'), 1500);
}
function gErr(msg){
  $('gprogFill').style.width = '100%';
  $('gprogLabel').textContent = '❌ ' + (msg || '失败');
  $('gprogPhase').textContent = '';
  setTimeout(() => $('gprog').classList.remove('show'), 2800);
}
function escapeHtml(s){
  return (s || '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function renderPartial(id, partial){
  const box = $(id);
  if (!box) return;
  if (!partial || !partial.files || !partial.files.length){
    box.style.display = 'none'; box.innerHTML = ''; return;
  }
  let html = '<div class="ph">⚠️ 任务中断，但已生成以下部分产物（可下载 / 复制后继续用）</div>';
  if (partial.text){
    html += '<div class="ptext"><pre>' + escapeHtml(partial.text.slice(0, 4000)) + '</pre>'
          + '<button class="btn mini ghost pcopy">📋 复制全文</button></div>';
  }
  if (partial.best_video){
    html += '<video class="pvideo" src="' + partial.best_video + '?t=' + Date.now() + '" controls playsinline></video>';
  }
  html += '<div class="plist">';
  (partial.files || []).forEach(f => {
    const sz = (f.size / 1024).toFixed(0) + ' KB';
    const tag = { video:'🎬', audio:'🎵', subtitle:'📝', text:'📄', file:'📎' }[f.kind] || '📎';
    html += '<div class="pitem"><span class="ptag">' + tag + '</span>'
          + '<a href="' + f.url + '" download="' + escapeHtml(f.name) + '" target="_blank">' + escapeHtml(f.name) + '</a>'
          + '<span class="psz">' + sz + '</span></div>';
  });
  html += '</div>';
  box.innerHTML = html;
  box.style.display = 'block';
  const cp = box.querySelector('.pcopy');
  if (cp) cp.addEventListener('click', () => {
    if (navigator.clipboard) navigator.clipboard.writeText(partial.text).catch(() => {});
  });
}
function gPreview(file, name){
  if (!file) return;
  const dock = $('previewDock');
  const url = '/media/' + file + '?t=' + Date.now();
  const card = document.createElement('div');
  card.className = 'pvcard';
  card.innerHTML =
    '<div class="pvhead"><span>✅ ' + (name || '已生成') + '</span><button class="pvclose">✕</button></div>' +
    '<video src="' + url + '" controls playsinline></video>' +
    '<div class="pvfoot"><a class="btn mini ghost" href="' + url + '" download="' + (name || 'video.mp4') + '">💾 保存</a></div>';
  card.querySelector('.pvclose').addEventListener('click', () => { card.remove(); if (!dock.children.length) dock.classList.remove('show'); });
  dock.appendChild(card);
  dock.classList.add('show');
  while (dock.children.length > 3) dock.firstElementChild.remove();
}

// ---- 顶部步骤导航：按执行步骤切换页面（卡片按 data-step 分组显示/隐藏） ----
const STEP_CARDS = {
  start: ['guideCard', 'instructCard'],
  upload: ['drop'],
  music: ['musicCard', 'libCard'],
  beatcut: ['beatcutCard'],
  narrate: ['narCard', 'movieCard'],
  ai: ['aiCard'],
  output: ['timelineCard', 'outCard'],
  build: ['buildCard'],
  history: ['histCard'],
};
function showStep(step){
  if (!STEP_CARDS[step]) return;
  Object.entries(STEP_CARDS).forEach(([s, ids]) => {
    const on = (s === step);
    ids.forEach(id => { const el = document.getElementById(id); if (el) el.classList.toggle('active', on); });
  });
  document.querySelectorAll('.stepbtn').forEach(b => b.classList.toggle('active', b.dataset.step === step));
  try { localStorage.setItem('springStudio.lastStep', step); } catch(e){}
}
function goStep(targetId){
  const el = document.getElementById(targetId);
  if (!el) return;
  const step = el.getAttribute('data-step');
  if (step) showStep(step);
  setTimeout(() => { el.scrollIntoView({ behavior: 'smooth', block: 'start' }); }, 60);
}
(function(){
  let init = 'start';
  try { init = localStorage.getItem('springStudio.lastStep') || 'start'; } catch(e){}
  if (!STEP_CARDS[init]) init = 'start';
  showStep(init);
  document.querySelectorAll('.stepbtn').forEach(b => b.addEventListener('click', () => showStep(b.dataset.step)));
  const st = document.getElementById('scrollTopBtn');
  const onScroll = () => { if (st) st.classList.toggle('show', window.scrollY > 420); };
  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();
})();
function scrollTop2(){ window.scrollTo({ top: 0, behavior: 'smooth' }); }

// ---- 模式选择 → 一键跳转到对应配置区 ----
function jumpToAISection(section){
  showStep('ai');
  const card = $('aiCard');
  if (!card) return;
  card.scrollIntoView({ behavior: 'smooth', block: 'start' });
  setTimeout(() => {
    const localD = $('aiLocalDetails');
    const cloudD = $('aiCloudDetails');
    if (section === 'cloud') {
      if (cloudD) cloudD.open = true;
      if (localD) localD.open = false;
    } else {
      // local / vlm / whisper / mirror 都在本地离线模型折叠区里
      if (localD) localD.open = true;
      if (cloudD) cloudD.open = false;
    }
  }, 350);
}
function scrollToAIConfig(modeSelectId){
  const sel = $(modeSelectId);
  const val = sel ? sel.value : 'eco';
  jumpToAISection(val === 'ai' ? 'cloud' : 'local');
}
function onModeChange(modeSelectId){
  // 切换模式时自动跳转到对应配置区，并更新提示
  updateModeHint(modeSelectId);
  scrollToAIConfig(modeSelectId);
}

function setRes(res, name){ $('res').value = res; $('status').textContent = '已设为：' + name + ' (' + res + ')'; }
function cancelRun(){
  if (_currentRunid){
    fetch('/api/cancel', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({runid:_currentRunid}) }).then(r=>r.json()).then(()=>{
      const el = $('status'); if (el) el.textContent='⏹ 正在取消…';
    }).catch(()=>{});
  }
}
function cancelBuild(){ cancelRun(); }

// ---- 🎯 智能强卡点 ----
let BC_VIDEO = null;
let _bcRunid = null;
(function(){
  const bd = $('bcDrop'), bi = $('bcInput');
  bd.addEventListener('click', () => bi.click());
  bi.addEventListener('change', e => { if(bi.files.length) setBCVideo(bi.files[0]); bi.value=''; });
  bd.addEventListener('dragover', e => { e.preventDefault(); bd.classList.add('over'); });
  bd.addEventListener('dragleave', () => bd.classList.remove('over'));
  bd.addEventListener('drop', e => { e.preventDefault(); bd.classList.remove('over'); if(e.dataTransfer.files.length) setBCVideo(e.dataTransfer.files[0]); });
})();
function setBCVideo(file){
  if(!file.type.startsWith('video/')){ $('bcInfo').textContent='❌ 只支持视频'; return; }
  BC_VIDEO = file;
  $('bcDrop').textContent = '🎬 已选：' + file.name;
  $('bcInfo').textContent = '已选视频，请选择背景音乐后点「一键强卡点」。';
}
(function(){
  const sen=$('bcSensitivity'), txt=$('bcSensitivityText');
  if(sen && txt){
    sen.addEventListener('input', ()=>{ txt.textContent = sen.value; });
  }
})();
async function buildBeatCut(){
  if(!BC_VIDEO){ $('bcStatus').innerHTML='❌ 请先拖入视频到上方区域'; $('bcDrop').classList.add('shake'); setTimeout(()=>$('bcDrop').classList.remove('shake'),600); return; }
  if(!MUSIC){ $('bcStatus').innerHTML='❌ 请先选择背景音乐 — <a href="javascript:void(0)" onclick="goStep(\'musicCard\')" style="color:#1d4ed8;text-decoration:underline;">🎵 去选音乐</a> 或 <a href="javascript:void(0)" onclick="goStep(\'libCard\')" style="color:#1d4ed8;text-decoration:underline;">🔎 免费曲库</a>';
    const h=$('bcMusicHint'); if(h){ h.style.background='#fef2f2'; h.style.borderColor='#fca5a5'; h.style.color='#991b1b'; setTimeout(()=>updateMusicHint(),1500); } return; }
  const go=$('bcQuickBtn')||$('bcGo'); go.disabled=true; $('bcResult').style.display='none';
  const syncSt=$('bcSyncStatus'); if(syncSt){ syncSt.style.display='none'; syncSt.style.background=''; syncSt.textContent=''; }
  $('bcStatus').textContent='上传视频…';
  gStart('⚡ 智能强卡点');
  const params={w:1280,h:720,fps:30,sceneTh:0.30,maxCuts:30, strength: $('bcStrength').value};
  if($('bcKeepAudio')){ params.keepAudio = $('bcKeepAudio').checked; }
  // 转场仅在智能强卡点（非节拍同步）生效
  params.transition = 'none';
  if($('bcTransition') && !($('bcBeatSync') && $('bcBeatSync').checked)){
    params.transition = $('bcTransition').value;
    params.transDur = parseFloat(($('bcTransDur')||{}).value) || 0.2;
  }
  if($('bcBeatSync') && $('bcBeatSync').checked){
    params.beatSync = true;
    params.beat_sensitivity = parseFloat($('bcSensitivity').value) || 0.5;
    params.min_clip_dur = parseFloat($('bcMinClip').value) || 0.6;
  }
  const body = { video:{name:BC_VIDEO.name, data: toB64(new Uint8Array(await BC_VIDEO.arrayBuffer()))}, music:null, params };
  if(MUSIC.catalogId){ body.music={source:'catalog', catalogId:MUSIC.catalogId}; }
  else { body.music={name:MUSIC.name, data: toB64(new Uint8Array(await MUSIC.file.arrayBuffer()))}; }
  try{
    const r=await fetch('/api/beatcut',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const out=await r.json();
    if(!out.ok) throw new Error(out.error||'失败');
    _bcRunid=out.runid;
    await pollBeatCut(_bcRunid);
  }catch(e){ $('bcStatus').textContent='❌ '+e.message; }
  go.disabled=false;
}
function pollBeatCut(runid){
  return new Promise(resolve=>{
    const iv=setInterval(()=>{
      fetch('/api/progress?run='+runid).then(r=>r.json()).then(p=>{
        const b=$('bcBar').querySelector('i');
        $('bcBar').style.display='block';
        if(p.pct) b.style.width=Math.min(100,p.pct)+'%';
        gSet(p.pct, p.phase);
        if(p.done){
          clearInterval(iv); $('bcBar').style.display='none';
          if(p.error){ renderPartial('bcPartial', p.partial); $('bcStatus').textContent='❌ '+p.error; gErr(p.error); resolve(); return; }
          $('bcStatus').textContent='✅ 完成'; gDone();
          $('bcResult').style.display='block';
          $('bcPlayer').src='/media/'+p.file+'?t='+Date.now();
          setModeBadge('bcMode', p.mode);
          gPreview(p.file, '强卡点短片');
          $('bcDl').href='/media/'+p.file;
          const d=p.diag||{};
          let txt;
          if(d.mode==='beat_sync'){
            txt='节拍点 '+ (d.beat_num||0) +' 个 · 可用素材片段 '+ (d.clip_num||0) +' 个';
            const st=$('bcSyncStatus');
            if(st){
              st.style.display='block';
              if(d.warning){ st.style.background='#fff2cc'; st.style.color='#92400e'; st.textContent='⚠️ '+d.warning; alert('⚠️ '+d.warning); }
              else { st.style.background='#e8f4ff'; st.style.color='#1e3a8a'; st.textContent='🎵 节拍同步完成：无素材不足告警。'; }
            }
          } else {
            txt='切换点 '+ (d.segments||0) +' 个 · 场景切 '+ (d.scene_cuts||[]).length +' · 动作/停顿 '+ (d.motion_cuts||[]).length +' · 镜头/色调 '+ (d.visual_cuts||[]).length +' · 强拍 '+ (d.strong_beats||[]).length + ' · 强度 '+(d.strength||'standard');
            if(d.transition && d.transition!=='none'){ txt += ' · 转场 '+d.transition; }
            if(d.keep_audio){ txt += ' · 保留原声'; }
            if(d.timeline){ txt += ' · 时间线 [' + d.timeline.map(t=>t.toFixed(1)).join(', ') + ']s'; }
          }
          $('bcDiag').textContent = txt;
          resolve(); return;
        }
        $('bcStatus').textContent = (p.phase||'分析中')+'… '+(p.pct||0)+'%';
      }).catch(()=>{});
    },400);
    setTimeout(()=>{ clearInterval(iv); resolve(); }, 600000);
  });
}

// ---- 🎬 电影解说 ----
let NAR_VIDEO = null;
(function(){
  const nd = $('narDrop'), ni = $('narInput');
  nd.addEventListener('click', () => ni.click());
  ni.addEventListener('change', e => { if(ni.files.length) setNarVideo(ni.files[0]); ni.value=''; });
  nd.addEventListener('dragover', e => { e.preventDefault(); nd.classList.add('over'); });
  nd.addEventListener('dragleave', () => nd.classList.remove('over'));
  nd.addEventListener('drop', e => { e.preventDefault(); nd.classList.remove('over'); if(e.dataTransfer.files.length) setNarVideo(e.dataTransfer.files[0]); });
})();
function setNarVideo(file){
  if(!file.type.startsWith('video/')){ $('narInfo').textContent='❌ 只支持视频'; return; }
  NAR_VIDEO = file;
  $('narDrop').textContent = '🎞️ 已选：' + file.name;
  $('narInfo').textContent = '已选视频，点「生成解说」开始（省流=免费配音）。';
}
async function buildNarrate(){
  if(!NAR_VIDEO){ $('narStatus').textContent='❌ 请先拖入视频到上方区域'; $('narDrop').classList.add('shake'); setTimeout(()=>$('narDrop').classList.remove('shake'),600); return; }
  const ok = await preflight('narrate'); if(!ok) return;
  const go=$('narQuickBtn')||$('narGo'); go.disabled=true; $('narResult').style.display='none';
  $('narStatus').textContent='上传视频…';
  gStart('🎬 生成短片解说');
  const mode=$('narMode').value;
  const body = { video:{name:NAR_VIDEO.name, data: toB64(new Uint8Array(await NAR_VIDEO.arrayBuffer()))},
                 params:{economy: mode!=='ai', maxSeg: parseFloat($('narMaxSeg').value)||25, w:1280, h:720, fps:30,
                         name: NAR_VIDEO.name, theme: ($('narTheme') ? $('narTheme').value.trim() : ''),
                         req: ($('narReq') ? $('narReq').value.trim() : '')} };
  if($('narBgm').checked && MUSIC){
    if(MUSIC.catalogId){ body.music={source:'catalog', catalogId:MUSIC.catalogId}; }
    else { body.music={name:MUSIC.name, data: toB64(new Uint8Array(await MUSIC.file.arrayBuffer()))}; }
  }
  try{
    const r=await fetch('/api/narrate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const out=await r.json();
    if(!out.ok) throw new Error(out.error||'失败');
    await pollNarrate(out.runid);
  }catch(e){ $('narStatus').textContent='❌ '+e.message; }
  go.disabled=false;
}
function pollNarrate(runid){
  return new Promise(resolve=>{
    _currentRunid = runid; const cb=$('narCancel'); if(cb) cb.style.display='';
    const iv=setInterval(()=>{
      fetch('/api/progress?run='+runid).then(r=>r.json()).then(p=>{
        const b=$('narBar').querySelector('i');
        $('narBar').style.display='block';
        if(p.pct) b.style.width=Math.min(100,p.pct)+'%';
        gSet(p.pct, p.phase);
        if(p.done){
          clearInterval(iv); $('narBar').style.display='none';
          _currentRunid=null; if(cb) cb.style.display='none';
          if(p.error){ renderPartial('narPartial', p.partial); $('narStatus').textContent='❌ '+p.error; gErr(p.error); resolve(); return; }
          $('narStatus').textContent='✅ 完成'; gDone();
          $('narResult').style.display='block';
          $('narPlayer').src='/media/'+p.file+'?t='+Date.now();
          setModeBadge('narBadge', p.mode);
          gPreview(p.file, '电影解说');
          $('narDl').href='/media/'+p.file;
          const d=p.diag||{};
          let txt='分段 '+(d.segments||0)+' · 台词 '+(d.asr_lines||0)+' 条 · 配音 '+(d.voice_clips||0)+' 段';
          if(d.narration){ txt += ' · 解说：' + d.narration.join(' / '); }
          $('narDiag').textContent = txt;
          resolve(); return;
        }
        $('narStatus').textContent = (p.phase||'处理中')+'… '+(p.pct||0)+'%';
      }).catch(()=>{});
    },400);
    setTimeout(()=>{ clearInterval(iv); _currentRunid=null; if(cb) cb.style.display='none'; resolve(); }, 900000);
  });
}

// ---- 🎬 电影解说（按片名·联网，Phase 3） ----
let MOVIE_VIDEO = null;
(function(){
  const md = $('movieDrop'), mi = $('movieInput');
  md.addEventListener('click', () => mi.click());
  mi.addEventListener('change', e => { if(mi.files.length) setMovieVideo(mi.files[0]); mi.value=''; });
  md.addEventListener('dragover', e => { e.preventDefault(); md.classList.add('over'); });
  md.addEventListener('dragleave', () => md.classList.remove('over'));
  md.addEventListener('drop', e => { e.preventDefault(); md.classList.remove('over'); if(e.dataTransfer.files.length) setMovieVideo(e.dataTransfer.files[0]); });
})();
function setMovieVideo(file){
  if(!file.type.startsWith('video/')){ $('movieInfo').textContent='❌ 只支持视频'; return; }
  MOVIE_VIDEO = file;
  $('movieDrop').textContent = '🎞️ 已选：' + file.name;
  $('movieInfo').textContent = '已选视频，点「生成（联网）解说」开始（联网搜索剧情并自动对齐）。';
}
function fillMovieNameFromBraces(){
  const m = ($('movieName').value || '').match(/《([^》]+)》/);
  if(m) $('movieName').value = m[1];
}
async function buildMovieNarrate(){
  const name = ($('movieName').value || '').trim();
  const plot = ($('moviePlot').value || '').trim();
  if(!name && !plot && !MOVIE_VIDEO){ $('movieStatus').textContent='❌ 请填片名 / 剧情，或上传视频'; return; }
  const ok = await preflight('movie'); if(!ok) return;
  const go = $('movieGo'); go.disabled = true; $('movieResult').style.display = 'none';
  $('movieStatus').textContent = '提交任务…';
  gStart('🌐 联网解说生成');
  const body = { movie: name, plot: plot,
    params: { economy: $('movieMode').value !== 'ai', maxSeg: parseFloat($('movieMaxSeg').value) || 25, w:1280, h:720, fps:30 } };
  if(MOVIE_VIDEO){ body.video = { name: MOVIE_VIDEO.name, data: toB64(new Uint8Array(await MOVIE_VIDEO.arrayBuffer())) }; }
  if($('movieBgm').checked && MUSIC){
    if(MUSIC.catalogId){ body.music = { source:'catalog', catalogId: MUSIC.catalogId }; }
    else { body.music = { name: MUSIC.name, data: toB64(new Uint8Array(await MUSIC.file.arrayBuffer())) }; }
  }
  try{
    const r = await fetch('/api/narrate_movie', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    const out = await r.json();
    if(!out.ok) throw new Error(out.error || '失败');
    await pollMovie(out.runid);
  }catch(e){ $('movieStatus').textContent = '❌ ' + e.message; }
  go.disabled = false;
}
function pollMovie(runid){
  return new Promise(resolve => {
    _currentRunid = runid; const cb=$('movieCancel'); if(cb) cb.style.display='';
    const iv = setInterval(() => {
      fetch('/api/progress?run=' + runid).then(r => r.json()).then(p => {
        const b = $('movieBar').querySelector('i'); $('movieBar').style.display = 'block';
        if(p.pct) b.style.width = Math.min(100, p.pct) + '%';
        gSet(p.pct, p.phase);
        if(p.done){
          clearInterval(iv); $('movieBar').style.display = 'none';
          _currentRunid=null; if(cb) cb.style.display='none';
          if(p.error){ renderPartial('moviePartial', p.partial); $('movieStatus').textContent = '❌ ' + p.error; gErr(p.error); resolve(); return; }
          $('movieStatus').textContent = '✅ 完成'; gDone();
          const d = p.diag || {};
          let txt = '事件 ' + (d.events || 0) + ' · 分段 ' + (d.segments || 0) + ' · 台词 ' + (d.asr_lines || 0) + ' 条 · 对齐 ' + (d.aligned || 0) + ' · 配音 ' + (d.voice_clips || 0) + ' 段';
          if(d.narration && d.narration.length) txt += '\n解说：' + d.narration.join(' / ');
          if(p.script && p.script.length && !p.file) txt += '\n（仅解说稿）' + p.script.map(s => s.desc).join(' / ');
          $('movieDiag').textContent = txt;
          if(p.file){
            $('movieResult').style.display = 'block';
            $('moviePlayer').src = '/media/' + p.file + '?t=' + Date.now();
            setModeBadge('movieBadge', p.mode);
            $('movieDl').href = '/media/' + p.file;
            gPreview(p.file, '联网解说');
          }
          resolve(); return;
        }
        $('movieStatus').textContent = (p.phase || '处理中') + '… ' + (p.pct || 0) + '%';
      }).catch(() => {});
    }, 400);
    setTimeout(() => { clearInterval(iv); _currentRunid=null; if(cb) cb.style.display='none'; resolve(); }, 900000);
  });
}

// ---- 💬 指令成片（Phase 4） ----
async function runInstruct(){
  const text = ($('instructInput').value || '').trim();
  if(!text){ $('instructHint').textContent = '❌ 请输入指令'; return; }
  const ok = await preflight('instruct'); if(!ok) return;
  const go = $('instructGo'); go.disabled = true; $('instructResult').style.display = 'none';
  $('instructStatus').textContent = '解析指令…';
  gStart('💬 指令成片');
  const ctx = {};
  if(MUSIC){ ctx.music = MUSIC.catalogId ? { source:'catalog', catalogId: MUSIC.catalogId, name: MUSIC.name } : { name: MUSIC.name }; }
  if(NAR_VIDEO) ctx.video = { name: NAR_VIDEO.name, data: toB64(new Uint8Array(await NAR_VIDEO.arrayBuffer())) };
  else if(BC_VIDEO) ctx.video = { name: BC_VIDEO.name, data: toB64(new Uint8Array(await BC_VIDEO.arrayBuffer())) };
  const items = [];
  for(const it of ITEMS){
    if(it.file && it.file.size <= 150 * 1024 * 1024){
      items.push({ kind: it.kind, name: it.name, dur: it.dur, data: toB64(new Uint8Array(await it.file.arrayBuffer())) });
    }
  }
  if(items.length) ctx.items = items;
  const body = { instruction: text, context: ctx };
  try{
    const r = await fetch('/api/instruct', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    const out = await r.json();
    if(!out.ok) throw new Error(out.error || '失败');
    await pollInstruct(out.runid);
  }catch(e){ $('instructStatus').textContent = '❌ ' + e.message; }
  go.disabled = false;
}
function pollInstruct(runid){
  return new Promise(resolve => {
    _currentRunid = runid; const cb=$('instructCancel'); if(cb) cb.style.display='';
    const iv = setInterval(() => {
      fetch('/api/progress?run=' + runid).then(r => r.json()).then(p => {
        const b = $('instructBar').querySelector('i'); $('instructBar').style.display = 'block';
        if(p.pct) b.style.width = Math.min(100, p.pct) + '%';
        gSet(p.pct, p.phase);
        if(p.done){
          clearInterval(iv); $('instructBar').style.display = 'none';
          _currentRunid=null; if(cb) cb.style.display='none';
          if(p.error){ renderPartial('instructPartial', p.partial); $('instructStatus').textContent = '❌ ' + p.error; gErr(p.error); resolve(); return; }
          $('instructStatus').textContent = '✅ 完成（' + (p.phase || '') + '）'; gDone();
          if(p.file){
            $('instructResult').style.display = 'block';
            $('instructPlayer').src = '/media/' + p.file + '?t=' + Date.now();
            setModeBadge('instructMode', p.mode);
            $('instructDl').href = '/media/' + p.file;
            gPreview(p.file, '指令成片');
          }
          const d = p.diag || {};
          let txt = (p.phase || '');
          if(d.narration) txt += ' · 解说：' + d.narration.join(' / ');
          if(d.segments) txt += ' · 分段 ' + d.segments;
          if(d.timeline) txt += ' · 切点 ' + d.timeline.length;
          if(p.script && p.script.length) txt += ' · 解说事件 ' + p.script.length;
          $('instructDiag').textContent = txt;
          resolve(); return;
        }
        $('instructStatus').textContent = (p.phase || '处理中') + '… ' + (p.pct || 0) + '%';
      }).catch(() => {});
    }, 400);
    setTimeout(() => { clearInterval(iv); _currentRunid=null; if(cb) cb.style.display='none'; resolve(); }, 900000);
  });
}

async function build(){
  const ok = await preflight('build'); if(!ok) return;
  const go = $('go'); go.disabled = true; $('result').style.display='none';
  $('cancelBtn').style.display = '';
  $('status').textContent = '上传素材…'; setBar(2);
  gStart('✨ 一键合成');
  const [rw, rh] = $('res').value.split('x').map(Number);
  const beatStep = parseFloat($('beatStep').value) || 1;
  const hardCut = $('hardCutSel').value === '1';
  const aiCap = $('aiCap').checked;
  const eco = $('eco') ? $('eco').checked : true;
  const body = { items: [], music: null, params: { w:rw, h:rh, fps:+$('fps').value, transition:$('trans').value, singleDur:+$('defDur').value||3, beatStep, hardCut, ai_captions: aiCap, economy: eco } };
  for (const it of ITEMS){
    if (it.file.size > 150 * 1024 * 1024){
      stopBar(); $('cancelBtn').style.display='none'; $('status').textContent='❌ 文件过大：'+it.name+'（>150MB）'; go.disabled=false; return;
    }
    const buf = await it.file.arrayBuffer();
    body.items.push({ kind:it.kind, name:it.name, dur:it.dur, data: toB64(new Uint8Array(buf)) });
  }
  if (MUSIC){
    let mp = null;
    if (MUSIC.catalogId){ mp = { source:'catalog', catalogId:MUSIC.catalogId, name:MUSIC.name }; }
    else { const mb = await MUSIC.file.arrayBuffer(); mp = { name:MUSIC.name, data: toB64(new Uint8Array(mb)) }; }
    body.music = mp;
  }
  try {
    $('status').textContent = '正在提交…';
    const r = await fetch('/api/build', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    const out = await r.json();
    if (!out.ok) throw new Error(out.error || '合成失败');
    // real progress polling
    _currentRunid = out.runid;
    await pollRun(out.runid);
  } catch(e){ stopBar(); $('status').textContent='❌ '+e.message; }
  $('cancelBtn').style.display='none';
  _currentRunid = null;
  go.disabled = false;
  loadHistory();
}
function pollRun(runid){
  return new Promise((resolve) => {
    let t0 = Date.now();
    const iv = setInterval(() => {
      fetch('/api/progress?run=' + runid).then(r=>r.json()).then(p => {
        if (p.pct) setBar(p.pct);
        gSet(p.pct, p.phase);
        const sec = Math.round((Date.now()-t0)/1000);
        if (p.done){
          clearInterval(iv); stopBar();
          if (p.error){ renderPartial('buildPartial', p.partial); $('status').textContent='❌ '+p.error; gErr(p.error); resolve(); return; }
          $('status').textContent = '✅ 完成（'+(p.duration||'')+'s）'; gDone();
          if (p.beat && p.beat.bpm){ $('musInfo2').textContent='💿 BPM '+p.beat.bpm+' · 节拍 '+p.beat.beat_count+' · 时长 '+(p.duration||'')+'s · 每'+ (p.beat.beatStep||1) +'拍切换，已对齐节拍。'; }
          $('result').style.display='block';
          $('player').src = '/media/' + p.file + '?t=' + Date.now();
          setModeBadge('buildMode', p.mode);
          $('dl').href = '/media/' + p.file;
          gPreview(p.file, '合成视频');
          resolve(); return;
        }
        $('status').textContent = (p.phase||'合成中') + '… ' + (p.pct||0) + '%（已 ' + sec + ' 秒）';
      }).catch(()=>{});
    }, 400);
    // safety: don't poll forever
    setTimeout(()=>{ clearInterval(iv); stopBar(); $('status').textContent='⚠️ 超时，请查看结果后重试'; resolve(); }, 600000);
  });
}

// ---- 模式选择动态提示（告诉用户当前模式需要什么配置，一键跳转） ----
function updateModeHint(selectId){
  const sel = $(selectId);
  const hint = $(selectId + 'Hint');
  if (!sel || !hint) return;
  const val = sel.value;
  if (val === 'ai') {
    hint.innerHTML = '💡 <b>云端智能模式</b>：需要在「🤖 AI 配置 → 🌐 API 云端配置」里填写视觉模型 API Key（DeepSeek/通义/智谱等）。未配置时会降级为免费本地模式。 <a href="javascript:void(0)" class="jump-link" onclick="jumpToAISection(\'cloud\')">⚙️ 去配置云端 API</a>';
  } else {
    hint.innerHTML = '💡 <b>免费本地模式</b>：需要在「🤖 AI 配置 → 🖥 本地离线模型」里启用本地模型（Ollama + qwen2.5）。未配置时会自动降级为模板解说。 <a href="javascript:void(0)" class="jump-link" onclick="jumpToAISection(\'local\')">⚙️ 去配置本地模型</a>';
  }
}
// 页面加载时初始化模式提示（脚本在 body 末尾加载，DOM 已就绪，直接执行）
updateModeHint('narMode');
updateModeHint('movieMode');
updateMusicHint();
updateBuildModeHint();

// ===========================================================================
// 🤝 人机协同：方案预览与微调（卡点 / 解说）
// 分析 → 展示「规划方案」（时间线+缩略图+解说稿）→ 用户微调 → 按用户方案合成
// ===========================================================================
let _planRunid = null, _planType = null;

async function _planMusicBodyAsync(){
  if(!MUSIC) return null;
  if(MUSIC.catalogId) return { source:'catalog', catalogId:MUSIC.catalogId };
  if(MUSIC.file) return { name:MUSIC.name, data: toB64(new Uint8Array(await MUSIC.file.arrayBuffer())) };
  return null;
}

async function planBeatCut(){
  if(!BC_VIDEO){ $('bcStatus').innerHTML='❌ 请先拖入视频到上方区域'; return; }
  if(!MUSIC){ $('bcStatus').innerHTML='❌ 请先选择背景音乐'; return; }
  const params={w:1280,h:720,fps:30,sceneTh:0.30,maxCuts:30, strength: $('bcStrength').value};
  if($('bcKeepAudio')){ params.keepAudio = $('bcKeepAudio').checked; }
  params.transition='none';
  if($('bcTransition') && !($('bcBeatSync') && $('bcBeatSync').checked)){
    params.transition=$('bcTransition').value;
    params.transDur=parseFloat(($('bcTransDur')||{}).value)||0.2;
  }
  await _startPlan('beatcut', params);
}
async function planNarrate(){
  if(!NAR_VIDEO){ $('narStatus').textContent='❌ 请先拖入视频到上方区域'; return; }
  const params={w:1280,h:720,fps:30, maxSeg: parseFloat(($('narMaxSeg')||{}).value)||25};
  await _startPlan('narrate', params);
}
async function _startPlan(type, params){
  const video = (type==='beatcut') ? BC_VIDEO : NAR_VIDEO;
  const st = (type==='beatcut') ? $('bcStatus') : $('narStatus');
  const mainBtn = (type==='beatcut') ? $('bcGo') : $('narGo');
  if(mainBtn) mainBtn.disabled=true;
  st.textContent='🔍 分析中…（首次可能较慢）';
  const body={ type, params, video:{name:video.name, data: toB64(new Uint8Array(await video.arrayBuffer()))} };
  if(type==='beatcut'){ body.music = await _planMusicBodyAsync(); }
  else if(MUSIC){ body.music = await _planMusicBodyAsync(); }
  try{
    const r=await fetch('/api/plan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const out=await r.json();
    if(!out.ok) throw new Error(out.error||'分析失败');
    await _pollPlan(out.runid, type);
  }catch(e){ st.textContent='❌ '+e.message; }
  if(mainBtn) mainBtn.disabled=false;
}
function _pollPlan(runid, type){
  return new Promise(resolve=>{
    const st=(type==='beatcut')?$('bcStatus'):$('narStatus');
    const mainBtn = (type==='beatcut') ? $('bcGo') : $('narGo');
    const done = ()=>{ if(mainBtn) mainBtn.disabled=false; };
    const iv=setInterval(()=>{
      fetch('/api/progress?run='+runid).then(r=>r.json()).then(p=>{
        if(p.plan_ready && p.plan){
          clearInterval(iv);
          _planRunid=runid; _planType=type;
          openPlanEditor(runid, p.plan);
          st.textContent='✅ 规划完成，请在弹窗中微调后点击「按我的调整合成」';
          done(); resolve(); return;
        }
        if(p.error){ st.textContent='❌ '+p.error; clearInterval(iv); done(); resolve(); return; }
        if(p.done){ st.textContent='❌ '+(p.error||'分析失败'); clearInterval(iv); done(); resolve(); return; }
        st.textContent=(p.phase||'分析中')+'… '+(p.pct||0)+'%';
      }).catch(()=>{});
    },400);
    setTimeout(()=>{ clearInterval(iv); done(); resolve(); }, 600000);
  });
}

function _fmtTime(t){ t=Math.max(0,t); const m=Math.floor(t/60), s=(t-m*60); return m+':'+(s<10?'0':'')+s.toFixed(1); }
function _parseTime(s){ const p=s.split(':'); return parseFloat(p[0])*60 + parseFloat(p[1]); }

let _curPlan = null;   // 当前方案 UI 数据
let _cutTimes = null;  // 卡点：当前切点集合（含 0 与 vdur，升序）

function openPlanEditor(runid, plan){
  _planRunid = runid; _planType = plan.type; _curPlan = plan;
  if(plan.type==='beatcut'){
    _cutTimes = [0].concat((plan.cuts||[]).map(c=>c.t), [plan.vdur||0])
      .filter(t=>!isNaN(t)).sort((a,b)=>a-b);
    // 去重（保留 0.001 精度）
    _cutTimes = Array.from(new Set(_cutTimes.map(t=>Math.round(t*1000)))).map(t=>t/1000);
  }
  renderPlanModal();
}

function renderPlanModal(){
  const plan=_curPlan; if(!plan) return;
  const box=$('planBox'); if(!box) return;
  const isBeat = plan.type==='beatcut';
  const title = isBeat ? '🎯 强卡点方案（可微调）' : '🎙 解说方案（可微调）';
  const desc = isBeat
    ? '上方时间轴显示全部切点（圆点）——点 ✕ 可删除某个切换点；在「添加切点」输入秒数可手动加一个切换点。'
      + '列表勾选 = 保留该镜头段；取消勾选 = 该处不切换（与前段合并）。'
    : '列表勾选 = 保留该段并配音；取消勾选 = 去掉该段。可直接编辑每段解说词；点「✂ 减词」可缩短成一句话；点「🔒」把该段锁定为必要（不可误删）。';
  let rowsHtml = '';
  if(isBeat){
    rowsHtml = _cutTimes.slice(0,-1).map((s,i)=>{
      const e=_cutTimes[i+1];
      return _planRowHtml(i, s, e, true, '', _findThumb(e), true);
    }).join('');
  } else {
    rowsHtml = (plan.segs||[]).map((s,i)=>_planRowHtml(i, s.start, s.end, true, s.caption||'', s.thumb||'', false)).join('');
  }
  const timelineHtml = isBeat ? _renderTimeline() : '';
  const addCutHtml = isBeat ? _addCutBarHtml() : '';
  box.innerHTML = '<div class="plan-head"><span class="plan-title">'+title+'</span>'
    + '<button class="btn mini ghost" onclick="closePlanModal()">✕ 关闭</button></div>'
    + '<div class="plan-desc">'+desc+'</div>'
    + timelineHtml
    + addCutHtml
    + '<div class="plan-list">'+rowsHtml+'</div>'
    + '<div class="plan-sum" id="planSum"></div>'
    + '<div class="plan-actions">'
    + '<button class="btn ghost" onclick="closePlanModal()">取消</button>'
    + '<button class="btn" onclick="confirmPlan()">✅ 按我的调整合成</button>'
    + '</div>';
  // 事件绑定
  box.querySelectorAll('.plan-row').forEach(row=>{
    const cb=row.querySelector('.on');
    cb.addEventListener('change', ()=>{
      row.classList.toggle('off', !cb.checked);
      _updatePlanSum();
    });
    const ess=row.querySelector('.ess');
    if(ess){
      ess.addEventListener('click', ()=>{
        const locked = ess.classList.toggle('locked');
        const onCb = row.querySelector('.on');
        if(locked){ onCb.checked=true; row.classList.remove('off'); onCb.disabled=true; ess.title='已锁定（必要片段）'; }
        else { onCb.disabled=false; ess.title='锁定为必要片段'; }
        _updatePlanSum();
      });
    }
    const sh=row.querySelector('.shrink');
    if(sh){
      sh.addEventListener('click', ()=>{
        const cap=row.querySelector('.cap');
        if(cap) cap.value = _shortenCaption(cap.value);
      });
    }
  });
  _updatePlanSum();
  $('planModal').style.display='flex';
}

function _planRowHtml(i, s, e, on, caption, thumb, isBeat){
  const th = thumb ? '<img src="/media/'+thumb+'?t='+Date.now()+'">' : '<img>';
  let capField='';
  if(!isBeat){
    capField = '<input type="text" class="cap" value="'+_esc(caption)+'" placeholder="解说词">'
      + '<button type="button" class="mini-btn shrink" title="缩短为一句">✂ 减词</button>'
      + '<button type="button" class="mini-btn ess" title="锁定为必要片段">🔒</button>';
  }
  return '<div class="plan-row" data-i="'+i+'">'
    + '<input type="checkbox" class="on" '+(on?'checked':'')+'>'
    + th
    + '<span class="time">'+_fmtTime(s)+' ~ '+_fmtTime(e)+'</span>'
    + capField
    + '</div>';
}

function _renderTimeline(){
  const vdur = _curPlan.vdur || 1;
  let marks='';
  for(let i=0;i<_cutTimes.length;i++){
    const t=_cutTimes[i];
    const pct=Math.min(100, Math.max(0, t/vdur*100));
    if(i===0 || i===_cutTimes.length-1){
      marks += '<div class="tl-marker tl-edge" style="left:'+pct.toFixed(1)+'%"><span>'+_fmtTime(t)+'</span></div>';
    }else{
      marks += '<div class="tl-marker tl-cut" style="left:'+pct.toFixed(1)+'%" title="点击 ✕ 删除此切点（'+_fmtTime(t)+'）" onclick="removePlanCut('+i+')">'
             + '<span>'+_fmtTime(t)+'</span><b>✕</b></div>';
    }
  }
  return '<div class="plan-timeline"><div class="tl-bar"></div>'+marks+'</div>';
}

function _addCutBarHtml(){
  const vdur=_curPlan.vdur||0;
  return '<div class="plan-addcut">'
    + '<input type="number" id="newCutT" min="0.1" max="'+(vdur>0?(vdur-0.3).toFixed(1):'999')+'" step="0.1" placeholder="输入秒数，如 3.5">'
    + '<button class="btn mini" onclick="addPlanCut()">＋ 添加切点</button>'
    + '<span class="hint">手动增加一个切换点（在 0.3 ~ '+(vdur>0?(vdur-0.3).toFixed(1):'…')+' 秒之间）</span>'
    + '</div>';
}

function _findThumb(e){
  const plan=_curPlan; if(!plan || !plan.segs) return '';
  let best='', bestD=1e9;
  (plan.segs||[]).forEach(seg=>{
    const d=Math.abs((seg.end||0) - e);
    if(d<bestD){ bestD=d; best=seg.thumb||''; }
  });
  return best;
}

function addPlanCut(){
  const inp=$('newCutT'); if(!inp) return;
  const t=parseFloat(inp.value);
  const vdur=_curPlan.vdur||0;
  if(!(t>0.3 && t<vdur-0.3)){ alert('切点需在视频中部（0.3 ~ '+(vdur-0.3).toFixed(1)+' 秒）'); return; }
  if(_cutTimes.some(x=>Math.abs(x-t)<0.5)){ alert('该位置附近已有切点'); return; }
  _cutTimes.push(t);
  _cutTimes.sort((a,b)=>a-b);
  renderPlanModal();
}

function removePlanCut(idx){
  if(!_cutTimes || idx<=0 || idx>=_cutTimes.length-1) return;
  _cutTimes.splice(idx,1);
  renderPlanModal();
}

function _shortenCaption(s){
  const t=(s||'').trim();
  if(!t) return '';
  const m=t.split(/[。！？!?；;\n]/)[0] || t;
  return m.length>14 ? m.slice(0,14)+'…' : m;
}

function _esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function _updatePlanSum(){
  const rows=[...$('planBox').querySelectorAll('.plan-row')];
  const keep=rows.filter(r=>r.querySelector('.on').checked).length;
  const sum=$('planSum'); if(sum) sum.textContent='已保留 '+keep+' / '+rows.length+' 段'+(keep<rows.length?'（未勾选的段会被跳过）':'');
}
function _collectPlanEdits(){
  const plan=_curPlan; const isBeat = plan && plan.type==='beatcut';
  const segs=[];
  const rows=[...$('planBox').querySelectorAll('.plan-row')];
  if(isBeat){
    // 按当前切点集合重建段（含用户新增的切点；缩略图沿用最近段）
    for(let i=0;i<_cutTimes.length-1;i++){
      const s=_cutTimes[i], e=_cutTimes[i+1];
      const row=rows[i];
      const on = row ? row.querySelector('.on').checked : true;
      segs.push({ start:s, end:e, caption:'', on });
    }
  }else{
    rows.forEach(r=>{
      const on=r.querySelector('.on').checked;
      const cap=r.querySelector('.cap');
      const t=r.querySelector('.time').textContent.split(' ~ ');
      segs.push({ start:_parseTime(t[0]), end:_parseTime(t[1]),
                  caption: cap?cap.value:'', on });
    });
  }
  return { segs };
}
async function confirmPlan(){
  if(!_planRunid) return;
  const edits=_collectPlanEdits();
  const btn=$('planBox').querySelector('.plan-actions .btn:last-child');
  if(btn){ btn.disabled=true; btn.textContent='⏳ 合成中…'; }
  const st=(_planType==='beatcut')?$('bcStatus'):$('narStatus');
  st.textContent='⏳ 正在按你的调整合成…';
  try{
    const r=await fetch('/api/confirm',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({runid:_planRunid, edits})});
    const out=await r.json();
    if(!out.ok) throw new Error(out.error||'合成失败');
    await _pollRender(out.runid, _planType);
  }catch(e){ st.textContent='❌ '+e.message; }
  closePlanModal();
}
function _pollRender(runid, type){
  return new Promise(resolve=>{
    const st=(type==='beatcut')?$('bcStatus'):$('narStatus');
    const iv=setInterval(()=>{
      fetch('/api/progress?run='+runid).then(r=>r.json()).then(p=>{
        if(p.done){
          clearInterval(iv);
          if(p.error){ st.textContent='❌ '+p.error; resolve(); return; }
          st.textContent='✅ 完成（已按你的调整合成）';
          _showPlanResult(type, p);
          resolve(); return;
        }
        st.textContent=(p.phase||'合成中')+'… '+(p.pct||0)+'%';
      }).catch(()=>{});
    },400);
    setTimeout(()=>{ clearInterval(iv); resolve(); }, 900000);
  });
}
function _showPlanResult(type, p){
  const g=(id)=>$(id);
  if(type==='beatcut'){
    g('bcResult').style.display='block';
    g('bcPlayer').src='/media/'+p.file+'?t='+Date.now();
    g('bcDl').href='/media/'+p.file;
    setModeBadge('bcMode','human');
    gPreview(p.file,'强卡点短片');
    const d=p.diag||{};
    g('bcDiag').textContent='已按你的调整合成 · 切换点 '+ (d.segments||0) +' 个'+(d.transition&&d.transition!=='none'?(' · 转场 '+d.transition):'')+(d.keep_audio?' · 保留原声':'');
  }else{
    g('narResult').style.display='block';
    g('narPlayer').src='/media/'+p.file+'?t='+Date.now();
    g('narDl').href='/media/'+p.file;
    setModeBadge('narBadge','human');
    gPreview(p.file,'电影解说');
    const d=p.diag||{};
    g('narDiag').textContent='已按你的调整合成 · 片段 '+ (d.segments||0) +' 段 · 配音 '+ (d.voice_clips||0) +' 段';
  }
}
function closePlanModal(){
  const m=$('planModal'); if(m) m.style.display='none';
}

