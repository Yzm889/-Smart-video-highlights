# -*- coding: utf-8 -*-
"""v1.4.79 配音运行环境一键安装回归钉。

v1.4.78 只覆盖权重（用户数据目录里的手工脚本收编）；运行环境（venv + PyTorch）
还得自己装，卡片只能显示「环境未就绪」。本轮把 Qwen3-TTS 这条复刻
cosyvoice_install_async 真机验证过的路径收进界面：找 Python 3.11/3.12 →
venv → torch cu121（国内镜像/官方源/CPU 版三级兜底）→ 依赖钉版 → 导入自检。

钉住的契约：
  · 环境就绪判据 = venv 解释器 + site-packages 同时有 torch 与 qwen_tts
  · 权重缺时一键安装先下载再装环境（顺序不可颠倒）
  · 取消报「已取消」绝不报成「安装失败」（否则用户以为装坏了）
  · torch 已在则跳过 2.4GB 下载（幂等，中断重跑不吃第二次）
  · IndexTTS-2 环境不收编，明确拒绝并给手动指引
  · 端点注册 + 前端四态按钮（一键安装 / 安装运行环境 / 手动指引禁用 / 取消）
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


def _wait_done(timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline and M._STATE['running']:
        time.sleep(0.2)
    assert M._STATE['running'] is False, '任务未在 %ds 内结束' % timeout


# ---------------------------------------------------------------------------
# 环境就绪判据 / 状态暴露
# ---------------------------------------------------------------------------

def test_qwen_env_ready_checks_interpreter_and_packages(monkeypatch, tmp_path):
    """环境就绪 = venv 解释器 + site-packages 里有 torch 和 qwen_tts。"""
    monkeypatch.setattr(T2, 'TTS2_DIR', str(tmp_path))
    monkeypatch.setattr(T2, 'QWEN_PY',
                        str(tmp_path / 'venv' / 'Scripts' / 'python.exe'))
    sp = tmp_path / 'venv' / 'Lib' / 'site-packages'
    assert M.qwen_env_ready() is False                    # 什么都没有
    (tmp_path / 'venv' / 'Scripts').mkdir(parents=True)
    (tmp_path / 'venv' / 'Scripts' / 'python.exe').write_bytes(b'x')
    assert M.qwen_env_ready() is False                    # 只有解释器
    sp.mkdir(parents=True)
    (sp / 'torch').mkdir()
    assert M.qwen_env_ready() is False                    # 缺 qwen_tts
    (sp / 'qwen_tts').mkdir()
    assert M.qwen_env_ready() is True


def test_status_exposes_env_flags():
    st = M.status()
    for eng in ('qwen3tts', 'indextts2'):
        assert 'env_ready' in st['engines'][eng]
        assert 'env_installable' in st['engines'][eng]
    assert st['engines']['qwen3tts']['env_installable'] is True
    assert st['engines']['indextts2']['env_installable'] is False, \
        'IndexTTS-2 环境不收编（依赖面太重），必须如实标 false'


def test_index_env_ready_reflects_real_venv(monkeypatch, tmp_path):
    """IndexTTS-2 环境不收编一键装，但状态必须如实——不许一律标未就绪。"""
    repo = tmp_path / 'repo'
    monkeypatch.setattr(T2, 'IDX_REPO', str(repo))
    monkeypatch.setattr(T2, 'IDX_PY', str(repo / '.venv' / 'Scripts' / 'python.exe'))
    assert M.index_env_ready() is False
    (repo / '.venv' / 'Scripts').mkdir(parents=True)
    (repo / '.venv' / 'Scripts' / 'python.exe').write_bytes(b'x')
    sp = repo / '.venv' / 'Lib' / 'site-packages'
    sp.mkdir(parents=True)
    assert M.index_env_ready() is False          # 缺 torch
    (sp / 'torch').mkdir()
    assert M.index_env_ready() is True


# ---------------------------------------------------------------------------
# 启动守卫
# ---------------------------------------------------------------------------

def test_install_async_rejects_index_and_unknown():
    ok, msg = M.install_async('indextts2')
    assert ok is False and '不支持一键安装' in msg
    ok2, msg2 = M.install_async('bogus')
    assert ok2 is False and '未知引擎' in msg2


def test_install_async_guards_concurrency():
    M._STATE.update(running=True, engine='qwen3tts')
    ok, msg = M.install_async('qwen3tts')
    assert ok is False and '已有任务' in msg


# ---------------------------------------------------------------------------
# 整链
# ---------------------------------------------------------------------------

def _stub_venv_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(T2, 'TTS2_DIR', str(tmp_path / 'tts2'))
    monkeypatch.setattr(T2, 'QWEN_PY',
                        str(tmp_path / 'tts2' / 'venv' / 'Scripts' / 'python.exe'))
    monkeypatch.setattr(M, '_find_py311', lambda: sys.executable)


def test_install_full_chain_success(monkeypatch, tmp_path):
    """真跑整链（权重已就绪 → 装环境）：假 _run_step 模拟各步骤成功。"""
    _stub_venv_paths(monkeypatch, tmp_path)
    monkeypatch.setitem(M.CATALOG['qwen3tts'], 'weights_ok', lambda: True)
    steps = []

    def fake_step(cmd, pct0, pct1, est, label, timeout):
        steps.append(label)
        if cmd[1:3] == ['-m', 'venv']:          # 模拟 venv 创建造出解释器
            s = tmp_path / 'tts2' / 'venv' / 'Scripts'
            s.mkdir(parents=True, exist_ok=True)
            (s / 'python.exe').write_bytes(b'x')
        return 0, ''
    monkeypatch.setattr(M, '_run_step', fake_step)
    monkeypatch.setattr(M, '_probe',
                        lambda py, code, timeout=60: 'ok')   # torch 探测 + 导入自检都成功
    ok, msg = M.install_async('qwen3tts')
    assert ok is True, msg
    _wait_done()
    assert M._STATE['ok'] is True, M._STATE['msg']
    assert '安装完成' in M._STATE['msg']
    assert M._STATE['op'] == 'install'
    assert steps, '安装步骤必须真跑过'


def test_install_downloads_weights_first_when_missing(monkeypatch, tmp_path):
    """权重缺 → 先下载再装环境（一键安装的意义就在这里，顺序不可颠倒）。"""
    _stub_venv_paths(monkeypatch, tmp_path)
    monkeypatch.setitem(M.CATALOG['qwen3tts'], 'weights_ok', lambda: False)
    order = []
    monkeypatch.setattr(M, '_download_phase',
                        lambda eng: (order.append('download'), (True, '下载完成'))[1])
    monkeypatch.setattr(M, '_install_qwen_env',
                        lambda: (order.append('env'), (True, '环境完成'))[1])
    ok, _msg = M.install_async('qwen3tts')
    assert ok is True
    _wait_done()
    assert order == ['download', 'env'], '必须先下载权重再装环境'
    assert M._STATE['ok'] is True


def test_install_stops_when_weight_download_fails(monkeypatch, tmp_path):
    """权重下载失败 → 不进入环境安装（不白等一轮 pip）。"""
    _stub_venv_paths(monkeypatch, tmp_path)
    monkeypatch.setitem(M.CATALOG['qwen3tts'], 'weights_ok', lambda: False)
    called = []
    monkeypatch.setattr(M, '_download_phase', lambda eng: (False, '下载失败：断网'))
    monkeypatch.setattr(M, '_install_qwen_env',
                        lambda: (called.append(1), (True, ''))[1])
    ok, _msg = M.install_async('qwen3tts')
    assert ok is True
    _wait_done()
    assert M._STATE['ok'] is False and '下载失败' in M._STATE['msg']
    assert called == [], '下载失败就不该再装环境'


def test_install_cancel_not_reported_as_failure(monkeypatch, tmp_path):
    """取消必须报「已取消」，绝不能报成「安装失败」。"""
    _stub_venv_paths(monkeypatch, tmp_path)
    monkeypatch.setitem(M.CATALOG['qwen3tts'], 'weights_ok', lambda: True)
    # 已存在一个解释器，跳过 venv 创建（本用例专测 pip 步骤取消）
    s = tmp_path / 'tts2' / 'venv' / 'Scripts'
    s.mkdir(parents=True, exist_ok=True)
    (s / 'python.exe').write_bytes(b'x')

    def fake_step(cmd, pct0, pct1, est, label, timeout):
        with M._LOCK:
            M._STATE['cancel'] = True
        return None, '已取消'
    monkeypatch.setattr(M, '_run_step', fake_step)
    monkeypatch.setattr(M, '_probe', lambda py, code, timeout=60: '')
    ok, _msg = M.install_async('qwen3tts')
    assert ok is True
    _wait_done()
    assert M._STATE['ok'] is False
    assert '已取消' in M._STATE['msg'], M._STATE['msg']
    assert '失败' not in M._STATE['msg'], '取消不许写成失败：%s' % M._STATE['msg']


def test_install_cancel_during_venv_not_reported_as_failure(monkeypatch, tmp_path):
    """venv 创建阶段取消也不能报成「venv 创建失败」（同类漏网点）。"""
    _stub_venv_paths(monkeypatch, tmp_path)
    monkeypatch.setitem(M.CATALOG['qwen3tts'], 'weights_ok', lambda: True)

    def fake_step(cmd, pct0, pct1, est, label, timeout):
        if cmd[1:3] == ['-m', 'venv']:      # 创建 venv 那一步被取消
            with M._LOCK:
                M._STATE['cancel'] = True
            return None, '已取消'
        return 0, ''
    monkeypatch.setattr(M, '_run_step', fake_step)
    monkeypatch.setattr(M, '_probe', lambda py, code, timeout=60: '')
    M.install_async('qwen3tts')
    _wait_done()
    assert M._STATE['ok'] is False
    assert '已取消' in M._STATE['msg'] and '失败' not in M._STATE['msg'], \
        'venv 阶段取消不许写成失败：%s' % M._STATE['msg']


def test_install_skips_torch_download_when_present(monkeypatch, tmp_path):
    """torch 已在 → 跳过 2.4GB 下载（幂等，重跑不吃第二次）。"""
    _stub_venv_paths(monkeypatch, tmp_path)
    monkeypatch.setitem(M.CATALOG['qwen3tts'], 'weights_ok', lambda: True)
    seen = []

    def fake_step(cmd, pct0, pct1, est, label, timeout):
        seen.append(label)
        if cmd[1:3] == ['-m', 'venv']:
            s = tmp_path / 'tts2' / 'venv' / 'Scripts'
            s.mkdir(parents=True, exist_ok=True)
            (s / 'python.exe').write_bytes(b'x')
        return 0, ''
    monkeypatch.setattr(M, '_run_step', fake_step)
    monkeypatch.setattr(M, '_probe',
                        lambda py, code, timeout=60:
                        '2.5.1+cu121' if 'torch' in code and 'import torch' in code else 'ok')
    ok, _msg = M.install_async('qwen3tts')
    assert ok is True
    _wait_done()
    assert not any('下载 PyTorch' in s for s in seen), 'torch 已在却仍去下载：%s' % seen
    assert any('依赖' in s for s in seen), '其余依赖仍要装'


def test_install_no_py311_fails_with_guidance(monkeypatch, tmp_path):
    """没有 3.11/3.12 → 明确报错并给安装指引，不许静默往下走。"""
    monkeypatch.setattr(T2, 'TTS2_DIR', str(tmp_path / 'tts2'))
    monkeypatch.setitem(M.CATALOG['qwen3tts'], 'weights_ok', lambda: True)
    monkeypatch.setattr(M, '_find_py311', lambda: None)
    ok, _msg = M.install_async('qwen3tts')
    assert ok is True
    _wait_done()
    assert M._STATE['ok'] is False and '3.11' in M._STATE['msg']


def test_import_selfcheck_failure_is_reported(monkeypatch, tmp_path):
    """导入自检失败 → 明确失败（pip 装完了但引擎 import 不动，不能算成功）。"""
    _stub_venv_paths(monkeypatch, tmp_path)
    monkeypatch.setitem(M.CATALOG['qwen3tts'], 'weights_ok', lambda: True)

    def fake_step(cmd, pct0, pct1, est, label, timeout):
        if cmd[1:3] == ['-m', 'venv']:
            s = tmp_path / 'tts2' / 'venv' / 'Scripts'
            s.mkdir(parents=True, exist_ok=True)
            (s / 'python.exe').write_bytes(b'x')
        return 0, ''
    monkeypatch.setattr(M, '_run_step', fake_step)
    # torch 探测成功（跳过下载），但导入自检（含 qwen_tts）返回空 = 失败
    monkeypatch.setattr(M, '_probe',
                        lambda py, code, timeout=60:
                        '2.5.1' if code.startswith('import torch; print') else '')
    ok, _msg = M.install_async('qwen3tts')
    assert ok is True
    _wait_done()
    assert M._STATE['ok'] is False and '自检失败' in M._STATE['msg']


# ---------------------------------------------------------------------------
# 端点与前端
# ---------------------------------------------------------------------------

def test_install_env_endpoint_registered():
    with open(os.path.join(ROOT, 'handler.py'), encoding='utf-8') as f:
        hd = f.read()
    assert "'/api/tts2/env/install': '_post_tts2_env_install'" in hd
    assert "__import__('tts2_models').install_async" in hd
    assert 'def _post_tts2_env_install' in hd


def test_frontend_install_button_states_actually_run():
    """node 真跑：四态按钮（一键安装 / 安装运行环境 / 手动禁用 / 取消）。"""
    env = dict(os.environ)
    env['FC_JS'] = JS
    p = subprocess.run(['node', '-'], input=_INSTALL_HARNESS, capture_output=True,
                       text=True, encoding='utf-8', env=env, timeout=60, cwd=ROOT)
    assert p.returncode == 0, 'harness rc=%s err=%s' % (
        p.returncode, (p.stderr or '')[-900:])
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert '一键安装' in out['idle']['btn'] and out['idle']['act'] == 'install'
    assert out['idle']['disabled'] is False
    assert '安装运行环境' in out['weights']['btn'] and out['weights']['act'] == 'install'
    assert '手动' in out['idxWeights']['btn'] and out['idxWeights']['disabled'] is True
    assert out['installing']['badge'] == '安装中'
    assert out['installing']['btn'].startswith('⏹')
    assert out['installing']['act'] == 'cancel'
    assert out['ready']['disabled'] is True


_INSTALL_HARNESS = r'''
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
function _esc(s){ return String(s || ""); }
function toast(){}
const state = {};
const src = [
  "let _tts2DlHandle = null;",
  "let _tts2TaskStart = Date.now();",
  "let _tts2Failed = null;",
  /* v1.4.81-ux：进度数字行抽成共用函数，harness 要把它们一起带进来 */
  blockOf("function _fmtDur"),
  blockOf("function _speedMB"),
  blockOf("function _fmtMB"),
  blockOf("function _progressMeta"),
  blockOf("function _pollTts2Model"),
  blockOf("function _renderTts2ModelCards"),
  "\nstate.render = _renderTts2ModelCards;"
].join("\n");
new Function("$", "_esc", "toast", "pollRun", "state", src)(
  $, _esc, toast, () => ({stop(){}}), state);
const out = {};
const mk = (over) => Object.assign({running:false, engine:"", op:"", ok:null, pct:0, msg:"",
  engines: {qwen3tts: {label:"Q", size_hint:"5.3 GB", ready:false, weights:false,
                       env_ready:false, env_installable:true},
            indextts2: {label:"I", size_hint:"6.8 GB", ready:false, weights:false,
                        env_ready:false, env_installable:false}}}, over);
state.render(mk({}));
out.idle = {btn: els.btnTts2DlQwen.textContent, act: els.btnTts2DlQwen.dataset.act,
            disabled: els.btnTts2DlQwen.disabled};
const w = mk({}); w.engines.qwen3tts.weights = true;
state.render(w);
out.weights = {btn: els.btnTts2DlQwen.textContent, act: els.btnTts2DlQwen.dataset.act};
const iw = mk({}); iw.engines.indextts2.weights = true;
state.render(iw);
out.idxWeights = {btn: els.btnTts2DlIdx.textContent, disabled: els.btnTts2DlIdx.disabled};
const run = mk({}); run.running = true; run.engine = "qwen3tts"; run.op = "install";
run.pct = 60;
state.render(run);
out.installing = {badge: els.tts2BadgeQwen.textContent, btn: els.btnTts2DlQwen.textContent,
                  act: els.btnTts2DlQwen.dataset.act};
const rd = mk({}); rd.engines.qwen3tts.ready = true;
state.render(rd);
out.ready = {disabled: els.btnTts2DlQwen.disabled};
console.log(JSON.stringify(out));
};
main();
'''
