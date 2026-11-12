# -*- coding: utf-8 -*-
"""v1.4.61 前端**执行级**回归钉（照 v1.4.50 模式）。

源码钉只防「被人改回」，防不住「自己改坏」：v1.4.49 误删 `let _srt` 时
node --check 与所有源码钉全绿，真机导入链却直接抛 ReferenceError。本文件
在 node 里真跑两条新链路：① 弹幕 XML 载入 → UI/状态更新；② 候选台词
toggle → 展开/收起、时间格式化、空态。
"""
import json
import os
import subprocess

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_JS = os.path.join(_ROOT, "static", "desktop", "desktop.js")
_HTML = os.path.join(_ROOT, "static", "desktop", "index.html")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


_HARNESS = r'''
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
const els = {};
function $(id){ return (els[id] = els[id] || {textContent:"", innerHTML:"", style:{},
  value:"", checked:false, addEventListener(){}}); }
const toasts = [];
function toast(m){ toasts.push(String(m)); }
function lcPreviewSeg(){}
const _lcVdur = 300;
/* 轻量 DOM 桩：够 _lcRow 真跑（createElement / append / appendChild /
   querySelector('.lc-s') / querySelector('input') / addEventListener）。 */
function makeEl(tag){
  return {
    tag, className:"", textContent:"", value:"", style:{}, children:[], listeners:{},
    append(...xs){ for(const x of xs) this.children.push(x); },
    appendChild(x){ this.children.push(x); return x; },
    addEventListener(ev, fn){ this.listeners[ev] = fn; },
    querySelector(sel){
      if(sel.charAt(0) === ".") return findCls(this, sel.slice(1));
      return findTag(this, sel);
    }
  };
}
function hasCls(node, cls){
  return String(node.className || "").split(/\s+/).indexOf(cls) >= 0;
}
function findCls(node, cls){
  if(hasCls(node, cls)) return node;
  for(const c of (node.children || [])){ const r = findCls(c, cls); if(r) return r; }
  return null;
}
function findTag(node, tag){
  if(node.tag === tag) return node;
  for(const c of (node.children || [])){ const r = findTag(c, tag); if(r) return r; }
  return null;
}
const document = { createElement: makeEl };
function find(node, cls){
  if(hasCls(node, cls)) return node;
  for(const c of (node.children || [])){ const r = find(c, cls); if(r) return r; }
  return null;
}
const src = [
  lineOf("let _lcDanmakuText"),
  blockOf("function _lcDanmakuUi"),
  blockOf("function _lcDanmakuLoaded"),
  blockOf("function _lcFmtT"),
  blockOf("function _lcRow"),
  "\nreturn { loaded: _lcDanmakuLoaded, ui: _lcDanmakuUi, row: _lcRow, toast: toast, els: () => els, toasts: () => toasts, text: () => _lcDanmakuText };"
].join("\n");
const api = new Function("$", "document", "toast", "lcPreviewSeg", "_lcVdur", "find", src)(
  $, document, toast, lcPreviewSeg, _lcVdur, find);
const out = {steps: []};
/* ① 弹幕载入链 */
api.loaded('<i><d p="12.5,1,25,16777215,0,0,0,0">哈哈</d></i>', 'danmaku.xml');
out.danmakuText = api.text().indexOf("哈哈") >= 0;
out.label = api.els().lcDanmakuLabel.textContent;
out.clearShown = api.els().lcDanmakuClear.style.display === "";
out.toastOk = api.toasts().some(t => t.indexOf("弹幕已载入") >= 0);
/* ② 候选台词 toggle */
const c = {start: 10, end: 30, dur: 20, score: 0.8, reasons: ["弹幕热度峰值"],
           peak_t: 20, lines: [{start: 12, end: 15, text: "卧槽这波"},
                               {start: 20, end: 22, text: "666"}]};
const row = api.row(c, 0);
const btn = find(row, "lc-lines-btn");
const box = find(row, "lc-lines");
out.hasToggle = !!btn && !!box;
out.initHidden = box.style.display === "none";
out.lineCount = box.children.length;
out.firstLine = box.children[0].textContent;
btn.listeners.click();
out.openShown = box.style.display === "block";
out.openLabel = btn.textContent;
btn.listeners.click();
out.closeHidden = box.style.display === "none";
out.closeLabel = btn.textContent;
/* ③ 无台词（未转写/纯音乐）要有可读空态，不能是一片空白 */
const row2 = api.row({start: 0, end: 20, dur: 20, reasons: [], score: "", lines: []}, 1);
const box2 = find(row2, "lc-lines");
out.emptyHint = box2.children[0].textContent;
console.log(JSON.stringify(out));
'''


def test_danmaku_and_lines_flow_actually_runs():
    """node 真跑：弹幕载入更新 UI、台词 toggle 展开/收起、空态可读。"""
    env = dict(os.environ)
    env["FC_JS"] = _JS
    p = subprocess.run(["node", "-"], input=_HARNESS, capture_output=True,
                       text=True, encoding="utf-8", env=env, timeout=60, cwd=_ROOT)
    assert p.returncode == 0, "harness 失败 rc=%s err=%s" % (p.returncode, (p.stderr or "")[-900:])
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out["danmakuText"] and "已载入弹幕 XML" in out["label"], '弹幕载入后 UI 必须更新'
    assert out["clearShown"] and out["toastOk"], '载入后要有清除入口与成功提示'
    assert out["hasToggle"] and out["initHidden"], '台词列表默认收起'
    assert out["lineCount"] == 2 and out["firstLine"].startswith("00:12"), out["firstLine"]
    assert "卧槽这波" in out["firstLine"], '台词文本必须可见'
    assert out["openShown"] and "收起" in out["openLabel"], '点击展开'
    assert out["closeHidden"] and out["closeLabel"] == "💬 台词", '再点收起'
    assert "没有台词记录" in out["emptyHint"], '空态必须可读'


def test_danmaku_wiring_source_pins():
    """链路源码钉：桥读取 / 解码链 / 请求体直传 / 后端放宽上限 / 缓存戳 38。"""
    js, html = _read(_JS), _read(_HTML)
    assert 'id="lcDanmaku"' in html and 'id="lcDanmakuClear"' in html
    assert 'id="lcDanmakuLabel"' in html and 'B 站直播弹幕 XML' in html
    assert "window.pywebview.api.read_text_file(r.path).then" in js, '桌面走桥读取'
    assert "_decodeSubtitleBytes(await f.arrayBuffer())" in js, '浏览器走 v1.4.49 解码链'
    assert "body.danmaku_text = _lcDanmakuText" in js, '分析请求必须带上弹幕文本'
    with open(os.path.join(_ROOT, "handler.py"), encoding="utf-8") as f:
        hd = f.read()
    assert "max_len=8 * 1024 * 1024" in hd, '弹幕 XML 会超 64KB，请求体上限必须放宽'
    assert 'desktop.js?v=38' in html and 'desktop.css?v=38' in html
