# -*- coding: utf-8 -*-
"""VLM 画面理解阶段的显存守门：抽样前腾位 + 任何路径都卸载 + UI 可见。

背景（2026-11-10 主人实测）：「跑 vlm 的时候显存给干满了」。
tts2_engines.gpu_vram 的注释写明了后果 —— WDDM 下占满不报错，而是把权重静默
换页到内存，GPU 利用率 0%，看起来就是「永远合成中」（2026-11-09 试听失败即此因）。

旧实现只有「正常结束后 vlm_unload()」，三个缺口：
  ① VLM 抽样前不腾位（free_vram_for_llm 只在文本模型调用前触发）；
  ② vlm_unload() 只在正常路径，取消/异常时 VLM 会按 keep_alive='30m' 驻留半小时；
  ③ 进度区没有任何「VLM 常驻显存、完成后自动释放」的可见提示。

本文件用源码级契约钉住修复，并给出非空转证明：导出 09ca29b 之前的
movie_narrator.py（git show）跑同一组断言，三条都应变红。
"""
import io
import os
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'movie_narrator.py')


def _git_has_commit(rev):
    """本地是否有这个 revision 的对象。

    2026-11-10：CI 的 actions/checkout@v4 默认 fetch-depth=1（浅克隆），
    仓库里只有当前一个 commit，`git show 09ca29b^:...` 会 `fatal: invalid
    object name`（returncode 128）。非空转证明本质是**开发者在本地写代码
    那一刻的一次性验证**，不是每轮必跑的回归 —— 取不到历史就明示 skip，
    绝不假装通过。
    """
    try:
        r = subprocess.run(
            ['git', 'rev-parse', '--verify', '--quiet', '%s^{commit}' % rev],
            capture_output=True, cwd=ROOT, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def _src():
    return io.open(SRC, encoding='utf-8').read()


def _vlm_block():
    """截取 _narrate_by_plot 里「取模型名 → 抽样 → 卸载」整段，避免全文误匹配。"""
    s = _src()
    i = s.index('# VLM缓存：同视频同模型不重复分析')
    j = s.index('保存scene_descs到run_dir', i)
    return s[i:j]


def test_vram_guard_runs_before_vlm_sampling():
    """抽样前必须腾显存，且顺序上在 _vlm_sample_timeline 之前。"""
    b = _vlm_block()
    assert 'free_vram_for_vlm' in b, '抽样前缺少显存守卫'
    assert b.index('free_vram_for_vlm') < b.index('_vlm_sample_timeline('), (
        '腾位必须发生在抽样之前，否则 VLM 已把显存灌满')


def test_vlm_unload_is_in_finally_covering_cancel_and_exception():
    """卸载必须在 finally 里，且包住抽样调用（覆盖取消 / 异常 / AbortError）。"""
    b = _vlm_block()
    assert 'finally:' in b, 'vlm_unload 缺少 finally 兜底'
    fi = b.index('finally:')
    sample = b.index('_vlm_sample_timeline(')
    unload = b.rindex('vlm_unload()')
    assert sample < fi < unload, (
        '结构应为 try{抽样} finally{卸载}；当前顺序 sample=%d finally=%d unload=%d'
        % (sample, fi, unload))


def test_vram_state_is_visible_in_progress_phase():
    """进度区必须让用户知道显存被占用、且会自动释放。"""
    b = _vlm_block()
    assert '常驻显存' in b and '自动释放' in b, (
        '缺少 VLM 常驻显存/完成后自动释放的可见提示')


def test_guard_failure_does_not_block_sampling():
    """腾显存失败不能阻断主流程（守卫是尽力而为）。"""
    b = _vlm_block()
    assert 'except Exception' in b and '不阻断' in b
    # 守卫自己也包在 try 内，不在主 try 之外
    assert b.index('free_vram_for_vlm') < b.index('try:\n            scene_descs')


def test_unload_keeps_existing_cache_save_behaviour():
    """修复不能把原有的画面索引缓存写盘弄丢。"""
    b = _vlm_block()
    assert '_cache_save(vlm_cache_key, scene_descs)' in b


def test_non_vacuous_proof_against_pre_fix_source():
    """非空转证明：修复前的源码跑上面三条断言必须变红。"""
    rev = '09ca29b^'  # 本次修复前的 HEAD
    if not _git_has_commit(rev):
        pytest.skip('仓库没有 %s 的历史对象（CI 浅克隆 fetch-depth=1），'
                    '此证明仅在本地完整克隆下执行' % rev)
    r = subprocess.run(
        ['git', 'show', '%s:movie_narrator.py' % rev],
        capture_output=True, cwd=ROOT, timeout=30)
    assert r.returncode == 0, '取不到修复前源码（git show 失败）'
    old = r.stdout.decode('utf-8', 'replace')
    assert len(old) > 100000, '导出的旧文件异常小，可能取错'
    i = old.index('vlm_cache_key = _video_cache_key(')
    j = old.index('保存scene_descs到run_dir', i)
    b = old[i:j]
    # 三条关键能力在旧版都不存在
    assert 'free_vram_for_llm' not in b, '旧版竟然已有腾位守卫？探针失效'
    assert 'free_vram_for_vlm' not in b, '旧版竟然已有 VLM 专用腾位？探针失效'
    assert 'finally:' not in b, '旧版竟然已有 finally？探针失效'
    assert '常驻显存' not in b, '旧版竟然已有 UI 提示？探针失效'
    assert 'vlm_unload()' in b, '旧版应仍有正常路径卸载（证明取的是正确版本）'


# ---------------------------------------------------------------------------
# 第二轮（2026-11-10 深夜）：主人报「每次跑显存内存都占满、VLM 推理到一点点就卡住、
# 16G 内存也卡」。上一轮的 free_vram_for_llm 只停 TTS worker、**不碰 Ollama**，
# 而真正的对手是 Ollama 里常驻的写稿模型（实测 qwen3:8b 驻留 9.12GB）。
# 另外 Ollama 默认按 32768 加载，qwen3-vl:8b 权重 5.38GB 却驻留 10.2GB ——
# KV 缓存吃掉 4.8GB。两条一起把 12GB 卡打到 11959/12288。
# ---------------------------------------------------------------------------
def test_vlm_guard_targets_ollama_not_just_tts_worker():
    """腾位必须点名 Ollama：只用 free_vram_for_llm（只停 worker）等于没修。

    断言用调用形态 `_T2.free_vram_for_llm`：注释里会提到旧写法做对照，
    纯字符串匹配会把说明文字也算进来。"""
    b = _vlm_block()
    assert 'free_vram_for_vlm' in b
    assert '_T2.free_vram_for_llm' not in b, (
        'free_vram_for_llm 只停 TTS worker、不卸 Ollama 模型，'
        '用它为 VLM 腾位是本轮 bug 的直接原因')


def test_vlm_num_ctx_is_pinned_to_both_call_paths():
    """num_ctx 必须显式给定且两条调用路径共用同一个值。

    不给 num_ctx 时 Ollama 按模型默认 32768 加载，KV 缓存和权重一样大。
    两条路径各写各的值更坑：ctx 一变就要重载模型，批量里混单帧调用会
    变成「每帧重载 5.4GB」。"""
    import io as _io
    ap = _io.open(os.path.join(ROOT, 'ai_providers.py'), encoding='utf-8').read()
    ws = _io.open(os.path.join(ROOT, 'webui_server.py'), encoding='utf-8').read()
    assert '_VLM_NUM_CTX = 16384' in ap, '上下文上限没钉住'
    # vlm_chat_multi（批量）
    assert "'num_ctx': _VLM_NUM_CTX" in ap
    # vlm_chat（单帧）必须复用它，而不是自己写一个数
    i = ws.index('def vlm_chat(image_path')
    seg = ws[i:i + 2000]
    assert "'num_ctx': _VLM_NUM_CTX" in seg, '单帧调用没有 num_ctx，会按 32K 重载'


def test_vlm_model_does_not_fall_back_to_literal_default():
    """vlm 段缺 model 键时不能拿到字符串 'default'。

    ai_config.json 实测就只有 enabled/base_url，没有 model。旧代码直接读配置 →
    'default' → 缓存键与内层对不上、腾位时查不到模型体积。"""
    b = _vlm_block()
    assert "get('model', 'default')" not in b, (
        "别再退回字面量 'default'，要走 vlm_cfg() 的 qwen3-vl:8b 兜底")
    assert 'vlm_cfg()' in b

