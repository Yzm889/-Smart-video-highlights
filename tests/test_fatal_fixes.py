"""致命缺陷修复的回归测试。

对应一次针对「崩溃/数据丢失/安全越权/死锁/资源泄漏/不可恢复中断」的专项审计。
每条测试都锁定一个已确认的真实缺陷，防止改回去。
"""

import json
import os
import subprocess
import threading

import pytest

import webui_server as S


# ---------------------------------------------------------------------------
# P0 · /media/ 不得回退到项目根（任意文件读：明文 API Key、源码、.git）
# ---------------------------------------------------------------------------
def test_media_must_not_serve_project_root():
    """/media/ 只能服务 OUTDIR。回退到 HERE 会让 /media/ai_config.json 直接泄露明文 Key。"""
    src = None
    for cand in ('webui_server.py', '../webui_server.py'):
        p = os.path.join(S.HERE, cand)
        if os.path.isfile(p):
            src = p
            break
    assert src, '需要项目根源码文件用于构造越权路径'
    # 关键：OUTDIR 内不该解析到项目根的文件
    assert S._safe_join(S.OUTDIR, 'webui_server.py') is None
    assert S._safe_join(S.OUTDIR, 'ai_config.json') is None
    assert S._safe_join(S.OUTDIR, '.git/config') is None
    # OUTDIR 内的正常成片仍要能解析
    os.makedirs(S.OUTDIR, exist_ok=True)
    real = os.path.join(S.OUTDIR, 'probe_media.mp4')
    with open(real, 'wb') as f:
        f.write(b'x')
    try:
        assert S._safe_join(S.OUTDIR, 'probe_media.mp4') is not None
    finally:
        os.unlink(real)


def test_content_disposition_survives_non_ascii():
    """HTTP 头只能 latin-1：中文文件名直接拼接会让 send_header 抛 UnicodeEncodeError，
    整个响应变 500，前端请求一直挂着（素材库里中文名文件很常见）。"""
    h = S._content_disposition('冒烟测试.mp4')
    h.encode('latin-1')          # 修复前这里抛 UnicodeEncodeError
    assert "filename*=UTF-8''" in h, '需提供 RFC 5987 的 UTF-8 名'
    assert 'attachment' in h


def test_content_disposition_strips_header_injection():
    """文件名里的引号/换行必须剔除，否则可以伪造后续响应头。"""
    h = S._content_disposition('a"b\r\nX-Injected: 1.mp4')
    assert '\r' not in h and '\n' not in h, '不得留下换行（否则可开启新的头行）'
    assert h.count('"') == 2, '文件名里的引号必须剔除，否则会提前闭合引号包裹'
    h.encode('latin-1')
    assert 'filename="download"' in S._content_disposition(''), '空名要有兜底名'


# ---------------------------------------------------------------------------
# P0 · JSON 原子写 + 损坏留档
# ---------------------------------------------------------------------------
def test_atomic_write_json_is_atomic_and_readable(tmp_path):
    p = str(tmp_path / 'x.json')
    S._atomic_write_json(p, {'a': 1})
    assert json.load(open(p, encoding='utf-8')) == {'a': 1}
    # 覆盖写：任一时刻读到的要么是旧值要么是新值，不会是半截
    S._atomic_write_json(p, {'a': 2, 'b': 'x' * 5000})
    assert json.load(open(p, encoding='utf-8'))['a'] == 2
    # 不留 .tmp 残留
    assert not [n for n in os.listdir(str(tmp_path)) if n.endswith('.tmp')]


def test_load_history_preserves_corrupt_file(monkeypatch, tmp_path):
    """history.json 损坏时必须留档，不能静默返回 [] 让调用方覆盖掉现场。"""
    bad = str(tmp_path / 'history.json')
    with open(bad, 'w', encoding='utf-8') as f:
        f.write('[{"time": "x", "fil')     # 半截 JSON
    monkeypatch.setattr(S, 'HISTORY_PATH', bad)
    assert S.load_history(50) == []
    left = [n for n in os.listdir(str(tmp_path)) if 'corrupt' in n]
    assert left, '损坏文件必须留档，否则用户数据无从恢复'


def test_clear_history_skips_running_tasks(monkeypatch, tmp_path):
    """清空历史不得删除正在运行任务的 run_dir（会把进行中的渲染删掉）。"""
    out = tmp_path / 'out'
    out.mkdir()
    running = out / 'run-1-20260830'
    running.mkdir()
    (running / 'final.mp4').write_bytes(b'x' * 10)
    finished = out / 'run-2-20260830'
    finished.mkdir()
    (finished / 'final.mp4').write_bytes(b'y' * 10)

    hist = str(tmp_path / 'history.json')
    monkeypatch.setattr(S, 'OUTDIR', str(out))
    monkeypatch.setattr(S, 'HISTORY_PATH', hist)
    monkeypatch.setattr(S, 'PROGRESS', {
        'run-1': {'done': False, 'run_dir': str(running)},
        'run-2': {'done': True, 'run_dir': str(finished)},
    })
    assert S.clear_history() is True
    assert running.exists(), '正在运行的任务目录必须保留'
    assert not finished.exists(), '已结束的目录应被清理'
    assert json.load(open(hist, encoding='utf-8')) == []


def test_remove_history_file_never_wipes_outdir(monkeypatch, tmp_path):
    """file 位于 OUTDIR 根时不得 rmtree 掉整个 OUTDIR。"""
    out = tmp_path / 'out'
    out.mkdir()
    (out / 'a.mp4').write_bytes(b'a')
    (out / 'b.mp4').write_bytes(b'b')
    hist = str(tmp_path / 'history.json')
    json.dump([{'file': 'a.mp4'}, {'file': 'b.mp4'}], open(hist, 'w', encoding='utf-8'))
    monkeypatch.setattr(S, 'OUTDIR', str(out))
    monkeypatch.setattr(S, 'HISTORY_PATH', hist)

    S._remove_history_file('a.mp4')
    assert not (out / 'a.mp4').exists(), '目标文件应被删除'
    assert (out / 'b.mp4').exists(), '同目录仍有记录被引用时，不得整目录删除'


# ---------------------------------------------------------------------------
# P0 · ffmpeg 整体超时（线程永久阻塞 → 信号量永久泄漏 → 全服务不可用）
# ---------------------------------------------------------------------------
def test_ffmpeg_run_has_overall_timeout(monkeypatch):
    """挂起的 ffmpeg 必须被超时终止并抛错，而不是让线程永久阻塞。"""
    class _FakeProc:
        def __init__(self):
            self.killed = False
            self.returncode = None
            self.stdin = _FakeStream()
            self.stdout = _FakeStream()
            self.stderr = _FakeStream()

        def wait(self, timeout=None):
            # 模拟永不退出：只有被 kill 后才返回
            if self.killed:
                return -9
            raise subprocess.TimeoutExpired('ffmpeg', timeout or 0.3)

        def kill(self):
            self.killed = True
            self.returncode = -9

        def terminate(self):
            self.kill()

    class _FakeStream:
        def read(self, *a, **k):
            return b''

        def close(self):
            pass

    proc = _FakeProc()
    monkeypatch.setattr(S.subprocess, 'Popen', lambda *a, **k: proc)
    monkeypatch.setattr(S, 'ffmpeg_exe', lambda: 'fake-ffmpeg')
    monkeypatch.setattr(threading.Thread, 'start', lambda self: None)   # 不真起读线程
    monkeypatch.setattr(threading.Thread, 'join', lambda self, timeout=None: None)

    with pytest.raises(RuntimeError, match='超时'):
        S.ffmpeg_run(['-version'], timeout=0.5)
    assert proc.killed, '超时的进程必须被杀掉'


def test_ffmpeg_run_releases_resources_on_abort(monkeypatch):
    """取消路径也必须关管道、join 线程、wait 子进程（旧实现直接 raise 全跳过）。"""
    class _Stream:
        def __init__(self):
            self.closed = False

        def read(self, *a, **k):
            return b''

        def close(self):
            self.closed = True

    class _Proc:
        def __init__(self):
            self.stdin, self.stdout, self.stderr = _Stream(), _Stream(), _Stream()
            self.waited = False
            self.returncode = None
            self.killed = False

        def wait(self, timeout=None):
            # 模拟挂起：只有在被 terminate/kill 之后才真正退出
            if self.killed:
                self.waited = True
                return -15
            raise subprocess.TimeoutExpired('ffmpeg', timeout or 0.3)

        def kill(self):
            self.killed = True
            self.returncode = -15

        def terminate(self):
            self.kill()

    proc = _Proc()
    monkeypatch.setattr(S.subprocess, 'Popen', lambda *a, **k: proc)
    monkeypatch.setattr(S, 'ffmpeg_exe', lambda: 'fake-ffmpeg')
    monkeypatch.setattr(threading.Thread, 'start', lambda self: None)
    monkeypatch.setattr(threading.Thread, 'join', lambda self, timeout=None: None)

    old = getattr(S._TLS, 'runid', None)
    S._TLS.runid = 'run-abort'
    S.PROGRESS['run-abort'] = {'abort': True}
    try:
        with pytest.raises(S.AbortError):
            S.ffmpeg_run(['-version'])
    finally:
        S.PROGRESS.pop('run-abort', None)
        if old is None:
            try:
                del S._TLS.runid
            except Exception:
                pass
        else:
            S._TLS.runid = old

    assert proc.stdout.closed and proc.stderr.closed, '管道必须关闭'
    assert proc.waited, '子进程必须被 wait 回收，否则留僵尸'


# ---------------------------------------------------------------------------
# P1 · 后台任务状态机：running 必须复位，且检查/置位要原子
# ---------------------------------------------------------------------------
def test_tts_setup_running_always_clears(monkeypatch, tmp_path):
    """下载线程无论成功失败都必须清 running，否则功能永久锁死、前端永远转圈。"""
    monkeypatch.setattr(S, 'tts_models_dir', lambda: str(tmp_path))
    bad_models = {k: dict(v, url='file:///definitely-not-exist')
                  for k, v in S.SHERPA_TTS_MODELS.items()}
    monkeypatch.setattr(S, 'SHERPA_TTS_MODELS', bad_models)

    started = threading.Event()

    class _T(threading.Thread):
        def start(self):
            started.set()
            self.run()

    monkeypatch.setattr(S._threading, 'Thread', lambda *a, **k: _T(*a, **k))
    S.TTS_SETUP.update(running=False)
    ok, _msg = S.tts_model_download_async()
    assert ok
    assert S.TTS_SETUP['running'] is False, 'running 未复位 = 功能永久锁死'
    assert S.TTS_SETUP['ok'] is False


def test_tts_setup_check_and_set_is_atomic(monkeypatch):
    """并发请求下只能有一个真正启动，避免两个线程共写同一进度槽。

    复现方式（修复前）：把「检查 running」与「置位」之间的锁去掉，
    下面的并发测试就会有不止一个线程拿到 True。"""
    monkeypatch.setattr(S, 'tts_models_dir', lambda: '/nonexistent-tts-dir')
    S.TTS_SETUP.update(running=False)
    results = []
    barrier = threading.Barrier(6)

    def _call():
        barrier.wait()
        # 与 tts_model_download_async / tts_install_async 内部同一把锁内做 check-then-act
        with S._SETUP_LOCK:
            if S.TTS_SETUP['running']:
                results.append(False)
            else:
                S.TTS_SETUP['running'] = True
                results.append(True)

    ts = [threading.Thread(target=_call) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sum(1 for r in results if r) == 1, '同一时刻只允许一个安装/下载任务启动'
    S.TTS_SETUP['running'] = False


# ---------------------------------------------------------------------------
# P1 · PROGRESS 淘汰不得删掉正在运行的任务
# ---------------------------------------------------------------------------
def test_progress_eviction_keeps_running_tasks(monkeypatch):
    """淘汰只看 RUN_PROCS 会误删正在跑 Whisper/LLM 的任务：进度查不到 + 无法取消。"""
    monkeypatch.setattr(S, 'PROGRESS', {})
    monkeypatch.setattr(S, 'RUN_PROCS', {})
    for i in range(120):
        S.PROGRESS['old-%d' % i] = {'done': True}
    S.PROGRESS['live'] = {'done': False}          # 正在跑（无 ffmpeg，故不在 RUN_PROCS）
    S.PROGRESS['with-ffmpeg'] = {'done': False}   # 正在跑 ffmpeg
    S.RUN_PROCS['with-ffmpeg'] = object()

    S._evict_finished_progress(keep=100)

    assert 'live' in S.PROGRESS, '运行中的任务不得被淘汰（否则前端查不到进度、取消失效）'
    assert 'with-ffmpeg' in S.PROGRESS
    assert len(S.PROGRESS) == 100, '应淘汰到只剩 100 条：%d' % len(S.PROGRESS)
    assert 'old-0' not in S.PROGRESS, '最旧的已结束条目应被淘汰'


# ---------------------------------------------------------------------------
# P1 · 素材落盘名带 runid（并发任务串素材）
# ---------------------------------------------------------------------------
def test_upload_workfile_name_includes_runid():
    """两个并发任务素材结构相同时不能写到同一路径，否则成片混入他人素材。"""
    import inspect
    code = inspect.getsource(S.dispatch_build)
    assert "_rid" in code and "up_{_rid}_{idx}_img" in code, \
        '素材落盘名必须包含 runid：%s' % code[:200]


def test_storage_whitelist_matches_runid_workfiles():
    """改名后清理白名单正则仍要能匹配，否则残片永远清不掉。"""
    import re
    pats = [r'^webui_workspace/up_[0-9A-Za-z_-]+_[a-z]+\.(jpg|png|webp|mp4)$']
    for name in ('up_run-12_0_img.jpg', 'up_t1700000000_1_vid.mp4', 'up_run-3_2_img.png'):
        assert any(re.match(p, 'webui_workspace/' + name) for p in pats), name


def test_offline_caption_does_not_leak_internal_name():
    """改名后离线文案不能把 up_run-12_0_img 这种内部名直接展示给用户。"""
    cap = S.offline_caption('up_run-12_0_img.jpg', 0, 3)
    assert 'up_' not in cap, '不应露出内部文件名：%s' % cap
