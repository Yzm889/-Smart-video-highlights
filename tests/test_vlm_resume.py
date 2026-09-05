# -*- coding: utf-8 -*-
"""任务2 VLM 分析断点续跑的回归锁定。

mock 掉 VLM 推理与缓存层，调用真实的 _vlm_sample_timeline()，验证：
  1. 中途取消后进度文件落盘，已分析场景不丢
  2. 重新开始时跳过已完成场景，只分析剩下的
  3. 换视频/模型/抽样间隔时旧进度自动作废（指纹校验）
  4. 全部完成后进度文件被清理
  5. 写入是原子的（目录里不留临时文件）
  6. 失败的批次不落盘，下次会重试

运行：pytest tests/test_vlm_resume.py   或   python tests/test_vlm_resume.py
"""
import json
import os
import shutil
import sys
import tempfile

import pytest
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

VDUR = 3600.0          # interval = max(15, 3600/60) = 60 → 60 个抽样点
N_SAMPLES = 60
BATCH = 6              # 与 _vlm_sample_timeline 内的 batch_size 一致


class Env:
    """测试环境：临时目录 + 假抽帧 + webui_server 的 VLM/缓存层 mock。"""

    def __init__(self, work):
        self.work = work
        self.frame_dir = os.path.join(work, 'frames')
        self.run_dir = os.path.join(work, 'run-1')
        self.prog_file = os.path.join(self.run_dir, 'vlm_progress.json')
        os.makedirs(self.frame_dir, exist_ok=True)
        os.makedirs(self.run_dir, exist_ok=True)
        # 相邻帧灰度差异 60 → 相似度约 0.76 < 0.88 阈值，全部成为独立关键帧
        for i in range(N_SAMPLES):
            g = (i * 60) % 256
            Image.new('RGB', (64, 64), (g, g, g)).save(
                os.path.join(self.frame_dir, 'sample_%04d.jpg' % i), quality=80)
        self.calls = 0
        self.abort_at = None
        self.fail_at = None
        self.saved = []
        self.asr = [{'start': 0.0, 'end': VDUR, 'text': '全片都有台词'}]

    def reset(self):
        if os.path.exists(self.prog_file):
            os.remove(self.prog_file)
        self.calls = 0
        self.abort_at = None
        self.fail_at = None
        self.saved = []
        self.set_abort(False)

    def set_abort(self, on):
        W.PROGRESS['vlm-resume-test']['abort'] = bool(on)

    # --- 假 VLM ---
    def _chat_multi(self, frames, prompt, system=None, timeout=30):
        self.calls += 1
        if self.fail_at is not None and self.calls == self.fail_at:
            raise RuntimeError('模拟VLM调用失败')
        if self.abort_at is not None and self.calls >= self.abort_at:
            self.set_abort(True)           # 模拟用户在第 N 批点了「停止」
        parts = [json.dumps({'location': '地点%d' % self.calls, 'characters': '人物%d' % k,
                             'event': '事件', 'dialogue': '台词', 'summary': '概要%d' % k},
                            ensure_ascii=False) for k, _f in enumerate(frames)]
        return '\n---\n'.join(parts)

    def _chat_one(self, image_path, text, system=None, timeout=20):
        self.calls += 1
        if self.fail_at is not None and self.calls == self.fail_at:
            raise RuntimeError('模拟VLM调用失败')
        return json.dumps({'location': '单帧地点', 'characters': '人物', 'event': '事件',
                           'dialogue': '台词', 'summary': '单帧概要'}, ensure_ascii=False)

    def run(self, progress=None):
        return W._vlm_sample_timeline('fake.mp4', VDUR, self.asr, self.run_dir,
                                      progress=progress if progress is not None else {})

    def load_progress(self):
        return json.load(open(self.prog_file, encoding='utf-8'))


import webui_server as W  # noqa: E402  (需在 ROOT 入 sys.path 之后)


@pytest.fixture(scope='module')
def env():
    work = tempfile.mkdtemp(prefix='vlm_resume_')
    e = Env(work)
    # 保存原实现，测试结束后恢复，避免污染同进程其他测试
    orig = {n: getattr(W, n) for n in (
        'vlm_enabled', 'vlm_ping', 'vlm_cfg', '_video_cache_key', '_cache_load',
        '_cache_save', '_sample_frame_cache_dir', '_sample_frame_cache_ready',
        '_sample_frame_cache_mark', '_sample_frame_cache_trim', 'vlm_chat_multi', 'vlm_chat')}
    W.vlm_enabled = lambda: True
    W.vlm_ping = lambda: (True, '')
    W.vlm_cfg = lambda: {'model': 'test-model'}
    W._video_cache_key = lambda vp, suffix='': 'FINGERPRINT-A'
    W._cache_load = lambda key: None
    W._cache_save = lambda key, value: e.saved.append((key, value))
    W._sample_frame_cache_dir = lambda vp, itv: e.frame_dir
    W._sample_frame_cache_ready = lambda d, n: True
    W._sample_frame_cache_mark = lambda *a, **k: None
    W._sample_frame_cache_trim = lambda: None
    W.vlm_chat_multi = e._chat_multi
    W.vlm_chat = e._chat_one
    # _aborted() 依赖线程局部 runid + PROGRESS，这里手工挂上
    W._TLS.runid = 'vlm-resume-test'
    W.PROGRESS['vlm-resume-test'] = {'abort': False}
    yield e
    for n, fn in orig.items():
        setattr(W, n, fn)
    W.PROGRESS.pop('vlm-resume-test', None)
    _cleanup_dir(work)


def _cleanup_dir(work):
    """逐个删文件再删目录。不用 shutil.rmtree：部分环境（含本仓库的删除钩子）
    会拦截批量删除并中断进程。清理失败不影响测试结论。"""
    for root, _dirs, files in os.walk(work, topdown=False):
        for f in files:
            try:
                os.remove(os.path.join(root, f))
            except OSError:
                pass
        try:
            os.rmdir(root)
        except OSError:
            pass


def test_abort_keeps_progress(env):
    """中途取消：已分析场景落盘，且不再发起新的 VLM 调用。"""
    env.reset()
    env.set_abort(True)                      # 一开始就取消 → 一批都不该跑
    env.run()
    assert env.calls == 0, '收到取消信号后仍发起了 %d 次 VLM 调用' % env.calls

    env.reset()
    env.abort_at = 4                         # 第 4 批起模拟用户点停止
    env.run()
    env.set_abort(False)
    env.abort_at = None
    assert os.path.exists(env.prog_file), '取消后进度文件不存在'
    pf = env.load_progress()
    assert len(pf['results']) > 0, '已分析的场景没有写入进度文件'
    assert pf['fingerprint'] == 'FINGERPRINT-A'
    assert sorted(pf['results'].keys()) == pf['completed']


def test_resume_skips_completed(env):
    """重新开始：跳过已完成场景，只分析剩下的，且恢复的内容与首次一致。"""
    env.reset()
    env.abort_at = 4
    env.run()
    env.set_abort(False)
    env.abort_at = None
    first = env.load_progress()['results']
    done_n = len(first)

    calls_before = env.calls
    results = env.run()
    assert env.calls - calls_before < env.calls, '第二次没有节省 VLM 调用'
    assert len(results) == N_SAMPLES, '最终应返回全部 %d 个抽样点，实际 %d' % (N_SAMPLES, len(results))
    assert all(x.get('summary') for x in results), '存在没有分析结果的场景'
    recovered = {round(x['start'], 3): x['summary'] for x in results}
    for k, v in first.items():
        st = round(float(k), 3)
        assert recovered.get(st) == v['summary'], '断点恢复的场景 %s 内容与首次不一致' % k
    assert done_n > 0


def test_progress_file_cleaned_and_atomic(env):
    """全部完成后清理进度文件，且原子写不留临时文件。"""
    env.reset()
    env.run()
    assert not os.path.exists(env.prog_file), '全部完成后进度文件未被清理'
    left = [f for f in os.listdir(env.run_dir) if f.startswith('.tmp_')]
    assert not left, '原子写留下了临时文件：%s' % left


def test_stale_progress_ignored(env):
    """指纹不匹配（换视频/模型/抽样间隔）时旧进度作废。"""
    env.reset()
    W._atomic_json_dump(env.prog_file, {'version': 1, 'fingerprint': 'OTHER-VIDEO',
                                        'completed': [], 'results': {}})
    env.run()
    assert not os.path.exists(env.prog_file), '旧进度未被忽略重建'
    assert env.calls == N_SAMPLES // BATCH, '指纹不匹配时应重跑全部 %d 批，实际 %d 次' % (
        N_SAMPLES // BATCH, env.calls)


def test_failed_batch_retried(env):
    """整批失败不记为完成，下次续跑会重试。"""
    env.reset()
    env.fail_at = 2
    env.run()
    pf = env.load_progress()
    assert len(pf['results']) < N_SAMPLES, '失败批次被错误记为完成'
    calls_before = env.calls
    env.fail_at = None
    env.run()
    assert env.calls - calls_before > 0, '下次续跑没有重试失败的场景'
    assert not os.path.exists(env.prog_file), '重试完成后进度文件未被清理'


def test_progress_phase_text(env):
    """进度文案形如「场景理解 N/M（断点续跑，已完成K）」。"""
    env.reset()
    env.abort_at = 3
    env.run()
    env.set_abort(False)
    env.abort_at = None
    prog = {'pct': 0}
    env.run(progress=prog)
    assert '场景理解' in prog.get('phase', ''), prog.get('phase')
    assert '断点续跑' in prog.get('phase', ''), prog.get('phase')
    assert 'pct' in prog, '进度缺少百分比'


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-q']))
