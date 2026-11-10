# -*- coding: utf-8 -*-
"""直播切片引擎（方案 A：本地直播录像 → 自动找高光 → N 条原声独立 mp4）。

【边界（2026-11-11 主人授权 AI 自行拍板，记录在 PLAN_LIVE_CLIP.md 第 0 节）】
  输入：本地直播录像（OBS 录制 / 平台回放下载的 mp4/mkv/flv/ts…）；
  输出：N 条独立 mp4（**保留原声**，不配音、不竖屏、不烧字幕）+ 可读报告；
  高光判定：只用**离线信号**（音量能量 + 画面运动 + 台词密度/高能词），
  阈值全部相对化（全片分位次），VLM 语义理解留作后续增强。

【为什么独立模块】对齐/打分是 movie_narrator 的命脉（DP 单调约束刚修到 0 倒流），
本模块绝不 import 它、绝不改它；只复用 beat_analysis 的帧分析、ffmpeg_utils 的
进程管理与 webui_server 的编码参数，风格与 beat_analysis 一致（宿主晚绑定）。

【回滚开关】环境变量 LIVE_CLIP=0 → dispatch 层直接拒绝，前端入口随测试钉住，
产品代码无需另行回滚。所有阈值可用环境变量覆盖（真机调优不重发版）。
"""
import json
import logging
import os
import re
import sys
import time
import wave as _wave

import numpy as np

from ffmpeg_utils import ffmpeg_run
from ai_providers import asr_segments

_log = logging.getLogger('framecut.live')

# ---- 宿主晚绑定（与 beat_analysis 同一套约定）----
_HERE = os.path.dirname(os.path.abspath(__file__))
_HOST_FILE = os.path.join(_HERE, 'webui_server.py')


def _host():
    m = sys.modules.get('webui_server')
    if m is not None:
        return m
    m = sys.modules.get('__main__')
    if m is not None and os.path.abspath(getattr(m, '__file__', '')) == _HOST_FILE:
        return m
    import webui_server
    return webui_server


def _env_float(name, default):
    try:
        v = os.environ.get(name, '')
        return float(v) if v else float(default)
    except (TypeError, ValueError):
        return float(default)


def enabled():
    """总开关：LIVE_CLIP=0 时整条功能拒绝执行（回滚开关）。"""
    return os.environ.get('LIVE_CLIP', '1') != '0'


# ---- 可调参数（环境变量覆盖）----
WINDOW = _env_float('LIVE_CLIP_WINDOW', 2.0)      # 信号窗口粒度（秒）
MIN_SEC = _env_float('LIVE_CLIP_MIN', 20.0)       # 单条最短
MAX_SEC = _env_float('LIVE_CLIP_MAX', 60.0)       # 单条最长
N_CLIPS = int(_env_float('LIVE_CLIP_N', 5))       # 默认出几条
MIN_GAP = _env_float('LIVE_CLIP_GAP', 45.0)       # 两条高光至少隔开（秒）
W_AUDIO = _env_float('LIVE_CLIP_W_AUDIO', 0.5)    # 音量权重
W_MOTION = _env_float('LIVE_CLIP_W_MOTION', 0.3)  # 画面运动权重
W_SPEECH = _env_float('LIVE_CLIP_W_SPEECH', 0.2)  # 台词密度权重
ASR_MAX_SEC = _env_float('LIVE_CLIP_ASR_MAX', 5400.0)  # 超过 90 分钟跳过台词信号
# 扩边时的能量地板：窗口分位次低于峰值这么多倍就认为高光结束
_EDGE_FLOOR = _env_float('LIVE_CLIP_EDGE_FLOOR', 0.35)

LIVE_EXTS = ('.mp4', '.mkv', '.flv', '.ts', '.mov', '.webm', '.avi')

# 高能词：直播里「观众/主播情绪爆棚」的口癖，作为台词信号的可读佐证。
# 故意只收高频、跨内容的词（游戏/带货/聊天通用），不追平台黑话。
HL_WORDS = ('哈哈', '笑死', '卧槽', '我草', '666', '233', '绝了', '牛',
            '翻盘', '名场面', '芜湖', '太强', '无敌', '炸裂', '精彩', 'awsl')


def _probe_duration(video_path):
    """返回 (时长秒, 是否有音轨)。复用 beat_analysis 的探测姿势：
    ffmpeg 无输出参数时 rc!=0 但 stderr 带完整流信息，从中解析。"""
    rc, _o, err = ffmpeg_run(['-i', video_path])
    text = err.decode('utf-8', 'ignore')
    md = re.search(r'Duration:\s*(\d+):(\d{2}):(\d+(?:\.\d+)?)', text)
    vdur = (int(md.group(1)) * 3600 + int(md.group(2)) * 60 + float(md.group(3))) if md else 0.0
    return vdur, ('Audio:' in text)


def _load_pcm(video_path, rate=8000, run_dir=None):
    """整条音轨解码成单声道 int16 numpy。wav 先落盘再读（管道直通会在
    2~6 小时录像上憋几百 MB 内存，落盘后按需读帧，峰值内存只有数组本身）。"""
    wav = os.path.join(run_dir or os.path.dirname(os.path.abspath(video_path)),
                       '_live_pcm_tmp.wav')
    rc, _o, err = ffmpeg_run(['-y', '-i', video_path, '-vn', '-ac', '1', '-ar', str(rate),
                              '-acodec', 'pcm_s16le', '-f', 'wav', wav])
    try:
        if rc != 0 or not os.path.exists(wav) or os.path.getsize(wav) < 44:
            _log.info('[LIVE] 音轨解码失败: %s', err.decode('utf-8', 'ignore')[-200:])
            return None, rate
        with _wave.open(wav, 'rb') as w:
            n = w.getnframes()
            data = np.frombuffer(w.readframes(n), dtype=np.int16)
        return data, rate
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass


def _rms_profile(pcm, rate, window):
    """每 window 秒一个 RMS（0~1 原始值）。pcm 为空/时长不足一个窗口 → 空数组。"""
    win = max(1, int(rate * window))
    nwin = len(pcm) // win
    if nwin <= 0:
        return np.zeros(0, dtype=np.float32)
    x = pcm[:nwin * win].reshape(nwin, win).astype(np.float32) / 32768.0
    return np.sqrt((x ** 2).mean(axis=1))


def _motion_profile(video_path, window, progress=None):
    """每 window 秒的平均画面运动量（均值帧差）。复用 beat_analysis 的流式抽帧
    （自适应降 fps + 降分辨率，长视频不会爆），返回 (profile|None, vdur)。"""
    import beat_analysis as BA
    an = BA._analyze_video_frames(video_path, progress=progress)
    if not an or not len(an.get('times') or []):
        return None, 0.0
    times = np.asarray(an['times'], dtype=np.float64)
    mot = np.asarray(an['motion'], dtype=np.float64)
    idx = (times / window).astype(int)
    nwin = int(idx.max()) + 1
    sums = np.bincount(idx, weights=mot, minlength=nwin)
    cnts = np.bincount(idx, minlength=nwin).astype(np.float64)
    cnts[cnts == 0] = 1.0
    return (sums / cnts).astype(np.float32), float(times.max() + 1.0 / 4.0)


def _speech_profile(asr, nwin, window):
    """每窗口的台词覆盖秒数 + 高能词次数。返回 (density, kw_counts)。"""
    density = np.zeros(nwin, dtype=np.float32)
    kw = np.zeros(nwin, dtype=np.float32)
    for seg in (asr or []):
        try:
            s = float(seg.get('start', 0) or 0)
            e = float(seg.get('end', s) or s)
        except (TypeError, ValueError):
            continue
        if e <= s:
            e = s + 0.5
        i0 = max(0, int(s / window))
        i1 = min(nwin, max(i0 + 1, int(e / window) + 1))
        for i in range(i0, i1):
            # 每个覆盖窗只累加**窗内实际覆盖秒数**（旧写法把整段时长加给了每个窗，
            # 一条 5 秒台词会让覆盖到的每个窗都 +5，密度曲线直接爆表）
            cov = min(e, (i + 1) * window) - max(s, i * window)
            if cov > 0:
                density[i] += min(cov, window)
        txt = str(seg.get('text', '') or '')
        if txt:
            kw[i0:i1] += float(min(3, sum(txt.count(w) for w in HL_WORDS)))
    return density, kw


def pct_rank(x):
    """0~1 全片分位次（相对化核心）：对绝对音量、编码响度差异、分辨率差异免疫。
    常数序列（无声/静态画面）返回全 0.5，避免把「全场一样响」误判成处处峰值。"""
    x = np.asarray(x, dtype=np.float64)
    if x.size == 0:
        return x
    if float(x.max() - x.min()) <= 1e-9:
        return np.full(x.shape, 0.5)
    order = np.argsort(x, kind='stable')
    ranks = np.empty(x.size, dtype=np.float64)
    ranks[order] = np.arange(x.size)
    return ranks / max(1, x.size - 1)


def smooth(x, k):
    """滑动平均（窗口 k，边缘归一），把逐窗噪声抹成可扩边的能量曲线。"""
    x = np.asarray(x, dtype=np.float64)
    if x.size == 0 or k <= 1:
        return x
    k = int(min(k, x.size))
    kernel = np.ones(k, dtype=np.float64) / k
    return np.convolve(x, kernel, mode='same')


def select_clips(score, window, vdur, n_clips=None, min_sec=None, max_sec=None,
                 min_gap=None):
    """从能量曲线挑 N 条高光段。

    策略：迭代取峰 → 向两侧扩边（能量跌破峰值的 _EDGE_FLOOR 即止，钳制
    [min_sec, max_sec]）→ 抑制已选段 ±min_gap → 合并重叠 → 按分数取前 N。
    返回 [{'start','end','score','peak_t'}]，按时间升序。"""
    n_clips = max(1, int(n_clips or N_CLIPS))
    min_sec = float(min_sec or MIN_SEC)
    max_sec = float(max_sec or MAX_SEC)
    min_gap = float(min_gap or MIN_GAP)
    max_sec = max(min_sec, max_sec)
    s = smooth(score, max(3, int(5.0 / window)))
    nwin = s.size
    if nwin == 0 or vdur <= 0:
        return []
    taken = []
    first_score = None
    for _guard in range(200):
        work = s.copy()
        for c in taken:                       # 抑制已选段 ±min_gap
            i0 = max(0, int((c['start'] - min_gap) / window))
            i1 = min(nwin, int((c['end'] + min_gap) / window) + 1)
            work[i0:i1] = -1.0
        pk = int(np.argmax(work))
        if work[pk] <= 0:
            break                             # 全被抑制/全零：没有更多高光了
        if first_score is None:
            first_score = float(work[pk])
        # 「显著高光」守门：候选峰必须仍是全片前列（≥中位）且不比第一条明显塌陷。
        # 没有这道闸，平坦地形（整场都安静的录像）会被切成一串 0.3 分的碎片。
        if work[pk] < max(0.5, 0.6 * first_score):
            break
        floor = float(s[pk]) * _EDGE_FLOOR
        lo = hi = pk
        while (hi - lo) * window < max_sec:
            grow_hi = hi + 1 < nwin and s[hi + 1] >= floor
            grow_lo = lo - 1 >= 0 and s[lo - 1] >= floor
            if not grow_hi and not grow_lo:
                break
            if grow_hi:
                hi += 1
            if grow_lo and (hi - lo) * window < max_sec:
                lo -= 1
        # 太短（峰附近都平静）→ 对称补到最短时长
        while (hi - lo) * window < min_sec and (lo > 0 or hi < nwin - 1):
            if lo > 0:
                lo -= 1
            if (hi - lo) * window < min_sec and hi < nwin - 1:
                hi += 1
        taken.append({
            'start': max(0.0, lo * window),
            'end': min(vdur, (hi + 1) * window),
            'score': float(s[pk]),
            'peak_t': (pk + 0.5) * window,
        })
    # 合并重叠（扩边可能让两段贴上）：按时间排序后相邻合并，分数取高者
    taken.sort(key=lambda c: c['start'])
    merged = []
    for c in taken:
        if merged and c['start'] <= merged[-1]['end'] + 1.0:
            m = merged[-1]
            m['end'] = max(m['end'], c['end'])
            m['score'] = max(m['score'], c['score'])
            m['peak_t'] = c['peak_t'] if c['score'] > m['score'] else m['peak_t']
        else:
            merged.append(dict(c))
    merged.sort(key=lambda c: -c['score'])
    out = merged[:n_clips]
    out.sort(key=lambda c: c['start'])
    return out


def _reasons(peak_idx, ranks, kw, keywords_names=('音量', '画面动作', '台词密度')):
    """人能看懂的入选理由：只报「全片前 X%」级别的强信号，最多三条。"""
    parts = []
    for name, r in zip(keywords_names, ranks):
        if r is None or r.size == 0 or peak_idx >= r.size:
            continue
        v = float(r[peak_idx])
        # 只报「真正靠前」的信号：v 是全片分位次（1=最高），≥0.70 才算前 30%
        # （旧判断写成 top>=30，低分位窗口反而顶着「全片前95%」的帽子入选——方向反了）
        if v >= 0.70:
            top = max(1, int(round((1.0 - v) * 100)))
            parts.append('%s峰值（全片前%d%%）' % (name, top))
    if kw is not None and kw.size > peak_idx and kw[peak_idx] >= 1:
        parts.append('出现高能词（如「哈哈/666」）')
    if not parts:
        parts.append('综合能量处于全片前列')
    return parts[:3]


def analyze_highlights(video_path, run_dir, progress=None, n_clips=None,
                       min_sec=None, max_sec=None, use_asr=True, asr_language=None):
    """主入口：一条录像 → 高光段列表（含可读理由）。

    进度约定与全产品一致：progress['phase']/['pct']，用户取消抛 AbortError
    （ffmpeg_run/帧分析/ASR 都已接 RUN_PROCS 协作取消）。"""
    up = (lambda phase, pct: progress.update({'phase': phase, 'pct': pct})
          if progress else (lambda phase, pct: None))
    vdur, has_audio = _probe_duration(video_path)
    if vdur <= 0:
        raise RuntimeError('读不到视频时长（文件损坏或不是视频？）')
    if not has_audio:
        raise RuntimeError('这条录像没有音轨——直播切片必须有声音才能评估高光')

    win = max(0.5, min(float(WINDOW), 10.0))
    up('分析音量能量', 8)
    pcm, rate = _load_pcm(video_path, run_dir=run_dir)
    if pcm is None or pcm.size == 0:
        raise RuntimeError('音轨解码失败（容器损坏或音频编码不受支持）')
    audio = _rms_profile(pcm, rate, win)
    nwin = audio.size

    up('检测画面运动（逐帧分析）', 18)
    motion, _mdur = _motion_profile(video_path, win, progress=None)
    if motion is not None and motion.size and motion.size != nwin:
        n = min(nwin, motion.size)
        m2 = np.zeros(nwin, dtype=np.float32)
        m2[:n] = motion[:n]
        motion = m2

    asr = []
    if use_asr and vdur <= ASR_MAX_SEC:
        _lang_note = {'auto': '自动判别'}.get(asr_language or '', asr_language)
        up('转写台词（Whisper · %s，长片请耐心）' % (_lang_note or '自动判别'), 40)
        try:
            asr = asr_segments(video_path, language=asr_language or None) or []
        except Exception as e:
            _log.info('[LIVE] ASR 失败（降级为音量+画面双信号）: %s', e)
            asr = []
    elif use_asr:
        _log.info('[LIVE] 录像 %.0f 分钟超过 %.0f 分钟，跳过台词信号（省一半时间）',
                  vdur / 60.0, ASR_MAX_SEC / 60.0)
    nwin2 = max(nwin, int(vdur / win) + 1)
    speech = np.zeros(nwin2, dtype=np.float32)
    kw = np.zeros(nwin2, dtype=np.float32)
    if asr:
        d, k = _speech_profile(asr, nwin2, win)
        speech[:min(nwin2, d.size)] = d[:min(nwin2, d.size)]
        kw[:min(nwin2, k.size)] = k[:min(nwin2, k.size)]

    # 融合：三路分位次按权重合成（某路缺席就把权重匀给其它路，不空算）
    up('挑选高光片段', 62)
    r_audio = pct_rank(audio)
    if r_audio.size < nwin2:                 # 音频比视频短（尾部无音轨）：中性值补齐
        a2 = np.full(nwin2, 0.5)
        a2[:r_audio.size] = r_audio
        r_audio = a2
    ranks = [r_audio]
    weights = [W_AUDIO]
    r_motion = pct_rank(motion) if motion is not None and motion.size else None
    r_speech = pct_rank(speech) if asr else None
    if r_motion is not None:
        if r_motion.size < nwin2:
            m2 = np.full(nwin2, 0.5)
            m2[:r_motion.size] = r_motion
            r_motion = m2
        ranks.append(r_motion)
        weights.append(W_MOTION)
    if r_speech is not None:
        ranks.append(r_speech)
        weights.append(W_SPEECH)
    wsum = float(sum(weights)) or 1.0
    combined = np.zeros(nwin2, dtype=np.float64)
    for r, w in zip(ranks, weights):
        combined[:r.size] += float(w / wsum) * r
    clips = select_clips(combined, win, vdur, n_clips=n_clips,
                         min_sec=min_sec, max_sec=max_sec)
    for c in clips:
        pk = int(c['peak_t'] / win)
        c['reasons'] = _reasons(pk, [ranks[0], r_motion, r_speech], kw)
        c['score'] = round(c['score'], 3)
        c['start'] = round(c['start'], 2)
        c['end'] = round(c['end'], 2)
        c['dur'] = round(c['end'] - c['start'], 2)
        c['peak_t'] = round(c['peak_t'], 2)
    if not clips:
        raise RuntimeError('没挑出任何高光段（录像可能整段都是静音/静止画面）')
    _log.info('[LIVE] 高光挑选完成: %d 条（音量窗口 %d，运动 %s，台词 %s段）',
              len(clips), nwin, '有' if motion is not None else '无', len(asr))
    return {'clips': clips, 'vdur': round(vdur, 2), 'window': win,
            'asr_used': bool(asr)}


def _vf_chain(aspect, tm):
    """按输出画幅构造 -vf 滤镜链。aspect：orig=原始 / crop=竖屏9:16居中裁切 /
    blur=竖屏9:16全景+虚化背景（发抖音推荐：侧边摄像头和弹幕都不丢）。
    tm 为 HDR tonemap 链（可能为空），永远排在最前（先调色再构图形）。

    【为什么竖屏默认给 blur 而不是硬裁】直播画面里主播摄像头在角落、弹幕在侧边，
    居中裁切会把它们全部切掉；虚化背景板（剪映同款）保留全部信息还更上镜。
    crop 留给「主体居中」的素材。"""
    _base = 'setpts=(PTS-STARTPTS)' + ((',' + tm) if tm else '')
    if aspect == 'crop':
        # 居中裁 9:16，再统一到 720x1280（短视频标准竖屏，无奇数维）
        crop = ("crop=w='min(iw\\,ih*9/16)':h=ih:"
                "x='(iw-min(iw\\,ih*9/16))/2':y=0")
        return _base + ',' + crop + ',scale=720:1280:flags=lanczos'
    if aspect == 'blur':
        # 背景：放大裁满 9:16 再高斯虚化；前景：**完整画面等比缩进画布**居中叠加
        # （decrease 而非 increase——increase 会把 16:9 前景放到 2276x1280 直接溢出
        # 720 宽画布，效果退化成居中裁切，虚化板完全被盖住，侧边内容照样丢：
        # 2026-11-11 E2E 抽帧实测踩中）。带 [标签] 的完整 filtergraph 必须走
        # -filter_complex（-vf 只收简单线性链），末尾标 [vout] 供 -map 精确取轨。
        return ('[0:v]' + _base + ',scale=720:1280:force_original_aspect_ratio=increase,'
                'crop=720:1280,gblur=sigma=24,setsar=1[bg];'
                '[0:v]' + _base + ',scale=720:1280:force_original_aspect_ratio=decrease:flags=lanczos[fg];'
                '[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[vout]')
    return _base


def make_covers(video_path, clips, run_dir, progress=None, aspect='orig'):
    """每条切片自动挑一张封面：在切片时间窗内采 6 个候选帧（峰值时刻 + 均布 5 点），
    复用产品已有的封面打分器（对比度+细节-过曝过暗）选最佳一帧，存 ASCII 文件名。
    aspect 为竖屏（crop/blur）时封面同步居中裁成 9:16——直发抖音用得上。
    返回与 clips 等长的封面路径列表（失败的位置为 ''，不阻断导出）。"""
    _w = _host()
    up = (lambda phase, pct: progress.update({'phase': phase, 'pct': pct})
          if progress else (lambda phase, pct: None))
    cand_dir = os.path.join(run_dir, '_covers')
    covers = []
    try:
        for i, c in enumerate(clips):
            up('挑封面 %d/%d' % (i + 1, len(clips)),
               90 + int(6 * i / max(1, len(clips))))
            span = max(1.0, c['end'] - c['start'])
            peak_t = c.get('peak_t') or (c['start'] + span / 2)
            ts_list = [min(max(t, c['start'] + 0.2), c['end'] - 0.2) for t in (
                peak_t,
                c['start'] + span * 0.25,
                c['start'] + span * 0.45,
                c['start'] + span * 0.65,
                c['start'] + span * 0.85,
                c['start'] + span * 0.5,
            )]
            try:
                _w._cover_candidates_sequential(video_path, cand_dir, ts_list)
            except Exception as e:
                _log.info('[LIVE] 封面候选抽帧失败（跳过该封面）: %s', e)
                covers.append('')
                continue
            best, best_s = '', float('-inf')
            from PIL import Image
            for k in range(len(ts_list)):
                fp = os.path.join(cand_dir, 'cand_%02d.jpg' % k)
                if not os.path.exists(fp) or os.path.getsize(fp) < 1024:
                    continue
                try:
                    im = Image.open(fp)
                    s = float(_w._cover_score(im))
                except Exception:
                    continue
                if s > best_s:
                    best, best_s = fp, s
            if not best:
                covers.append('')
                continue
            out = os.path.join(run_dir, _cover_name(i + 1, clips[i]['start']))
            try:
                from PIL import Image as _I
                im = _I.open(best).convert('RGB')
                if aspect in ('crop', 'blur'):
                    # 封面跟随输出画幅：居中裁 9:16，发抖音不用再手动裁
                    w, h = im.size
                    cw = min(w, int(h * 9 / 16))
                    if cw % 2:
                        cw -= 1
                    x0 = (w - cw) // 2
                    im = im.crop((x0, 0, x0 + cw, h))
                im.save(out, 'JPEG', quality=88)
                covers.append(out)
            except Exception as e:
                _log.info('[LIVE] 封面落盘失败: %s', e)
                covers.append('')
    finally:
        try:
            import shutil as _sh
            _sh.rmtree(cand_dir, ignore_errors=True)
        except Exception:
            pass
    return covers


def _cover_name(i, start):
    h = int(start // 3600)
    m = int(start % 3600 // 60)
    s = int(start % 60)
    return 'cover%02d_%02dh%02dm%02ds.jpg' % (i, h, m, s)


def _clip_name(i, start):
    """ASCII 文件名（/media/ 路由 + 跨编码安全），中文说明留给报告文本。"""
    h = int(start // 3600)
    m = int(start % 3600 // 60)
    s = int(start % 60)
    return 'clip%02d_%02dh%02dm%02ds.mp4' % (i, h, m, s)


def export_clips(video_path, clips, run_dir, progress=None, aspect='orig'):
    """把高光段剪成独立 mp4（重编码，-ss 前置秒级 seek + 帧精确重编码）。

    aspect：orig / crop / blur（见 _vf_chain）。
    编码契约与 video_render 的中间产物一致（NVENC p1/qp16 或 x264 ultrafast/crf16，
    音轨 aac 160k 44100/2）；HDR 直播录像（少见但存在）套 tonemap 链，失败不阻断。"""
    _w = _host()
    up = (lambda phase, pct: progress.update({'phase': phase, 'pct': pct})
          if progress else (lambda phase, pct: None))
    enc = list(_w.video_encode_args_interim())
    _tm = ''
    _tags = []
    try:
        from video_render import probe_is_hdr, tonemap_available, tonemap_chain, BT709_TAGS
        if probe_is_hdr(video_path) and tonemap_available(video_path):
            _tm = tonemap_chain()
            _tags = list(BT709_TAGS)
    except Exception as _e:
        _log.info('[LIVE] HDR 探测跳过: %s', _e)
    vf = _vf_chain(aspect, _tm)
    outs = []
    for i, c in enumerate(clips):
        up('导出切片 %d/%d' % (i + 1, len(clips)),
           70 + int(25 * i / max(1, len(clips))))
        out = os.path.join(run_dir, _clip_name(i + 1, c['start']))
        cmd = ['-y', '-ss', '%.3f' % c['start'], '-i', video_path,
               '-t', '%.3f' % (c['end'] - c['start'])]
        if vf.startswith('[0:v]'):
            # 带 [标签] 的完整 filtergraph：走 -filter_complex 并显式映射视频/音频
            cmd += ['-filter_complex', vf, '-map', '[vout]', '-map', '0:a?']
        else:
            cmd += ['-vf', vf]
        cmd += enc
        if _tm:
            cmd += _tags
        cmd += ['-c:a', 'aac', '-b:a', '160k', '-ar', '44100', '-ac', '2',
                '-movflags', '+faststart', out]
        rc, _o, e = ffmpeg_run(cmd)
        if rc != 0 or not os.path.exists(out) or os.path.getsize(out) < 10240:
            raise RuntimeError('切片 %d 导出失败: %s'
                               % (i + 1, e.decode('utf-8', 'ignore')[-200:]))
        outs.append(out)
    return outs


def validate_segments(segs, vdur, min_sec=3.0, max_sec=600.0, max_count=20):
    """半手动路线：把用户调整后的段落清单清洗净化成可导出形态。

    规则：数值转 float、钳制到 [0, vdur]、start<end 且时长 ≥ min_sec（防手滑出
    1 秒碎片）、上限 max_sec（防把整场录像当一条）、按时间排序、最多 max_count 条。
    无效段直接丢弃（不报错——用户在界面上的每次删改都不该被一行坏数据卡死）。
    返回 [{'start','end','dur'}]。"""
    out = []
    for s in (segs or []):
        try:
            a = float(s.get('start'))
            b = float(s.get('end'))
        except (TypeError, ValueError, AttributeError):
            continue
        a = max(0.0, min(a, vdur))
        b = max(0.0, min(b, vdur))
        if b - a < min_sec:
            continue
        if b - a > max_sec:
            b = a + max_sec
        out.append({'start': round(a, 2), 'end': round(b, 2),
                    'dur': round(b - a, 2)})
    out.sort(key=lambda c: c['start'])
    return out[:max_count]


def carry_reasons(segs, draft_clips, overlap_ratio=0.5):
    """半手动路线：调整后的段落尽量继承原候选的分数/理由（按重叠占比匹配）。

    重叠 ≥ 50% 且边界没动（±1 秒内）→ 原样继承；动了边界 → 继承理由并标注
    「已调整」；完全对不上（用户自己圈的段）→ 标「手动指定」。"""
    res = []
    for s in segs:
        best, best_ol = None, 0.0
        for d in (draft_clips or []):
            ol = min(s['end'], float(d.get('end') or 0)) - max(s['start'], float(d.get('start') or 0))
            if ol > best_ol:
                best, best_ol = d, ol
        if best is not None and best_ol / max(1e-6, s['dur']) >= overlap_ratio:
            reasons = list(best.get('reasons') or [])
            score = best.get('score')
            untouched = (abs(s['start'] - float(best.get('start') or 0)) < 1.0
                         and abs(s['end'] - float(best.get('end') or 0)) < 1.0)
            if not untouched:
                reasons = list(reasons) + ['（边界已手动调整）']
                score = round(float(score or 0) * 0.9, 3)
            res.append({'score': score, 'reasons': reasons or ['手动指定']})
        else:
            res.append({'score': '', 'reasons': ['手动指定']})
    return res


def save_draft(run_dir, data):
    """阶段①分析结果落草稿（阶段②按它导出；也天然支持中断后续跑）。"""
    fp = os.path.join(run_dir, 'live_clip_draft.json')
    with open(fp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    return fp


def load_draft(run_dir):
    fp = os.path.join(run_dir, 'live_clip_draft.json')
    if not os.path.exists(fp):
        return None
    try:
        with open(fp, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def write_report(meta, clips, files, run_dir, covers=None, aspect='orig'):
    """落 report.json（机器可读）+ 切片报告.txt（人可读）。返回 (json_rel, txt_rel)。"""
    covers = covers or []
    _aspect_names = {'orig': '原始横屏', 'crop': '竖屏 9:16（居中裁切）',
                     'blur': '竖屏 9:16（全景+虚化背景）'}
    items = []
    for k, (c, f) in enumerate(zip(clips, files)):
        cov = covers[k] if k < len(covers) else ''
        items.append({'file': os.path.basename(f), 'start': c['start'],
                      'end': c['end'], 'dur': c.get('dur'),
                      'score': c.get('score'), 'peak_t': c.get('peak_t'),
                      'reasons': c.get('reasons') or [],
                      'cover': os.path.basename(cov) if cov else ''})
    data = {'video': os.path.basename(meta.get('video') or ''),
            'video_dur': meta.get('vdur'), 'window': meta.get('window'),
            'asr_used': meta.get('asr_used'),
            'aspect': _aspect_names.get(aspect, aspect),
            'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'clips': items}
    jp = os.path.join(run_dir, 'live_clip_report.json')
    with open(jp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    lines = ['直播切片报告（%s）' % data['generated_at'],
             '源片：%s（时长 %.1f 分钟）｜输出画幅：%s'
             % (data['video'], (meta.get('vdur') or 0) / 60.0, data['aspect']),
             '共 %d 条切片：' % len(items)]
    for i, it in enumerate(items):
        cov = ('（封面 %s）' % it['cover']) if it['cover'] else ''
        lines.append('%d. %s  第%.0f秒~第%.0f秒（%d 秒）  %s%s'
                     % (i + 1, it['file'], it['start'], it['end'], int(it['dur'] or 0),
                        '；'.join(it['reasons']), cov))
    lines.append('切片为保留原声的独立 mp4（含同名封面 jpg），可直接发短视频平台。')
    tp = os.path.join(run_dir, 'live_clip_report.txt')
    with open(tp, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lines) + '\n')
    return os.path.basename(jp), os.path.basename(tp)
