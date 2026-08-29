# -*- coding: utf-8 -*-
"""
webui_server.py — 春天短视频工坊 · 本地图形化 WebUI 后端

功能：
  - 在 http://127.0.0.1:PORT/ 提供图形化网页
  - 支持拖入/上传 图片 和 视频，混排成一条时间线
  - 点击“合成”，把 图片(带 Ken Burns 镜头运动) + 视频片段 用交叉淡入淡出
    串成一段短.mp4，网页内预览并可保存。

依赖：Pillow / numpy / imageio-ffmpeg（第一次会自动 pip 安装）
"""
import os, sys, json, math, random, shutil, subprocess, threading, time, base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
WORKDIR = os.path.join(HERE, 'webui_workspace')
PROGRESS = {}          # runid -> mutable progress dict for the UI poller
PLANS = {}             # runid -> 人机协同「规划方案」（分析结果，等待用户确认/微调后再渲染）
RUNSEQ = [0]           # monotonic run id counter
import threading as _threading
OUTDIR = os.path.join(HERE, 'webui_output')
RUN_PROCS = {}          # runid -> 当前活跃的 ffmpeg Popen（用于取消时终止）
_PROC_LOCK = threading.Lock()
_TLS = threading.local()   # 每个任务线程绑定自己的 runid，供 ffmpeg_run 读取

class AbortError(Exception):
    """任务被用户取消时抛出。"""
    pass


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
FONT_PATH = "C:/Windows/Fonts/msyh.ttc"
AI_CONFIG_PATH = os.path.join(HERE, 'ai_config.json')
HISTORY_PATH = os.path.join(HERE, 'history.json')
STATIC_DIR = os.path.join(HERE, 'static')
_LAST_TTS_ERR = ''

def load_ai_config():
    try:
        with open(AI_CONFIG_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def save_ai_config(cfg):
    with open(AI_CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return cfg


def mirror_cfg():
    """国内下载镜像配置：让 whisper(来自 HuggingFace) 与 ollama 模型拉取走镜像/代理，免科学上网。"""
    cfg = load_ai_config().get('mirror') or {}
    return {
        'hf_endpoint': (cfg.get('hf_endpoint') or 'https://hf-mirror.com').strip(),
        'use_hf_mirror': bool(cfg.get('use_hf_mirror', True)),  # 默认开，中文用户友好
        'ollama_proxy': (cfg.get('ollama_proxy') or '').strip(),
    }


# Ollama 安装包（OllamaSetup.exe）GitHub 加速镜像候选池：网页会自动探测其中可用者，免去人工替换失效链接。
# 前缀规则：base + '/https://github.com/ollama/ollama/releases/latest'
OLLAMA_INSTALL_MIRRORS = [
    'https://ghfast.top',
    'https://gh.llkk.cc',
    'https://gh-proxy.com',
    'https://ghproxy.com',
    'https://gh.ddlc.top',
    'https://gh.idayer.com',
    'https://gh.widyun.com',
    'https://gh.con.sh',
]


def probe_ollama_mirror(base, timeout=10):
    """探测单个 GitHub 加速镜像是否可用（能正常打开 Ollama releases 页、且证书安全）。
    返回 (ok, note)。证书不安全/超时/连接失败都会被判为不可用，正好过滤掉 ghproxy.net 这类坑。"""
    import ssl, urllib.request, urllib.error
    base = (base or '').strip().rstrip('/')
    if not base:
        return False, '空地址'
    target = base + '/https://github.com/ollama/ollama/releases/latest'
    try:
        req = urllib.request.Request(target, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.getcode()
            if code != 200:
                return False, 'HTTP %d' % code
            data = resp.read(300000).decode('utf-8', 'ignore')
        if 'Ollama' in data and ('OllamaSetup' in data or 'releases' in data or 'Setup' in data):
            return True, '可用：含 OllamaSetup.exe 下载'
        if 'Ollama' in data:
            return True, '可用（页面含 Ollama，建议点开确认）'
        return False, '未返回有效版本页'
    except _urlerr.HTTPError as e:
        return False, 'HTTP %d' % e.code
    except urllib.error.URLError as e:
        rs = str(getattr(e, 'reason', e))
        if 'CERT' in rs.upper() or 'certificate' in rs.lower():
            return False, '证书不安全'
        if 'timed out' in rs.lower() or 'timeout' in rs.lower():
            return False, '超时'
        return False, '连接失败'
    except ssl.SSLError:
        return False, '证书不安全'
    except Exception as e:
        return False, '失败：%s' % str(e)[:40]


def scan_ollama_mirrors(timeout=10):
    """并发探测全部候选镜像，返回 {mirrors:[{base,url,ok,note}], best(首个可用的 base 或 None), scanned_at}。"""
    import concurrent.futures, time
    mirrors = OLLAMA_INSTALL_MIRRORS

    def _probe(m):
        ok, note = probe_ollama_mirror(m, timeout)
        return {'base': m, 'url': m + '/https://github.com/ollama/ollama/releases/latest',
                'ok': ok, 'note': note}

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(mirrors))) as ex:
        results = list(ex.map(_probe, mirrors))
    best = next((r['base'] for r in results if r['ok']), None)
    return {'mirrors': results, 'best': best, 'scanned_at': int(time.time())}


def ai_enabled(section='chat'):
    """判断某个 AI 段是否可用（base_url + api_key + model 齐备）。
    section='chat' 时兼容旧配置：没有 chat 段则回退看 vision 段（deepseek 等共用端点）。"""
    cfg = load_ai_config().get(section) or {}
    if section == 'chat' and not cfg:
        cfg = load_ai_config().get('vision') or {}
    return bool(cfg.get('base_url') and cfg.get('api_key') and cfg.get('model'))


def chat_cfg():
    """解说/剧情类纯文本 LLM 配置：优先读 ai_config.chat，回退到 vision（兼容旧配置，
    因 deepseek 等 chat 与 vision 共用 /chat/completions 端点）。画面描述仍走 vision 段。"""
    cfg = load_ai_config()
    return cfg.get('chat') or cfg.get('vision') or {}


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


def local_llm_enabled():
    """是否已启用本地模型（配置层面：enabled 且填了 base_url）。"""
    cfg = local_llm_cfg()
    return bool(cfg['enabled'] and cfg['base_url'])


def local_llm_ping():
    """轻量探活：GET {base_url}/models。返回 (ok, message)。不加载模型、不阻塞。"""
    cfg = local_llm_cfg()
    if not cfg['base_url']:
        return False, '未配置 base_url'
    import urllib.request
    try:
        req = urllib.request.Request(cfg['base_url'] + '/models', method='GET')
        if cfg['api_key']:
            req.add_header('Authorization', 'Bearer ' + cfg['api_key'])
        with urllib.request.urlopen(req, timeout=8) as r:
            return (r.status == 200), ('本地模型服务可达' if r.status == 200 else '服务返回 %s' % r.status)
    except Exception as e:
        return False, str(e)[:200]


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
    return (data.get('choices') or [{}])[0].get('message', {}).get('content', '')


# ---------------------------------------------------------------------------
# Whisper (本地 ASR) 模型配置：可切换 tiny/base/small/medium/large，权重缓存进项目目录
# ---------------------------------------------------------------------------
_WHISPER_MODELS = ['tiny', 'base', 'small', 'medium', 'large-v3']

def whisper_models_dir():
    """faster-whisper 模型权重统一缓存到项目 models/whisper，方便引导用户管理/查看。"""
    return os.path.join(HERE, 'models', 'whisper')

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

def whisper_model_name():
    """读取配置的 whisper 模型名（默认 base）。非法值回退 base。"""
    cfg = load_ai_config().get('whisper') or {}
    m = (cfg.get('model') or 'base')
    return m if m in _WHISPER_MODELS else 'base'

def whisper_model_ready(model=None):
    """检测指定(或当前配置)模型权重是否已在本机缓存，可直接加载。
    兼容两种布局：① 手动 aria2c 放置到 models/whisper/<m>/ 子目录；② faster-whisper 默认平铺到 models/whisper/。
    判据以 model.bin（核心权重）为准，避免 ai_config.json 等文件被 endswith('config.json') 误判为模型已就绪。"""
    m = model or whisper_model_name()

    def _check(dirpath):
        if not os.path.isdir(dirpath):
            return False
        try:
            names = os.listdir(dirpath)
        except Exception:
            return False
        if 'model.bin' in names:
            return True
        # 兼容某些发行版只放 .safetensors / vocab 的情况：需同时存在配置文件才视为就绪
        return (('config.json' in names or 'vocabulary.txt' in names)
                and any(n.endswith(('.bin', '.safetensors')) for n in names))

    # ① 子目录形式（手动放置）
    if _check(os.path.join(whisper_models_dir(), m)):
        return True
    # ② 平铺形式（faster-whisper 直接下载到 download_root）
    if _check(whisper_models_dir()):
        return True
    return False

_WHISPER_REPO = {
    'tiny': 'Systran/faster-whisper-tiny',
    'base': 'Systran/faster-whisper-base',
    'small': 'Systran/faster-whisper-small',
    'medium': 'Systran/faster-whisper-medium',
    'large-v3': 'Systran/faster-whisper-large-v3',
}

def whisper_prepare(model=None):
    """预下载/加载指定 whisper 模型（触发 faster-whisper 自动下载到项目目录）。返回 (ok,msg)。"""
    m = model or whisper_model_name()
    try:
        from faster_whisper import WhisperModel
    except Exception as e:
        return False, '未安装 faster-whisper：' + str(e)[:160]
    try:
        device, ctype = whisper_device()
        _whisper_env_setup()
        local = _whisper_load_path(m)
        if local != m:
            # 本地目录已就绪（手动 aria2c 放置 / 上次已复制真实文件）→ 直接加载，不再联网
            WhisperModel(local, device=device, compute_type=ctype)
            return True, '模型 %s 已就绪' % m
        # 本地未就绪 → 显式下载为「真实文件」(禁用符号链接)：Windows 下 huggingface_hub 默认用符号链接，
        # 在 bat 启动的标准 Python 中常解析失败 → WhisperModel 秒报错、状态回退「未下载」。强制复制可根除该坑。
        try:
            import huggingface_hub
            repo = _WHISPER_REPO.get(m, 'Systran/faster-whisper-' + m)
            huggingface_hub.snapshot_download(
                repo,
                local_dir=os.path.join(whisper_models_dir(), m),
                local_dir_use_symlinks=False,
            )
            # 下载成功 → 加载扁平目录（真实文件，无符号链接）
            WhisperModel(os.path.join(whisper_models_dir(), m), device=device, compute_type=ctype)
        except Exception:
            # 镜像 snapshot 拉取失败 → 退回 faster-whisper 自带下载（走同一镜像与代理环境）
            WhisperModel(m, device=device, compute_type=ctype, download_root=whisper_models_dir())
        return True, '模型 %s 已就绪' % m
    except Exception as e:
        e2 = str(e)
        hint = ''
        if '10061' in e2 or '拒绝' in e2 or 'ConnectError' in e2:
            hint = '（多为代理未开/镜像连不上：请在「🌐 国内下载镜像」检查代理是否运行，或改用直连；改完点「下载/预载」重试）'
        return False, '加载/下载失败：' + e2[:160] + hint

def whisper_download_async(model=None):
    """后台异步预下载 whisper 模型（避免阻塞 HTTP 请求）。通过 /api/whisper/status 轮询。"""
    m = model or whisper_model_name()
    if WHISPER_DL['running']:
        return False, '已有下载任务进行中（%s）' % WHISPER_DL['model']
    WHISPER_DL['model'] = m
    threading.Thread(target=_whisper_download_thread, args=(m,), daemon=True).start()
    return True, '已开始下载 %s（可在状态中查看进度）' % m


WHISPER_DL = {'model': None, 'running': False, 'ok': None, 'msg': ''}

def _whisper_download_thread(model):
    WHISPER_DL['running'] = True
    WHISPER_DL['ok'] = None
    WHISPER_DL['msg'] = '下载中…'
    # 统一环境：走 HF 镜像 + 清理失效代理（代理没开时 huggingface_hub 会撞 127.0.0.1 被拒 → 10061）
    _whisper_env_setup()
    ok, msg = whisper_prepare(model)
    WHISPER_DL['running'] = False
    WHISPER_DL['ok'] = ok
    WHISPER_DL['msg'] = msg


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

def vlm_enabled():
    return bool(vlm_cfg()['enabled'] and vlm_cfg()['base_url'])

def vlm_ping():
    """探活 + 检测目标模型是否已拉取。返回 (ok, msg)。"""
    c = vlm_cfg()
    if not c['base_url']:
        return False, '未配置 VLM 地址'
    import urllib.request, json as _json
    try:
        if c['mode'] == 'ollama':
            req = urllib.request.Request(c['base_url'] + '/api/tags', method='GET')
            with urllib.request.urlopen(req, timeout=8) as r:
                data = _json.loads(r.read().decode('utf-8'))
            models = [m.get('name', '') for m in (data.get('models') or [])]
            if c['model'] in models:
                return True, 'Ollama 可达，模型 %s 已就绪' % c['model']
            return False, 'Ollama 可达，但未拉取 %s（请执行 ollama pull %s）' % (c['model'], c['model'])
        else:
            req = urllib.request.Request(c['base_url'] + '/models', method='GET')
            if c['api_key']:
                req.add_header('Authorization', 'Bearer ' + c['api_key'])
            with urllib.request.urlopen(req, timeout=8) as r:
                return (r.status == 200), 'VLM 服务可达'
    except Exception as e:
        return False, str(e)[:200]

def vlm_chat(image_path, text, system=None, timeout=180):
    """调用本地/云端 VLM：传入一张图 + 文本，返回模型文本回复。失败抛异常。"""
    c = vlm_cfg()
    if not c['base_url']:
        raise RuntimeError('VLM 未配置')
    import urllib.request, base64 as _b64, json as _json
    with open(image_path, 'rb') as f:
        b64 = _b64.b64encode(f.read()).decode('ascii')
    headers = {'Content-Type': 'application/json'}
    if c['api_key']:
        headers['Authorization'] = 'Bearer ' + c['api_key']
    if c['mode'] == 'ollama':
        msg = {'role': 'user', 'content': text, 'images': [b64]}
        messages = ([{'role': 'system', 'content': system}] if system else []) + [msg]
        payload = {'model': c['model'], 'messages': messages, 'stream': False}
        url = c['base_url'] + '/api/chat'
    else:
        content = [{'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + b64}},
                   {'type': 'text', 'text': text}]
        messages = ([{'role': 'system', 'content': system}] if system else []) + [{'role': 'user', 'content': content}]
        payload = {'model': c['model'], 'messages': messages, 'max_tokens': 600, 'temperature': 0.7}
        url = c['base_url'] + '/v1/chat/completions'
    req = urllib.request.Request(url, data=_json.dumps(payload).encode('utf-8'), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = _json.loads(resp.read().decode('utf-8'))
    if c['mode'] == 'ollama':
        return (data.get('message') or {}).get('content', '')
    return (data.get('choices') or [{}])[0].get('message', {}).get('content', '')


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


def vlm_text(text, system=None, timeout=180):
    """调用本地/云端 VLM 的纯文本能力（不带图）。用于在已有多帧剧情理解后，纯文本生成连贯解说稿，
    避免逐段重复看图导致的复读。失败抛异常。"""
    c = vlm_cfg()
    if not c['base_url']:
        raise RuntimeError('VLM 未配置')
    import urllib.request, json as _json
    headers = {'Content-Type': 'application/json'}
    if c['api_key']:
        headers['Authorization'] = 'Bearer ' + c['api_key']
    if c['mode'] == 'ollama':
        messages = ([{'role': 'system', 'content': system}] if system else []) + [{'role': 'user', 'content': text}]
        payload = {'model': c['model'], 'messages': messages, 'stream': False}
        url = c['base_url'] + '/api/chat'
    else:
        messages = ([{'role': 'system', 'content': system}] if system else []) + [{'role': 'user', 'content': text}]
        payload = {'model': c['model'], 'messages': messages, 'max_tokens': 1200, 'temperature': 0.8}
        url = c['base_url'] + '/v1/chat/completions'
    req = urllib.request.Request(url, data=_json.dumps(payload).encode('utf-8'), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = _json.loads(resp.read().decode('utf-8'))
    if c['mode'] == 'ollama':
        return (data.get('message') or {}).get('content', '')
    return (data.get('choices') or [{}])[0].get('message', {}).get('content', '')


VLM_PULL = {'model': None, 'running': False, 'ok': None, 'msg': '', 'pct': 0}

def vlm_pull_async(model=None):
    """后台异步执行 `ollama pull <model>`（避免阻塞请求）。通过 /api/vlm/status 轮询。"""
    m = model or vlm_cfg()['model']
    if VLM_PULL['running']:
        return False, '已有拉取任务进行中'
    VLM_PULL['model'] = m
    threading.Thread(target=_vlm_pull_thread, args=(m,), daemon=True).start()
    return True, '已开始拉取 %s' % m

def _vlm_pull_thread(model):
    VLM_PULL['running'] = True
    VLM_PULL['ok'] = None
    VLM_PULL['pct'] = 0
    VLM_PULL['msg'] = '拉取中…'
    c = vlm_cfg()
    try:
        import subprocess as _sp, re as _re, urllib.request, json as _json
        if c['mode'] != 'ollama':
            VLM_PULL['ok'] = False
            VLM_PULL['msg'] = '当前为 openai 模式，请在你部署的服务端手动拉取模型'
            return
        base = c['base_url'].replace('/v1', '').rstrip('/')
        if not base:
            VLM_PULL['ok'] = False
            VLM_PULL['msg'] = 'VLM base_url 为空，请填写 Ollama 地址（如 http://localhost:11434）'
            return
        # 启动前先探测 Ollama 服务是否可用，避免拉取命令静默卡死 / 无报错
        try:
            _req = urllib.request.Request(base + '/api/version', method='GET')
            with urllib.request.urlopen(_req, timeout=8) as _r:
                _json.loads(_r.read().decode('utf-8'))
        except Exception as _e:
            VLM_PULL['ok'] = False
            VLM_PULL['msg'] = ('Ollama 服务未响应（%s）。请确认已安装并启动 Ollama（托盘应出现图标），'
                               '再点拉取；若端口不是 11434 请在上方修改 base_url。' % str(_e)[:120])
            return
        # 国内镜像：若配置了代理，让 ollama pull 走代理拉取，免科学上网
        mc = mirror_cfg()
        if mc['ollama_proxy']:
            os.environ['HTTP_PROXY'] = mc['ollama_proxy']
            os.environ['HTTPS_PROXY'] = mc['ollama_proxy']
        # 流式读取 ollama pull 输出，实时解析进度百分比（ollama 用 \r 原地刷新进度，
        # Python 文本模式会按行切分），避免全过程无进度、卡死也无报错。
        try:
            p = _sp.Popen(['ollama', 'pull', model], stdout=_sp.PIPE, stderr=_sp.STDOUT,
                          text=True, bufsize=1, encoding='utf-8', errors='ignore')
        except FileNotFoundError:
            VLM_PULL['ok'] = False
            VLM_PULL['msg'] = '未找到 ollama 命令：Ollama 未安装或未加入 PATH。请先安装 Ollama 并重启本工具。'
            return
        def _reader(stream):
            for _raw in stream:
                _line = _raw.strip()
                if not _line:
                    continue
                VLM_PULL['msg'] = _line[:400]
                _m = _re.search(r'(\d{1,3}(?:\.\d+)?)\s*%', _line)
                if _m:
                    try:
                        VLM_PULL['pct'] = min(100, max(0, float(_m.group(1))))
                    except Exception:
                        pass
        _t = threading.Thread(target=_reader, args=(p.stdout,), daemon=True)
        _t.start()
        try:
            rc = p.wait(timeout=1800)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
            rc = -1
        try:
            _t.join(timeout=5)
        except Exception:
            pass
        VLM_PULL['ok'] = (rc == 0)
        if rc != 0 and not VLM_PULL['msg']:
            VLM_PULL['msg'] = 'ollama pull 退出码 %d（可能网络不通 / 模型名错误 / 磁盘空间不足）' % rc
    except Exception as e:
        VLM_PULL['ok'] = False
        VLM_PULL['msg'] = str(e)[:300]
    finally:
        VLM_PULL['running'] = False

LOCAL_PULL = {'model': None, 'running': False, 'ok': None, 'msg': '', 'pct': 0}

def local_model_exists(model):
    """查询 Ollama 是否已存在该模型（/api/tags），避免对已装模型触发整包重复拉取。返回 True/False/None(未知)。"""
    cfg = local_llm_cfg()
    root = cfg['base_url'].replace('/v1', '').rstrip('/')
    if not root:
        return None
    import urllib.request, json as _json
    try:
        req = urllib.request.Request(root + '/api/tags', method='GET')
        if cfg['api_key']:
            req.add_header('Authorization', 'Bearer ' + cfg['api_key'])
        with urllib.request.urlopen(req, timeout=8) as r:
            data = _json.loads(r.read().decode('utf-8'))
        names = [m.get('name', '') for m in data.get('models', [])]
        base = model.split(':')[0]
        for n in names:
            if n == model or n.startswith(model + ':') or n == base:
                return True
        return False
    except Exception:
        return None

def local_pull_async(model=None, force=False):
    """后台异步执行 `ollama pull <model>`（文字解说模型）。通过 /api/local/status 轮询。
    若模型已存在则跳过重复拉取（手动 ollama create 导入的模型与官方打包版 manifest 不同，
    直接 ollama pull 会误判为需整包重下，浪费带宽）。"""
    m = model or local_llm_cfg()['model']
    if LOCAL_PULL['running']:
        return False, '已有拉取任务进行中'
    if not force:
        exists = local_model_exists(m)
        if exists is True:
            LOCAL_PULL['running'] = False
            LOCAL_PULL['ok'] = True
            LOCAL_PULL['model'] = m
            LOCAL_PULL['msg'] = '模型已存在，无需重复拉取（可直接点🧪测试连接）'
            return True, '模型已存在，无需重复拉取'
    LOCAL_PULL['model'] = m
    threading.Thread(target=_local_pull_thread, args=(m,), daemon=True).start()
    return True, '已开始拉取 %s' % m

# 国内加速源表：优先用「非 split 单文件 GGUF（bartowski, Q4_K_M）+ aria2c 多线程下载 + ollama create 本地导入」，
# 远快于官方 registry.ollama.ai。键为 ollama 模型名，值为 (GGUF 直链, 本地文件名)。
FAST_GGUF_SOURCES = {
    'qwen2.5:14b': ('https://hf-mirror.com/bartowski/Qwen2.5-14B-Instruct-GGUF/resolve/main/Qwen2.5-14B-Instruct-Q4_K_M.gguf', 'qwen2.5-14b-instruct-q4_k_m.gguf'),
    'qwen2.5:7b': ('https://hf-mirror.com/bartowski/Qwen2.5-7B-Instruct-GGUF/resolve/main/Qwen2.5-7B-Instruct-Q4_K_M.gguf', 'qwen2.5-7b-instruct-q4_k_m.gguf'),
    'qwen2.5:latest': ('https://hf-mirror.com/bartowski/Qwen2.5-7B-Instruct-GGUF/resolve/main/Qwen2.5-7B-Instruct-Q4_K_M.gguf', 'qwen2.5-7b-instruct-q4_k_m.gguf'),
}


def _fast_pull_local(model):
    """加速通道：下载非 split 单文件 GGUF（bartowski Q4_K_M）+ aria2c 多线程 + ollama create 导入。
    返回 (ok, msg)。失败返回 (False, 原因)，由调用方回退官方源。"""
    import subprocess as _sp, urllib.request, shutil, time as _t, re as _re
    src = FAST_GGUF_SOURCES.get(model)
    if not src:
        return False, '该模型没有内置加速源'
    url, fname = src
    aria = shutil.which('aria2c')
    if not aria:
        return False, '未找到 aria2c（多线程下载器），可安装 aria2 后重试，或改用官方源'
    dl_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_dl')
    os.makedirs(dl_dir, exist_ok=True)
    target = os.path.join(dl_dir, fname)
    # 解析文件总大小（Content-Range），拿不到则按 0 处理（进度显示为字节数）
    total = 0
    try:
        _req = urllib.request.Request(url)
        _req.add_header('Range', 'bytes=0-0')
        with urllib.request.urlopen(_req, timeout=30) as _r:
            _cr = _r.headers.get('Content-Range', '') or ''
            if '/' in _cr:
                total = int(_cr.split('/')[-1])
    except Exception:
        pass
    # 下载（-c 断点续传；已有完整文件则跳过）
    if not (os.path.exists(target) and (not total or os.path.getsize(target) >= total)):
        p = _sp.Popen([aria, '-c', '-x', '8', '-s', '8', '-k', '1M', '--max-tries=0',
                       '--retry-wait=3', '--timeout=60', '--console-log-level=warn',
                       '-o', fname, url], cwd=dl_dir,
                      stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        while p.poll() is None:
            if total and os.path.exists(target):
                LOCAL_PULL['pct'] = min(92, int(os.path.getsize(target) * 100 // max(1, total)))
            _t.sleep(1)
        if p.returncode != 0:
            return False, 'aria2c 下载失败（退出码 %d），已回退官方源' % p.returncode
    # ollama create 导入（cmd /c 规避 PowerShell 对 stderr 进度条的误判；Modelfile 用绝对路径）
    mf = os.path.join(dl_dir, 'Modelfile_' + _re.sub(r'[^0-9A-Za-z]', '_', model))
    with open(mf, 'w', encoding='utf-8') as f:
        f.write('FROM ' + target.replace('\\', '/') + '\n')
    try:
        r = _sp.run(['cmd', '/c', 'ollama', 'create', model, '-f', mf],
                    capture_output=True, text=True, timeout=1200)
    except Exception as e:
        return False, 'ollama create 失败：%s' % str(e)[:120]
    finally:
        for _f in (target, mf):
            try:
                os.remove(_f)
            except Exception:
                pass
    if r.returncode != 0:
        return False, 'ollama create 失败：%s' % ((r.stdout or r.stderr or '')[-200:])
    return True, '加速通道完成：%s（约 %d MB）' % (fname, (total or 0) // 1048576)


def _local_pull_thread(model):
    LOCAL_PULL['running'] = True
    LOCAL_PULL['ok'] = None
    LOCAL_PULL['pct'] = 0
    LOCAL_PULL['msg'] = '拉取中…'
    try:
        import subprocess as _sp, re as _re, urllib.request, json as _json
        cfg = local_llm_cfg()
        root = (cfg.get('base_url') or '').replace('/v1', '').rstrip('/')
        if not root:
            LOCAL_PULL['ok'] = False
            LOCAL_PULL['msg'] = '本地模型 base_url 为空，请填写 Ollama 地址（如 http://localhost:11434）'
            return
        # 启动前先探测 Ollama 服务是否可用，避免拉取命令静默卡死 / 无报错
        try:
            _req = urllib.request.Request(root + '/api/version', method='GET')
            with urllib.request.urlopen(_req, timeout=8) as _r:
                _json.loads(_r.read().decode('utf-8'))
        except Exception as _e:
            LOCAL_PULL['ok'] = False
            LOCAL_PULL['msg'] = ('Ollama 服务未响应（%s）。请确认已安装并启动 Ollama（托盘应出现图标），'
                                 '再点拉取；若端口不是 11434 请在上方修改 base_url。' % str(_e)[:120])
            return
        mc = mirror_cfg()
        if mc['ollama_proxy']:
            os.environ['HTTP_PROXY'] = mc['ollama_proxy']
            os.environ['HTTPS_PROXY'] = mc['ollama_proxy']
        # ---- 优先走国内加速通道（bartowski 单文件 + aria2c + ollama create），失败再回退官方源 ----
        if model in FAST_GGUF_SOURCES:
            LOCAL_PULL['msg'] = '使用国内加速通道（多线程下载单文件 GGUF + 本地导入）…'
            _fok, _fmsg = _fast_pull_local(model)
            if _fok:
                LOCAL_PULL['ok'] = True
                LOCAL_PULL['pct'] = 100
                LOCAL_PULL['msg'] = '✅ ' + _fmsg
                return
            LOCAL_PULL['msg'] = '加速通道未成功（%s），回退官方源拉取…' % _fmsg
        # 流式读取 ollama pull 输出，实时解析进度百分比（ollama 用 \r 原地刷新，Python 文本模式按行切分）
        try:
            p = _sp.Popen(['ollama', 'pull', model], stdout=_sp.PIPE, stderr=_sp.STDOUT,
                          text=True, bufsize=1, encoding='utf-8', errors='ignore')
        except FileNotFoundError:
            LOCAL_PULL['ok'] = False
            LOCAL_PULL['msg'] = '未找到 ollama 命令：Ollama 未安装或未加入 PATH。请先安装 Ollama 并重启本工具。'
            return
        def _reader(stream):
            for _raw in stream:
                _line = _raw.strip()
                if not _line:
                    continue
                LOCAL_PULL['msg'] = _line[:400]
                _m = _re.search(r'(\d{1,3}(?:\.\d+)?)\s*%', _line)
                if _m:
                    try:
                        LOCAL_PULL['pct'] = min(100, max(0, float(_m.group(1))))
                    except Exception:
                        pass
        _t = threading.Thread(target=_reader, args=(p.stdout,), daemon=True)
        _t.start()
        try:
            rc = p.wait(timeout=1800)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
            rc = -1
        try:
            _t.join(timeout=5)
        except Exception:
            pass
        LOCAL_PULL['ok'] = (rc == 0)
        if rc != 0 and not LOCAL_PULL['msg']:
            LOCAL_PULL['msg'] = 'ollama pull 退出码 %d（可能网络不通 / 模型名错误 / 磁盘空间不足）' % rc
    except Exception as e:
        LOCAL_PULL['ok'] = False
        LOCAL_PULL['msg'] = str(e)[:300]
    finally:
        LOCAL_PULL['running'] = False

def extract_segment_frames(video_path, segs, out_dir, max_side=640):
    """为每个镜头段抽 1 张中间帧(jpg)，用于 VLM 视觉理解。返回 {seg_idx: frame_path}。"""
    os.makedirs(out_dir, exist_ok=True)
    frames = {}
    vdur = probe_audio_len(video_path) or 0.0
    for i, (s0, s1) in enumerate(segs):
        mid = max(0.0, (s0 + s1) / 2.0)
        if vdur and mid > vdur:
            mid = max(0.0, vdur - 0.1)
        fp = os.path.join(out_dir, f'frame_{i}.jpg')
        rc, _o, _e = ffmpeg_run(['-y', '-ss', '%.3f' % mid, '-i', video_path,
                                '-frames:v', '1', '-vf', 'scale=min(iw\\,%d):-2' % max_side,
                                '-q:v', '4', '-an', fp])
        if rc == 0 and os.path.exists(fp):
            frames[i] = fp
    return frames

def _plot_brief(frames, per_seg, params):
    """剧情理解（阶段一）：把整片关键帧（按时间顺序）+ 各段台词一次性喂给 VLM，
    判断片段类型 / 识别影视作品 / **结合台词重构具体剧情事件**，返回剧情梗概文本。失败返回 None。
    这是「剧情解说」与「画面描述」的分水岭：先让模型看懂整个故事，再写旁白。"""
    if not frames:
        return None
    idxs = sorted(frames.keys())
    step = max(1, (len(idxs) + 7) // 8)
    picked = [frames[i] for i in idxs[::step]][:8]
    if not picked:
        return None
    dlg = []
    for s0, s1, txt in per_seg:
        t = (txt or '').strip()
        dlg.append('%s-%s秒 台词：%s' % (int(s0), int(s1), t[:100] if t else '(无台词画面)'))
        if len(dlg) >= 12:
            break
    theme = (params.get('theme') or '').strip()
    name = (params.get('name') or '').strip()
    sys = '你是资深影视解说编辑，擅长从若干关键画面与台词重构一段视频的完整剧情。'
    prompt = ('以下是同一段视频中按时间先后排列的关键画面，以及对应时段的台词（台词可能含识别误差，但能帮助理解剧情）。\n'
              '请回答：\n'
              '1) 这是一段什么内容（影视剧片段 / 纪录片 / 口播 / 风光等）；\n'
              '2) 如果明显来自某部影视作品，请点出片名与年代背景；无法确定时务必写明“无法确认片名”，不要编造；\n'
              '3) 重点：结合台词与画面，把这段讲的具体剧情事件写清楚——人物是谁、身在何处、在做什么、'
              '台词里透露了哪些冲突或信息（例如借钱/赶路/争吵/求助等），结果如何；\n'
              '4) 最后用 4~6 句话完整概括这段视频的剧情（供后续解说使用）。\n')
    if name:
        prompt += '\n视频文件名：' + name
    if theme:
        prompt += '\n用户提供的主题/梗概：' + theme
    prompt += '\n\n各时段台词：\n' + '\n'.join(dlg)
    try:
        out = vlm_chat_multi(picked, prompt, system=sys, timeout=300)
    except Exception:
        return None
    return (out or '').strip()[:1200] or None


def _try_parse_json(text):
    """从模型输出中尽力提取 JSON 对象。返回 dict 或 None。"""
    if not text:
        return None
    import re as _re, json as _json
    try:
        return _json.loads(text)
    except Exception:
        pass
    m = _re.search(r'\{.*\}', text, _re.S)
    if m:
        try:
            return _json.loads(m.group(0))
        except Exception:
            pass
    return None


def _beat_plan(per_seg, plot, params):
    """详略规划（文字模型）：基于剧情理解 + 各环节台词，判断每段在剧情中的作用与重要性
    （key 关键/转折/高光 → 展开讲；advance 推进 → 正常讲；transition 过渡/铺垫 → 一句带过；
    mood 氛围/情绪 → 简略渲染）。返回 {summary, beats:[{i,importance,role}]}。
    这是「详略有当、像写作文一样」的关键，也是「理解场景含义」的落点。失败回退默认全部 advance。"""
    n = len(per_seg)
    default = {'summary': '', 'beats': [{'i': i + 1, 'importance': 'advance', 'role': ''} for i in range(n)]}
    if not per_seg:
        return default
    ctx = []
    if plot:
        ctx.append('【视觉剧情理解】' + plot)
    for i, (s0, s1, txt) in enumerate(per_seg):
        ctx.append('环节%d(%s-%s秒)：%s' % (i + 1, int(s0), int(s1), (txt or '').strip()[:90] or '(无台词画面)'))
    sys_ = '你是资深电影解说导演，擅长把一段视频拆成有详有略的剧情节奏。'
    prompt = ('下面是这段视频的整体剧情理解与各时间环节。请像写作文一样为每个环节标注「详略级别」和「剧情作用」。\n'
              'importance 只允许这 4 个取值：\n'
              '- key：剧情关键 / 转折 / 高光，需要展开讲透；\n'
              '- advance：推进剧情，正常讲；\n'
              '- transition：过渡 / 铺垫，一句带过即可；\n'
              '- mood：氛围 / 情绪镜头，简略渲染即可。\n'
              'role：用一句话说明本段在剧情推进中的作用——人物此时在做什么/处境或事态怎么变化/为后文做什么铺垫。'
              '讲事实与因果，不要强行升华时代、社会或抽象意义。\n'
              '另给出 summary：整段视频的剧情梗概（4~6 句）。\n'
              '只输出 JSON，不要其他任何文字：\n'
              '{"summary":"...","beats":[{"i":1,"importance":"advance","role":"..."}]}\n\n'
              + '\n'.join(ctx))
    out = None
    use_local = _local_model_available()
    try:
        if use_local:
            out = local_llm_chat(prompt, system=sys_, timeout=240)
        else:
            out = vlm_text(prompt, system=sys_, timeout=240)
    except Exception:
        out = None
    data = _try_parse_json(out)
    if not data or not isinstance(data.get('beats'), list):
        if data and data.get('summary'):
            default['summary'] = str(data['summary']).strip()
        return default
    beats = []
    for i in range(n):
        b = data['beats'][i] if i < len(data['beats']) and isinstance(data['beats'][i], dict) else {}
        imp = str(b.get('importance', 'advance') or 'advance')
        if imp not in ('key', 'advance', 'transition', 'mood'):
            imp = 'advance'
        beats.append({'i': i + 1, 'importance': imp, 'role': str(b.get('role', '') or '').strip()})
    return {'summary': str(data.get('summary', '') or '').strip(), 'beats': beats}


def _split_nar_lines(text):
    """把模型输出按行切分，去掉空行 / 编号前缀 / 引号。"""
    import re as _re
    if not text:
        return []
    lines = []
    for raw in _re.split(r'[\r\n]+', text):
        l = raw.strip().strip('"').strip()
        if not l:
            continue
        l = _re.sub(r'^(?:第?\d+[\.、)．:：]|\[\d+\]|（\d+）)\s*', '', l)
        if l:
            lines.append(l)
    return lines


def _split_nar_sentences(line):
    """把一个长行按句切分（。！？!?），用于行数不足时补齐镜头。"""
    import re as _re
    parts = _re.split(r'(?<=[。！？!?])', line)
    return [p.strip() for p in parts if p.strip()]


def _distribute_sents(sents, n):
    """把 m 个短句按镜头数 n 均匀分布拼接（每镜头至少一句，多句的合并）。"""
    m = len(sents)
    out = []
    for i in range(n):
        a = int(round(i * m / n))
        b = int(round((i + 1) * m / n))
        chunk = sents[a:b]
        out.append(''.join(chunk) if chunk else sents[min(i, m - 1)])
    return out


def _map_lines_to_segs(lines, n):
    """把整稿行映射回 n 个镜头：行数相等直接一一对应；不足时按句切分后均匀分布；过多则线性就近取行。"""
    m = len(lines)
    if m == 0:
        return ['' for _ in range(n)]
    if m == n:
        return list(lines)
    if m < n:
        sents = []
        for l in lines:
            sents.extend(_split_nar_sentences(l))
        if len(sents) >= n:
            return _distribute_sents(sents, n)
    out = []
    for i in range(n):
        if n <= 1:
            src = 0
        else:
            src = min(m - 1, int(round(i * (m - 1) / max(1, n - 1))))
        out.append(lines[src])
    return out


def local_vlm_narrate(per_seg, frames, params):
    """本地真解说（连贯真人版 · 整稿生成 + 少升华 + 前置要求 + 自优化）：
    ① _plot_brief 视觉理解；② _beat_plan 详略规划（重要镜头展开、过渡镜头带过）；
    ③ 一次生成「像真人一样从头讲到尾」的连贯解说整稿（一行对应一个镜头、自然衔接、
       重点讲剧情本身、非高光不升华），并让模型自查润色一遍（自优化）；
    ④ 按行切分回各镜头；整稿失败则逐段回退（少升华 + 承接上文）。返回 (lines, True)。"""
    templates = [
        '镜头缓缓推进，故事就此展开。', '画面一转，新的转折正在发生。',
        '气氛渐起，关键情节悄然铺开。', '人物登场，冲突拉开了序幕。',
        '悬念浮现，让人忍不住屏息。', '节奏陡然加快，高潮正在靠近。',
        '真相逼近，谜底即将揭晓。', '余波未平，故事仍在继续。',
    ]
    theme = (params.get('theme') or '').strip()
    name = (params.get('name') or '').strip()
    req = (params.get('req') or '').strip()
    plot = _plot_brief(frames, per_seg, params)
    beat = _beat_plan(per_seg, plot, params)
    summary = beat.get('summary', '')
    beats = beat.get('beats', [])
    use_local_text = _local_model_available()

    def _write(p, s_, timeout=300):
        if _aborted():
            raise AbortError('用户取消了任务')
        if use_local_text:
            try:
                return local_llm_chat(p, system=s_, timeout=timeout)
            except Exception:
                pass
        try:
            return vlm_text(p, system=s_, timeout=timeout)
        except Exception:
            return None

    n = len(per_seg)
    sys_ = '你是资深电影解说博主，正在给观众连续地讲这个故事，口气自然、像真人聊天讲故事一样。'
    req_line = '- 你的额外要求：%s\n' % req if req else ''

    # —— 整稿生成：像真人一样把故事从头讲到尾 ——
    seg_brief = []
    for i, (s0, s1, txt) in enumerate(per_seg):
        b = beats[i] if i < len(beats) else {}
        imp = b.get('importance', 'advance')
        tag = {'key': '（关键/高光，可展开）', 'transition': '（过渡）', 'mood': '（氛围）'}.get(imp, '')
        t = (txt or '').strip()[:80]
        seg_brief.append('第%d段 %s %s' % (i + 1, tag, ('台词：' + t if t else '无台词画面')))
    ctx = []
    if name:
        ctx.append('视频：' + name)
    if theme:
        ctx.append('主题/梗概：' + theme)
    if summary:
        ctx.append('【整段剧情梗概】' + summary)
    if plot:
        ctx.append('【视觉剧情理解】' + plot)
    prompt = ('下面是这段视频的整体剧情理解与各镜头环节（括号内是该镜头的详略提示）。\n'
              + '\n'.join(ctx + seg_brief)
              + ('\n\n请写【%d 行】连贯的中文电影解说词，一行对应一个镜头，从上到下依次是第1、第2…段：\n' % n)
              + '- 【必须】恰好输出 %d 行，一行对应一个镜头：不要合并镜头、不要把多个镜头写成一行；\n' % n
              + '- 像真人解说一样把故事从头讲到尾、一气呵成：镜头之间要自然衔接、层层递进'
                '（可用“此时/紧接着/可没想到/而另一边/偏偏这时候”等承接），不要每段都另起炉灶；\n'
              + '- 重点是【讲剧情本身】：这段发生了什么、人物做了什么说了什么、事态怎么变，像讲故事，不是描述画面；\n'
              + '- 详略有当：标“关键/高光”的多讲（可两句），过渡镜头一句带过，不要平均用力、不要每段一样长；\n'
              + '- 除非某镜头真的是剧情转折/高光，否则【不要】总结“这反映了/象征着/揭示了/暗示了”这类意义升华；\n'
              + '- 台词只转述大意，不原样照搬；不编造剧情里没有的事实；不堆“高潮/悬念/震撼”等空泛词。\n'
              + req_line
              + '直接输出 %d 行解说词，不要编号、不要解释。' % n)
    out = _write(prompt, sys_)
    lines = _split_nar_lines(out)

    # —— 自优化：让模型自查衔接/重复/详略并输出优化稿（整稿成形且用强文字模型时）——
    if use_local_text and len(lines) >= max(2, (n + 1) // 2):
        polish = ('下面是电影解说稿草稿（一行对应一个镜头）。\n' + '\n'.join(lines)
                  + '\n\n请以资深电影解说编辑的身份审阅并优化，输出【改进后的完整稿】，让整篇像真人电影解说一样流畅自然：'
                    '①镜头之间衔接更顺，有“接着讲下去”的连贯感；②删掉重复、套话和空泛的升华；'
                    '③保持详略得当（重要镜头多讲、过渡镜头短）；④每行对应一个镜头、行数不变。'
                    '直接输出优化后的完整稿，不要编号、不要解释。')
        out2 = _write(polish, '你是专业电影解说编辑。', timeout=300)
        if out2 and out2.strip():
            lines2 = _split_nar_lines(out2)
            if len(lines2) >= max(2, (n + 1) // 2):
                lines = lines2

    # —— 行 → 镜头 映射 ——
    if lines:
        mapped = _map_lines_to_segs(lines, n)
    else:
        # 整稿失败：逐段回退（少升华 + 承接上一段结尾，保持连贯）
        mapped = []
        prev_tail = ''
        LEN = {
            'key': '这段是剧情关键/转折/高光，可展开讲透：2~3 句、70~110 字。',
            'advance': '这段推进剧情，正常讲：1~2 句、40~70 字，聚焦本段实际发生的事。',
            'transition': '这段是过渡/铺垫，一句带过即可：20~40 字。',
            'mood': '这段是氛围/情绪镜头，简略渲染即可：20~40 字。',
        }
        for i, (s0, s1, txt) in enumerate(per_seg):
            b = beats[i] if i < len(beats) else {}
            imp = b.get('importance', 'advance')
            role_desc = b.get('role', '')
            ctx2 = []
            if i == 0:
                if name:
                    ctx2.append('视频：' + name)
                if theme:
                    ctx2.append('主题/梗概：' + theme)
                if summary:
                    ctx2.append('【整段剧情梗概】' + summary)
                if plot:
                    ctx2.append('【视觉剧情理解】' + plot)
            if role_desc:
                ctx2.append('【本段剧情作用】' + role_desc)
            ctx2.append('本段(%s-%s秒) %s' % (int(s0), int(s1), ('台词：' + txt.strip()[:80] if txt.strip() else '无明显台词')))
            if prev_tail:
                ctx2.append('【上一段结尾，接着往下讲】' + prev_tail)
            p2 = ('你是电影解说博主，正在给观众连续地讲这段视频的剧情。请为本段写中文解说旁白，'
                  '【接着上一段自然往下讲】。\n'
                  + ('第 1 段是开场：请写一句有吸引力的开场旁白引入剧情（能确认片名则点出片名/年代/背景；'
                     '未确认时用“故事从……”/“镜头对准……”自然引入）。\n' if i == 0 else '')
                  + '要求：' + LEN.get(imp, LEN['advance'])
                  + '围绕本段实际发生的剧情向前推进，讲清人物做了什么、事态怎么变，像讲故事；'
                    '除非这是真正的转折/高光，否则不要总结“这反映了/象征着/揭示了”这类意义升华；'
                    '不要描述画面本身；开头不要用“然而/但”等接续词；台词转述、不要原样引用；'
                    '不要编造剧情里没有的事实。'
                  + ('额外要求：' + req + '。' if req else '')
                  + '直接输出旁白，不要编号/引号/解释。\n\n' + '\n'.join(ctx2))
            o = _write(p2, sys_)
            seg_text = (o or '').strip().strip('"').strip()[:160] \
                or (txt.strip()[:40] if txt.strip() else templates[i % len(templates)])
            mapped.append(seg_text)
            prev_tail = seg_text[-18:]

    for i, l in enumerate(mapped):
        if not l or not l.strip():
            mapped[i] = (per_seg[i][2] or '').strip()[:40] or templates[i % len(templates)]
    return mapped[:n], True


_local_model_cache = {'t': 0, 'ok': False}

def _local_model_available():
    """检测本地「文字模型」是否真实可用（enabled 且对应 model 已在服务端列表里）。
    缓存 60s，避免每次解说都探活。这是「写解说稿的主力」——部署了 qwen2.5:14b 等文字模型，
    解说质量才会上一个台阶（qwen2.5vl 只负责看图，不负责写稿）。"""
    cfg = local_llm_cfg()
    if not (cfg['enabled'] and cfg['base_url']):
        return False
    now = time.time()
    if now - _local_model_cache['t'] < 60:
        return _local_model_cache['ok']
    ok = False
    try:
        import urllib.request, json as _json
        url = cfg['base_url'].rstrip('/') + '/models'
        req = urllib.request.Request(url, method='GET')
        if cfg['api_key']:
            req.add_header('Authorization', 'Bearer ' + cfg['api_key'])
        with urllib.request.urlopen(req, timeout=6) as r:
            data = _json.loads(r.read().decode('utf-8'))
        models = [m.get('id', '') for m in (data.get('data') or [])]
        target = cfg['model'] or ''
        ok = bool(target) and any(target in m or m in target for m in models)
    except Exception:
        ok = False
    _local_model_cache.update(t=now, ok=ok)
    return ok


def _is_weak_vlm(model):
    """判断视觉模型是否偏弱（只适合“看懂画面”，不擅长写剧情解说）。
    qwen2.5vl:latest / 3b / 4b / 7b 视为弱；14b / 32b 及以上的大视觉模型视为可用。"""
    m = (model or '').lower()
    if 'vl' not in m:
        return False
    return any(x in m for x in ('3b', '4b', '7b', 'latest'))


def _installed_local_models():
    """探测 Ollama / OpenAI 兼容端点已安装的模型名列表（供前端展示与推荐判断）。"""
    out, seen = [], set()
    vb = (vlm_cfg().get('base_url') or '').rstrip('/')
    lb = (local_llm_cfg().get('base_url') or '').rstrip('/')
    urls = []
    if vb and 'v1' not in vb:
        urls.append(vb + '/api/tags')
    if lb and lb.endswith('/v1'):
        urls.append(lb + '/models')
    import urllib.request, json as _json
    for url in urls:
        try:
            req = urllib.request.Request(url, method='GET')
            with urllib.request.urlopen(req, timeout=5) as r:
                data = _json.loads(r.read().decode('utf-8'))
            items = [m.get('name', '') for m in (data.get('models') or [])] \
                or [m.get('id', '') for m in (data.get('data') or [])]
            for it in items:
                if it and it not in seen:
                    seen.add(it); out.append(it)
        except Exception:
            pass
    return out


def _model_narr_guide():
    """解说模型引导：检测当前 VLM 是否偏弱、本地是否已部署可用的文字模型（写解说稿主力），
    返回给前端做「qwen2.5vl 局限提示 + 部署标准解说模型引导」。"""
    vc = vlm_cfg()
    lc = local_llm_cfg()
    guide = {
        'weak_vlm': _is_weak_vlm(vc.get('model')),
        'vlm_model': vc.get('model') or '',
        'local_ok': _local_model_available(),
        'local_model': lc.get('model') or '',
        'installed': _installed_local_models(),
        'recommend': '',
        'recommend_note': '',
    }
    if guide['local_ok']:
        guide['recommend'] = ''
        guide['recommend_note'] = '本地文字模型 %s 已就绪，解说稿走它生成，质量最佳。' % guide['local_model']
    else:
        # 有本地视觉模型但缺文字模型 → 引导补装文字模型（写解说稿的主力）
        guide['recommend'] = 'ollama pull qwen2.5:14b'
        guide['recommend_note'] = ('当前只配置了视觉模型（%s）：它只负责“看懂画面”，写剧情解说词很弱。'
                                   '请部署一个文字模型（推荐 qwen2.5:14b，写剧情解说更靠谱），'
                                   '并在「AI 配置 → ③ 本地模型」里把模型填成它。' % guide['vlm_model'])
    return guide


def ai_status():
    """返回各 AI 能力的就绪状态，供前端做生成前置引导（未配 key 时不应静默免费生成）。"""
    vok, vmsg = (vlm_ping() if vlm_enabled() else (False, 'VLM 未启用'))
    return {
        'chat': ai_enabled('chat'),
        'vision': ai_enabled('vision'),
        'tts': _tts_available(),
        'local': local_llm_enabled(),
        'whisper_model': whisper_model_name(),
        'whisper_ready': whisper_model_ready(),
        'vlm_enabled': vlm_enabled(),
        'vlm_ready': bool(vok) if vlm_enabled() else False,
        'vlm_msg': vmsg,
        'narr_guide': _model_narr_guide(),
        'any_ai': ai_enabled('chat') or ai_enabled('vision'),
        'configured': bool(load_ai_config()),
        'mirror': mirror_cfg(),
    }


def compute_mode(params, needs_chat=True):
    """判定本次任务实际会以哪种模式运行，用于结果打标与前置引导。
    返回 'free'（免费模板/离线）或 'ai'（真 AI）。
    - economy 显式 False 且 chat 已配置 → 'ai'
    - 其余（默认免费 / 缺 key 强制降级）→ 'free'"""
    econ = params.get('economy')
    if econ is None:
        econ = True  # 默认省流免费
    if needs_chat and not ai_enabled('chat'):
        econ = True  # 缺 LLM key 强制降级为免费
    return 'free' if econ else 'ai'


# ---------------------------------------------------------------------------
# 📺 B 站素材：搜索 + 下载 MP4（yt-dlp 搜索 + playurl 直连/yt-dlp 双引擎下载）。
# 说明：无需跳转第三方提取站；下载仅供个人在拥有权限的内容上使用（版权自负）。
# 风控说明：B 站对匿名请求有 WAF（412）——引擎失败时给出明确提示；
# 在 ai_config.json 配 `bili.cookie`（浏览器登录 Cookie）后更稳且可下更高清晰度。
# ---------------------------------------------------------------------------
import urllib.request as _urlreq, urllib.error as _urlerr, http.cookiejar as _cjar
BILI_DIR = os.path.join(OUTDIR, 'bili')
BILI_PULL = {'running': False, 'ok': None, 'pct': 0, 'msg': '', 'file': '', 'title': '', 'abort': False}
_BILI_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
_BILI_HDRS = {'User-Agent': _BILI_UA, 'Referer': 'https://www.bilibili.com/',
              'Accept': 'application/json, text/plain, */*', 'Accept-Language': 'zh-CN,zh;q=0.9'}


def bili_cfg():
    """B 站配置：可选 cookie（用户从浏览器复制的整段 Cookie 头，登录态更稳/清晰度更高）。"""
    cfg = load_ai_config().get('bili') or {}
    return {'cookie': (cfg.get('cookie') or '').strip()}


def _bili_cookie_header():
    """返回请求用的 Cookie 头：优先 ai_config 的 bili.cookie；否则自动访问 B 站首页收割 buvid3。"""
    ck = bili_cfg().get('cookie')
    if ck:
        return ck
    jar = _cjar.CookieJar()
    opener = _urlreq.build_opener(_urlreq.HTTPCookieProcessor(jar))
    for k, v in _BILI_HDRS.items():
        opener.addheaders.append((k, v))
    opener.open('https://www.bilibili.com/', timeout=15).read()
    return '; '.join('%s=%s' % (c.name, c.value) for c in jar)


def _bili_cookiefile():
    """把 Cookie 头写成 Netscape cookie 文件（yt-dlp 用）。"""
    os.makedirs(WORKDIR, exist_ok=True)
    cf = os.path.join(WORKDIR, 'bili_cookies.txt')
    with open(cf, 'w', encoding='utf-8') as f:
        f.write('# Netscape HTTP Cookie File\n')
        for pair in _bili_cookie_header().split(';'):
            if '=' in pair:
                k, _, v = pair.strip().partition('=')
                if k.strip():
                    f.write('.bilibili.com\tTRUE\t/\tTRUE\t2100000000\t%s\t%s\n' % (k.strip(), v))
    return cf


def _bili_get_json(url):
    req = _urlreq.Request(url, headers=dict(_BILI_HDRS, **{'Cookie': _bili_cookie_header()}))
    with _urlreq.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode('utf-8'))


def _bili_valid_bvid(bvid):
    import re as _re
    return bool(_re.match(r'^BV[0-9A-Za-z]{10}$', bvid or ''))


def bili_search(keyword, n=8):
    """yt-dlp bilisearch 搜索 B 站视频（已实测可用，需 buvid3 cookie——见 _bili_cookiefile）。
    返回 [{bvid,title,author,duration,pic}]；失败抛异常（含 412 风控提示）。"""
    import yt_dlp
    opts = {'quiet': True, 'no_warnings': True, 'skip_download': True, 'noplaylist': True,
            'playlistend': max(1, min(12, int(n))), 'socket_timeout': 20,
            'cookiefile': _bili_cookiefile(), 'http_headers': dict(_BILI_HDRS)}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info('bilisearch%d:%s' % (int(n), keyword), download=False)
    except Exception as e:
        msg = str(e)
        if '412' in msg:
            raise RuntimeError('B 站风控拦截（412）：请稍后再试；或在 ai_config.json 配 bili.cookie 后重试')
        raise
    out = []
    for e in (info or {}).get('entries') or []:
        if not e or not e.get('id'):
            continue
        out.append({'bvid': e.get('id'), 'title': (e.get('title') or '')[:90],
                    'author': (e.get('uploader') or '')[:40],
                    'duration': int(e.get('duration') or 0),
                    'pic': ((e.get('thumbnails') or [{}])[-1].get('url') or '')})
    return out


def _safe_filename(name):
    import re as _re
    return _re.sub(r'[\\/:*?"<>|]', '_', name or '')[:120] or 'video'


def _bili_download_direct(bvid):
    """引擎①：playurl html5 直连（未登录通常 480p；不依赖 yt-dlp 下载）。
    返回 (相对 OUTDIR 的文件路径, 标题)。"""
    v = _bili_get_json('https://api.bilibili.com/x/web-interface/view?bvid=' + bvid)
    if v.get('code') != 0:
        raise RuntimeError('读取视频信息失败：%s' % (v.get('message') or v.get('code')))
    cid = v['data']['cid']
    title = v['data'].get('title') or bvid
    BILI_PULL.update({'title': title[:60], 'pct': 5, 'msg': '获取播放地址…'})
    p = _bili_get_json('https://api.bilibili.com/x/player/playurl?bvid=%s&cid=%s&qn=64&platform=html5&high_quality=1'
                       % (bvid, cid))
    if p.get('code') != 0:
        raise RuntimeError('获取播放地址失败：%s' % (p.get('message') or p.get('code')))
    durl = (p['data'].get('durl') or [{}])[0]
    url = durl.get('url') or ''
    if not url:
        raise RuntimeError('未取得视频直链（可能需要登录/大会员）')
    size = durl.get('size') or 0
    req = _urlreq.Request(url, headers=dict(_BILI_HDRS, **{'Cookie': _bili_cookie_header()}))
    final = os.path.join(BILI_DIR, _safe_filename(bvid + '.mp4'))
    done = 0
    with _urlreq.urlopen(req, timeout=30) as resp, open(final, 'wb') as f:
        while True:
            b = resp.read(256 * 1024)
            if not b:
                break
            f.write(b)
            done += len(b)
            if done > UPLOAD_TOTAL_MAX:
                raise RuntimeError('文件超过 2GB 上限')
            if size:
                BILI_PULL['pct'] = min(95, int(done * 100 // size))
            BILI_PULL['msg'] = '下载中 %.1fMB' % (done / 1048576)
            if BILI_PULL.get('abort'):
                raise AbortError('用户取消了下载')
    return os.path.relpath(final, OUTDIR).replace('\\', '/'), title


def _bili_download_ytdlp(bvid):
    """引擎②（兜底）：yt-dlp 内置 B 站提取器，自行处理更多边角（登录 cookie 同样生效）。"""
    import yt_dlp

    def hook(d):
        if d.get('status') == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            if total:
                BILI_PULL['pct'] = min(95, int((d.get('downloaded_bytes') or 0) * 100 // total))
            BILI_PULL['msg'] = '下载中（yt-dlp）…'
        else:
            BILI_PULL['msg'] = '合并音视频…'
        if BILI_PULL.get('abort'):
            raise yt_dlp.utils.DownloadCancelled('用户取消了下载')

    opts = {'format': 'bv*[height<=720]+ba/b[height<=720]/b', 'merge_output_format': 'mp4',
            'outtmpl': os.path.join(BILI_DIR, '%(id)s.%(ext)s'),
            'ffmpeg_location': os.path.dirname(ffmpeg_exe()),
            'noplaylist': True, 'cookiefile': _bili_cookiefile(), 'socket_timeout': 20, 'retries': 2,
            'progress_hooks': [hook], 'quiet': True, 'no_warnings': True, 'http_headers': dict(_BILI_HDRS)}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info('https://www.bilibili.com/video/' + bvid, download=True)
    fp = ((info or {}).get('requested_downloads') or [{}])[0].get('filepath')
    if not fp or not os.path.isfile(fp):
        raise RuntimeError('yt-dlp 未产出文件')
    return os.path.relpath(fp, OUTDIR).replace('\\', '/'), (info.get('title') or bvid)


def _bili_download_thread(bvid):
    """下载编排：引擎①直连 → 失败自动切引擎②yt-dlp → 都失败给风控提示。"""
    BILI_PULL.update({'running': True, 'ok': None, 'pct': 1, 'msg': '读取视频信息…', 'file': '',
                      'title': '', 'abort': False})
    os.makedirs(BILI_DIR, exist_ok=True)
    try:
        try:
            rel, title = _bili_download_direct(bvid)
        except AbortError:
            raise
        except Exception as e1:
            if BILI_PULL.get('abort'):
                raise AbortError('用户取消了下载')
            last_err = str(e1)
            BILI_PULL.update({'pct': 3, 'msg': '直连失败（%s），改用 yt-dlp 引擎…' % last_err[:60]})
            rel, title = _bili_download_ytdlp(bvid)
        BILI_PULL.update({'running': False, 'ok': True, 'pct': 100, 'file': rel, 'title': title[:60],
                          'msg': '完成：%s' % title[:50]})
    except AbortError:
        BILI_PULL.update({'running': False, 'ok': False, 'msg': '已取消下载'})
    except Exception as e:
        msg = str(e)[:180]
        if '412' in msg or '风控' in msg:
            msg += ' —— 触发了 B 站风控：请等几分钟再试；或在 ai_config.json 配 bili.cookie（浏览器登录 Cookie）后重试，登录态更稳且清晰度更高。'
        BILI_PULL.update({'running': False, 'ok': False, 'msg': msg})


def _bili_start_download(bvid):
    """校验并启动下载线程；已有下载进行中时拒绝（简单串行，避免并发写同一进度槽）。"""
    if not _bili_valid_bvid(bvid):
        return {'ok': False, 'error': 'BV 号格式不正确'}
    if BILI_PULL.get('running'):
        return {'ok': False, 'error': '已有下载在进行中，请先等待或取消'}
    threading.Thread(target=_bili_download_thread, args=(bvid,), daemon=True).start()
    return {'ok': True}


# ---------------------------------------------------------------------------
# 🖼 封面生成：从成片智能选帧（对比度+细节打分）+ 大字标题合成封面图。
# 发抖音/B站都需要封面——出片后一键生成，可换帧/改标题/换版式。
# ---------------------------------------------------------------------------
COVER_CANDIDATES = 8


def _cover_score(im):
    """封面帧打分：对比度(灰度std) + 细节(边缘能量) - 过曝过暗惩罚，分越高越"有内容"。"""
    import numpy as np
    g = np.asarray(im.convert('L'), dtype=np.float32)
    contrast = float(g.std())
    edges = np.asarray(im.convert('L').filter(ImageFilter.FIND_EDGES), dtype=np.float32)
    detail = float(edges.mean())
    ext = float((g < 8).mean() + (g > 247).mean())
    return round(contrast * 0.6 + detail * 2.0 - ext * 40.0, 1)


def _cover_candidates(video_path, run_dir, n=COVER_CANDIDATES, max_side=640):
    """均匀抽 n 帧做候选（预览小图存 run_dir/cover_cand，带打分）。返回按时间序的候选列表。"""
    vdur = probe_audio_len(video_path) or 0.0
    if vdur <= 0:
        raise RuntimeError('无法读取视频时长')
    cand_dir = os.path.join(run_dir, 'cover_cand')
    os.makedirs(cand_dir, exist_ok=True)
    out = []
    for k in range(n):
        ts = round(vdur * (k + 0.5) / n, 2)
        fp = os.path.join(cand_dir, 'cand_%02d.jpg' % k)
        rc, _o, _e = ffmpeg_run(['-y', '-ss', '%.2f' % ts, '-i', video_path,
                                 '-frames:v', '1', '-vf', 'scale=min(iw\\,%d):-2' % max_side,
                                 '-q:v', '4', '-an', fp])
        if rc == 0 and os.path.isfile(fp):
            try:
                score = _cover_score(Image.open(fp))
            except Exception:
                score = 0.0
            out.append({'ts': ts, 'thumb': os.path.relpath(fp, OUTDIR).replace('\\', '/'),
                        'score': score})
    return out


def _cover_render(video_path, ts, title, sub, style, out_path, w_cap=1920):
    """抽全分辨率帧 + 叠加标题/副标题 → 封面图。style: 0 居中大字 / 1 底部条幅 / 2 左上角。"""
    tmp = out_path + '.frame.jpg'
    rc, _o, _e = ffmpeg_run(['-y', '-ss', '%.2f' % ts, '-i', video_path,
                             '-frames:v', '1', '-q:v', '2', '-an', tmp])
    if rc != 0 or not os.path.isfile(tmp):
        raise RuntimeError('抽帧失败（该时间点可能超出视频范围）')
    im = Image.open(tmp).convert('RGB')
    try:
        os.remove(tmp)
    except OSError:
        pass
    if im.width > w_cap:
        im = im.resize((w_cap, int(im.height * w_cap / im.width)), Image.LANCZOS)
    draw = ImageDraw.Draw(im, 'RGBA')
    W, H = im.size
    fs = max(28, W // 16)

    def font(size):
        try:
            return ImageFont.truetype(FONT_PATH, size)
        except Exception:
            return ImageFont.load_default()

    def wrap(text, fnt, maxw, maxlines=3):
        lines, cur = [], ''
        for ch in text:
            if draw.textlength(cur + ch, font=fnt) <= maxw or not cur:
                cur += ch
            else:
                lines.append(cur)
                cur = ch
        if cur:
            lines.append(cur)
        return lines[:maxlines]

    t = _clean_caption(title)
    lines = wrap(t, font(fs), int(W * 0.86)) if t else []
    lh = int(fs * 1.3)
    if style == 1 and lines:   # 底部条幅：半透明黑条 + 白字
        bh = int(H * 0.08) * len(lines) + int(H * 0.04)
        overlay = Image.new('RGBA', im.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rectangle([0, H - bh, W, H], fill=(0, 0, 0, 150))
        im = Image.alpha_composite(im.convert('RGBA'), overlay).convert('RGB')
        draw = ImageDraw.Draw(im, 'RGBA')
        y = H - bh + int(H * 0.02)
        for ln in lines:
            draw.text((int(W * 0.04), y), ln, font=font(fs), fill=(255, 255, 255, 255))
            y += lh
    else:                       # 居中大字 / 左上角：白字黑描边
        y = (H - len(lines) * lh) // 2 if style == 0 else int(H * 0.06)
        for ln in lines:
            x = int((W - draw.textlength(ln, font=font(fs))) // 2) if style == 0 else int(W * 0.05)
            draw.text((x, y), ln, font=font(fs), fill=(255, 255, 255, 255),
                      stroke_width=max(2, fs // 14), stroke_fill=(0, 0, 0, 220))
            y += lh
    if sub:
        f_sub = font(max(20, fs // 2))
        sw = draw.textlength(sub, font=f_sub)
        sx = int((W - sw) // 2) if style != 2 else int(W * 0.05)
        sy = min(int(H * 0.9), y + int(fs * 0.2))
        draw.text((max(4, sx), sy), sub, font=f_sub, fill=(255, 255, 255, 230),
                  stroke_width=2, stroke_fill=(0, 0, 0, 200))
    im.save(out_path, quality=90)
    return out_path


# ---------------------------------------------------------------------------
# 🗂 本地素材库：独立文件夹 material_library/，素材持久保存、刷新/重启不丢。
# 上传（小文件 base64 / 大文件复用分片协议）、从成片目录存入（如 B 站下载的视频）、
# 删除；任务请求里 video/item 传 {name, mlib} 即可直接使用库内素材（copy 进 run_dir）。
# ---------------------------------------------------------------------------
MATERIAL_DIR = os.path.join(HERE, 'material_library')


def _material_path(name):
    """素材库内文件的安全路径（防穿越 + 必须存在）；非法返回 None。"""
    return _safe_join(MATERIAL_DIR, name)


def material_list():
    """列出素材库中的视频/图片素材（按名称排序）。"""
    if not os.path.isdir(MATERIAL_DIR):
        os.makedirs(MATERIAL_DIR, exist_ok=True)
    out = []
    for fn in sorted(os.listdir(MATERIAL_DIR)):
        p = os.path.join(MATERIAL_DIR, fn)
        if not os.path.isfile(p):
            continue
        ext = os.path.splitext(fn)[1].lower()
        if ext in ('.mp4', '.mov', '.webm', '.avi', '.mkv', '.m4v'):
            kind = 'video'
        elif ext in ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp'):
            kind = 'image'
        else:
            continue
        try:
            out.append({'name': fn, 'kind': kind, 'size': os.path.getsize(p),
                        'mtime': int(os.path.getmtime(p))})
        except OSError:
            pass
    return out


def material_save_file(src_path):
    """把一个已存在的文件复制进素材库（重名自动加 (1)(2) 序号）。返回最终文件名。"""
    os.makedirs(MATERIAL_DIR, exist_ok=True)
    base = os.path.basename(src_path)
    stem, ext = os.path.splitext(base)
    candidate, i = base, 1
    while os.path.exists(os.path.join(MATERIAL_DIR, candidate)):
        candidate = '%s(%d)%s' % (stem, i, ext)
        i += 1
    shutil.copy2(src_path, os.path.join(MATERIAL_DIR, candidate))
    return candidate


def material_save_bytes(name, data):
    """上传的字节存入素材库（保留原始文件名，重名自动加序号）。返回 (最终文件名|None, error)。"""
    base = os.path.basename(_safe_filename(name or ''))
    if not base or not data:
        return None, '文件名或内容为空'
    os.makedirs(MATERIAL_DIR, exist_ok=True)
    stem, ext = os.path.splitext(base)
    candidate, i = base, 1
    while os.path.exists(os.path.join(MATERIAL_DIR, candidate)):
        candidate = '%s(%d)%s' % (stem, i, ext)
        i += 1
    with open(os.path.join(MATERIAL_DIR, candidate), 'wb') as f:
        f.write(data)
    return candidate, ''


def material_delete(name):
    fp = _material_path(name)
    if not fp:
        return False, '素材不存在'
    try:
        os.remove(fp)
    except OSError as e:
        return False, str(e)[:80]
    return True, ''


_HIST_LOCK = threading.RLock()   # history.json 读写锁（可重入：add_history 持锁时会再调 load_history）；防丢条目、防读到半写文件


def load_history(limit=50):
    try:
        with _HIST_LOCK:
            with open(HISTORY_PATH, 'r', encoding='utf-8') as f:
                items = json.load(f)
        if not isinstance(items, list):
            items = []
    except Exception:
        items = []
    return items[:limit]


def _record_history(req, prog, kind=''):
    """统一写生成历史（⑨记录）。此前只有一键合成写历史，强卡点/解说/联网解说/
    按方案渲染的成片都不会出现在记录里。失败静默，不影响出片主流程。
    容量淘汰遵循 add_history 的规则（超 100 条丢弃最旧并清理其成片文件）。"""
    try:
        if not prog.get('file'):
            return
        f = os.path.join(OUTDIR, prog['file'])
        add_history({
            'time': time.strftime('%Y-%m-%d %H:%M:%S'),
            'file': prog['file'],
            'duration': round(float(probe_audio_len(f) or 0.0), 2),
            'music': (req.get('music') or {}).get('name') if isinstance(req.get('music'), dict) else None,
            'voice': False, 'captions': [],
            'kind': kind,
        })
    except Exception:
        pass


def _safe_join(base, name):
    """拼接文件路径并校验结果仍落在 base 目录内。防 /media/../ 等目录穿越
    读到 ai_config.json 等敏感文件（默认只绑 127.0.0.1 风险低，但 Docker 部署
    HOST=0.0.0.0 时必须兜住）。返回可读文件的绝对路径；穿越/不存在返回 None。"""
    try:
        if not name or name.startswith(('/', '\\')) or ':' in name:
            return None
        base_abs = os.path.abspath(base)
        full = os.path.abspath(os.path.join(base_abs, name))
        if os.path.commonpath([base_abs, full]) != base_abs:
            return None
        return full if os.path.isfile(full) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 📤 大文件分片上传：>64MB 的视频走「分片 base64」而非一次性 base64 JSON——
# 旧路径会把整个文件膨胀 1.37 倍塞进一个 JSON（334MB 视频 ≈ 446MB 请求体 + GB 级内存峰值）。
# 三接口：init(开会话) → chunk(乱序写分片) → done(按序合并)。小文件仍走 base64 旧路径。
# ---------------------------------------------------------------------------
UPLOAD_DIR = os.path.join(WORKDIR, 'uploads')
UPLOAD_CHUNK_MAX = 8 * 1024 * 1024          # 单分片解码后上限
UPLOAD_TOTAL_MAX = 2 * 1024 * 1024 * 1024   # 单文件总量上限 2GB


def _upload_dir_of(upload_id):
    """校验 upload_id（仅字母数字与连字符，≤64 位）并返回其暂存目录；非法返回 None。"""
    import re as _re
    if not upload_id or len(upload_id) > 64:
        return None
    if not _re.match(r'^up-[0-9A-Za-z-]+$', upload_id):
        return None
    return os.path.join(UPLOAD_DIR, upload_id)


def _upload_prune():
    """清理超 24 小时未动的上传会话（用户中途放弃的残片）；
    活跃会话数超过 100 时再清最旧的——防 HOST=0.0.0.0 部署下被会话数量滥用撑爆磁盘。"""
    try:
        if not os.path.isdir(UPLOAD_DIR):
            return
        now = time.time()
        entries = []
        for fn in os.listdir(UPLOAD_DIR):
            p = os.path.join(UPLOAD_DIR, fn)
            try:
                entries.append((os.path.getmtime(p), p))
            except OSError:
                pass
        entries.sort()
        keep = []
        for mt, p in entries:
            if now - mt > 86400:
                shutil.rmtree(p, ignore_errors=True)
            else:
                keep.append(p)
        excess = len(keep) - 100
        for p in (keep[:excess] if excess > 0 else []):
            shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass


def _upload_chunk_write(upload_id, idx, data_bytes):
    """写一个分片（每片一个 part 文件，乱序到达也安全）。返回 (ok, error)。"""
    d = _upload_dir_of(upload_id)
    if d is None:
        return False, '非法 upload_id'
    if not os.path.isdir(d):
        return False, '上传会话不存在或已过期，请重新开始上传'
    try:
        idx = int(idx)
    except Exception:
        return False, '非法分片序号'
    if idx < 0 or idx > 4096:
        return False, '非法分片序号'
    if len(data_bytes) > UPLOAD_CHUNK_MAX:
        return False, '分片过大'
    try:
        used = 0
        for fn in os.listdir(d):
            try:
                used += os.path.getsize(os.path.join(d, fn))
            except OSError:
                pass
        if used + len(data_bytes) > UPLOAD_TOTAL_MAX:
            return False, '文件超过 2GB 上限'
        with open(os.path.join(d, 'part_%04d' % idx), 'wb') as f:
            f.write(data_bytes)
    except OSError as e:
        return False, '分片写入失败：%s' % str(e)[:80]
    return True, ''


def _upload_have_parts(upload_id):
    """断点续传：返回会话中已到齐的分片序号（升序）。会话非法/不存在返回 None。"""
    d = _upload_dir_of(upload_id)
    if d is None or not os.path.isdir(d):
        return None
    have = []
    for fn in os.listdir(d):
        if fn.startswith('part_'):
            try:
                have.append(int(fn.split('_')[1]))
            except (IndexError, ValueError):
                pass
    return sorted(have)


def _upload_finalize(upload_id, name, chunks):
    """按序合并分片为成品文件 final__<name>。返回 (final_path|None, error)。"""
    d = _upload_dir_of(upload_id)
    if d is None:
        return None, '非法 upload_id'
    try:
        chunks = int(chunks)
    except Exception:
        return None, '非法分片数'
    if chunks < 1 or chunks > 4096:
        return None, '非法分片数'
    parts = []
    for i in range(chunks):
        p = os.path.join(d, 'part_%04d' % i)
        if not os.path.isfile(p):
            return None, '缺少分片 %d/%d，请重新上传该分片' % (i + 1, chunks)
        parts.append(p)
    base = os.path.basename(name or 'video.mp4') or 'video.mp4'
    final = os.path.join(d, 'final__' + base)
    total = 0
    try:
        with open(final, 'wb') as out:
            for p in parts:
                with open(p, 'rb') as f:
                    while True:
                        b = f.read(4 * 1024 * 1024)
                        if not b:
                            break
                        out.write(b)
                        total += len(b)
                        if total > UPLOAD_TOTAL_MAX:
                            raise RuntimeError('文件超过 2GB 上限')
    except Exception as e:
        try:
            os.remove(final)
        except OSError:
            pass
        return None, str(e)[:120]
    for p in parts:
        try:
            os.remove(p)
        except OSError:
            pass
    return final, ''


def _upload_final_path(upload_id, name):
    """分片合并后的成品文件路径；校验归属与存在，非法/不存在返回 None。"""
    d = _upload_dir_of(upload_id)
    if d is None:
        return None
    base = os.path.basename(name or 'video.mp4') or 'video.mp4'
    fp = os.path.join(d, 'final__' + base)
    return fp if os.path.isfile(fp) else None


def _resolve_upload_video(vobj, run_dir, prefix='src'):
    """视频对象统一落盘到 run_dir，两种形态并存：
    {name, data(base64)} 旧路径（小文件）；{name, upload_id} 分片上传（大文件——
    直接移动合并成品，省一次整文件拷贝）。返回本地文件路径；无效返回 None。"""
    if not vobj:
        return None
    ext = os.path.splitext(vobj.get('name') or 'x.mp4')[1] or '.mp4'
    if vobj.get('mlib'):
        src = _material_path(vobj.get('mlib'))
        if not src:
            return None
        fp = os.path.join(run_dir, prefix + ext)
        shutil.copy2(src, fp)   # 素材库持久保留，任务用拷贝
        return fp
    if vobj.get('data'):
        fp = os.path.join(run_dir, prefix + ext)
        open(fp, 'wb').write(base64.b64decode(vobj.get('data', '')))
        return fp
    src = _upload_final_path(vobj.get('upload_id'), vobj.get('name'))
    if src:
        fp = os.path.join(run_dir, prefix + ext)
        shutil.move(src, fp)
        try:
            d = _upload_dir_of(vobj.get('upload_id'))
            if d and os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
        except OSError:
            pass
        return fp
    return None


def add_history(entry):
    try:
        with _HIST_LOCK:
            items = load_history(500)
            items.insert(0, entry)
            # 容量上限：超过 MAX_HISTORY_KEEP 条时，丢弃最旧记录并清理其磁盘文件，防止 webui_output 无限涨盘
            MAX_HISTORY_KEEP = 100
            if len(items) > MAX_HISTORY_KEEP:
                dropped = items[MAX_HISTORY_KEEP:]
                items = items[:MAX_HISTORY_KEEP]
                for old in dropped:
                    _remove_history_file(old.get('file'))
            with open(HISTORY_PATH, 'w', encoding='utf-8') as f:
                json.dump(items, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _remove_history_file(rel):
    """删除一条历史对应的成片文件及其所在 run_dir（若存在）。失败静默。
    history 中的 file 路径相对 OUTDIR（如 'run-X/final.mp4'），故以 OUTDIR 为基准解析。"""
    if not rel:
        return
    try:
        fp = os.path.join(OUTDIR, rel) if not os.path.isabs(rel) else rel
        if os.path.isfile(fp):
            os.remove(fp)
            # 一并清理同目录（独立 run_dir）下的中间产物
            parent = os.path.dirname(fp)
            if parent and os.path.isdir(parent) and os.path.abspath(parent).startswith(os.path.abspath(OUTDIR)):
                shutil.rmtree(parent, ignore_errors=True)
    except Exception:
        pass


def delete_history(file):
    """按 file 删除一条历史记录及其磁盘文件。返回是否删除成功。"""
    try:
        items = load_history(500)
        new = [it for it in items if it.get('file') != file]
        if len(new) == len(items):
            return False
        with open(HISTORY_PATH, 'w', encoding='utf-8') as f:
            json.dump(new, f, ensure_ascii=False, indent=2)
        _remove_history_file(file)
        return True
    except Exception:
        return False


def clear_history():
    """清空全部历史记录并删除 webui_output 下所有成片（保留目录本身）。"""
    try:
        if os.path.isdir(OUTDIR):
            for name in os.listdir(OUTDIR):
                p = os.path.join(OUTDIR, name)
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                elif os.path.isfile(p):
                    os.remove(p)
        with open(HISTORY_PATH, 'w', encoding='utf-8') as f:
            json.dump([], f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


# Offline caption fallback: turn a plain filename into a short spring-y caption if the
# user has not configured any AI. Keeps the pipeline functional without a key.
def offline_caption(name, idx, n_total):
    base = os.path.splitext(os.path.basename(name))[0]
    import re as _re
    m = _re.search(r'up_(\d+)_\d+_img', base)
    if m:
        i = int(m.group(1))
        phrases = ['春意初醒', '花开时节', '绿野青青', '溪水潺潺', '山色葱茏', '暖阳正好']
        return f'{phrases[(i - 1) % len(phrases)]} · 第{i}帧'
    if 'spring' in base or 'img' in base:
        return f'第 {idx} 帧 · 春日风景'
    return f'第 {idx} 帧 · {base}'

W, H = 1920, 1080

# ---------------------------------------------------------------------------
# 依赖安装（首次）
# ---------------------------------------------------------------------------
def ensure_deps():
    def has(mod):
        try:
            __import__(mod); return True
        except Exception:
            return False
    missing = []
    if not has('PIL'): missing.append('Pillow')
    if not has('numpy'): missing.append('numpy')
    if not has('imageio_ffmpeg'): missing.append('imageio-ffmpeg')
    if not has('yt_dlp'): missing.append('yt-dlp')
    if missing:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                               '--no-input'] + missing)

def ffmpeg_exe():
    from imageio_ffmpeg import get_ffmpeg_exe
    return get_ffmpeg_exe()

# ---------------------------------------------------------------------------
# 图片生成（复用 spring_video 的绘制逻辑）
# ---------------------------------------------------------------------------
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

def hx(h):
    h = h.lstrip('#'); return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

def gradient(w, h, top, bot):
    top = hx(top); bot = hx(bot)
    img = Image.new('RGB', (w, h)); px = img.load()
    for y in range(h):
        t = y / max(1, h - 1)
        r = int(top[0] + (bot[0]-top[0])*t); g = int(top[1] + (bot[1]-top[1])*t); b = int(top[2] + (bot[2]-top[2])*t)
        for x in range(0, w, 3):
            px[x, y] = (r, g, b)
            if x+1 < w: px[x+1, y] = (r, g, b)
            if x+2 < w: px[x+2, y] = (r, g, b)
    return img.resize((w, h)).convert('RGB')

def blend(base, layer):
    if base.mode != 'RGBA': base = base.convert('RGBA')
    return Image.alpha_composite(base, layer).convert('RGB')

def sun_layer(cx, cy, r, color, blur=20, st=20):
    s = Image.new('RGBA', (W, H), (0, 0, 0, 0)); d = ImageDraw.Draw(s)
    for i in range(8, 0, -1):
        d.ellipse([cx-r*i, cy-r*i, cx+r*i, cy+r*i], fill=color + (max(0, int(st*i)),))
    s = s.filter(ImageFilter.GaussianBlur(blur))
    ImageDraw.Draw(s).ellipse([cx-r, cy-r, cx+r, cy+r], fill=color + (255,))
    return s

def clouds(d, x, y, s, shade):
    r = int(60*s)
    for dx, dy, rr in [(0,0,r),(int(r*.8),-int(r*.3),int(r*.75)),(-int(r*.9),int(r*.15),int(r*.7)),(int(r*.9),int(r*.1),int(r*.6))]:
        d.ellipse([x+dx-rr, y+dy-int(rr*.6), x+dx+rr, y+dy+int(rr*.7)], fill=shade)

def tree(d, x, y, gh, tc, l1, l2):
    tw = max(4, int(gh*.05)); d.rectangle([x-tw//2, y-gh, x+tw//2, y], fill=tc)
    for i in range(5):
        ox = (i-2)*gh*.28; oy = -gh*(.35+.12*((i % 3)-1)); rr = gh*(.28+.1*(i % 2))
        d.ellipse([x+ox-rr, y+oy-rr, x+ox+rr, y+oy+rr], fill=l1)
        d.ellipse([x+ox-int(rr*.5), y+oy-int(rr*.4), x+ox+int(rr*.5), y+oy+int(rr*.6)], fill=l2)

def scene1():
    img = gradient(W, H, '#cfe8ff', '#eef7ff')
    img = blend(img, sun_layer(int(W*.78), int(H*.18), 60, (255, 244, 200)))
    c = ImageDraw.Draw(img, 'RGBA')
    clouds(c, int(W*.2), int(H*.12), 1.1, (255,255,255,220))
    clouds(c, int(W*.6), int(H*.08), .8, (255,255,255,200))
    clouds(c, int(W*.92), int(H*.2), .6, (255,255,255,190))
    rnd = random.Random(11); b1 = int(H*.42); p = []
    for i in range(25): p.append((i/24*W, b1 - int(rnd.random()*180)))
    p += [(W, H), (0, H)]; c.polygon(p, fill=hx('#9fc5e0'))
    rnd2 = random.Random(12); b2 = int(H*.5); p = []
    for i in range(19): p.append((i/18*W, b2 - int(rnd2.random()*220)))
    p += [(W, H), (0, H)]; c.polygon(p, fill=hx('#8fb3d6'))
    c.polygon([(0, int(H*.55)), (W, int(H*.5)), (W, H), (0, H)], fill=hx('#9ed66a'))
    c.polygon([(0, int(H*.62)), (W, int(H*.56)), (W, H), (0, H)], fill=hx('#7fbe57'))
    sd = random.Random(7)
    for _ in range(400):
        x = sd.randint(0, W); y = sd.randint(int(H*.62), H)
        c.ellipse([x-2, y-6, x+2, y], fill=hx(sd.choice(['#5da73f','#79c04f','#8ed060'])))
    bloom = Image.new('RGBA', (W, H), (0, 0, 0, 0)); bd = ImageDraw.Draw(bloom)
    for bx, gh, fl in [(int(W*.12), 360, 1), (int(W*.88), 420, -1)]:
        tw = int(gh*.03); bd.line([(bx, 0), (bx, int(gh*.4))], fill=(120,70,50,255), width=tw)
        for i in range(4):
            hy = int(gh*(.2+.16*i)); bx2 = bx + fl*int(gh*(.2+.1*i))
            bd.line([(bx, int(gh*.4)), (bx2, hy)], fill=(130,80,55,255), width=int(tw*.6))
            rd = random.Random(20+i)
            for _ in range(14):
                ox = bx2 + rd.randint(-int(gh*.28), int(gh*.28)); oy = hy + rd.randint(-int(gh*.15), int(gh*.06)); rr = rd.randint(6, 14)
                bd.ellipse([ox-rr, oy-rr, ox+rr, oy+rr], fill=(247,180,197,200))
                bd.ellipse([ox-int(rr*.5), oy-int(rr*.5), ox+int(rr*.5), oy+int(rr*.5)], fill=(255,230,240,230))
    img = blend(img, bloom)
    pet = Image.new('RGBA', (W, H), (0, 0, 0, 0)); pd = ImageDraw.Draw(pet); rd = random.Random(55)
    for _ in range(60):
        x = rd.randint(0, W); y = rd.randint(int(H*.3), H); rr = rd.randint(3, 7)
        pd.ellipse([x-rr, y-rr//2, x+rr, y+rr//2], fill=(255,200,214,200))
    return blend(img, pet)

def scene2():
    img = gradient(W, H, '#8fd0f5', '#e6f6ff')
    img = blend(img, sun_layer(int(W*.25), int(H*.18), 55, (255, 246, 200), blur=18))
    c = ImageDraw.Draw(img, 'RGBA')
    for (a, b, s) in [(.1, .14, 1.2), (.45, .1, .9), (.8, .22, .8), (.95, .12, .5)]:
        clouds(c, int(W*a), int(H*b), s, (255,255,255,225))
    c.ellipse([-W*.4, int(H*.28), W*.6, int(H*.8)], fill=hx('#a6d87a'))
    c.ellipse([W*.3, int(H*.26), W*1.3, int(H*.82)], fill=hx('#8fd06b'))
    c.polygon([(0, int(H*.5)), (W, int(H*.46)), (W, H), (0, H)], fill=hx('#f2d94e'))
    c.polygon([(0, int(H*.6)), (W, int(H*.56)), (W, H), (0, H)], fill=hx('#e6c93c'))
    rd = random.Random(3)
    for by, cnt in [(int(H*.66), 3), (int(H*.78), 2), (int(H*.9), 2)]:
        for i in range(cnt):
            y = by + i*int(H*.05) + rd.randint(-8, 8); c.line([(0, y), (W, y)], fill=hx('#cfad2e'), width=4)
    rd = random.Random(4)
    for _ in range(9):
        x = rd.randint(200, W-200); y = int(H*.47)+rd.randint(-14, 10); w = rd.randint(50, 90); gh = rd.randint(30, 50)
        c.polygon([(x, y), (x+w, y), (x+w//2, y-gh)], fill=hx('#b0442f'))
        c.rectangle([x+w//4, y, x+w*3//4, y+gh], fill=hx('#efe0c8'))
        c.rectangle([x+w//2-3, y+4, x+w//2+3, y+14], fill=hx('#6b4a2a'))
    rd = random.Random(5)
    for _ in range(6):
        x = rd.randint(100, W-100); y = int(H*.47)+rd.randint(0, 20); gh = rd.randint(140, 240)
        tree(c, x, y, gh, hx('#5a4632'), hx('#4e7a38'), hx('#6f9e46'))
    rd = random.Random(8)
    for _ in range(500):
        x = rd.randint(0, W); y = rd.randint(int(H*.6), H)
        c.ellipse([x-2, y-2, x+2, y+2], fill=hx(rd.choice(['#fff3a0','#f7d94b','#ffd93d'])))
    return img

def scene3():
    img = gradient(W, H, '#bde5ff', '#f2faff')
    img = blend(img, sun_layer(int(W*.7), int(H*.15), 50, (255, 242, 190), blur=16))
    c = ImageDraw.Draw(img, 'RGBA')
    clouds(c, int(W*.15), int(H*.12), 1.0, (255,255,255,220)); clouds(c, int(W*.85), int(H*.2), .7, (255,255,255,200))
    c.ellipse([-W*.5, int(H*.3), W*.7, int(H*.85)], fill=hx('#b7e2a0'))
    c.ellipse([W*.4, int(H*.3), W*1.4, int(H*.85)], fill=hx('#a7d98f'))
    c.polygon([(int(W*.34), 0), (int(W*.58), 0), (int(W*.5), H), (int(W*.3), H)], fill=hx('#bfe8f5'))
    c.polygon([(0, int(H*.42)), (int(W*.34), int(H*.4)), (int(W*.3), H), (0, H)], fill=hx('#7ec850'))
    c.polygon([(int(W*.58), int(H*.4)), (W, int(H*.44)), (W, H), (int(W*.5), H)], fill=hx('#7ec850'))
    for _ in range(40):
        x = random.Random(200+_).uniform(int(W*.31), int(W*.55)); y = random.Random(60+_).uniform(int(H*.45), H-20)
        ln = random.Random(80+_).uniform(30, 120); c.line([(x, y), (x+ln, y)], fill=hx('#e2f6ff'), width=2)
    def pt(c, _x, _y, gh):
        tw = int(gh*.05); c.line([(_x, _y), (_x, int(_y-gh*.5))], fill=(120,66,44,255), width=tw)
        rd = random.Random(int(_x))
        for i in range(6):
            hy = _y-int(gh*(.3+.12*i)); bx = _x+int(gh*(.2+.08*i))*(-1 if i % 2 else 1)
            c.line([(_x, int(_y-gh*.5)), (bx, hy)], fill=(130,72,46,255), width=int(tw*.6))
            for j in range(9):
                ox = bx+rd.randint(-int(gh*.26), int(gh*.26)); oy = hy+rd.randint(-int(gh*.14), int(gh*.05)); rr = rd.randint(6, 12)
                c.ellipse([ox-rr, oy-rr, ox+rr, oy+rr], fill=(245,168,185,210))
                c.ellipse([ox-int(rr*.5), oy-int(rr*.5), ox+int(rr*.5), oy+int(rr*.5)], fill=(255,222,232,235))
    pt(c, int(W*.16), int(H*.66), 250); pt(c, int(W*.84), int(H*.7), 280)
    for wx in [int(W*.62), int(W*.75)]:
        c.line([(wx, int(H*.4)), (wx, int(H*.55))], fill=(130,80,50,255), width=6); rd = random.Random(wx)
        for k in range(18):
            ox = wx+rd.randint(-20, 20); oy = int(H*.55)+k*4
            c.line([(ox, int(H*.55)), (ox+rd.randint(-8, 8), oy)], fill=(140,190,120,200), width=3)
    for _ in range(40):
        x = random.Random(300+_).uniform(int(W*.32), int(W*.54)); y = random.Random(90+_).uniform(int(H*.45), H-20)
        c.ellipse([x-4, y-2, x+4, y+2], fill=(255,190,205,210))
    return img

def scene4():
    img = gradient(W, H, '#aee0f7', '#f0faff')
    img = blend(img, sun_layer(int(W*.5), int(H*.12), 70, (255, 246, 190), blur=30, st=26))
    beam = Image.new('RGBA', (W, H), (0, 0, 0, 0)); bd = ImageDraw.Draw(beam); rd = random.Random(40)
    for i in range(8):
        a0 = rd.uniform(-.5, .5); a1 = rd.uniform(.6, 1.4)
        bd.polygon([(int(W*.5), int(H*.12)), (int(W*.5)+math.sin(a0)*1500, H+200), (int(W*.5)+math.sin(a1)*1500, H+200)], fill=(255,255,210, int(14+i*3)))
    beam = beam.filter(ImageFilter.GaussianBlur(6)); img = blend(img, beam)
    c = ImageDraw.Draw(img, 'RGBA')
    c.rectangle([0, int(H*.55), W, H], fill=hx('#9ed76e'))
    c.polygon([(0, int(H*.5)), (W, int(H*.46)), (W, int(H*.6)), (0, int(H*.62))], fill=hx('#8ccb60'))
    rd = random.Random(33)
    for i in range(9):
        x = int(W*(.05+.11*i)); tw = rd.randint(18, 30)
        c.rectangle([x-tw//2, int(H*.35), x+tw//2, int(H*.62)], fill=(120,84,50,255))
        c.line([(x, int(H*.4)), (x-tw*2, int(H*.34))], fill=(120,84,50,255), width=int(tw*.4))
        c.line([(x, int(H*.42)), (x+tw*2, int(H*.36))], fill=(120,84,50,255), width=int(tw*.4))
    tn = Image.new('RGBA', (W, H), (0, 0, 0, 0)); td = ImageDraw.Draw(tn); rd = random.Random(31)
    for i in range(9):
        x = int(W*(.05+.11*i)); hy = int(H*(.34+rd.uniform(0, .06)))
        for j in range(7):
            ox = x+rd.randint(-70, 70); oy = hy+rd.randint(-50, 30); rr = rd.randint(40, 80)
            td.ellipse([ox-rr, oy-rr//2, ox+rr, oy+rr//2], fill=(120,180,110,200))
    tn = tn.filter(ImageFilter.GaussianBlur(2)); img = blend(img, tn)
    c2 = ImageDraw.Draw(img, 'RGBA'); rd = random.Random(77)
    for _ in range(400):
        x = rd.randint(0, W); y = rd.randint(int(H*.55), H)
        c2.ellipse([x-2, y-8, x+2, y], fill=hx(rd.choice(['#6cb94a','#82cc5c','#5da83f'])))
    rd = random.Random(120)
    for _ in range(90):
        x = rd.randint(0, W); y = rd.randint(int(H*.6), H); rr = rd.randint(6, 14)
        colc = rd.choice(['#ffffff','#ffe77a','#ff9bd2','#ffffff','#ffd9a0'])
        for a in range(8):
            ax = x+int(rr*.8*math.cos(a*math.pi/4)); ay = y+int(rr*.5*math.sin(a*math.pi/4))
            c2.ellipse([ax-3, ay-3, ax+3, ay+3], fill=hx(colc))
        c2.ellipse([x-3, y-3, x+3, y+3], fill=hx('#f5c241'))
    return img

SCENES = [scene1, scene2, scene3, scene4]
SCENE_TITLES = ['花开似锦 · 樱花漫山', '金色田野 · 油菜花开', '桃花流水 · 春水盈盈', '春林新绿 · 阳光正好']

def stamp_title(img, text):
    try:
        font = ImageFont.truetype(FONT_PATH, 54); font_small = ImageFont.truetype(FONT_PATH, 30)
    except Exception:
        font = ImageFont.load_default(); font_small = font
    d = ImageDraw.Draw(img, 'RGBA'); sub = '· 春日 ·'
    d.text((34, H-120), text, font=font, fill=(255,255,255,120)); d.text((36, H-116), sub, font=font_small, fill=(255,255,255,120))
    d.text((30, H-120), text, font=font, fill=(40,70,40,255)); d.text((32, H-118), sub, font=font_small, fill=(60,90,50,255))
    return img

def ensure_default_images():
    """Write the 4 built-in spring images if not present."""
    out = []
    for i, fn in enumerate(SCENES):
        path = os.path.join(HERE, f'img{i+1}.png')
        if not os.path.exists(path):
            img = stamp_title(fn(), SCENE_TITLES[i]).convert('RGB').resize((W, H), Image.LANCZOS)
            img.save(path, 'PNG')
        out.append(path)
    return out

# ---------------------------------------------------------------------------
# 合成引擎
# ---------------------------------------------------------------------------
def _parse_time_str(t):
    """'HH:MM:SS.cc' → 秒（float）；格式不符返回 None。"""
    try:
        hh, mm, ss = t.split(':')
        return int(hh) * 3600 + int(mm) * 60 + float(ss)
    except Exception:
        return None


def ffmpeg_run(args, input_data=None, on_progress=None):
    """运行 ffmpeg。若当前任务线程绑定了 runid（见 _spawn），则把进程注册到
    RUN_PROCS，并每 0.3s 检查 PROGRESS[runid]['abort']；用户取消时立即终止进程
    并抛 AbortError，使整条流水线真正中断。
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

    def _read_stderr():
        nonlocal tail
        try:
            for chunk in iter(lambda: proc.stderr.read(65536), b''):
                err_chunks.append(chunk)
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
                proc.stdin.write(input_data)
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
    try:
        while True:
            try:
                rc = proc.wait(timeout=0.3)
                break
            except subprocess.TimeoutExpired:
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
                    raise AbortError('用户取消了任务')
        try:
            out = proc.stdout.read()
        except Exception:
            pass
        t_err.join(timeout=2)
        t_in.join(timeout=2)
        err = b''.join(err_chunks)
    finally:
        if runid:
            with _PROC_LOCK:
                RUN_PROCS.pop(runid, None)
    return rc, out, err

def probe_duration(path):
    rc, out, err = ffmpeg_run(['-i', path, '-f', 'null', '-'])
    import re
    m = re.search(r'Duration:\s*(\d+):(\d+):([\d.]+)', err.decode('utf-8', 'ignore'))
    if not m:
        return None
    h, mm, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return h * 3600 + mm * 60 + s

def make_image_clip(img_path, dur, motion, out_path, w, h, fps):
    """Render one Ken Burns image clip (dur seconds) as an mp4 segment."""
    im = np.asarray(Image.open(img_path).convert('RGB'), dtype=np.float32)
    N = int(round(dur * fps))
    base_w = int(w * 1.2); base_h = int(h * 1.2)
    iw, ih = im.shape[1], im.shape[0]
    # downscale once to a small working canvas (output x ~1.2) for speed
    scale = max(base_w / iw, base_h / ih)
    nw = int(round(iw * scale)); nh = int(round(ih * scale))
    pil = Image.fromarray(np.clip(im, 0, 255).astype(np.uint8)).resize((nw, nh), Image.LANCZOS)
    ox = (nw - base_w) // 2; oy = (nh - base_h) // 2
    canvas_pil = pil.crop((ox, oy, ox + base_w, oy + base_h))
    canvas_w, canvas_h = canvas_pil.size

    def move(which, t):
        iw2, ih2 = canvas_w, canvas_h
        if which == 0:
            z = 1 - 0.26*t; cw = iw2*z; ch = ih2*z; cx = iw2/2; cy = ih2/2
        elif which == 1:
            cw = iw2*0.85; ch = ih2; cx = iw2/2 + (iw2*0.15/2)*t; cy = ih2/2
        elif which == 2:
            z = 0.74 + 0.26*t; cw = iw2*z; ch = ih2*z; cx = iw2/2; cy = ih2/2
        else:
            cw = iw2*0.8; ch = ih2*0.8; cx = iw2*(0.5+0.5*t); cy = ih2*(0.5+0.5*t)
        cw = min(cw, iw2); ch = min(ch, ih2)
        cx = max(cw/2, min(iw2-cw/2, cx)); cy = max(ch/2, min(ih2-ch/2, cy))
        return cx, cy, cw, ch

    # crop is cheap on PIL; resize per frame with BILINEAR (fast, good enough).
    out_frames = []
    for k in range(max(1, N)):
        t = k / max(1, N - 1)
        cx, cy, cw, ch = move(motion % 4, t)
        x0 = max(0, int(round(cx - cw / 2))); y0 = max(0, int(round(cy - ch / 2)))
        x1 = min(canvas_w, int(x0 + cw)); y1 = min(canvas_h, int(y0 + ch))
        win = canvas_pil.crop((x0, y0, x1, y1))
        out_frames.append(np.asarray(win.resize((w, h), Image.BILINEAR), dtype=np.float32))
    data = np.stack(out_frames) if len(out_frames) > 1 else out_frames[0][None]
    rc, o, e = ffmpeg_run(['-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-s', f'{w}x{h}',
                            '-r', str(fps), '-i', '-'] + video_encode_args(20) + ['-threads', '0', out_path], input_data=data.astype(np.uint8).tobytes())
    return out_path

def make_video_clip(src, dur, out_path, w, h, fps, start=0.0):
    """Trim a source video from `start` to dur seconds and scale/pad to w x h; returns its real duration.
    start 为源视频内的起始时间（默认 0）。调用方按时间线切片时务必传入，否则每段都会从 0 秒截取，
    导致片头画面（如商标/Logo）被反复重复、后面内容完全缺失。"""
    real = probe_duration(src) or dur
    use = min(dur, max(0.0, real - start))
    if use < 0.5:
        use = dur
    rc, o, e = ffmpeg_run(['-y', '-ss', f'{start:.3f}', '-i', src, '-t', f'{use:.3f}',
                            '-vf', f'scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1',
                            '-r', str(fps)] + video_encode_args(20) + ['-threads', '0', '-an', out_path])
    return out_path, use


# ---------------------------------------------------------------------------
# 视频编码器选择：GPU 硬编(h264_nvenc) 优先，不可用时回退 CPU 软编(libx264)
# 渲染是长视频全流程的瓶颈（8 分钟实测约 360s），Whisper 早已走 CUDA，编码仍纯 CPU。
# ---------------------------------------------------------------------------
_ENC_CACHE = {'probe': None}


def video_encoder_cfg():
    """读取用户选择的编码策略：auto(默认·GPU 可用则用) / cpu / gpu。"""
    v = (load_ai_config().get('video') or {})
    mode = str(v.get('encoder') or 'auto').strip().lower()
    return mode if mode in ('auto', 'cpu', 'gpu') else 'auto'


def _probe_nvenc():
    """实际跑一个极短测试编码，确认 h264_nvenc 真能出片。
    仅查 `ffmpeg -encoders` 不够——驱动/格式不匹配时会"列表里有、运行期失败"。"""
    import tempfile
    out = os.path.join(tempfile.gettempdir(), '_framecut_nvenc_probe.mp4')
    ok = False
    try:
        rc, _o, _e = ffmpeg_run(['-y', '-f', 'lavfi', '-i', 'testsrc2=size=320x240:rate=10:duration=1',
                                 '-c:v', 'h264_nvenc', '-pix_fmt', 'yuv420p',
                                 '-preset', 'p4', '-rc', 'constqp', '-qp', '26', out])
        ok = (rc == 0) and os.path.exists(out) and os.path.getsize(out) > 0
    except Exception:
        ok = False
    try:
        if os.path.exists(out):
            os.remove(out)
    except Exception:
        pass
    return ok


def _nvenc_usable():
    """缓存探测结果（进程内只探测一次，避免每条命令都跑测试编码）。"""
    if _ENC_CACHE['probe'] is None:
        _ENC_CACHE['probe'] = bool(_probe_nvenc())
    return _ENC_CACHE['probe']


def reset_encoder_probe():
    """清空编码探测缓存（切换策略后 / 测试用）。"""
    _ENC_CACHE['probe'] = None


def video_encode_args(quality=23):
    """返回视频编码参数片段（list）。GPU 硬编可用且用户未禁用时用 h264_nvenc，否则回退 libx264。
    quality：质量档，越小越清晰（libx264 的 crf / nvenc 的 qp，语义对齐）。"""
    mode = video_encoder_cfg()
    if mode in ('auto', 'gpu') and _nvenc_usable():
        return ['-c:v', 'h264_nvenc', '-pix_fmt', 'yuv420p',
                '-preset', 'p4', '-rc', 'constqp', '-qp', str(int(quality))]
    # auto 但 GPU 不可用，或用户强制 cpu，或强制 gpu 却探测失败 → 一律回退 CPU 软编
    return ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-preset', 'veryfast',
            '-crf', str(int(quality))]


def video_encoder_label():
    """给前端展示当前实际生效的编码器。"""
    mode = video_encoder_cfg()
    if mode == 'cpu':
        return 'CPU 软编 libx264（已手动指定）'
    if _nvenc_usable():
        return 'GPU 硬编 h264_nvenc'
    return 'CPU 软编 libx264（未检测到可用 GPU 编码器）'


def probe_audio_len(path):
    """Return audio duration in seconds using ffmpeg."""
    rc, out, err = ffmpeg_run(['-i', path])
    import re
    m = re.search(r'Duration:\s*(\d+):(\d+):([\d.]+)', err.decode('utf-8', 'ignore'))
    if not m:
        return None
    hh, mm, ss = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return hh * 3600 + mm * 60 + ss


def analyze_beats(path):
    """Analyze audio with librosa: return (bpm, beat_times_in_seconds).
    beat_times are precise floats (ms precision). Returns None if analysis fails."""
    try:
        import librosa
    except Exception:
        return None
    try:
        y, sr = librosa.load(path, sr=22050, mono=True)
        tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units='frames')
        if beat_frames is None or len(beat_frames) == 0:
            tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
        beats = [float(v) for v in librosa.frames_to_time(beat_frames, sr=sr)]
        # filter out beats beyond audio length
        T = float(len(y)) / sr
        beats = [b for b in beats if b < T - 0.05]
        bpm_val = None
        try:
            arr = np.asarray(tempo)
            bpm_val = float(arr.ravel()[0] if arr.ndim else arr)
        except Exception:
            bpm_val = None
        return bpm_val, beats
    except Exception:
        return None


def plan_beat_durations(item_durs, beats, fade, step=1):
    """Return per-item display durations (list) such that:
       - total video length = sum(item_durs)  (driven by photo/video count & durations)
       - the N-1 interior cuts land on music beats, spaced `step` beats apart.
       step: 0.5 = every half-beat, 1 = every beat, 2 = every other beat, 4 = every 4th.
       Falls back to equal item_durs when beats are unusable."""
    N = len(item_durs)
    total = float(sum(item_durs))
    if N <= 1:
        return list(item_durs)
    try:
        step = float(step or 1.0)
    except (TypeError, ValueError):
        step = 1.0
    cum = []
    acc = 0.0
    for i in range(N - 1):
        acc += item_durs[i]
        cum.append(acc)

    # build a grid of allowed cut instants from the beats (incl. half-beat midpoints if step<1)
    grid = []
    if beats and len(beats) >= 2:
        for i in range(len(beats) - 1):
            b0, b1 = beats[i], beats[i + 1]
            if step < 1:
                grid.append((b0 + b1) / 2.0)
            grid.append(b1)
        grid = sorted(set(round(g, 4) for g in grid if g > 0))

    def nearest_allowed(cp):
        if not grid:
            return cp
        return min(grid, key=lambda g: abs(g - cp))

    snaps = [nearest_allowed(cp) for cp in cum]
    for i in range(1, len(snaps)):
        if snaps[i] <= snaps[i - 1]:
            snaps[i] = snaps[i - 1] + (0.3 * step if step >= 1 else 0.15)
    disp = []
    prev = 0.0
    for i in range(N - 1):
        disp.append(max(0.5, snaps[i] - prev))
        prev = snaps[i]
    last = max(0.6, total - prev)
    disp.append(last)
    return disp



# ---------------------------------------------------------------------------
# 内置免费踩点音乐曲库（Incompetech / CC.BY，可商用，署名即可）
# bpm/duration 为估算值，音频来自逐轨真实下载。
# ---------------------------------------------------------------------------
MUSIC_DIR = os.path.join(HERE, 'music_library')

MUSIC_CATALOG = [
    {'id': 'rising-game',    'title': 'Rising Game',      'genre': '电子/律动', 'bpm': 128, 'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Rising%20Game.mp3'},
    {'id': 'electro-cabello','title': 'Electro Cabello',  'genre': '电子/流行', 'bpm': 120, 'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Electro%20Cabello.mp3'},
    {'id': 'glitter-blast',  'title': 'Glitter Blast',    'genre': '电子/活力', 'bpm': 132, 'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Glitter%20Blast.mp3'},
    {'id': 'long-stroll',    'title': 'Long Stroll',      'genre': '轻快/行走', 'bpm': 110, 'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Long%20Stroll.mp3'},
    {'id': 'carefree',       'title': 'Carefree',         'genre': '轻快/乐观', 'bpm': 96,  'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Carefree.mp3'},
    {'id': 'cambodian-odyssey','title': 'Cambodian Odyssey','genre': '世界/律动','bpm': 100, 'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Cambodian%20Odyssey.mp3'},
    {'id': 'wholesome',      'title': 'Wholesome',        'genre': '温暖/治愈', 'bpm': 90,  'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Wholesome.mp3'},
    {'id': 'wallpaper',      'title': 'Wallpaper',        'genre': '氛围/环境', 'bpm': 80,  'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Wallpaper.mp3'},
    {'id': 'monkeys-spinning','title': 'Monkeys Spinning Monkeys','genre':'幽默/欢乐','bpm':156, 'license':'CC.BY 4.0','attri':'Kevin MacLeod (incompetech.com)','licenseUrl':'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Monkeys%20Spinning%20Monkeys.mp3'},
    {'id': 'fluffing-a-duck','title': 'Fluffing a Duck',  'genre': '轻快/趣味', 'bpm': 105, 'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Fluffing%20a%20Duck.mp3'},
    {'id': 'airport-lounge', 'title': 'Airport Lounge',   'genre': '爵士/氛围', 'bpm': 92,  'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Airport%20Lounge.mp3'},
    {'id': 'prelude-and-action','title': 'Prelude and Action','genre':'电影/磅礴','bpm':118, 'license':'CC.BY 4.0','attri':'Kevin MacLeod (incompetech.com)','licenseUrl':'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Prelude%20and%20Action.mp3'},
    {'id': 'lightless-dawn', 'title': 'Lightless Dawn',   'genre': '氛围/缓拍', 'bpm': 84,  'license': 'CC.BY 4.0', 'attri': 'Kevin MacLeod (incompetech.com)', 'licenseUrl': 'https://incompetech.com/music/royalty-free/mp3-royaltyfree/Lightless%20Dawn.mp3'},
]

def catalog_cached_size(mid):
    p = os.path.join(MUSIC_DIR, mid + '.mp3')
    if os.path.exists(p) and os.path.getsize(p) > 5000:
        try:
            return round(probe_audio_len(p), 2)
        except Exception:
            return None
    return None

def search_catalog(q=''):
    q = (q or '').strip().lower()
    os.makedirs(MUSIC_DIR, exist_ok=True)
    out = []
    for t in MUSIC_CATALOG:
        hay = (t['title'] + ' ' + t['genre'] + ' ' + t['id']).lower()
        if q and q not in hay:
            continue
        d = catalog_cached_size(t['id'])
        out.append({'id': t['id'], 'title': t['title'], 'genre': t['genre'],
                    'bpm': t['bpm'], 'license': t['license'], 'attri': t['attri'],
                    'licenseUrl': t['licenseUrl'], 'cached': d is not None,
                    'length': d})
    return out

def catalog_path(mid):
    return os.path.join(MUSIC_DIR, mid + '.mp3')

def download_catalog(mid):
    """Download a catalog track if not cached; return path or raise."""
    path = catalog_path(mid)
    if os.path.exists(path) and os.path.getsize(path) > 5000:
        return path
    track = next((t for t in MUSIC_CATALOG if t['id'] == mid), None)
    if not track:
        raise RuntimeError('未知曲目')
    os.makedirs(MUSIC_DIR, exist_ok=True)
    import urllib.request
    req = urllib.request.Request(track['licenseUrl'], headers={'User-Agent': 'Mozilla/5.0 SpringStudio'})
    with urllib.request.urlopen(req, timeout=120) as resp, open(path, 'wb') as f:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            f.write(chunk)
    if os.path.getsize(path) < 5000:
        os.remove(path)
        raise RuntimeError('下载失败')
    return path


# ---------------------------------------------------------------------------
# AI 能力：两套独立接口 —— 视觉(看图写文案) 与 TTS(中文配音) 可分别配 base_url/key/model
# 未配置该通道的 key 时，自动退回对应离线兜底，能力不中断。
# 配置结构（ai_config.json）：
#   vision:  {base_url, api_key, model}
#   tts:     {base_url, api_key, model, voice}
# ---------------------------------------------------------------------------
def _vision_available():
    v = load_ai_config().get('vision') or {}
    return bool(v.get('base_url') and v.get('api_key') and v.get('model'))


def _tts_available():
    t = load_ai_config().get('tts') or {}
    if not (t.get('api_key') and t.get('model')):
        return False
    # DashScope / MiMo have default endpoints, so base_url is optional
    if (t.get('provider') or 'openai').lower() in ('dashscope', 'mimo'):
        return True
    return bool(t.get('base_url'))


def ai_describe_image(img_path, name=''):
    """Use a vision (OpenAI-compatible) model to write a short Chinese caption for
    one image. Returns a short Chinese sentence. Falls back to offline template."""
    cfg = (load_ai_config().get('vision') or {})
    if not (cfg.get('base_url') and cfg.get('api_key') and cfg.get('model')):
        return offline_caption(name or img_path, 1, 1)
    try:
        import urllib.request, base64 as _b64, json as _json
        im = fromPIL(img_path, max_side=512)
        b64 = _b64.b64encode(im).decode('ascii')
        payload = {
            'model': cfg.get('model'),
            'messages': [{
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': '请用一句不超过20字的中文，描写这张春天风景图片的内容与氛围，直接输出这一句话，不要引号。'},
                    {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64}'}},
                ],
            }],
            'max_tokens': 500,
            'temperature': 0.7,
        }
        url = (cfg.get('base_url', '').rstrip('/')) + '/chat/completions'
        req = urllib.request.Request(url, data=_json.dumps(payload).encode('utf-8'),
                                     headers={'Content-Type': 'application/json',
                                              'Authorization': 'Bearer ' + cfg.get('api_key', '')})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = _json.loads(resp.read().decode('utf-8'))
        # content may be a string or a list; extract text, fall back if empty
        content = data['choices'][0]['message'].get('content')
        txt = ''
        if isinstance(content, str):
            txt = content
        elif isinstance(content, list):
            parts = [p.get('text', '') for p in content if isinstance(p, dict) and p.get('text')]
            txt = ''.join(parts)
        txt = (txt or '').strip()
        if txt:
            return txt[:40]
        return offline_caption(name or img_path, 1, 1)
    except Exception:
        return offline_caption(name or img_path, 1, 1)


def fromPIL(path, max_side=512):
    from PIL import Image as _I
    im = _I.open(path).convert('RGB')
    w, h = im.size
    scale = max_side / max(w, h)
    if scale < 1:
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), _I.BILINEAR)
    import io
    buf = io.BytesIO()
    im.save(buf, 'JPEG', quality=70)
    return buf.getvalue()


def ai_tts(text, out_path, voice=None):
    """Synthesize Chinese narration via configured TTS provider (openai-openai兼容 / dashscope通义千问). """
    cfg = (load_ai_config().get('tts') or {})
    api_key = cfg.get('api_key')
    model = cfg.get('model')
    if not (api_key and model):
        return False
    provider = (cfg.get('provider') or 'openai').lower()
    try:
        import urllib.request, json as _json
        if provider == 'dashscope':
            # 通义千问 / DashScope 非实时语音合成 (Model Studio) — 用配置的主机，缺省公共端点
            return _tts_dashscope(text, out_path, api_key, model, voice or cfg.get('voice', 'allmina'),
                                  cfg.get('base_url'))
        if provider == 'mimo':
            # 小米 MiMo 语音合成 (mimo.mi.com) — OpenAI 兼容 chat/completions + audio 参数
            return _tts_mimo(text, out_path, api_key, model, voice or cfg.get('voice', 'mimo_default'),
                             cfg.get('base_url'))
        # OpenAI-compatible
        url = (cfg.get('base_url', '').rstrip('/')) + '/audio/speech'
        payload = {'model': model, 'input': text,
                   'voice': voice or cfg.get('voice', 'alloy')}
        req = urllib.request.Request(url, data=_json.dumps(payload).encode('utf-8'),
                                     headers={'Content-Type': 'application/json',
                                              'Authorization': 'Bearer ' + api_key})
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = resp.read()
        with open(out_path, 'wb') as f:
            f.write(data)
        return os.path.getsize(out_path) > 500
    except Exception:
        return False


def _tts_dashscope(text, out_path, api_key, model, voice, base_url=None):
    """通义千问 DashScope 非实时语音合成 HTTP API.
    Uses the configured base_url (defaults to the public DashScope endpoint).
    model 例: qwen-audio-turbo / qwen2.5-audio-turbo / cosyvoice-v1
    voice 例: allmina / longxiaochun / cherry ...
    Sets _LAST_TTS_ERR on failure for the test panel. Returns True on success."""
    global _LAST_TTS_ERR
    import urllib.request, json as _json
    base = (base_url or 'https://dashscope.aliyuncs.com/api/v1').rstrip('/')
    # DashScope non-realtime TTS path is appended to the /api/v1 root
    url = base + '/services/aigc/text2audio/tts'
    payload = {
        'model': model,
        'input': {'text': text},
        'voice': voice,
        'parameters': {'format': 'mp3', 'sample_rate': 48000},
    }
    req = urllib.request.Request(url, data=_json.dumps(payload).encode('utf-8'),
                                 headers={'Content-Type': 'application/json',
                                          'Authorization': 'Bearer ' + api_key})
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            ctype = resp.headers.get('Content-Type', '')
            data = resp.read()
    except Exception as e:
        code = getattr(e, 'code', None)
        body = ''
        try:
            raw = getattr(e, 'read', lambda: b'')() if hasattr(e, 'read') else b''
            body = raw.decode('utf-8', 'ignore') if isinstance(raw, bytes) else str(raw)
        except Exception:
            pass
        _LAST_TTS_ERR = f'HTTP{code}: {body[:200]}' if code else str(e)[:200]
        return False
    # If JSON came back, it's an error/status payload
    if 'json' in ctype.lower():
        try:
            obj = _json.loads(data.decode('utf-8', 'ignore'))
            _LAST_TTS_ERR = '服务返回: ' + json.dumps(obj, ensure_ascii=False)[:200]
            return False
        except Exception:
            pass
    with open(out_path, 'wb') as f:
        f.write(data)
    return os.path.getsize(out_path) > 500


def _tts_mimo(text, out_path, api_key, model, voice, base_url=None):
    """小米 MiMo 语音合成（mimo.mi.com）。走 OpenAI 兼容 chat/completions + audio 参数。
    model 例: mimo-v2.5-tts / mimo-v2.5-tts-voicedesign / mimo-v2.5-tts-voiceclone
    voice 例: mimo_default / Mia / Chloe / Milo / Dean
    文本放在 assistant 消息；返回 audio.data(base64)。失败时设置 _LAST_TTS_ERR。"""
    global _LAST_TTS_ERR
    import urllib.request, json as _json, base64 as _b64
    base = (base_url or 'https://api.xiaomimimo.com/v1').rstrip('/')
    url = base + '/chat/completions'
    payload = {
        'model': model,
        'messages': [{'role': 'assistant', 'content': text}],
        'audio': {'voice': voice, 'format': 'mp3'},
    }
    req = urllib.request.Request(url, data=_json.dumps(payload).encode('utf-8'),
                                 headers={'Content-Type': 'application/json',
                                          'Authorization': 'Bearer ' + api_key})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = _json.loads(resp.read().decode('utf-8', 'ignore'))
    except Exception as e:
        code = getattr(e, 'code', None)
        body = ''
        try:
            raw = getattr(e, 'read', lambda: b'')() if hasattr(e, 'read') else b''
            body = raw.decode('utf-8', 'ignore') if isinstance(raw, bytes) else str(raw)
        except Exception:
            pass
        _LAST_TTS_ERR = (f'HTTP{code}: {body[:200]}' if code else str(e)[:200])
        return False
    try:
        audio = data['choices'][0]['message']['audio']
        b64 = audio['data']
        raw = _b64.b64decode(b64)
        with open(out_path, 'wb') as f:
            f.write(raw)
        return os.path.getsize(out_path) > 500
    except Exception:
        _LAST_TTS_ERR = '返回格式异常: ' + _json.dumps(data, ensure_ascii=False)[:200]
        return False


# ---------------------------------------------------------------------------
# 逐通道测试：用当前填写的配置实调一次接口，反馈是否有效
# ---------------------------------------------------------------------------
def _test_vision():
    """Test the configured vision channel with a bundled spring image. Returns (ok, msg)."""
    cfg = (load_ai_config().get('vision') or {})
    if not (cfg.get('base_url') and cfg.get('api_key') and cfg.get('model')):
        return False, '未配置：请填 ① 视觉 的 base_url + api_key + model'
    try:
        import urllib.request, base64 as _b64, json as _json
        test_img = None
        for i in (1, 2, 3, 4):
            p = os.path.join(HERE, f'img{i}.png')
            if os.path.exists(p):
                test_img = p
                break
        if not test_img:
            test_img = os.path.join(HERE, 'img1.png')
        im = fromPIL(test_img, max_side=256)
        b64 = _b64.b64encode(im).decode('ascii')
        payload = {
            'model': cfg.get('model'),
            'messages': [{'role': 'user', 'content': [
                {'type': 'text', 'text': '只回复“OK”，表示你能看到图片。'},
                {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{b64}'}},
            ]}],
            'max_tokens': 20,
        }
        url = (cfg.get('base_url', '').rstrip('/')) + '/chat/completions'
        req = urllib.request.Request(url, data=_json.dumps(payload).encode('utf-8'),
                                     headers={'Content-Type': 'application/json',
                                              'Authorization': 'Bearer ' + cfg.get('api_key', '')})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = _json.loads(resp.read().decode('utf-8'))
        txt = data['choices'][0]['message']['content'].strip()
        return True, f'有效（模型回复：{txt[:20]}）'
    except Exception as e:
        body = ''
        try:
            raw = getattr(e, 'read', lambda: b'')() if hasattr(e, 'read') else b''
            body = raw.decode('utf-8', 'ignore') if isinstance(raw, bytes) else str(raw)
        except Exception:
            pass
        code = getattr(e, 'code', None)
        return False, f'失败{(" HTTP"+str(code)) if code else ""}：{str(e)[:120]} {body[:200]}'


def _test_tts():
    """Test the configured TTS channel by synthesizing a short phrase. Returns (ok, msg)."""
    global _LAST_TTS_ERR
    cfg = (load_ai_config().get('tts') or {})
    if not (cfg.get('api_key') and cfg.get('model')):
        return False, '未配置：请填 ② TTS 的 api_key + 模型'
    os.makedirs(WORKDIR, exist_ok=True)
    out = os.path.join(WORKDIR, f'_tts_test_{int(time.time()*1000)}.mp3')
    _LAST_TTS_ERR = ''
    ok = ai_tts('春天来了', out)
    if ok and os.path.exists(out):
        size = os.path.getsize(out)
        try:
            os.remove(out)
        except Exception:
            pass
        return True, f'有效（生成 {round(size/1024,1)}KB 音频）'
    try:
        if os.path.exists(out):
            os.remove(out)
    except Exception:
        pass
    if _LAST_TTS_ERR:
        return False, '失败：' + _LAST_TTS_ERR
    provider = (cfg.get('provider') or 'openai').lower()
    if provider == 'dashscope':
        return False, '失败：请确认 DashScope Key 有效、模型已开通（如 qwen-audio-turbo）'
    return False, '失败：请确认 base_url/Key/model 正确，接口位于 {base_url}/audio/speech'


def build_srt(captions, starts, durs, out_path):
    """captions[i] with start/end seconds -> SRT subtitle file."""
    def ts(sec):
        hh = int(sec // 3600); mm = int((sec % 3600) // 60); ss = int(sec % 60); ms = int((sec % 1) * 1000)
        return f'{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}'
    lines = []
    for i, cap in enumerate(captions, 1):
        s, d = starts[i - 1], durs[i - 1]
        lines.append(str(i))
        lines.append(f'{ts(s)} --> {ts(s + d)}')
        lines.append(cap)
        lines.append('')
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    return out_path


# ---------------------------------------------------------------------------
# 🗂 分析缓存：场景切点/帧信号按「文件指纹+参数+分析版本」落盘复用，避免重复全片解码。
# 收益：人机协同反复调 strength/maxCuts 重看方案从分钟级变秒级；
#       卡点+解说连续作业共享同一套场景切点（解说分段不再单独全片再扫一遍）。
# 原则：任何缓存读写失败都静默回退实时分析，缓存只影响速度、不影响正确性。
# ---------------------------------------------------------------------------
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


def _cached_scene_cuts(video_path, threshold=0.30, progress=None):
    """场景切点（带磁盘缓存）。键含阈值+版本+文件指纹：改参数、换文件、
    文件内容被替换（size/mtime 变化）都会自动重新分析。"""
    fp = _file_fp(video_path)
    if not fp:
        return detect_scene_cuts(video_path, threshold=threshold)
    key = f'scene_v{ANALYSIS_VERSION}_th{threshold:g}_{fp}'
    hit = _analysis_cache_load(key)
    if isinstance(hit, list):
        return [float(x) for x in hit]
    kw = {'progress': progress} if progress is not None else {}
    cuts = detect_scene_cuts(video_path, threshold=threshold, **kw)
    _analysis_cache_save(key, cuts)   # 空结果也缓存：确属「无切点」的视频不必反复扫
    return cuts


def _cached_frame_signals(video_path, fps_s=4.0, progress=None):
    """帧信号（带磁盘缓存）：缓存 _analyze_video_frames 的信号 dict。
    键用「实际生效的 fps」（长视频会被 _adaptive_fps 降帧），避免键名与缓存内容不一致。"""
    fp = _file_fp(video_path)
    if not fp:
        return _analyze_video_frames(video_path, fps_s=fps_s, progress=progress)
    fps_eff = _adaptive_fps(probe_audio_len(video_path) or 0.0, fps_s)
    key = f'frames_v{ANALYSIS_VERSION}_fps{fps_eff:g}_{fp}'
    hit = _analysis_cache_load(key)
    if isinstance(hit, dict) and hit.get('times'):
        return hit
    an = _analyze_video_frames(video_path, fps_s=fps_eff, progress=progress)
    if an and an.get('times'):
        _analysis_cache_save(key, an)
    return an


# ---------------------------------------------------------------------------
# 🎯 智能强卡点引擎：场景切换/动作停顿帧 ↔ 音乐大鼓点 匹对
# 全部本地计算（ffmpeg scene 检测 + librosa 强拍检测 + numpy 帧差），不花 API 钱
# ---------------------------------------------------------------------------
def detect_scene_cuts(video_path, threshold=0.30, progress=None):
    """用 ffmpeg scene 滤镜检测视频场景切换点，返回切点秒列表（升序）。
    progress（可选 dict）：场景解码阶段按 time= 统计平滑推进 pct（5→24）。"""
    import re
    vdur = probe_audio_len(video_path) or 0.0

    def on_progress(sec):
        if progress and vdur > 0:
            progress['phase'] = '检测场景切换（全片解码）'
            progress['pct'] = min(24, 5 + int(sec * 19 / vdur))

    rc, out, err = ffmpeg_run(['-hide_banner', '-i', video_path,
                               '-vf', f"select='gt(scene,{threshold})',showinfo",
                               '-an', '-f', 'null', '-'], on_progress=on_progress)
    cuts = []
    for m in re.finditer(r'pts_time:([0-9.]+)', err.decode('utf-8', 'ignore')):
        t = float(m.group(1))
        if t > 0.3:
            cuts.append(round(t, 3))
    cuts = sorted(set(cuts))
    return cuts


# ---------------------------------------------------------------------------
# 长视频抽帧保护：总帧数封顶 + 分析帧长边降采样。
# 依据：下游阈值全是分位数/IQR 相对阈值(_detect_motion_from_frames/_detect_visual_from_frames)，
# 分辨率缩放不影响检测；切点最小间隔 min_gap 0.5~0.6s，fps≥2 的时间粒度足够。
# ---------------------------------------------------------------------------
_ANALYZE_MAX_SIDE = 640     # 分析帧长边上限（1080p 一帧 ~6MB → 640p ~0.7MB，管道 I/O 大减）
_ANALYZE_MAX_FRAMES = 1800  # 单次分析总帧数上限，超出按视频时长自适应降 fps


def _scaled_dims(w, h, max_side=_ANALYZE_MAX_SIDE):
    """把分析帧按长边等比压到 max_side 以内（宽高取偶），已达标则原样返回。"""
    w, h = int(w), int(h)
    m = max(w, h)
    if m <= max_side or m <= 0:
        return w, h
    f = max_side / float(m)
    w2 = max(2, int(w * f) // 2 * 2)
    h2 = max(2, int(h * f) // 2 * 2)
    return w2, h2


def _adaptive_fps(duration_s, fps_s=4.0, max_frames=_ANALYZE_MAX_FRAMES):
    """长视频自适应降 fps：总帧数 ≤ max_frames 封顶（下限 0.5——1 小时视频恰为 1800 帧；
    0.5s 粒度对切点候选足够，精确切点由独立的场景检测全片解码承担）。
    短视频（≤max_frames/fps_s 秒）与未知时长不受影响，fps 原样透传。"""
    if not duration_s or duration_s <= 0:
        return fps_s
    return round(min(float(fps_s), max(0.5, max_frames / float(duration_s))), 3)


def _analyze_video_frames(video_path, fps_s=4.0, progress=None):
    """流式抽帧，一次管道同时算 4 类信号：
       motion(均值帧差) / frac(像素变化比例,更灵敏) / hist(色相直方图突变) / bright(亮度)。
    返回 dict：{times, motion, frac, hist, bright}（等长，首帧 motion/frac/hist 为 0）。
    长视频保护：复用同一次探测的 stderr 顺带解析时长，自动降 fps 封顶总帧数，
    并把分析帧长边压到 _ANALYZE_MAX_SIDE（不加参数、不多跑一次 ffmpeg）。"""
    import subprocess as _sp
    import numpy as np
    import re
    exe = ffmpeg_exe()
    rc, out, err = ffmpeg_run(['-i', video_path])
    err_text = err.decode('utf-8', 'ignore')
    m = re.search(r'(\d{2,4})x(\d{2,4})', err_text)
    if not m:
        return None
    md = re.search(r'Duration:\s*(\d+):(\d{2}):(\d+(?:\.\d+)?)', err_text)
    vdur = (int(md.group(1)) * 3600 + int(md.group(2)) * 60 + float(md.group(3))) if md else 0.0
    fps_s = _adaptive_fps(vdur, fps_s)
    w, h = _scaled_dims(int(m.group(1)), int(m.group(2)))
    expected = max(1, int(vdur * fps_s))
    fb = w * h * 3  # rgb24（降采样后）
    if progress:
        progress['phase'] = '检测动作/镜头切换（逐帧分析）'
        progress['pct'] = 25
    proc = _sp.Popen([exe, '-hide_banner', '-i', video_path, '-vf', f'fps={fps_s},scale={w}:{h}',
                      '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-vcodec', 'rawvideo', '-'],
                     stdout=_sp.PIPE, stderr=_sp.DEVNULL)
    times, motion, frac, hist, bright = [], [], [], [], []
    prev_gray = None
    prev_hist = None
    t = 0.0
    try:
        while True:
            data = proc.stdout.read(fb)
            if not data or len(data) < fb:
                break
            rgb = np.frombuffer(data, dtype=np.uint8).reshape(h, w, 3)
            gray = rgb.astype(np.float32).mean(axis=2)
            hcat = []
            for ch in range(3):
                hh, _ = np.histogram(rgb[:, :, ch], bins=16, range=(0, 256))
                hcat.append(hh)
            hcat = np.concatenate(hcat).astype(np.float32)
            hsum = float(hcat.sum())
            if hsum > 0:
                hcat /= hsum
            if prev_gray is not None:
                adiff = np.abs(gray - prev_gray)
                motion.append(float(adiff.mean()))
                frac.append(float((adiff > 15.0).mean()))
                hist.append(float(np.abs(hcat - prev_hist).sum()))
            else:
                motion.append(0.0)
                frac.append(0.0)
                hist.append(0.0)
            prev_gray = gray
            prev_hist = hcat
            bright.append(float(gray.mean()))
            times.append(t)
            t += 1.0 / fps_s
            if progress and len(times) % 20 == 0:   # 每 20 帧推进一次，长视频不再长期停在固定百分比
                progress['pct'] = min(44, 25 + int(len(times) * 19 / expected))
    finally:
        try:
            proc.kill()
        except Exception:
            pass
    return {'times': times, 'motion': motion, 'frac': frac, 'hist': hist, 'bright': bright}


def _detect_motion_from_frames(an, min_gap=0.6, strength='standard'):
    """从已抽帧信号(an=_analyze_video_frames 返回值)检测动作/停顿候选切点。
    供 _analyze_beatcut 一次抽帧同时算动作+视觉，避免重复全片解码。"""
    import numpy as np
    if not an or len(an['frac']) < 5:
        return []
    times, frac = an['times'], an['frac']
    vals = frac[1:]
    if not vals:
        return []
    p25, p75 = [float(x) for x in np.percentile(vals, [25, 75])]
    iqr = p75 - p25
    th = max(p75 + 1.5 * iqr, 0.004)   # 相对阈值，几乎无绝对下限
    if strength == 'soft':
        th *= 1.6
    elif strength == 'strong':
        th *= 0.5
    peaks = []
    pauses = []
    for i in range(1, len(frac) - 1):
        if frac[i] > th and frac[i] >= frac[i - 1] and frac[i] >= frac[i + 1]:
            peaks.append(times[i])
        if frac[i] < p25 * 0.9 and frac[i] <= frac[i - 1] and frac[i] <= frac[i + 1]:
            pauses.append(times[i])
    cands = sorted(set([round(x, 2) for x in peaks + pauses]))
    out = []
    last = -1e9
    for c in cands:
        if c - last >= min_gap:
            out.append(c)
            last = c
    return out


def _detect_visual_from_frames(an, strength='standard'):
    """从已抽帧信号(an=_analyze_video_frames 返回值)检测镜头/色调/亮度切换候选切点。"""
    import numpy as np
    if not an or len(an['hist']) < 5:
        return []
    times, hist, bright = an['times'], an['hist'], an['bright']
    hv = hist[1:]
    th_h = max(float(np.percentile(hv, 80)) * 0.8, 0.05)
    bd = [abs(bright[i] - bright[i - 1]) for i in range(1, len(bright))]
    th_b = max(float(np.percentile(bd, 90)) * 0.7, 8.0)
    if strength == 'soft':
        th_h *= 1.6
        th_b *= 1.5
    elif strength == 'strong':
        th_h *= 0.5
        th_b *= 0.6
    cands = []
    for i in range(1, len(times)):
        if hist[i] > th_h or bd[i - 1] > th_b:
            cands.append(times[i])
    out = []
    last = -1e9
    for c in cands:
        if c - last >= 0.5:
            out.append(round(c, 2))
            last = c
    return out


def detect_motion_points(video_path, fps_s=4.0, min_gap=0.6, strength='standard'):
    """帧级动作检测：用像素变化比例(frac)做信号，分位数自适应阈值 + 动作峰 + 停顿帧。
    strength: soft(柔和)/standard/strong(强力)。返回候选切点秒列表。"""
    return _detect_motion_from_frames(_analyze_video_frames(video_path, fps_s), min_gap, strength)


def detect_visual_cues(video_path, fps_s=4.0, strength='standard'):
    """帧级视觉线索：色相直方图突变(镜头/色调切换) + 亮度突变(曝光/场景切换)。"""
    return _detect_visual_from_frames(_analyze_video_frames(video_path, fps_s), strength)


def _music_onset_peaks(music_path, delta=0.18, wait=0.25, sr=22050, hop=512):
    """librosa onset 峰值检测公共层：「智能强卡点」与「节拍同步」两引擎共用同一套
    加载+onset 包络+峰值挑选，避免两份逐行重复的 librosa 代码各自漂移。
    返回 (峰值秒列表, 峰值onset强度列表, 音乐总时长)；librosa 不可用/失败返回 ([], [], 0.0)。
    注意：librosa peak_pick 的 wait 单位是「帧」不是秒（hop=512@22050Hz ≈ 23ms/帧）。"""
    try:
        import librosa
    except Exception:
        return [], [], 0.0
    try:
        y, sr = librosa.load(music_path, sr=sr, mono=True)
        if len(y) < sr:
            return [], [], 0.0
        T = float(len(y)) / sr
        onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
        times = librosa.times_like(onset_env, sr=sr, hop_length=hop)
        peaks = librosa.util.peak_pick(onset_env, pre_max=8, post_max=8, pre_avg=8,
                                       post_avg=8, delta=delta, wait=wait)
        pts = [float(times[p]) for p in peaks]
        vals = [float(onset_env[p]) for p in peaks]
        return pts, vals, T
    except Exception:
        return [], [], 0.0


def detect_strong_beats(music_path, top_k=None, min_sep=0.25):
    """检测音乐"大鼓点"（强 onset 峰值）。返回(强拍秒列表升序, 每秒拍数估计)。
    强拍用「时间窗分桶」挑选而非全局最强 top_k：否则最强 onset 会集中在音乐前段，
    视频后段没有强拍可吸附，导致卡点全部落在片头、后半段完全踩不上鼓点。"""
    pts, vals, T = _music_onset_peaks(music_path, delta=0.18, wait=min_sep)
    if not pts:
        return [], None
    # 时间窗分桶：把整段音乐均分 top_k 个窗，每窗取局部最强 onset → 强拍均匀覆盖全曲
    if top_k and len(pts) > top_k:
        picked = []
        seen = set()
        win = T / top_k
        for k in range(top_k):
            w0, w1 = k * win, (k + 1) * win
            best_i, best_v = -1, -1.0
            for i in range(len(pts)):
                if pts[i] < w0 or pts[i] >= w1:
                    continue
                if vals[i] > best_v:
                    best_v, best_i = vals[i], i
            if best_i < 0:
                # 空窗（该段音乐平缓无 onset）：不强凑，交给相邻窗
                continue
            if pts[best_i] in seen:
                continue
            picked.append(pts[best_i])
            seen.add(pts[best_i])
        if picked:
            pts = sorted(picked)
    # estimate beats-per-second from inter-peak median
    gaps = [pts[i+1] - pts[i] for i in range(len(pts) - 1) if pts[i+1] - pts[i] > 0.2]
    bps = None
    if gaps:
        import numpy as np
        bps = 1.0 / float(np.median(gaps))
    return pts, bps


def plan_beat_cuts(scene_cuts, motion_cuts, beats, video_dur, min_seg=None, max_seg=9.0, tol=0.35,
                   visual_cuts=None, strength='standard', max_cuts=None):
    """把视频切点(场景+动作+视觉线索)加权匹对到最近强拍，生成强卡点时间线。
    权重：场景切>动作>视觉线索；强力模式更严格吸附强拍；段过长时用强拍网格兜底。
    - min_seg 默认按强度自适应（soft 1.5 / standard 1.2 / strong 1.0），避免 1 秒碎切闪屏；
    - max_cuts 限制最终段数（约 3.5s/段，默认最多 48）：切点超量时均匀抽稀保留首尾，
      否则卡点会切成几十个 1 秒碎段，观感像画面反复跳。"""
    if not beats:
        beats = []
    if min_seg is None:
        min_seg = {'soft': 1.5, 'standard': 1.2, 'strong': 1.0}.get(strength, 1.2)
    if max_cuts is None:
        max_cuts = max(6, min(48, int(video_dur / 3.5)))
    # weighted candidates
    cand = []
    for c in (scene_cuts or []):
        cand.append((round(float(c), 2), 3))
    for c in (motion_cuts or []):
        cand.append((round(float(c), 2), 2))
    for c in (visual_cuts or []):
        cand.append((round(float(c), 2), 1))
    merged = {}
    for c, w in cand:
        if c in merged:
            merged[c] = max(merged[c], w)
        else:
            merged[c] = w
    # 候选过多时先控制规模：按权重优先 + 时间均匀去重
    if len(merged) > max_cuts * 3:
        keep = []
        ranked = sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))
        step = video_dur / max_cuts
        last = -1e9
        for c, w in ranked:
            if c - last >= step * 0.9:
                keep.append(c)
                last = c
        if len(keep) < max_cuts:
            keys = sorted(merged.keys())
            keep = keys[::max(1, len(keys) // max_cuts)][:max_cuts]
        merged = {c: merged[c] for c in sorted(keep)}
    cuts = []
    used = set()
    eff_tol = tol * 0.7 if strength == 'strong' else (tol * 1.4 if strength == 'soft' else tol)
    for c in sorted(merged):
        w = merged[c]
        if c <= 0.3 or c >= video_dur - 0.3:
            continue
        best = min(beats, key=lambda b: abs(b - c)) if beats else None
        target = best if (best is not None and abs(best - c) <= eff_tol) else c
        if any(abs(target - u) < min_seg for u in used):
            continue
        cuts.append(round(target, 3))
        used.add(target)
    cuts.sort()
    timeline = [0.0]
    for c in cuts:
        if c - timeline[-1] >= min_seg and video_dur - c >= min_seg * 0.6:
            timeline.append(c)
    # 强拍网格兜底：某段过长时，在中点附近最近的强拍补切
    if beats:
        i = 1
        guard = 0
        while i < len(timeline) and guard < 200:
            guard += 1
            if timeline[i] - timeline[i - 1] > max_seg:
                mid = (timeline[i] + timeline[i - 1]) / 2.0
                nb = min(beats, key=lambda b: abs(b - mid))
                if timeline[i - 1] + min_seg < nb < timeline[i] - min_seg:
                    timeline.insert(i, round(nb, 3))
                    continue
            i += 1
    # 段数超限 → 均匀抽稀（保留首尾），避免几十个 1 秒碎段闪屏
    if len(timeline) - 1 > max_cuts:
        n = len(timeline) - 1
        keep_idx = sorted(set([0, n] + [int(round(j * n / max_cuts)) for j in range(1, max_cuts)]))
        timeline = [timeline[i] for i in keep_idx]
        tl2 = [timeline[0]]
        for c in timeline[1:]:
            if c - tl2[-1] >= min_seg * 0.6:
                tl2.append(c)
        if len(tl2) > 1 and video_dur - tl2[-1] < 0.4:
            tl2.pop()
        timeline = tl2
    timeline.append(video_dur)
    return timeline


def _analyze_beatcut(video_path, music_path, params, progress=None):
    """卡点分析阶段：场景/动作/视觉线索 + 音乐大鼓点 → 切点时间线。
    拆出供「人机协同」复用：用户可在预览界面增删切点后再按自己的时间线渲染。"""
    def up(ph, pct):
        if progress:
            progress['phase'] = ph; progress['pct'] = pct
    strength = params.get('strength', 'standard')
    vdur = probe_audio_len(video_path) or 0.0
    if vdur <= 0:
        raise RuntimeError('无法读取视频时长')
    # maxCuts 服务端钳制 3~96：API/指令路径可绕过前端滑条范围，极端值会生成上百段拖垮渲染
    max_cuts = max(3, min(96, int(params.get('maxCuts', 30) or 30)))
    up('检测场景切换', 5)
    scene_cuts = _cached_scene_cuts(video_path, threshold=float(params.get('sceneTh', 0.30)), progress=progress)
    up('检测动作/镜头切换', 25)
    # 一次抽帧同时算动作+视觉线索（避免两次全片解码）；结果带磁盘缓存，重跑方案秒级返回
    frames = _cached_frame_signals(video_path, 4.0, progress=progress)
    motion_cuts = _detect_motion_from_frames(frames, strength=strength)
    visual_cuts = _detect_visual_from_frames(frames, strength=strength)
    up('分析音乐大鼓点', 45)
    strong_beats, bps = detect_strong_beats(music_path, top_k=max_cuts)
    timeline = plan_beat_cuts(scene_cuts, motion_cuts, strong_beats, vdur,
                              visual_cuts=visual_cuts, strength=strength,
                              max_cuts=max_cuts)
    diag = {
        'scene_cuts': scene_cuts,
        'motion_cuts': motion_cuts,
        'visual_cuts': visual_cuts,
        'strong_beats': strong_beats[:40],
        'timeline': timeline,
        'segments': len(timeline) - 1,
        'strength': strength,
    }
    return timeline, diag, vdur


def _render_beatcut(video_path, music_path, timeline, params, run_dir, progress=None, diag=None, pct_base=30):
    """卡点渲染阶段：按给定切点时间线切片→拼接(硬切/转场)→配乐(纯音乐/保留原声)。返回 final 路径。
    pct_base：进度起点——直连生成时分析阶段已占 0~50，渲染从 50 起（避免进度回跳）；
    人机协同确认渲染没有分析阶段，维持默认 30。"""
    def up(ph, pct):
        if progress:
            progress['phase'] = ph; progress['pct'] = pct
    span0, span1 = pct_base, 95

    def rp(frac):
        return int(span0 + (span1 - span0) * frac)

    nseg = max(1, len(timeline) - 1)
    up('按鼓点切片', rp(0.0))
    segs = []
    for i in range(len(timeline) - 1):
        if _aborted():
            raise AbortError('用户取消了任务')
        up('按鼓点切片 %d/%d' % (i + 1, nseg), rp((i + 1) / nseg * 0.6))
        seg = os.path.join(run_dir, f'bc{i}.mp4')
        segs.append(seg)
        seg_dur = timeline[i + 1] - timeline[i]
        # 必须传 start=timeline[i]：否则每段都从源视频 0 秒截取，片头画面反复出现
        make_video_clip(video_path, seg_dur, seg,
                        w=int(params.get('w', W)), h=int(params.get('h', H)), fps=int(params.get('fps', 30)),
                        start=timeline[i])
    # 拼接：有转场 → xfade 链（重编码）；否则 concat demuxer 免重编码（快），失败兜底 filter concat
    transition = params.get('transition') or 'none'
    fade_dur = min(0.6, max(0.1, float(params.get('transDur', 0.2))))
    silent = os.path.join(run_dir, 'bc_silent.mp4')
    if transition and transition != 'none':
        up('按鼓点拼接（转场 %s）' % transition, 32)
        inputs = ['-y']
        for s in segs:
            inputs += ['-i', s]
        durs = [probe_audio_len(s) or max(0.3, timeline[i + 1] - timeline[i]) for i, s in enumerate(segs)]
        fc_parts = []
        prev = '[0:v]'
        cum = 0.0
        n = len(segs)
        for i in range(1, n):
            cum += durs[i - 1]
            off = cum - i * fade_dur
            label = 'v%d' % i
            fc_parts.append('%s[%d:v]xfade=transition=%s:duration=%.3f:offset=%.3f[%s]'
                            % (prev, i, transition, fade_dur, max(0.0, off), label))
            prev = '[%s]' % label
        fc_parts.append('%sformat=yuv420p[vo]' % prev)
        fc = ';'.join(fc_parts)
        cmd = inputs + ['-filter_complex', fc, '-map', '[vo]',
                        ] + video_encode_args() + ['-threads', '0', '-r', str(int(params.get('fps', 30))), silent]
        rc, o, e = ffmpeg_run(cmd)
    else:
        up('按鼓点硬切拼接', 30)
        concat_txt = os.path.join(run_dir, 'bc.concat.txt')
        with open(concat_txt, 'w', encoding='utf8') as f:
            for s in segs:
                f.write("file '%s'\n" % s.replace("'", "'\\''"))
        try:
            rc, o, e = ffmpeg_run(['-y', '-f', 'concat', '-safe', '0', '-i', concat_txt,
                                   '-c', 'copy', silent])
        finally:
            if os.path.exists(concat_txt):
                try:
                    os.unlink(concat_txt)
                except Exception:
                    pass
        if rc != 0:
            # 个别段编码参数不一致导致 copy 失败 → 兜底重编码拼接
            parts = ''.join(f'[{i}:v]' for i in range(len(segs)))
            fc = f'{parts}concat=n={len(segs)}:v=1:a=0[vout]'
            cmd = ['-y']
            for s in segs:
                cmd += ['-i', s]
            cmd += ['-filter_complex', fc, '-map', '[vout]'] + video_encode_args() + ['-threads', '0', silent]
            rc, o, e = ffmpeg_run(cmd)
    if rc != 0:
        raise RuntimeError('卡点拼接失败: ' + e.decode('utf-8', 'ignore')[-400:])
    silent_dur = probe_audio_len(silent) or timeline[-1]
    up('合成配乐', 55)
    final = os.path.join(run_dir, 'final.mp4')
    keep_audio = bool(params.get('keepAudio'))
    if keep_audio and _has_audio_track(video_path):
        # 保留原声：原声压低(0.3) + 音乐铺底(0.7)，节奏感与内容兼顾
        cmd = ['-y', '-i', silent, '-i', video_path, '-stream_loop', '-1', '-i', music_path,
               '-filter_complex',
               "[1:a]aresample=44100,aformat=channel_layouts=stereo,volume=0.3[o];"
               "[2:a]aresample=44100,aformat=channel_layouts=stereo,volume=0.7[bgm];"
               "[o][bgm]amix=inputs=2:normalize=0,aformat=fltp[aout]",
               '-map', '0:v:0', '-map', '[aout]',
               '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
               '-t', '%.2f' % silent_dur, '-movflags', '+faststart', final]
    else:
        # 纯音乐（默认）：音乐铺满视频时长
        cmd = ['-y', '-stream_loop', '-1', '-i', music_path, '-i', silent,
               '-map', '1:v:0', '-map', '0:a:0', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
               '-t', '%.2f' % silent_dur, '-movflags', '+faststart', final]
    rc, o, e = ffmpeg_run(cmd)
    if rc != 0:
        raise RuntimeError('配乐失败: ' + e.decode('utf-8', 'ignore')[-400:])
    if progress:
        progress['done'] = True
        progress['pct'] = 100
        progress['file'] = os.path.relpath(final, OUTDIR).replace('\\', '/')
    if diag is not None:
        diag.update({
            'timeline': timeline,
            'segments': len(timeline) - 1,
            'transition': transition,
            'keep_audio': bool(params.get('keepAudio')),
        })
    return final


def beat_cut_video(video_path, music_path, run_dir, params, progress=None):
    """智能强卡点主流程：分析→对齐→硬切拼接→配乐。返回 final 路径与诊断信息。"""
    timeline, diag, vdur = _analyze_beatcut(video_path, music_path, params, progress)
    final = _render_beatcut(video_path, music_path, timeline, params, run_dir, progress, diag, pct_base=50)
    return final, diag
def detect_beats(audio_path, sensitivity=0.5):
    """检测音乐节拍点(秒，升序)。sensitivity∈(0,1) 越高越灵敏；检测失败返回 []。"""
    # 灵敏度越高 → delta 越小 → 检出越多拍点；wait=15 帧约 0.35s 最小拍间隔
    delta = max(0.02, 0.30 - 0.25 * float(sensitivity))
    pts, _vals, T = _music_onset_peaks(audio_path, delta=delta, wait=15)
    if not pts:
        return []
    return [t for t in pts if t < T - 0.05]


def generate_beat_sync_video(video_path, audio_path, output_path, beat_sensitivity=0.5,
                             min_clip_dur=0.6, progress=None):
    """节拍同步成片：按音乐节拍把源视频切成不重复片段硬接，再混入音乐。
    - 节拍识别失败时自动生成虚拟节拍兜底；
    - 片段池顺序轮转 + 轮空洗牌，避免画面顺序重复；
    - concat demuxer + inpoint/outpoint，免重编码，不闪跳；
    - ffmpeg 出错直接抛异常，绝不返回损坏视频。
    返回 {ok, output, beat_num, clip_num, warning}。"""
    def up(ph, pct):
        if progress is not None:
            progress['phase'] = ph
            progress['pct'] = pct

    video_path = str(video_path)
    audio_path = str(audio_path)
    output_path = str(output_path)
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    music_dur = probe_audio_len(audio_path)
    src_video_dur = probe_audio_len(video_path)
    if not music_dur or not src_video_dur:
        raise RuntimeError('无法读取音视频时长')

    up('检测节拍', 8)
    beat_times = detect_beats(audio_path, sensitivity=beat_sensitivity)
    min_beat_gap = 0.35
    filtered = []
    prev = -999
    for t in beat_times:
        if t - prev >= min_beat_gap and t < music_dur:
            filtered.append(round(t, 2))
            prev = t
    beat_times = filtered

    if len(beat_times) < 2:
        # 节拍识别失败/拍点过少 → 虚拟节拍兜底
        virtual_gap = 1.2
        beat_times = []
        cur = virtual_gap
        while cur < music_dur:
            beat_times.append(round(cur, 2))
            cur += virtual_gap
    beat_times = [0.0] + beat_times
    beat_times.append(round(music_dur, 2))
    beat_count = len(beat_times) - 1

    up('切分素材片段', 16)
    clip_pool = []
    # 片段长度需能覆盖最长节拍段：否则每个节拍段的画面只截了 min_clip_dur，
    # 拼接总长会明显短于音乐时长，-shortest 会把音乐结尾砍掉（音画错位）。
    beat_gaps = [beat_times[i + 1] - beat_times[i] for i in range(len(beat_times) - 1)]
    max_gap = max(beat_gaps) if beat_gaps else min_clip_dur
    clip_len = max(float(min_clip_dur), float(max_gap) * 1.2, 1.2)
    # 片段池必须覆盖全片且数量充足：seg_step 取 clip_len 的 50%（片段互相重叠），
    # 否则池子会只有片头 1~2 段（旧 bug：所有节拍段反复复用开头画面，后面内容完全用不上）。
    seg_step = max(float(min_clip_dur), clip_len * 0.5)
    t = 0.0
    while t + clip_len <= src_video_dur:
        end = round(t + clip_len + random.uniform(0, 0.4), 2)
        clip_pool.append((round(t, 2), end))
        t += seg_step
    # 让最后一段尽量覆盖到片尾，避免尾部素材完全用不上
    if clip_pool and src_video_dur - clip_pool[-1][0] > clip_len * 0.8:
        clip_pool.append((round(max(0.0, src_video_dur - clip_len), 2), round(src_video_dur, 2)))
    if not clip_pool:
        # 源视频过短：把整段作为唯一素材片段
        clip_pool.append((0.0, round(src_video_dur, 2)))
    total_clips = len(clip_pool)
    warn_msg = ''
    if total_clips == 0:
        raise RuntimeError('源视频无法提取可用片段，请换更长的源视频')

    assign_list = []
    idx = 0
    clip_indices = list(range(total_clips))
    for i in range(beat_count):
        s_t = beat_times[i]
        e_t = beat_times[i + 1]
        seg_dur = round(e_t - s_t, 2)
        if seg_dur < min_clip_dur:
            seg_dur = min_clip_dur
        if e_t > music_dur:
            e_t = music_dur
            seg_dur = round(e_t - s_t, 2)
        if idx >= len(clip_indices):
            random.shuffle(clip_indices)
            idx = 0
        clip_idx = clip_indices[idx]
        idx += 1
        src_s, src_e = clip_pool[clip_idx]
        # 关键：该节拍段的画面时长 = seg_dur（拍点间隔），从源片段截取 seg_dur 长，
        # 保证拼接总长 ≈ 音乐时长，音乐完整播完、卡点精确对齐。
        out_end = min(src_e, round(src_s + seg_dur, 2))
        assign_list.append({
            'beat_start': s_t,
            'beat_end': e_t,
            'src_start': src_s,
            'src_end': out_end,
            'seg_dur': seg_dur,
        })
    if beat_count > total_clips:
        warn_msg = '节拍点%d个，可用素材片段%d个，部分画面会循环复用' % (beat_count, total_clips)

    up('按节拍拼接', 40)
    base = os.path.splitext(output_path)[0]
    concat_txt = base + '.concat.txt'
    with open(concat_txt, 'w', encoding='utf8') as f:
        for item in assign_list:
            safe_path = video_path.replace("'", "'\\''")
            f.write("file '%s'\n" % safe_path)
            f.write('inpoint %s\n' % item['src_start'])
            f.write('outpoint %s\n' % item['src_end'])

    temp_video = base + '.temp_noaudio.mp4'
    try:
        rc, out, err = ffmpeg_run(['-y', '-f', 'concat', '-safe', '0', '-i', concat_txt,
                                   '-t', str(music_dur)] + video_encode_args() + [temp_video])
        if rc != 0:
            raise RuntimeError('片段拼接失败: ' + err.decode('utf-8', 'ignore')[-1200:])
        up('合成配乐', 80)
        rc, out, err = ffmpeg_run(['-y', '-i', temp_video, '-i', audio_path,
                                   '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-ar', '44100',
                                   '-shortest', output_path])
        if rc != 0:
            raise RuntimeError('音频混流失败: ' + err.decode('utf-8', 'ignore')[-1200:])
    finally:
        if os.path.exists(concat_txt):
            try:
                os.unlink(concat_txt)
            except Exception:
                pass
        if os.path.exists(temp_video):
            try:
                os.unlink(temp_video)
            except Exception:
                pass

    return {'ok': True, 'output': output_path, 'beat_num': beat_count,
            'clip_num': total_clips, 'warning': warn_msg}


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


def whisper_device():
    """返回 (device, compute_type)：检测到 NVIDIA CUDA 就用 GPU 加速（float16），否则回退 CPU(int8)。
    让「省流(本地离线)」模式在有无显卡的机器上都能跑，且尽量用显卡提速。"""
    try:
        import torch
        if getattr(torch, 'cuda', None) is not None and torch.cuda.is_available():
            return 'cuda', 'float16'
    except Exception:
        pass
    return 'cpu', 'int8'


def asr_segments(video_path):
    """faster-whisper 本地转写台词，返回 [{start,end,text}]。不可用/失败返回 []。
    自动用 GPU（CUDA）加速；无显卡回退 CPU。模型权重首次运行联网下载一次（~140MB）。"""
    try:
        from faster_whisper import WhisperModel
    except Exception:
        return []
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
        segments, _info = model.transcribe(wav, language='zh', vad_filter=True,
                                           initial_prompt='以下是普通话的句子。')
        segs = [{'start': float(s.start), 'end': float(s.end), 'text': (s.text or '').strip()}
                for s in segments if s.text and s.text.strip()]
        try:
            os.remove(wav)
        except Exception:
            pass
        return segs
    except Exception:
        try:
            if os.path.exists(wav):
                os.remove(wav)
        except Exception:
            pass
        return []


def _segment_timeline(video_path, max_seg=25.0):
    """解说分段：场景切分优先，无切点则按 max_seg 均分。返回 [(start,end)]。
    max_seg 服务端钳制到 4~600 秒：API/指令路径可绕过前端 min=8 的限制，
    极端值（如 0.5s）会切出成百上千个碎段，直接拖垮解说稿生成与配音。"""
    max_seg = min(600.0, max(4.0, float(max_seg or 25.0)))
    sc = _cached_scene_cuts(video_path, threshold=0.25)   # 与卡点共享场景切点缓存
    vdur = probe_audio_len(video_path) or 0.0
    if vdur <= 0:
        return []
    if sc:
        tl = [0.0]
        for c in sc:
            if c - tl[-1] >= 4.0 and vdur - c >= 3.0:
                tl.append(c)
        tl.append(vdur)
    else:
        n = max(1, int(vdur / max_seg))
        tl = [i * vdur / n for i in range(n + 1)]
    segs = []
    for i in range(len(tl) - 1):
        if tl[i + 1] - tl[i] >= 1.5:
            segs.append((tl[i], tl[i + 1]))
    return segs


def _local_narrate(per_seg, params):
    """省流 + 本地模型：用本地 qwen/ollama 等离线生成/改写每段解说词（0 元、不调云端）。
    返回 (lines, True)。无台词画面时依赖 params 里的 theme/name 给出像样文案。"""
    templates = [
        '镜头缓缓推进，故事就此展开。',
        '画面一转，新的转折正在发生。',
        '气氛渐起，关键情节悄然铺开。',
        '人物登场，冲突拉开了序幕。',
        '悬念浮现，让人忍不住屏息。',
        '节奏陡然加快，高潮正在靠近。',
        '真相逼近，谜底即将揭晓。',
        '余波未平，故事仍在继续。',
    ]
    theme = (params.get('theme') or '').strip()
    name = (params.get('name') or '').strip()
    brief = '\n'.join(
        f'[{s0:.1f}-{s1:.1f}s] {"台词:" + txt if txt else "(无台词画面)"}'
        for s0, s1, txt in per_seg)
    ctx = []
    if name:
        ctx.append('视频文件名：' + name)
    if theme:
        ctx.append('视频主题/梗概：' + theme)
    prompt = ('你是一个电影解说文案助手。下面是一段视频的分段时间轴与台词。\n'
              + ('补充信息：\n' + '\n'.join(ctx) + '\n' if ctx else '')
              + '请生成一段面向观众的连贯中文剧情解说稿：第 1 段开场引入剧情（不确定片名就用“故事从……”自然引入），'
                '后续每段承接上文叙述本段剧情事件（讲“发生了什么”，不要描述画面本身）。'
                '每段一句，30~60字，口播风格、有推进感，严格按顺序每段一行输出，不要编号、不要引号、不要解释。\n\n' + brief)
    text = local_llm_chat(prompt,
                          system='你是资深电影解说博主，擅长把剧情讲得生动有感染力，让观众想看下去。')
    lines = [l.strip().strip('"').strip() for l in text.splitlines() if l.strip()]
    if len(lines) < len(per_seg):
        for i, (s0, s1, txt) in enumerate(per_seg):
            if i < len(lines):
                continue
            lines.append(txt[:40] if txt else templates[i % len(templates)])
    return lines[:len(per_seg)], True


def generate_narration(segs, asr, params, frames=None):
    """返回 (narr_list, used_local)。
    离线(省流)优先级：本地 VLM(看图+台词+梗概→真解说) > 本地文本模型(台词改写) > 真实台词/模板。
    智能(云端)：有视觉端点则附画面描述，否则纯台词交给 DeepSeek 写解说。"""
    frames = frames or {}
    templates = [
        '镜头缓缓推进，故事就此展开。',
        '画面一转，新的转折正在发生。',
        '气氛渐起，关键情节悄然铺开。',
        '人物登场，冲突拉开了序幕。',
        '悬念浮现，让人忍不住屏息。',
        '节奏陡然加快，高潮正在靠近。',
        '真相逼近，谜底即将揭晓。',
        '余波未平，故事仍在继续。',
    ]
    # 每段聚合台词：按台词中点归到唯一镜头段（bisect 二分）。
    # 旧条件「整句须落在段内」会把骑跨段边界的台词两段都分不到而丢失，模型拿到的剧情信息变少。
    import bisect as _bisect
    seg_starts = [s0 for (s0, _s1) in segs]
    seg_txt = ['' for _ in segs]
    for x in asr:
        mid = (x['start'] + x['end']) / 2.0
        j = _bisect.bisect_right(seg_starts, mid) - 1
        j = max(0, min(len(segs) - 1, j))
        seg_txt[j] += (' ' if seg_txt[j] else '') + x['text']
    per_seg = [(s0, s1, t.strip()) for (s0, s1), t in zip(segs, seg_txt)]
    # 自动选路（不再区分省流/智能）：① 本地模型（免费）优先 ② 配置了云端 key 才用云端 ③ 台词/模板兜底
    if vlm_enabled():
        # ① 本地 VLM 真解说（看画面，从「复读」变「真解说」）
        try:
            return local_vlm_narrate(per_seg, frames, params)
        except Exception:
            pass
    if local_llm_enabled():
        # ② 本地文本模型改写（无画面理解）
        try:
            return _local_narrate(per_seg, params)
        except Exception:
            pass
    if not ai_enabled('chat'):
        # ③ 真实台词 / 模板兜底（未部署任何模型时保底出片）
        out = []
        for i, (s0, s1, txt) in enumerate(per_seg):
            out.append(txt[:40] if txt else templates[i % len(templates)])
        return out, False
    # ④ 云端 LLM 生成「连贯剧情解说稿」（在 AI 配置里填了 key = 明确同意使用；先整体理解剧情，再分段输出）
    try:
        import urllib.request, json as _json
        cfg = chat_cfg()
        use_vision = ai_enabled('vision')
        plot_ctx = ''
        if frames and use_vision:
            try:
                idxs = sorted(frames.keys())
                step = max(1, (len(idxs) + 4) // 5)
                descs = []
                for i in idxs[::step][:5]:
                    d = ai_describe_image(frames[i], '')
                    if d:
                        descs.append(d)
                if descs:
                    plot_ctx = '整体画面线索：' + ('；'.join(descs))
            except Exception:
                plot_ctx = ''
        lines_brief = []
        for i, (s0, s1, txt) in enumerate(per_seg):
            lines_brief.append(f'[{s0:.1f}-{s1:.1f}s] {"台词:" + txt if txt else "(无台词画面)"}')
        brief = '\n'.join(lines_brief)
        req_txt = (params.get('req') or '').strip()
        instr = ('下面是一段视频的分段时间轴、台词与画面线索。请生成一段面向观众的连贯中文电影解说稿'
                 '（剧情解说，不是画面描述）：\n'
                 '- 像真人解说一样把故事从头讲到尾、一气呵成：镜头之间自然衔接、层层递进'
                 '（可用“此时/紧接着/可没想到/而另一边/偏偏这时候”等承接），不要每段都另起炉灶；\n'
                 '- 第 1 段：开场引入剧情（若画面能确认影视作品，点出片名/年代/背景；不确定不要编造片名，'
                 '用“故事从……”/“镜头对准……”自然引入）；\n'
                 '- 后续每段：承接上文，叙述本段剧情本身（人物做了什么/事态怎么变），像讲故事；'
                 '除非这段真是剧情转折/高光，否则不要总结“这反映了/象征着/揭示了”这类意义升华；\n'
                 '- 详略有当：关键/转折/高光段展开讲（2~3 句），过渡/铺垫段一句带过，不要平均用力；\n'
                 '- 每段 20~120 字，口播风格、有推进感；台词转述、不要原样引用对话；'
                 '不编造剧情外事实；不堆“高潮/悬念/震撼”等空泛词；\n'
                 '- 严格按顺序每段一行输出，不要编号、不要引号、不要解释。\n\n')
        if req_txt:
            instr += '【额外要求】' + req_txt + '\n\n'
        payload = {
            'model': cfg.get('model'),
            'messages': [{'role': 'user', 'content': instr + (plot_ctx + '\n\n' if plot_ctx else '') + brief}],
            'max_tokens': 1800,
            'temperature': 0.8,
        }
        url = (cfg.get('base_url', '').rstrip('/')) + '/chat/completions'
        req = urllib.request.Request(url, data=_json.dumps(payload).encode('utf-8'),
                                     headers={'Content-Type': 'application/json',
                                              'Authorization': 'Bearer ' + cfg.get('api_key', '')})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = _json.loads(resp.read().decode('utf-8'))
        lines = [l.strip().strip('"').strip() for l in data['choices'][0]['message']['content'].splitlines() if l.strip()]
        if len(lines) >= len(per_seg):
            return lines[:len(per_seg)], False
        out = list(lines)
        for i in range(len(out), len(per_seg)):
            out.append(templates[i % len(templates)])
        return out, False
    except Exception:
        return [templates[i % len(templates)] for i in range(len(per_seg))], False


def _merge_segs(segs, max_keep=None):
    """合并过碎/过多镜头段到「剧情环节」。
    影视片段常被场景切分切成很多 4~8 秒的短镜头，逐段解说必然重复；
    按总时长均分成少量环节后，每环节一句解说、连贯推进，配合字幕/配音更接近真人电影解说。
    max_keep 缺省按时长自适应（约每 10 秒一个环节，4~14 个）：固定 ≤6 会让长视频
    一行解说扛 20~40 秒画面——字幕密度不够，且内容晚于段首出现，观感像"时间轴错位"。"""
    if not segs:
        return segs
    n = len(segs)
    if max_keep is None:
        max_keep = max(4, min(14, int(segs[-1][1] / 10)))
    if n <= max_keep:
        return segs
    vdur = segs[-1][1]
    target = vdur / max_keep
    merged = []
    cur_s, cur_e, acc = None, None, 0.0
    for s, e in segs:
        if cur_s is None:
            cur_s, cur_e, acc = s, e, (e - s)
        elif (acc + (e - s) <= target * 1.5) or (len(merged) + 1 >= max_keep):
            cur_e = e
            acc += (e - s)
        else:
            merged.append((cur_s, cur_e))
            cur_s, cur_e, acc = s, e, (e - s)
    if cur_s is not None:
        merged.append((cur_s, cur_e))
    return merged


def _narrate_analysis(video_path, params, run_dir, progress=None):
    """解说分析公共层：分段→合并环节→ASR台词→(可选)关键帧→解说稿。
    「人机协同分析(/api/plan)」与「直接生成解说(narrate_video)」共用此流程，
    避免两份逐行重复的实现各自漂移。返回 (segs, narr, asr, frames, mode)。"""
    def up(ph, pct):
        if progress:
            progress['phase'] = ph; progress['pct'] = pct
    up('场景分段', 4)
    segs = _segment_timeline(video_path, max_seg=float(params.get('maxSeg', 25)))
    if not segs:
        raise RuntimeError('无法分析视频时长')
    # 合并过碎镜头段为「剧情环节」（环节数按时长自适应，约 10s/环节），避免逐段解说重复、更贴近真人电影解说
    segs = _merge_segs(segs)
    up('识别台词(本地Whisper)', 10)
    asr = asr_segments(video_path)
    need_frames = vlm_enabled() or ai_enabled('vision')   # 任一视觉能力可用就抽帧（自动选路）
    frames = {}
    if need_frames:
        up('抽取关键帧(供视觉理解)', 16)
        frames = extract_segment_frames(video_path, segs, os.path.join(run_dir, 'frames'))
    up('生成解说稿', 20)
    narr, used_local = generate_narration(segs, asr, params, frames=frames)
    mode = None
    if vlm_enabled() and frames:
        mode = 'vlm'
    elif used_local:
        mode = 'local'
    return segs, narr, asr, frames, mode


def _analyze_narrate(video_path, params, run_dir, progress=None):
    """解说分析阶段：分段→ASR台词→解说稿→(可选)关键帧。返回 (segs, narr, asr, diag, mode)。
    拆出供「人机协同」复用：用户可在预览界面编辑每段解说词/删除段后再渲染。"""
    segs, narr, asr, frames, mode = _narrate_analysis(video_path, params, run_dir, progress)
    diag = {'segments': len(segs), 'asr_lines': len(asr), 'narration': narr}
    return segs, narr, asr, diag, mode


def _render_narrate(video_path, segs, narr, params, run_dir, progress=None, music_path=None, mode=None):
    """解说渲染阶段：按给定镜头段与解说稿逐段配音→混音→烧字幕→配乐。返回 final 路径。"""
    def up(ph, pct):
        if progress:
            progress['phase'] = ph; progress['pct'] = pct
    up('逐段配音', 30)
    tts_paths = []
    voice_spans = {}   # seg_idx -> (start, end)：字幕窗口跟随配音（有声才显字、念完即收）
    use_mimo = _tts_available()   # 自动：配置了云端 TTS key 即视为同意使用，否则免费 SAPI
    for i, txt in enumerate(narr):
        if _aborted():
            raise AbortError('用户取消了任务')
        if not (txt and txt.strip()):
            continue
        seg_span = segs[i] if i < len(segs) else (0.0, 10.0)
        clip = None
        if use_mimo:
            np_ = os.path.join(run_dir, f'narr{i}.mp3')
            if ai_tts(txt, np_):
                clip = np_
        if clip is None:
            wv = os.path.join(run_dir, f'narr{i}.wav')
            if sapi_tts(txt, wv):
                clip = wv
        if clip is not None:
            tts_paths.append((clip, seg_span[0], seg_span[1]))
            # 字幕只在「这句话正在被念」时显示：一行字挂满整个镜头段会让后段才发生的
            # 画面内容提前出现在段首，观感像字幕与时间轴错位
            v_len = probe_audio_len(clip) or max(0.5, seg_span[1] - seg_span[0])
            voice_spans[i] = (seg_span[0], min(seg_span[1], seg_span[0] + v_len + 0.35))
    up('混音+烧字幕+配乐', 60)
    final = _compose_narration_video(video_path, segs, narr, tts_paths, run_dir, params,
                                     music_path=music_path, voice_spans=voice_spans)
    if progress:
        progress['done'] = True
        progress['pct'] = 100
        progress['file'] = os.path.relpath(final, OUTDIR).replace('\\', '/')
        if mode:
            progress['mode'] = mode
    return final, len(tts_paths)


def narrate_video(video_path, params, run_dir, progress=None, music_path=None):
    """电影解说主流程：分段→ASR→解说稿→SAPI/MiMo配音→混音→字幕→成片。
    music_path: 可选背景音乐，混入成品（按 Phase 2「配乐」要求）。"""
    segs, narr, asr, frames, mode = _narrate_analysis(video_path, params, run_dir, progress)
    if progress and mode:
        progress['mode'] = mode
    final, vc = _render_narrate(video_path, segs, narr, params, run_dir, progress=progress,
                                music_path=music_path, mode=progress.get('mode') if progress else None)
    diag = {'segments': len(segs), 'asr_lines': len(asr), 'voice_clips': vc,
            'narration': narr}
    return final, diag
def _has_audio_track(p):
    """返回视频文件是否含音轨。"""
    rc, o, e = ffmpeg_run(['-i', p])
    return 'Audio:' in e.decode('utf-8', 'ignore')


def _clean_caption(text):
    """清洗单条字幕文案：去首尾空白/引号、把内部换行替换为空格、合并多余空格。
    LLM/模板输出偶尔带换行或引号，若原样写入 SRT 会破坏字幕时间轴格式。"""
    if not text:
        return ''
    import re as _re
    t = _re.sub(r'\s+', ' ', str(text)).strip()
    # 去掉首尾成对的引号（含中文弯引号）
    for a, b in (('"', '"'), ("'", "'"), ('“', '”'), ('‘', '’')):
        if len(t) >= 2 and t[0] == a and t[-1] == b:
            t = t[1:-1].strip()
    return t


def _compose_narration_video(video_path, segs, narr, tts_paths, run_dir, params, music_path=None,
                             voice_spans=None):
    """把解说配音按时间轴混入原视频，烧录解说字幕，可选叠加背景音乐，输出 final.mp4。
    - narr[i] 对应 segs[i]（镜头段时间轴），作为该段字幕与配音文案。
    - tts_paths: [(audio_path, start_sec)]，按各自起始时间对齐到时间轴。
    - voice_spans: 可选 {seg_idx: (start,end)}，字幕窗口跟随配音（有声才显字、念完即收）；
      缺省整段显示（兼容旧行为）。
    - music_path: 可选 BGM，循环铺底、低音量。"""
    vdur = probe_audio_len(video_path) or 10.0
    # 1) 烧字幕：解说词按段显示；有配音时间窗时字随声走
    srt = os.path.join(run_dir, 'narr.srt')
    with open(srt, 'w', encoding='utf-8') as f:
        def ts(sec):
            hh = int(sec // 3600); mm = int((sec % 3600) // 60); ss = int(sec % 60); ms = int((sec % 1) * 1000)
            return f'{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}'
        seq = 0
        for i, (s0, s1) in enumerate(segs):
            cap = _clean_caption(narr[i] if i < len(narr) else '')
            if not cap:
                continue
            w0, w1 = s0, s1
            vs = (voice_spans or {}).get(i)
            if vs:
                w0 = max(s0, vs[0])
                w1 = min(s1, max(vs[1], w0 + 0.8))   # 至少显示 0.8s，避免一闪而过
            seq += 1
            f.write(f'{seq}\n{ts(w0)} --> {ts(w1)}\n{cap}\n\n')
    esc = srt.replace('\\', '/').replace(':', '\\:')
    vsub = os.path.join(run_dir, 'vsub.mp4')
    rc, o, e = ffmpeg_run(['-y', '-i', video_path,
                           '-vf', f"subtitles='{esc}':force_style='FontName=Microsoft YaHei,FontSize=22,Alignment=2,MarginV=50'",
                           ] + video_encode_args() + ['-threads', '0', '-an', vsub])
    base_video = vsub if (rc == 0 and os.path.exists(vsub)) else video_path

    has_orig_audio = _has_audio_track(video_path)
    use_music = bool(music_path) and os.path.exists(music_path or '')

    # 没有任何音频需要混：直接保留画面（含字幕）
    if not tts_paths and not has_orig_audio and not use_music:
        final = os.path.join(run_dir, 'final.mp4')
        cmd = ['-y', '-i', base_video, '-c:v', 'copy', '-an', '-movflags', '+faststart', final]
        rc, o, e = ffmpeg_run(cmd)
        if rc != 0:
            raise RuntimeError('成片失败: ' + e.decode('utf-8', 'ignore')[-300:])
        return final

    # 构建音频滤镜图
    # tts_paths 元素：2 元组 (audio, start) 或 3 元组 (audio, start, end)，后者用于把配音裁剪在本镜头段内，
    # 避免配音时长超过镜头段导致的跨段语音重叠。
    def _tt_span(item):
        if len(item) >= 3:
            return item[0], item[1], item[2]
        return item[0], item[1], None

    inputs = ['-y', '-i', base_video]   # 0 = 字幕视频(画面)
    fparts = []
    mixin = ''
    next_idx = 1                        # 下一个音频输入索引（显式记录，避免 count("-i") 脆弱）
    if has_orig_audio:
        inputs += ['-i', video_path]    # 1 = 原视频(音频)
        if tts_paths:
            # 解说配音存在时做 ducking：解说段内原声压到 0.08，缝隙还原 0.5，杜绝双声重叠
            expr = '0.5'
            for item in tts_paths:
                np_, od, _oe = _tt_span(item)
                dur = probe_audio_len(np_) or 3.0
                expr = "if(between(t,%.2f,%.2f),0.08,%s)" % (od, od + dur, expr)
            # 注意：表达式含逗号，必须用单引号包裹，否则 ffmpeg 会把逗号当作滤镜链分隔符导致解析失败
            fparts.append("[1:a]volume='%s':eval=frame[orig]" % expr)
        else:
            fparts.append('[1:a]volume=0.5[orig]')
        mixin = '[orig]'
        next_idx += 1
    for k2, item in enumerate(tts_paths):
        np_, od, oe = _tt_span(item)
        inputs += ['-i', np_]
        if oe is not None and oe > od:
            # 限制配音时长不超过本镜头段，杜绝「这段解说拖到下一段画面」的重叠
            dmax = max(0.05, oe - od)
            fparts.append(f'[{next_idx + k2}:a]aresample=44100,adelay={int(od * 1000)}|{int(od * 1000)},'
                          f'atrim=0:{dmax:.2f},apad=whole_dur={vdur:.2f}[t{k2}]')
        else:
            fparts.append(f'[{next_idx + k2}:a]aresample=44100,adelay={int(od * 1000)}|{int(od * 1000)},'
                          f'apad=whole_dur={vdur:.2f}[t{k2}]')
        mixin += f'[t{k2}]'
    if use_music:
        # 背景乐：单次输入 + atrim 截到视频时长 + apad 补到视频时长。
        # 不要用 `-stream_loop -1` 无限循环：apad(whole_dur) 需要读到输入 EOF 才会输出，
        # 无限循环流没有 EOF，会导致 ffmpeg 永久挂起（实测解说+配乐必卡死）。
        inputs += ['-i', music_path]
        fparts.append(f'[{next_idx + len(tts_paths)}:a]aresample=44100,volume=0.16,'
                      f'atrim=0:{vdur:.2f},apad=whole_dur={vdur:.2f}[bgm]')
        mixin += '[bgm]'
    n_mix = len(tts_paths) + (1 if has_orig_audio else 0) + (1 if use_music else 0)
    # amix 前统一采样率/声道，避免不同来源（SAPI 22k mono / 云端 TTS 24-48k / 原声 44.1k stereo）混音异常或音量失衡
    fparts.append(f'{mixin}amix=inputs={n_mix}:normalize=0,aresample=44100,aformat=channel_layouts=stereo,'
                  f'aformat=fltp[aout]')
    final = os.path.join(run_dir, 'final.mp4')
    cmd = inputs + ['-filter_complex', ';'.join(fparts), '-map', '0:v:0', '-map', '[aout]',
                    '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', final]
    rc, o, e = ffmpeg_run(cmd)
    if rc != 0:
        # 兜底：丢弃配音，仅保留原声/画面的简单封装
        fb = os.path.join(run_dir, 'final.mp4')
        rc2, o2, e2 = ffmpeg_run(['-y', '-i', base_video, '-c:v', 'copy', '-c:a', 'aac', '-b:a', '160k', fb])
        if rc2 == 0 and os.path.exists(fb):
            return fb
        raise RuntimeError('混音失败: ' + e.decode('utf-8', 'ignore')[-300:])
    return final


# ---------------------------------------------------------------------------
# Phase 3 · 联网搜索 + 全自动剧情解说
# ---------------------------------------------------------------------------
def web_search(query, max_results=6):
    """免费联网搜索（多源容灾：百度/必应优先，DuckDuckGo 兜底，无需 API Key）。
    返回 [(title, snippet, url)]，失败返回 []。任一源成功即返回，避免单一源被墙导致整功能挂掉。"""
    import urllib.request, urllib.parse, re as _re
    q = urllib.parse.quote(query)
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
               'Accept-Language': 'zh-CN,zh;q=0.9'}
    _strip = lambda h: _re.sub(r'<[^>]+>', '', h or '').strip()

    def _get(url):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode('utf-8', 'ignore')
        except Exception:
            return ''

    def _parse_baidu(html):
        out = []
        # 新版百度：每个结果是一个 c-container 容器；标题在容器内 h3>a，摘要在 c-abstract / content-right
        blocks = [b for b in _re.split(r'<div[^>]*class="[^"]*result[^"]*c-container', html)][1:]
        for blk in blocks:
            mh = _re.search(r'<h3[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', blk, _re.S)
            if not mh:
                continue
            url, title = mh.group(1), _strip(mh.group(2))
            if not title:
                continue
            ma = (_re.search(r'class="c-abstract"[^>]*>(.*?)</(?:div|span)>', blk, _re.S)
                  or _re.search(r'class="content-right[^"]*"[^>]*>(.*?)</span>', blk, _re.S))
            snip = _strip(ma.group(1)) if ma else ''
            if not snip:
                # 摘要有时以 c-span-len / 富摘要形式出现，兜底取容器内 p 文本
                mp = _re.search(r'<p[^>]*>(.*?)</p>', blk, _re.S)
                snip = _strip(mp.group(1)) if mp else ''
            out.append((title, snip, url))
        return out

    def _parse_bing(html):
        out = []
        blocks = _re.findall(r'<li class="b_algo"[^>]*>(.*?)</li>', html, _re.S)
        for b in blocks:
            m = _re.search(r'<h2><a[^>]*href="([^"]+)"[^>]*>(.*?)</a></h2>', b, _re.S)
            if not m:
                continue
            url, title = m.group(1), _strip(m.group(2))
            p = _re.search(r'<p[^>]*>(.*?)</p>', b, _re.S)
            snip = _strip(p.group(1)) if p else ''
            if title:
                out.append((title, snip, url))
        return out

    def _parse_ddg(html):
        out = []
        titles = _re.findall(r'class="result-link"[^>]*>(.*?)</a>', html, _re.S)
        if not titles:
            titles = _re.findall(r'class="result__a"[^>]*>(.*?)</a>', html, _re.S)
        for t in titles:
            out.append((_strip(t), '', ''))
        snippets = _re.findall(r'class="result-snippet"[^>]*>(.*?)</td>', html, _re.S)
        if not snippets:
            snippets = _re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, _re.S)
        for i in range(len(out)):
            if i < len(snippets):
                out[i] = (out[i][0], _strip(snippets[i]), out[i][2])
        return out

    # 大陆优先百度/必应；境外 DuckDuckGo 兜底。任一源成功即用，保证可用性。
    sources = [
        ('baidu', f'https://www.baidu.com/s?wd={q}&rn=10', _parse_baidu),
        ('bing', f'https://www.bing.com/search?q={q}&setlang=zh-CN', _parse_bing),
        ('ddg', f'https://lite.duckduckgo.com/lite/?q={q}', _parse_ddg),
        ('ddg', f'https://html.duckduckgo.com/html/?q={q}', _parse_ddg),
    ]
    try:
        for name, url, parser in sources:
            html = _get(url)
            if not html:
                continue
            res = parser(html)
            if res:
                seen, uniq = set(), []
                for t, s, u in res:
                    if t and t not in seen:
                        seen.add(t); uniq.append((t, s, u))
                if uniq:
                    return uniq[:max_results]
    except Exception:
        pass
    return []


def _char_bigrams(s):
    """取中文字符的二元组集合（粗粒度语义指纹），用于解说事件↔台词匹配。"""
    s = ''.join(ch for ch in s if '\u4e00' <= ch <= '\u9fff')
    return set(s[i:i + 2] for i in range(len(s) - 1))


def llm_movie_script(movie_name, plot_text, economy=False):
    """根据片名 + 剧情文本，让 LLM 产出结构化解说事件列表 [{desc, keywords}]。
    兜底优先级（保证离线/断网也能出稿，绝不因缺剧情而空返回）：
      剧情切句 → 本地模型(若可用) → 片名模板。"""
    import re as _re
    def _split_sentences(text):
        lines = [l.strip() for l in _re.split(r'[\n。！？!?]', text or '') if len(l.strip()) > 4]
        return [{'desc': l[:30], 'keywords': list(set(l))[:5]} for l in lines[:12]]

    def _parse_events(content):
        """从 LLM 文本中提取并规范化 JSON 解说事件数组；失败返回 []。"""
        import json as _json
        s = content.find('['); e = content.rfind(']')
        if s < 0 or e <= s:
            return []
        try:
            arr = _json.loads(content[s:e + 1])
        except Exception:
            return []
        out = []
        for it in arr:
            if isinstance(it, dict) and it.get('desc'):
                kw = it.get('keywords') or []
                if isinstance(kw, str):
                    kw = [kw]
                out.append({'desc': str(it['desc'])[:40], 'keywords': [str(k) for k in kw][:6]})
        return out

    def _local_script(name, plot):
        """本地模型(Ollama 等)离线生成解说事件：断网也能用，失败返回 []。"""
        if not local_llm_enabled():
            return []
        try:
            if not local_llm_ping()[0]:
                return []
        except Exception:
            return []
        brief = ((name or '') + '\n' + (plot or ''))[:4000]
        prompt = ('你是电影/动漫解说编剧。下面是一部作品的片名'
                  + ('与剧情梗概。' if plot else '（没有剧情资料，请根据片名合理发挥）。') + '\n'
                  '请输出一个 JSON 数组，每条解说事件形如 {"desc":"一句≤30字中文解说词",'
                  '"keywords":["关键词1","关键词2"]}，按时间顺序覆盖主要情节，共 6-12 条。'
                  '只输出 JSON，不要解释、不要代码块标记。\n\n' + brief)
        try:
            return _parse_events(local_llm_chat(prompt, timeout=90))
        except Exception:
            return []

    def _template_script(name):
        """最终兜底：基于片名生成通用解说事件（0 元、0 依赖，保证断网也能出稿）。"""
        name = (name or '').strip()
        if name:
            lines = [
                '今天要讲的这部电影是《%s》。' % name,
                '故事从一场不寻常的相遇悄然展开。',
                '主角登场，命运的齿轮开始转动。',
                '平静之下暗流涌动，冲突一触即发。',
                '转折来临，局面陡然扑朔迷离。',
                '悬念层层叠加，让人屏住呼吸。',
                '高潮将至，所有线索开始交汇。',
                '真相浮出水面，结局出人意料。',
            ]
        else:
            lines = [
                '今天要讲的这部电影，故事从一场不寻常的相遇悄然展开。',
                '主角登场，命运的齿轮开始转动。',
                '平静之下暗流涌动，冲突一触即发。',
                '转折来临，局面陡然扑朔迷离。',
                '悬念层层叠加，让人屏住呼吸。',
                '高潮将至，所有线索开始交汇。',
                '真相浮出水面，结局出人意料。',
            ]
        return [{'desc': l[:30], 'keywords': list(set(l))[:5]} for l in lines]

    # 省流模式：直接离线切句，不调任何付费接口
    if economy or not ai_enabled('chat'):
        ev = _split_sentences(plot_text)
        if ev:
            return ev
        # 无剧情文本时：优先本地模型(若有) → 片名模板，保证不空
        return _local_script(movie_name, plot_text) or _template_script(movie_name)
    brief = ((movie_name or '') + '\n' + (plot_text or ''))[:4000]
    prompt = ('你是电影/动漫解说编剧。下面是一部作品的片名与剧情梗概。\n'
              '请输出一个 JSON 数组，每条解说事件形如 {"desc":"一句≤30字中文解说词",'
              '"keywords":["关键词1","关键词2"]}，按时间顺序覆盖主要情节，共 6-12 条。'
              '只输出 JSON，不要解释、不要代码块标记。\n\n' + brief)
    try:
        import urllib.request, json as _json
        cfg = chat_cfg()
        payload = {'model': cfg.get('model'),
                   'messages': [{'role': 'user', 'content': prompt}],
                   'max_tokens': 1500, 'temperature': 0.7}
        url = (cfg.get('base_url', '').rstrip('/')) + '/chat/completions'
        req = urllib.request.Request(url, data=_json.dumps(payload).encode('utf-8'),
                                     headers={'Content-Type': 'application/json',
                                              'Authorization': 'Bearer ' + cfg.get('api_key', '')})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = _json.loads(resp.read().decode('utf-8'))
        ev = _parse_events(data['choices'][0]['message'].get('content', ''))
        if ev:
            return ev
    except Exception:
        pass
    # 兜底：剧情切句 → 本地模型 → 片名模板（保证离线也能出稿）
    ev = _split_sentences(plot_text)
    if ev:
        return ev
    return _local_script(movie_name, plot_text) or _template_script(movie_name)


def align_script_to_segments(events, segs, asr):
    """把解说事件对齐到电影镜头段：时序保持的加权匹配（核心技术难点）。
    - 事件按剧情顺序、镜头段按时间顺序，单调分配：一旦事件 k 命中段 i，事件 k+1 只在段 > i 中找，
      避免「第3段解说词错配到更早画面」的乱序问题。
    - 匹配分 = 台词字符 bigram Jaccard；未命中阈值的事件按顺序补到空闲段，保证全覆盖。
    返回 [(start, desc)]（按 start 升序）。"""
    seg_text = []
    for (s0, s1) in segs:
        txt = ' '.join(x['text'] for x in asr if x['start'] >= s0 - 0.5 and x['end'] <= s1 + 0.5)
        seg_text.append(txt)
    seg_bg = [_char_bigrams(t) for t in seg_text]

    def _score(qbg, bg):
        if not qbg or not bg:
            return 0.0
        return len(qbg & bg) / (len(qbg | bg) or 1)

    assigned = []          # [(seg_index, desc)]
    used = set()
    seg_ptr = 0            # 单调指针：后续事件只在 seg_ptr 之后找
    pending = []           # 未命中的事件，留作补位
    for ev in events:
        q = (ev.get('desc', '') + ' ' + ' '.join(ev.get('keywords') or []))
        qbg = _char_bigrams(q)
        best_i, best_score = -1, 0.0
        for i in range(seg_ptr, len(segs)):
            if i in used:
                continue
            sc = _score(qbg, seg_bg[i])
            if sc > best_score:
                best_score, best_i = sc, i
        if best_i >= 0 and best_score > 0.04:
            used.add(best_i)
            assigned.append((best_i, ev.get('desc', '')))
            seg_ptr = best_i + 1
        else:
            pending.append(ev)
    # 补位：未命中的事件按序填进剩余空闲段
    free = [i for i in range(len(segs)) if i not in used]
    fi = 0
    for ev in pending:
        if fi < len(free):
            i = free[fi]; fi += 1
            assigned.append((i, ev.get('desc', '')))
    # 按镜头段出现顺序输出
    assigned.sort(key=lambda x: x[0])
    return [(segs[i][0], desc) for i, desc in assigned]


def narrate_movie(movie_name, plot, video_path, params, run_dir, progress=None, music_path=None):
    """Phase 3 主流程：联网搜索剧情 → LLM 生成解说稿 → (上传电影时)ASR+语义对齐 → 配音+字幕+配乐成片。
    未上传视频时只产出解说稿（progress['script']）。"""
    def up(ph, pct):
        if progress:
            progress['phase'] = ph; progress['pct'] = pct
    up('联网搜索剧情', 4)
    if not plot:
        hits = web_search((movie_name or '') + ' 剧情 简介 豆瓣 梗概 分幕')
        plot = '\n'.join(t for t, _, _ in hits) or ''
    up('LLM 生成解说稿', 14)
    # 省流优先：默认 economy=True（离线切句/模板 0 元），只有显式「真AI」才调用付费 LLM
    events = llm_movie_script(movie_name, plot, economy=not ai_enabled('chat'))
    if not events:
        raise RuntimeError('无法生成解说稿（请检查网络，或在指令里粘贴剧情文本）')
    if not video_path:
        if progress:
            progress['done'] = True; progress['pct'] = 100
            progress['file'] = ''; progress['script'] = events
            progress['mode'] = compute_mode(params, needs_chat=True)
        return None, {'events': events, 'no_video': True}
    up('场景分段', 22)
    segs = _segment_timeline(video_path, max_seg=float(params.get('maxSeg', 25)))
    if not segs:
        raise RuntimeError('无法分析视频时长')
    up('识别台词(本地Whisper)', 32)
    asr = asr_segments(video_path)
    up('解说↔片段对齐', 46)
    aligned = align_script_to_segments(events, segs, asr)
    # 未匹配事件按顺序补到空闲镜头段，保证覆盖面
    taken = {s for s, _ in aligned}
    free = [i for i in range(len(segs)) if segs[i][0] not in taken]
    extra = []
    for ev in events:
        if any(ev['desc'] == d for _, d in aligned):
            continue
        if free:
            extra.append((segs[free.pop(0)][0], ev['desc']))
    aligned = sorted(aligned + extra, key=lambda x: x[0])
    # 映射到镜头段索引（保证字幕/配音与 segs 对齐）
    seg_narr = [''] * len(segs)
    for (start, desc) in aligned:
        for i, (s0, s1) in enumerate(segs):
            if abs(s0 - start) < 0.01:
                seg_narr[i] = desc; break
    narr = seg_narr
    up('逐段配音', 58)
    tts_paths = []
    voice_spans = {}   # 字幕窗口跟随配音（与短片解说一致：有声才显字、念完即收）
    use_mimo = _tts_available()   # 自动：配置了云端 TTS key 即视为同意使用
    for i, txt in enumerate(narr):
        if not txt.strip():
            continue
        seg_span = segs[i] if i < len(segs) else (0.0, 10.0)
        clip = None
        if use_mimo:
            np_ = os.path.join(run_dir, f'narr{i}.mp3')
            if ai_tts(txt, np_):
                clip = np_
        if clip is None:
            wv = os.path.join(run_dir, f'narr{i}.wav')
            if sapi_tts(txt, wv):
                clip = wv
        if clip is not None:
            tts_paths.append((clip, seg_span[0], seg_span[1]))
            v_len = probe_audio_len(clip) or max(0.5, seg_span[1] - seg_span[0])
            voice_spans[i] = (seg_span[0], min(seg_span[1], seg_span[0] + v_len + 0.35))
    up('混音+烧字幕+配乐', 70)
    final = _compose_narration_video(video_path, segs, narr, tts_paths, run_dir, params,
                                     music_path=music_path, voice_spans=voice_spans)
    if progress:
        progress['done'] = True; progress['pct'] = 100
        progress['file'] = os.path.relpath(final, OUTDIR).replace('\\', '/') if final else ''
        progress['script'] = events
        progress['mode'] = compute_mode(params, needs_chat=True)
    diag = {'events': len(events), 'segments': len(segs), 'asr_lines': len(asr),
            'aligned': len(aligned), 'voice_clips': len(tts_paths), 'narration': narr}
    return final, diag


# ---------------------------------------------------------------------------
# Phase 4 · 指令解析层（自然语言 → 路由到对应工作流）
# ---------------------------------------------------------------------------
def parse_instruction(text, ctx=None):
    """解析自然语言指令 → {action, params, movie, note}。action ∈ beatcut|narrate|movie|compose。"""
    import re as _re
    t = (text or '').strip()
    tl = t.lower()
    ctx = ctx or {}
    action = 'compose'
    params = {}
    movie = ''
    m = _re.search(r'《([^》]+)》', t)
    if m:
        movie = m.group(1)
    # 联网搜索/按片名解说 → movie 工作流；本地短片解说（有《》但更可能是上传视频）→ 仍走 movie 以利用联网梗概
    if movie or any(k in tl for k in ('联网', '搜索', '网上', '剧情梗概', '查一下', '简介')):
        action = 'movie'
    elif '解说' in t or '剧情' in t or '旁白' in t or 'narration' in tl:
        action = 'narrate'
    elif any(k in tl for k in ('卡点', '踩点', '强卡点', '配乐', '鼓点', '剪一段', '混剪', '卡点视频')):
        action = 'beatcut'
    else:
        action = 'compose'
    # 模式：省流(免费) vs 真AI(花钱)
    if any(k in tl for k in ('省流', '免费', '0元', '不花钱', '离线', '模板')):
        params['economy'] = True
    elif any(k in tl for k in ('真ai', '智能', '花钱', '付费', 'ai解说')):
        params['economy'] = False
    # 分辨率
    if any(k in tl for k in ('竖屏', '抖音', '快手', '9:16')):
        params['w'], params['h'] = 1080, 1920
    elif '横屏' in tl:
        params['w'], params['h'] = 1920, 1080
    # 携带音乐/最大分段等上下文
    if ctx.get('music'):
        params['music'] = ctx['music']
    for k in ('maxSeg', 'fps', 'mode'):
        if k in ctx:
            params[k] = ctx[k]
    return {'action': action, 'params': params, 'movie': movie, 'note': []}


def _resolve_music(music_data):
    """把 req 的 music 字段解析为本地路径（catalog 下载 / base64 上传）。无则返回 None。"""
    if not music_data:
        return None
    try:
        if music_data.get('source') == 'catalog':
            return download_catalog(music_data.get('catalogId', ''))
        if music_data.get('data'):
            mdata = base64.b64decode(music_data.get('data', ''))
            mname = music_data.get('name', 'music.mp3')
            mpath = os.path.join(WORKDIR, 'music_' + str(int(time.time() * 1000)) +
                                 (os.path.splitext(mname)[1] or '.mp3'))
            os.makedirs(WORKDIR, exist_ok=True)
            open(mpath, 'wb').write(mdata)
            return mpath
    except Exception:
        return None
    return None


def dispatch_build(req, prog):
    """通用合成（图片/视频混排 + 节拍对齐）。与 /api/build 共用。"""
    try:
        params = req.get('params', {})
        items = req.get('items', [])
        music_path = _resolve_music(req.get('music'))
        work = []
        for idx, it in enumerate(items):
            if it['kind'] == 'image':
                ext = os.path.splitext(it.get('name', 'x.jpg'))[1] or '.jpg'
                fp = os.path.join(WORKDIR, f'up_{len(work)}_{idx}_img{ext}')
                os.makedirs(WORKDIR, exist_ok=True)
                if it.get('mlib'):
                    msrc = _material_path(it.get('mlib'))
                    if not msrc:
                        raise RuntimeError('素材库中找不到 %s，请刷新素材库' % it.get('mlib'))
                    shutil.copy2(msrc, fp)
                else:
                    data = base64.b64decode(it.get('data', ''))
                    open(fp, 'wb').write(data)
                # name 保留用户原始文件名：省流文案直接对用户展示，不能露出 up_N 内部名
                work.append({'kind': 'image', 'src': fp, 'name': it.get('name', ''), 'dur': it.get('dur', 3), 'motion': len(work) % 4})
            else:
                # 视频素材两种形态：base64（小文件）或分片上传 upload_id（大文件，直接 move 免二次拷贝）
                fp = os.path.join(WORKDIR, f'up_{len(work)}_{idx}_vid.mp4')
                os.makedirs(WORKDIR, exist_ok=True)
                src = _resolve_upload_video(it, WORKDIR, f'up_{len(work)}_{idx}_vid')
                if src is None:
                    raise RuntimeError('素材 %s 缺少数据（data/upload_id），请重新上传' % (it.get('name') or idx))
                work.append({'kind': 'video', 'src': src, 'name': it.get('name', ''), 'dur': it.get('dur', 3)})
        if not work:
            defaults = ensure_default_images()
            single = params.get('singleDur', 3) or 3
            for i, p in enumerate(defaults):
                work.append({'kind': 'image', 'src': p, 'dur': single, 'motion': i})
        captions = None
        if params.get('ai_captions') and work:
            prog['phase'] = '按画面生成文案'
            prog['pct'] = 4
            economy = not ai_enabled('vision')   # 自动：配置了云端视觉 key 用 AI 文案，否则离线模板
            captions = []
            for w_ in work:
                if economy:
                    cap = offline_caption(w_.get('name') or w_.get('src', ''), 0, len(work))
                else:
                    cap = ai_describe_image(w_['src'], w_.get('src', ''))
                captions.append(cap)
            params['economy'] = economy
        vid, total_len, beat_info = assemble(work, params, music_path, prog, run_dir=prog.get('run_dir'))
        final = finalize(vid, params, music_path, captions,
                         (beat_info or {}).get('durations'), prog)
        prog['done'] = True
        prog['pct'] = 100
        prog['file'] = os.path.relpath(final, OUTDIR).replace('\\', '/')
        prog['duration'] = round(float(total_len), 2)
        prog['beat'] = beat_info
        prog['captions'] = captions
        prog['mode'] = 'ai' if (params.get('ai_captions') and ai_enabled('vision')) else 'free'
        try:
            add_history({
                'time': time.strftime('%Y-%m-%d %H:%M:%S'),
                'file': prog['file'], 'duration': prog['duration'],
                'music': (req.get('music') or {}).get('name') if isinstance(req.get('music'), dict) else None,
                'voice': bool(captions and _tts_available()), 'captions': captions,
                'mode': prog['mode'],
                'w': params.get('w', W), 'h': params.get('h', H), 'fps': params.get('fps', 30),
            })
        except Exception:
            pass
    except Exception as e:
        import traceback
        traceback.print_exc()
        prog['done'] = True
        prog['error'] = str(e)
        if prog.get('run_dir'):
            try:
                prog['partial'] = collect_partial(prog['run_dir'])
            except Exception:
                pass


def _plan_thumbs(video_path, segs, run_dir, max_side=220):
    """为每个镜头段抽一张中间帧缩略图(jpg)，用于人机协同预览。返回 {idx: 绝对路径}。"""
    try:
        return extract_segment_frames(video_path, segs, os.path.join(run_dir, 'thumbs'), max_side=max_side)
    except Exception:
        return {}


def _plan_to_ui(plan, run_dir):
    """把内部 plan 转成前端可渲染的 JSON（缩略图换成 /media 相对路径、不含二进制）。"""
    rel = lambda p: (os.path.relpath(p, OUTDIR).replace('\\', '/') if p and os.path.exists(p) else '')
    ui = {'type': plan['type'], 'run_dir': os.path.basename(run_dir)}
    if plan['type'] == 'beatcut':
        tl = plan['timeline']
        ui['vdur'] = plan['vdur']
        ui['segs'] = []
        for i in range(len(tl) - 1):
            ui['segs'].append({'i': i, 'start': tl[i], 'end': tl[i + 1],
                               'thumb': rel(plan.get('thumbs', {}).get(i))})
        ui['cuts'] = [{'t': round(t, 3)} for t in tl[1:-1]]
    else:  # narrate
        ui['mode'] = plan.get('mode')
        ui['segs'] = []
        for i, (s0, s1) in enumerate(plan['segs']):
            ui['segs'].append({'i': i, 'start': s0, 'end': s1,
                               'caption': plan['narr'][i] if i < len(plan['narr']) else '',
                               'thumb': rel(plan.get('thumbs', {}).get(i))})
    return ui


def _analyze_plan_job(req, prog):
    """人机协同·分析阶段：分析素材生成「规划方案」，存 PLANS，等待用户确认/微调后渲染。"""
    try:
        run_dir = prog['run_dir']
        ptype = req.get('type') or 'beatcut'
        params = req.get('params') or {}
        # 视频两种形态：base64（小文件）或分片上传/素材库引用（长视频走这两者——此前漏接导致长视频分析预览报错）
        vobj = req.get('video') or {}
        vp = _resolve_upload_video(vobj, run_dir, 'src_video')
        had_video = bool(vobj.get('data') or vobj.get('upload_id') or vobj.get('mlib'))
        if had_video and not vp:
            raise RuntimeError('视频读取失败（上传会话可能已过期，请重新分析）')
        if ptype == 'beatcut':
            if not vp:
                raise RuntimeError('请先上传视频')
            music_path = _resolve_music(req.get('music'))
            if not music_path:
                raise RuntimeError('请先选择背景音乐')
            timeline, diag, vdur = _analyze_beatcut(vp, music_path, params, prog)
            segs = [(timeline[i], timeline[i + 1]) for i in range(len(timeline) - 1)]
            plan = {'type': 'beatcut', 'video': vp, 'music': music_path, 'timeline': timeline,
                    'vdur': vdur, 'params': params, 'diag': diag,
                    'thumbs': _plan_thumbs(vp, segs, run_dir)}
        elif ptype == 'narrate':
            if not vp:
                raise RuntimeError('请先上传视频')
            segs, narr, asr, diag, mode = _analyze_narrate(vp, params, run_dir, prog)
            music_path = _resolve_music(req.get('music'))
            plan = {'type': 'narrate', 'video': vp, 'segs': segs, 'narr': narr,
                    'params': params, 'music': music_path, 'diag': diag, 'mode': mode,
                    'thumbs': _plan_thumbs(vp, segs, run_dir)}
        else:
            raise RuntimeError('未知分析类型: ' + str(ptype))
        PLANS[prog['runid']] = plan
        # 用户可能分析后不确认：只保留最近 30 个方案，防止长驻进程内存增长
        if len(PLANS) > 30:
            for k in list(PLANS.keys())[:-30]:
                PLANS.pop(k, None)
        prog['plan_ready'] = True
        prog['plan'] = _plan_to_ui(plan, run_dir)
        prog['phase'] = '规划完成，请在下方微调后点击「按我的调整合成」'
        prog['pct'] = 100
    except Exception as e:
        import traceback
        traceback.print_exc()
        prog['done'] = True
        prog['error'] = str(e)


def _render_plan_job(req, prog):
    """人机协同·渲染阶段：按用户微调后的方案（编辑过的切点/解说稿）合成成片。"""
    try:
        # plan 挂在「分析阶段」的旧 runid 上；confirm 请求透传了该 runid
        src_runid = req.get('runid') or prog['runid']
        plan = PLANS.get(src_runid)
        if not plan:
            raise RuntimeError('规划方案不存在或已过期，请重新分析')
        params = dict(plan.get('params') or {})
        extra = req.get('params') or {}
        if extra:
            params.update(extra)
        edits = req.get('edits') or {}
        if plan['type'] == 'beatcut':
            vdur = plan['vdur']
            seg_edits = edits.get('segs') or []
            tl2 = [0.0]
            if seg_edits:
                # 由每段的保留开关 + 段尾时间重建切点（末段 end=vdur 不是切点）；未传 on 默认保留
                for s in seg_edits:
                    if s.get('on', True):
                        t = round(float(s.get('end', 0)), 3)
                        if 0.3 < t < vdur - 0.3 and t - tl2[-1] >= 0.8:
                            tl2.append(t)
            else:
                for c in (edits.get('cuts') or []):
                    if c.get('on', True):
                        t = round(float(c['t']), 3)
                        if 0.3 < t < vdur - 0.3 and t - tl2[-1] >= 0.8:
                            tl2.append(t)
            tl2 = sorted(set(tl2))
            if vdur - tl2[-1] < 0.4:
                tl2[-1] = vdur
            else:
                tl2.append(vdur)
            tl2 = sorted(set(tl2))
            final = _render_beatcut(plan['video'], plan['music'], tl2, params, prog['run_dir'],
                                    prog, diag=None)
        elif plan['type'] == 'narrate':
            segs, narr = [], []
            for s in (edits.get('segs') or []):
                if not s.get('on', True):
                    continue
                segs.append((float(s['start']), float(s['end'])))
                narr.append(str(s.get('caption', '')))
            if not segs:
                raise RuntimeError('没有保留任何片段，请至少勾选一段')
            # _render_narrate 返回 (final_path, voice_clips)，必须解包
            final, voice_clips = _render_narrate(plan['video'], segs, narr, params, prog['run_dir'], prog,
                                                 music_path=plan.get('music'), mode=plan.get('mode'))
        else:
            raise RuntimeError('未知方案类型')
        prog['done'] = True
        prog['pct'] = 100
        prog['file'] = os.path.relpath(final, OUTDIR).replace('\\', '/')
        prog['diag'] = dict(plan.get('diag') or {})
        prog['diag']['segments'] = len(tl2) - 1 if plan['type'] == 'beatcut' else len(segs)
        if plan['type'] == 'narrate':
            prog['diag']['voice_clips'] = voice_clips
        _record_history(req, prog, 'plan-' + plan['type'])
        PLANS.pop(src_runid, None)
    except Exception as e:
        import traceback
        traceback.print_exc()
        prog['done'] = True
        prog['error'] = str(e)


def dispatch_beatcut(req, prog):
    """强卡点。与 /api/beatcut 共用。params.beatSync=True 时走「节拍同步」新引擎。"""
    try:
        run_dir = prog.get('run_dir') or os.path.join(OUTDIR, time.strftime('%Y%m%d-%H%M%S'))
        os.makedirs(run_dir, exist_ok=True)
        vp = _resolve_upload_video(req.get('video'), run_dir, 'src_video')
        if not vp:
            raise RuntimeError('未收到视频（或上传会话已过期，请重新上传）')
        mp = _resolve_music(req.get('music'))
        if not mp:
            raise RuntimeError('请先选择背景音乐')
        params = req.get('params', {})
        if params.get('beatSync'):
            final = os.path.join(run_dir, 'final.mp4')
            ret = generate_beat_sync_video(
                vp, mp, final,
                beat_sensitivity=float(params.get('beat_sensitivity', 0.5)),
                min_clip_dur=float(params.get('min_clip_dur', 0.6)),
                progress=prog)
            prog['done'] = True
            prog['pct'] = 100
            prog['file'] = os.path.relpath(ret['output'], OUTDIR).replace('\\', '/')
            prog['diag'] = {
                'mode': 'beat_sync',
                'beat_num': ret['beat_num'],
                'clip_num': ret['clip_num'],
                'warning': ret['warning'],
            }
            prog['mode'] = 'free'  # 节拍同步为离线模板，无需 LLM
            _record_history(req, prog, 'beatsync')
        else:
            final, diag = beat_cut_video(vp, mp, run_dir, params, prog)
            prog['done'] = True
            prog['pct'] = 100
            prog['file'] = os.path.relpath(final, OUTDIR).replace('\\', '/')
            prog['diag'] = diag
            prog['mode'] = 'free'  # 强卡点为离线节拍模板，无需 LLM
            _record_history(req, prog, 'beatcut')
    except Exception as e:
        import traceback
        traceback.print_exc()
        prog['done'] = True
        prog['error'] = str(e)
        if prog.get('run_dir'):
            try:
                prog['partial'] = collect_partial(prog['run_dir'])
            except Exception:
                pass


def dispatch_narrate(req, prog):
    """电影解说（本地短片版）。与 /api/narrate 共用，支持可选 BGM。"""
    try:
        run_dir = prog.get('run_dir') or os.path.join(OUTDIR, time.strftime('%Y%m%d-%H%M%S'))
        os.makedirs(run_dir, exist_ok=True)
        vp = _resolve_upload_video(req.get('video'), run_dir, 'src')
        if not vp:
            raise RuntimeError('未收到视频（或上传会话已过期，请重新上传）')
        music_path = _resolve_music(req.get('music'))
        final, diag = narrate_video(vp, req.get('params', {}), run_dir, prog, music_path=music_path)
        prog['done'] = True
        prog['pct'] = 100
        prog['file'] = os.path.relpath(final, OUTDIR).replace('\\', '/')
        prog['diag'] = diag
        prog['mode'] = prog.get('mode') or compute_mode(req.get('params', {}), needs_chat=True)
        _record_history(req, prog, 'narrate')
    except Exception as e:
        import traceback
        traceback.print_exc()
        prog['done'] = True
        prog['error'] = str(e)
        if prog.get('run_dir'):
            try:
                prog['partial'] = collect_partial(prog['run_dir'])
            except Exception:
                pass


def dispatch_movie(req, prog):
    """联网搜索 + 自动解说（Phase 3）。"""
    try:
        run_dir = prog.get('run_dir') or os.path.join(OUTDIR, time.strftime('%Y%m%d-%H%M%S'))
        os.makedirs(run_dir, exist_ok=True)
        vp = _resolve_upload_video(req.get('video'), run_dir, 'src')
        if vp is None and req.get('video'):
            raise RuntimeError('未收到视频（或上传会话已过期，请重新上传）')
        music_path = _resolve_music(req.get('music'))
        final, diag = narrate_movie(req.get('movie', ''), req.get('plot', ''), vp,
                                    req.get('params', {}), run_dir, prog, music_path=music_path)
        prog['done'] = True
        prog['pct'] = 100
        if final:
            prog['file'] = os.path.relpath(final, OUTDIR).replace('\\', '/')
        prog['diag'] = diag
        _record_history(req, prog, 'movie')
    except Exception as e:
        import traceback
        traceback.print_exc()
        prog['done'] = True
        prog['error'] = str(e)
        if prog.get('run_dir'):
            try:
                prog['partial'] = collect_partial(prog['run_dir'])
            except Exception:
                pass


def dispatch_instruct(req, prog):
    """指令解析层：解析自然语言 → 路由到对应工作流。与 /api/instruct 共用。"""
    instr = req.get('instruction', '')
    ctx = req.get('context', {}) or {}
    parsed = parse_instruction(instr, ctx)
    action = parsed['action']
    params = dict(parsed.get('params', {}))
    prog['phase'] = '解析指令 → ' + action
    prog['pct'] = 2
    if action == 'movie':
        mreq = {
            'movie': parsed.get('movie', '') or ctx.get('movie', ''),
            'plot': ctx.get('plot', '') or req.get('plot', ''),
            'video': req.get('video') or ctx.get('video'),
            'params': {**params, 'economy': params.get('economy', True), 'maxSeg': params.get('maxSeg', 25)},
            'music': params.get('music'),
        }
        return dispatch_movie(mreq, prog)
    if action == 'narrate':
        nreq = {
            'video': req.get('video') or ctx.get('video'),
            'params': {**params, 'economy': params.get('economy', True), 'maxSeg': params.get('maxSeg', 25)},
            'music': params.get('music'),
        }
        return dispatch_narrate(nreq, prog)
    if action == 'beatcut':
        breq = {
            'video': req.get('video') or ctx.get('video'),
            'music': params.get('music') or req.get('music'),
            'params': {**params},
        }
        return dispatch_beatcut(breq, prog)
    # compose
    bread = {
        'items': req.get('items') or ctx.get('items', []),
        'music': params.get('music') or req.get('music'),
        'params': {**params},
    }
    return dispatch_build(bread, prog)


def collect_partial(run_dir):
    """任务失败时收集 run_dir 中已生成的中间产物，便于用户拿到部分成果。
    返回 {'files':[{name,ext,size,kind,rel,url}], 'text':<首个文本文件内容>, 'best_video':<最成品视频url>}。"""
    if not run_dir or not os.path.isdir(run_dir):
        return {'files': [], 'text': None, 'best_video': None}
    files, text, best_video = [], None, None
    try:
        for fn in sorted(os.listdir(run_dir)):
            fp = os.path.join(run_dir, fn)
            if not os.path.isfile(fp):
                continue
            ext = os.path.splitext(fn)[1].lower()
            rel = os.path.relpath(fp, OUTDIR).replace('\\', '/')
            size = os.path.getsize(fp)
            if ext == '.mp4':
                kind = 'video'
            elif ext in ('.wav', '.mp3', '.m4a', '.aac', '.ogg'):
                kind = 'audio'
            elif ext == '.srt':
                kind = 'subtitle'
            elif ext == '.txt':
                kind = 'text'
            else:
                kind = 'file'
            entry = {'name': fn, 'ext': ext, 'size': size, 'kind': kind,
                     'rel': rel, 'url': '/media/' + rel}
            if kind == 'text' and text is None and size < 200000:
                try:
                    text = open(fp, 'r', encoding='utf-8', errors='ignore').read()
                except Exception:
                    pass
            files.append(entry)
    except Exception:
        pass
    videos = [e for e in files if e['kind'] == 'video']

    def vrank(e):
        n = e['name']
        if n == 'final.mp4':
            return 0
        if n == 'vid_sub.mp4':
            return 1
        if n.startswith('vid'):
            return 2
        if n.startswith('bc'):
            return 3
        if n.startswith('nar'):
            return 4
        if n.startswith('seg'):
            return 5
        return 6
    if videos:
        videos.sort(key=vrank)
        best_video = videos[0]['url']
    kind_order = {'video': 0, 'audio': 1, 'subtitle': 2, 'text': 3, 'file': 4}
    files.sort(key=lambda e: (kind_order.get(e['kind'], 9), e['name']))
    return {'files': files, 'text': text, 'best_video': best_video}


def assemble(items, params, music=None, progress=None, run_dir=None):
    """items: list of {kind:'image'|'video', src, dur, motion}
       params: {w, h, fps, transition}
       music: optional path to an audio file (mp3/wav).
       progress: optional mutable dict updated with phase/pct/done/error for a UI poller.
       If music given: clips are beat-aligned, audio is the music, total = music length.
       If no music: fall back to equal-duration clips with no audio track.
       Returns path to final mp4, or raises."""
    def up(phase, pct):
        if progress is not None:
            progress['phase'] = phase
            progress['pct'] = pct
    up('解析素材', 2)
    run_dir = run_dir or os.path.join(OUTDIR, time.strftime('%Y%m%d-%H%M%S'))
    os.makedirs(run_dir, exist_ok=True)
    w = int(params.get('w', W)); h = int(params.get('h', H))
    fps = int(params.get('fps', 30))
    trans = params.get('transition', 'fade')

    # ---- resolve per-item display durations (driven by photo/video count & durations ----
    N = len(items)
    if N == 0:
        raise RuntimeError('没有可合成的素材')
    item_durs = [float(it.get('dur', 3)) for it in items]
    d0 = item_durs[0] if item_durs else 3
    if params.get('hardCut'):
        fade = 0.0   # hard cut: precise on-beat switching with no crossfade
    else:
        fade = min(0.6, d0 / 2)
    target_total = float(sum(item_durs))

    if music:
        analysis = analyze_beats(music)
        bpm, beats = (analysis if analysis else (None, []))
        mlen = probe_audio_len(music) or 0.0
        if mlen <= 0:
            raise RuntimeError('无法读取音乐时长')
        # video length = photo-driven; interior cuts land near beats with given interval
        step = float(params.get('beatStep', 1) or 1)
        disp = plan_beat_durations(item_durs, beats or [], fade, step)
        total_len = float(sum(disp))
        beat_info = {'bpm': round(float(bpm), 1) if bpm is not None else None,
                     'beat_count': len(beats),
                     'music_len': round(mlen, 2),
                     'clips': N,
                     'beatStep': step,
                     'durations': [round(float(x), 3) for x in disp]}
    else:
        total_len = target_total
        beat_info = {'bpm': None, 'beat_count': 0,
                     'durations': [round(float(x), 3) for x in item_durs]}
        disp = list(item_durs)

    # timing list of (start, disp) for segment building
    timing = []
    s = 0.0
    for i, d in enumerate(disp):
        timing.append((s, d))
        s += d
    if music and total_len is not None and total_len > 0:
        total_len = s

    # ---- 1) build segments with their intended display duration + fade padding ----
    segments = []
    real_durs = []
    for idx, it in enumerate(items):
        if progress is not None and progress.get('abort'):
            raise RuntimeError('已取消')
        up(f'渲染镜头 {idx + 1}/{len(items)}', 8 + int(52 * idx / max(1, len(items))))
        start, disp = timing[idx]
        # pad each segment by `fade` so the xfade overlap keeps the total timeline
        seg_dur = disp + fade
        seg = os.path.join(run_dir, f'seg{idx}.mp4')
        if it['kind'] == 'image':
            make_image_clip(it['src'], seg_dur, int(it.get('motion', idx % 4)), seg, w, h, fps)
            real_durs.append(seg_dur)
        else:
            seg, real = make_video_clip(it['src'], seg_dur, seg, w, h, fps)
            real_durs.append(real)
        segments.append(seg)
    up('合并片段(转场)', 64)

    if len(segments) == 1:
        src_out = os.path.join(run_dir, 'vid_silent.mp4')
        shutil.copy(segments[0], src_out)
    else:
        for i, s in enumerate(segments):
            if not os.path.exists(s) or os.path.getsize(s) < 100:
                raise RuntimeError(f'片段 {i} 生成失败')
        if fade <= 0:
            # hard cut: pure concat (each segment exact), total = sum(disp)
            parts = ''.join(f'[{i}:v]' for i in range(len(segments)))
            filter_str = f'{parts}concat=n={len(segments)}:v=1:a=0[vout]'
        else:
            # xfade chain: offset_k puts the transition near the beat start.
            offsets = []
            acc = real_durs[0]
            for i in range(1, len(segments)):
                offsets.append(acc - fade)
                acc += real_durs[i] - fade
            chain = []
            prev = '[0:v]'
            for i in range(1, len(segments)):
                out_label = 'vout' if i == len(segments) - 1 else f'x{i}'
                chain.append(f"{prev}[{i}:v]xfade=transition={trans}:duration={fade:.3f}:offset={offsets[i-1]:.3f}[{out_label}]")
                prev = f'[{out_label}]'
            filter_str = ';'.join(chain)
        cmd = ['-y']
        for s in segments:
            cmd += ['-i', s]
        cmd += ['-filter_complex', filter_str, '-map', '[vout]'] + video_encode_args() + [
                '-threads', '0', os.path.join(run_dir, 'vid_silent.mp4')]
        rc, o, e = ffmpeg_run(cmd)
        if rc != 0:
            raise RuntimeError('合成失败: ' + e.decode('utf-8', 'ignore')[-600:])

    # assemble returns the concatenated silent video (no audio yet).
    if progress is not None:
        progress['pct'] = 70
        progress['done'] = False
        progress['beat'] = beat_info
        progress['duration'] = round(float(total_len), 2)
    return os.path.join(run_dir, 'vid_silent.mp4'), total_len, beat_info


# ---------------------------------------------------------------------------
# 后期：烧字幕(.srt) + AI配音 + 背景音乐混音，输出最终 final.mp4
# ---------------------------------------------------------------------------
def finalize(video_path, params, music, captions, durations=None, progress=None):
    """video_path: assembled silent mp4 (len = photo-driven total)
       music: optional bg audio
       captions: optional list of per-clip Chinese captions (else no subs/narration)
       durations: optional per-clip display durations (from assemble) for exact subtitle/narration timing
       Returns final mp4 path. Raises on failure."""
    run_dir = os.path.dirname(video_path)
    voice_over = bool(captions)
    narration_path = None
    srt_path = None

    # 1) subtitles (align each caption to its clip's display window)
    total = probe_audio_len(video_path) or 0.0
    N = len(captions) if captions else 0
    if N > 0:
        starts = []; durs = []
        if durations and len(durations) == N:
            acc = 0.0
            for dd in durations:
                starts.append(acc); acc += dd
            durs = list(durations)
            # scale to actual total if slight mismatch
            sm = sum(durs)
            if sm > 0 and abs(sm - total) > 0.05:
                k = total / sm
                durs = [x * k for x in durs]
                starts = []
                acc = 0.0
                for dd in durs:
                    starts.append(acc); acc += dd
        else:
            d = total / max(1, N)
            starts = [i * d for i in range(N)]
            durs = [d for _ in range(N)]
        if progress:
            progress['phase'] = '生成字幕'
            progress['pct'] = 74
        srt_path = os.path.join(run_dir, 'subs.srt')
        build_srt(captions, starts, durs, srt_path)
        # burn subtitles (needs libass) — fallback: if filter fails, keep unburned
        burned = os.path.join(run_dir, 'vid_sub.mp4')
        # escape path for filter
        esc = srt_path.replace('\\', '/').replace(':', '\\:').replace('\'', '\\\'')
        rc, o, e = ffmpeg_run(['-y', '-i', video_path,
                               '-vf', f"subtitles='{esc}':force_style='FontName=Microsoft YaHei,FontSize=20,Alignment=2,MarginV=40'",
                               ] + video_encode_args() + ['-threads', '0', '-an', burned])
        if rc == 0 and os.path.exists(burned):
            video_path = burned
        if progress:
            progress['phase'] = '配音合成'
            progress['pct'] = 80

    # 2) TTS narration for each caption (only if TTS configured & succeeded)
    narration_path = None
    if voice_over and _tts_available():
        clips = []
        for i, cap in enumerate(captions if N else []):
            if not (cap and cap.strip()):
                continue
            np_ = os.path.join(run_dir, f'nar{i}.mp3')
            od = starts[i] if i < len(starts) else (total / max(1, N)) * i
            if ai_tts(cap, np_):
                clips.append((np_, od, durs[i] if i < len(durs) else 0))
        if clips:
            narration_path = os.path.join(run_dir, 'narration.m4a')
            inputs = []
            fparts = []
            for k2, (np_, od, odur) in enumerate(clips):
                inputs += ['-i', np_]
                fparts.append(f'[{k2}:a]adelay={int(od*1000)}|{int(od*1000)},apad=whole_dur={int(total*1000)}[v{k2}]')
            mixin = ''.join(f'[v{k2}]' for k2 in range(len(clips)))
            fparts.append(f'{mixin}amix=inputs={len(clips)}:normalize=0,atrim=0:{total:.3f},aformat=fltp[aout]')
            fc = ';'.join(fparts)
            cmd = ['-y'] + inputs + ['-filter_complex', fc, '-map', '[aout]',
                                     '-c:a', 'aac', '-b:a', '160k', narration_path]
            rc, o, e = ffmpeg_run(cmd)
            if rc == 0 and os.path.exists(narration_path) and os.path.getsize(narration_path) > 500:
                pass
            else:
                narration_path = None

    # 3) final mux: video(+subs burned) + bg music + narration(mixed) if any
    final = os.path.join(run_dir, 'final.mp4')
    if progress:
        progress['phase'] = '合成音频轨'
        progress['pct'] = 90
    if music and narration_path and os.path.exists(narration_path):
        # video + narration as main audio, music as quieter bg
        cmd = ['-y', '-stream_loop', '-1', '-i', music, '-i', narration_path, '-i', video_path,
               '-filter_complex',
               '[0:a]volume=0.45[bg0];[1:a]aformat=fltp[na];[bg0][na]amix=inputs=2:normalize=0[aout]',
               '-map', '2:v:0', '-map', '[aout]', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
               '-shortest', '-movflags', '+faststart', final]
    elif narration_path and os.path.exists(narration_path):
        # narration only (no bg music)
        cmd = ['-y', '-i', narration_path, '-i', video_path,
               '-map', '1:v:0', '-map', '0:a:0', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
               '-movflags', '+faststart', final]
    elif music:
        cmd = ['-y', '-stream_loop', '-1', '-i', music, '-i', video_path,
               '-map', '1:v:0', '-map', '0:a:0', '-c:v', 'copy', '-c:a', 'aac',
               '-b:a', '192k', '-shortest', '-movflags', '+faststart', final]
    else:
        cmd = ['-y', '-i', video_path,
               '-c:v', 'copy', '-c:a', 'aac', '-b:a', '160k', '-movflags', '+faststart', final]
    rc, o, e = ffmpeg_run(cmd)
    if rc != 0:
        raise RuntimeError('最终合成失败: ' + e.decode('utf-8', 'ignore')[-600:])
    if progress:
        progress['phase'] = '完成'
        progress['pct'] = 100
        progress['done'] = True
        progress['file'] = os.path.relpath(final, OUTDIR).replace('\\', '/')
    return final


# ---------------------------------------------------------------------------
# 本地 HTTP 服务 + 图形化前端
# ---------------------------------------------------------------------------
MIME = {
    '.html': 'text/html; charset=utf-8', '.js': 'application/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8', '.png': 'image/png', '.mp4': 'video/mp4',
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, content, ctype='text/plain; charset=utf-8', extra=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(content)))
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(content)

    def _read_json(self, length, max_len=300 * 1024 * 1024):
        """按 Content-Length 分块读取并解析 JSON 请求体。
        超过 max_len 时也必须把请求体读完（排空）再返回 None——提前关连接的话，
        客户端还在发送会收到 WinError 10053 连接中断，看不到友好的「请求过大」。
        用 list 收集 + b''.join 一次拼接：旧写法 raw += chunk 是 O(n²) 拷贝，大文件上传显著变慢。"""
        if length <= 0:
            return {}
        if length > max_len:
            remaining = length
            try:
                while remaining > 0:
                    chunk = self.rfile.read(min(4 * 1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except Exception:
                pass
            return None
        parts = []
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            parts.append(chunk)
            remaining -= len(chunk)
        raw = b''.join(parts)
        return json.loads(raw.decode('utf-8'))

    def _spawn(self, fn, req):
        """登记一个后台任务并启动线程，返回 runid 供前端轮询。
        每个任务拥有独立的 run_dir（OUTDIR/runid-时间戳），产物互不干扰；并把 runid 绑定到
        任务线程的 TLS，使 ffmpeg_run 能注册进程并响应「取消」。"""
        RUNSEQ[0] += 1
        runid = 'run-%d' % RUNSEQ[0]
        # 目录名带时间戳：服务重启后 RUNSEQ 归零会复用 run-N 名字，
        # 否则新任务会写进旧目录覆盖成片，历史记录（⑨记录）也随之指向错误文件
        run_dir = os.path.join(OUTDIR, '%s-%s' % (runid, time.strftime('%Y%m%d-%H%M%S')))
        os.makedirs(run_dir, exist_ok=True)
        prog = {'phase': '排队', 'pct': 0, 'done': False, 'runid': runid, 'run_dir': run_dir}
        PROGRESS[runid] = prog
        # 防内存泄漏：PROGRESS 只增不减（diag/解说稿可能不小），长驻进程只保留最近 100 条
        if len(PROGRESS) > 100:
            for k in list(PROGRESS.keys())[:-100]:
                if k not in RUN_PROCS:
                    PROGRESS.pop(k, None)

        def _runner():
            _TLS.runid = runid
            try:
                fn(req, prog)
            except AbortError:
                prog['done'] = True
                prog['aborted'] = True
                prog['error'] = '已取消（用户中断）'
            except Exception as e:
                import traceback
                traceback.print_exc()
                prog['done'] = True
                prog['error'] = str(e)
                if prog.get('run_dir'):
                    try:
                        prog['partial'] = collect_partial(prog['run_dir'])
                    except Exception:
                        pass

        _threading.Thread(target=_runner, daemon=True).start()
        return runid

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ('/', '/index.html'):
            idx = os.path.join(STATIC_DIR, 'index.html')
            if os.path.exists(idx):
                self._send(200, open(idx, 'rb').read(), 'text/html; charset=utf-8')
            else:
                self._send(500, '前端文件缺失：请确保 static/ 目录存在'.encode('utf-8'), 'text/html; charset=utf-8')
            return
        if path.startswith('/static/'):
            name = path[len('/static/'):].split('?')[0]
            full = os.path.join(STATIC_DIR, os.path.basename(name))
            if os.path.isfile(full):
                ext = os.path.splitext(full)[1].lower()
                self._send(200, open(full, 'rb').read(), MIME.get(ext, 'application/octet-stream'))
                return
            self._send(404, b'not found')
            return
        if path.startswith('/media/'):
            name = path[len('/media/'):].split('?')[0]
            # first look in run output dir, then the folder containing built-in assets
            for base in (OUTDIR, HERE):
                full = _safe_join(base, name)
                if full:
                    ext = os.path.splitext(full)[1].lower()
                    self._send(200, open(full, 'rb').read(), MIME.get(ext, 'application/octet-stream'))
                    return
            self._send(404, b'not found')
            return
        if path.startswith('/music_lib/'):
            name = path[len('/music_lib/'):].split('?')[0]
            full = _safe_join(MUSIC_DIR, name)
            if full:
                self._send(200, open(full, 'rb').read(), MIME.get('.mp3', 'audio/mpeg'))
                return
            self._send(404, b'not found')
            return
        if path == '/api/music/search':
            q = parse_qs(urlparse(self.path).query).get('q', [''])[0]
            self._send(200, json.dumps({'ok': True, 'results': search_catalog(q)}).encode('utf-8'),
                       'application/json')
            return
        if path == '/api/music/use':
            q = parse_qs(urlparse(self.path).query).get('id', [None])[0]
            if not q:
                self._send(200, json.dumps({'ok': False, 'error': '缺少 id'}).encode('utf-8'), 'application/json')
                return
            try:
                p = download_catalog(q)
                self._send(200, json.dumps({'ok': True, 'file': os.path.basename(p),
                                            'url': '/music_lib/' + os.path.basename(p)}).encode('utf-8'),
                           'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/material/list':
            self._send(200, json.dumps({'ok': True, 'items': material_list()}).encode('utf-8'),
                       'application/json')
            return
        if path.startswith('/material_lib/'):
            # 中文文件名：URL 里的百分号编码必须解码后再查文件（/media 一直是 ASCII 名所以没暴露过）
            name = unquote(path[len('/material_lib/'):].split('?')[0])
            full = _safe_join(MATERIAL_DIR, name)
            if full:
                ext = os.path.splitext(full)[1].lower()
                self._send(200, open(full, 'rb').read(), MIME.get(ext, 'application/octet-stream'))
                return
            self._send(404, b'not found')
            return
        if path == '/api/bili/search':
            kw = parse_qs(urlparse(self.path).query).get('kw', [''])[0].strip()
            if not kw:
                self._send(200, json.dumps({'ok': False, 'error': '缺少关键词'}).encode('utf-8'), 'application/json')
                return
            try:
                res = bili_search(kw, 8)
                self._send(200, json.dumps({'ok': True, 'results': res}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)[:180]}).encode('utf-8'), 'application/json')
            return
        if path == '/api/bili/status':
            self._send(200, json.dumps({'ok': True, **BILI_PULL}).encode('utf-8'), 'application/json')
            return
        if path == '/api/progress':
            runid = parse_qs(urlparse(self.path).query).get('run', [None])[0]
            if not runid or runid not in PROGRESS:
                self._send(404, json.dumps({'error': '未知 run'}).encode('utf-8'), 'application/json')
                return
            self._send(200, json.dumps(PROGRESS[runid]).encode('utf-8'), 'application/json')
            return
        if path == '/api/history':
            self._send(200, json.dumps({'ok': True, 'history': load_history(50)}).encode('utf-8'),
                       'application/json')
            return
        if path == '/api/ai/config':
            cfg = load_ai_config()
            def mask(ch):
                ch = dict(ch or {})
                if ch.get('api_key'):
                    ch['api_key'] = ('*' * 6) + ch['api_key'][-4:]
                return ch
            self._send(200, json.dumps({
                'ok': True,
                'config': {'vision': mask(cfg.get('vision')), 'tts': mask(cfg.get('tts')),
                           'local': mask(cfg.get('local')),
                           'whisper': dict(cfg.get('whisper') or {}),
                           'vlm': mask(cfg.get('vlm')),
                           'mirror': dict(cfg.get('mirror') or {}),
                           'video': dict(cfg.get('video') or {})},
                'vision_available': _vision_available(),
                'tts_available': _tts_available(),
                'local_enabled': local_llm_enabled(),
                'whisper_ready': whisper_model_ready(),
                'vlm_enabled': vlm_enabled(),
                'video_encoder': video_encoder_label(),
            }).encode('utf-8'), 'application/json')
            return
        if path == '/api/ai_status':
            self._send(200, json.dumps(ai_status()).encode('utf-8'), 'application/json')
            return
        if path == '/api/local/test':
            ok, msg = local_llm_ping()
            self._send(200, json.dumps({'ok': True, 'test_ok': ok, 'message': msg}).encode('utf-8'),
                       'application/json')
            return
        if path == '/api/local/status':
            ok, msg = (local_llm_ping() if local_llm_enabled() else (False, '本地模型未启用'))
            self._send(200, json.dumps({'ok': True, 'enabled': local_llm_enabled(), 'ready': bool(ok),
                                        'message': msg, 'model': local_llm_cfg()['model'],
                                        'pulling': LOCAL_PULL['running'], 'pull_model': LOCAL_PULL['model'],
                                        'pull_ok': LOCAL_PULL['ok'], 'pull_msg': LOCAL_PULL['msg'],
                                        'pull_pct': LOCAL_PULL.get('pct', 0)}).encode('utf-8'),
                           'application/json')
            return
        if path == '/api/ai/test':
            # run the tests (network) and report both channels; block current thread until done
            v_ok, v_msg = _test_vision()
            t_ok, t_msg = _test_tts()
            self._send(200, json.dumps({'ok': True,
                                        'vision': {'test_ok': v_ok, 'message': v_msg},
                                        'tts': {'test_ok': t_ok, 'message': t_msg},
                                        }).encode('utf-8'), 'application/json')
            return
        if path == '/api/whisper/status':
            md = whisper_models_dir()
            avail = sorted(d for d in os.listdir(md)) if os.path.isdir(md) else []
            self._send(200, json.dumps({
                'ok': True,
                'selected': whisper_model_name(),
                'models_dir': md,
                'ready': whisper_model_ready(),
                'downloading': WHISPER_DL['running'],
                'download_model': WHISPER_DL['model'],
                'download_ok': WHISPER_DL['ok'],
                'download_msg': WHISPER_DL['msg'],
                'available': avail,
                'valid_models': _WHISPER_MODELS,
            }).encode('utf-8'), 'application/json')
            return
        if path == '/api/vlm/status':
            ok, msg = (vlm_ping() if vlm_enabled() else (False, 'VLM 未启用'))
            self._send(200, json.dumps({'ok': True, 'enabled': vlm_enabled(), 'ready': bool(ok),
                                        'message': msg, 'model': vlm_cfg()['model'],
                                        'pulling': VLM_PULL['running'], 'pull_model': VLM_PULL['model'],
                                        'pull_ok': VLM_PULL['ok'], 'pull_msg': VLM_PULL['msg'],
                                        'pull_pct': VLM_PULL.get('pct', 0)}).encode('utf-8'),
                           'application/json')
            return
        self._send(404, b'not found')

    def do_POST(self):
        path = urlparse(self.path).path
        if path == '/api/build':
            try:
                length = int(self.headers.get('Content-Length', 0))
                req = self._read_json(length, max_len=220 * 1024 * 1024)
                if req is None:
                    self._send(200, json.dumps({'ok': False, 'error': '请求过大(>220MB)或读取失败'}).encode('utf-8'), 'application/json')
                    return
                runid = self._spawn(dispatch_build, req)
                self._send(200, json.dumps({'ok': True, 'runid': runid}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/ai/config':
            try:
                length = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(length) if length else b'{}'
                data = json.loads(raw.decode('utf-8') or '{}')
                cfg = load_ai_config()
                # incoming shape: { vision: {base_url,api_key,model}, tts:{base_url,api_key,model,voice}, local:{enabled,base_url,model,api_key} }
                for ch in ('vision', 'tts'):
                    inc = data.get(ch)
                    if not isinstance(inc, dict):
                        continue
                    cur = dict(cfg.get(ch) or {})
                    for k, v in inc.items():
                        if v is not None:
                            cur[k] = str(v).strip()
                        elif k in cur:
                            del cur[k]
                    cfg[ch] = cur
                if isinstance(data.get('local'), dict):
                    inc = data['local']
                    cur = dict(cfg.get('local') or {})
                    for k in ('base_url', 'model', 'api_key'):
                        if inc.get(k) is not None:
                            cur[k] = str(inc[k]).strip()
                        elif k in cur:
                            del cur[k]
                    if 'enabled' in inc:
                        cur['enabled'] = bool(inc['enabled'])
                    cfg['local'] = cur
                if isinstance(data.get('mirror'), dict):
                    inc = data['mirror']
                    cur = dict(cfg.get('mirror') or {})
                    if inc.get('hf_endpoint') is not None:
                        cur['hf_endpoint'] = str(inc['hf_endpoint']).strip()
                    if inc.get('ollama_proxy') is not None:
                        cur['ollama_proxy'] = str(inc['ollama_proxy']).strip()
                    if 'use_hf_mirror' in inc:
                        cur['use_hf_mirror'] = bool(inc['use_hf_mirror'])
                    cfg['mirror'] = cur
                if isinstance(data.get('whisper'), dict):
                    inc = data['whisper']
                    cur = dict(cfg.get('whisper') or {})
                    if inc.get('model') is not None:
                        cur['model'] = str(inc['model']).strip()
                    cfg['whisper'] = cur
                if isinstance(data.get('vlm'), dict):
                    inc = data['vlm']
                    cur = dict(cfg.get('vlm') or {})
                    for k in ('base_url', 'model', 'api_key', 'mode'):
                        if inc.get(k) is not None:
                            cur[k] = str(inc[k]).strip()
                        elif k in cur:
                            del cur[k]
                    if 'enabled' in inc:
                        cur['enabled'] = bool(inc['enabled'])
                    cfg['vlm'] = cur
                if isinstance(data.get('video'), dict):
                    # 编码策略：auto(默认·GPU可用则用) / cpu / gpu
                    inc = data['video']
                    cur = dict(cfg.get('video') or {})
                    enc = str(inc.get('encoder') or '').strip().lower()
                    if enc in ('auto', 'cpu', 'gpu'):
                        cur['encoder'] = enc
                    cfg['video'] = cur
                save_ai_config(cfg)
                self._send(200, json.dumps({'ok': True,
                                            'vision_available': _vision_available(),
                                            'tts_available': _tts_available(),
                                            'video_encoder': video_encoder_label()}).encode('utf-8'),
                              'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/whisper/download':
            try:
                length = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(length) if length else b'{}'
                data = json.loads(raw.decode('utf-8') or '{}')
                ok, msg = whisper_download_async(data.get('model'))
                self._send(200, json.dumps({'ok': ok, 'message': msg}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/vlm/pull':
            try:
                length = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(length) if length else b'{}'
                data = json.loads(raw.decode('utf-8') or '{}')
                ok, msg = vlm_pull_async(data.get('model'))
                self._send(200, json.dumps({'ok': ok, 'message': msg}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/local/pull':
            try:
                length = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(length) if length else b'{}'
                data = json.loads(raw.decode('utf-8') or '{}')
                ok, msg = local_pull_async(data.get('model'))
                self._send(200, json.dumps({'ok': ok, 'message': msg}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/mirror/scan':
            # 自动探测可用 Ollama 安装包镜像，免去人工替换失效链接；并发探测（约 6s）后返回可用列表与推荐。
            try:
                self._send(200, json.dumps({'ok': True, 'result': scan_ollama_mirrors()}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/cancel':
            # 取消任务：置 abort 标记（ffmpeg_run 每 0.3s 轮询到后立即终止并抛 AbortError）+
            # 立即 terminate 正在运行的 ffmpeg 进程，保证合成/解说可真正中断
            try:
                length = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(length) if length else b'{}'
                data = json.loads(raw.decode('utf-8') or '{}')
                runid = data.get('runid')
                if runid and runid in PROGRESS:
                    PROGRESS[runid]['abort'] = True
                    with _PROC_LOCK:
                        proc = RUN_PROCS.get(runid)
                    if proc is not None:
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
                    self._send(200, json.dumps({'ok': True}).encode('utf-8'), 'application/json')
                else:
                    self._send(200, json.dumps({'ok': False, 'error': '未知 run'}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/history/delete':
            try:
                length = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(length) if length else b'{}'
                data = json.loads(raw.decode('utf-8') or '{}')
                ok = delete_history(data.get('file'))
                self._send(200, json.dumps({'ok': ok}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/history/clear':
            try:
                clear_history()
                self._send(200, json.dumps({'ok': True}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/cover':
            # 封面生成：ts 为空 → 智能选帧（返回全部候选缩略图供换帧）；带 ts → 按当前设置重渲染
            try:
                length = int(self.headers.get('Content-Length', 0))
                data = self._read_json(length, max_len=64 * 1024) or {}
                fp = _safe_join(OUTDIR, data.get('file') or '')
                if not fp:
                    raise RuntimeError('视频不存在或不在产物目录内')
                run_dir = os.path.dirname(fp)
                rel = lambda p: os.path.relpath(p, OUTDIR).replace('\\', '/')
                title = str(data.get('title') or '')[:80]
                sub = str(data.get('sub') or '')[:40]
                style = max(0, min(2, int(data.get('style') or 0)))
                cand_dir = os.path.join(run_dir, 'cover_cand')
                list_json = os.path.join(cand_dir, 'list.json')
                cands = []
                if os.path.isfile(list_json):
                    try:
                        with open(list_json, 'r', encoding='utf-8') as f:
                            cands = json.load(f)
                    except Exception:
                        cands = []
                ts = data.get('ts')
                if ts is None or not cands:
                    cands = _cover_candidates(fp, run_dir)
                    if not cands:
                        raise RuntimeError('候选帧抽取失败')
                    ts = max(cands, key=lambda c: c['score'])['ts']
                    try:
                        os.makedirs(cand_dir, exist_ok=True)
                        with open(list_json, 'w', encoding='utf-8') as f:
                            json.dump(cands, f, ensure_ascii=False)
                    except Exception:
                        pass
                else:
                    ts = round(float(ts), 2)
                cover = os.path.join(run_dir, 'cover.jpg')
                _cover_render(fp, ts, title, sub, style, cover)
                for c in cands:
                    c['thumb'] = rel(os.path.join(cand_dir, os.path.basename(c['thumb'])))
                self._send(200, json.dumps({'ok': True, 'cover': rel(cover), 'ts': ts, 'title': title,
                                            'candidates': cands}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)[:180]}).encode('utf-8'), 'application/json')
            return
        if path == '/api/material/upload':
            # 小文件（≤64MB 由前端判定）直接 base64 存入素材库
            try:
                length = int(self.headers.get('Content-Length', 0))
                data = self._read_json(length, max_len=220 * 1024 * 1024) or {}
                name, err = material_save_bytes(data.get('name') or '',
                                                base64.b64decode(data.get('data', '') or ''))
                self._send(200, json.dumps({'ok': bool(name), 'name': name, 'error': err}).encode('utf-8'),
                           'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/material/from_upload':
            # 大文件：把分片上传会话的成品 move 进素材库
            try:
                length = int(self.headers.get('Content-Length', 0))
                data = self._read_json(length, max_len=64 * 1024) or {}
                src = _upload_final_path(data.get('upload_id'), data.get('name'))
                if not src:
                    raise RuntimeError('上传会话不存在或未完成')
                name = material_save_file(src)
                try:
                    d = _upload_dir_of(data.get('upload_id'))
                    if d and os.path.isdir(d):
                        shutil.rmtree(d, ignore_errors=True)
                except OSError:
                    pass
                self._send(200, json.dumps({'ok': True, 'name': name}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/material/save_from_media':
            # 把产物目录里的文件（如 B 站下载的视频）复制进素材库
            try:
                length = int(self.headers.get('Content-Length', 0))
                data = self._read_json(length, max_len=64 * 1024) or {}
                src = _safe_join(OUTDIR, data.get('file') or '')
                if not src:
                    raise RuntimeError('源文件不存在')
                name = material_save_file(src)
                self._send(200, json.dumps({'ok': True, 'name': name}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/material/delete':
            try:
                length = int(self.headers.get('Content-Length', 0))
                data = self._read_json(length, max_len=64 * 1024) or {}
                ok, err = material_delete(data.get('name') or '')
                self._send(200, json.dumps({'ok': ok, 'error': err}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/bili/download':
            try:
                length = int(self.headers.get('Content-Length', 0))
                data = self._read_json(length, max_len=64 * 1024) or {}
                self._send(200, json.dumps(_bili_start_download((data.get('bvid') or '').strip())).encode('utf-8'),
                           'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/bili/cancel':
            try:
                BILI_PULL['abort'] = True
                self._send(200, json.dumps({'ok': True}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/upload/init':
            # 大视频分片上传第一步：开会话；带 upload_id 则为断点续传（返回已到齐分片列表，
            # 前端跳过这些分片——会话在磁盘上，服务重启后也能续传）。顺手清理超 24h 的废弃会话。
            try:
                length = int(self.headers.get('Content-Length', 0))
                data = self._read_json(length, max_len=64 * 1024) or {}
                have = None
                uid = data.get('upload_id')
                if uid:
                    d = _upload_dir_of(uid)
                    if d is not None and os.path.isdir(d) and any(
                            fn.startswith('final__') for fn in os.listdir(d)):
                        uid = None   # 该会话已完成（成品待任务取走）→ 按新会话处理，避免重传覆盖
                    else:
                        have = _upload_have_parts(uid)
                        if have is None:
                            uid = None   # 会话过期/非法 → 按新会话处理
                if uid is None:
                    uid = 'up-%d-%s' % (int(time.time() * 1000), ''.join(random.choice('0123456789abcdef') for _ in range(6)))
                    d = _upload_dir_of(uid)
                    if d is None:
                        raise RuntimeError('会话 id 生成失败')
                    os.makedirs(d, exist_ok=True)
                    have = []
                # 清理放在会话创建之后：新会话也计入数量上限（否则长期停在 上限+1）
                _upload_prune()
                self._send(200, json.dumps({'ok': True, 'upload_id': uid, 'have': have}).encode('utf-8'),
                           'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/upload/chunk':
            # 单分片：base64 解码后 ≤8MB，写 part 文件（乱序到达安全）
            try:
                length = int(self.headers.get('Content-Length', 0))
                data = self._read_json(length, max_len=16 * 1024 * 1024)
                if data is None:
                    raise RuntimeError('分片过大或读取失败')
                ok, err = _upload_chunk_write(data.get('upload_id'), data.get('idx'),
                                              base64.b64decode(data.get('data', '') or ''))
                self._send(200, json.dumps({'ok': ok, 'error': err}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/upload/done':
            # 收尾：按序合并分片。成品留在会话目录，由任务 dispatch 取走（move，免二次拷贝）
            try:
                length = int(self.headers.get('Content-Length', 0))
                data = self._read_json(length, max_len=64 * 1024) or {}
                final, err = _upload_finalize(data.get('upload_id'), data.get('name'), data.get('chunks'))
                self._send(200, json.dumps({'ok': bool(final), 'error': err,
                                            'size': os.path.getsize(final) if final else 0}).encode('utf-8'),
                           'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/beatcut':
            try:
                length = int(self.headers.get('Content-Length', 0))
                req = self._read_json(length, max_len=300 * 1024 * 1024)
                if req is None:
                    self._send(200, json.dumps({'ok': False, 'error': '请求过大(>300MB)或读取失败'}).encode('utf-8'), 'application/json')
                    return
                runid = self._spawn(dispatch_beatcut, req)
                self._send(200, json.dumps({'ok': True, 'runid': runid}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/narrate':
            try:
                length = int(self.headers.get('Content-Length', 0))
                req = self._read_json(length, max_len=300 * 1024 * 1024)
                if req is None:
                    self._send(200, json.dumps({'ok': False, 'error': '请求过大(>300MB)或读取失败'}).encode('utf-8'), 'application/json')
                    return
                runid = self._spawn(dispatch_narrate, req)
                self._send(200, json.dumps({'ok': True, 'runid': runid}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/narrate_movie':
            try:
                length = int(self.headers.get('Content-Length', 0))
                req = self._read_json(length, max_len=300 * 1024 * 1024)
                if req is None:
                    self._send(200, json.dumps({'ok': False, 'error': '请求过大(>300MB)或读取失败'}).encode('utf-8'), 'application/json')
                    return
                runid = self._spawn(dispatch_movie, req)
                self._send(200, json.dumps({'ok': True, 'runid': runid}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/plan':
            # 人机协同·分析：分析素材生成「规划方案」，等待用户在预览界面微调
            try:
                length = int(self.headers.get('Content-Length', 0))
                req = self._read_json(length, max_len=300 * 1024 * 1024)
                if req is None:
                    self._send(200, json.dumps({'ok': False, 'error': '请求过大(>300MB)或读取失败'}).encode('utf-8'), 'application/json')
                    return
                runid = self._spawn(_analyze_plan_job, req)
                self._send(200, json.dumps({'ok': True, 'runid': runid}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/confirm':
            # 人机协同·渲染：按用户微调后的方案合成成片
            try:
                length = int(self.headers.get('Content-Length', 0))
                req = self._read_json(length) if length else {}
                if req is None:
                    req = {}
                runid = req.get('runid')
                if not runid or runid not in PROGRESS or runid not in PLANS:
                    self._send(200, json.dumps({'ok': False, 'error': '方案不存在或已过期，请重新分析'}).encode('utf-8'), 'application/json')
                    return
                nrunid = self._spawn(_render_plan_job, req)
                self._send(200, json.dumps({'ok': True, 'runid': nrunid}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/instruct':
            try:
                length = int(self.headers.get('Content-Length', 0))
                if length > 300 * 1024 * 1024:
                    self._send(200, json.dumps({'ok': False, 'error': '请求过大'}).encode('utf-8'), 'application/json')
                    return
                req = self._read_json(length) or {}
                runid = self._spawn(dispatch_instruct, req)
                self._send(200, json.dumps({'ok': True, 'runid': runid}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        self._send(404, b'not found')


def start_server(port=8765, open_browser=True):
    os.makedirs(WORKDIR, exist_ok=True)
    os.makedirs(OUTDIR, exist_ok=True)
    ensure_default_images()
    host = os.environ.get('HOST', '127.0.0.1')
    srv = ThreadingHTTPServer((host, port), Handler)
    url = f'http://{host}:{port}/'
    print('=' * 52)
    print('  [Spring Video Studio] started')
    print('  Open in browser:', url)
    print('  Press Ctrl+C to stop')
    print('=' * 52, flush=True)
    if open_browser and host in ('127.0.0.1', 'localhost'):
        threading.Timer(0.7, lambda: webbrowser_open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def webbrowser_open(url):
    import webbrowser
    try:
        webbrowser.open(url)
    except Exception:
        pass


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    ensure_deps()
    port = int(os.environ.get('PORT', '8765'))
    start_server(port)