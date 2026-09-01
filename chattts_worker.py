# -*- coding: utf-8 -*-
"""ChatTTS 子进程合成脚本：由 webui_server.py 通过 venv Python 调用。"""
import sys, os, json, traceback

def main():
    if len(sys.argv) < 3:
        print(json.dumps({'error': 'usage: chattts_worker.py <text_file> <out_wav>'}))
        sys.exit(1)
    text_file = sys.argv[1]
    out_path = sys.argv[2]

    with open(text_file, 'r', encoding='utf-8-sig') as f:  # utf-8-sig 去 BOM
        text = f.read().strip()
    if not text:
        print(json.dumps({'error': 'empty text'}))
        sys.exit(1)

    try:
        import torch
        import numpy as np
        import ChatTTS
        model = ChatTTS.Chat()
        local_model = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'models', 'chattts')
        ok = model.load(source='local', custom_path=local_model, compile=False)
        if not ok:
            print(json.dumps({'error': 'model load failed'}))
            sys.exit(1)
        wavs = model.infer(text, use_decoder=True)
        if wavs and len(wavs) > 0:
            wav = wavs[0]
            # 统一转成 2D numpy [1, samples] 供 soundfile 保存
            if isinstance(wav, torch.Tensor):
                arr = wav.cpu().numpy()
            else:
                arr = np.array(wav)
            if arr.ndim == 1:
                arr = arr.reshape(1, -1)
            elif arr.ndim == 2 and arr.shape[0] > arr.shape[1]:
                arr = arr.T  # 确保是 [channels, samples]
            import soundfile as sf
            sf.write(out_path, arr.T, 24000)  # soundfile 需要 [samples, channels]
            if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
                print(json.dumps({'ok': True, 'path': out_path}))
                sys.exit(0)
        print(json.dumps({'error': 'no audio output'}))
        sys.exit(1)
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({'error': str(e)[:500]}))
        sys.exit(1)

if __name__ == '__main__':
    main()
