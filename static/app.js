
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
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) || '{}');
    if (saved.res && $('res').querySelector('option[value="'+saved.res+'"]')) $('res').value = saved.res;
    if (saved.fps) $('fps').value = saved.fps;
    if (saved.trans) $('trans').value = saved.trans;
    if (saved.beatStep) $('beatStep').value = saved.beatStep;
    if (saved.hardCutSel) $('hardCutSel').value = saved.hardCutSel;
    if (saved.defDur) $('defDur').value = saved.defDur;
    if ($('aiCap')) $('aiCap').checked = !!saved.aiCap;
  } catch(e){}
  function save(){
    try { localStorage.setItem(KEY, JSON.stringify({ res:$('res').value, fps:$('fps').value, trans:$('trans').value, beatStep:$('beatStep').value, hardCutSel:$('hardCutSel').value, defDur:$('defDur').value, aiCap: $('aiCap')?$('aiCap').checked:false })); } catch(e){}
  }
  ['res','fps','trans','beatStep','hardCutSel','defDur','aiCap'].forEach(id => { const el=$(id); if(el) el.addEventListener('change', save); });
})();

// ---- background music upload ----
let MUSIC = null;
const musDrop = $('musDrop'), mi = $('musInput');
function setMusic(file){
  if (!file) { MUSIC = null; musDrop.textContent = '把 mp3/wav 音乐拖到这里，或点此选择背景音乐'; $('musInfo2').style.display='none'; return; }
  MUSIC = { name:file.name, file };
  musDrop.textContent = '🎵 已选：' + file.name;
  $('musInfo').style.display = file ? 'none' : '';
  $('musInfo2').style.display = 'block';
  $('musInfo2').textContent = '已选音乐，合成时自动分析节拍并对齐素材切换点。';
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
  };
  fetch('/api/ai/config', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) })
    .then(r=>r.json()).then(res=>{
      const st=[];
      if(res.ok){
        st.push(res.vision_available?'视觉✅':'视觉(离线)');
        st.push(res.tts_available?'配音✅':'配音未配');
        $('aiStatus').textContent = '✅ 已保存 · ' + st.join(' · ');
      } else $('aiStatus').textContent = '❌ '+res.error;
    }).catch(()=>$('aiStatus').textContent='❌ 保存失败');
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
        <a class="btn mini ghost" href="/media/${h.file}" download="spring-${h.time||''}.mp4">⬇ 下载</a>`;
      d.querySelector('a').addEventListener('click', (e)=>{ e.preventDefault(); const a=e.currentTarget; a.href='/media/'+h.file+'?t='+Date.now(); a.click(); });
      box.appendChild(d);
    });
  }).catch(()=>{});
}
loadAIConfig();
loadHistory();

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

function setRes(res, name){ $('res').value = res; $('status').textContent = '已设为：' + name + ' (' + res + ')'; }
function cancelBuild(){
  if (_currentRunid){
    fetch('/api/cancel', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({runid:_currentRunid}) }).then(r=>r.json()).then(()=>{
      $('status').textContent='⏹ 正在取消…';
    }).catch(()=>{});
  }
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
async function buildBeatCut(){
  if(!BC_VIDEO){ $('bcStatus').textContent='❌ 请先拖入视频'; return; }
  if(!MUSIC){ $('bcStatus').textContent='❌ 请先选择背景音乐（上方🎵或🔎曲库）'; return; }
  const go=$('bcGo'); go.disabled=true; $('bcResult').style.display='none';
  $('bcStatus').textContent='上传视频…';
  const body = { video:{name:BC_VIDEO.name, data: toB64(new Uint8Array(await BC_VIDEO.arrayBuffer()))}, music:null, params:{w:1280,h:720,fps:30,sceneTh:0.30,maxCuts:30} };
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
        if(p.done){
          clearInterval(iv); $('bcBar').style.display='none';
          if(p.error){ $('bcStatus').textContent='❌ '+p.error; resolve(); return; }
          $('bcStatus').textContent='✅ 完成';
          $('bcResult').style.display='block';
          $('bcPlayer').src='/media/'+p.file+'?t='+Date.now();
          $('bcDl').href='/media/'+p.file;
          const d=p.diag||{};
          let txt='切换点 '+ (d.segments||0) +' 个 · 场景切 '+ (d.scene_cuts||[]).length +' · 动作点 '+ (d.motion_cuts||[]).length +' · 强拍 '+ (d.strong_beats||[]).length;
          if(d.timeline){ txt += ' · 时间线 [' + d.timeline.map(t=>t.toFixed(1)).join(', ') + ']s'; }
          $('bcDiag').textContent = txt;
          resolve(); return;
        }
        $('bcStatus').textContent = (p.phase||'分析中')+'… '+(p.pct||0)+'%';
      }).catch(()=>{});
    },400);
    setTimeout(()=>{ clearInterval(iv); resolve(); }, 600000);
  });
}

async function build(){
  const go = $('go'); go.disabled = true; $('result').style.display='none';
  $('cancelBtn').style.display = '';
  $('status').textContent = '上传素材…'; setBar(2);
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
        const sec = Math.round((Date.now()-t0)/1000);
        if (p.done){
          clearInterval(iv); stopBar();
          if (p.error){ $('status').textContent='❌ '+p.error; resolve(); return; }
          $('status').textContent = '✅ 完成（'+(p.duration||'')+'s）';
          if (p.beat && p.beat.bpm){ $('musInfo2').textContent='💿 BPM '+p.beat.bpm+' · 节拍 '+p.beat.beat_count+' · 时长 '+(p.duration||'')+'s · 每'+ (p.beat.beatStep||1) +'拍切换，已对齐节拍。'; }
          $('result').style.display='block';
          $('player').src = '/media/' + p.file + '?t=' + Date.now();
          $('dl').href = '/media/' + p.file;
          resolve(); return;
        }
        $('status').textContent = (p.phase||'合成中') + '… ' + (p.pct||0) + '%（已 ' + sec + ' 秒）';
      }).catch(()=>{});
    }, 400);
    // safety: don't poll forever
    setTimeout(()=>{ clearInterval(iv); stopBar(); $('status').textContent='⚠️ 超时，请查看结果后重试'; resolve(); }, 600000);
  });
}
