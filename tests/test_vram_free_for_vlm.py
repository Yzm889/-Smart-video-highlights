# -*- coding: utf-8 -*-
"""free_vram_for_vlm：画面理解前腾显存的正确姿势。

背景（2026-11-10 主人实测）：「每次跑都给内存显存占满、效率更慢、电脑容易卡，
就 16G 运行内存，VLM 推理到一点点就会卡住」。

实测证据（同一台机器）：
  qwen3-vl:8b  磁盘 5.38GB → 驻留 10.2GB（ctx 32768，KV 缓存吃掉 4.8GB）
  qwen3:8b     磁盘 4.68GB → 驻留  9.12GB
  nvidia-smi   11959 / 12288 MiB  ← 打满

上一版在 VLM 抽样前调的是 free_vram_for_llm，它**只停 TTS worker、完全不碰
Ollama**；而真正的对手是 Ollama 里常驻的写稿模型。于是显存一点没腾出来，
VLM 只剩约 2.6GB，七成层跑在 CPU/内存上 —— 就是「推理到一点点就卡住」。

本文件钉住三条：显存够时别乱动、不够时卸别的模型、绝不卸马上要用的那个。
"""
import pytest

import tts2_engines as T2


def _patch(monkeypatch, used, total, loaded, recorder):
    monkeypatch.setattr(T2, 'gpu_vram', lambda idx=0: (used, total))
    monkeypatch.setattr(T2, 'ollama_loaded', lambda base=None: list(loaded))

    def _stop():
        recorder.append('__release_engines__')
        return ['qwen3tts']

    def _http(base=None, path=None, payload=None, timeout=None):
        recorder.append((payload or {}).get('model'))
        return b'{}'

    monkeypatch.setattr(T2, 'release_engines', _stop)
    monkeypatch.setattr(T2, '_http', _http)


def test_enough_vram_does_nothing(monkeypatch):
    """显存本来就够 → 不许停 worker、不许卸模型（停了重载一次要几分钟）。"""
    rec = []
    _patch(monkeypatch, used=2000, total=12288, loaded=['qwen3:8b'], recorder=rec)
    assert T2.free_vram_for_vlm('qwen3-vl:8b') == []
    assert rec == [], '显存够的时候乱动会把「更慢」做实'


def test_not_enough_vram_unloads_other_ollama_models(monkeypatch):
    """不够时：停 worker + 把 Ollama 里的**别人**请出去。"""
    rec = []
    _patch(monkeypatch, used=11000, total=12288,
           loaded=['qwen3:8b', 'nomic-embed-text:latest'], recorder=rec)
    freed = T2.free_vram_for_vlm('qwen3-vl:8b', wait_sec=0)
    assert '__release_engines__' in rec, '必须停掉常驻配音 worker'
    assert 'qwen3:8b' in freed, '写稿模型占 9.12GB，不卸它 VLM 挤不进来'
    assert 'nomic-embed-text:latest' in freed


def test_vlm_already_resident_is_left_completely_alone(monkeypatch):
    """VLM 已经在显存里 → 连 worker 都不许停。

    它既然加载成功了，就说明占得住；这时再腾位只是白白付出重载代价。"""
    rec = []
    _patch(monkeypatch, used=11000, total=12288,
           loaded=['qwen3:8b', 'qwen3-vl:8b'], recorder=rec)
    assert T2.free_vram_for_vlm('qwen3-vl:8b') == []
    assert rec == [], 'VLM 已在显存，不该动任何东西'


def test_keep_matches_without_tag(monkeypatch):
    """配置写 qwen3-vl、Ollama 里叫 qwen3-vl:8b，得认出来是同一个人。"""
    rec = []
    _patch(monkeypatch, used=11000, total=12288,
           loaded=['qwen3-vl:8b'], recorder=rec)
    assert T2.free_vram_for_vlm('qwen3-vl') == []
    assert rec == []


def test_different_tag_is_not_the_same_model(monkeypatch):
    """qwen3-vl:4b ≠ qwen3-vl:8b：该卸的必须卸，别被宽松匹配蒙住。"""
    rec = []
    _patch(monkeypatch, used=11000, total=12288,
           loaded=['qwen3-vl:4b'], recorder=rec)
    freed = T2.free_vram_for_vlm('qwen3-vl:8b', wait_sec=0)
    assert 'qwen3-vl:4b' in freed


def test_no_gpu_info_does_not_block(monkeypatch):
    """拿不到显存信息（没装驱动/非 N 卡）时直接放行，不能卡死主流程。"""
    rec = []
    _patch(monkeypatch, used=0, total=0, loaded=['qwen3:8b'], recorder=rec)
    assert T2.free_vram_for_vlm('qwen3-vl:8b') == []
    assert rec == []


def test_threshold_scales_down_on_small_cards(monkeypatch):
    """8GB 卡上 6500 的硬阈值会比「可用显存」还高，永远判不够 → 按六成缩。"""
    rec = []
    # 8GB 卡：need = 8192*0.6 = 4915；空闲 5200 已经够了
    _patch(monkeypatch, used=2992, total=8192, loaded=['qwen3:8b'], recorder=rec)
    assert T2.free_vram_for_vlm('qwen3-vl:8b') == []
    assert rec == []
