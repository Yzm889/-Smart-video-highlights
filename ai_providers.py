# -*- coding: utf-8 -*-
"""引擎/工具模块（由 webui_server.py 第4批拆分生成，符号与拆分前等价）。"""
import os, json, threading, time
# ---- 跨模块依赖（拆分后显式导入） ----
from cache_utils import HERE, WORKDIR
from ffmpeg_utils import AbortError, PROGRESS, _TLS, ffmpeg_run

# 本来在 webui 模块级、仅供本模块使用的常量（随迁移）；webui 侧通过 re-export 保持旧命名空间
AI_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ai_config.json')
_WHISPER_MODELS = {}

_WHISPER_KNOWN = {
    'tiny', 'base', 'small', 'medium',
    'large-v3', 'large-v2', 'large',
    'distil-large-v3', 'distil-large-v2', 'distil-medium',
    'distil-small',
}

def refresh_whisper_models():
    """扫描 models/whisper/ 目录，把已下载的模型名写入 _WHISPER_MODELS。
    同时保留已知模型名（即使尚未下载，也允许用户选择后触发下载）。
    前端 /api/whisper/status 的 valid_models 与本函数联动——用户下载新模型后点刷新即可热切换。"""
    _WHISPER_MODELS.clear()
    for name in _WHISPER_KNOWN:
        _WHISPER_MODELS[name] = {'name': name, 'downloaded': False}
    md = whisper_models_dir()
    if not os.path.isdir(md):
        return
    for entry in os.listdir(md):
        full = os.path.join(md, entry)
        if not os.path.isdir(full):
            continue
        names = []
        try:
            names = os.listdir(full)
        except Exception:
            pass
        if 'model.bin' in names or any(n.endswith('.safetensors') for n in names):
            _WHISPER_MODELS.setdefault(entry, {'name': entry, 'downloaded': False})
            _WHISPER_MODELS[entry]['downloaded'] = True
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


def _load_wav_as_float32(wav_path):
    """把 16kHz/单声道/16bit PCM 的 wav 读成 float32 numpy 数组。

    为什么不用 faster_whisper 内部的 decode_audio：那条路径走 PyAV 的
    av.open(..., metadata_errors='ignore')，而 PyAV>=15 已移除该关键字参数
    （本机 av19.0.1 + faster-whisper1.2.1 实测抛
    `TypeError: open() got an unexpected keyword argument 'metadata_errors'`），
    结果整个 ASR 静默失败。我们自己产出的 wav 一定是标准 PCM，用 stdlib wave
    读即可，既绕开版本耦合，也省掉一次解码。"""
    import wave
    import numpy as np
    with wave.open(wav_path, 'rb') as wf:
        if wf.getsampwidth() != 2 or wf.getnchannels() != 1:
            raise RuntimeError('wav 格式不符：需要 16bit 单声道，实际 %dbit/%dch'
                               % (wf.getsampwidth() * 8, wf.getnchannels()))
        raw = wf.readframes(wf.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


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
        _use_gpu = (device == 'cuda')

        def _transcribe(dev=None, ctype_=None):
            # 模型加载 + 转写 + 消费生成器：整段处于推理态
            model = WhisperModel(_whisper_load_path(whisper_model_name()),
                                 device=dev or device, compute_type=ctype_ or ctype,
                                 download_root=whisper_models_dir())
            segments, info = model.transcribe(_load_wav_as_float32(wav), language='zh', vad_filter=True,
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
        try:
            if os.path.exists(wav):
                os.remove(wav)
        except Exception:
            pass
        return []

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
        'model': cfg.get('model') or 'qwen2.5:latest',
        'api_key': cfg.get('api_key') or '',
    }

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
    payload = _llm_payload(cfg['model'], messages)
    headers = {'Content-Type': 'application/json'}
    if cfg['api_key']:
        headers['Authorization'] = 'Bearer ' + cfg['api_key']
    req = urllib.request.Request(cfg['base_url'] + '/chat/completions',
                                 data=_json.dumps(payload).encode('utf-8'), headers=headers)

    def _call():
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = _json.loads(resp.read().decode('utf-8'))
        return _strip_think((data.get('choices') or [{}])[0].get('message', {}).get('content', ''))

    # S7: 本地推理独占 GPU 槽；远端（云端/远程 Ollama）不加锁
    if _is_local_url(cfg['base_url']):
        with _gpu_slot():
            return _call()
    return _call()

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


def local_llm_light_chat(prompt, system=None, timeout=180):
    """S4: 轻量辅助模型调用（Ollama /v1/chat/completions）。未配置抛 RuntimeError，
    由调用方回退主模型；本地端点同样独占 GPU 槽（S7），远端不加锁。"""
    cfg = local_llm_light_cfg()
    if not cfg:
        raise RuntimeError('轻量辅助模型未配置')
    import urllib.request, json as _json
    messages = []
    if system:
        messages.append({'role': 'system', 'content': system})
    messages.append({'role': 'user', 'content': prompt})
    payload = _llm_payload(cfg['model'], messages)
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

    def _call():
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = _json.loads(resp.read().decode('utf-8'))
        if c['mode'] == 'ollama':
            return (data.get('message') or {}).get('content', '')
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
                           capture_output=True, text=True, timeout=5)
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
