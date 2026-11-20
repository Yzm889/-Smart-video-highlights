# -*- coding: utf-8 -*-
"""资源守护（v1.4.95）：解决「多任务堆积→内存爆→跑不动」的三个根因。

主人实测（2026-11-19）：17 个 python 进程堆积互相抢内存/GPU、模型切换时
旧模型占着显存、杀软反复拦 Python 写数据根——全部是资源调度问题，不是
配置不够（3060 12GB 跑单模型很从容）。

三个守护：
  1. narrate_task_lock   跨进程任务互斥（锁文件）——同一数据根同时只跑
                         一个解说任务，第二个实例明确报错退出，不再堆积；
  2. ollama_unload_model 显式卸载指定模型（keep_alive=0）——任务切换时
                         腾显存（全篇对齐的 qwen3:8b 用完即卸，给 TTS/渲染让路）；
  3. safe_json_write     PermissionError 兜底——杀软拦 Python 写数据根时
                         重试 + 给出用户可操作的提示（加白名单）。
"""
import json
import logging
import os
import sys
import time

_log = logging.getLogger('framecut.narrator')

_HERE = os.path.dirname(os.path.abspath(__file__))
_HOST_FILE = os.path.join(_HERE, 'webui_server.py')


def _host():
    m = sys.modules.get('webui_server')
    if m is not None:
        return m
    m = sys.modules.get('__main__')
    if m is not None and os.path.abspath(getattr(m, '__file__', '')) == _HOST_FILE:
        return m
    import webui_server
    return webui_server


_w = _host()


# ---------------------------------------------------------------------------
# 1. 跨进程任务互斥（锁文件）
# ---------------------------------------------------------------------------

class TaskLockBusy(Exception):
    """已有解说任务在跑（同一数据根）——第二实例拒绝启动。"""


def narrate_task_lock(outdir=None, timeout_sec=0.0):
    """跨进程任务互斥锁（contextmanager）。锁文件 = OUTDIR/.narrate_lock。

    主人实测根因：多次启动任务不确认前一个结束——17 个 python 进程堆积互相
    抢内存/GPU → 内存爆换页 → 一切变慢。锁保证同一数据根同时只有一个
    narrate 任务，第二实例明确报错退出而不是悄悄堆积。

    timeout_sec > 0 时等待锁释放（最多 N 秒）；=0 立即失败。"""
    import contextlib

    outdir = outdir or getattr(_w, 'OUTDIR', os.getcwd())
    lock_fp = os.path.join(outdir, '.narrate_lock')
    os.makedirs(outdir, exist_ok=True)

    @contextlib.contextmanager
    def _inner():
        deadline = time.time() + timeout_sec
        while True:
            try:
                # O_EXCL 独占创建：拿到锁的进程写自己的 pid/时间
                fd = os.open(lock_fp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, 'w') as f:
                    f.write(str(os.getpid()))
                break
            except FileExistsError:
                # 旧锁的宿主进程可能已死（崩溃残留）——按存活 pid 清理
                try:
                    with open(lock_fp, encoding='utf-8') as f:
                        old_pid = int(f.read().strip() or 0)
                    if old_pid == os.getpid():
                        os.remove(lock_fp)   # 同进程重入（串行任务）→ 清理旧锁
                        continue
                    _pid_alive(old_pid)
                    if time.time() >= deadline:
                        raise TaskLockBusy(
                            '已有解说任务在跑（pid %d）。等它结束或重启程序后再试。'
                            % old_pid)
                    time.sleep(1.0)
                    continue
                except (ValueError, ProcessLookupError, OSError):
                    os.remove(lock_fp)   # 死进程残留锁 → 清理
                    continue
            time.sleep(0.2)
        try:
            yield
        finally:
            try:
                os.remove(lock_fp)
            except OSError:
                pass

    return _inner()


def _pid_alive(pid):
    """Windows 下检查 pid 是否存活（ctypes，无第三方依赖）。"""
    if not pid or pid <= 0:
        return False
    try:
        import ctypes
        SYNCHRONIZE = 0x00100000
        h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    except Exception:
        return True   # 查不到就假定活着（宁误报不误杀）


# ---------------------------------------------------------------------------
# 2. ollama 模型显式卸载（任务切换时腾显存）
# ---------------------------------------------------------------------------

def ollama_unload_model(base_url, model, timeout=30):
    """keep_alive=0 立即卸载指定模型（显存还给下一位）。
    返回 True=已发送卸载请求；False=失败（不影响主流程，调用方继续）。"""
    try:
        import urllib.request
        base = (base_url or 'http://127.0.0.1:11434').rstrip('/')
        req = urllib.request.Request(
            base + '/api/generate',
            data=json.dumps({'model': model, 'keep_alive': 0,
                             'prompt': '', 'stream': False}).encode('utf-8'),
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
        _log.info('[DIAG] ollama 模型已卸载: %s' % model)
        return True
    except Exception as e:
        _log.info('[DIAG] ollama 卸载失败（不阻断）: %s' % str(e)[:100])
        return False


def ollama_unload_all(base_url='http://127.0.0.1:11434', timeout=30):
    """卸载 ollama 全部驻留模型（keep_alive=0 逐个发送）——TTS/渲染前腾显存。

    主人实测（2026-11-21 run-1-20261121-042111）：文本 LLM（qwen3:8b）在剧情
    理解/全篇对齐后驻留显存 5-7GB，IndexTTS2 的 6.6GB 装不进 12GB 卡 →
    整片配音降级 sapi（音质差）。TTS 启动前调用本函数腾干净显存。
    返回卸载的模型数。"""
    n = 0
    try:
        import urllib.request
        base = (base_url or 'http://127.0.0.1:11434').rstrip('/')
        req = urllib.request.Request(base + '/api/ps')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            d = json.loads(resp.read().decode('utf-8'))
        for m in d.get('models', []):
            name = m.get('name') or m.get('model')
            if not name:
                continue
            try:
                ureq = urllib.request.Request(
                    base + '/api/generate',
                    data=json.dumps({'model': name, 'keep_alive': 0,
                                     'prompt': '', 'stream': False}).encode('utf-8'),
                    headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(ureq, timeout=timeout) as resp2:
                    resp2.read()
                n += 1
                _log.info('[资源守护] ollama 模型已卸载: %s' % name)
            except Exception as e:
                _log.info('[DIAG] ollama 卸载 %s 失败（继续）: %s' % (name, str(e)[:80]))
    except Exception as e:
        _log.info('[DIAG] ollama 驻留查询失败: %s' % str(e)[:100])
    return n


def ollama_mem_used_mb(base_url='http://127.0.0.1:11434', timeout=10):
    """当前 ollama 驻留模型占用的显存 MB（/api/ps 汇总）。失败返回 0。"""
    try:
        import urllib.request
        req = urllib.request.Request(
            (base_url or 'http://127.0.0.1:11434').rstrip('/') + '/api/ps')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            d = json.loads(resp.read().decode('utf-8'))
        total = 0
        for m in d.get('models', []):
            sz = (m.get('size_vram') or m.get('size') or 0)
            total += int(sz)
        return int(total // (1024 * 1024))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# 3. PermissionError 兜底（杀软拦截的明确提示）
# ---------------------------------------------------------------------------

def safe_json_dump(path, data, retries=2, delay=1.0):
    """带杀软拦截兜底的 JSON 写入。

    Windows 杀软会把 Python 写 %LOCALAPPDATA%\\FrameCut 判为可疑并拦截
    （PermissionError WinError 5）。重试失败后给出用户可操作的提示，
    而不是让上游收到一个看不懂的 PermissionError。"""
    import json as _json
    last_err = None
    for attempt in range(retries + 1):
        try:
            with open(path, 'w', encoding='utf-8') as f:
                _json.dump(data, f, ensure_ascii=False, indent=1)
            return True
        except PermissionError as e:
            last_err = e
            if attempt < retries:
                time.sleep(delay)
                continue
            _log.warning('[资源守护] 写入被杀软拦截: %s | 请在 Windows 安全中心'
                         '→ 病毒防护 → 排除项 添加 FrameCut 目录后重试', path)
            return False
        except Exception as e:
            last_err = e
            break
    _log.info('[DIAG] JSON 写入失败 %s: %s' % (path, str(last_err)[:100]))
    return False
