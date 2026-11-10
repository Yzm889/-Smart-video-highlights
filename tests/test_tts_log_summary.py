# -*- coding: utf-8 -*-
"""tts_log.json 的 summary 统计口径回归钉（[2.5]）。

真实故障：真机产物 `run-1-20261109-185905/tts_log.json` 里 summary 是
  {"total":52,"success":52,"failed":0,"skipped":0,
   "engines_used":["indextts2","none"],
   "failed_segments":[{"idx":0,"engine":"none","error":"all_engines_failed"}]}
—— `failed:0` 和 `failed_segments` 有 1 条，**同一份 summary 自相矛盾**。
根因：`failed` 统计的是「最终仍缺失」= len(narr)-len(results)-skipped，
重试兜回后 len(results)==len(narr)，它恒为 0，「第一轮曾经失败过」彻底不可见。
（idx 0 是首段冷启动失败 3273ms → 1 秒后重试成功 8004ms；成片 52/52 完整，
 所以不是丢了配音，是日志看不出曾经失败。）

修法（不动 `failed` 语义，它注释写着兼容旧字段）：新增
  `first_failures` = 首轮（source=='fresh'）失败段数
  `retried`        = 被重试兜回（source=='retry' 且 ok）段数
并钉住不变量 `failed + retried == first_failures`（正常非取消路径下成立）。
"""
import json
import os

import webui_server as S  # noqa: F401,E402  先导入宿主（符号晚绑定）
import movie_narrator as M  # noqa: E402


def _run(monkeypatch, tmp_path, n=4, fail_idx=0):
    """驱动真实 _generate_all_tts：fail_idx 那段首轮失败、重试成功。"""
    attempts = {}

    def fake_speak(text, out_path):
        attempts[text] = attempts.get(text, 0) + 1
        # 只有指定段落在「第一次」失败，模拟 all_engines_failed 冷启动
        if text == '第%d句' % fail_idx and attempts[text] == 1:
            return False, '', ''      # -> _err='all_engines_failed', _eng='none'
        with open(out_path, 'wb') as f:
            f.write(b'0' * 2048)
        return True, 'indextts2', out_path

    monkeypatch.setattr(M, 'load_ai_config', lambda: {'tts': {}})
    monkeypatch.setattr(M, 'edge_tts_available', lambda: True)
    monkeypatch.setattr(M, 'edge_tts_dead_reason', lambda: '')
    monkeypatch.setattr(M, '_edge_probe_recover', lambda rd: None)
    monkeypatch.setattr(M, '_aborted', lambda: False)
    monkeypatch.setattr(M, 'local_tts_speak', fake_speak)
    monkeypatch.setenv('TTS_CONCURRENCY', '1')   # 串行，避开预热/并发对断言的干扰

    prog = {'phase': '', 'pct': 0}
    narr = ['第%d句' % i for i in range(n)]
    res = M._generate_all_tts(narr, str(tmp_path), progress=prog)
    with open(os.path.join(str(tmp_path), 'tts_log.json'), encoding='utf8') as f:
        return json.load(f), res


def test_first_failure_is_visible_even_after_retry_succeeds(monkeypatch, tmp_path):
    """核心钉：首轮失败 1 段 + 重试成功 → 日志必须同时反映两件事。"""
    jd, res = _run(monkeypatch, tmp_path)
    s = jd['summary']
    assert s['first_failures'] == 1, \
        '首轮那 1 次失败必须可见，实际 first_failures=%r' % (s.get('first_failures'),)
    assert s['retried'] == 1, '必须记录「被重试兜回 1 段」，实际 %r' % (s.get('retried'),)
    assert s['failed'] == 0, '最终不缺失 —— 修的正是「failed=0 掩盖首轮失败」，别改掉它'
    assert len(s['failed_segments']) == 1, '首轮失败明细必须仍在'
    assert s['failed_segments'][0]['error'] == 'all_engines_failed'
    assert len(res) == 4, '四段配音最终都要拿到'


def test_retry_survives_cancel_free_normal_run(monkeypatch, tmp_path):
    """不变量：failed + retried == first_failures（正常路径下必须自洽）。"""
    jd, _res = _run(monkeypatch, tmp_path, n=4, fail_idx=2)
    s = jd['summary']
    assert s['failed'] + s['retried'] == s['first_failures'], \
        'summary 自相矛盾：failed=%r retried=%r first_failures=%r' % (
            s['failed'], s['retried'], s['first_failures'])
    assert s['first_failures'] == 1 and s['retried'] == 1


def test_clean_run_reports_zero_first_failures(monkeypatch, tmp_path):
    """一次都没失败时不得误报（否则排障时全是假阳性）。"""
    jd, _res = _run(monkeypatch, tmp_path, n=3, fail_idx=-1)
    s = jd['summary']
    assert s['first_failures'] == 0 and s['retried'] == 0
    assert s['failed'] == 0 and s['failed_segments'] == []
    assert s['failed'] + s['retried'] == s['first_failures']


def test_summary_keys_stay_backward_compatible(monkeypatch, tmp_path):
    """新增字段是「加」不是「换」：旧字段一个都不能少。"""
    jd, _res = _run(monkeypatch, tmp_path, n=2, fail_idx=0)
    s = jd['summary']
    for k in ('total', 'success', 'failed', 'skipped',
              'engines_used', 'broken_until', 'failed_segments'):
        assert k in s, '旧字段 %s 被删了 —— 会破坏既有消费方' % k
    assert 'segments' in jd and isinstance(jd['segments'], list)


def test_failed_segments_is_first_round_and_failed_is_final(monkeypatch, tmp_path):
    """两个字段各自的语义要钉死，别混为一谈。

    `failed_segments` = **首轮**失败明细（所以重试救回的段**本来就会**出现在这里，
    真机产物 idx 0 正是如此）；`failed` = **最终仍缺失**段数（救回的段不该算进去）。
    """
    jd, _res = _run(monkeypatch, tmp_path, n=3, fail_idx=1)
    s = jd['summary']
    segs = jd['segments']
    first_bad = set(e['idx'] for e in segs
                    if not e.get('ok') and (e.get('source') or '') == 'fresh')
    failed_idx = set(e['idx'] for e in s['failed_segments'])
    assert failed_idx == first_bad == {1}, \
        'failed_segments 必须等于首轮失败集合，实际 %r vs %r' % (failed_idx, first_bad)
    finally_missing = set(e['idx'] for e in segs if not e.get('ok'))
    # 救回的那段在 segments 里同时有失败行与成功行，但没有任何段是「最终仍缺失」
    assert s['failed'] == len(finally_missing - first_bad) == 0, \
        '被重试救回的段绝不能算进「最终仍缺失」'
