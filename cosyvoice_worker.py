# -*- coding: utf-8 -*-
"""CosyVoice2-0.5B 推理脚本（venv子进程）。

两种用法：
  常驻模式（推荐）： cosyvoice_worker.py serve <model_dir>
      模型只加载一次；从 stdin 逐行读 JSON 请求：
        {"txt": "要合成的文本", "out": "输出wav路径", "ref": "参考音频路径"}
      每个请求回写一行 JSON：
        {"ok": true, "out": "..."} 或 {"ok": false, "error": "..."}
      收到 "QUIT" 或 EOF 退出。文本单行（解说稿已按句切分，天然满足）。
  单发模式（调试兼容）： cosyvoice_worker.py <txt_file> <out_wav> <model_dir> <ref_wav>

为什么需要常驻模式：CosyVoice2-0.5B 模型加载约 100s，逐句起新进程会让一条
30 句的解说在 TTS 上耗近一小时（2026-11-06 实测）；常驻后仅首句付加载成本。
"""
import json
import os
import sys
import time
import traceback

repo_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'CosyVoice')
matcha = os.path.join(repo_dir, 'third_party', 'Matcha-TTS')
if os.path.isdir(matcha):
    sys.path.insert(0, matcha)
if os.path.isdir(repo_dir):
    sys.path.insert(0, repo_dir)

try:  # Windows 管道默认跟随本地编码（中文系统是 GBK），显式切 UTF-8
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass


def _load_model(model_dir):
    from cosyvoice.cli.cosyvoice import CosyVoice2
    # fp16：显存砍半（~2GB），留给同机的 Ollama 文字模型（实测它常驻占 9GB+，
    # fp32 会把 CosyVoice 挤到共享内存，每句慢 10 倍以上）；音质损失可忽略。
    return CosyVoice2(model_dir, load_jit=False, load_trt=False, fp16=True)


def _synth(model, text, ref_wav, out_path):
    """zero-shot 声音克隆合成，成功返回 True。"""
    import torchaudio
    d = os.path.dirname(os.path.abspath(out_path))
    if d:
        os.makedirs(d, exist_ok=True)
    for i, j in enumerate(model.inference_zero_shot(
            text, '', ref_wav, zero_shot_spk_id='', stream=False)):
        torchaudio.save(out_path, j['tts_speech'], model.sample_rate)
        return os.path.exists(out_path) and os.path.getsize(out_path) > 1000
    return False


def serve(model_dir):
    if not os.path.isdir(model_dir):
        print(json.dumps({'ok': False, 'error': 'model dir not found: %s' % model_dir}, ensure_ascii=False), flush=True)
        sys.exit(1)
    t0 = time.time()
    print(json.dumps({'ok': True, 'event': 'loading'}, ensure_ascii=False), flush=True)
    try:
        model = _load_model(model_dir)
    except Exception as e:
        print(json.dumps({'ok': False, 'error': 'load failed: %s' % e, 'traceback': traceback.format_exc()[-400:]}, ensure_ascii=False), flush=True)
        sys.exit(1)
    print(json.dumps({'ok': True, 'event': 'ready', 'load_sec': round(time.time() - t0, 1)}, ensure_ascii=False), flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        if line == 'QUIT':
            break
        try:
            req = json.loads(line)
            txt = (req.get('txt') or '').strip()
            out = req.get('out') or ''
            ref = req.get('ref') or ''
            if not txt or not out or not os.path.exists(ref):
                print(json.dumps({'ok': False, 'error': 'bad request: txt/out/ref required, ref must exist'}, ensure_ascii=False), flush=True)
                continue
            t1 = time.time()
            ok = _synth(model, txt, ref, out)
            print(json.dumps({'ok': bool(ok), 'out': out, 'sec': round(time.time() - t1, 1)}, ensure_ascii=False), flush=True)
        except Exception as e:
            print(json.dumps({'ok': False, 'error': str(e)[:300], 'traceback': traceback.format_exc()[-300:]}, ensure_ascii=False), flush=True)


def one_shot(txt_file, out_path, model_dir, ref_wav):
    """单发模式：兼容旧调用与手动调试。"""
    with open(txt_file, encoding='utf-8') as f:
        text = f.read().strip()
    if not text:
        print('FAIL: empty text')
        sys.exit(1)
    if not os.path.exists(ref_wav):
        print(f'FAIL: ref audio not found: {ref_wav}')
        sys.exit(1)
    if not os.path.isdir(model_dir):
        print(f'FAIL: model dir not found: {model_dir}')
        sys.exit(1)
    print('[CosyVoice] loading model...', flush=True)
    model = _load_model(model_dir)
    print('[CosyVoice] synthesizing...', flush=True)
    ok = _synth(model, text, ref_wav, out_path)
    print('OK' if ok else 'FAIL: output too small or missing')
    sys.exit(0 if ok else 1)


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == 'serve':
        serve(sys.argv[2])
    elif len(sys.argv) >= 5:
        one_shot(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        print('Usage: cosyvoice_worker.py serve <model_dir>')
        print('       cosyvoice_worker.py <txt_file> <out_wav> <model_dir> <ref_wav>')
        sys.exit(1)


if __name__ == '__main__':
    main()
