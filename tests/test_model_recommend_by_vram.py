# -*- coding: utf-8 -*-
"""按显存分档的模型推荐回归（2026-10-26）。

背景（真机实测，RTX3060 **12GB** + 16GB 内存）：原实现给 12GB 卡的「甜点档」推荐
`qwen3:14b-q4_K_M`（权重 9.28GB）+ `minicpm-v4.5`（6.12GB）= **15.4GB > 12GB**，
两个大模型互相驱逐 → 每次重读 9.28GB 权重 → 磁盘 I/O 打满 + 内存页缓存抖动 →
跑 20 分钟视频时整机卡死（用户反馈「网卡都卡掉」）。且失败被 `except: pass` 吞掉，
用户只看到「卡」。

本文件锁定：
  ① 12GB 档**不得**推荐 14B（后端分档表 + 前端档位卡 + 引导文案三处都要查）；
  ② 16GB/24GB 档仍能拿到 14B/30B（不能矫枉过正把大卡也降级）；
  ③ 前端模型卡必须带 minVram 标注（放不下时要能标红提示）。
"""
import re

import pytest

import webui_server as S
import ai_providers as A


# ---------------------------------------------------------------------------
# 后端：按显存的文字模型推荐
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('vram,expect_not', [
    (12, '14b'),   # 12GB 卡：绝不能推 14B
    (10, '14b'),
    (8, '14b'),
    (6, '14b'),
])
def test_low_vram_never_recommends_14b(vram, expect_not):
    tag, note = A.recommend_text_model(vram, with_vlm=True)
    assert expect_not not in tag, '%.0fGB 卡不应推荐 %s（%s）' % (vram, tag, note)
    assert '14b' not in note or '不要用' in note, '说明里若提到 14B，必须是"不要用"的警示'


def test_12gb_recommends_8b():
    tag, _ = A.recommend_text_model(12, with_vlm=True)
    assert tag == 'qwen3:8b', tag


def test_high_vram_still_gets_big_models():
    """不能矫枉过正：16GB/24GB 仍应拿到 14B/30B。"""
    assert A.recommend_text_model(16, with_vlm=True)[0] == 'qwen3:14b-q4_K_M'
    assert A.recommend_text_model(24, with_vlm=True)[0] == 'qwen3:30b-q4_K_M'


def test_hardware_tier_table_12gb_not_14b():
    """HARDWARE_MODEL_RECS 的 12GB 档不得推荐 14B。"""
    hit = None
    for min_vram, tier, vlm, whisper, tts, text, note in S.HARDWARE_MODEL_RECS:
        if min_vram == 12:
            hit = (tier, text, note)
    assert hit, '应存在 12GB 档'
    assert '14b' not in hit[1], hit
    assert '不要用 14B' in hit[2] or '不要用14B' in hit[2].replace(' ', ''), hit[2]


def test_hardware_tier_table_sorted_desc():
    """分档表必须按显存降序（否则匹配循环会先命中低档）。"""
    vs = [r[0] for r in S.HARDWARE_MODEL_RECS]
    assert vs == sorted(vs, reverse=True), vs


def test_detect_hardware_recommends_8b_on_12gb(monkeypatch):
    """端到端：模拟 12GB 显卡，detect_hardware 应给出 8B 而不是 14B。"""
    import subprocess
    real_run = subprocess.run

    def _fake_run(cmd, *a, **k):
        if isinstance(cmd, (list, tuple)) and cmd and 'nvidia-smi' in str(cmd[0]):
            class _R:
                returncode = 0
                stdout = 'NVIDIA GeForce RTX 3060 Laptop GPU, 12288\n'
            return _R()
        return real_run(cmd, *a, **k)
    monkeypatch.setattr(subprocess, 'run', _fake_run)
    monkeypatch.setattr(A, '_GPU_VRAM_CACHE', {'val': None})
    info = S.detect_hardware()
    assert info['gpu_vram_gb'] == 12.0, info['gpu_vram_gb']
    assert '14b' not in (info['recommendations'].get('text') or ''), info['recommendations']
    assert info['recommendations'].get('text') == 'qwen3:8b', info['recommendations']


def test_narr_guide_recommends_by_vram(monkeypatch):
    """解说模型引导（引导用户 pull 哪个模型）也必须按显存，不能再固定 14B。"""
    import movie_narrator as MN
    monkeypatch.setattr(A, '_GPU_VRAM_CACHE', {'val': 12.0})
    monkeypatch.setattr(S, 'gpu_vram_gb', lambda: 12.0)
    monkeypatch.setattr(MN, 'local_llm_cfg', lambda: {'model': '', 'base_url': '', 'api_key': ''})
    monkeypatch.setattr(MN, '_local_model_available', lambda: False)
    monkeypatch.setattr(MN, 'vlm_cfg', lambda: {'model': 'minicpm-v4.5'})
    monkeypatch.setattr(MN, '_installed_local_models', lambda: [])
    guide = MN._model_narr_guide()
    assert '14b' not in (guide['recommend'] or ''), guide
    assert 'qwen3:8b' in (guide['recommend'] or ''), guide


# ---------------------------------------------------------------------------
# 前端：档位卡与模型卡片的静态契约
# ---------------------------------------------------------------------------
def _app_js():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'static', 'app.js'), encoding='utf-8') as f:
        return f.read()


def test_frontend_tier_g12_not_14b():
    """前端「甜点·12G」档位卡不得再给 14B。"""
    js = _app_js()
    m = re.search(r"\{id:'g12'.*?\},", js, re.S)
    assert m, '未找到 g12 档位定义'
    line = m.group(0)
    assert '14b' not in line, line
    assert 'qwen3:8b' in line, line


def test_frontend_model_cards_have_min_vram():
    """每个 14B 卡片必须带 minVram≥16（前端据此标红提示放不下）。"""
    js = _app_js()
    for tag, need in (('qwen3:14b-q4_K_M', 16), ('qwen2.5:14b', 16)):
        m = re.search(r"\{tag:'%s'.*?\}\s*," % re.escape(tag), js)
        assert m, '未找到卡片 %s' % tag
        seg = m.group(0)
        mv = re.search(r"minVram:(\d+)", seg)
        assert mv, '卡片 %s 缺 minVram 标注：%s' % (tag, seg)
        assert int(mv.group(1)) >= need, '%s 的 minVram 应 ≥%d' % (tag, need)


def test_frontend_no_hardcoded_14b_pull_fallback():
    """前端兜底文案不得再硬编码 14B（12GB 卡会被误导）。"""
    js = _app_js()
    assert "ollama pull qwen2.5:14b" not in js, '仍存在硬编码的 qwen2.5:14b 兜底'
