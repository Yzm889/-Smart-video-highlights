# -*- coding: utf-8 -*-
"""引擎/工具模块（由 webui_server.py 第4批拆分生成，符号与拆分前等价）。"""
import os, time
# ---- 跨模块依赖（拆分后显式导入） ----
from ai_providers import load_ai_config
from ffmpeg_utils import _TLS, _concat_audio_clips
from text_utils import _split_long_text, _strip_tts_markup

import re
import subprocess
import sys
# ---- 熔断 / 降权 / 引擎缓存 / 模型表（原 webui_server 全局，随引擎层迁入，webui 经 re-export 共享同一对象） ----
HERE = os.path.dirname(os.path.abspath(__file__))
_EDGE_RETRY = 3
_EDGE_RETRY_SLEEP = 1.2
_EDGE_MAX_FAILS = 4
_EDGE_DEAD_SECONDS = 120
_EDGE_RUN_DOWNGRADE = 2
_EDGE_STATE = {'fails': 0, 'dead_until': 0.0, 'reason': '', 'probe_tick': 0}
_CHATTS = {'model': None, 'loading': False, 'error': ''}
_COSYVOICE = {'model': None, 'loading': False, 'error': '', 'voice': '中文女'}
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
    """本地配音配置 {engine, voice, rate}；engine ∈ auto|edge|cosyvoice|chattts|sherpa|sapi。"""
    c = load_ai_config().get('tts_local') or {}
    rate = str(c.get('rate') or '+0%').strip()
    if rate and not rate.startswith(('+', '-')):
        rate = '+' + rate.replace('%', '') + '%'
    return {'engine': str(c.get('engine') or 'auto').lower(),
            'voice': str(c.get('voice') or 'zh-CN-XiaoxiaoNeural').strip(),
            'rate': rate or '+0%',
            'cosy_voice': str(c.get('cosy_voice') or '中文女').strip()}

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

def edge_tts_available():
    """edge-tts 是否已安装且当前可用（python 模块或命令行任一即可，熔断期内视为不可用）。"""
    if edge_tts_dead_reason():
        return False
    try:
        import importlib.util as _u
        if _u.find_spec('edge_tts') is not None:
            return True
    except Exception:
        pass
    try:
        r = subprocess.run(['edge-tts', '--version'], capture_output=True, timeout=25)
        return r.returncode == 0
    except Exception:
        return False

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
            r = subprocess.run([venv_py, worker, txt_file, out_path],
                               capture_output=True, timeout=300, cwd=HERE)
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
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                base = r.stdout.strip()
                candidates.insert(0, os.path.join(base, 'python.exe'))
        except Exception:
            pass
    for p in candidates:
        if p and os.path.exists(p):
            try:
                r = subprocess.run([p, '--version'], capture_output=True, text=True, timeout=10)
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

def cosyvoice_speak(text, out_path, progress=None):
    """用 CosyVoice2-0.5B 合成语音。成功返回 True。
    优先主进程内推理，否则用 venv 子进程。不支持{{情绪}}标记，会自动剥离。"""
    if not (text or '').strip():
        return False
    clean_text = _strip_tts_markup(text)
    if not clean_text.strip():
        return False

    # 方式1：venv 子进程（推荐，隔离PyTorch环境）
    venv_py = _cosyvoice_venv_python()
    if venv_py and os.path.isdir(COSYVOICE_MODEL_DIR) and os.path.exists(os.path.join(HERE, 'cosyvoice_worker.py')):
        try:
            d = os.path.dirname(os.path.abspath(out_path))
            if d:
                os.makedirs(d, exist_ok=True)
            txt_file = out_path + '.txt'
            with open(txt_file, 'w', encoding='utf-8') as f:
                f.write(clean_text)
            worker = os.path.join(HERE, 'cosyvoice_worker.py')
            _cv = tts_local_cfg().get('cosy_voice') or _COSYVOICE['voice']
            # 根据音色名找参考音频（zero-shot声音克隆需要参考音频）
            _ref_dir = os.path.join(HERE, 'models', 'cosyvoice', 'voices')
            _ref_wav = os.path.join(_ref_dir, _cv + '.wav')
            if not os.path.exists(_ref_wav):
                _ref_wav = os.path.join(_ref_dir, _cv + '.mp3')
            if not os.path.exists(_ref_wav):
                _ref_wav = os.path.join(_ref_dir, '中文女.wav')
            if not os.path.exists(_ref_wav):
                _ref_wav = os.path.join(_ref_dir, '中文女.mp3')
            if not os.path.exists(_ref_wav):
                _COSYVOICE['error'] = '参考音频不存在，请重新安装CosyVoice'
                print('[COSYVOICE] no ref audio')
                return False
            r = subprocess.run([venv_py, worker, txt_file, out_path, COSYVOICE_MODEL_DIR, _ref_wav],
                               capture_output=True, timeout=300, cwd=COSYVOICE_REPO_DIR if os.path.isdir(COSYVOICE_REPO_DIR) else HERE)
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
                r = subprocess.run(cmd, capture_output=True, timeout=180)
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
    if eng not in ('edge', 'auto'):
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
    cfg = tts_local_cfg()
    eng_set = cfg['engine']
    if eng_set == 'edge':
        order = ['edge', 'cosyvoice', 'chattts', 'sherpa', 'sapi']
    elif eng_set == 'cosyvoice':
        order = ['cosyvoice', 'edge', 'chattts', 'sherpa', 'sapi']
    elif eng_set == 'chattts':
        order = ['chattts', 'cosyvoice', 'edge', 'sherpa', 'sapi']
    elif eng_set == 'sherpa':
        order = ['sherpa', 'edge', 'cosyvoice', 'sapi']
    elif eng_set == 'sapi':
        order = ['sapi', 'sherpa', 'edge']
    else:   # auto：音质优先（edge）→ CosyVoice → ChatTTS → sherpa → 系统兜底
        order = ['edge', 'cosyvoice', 'chattts', 'sherpa', 'sapi']
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
    # 只有 edge-tts 引擎做首选时才触发（其他引擎对长文本无截断限制）
    enable_split = (order[0] == 'edge')
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
                        _tts_note_ok(eng)
                        _TLS.tts_engine = eng
                        return True, eng, out_path
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
        if eng == 'edge':
            if not edge_tts_available():
                continue
            clean_text = _strip_tts_markup(text)
            if edge_tts_speak(clean_text, out_path):
                _tts_note_ok('edge')
                _TLS.tts_engine = 'edge'      # 锁定：后续段落沿用同一音色
                return True, 'edge', out_path
            _tts_note_fail('edge')            # [2.1] edge 整轮重试仍失败 → 记录连续失败
        elif eng == 'cosyvoice':
            if not cosyvoice_available():
                continue
            wv = stem + '_cosyvoice.wav'
            clean_text = _strip_tts_markup(text)
            if cosyvoice_speak(clean_text, wv):
                _tts_note_ok('cosyvoice')
                _TLS.tts_engine = 'cosyvoice'
                return True, 'cosyvoice', wv
            _tts_note_fail('cosyvoice')
        elif eng == 'chattts':
            if not chattts_available():
                continue
            wv = stem + '_chattts.wav'
            clean_text = _strip_tts_markup(text)
            if chattts_speak(clean_text, wv):
                _tts_note_ok('chattts')
                _TLS.tts_engine = 'chattts'
                return True, 'chattts', wv
            _tts_note_fail('chattts')
        elif eng == 'sherpa':
            if not sherpa_tts_available():
                continue
            wv = stem + '_sherpa.wav'
            clean_text = _strip_tts_markup(text)
            if sherpa_tts_speak(clean_text, wv, speed=speed):
                _tts_note_ok('sherpa')
                _TLS.tts_engine = 'sherpa'
                return True, 'sherpa', wv
            _tts_note_fail('sherpa')
        else:
            wv = stem + '.wav'
            clean_text = _strip_tts_markup(text)
            if sapi_tts(clean_text, wv):
                _tts_note_ok('sapi')
                _TLS.tts_engine = 'sapi'
                return True, 'sapi', wv
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
    """用离线模型合成中文配音（完全不联网）。成功返回 True。"""
    if not (text or '').strip():
        return False
    d = os.path.dirname(os.path.abspath(out_path))
    if d:
        os.makedirs(d, exist_ok=True)
    try:
        tts = _sherpa_load()
        audio = tts.generate(text, speed=float(speed or 1.0))
        if audio is None or not len(audio.samples):
            return False
        import soundfile as _sf            # 写 wav；没装则用 wave 模块兜底
        _sf.write(out_path, audio.samples, audio.sample_rate)
        # 强制刷盘：避免进程被杀时大文件丢失（诊断日志说成功但文件不存在的根因）
        try:
            with open(out_path, 'rb') as _ff:
                os.fsync(_ff.fileno())
        except Exception:
            pass
    except ImportError:
        try:
            import wave, struct
            with wave.open(out_path, 'wb') as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(audio.sample_rate)
                w.writeframes(b''.join(struct.pack('<h', int(max(-1.0, min(1.0, s)) * 32767))
                                       for s in audio.samples))
            # 强制刷盘
            try:
                with open(out_path, 'rb') as _ff:
                    os.fsync(_ff.fileno())
            except Exception:
                pass
        except Exception:
            return False
    except Exception:
        return False
    return os.path.exists(out_path) and os.path.getsize(out_path) > 1000

def local_tts_label():
    """给前端看的当前配音来源说明（用于解说卡状态提示）。"""
    if _tts_available():
        t = load_ai_config().get('tts') or {}
        return '云端 %s' % (t.get('provider') or 'tts')
    cfg = tts_local_cfg()
    if cfg['engine'] == 'edge':
        return 'edge-tts（%s）' % cfg['voice'] if edge_tts_available() else 'edge-tts（不可用，已回退系统 SAPI）'
    if cfg['engine'] == 'cosyvoice':
        return 'CosyVoice（质量最高·%s）' % _COSYVOICE['voice'] if cosyvoice_available() else 'CosyVoice（未安装，已回退）'
    if cfg['engine'] == 'chattts':
        return 'ChatTTS（自然·本地GPU）' if chattts_available() else 'ChatTTS（未安装，已回退）'
    if cfg['engine'] == 'sherpa':
        return '离线模型 · 华研女声' if sherpa_tts_available() else '离线模型（未下载，已回退系统 SAPI）'
    if cfg['engine'] == 'sapi':
        return '系统 SAPI'
    # auto
    if edge_tts_available():
        return 'edge-tts（%s）' % cfg['voice']
    if cosyvoice_available():
        return 'CosyVoice（质量最高·%s）' % _COSYVOICE['voice']
    if chattts_available():
        return 'ChatTTS（自然·本地GPU）'
    if sherpa_tts_available():
        return '离线模型 · 华研女声'
    return '系统 SAPI（可选装 edge-tts/CosyVoice/离线模型）'

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

def has_tts_markup(text):
    """检查文案是否包含 TTS 标记。"""
    return bool(re.search(r'\{(情绪|停顿|慢|快|高音|低音|大声|小声)', text or ''))

def _enhance_tts_markup(texts):
    """TTS 标记后处理：检查 LLM 生成的解说词标记是否完整合理，自动补全/修正。
    - 修复未闭合的 {情绪:xx}/{慢} 等标签
    - 完全没有标记的段落，根据内容自动添加停顿/情绪
    - 避免过度标记（每句都加情绪），保持详略得当
    """
    if not texts:
        return texts

    # 情绪关键词映射
    emotion_keywords = {
        '激动': ['没想到', '竟然', '居然', '惊人', '震撼', '奇迹', '破纪录', '夺冠', '加冕', '巅峰', '高光'],
        '悲伤': ['死', '牺牲', '悲剧', '去世', '离别', '崩溃', '绝望', '痛苦', '眼泪', '心碎', '遗憾'],
        '紧张': ['危险', '紧张', '追逐', '打斗', '千钧一发', '命悬一线', '危机', '追杀', '逃亡', '惊险'],
        '严肃': ['秘密', '真相', '阴谋', '背叛', '悬念', '重要', '关键', '决定', '命运', '历史'],
        '温柔': ['温柔', '回忆', '温情', '爱情', '家人', '陪伴', '温暖', '幸福', '感动', '深情'],
    }
    # 慢读关键词（重要信息）
    slow_keywords = ['年', '万', '亿', '%', '第', '首次', '唯一', '最', '纪录', '票房', '冠军']

    result = []
    for idx, text in enumerate(texts):
        if not text or not text.strip():
            result.append(text)
            continue

        t = text.strip()
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
            for emo, kws in emotion_keywords.items():
                hits = sum(1 for kw in kws if kw in t)
                if hits > emo_hits:
                    emotion = emo
                    emo_hits = hits
            # 有情绪关键词时添加，但整段最多50%的节有情绪标记（避免过度）
            if emotion and emo_hits > 0:
                for kw in emotion_keywords[emotion]:
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
            if any(kw in t for kw in slow_keywords) and '慢' not in added:
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
    """去除文案中的 TTS 标记（用于字幕显示）。"""
    if not text:
        return text
    text = re.sub(r'\{/?(情绪|慢|快|高音|低音|大声|小声)(?::[^}]*)?\}', '', text)
    text = re.sub(r'\{停顿:[^}]*\}', '', text)
    return text.strip()



def _tts_available():
    """TTS 是否可用：云端 API 或本地任一引擎（edge-tts/ChatTTS/sherpa/SAPI）。"""
    # 1. 云端 TTS
    t = load_ai_config().get('tts') or {}
    if t.get('api_key') and t.get('model'):
        if (t.get('provider') or 'openai').lower() in ('dashscope', 'mimo'):
            return True
        if t.get('base_url'):
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

def _fix_unclosed_tags(text):
    """修复未闭合的 TTS 标记：{情绪:xx} 必须有 {/情绪}，{慢} 必须有 {/慢}。"""
    # 检查情绪标签
    open_emotion = re.findall(r'\{情绪:([^}]+)\}', text)
    close_emotion = len(re.findall(r'\{/情绪\}', text))
    if len(open_emotion) > close_emotion:
        text = text + '{/情绪}' * (len(open_emotion) - close_emotion)
    # 检查 prosody 标签
    for tag in ['慢', '快', '高音', '低音', '大声', '小声']:
        opens = len(re.findall(r'\{%s\}' % tag, text))
        closes = len(re.findall(r'\{/%s\}' % tag, text))
        if opens > closes:
            text = text + ('{/%s}' % tag) * (opens - closes)
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