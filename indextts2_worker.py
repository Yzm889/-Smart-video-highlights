# -*- coding: utf-8 -*-
"""IndexTTS2 常驻 worker（8 维情感向量 + 时长控制，模型只加载一次）。

由 tts_engines.py 用 index-tts 仓库自己的 uv venv 启动，走 stdin/stdout 单行 JSON：
  启动成功  -> {"event":"ready"}
  每请求    <- {"text":..., "out":..., "spk":..., "emo_vector":[8], "emo_text":...,
                "emo_alpha":0.6, "duration_factor":1.0}
  每应答    -> {"ok":true,"path":...} / {"ok":false,"error":...}

默认音色用仓库自带 examples/voice_01.wav —— 用户不录音也能出声，
这是和旧 CosyVoice「必须手动放参考音」最大的区别。
"""
import json
import os

# v1.4.16：模型已完整落在本地目录，禁止 huggingface_hub 再联网做 HEAD 探测。
# huggingface.co 直连不通时会 5xN 次重试把常驻 worker 卡死（实测 240s 无 ready）。
# 只把 endpoint 换成国内可达的镜像；不要设 HF_HUB_OFFLINE —— qwen_tts
# 的 AutoProcessor 还要补一个小 config，强制离线反而会直接报缺文件。
os.environ.setdefault('HF_ENDPOINT', 'https://hf-mirror.com')
import os
import sys
import time

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
    if len(sys.argv) < 3:
        print(json.dumps({'event': 'error',
                          'error': 'usage: serve <repo_dir> <checkpoints_dir>'}))
        return 1
    repo_dir, ckpt_dir = sys.argv[-2], sys.argv[-1]   # <worker> serve <repo> <ckpt>
    sys.path.insert(0, repo_dir)
    t0 = time.time()
    try:
        from indextts.infer_v2 import IndexTTS2
        tts = IndexTTS2(
            cfg_path=os.path.join(ckpt_dir, 'config.yaml'),
            model_dir=ckpt_dir,
            use_fp16=True,           # 3060 12GB 必须半精度，否则显存踩线
            use_cuda_kernel=False,
            use_deepspeed=False,
        )
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({'event': 'error', 'error': str(e)[:500]}), flush=True)
        return 1

    default_spk = os.path.join(repo_dir, 'examples', 'voice_01.wav')
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
            spk = req.get('spk') or default_spk
            kw = dict(spk_audio_prompt=spk, text=text,
                      output_path=out, use_random=False, verbose=False)
            if req.get('emo_vector'):
                kw['emo_vector'] = [float(x) for x in req['emo_vector']]
            if req.get('emo_text'):
                kw['use_emo_text'] = True
                kw['emo_text'] = req['emo_text']
                kw['emo_alpha'] = float(req.get('emo_alpha') or 0.6)
            if req.get('duration_factor'):
                kw['duration_factor'] = float(req['duration_factor'])
            tts.infer(**kw)
            ok = os.path.exists(out) and os.path.getsize(out) > 1000
            print(json.dumps({'ok': ok, 'path': out,
                              'error': '' if ok else 'no audio output'}), flush=True)
        except Exception as e:
            print(json.dumps({'ok': False, 'error': str(e)[:500]}), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
