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
import os, sys, json, math, random, re, shutil, subprocess, threading, time, base64, itertools, atexit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
WORKDIR = os.path.join(HERE, 'webui_workspace')
PROGRESS = {}          # runid -> mutable progress dict for the UI poller
PLANS = {}             # runid -> 人机协同「规划方案」（分析结果，等待用户确认/微调后再渲染）
import threading as _threading
OUTDIR = os.path.join(HERE, 'webui_output')
PROGRESS_FILE = os.path.join(OUTDIR, 'progress_state.json')  # 进度持久化（服务重启不丢失）


def _save_progress():
    """把 PROGRESS 快照写入磁盘（每5秒由后台线程调用，服务重启后可恢复）。"""
    try:
        snap = {}
        for rid, p in PROGRESS.items():
            if isinstance(p, dict):
                snap[rid] = {k: v for k, v in p.items() if k != '_thread'}
        with open(PROGRESS_FILE, 'w', encoding='utf-8') as f:
            json.dump(snap, f, ensure_ascii=False, default=str)
    except Exception:
        pass


def _load_progress():
    """启动时从磁盘恢复 PROGRESS（只恢复已完成/失败的，运行中的标记为中断）。"""
    try:
        if os.path.isfile(PROGRESS_FILE):
            with open(PROGRESS_FILE, 'r', encoding='utf-8') as f:
                snap = json.load(f)
            for rid, p in snap.items():
                if isinstance(p, dict) and rid not in PROGRESS:
                    if not p.get('done') and not p.get('error'):
                        p['error'] = '服务重启，任务已中断（成片文件仍在磁盘上，可在记录中下载）'
                        p['done'] = True
                    PROGRESS[rid] = p
    except Exception:
        pass


def _start_progress_saver():
    """后台线程：每5秒持久化一次 PROGRESS。"""
    def _loop():
        while True:
            try:
                _save_progress()
            except Exception:
                pass
            time.sleep(5)
    t = _threading.Thread(target=_loop, daemon=True, name='progress-saver')
    t.start()
RUN_PROCS = {}          # runid -> 当前活跃的 ffmpeg Popen（用于取消时终止）
_PROC_LOCK = threading.Lock()
_TLS = threading.local()   # 每个任务线程绑定自己的 runid，供 ffmpeg_run 读取
_RUN_CTR = itertools.count(1)   # 原子自增的 run id 计数器（原来的 RUNSEQ[0] += 1 非原子，多线程下可能撞号）


def _max_concurrent_tasks():
    """任务并发上限：默认 2，可用环境变量 MAX_CONCURRENT_TASKS 覆盖；非法值退回默认。"""
    try:
        v = int(os.environ.get('MAX_CONCURRENT_TASKS', '') or 2)
    except ValueError:
        v = 2
    return max(1, v)


# ffmpeg 是 CPU / 内存大户，放任并发会把机器打满、多个任务互相拖慢到不可用。
# 超限采用「直接拒绝并提示」而不是无限排队——排队没有任何可见反馈，用户只会以为卡死。
_MAX_CONCURRENT_TASKS = _max_concurrent_tasks()
_TASK_SEM = threading.Semaphore(_MAX_CONCURRENT_TASKS)

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
# ---------------------------------------------------------------------------
# 中文字体解析（跨平台探测 · 缺字形校验 · 不静默降级）
#
# 历史硬伤：这里曾是 FONT_PATH = "C:/Windows/Fonts/msyh.ttc"，且加载失败时
# `except: ImageFont.load_default()` —— 该默认字体**不含中文字形**且**不报错**，
# 于是 macOS/Linux/Docker 上烧字幕与封面标题会静默变成「豆腐块」，用户只看到
# 一堆方框却无从得知原因（README 当时还宣称 Docker 镜像已装好中文字体）。
# 现策略：显式探测 → 逐字校验是否真含中文字形 → 找不到就抛 FontMissingError
# 并给出可执行的修复指引。宁可明确失败，也不产出不可读的视频。
#
# 待办（合规）：微软雅黑版权归方正/微软，渲染进公开发布的商业视频存在授权风险，
# 后续拟换成 SIL OFL 协议字体（思源黑体 Noto Sans SC）随仓库分发。探测顺序已
# 为「仓库自带 assets/fonts/」预留优先级，换字体时只需丢文件进去，不用改代码。
# ---------------------------------------------------------------------------
FONT_ENV = 'SPRING_VIDEO_FONT'                      # 环境变量：显式指定字体文件
FONT_DIR = os.path.join(HERE, 'assets', 'fonts')    # 仓库自带字体目录（建议放 OFL 字体）
_CJK_SAMPLE = '中文字幕测试'                          # 字形校验采样
_PUA_PROBE = '\ue000'        # 私用区码位：常规字体必然缺失，用作「缺字形」基准位图
_FONT_LOCK = threading.Lock()
_FONT_CACHE = {'checked': False, 'path': '', 'reason': ''}
_font_cache_by_size = {}


def _font_has_cjk(path, size=48):
    """判断字体文件是否**真的**含中文字形（只看文件存在与否是不够的）。

    原理：把每个采样汉字渲染为位图，与「必然缺失」的私用区码位位图逐一比对 ——
    完全相同说明该字落到了 .notdef（缺字形）。实测可正确区分
    微软雅黑/宋体（含中文）与 Arial / Segoe UI / Times / Pillow 默认字体（不含中文）。
    个别字体（如 simsun）连 .notdef 都不绘制，此时退化为「是否画出任何笔画」判定。
    """
    try:
        f = ImageFont.truetype(path, size)
    except Exception:
        return False

    def _bmp(ch):
        img = Image.new('L', (size * 3, size * 3), 0)
        ImageDraw.Draw(img).text((8, 8), ch, font=f, fill=255)
        return img.tobytes()

    try:
        miss = _bmp(_PUA_PROBE)
        if not any(miss):                       # 该字体不绘制 .notdef（空白）
            return all(any(_bmp(ch)) for ch in _CJK_SAMPLE)
        return all(_bmp(ch) != miss for ch in _CJK_SAMPLE)
    except Exception:
        return False


# 各平台常见中文字体（按优先级）
_FONT_CANDIDATES = {
    'win32': ['C:/Windows/Fonts/msyh.ttc', 'C:/Windows/Fonts/msyhl.ttc',
              'C:/Windows/Fonts/msyhbd.ttc', 'C:/Windows/Fonts/simhei.ttf',
              'C:/Windows/Fonts/simsun.ttc', 'C:/Windows/Fonts/Deng.ttf'],
    'darwin': ['/System/Library/Fonts/PingFang.ttc',
               '/System/Library/Fonts/STHeiti Medium.ttc',
               '/System/Library/Fonts/STHeiti Light.ttc',
               '/System/Library/Fonts/Hiragino Sans GB.ttc',
               '/Library/Fonts/Arial Unicode.ttf',
               '/Library/Fonts/Noto Sans CJK SC Regular.otf',
               os.path.expanduser('~/Library/Fonts/Noto Sans SC Regular.otf')],
}
_FONT_CANDIDATES['linux'] = [
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf',   # Debian 12+ / Ubuntu 22.04+
    '/usr/share/fonts/opentype/noto/NotoSansSC-Regular.otf',      # 上游 Noto Sans SC（OFL）
    '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc',
    '/usr/share/fonts/truetype/wqy/wqy-microhei.ttc',
    '/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf',
    '/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf',
    '/usr/share/fonts/truetype/arphic/uming.ttc',
    os.path.expanduser('~/.fonts/NotoSansSC-Regular.otf'),
    os.path.expanduser('~/.local/share/fonts/NotoSansSC-Regular.otf'),
]
# 候选都没命中时的兜底：扫描系统字体目录（限流，避免启动拖慢）
_FONT_SCAN_DIRS = {
    'win32': ['C:/Windows/Fonts'],
    'darwin': ['/System/Library/Fonts', '/Library/Fonts',
               os.path.expanduser('~/Library/Fonts')],
    'linux': ['/usr/share/fonts', '/usr/local/share/fonts',
              os.path.expanduser('~/.fonts'), os.path.expanduser('~/.local/share/fonts')],
}
_FONT_SCAN_MAX_FILES = 400      # 最多校验这么多字体文件
_FONT_SCAN_DEADLINE = 8.0       # 或最多花这么多秒


def _platform_key():
    p = (sys.platform or '').lower()
    if p.startswith('win'):
        return 'win32'
    if p.startswith('darwin'):
        return 'darwin'
    return 'linux'


def _iter_bundled_fonts():
    try:
        for fn in sorted(os.listdir(FONT_DIR)):
            if fn.lower().endswith(('.ttf', '.otf', '.ttc')):
                yield os.path.join(FONT_DIR, fn)
    except OSError:
        return


def _scan_system_fonts(key, deadline_ts):
    """遍历系统字体目录找第一个含中文字形的字体（限文件数与时间）。"""
    n = 0
    for root in _FONT_SCAN_DIRS.get(key, []):
        if not os.path.isdir(root):
            continue
        for dirpath, _dn, filenames in os.walk(root):
            for fn in sorted(filenames):
                if not fn.lower().endswith(('.ttf', '.otf', '.ttc')):
                    continue
                n += 1
                if n > _FONT_SCAN_MAX_FILES or time.time() > deadline_ts:
                    return ''
                p = os.path.join(dirpath, fn)
                if _font_has_cjk(p):
                    return p
    return ''


def _resolve_cjk_font(force=False):
    """惰性解析中文字体路径，结果进程内缓存。返回 '' 表示没找到（附 reason）。"""
    with _FONT_LOCK:
        if _FONT_CACHE['checked'] and not force:
            return _FONT_CACHE['path']
        key = _platform_key()
        found, notes = '', []
        deadline = time.time() + _FONT_SCAN_DEADLINE

        env = (os.environ.get(FONT_ENV) or '').strip()
        if env:
            if os.path.isfile(env) and _font_has_cjk(env):
                found = os.path.abspath(env)
            else:
                notes.append(f'环境变量 {FONT_ENV} 指定的字体不可用或不含中文字形：{env}')
        if not found:
            for p in _iter_bundled_fonts():     # 仓库自带（OFL 字体放这里即可，无需改代码）
                if _font_has_cjk(p):
                    found = p
                    break
        if not found:
            for p in _FONT_CANDIDATES.get(key, []):
                if os.path.isfile(p) and _font_has_cjk(p):
                    found = p
                    break
        if not found:
            found = _scan_system_fonts(key, deadline)
        if not found and not notes:
            notes.append('已检查 环境变量 / assets/fonts / 系统常见中文字体 / 系统字体目录，均未找到含中文字形的字体')
        _FONT_CACHE.update(checked=True, path=found, reason='；'.join(notes))
        global FONT_PATH
        FONT_PATH = found or ''
        return _FONT_CACHE['path']


class FontMissingError(RuntimeError):
    """找不到含中文字形的字体。绝不静默降级为不含中文的字体（那会产出豆腐块）。"""


def font_missing_help():
    """缺字体时的可执行修复指引（各平台安装命令 + 两种免安装兜底）。"""
    return (
        '未找到含中文字形的字体，已中止渲染 —— 不会生成「豆腐块」字幕/封面标题。'
        f'（{_FONT_CACHE.get("reason") or "系统无可用中文字体"}）\n'
        '任选一种方式修复：\n'
        '  1) 安装开源中文字体（推荐思源黑体 Noto Sans SC，SIL OFL 协议，可商用）：\n'
        '     · Debian/Ubuntu : sudo apt-get install -y fonts-noto-cjk\n'
        '     · Alpine(Docker): apk add --no-cache font-noto-cjk\n'
        '     · CentOS/RHEL   : sudo yum install -y google-noto-sans-cjk-fonts\n'
        '  2) 把字体文件（.ttf/.otf/.ttc）放进项目的 assets/fonts/ 目录后重启\n'
        f'  3) 用环境变量指定：{FONT_ENV}=/path/to/NotoSansSC-Regular.otf'
    )


def cjk_font(size):
    """加载中文字体。找不到含中文字形的字体时抛 FontMissingError（而非静默降级）。"""
    p = _resolve_cjk_font()
    if not p:
        raise FontMissingError(font_missing_help())
    with _FONT_LOCK:
        f = _font_cache_by_size.get(size)
    if f is None:
        try:
            f = ImageFont.truetype(p, size)
        except Exception as e:
            raise FontMissingError(f'字体加载失败：{p}（{e}）。{font_missing_help()}')
        with _FONT_LOCK:
            _font_cache_by_size[size] = f
    return f


def font_selfcheck():
    """启动自检：返回 (ok, 说明文本)。缺字体时给出警告而非让用户在成片里踩坑。"""
    p = _resolve_cjk_font()
    if p:
        return True, f'中文字体：{p}'
    return False, '[警告] ' + font_missing_help().replace('\n', '\n        ')


FONT_PATH = ''   # 由 _resolve_cjk_font() 惰性填充；保留名字仅为向后兼容
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

def _atomic_write_json(path, obj, indent=2):
    """原子写 JSON：先写同目录 .tmp，再 os.replace 覆盖。

    旧实现 open(path,'w') 会先截断原文件：写入过程中进程被杀 / 磁盘满 / 被占用，
    文件就变成半截 JSON。下一次 load 静默解析失败返回空，随后任何一次写都以空为基底
    覆盖回去 —— 用户表现为「历史记录全没了 / 配置全空了」，且无法恢复。
    os.replace 在同一卷上是原子的：要么读到旧内容，要么读到新内容，不存在中间态。"""
    directory = os.path.dirname(os.path.abspath(path)) or '.'
    os.makedirs(directory, exist_ok=True)
    tmp = os.path.join(directory, '.' + os.path.basename(path) + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return obj


def save_ai_config(cfg):
    return _atomic_write_json(AI_CONFIG_PATH, cfg)


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


def _strip_think(text):
    """剥离 Qwen3 等混合思考模型输出中的 <think>…</think> 段（含未闭合残段），
    保证解说稿不被思考文本污染。无思考段时原样返回。"""
    import re as _re
    if not text:
        return text
    out = _re.sub(r'<think>.*?</think>', '', text, flags=_re.S)
    out = _re.sub(r'<think>.*$', '', out, flags=_re.S)   # 未闭合残段：丢弃其后全部内容
    return out.strip()


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


# ---------------------------------------------------------------------------
# Whisper (本地 ASR) 模型配置：可切换 tiny/base/small/medium/large，权重缓存进项目目录
# ---------------------------------------------------------------------------
_WHISPER_MODELS = ['tiny', 'base', 'small', 'medium', 'large-v3', 'distil-large-v3']

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
    m = (cfg.get('model') or 'distil-large-v3')
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
    'distil-large-v3': 'Systran/faster-distil-whisper-large-v3',
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
        return _strip_think((data.get('message') or {}).get('content', ''))
    return _strip_think((data.get('choices') or [{}])[0].get('message', {}).get('content', ''))


VLM_PULL = {'model': None, 'running': False, 'ok': None, 'msg': '', 'pct': 0}
VLM_FAST_GGUF_SOURCES = {

    'qwen3-vl:8b': (

        'https://hf-mirror.com/Qwen/Qwen3-VL-8B-Instruct-GGUF/resolve/main/Qwen3VL-8B-Instruct-Q4_K_M.gguf',

        'Qwen3VL-8B-Instruct-Q4_K_M.gguf',

        'https://hf-mirror.com/Qwen/Qwen3-VL-8B-Instruct-GGUF/resolve/main/mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf',

        'mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf'),

}




def vlm_pull_async(model=None):
    """后台异步执行 `ollama pull <model>`（避免阻塞请求）。通过 /api/vlm/status 轮询。"""
    m = model or vlm_cfg()['model']
    running = _model_pull_running()
    if running:
        return False, '已有模型在后台下载（%s），请等它完成后再试' % running
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
        # 加速通道：白名单模型走 hf-mirror GGUF + aria2c + ollama create（含视觉投影 mmproj）

        spec = VLM_FAST_GGUF_SOURCES.get(model)

        if spec:

            _fast_pull_local(model, spec=spec, slot=VLM_PULL)

            return

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

def _model_pull_running():
    """任一模型（写稿/看图）正在后台下载时返回其名称；否则 None。
    同时下载两个大模型会互相抢带宽、进度条也互相干扰——统一串行。"""
    if LOCAL_PULL.get('running'):
        return LOCAL_PULL.get('model') or '本地模型'
    if VLM_PULL.get('running'):
        return VLM_PULL.get('model') or '视觉模型'
    return None


def local_pull_async(model=None, force=False):
    """后台异步执行 `ollama pull <model>`（文字解说模型）。通过 /api/local/status 轮询。
    若模型已存在则跳过重复拉取（手动 ollama create 导入的模型与官方打包版 manifest 不同，
    直接 ollama pull 会误判为需整包重下，浪费带宽）。"""
    m = model or local_llm_cfg()['model']
    running = _model_pull_running()
    if running:
        return False, '已有模型在后台下载（%s），请等它完成后再试' % running
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
    'qwen3:14b-q4_K_M': ('https://hf-mirror.com/bartowski/Qwen_Qwen3-14B-GGUF/resolve/main/Qwen_Qwen3-14B-Q4_K_M.gguf', 'Qwen_Qwen3-14B-Q4_K_M.gguf'),

    'qwen2.5:latest': ('https://hf-mirror.com/bartowski/Qwen2.5-7B-Instruct-GGUF/resolve/main/Qwen2.5-7B-Instruct-Q4_K_M.gguf', 'qwen2.5-7b-instruct-q4_k_m.gguf'),
}


def _fast_pull_local(model, spec=None, slot=None):
    """加速通道：下载单文件 GGUF（可含 mmproj 视觉投影）+ aria2c 多线程 + ollama create 导入。
    spec: (url, 文件名) 或 (url, 文件名, mmproj_url, mmproj文件名)；缺省查 FAST_GGUF_SOURCES。
    slot: 进度槽（LOCAL_PULL / VLM_PULL）。返回 (ok, msg)。失败返回 (False, 原因)，由调用方回退官方源。"""
    import subprocess as _sp, urllib.request, shutil, time as _t, re as _re
    src = spec or FAST_GGUF_SOURCES.get(model)
    if not src:
        return False, '该模型没有内置加速源'
    slot = slot or LOCAL_PULL
    m_url, m_fname = src[0], src[1]
    p_url = src[2] if len(src) > 2 else None
    p_fname = src[3] if len(src) > 3 else None
    aria = shutil.which('aria2c')
    if not aria:
        return False, '未找到 aria2c（多线程下载器），可安装 aria2 后重试，或改用官方源'
    dl_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_dl')
    os.makedirs(dl_dir, exist_ok=True)
    def _pull_one(u, fn, lo, hi):
        # 单文件下载（-c 断点续传；已有完整文件则跳过），进度按 lo~hi 区间映射
        total = 0
        try:
            _req = urllib.request.Request(u)
            _req.add_header('Range', 'bytes=0-0')
            with urllib.request.urlopen(_req, timeout=30) as _r:
                _cr = _r.headers.get('Content-Range', '') or ''
                if '/' in _cr:
                    total = int(_cr.split('/')[-1])
        except Exception:
            pass
        tgt = os.path.join(dl_dir, fn)
        if os.path.exists(tgt) and (not total or os.path.getsize(tgt) >= total):
            return True
        pp = _sp.Popen([aria, '-c', '-x', '8', '-s', '8', '-k', '1M', '--max-tries=0',
                        '--retry-wait=3', '--timeout=60', '--console-log-level=warn',
                        '-o', fn, u], cwd=dl_dir,
                       stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
        while pp.poll() is None:
            if total and os.path.exists(tgt):
                slot['pct'] = min(hi, lo + int(os.path.getsize(tgt) * (hi - lo) // max(1, total)))
            _t.sleep(1)
        return pp.returncode == 0

    if not _pull_one(m_url, m_fname, 0, 85):
        return False, 'aria2c 下载失败，已回退官方源'
    if p_url:
        if not _pull_one(p_url, p_fname, 85, 92):
            return False, '视觉投影（mmproj）下载失败'
    # ollama create 导入（cmd /c 规避 PowerShell 对 stderr 进度条的误判；Modelfile 用绝对路径）
    mf = os.path.join(dl_dir, 'Modelfile_' + _re.sub(r'[^0-9A-Za-z]', '_', model))
    with open(mf, 'w', encoding='utf-8') as f:
        f.write('FROM ' + os.path.join(dl_dir, m_fname).replace('\\', '/') + '\n')
        if p_fname:

            f.write('FROM ' + os.path.join(dl_dir, p_fname).replace('\\', '/') + '\n')

    try:
        r = _sp.run(['cmd', '/c', 'ollama', 'create', model, '-f', mf],
                    capture_output=True, text=True, timeout=1200)
    except Exception as e:
        return False, 'ollama create 失败：%s' % str(e)[:120]
    finally:
        for _f in (os.path.join(dl_dir, m_fname), mf):
            try:
                os.remove(_f)
            except Exception:
                pass
    if r.returncode != 0:
        return False, 'ollama create 失败：%s' % ((r.stdout or r.stderr or '')[-200:])
    return True, '加速通道完成：%s' % m_fname


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
    # 均匀采样 8 帧：首尾必采、中间等距，确保长视频后面的画面也被模型看到
    # （旧实现 idxs[::step][:8] 只取前 8 个采样点，长视频末尾 10%+ 的画面完全缺失）
    n_pick = min(8, len(idxs))
    if n_pick > 1:
        pick_idx = [idxs[int(i * (len(idxs) - 1) / (n_pick - 1))] for i in range(n_pick)]
    else:
        pick_idx = [idxs[0]]
    picked = [frames[i] for i in pick_idx]
    if not picked:
        return None
    # 台词也均匀采样覆盖全片（最多 12 段），首尾必采；旧实现只取前 12 段，后面剧情的台词全部丢失
    n_dlg = min(12, len(per_seg))
    if n_dlg > 1:
        dlg_idx = [int(i * (len(per_seg) - 1) / (n_dlg - 1)) for i in range(n_dlg)]
    else:
        dlg_idx = [0]
    dlg = []
    for j in dlg_idx:
        s0, s1, txt = per_seg[j]
        t = (txt or '').strip()
        dlg.append('%s-%s秒 台词：%s' % (int(s0), int(s1), t[:100] if t else '(无台词画面)'))
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


def _split_nar_clauses(line):
    """二级切分：句内再按逗号/分号/顿号切小句（句子数仍不够镜头数时兜底）。"""
    import re as _re
    parts = _re.split(r'(?<=[，,；;、])', line)
    return [p.strip() for p in parts if p.strip()]


def _split_into_k(text, k):
    """把一段文字尽量均分成 k 份，优先在标点处断开（极端兜底：只有一句话却要覆盖多个镜头）。"""
    if k <= 1 or not text:
        return [text]
    n = len(text)
    cuts, prev = [], 0
    for i in range(1, k):
        want = prev + max(1, round((n - prev) / (k - i + 1)))
        pos = None
        for j in range(min(want + 6, n - 1), max(prev + 1, want - 6), -1):
            if text[j - 1] in '。！？!?，,；;、':
                pos = j
                break
        if pos is None:
            pos = min(n - 1, max(prev + 1, want))
        cuts.append(pos)
        prev = pos
    out, last = [], 0
    for c in cuts:
        out.append(text[last:c].strip())
        last = c
    out.append(text[last:].strip())
    return [x for x in out if x] or [text]


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
    """把整稿行映射回 n 个镜头：行数相等直接一一对应；不足时按句 → 小句 → 字数逐级拆细后均匀分布；过多则线性就近取行。

    ⚠️ 关键不变量：**绝不把同一句原文复制给所有镜头**。旧实现在「模型只输出 1 行且句数不足」
    时会走进兜底循环，让 n 段拿到完全相同的解说——表现为「整片解说只有一句话反复出现」。
    现改为逐级拆细：按句 → 按逗号小句 → 按字数均分，尽量让每段拿到不同内容。"""
    if n <= 0:
        return []
    m = len(lines)
    if m == 0:
        return ['' for _ in range(n)]
    if m == n:
        return list(lines)

    # ① 句子池
    sents = []
    for l in lines:
        sents.extend(_split_nar_sentences(l))

    # ② 句数不够 → 再按小句（逗号/分号/顿号）拆
    if len(sents) < n:
        clauses = []
        for s in sents:
            clauses.extend(_split_nar_clauses(s))
        if len(clauses) > len(sents):
            sents = clauses

    # ③ 仍不够 → 反复把最长的可断条目在中点附近的标点处劈开，直到凑够 n 条。
    #    只在标点处断开、绝不硬切字符——否则会把「便利店」劈成「便利」+「店想」这类半个词。
    guard = 0
    while len(sents) < n and guard < 256:
        guard += 1
        best = None
        for i, s in enumerate(sents):
            if len(s) < 12:
                continue
            half = len(s) // 2
            for j in range(half, len(s) - 3):
                if s[j] in '，,；;、。！？!?':
                    if best is None or len(s) > len(sents[best[0]]):
                        best = (i, j + 1)
                    break
        if best is None:
            break
        i, cut = best
        a, b = sents[i][:cut].strip(), sents[i][cut:].strip()
        if not a or not b:
            break
        sents[i:i + 1] = [a, b]

    if len(sents) >= n:
        return _distribute_sents(sents, n)

    # ④ 极端兜底：只剩一句。若这句够长就按字数均分（每段 >=8 字才切，避免切成「这是一/个男子在」
    #    这种读不通的碎片）；太短则原样重复——宁可重复，也不产出看不懂的半截话。
    if len(sents) == 1 and len(sents[0]) >= max(8 * n, 16):
        return _distribute_sents(_split_into_k(sents[0], n), n)

    out = []
    for i in range(n):
        out.append(sents[i % len(sents)])
    return out


def _seg_visual_captions(frames, per_seg, params, max_seg=14):
    """逐段画面描述：把各段的中间帧按时间顺序一次性交给 VLM，
    要求按「第k段: 画面内容」逐段输出——这是解说稿贴合画面的地基：
    写稿模型拿到每段「实际可见的内容」后，才不会把后面的事件提前讲到前面。
    返回 {段下标(0基): 画面描述}；VLM 不可用/失败返回 {}。"""
    import re as _re
    n = len(per_seg)
    # 段数超限时均匀采样覆盖全片（首尾必采），而不是只取前 max_seg 段——
    # 旧实现只看前 14 段，长视频后面的画面完全没被模型描述，解说词不贴合后面的画面。
    if n <= max_seg:
        pick_idx = list(range(n))
    else:
        pick_idx = sorted(set(int(i * (n - 1) / (max_seg - 1)) for i in range(max_seg)))
    imgs = [(i, frames[i]) for i in pick_idx if frames.get(i)]
    if not imgs:
        return {}
    img_list = [p for _, p in imgs]
    sys_ = '你是视频画面描述助手，只描述画面里实际可见的内容。'
    prompt = ('下面是同一段视频按时间顺序抽取的各镜头画面：第1张对应第1段，第2张对应第2段……依此类推。'
              '请严格按顺序逐段输出每段画面里实际可见的内容（人物/动作/场景/屏幕文字）。'
              '每段一行，格式严格为「第k段: 画面内容」，k 从 1 开始递增。'
              '只描述看到的，不要推测剧情、不要总结、不要遗漏任何一段。')
    try:
        out = vlm_chat_multi(img_list, prompt, system=sys_, timeout=240) or ''
    except Exception:
        return {}
    caps = {}
    for l in out.splitlines():
        l = l.strip()
        m = _re.match(r'^第(\d+)段[:：]\s*(.+)$', l)
        if m:
            k = int(m.group(1))
            if 1 <= k <= len(imgs):
                orig_i = imgs[k - 1][0]
                caps[orig_i] = m.group(2).strip()[:70]
    return caps


# ---------------------------------------------------------------------------
# 题材化模板：六套标准解说结构（悬疑/爱情/恐怖/动作/喜剧/历史）。
# 解说工业化流水线：拿到任何片子先定型，再按模板的钩子公式/结构节奏/升华方向填细节。
# 来源：用户提供的实战方法论（无损压缩注入 prompt）。
# ---------------------------------------------------------------------------
NARR_STYLES = {
    'movie':    {'label': '电影解说', 'system': '你是坐拥百万粉丝的资深影视解说博主——拼的是认知差与情绪共振，带观众用更高视角看故事，口气自然像唠嗑。'},
    'science':  {'label': '科普讲解', 'system': '你是深入浅出的科普讲解者——用通俗语言讲清原理和逻辑，数据准确，不夸张不煽情，让观众听懂并记住。'},
    'funny':    {'label': '搞笑吐槽', 'system': '你是幽默风趣的吐槽博主——语言轻松有梗，节奏明快，用调侃和反差制造笑点，但不低俗不冒犯。'},
    'suspense': {'label': '悬疑解读', 'system': '你是擅长制造悬念的解说者——节奏沉稳，层层铺垫，留钩子引导观众思考，语气克制有张力。'},
}
DETAIL_LEVELS = {'detailed': 1.30, 'balanced': 1.0, 'concise': 0.70}


GENRE_TEMPLATES = {
    'suspense': {'name': '悬疑/烧脑/反转',
        'focus': '讲诡计的设计逻辑与视角的欺骗性，不做凶手是谁的复读机',
        'hook': '结果前置 + 灵魂拷问。公式例：如果告诉你，主角在开场第 X 分钟就死了，你信吗？别急——你亲眼看见的真相全是假的。',
        'structure': '铺垫期快速讲清规则（循环/密室/狼人杀规则，不讲细枝末节）；搅局期抛第一个反常点（为什么只有主角记得昨天）；破局期强行剧透式反转（镜头回放第 3 遍你会发现，导演早就在背景的报纸上写了答案）',
        'ending': '升维到诡计背后的动机（为了爱？为了阶层跨越？）。金句方向：比悬疑更难的，是算尽人心；比算尽人心更难的，是承认自己也在局中。',
        'tone': '冷静克制，突出设计感'},
    'romance': {'name': '爱情/青春/文艺',
        'focus': '不说剧情，说情绪颗粒度——把情节翻译成观众共情的恋爱瞬间',
        'hook': '痛点代入 + 细节特写。公式例：你上一次删掉聊天记录是什么时候？这部片里的男女主，删了整整 7 年。他们删掉的不是争吵，而是……',
        'structure': '相遇期极简交代相识，重点写第一次心动的小动作（慢、轻）；拉扯期不讲大冲突，讲错过的时间差（他想复合时她刚死心）；结局期不强求圆满、放大遗憾（BE 就放慢，解说停一下补一句轻话）',
        'ending': '金句要像朋友圈文案。方向：爱情经得起风雨，却经不起平凡。',
        'tone': '慢、轻、克制'},
    'horror': {'name': '恐怖/惊悚/心理阴暗',
        'focus': '放弃高能预警，讲无处可逃的日常感',
        'hook': '环境沉浸 + 反常理行为。公式例：关掉灯，戴上耳机。接下来的 3 分钟，你会觉得你家的衣柜、床底都有东西在盯着你——这部片的恐怖在于：鬼从来不敲门。',
        'structure': '日常崩坏期极平淡地介绍主角的普通生活（越平淡后续越瘆人）；异常累积期用短促断句造压迫（他动了。她没醒。墙上的照片，眼睛闭上了。）；源头揭秘期讲心理创伤的外化（鬼是内疚的化身），不说鬼怪长相',
        'ending': '吓完要治愈或警告。方向：比鬼更可怕的，是走不出的心魔；比心魔更可怕的，是你明明醒着，却动不了。',
        'tone': '短促断句、压迫感'},
    'action': {'name': '动作/科幻/超英/灾难',
        'focus': '不讲打斗过程，讲世界观设定和代价',
        'hook': '脑洞设定 + 极致假设。公式例：如果给你一件战甲，但每穿一次就少活一天，你穿还是不穿？这部片告诉你什么叫能力越大，债务越多。',
        'structure': '设定期用最通俗的比喻讲清世界观；升级期跳过所有小喽啰打斗，只讲 BOSS 战的破局逻辑（为什么打不过？找到了什么漏洞？）；代价期强调牺牲（赢了，但失去了什么）',
        'ending': '热血燃向。方向：所谓英雄，不是不会害怕，而是双腿发抖，却依然挡在普通人前面。',
        'tone': '热血、节奏快'},
    'comedy': {'name': '喜剧/荒诞/黑色幽默',
        'focus': '把画面里的尴尬翻译成屏幕前的哈哈大笑，语调欢脱、带点阴阳怪气',
        'hook': '高能名场面截取。公式例：建议别看这部片，真的会笑到邻居来敲门——尤其男主在殡仪馆点外卖那段，把葬礼整成了相声现场。',
        'structure': '人设期用夸张标签给主角贴标签（抠门鬼/倒霉蛋）；连环期讲多米诺骨牌式连锁倒霉，语速加快用排比（刚躲过追债的，撞上前女友；刚甩掉前女友，亲爹把他存款捐了）；笑中带泪，结尾突然收住笑，点出小人物心酸',
        'ending': '方向：所谓喜剧，就是把人生的烂摊子，扎成一束捧花。',
        'tone': '欢脱、嘴碎'},
    'history': {'name': '历史/战争/史诗/传记',
        'focus': '放弃宏观大词，凝视大时代下的小人物——不谈战役意义，谈那场雪有多冷、那封信有多重',
        'hook': '个人命运与时代冲撞。公式例：历史书上那行冰冷的伤亡数字，在这部片里，变成了一个母亲等了 20 年的空碗。这部战争片没有英雄，只有一群想活命的普通人。',
        'structure': '背景期仅 1 句交代年代，极速切入主角日常（种地/织布/上课）；浪潮期讲抉择的被动性（不是他们选择了战争，是战争闯进了家），解说沉重；长镜头期找一个最经典的无言画面，此处全篇不给一句解说词，只放背景音让画面说话',
        'ending': '必须拔高立意、关联当下和平。方向：我们并非生在一个和平的年代，只是生在一个和平的国家。忘了历史，就是第二次背叛。',
        'tone': '沉重、凝视'},
}

def _detect_genre(plot):
    """根据剧情梗概判断题材类型（一次轻量文字调用）；失败返回空串。"""

    if not plot or not local_llm_enabled():
        return ''
    names = '、'.join(v['name'] for v in GENRE_TEMPLATES.values())
    try:
        out = local_llm_chat('这段视频的剧情梗概：' + str(plot)[:600] + '。它最适合按哪类题材解说？只回答其中一个类型名（' + names + ' 之一），不要任何其他内容。',
                              system='只输出类型名。', timeout=60)
    except Exception:
        return ''
    out = (out or "").strip()
    for k, v in GENRE_TEMPLATES.items():
        if v['name'] in out or k in out:
            return k
    return ''

def _genre_template_block(key):
    """按题材 key 生成写稿 prompt 的模板规则块；未知/空返回空串。"""

    t = GENRE_TEMPLATES.get(key or "")
    if not t:
        return ""
    return chr(10).join([
        '【题材模板·' + t['name'] + '】本片按以下结构写（模板优先于默认写法）：',
        '- 侧重点：' + t['focus'] + '；',
        '- 开篇钩子公式：' + t['hook'] + '；',
        '- 结构节奏：' + t['structure'] + '；',
        '- 结尾升华方向：' + t['ending'] + '；',
        '- 语调：' + t['tone'] + '。',
    ])


def local_vlm_narrate(per_seg, frames, params, plot=None, beat_outline=None):
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
    if plot is None and frames:
        plot = _plot_brief(frames, per_seg, params)
    if beat_outline is not None:
        # 复用分段层已规划好的主线/过渡，避免重复调用模型
        beats = [{'i': i + 1, 'importance': o.get('importance', 'advance'), 'role': ''}
                 for i, o in enumerate(beat_outline)]
        summary = ''
    else:
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
    _style = NARR_STYLES.get(params.get('narr_style', 'movie'), NARR_STYLES['movie'])
    sys_ = _style['system']
    req_line = '- 你的额外要求：%s\n' % req if req else ''

    # —— 整稿生成：像真人一样把故事从头讲到尾 ——
    # 逐段画面描述（写稿地基）：让模型知道每一段画面里实际有什么，
    # 否则它只能按剧情梗概想象——事件会漂移，把后面发生的事提前讲到前面
    seg_vis = _seg_visual_captions(frames, per_seg, params)
    # 逐段画面描述（写稿地基）：让模型知道每一段画面里实际有什么，
    # 否则它只能按剧情梗概想象——事件会漂移，把后面发生的事提前讲到前面
    seg_vis = _seg_visual_captions(frames, per_seg, params)
    seg_brief = []
    for i, (s0, s1, txt) in enumerate(per_seg):
        b = beats[i] if i < len(beats) else {}
        imp = b.get('importance', 'advance')
        tag = {'key': '（关键/高光，可展开）', 'transition': '（过渡）', 'mood': '（氛围）'}.get(imp, '')
        t = (txt or '').strip()[:80]
        # 带上「建议字数」：模型按画面时长决定写多长，配音才念得完也填得满（贴合时间轴）
        lo, hi = _target_chars((s1 - s0) * DETAIL_LEVELS.get(params.get('detail_level', 'balanced'), 1.0))
        vis = ('画面：' + seg_vis[i]) if i in seg_vis else ''
        seg_brief.append('第%d段 %s[建议%d~%d字] %s %s'
                         % (i + 1, tag, lo, hi, ('台词：' + t if t else '无台词画面'), vis))
    # 题材模板：用户显式选择优先；自动则按剧情梗概轻量判型（失败则不套模板）
    genre = (params.get('genre') or 'auto').strip()
    if genre not in GENRE_TEMPLATES or genre == 'auto':
        genre = _detect_genre(plot) or ''
    genre_block = _genre_template_block(genre)
    ctx = []
    if genre_block:
        ctx.append(genre_block)
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
              + '- 【黄金7秒·钩子开篇】第 1 行是生死线：用反常理悬念或人性拷问破题'
                '（示范："如果你回到1939年，敢不敢向整个德国撒谎？"），'
                '严禁"今天讲一部关于XX的电影"式平淡开场；\n'
              + '- 【认知提炼·拒绝流水账】解说≠复述：只讲推动人物命运转折的情节、揭示人性底色的细节，'
                '过场动作全部砍掉；每一行都要给观众一个"他们自己看不出来的信息差"或情绪点；\n'
              + '- 【画面为准，顺序不漂移】每段都标了该段「画面：」的实际内容——这一行的解说必须以这段画面为基础展开；'
              + '- 【万能衔接】想不出过渡时可用（每片最多 1 次）：然而，命运没打算放过他……/就在所有人以为结束时，真正的修罗场才刚刚开始……/但导演的镜头一转，揭示了最残酷的真相……；\n'
              + '整体梗概只用于衔接语气，绝不允许把后面段落的事件提前到前面段落讲；'
              + '- 【口语化讲述感】短句、多动词、少形容词，单句超过 20 字必须停顿；用「我们/你我」的唠嗑感；'
                '关键处可插入一句主观吐槽（例："说实话，换我早跪了"）拉近距离；\n'
              + '- 【留白】若某段是情感爆发点（痛哭/决裂/无言以对），该行只写「（留白）」三个字：解说闭嘴，让原片声音飞——文字不给满，情绪才溢得出来；\n'
              + '- 【详略有当】标“关键/高光”的多讲（可两句），过渡镜头一句带过，不要平均用力、不要每段一样长；\n'
              + '- 【字数严格对应时长】每段标注了“建议N~M字”，这是该镜头的配音容量：\n'
                '  写太长会念不完被截断，写太短画面会空着。请让每行字数落在该区间内（允许 ±10% 浮动）；\n'
              + '- 【中段克制·结尾升华】中间各行不总结、不升华，只推进剧情与情绪；'
                '最后一段承担全片金句收尾：把故事映射到现实共鸣（职场/婚姻/原生家庭/阶层），'
                '用 ≤2 句散文诗式总结点题——这是观众收藏转发的理由；\n'
              + '- 【红线】涉及暴力用"清理/离世"等温和词、侧重心理而非过程；主角若违法，'
                '最后一段必须点出"违法行为终将受到法律制裁"，不美化犯罪动机；\n'
              + '- 台词只转述大意，不原样照搬；不编造剧情里没有的事实；不堆"高潮/悬念/震撼"等空泛词。\n'
              + req_line
              + '直接输出 %d 行解说词，不要编号、不要解释。\n' % n
              + '- 【格式】必须用换行分隔成 %d 行，**不要写成一整段话**；每行只讲对应镜头的内容。' % n)
    out = _write(prompt, sys_)
    lines = _split_nar_lines(out)

    # —— 行数不足时重试一次：模型常把整稿写成一整段（无换行），
    #    旧逻辑会直接把这一句复制给所有镜头 → 整片解说只剩一句话。
    if len(lines) < n and n >= 2:
        retry = ('你刚才输出的内容没有按行分开（只解析出 %d 行），但需要恰好 %d 行，'
                 '一行对应一个镜头。\n请把下面的解说稿**原样拆成 %d 行**并输出：'
                 '保持原有文字与顺序，只做换行拆分，不要新增、不要删减、不要改写，'
                 '不要编号、不要解释。\n\n' % (len(lines), n, n)
                 + '\n'.join(lines))
        out_r = _write(retry, sys_, timeout=300)
        if out_r and out_r.strip():
            lines_r = _split_nar_lines(out_r)
            if len(lines_r) > len(lines):
                lines = lines_r

    # —— 自优化：让模型自查衔接/重复/详略并输出优化稿（整稿成形且用强文字模型时）——
    if use_local_text and len(lines) >= max(2, (n + 1) // 2):
        polish = ('下面是电影解说稿草稿（一行对应一个镜头）。\n' + '\n'.join(lines)
                  + '\n\n请以资深电影解说编辑的身份审阅并优化，输出【改进后的完整稿】，让整篇像真人电影解说一样流畅自然：'
                    '①镜头之间衔接更顺，有“接着讲下去”的连贯感；②删掉重复、套话和空泛的升华（「（留白）」标记与结尾金句收尾必须原样保留）；'
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



# ---------------------------------------------------------------------------
# 硬件检测 + 模型推荐（根据用户显卡/内存给出最优模型配置）
# ---------------------------------------------------------------------------
HARDWARE_MODEL_RECS = [
    # (最低显存GB, 档位名, VLM推荐, Whisper推荐, TTS推荐, 文本模型推荐, 说明)
    (16, '旗舰独显(≥16GB)', 'qwen3-vl:30b', 'large-v3', 'melo-zh', 'qwen3:30b-q4_K_M',
     '16GB显存+32GB内存可跑30B MoE视觉模型（激活3B，速度快理解强），或Qwen3.8-27B原生多模态；MiniCPM-V4.5(8B)是速度优先的替代，视频token压缩96x适合长视频'),
    (12, '高端独显(12-16GB)', 'minicpm-v4.5', 'large-v3', 'melo-zh', 'qwen3:14b-q4_K_M',
     '推荐MiniCPM-V4.5（8B，视频理解专项优化，96x token压缩，同显存多看10倍帧）；或qwen3-vl:8b（综合强）；内存≥32GB可尝试qwen3-vl:30b MoE'),
    (8, '中高端独显(8-12GB)', 'minicpm-v4.5', 'large-v3', 'melo-zh', 'qwen3:14b-q4_K_M',
     'MiniCPM-V4.5（8B，视频理解强，显存占用小）或qwen3-vl:8b，Whisper GPU加速，MeloTTS配音'),
    (6, '中端独显(6-8GB)', 'qwen3-vl:8b', 'medium', 'melo-zh', 'qwen3:8b',
     '8B视觉模型可用，Whisper用medium平衡速度精度，MiniCPM-V4.5更省显存'),
    (4, '入门独显(4-6GB)', 'qwen3-vl:4b', 'small', 'piper-huayan', 'qwen3:8b',
     '4B视觉模型，Whisper用small，TTS用轻量piper'),
    (0, '纯CPU/无独显', 'qwen3-vl:4b', 'base', 'piper-huayan', 'qwen3:8b',
     '无显卡，全部走CPU，选最小模型保证速度'),
]


def detect_hardware():
    """检测本机 GPU/内存/Ollama 状态，返回硬件信息 + 推荐模型 + 当前配置对比。
    供前端 AI 设置页展示「你的硬件适合用什么模型」，避免用户盲目选小模型浪费显卡。"""
    info = {'gpu': None, 'gpu_vram_gb': 0, 'ram_gb': 0, 'ollama': False, 'ollama_models': [],
            'tier': None, 'recommendations': {}, 'current': {}, 'upgrades': []}
    # GPU
    try:
        import subprocess
        r = subprocess.run(['nvidia-smi', '--query-gpu=name,memory.total', '--format=csv,noheader,nounits'],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            parts = r.stdout.strip().split(',')
            info['gpu'] = parts[0].strip()
            info['gpu_vram_gb'] = round(int(parts[1].strip()) / 1024.0, 1)
    except Exception:
        pass
    # 内存
    try:
        import ctypes
        class _MS(ctypes.Structure):
            _fields_ = [('len', ctypes.c_ulong), ('load', ctypes.c_ulong),
                        ('totalPhys', ctypes.c_ulonglong), ('availPhys', ctypes.c_ulonglong),
                        ('totalPage', ctypes.c_ulonglong), ('availPage', ctypes.c_ulonglong),
                        ('totalVirtual', ctypes.c_ulonglong), ('availVirtual', ctypes.c_ulonglong),
                        ('availExtended', ctypes.c_ulonglong)]
        ms = _MS()
        ms.len = ctypes.sizeof(ms)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            info['ram_gb'] = round(ms.totalPhys / (1024**3), 1)
    except Exception:
        try:
            import psutil
            info['ram_gb'] = round(psutil.virtual_memory().total / (1024**3), 1)
        except Exception:
            pass
    # Ollama
    try:
        import urllib.request
        req = urllib.request.Request('http://localhost:11434/api/tags')
        resp = urllib.request.urlopen(req, timeout=3)
        data = json.loads(resp.read().decode('utf-8'))
        info['ollama'] = True
        info['ollama_models'] = [m.get('name', '') for m in data.get('models', [])]
    except Exception:
        info['ollama'] = False
    # 匹配档位
    vram = info['gpu_vram_gb']
    for min_vram, tier, vlm, whisper, tts, text, note in HARDWARE_MODEL_RECS:
        if vram >= min_vram:
            info['tier'] = tier
            info['recommendations'] = {'vlm': vlm, 'whisper': whisper, 'tts': tts, 'text': text, 'note': note}
            break
    # 当前配置
    cfg = load_ai_config()
    info['current'] = {
        'vlm': (cfg.get('vlm') or {}).get('model', ''),
        'whisper': (cfg.get('whisper') or {}).get('model', 'base'),
        'tts_engine': (cfg.get('tts_local') or {}).get('engine', ''),
        'tts_sherpa': (cfg.get('tts_local') or {}).get('sherpa_model', ''),
        'text': (cfg.get('local') or {}).get('model', ''),
    }
    # 可升级项对比
    rec = info['recommendations']
    cur = info['current']
    if rec.get('vlm') and cur.get('vlm') and rec['vlm'] != cur['vlm']:
        info['upgrades'].append({'slot': 'VLM 视觉模型', 'current': cur['vlm'], 'recommend': rec['vlm'],
                                 'reason': '你的显存放得下更大的视觉模型，剧情理解会更强'})
    if rec.get('whisper') and cur.get('whisper') and rec['whisper'] != cur['whisper']:
        info['upgrades'].append({'slot': 'Whisper 转写模型', 'current': cur['whisper'], 'recommend': rec['whisper'],
                                 'reason': '大模型转写更准，GPU 加速后速度也快'})
    if rec.get('tts') == 'melo-zh' and not _sherpa_ready('melo-zh'):
        info['upgrades'].append({'slot': 'TTS 配音模型', 'current': 'piper(机械感)', 'recommend': 'melo-zh(自然)',
                                 'reason': 'MeloTTS 中文自然度明显更好，项目已支持只需下载'})
    return info


def ai_status():
    """返回各 AI 能力的就绪状态，供前端做生成前置引导（未配 key 时不应静默免费生成）。"""
    vok, vmsg = (vlm_ping() if vlm_enabled() else (False, 'VLM 未启用'))
    return {
        'chat': ai_enabled('chat'),
        'vision': ai_enabled('vision'),
        'tts': _tts_available(),
        'tts_local': {
            'cfg': tts_local_cfg(),
            'edge_installed': edge_tts_available(),
            'edge_dead': edge_tts_dead_reason(),
            'sherpa_installed': sherpa_tts_available(),
            'sherpa_model_ready': sherpa_tts_ready(),
            'sherpa_model': sherpa_model_key(),
            'sherpa_models': [{'key': k, 'label': m['label'], 'ready': _sherpa_ready(k)}
                              for k, m in SHERPA_TTS_MODELS.items()],
            'voices': EDGE_TTS_VOICES,
            'label': local_tts_label(),
        },
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


def _cover_candidates_sequential(video_path, cand_dir, ts_list, max_side=640, only=None):
    """逐帧抽帧（保底实现）：每个候选单独起一次 ffmpeg，-ss 精确 seek + 单帧输出。

    批量抽帧不可用时（老版本 ffmpeg 不认 -fps_mode / fps:start_time，或落盘帧数不足）
    退回本函数。only 为下标列表时只补抽这些下标（批量已抽到的不重复劳动）。
    共 ts_list 与 cand_dir 两个参数：时间与目录由调用方统一算好，避免两条路径漂移。"""
    os.makedirs(cand_dir, exist_ok=True)
    idxs = range(len(ts_list)) if only is None else only
    for k in idxs:
        fp = os.path.join(cand_dir, 'cand_%02d.jpg' % k)
        rc, _o, _e = ffmpeg_run(['-y', '-ss', '%.2f' % ts_list[k], '-i', video_path,
                                 '-frames:v', '1', '-vf', 'scale=min(iw\\,%d):-2' % max_side,
                                 '-q:v', '4', '-an', fp])
        if rc != 0 and os.path.isfile(fp):
            # 失败残留的半张图会污染候选列表，直接清掉交给上层判缺失
            try:
                os.remove(fp)
            except OSError:
                pass


def _cover_candidates_batch(video_path, cand_dir, n, max_side, vdur):
    """一次 ffmpeg 调用抽 n 帧：fps 滤镜按 n/vdur 分桶 + scale + 模板输出 cand_%02d.jpg。
    返回 True 表示 n 张 jpg 全部落盘。

    【为什么要这么写】fps 滤镜默认的 round=near 会把时间轴按 vdur/n 分桶，桶边界正好
    落在 (k+0.5)*vdur/n —— 也就是逐帧路径的 ts_k，所以「不碰时间轴」反而天然对齐。
    实测（1080p/30fps 样片，n=4/6/8/12）抽到的帧与逐帧 -ss 抽到的相差 <8/255 灰度，
    肉眼无差别。以下三种「看起来更对」的写法实测都是错的，别再改回去：
      · fps=...:start_time=ts0 —— start_time 会重置锚点，桶边界整体后移 vdur/2n
        （实测 0.625s），缩略图与最终封面完全不是同一画面（差异 35~120）。
      · -ss ts0（输入侧 seek）—— 带滤镜链时输出时间戳不因 -ss 归零，同样整组后移。
      · -ss ts0 + setpts=PTS-ts0/TB —— 锚点被挪到首个候选时刻之后，同样错位。
    另注：fps 取的是「桶内最后一帧」，逐帧 -ss 取的是「ts 之后第一帧」，两者最多差
    一帧（33ms@30fps）；这条差异无法通过挪网格消除，只能接受。
    三级回退：-fps_mode vfr（ffmpeg 6+）→ -vsync 0（旧版）→ 全失败交给调用方逐帧抽。"""
    out_tpl = os.path.join(cand_dir, 'cand_%02d.jpg')
    rate = n / vdur if vdur > 0 and n > 0 else 1.0
    vf = 'fps=fps=%.6f,scale=min(iw\\,%d):-2' % (rate, max_side)
    for extra in (['-fps_mode', 'vfr'], ['-vsync', '0']):
        # -start_number 0 必须显式给：image2 默认从 1 开始编号，与逐帧路径的
        # cand_%02d.jpg（0 起）对不上，会让「帧已抽到」的判定全部落空而白白回退。
        # 输出选项一律排在 -an 之前：保持「输出路径紧跟 -an」的位置约定。
        rc, _o, _e = ffmpeg_run(['-y', '-i', video_path, '-vf', vf, '-q:v', '4',
                                 '-start_number', '0'] + extra + ['-an', out_tpl])
        if rc == 0 and all(os.path.isfile(out_tpl % k) for k in range(n)):
            # fps 滤镜会向上取整多吐一帧（时刻已超出片长，是尾帧的重复），删掉免得
            # 留在 run_dir 里被当成有效候选 / 中间产物
            for name in sorted(os.listdir(cand_dir)):
                m = re.match(r'cand_(\d+)\.jpg$', name)
                if m and int(m.group(1)) >= n:
                    try:
                        os.remove(os.path.join(cand_dir, name))
                    except OSError:
                        pass
            return True
    return False


def _cover_candidates(video_path, run_dir, n=COVER_CANDIDATES, max_side=640):
    """均匀抽 n 帧做候选（预览小图存 run_dir/cover_cand，带打分）。返回按时间序的候选列表。

    默认走一次 ffmpeg 批量抽 n 帧（原来 n 次进程 → 1 次，省掉 n-1 次 seek+解码开销）；
    批量不可用或落盘不足时按缺失下标逐帧补抽，失败则退回全量逐帧。"""
    vdur = probe_audio_len(video_path) or 0.0
    if vdur <= 0:
        raise RuntimeError('无法读取视频时长')
    cand_dir = os.path.join(run_dir, 'cover_cand')
    os.makedirs(cand_dir, exist_ok=True)
    ts_list = [round(vdur * (k + 0.5) / n, 2) for k in range(n)]
    if _cover_candidates_batch(video_path, cand_dir, n, max_side, vdur):
        missing = [k for k in range(n) if not os.path.isfile(os.path.join(cand_dir, 'cand_%02d.jpg' % k))]
        if missing:
            # 批量抽够了但个别帧缺（尾部帧可能落在时长之外）→ 只补这几帧
            _cover_candidates_sequential(video_path, cand_dir, ts_list, max_side, only=missing)
    else:
        _cover_candidates_sequential(video_path, cand_dir, ts_list, max_side)
    out = []
    for k, ts in enumerate(ts_list):
        fp = os.path.join(cand_dir, 'cand_%02d.jpg' % k)
        if not os.path.isfile(fp):
            continue
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
        # 找不到含中文字形的字体时显式报错（cjk_font 抛 FontMissingError），
        # 绝不回退到 ImageFont.load_default() —— 那个字体不含中文，只会画出豆腐块。
        return cjk_font(size)

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


# 可执行/可渲染扩展名：这类文件放进素材库并被同源访问即为存储型 XSS
# （配合 /material_lib/ 的 attachment 响应与 nosniff，构成纵深防御）
_MATERIAL_BLOCKED_EXT = {'.html', '.htm', '.xhtml', '.svg', '.js', '.mjs',
                         '.xml', '.xsl', '.swf', '.php', '.jsp'}


def material_save_bytes(name, data):
    """上传的字节存入素材库（保留原始文件名，重名自动加序号）。返回 (最终文件名|None, error)。"""
    base = os.path.basename(_safe_filename(name or ''))
    if not base or not data:
        return None, '文件名或内容为空'
    if os.path.splitext(base)[1].lower() in _MATERIAL_BLOCKED_EXT:
        return None, '不支持该文件类型（网页/脚本类文件不能作为素材）'
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
    except FileNotFoundError:
        items = []
    except Exception:
        # 文件存在但解析失败（半截 JSON / 被写坏）：必须**保留现场**再返回空。
        # 旧实现直接返回 []，调用方随后以 [] 为基底整体覆盖写回，损坏文件被彻底抹掉 ——
        # 用户看到「历史全空」且无从恢复。这里改名留档，至少还能人工捞回来。
        items = []
        try:
            if os.path.exists(HISTORY_PATH):
                bak = HISTORY_PATH + '.corrupt.' + time.strftime('%Y%m%d-%H%M%S')
                os.replace(HISTORY_PATH, bak)
                print('[警告] history.json 解析失败，已留档为 %s，本次以空历史启动' % bak)
        except Exception:
            pass
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
# 🧹 存储管理：扫描各类磁盘占用 + 安全删除（路径白名单防穿越）
# ---------------------------------------------------------------------------
def _storage_dir_size(p):
    """递归统计目录体积（字节）。"""
    tot = 0
    try:
        for root, _dirs, files in os.walk(p):
            for f in files:
                try:
                    tot += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return tot


# 可被存储面板安全删除的路径白名单（相对项目根，正则匹配；禁止任何穿越/越界子路径）
_STORAGE_ALLOW = [
    r'^webui_output/run-[^/]+$',
    r'^webui_workspace/uploads/up-[0-9A-Za-z-]+$',
    r'^webui_workspace/asr_[0-9]+\.wav$',
    r'^webui_workspace/music_[0-9]+\.(mp3|wav)$',
    # 素材落盘名带 runid（见 dispatch_build），故第一段允许字母/数字/横线下划线
    r'^webui_workspace/up_[0-9A-Za-z_-]+_[a-z]+\.(jpg|png|webp|mp4)$',
    r'^webui_workspace/analysis_cache$',
    r'^models(/whisper)?$',
]


def _storage_resolve_deletable(rel):
    """校验 rel 是否为可清理路径，返回绝对路径；否则 None（拒绝穿越/越权删除）。"""
    import re as _re
    if not rel:
        return None
    rel = rel.replace('\\', '/')
    if rel.startswith('/') or '..' in rel.split('/'):
        return None
    for pat in _STORAGE_ALLOW:
        if _re.match(pat, rel):
            full = os.path.normpath(os.path.join(HERE, rel))
            base_abs = os.path.abspath(HERE)
            if os.path.commonpath([base_abs, full]) == base_abs:
                return full
            return None
    return None


def _storage_scan():
    """扫描项目内各类磁盘占用，分组返回，供前端存储管理面板展示与清理。

    档位：keep=保留不可删 / safe=临时可回收 / review=删除需重新下载。
    """
    import re as _re
    groups = []

    out_names = sorted(os.listdir(OUTDIR)) if os.path.isdir(OUTDIR) else []
    out_items, out_total = [], 0
    run_items, run_total = [], 0
    for name in out_names:
        p = os.path.join(OUTDIR, name)
        if not os.path.isdir(p):
            continue
        s = _storage_dir_size(p)
        mtime = int(os.path.getmtime(p))
        if name.startswith('run-'):
            run_total += s
            run_items.append({'name': name, 'rel': 'webui_output/' + name,
                              'size': s, 'mtime': mtime})
        else:
            out_total += s
            out_items.append({'name': name, 'rel': 'webui_output/' + name,
                              'size': s, 'mtime': mtime})
    groups.append({'key': 'outputs', 'label': '成片（webui_output 下日期目录）',
                   'tier': 'keep', 'deletable': False, 'total': out_total, 'items': out_items})
    groups.append({'key': 'run_residual', 'label': '任务残留（run-* 临时帧/缩略图）',
                   'tier': 'safe', 'deletable': True, 'total': run_total, 'items': run_items})

    up_items, up_total = [], 0
    if os.path.isdir(UPLOAD_DIR):
        for name in sorted(os.listdir(UPLOAD_DIR)):
            p = os.path.join(UPLOAD_DIR, name)
            if os.path.isdir(p):
                s = _storage_dir_size(p)
                up_total += s
                up_items.append({'name': name, 'rel': 'webui_workspace/uploads/' + name,
                                 'size': s, 'mtime': int(os.path.getmtime(p))})
    groups.append({'key': 'uploads', 'label': '上传会话成品（webui_workspace/uploads）',
                   'tier': 'safe', 'deletable': True, 'total': up_total, 'items': up_items})

    def temp_group(pattern, key, label):
        items, total = [], 0
        if os.path.isdir(WORKDIR):
            for fn in sorted(os.listdir(WORKDIR)):
                fp = os.path.join(WORKDIR, fn)
                if _re.match(pattern, fn) and os.path.isfile(fp):
                    s = os.path.getsize(fp)
                    total += s
                    items.append({'name': fn, 'rel': 'webui_workspace/' + fn,
                                  'size': s, 'mtime': int(os.path.getmtime(fp))})
        groups.append({'key': key, 'label': label, 'tier': 'safe',
                       'deletable': True, 'total': total, 'items': items})

    temp_group(r'^asr_[0-9]+\.wav$', 'asr_temp', 'ASR 临时音频（asr_*.wav）')
    temp_group(r'^music_[0-9]+\.(mp3|wav)$', 'music_temp', '音乐临时文件（music_*.mp3/wav）')
    temp_group(r'^up_[0-9A-Za-z_-]+_[a-z]+\.(jpg|png|webp|mp4)$', 'upload_leftover', '上传素材残留（up_*_*）')

    ac = os.path.join(WORKDIR, 'analysis_cache')
    ac_size = _storage_dir_size(ac) if os.path.isdir(ac) else 0
    groups.append({'key': 'analysis_cache', 'label': '分析缓存（webui_workspace/analysis_cache）',
                   'tier': 'safe', 'deletable': True, 'total': ac_size,
                   'items': [{'name': 'analysis_cache', 'rel': 'webui_workspace/analysis_cache',
                              'size': ac_size, 'mtime': int(os.path.getmtime(ac))}] if ac_size else []})

    models_dir = os.path.join(HERE, 'models')
    if os.path.isdir(models_dir):
        msize = _storage_dir_size(models_dir)
        groups.append({'key': 'models', 'label': '模型权重（models/，删除需重新下载）',
                       'tier': 'review', 'deletable': True, 'total': msize,
                       'items': [{'name': 'models', 'rel': 'models',
                                  'size': msize, 'mtime': int(os.path.getmtime(models_dir))}]})

    git_dir = os.path.join(HERE, '.git')
    if os.path.isdir(git_dir):
        gsize = _storage_dir_size(git_dir)
        groups.append({'key': 'git', 'label': '.git 版本历史（不建议删）',
                       'tier': 'keep', 'deletable': False, 'total': gsize, 'items': []})

    reclaimable = sum(g['total'] for g in groups if g['tier'] in ('safe', 'review') and g['deletable'])
    total_all = sum(g['total'] for g in groups)
    try:
        free = shutil.disk_usage(HERE).free
    except Exception:
        free = 0
    return {'ok': True, 'groups': groups, 'total_bytes': total_all,
            'reclaimable_bytes': reclaimable, 'free_bytes': free}


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


def _parse_multipart(raw, boundary):
    """轻量 multipart/form-data 解析器：返回 {字段名: 字符串值 或 字节}。
    只处理本项目上传用到的字段（upload_id/idx/chunk），不做完整 RFC 2046 解析。"""
    result = {}
    delim = b'--' + boundary
    for part in raw.split(delim):
        if not part or part in (b'--', b'--\r\n', b'\r\n'):
            continue
        if b'\r\n\r\n' not in part:
            continue
        header, body = part.split(b'\r\n\r\n', 1)
        body = body[:-2] if body.endswith(b'\r\n') else body
        try:
            hdr = header.decode('utf-8', errors='replace')
        except Exception:
            continue
        name_m = re.search(r'name="([^"]+)"', hdr)
        if not name_m:
            continue
        field = name_m.group(1)
        if 'filename=' in hdr:
            result[field] = body
        else:
            result[field] = body.decode('utf-8', errors='replace')
    return result


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
            _atomic_write_json(HISTORY_PATH, items)
        # 任务成功后自动清理中间产物（配置开启时，默认开）
        try:
            if load_ai_config().get('cleanup_mid', True):
                rel = entry.get('file')
                if rel:
                    fp = os.path.join(OUTDIR, rel) if not os.path.isabs(rel) else rel
                    if os.path.isfile(fp) and os.path.getsize(fp) > 1024:
                        cleanup_run_mid(os.path.dirname(fp))
        except Exception:
            pass
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
            # 一并清理同目录（独立 run_dir）下的中间产物。
            # 两道边界：① parent 正好等于 OUTDIR 时绝不能 rmtree，否则清空全部成片；
            # ② 该目录下若还有其他记录仍在使用，只删当前文件，不动目录。
            parent = os.path.dirname(fp)
            parent_abs = os.path.abspath(parent) if parent else ''
            out_abs = os.path.abspath(OUTDIR)
            if (parent_abs and os.path.isdir(parent) and parent_abs != out_abs
                    and parent_abs.startswith(out_abs)
                    and not _dir_still_referenced(parent_abs)):
                shutil.rmtree(parent, ignore_errors=True)
    except Exception:
        pass


def cleanup_run_mid(run_dir):
    """任务成功后清理 run_dir 下可再生成的中间产物，保留 final.mp4 / src 源视频 / cover 封面 / 音乐 mp3。
    避免 webui_output 无限膨胀（之前手动清过 3GB）。失败静默。"""
    if not run_dir or not os.path.isdir(run_dir):
        return 0
    keep_prefix = ('final', 'src', 'cover', 'poster')
    mid_patterns = [
        re.compile(r'^narr\d+.*\.(wav|mp3)$'),
        re.compile(r'^frame_\d+\.jpg$'),
        re.compile(r'^cut.*\.mp4$'),
        re.compile(r'^bc\d+\.mp4$'),
        re.compile(r'^bc_silent\.mp4$'),
        re.compile(r'^vsub\.mp4$'),
        re.compile(r'^.*\.srt$'),
        re.compile(r'^.*\.concat\.txt$'),
    ]
    freed = 0
    for root, dirs, files in os.walk(run_dir):
        for f in files:
            fp = os.path.join(root, f)
            low = f.lower()
            if low.startswith(keep_prefix) or low.endswith('.mp3'):
                continue
            if any(p.match(f) for p in mid_patterns):
                try:
                    freed += os.path.getsize(fp)
                    os.remove(fp)
                except Exception:
                    pass
    for root, dirs, files in os.walk(run_dir, topdown=False):
        for d in list(dirs):
            dp = os.path.join(root, d)
            try:
                if not os.listdir(dp):
                    os.rmdir(dp)
            except Exception:
                pass
    return freed


def _dir_still_referenced(parent_abs):
    """history 里是否还有别的记录指向该目录——有则不能整目录删除。"""
    try:
        for it in load_history(500):
            f = (it or {}).get('file')
            if not f:
                continue
            p = os.path.abspath(os.path.join(OUTDIR, f) if not os.path.isabs(f) else f)
            if os.path.dirname(p).lower() == parent_abs.lower():
                return True
    except Exception:
        return True      # 判断失败时保守处理：宁可不删
    return False


def delete_history(file):
    """按 file 删除一条历史记录及其磁盘文件。返回是否删除成功。"""
    try:
        with _HIST_LOCK:   # 与 add_history 互斥：否则后台任务收尾写入的记录会被这里读到的旧快照覆盖掉
            items = load_history(500)
            new = [it for it in items if it.get('file') != file]
            if len(new) == len(items):
                return False
            _atomic_write_json(HISTORY_PATH, new)
        _remove_history_file(file)
        return True
    except Exception:
        return False


def _active_run_dirs():
    """当前仍有任务在使用的 run_dir 集合（绝对路径，小写）。

    供 clear_history 跳过——旧实现无差别 rmtree 掉 OUTDIR 下所有目录，
    会把正在渲染的任务目录一并删掉：ffmpeg 写入中途目录消失，几十分钟白跑，
    且报错对用户毫无意义。"""
    out = set()
    try:
        for p in list(PROGRESS.values()):
            rd = (p or {}).get('run_dir')
            if rd and not (p or {}).get('done'):
                out.add(os.path.abspath(rd).lower())
    except Exception:
        pass
    return out


def clear_history():
    """清空全部历史记录并删除成片（保留目录本身）。

    两点关键：
    ① 先写空索引再删文件——反过来的话，中途异常会留下「文件已删、索引还在」的状态，
       所有记录点开都是 404。
    ② 跳过正在运行任务的 run_dir，避免把进行中的渲染删掉。"""
    try:
        active = _active_run_dirs()
        with _HIST_LOCK:
            _atomic_write_json(HISTORY_PATH, [])
        if os.path.isdir(OUTDIR):
            for name in os.listdir(OUTDIR):
                p = os.path.join(OUTDIR, name)
                if os.path.isdir(p):
                    if os.path.abspath(p).lower() in active:
                        continue       # 正在生成，留着
                    shutil.rmtree(p, ignore_errors=True)
                elif os.path.isfile(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass
        return True
    except Exception:
        return False


# Offline caption fallback: turn a plain filename into a short spring-y caption if the
# user has not configured any AI. Keeps the pipeline functional without a key.
def offline_caption(name, idx, n_total):
    base = os.path.splitext(os.path.basename(name))[0]
    import re as _re
    # 落盘名为 up_<runid>_<idx>_img；runid 可能是 run-12 / t1234 等非纯数字
    m = _re.search(r'up_[0-9A-Za-z_-]+?_(\d+)_img', base)
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
        font = cjk_font(54); font_small = cjk_font(30)
    except FontMissingError:
        # 默认示例图上的标题只是装饰文字：宁可不加字，也不画一堆豆腐块。
        # 真正的成片渲染路径（字幕/封面）会显式抛错，不会走到这种静默分支。
        if not getattr(stamp_title, '_warned', False):
            stamp_title._warned = True
            print('[警告] ' + font_missing_help(), flush=True)
        return img
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
    """Return audio duration in seconds using ffmpeg. 只读容器头，不解码。"""
    return _probe_duration_cached(path,
                                  lambda p: _parse_duration(ffmpeg_run(['-hide_banner', '-i', p])[2]))


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
    # 注册到 RUN_PROCS：/api/cancel 才能真的把这个进程杀掉。
    # 旧实现是裸 Popen，长视频全片解码要跑数分钟到数十分钟，期间「取消」按钮完全无效。
    _runid = getattr(_TLS, 'runid', None)
    if _runid:
        with _PROC_LOCK:
            RUN_PROCS[_runid] = proc
    times, motion, frac, hist, bright = [], [], [], [], []
    prev_gray = None
    prev_hist = None
    t = 0.0
    try:
        while True:
            if _runid and PROGRESS.get(_runid, {}).get('abort'):
                break
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
        # kill 之后必须 wait，否则进程句柄残留成僵尸直到 Popen 被 GC
        try:
            proc.stdout.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        if _runid:
            with _PROC_LOCK:
                RUN_PROCS.pop(_runid, None)
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
                   visual_cuts=None, strength='standard', max_cuts=None, skip_head=0.0):
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
        if c < max(0.3, skip_head) or c >= video_dur - 0.3:
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
    # 同场景去重：相邻片段之间无场景切换且都短于 min_seg*1.5 → 合并（避免同一场景反复切）
    if scene_cuts and len(timeline) > 2:
        sc_set = set(round(float(s), 1) for s in scene_cuts)
        merged = [timeline[0]]
        for i in range(1, len(timeline)):
            seg_dur = timeline[i] - merged[-1]
            # 该切点附近有无场景切换（±0.5s）
            has_scene = any(abs(timeline[i] - sc) <= 0.5 for sc in sc_set)
            if seg_dur < min_seg * 1.5 and not has_scene:
                continue  # 跳过此切点，合并到前一段
            merged.append(timeline[i])
        timeline = merged
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
    skip_head = max(0.0, min(30.0, float(params.get('skipHead', 3.0) or 3.0)))
    timeline = plan_beat_cuts(scene_cuts, motion_cuts, strong_beats, vdur,
                              visual_cuts=visual_cuts, strength=strength,
                              max_cuts=max_cuts, skip_head=skip_head)
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


# ---------------------------------------------------------------------------
# 本地配音（免费）：edge-tts（微软朗读，免 Key、音质好）+ Windows SAPI（系统内置、完全离线）
#
# 历史缺口：界面上只有「云端 TTS」要 Key 的选项，本地配音只能吃系统 SAPI ——
# 而多数 Windows 只装了一个 zh-CN 音色（Microsoft Huihui），机械味重且无法选择，
# 用户既看不到配置入口也无从下载更好的引擎。这里补上本地配音引擎的选择与安装。
# ---------------------------------------------------------------------------
EDGE_TTS_VOICES = [
    ('zh-CN-XiaoxiaoNeural', '晓晓 · 女声（温柔自然·推荐）'),
    ('zh-CN-YunxiNeural', '云希 · 男声（清朗）'),
    ('zh-CN-YunyangNeural', '云扬 · 男声（播报/解说腔）'),
    ('zh-CN-XiaoyiNeural', '晓伊 · 女声（活泼）'),
    ('zh-CN-YunjianNeural', '云健 · 男声（沉稳叙事）'),
    ('zh-CN-YunxiaNeural', '云夏 · 男声（少年感）'),
    ('zh-CN-liaoning-XiaobeiNeural', '晓北 · 女声（东北话）'),
    ('zh-CN-shaanxi-XiaoniNeural', '晓妮 · 女声（陕西话）'),
    ('zh-HK-WanLungNeural', '粤语 · 云龙（男）'),
    ('zh-TW-HsiaoChenNeural', '台湾腔 · 晓臻（女）'),
]


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


# edge-tts 需要访问微软朗读服务。本机实测**单次成功率只有约 2/3**（连接被会随时重置），
# 但同一次内容重试一两次基本都能成——所以这里分两层：
#   ① 单次合成内重试（_EDGE_RETRY 次，间隔递增）：把 67% 拉到接近 100%，
#      这是「用得上 edge-tts 好音质」的关键，否则动不动就掉到机械感重的离线模型。
#   ② 整条链路熔断：连续 _EDGE_MAX_FAILS 次「重试后仍失败」才判死一段时间，
#      避免网络真断了以后每段都白等一轮重试，把几十段的解说拖成假死。
_EDGE_RETRY = 3              # 单次合成的重试次数
_EDGE_RETRY_SLEEP = 1.2      # 重试间隔基数（秒），按 1x/2x/3x 递增
_EDGE_MAX_FAILS = 4          # 连续多少次「整轮重试失败」才熔断
_EDGE_DEAD_SECONDS = 300     # 熔断时长（5 分钟，原为 10 分钟，恢复过快会反复抖动）
_EDGE_STATE = {'fails': 0, 'dead_until': 0.0, 'reason': ''}


def edge_tts_dead_reason():
    """返回 edge-tts 当前被熔断的原因（未熔断返回 ''）。"""
    if time.time() < _EDGE_STATE['dead_until']:
        return _EDGE_STATE['reason'] or '连续失败'
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
    """记一次「整轮重试后仍失败」。达到阈值才熔断。"""
    _EDGE_STATE['fails'] += 1
    if _EDGE_STATE['fails'] >= _EDGE_MAX_FAILS:
        _EDGE_STATE['dead_until'] = time.time() + _EDGE_DEAD_SECONDS
        _EDGE_STATE['reason'] = reason or ('连续 %d 次合成失败' % _EDGE_MAX_FAILS)
    return _EDGE_STATE['fails']


# ChatTTS 本地引擎（更自然的对话式语音，需 torch + ChatTTS，延迟加载）
_CHATTS = {'model': None, 'loading': False, 'error': ''}


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


def _chattts_venv_python():
    """返回 ChatTTS venv 的 Python 路径（Python 3.11 + CUDA torch），不存在返回 None。"""
    p = os.path.join(HERE, '.venv_tts', 'Scripts', 'python.exe')
    return p if os.path.exists(p) else None


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


# ============ CosyVoice 离线配音（阿里开源·质量最高·需GPU） ============
_COSYVOICE = {'model': None, 'loading': False, 'error': '', 'voice': '中文女'}
COSYVOICE_REPO_DIR = os.path.join(HERE, 'CosyVoice')
COSYVOICE_MODEL_DIR = os.path.join(HERE, 'models', 'cosyvoice', 'CosyVoice2-0.5B')
COSYVOICE_VENV_PY = os.path.join(HERE, '.venv_cosyvoice', 'Scripts', 'python.exe')


def _cosyvoice_venv_python():
    """返回 CosyVoice venv 的 Python 路径，不存在返回 None。"""
    return COSYVOICE_VENV_PY if os.path.exists(COSYVOICE_VENV_PY) else None


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
                    import torchaudio
                    model = CosyVoice2(COSYVOICE_MODEL_DIR, load_jit=False, load_trt=False, fp16=False)
                    _COSYVOICE['model'] = model
                finally:
                    _COSYVOICE['loading'] = False
        except Exception as e:
            _COSYVOICE['error'] = str(e)[:300]
            _COSYVOICE['loading'] = False

    if _COSYVOICE['model'] is not None:
        try:
            model = _COSYVOICE['model']
            for i, j in enumerate(model.inference_sft(clean_text, _COSYVOICE['voice'], stream=False)):
                import torchaudio
                torchaudio.save(out_path, j['tts_speech'], model.sample_rate)
                break
            return os.path.exists(out_path) and os.path.getsize(out_path) > 1000
        except Exception as e:
            _COSYVOICE['error'] = str(e)[:300]
            print('[COSYVOICE] 主进程推理失败:', _COSYVOICE['error'][:200])
    return False


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


def _run_pip(pip, args, timeout=600, cwd=None, label=''):
    """运行pip命令，检查返回码，失败时返回错误信息。成功返回(0,'')。
    自动加--timeout 30 --retries 5避免网络卡死。"""
    # 只在install命令时加网络参数
    if args and args[0] == 'install':
        extra = ['--timeout', '30', '--retries', '5', '--progress-bar', 'off']
        # 插入到install之后、包名之前
        args = [args[0]] + extra + args[1:]
    try:
        r = subprocess.run([pip] + args, capture_output=True, timeout=timeout, cwd=cwd)
        if r.returncode != 0:
            err = (r.stderr or b'').decode('utf-8', 'ignore')[-500:]
            return r.returncode, (label + '失败：' + err if label else err)
        return 0, ''
    except subprocess.TimeoutExpired:
        return -1, (label or 'pip') + '超时（%ds）' % timeout
    except Exception as e:
        return -2, (label or 'pip') + '异常：' + str(e)[:200]


def cosyvoice_install_async():
    """后台安装 CosyVoice：创建venv+装PyTorch+clone仓库+下载模型。返回 (ok, msg)。"""
    with _SETUP_LOCK:
        if TTS_SETUP['running']:
            return False, '已有安装任务在进行中'
        TTS_SETUP.update(running=True, op='cosyvoice', pct=0, msg='正在准备 CosyVoice 安装环境…', ok=None)

    def _run():
        try:
            import venv as _venv
            import threading as _th
            MIRROR = ['-i', 'https://mirrors.aliyun.com/pypi/simple/', '--trusted-host', 'mirrors.aliyun.com']
            # 1. 创建venv（必须用Python 3.11/3.12，PyTorch不支持3.14）
            TTS_SETUP.update(pct=3, msg='查找 Python 3.11/3.12（PyTorch不支持3.14）…')
            base_py = _find_cosyvoice_python()
            if not base_py:
                TTS_SETUP.update(ok=False, pct=3, msg='❌ 未找到Python 3.11/3.12。请先安装Python 3.11（推荐uv或官网）', running=False)
                return
            TTS_SETUP.update(pct=5, msg='创建 Python 虚拟环境（基于 %s）…' % os.path.basename(os.path.dirname(base_py)))
            venv_dir = os.path.join(HERE, '.venv_cosyvoice')
            # 检查已有venv的Python版本，如果是3.13+（不兼容PyTorch）则删除重建
            if os.path.exists(COSYVOICE_VENV_PY):
                try:
                    vr = subprocess.run([COSYVOICE_VENV_PY, '--version'], capture_output=True, text=True, timeout=10)
                    ver = vr.stdout.strip()
                    if '3.13' in ver or '3.14' in ver or '3.15' in ver:
                        TTS_SETUP.update(pct=5, msg='检测到旧venv为%s（不兼容PyTorch），删除重建…' % ver)
                        import shutil as _sh
                        _sh.rmtree(venv_dir, ignore_errors=True)
                except Exception:
                    pass
            if not os.path.exists(COSYVOICE_VENV_PY):
                # 用找到的Python 3.11创建venv，而不是当前的3.14
                r = subprocess.run([base_py, '-m', 'venv', venv_dir], capture_output=True, timeout=120)
                if r.returncode != 0:
                    TTS_SETUP.update(ok=False, pct=5, msg='❌ venv创建失败：' + (r.stderr or b'').decode('utf-8','ignore')[:200], running=False)
                    return
            if not os.path.exists(COSYVOICE_VENV_PY):
                TTS_SETUP.update(ok=False, pct=5, msg='❌ venv创建失败，python.exe不存在', running=False)
                return
            pip = os.path.join(venv_dir, 'Scripts', 'pip.exe')
            py = COSYVOICE_VENV_PY
            # 升级pip
            TTS_SETUP.update(pct=8, msg='升级 pip…')
            _run_pip(pip, ['install', '--upgrade', 'pip'] + MIRROR, timeout=120, label='pip升级')
            # 先验证venv的pip可用
            TTS_SETUP.update(pct=10, msg='验证 pip 环境…')
            rc, err = _run_pip(pip, ['--version'], timeout=30, label='pip验证')
            if rc != 0:
                TTS_SETUP.update(ok=False, pct=10, msg='❌ venv的pip不可用：' + err[:200] + '。请删除.venv_cosyvoice后重试', running=False)
                return
            # 2. 安装PyTorch CUDA版（先检查是否已装，避免重复下载卡住）
            torch_ok = False
            try:
                _chk = subprocess.run([py, '-c', 'import torch; print(torch.__version__)'],
                                      capture_output=True, text=True, timeout=60)
                if _chk.returncode == 0 and _chk.stdout.strip():
                    torch_ok = True
                    TTS_SETUP.update(pct=25, msg='✅ PyTorch %s 已安装，跳过下载' % _chk.stdout.strip())
            except Exception:
                pass
            if not torch_ok:
                # 心跳：下载中每隔一段时间更新提示
                def _torch_heartbeat(stop_event, start_pct):
                    mins = 0
                    while not stop_event.is_set():
                        time.sleep(20)
                        mins += 1
                        if TTS_SETUP['running']:
                            TTS_SETUP.update(pct=min(start_pct + mins * 0.5, 45),
                                             msg='正在下载 PyTorch CUDA版（约2.4GB，已等%d分钟，请耐心等待）…' % mins)
                _stop_hb = _th.Event()
                _hb = _th.Thread(target=_torch_heartbeat, args=(_stop_hb, 15), daemon=True)
                _hb.start()
                torch_err = ''
                try:
                    TTS_SETUP.update(pct=15, msg='正在下载 PyTorch CUDA版（约2.4GB，根据网速需5-30分钟）…')
                    rc, torch_err = _run_pip(pip, ['install', 'torch', 'torchaudio', '--index-url',
                                                    'https://download.pytorch.org/whl/cu121'],
                                             timeout=3600, label='PyTorch CUDA')
                    torch_ok = (rc == 0)
                finally:
                    _stop_hb.set()
                # CUDA版失败，回退CPU版
                if not torch_ok:
                    TTS_SETUP.update(pct=18, msg='⚠️ CUDA版下载失败，尝试CPU版…')
                    rc2, err2 = _run_pip(pip, ['install', 'torch', 'torchaudio'] + MIRROR, timeout=600, label='PyTorch CPU')
                    if rc2 == 0:
                        torch_ok = True
                        TTS_SETUP.update(pct=20, msg='✅ PyTorch CPU版安装成功（无GPU加速，推理较慢）')
                    else:
                        torch_err = torch_err + ' | CPU版也失败: ' + err2
                if not torch_ok:
                    TTS_SETUP.update(ok=False, pct=15, msg='❌ PyTorch安装失败：' + torch_err[:300], running=False)
                    return
            # 3. 克隆CosyVoice仓库
            TTS_SETUP.update(pct=35, msg='克隆 CosyVoice 仓库…')
            if not os.path.isdir(COSYVOICE_REPO_DIR):
                try:
                    r = subprocess.run(['git', 'clone', '--depth', '1',
                                        'https://github.com/FunAudioLLM/CosyVoice.git', COSYVOICE_REPO_DIR],
                                       capture_output=True, timeout=300, cwd=HERE)
                    if r.returncode != 0:
                        # git失败，尝试用gitee镜像
                        r2 = subprocess.run(['git', 'clone', '--depth', '1',
                                             'https://gitee.com/mirrors/CosyVoice.git', COSYVOICE_REPO_DIR],
                                            capture_output=True, timeout=300, cwd=HERE)
                        if r2.returncode != 0:
                            TTS_SETUP.update(ok=False, pct=35,
                                             msg='❌ git clone失败：' + (r.stderr or r2.stderr or b'').decode('utf-8','ignore')[:200],
                                             running=False)
                            return
                except Exception as e:
                    TTS_SETUP.update(ok=False, pct=35, msg='❌ git clone异常：' + str(e)[:200], running=False)
                    return
            # 4. 安装依赖（国内镜像）
            TTS_SETUP.update(pct=50, msg='安装 CosyVoice 依赖（国内镜像，约2-3分钟）…')
            req = os.path.join(COSYVOICE_REPO_DIR, 'requirements.txt')
            if os.path.exists(req):
                rc, err = _run_pip(pip, ['install', '-r', req] + MIRROR, timeout=900,
                                   cwd=COSYVOICE_REPO_DIR, label='依赖安装')
                if rc != 0:
                    # requirements.txt失败，尝试最小依赖
                    TTS_SETUP.update(pct=52, msg='⚠️ 完整依赖安装失败，尝试最小依赖…')
                    # 分包安装，避免一个大包卡死整个流程
                    dep_packages = ['numpy', 'scipy', 'librosa', 'soundfile',
                                    'transformers', 'onnxruntime', 'modelscope',
                                    'einops', 'rotary-embedding-torch', 'tqdm', 'pillow',
                                    'hyperpyyaml', 'conformer', 'omegaconf', 'hydra-core',
                                    'pyworld', 'wetext', 'inflect', 'openai-whisper']
                    dep_ok = True
                    dep_err = ''
                    for i, pkg in enumerate(dep_packages):
                        TTS_SETUP.update(pct=52 + i * 0.9, msg='正在装依赖 %d/%d：%s…' % (i+1, len(dep_packages), pkg))
                        rc_p, err_p = _run_pip(pip, ['install', pkg] + MIRROR, timeout=300, label=pkg)
                        if rc_p != 0:
                            # modelscope是必须的（下载模型用），其他包失败可以继续
                            if pkg == 'modelscope':
                                dep_ok = False
                                dep_err = err_p
                                break
                            else:
                                TTS_SETUP.update(pct=52 + i * 0.9, msg='⚠️ %s装失败，跳过继续…' % pkg)
                                continue
                    if not dep_ok:
                        TTS_SETUP.update(ok=False, pct=55, msg='❌ 关键依赖modelscope安装失败：' + dep_err[:300], running=False)
                        return
            # 确保modelscope已装（下载模型需要）
            rc, err = _run_pip(pip, ['install', 'modelscope'] + MIRROR, timeout=300, label='modelscope')
            # 5. 下载模型（ModelScope国内镜像）
            # 检查是否已有模型文件，有则跳过下载
            os.makedirs(os.path.dirname(COSYVOICE_MODEL_DIR), exist_ok=True)
            existing_files = []
            try:
                existing_files = os.listdir(COSYVOICE_MODEL_DIR)
            except Exception:
                pass
            has_model = any(f.endswith('.pt') or f.endswith('.onnx') or f == 'config.yaml' for f in existing_files)
            if has_model:
                TTS_SETUP.update(pct=90, msg='✅ 模型已存在，跳过下载')
            else:
                # 下载心跳
                _stop_dl = _th.Event()
                def _dl_heartbeat(stop_evt, base_pct):
                    m = 0
                    while not stop_evt.is_set():
                        time.sleep(20)
                        m += 1
                        if TTS_SETUP['running']:
                            # 检查模型目录大小，估算进度
                            try:
                                total = 0
                                for root, dirs, files in os.walk(COSYVOICE_MODEL_DIR):
                                    for f in files:
                                        total += os.path.getsize(os.path.join(root, f))
                                gb = total / (1024**3)
                                est = min(95, base_pct + gb * 3.0)  # 9GB约占30%进度
                                TTS_SETUP.update(pct=est, msg='正在下载模型（已等%d分钟，已下%.1fGB/约9GB）…' % (m, gb))
                            except Exception:
                                TTS_SETUP.update(pct=min(base_pct + m, 90), msg='正在下载模型（已等%d分钟）…' % m)
                _dl_hb = _th.Thread(target=_dl_heartbeat, args=(_stop_dl, 65), daemon=True)
                _dl_hb.start()
                TTS_SETUP.update(pct=65, msg='下载 CosyVoice2-0.5B 模型（约9GB，国内镜像，需10-20分钟）…')
                dl_script = os.path.join(HERE, '_cosyvoice_dl.py')
                with open(dl_script, 'w', encoding='utf-8') as f:
                    f.write('''from modelscope import snapshot_download
snapshot_download("iic/CosyVoice2-0.5B", local_dir=r"%s")
''' % COSYVOICE_MODEL_DIR.replace('\\', '\\\\'))
                try:
                    r = subprocess.run([py, dl_script], capture_output=True, timeout=3600, cwd=HERE)
                    if r.returncode != 0:
                        dl_err = (r.stderr or b'').decode('utf-8', 'ignore')[-400:]
                        TTS_SETUP.update(ok=False, pct=65, msg='❌ 模型下载失败：' + dl_err, running=False)
                        return
                finally:
                    _stop_dl.set()
                    try: os.unlink(dl_script)
                    except Exception: pass
            # 6. 创建worker脚本
            TTS_SETUP.update(pct=95, msg='配置推理脚本…')
            _ensure_cosyvoice_worker()
            # 验证模型目录有关键文件
            model_files = []
            try:
                model_files = os.listdir(COSYVOICE_MODEL_DIR)
            except Exception:
                pass
            has_model = any(f.endswith('.pt') or f.endswith('.onnx') or f == 'config.yaml' for f in model_files)
            ok = has_model and os.path.exists(COSYVOICE_VENV_PY)
            if ok:
                # 最终验证：尝试import CosyVoice2，确保依赖完整
                TTS_SETUP.update(pct=98, msg='验证推理环境…')
                try:
                    _vfy = subprocess.run(
                        [py, '-c',
                         "import sys; sys.path.insert(0, r'%s\\third_party\\Matcha-TTS'); sys.path.insert(0, r'%s'); from cosyvoice.cli.cosyvoice import CosyVoice2; print('OK')"
                         % (COSYVOICE_REPO_DIR, COSYVOICE_REPO_DIR)],
                        capture_output=True, text=True, timeout=120, cwd=HERE)
                    if _vfy.returncode == 0 and 'OK' in _vfy.stdout:
                        TTS_SETUP.update(ok=True, pct=100, msg='✅ CosyVoice 安装完成！引擎选「CosyVoice」即可使用', running=False)
                    else:
                        _verr = (_vfy.stderr or '')[-300:]
                        TTS_SETUP.update(ok=True, pct=100, msg='✅ CosyVoice 安装完成（验证跳过：%s）。引擎选「CosyVoice」即可使用' % _verr.split(chr(10))[-1][:80], running=False)
                except Exception as _ve:
                    TTS_SETUP.update(ok=True, pct=100, msg='✅ CosyVoice 安装完成（验证超时，不影响使用）。引擎选「CosyVoice」即可使用', running=False)
            else:
                TTS_SETUP.update(ok=False, pct=95, msg='❌ 模型文件不完整，请检查网络后重试', running=False)
        except Exception as e:
            import traceback
            TTS_SETUP.update(ok=False, pct=0, msg='❌ 安装异常：' + str(e)[:200] + ' | ' + traceback.format_exc()[-200:], running=False)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return True, '开始安装 CosyVoice（约10-20分钟，含9GB模型下载）'


def _ensure_cosyvoice_worker():
    """确保 cosyvoice_worker.py 存在（venv子进程推理脚本）。"""
    worker = os.path.join(HERE, 'cosyvoice_worker.py')
    if os.path.exists(worker):
        return
    with open(worker, 'w', encoding='utf-8') as f:
        f.write('''# -*- coding: utf-8 -*-
import sys, os
# CosyVoice 需要 third_party/Matcha-TTS 在 path 中
repo_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'CosyVoice')
matcha = os.path.join(repo_dir, 'third_party', 'Matcha-TTS')
if os.path.isdir(matcha):
    sys.path.insert(0, matcha)
if os.path.isdir(repo_dir):
    sys.path.insert(0, repo_dir)

def main():
    if len(sys.argv) < 5:
        print('Usage: cosyvoice_worker.py <txt_file> <out_wav> <model_dir> <voice>')
        sys.exit(1)
    txt_file, out_path, model_dir, voice = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    with open(txt_file, encoding='utf-8') as f:
        text = f.read().strip()
    if not text:
        print('empty text')
        sys.exit(1)
    from cosyvoice.cli.cosyvoice import CosyVoice2
    import torchaudio
    model = CosyVoice2(model_dir, load_jit=False, load_trt=False, fp16=False)
    for i, j in enumerate(model.inference_sft(text, voice, stream=False)):
        torchaudio.save(out_path, j['tts_speech'], model.sample_rate)
        break
    if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
        print('OK')
    else:
        print('FAIL: output too small')
        sys.exit(1)

if __name__ == '__main__':
    main()
''')


def edge_tts_reset():
    """手动解除熔断（供界面「重试配音引擎」调用）：网络恢复后不必干等。"""
    _EDGE_STATE.update(fails=0, dead_until=0.0, reason='')
    return True


# ---------------------------------------------------------------------------
# 🎙️ TTS 情感化标记系统
# 文案标记格式（LLM 生成解说词时使用）：
#   {情绪:欢快|悲伤|激动|严肃|紧张|温柔|深情|播报|不满} ... {/情绪}
#   {停顿:0.5}          → 0.5秒停顿
#   {慢}...{/慢}        → 语速减慢
#   {快}...{/快}        → 语速加快
#   {高音}...{/高音}    → 音调升高
#   {低音}...{/低音}    → 音调降低
#   {大声}...{/大声}    → 音量增大
#   {小声}...{/小声}    → 音量减小
# ---------------------------------------------------------------------------

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

# edge-tts 支持情感的中文声线
_EMOTION_VOICES = {
    'zh-CN-XiaoxiaoNeural': list(_EMOTION_MAP.values()),
    'zh-CN-YunxiNeural': ['cheerful', 'sad', 'angry', 'serious', 'gentle', 'newscast'],
    'zh-CN-XiaoyiNeural': ['cheerful', 'sad', 'gentle', 'serious'],
    'zh-CN-YunjianNeural': [],  # 体育解说风格，不支持情感标签
}


def parse_tts_markup(text):
    """解析文案中的 TTS 标记，返回结构化片段列表。
    每个片段: {'type': 'text'|'pause'|'emotion_start'|'emotion_end'|'prosody_start'|'prosody_end',
               'content': str, 'value': str}
    """
    import re
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
    import re
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
    import re
    return bool(re.search(r'\{(情绪|停顿|慢|快|高音|低音|大声|小声)', text or ''))


def _enhance_tts_markup(texts):
    """TTS 标记后处理：检查 LLM 生成的解说词标记是否完整合理，自动补全/修正。
    - 修复未闭合的 {情绪:xx}/{慢} 等标签
    - 完全没有标记的段落，根据内容自动添加停顿/情绪
    - 避免过度标记（每句都加情绪），保持详略得当
    """
    import re
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


def _fix_unclosed_tags(text):
    """修复未闭合的 TTS 标记：{情绪:xx} 必须有 {/情绪}，{慢} 必须有 {/慢}。"""
    import re
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


def strip_tts_markup(text):
    """去除文案中的 TTS 标记（用于字幕显示）。"""
    import re
    if not text:
        return text
    text = re.sub(r'\{/?(情绪|慢|快|高音|低音|大声|小声)(?::[^}]*)?\}', '', text)
    text = re.sub(r'\{停顿:[^}]*\}', '', text)
    return text.strip()


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
                    _EDGE_STATE.update(fails=0, dead_until=0.0, reason='')   # 成功即复位熔断
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


def _strip_tts_markup(text):
    """剥离TTS控制标记：{停顿:0.3} {情绪:激动} {慢} {/情绪} 等，供不支持SSML的引擎使用。"""
    import re as _re
    if not text:
        return text
    t = _re.sub(r'\{(?:情绪|停顿|慢|快|高音|低音|大声|小声)[^}]*\}', '', text)
    t = _re.sub(r'\{/(?:情绪|停顿|慢|快|高音|低音|大声|小声)\}', '', t)
    return _re.sub(r'\s+', ' ', t).strip()


def local_tts_speak(text, out_path):
    """本地免费配音统一入口。

    按配置在 edge-tts / 离线模型 / 系统 SAPI 之间选择，任一失败自动回退下一个，
    保证「要么出声、要么明确失败」，不因某条路不可用就静默无配音。

    【音色一致性】一个任务内一旦选定引擎就锁死在 _TLS 上。否则长解说前半段用
    edge-tts、后半段因网络抖动掉到离线模型，观众会听到明显的音色突变。
    只有锁定引擎彻底失败时才改锁到备用引擎。
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
    stem, _ext = os.path.splitext(out_path)
    try:
        speed = 1.0 + (float(str(cfg['rate']).replace('%', '').replace('+', '')) / 100.0)
    except Exception:
        speed = 1.0
    for eng in order:
        if eng == 'edge':
            if not edge_tts_available():
                continue
            if edge_tts_speak(text, out_path):
                _TLS.tts_engine = 'edge'      # 锁定：后续段落沿用同一音色
                return True, 'edge', out_path
        elif eng == 'cosyvoice':
            if not cosyvoice_available():
                continue
            wv = stem + '_cosyvoice.wav'
            if cosyvoice_speak(text, wv):
                _TLS.tts_engine = 'cosyvoice'
                return True, 'cosyvoice', wv
        elif eng == 'chattts':
            if not chattts_available():
                continue
            wv = stem + '_chattts.wav'
            # ChatTTS也不支持{停顿:0.3}等花括号标记，先剥离
            clean_text = _strip_tts_markup(text)
            if chattts_speak(clean_text, wv):
                _TLS.tts_engine = 'chattts'
                return True, 'chattts', wv
        elif eng == 'sherpa':
            if not sherpa_tts_available():
                continue
            wv = stem + '_sherpa.wav'
            # sherpa不支持{停顿:0.3}{情绪:xx}等标记，先剥离避免念出"停顿"
            clean_text = _strip_tts_markup(text)
            if sherpa_tts_speak(clean_text, wv, speed=speed):
                _TLS.tts_engine = 'sherpa'
                return True, 'sherpa', wv
        else:
            wv = stem + '.wav'
            # SAPI也不支持TTS标记，先剥离
            clean_text = _strip_tts_markup(text)
            if sapi_tts(clean_text, wv):
                _TLS.tts_engine = 'sapi'
                return True, 'sapi', wv
    return False, None, out_path


def tts_models_dir():
    """离线配音模型目录：models/tts（与 models/whisper 并列，方便用户自行查看/替换）。"""
    return os.path.join(HERE, 'models', 'tts')


# 离线配音模型（sherpa-onnx）。可选多个，音质差别明显：
#   melo-zh      = MeloTTS 中文：自然度明显好于 piper，接近在线神经 TTS，约 180MB
#   piper-huayan = piper 中文小模型：体积最小、最快，但机械感重、几乎没有语调起伏
# 实测用户反馈「拟真和感情不行」时用的正是 piper-huayan，故默认优先 melo。
SHERPA_TTS_MODELS = {
    'melo-zh': {
        'name': 'vits-melo-tts-zh_en',
        'label': 'MeloTTS 中文（自然·推荐）',
        'url': 'https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/'
               'vits-melo-tts-zh_en.tar.bz2',
    },
    'piper-huayan': {
        'name': 'vits-piper-zh_CN-huayan-medium',
        'label': '华研 · 女声（轻量·机械感较重）',
        'url': 'https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/'
               'vits-piper-zh_CN-huayan-medium.tar.bz2',
    },
}
SHERPA_DEFAULT_MODEL = 'melo-zh'


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



def tts_model_uninstall(key):
    """删除已下载的离线配音模型目录（models/tts/<name>/），释放磁盘空间。
    返回 (ok, msg)。"""
    m = SHERPA_TTS_MODELS.get(key)
    if not m:
        return False, '未知模型：%s' % key
    d = os.path.join(tts_models_dir(), m['name'])
    if not os.path.isdir(d):
        return False, '模型未下载，无需卸载'
    import shutil
    try:
        shutil.rmtree(d)
        return True, '已卸载 %s（%s）' % (m['label'], m['name'])
    except Exception as e:
        return False, '卸载失败：%s' % str(e)


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
                num_threads=2,
                provider='cpu',
            ),
            rule_fsts='',
            max_num_sentences=1,
        ))
    _SHERPA_TTS['obj'] = tts
    _SHERPA_TTS['key'] = key       # 换模型时缓存失效，避免继续用旧音色
    return tts


_SHERPA_TTS = {'obj': None, 'key': None}


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
    except ImportError:
        try:
            import wave, struct
            with wave.open(out_path, 'wb') as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(audio.sample_rate)
                w.writeframes(b''.join(struct.pack('<h', int(max(-1.0, min(1.0, s)) * 32767))
                                       for s in audio.samples))
        except Exception:
            return False
    except Exception:
        return False
    return os.path.exists(out_path) and os.path.getsize(out_path) > 1000


TTS_SETUP = {'running': False, 'op': '', 'pct': 0, 'msg': '', 'ok': None}
_SETUP_LOCK = threading.Lock()      # 保护「检查 running → 置位 → 启动线程」的原子性


def tts_install_async(pkg='edge-tts'):
    """后台 pip 安装本地配音引擎。pkg ∈ edge-tts|sherpa-onnx。"""
    pkg = 'sherpa-onnx' if 'sherpa' in str(pkg).lower() else 'edge-tts'
    # 检查与置位必须在同一把锁内：多线程 HTTP 服务下，两次点击可能同时通过检查，
    # 起两个线程共写同一个进度槽，进度条乱跳、模型目录被 os.replace 两次而报错。
    with _SETUP_LOCK:
        if TTS_SETUP['running']:
            return False, '已有一个安装/下载任务在进行中'
        TTS_SETUP.update(running=True, op='pip:' + pkg, pct=0,
                         msg='正在安装 %s…' % pkg, ok=None)

    def _run():
        try:
            r = subprocess.run(
                [sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                 '--no-input', pkg],
                capture_output=True, timeout=600)
            if r.returncode == 0:
                TTS_SETUP.update(ok=True, pct=100, msg='✅ %s 安装完成（点「🧪 试听」验证）' % pkg)
            else:
                TTS_SETUP.update(ok=False, pct=100,
                                 msg='❌ 安装失败：' + (r.stderr or b'').decode('utf-8', 'ignore')[-300:])
        except Exception as e:
            TTS_SETUP.update(ok=False, pct=100, msg='❌ 安装异常：' + str(e)[-300:])
        finally:
            TTS_SETUP['running'] = False

    _threading.Thread(target=_run, daemon=True).start()
    return True, '开始安装 %s（约 1 分钟，可在下方看进度）' % pkg


def tts_install_chattts_async():
    """后台安装 ChatTTS：uv Python 3.11 venv + CUDA torch + ChatTTS。"""
    with _SETUP_LOCK:
        if TTS_SETUP['running']:
            return False, '已有一个安装/下载任务在进行中'
        TTS_SETUP.update(running=True, op='pip:chattts', pct=5,
                         msg='正在安装 ChatTTS（torch约2.5GB，首次较慢）…', ok=None)

    def _run():
        try:
            venv_py = os.path.join(HERE, '.venv_tts', 'Scripts', 'python.exe')
            if not os.path.exists(venv_py):
                TTS_SETUP.update(pct=10, msg='创建 Python 3.11 虚拟环境…')
                subprocess.run(['uv', 'venv', '--python', '3.11', '.venv_tts'],
                               cwd=HERE, capture_output=True, timeout=120)
            TTS_SETUP.update(pct=20, msg='安装 CUDA torch（约2.5GB，较慢）…')
            subprocess.run(['uv', 'pip', 'install', '--python', venv_py,
                            'torch', 'torchaudio', '--index-url',
                            'https://download.pytorch.org/whl/cu121'],
                           cwd=HERE, capture_output=True, timeout=1800)
            TTS_SETUP.update(pct=80, msg='安装 ChatTTS…')
            subprocess.run(['uv', 'pip', 'install', '--python', venv_py,
                            'ChatTTS', 'soundfile', 'numpy'],
                           cwd=HERE, capture_output=True, timeout=600)
            TTS_SETUP.update(ok=True, pct=100, msg='✅ ChatTTS 安装完成（点「🧪 试听」验证）')
        except Exception as e:
            TTS_SETUP.update(ok=False, pct=100, msg='❌ 安装异常：' + str(e)[-300:])
        finally:
            TTS_SETUP['running'] = False

    _threading.Thread(target=_run, daemon=True).start()
    return True, '已开始后台安装 ChatTTS，可在下方看进度'


def tts_model_download_async(model_key=None):
    """后台下载离线中文配音模型（tar.bz2）到 models/tts/<name>/ 并解压。

    model_key：SHERPA_TTS_MODELS 的键；不传则下载当前选中的（或默认推荐）模型。"""
    key = model_key or sherpa_model_key()
    m = SHERPA_TTS_MODELS.get(key)
    if not m:
        return False, '未知模型：%s' % key
    name = m['name']
    dest = os.path.join(tts_models_dir(), name)
    if _sherpa_ready(key):
        return True, '模型已就绪，无需重复下载'
    with _SETUP_LOCK:      # 与 tts_install_async 互斥，防双点起两个下载线程
        if TTS_SETUP['running']:
            return False, '已有一个安装/下载任务在进行中'
        TTS_SETUP.update(running=True, op='model:' + key, pct=0,
                         msg='正在下载离线配音模型（%s）…' % m['label'], ok=None)

    def _run():
        import tarfile
        import urllib.request as _u
        # 文件名带模型 key：两个模型可以同时/先后下载，互不覆盖
        arch = os.path.join(tts_models_dir(), '_dl_%s.tar.bz2' % key)
        try:
            os.makedirs(tts_models_dir(), exist_ok=True)
            tmp = arch + '.part'
            _u.urlretrieve(m['url'], tmp)
            TTS_SETUP.update(pct=70, msg='下载完成，正在解压…')
            os.replace(tmp, arch)
            with tarfile.open(arch, 'r:bz2') as tf:
                tf.extractall(tts_models_dir())
            os.unlink(arch)
            # 发行包内目录名可能与模型名不同 → 归一到 <name>
            if not os.path.isdir(dest):
                for n in os.listdir(tts_models_dir()):
                    p = os.path.join(tts_models_dir(), n)
                    if os.path.isdir(p) and any(f.endswith('.onnx') for f in os.listdir(p)):
                        os.replace(p, dest)
                        break
            ok = _sherpa_ready(key)
            TTS_SETUP.update(ok=ok, pct=100,
                             msg='✅ %s 就绪' % m['label'] if ok
                             else '❌ 解压后未找到模型文件，请重试')
        except Exception as e:
            TTS_SETUP.update(ok=False, pct=100, msg='❌ 下载失败：' + str(e)[-300:])
        finally:
            # 【必须】缺这一句时 running 永远为 True：前端轮询定时器不停（永久转圈），
            # 且此后所有安装/下载请求都被「已有一个任务在进行中」挡掉，只能重启服务。
            TTS_SETUP['running'] = False

    _threading.Thread(target=_run, daemon=True).start()
    return True, '开始下载离线配音模型（约 130MB，取决于网速）'


def tts_test_speak(text='这是一段中文配音试听。'):
    """试听一句：返回 (ok, msg, engine, rel_path)。rel_path 相对 OUTDIR，供 /media 播放。"""
    text = (text or '').strip() or '这是一段中文配音试听。'
    out_dir = os.path.join(OUTDIR, '_tts_test')
    os.makedirs(out_dir, exist_ok=True)
    # 文件名带时间戳：连点两次「试听」时，旧实现的固定名 sample.mp3 会被第二次
    # 先删后写，导致第一次正在播放的请求读到 404 或半截文件
    out = os.path.join(out_dir, 'sample_%d.mp3' % int(time.time() * 1000))
    ok, eng, path = local_tts_speak(text, out)
    if not ok or not os.path.exists(path):
        hint = ''
        if not edge_tts_available() and edge_tts_dead_reason():
            hint = '（edge-tts 暂时不可用：%s）' % edge_tts_dead_reason()
        return False, '配音失败：没有可用的配音引擎，先装 edge-tts 或下载离线模型' + hint, None, ''
    rel = os.path.relpath(path, OUTDIR).replace('\\', '/')
    label = {'edge': 'edge-tts', 'sherpa': '离线模型', 'sapi': '系统 SAPI'}.get(eng, eng or '未知')
    return True, '✅ 试听已生成（%s，%.1f 秒）' % (label, probe_audio_len(path) or 0), eng, rel


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


def whisper_device():
    """返回 (device, compute_type)：检测到 NVIDIA CUDA 就用 GPU 加速（float16），否则回退 CPU(int8)。
    让「省流(本地离线)」模式在有无显卡的机器上都能跑，且尽量用显卡提速。"""
    if _cuda_available():
        return 'cuda', 'float16'
    return 'cpu', 'int8'


def _fmt_hms(sec):
    sec = max(0.0, float(sec or 0))
    m, s = int(sec // 60), int(sec % 60)
    return '%d:%02d' % (m, s)


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


def _local_narrate(per_seg, params, plot=None):
    """省流 + 本地模型：用本地 qwen/ollama 等离线生成/改写每段解说词（0 元、不调云端）。
    返回 (lines, True)。行数不足时按上文口吻续写（见 _fill_missing_lines），不回填模板。"""
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
    # 旧提示词只说「讲发生了什么」，模型于是自由发挥成抒情散文
    # （实测产出「像举着半透明的糖纸」「油菜花的呼吸声」这类文艺腔），
    # 这里给出明确结构 + 禁用词表，把风格压回「讲故事的口播稿」。
    prompt = ('你是一个电影解说文案助手。下面是一段视频的分段时间轴与台词。\n'
              + ('补充信息：\n' + '\n'.join(ctx) + '\n' if ctx else '')
              + '请生成一段面向观众的连贯中文解说稿：\n'
                '- 第 1 段开场：点出主题（不确定片名就用“故事从……”自然引入），抛出一个抓人的悬念。\n'
                '- 中间每段：承接上文，讲清楚「谁 + 做了什么 + 为什么」，'
                '用口语推进节奏（没想到／就在这时／结果／也正是这时）。\n'
                '- 最后一段：收束或升华一句。\n'
                '- 每段 40~90 字，口播风格，句子要能直接念出来。\n'
                '- 严禁文艺腔与抒情排比；严禁出现这些词：画面里、镜头中、我们看到、仿佛、像极了、藏着。\n'
                '- 严格按顺序每段一行输出，不要编号、不要引号、不要括号、不要解释。\n\n' + brief)
    _style = NARR_STYLES.get(params.get('narr_style', 'movie'), NARR_STYLES['movie'])
    _detail = DETAIL_LEVELS.get(params.get('detail_level', 'balanced'), 1.0)
    _lo, _hi = int(40 * _detail), int(90 * _detail)
    prompt = prompt.replace('每段 40~90 字', '每段 %d~%d 字' % (_lo, _hi))
    text = local_llm_chat(prompt, system=_style['system'])
    lines = [l.strip().strip('"').strip() for l in text.splitlines() if l.strip()]
    if len(lines) < len(per_seg):
        # 行数不足：按上文口吻续写缺失镜头（不再回填模板，避免风格断裂/内容错配）
        filled = _fill_missing_lines(lines, per_seg[len(lines):], params, plot=plot)
        lines = lines + filled
    if len(lines) < len(per_seg):
        # 终极兜底：把已有行按镜头数分布（不复制、不模板）
        lines = _map_lines_to_segs(lines, len(per_seg))
    return lines[:len(per_seg)], True


def _fill_missing_lines(existing, remaining, params, plot=None):
    """解说行数不足时，按上文口吻补写剩余镜头的解说（避免回填模板导致风格断裂/内容错配）。
    返回补写的行列表（长度 ≤ len(remaining)）；模型不可用返回 []。"""
    if not remaining:
        return []
    name = (params.get('name') or '').strip()
    theme = (params.get('theme') or '').strip()
    req = (params.get('req') or '').strip()
    ctx = []
    if name:
        ctx.append('视频：' + name)
    if theme:
        ctx.append('主题/梗概：' + theme)
    if plot:
        ctx.append('【剧情理解】' + plot)
    prompt = ('你正在为一段视频写中文电影解说稿，已经写好了前面的段落（请保持口吻一致）：\n'
              + ('\n'.join('·' + l for l in existing) if existing else '（前面还没有内容）') + '\n\n'
              '请继续为下面 %d 个镜头环节各写一句解说，承接上文、自然衔接、讲剧情不描述画面：\n' % len(remaining)
              + '\n'.join('%d. %s-%ss %s' % (i + 1, int(s0), int(s1),
                                            ('台词：' + t[:80] if t else '无台词画面'))
                          for i, (s0, s1, t) in enumerate(remaining))
              + '\n严格按镜头顺序输出 %d 行，不要编号、不要解释。' % len(remaining))
    sys_ = '你是资深电影解说博主，口气自然、像真人聊天讲故事一样。'
    if req:
        prompt += '\n【额外要求】' + req
    try:
        out = _llm_text(prompt, system=sys_, timeout=180)
    except Exception:
        return []
    if not out:
        return []
    lines = _split_nar_lines(out)
    if not lines:
        return []
    return lines[:len(remaining)] if len(lines) >= len(remaining) else lines


def generate_narration(segs, asr, params, frames=None, plot=None, beat_outline=None):
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
            return local_vlm_narrate(per_seg, frames, params, plot=plot, beat_outline=beat_outline)
        except Exception:
            pass
    if local_llm_enabled():
        # ② 本地文本模型改写（无画面理解）
        try:
            return _local_narrate(per_seg, params, plot=plot)
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
                 '- 第 1 段：开场即钩子（黄金 7 秒）：用反常理悬念或人性拷问破题，'
                 '严禁“今天讲一部关于XX的电影”式平淡开场；若画面能确认影视作品，点出片名/年代/背景（不确定不要编造片名）；\n'
                 '- 后续每段：承接上文，叙述本段剧情本身（人物做了什么/事态怎么变），像讲故事；'
                 '除非这段真是剧情转折/高光，否则不要总结“这反映了/象征着/揭示了”这类意义升华；\n'
                 '- 详略有当：关键/转折/高光段展开讲（2~3 句），过渡/铺垫段一句带过，不要平均用力；\n'
                 '- 口语化讲述感：短句、多动词少形容词，单句别超过 20 字不停顿，可用「我们/你我」的唠嗑感；\n'
                 '- 【中段克制·结尾升华】中间各行不总结不升华，只推进剧情与情绪；最后一段金句收尾：'
                 '把故事映射到现实共鸣（职场/婚姻/原生家庭/阶层），≤2 句散文诗式总结；\n'
                 '- 【红线】涉暴力用温和词、侧重心理而非过程；主角若违法，须点出“违法行为终将受到法律制裁”；\n'
                 '- 每段 20~120 字，口播风格、有推进感；台词转述、不要原样引用对话；'
                 '不编造剧情外事实；不堆“高潮/悬念/震撼”等空泛词；\n'
                 '- 严格按顺序每段一行输出，不要编号、不要引号、不要解释。\n\n')
        genre = (params.get('genre') or '').strip()
        if genre and genre != 'auto':
            g_block = _genre_template_block(genre)
            if g_block:
                instr = instr + g_block + '\n\n'
        if req_txt:
            instr += '【额外要求】' + req_txt + '\n\n'
        payload = {
            'model': cfg.get('model'),
            'messages': [{'role': 'user', 'content': instr + (plot_ctx + '\n\n' if plot_ctx else '') + brief}],
            'max_tokens': 1800,
            'temperature': 0.5,
        }
        url = (cfg.get('base_url', '').rstrip('/')) + '/chat/completions'
        req = urllib.request.Request(url, data=_json.dumps(payload).encode('utf-8'),
                                     headers={'Content-Type': 'application/json',
                                              'Authorization': 'Bearer ' + cfg.get('api_key', '')})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = _json.loads(resp.read().decode('utf-8'))
        lines = [l.strip().strip('"').strip() for l in data['choices'][0]['message']['content'].splitlines() if l.strip()]
        if len(lines) < len(per_seg):
            # 行数不足：按上文口吻续写缺失镜头（不再回填模板，避免风格断裂/内容错配）
            filled = _fill_missing_lines(lines, per_seg[len(lines):], params, plot=plot)
            lines = lines + filled
        if len(lines) < len(per_seg):
            # 终极兜底：把已有行按镜头数分布（不复制、不模板）
            lines = _map_lines_to_segs(lines, len(per_seg))
        return lines[:len(per_seg)], False
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


# ---------------------------------------------------------------------------
# 内容感知 · 主线剪辑与解说密度调控（解决「密度异常 / 时间轴错位 / 不剪主线」）
# ---------------------------------------------------------------------------
_IMP_ORDER = {'transition': 0, 'mood': 1, 'advance': 2, 'key': 3}

def _max_imp(a, b):
    return a if _IMP_ORDER.get(a, 1) >= _IMP_ORDER.get(b, 1) else b

def _cap_seg_duration(segs, cap):
    """把超过 cap 秒的镜头段在内部均分切开，避免单条解说词扛过长画面导致时间轴错位。"""
    out = []
    for (s, e) in segs:
        if e - s <= cap + 1e-6:
            out.append((s, e)); continue
        n = max(2, int(-(-(e - s) // cap)) or 2)  # 向上取整，保证每片 <= cap
        for j in range(n):
            out.append((s + (e - s) * j / n, s + (e - s) * (j + 1) / n))
    return out

def _split_unit_at_gaps(fine_segs, members, cap):
    """把总时长超 cap 的成员组合，按内部成员边界切成 ≤cap 的子段（保留场景切点，不硬切）。"""
    out = []
    cur_s = fine_segs[members[0]][0]; cur_e = cur_s; cur_dur = 0.0
    for m in members:
        s, e = fine_segs[m]
        if cur_dur + (e - s) > cap and cur_dur > 0:
            out.append((cur_s, cur_e)); cur_s = s; cur_dur = 0.0
        cur_e = e; cur_dur += (e - s)
    if cur_dur > 0:
        out.append((cur_s, cur_e))
    return out or [(fine_segs[members[0]][0], fine_segs[members[-1]][1])]

def _condense_segs(fine_segs, asr, params, plot=None, beat_plan=None, frames=None):
    """把细粒度场景段浓缩为「解说环节」：

    - 有模型时：依 _beat_plan 的重要性，把连续的 transition/mood（且无台词）并入相邻主线段，
      实现「密度正常 + 聚焦主线」——过渡段不再被平等解说；纯填充微段(<2.5s 无台词、非主线)标记 keep=False 供剪辑主干时剪除。
    - 所有段按重要性施加时长上限（key/advance ≤14s，过渡 ≤20s），保证解说词贴合当前画面（时间轴匹配）。
    - 无模型时退化为 _merge_segs + 时长上限（纯离线也不至于单条解说扛 30s）。

    返回 (condensed_segs, outline)；outline 为 [{start,end,importance,keep}]，长度 == len(condensed)。"""
    if not fine_segs:
        return [], []
    n = len(fine_segs)
    has_dlg = [bool(_asr_text_in(asr, s, e).strip()) for (s, e) in fine_segs]
    if beat_plan is None and (_local_model_available() or vlm_enabled()):
        per_seg = [(s0, s1, _asr_text_in(asr, s0, s1)) for (s0, s1) in fine_segs]
        if plot is None and frames:
            plot = _plot_brief(frames, per_seg, params)
        beat_plan = _beat_plan(per_seg, plot, params)
    if not beat_plan or not isinstance(beat_plan.get('beats'), list):
        # 离线兜底：时间均分 + 时长上限
        merged = _merge_segs(fine_segs)
        capped = _cap_seg_duration(merged, 16.0)
        return capped, [{'start': s, 'end': e, 'importance': 'advance', 'keep': True} for (s, e) in capped]
    imp = []
    for i in range(n):
        b = beat_plan['beats'][i] if i < len(beat_plan['beats']) and isinstance(beat_plan['beats'][i], dict) else {}
        v = str(b.get('importance', 'advance') or 'advance')
        imp.append(v if v in _IMP_ORDER else 'advance')
    # 分组：长过渡(无台词)并入相邻主线段（密度正常、聚焦主线）；
    # 纯填充微段(<2.5s 无台词、非主线)单独成段并标记 keep=False（剪辑主线时从成片剪除）。
    groups = []          # (members, unit_imp, keep)
    cur, cur_imp = None, None
    for i in range(n):
        dur = fine_segs[i][1] - fine_segs[i][0]
        is_filler = imp[i] in ('transition', 'mood')
        is_micro = is_filler and not has_dlg[i] and dur < 2.5
        if is_micro:
            if cur is not None:
                groups.append((list(cur), cur_imp, True))
            groups.append(([i], imp[i], False))   # 可剪：渲染时不进成片
            cur, cur_imp = None, None
            continue
        merge = cur is not None and is_filler and not has_dlg[i]
        if merge:
            cur.append(i); cur_imp = _max_imp(cur_imp, imp[i])
        else:
            if cur is not None:
                groups.append((list(cur), cur_imp, True))
            cur, cur_imp = [i], imp[i]
    if cur is not None:
        groups.append((list(cur), cur_imp, True))
    condensed, outline = [], []
    for members, uimp, keep in groups:
        s0 = fine_segs[members[0]][0]; s1 = fine_segs[members[-1]][1]
        cap = 14.0 if uimp in ('key', 'advance') else 20.0
        if s1 - s0 > cap:
            sub = _split_unit_at_gaps(fine_segs, members, cap)
        else:
            sub = [(s0, s1)]
        for (a, b) in sub:
            condensed.append((a, b))
            outline.append({'start': a, 'end': b, 'importance': uimp, 'keep': keep})
    return condensed, outline


def _narrate_candidate_shots(video_path, params):
    """解说「候选镜头」＝未合并的细粒度场景段（供用户改完解说词后重新匹配分镜用）。
    场景切点命中缓存，重复调用几乎零成本。若切点太少（长镜头视频），再按 maxSeg 细分，
    保证重匹配时有足够的可组合粒度。"""
    segs = _segment_timeline(video_path, max_seg=float((params or {}).get('maxSeg', 25)))
    if not segs:
        return []
    return segs


def _llm_text(prompt, system='', timeout=180):
    """本地文字模型优先（写稿主力），不可用/失败时回退视觉模型的文字通道；都不可用返回 None。"""
    if _local_model_available():
        try:
            r = local_llm_chat(prompt, system=system, timeout=timeout)
            if r and r.strip():
                return r
        except Exception:
            pass
    try:
        return vlm_text(prompt, system=system, timeout=timeout)
    except Exception:
        return None


def _asr_text_in(asr, s, e):
    """取时间窗 [s,e) 内的台词（按台词中点归窗，与解说稿聚合口径一致）。"""
    parts = []
    for x in (asr or []):
        try:
            mid = (float(x.get('start', 0)) + float(x.get('end', 0))) / 2.0
        except Exception:
            continue
        if s <= mid < e:
            t = (x.get('text') or '').strip()
            if t:
                parts.append(t)
    return ' '.join(parts)[:120]


def _algo_align_shots(shots, lines):
    """算法兜底：按解说词字数权重把镜头顺序分配给各句（不调模型，离线可用）。
    返回 bounds：第 i 句解说对应的最后一个镜头编号（1-based）。"""
    m, k = len(shots), len(lines)
    if m <= 0 or k <= 0:
        return []
    w = [max(1, len(str(l))) for l in lines]
    tot = float(sum(w))
    bounds, acc = [], 0
    for i in range(k):
        acc += w[i]
        b = int(round(acc * m / tot))
        # 每句至少 1 个镜头，且给后面的句子留够（避免最后几句分不到镜头）
        b = max(i + 1, min(m - (k - 1 - i), b))
        bounds.append(b)
    return bounds


def _model_align_shots(shots, lines, asr=None, params=None):
    """让模型按解说词语义把镜头分配给各句（asr 仅作语义依据，可缺省）。
    返回 bounds(list[int])；失败/结果不合法返回 None，由调用方回退到 _algo_align_shots。"""
    m, k = len(shots), len(lines)
    if m <= 0 or k <= 0:
        return None
    shot_lines = []
    for i, (s, e) in enumerate(shots, 1):
        t = _asr_text_in(asr, s, e)
        shot_lines.append('%d [%.1f-%.1fs] %s' % (i, s, e, ('台词：' + t) if t else '（无台词画面）'))
    prompt = ('下面是视频的镜头清单（编号 / 时间 / 该镜头里的台词），以及用户改写后的解说词。\n'
              '请把每个镜头分配给【内容最贴合】的那句解说。\n\n'
              '【镜头清单】\n' + '\n'.join(shot_lines)
              + '\n\n【解说词】共 %d 句\n' % k
              + '\n'.join('%d. %s' % (i + 1, l) for i, l in enumerate(lines))
              + '\n\n要求：\n'
              '- 必须按镜头顺序分配：不能颠倒、不能跳号、不能遗漏，每个镜头只能属于一句解说；\n'
              '- 一句解说可以对应一个或多个【连续】镜头；\n'
              '- 输出一个 JSON 数组，共 %d 个整数，第 i 个数 = 第 i 句解说对应的最后一个镜头编号；\n' % k
              + '- 数组最后一个数必须等于 %d（总镜头数），保证全部镜头都被覆盖。\n' % m
              + '只输出这个 JSON 数组，不要解释、不要其他文字。')
    out = _llm_text(prompt, '你是影视剪辑师，擅长把解说词与画面对位。', timeout=180)
    if not out:
        return None
    import re as _re, json as _json
    m2 = _re.search(r'\[[^\]]*\]', out, _re.S)
    if not m2:
        return None
    try:
        arr = _json.loads(m2.group(0))
    except Exception:
        return None
    if not isinstance(arr, list) or len(arr) != k:
        return None
    bounds = []
    for v in arr:
        try:
            bounds.append(int(round(float(v))))
        except Exception:
            return None
    # 合法化：单调递增、每句至少 1 个镜头、末值 = 总镜头数
    for i in range(k):
        lo, hi = i + 1, m - (k - 1 - i)
        if bounds[i] < lo:
            bounds[i] = lo
        elif bounds[i] > hi:
            bounds[i] = hi
    for i in range(1, k):
        if bounds[i] < bounds[i - 1]:
            return None      # 出现倒序 → 判定模型输出不可用，交给算法兜底
    if bounds[-1] != m:
        bounds[-1] = m
    return bounds


def _expand_shots(shots, k):
    """候选镜头数少于解说句数时，把每个镜头按时间均分成若干子段，
    保证每句解说至少能分到一个画面单元（子段仍在原镜头内，不跨镜头）。
    否则「每句至少 1 个镜头」的约束无解，会导致索引越界。"""
    m = len(shots)
    if m <= 0 or k <= 0 or m >= k:
        return shots
    per = -(-k // m)          # 整数向上取整，免 import math
    out = []
    for (s, e) in shots:
        span = (e - s) / float(per)
        for j in range(per):
            out.append((s + span * j, s + span * (j + 1)))
    return out


def _align_shots_to_lines(shots, lines, asr=None, params=None, use_model=True):
    """把候选镜头按（用户改写后的）解说词重新分配，产出新的分镜段。
    模型语义匹配优先；模型不可用/输出不合法时回退按字数权重的算法分配。
    返回 (segs, source)；segs 为 [(start, end)]，长度 = len(lines)。"""
    if not shots or not lines:
        return [], 'none'
    # 句数多于镜头数时先细分镜头，保证分配有解（否则每句至少 1 镜头不可满足 → 越界崩溃）
    shots = _expand_shots(shots, len(lines))
    bounds = None
    if use_model:
        bounds = _model_align_shots(shots, lines, asr, params)
    src = 'model'
    if not bounds:
        bounds = _algo_align_shots(shots, lines)
        src = 'algo'
    if not bounds:
        return [], 'none'
    segs, prev = [], 0
    for b in bounds:
        if prev >= len(shots):      # 防御：镜头已分完，剩余句子复用最后一段
            segs.append(segs[-1] if segs else (float(shots[-1][0]), float(shots[-1][1])))
            continue
        b = max(prev + 1, min(len(shots), b))
        segs.append((float(shots[prev][0]), float(shots[b - 1][1])))
        prev = b
    return segs, src


def _narrate_analysis(video_path, params, run_dir, progress=None):
    """解说分析公共层：分段→ASR台词→(可选)关键帧→内容感知主线浓缩→解说稿。
    「人机协同分析(/api/plan)」与「直接生成解说(narrate_video)」共用此流程，
    避免两份逐行重复的实现各自漂移。
    返回 (segs, narr, asr, frames, mode, outline)；outline 为 [{start,end,importance,keep}]，
    标记每个解说环节是主线(key/advance)还是过渡/氛围(transition/mood)，供「剪辑主线」使用。"""
    def up(ph, pct):
        if progress:
            progress['phase'] = ph; progress['pct'] = pct
    # 同上：分段 / Whisper / 抽帧 / 生成解说稿都是无 ffmpeg 的长阶段，靠协作式取消响应「⏹ 停止」
    up('场景分段', 4)
    if _aborted():
        raise AbortError('用户取消了任务')
    fine = _segment_timeline(video_path, max_seg=float(params.get('maxSeg', 25)))
    if not fine:
        raise RuntimeError('无法分析视频时长')
    up('识别台词(本地Whisper)', 10)
    if _aborted():
        raise AbortError('用户取消了任务')
    # 传入 progress：十几分钟的长视频转写要跑很久，没有逐段进度就是「看着像卡死」
    asr = asr_segments(video_path, progress=progress, pct_range=(10, 16))
    need_frames = vlm_enabled() or ai_enabled('vision')   # 任一视觉能力可用就抽帧（自动选路）
    frames = {}
    if need_frames:
        up('抽取关键帧(供视觉理解)', 16)
        frames = extract_segment_frames(video_path, fine, os.path.join(run_dir, 'frames'))
    # 内容感知主线浓缩：复用视觉理解 + 节拍规划，按重要性合并过渡段、剪纯填充微段、按时长上限防时间轴错位
    up('规划主线与解说密度', 18)
    per_seg = [(s0, s1, _asr_text_in(asr, s0, s1)) for (s0, s1) in fine]
    plot = _plot_brief(frames, per_seg, params) if frames else None
    beat_plan = _beat_plan(per_seg, plot, params) if (_local_model_available() or vlm_enabled()) else None
    segs, outline = _condense_segs(fine, asr, params, plot=plot, beat_plan=beat_plan, frames=frames)
    if not segs:
        segs = fine
        outline = [{'start': s, 'end': e, 'importance': 'advance', 'keep': True} for (s, e) in fine]
    # 剪辑主线：剪除纯填充微段（keep=False），让成片聚焦主线、密度正常
    kept = [(s, o) for s, o in zip(segs, outline) if o.get('keep', True)]
    if kept:
        segs = [s for s, _ in kept]
        outline = [o for _, o in kept]
    if _aborted():
        raise AbortError('用户取消了任务')
    up('生成解说稿', 22)
    narr, used_local = generate_narration(segs, asr, params, frames=frames, plot=plot, beat_outline=outline)
    mode = None
    if vlm_enabled() and frames:
        mode = 'vlm'
    elif used_local:
        mode = 'local'
    return segs, narr, asr, frames, mode, outline


def _analyze_narrate(video_path, params, run_dir, progress=None):
    """解说分析阶段：分段→ASR台词→解说稿→(可选)关键帧。返回 (segs, narr, asr, diag, mode, outline)。
    拆出供「人机协同」复用：用户可在预览界面编辑每段解说词/删除段后再渲染。"""
    segs, narr, asr, frames, mode, outline = _narrate_analysis(video_path, params, run_dir, progress)
    diag = {'segments': len(segs), 'asr_lines': len(asr), 'narration': narr}
    return segs, narr, asr, diag, mode, outline


# ---------------------------------------------------------------------------
# 配音时长自适应（解决「解说词念不完被腰斩 / 念完还剩一大段画面空窗」的时间轴错位）
# ---------------------------------------------------------------------------
_NAR_CPS = 4.6          # 中文口播经验语速：字/秒（SAPI 与云端 TTS 实测的折中值）
_NAR_MIN_CHARS = 12     # 极短镜头也至少说满一句话，避免只剩半句
_NAR_MAX_CHARS = 95     # 单段解说上限，避免长镜头堆字导致语速被迫过快
_NAR_MAX_SPEED = 1.35   # atempo 最大加速倍率，超过会有明显失真
_NAR_MIN_SPEED = 1.03   # 低于此倍率听不出差别，不必重编码


def _target_chars(dur):
    """把画面时长换算成解说词目标字数区间 (lo, hi)。

    配音时长 ≈ 字数 / _NAR_CPS；让字数贴合时长，解说才不会溢出到下一个镜头
    （溢出会被 atrim 腰斩）也不会念完还剩大片空窗。"""
    try:
        dur = float(dur)
    except Exception:
        dur = 5.0
    if dur <= 0:
        dur = 5.0
    base = max(_NAR_MIN_CHARS * 1.0, min(float(_NAR_MAX_CHARS), dur * _NAR_CPS))
    return (int(round(base * 0.80)), int(round(base * 1.05)))


def _fit_voice(voice_len, span_len):
    """给出让配音贴合画面时长的策略。

    返回 {'speed': 建议 atempo 倍率, 'trim': 加速后是否仍需截断, 'over': 溢出秒数(负=空窗)}。
    - 配音长于画面：适度加速（上限 _NAR_MAX_SPEED），仍超则标记 trim 交给下游裁剪。
    - 配音短于画面：不加速（宁可留白也不拖慢口播），over 为负数表示空窗时长。"""
    try:
        voice_len = float(voice_len); span_len = float(span_len)
    except Exception:
        return {'speed': 1.0, 'trim': False, 'over': 0.0}
    if span_len <= 0.3:
        return {'speed': 1.0, 'trim': False, 'over': voice_len}
    over = voice_len - span_len
    if over <= 0:
        return {'speed': 1.0, 'trim': False, 'over': over}
    need = voice_len / span_len
    if need <= _NAR_MAX_SPEED:
        return {'speed': round(need, 3), 'trim': False, 'over': over}
    return {'speed': _NAR_MAX_SPEED, 'trim': True, 'over': over}


def _clamp_line(text, max_chars):
    """把解说词按【句读】截断到 max_chars 字以内，绝不把句子切在半截词中间。

    优先在句号/感叹/疑问/分号处断开；没有句读时退到逗号/顿号；都没有才硬切。
    超长解说若不截断，配音会被 atrim 在段末腰斩——听众听到一半就没了。"""
    t = (text or '').strip()
    if max_chars is None or max_chars <= 0 or len(t) <= max_chars:
        return t
    import re as _re
    window = t[:max_chars]
    # 从后往前找最近的自然断点
    for pat in (r'[。！？；]', r'[，、,;:]', r'\s'):
        hits = list(_re.finditer(pat, window))
        if hits:
            cut = hits[-1].end()
            # 断点太靠前（丢掉超过 40% 内容）就不值得断，宁可硬切保留更多信息
            if cut >= max_chars * 0.6:
                return window[:cut].strip()
    return window.strip()


def _render_narrate(video_path, segs, narr, params, run_dir, progress=None, music_path=None, mode=None,
                    auto_cut=True, narr_map=None):
    """解说渲染阶段：按分镜剪辑(可选)→逐段配音→混音→烧字幕→配乐。

    auto_cut=True 时先按保留段真剪辑（剪掉未勾选/无解说的画面），字幕与配音自动对齐到
    剪辑后的新时间轴。返回 (final, voice_clips, cut_info)。"""
    def up(ph, pct):
        if progress:
            progress['phase'] = ph; progress['pct'] = pct

    # ---- 第一步：真剪辑（此前缺失，导致成片恒等于原片时长，「剪辑解说」名不副实）----
    src_video = video_path
    cut_info = {'cut_sec': 0.0, 'src_dur': round(probe_audio_len(video_path) or 0.0, 2),
                'out_dur': None, 'segs': len(segs)}
    if auto_cut:
        up('按分镜剪辑画面', 26)
        src_video, segs, cut_sec = _cut_video_by_spans(video_path, segs, run_dir, progress)
        cut_info['cut_sec'] = cut_sec
        cut_info['segs'] = len(segs)
    cut_info['out_dur'] = round(probe_audio_len(src_video) or cut_info['src_dur'], 2)
    # 高密度剪辑：一节解说词对应多个子片段，按 narr_map 聚合
    if narr_map and len(narr_map) == len(segs):
        beat_ranges = []
        for bi in range(len(narr)):
            bsegs = [segs[k] for k in range(len(segs)) if narr_map[k] == bi]
            if bsegs:
                beat_ranges.append((bsegs[0][0], bsegs[-1][1]))
            else:
                beat_ranges.append((0.0, 0.0))
        segs = beat_ranges
        print(f'[DIAG] 高密度剪辑: {len(narr)}节 -> {len(narr_map)}个片段')
    print(f'[DIAG] auto_cut后: segs={len(segs)} 总时长={sum(b-a for a,b in segs):.1f}s 视频时长={cut_info["out_dur"]}s narr={len(narr)}')

    # 长度对齐保护：narr 与 segs 必须一一对应。模型偶尔多输出/少输出行，
    # 不修正会导致越界（i >= len(segs) 时全部堆在 0-10s）或后面段无解说。
    if len(narr) > len(segs):
        narr = narr[:len(segs)]
    elif len(narr) < len(segs):
        narr = list(narr) + [''] * (len(segs) - len(narr))

    up('逐段配音', 30)
    tts_paths = []
    voice_spans = {}   # seg_idx -> (start, end)：字幕窗口跟随配音（有声才显字、念完即收）
    # 只在云端 TTS 真正配了 api_key+model 时才走云端；否则直接用本地免费引擎
    # 旧写法 _tts_available() 会把本地引擎也算进去，导致每段先试云端（无key必失败）再回退本地，长视频严重拖慢甚至后面超时失声
    _tcfg = load_ai_config().get('tts') or {}
    use_mimo = bool(_tcfg.get('api_key')) and bool(_tcfg.get('model'))
    for i, txt in enumerate(narr):
        if _aborted():
            raise AbortError('用户取消了任务')
        if not (txt and txt.strip()):
            continue
        if txt.strip() in ('（留白）', '(留白)'):
            continue   # 留白段：不配音不出字幕，让原片声音飞（第五原则·留白意识）
        seg_span = segs[i] if i < len(segs) else (0.0, 10.0)
        span_len = max(0.0, seg_span[1] - seg_span[0])
        # 配音前先按画面时长做字数硬上限兜底：给足 _NAR_MAX_SPEED 的加速余量，
        # 超出的部分宁可精简，也不要让配音被 atrim 在段末腰斩（听众只听到半句）
        hard_cap = int(round(_target_chars(span_len)[1] * _NAR_MAX_SPEED))
        spoken = _clamp_line(txt, hard_cap) or txt
        clip = None
        if use_mimo:
            np_ = os.path.join(run_dir, f'narr{i}.mp3')
            if ai_tts(spoken, np_):
                clip = np_
        if clip is None:
            # 本地免费配音：edge-tts（免 Key）→ 离线模型（sherpa-onnx）→ 系统 SAPI 兜底
            ok, _eng, lp = local_tts_speak(spoken, os.path.join(run_dir, f'narr{i}.mp3'))
            if ok:
                clip = lp
            else:
                print(f'[DIAG] TTS失败 seg={i} 字数={len(spoken)} 文本前20字={spoken[:20]}')
        if clip is not None:
            # 配音时长自适应：念不完就用 atempo 适度提速贴合镜头，避免跨段重叠/腰斩
            v_len = probe_audio_len(clip) or max(0.5, span_len)
            fit = _fit_voice(v_len, span_len)
            if fit['speed'] > _NAR_MIN_SPEED:
                fast = os.path.join(run_dir, f'narr{i}_fit.mp3')
                rc, _o, _e = ffmpeg_run(['-y', '-i', clip, '-vn',
                                         '-filter:a', 'atempo=%.3f' % fit['speed'],
                                         '-c:a', 'libmp3lame', '-q:a', '4', fast])
                if rc == 0 and os.path.exists(fast):
                    clip = fast
                    v_len = probe_audio_len(clip) or (v_len / fit['speed'])
            tts_paths.append((clip, seg_span[0], seg_span[1]))
            # 字幕只在「这句话正在被念」时显示：一行字挂满整个镜头段会让后段才发生的
            # 画面内容提前出现在段首，观感像字幕与时间轴错位
            voice_spans[i] = (seg_span[0], min(seg_span[1], seg_span[0] + v_len + 0.35))
    print(f'[DIAG] 配音完成: tts_paths={len(tts_paths)}/{len([t for t in narr if t and t.strip()])} voice_spans={len(voice_spans)}')
    up('混音+烧字幕+配乐', 60)
    narr_srt = ['' if (t or '').strip() in ('（留白）', '(留白)') else strip_tts_markup(t) for t in narr]
    final = _compose_narration_video(src_video, segs, narr_srt, tts_paths, run_dir, params,
                                     music_path=music_path, voice_spans=voice_spans)
    if progress:
        progress['done'] = True
        progress['pct'] = 100
        progress['file'] = os.path.relpath(final, OUTDIR).replace('\\', '/')
        if mode:
            progress['mode'] = mode
    return final, len(tts_paths), cut_info


def narrate_video(video_path, params, run_dir, progress=None, music_path=None):
    """电影解说主流程：分段→ASR→解说稿→SAPI/MiMo配音→混音→字幕→成片。
    music_path: 可选背景音乐，混入成品（按 Phase 2「配乐」要求）。"""
    segs, narr, asr, frames, mode, _outline = _narrate_analysis(video_path, params, run_dir, progress)
    if progress and mode:
        progress['mode'] = mode
    auto_cut = params.get('autoCut', True)
    final, vc, cut_info = _render_narrate(video_path, segs, narr, params, run_dir, progress=progress,
                                          music_path=music_path,
                                          mode=progress.get('mode') if progress else None,
                                          auto_cut=auto_cut)
    diag = {'segments': len(segs), 'asr_lines': len(asr), 'voice_clips': vc,
            'narration': narr, 'cut': cut_info}
    return final, diag
def _has_audio_track(p):
    """返回视频文件是否含音轨。"""
    rc, o, e = ffmpeg_run(['-i', p])
    return 'Audio:' in e.decode('utf-8', 'ignore')


def _clean_caption(text):
    """清洗单条字幕文案：去首尾空白/引号、把内部换行替换为空格、合并多余空格。
    LLM/模板输出偶尔带换行或引号，若原样写入 SRT 会破坏字幕时间轴格式。
    同时剥掉模型误输出的元信息括号（如「（画面：绿色田野+蓝天）」「（结尾金句）」）——
    这类注释一旦进配音，观众会听到「画面绿色田野」，非常出戏；（留白）是功能标记，保留。"""
    if not text:
        return ''
    import re as _re
    t = _re.sub(r'\s+', ' ', str(text)).strip()
    if t in ('（留白）', '(留白)'):
        return t
    # 元信息括号：画面/镜头/结尾金句/开场/钩子/旁白/字幕 等拍摄说明
    t = _re.sub(r'[（(]\s*(?:画面|镜头|结尾金句|开场|钩子|旁白|字幕|音效|转场)[^）)]{0,60}[）)]',
                '', t)
    # TTS控制标记：{停顿:0.6} {情绪:激动} {慢} {快} 等，字幕里不能出现
    t = _re.sub(r'\{(?:情绪|停顿|慢|快|高音|低音|大声|小声)[^}]*\}', '', t)
    t = _re.sub(r'\s+', ' ', t).strip()
    # 去掉首尾成对的引号（含中文弯引号）
    for a, b in (('"', '"'), ("'", "'"), ('“', '”'), ('‘', '’')):
        if len(t) >= 2 and t[0] == a and t[-1] == b:
            t = t[1:-1].strip()
    return t


def _merge_spans(spans, eps=0.05):
    """合并重叠/紧邻的区间并按时序排序，避免剪辑时同一段画面被重复拼接。"""
    out = []
    for s0, s1 in sorted((float(a), float(b)) for a, b in (spans or [])
                         if float(b) - float(a) > 0.02):
        if out and s0 <= out[-1][1] + eps:
            out[-1] = (out[-1][0], max(out[-1][1], s1))
        else:
            out.append((s0, s1))
    return out


def _cut_video_by_spans(video_path, spans, run_dir, progress=None):
    """按保留区间真剪辑：只留 spans 覆盖的画面，顺序拼成新片，并给出新时间轴。

    这是「剧情驱动剪辑」名副其实的关键。历史实现里解说链路只做「烧字幕 + 混音」，
    成片时长恒等于原片，用户在预览里取消勾选的段落画面照样留在成片里 —— 等于没剪。

    返回 (cut_path, new_spans, cut_seconds)：
    - new_spans[i] 是 spans[i] 在拼接后新片里的 (start, end)，字幕与配音必须按它对齐
    - cut_seconds 为被剪掉的总时长（0 表示未发生剪辑）
    剪辑失败时安全降级为 (video_path, spans, 0)：宁可不剪，也不因剪辑把出片搞崩。
    """
    def up(ph, pct):
        if progress is not None:
            progress['phase'] = ph
            progress['pct'] = pct

    vdur = probe_audio_len(video_path) or 0.0
    # 夹到 [0, vdur]，避免分析阶段给出的切点越界导致 ffmpeg 报错
    raw = [(max(0.0, min(vdur, float(a))), max(0.0, min(vdur, float(b))))
           for a, b in (spans or [])]
    raw = [(a, b) for a, b in raw if b - a > 0.02]
    if not raw or vdur <= 0:
        return video_path, raw, 0.0
    # 修正重叠段：分析阶段/用户微调可能产生重叠（后段 start < 前段 end），
    # 若不修正，_merge_spans 会合并重叠区，但 new_spans 仍按原始段长累计 →
    # new_spans 总时长 > 剪辑后实际视频时长 → 后半段字幕/配音落在视频结束点之后，用户看不到听不到。
    # 修正方式：后段 start 移到前段 end（重叠画面只出现一次，归属前段）。
    _fixed = []
    for a, b in raw:
        if _fixed and a < _fixed[-1][1]:
            a = _fixed[-1][1]
        # 修正后过短的片段给最小 0.5 秒时长，绝不过滤——过滤会导致 narr 与 segs 长度不匹配，
        # 后面的解说词无对应画面段，配音丢失、字幕错位。
        if b - a <= 0.02:
            b = min(vdur, a + 0.5)
        if b - a > 0.02:
            _fixed.append((a, b))
    raw = _fixed
    if not raw:
        return video_path, raw, 0.0

    # 剪切用的区间做合并（重叠/紧邻不重复切），但**返回的时间轴必须逐段等长**：
    # 调用方 segs 与 narr 是一一对应的，这里少返回一段就会让字幕与配音整体错位。
    spans = _merge_spans(raw)
    if not spans:
        return video_path, raw, 0.0

    keep = sum(b - a for a, b in spans)
    gap = vdur - keep
    # 连续覆盖全片（中间没有实质空隙）→ 没有可剪的内容，直接跳过，省一次全片重编码
    covered_gap = sum(max(0.0, spans[i + 1][0] - spans[i][1]) for i in range(len(spans) - 1))
    if spans[0][0] <= 0.05 and vdur - spans[-1][1] <= 0.05 and covered_gap <= 0.25:
        return video_path, raw, 0.0

    cut_dir = os.path.join(run_dir, 'cuts')
    os.makedirs(cut_dir, exist_ok=True)
    has_audio = _has_audio_track(video_path)
    pieces = []
    try:
        for i, (s0, s1) in enumerate(spans):
            up('✂ 剪辑片段 %d/%d' % (i + 1, len(spans)), 34 + int(20 * i / max(1, len(spans))))
            p = os.path.join(cut_dir, 'cut%03d.mp4' % i)
            # -ss 放 -i 前走快 seek（重编码时仍精确到帧）；-t 用段长，避免 -to 语义混淆
            cmd = ['-y', '-ss', '%.3f' % s0, '-i', video_path, '-t', '%.3f' % (s1 - s0)]
            cmd += video_encode_args()
            cmd += ['-threads', '0']
            if has_audio:
                cmd += ['-c:a', 'aac', '-b:a', '160k', '-ar', '44100', '-ac', '2']
            else:
                cmd += ['-an']
            cmd += [p]
            rc, _o, e = ffmpeg_run(cmd)
            if rc != 0 or not os.path.exists(p):
                raise RuntimeError('片段 %d 剪切失败: %s' % (i, e.decode('utf-8', 'ignore')[-200:]))
            pieces.append(p)

        out = os.path.join(run_dir, 'cut.mp4')
        concat_txt = os.path.join(cut_dir, 'concat.txt')
        with open(concat_txt, 'w', encoding='utf-8') as f:
            for p in pieces:
                f.write("file '%s'\n" % p.replace('\\', '/').replace("'", "'\\''"))
        up('拼接保留片段', 56)
        rc, _o, e = ffmpeg_run(['-y', '-f', 'concat', '-safe', '0', '-i', concat_txt,
                                '-c', 'copy', '-movflags', '+faststart', out])
        if rc != 0 or not os.path.exists(out):
            # 各段编码参数不一致时 copy 会失败 → 兜底 filter concat（重编码，慢但稳）
            inputs = []
            for p in pieces:
                inputs += ['-i', p]
            fc = ''.join('[%d:v]' % i for i in range(len(pieces)))
            if has_audio:
                fc += ''.join('[%d:a]' % i for i in range(len(pieces)))
                fc += 'concat=n=%d:v=1:a=1[vout][aout]' % len(pieces)
                cmd = ['-y'] + inputs + ['-filter_complex', fc, '-map', '[vout]', '-map', '[aout]']
                cmd += video_encode_args() + ['-c:a', 'aac', '-b:a', '160k', '-threads', '0', out]
            else:
                fc += 'concat=n=%d:v=1:a=0[vout]' % len(pieces)
                cmd = ['-y'] + inputs + ['-filter_complex', fc, '-map', '[vout]']
                cmd += video_encode_args() + ['-threads', '0', out]
            rc, _o, e = ffmpeg_run(cmd)
        if rc != 0 or not os.path.exists(out):
            raise RuntimeError('拼接失败: ' + e.decode('utf-8', 'ignore')[-300:])
    except Exception:
        # 剪辑属增强项：失败就退回原片，保证「能出片」优先于「剪得漂亮」
        return video_path, raw, 0.0

    # 拼接后的新时间轴：按原始段逐段累计（段数与输入严格一致，保证与解说词一一对应）
    new_spans, cur = [], 0.0
    for a, b in raw:
        new_spans.append((round(cur, 3), round(cur + (b - a), 3)))
        cur += (b - a)
    # -c copy 拼接 VFR 视频时，各片段实际时长可能与 -t 指定的有偏差，
    # 导致拼接后视频实际时长 != new_spans 总时长，后面的配音/字幕落在视频结束点之后。
    # 修复：ffprobe 检查实际时长，偏差>0.5s 时按比例缩放 new_spans，保证与视频对齐。
    actual_dur = probe_audio_len(out) or cur
    expected_dur = cur
    if actual_dur > 0 and abs(actual_dur - expected_dur) > 0.5:
        scale = actual_dur / expected_dur
        print(f'[DIAG] 拼接时长偏差: 预期={expected_dur:.1f}s 实际={actual_dur:.1f}s 缩放={scale:.3f}')
        new_spans = [(round(s * scale, 3), round(min(actual_dur, e * scale), 3)) for s, e in new_spans]
    # 被剪掉的总时长 = 原片时长 - 保留时长（gap 就是这个值，别再扣一次段间空隙）
    return out, new_spans, round(max(0.0, gap), 3)


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


# ---------------------------------------------------------------------------
# 影视剧情解说稿（「解说驱动剪辑」的核心）
#
# 旧实现 llm_movie_script 的三个硬伤，正是「怎么调都达不到普遍解说效果」的根因：
#   1. 每句被硬截断到 30/40 字（_split_sentences 的 l[:30]、_parse_events 的 desc[:40]）
#      → 解说碎片化，讲不成故事，甚至出现「随后追」这种半截话。
#   2. 每个事件各自独立成句、互不衔接 → 没有开场钩子、没有因果推进、没有结尾升华。
#   3. 事件条数由模型随意给 6-12 条，与画面段数无关 → 剧情只能往画面上「贴」，
#      段少了剧情被丢弃、段多了剧情被复制铺满，等于没剪。
#
# llm_movie_full_script 改为一次产出完整的、可直接口播的解说稿；画面反过来去迁就它。
# ---------------------------------------------------------------------------
NAR_BEAT_MIN = 40       # 每节解说词最少字数：少于此讲不成完整的一句
NAR_BEAT_MAX = 90       # 每节最多字数：超过则口播发赶、字幕一行放不下
NAR_SCRIPT_CPS = 5.2    # 影视解说话速（字/秒）：比普通解说略快，更贴近 B 站观感
NAR_SEG_TARGET = 5.0    # 每个视频子片段目标时长（秒）：一节解说词拆成多个短片段，提升剪辑密度

SCRIPT_STYLE_MOVIE = (
    '你是资深影视解说博主（B站/抖音影视解说风格）。请根据片名与剧情资料，'
    '写一篇可以直接照着念的完整解说稿。\n\n'
    '【结构】三段，缺一不可：\n'
    '1. hook 开场：1-2 句，点出片名，抛出全片最抓人的悬念或反差，但不要剧透结局。\n'
    '2. beats 主体：按剧情时间顺序分成若干节，每节 40-90 字，讲清楚人物、动机、'
    '冲突与转折，让没看过的人也能听懂。\n'
    '3. outro 结尾：1-2 句，点题或升华，给出回味。\n\n'
    '【每节写法】\n'
    '- 讲「谁 + 做了什么 + 为什么」，不要描述画面（禁止出现：画面里、镜头中、我们看到）。\n'
    '- 人物第一次出现要带上姓名与身份，例如「副警长瑞克」「搭档肖恩」。\n'
    '- 用口语衔接词推进节奏：没想到、就在这时、结果、也正是这时、可他不知道的是。\n'
    '- 每节末尾留一点钩子，让观众想继续看。\n\n'
    '【配音标记】在 text 中适当插入以下标记，让配音有情感起伏和顿挫（不要每句都加，只在关键处）：\n'
    '- {情绪:欢快}...{/情绪}  激动、兴奋、反转成功时\n'
    '- {情绪:悲伤}...{/情绪}  悲剧、牺牲、感人时刻\n'
    '- {情绪:严肃}...{/情绪}  重要设定、悬念、危险降临\n'
    '- {情绪:紧张}...{/情绪}  追逐、打斗、千钧一发\n'
    '- {情绪:温柔}...{/情绪}  温情、回忆、感情戏\n'
    '- {停顿:0.5}  短句后短暂停顿（0.3-1.0秒），{停顿:1.0} 长停顿用于强调\n'
    '- {慢}...{/慢}  重要信息放慢说，{快}...{/快}  紧张节奏加快\n'
    '示例：1990年一部名为《赌圣》的电影横空出世{停顿:0.8}{情绪:激动}它不仅斩获4132万港币票房{/情绪}{停顿:0.5}更让跑龙套的星仔正式加冕为星爷{停顿:1.0}\n\n'
    '【禁止】编号、引号、括号注释、markdown、代码块、任何解释性文字。\n\n'
    '只输出如下 JSON：\n'
    '{"title":"片名","hook":"开场白","beats":[{"text":"第1节解说词",'
    '"keywords":["关键词1","关键词2"],"importance":"key|advance|transition"}],"outro":"结尾"}\n\n'
    'importance：key＝主线关键情节点，advance＝一般推进，transition＝过渡铺垫。\n'
)


def _script_from_obj(obj, movie_name=''):
    """把 LLM 返回的 JSON 对象规范化成解说稿 dict；结构不对返回 None。"""
    if not isinstance(obj, dict):
        return None
    beats = []
    for it in (obj.get('beats') or []):
        if isinstance(it, dict) and it.get('text'):
            kw = it.get('keywords') or []
            if isinstance(kw, str):
                kw = [kw]
            imp = str(it.get('importance') or 'advance').lower()
            if imp not in ('key', 'advance', 'transition'):
                imp = 'advance'
            beats.append({'text': str(it['text']).strip(),
                          'keywords': [str(k) for k in kw][:6],
                          'importance': imp})
        elif isinstance(it, str) and it.strip():
            beats.append({'text': it.strip(), 'keywords': [], 'importance': 'advance'})
    if not beats:
        return None
    return {'title': str(obj.get('title') or movie_name or '').strip(),
            'hook': str(obj.get('hook') or '').strip(),
            'beats': beats,
            'outro': str(obj.get('outro') or '').strip()}


def _extract_json_obj(content):
    """从 LLM 输出里抠出第一个完整 JSON 对象（容忍前后废话与 ```json 包裹）。"""
    import json as _json
    s = content.find('{')
    if s < 0:
        return None
    depth, end = 0, -1
    for i in range(s, len(content)):
        if content[i] == '{':
            depth += 1
        elif content[i] == '}':
            depth -= 1
            if depth == 0:
                end = i
                break
    if end <= s:
        return None
    try:
        return _json.loads(content[s:end + 1])
    except Exception:
        return None


def _asr_density(asr, vdur, step=0.5):
    """台词密度表：每 step 秒一个格子，值为该格子内的台词字数。
    用于判断「哪段画面有内容」——解说驱动剪辑靠它决定跳过哪些空镜与过场。"""
    n = max(1, int(vdur / step) + 1)
    d = [0.0] * n
    for x in (asr or []):
        try:
            s0, s1 = float(x['start']), float(x['end'])
        except Exception:
            continue
        i0 = max(0, int(s0 / step))
        i1 = min(n, max(i0 + 1, int(s1 / step) + 1))
        txt = str(x.get('text') or '')
        for i in range(i0, i1):
            d[i] += len(txt) / max(1.0, (s1 - s0) / step)
    return d, step


def _build_scenes(cuts, vdur, min_len=3.0):
    """把场景切点转成场景段列表，过短的场景合并到相邻段。
    返回 [(start, end), ...]，覆盖 [0, vdur]。"""
    pts = [0.0] + [float(c) for c in (cuts or []) if 0 < float(c) < vdur] + [vdur]
    pts = sorted(set(round(p, 3) for p in pts))
    raw = [(pts[i], pts[i+1]) for i in range(len(pts)-1) if pts[i+1] - pts[i] > 0.1]
    # 合并过短场景
    merged = []
    for s, e in raw:
        if merged and (e - s < min_len or merged[-1][1] - merged[-1][0] < min_len):
            ps, pe = merged.pop()
            merged.append((ps, e))
        else:
            merged.append((s, e))
    return merged


def _vlm_sample_timeline(video_path, vdur, asr, run_dir, progress=None, interval=28.0):
    """时间轴驱动：均匀抽样建立画面索引，跳过场景检测。

    每interval秒抽1帧，±12秒内有台词才跑VLM，3帧批量调用。
    返回格式与 _vlm_describe_scenes 完全一致，下游无需改动。
    1小时视频：~120个抽样点 -> 有台词的约60个 -> 批量VLM约20次。"""
    if not vlm_enabled():
        return []
    try:
        if not vlm_ping()[0]:
            return []
    except Exception:
        return []

    # 缓存检查
    try:
        vlm_model = vlm_cfg().get('model', 'default')
    except Exception:
        vlm_model = 'default'
    cache_key = _video_cache_key(video_path, f'vlm_sample_{vlm_model}_{int(interval)}')
    cached = _cache_load(cache_key)
    if cached:
        print(f'[DIAG] VLM抽样命中缓存: {len(cached)}个时间点')
        if progress:
            progress['phase'] = '画面索引（缓存命中）'
            progress['pct'] = 46
        return cached

    frame_dir = os.path.join(run_dir, 'sample_frames')
    os.makedirs(frame_dir, exist_ok=True)
    results = []
    sys_prompt = ('你是影视场景分析助手。根据画面和提供的台词，用JSON格式结构化描述这个时间点的画面。'
                  '字段：location(地点)，characters(主要人物)，'
                  'event(正在发生什么事，一句话)，'
                  'dialogue(这段台词的核心内容，没有则留空)，'
                  'summary(画面整体概括，不超过30字)。只输出JSON。')

    # 生成抽样时间点
    sample_times = []
    t = interval / 2.0  # 从中间开始，避免片头黑屏
    while t < vdur:
        sample_times.append(t)
        t += interval

    # 预计算每个抽样点附近是否有台词（±12秒窗口）
    def _near_dialogue(ts):
        if not asr:
            return True
        for seg in asr:
            try:
                t0 = float(seg.get('start', 0))
                t1 = float(seg.get('end', t0 + 1))
                txt = str(seg.get('text', '')).strip()
                if txt and (t0 - 12 <= ts <= t1 + 12):
                    return True
            except (ValueError, TypeError):
                continue
        return False

    # 第一遍：标记哪些点需要VLM，先抽帧
    need_vlm = []  # [(idx, ts, fp)]
    skip_count = 0
    for idx, ts in enumerate(sample_times):
        if not _near_dialogue(ts):
            skip_count += 1
            results.append({'start': max(0, ts - interval/2), 'end': min(vdur, ts + interval/2),
                            'location': '', 'characters': '', 'event': '',
                            'dialogue': '', 'summary': '无台词区间'})
        else:
            fp = os.path.join(frame_dir, 'sample_%04d.jpg' % idx)
            rc, _o, _e = ffmpeg_run(['-y', '-ss', '%.3f' % ts, '-i', video_path,
                                     '-frames:v', '1', '-vf', 'scale=min(iw\\,768):-2',
                                     '-q:v', '4', '-an', fp])
            if rc == 0 and os.path.exists(fp):
                need_vlm.append((idx, ts, fp))
            else:
                results.append({'start': max(0, ts - interval/2), 'end': min(vdur, ts + interval/2),
                                'location': '', 'characters': '', 'event': '',
                                'dialogue': '', 'summary': ''})

    # 第二遍：批量VLM调用，3帧一次（VLM调用次数降2/3）
    vlm_count = 0
    batch_size = 3
    batch_prompt = ('你是影视场景分析助手。以下按时间顺序给出%d张画面帧。'
                    '请对每张帧分别用JSON描述，帧之间用---分隔。'
                    '每个JSON字段：location/characters/event/dialogue/summary。只输出JSON和---分隔符。' % batch_size)
    for bi in range(0, len(need_vlm), batch_size):
        batch = need_vlm[bi:bi + batch_size]
        if progress:
            progress['phase'] = '画面索引 批量%d/%d（VLM推理中…）' % (bi//batch_size + 1, (len(need_vlm)+batch_size-1)//batch_size)
            progress['pct'] = 44 + int(6 * bi / max(1, len(need_vlm)))
        frames = [b[2] for b in batch]
        vlm_count += 1
        try:
            if len(frames) == 1:
                resp = vlm_chat(frames[0], '请用JSON描述这个画面：location/characters/event/dialogue/summary',
                                system=sys_prompt, timeout=20)
                objs = [_extract_json_obj(resp) or {}]
            else:
                resp = vlm_chat_multi(frames, batch_prompt, system=sys_prompt, timeout=60)
                parts = resp.split('---')
                objs = []
                for part in parts[:len(frames)]:
                    objs.append(_extract_json_obj(part) or {})
                while len(objs) < len(frames):
                    objs.append({})
            for j, (idx, ts, fp) in enumerate(batch):
                obj = objs[j] if j < len(objs) else {}
                results.append({
                    'start': max(0, ts - interval/2), 'end': min(vdur, ts + interval/2),
                    'location': str(obj.get('location', '')).strip()[:50],
                    'characters': str(obj.get('characters', '')).strip()[:80],
                    'event': str(obj.get('event', '')).strip()[:100],
                    'dialogue': str(obj.get('dialogue', '')).strip()[:100],
                    'summary': str(obj.get('summary', '')).strip()[:60],
                })
        except Exception:
            for idx, ts, fp in batch:
                results.append({'start': max(0, ts - interval/2), 'end': min(vdur, ts + interval/2),
                                'location': '', 'characters': '', 'event': '',
                                'dialogue': '', 'summary': 'VLM超时跳过'})

    results.sort(key=lambda x: x['start'])
    print(f'[DIAG] VLM均匀抽样(批量): {len(sample_times)}个点, 批量VLM {vlm_count}次(原需{len(need_vlm)}次), 跳过{skip_count}个无台词')
    _cache_save(cache_key, results)
    return results


def _vlm_sample_captions(video_path, vdur, run_dir, n_samples=24, progress=None):
    """均匀采样全片 N 帧，VLM 描述画面内容，返回 [(time_sec, caption), ...]。

    用于「台词匹配 + 画面匹配」融合评分：选片段时不只看台词是否提到关键词，
    还要看画面内容是否和解说词相关。VLM 不可用时返回 []，退化为纯台词匹配。
    """
    if not vlm_enabled():
        return []
    try:
        if not vlm_ping()[0]:
            return []
    except Exception:
        return []

    frame_dir = os.path.join(run_dir, 'vlm_samples')
    os.makedirs(frame_dir, exist_ok=True)

    # 均匀采样时间点：首尾各留 2% 余量，避免黑帧
    times = [vdur * (0.02 + 0.96 * i / max(1, n_samples - 1)) for i in range(n_samples)]
    captions = []
    for idx, t in enumerate(times):
        if progress:
            progress['phase'] = '画面采样 %d/%d' % (idx + 1, n_samples)
            progress['pct'] = 44 + int(6 * idx / n_samples)
        fp = os.path.join(frame_dir, 'sample_%03d.jpg' % idx)
        rc, _o, _e = ffmpeg_run(['-y', '-ss', '%.3f' % t, '-i', video_path,
                                 '-frames:v', '1', '-vf', 'scale=min(iw\\,768):-2',
                                 '-q:v', '4', '-an', fp])
        if rc != 0 or not os.path.exists(fp):
            continue
        try:
            cap = vlm_chat(fp, '用一句话描述这个画面里的人物、场景、动作和关键物品。只描述画面，不要推测剧情。',
                           system='你是影视画面分析助手。', timeout=30)
            cap = (cap or '').strip().replace('\n', ' ')[:200]
            if cap:
                captions.append((round(t, 1), cap))
        except Exception:
            continue
    return captions


def _llm_align_beats_to_scenes(beats, scenes, movie_name=''):
    """用LLM把解说词和场景做语义对齐，返回 {beat_idx: [scene_idx, ...]}，失败返回 {}。"""
    if not beats or not scenes:
        return {}
    scene_lines = []
    for i, sc in enumerate(scenes):
        parts = []
        if sc.get('location'): parts.append('地点:' + sc['location'])
        if sc.get('characters'): parts.append('人物:' + sc['characters'])
        if sc.get('event'): parts.append('事件:' + sc['event'])
        if sc.get('dialogue'): parts.append('台词:' + sc['dialogue'])
        scene_lines.append('场景%d(%.0f-%.0fs): %s' % (i, sc.get('start', 0), sc.get('end', 0), '；'.join(parts)))
    beat_lines = ['解说词%d: %s' % (i, t[:120]) for i, t in enumerate(beats)]
    prompt = '你是影视剪辑师。根据场景描述，把每段解说词对齐到最相关的场景序号。\n'
    prompt += '输出JSON格式：{"对齐":[{"解说词":0,"场景":[1,2]},...]}\n'
    prompt += '只输出JSON，不要其他文字。\n\n'
    if movie_name:
        prompt += '电影：%s\n\n' % movie_name
    prompt += '【场景列表】\n' + '\n'.join(scene_lines[:80])
    prompt += '\n\n【解说词列表】\n' + '\n'.join(beat_lines[:60])
    # 优先本地LLM
    try:
        if local_llm_enabled() and local_llm_ping()[0]:
            resp = local_llm_chat(prompt, timeout=120)
            obj = _extract_json_obj(resp) or {}
            alignment = {}
            items = obj.get('对齐') or obj.get('alignment') or []
            if isinstance(items, list):
                for item in items:
                    bi = item.get('解说词') or item.get('beat')
                    sids = item.get('场景') or item.get('scenes')
                    if isinstance(bi, int) and isinstance(sids, list):
                        alignment[bi] = [int(x) for x in sids if isinstance(x, (int, float))]
            for bi in list(alignment.keys()):
                alignment[bi] = [s for s in alignment[bi] if 0 <= s < len(scenes)]
                if not alignment[bi]:
                    del alignment[bi]
            return alignment
    except Exception:
        pass
    # 云端兜底
    try:
        if ai_enabled('chat'):
            cfg = chat_cfg()
            import urllib.request, json as _json
            payload = {'model': cfg.get('model', 'gpt-4o-mini'), 'messages': [
                {'role': 'system', 'content': '你是影视剪辑师，输出JSON。'},
                {'role': 'user', 'content': prompt}], 'temperature': 0.3}
            req = urllib.request.Request(cfg['base_url'].rstrip('/') + '/chat/completions',
                                         data=_json.dumps(payload).encode('utf-8'),
                                         headers={'Content-Type': 'application/json',
                                                  'Authorization': 'Bearer ' + cfg.get('api_key', '')})
            with urllib.request.urlopen(req, timeout=60) as r:
                resp = (_json.loads(r.read()).get('choices') or [{}])[0].get('message', {}).get('content', '')
            obj = _extract_json_obj(resp) or {}
            alignment = {}
            items = obj.get('对齐') or obj.get('alignment') or []
            if isinstance(items, list):
                for item in items:
                    bi = item.get('解说词') or item.get('beat')
                    sids = item.get('场景') or item.get('scenes')
                    if isinstance(bi, int) and isinstance(sids, list):
                        alignment[bi] = [int(x) for x in sids if isinstance(x, (int, float))]
            for bi in list(alignment.keys()):
                alignment[bi] = [s for s in alignment[bi] if 0 <= s < len(scenes)]
                if not alignment[bi]:
                    del alignment[bi]
            return alignment
    except Exception:
        pass
    return {}


def _allocate_script_spans(texts, vdur, asr=None, cps=None, min_dur=1.0, vlm_captions=None, scene_alignment=None, scenes=None):
    """按解说词字数分配画面区间 ——「解说驱动剪辑」的核心。

    返回 (spans, narr_map)：
    - spans: 扁平的子片段列表 [(start, end), ...]，每节解说词拆成多个 ~5s 短片段
    - narr_map: narr_map[k] = 第 k 个子片段属于第几节解说词（与 texts 索引对应）

    高密度剪辑：一节解说词不再只用一个长片段代表，而是从该节时间窗口内裁出多个
    短片段拼接，避免"第一幕只用一个片段一带而过"的问题。
    """
    cps = float(cps or NAR_SCRIPT_CPS)
    n = len(texts or [])
    if n == 0 or vdur <= 0:
        return [], []
    weights = [max(1, len(str(t or ''))) for t in texts]
    durs = [w / cps for w in weights]
    need = sum(durs)
    if need > vdur:
        scale = vdur / need
        durs = [max(min_dur * 0.5, d * scale) for d in durs]
        need = sum(durs)
        if need > vdur:
            k = vdur / need
            durs = [max(0.4, d * k) for d in durs]
            need = sum(durs)

    dens, step = _asr_density(asr, vdur)
    pref = [0.0] * (len(dens) + 1)
    for i, v in enumerate(dens):
        pref[i + 1] = pref[i] + v

    def _score(t0, t1):
        i0 = max(0, int(t0 / step))
        i1 = min(len(dens), max(i0 + 1, int(t1 / step)))
        return pref[min(len(pref) - 1, i1)] - pref[min(len(pref) - 1, i0)]

    # 解说词与 ASR 文本的字符级匹配：片段内台词与解说词共享多少字
    # 解决"只看台词密度导致片段内容和字幕对不上"的问题
    def _bigrams(text):
        text = str(text or '')
        return set(text[i:i+2] for i in range(len(text)-1) if text[i:i+2].strip())

    def _text_match(beat_text, t0, t1):
        if not asr or not beat_text:
            return 0.0
        qbg = _bigrams(beat_text)
        if not qbg:
            return 0.0
        overlap = 0
        total_bg = 0
        for x in asr:
            try:
                s0, s1 = float(x['start']), float(x['end'])
            except Exception:
                continue
            if s1 <= t0 or s0 >= t1:
                continue
            txt = str(x.get('text') or '')
            bg = _bigrams(txt)
            total_bg += len(bg)
            overlap += len(qbg & bg)
        if total_bg == 0:
            return 0.0
        return overlap / total_bg

    # 解说词与 VLM 画面描述的字符级匹配：片段附近的画面内容是否和解说词相关
    def _visual_match(beat_text, t0, t1):
        if not vlm_captions or not beat_text:
            return 0.0
        beat_chars = set(beat_text)
        overlap = 0
        total = 0
        # 查找片段时间范围内及前后各5秒的 VLM 描述
        for ct, cap in vlm_captions:
            if ct < t0 - 5.0 or ct > t1 + 5.0:
                continue
            total += len(cap)
            overlap += sum(1 for c in cap if c in beat_chars)
        if total == 0:
            return 0.0
        return overlap / total

    spans = []
    narr_map = []
    last_end = 0.0

    for i, d in enumerate(durs):
        # 每节拆成多个子片段：节时长 / 目标片段时长，至少 1 个
        n_sub = max(1, int(round(d / NAR_SEG_TARGET)))
        sub_d = d / n_sub

        # 搜索窗口：优先用场景对齐结果（在匹配的场景范围内选片段），
        # 没有对齐时退回按节数均匀分布（旧行为）。
        if scene_alignment and scenes and i in scene_alignment:
            matched = scene_alignment[i]
            sc_starts = [scenes[s]['start'] for s in matched if 0 <= s < len(scenes)]
            sc_ends = [scenes[s]['end'] for s in matched if 0 <= s < len(scenes)]
            if sc_starts and sc_ends:
                lo = max(0.0, min(sc_starts))
                hi = min(vdur - sub_d, max(sc_ends))
            else:
                center = (i + 0.5) * vdur / n
                half_win = max(d * 2.5, vdur / n * 0.9)
                lo = max(0.0, center - half_win)
                hi = min(vdur - sub_d, center + half_win)
        else:
            center = (i + 0.5) * vdur / n
            half_win = max(d * 2.5, vdur / n * 0.9)
            lo = max(0.0, center - half_win)
            hi = min(vdur - sub_d, center + half_win)
        lo = max(lo, last_end)  # 不与前一节最后一个片段重叠
        if hi < lo:
            lo = max(0.0, i * vdur / n)
            hi = min(vdur - sub_d, (i + 1) * vdur / n)
            lo = max(lo, last_end)
            if hi < lo:
                hi = min(vdur - sub_d, lo + sub_d)

        # 窗口均分成 n_sub 个子区，每个子区选台词密度最高的起点
        # 这样片段在节内均匀分布，不会全挤在一处，也保证不重叠
        win_len = max(0.0, hi - lo)
        for j in range(n_sub):
            sub_lo = lo + win_len * j / n_sub
            sub_hi = lo + win_len * (j + 1) / n_sub
            sub_hi = min(sub_hi, vdur - sub_d)
            if spans:
                sub_lo = max(sub_lo, spans[-1][1])  # 不与前一个子片段重叠
            if sub_hi < sub_lo:
                sub_lo = last_end if not spans else spans[-1][1]
                sub_hi = min(vdur - sub_d, sub_lo + sub_d)

            scan_step = max(step, (sub_hi - sub_lo) / 15.0) if sub_hi > sub_lo else step
            best_t, best_s = sub_lo, -1.0
            beat_txt = str(texts[i] or '') if i < len(texts) else ''
            t = sub_lo
            while t <= sub_hi + 1e-6:
                t1 = min(vdur, t + sub_d)
                density = _score(t, t1)
                t_match = _text_match(beat_txt, t, t1)
                v_match = _visual_match(beat_txt, t, t1)
                # 融合评分：台词密度为基础，台词匹配×2 + 画面匹配×1.5
                # 画面匹配权重低于台词匹配，因为 VLM 描述可能不够精确，但能排除"台词对但画面不对"的情况
                s = density * (1.0 + t_match * 2.0 + v_match * 0.0)  # v_match权重暂设0：本地VLM描述+字符重叠是噪声，后续换语义匹配
                if s > best_s + 1e-9:
                    best_s, best_t = s, t
                t += scan_step
            s0 = min(max(0.0, best_t), max(0.0, vdur - sub_d))
            s1 = min(vdur, s0 + sub_d)
            spans.append((round(s0, 3), round(s1, 3)))
            narr_map.append(i)
            last_end = s1

    return spans, narr_map


def _fallback_full_script(movie_name, plot_text, target_sec=None):
    """离线兜底：把剧情文本按句切分并合并成 40-90 字的解说节。
    不再硬截断到 30 字——宁可保留整句，也不要「随后追」这种半截话。"""
    import re as _re
    raw = []
    for l in _re.split(r'[\n。！？!?]', plot_text or ''):
        l = l.strip()
        if len(l) <= 4:
            continue
        l = _re.sub(r'^(?:第?\d+[\.、)．:：]|\[\d+\]|（\d+）)\s*', '', l).strip()
        if len(l) > 4:
            raw.append(l)
    if not raw:
        return None
    # 短句合并到 NAR_BEAT_MIN 以上、不超过 NAR_BEAT_MAX
    merged, buf = [], ''
    for s in raw:
        if not buf:
            buf = s
        elif len(buf) + len(s) + 1 <= NAR_BEAT_MAX:
            buf = buf + '，' + s
        else:
            merged.append(buf)
            buf = s
    if buf:
        merged.append(buf)
    # 过长的单句按逗号再切一刀（避免整段 300 字撑爆一个镜头）
    final = []
    for m in merged:
        while len(m) > NAR_BEAT_MAX:
            cut = m.rfind('，', 0, NAR_BEAT_MAX)
            cut = cut if cut > NAR_BEAT_MIN // 2 else NAR_BEAT_MAX
            final.append(m[:cut].strip('，'))
            m = m[cut:].strip('，')
        if m:
            final.append(m)
    if not final:
        return None
    name = (movie_name or '').strip()
    hook = ('今天要讲的这部电影是《%s》。' % name) if name else ''
    return {'title': name,
            'hook': hook,
            'beats': [{'text': t, 'keywords': [], 'importance': 'advance'} for t in final],
            'outro': ''}


def llm_movie_full_script(movie_name, plot_text, economy=False, target_sec=None, style='movie'):
    """生成完整影视解说稿（解说驱动剪辑的起点）。

    返回 {title, hook, beats:[{text, keywords, importance}], outro}；三层兜底保证绝不空返回：
      云端/本地 LLM → 剧情切句合并 → 片名模板。
    target_sec：期望成片时长，用来估算该写多少节（节数 = 时长×语速÷每节字数）。"""
    n_beats = None
    if target_sec:
        try:
            total_chars = float(target_sec) * NAR_SCRIPT_CPS
            n_beats = max(4, min(60, int(round(total_chars / ((NAR_BEAT_MIN + NAR_BEAT_MAX) / 2.0)))))
        except Exception:
            n_beats = None
    want = ('\n\n这一版请写成约 %d 节。' % n_beats) if n_beats else ''
    # 严格约束：不添加剧情资料里没有的情节、人物、台词、数字
    strict = ('\n\n【严格约束】\n'
              '- 只使用上面【片名与剧情资料】里出现过的内容，严禁编造未提及的人物、情节、台词、数字、结局。\n'
              '- 资料里没有的细节宁可跳过，也不要自行脑补或拓展。\n'
              '- 可以调整叙述顺序和表达方式，但不能改变事实。\n'
              '- 如果资料不足，少写几节也没关系，不要凑字数。')

    # ① 本地模型（免费优先）
    if local_llm_enabled() and not economy:
        try:
            if local_llm_ping()[0]:
                brief = ((movie_name or '') + '\n' + (plot_text or ''))[:8000]
                prompt = SCRIPT_STYLE_MOVIE + want + strict + '\n\n【片名与剧情资料】\n' + brief
                obj = _extract_json_obj(local_llm_chat(prompt, timeout=180))
                sc = _script_from_obj(obj, movie_name)
                if sc and sc['beats']:
                    return sc
        except Exception:
            pass
    # ② 云端 chat
    if ai_enabled('chat') and not economy:
        try:
            import urllib.request
            import json as _json
            brief = ((movie_name or '') + '\n' + (plot_text or ''))[:8000]
            prompt = SCRIPT_STYLE_MOVIE + want + strict + '\n\n【片名与剧情资料】\n' + brief
            cfg = chat_cfg()
            payload = {'model': cfg.get('model'),
                       'messages': [{'role': 'user', 'content': prompt}],
                       'max_tokens': 3000, 'temperature': 0.75}
            url = (cfg.get('base_url', '').rstrip('/')) + '/chat/completions'
            req = urllib.request.Request(
                url, data=_json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json',
                         'Authorization': 'Bearer ' + cfg.get('api_key', '')})
            with urllib.request.urlopen(req, timeout=180) as r:
                data = _json.loads(r.read().decode('utf-8'))
            content = data['choices'][0]['message']['content']
            sc = _script_from_obj(_extract_json_obj(content), movie_name)
            if sc and sc['beats']:
                return sc
        except Exception:
            pass
    # ③ 离线兜底：剧情切句（整句保留，不再 30 字截断）
    sc = _fallback_full_script(movie_name, plot_text, target_sec)
    if sc:
        return sc
    # ④ 片名模板：保证任何情况下都有稿
    name = (movie_name or '').strip()
    tpl = [
        '故事从一场不寻常的相遇悄然展开。',
        '主角登场，命运的齿轮开始转动。',
        '平静之下暗流涌动，冲突一触即发。',
        '转折来临，局面陡然扑朔迷离。',
        '真相浮出水面，结局出人意料。',
    ]
    return {'title': name,
            'hook': ('今天要讲的这部电影是《%s》。' % name) if name else '',
            'beats': [{'text': t, 'keywords': [], 'importance': 'advance'} for t in tpl],
            'outro': ''}


def llm_movie_script(movie_name, plot_text, economy=False):
    """根据片名 + 剧情文本，让 LLM 产出结构化解说事件列表 [{desc, keywords}]。
    兜底优先级（保证离线/断网也能出稿，绝不因缺剧情而空返回）：
      剧情切句 → 本地模型(若可用) → 片名模板。"""
    import re as _re
    def _split_sentences(text):
        """把剧情文本按句拆成解说事件；去掉「1. 2.」等分幕编号前缀，避免解说词带序号。"""
        lines = []
        for l in _re.split(r'[\n。！？!?]', text or ''):
            l = l.strip()
            if len(l) <= 4:
                continue
            l = _re.sub(r'^(?:第?\d+[\.、)．:：]|\[\d+\]|（\d+）)\s*', '', l).strip()
            if len(l) <= 4:
                continue
            lines.append({'desc': l[:30], 'keywords': list(set(l))[:5]})
        return lines[:12]

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


def _narrate_by_plot(video_path, plot, params, run_dir, progress=None, movie_name='', events=None):
    """🎭 剧情驱动核心（解说驱动剪辑）：
        完整解说稿 → 按每句字数分配画面 → 返回「解说词与画面一一对应」的区间。

    返回 (segs, narr, asr, frames, mode, events)。供 narrate_movie 与 规划分析(narrate 剧情模式)复用。

    【与旧实现的根本区别】旧版是「先按画面切换分段，再把剧情句贴上去」：
      - 段少 → 剧情被丢弃（实测 3 条剧情只用 1 条）；段多 → 剧情被复制铺满
      - 每句被硬截断到 30/40 字，讲到一半就断
      - 画面段由场景检测决定，与剧情结构无关 → 成片＝原片，谈不上剪辑
    新版反过来：先一次写完完整解说稿（开场钩子 / 因果推进 / 结尾升华），
    再按每句字数算出它需要多少秒画面，从原片里挑「有内容」的区间取用。
    解说不需要的画面自然被跳过，成片因此明显短于原片 —— 这才是普遍解说的做法。"""
    def up(ph, pct):
        if progress:
            progress['phase'] = ph; progress['pct'] = pct

    # 每个长阶段前检查取消：Whisper/LLM 都不走 ffmpeg，靠协作式中断响应「⏹ 停止」
    up('探测片长', 22)
    if _aborted():
        raise AbortError('用户取消了任务')
    vdur = probe_audio_len(video_path) or 0.0
    if vdur <= 0:
        raise RuntimeError('无法分析视频时长')
    up('识别台词(本地Whisper)', 32)
    if _aborted():
        raise AbortError('用户取消了任务')
    # ASR缓存：同视频同模型不重复转写
    whisper_model = whisper_model_name()
    asr_cache_key = _video_cache_key(video_path, f'asr_{whisper_model}')
    asr = _cache_load(asr_cache_key)
    if asr:
        print(f'[DIAG] ASR命中缓存: {len(asr)}段台词')
        if progress:
            progress['phase'] = '台词识别（缓存命中）'
            progress['pct'] = 40
    else:
        asr = asr_segments(video_path, progress=progress, pct_range=(32, 42))
        if asr:
            _cache_save(asr_cache_key, asr)
    if _aborted():
        raise AbortError('用户取消了任务')

    # === 串行：先VLM建立画面索引，再写解说稿 ===
    # 注意：12GB显存无法同时加载VLM(6.5GB)+LLM(9GB)，并行会导致Ollama反复换模型，
    # 反而比串行慢2-3倍且容易卡死。TTS(网络/CPU)+裁剪(GPU编码NVENC)仍可并行，不冲突。
    up('建立画面索引（均匀抽样）', 44)
    scene_descs = _vlm_sample_timeline(video_path, vdur, asr, run_dir, progress=progress)
    if scene_descs:
        print(f'[DIAG] 画面索引: {len(scene_descs)}个时间点已建立')

    up('写完整解说稿', 42)
    target_sec = params.get('targetSec')
    try:
        target_sec = float(target_sec) if target_sec else None
    except Exception:
        target_sec = None
    # target_sec 为 0/None（自动）时按视频时长生成解说稿，覆盖全片；
    # 否则模型自由发挥节数，常只写几分钟，导致 15 分钟视频只取前几分钟画面。
    if not target_sec:
        target_sec = vdur
    # economy=False：本地模型是免费的，必须让它先写稿；云端用不用由 ai_enabled('chat') 决定。
    # （历史坑：这里曾传 economy=not ai_enabled('chat')，未配云端时 economy=True 会直接跳过
    #   本地模型走切句兜底，产出的是「剧情原文拼接」而不是解说稿——正是解说不像解说的原因。）
    script = llm_movie_full_script(movie_name, plot, economy=False, target_sec=target_sec)
    if not script or not script.get('beats'):
        raise RuntimeError('无法从剧情生成解说稿（请检查剧情文本或本地模型）')

    # hook + 各节拍 + outro 串成可直接口播的解说词序列
    texts = []
    if script.get('hook'):
        texts.append(script['hook'])
    for b in script['beats']:
        if str(b.get('text') or '').strip():
            texts.append(str(b['text']).strip())
    if script.get('outro'):
        texts.append(script['outro'])
    if not texts:
        raise RuntimeError('解说稿为空（请检查剧情文本或本地模型）')

    # TTS 标记后处理：检查标记完整性，自动补全停顿/情绪（本地LLM可能忽略标记指导）
    texts = _enhance_tts_markup(texts)
    n_marked = sum(1 for t in texts if has_tts_markup(t))
    print(f'[DIAG] TTS标记: {n_marked}/{len(texts)}节含标记')

    if _aborted():
        raise AbortError('用户取消了任务')

    # 阶段2：LLM语义对齐
    scene_alignment = {}
    if scene_descs and any(s['event'] or s['location'] for s in scene_descs):
        up('解说词-场景语义对齐', 48)
        scene_alignment = _llm_align_beats_to_scenes(texts, scene_descs, movie_name=movie_name)
        if scene_alignment:
            print(f'[DIAG] LLM语义对齐: {len(scene_alignment)}/{len(texts)}节已对齐')
    # 阶段3：在对齐的场景内选片段（_allocate_script_spans 内部处理），
    # 对齐失败时自动退回台词bigram匹配保底
    up('按解说分配画面', 52)
    segs, narr_map = _allocate_script_spans(texts, vdur, asr=asr, vlm_captions=None,
                                             scene_alignment=scene_alignment, scenes=scene_descs)
    if not segs:
        raise RuntimeError('画面分配失败（视频可能过短）')

    # events 保持旧格式返回，供 diag 与「按解说词重新匹配分镜」复用
    events = [{'desc': t[:40], 'keywords': []} for t in texts]
    return segs, texts, asr, {}, 'movie', events, narr_map


def _generate_all_tts(narr, run_dir, progress=None):
    """逐段生成所有配音，返回 [(index, clip_path), ...]。不做视频裁剪。"""
    results = []
    _tcfg = load_ai_config().get('tts') or {}
    use_mimo = bool(_tcfg.get('api_key')) and bool(_tcfg.get('model'))
    for i, txt in enumerate(narr):
        if _aborted():
            break
        if not txt.strip():
            continue
        clip = None
        if use_mimo:
            np_ = os.path.join(run_dir, 'narr%d.mp3' % i)
            if ai_tts(txt, np_):
                clip = np_
        if clip is None:
            ok, _eng, lp = local_tts_speak(txt, os.path.join(run_dir, 'narr%d.mp3' % i))
            if ok:
                clip = lp
        if clip is not None:
            results.append((i, clip))
        if progress:
            progress['phase'] = '逐段配音 %d/%d' % (i + 1, len(narr))
            progress['pct'] = 55 + int(25 * (i + 1) / max(1, len(narr)))
    print('[DIAG] TTS第一轮完成: %d/%d段' % (len(results), len(narr)))
    # 失败重试：第一轮没成功的段落再试一次（可能是瞬时网络抖动或熔断恢复）
    success_idx = set(i for i, _ in results)
    failed = [i for i in range(len(narr)) if i not in success_idx and narr[i].strip()]
    if failed:
        print('[DIAG] TTS重试 %d 个失败段落' % len(failed))
        import time as _t
        _t.sleep(1.0)  # 等熔断恢复
        for i in failed:
            if _aborted(): break
            txt = narr[i]
            clip = None
            if use_mimo:
                np_ = os.path.join(run_dir, 'narr%d.mp3' % i)
                if ai_tts(txt, np_): clip = np_
            if clip is None:
                ok, _eng, lp = local_tts_speak(txt, os.path.join(run_dir, 'narr%d.mp3' % i))
                if ok: clip = lp
            if clip is not None:
                results.append((i, clip))
            if progress:
                progress['phase'] = '配音重试 %d/%d' % (len([r for r in results if r[0] in failed]), len(failed))
    results.sort(key=lambda x: x[0])
    print('[DIAG] TTS最终完成: %d/%d段' % (len(results), len(narr)))
    return results


def compose_movie_from_tts(run_dir, progress=None, music_path=None, adjusted_items=None, skip=None):
    """Phase 2：加载已保存的配音状态，裁剪视频+合成。用户确认配音后调用。
    adjusted_items: 用户调整后的片段列表 [{index, text, audio}]，None则用原始状态
    skip: 要跳过的片段索引列表
    """
    import json as _json
    state_path = os.path.join(run_dir, 'tts_state.json')
    if not os.path.exists(state_path):
        raise RuntimeError('未找到配音状态文件，请先生成配音')
    state = _json.load(open(state_path, encoding='utf-8'))
    video_path = state['video_path']
    segs = [tuple(s) for s in state['segs']]
    narr = state['narr']
    narr_map = state.get('narr_map') or []
    params = state.get('params') or {}
    # 应用用户调整：跳过指定段，用调整后的文本/音频
    skip_set = set(skip or [])
    if adjusted_items:
        tts_results = []
        new_narr = []
        new_narr_map = []
        # 重建索引：只保留未跳过的段
        old_to_new = {}
        new_idx = 0
        for item in adjusted_items:
            old_i = item.get('index', 0)
            if old_i in skip_set:
                continue
            old_to_new[old_i] = new_idx
            new_narr.append(item.get('text', ''))
            if item.get('audio'):
                tts_results.append((new_idx, item['audio']))
            new_idx += 1
        # 收集用户调整的视频时间范围（按原始索引）
        user_video_spans = {}
        for item in adjusted_items:
            old_i = item.get('index', 0)
            if old_i in skip_set:
                continue
            vs = item.get('video_start')
            ve = item.get('video_end')
            if vs is not None and ve is not None and ve > vs:
                user_video_spans[old_i] = (float(vs), float(ve))
        # 重建segs和narr_map（只保留未跳过的段对应的画面）
        if narr_map and len(narr_map) == len(segs):
            new_segs = []
            new_narr_map = []
            for k in range(len(segs)):
                bi = narr_map[k]
                if bi in old_to_new:
                    new_segs.append(segs[k])
                    new_narr_map.append(old_to_new[bi])
            segs = new_segs
            narr_map = new_narr_map
        narr = new_narr
    else:
        tts_results = [(i, p) for i, p in state.get('tts_results', []) if i not in skip_set]
        if skip_set:
            # 跳过段后重建索引
            old_to_new = {}
            new_idx = 0
            for i in range(len(narr)):
                if i not in skip_set:
                    old_to_new[i] = new_idx
                    new_idx += 1
            new_narr = [narr[i] for i in range(len(narr)) if i not in skip_set]
            new_tts = [(old_to_new[i], p) for i, p in tts_results if i in old_to_new]
            if narr_map and len(narr_map) == len(segs):
                new_segs = []
                new_narr_map = []
                for k in range(len(segs)):
                    bi = narr_map[k]
                    if bi in old_to_new:
                        new_segs.append(segs[k])
                        new_narr_map.append(old_to_new[bi])
                segs = new_segs
                narr_map = new_narr_map
            narr = new_narr
            tts_results = new_tts
    def up(ph, pct):
        if progress:
            progress['phase'] = ph; progress['pct'] = pct

    src_video = video_path
    cut_info = {'cut_sec': 0.0, 'src_dur': round(probe_audio_len(video_path) or 0.0, 2)}
    # 先聚合：把同属一个beat的多个seg合并成一个时间范围（用原始segs+narr_map，不依赖裁剪后数量）
    if narr_map and len(narr_map) == len(segs):
        beat_ranges = []
        for bi in range(len(narr)):
            bsegs = [segs[k] for k in range(len(segs)) if narr_map[k] == bi]
            if bsegs:
                beat_ranges.append((bsegs[0][0], bsegs[-1][1]))
            else:
                beat_ranges.append((0.0, 0.0))
        segs = beat_ranges
        print('[DIAG] 高密度聚合: %d节 -> %d个片段' % (len(narr), len(segs)))
    # 应用用户手动调整的视频时间范围（必须在所有seg处理之后，用old_to_new映射覆盖）
    if adjusted_items and user_video_spans:
        video_dur = probe_audio_len(video_path) or 0
        print('[DIAG] 用户手动调整了%d段画面时间，视频时长%.1f秒，共%d段画面' % (len(user_video_spans), video_dur, len(segs)))
        for old_i, (vs, ve) in user_video_spans.items():
            new_i = old_to_new.get(old_i, old_i) if 'old_to_new' in dir() else old_i
            if 0 <= new_i < len(segs):
                if ve > vs and vs >= 0 and (video_dur == 0 or ve <= video_dur + 1):
                    segs[new_i] = (vs, min(ve, video_dur) if video_dur else ve)
                    print('[DIAG] 第%d段(原%d)画面已覆盖为: %.1f-%.1f秒' % (new_i + 1, old_i + 1, vs, ve))
                else:
                    print('[DIAG] 第%d段(原%d)画面时间不合理(%.1f-%.1f)，保留自动范围%.1f-%.1f' % (new_i + 1, old_i + 1, vs, ve, segs[new_i][0], segs[new_i][1]))
            else:
                print('[DIAG] 第%d段(原%d)索引越界(共%d段)，跳过' % (new_i + 1, old_i + 1, len(segs)))
    # 再裁剪（segs现在是每节一个时间范围，数量=解说词段数，TTS索引直接对应）
    if params.get('autoCut', True):
        up('按分镜剪辑画面', 60)
        src_video, segs, cut_sec = _cut_video_by_spans(video_path, segs, run_dir, progress)
        cut_info['cut_sec'] = cut_sec
    cut_info['out_dur'] = round(probe_audio_len(src_video) or cut_info['src_dur'], 2)
    # 计算voice_spans和tts_paths（segs[i]就是第i节解说词对应的画面范围）
    tts_paths = []
    voice_spans = {}
    for i, clip in tts_results:
        seg_span = segs[i] if i < len(segs) else (0.0, 10.0)
        tts_paths.append((clip, seg_span[0], seg_span[1]))
        v_len = probe_audio_len(clip) or max(0.5, seg_span[1] - seg_span[0])
        voice_spans[i] = (seg_span[0], min(seg_span[1], seg_span[0] + v_len + 0.35))
    print('[DIAG] 合成阶段: %d段配音, %d个画面片段' % (len(tts_paths), len(segs)))
    up('混音+烧字幕+配乐', 80)
    narr_srt = ['' if (t or '').strip() in ('（留白）', '(留白)') else _clean_caption(t) for t in narr]
    final = _compose_narration_video(src_video, segs, narr_srt, tts_paths, run_dir, params,
                                     music_path=music_path, voice_spans=voice_spans)
    if progress:
        progress['done'] = True; progress['pct'] = 100
        progress['file'] = os.path.relpath(final, OUTDIR).replace('\\', '/') if final else ''
    return final


def narrate_movie(movie_name, plot, video_path, params, run_dir, progress=None, music_path=None, tts_only=False):
    """Phase 3 主流程：联网搜索剧情 → LLM 生成解说稿 → (上传电影时)ASR+语义对齐 → 配音+字幕+配乐成片。
    未上传视频时只产出解说稿（progress['script']）。"""
    def up(ph, pct):
        if progress:
            progress['phase'] = ph; progress['pct'] = pct
    up('联网搜索剧情', 4)
    if not plot:
        hits = web_search((movie_name or '') + ' 剧情 简介 豆瓣 梗概 分幕')
        plot = '\n'.join(t for t, _, _ in hits) or ''
    if not video_path:
        # 仅解说稿：直接按剧情产出事件（不分析视频）
        events = llm_movie_script(movie_name, plot, economy=not ai_enabled('chat'))
        if not events:
            raise RuntimeError('无法生成解说稿（请检查网络，或在指令里粘贴剧情文本）')
        if progress:
            progress['done'] = True; progress['pct'] = 100
            progress['file'] = ''; progress['script'] = events
            progress['mode'] = compute_mode(params, needs_chat=True)
        return None, {'events': events, 'no_video': True}
    # 有视频：剧情驱动（解说稿 + 画面分配都在 _narrate_by_plot 内一次完成）。
    # 此处不再单独调一次 llm_movie_script——那会白白多跑一轮模型，且旧接口只产出
    # 30 字一句的碎片事件，与完整解说稿不是一回事。
    segs, narr, asr, _frames, mode, events, narr_map = _narrate_by_plot(
        video_path, plot, params, run_dir, progress, movie_name=movie_name)

    # === 两步走模式：先生成配音，等用户确认后再合成 ===
    if tts_only:
        up('逐段生成配音', 55)
        tts_results = _generate_all_tts(narr, run_dir, progress=progress)
        import json as _json
        tts_state = {
            'video_path': os.path.abspath(video_path),
            'segs': [[float(s), float(e)] for s, e in segs],
            'narr': list(narr),
            'narr_map': list(narr_map) if narr_map else [],
            'params': params,
            'tts_results': [[i, p] for i, p in tts_results],
            'movie_name': movie_name,
        }
        _json.dump(tts_state, open(os.path.join(run_dir, 'tts_state.json'), 'w', encoding='utf-8'),
                   ensure_ascii=False, indent=2)
        if progress:
            progress['done'] = True
            progress['pct'] = 100
            progress['phase'] = '配音已生成，请确认后合成'
            progress['tts_list'] = [{'index': i, 'text': narr[i] if i < len(narr) else '',
                                      'audio': os.path.relpath(p, OUTDIR).replace('\\', '/'),
                                      'duration': round(probe_audio_len(p) or 0, 1)}
                                     for i, p in tts_results]
            progress['run_dir'] = os.path.basename(run_dir)
            progress['script'] = events
        diag = {'narr': len(narr), 'tts_ok': len(tts_results), 'tts_total': len(narr),
                'awaiting_confirm': True, 'run_dir': os.path.basename(run_dir)}
        return None, diag

    # ✂ 真剪辑：只保留解说覆盖到的镜头段（此前整链路不剪切，成片恒等于原片时长）
    src_video = video_path
    cut_info = {'cut_sec': 0.0,
                'src_dur': round(probe_audio_len(video_path) or 0.0, 2),
                'out_dur': None, 'segs': len(segs)}

    # === 并行优化：TTS配音在后台线程跑，主线程同时做视频裁剪 ===
    # TTS音频生成只依赖解说文本(narr)，不依赖裁剪结果；裁剪完成后再算voice_spans
    import threading as _th_tts
    _tts_results = []  # [(index, clip_path)]
    _tts_error = [None]
    _tcfg2 = load_ai_config().get('tts') or {}
    use_mimo = bool(_tcfg2.get('api_key')) and bool(_tcfg2.get('model'))
    def _tts_worker():
        try:
            for i, txt in enumerate(narr):
                if _aborted():
                    return
                if not txt.strip():
                    continue
                clip = None
                if use_mimo:
                    np_ = os.path.join(run_dir, f'narr{i}.mp3')
                    if ai_tts(txt, np_):
                        clip = np_
                if clip is None:
                    ok, _eng, lp = local_tts_speak(txt, os.path.join(run_dir, f'narr{i}.mp3'))
                    if ok:
                        clip = lp
                if clip is not None:
                    _tts_results.append((i, clip))
                if progress:
                    progress['phase'] = f'逐段配音（并行）{i+1}/{len(narr)}'
        except Exception as e:
            _tts_error[0] = e
    _tts_thread = _th_tts.Thread(target=_tts_worker, daemon=True, name='tts-worker')
    _tts_thread.start()
    print('[DIAG] TTS配音已启动（与裁剪并行）')

    up('逐段配音+裁剪（并行）', 58)
    # 先聚合：同属一个beat的多个seg合并成一个时间范围（用原始segs，不依赖裁剪后数量）
    if narr_map and len(narr_map) == len(segs):
        beat_ranges = []
        for bi in range(len(narr)):
            bsegs = [segs[k] for k in range(len(segs)) if narr_map[k] == bi]
            if bsegs:
                beat_ranges.append((bsegs[0][0], bsegs[-1][1]))
            else:
                beat_ranges.append((0.0, 0.0))
        segs = beat_ranges
        print(f'[DIAG] 高密度聚合: {len(narr)}节 -> {len(segs)}个片段')
    # 主线程继续做裁剪（TTS在后台跑，segs现在是每节一个范围）
    if params.get('autoCut', True):
        up('按分镜剪辑画面', 54)
        src_video, segs, cut_sec = _cut_video_by_spans(video_path, segs, run_dir, progress)
        cut_info['cut_sec'] = cut_sec
        cut_info['segs'] = len(segs)
    cut_info['out_dur'] = round(probe_audio_len(src_video) or cut_info['src_dur'], 2)
    # 等待TTS线程完成
    _tts_thread.join()
    if _tts_error[0]:
        raise _tts_error[0]
    # 用裁剪后的segs计算voice_spans和tts_paths
    tts_paths = []
    voice_spans = {}
    for i, clip in _tts_results:
        seg_span = segs[i] if i < len(segs) else (0.0, 10.0)
        tts_paths.append((clip, seg_span[0], seg_span[1]))
        v_len = probe_audio_len(clip) or max(0.5, seg_span[1] - seg_span[0])
        voice_spans[i] = (seg_span[0], min(seg_span[1], seg_span[0] + v_len + 0.35))
    print(f'[DIAG] TTS并行完成: {len(tts_paths)}/{len(narr)}段配音成功')
    up('混音+烧字幕+配乐', 70)
    narr_srt = ['' if (t or '').strip() in ('（留白）', '(留白)') else _clean_caption(t) for t in narr]
    final = _compose_narration_video(src_video, segs, narr_srt, tts_paths, run_dir, params,
                                     music_path=music_path, voice_spans=voice_spans)
    if progress:
        progress['done'] = True; progress['pct'] = 100
        progress['file'] = os.path.relpath(final, OUTDIR).replace('\\', '/') if final else ''
        progress['script'] = events
        progress['mode'] = compute_mode(params, needs_chat=True)
    # 保存中间状态（用于增量重生成：改某段解说词只重生成该段）
    try:
        _state = {
            'src_video': os.path.abspath(src_video),
            'segs': [[float(s), float(e)] for s, e in segs],
            'narr': list(narr),
            'tts_paths': [[p, float(s), float(e)] for p, s, e in tts_paths],
            'voice_spans': {str(k): [float(v[0]), float(v[1])] for k, v in voice_spans.items()},
            'music_path': os.path.abspath(music_path) if music_path else None,
            'params': params,
            'final': os.path.abspath(final) if final else None,
        }
        import json as _json
        _json.dump(_state, open(os.path.join(run_dir, 'state.json'), 'w', encoding='utf-8'),
                   ensure_ascii=False, indent=2)
        print(f'[DIAG] 增量状态已保存: {len(narr)}段')
    except Exception as _e:
        print(f'[DIAG] 增量状态保存失败: {_e}')
    # 剪辑质量自检：对比每段解说词和对应时间段的台词，标记可能不匹配的片段
    quality_report = []
    try:
        for i, txt in enumerate(narr):
            if not txt.strip():
                quality_report.append({'seg': i, 'score': 1.0, 'flag': 'empty'})
                continue
            seg_span = segs[i] if i < len(segs) else (0, 0)
            # 找该时间段内的ASR台词
            seg_asr = [a.get('text', '') for a in asr if a.get('start', 0) >= seg_span[0] - 2
                       and a.get('end', 0) <= seg_span[1] + 2]
            seg_text = ' '.join(seg_asr)
            # 简单关键词重叠度：解说词中的词在台词中出现的比例
            txt_words = set(re.findall(r'[\u4e00-\u9fa5]{2,}|[a-zA-Z]{3,}', txt))
            seg_words = set(re.findall(r'[\u4e00-\u9fa5]{2,}|[a-zA-Z]{3,}', seg_text))
            if txt_words and seg_words:
                overlap = len(txt_words & seg_words) / len(txt_words)
            elif txt_words and not seg_text:
                overlap = 0.0  # 该片段无台词，纯画面
            else:
                overlap = 1.0
            flag = 'ok' if overlap >= 0.1 else ('no_dialogue' if not seg_text else 'mismatch')
            quality_report.append({'seg': i, 'score': round(overlap, 2), 'flag': flag,
                                   'narration': txt[:50], 'asr': seg_text[:50]})
        mismatch_count = sum(1 for q in quality_report if q['flag'] == 'mismatch')
        print(f'[DIAG] 质量自检: {len(quality_report)}段, {mismatch_count}段可能不匹配')
        # 保存质量报告
        import json as _jq
        _jq.dump(quality_report, open(os.path.join(run_dir, 'quality.json'), 'w', encoding='utf-8'),
                 ensure_ascii=False, indent=2)
    except Exception as _qe:
        print(f'[DIAG] 质量自检失败: {_qe}')
    diag = {'events': len(events), 'segments': len(segs), 'asr_lines': len(asr),
            'aligned': sum(1 for x in narr if x.strip()), 'voice_clips': len(tts_paths),
            'narration': narr, 'cut': cut_info,
            'quality': {'mismatch': sum(1 for q in quality_report if q['flag'] == 'mismatch'),
                        'total': len(quality_report), 'report': quality_report}}
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


def fail_task(prog, e):
    """统一的任务失败收尾：落 error 三件套 + 收集已产出的中间文件。

    prog['error']       保留原有的一句话错误（前端主展示，语义不变）
    prog['error_stage'] 出错时任务所处的阶段（prog['phase'] 的快照，先取后改）
    prog['error_detail']异常类型 + 消息 + 末尾若干帧堆栈（给前端「查看详情」用）
    各 dispatch_* 原先各写一份相同的 except body，抽出来避免字段遗漏。"""
    import traceback
    traceback.print_exc()
    stage = prog.get('phase') or '未开始'          # 必须在改 phase 之前快照
    prog['error_stage'] = stage
    try:
        prog['error_detail'] = ('%s: %s\n%s' % (
            type(e).__name__, e, traceback.format_exc(limit=8)))[:2000]
    except Exception:
        prog['error_detail'] = '%s: %s' % (type(e).__name__, e)
    prog['done'] = True
    prog['error'] = str(e)
    if prog.get('run_dir'):
        try:
            prog['partial'] = collect_partial(prog['run_dir'])
        except Exception:
            pass


# ---------------------------------------------------------------------------
# CC.BY 音乐署名（合规）
#
# 内置曲库 12 首来自 Incompetech（Kevin MacLeod，CC BY 4.0）。按协议，公开发布
# 成片必须给出 TASL 四要素：Title / Author / Source / License。以往选曲界面展示了
# 许可信息，但成片里没有任何署名——用户一发抖音/B站就踩侵权线。
# 本轮只做「机制」：生成署名文本 → 写 run_dir/credits.txt + 放进 prog['credits']，
# 由前端展示并支持一键复制。文案最终措辞待法务定稿；不进渲染管线、不加片尾卡。
# ---------------------------------------------------------------------------
def _music_catalog_entry(music_data):
    """任务参数里的 music 字段若指向内置曲库，返回曲库条目；否则返回 None（用户自带音乐不署名）。"""
    if not isinstance(music_data, dict):
        return None
    if music_data.get('source') != 'catalog':
        return None
    mid = (music_data.get('catalogId') or '').strip()
    if not mid:
        return None
    return next((t for t in MUSIC_CATALOG if t['id'] == mid), None)


def _task_credits(req):
    """按 CC.BY 4.0 的 TASL 要求生成纯文本署名；没用内置曲库音乐时返回 ''（空串=无需署名）。"""
    entries = []
    # 音乐可能挂在 req['music']（直接调用）或 req['params']['music']（指令解析层下传）
    for cand in (req.get('music'), (req.get('params') or {}).get('music')):
        t = _music_catalog_entry(cand)
        if t and t not in entries:
            entries.append(t)
    if not entries:
        return ''
    lines = ['背景音乐署名（CC BY 4.0 协议要求，公开发布本片时请保留以下信息）：']
    for t in entries:
        lic = str(t.get('license') or 'CC BY 4.0').replace('CC.BY', 'CC BY')
        lines += [
            '',
            'Title: %s' % t.get('title', ''),
            'Author: %s' % (t.get('attri') or 'Kevin MacLeod'),
            'Source: %s' % (t.get('licenseUrl') or 'https://incompetech.com/'),
            'License: %s — https://creativecommons.org/licenses/by/4.0/' % lic,
        ]
    return '\n'.join(lines)


def _finish_task_credits(req, prog):
    """任务成功收尾：写 run_dir/credits.txt 并把同一段文本放进 prog['credits']。

    没用曲库音乐时 prog['credits'] 为空串、不落文件、不塞占位文案。
    署名只是附加产物，任何失败都吞掉，绝不能因为它把已完成的成片判成失败。"""
    try:
        text = _task_credits(req)
    except Exception:
        text = ''
    prog['credits'] = text
    if not text or not prog.get('run_dir'):
        return
    try:
        with open(os.path.join(prog['run_dir'], 'credits.txt'), 'w', encoding='utf-8') as f:
            f.write('# 生成时间：%s\n' % time.strftime('%Y-%m-%d %H:%M:%S'))
            f.write(text)
            f.write('\n')
    except OSError:
        pass


def dispatch_build(req, prog):
    """通用合成（图片/视频混排 + 节拍对齐）。与 /api/build 共用。"""
    try:
        params = req.get('params', {})
        items = req.get('items', [])
        music_path = _resolve_music(req.get('music'))
        # 落盘名必须带 runid：旧实现是 up_{序号}_{idx}_ext，两个并发任务若素材结构相同
        # （比如都是 3 张图）会写到同一路径，后写的覆盖先写的，成片里混入另一个任务的素材。
        _rid = (prog or {}).get('runid') or getattr(_TLS, 'runid', None) or ('t%d' % int(time.time()))
        _rid = str(_rid).replace('\\', '_').replace('/', '_')
        work = []
        for idx, it in enumerate(items):
            if it['kind'] == 'image':
                ext = os.path.splitext(it.get('name', 'x.jpg'))[1] or '.jpg'
                fp = os.path.join(WORKDIR, f'up_{_rid}_{idx}_img{ext}')
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
                fp = os.path.join(WORKDIR, f'up_{_rid}_{idx}_vid.mp4')
                os.makedirs(WORKDIR, exist_ok=True)
                src = _resolve_upload_video(it, WORKDIR, f'up_{_rid}_{idx}_vid')
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
        fail_task(prog, e)


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
        outline = plan.get('outline') or []
        ui['segs'] = []
        for i, (s0, s1) in enumerate(plan['segs']):
            o = outline[i] if i < len(outline) else {}
            ui['segs'].append({'i': i, 'start': s0, 'end': s1,
                               'caption': plan['narr'][i] if i < len(plan['narr']) else '',
                               'thumb': rel(plan.get('thumbs', {}).get(i)),
                               'importance': o.get('importance', 'advance'),
                               'keep': o.get('keep', True)})
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
            plot = (req.get('plot') or '').strip()
            outline = []
            if plot:
                # 🎭 剧情驱动：不靠 AI 识别画面，按用户剧情剪分镜 + 写解说
                segs, narr, asr, _frames, _mode, events, _nmap = _narrate_by_plot(
                    vp, plot, params, run_dir, prog, movie_name='')
                diag = {'segments': len(segs), 'asr_lines': len(asr),
                        'narration': narr, 'plot_driven': True, 'events': len(events)}
                mode = 'movie'
                # 剧情驱动没有「主线浓缩」这一步，也就没有 _condense_segs 产出的 outline；
                # 但预览面板与渲染阶段都按 outline 取每段的保留标记，这里补一份全保留的默认值
                # （历史 bug：此分支漏赋值 outline → 组合 plan 时 UnboundLocalError）
                outline = [{'start': s0, 'end': s1, 'importance': 'advance', 'keep': True}
                           for (s0, s1) in segs]
            else:
                segs, narr, asr, diag, mode, outline = _analyze_narrate(vp, params, run_dir, prog)
            music_path = _resolve_music(req.get('music'))
            # 额外保存「未合并的细粒度候选镜头」与台词：用户改完解说词后
            # 需要按新解说重新匹配分镜（/api/narrate/align），合并后的环节粒度太粗无法重排
            shots = _narrate_candidate_shots(vp, params)
            plan = {'type': 'narrate', 'video': vp, 'segs': segs, 'narr': narr,
                    'shots': shots, 'asr': asr, 'run_dir': run_dir, 'outline': outline,
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
        fail_task(prog, e)


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
            # _render_narrate 返回 (final_path, voice_clips, cut_info)，必须解包
            final, voice_clips, cut_info = _render_narrate(
                plan['video'], segs, narr, params, prog['run_dir'], prog,
                music_path=plan.get('music'), mode=plan.get('mode'),
                auto_cut=bool(params.get('autoCut', True)))
        else:
            raise RuntimeError('未知方案类型')
        prog['done'] = True
        prog['pct'] = 100
        prog['file'] = os.path.relpath(final, OUTDIR).replace('\\', '/')
        prog['diag'] = dict(plan.get('diag') or {})
        prog['diag']['segments'] = len(tl2) - 1 if plan['type'] == 'beatcut' else len(segs)
        if plan['type'] == 'narrate':
            prog['diag']['voice_clips'] = voice_clips
            prog['diag']['cut'] = cut_info
        _record_history(req, prog, 'plan-' + plan['type'])
        PLANS.pop(src_runid, None)
    except Exception as e:
        fail_task(prog, e)


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
        fail_task(prog, e)


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
        fail_task(prog, e)


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
        fail_task(prog, e)


def dispatch_movie_tts(req, prog):
    """Phase 1：生成解说稿+所有配音，暂停等用户确认。"""
    try:
        run_dir = prog.get('run_dir') or os.path.join(OUTDIR, time.strftime('%Y%m%d-%H%M%S'))
        os.makedirs(run_dir, exist_ok=True)
        vp = _resolve_upload_video(req.get('video'), run_dir, 'src')
        if vp is None and req.get('video'):
            raise RuntimeError('未收到视频（或上传会话已过期，请重新上传）')
        _final, diag = narrate_movie(req.get('movie', ''), req.get('plot', ''), vp,
                                     req.get('params', {}), run_dir, prog,
                                     music_path=_resolve_music(req.get('music')),
                                     tts_only=True)
        prog['diag'] = diag
        _record_history(req, prog, 'movie_tts')
    except Exception as e:
        fail_task(prog, e)


def dispatch_movie_compose(req, prog):
    """Phase 2：用户确认配音后，裁剪视频+合成。"""
    try:
        run_dir_name = req.get('run_dir') or prog.get('run_dir')
        if not run_dir_name:
            raise RuntimeError('缺少run_dir参数')
        run_dir = os.path.join(OUTDIR, run_dir_name) if not os.path.isabs(run_dir_name) else run_dir_name
        if not os.path.exists(os.path.join(run_dir, 'tts_state.json')):
            raise RuntimeError('配音状态不存在，请先生成配音')
        music_path = _resolve_music(req.get('music'))
        adjusted = req.get('items')
        skip = req.get('skip') or []
        final = compose_movie_from_tts(run_dir, prog, music_path=music_path,
                                       adjusted_items=adjusted, skip=skip)
        prog['done'] = True
        prog['pct'] = 100
        if final:
            prog['file'] = os.path.relpath(final, OUTDIR).replace('\\', '/')
        _record_history(req, prog, 'movie')
    except Exception as e:
        fail_task(prog, e)


def dispatch_tts_single(req, prog):
    """单段配音重生成。"""
    try:
        text = (req.get('text') or '').strip()
        run_dir_name = req.get('run_dir') or ''
        idx = int(req.get('index', 0))
        if not text or not run_dir_name:
            prog['error'] = '缺少text或run_dir'
            prog['done'] = True
            return
        run_dir = os.path.join(OUTDIR, run_dir_name) if not os.path.isabs(run_dir_name) else run_dir_name
        os.makedirs(run_dir, exist_ok=True)
        out_path = os.path.join(run_dir, 'narr%d.mp3' % idx)
        ok, _eng, lp = local_tts_speak(text, out_path)
        if not ok:
            # 重试一次
            import time as _t; _t.sleep(0.5)
            ok, _eng, lp = local_tts_speak(text, out_path)
        if ok:
            prog['done'] = True
            prog['audio'] = os.path.relpath(lp, OUTDIR).replace('\\', '/')
            prog['duration'] = round(probe_audio_len(lp) or 0, 1)
        else:
            prog['error'] = '配音生成失败，请检查网络或TTS引擎'
            prog['done'] = True
    except Exception as e:
        fail_task(prog, e)


def dispatch_tts_regen_all(req, prog):
    """全部配音重生成。"""
    try:
        texts = req.get('texts') or []
        run_dir_name = req.get('run_dir') or ''
        if not texts or not run_dir_name:
            prog['error'] = '缺少texts或run_dir'; prog['done'] = True; return
        run_dir = os.path.join(OUTDIR, run_dir_name) if not os.path.isabs(run_dir_name) else run_dir_name
        os.makedirs(run_dir, exist_ok=True)
        results = _generate_all_tts(texts, run_dir, progress=prog)
        items = [{'index': i, 'text': texts[i] if i < len(texts) else '',
                  'audio': os.path.relpath(p, OUTDIR).replace('\\', '/'),
                  'duration': round(probe_audio_len(p) or 0, 1)}
                 for i, p in results]
        prog['done'] = True
        prog['items'] = items
    except Exception as e:
        fail_task(prog, e)


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
    # 音频：配音试听（/media/_tts_test/sample.mp3）与配音片段走 /media 时
    # 缺 MIME 会退化成 application/octet-stream，<audio> 在部分浏览器上拒绝播放
    '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.m4a': 'audio/mp4',
    '.mp4': 'video/mp4', '.srt': 'text/plain; charset=utf-8', '.webm': 'video/webm',
}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, content, ctype='text/plain; charset=utf-8', extra=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(content)))
        # 页面与静态资源禁止缓存：前端更新后浏览器必须拉最新版，
        # 否则旧缓存的 index.html/app.js 会与新后端接口错位（本机单用户场景无性能顾虑）
        self.send_header('Cache-Control', 'no-cache')
        # 禁止 MIME 嗅探：否则上传的 .html 素材被当成 text/html 在同源下执行（存储型 XSS）
        self.send_header('X-Content-Type-Options', 'nosniff')
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(content)

    def _send_file(self, full, ctype, attachment=False):
        """流式发送文件：不整份读进内存，句柄随 with 关闭。

        旧实现 `open(full,'rb').read()` 有两个后果：2GB 成片预览时内存峰值等于文件体积
        （多标签页并发即 OOM）；Windows 下句柄不释放会导致该文件无法被删除/覆盖。"""
        size = os.path.getsize(full)
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(size))
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if attachment:
            self.send_header('Content-Disposition',
                             _content_disposition(os.path.basename(full)))
        self.end_headers()
        with open(full, 'rb') as f:
            shutil.copyfileobj(f, self.wfile, 1 << 20)

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
        任务线程的 TLS，使 ffmpeg_run 能注册进程并响应「取消」。
        并发上限见 _TASK_SEM：拿不到名额直接拒绝（do_POST 会把这句原样显示给用户）。"""
        # 非阻塞取名额：宁可明确拒绝，也不让用户在看不到进度的队列里干等
        if not _TASK_SEM.acquire(blocking=False):
            raise RuntimeError('已经有 %d 个任务在跑了（同时最多 %d 个），'
                               '请等其中一个做完再提交'
                               % (_MAX_CONCURRENT_TASKS, _MAX_CONCURRENT_TASKS))
        try:
            runid = 'run-%d' % next(_RUN_CTR)
            # 目录名带时间戳：服务重启后 runid 从 1 重新计数会复用 run-N 名字，
            # 否则新任务会写进旧目录覆盖成片，历史记录（⑨记录）也随之指向错误文件
            run_dir = os.path.join(OUTDIR, '%s-%s' % (runid, time.strftime('%Y%m%d-%H%M%S')))
            os.makedirs(run_dir, exist_ok=True)
            prog = {'phase': '排队', 'pct': 0, 'done': False, 'runid': runid, 'run_dir': run_dir}
            PROGRESS[runid] = prog
            _evict_finished_progress(keep=100)

            def _runner():
                _TLS.runid = runid
                # 清掉上一次任务锁定的配音引擎：每个任务重新选，避免沿用旧音色
                try:
                    del _TLS.tts_engine
                except Exception:
                    pass
                try:
                    fn(req, prog)
                    # 成功收尾才署名：失败的视频不会流出去，也就没有署名义务
                    # （各 dispatch_* 自己吞异常，失败态只能靠 prog['error'] 判断）
                    if not prog.get('error'):
                        _finish_task_credits(req, prog)
                except AbortError:
                    prog['done'] = True
                    prog['aborted'] = True
                    prog['error'] = '已取消（用户中断）'
                except Exception as e:
                    fail_task(prog, e)
                finally:
                    _TASK_SEM.release()   # 无论成功/失败/取消都必须归还名额

            _threading.Thread(target=_runner, daemon=True).start()
        except Exception:
            _TASK_SEM.release()   # 建目录/登记失败时别把名额漏掉
            raise
        return runid

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ('/', '/index.html'):
            idx = os.path.join(STATIC_DIR, 'index.html')
            if os.path.exists(idx):
                self._send_file(idx, 'text/html; charset=utf-8')
            else:
                self._send(500, '前端文件缺失：请确保 static/ 目录存在'.encode('utf-8'), 'text/html; charset=utf-8')
            return
        if path.startswith('/static/'):
            name = path[len('/static/'):].split('?')[0]
            full = os.path.join(STATIC_DIR, os.path.basename(name))
            if os.path.isfile(full):
                ext = os.path.splitext(full)[1].lower()
                self._send_file(full, MIME.get(ext, 'application/octet-stream'))
                return
            self._send(404, b'not found')
            return
        if path.startswith('/media/'):
            name = path[len('/media/'):].split('?')[0]
            # 只服务 OUTDIR（成片/中间产物）。
            # 【安全修复】旧实现会回退到项目根 HERE —— /media/ai_config.json 可无鉴权
            # 读出明文 API Key，/media/webui_server.py 与 /media/.git/config 同样可读。
            # 实测确认可泄露，且 HOST=0.0.0.0（Docker）时局域网内任何人可拿。
            # 内置图片（img1~4.png）由后端按本地路径直接交给 ffmpeg，不经 /media/，删除回退无影响。
            full = _safe_join(OUTDIR, name)
            if full:
                ext = os.path.splitext(full)[1].lower()
                self._send_file(full, MIME.get(ext, 'application/octet-stream'))
                return
            self._send(404, b'not found')
            return
        if path.startswith('/music_lib/'):
            name = path[len('/music_lib/'):].split('?')[0]
            full = _safe_join(MUSIC_DIR, name)
            if full:
                self._send_file(full, MIME.get('.mp3', 'audio/mpeg'))
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
                # 【安全修复】素材库接受任意扩展名上传，若按 MIME 返回 text/html，
                # 上传的 .html 会在 http://localhost:8765 同源下执行 —— 后端无鉴权，
                # 脚本可直接调 /api/ai/config、/api/history/clear（存储型 XSS）。
                # 统一以附件下载方式返回，浏览器不会把它当页面渲染。
                self._send_file(full, 'application/octet-stream', attachment=True)
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
        if path == '/api/tts_reset':
            # 重置edge-tts熔断状态（网络恢复后立即重新启用）
            try:
                _EDGE_STATE.update(fails=0, dead_until=0.0, reason='')
                # 同时清除TLS引擎锁定，下次配音重新选择最优引擎
                try:
                    if hasattr(_TLS, 'tts_engine'):
                        delattr(_TLS, 'tts_engine')
                except Exception:
                    pass
                self._send(200, json.dumps({'ok': True, 'msg': '配音引擎已重置，edge-tts熔断已解除'}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/tts_recent':
            # 返回最近有tts_state.json的任务列表（用于恢复配音）
            try:
                import glob as _glob
                dirs = sorted(_glob.glob(os.path.join(OUTDIR, '*')), key=os.path.getmtime, reverse=True)
                recent = []
                for d in dirs[:20]:
                    if os.path.isdir(d) and os.path.exists(os.path.join(d, 'tts_state.json')):
                        import json as _j
                        try:
                            st = _j.load(open(os.path.join(d, 'tts_state.json'), encoding='utf-8'))
                            recent.append({
                                'run_dir': os.path.basename(d),
                                'movie': st.get('movie_name', ''),
                                'narr_count': len(st.get('narr', [])),
                                'tts_count': len(st.get('tts_results', [])),
                                'time': time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(d)))
                            })
                        except Exception:
                            pass
                self._send(200, json.dumps({'ok': True, 'list': recent}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/tts_state':
            # 返回指定run_dir的配音列表
            try:
                qs = parse_qs(urlparse(self.path).query)
                run_dir_name = (qs.get('run_dir') or [''])[0]
                if not run_dir_name:
                    self._send(200, json.dumps({'ok': False, 'error': '缺少run_dir'}).encode('utf-8'), 'application/json')
                    return
                run_dir = os.path.join(OUTDIR, run_dir_name) if not os.path.isabs(run_dir_name) else run_dir_name
                state_path = os.path.join(run_dir, 'tts_state.json')
                if not os.path.exists(state_path):
                    self._send(200, json.dumps({'ok': False, 'error': 'tts_state.json不存在'}).encode('utf-8'), 'application/json')
                    return
                import json as _j
                state = _j.load(open(state_path, encoding='utf-8'))
                tts_list = []
                # 计算每段解说词对应的视频时间范围（聚合narr_map+segs）
                segs_state = [tuple(s) for s in state.get('segs', [])]
                narr_map_state = state.get('narr_map') or []
                video_spans = {}
                n_narr = len(state.get('narr', []))
                if narr_map_state and len(narr_map_state) == len(segs_state):
                    for bi in range(n_narr):
                        bsegs = [segs_state[k] for k in range(len(segs_state)) if narr_map_state[k] == bi]
                        if bsegs:
                            video_spans[bi] = {'start': round(bsegs[0][0], 2), 'end': round(bsegs[-1][1], 2)}
                video_dur = round(probe_audio_len(state['video_path']) or 0, 1)
                # 回退：narr_map不对（全0或不匹配）时，按时长均匀分配默认位置
                _missing = [i for i in range(n_narr) if i not in video_spans]
                if _missing and video_dur > 0 and n_narr > 0:
                    _step = video_dur / n_narr
                    for i in _missing:
                        _s = round(i * _step, 2)
                        _e = round(min((i + 1) * _step, video_dur), 2)
                        video_spans[i] = {'start': _s, 'end': _e}
                    print('[DIAG] narr_map不完整，%d段默认时间按均匀分配(每段%.0f秒)' % (len(_missing), _step))
                for i, p in state.get('tts_results', []):
                    span = video_spans.get(i, {'start': 0, 'end': min(5.0, video_dur) if video_dur else 5.0})
                    tts_list.append({
                        'index': i,
                        'text': state['narr'][i] if i < len(state.get('narr', [])) else '',
                        'audio': os.path.relpath(p, OUTDIR).replace('\\', '/'),
                        'duration': round(probe_audio_len(p) or 0, 1),
                        'video_start': span['start'],
                        'video_end': span['end']
                    })
                self._send(200, json.dumps({'ok': True, 'run_dir': run_dir_name, 'tts_list': tts_list, 'video_duration': video_dur, 'video_path': os.path.basename(state['video_path'])}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/video_frame':
            # 提取视频指定时间点的帧，返回JPEG（用于手动调整时预览画面）
            try:
                qs = parse_qs(urlparse(self.path).query)
                run_dir_name = (qs.get('run_dir') or [''])[0]
                t = float((qs.get('time') or ['0'])[0])
                if not run_dir_name:
                    self._send(400, b'missing run_dir', 'text/plain')
                    return
                run_dir = os.path.join(OUTDIR, run_dir_name) if not os.path.isabs(run_dir_name) else run_dir_name
                state_path = os.path.join(run_dir, 'tts_state.json')
                if not os.path.exists(state_path):
                    self._send(404, b'state not found', 'text/plain')
                    return
                import json as _j
                state = _j.load(open(state_path, encoding='utf-8'))
                video_path = state['video_path']
                if not os.path.exists(video_path):
                    self._send(404, b'video not found', 'text/plain')
                    return
                # 用ffmpeg提取帧
                import imageio_ffmpeg as _iff
                ff = _iff.get_ffmpeg_exe()
                frame_path = os.path.join(run_dir, 'preview_%d.jpg' % int(t * 1000))
                import subprocess as _sp
                cmd = [ff, '-y', '-ss', str(max(0, t)), '-i', video_path, '-frames:v', '1', '-q:v', '3', frame_path]
                _sp.run(cmd, capture_output=True, timeout=30)
                if os.path.exists(frame_path):
                    with open(frame_path, 'rb') as f:
                        data = f.read()
                    self._send(200, data, 'image/jpeg')
                else:
                    self._send(500, b'frame extract failed', 'text/plain')
            except Exception as e:
                self._send(500, str(e).encode('utf-8'), 'text/plain')
            return
        if path == '/api/progress':
            runid = parse_qs(urlparse(self.path).query).get('run', [None])[0]
            if not runid or runid not in PROGRESS:
                self._send(404, json.dumps({'error': '未知 run'}).encode('utf-8'), 'application/json')
                return
            self._send(200, json.dumps(PROGRESS[runid]).encode('utf-8'), 'application/json')
            return
        if path == '/api/model/remove':
            # 卸载已安装的Ollama模型
            try:
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
            except Exception:
                body = {}
            model = str(body.get('model', '')).strip()
            if not model:
                self._send(400, json.dumps({'ok': False, 'error': '缺少model参数'}).encode('utf-8'), 'application/json')
                return
            try:
                import subprocess as _sp
                r = _sp.run(['ollama', 'rm', model], capture_output=True, text=True, timeout=60)
                ok = (r.returncode == 0)
                msg = (r.stdout or r.stderr or '').strip()[:200]
                self._send(200, json.dumps({'ok': ok, 'msg': msg}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(500, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/tasks':
            # 任务中心：列出所有运行中+最近完成的任务，供任务中心页面多任务展示
            tasks = []
            for rid, p in PROGRESS.items():
                if not isinstance(p, dict):
                    continue
                tasks.append({
                    'runid': rid,
                    'phase': p.get('phase', ''),
                    'pct': p.get('pct', 0),
                    'done': p.get('done', False),
                    'error': p.get('error', ''),
                    'file': p.get('file', ''),
                    'mode': p.get('mode', ''),
                    'start_time': p.get('start_time', ''),
                })
            # 运行中的排前面，然后按时间倒序
            tasks.sort(key=lambda t: (t['done'], t['runid']), reverse=False)
            self._send(200, json.dumps({'ok': True, 'tasks': tasks, 'running': sum(1 for t in tasks if not t['done'])})
                       .encode('utf-8'), 'application/json')
            return
        if path == '/api/regen_segment':
            # 增量重生成：只改某一段解说词，重生成该段TTS并重新合成，不重跑全流程
            try:
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
            except Exception:
                body = {}
            run_id = str(body.get('run_id', ''))
            seg_idx = int(body.get('seg_idx', -1))
            new_text = str(body.get('text', '')).strip()
            if not run_id or seg_idx < 0 or not new_text:
                self._send(400, json.dumps({'ok': False, 'error': '缺少run_id/seg_idx/text'}).encode('utf-8'), 'application/json')
                return
            run_dir = os.path.join(OUTDIR, run_id)
            state_path = os.path.join(run_dir, 'state.json')
            if not os.path.exists(state_path):
                self._send(404, json.dumps({'ok': False, 'error': '该任务没有保存中间状态（可能是旧版本生成的）'}).encode('utf-8'), 'application/json')
                return
            try:
                st = json.load(open(state_path, encoding='utf-8'))
            except Exception as e:
                self._send(500, json.dumps({'ok': False, 'error': f'状态读取失败: {e}'}).encode('utf-8'), 'application/json')
                return
            narr = st.get('narr', [])
            if seg_idx >= len(narr):
                self._send(400, json.dumps({'ok': False, 'error': f'段索引越界: {seg_idx}/{len(narr)}'}).encode('utf-8'), 'application/json')
                return
            # 更新解说词
            narr[seg_idx] = new_text
            st['narr'] = narr
            # 重生成该段TTS
            tts_paths = st.get('tts_paths', [])
            seg_span = st['segs'][seg_idx] if seg_idx < len(st['segs']) else [0.0, 10.0]
            _tcfg = load_ai_config().get('tts') or {}
            use_mimo = bool(_tcfg.get('api_key')) and bool(_tcfg.get('model'))
            clip = None
            if use_mimo:
                np_ = os.path.join(run_dir, f'narr{seg_idx}_regen.mp3')
                if ai_tts(new_text, np_):
                    clip = np_
            if clip is None:
                ok, _eng, lp = local_tts_speak(new_text, os.path.join(run_dir, f'narr{seg_idx}_regen.mp3'))
                if ok:
                    clip = lp
            if clip is None:
                self._send(500, json.dumps({'ok': False, 'error': 'TTS生成失败'}).encode('utf-8'), 'application/json')
                return
            # 更新tts_paths和voice_spans
            tts_paths = [list(t) for t in tts_paths]
            found = False
            for i, t in enumerate(tts_paths):
                # 按起始时间匹配同一段
                if abs(t[1] - seg_span[0]) < 0.5:
                    tts_paths[i] = [clip, float(seg_span[0]), float(seg_span[1])]
                    found = True
                    break
            if not found:
                tts_paths.append([clip, float(seg_span[0]), float(seg_span[1])])
            st['tts_paths'] = tts_paths
            v_len = probe_audio_len(clip) or max(0.5, seg_span[1] - seg_span[0])
            voice_spans = st.get('voice_spans', {})
            voice_spans[str(seg_idx)] = [float(seg_span[0]), min(float(seg_span[1]), float(seg_span[0]) + v_len + 0.35)]
            st['voice_spans'] = voice_spans
            # 重新合成
            segs = [tuple(s) for s in st['segs']]
            narr_srt = ['' if (t or '').strip() in ('（留白）', '(留白)') else t for t in narr]
            tps = [(t[0], t[1], t[2]) for t in tts_paths]
            vs = {int(k): tuple(v) for k, v in voice_spans.items()}
            music_path = st.get('music_path')
            params = st.get('params', {})
            final = _compose_narration_video(st['src_video'], segs, narr_srt, tps, run_dir, params,
                                             music_path=music_path, voice_spans=vs)
            st['final'] = os.path.abspath(final) if final else None
            json.dump(st, open(state_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
            # 更新历史记录
            if final:
                try:
                    import time as _time
                    add_history({
                        'time': _time.strftime('%Y-%m-%d %H:%M:%S'),
                        'file': os.path.relpath(final, OUTDIR).replace('\\', '/'),
                        'duration': round(probe_audio_len(final) or 0, 1),
                        'music': None, 'voice': True, 'captions': narr,
                        'mode': 'regen', 'w': 0, 'h': 0, 'fps': 0,
                    })
                except Exception:
                    pass
            self._send(200, json.dumps({'ok': True, 'file': os.path.relpath(final, OUTDIR).replace('\\', '/') if final else '',
                                        'narr': narr}).encode('utf-8'), 'application/json')
            return
        if path == '/api/history':
            items = load_history(50)
            # 逐条体检：成片文件已丢失的条目标记 missing（前端降级展示，不给下载/封面入口）；
            # 完好的条目附带 cover.jpg 封面（⑨记录里直接可预览/重生成）
            for h in items:
                rel = (h.get('file') or '').replace(chr(92), '/')
                if not rel:
                    h['missing'] = True
                    continue
                fp = os.path.join(OUTDIR, os.path.dirname(rel.replace('/', os.sep)), os.path.basename(rel))
                if not os.path.isfile(fp):
                    h['missing'] = True
                    continue
                cover = os.path.join(os.path.dirname(fp), 'cover.jpg')
                if os.path.isfile(cover):
                    h['cover'] = os.path.dirname(rel) + '/cover.jpg'
            self._send(200, json.dumps({'ok': True, 'history': items}).encode('utf-8'),
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
                           'tts_local': dict(cfg.get('tts_local') or {}),
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
        if path == '/api/hardware':
            self._send(200, json.dumps(detect_hardware()).encode('utf-8'), 'application/json')
            return
        if path == '/api/tts/voices':
            # 本地配音音色清单 + 各引擎就绪状态（供 AI 配置页渲染下拉与安装按钮）
            self._send(200, json.dumps({
                'ok': True,
                'voices': EDGE_TTS_VOICES,
                'edge_installed': edge_tts_available(),
                'edge_dead': edge_tts_dead_reason(),
                'sherpa_installed': sherpa_tts_available(),
                'sherpa_model_ready': sherpa_tts_ready(),
                'sherpa_model': sherpa_model_key(),
                'sherpa_models': [{'key': k, 'label': m['label'], 'ready': _sherpa_ready(k)}
                                  for k, m in SHERPA_TTS_MODELS.items()],
                'cosyvoice_installed': cosyvoice_available(),
                'cosyvoice_voice': _COSYVOICE['voice'],
                'cfg': tts_local_cfg(),
                'label': local_tts_label(),
                'setup': dict(TTS_SETUP),
            }).encode('utf-8'), 'application/json')
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
                                        'installed': _installed_local_models(),
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
            VLM_PULL['msg'] = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', VLM_PULL.get('msg',''))
            self._send(200, json.dumps({'ok': True, 'enabled': vlm_enabled(), 'ready': bool(ok),
                                        'message': msg, 'model': vlm_cfg()['model'],
                                        'installed': _installed_local_models(),
                                        'pulling': VLM_PULL['running'], 'pull_model': VLM_PULL['model'],
                                        'pull_ok': VLM_PULL['ok'], 'pull_msg': VLM_PULL['msg'],
                                        'pull_pct': VLM_PULL.get('pct', 0)}).encode('utf-8'),
                           'application/json')
            return
        if path == '/api/storage':
            # 存储管理：扫描项目磁盘占用（前端用 GET 请求）
            try:
                self._send(200, json.dumps(_storage_scan()).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)[:180]}).encode('utf-8'), 'application/json')
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
                if isinstance(data.get('tts_local'), dict):
                    # 本地免费配音：engine(auto|edge|sherpa|sapi) + voice + rate
                    inc = data['tts_local']
                    cur = dict(cfg.get('tts_local') or {})
                    eng = str(inc.get('engine') or '').strip().lower()
                    if eng in ('auto', 'edge', 'sherpa', 'sapi'):
                        cur['engine'] = eng
                    if inc.get('voice'):
                        cur['voice'] = str(inc['voice']).strip()
                    mk = str(inc.get('sherpa_model') or '').strip()
                    if mk in SHERPA_TTS_MODELS:
                        cur['sherpa_model'] = mk
                    rate = str(inc.get('rate') or '').strip()
                    if rate:
                        if not rate.startswith(('+', '-')):
                            rate = '+' + rate.replace('%', '')
                        if not rate.endswith('%'):
                            rate += '%'
                        cur['rate'] = rate
                    cfg['tts_local'] = cur
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
        if path == '/api/tts/install':
            # 安装本地配音引擎（pip）：edge-tts（免 Key·需联网）/ sherpa-onnx（离线推理运行时）
            try:
                length = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(length) if length else b'{}'
                data = json.loads(raw.decode('utf-8') or '{}')
                ok, msg = tts_install_async(str(data.get('pkg') or 'edge-tts'))
                self._send(200, json.dumps({'ok': ok, 'message': msg}).encode('utf-8'),
                           'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'),
                           'application/json')
            return
        if path == '/api/tts/install_chattts':
            # 安装 ChatTTS：创建 Python 3.11 venv + CUDA torch + ChatTTS（后台异步）
            try:
                ok, msg = tts_install_chattts_async()
                self._send(200, json.dumps({'ok': ok, 'message': msg}).encode('utf-8'),
                           'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'),
                           'application/json')
            return
        if path == '/api/tts/cosyvoice/install':
            # 一键安装 CosyVoice（venv+PyTorch+仓库+9GB模型）
            try:
                ok, msg = cosyvoice_install_async()
                self._send(200, json.dumps({'ok': ok, 'message': msg}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/tts/cosyvoice/voices':
            _vdir = os.path.join(HERE, 'models', 'cosyvoice', 'voices')
            _voices = []
            if os.path.isdir(_vdir):
                for _f in sorted(os.listdir(_vdir)):
                    if _f.endswith('.wav') or _f.endswith('.mp3'):
                        _name = os.path.splitext(_f)[0]
                        _voices.append({'name': _name, 'file': _f,
                                        'custom': _name not in ['中文女','中文男','英文女','英文男','粤语女','日语女']})
            self._send(200, json.dumps({'ok': True, 'voices': _voices}).encode('utf-8'), 'application/json')
            return
        if path == '/api/tts/cosyvoice/add_voice':
            try:
                length = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(length) if length else b'{}'
                data = json.loads(raw.decode('utf-8') or '{}')
                _name = (data.get('name') or '').strip()
                _audio_b64 = data.get('audio') or ''
                if not _name or not _audio_b64:
                    self._send(200, json.dumps({'ok': False, 'error': '需要音色名称和音频'}).encode('utf-8'), 'application/json')
                    return
                import base64, re
                # 放宽正则：支持 audio/m4a, audio/mp4, audio/x-m4a, audio/wav, audio/webm 等
                _audio_b64 = re.sub(r'^data:audio/[^;]+;base64,', '', _audio_b64)
                try:
                    _audio_data = base64.b64decode(_audio_b64)
                except Exception as _e:
                    self._send(200, json.dumps({'ok': False, 'error': '音频解码失败: ' + str(_e)[:100]}).encode('utf-8'), 'application/json')
                    return
                if len(_audio_data) < 1000:
                    self._send(200, json.dumps({'ok': False, 'error': '音频文件太小（<1KB），请上传3秒以上的清晰人声'}).encode('utf-8'), 'application/json')
                    return
                _vdir = os.path.join(HERE, 'models', 'cosyvoice', 'voices')
                os.makedirs(_vdir, exist_ok=True)
                # 用随机临时名避免中文/特殊字符问题
                import uuid as _uuid
                _tmp = os.path.join(_vdir, '_clone_' + _uuid.uuid4().hex[:8] + '.bin')
                _out = os.path.join(_vdir, _name + '.wav')
                with open(_tmp, 'wb') as f:
                    f.write(_audio_data)
                _ff_err = ''
                try:
                    import imageio_ffmpeg, subprocess as _sp
                    _ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
                    _r = _sp.run([_ffmpeg, '-y', '-i', _tmp, '-ar', '16000', '-ac', '1', _out],
                                 capture_output=True, timeout=120)
                    if _r.returncode != 0:
                        _ff_err = _r.stderr.decode('utf-8', errors='ignore')[-300:]
                except Exception as _e:
                    _ff_err = str(_e)[:200]
                # 清理临时文件
                if os.path.exists(_tmp):
                    try: os.unlink(_tmp)
                    except: pass
                if os.path.exists(_out) and os.path.getsize(_out) > 1000:
                    self._send(200, json.dumps({'ok': True, 'name': _name}).encode('utf-8'), 'application/json')
                else:
                    _err = '音频转换失败'
                    if _ff_err:
                        _err += ': ' + _ff_err[:150]
                    self._send(200, json.dumps({'ok': False, 'error': _err}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)[:200]}).encode('utf-8'), 'application/json')
            return
        if path == '/api/tts/model/download':
            # 下载离线中文配音模型到 models/tts/<name>/（可选 model 指定下载哪一个）
            try:
                length = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(length) if length else b'{}'
                data = json.loads(raw.decode('utf-8') or '{}')
                ok, msg = tts_model_download_async(data.get('model'))
                self._send(200, json.dumps({'ok': ok, 'message': msg}).encode('utf-8'),
                           'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'),
                           'application/json')
            return
        if path == '/api/tts/model/uninstall':
            # 卸载已下载的离线配音模型（删除 models/tts/<name>/ 目录）
            try:
                length = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(length) if length else b'{}'
                data = json.loads(raw.decode('utf-8') or '{}')
                ok, msg = tts_model_uninstall(str(data.get('model') or ''))
                self._send(200, json.dumps({'ok': ok, 'message': msg}).encode('utf-8'),
                           'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'),
                           'application/json')
            return
        if path == '/api/tts/test':
            # 试听：用当前配置朗读一句，返回可播放的音频（相对 /media 的路径）
            try:
                length = int(self.headers.get('Content-Length', 0))
                raw = self.rfile.read(length) if length else b'{}'
                data = json.loads(raw.decode('utf-8') or '{}')
                ok, msg, engine, rel = tts_test_speak(str(data.get('text') or '这是一段中文配音试听。'))
                self._send(200, json.dumps({'ok': ok, 'message': msg, 'engine': engine,
                                            'file': rel}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'),
                           'application/json')
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
            # 单分片：支持 FormData 二进制直传（新，省 base64 1.37x 膨胀+CPU 编码）和 JSON base64（旧兼容）
            try:
                ctype = self.headers.get('Content-Type', '')
                if 'multipart/form-data' in ctype:
                    boundary = ctype.split('boundary=')[-1].strip().encode()
                    length = int(self.headers.get('Content-Length', 0))
                    raw = self.rfile.read(length) if length else b''
                    fields = _parse_multipart(raw, boundary)
                    ok, err = _upload_chunk_write(fields.get('upload_id'), fields.get('idx'),
                                                  fields.get('chunk') or b'')
                else:
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
        if path == '/api/movie_tts':
            # 两步走·第一步：生成解说稿+所有配音，暂停等用户确认
            try:
                length = int(self.headers.get('Content-Length', 0))
                req = self._read_json(length, max_len=300 * 1024 * 1024)
                if req is None:
                    self._send(200, json.dumps({'ok': False, 'error': '请求过大或读取失败'}).encode('utf-8'), 'application/json')
                    return
                runid = self._spawn(dispatch_movie_tts, req)
                self._send(200, json.dumps({'ok': True, 'runid': runid}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/movie_compose':
            # 两步走·第二步：用户确认配音后，裁剪视频+合成
            try:
                length = int(self.headers.get('Content-Length', 0))
                req = self._read_json(length) if length else {}
                if req is None:
                    req = {}
                runid = self._spawn(dispatch_movie_compose, req)
                self._send(200, json.dumps({'ok': True, 'runid': runid}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/tts_single':
            # 单段配音重生成
            try:
                length = int(self.headers.get('Content-Length', 0))
                req = self._read_json(length) if length else {}
                if req is None:
                    req = {}
                runid = self._spawn(dispatch_tts_single, req)
                self._send(200, json.dumps({'ok': True, 'runid': runid}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)}).encode('utf-8'), 'application/json')
            return
        if path == '/api/tts_regen_all':
            # 全部配音重生成
            try:
                length = int(self.headers.get('Content-Length', 0))
                req = self._read_json(length) if length else {}
                if req is None:
                    req = {}
                runid = self._spawn(dispatch_tts_regen_all, req)
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
        if path == '/api/narrate/align':
            # 解说词驱动的分镜重匹配：用户改完解说词后，按新解说把候选镜头重新分配、重剪分镜
            try:
                length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(length).decode('utf-8') or '{}') if length else {}
                runid = data.get('runid')
                plan = PLANS.get(runid) if runid else None
                if not plan or plan.get('type') != 'narrate':
                    self._send(200, json.dumps({'ok': False, 'error': '方案不存在/已过期或不是解说方案，请重新分析'}).encode('utf-8'), 'application/json')
                    return
                lines = [str(x).strip() for x in (data.get('lines') or []) if str(x).strip()]
                if not lines:
                    self._send(200, json.dumps({'ok': False, 'error': '解说词为空'}).encode('utf-8'), 'application/json')
                    return
                shots = plan.get('shots') or plan.get('segs') or []
                use_model = (str(data.get('mode') or 'auto').lower() != 'algo')
                segs, src = _align_shots_to_lines(shots, lines, plan.get('asr'),
                                                 plan.get('params'), use_model=use_model)
                if not segs:
                    self._send(200, json.dumps({'ok': False, 'error': '分镜重匹配失败'}).encode('utf-8'), 'application/json')
                    return
                # 回写方案：后续 /api/confirm 直接用新分镜渲染
                plan['segs'] = segs
                plan['narr'] = lines
                plan['align_source'] = src
                try:
                    plan['thumbs'] = _plan_thumbs(plan['video'], segs,
                                                  plan.get('run_dir') or os.path.dirname(plan['video']))
                except Exception:
                    pass
                rel = lambda p: (os.path.relpath(p, OUTDIR).replace('\\', '/') if p and os.path.exists(p) else '')
                self._send(200, json.dumps({
                    'ok': True, 'source': src, 'shots': len(shots),
                    'msg': ('已按解说词语义重新匹配分镜' if src == 'model' else '模型不可用，已按解说词长度比例分配分镜'),
                    'segs': [{'i': i, 'start': round(a, 3), 'end': round(b, 3), 'caption': c,
                              'thumb': rel(plan.get('thumbs', {}).get(i))}
                             for i, ((a, b), c) in enumerate(zip(segs, lines))],
                }).encode('utf-8'), 'application/json')
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
        if path == '/api/storage':
            # 扫描项目内各类磁盘占用，分组返回（供存储管理面板展示）
            try:
                self._send(200, json.dumps(_storage_scan()).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)[:180]}).encode('utf-8'), 'application/json')
            return
        if path == '/api/storage/delete':
            # 删除单条可清理项：路径必须命中白名单且仍在项目内（防穿越/越权）
            try:
                length = int(self.headers.get('Content-Length', 0))
                data = self._read_json(length, max_len=64 * 1024) or {}
                full = _storage_resolve_deletable(data.get('path') or '')
                if not full:
                    raise RuntimeError('该路径不在可清理范围内，或尝试越权删除（已拒绝）')
                if os.path.isdir(full):
                    shutil.rmtree(full)
                else:
                    os.remove(full)
                self._send(200, json.dumps(_storage_scan()).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)[:180]}).encode('utf-8'), 'application/json')
            return
        if path == '/api/model/remove':
            # 卸载已安装的Ollama模型（POST版，前端用POST调用）
            try:
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
            except Exception:
                body = {}
            model = str(body.get('model', '')).strip()
            if not model:
                self._send(400, json.dumps({'ok': False, 'error': '缺少model参数'}).encode('utf-8'), 'application/json')
                return
            try:
                import subprocess as _sp
                r = _sp.run(['ollama', 'rm', model], capture_output=True, text=True, timeout=60)
                ok = (r.returncode == 0)
                msg = (r.stdout or r.stderr or '').strip()[:200]
                self._send(200, json.dumps({'ok': ok, 'msg': msg, 'error': '' if ok else msg}).encode('utf-8'), 'application/json')
            except Exception as e:
                self._send(200, json.dumps({'ok': False, 'error': str(e)[:200]}).encode('utf-8'), 'application/json')
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
    _fok, _fmsg = font_selfcheck()      # 启动自检：无中文字体时提前告警，别等成片全是方框
    if not _fok:
        print('  ' + _fmsg, flush=True)
    print('=' * 52, flush=True)
    if open_browser and host in ('127.0.0.1', 'localhost'):
        threading.Timer(0.7, lambda: webbrowser_open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # 退出前收掉所有仍在跑的 ffmpeg：否则 Ctrl+C / 关掉命令行窗口后，
        # 子进程变孤儿继续占满 CPU 并往已失效的 run_dir 写文件。
        _kill_all_child_processes()


def _content_disposition(filename, disposition='attachment'):
    """构造 Content-Disposition 头（RFC 6266）。

    HTTP 头按标准只能带 latin-1 字符，直接把中文文件名拼进去会抛
    `UnicodeEncodeError: 'latin-1' codec can't encode characters` ——
    异常发生在 send_header 里，整个响应变成 500，前端请求一直挂着。
    这里给 ASCII 兜底名 + filename* 传 UTF-8（浏览器按 RFC 5987 取后者）。
    顺带剔除引号/换行，避免文件名把头部结构搞坏。"""
    import urllib.parse as _up
    safe_ascii = ''.join(ch if ord(ch) < 128 else '_' for ch in (filename or ''))
    for bad in ('\\', '"', '\r', '\n', '\t'):
        safe_ascii = safe_ascii.replace(bad, '_')
    safe_ascii = safe_ascii.strip() or 'download'
    utf8_part = _up.quote(filename or '', safe='')
    return "%s; filename=\"%s\"; filename*=UTF-8''%s" % (disposition, safe_ascii, utf8_part)


def _evict_finished_progress(keep=100):
    """淘汰 PROGRESS 里的旧条目，防止长驻进程内存无限增长。

    只能淘汰**已结束**的任务。旧条件只看 `k not in RUN_PROCS`，而 RUN_PROCS 仅在
    ffmpeg 真正执行的窗口内有条目——正在跑 Whisper / LLM / 配音的任务会被误淘汰：
    前端 /api/progress 查不到（一直轮询到超时才提示），取消按钮也失效
    （/api/cancel 判定「未知 run」），任务白跑完还占着并发名额。"""
    if len(PROGRESS) <= keep:
        return
    with _PROC_LOCK:
        active = set(RUN_PROCS.keys())
    for k in list(PROGRESS.keys())[:-keep]:
        if k in active:
            continue
        if not PROGRESS[k].get('done'):
            continue          # 仍在运行，绝不淘汰
        PROGRESS.pop(k, None)


def _kill_all_child_processes():
    """终止 RUN_PROCS 中登记的全部子进程（进程退出与 /api/cancel 复用同一逻辑）。"""
    try:
        with _PROC_LOCK:
            procs = list(RUN_PROCS.values())
            RUN_PROCS.clear()
    except Exception:
        procs = []
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    for p in procs:
        try:
            p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


atexit.register(_kill_all_child_processes)


def webbrowser_open(url):
    import webbrowser
    try:
        webbrowser.open(url)
    except Exception:
        pass


if __name__ == '__main__':
    _load_progress()
    _start_progress_saver()
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    ensure_deps()
    port = int(os.environ.get('PORT', '8765'))
    start_server(port)