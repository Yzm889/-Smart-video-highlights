# -*- coding: utf-8 -*-
"""cache_utils: 分析/抽帧磁盘缓存工具。
依赖常量（WORKDIR/ANALYSIS_CACHE_*/SAMPLE_FRAME_CACHE_*）随本模块迁移。"""
import os, json, time, shutil

HERE = os.path.dirname(os.path.abspath(__file__))

WORKDIR = os.path.join(HERE, 'webui_workspace')

ANALYSIS_VERSION = 1        # 分析逻辑变更时 +1，旧缓存自动全部失效

ANALYSIS_CACHE_DIR = os.path.join(WORKDIR, 'analysis_cache')

ANALYSIS_CACHE_KEEP = 200   # 最多保留条数，超出按修改时间清最旧

def _file_fp(path, min_size=4096):

    """缓存键用的文件指纹 'size:mtime'。文件不存在或小于 min_size（测试假文件/

    异常输入）返回空串，空指纹一律直连实时分析，杜绝把 mock 数据写进缓存。"""

    try:

        st = os.stat(path)

    except OSError:

        return ''

    if st.st_size < min_size:

        return ''

    return f'{st.st_size}:{int(st.st_mtime)}'

def _analysis_cache_path(key):

    import hashlib

    return os.path.join(ANALYSIS_CACHE_DIR, hashlib.md5(key.encode('utf-8')).hexdigest() + '.json')

def _analysis_cache_load(key):

    try:

        p = _analysis_cache_path(key)

        if os.path.exists(p):

            with open(p, 'r', encoding='utf-8') as f:

                data = json.load(f)

            if isinstance(data, dict) and data.get('key') == key:

                return data.get('value')

    except Exception:

        pass

    return None

def _analysis_cache_save(key, value):

    try:

        os.makedirs(ANALYSIS_CACHE_DIR, exist_ok=True)

        p = _analysis_cache_path(key)

        tmp = p + '.tmp'

        with open(tmp, 'w', encoding='utf-8') as f:

            json.dump({'key': key, 'value': value}, f, ensure_ascii=False)

        os.replace(tmp, p)   # 原子替换：避免 Windows 下并发读到半写文件

        _analysis_cache_trim()

    except Exception:

        pass

def _video_cache_key(video_path, suffix=''):

    """基于视频文件大小+mtime+前1MB哈希生成缓存key，同文件重跑命中缓存。"""

    import hashlib

    try:

        st = os.stat(video_path)

        h = hashlib.md5()

        h.update(str(st.st_size).encode())

        h.update(str(int(st.st_mtime)).encode())

        try:

            with open(video_path, 'rb') as f:

                h.update(f.read(1024 * 1024))

        except Exception:

            pass

        return h.hexdigest() + '_' + suffix

    except Exception:

        return os.path.basename(video_path) + '_' + suffix

def _cache_load(key):

    """通用缓存读取（复用analysis缓存基础设施）。"""

    return _analysis_cache_load(key)

def _cache_save(key, value):

    """通用缓存写入（复用analysis缓存基础设施）。"""

    _analysis_cache_save(key, value)

def _analysis_cache_trim():

    """缓存条数超过 ANALYSIS_CACHE_KEEP 时按修改时间清最旧。"""

    try:

        if not os.path.isdir(ANALYSIS_CACHE_DIR):

            return

        entries = []

        for fn in os.listdir(ANALYSIS_CACHE_DIR):

            if not fn.endswith('.json'):

                continue

            p = os.path.join(ANALYSIS_CACHE_DIR, fn)

            try:

                entries.append((os.path.getmtime(p), p))

            except OSError:

                pass

        if len(entries) > ANALYSIS_CACHE_KEEP:

            entries.sort()

            for _, p in entries[:len(entries) - ANALYSIS_CACHE_KEEP]:

                try:

                    os.remove(p)

                except OSError:

                    pass

    except Exception:

        pass

# ---------------------------------------------------------------------------

# [P0-1 方案1.5] 抽帧产物缓存：sample_frames/ 帧序列按「视频指纹+间隔」复用，

# 换文案/重复分析时免于重新抽帧（ffmpeg 进程从 ~60 次降到 0 次）。

# 缓存放 WORKDIR/sample_frames_cache/<md5>，与 analysis_cache 平级，互不污染。

# 容量上限 SAMPLE_FRAME_CACHE_KEEP：超出按目录 mtime 清最旧，防止无限堆积。

# 键与 VLM/ASR 缓存共用 _video_cache_key 指纹（size+mtime+前1MB哈希），

# 同一视频指纹才可复用，避免脏读。

# ---------------------------------------------------------------------------

SAMPLE_FRAME_CACHE_DIR = os.path.join(WORKDIR, 'sample_frames_cache')  # [P0-1 方案1.5] 抽帧产物缓存根目录（新增，原无此缓存）

SAMPLE_FRAME_CACHE_KEEP = 32   # [P0-1 方案1.5] 最多保留多少个视频的抽帧产物目录，超出按修改时间清最旧

def _sample_frame_cache_dir(video_path, interval):

    """抽帧产物缓存目录：按视频指纹+间隔定位（与 VLM/ASR 缓存同一指纹来源）。"""

    import hashlib

    key = _video_cache_key(video_path, f'frames_{int(interval)}')

    d = os.path.join(SAMPLE_FRAME_CACHE_DIR, hashlib.md5(key.encode('utf-8')).hexdigest())

    try:

        os.makedirs(d, exist_ok=True)

    except OSError:

        pass

    return d

def _sample_frame_cache_ready(dirpath, n):

    """缓存是否完整可用：meta.json 计数匹配且 n 张帧齐全，否则视为脏缓存重建。"""

    try:

        meta_p = os.path.join(dirpath, 'meta.json')

        if not os.path.exists(meta_p):

            return False

        with open(meta_p, 'r', encoding='utf-8') as f:

            meta = json.load(f)

        if meta.get('n') != n:

            return False

        return all(os.path.exists(os.path.join(dirpath, 'sample_%04d.jpg' % i))

                   for i in range(n))

    except Exception:

        return False

def _sample_frame_cache_mark(dirpath, n):

    """写完整性标记：抽帧全量完成后写入，避免半截帧集被误复用。"""

    try:

        with open(os.path.join(dirpath, 'meta.json'), 'w', encoding='utf-8') as f:

            json.dump({'n': n, 'ts': time.time()}, f)

    except Exception:

        pass

def _sample_frame_cache_trim():

    """容量上限：目录数超过 SAMPLE_FRAME_CACHE_KEEP 时按 mtime 清最旧。"""

    try:

        if not os.path.isdir(SAMPLE_FRAME_CACHE_DIR):

            return

        dirs = []

        for fn in os.listdir(SAMPLE_FRAME_CACHE_DIR):

            p = os.path.join(SAMPLE_FRAME_CACHE_DIR, fn)

            try:

                if os.path.isdir(p):

                    dirs.append((os.path.getmtime(p), p))

            except OSError:

                pass

        if len(dirs) > SAMPLE_FRAME_CACHE_KEEP:

            dirs.sort()

            for _, p in dirs[:len(dirs) - SAMPLE_FRAME_CACHE_KEEP]:

                shutil.rmtree(p, ignore_errors=True)

    except Exception:

        pass

