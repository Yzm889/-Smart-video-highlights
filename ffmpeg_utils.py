# -*- coding: utf-8 -*-
"""ffmpeg_utils: ffmpeg 子进程封装 + 媒体时长探测 + 运行接线状态。
PROGRESS/RUN_PROCS/_PROC_LOCK/_TLS 的单一事实源，webui_server re-export。"""
import os, subprocess, threading, time, re


PROGRESS = {}          # runid -> mutable progress dict for the UI poller

RUN_PROCS = {}          # runid -> 当前活跃的 ffmpeg Popen（用于取消时终止）

_PROC_LOCK = threading.Lock()

_TLS = threading.local()   # 每个任务线程绑定自己的 runid，供 ffmpeg_run 读取

class AbortError(Exception):

    """任务被用户取消时抛出。"""

    pass

def ffmpeg_exe():

    from imageio_ffmpeg import get_ffmpeg_exe

    return get_ffmpeg_exe()

# ---------------------------------------------------------------------------

# 图片生成（复用 spring_video 的绘制逻辑）

# ---------------------------------------------------------------------------

def _parse_time_str(t):

    """'HH:MM:SS.cc' → 秒（float）；格式不符返回 None。"""

    try:

        hh, mm, ss = t.split(':')

        return int(hh) * 3600 + int(mm) * 60 + float(ss)

    except Exception:

        return None

FFMPEG_MAX_SECONDS = 4 * 3600     # 单次 ffmpeg 调用的整体上限（4 小时）

_ERR_CHUNK_MAX = 600               # stderr 最多保留的块数（每块 64KB），防长任务内存无限涨

def ffmpeg_run(args, input_data=None, on_progress=None, timeout=FFMPEG_MAX_SECONDS):

    """运行 ffmpeg。若当前任务线程绑定了 runid（见 _spawn），则把进程注册到

    RUN_PROCS，并每 0.3s 检查 PROGRESS[runid]['abort']；用户取消时立即终止进程

    并抛 AbortError，使整条流水线真正中断。

    timeout：整体时限（秒）。旧实现是 `while True: proc.wait(0.3)` 无累计上限——

    ffmpeg 一旦挂起（损坏输入 / NVENC 驱动卡死 / 输入流无 EOF），后台线程永久阻塞，

    进度永远不置 done（前端无限轮询），且 _TASK_SEM 名额永久泄漏，

    攒够并发上限后所有新任务都被拒，只能重启服务。

    on_progress(seconds_done)：可选回调——从 stderr 的 time= 统计行解析当前解码/编码

    位置（需命令不带 -nostats），供长视频把阶段进度做平滑推进。"""

    import re as _re

    exe = ffmpeg_exe()

    runid = getattr(_TLS, 'runid', None)

    proc = subprocess.Popen([exe] + args, stdin=subprocess.PIPE,

                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    if runid:

        with _PROC_LOCK:

            RUN_PROCS[runid] = proc

    err_chunks = []

    tail = ''

    deadline = (time.time() + float(timeout)) if timeout else None

    def _read_stderr():

        nonlocal tail

        try:

            for chunk in iter(lambda: proc.stderr.read(65536), b''):

                # 长视频逐帧警告可达数百 MB：只保留最后若干块，报错信息照样够用

                if len(err_chunks) < _ERR_CHUNK_MAX:

                    err_chunks.append(chunk)

                elif on_progress is None:

                    break

                if on_progress is not None:

                    tail = (tail + chunk.decode('utf-8', 'ignore'))[-2000:]

                    m = None

                    for m in _re.finditer(r'time=(\d+):(\d+):(\d+(?:\.\d+)?)', tail):

                        pass

                    if m:

                        sec = _parse_time_str('%s:%s:%s' % m.groups())

                        if sec is not None:

                            try:

                                on_progress(sec)

                            except Exception:

                                pass

        except Exception:

            pass

    def _write_stdin():

        try:

            if input_data is not None:

                if isinstance(input_data, (bytes, bytearray, memoryview)):

                    proc.stdin.write(input_data)

                else:

                    # 生成器/可迭代：逐块流式写入（batch5-5.3 图片逐帧编码）。

                    # 峰值内存从「全部帧常驻」降到「单帧」；写失败（进程已死/取消）

                    # 与 bytes 路径同样静默，靠 rc 非零上抛。

                    for chunk in input_data:

                        proc.stdin.write(chunk)

        except Exception:

            pass

        finally:

            try:

                proc.stdin.close()

            except Exception:

                pass

    t_err = threading.Thread(target=_read_stderr, daemon=True)

    t_in = threading.Thread(target=_write_stdin, daemon=True)

    t_err.start()

    t_in.start()

    rc, out, err = proc.returncode, b'', b''

    killed = False

    try:

        while True:

            try:

                rc = proc.wait(timeout=0.3)

                break

            except subprocess.TimeoutExpired:

                if deadline and time.time() > deadline:

                    # 挂死兜底：杀进程并抛错，让任务失败而不是永久卡住

                    try:

                        proc.kill()

                    except Exception:

                        pass

                    killed = True

                    break

                if runid and PROGRESS.get(runid, {}).get('abort'):

                    try:

                        proc.terminate()

                    except Exception:

                        pass

                    try:

                        proc.wait(timeout=2)

                    except Exception:

                        try:

                            proc.kill()

                        except Exception:

                            pass

                    killed = True

                    break

    finally:

        # 无论正常结束 / 取消 / 超时，都必须排空并关闭管道、回收子进程。

        # 旧实现在取消路径直接 raise，跳过下面这段，每次取消泄漏 2 个文件句柄 +

        # 2 个读线程，kill 之后不 wait 还会留下僵尸进程。

        try:

            out = proc.stdout.read()

        except Exception:

            pass

        t_err.join(timeout=2)

        t_in.join(timeout=2)

        err = b''.join(err_chunks)

        for stream in (proc.stdin, proc.stdout, proc.stderr):

            try:

                if stream:

                    stream.close()

            except Exception:

                pass

        try:

            proc.wait(timeout=5)      # 回收僵尸进程（kill 之后仍要 wait）

        except Exception:

            pass

        if runid:

            with _PROC_LOCK:

                RUN_PROCS.pop(runid, None)

    if killed:

        if runid and PROGRESS.get(runid, {}).get('abort'):

            raise AbortError('用户取消了任务')

        raise RuntimeError('ffmpeg 执行超时（超过 %d 秒，已终止）'

                           % int(timeout or FFMPEG_MAX_SECONDS))

    return rc, out, err

# --- 媒体时长探测 -----------------------------------------------------------

# 关键：取时长只应读容器头，绝不能加 `-f null -`——那会让 ffmpeg 把整片完整解码一遍。

# 实测 60s 1080p：0.12s（只读头） vs 1.77s（全片解码），且后者随片长线性增长。

# 切片循环会对同一源文件探测 N 次（见 _render_beatcut → make_video_clip），

# 故这里再叠一层按 (mtime_ns, size) 失效的缓存：源文件被替换时自动重新探测。

_DUR_CACHE = {}

_DUR_CACHE_LOCK = threading.Lock()

_DUR_CACHE_MAX = 512

def _dur_cache_key(path):

    try:

        st = os.stat(path)

    except OSError:

        return None

    return (os.path.abspath(path), st.st_mtime_ns, st.st_size)

def _probe_duration_cached(path, runner):

    key = _dur_cache_key(path)

    if key is None:            # 文件不存在：直接跑，让调用方拿到 None

        return runner(path)

    with _DUR_CACHE_LOCK:

        if key in _DUR_CACHE:

            return _DUR_CACHE[key]

    val = runner(path)

    with _DUR_CACHE_LOCK:

        if len(_DUR_CACHE) >= _DUR_CACHE_MAX:

            _DUR_CACHE.clear()

        _DUR_CACHE[key] = val

    return val

def _parse_duration(err):

    m = re.search(r'Duration:\s*(\d+):(\d+):([\d.]+)', err.decode('utf-8', 'ignore'))

    if not m:

        return None

    h, mm, s = int(m.group(1)), int(m.group(2)), float(m.group(3))

    return h * 3600 + mm * 60 + s

def probe_duration(path):

    """视频时长（秒）；不可读时返回 None。只读容器头，不解码。"""

    return _probe_duration_cached(path,

                                  lambda p: _parse_duration(ffmpeg_run(['-hide_banner', '-i', p])[2]))

def probe_audio_len(path):

    """Return audio duration in seconds using ffmpeg. 只读容器头，不解码。"""

    return _probe_duration_cached(path,

                                  lambda p: _parse_duration(ffmpeg_run(['-hide_banner', '-i', p])[2]))

def _concat_audio_clips(clip_paths, out_path):

    """用 ffmpeg concat 拼接多个音频文件为一个。成功返回 True。

    所有片段需同编码（同引擎输出即同编码）。最后清理临时片段文件。"""

    if not clip_paths:

        return False

    if len(clip_paths) == 1:

        # 单文件无需拼接，直接移动

        import shutil as _sh

        try:

            _sh.move(clip_paths[0], out_path)

            return True

        except Exception:

            return False

    concat_txt = out_path + '.subclauses.txt'

    try:

        with open(concat_txt, 'w', encoding='utf8') as f:

            for c in clip_paths:

                f.write("file '%s'\n" % os.path.abspath(c).replace("'", "'\\''"))

        rc, _o, _e = ffmpeg_run(['-y', '-f', 'concat', '-safe', '0', '-i', concat_txt,

                                  '-c', 'copy', out_path])

        return rc == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1000

    finally:

        if os.path.exists(concat_txt):

            try:

                os.unlink(concat_txt)

            except Exception:

                pass

        # 清理临时子句文件（无论成败）

        for c in clip_paths:

            if c and os.path.exists(c) and c != out_path:

                try:

                    os.unlink(c)

                except Exception:

                    pass

def _has_audio_track(p):

    """返回视频文件是否含音轨。"""

    rc, o, e = ffmpeg_run(['-i', p])

    return 'Audio:' in e.decode('utf-8', 'ignore')

