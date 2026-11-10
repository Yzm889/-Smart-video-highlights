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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, 'movie_narrator.py')


def _src():
    return io.open(SRC, encoding='utf-8').read()


def _vlm_block():
    """截取 _narrate_by_plot 里「else: VLM 抽样」那一段，避免全文误匹配。"""
    s = _src()
    i = s.index('vlm_cache_key = _video_cache_key(')
    j = s.index('保存scene_descs到run_dir', i)
    return s[i:j]


def test_vram_guard_runs_before_vlm_sampling():
    """抽样前必须腾显存，且顺序上在 _vlm_sample_timeline 之前。"""
    b = _vlm_block()
    assert 'free_vram_for_llm' in b, '抽样前缺少显存守卫'
    assert b.index('free_vram_for_llm') < b.index('_vlm_sample_timeline('), (
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
    assert b.index('free_vram_for_llm') < b.index('try:\n            scene_descs')


def test_unload_keeps_existing_cache_save_behaviour():
    """修复不能把原有的画面索引缓存写盘弄丢。"""
    b = _vlm_block()
    assert '_cache_save(vlm_cache_key, scene_descs)' in b


def test_non_vacuous_proof_against_pre_fix_source():
    """非空转证明：修复前的源码跑上面三条断言必须变红。"""
    rev = '09ca29b^'  # 本次修复前的 HEAD
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
    assert 'finally:' not in b, '旧版竟然已有 finally？探针失效'
    assert '常驻显存' not in b, '旧版竟然已有 UI 提示？探针失效'
    assert 'vlm_unload()' in b, '旧版应仍有正常路径卸载（证明取的是正确版本）'
