// ---- 主题切换（日间/夜间） ----
(function(){
  const saved = localStorage.getItem('framecut_theme') || 'dark';
  applyTheme(saved);
})();

function applyTheme(theme){
  document.documentElement.setAttribute('data-theme', theme);
  const btn = document.getElementById('themeToggle');
  if(btn){
    btn.textContent = theme === 'dark' ? '🌙 夜间' : '☀️ 日间';
  }
}

function toggleTheme(){
  const cur = document.documentElement.getAttribute('data-theme') || 'dark';
  const next = cur === 'dark' ? 'light' : 'dark';
  applyTheme(next);
  localStorage.setItem('framecut_theme', next);
}


const ITEMS = [];
const $ = id => document.getElementById(id);
const drop = $('drop'), fi = $('fileInput');
drop.addEventListener('click', () => fi.click());
fi.addEventListener('change', e => { for (const f of fi.files) handle(f); fi.value=''; });
drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('dragover'); });
drop.addEventListener('dragleave', () => drop.classList.remove('dragover'));
drop.addEventListener('drop', e => { e.preventDefault(); drop.classList.remove('dragover'); for (const f of e.dataTransfer.files) handle(f); });

// ---- remember last-used settings across sessions (localStorage) ----
(function(){
  const KEY = 'springStudio.settings';
  // 追加记忆的控件：强卡点 / 短片解说 / 联网解说 参数（刷新后不丢失）
  const EXT = ['bcStrength','bcSensitivity','bcMinClip','bcBeatSync','bcKeepAudio','bcTransition','bcTransDur',
               'narMaxSeg','movieMaxSeg','narTheme','narReq','narBgm','movieBgm'];
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) || '{}');
    if (saved.res && $('res').querySelector('option[value="'+saved.res+'"]')) $('res').value = saved.res;
    if (saved.fps) $('fps').value = saved.fps;
    if (saved.trans) $('trans').value = saved.trans;
        if (saved.hardCutSel) $('hardCutSel').value = saved.hardCutSel;
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
    const o = { res:$('res').value, fps:$('fps').value, trans:$('trans').value, hardCutSel:$('hardCutSel').value, aiCap: $('aiCap')?$('aiCap').checked:false };
    EXT.forEach(id => { const el=$(id); if (el) o[id] = el.type==='checkbox' ? el.checked : el.value; });
    try { localStorage.setItem(KEY, JSON.stringify(o)); } catch(e){}
  }
  const ids = ['res','fps','trans','hardCutSel','aiCap'].concat(EXT);
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
  const aiCap = $('aiCap');
  const aiOn = aiCap ? aiCap.checked : false;
  hint.innerHTML = '🤖 <b>自动路径</b>：本地模型（免费）优先 → 配置了云端 key 时用云端增强 → 都没有时离线模板兜底。'
    + (aiOn ? '已勾选「按画面生成中文文案」：有可用的看图能力（本地视觉理解或云端视觉 API）时自动生成，否则用离线模板。'
            : '未勾选「按画面生成中文文案」：不会生成 AI 文案。');
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
      const lenTxt = (t.length !== null && t.length !== undefined) ? ('时长' + t.length + 's') : '待缓存';
      d.innerHTML = `<div class="info"><div class="t">${escapeHtml(t.title)}</div>
        <div class="m">${escapeHtml(t.genre)} · BPM~${escapeHtml(t.bpm)} · ${escapeHtml(lenTxt)} · ${escapeHtml(t.license)}</div></div>
        <audio preload="none"></audio>
        <button class="btn mini ghost mprev">▶ 预览</button>
        <button class="btn mini ghost muse">＋ 使用</button>`;
      // 用监听器代替内联 onclick：标题/ID 里的引号不会再破坏 HTML 属性与 JS 字符串
      d.querySelector('.mprev').addEventListener('click', function(){ previewMusic(t.id, '/music_lib/' + t.id + '.mp3', this); });
      d.querySelector('.muse').addEventListener('click', () => useMusic(t.id, t.title));
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
    const vid = c.video || {};
    if($('videoEnc') && vid.encoder) $('videoEnc').value = vid.encoder;
    if($('videoEncInfo')){
      $('videoEncInfo').textContent = '当前生效：' + (res.video_encoder || 'CPU 软编 libx264')
        + '　（渲染是长视频出片的主要耗时环节，GPU 硬编通常更快）';
    }
    if(vlm.mode) $('vlmMode').value = vlm.mode;
    const mir = c.mirror || {};
    if($('mirUseMirror')) $('mirUseMirror').value = (mir.use_hf_mirror === false) ? '0' : '1';
    if(mir.hf_endpoint) $('mirHf').value = mir.hf_endpoint;
    if($('mirProxy')) $('mirProxy').value = mir.ollama_proxy || '';
    ttsProviderHint();
    if($('cleanupMid')) $('cleanupMid').checked = (c.cleanup_mid !== false);
    const st = [];
    if(res.vision_available) st.push('视觉✅'); else st.push('视觉(离线)');
    if(res.tts_available) st.push('配音✅'); else st.push('配音未配');
    $('aiStatus').textContent = st.join(' · ');
    // 自动勾选「按画面生成中文文案」：VLM已启用且就绪时自动勾，不用手动选
    const aiCap = $('aiCap');
    if(aiCap && res.vlm_enabled && res.vlm_ready && !aiCap.checked){
      aiCap.checked = true;
      if(typeof updateBuildModeHint === 'function') updateBuildModeHint();
    }
    loadHardware();
  }).catch(e=>console.warn('加载 AI 配置失败', e));
}


function saveCleanupMid(){
  const on = $('cleanupMid') ? $('cleanupMid').checked : true;
  fetch('/api/ai/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({cleanup_mid: on})})
    .then(r=>r.json()).then(res=>{ if(!res.ok) console.warn('清理配置保存失败', res.error); }).catch(e=>console.warn(e));
}

// ---- 硬件检测与模型推荐 ----
function loadHardware(){
  fetch('/api/hardware').then(r=>r.json()).then(h=>{
    const banner = $('hardwareBanner'); if(!banner) return;
    banner.style.display = 'block';
    const info = [];
    if(h.gpu) info.push('GPU: ' + h.gpu + ' (' + h.gpu_vram_gb + 'GB 显存)');
    if(h.ram_gb) info.push('内存: ' + h.ram_gb + 'GB');
    info.push('Ollama: ' + (h.ollama ? '✅ 运行中 (' + h.ollama_models.length + ' 个模型)' : '❌ 未运行'));
    if(h.tier) info.push('档位: ' + h.tier);
    $('hardwareInfo').innerHTML = info.join(' · ');
    const rec = h.recommendations || {};
    const cur = h.current || {};
    let recHtml = '<div style="margin-bottom:4px;"><b>推荐配置：</b></div>';
    recHtml += '<div style="display:grid; grid-template-columns:1fr 1fr; gap:4px 16px; font-size:12px;">';
    const labels = {vlm:'VLM 视觉模型', whisper:'Whisper 转写', tts:'TTS 配音', text:'文本写稿'};
    for(const k of ['vlm','whisper','tts','text']){
      if(rec[k]){
        let curVal = cur[k] || '';
        if(k==='tts') curVal = cur.tts_engine || '';
        const isDiff = rec[k] !== curVal;
        recHtml += '<div>' + labels[k] + ': <b>' + rec[k] + '</b>' + (isDiff?' <span style="color:#e65100;">(当前:'+(curVal||'未设')+')</span>':'') + '</div>';
      }
    }
    recHtml += '</div>';
    if(rec.note) recHtml += '<div style="margin-top:4px; color:#555;">💡 ' + rec.note + '</div>';
    if(h.upgrades && h.upgrades.length){
      recHtml += '<div style="margin-top:6px; color:#c62828;"><b>可升级：</b>' + h.upgrades.map(u=>u.slot+' '+u.current+'→'+u.recommend).join('；') + '</div>';
    }
    $('hardwareRecs').innerHTML = recHtml;
    const hasUpgrade = h.upgrades && h.upgrades.length > 0;
    $('hardwareActions').innerHTML = hasUpgrade
      ? '<button class="btn" onclick="applyHardwareRecs()">⚡ 一键应用推荐配置</button> <span style="font-size:12px;color:#666;">（修改模型选择，缺失模型需另行下载）</span>'
      : '<span style="color:#2e7d32; font-size:12px;">✅ 当前配置已是该硬件的最优选择</span>';
  }).catch(e=>console.warn('硬件检测失败', e));
}
function applyHardwareRecs(){
  fetch('/api/hardware').then(r=>r.json()).then(h=>{
    const rec = h.recommendations || {};
    const body = {};
    if(rec.whisper) body.whisper = {model: rec.whisper};
    if(rec.vlm) body.vlm = {enabled: true, mode: 'ollama', base_url: 'http://localhost:11434', model: rec.vlm};
    if(rec.text) body.local = {enabled: true, base_url: 'http://localhost:11434/v1', model: rec.text};
    if(rec.tts === 'melo-zh') body.tts_local = {engine: 'sherpa', voice: 'zh-CN-XiaoxiaoNeural', rate: '+7%'};
    fetch('/api/ai/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)})
      .then(r=>r.json()).then(res=>{
        if(res.ok){ $('aiStatus').textContent = '✅ 已应用推荐配置，请下载缺失模型'; loadAIConfig(); loadHardware(); }
        else $('aiStatus').textContent = '❌ ' + res.error;
      }).catch(()=>$('aiStatus').textContent='❌ 应用失败');
  });
}
// ---------------------------------------------------------------------------
// 🔊 本地配音引擎（免费）：edge-tts / 离线模型(sherpa-onnx) / 系统 SAPI
// 历史缺口：界面上只有「云端 TTS 要 Key」的选项，本地配音既没有引擎选择也没有下载入口，
// 用户只能吃到系统自带的那一个中文音色。这里补齐选择 + 安装 + 试听。
// ---------------------------------------------------------------------------
let _ttsSetupTimer = null;
function ttsLocalHint(){
  const eng = $('ttsLocalEngine') ? $('ttsLocalEngine').value : 'auto';
  const voiceSel = $('ttsLocalVoice');
  if(voiceSel) voiceSel.style.display = (eng === 'sapi' || eng === 'sherpa' || eng === 'cosyvoice' || eng === 'chattts') ? 'none' : '';
  // 引擎选CosyVoice时：隐藏sherpa离线模型下拉，显示CosyVoice音色下拉
  const sherpaField = $('ttsSherpaField');
  const cosyField = $('ttsCosyField');
  if(sherpaField) sherpaField.style.display = (eng === 'cosyvoice' || eng === 'chattts' || eng === 'sapi') ? 'none' : '';
  if(cosyField) cosyField.style.display = (eng === 'cosyvoice') ? '' : 'none';
  const cloneField = $('cosyVoiceCloneField');
  if(cloneField) cloneField.style.display = (eng === 'cosyvoice') ? 'flex' : 'none';
  if(eng === 'cosyvoice') loadCosyVoices();
  const hint = $('ttsLocalHint');
  if(!hint) return;
  const m = {
    auto: '自动：优先 edge-tts（免 Key、音色最多）→ CosyVoice → ChatTTS → 离线模型 → 系统 SAPI 兜底。',
    edge: 'edge-tts 免 Key，但要能访问微软朗读服务；连不上会自动改用下一条，并暂时不再重试（不会拖慢出片）。',
    cosyvoice: 'CosyVoice 质量最高（接近商业级），支持3秒声音克隆。需先点「📥 装 CosyVoice」安装（约9GB模型，10-20分钟）。',
    chattts: 'ChatTTS 很自然，对话式语气，本地GPU推理。需先点「📥 装 ChatTTS」安装（约3GB）。',
    sherpa: '离线模型需先点「📥 下载选中的离线模型」下载一次（约 130MB），之后断网也能配音。',
    sapi: '系统 SAPI 零安装但音色少（多数 Windows 只有一个中文女声），机械味较重。',
  };
  hint.textContent = m[eng] || m.auto;
  // 引擎切换即时保存（否则试听仍用旧引擎）
  saveTtsLocal();
}

function saveTtsLocal(){
  const body = { tts_local: {
    engine: $('ttsLocalEngine') ? $('ttsLocalEngine').value : 'auto',
    voice: $('ttsLocalVoice') ? $('ttsLocalVoice').value : 'zh-CN-XiaoxiaoNeural',
    rate: $('ttsLocalRate') ? $('ttsLocalRate').value.trim() : '+0%',
    cosy_voice: $('ttsCosyVoice') ? $('ttsCosyVoice').value : '中文女',
    sherpa_model: $('ttsSherpaModel') ? $('ttsSherpaModel').value : '',
  }};
  fetch('/api/ai/config', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body)}).catch(()=>{});
}

// 加载CosyVoice所有音色（预设+自定义克隆）
function loadCosyVoices(){
  fetch('/api/tts/cosyvoice/voices').then(r=>r.json()).then(res=>{
    if(!res.ok || !res.voices) return;
    const sel = $('ttsCosyVoice');
    if(!sel) return;
    const cur = sel.value;
    const presets = ['中文女','中文男','英文女','英文男','粤语女','日语女'];
    let html = '';
    for(const v of res.voices){
      const label = v.custom ? ('🎙️ ' + v.name + '（克隆）') : v.name;
      html += '<option value="'+escapeHtml(v.name)+'">'+escapeHtml(label)+'</option>';
    }
    sel.innerHTML = html;
    if(cur) sel.value = cur;
  }).catch(()=>{});
}

// 上传参考音频添加克隆音色
function addCosyVoice(){
  const name = $('cosyCloneName') ? $('cosyCloneName').value.trim() : '';
  const fileInput = $('cosyCloneAudio');
  const status = $('cosyCloneStatus');
  if(!name){ if(status) status.textContent = '❌ 请输入音色名称'; return; }
  if(!fileInput || !fileInput.files || !fileInput.files[0]){
    if(status) status.textContent = '❌ 请选择音频文件（3秒以上清晰人声）';
    return;
  }
  const file = fileInput.files[0];
  if(file.size > 30 * 1024 * 1024){
    if(status) status.textContent = '❌ 文件太大（>30MB），请用3-10秒的短音频';
    return;
  }
  if(status) status.textContent = '⏳ 正在处理音频（' + (file.size/1024/1024).toFixed(1) + 'MB）…';
  const reader = new FileReader();
  reader.onload = function(e){
    fetch('/api/tts/cosyvoice/add_voice', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name: name, audio: e.target.result})
    }).then(r=>r.json()).then(res=>{
      if(res.ok){
        if(status) status.textContent = '✅ 音色「' + name + '」已添加';
        if($('cosyCloneName')) $('cosyCloneName').value = '';
        if(fileInput) fileInput.value = '';
        loadCosyVoices();
        saveTtsLocal();
      } else {
        if(status) status.textContent = '❌ ' + (res.error || '添加失败');
      }
    }).catch(e=>{ if(status) status.textContent = '❌ ' + e.message; });
  };
  reader.readAsDataURL(fileInput.files[0]);
}
function loadTtsLocal(){
  fetch('/api/tts/voices').then(r=>r.json()).then(res=>{
    if(!res.ok) return;
    const sel = $('ttsLocalVoice');
    if(sel){
      const cur = (res.cfg && res.cfg.voice) || sel.value;
      sel.innerHTML = (res.voices||[]).map(v=>'<option value="'+escapeHtml(v[0])+'">'+escapeHtml(v[1])+'</option>').join('');
      if(cur) sel.value = cur;
    }
    const cfg = res.cfg || {};
    if(cfg.engine && $('ttsLocalEngine')) $('ttsLocalEngine').value = cfg.engine;
    if(cfg.rate && $('ttsLocalRate')) $('ttsLocalRate').value = cfg.rate;
    if(cfg.cosy_voice && $('ttsCosyVoice')) $('ttsCosyVoice').value = cfg.cosy_voice;
    const edgeBtn = $('ttsEdgeBtn'), modelBtn = $('ttsModelBtn');
    if(edgeBtn) edgeBtn.textContent = res.edge_installed ? '✅ edge-tts 已装' : '📥 装 edge-tts';
    if(modelBtn) modelBtn.textContent = res.sherpa_model_ready ? '✅ 离线模型已装' : '📥 下载选中的离线模型';
    // CosyVoice状态：已装则按钮变灰显示已装；安装中则显示安装进度
    const cosyBtn = document.querySelector('button[onclick="installCosyVoice()"]');
    const setup = res.setup || {};
    if(cosyBtn){
      if(setup.running){
        cosyBtn.textContent = '⏳ 安装中…' + (setup.pct ? Math.round(setup.pct) + '%' : '');
        cosyBtn.disabled = true;
      } else if(res.cosyvoice_installed){
        cosyBtn.textContent = '✅ CosyVoice 已装';
        cosyBtn.disabled = true;
      } else {
        cosyBtn.textContent = '📥 装 CosyVoice（推荐）';
        cosyBtn.disabled = false;
      }
    }
    // 页面刷新后恢复安装进度轮询
    if(setup.running && !_ttsSetupTimer){
      _ttsSetupTimer = setInterval(_ttsSetupPoll, 2000);
      const bar = $('ttsSetupBar'), fill = $('ttsSetupFill');
      if(bar) bar.style.display = 'block';
      if(fill && setup.pct) fill.style.width = Math.min(100, setup.pct) + '%';
    }
    const st = $('ttsLocalState');
    if(st){
      const parts = ['当前配音：' + (res.label||'未知')];
      if(!res.edge_installed) parts.push('edge-tts 未安装');
      else if(res.edge_dead) parts.push('edge-tts 暂不可用（' + res.edge_dead.slice(0,60) + '）');
      if(!res.sherpa_model_ready) parts.push('离线模型未下载（' + (res.sherpa_model_label||'') + '）');
      st.textContent = parts.join(' · ');
    }
    // 解说卡直接显示「这条解说会用哪种声音」，不用跑去 AI 页才知道
    const nv = $('narVoiceInfo');
    if(nv){
      nv.innerHTML = '🔊 配音：<b>' + escapeHtml(res.label || '未知') + '</b>'
        + '　<a href="javascript:void(0)" class="jump-link" onclick="jumpToAISection(\'local\')">⚙️ 换配音引擎/音色</a>';
    }
    // 填充离线模型下拉框 + 控制卸载按钮
    const sherpaSel = $('ttsSherpaModel');
    const uninstBtn = $('ttsUninstallBtn');
    if(sherpaSel && res.sherpa_models){
      const curKey = res.sherpa_model || '';
      sherpaSel.innerHTML = res.sherpa_models.map(m =>
        '<option value="' + m.key + '"' + (m.key===curKey?' selected':'') + '>'
        + m.label + (m.ready ? ' ✅' : ' ⬜未下载') + '</option>'
      ).join('');
      // 卸载按钮：仅当选中的模型已下载时显示
      if(uninstBtn){
        const selModel = res.sherpa_models.find(m => m.key === sherpaSel.value);
        uninstBtn.style.display = selModel && selModel.ready ? 'inline-block' : 'none';
      }
    }
    ttsLocalHint();
  }).catch(e=>console.warn('本地配音状态加载失败', e));
}
function _ttsSetupPoll(){
  fetch('/api/tts/voices').then(r=>r.json()).then(res=>{
    const s = res.setup || {};
    const bar = $('ttsSetupBar'), fill = $('ttsSetupFill');
    if(bar) bar.style.display = s.running ? 'block' : 'none';
    if(fill && s.pct) fill.style.width = Math.min(100, s.pct) + '%';
    const st = $('ttsLocalState');
    if(st){
      if(s.running && s.msg){
        const pct = s.pct ? ' (' + Math.round(s.pct) + '%)' : '';
        st.textContent = '⏳ ' + s.msg + pct;
        st.classList.add('tts-installing');
      } else {
        st.classList.remove('tts-installing');
      }
    }
    // 更新CosyVoice按钮百分比
    const cosyBtn = document.querySelector('button[onclick="installCosyVoice()"]');
    if(cosyBtn && s.running){
      cosyBtn.textContent = '⏳ 安装中…' + (s.pct ? Math.round(s.pct) + '%' : '');
    }
    if(!s.running){
      clearInterval(_ttsSetupTimer); _ttsSetupTimer = null;
      loadTtsLocal();
    }
  }).catch(()=>{});
}
function installTtsPkg(pkg){
  const st = $('ttsLocalState');
  if(st) st.textContent = '⏳ 正在安装 ' + pkg + '（约 1 分钟）…';
  fetch('/api/tts/install', {method:'POST', headers:{'Content-Type':'application/json'},
                             body: JSON.stringify({pkg: pkg})})
    .then(r=>r.json()).then(res=>{
      if(!res.ok && st) st.textContent = '❌ ' + (res.error || res.message || '安装失败');
    }).catch(e=>{ if(st) st.textContent = '❌ 安装请求失败：' + e.message; });
  if(!_ttsSetupTimer) _ttsSetupTimer = setInterval(_ttsSetupPoll, 1500);
}
function installChatTts(){
  const st = $('ttsLocalState');
  if(st) st.textContent = '⏳ 正在安装 ChatTTS（torch+模型约3GB，首次较慢）…';
  fetch('/api/tts/install_chattts', {method:'POST', headers:{'Content-Type':'application/json'},
                                     body: JSON.stringify({})})
    .then(r=>r.json()).then(res=>{
      if(st) st.textContent = (res.ok ? '✅ ' : '❌ ') + (res.message || res.error || '');
    }).catch(e=>{ if(st) st.textContent = '❌ 安装请求失败：' + e.message; });
  if(!_ttsSetupTimer) _ttsSetupTimer = setInterval(_ttsSetupPoll, 3000);
}
function installCosyVoice(){
  const st = $('ttsLocalState');
  const btn = document.querySelector('button[onclick="installCosyVoice()"]');
  // 防重复安装
  if(btn && btn.disabled){
    if(st) st.textContent = '⏳ CosyVoice 正在安装中，请勿重复点击…';
    return;
  }
  if(btn){ btn.disabled = true; btn.textContent = '⏳ 安装中…'; }
  if(st) st.textContent = '⏳ 正在安装 CosyVoice（venv+PyTorch+9GB模型，约10-20分钟，请勿关闭页面）…';
  fetch('/api/tts/cosyvoice/install', {method:'POST', headers:{'Content-Type':'application/json'},
                                       body: JSON.stringify({})})
    .then(r=>r.json()).then(res=>{
      if(st) st.textContent = (res.ok ? '✅ ' : '❌ ') + (res.message || res.error || '');
      if(btn && !res.ok){ btn.disabled = false; btn.textContent = '📥 装 CosyVoice（推荐）'; }
    }).catch(e=>{
      if(st) st.textContent = '❌ 安装请求失败：' + e.message;
      if(btn){ btn.disabled = false; btn.textContent = '📥 装 CosyVoice（推荐）'; }
    });
  if(!_ttsSetupTimer) _ttsSetupTimer = setInterval(_ttsSetupPoll, 2000);
}
function downloadTtsModel(){
  const st = $('ttsLocalState');
  const sel = $('ttsSherpaModel');
  const model = sel ? sel.value : 'melo-zh';
  const label = sel ? sel.options[sel.selectedIndex].text.replace(' ✅','').replace(' ⬜未下载','') : model;
  if(st) st.textContent = '⏳ 正在下载 ' + label + '…';
  fetch('/api/tts/model/download', {method:'POST', headers:{'Content-Type':'application/json'},
                                    body: JSON.stringify({model: model})})
    .then(r=>r.json()).then(res=>{
      if(!res.ok && st) st.textContent = '❌ ' + (res.error || res.message || '下载失败');
    }).catch(e=>{ if(st) st.textContent = '❌ 下载请求失败：' + e.message; });
  if(!_ttsSetupTimer) _ttsSetupTimer = setInterval(_ttsSetupPoll, 2000);
}

function saveTtsCosy(){
  const sel = $('ttsCosyVoice'); if(!sel) return;
  saveTtsLocal();
}
function saveTtsSherpa(){
  const sel = $('ttsSherpaModel'); if(!sel) return;
  saveTtsLocal();
  loadTtsLocal();
}

function uninstallTtsModel(){
  const sel = $('ttsSherpaModel'); if(!sel) return;
  const key = sel.value;
  if(!key) return;
  const label = sel.options[sel.selectedIndex] ? sel.options[sel.selectedIndex].text : key;
  if(!confirm('确定卸载「' + label + '」？\n\n这会删除模型文件释放磁盘空间，之后需要重新下载。')) return;
  const st = $('ttsLocalState');
  if(st) st.textContent = '⏳ 正在卸载 ' + label + '…';
  fetch('/api/tts/model/uninstall', {method:'POST', headers:{'Content-Type':'application/json'},
                                     body: JSON.stringify({model: key})})
    .then(r=>r.json()).then(res=>{
      if(st) st.textContent = (res.ok ? '✅ ' : '❌ ') + (res.message || res.error || '');
      loadTtsLocal();
    }).catch(e=>{ if(st) st.textContent = '❌ 卸载请求失败：' + e.message; });
}

function resetTtsEngine(){
  fetch('/api/tts_reset').then(r=>r.json()).then(out=>{
    alert(out.ok ? '✅ 配音引擎已重置，edge-tts熔断已解除' : ('❌ ' + (out.error||'失败')));
  }).catch(e=>alert('❌ '+e.message));
}

function testLocalTts(){
  const st = $('ttsLocalState'), pl = $('ttsTestPlayer');
  if(st) st.textContent = '⏳ 正在试听合成…';
  fetch('/api/tts/test', {method:'POST', headers:{'Content-Type':'application/json'},
                          body: JSON.stringify({text: '画面缓缓推进，主角从人群中走出来。'})})
    .then(r=>r.json()).then(res=>{
      if(st) st.textContent = (res.ok ? '✅ ' : '❌ ') + (res.message || '');
      if(res.ok && res.file && pl){
        pl.src = '/media/' + res.file + '?t=' + Date.now();
        pl.style.display = 'block';
        pl.play().catch(()=>{});
      }
    }).catch(e=>{ if(st) st.textContent = '❌ 试听失败：' + e.message; });
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
    tts_local: { engine: $('ttsLocalEngine') ? $('ttsLocalEngine').value : 'auto',
                 voice: $('ttsLocalVoice') ? $('ttsLocalVoice').value : 'zh-CN-XiaoxiaoNeural',
                 rate: $('ttsLocalRate') ? $('ttsLocalRate').value.trim() : '+0%' },
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
    .then(()=>loadWhisperStatus()).catch(e=>console.warn('Whisper 配置保存失败', e));
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
    // 渲染模型卡片
    const installed = res.available || [];
    const cur = res.selected || 'base';
    const downloading = res.downloading ? res.download_model : null;
    if(typeof _renderModelCards === 'function'){
      _renderModelCards('whisperModelCards', WHISPER_MODEL_CATALOG, cur, installed, downloading, 'pickWhisperModel');
    }
    // 自动续轮询：下载中持续刷新
    if(res.downloading) setTimeout(loadWhisperStatus, 2000);
  }).catch(e=>console.warn('加载 Whisper 状态失败', e));
}
function pickWhisperModel(tag, installed){
  const el = $('whisperModel'); if(!el) return;
  el.value = tag;
  saveWhisper();
  if(!installed){
    // 未安装则自动开始下载
    const btn = $('dlWhisper'); if(btn){ btn.textContent='⏳ 下载中…'; btn.disabled=true; }
    fetch('/api/whisper/download', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ model: tag }) })
      .then(r=>r.json()).then(res=>{
        if($('whisperStatus')) $('whisperStatus').textContent = (res.ok?'⏳ ':'❌ ') + (res.message||'');
        loadWhisperStatus();
      }).catch(()=>{ if(btn){ btn.textContent='⬇ 下载/预载模型'; btn.disabled=false; } });
  }
}

// ---- 本地视觉理解 VLM：启用保存 / 拉取 / 检测轮询 ----
function saveVlm(){
  fetch('/api/ai/config', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ vlm: {
      enabled: $('vlmEnabled').checked, mode: $('vlmMode').value.trim(),
      base_url: $('vlmBase').value.trim(), model: $('vlmModel').value.trim() } }) })
    .then(()=>loadVlmStatus()).catch(e=>console.warn('VLM 配置保存失败', e));
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
    if (typeof refreshModelCards === 'function') _renderModelCards('vlmModelCards', VLM_MODEL_CATALOG,
      (res.model||'').trim(), res.installed, res.pulling ? res.pull_model : null, 'pickVlmModel');
  }).catch(e=>console.warn('加载 VLM 状态失败', e));
}
// ---- 本地模型（文字解说）：网页内一键拉取 + 状态轮询 ----
// 模型选择卡：状态驱动——如实显示 使用中 / 已安装·点击启用 / 未安装·下载中
const LOCAL_MODEL_CATALOG = [
  {tag:'qwen3:14b-q4_K_M', label:'⭐ qwen3:14b', desc:'新一代 · 写作更强', size:'≈9.0GB'},
  {tag:'qwen2.5:14b',      label:'qwen2.5:14b', desc:'稳定基准（回退选）', size:'≈8.4GB'},
  {tag:'qwen3:8b',         label:'qwen3:8b', desc:'轻快省显存 · 共存更稳', size:'≈5.2GB'},
];
const VLM_MODEL_CATALOG = [
  {tag:'minicpm-v4.5:q8_0', label:'⭐ MiniCPM-V 4.5', desc:'视频理解专项·96x压缩·同显存多看10倍帧', size:'≈6.5GB'},
  {tag:'qwen3-vl:8b',      label:'qwen3-vl:8b', desc:'新一代视觉·综合强·剧情理解好', size:'≈6.1GB'},
  {tag:'qwen3-vl:30b',     label:'qwen3-vl:30b (MoE)', desc:'旗舰·激活3B速度快·需16GB显存', size:'≈20GB'},
  {tag:'qwen2.5vl:latest', label:'qwen2.5vl', desc:'稳定基准（回退选）', size:'≈5.6GB'},
];

const WHISPER_MODEL_CATALOG = [
  {tag:'tiny',            label:'tiny', desc:'最快·最糙·适合快速预览', size:'≈75MB'},
  {tag:'base',            label:'⭐ base', desc:'默认·平衡·纯CPU推荐', size:'≈150MB'},
  {tag:'small',           label:'small', desc:'更准·推荐·CPU也能跑', size:'≈500MB'},
  {tag:'medium',          label:'medium', desc:'很准·较慢·需GPU', size:'≈1.5GB'},
  {tag:'large-v3',        label:'large-v3', desc:'最准·GPU加速·12GB推荐', size:'≈3GB'},
  {tag:'distil-large-v3', label:'distil-large-v3', desc:'large-v3蒸馏·更快·精度接近', size:'≈1.5GB'},
];
function _renderModelCards(boxId, catalog, cur, installed, pullingModel, pickFn){
  const box = $(boxId); if(!box) return;
  box.innerHTML = catalog.map(m=>{
    const inst = (installed||[]).some(x => x === m.tag || x.indexOf(m.tag) === 0);
    const active = cur === m.tag;
    const pulling = pullingModel === m.tag;
    let state, btnCls, btn;
    if (pulling){ state = '⏳ 下载中…'; btnCls = 'btn mini'; btn = '下载中'; }
    else if (active){ state = '✅ 使用中'; btnCls = 'btn mini'; btn = '使用中'; }
    else if (inst){ state = '✅ 已安装'; btnCls = 'btn mini ghost'; btn = '点击启用'; }
    else { state = '⬜ 未安装'; btnCls = 'btn mini ghost'; btn = '⬇ 下载并启用'; }
    const rmBtn = (inst && !active) ? `<span style="display:inline-block;margin-top:4px;font-size:11px;color:#ef4444;cursor:pointer;text-decoration:underline;" onclick="event.stopPropagation();removeModel('${m.tag}')">卸载</span>` : '';
    return `<button class="${btnCls}" style="flex:1;min-width:150px;text-align:left;${active?'outline:2px solid #16a34a;':''}" onclick="${pickFn}('${m.tag}',${inst})" ${pulling?'disabled':''}>
      <span style="display:block;font-weight:600;">${m.label} · ${m.size}</span>
      <span style="display:block;font-size:12px;opacity:.75;">${m.desc}</span>
      <span style="display:block;font-size:12px;margin-top:2px;">${state}</span>${rmBtn}</button>`;
  }).join('');
}
function refreshModelCards(){
  fetch('/api/local/status').then(r=>r.json()).then(st=>{
    _renderModelCards('localModelCards', LOCAL_MODEL_CATALOG,
                      (st.model||'').trim(), st.installed, st.pulling ? st.pull_model : null, 'pickLocalModel');
  }).catch(()=>{});
  fetch('/api/vlm/status').then(r=>r.json()).then(st=>{
    _renderModelCards('vlmModelCards', VLM_MODEL_CATALOG,
                      (st.model||'').trim(), st.installed, st.pulling ? st.pull_model : null, 'pickVlmModel');
  }).catch(()=>{});
}
function pickLocalModel(tag, installed){
  const el = $('localModel'); if(!el) return;
  el.value = tag;
  saveAIConfig();          // 切换立即生效
  if(!installed) pullLocalModel();  // 未安装才触发拉取，已安装直接用
  refreshModelCards();
}
function pickVlmModel(tag, installed){
  const el = $('vlmModel'); if(!el) return;
  el.value = tag;
  saveVlm();               // 切换立即生效
  if(!installed) pullVlm();  // 未安装才触发拉取，已安装直接用
  refreshModelCards();
}
let _localPullTimer = null;
function pullLocalModel(){
  const btn = $('pullLocal'); if(btn){ btn.textContent='⏳ 拉取中…'; btn.disabled=true; }
  const bar = $('localPullBar'), pct = $('localPullPct');
  if(bar) bar.style.display = 'block';
  if($('localPullFill')) $('localPullFill').style.width = '0%';
  if(pct){ pct.style.display = 'block'; pct.textContent = ($('localModel').value.trim() || 'qwen2.5:latest') + '　0%　准备中…'; }
  fetch('/api/local/pull', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ model: $('localModel').value.trim() }) })
    .then(r=>r.json()).then(res=>{
      if(!res.ok){ $('localPullRes').textContent = '❌ ' + (res.message || res.error || '拉取未启动'); return; }
      if(_localPullTimer) clearInterval(_localPullTimer);
      _localPullTimer = setInterval(()=>{
        fetch('/api/local/status').then(r=>r.json()).then(st=>{
          const fill = $('localPullFill'), pct2 = $('localPullPct');
          if(fill) fill.style.width = Math.min(100, Number(st.pull_pct||0)) + '%';
          if(pct2) pct2.textContent = (st.pull_model||'') + '　' + Math.round(Number(st.pull_pct||0)) + '%　' + (st.pull_msg||'');
          if(!st.pulling){
            clearInterval(_localPullTimer); _localPullTimer = null;
            if(btn){ btn.textContent = st.pull_ok ? '✅ 拉取完成' : '📥 拉取模型'; btn.disabled = false; }
            loadLocalStatus();
            refreshModelCards();
          }
        }).catch(()=>{});
      }, 2000);
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
    if (typeof refreshModelCards === 'function') _renderModelCards('localModelCards', LOCAL_MODEL_CATALOG,
      (res.model||'').trim(), res.installed, res.pulling ? res.pull_model : null, 'pickLocalModel');
  }).catch(e=>console.warn('加载本地模型状态失败', e));
}
// ---- 本地视觉理解 VLM：网页内一键拉取（按钮此前未接，这里补上）----
let _vlmPullTimer = null;
function pullVlm(){
  const btn = $('pullVlm'); if(btn){ btn.textContent='⏳ 拉取中…'; btn.disabled=true; }
  const bar = $('vlmPullBar'), pct = $('vlmPullPct');
  if(bar) bar.style.display = 'block';
  if($('vlmPullFill')) $('vlmPullFill').style.width = '0%';
  if(pct){ pct.style.display = 'block'; pct.textContent = ($('vlmModel').value.trim() || 'qwen2.5vl:latest') + '　0%　准备中…'; }
  fetch('/api/vlm/pull', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ model: $('vlmModel').value.trim() }) })
    .then(r=>r.json()).then(res=>{
      if(!res.ok){ $('vlmStatus').textContent = '❌ ' + (res.message || res.error || '拉取未启动'); return; }
      // 启动轮询：每2秒刷新进度，直到拉取完成
      if(_vlmPullTimer) clearInterval(_vlmPullTimer);
      _vlmPullTimer = setInterval(()=>{
        fetch('/api/vlm/status').then(r=>r.json()).then(st=>{
          // 更新进度条
          const fill = $('vlmPullFill'), pct2 = $('vlmPullPct');
          if(fill) fill.style.width = Math.min(100, Number(st.pull_pct||0)) + '%';
          if(pct2) pct2.textContent = (st.pull_model||'') + '　' + Math.round(Number(st.pull_pct||0)) + '%　' + (st.pull_msg||'');
          if(!st.pulling){
            clearInterval(_vlmPullTimer); _vlmPullTimer = null;
            if(btn){ btn.textContent = st.pull_ok ? '✅ 拉取完成' : '📥 拉取模型'; btn.disabled = false; }
            loadVlmStatus();
            refreshModelCards();
          }
        }).catch(()=>{});
      }, 2000);
    }).catch(()=>{ if(btn){ btn.textContent='📥 拉取模型'; btn.disabled=false; } });
}


function saveVideoEnc(){
  const sel = $('videoEnc'); if(!sel) return;
  fetch('/api/ai/config', { method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ video: { encoder: sel.value } }) })
    .then(r=>r.json()).then(res=>{
      if($('videoEncInfo')){
        $('videoEncInfo').textContent = (res.ok ? '✅ 已保存，' : '❌ 保存失败，')
          + '当前生效：' + (res.video_encoder || 'CPU 软编 libx264');
      }
    }).catch(()=>{ if($('videoEncInfo')) $('videoEncInfo').textContent = '❌ 保存失败：无法连接服务'; });
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
    if(!res.ok || !res.result){ if(box) box.innerHTML = '<span class="hint">❌ 检测失败：'+escapeHtml(res.error||'未知错误')+'</span>'; return; }
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
      const href = safeUrl(m.url);   // 只放行 http(s)，其余不渲染成链接
      const link = href ? `<a class="open" target="_blank" rel="noopener noreferrer" href="${escapeHtml(href)}">↗ 打开</a>` : '';
      return `<div class="mirroritem${rec}">
        ${tag}
        <code id="${cid}">${escapeHtml(m.url)}</code>
        ${link}
        <button class="btn mini ghost cp" onclick="copyText('${cid}')">📋 复制</button>
        <div class="note">${escapeHtml(m.note||'')}</div>
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
    res.history.forEach((h, i) => {
      const d = document.createElement('div');
      d.className = 'item';
      const secs = h.duration || 0;
      const tag = [h.music?'🎵':'', h.voice?'🗣️':'', (h.w||'')+'x'+(h.h||'')].filter(Boolean).join(' ');
      const capTxt = (h.captions && h.captions.length) ? ('文案:' + h.captions.join(' / ')) : '';
      const missing = !!h.missing;
      d.innerHTML = `${h.cover?`<img class="thumb" src="/media/${h.cover}?t=${Date.now()}" alt="封面">`:''}<div class="meta"><div class="name">🕘 ${escapeHtml(h.time||'')} · ${escapeHtml(secs)}s ${escapeHtml(tag)}${missing?' <span style="color:#b45309;">⚠️ 成片文件已丢失</span>':''}</div>
        <div class="kind">${escapeHtml(capTxt)}</div></div>
        ${missing?'':`<a class="btn mini ghost" href="/media/${escapeHtml(h.file)}" download="spring-${escapeHtml(h.time||'')}.mp4">⬇ 下载</a>
        <button class="btn mini ghost cov" title="生成或重做该成片的封面">🖼 封面</button>
        ${(h.captions&&h.captions.length)?'<button class="btn mini ghost narredit" title="编辑解说词，只重生成修改的段落">✏️ 编辑</button>':''}`}
        <button class="btn mini del">🗑 删除</button>`;
      const dlBtn = d.querySelector('a');
      if(dlBtn) dlBtn.addEventListener('click', (e)=>{ e.preventDefault(); const a=e.currentTarget; a.href='/media/'+h.file+'?t='+Date.now(); a.click(); });
      d.querySelector('button.del').addEventListener('click', () => deleteHistory(h.file));
      const narrEditBtn = d.querySelector('button.narredit');
      if(narrEditBtn) narrEditBtn.addEventListener('click', () => {
        const runId = (h.file||'').split('/')[0];
        openNarrEdit(runId, h.captions||[]);
      });
      const covBtn = d.querySelector('button.cov');
      if(covBtn) covBtn.addEventListener('click', () => {
        let cb = document.getElementById('coverBox_h' + i);
        if (!cb) {
          cb = document.createElement('div');
          cb.id = 'coverBox_h' + i;
          cb.style.display = 'none'; cb.style.width = '100%';
          d.appendChild(cb);
        }
        const show = cb.style.display !== 'block';
        cb.style.display = show ? 'block' : 'none';
        if (show) makeCover(h.file, cb.id, (h.captions && h.captions[0]) || '成片封面');
      });
      box.appendChild(d);
    });
  }).catch(e=>console.warn('加载历史记录失败', e));
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
loadTtsLocal();        // 本地配音引擎状态与音色列表

function clearEmpty(){ $('emptyHint').style.display = ITEMS.length ? 'none' : ''; }

function isVideo(name){ return /\.(mp4|mov|webm|avi|mkv|m4v)$/i.test(name); }

function handle(file){
  if (!(file.type.startsWith('image/') || file.type.startsWith('video/') || isVideo(file.name))) return;
  const id = 'it' + Date.now() + Math.random().toString(36).slice(2,6);
  const dur = 3;  // 图片默认时长（原可配置项已从UI移除）
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
      <img class="thumb" src="${escapeHtml(it.url)}" alt="">
      <div class="meta"><div class="name">${escapeHtml(it.name)}</div><div class="kind">${kindTxt}</div></div>
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
let _stopFlag = false;   // 用户点了「⏹ 停止」：轮询不再覆盖状态文案，等后端中断后收尾
function setBar(p){ const b=$('bar').querySelector('i'); $('bar').style.display='block'; b.style.width=Math.min(100,Math.max(1,p))+'%'; }
function stopBar(){ clearInterval(_tickTimer); $('bar').style.display='none'; }

// 统一的中途停止入口：各页面的「⏹ 停止」都调它，状态写到对应卡片而不是顶部全局 status
function stopRun(statusId){
  const el = statusId ? $(statusId) : null;
  if(!_currentRunid){
    if(el) el.textContent = 'ℹ️ 当前没有正在生成的任务';
    return;
  }
  if(_stopFlag){ if(el) el.textContent = '⏹ 正在停止…请稍候'; return; }
  _stopFlag = true;
  if(el) el.textContent = '⏹ 正在停止…（当前阶段跑完即中断）';
  fetch('/api/cancel', { method:'POST', headers:{'Content-Type':'application/json'},
                         body: JSON.stringify({runid:_currentRunid}) })
    .then(()=>{ if(el) el.textContent = '⏹ 已下达停止指令，正在中断当前阶段…'; })
    .catch(()=>{ if(el) el.textContent = '⚠️ 停止指令发送失败，请检查服务是否在运行'; });
}
// 兼容旧调用（一键合成等页面）
function cancelRun(){ stopRun('status'); }
function cancelBuild(){ stopRun('status'); }

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
    if (chat){
      const chatOk = s.chat || s.local;
      chat.className = 'aichip ' + (chatOk ? 'ok' : 'no');
      chat.textContent = s.chat ? '☁️ 云端LLM 已配置' : (s.local ? '🖥 本地LLM 已就绪' : '🤖 真AI(LLM) 未配置'); }
    if (vis){
      const visOk = s.vision || s.vlm_ready;
      vis.className = 'aichip ' + (visOk ? 'ok' : 'no');
      vis.textContent = s.vision ? '☁️ 云端视觉 已配置' : (s.vlm_ready ? '📷 本地视觉 已就绪' : '👁 画面描述 未配置'); }
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
  }).catch(e=>console.warn('加载 AI 就绪状态失败', e));
}

// 解说模型引导：提示 qwen2.5vl 的局限 + 引导部署「文字模型」写剧情解说
function renderNarrGuide(s){
  const ng = $('narrGuide');
  if (!ng) return;
  const g = s.narr_guide || {};
  const esc = escapeHtml;   // 统一转义入口（escapeHtml 额外转义引号，更安全）
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
      // 注：「省流/智能」模式选择器已于第 26 轮移除，改为后端自动选路（本地优先、配了 key 才用云端），
      // 故不再读取 narMode/movieMode/eco；此处只对「主动勾选了画面描述」这类仍存在的开关做前置确认。
      const aiCap = $('aiCap');
      if (task === 'build' && aiCap && aiCap.checked && !s.vision && !s.vlm_ready){ missing = '画面描述(Vision)'; explicit = true; }
      else if (task === 'instruct' && !s.chat && !s.local){ missing = '真AI 解说(LLM)'; explicit = false; }
      if (missing){
        _pendingGen = resolve;  // 点「仍用免费生成」时 resolve(true)
        // 根据缺少的配置类型决定「去配置」跳转到云端还是本地
        _pfTarget = (explicit && (missing.indexOf('LLM') >= 0 || missing.indexOf('Vision') >= 0 || missing.indexOf('真AI') >= 0)) ? 'cloud' : 'local';
        $('pfTitle').textContent = (explicit ? '已选真AI 但未配置 ' : '尚未配置 ') + missing + ' API';
        $('pfMsg').innerHTML = explicit
          ? '你选择了「<b>真AI</b>」模式，但 <b>' + missing + ' API</b> 还没配置，无法生成。<br>请先去「🤖 AI 配置」填好 Key；不填也没关系，会自动使用免费本地路径。'
          : '你还没有配置 <b>' + missing + ' API</b>。直接点「生成」会用 <b>本地离线模式</b>：本地 faster-whisper 识别真实台词 + SAPI 免费配音（不调任何付费接口，有显卡会用 GPU 加速）。<br>想让免费模式解说词更聪明：在「🤖 AI 配置 → ③ 本地模型」部署本机 Ollama+qwen 离线改写；或在「⑤ 本地视觉理解 VLM」部署 qwen2.5vl，会自动<b>逐段看画面</b>生成真解说（仍不花一分钱）；或点「仍用本地离线生成」继续。';
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
  _gSmoothedTotal = null;
  _stopFlag = false;   // 新任务开始：清掉上一次的停止标记，否则状态文案会被锁住
  $('gprogLabel').textContent = label || '处理中…';
  $('gprogPct').textContent = '0%';
  $('gprogPhase').textContent = '';
  $('gprogFill').style.width = '3%';
  $('gprog').classList.add('show');
}
function gSet(pct, phase){
  const gc = $('gprogCancel'); if (gc) gc.style.display = _currentRunid ? '' : 'none';
  pct = Math.min(99, Math.max(1, pct || 0));
  $('gprogFill').style.width = pct + '%';
  $('gprogPct').textContent = Math.floor(pct) + '%';
  const el = (Date.now() - _gStart) / 1000;
  let txt = phase || '';
  if (pct > 3 && el > 5){
    // 平滑ETA：用指数移动平均估计总时长，避免进度跳变导致ETA猛增猛减
    const rawTotal = el / (pct / 100);
    if (typeof _gSmoothedTotal === 'undefined' || _gSmoothedTotal === null) _gSmoothedTotal = rawTotal;
    else _gSmoothedTotal = _gSmoothedTotal * 0.85 + rawTotal * 0.15;
    const eta = Math.max(0, _gSmoothedTotal - el);
    // ETA超过10分钟显示分钟，否则秒
    if (eta > 600) txt += (txt ? ' · ' : '') + '预计还需 ' + Math.ceil(eta/60) + '分钟';
    else txt += (txt ? ' · ' : '') + '预计还需 ' + Math.ceil(eta) + 's';
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
  const c = classifyError(msg, null);
  $('gprogLabel').textContent = c.head + (c.kind === 'other' && msg ? '：' + String(msg).slice(0, 40) : '');
  $('gprogPhase').textContent = '';
  setTimeout(() => $('gprog').classList.remove('show'), 2800);
}

// ---- 任务失败分级呈现：人话首行 + 可展开技术细节 + 一键复制 ----
// 后端若已给出 error_kind / error_stage / error_detail 则优先使用，否则按 message 关键字归类
const ERR_RULES = [
  { kind:'cancel',  re:/取消|cancel(led)?/i,
    head:'⏹ 已取消', tip:'任务已停止，不会产生新的成片。想继续的话重新发起即可。' },
  { kind:'busy',    re:/并发上限|已达上限|正在运行|已有任务|queue is full|too many/i,
    head:'⏳ 同时只能跑一个任务', tip:'已经有任务在跑了。请等它完成，或先点「⏹ 取消」再重新发起。' },
  { kind:'lost',    re:/失去连接|服务可能已重启/i,
    head:'🔌 与服务失去连接', tip:'后台服务可能已重启或崩溃。请刷新页面后重新发起任务。' },
  { kind:'timeout', re:/等待超时|轮询超时|timed out|read timeout/i,
    head:'⏳ 等待超时', tip:'等待时间超过了上限（任务可能仍在后台进行）。请稍后到「⑨记录」查看结果，或重新发起。' },
  { kind:'font',    re:/豆腐块|中文字形|中文字体|字体缺失|no cjk|cjk font|glyph/i,
    head:'🔤 缺少中文字体（画面文字变成方框）', tip:'请把一个中文字体（如思源黑体、微软雅黑 msyh.ttc）放进项目 assets/fonts 目录，或在「🤖 AI 配置」里改指字体后重试。' },
  { kind:'frame',   re:/抽帧|ffmpeg|exit code|invalid data|out of range|超出范围|编码失败|no such filter|moov atom/i,
    head:'🎞️ 素材抽帧 / 编码失败', tip:'素材可能已损坏，或抽帧的时间点超出了视频长度。换个时间点，或重新选一段素材再试。' },
  { kind:'model',   re:/whisper|ollama|vlm|qwen|权重|未下载|未部署|model not found|no such model|connection refused/i,
    head:'🧠 本地模型未就绪', tip:'任务需要的本地模型还没下载/启动。请到「🤖 AI 配置 → 🖥 本地离线模型」点「下载 / 部署」，完成后再重试。' },
  { kind:'disk',    re:/空间不足|磁盘|no space|not enough space/i,
    head:'💾 磁盘空间不足', tip:'请到「🧹 存储」页清理可回收的临时文件，腾出空间后重试。' },
  { kind:'net',     re:/超时|timeout|连接失败|connection|网络|api ?key|401|403|429|quota|unauthorized|ssl|proxy/i,
    head:'🌐 AI / 网络请求失败', tip:'请检查网络连通性，以及「🤖 AI 配置」里的接口地址与 API Key 是否正确（Key 无效或欠费也会报这类错）。' },
];
function classifyError(msg, p){
  const raw = String(msg == null ? '' : msg);
  let kind = (p && p.error_kind) ? String(p.error_kind) : '';
  if (!kind){
    for (const r of ERR_RULES){ if (r.re.test(raw)){ kind = r.kind; break; } }
  }
  const rule = ERR_RULES.filter(r => r.kind === kind)[0];
  if (rule) return { kind: rule.kind, head: rule.head, tip: rule.tip };
  return { kind:'other', head:'❌ 任务失败',
           tip:'遇到了未归类的问题。点下面的「📋 复制错误信息」把详情发给作者，能最快定位。' };
}
function showTaskError(el, err, detail){
  if (!el) return;
  const p = (detail && typeof detail === 'object') ? detail : null;
  const msg = String(err == null ? '' : err);
  const c = classifyError(msg, p);
  const tech = p ? (p.error_detail || msg) : (detail ? String(detail) : msg);
  const stage = p ? (p.error_stage || p.phase || '') : '';
  el.innerHTML = '';
  const box = document.createElement('div');
  box.className = 'taskerr' + (c.kind === 'cancel' ? ' cancel' : '');
  let html = '<div class="te-head">' + escapeHtml(c.head) + '</div>'
           + '<div class="te-tip">' + escapeHtml(c.tip) + '</div>';
  if (c.kind === 'other' && msg) html += '<div class="te-raw">' + escapeHtml(msg) + '</div>';
  if (stage) html += '<div class="te-stage">出错阶段：' + escapeHtml(stage) + '</div>';
  html += '<details class="te-detail"><summary>🔍 技术细节</summary><pre class="te-pre"></pre></details>'
        + '<button type="button" class="btn mini ghost te-copy">📋 复制错误信息</button>';
  box.innerHTML = html;
  box.querySelector('.te-pre').textContent = tech || msg || '（后端未返回详细信息）';
  const report = '【一帧成片·错误反馈】\n提示：' + c.head + '\n建议：' + c.tip
    + (stage ? '\n出错阶段：' + stage : '')
    + '\n技术细节：\n' + (tech || msg || '（无）');
  const btn = box.querySelector('.te-copy');
  const pre = box.querySelector('.te-pre');
  const det = box.querySelector('.te-detail');
  btn.addEventListener('click', () => {
    copyString(report).then(ok => {
      if (ok){ btn.textContent = '✅ 已复制'; }
      else { det.open = true; _selectNode(pre); btn.textContent = '⚠️ 复制失败，请手动复制（已选中）'; }
      setTimeout(() => { btn.textContent = '📋 复制错误信息'; }, 2200);
    });
  });
  el.appendChild(box);
}
function _selectNode(node){
  try {
    const rg = document.createRange(); rg.selectNodeContents(node);
    const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(rg);
  } catch(e){}
}
// 复制：navigator.clipboard → execCommand 兜底 → 返回 false 交由调用方提示手动复制
function copyString(text){
  if (navigator.clipboard && navigator.clipboard.writeText){
    return navigator.clipboard.writeText(text).then(() => true).catch(() => Promise.resolve(_legacyCopy(text)));
  }
  return Promise.resolve(_legacyCopy(text));
}
function _legacyCopy(text){
  try {
    const ta = document.createElement('textarea');
    ta.value = text; ta.setAttribute('readonly', '');
    ta.style.position = 'fixed'; ta.style.top = '-1000px'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(ta);
    return !!ok;
  } catch(e){ return false; }
}

// ---- CC.BY 音乐署名（任务完成时展示；credits 为空则不渲染）----
function renderCredits(hostId, credits){
  const host = $(hostId);
  if (!host) return;
  const cid = hostId + '_credits';
  let box = $(cid);
  const text = String(credits == null ? '' : credits).trim();
  if (!text){ if (box) box.remove(); return; }
  if (!box){
    box = document.createElement('div');
    box.id = cid; box.className = 'credits';
    host.appendChild(box);
  }
  box.innerHTML = '<div class="cr-title">🎵 音乐署名（CC.BY 协议要求）</div>'
    + '<pre class="cr-text"></pre>'
    + '<button type="button" class="btn mini ghost cr-copy">📋 复制署名</button>'
    + '<div class="cr-note">发布到公开平台时，请把上面的署名放进视频简介或片尾。</div>';
  box.querySelector('.cr-text').textContent = text;
  const btn = box.querySelector('.cr-copy');
  btn.addEventListener('click', () => {
    copyString(text).then(ok => {
      btn.textContent = ok ? '✅ 已复制' : '⚠️ 复制失败，请手动选中复制';
      setTimeout(() => { btn.textContent = '📋 复制署名'; }, 1800);
    });
  });
}
function escapeHtml(s){
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
// URL 白名单：只放行 http(s) 与站内相对路径（排除 //host 协议相对地址），其余返回空串 → 调用方不渲染该链接
function safeUrl(u){
  const s = String(u == null ? '' : u).trim();
  if (!s) return '';
  if (/^https?:\/\//i.test(s)) return s;
  if (s.indexOf('//') === 0) return '';
  if (s.indexOf('/') === 0) return s;
  return '';
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
    html += '<video class="pvideo" src="' + escapeHtml(partial.best_video) + '?t=' + Date.now() + '" controls playsinline></video>';
  }
  html += '<div class="plist">';
  (partial.files || []).forEach(f => {
    const sz = (f.size / 1024).toFixed(0) + ' KB';
    const tag = { video:'🎬', audio:'🎵', subtitle:'📝', text:'📄', file:'📎' }[f.kind] || '📎';
    html += '<div class="pitem"><span class="ptag">' + tag + '</span>'
          + '<a href="' + escapeHtml(f.url) + '" download="' + escapeHtml(f.name) + '" target="_blank">' + escapeHtml(f.name) + '</a>'
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
    '<div class="pvhead"><span>✅ ' + escapeHtml(name || '已生成') + '</span><button class="pvclose">✕</button></div>' +
    '<video src="' + escapeHtml(url) + '" controls playsinline></video>' +
    '<div class="pvfoot"><a class="btn mini ghost" href="' + escapeHtml(url) + '" download="' + escapeHtml(name || 'video.mp4') + '">💾 保存</a></div>';
  card.querySelector('.pvclose').addEventListener('click', () => { card.remove(); if (!dock.children.length) dock.classList.remove('show'); });
  dock.appendChild(card);
  dock.classList.add('show');
  while (dock.children.length > 3) dock.firstElementChild.remove();
}

// ---- 顶部步骤导航：按执行步骤切换页面（卡片按 data-step 分组显示/隐藏） ----
let _tasksTimer = null;   // 任务中心轮询定时器（声明在最前，避免 showStep 中 TDZ 报错）
const STEP_CARDS = {
  start: ['guideCard', 'smartCard', 'instructCard'],
  upload: ['drop'],
  music: ['musicCard', 'libCard'],
  beatcut: ['beatcutCard'],
  narrate: ['narCard', 'movieCard'],
  adjust: ['adjustCard'],
  ai: ['aiCard'],
  output: ['timelineCard', 'outCard'],
  build: ['buildCard'],
  tasks: ['tasksCard'],
  history: ['histCard'],
  storage: ['storageCard'],
};
function showStep(step){
  if (!STEP_CARDS[step]) return;
  Object.entries(STEP_CARDS).forEach(([s, ids]) => {
    const on = (s === step);
    ids.forEach(id => { const el = document.getElementById(id); if (el) el.classList.toggle('active', on); });
  });
  document.querySelectorAll('.stepbtn').forEach(b => b.classList.toggle('active', b.dataset.step === step));
  try { localStorage.setItem('springStudio.lastStep', step); } catch(e){}
  // 任务中心：进入时启动轮询，离开时停止（try/catch防御：出错不影响页面切换）
  try {
    if(step === 'tasks'){
      if(typeof loadTasks === 'function') loadTasks();
      if(_tasksTimer) clearInterval(_tasksTimer);
      _tasksTimer = setInterval(function(){ try{ if(typeof loadTasks === 'function') loadTasks(); }catch(e){} }, 2000);
    } else if(_tasksTimer){
      clearInterval(_tasksTimer); _tasksTimer = null;
    }
  } catch(e){ console.warn('任务中心轮询出错:', e); }
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
  document.querySelectorAll('.stepbtn[data-step="storage"]').forEach(b => b.addEventListener('click', loadStorage));
  if (init === 'storage') loadStorage();
})();
function scrollTop2(){ window.scrollTo({ top: 0, behavior: 'smooth' }); }

// ---- 任务中心：多任务实时进度 ----
// （_tasksTimer 已在顶部声明，showStep 中的轮询逻辑已直接写入 showStep 函数）
function loadTasks(){
  fetch('/api/tasks').then(r=>r.json()).then(res=>{
    const tasks = res.tasks || [];
    const running = tasks.filter(t=>!t.done && !t.error).length;
    const done = tasks.filter(t=>t.done && !t.error).length;
    const failed = tasks.filter(t=>t.error).length;
    if($('tasksRunning')) $('tasksRunning').textContent = running;
    if($('tasksDone')) $('tasksDone').textContent = done;
    if($('tasksFailed')) $('tasksFailed').textContent = failed;

    const list = $('tasksList');
    if(!list) return;
    if(tasks.length === 0){
      list.innerHTML = '<div class="hint" style="text-align:center;padding:30px;">暂无任务，提交任务后这里会显示实时进度</div>';
      return;
    }
    list.innerHTML = tasks.map(t=>{
      const isRunning = !t.done && !t.error;
      const isFailed = !!t.error;
      const pct = Math.min(100, Math.max(0, Number(t.pct)||0));
      let statusCls = isRunning ? 'task-running' : (isFailed ? 'task-failed' : 'task-done');
      let statusText = isRunning ? '⏳ 运行中' : (isFailed ? '❌ 失败' : '✅ 已完成');
      let phaseText = t.phase || '';
      let actions = '';
      if(isRunning){
        actions = '<button class="btn mini danger" onclick="cancelTask(\''+t.runid+'\')">⏹ 取消</button>';
      } else if(t.done && t.file){
        actions = '<a class="btn mini" href="/media/'+t.file+'" target="_blank">▶ 查看成片</a>';
      }
      if(isFailed && t.error){
        phaseText = '错误：' + String(t.error).slice(0,100);
      }
      return '<div class="task-card '+statusCls+'">'
        + '<div class="task-head">'
        + '<span class="task-id">'+t.runid+'</span>'
        + '<span class="task-status">'+statusText+'</span>'
        + '</div>'
        + '<div class="task-phase">'+phaseText+'</div>'
        + '<div class="task-bar"><div class="task-fill" style="width:'+pct+'%"></div></div>'
        + '<div class="task-foot">'
        + '<span class="task-pct">'+Math.round(pct)+'%</span>'
        + actions
        + '</div></div>';
    }).join('');
  }).catch(()=>{});
}
function cancelTask(runid){
  if(!confirm('确定取消任务 '+runid+'？')) return;
  fetch('/api/cancel', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({runid: runid})})
    .then(r=>r.json()).then(()=>{ setTimeout(loadTasks, 500); })
    .catch(()=>{});
}
// 顶部进度条点击跳任务中心
document.addEventListener('DOMContentLoaded', () => {
  const gp = document.getElementById('gprog');
  if(gp) gp.addEventListener('click', () => showStep('tasks'));
});

// ---- 存储管理面板：展示占用 + 用户自主删除（不自动清理） ----
function fmtBytes(n){
  n = Number(n) || 0;
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1){ n /= 1024; i++; }
  return (i === 0 ? n : n.toFixed(n < 10 ? 2 : 1)) + ' ' + u[i];
}

function loadStorage(){
  const sum = $('storageSummary');
  const list = $('storageList');
  if (sum) sum.textContent = '扫描中…';
  fetch('/api/storage').then(r => r.json()).then(d => {
    if (!d.ok){ if (sum) sum.textContent = '扫描失败：' + (d.error || ''); return; }
    const tierBadge = { keep: '🔒 保留', safe: '🟢 可清理', review: '🟡 删除需重下' };
    const tierCls = { keep: 'st-keep', safe: 'st-safe', review: 'st-review' };
    let html = `<div class="st-summary">项目占用 <b>${fmtBytes(d.total_bytes)}</b> · 可回收 <b style="color:#1a7f37">${fmtBytes(d.reclaimable_bytes)}</b> · 磁盘剩余 <b>${fmtBytes(d.free_bytes)}</b></div>`;
    (d.groups || []).forEach(g => {
      if (g.total === 0 && g.items.length === 0) return;
      html += `<div class="st-group"><div class="st-group-head"><span class="st-badge ${tierCls[g.tier] || ''}">${tierBadge[g.tier] || escapeHtml(g.tier)}</span><b>${escapeHtml(g.label)}</b><span class="st-size">${fmtBytes(g.total)}</span></div>`;
      if (g.items && g.items.length){
        html += '<div class="st-items">';
        g.items.forEach(it => {
          // 用 data-* + 监听器传参：路径里的引号不会破坏 onclick 的 JS 字符串
          const del = g.deletable
            ? `<button class="btn mini danger st-del" data-rel="${escapeHtml(it.rel)}" data-size="${escapeHtml(it.size)}">🗑 删除</button>`
            : '<span class="hint">不可删</span>';
          html += `<div class="st-item"><span class="st-name" title="${escapeHtml(it.rel)}">${escapeHtml(it.name)}</span><span class="st-size">${fmtBytes(it.size)}</span>${del}</div>`;
        });
        html += '</div>';
      }
      html += '</div>';
    });
    if (list) list.innerHTML = html;
    if (list) list.querySelectorAll('.st-del').forEach(b => {
      b.addEventListener('click', () => deleteStorageItem(b.dataset.rel, Number(b.dataset.size) || 0));
    });
    if (sum) sum.innerHTML = `项目占用 <b>${fmtBytes(d.total_bytes)}</b> · 可回收 <b style="color:#1a7f37">${fmtBytes(d.reclaimable_bytes)}</b> · 磁盘剩余 <b>${fmtBytes(d.free_bytes)}</b>`;
    const rec = (d.groups || []).filter(g => g.deletable && g.tier === 'safe').reduce((a, g) => a + g.total, 0);
    const btn = $('storageCleanAll');
    if (btn){ btn.dataset.amount = rec; btn.textContent = `🧹 一键清理可回收项（释放约 ${fmtBytes(rec)}）`; }
  }).catch(e => { if (sum) sum.textContent = '请求失败：' + e; });
}

function deleteStorageItem(rel, size){
  rel = String(rel == null ? '' : rel);
  const name = rel.split('/').pop();
  if (!confirm(`确认删除「${name}」（${fmtBytes(size)}）？\n该操作不可恢复。`)) return;
  fetch('/api/storage/delete', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path: rel })
  }).then(r => r.json()).then(d => {
    if (d.ok) loadStorage();
    else alert('删除失败：' + (d.error || ''));
  }).catch(e => alert('删除失败：' + e));
}

function cleanStorageAll(){
  const amount = Number(($('storageCleanAll') || {}).dataset ? ($('storageCleanAll').dataset.amount || 0) : 0);
  if (!confirm(`将删除全部「🟢 可清理」类临时文件（run-* 残留、上传会话、ASR/音乐临时、分析缓存），释放约 ${fmtBytes(amount)}。\n⚠️ 模型权重(🟡)与成片(🔒)不会被删。此操作不可恢复，确认？`)) return;
  fetch('/api/storage').then(r => r.json()).then(d => {
    const safe = (d.groups || []).filter(g => g.deletable && g.tier === 'safe');
    const rels = [];
    safe.forEach(g => (g.items || []).forEach(it => rels.push(it.rel)));
    if (!rels.length){ alert('没有可清理项'); return; }
    Promise.all(rels.map(rel => fetch('/api/storage/delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: rel })
    }).then(r => r.json()).then(x => ({ rel, ok: x.ok, err: x.error }))
      .catch(e => ({ rel, ok: false, err: String(e) })))
    ).then(results => {
      const fails = results.filter(r => !r.ok);
      loadStorage();
      if (fails.length) alert('完成，但 ' + fails.length + ' 项删除失败：\n' + fails.map(f => f.rel + '：' + (f.err || '')).join('\n'));
    });
  });
}

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
// 注：cancelRun / cancelBuild 已上移为 stopRun 的兼容别名（见文件顶部「中途停止」区），此处不再重复定义

// 大视频走分片上传（>64MB）：旧路径把整文件 base64 塞进一个 JSON，体积膨胀 1.37 倍且有内存峰值；
// 分片路径每片 4MB、3 路并发提交，后端按序合并。小文件维持 base64 旧路径（少 3 个请求）。
// 断点续传：同一文件（名+大小+mtime）的会话 id 记在 localStorage——刷新页面/换标签页后已传分片不重传；
// 会话在服务端磁盘上，服务重启也能续传（24h 内）。续传键只保留最近 6 个，防 localStorage 堆积。
async function uploadChunksOnly(file){
  const KEY = 'springStudio.up.' + file.name + '.' + file.size + '.' + (file.lastModified||0);
  let uid = null;
  try { uid = localStorage.getItem(KEY) || null; } catch(e){}
  let init = await fetch('/api/upload/init',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:file.name,size:file.size, upload_id: uid})}).then(r=>r.json());
  if(!init.ok && uid){
    try { localStorage.removeItem(KEY); } catch(e){}
    uid = null;   // 会话过期/被清理 → 重新开会话
    init = await fetch('/api/upload/init',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:file.name,size:file.size})}).then(r=>r.json());
  }
  if(!init.ok) throw new Error(init.error||'上传初始化失败');
  uid = init.upload_id;
  const have = init.have || [];
  try {
    localStorage.setItem(KEY, uid);
    const ks = Object.keys(localStorage).filter(k => k.indexOf('springStudio.up.') === 0);
    for (let k = 0; k < ks.length - 6; k++) { try { localStorage.removeItem(ks[k]); } catch(e){} }
  } catch(e){}
  const CH=4*1024*1024, n=Math.ceil(file.size/CH);
  const todo = [];
  for(let i=0;i<n;i++) if(have.indexOf(i) < 0) todo.push(i);
  const total = todo.length;
  let doneN = 0, failed = null;
  // 带超时的fetch：60秒超时，超时自动重试（最多3次），避免某片挂住导致整体卡死
  async function fetchWithTimeout(url, opts, timeoutMs){
    const ctrl = new AbortController();
    const timer = setTimeout(()=>ctrl.abort(), timeoutMs);
    try { return await fetch(url, {...opts, signal: ctrl.signal}); }
    finally { clearTimeout(timer); }
  }
  async function uploadOneChunk(i, attempt){
    const blob = file.slice(i*CH, (i+1)*CH);
    const fd = new FormData();
    fd.append('upload_id', uid);
    fd.append('idx', i);
    fd.append('chunk', blob, 'part_' + i);
    const r = await fetchWithTimeout('/api/upload/chunk', {method:'POST', body: fd}, 60000);
    const j = await r.json();
    if(!j.ok) throw new Error(j.error||('分片'+(i+1)+'上传失败'));
  }
  // 3 路并发上传
  async function worker(){
    while(todo.length && !failed){
      const i = todo.shift();
      let ok = false;
      for(let attempt = 1; attempt <= 3 && !ok && !failed; attempt++){
        try {
          await uploadOneChunk(i, attempt);
          ok = true;
        } catch(e){
          if(attempt < 3){
            gSet(2 + Math.round(doneN*20/total), '📤 视频上传中 ' + doneN + '/' + total + '（第'+(i+1)+'片重试 '+attempt+'/3）');
            await new Promise(r=>setTimeout(r, 500*attempt));
          } else if(!failed){
            failed = e;
          }
        }
      }
      if(ok && !failed){
        doneN++;
        gSet(2 + Math.round(doneN*20/total), '📤 视频上传中 ' + doneN + '/' + total + (have.length ? '（已续传 ' + have.length + ' 片）' : ''));
      }
    }
  }
  await Promise.all([worker(), worker(), worker()]);
  if(failed) throw failed;
  // 合并阶段：大文件合并需要几秒到几十秒，单独显示进度避免"假死"
  gSet(22, '📦 正在合并视频文件（' + (file.size/1024/1024).toFixed(0) + 'MB）…');
  const fin = await fetchWithTimeout('/api/upload/done', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({upload_id:uid, name:file.name, chunks:n})}, 120000).then(r=>r.json());
  if(!fin.ok) throw new Error(fin.error||'上传收尾失败');
  try { localStorage.removeItem(KEY); } catch(e){}
  return uid;
}

async function videoToBody(file){
  if(file.size <= 64*1024*1024){
    return { name:file.name, data: toB64(new Uint8Array(await file.arrayBuffer())) };
  }
  const uid = await uploadChunksOnly(file);
  return { name:file.name, upload_id: uid };
}

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
  if(BC_VIDEO.size > 2*1024*1024*1024){ $('bcStatus').textContent='❌ 视频过大（'+(BC_VIDEO.size/1073741824).toFixed(1)+'GB > 2GB），请先剪辑或压缩后再生成'; return; }
  if(!MUSIC){ $('bcStatus').innerHTML='❌ 请先选择背景音乐 — <a href="javascript:void(0)" onclick="goStep(\'musicCard\')" style="color:#1d4ed8;text-decoration:underline;">🎵 去选音乐</a> 或 <a href="javascript:void(0)" onclick="goStep(\'libCard\')" style="color:#1d4ed8;text-decoration:underline;">🔎 免费曲库</a>';
    const h=$('bcMusicHint'); if(h){ h.style.background='#fef2f2'; h.style.borderColor='#fca5a5'; h.style.color='#991b1b'; setTimeout(()=>updateMusicHint(),1500); } return; }
  const go=$('bcQuickBtn')||$('bcGo'); go.disabled=true; $('bcResult').style.display='none';
  const syncSt=$('bcSyncStatus'); if(syncSt){ syncSt.style.display='none'; syncSt.style.background=''; syncSt.textContent=''; }
  $('bcStatus').textContent='上传视频…';
  gStart('⚡ 智能强卡点');
  const params={w:1280,h:720,fps:30,sceneTh:0.30,maxCuts:30, strength: $('bcStrength').value, skipHead: parseFloat(($('bcSkipHead')||{}).value) || 3.0};
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
  const videoObj = BC_VIDEO.mlib ? {name: BC_VIDEO.name, mlib: BC_VIDEO.mlib} : await videoToBody(BC_VIDEO);
  const body = { video: videoObj, music:null, params };
  if(MUSIC.catalogId){ body.music={source:'catalog', catalogId:MUSIC.catalogId}; }
  else { body.music={name:MUSIC.name, data: toB64(new Uint8Array(await MUSIC.file.arrayBuffer()))}; }
  try{
    const r=await fetch('/api/beatcut',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const out=await r.json();
    if(!out.ok) throw new Error(out.error||'失败');
    _bcRunid=out.runid;
    await pollBeatCut(_bcRunid);
  }catch(e){ $('bcStatus').textContent='❌ '+netErrMsg(e); }
  go.disabled=false;
}
function pollBeatCut(runid){
  return new Promise(resolve=>{
    let _errs = 0;   // 连续失败计数：服务重启/断网时明确报错，不永久转圈
    _currentRunid = runid; const cb=$('bcCancel'); if(cb) cb.style.display='';
    const iv=setInterval(()=>{
      fetch('/api/progress?run='+runid).then(r=>r.json()).then(p=>{
        const b=$('bcBar').querySelector('i');
        $('bcBar').style.display='block';
        if(p.pct) b.style.width=Math.min(100,p.pct)+'%';
        gSet(p.pct, p.phase);
        if(p.done){
          clearInterval(iv); $('bcBar').style.display='none';
          _currentRunid=null; if(cb) cb.style.display='none';
          if(p.error){ renderPartial('bcPartial', p.partial); showTaskError($('bcStatus'), p.error, p); gErr(p.error); resolve(); return; }
          $('bcStatus').textContent='✅ 完成'; gDone();
          $('bcResult').style.display='block';
          $('bcPlayer').src='/media/'+p.file+'?t='+Date.now();
          setModeBadge('bcMode', p.mode);
          gPreview(p.file, '强卡点短片');
          $('bcDl').href='/media/'+p.file;
          renderCredits('bcResult', p.credits);
          _coverCtx.bc = {file: p.file}; const _ccb=$('bcCoverBtn'); if(_ccb) _ccb.style.display='';
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
      }).catch(()=>{ if(++_errs>=8){ clearInterval(iv); $('bcBar').style.display='none'; _currentRunid=null; if(cb) cb.style.display='none'; $('bcStatus').textContent='❌ 与服务失去连接（服务可能已重启），请重新发起'; gErr('与服务失去连接'); resolve(); } });
    },400);
    setTimeout(()=>{ clearInterval(iv); _currentRunid=null; if(cb) cb.style.display='none'; $('bcStatus').textContent='⚠️ 等待超时已停止刷新（任务可能仍在后台进行），请稍后到「⑨记录」查看结果'; gErr('等待超时'); resolve(); }, 1800000);
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
  $('narInfo').textContent = '已选视频，点「生成解说」开始（默认免费配音）。';
}
// ---- 🔎 DeepSeek 直达：复制「查剧情」提问模板 + 打开 DeepSeek，方便搜剧情概况 ----
function _deepSeekPrompt(which){
  const isMovie = (which === 'movie');
  const nameEl = isMovie ? $('movieName') : $('narTheme');
  const v = isMovie ? MOVIE_VIDEO : NAR_VIDEO;
  let key = ((nameEl && nameEl.value) || '').trim();
  if(!key && v && v.name) key = String(v.name).replace(/\.[^.]+$/, '').replace(/[_\-]+/g, ' ').trim();
  if(key.length > 24){
    // 主题框里写的是长描述而非片名 → 直接围绕它整理
    return '我要为下面这段视频写解说词，请帮我把它的剧情/内容按时间顺序整理成 8-12 幕，'
      + '每一幕用一到两句话概括关键情节，不要写评价：\n' + key;
  }
  return key
    ? '请给我《' + key + '》的详细剧情梗概（请开启联网搜索，参考豆瓣/百科）：按时间顺序分 8-12 幕，'
      + '每一幕用一到两句话概括关键情节，人物给出姓名与身份，不要剧透结局，不要写评价，直接输出分幕正文。'
    : '请给我一部影视作品的剧情梗概（我稍后补充片名）：按时间顺序分 8-12 幕，每幕一到两句话概括关键情节，不要剧透结局。';
}
function openDeepSeek(which){
  const isMovie = (which === 'movie');
  const prompt = _deepSeekPrompt(which);
  const win = window.open('https://chat.deepseek.com/', '_blank', 'noopener');
  const hintEl = $(isMovie ? 'movieDsHint' : 'narDsHint');
  copyString(prompt).then(ok => {
    if(!hintEl) return;
    if(ok){
      hintEl.innerHTML = '✅ 提问模板已复制。在 DeepSeek 里 <b>Ctrl+V 粘贴发送</b>（记得开「联网搜索」），把回复的剧情粘回左边输入框。';
    }else{
      hintEl.innerHTML = '⚠️ 自动复制失败，请手动复制下面这句提问：<br><code style="word-break:break-all;">'
        + escapeHtml(prompt) + '</code>';
    }
  });
  if(!win && hintEl){
    hintEl.innerHTML = '⚠️ 新标签被浏览器拦截了，请<a href="https://chat.deepseek.com/" target="_blank" rel="noopener">手动打开 DeepSeek</a>（提问已复制）。';
  }
}

async function buildNarrate(){
  if(!NAR_VIDEO){ $('narStatus').textContent='❌ 请先拖入视频到上方区域'; $('narDrop').classList.add('shake'); setTimeout(()=>$('narDrop').classList.remove('shake'),600); return; }
  if(NAR_VIDEO.size > 2*1024*1024*1024){ $('narStatus').textContent='❌ 视频过大（'+(NAR_VIDEO.size/1073741824).toFixed(1)+'GB > 2GB），请先剪辑或压缩后再生成'; return; }
  const ok = await preflight('narrate'); if(!ok) return;
  const go=$('narQuickBtn')||$('narGo'); go.disabled=true; $('narResult').style.display='none';
  let plot = ($('narPlot') && $('narPlot').value.trim()) ? $('narPlot').value.trim() : '';
  // 没有填剧情但填了主题/要求时，合并成剧情文本，走剧情驱动（质量更好）
  if(!plot){
    const theme = ($('narTheme') ? $('narTheme').value.trim() : '');
    const req = ($('narReq') ? $('narReq').value.trim() : '');
    if(theme || req){ plot = (theme ? '主题：'+theme+'\n' : '') + (req ? '要求：'+req : ''); }
  }
  $('narStatus').textContent = plot ? '🎭 剧情驱动剪辑中（按你的剧情剪分镜+写解说）…' : '上传视频…';
  gStart(plot ? '🎭 剧情驱动剪辑' : '🎬 生成短片解说');
  const videoObj = NAR_VIDEO.mlib ? {name: NAR_VIDEO.name, mlib: NAR_VIDEO.mlib} : await videoToBody(NAR_VIDEO);
  const body = { video: videoObj,
                 params:{maxSeg: parseFloat($('narMaxSeg').value)||25, w:1280, h:720, fps:30,
                         name: NAR_VIDEO.name, theme: ($('narTheme') ? $('narTheme').value.trim() : ''),
                         req: ($('narReq') ? $('narReq').value.trim() : ''),
                         narr_style: ($('narrStyle') ? $('narrStyle').value : 'movie'),
                         detail_level: ($('detailLevel') ? $('detailLevel').value : 'balanced'),
                         autoCut: $('narAutoCut') ? $('narAutoCut').checked : true,
                         targetSec: parseFloat(($('narTargetSec')||{}).value) || 0,
                         subtitle: getSubtitleStyle()} };
  if(plot){ body.movie=''; body.plot=plot; }   // 剧情驱动：走 /api/movie_tts（两步走）
  if($('narBgm').checked && MUSIC){
    if(MUSIC.catalogId){ body.music={source:'catalog', catalogId:MUSIC.catalogId}; }
    else { body.music={name:MUSIC.name, data: toB64(new Uint8Array(await MUSIC.file.arrayBuffer()))}; }
  }
  try{
    // 有视频+剧情时走两步走：先生成配音，用户确认后再合成
    const api = (plot && NAR_VIDEO) ? '/api/movie_tts' : (plot ? '/api/narrate_movie' : '/api/narrate');
    const r=await fetch(api,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const out=await r.json();
    if(!out.ok) throw new Error(out.error||'失败');
    await pollNarrate(out.runid);
  }catch(e){ $('narStatus').textContent='❌ '+netErrMsg(e); }
  go.disabled=false;
}
function pollNarrate(runid){
  return new Promise(resolve=>{
    let _errs = 0;   // 连续失败计数：服务重启/断网时明确报错，不永久转圈
    _stopFlag = false;
    _currentRunid = runid; const cb=$('narCancel'); if(cb){ cb.style.display=''; cb.disabled=false; cb.textContent='⏹ 停止生成'; }
    const iv=setInterval(()=>{
      fetch('/api/progress?run='+runid).then(r=>r.json()).then(p=>{
        const b=$('narBar').querySelector('i');
        $('narBar').style.display='block';
        if(p.pct) b.style.width=Math.min(100,p.pct)+'%';
        gSet(p.pct, p.phase);
        if(p.done){
          clearInterval(iv); $('narBar').style.display='none';
          _currentRunid=null; _stopFlag=false; if(cb) cb.style.display='';
          if(p.error){ renderPartial('narPartial', p.partial); showTaskError($('narStatus'), p.error, p); gErr(p.error); resolve(); return; }
          // 两步走·第一步：配音已生成，展示确认面板
          if(p.tts_list && p.tts_list.length){
            $('narStatus').textContent = '🎙️ 配音已生成（'+p.tts_list.length+'段），正在跳转到手动调整…';
            renderAdjustPanel(p.tts_list, p.run_dir || '', 'nar', p.script || []);
            if(typeof showStep === 'function') showStep('adjust');
            gDone();
            resolve(); return;
          }
          $('narStatus').textContent='✅ 完成'; gDone();
          $('narResult').style.display='block';
          $('narPlayer').src='/media/'+p.file+'?t='+Date.now();
          setModeBadge('narBadge', p.mode);
          gPreview(p.file, '电影解说');
          $('narDl').href='/media/'+p.file;
          renderCredits('narResult', p.credits);
          _coverCtx.nar = {file: p.file}; const _ncb=$('narCoverBtn'); if(_ncb) _ncb.style.display='';
          const d=p.diag||{};
          let txt='分段 '+(d.segments||0)+' · 台词 '+(d.asr_lines||0)+' 条 · 配音 '+(d.voice_clips||0)+' 段';
          txt += _cutDiag(d.cut);
          if(d.narration){ txt += ' · 解说：' + d.narration.join(' / '); }
          $('narDiag').textContent = txt;
          resolve(); return;
        }
        // 已点「⏹ 停止」时不要再覆盖状态文案，否则用户看不到停止反馈
        if(!_stopFlag) $('narStatus').textContent = (p.phase||'处理中')+'… '+(p.pct||0)+'%';
      }).catch(()=>{ if(++_errs>=8){ clearInterval(iv); $('narBar').style.display='none'; _currentRunid=null; _stopFlag=false; if(cb) cb.style.display=''; $('narStatus').textContent='❌ 与服务失去连接（服务可能已重启），请重新发起'; gErr('与服务失去连接'); resolve(); } });
    },400);
    setTimeout(()=>{ clearInterval(iv); _currentRunid=null; _stopFlag=false; if(cb) cb.style.display=''; $('narStatus').textContent='⚠️ 等待超时已停止刷新（任务可能仍在后台进行），请稍后到「⑨记录」查看结果'; gErr('等待超时'); resolve(); }, 1800000);
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
  if(MOVIE_VIDEO && MOVIE_VIDEO.size > 2*1024*1024*1024){ $('movieStatus').textContent='❌ 视频过大（'+(MOVIE_VIDEO.size/1073741824).toFixed(1)+'GB > 2GB），请先剪辑或压缩后再生成'; return; }
  const ok = await preflight('movie'); if(!ok) return;
  const go = $('movieGo'); go.disabled = true; $('movieResult').style.display = 'none';
  $('movieStatus').textContent = '提交任务…';
  gStart('🌐 联网解说生成');
  const body = { movie: name, plot: plot,
    params: { maxSeg: parseFloat($('movieMaxSeg').value) || 25, w:1280, h:720, fps:30, plotRefine: $('moviePlotRefine') ? $('moviePlotRefine').checked : true } };
  if(MOVIE_VIDEO){ body.video = await videoToBody(MOVIE_VIDEO); }
  if($('movieBgm').checked && MUSIC){
    if(MUSIC.catalogId){ body.music = { source:'catalog', catalogId: MUSIC.catalogId }; }
    else { body.music = { name: MUSIC.name, data: toB64(new Uint8Array(await MUSIC.file.arrayBuffer())) }; }
  }
  try{
    const api = MOVIE_VIDEO ? '/api/movie_tts' : '/api/narrate_movie';
    const r = await fetch(api, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    const out = await r.json();
    if(!out.ok) throw new Error(out.error || '失败');
    await pollMovie(out.runid);
  }catch(e){ $('movieStatus').textContent = '❌ ' + e.message; }
  go.disabled = false;
}
function pollMovie(runid){
  return new Promise(resolve => {
    let _errs = 0;   // 连续失败计数：服务重启/断网时明确报错，不永久转圈
    _stopFlag = false;
    _currentRunid = runid; const cb=$('movieCancel'); if(cb){ cb.style.display=''; cb.disabled=false; cb.textContent='⏹ 停止生成'; }
    const iv = setInterval(() => {
      fetch('/api/progress?run=' + runid).then(r => r.json()).then(p => {
        const b = $('movieBar').querySelector('i'); $('movieBar').style.display = 'block';
        if(p.pct) b.style.width = Math.min(100, p.pct) + '%';
        gSet(p.pct, p.phase);
        if(p.done){
          clearInterval(iv); $('movieBar').style.display = 'none';
          _currentRunid=null; _stopFlag=false; if(cb) cb.style.display='';
          if(p.error){ renderPartial('moviePartial', p.partial); showTaskError($('movieStatus'), p.error, p); gErr(p.error); resolve(); return; }
          if(p.tts_list && p.tts_list.length){
            $('movieStatus').textContent = '🎙️ 配音已生成（'+p.tts_list.length+'段），正在跳转到手动调整…';
            renderAdjustPanel(p.tts_list, p.run_dir || '', 'movie', p.script || []);
            if(typeof showStep === 'function') showStep('adjust');
            gDone();
            resolve(); return;
          }
          $('movieStatus').textContent = '✅ 完成'; gDone();
          const d = p.diag || {};
          let txt = '事件 ' + (d.events || 0) + ' · 分段 ' + (d.segments || 0) + ' · 台词 ' + (d.asr_lines || 0) + ' 条 · 对齐 ' + (d.aligned || 0) + ' · 配音 ' + (d.voice_clips || 0) + ' 段';
          txt += _cutDiag(d.cut);
          if(d.narration && d.narration.length) txt += '\n解说：' + d.narration.join(' / ');
          if(p.script && p.script.length && !p.file) txt += '\n（仅解说稿）' + p.script.map(s => s.desc).join(' / ');
          $('movieDiag').textContent = txt;
          // 质量自检：标记不匹配片段
          var qWrap = document.getElementById('movieQuality');
          if(d.quality && d.quality.mismatch > 0){
            var mis = (d.quality.report||[]).filter(function(r){return r.flag==='mismatch';});
            var qHtml = '<div style="margin:8px 0;padding:10px 14px;background:rgba(245,158,11,0.1);border:1px solid rgba(245,158,11,0.3);border-radius:8px;font-size:13px">';
            qHtml += '⚠️ 质量自检：<b>'+d.quality.mismatch+'</b>/'+d.quality.total+'段解说词可能与画面台词不匹配，建议到手动调整页修正：<br>';
            mis.slice(0,5).forEach(function(r){
              qHtml += '<span style="color:#fbbf24">第'+(r.seg+1)+'段</span>："'+(r.narration||'').substring(0,30)+'…"<br>';
            });
            if(mis.length > 5) qHtml += '<span style="color:var(--muted)">…等共'+mis.length+'段</span>';
            qHtml += '</div>';
            if(qWrap){ qWrap.innerHTML = qHtml; qWrap.style.display='block'; }
          } else if(qWrap){ qWrap.style.display='none'; }
          if(p.file){
            $('movieResult').style.display = 'block';
            $('moviePlayer').src = '/media/' + p.file + '?t=' + Date.now();
            setModeBadge('movieBadge', p.mode);
            $('movieDl').href = '/media/' + p.file;
            _coverCtx.movie = {file: p.file}; const _mvcb=$('movieCoverBtn'); if(_mvcb) _mvcb.style.display='';
            gPreview(p.file, '联网解说');
            renderCredits('movieResult', p.credits);
          }
          resolve(); return;
        }
        if(!_stopFlag) $('movieStatus').textContent = (p.phase || '处理中') + '… ' + (p.pct || 0) + '%';
      }).catch(() => { if(++_errs>=8){ clearInterval(iv); $('movieBar').style.display = 'none'; _currentRunid=null; _stopFlag=false; if(cb) cb.style.display=''; $('movieStatus').textContent = '❌ 与服务失去连接（服务可能已重启），请重新发起'; gErr('与服务失去连接'); resolve(); } });
    }, 400);
    setTimeout(() => { clearInterval(iv); _currentRunid=null; _stopFlag=false; if(cb) cb.style.display=''; $('movieStatus').textContent = '⚠️ 等待超时已停止刷新（任务可能仍在后台进行），请稍后到「⑨记录」查看结果'; gErr('等待超时'); resolve(); }, 1800000);
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
  if(NAR_VIDEO) ctx.video = NAR_VIDEO.mlib ? {name:NAR_VIDEO.name, mlib:NAR_VIDEO.mlib} : await videoToBody(NAR_VIDEO);
  else if(BC_VIDEO) ctx.video = BC_VIDEO.mlib ? {name:BC_VIDEO.name, mlib:BC_VIDEO.mlib} : await videoToBody(BC_VIDEO);
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
    let _errs = 0;   // 连续失败计数：服务重启/断网时明确报错，不永久转圈
    _currentRunid = runid; const cb=$('instructCancel'); if(cb) cb.style.display='';
    const iv = setInterval(() => {
      fetch('/api/progress?run=' + runid).then(r => r.json()).then(p => {
        const b = $('instructBar').querySelector('i'); $('instructBar').style.display = 'block';
        if(p.pct) b.style.width = Math.min(100, p.pct) + '%';
        gSet(p.pct, p.phase);
        if(p.done){
          clearInterval(iv); $('instructBar').style.display = 'none';
          _currentRunid=null; if(cb) cb.style.display='none';
          if(p.error){ renderPartial('instructPartial', p.partial); showTaskError($('instructStatus'), p.error, p); gErr(p.error); resolve(); return; }
          $('instructStatus').textContent = '✅ 完成（' + (p.phase || '') + '）'; gDone();
          if(p.file){
            $('instructResult').style.display = 'block';
            $('instructPlayer').src = '/media/' + p.file + '?t=' + Date.now();
            setModeBadge('instructMode', p.mode);
            $('instructDl').href = '/media/' + p.file;
            _coverCtx.instruct = {file: p.file}; const _icb=$('instructCoverBtn'); if(_icb) _icb.style.display='';
            gPreview(p.file, '指令成片');
            renderCredits('instructResult', p.credits);
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
      }).catch(() => { if(++_errs>=8){ clearInterval(iv); $('instructBar').style.display = 'none'; _currentRunid=null; if(cb) cb.style.display='none'; $('instructStatus').textContent = '❌ 与服务失去连接（服务可能已重启），请重新发起'; gErr('与服务失去连接'); resolve(); } });
    }, 400);
    setTimeout(() => { clearInterval(iv); _currentRunid=null; if(cb) cb.style.display='none'; $('instructStatus').textContent = '⚠️ 等待超时已停止刷新（任务可能仍在后台进行），请稍后到「⑨记录」查看结果'; gErr('等待超时'); resolve(); }, 1800000);
  });
}

async function build(){
  const ok = await preflight('build'); if(!ok) return;
  const go = $('go'); go.disabled = true; $('result').style.display='none';
  $('cancelBtn').style.display = '';
  $('status').textContent = '上传素材…'; setBar(2);
  gStart('✨ 一键合成');
  const [rw, rh] = $('res').value.split('x').map(Number);
  const beatStep = 1;  // 每拍切换（原可配置项已从UI移除）
  const hardCut = $('hardCutSel').value === '1';
  const aiCap = $('aiCap').checked;
  const body = { items: [], music: null, params: { w:rw, h:rh, fps:+$('fps').value, transition:$('trans').value, singleDur:3, beatStep, hardCut, ai_captions: aiCap } };
  for (const it of ITEMS){
    if (it.mlib){   // 素材库条目：文件已在服务器，只传引用
      body.items.push({ kind:it.kind, name:it.name, dur:it.dur, mlib: it.mlib });
      continue;
    }
    if (it.file.size > 2*1024*1024*1024){
      stopBar(); $('cancelBtn').style.display='none'; $('status').textContent='❌ 文件过大：'+it.name+'（>2GB）'; gErr('文件过大'); go.disabled=false; return;
    }
    // 视频素材 >64MB 走分片上传（与卡点/解说同一协议）；图片与小视频维持 base64
    if (it.kind === 'video' && it.file.size > 64*1024*1024){
      const v = await videoToBody(it.file);
      body.items.push({ kind:it.kind, name:it.name, dur:it.dur, upload_id: v.upload_id });
    } else {
      const buf = await it.file.arrayBuffer();
      body.items.push({ kind:it.kind, name:it.name, dur:it.dur, data: toB64(new Uint8Array(buf)) });
    }
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
    let _errs = 0;   // 连续失败计数：服务重启/断网时明确报错，不永久转圈
    const iv = setInterval(() => {
      fetch('/api/progress?run=' + runid).then(r=>r.json()).then(p => {
        if (p.pct) setBar(p.pct);
        gSet(p.pct, p.phase);
        const sec = Math.round((Date.now()-t0)/1000);
        if (p.done){
          clearInterval(iv); stopBar();
          if (p.error){ renderPartial('buildPartial', p.partial); showTaskError($('status'), p.error, p); gErr(p.error); resolve(); return; }
          $('status').textContent = '✅ 完成（'+(p.duration||'')+'s）'; gDone();
          if (p.beat && p.beat.bpm){ $('musInfo2').textContent='💿 BPM '+p.beat.bpm+' · 节拍 '+p.beat.beat_count+' · 时长 '+(p.duration||'')+'s · 每'+ (p.beat.beatStep||1) +'拍切换，已对齐节拍。'; }
          $('result').style.display='block';
          $('player').src = '/media/' + p.file + '?t=' + Date.now();
          setModeBadge('buildMode', p.mode);
          $('dl').href = '/media/' + p.file;
          _coverCtx.build = {file: p.file}; const _bcb=$('buildCoverBtn'); if(_bcb) _bcb.style.display='';
          gPreview(p.file, '合成视频');
          renderCredits('result', p.credits);
          resolve(); return;
        }
        $('status').textContent = (p.phase||'合成中') + '… ' + (p.pct||0) + '%（已 ' + sec + ' 秒）';
      }).catch(()=>{ if(++_errs>=8){ clearInterval(iv); stopBar(); $('status').textContent='❌ 与服务失去连接（服务可能已重启），请重新发起'; gErr('与服务失去连接'); resolve(); } });
    }, 400);
    // safety: don't poll forever
    setTimeout(()=>{ clearInterval(iv); stopBar(); $('status').textContent='⚠️ 等待超时已停止刷新（任务可能仍在后台进行），请稍后到「⑨记录」查看结果'; gErr('等待超时'); resolve(); }, 1800000);
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
  if(BC_VIDEO.size > 2*1024*1024*1024){ $('bcStatus').textContent='❌ 视频过大（'+(BC_VIDEO.size/1073741824).toFixed(1)+'GB > 2GB），请先剪辑或压缩后再分析'; return; }
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
  // 分析并预览：走两步走流程（分析+生成配音），完成后自动跳转到⑥手动调整页面
  await buildNarrate();
}
async function _startPlan(type, params, plot){
  const video = (type==='beatcut') ? BC_VIDEO : NAR_VIDEO;
  const st = (type==='beatcut') ? $('bcStatus') : $('narStatus');
  const mainBtn = (type==='beatcut') ? $('bcGo') : $('narGo');
  if(mainBtn) mainBtn.disabled=true;
  st.textContent='🔍 分析中…（首次可能较慢，大视频需先分片上传）';
  _currentRunid = null;
  try{
    const videoObj = video.mlib ? {name:video.name, mlib:video.mlib} : await videoToBody(video);
    const body={ type, params, video: videoObj };
    if(plot){ body.plot = plot; }   // 🎭 剧情驱动：透传到 /api/plan，分析阶段按剧情剪分镜+写解说
    if(type==='beatcut'){ body.music = await _planMusicBodyAsync(); }
    else if(MUSIC){ body.music = await _planMusicBodyAsync(); }
    const r=await fetch('/api/plan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const out=await r.json();
    if(!out.ok) throw new Error(out.error||'分析失败');
    await _pollPlan(out.runid, type);
  }catch(e){ st.textContent='❌ '+netErrMsg(e); }
  if(mainBtn) mainBtn.disabled=false;
}
function _pollPlan(runid, type){
  return new Promise(resolve=>{
    let _errs = 0;   // 连续失败计数：服务重启/断网时明确报错，不永久转圈
    const st=(type==='beatcut')?$('bcStatus'):$('narStatus');
    const mainBtn = (type==='beatcut') ? $('bcGo') : $('narGo');
    const done = ()=>{ if(mainBtn) mainBtn.disabled=false; };
    _stopFlag = false;
    _currentRunid = runid; const cb=$((type==='beatcut')?'bcCancel':'narCancel');
    if(cb){ cb.style.display=''; cb.disabled=false; cb.textContent='⏹ 停止生成'; }
    const iv=setInterval(()=>{
      fetch('/api/progress?run='+runid).then(r=>r.json()).then(p=>{
        if(p.plan_ready && p.plan){
          clearInterval(iv);
          _currentRunid=null; _stopFlag=false; if(cb) cb.style.display='';
          _planRunid=runid; _planType=type;
          openPlanEditor(runid, p.plan);
          st.textContent='✅ 规划完成，请在弹窗中微调后点击「按我的调整合成」';
          done(); resolve(); return;
        }
        if(p.error){ showTaskError(st, p.error, p); clearInterval(iv); _currentRunid=null; _stopFlag=false; if(cb) cb.style.display=''; done(); resolve(); return; }
        if(p.done){ showTaskError(st, p.error||'分析失败', p); clearInterval(iv); _currentRunid=null; _stopFlag=false; if(cb) cb.style.display=''; done(); resolve(); return; }
        if(!_stopFlag) st.textContent=(p.phase||'分析中')+'… '+(p.pct||0)+'%';
      }).catch(()=>{ if(++_errs>=8){ clearInterval(iv); _currentRunid=null; _stopFlag=false; if(cb) cb.style.display=''; st.textContent='❌ 与服务失去连接（服务可能已重启），请重新分析'; done(); resolve(); } });
    },400);
    setTimeout(()=>{ clearInterval(iv); _currentRunid=null; _stopFlag=false; if(cb) cb.style.display=''; st.textContent='⚠️ 分析超时未返回（长视频解说分析可能需要 30 分钟以上），请稍后在⑨记录查看或重试'; done(); resolve(); }, 3600000);
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
    : '列表勾选 = 保留该段并配音；<b>取消勾选 = 把这段画面真的剪掉</b>（成片里不再出现，时长同步缩短）。'
      + '可直接编辑每段解说词；点「✂ 减词」可缩短成一句话；点「🔒」把该段锁定为必要（不可误删）。';
  let rowsHtml = '';
  if(isBeat){
    rowsHtml = _cutTimes.slice(0,-1).map((s,i)=>{
      const e=_cutTimes[i+1];
      return _planRowHtml(i, s, e, true, '', _findThumb(e), true);
    }).join('');
  } else {
    rowsHtml = (plan.segs||[]).map((s,i)=>_planRowHtml(i, s.start, s.end, s.keep!==false, s.caption||'', s.thumb||'', false, s.importance, s.keep)).join('');
  }
  const timelineHtml = isBeat ? _renderTimeline() : '';
  const addCutHtml = isBeat ? _addCutBarHtml() : '';
  // 解说方案：改完解说词后，可让模型按新解说重新匹配分镜并更新下方列表
  const alignHtml = isBeat ? '' : (
      '<div class="plan-align">'
    + '<button class="btn mini" id="alignShotsBtn" onclick="alignNarrateShots()">🧩 按解说词重新匹配分镜</button>'
    + '<span class="hint">改完上方解说词后点这里：会把候选镜头按新解说重新分配（增删句子、调换顺序都会重排分镜）。'
    + '模型可用时按语义匹配，不可用则按句子长度比例分配。</span>'
    + '<div class="hint" id="alignMsg"></div></div>');
  box.innerHTML = '<div class="plan-head"><span class="plan-title">'+title+'</span>'
    + '<button class="btn mini ghost" onclick="closePlanModal()">✕ 关闭</button></div>'
    + '<div class="plan-desc">'+desc+'</div>'
    + timelineHtml
    + addCutHtml
    + alignHtml
    + '<div class="plan-list">'+rowsHtml+'</div>'
    + '<div class="plan-sum" id="planSum"></div>'
    + '<div class="plan-actions">'
    + '<button class="btn ghost" onclick="closePlanModal()">取消</button>'
    + (isBeat ? '' : '<label class="cut-toggle" title="关闭则保留整段原片，只加字幕与配音">'
       + '<input type="checkbox" id="planAutoCut" checked style="width:auto"> ✂ 剪掉未勾选片段</label>')
    + '<span style="flex:1"></span>'
    + '<button class="btn danger" onclick="stopRun(' + (isBeat ? "'bcStatus'" : "'narStatus'") + ')">⏹ 停止生成</button>'
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

function _planRowHtml(i, s, e, on, caption, thumb, isBeat, importance, keep){
  const th = thumb ? '<img src="/media/'+_esc(thumb)+'?t='+Date.now()+'">' : '<img>';
  let capField='';
  let tag='';
  if(!isBeat){
    const imp = importance||'advance';
    if(imp==='transition'||imp==='mood') tag='<span class="seg-tag tag-fill">过渡/氛围</span>';
    else if(imp==='key') tag='<span class="seg-tag tag-key">主线·关键</span>';
    else tag='<span class="seg-tag tag-main">主线</span>';
    if(keep===false) tag+='<span class="seg-tag tag-cut">可剪</span>';
    capField = '<input type="text" class="cap" value="'+_esc(caption)+'" placeholder="解说词">'
      + '<button type="button" class="mini-btn shrink" title="缩短为一句">✂ 减词</button>'
      + '<button type="button" class="mini-btn ess" title="锁定为必要片段">🔒</button>';
  }
  // data-s/data-e 供 _updatePlanSum 实时算「剪掉多少 / 成片多长」
  return '<div class="plan-row" data-i="'+i+'" data-s="'+Number(s||0)+'" data-e="'+Number(e||0)+'">'
    + '<input type="checkbox" class="on" '+(on?'checked':'')+'>'
    + th
    + '<span class="time">'+_fmtTime(s)+' ~ '+_fmtTime(e)+'</span>'
    + tag
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

const _esc = escapeHtml;   // 统一转义入口（escapeHtml 额外转义引号，属性值里更安全）
function _updatePlanSum(){
  const rows=[...$('planBox').querySelectorAll('.plan-row')];
  const onRows=rows.filter(r=>r.querySelector('.on').checked);
  const keep=onRows.length;
  const sum=$('planSum'); if(!sum) return;
  let txt='已保留 '+keep+' / '+rows.length+' 段';
  const isBeat = _curPlan && _curPlan.type==='beatcut';
  if(!isBeat){
    // 解说方案：直接算出剪掉/留下的时长，让「取消勾选=真剪掉」看得见
    const kept=onRows.reduce((a,r)=>a+Math.max(0, (parseFloat(r.dataset.e)||0)-(parseFloat(r.dataset.s)||0)), 0);
    const all=rows.reduce((a,r)=>a+Math.max(0, (parseFloat(r.dataset.e)||0)-(parseFloat(r.dataset.s)||0)), 0);
    if(keep<rows.length && all>0){
      txt+='（✂ 剪掉 '+_fmtDur(all-kept)+'，成片约 '+_fmtDur(kept)+' / 原片 '+_fmtDur(all)+'）';
    }else if(all>0){
      txt+='（全片保留，成片约 '+_fmtDur(all)+'）';
    }
  }else if(keep<rows.length){
    txt+='（未勾选的段会被跳过）';
  }
  sum.textContent=txt;
}
function _fmtDur(sec){
  sec=Math.max(0,sec||0);
  const m=Math.floor(sec/60), s=sec-m*60;
  return m>0 ? (m+'分'+(s<10?'0':'')+s.toFixed(1)+'秒') : s.toFixed(1)+'秒';
}
// 剪辑结果摘要：让「真的剪了多少」在结果区看得见（此前解说成片恒等于原片时长）
function _cutDiag(cut){
  if(!cut || !(cut.cut_sec > 0)) return '';
  return ' · ✂ 剪掉 ' + _fmtDur(cut.cut_sec) + '（原片 ' + _fmtDur(cut.src_dur) + ' → 成片 ' + _fmtDur(cut.out_dur) + '）';
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
// 解说词驱动的分镜重匹配：把用户改写后的解说词送回后端，由模型（或算法）重新分配镜头
async function alignNarrateShots(){
  if(!_planRunid || !_curPlan) return;
  const lines = [];
  $('planBox').querySelectorAll('.plan-row').forEach(row=>{
    const cap = row.querySelector('.cap');
    if(cap) lines.push((cap.value||'').trim());
  });
  if(!lines.some(x=>x)){ alert('请至少填写一句解说词'); return; }
  const btn = $('alignShotsBtn');
  const msg = $('alignMsg');
  if(btn){ btn.disabled = true; btn.textContent = '⏳ 匹配中…'; }
  if(msg) msg.textContent = '正在按解说词重新匹配分镜…';
  try{
    const r = await fetch('/api/narrate/align', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ runid:_planRunid, lines }) });
    const out = await r.json();
    if(!out.ok) throw new Error(out.error || '匹配失败');
    // 用新的分镜刷新方案并重新渲染
    _curPlan.segs = out.segs;
    _curPlan.narr = lines;
    renderPlanModal();
    const m = $('alignMsg');
    if(m){
      m.textContent = '✅ ' + (out.msg||'已重新匹配')
        + '（候选镜头 ' + (out.shots||0) + ' 个 → ' + out.segs.length + ' 段）';
      m.style.color = (out.source === 'model') ? '#2e7d32' : '#b8860b';
    }
  }catch(e){
    const m = $('alignMsg');
    if(m){ m.textContent = '❌ ' + e.message; m.style.color = '#c0392b'; }
  }finally{
    const b = $('alignShotsBtn');
    if(b){ b.disabled = false; b.textContent = '🧩 按解说词重新匹配分镜'; }
  }
}

async function confirmPlan(){
  if(!_planRunid) return;
  const edits=_collectPlanEdits();
  const btn=$('planBox').querySelector('.plan-actions .btn:last-child');
  if(btn){ btn.disabled=true; btn.textContent='⏳ 合成中…'; }
  const st=(_planType==='beatcut')?$('bcStatus'):$('narStatus');
  st.textContent='⏳ 正在按你的调整合成…';
  try{
    const ac=$('planAutoCut');
    const r=await fetch('/api/confirm',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({runid:_planRunid, edits,
                           params:{ autoCut: ac ? ac.checked : (($('narAutoCut')||{}).checked !== false) }})});
    const out=await r.json();
    if(!out.ok) throw new Error(out.error||'合成失败');
    await _pollRender(out.runid, _planType);
  }catch(e){ st.textContent='❌ '+e.message; }
  closePlanModal();
}
function _pollRender(runid, type){
  return new Promise(resolve=>{
    let _errs = 0;   // 连续失败计数：服务重启/断网时明确报错，不永久转圈
    const st=(type==='beatcut')?$('bcStatus'):$('narStatus');
    _stopFlag = false;
    _currentRunid = runid; const cb=$((type==='beatcut')?'bcCancel':'narCancel');
    if(cb){ cb.style.display=''; cb.disabled=false; cb.textContent='⏹ 停止生成'; }
    const iv=setInterval(()=>{
      fetch('/api/progress?run='+runid).then(r=>r.json()).then(p=>{
        if(p.done){
          clearInterval(iv);
          _currentRunid=null; _stopFlag=false; if(cb) cb.style.display='';
          if(p.error){ showTaskError(st, p.error, p); resolve(); return; }
          st.textContent='✅ 完成（已按你的调整合成）';
          _showPlanResult(type, p);
          resolve(); return;
        }
        if(!_stopFlag) st.textContent=(p.phase||'合成中')+'… '+(p.pct||0)+'%';
      }).catch(()=>{ if(++_errs>=8){ clearInterval(iv); _currentRunid=null; _stopFlag=false; if(cb) cb.style.display=''; st.textContent='❌ 与服务失去连接（服务可能已重启），请重新发起'; resolve(); } });
    },400);
    setTimeout(()=>{ clearInterval(iv); _currentRunid=null; _stopFlag=false; if(cb) cb.style.display=''; st.textContent='⚠️ 等待超时已停止刷新（任务可能仍在后台进行），请稍后到「⑨记录」查看结果'; gErr('等待超时'); resolve(); }, 1800000);
  });
}
function _showPlanResult(type, p){
  const g=(id)=>$(id);
  if(type==='beatcut'){
    g('bcResult').style.display='block';
    g('bcPlayer').src='/media/'+p.file+'?t='+Date.now();
    g('bcDl').href='/media/'+p.file;
    _coverCtx.bc = {file: p.file}; const _ccb=$('bcCoverBtn'); if(_ccb) _ccb.style.display='';
    setModeBadge('bcMode','human');
    gPreview(p.file,'强卡点短片');
    renderCredits('bcResult', p.credits);
    const d=p.diag||{};
    g('bcDiag').textContent='已按你的调整合成 · 切换点 '+ (d.segments||0) +' 个'+(d.transition&&d.transition!=='none'?(' · 转场 '+d.transition):'')+(d.keep_audio?' · 保留原声':'');
  }else{
    g('narResult').style.display='block';
    g('narPlayer').src='/media/'+p.file+'?t='+Date.now();
    g('narDl').href='/media/'+p.file;
    _coverCtx.nar = {file: p.file}; const _ncb=$('narCoverBtn'); if(_ncb) _ncb.style.display='';
    setModeBadge('narBadge','human');
    gPreview(p.file,'电影解说');
    renderCredits('narResult', p.credits);
    const d=p.diag||{};
    g('narDiag').textContent='已按你的调整合成 · 片段 '+ (d.segments||0) +' 段 · 配音 '+ (d.voice_clips||0) +' 段'
      + _cutDiag(d.cut);
  }
}
function closePlanModal(){
  const m=$('planModal'); if(m) m.style.display='none';
}

// ---- 📺 B 站素材：搜索 + 下载（后端 /api/bili/*：yt-dlp 搜索 + playurl/yt-dlp 双引擎下载）----
function biliSearch(){
  const kw = ($('biliKw').value||'').trim();
  const box = $('biliResults'), btn = $('biliSearchBtn');
  if(!kw){ box.style.display='block'; box.innerHTML='<div class="hint">请输入关键词</div>'; return; }
  btn.disabled = true; btn.textContent='⏳ 搜索中…';
  box.style.display='block'; box.innerHTML='<div class="hint">⏳ 正在搜索 B 站（约 3~8 秒）…</div>';
  fetch('/api/bili/search?kw=' + encodeURIComponent(kw)).then(r=>r.json()).then(res=>{
    btn.disabled = false; btn.textContent='🔍 搜 B 站';
    if(!res.ok){ box.innerHTML='<div class="hint">❌ '+escapeHtml(res.error||'搜索失败')+'</div>'; return; }
    if(!(res.results||[]).length){ box.innerHTML='<div class="hint">没找到，换个关键词试试。</div>'; return; }
    box.innerHTML = '';
    (res.results||[]).forEach(it=>{
      const mins = Math.floor((it.duration||0)/60), secs = (it.duration||0)%60;
      const d = document.createElement('div');
      d.className = 'item'; d.style.marginBottom='6px';
      // 封面/标题/UP 主来自第三方：封面 URL 过协议白名单，文本一律转义
      const pic = safeUrl(it.pic);
      const thumb = pic ? `<img class="thumb" src="${escapeHtml(pic)}" referrerpolicy="no-referrer" alt="">`
                        : `<span class="thumb" style="display:inline-block"></span>`;
      const bvid = /^BV[0-9A-Za-z]{6,}$/.test(it.bvid||'') ? it.bvid : '';
      const open = bvid ? `<a class="btn mini ghost" href="https://www.bilibili.com/video/${encodeURIComponent(bvid)}" target="_blank" rel="noopener">↗</a>` : '';
      const dlId = 'biliDl_' + Math.random().toString(36).slice(2, 9);   // 不再把第三方 bvid 拼进 id
      d.innerHTML = `${thumb}
        <div class="meta"><div class="name">${escapeHtml(it.title||it.bvid)}</div><div class="kind">${escapeHtml(it.author||'')} · ${escapeHtml(mins+':'+String(secs).padStart(2,'0'))}</div></div>
        <button class="btn mini" id="${dlId}">⬇ 下载 MP4</button>
        ${open}`;
      box.appendChild(d);
      d.querySelector('button').addEventListener('click', ()=>biliDownload(it.bvid, d));
    });
  }).catch(e=>{ btn.disabled=false; btn.textContent='🔍 搜 B 站'; box.innerHTML='<div class="hint">❌ 搜索请求失败：'+escapeHtml(e.message)+'</div>'; });
}
let _biliTimer = null;
// 把浏览器底层网络错误翻译成人话（Failed to fetch = 请求根本没到达服务器）
function netErrMsg(e){
  const m = (e && e.message) || String(e);
  if (m.indexOf('Failed to fetch') >= 0 || m.indexOf('NetworkError') >= 0 || m.indexOf('Load failed') >= 0)
    return '无法连接服务器——请确认服务已启动且当前没有正在重启，然后刷新页面重试';
  return m;
}
function biliCancel(){ fetch('/api/bili/cancel', {method:'POST'}); }
function biliDownload(bvid, row){
  const btn = row ? row.querySelector('button') : null;   // 行内第一个按钮即「⬇ 下载 MP4」
  if(btn){ btn.disabled=true; btn.textContent='⏳ 提交…'; }
  fetch('/api/bili/download', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({bvid})}).then(r=>r.json()).then(res=>{
    if(!res.ok){ if(btn){ btn.disabled=false; btn.textContent='⬇ 下载 MP4'; } alert('❌ '+(res.error||'下载未启动')); return; }
    if(btn) btn.textContent='⏳ 下载中…';
    const bar = document.createElement('div'); bar.className='hint'; row.appendChild(bar);
    if(_biliTimer) clearInterval(_biliTimer);
    _biliTimer = setInterval(()=>{
      fetch('/api/bili/status').then(r=>r.json()).then(st=>{
        if(st.running){
          bar.innerHTML = '⏳ ' + escapeHtml(st.msg||'') + ' ' + escapeHtml(st.pct||0) + '% <button class="btn mini danger" onclick="biliCancel()">⏹ 取消</button>';
          return;
        }
        clearInterval(_biliTimer);
        if(!st.ok){ bar.textContent='❌ '+st.msg; if(btn){btn.disabled=false; btn.textContent='⬇ 重试下载';} return; }
        bar.textContent = '✅ ' + (st.title||'已下载');
        const act = document.createElement('div'); act.style.marginTop='4px';
        act.innerHTML = `<button class="btn mini">➕ 加入素材</button>
          <button class="btn mini ghost">🎬 设为解说视频</button>
          <button class="btn mini ghost">🎯 设为卡点视频</button>
          <button class="btn mini ghost">🗂 存入素材库</button>
          <a class="btn mini ghost" href="/media/${escapeHtml(st.file)}" download>💾 保存</a>`;
        row.appendChild(act);
        const [bItems, bNar, bBc, bMlib] = act.querySelectorAll('button');
        bItems.addEventListener('click', ()=>biliToItems(st.file, bItems));
        bNar.addEventListener('click', ()=>biliToSlot(st.file, 'nar', bNar));
        bBc.addEventListener('click', ()=>biliToSlot(st.file, 'bc', bBc));
        bMlib.addEventListener('click', ()=>{
          fetch('/api/material/save_from_media', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({file: st.file})}).then(r=>r.json()).then(x=>{
            bMlib.textContent = x.ok ? '✅ 已入库' : '❌ '+(x.error||'失败');
            if(x.ok) mlibList();
          }).catch(()=>{ bMlib.textContent='❌ 失败'; });
        });
      }).catch(()=>{});
    }, 900);
  }).catch(e=>{ if(btn){ btn.disabled=false; btn.textContent='⬇ 下载 MP4'; } alert('❌ '+e.message); });
}
async function biliFetchFile(rel){
  const r = await fetch('/media/' + rel);
  const blob = await r.blob();
  return new File([blob], rel.split('/').pop(), {type:'video/mp4'});
}
function biliToItems(rel, btn){
  if(btn){ btn.disabled=true; btn.textContent='⏳ 载入…'; }
  biliFetchFile(rel).then(f=>{
    ITEMS.push({ id:'it'+Date.now(), name:f.name, kind:'video', dur: parseInt(($('defDur')||{}).value)||3, url:URL.createObjectURL(f), file:f });
    render();
    if(btn) btn.textContent='✅ 已加入素材';
  }).catch(e=>{ if(btn){ btn.disabled=false; btn.textContent='➕ 加入素材'; } alert('❌ '+e.message); });
}
function biliToSlot(rel, which, btn){
  if(btn){ btn.disabled=true; btn.textContent='⏳ 载入…'; }
  biliFetchFile(rel).then(f=>{
    if(which==='nar'){ setNarVideo(f); goStep('narCard'); }
    else { setBCVideo(f); goStep('beatcutCard'); }
    if(btn) btn.textContent='✅ 已设置';
  }).catch(e=>{ if(btn){ btn.disabled=false; } alert('❌ '+e.message); });
}

// ---- 🖼 封面生成：智能选帧 + 大字标题（后端 /api/cover，三种版式可换帧）----
const _coverCtx = {};   // boxId -> {file, title, sub, style, ts, cands}
function makeCover(rel, boxId, defaultTitle){
  const box = $(boxId); box.style.display='block'; box.innerHTML='<div class="hint">⏳ 正在智能选帧生成封面…</div>';
  _coverCtx[boxId] = { file: rel, title: defaultTitle||'', sub:'', style:0, ts:null };
  fetch('/api/cover', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({file: rel, title: defaultTitle||''})})
    .then(r=>r.json()).then(res=>{
      if(!res.ok){ box.innerHTML='<div class="hint">❌ '+escapeHtml(res.error||'生成失败')+'</div>'; return; }
      Object.assign(_coverCtx[boxId], { ts: res.ts, cands: res.candidates||[] });
      _coverDraw(boxId, res.cover);
    }).catch(e=>{ box.innerHTML='<div class="hint">❌ '+escapeHtml(e.message)+'</div>'; });
}
function _coverDraw(boxId, cover){
  const st = _coverCtx[boxId], box = $(boxId);
  const styleNames = ['居中大字','底部条幅','左上角'];
  const cands = (st.cands||[]).map(c =>
    `<button class="btn mini ghost" style="${Math.abs(c.ts-st.ts)<0.011?'outline:2px solid #1d4ed8':''}" onclick="coverPickFrame('${boxId}',${c.ts})" title="换用这一帧">🎞 ${c.ts.toFixed(1)}s</button>`).join(' ');
  const coverUrl = '/media/' + cover;
  box.innerHTML = `
    <img src="${escapeHtml(coverUrl)}?t=${Date.now()}" style="max-width:320px;border-radius:8px;display:block;margin:6px 0;" alt="封面预览">
    <div class="row" style="gap:6px; align-items:center; margin:4px 0;">
      <input type="text" id="${boxId}_title" placeholder="封面标题（可留空）" value="${escapeHtml(st.title||'')}" style="flex:1" oninput="coverSetTitle('${boxId}', this.value)">
      <select id="${boxId}_style" onchange="coverSetStyle('${boxId}', this.value)">
        ${styleNames.map((nm,i)=>`<option value="${i}" ${st.style===i?'selected':''}>${escapeHtml(nm)}</option>`).join('')}
      </select>
    </div>
    <div class="row" style="gap:6px; flex-wrap:wrap; margin:4px 0; align-items:center;">
      <button class="btn mini" onclick="coverUpdate('${boxId}')">🖼 按当前设置重做</button>
      ${cands}
      <a class="btn mini" href="${escapeHtml(coverUrl)}?t=${Date.now()}" download="cover.jpg">⬇ 下载封面</a>
    </div>`;
}
function coverSetTitle(boxId, v){ if(_coverCtx[boxId]) _coverCtx[boxId].title = v; }
function coverSetStyle(boxId, v){ if(_coverCtx[boxId]){ _coverCtx[boxId].style = parseInt(v)||0; coverUpdate(boxId); } }
function coverPickFrame(boxId, ts){ if(_coverCtx[boxId]){ _coverCtx[boxId].ts = ts; coverUpdate(boxId); } }
function coverUpdate(boxId){
  const st = _coverCtx[boxId], box = $(boxId);
  box.innerHTML='<div class="hint">⏳ 重新渲染封面…</div>';
  fetch('/api/cover', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({file: st.file, title: st.title, sub: st.sub, style: st.style, ts: st.ts})})
    .then(r=>r.json()).then(res=>{
      if(!res.ok){ box.innerHTML='<div class="hint">❌ '+escapeHtml(res.error||'生成失败')+'</div>'; return; }
      st.ts = res.ts; st.cands = res.candidates||st.cands;
      _coverDraw(boxId, res.cover);
    }).catch(e=>{ box.innerHTML='<div class="hint">❌ '+escapeHtml(e.message)+'</div>'; });
}


// ---- 🗂 本地素材库：持久保存（material_library/，刷新/重启不丢）----
function mlibList(){
  const box = $('mlibList');
  if(!box) return;
  fetch('/api/material/list').then(r=>r.json()).then(res=>{
    if(!res.ok){ box.innerHTML='<div class="hint">❌ 加载失败</div>'; return; }
    if(!(res.items||[]).length){ box.innerHTML='<div class="hint">素材库还是空的：上传文件，或把 B 站下载的视频「🗂 存入素材库」。</div>'; return; }
    box.innerHTML='';
    (res.items||[]).forEach(m=>{
      const url = '/material_lib/' + encodeURIComponent(m.name);
      const sz = (m.size/1048576).toFixed(1)+'MB';
      const d = document.createElement('div'); d.className='item'; d.style.marginBottom='6px';
      d.innerHTML = `${m.kind==='image' ? `<img class="thumb" src="${escapeHtml(url)}">` : `<video class="thumb" src="${escapeHtml(url)}#t=1" preload="metadata" muted></video>`}
        <div class="meta"><div class="name">${escapeHtml(m.name)}</div><div class="kind">${m.kind==='video'?'🎬':'🖼️'} ${sz}</div></div>
        <button class="btn mini">➕ 加入素材列表</button>
        <button class="btn mini ghost">🎬 设为解说</button>
        <button class="btn mini ghost">🎯 设为卡点</button>
        <button class="btn mini danger" title="删除">🗑</button>`;
      box.appendChild(d);
      const [bAdd, bNar, bBc, bDel] = d.querySelectorAll('button');
      bAdd.addEventListener('click', ()=>{
        ITEMS.push({ id:'it'+Date.now()+Math.random().toString(36).slice(2,6), name:m.name, kind:m.kind, dur:parseInt(($('defDur')||{}).value)||3, mlib:m.name, url });
        render();
        bAdd.textContent='✅ 已加入'; setTimeout(()=>bAdd.textContent='➕ 加入素材列表', 1200);
      });
      bNar.addEventListener('click', ()=>mlibToSlot(m.name, 'nar', bNar));
      bBc.addEventListener('click', ()=>mlibToSlot(m.name, 'bc', bBc));
      bDel.addEventListener('click', ()=>{
        if(!confirm('从素材库删除 '+m.name+' ？（已生成的成片不受影响）')) return;
        fetch('/api/material/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name:m.name})}).then(()=>mlibList()).catch(()=>{});
      });
    });
  }).catch(()=>{ box.innerHTML='<div class="hint">❌ 请求失败</div>'; });
}
async function mlibUploadFile(f){
  let r;
  if(f.size > 64*1024*1024){
    const uid = await uploadChunksOnly(f);
    r = await fetch('/api/material/from_upload', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({upload_id: uid, name: f.name})}).then(x=>x.json());
  } else {
    const data = toB64(new Uint8Array(await f.arrayBuffer()));
    r = await fetch('/api/material/upload', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name: f.name, data})}).then(x=>x.json());
  }
  if(!r.ok) throw new Error(r.error||'上传失败');
  return r.name;
}
async function mlibUpload(files){
  for(const f of files){
    try { await mlibUploadFile(f); } catch(e){ alert('❌ '+f.name+'：'+e.message); }
  }
  mlibList();
}
function mlibToSlot(name, which, btn){
  if(btn){ btn.disabled=true; btn.textContent='⏳…'; }
  if(which==='nar'){ NAR_VIDEO = {name, mlib:name}; $('narDrop').textContent='🎞️ 已选（素材库）：'+name; $('narInfo').textContent='已从素材库设置视频，可直接点「生成解说」。'; if(btn) btn.textContent='✅'; goStep('narCard'); }
  else { BC_VIDEO = {name, mlib:name}; $('bcDrop').textContent='🎬 已选（素材库）：'+name; $('bcInfo').textContent='已从素材库设置视频，请选择背景音乐后生成。'; if(btn) btn.textContent='✅'; goStep('beatcutCard'); }
}
try { const ng = localStorage.getItem('springStudio.narGenre'); if (ng && $('narGenre')) $('narGenre').value = ng; } catch(e){}
mlibList();


// ---- ⚡ 一键智能模式 ----
let SMART_VIDEO = null;

(function(){
  const drop = $('smartDrop'), fi = $('smartInput');
  if(!drop || !fi) return;
  drop.addEventListener('click', () => fi.click());
  fi.addEventListener('change', e => { if(fi.files[0]) handleSmartVideo(fi.files[0]); fi.value=''; });
  drop.addEventListener('dragover', e => { e.preventDefault(); drop.style.background='#e8f5e9'; });
  drop.addEventListener('dragleave', () => drop.style.background='#fff');
  drop.addEventListener('drop', e => { e.preventDefault(); drop.style.background='#fff'; if(e.dataTransfer.files[0]) handleSmartVideo(e.dataTransfer.files[0]); });
})();

function handleSmartVideo(file){
  if(!file.type.startsWith('video/')){ $('smartStatus').textContent='❌ 请选择视频文件'; return; }
  SMART_VIDEO = file;
  $('smartDrop').innerHTML = '✅ 已选：' + file.name + '（' + (file.size/1048576).toFixed(1) + 'MB）';
  $('smartDrop').style.color = 'var(--accent)';
  const info = $('smartVideoInfo');
  info.style.display = 'flex';
  info.innerHTML = '<span>视频已就绪，填电影名后点一键生成</span><button class="btn mini danger" onclick="removeSmartVideo()" style="padding:3px 10px;font-size:12px;">✕ 移除</button>';
  detectSmartConfig();
}

function removeSmartVideo(){
  SMART_VIDEO = null;
  $('smartInput').value = '';
  $('smartDrop').innerHTML = '🎬 拖入视频文件，或点此选择';
  $('smartDrop').style.color = '';
  $('smartVideoInfo').style.display = 'none';
  $('smartAutoConfig').style.display = 'none';
  $('smartStatus').textContent = '';
}

function detectSmartConfig(){
  // 自动检测可用配置并显示
  fetch('/api/ai_status').then(r=>r.json()).then(s=>{
    const parts = [];
    parts.push(s.vlm_ready ? '视觉模型✅' : '视觉模型❌（用台词匹配）');
    parts.push(s.tts ? '配音✅' : '配音❌');
    fetch('/api/hardware').then(r=>r.json()).then(h=>{
      if(h.gpu) parts.push(h.gpu + ' ' + h.gpu_vram_gb + 'GB');
      if(h.tier) parts.push('档位:' + h.tier);
      $('smartConfigText').textContent = parts.join(' · ');
      $('smartAutoConfig').style.display = 'block';
    }).catch(()=>{
      $('smartConfigText').textContent = parts.join(' · ');
      $('smartAutoConfig').style.display = 'block';
    });
  }).catch(()=>{});
}

function toggleProSettings(){
  // 跳转到AI配置页面（专业设置都在那里：模型选择、下载、卸载、VLM开关等）
  if(typeof showStep === 'function'){
    showStep('ai');
    setTimeout(()=>window.scrollTo({top:0, behavior:'smooth'}), 50);
  }
}

async function smartGenerate(){
  const name = ($('smartMovieName').value || '').trim();
  if(!SMART_VIDEO && !name){ $('smartStatus').textContent='❌ 请上传视频或填电影名'; return; }
  if(SMART_VIDEO && SMART_VIDEO.size > 2*1024*1024*1024){ $('smartStatus').textContent='❌ 视频过大（>2GB），请先压缩'; return; }

  const go = $('smartGo'); go.disabled = true;
  $('smartResult').style.display = 'none';
  $('smartStatus').textContent = '提交任务…';
  gStart('⚡ 一键智能生成');

  // 自动设置参数：maxSeg 根据视频时长估算（默认25秒一段）
  const body = { movie: name, plot: '', params: { maxSeg: 25, w:1280, h:720, fps:30 } };
  if(SMART_VIDEO){ body.video = await videoToBody(SMART_VIDEO); }

  try{
    const r = await fetch('/api/narrate_movie', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    const out = await r.json();
    if(!out.ok) throw new Error(out.error || '失败');
    await pollSmart(out.runid);
  }catch(e){ $('smartStatus').textContent = '❌ ' + e.message; gErr(e.message); }
  go.disabled = false;
}

function pollSmart(runid){
  return new Promise(resolve => {
    let _errs = 0;
    _stopFlag = false;
    _currentRunid = runid;
    const cb = $('smartCancel'); if(cb){ cb.style.display=''; cb.disabled=false; }
    const iv = setInterval(() => {
      fetch('/api/progress?run=' + runid).then(r=>r.json()).then(p => {
        const b = $('smartBar').querySelector('i'); $('smartBar').style.display='block';
        if(p.pct) b.style.width = Math.min(100, p.pct) + '%';
        gSet(p.pct, p.phase);
        if(p.done){
          clearInterval(iv); $('smartBar').style.display='none';
          _currentRunid=null; _stopFlag=false; if(cb) cb.style.display='';
          if(p.error){ $('smartStatus').textContent='❌ ' + (p.error||'失败'); gErr(p.error); resolve(); return; }
          $('smartStatus').textContent = '✅ 完成！'; gDone();
          if(p.file){
            $('smartResult').style.display='block';
            $('smartPlayer').src = '/media/' + p.file + '?t=' + Date.now();
            $('smartDl').href = '/media/' + p.file;
            gPreview(p.file, '智能解说');
          }
          resolve(); return;
        }
        if(!_stopFlag) $('smartStatus').textContent = (p.phase||'处理中') + '… ' + (p.pct||0) + '%';
      }).catch(() => { if(++_errs>=8){ clearInterval(iv); $('smartBar').style.display='none'; $('smartStatus').textContent='❌ 与服务失去连接'; resolve(); } });
    }, 400);
    setTimeout(() => { clearInterval(iv); $('smartStatus').textContent='⚠️ 超时'; resolve(); }, 1800000);
  });
}


// ===== 增量重生成：编辑解说词 =====
let _narrEditRunId = '';
let _narrEditList = [];
function openNarrEdit(runId, captions){
  _narrEditRunId = runId;
  _narrEditList = captions.slice();
  $('narrEditInfo').textContent = '任务: ' + runId + ' · 共 ' + captions.length + ' 段解说词，修改后点保存只重生成改动的段落';
  const box = $('narrEditList');
  box.innerHTML = '';
  captions.forEach((txt, i) => {
    const wrap = document.createElement('div');
    wrap.style.cssText = 'margin-bottom:10px;';
    wrap.innerHTML = '<div style="font-size:12px;color:#888;margin-bottom:3px;">第 ' + (i+1) + ' 段</div>' +
      '<textarea data-idx="' + i + '" style="width:100%;min-height:50px;padding:8px;border-radius:8px;border:1px solid rgba(255,255,255,0.15);background:rgba(255,255,255,0.05);color:var(--ink);font-size:13px;resize:vertical;">' + escapeHtml(txt||'') + '</textarea>';
    box.appendChild(wrap);
  });
  $('narrEditModal').style.display = 'flex';
}
function closeNarrEdit(){ $('narrEditModal').style.display = 'none'; }
function saveNarrEdit(){
  const btn = $('narrEditSave');
  btn.disabled = true; btn.textContent = '重生成中...';
  const areas = $('narrEditList').querySelectorAll('textarea');
  let changed = 0;
  const doRegen = (idx) => {
    if(idx >= areas.length){
      btn.disabled = false; btn.textContent = '保存并重生成';
      $('narrEditInfo').textContent = '完成！已重生成 ' + changed + ' 段，刷新历史记录查看新成片';
      setTimeout(()=>{ closeNarrEdit(); loadHistory(); }, 1500);
      return;
    }
    const ta = areas[idx];
    const newText = ta.value.trim();
    const oldText = _narrEditList[idx] || '';
    if(newText !== oldText && newText){
      changed++;
      fetch('/api/regen_segment', {method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({run_id:_narrEditRunId, seg_idx:parseInt(ta.dataset.idx), text:newText})
      }).then(r=>r.json()).then(res=>{
        if(res.ok){ _narrEditList[parseInt(ta.dataset.idx)] = newText; ta.style.borderColor = '#4ade80'; }
        else { ta.style.borderColor = '#f87171'; alert('第'+(parseInt(ta.dataset.idx)+1)+'段重生成失败: '+res.error); }
        doRegen(idx+1);
      }).catch(e=>{ ta.style.borderColor='#f87171'; doRegen(idx+1); });
    } else {
      doRegen(idx+1);
    }
  };
  doRegen(0);
}


function removeModel(tag){
  if(!confirm('确定卸载模型 ' + tag + ' ？释放磁盘空间，之后需重新下载。')) return;
  fetch('/api/model/remove', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({model: tag})}).then(r=>{
      return r.text().then(t=>{ try{ return JSON.parse(t); }catch(e){ return {ok:false, error:'服务器返回异常: '+t.slice(0,100)}; } });
    }).then(res=>{
    if(res.ok){ alert('已卸载 ' + tag); if(typeof refreshModelCards==='function') refreshModelCards(); if(typeof loadVlmStatus==='function') loadVlmStatus(); }
    else alert('卸载失败: ' + (res.error||res.msg||'未知错误'));
  }).catch(e=>alert('卸载失败: '+e.message));
}


function renderTtsConfirm(containerId, ttsList, runDir, mode){
  let box = document.getElementById(containerId);
  if(!box){
    box = document.createElement('div');
    box.id = containerId;
    box.className = 'tts-confirm-panel';
    const result = document.getElementById(mode === 'movie' ? 'movieResult' : 'narResult');
    if(result && result.parentNode){ result.parentNode.insertBefore(box, result); }
    else { document.body.appendChild(box); }
  }
  let html = '<div class="tts-confirm-header">';
  html += '<h4>🎙️ 配音试听确认（'+ttsList.length+'段）</h4>';
  html += '<p style="font-size:12px;color:var(--muted);margin:4px 0">逐段试听，确认无误后点击开始合成。</p>';
  html += '</div>';
  html += '<div class="tts-list" style="max-height:300px;overflow-y:auto;margin:8px 0">';
  ttsList.forEach(function(item, idx){
    html += '<div class="tts-item" style="padding:8px;margin:4px 0;border-radius:8px;background:var(--bg2);border:1px solid var(--border)">';
    html += '<div style="font-size:12px;color:var(--muted);margin-bottom:4px">第 '+(idx+1)+' 段（'+(item.duration||0)+'秒）</div>';
    html += '<div style="font-size:13px;margin-bottom:6px;line-height:1.4">'+(item.text||'').substring(0,80)+((item.text||'').length>80?'…':'')+'</div>';
    html += '<audio controls preload="none" style="width:100%;height:32px"><source src="/media/'+item.audio+'" type="audio/mpeg"></audio>';
    html += '</div>';
  });
  html += '</div>';
  html += '<div style="display:flex;gap:8px;margin-top:8px">';
  html += '<button id="'+mode+'TtsConfirmBtn" class="btn-primary" style="flex:1">✅ 确认配音，开始合成视频</button>';
  html += '</div>';
  box.innerHTML = html;
  box.style.display = 'block';
  const btn = document.getElementById(mode+'TtsConfirmBtn');
  if(btn){ btn.onclick = function(){ confirmTtsAndCompose(runDir, mode, ttsList); }; }
}

async function confirmTtsAndCompose(runDir, mode, ttsList){
  if(!runDir){ alert('缺少run_dir'); return; }
  const btn = document.getElementById(mode+'TtsConfirmBtn');
  if(btn){ btn.disabled = true; btn.textContent = '合成中…'; }
  try{
    const body = { run_dir: runDir };
    if(mode === 'movie' && document.getElementById('movieBgm') && document.getElementById('movieBgm').checked && MUSIC){
      if(MUSIC.catalogId){ body.music = { source:'catalog', catalogId: MUSIC.catalogId }; }
      else { body.music = { name: MUSIC.name, data: toB64(new Uint8Array(await MUSIC.file.arrayBuffer())) }; }
    }
    const r = await fetch('/api/movie_compose', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    const out = await r.json();
    if(!out.ok) throw new Error(out.error || '失败');
    await pollMovieCompose(out.runid, mode);
  }catch(e){
    alert('合成失败：' + e.message);
    if(btn){ btn.disabled = false; btn.textContent = '✅ 确认配音，开始合成视频'; }
  }
}

function pollMovieCompose(runid, mode){
  return new Promise(resolve => {
    let _errs = 0;
    const iv = setInterval(() => {
      fetch('/api/progress?run=' + runid).then(r => r.json()).then(p => {
        if(p.done){
          clearInterval(iv);
          if(p.error){ alert('合成失败：' + p.error); resolve(); return; }
          const box = document.getElementById(mode === 'movie' ? 'movieTtsConfirm' : 'narTtsConfirm');
          if(box) box.style.display = 'none';
          if(p.file){
            const result = document.getElementById(mode === 'movie' ? 'movieResult' : 'narResult');
            if(result) result.style.display = 'block';
            const player = document.getElementById(mode === 'movie' ? 'moviePlayer' : 'narPlayer');
            if(player) player.src = '/media/' + p.file + '?t=' + Date.now();
            const dl = document.getElementById(mode === 'movie' ? 'movieDl' : 'narDl');
            if(dl) dl.href = '/media/' + p.file;
          }
          resolve();
        }
      }).catch(() => { if(++_errs >= 8){ clearInterval(iv); resolve(); } });
    }, 800);
  });
}


// === ⑥ 手动调整页面 ===
async function loadRecentTts(){
  const status = document.getElementById('adjustStatus');
  const sel = document.getElementById('adjustRestoreSelect');
  status.textContent = '📂 正在查找最近的配音任务…';
  try{
    const r = await fetch('/api/tts_recent');
    const out = await r.json();
    if(!out.ok || !out.list || out.list.length === 0){
      status.textContent = '❌ 没有找到已生成的配音任务，请先在⑤解说生成';
      return;
    }
    // 显示下拉选择
    sel.style.display = 'inline-block';
    sel.innerHTML = '';
    out.list.forEach(function(item){
      const opt = document.createElement('option');
      opt.value = item.run_dir;
      opt.textContent = item.time + ' · ' + (item.movie || '未命名') + ' · ' + item.tts_count + '段配音';
      sel.appendChild(opt);
    });
    sel.onchange = function(){ restoreTtsState(this.value); };
    status.textContent = '✅ 找到 ' + out.list.length + ' 个配音任务，选择后自动恢复';
    // 自动恢复最近的一个
    restoreTtsState(out.list[0].run_dir);
  }catch(e){
    status.textContent = '❌ 加载失败：' + e.message;
  }
}

async function restoreTtsState(runDir){
  const status = document.getElementById('adjustStatus');
  status.textContent = '📂 正在恢复配音…';
  try{
    const r = await fetch('/api/tts_state?run_dir=' + encodeURIComponent(runDir));
    const out = await r.json();
    if(!out.ok) throw new Error(out.error || '失败');
    renderAdjustPanel(out.tts_list, runDir, 'movie', []);
    status.textContent = '✅ 已恢复 ' + out.tts_list.length + ' 段配音，可试听调整后合成';
  }catch(e){
    status.textContent = '❌ 恢复失败：' + e.message;
  }
}


let _adjustState = { runDir: '', mode: '', items: [], changed: {} };
let _undoStack = [];
let _redoStack = [];
const MAX_UNDO = 50;

function _pushUndo(){
  if(!_adjustState.items) return;
  _undoStack.push(JSON.stringify(_adjustState.items));
  if(_undoStack.length > MAX_UNDO) _undoStack.shift();
  _redoStack = []; // 新操作清空重做栈
}

function undoAdjust(){
  if(_undoStack.length === 0){ return; }
  // 保存当前状态到重做栈
  _redoStack.push(JSON.stringify(_adjustState.items));
  var prev = JSON.parse(_undoStack.pop());
  _adjustState.items = prev;
  renderAdjustPanel(prev, _adjustState.runDir, _adjustState.mode, []);
  var hint = document.getElementById('adjustVideoHint');
  if(hint) hint.textContent = '↩️ 已撤销（还可撤销'+_undoStack.length+'次）';
}

function redoAdjust(){
  if(_redoStack.length === 0){ return; }
  _undoStack.push(JSON.stringify(_adjustState.items));
  var next = JSON.parse(_redoStack.pop());
  _adjustState.items = next;
  renderAdjustPanel(next, _adjustState.runDir, _adjustState.mode, []);
  var hint = document.getElementById('adjustVideoHint');
  if(hint) hint.textContent = '↪️ 已重做（还可重做'+_redoStack.length+'次）';
}

function renderAdjustPanel(ttsList, runDir, mode, script){
  _adjustState = { runDir: runDir, mode: mode, items: JSON.parse(JSON.stringify(ttsList)), changed: {} };
  const list = document.getElementById('adjustList');
  const status = document.getElementById('adjustStatus');
  const actions = document.getElementById('adjustActions');
  if(!list) return;
  status.textContent = '🎙️ 共 ' + ttsList.length + ' 段配音 · 逐段试听，可修改文字后重生成，或勾选跳过';
  let html = '';
  ttsList.forEach(function(item, idx){
    let cleanText = (item.text||'').replace(/\{(?:情绪|停顿|慢|快|高音|低音|大声|小声)[^}]*\}/g, '').replace(/\{\/(?:情绪|停顿|慢|快|高音|低音|大声|小声)\}/g, '').replace(/\s+/g, ' ').trim();
    let vs = item.video_start !== undefined ? item.video_start : 0;
    let ve = item.video_end !== undefined ? item.video_end : 0;
    let hasVideoSpan = (vs > 0 || ve > 0);
    let spanHint = hasVideoSpan ? '' : '<span style="color:#f59e0b;font-size:11px;margin-left:4px">（自动匹配中，可手动输入时间）</span>';
    html += '<div class="adjust-item" id="adjItem'+idx+'" style="padding:12px;margin:8px 0;border-radius:10px;background:var(--bg2);border:1px solid var(--border)">';
    html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">';
    html += '<span style="font-size:13px;font-weight:600">第 '+(idx+1)+' 段 <span style="color:var(--muted);font-weight:400">配音'+(item.duration||0)+'秒</span></span>';
    html += '<label style="font-size:12px;color:var(--muted);cursor:pointer"><input type="checkbox" id="adjSkip'+idx+'" style="margin-right:4px;vertical-align:middle">跳过</label>';
    html += '<button class="btn-secondary" style="padding:2px 8px;font-size:11px;color:#f87171;border-color:#f87171;background:transparent" onclick="deleteAdjustItem('+idx+')" title="删除此段（不进合成）">🗑 删除</button>';
    html += '</div>';
    html += '<textarea id="adjText'+idx+'" rows="2" style="width:100%;padding:8px;border-radius:6px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:13px;resize:vertical;box-sizing:border-box" oninput="_adjustState.changed['+idx+']=true">'+cleanText.replace(/</g,'&lt;').replace(/>/g,'&gt;')+'</textarea>';
    html += '<div style="display:flex;gap:8px;margin-top:8px;align-items:center;flex-wrap:wrap">';
    html += '<span style="font-size:12px;color:var(--muted);white-space:nowrap">🎬 对应画面:</span>';
    html += '<span style="font-size:12px;color:var(--muted)">从</span>';
    html += '<input type="number" id="adjVStart'+idx+'" value="'+vs+'" step="0.5" min="0" style="width:70px;padding:4px 6px;border-radius:4px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:12px" onchange="_adjustState.changed['+idx+']=true">';
    html += '<span style="font-size:12px;color:var(--muted)">秒到</span>';
    html += '<input type="number" id="adjVEnd'+idx+'" value="'+ve+'" step="0.5" min="0" style="width:70px;padding:4px 6px;border-radius:4px;border:1px solid var(--border);background:var(--bg);color:var(--text);font-size:12px" onchange="_adjustState.changed['+idx+']=true">';
    html += '<span style="font-size:12px;color:var(--muted)">秒</span>'+spanHint;
    html += '<button class="btn-secondary" style="padding:4px 10px;font-size:11px;white-space:nowrap" onclick="seekAdjVideo('+idx+')">▶️ 预览</button>';
    html += '<button class="btn-secondary" style="padding:4px 10px;font-size:11px;white-space:nowrap" onclick="alignSegmentToAudio('+idx+')">⇔ 对齐配音</button>';
    html += '<button class="btn-secondary" style="padding:4px 10px;font-size:11px;white-space:nowrap" onclick="previewVideoFrame('+idx+')">🖼️ 截图</button>';
    html += '<button class="btn-secondary" style="padding:4px 10px;font-size:11px;white-space:nowrap" onclick="recommendSegments('+idx+')">🤖 AI推荐</button>';
    html += '</div>';
    html += '<div id="adjRecommend'+idx+'" style="margin-top:6px;display:none"></div>';
    html += '<div id="adjFrame'+idx+'" style="margin-top:6px;display:none"><img id="adjFrameImg'+idx+'" style="max-width:100%;max-height:160px;border-radius:6px;border:1px solid var(--border)"></div>';
    html += '<div id="adjVideo'+idx+'" style="margin-top:6px;display:none;position:relative">';
    html += '<video id="adjVideoEl'+idx+'" style="max-width:100%;max-height:240px;border-radius:6px;border:1px solid var(--border);background:#000" playsinline></video>';
    html += '<div id="adjVideoHint'+idx+'" style="position:absolute;top:8px;left:8px;background:rgba(0,0,0,0.7);color:#fff;padding:3px 8px;border-radius:4px;font-size:11px;display:none">🎬 片段预览中…点击视频暂停</div>';
    html += '</div>';
    html += '<div style="display:flex;gap:6px;margin-top:6px;align-items:center">';
    html += '<audio controls preload="none" id="adjAudio'+idx+'" style="flex:1;height:32px"><source src="/media/'+item.audio+'" type="audio/mpeg"></audio>';
    html += '<button class="btn-secondary" style="padding:6px 12px;font-size:12px;white-space:nowrap" onclick="regenSingleTts('+idx+')">🔄 重生成</button>';
    html += '</div>';
    html += '<div id="adjStatus'+idx+'" style="font-size:11px;color:var(--muted);margin-top:4px;display:none"></div>';
    html += '</div>';
  });
  list.innerHTML = html;
  actions.style.display = 'flex';
  // 显示预览区和时间轴
  const preview = document.getElementById('adjustVideoPreview');
  const timeline = document.getElementById('adjustTimeline');
  if(preview) preview.style.display = 'block';
  if(timeline) timeline.style.display = 'block';
  // 保存视频时长（从tts_list或探测）
  if(!_adjustState.videoDuration){
    _adjustState.videoDuration = 0;
    // 用最大的video_end估算
    let maxEnd = 0;
    ttsList.forEach(function(it){ if(it.video_end > maxEnd) maxEnd = it.video_end; });
    if(maxEnd > 0) _adjustState.videoDuration = Math.ceil(maxEnd / 60) * 60 + 60;
  }
  renderTimeline();
  ensureAdjVideoLoaded();
  // 绑定确认按钮
  const btn = document.getElementById('adjustConfirmBtn');
  if(btn) btn.onclick = confirmAdjustAndCompose;
  const rbtn = document.getElementById('adjustRegenAllBtn');
  if(rbtn) rbtn.onclick = function(){ if(confirm('确定重新生成全部配音吗？')){ regenAllTts(); } };
  // 绑定全局播放按钮
  const playBtn = document.getElementById('adjPlayBtn');
  if(playBtn) playBtn.onclick = toggleAdjPlay;
  // 滚动到顶部
  window.scrollTo({top:0, behavior:'smooth'});
  // 键盘快捷键（只在手动调整页面激活时绑定）
  if(!window._adjKeyBound){
    window._adjKeyBound = true;
    document.addEventListener('keydown', function(e){
      // 只在手动调整面板可见时响应
      var panel = document.getElementById('adjustPanel');
      if(!panel || panel.style.display === 'none') return;
      // 输入框中不响应快捷键
      var tag = e.target.tagName;
      if(tag === 'INPUT' || tag === 'TEXTAREA' || e.target.isContentEditable) return;
      if(e.code === 'Space'){
        e.preventDefault();
        toggleAdjPlay();
      } else if((e.ctrlKey || e.metaKey) && e.code === 'KeyZ' && !e.shiftKey){
        e.preventDefault();
        undoAdjust();
      } else if((e.ctrlKey || e.metaKey) && (e.code === 'KeyY' || (e.code === 'KeyZ' && e.shiftKey))){
        e.preventDefault();
        redoAdjust();
      } else if(e.code === 'Delete' || e.code === 'Backspace'){
        if(_selectedSeg >= 0){
          e.preventDefault();
          deleteAdjustItem(_selectedSeg);
        }
      } else if(e.code === 'ArrowLeft'){
        if(_selectedSeg >= 0 && _adjustState.items && _adjustState.items[_selectedSeg]){
          e.preventDefault();
          var it = _adjustState.items[_selectedSeg];
          var step = e.shiftKey ? 0.1 : 0.5;
          it.video_start = Math.max(0, (it.video_start || 0) - step);
          it.video_end = Math.max(it.video_start + 0.5, (it.video_end || 0) - step);
          // 更新输入框
          var vsI = document.getElementById('adjVStart'+_selectedSeg);
          var veI = document.getElementById('adjVEnd'+_selectedSeg);
          if(vsI) vsI.value = it.video_start.toFixed(1);
          if(veI) veI.value = it.video_end.toFixed(1);
          renderTimeline();
          seekAdjVideo(it.video_start);
        }
      } else if(e.code === 'ArrowRight'){
        if(_selectedSeg >= 0 && _adjustState.items && _adjustState.items[_selectedSeg]){
          e.preventDefault();
          var it2 = _adjustState.items[_selectedSeg];
          var step2 = e.shiftKey ? 0.1 : 0.5;
          var maxDur = _adjustState.videoDuration || 600;
          it2.video_start = Math.min(maxDur - 1, (it2.video_start || 0) + step2);
          it2.video_end = Math.min(maxDur, (it2.video_end || 0) + step2);
          var vsI2 = document.getElementById('adjVStart'+_selectedSeg);
          var veI2 = document.getElementById('adjVEnd'+_selectedSeg);
          if(vsI2) vsI2.value = it2.video_start.toFixed(1);
          if(veI2) veI2.value = it2.video_end.toFixed(1);
          renderTimeline();
          seekAdjVideo(it2.video_start);
        }
      }
    });
  }
}

function previewVideoFrame(idx){
  const t = parseFloat(document.getElementById('adjVStart'+idx).value) || 0;
  const frameDiv = document.getElementById('adjFrame'+idx);
  const img = document.getElementById('adjFrameImg'+idx);
  // 隐藏视频预览
  const vDiv = document.getElementById('adjVideo'+idx);
  if(vDiv) vDiv.style.display = 'none';
  frameDiv.style.display = 'block';
  img.src = '/api/video_frame?run_dir=' + encodeURIComponent(_adjustState.runDir) + '&time=' + t + '&t=' + Date.now();
  img.onerror = function(){ frameDiv.style.display = 'none'; };
}


// === 时间轴编辑器 ===
let _tlDrag = null;
let _playheadRAF = null;
let _selectedSeg = -1;  // 当前选中的片段索引
let _tlZoom = 1;  // 时间轴缩放级别
let _playSegIndex = -1;  // 当前播放到第几段（按片段顺序连续播放）
let _lastJumpAt = 0;  // 上次跳转时间戳（跳转后300ms内不检测段结束）

function renderTimeline(){
  const items = _adjustState.items;
  if(!items || items.length === 0) return;
  // 时间轴总时长=max(累计视频取片时长, 累计配音时长)
  let totalVideoDur = 0, totalAudioDur = 0;
  items.forEach(function(it){
    const vs = it.video_start || 0, ve = it.video_end || 0;
    if(ve > vs) totalVideoDur += (ve - vs);
    totalAudioDur += (it.duration || 3);
  });
  let totalDur = Math.max(totalVideoDur, totalAudioDur, 10);
  _adjustState.timelineDuration = totalDur;
  _adjustState.totalVideoDur = totalVideoDur;
  _adjustState.totalAudioDur = totalAudioDur;
  const inner = document.getElementById('timelineInner');
  const ruler = document.getElementById('timelineRuler');
  const vTrack = document.getElementById('videoTrack');
  const aTrack = document.getElementById('audioTrack');
  if(!inner || !ruler || !vTrack || !aTrack) return;

  // 时间轴宽度：缩放控制
  const pxPerSec = Math.max(6, 12 * _tlZoom);
  const width = Math.max(800, totalDur * pxPerSec);
  inner.style.width = width + 'px';

  // 标尺（成片时间）
  let rulerHtml = '';
  const tickInterval = totalDur > 300 ? 30 : (totalDur > 120 ? 15 : (totalDur > 60 ? 10 : 5));
  for(let t = 0; t <= totalDur; t += tickInterval){
    const left = (t / totalDur) * 100;
    const mm = Math.floor(t / 60), ss = Math.floor(t % 60);
    rulerHtml += '<div style="position:absolute;left:'+left+'%;top:0;height:100%;border-left:1px solid var(--border);padding-left:3px;line-height:22px;white-space:nowrap">'+mm+':'+(ss<10?'0':'')+ss+'</div>';
  }
  ruler.innerHTML = rulerHtml;

  // 视频轨：色块宽度=取片时长，位置=累计视频时长
  let vHtml = '';
  let cumVideo = 0;
  items.forEach(function(it, idx){
    const vs = it.video_start || 0;
    const ve = it.video_end || 0;
    const vdur = Math.max(0.1, ve - vs);
    const adur = it.duration || 3;
    const left = (cumVideo / totalDur) * 100;
    const w = (vdur / totalDur) * 100;
    const colors = ['#6366f1','#8b5cf6','#ec4899','#f59e0b','#10b981','#06b6d4','#ef4444','#84cc16','#f97316'];
    const c = colors[idx % colors.length];
    let warn = '';
    if(vdur < adur - 0.3){ warn = ' ⚠️视频短'; }
    else if(vdur > adur + 1){ warn = ' ⚠️视频长'; }
    vHtml += '<div class="tl-vseg" data-idx="'+idx+'" style="position:absolute;left:'+left+'%;top:3px;width:'+w+'%;height:32px;background:'+c+';border-radius:4px;overflow:hidden;opacity:0.85;border:1px solid rgba(255,255,255,0.2)">';
    vHtml += '<div class="tl-handle tl-resize-l" data-idx="'+idx+'" data-side="l" title="调整源视频入点" style="position:absolute;left:0;top:0;width:8px;height:100%;cursor:w-resize;background:rgba(0,0,0,0.4);border-right:1px dashed rgba(255,255,255,0.5)"></div>';
    vHtml += '<div class="tl-move" data-idx="'+idx+'" data-side="m" title="点击选中并播放" style="position:absolute;left:8px;right:8px;top:0;height:100%;cursor:pointer">';
    vHtml += '<span style="position:absolute;left:6px;top:0;line-height:32px;font-size:11px;color:#fff;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;right:6px">第'+(idx+1)+'段 · 源'+vs.toFixed(0)+'-'+ve.toFixed(0)+'s · '+vdur.toFixed(1)+'s'+warn+'</span>';
    vHtml += '</div>';
    vHtml += '<div class="tl-handle tl-resize-r" data-idx="'+idx+'" data-side="r" title="调整源视频出点" style="position:absolute;right:0;top:0;width:8px;height:100%;cursor:e-resize;background:rgba(0,0,0,0.4);border-left:1px dashed rgba(255,255,255,0.5)"></div>';
    vHtml += '</div>';
    cumVideo += vdur;
  });
  vTrack.innerHTML = vHtml;

  // 配音轨：宽度=配音时长，位置=累计视频时长+audio_offset，可拖动
  let aHtml = '';
  let cumVideoForAudio = 0;
  items.forEach(function(it, idx){
    const dur = it.duration || 3;
    const vs = it.video_start || 0, ve = it.video_end || 0;
    const vdur = Math.max(0, ve - vs);
    const offset = it.audio_offset || 0;
    const left = ((cumVideoForAudio + offset) / totalDur) * 100;
    const w = (dur / totalDur) * 100;
    let matchColor = 'rgba(255,255,255,0.3)';
    if(vdur < dur - 0.3) matchColor = 'rgba(239,68,68,0.6)';
    else if(vdur > dur + 1) matchColor = 'rgba(245,158,11,0.6)';
    const offsetLabel = offset !== 0 ? (offset > 0 ? ' +'+offset.toFixed(1)+'s' : ' '+offset.toFixed(1)+'s') : '';
    aHtml += '<div class="tl-aseg tl-audio-drag" data-idx="'+idx+'" style="position:absolute;left:'+left+'%;top:3px;width:'+w+'%;height:32px;background:rgba(139,92,246,0.15);border-radius:4px;cursor:grab;border:1px dashed '+matchColor+';transition:none">';
    aHtml += '<span style="position:absolute;left:8px;top:0;line-height:32px;font-size:10px;color:#c4b5fd;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;right:8px">🔊 '+dur.toFixed(1)+'s'+offsetLabel+'</span>';
    aHtml += '</div>';
    cumVideoForAudio += vdur;
  });
  aTrack.innerHTML = aHtml;
  // 绑定音频块拖动
  var aSegs = document.querySelectorAll('.tl-audio-drag');
  for(var ai=0;ai<aSegs.length;ai++){
    aSegs[ai].addEventListener('mousedown', tlStartAudioDrag);
  }

  // 绑定拖拽（左裁剪|中间拖动|右裁剪）
  var allHandles = document.querySelectorAll('.tl-resize-l, .tl-resize-r, .tl-move');
  for(var i=0;i<allHandles.length;i++){
    allHandles[i].addEventListener('mousedown', tlStartResize);
  }
  // 点击片段：设为选中，跳转并限制播放范围
  var vSegs = document.querySelectorAll('.tl-vseg');
  for(var j=0;j<vSegs.length;j++){
    vSegs[j].addEventListener('click', function(e){
      if(e.target.classList.contains('tl-resize-l') || e.target.classList.contains('tl-resize-r')) return;
      var idx = parseInt(this.getAttribute('data-idx'));
      _selectedSeg = idx;
      _playSegIndex = idx;
      var it = _adjustState.items[idx];
      if(it){
        var allSegs = document.querySelectorAll('.tl-vseg');
        for(var k=0;k<allSegs.length;k++){ allSegs[k].style.outline = ''; }
        this.style.outline = '2px solid #fbbf24';
        seekAdjVideo(it.video_start || 0);
        var hint = document.getElementById('adjustVideoHint');
        if(hint) hint.textContent = '📌 第'+(idx+1)+'段 · 源视频'+(it.video_start||0).toFixed(1)+'-'+(it.video_end||0).toFixed(1)+'s · 配音'+(it.duration||0).toFixed(1)+'s';
      }
    });
  }
  // 点击时间轴空白处跳转：按视频色块位置定位
  inner.addEventListener('click', function(e){
    if(e.target.classList.contains('tl-resize-l') || e.target.classList.contains('tl-resize-r') || e.target.closest('.tl-vseg')) return;
    var rect = inner.getBoundingClientRect();
    var x = e.clientX - rect.left;
    var compT = (x / rect.width) * totalDur;
    // 按视频累计时长找到对应的片段
    var cum = 0, targetIdx = 0, targetOffset = 0;
    for(var i=0;i<items.length;i++){
      var vs = items[i].video_start || 0, ve = items[i].video_end || 0;
      var vd = Math.max(0, ve - vs);
      if(compT < cum + vd){ targetIdx = i; targetOffset = compT - cum; break; }
      cum += vd;
      targetIdx = i;
    }
    _playSegIndex = targetIdx;
    _selectedSeg = targetIdx;
    var srcT = (items[targetIdx].video_start || 0) + targetOffset;
    seekAdjVideo(srcT);
    var allSegs = document.querySelectorAll('.tl-vseg');
    for(var k=0;k<allSegs.length;k++){ allSegs[k].style.outline = ''; }
    if(allSegs[targetIdx]) allSegs[targetIdx].style.outline = '2px solid #fbbf24';
  });
}

function tlStartResize(e){
  e.preventDefault();
  e.stopPropagation();
  var idx = parseInt(this.getAttribute('data-idx'));
  var side = this.getAttribute('data-side');
  var it = _adjustState.items[idx];
  if(!it) return;
  var pxPerSec = Math.max(6, 12 * _tlZoom);
  var videoDur = _adjustState.videoDuration || 600;
  var inner = document.getElementById('timelineInner');
  _pushUndo(); // 拖拽开始前保存状态
  _tlDrag = {
    idx: idx, side: side,
    startX: e.clientX,
    origStart: it.video_start || 0,
    origEnd: it.video_end || 0,
    pxPerSec: pxPerSec,
    videoDur: videoDur,
    inner: inner,
    duration: (it.video_end || 0) - (it.video_start || 0)
  };
  this.style.cursor = side === 'm' ? 'grabbing' : (side === 'l' ? 'w-resize' : 'e-resize');
  document.addEventListener('mousemove', tlDoResize);
  document.addEventListener('mouseup', tlEndResize);
}

function tlDoResize(e){
  if(!_tlDrag) return;
  var dx = e.clientX - _tlDrag.startX;
  // 用和时间轴渲染一致的pxPerSec换算，1像素=1/pxPerSec秒
  // 按住Shift键时5倍精确微调
  var sensitivity = e.shiftKey ? 0.2 : 1;
  var dt = (dx / _tlDrag.pxPerSec) * sensitivity;
  var it = _adjustState.items[_tlDrag.idx];
  if(!it) return;
  var snapThreshold = 1.0; // 磁吸阈值：1秒
  var snapped = false;

  if(_tlDrag.side === 'l'){
    var newStart = _tlDrag.origStart + dt;
    // 磁吸：吸附到配音块边缘
    var snapTarget = _findSnapPoint(newStart, _tlDrag.idx, 'start');
    if(snapTarget !== null && Math.abs(newStart - snapTarget) < snapThreshold){
      newStart = snapTarget; snapped = true;
    }
    it.video_start = Math.max(0, Math.min(newStart, (it.video_end || 0) - 0.5));
  } else if(_tlDrag.side === 'r'){
    var newEnd = _tlDrag.origEnd + dt;
    var snapTarget2 = _findSnapPoint(newEnd, _tlDrag.idx, 'end');
    if(snapTarget2 !== null && Math.abs(newEnd - snapTarget2) < snapThreshold){
      newEnd = snapTarget2; snapped = true;
    }
    it.video_end = Math.max((it.video_start || 0) + 0.5, Math.min(newEnd, _tlDrag.videoDur));
  } else if(_tlDrag.side === 'm'){
    // 整体拖动：保持长度，同时移动start和end
    var newStart2 = _tlDrag.origStart + dt;
    var newEnd2 = _tlDrag.origEnd + dt;
    // 边界限制
    if(newStart2 < 0){ newEnd2 -= newStart2; newStart2 = 0; }
    if(newEnd2 > _tlDrag.videoDur){ newStart2 -= (newEnd2 - _tlDrag.videoDur); newEnd2 = _tlDrag.videoDur; }
    // 磁吸：吸附到配音块
    var snapTarget3 = _findSnapPoint(newStart2, _tlDrag.idx, 'start');
    if(snapTarget3 !== null && Math.abs(newStart2 - snapTarget3) < snapThreshold){
      var delta = snapTarget3 - newStart2;
      newStart2 += delta; newEnd2 += delta; snapped = true;
    }
    it.video_start = Math.max(0, newStart2);
    it.video_end = Math.min(_tlDrag.totalDur, newEnd2);
  }
  // 显示磁吸辅助线
  _showSnapLine(snapped ? it.video_start : null);
  // 更新输入框
  var vsInput = document.getElementById('adjVStart' + _tlDrag.idx);
  var veInput = document.getElementById('adjVEnd' + _tlDrag.idx);
  if(vsInput) vsInput.value = it.video_start.toFixed(1);
  if(veInput) veInput.value = it.video_end.toFixed(1);
  _adjustState.changed[_tlDrag.idx] = true;
  renderTimeline();
}

function tlEndResize(){
  if(_tlDrag){
    var el = document.querySelector('.tl-move[data-idx="'+_tlDrag.idx+'"]');
    if(el) el.style.cursor = 'grab';
  }
  _tlDrag = null;
  _showSnapLine(null);
  document.removeEventListener('mousemove', tlDoResize);
  document.removeEventListener('mouseup', tlEndResize);
}

// 音频块拖动：调整audio_offset
function tlStartAudioDrag(e){
  e.preventDefault();
  e.stopPropagation();
  var idx = parseInt(this.getAttribute('data-idx'));
  var it = _adjustState.items[idx];
  if(!it) return;
  var pxPerSec = Math.max(6, 12 * _tlZoom);
  _pushUndo();
  _tlDrag = {
    idx: idx, side: 'audio',
    startX: e.clientX,
    origOffset: it.audio_offset || 0,
    pxPerSec: pxPerSec,
    inner: document.getElementById('timelineInner')
  };
  this.style.cursor = 'grabbing';
  document.addEventListener('mousemove', tlDoAudioDrag);
  document.addEventListener('mouseup', tlEndAudioDrag);
}

function tlDoAudioDrag(e){
  if(!_tlDrag || _tlDrag.side !== 'audio') return;
  var dx = e.clientX - _tlDrag.startX;
  var sensitivity = e.shiftKey ? 0.2 : 1;
  var dt = (dx / _tlDrag.pxPerSec) * sensitivity;
  var it = _adjustState.items[_tlDrag.idx];
  if(!it) return;
  it.audio_offset = Math.round((_tlDrag.origOffset + dt) * 10) / 10;
  renderTimeline();
}

function tlEndAudioDrag(e){
  if(!_tlDrag || _tlDrag.side !== 'audio') return;
  var idx = _tlDrag.idx;
  var it = _adjustState.items[idx];
  document.removeEventListener('mousemove', tlDoAudioDrag);
  document.removeEventListener('mouseup', tlEndAudioDrag);
  _tlDrag = null;
  if(it){
    var hint = document.getElementById('adjustVideoHint');
    if(hint) hint.textContent = '🔊 第'+(idx+1)+'段配音偏移: '+(it.audio_offset||0).toFixed(1)+'s ('+(it.audio_offset>0?'延后':'提前')+')';
  }
  renderTimeline();
}

// 磁吸：只对齐视频块边缘与配音时长，不对齐相邻视频片段
// （视频片段是源视频中独立取的，首尾相接没有意义）
function _findSnapPoint(t, idx, which){
  var items = _adjustState.items;
  if(!items) return null;
  var candidates = [];
  var it = items[idx];
  if(it && it.duration){
    var aStart = it.video_start || 0;
    var aEnd = aStart + (it.duration || 0) + 0.3; // 配音时长+0.3秒缓冲
    // 右边缘裁剪时，磁吸到配音结束位置（视频长度=配音长度）
    if(which === 'end') candidates.push(aEnd);
  }
  if(candidates.length === 0) return null;
  // 找最近的
  var best = null, bestDist = Infinity;
  for(var i=0;i<candidates.length;i++){
    var d = Math.abs(candidates[i] - t);
    if(d < bestDist){ bestDist = d; best = candidates[i]; }
  }
  return best;
}

// 显示磁吸辅助线
function _showSnapLine(t){
  var inner = document.getElementById('timelineInner');
  if(!inner) return;
  var line = document.getElementById('snapLine');
  if(!line){
    line = document.createElement('div');
    line.id = 'snapLine';
    line.style.cssText = 'position:absolute;top:0;bottom:0;width:1px;background:#fbbf24;z-index:9;pointer-events:none;display:none';
    inner.appendChild(line);
  }
  if(t === null){ line.style.display = 'none'; return; }
  var totalDur = _adjustState.videoDuration || 600;
  line.style.display = 'block';
  line.style.left = (t / totalDur * 100) + '%';
}

// 一键对齐：视频段长度匹配配音时长
function alignSegmentToAudio(idx){
  var it = _adjustState.items[idx];
  if(!it || !it.duration) return;
  it.video_end = (it.video_start || 0) + it.duration + 0.3;
  var veInput = document.getElementById('adjVEnd' + idx);
  if(veInput) veInput.value = it.video_end.toFixed(1);
  _adjustState.changed[idx] = true;
  renderTimeline();
}

// 全部对齐：所有段视频长度匹配配音时长
function alignAllToAudio(){
  if(!_adjustState.items) return;
  for(var i=0;i<_adjustState.items.length;i++){
    alignSegmentToAudio(i);
  }
}

// 全局视频预览控制
function seekAdjVideo(tOrIdx){
  var video = document.getElementById('adjGlobalVideo');
  if(!video) return;
  var totalDur = _adjustState.videoDuration || 600;
  // 如果传入的是段索引（整数且小于段数），转为该段的video_start
  var t;
  if(Number.isInteger(tOrIdx) && _adjustState.items && tOrIdx < _adjustState.items.length){
    t = (_adjustState.items[tOrIdx].video_start) || 0;
  } else {
    t = tOrIdx;
  }
  t = Math.max(0, Math.min(t, totalDur));
  if(video.readyState >= 1){
    video.currentTime = t;
  } else {
    video.addEventListener('loadedmetadata', function onMeta(){
      video.removeEventListener('loadedmetadata', onMeta);
      video.currentTime = t;
    });
  }
  updatePlayhead(t);
}

function updatePlayhead(t){
  var ph = document.getElementById('playhead');
  var inner = document.getElementById('timelineInner');
  var video = document.getElementById('adjGlobalVideo');
  if(!ph || !inner) return;
  var totalDur = _adjustState.timelineDuration || 60;
  var left = (t / totalDur) * 100;
  ph.style.display = 'block';
  ph.style.left = left + '%';
  // 更新时间显示（成片时间）
  var timeEl = document.getElementById('adjustVideoTime');
  if(timeEl){
    timeEl.textContent = fmtTime(t) + ' / ' + fmtTime(totalDur);
  }
}

function fmtTime(s){
  var m = Math.floor(s / 60), ss = Math.floor(s % 60);
  return m + ':' + (ss < 10 ? '0' : '') + ss;
}

function toggleAdjPlay(){
  var video = document.getElementById('adjGlobalVideo');
  var btn = document.getElementById('adjPlayBtn');
  if(!video) return;
  // 确保视频源已设置
  if(!video.src || video.src.indexOf('src.mp4') < 0){
    var videoUrl = '/media/' + _adjustState.runDir.replace(/\\/g,'/') + '/src.mp4';
    video.src = videoUrl;
    video.load();
  }
  if(video.paused){
    var items = _adjustState.items;
    console.log('[时间轴] 播放开始，共'+(items?items.length:0)+'段，_selectedSeg='+_selectedSeg);
    // 设置播放起始段
    if(_selectedSeg >= 0 && items && _selectedSeg < items.length){
      _playSegIndex = _selectedSeg;
    } else if(items && items.length > 0){
      _playSegIndex = 0;
      _selectedSeg = 0;
    } else {
      console.log('[时间轴] 没有片段数据，无法连续播放');
    }
    var startT = 0;
    if(_playSegIndex >= 0 && items && _playSegIndex < items.length){
      var it = items[_playSegIndex];
      startT = it ? (it.video_start || 0) : 0;
      var hint = document.getElementById('adjustVideoHint');
      if(hint) hint.textContent = '🎬 从第'+(_playSegIndex+1)+'/'+items.length+'段开始播放';
      var allSegs = document.querySelectorAll('.tl-vseg');
      for(var k=0;k<allSegs.length;k++){ allSegs[k].style.outline = ''; }
      if(allSegs[_playSegIndex]) allSegs[_playSegIndex].style.outline = '2px solid #22c55e';
    }
    // 等视频ready后seek再播放
    var doPlay = function(){
      _lastJumpAt = Date.now();
      try { video.currentTime = startT; } catch(e){}
      video.play().then(function(){
        if(btn) btn.textContent = '⏸ 暂停';
        startPlayheadSync();
      }).catch(function(e){
        if(btn) btn.textContent = '❌ ' + (e.message || '播放失败');
      });
    };
    if(video.readyState >= 1){
      doPlay();
    } else {
      if(btn) btn.textContent = '⏳ 加载中…';
      var onMeta = function(){
        video.removeEventListener('loadedmetadata', onMeta);
        doPlay();
      };
      video.addEventListener('loadedmetadata', onMeta);
    }
  } else {
    video.pause();
    if(btn) btn.textContent = '▶️ 播放';
    stopPlayheadSync();
  }
}

// 时间轴缩放
function zoomTimeline(factor){
  _tlZoom = Math.max(0.5, Math.min(20, _tlZoom * factor));
  var zl = document.getElementById('tlZoomLevel');
  if(zl) zl.textContent = _tlZoom.toFixed(1) + 'x';
  renderTimeline();
  // 滚动到选中片段
  if(_selectedSeg >= 0 && _adjustState.items && _selectedSeg < _adjustState.items.length){
    var it = _adjustState.items[_selectedSeg];
    if(it){
      var container = document.getElementById('timelineContainer');
      var inner = document.getElementById('timelineInner');
      if(container && inner){
        var pxPerSec = Math.max(4, 8 * _tlZoom);
        container.scrollLeft = (it.video_start || 0) * pxPerSec - container.clientWidth / 3;
      }
    }
  }
}

function startPlayheadSync(){
  stopPlayheadSync();
  var video = document.getElementById('adjGlobalVideo');
  if(!video) return;
  var _logCounter = 0;
  function tick(){
    if(video && !video.paused){
      var cur = video.currentTime;
      var items = _adjustState.items;
      // 计算播放头位置（按视频轨累计时长）
      var compTime = 0;
      if(items && _playSegIndex >= 0 && _playSegIndex < items.length){
        for(var i=0;i<_playSegIndex;i++){
          var pv = items[i].video_end - items[i].video_start;
          compTime += Math.max(0, pv);
        }
        var it = items[_playSegIndex];
        if(it){
          var vs = it.video_start || 0;
          var ve = it.video_end || 0;
          var vdur = Math.max(0, ve - vs);
          var within = Math.max(0, Math.min(cur - vs, vdur));
          compTime += within;
          _logCounter++;
          if(_logCounter % 60 === 0) console.log('[PR] 段'+(_playSegIndex+1)+'/'+items.length+' 源='+cur.toFixed(2)+' 成片='+compTime.toFixed(2)+' 取片='+vs.toFixed(1)+'-'+ve.toFixed(1));
          // 超出取片结尾或配音时长：切下一段
          if((ve > vs && cur >= ve - 0.02) || (it.duration && within >= it.duration - 0.02)){
            _playSegIndex++;
            if(_playSegIndex < items.length){
              var next = items[_playSegIndex];
              var nvs = next ? (next.video_start || 0) : 0;
              console.log('[PR] 切到段'+(_playSegIndex+1)+' 源@'+nvs.toFixed(1)+'s');
              video.currentTime = nvs;
              var hint = document.getElementById('adjustVideoHint');
              if(hint) hint.textContent = '✂️ 段'+(_playSegIndex)+'结束 → 段'+(_playSegIndex+1)+' 源@'+nvs.toFixed(1)+'s';
              var allSegs = document.querySelectorAll('.tl-vseg');
              for(var k=0;k<allSegs.length;k++){ allSegs[k].style.outline = ''; }
              if(allSegs[_playSegIndex]) allSegs[_playSegIndex].style.outline = '2px solid #22c55e';
            } else {
              console.log('[PR] 全部播完');
              video.pause();
              var btn = document.getElementById('adjPlayBtn');
              if(btn) btn.textContent = '▶️ 播放';
              var hint2 = document.getElementById('adjustVideoHint');
              if(hint2) hint2.textContent = '✅ 全部'+items.length+'段播放完毕';
              stopPlayheadSync();
              return;
            }
          } else if(cur < vs - 0.02){
            video.currentTime = vs;
          }
        }
      }
      updatePlayhead(compTime);
      _playheadRAF = requestAnimationFrame(tick);
    } else {
      _playheadRAF = null;
    }
  }
  _playheadRAF = requestAnimationFrame(tick);
}

function stopPlayheadSync(){
  if(_playheadRAF){ cancelAnimationFrame(_playheadRAF); _playheadRAF = null; }
}

// 选中片段时，监听视频seek，更新播放索引
function _enforceSegBounds(){
  var video = document.getElementById('adjGlobalVideo');
  if(!video || !_adjustState.items) return;
  // 用户手动seek时，找到最近的片段作为当前播放段
  var t = video.currentTime;
  var items = _adjustState.items;
  for(var i=0;i<items.length;i++){
    var vs = items[i].video_start || 0;
    var ve = items[i].video_end || 0;
    if(t >= vs && t <= ve){
      _playSegIndex = i;
      _selectedSeg = i;
      var allSegs = document.querySelectorAll('.tl-vseg');
      for(var k=0;k<allSegs.length;k++){ allSegs[k].style.outline = ''; }
      if(allSegs[i]) allSegs[i].style.outline = '2px solid #fbbf24';
      break;
    }
  }
}

function stopPlayheadSync(){
  if(_playheadRAF){ cancelAnimationFrame(_playheadRAF); _playheadRAF = null; }
}

// 确保全局视频源已加载
function ensureAdjVideoLoaded(){
  var video = document.getElementById('adjGlobalVideo');
  if(!video || !_adjustState.runDir) return;
  if(!video.src || video.src.indexOf('src.mp4') < 0){
    var videoUrl = '/media/' + _adjustState.runDir.replace(/\\/g,'/') + '/src.mp4';
    video.src = videoUrl;
    video.load();
  }
}

// 预览视频片段：同时播放视频段和配音
let _adjPlayingIdx = -1;
function previewVideoSegment(idx){
  const vs = parseFloat(document.getElementById('adjVStart'+idx).value) || 0;
  const ve = parseFloat(document.getElementById('adjVEnd'+idx).value) || 0;
  const vDiv = document.getElementById('adjVideo'+idx);
  const video = document.getElementById('adjVideoEl'+idx);
  const hint = document.getElementById('adjVideoHint'+idx);
  const audio = document.getElementById('adjAudio'+idx);
  const frameDiv = document.getElementById('adjFrame'+idx);

  // 停止之前播放的
  if(_adjPlayingIdx >= 0 && _adjPlayingIdx !== idx){
    const oldV = document.getElementById('adjVideoEl'+_adjPlayingIdx);
    const oldA = document.getElementById('adjAudio'+_adjPlayingIdx);
    const oldH = document.getElementById('adjVideoHint'+_adjPlayingIdx);
    if(oldV){ oldV.pause(); }
    if(oldA){ oldA.pause(); }
    if(oldH) oldH.style.display = 'none';
  }
  _adjPlayingIdx = idx;

  if(frameDiv) frameDiv.style.display = 'none';
  vDiv.style.display = 'block';
  hint.style.display = 'block';
  hint.textContent = '⏳ 加载视频中…';

  // 设置视频源
  const videoUrl = '/media/' + _adjustState.runDir.replace(/\\/g,'/') + '/src.mp4';
  const needLoad = !video.src || video.src.indexOf('src.mp4') < 0;
  if(needLoad){ video.src = videoUrl; }

  // 视频播放到end时停止
  const onTimeUpdate = function(){
    if(ve > vs && video.currentTime >= ve){
      video.pause();
      if(audio) audio.pause();
      hint.style.display = 'none';
      video.removeEventListener('timeupdate', onTimeUpdate);
    }
  };
  video.addEventListener('timeupdate', onTimeUpdate);
  video.onended = function(){ if(audio) audio.pause(); hint.style.display = 'none'; };

  // 等元数据加载后再跳转到指定时间并播放
  const doPlay = function(){
    try { video.currentTime = vs; } catch(e){}
    hint.textContent = '🎬 ' + vs.toFixed(1) + 's → ' + (ve > vs ? ve.toFixed(1)+'s' : '播放中') + ' · 配音同步';
    video.play().then(()=>{
      if(audio){ audio.currentTime = 0; audio.play().catch(()=>{}); }
    }).catch(e=>{
      hint.textContent = '❌ 播放失败: ' + e.message;
    });
  };

  if(video.readyState >= 1){ // HAVE_METADATA
    doPlay();
  } else {
    const onMeta = function(){
      video.removeEventListener('loadedmetadata', onMeta);
      doPlay();
    };
    video.addEventListener('loadedmetadata', onMeta);
    if(needLoad) video.load();
  }

  // 点击视频暂停/继续
  video.onclick = function(){
    if(video.paused){ video.play(); if(audio) audio.play().catch(()=>{}); hint.style.display='block'; }
    else { video.pause(); if(audio) audio.pause(); hint.style.display='none'; }
  };
}

function getSubtitleStyle(){
  return {
    fontSize: parseInt((document.getElementById('subFontSize')||{}).value) || 22,
    color: (document.getElementById('subColor')||{}).value || '#FFFFFF',
    outlineColor: (document.getElementById('subOutlineColor')||{}).value || '#000000',
    outlineWidth: parseFloat((document.getElementById('subOutlineWidth')||{}).value) || 2,
    alignment: parseInt((document.getElementById('subAlignment')||{}).value) || 2,
    marginV: parseInt((document.getElementById('subMarginV')||{}).value) || 50
  };
}

async function recommendSegments(idx){
  var text = document.getElementById('adjText'+idx).value.trim();
  if(!text){ alert('解说词不能为空'); return; }
  var box = document.getElementById('adjRecommend'+idx);
  if(!box) return;
  box.style.display = 'block';
  box.innerHTML = '<span style="font-size:12px;color:var(--muted)">⏳ 正在分析台词匹配最佳画面…</span>';
  try{
    var r = await fetch('/api/recommend_segments', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({run_id: _adjustState.runDir, text: text, seg_idx: idx})});
    var out = await r.json();
    if(!out.ok){ box.innerHTML = '<span style="color:#f87171;font-size:12px">❌ '+out.error+'</span>'; return; }
    if(!out.candidates || out.candidates.length === 0){
      box.innerHTML = '<span style="color:var(--muted);font-size:12px">未找到匹配片段（'+(out.note||'无匹配台词')+'），可手动输入时间</span>';
      return;
    }
    var html = '<div style="font-size:12px;color:var(--muted);margin-bottom:4px">🎯 找到'+out.candidates.length+'个匹配片段，点击应用：</div>';
    out.candidates.forEach(function(c, i){
      var dur = (c.end - c.start).toFixed(1);
      html += '<div style="display:flex;gap:6px;align-items:center;margin:3px 0;padding:6px 8px;background:var(--bg2);border-radius:6px;border:1px solid var(--border);cursor:pointer" onclick="applyRecommend('+idx+','+c.start+','+c.end+')">';
      html += '<span style="background:#6366f1;color:#fff;padding:1px 6px;border-radius:3px;font-size:11px;white-space:nowrap">候选'+(i+1)+'</span>';
      html += '<span style="font-size:12px;white-space:nowrap">'+c.start.toFixed(1)+'-'+c.end.toFixed(1)+'s ('+dur+'s)</span>';
      html += '<span style="font-size:11px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1">"'+c.dialogue+'"</span>';
      html += '<span style="font-size:10px;color:#4ade80;white-space:nowrap">匹配'+c.score+'词</span>';
      html += '</div>';
    });
    box.innerHTML = html;
  }catch(e){
    box.innerHTML = '<span style="color:#f87171;font-size:12px">❌ '+e.message+'</span>';
  }
}

function applyRecommend(idx, start, end){
  var it = _adjustState.items[idx];
  if(!it) return;
  _pushUndo();
  it.video_start = start;
  it.video_end = end;
  // 更新输入框
  var vsI = document.getElementById('adjVStart'+idx);
  var veI = document.getElementById('adjVEnd'+idx);
  if(vsI) vsI.value = start;
  if(veI) veI.value = end;
  renderTimeline();
  seekAdjVideo(start);
  var box = document.getElementById('adjRecommend'+idx);
  if(box) box.innerHTML = '<span style="color:#4ade80;font-size:12px">✅ 已应用 '+start.toFixed(1)+'-'+end.toFixed(1)+'s</span>';
  var hint = document.getElementById('adjustVideoHint');
  if(hint) hint.textContent = '✅ 第'+(idx+1)+'段已应用AI推荐片段';
}

async function regenSingleTts(idx){
  const text = document.getElementById('adjText'+idx).value.trim();
  if(!text){ alert('解说词不能为空'); return; }
  const statusEl = document.getElementById('adjStatus'+idx);
  const btn = event.target;
  btn.disabled = true; btn.textContent = '生成中…';
  statusEl.style.display = 'block'; statusEl.style.color = 'var(--muted)'; statusEl.textContent = '正在生成…';
  try{
    const r = await fetch('/api/tts_single', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ text: text, run_dir: _adjustState.runDir, index: idx }) });
    const out = await r.json();
    if(!out.ok) throw new Error(out.error || '失败');
    // 轮询任务结果
    const result = await pollTaskSimple(out.runid);
    if(result.error) throw new Error(result.error);
    // 更新音频
    const audio = document.getElementById('adjAudio'+idx);
    if(audio && result.audio){ audio.src = '/media/'+result.audio+'?t='+Date.now(); audio.load(); }
    _adjustState.items[idx].text = text;
    if(result.audio) _adjustState.items[idx].audio = result.audio;
    if(result.duration) _adjustState.items[idx].duration = result.duration;
    statusEl.style.color = '#4ade80'; statusEl.textContent = '✅ 已更新';
    setTimeout(function(){ statusEl.style.display='none'; }, 2000);
  }catch(e){
    statusEl.style.color = '#f87171'; statusEl.textContent = '❌ ' + e.message;
  }
  btn.disabled = false; btn.textContent = '🔄 重生成';
}

function deleteAdjustItem(idx){
  if(!_adjustState.items || idx < 0 || idx >= _adjustState.items.length) return;
  var it = _adjustState.items[idx];
  var textPreview = (it.text || '').substring(0, 30);
  if(!confirm('确定删除第 '+(idx+1)+' 段吗？\n\n"'+textPreview+'..."\n\n删除后不进合成，可撤销恢复。')) return;
  _pushUndo();
  _adjustState.items.splice(idx, 1);
  // 重置选中和播放索引
  if(_selectedSeg >= _adjustState.items.length) _selectedSeg = Math.max(0, _adjustState.items.length - 1);
  if(_playSegIndex >= _adjustState.items.length) _playSegIndex = Math.max(0, _adjustState.items.length - 1);
  // 重新渲染
  renderAdjustPanel(_adjustState.items, _adjustState.runDir, _adjustState.mode, []);
  var status = document.getElementById('adjustStatus');
  if(status) status.textContent = '🗑 已删除第 '+(idx+1)+' 段，剩余 '+_adjustState.items.length+' 段';
}

function pollTaskSimple(runid, onProgress, timeoutMs){
  const to = timeoutMs || 600000;  // 默认10分钟
  return new Promise((resolve, reject) => {
    const iv = setInterval(() => {
      fetch('/api/progress?run=' + runid).then(r => r.json()).then(p => {
        if(onProgress && (p.phase || p.pct !== undefined)) onProgress(p);
        if(p.done){ clearInterval(iv); resolve(p); }
      }).catch(() => {});
    }, 800);
    setTimeout(() => { clearInterval(iv); reject(new Error('超时（超过' + Math.round(to/1000) + '秒），可尝试单段重生成')); }, to);
  });
}

async function regenAllTts(){
  const status = document.getElementById('adjustStatus');
  status.textContent = '🔄 正在重新生成全部配音…';
  try{
    const texts = [];
    for(let i=0;i<_adjustState.items.length;i++){
      const t = document.getElementById('adjText'+i);
      texts.push(t ? t.value.trim() : _adjustState.items[i].text);
    }
    const r = await fetch('/api/tts_regen_all', { method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ texts: texts, run_dir: _adjustState.runDir }) });
    const out = await r.json();
    if(!out.ok) throw new Error(out.error || '失败');
    const result = await pollTaskSimple(out.runid, function(p){
      status.textContent = '🔄 ' + (p.phase || '正在生成…') + (p.pct !== undefined ? '（' + p.pct + '%）' : '');
    }, 600000);
    if(result.error) throw new Error(result.error);
    // 更新列表
    _adjustState.items = result.items || [];
    renderAdjustPanel(_adjustState.items, _adjustState.runDir, _adjustState.mode, []);
    status.textContent = '✅ 全部配音已重新生成（'+(_adjustState.items.length)+'段）';
  }catch(e){
    status.textContent = '❌ ' + e.message;
  }
}

async function confirmAdjustAndCompose(){
  const btn = document.getElementById('adjustConfirmBtn');
  btn.disabled = true; btn.textContent = '合成中…';
  // 收集跳过的段
  const skip = [];
  const finalItems = [];
  for(let i=0;i<_adjustState.items.length;i++){
    const cb = document.getElementById('adjSkip'+i);
    if(cb && cb.checked){ skip.push(i); continue; }
    const t = document.getElementById('adjText'+i);
    const vs = document.getElementById('adjVStart'+i);
    const ve = document.getElementById('adjVEnd'+i);
    finalItems.push({
      index: i,
      text: t ? t.value.trim() : _adjustState.items[i].text,
      audio: _adjustState.items[i].audio,
      video_start: vs ? parseFloat(vs.value) : (_adjustState.items[i].video_start || 0),
      video_end: ve ? parseFloat(ve.value) : (_adjustState.items[i].video_end || 0),
      audio_offset: _adjustState.items[i].audio_offset || 0
    });
  }
  if(finalItems.length === 0){
    alert('⚠️ 所有段都被跳过了，至少保留一段才能合成');
    btn.disabled = false; btn.textContent = '✅ 确认调整，开始合成视频';
    return;
  }
  try{
    const body = { run_dir: _adjustState.runDir, items: finalItems, skip: skip, params: { subtitle: getSubtitleStyle() } };
    // 带上配乐
    if(_adjustState.mode === 'movie' && document.getElementById('movieBgm') && document.getElementById('movieBgm').checked && MUSIC){
      if(MUSIC.catalogId){ body.music = { source:'catalog', catalogId: MUSIC.catalogId }; }
      else { body.music = { name: MUSIC.name, data: toB64(new Uint8Array(await MUSIC.file.arrayBuffer())) }; }
    }
    if(_adjustState.mode === 'nar' && document.getElementById('narBgm') && document.getElementById('narBgm').checked && MUSIC){
      if(MUSIC.catalogId){ body.music = { source:'catalog', catalogId: MUSIC.catalogId }; }
      else { body.music = { name: MUSIC.name, data: toB64(new Uint8Array(await MUSIC.file.arrayBuffer())) }; }
    }
    const r = await fetch('/api/movie_compose', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    const out = await r.json();
    if(!out.ok) throw new Error(out.error || '失败');
    await pollMovieCompose(out.runid, _adjustState.mode);
  }catch(e){
    alert('合成失败：' + e.message);
    btn.disabled = false; btn.textContent = '✅ 确认调整，开始合成视频';
  }
}
