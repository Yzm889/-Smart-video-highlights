# -*- coding: utf-8 -*-
"""ffmpeg_utils: ffmpeg 子进程封装 + 媒体时长探测 + 运行接线状态。
PROGRESS/RUN_PROCS/_PROC_LOCK/_TLS 的单一事实源，webui_server re-export。"""
import os, subprocess, threading, time, re


PROGRESS = {}          # runid -> mutable progress dict for the UI poller

RUN_PROCS = {}          # runid -> 当前活跃的 ffmpeg Popen（用于取消时终止）

_PROC_LOCK = threading.Lock()

_TLS = threading.local()   # 每个任务线程绑定自己的 runid，供 ffmpeg_run 读取

# 【P0-2 闪窗】Windows 下 GUI（pythonw/EXE 无控制台）启动控制台子进程时，
# 系统默认会给子进程新建一个控制台窗口 —— 主人实测「命令行窗口不断闪跳」。
# 一次出片要调几十次 ffmpeg（抽帧/场景探测/单段剪辑/合成/探测），于是黑框连成一片。
# CREATE_NO_WINDOW 让子进程不要窗口；非 Windows 上 getattr 取不到该常量 → 0（无副作用）。
# 项目里 webui_server.py:753 与 tts2_engines.py:96 早已这么写，只是没覆盖 ffmpeg 主路径。
_NO_WIN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

class AbortError(Exception):

    """任务被用户取消时抛出。"""

    pass

_FFMPEG_EXE = None

def ffmpeg_exe():
    global _FFMPEG_EXE
    if _FFMPEG_EXE is None:
        from imageio_ffmpeg import get_ffmpeg_exe
        _FFMPEG_EXE = get_ffmpeg_exe()
    return _FFMPEG_EXE

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
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=_NO_WIN)

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

                # 超出保留上限后必须继续排水（只丢弃、不再存）：
                # 若在这里 break 停止读取，ffmpeg 写 stderr 状态行会被管道缓冲卡死——
                # 实测 4K→720p 预览代理（长任务、无 on_progress）~5 分钟后整条编码
                # 停摆、输出 0 字节。内存上限不因继续读而失控：超限块直接丢弃。
                elif on_progress is None:

                    pass

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


# ---------------------------------------------------------------------------
# HDR 探测（v1.4.10）
# 【为什么需要】4K 片源大多是 HDR（PQ=smpte2084 / HLG=arib-std-b67）。之前的管线
# 直接把 10bit HDR 解成 8bit yuv420p 再编码，而 ffmpeg 会把输入的色彩标记
# （bt2020nc/bt2020/smpte2084）原样传到输出 —— 成品变成「贴了 HDR 标签却只有 8bit」：
# 识别 HDR 的播放器上偏暗发灰，8bit PQ 还容易出色带。实测整集压测成片即如此。
# 这里只负责「判断是不是 HDR」，具体 tonemap 滤镜链由 video_render 决定。
# 缓存独立于时长缓存：同一个文件的两个探测结果语义不同，共用缓存会串味。
_HDR_CACHE = {}
_HDR_CACHE_LOCK = threading.Lock()
_HDR_CACHE_MAX = 256


def _parse_is_hdr(err):

    """从 `ffmpeg -i` 的 stderr 判断视频流是否 HDR。

    只看 Video: 那一行——形如
      Video: hevc (Main 10), yuv420p10le(tv, bt2020nc/bt2020/smpte2084, progressive)
    PQ(smpte2084)/HLG(arib-std-b67) 才算 HDR；只有 bt2020 原色但传输是 bt709 的
    （极少见）按 SDR 处理，宁可不动画面也不乱 tonemap。"""

    txt = err.decode('utf-8', 'ignore') if isinstance(err, (bytes, bytearray)) else str(err or '')

    for line in txt.splitlines():

        if 'Video:' not in line:

            continue

        if 'smpte2084' in line or 'arib-std-b67' in line:

            return True

    return False


def probe_is_hdr(path):

    """片源是否 HDR。读不到/异常一律 False（按 SDR 走原路径，绝不因探测失败而中断出片）。"""

    key = _dur_cache_key(path)

    if key is None:

        return False

    with _HDR_CACHE_LOCK:

        if key in _HDR_CACHE:

            return _HDR_CACHE[key]

    try:

        val = _parse_is_hdr(ffmpeg_run(['-hide_banner', '-i', path])[2])

    except Exception:

        val = False

    with _HDR_CACHE_LOCK:

        if len(_HDR_CACHE) >= _HDR_CACHE_MAX:

            _HDR_CACHE.clear()

        _HDR_CACHE[key] = val

    return val


# tonemap 链是否可用：本机 ffmpeg 若缺 zscale，第一次失败后全局关掉，
# 后续所有片段直接走原路径（不能每段都白跑一次失败重试）。
TONEMAP_STATE = {'ok': True}


def tonemap_chain():

    """HDR(PQ/HLG) → SDR(bt709) 滤镜链（hable 映射）。

    两条视频管线共用：两遍管线的单段剪辑（webui_server.make_video_clip）
    与单遍合并的整段滤镜图（video_render）。zscale 需要 ffmpeg 带 zimg，
    imageio-ffmpeg 的 7.1 / 9.0 实测都支持。"""

    return ('zscale=t=linear:npl=100,format=gbrpf32le,'
            'zscale=p=bt709,tonemap=tonemap=hable:desat=0,'
            'zscale=t=bt709:m=bt709:r=tv,format=yuv420p')


# 输出侧色彩标记三件套：告诉播放器「这是普通 SDR」，避免继承源片的 PQ 标签
BT709_TAGS = ['-color_primaries', 'bt709', '-color_trc', 'bt709', '-colorspace', 'bt709']

_TM_PROBE = {'done': False}


def tonemap_available(path=None):
    """本机 ffmpeg 能不能对**这个片源**跑 tonemap 链。进程内只探一次。

    【为什么用真实片源探、不用 testsrc】实测 v7.1 对 `testsrc2` 合成源会报
    `Generic error in an external library`（RGB 源进 zscale 的必然问题），
    但对真实 HDR mkv 跑同一条链是成功的 —— 拿合成源探会误判成「不支持」，
    白白关掉调色。所以这里抽真实片源的一帧试跑。
    """
    if _TM_PROBE['done']:
        return TONEMAP_STATE['ok']
    if not path:
        return TONEMAP_STATE['ok']
    _TM_PROBE['done'] = True
    try:
        rc, _o, _e = ffmpeg_run(['-hide_banner', '-ss', '0', '-i', path,
                                 '-vf', tonemap_chain(), '-frames:v', '1',
                                 '-an', '-sn', '-f', 'null', '-'])
        ok = (rc == 0)
    except Exception:
        ok = False
    TONEMAP_STATE['ok'] = bool(ok)
    return TONEMAP_STATE['ok']


_SIZE_CACHE = {}
_SIZE_CACHE_LOCK = threading.Lock()
_SIZE_CACHE_MAX = 256


def _parse_size(err):
    """从 `ffmpeg -i` 的 stderr 里取视频分辨率。只认 Video: 那一行。"""
    txt = err.decode('utf-8', 'ignore') if isinstance(err, (bytes, bytearray)) else str(err or '')
    for line in txt.splitlines():
        if 'Video:' not in line:
            continue
        m = re.search(r'(\d{2,5})x(\d{2,5})', line)
        if m:
            return (int(m.group(1)), int(m.group(2)))
    return None


def probe_size(path):
    """(宽, 高)；读不到返回 None。按路径+mtime+size 缓存，与时长/HDR 缓存互不干扰。"""
    key = _dur_cache_key(path)
    if key is None:
        return None
    with _SIZE_CACHE_LOCK:
        if key in _SIZE_CACHE:
            return _SIZE_CACHE[key]
    try:
        val = _parse_size(ffmpeg_run(['-hide_banner', '-i', path])[2])
    except Exception:
        val = None
    with _SIZE_CACHE_LOCK:
        if len(_SIZE_CACHE) >= _SIZE_CACHE_MAX:
            _SIZE_CACHE.clear()
        _SIZE_CACHE[key] = val
    return val


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

_AUDIO_TRACK_CACHE = {}
_AUDIO_TRACK_LOCK = threading.Lock()

def _has_audio_track(p):
    """返回视频文件是否含音轨。结果按路径+mtime缓存，同一文件不重复启动 ffmpeg。"""
    try:
        key = (os.path.abspath(p), os.path.getmtime(p))
    except OSError:
        key = (os.path.abspath(p), 0)
    with _AUDIO_TRACK_LOCK:
        if key in _AUDIO_TRACK_CACHE:
            return _AUDIO_TRACK_CACHE[key]
    rc, o, e = ffmpeg_run(['-i', p])
    result = 'Audio:' in e.decode('utf-8', 'ignore')
    with _AUDIO_TRACK_LOCK:
        _AUDIO_TRACK_CACHE[key] = result
    return result

