# -*- coding: utf-8 -*-
"""v1.4.76 情感接线前端**执行级**回归钉（照 v1.4.50 模式）。

v1.4.49 的教训：源码钉全绿、真机 ReferenceError。这里在 node 里真跑
设置页情感链路：① fillTtsSettings 回填三件套 + 按钮按后端词表渲染；
② 点情绪按钮 → 请求带 emo_name → 音频回放、busy 复位；③ 强度滑条显示联动。
"""
import json
import os
import re
import subprocess

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_JS = os.path.join(_ROOT, "static", "desktop", "desktop.js")
_HTML = os.path.join(_ROOT, "static", "desktop", "index.html")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _cache_bust_ok(html):
    js_v = re.search(r'desktop\.js\?v=(\d+)', html)
    css_v = re.search(r'desktop\.css\?v=(\d+)', html)
    return bool(js_v and css_v and js_v.group(1) == css_v.group(1))


_HARNESS = r'''
const main = async () => {
const fs = require("fs");
const raw = fs.readFileSync(process.env.FC_JS, "utf8");
const lines = raw.split("\n");
function blockOf(prefix){
  const start = lines.findIndex(l => l.startsWith(prefix));
  if(start < 0) throw new Error("not found: " + prefix);
  let depth = 0, started = false;
  for(let i = start; i < lines.length; i++){
    for(const ch of lines[i]){
      if(ch === "{"){ depth++; started = true; } else if(ch === "}"){ depth--; }
    }
    if(started && depth === 0) return lines.slice(start, i + 1).join("\n");
  }
  throw new Error("no end: " + prefix);
}
function lineOf(prefix){
  const l = lines.find(l => l.startsWith(prefix));
  if(!l) throw new Error("no decl: " + prefix);
  return l;
}
/* DOM/网络桩：只覆盖抽取出的函数真正摸到的东西 */
const els = {};
function $(id){
  return (els[id] = els[id] || {textContent:"", innerHTML:"", style:{}, value:"",
    disabled:false, dataset:{}, listeners:{}, addEventListener(ev, fn){ this.listeners[ev] = fn; }});
}
function _esc(s){ return String(s || "").replace(/&/g, "&amp;"); }
let audioSrc = "", audioPlayed = false;
class Audio {
  constructor(src){ audioSrc = src; }
  play(){ audioPlayed = true; return Promise.resolve(); }
}
let fetchCalls = [];
function fetch(url, opt){
  fetchCalls.push({url, body: (opt && opt.body) || ""});
  let payload = {};
  if(url === "/api/tts/voices"){
    payload = {voices:["v1"], cfg:{engine:"auto", voice:"", cosy_voice:"", rate:"+0%",
      instruct:"", emotion:"vector", emotion_alpha:0.8, interval_silence:350},
      emo_words:["紧张","悲伤"], label:"", tts2:{}};
  } else if(url === "/api/tts/cosyvoice/voices"){
    payload = {voices:[]};
  } else if(url === "/api/tts/test"){
    payload = {ok:true, message:"✅ 试听已生成（IndexTTS2，1.2 秒）", engine:"indextts2",
               file:"_tts_test/sample.wav"};
  }
  return Promise.resolve({json: async () => payload});
}
function toast(){}
function _ttsStateHint(){ return []; }
function renderTtsInstalls(){}
const TTS_ENGINES = [["auto","自动"],["qwen3tts","Q"],["indextts2","I"]];
const state = {};
const src = [
  lineOf("const EMO_SAMPLE_TEXT"),
  blockOf("function renderEmoPreview"),
  blockOf("function _autoEngineLabel"),
  blockOf("function _qwenEngineLabel"),
  blockOf("function _autoEngineHint"),
  blockOf("async function fillTtsSettings"),
  blockOf("$('emoPreviewRow').addEventListener('click'"),
  blockOf("$('setTtsEmoAlpha').addEventListener('input'"),
  "\nstate.fillP = fillTtsSettings();"
].join("\n");
new Function("$", "_esc", "Audio", "fetch", "toast", "TTS_ENGINES",
  "_ttsStateHint", "renderTtsInstalls", "state", src)($, _esc, Audio, fetch, toast,
  TTS_ENGINES, _ttsStateHint, renderTtsInstalls, state);
const out = {};

/* ① fillTtsSettings：三件套回填 + 试听按钮按后端词表渲染 */
await state.fillP;
out.emotionVal = els.setTtsEmotion.value;
out.alphaVal = +els.setTtsEmoAlpha.value;
out.alphaDisplay = els.setTtsEmoAlphaVal.textContent;
out.gapVal = +els.setTtsGap.value;
out.previewCount = (els.emoPreviewRow.innerHTML.match(/emo-btn/g) || []).length;
out.firstEmo = (els.emoPreviewRow.innerHTML.match(/data-emo="([^"]+)"/) || [])[1];

/* ② 点情绪按钮：请求带 emo_name + 固定示例句；成功后播放、busy 复位 */
const fakeBtn = {dataset: {emo: "紧张", busy: ""}, disabled: false};
await els.emoPreviewRow.listeners.click({target: {closest: c => c === ".emo-btn" ? fakeBtn : null}});
const testCall = fetchCalls.find(c => c.url === "/api/tts/test");
out.emoRequestBody = testCall ? testCall.body : "";
out.audioSrc = audioSrc;
out.audioPlayed = audioPlayed;
out.busyCleared = fakeBtn.dataset.busy === "" && fakeBtn.disabled === false;
out.hint = els.emoPreviewHint.textContent;

/* ③ 强度滑条联动显示 */
els.setTtsEmoAlpha.value = "0.75";
els.setTtsEmoAlpha.listeners.input();
out.alphaAfterInput = els.setTtsEmoAlphaVal.textContent;
console.log(JSON.stringify(out));
};
main();
'''


def test_emotion_frontend_actually_runs():
    env = dict(os.environ)
    env["FC_JS"] = _JS
    p = subprocess.run(["node", "-"], input=_HARNESS, capture_output=True,
                       text=True, encoding="utf-8", env=env, timeout=60, cwd=_ROOT)
    assert p.returncode == 0, "harness 失败 rc=%s err=%s" % (p.returncode, (p.stderr or "")[-900:])
    out = json.loads(p.stdout.strip().splitlines()[-1])
    # 回填链：设置页打开时三件套必须按 cfg 落到控件上（v1.4.49 教训：断链=ReferenceError）
    assert out["emotionVal"] == "vector", out
    assert abs(out["alphaVal"] - 0.8) < 1e-9 and out["alphaDisplay"] == "0.80"
    assert out["gapVal"] == 350
    # 试听按钮来自后端词表 emo_words
    assert out["previewCount"] == 2 and out["firstEmo"] == "紧张"
    # 点击 → 请求带 emo_name + 固定示例句；成功后播放、busy 复位
    body = (out["emoRequestBody"] or "").replace(" ", "")
    assert '"emo_name":"紧张"' in body, out["emoRequestBody"]
    assert "他推开门" in body, '情绪试听必须用固定示例句'
    assert out["audioPlayed"] and out["audioSrc"].endswith("/media/_tts_test/sample.wav")
    assert out["busyCleared"]
    assert "紧张" in out["hint"] and "试听已生成" in out["hint"]
    # 滑条 → 显示联动
    assert out["alphaAfterInput"] == "0.75"


def test_emotion_wiring_source_pins():
    """链路源码钉：HTML 控件存在、保存 payload 带三件套、后端白名单收下、缓存戳同步。"""
    js, html = _read(_JS), _read(_HTML)
    for eid in ("setTtsEmotion", "setTtsEmoAlpha", "setTtsEmoAlphaVal",
                "setTtsGap", "emoPreviewRow", "emoPreviewHint"):
        assert 'id="%s"' % eid in html, '设置页缺 #%s' % eid
    # 保存 payload 必须带三件套（后端白名单有收，漏存=刷新弹回）
    assert "emotion: _emoEl ? (_emoEl.value || 'auto') : 'auto'" in js
    assert "emotion_alpha: _eaEl ? (+_eaEl.value || 0.6) : 0.6" in js
    assert "interval_silence: _gapEl ? (Math.round(+_gapEl.value) || 0) : 200" in js
    # 回填 + 渲染链在
    assert "renderEmoPreview(v.emo_words || [])" in js
    assert "cfg.emotion || 'auto'" in js
    with open(os.path.join(_ROOT, "handler.py"), encoding="utf-8") as f:
        hd = f.read()
    assert "emo in ('auto', 'vector', 'off')" in hd, '/api/ai/config 必须收 emotion 白名单'
    assert "cur['interval_silence'] = int(float(inc['interval_silence']))" in hd
    assert "'emo_words': list(__import__('tts2_engines').EMO_VECTORS.keys())" in hd
    assert _cache_bust_ok(html), 'desktop.js/css 缓存戳必须存在且版本号一致'
