# -*- coding: utf-8 -*-
"""引擎/工具模块（由 webui_server.py 第4批拆分生成，符号与拆分前等价）。"""
import os, json, time
# ---- 跨模块依赖（拆分后显式导入） ----
from cache_utils import HERE, WORKDIR
from ffmpeg_utils import AbortError, PROGRESS, _TLS, ffmpeg_run

# 本来在 webui 模块级、仅供本模块使用的常量（随迁移）；webui 侧通过 re-export 保持旧命名空间
AI_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ai_config.json')
_WHISPER_MODELS = {}


def _whisper_env_setup():
    """统一设置 Whisper 加载/下载环境：走 HF 镜像 + 清理失效系统代理（避免 WinError 10061）。
    所有 WhisperModel 调用前都要跑（含首次生成时的 asr_segments），否则系统里没开的代理会令连接被本机拒绝。"""
    mc = mirror_cfg()
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

def asr_segments(video_path, progress=None, pct_range=None):
    """faster-whisper 本地转写台词，返回 [{start,end,text}]。不可用/失败返回 []。
    自动用 GPU（CUDA）加速；无显卡回退 CPU。模型权重首次运行联网下载一次（~140MB）。

    progress / pct_range：可选。传了就逐段推进进度并响应取消——
    旧实现一次性消费 transcribe 的生成器，长视频（十几分钟）转写期间进度条
    长时间停在同一个百分比，观感与「卡死」无异，且中途无法取消。"""
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
        model = WhisperModel(_whisper_load_path(whisper_model_name()), device=device, compute_type=ctype,
                             download_root=whisper_models_dir())
        segments, info = model.transcribe(wav, language='zh', vad_filter=True,
                                          initial_prompt='以下是普通话的句子。')
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
        return segs
    except Exception:
        try:
            if os.path.exists(wav):
                os.remove(wav)
        except Exception:
            pass
        return []

def load_ai_config():
    try:
        with open(AI_CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
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
        'model': cfg.get('model') or 'qwen2.5:latest',
        'api_key': cfg.get('api_key') or '',
    }

def local_llm_chat(prompt, system=None, timeout=180):
    """调用本地 OpenAI 兼容端点生成文本（Ollama /v1/chat/completions）。失败抛异常。"""
    cfg = local_llm_cfg()
    if not cfg['base_url']:
        raise RuntimeError('本地模型未配置')
    import urllib.request, json as _json
    messages = []
    if system:
        messages.append({'role': 'system', 'content': system})
    messages.append({'role': 'user', 'content': prompt})
    payload = {'model': cfg['model'], 'messages': messages, 'max_tokens': 1500, 'temperature': 0.8}
    headers = {'Content-Type': 'application/json'}
    if cfg['api_key']:
        headers['Authorization'] = 'Bearer ' + cfg['api_key']
    req = urllib.request.Request(cfg['base_url'] + '/chat/completions',
                                 data=_json.dumps(payload).encode('utf-8'), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = _json.loads(resp.read().decode('utf-8'))
    return _strip_think((data.get('choices') or [{}])[0].get('message', {}).get('content', ''))

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
    cfg = load_ai_config().get('vlm') or {}
    base = (cfg.get('base_url') or 'http://localhost:11434').rstrip('/')
    return {
        'enabled': bool(cfg.get('enabled', False)),
        'mode': (cfg.get('mode') or 'ollama'),  # ollama(原生 /api/chat) | openai(/v1/chat/completions 多模态)
        'base_url': base,
        'model': cfg.get('model') or 'qwen2.5vl:latest',
        'api_key': cfg.get('api_key') or '',
    }

def vlm_chat_multi(image_paths, text, system=None, timeout=240):
    """调用本地/云端 VLM：一次传入多张图（按时间先后顺序），返回模型文本回复。失败抛异常。
    用于「剧情理解」：把整片关键帧一起看，判断作品/人物/剧情，而不是逐帧孤岛描述。"""
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
        payload = {'model': c['model'], 'messages': messages, 'stream': False}
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
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = _json.loads(resp.read().decode('utf-8'))
    if c['mode'] == 'ollama':
        return (data.get('message') or {}).get('content', '')
    return (data.get('choices') or [{}])[0].get('message', {}).get('content', '')

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

def whisper_models_dir():
    """faster-whisper 模型权重统一缓存到项目 models/whisper，方便引导用户管理/查看。"""
    return os.path.join(HERE, 'models', 'whisper')

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

def _cuda_available():
    """轻量检测 NVIDIA GPU 是否可用：不依赖 torch（很多用户没装），先用 nvidia-smi，再 fallback torch。
    faster-whisper 的 CTranslate2 后端原生支持 CUDA，只要驱动在就能用，不需要 torch。"""
    try:
        import subprocess
        r = subprocess.run(['nvidia-smi', '--query-gpu=memory.total', '--format=csv,noheader,nounits'],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return True
    except Exception:
        pass
    try:
        import torch
        if getattr(torch, 'cuda', None) is not None and torch.cuda.is_available():
            return True
    except Exception:
        pass
    return False

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
