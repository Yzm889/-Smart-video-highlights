# -*- coding: utf-8 -*-
"""CosyVoice2-0.5B 推理脚本（venv子进程）。
用法: cosyvoice_worker.py <txt_file> <out_wav> <model_dir> <ref_wav>
使用 zero-shot 声音克隆：参考音频(ref_wav)决定音色。
"""
import sys, os, traceback

repo_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'CosyVoice')
matcha = os.path.join(repo_dir, 'third_party', 'Matcha-TTS')
if os.path.isdir(matcha):
    sys.path.insert(0, matcha)
if os.path.isdir(repo_dir):
    sys.path.insert(0, repo_dir)


def main():
    if len(sys.argv) < 5:
        print('Usage: cosyvoice_worker.py <txt_file> <out_wav> <model_dir> <ref_wav>')
        sys.exit(1)
    txt_file, out_path, model_dir, ref_wav = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

    try:
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

        print(f'[CosyVoice] loading model from {model_dir}...', flush=True)
        from cosyvoice.cli.cosyvoice import CosyVoice2
        import torchaudio

        model = CosyVoice2(model_dir, load_jit=False, load_trt=False, fp16=False)
        print('[CosyVoice] model loaded, synthesizing...', flush=True)

        # zero-shot 推理：prompt_wav 必须是文件路径（CosyVoice内部load_wav）
        for i, j in enumerate(model.inference_zero_shot(
                text, '', ref_wav, zero_shot_spk_id='', stream=False)):
            torchaudio.save(out_path, j['tts_speech'], model.sample_rate)
            print(f'[CosyVoice] saved chunk {i}', flush=True)
            break

        if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
            print('OK')
        else:
            print('FAIL: output too small or missing')
            sys.exit(1)
    except Exception as e:
        print(f'FAIL: {e}')
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
