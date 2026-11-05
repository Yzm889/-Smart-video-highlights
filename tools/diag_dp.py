# -*- coding: utf-8 -*-
"""离线验证窗口化 DP 对齐（复刻 _algo_align_beats_to_scenes 的打分，便于扫参）。

用法： py -3.13 -u tools/diag_dp.py <run_dir> [prior_w] [win_mult] [same_pen]（在项目根目录运行）
"""
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 项目根目录（脚本在 tools/ 下）
import webui_server  # noqa: F401
import movie_narrator as mn

RUN = sys.argv[1]
PW = float(sys.argv[2]) if len(sys.argv) > 2 else 0.10
WM = float(sys.argv[3]) if len(sys.argv) > 3 else 2.5
SP = float(sys.argv[4]) if len(sys.argv) > 4 else 0.25

TRUTH = [60, 130, 168, 185, 200, 225, 245, 250, 256, 257, 258, 265, 280, 440, 455,
         465, 480, 520, 570, 650, 715, 790, 795, 818, 840, 860, 880, 920, 960]

d = json.load(open(os.path.join(RUN, 'tts_state.json'), encoding='utf-8'))
narr, asr = d['narr'], d['asr']
ss = {int(k): v for k, v in (d.get('scene_story') or {}).items()}
descs = json.load(open(os.path.join(RUN, 'scene_descs.json'), encoding='utf-8'))
if isinstance(descs, dict):
    descs = descs.get('scenes') or descs.get('items') or []


def bg(s):
    s = str(s or '')
    return set(s[i:i + 2] for i in range(len(s) - 1) if s[i:i + 2].strip())


scene_bg = []
for j, sc in enumerate(descs):
    parts = [mn._scene_describe(sc)]
    st = ss.get(j) or {}
    if st.get('story'):
        parts.append(str(st['story']))
    if st.get('keywords'):
        parts.append(''.join(str(k) for k in st['keywords']))
    try:
        parts.append(mn._asr_text_in(asr, float(sc.get('start', 0) or 0),
                                      float(sc.get('end', 0) or 0)))
    except Exception:
        pass
    scene_bg.append(bg(' '.join(p for p in parts if p)))
beat_bg = [bg(b) for b in narr]
df = {}
for s in scene_bg:
    for g in s:
        df[g] = df.get(g, 0) + 1
N = max(1, len(scene_bg))


def w(g):
    return math.log((N + 1.0) / (df.get(g, 0) + 0.5))


def score(i, j):
    q, s = beat_bg[i], scene_bg[j]
    if not q or not s:
        return 0.0
    sh = q & s
    if not sh:
        return 0.0
    return sum(w(g) for g in sh) / math.sqrt(sum(w(g) for g in q) or 1.0)


starts = [float(s.get('start', 0) or 0) for s in descs]
m, n = len(descs), len(narr)
vdur = max(float(s.get('end', 0) or 0) for s in descs)
wts = [max(1, len(t)) for t in narr]
tot = float(sum(wts))
cum, acc = [], 0.0
for x in wts:
    cum.append((acc + x / 2.0) / tot)
    acc += x

sigma = max(20.0, vdur / n * 2.5)
WIN = max(2, int(round(WM * m / float(n))))
# 每句的语义峰值：决定先验影响力。零信号句（如「片头音乐炸开」）不该被先验
# 单独锚到远处——真机实测它把自己锚到 499 秒，单调约束又把后面 18 句全部顶后。
val = []
for i in range(n):
    te = cum[i] * vdur
    peak = max(score(i, j) for j in range(m))
    pscale = min(1.0, peak / 0.30)
    row = []
    for j in range(m):
        d = (starts[j] - te) / sigma
        row.append(score(i, j) + PW * pscale * math.exp(-0.5 * d * d))
    val.append(row)

# 自适应连用惩罚：零信号句不主动推进时间轴
pens = [SP * min(1.0, max(score(i, j) for j in range(m)) / 0.30) for i in range(n)]
NEG = -1e18
dp = [[NEG] * m for _ in range(n)]
bk = [[-1] * m for _ in range(n)]
for j in range(m):
    dp[0][j] = val[0][j]
for i in range(1, n):
    for j in range(m):
        best, bj = NEG, -1
        for k in range(max(0, j - WIN), j + 1):
            cand = dp[i - 1][k] - (pens[i] if k == j else 0.0)
            if cand > best:
                best, bj = cand, k
        dp[i][j] = val[i][j] + best
        bk[i][j] = bj
j = max(range(m), key=lambda x: dp[n - 1][x])
pick = [0] * n
for i in range(n - 1, -1, -1):
    pick[i] = j
    if i > 0:
        j = bk[i][j]

mae = 0.0
for i in range(n):
    t = starts[pick[i]]
    mae += abs(t - TRUTH[i])
    mark = ' <<<' if abs(t - TRUTH[i]) > 90 else ''
    print('%2d 对齐%7.1f 参考%7.1f 差%+7.1f%s %s' % (i, t, TRUTH[i], t - TRUTH[i], mark, narr[i][:24]))
print('窗口=%d  PRIOR_W=%.2f  SAME_PEN=%.2f  MAE=%.0fs  扑咬段(4-10)=%s'
      % (WIN, PW, SP, mae / n, [int(starts[pick[i]]) for i in range(4, 11)]))
