# -*- coding: utf-8 -*-
"""配音模型在线下载与运行环境安装（v1.4.78 权重 / v1.4.79 环境）。

背景：tts2 权重当年靠用户数据目录里的两个一次性脚本装进来——
`tts2/dl_models.py`（Qwen3-TTS VoiceDesign + Tokenizer，modelscope）与
`tts2/indextts2/dl_ckpt.py`（IndexTTS-2，modelscope）。普通用户拿不到它们，
模型中心也没有配音模型的任何下载入口（desktop.js 里甚至留过一句
「设置页暂时无法在线补装」）。本模块收编成统一入口。

【下载源】huggingface_hub + hf-mirror（ whispers 下载已验证的同一条国内直连
通道；子进程里显式清掉代理变量——系统代理会劫持大文件下载）。权重是最大头的
部分（Qwen3 ~5.3GB / IndexTTS2 ~6.8GB），modelscope 与 HF 对这两家仓库同源。

【运行环境（v1.4.79）】Qwen3-TTS 的 venv + PyTorch 走一键安装——完全复刻
cosyvoice_install_async 已在真机验证过的路径：找 Python 3.11/3.12 → venv →
torch 2.5.1+cu121（阿里云主索引 + SJTU pytorch-wheels 镜像，官方源兜底、
CPU 版再兜底）→ 按真机实测过的版本钉死其余依赖。IndexTTS-2 的依赖面太重
（keras/tensorboard/torch 2.8，镜像覆盖不确定），保持如实指引手动安装。

【取消】下载/安装都跑在独立子进程里，取消 = 杀当前子进程；snapshot_download
自带断点续传，pip 步骤幂等（已装会秒过）。
"""
import json
import os
import subprocess
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
import tts2_engines as _T2

_NO_WIN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

# 每个 engine 的仓库清单：(HF repo_id, TTS2_DIR 相对目录, 权重就绪判据文件)
CATALOG = {
    'qwen3tts': {
        'label': 'Qwen3-TTS · VoiceDesign（含 Tokenizer）',
        'size_hint': '约 5.3 GB',
        'mb_total': 5300,
        'repos': [
            ('Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign', os.path.join('models', 'VoiceDesign')),
            ('Qwen/Qwen3-TTS-Tokenizer-12Hz', os.path.join('models', 'Tokenizer')),
        ],
        'weights_ok': lambda: (
            _dir_min_mb(os.path.join(_T2.TTS2_DIR, 'models', 'VoiceDesign'), 3000)
            and _dir_min_mb(os.path.join(_T2.TTS2_DIR, 'models', 'Tokenizer'), 200)),
    },
    'indextts2': {
        'label': 'IndexTTS-2 权重（checkpoints_2）',
        'size_hint': '约 6.8 GB',
        'mb_total': 6800,
        'repos': [
            ('IndexTeam/IndexTTS-2', os.path.join('indextts2', 'checkpoints_2')),
        ],
        'weights_ok': lambda: (
            os.path.isfile(os.path.join(_T2.TTS2_DIR, 'indextts2', 'checkpoints_2', 'config.yaml'))
            and _dir_min_mb(os.path.join(_T2.TTS2_DIR, 'indextts2', 'checkpoints_2'), 1000)),
    },
}

# 下载子进程：host Python + huggingface_hub（requirements.txt 自带）+ hf-mirror 直连。
# 走子进程而非线程：①取消 = 杀进程，干净；②host 不用为了进度卡着一个大下载。
_DL_SCRIPT = r'''
import json, os, sys
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
for _k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy",
           "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
spec = json.loads(sys.argv[1])
from huggingface_hub import snapshot_download
snapshot_download(repo_id=spec["repo"], local_dir=spec["dst"],
                  endpoint="https://hf-mirror.com", max_workers=4)
print("OK", flush=True)
'''

_STATE = {'running': False, 'engine': '', 'op': '', 'pct': 0, 'mb_done': 0,
          'mb_total': 0, 'speed_mb_s': 0.0, 'msg': '', 'ok': None,
          'cancel': False}
_LOCK = threading.Lock()
# 当前子进程（安装步骤用；下载阶段自己管 procs 列表）。cancel 立即杀它，
# 不等 2s 轮询——pip 装一半被杀不影响幂等重跑。
_CUR_PROC = {'p': None}


def _kill_current():
    p = _CUR_PROC.get('p')
    if p is not None:
        try:
            if p.poll() is None:
                p.kill()
        except OSError:
            pass


def _dir_min_mb(path, min_mb):
    """目录存在且累计体积 ≥ min_mb 才算权重已落盘（防「建了空目录就报装好」）。"""
    total = 0.0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        return False
    return total >= min_mb * 1024 * 1024


def _dir_mb(path):
    total = 0.0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total / (1024.0 * 1024.0)


def status():
    """给前端模型中心卡的实时状态（每引擎 ready/weights/env_ready + 任务态）。"""
    with _LOCK:
        st = dict(_STATE)
    st['engines'] = {}
    for eng, cfg in CATALOG.items():
        ready = {'qwen3tts': _T2.qwen3tts_ready,
                 'indextts2': _T2.indextts2_ready}.get(eng, lambda: False)
        env_ok = {'qwen3tts': qwen_env_ready,
                  'indextts2': index_env_ready}.get(eng, lambda: False)
        st['engines'][eng] = {
            'label': cfg['label'],
            'size_hint': cfg['size_hint'],
            'ready': bool(ready()),
            'weights': bool(cfg['weights_ok']()),
            'env_ready': bool(env_ok()),
            # 环境可一键安装的引擎（v1.4.79 只收编了 Qwen3-TTS 这条已验证路径）
            'env_installable': eng == 'qwen3tts',
        }
    return st


def dl_worker_main(spec_json):
    """打包版自举下载子进程入口（desktop.py __main__ 守卫转发到这里）。

    打包 EXE 里 huggingface_hub 已随包（Whisper 下载同源），直接进程内下载；
    源码版走内联脚本，两条路行为一致：下完打印 OK，失败带 stderr 退出码非 0。
    """
    try:
        os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'
        for _k in ('HTTPS_PROXY', 'HTTP_PROXY', 'https_proxy', 'http_proxy',
                   'ALL_PROXY', 'all_proxy'):
            os.environ.pop(_k, None)
        spec = json.loads(spec_json)
        from huggingface_hub import snapshot_download
        snapshot_download(repo_id=spec['repo'], local_dir=spec['dst'],
                          endpoint='https://hf-mirror.com', max_workers=4)
        print('OK', flush=True)
        return 0
    except Exception as e:   # noqa: BLE001 子进程失败要带原因退出
        print('DL_FAIL %s' % str(e)[:400], file=sys.stderr, flush=True)
        return 1


def _dl_cmd(spec):
    """下载子进程命令行。打包版 = EXE 自举（--tts2-dl-worker）；
    源码版 = 当前解释器 + 内联脚本（-I 隔离 PYTHON* 环境变量）。"""
    if getattr(sys, 'frozen', False):
        return [sys.executable, '--tts2-dl-worker', json.dumps(spec)]
    return [sys.executable, '-I', '-c', _DL_SCRIPT, json.dumps(spec)]


def cancel():
    """请求取消当前下载/安装任务（杀当前子进程；已落盘部分保留，pip 幂等）。"""
    with _LOCK:
        if _STATE['running']:
            _STATE['cancel'] = True
            _kill_current()
            return True, '已请求取消（几秒内生效，已下载的部分保留）'
        return False, '当前没有下载任务'


def download_async(engine):
    """启动后台下载。返回 (ok, msg)；同一时间只允许一个任务。"""
    if engine not in CATALOG:
        return False, '未知引擎：%s' % engine
    with _LOCK:
        if _STATE['running']:
            return False, '已有下载任务在进行中（%s）' % _STATE['engine']
        _STATE.update(running=True, engine=engine, op='download', pct=0,
                      mb_done=0, mb_total=CATALOG[engine]['mb_total'],
                      speed_mb_s=0.0, msg='正在启动下载…', ok=None, cancel=False)
    threading.Thread(target=_run_download, args=(engine,), daemon=True).start()
    return True, '开始下载 %s（%s，断点续传，可随时取消）' % (
        CATALOG[engine]['label'], CATALOG[engine]['size_hint'])


def _flatten_if_nested(dst):
    """HF 仓库若把权重放在子目录里（local_dir 会出现 dst/<子目录>/...），
    下载完自动摊平一层——本地实际布局是 config.yaml 直接在 checkpoints_2 下。"""
    marker = os.path.join(dst, 'config.yaml')
    if os.path.isfile(marker):
        return True
    try:
        for name in os.listdir(dst):
            sub = os.path.join(dst, name)
            if os.path.isdir(sub) and os.path.isfile(os.path.join(sub, 'config.yaml')):
                for root, _dirs, files in os.walk(sub):
                    rel = os.path.relpath(root, sub)
                    tgt = os.path.join(dst, rel) if rel != '.' else dst
                    os.makedirs(tgt, exist_ok=True)
                    for f in files:
                        os.replace(os.path.join(root, f), os.path.join(tgt, f))
                return os.path.isfile(marker)
    except OSError:
        pass
    return False


def _download_phase(engine):
    """权重下载阶段（v1.4.79 从 _run 拆出）：返回 (ok, msg)，不动任务终态。

    install 链会先调它再装环境；download_async 单独跑时由 _run_download 收尾。"""
    cfg = CATALOG[engine]
    procs = []
    try:
        for repo, rel in cfg['repos']:
            dst = os.path.join(_T2.TTS2_DIR, rel)
            os.makedirs(dst, exist_ok=True)
            with _LOCK:
                if _STATE['cancel']:
                    return False, '已取消（已下载的部分保留，下次继续）'
                _STATE['msg'] = '下载 %s …' % repo
            p = subprocess.Popen(
                _dl_cmd({'repo': repo, 'dst': dst}),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8', errors='replace',
                creationflags=_NO_WIN)
            procs.append((p, dst, repo))
        # 监视循环：已落盘 MB 实时更新进度；看到 cancel 就杀子进程
        t_last, mb_last = time.time(), 0.0
        while True:
            time.sleep(2.0)
            alive = [x for x in procs if x[0].poll() is None]
            mb_now = sum(_dir_mb(d) for _p, d, _r in procs)
            now = time.time()
            with _LOCK:
                _STATE['mb_done'] = int(mb_now)
                dt = max(0.1, now - t_last)
                _STATE['speed_mb_s'] = round(max(0.0, (mb_now - mb_last)) / dt, 1)
                _STATE['pct'] = min(97, int(mb_now / max(1, _STATE['mb_total']) * 100))
                _STATE['msg'] = ('已下载 %d MB / 约 %d MB（%.1f MB/s）'
                                 % (int(mb_now), _STATE['mb_total'],
                                    _STATE['speed_mb_s']))
            t_last, mb_last = now, mb_now
            with _LOCK:
                if _STATE['cancel']:
                    for p, _d, _r in procs:
                        try:
                            p.kill()
                        except OSError:
                            pass
                    return False, '已取消（已下载的部分保留，下次继续）'
            if alive:
                continue
            rc_bad = [(p, r) for p, _d, r in procs if p.poll() != 0]
            if rc_bad:
                p0 = rc_bad[0][0]
                err = ''
                try:
                    err = (p0.stderr.read() or '')[-300:]
                except Exception:
                    pass
                return False, '下载失败：%s%s' % (
                    rc_bad[0][1], ('｜' + err) if err else '')
            for _p, dst, _r in procs:
                _flatten_if_nested(dst)
            if cfg['weights_ok']():
                return True, '✅ %s 权重下载完成' % cfg['label']
            return False, ('下载结束但权重校验未通过（文件不全或仓库布局变了），'
                           '请重试一次；反复失败请查看日志')
    except Exception as e:
        return False, '下载异常：%s' % str(e)[:300]


def _run_download(engine):
    """download_async 的线程体：跑下载阶段并收任务终态。"""
    ok, msg = _download_phase(engine)
    with _LOCK:
        _STATE.update(ok=ok, running=False, pct=100 if ok else _STATE['pct'],
                      msg=msg or _STATE['msg'])


# ---------------------------------------------------------------------------
# v1.4.79 运行环境一键安装（Qwen3-TTS）——复刻 cosyvoice_install_async 真机
# 验证过的路径：找 Python 3.11/3.12 → venv → torch cu121（阿里云+SJTU 镜像、
# 官方源与 CPU 版兜底）→ 按主人 venv 实测版本钉死其余依赖 → 导入自检。
# IndexTTS-2 依赖面太重（keras/tensorboard/torch 2.8），不进本轮，保持手动指引。
# ---------------------------------------------------------------------------

def qwen_env_ready():
    """Qwen3-TTS venv 环境就绪判据（纯文件检查，可被前端 2s 轮询）：
    venv 解释器 + site-packages 里同时有 torch 和 qwen_tts。"""
    sp = os.path.join(_T2.TTS2_DIR, 'venv', 'Lib', 'site-packages')
    return (os.path.isfile(_T2.QWEN_PY)
            and os.path.isdir(os.path.join(sp, 'torch'))
            and os.path.isdir(os.path.join(sp, 'qwen_tts')))


def index_env_ready():
    """IndexTTS-2 运行环境就绪判据：它自带 venv（IDX_PY）+ 依赖装好
    （site-packages 里有 torch）。环境不收编一键装，但状态要如实显示——
    本机装好了就画「已就绪」，不许一律标「未就绪」。"""
    sp = os.path.join(_T2.IDX_REPO, '.venv', 'Lib', 'site-packages')
    return (os.path.isfile(_T2.IDX_PY)
            and os.path.isdir(os.path.join(sp, 'torch')))


def _run_step(cmd, pct0, pct1, est_secs, label, timeout):
    """跑一个安装子步骤：进度按耗时在 [pct0, pct1) 内爬行，取消/超时即杀。

    stdout 丢弃（pip 的进度条没有回放价值），stderr 落临时文件再读尾巴——
    直接 PIPE 会有缓冲区写满死锁的风险（pip 解析冲突时 stderr 很长）。
    返回 (rc, err_tail)；rc 为 None 表示被取消/超时（err_tail 里带原因）。
    """
    import tempfile
    errf = tempfile.TemporaryFile(mode='w+', encoding='utf-8', errors='replace')
    try:
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=errf,
                                 creationflags=_NO_WIN)
        except Exception as e:
            return None, '%s 启动失败：%s' % (label, str(e)[:200])
        _CUR_PROC['p'] = p
        t0 = time.time()
        while p.poll() is None:
            if time.time() - t0 > timeout:
                _kill_proc(p)
                return None, '%s 超时（%d 秒）' % (label, timeout)
            with _LOCK:
                if _STATE['cancel']:
                    _kill_proc(p)
                    return None, '已取消'
                frac = min(0.98, (time.time() - t0) / max(1.0, est_secs))
                _STATE['pct'] = int(pct0 + (pct1 - pct0) * frac)
                _STATE['msg'] = '%s（已等 %d 分钟）…' % (
                    label, int((time.time() - t0) // 60))
            time.sleep(2.0)
        errf.seek(0)
        return p.returncode, (errf.read() or '')[-400:]
    finally:
        _CUR_PROC['p'] = None
        try:
            errf.close()
        except OSError:
            pass


def _kill_proc(p):
    try:
        if p is not None and p.poll() is None:
            p.kill()
    except OSError:
        pass


def _find_py311():
    from tts_engines import _find_cosyvoice_python
    return _find_cosyvoice_python()


def _probe(py, code, timeout=60):
    """在指定解释器里跑一段 -c 代码，返回 stdout（失败/异常返回 ''）。

    抽出成模块级函数：安装链里三处「探版本 / 探 torch / 探导入」共用同一条
    路径，测试可以整体替换它，不必真的去跑 venv 解释器。"""
    try:
        r = subprocess.run([py, '-c', code], capture_output=True, text=True,
                           timeout=timeout, creationflags=_NO_WIN)
        return (r.stdout or '') if r.returncode == 0 else ''
    except Exception:
        return ''


_MIRROR = ['-i', 'https://mirrors.aliyun.com/pypi/simple/',
           '--trusted-host', 'mirrors.aliyun.com']
# 依赖钉死版本 = 主人真机 venv（2026-11 装）实测可用的组合；qwen-tts 是 worker
# 直接 import 的包，transformers/accelerate 是 from_pretrained 的依赖。
_QWEN_DEPS = ['qwen-tts==0.1.1', 'transformers==4.57.3', 'accelerate==1.12.0',
              'soundfile==0.14.0', 'librosa==0.11.0']


def _install_qwen_env():
    """Qwen3-TTS 运行环境安装：返回 (ok, msg)。步骤幂等，中断重跑秒过已装部分。"""
    def _cancelled():
        with _LOCK:
            return _STATE['cancel']

    def _fail_of(rc, err, what):
        """安装子步骤失败原因归类：取消 / 超时 / 真失败，三者文案必须分得清
        （把用户主动取消报成「安装失败」会让人以为装坏了）。"""
        if rc is None:
            if _cancelled():
                return '已取消'
            return '%s失败：%s' % (what, err or '超时')
        return '%s失败：%s' % (what, err or '未知原因')

    # ① 找 Python 3.11/3.12（PyTorch cu121 不支持 3.13+）
    with _LOCK:
        _STATE.update(pct=3, msg='查找 Python 3.11 / 3.12…')
    base_py = _find_py311()
    if not base_py:
        return False, ('未找到 Python 3.11/3.12（PyTorch 不支持 3.13+）。'
                       '请先安装 Python 3.11（python.org 或 uv），再回来点安装')
    # ② venv（存在就复用；解释器版本不对才重建）
    venv_dir = os.path.join(_T2.TTS2_DIR, 'venv')
    if os.path.isfile(_T2.QWEN_PY):
        ver = _probe(_T2.QWEN_PY, 'import sys; print(sys.version)', timeout=15)
        if '3.13' in ver or '3.14' in ver or '3.15' in ver:
            import shutil
            with _LOCK:
                _STATE.update(pct=8, msg='旧 venv 不兼容 PyTorch，重建…')
            shutil.rmtree(venv_dir, ignore_errors=True)
    if not os.path.isfile(_T2.QWEN_PY):
        with _LOCK:
            _STATE.update(pct=10, msg='创建虚拟环境（%s）…'
                          % os.path.basename(os.path.dirname(os.path.dirname(base_py))))
        rc, err = _run_step([base_py, '-m', 'venv', venv_dir], 10, 12, 60,
                            '创建虚拟环境', 180)
        if rc != 0 or not os.path.isfile(_T2.QWEN_PY):
            # 取消/超时走 _fail_of 归类——否则用户主动取消会被写成「venv 创建失败」
            return False, _fail_of(rc, err, 'venv 创建')
        if _cancelled():
            return False, '已取消'
    pip = os.path.join(venv_dir, 'Scripts', 'pip.exe')
    py = _T2.QWEN_PY
    # ③ pip 升级（幂等）
    with _LOCK:
        _STATE.update(pct=13, msg='升级 pip（国内镜像）…')
    _run_step([pip, 'install', '--upgrade', 'pip'] + _MIRROR, 13, 15, 60,
              '升级 pip', 180)
    # ④ torch：先探已装（幂等跳过 2.4GB 下载）
    torch_ver = _probe(py, 'import torch; print(torch.__version__)')
    if torch_ver:
        with _LOCK:
            _STATE.update(pct=45, msg='✅ PyTorch %s 已在，跳过下载' % torch_ver)
    else:
        if _cancelled():
            return False, '已取消'
        with _LOCK:
            _STATE.update(pct=15, msg='下载 PyTorch CUDA 版（约 2.4GB，国内镜像）…')
        rc, err = _run_step(
            [pip, 'install', 'torch==2.5.1+cu121', 'torchaudio==2.5.1+cu121']
            + _MIRROR + ['--extra-index-url',
                         'https://mirror.sjtu.edu.cn/pytorch-wheels/cu121'],
            15, 45, 1500, '下载 PyTorch CUDA 版（国内镜像）', 3600)
        if rc != 0:
            if _cancelled():
                return False, '已取消'
            with _LOCK:
                _STATE.update(pct=15, msg='镜像失败，改走官方源（国内较慢）…')
            rc, err = _run_step(
                [pip, 'install', 'torch==2.5.1', 'torchaudio==2.5.1',
                 '--index-url', 'https://download.pytorch.org/whl/cu121'],
                15, 45, 1500, '下载 PyTorch CUDA 版（官方源）', 3600)
        if rc != 0:
            if _cancelled():
                return False, '已取消'
            with _LOCK:
                _STATE.update(pct=18, msg='CUDA 版失败，尝试 CPU 版（推理较慢）…')
            rc, err = _run_step([pip, 'install', 'torch', 'torchaudio'] + _MIRROR,
                                18, 30, 600, '下载 PyTorch CPU 版', 1200)
        if rc != 0:
            return False, _fail_of(rc, err, 'PyTorch 安装')
    if _cancelled():
        return False, '已取消'
    # ⑤ 其余依赖（钉死版本，全部走国内镜像）
    with _LOCK:
        _STATE.update(pct=50, msg='安装配音引擎依赖（qwen-tts 等，约 1-3 分钟）…')
    rc, err = _run_step([pip, 'install'] + _QWEN_DEPS + _MIRROR,
                        50, 88, 300, '安装配音引擎依赖', 1800)
    if rc != 0:
        return False, _fail_of(rc, err, '依赖安装')
    if _cancelled():
        return False, '已取消'
    # ⑥ 导入自检：worker 真正要用的三件套必须 import 得动
    with _LOCK:
        _STATE.update(pct=92, msg='自检：导入引擎验证安装…')
    ok_import = _probe(py, 'import torch, soundfile; from qwen_tts import Qwen3TTSModel',
                       timeout=120)
    if not ok_import:
        return False, '安装完成但自检失败（引擎导入报错），请重试或查看安装日志'
    return True, '✅ Qwen3-TTS 运行环境安装完成，可回配音页试听'


def install_async(engine):
    """一键安装（v1.4.79）：权重缺就先下载，再装运行环境。

    qwen3tts 支持整链；indextts2 环境不收编（依赖面太重），明确拒绝。"""
    if engine not in CATALOG:
        return False, '未知引擎：%s' % engine
    if engine != 'qwen3tts':
        return False, ('%s 的运行环境暂不支持一键安装（依赖较重），'
                       '请按 README 手动安装；权重仍可在线下载' % engine)
    with _LOCK:
        if _STATE['running']:
            return False, '已有任务在进行中（%s）' % _STATE['engine']
        _STATE.update(running=True, engine=engine, op='install', pct=0,
                      mb_done=0, mb_total=0, speed_mb_s=0.0,
                      msg='正在准备一键安装…', ok=None, cancel=False)
    threading.Thread(target=_run_install, args=(engine,), daemon=True).start()
    return True, '开始一键安装 Qwen3-TTS（缺权重会先下载，全程可取消）'


def _run_install(engine):
    """install_async 的线程体：权重 → 环境 两段链，任一段失败即停。"""
    try:
        cfg = CATALOG[engine]
        if not cfg['weights_ok']():
            with _LOCK:
                _STATE.update(op='download', mb_total=cfg['mb_total'],
                              msg='权重未下载，先下载（%s）…' % cfg['size_hint'])
            ok, msg = _download_phase(engine)
            if not ok:
                with _LOCK:
                    _STATE.update(ok=False, running=False, msg=msg)
                return
        with _LOCK:
            if _STATE['cancel']:
                _STATE.update(ok=False, running=False, msg='已取消')
                return
            _STATE.update(op='install', msg='权重就绪，安装运行环境…')
        ok, msg = _install_qwen_env()
        with _LOCK:
            _STATE.update(ok=ok, running=False,
                          pct=100 if ok else _STATE['pct'], msg=msg)
    except Exception as e:
        with _LOCK:
            _STATE.update(ok=False, running=False, msg='安装异常：%s' % str(e)[:300])
