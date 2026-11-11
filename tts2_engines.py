# -*- coding: utf-8 -*-
"""v1.4.16 新一代配音引擎适配层：Qwen3-TTS VoiceDesign + IndexTTS2。

两个引擎都跑在独立 venv 的**常驻 worker**里（1.7B / 5.5GB 模型加载几十秒，
逐句起新进程等于每句重付一次，CosyVoice 当年踩过这个坑）。

资产统一放 TTS2_DIR，**不放在 EXE 的 _internal 下** —— 桌面成品每次发版都整体
替换目录，放里面会被替换脚本迁走或直接丢失。可用 FRAMECUT_TTS2_DIR 覆盖。
"""
import atexit
import hashlib
import json
import os
import queue
import subprocess
import sys
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


def _ollama_unload(names, base='http://127.0.0.1:11434', timeout=90):
    """把一组**已驻留**的模型从 Ollama 显存里请出去，返回发出成功请求的名字列表。

    【2026-11-11 对照实验（A/B/C 三种卸载方式 × 75s 观察）】模型驻留时
    /api/generate{keep_alive:0} 秒回、卸载干净、75s 内零弹回——「卸了又弹回」的
    真凶不是卸载 API，是真实推理探针每 60s 把模型重新灌回来（handler 的
    /api/local/status 探针，桌面 UI 每 15s 轮询触发）。这里仍做三处加固：
    ① prompt 置空 + num_predict=1 + think=False——就算碰上「ps 之后被别人抢先
       卸载」的竞态，这条请求触发的重载也是最小代价（空 prompt 不再让 qwen3
       进思考自由发挥）；② timeout 从 6s 放宽到 90s——竞态下要冷加载 40~60s，
       6s 就断开会让 keep_alive=0 没生效、模型按旧 keep_alive 继续驻留；
    ③ 等请求完成再返回（旧实现 6s 客户端断开，服务端可能没把卸载落地）。"""
    freed = []
    for n in names:
        if not str(n or '').strip():
            continue
        try:
            _http(base=base, path='/api/generate',
                  payload={'model': n, 'prompt': '', 'stream': False, 'think': False,
                           'keep_alive': 0, 'options': {'num_predict': 1}},
                  timeout=timeout)
            freed.append(n)
        except Exception:
            pass
    return freed


def ensure_tts_vram(headroom_mb=2000, wait_sec=10):
    """配音开工/进行中的显存保活：余量低于 headroom 就把 Ollama 模型请出去。

    与 release_vram 的区别：release_vram 服务于「worker 还没起，要给它腾出 want」；
    这里服务于「worker 已经常驻/正要常驻，配音阶段写稿和画面理解都用不上 Ollama」。
    2026-11-11 主人实测：qwen3:8b（默认 32K ctx 驻留 9.65GB）+ IndexTTS2（6.6GB）
    = 15.6GB > 12GB 显存 → WDDM 静默换页进内存 → 每句推理 5s 变 60~100s →
    ask 超时 → worker 反复重启（每次闪一个黑窗）。配音期间 Ollama 的模型纯属
    抢地盘的，卸了下次用到会自动重载。
    只卸 Ollama，绝不抛错/不返回失败：卸完还不够也只是慢，不值得为它中断配音。
    返回本次真正卸掉的模型名列表。"""
    used, total = gpu_vram(0)
    if not total:
        return []                            # 拿不到显存信息：不阻断，让引擎自己撞
    if total - used >= headroom_mb:
        return []                            # 余量够：什么都不做
    names = ollama_loaded()
    if not names:
        return []
    freed = _ollama_unload(names)
    if not freed:
        return []
    # Ollama 卸载是异步的，等显存真的回落（不达标也不算失败，配音继续）
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        u2, t2 = gpu_vram(0)
        if t2 and t2 - u2 >= headroom_mb:
            break
        time.sleep(0.5)
    return freed


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
    freed = _ollama_unload(names)
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


# 允许传给 worker 的环境变量白名单。别用 os.environ.copy()：打包进程里塞着
# PYTHONPATH/PYTHONHOME 等指向 _internal 的变量（外加一堆我们不了解的），
# worker 的 3.11 解释器一旦看见它们就会拣错扩展模块。
_ENV_KEEP = ('SYSTEMROOT', 'SYSTEMDRIVE', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP',
             'LOCALAPPDATA', 'APPDATA', 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH',
             'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE',
             'CUDA_PATH', 'CUDA_HOME', 'FRAMECUT_TTS2_DIR')


def child_env():
    """给独立 venv 的 worker 准备干净环境（白名单继承 + 剔除打包目录）。"""
    env = {}
    for k in _ENV_KEEP:
        v = os.environ.get(k)
        if v:
            env[k] = v
    sysroot = env.get('SYSTEMROOT') or r'C:\Windows'
    env['SYSTEMROOT'] = sysroot
    env.setdefault('WINDIR', sysroot)
    env.setdefault('TEMP', r'C:\Windows\Temp')
    env['TMP'] = env.get('TMP') or env['TEMP']
    env['HF_ENDPOINT'] = 'https://hf-mirror.com'
    meipass = (getattr(sys, '_MEIPASS', '') or '').lower().replace('/', '\\')
    dirs = []
    for d in (os.environ.get('PATH') or '').split(os.pathsep):
        if not d:
            continue
        dl = d.lower().replace('/', '\\')
        # 打包目录从 DLL 搜索路径里剔掉：里面躺着另一个版本的 python3xx.dll
        if '_internal' in dl or (meipass and dl.startswith(meipass)):
            continue
        dirs.append(d)
    for d in (os.path.dirname(QWEN_PY), os.path.dirname(os.path.dirname(QWEN_PY))):
        if d and d not in dirs:
            dirs.insert(0, d)
    env['PATH'] = os.pathsep.join(dirs)
    return env


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
        self.starting = False   # 正在拉起进程：此时不许被「让显存」误杀

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

    def _drain(self):
        """丢掉上一轮残留的应答/_eos。

        2026-11-09：worker 死过一次后，reader 线程留下的 `_eos` 会一直躺在队列里；
        下一次 _start 拉起进程后立刻读到它，于是**秒判失败并 kill 掉刚起来的 worker**
        （日志一片空白，完全看不出原因）。每次启动前必须清干净。
        """
        while True:
            try:
                self.q.get_nowait()
            except Exception:
                return

    def _log_tail(self, limit=200):
        """把 worker 日志里最后几行有用的内容抓出来（跳过 sox 那堆噪音）。"""
        try:
            with open(os.path.join(TTS2_DIR, self.name + '.log'),
                      'r', encoding='utf-8', errors='replace') as f:
                data = f.read()[-4000:]
        except Exception:
            return ''
        noise = ('sox could not be found', 'proceed here', '- - -', 'path variables',
                 "'sox'", 'if you do', 'if you do not')
        keep = []
        for ln in data.splitlines():
            s = ln.strip()
            if not s or any(n in s.lower() for n in noise):
                continue
            keep.append(s)
        return ' | '.join(keep[-3:])[-limit:]

    def _start(self):
        self.starting = True
        try:
            for attempt in (1, 2):
                t0 = time.time()
                if self._start_inner():
                    return True
                # 秒退（<6s）多半是上一次强杀留下的 CUDA 上下文/DLL 撞车，
                # 换个新进程往往就好了；显存类失败重试没意义，直接报。
                if attempt == 1 and time.time() - t0 < 6 \
                        and '显存' not in (self.error or ''):
                    time.sleep(1.0)
                    self._drain()
                    continue
                return False
            return False
        finally:
            self.starting = False

    def _start_inner(self):
        # 先确认显存：12GB 卡上 Ollama 的 qwen3:8b 能吃掉 9.6GB，
        # 模型照样能加载，但会被驱动换页到内存，推理永远算不完。
        ok_vram, note = release_vram(want=self.need_mb)
        self.note = note
        if not ok_vram:
            self.error = note
            return False
        self._drain()                       # 上一轮的 _eos 不能带进来
        self.error = ''
        cmd = list(self.cmd)
        # 每次起进程前同步 worker（升级成品后副本要跟着变），并把脚本路径换成
        # assets 里的副本；-E 忽略 PYTHONPATH/PYTHONHOME，防止 3.11 的 worker
        # 从打包目录拣到 3.13 的扩展模块（详见 sync_worker 注释）。
        for i, part in enumerate(cmd):
            if isinstance(part, str) and part.endswith('_worker.py'):
                cmd[i] = sync_worker(os.path.basename(part))
                if '-E' not in cmd:
                    # -E：忽略 PYTHON* 环境变量（打包目录会顺着 PYTHONPATH 漏进来）
                    # -X utf8：但 -E 也顺手关掉了 PYTHONUTF8，子进程 stdin 会退回
                    # GBK —— 中文 JSON 一进去就成乱码，tokenizer 报
                    # TextEncodeInput must be Union[...]。必须显式开 UTF-8 模式。
                    cmd[1:1] = ['-E', '-X', 'utf8']
                break
        self.cmd_used = cmd
        try:
            env = child_env()
            # stderr 落日志：以前 DEVNULL 把 worker 的加载报错全吞了，
            # 试听卡住时完全看不到原因（这次事故的直接教训）。
            os.makedirs(TTS2_DIR, exist_ok=True)
            self._errf = open(os.path.join(TTS2_DIR, self.name + '.log'),
                              'a', encoding='utf-8')
            si = None
            if os.name == 'nt':
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = subprocess.SW_HIDE
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=self._errf,
                                 text=True, encoding='utf-8', errors='replace',
                                 bufsize=1, env=env, startupinfo=si,
                                 creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0))
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
        # 走到这里就是没起来。把退出码和日志尾巴一并带上：
        # 以前只剩一句「worker 启动失败」，什么都查不到。
        rc = p.poll()
        tail = self._log_tail()
        detail = '退出码 %s' % (rc if rc is not None else '未知')
        if tail:
            detail += '｜' + tail
        if not self.error:
            self.error = 'worker 启动失败（%s）' % detail
        elif 'worker 启动失败' in self.error:
            self.error = '%s（%s）' % (self.error, detail)
        try:
            p.kill()
        except Exception:
            pass
        try:
            self._errf.close()      # 失败就别攥着日志句柄，反复重试会泄漏
        except Exception:
            pass
        self.proc = None
        return False

    def _reap(self, p):
        """判定这一轮结束/失败时回收进程。
        以前只写 self.proc = None，进程还活着却没人读它的管道 ——
        它算完往 stdout 写结果时抛 Errno 22，整句配音白算。
        """
        try:
            if p and p.poll() is None:
                p.kill()
        except Exception:
            pass
        try:
            if p and p.stdin:
                p.stdin.close()
        except Exception:
            pass

    def ask(self, req, timeout=240):
        with self.lock:
            if self.proc is None or self.proc.poll() is not None:
                if not self._start():
                    return False, self.error or 'worker 不可用'
            p = self.proc
            try:
                # 一律 ASCII 转义：不管子进程 stdin 是 UTF-8 还是 GBK，
                # \uXXXX 都能被 json.loads 还原成原字符，绝不出现乱码
                p.stdin.write(json.dumps(req) + '\n')
                p.stdin.flush()
            except Exception as e:
                self._reap(p)
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
                    self._reap(p)
                    self.proc = None
                    break
                if 'ok' in msg:
                    if msg.get('ok'):
                        return True, ''
                    return False, str(msg.get('error') or '引擎返回失败')[:300]
            # 循环出来的两种结局必须分开说：worker 死了 ≠ 算得慢。
            # 旧实现统一报「合成超时」，worker 早就崩了还让人等满 240s，
            # 排障时被引向「显存被抢占」这个错误方向（2026-11-11 主人实测踩中）。
            if p.poll() is not None:
                detail = '退出码 %s' % p.poll()
                tail = self._log_tail()
                if tail:
                    detail += '｜' + tail
                self._reap(p)
                self.proc = None
                return False, ('配音进程已退出（%s）。通常是显存不足被系统换页拖死'
                               '或模型崩溃——日志尾巴里是它最后的话' % detail)
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


def release_engines(skip_busy=False):
    """停掉常驻 TTS worker，把显存让回给本地大模型（解说生成用）。

    12GB 卡上 Qwen3-TTS 常驻 4.4GB + qwen3:8b 要 9.6GB 装不下，
    Ollama 不会报错而是把一半层丢 CPU，解说会慢到不可用 —— 比等 45s 重载更亏。
    加锁是为了不打断正在合成的请求（等它跑完再停）。
    【v1.4.38】skip_busy=True：正在合成的 worker 不杀——0.5s 内拿不到锁 = 有请求
    在算，跳过它。free_vram_for_llm 用这个语义：配音中途的 LLM 调用宁可自己慢
    一点（WDDM 换页），打断配音会引发句子失败→worker 重启链，比慢更贵。
    atexit（_kill_all）与 VLM 腾位走默认阻塞语义：等在算的句子跑完再停，保证停净。
    """
    stopped = []
    for srv in (_QWEN, _IDX):
        if srv is None:
            continue
        got = srv.lock.acquire(timeout=0.5) if skip_busy else srv.lock.acquire()
        if not got:
            continue            # skip_busy 且正在合成：不打断
        try:
            p = srv.proc
            if srv.starting:
                continue        # 正在拉起，杀了等于自杀（会报「worker 启动失败」）
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
        finally:
            srv.lock.release()
    return stopped


def _stop_engine(srv):
    """停掉单个常驻 worker（造完音色样本要把 Qwen3 的 5.2GB 让给 IndexTTS2）。"""
    if srv is None:
        return False
    with srv.lock:
        p = srv.proc
        if srv.starting:
            return False        # 正在拉起，杀了等于自杀
        srv.proc = None
        if p is None or p.poll() is not None:
            return False
        try:
            p.stdin.close()
        except Exception:
            pass
        try:
            p.kill()
        except Exception:
            pass
        return True


# 造音色样本用的固定台词：只用于「定人」，不进成片，所以内容无意义、只求咬字完整。
VOICE_REF_TEXT = '这是本次解说的音色样本，接下来，由我为你讲述这个故事。'
_VOICE_REF_LOCK = threading.Lock()


def voice_ref(instruct=None):
    """本次解说固定使用的参考音 wav；造不出来返回 None（调用方退回引擎自带示例音）。

    【为什么必须「只造一次」】Qwen3-TTS 的 VoiceDesign 是**按文字描述现场造音色**，
    每一条请求都重新采样一个发音人 —— 同一段 instruct、三句不同文本，实测基频中位数
    105.7 / 96.8 / 89.4 Hz（离散 16.8%）；改成批量一次生成也一样（100.4 / 91.6 /
    111.1 Hz，19.3%）；关掉采样走贪心（do_sample=False）仍然 17.5%。
    听感就是「每句换一个人」，这正是用户说的「音色、语气都割裂」。

    修法：让 Qwen3 只造**一条**音色样本并缓存，整片用 IndexTTS2 拿它当零样本参考音
    克隆。同样三句实测 F0 100.2 / 98.9 / 99.3 Hz，离散 **1.4%**，而且每句 6.7~9.5s
    （Qwen3 逐句要 11~30s）。

    缓存键带 instruct 指纹：用户改音色描述 → 自动造一条新的，不复用旧音色。
    """
    txt = str(instruct or DEFAULT_INSTRUCT).strip()
    try:
        key = hashlib.md5(txt.encode('utf-8')).hexdigest()[:10]
    except Exception:
        key = 'default'
    try:
        os.makedirs(BIN_DIR, exist_ok=True)
    except Exception:
        return None
    dst = os.path.join(BIN_DIR, 'voice_ref_%s.wav' % key)
    if os.path.isfile(dst) and os.path.getsize(dst) > 20000:
        return dst
    with _VOICE_REF_LOCK:
        if os.path.isfile(dst) and os.path.getsize(dst) > 20000:
            return dst
        if not qwen3tts_ready():
            return None
        tmp = dst + '.tmp.wav'
        if not qwen3tts_speak(VOICE_REF_TEXT, tmp, instruct=txt):
            return None
        if not os.path.isfile(tmp) or os.path.getsize(tmp) < 20000:
            return None
        try:
            os.replace(tmp, dst)
        except Exception:
            return None
        # 音色已经落盘，Qwen3 的显存在这条片子里再没用了；让给 IndexTTS2（6.6GB）。
        _stop_engine(_QWEN)
        return dst


_MODEL_SIZE_CACHE = {'t': 0.0, 'm': None, 'sz': 0}


def _ollama_model_size(model, base='http://127.0.0.1:11434'):
    """从 /api/tags 取模型体积（字节）；取不到返回 0。

    【v1.4.38】60s 进程内缓存：模型体积不会变，而 free_vram_for_llm 每次守卫都查
    （探针 60s 一次、写稿逐段调用更密），没必要每次都打一遍 HTTP。
    """
    want = str(model or '').strip().lower()
    now = time.time()
    if want == _MODEL_SIZE_CACHE['m'] and now - _MODEL_SIZE_CACHE['t'] < 60:
        return _MODEL_SIZE_CACHE['sz']
    sz = 0
    try:
        data = json.loads((_http(base, '/api/tags') or b'{}').decode('utf-8') or '{}')
        for m in (data.get('models') or []):
            name = str(m.get('name') or m.get('model') or '').lower()
            if not name:
                continue
            if name == want or name.split(':')[0] == want.split(':')[0]:
                sz = int(m.get('size') or 0)
                break
    except Exception:
        sz = 0
    _MODEL_SIZE_CACHE['t'], _MODEL_SIZE_CACHE['m'], _MODEL_SIZE_CACHE['sz'] = now, want, sz
    return sz


_LLM_GUARD = {'t': -99.0}


def free_vram_for_llm(model=None, min_free_mb=7000):
    """解说开工前调用：显存不够就停掉配音 worker。

    需要多少按 Ollama 里该模型的体积估。【v1.4.38 重标定】_LLM_NUM_CTX=16384 之后
    实测 qwen3:8b（磁盘 5.2GB）驻留约 5.5GB——旧公式 size*1.35+2500 是 32K ctx 时代
    的标定（按它算要 9.6GB），Qwen3-TTS worker 只占 4.4GB、明明装得下也被杀，白白付
    一次 worker 冷启动。按 16K 实测改 size*1.1+800（5.2GB → 约 6.5GB）。
    模型体积未知时退化成 min_free_mb 阈值。5s 内只查一次，别给逐句调用加负担。
    【v1.4.38】release_engines(skip_busy=True)：正在合成的 worker 不杀——配音中途
    的 LLM 调用宁可自己慢一点，打断配音引发的重启链比慢更贵。atexit 兜底仍阻塞全停。
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
        need = int(sz / 1048576 * 1.1) + 800
    if total - used >= need:
        return []
    return release_engines(skip_busy=True)


def _same_ollama_model(a, b):
    """判断两个 Ollama 模型名是不是同一个。

    Ollama 会给缺省 tag 补 :latest（配置写 qwen3-vl、实际叫 qwen3-vl:8b），
    所以要允许「一边没 tag」的宽松匹配；但两边都带 tag 时必须是同一个——
    否则 qwen3-vl:4b 与 qwen3-vl:8b 会被当成一个，该卸的没卸。
    """
    a = str(a or '').strip().lower()
    b = str(b or '').strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    ap, bp = a.split(':'), b.split(':')
    if len(ap) > 1 and len(bp) > 1:
        return False
    return ap[0] == bp[0]


def free_vram_for_vlm(vlm_model=None, need_mb=None, wait_sec=10):
    """画面理解开工前腾显存：停配音 worker + 把 Ollama 里**别的**模型请出去。

    【为什么不能用 free_vram_for_llm —— 2026-11-10 主人实测教训】
    free_vram_for_llm 的 need 是按「文本模型」的经验式估的：size*1.35+2500。
    拿它来为 VLM 腾位，会把 VLM 的磁盘体积也乘 1.35 再加 2500（qwen3-vl:8b 磁盘
    5.38GB → 要 10242MB 空闲），12GB 卡上**永远判不够** → 每次都白停一遍 worker，
    显存一点没腾出来，用户只觉得「改了之后更慢、还是卡」。
    而且它只会停 TTS worker，**完全不碰 Ollama**——而 VLM 阶段真正的对手恰恰是
    Ollama 里常驻的写稿模型（实测 qwen3:8b 驻留 9.12GB），不把它请出去，VLM 只剩
    约 2.6GB，七成层跑在 CPU/内存上，就是「推理到一点点就卡住」。

    与 release_vram 的区别：那个是配音前的通用腾位，会把**所有** loaded 模型都卸，
    包括正要用的 VLM 自己（卸了再重载 5.4GB，反而更慢）。这里用 keep 把它保住。

    返回本次真正释放掉的模型名列表（可能为空 = 本来就不缺）。
    """
    used, total = gpu_vram(0)
    if not total:
        return []                            # 拿不到显存信息：不阻断，让引擎自己撞
    keep = str(vlm_model or '').strip().lower()
    names = ollama_loaded()
    # VLM 已经驻留（上一段任务残留 / 刚试听过）→ 它已经成功占住自己的位置了，
    # 这时再「腾位」只会白停一遍 worker。直接放行。
    if keep:
        for n in names:
            if _same_ollama_model(n, keep):
                return []
    if need_mb is None:
        # 12GB 卡给 VLM 留 6.5GB（权重 5.4GB + 16K KV 约 1.2GB）；
        # 小卡按六成算，免得阈值比总显存还高、永远判不够。
        need_mb = 6500 if total >= 11000 else int(total * 0.6)
    if total - used >= need_mb:
        return []                            # 已经够：别乱停 worker（停一次重载很贵）
    freed = release_engines()
    _targets = [n for n in names
                if str(n or '').strip() and not (keep and _same_ollama_model(n, keep))]
    freed += _ollama_unload(_targets)        # 自己马上要用的（keep）已在 _targets 里排除
    if not freed:
        return []
    # Ollama 卸载是异步的，等显存真的回落再开始抽样（不然第一批照样撞墙）
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        u2, t2 = gpu_vram(0)
        if t2 and t2 - u2 >= need_mb:
            break
        time.sleep(0.5)
    return freed


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


# 解说默认加速档：参考音是用 VoiceDesign 造的「沉稳克制…句间有自然停顿」，
# 克隆会把这个慢节奏一起学过去（实测单句 5.3 字/秒，解说普遍 6 字/秒以上）。
# 1.12 是「听起来赶但不飘」的档位。
NARR_SPEED_DEFAULT = 1.12


def rate_to_speed(rate, default_speed=NARR_SPEED_DEFAULT):
    """把 rate（'+10%'，正数更快）换算成倍速；rate 为 0/缺省时用解说默认档。

    【为什么不用 IndexTTS2 的 duration_factor】本机这版 indextts 的 infer()
    不接受该参数，会被当成未知 model_kwargs 转给 HF generate 直接报错：
    "The following model_kwargs are not used by the model: ['duration_factor']"。
    所以语速统一在生成后用 ffmpeg atempo 做（保音高，1.12 倍听不出失真）。
    """
    pct = 0.0
    try:
        pct = float(str(rate or '').replace('%', '').replace('+', '').strip() or 0)
    except Exception:
        pct = 0.0
    speed = (1.0 + pct / 100.0) if abs(pct) > 0.5 else float(default_speed)
    return max(0.5, min(2.0, speed))


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
