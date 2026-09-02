# -*- coding: utf-8 -*-
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
