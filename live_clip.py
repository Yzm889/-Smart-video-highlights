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
from ai_providers import (asr_segments, local_llm_chat, note_asr_model_mismatch,
                          whisper_language, whisper_model_for_language)

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
# 弹幕热度权重（v1.4.61）：观众用弹幕直接投票「哪里好看」，信号价值高于音量
# 之外的任何一路；缺席（没传弹幕）自动把权重匀给其它路，行为与旧版完全一致。
W_DANMAKU = _env_float('LIVE_CLIP_W_DANMAKU', 0.4)
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
    """每 window 秒一个 RMS（0~1 原始值）。pcm 为空/时长不足一个窗口 → 空数组。

    分块计算：一次性 reshape 成 float32 会让 6 小时录像（1.7 亿采样）瞬吃
    700MB+ 内存（16GB 机器正在被显存换页折磨时这就是压死骆驼的一块砖）。"""
    win = max(1, int(rate * window))
    nwin = len(pcm) // win
    if nwin <= 0:
        return np.zeros(0, dtype=np.float32)
    out = np.empty(nwin, dtype=np.float32)
    CH = 512                                   # 每批 512 个窗口（~1024 秒音频）
    for lo in range(0, nwin, CH):
        hi = min(nwin, lo + CH)
        x = pcm[lo * win:hi * win].reshape(hi - lo, win).astype(np.float32) / 32768.0
        out[lo:hi] = np.sqrt((x ** 2).mean(axis=1))
    return out


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


def _line_weight(text):
    """一条台词有多少「信息量」（0~1）：给台词密度信号降权 / 过滤噪音注释。

    【判据是「整条是不是同一个单元在复读」，不是「字种少」】
    噪音段 ASR 输出的是**周期串**：『呃呃』『纵纵』『对对对』『哈哈哈哈』；
    而真实短句『好的』『卧槽666』虽然也短、字种也少，但不是周期串。
    早先按「不同字数 ≤N」判，把『卧槽666』这种真高能台词也打成噪音，回归测试
    直接抓住了（test_speech_profile_counts_coverage_and_keywords）。"""
    t = ''.join(ch for ch in str(text or '')
                if '\u4e00' <= ch <= '\u9fa5' or ch.isalnum())
    n = len(t)
    if n <= 1:
        return 0.0
    for L in range(1, n // 2 + 1):
        if n % L:
            continue
        rep = n // L
        # 单字单元重复 2 次就算（『呃呃』『对对』）；多字单元要 3 次以上
        # （『我不怕我不怕』是真人强调，不算幻觉）
        if L > 1 and rep < 3:
            continue
        if t == t[:L] * rep:
            return 0.12          # 整条 = 同一个单元复读：幻觉，几乎不计分
    return 1.0


def _clean_lines(lines):
    """只留有信息量的台词行（去掉音乐/噪音段的复读幻觉）。"""
    return [x for x in (lines or [])
            if _line_weight((x or {}).get('text')) >= 0.40]


def _speech_profile(asr, nwin, window):
    """每窗口的台词覆盖秒数 + 高能词次数。返回 (density, kw_counts)。

    覆盖秒数按台词的「信息量」加权（见 _line_weight）：噪音段的复读只算几乎
    不计分，避免 ASR 幻觉把静音段抬成高能点。"""
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
        txt = str(seg.get('text', '') or '')
        w = _line_weight(txt)
        if w <= 0:
            continue                     # 连一个字都没有：不是「有人说话」的证据
        for i in range(i0, i1):
            # 每个覆盖窗只累加**窗内实际覆盖秒数**（旧写法把整段时长加给了每个窗，
            # 一条 5 秒台词会让覆盖到的每个窗都 +5，密度曲线直接爆表）
            cov = min(e, (i + 1) * window) - max(s, i * window)
            if cov > 0:
                density[i] += min(cov, window) * w
        if txt:
            kw[i0:i1] += float(min(3, sum(txt.count(w2) for w2 in HL_WORDS))) * w
    return density, kw


def _parse_danmaku_xml(text):
    """解析 B 站直播弹幕 XML（录播姬/直播工具导出的 <d p="时间,模式,...">文本</d>）。

    【v1.4.61 弹幕信号】范文（切片助手演示）的核心优势：按弹幕数量给高光排序
    ——观众用弹幕投票，比音量/画面信号都准。stdlib ElementTree 解析（Python
    3.7.1+ 默认不解析外部实体，无 XXE 风险）；空/坏输入返回 [] 绝不抛。
    返回 [{t: 秒, text: 弹幕文本}]，按时间升序。"""
    if not text or not str(text).strip():
        return []
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(str(text))
    except Exception as e:
        _log.info('[LIVE] 弹幕 XML 解析失败（忽略弹幕信号）: %s', e)
        return []
    out = []
    for d in root.iter('d'):
        p = str(d.get('p') or '')
        parts = p.split(',')
        if not parts or not parts[0]:
            continue
        try:
            t = float(parts[0])
        except ValueError:
            continue
        txt = (d.text or '').strip()
        if txt:
            out.append({'t': max(0.0, t), 'text': txt})
    out.sort(key=lambda x: x['t'])
    _log.info('[LIVE] 弹幕解析: %d 条（时间跨度 %.0f 秒）',
              len(out), (out[-1]['t'] - out[0]['t']) if out else 0.0)
    return out


def _danmaku_profile(events, nwin, window):
    """每 window 秒的弹幕条数曲线（v1.4.61）。仿 _speech_profile 的窗口聚合：
    弹幕时刻 t 落进第 int(t/window) 窗，计数 +1。"""
    curve = np.zeros(nwin, dtype=np.float32)
    for ev in (events or []):
        try:
            t = float(ev.get('t', 0) or 0)
        except (TypeError, ValueError):
            continue
        i = int(t / window)
        if 0 <= i < nwin:
            curve[i] += 1.0
    return curve


def pct_rank(x, ties=False):
    """0~1 全片分位次（相对化核心）：对绝对音量、编码响度差异、分辨率差异免疫。
    常数序列（无声/静态画面）返回全 0.5，避免把「全场一样响」误判成处处峰值。

    ties=True（v1.4.61 弹幕专用）：**相同值给相同排位**（并列取平均名次）。
    弹幕是稀疏计数——90 分钟录像里 95% 的窗口都是 0；stable 排序会把这些
    并列的 0 按索引依次排开，分位次从 0 一路涨到 0.95，片尾「一条弹幕都没有」
    的窗口反而成了全片最高分（实测高光峰被拖到 281s，弹幕密集的 210s 落选）。
    默认 False → 音量/画面/台词三路行为逐位不变。"""
    x = np.asarray(x, dtype=np.float64)
    if x.size == 0:
        return x
    if float(x.max() - x.min()) <= 1e-9:
        return np.full(x.shape, 0.5)
    if ties:
        _uniq, inv, cnts = np.unique(x, return_inverse=True, return_counts=True)
        starts = np.cumsum(cnts) - cnts
        mid = starts + (cnts - 1) / 2.0
        return mid[inv] / max(1, x.size - 1)
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
    # 合并可能产出超长段（两段贴脸高光连成一片，90min 实测出过 124s > max_sec=60）：
    # 以峰值为中心收缩回 max_sec，保住能量最高的部分，兑现「单条最长」的用户承诺。
    for m in merged:
        if m['end'] - m['start'] > max_sec:
            mid = min(max(m['peak_t'], m['start'] + max_sec / 2),
                      m['end'] - max_sec / 2)
            m['start'] = max(0.0, mid - max_sec / 2)
            m['end'] = min(vdur, m['start'] + max_sec)
            m['start'] = max(0.0, m['end'] - max_sec)
    merged.sort(key=lambda c: -c['score'])
    out = merged[:n_clips]
    out.sort(key=lambda c: c['start'])
    return out


def detect_points(score, window, vdur, min_sec=6.0, max_sec=40.0, min_gap=10.0,
                  max_points=30):
    """v1.4.64 精剪路线：把全片**每个值得一看的局部峰**都标出来，而不是只留 N 条。

    与 select_clips 的区别（都是产品决策，不是实现差异）：
      · 数量：最多 max_points 个点（默认 30），不是 5 条；
      · 时长：默认 6~40 秒（快速出片是 20~60 秒），点要短，用户自己延长；
      · 门槛：要求「绝对高度 + 局部显著性」双达标（见下方双闸），中腰部高光
        也保留，但平坦录像里的随机噪声不会被硬凑成注释；
      · 间距：min_gap 默认 10 秒（快速出片 45 秒），允许密集高光挨着出现。

    峰位判定用 ±min_gap/2 窗口的局部极大值 + 全局分位地板，再按分数贪心去重
    （贪心而不是按时间扫描：一条 90 分钟直播里最热的往往集中在几个段落，
    按分数优先能保证先保住真正的高光，后补中腰部）。
    返回 [{start,end,peak_t,score,pct,dur}]，按时间升序，互不重叠。"""
    s = smooth(score, max(3, int(2.5 / max(0.5, float(window)))))
    n = int(s.size)
    if n <= 0 or vdur <= 0:
        return []
    rk = pct_rank(s)
    # 【显著性闸，2026-11-12 合成曲线实测补的】只看「全片前 X%」会把平坦录像里
    # 的随机噪声也标成高能点（实测 15 分钟纯噪声能填满 30 个点、评分还都 80+）。
    # 这里用 (中位数 → P95) 的动态范围自适应定门槛：真高光至少落在该区间前 60%
    # 的位置。P95 会被高光本身抬高，所以吵闹片/安静片是同一套相对标准。
    # 不用「局部突出度」：一场 2 分钟的持续高潮会被邻域中位数吃掉，整段漏标。
    med = float(np.median(s))
    span = max(0.0, float(np.percentile(s, 95)) - med)
    if span < 0.03:                       # 曲线几乎是平的：这段录像没有高光可言
        _log.info('[LIVE] 高能曲线过于平坦（动态范围 %.3f），不生成注释', span)
        return []
    thr = med + 0.40 * span
    half = max(1, int(float(min_gap) / max(0.5, float(window)) / 2.0))
    cand = []
    for i in range(n):
        lo, hi = max(0, i - half), min(n, i + half + 1)
        if s[i] >= thr and s[i] >= float(s[lo:hi].max()) - 1e-9:
            cand.append(i)
    cand.sort(key=lambda i: (-float(s[i]), i))
    kept = []
    gap = max(1.0, float(min_gap))
    for i in cand:
        t = (i + 0.5) * float(window)
        if all(abs(t - (k + 0.5) * float(window)) >= gap for k in kept):
            kept.append(i)
        if len(kept) >= max(1, int(max_points)):
            break
    if not kept:
        return []
    pts = []
    for i in kept:
        floor = float(s[i]) * 0.60
        lo = hi = i
        while (hi - lo) * window < max_sec:
            grow_hi = hi + 1 < n and s[hi + 1] >= floor
            grow_lo = lo - 1 >= 0 and s[lo - 1] >= floor
            if not grow_hi and not grow_lo:
                break
            if grow_hi:
                hi += 1
            if grow_lo and (hi - lo) * window < max_sec:
                lo -= 1
        while (hi - lo) * window < min_sec and (lo > 0 or hi < n - 1):
            if lo > 0:
                lo -= 1
            if (hi - lo) * window < min_sec and hi < n - 1:
                hi += 1
        pts.append({'start': max(0.0, lo * window),
                    'end': min(vdur, (hi + 1) * window),
                    'peak_t': (i + 0.5) * window,
                    'score': round(float(s[i]), 3),
                    'pct': int(round(float(rk[i]) * 100))})
    pts.sort(key=lambda c: c['start'])
    # 扩边会让相邻点贴上：在重叠区中点切开。不合并——两个峰就是两个注释，
    # 合并会把用户想单独保留/删除的高光吞掉。
    for a, b in zip(pts, pts[1:]):
        if a['end'] > b['start']:
            mid = (max(a['start'], b['start']) + min(a['end'], b['end'])) / 2.0
            a['end'] = min(a['end'], max(a['start'] + 1.0, mid))
            b['start'] = max(b['start'], min(b['end'] - 1.0, mid))
    out = []
    for p in pts:
        if p['end'] - p['start'] < 1.0:
            continue
        p['start'] = round(p['start'], 2)
        p['end'] = round(p['end'], 2)
        p['peak_t'] = round(p['peak_t'], 2)
        p['dur'] = round(p['end'] - p['start'], 2)
        out.append(p)
    return out


def analyze_timeline(video_path, run_dir, progress=None, use_asr=True,
                     asr_language=None, danmaku_events=None, max_points=None,
                     max_sec=None, min_gap=None):
    """v1.4.64 精剪阶段①：一条录像 → 全片高能点（注释）+ 热度曲线 + 全量台词。

    与 analyze_highlights 共用 _compute_signals，所以「精剪里看到的高能点」与
    「快速出片挑的片段」是同一套分数，不会出现两条路径结论互相打脸。

    返回：
      points  每个高能点的起止/峰值/评分(0~1)/全片分位(pct 0~100)/理由/台词行
      curve   热度曲线（每 window 秒一点，0~1），前端画时间轴热力底图
      asr     全量台词（右侧注释面板可翻看/搜索，点行跳转）"""
    up = (lambda phase, pct: progress.update({'phase': phase, 'pct': pct})
          if progress else (lambda phase, pct: None))
    sig = _compute_signals(video_path, run_dir, progress=progress, use_asr=use_asr,
                           asr_language=asr_language, danmaku_events=danmaku_events)
    win, vdur = sig['win'], sig['vdur']
    npts = max(5, min(60, int(max_points or 30)))
    mx = max(5.0, min(120.0, float(max_sec or 40.0)))
    mg = max(4.0, min(120.0, float(min_gap or 10.0)))
    up('标记全片高能点', 72)
    points = detect_points(sig['combined'], win, vdur, min_sec=min(6.0, mx),
                          max_sec=mx, min_gap=mg, max_points=npts)
    if not points:
        raise RuntimeError('没检测到高能点（录像可能整段都是静音/静止画面）')
    for p in points:
        pk = int(p['peak_t'] / win)
        p['reasons'] = _reasons(pk, [sig['r_danmaku'], sig['ranks'][0],
                                     sig['r_motion'], sig['r_speech']], sig['kw'],
                                ('弹幕热度', '音量', '画面动作', '台词密度'))
        _raw = _lines_in_range(sig['asr'], p['start'], p['end'])
        p['lines'] = _clean_lines(_raw)
        p['text'] = ' '.join(str(x.get('text') or '') for x in p['lines']).strip()[:200]
        # 有台词但全是噪音复读：注释面板显示「像音乐/掌声」，而不是让用户看『纵纵』
        p['noisy'] = bool(_raw) and not p['lines']
    curve = [round(float(x), 4) for x in sig['combined']]
    _log.info('[LIVE] 精剪高能点: %d 个（曲线 %d 窗，台词 %d 段，弹幕 %s）',
              len(points), len(curve), len(sig['asr']),
              '有' if sig['danmaku'] is not None else '无')
    return {'points': points, 'curve': curve, 'window': win,
            'vdur': round(vdur, 2), 'asr_used': bool(sig['asr']),
            'danmaku_used': bool(sig['danmaku'] is not None),
            'asr': [{'start': s.get('start'), 'end': s.get('end'),
                     'text': s.get('text')} for s in sig['asr']
                    if isinstance(s, dict)]}


def _lines_in_range(asr, start, end, limit=200):
    """v1.4.61 候选台词明细：取与 [start, end) 有重叠的台词（截 limit 条）。

    范文里「事件下方字幕列表可翻看」——主人调段时不用盲猜这段在说什么。
    重叠判定而不是「起点落在段内」：跨边界的长台词也要能看到（切出来会带半句）。
    返回 [{start, end, text}]，坏数据跳过、绝不抛。"""
    out = []
    for s in (asr or []):
        try:
            s0 = float(s.get('start', 0) or 0)
            s1 = float(s.get('end', s0) or s0)
        except (TypeError, ValueError, AttributeError):
            continue
        if s1 > start and s0 < end:
            out.append({'start': round(s0, 2), 'end': round(s1, 2),
                        'text': str(s.get('text') or '')})
            if len(out) >= limit:
                break
    return out


def _reasons(peak_idx, ranks, kw, keywords_names=('音量', '画面动作', '台词密度')):
    """人能看懂的入选理由：只报「全片前 X%」级别的强信号，最多三条。

    默认三路与 ranks 一一对应（既有调用不变）。v1.4.61 的 analyze_highlights
    会显式传 4 元组并把弹幕放**首位**——观众直接投票的高热段，理由必须能被
    看见（parts 只留 3 条，追加在末尾会被音量/画面/台词挤掉）。"""
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


def _asr_cache_path(run_dir):
    return os.path.join(str(run_dir or ''), 'live_clip_asr.json')


# 台词缓存格式/清洗规则版本：文本后处理（复读折叠、垃圾字符清理）改了就要 +1，
# 否则旧缓存里的脏台词会一直「命中」下去——重跑也不会变干净。
_ASR_CACHE_VER = 2


def _asr_signature(asr_language=None):
    """v1.4.64：本次转写的「语言 + 模型」指纹，用来校验台词缓存。

    【为什么必须校验】缓存只按片名+时长认片，换模型/换语言不识别的还是同一片，
    旧缓存就会被当命中直接复用。实测踩过：默认 distil-large-v3 把中文整片转成
    英文幻觉并已落盘缓存，切到多语言模型重跑，因为「缓存命中」一条都没重转，
    界面上看起来「改了配置也没用」。指纹对不上就作废旧缓存，重新转写。"""
    lang = str(asr_language or '').strip().lower()
    if lang in ('', 'auto'):
        try:
            lang = str(whisper_language() or 'auto').strip().lower()
        except Exception:
            lang = 'auto'
    eff = None if lang in ('', 'auto') else lang
    try:
        model = whisper_model_for_language(None, eff)
    except Exception:
        model = 'base'
    return {'model': model, 'lang': eff or 'auto'}


def _save_asr_cache(run_dir, video_path, vdur, asr, sig=None):
    """v1.4.64：台词转写落盘缓存——精剪时间轴复用同一任务目录时不必再跑一遍
    Whisper（15 分钟片约 40 秒、90 分钟片要好几分钟）。写临时文件再 os.replace，
    用户中途取消不会留下半个 JSON 把下次复用带崩。"""
    if not run_dir or not asr:
        return
    try:
        fp = _asr_cache_path(run_dir)
        tmp = fp + '.tmp'
        sig = sig or {}
        data = {'video': os.path.basename(str(video_path or '').replace('\\', '/')),
                'vdur': round(float(vdur or 0), 1),
                'v': _ASR_CACHE_VER,
                'model': sig.get('model') or '',
                'lang': sig.get('lang') or '',
                'segments': [{'start': s.get('start'), 'end': s.get('end'),
                              'text': s.get('text')}
                             for s in asr if isinstance(s, dict)]}
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, fp)
    except Exception as e:
        _log.info('[LIVE] 台词缓存写入失败（不影响本次分析）: %s', e)


def _load_asr_cache(run_dir, video_path, vdur, sig=None):
    """读台词缓存；片名、时长或「模型+语言指纹」对不上就作废——绝不把别的录像
    的台词、或别的模型转出的台词套到这次结果上。缓存坏了也只当没有。"""
    if not run_dir:
        return None
    try:
        fp = _asr_cache_path(run_dir)
        if not os.path.exists(fp):
            return None
        with open(fp, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if os.path.basename(str(video_path or '').replace('\\', '/')) != data.get('video'):
            return None
        if int(data.get('v') or 0) != _ASR_CACHE_VER:
            _log.info('[LIVE] 台词缓存是旧清洗规则（v%s ≠ v%s），重新转写',
                      data.get('v'), _ASR_CACHE_VER)
            return None
        if abs(float(data.get('vdur') or 0) - float(vdur or 0)) > 1.5:
            return None
        sig = sig or {}
        if sig:
            if str(data.get('model') or '') != str(sig.get('model') or '') \
                    or str(data.get('lang') or '') != str(sig.get('lang') or ''):
                _log.info('[LIVE] 台词缓存与本次模型不符（旧 %s/%s ≠ 新 %s/%s），重新转写',
                          data.get('model'), data.get('lang'),
                          sig.get('model'), sig.get('lang'))
                return None
        segs = [s for s in (data.get('segments') or []) if isinstance(s, dict)]
        return segs or None
    except Exception:
        return None


def _compute_signals(video_path, run_dir, progress=None, use_asr=True,
                     asr_language=None, danmaku_events=None):
    """v1.4.64 抽出的信号层：音量 / 画面运动 / 台词 / 弹幕 → 相对化融合曲线。

    阶段①「挑 N 条」与精剪时间轴「全片每个高能点」共用同一次计算——两条产品
    路径的高光口径不会分叉，时间轴里标记的热度也正好等于快速出片用的分数。"""
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
    # progress 透传：逐帧分析长片要跑几十秒~几分钟，beat_analysis 会在同一段
    # progress 上推进百分比——不透传的话进度条在这段全程冻结，像卡死。
    motion, _mdur = _motion_profile(video_path, win, progress=progress)
    if motion is not None and motion.size and motion.size != nwin:
        n = min(nwin, motion.size)
        m2 = np.zeros(nwin, dtype=np.float32)
        m2[:n] = motion[:n]
        motion = m2

    asr = []
    asr_sig = _asr_signature(asr_language) if (use_asr and vdur <= ASR_MAX_SEC) else {}
    if use_asr and vdur <= ASR_MAX_SEC:
        asr = _load_asr_cache(run_dir, video_path, vdur, asr_sig) or []
        if asr:
            up('复用已转写台词（同一任务无需重跑 Whisper）', 40)
            # 缓存命中就不跑 asr_segments 了，那里挂的弹窗提示也就不会触发；
            # 但「配置的模型只认英文」这件事没变，用户下次还是会中招。
            # force=True：缓存是别的模型转的，说明当时就是靠兜底才转对。
            try:
                note_asr_model_mismatch(None, asr_sig.get('lang'),
                                        used=asr_sig.get('model'), force=True)
            except Exception:
                pass
        else:
            _lang_note = {'auto': '自动判别'}.get(asr_language or '', asr_language)
            up('转写台词（Whisper · %s · %s，长片请耐心）'
               % (_lang_note or '自动判别', asr_sig.get('model') or '默认模型'), 40)
            try:
                asr = asr_segments(video_path, language=asr_language or None) or []
            except Exception as e:
                _log.info('[LIVE] ASR 失败（降级为音量+画面双信号）: %s', e)
                asr = []
            if asr:
                _save_asr_cache(run_dir, video_path, vdur, asr, asr_sig)
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

    # v1.4.61 弹幕热度：观众用弹幕投票「哪里好看」。时间轴与音量/画面共用
    # 同一个 nwin2 窗格；全 0（比如弹幕时刻全落在片长之外）视为缺席，不参与
    # 融合——否则一条常数曲线会把所有窗口的分数拉平。
    danmaku = None
    if danmaku_events:
        dc = _danmaku_profile(danmaku_events, nwin2, win)
        if dc.size and float(dc.max()) > 0:
            danmaku = dc

    # 融合：各路分位次按权重合成（某路缺席就把权重匀给其它路，不空算）
    up('计算综合高能曲线', 62)
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
    # ties=True：弹幕是稀疏计数，并列的 0 窗必须同分位次，不许按时间索引错开
    r_danmaku = pct_rank(danmaku, ties=True) if danmaku is not None else None
    if r_danmaku is not None:
        ranks.append(r_danmaku)
        weights.append(W_DANMAKU)
    wsum = float(sum(weights)) or 1.0
    combined = np.zeros(nwin2, dtype=np.float64)
    for r, w in zip(ranks, weights):
        combined[:r.size] += float(w / wsum) * r
    return {'vdur': vdur, 'has_audio': has_audio, 'win': win, 'nwin': nwin,
            'nwin2': nwin2, 'combined': combined, 'asr': asr,
            'kw': kw, 'ranks': ranks, 'r_danmaku': r_danmaku,
            'r_motion': r_motion, 'r_speech': r_speech, 'danmaku': danmaku}


def analyze_highlights(video_path, run_dir, progress=None, n_clips=None,
                       min_sec=None, max_sec=None, use_asr=True, asr_language=None,
                       danmaku_events=None):
    """主入口：一条录像 → 高光段列表（含可读理由）。

    danmaku_events（v1.4.61）：可选的 [{t, text}] 直播弹幕（B 站录播姬 XML
    解析产物）。给了就多融一路「观众投票」，没给则整条链路与旧版逐位等价。

    进度约定与全产品一致：progress['phase']/['pct']，用户取消抛 AbortError
    （ffmpeg_run/帧分析/ASR 都已接 RUN_PROCS 协作取消）。"""
    up = (lambda phase, pct: progress.update({'phase': phase, 'pct': pct})
          if progress else (lambda phase, pct: None))
    sig = _compute_signals(video_path, run_dir, progress=progress, use_asr=use_asr,
                           asr_language=asr_language, danmaku_events=danmaku_events)
    vdur, win = sig['vdur'], sig['win']
    up('挑选高光片段', 70)
    clips = select_clips(sig['combined'], win, vdur, n_clips=n_clips,
                         min_sec=min_sec, max_sec=max_sec)
    for c in clips:
        pk = int(c['peak_t'] / win)
        # 弹幕置顶：parts 只留 3 条，观众投票的理由不能被音量/画面挤掉
        c['reasons'] = _reasons(pk, [sig['r_danmaku'], sig['ranks'][0],
                                     sig['r_motion'], sig['r_speech']], sig['kw'],
                                ('弹幕热度', '音量', '画面动作', '台词密度'))
        c['lines'] = _clean_lines(_lines_in_range(sig['asr'], c['start'], c['end']))
        c['score'] = round(c['score'], 3)
        c['start'] = round(c['start'], 2)
        c['end'] = round(c['end'], 2)
        c['dur'] = round(c['end'] - c['start'], 2)
        c['peak_t'] = round(c['peak_t'], 2)
    if not clips:
        raise RuntimeError('没挑出任何高光段（录像可能整段都是静音/静止画面）')
    _log.info('[LIVE] 高光挑选完成: %d 条（音量窗口 %d，运动 %s，台词 %s段，弹幕 %s）',
              len(clips), sig['nwin'], '有' if sig['r_motion'] is not None else '无',
              len(sig['asr']), '有' if sig['danmaku'] is not None else '无')
    # asr 全量只随 meta 返回给上层（不落草稿，避免 90 分钟片的草稿膨胀）；
    # 每段可见的 lines 已经附在 clips 上，随 draft 持久化、恢复候选也在。
    return {'clips': clips, 'vdur': round(vdur, 2), 'window': win,
            'asr_used': bool(sig['asr']),
            'danmaku_used': bool(sig['danmaku'] is not None),
            'asr': [{'start': s.get('start'), 'end': s.get('end'),
                     'text': s.get('text')} for s in sig['asr']
                    if isinstance(s, dict)]}


def _clamp_focus(focus):
    """v1.4.62 水平焦点：钳制到 [0,1]，None / 坏值 / 非有限数 → 0.5（居中，旧行为）。

    0=贴左边缘，1=贴右边缘。前端滑条是 0~100%，提交前除以 100。
    坏值绝不能拼进 ffmpeg 表达式（会被当变量或语法错误，整条导出失败）。"""
    if focus is None:
        return 0.5
    try:
        v = float(focus)
    except (TypeError, ValueError):
        return 0.5
    if not np.isfinite(v):
        return 0.5
    return max(0.0, min(1.0, v))


def _vf_chain(aspect, tm, focus=None):
    """按输出画幅构造 -vf 滤镜链。aspect：orig=原始 / crop=竖屏9:16裁切 /
    blur=竖屏9:16全景+虚化背景（发抖音推荐：侧边摄像头和弹幕都不丢） /
    hd=横屏16:9标准化1280x720（发B站/YouTube横屏投稿）。
    tm 为 HDR tonemap 链（可能为空），永远排在最前（先调色再构图形）。

    focus（v1.4.62）：仅 crop 生效的水平焦点 0~1（None=0.5 居中，与旧版
    逐位等价）。范文「视觉中心」的轻量版——主播/人脸不在画面正中时，把
    裁切窗口挪过去，防「人脸截到角落」。

    【为什么竖屏默认给 blur 而不是硬裁】直播画面里主播摄像头在角落、弹幕在侧边，
    居中裁切会把它们全部切掉；虚化背景板（剪映同款）保留全部信息还更上镜。
    crop 留给「主体居中或能调焦点」的素材。"""
    _base = 'setpts=(PTS-STARTPTS)' + ((',' + tm) if tm else '')
    if aspect == 'crop':
        # 9:16 裁切，x=(iw-cw)*focus；0.5000 与旧的 /2 数值完全一致
        crop = ("crop=w='min(iw\\,ih*9/16)':h=ih:"
                "x='(iw-min(iw\\,ih*9/16))*%.4f':y=0" % _clamp_focus(focus))
        return _base + ',' + crop + ',scale=720:1280:flags=lanczos'
    if aspect == 'hd':
        # 横屏 16:9 标准化 1280x720：任何源画幅都归一到标准横屏（B 站/YouTube
        # 横屏投稿直接可用）。等比缩进 + 居中 pad（源即 16:9 时无黑边，等效纯缩放）。
        # 简单线性链（无 [标签]），走 -vf 即可，与 crop 同款。
        return (_base + ',scale=1280:720:force_original_aspect_ratio=decrease:flags=lanczos,'
                'pad=1280:720:(ow-iw)/2:(oh-ih)/2')
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


def _crop_cover_image(im, aspect, focus=0.5):
    """封面按输出画幅裁切（make_covers 与换封面端点共用，v1.4.60 抽出）。
    crop/blur=横轴按 focus 裁 9:16（v1.4.62，与视频 crop 链同一焦点）；
    hd=居中裁 16:9（垂直裁，水平焦点不适用）；orig=原样。返回 PIL Image。"""
    if aspect in ('crop', 'blur'):
        w, h = im.size
        cw = min(w, int(h * 9 / 16))
        if cw % 2:
            cw -= 1
        # 与 ffmpeg 的 x=(iw-cw)*focus 同一公式；再钳一次防浮点越界
        x0 = max(0, min(w - cw, int(round((w - cw) * _clamp_focus(focus)))))
        return im.crop((x0, 0, x0 + cw, h))
    if aspect == 'hd':
        w, h = im.size
        ch = min(h, int(w * 9 / 16))
        if ch % 2:
            ch -= 1
        y0 = (h - ch) // 2
        return im.crop((0, y0, w, y0 + ch))
    return im


def rebuild_cover(video_path, t, aspect, out_path, focus=0.5):
    """按指定时刻重抽一帧做封面（换封面端点用）。返回 out_path；失败抛异常。

    focus（v1.4.62）：与导出画幅的 crop 焦点一致——换完封面不能跟成片错位。"""
    _w = _host()
    cand_dir = os.path.join(os.path.dirname(out_path), '_rebuild_cover')
    os.makedirs(cand_dir, exist_ok=True)
    try:
        _w._cover_candidates_sequential(video_path, cand_dir, [float(t)])
        fp = os.path.join(cand_dir, 'cand_00.jpg')
        if not os.path.exists(fp) or os.path.getsize(fp) < 1024:
            raise RuntimeError('抽帧失败（时刻可能越界或画面全黑）')
        from PIL import Image as _I
        im = _crop_cover_image(_I.open(fp).convert('RGB'), aspect, focus)
        im.save(out_path, 'JPEG', quality=88)
        return out_path
    finally:
        try:
            import shutil as _sh
            _sh.rmtree(cand_dir, ignore_errors=True)
        except Exception:
            pass


def make_covers(video_path, clips, run_dir, progress=None, aspect='orig', focus=0.5,
                titles=None):
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
            out = os.path.join(run_dir, _cover_name(i + 1, clips[i]['start'],
                                                    (titles[i] if titles else None)))
            try:
                from PIL import Image as _I
                im = _crop_cover_image(_I.open(best).convert('RGB'), aspect, focus)
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


def _sanitize_title(t):
    """把模型吐出的标题洗成可安全进文件名/URL 的短串：去掉 Windows/URL 非法字符、
    包裹引号、首尾空白与多余点，限长 20 字（模型侧已限 15 字，这里是兜底）。"""
    if not t:
        return ''
    bad = set('\\/:*?"<>|\r\n\t')
    t = ''.join(ch for ch in t if ch not in bad).strip().strip('.')
    t = t.strip('\'"「」“”‘’（）()【】')
    return t[:20]


def summarize_segment(lines, timeout=60):
    """v1.4.63：直播切片导出时为每段生成极简中文标题（概括这段说了什么），用于把
    文件名命名成「clipNN_时长_标题.mp4」。复用本地 Ollama qwen3:8b（local_llm_chat，
    关思考、有熔断），完全离线免 Key。

    失败/无台词/模型不可用 → 返回 None，调用方回退到时间戳文件名，**绝不阻断导出**。"""
    txt = ' '.join((str(x.get('text') or '').strip() for x in (lines or []))).strip()
    if not txt:
        return None
    txt = txt[:400]
    sys_p = ('你是短视频切片助手。下面是一段游戏直播切片里主播说过的台词（含口语、'
             '游戏术语、弹幕梗）。请用一句极简中文概括这段内容作为视频标题：'
             '只输出标题本身，不超过15个汉字，不要引号、不要序号、不要任何解释。')
    usr = '台词：' + txt
    try:
        out = local_llm_chat(usr, system=sys_p, timeout=timeout, max_tokens=64)
        title = _sanitize_title(out)
        return title or None
    except Exception as e:
        _log.info('[LIVE] 片段总结失败（回退时间戳命名）: %s', e)
        return None


def _cover_name(i, start, title=None):
    """v1.4.63 起可附带中文标题；默认仍是 ASCII 前缀（/media 路由 + 跨编码安全）。"""
    h = int(start // 3600)
    m = int(start % 3600 // 60)
    s = int(start % 60)
    base = 'cover%02d_%02dh%02dm%02ds' % (i, h, m, s)
    return ('%s_%s.jpg' % (base, title)) if title else (base + '.jpg')


def _clip_name(i, start, title=None):
    """v1.4.63 起可附带中文标题；默认仍是 ASCII 前缀（/media 路由 + 跨编码安全）。"""
    h = int(start // 3600)
    m = int(start % 3600 // 60)
    s = int(start % 60)
    base = 'clip%02d_%02dh%02dm%02ds' % (i, h, m, s)
    return ('%s_%s.mp4' % (base, title)) if title else (base + '.mp4')


def export_clips(video_path, clips, run_dir, progress=None, aspect='orig', focus=None,
                titles=None):
    """把高光段剪成独立 mp4（重编码，-ss 前置秒级 seek + 帧精确重编码）。

    aspect：orig / crop / blur（见 _vf_chain）；focus：crop 的水平焦点 0~1。
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
    vf = _vf_chain(aspect, _tm, focus)
    outs = []
    for i, c in enumerate(clips):
        up('导出切片 %d/%d' % (i + 1, len(clips)),
           70 + int(25 * i / max(1, len(clips))))
        out = os.path.join(run_dir, _clip_name(i + 1, c['start'],
                                                (titles[i] if titles else None)))
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


def _timeline_volume(s):
    """精剪音量：百分比 → 倍率，钳制 0~200%。坏值按 100%（原声）处理。"""
    try:
        v = float(s.get('volume', 1.0))
    except (TypeError, ValueError, AttributeError):
        v = 1.0
    if not np.isfinite(v):
        v = 1.0
    return max(0.0, min(2.0, v))


def validate_timeline_segments(segs, vdur, min_sec=2.0, max_sec=600.0, max_count=60):
    """v1.4.64 精剪：清洗用户排好的顺序片段清单。

    与 validate_segments 的关键区别：**不按时间排序**——时间轴上的先后就是
    成片顺序（用户可以把 10:00 的片段拖到 02:00 前面），排序会直接毁掉「更改
    位置」这个功能。过短/坏数据丢弃，音量钳制 0~200%。"""
    out = []
    for s in (segs or []):
        try:
            a = float(s.get('start'))
            b = float(s.get('end'))
        except (TypeError, ValueError, AttributeError):
            continue
        a = max(0.0, min(a, float(vdur or 0)))
        b = max(0.0, min(b, float(vdur or 0)))
        if b - a < min_sec:
            continue
        if b - a > max_sec:
            b = a + max_sec
        out.append({'start': round(a, 2), 'end': round(b, 2),
                    'dur': round(b - a, 2), 'volume': round(_timeline_volume(s), 3)})
        if len(out) >= max_count:
            break
    return out


def save_timeline_draft(run_dir, data):
    """精剪阶段①结果落盘：live_clip_timeline.json（与快速出片的 draft 分开存，
    互不覆盖——同一任务可以两种方式各来一遍）。"""
    fp = os.path.join(run_dir, 'live_clip_timeline.json')
    tmp = fp + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, fp)
    return fp


def load_timeline_draft(run_dir):
    fp = os.path.join(str(run_dir or ''), 'live_clip_timeline.json')
    if not os.path.exists(fp):
        return None
    try:
        with open(fp, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _reel_name(n, total, run_dir):
    """精剪成片文件名：段数 + 总时长一眼可见（文件名里带时长是剪辑工具惯例）。
    中文名现在能走 /media（unquote），重名自动加序号不覆盖上一条成片。"""
    base = '精剪成片_%d段_%02dm%02ds' % (n, int(total // 60), int(total % 60))
    name = base + '.mp4'
    k = 2
    while os.path.exists(os.path.join(run_dir, name)):
        name = '%s_%d.mp4' % (base, k)
        k += 1
    return name


def export_timeline(video_path, segs, run_dir, progress=None, aspect='blur', focus=0.5):
    """v1.4.64 精剪阶段②：按时间轴顺序逐段渲染（各自音量）→ 拼成一条成片。

    为什么两段式（先逐段 mp4 再 concat）而不是一条巨型 filter_complex：
      · 30 段时命令行与滤镜图会膨胀到几 KB，出错时日志根本看不清是哪一段；
      · 逐段失败能精确报「第 N 段渲染失败」，用户可以删掉那段再导；
      · concat 直拷（-c copy）零重编码，30 段也只要几秒；失败自动回退重编码。
    """
    _w = _host()
    up = (lambda phase, pct: progress.update({'phase': phase, 'pct': pct})
          if progress else (lambda phase, pct: None))
    if not segs:
        raise RuntimeError('时间轴是空的：先保留至少一个片段')
    vdur, has_audio = _probe_duration(video_path)
    if vdur <= 0:
        raise RuntimeError('读不到源片时长（文件损坏？）')
    total = sum(float(s['end']) - float(s['start']) for s in segs)
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
    vf = _vf_chain(aspect, _tm, focus)
    parts_dir = os.path.join(run_dir, '_tl_parts')
    import shutil as _sh
    _sh.rmtree(parts_dir, ignore_errors=True)
    os.makedirs(parts_dir, exist_ok=True)
    parts = []
    n = len(segs)
    for i, s in enumerate(segs):
        up('渲染片段 %d/%d（成片共 %.0f 秒）' % (i + 1, n, total),
           5 + int(65 * i / max(1, n)))
        out = os.path.join(parts_dir, 'p%03d.mp4' % i)
        vol = _timeline_volume(s)
        cmd = ['-y', '-ss', '%.3f' % float(s['start']), '-i', video_path,
               '-t', '%.3f' % (float(s['end']) - float(s['start']))]
        if vf.startswith('[0:v]'):
            graph, maps = vf, ['-map', '[vout]']
            if has_audio:
                graph += ';[0:a]volume=%.4f,asetpts=PTS-STARTPTS[aout]' % vol
                maps += ['-map', '[aout]']
            cmd += ['-filter_complex', graph] + maps
        else:
            cmd += ['-vf', vf]
            if has_audio:
                cmd += ['-af', 'volume=%.4f,asetpts=PTS-STARTPTS' % vol]
        cmd += enc
        if _tm:
            cmd += _tags
        cmd += ['-c:a', 'aac', '-b:a', '160k', '-ar', '44100', '-ac', '2', out]
        rc, _o, e = ffmpeg_run(cmd)
        if rc != 0 or not os.path.exists(out) or os.path.getsize(out) < 10240:
            raise RuntimeError('第 %d 段渲染失败（%s）: %s'
                               % (i + 1, _fmt_range(s), e.decode('utf-8', 'ignore')[-200:]))
        parts.append(out)
    up('拼接成片（%d 段 · %.0f 秒）' % (n, total), 74)
    final = os.path.join(run_dir, _reel_name(n, total, run_dir))
    lst = os.path.join(parts_dir, 'concat.txt')
    with open(lst, 'w', encoding='utf-8') as f:
        for p in parts:
            f.write("file '%s'\n" % os.path.basename(p))
    rc, _o, e = ffmpeg_run(['-y', '-f', 'concat', '-safe', '0', '-i', lst,
                            '-c', 'copy', '-movflags', '+faststart', final])
    ok = rc == 0 and os.path.exists(final) and os.path.getsize(final) > 10240
    if not ok:
        _log.info('[LIVE] concat 直拷失败，回退重编码拼接: %s',
                  e.decode('utf-8', 'ignore')[-200:])
        cmd = ['-y']
        for p in parts:
            cmd += ['-i', p]
        if has_audio:
            graph = ''.join('[%d:v:0][%d:a:0]' % (k, k) for k in range(n))
            graph += 'concat=n=%d:v=1:a=1[v][a]' % n
            maps = ['-map', '[v]', '-map', '[a]']
        else:
            graph = ''.join('[%d:v:0]' % k for k in range(n))
            graph += 'concat=n=%d:v=1:a=0[v]' % n
            maps = ['-map', '[v]']
        cmd += ['-filter_complex', graph] + maps + enc
        if _tm:
            cmd += _tags
        cmd += ['-c:a', 'aac', '-b:a', '160k', '-ar', '44100', '-ac', '2',
                '-movflags', '+faststart', final]
        rc, _o, e = ffmpeg_run(cmd)
        if rc != 0 or not os.path.exists(final) or os.path.getsize(final) < 10240:
            raise RuntimeError('拼接成片失败: %s' % e.decode('utf-8', 'ignore')[-200:])
    up('清理临时片段', 90)
    try:
        _sh.rmtree(parts_dir, ignore_errors=True)
    except Exception:
        pass
    return {'file': final, 'dur': round(total, 2), 'n': n,
            'segments': [{'start': s['start'], 'end': s['end'],
                          'dur': s['dur'], 'volume': s['volume']} for s in segs]}


def _fmt_range(s):
    def _t(v):
        v = max(0.0, float(v or 0))
        return '%02d:%02d' % (int(v // 60), int(v % 60))
    return '%s~%s' % (_t(s.get('start')), _t(s.get('end')))


def write_precise_report(meta, segs, out_path, run_dir, aspect='blur'):
    """精剪成片报告：顺序、源片区间、每段音量、总时长（复盘/二次编辑用）。"""
    _names = {'orig': '原始横屏', 'crop': '竖屏 9:16（居中裁切）',
              'blur': '竖屏 9:16（全景+虚化背景）', 'hd': '横屏 16:9（1280×720）'}
    total = sum(float(s['end']) - float(s['start']) for s in segs)
    items = []
    for i, s in enumerate(segs):
        items.append({'order': i + 1, 'start': s['start'], 'end': s['end'],
                      'dur': s.get('dur'), 'volume': s.get('volume', 1.0),
                      'range': _fmt_range(s)})
    data = {'video': os.path.basename(str(meta.get('video') or '').replace('\\', '/')),
            'video_dur': meta.get('vdur'),
            'aspect': _names.get(aspect, aspect),
            'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'total_dur': round(total, 2), 'reel': os.path.basename(out_path),
            'segments': items}
    jp = os.path.join(run_dir, 'live_clip_precise_report.json')
    with open(jp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    lines = ['直播精剪报告（%s）' % data['generated_at'],
             '源片：%s（时长 %.1f 分钟）｜输出画幅：%s'
             % (data['video'], (meta.get('vdur') or 0) / 60.0, data['aspect']),
             '成片：%s（%d 段 · 共 %.1f 秒）'
             % (data['reel'], len(items), total),
             '']
    for it in items:
        vol = int(round(float(it['volume']) * 100))
        lines.append('%d. 源片 %s（%d 秒）  音量 %d%%%s'
                     % (it['order'], it['range'], int(it['dur'] or 0), vol,
                        '（静音）' if vol == 0 else ''))
    lines.append('')
    lines.append('成片按时间轴顺序拼接，保留原声；音量是各段独立增益。')
    tp = os.path.join(run_dir, 'live_clip_precise_report.txt')
    with open(tp, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    return os.path.basename(jp), os.path.basename(tp)


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
    # 源片路径可能是 Windows 反斜杠形态（Docker/Linux 上 os.path.basename 不认 \，
    # 会把整个 C:\rec\x.mkv 当文件名写进报告——CI 实测踩中），先归一化再取尾。
    data = {'video': os.path.basename(str(meta.get('video') or '').replace('\\', '/')),
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


def make_candidate_thumbs(video_path, clips, run_dir, progress=None):
    """阶段①给每个候选段抽一帧峰值画面（封面候选的同款姿势，1 帧/段），
    供候选编辑器直出缩略图——主人调段时不用盲猜每段是什么画面。
    返回与 clips 等长的缩略图路径列表（失败位为 ''，不阻断分析）。"""
    from ffmpeg_utils import ffmpeg_run as _fr
    outs = []
    for i, c in enumerate(clips):
        span = max(1.0, c['end'] - c['start'])
        t = min(max(c.get('peak_t') or (c['start'] + span / 2),
                    c['start'] + 0.2), c['end'] - 0.2)
        out = os.path.join(run_dir, _thumb_name(i + 1, c['start']))
        try:
            rc, _o, e = _fr(['-y', '-ss', '%.3f' % t, '-i', video_path,
                             '-frames:v', '1', '-q:v', '3', out])
            if rc == 0 and os.path.isfile(out) and os.path.getsize(out) > 512:
                outs.append(out)
            else:
                outs.append('')
        except Exception:
            outs.append('')
    return outs


def _thumb_name(i, start):
    h = int(start // 3600)
    m = int(start % 3600 // 60)
    s = int(start % 60)
    return 'thumb%02d_%02dh%02dm%02ds.jpg' % (i, h, m, s)
