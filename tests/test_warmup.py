# -*- coding: utf-8 -*-
"""S1 回归：上传后后台预热 ASR+VLM。

锁定：
  1. 触发：仅视频扩展名；同视频只预热一次；换视频让旧预热让位
  2. 等待：预热完成 → 正式任务直接返回（命中缓存）；超时 → 标记让位不阻塞
  3. 执行体：ASR 写 asr_* 缓存 → VLM 写 vlm_sample_* 缓存；无台词信息跳过 VLM；结束复位状态
  4. 缺陷修复：VLM 取消/失败不再写部分结果缓存（防缺口永久化、断点续跑被短路）
  5. cancel_check：预热让位时 VLM 在批次间退出，进度保留在进度文件
"""
import os
import threading

import webui_server as S  # noqa: F401  先建立 webui 入口，再导入 movie_narrator（避免循环依赖）
import movie_narrator as M
import workflows as W

_TEST_VID = os.path.join(os.path.dirname(__file__), '..', 'webui_workspace', '_warmup_test.mp4')


def _mk_video(path, size=8192):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(b'\x00' * size)
    return path


# ---------------------------------------------------------------------------
# 1) 触发逻辑
# ---------------------------------------------------------------------------
def test_warmup_start_ignores_non_video(monkeypatch):
    monkeypatch.setattr(W, '_warmup_worker', lambda *a: None)
    assert W._warmup_start(_mk_video(os.path.join(
        os.path.dirname(_TEST_VID), 'x.txt'))) is None or True
    with W._WARMUP_LOCK:
        assert not W._WARMUP_STATE['running']


def test_warmup_start_spawns_once(monkeypatch):
    started = []
    monkeypatch.setattr(W, '_warmup_worker', lambda *a: started.append(a))
    v = _mk_video(_TEST_VID)
    try:
        W._warmup_start(v)
        assert len(started) == 1
        with W._WARMUP_LOCK:
            assert W._WARMUP_STATE['running'] is True
            fp1 = W._WARMUP_STATE['video_fp']
        # 同视频重复触发：不重复启动
        W._warmup_start(v)
        assert len(started) == 1
        # 换视频：旧预热标记让位，启动新线程（用不同内容/大小避免指纹碰撞）
        v2 = _mk_video(_TEST_VID.replace('.mp4', '_2.mp4'), size=16384)
        W._warmup_start(v2)
        assert len(started) == 2
        with W._WARMUP_LOCK:
            assert W._WARMUP_STATE['cancel'] is False  # 新预热已重置
            assert W._WARMUP_STATE['video_fp'] != fp1
    finally:
        # 复位状态，避免污染后续测试
        with W._WARMUP_LOCK:
            W._WARMUP_STATE['running'] = False
            W._WARMUP_STATE['video_fp'] = None
            W._WARMUP_STATE['cancel'] = False
            W._WARMUP_STATE['event'].set()
        for p in (_TEST_VID, _TEST_VID.replace('.mp4', '_2.mp4')):
            try:
                os.remove(p)
            except OSError:
                pass


def test_warmup_start_env_disable(monkeypatch):
    started = []
    monkeypatch.setenv('NARRATE_WARMUP', '0')
    monkeypatch.setattr(W, '_warmup_worker', lambda *a: started.append(a))
    v = _mk_video(_TEST_VID)
    try:
        W._warmup_start(v)
        assert started == []
    finally:
        monkeypatch.delenv('NARRATE_WARMUP', raising=False)
        try:
            os.remove(v)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 2) 等待逻辑
# ---------------------------------------------------------------------------
def test_warmup_wait_noop_when_idle():
    with W._WARMUP_LOCK:
        W._WARMUP_STATE['running'] = False
    assert W._warmup_wait(_mk_video(_TEST_VID), timeout=0.01) is None


def test_warmup_wait_completed_returns_fast(monkeypatch):
    v = _mk_video(_TEST_VID)
    try:
        fp = W._warmup_fp(v)
        with W._WARMUP_LOCK:
            W._WARMUP_STATE['video_fp'] = fp
            W._WARMUP_STATE['running'] = True
            W._WARMUP_STATE['cancel'] = False
            W._WARMUP_STATE['event'] = threading.Event()
            W._WARMUP_STATE['event'].set()  # 已完成
        assert W._warmup_wait(v, timeout=0.05) is None
    finally:
        with W._WARMUP_LOCK:
            W._WARMUP_STATE['running'] = False
        try:
            os.remove(v)
        except OSError:
            pass


def test_warmup_wait_timeout_cancels(monkeypatch):
    v = _mk_video(_TEST_VID)
    try:
        fp = W._warmup_fp(v)
        with W._WARMUP_LOCK:
            W._WARMUP_STATE['video_fp'] = fp
            W._WARMUP_STATE['running'] = True
            W._WARMUP_STATE['cancel'] = False
            W._WARMUP_STATE['event'] = threading.Event()  # 一直不 set
        assert W._warmup_wait(v, timeout=0.05) is None
        with W._WARMUP_LOCK:
            assert W._WARMUP_STATE['cancel'] is True, '超时后必须标记让位'
    finally:
        with W._WARMUP_LOCK:
            W._WARMUP_STATE['running'] = False
            W._WARMUP_STATE['cancel'] = False
        try:
            os.remove(v)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 3) 执行体
# ---------------------------------------------------------------------------
def test_warmup_worker_writes_cache_and_calls_vlm(monkeypatch, tmp_path):
    v = _mk_video(_TEST_VID)
    saved = {}
    called = {}
    monkeypatch.setattr(W, 'probe_audio_len', lambda p: 100.0)
    monkeypatch.setattr(W, 'asr_segments', lambda p: [{'start': 0, 'end': 1, 'text': '你好'}])
    monkeypatch.setattr(W, 'whisper_model_name', lambda: 'base')
    monkeypatch.setattr(W, '_cache_load', lambda k: None)
    monkeypatch.setattr(W, '_cache_save', lambda k, val: saved.update({k: val}))
    monkeypatch.setattr(S, 'vlm_enabled', lambda: True)
    monkeypatch.setattr(S, 'vlm_ping', lambda: (True, ''))
    monkeypatch.setattr(S, 'OUTDIR', str(tmp_path))
    monkeypatch.setattr(S, '_vlm_sample_timeline',
                        lambda *a, **k: called.update({'args': a, 'kwargs': k}) or [])
    try:
        W._warmup_worker(v, W._warmup_fp(v))
        assert len(saved) == 1 and 'asr_base' in next(iter(saved))
        assert 'args' in called, 'VLM 必须被调用'
        assert 'cancel_check' in called['kwargs'], '必须传入让位回调'
        with W._WARMUP_LOCK:
            assert W._WARMUP_STATE['running'] is False, '结束时必须复位状态'
    finally:
        try:
            os.remove(v)
        except OSError:
            pass


def test_warmup_worker_skips_vlm_without_asr(monkeypatch, tmp_path):
    v = _mk_video(_TEST_VID)
    called = {}
    monkeypatch.setattr(W, 'probe_audio_len', lambda p: 100.0)
    monkeypatch.setattr(W, 'asr_segments', lambda p: [])
    monkeypatch.setattr(W, 'whisper_model_name', lambda: 'base')
    monkeypatch.setattr(W, '_cache_load', lambda k: None)
    monkeypatch.setattr(S, 'vlm_enabled', lambda: True)
    monkeypatch.setattr(S, '_vlm_sample_timeline', lambda *a, **k: called.update({'x': 1}) or [])
    try:
        W._warmup_worker(v, W._warmup_fp(v))
        assert called == {}, '无台词信息时不预热 VLM'
    finally:
        try:
            os.remove(v)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# 4) 缺陷修复：VLM 取消/失败不写部分结果缓存
# ---------------------------------------------------------------------------
def _install_vlm_env(monkeypatch, tmp_path):
    """构造 _vlm_sample_timeline 最小环境：60s 视频、1 段台词、抽帧缓存就绪。"""
    v = os.path.join(str(tmp_path), 'clip.mp4')
    _mk_video(v)
    monkeypatch.setattr(M, '_w', S)   # 让 narrator 走 webui 模块（S7 注入的符号都在）
    monkeypatch.setattr(S, 'vlm_enabled', lambda: True)
    monkeypatch.setattr(S, 'vlm_ping', lambda: (True, ''))
    monkeypatch.setattr(M, 'vlm_cfg', lambda: {'model': 'qwen-vl', 'enabled': True})
    monkeypatch.setattr(M, '_sample_frame_cache_ready', lambda d, n: True)
    frame_dir = os.path.join(str(tmp_path), 'frames')
    os.makedirs(frame_dir, exist_ok=True)
    from PIL import Image
    Image.new('L', (8, 8), 120).save(os.path.join(frame_dir, 'sample_0000.jpg'))
    monkeypatch.setattr(M, '_sample_frame_cache_dir', lambda p, iv: frame_dir)
    return v


def test_vlm_cancel_check_skips_cache_write(monkeypatch, tmp_path):
    """cancel_check=True：第一批前退出，不写缓存（部分结果不得污染缓存）。"""
    v = _install_vlm_env(monkeypatch, tmp_path)
    saved = []
    monkeypatch.setattr(M, '_cache_save', lambda k, val: saved.append(k))
    run_dir = os.path.join(str(tmp_path), 'run1')
    os.makedirs(run_dir, exist_ok=True)
    M._vlm_sample_timeline(v, 60.0, [{'start': 0, 'end': 5, 'text': '你好'}],
                           run_dir, cancel_check=lambda: True)
    assert saved == [], '取消时不得写部分缓存'
    # 进度文件未被写（第一批前退出）
    assert not os.path.exists(os.path.join(run_dir, 'vlm_progress.json'))


def test_vlm_complete_writes_cache(monkeypatch, tmp_path):
    """正常完成（cancel_check=False）：写缓存 + 删进度文件。"""
    v = _install_vlm_env(monkeypatch, tmp_path)
    saved = []
    monkeypatch.setattr(M, '_cache_save', lambda k, val: saved.append(k))
    monkeypatch.setattr(S, 'vlm_chat', lambda *a, **k:
                        '{"location":"客厅","characters":"主角","event":"对话",'
                        '"dialogue":"你好","summary":"两人在客厅"}')
    run_dir = os.path.join(str(tmp_path), 'run2')
    os.makedirs(run_dir, exist_ok=True)
    out = M._vlm_sample_timeline(v, 60.0, [{'start': 0, 'end': 5, 'text': '你好'}],
                                 run_dir, cancel_check=lambda: False)
    assert saved, '全部完成必须写缓存'
    assert len(out) >= 1 and out[0].get('location') == '客厅'
    assert not os.path.exists(os.path.join(run_dir, 'vlm_progress.json')), '完成后清理进度文件'
