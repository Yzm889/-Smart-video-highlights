# -*- coding: utf-8 -*-
"""分镜/卡点分析引擎（由 webui_server.py 拆出）。

场景切点检测、帧信号分析、音乐鼓点匹配、卡点剪辑。
全部本地计算（ffmpeg scene + librosa + numpy），不花 API 钱。
约定与 bili_downloader.py 一致：宿主符号经 _w.<name> 晚绑定，
本模块公共符号在文件末尾注入回宿主命名空间。
"""
import logging, os, re, sys, time, random, subprocess as _sp
import numpy as np

from cache_utils import ANALYSIS_VERSION, _file_fp, _analysis_cache_load, _analysis_cache_save
from ffmpeg_utils import AbortError, PROGRESS, RUN_PROCS, _PROC_LOCK, _TLS, ffmpeg_exe, ffmpeg_run, probe_audio_len, _has_audio_track
from ai_providers import _aborted

_log = logging.getLogger('framecut.beat')

# ---- 宿主晚绑定 ----
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


_w = _host()

# ---------------------------------------------------------------------------
# 分析帧上限常量
# ---------------------------------------------------------------------------
_ANALYZE_MAX_SIDE = 640     # 分析帧长边上限（1080p 一帧 ~6MB → 640p ~0.7MB，管道 I/O 大减）
_ANALYZE_MAX_FRAMES = 1800  # 单次分析总帧数上限，超出按视频时长自适应降 fps


# ---------------------------------------------------------------------------
# 🗂 分析缓存：场景切点/帧信号按「文件指纹+参数+分析版本」落盘复用，避免重复全片解码。
# 收益：人机协同反复调 strength/maxCuts 重看方案从分钟级变秒级；
#       卡点+解说连续作业共享同一套场景切点（解说分段不再单独全片再扫一遍）。
# 原则：任何缓存读写失败都静默回退实时分析，缓存只影响速度、不影响正确性。
# ---------------------------------------------------------------------------

def _cached_scene_cuts(video_path, threshold=0.30, progress=None):
    """场景切点（带磁盘缓存）。键含阈值+版本+文件指纹：改参数、换文件、
    文件内容被替换（size/mtime 变化）都会自动重新分析。"""
    fp = _file_fp(video_path)
    if not fp:
        return detect_scene_cuts(video_path, threshold=threshold)
    key = f'scene_v{ANALYSIS_VERSION}_th{threshold:g}_{fp}'
    hit = _analysis_cache_load(key)
    if isinstance(hit, list):
        return [float(x) for x in hit]
    kw = {'progress': progress} if progress is not None else {}
    cuts = detect_scene_cuts(video_path, threshold=threshold, **kw)
    _analysis_cache_save(key, cuts)   # 空结果也缓存：确属「无切点」的视频不必反复扫
    return cuts


def _cached_frame_signals(video_path, fps_s=4.0, progress=None):
    """帧信号（带磁盘缓存）：缓存 _analyze_video_frames 的信号 dict。
    键用「实际生效的 fps」（长视频会被 _adaptive_fps 降帧），避免键名与缓存内容不一致。"""
    fp = _file_fp(video_path)
    if not fp:
        return _analyze_video_frames(video_path, fps_s=fps_s, progress=progress)
    fps_eff = _adaptive_fps(probe_audio_len(video_path) or 0.0, fps_s)
    key = f'frames_v{ANALYSIS_VERSION}_fps{fps_eff:g}_{fp}'
    hit = _analysis_cache_load(key)
    if isinstance(hit, dict) and hit.get('times'):
        return hit
    an = _analyze_video_frames(video_path, fps_s=fps_eff, progress=progress)
    if an and an.get('times'):
        _analysis_cache_save(key, an)
    return an


# ---------------------------------------------------------------------------
# 🎯 智能强卡点引擎：场景切换/动作停顿帧 ↔ 音乐大鼓点 匹对
# 全部本地计算（ffmpeg scene 检测 + librosa 强拍检测 + numpy 帧差），不花 API 钱
# ---------------------------------------------------------------------------
def detect_scene_cuts(video_path, threshold=0.30, progress=None):
    """用 ffmpeg scene 滤镜检测视频场景切换点，返回切点秒列表（升序）。
    progress（可选 dict）：场景解码阶段按 time= 统计平滑推进 pct（5→24）。"""
    vdur = probe_audio_len(video_path) or 0.0

    def on_progress(sec):
        if progress and vdur > 0:
            progress['phase'] = '检测场景切换（全片解码）'
            progress['pct'] = min(24, 5 + int(sec * 19 / vdur))

    rc, out, err = ffmpeg_run(['-hide_banner', '-i', video_path,
                               '-vf', f"select='gt(scene,{threshold})',showinfo",
                               '-an', '-f', 'null', '-'], on_progress=on_progress)
    cuts = []
    for m in re.finditer(r'pts_time:([0-9.]+)', err.decode('utf-8', 'ignore')):
        t = float(m.group(1))
        if t > 0.3:
            cuts.append(round(t, 3))
    cuts = sorted(set(cuts))
    return cuts


# ---------------------------------------------------------------------------
# 长视频抽帧保护：总帧数封顶 + 分析帧长边降采样。
# 依据：下游阈值全是分位数/IQR 相对阈值(_detect_motion_from_frames/_detect_visual_from_frames)，
# 分辨率缩放不影响检测；切点最小间隔 min_gap 0.5~0.6s，fps≥2 的时间粒度足够。
# ---------------------------------------------------------------------------

def _scaled_dims(w, h, max_side=_ANALYZE_MAX_SIDE):
    """把分析帧按长边等比压到 max_side 以内（宽高取偶），已达标则原样返回。"""
    w, h = int(w), int(h)
    m = max(w, h)
    if m <= max_side or m <= 0:
        return w, h
    f = max_side / float(m)
    w2 = max(2, int(w * f) // 2 * 2)
    h2 = max(2, int(h * f) // 2 * 2)
    return w2, h2


def _adaptive_fps(duration_s, fps_s=4.0, max_frames=_ANALYZE_MAX_FRAMES):
    """长视频自适应降 fps：总帧数 ≤ max_frames 封顶（下限 0.5——1 小时视频恰为 1800 帧；
    0.5s 粒度对切点候选足够，精确切点由独立的场景检测全片解码承担）。
    短视频（≤max_frames/fps_s 秒）与未知时长不受影响，fps 原样透传。"""
    if not duration_s or duration_s <= 0:
        return fps_s
    return round(min(float(fps_s), max(0.5, max_frames / float(duration_s))), 3)


def _analyze_video_frames(video_path, fps_s=4.0, progress=None):
    """流式抽帧，一次管道同时算 4 类信号：
       motion(均值帧差) / frac(像素变化比例,更灵敏) / hist(色相直方图突变) / bright(亮度)。
    返回 dict：{times, motion, frac, hist, bright}（等长，首帧 motion/frac/hist 为 0）。
    长视频保护：复用同一次探测的 stderr 顺带解析时长，自动降 fps 封顶总帧数，
    并把分析帧长边压到 _ANALYZE_MAX_SIDE（不加参数、不多跑一次 ffmpeg）。"""
    exe = ffmpeg_exe()
    rc, out, err = ffmpeg_run(['-i', video_path])
    err_text = err.decode('utf-8', 'ignore')
    m = re.search(r'(\d{2,4})x(\d{2,4})', err_text)
    if not m:
        return None
    md = re.search(r'Duration:\s*(\d+):(\d{2}):(\d+(?:\.\d+)?)', err_text)
    vdur = (int(md.group(1)) * 3600 + int(md.group(2)) * 60 + float(md.group(3))) if md else 0.0
    fps_s = _adaptive_fps(vdur, fps_s)
    w, h = _scaled_dims(int(m.group(1)), int(m.group(2)))
    expected = max(1, int(vdur * fps_s))
    fb = w * h * 3  # rgb24（降采样后）
    if progress:
        progress['phase'] = '检测动作/镜头切换（逐帧分析）'
        progress['pct'] = 25
    proc = _sp.Popen([exe, '-hide_banner', '-i', video_path, '-vf', f'fps={fps_s},scale={w}:{h}',
                      '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-vcodec', 'rawvideo', '-'],
                     stdout=_sp.PIPE, stderr=_sp.DEVNULL)
    # 注册到 RUN_PROCS：/api/cancel 才能真的把这个进程杀掉。
    # 旧实现是裸 Popen，长视频全片解码要跑数分钟到数十分钟，期间「取消」按钮完全无效。
    _runid = getattr(_TLS, 'runid', None)
    if _runid:
        with _PROC_LOCK:
            RUN_PROCS[_runid] = proc
    times, motion, frac, hist, bright = [], [], [], [], []
    prev_gray = None
    prev_hist = None
    t = 0.0
    try:
        while True:
            if _runid and PROGRESS.get(_runid, {}).get('abort'):
                break
            data = proc.stdout.read(fb)
            if not data or len(data) < fb:
                break
            rgb = np.frombuffer(data, dtype=np.uint8).reshape(h, w, 3)
            gray = rgb.astype(np.float32).mean(axis=2)
            hcat = []
            for ch in range(3):
                hh, _ = np.histogram(rgb[:, :, ch], bins=16, range=(0, 256))
                hcat.append(hh)
            hcat = np.concatenate(hcat).astype(np.float32)
            hsum = float(hcat.sum())
            if hsum > 0:
                hcat /= hsum
            if prev_gray is not None:
                adiff = np.abs(gray - prev_gray)
                motion.append(float(adiff.mean()))
                frac.append(float((adiff > 15.0).mean()))
                hist.append(float(np.abs(hcat - prev_hist).sum()))
            else:
                motion.append(0.0)
                frac.append(0.0)
                hist.append(0.0)
            prev_gray = gray
            prev_hist = hcat
            bright.append(float(gray.mean()))
            times.append(t)
            t += 1.0 / fps_s
            if progress and len(times) % 20 == 0:   # 每 20 帧推进一次，长视频不再长期停在固定百分比
                progress['pct'] = min(44, 25 + int(len(times) * 19 / expected))
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        # kill 之后必须 wait，否则进程句柄残留成僵尸直到 Popen 被 GC
        try:
            proc.stdout.close()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        if _runid:
            with _PROC_LOCK:
                RUN_PROCS.pop(_runid, None)
    return {'times': times, 'motion': motion, 'frac': frac, 'hist': hist, 'bright': bright}


def _detect_motion_from_frames(an, min_gap=0.6, strength='standard'):
    """从已抽帧信号(an=_analyze_video_frames 返回值)检测动作/停顿候选切点。
    供 _analyze_beatcut 一次抽帧同时算动作+视觉，避免重复全片解码。"""
    if not an or len(an['frac']) < 5:
        return []
    times, frac = an['times'], an['frac']
    vals = frac[1:]
    if not vals:
        return []
    p25, p75 = [float(x) for x in np.percentile(vals, [25, 75])]
    iqr = p75 - p25
    th = max(p75 + 1.5 * iqr, 0.004)   # 相对阈值，几乎无绝对下限
    if strength == 'soft':
        th *= 1.6
    elif strength == 'strong':
        th *= 0.5
    peaks = []
    pauses = []
    for i in range(1, len(frac) - 1):
        if frac[i] > th and frac[i] >= frac[i - 1] and frac[i] >= frac[i + 1]:
            peaks.append(times[i])
        if frac[i] < p25 * 0.9 and frac[i] <= frac[i - 1] and frac[i] <= frac[i + 1]:
            pauses.append(times[i])
    cands = sorted(set([round(x, 2) for x in peaks + pauses]))
    out = []
    last = -1e9
    for c in cands:
        if c - last >= min_gap:
            out.append(c)
            last = c
    return out


def _detect_visual_from_frames(an, strength='standard'):
    """从已抽帧信号(an=_analyze_video_frames 返回值)检测镜头/色调/亮度切换候选切点。"""
    if not an or len(an['hist']) < 5:
        return []
    times, hist, bright = an['times'], an['hist'], an['bright']
    hv = hist[1:]
    th_h = max(float(np.percentile(hv, 80)) * 0.8, 0.05)
    bd = [abs(bright[i] - bright[i - 1]) for i in range(1, len(bright))]
    th_b = max(float(np.percentile(bd, 90)) * 0.7, 8.0)
    if strength == 'soft':
        th_h *= 1.6
        th_b *= 1.5
    elif strength == 'strong':
        th_h *= 0.5
        th_b *= 0.6
    cands = []
    for i in range(1, len(times)):
        if hist[i] > th_h or bd[i - 1] > th_b:
            cands.append(times[i])
    out = []
    last = -1e9
    for c in cands:
        if c - last >= 0.5:
            out.append(round(c, 2))
            last = c
    return out


def detect_motion_points(video_path, fps_s=4.0, min_gap=0.6, strength='standard'):
    """帧级动作检测：用像素变化比例(frac)做信号，分位数自适应阈值 + 动作峰 + 停顿帧。
    strength: soft(柔和)/standard/strong(强力)。返回候选切点秒列表。"""
    return _detect_motion_from_frames(_analyze_video_frames(video_path, fps_s), min_gap, strength)


def detect_visual_cues(video_path, fps_s=4.0, strength='standard'):
    """帧级视觉线索：色相直方图突变(镜头/色调切换) + 亮度突变(曝光/场景切换)。"""
    return _detect_visual_from_frames(_analyze_video_frames(video_path, fps_s), strength)


def _music_onset_peaks(music_path, delta=0.18, wait=0.25, sr=22050, hop=512):
    """librosa onset 峰值检测公共层：「智能强卡点」与「节拍同步」两引擎共用同一套
    加载+onset 包络+峰值挑选，避免两份逐行重复的 librosa 代码各自漂移。
    返回 (峰值秒列表, 峰值onset强度列表, 音乐总时长)；librosa 不可用/失败返回 ([], [], 0.0)。
    注意：librosa peak_pick 的 wait 单位是「帧」不是秒（hop=512@22050Hz ≈ 23ms/帧）。"""
    try:
        import librosa
    except Exception:
        return [], [], 0.0
    try:
        y, sr = librosa.load(music_path, sr=sr, mono=True)
        if len(y) < sr:
            return [], [], 0.0
        T = float(len(y)) / sr
        onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
        times = librosa.times_like(onset_env, sr=sr, hop_length=hop)
        peaks = librosa.util.peak_pick(onset_env, pre_max=8, post_max=8, pre_avg=8,
                                       post_avg=8, delta=delta, wait=wait)
        pts = [float(times[p]) for p in peaks]
        vals = [float(onset_env[p]) for p in peaks]
        return pts, vals, T
    except Exception:
        return [], [], 0.0


def detect_strong_beats(music_path, top_k=None, min_sep=0.25):
    """检测音乐"大鼓点"（强 onset 峰值）。返回(强拍秒列表升序, 每秒拍数估计)。
    强拍用「时间窗分桶」挑选而非全局最强 top_k：否则最强 onset 会集中在音乐前段，
    视频后段没有强拍可吸附，导致卡点全部落在片头、后半段完全踩不上鼓点。"""
    pts, vals, T = _music_onset_peaks(music_path, delta=0.18, wait=min_sep)
    if not pts:
        return [], None
    # 时间窗分桶：把整段音乐均分 top_k 个窗，每窗取局部最强 onset → 强拍均匀覆盖全曲
    if top_k and len(pts) > top_k:
        picked = []
        seen = set()
        win = T / top_k
        for k in range(top_k):
            w0, w1 = k * win, (k + 1) * win
            best_i, best_v = -1, -1.0
            for i in range(len(pts)):
                if pts[i] < w0 or pts[i] >= w1:
                    continue
                if vals[i] > best_v:
                    best_v, best_i = vals[i], i
            if best_i < 0:
                # 空窗（该段音乐平缓无 onset）：不强凑，交给相邻窗
                continue
            if pts[best_i] in seen:
                continue
            picked.append(pts[best_i])
            seen.add(pts[best_i])
        if picked:
            pts = sorted(picked)
    # estimate beats-per-second from inter-peak median
    gaps = [pts[i+1] - pts[i] for i in range(len(pts) - 1) if pts[i+1] - pts[i] > 0.2]
    bps = None
    if gaps:
        bps = 1.0 / float(np.median(gaps))
    return pts, bps


def plan_beat_cuts(scene_cuts, motion_cuts, beats, video_dur, min_seg=None, max_seg=9.0, tol=0.35,
                   visual_cuts=None, strength='standard', max_cuts=None, skip_head=0.0):
    """把视频切点(场景+动作+视觉线索)加权匹对到最近强拍，生成强卡点时间线。
    权重：场景切>动作>视觉线索；强力模式更严格吸附强拍；段过长时用强拍网格兜底。
    - min_seg 默认按强度自适应（soft 1.5 / standard 1.2 / strong 1.0），避免 1 秒碎切闪屏；
    - max_cuts 限制最终段数（约 3.5s/段，默认最多 48）：切点超量时均匀抽稀保留首尾，
      否则卡点会切成几十个 1 秒碎段，观感像画面反复跳。"""
    if not beats:
        beats = []
    if min_seg is None:
        min_seg = {'soft': 1.5, 'standard': 1.2, 'strong': 1.0}.get(strength, 1.2)
    if max_cuts is None:
        max_cuts = max(6, min(48, int(video_dur / 3.5)))
    # weighted candidates
    cand = []
    for c in (scene_cuts or []):
        cand.append((round(float(c), 2), 3))
    for c in (motion_cuts or []):
        cand.append((round(float(c), 2), 2))
    for c in (visual_cuts or []):
        cand.append((round(float(c), 2), 1))
    merged = {}
    for c, w in cand:
        if c in merged:
            merged[c] = max(merged[c], w)
        else:
            merged[c] = w
    # 候选过多时先控制规模：按权重优先 + 时间均匀去重
    if len(merged) > max_cuts * 3:
        keep = []
        ranked = sorted(merged.items(), key=lambda kv: (-kv[1], kv[0]))
        step = video_dur / max_cuts
        last = -1e9
        for c, w in ranked:
            if c - last >= step * 0.9:
                keep.append(c)
                last = c
        if len(keep) < max_cuts:
            keys = sorted(merged.keys())
            keep = keys[::max(1, len(keys) // max_cuts)][:max_cuts]
        merged = {c: merged[c] for c in sorted(keep)}
    cuts = []
    used = set()
    eff_tol = tol * 0.7 if strength == 'strong' else (tol * 1.4 if strength == 'soft' else tol)
    for c in sorted(merged):
        w = merged[c]
        if c < max(0.3, skip_head) or c >= video_dur - 0.3:
            continue
        best = min(beats, key=lambda b: abs(b - c)) if beats else None
        target = best if (best is not None and abs(best - c) <= eff_tol) else c
        if any(abs(target - u) < min_seg for u in used):
            continue
        cuts.append(round(target, 3))
        used.add(target)
    cuts.sort()
    timeline = [0.0]
    for c in cuts:
        if c - timeline[-1] >= min_seg and video_dur - c >= min_seg * 0.6:
            timeline.append(c)
    # 强拍网格兜底：某段过长时，在中点附近最近的强拍补切
    if beats:
        i = 1
        guard = 0
        while i < len(timeline) and guard < 200:
            guard += 1
            if timeline[i] - timeline[i - 1] > max_seg:
                mid = (timeline[i] + timeline[i - 1]) / 2.0
                nb = min(beats, key=lambda b: abs(b - mid))
                if timeline[i - 1] + min_seg < nb < timeline[i] - min_seg:
                    timeline.insert(i, round(nb, 3))
                    continue
            i += 1
    # 段数超限 → 均匀抽稀（保留首尾），避免几十个 1 秒碎段闪屏
    if len(timeline) - 1 > max_cuts:
        n = len(timeline) - 1
        keep_idx = sorted(set([0, n] + [int(round(j * n / max_cuts)) for j in range(1, max_cuts)]))
        timeline = [timeline[i] for i in keep_idx]
        tl2 = [timeline[0]]
        for c in timeline[1:]:
            if c - tl2[-1] >= min_seg * 0.6:
                tl2.append(c)
        if len(tl2) > 1 and video_dur - tl2[-1] < 0.4:
            tl2.pop()
        timeline = tl2
    # 同场景去重：相邻片段之间无场景切换且都短于 min_seg*1.5 → 合并（避免同一场景反复切）
    if scene_cuts and len(timeline) > 2:
        sc_set = set(round(float(s), 1) for s in scene_cuts)
        merged = [timeline[0]]
        for i in range(1, len(timeline)):
            seg_dur = timeline[i] - merged[-1]
            # 该切点附近有无场景切换（±0.5s）
            has_scene = any(abs(timeline[i] - sc) <= 0.5 for sc in sc_set)
            if seg_dur < min_seg * 1.5 and not has_scene:
                continue  # 跳过此切点，合并到前一段
            merged.append(timeline[i])
        timeline = merged
    timeline.append(video_dur)
    return timeline


def _analyze_beatcut(video_path, music_path, params, progress=None):
    """卡点分析阶段：场景/动作/视觉线索 + 音乐大鼓点 → 切点时间线。
    拆出供「人机协同」复用：用户可在预览界面增删切点后再按自己的时间线渲染。"""
    def up(ph, pct):
        if progress:
            progress['phase'] = ph; progress['pct'] = pct
    strength = params.get('strength', 'standard')
    vdur = probe_audio_len(video_path) or 0.0
    if vdur <= 0:
        raise RuntimeError('无法读取视频时长')
    # maxCuts 服务端钳制 3~96：API/指令路径可绕过前端滑条范围，极端值会生成上百段拖垮渲染
    max_cuts = max(3, min(96, int(params.get('maxCuts', 30) or 30)))
    up('检测场景切换', 5)
    scene_cuts = _cached_scene_cuts(video_path, threshold=float(params.get('sceneTh', 0.30)), progress=progress)
    up('检测动作/镜头切换', 25)
    # 一次抽帧同时算动作+视觉线索（避免两次全片解码）；结果带磁盘缓存，重跑方案秒级返回
    frames = _cached_frame_signals(video_path, 4.0, progress=progress)
    motion_cuts = _detect_motion_from_frames(frames, strength=strength)
    visual_cuts = _detect_visual_from_frames(frames, strength=strength)
    up('分析音乐大鼓点', 45)
    strong_beats, bps = detect_strong_beats(music_path, top_k=max_cuts)
    skip_head = max(0.0, min(30.0, float(params.get('skipHead', 3.0) or 3.0)))
    timeline = plan_beat_cuts(scene_cuts, motion_cuts, strong_beats, vdur,
                              visual_cuts=visual_cuts, strength=strength,
                              max_cuts=max_cuts, skip_head=skip_head)
    diag = {
        'scene_cuts': scene_cuts,
        'motion_cuts': motion_cuts,
        'visual_cuts': visual_cuts,
        'strong_beats': strong_beats[:40],
        'timeline': timeline,
        'segments': len(timeline) - 1,
        'strength': strength,
    }
    return timeline, diag, vdur


def _render_beatcut(video_path, music_path, timeline, params, run_dir, progress=None, diag=None, pct_base=30):
    """卡点渲染阶段：按给定切点时间线切片→拼接(硬切/转场)→配乐(纯音乐/保留原声)。返回 final 路径。
    pct_base：进度起点——直连生成时分析阶段已占 0~50，渲染从 50 起（避免进度回跳）；
    人机协同确认渲染没有分析阶段，维持默认 30。"""
    def up(ph, pct):
        if progress:
            progress['phase'] = ph; progress['pct'] = pct
    span0, span1 = pct_base, 95

    def rp(frac):
        return int(span0 + (span1 - span0) * frac)

    nseg = max(1, len(timeline) - 1)
    up('按鼓点切片', rp(0.0))
    segs = []
    for i in range(len(timeline) - 1):
        if _aborted():
            raise AbortError('用户取消了任务')
        up('按鼓点切片 %d/%d' % (i + 1, nseg), rp((i + 1) / nseg * 0.6))
        seg = os.path.join(run_dir, f'bc{i}.mp4')
        segs.append(seg)
        seg_dur = timeline[i + 1] - timeline[i]
        # 必须传 start=timeline[i]：否则每段都从源视频 0 秒截取，片头画面反复出现
        for attempt in range(2):
            try:
                _w.make_video_clip(video_path, seg_dur, seg,
                                w=int(params.get('w', _w.W)), h=int(params.get('h', _w.H)), fps=int(params.get('fps', 30)),
                                start=timeline[i])
                break
            except RuntimeError:
                if attempt == 0:
                    time.sleep(0.5)
                    continue
                raise
    # 拼接：有转场 → xfade 链（重编码）；否则 concat demuxer 免重编码（快），失败兜底 filter concat
    transition = params.get('transition') or 'none'
    fade_dur = min(0.6, max(0.1, float(params.get('transDur', 0.2))))
    silent = os.path.join(run_dir, 'bc_silent.mp4')
    if transition and transition != 'none':
        up('按鼓点拼接（转场 %s）' % transition, 32)
        inputs = ['-y']
        for s in segs:
            inputs += ['-i', s]
        durs = [probe_audio_len(s) or max(0.3, timeline[i + 1] - timeline[i]) for i, s in enumerate(segs)]
        fc_parts = []
        prev = '[0:v]'
        cum = 0.0
        n = len(segs)
        for i in range(1, n):
            cum += durs[i - 1]
            off = cum - i * fade_dur
            label = 'v%d' % i
            fc_parts.append('%s[%d:v]xfade=transition=%s:duration=%.3f:offset=%.3f[%s]'
                            % (prev, i, transition, fade_dur, max(0.0, off), label))
            prev = '[%s]' % label
        fc_parts.append('%sformat=yuv420p[vo]' % prev)
        fc = ';'.join(fc_parts)
        cmd = inputs + ['-filter_complex', fc, '-map', '[vo]',
                        ] + _w.video_encode_args() + ['-threads', '0', '-r', str(int(params.get('fps', 30))), silent]
        rc, o, e = ffmpeg_run(cmd)
    else:
        up('按鼓点硬切拼接', 30)
        concat_txt = os.path.join(run_dir, 'bc.concat.txt')
        with open(concat_txt, 'w', encoding='utf8') as f:
            for s in segs:
                f.write("file '%s'\n" % s.replace("'", "'\\''"))
        try:
            rc, o, e = ffmpeg_run(['-y', '-f', 'concat', '-safe', '0', '-i', concat_txt,
                                   '-c', 'copy', silent])
        finally:
            if os.path.exists(concat_txt):
                try:
                    os.unlink(concat_txt)
                except Exception:
                    pass
        if rc != 0:
            # 个别段编码参数不一致导致 copy 失败 → 兜底重编码拼接
            parts = ''.join(f'[{i}:v]' for i in range(len(segs)))
            fc = f'{parts}concat=n={len(segs)}:v=1:a=0[vout]'
            cmd = ['-y']
            for s in segs:
                cmd += ['-i', s]
            cmd += ['-filter_complex', fc, '-map', '[vout]'] + _w.video_encode_args() + ['-threads', '0', silent]
            rc, o, e = ffmpeg_run(cmd)
    if rc != 0:
        raise RuntimeError('卡点拼接失败: ' + e.decode('utf-8', 'ignore')[-400:])
    silent_dur = probe_audio_len(silent) or timeline[-1]
    up('合成配乐', 55)
    final = os.path.join(run_dir, 'final.mp4')
    keep_audio = bool(params.get('keepAudio'))
    if keep_audio and _has_audio_track(video_path):
        # 保留原声：原声压低(0.3) + 音乐铺底(0.7)，节奏感与内容兼顾
        # 【任务4】统一 3 秒淡入淡出，视频短于 6 秒减半。
        _fade = 3.0 if silent_dur >= 6.0 else max(0.5, silent_dur / 4.0)
        _fade_out_st = max(0.0, silent_dur - _fade)
        cmd = ['-y', '-i', silent, '-i', video_path, '-stream_loop', '-1', '-i', music_path,
               '-filter_complex',
               "[1:a]aresample=44100,aformat=channel_layouts=stereo,volume=0.3[o];"
               f"[2:a]aresample=44100,aformat=channel_layouts=stereo,volume=0.7,"
               f"afade=t=in:st=0:d={_fade:.2f},afade=t=out:st={_fade_out_st:.2f}:d={_fade:.2f}[bgm];"
               "[o][bgm]amix=inputs=2:normalize=0,aformat=fltp[aout]",
               '-map', '0:v:0', '-map', '[aout]',
               '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
               '-t', '%.2f' % silent_dur, '-movflags', '+faststart', final]
    else:
        # 纯音乐（默认）：音乐铺满视频时长
        # 【任务4】统一 3 秒淡入淡出，视频短于 6 秒减半。
        _fade = 3.0 if silent_dur >= 6.0 else max(0.5, silent_dur / 4.0)
        _fade_out_st = max(0.0, silent_dur - _fade)
        cmd = ['-y', '-stream_loop', '-1', '-i', music_path, '-i', silent,
               '-filter.a', 'afade=t=in:st=0:d=%.2f,afade=t=out:st=%.2f:d=%.2f' % (_fade, _fade_out_st, _fade),
               '-map', '1:v:0', '-map', '0:a:0', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
               '-t', '%.2f' % silent_dur, '-movflags', '+faststart', final]
    rc, o, e = ffmpeg_run(cmd)
    if rc != 0:
        raise RuntimeError('配乐失败: ' + e.decode('utf-8', 'ignore')[-400:])
    if progress:
        progress['done'] = True
        progress['pct'] = 100
        progress['file'] = os.path.relpath(final, _w.OUTDIR).replace('\\', '/')
    if diag is not None:
        diag.update({
            'timeline': timeline,
            'segments': len(timeline) - 1,
            'transition': transition,
            'keep_audio': bool(params.get('keepAudio')),
        })
    return final


def beat_cut_video(video_path, music_path, run_dir, params, progress=None):
    """智能强卡点主流程：分析→对齐→硬切拼接→配乐。返回 final 路径与诊断信息。"""
    timeline, diag, vdur = _analyze_beatcut(video_path, music_path, params, progress)
    final = _render_beatcut(video_path, music_path, timeline, params, run_dir, progress, diag, pct_base=50)
    return final, diag
def detect_beats(audio_path, sensitivity=0.5):
    """检测音乐节拍点(秒，升序)。sensitivity∈(0,1) 越高越灵敏；检测失败返回 []。"""
    # 灵敏度越高 → delta 越小 → 检出越多拍点；wait=15 帧约 0.35s 最小拍间隔
    delta = max(0.02, 0.30 - 0.25 * float(sensitivity))
    pts, _vals, T = _music_onset_peaks(audio_path, delta=delta, wait=15)
    if not pts:
        return []
    return [t for t in pts if t < T - 0.05]


def generate_beat_sync_video(video_path, audio_path, output_path, beat_sensitivity=0.5,
                             min_clip_dur=0.6, progress=None):
    """节拍同步成片：按音乐节拍把源视频切成不重复片段硬接，再混入音乐。
    - 节拍识别失败时自动生成虚拟节拍兜底；
    - 片段池顺序轮转 + 轮空洗牌，避免画面顺序重复；
    - concat demuxer + inpoint/outpoint，免重编码，不闪跳；
    - ffmpeg 出错直接抛异常，绝不返回损坏视频。
    返回 {ok, output, beat_num, clip_num, warning}。"""
    def up(ph, pct):
        if progress is not None:
            progress['phase'] = ph
            progress['pct'] = pct

    video_path = str(video_path)
    audio_path = str(audio_path)
    output_path = str(output_path)
    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    music_dur = probe_audio_len(audio_path)
    src_video_dur = probe_audio_len(video_path)
    if not music_dur or not src_video_dur:
        raise RuntimeError('无法读取音视频时长')

    up('检测节拍', 8)
    beat_times = detect_beats(audio_path, sensitivity=beat_sensitivity)
    min_beat_gap = 0.35
    filtered = []
    prev = -999
    for t in beat_times:
        if t - prev >= min_beat_gap and t < music_dur:
            filtered.append(round(t, 2))
            prev = t
    beat_times = filtered

    if len(beat_times) < 2:
        # 节拍识别失败/拍点过少 → 虚拟节拍兜底
        virtual_gap = 1.2
        beat_times = []
        cur = virtual_gap
        while cur < music_dur:
            beat_times.append(round(cur, 2))
            cur += virtual_gap
    beat_times = [0.0] + beat_times
    beat_times.append(round(music_dur, 2))
    beat_count = len(beat_times) - 1

    up('切分素材片段', 16)
    clip_pool = []
    # 片段长度需能覆盖最长节拍段：否则每个节拍段的画面只截了 min_clip_dur，
    # 拼接总长会明显短于音乐时长，-shortest 会把音乐结尾砍掉（音画错位）。
    beat_gaps = [beat_times[i + 1] - beat_times[i] for i in range(len(beat_times) - 1)]
    max_gap = max(beat_gaps) if beat_gaps else min_clip_dur
    clip_len = max(float(min_clip_dur), float(max_gap) * 1.2, 1.2)
    # 片段池必须覆盖全片且数量充足：seg_step 取 clip_len 的 50%（片段互相重叠），
    # 否则池子会只有片头 1~2 段（旧 bug：所有节拍段反复复用开头画面，后面内容完全用不上）。
    seg_step = max(float(min_clip_dur), clip_len * 0.5)
    t = 0.0
    while t + clip_len <= src_video_dur:
        end = round(t + clip_len + random.uniform(0, 0.4), 2)
        clip_pool.append((round(t, 2), end))
        t += seg_step
    # 让最后一段尽量覆盖到片尾，避免尾部素材完全用不上
    if clip_pool and src_video_dur - clip_pool[-1][0] > clip_len * 0.8:
        clip_pool.append((round(max(0.0, src_video_dur - clip_len), 2), round(src_video_dur, 2)))
    if not clip_pool:
        # 源视频过短：把整段作为唯一素材片段
        clip_pool.append((0.0, round(src_video_dur, 2)))
    total_clips = len(clip_pool)
    warn_msg = ''
    if total_clips == 0:
        raise RuntimeError('源视频无法提取可用片段，请换更长的源视频')

    assign_list = []
    idx = 0
    clip_indices = list(range(total_clips))
    for i in range(beat_count):
        s_t = beat_times[i]
        e_t = beat_times[i + 1]
        seg_dur = round(e_t - s_t, 2)
        if seg_dur < min_clip_dur:
            seg_dur = min_clip_dur
        if e_t > music_dur:
            e_t = music_dur
            seg_dur = round(e_t - s_t, 2)
        if idx >= len(clip_indices):
            random.shuffle(clip_indices)
            idx = 0
        clip_idx = clip_indices[idx]
        idx += 1
        src_s, src_e = clip_pool[clip_idx]
        # 关键：该节拍段的画面时长 = seg_dur（拍点间隔），从源片段截取 seg_dur 长，
        # 保证拼接总长 ≈ 音乐时长，音乐完整播完、卡点精确对齐。
        out_end = min(src_e, round(src_s + seg_dur, 2))
        assign_list.append({
            'beat_start': s_t,
            'beat_end': e_t,
            'src_start': src_s,
            'src_end': out_end,
            'seg_dur': seg_dur,
        })
    if beat_count > total_clips:
        warn_msg = '节拍点%d个，可用素材片段%d个，部分画面会循环复用' % (beat_count, total_clips)

    up('按节拍拼接', 40)
    base = os.path.splitext(output_path)[0]
    concat_txt = base + '.concat.txt'
    with open(concat_txt, 'w', encoding='utf8') as f:
        for item in assign_list:
            safe_path = video_path.replace("'", "'\\''")
            f.write("file '%s'\n" % safe_path)
            f.write('inpoint %s\n' % item['src_start'])
            f.write('outpoint %s\n' % item['src_end'])

    temp_video = base + '.temp_noaudio.mp4'
    try:
        rc, out, err = ffmpeg_run(['-y', '-f', 'concat', '-safe', '0', '-i', concat_txt,
                                   '-t', str(music_dur)] + _w.video_encode_args() + [temp_video])
        if rc != 0:
            raise RuntimeError('片段拼接失败: ' + err.decode('utf-8', 'ignore')[-1200:])
        up('合成配乐', 80)
        rc, out, err = ffmpeg_run(['-y', '-i', temp_video, '-i', audio_path,
                                   '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-ar', '44100',
                                   '-shortest', output_path])
        if rc != 0:
            raise RuntimeError('音频混流失败: ' + err.decode('utf-8', 'ignore')[-1200:])
    finally:
        if os.path.exists(concat_txt):
            try:
                os.unlink(concat_txt)
            except Exception:
                pass
        if os.path.exists(temp_video):
            try:
                os.unlink(temp_video)
            except Exception:
                pass

    return {'ok': True, 'output': output_path, 'beat_num': beat_count,
            'clip_num': total_clips, 'warning': warn_msg}


# ---------------------------------------------------------------------------
# 回注宿主命名空间：handler.py / tests / workflows 继续经 _w. / S. 访问
# ---------------------------------------------------------------------------
for _sym in ('_cached_scene_cuts', '_cached_frame_signals',
             'detect_scene_cuts', '_scaled_dims', '_adaptive_fps',
             '_analyze_video_frames', '_detect_motion_from_frames', '_detect_visual_from_frames',
             'detect_motion_points', 'detect_visual_cues', '_music_onset_peaks',
             'detect_strong_beats', 'plan_beat_cuts',
             '_analyze_beatcut', '_render_beatcut',
             'beat_cut_video', 'detect_beats', 'generate_beat_sync_video',
             '_ANALYZE_MAX_SIDE', '_ANALYZE_MAX_FRAMES'):
    setattr(_w, _sym, globals()[_sym])
