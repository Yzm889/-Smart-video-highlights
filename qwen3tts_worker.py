# -*- coding: utf-8 -*-
"""Qwen3-TTS VoiceDesign 常驻 worker（模型只加载一次，句句复用）。

由 tts_engines.py 用独立 venv 的 Python 启动，走 stdin/stdout 单行 JSON 协议：
  启动成功  -> {"event":"ready"}
  每请求    <- {"text":..., "instruct":..., "out":..., "language":"Chinese"}
  每应答    -> {"ok":true,"path":...} / {"ok":false,"error":...}

为什么常驻：1.7B 模型加载几十秒，逐句起进程等于每句重付一次，
一条 30 句解说会拖成十几分钟（CosyVoice 当年踩过同一个坑）。
"""
import json
import os

# v1.4.16：模型已完整落在本地目录，禁止 huggingface_hub 再联网做 HEAD 探测。
# huggingface.co 直连不通时会 5xN 次重试把常驻 worker 卡死（实测 240s 无 ready）。
# 只把 endpoint 换成国内可达的镜像；不要设 HF_HUB_OFFLINE —— qwen_tts
# 的 AutoProcessor 还要补一个小 config，强制离线反而会直接报缺文件。
os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'
# 本机代理对 hf 域名会读超时；镜像直连可达（curl 实测 200）
for _k in ('HTTPS_PROXY','HTTP_PROXY','https_proxy','http_proxy','ALL_PROXY'):
    os.environ.pop(_k, None)
import sys
import time

DEFAULT_INSTRUCT = ('沉稳克制的中文男声，影视剧情解说风格，语气有起伏但不夸张，'
                    '句间有自然停顿，咬字清晰，不要播报腔')

import sys as _sys

class _StdoutGuard:
    """第三方库会往 stdout 裸 print（如 IndexTTS2 的 print(wav.shape)），
    而 stdout 是本进程与父进程的 JSON 协议通道。管道一断，那句 print 会抛
    OSError 22，把整句配音一起毁掉。包一层：写失败就丢弃，绝不让一句
    调试输出毁掉一次推理。"""
    def __init__(self, real):
        self._real = real
    def write(self, s):
        try:
            return self._real.write(s)
        except Exception:
            return len(s) if s else 0
    def flush(self):
        try:
            self._real.flush()
        except Exception:
            pass
    def __getattr__(self, name):
        return getattr(self._real, name)

_sys.stdout = _StdoutGuard(_sys.stdout)


def main():
    if len(sys.argv) < 2:
        print(json.dumps({'event': 'error', 'error': 'usage: serve <model_dir>'}))
        return 1
    model_dir = sys.argv[-1]   # 命令是 <worker> serve <model_dir>
    t0 = time.time()
    try:
        import torch
        import soundfile as sf
        from qwen_tts import Qwen3TTSModel

        cuda = torch.cuda.is_available()
        # local_files_only=True 是关键：qwen_tts 内部 cached_file 默认
        # local_files_only=False，即使模型全在本地也会去 HF 做 HEAD 探测，
        # huggingface.co 直连不通时 5xN 次重试把 worker 卡死。
        kw = {'device_map': 'cuda:0' if cuda else 'cpu',
              'dtype': torch.bfloat16 if cuda else torch.float32}
        # Windows 上 flash-attn 极难编译安装；优先 sdpa，不支持就退回库默认实现
        try:
            model = Qwen3TTSModel.from_pretrained(
                model_dir, attn_implementation='sdpa', **kw)
        except Exception:
            model = Qwen3TTSModel.from_pretrained(model_dir, **kw)
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({'event': 'error', 'error': str(e)[:500]}), flush=True)
        return 1

    print(json.dumps({'event': 'ready', 'load_sec': round(time.time() - t0, 1)}),
          flush=True)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            text = (req.get('text') or '').strip()
            out = req.get('out') or ''
            if not text or not out:
                raise ValueError('缺 text / out')
            wavs, sr = model.generate_voice_design(
                text=text,
                language=req.get('language') or 'Chinese',
                instruct=(req.get('instruct') or '').strip() or DEFAULT_INSTRUCT,
            )
            sf.write(out, wavs[0], sr)
            print(json.dumps({'ok': True, 'path': out}), flush=True)
        except Exception as e:
            print(json.dumps({'ok': False, 'error': str(e)[:500]}), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
