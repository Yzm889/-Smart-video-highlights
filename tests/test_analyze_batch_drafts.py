# -*- coding: utf-8 -*-
"""阶段①草稿批量化回归（task #26）。

背景（实测）：20 分钟视频切 60 段，旧实现**逐段串行**调用 LLM → 60 次模型请求。
本机（RTX3060 12GB + 16GB 内存）单次调用 47~159 秒，且失败后模型不驻留、每次要
重读 9.28GB 权重 → 磁盘 I/O 打满 + 页缓存抖动 → 整机卡死（用户反馈「网卡都卡掉」）。

本文件锁定：
  ① 批量路径：N 段只发**1 次**请求（而不是 N 次）；
  ② 解析：数组被模型包在解释文字/```json 围栏里也能解出；模型漏写 i 时按位置对应；
  ③ 段数少时仍走原逐段路径（行为不变、失败粒度更细）；
  ④ 解析不完整/失败必须产出可见告警，不静默；
  ⑤ 本地模型失败原因要进 warnings（原来被 except: pass 吞掉，用户只感到「卡」）。
"""
import json
import os

import pytest

import webui_server          # 必须先导入宿主，规避 video_render<->webui_server 循环导入
import analyze_pipeline as A


# ---------------------------------------------------------------------------
# 解析：_try_parse_json_array
# ---------------------------------------------------------------------------
def test_parse_plain_array():
    arr = A._try_parse_json_array('[{"i":0,"简洁":"a"},{"i":1,"简洁":"b"}]')
    assert isinstance(arr, list) and len(arr) == 2


def test_parse_array_with_surrounding_text():
    txt = '好的，以下是结果：\n[{"i":0,"简洁":"a"}]\n希望对你有帮助！'
    arr = A._try_parse_json_array(txt)
    assert arr and arr[0]['i'] == 0


def test_parse_array_in_code_fence():
    txt = '```json\n[{"i":0,"简洁":"a"}]\n```'
    arr = A._try_parse_json_array(txt)
    assert arr and arr[0]['简洁'] == 'a'


def test_parse_multiline_objects_fallback():
    """模型把数组写成多行对象时也要能兜住。"""
    txt = '{"i":0,"简洁":"a"}\n{"i":1,"简洁":"b"}'
    arr = A._try_parse_json_array(txt)
    assert arr and len(arr) == 2 and arr[1]['i'] == 1


def test_parse_garbage_returns_none():
    assert A._try_parse_json_array('完全不是 JSON') is None
    assert A._try_parse_json_array('') is None


# ---------------------------------------------------------------------------
# 批量草稿
# ---------------------------------------------------------------------------
def _batch_json(n, with_i=True):
    items = []
    for i in range(n):
        d = {'简洁': '简洁%d' % i, '悬念': '悬念%d' % i, '口语': '口语%d' % i}
        if with_i:
            d['i'] = i
        items.append(d)
    return json.dumps(items, ensure_ascii=False)


def test_batch_maps_to_correct_segments(monkeypatch):
    """草稿必须归到正确的段（错位比没有更糟）。"""
    calls = {'n': 0}

    def _fake_llm(prompt, system='', timeout=180, max_tokens=None):
        calls['n'] += 1
        assert 'max_tokens' in str(max_tokens) or max_tokens  # 批量必须给足输出预算
        return _batch_json(3)
    monkeypatch.setattr(A, '_llm_light', _fake_llm)
    items = [(10, '台词A'), (20, '台词B'), (30, '台词C')]
    got, err = A._drafts_for_segments_batch(items)
    assert err is None
    assert calls['n'] == 1
    assert got[10][0]['text'] == '简洁0'
    assert got[20][0]['text'] == '简洁1'
    assert got[30][0]['text'] == '简洁2'


def test_batch_positional_fallback_when_model_omits_i(monkeypatch):
    """模型忽略 i 字段时按位置对应（数量正好相等才敢猜）。"""
    monkeypatch.setattr(A, '_llm_light',
                        lambda prompt, system='', timeout=180, max_tokens=None: _batch_json(3, with_i=False))
    got, err = A._drafts_for_segments_batch([(1, 'a'), (2, 'b'), (3, 'c')])
    assert err is None
    assert got[1][0]['text'] == '简洁0'
    assert got[3][0]['text'] == '简洁2'


def test_batch_parse_failure_is_visible(monkeypatch):
    """解析失败必须返回错误标记（调用方据此给可见告警），不静默返回空。"""
    monkeypatch.setattr(A, '_llm_light', lambda *a, **k: '这不是 JSON')
    got, err = A._drafts_for_segments_batch([(1, 'a'), (2, 'b')])
    assert got == {}
    assert err == 'llm_parse_fail'


# ---------------------------------------------------------------------------
# 端到端：dispatch_analyze 的调用次数
# ---------------------------------------------------------------------------
def _setup_pipeline(monkeypatch, tmp_path, n_segs, llm_impl):
    import ai_providers as AP
    # 隔离真实推理探针：本文件测的是「批量/并发」逻辑，探针另有专门用例
    # （tests/test_local_infer_probe.py）。不隔离会打到真实 Ollama（可能卡 25s）。
    monkeypatch.setattr(AP, 'local_llm_infer_probe', lambda *a, **k: (True, '推理正常'))
    video = tmp_path / 'src.mp4'
    video.write_bytes(b'0' * 1024)
    segs = [(i * 20.0, (i + 1) * 20.0) for i in range(n_segs)]
    monkeypatch.setattr(A, '_segment_timeline', lambda vp, max_seg=25.0: segs)
    monkeypatch.setattr(A, 'probe_audio_len', lambda p: n_segs * 20.0)
    asr = [{'start': i * 20.0 + 1, 'end': i * 20.0 + 19, 'text': '第%d段台词' % i} for i in range(n_segs)]
    monkeypatch.setattr(A, 'asr_segments', lambda vp, progress=None, pct_range=None: asr)

    def _thumb(video_path, mid, out_jpg):
        with open(out_jpg, 'wb') as f:
            f.write(b'0' * 100)
        return True
    monkeypatch.setattr(A, '_make_thumbnail', _thumb)
    monkeypatch.setattr(A, '_llm_light', llm_impl)
    run_dir = tmp_path / 'run'
    run_dir.mkdir()
    prog = {'run_dir': str(run_dir), 'warnings': []}
    return video, prog


def test_dispatch_many_segments_uses_chunked_llm_calls(monkeypatch, tmp_path):
    """核心锁定：批量路径**按块**请求（不再逐段 60 次），且每段都拿到草稿。

    一次请求 60 段会有两个问题：max_tokens 封顶导致 JSON 数组尾部被截断；
    模型一次处理 60 段容易"偷懒只写一条"。所以按 DRAFT_BATCH_CHUNK 分块。"""
    calls = {'draft': 0}

    def _llm(prompt, system='', timeout=180, max_tokens=None):
        if '同一部视频的' in prompt:
            calls['draft'] += 1
        return _batch_json(10)
    video, prog = _setup_pipeline(monkeypatch, tmp_path, 10, _llm)
    A.dispatch_analyze({'video_path': str(video), 'options': {'draft': True}}, prog)
    import math
    expect = math.ceil(10 / A.DRAFT_BATCH_CHUNK)
    assert calls['draft'] == expect, '应分 %d 块请求，实际 %d 次' % (expect, calls['draft'])
    res = prog.get('analyze_result')
    assert res and len(res['segments']) == 10
    assert all(len(s['ai_draft_narrations']) == 3 for s in res['segments']), '每段都应有 3 版草稿'


def test_batch_chunks_large_batch(monkeypatch):
    """大批量必须切成多块（控制输出长度，避免 JSON 数组被截断）。"""
    calls = {'n': 0, 'sizes': []}

    def _llm(prompt, system='', timeout=180, max_tokens=None):
        calls['n'] += 1
        n = prompt.count('\n[')
        calls['sizes'].append(n)
        return _batch_json(n)
    monkeypatch.setattr(A, '_llm_light', _llm)
    items = [(i, '台词%d' % i) for i in range(20)]
    got, err = A._drafts_for_segments_batch(items)
    assert err is None, err
    assert calls['n'] == 3, '20 段应切 3 块，实际 %d 块' % calls['n']
    assert all(s <= A.DRAFT_BATCH_CHUNK for s in calls['sizes']), calls['sizes']
    assert len(got) == 20, '应有 20 段草稿，实际 %d' % len(got)


def test_batch_retries_missing_segments(monkeypatch):
    """模型漏写（只吐 1 条）时，应自动补跑缺的段，而不是直接放弃。"""
    state = {'first': True}

    def _llm(prompt, system='', timeout=180, max_tokens=None):
        n = prompt.count('\n[')
        if state['first']:
            state['first'] = False
            return json.dumps([{'i': 0, '简洁': 'a', '悬念': 'b', '口语': 'c'}], ensure_ascii=False)
        return _batch_json(n)
    monkeypatch.setattr(A, '_llm_light', _llm)
    items = [(i, '台词%d' % i) for i in range(5)]
    got, err = A._drafts_for_segments_batch(items)
    assert err is None, err
    assert len(got) == 5, '补跑后应覆盖 5 段，实际 %d' % len(got)


def test_dispatch_few_segments_keeps_per_segment_path(monkeypatch, tmp_path):
    """段数少时仍走原逐段路径（行为不变）。"""
    calls = {'draft': 0}

    def _llm(prompt, system='', timeout=180, max_tokens=None):
        if '下面是一段视频台词' in prompt:
            calls['draft'] += 1
            return json.dumps({'简洁': 'a', '悬念': 'b', '口语': 'c'}, ensure_ascii=False)
        return '{}'
    n = A.DRAFT_BATCH_MIN - 1
    video, prog = _setup_pipeline(monkeypatch, tmp_path, n, _llm)
    A.dispatch_analyze({'video_path': str(video), 'options': {'draft': True}}, prog)
    assert calls['draft'] == n, '少段时应逐段调用（%d 次），实际 %d 次' % (n, calls['draft'])


def test_dispatch_reports_local_model_error(monkeypatch, tmp_path):
    """本地模型失败原因必须进 warnings（不静默）。"""
    import ai_providers as AP

    def _llm(prompt, system='', timeout=180, max_tokens=None):
        AP._LOCAL_LLM_FAIL.update({'count': 2, 'last_error': 'llama-server process has terminated: exit 0xe06d7363',
                                   'last_model': 'qwen3:14b-q4_K_M'})
        return '这不是 JSON'
    video, prog = _setup_pipeline(monkeypatch, tmp_path, 10, _llm)
    try:
        A.dispatch_analyze({'video_path': str(video), 'options': {'draft': True}}, prog)
        res = prog.get('analyze_result')
        warns = ' '.join(res['warnings'])
        assert '本地模型' in warns, res['warnings']
        assert 'llama-server' in warns, res['warnings']
    finally:
        AP._LOCAL_LLM_FAIL.update({'count': 0, 'last_error': '', 'last_model': ''})


def test_dispatch_thumbnails_all_extracted(monkeypatch, tmp_path):
    """并发抽帧后每段缩略图都要有（不能因为并发丢图）。"""
    def _llm(prompt, system='', timeout=180, max_tokens=None):
        return _batch_json(10)
    video, prog = _setup_pipeline(monkeypatch, tmp_path, 10, _llm)
    A.dispatch_analyze({'video_path': str(video), 'options': {'draft': True}}, prog)
    res = prog.get('analyze_result')
    for s in res['segments']:
        assert s['thumbnail_url'], '段 %d 缺缩略图' % s['segment_id']
        assert os.path.basename(s['thumbnail_url']).endswith('.jpg')
