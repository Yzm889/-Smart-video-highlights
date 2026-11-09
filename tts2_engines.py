# -*- coding: utf-8 -*-
"""v1.4.16 新一代配音引擎适配层：Qwen3-TTS VoiceDesign + IndexTTS2。

两个引擎都跑在独立 venv 的**常驻 worker**里（1.7B / 5.5GB 模型加载几十秒，
逐句起新进程等于每句重付一次，CosyVoice 当年踩过这个坑）。

资产统一放 TTS2_DIR，**不放在 EXE 的 _internal 下** —— 桌面成品每次发版都整体
替换目录，放里面会被替换脚本迁走或直接丢失。可用 FRAMECUT_TTS2_DIR 覆盖。
"""
import atexit
import json
import os
import queue
import subprocess
import threading
import time
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
TTS2_DIR = (os.environ.get('FRAMECUT_TTS2_DIR')
            or os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'),
                            'FrameCut', 'tts2'))


def _p(*parts):
    return os.path.join(TTS2_DIR, *parts)


QWEN_PY = _p('venv', 'Scripts', 'python.exe')
QWEN_MODEL = _p('models', 'VoiceDesign')
QWEN_WORKER = os.path.join(_HERE, 'qwen3tts_worker.py')
IDX_REPO = _p('indextts2', 'repo')
IDX_CKPT = _p('indextts2', 'checkpoints_2')
IDX_PY = os.path.join(IDX_REPO, '.venv', 'Scripts', 'python.exe')
IDX_WORKER = os.path.join(_HERE, 'indextts2_worker.py')

BIN_DIR = _p('bin')


def sync_worker(src_name):
    """把 worker 脚本复制到 TTS2_DIR/bin 并返回那份副本的路径。

    【为什么不能直接用 _internal 里的那份 —— 2026-11-09 事故】
    冻结后 PyInstaller 会把打包目录塞进 PYTHONPATH，而 worker 跑的是**独立
    venv 的 3.11**；sys.path 里 `_internal` 排在标准库前面，于是
    `import _ctypes` 拣到打包进来的 3.13 `_ctypes.pyd`：
        ImportError: Module use of python313.dll conflicts with this version of Python
    表现就是「试听失败，worker 启动失败」。脚本放到 assets 目录 + 启动加 -E
    （忽略所有 PYTHON* 环境变量），就再也不碰打包目录。
    每次启动都覆盖一次：几十 KB，换来的是升级成品后 worker 一定是最新版。
    """
    src = os.path.join(_HERE, src_name)
    dst = os.path.join(BIN_DIR, src_name)
    if not os.path.isfile(src):
        # 源码态/异常态：源没了就用已有副本，总比返回一个不存在的路径好
        return dst if os.path.isfile(dst) else src
    try:
        with open(src, 'rb') as f:
            new = f.read()
        old = b''
        if os.path.isfile(dst):
            with open(dst, 'rb') as f:
                old = f.read()
        if new != old:
            os.makedirs(BIN_DIR, exist_ok=True)
            tmp = dst + '.tmp'
            with open(tmp, 'wb') as f:
                f.write(new)
            os.replace(tmp, dst)
    except Exception:
        return dst if os.path.isfile(dst) else src
    return dst

# VoiceDesign 的默认音色描述：纯文字造音色，不用录参考音
DEFAULT_INSTRUCT = ('沉稳克制的中文男声，影视剧情解说风格，语气有起伏但不夸张，'
                    '句间有自然停顿，咬字清晰，不要播报腔')

# IndexTTS2 八维情绪向量：[喜, 怒, 哀, 惧, 厌, 忧, 惊, 平]
EMO_VECTORS = {
    '紧张': [0, 0, 0, 0.7, 0, 0, 0.3, 0],
    '惊险': [0, 0, 0, 0.7, 0, 0, 0.3, 0],
    '悲伤': [0, 0, 0.8, 0, 0, 0.2, 0, 0],
    '欢快': [0.7, 0, 0, 0, 0, 0, 0.2, 0],
    '愤怒': [0, 0.8, 0, 0, 0, 0, 0.2, 0],
    '温柔': [0, 0, 0, 0, 0, 0, 0, 0.6],
    '深情': [0, 0, 0, 0, 0, 0.5, 0, 0.4],
    '平静': [0, 0, 0, 0, 0, 0, 0, 0.8],
}


# 常驻 worker 各自要占的显存（含 KV cache 余量，按实测 4438MB / ~5.5GB 上浮）
VRAM_NEED = {'qwen3tts': 5200, 'indextts2': 6600}

_NO_WIN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
_VRAM_CACHE = {'t': -99.0, 'v': (0, 0)}


def gpu_vram(max_age=3.0):
    """返回 (已用MB, 总量MB)。无 N 卡或取不到时返回 (0, 0)，调用方据此跳过守门。

    WDDM 下显存被占满不会报错，而是把权重偷偷换页到内存：GPU 占用满、
    利用率 0%，推理在 PCIe 上反复抖，看起来就是「永远合成中」。
    2026-11-09 用户试听失败的根因就是 Ollama 的 qwen3:8b 常驻吃了 9.6GB。
    """
    now = time.time()
    if now - _VRAM_CACHE['t'] < max_age:
        return _VRAM_CACHE['v']
    val = (0, 0)
    try:
        r = subprocess.run(
            ['nvidia-smi', '--query-gpu=memory.used,memory.total',
             '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=4, creationflags=_NO_WIN)
        first = (r.stdout or '').strip().splitlines()[0]
        used, total = [int(x.strip()) for x in first.split(',')[:2]]
        val = (used, total)
    except Exception:
        val = (0, 0)
    _VRAM_CACHE['t'], _VRAM_CACHE['v'] = now, val
    return val


def _http(base, path, payload=None, timeout=4):
    """只连本机 Ollama：显式禁用代理，否则 HTTP_PROXY 会把 127.0.0.1 也代走。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(base + path, data=data, headers=headers)
    with opener.open(req, timeout=timeout) as resp:
        return resp.read()


def ollama_loaded(base='http://127.0.0.1:11434'):
    """当前 Ollama 驻留在显存里的模型名列表；没装/没开就是空列表。"""
    try:
        raw = _http(base, '/api/ps', timeout=2)
        data = json.loads(raw.decode('utf-8') or '{}')
    except Exception:
        return []
    names = []
    for m in (data.get('models') or []):
        n = m.get('name') or m.get('model')
        if n:
            names.append(n)
    return names


def release_vram(want=5200, wait_sec=15):
    """开工前确认显存够用；不够就让 Ollama 把模型请出显存（keep_alive=0）。

    返回 (ok, note)。note 是给用户看的说明，成功但释放过也会带一句。
    只卸 Ollama：它是本机唯一会长时间常驻显存的「自己人」，卸了下次用到
    会自动重新加载（qwen3:8b 约 10~20s），比让配音静静算不完划算。
    """
    used, total = gpu_vram(0)
    if not total:
        return True, ''                      # 拿不到显存信息：不阻断，让引擎自己撞
    if total - used >= want:
        return True, ''
    names = ollama_loaded()
    freed = []
    for n in names:
        try:
            _http(base='http://127.0.0.1:11434', path='/api/generate',
                  payload={'model': n, 'keep_alive': 0}, timeout=6)
            freed.append(n)
        except Exception:
            pass
    if not freed:
        return False, ('显存不足：%d/%d MB 已用，配音需要约 %d MB。'
                       '请关闭占显卡的程序（Ollama、其它 AI 工具、游戏）后重试'
                       % (used, total, want))
    deadline = time.time() + wait_sec
    while True:
        used, total = gpu_vram(0)
        if total - used >= want:
            return True, '已临时腾出显存（%s 已卸载，下次用会自动重新加载）' % '、'.join(freed)
        if time.time() >= deadline:
            break
        time.sleep(0.5)
    return False, ('显存仍不足：%d/%d MB 已用，配音需要约 %d MB。'
                   '已尝试卸载 %s 但没腾够，请关掉其它占显卡的程序后重试'
                   % (used, total, want, '、'.join(freed)))


def qwen3tts_ready():
    return (os.path.isfile(QWEN_PY) and os.path.isfile(QWEN_WORKER)
            and os.path.isdir(QWEN_MODEL))


def indextts2_ready():
    return (os.path.isfile(IDX_PY) and os.path.isfile(IDX_WORKER)
            and os.path.isdir(IDX_CKPT))


class _Serve:
    """常驻 worker：启动一次，之后每行 JSON 一个请求 / 一行 JSON 一个应答。"""

    def __init__(self, name, cmd, ready_timeout=150, need_mb=5200):
        self.name = name
        self.cmd = cmd
        self.ready_timeout = ready_timeout
        self.need_mb = need_mb
        self.proc = None
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.error = ''
        self.note = ''          # 「已腾出显存」之类的软提示，成功后也带给前端

    def _reader(self, proc):
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    self.q.put(json.loads(line))
                except Exception:
                    pass        # 模型库往 stdout 打的进度噪音，跳过
        except Exception:
            pass
        finally:
            self.q.put({'_eos': True})

    def _start(self):
        # 先确认显存：12GB 卡上 Ollama 的 qwen3:8b 能吃掉 9.6GB，
        # 模型照样能加载，但会被驱动换页到内存，推理永远算不完。
        ok_vram, note = release_vram(want=self.need_mb)
        self.note = note
        if not ok_vram:
            self.error = note
            return False
        cmd = list(self.cmd)
        # 每次起进程前同步 worker（升级成品后副本要跟着变），并把脚本路径换成
        # assets 里的副本；-E 忽略 PYTHONPATH/PYTHONHOME，防止 3.11 的 worker
        # 从打包目录拣到 3.13 的扩展模块（详见 sync_worker 注释）。
        for i, part in enumerate(cmd):
            if isinstance(part, str) and part.endswith('_worker.py'):
                cmd[i] = sync_worker(os.path.basename(part))
                if '-E' not in cmd:
                    cmd.insert(1, '-E')
                break
        try:
            env = os.environ.copy()
            env['HF_ENDPOINT'] = 'https://hf-mirror.com'
            for _k in ('HTTPS_PROXY', 'HTTP_PROXY', 'https_proxy', 'http_proxy', 'ALL_PROXY'):
                env.pop(_k, None)
            # -E 之外再清一遍：万一副本同步失败退回打包目录那份，这里也能兜住
            for _k in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP'):
                env.pop(_k, None)
            # stderr 落日志：以前 DEVNULL 把 worker 的加载报错全吞了，
            # 试听卡住时完全看不到原因（这次事故的直接教训）。
            os.makedirs(TTS2_DIR, exist_ok=True)
            self._errf = open(os.path.join(TTS2_DIR, self.name + '.log'),
                              'a', encoding='utf-8')
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=self._errf,
                                 text=True, encoding='utf-8', errors='replace',
                                 bufsize=1, env=env)
        except Exception as e:
            self.error = str(e)[:300]
            return False
        threading.Thread(target=self._reader, args=(p,), daemon=True).start()
        deadline = time.time() + self.ready_timeout
        while time.time() < deadline:
            try:
                msg = self.q.get(timeout=5)
            except Exception:
                if p.poll() is not None:
                    break
                continue
            if msg.get('event') == 'ready':
                self.proc, self.error = p, ''
                return True
            if msg.get('event') == 'error' or msg.get('_eos'):
                self.error = str(msg.get('error') or 'worker 启动失败')[:300]
                break
        try:
            p.kill()
        except Exception:
            pass
        self.proc = None
        return False

    def ask(self, req, timeout=240):
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                if not self._start():
                    return False, self.error or 'worker 不可用'
            p = self.proc
            try:
                p.stdin.write(json.dumps(req, ensure_ascii=False) + '\n')
                p.stdin.flush()
            except Exception as e:
                self.proc = None
                return False, str(e)[:300]
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    msg = self.q.get(timeout=5)
                except Exception:
                    if p.poll() is not None:
                        break
                    continue
                if msg.get('_eos'):
                    self.proc = None
                    break
                if 'ok' in msg:
                    if msg.get('ok'):
                        return True, ''
                    return False, str(msg.get('error') or '引擎返回失败')[:300]
            return False, ('合成超时（%d 秒）：模型已加载但没算完，'
                           '通常是显存被别的程序抢占' % timeout)


_QWEN = None
_IDX = None

# 常驻 worker 是独立进程，主程序退出不会自动带走它（Windows 无 job 对象）：
# 不注册退出钩子的话，关掉 FrameCut 后那 4.3GB 显存会一直挂着。
def _kill_all():
    try:
        release_engines()
    except Exception:
        pass


atexit.register(_kill_all)


def _get_qwen():
    global _QWEN
    if _QWEN is None:
        # -u 关掉 stdout 块缓冲；ready_timeout 按引擎实际冷启动给
        _QWEN = _Serve('qwen3tts',
                       [QWEN_PY, '-u', QWEN_WORKER, 'serve', QWEN_MODEL],
                       ready_timeout=150, need_mb=VRAM_NEED['qwen3tts'])
    return _QWEN


def _get_idx():
    global _IDX
    if _IDX is None:
        # IndexTTS2 首次要换入 5.5GB 权重，实测约 202s，给足 360s
        _IDX = _Serve('indextts2',
                      [IDX_PY, '-u', IDX_WORKER, 'serve', IDX_REPO, IDX_CKPT],
                      ready_timeout=360, need_mb=VRAM_NEED['indextts2'])
    return _IDX


def release_engines():
    """停掉常驻 TTS worker，把显存让回给本地大模型（解说生成用）。

    12GB 卡上 Qwen3-TTS 常驻 4.4GB + qwen3:8b 要 9.6GB 装不下，
    Ollama 不会报错而是把一半层丢 CPU，解说会慢到不可用 —— 比等 45s 重载更亏。
    加锁是为了不打断正在合成的请求（等它跑完再停）。
    """
    stopped = []
    for srv in (_QWEN, _IDX):
        if srv is None:
            continue
        with srv.lock:
            p = srv.proc
            if p is None or p.poll() is not None:
                continue
            srv.proc = None
            try:
                p.stdin.close()
            except Exception:
                pass
            try:
                p.kill()
            except Exception:
                pass
            stopped.append(srv.name)
    return stopped


def _ollama_model_size(model, base='http://127.0.0.1:11434'):
    """从 /api/tags 取模型体积（字节）；取不到返回 0。"""
    try:
        data = json.loads((_http(base, '/api/tags') or b'{}').decode('utf-8') or '{}')
    except Exception:
        return 0
    want = str(model or '').strip().lower()
    for m in (data.get('models') or []):
        name = str(m.get('name') or m.get('model') or '').lower()
        if not name:
            continue
        if name == want or name.split(':')[0] == want.split(':')[0]:
            try:
                return int(m.get('size') or 0)
            except Exception:
                return 0
    return 0


_LLM_GUARD = {'t': -99.0}


def free_vram_for_llm(model=None, min_free_mb=7000):
    """解说开工前调用：显存不够就停掉配音 worker。

    需要多少按 Ollama 里该模型的体积估。磁盘体积只算权重，32K 上下文还要
    再吃 2GB 多 KV —— 实测 qwen3:8b 磁盘 5.2GB、驻留 9.6GB，按 size*1.35+2500 才准。
    模型体积未知时退化成 min_free_mb 阈值。5s 内只查一次，别给逐句调用加负担。
    """
    now = time.time()
    if now - _LLM_GUARD['t'] < 5:
        return []
    _LLM_GUARD['t'] = now
    used, total = gpu_vram(0)
    if not total:
        return []
    need = min_free_mb
    sz = _ollama_model_size(model) if model else 0
    if sz:
        need = int(sz / 1048576 * 1.35) + 2500
    if total - used >= need:
        return []
    return release_engines()


def _fill(diag, srv, engine, err):
    if diag is None:
        return
    diag['engine'] = engine
    diag['reason'] = (err or '').strip()
    diag['note'] = getattr(srv, 'note', '') or ''
    used, total = gpu_vram()
    diag['vram'] = {'used': used, 'total': total}


def qwen3tts_speak(text, out_path, instruct=None, diag=None):
    """VoiceDesign：纯文字描述造音色，不需要任何参考音频。

    diag 传一个 dict 进来可拿到失败原因（reason / note / vram）；
    以前只返回 bool，真实报错全被吞掉，前端只能说「未就绪或加载超时」。
    """
    if not (text or '').strip() or not qwen3tts_ready():
        if diag is not None:
            diag.update({'engine': 'qwen3tts', 'reason': '引擎资产不完整',
                         'note': '', 'vram': {}})
        return False
    srv = _get_qwen()
    ok, err = srv.ask({
        'text': text,
        'instruct': (instruct or '').strip() or DEFAULT_INSTRUCT,
        'out': out_path,
        'language': 'Chinese',
    })
    _fill(diag, srv, 'qwen3tts', err)
    return ok and os.path.isfile(out_path) and os.path.getsize(out_path) > 1000


def indextts2_speak(text, out_path, emo_vector=None, emo_text=None,
                    duration_factor=None, spk=None, diag=None):
    """IndexTTS2：8 维情感 + 时长控制；不传 spk 就用仓库自带示例音色。"""
    if not (text or '').strip() or not indextts2_ready():
        if diag is not None:
            diag.update({'engine': 'indextts2', 'reason': '引擎资产不完整',
                         'note': '', 'vram': {}})
        return False
    req = {'text': text, 'out': out_path}
    if emo_vector:
        req['emo_vector'] = emo_vector
    if emo_text:
        req['emo_text'] = emo_text
        req['emo_alpha'] = 0.6
    if duration_factor:
        req['duration_factor'] = duration_factor
    if spk:
        req['spk'] = spk
    srv = _get_idx()
    ok, err = srv.ask(req)
    _fill(diag, srv, 'indextts2', err)
    return ok and os.path.isfile(out_path) and os.path.getsize(out_path) > 1000


def engine_status():
    """给前端/日志看的就绪摘要。"""
    used, total = gpu_vram()
    return {'dir': TTS2_DIR,
            'qwen3tts': qwen3tts_ready(),
            'indextts2': indextts2_ready(),
            'vram': {'used': used, 'total': total,
                     'free': max(0, total - used) if total else 0},
            'ollama': ollama_loaded()}
