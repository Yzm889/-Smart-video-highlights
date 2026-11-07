# -*- coding: utf-8 -*-
"""Whisper 模型「已下载」判定回归钉（v1.4.8）。

真缺陷：`refresh_whisper_models()` 只认扁平布局（目录里直接有 model.bin），
但 faster-whisper 走 huggingface_hub 时落的是 HF 缓存布局：

    models/whisper/models--Systran--faster-distil-whisper-large-v3/snapshots/<sha>/model.bin

于是模型明明有 1.5G，`/api/whisper/status` 却全表 `downloaded: false`，
前端下拉全显示「（未下载）」，主人会以为没下过、又去点下载重下 1.5G。
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import ai_providers as A  # noqa: E402


def _mk(path, files=('model.bin',), size=1024):
    os.makedirs(path, exist_ok=True)
    for f in files:
        with open(os.path.join(path, f), 'wb') as fh:
            fh.write(b'x' * size)


def test_hf_cache_layout_detected(tmp_path, monkeypatch):
    md = tmp_path / 'whisper'
    md.mkdir()
    snap = md / 'models--Systran--faster-distil-whisper-large-v3' / 'snapshots' / 'c3058b4'
    _mk(str(snap))
    monkeypatch.setattr(A, 'whisper_models_dir', lambda: str(md))
    A.refresh_whisper_models()
    assert A._WHISPER_MODELS['distil-large-v3']['downloaded'] is True, \
        'HF 缓存布局没被识别：%r' % A._WHISPER_MODELS['distil-large-v3']
    assert A._WHISPER_MODELS['large-v3']['downloaded'] is False, '别的模型不该被连带标记'


def test_flat_layout_still_detected(tmp_path, monkeypatch):
    md = tmp_path / 'whisper'
    _mk(str(md / 'medium'))
    monkeypatch.setattr(A, 'whisper_models_dir', lambda: str(md))
    A.refresh_whisper_models()
    assert A._WHISPER_MODELS['medium']['downloaded'] is True


def test_incomplete_snapshot_not_counted(tmp_path, monkeypatch):
    """只有 config.json、没有 model.bin 的快照 = 下了一半，不能算已下载。"""
    md = tmp_path / 'whisper'
    snap = md / 'models--Systran--faster-distil-whisper-large-v3' / 'snapshots' / 'abc'
    os.makedirs(str(snap), exist_ok=True)
    with open(os.path.join(str(snap), 'config.json'), 'w') as fh:
        fh.write('{}')
    monkeypatch.setattr(A, 'whisper_models_dir', lambda: str(md))
    A.refresh_whisper_models()
    assert A._WHISPER_MODELS['distil-large-v3']['downloaded'] is False


def test_repo_name_mapping():
    assert A._whisper_name_from_repo('faster-distil-whisper-large-v3') == 'distil-large-v3'
    assert A._whisper_name_from_repo('faster-whisper-large-v3') == 'large-v3'
    assert A._whisper_name_from_repo('whisper-medium') == 'medium'
    assert A._whisper_name_from_repo('distil-whisper-large-v3') == 'distil-large-v3'
    assert A._whisper_name_from_repo('faster-whisper-tiny') == 'tiny'
