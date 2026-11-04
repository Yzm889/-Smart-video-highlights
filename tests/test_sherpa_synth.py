# -*- coding: utf-8 -*-
"""离线配音（sherpa-onnx）合成路径接线回归（task：让本地 TTS 真正可用）。

为什么需要它：用户下完离线模型却「判不可用 / 合成失败」是高概率坑。
真机合成需装 sherpa_onnx 重依赖 + 130MB 模型，CI/沙箱跑不了；本文件用 Fake 模块
锁住「模型就绪 → _sherpa_load 解析 onnx/ tokens/ lexicon/ data_dir → sherpa_tts_speak
写出 wav」这条代码链路不被改坏，且不依赖真实模型与运行包。

守什么：
  1. _sherpa_load 能正确解析权重文件（标称 model.onnx 缺失时回退目录里第一个 .onnx）；
  2. sherpa_tts_speak 在合成成功时写出 >1000 字节的 wav 并返回 True；
  3. sherpa_tts_speak 在 generate 返回空样本时优雅返回 False（不抛、不假成功）。
"""
import os
import sys

import pytest

import tts_engines as T


# ---- Fake sherpa_onnx：只接住真实代码构造的配置参数，generate 返回可写 wav 的样本 ----
class _Samples:
    def __init__(self, n, sr):
        self.samples = [0.0] * n
        self.sample_rate = sr


class _OfflineTts:
    def __init__(self, config):
        self.config = config

    def generate(self, text, speed=1.0):
        if text.strip() == 'FAIL':
            return None
        return _Samples(8000, 22050)


class _OfflineTtsVitsModelConfig:
    def __init__(self, *a, **k):
        self.__dict__.update(k)


class _OfflineTtsModelConfig:
    def __init__(self, *a, **k):
        self.__dict__.update(k)


class _OfflineTtsConfig:
    def __init__(self, *a, **k):
        self.__dict__.update(k)


class _FakeSherpa:
    OfflineTts = _OfflineTts
    OfflineTtsConfig = _OfflineTtsConfig
    OfflineTtsModelConfig = _OfflineTtsModelConfig
    OfflineTtsVitsModelConfig = _OfflineTtsVitsModelConfig


# ---- Fake soundfile：write 写出一个 >1000 字节的文件即满足校验 ----
class _FakeSoundfile:
    @staticmethod
    def write(path, data, sr):
        with open(path, 'wb') as f:
            f.write(b'\0' * 2000)


def _fake_sherpa_in_sys_modules(monkeypatch):
    monkeypatch.setitem(sys.modules, 'sherpa_onnx', _FakeSherpa())
    monkeypatch.setitem(sys.modules, 'soundfile', _FakeSoundfile())


def _make_model_dir(base, model_filename='model.onnx'):
    d = base / 'vits-melo-tts-zh_en'
    d.mkdir(parents=True, exist_ok=True)
    (d / model_filename).write_bytes(b'FAKE_ONNX')
    (d / 'tokens.txt').write_bytes(b'TOKEN')
    (d / 'lexicon.txt').write_bytes(b'LEX')
    (d / 'espeak-ng-data').mkdir(exist_ok=True)
    return d


@pytest.fixture
def sherpa_ready_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(T, 'tts_models_dir', lambda: str(tmp_path / 'tts'))
    # 重置离线引擎缓存，避免跨测试命中旧实例
    monkeypatch.setattr(T, '_SHERPA_TTS', {'obj': None, 'key': None})
    _make_model_dir(tmp_path / 'tts')
    _fake_sherpa_in_sys_modules(monkeypatch)
    yield tmp_path


def test_sherpa_load_resolves_onnx_path(sherpa_ready_dir):
    """_sherpa_load 应解析出目录里存在的权重文件（默认 model.onnx）。"""
    tts = T._sherpa_load()
    assert isinstance(tts, _OfflineTts)
    model_path = tts.config.model.vits.model
    assert model_path.endswith('model.onnx'), model_path
    assert tts.config.model.vits.tokens.endswith('tokens.txt')


def test_sherpa_load_falls_back_to_first_onnx(sherpa_ready_dir, tmp_path):
    """标称 model.onnx 缺失时，应回退用目录里第一个 .onnx（真实发行包文件名不一致场景）。"""
    d = tmp_path / 'tts' / 'vits-melo-tts-zh_en'
    # 删掉 model.onnx，放一个命名不同的权重
    (d / 'model.onnx').unlink()
    (d / 'vits-melo-tts-zh_en.onnx').write_bytes(b'FAKE_ONNX2')
    tts = T._sherpa_load()
    assert tts.config.model.vits.model.endswith('vits-melo-tts-zh_en.onnx')


def test_sherpa_speak_writes_wav(sherpa_ready_dir, tmp_path):
    """合成成功 → 写出 >1000 字节 wav 并返回 True。"""
    out = str(tmp_path / 'out.wav')
    ok = T.sherpa_tts_speak('你好，世界', out)
    assert ok is True, '合成应成功'
    assert os.path.exists(out) and os.path.getsize(out) > 1000


def test_sherpa_speak_returns_false_on_empty_samples(sherpa_ready_dir, tmp_path):
    """generate 返回空样本 → 优雅返回 False（不抛、不假成功）。"""
    out = str(tmp_path / 'out2.wav')
    ok = T.sherpa_tts_speak('FAIL', out)
    assert ok is False
    assert not (os.path.exists(out) and os.path.getsize(out) > 1000)
