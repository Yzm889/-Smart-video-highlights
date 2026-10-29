# -*- coding: utf-8 -*-
"""离线配音模型「下载→解压→就绪判定」回归（task：TTS 离线模型下载链路）。

为什么需要它：用户实测卡在「点下载后一直转、没提示、疑似卡死」。
根因是 GitHub 真实文件在 release-assets.githubusercontent.com（国内常被墙），
旧 urlretrieve 无超时、无回退、无进度，看起来像死循环。
本文件不依赖真实网络，用合成 tar.bz2 + 模拟 urlopen 锁定：

  1. 主源可达 → 能正确解压并在 models/tts/<name>/ 产出 onnx+tokens.txt，_sherpa_ready 判为就绪；
  2. 主源不可达 → 必须自动回退到 gh-proxy 镜像并成功（验证 9e72f46 的回退逻辑不被改坏）；
  3. 解压后不残留 .part 临时文件。

真机下载请跑 test_http_smoke / 真机手动验证；本文件只守「代码逻辑」不被回归。
"""
import importlib.machinery
import io
import tarfile
import time
import urllib.error
import urllib.request

import pytest

import webui_server as w
import tts_engines as T


# 与 SHERPA_TTS_MODELS['melo-zh']['name'] 一致；含 model.onnx + tokens.txt 即满足 _sherpa_ready
_MODEL_NAME = 'vits-melo-tts-zh_en'


def _make_synthetic_bz2():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:bz2') as tf:
        onnx = b'FAKE_ONNX_WEIGHTS_FOR_TEST'
        ti = tarfile.TarInfo(name=_MODEL_NAME + '/model.onnx')
        ti.size = len(onnx)
        tf.addfile(ti, io.BytesIO(onnx))
        tok = b'TOKEN_DATA_FOR_TEST'
        ti2 = tarfile.TarInfo(name=_MODEL_NAME + '/tokens.txt')
        ti2.size = len(tok)
        tf.addfile(ti2, io.BytesIO(tok))
    return buf.getvalue()


_SYNTHETIC = _make_synthetic_bz2()


class _FakeResp:
    def __init__(self, data, total):
        self._data = data
        self._pos = 0
        self.headers = {'Content-Length': str(total)}

    def read(self, n=-1):
        if n == -1:
            chunk = self._data[self._pos:]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk


def _wait_done(timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not w.TTS_SETUP['running']:
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def patched_env(monkeypatch, tmp_path):
    monkeypatch.setattr(w, 'tts_models_dir', lambda: str(tmp_path))
    monkeypatch.setattr(T, 'tts_models_dir', lambda: str(tmp_path))
    # 让下载函数在测试里不要真的去 pip 安装 sherpa-onnx（环境是否已装无所谓，跳过即可）
    import importlib.util as _iu
    _real = _iu.find_spec
    def _fake_find(name, *a, **k):
        if name == 'sherpa_onnx':
            return importlib.machinery.ModuleSpec(name, None)
        return _real(name, *a, **k)
    monkeypatch.setattr(_iu, 'find_spec', _fake_find)
    yield tmp_path


def test_download_extract_and_ready(patched_env, monkeypatch):
    """主源可达：解压后在 <name>/ 产出 onnx+tokens.txt，_sherpa_ready 判为就绪。"""
    def _ok_urlopen(req, timeout=30):
        return _FakeResp(_SYNTHETIC, len(_SYNTHETIC))
    monkeypatch.setattr(urllib.request, 'urlopen', _ok_urlopen)

    ok, msg = w.tts_model_download_async('melo-zh')
    assert ok is True, msg
    assert _wait_done(), '下载线程未在超时内结束'
    assert w.TTS_SETUP['ok'] is True, w.TTS_SETUP.get('msg')
    assert w._sherpa_ready('melo-zh') is True, '解压后未判定为就绪'
    # 不应残留临时文件
    leftovers = [f for f in patched_env.iterdir() if f.name.endswith('.part')]
    assert not leftovers, '残留 .part 临时文件：%s' % leftovers


def test_download_falls_back_to_mirror_on_primary_failure(patched_env, monkeypatch):
    """主源不可达 → 自动回退 gh-proxy 镜像并成功（守 9e72f46 回退逻辑）。"""
    def _flaky_urlopen(req, timeout=30):
        url = getattr(req, 'full_url', str(req))
        if 'gh-proxy.com' in url:
            return _FakeResp(_SYNTHETIC, len(_SYNTHETIC))
        raise urllib.error.URLError('模拟主源（github）不可达')
    monkeypatch.setattr(urllib.request, 'urlopen', _flaky_urlopen)

    ok, msg = w.tts_model_download_async('melo-zh')
    assert ok is True, msg
    assert _wait_done(), '下载线程未在超时内结束'
    assert w.TTS_SETUP['ok'] is True, w.TTS_SETUP.get('msg')
    assert w._sherpa_ready('melo-zh') is True
