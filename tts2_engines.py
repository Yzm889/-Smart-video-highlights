# -*- coding: utf-8 -*-
"""v1.4.16 新一代配音引擎适配层：Qwen3-TTS VoiceDesign + IndexTTS2。

两个引擎都跑在独立 venv 的**常驻 worker**里（1.7B / 5.5GB 模型加载几十秒，
逐句起新进程等于每句重付一次，CosyVoice 当年踩过这个坑）。

资产统一放 TTS2_DIR，**不放在 EXE 的 _internal 下** —— 桌面成品每次发版都整体
替换目录，放里面会被替换脚本迁走或直接丢失。可用 FRAMECUT_TTS2_DIR 覆盖。
"""
import json
import os
import queue
import subprocess
import threading
import time

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


def qwen3tts_ready():
    return (os.path.isfile(QWEN_PY) and os.path.isfile(QWEN_WORKER)
            and os.path.isdir(QWEN_MODEL))


def indextts2_ready():
    return (os.path.isfile(IDX_PY) and os.path.isfile(IDX_WORKER)
            and os.path.isdir(IDX_CKPT))


class _Serve:
    """常驻 worker：启动一次，之后每行 JSON 一个请求 / 一行 JSON 一个应答。"""

    def __init__(self, name, cmd, ready_timeout=900):
        self.name = name
        self.cmd = cmd
        self.ready_timeout = ready_timeout
        self.proc = None
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.error = ''

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
        try:
            env = os.environ.copy()
            env['HF_ENDPOINT'] = 'https://hf-mirror.com'
            for _k in ('HTTPS_PROXY', 'HTTP_PROXY', 'https_proxy', 'http_proxy', 'ALL_PROXY'):
                env.pop(_k, None)
            p = subprocess.Popen(self.cmd, stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
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

    def ask(self, req, timeout=300):
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
                    return bool(msg.get('ok')), str(msg.get('error') or '')
            return False, 'worker 超时或无响应'


_QWEN = None
_IDX = None


def _get_qwen():
    global _QWEN
    if _QWEN is None:
        _QWEN = _Serve('qwen3tts', [QWEN_PY, QWEN_WORKER, 'serve', QWEN_MODEL])
    return _QWEN


def _get_idx():
    global _IDX
    if _IDX is None:
        _IDX = _Serve('indextts2', [IDX_PY, IDX_WORKER, 'serve', IDX_REPO, IDX_CKPT])
    return _IDX


def qwen3tts_speak(text, out_path, instruct=None):
    """VoiceDesign：纯文字描述造音色，不需要任何参考音频。"""
    if not (text or '').strip() or not qwen3tts_ready():
        return False
    ok, _err = _get_qwen().ask({
        'text': text,
        'instruct': (instruct or '').strip() or DEFAULT_INSTRUCT,
        'out': out_path,
        'language': 'Chinese',
    })
    return ok and os.path.isfile(out_path) and os.path.getsize(out_path) > 1000


def indextts2_speak(text, out_path, emo_vector=None, emo_text=None,
                    duration_factor=None, spk=None):
    """IndexTTS2：8 维情感 + 时长控制；不传 spk 就用仓库自带示例音色。"""
    if not (text or '').strip() or not indextts2_ready():
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
    ok, _err = _get_idx().ask(req)
    return ok and os.path.isfile(out_path) and os.path.getsize(out_path) > 1000


def engine_status():
    """给前端/日志看的就绪摘要。"""
    return {'dir': TTS2_DIR,
            'qwen3tts': qwen3tts_ready(),
            'indextts2': indextts2_ready()}
