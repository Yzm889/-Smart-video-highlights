# -*- coding: utf-8 -*-
"""引擎/工具模块（由 webui_server.py 第4批拆分生成，符号与拆分前等价）。"""
import os, time
# ---- 跨模块依赖（拆分后显式导入） ----
from ai_providers import load_ai_config, _gpu_slot
from ffmpeg_utils import _TLS, _concat_audio_clips, ffmpeg_run
from text_utils import _split_long_text, _strip_tts_markup
import tts2_engines as T2   # v1.4.16 新一代配音引擎（VoiceDesign / IndexTTS2）

import json
import queue
import re
import subprocess
import sys
import threading

# Windows 下从 GUI 进程起控制台子进程会新建一个黑框；本模块多处逐句/探测调用
# 会在出片期连成一片闪跳。与 ffmpeg_utils.py / tts2_engines.py 保持同一写法：
# 非 Windows 取不到该常量时为 0，无副作用。
_NO_WIN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
# ---- 熔断 / 降权 / 引擎缓存 / 模型表（原 webui_server 全局，随引擎层迁入，webui 经 re-export 共享同一对象） ----
HERE = os.path.dirname(os.path.abspath(__file__))
_EDGE_RETRY = 3
_EDGE_RETRY_SLEEP = 1.2
_EDGE_MAX_FAILS = 4
_EDGE_DEAD_SECONDS = 120
_EDGE_RUN_DOWNGRADE = 2
# 只探 edge 真正依赖的朗读服务端点。曾加入 login.live.com 作备选，
# 实测该域名能 TLS 握手而朗读服务被拦，导致「任一通即可用」误判，
# 每段仍旧卡满超时——探测必须对准实际用的那个域名。
_EDGE_HOSTS = ('speech.platform.bing.com',)
# edge-tts 单个子进程超时。原值 180s 在服务端连不上时会每轮跑满超时：
# 3 轮重试 × 数十段 = 卡死几十分钟（长视频可达数小时），表现为「合成假死」。
# 一句解说词（≤200 字）正常 1~3s 出结果，25s 留足网络抖动余量。
_EDGE_SUBPROC_TIMEOUT = 25
_EDGE_STATE = {'fails': 0, 'dead_until': 0.0, 'reason': '', 'probe_tick': 0}
_CHATTS = {'model': None, 'loading': False, 'error': ''}
_COSYVOICE = {'model': None, 'loading': False, 'error': '', 'voice': '中文女'}
# CosyVoice 是否参与 auto/edge 等引擎的「兜底链」。当前为 False：venv 子进程
# 每句重载 9GB 模型（~100s/句），进兜底链会让 edge 一抽风就整条解说龟速
# （2026-11-06 实测）。用户仍可在设置里显式选 CosyVoice 引擎。
# 常驻 worker（模型加载一次、句句复用）落地后翻回 True。
# v1.4.14：常驻 worker 已落地（首句约 30s、之后秒级），可以回归自动兜底链了。
# 但必须由 cosyvoice_resident_ready() 守卫：venv / 模型 / worker 三件套不齐时
# 仍会逐句起子进程重载 9GB 模型（实测 ~100s/句），一次 edge 抖动就能把整条
# 解说拖进百秒一句的慢坑。用户显式选 cosyvoice 不走这个守卫。
_COSY_IN_FALLBACK_CHAIN = True
COSYVOICE_REPO_DIR = os.path.join(HERE, 'CosyVoice')
COSYVOICE_MODEL_DIR = os.path.join(HERE, 'models', 'cosyvoice', 'CosyVoice2-0.5B')
COSYVOICE_VENV_PY = os.path.join(HERE, '.venv_cosyvoice', 'Scripts', 'python.exe')
SHERPA_TTS_MODELS = {
    'melo-zh': {
        'name': 'vits-melo-tts-zh_en',
        'label': 'MeloTTS 中文（自然·推荐）',
        'url': 'https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/vits-melo-tts-zh_en.tar.bz2',
    },
    'piper-huayan': {
        'name': 'vits-piper-zh_CN-huayan-medium',
        'label': '华研 · 女声（轻量·机械感较重）',
        'url': 'https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/vits-piper-zh_CN-huayan-medium.tar.bz2',
    },
}
SHERPA_DEFAULT_MODEL = 'melo-zh'
_SHERPA_TTS = {'obj': None, 'key': None}
_EMOTION_MAP = {
    '欢快': 'cheerful', '开心': 'cheerful', '激动': 'cheerful', '兴奋': 'cheerful',
    '悲伤': 'sad', '难过': 'sad', '伤感': 'sad',
    '严肃': 'serious', '郑重': 'serious', '沉稳': 'serious',
    '紧张': 'fearful', '恐惧': 'fearful', '惊险': 'fearful',
    '温柔': 'gentle', '柔和': 'gentle',
    '深情': 'affectionate', '动情': 'affectionate',
    '播报': 'newscast', '解说': 'newscast', '正式': 'newscast',
    '不满': 'disgruntled', '愤怒': 'angry', '生气': 'angry',
    '恐惧': 'fearful', '害怕': 'fearful',
}
_EMOTION_VOICES = {
    'zh-CN-XiaoxiaoNeural': list(_EMOTION_MAP.values()),
    'zh-CN-YunxiNeural': ['cheerful', 'sad', 'angry', 'serious', 'gentle', 'newscast'],
    'zh-CN-XiaoyiNeural': ['cheerful', 'sad', 'gentle', 'serious'],
    'zh-CN-YunjianNeural': [],
}


def tts_local_cfg():
    """本地配音配置 {engine, voice, rate, cosy_voice, instruct}。

    engine ∈ auto|qwen3tts|indextts2（默认链，见 _auto_order()）
             | edge|cosyvoice|chattts|sherpa|sapi（旧引擎，显式选用仍可用）。
    instruct 是音色描述：Qwen3-TTS VoiceDesign 拿它造音色、IndexTTS2 拿它造参考音；
    空串表示用 tts2_engines.DEFAULT_INSTRUCT。
    """
    c = load_ai_config().get('tts_local') or {}
    rate = str(c.get('rate') or '+0%').strip()
    if rate and not rate.startswith(('+', '-')):
        rate = '+' + rate.replace('%', '') + '%'
    return {'engine': str(c.get('engine') or 'auto').lower(),
            'voice': str(c.get('voice') or 'zh-CN-XiaoxiaoNeural').strip(),
            'rate': rate or '+0%',
            'cosy_voice': str(c.get('cosy_voice') or '中文女').strip(),
            'instruct': str(c.get('instruct') or '').strip()}

# ---------------------------------------------------------------------------
# 🎬 电影解说引擎：分段→ASR台词→解说稿→时间轴→配音→混音→字幕
# 省流(本地离线)默认：faster-whisper 本地识别真实台词(可 GPU 加速) + SAPI 免费配音 + 真实台词当解说（0 元、不调 API）；可切智能(AI)模式
# ---------------------------------------------------------------------------
def sapi_tts(text, out_path):
    """Windows SAPI 免费中文配音（zh-CN 语音）。成功返回 True。"""
    try:
        import subprocess as _sp
        safe = text.replace("'", "''")
        ps = ("Add-Type -AssemblyName System.Speech; "
              "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
              "$v = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'zh*' } | Select-Object -First 1; "
              "if($v){ $s.SelectVoice($v.VoiceInfo.Name) }; "
              "$s.Rate = 0; "
              f"$s.SetOutputToWaveFile('{out_path}'); "
              f"$s.Speak('{safe}'); $s.Dispose()")
        _sp.run(['powershell', '-NoProfile', '-Command', ps], capture_output=True, timeout=90)
        return os.path.exists(out_path) and os.path.getsize(out_path) > 1000
    except Exception:
        return False

def _tls_edge_state():
    """[2.4] 配音任务线程的 edge 熔断状态（per-runid / per-thread，参照 _TLS 既有模式）。
    只有配音线程主动在此建立状态；其余线程（前端查询等）走全局 _EDGE_STATE 兜底。"""
    s = getattr(_TLS, 'edge_state', None)
    if s is None:
        s = {'fails': 0, 'dead_until': 0.0, 'reason': '', 'probe_tick': 0}
        _TLS.edge_state = s
    return s

def _edge_internal():
    """返回当前线程应读写的熔断状态：配音线程用线程级（隔离），其他线程用全局（兜底）。"""
    return getattr(_TLS, 'edge_state', None) or _EDGE_STATE

def edge_tts_dead_reason():
    """返回 edge-tts 当前被熔断的原因（未熔断返回 ''）。"""
    _s = _edge_internal()
    if time.time() < _s['dead_until']:
        return _s['reason'] or '连续失败'
    return ''

_EDGE_INSTALLED_CACHE = {'val': None, 'ts': 0}
_EDGE_INSTALLED_TTL = 300.0   # 秒；见 _edge_tts_installed 里的 T0' 探针说明
# 探测锁：并发 worker 在缓存失效时会各自起 25s 子进程 / TLS 握手，
# 用一把锁 + 双重检查，保证同一时刻只有一个线程在探测，其余等待复用结果。
_EDGE_PROBE_LOCK = threading.Lock()

def _edge_tts_installed():
    """edge-tts 是否已安装（模块或命令行）；结果缓存 30 秒避免重复 find_spec/subprocess。"""
    now = time.time()
    # TTL 30s→300s：T0' 探针实测 edge-tts --version 每约 45s 被重探一次，
    # 12 分钟拉起 14 个 conhost.exe（主人肉眼可见的闪窗之一）。安装状态在一次
    # 出片内几乎不会变，缓存拉长不改变任何判定语义，只砍掉重复子进程。
    if _EDGE_INSTALLED_CACHE['val'] is not None and (now - _EDGE_INSTALLED_CACHE['ts']) < _EDGE_INSTALLED_TTL:
        return _EDGE_INSTALLED_CACHE['val']
    with _EDGE_PROBE_LOCK:
        # 双重检查：拿到锁时别的线程可能已探测完
        if _EDGE_INSTALLED_CACHE['val'] is not None and (time.time() - _EDGE_INSTALLED_CACHE['ts']) < _EDGE_INSTALLED_TTL:
            return _EDGE_INSTALLED_CACHE['val']
        result = False
        try:
            import importlib.util as _u
            if _u.find_spec('edge_tts') is not None:
                result = True
        except Exception:
            pass
        if not result:
            try:
                r = subprocess.run(['edge-tts', '--version'], capture_output=True, timeout=25,
                                   creationflags=_NO_WIN)
                result = (r.returncode == 0)
            except Exception:
                result = False
        _EDGE_INSTALLED_CACHE['val'] = result
        _EDGE_INSTALLED_CACHE['ts'] = time.time()
        return result

_EDGE_NET_CACHE = {'ok': None, 'at': 0.0}


def _edge_reachable(ttl=60.0):
    """edge-tts 依赖的微软朗读服务是否可达（轻量 TCP 探测，结果缓存 ttl 秒）。

    只检查「装没装」是不够的：edge-tts 是联网服务，断网/墙/代理拦截时
    模块在、服务不通，edge_tts_speak 每次都要跑满 _EDGE_RETRY 轮才失败，
    _generate_all_tts 还会据此启用并发路径，白白浪费几十秒。
    这里先探一次 TCP（不做 TLS 握手，够快），不通就直接走本地引擎。"""
    import time as _t
    now = _t.time()
    c = _EDGE_NET_CACHE
    if c['ok'] is not None and now - c['at'] < ttl:
        return c['ok']
    with _EDGE_PROBE_LOCK:
        # 双重检查：拿到锁时别的线程可能已探测完
        if c['ok'] is not None and _t.time() - c['at'] < ttl:
            return c['ok']
        ok = False
        try:
            import socket
            import ssl
            ctx = ssl.create_default_context()
            for host in _EDGE_HOSTS:
                try:
                    # 必须做 TLS 握手而不是只建 TCP：实测本机能连上 443（TCP OK）
                    # 但 TLS 握手被拦，只探 TCP 会误判「edge 可用」，
                    # 结果每段都去跑满子进程超时，长视频直接卡死。
                    with socket.create_connection((host, 443), timeout=3) as s:
                        with ctx.wrap_socket(s, server_hostname=host):
                            ok = True
                            break
                except Exception:
                    continue
        except Exception:
            pass
        c['ok'], c['at'] = ok, _t.time()
        return ok


def edge_tts_available():
    """edge-tts 是否可用：模块已装 + 不在熔断期 + 服务端可达。

    三项缺一不可——只查模块会造成「可用但实际次次失败」的假阳性，
    让每段配音都白等重试，最后还静悄悄掉到 SAPI。"""
    if edge_tts_dead_reason():
        return False
    if not _edge_tts_installed():
        return False
    return _edge_reachable()

def _edge_note_failure(reason=''):
    """记一次「整轮重试后仍失败」。达到阈值才熔断。
    [2.4] 熔断状态改为线程级（配音任务线程），仅作本 run 熔断；无线程态时回退全局兜底。"""
    _s = _edge_internal()
    _s['fails'] += 1
    if _s['fails'] >= _EDGE_MAX_FAILS:
        _s['dead_until'] = time.time() + _EDGE_DEAD_SECONDS
        _s['reason'] = reason or ('连续 %d 次合成失败' % _EDGE_MAX_FAILS)
    return _s['fails']

def edge_tts_reset():
    """手动解除熔断（供界面「重试配音引擎」调用）：网络恢复后不必干等。"""
    _EDGE_STATE.update(fails=0, dead_until=0.0, reason='')   # 全局兜底复位（原值）
    _s = getattr(_TLS, 'edge_state', None)
    if _s is not None:
        _s.update(fails=0, dead_until=0.0, reason='')          # [2.4] 线程级熔断一并复位
    return True

def _chattts_venv_python():
    """返回 ChatTTS venv 的 Python 路径（Python 3.11 + CUDA torch），不存在返回 None。"""
    p = os.path.join(HERE, '.venv_tts', 'Scripts', 'python.exe')
    return p if os.path.exists(p) else None

def chattts_available():
    """ChatTTS 是否可用（主进程已加载，或 venv 子进程可用）。"""
    if _CHATTS['model'] is not None:
        return True
    if _chattts_venv_python():
        return True
    if _CHATTS['error']:
        return False
    try:
        import importlib.util as _u
        return _u.find_spec('ChatTTS') is not None
    except Exception:
        return False

def chattts_load(progress=None):
    """加载 ChatTTS 模型（首次调用较慢，约 30-60 秒）。成功返回 True。"""
    if _CHATTS['model'] is not None:
        return True
    if _CHATTS['loading']:
        return False
    _CHATTS['loading'] = True
    _CHATTS['error'] = ''
    try:
        import importlib.util
        if importlib.util.find_spec('torch') is None:
            raise ImportError('ChatTTS 需要 torch（未安装）')
        import ChatTTS
        if progress:
            progress['phase'] = '加载 ChatTTS 模型'
            progress['pct'] = 5
        model = ChatTTS.Chat()
        model.load(compile=False)  # compile=False 避免 Windows 编译问题
        _CHATTS['model'] = model
        return True
    except Exception as e:
        _CHATTS['error'] = str(e)[:300]
        return False
    finally:
        _CHATTS['loading'] = False

def chattts_speak(text, out_path, progress=None):
    """用 ChatTTS 合成语音。支持标记中的停顿（[uv_break]/[lbreak]）。成功返回 True。
    优先主进程内推理（如果装了 torch），否则用 venv Python 3.11 子进程。"""
    if not (text or '').strip():
        return False
    chat_text = markup_to_chattts_text(text)

    # 方式1：主进程内推理（需要当前 Python 有 torch + ChatTTS）
    if chattts_load(progress):
        model = _CHATTS['model']
        if model is not None:
            try:
                with _gpu_slot():
                    wavs = model.infer(chat_text, use_decoder=True, do_sample=True,
                                       temperature=0.3, top_P=0.7, top_K=20)
                    if wavs and len(wavs) > 0:
                        import torchaudio
                        import torch
                        wav = wavs[0]
                        if isinstance(wav, torch.Tensor):
                            if wav.dim() == 1:
                                wav = wav.unsqueeze(0)
                            torchaudio.save(out_path, wav.cpu(), 24000)
                        else:
                            import numpy as np
                            import soundfile as sf
                            sf.write(out_path, np.array(wav[0]), 24000)
                    return os.path.exists(out_path) and os.path.getsize(out_path) > 1000
            except Exception as e:
                _CHATTS['error'] = str(e)[:300]

    # 方式2：venv 子进程（Python 3.11 + CUDA torch）
    venv_py = _chattts_venv_python()
    if venv_py:
        try:
            d = os.path.dirname(os.path.abspath(out_path))
            if d:
                os.makedirs(d, exist_ok=True)
            txt_file = out_path + '.txt'
            with open(txt_file, 'w', encoding='utf-8') as f:
                f.write(chat_text)
            worker = os.path.join(HERE, 'chattts_worker.py')
            with _gpu_slot():
                r = subprocess.run([venv_py, worker, txt_file, out_path],
                                   capture_output=True, timeout=300, cwd=HERE,
                                   creationflags=_NO_WIN)
            try:
                os.unlink(txt_file)
            except Exception:
                pass
            if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
                return True
            _CHATTS['error'] = (r.stderr or b'').decode('utf-8', 'ignore')[-300:]
        except Exception as e:
            _CHATTS['error'] = str(e)[:300]
    return False

def _cosyvoice_venv_python():
    """返回 CosyVoice venv 的 Python 路径，不存在返回 None。"""
    return COSYVOICE_VENV_PY if os.path.exists(COSYVOICE_VENV_PY) else None

def _find_cosyvoice_python():
    """查找支持PyTorch的Python（3.11或3.12），返回python.exe路径，找不到返回None。
    PyTorch不支持Python 3.14，必须用3.11/3.12创建venv。"""
    candidates = [
        os.path.join(os.environ.get('APPDATA', ''), 'uv', 'python', 'cpython-3.11-windows-x86_64-none', 'python.exe'),
        os.path.join(os.environ.get('APPDATA', ''), 'uv', 'python', 'cpython-3.12-windows-x86_64-none', 'python.exe'),
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'Python', 'Python311', 'python.exe'),
        os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'Python', 'Python312', 'python.exe'),
        r'C:\Python311\python.exe',
        r'C:\Python312\python.exe',
    ]
    # 也从ChatTTS venv反查
    chattts_venv = os.path.join(HERE, '.venv_tts', 'Scripts', 'python.exe')
    if os.path.exists(chattts_venv):
        try:
            r = subprocess.run([chattts_venv, '-c', 'import sys; print(sys.base_prefix)'],
                               capture_output=True, text=True, timeout=10,
                               creationflags=_NO_WIN)
            if r.returncode == 0:
                base = r.stdout.strip()
                candidates.insert(0, os.path.join(base, 'python.exe'))
        except Exception:
            pass
    for p in candidates:
        if p and os.path.exists(p):
            try:
                r = subprocess.run([p, '--version'], capture_output=True, text=True, timeout=10,
                                   creationflags=_NO_WIN)
                ver = r.stdout.strip()
                if '3.11' in ver or '3.12' in ver:
                    return p
            except Exception:
                continue
    return None

def cosyvoice_available():
    """CosyVoice 是否可用（主进程已加载，或 venv 子进程可用且模型已下载）。"""
    if _COSYVOICE['model'] is not None:
        return True
    if _cosyvoice_venv_python() and os.path.isdir(COSYVOICE_MODEL_DIR):
        # 检查模型目录有关键文件
        try:
            files = os.listdir(COSYVOICE_MODEL_DIR)
            if any(f.endswith('.pt') or f.endswith('.onnx') or f == 'config.yaml' for f in files):
                return True
        except Exception:
            pass
    if _COSYVOICE['error']:
        return False
    try:
        import importlib.util as _u
        if _u.find_spec('cosyvoice') is not None and os.path.isdir(COSYVOICE_MODEL_DIR):
            return True
    except Exception:
        pass
    return False


def cosyvoice_resident_ready():
    """CosyVoice 常驻 worker 三件套齐全（venv python / 模型目录 / worker 脚本）。

    只有满足时才允许把 CosyVoice 放进**自动兜底链**：否则 venv 单发子进程会
    逐句重载 9GB 模型（实测 ~100s/句），edge 一次网络抖动就能把整条解说拖成
    一小时。用户显式把引擎选成 cosyvoice 时不走这个守卫（自己选的自己认）。"""
    return bool(_cosyvoice_venv_python()
                and os.path.isdir(COSYVOICE_MODEL_DIR)
                and os.path.exists(os.path.join(HERE, 'cosyvoice_worker.py')))


def _auto_order():
    """auto 模式（v1.4.20 起：克隆优先，VoiceDesign 退回「造音色」专职）。

    旧版把 qwen3tts 排第一，于是整片每一句都独立 VoiceDesign 一次 —— 每次都按
    文字重新采样一个发音人，实测组内基频离散 16.8%~19.3%（贪心解码也 17.5%），
    听感就是「一句一个人」。用户 2026-11-09 实跑「惩罚者前二十分钟」当场指出。

    现在反过来：IndexTTS2 拿**一条固定的参考音**做零样本克隆（离散 1.4%），
    参考音由 Qwen3 按用户的音色描述造一次并缓存（见 T2.voice_ref）。两个引擎都
    不可用时 local_tts_speak 会明确失败，由前端提示安装。"""
    return ['indextts2', 'qwen3tts']


def _first_available_engine(order):
    """按顺序返回第一个真正能用的引擎名（子句切分 / 标签 / 自检共用）。"""
    for e in order:
        if e == 'qwen3tts':
            if T2.qwen3tts_ready():
                return e
        elif e == 'indextts2':
            if T2.indextts2_ready():
                return e
        elif e == 'edge':
            if edge_tts_available():
                return e
        elif e == 'cosyvoice':
            if cosyvoice_available():
                return e
        elif e == 'chattts':
            if chattts_available():
                return e
        elif e == 'sherpa':
            if sherpa_tts_available():
                return e
        elif e == 'sapi':
            return e
    return None


# ---- CosyVoice 常驻 worker（模型加载一次、句句复用）----
# 逐句起新进程要每次重载 9GB 模型（实测 ~100s/句，一条 30 句解说 TTS 要近一小时）；
# 常驻后仅首句付加载成本，之后每句秒级。进程挂掉由下一次调用自动重启。
_COSY_SERVE = {'proc': None, 'q': None, 'lock': threading.Lock()}


def _cosy_serve_reader(proc, q):
    """后台线程：把 worker 的 stdout 行解析成 JSON 推进队列；进程结束推 _eos 哨兵。
    worker 加载/推理时 modelscope 等库会往 stdout 打进度噪音（非 JSON），静默跳过——
    协议应答（ready 事件、每请求一行 JSON）都是合法 JSON，不受影响。"""
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            q.put(msg)
    except Exception:
        pass
    q.put({'_eos': True})


def _cosy_serve_start():
    """启动常驻 worker 并等模型 ready。须持有 _COSY_SERVE['lock']。"""
    venv_py = _cosyvoice_venv_python()
    worker = os.path.join(HERE, 'cosyvoice_worker.py')
    if not (venv_py and os.path.isdir(COSYVOICE_MODEL_DIR) and os.path.exists(worker)):
        _COSYVOICE['error'] = 'venv/模型/worker 脚本不齐全'
        return False
    try:
        proc = subprocess.Popen(
            [venv_py, worker, 'serve', COSYVOICE_MODEL_DIR],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=COSYVOICE_REPO_DIR if os.path.isdir(COSYVOICE_REPO_DIR) else HERE,
            text=True, encoding='utf-8', errors='replace', bufsize=1,
            creationflags=_NO_WIN)
    except Exception as e:
        _COSYVOICE['error'] = str(e)[:300]
        return False
    q = queue.Queue()
    threading.Thread(target=_cosy_serve_reader, args=(proc, q), daemon=True).start()
    deadline = time.time() + 600  # 首次冷启动从磁盘换入 9GB 权重，给足 10 分钟
    while time.time() < deadline:
        try:
            msg = q.get(timeout=5)
        except Exception:
            if proc.poll() is not None:
                break
            continue
        if msg.get('event') == 'ready':
            _COSY_SERVE.update(proc=proc, q=q)
            print('[COSYVOICE] 常驻 worker 就绪（加载 %.0fs）' % (msg.get('load_sec') or 0), flush=True)
            return True
        if msg.get('_eos') or msg.get('ok') is False:
            _COSYVOICE['error'] = str(msg.get('error') or 'worker 启动失败')[:300]
            break
    try:
        proc.kill()
    except Exception:
        pass
    _COSY_SERVE.update(proc=None, q=None)
    return False


def _cosy_serve_synth(txt, out_path, ref_wav, prompt_text='', timeout=300):
    """经常驻 worker 合成一句。成功 True；失败时进程已清理，下次调用自动重启。"""
    with _COSY_SERVE['lock']:
        if _COSY_SERVE['proc'] is None and not _cosy_serve_start():
            return False
        proc, q = _COSY_SERVE['proc'], _COSY_SERVE['q']
        if proc is None or proc.poll() is not None:
            _COSY_SERVE.update(proc=None, q=None)
            return False
        try:
            # 同 v1.4.19 的教训：子进程 stdin 在中文系统上是 GBK，裸 UTF-8 会变
            # 乱码，一律 ASCII 转义（\uXXXX）由 json.loads 还原
            proc.stdin.write(json.dumps({'txt': txt, 'out': out_path, 'ref': ref_wav,
                                         'prompt_text': prompt_text or ''}) + '\n')
            proc.stdin.flush()
        except Exception:
            _COSY_SERVE.update(proc=None, q=None)
            return False
        deadline = time.time() + timeout
        while True:
            remain = deadline - time.time()
            if remain <= 0:
                _COSYVOICE['error'] = '常驻 worker 合成超时（%ds）' % timeout
                try:
                    proc.kill()
                except Exception:
                    pass
                _COSY_SERVE.update(proc=None, q=None)
                return False
            try:
                msg = q.get(timeout=min(remain, 10))
            except Exception:
                continue
            if msg.get('_eos'):
                _COSY_SERVE.update(proc=None, q=None)
                return False
            if msg.get('ok'):
                return True
            _COSYVOICE['error'] = str(msg.get('error') or 'worker 返回失败')[:300]
            return False


def _cosyvoice_ref_wav():
    """按配置音色名找参考音频（zero-shot 克隆的音色来源）；找不到返回 None。"""
    _cv = tts_local_cfg().get('cosy_voice') or _COSYVOICE['voice']
    _ref_dir = os.path.join(HERE, 'models', 'cosyvoice', 'voices')
    for cand in (_cv + '.wav', _cv + '.mp3', '中文女.wav', '中文女.mp3'):
        p = os.path.join(_ref_dir, cand)
        if os.path.exists(p):
            return p
    return None


# whisper tiny 常驻缓存（参考音文字稿自动转写用；tiny 仅 75MB、CPU int8 秒级）
_COSY_ASR = {'model': None}


def _cosyvoice_prompt_text(ref_wav):
    """取参考音频的文字稿。zero-shot 克隆必须「音频+文字稿」成对传入：
    官方文档如此要求，且实测缺失时合成出的是乱码音节（2026-11-06 主人反馈
    「乱说无厘头的字」，whisper 反转 cosyvoice.wav 证实为胡言乱语）。

    查找顺序：ref 同名 .txt 侧车文件 → 本地 whisper tiny 自动转写（结果缓存为侧车）。
    返回 '' 表示拿不到，调用方应跳过 CosyVoice（宁可不合成也不出乱码）。
    """
    base = os.path.splitext(ref_wav)[0]
    sidecar = base + '.txt'
    if os.path.exists(sidecar):
        try:
            with open(sidecar, encoding='utf-8') as f:
                t = f.read().strip()
            if t:
                return t
        except Exception:
            pass
    try:
        import numpy as np
        import imageio_ffmpeg
        from faster_whisper import WhisperModel
        # 参考音文字稿质量直接决定克隆发音：tiny 实测转写误差大（主人录音
        # 转出「這如我輕輕地來…雲菜」级别的乱码稿），large-v3 同一段转得干净准确。
        # 优先本机 large-v3（ASR 同款、CPU int8，短参考音加载+识别约 20s、
        # 结果缓存为侧车文件只算一次），机器上没有再退 tiny。
        wdir = os.path.join(HERE, 'models', 'whisper', 'large-v3')
        if not os.path.isdir(wdir):
            wdir = os.path.join(HERE, 'models', 'whisper', 'tiny')
        if not os.path.isdir(wdir):
            return ''
        if _COSY_ASR['model'] is None or _COSY_ASR.get('dir') != wdir:
            _COSY_ASR['model'] = WhisperModel(wdir, device='cpu', compute_type='int8')
            _COSY_ASR['dir'] = wdir
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        raw = subprocess.run([exe, '-i', ref_wav, '-f', 'f32le', '-ac', '1', '-ar', '16000', '-'],
                             capture_output=True, timeout=60,
                             creationflags=_NO_WIN).stdout
        audio = np.frombuffer(raw, dtype=np.float32)
        if audio.size < 1600:
            return ''
        segs, _ = _COSY_ASR['model'].transcribe(audio, language='zh')
        text = ''.join(s.text for s in segs).strip()
        if text:
            try:
                with open(sidecar, 'w', encoding='utf-8') as f:
                    f.write(text)
            except Exception:
                pass
        return text
    except Exception:
        return ''


def cosyvoice_speak(text, out_path, progress=None):
    """用 CosyVoice2-0.5B 合成语音。成功返回 True。
    优先常驻 worker（模型只加载一次），失败回退 venv 单发子进程，再回退主进程内推理。
    不支持{{情绪}}标记，会自动剥离。"""
    if not (text or '').strip():
        return False
    clean_text = _strip_tts_markup(text)
    if not clean_text.strip():
        return False

    # 方式1：常驻 worker（推荐；首句付 ~30s 模型加载，之后句句秒级）
    venv_py = _cosyvoice_venv_python()
    if venv_py and os.path.isdir(COSYVOICE_MODEL_DIR) and os.path.exists(os.path.join(HERE, 'cosyvoice_worker.py')):
        _ref_wav = _cosyvoice_ref_wav()
        if _ref_wav is None:
            _COSYVOICE['error'] = '参考音频不存在，请重新安装CosyVoice'
            print('[COSYVOICE] no ref audio')
            return False
        # zero-shot 必须带参考音文字稿：缺了会合成乱码音节（实测），宁可不合成
        _ref_txt = _cosyvoice_prompt_text(_ref_wav)
        if not _ref_txt:
            _COSYVOICE['error'] = '参考音频缺文字稿（且自动转写失败），已跳过 CosyVoice'
            print('[COSYVOICE] no prompt text for', _ref_wav)
            return False
        d = os.path.dirname(os.path.abspath(out_path))
        if d:
            os.makedirs(d, exist_ok=True)
        # worker 进程的 cwd 是 CosyVoice 仓库目录，相对路径会写错位置
        # （run_dir 等常以 webui_output/... 相对形式传入），必须先转绝对路径
        _abs_out = os.path.abspath(out_path)
        if _cosy_serve_synth(clean_text, _abs_out, _ref_wav, _ref_txt):
            return True
        print('[COSYVOICE] 常驻 worker 不可用，回退单发模式：', _COSYVOICE['error'][:120], flush=True)
        # 方式1b：单发子进程兜底（每句付 ~100s 模型加载，慢但独立可用）
        try:
            txt_file = out_path + '.txt'
            with open(txt_file, 'w', encoding='utf-8') as f:
                f.write(clean_text)
            worker = os.path.join(HERE, 'cosyvoice_worker.py')
            with _gpu_slot():
                r = subprocess.run([venv_py, worker, txt_file, out_path, COSYVOICE_MODEL_DIR, _ref_wav],
                                   capture_output=True, timeout=300,
                                   cwd=COSYVOICE_REPO_DIR if os.path.isdir(COSYVOICE_REPO_DIR) else HERE,
                                   creationflags=_NO_WIN)
            try:
                os.unlink(txt_file)
            except Exception:
                pass
            if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
                return True
            _COSYVOICE['error'] = (r.stderr or b'').decode('utf-8', 'ignore')[-400:]
            print('[COSYVOICE] venv推理失败:', _COSYVOICE['error'][:200])
        except Exception as e:
            _COSYVOICE['error'] = str(e)[:300]

    # 方式2：主进程内推理（需要当前Python有torch+cosyvoice）
    if not _COSYVOICE['loading'] and _COSYVOICE['model'] is None:
        try:
            import importlib.util
            if importlib.util.find_spec('cosyvoice') is not None and os.path.isdir(COSYVOICE_MODEL_DIR):
                _COSYVOICE['loading'] = True
                try:
                    import sys as _sys
                    if os.path.isdir(os.path.join(COSYVOICE_REPO_DIR, 'third_party', 'Matcha-TTS')):
                        _sys.path.insert(0, os.path.join(COSYVOICE_REPO_DIR, 'third_party', 'Matcha-TTS'))
                    from cosyvoice.cli.cosyvoice import CosyVoice2
                    model = CosyVoice2(COSYVOICE_MODEL_DIR, load_jit=False, load_trt=False, fp16=False)
                    _COSYVOICE['model'] = model
                finally:
                    _COSYVOICE['loading'] = False
        except Exception as e:
            _COSYVOICE['error'] = str(e)[:300]
            _COSYVOICE['loading'] = False

    if _COSYVOICE['model'] is not None:
        try:
            with _gpu_slot():
                import torchaudio
                model = _COSYVOICE['model']
                for i, j in enumerate(model.inference_sft(clean_text, _COSYVOICE['voice'], stream=False)):
                    torchaudio.save(out_path, j['tts_speech'], model.sample_rate)
                    break
                return os.path.exists(out_path) and os.path.getsize(out_path) > 1000
        except Exception as e:
            _COSYVOICE['error'] = str(e)[:300]
            print('[COSYVOICE] 主进程推理失败:', _COSYVOICE['error'][:200])
    return False

def edge_tts_speak(text, out_path, voice=None, rate=None):
    """用 edge-tts 合成中文配音（免费、无需 API Key，需能访问微软朗读服务）。成功返回 True。
    支持 SSML 情感/停顿/语速控制：文案中包含 {情绪:xx}/{停顿:n}/{慢} 等标记时自动转 SSML。
    走子进程而非 asyncio，避免与 ffmpeg 子进程/事件循环相互干扰。"""
    if not (text or '').strip():
        return False
    cfg = tts_local_cfg()
    voice = voice or cfg['voice']
    rate = rate or cfg['rate']
    if not str(rate).startswith(('+', '-')):
        rate = '+' + str(rate).replace('%', '') + '%'
    d = os.path.dirname(os.path.abspath(out_path))
    if d:
        os.makedirs(d, exist_ok=True)

    # 检测标记，有标记则用 SSML 模式（情感+停顿+语速），无标记用纯文本
    use_ssml = has_tts_markup(text)
    if use_ssml:
        ssml = markup_to_ssml(text, voice=voice, rate=rate)
        # SSML 模式：写临时文件，用 --ssml 或 python -c 调用
        ssml_file = out_path + '.ssml'
        with open(ssml_file, 'w', encoding='utf-8') as f:
            f.write(ssml)
        tries = [
            [sys.executable, '-c',
             'import asyncio,edge_tts,sys;'
             'async def m():'
             '  c=edge_tts.Communicate(ssml=open(sys.argv[1],encoding="utf-8").read(),voice="%s");'
             '  await c.save(sys.argv[2]);'
             'asyncio.run(m())' % voice,
             ssml_file, out_path],
        ]
    else:
        tries = [['edge-tts', '--voice', voice, '--rate=' + rate, '--text', text,
                  '--write-media', out_path],
                 [sys.executable, '-m', 'edge_tts', '--voice', voice, '--rate=' + rate,
                  '--text', text, '--write-media', out_path]]
    last_err = ''
    # 外层：重试整轮（本机实测单次成功率仅约 2/3，重试后才谈得上「用得上」）
    for attempt in range(_EDGE_RETRY):
        for cmd in tries:
            # SSML模式失败后，自动回退到纯文本模式（剥离TTS标记，避免把"停顿"/"情绪"念出来）
            if use_ssml and attempt >= 1 and 'ssml' in str(cmd):
                clean_text = _strip_tts_markup(text)
                if clean_text:
                    cmd = [sys.executable, '-m', 'edge_tts', '--voice', voice, '--rate=' + rate,
                           '--text', clean_text, '--write-media', out_path]
            try:
                if os.path.exists(out_path):
                    os.unlink(out_path)
            except Exception:
                pass
            try:
                r = subprocess.run(cmd, capture_output=True,
                                   timeout=_EDGE_SUBPROC_TIMEOUT,
                                   creationflags=_NO_WIN)
                if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
                    _edge_internal().update(fails=0, dead_until=0.0, reason='')   # 成功即复位熔断（[2.4] 线程级，无线程态时回退全局）
                    return True
                last_err = (r.stderr or b'').decode('utf-8', 'ignore')[-160:]
            except Exception as e:
                last_err = str(e)[-160:]
                continue
        if attempt < _EDGE_RETRY - 1:
            # 连接被重置多为瞬时抖动，稍等再试；递增间隔避免高频打服务端
            time.sleep(_EDGE_RETRY_SLEEP * (attempt + 1))
    _edge_note_failure(last_err or '合成失败')
    return False

def _speak_short_clause(text, out_path, engine, voice, rate, speed):
    """子句级短 TTS 合成（不带引擎选择/锁定逻辑，直接用指定引擎）。
    用于长文本子句切分后逐句合成。返回 (ok, actual_path)。"""
    clean_text = _strip_tts_markup(text) if engine in ('edge', 'sherpa', 'sapi') else text
    if not clean_text.strip():
        return False, out_path
    stem, _ext = os.path.splitext(out_path)
    if engine == 'edge':
        if edge_tts_speak(clean_text, out_path):
            return True, out_path
    elif engine == 'cosyvoice':
        wv = stem + '_cosyvoice.wav'
        if cosyvoice_speak(clean_text, wv):
            return True, wv
    elif engine == 'chattts':
        wv = stem + '_chattts.wav'
        if chattts_speak(clean_text, wv):
            return True, wv
    elif engine == 'sherpa':
        wv = stem + '_sherpa.wav'
        if sherpa_tts_speak(clean_text, wv, speed=speed):
            return True, wv
    else:  # sapi
        wv = stem + '.wav'
        if sapi_tts(clean_text, wv):
            return True, wv
    return False, out_path

def _tts_run_banned():
    """[2.1] 本 run 内被降权的引擎集合（按 _TLS 线程隔离，per-runid）。"""
    return set(getattr(_TLS, 'tts_downgraded', ()) or ())

def _tts_note_fail(eng):
    """[2.1] 记录某引擎一次「整轮失败」。连续失败达阈值即本 run 内降权到最后兜底，
    替代原「连续 4 段整链熔断 120s」逻辑——每段都会落到 sherpa/SAPI 保证有声。"""
    fails = getattr(_TLS, 'tts_engine_fails', None)
    if fails is None:
        fails = {}
        _TLS.tts_engine_fails = fails
    fails[eng] = fails.get(eng, 0) + 1
    if fails[eng] >= _EDGE_RUN_DOWNGRADE:
        banned = set(getattr(_TLS, 'tts_downgraded', ()) or ())
        banned.add(eng)
        _TLS.tts_downgraded = banned
        print('[DIAG] TTS引擎 %s 连续失败 %d 次，本 run 内降权到最后兜底' % (eng, fails[eng]))

def _tts_note_ok(eng):
    """[2.1] 记录某引擎一次成功：清零其连续失败计数（已降权则 run 内继续降权）。"""
    fails = getattr(_TLS, 'tts_engine_fails', None)
    if fails is not None and fails.get(eng):
        fails[eng] = 0

def _tts_force_downgrade(eng):
    """[2.3] 直接把某引擎在本 run 内降权到最后（引擎自检失败时立即切换）。"""
    banned = set(getattr(_TLS, 'tts_downgraded', ()) or ())
    banned.add(eng)
    _TLS.tts_downgraded = banned
    fails = getattr(_TLS, 'tts_engine_fails', None)
    if fails is not None:
        fails[eng] = fails.get(eng, 0) + 1


# ---------------------------------------------------------------------------
# [task #22] 配音「静默质量降级」可视化
#
# local_tts_speak 的契约是「要么出声、要么明确失败」：首选引擎（edge-tts，情感好）
# 失败后会自动回退到系统 SAPI（机器音），返回值仍是 ok=True——用户只会觉得
# 「这版配音怎么变难听了」，却完全不知道是引擎降级导致的，也无法自助修复
# （装 edge-tts / 点重试配音引擎）。这是最典型的一类「不报错但体验劣化」。
# 这里把「首选 → 实际」的降级记录下来，经 /api/tts/voices 的 degrade_msg 暴露给 UI。
# ---------------------------------------------------------------------------
_ENGINE_LABEL = {
    'edge': 'edge-tts（情感/顿挫最好）',
    'cosyvoice': 'CosyVoice（克隆音色）',
    'chattts': 'ChatTTS',
    'sherpa': '离线模型（断网可用）',
    'sapi': '系统 SAPI（机器音·音质最差）',
}
_TTS_DEGRADE = {'from': None, 'to': None, 'msg': '', 'count': 0}
_TTS_DEGRADE_LOCK = threading.Lock()


def _tts_note_degrade(from_eng, to_eng):
    """记录一次「首选引擎不可用 → 降级到备用引擎」。"""
    if not from_eng or not to_eng or from_eng == to_eng:
        return
    msg = ('⚠️ 首选配音引擎 %s 不可用，已降级为 %s（音质/情感下降；'
           '可点「🔄 重试配音引擎」或在「🔊 本地配音引擎」换引擎）'
           % (_ENGINE_LABEL.get(from_eng, from_eng), _ENGINE_LABEL.get(to_eng, to_eng)))
    with _TTS_DEGRADE_LOCK:
        _TTS_DEGRADE['from'] = from_eng
        _TTS_DEGRADE['to'] = to_eng
        _TTS_DEGRADE['msg'] = msg
        _TTS_DEGRADE['count'] = int(_TTS_DEGRADE.get('count', 0)) + 1
    print('[DIAG] TTS 降级：%s → %s' % (from_eng, to_eng))


def tts_degrade_msg():
    """最近一次配音引擎降级提示；无降级返回 ''（供 /api/tts/voices 暴露给前端）。"""
    with _TTS_DEGRADE_LOCK:
        return _TTS_DEGRADE.get('msg') or ''

def _drop_empty(path):
    """删除 0 字节/极小的失败产物（配音文件残留会让用户误以为「有配音没声音」）。"""
    try:
        if path and os.path.exists(path) and os.path.getsize(path) < 100:
            os.unlink(path)
    except Exception:
        pass


def _ping_preferred_engine(run_dir):
    """[2.3] 配音前对首选引擎做一次「首段试点」：用一句固定文本 ping。
    成功 → 按当前引擎顺序走；失败 → 立即把该引擎在本 run 内降权（切 sherpa/sapi），
    避免几十段每段先试错一次 edge（既省时间又避免无谓累计失败触发降权）。"""
    if not run_dir:
        return
    try:
        eng = (tts_local_cfg() or {}).get('engine') or 'auto'
    except Exception:
        eng = 'auto'
    if eng == 'auto':
        return  # v1.4.16：auto 只含本地新引擎，没有联网 edge 需要端到端试点
    elif eng != 'edge':
        return  # 首选非 edge：各自本地化引擎的可用性检查已足够，无需端到端 ping
    probe_text = '你好，这里是配音引擎自检。'
    probe_out = os.path.join(run_dir, '.engine_probe.mp3')
    try:
        if not edge_tts_available():
            return  # edge 未装/已被全局兜底熔断，后续 local 链自然会跳过 edge
        try:
            ok = edge_tts_speak(probe_text, probe_out)
        except Exception:
            ok = False
        if ok:
            print('[DIAG] TTS引擎自检通过（edge）')
        else:
            print('[DIAG] TTS引擎自检失败：edge 不可用，本 run 内降权（切 sherpa/sapi 兜底）')
            _tts_force_downgrade('edge')
            _edge_note_failure('引擎自检失败')
    finally:
        for _p in (probe_out, probe_out + '.ssml'):
            if _p and os.path.exists(_p):
                try:
                    os.unlink(_p)
                except Exception:
                    pass

def _apply_speed(path, speed):
    """按倍速重写配音文件（ffmpeg atempo，保音高），原地替换同名文件。

    只用 atempo 整数倍以外的单一倍速（0.5~2.0 内 atempo 原生支持），
    1.12 倍这类轻微加速听不出失真。失败就原样返回，绝不让变速拖垮配音。
    """
    try:
        speed = float(speed)
    except Exception:
        return path
    if abs(speed - 1.0) < 0.02 or not path or not os.path.exists(path):
        return path
    _stem, _ext = os.path.splitext(path)
    tmp = path + '.spd' + _ext
    try:
        rc, _o, _e = ffmpeg_run(['-y', '-i', path, '-filter:a', 'atempo=%.4f' % speed, tmp])
    except Exception:
        return path
    if rc == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 1000:
        try:
            os.replace(tmp, path)     # 原地替换：断点续跑按原文件名找得到
            return path
        except Exception:
            return path
    try:
        os.remove(tmp)
    except Exception:
        pass
    return path


def _engine_order_for(eng_set):
    """配置里「选中」的引擎 → 实际尝试顺序。

    【为什么单独抽成函数】以前这段 if/elif 内联在 local_tts_speak 里，
    「显式选中」和「auto」是两条互不相干的分支，于是 v1.4.20 修音色割裂时
    只改了 _auto_order()，显式分支原地不动、悄悄分叉（主人的配置正好是
    engine='qwen3tts'）。抽出来才能用一个断言同时钉住两条路。
    """
    # CosyVoice 进自动兜底链的前提：常驻 worker 三件套齐全（否则 venv 单发
    # 子进程逐句重载 9GB 模型，实测 ~100s/句）。用户显式选 cosyvoice 不受此限。
    _cosy_fb = ['cosyvoice'] if (_COSY_IN_FALLBACK_CHAIN and cosyvoice_resident_ready()) else []
    if eng_set == 'qwen3tts':
        # 【2026-11-10 修音色割裂：显式选 Qwen3 也必须走克隆】
        # 以前这里是 ['qwen3tts', 'indextts2'] —— 首句起就逐句 VoiceDesign 现场造音色，
        # 每条请求重新采样一个发音人（实测组内 F0 离散 16.8%~19.3%，贪心也 17.5%），
        # 听感「一句一个人」。这正是主人报的「音色不稳定，昨天修过又出现」：
        # v1.4.20 只改了 _auto_order()（auto 档），**显式选中引擎走这个直达分支、绕过了它**，
        # 而主人的配置恰好是 engine='qwen3tts'，所以修好的是他没用到的路。
        # 现在与 auto 一致：先让 IndexTTS2 拿「Qwen3 按文字描述造的那一条参考音」克隆
        # （离散 1.4%），Qwen3 退到第二位，只在 IndexTTS2 不可用时才逐句兜底。
        order = ['indextts2', 'qwen3tts']
    elif eng_set == 'indextts2':
        order = ['indextts2', 'qwen3tts']
    elif eng_set == 'edge':
        order = ['edge'] + _cosy_fb + ['chattts', 'sherpa', 'sapi']
    elif eng_set == 'cosyvoice':
        order = ['cosyvoice', 'edge', 'chattts', 'sherpa', 'sapi']
    elif eng_set == 'chattts':
        order = ['chattts'] + _cosy_fb + ['edge', 'sherpa', 'sapi']
    elif eng_set == 'sherpa':
        order = ['sherpa', 'edge'] + _cosy_fb + ['sapi']
    elif eng_set == 'sapi':
        order = ['sapi', 'sherpa', 'edge']
    else:   # auto：本地优先（v1.4.14），顺序统一由 _auto_order() 给出
        order = _auto_order()
    return order


def local_tts_speak(text, out_path):
    """本地免费配音统一入口。

    按配置在 edge-tts / 离线模型 / 系统 SAPI 之间选择，任一失败自动回退下一个，
    保证「要么出声、要么明确失败」，不因某条路不可用就静默无配音。

    【音色一致性】一个任务内一旦选定引擎就锁死在 _TLS 上。否则长解说前半段用
    edge-tts、后半段因网络抖动掉到离线模型，观众会听到明显的音色突变。
    只有锁定引擎彻底失败时才改锁到备用引擎。

    【长文本子句切分】超 200 字符时按标点切分后逐句合成，最后 ffmpeg concat 拼接。
    规避 edge-tts 命令行长度限制和长文本超时问题（根因 3）。子句级复用锁定引擎，
    保证整段音色一致。

    返回 (ok, engine, actual_path)；SAPI/离线输出 wav，故实际路径后缀可能不同。"""
    # 用户自带稿子/无标记文案：在这里注入韵律（只改送往引擎的文本）。
    # 下游 narr/texts 与字幕仍用原文，用户稿子一个字都不动。
    text = prosody_for_tts(text)
    cfg = tts_local_cfg()
    eng_set = cfg['engine']
    order = _engine_order_for(eng_set)
    # 本任务已锁定过引擎 → 优先沿用，其余仍作后备
    locked = getattr(_TLS, 'tts_engine', None)
    if locked in order:
        order = [locked] + [e for e in order if e != locked]
    # [2.1][2.4] 配音任务线程建立线程级熔断状态（per-runid 隔离，任务间互不拖累）；
    # 并应用本 run 内已降权引擎的「排到最后兜底」排序（sherpa/sapi 优先），
    # 而非「连续失败就整链熔断」——任何段落绝不整段静默。
    _tls_edge_state()
    banned = _tts_run_banned()
    if banned:
        order = [e for e in order if e not in banned] + [e for e in order if e in banned]
    if not edge_tts_available() and 'edge' in order and order[0] == 'edge':
        # [2.3] 首选 edge 但已被全局兜底熔断 → 直接排到最后（sherpa/sapi 优先），
        #      避免每段都先白试一次 edge 子进程。
        order = [e for e in order if e != 'edge'] + ['edge']
    # [task #22] 首选引擎：本轮「真正能用」的第一个（已含锁定/降权/熔断重排）。
    # v1.4.14：本地优先后 order[0] 常是没装的本地引擎，若拿它当 preferred，
    # 用户会看到「ChatTTS 不可用，已降级为 edge-tts」这种莫名其妙的提示。
    # 实际合成若落到它之后，才说明发生了降级——那种情况必须让用户看到。
    _first_eng = _first_available_engine(order)
    preferred = _first_eng or (order[0] if order else None)

    def _ok(eng, path):
        """成功收尾：锁定引擎 + 记录「首选→实际」的静默质量降级。"""
        _tts_note_ok(eng)
        _TLS.tts_engine = eng
        if eng != preferred:
            _tts_note_degrade(preferred, eng)
        return True, eng, path

    stem, _ext = os.path.splitext(out_path)
    try:
        speed = 1.0 + (float(str(cfg['rate']).replace('%', '').replace('+', '')) / 100.0)
    except Exception:
        speed = 1.0
    try:
        voice = cfg['voice']
    except Exception:
        voice = None
    try:
        rate = cfg['rate']
    except Exception:
        rate = '+0%'

    # === 长文本子句切分（根因 3）===
    # 只有实际选中的引擎是 edge 才需要子句切分：旧写法看 order[0]，本地优先后
    # order[0] 是本地引擎，长文本走 edge 时反而绕开了切分保护。
    enable_split = (_first_eng == 'edge')
    clean_for_len = _strip_tts_markup(text)
    if enable_split and len(clean_for_len) > 200:
        clauses = _split_long_text(text, max_len=200)
        if len(clauses) > 1:
            # 对每个引擎尝试：所有子句都用同一引擎，成功后拼接
            for eng in order:
                if eng == 'edge' and not edge_tts_available():
                    continue
                elif eng == 'cosyvoice' and not cosyvoice_available():
                    continue
                elif eng == 'chattts' and not chattts_available():
                    continue
                elif eng == 'sherpa' and not sherpa_tts_available():
                    continue
                # sapi 始终可用（系统自带）
                # 子句合成
                sub_paths = []
                sub_ok = True
                for ci, clause in enumerate(clauses):
                    sp = '%s_sub%d.%s' % (stem, ci, 'mp3' if eng == 'edge' else 'wav')
                    ok, ap = _speak_short_clause(clause, sp, eng, voice, rate, speed)
                    if ok and os.path.exists(ap) and os.path.getsize(ap) > 500:
                        sub_paths.append(ap)
                    else:
                        sub_ok = False
                        _tts_note_fail(eng)   # [2.1] 子句级失败也计入引擎连续失败
                        # 清理已生成的子句
                        for p in sub_paths:
                            try:
                                os.unlink(p)
                            except Exception:
                                pass
                        try:
                            os.unlink(sp)
                        except Exception:
                            pass
                        break
                if sub_ok and sub_paths:
                    # ffmpeg concat 拼接
                    if _concat_audio_clips(sub_paths, out_path):
                        return _ok(eng, out_path)
                    # 拼接失败 → 清理并继续下一引擎
                    _tts_note_fail(eng)
                    for p in sub_paths:
                        if os.path.exists(p) and p != out_path:
                            try:
                                os.unlink(p)
                            except Exception:
                                pass

    # === 短文本 / 子句切分回退：走原有引擎选择逻辑 ===
    for eng in order:
        if eng == 'qwen3tts':
            if not T2.qwen3tts_ready():
                continue
            wv = stem + '_qwen3.wav'
            _hint, _vec = _tts2_style(text)
            # 韵律标记在这里真正被消费：情绪词进 VoiceDesign 的 instruct；
            # 用户在配置里写的音色描述优先于默认描述。
            _base = cfg.get('instruct') or T2.DEFAULT_INSTRUCT
            if T2.qwen3tts_speak(_tts2_clean(text), wv,
                                 instruct=_base + _hint):
                return _ok('qwen3tts', _apply_speed(wv, T2.rate_to_speed(rate)))
            _tts_note_fail('qwen3tts')
        elif eng == 'indextts2':
            if not T2.indextts2_ready():
                continue
            wv = stem + '_indextts2.wav'
            _hint, _vec = _tts2_style(text)
            # 固定参考音：整片同一把嗓子。造不出来（Qwen3 没装好/坏了）就退回
            # worker 自带示例音——仍然是一个固定的人，绝不退回逐句造音色。
            _ref = T2.voice_ref(cfg.get('instruct'))
            # 情绪词映射成 8 维向量，不再像旧引擎那样被 _strip_tts_markup 丢掉
            if T2.indextts2_speak(_tts2_clean(text), wv, emo_vector=_vec, spk=_ref):
                # 语速：克隆会继承参考音的慢节奏（实测单句 5.3 字/秒），
                # 这里统一按 rate 做保音高变速；本机 indextts 的 infer()
                # 不接受 duration_factor（会当未知参数报错），只能事后调。
                return _ok('indextts2', _apply_speed(wv, T2.rate_to_speed(rate)))
            _tts_note_fail('indextts2')
        elif eng == 'edge':
            if not edge_tts_available():
                continue
            # 【不能剥标记】edge_tts_speak 靠 has_tts_markup 走 SSML 分支，
            # 这里一剥，{情绪}/{停顿} 全部失效，9 种情感从来没被真正用过。
            # 标记由 prosody_for_tts / _enhance_tts_markup 生成，均为合法配对。
            if edge_tts_speak(text, out_path):
                return _ok('edge', out_path)
            _tts_note_fail('edge')            # [2.1] edge 整轮重试仍失败 → 记录连续失败
            # edge-tts 命令会先建文件再写入，失败时留下 0 字节 mp3 赖在产物目录里，
            # 用户看到「有配音文件却没声音」会摸不着头脑。这里立刻抹掉。
            _drop_empty(out_path)
        elif eng == 'cosyvoice':
            if not cosyvoice_available():
                continue
            wv = stem + '_cosyvoice.wav'
            clean_text = _strip_tts_markup(text)
            if cosyvoice_speak(clean_text, wv):
                return _ok('cosyvoice', wv)
            _tts_note_fail('cosyvoice')
        elif eng == 'chattts':
            if not chattts_available():
                continue
            wv = stem + '_chattts.wav'
            clean_text = _strip_tts_markup(text)
            if chattts_speak(clean_text, wv):
                return _ok('chattts', wv)
            _tts_note_fail('chattts')
        elif eng == 'sherpa':
            if not sherpa_tts_available():
                continue
            wv = stem + '_sherpa.wav'
            # 传带标记文本：sherpa_tts_speak 内部按 {停顿:n} 分段并插入真实静音，
            # 让离线模型也有「呼吸感」，而不是一口气平读到底。
            if sherpa_tts_speak(text, wv, speed=speed):
                return _ok('sherpa', wv)
            _tts_note_fail('sherpa')
        else:
            wv = stem + '.wav'
            clean_text = _strip_tts_markup(text)
            if sapi_tts(clean_text, wv):
                return _ok('sapi', wv)
            _tts_note_fail('sapi')
    return False, None, out_path

def tts_models_dir():
    """离线配音模型目录：models/tts（与 models/whisper 并列，方便用户自行查看/替换）。"""
    return os.path.join(HERE, 'models', 'tts')

def _sherpa_ready(key):
    """指定模型是否已下载到 models/tts/<name>/（判据：onnx 权重 + tokens.txt）。"""
    m = SHERPA_TTS_MODELS.get(key)
    if not m:
        return False
    d = os.path.join(tts_models_dir(), m['name'])
    if not os.path.isdir(d):
        return False
    try:
        names = os.listdir(d)
    except Exception:
        return False
    return any(n.endswith('.onnx') for n in names) and ('tokens.txt' in names)

def sherpa_model_key():
    """当前使用的离线模型 key。

    未配置或配置的模型还没下载 → 自动挑一个已就绪的（优先 melo）。
    这样新模型下载完成后无需改配置就会自动启用。"""
    cfg = load_ai_config().get('tts_local') or {}
    k = str(cfg.get('sherpa_model') or '').strip()
    if k in SHERPA_TTS_MODELS and _sherpa_ready(k):
        return k
    for cand in (SHERPA_DEFAULT_MODEL, 'melo-zh', 'piper-huayan'):
        if _sherpa_ready(cand):
            return cand
    return k if k in SHERPA_TTS_MODELS else SHERPA_DEFAULT_MODEL

def sherpa_tts_ready():
    """当前选中的离线模型是否已就绪。"""
    return _sherpa_ready(sherpa_model_key())

def sherpa_tts_available():
    """sherpa-onnx 引擎是否可用（python 包已装 + 模型已下载）。"""
    if not sherpa_tts_ready():
        return False
    try:
        import importlib.util as _u
        return _u.find_spec('sherpa_onnx') is not None
    except Exception:
        return False

def _sherpa_load():
    """加载（并缓存）sherpa-onnx 离线 TTS 实例。模型加载较慢，缓存避免每段重复加载。"""
    key = sherpa_model_key()
    m = SHERPA_TTS_MODELS.get(key) or {}
    if _SHERPA_TTS.get('obj') is not None and _SHERPA_TTS.get('key') == key:
        return _SHERPA_TTS['obj']
    import sherpa_onnx            # 重依赖，仅在真正用到时导入
    d = os.path.join(tts_models_dir(), m.get('name', ''))
    if not os.path.isdir(d):
        raise RuntimeError('离线配音模型未下载：%s' % m.get('label', key))
    # 不同发行版 onnx 文件名不同：优先标称名，否则取目录里第一个 .onnx
    model = os.path.join(d, m.get('model_file') or 'model.onnx')
    if not os.path.exists(model):
        cands = [n for n in sorted(os.listdir(d)) if n.endswith('.onnx')]
        if not cands:
            raise RuntimeError('模型目录里没有 .onnx 权重')
        model = os.path.join(d, cands[0])
    lexicon = os.path.join(d, 'lexicon.txt')
    data_dir = os.path.join(d, 'espeak-ng-data')
    tts = sherpa_onnx.OfflineTts(
        sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(
                vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                    model=model,
                    lexicon=lexicon if os.path.exists(lexicon) else '',
                    tokens=os.path.join(d, 'tokens.txt'),
                    data_dir=data_dir if os.path.isdir(data_dir) else '',
                ),
                num_threads=4,  # [P0-1 低风险优化] 原值 2，16GB内存下可提到4加速CPU合成
                provider='cpu',
            ),
            rule_fsts='',
            max_num_sentences=1,
        ))
    _SHERPA_TTS['obj'] = tts
    _SHERPA_TTS['key'] = key       # 换模型时缓存失效，避免继续用旧音色
    return tts

def sherpa_tts_speak(text, out_path, speed=1.0):
    """用离线模型合成中文配音（完全不联网）。成功返回 True。

    离线模型没有 SSML 情感，但可以按 {停顿:n} 把一句拆成多个短句逐段合成、
    中间插入真实静音——机械朗读最大的破绽就是「一口气念到底、没有呼吸」。
    有了呼吸和句读，同样的模型听感会明显像人在讲，而不是机器在报字。
    """
    if not (text or '').strip():
        return False
    d = os.path.dirname(os.path.abspath(out_path))
    if d:
        os.makedirs(d, exist_ok=True)
    try:
        tts = _sherpa_load()
        try:
            import numpy as _np
        except ImportError:
            _np = None
        segs = prosody_segments(text)
        if _np is not None and len(segs) > 1:
            chunks, sr = [], None
            for seg_txt, pause in segs:
                a = tts.generate(seg_txt, speed=float(speed or 1.0))
                if a is None or not len(a.samples):
                    return False
                sr = a.sample_rate
                chunks.append(_np.asarray(a.samples, dtype='float32'))
                if pause > 0:
                    chunks.append(_np.zeros(max(1, int(pause * sr)), dtype='float32'))
            samples = _np.concatenate(chunks)
        else:
            # 无 numpy / 单段：退回原行为，标记必须剥掉以免被念出来
            audio = tts.generate(_strip_tts_markup(text), speed=float(speed or 1.0))
            if audio is None or not len(audio.samples):
                return False
            samples, sr = audio.samples, audio.sample_rate
        try:
            import soundfile as _sf            # 写 wav；没装则用 wave 模块兜底
            _sf.write(out_path, samples, sr)
        except ImportError:
            import wave, struct
            with wave.open(out_path, 'wb') as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
                w.writeframes(b''.join(struct.pack('<h', int(max(-1.0, min(1.0, float(s))) * 32767))
                                       for s in samples))
        # 强制刷盘：避免进程被杀时大文件丢失（诊断日志说成功但文件不存在的根因）
        try:
            with open(out_path, 'rb') as _ff:
                os.fsync(_ff.fileno())
        except Exception:
            pass
    except Exception:
        return False
    return os.path.exists(out_path) and os.path.getsize(out_path) > 1000

def _cloud_tts_ready():
    """云端 TTS 是否真的配好（有 Key + 有 model，且 provider/base_url 合规）。

    与 _tts_available() 的区别：后者是「云端或本地任一可用」（handler 的 tts_available
    状态用它），判断「当前是不是在用云端」必须用本函数。曾把 _tts_available() 误用在
    local_tts_label()：没配 Key 也显示「云端 mimo」，用户看着云端标签、实际听的是
    本地引擎，完全无法判断声音差在哪一环（2026-11-05 主人反馈的根因之一）。
    """
    t = load_ai_config().get('tts') or {}
    if t.get('api_key') and t.get('model'):
        if (t.get('provider') or 'openai').lower() in ('dashscope', 'mimo'):
            return True
        if t.get('base_url'):
            return True
    return False


def _tts2_style(text):
    """把 prosody_for_tts 注入的 {情绪:xx} 标记转成新引擎能用的风格信息。

    旧引擎一律 _strip_tts_markup 把标记丢光，所以「换好嗓子还是平读」；
    这里把同一个情绪词喂给 VoiceDesign 的 instruct 与 IndexTTS2 的 8 维向量。"""
    m = re.search(r'\{情绪[:：]([^}]*)\}', text or '')
    name = (m.group(1).strip() if m else '')
    vec = T2.EMO_VECTORS.get(name)
    return ('，语气要求：%s' % name if name else ''), vec


def _tts2_clean(text):
    """新引擎不认 {停顿:n} / {情绪:xx} 字面量：停顿转成逗号，其余标记剥掉。"""
    t = re.sub(r'\{停顿[:：]?[^}]*\}', '，', text or '')
    t = re.sub(r'\{/?(?:情绪|慢|快|高音|低音|大声|小声)[^}]*\}', '', t)
    return re.sub(r'，{2,}', '，', t).strip()


def local_tts_label():
    """给前端看的当前配音来源说明（用于解说卡状态提示）。"""
    if _cloud_tts_ready():
        t = load_ai_config().get('tts') or {}
        return '云端 %s' % (t.get('provider') or 'tts')
    cfg = tts_local_cfg()
    _eng = cfg['engine']
    if _eng == 'edge':
        return 'edge-tts（%s）' % cfg['voice'] if edge_tts_available() else 'edge-tts（不可用，已回退系统 SAPI）'
    if _eng == 'cosyvoice':
        return 'CosyVoice（质量最高·%s）' % _COSYVOICE['voice'] if cosyvoice_available() else 'CosyVoice（未安装，已回退）'
    if _eng == 'chattts':
        return 'ChatTTS（自然·本地GPU）' if chattts_available() else 'ChatTTS（未安装，已回退）'
    if _eng == 'sherpa':
        _m = (SHERPA_TTS_MODELS.get(sherpa_model_key()) or {}).get('label') or '已就绪'
        return '离线模型 · %s' % _m if sherpa_tts_available() else '离线模型（未下载，已回退系统 SAPI）'
    if _eng == 'sapi':
        return '系统 SAPI'
    # 新引擎（auto / qwen3tts / indextts2）统一走 _first_available_engine(_engine_order_for())，
    # 与 local_tts_speak 同一条链。以前 qwen3tts 有个独立分支直接写「文字造音色」，
    # 2026-11-10 修 P1-3 后它实际也是克隆（Qwen3 造参考音 → IndexTTS2 逐句克隆），
    # 标签再照旧写就是在骗用户 —— 与 P1-1「谎报未装引擎」同一类缺陷。
    _first = _first_available_engine(_engine_order_for(_eng))
    if _first == 'qwen3tts':
        # 只在 IndexTTS2 不可用、真的退化成逐句现造时才这么说
        return 'Qwen3-TTS · VoiceDesign（逐句文字造音色）'
    if _first == 'indextts2':
        # 说清楚「为什么不再是一句一个音色」：Qwen3 只负责造一条参考音，
        # 整片由 IndexTTS2 克隆它。用户听到的是同一个人从片头讲到片尾。
        # auto 与显式选 qwen3tts / indextts2 都是这条。
        return 'IndexTTS2 · 克隆音色（整片同一把嗓子，情感可控）'
    if _first == 'edge':
        return 'edge-tts（%s）' % cfg['voice']
    if _first == 'cosyvoice':
        return 'CosyVoice（质量最高·%s）' % _COSYVOICE['voice']
    if _first == 'chattts':
        return 'ChatTTS（自然·本地GPU）'
    if _first == 'sherpa':
        _m = (SHERPA_TTS_MODELS.get(sherpa_model_key()) or {}).get('label') or '已就绪'
        return '离线模型 · %s' % _m
    # 显式选的新引擎没就绪 → 说清是这个没装，别含糊成「都没装」
    _nm = {'qwen3tts': 'Qwen3-TTS', 'indextts2': 'IndexTTS2'}.get(_eng)
    if _nm:
        return '%s（未安装）' % _nm
    return '⚠ 未安装配音引擎：请在设置页装 Qwen3-TTS 或 IndexTTS2'

def parse_tts_markup(text):
    """解析文案中的 TTS 标记，返回结构化片段列表。
    每个片段: {'type': 'text'|'pause'|'emotion_start'|'emotion_end'|'prosody_start'|'prosody_end',
               'content': str, 'value': str}
    """
    segments = []
    # 匹配所有标记
    pattern = r'\{(情绪|停顿|慢|快|高音|低音|大声|小声)(?::([^}]*))?\}|\{/(情绪|慢|快|高音|低音|大声|小声)\}'
    pos = 0
    for m in re.finditer(pattern, text):
        if m.start() > pos:
            segments.append({'type': 'text', 'content': text[pos:m.start()], 'value': ''})
        tag = m.group(1) or m.group(3)  # 开标签或闭标签
        is_close = m.group(0).startswith('{/')
        val = (m.group(2) or '').strip()
        if is_close:
            if tag == '情绪':
                segments.append({'type': 'emotion_end', 'content': '', 'value': ''})
            else:
                segments.append({'type': 'prosody_end', 'content': '', 'value': tag})
        else:
            if tag == '情绪':
                segments.append({'type': 'emotion_start', 'content': '', 'value': val})
            elif tag == '停顿':
                segments.append({'type': 'pause', 'content': '', 'value': val})
            else:
                segments.append({'type': 'prosody_start', 'content': '', 'value': tag})
        pos = m.end()
    if pos < len(text):
        segments.append({'type': 'text', 'content': text[pos:], 'value': ''})
    return segments

def markup_to_ssml(text, voice='zh-CN-XiaoxiaoNeural', rate='+0%'):
    """把带标记的文案转成 edge-tts SSML。"""
    # 先把全局 rate 解析成数字
    rate_num = 0
    m = re.search(r'([+-]?\d+)', str(rate))
    if m:
        rate_num = int(m.group(1))

    segs = parse_tts_markup(text)
    # 检查声线是否支持情感
    support_emotion = bool(_EMOTION_VOICES.get(voice))

    parts = []
    emotion_stack = []
    prosody_stack = []

    for seg in segs:
        t = seg['type']
        if t == 'text':
            # XML 转义
            content = seg['content'].replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            parts.append(content)
        elif t == 'pause':
            try:
                sec = float(seg['value']) if seg['value'] else 0.5
            except ValueError:
                sec = 0.5
            ms = max(100, min(5000, int(sec * 1000)))
            parts.append('<break time="%dms"/>' % ms)
        elif t == 'emotion_start':
            if support_emotion:
                style = _EMOTION_MAP.get(seg['value'], '')
                if style and style in _EMOTION_VOICES.get(voice, []):
                    parts.append('<mstts:express-as style="%s">' % style)
                    emotion_stack.append(style)
        elif t == 'emotion_end':
            if emotion_stack:
                parts.append('</mstts:express-as>')
                emotion_stack.pop()
        elif t == 'prosody_start':
            tag = seg['value']
            attrs = []
            if tag == '慢':
                attrs.append('rate="%d%%"' % (rate_num - 20))
            elif tag == '快':
                attrs.append('rate="%d%%"' % (rate_num + 20))
            elif tag == '高音':
                attrs.append('pitch="+15%%"')
            elif tag == '低音':
                attrs.append('pitch="-15%%"')
            elif tag == '大声':
                attrs.append('volume="+20%%"')
            elif tag == '小声':
                attrs.append('volume="-20%%"')
            if attrs:
                parts.append('<prosody %s>' % ' '.join(attrs))
                prosody_stack.append(tag)
        elif t == 'prosody_end':
            if prosody_stack:
                parts.append('</prosody>')
                prosody_stack.pop()

    # 闭合未闭合的标签
    while emotion_stack:
        parts.append('</mstts:express-as>')
        emotion_stack.pop()
    while prosody_stack:
        parts.append('</prosody>')
        prosody_stack.pop()

    body = ''.join(parts)
    ssml = (
        '<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
        'xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="zh-CN">'
        '<voice name="%s">%s</voice></speak>' % (voice, body)
    )
    return ssml

def markup_to_chattts_text(text):
    """把带标记的文案转成 ChatTTS 输入文本（用 [uv_break]/[lbreak] 控制停顿）。"""
    segs = parse_tts_markup(text)
    parts = []
    for seg in segs:
        t = seg['type']
        if t == 'text':
            parts.append(seg['content'])
        elif t == 'pause':
            try:
                sec = float(seg['value']) if seg['value'] else 0.5
            except ValueError:
                sec = 0.5
            if sec >= 1.0:
                parts.append('[lbreak]')
            else:
                parts.append('[uv_break]')
        # emotion/prosody 标记在 ChatTTS 中通过文本本身的语气传达，不做特殊转换
    return ''.join(parts)

# ---------------------------------------------------------------------------
# 影视解说韵律注入：给「用户自带稿子」这类没有 TTS 标记的成品文案自动加情绪/停顿。
# 只在送进合成引擎的文本里生效，不改 narr/texts、不改字幕、不改用户一个字。
# 为什么必须有：用户稿子路线刻意跳过了 _enhance_tts_markup，结果即使换到
# edge-tts 也只是「更好的嗓子平读」；而 local_tts_speak 又会把标记全剥掉，
# SSML 情感分支从未被走到——两边叠加，成片必然是机械朗读。
# ---------------------------------------------------------------------------
_PROSODY_EMOTION = {
    '紧张': ('枪', '开枪', '枪响', '扑', '低吼', '吼', '追', '逃', '血', '尸', '丧尸',
             '倒地', '中枪', '猛地', '突然', '危险', '翻倒', '追捕', '逃窜', '开火',
             '对峙', '倒下', '撞', '警报', '尖叫', '僵硬', '撕裂', '浑浊', '锁在'),
    '悲伤': ('枯萎', '枯', '没有回应', '寂静', '空荡', '废墟', '走远', '再也', '腐烂',
             '碎屑', '沉默', '消失', '睡过去', '死', '残破', '破碎'),
    '温柔': ('别怕', '轻轻', '放轻', '缓缓', '小心', '小姑娘', '孩子', '安静',
             '慢慢', '平淡', '熟稔', '一搭没一搭'),
    '激动': ('炸开', '终于', '竟然', '轰', '爆发', '冲进', '掀开', '第一次'),
}
_PROSODY_PAUSE = {'，': 0.12, '、': 0.10, '；': 0.18, '：': 0.18,
                  '。': 0.26, '！': 0.30, '？': 0.30, '—': 0.11}


def _prosody_emotion(clause):
    """给单个子句选情绪：命中 >=2 个同情绪关键词才切，否则用「解说」讲述感。"""
    best, hits_max = '解说', 0
    for name, kws in _PROSODY_EMOTION.items():
        hits = sum(1 for k in kws if k in clause)
        if hits >= 2 and hits > hits_max:
            best, hits_max = name, hits
    return best


def emotion_of(text):
    """整段文案的主情绪（只读复用，供配乐/渲染等非 TTS 场景使用）。

    与 _prosody_emotion 共用同一张关键词表，但作用域不同：
      · _prosody_emotion 是逐「子句」判定（<=2 个命中才切，宁稳不噪）；
      · 本函数是整段汇总，统计全段命中总数，取最多且 >=2 次的那一类，
        都没有就归为「解说」（平铺讲述）。
    这样配乐包络拿到的是「这一段在讲什么情绪」，而不是被一句话里的单个词带偏。
    """
    s = str(text or '')
    if not s:
        return '解说'
    best, hits_max = '解说', 0
    for name, kws in _PROSODY_EMOTION.items():
        hits = sum(s.count(k) for k in kws)
        if hits >= 2 and hits > hits_max:
            best, hits_max = name, hits
    return best


def prosody_for_tts(text):
    """把无标记的解说句转成「仅用于合成」的带标记文本。

    按子句贴情绪，不整句一刀切：一句里既有「放轻声音喊她」又有「皮肉烂穿」时，
    整句贴一个情绪必然错。真实解说是整体平稳讲述、关键处才起情绪。
    引号内的对白不插停顿、不拆开，免得把一句台词念碎。
    """
    s = str(text or '').strip()
    if len(s) < 12 or has_tts_markup(s):
        return text
    clauses = []
    quote = False
    buf = []
    for i, ch in enumerate(s):
        if ch in '“‘「『':
            quote = True
        elif ch in '”’」』':
            quote = False
        buf.append(ch)
        if not quote and i < len(s) - 1 and ch in _PROSODY_PAUSE:
            clauses.append((''.join(buf), _PROSODY_PAUSE[ch]))
            buf = []
    if buf:
        clauses.append((''.join(buf), 0.0))
    if not clauses:
        return text
    out = []
    for clause, pause in clauses:
        out.append('{情绪:%s}' % _prosody_emotion(clause) + clause + '{/情绪}')
        if pause > 0:
            out.append('{停顿:%.2f}' % pause)
    return ''.join(out)


def prosody_segments(text):
    """把带标记文本拆成 [(朗读文本, 停顿秒数)]，用于不支持 SSML 的离线引擎。"""
    s = re.sub(r'\{/?(?:情绪|慢|快|高音|低音|大声|小声)(?::[^}]*)?\}', '',
               str(text or ''))
    parts = re.split(r'\{停顿\s*:\s*([0-9.]+)\}', s)
    segs = []
    for i in range(0, len(parts) - 1, 2):
        try:
            pause = float(parts[i + 1])
        except (ValueError, IndexError):
            pause = 0.0
        segs.append((parts[i], pause))
    if len(parts) % 2 == 1:
        segs.append((parts[-1], 0.0))
    return [(t, p) for t, p in segs if t.strip()]


def has_tts_markup(text):
    """检查文案是否包含 TTS 标记。"""
    return bool(re.search(r'\{(情绪|停顿|慢|快|高音|低音|大声|小声)', text or ''))

_EMOTION_KEYWORDS = {
    '激动': ['没想到', '竟然', '居然', '惊人', '震撼', '奇迹', '破纪录', '夺冠', '加冕', '巅峰', '高光'],
    '悲伤': ['死', '牺牲', '悲剧', '去世', '离别', '崩溃', '绝望', '痛苦', '眼泪', '心碎', '遗憾'],
    '紧张': ['危险', '紧张', '追逐', '打斗', '千钧一发', '命悬一线', '危机', '追杀', '逃亡', '惊险'],
    '严肃': ['秘密', '真相', '阴谋', '背叛', '悬念', '重要', '关键', '决定', '命运', '历史'],
    '温柔': ['温柔', '回忆', '温情', '爱情', '家人', '陪伴', '温暖', '幸福', '感动', '深情'],
}
_SLOW_KEYWORDS = ['年', '万', '亿', '%', '第', '首次', '唯一', '最', '纪录', '票房', '冠军']

_PAUSE_RUN = re.compile(r'(?:\{停顿\s*:\s*[0-9.]+\}\s*){2,}')
_DUP_CLOSER = re.compile(r'(\{/(?:情绪|慢|快|高音|低音|大声|小声)\})(?:\s*\1)+')


def _normalize_markup(t):
    """规范化模型给出的标记，去掉会被念出来 / 让停顿失控的写法。

    实测模型会输出三类坏标记：
      · {停顿:0.3}{/停顿}  —— 停顿是单点标记，模型自行加了闭合标签，念稿会出现异常停顿
      · {停顿:0.3}{停顿:0.3}{/停顿}{/停顿} —— 重复叠放，停顿时长失控
      · {情绪:紧张}{/情绪}{/情绪} —— 重复闭合
    """
    if not t:
        return t
    s = str(t)
    # 停顿没有闭合标签：一律删掉 {/停顿}
    s = re.sub(r'\{/\s*停顿\s*\}', '', s)

    # 连续多个停顿合并成一个（取最大值，封顶 2 秒，避免空转）
    def _merge_pauses(m):
        try:
            v = max(float(x) for x in re.findall(r'[0-9.]+', m.group(0)))
        except (ValueError, TypeError):
            v = 0.5
        return '{停顿:%.1f}' % min(2.0, v)

    s = _PAUSE_RUN.sub(_merge_pauses, s)
    # 重复闭合标签只留一个
    s = _DUP_CLOSER.sub(r'\1', s)
    return s


def _enhance_tts_markup(texts):
    """TTS 标记后处理：检查 LLM 生成的解说词标记是否完整合理，自动补全/修正。
    - 修复未闭合的 {情绪:xx}/{慢} 等标签
    - 完全没有标记的段落，根据内容自动添加停顿/情绪
    - 避免过度标记（每句都加情绪），保持详略得当
    """
    if not texts:
        return texts

    result = []
    for idx, text in enumerate(texts):
        if not text or not text.strip():
            result.append(text)
            continue

        # 0. 先规范化模型给的原标记（坏闭合标签/重复停顿会直接影响念稿效果）
        t = _normalize_markup(text.strip())
        has_markup = has_tts_markup(t)

        # 1. 修复未闭合标签
        t = _fix_unclosed_tags(t)

        # 2. 如果完全没有标记，根据内容自动添加
        if not has_markup:
            added = []
            # 开头（第一节/hook）加停顿
            if idx == 0:
                # 在片名或关键词后加停顿
                m = re.search(r'《[^》]+》', t)
                if m:
                    t = t[:m.end()] + '{停顿:0.6}' + t[m.end():]
                    added.append('pause')
                else:
                    t = '{停顿:0.3}' + t
                    added.append('pause')

            # 检测情绪（按关键词匹配强度，不按序号奇偶）
            emotion = None
            emo_hits = 0
            for emo, kws in _EMOTION_KEYWORDS.items():
                hits = sum(1 for kw in kws if kw in t)
                if hits > emo_hits:
                    emotion = emo
                    emo_hits = hits
            # 有情绪关键词时添加，但整段最多50%的节有情绪标记（避免过度）
            if emotion and emo_hits > 0:
                for kw in _EMOTION_KEYWORDS[emotion]:
                    if kw in t:
                        pos = t.find(kw)
                        # 从关键词前最近的标点或句首开始
                        start = pos
                        while start > 0 and t[start-1] not in '，。！？、；：':
                            start -= 1
                        t = t[:start] + '{情绪:%s}' % emotion + t[start:] + '{/情绪}'
                        added.append('emotion:' + emotion)
                        break

            # 重要信息放慢（只包裹数字本身+后面几个字，不切断词语）
            if any(kw in t for kw in _SLOW_KEYWORDS) and '慢' not in added:
                m = re.search(r'\d+[万亿%]?', t)
                if m:
                    # 从数字前一个标点或句首开始，到数字后最近的标点结束
                    start = m.start()
                    while start > 0 and t[start-1] not in '，。！？、；：':
                        start -= 1
                    end = m.end()
                    # 向后找到最近的标点，最多延伸10字
                    while end < len(t) and t[end] not in '，。！？、；：' and end - m.end() < 10:
                        end += 1
                    t = t[:start] + '{慢}' + t[start:end] + '{/慢}' + t[end:]
                    added.append('slow')

            # 句末加短停顿（如果没有）
            if not t.endswith(('{停顿:0.3}', '{停顿:0.5}', '{停顿:0.8}', '{停顿:1.0}')):
                t = t + '{停顿:0.3}'
                added.append('pause_end')

        # 3. 保证所有开标签都有闭标签（再次检查）
        t = _fix_unclosed_tags(t)

        result.append(t)

    return result

def strip_tts_markup(text):
    """去除文案中的 TTS 标记（用于字幕显示）。

    必须同时认 {/停顿}：个别模型会给单点标记补闭合标签，
    漏掉就会让字幕里原样出现「{/停顿}」。
    """
    if not text:
        return text
    text = re.sub(r'\{/?(情绪|慢|快|高音|低音|大声|小声)(?::[^}]*)?\}', '', text)
    text = re.sub(r'\{/?停顿(?::[^}]*)?\}', '', text)
    return text.strip()



def _tts_available():
    """TTS 是否可用：云端 API 或本地任一引擎。

    本地口径：edge-tts / ChatTTS / sherpa / Qwen3-TTS / IndexTTS2 / SAPI 任一。
    """
    # 1. 云端 TTS
    if _cloud_tts_ready():
        return True
    # 2. 本地引擎
    try:
        if edge_tts_available():
            return True
    except Exception:
        pass
    try:
        if chattts_available():
            return True
    except Exception:
        pass
    try:
        if sherpa_tts_available():
            return True
    except Exception:
        pass
    # 2b. 新一代引擎（Qwen3-TTS / IndexTTS2）。【2026-11-10 补】
    # 这里以前也只列老引擎，主人换到新引擎后本函数仍判 False，后果比「显示未就绪」
    # 更硬：workflows.py:989 的 `if voice_over and _w._tts_available():` 会**整段跳过
    # 配音**（narration_path 保持 None），成片没声音却照样报成功。
    # 与 _voice_status() 是同一个漏项的第二个出口，必须一起修。
    # 两个 ready 都是纯文件系统检查（解释器 + worker + 权重目录），不起进程。
    for _fn in (T2.qwen3tts_ready, T2.indextts2_ready):
        try:
            if _fn():
                return True
        except Exception:
            pass
    # 3. SAPI 兜底（Windows 自带，pyttsx3 存在即视为可用——仅探测不使用）
    try:
        import importlib.util
        if importlib.util.find_spec('pyttsx3') is not None:
            return True
        return False
    except Exception:
        pass
    return False

# [3.2b] 由 webui 迁入的会话辅助函数（被本地引擎/前端引用）

_OPEN_TAG_RE = re.compile(r'\{(\w+)(?::[^}]*)?\}')
_CLOSE_TAG_RE = re.compile(r'\{/(\w+)\}')
# 需要成对闭合的标记；{停顿:x} 是单点标记，不在其中
_PAIRED_TAGS = {'情绪', '慢', '快', '高音', '低音', '大声', '小声'}

def _fix_unclosed_tags(text):
    """修复未闭合的 TTS 标记：{情绪:xx} 必须有 {/情绪}，{慢} 必须有 {/慢}。

    【坑】{停顿:x} 是单点标记，没有闭合标签。旧实现把它也当成「需要闭合」，
    于是每段带停顿的稿子都会被追加一个 {/停顿}，stdout 可见
    「{停顿:0.3}{/停顿}」这种畸形标记，配音时会产生多余停顿。
    """
    counts = {}
    for m in _OPEN_TAG_RE.finditer(text):
        tag = m.group(1)
        if tag not in _PAIRED_TAGS:
            continue                      # 停顿等单点标记不参与配对
        counts[tag] = counts.get(tag, 0) + 1
    for m in _CLOSE_TAG_RE.finditer(text):
        tag = m.group(1)
        if tag not in _PAIRED_TAGS:
            continue
        counts[tag] = counts.get(tag, 0) - 1
    for tag, n in counts.items():
        if n > 0:
            text += ('{/%s}' % tag) * n
        elif n < 0:
            # 孤儿闭合标签：稿子在清洗阶段删掉了带 {情绪:xx} 开标签的分句，
            # 只剩 {/情绪}。留着会让配音引擎找不到配对开标签（部分地区直接报错），
            # 字幕里也会冒出花括号。从后往前删掉多余的闭合标签。
            _cl = '{/%s}' % tag
            _idxs = [m.start() for m in re.finditer(re.escape(_cl), text)]
            for i in reversed(_idxs[n:]):
                text = text[:i] + text[i + len(_cl):]
    return text


# ---- 全量符号白名单：供 webui_server `from tts_engines import *` 完整重新导出 ----
# 拆分后这些函数/常量定义在本模块，函数内部也按本模块命名空间解析；
# 外部命名空间（webui_server）若要透过别名使用同名符号，必须经本名单一次性
# re-export，才能保证旧命名空间 API 完整（含下划线熔断常量与引擎内部状态）。
__all__ = [
    # 配置与引擎入口
    "tts_local_cfg", "local_tts_speak", "local_tts_label", "tts_models_dir",
    "sapi_tts", "_tts_available",
    # edge-tts 与熔断状态
    "edge_tts_dead_reason", "edge_tts_available", "edge_tts_reset",
    "edge_tts_speak", "_edge_internal", "_edge_note_failure",
    "_tls_edge_state", "_EDGE_RETRY", "_EDGE_RETRY_SLEEP", "_EDGE_MAX_FAILS",
    "_EDGE_DEAD_SECONDS", "_EDGE_RUN_DOWNGRADE", "_EDGE_STATE",
    # ChatTTS
    "_chattts_venv_python", "chattts_available", "chattts_load", "chattts_speak",
    # CosyVoice
    "_cosyvoice_venv_python", "_find_cosyvoice_python", "cosyvoice_available",
    "cosyvoice_speak", "_COSYVOICE", "COSYVOICE_REPO_DIR", "COSYVOICE_MODEL_DIR",
    "COSYVOICE_VENV_PY",
    # sherpa-onnx
    "_sherpa_ready", "sherpa_model_key", "sherpa_tts_ready",
    "sherpa_tts_available", "_sherpa_load", "sherpa_tts_speak",
    "_SHERPA_TTS", "SHERPA_TTS_MODELS", "SHERPA_DEFAULT_MODEL",
    # 配音控制
    "_speak_short_clause", "_tts_run_banned", "_tts_note_fail", "_tts_note_ok",
    "_tts_force_downgrade", "_ping_preferred_engine",
    # 标注 / 情绪
    "parse_tts_markup", "markup_to_ssml", "markup_to_chattts_text",
    "has_tts_markup", "_enhance_tts_markup", "strip_tts_markup",
    "_fix_unclosed_tags", "_EMOTION_MAP", "_EMOTION_VOICES",
    # 其它模块级常量
    "HERE", "_CHATTS",
]