# -*- coding: utf-8 -*-
"""v1.4.78 模型中心·配音权重在线下载回归钉。

背景：权重当年靠主人手工脚本（tts2/dl_models.py、tts2/indextts2/dl_ckpt.py，
modelscope）装进来，普通用户没有入口。收编成 tts2_models 统一模块后钉住：
下载落点与 tts2_engines 既有路径一致、状态机契约、下载子进程命令行
（打包版 EXE 自举 --tts2-dl-worker）、嵌套布局自动摊平、取消杀进程、
前端执行级卡片渲染三态与点击请求体、voice 页引导闭环。
"""

import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tts2_engines as T2  # noqa: E402
import tts2_models as M  # noqa: E402

JS = os.path.join(ROOT, 'static', 'desktop', 'desktop.js')


def setup_function(fn):
    M._STATE.update(running=False, engine='', op='', pct=0, mb_done=0, mb_total=0,
                    speed_mb_s=0.0, msg='', ok=None, cancel=False)
    M._CUR_PROC['p'] = None


def _dst(eng, i):
    _repo, rel = M.CATALOG[eng]['repos'][i]
    return os.path.join(T2.TTS2_DIR, rel)


def test_catalog_targets_existing_tts2_dirs():
    """下载落点必须与 tts2_engines 的既有路径一致（下错目录 = 白下 12GB）。"""
    assert os.path.normcase(_dst('qwen3tts', 0)).endswith(
        os.path.normcase(os.path.join('models', 'VoiceDesign')))
    assert os.path.normcase(_dst('qwen3tts', 1)).endswith(
        os.path.normcase(os.path.join('models', 'Tokenizer')))
    assert os.path.normcase(_dst('indextts2', 0)).endswith(
        os.path.normcase(os.path.join('indextts2', 'checkpoints_2')))


def test_status_shape_and_ready_flags(monkeypatch):
    monkeypatch.setattr(T2, 'qwen3tts_ready', lambda: True)
    monkeypatch.setattr(T2, 'indextts2_ready', lambda: False)
    st = M.status()
    assert set(st['engines']) == {'qwen3tts', 'indextts2'}
    assert st['engines']['qwen3tts']['ready'] is True
    assert st['engines']['indextts2']['ready'] is False
    for eng in st['engines']:
        assert 'label' in st['engines'][eng] and 'size_hint' in st['engines'][eng]
        assert 'weights' in st['engines'][eng]


def test_weights_ok_needs_real_bytes(monkeypatch):
    """建了空目录不算「权重已下载」——判据是真实字节数（防假就绪）。"""
    monkeypatch.setattr(M, '_dir_min_mb', lambda p, mb: False)
    assert M.CATALOG['qwen3tts']['weights_ok']() is False
    assert M.CATALOG['indextts2']['weights_ok']() is False


def test_download_async_guards_concurrency():
    M._STATE.update(running=True, engine='qwen3tts')
    ok, msg = M.download_async('indextts2')
    assert ok is False and '已有下载任务' in msg
    ok2, msg2 = M.download_async('bogus')
    assert ok2 is False and '未知引擎' in msg2


def _fake_repo_cmd(fixture_py):
    """夹具「仓库」下载器：直接跑本地脚本模仿 snapshot_download。"""
    return lambda spec: [sys.executable, str(fixture_py), json.dumps(spec)]


def test_download_runs_subprocess_and_reports(monkeypatch, tmp_path):
    """真跑一次子进程下载（夹具瞬间完成），状态机要走完 ok=True。"""
    fixture = tmp_path / 'fake_hf.py'
    fixture.write_text('\n'.join([
        'import json, os, sys',
        'spec = json.loads(sys.argv[1])',
        'os.makedirs(spec["dst"], exist_ok=True)',
        'with open(os.path.join(spec["dst"], "config.yaml"), "wb") as f:',
        '    f.write(b"0" * (1024 * 1024))   # 1MB',
        'print("OK", flush=True)',
    ]), encoding='utf-8')
    monkeypatch.setattr(M, '_dl_cmd', _fake_repo_cmd(fixture))
    monkeypatch.setattr(T2, 'TTS2_DIR', str(tmp_path / 'tts2'))
    monkeypatch.setitem(M.CATALOG['indextts2'], 'weights_ok', lambda: True)
    ok, msg = M.download_async('indextts2')
    assert ok is True, msg
    deadline = time.time() + 20
    while time.time() < deadline and M._STATE['running']:
        time.sleep(0.2)
    assert M._STATE['running'] is False
    assert M._STATE['ok'] is True, M._STATE['msg']
    assert '下载完成' in M._STATE['msg']
    assert (tmp_path / 'tts2' / 'indextts2' / 'checkpoints_2' / 'config.yaml').is_file()


def test_download_cancel_kills_subprocess(monkeypatch, tmp_path):
    """取消 = 杀子进程：慢仓库下到一半点取消，状态转「已取消」，进程被回收。"""
    slow = tmp_path / 'slow_dl.py'
    slow.write_text('\n'.join([
        'import json, os, sys, time',
        'spec = json.loads(sys.argv[1])',
        'os.makedirs(spec["dst"], exist_ok=True)',
        'with open(os.path.join(spec["dst"], "part.bin"), "wb") as f:',
        '    f.write(b"0" * (2 * 1024 * 1024))',
        'time.sleep(60)   # 假装还在慢慢下',
    ]), encoding='utf-8')
    monkeypatch.setattr(M, '_dl_cmd', _fake_repo_cmd(slow))
    monkeypatch.setattr(T2, 'TTS2_DIR', str(tmp_path / 'tts2'))
    monkeypatch.setitem(M.CATALOG['indextts2'], 'weights_ok', lambda: False)
    ok, _msg = M.download_async('indextts2')
    assert ok is True
    time.sleep(1.0)
    cok, cmsg = M.cancel()
    assert cok is True, cmsg
    deadline = time.time() + 20
    while time.time() < deadline and M._STATE['running']:
        time.sleep(0.2)
    assert M._STATE['running'] is False
    assert M._STATE['ok'] is False and '取消' in M._STATE['msg']


def test_flatten_if_nested_moves_marker_up(tmp_path):
    """HF 仓库权重若带子目录（dst/<子目录>/config.yaml），下载完自动摊平。"""
    dst = tmp_path / 'checkpoints_2'
    sub = dst / 'checkpoints_2'
    sub.mkdir(parents=True)
    (sub / 'config.yaml').write_text('x')
    (sub / 'gpt.pth').write_bytes(b'0' * 10)
    assert M._flatten_if_nested(str(dst)) is True
    assert (dst / 'config.yaml').is_file() and (dst / 'gpt.pth').is_file()
    dst2 = tmp_path / 'flat'
    dst2.mkdir()
    (dst2 / 'config.yaml').write_text('x')
    assert M._flatten_if_nested(str(dst2)) is True


def test_dl_cmd_frozen_bootstraps_exe(monkeypatch):
    """打包版用 EXE 自举（--tts2-dl-worker），源码版用解释器 + 内联脚本。"""
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    cmd = M._dl_cmd({'repo': 'a/b', 'dst': 'd'})
    assert cmd[0] == sys.executable and '--tts2-dl-worker' in cmd[1:3]
    monkeypatch.setattr(sys, 'frozen', False, raising=False)
    cmd2 = M._dl_cmd({'repo': 'a/b', 'dst': 'd'})
    assert '-c' in cmd2 and M._DL_SCRIPT in cmd2[cmd2.index('-c') + 1]


def test_dl_worker_main_runs_real_download(tmp_path, monkeypatch):
    """dl_worker_main（打包版子进程入口）真跑：夹具 hf 包成功返回 0、坏 spec 返回 1。"""
    import importlib.util
    fake = tmp_path / 'fake_hfmod.py'
    fake.write_text('\n'.join([
        'def snapshot_download(**kw):',
        '    open(kw["local_dir"] + "/config.yaml", "wb").write(b"0" * 10)',
    ]), encoding='utf-8')
    spec_m = importlib.util.spec_from_file_location('huggingface_hub', str(fake))
    mod = importlib.util.module_from_spec(spec_m)
    spec_m.loader.exec_module(mod)
    monkeypatch.setitem(sys.modules, 'huggingface_hub', mod)
    dst = tmp_path / 'dst'
    dst.mkdir()
    rc = M.dl_worker_main(json.dumps({'repo': 'a/b', 'dst': str(dst)}))
    assert rc == 0 and (dst / 'config.yaml').is_file()
    rc2 = M.dl_worker_main('not-json')
    assert rc2 == 1


def test_handler_routes_registered():
    with open(os.path.join(ROOT, 'handler.py'), encoding='utf-8') as f:
        hd = f.read()
    assert "'/api/tts2/model/download': '_post_tts2_model_download'" in hd
    assert "'/api/tts2/model/cancel': '_post_tts2_model_cancel'" in hd
    assert "'/api/tts2/model/status': '_get_tts2_model_status'" in hd
    assert "__import__('tts2_models').download_async" in hd


def test_desktop_bootstrap_guard():
    """打包 EXE 的下载子进程守卫必须在 main() 之前（不开窗口不起服务）。"""
    with open(os.path.join(ROOT, 'desktop.py'), encoding='utf-8') as f:
        src = f.read()
    guard_at = src.index("--tts2-dl-worker")
    main_at = src.index('if __name__')
    assert guard_at > main_at, '守卫必须在 __main__ 里'
    tail = src[main_at:]
    assert tail.index('sys.exit') < tail.index('\n    main()'), '守卫要在 main() 前'


_FRONT_HARNESS = r'''
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
function toast(){}
let fetchCalls = [], statusPayloads = [];
function fetch(url, opt){
  fetchCalls.push({url, body: (opt && opt.body) || ""});
  let payload = {};
  if(url === "/api/tts2/model/status"){ payload = statusPayloads.shift() || {}; }
  else if(url === "/api/tts2/model/download"){ payload = {ok: true, message: "开始下载"}; }
  return Promise.resolve({json: async () => payload});
}
const src = [
  "let _tts2DlHandle = null;",
  blockOf("function _pollTts2Model"),
  blockOf("function _renderTts2ModelCards"),
  blockOf("$('secTts2Models').addEventListener"),
  "\nstate.render = _renderTts2ModelCards;"
].join("\n");
const state = {};
new Function("$", "_esc", "toast", "fetch", "pollRun", "fillModelCenter",
  "fillTtsSettings", "els", "fetchCalls", "state", src)(
  $, _esc, toast, fetch, () => ({stop(){}}), () => {}, () => {},
  els, fetchCalls, state);
const out = {};
const mk = (ready, weights) => ({running: false, engine: "", ok: null, pct: 0, msg: "",
  engines: {qwen3tts: {label: "Q", size_hint: "5.3 GB", ready: true, weights: true},
            indextts2: {label: "I", size_hint: "6.8 GB", ready, weights}}});
/* ① 未下载态 */
state.render(mk(false, false));
out.idle = {badge: els.tts2BadgeIdx.textContent, badgeCls: els.tts2BadgeIdx.className,
            btn: els.btnTts2DlIdx.textContent, btnDisabled: els.btnTts2DlIdx.disabled};
/* ② 下载中态：同卡=取消、另一卡禁用、进度条出现 */
const runState = mk(false, false);
runState.running = true; runState.engine = "indextts2";
runState.pct = 42; runState.msg = "已下载 2800 MB / 约 6800 MB（30.0 MB/s）";
state.render(runState);
out.running = {badge: els.tts2BadgeIdx.textContent, badgeCls: els.tts2BadgeIdx.className,
               btn: els.btnTts2DlIdx.textContent, otherDisabled: els.btnTts2DlQwen.disabled};
out.barShown = els.tts2PullWrap.style.display !== "none";
out.barMeta = els.tts2PullMeta.textContent;
/* ③ 已就绪态 */
state.render(mk(true, true));
out.ready = {badge: els.tts2BadgeIdx.textContent, badgeCls: els.tts2BadgeIdx.className,
             btnDisabled: els.btnTts2DlIdx.disabled};
/* ④ 点击下载：请求体带 engine */
statusPayloads.unshift(mk(false, false));
await els.secTts2Models.listeners.click({target: {closest: c => c === ".mini-btn" ? els.btnTts2DlIdx : null}});
const dl = fetchCalls.find(c => c.url === "/api/tts2/model/download");
out.clickBody = dl ? dl.body : "";
/* ⑤ voice 页引导按钮存在（renderTtsInstalls 真跑） */
const rSrc = blockOf("function renderTtsInstalls");
const box = $("ttsInstalls");
box.innerHTML = "";
new Function("$", "_esc", "_vramRow",
  rSrc + "\nrenderTtsInstalls({tts2: {qwen3tts: false, indextts2: false, dir: 'X', vram: {}, ollama: []}});")(
  $, _esc, () => "");
out.guideBtn = (box.innerHTML.indexOf("data-go-tts2") >= 0);
console.log(JSON.stringify(out));
};
main();
'''


def test_frontend_model_cards_actually_run():
    """node 真跑：卡片渲染三态（未下载/下载中/已就绪）+ 进度条 + 点击请求体 + 引导按钮。"""
    env = dict(os.environ)
    env["FC_JS"] = JS
    p = subprocess.run(["node", "-"], input=_FRONT_HARNESS, capture_output=True,
                       text=True, encoding="utf-8", env=env, timeout=60, cwd=ROOT)
    assert p.returncode == 0, "harness 失败 rc=%s err=%s" % (
        p.returncode, (p.stderr or "")[-900:])
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out["idle"]["badgeCls"].endswith("bad") and "未下载" in out["idle"]["badge"]
    assert out["idle"]["btn"].startswith("⬇")
    assert out["running"]["badgeCls"].endswith("wait") and "下载中" in out["running"]["badge"]
    assert out["running"]["btn"].startswith("⏹"), '下载中同卡按钮必须是取消'
    assert out["running"]["otherDisabled"] is True, '下载中另一张卡必须禁用'
    assert out["ready"]["badgeCls"].endswith("ok") and out["ready"]["btnDisabled"]
    assert out["barShown"] and "MB" in out["barMeta"]
    assert '"engine":"indextts2"' in out["clickBody"].replace(" ", "")
    assert out["guideBtn"] is True
