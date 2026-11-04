# -*- coding: utf-8 -*-
"""离线配音「运行包补装」回归（task：模型齐了却永远用不了自然音）。

实测根因：models/tts/vits-melo-tts-zh_en/ 里 model.onnx + tokens.txt 都在，
_sherpa_ready('melo-zh') = True，但 sherpa_onnx 运行包没装 → sherpa_tts_available() = False。
旧 tts_model_download_async 在模型 ready 时直接返回「无需重复下载」，
用户点多少次都补不上运行包，edge-tts 一失败就静默掉到 SAPI 机械音。

本文件不联网、不装真包，用 monkeypatch 锁住三条契约：
  1. 模型齐 + 包已装 → 直接返回，不触发任何下载/安装线程；
  2. 模型齐 + 包缺失 → 只补装运行包，绝不调用 urlopen 重下上百 MB 模型；
  3. 装包失败 → TTS_SETUP.ok=False 且 running 复位（前端不永久转圈）。
"""
import importlib.machinery
import importlib.util
import time
import urllib.request

import pytest

import webui_server as w


def _wait_done(timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not w.TTS_SETUP['running']:
            return True
        time.sleep(0.05)
    return False


@pytest.fixture(autouse=True)
def _reset_setup():
    """每个用例前后清干净全局安装状态，避免用例间串味。"""
    w.TTS_SETUP.update(running=False, ok=None, pct=0, msg='', op='')
    yield
    w.TTS_SETUP.update(running=False, ok=None, pct=0, msg='', op='')


def _patch_model_ready(monkeypatch, ready=True):
    monkeypatch.setattr(w, '_sherpa_ready', lambda key: ready)


def _patch_find_spec(monkeypatch, installed):
    _real = importlib.util.find_spec

    def _fake(name, *a, **k):
        if name == 'sherpa_onnx':
            return importlib.machinery.ModuleSpec(name, None) if installed else None
        return _real(name, *a, **k)

    monkeypatch.setattr(importlib.util, 'find_spec', _fake)


def test_model_ready_and_runtime_installed_returns_early(monkeypatch):
    """模型齐 + 运行包已装：直接返回，不起线程、不下载、不装包。"""
    _patch_model_ready(monkeypatch, True)
    _patch_find_spec(monkeypatch, True)
    called = []
    monkeypatch.setattr(w, '_run_pip',
                        lambda *a, **k: called.append('pip') or (0, ''))
    monkeypatch.setattr(urllib.request, 'urlopen',
                        lambda *a, **k: called.append('urlopen') or None)

    ok, msg = w.tts_model_download_async('melo-zh')
    assert ok is True
    assert '无需重复下载' in msg
    assert called == [], '不该触发任何安装或下载'


def test_model_ready_but_runtime_missing_only_installs_runtime(monkeypatch):
    """模型齐 + 运行包缺失：只补装包，不重下模型（关键回归）。"""
    _patch_model_ready(monkeypatch, True)
    _patch_find_spec(monkeypatch, False)
    calls = []
    monkeypatch.setattr(w, '_run_pip',
                        lambda *a, **k: calls.append(('pip', a, k)) or (0, ''))
    monkeypatch.setattr(urllib.request, 'urlopen',
                        lambda *a, **k: calls.append(('urlopen', a, k)) or None)

    ok, msg = w.tts_model_download_async('melo-zh')
    assert ok is True, msg
    assert '运行包' in msg
    assert _wait_done(), '补装线程未在超时内结束'
    assert w.TTS_SETUP['ok'] is True, w.TTS_SETUP.get('msg')
    assert any(c[0] == 'pip' for c in calls), '未触发运行包安装'
    assert not any(c[0] == 'urlopen' for c in calls), '不该重下上百 MB 的模型'
    # 装的是 sherpa-onnx，且走国内镜像
    pip_args = next(c[1] for c in calls if c[0] == 'pip')
    assert 'sherpa-onnx' in pip_args[1], pip_args


def test_runtime_install_failure_resets_running(monkeypatch):
    """装包失败：明确失败提示，且 running 复位（前端不永久转圈）。"""
    _patch_model_ready(monkeypatch, True)
    _patch_find_spec(monkeypatch, False)
    monkeypatch.setattr(w, '_run_pip', lambda *a, **k: (1, '模拟网络失败'))

    ok, msg = w.tts_model_download_async('melo-zh')
    assert ok is True, msg
    assert _wait_done(), 'running 未复位，前端会永久转圈'
    assert w.TTS_SETUP['ok'] is False
    assert '失败' in (w.TTS_SETUP.get('msg') or '')
