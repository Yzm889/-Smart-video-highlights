# -*- coding: utf-8 -*-
"""引擎/工具模块（由 webui_server.py 第4批拆分生成，符号与拆分前等价）。"""
import os, json, threading, time
# ---- 跨模块依赖（拆分后显式导入） ----
from cache_utils import HERE, WORKDIR
from ffmpeg_utils import AbortError, PROGRESS, _TLS, ffmpeg_run, probe_duration, _NO_WIN

# 本来在 webui 模块级、仅供本模块使用的常量（随迁移）；webui 侧通过 re-export 保持旧命名空间
AI_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ai_config.json')
_WHISPER_MODELS = {}

_WHISPER_KNOWN = {
    'tiny', 'base', 'small', 'medium',
    'large-v3', 'large-v2', 'large',
    'distil-large-v3', 'distil-large-v2', 'distil-medium',
    'distil-small',
}

def _whisper_name_from_repo(repo):
    """HF 仓库名 → 项目内部模型名。认三种常见前缀，认不出返回 None。

    faster-distil-whisper-large-v3 → distil-large-v3
    faster-whisper-large-v3        → large-v3
    whisper-large-v3               → large-v3
    distil-whisper-large-v3        → distil-large-v3
    """
    repo = (repo or '').strip()
    for pre in ('faster-distil-whisper-', 'distil-whisper-'):
        if repo.startswith(pre):
            return 'distil-' + repo[len(pre):]
    for pre in ('faster-whisper-', 'whisper-'):
        if repo.startswith(pre):
            return repo[len(pre):]
    return repo or None


def refresh_whisper_models():
    """扫描 models/whisper/ 目录，把已下载的模型名写入 _WHISPER_MODELS。
    同时保留已知模型名（即使尚未下载，也允许用户选择后触发下载）。
    前端 /api/whisper/status 的 valid_models 与本函数联动——用户下载新模型后点刷新即可热切换。

    【v1.4.8 修】原来只认「扁平布局」（目录里直接有 model.bin / *.safetensors），
    但 faster-whisper 走 huggingface_hub 下载时落的是 HF 缓存布局：
        models/whisper/models--Systran--faster-distil-whisper-large-v3/
            snapshots/<sha>/model.bin
    于是模型明明躺着 1.5G，状态却报 downloaded=False、ready=False ——
    前端的模型下拉全显示「（未下载）」，主人会以为没下过、再去点下载重下 1.5G。
    实测证据：`models--Systran--faster-distil-whisper-large-v3/snapshots/c3058b4…/model.bin`
    存在（1,512,927,867 字节），而 /api/whisper/status 全表 downloaded=False。
    现在两种布局都认：扁平 + HF 缓存（snapshots/<ref>/ 下有 model.bin 或 *.safetensors）。
    """
    _WHISPER_MODELS.clear()
    for name in _WHISPER_KNOWN:
        _WHISPER_MODELS[name] = {'name': name, 'downloaded': False}
    md = whisper_models_dir()
    if not os.path.isdir(md):
        return

    def _has_weights(d):
        try:
            names = os.listdir(d)
        except Exception:
            return False
        return 'model.bin' in names or any(n.endswith('.safetensors') for n in names)

    for entry in os.listdir(md):
        full = os.path.join(md, entry)
        if not os.path.isdir(full):
            continue
        if _has_weights(full):
            _WHISPER_MODELS.setdefault(entry, {'name': entry, 'downloaded': False})
            _WHISPER_MODELS[entry]['downloaded'] = True
            continue
        # HF 缓存布局：model 名从目录名反推（models--Org--Repo → Repo 再归一化）
        if not entry.startswith('models--'):
            continue
        snaps = os.path.join(full, 'snapshots')
        if not os.path.isdir(snaps):
            continue
        done = False
        try:
            for sha in os.listdir(snaps):
                if _has_weights(os.path.join(snaps, sha)):
                    done = True
                    break
        except Exception:
            pass
        if not done:
            continue
        repo = entry.split('--')[-1]                      # 如 faster-distil-whisper-large-v3
        name = _whisper_name_from_repo(repo)
        if name and name in _WHISPER_MODELS:
            _WHISPER_MODELS[name]['downloaded'] = True
    flat_names = []
    try:
        flat_names = os.listdir(md)
    except Exception:
        pass
    if 'model.bin' in flat_names or any(n.endswith('.safetensors') for n in flat_names):
        for name in _WHISPER_KNOWN:
            _WHISPER_MODELS.setdefault(name, {'name': name, 'downloaded': True})
            _WHISPER_MODELS[name]['downloaded'] = True


def _whisper_env_setup():
    """统一设置 Whisper 加载/下载环境：走 HF 镜像 + 清理失效系统代理（避免 WinError 10061）。
    所有 WhisperModel 调用前都要跑（含首次生成时的 asr_segments），否则系统里没开的代理会令连接被本机拒绝。"""
    mc = mirror_cfg()
    # 禁用 HuggingFace 新版 Xet 存储协议（cas-server.xethub.hf.co）：
    # 该协议在 hf-mirror 下会返回 401 Unauthorized，强制走普通 HTTP LFS 下载，镜像才能正常代理。
    os.environ['HF_HUB_DISABLE_XET'] = '1'
    os.environ['HF_XET_DISABLE'] = '1'
    try:
        import huggingface_hub
        if hasattr(huggingface_hub.constants, 'HF_HUB_DISABLE_XET'):
            huggingface_hub.constants.HF_HUB_DISABLE_XET = True
    except Exception:
        pass
    if mc.get('use_hf_mirror') and mc.get('hf_endpoint'):
        os.environ['HF_ENDPOINT'] = mc['hf_endpoint']
        try:
            import huggingface_hub
            huggingface_hub.constants.HF_ENDPOINT = mc['hf_endpoint']
            huggingface_hub.constants.ENDPOINT = mc['hf_endpoint']
        except Exception:
            pass
    # 压掉 huggingface_hub「未登录」良性告警（仅影响限速、不阻断下载），避免用户误以为出错
    try:
        import logging as _lg
        _lg.getLogger('huggingface_hub').setLevel(_lg.ERROR)
    except Exception:
        pass
    if mc.get('ollama_proxy'):
        # 用户明确填了代理 → 走代理（需确保代理软件在运行）
        os.environ['HTTP_PROXY'] = mc['ollama_proxy']
        os.environ['HTTPS_PROXY'] = mc['ollama_proxy']
    else:
        # 没填代理 → 清掉从系统/会话继承的失效代理（代理没开时 huggingface_hub 会撞 127.0.0.1 被拒 → 10061）
        for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy',
                  'ALL_PROXY', 'all_proxy', 'NO_PROXY', 'no_proxy'):
            os.environ.pop(k, None)

def _whisper_load_path(m):
    """若 models/whisper/<m>/ 已含完整文件（手动 aria2c 放置），返回该绝对目录让 WhisperModel 直接复用，
    避免重复联网下载。否则返回模型名字符串，由 faster-whisper 走默认目录。"""
    d = os.path.join(whisper_models_dir(), m)
    if os.path.isdir(d) and os.path.exists(os.path.join(d, 'model.bin')) \
            and os.path.exists(os.path.join(d, 'config.json')):
        return d
    return m

_CUDA_DLLS_REGISTERED = {'done': False}


def _cuda_dll_dirs():
    """返回 pip 装的 CUDA 运行时库所在目录（仅 Windows）。

    装了 nvidia-cublas-cu12 / nvidia-cudnn-cu12 后，DLL 落在
    site-packages/nvidia/{cublas,cudnn}/bin/ 下。这个目录既不在 PATH、也不在
    ctranslate2 自己旁边，ctranslate2 加载时直接报
    `Library cublas64_12.dll is not found`。"""
    if os.name != 'nt':
        return []
    import site
    roots = [os.path.dirname(os.path.abspath(__file__))]
    try:
        roots.extend(site.getsitepackages())
        roots.append(site.getusersitepackages())
    except Exception:
        pass
    out = []
    for root in roots:
        for sub in ('nvidia/cublas/bin', 'nvidia/cudnn/bin',
                    'nvidia/cuda_runtime/bin', 'nvidia/cuda_nvrtc/bin'):
            d = os.path.join(root, *sub.split('/'))
            if os.path.isdir(d) and d not in out:
                out.append(d)
    return out


def _ensure_cuda_dll_path():
    """把 CUDA 库目录前置到本进程 PATH，让 ctranslate2 能加载到 cublas。

    注意：os.add_dll_directory 对 ctranslate2 无效（它走传统 DLL 搜索，
    实测仍然报 cublas64_12.dll not found）。这里改的是**本进程的环境变量副本**，
    不碰用户系统 PATH；子进程（ffmpeg 等）会继承，但也只是多几个搜索目录。
    代价：98 分钟视频的 ASR 从 30 分钟（CPU int8）回到 5 分钟（GPU fp16）。"""
    if _CUDA_DLLS_REGISTERED['done']:
        return
    _CUDA_DLLS_REGISTERED['done'] = True
    try:
        dirs = _cuda_dll_dirs()
        if not dirs:
            return
        cur = os.environ.get('PATH') or ''
        missing = [d for d in dirs if d.lower() not in cur.lower()]
        if missing:
            os.environ['PATH'] = os.pathsep.join(missing) + os.pathsep + cur
        # 已加载过 ctypes 的 DLL 不受影响，但 WinDLL 走 PATH，这里再主动加载一次
        import ctypes
        for name in ('cublas64_12.dll', 'cudnn64_9.dll'):
            for d in dirs:
                p = os.path.join(d, name)
                if os.path.exists(p):
                    try:
                        ctypes.CDLL(p)
                        break
                    except Exception:
                        continue
    except Exception:
        pass


def _note_progress_warning(msg):
    """把一条告警追加到当前任务（PROGRESS[runid]）的 warnings 上。

    PROGRESS 是按 runid 索引的，必须写到 PROGRESS[rid]；写顶层键会污染全局字典。
    非任务线程（测试/主线程）没有 runid，此时静默跳过——那里没有对应的进度面板可展示。
    """
    rid = getattr(_TLS, 'runid', None)
    if not rid:
        return
    p = PROGRESS.get(rid)
    if isinstance(p, dict):
        p['warnings'] = list(p.get('warnings') or []) + [msg]


def _silent_remove(path):
    """删掉中间文件，删不掉也不影响主流程（临时文件清理不该让任务失败）。"""
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _load_wav_as_float32(wav_path, chunk_frames=1 << 20):
    """把 16kHz/单声道/16bit PCM 的 wav 读成 float32 numpy 数组。

    为什么不用 faster_whisper 内部的 decode_audio：那条路径走 PyAV 的
    av.open(..., metadata_errors='ignore')，而 PyAV>=15 已移除该关键字参数
    （本机 av19.0.1 + faster-whisper1.2.1 实测抛
    `TypeError: open() got an unexpected keyword argument 'metadata_errors'`），
    结果整个 ASR 静默失败。我们自己产出的 wav 一定是标准 PCM，用 stdlib wave
    读即可，既绕开版本耦合，也省掉一次解码。

    【F8/M2-5 内存】旧实现 `raw = wf.readframes(n)` 一次性把整段字节读进内存，
    紧接着又 `np.frombuffer(raw).astype(float32)` 新建同尺寸 float 数组——两副本
    同时驻留，98 分钟视频峰值约 560MB。改为「预分配 float 缓冲 + 分块读入」，
    每块转换完即释放 raw 字节，峰值降到仅 float 缓冲（约 376MB），数值结果与旧实现
    逐位一致（不引入任何精度损失，确保 ASR 行为不变）。"""
    import wave
    import numpy as np
    with wave.open(wav_path, 'rb') as wf:
        if wf.getsampwidth() != 2 or wf.getnchannels() != 1:
            raise RuntimeError('wav 格式不符：需要 16bit 单声道，实际 %dbit/%dch'
                               % (wf.getsampwidth() * 8, wf.getnchannels()))
        n = wf.getnframes()
        out = np.empty(n, dtype=np.float32)
        read = 0
        while read < n:
            c = min(chunk_frames, n - read)
            raw = wf.readframes(c)
            if not raw:
                break
            out[read:read + c] = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            read += c
    return out[:read]


def _sec_to_float(t):
    """'00:01:02,500' / '00:01:02.500' / '01:02.5' → 秒。"""
    t = (t or '').strip().replace(',', '.')
    parts = t.split(':')
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except Exception:
        return 0.0


def _parse_notes_timeline(lines):
    r"""解析「笔记式时间轴」字幕：每行 `\[MM:SS\] 台词` 或 `\[HH:MM:SS\] 台词`。

    主人手写稿 / 速记工具最常输出的就是这种格式（无序号、无 `-->` 时间轴、
    只有每段起始时间）——对「台词吸附 / 引用让位」完全够用：
    start = 行首标注时间，end = 下一行标注时间（最后一条 +15 秒兜底）。
    不带时间标注的行 → 视为上一条的换行续文，拼回去。
    条目 <2 视为不是这种格式，返回 []（防止把普通带方括号文本的文件误判）。"""
    import re as _re
    pat = _re.compile(r'^\s*[\[【]\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*[\]】]\s*(.*)$')
    cues = []
    for line in lines:
        m = pat.match(line or '')
        if m:
            parts = [int(x) for x in m.group(1).split(':')]
            if len(parts) == 2:
                st = float(parts[0] * 60 + parts[1])
            else:
                st = float(parts[0] * 3600 + parts[1] * 60 + parts[2])
            cues.append([st, str(m.group(2) or '').strip()])
        elif cues and (line or '').strip():
            cues[-1][1] = (cues[-1][1] + ' ' + line.strip()).strip()
    if len(cues) < 2:
        return []
    segs = []
    for i, (st, txt) in enumerate(cues):
        en = cues[i + 1][0] if i + 1 < len(cues) else st + 15.0
        if en <= st:
            en = st + 1.0
        if txt:
            segs.append({'start': st, 'end': en, 'text': txt})
    return segs


def parse_subtitle_file(path):
    """解析字幕文件 → [{start,end,text}]。支持 SRT / VTT / ASS / SSA / 笔记式时间轴。

    供两条路径复用：① 容器内字幕轨抽取出来的 .srt；② 外挂字幕文件（同目录同名）。
    """
    import re as _re
    segs = []
    try:
        with open(path, 'rb') as f:
            raw = f.read()
        # 字幕编码不统一：UTF-8(可带 BOM) / GB18030(中文外挂字幕，兼容 GBK、
        # GB2312) / UTF-16(带 BOM)。逐个严格解码，首个成功的即为正确编码；
        # 全失败才退回原行为(utf-8-sig + errors=ignore)，保证不抛异常。
        # 修正：此前硬编码 utf-8-sig + errors=ignore，会把 GB18030 中文字幕
        # 的字节整段吞掉，文本变乱码、部分条目因空文本被静默丢弃。
        encs = ('utf-16',) if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else ()
        encs = encs + ('utf-8-sig', 'gb18030')
        text = None
        for enc in encs:
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            text = raw.decode('utf-8-sig', errors='ignore')
        lines = text.splitlines()
    except Exception:
        return []
    # ASS/SSA：Dialogue: 起始,结束,...,文本
    if any(l.strip().startswith('Dialogue:') for l in lines[:60]):
        for l in lines:
            if not l.strip().startswith('Dialogue:'):
                continue
            body = l.strip()[len('Dialogue:'):]
            parts = body.split(',', 9)
            # ASS 标准字段序：Layer, Start, End, Style, Name, ML, MR, MV, Effect, Text
            # （Start/End 在索引 1/2，Text 在最后一个）
            if len(parts) < 10:
                continue
            try:
                st = _sec_to_float(parts[1].strip())
                en = _sec_to_float(parts[2].strip())
            except Exception:
                continue
            txt = parts[9].replace('\\N', ' ').replace('\\n', ' ').strip()
            if txt and en > st:
                segs.append({'start': st, 'end': en, 'text': txt})
        return segs
    # SRT / VTT
    cur_start = cur_end = None
    buf = []
    for line in lines:
        line = (line or '').rstrip('\n').strip()
        if line.startswith('WEBVTT') or line.startswith('NOTE') or line.startswith('STYLE'):
            continue
        if '-->' in line:
            if cur_start is not None and buf:
                segs.append({'start': cur_start, 'end': cur_end, 'text': ' '.join(buf).strip()})
            buf = []
            left, _, right = line.partition('-->')
            # 右半段 = 「结束时间」[+ 行尾文字]（畸形但常见的写法，别把文字丢了）
            m = _re.match(r'\s*([0-9:.,]+)\s*(.*)$', right or '')
            end_ts = m.group(1) if m else (right or '')
            tail = (m.group(2) if m else '').strip()
            cur_start, cur_end = _sec_to_float(left), _sec_to_float(end_ts)
            if tail:
                buf.append(tail)
        elif line and not line.isdigit():
            buf.append(line)
    if cur_start is not None and buf:
        segs.append({'start': cur_start, 'end': cur_end, 'text': ' '.join(buf).strip()})
    segs = [s for s in segs if s['text']]
    # 笔记式时间轴：`\[MM:SS\] 台词`（手写速记最常见格式）——SRT/ASS 都没命中时尝试。
    # 主人 2026-11-11 的手稿正是这种格式，旧解析器整份拒收 → 静默退回 ASR 白跑 3 分钟。
    if not segs:
        segs = _parse_notes_timeline(lines)
    return segs


def _external_subtitle_segments(video_path):
    """查找视频同目录的**外挂字幕文件**并解析 → [{start,end,text}]；没有返回 None。

    国内剧集/影视资源最常见的形态就是「视频 + 同名字幕文件」，字幕是人工校对的原文，
    比语音识别准得多——能找到就该直接用（比 ASR 快、还不占 GPU）。
    依次找：完全同名 → 同名带语言后缀（xxx.zh.srt / xxx.chs.srt / xxx.简中.srt 等）。
    """
    base = os.path.splitext(video_path)[0]
    exts = ('.srt', '.ass', '.vtt', '.ssa')
    for ext in exts:
        p = base + ext
        if os.path.isfile(p) and os.path.getsize(p) > 16:
            segs = parse_subtitle_file(p)
            if segs:
                return segs
    # 同名 + 语言后缀（.zh.srt / .chs.srt / .简体.srt …）
    # 只认**语言标签白名单**里的后缀：否则同目录下「第01集第2集.srt」这种也会
    # startswith 命中 → 拿错集的字幕，比识别错更坑。
    _LANGS = {'zh', 'zhs', 'chs', 'ch', 'sc', 'tc', 'tw', 'hk', 'cn', 'en', 'eng',
              'jp', 'ja', 'jpn', 'kr', 'ko', 'kor', 'zh-cn', 'zh-hans', 'zh-tw',
              'zh-hant', 'zh_cn', 'zh_tw', 'en-us', '简体', '繁體', '简中', '繁中',
              '中文', '英文', '日语', '韩语', '简体字幕', '繁體字幕'}
    d = os.path.dirname(os.path.abspath(video_path))
    stem = os.path.basename(base).lower()
    try:
        for fn in sorted(os.listdir(d)):
            low = fn.lower()
            if not low.endswith(exts):
                continue
            if not low.startswith(stem):
                continue
            rest = low[len(stem):-len(os.path.splitext(low)[1])]  # 去掉前缀与扩展名
            tag = rest.strip('._- ')
            if tag and tag not in _LANGS:
                continue
            p = os.path.join(d, fn)
            try:
                if os.path.getsize(p) <= 16:
                    continue
            except OSError:
                continue
            segs = parse_subtitle_file(p)
            if segs:
                return segs
    except Exception:
        pass
    return None


def _subtitle_streams(video_path):
    """探测视频里的字幕轨（返回 (数量, 首个轨索引)）。用 ffmpeg -i 解析 stderr，不依赖 ffprobe。

    注意：ffmpeg_run 返回的 stderr 是 **bytes**（项目惯例见 ffmpeg_utils._parse_duration），
    必须先 decode——单测用 str mock 看不出来，真机第一次跑就 TypeError。"""
    import re as _re
    try:
        rc, o, e = ffmpeg_run(['-hide_banner', '-i', video_path])
    except Exception:
        return 0, None
    if not e:
        return 0, None
    if isinstance(e, (bytes, bytearray)):
        e = e.decode('utf-8', 'ignore')
    elif not isinstance(e, str):
        e = str(e)
    idx = None
    n = 0
    for m in _re.finditer(r'Stream #\d+:\d+(?:\[[^\]]*\])?(?:\([^)]*\))?:\s*Subtitle:\s*(\w+)', e):
        n += 1
        if idx is None:
            # 从 "Stream #0:2" 里取轨序号
            sm = _re.search(r'Stream #\d+:(\d+)', e[m.start():m.start() + 40])
            idx = int(sm.group(1)) if sm else 0
    return n, idx


def _subtitle_segments(video_path):
    """抽取视频内嵌字幕轨 → [{start,end,text}]。没有字幕轨返回 None。

    【为什么要字幕轨优先】很多影视/网盘视频自带字幕轨（srt/ass）。字幕是**人工校对过的
    原文**，比语音识别准得多；而 ASR 在背景音、音乐、多人抢白、音效下会明显出错
    （用户反馈「识别字幕也不对，识别语音也不对」）。有字幕轨就用它，一条都不用 ASR。
    """
    n, idx = _subtitle_streams(video_path)
    if not n or idx is None:
        return None
    tmp = None
    try:
        tmp = os.path.join(WORKDIR, 'sub_%d.srt' % int(time.time() * 1000))
        os.makedirs(WORKDIR, exist_ok=True)
        rc, o, e = ffmpeg_run(['-y', '-i', video_path, '-map', '0:%d' % idx, '-f', 'srt', tmp])
        if rc != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) < 16:
            return None
        import re as _re
        segs = []
        cur_start = cur_end = None
        buf = []

        def _sec(t):
            t = (t or '').strip().replace(',', '.')
            parts = t.split(':')
            try:
                if len(parts) == 3:
                    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
                if len(parts) == 2:
                    return int(parts[0]) * 60 + float(parts[1])
                return float(parts[0])
            except Exception:
                return 0.0

        with open(tmp, 'r', encoding='utf-8-sig', errors='ignore') as f:
            for line in f:
                line = (line or '').rstrip('\n').strip()
                if '-->' in line:
                    if cur_start is not None and buf:
                        segs.append({'start': cur_start, 'end': cur_end,
                                     'text': ' '.join(buf).strip()})
                    buf = []
                    left, _, right = line.partition('-->')
                    # 右半段 = 「结束时间」[+ 行尾文字]（畸形但常见的写法，别把文字丢了）
                    m = _re.match(r'\s*([0-9:.,]+)\s*(.*)$', right or '')
                    end_ts = m.group(1) if m else (right or '')
                    tail = (m.group(2) if m else '').strip()
                    cur_start, cur_end = _sec(left), _sec(end_ts)
                    if tail:
                        buf.append(tail)
                elif line and not line.isdigit():
                    buf.append(line)
        if cur_start is not None and buf:
            segs.append({'start': cur_start, 'end': cur_end, 'text': ' '.join(buf).strip()})
        segs = [s for s in segs if s['text']]
        return segs or None
    except Exception:
        return None
    finally:
        _silent_remove(tmp)


def _asr_collapse_token_runs(text, min_total=4):
    """折叠单个片段内部的复读（『no, no, no…』×44、『shh, shh…』×70）。

    段间折叠管不到这种情况：它们是一条长片段里的同一个词反复出现，
    原片只有一声「no」，转写却被填成一长串。这些串留在台词里会
    稀释真正有信息量的对白（实测出事现场那段几乎全是 no/sh sh）。
    min_total=4 表示同一个词至少连续出现 4 次才折叠，保留「好，好」这类正常强调。
    """
    import re as _re
    if not text:
        return text
    s = str(text)
    # 先清掉不可见垃圾：U+FFFD（解码失败的占位符）、零宽字符、控制符。
    # 实测中文直播里会蹦出「啵啵\ufffd」这种尾巴，看起来像程序出错。
    s = _re.sub(r'[\ufffd\u200b-\u200f\u2028\u2029\u0000-\u0008\u000b\u000c\u000e-\u001f]', '', s)
    # 英文/数字词：允许词间夹逗号句号空格
    pat_word = _re.compile(r'\b(\w{1,12})\b(?:[\s,，、。.!?！？;；]*\1\b){%d,}' % (min_total - 1),
                           _re.IGNORECASE)
    prev = None
    while prev != s:                     # 反复折叠，处理「a a a a b b b b」这类多段
        prev = s
        s = pat_word.sub(r'\1', s)
    # 中日韩单字复读：「哈哈哈哈」「闪闪闪」——中文没有词边界，单独处理
    s = _re.sub(r'([\u4e00-\u9fa5])\1{3,}', r'\1\1', s)
    # 中日韩**词组**复读：「大人计大人计大人计…」×15。中文没有词边界，上面的
    # \b 规则完全抓不到，base 模型在纯音乐段实测就是这么吐的——一条 17 秒的
    # 注释全是同一个词，用户在精剪列表里根本没法判断这段该不该留。
    s = _asr_collapse_cjk_runs(s)
    return s


_PUNCT_JUNK = set('，。、！？；：,.!?;:… \t\r\n-—~～「」《》""\'\'（）()')


def _asr_collapse_cjk_runs(s, min_run=3, min_len=2, max_len=6):
    """折叠中文词组的连续复读（容忍词间标点），并吃掉末尾残留的半截。

    为什么不用正则：中文没有词边界（\\b 失效），同一个 2~6 字词组重复时往往
    还夹着逗号（『大人计，大人计，大人计』），正则要把「词 + 任意标点 + 同词」
    写成嵌套量词，既难读又容易回溯爆炸。这里直接按「有效字符」滑动比对：
    跳过标点后看连续几段是否同一个词组，是就只保留一份。"""
    if not s:
        return s
    pos = [k for k, ch in enumerate(str(s)) if ch not in _PUNCT_JUNK]
    n = len(pos)
    if n < min_run * min_len:
        return s

    def unit_at(p, L):
        if p + L > n:
            return None
        cs = [str(s)[pos[p + t]] for t in range(L)]
        for ch in cs:
            if not ('\u4e00' <= ch <= '\u9fa5'):
                return None
        return ''.join(cs)

    out = []
    i = 0
    done = 0                     # 已经输出到原文的哪个下标
    while i < n:
        hit = None
        # 由短到长试：词组长度按**最小周期**取，否则『大人计』×15 会被 6 字
        # 窗口匹配成『大人计大人』（6 是 3 的整数倍，看上去也在「重复」），
        # 折叠出来的结果会多留一份。
        for L in range(min_len, max_len + 1):
            u = unit_at(i, L)
            if not u:
                continue
            cnt, j = 1, i + L
            while unit_at(j, L) == u:
                cnt += 1
                j += L
            if cnt >= min_run:
                hit = (u, j, L)
                break
        if not hit:
            i += 1
            continue
        u, j, L = hit
        out.append(str(s)[done:pos[i]])          # 复读之前的原文（原样，含标点）
        out.append(u)
        tail = ''.join(str(s)[pos[t]] for t in range(j, n))
        if tail and len(tail) < L and u.startswith(tail):
            j = n                                # 末尾半截重复（『大人计』后剩个『大』）
        done = pos[j] if j < n else len(str(s))
        i = j
    out.append(str(s)[done:])
    return ''.join(out)


def _asr_collapse_repeats(segs, min_run=2):
    """折叠 ASR 的连续重复幻觉，返回新的 [{start,end,text}]。

    【为什么必须做】实测英文片被强制按中文转写时，模型在音乐/噪音段会复读同一句
    十几次（『我跟他们说』×12、『林顿省警察受伤』×4）。这些行原片里并不存在，
    却会被下游当成「此处有台词」的证据，把剧情理解和解说词都拉到无意义的重复上。
    规则：连续 >=min_run 行文本相同（忽略标点空白）时只保留第一行，
    时间区间延长到最后一行的结束，保持时间轴连续、不出现空洞。
    """
    if not segs:
        return []
    import re as _re

    def norm(t):
        return _re.sub(r'[\s，。！？、,.!?;；:："\'“”‘’…—\-]+', '', str(t or ''))

    out = []
    i, n = 0, len(segs)
    while i < n:
        cur = segs[i]
        key = norm(cur.get('text'))
        j = i + 1
        while j < n and norm(segs[j].get('text')) == key:
            j += 1
        if key and (j - i) >= min_run:
            out.append({'start': float(cur.get('start', 0) or 0),
                        'end': float(segs[j - 1].get('end', cur.get('end', 0)) or 0),
                        'text': _asr_collapse_token_runs(str(cur.get('text') or '').strip())})
        else:
            for _sg in segs[i:j]:
                out.append({'start': _sg.get('start', 0), 'end': _sg.get('end', 0),
                            'text': _asr_collapse_token_runs(str(_sg.get('text') or '').strip())})
        i = j
    return out


def _report_subtitle_sync(segs, video_path):
    """把字幕-视频版本校验结果写进任务 warnings；'ok' 时静默。

    只走 _note_progress_warning（与本文件其它提示的惯例一致），不引入 logger：
    这几条提示是给用户看的，不是给排查用的。
    """
    try:
        level, msgs, _gap = subtitle_sync_report(segs, video_path)
    except Exception:
        return
    if level == 'ok' or not msgs:
        return
    for m in msgs:
        _note_progress_warning(m)


def subtitle_sync_report(segs, video_path):
    """检查字幕时间轴与视频是否**同一版本**，返回 (level, messages, offset_hint)。

    【为什么必须查这一项】字幕路线最大的优点是不幻觉，但它有一个专属失效模式
    **错版**：内容全对，时间对不上。典型三种：
      · 视频无删减、字幕是删减版 → 字幕只覆盖前 25 分钟，后 5 分钟没台词
      · 视频是 WEB 版（片头有平台 logo + 片头曲）、字幕是 BD 版 → 全部台词整体偏移
      · 帧率不同（25 vs 23.976）→ 越往后错得越多
    前两种能靠数值检查抓出来；第三种（缓慢累积漂移）本地无法可靠判断，不假装能查。

    「硬字幕」会读错字，但读的就是这一帧；「错版字幕」每个字都对，读的却是别的时刻。
    后者更难发现，所以这里宁可多提示，也不要静默产出剧情断层的成片。

    level: 'ok' / 'warn' / 'bad'；messages 面向用户，直接进任务 warnings；
    offset_hint 是估算的整体偏移秒数（无把握时为 0.0，绝不猜）。
    """
    msgs = []
    if not segs:
        return 'ok', msgs, 0.0
    vdur = 0.0
    try:
        vdur = probe_duration(video_path) or 0.0
    except Exception:
        vdur = 0.0
    if vdur <= 1.0:
        return 'ok', msgs, 0.0        # 拿不到时长就不瞎判，宁可不说

    starts, ends = [], []
    for s in segs:
        try:
            starts.append(float(s.get('start', 0) or 0))
            ends.append(float(s.get('end', 0) or 0))
        except (ValueError, TypeError):
            continue
    if not starts:
        return 'ok', msgs, 0.0
    first, last = min(starts), max(ends)

    level = 'ok'
    _head_shift = False
    # ① 尾部缺口：字幕结束得比视频早太多 → 后半段没台词（删减版最典型）
    # 阈值定得比"片尾正常空档"大：片尾常有无人声的长镜头/字幕表/片尾曲，
    # 差 10%~20% 秒属于正常。误报会让用户习惯性无视警告，比不报更糟。
    tail_gap = vdur - last
    if tail_gap > max(60.0, vdur * 0.15):
        level = 'bad'
        msgs.append(
            '⚠️ 字幕只到 %s，视频长 %s，后半段差 %s —— 这多半是「删减版字幕配了完整版视频」，'
            '结局段会没有台词依据。建议换成与视频同版本的字幕。'
            % (_fmt_mmss(last), _fmt_mmss(vdur), _fmt_mmss(tail_gap)))
    elif tail_gap > max(90.0, vdur * 0.30):
        level = 'warn'
        msgs.append('提示：字幕比视频早结束 %s（若片尾是长镜头/无人声段落，可忽略）'
                    % _fmt_mmss(tail_gap))

    # ② 头部空白：字幕迟迟不开始 → 可能缺片头，导致整体后移
    # 90 秒是常见片头长度（平台 logo + 片头曲）；达到它就值得怀疑（用 >= 不用 >：
    # 整 90 秒的偏移正是最典型的情况，卡在边界上漏判等于这道检查白做）。
    # 注意：整体后移会让片尾也自然提前，所以判定为头部偏移后就不再重复报尾部缺口
    # （同一个病根报两条，用户会以为是两个问题）。
    if first >= max(90.0, vdur * 0.10):
        level = 'bad'
        _head_shift = True
        msgs.append(
            '⚠️ 字幕从 %s 才开始，前 %s 完全没有台词 —— 可能视频片头（平台 logo/片头曲）'
            '比字幕版本长，导致**全部台词整体偏移**。成片可能出现「画面与旁白错位」。'
            % (_fmt_mmss(first), _fmt_mmss(first)))
    elif first > 45.0:
        level = 'warn'
        msgs.append('提示：字幕 %s 才开始（片头较长？若台词确实从这时说起可忽略）'
                    % _fmt_mmss(first))
    if _head_shift and len(msgs) > 1:
        # 头部已判定整体后移 → 去掉尾部缺口那条（同源问题）
        msgs = [m for m in msgs if '字幕比视频早结束' not in m]

    # ②b 字幕比视频长：有相当一批台词**起点就落在视频结束之后**。
    # 这是尾部缺口的镜像情形，同样属于「错版」：字幕是更长版本（导演剪辑/未删减）
    # 的，套在短版本视频上。① 抓不到它，因为 tail_gap 变成负数，一个分支都不进。
    #
    # 为什么不直接用 last - vdur：有些字幕文件末尾带一条时间轴乱写的译者鸣谢
    # （比如 99:59:59），单看最后一条会把正常字幕误判成错版。改成数「落在片外的
    # 台词条数」，一两条野数据淹没不掉，而真的长版本字幕是成批超出。
    beyond = sum(1 for st in starts if st >= vdur)
    if not _head_shift and beyond >= max(3, int(len(starts) * 0.02)):
        level = 'bad'
        msgs.append(
            '⚠️ 有 %d 条台词（共 %d 条）的起点在视频结束之后 —— 这个字幕比视频长，'
            '多半是「未删减版字幕配了删减版视频」，对白与画面的对应关系会整体错位。'
            '建议换成与视频同版本的字幕。'
            % (beyond, len(starts)))

    # ③ 覆盖率：视频大面积没有台词覆盖（中间断档多）→ 字幕可能是删减版
    covered = sum(max(0.0, e - s) for s, e in zip(starts, ends))
    ratio = covered / vdur if vdur > 0 else 0.0
    if 0 < ratio < 0.15 and vdur > 60:
        if level == 'ok':
            level = 'warn'
        msgs.append('提示：字幕只覆盖全片 %.0f%% 的时长（%d 条）。若这是对话稀疏的片种可忽略；'
                    '否则说明字幕与视频版本可能不一致。' % (ratio * 100, len(segs)))

    # ④ 时间轴自身异常：乱序/重叠/零长，会让吸附与对齐拿到脏锚点
    bad_rows = sum(1 for s, e in zip(starts, ends) if e <= s)
    if bad_rows:
        if level == 'ok':
            level = 'warn'
        msgs.append('提示：字幕里有 %d 条时间异常（结束≤开始），已按解析顺序跳过其锚点。'
                    % bad_rows)

    return level, msgs, tail_gap


def _fmt_mmss(sec):
    """秒 → '12:34'。只用于提示文案，留空也不影响流程。"""
    try:
        s = int(round(float(sec)))
    except (ValueError, TypeError):
        return '--:--'
    if s < 0:
        s = 0
    return '%d:%02d' % (s // 60, s % 60) if s < 3600 else '%d:%02d:%02d' % (s // 3600, s % 3600 // 60, s % 60)


def asr_segments(video_path, progress=None, pct_range=None, language=None,
                 model_name=None):
    """本地转写台词，返回 [{start,end,text}]。不可用/失败返回 []。
    自动用 GPU（CUDA）加速；无显卡回退 CPU。模型权重首次运行联网下载一次（~140MB）。

    **优先用视频内嵌字幕轨**（人工校对，比 ASR 准）；没有字幕轨才走语音识别。
    用了哪种来源会写进当前任务的 warnings，用户在结果里能直接看到。
    用字幕时还会做一次「字幕与视频是否同版本」的校验（见 subtitle_sync_report）：
    错版字幕不会产出幻觉，但会让旁白与画面整体错位，且看起来一切正常，必须提示出来。

    language：可选，本次调用的语言覆盖（'zh'/'en'/'ja'…；'' 或 'auto' 表示交给
    模型判别）。不传 = 走全局配置 whisper.language（见 whisper_language）。
    直播切片等场景音频语言用户心里有数，固定下来可以避免自动判别翻车
    （2026-11-11 实测：中文直播被误判成 en，台词文本打折）。

    model_name：可选，本次调用的模型覆盖。v1.4.64 新增：配置里的
    distil-large-v3 是**英文专用**蒸馏模型，中文内容会被硬转成英文（实测
    「这是好的几说是比较复杂」→「that's, I'm just I'm not…」），精剪注释
    直接变成乱码。调用方（live_clip）在语言非英文时改传多语言模型兜底。
    不传 = 走全局配置（行为与旧版逐位一致）。

    progress / pct_range：可选。传了就逐段推进进度并响应取消——
    旧实现一次性消费 transcribe 的生成器，长视频（十几分钟）转写期间进度条
    长时间停在同一个百分比，观感与「卡死」无异，且中途无法取消。"""
    # ① 字幕轨优先（顺序：容器内字幕轨 → 外挂字幕文件 → 语音识别）
    try:
        _subs = _subtitle_segments(video_path)
    except Exception:
        _subs = None
    if _subs:
        _note_progress_warning('台词来源：视频内嵌字幕轨（%d 条，比语音识别更准）' % len(_subs))
        _report_subtitle_sync(_subs, video_path)
        return _subs
    try:
        _ext = _external_subtitle_segments(video_path)
    except Exception:
        _ext = None
    if _ext:
        _note_progress_warning('台词来源：同目录外挂字幕文件（%d 条，比语音识别更准）' % len(_ext))
        _report_subtitle_sync(_ext, video_path)
        return _ext
    try:
        from faster_whisper import WhisperModel
    except Exception:
        return []
    wav = None
    try:
        wav = os.path.join(WORKDIR, f'asr_{int(time.time()*1000)}.wav')
        os.makedirs(WORKDIR, exist_ok=True)
        rc, o, e = ffmpeg_run(['-y', '-i', video_path, '-ar', '16000', '-ac', '1', '-f', 'wav', wav])
        if rc != 0 or not os.path.exists(wav):
            return []
        device, ctype = whisper_device()
        _whisper_env_setup()
        _use_gpu = (device == 'cuda')

        def _transcribe(dev=None, ctype_=None):
            # 语言先定、模型后选：模型必须先和语言对得上，否则中文音频喂给
            # 英文专用 distil 模型会整片变成英文幻觉（见 whisper_is_en_only）。
            _lang = whisper_language()
            # 本次调用的显式覆盖优先（'auto'/空 = 不覆盖，走全局配置）
            _ovr = str(language or '').strip().lower()
            if _ovr and _ovr != 'auto':
                _lang = _ovr
            _auto = (_lang in ('', 'auto', None))
            _eff_lang = None if _auto else _lang
            _cfg_model = model_name or whisper_model_name()
            _mn = whisper_model_for_language(_cfg_model, _eff_lang)
            if _mn != _cfg_model:
                _note_progress_warning(
                    '识别模型 %s 只支持英文，本次内容语言为「%s」，已自动改用多语言模型 %s'
                    % (_cfg_model, _eff_lang or '自动判别', _mn))
            # 模型加载 + 转写 + 消费生成器：整段处于推理态
            model = WhisperModel(_whisper_load_path(_mn),
                                 device=dev or device, compute_type=ctype_ or ctype,
                                 download_root=whisper_models_dir())
            # 参数说明（都是「让结果更准/更稳」，实测影响很大）：
            #  condition_on_previous_text=False —— **最关键**：默认 True 会让模型把上一句
            #    当上下文，一旦某句识别错，后面会顺着错下去、反复复读同一句直到结尾
            #    （用户反馈「识别语音也对不上」的典型成因）。关掉后每段独立识别。
            #  temperature=0 —— 贪心解码，结果稳定可复现（默认会采样，同一视频两次结果不同）。
            #  beam_size=5 —— 比贪心更准一点。
            #  compression_ratio_threshold / log_prob_threshold / no_speech_threshold
            #    —— 标准抗幻觉阈值：模型对某段"没把握"时宁可标成静音也不硬编台词。
            #  language —— 配置值 'auto' 必须真的交给模型自动判别（传 None），
            #    而不是退化成 'zh'。把英文片硬按中文解，模型会「照着中文往下猜」，
            #    输出的是一串翻译腔幻觉（实测：英文对白被转成『我跟他们说』×12、
            #    『林顿省警察受伤』×4 这类重复垃圾），而下游剧情理解/语义对齐
            #    会把这些垃圾当台词证据，解说质量从源头就歪了。
            # initial_prompt 只对中文有效：给英文音频塞『以下是普通话的句子』
            # 等于主动把解码器推向中文输出。
            _prompt = '以下是普通话的句子。' if (not _auto and str(_eff_lang).startswith('zh')) else None
            segments, info = model.transcribe(
                _load_wav_as_float32(wav), language=_eff_lang, vad_filter=True,
                initial_prompt=_prompt,
                condition_on_previous_text=False,
                temperature=0.0,
                beam_size=5,
                compression_ratio_threshold=2.4,
                log_prob_threshold=-1.0,
                no_speech_threshold=0.6)
            try:
                _det = str(getattr(info, 'language', '') or '')
                if _det:
                    _note_progress_warning('识别语言：%s（%s）' % (
                        _det, '自动判别' if _auto else '按配置指定'))
            except Exception:
                pass
            total = 0.0
            try:
                total = float(getattr(info, 'duration', 0) or 0)
            except Exception:
                total = 0.0
            lo, hi = (pct_range or (0, 0))
            segs = []
            n = 0
            for s in segments:
                n += 1
                # 段间检查取消：transcribe 一旦启动无法从外部中断，只能在消费间隙响应
                if _aborted():
                    raise AbortError('用户取消了任务')
                if s.text and s.text.strip():
                    segs.append({'start': float(s.start), 'end': float(s.end),
                                 'text': (s.text or '').strip()})
                if progress is not None and (n % 3 == 0):
                    frac = min(1.0, (float(s.end) / total)) if total > 0 else 0.5
                    progress['pct'] = int(lo + (hi - lo) * frac)
                    progress['phase'] = ('识别台词 %s / %s' % (_fmt_hms(s.end), _fmt_hms(total))
                                         if total > 0 else '识别台词…')
            if segs:
                _note_progress_warning('台词来源：语音识别（视频没有可用的字幕轨/外挂字幕）')
            return _asr_collapse_repeats(segs)

        # GPU 推理失败（显存不足 / cuBLAS 缺库 / 驱动抽风）时降级 CPU 重跑一次，
        # 而不是让整条 ASR 直接报废——CPU 慢但一定能出结果，好过一句「没识别出台词」。
        if _use_gpu:
            try:
                with _gpu_slot():
                    return _transcribe()
            except AbortError:
                raise
            except Exception as ex:
                _note_progress_warning(
                    'GPU 转写失败，已降级 CPU（较慢）：%s: %s' % (type(ex).__name__, str(ex)[:160]))
                return _transcribe('cpu', 'int8')
        return _transcribe()
    except Exception as ex:
        # 失败必须留下痕迹：这条路径以前是裸 except -> return []，
        # 上层只看到「ASR 没结果」，用户完全不知道是模型没装、CUDA 缺库还是网络问题。
        # 这里把真实原因挂到当前任务的 warnings，analyze_pipeline 会写进结果并显示。
        _note_progress_warning('ASR 转写失败：%s: %s' % (type(ex).__name__, str(ex)[:200]))
        return []
    finally:
        # 中间 wav 无论成功失败都必须删：16kHz/16bit 单声道约 1.9MB/分钟，
        # 98 分钟就是 181MB。以前只在异常分支删、成功路径直接 return 掉，
        # 实测 webui_workspace/ 累计沉淀了 247MB。
        _silent_remove(wav)

_AI_CFG_CACHE = {'data': None, 'mtime': 0}

def load_ai_config():
    try:
        mt = os.path.getmtime(AI_CONFIG_PATH)
        if mt != _AI_CFG_CACHE['mtime']:
            with open(AI_CONFIG_PATH, 'r', encoding='utf-8') as f:
                _AI_CFG_CACHE['data'] = json.load(f)
            _AI_CFG_CACHE['mtime'] = mt
        return _AI_CFG_CACHE['data'] or {}
    except Exception:
        return {}

def local_llm_cfg():
    """读取本地模型(Ollama 等 OpenAI 兼容端点)配置；未配置时给 Ollama 默认值。
    enabled 默认 False：未显式开启本地模型时绝不尝试连 localhost:11434（避免无服务时拖慢省流解说、
    或误回显 prompt 污染解说稿）。"""
    cfg = load_ai_config().get('local') or {}
    return {
        'enabled': cfg.get('enabled', False),
        'base_url': (cfg.get('base_url') or 'http://localhost:11434/v1').rstrip('/'),
        # 默认与桌面版装机引导一致（2026-11-07：EXE 无配置快照时旧默认 qwen2.5 会拉错模型）
        'model': cfg.get('model') or 'qwen3:8b',
        'api_key': cfg.get('api_key') or '',
    }

# ---------------------------------------------------------------------------
# 本地模型失败状态：可见 + 快速失败（熔断）
#
# 实测（RTX3060 12GB + 16GB 内存，Ollama 0.40.11）：配置 qwen3:14b-q4_K_M（9.28GB 权重）
# 时，单次调用要 47~159 秒，且间歇性 HTTP 500（"llama-server process has terminated:
# exit status 0xe06d7363"）；换 minicpm-v4.5（6.12GB）同请求 33.5s 正常出文。
# 关键危害不是慢本身，而是：失败后模型不驻留 → 下一段又要重读 9.28GB 权重 →
# 磁盘 I/O 打满 + 16GB 内存页缓存抖动 → 整机卡死（用户反馈「网卡都卡掉」）；
# 而旧代码 `except: pass` 把异常吞光，用户只看到「卡」，看不到原因。
# 故：连续失败达阈值即熔断（本轮不再重复试），并保留最后错误供 UI 展示。
# ---------------------------------------------------------------------------
_LOCAL_LLM_FAIL = {'count': 0, 'last_error': '', 'last_model': ''}
_LOCAL_LLM_FAIL_MAX = 2          # 连续失败 2 次即熔断（再试也大概率同样失败）

# 真实推理探针的状态（与熔断分开：探针只回答「能不能出字」，不参与熔断计数）
_LOCAL_INFER_PROBE = {'ts': 0.0, 'ok': None, 'msg': ''}
_LOCAL_INFER_PROBE_TTL = 60.0    # 结果缓存 60 秒
_LOCAL_INFER_PROBE_TIMEOUT = 25   # 探针超时：卡死的 Ollama 必须快速暴露


def _ollama_err_text(body):
    """从 Ollama 错误响应体里取出真正的原因文本。

    Ollama 把关键信息放在 body 里（JSON: {"error":{"message":"llama-server process has
    terminated: exit status 0xe06d7363"}}），只看 HTTP 状态码会丢���全部线索。"""
    if not body:
        return ''
    try:
        import json as _j
        d = _j.loads(body)
        if isinstance(d, dict):
            err = d.get('error')
            if isinstance(err, dict):
                return str(err.get('message') or '')[:300]
            if isinstance(err, str):
                return err[:300]
            return str(d.get('message') or '')[:300]
    except Exception:
        pass
    return str(body)[:200]


def _llm_payload(model, messages, max_tokens=1500, temperature=0.8):
    """构造 Ollama 请求体，对混合思考模型（Qwen3 等）显式关闭思考。

    Qwen3 默认开启 thinking：实测 14b-q4_K_M 一次「自我介绍」就用掉 492 字思考
    token，而 max_tokens 是思考+正文的共享配额——写解说稿这种需要 1500 token
    的任务，思考一开就把配额吃光，正文返回空串，上层拿到 llm_empty，
    阶段①所有段落在编辑台都是「无草稿」。
    关思考后：5.7s vs 28.9s（快 5 倍），正文完整。解说稿是短文本格式活，
    不需要链式推理，关掉思考是正确的取舍。
    Ollama >= 0.9 支持 think 字段；老版本忽略未知字段，不影响。"""
    return {
        'model': model,
        'messages': messages,
        'max_tokens': max_tokens,
        'temperature': temperature,
        'think': False,
    }


def local_llm_infer_probe(timeout=_LOCAL_INFER_PROBE_TIMEOUT, force=False):
    """**真实推理**探针：发一个极小的生成请求，看本地模型能否真的出字。

    旧 `local_llm_ping` 只探 /models（服务在、模型列表在）→ 「就绪」是假的：
    实测模型能列出、真推理却要么 llama-server 直接崩（0xe06d7363）、要么请求
    挂起 200+ 秒无响应；用户在跑 20 分钟视频时才发现，整机已被拖死。
    这里真打一次极小请求，25 秒内出不来即判不可用，并把真实原因带出去。

    返回 (ok, msg)；结果缓存 60 秒（探针本身要几十秒，不能每次都打）。
    """
    import time as _t
    now = _t.time()
    if (not force and _LOCAL_INFER_PROBE['ok'] is not None
            and now - _LOCAL_INFER_PROBE['ts'] < _LOCAL_INFER_PROBE_TTL):
        return bool(_LOCAL_INFER_PROBE['ok']), _LOCAL_INFER_PROBE['msg']
    # 探「辅助任务实际会用到的模型」：配了 light_model 就用它，否则用主模型
    _light = local_llm_light_cfg()
    cfg = _light if _light else local_llm_cfg()
    model = (cfg or {}).get('model')
    if not (cfg or {}).get('base_url') or not model:
        _LOCAL_INFER_PROBE.update({'ts': now, 'ok': False, 'msg': '本地模型未配置'})
        return False, '本地模型未配置'
    import urllib.request, urllib.error, json as _j
    ok, msg = False, ''
    try:
        if _is_local_ollama((cfg or {}).get('base_url')):
            # 本机 Ollama → 原生端点：兼容端点不认 think:false，思考会吃光这 16 token
            # 预算 → 正文空 → 探针误判「模型不可用」→ 整个草稿阶段被跳过（实测 48 段 0 段草稿）
            out = _ollama_native_chat(cfg, [{'role': 'user', 'content': '只回复一个字：好'}],
                                     16, 0, timeout)
        else:
            payload = {'model': model,
                       'messages': [{'role': 'user', 'content': '只回复一个字：好'}],
                       'max_tokens': 16, 'temperature': 0, 'think': False,
                       'options': {'num_predict': 16}}
            req = urllib.request.Request(cfg['base_url'].rstrip('/') + '/chat/completions',
                                         data=_j.dumps(payload).encode('utf-8'),
                                         headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = _j.loads(resp.read().decode('utf-8'))
            out = _strip_think((data.get('choices') or [{}])[0].get('message', {}).get('content', ''))
        ok = bool((out or '').strip())
        msg = '推理正常' if ok else '推理返回空内容（模型可能崩溃或思考吃满配额）'
    except urllib.error.HTTPError as e:
        body = ''
        try:
            body = e.read().decode('utf-8', 'ignore')
        except Exception:
            pass
        msg = '推理失败 HTTP %s：%s' % (e.code, _ollama_err_text(body) or (e.reason or ''))
    except RuntimeError as e:
        _s = str(e)
        # 原生端点把超时包成 RuntimeError，这里还原成「无响应」语义（便于前端区分
        # 「模型崩了」与「模型没响应」）
        if 'timed out' in _s.lower() or 'timeout' in _s.lower():
            msg = '推理无响应（超时）：%s' % _s[:140]
        else:
            msg = _s[:220]
    except Exception as e:
        _s = str(e)
        msg = '推理无响应（%s）：%s' % ('超时' if ('timed out' in _s.lower() or 'timeout' in _s.lower()) else '异常',
                                     _s[:140])
    _LOCAL_INFER_PROBE.update({'ts': now, 'ok': ok, 'msg': msg})
    return ok, msg


def _is_local_ollama(base_url):
    """base_url 是否指向**本机** Ollama（形如 http://localhost:11434/v1）。"""
    if not base_url:
        return False
    u = str(base_url).lower()
    return ('localhost' in u or '127.0.0.1' in u) and '11434' in u


# 文本模型上下文：不给 num_ctx 时 Ollama 按模型默认 32768 加载，KV 缓存吃掉约 4.8GB
#（实测本机 qwen3:8b 驻留 9.65GB，其中权重只有 ~4.7GB）。12GB 卡上它和配音 worker
# 的 6.6GB 必然对撞（15.6GB > 12GB）→ WDDM 把权重静默换页进内存 → 每句配音 5s 变
# 60~100s → ask 超时 → worker 反复重启闪黑窗（2026-11-11 主人实测）。
# 【主人拍板降为 16384，驻留约 5.5GB】代价：超长片写稿 prompt 超 16K 会被 Ollama
# 截断（质量略降、不崩），换来「配音阶段显存装得下」的确定性。
# 注意：真实调用与推理探针都走 _ollama_native_chat，两条路径共用同一个值——
# ctx 一变 Ollama 就要重载，探针和正式调用若不同值会互相踢。VLM 见 _VLM_NUM_CTX。
_LLM_NUM_CTX = 16384

# 文本模型驻留时长。【v1.4.38 治「Ollama 反复加载模型」（v1.4.34 真机取证：12 分钟
# 任务 43 个 llama-server/ollama 事件）】不传 keep_alive 时 Ollama 默认只驻留 5 分钟，
# 写稿各段之间、写稿→对齐之间只要隔得久一点，下一次调用就是一次冷加载（spawn 新的
# llama-server + conhost 闪窗）。给 30m 常驻后，任务内 LLM 各阶段共享一次加载；阶段
# 守卫（VLM 前/后、配音前的腾位）仍会用 keep_alive:0 把它请出去——12GB 卡上文本模型
# 与 VLM/配音 worker 物理上装不下，那是固有行为，不属于这里的治理对象。
# 同一端点 vlm_chat_multi 带 keep_alive 已长期跑通（见 _VLM_KEEP），Ollama 0.40.15 实证。
_LLM_KEEP = '30m'


def _ollama_native_chat(cfg, messages, max_tokens, temperature, timeout):
    """走 Ollama **原生** /api/chat 端点（仅本机 Ollama）。

    【为什么必须用原生端点——2026-10-27 实测】
    Ollama 0.40.11 的 OpenAI 兼容端点（/v1/chat/completions）**完全忽略**
    `think:false` 与 `chat_template_kwargs.enable_thinking` → 混合思考模型（Qwen3）
    照跑思考，把 max_tokens 吃光 → 正文返回空串。实测同一请求：
      · 兼容端点 max_tokens=16 → 正文 0 字、思考 22 字（正文被思考吃光）
      · 原生端点 think:false  → 正文「好」1 字、思考 0 字、1.1s（热调用 0.1s）
    真实后果：16 token 的健康探针误判「模型不可用」→ **阶段①整个草稿阶段被跳过**
    （48 段 0 段有草稿），而且每次调用白白多花 21+ token 思考时间。
    远程 Ollama / 云端 OpenAI 兼容服务仍走原路径（它们没有 /api/chat）。
    """
    import urllib.request, urllib.error, json as _j, re as _re
    root = _re.sub(r'/v1/?$', '', str(cfg['base_url']).rstrip('/'))
    # 配音 worker 常驻吃 4.4~6.6GB：显存不够时先让它让开，否则 Ollama 会把一半层
    # 丢到 CPU（不报错，但解说慢到不可用）。失败不影响主流程。
    # 【v1.4.38 修订】v1.4.34 遗留的「每次调用都无条件调」保留调用点，但
    # free_vram_for_llm 侧两处收紧：① skip_busy——正在合成的 worker 不杀（宁可
    # 这次 LLM 慢一点，不能打断配音，那会引发 worker 重启链）；② need 公式按
    # 16K ctx 重标定（旧公式是 32K 时代的，驻留 5.5GB 的模型按 9.6GB 判，过度杀）。
    try:
        import tts2_engines as _T2
        _T2.free_vram_for_llm(cfg.get('model'))
    except Exception:
        pass
    url = root + '/api/chat'
    payload = {'model': cfg['model'], 'messages': messages, 'stream': False,
               'think': False, 'keep_alive': _LLM_KEEP,
               'options': {'num_predict': int(max_tokens or 1500),
                           'temperature': temperature,
                           'num_ctx': _LLM_NUM_CTX}}
    req = urllib.request.Request(url, data=_j.dumps(payload).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = _j.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body = ''
        try:
            body = e.read().decode('utf-8', 'ignore')
        except Exception:
            pass
        raise RuntimeError('本地模型 %s 调用失败 HTTP %s：%s'
                           % (cfg['model'], e.code, _ollama_err_text(body) or (e.reason or '')))
    except Exception as e:
        raise RuntimeError('本地模型 %s 调用失败：%s' % (cfg['model'], str(e)[:200]))
    return _strip_think((data.get('message') or {}).get('content', ''))


# ---------------------------------------------------------------------------
# 本地向量语义（Ollama /api/embed）：「解说↔画面对得上」的语义匹配素材（2026-11-06）
# ---------------------------------------------------------------------------
_EMBED_CACHE = {}           # (model, prefix, text) -> L2 归一化向量。进程内缓存：场景描述
                            # 稳定、解说句重复生成间高度重合，第二次起零成本、零网络。
_EMBED_CACHE_MAX = 20000    # 防无界增长（252 场景+几百句≈千条级；超限整表清空，重算也就几秒）


def embed_cfg():
    """向量语义配置：ai_config.json 的 embed 节。缺省=本机 Ollama + nomic-embed-text。

    enabled 缺省 True：只在真正调用时才连；Ollama 不在/超时 → ollama_embed 返回 None，
    调用方自动降级（向量语义是锦上添花，绝不能拖累出片主流程）。
    """
    cfg = load_ai_config().get('embed') or {}
    return {
        'enabled': bool(cfg.get('enabled', True)),
        'base_url': (cfg.get('base_url') or 'http://localhost:11434').rstrip('/'),
        'model': (cfg.get('model') or 'nomic-embed-text').strip(),
    }


def ollama_embed(texts, prefix='', timeout=30, keep_alive='15m'):
    """批量把文本 embed 成向量（Ollama /api/embed），返回**L2 归一化**后的向量列表
    （归一化后余弦相似度=点积，打分端不用再除模长）。

    - 任何失败（服务不在/模型缺/超时/返回不齐）一律返回 None，绝不抛异常——
      调用方必须能据此降级（bigram/无语义），不许让向量问题打断出片。
    - 【代理坑】系统 HTTP_PROXY 会拦 127.0.0.1 → 用空 ProxyHandler 强制直连。
    - prefix：nomic-embed 官方建议检索任务加任务前缀（场景描述用 'search_document: '、
      解说句用 'search_query: '）；同侧文本同前缀，跨侧才有检索意义的相似度。
    - keep_alive 默认 '15m'：274MB 小模型，常驻避免每次生成重复从磁盘加载。
    """
    texts = [str(t or '') for t in (texts or [])]
    if not texts:
        return None
    c = embed_cfg()
    if not c['enabled'] or not c['model']:
        return None
    import urllib.request as _u, json as _j
    keys = [(c['model'], prefix, t) for t in texts]
    miss = [k for k in keys if k not in _EMBED_CACHE]
    if miss:
        payload = {'model': c['model'], 'input': [prefix + k[2] for k in miss],
                   'keep_alive': keep_alive}
        req = _u.Request(c['base_url'] + '/api/embed', data=_j.dumps(payload).encode('utf-8'),
                         headers={'Content-Type': 'application/json'})
        opener = _u.build_opener(_u.ProxyHandler({}))   # 强制直连，绕开系统/会话代理
        try:
            with opener.open(req, timeout=timeout) as resp:
                data = _j.loads(resp.read().decode('utf-8'))
            embs = data.get('embeddings') or []
            if len(embs) != len(miss):
                return None
            for k, vec in zip(miss, embs):
                if not vec:
                    return None
                n = (sum(float(x) * float(x) for x in vec)) ** 0.5 or 1.0
                _EMBED_CACHE[k] = tuple(float(x) / n for x in vec)
            if len(_EMBED_CACHE) > _EMBED_CACHE_MAX:
                _EMBED_CACHE.clear()
        except Exception:
            return None
    return [_EMBED_CACHE[k] for k in keys]


def vlm_unload():
    """显式让本地 VLM 退出显存，返回是否已发出卸载请求。失败不抛异常。

    【为什么需要】质量模式下画面理解会让 VLM 常驻（keep_alive='30m'），把每批
    40s+ 的权重加载省掉，110 个抽样点从「112 分钟且全部超时」降到 5 分钟。
    但 12GB 卡上 VLM(6.1GB) 与文本模型(5.0GB) 同时驻留会超出可用显存，
    所以画面理解一结束就主动卸载，把显存让给写稿/对齐/润色用的文本模型。
    """
    try:
        c = vlm_cfg()
        if c.get('mode') != 'ollama' or not c.get('base_url'):
            return False
        import urllib.request, json as _j, re as _re
        root = _re.sub(r'/v1/?$', '', str(c['base_url']).rstrip('/'))
        req = urllib.request.Request(
            root + '/api/generate',
            data=_j.dumps({'model': c['model'], 'keep_alive': 0}).encode('utf-8'),
            headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        return True
    except Exception:
        return False


def local_llm_chat(prompt, system=None, timeout=180, max_tokens=None):
    """调用本地 OpenAI 兼容端点生成文本（Ollama /v1/chat/completions）。失败抛异常。

    【P0 性能·可见性】本地模型起不来时，Ollama 的真实原因只在 HTTPError 的 body 里
    （"llama-server process has terminated: exit status 0xe06d7363"），旧实现只把
    "HTTP Error 500" 抛出去、再被上层 `except: pass` 吞掉 → 用户只看到「卡」，
    看不到任何原因。
    另外加**连续失败熔断**：实测本机（3060 12GB + 16GB 内存）配置 qwen3:14b-q4_K_M
    （9.28GB 权重）时，单次调用要 47~159 秒且间歇性 500；模型每次失败后不驻留，
    下一段又要重读 9.28GB 权重 → 磁盘 I/O 打满 + 页缓存抖动 → 整机卡死。
    连续失败达阈值即熔断，本轮不再重复试，把「60 段 × 100s」压成「1 次失败 + 快速跳过」。"""
    cfg = local_llm_cfg()
    if not cfg['base_url']:
        raise RuntimeError('本地模型未配置')
    if _LOCAL_LLM_FAIL['count'] >= _LOCAL_LLM_FAIL_MAX:
        raise RuntimeError('本地模型 %s 连续失败 %d 次，本轮已跳过重试（最后错误：%s）'
                           % (cfg['model'], _LOCAL_LLM_FAIL['count'],
                              _LOCAL_LLM_FAIL['last_error'] or '未知'))
    import urllib.request, urllib.error, json as _json
    messages = []
    if system:
        messages.append({'role': 'system', 'content': system})
    messages.append({'role': 'user', 'content': prompt})
    payload = _llm_payload(cfg['model'], messages,
                            max_tokens=(max_tokens or 1500))
    headers = {'Content-Type': 'application/json'}
    if cfg['api_key']:
        headers['Authorization'] = 'Bearer ' + cfg['api_key']
    req = urllib.request.Request(cfg['base_url'] + '/chat/completions',
                                 data=_json.dumps(payload).encode('utf-8'), headers=headers)

    def _call():
        if _is_local_ollama(cfg['base_url']):
            # 本机 Ollama → 原生 /api/chat（think:false 真正生效，见 _ollama_native_chat 说明）
            out = _ollama_native_chat(cfg, messages, (max_tokens or 1500), 0.8, timeout)
        else:
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = _json.loads(resp.read().decode('utf-8'))
            except urllib.error.HTTPError as e:
                body = ''
                try:
                    body = e.read().decode('utf-8', 'ignore')
                except Exception:
                    pass
                raise RuntimeError('本地模型 %s 调用失败 HTTP %s：%s'
                                   % (cfg['model'], e.code, _ollama_err_text(body) or (e.reason or '')))
            except RuntimeError:
                raise
            except Exception as e:
                raise RuntimeError('本地模型 %s 调用失败：%s' % (cfg['model'], str(e)[:200]))
            out = _strip_think((data.get('choices') or [{}])[0].get('message', {}).get('content', ''))
        # 空内容必须算失败（两条路径都要判）：否则上层拿到空串会静默当成功
        if not (out or '').strip():
            raise RuntimeError('本地模型 %s 返回空内容（模型崩溃或思考吃满配额）' % cfg['model'])
        return out

    def _guarded():
        """记录失败原因 + 连续失败熔断计数；成功则清零。"""
        try:
            out = _call()
        except RuntimeError as e:
            _LOCAL_LLM_FAIL['count'] += 1
            _LOCAL_LLM_FAIL['last_error'] = str(e)[:300]
            _LOCAL_LLM_FAIL['last_model'] = cfg['model']
            raise
        _LOCAL_LLM_FAIL['count'] = 0
        _LOCAL_LLM_FAIL['last_error'] = ''
        return out

    # S7: 本地推理独占 GPU 槽；远端（云端/远程 Ollama）不加锁
    if _is_local_url(cfg['base_url']):
        with _gpu_slot():
            return _guarded()
    return _guarded()


def local_llm_last_error():
    """最近一次本地模型失败的原因（供 UI/告警展示）；无失败返回 ''。"""
    return _LOCAL_LLM_FAIL.get('last_error') or ''


def local_llm_fail_count():
    """本地模型连续失败次数（达到熔断阈值后不再重试）。"""
    return int(_LOCAL_LLM_FAIL.get('count') or 0)

def local_llm_light_cfg():
    """S4: 轻量辅助模型配置（local.light_model）。辅助任务（判型/详略规划/剧情理解/对齐）
    优先使用更小的模型，缩短独占 GPU 槽的推理时间；未配置返回 None，调用方回退主模型。"""
    cfg = load_ai_config().get('local') or {}
    lm = (cfg.get('light_model') or '').strip()
    if not lm:
        return None
    return {
        'base_url': (cfg.get('base_url') or 'http://localhost:11434/v1').rstrip('/'),
        'model': lm,
        'api_key': cfg.get('api_key') or '',
    }


def local_llm_light_chat(prompt, system=None, timeout=180, max_tokens=None):
    """S4: 轻量辅助模型调用（Ollama /v1/chat/completions）。未配置抛 RuntimeError，
    由调用方回退主模型；本地端点同样独占 GPU 槽（S7），远端不加锁。

    max_tokens：批量任务（一次出多段草稿）需要更大输出预算，可显式指定。"""
    cfg = local_llm_light_cfg()
    if not cfg:
        raise RuntimeError('轻量辅助模型未配置')
    import urllib.request, json as _json
    messages = []
    if system:
        messages.append({'role': 'system', 'content': system})
    messages.append({'role': 'user', 'content': prompt})
    payload = _llm_payload(cfg['model'], messages, max_tokens=(max_tokens or 1500))
    headers = {'Content-Type': 'application/json'}
    if cfg['api_key']:
        headers['Authorization'] = 'Bearer ' + cfg['api_key']
    req = urllib.request.Request(cfg['base_url'] + '/chat/completions',
                                 data=_json.dumps(payload).encode('utf-8'), headers=headers)

    def _call():
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = _json.loads(resp.read().decode('utf-8'))
        return _strip_think((data.get('choices') or [{}])[0].get('message', {}).get('content', ''))

    if _is_local_url(cfg['base_url']):
        with _gpu_slot():
            return _call()
    return _call()


def mirror_cfg():
    """国内下载镜像配置：让 whisper(来自 HuggingFace) 与 ollama 模型拉取走镜像/代理，免科学上网。"""
    cfg = load_ai_config().get('mirror') or {}
    return {
        'hf_endpoint': (cfg.get('hf_endpoint') or 'https://hf-mirror.com').strip(),
        'use_hf_mirror': bool(cfg.get('use_hf_mirror', True)),  # 默认开，中文用户友好
        'ollama_proxy': (cfg.get('ollama_proxy') or '').strip(),
    }

# ---------------------------------------------------------------------------
# 本地视觉理解 (VLM, Qwen2.5-VL 等)：看图 + 台词 + 梗概 → 真解说
# ---------------------------------------------------------------------------
def vlm_cfg():
    """看图（VLM）配置。兼容两套键名，并规范化 base_url。

    【为什么要有兼容】设置页的「视觉模型」输入框把配置写到 ai_config['vision']，
    而运行时这里只读 ai_config['vlm'] —— 键名对不上，用户在设置页填了模型名、
    或在下拉里选了已装模型，程序仍然认为「VLM 未启用」。实测 2026-11-09：
    用户明明装了 MiniCPM-V 4.5 / qwen3-vl:8b，状态接口却是 enabled=false。

    【base_url 去掉 /v1】探活请求打的是 Ollama 原生 /api/tags、对话打 /api/chat。
    前端下拉传的是 http://localhost:11434/v1，直接拼会变成 /v1/api/tags（必然 404），
    于是「选了已装模型也不生效」。
    """
    cfg = load_ai_config().get('vlm') or {}
    if not cfg:
        _v = load_ai_config().get('vision') or {}
        if _v.get('model'):
            cfg = {'enabled': True, 'mode': 'ollama',
                   'base_url': _v.get('base_url') or 'http://localhost:11434',
                   'model': _v.get('model'), 'api_key': _v.get('api_key') or ''}
    base = (cfg.get('base_url') or 'http://localhost:11434').rstrip('/')
    if base.endswith('/v1'):
        base = base[:-3]
    return {
        'enabled': bool(cfg.get('enabled', False)),
        'mode': (cfg.get('mode') or 'ollama'),  # ollama(原生 /api/chat) | openai(/v1/chat/completions 多模态)
        'base_url': base,
        'model': cfg.get('model') or 'qwen3-vl:8b',
        'api_key': cfg.get('api_key') or '',
    }

# VLM 采样温度：画面描述/剧情理解要的是「转述看到的」，不是创意发挥，
# 取低温让同一次分析可复现（同一段视频两次跑出的画面描述一致，便于对照问题）。
_VLM_TEMPERATURE = 0.3

# VLM 上下文上限。【2026-11-10 主人实测：显存/内存双爆、VLM 推理到一点点就卡住】
# 不给 num_ctx 时 Ollama 按模型默认 32768 加载。实测本机 qwen3-vl:8b 权重只有
# 5.38GB，驻留却是 10.2GB —— **KV 缓存吃掉 4.8GB，和权重一样大**。12GB 卡上再叠加
# 写稿用的 qwen3:8b（权重 4.68GB / 驻留 9.12GB），nvidia-smi 直接 11959/12288 打满；
# VLM 只剩约 2.6GB，七成层被迫跑在 CPU/内存上，于是「推理到一点点就卡住」，
# 16GB 内存也被换页拖爆。
# 本项目单批最多 8 帧（极速档）×720px ≈ 每帧约 2600 token + 提示词，16K 足够，
# 减半后 VLM 总驻留降到约 7.8GB，给写稿模型留出整块显存。
# **改这个值会让 Ollama 重载模型**（ctx 变了要重新分配 KV），所以要全局统一：
# vlm_chat 与 vlm_chat_multi 共用，别各写各的。
_VLM_NUM_CTX = 16384


def vlm_chat_multi(image_paths, text, system=None, timeout=240, max_tokens=None, keep_alive=0):
    """调用本地/云端 VLM：一次传入多张图（按时间先后顺序），返回模型文本回复。失败抛异常。
    用于「剧情理解」：把整片关键帧一起看，判断作品/人物/剧情，而不是逐帧孤岛描述。

    max_tokens：ollama 原生端点下对应 num_predict（默认 1500）。画面理解类任务
    一次要输出 N 段描述，提示词长时 1500 会被截断——需要更大值就显式传入。

    keep_alive：模型在显存里的驻留时长（ollama 原生参数）。默认 0 = 调用完立即卸载，
    适合「偶发单次调用」省显存；但批量场景分析是连续几十次调用，0 会让每次调用都
    重新从磁盘加载 6GB 权重（实测单批 >80s 不返回 → 全部超时 → 场景描述大面积缺失）。
    批量分析必须传 keep_alive='30m' 之类的常驻值，任务结束再显式卸载。"""
    c = vlm_cfg()
    if not c['base_url']:
        raise RuntimeError('VLM 未配置')
    import urllib.request, base64 as _b64, json as _json
    headers = {'Content-Type': 'application/json'}
    if c['api_key']:
        headers['Authorization'] = 'Bearer ' + c['api_key']
    if c['mode'] == 'ollama':
        imgs = []
        for p in image_paths:
            with open(p, 'rb') as f:
                imgs.append(_b64.b64encode(f.read()).decode('ascii'))
        msg = {'role': 'user', 'content': text, 'images': imgs}
        messages = ([{'role': 'system', 'content': system}] if system else []) + [msg]
        # 【实测 minicpm-v4.5 / 2026-10-28】必须显式关思考，否则输出会被严重挤占：
        #   不关思考 → 思考 141 字、正文只有 41 字（残缺）、4.8s
        #   关掉思考 → 思考 0 字、正文 76 字且完整、1.2s（又快又准）
        # 多模态模型同样有 thinking 通道，Multi-image 时思考更长 → 正文可被挤到空。
        payload = {'model': c['model'], 'messages': messages, 'stream': False,
                   'think': False, 'keep_alive': keep_alive,
                   'options': {'num_predict': max_tokens or 1500,
                               'temperature': _VLM_TEMPERATURE,
                               'num_ctx': _VLM_NUM_CTX}}
        url = c['base_url'] + '/api/chat'
    else:
        content = []
        for p in image_paths:
            with open(p, 'rb') as f:
                b64 = _b64.b64encode(f.read()).decode('ascii')
            content.append({'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + b64}})
        content.append({'type': 'text', 'text': text})
        messages = ([{'role': 'system', 'content': system}] if system else []) + [{'role': 'user', 'content': content}]
        payload = {'model': c['model'], 'messages': messages, 'max_tokens': 1500, 'temperature': 0.7}
        url = c['base_url'] + '/v1/chat/completions'
    req = urllib.request.Request(url, data=_json.dumps(payload).encode('utf-8'), headers=headers)

    def _call():
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = _json.loads(resp.read().decode('utf-8'))
        if c['mode'] == 'ollama':
            msg = data.get('message') or {}
            out = (msg.get('content') or '').strip()
            if not out:
                # 兜底：个别模型/老版本对 think 参数兼容不佳时正文仍可能为空，
                # 此时退回思考内容（剥离 <think> 标签）——宁可是推理草稿，
                # 也别让「画面理解/剧情理解」整条链路静默拿到空串。
                out = _strip_think(msg.get('thinking') or '').strip()
            return out
        return (data.get('choices') or [{}])[0].get('message', {}).get('content', '')

    # S7: 本地 ollama 推理独占 GPU 槽；openai 模式（可能云端）与远程不加锁
    if c['mode'] == 'ollama' and _is_local_url(c['base_url']):
        with _gpu_slot():
            return _call()
    return _call()

def whisper_device():
    """返回 (device, compute_type)：检测到 NVIDIA CUDA 就用 GPU 加速（float16），否则回退 CPU(int8)。
    让「省流(本地离线)」模式在有无显卡的机器上都能跑，且尽量用显卡提速。"""
    if _cuda_available():
        return 'cuda', 'float16'
    return 'cpu', 'int8'

def whisper_model_name():
    """读取配置的 whisper 模型名（默认 base）。非法值回退 base。"""
    cfg = load_ai_config().get('whisper') or {}
    m = (cfg.get('model') or 'distil-large-v3')
    return m if m in _WHISPER_MODELS else 'base'


# 多语言模型优先级（质量降序）。英文专用模型遇到非英文内容时按此表挑第一个
# 已下载的做替代——用户手上有 large-v3 就用 large-v3，只有 tiny 就用 tiny。
_WHISPER_ML_ORDER = ('large-v3', 'large-v2', 'large', 'medium', 'small', 'base', 'tiny')


def whisper_is_en_only(name):
    """该模型是不是「只认英文」：distil-* 蒸馏系列 + *.en 变体。

    faster-whisper 的 distil-large-v3 官方只发布了 en 版本
    （Systran/faster-distil-whisper-large-v3 就是英文专用），没有中文词表。
    把中文音频喂进去，解码器会「照着英文往下猜」，输出一串听上去像样的英文
    幻觉：实测「这是好的几说是比较复杂」→「that's, I'm just I'm not yet,
    you know」。流程不报错、进度正常、结果有文本，最容易被当成偶发 bug 放过。
    强制 language='zh' 也救不回来——模型本身没有那个词表。"""
    n = str(name or '').strip().lower()
    return n.startswith('distil-') or n.endswith('.en')


def whisper_ml_fallback():
    """给英文专用模型挑一个可用的多语言替代（优先已下载的，避免现场下 1.5G）。"""
    try:
        refresh_whisper_models()
        for m in _WHISPER_ML_ORDER:
            if (_WHISPER_MODELS.get(m) or {}).get('downloaded'):
                return m
    except Exception:
        pass
    return 'base'          # 谁都没下载：base 只要 ~140MB，首次自动拉取


def whisper_model_for_language(model=None, language=None):
    """把「配置的模型」和「本次要识别的语言」对齐，返回真正该加载的模型名。

    · 语言明确不是英文（zh / ja / ko …）→ 英文专用模型必然产出垃圾，换多语言模型。
    · 语言为 auto（'' / 'auto' / None）→ 同样换：自动判别等于「任何语言都可能」，
      宁可英文精度略降，也不能让非英文片整片变乱码。
    · 明确要识别英文（en）→ 保持原模型（distil 在英文上又快又准，这是它的主场）。

    model / language 都是「本次调用的覆盖值」，不传则走全局配置。"""
    m = model or whisper_model_name()
    lang = str(language or '').strip().lower()
    if lang in ('', 'auto', 'none'):
        lang = str(whisper_language() or '').strip().lower()
    if whisper_is_en_only(m) and lang != 'en':
        return whisper_ml_fallback()
    return m


def whisper_language():
    """读取识别语言，默认 'auto'（由模型自动判别）。

    【为什么默认 auto】旧默认是 'zh'：英文/日文片会被硬按中文解码，模型「顺着中文
    往下猜」，输出成串翻译腔幻觉（实测『我跟他们说』×12、『林顿省警察受伤』×4），
    再被剧情理解层当台词证据 → 解说从源头就歪。影视解说的片源语言本来就杂，
    默认交给模型判别更稳；判别结果会写进 warnings，用户看得到。
    要固定语言就配 ai_config.json → whisper.language = "zh" / "en" / "ja" …"""
    cfg = load_ai_config().get('whisper') or {}
    lang = str(cfg.get('language') or 'auto').strip().lower()
    if not lang:
        return 'auto'
    return lang

def whisper_models_dir():
    """faster-whisper 模型权重统一缓存到项目 models/whisper，方便引导用户管理/查看。"""
    return os.path.join(HERE, 'models', 'whisper')

refresh_whisper_models()

def _aborted():
    """协作式取消：检查当前任务线程是否被用户取消。
    分析/解说稿生成/配音等无 ffmpeg 的长阶段，ffmpeg_run 感知不到 abort 标志，
    需要在阶段间调用本函数并主动抛 AbortError，让「取消」按钮秒级生效。
    非任务线程（测试/主线程）无 runid 绑定，恒为 False。"""
    rid = getattr(_TLS, 'runid', None)
    if not rid:
        return False
    p = PROGRESS.get(rid)
    return bool(p and p.get('abort'))

_CUDA_CACHE = {'val': None}

def _cuda_runtime_present():
    """检测 CTranslate2 GPU 推理真正需要的 CUDA 运行时库是否在进程可加载范围内。

    nvidia-smi 只证明「驱动在」，不证明「CUDA 库在」。ctranslate2 走 cuBLAS，
    缺库时首次推理才抛 `Library cublas64_12.dll is not found`——那时整条 ASR 已经
    被上层 except 吞掉，用户只看到「没识别出台词」。这里提前探一次：
      1) 先看 ctranslate2 自带的 dll 目录（pip 装了 nvidia-cublas-cu12 就会落在这）；
      2) 再看系统 PATH 里的 CUDA Toolkit。
    两处都没有就老实走 CPU，别让GPU 空转。"""
    # 探测前先确保 pip 装的 CUDA 库目录已注册进 DLL 搜索路径，
    # 否则「文件在磁盘上但加载不到」会被误判成不可用，白白放弃 GPU。
    _ensure_cuda_dll_path()
    import glob
    import site
    roots = [os.path.dirname(os.path.abspath(__file__))]
    try:
        roots.extend(site.getsitepackages())
        roots.append(site.getusersitepackages())
    except Exception:
        pass
    for root in roots:
        for pat in ('nvidia/cublas/bin/cublas64_12.dll',
                    'nvidia/cudnn/bin/cudnn64_9.dll',
                    'nvidia/cublas/bin/cublas64_11.dll',
                    '**/cublas64_12.dll', '**/cudnn64_9.dll'):
            if glob.glob(os.path.join(root, *pat.split('/')), recursive=True):
                return True
    for name in ('cublas64_12.dll', 'cudnn64_9.dll', 'cublas64_11.dll'):
        for p in (os.environ.get('PATH') or '').split(os.pathsep):
            if p and os.path.exists(os.path.join(p, name)):
                return True
    return False

_GPU_VRAM_CACHE = {'val': None}


def gpu_vram_gb():
    """本机 NVIDIA 显存（GB，浮点）；探测不到返回 0.0。结果缓存（硬件在进程期内不变）。"""
    if _GPU_VRAM_CACHE['val'] is not None:
        return _GPU_VRAM_CACHE['val']
    val = 0.0
    try:
        import subprocess
        r = subprocess.run(['nvidia-smi', '--query-gpu=memory.total', '--format=csv,noheader,nounits'],
                           capture_output=True, text=True, timeout=5, creationflags=_NO_WIN)
        if r.returncode == 0 and r.stdout.strip():
            val = round(int(r.stdout.strip().splitlines()[0].strip()) / 1024.0, 1)
    except Exception:
        pass
    _GPU_VRAM_CACHE['val'] = val
    return val


# 文字模型「驻留显存需求」（GB）= 权重文件大小 + 运行时开销（KV cache / 计算缓冲 / CUDA 上下文）。
#
# 【口径说明，别混淆两个数】
#   · Ollama /api/tags 的 `size` = **磁盘上模型文件多大**（十进制 GB）。
#     实测对账：qwen3:14b-q4_K_M 报 9.28GB ↔ 磁盘 blob 8.64GiB；minicpm-v4.5 报 6.12GB
#     ↔ 主模型 5.03GB + 视觉投影 1.10GB。
#   · **驻留显存**（/api/ps 的 size_vram）是另一个数：Q4 权重被原样搬进显存，所以
#     ≈ 文件大小，再加 KV cache（随上下文长度增长）+ 计算缓冲 + CUDA 上下文。
#   · 下面这张表是「要把它和另一个模型同时塞进显存」所需的**最低显存**，
#     所以取值比裸文件大小更保守（留出运行时开销与共存空间）。
#
# 实测依据（RTX3060 **12GB** + 16GB 内存，Ollama 0.40.11，2026-10-26）：
#   qwen3:14b-q4_K_M 权重 9.28GB → 12GB 卡只剩 ~2.7GB 给 KV cache 与计算缓冲，
#   实测单次调用 47~215 秒，且间歇性 llama-server 崩溃（exit 0xe06d7363）；
#   再叠加 MiniCPM-V 4.5(6.12GB) 合计 15.4GB > 12GB → 两模型互相驱逐 → 每次重读
#   9.28GB 权重 → 磁盘 I/O 打满 + 16GB 内存页缓存抖动 → 整机卡死。
#   → 所以 **12GB 档不再推荐 14B**：改成 8B（与视觉模型合计 11.3GB，可共存）。
TEXT_MODEL_VRAM = {
    'qwen3:30b-q4_K_M': 24.0,
    'qwen3:14b-q4_K_M': 16.0,
    'qwen2.5:14b': 16.0,
    'qwen3:8b': 8.0,
    'qwen2.5:7b': 8.0,
    'qwen2.5:3b': 4.0,
}
VLM_MODEL_VRAM = {
    'qwen3-vl:30b': 20.0,
    'minicpm-v4.5': 6.5,
    'qwen3-vl:8b': 6.1,
    'qwen2.5vl:latest': 5.6,
    'qwen3-vl:4b': 3.5,
}


def recommend_text_model(vram_gb=None, with_vlm=True):
    """按显存推荐「写解说稿」用的文字模型。返回 (tag, 说明)。

    关键：文字模型通常要与**视觉模型共存**（一个看图、一个写稿），所以不能只看
    「模型能不能单独装下」，还要给视觉模型留出显存——否则两个大模型互相驱逐，
    表现就是「跑长视频时整机卡死」（实测 12GB 卡上 14B + MiniCPM 的真实故障）。
    """
    v = gpu_vram_gb() if vram_gb is None else float(vram_gb or 0)
    if v >= 24:
        return 'qwen3:30b-q4_K_M', '≥24GB 独显：可跑 30B MoE，写稿最强'
    if v >= 16:
        return 'qwen3:14b-q4_K_M', ('≥16GB 独显：14B(约9.3GB) 可用，'
                                    '与 MiniCPM-V 4.5(约6.1GB) 合计约 15.4GB，刚好共存')
    if v >= 12:
        return 'qwen3:8b', ('12GB 独显：推荐 8B(约5.2GB)。**不要用 14B**——权重 9.3GB 会与'
                            '视觉模型抢显存，实测单次调用 47~215 秒、甚至 llama-server 崩溃')
    if v >= 10:
        return 'qwen2.5:7b', '10GB 独显：推荐 7B(约4.7GB)，配 4B 视觉模型'
    if v >= 8:
        return 'qwen2.5:3b', '8GB 独显：文字模型用 3B(约2GB) 速度优先；配 4B 视觉模型'
    if v >= 6:
        return 'qwen2.5:3b', '6GB 独显：文字模型用 3B(约2GB)，视觉模型用 4B'
    return 'qwen2.5:3b', ('无独显/纯 CPU：只能跑最小模型，建议改用云端 chat API 写稿，'
                          '本地模型只做兜底')


def _cuda_available():
    """轻量检测 NVIDIA GPU 是否可用于推理：既要有驱动（nvidia-smi / torch），
    也要有 CUDA 运行时库（cublas/cudnn）。两者齐备才算可用。
    原因见 _cuda_runtime_present()：只有驱动没有库时，GPU 推理必崩且错误被上层吞掉。
    结果缓存：硬件/环境在进程生命周期内不变。"""
    if _CUDA_CACHE['val'] is not None:
        return _CUDA_CACHE['val']
    result = False
    try:
        import subprocess
        r = subprocess.run(['nvidia-smi', '--query-gpu=memory.total', '--format=csv,noheader,nounits'],
                           capture_output=True, text=True, timeout=5, creationflags=_NO_WIN)
        if r.returncode == 0 and r.stdout.strip():
            result = True
    except Exception:
        pass
    if not result:
        try:
            import torch
            if getattr(torch, 'cuda', None) is not None and torch.cuda.is_available():
                result = True
        except Exception:
            pass
    if result:
        result = _cuda_runtime_present()
    _CUDA_CACHE['val'] = result
    return result

def _fmt_hms(sec):
    sec = max(0.0, float(sec or 0))
    m, s = int(sec // 60), int(sec % 60)
    return '%d:%02d' % (m, s)

def _strip_think(text):
    """剥离 Qwen3 等混合思考模型输出中的 <think>…</think> 段（含未闭合残段），
    保证解说稿不被思考文本污染。无思考段时原样返回。"""
    import re as _re
    if not text:
        return text
    out = _re.sub(r'<think>.*?</think>', '', text, flags=_re.S)
    out = _re.sub(r'<think>.*$', '', out, flags=_re.S)   # 未闭合残段：丢弃其后全部内容
    return out.strip()

# ---------------------------------------------------------------------------
# S7 · GPU 资源隔离（本地推理全局互斥）
# ---------------------------------------------------------------------------
# 背景：12GB 显存无法同时驻留 VLM(约6.5G) 与 14B LLM(约9G)；两个任务并发时
# Ollama 会反复换模型（比串行更慢）。方案：所有「本地推理」入口共用一把进程级
# 互斥锁 —— 同一时刻只有一个任务在推理；网络远端（云端 API / 远程 Ollama）与
# CPU 转写（Whisper device=cpu）不占用本地显存，不加锁、可并行。
_GPU_LOCK = threading.RLock()   # RLock：防同一线程内重入死锁


def _is_local_url(url):
    """判断 base_url 是否指向本机（localhost/127.0.0.1/::1）。远程 Ollama/云端不加 GPU 锁。"""
    host = (url or '').split('://', 1)[-1].split('/', 1)[0].split(':', 1)[0].lower()
    return host in ('localhost', '127.0.0.1', '::1', '0.0.0.0', '')


def _gpu_slot():
    """本地推理互斥槽：with _gpu_slot(): ... 期间独占本地推理资源。
    仅用于「本地」模型调用（见各调用点注释）；远端请求不应进入。"""
    import contextlib
    @contextlib.contextmanager
    def _inner():
        with _GPU_LOCK:
            yield
    return _inner()
