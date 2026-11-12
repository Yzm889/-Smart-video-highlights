# -*- coding: utf-8 -*-
"""配音模型在线下载（v1.4.78）：把主人手工安装时的临时脚本收编进仓库。

背景：tts2 权重当年靠用户数据目录里的两个一次性脚本装进来——
`tts2/dl_models.py`（Qwen3-TTS VoiceDesign + Tokenizer，modelscope）与
`tts2/indextts2/dl_ckpt.py`（IndexTTS-2，modelscope）。普通用户拿不到它们，
模型中心也没有配音模型的任何下载入口（desktop.js 里甚至留过一句
「设置页暂时无法在线补装」）。本模块收编成统一入口。

【下载源】huggingface_hub + hf-mirror（ whispers 下载已验证的同一条国内直连
通道；子进程里显式清掉代理变量——系统代理会劫持大文件下载）。权重是最大头的
部分（Qwen3 ~5.3GB / IndexTTS2 ~6.8GB），modelscope 与 HF 对这两家仓库同源。

【只下权重，不装运行环境】venv + PyTorch 是安装器级工程（CosyVoice 安装器
已证明可行，但独立一轮更稳）。权重齐而 venv 缺时 UI 如实显示
「权重已下载，环境未就绪」，绝不谎报就绪（降级必须可见）。

【取消】下载跑在独立子进程里，取消 = 杀进程；snapshot_download 自带断点续传，
部分文件下次接着下。
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

_STATE = {'running': False, 'engine': '', 'pct': 0, 'mb_done': 0,
          'mb_total': 0, 'speed_mb_s': 0.0, 'msg': '', 'ok': None,
          'cancel': False}
_LOCK = threading.Lock()


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
    """给前端模型中心卡的实时状态（自带两个引擎的就绪/权重判定）。"""
    with _LOCK:
        st = dict(_STATE)
    st['engines'] = {}
    for eng, cfg in CATALOG.items():
        ready = {'qwen3tts': _T2.qwen3tts_ready,
                 'indextts2': _T2.indextts2_ready}.get(eng, lambda: False)
        st['engines'][eng] = {
            'label': cfg['label'],
            'size_hint': cfg['size_hint'],
            'ready': bool(ready()),
            'weights': bool(cfg['weights_ok']()),
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
    """请求取消当前下载（杀子进程；已落盘部分保留，断点续传下次接着下）。"""
    with _LOCK:
        if _STATE['running']:
            _STATE['cancel'] = True
            return True, '已请求取消（几秒内生效，已下载的部分保留）'
        return False, '当前没有下载任务'


def download_async(engine):
    """启动后台下载。返回 (ok, msg)；同一时间只允许一个下载任务。"""
    if engine not in CATALOG:
        return False, '未知引擎：%s' % engine
    with _LOCK:
        if _STATE['running']:
            return False, '已有下载任务在进行中（%s）' % _STATE['engine']
        _STATE.update(running=True, engine=engine, pct=0, mb_done=0,
                      mb_total=CATALOG[engine]['mb_total'], speed_mb_s=0.0,
                      msg='正在启动下载…', ok=None, cancel=False)
    threading.Thread(target=_run, args=(engine,), daemon=True).start()
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


def _run(engine):
    cfg = CATALOG[engine]
    procs = []
    ok = False
    msg = ''
    try:
        for repo, rel in cfg['repos']:
            dst = os.path.join(_T2.TTS2_DIR, rel)
            os.makedirs(dst, exist_ok=True)
            with _LOCK:
                if _STATE['cancel']:
                    _STATE.update(ok=False, running=False,
                                  msg='已取消（已下载的部分保留，下次继续）')
                    return
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
                if _STATE['cancel'] and not ok:
                    for p, _d, _r in procs:
                        try:
                            p.kill()
                        except OSError:
                            pass
                    _STATE.update(ok=False, running=False,
                                  msg='已取消（已下载的部分保留，下次继续）')
                    return
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
                msg = '下载失败：%s%s' % (rc_bad[0][1], ('｜' + err) if err else '')
                break
            for _p, dst, _r in procs:
                _flatten_if_nested(dst)
            if cfg['weights_ok']():
                ok = True
                msg = '✅ %s 权重下载完成' % cfg['label']
            else:
                msg = ('下载结束但权重校验未通过（文件不全或仓库布局变了），'
                       '请重试一次；反复失败请查看日志')
            break
    except Exception as e:
        msg = '下载异常：%s' % str(e)[:300]
    finally:
        with _LOCK:
            _STATE.update(ok=ok, running=False, pct=100 if ok else _STATE['pct'],
                          msg=msg or _STATE['msg'])
