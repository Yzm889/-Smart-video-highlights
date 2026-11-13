# -*- coding: utf-8 -*-
"""v1.4.84 配音引擎引导随实际状态走——回归钉。

主人实测抓的引导矛盾：IndexTTS2 未装时，状态区写「未装」、下拉却写
「自动（IndexTTS2 克隆优先）」——用户没法判断现在会用谁、要不要装。
契约：下拉标签 / 提示 / 引擎状态引导行必须反映真实就绪状态。
"""

import json
import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_JS = os.path.join(ROOT, "static", "desktop", "desktop.js")
_HTML = os.path.join(ROOT, "static", "desktop", "index.html")


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
const els = {};
function $(id){
  return (els[id] = els[id] || {textContent:"", innerHTML:"", style:{}, value:"",
    disabled:false, dataset:{}, listeners:{}, addEventListener(ev, fn){ this.listeners[ev] = fn; }});
}
function _esc(s){ return String(s || "").replace(/&/g, "&amp;"); }
let fetchCalls = [];
let statusPayloads = [];
function fetch(url, opt){
  fetchCalls.push({url, body: (opt && opt.body) || ""});
  let payload = {};
  if(url === "/api/tts/voices"){
    payload = {voices:["v1"], cfg:{engine:"auto", rate:"+0%"}, emo_words:[],
               tts2: statusPayloads.shift() || {}, label:"", tts2b:{}};
  } else if(url === "/api/tts/cosyvoice/voices"){
    payload = {voices:[]};
  }
  return Promise.resolve({json: async () => payload});
}
function toast(){}
function _ttsStateHint(){ return []; }
function renderTtsInstalls(){}
const TTS_ENGINES = [["auto","旧标签"],["qwen3tts","旧Q"],["indextts2","旧I"]];
const src = [
  blockOf("function _autoEngineLabel"),
  blockOf("function _qwenEngineLabel"),
  blockOf("function _autoEngineHint"),
  blockOf("async function fillTtsSettings"),
  "\nstate.fill = fillTtsSettings;"
].join("\n");
const state = {};
new Function("$", "_esc", "fetch", "toast", "TTS_ENGINES", "_ttsStateHint",
  "renderTtsInstalls", "statusPayloads", "state", src)(
  $, _esc, fetch, toast, TTS_ENGINES, _ttsStateHint, renderTtsInstalls,
  statusPayloads, state);
const out = {};
const _st = (q, i) => ({qwen3tts: q, indextts2: i});

/* ① 双装：克隆优先原标签 */
statusPayloads.unshift(_st(true, true));
await state.fill();
out.both = {auto: els.setTtsEngine.value,
            autoText: (els.setTtsEngine.innerHTML.match(/value="auto"[^>]*>([^<]*)/)||[])[1],
            qwenText: (els.setTtsEngine.innerHTML.match(/value="qwen3tts"[^>]*>([^<]*)/)||[])[1],
            hint: els.ttsEngineHint.textContent};

/* ② 只装 Qwen3-TTS（主人现状）：自动标签必须说清「当前用谁」 */
statusPayloads.unshift(_st(true, false));
await state.fill();
out.qwenOnly = {autoText: (els.setTtsEngine.innerHTML.match(/value="auto"[^>]*>([^<]*)/)||[])[1],
                qwenText: (els.setTtsEngine.innerHTML.match(/value="qwen3tts"[^>]*>([^<]*)/)||[])[1],
                hint: els.ttsEngineHint.textContent};

/* ③ 都没装：引导去模型中心 */
statusPayloads.unshift(_st(false, false));
await state.fill();
out.none = {autoText: (els.setTtsEngine.innerHTML.match(/value="auto"[^>]*>([^<]*)/)||[])[1],
            hint: els.ttsEngineHint.textContent};
console.log(JSON.stringify(out));
};
main();
'''


def test_engine_guidance_follows_real_state():
    """node 真跑：下拉标签与提示必须随就绪状态变化，不许再「谎报」。"""
    env = dict(os.environ)
    env["FC_JS"] = _JS
    p = subprocess.run(["node", "-"], input=_HARNESS, capture_output=True,
                       text=True, encoding="utf-8", env=env, timeout=60, cwd=ROOT)
    assert p.returncode == 0, "harness rc=%s err=%s" % (p.returncode, (p.stderr or "")[-900:])
    out = json.loads(p.stdout.strip().splitlines()[-1])
    # 双装：克隆优先
    assert "IndexTTS2 克隆优先" in out["both"]["autoText"], out["both"]
    assert "克隆" in out["both"]["qwenText"]
    # 只装 Qwen3-TTS（主人现状）：自动标签必须说清「当前用谁」
    assert "IndexTTS2 未装" in out["qwenOnly"]["autoText"], out["qwenOnly"]
    assert "当前用 Qwen3-TTS 配音" in out["qwenOnly"]["autoText"]
    assert "现在就能跑" in out["qwenOnly"]["hint"], out["qwenOnly"]["hint"]
    # 都没装：引导去模型中心
    assert "模型中心" in out["none"]["autoText"] and "一键安装" in out["none"]["autoText"], out["none"]


def test_guidance_source_pins():
    js, html = _read(_JS), _read(_HTML)
    assert 'id="ttsEngineHint"' in html, '下拉下方提示必须有 id 供 JS 填充'
    assert "_engHint.textContent = _autoEngineHint(st2)" in js
    # 引擎状态区：先说「现在能不能跑」再说缺什么
    assert "现在就能配音" in js, '引擎状态区缺「可配音」引导行'
    assert "另一个装不装都不影响当前使用" in js
    assert _cache_bust_ok(html), '缓存戳必须成对'
