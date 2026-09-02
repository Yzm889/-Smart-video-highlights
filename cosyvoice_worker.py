# -*- coding: utf-8 -*-
"""CosyVoice2-0.5B 推理脚本（venv子进程）。
用法: cosyvoice_worker.py <txt_file> <out_wav> <model_dir> <ref_wav>
使用 zero-shot 声音克隆：参考音频(ref_wav)决定音色。
"""
import sys, os

repo_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'CosyVoice')
matcha = os.path.join(repo_dir, 'third_party', 'Matcha-TTS')
if os.path.isdir(matcha):
    sys.path.insert(0, matcha)
if os.path.isdir(repo_dir):
    sys.path.insert(0, repo_dir)


def load_ref_audio(ref_wav, target_sr=16000):
    """加载参考音频并重采样到16kHz。返回 torch.Tensor (1, T)。"""
    import torchaudio
    import torch
    wav, sr = torchaudio.load(ref_wav)
    if wav.shape[0] > 1:
        wav = wav.mean(dim=0, keepdim=True)
    if sr != target_sr:
        resampler = torchaudio.transforms.Resample(orig_freq=sr, new_freq=target_sr)
        wav = resampler(wav)
    return wav


def main():
    if len(sys.argv) < 5:
        print('Usage: cosyvoice_worker.py <txt_file> <out_wav> <model_dir> <ref_wav>')
        sys.exit(1)
    txt_file, out_path, model_dir, ref_wav = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

    with open(txt_file, encoding='utf-8') as f:
        text = f.read().strip()
    if not text:
        print('empty text')
        sys.exit(1)
    if not os.path.exists(ref_wav):
        print(f'FAIL: ref audio not found: {ref_wav}')
        sys.exit(1)

    from cosyvoice.cli.cosyvoice import CosyVoice2
    import torchaudio

    model = CosyVoice2(model_dir, load_jit=False, load_trt=False, fp16=False)

    # 加载参考音频（16kHz）
    prompt_speech_16k = load_ref_audio(ref_wav, target_sr=16000)

    # zero-shot 推理：用参考音频的音色合成文本
    # prompt_text 留空（CosyVoice2 支持空 prompt_text）
    for i, j in enumerate(model.inference_zero_shot(
            text, '', prompt_speech_16k, zero_shot_spk_id='', stream=False)):
        torchaudio.save(out_path, j['tts_speech'], model.sample_rate)
        break

    if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
        print('OK')
    else:
        print('FAIL: output too small')
        sys.exit(1)


if __name__ == '__main__':
    main()
