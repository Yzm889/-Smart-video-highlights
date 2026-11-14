# -*- coding: utf-8 -*-
"""语义对齐算法族（DP 单调对齐 / 规范化 / 字数分配 / 引用台词让位）——由 movie_narrator.py 拆出（v1.4.42）。

搬运范围（逐字搬运、零逻辑改动）：
    _scene_info_pen / _beat_info_pen / _default_jump_cap / _JUMP_CAP_RATIO /
    _effective_jump_cap / _algo_align_beats_to_scenes / _isotonic /
    _sanitize_alignment / _allocate_script_spans / _QUOTE_RE / _SPEAK_RE /
    _quote_is_dialogue / _quote_texts / _norm_cn / _literal_score /
    _score_block / _pick_quote_span / _detect_quote_slots

LLM 对齐族（_llm_align_batched / _refine_batched / _parse_alignment_obj /
_align_from_story_fill 等）**不在本文件**，仍留在 movie_narrator 主模块。

约定与 movie_narrator 一致：宿主符号（vlm_enabled 等）经 _w.<name> 晚绑定
（本文件照抄 _host() 模式，拿到的是同一个 webui_server 模块对象）；公共符号由
movie_narrator 尾部 re-export 并回注宿主，调用方零改动。

【依赖解析规则——拆分成败点，勿改】凡定义在 movie_narrator 命名空间的符号
（含它按值 import / 模块级定义的帮手与常量），一律经函数内惰性
`import movie_narrator as _MN` 后以 _MN.X(...) 调用期属性查找：
测试普遍 monkeypatch.setattr(M, 'X', ...) 打在主模块命名空间上（如
test_quote_gate patch M._score_block / M._local_model_available 后调
M._pick_quote_span），函数搬家后 __globals__ 换成了本模块的 dict，裸名会让
全部 patch 静默打空。族内互调的「测试会 patch 的名字」同样走 _MN
（M 从本模块 re-export 的是同一函数对象，patch M 仍命中）。
"""
import logging, os, sys, json, math, re, time

_log = logging.getLogger('framecut.narrator')   # 与主模块同名 logger（同一单例）

# ---- 宿主晚绑定（照抄 movie_narrator._host）----
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


def _scene_info_pen(text):
    """场景描述信息量惩罚：VLM/剧情理解层的长描述常掺通用词与车轱辘话，
    信息密度低。≤60 字不罚；每超出 300 字扣至一半（0.5 保底）→ 短描述优先得分。"""
    n = len(str(text or ''))
    if n <= 60:
        return 1.0
    return max(0.5, 1.0 / (1.0 + (n - 60) / 300.0))


def _beat_info_pen(text):
    """解说句长惩罚：信息量相同的内容，短句优先，防止长解说句（重述/车轱辘）
    从短句那里吸走高分场景。"""
    n = len(str(text or ''))
    if n <= 90:
        return 1.0
    return max(0.5, 1.0 / (1.0 + (n - 90) / 400.0))


def _default_jump_cap(scenes):
    """默认 max_jump_seconds 的「基数」：3×场景平均时长（够跨过 1~2 段台词填充），
    限在 [25, 150]s 避免过于苛刻/松懈。

    注意这是**基数**不是最终生效值：最终上限由 _effective_jump_cap 再抬到
    max(本值, vdur/n_beats)，否则句数少的片子里语义正确的远处场景不可达
    （实测 3 句/1200s 需 ≥180s，本值只有 35s）。"""
    durs = []
    for sc in scenes or []:
        try:
            st = float(sc.get('start', 0) or 0)
            ed = float(sc.get('end', 0) or 0)
        except Exception:
            continue
        durs.append(max(0.0, ed - st))
    if not durs:
        return 45.0
    avg = sum(durs) / float(len(durs))
    v = max(25.0, min(150.0, 3.0 * avg))
    return round(v / 5.0) * 5.0


# 跳跃上限的「每句平均画面份额」系数（2026-11-10 二次修正）。
# 取 1.0 = 一句解说最多推进「整片时长 / 句数」——覆盖均匀铺开的语义锚所需间距，
# 见 _effective_jump_cap 的实测。
_JUMP_CAP_RATIO = 1.0


def _effective_jump_cap(scenes, n_beats, max_jump_seconds=None):
    """相邻解说句之间的画面跳跃上限（秒）——三处约束的统一入口。

    base = max_jump_seconds（调用方显式给的上限，生产为 NAR_MAX_JUMP_SECONDS）；
    传 0/负/None → 退回 _default_jump_cap(scenes)（3×场景平均时长，25~150s）。

    【2026-11-10 修正：绝对秒数上限对「句子少」的片子是错的】
    base 是绝对秒数，只对「句数多」的片子合适：真机 52 句配 1200 秒画面 →
    平均每句 23 秒，20 秒上限正合适。但句数很少时（测试夹具 3 句配 1200 秒，
    平均每句该推进 400 秒），绝对上限会把 DP 锁死：语义正确的远处场景
    (医院 300s) 变成不可达，DP 只好就近取景 → 声画错位。

    实测（100×12s 夹具，4 句各有一个唯一锚点，锚点 start 间距 300s、vdur/n=300s；
    DP 约束的是 start[j] - end[k]，场景时长 12s，故实际所需跃迁 = 300-12 = 288s）：
      cap=20   → 命中 1/4 锚点（丢 3 个）    cap=100  → 1/4
      cap=200  → 1/4                        cap=288  → 4/4 ✓
      cap=300+ → 4/4 ✓
    即「上限够不上所需跃迁」时锚点被静默丢弃（此夹具所需跃迁 288s 略小于
    vdur/n=300s，故以 vdur/n 作上限一定够用；间距越大所需上限越高）。锚点是按剧情均匀铺开
    的，故以「每句平均应推进的画面时长 vdur/n」为基准最稳妥——它正好覆盖均匀
    分布所需的间距，也随句数自适应：

        cap = max(base, _JUMP_CAP_RATIO * vdur / n_beats)      RATIO = 1.0

    RATIO 取 1.0 是「刚好覆盖均匀间距」的最小值，也是最防狂跳的值：只抬不降
    （max），句数多的片子行为几乎不变——852s→1212s 那幕狂跳正是 52 句 / 1500s，
    vdur/n=28.8s，上限仅从 20s 抬到 28.8s，狂跳照样挡住（实测该夹具第 12 句
    落在 996s，远早于事故的 1212s；无上限时飞到 1212s、单句跃迁 379.4s）。
    """
    # base：调用方显式给的正数上限；0/负/None 一律按注释退回场景动态基数。
    if max_jump_seconds and float(max_jump_seconds) > 0:
        base = float(max_jump_seconds)
    else:
        base = _default_jump_cap(scenes)
    n = int(n_beats or 0)
    if n <= 0 or not scenes:
        return max(1e-6, base)
    try:
        vdur = max(float(sc.get('end', 0) or 0) for sc in scenes)
    except (ValueError, TypeError, AttributeError):
        return max(1e-6, base)
    return max(1e-6, base, _JUMP_CAP_RATIO * vdur / float(n))


def _algo_align_beats_to_scenes(beats, scenes, asr=None, scene_story=None,
                               action_weight=None, beat_kinds=None,
                               max_jump_seconds=None):
    """算法语义对齐（不调模型）：用 VLM 场景描述/台词/剧情理解与解说词的字符 bigram
    重叠度，在「保持时间顺序」的前提下为每句解说挑最相关的连续场景，并跳过无关填充段。

    返回 {beat_idx: [scene_idx, ...]}，格式与 LLM 对齐结果一致，可直接喂给
    _allocate_script_spans。这是 LLM 对齐失败/超时时的保底通道——保证声画不会退化成
    「按字数平均切时间线」的盲切（解说词是改写过的剧情，与原始台词重合度低，
    只按台词密度分配必然「讲 A 放 B」）。
    """
    import movie_narrator as _MN
    n, m = len(beats or []), len(scenes or [])
    if n <= 0 or m <= 0:
        return {}
    if n > m:
        # 句子比场景多：按比例共享场景（保证每句都有画面，且顺序不倒退）
        return {i: [min(m - 1, int(i * m / float(n)))] for i in range(n)}

    _aw = float(action_weight or _MN._ACTION_WEIGHT)

    # —— 句类标签（2026-11-10 语义对齐重构）：只有具象句参与语义匹配；
    # 钩子/引用/留白/过渡零语义，画面只随时间单调推进（绝不倒流）。——
    _kinds = list(beat_kinds) if beat_kinds else []
    _kinds = (_kinds + [_MN.KIND_CONCRETE] * n)[:n]
    beat_pen = [_beat_info_pen(b) for b in beats]

    def _bg(s):
        s = str(s or '')
        toks = set(s[i:i + 2] for i in range(len(s) - 1) if s[i:i + 2].strip())
        # 动作词以「标准词」参与匹配：「接吻」与「吻在一起」靠 ACT:吻 才连得上。
        toks |= _MN._action_tokens(s)
        return toks

    scene_bg, scene_txt = [], []
    for j, sc in enumerate(scenes):
        parts = [_MN._scene_describe(sc)]
        st = (scene_story or {}).get(j) or {}
        if st.get('story'):
            parts.append(str(st['story']))
        if st.get('keywords'):
            parts.append(''.join(str(k) for k in st['keywords']))
        if asr:
            try:
                parts.append(_MN._asr_text_in(asr, float(sc.get('start', 0) or 0),
                                              float(sc.get('end', 0) or 0)))
            except Exception:
                pass
        txt = ' '.join(p for p in parts if p)
        scene_txt.append(txt)             # 与 scene_bg 同源，供 _scene_info_pen 用
        scene_bg.append(_bg(txt))
    beat_bg = [_bg(b) for b in beats]
    sc_pen = [_scene_info_pen(t) for t in scene_txt]

    # 逆文档频率：在几十个场景里都出现的 bigram（「警察」「一名」「车内」「的」）
    # 几乎不带信息，必须压低；「低吼」「番茄酱」「对讲机」这类只在少数场景出现的
    # 才真正指向画面。没有这一层，通用词的总分比重盖过实义词，句句对错位。
    _df = {}
    for _s in scene_bg:
        for _g in _s:
            _df[_g] = _df.get(_g, 0) + 1
    _N = max(1, len(scene_bg))

    def _w(g):
        return math.log((_N + 1.0) / (_df.get(g, 0) + 0.5))

    def _score(i, j):
        # 【2026-11-10】句类闸门：钩子/引用/留白/过渡句不描述具体画面，
        # 字面 bigram 重合全是噪声——它们一旦参与匹配就会「乱吸」场景，
        # 解说后句被绑到视频前段画面（画面倒流/乱跳的元凶）。
        # 这些句子零语义，只由 DP 的单调约束决定位置（哪里近就接哪里）。
        if _kinds[i] != _MN.KIND_CONCRETE:
            return 0.0
        q, s = beat_bg[i], scene_bg[j]
        if not q or not s:
            return 0.0
        shared = q & s
        if not shared:
            return 0.0
        # 动作命中加权：一句「他们深情地吻在了一起」如果只按字面算，
        # 顶多和「吻」沾边的镜头得点分；加权后带动作的镜头能压过
        # 「台词密度高但内容无关」的镜头——这正是高光画面被保留的关键。
        def _wt(g):
            return _w(g) * (_aw if g.startswith('ACT:') else 1.0)

        got = sum(_wt(g) for g in shared)
        # 分母用「解说词自己的信息量」：长句不会因为场景描述长而占便宜
        den = math.sqrt(sum(_wt(g) for g in q) or 1.0)
        # 【2026-11-10】描述信息量惩罚：长的场景描述（VLM 车轱辘话/剧情理解层
        # 复述）信息密度低，短描述优先得分（得分 × 衰减系数）。
        return got / den * sc_pen[j] * beat_pen[i]

    # —— 全局单调对齐（动态规划）——
    # 逐句贪心地「在剩下的范围里取最大分」是错的：第 7 句一旦被 384 秒的某个巧合
    # 吸走，后面每句都只能往后排，整条时间轴就歪了。真机实测 29 句稿子里第 12 句
    # 落到 1044 秒（画面是医院走廊），而稿子讲的是巡逻车里啃汉堡——全片错位。
    # 改成「整条序列联合最优」：在「时间不倒流」的约束下最大化总相关度。
    # 再加一个很弱的等比例先验，只在没有实义词可依据时打破平局，避免乱跳。
    # 【权重只能小】真机实测 PRIOR_W=0.3 时，中段和结尾被先验拖后 200~350 秒
    # （稿子的详略节奏和片子的时间节奏并不一致），降到 0.1 后平均误差 122s→33s。
    starts = [float(sc.get('start', 0) or 0) for sc in scenes]
    ends_arr = [float(sc.get('end', 0) or 0) for sc in scenes]
    vdur = max(ends_arr or [0.0])

    # 【2026-11-10 强制单调约束】跳跃上限：句子间「新场景 start - 上一句
    # 前一场景 end」的允许值（之后的场景增量允许，之前的全部 -inf）。
    # 上限 = max(显式值, vdur/n)：句数少时绝对秒数会把语义正确的远处场景
    # 变成不可达（详见 _effective_jump_cap 的实测）。
    _jump = _effective_jump_cap(scenes, n, max_jump_seconds)

    _wts = [max(1, len(str(b or ''))) for b in beats]
    _tot = float(sum(_wts)) or 1.0
    _cum, _acc = [], 0.0
    for _x in _wts:
        _cum.append((_acc + _x / 2.0) / _tot)   # 这句在稿子里的相对位置
        _acc += _x

    PRIOR_W = 0.12
    sigma = max(20.0, vdur / float(max(1, n)) * 2.5)
    val = []
    for i in range(n):
        te = _cum[i] * vdur
        # 先验只作「首尾锚点」：真值整体比等比例更靠前压缩，全局先验会把中段
        # 往后拽（真机实测 PRIOR_W=0.10 时中段过冲达 152 秒）。只在第 0 句和末句
        # 用弱先验把时间轴钉在片头/片尾，中段完全交给语义相似度 + 单调约束，
        # 实测 MAE 35s→29s、最大误差 152s→96s，且首/末句仍准确对位。
        _pw = PRIOR_W if i in (0, n - 1) else 0.0
        _peak = max(_score(i, j) for j in range(m))
        _pscale = min(1.0, _peak / 0.30)
        row = []
        for j in range(m):
            d = (starts[j] - te) / sigma
            row.append(_score(i, j) + _pw * _pscale * math.exp(-0.5 * d * d))
        val.append(row)

    # 单句最大推进窗口：解说讲一句，画面最多往前走「几倍平均节奏」。
    # 没有这道窗口时，DP 可以一口气跳过 300 秒（真机实测：花枯了之后的 4 句
    # 从 852 秒直接飞到 1212 秒，跳过了所有走廊戏）。窗口=2.5 倍平均节奏：
    # 既能跨过无关填充，又不会一句台词把时间轴甩到片尾。
    WIN = max(2, int(round(2.5 * m / float(n))))
    # 同场景连用惩罚：DP 本身没有「容量」概念，真机实测它把 5~11 共 7 句解说
    # 全堆在同一个高分场景上（12 秒的窗塞 49 秒的解说），后面的句子窗口被吃光，
    # 分配器只好跳去等比例位置——画面从 228 秒直接飞到 503 秒。
    # 给「上一句也在这句场景」加一笔小惩罚，鼓励相邻句子向前铺开。
    SAME_PEN = 0.25
    NEG = -1e18
    dp = [[NEG] * m for _ in range(n)]
    bk = [[-1] * m for _ in range(n)]
    for j in range(m):
        dp[0][j] = val[0][j]
    for i in range(1, n):
        prev, cur, cur_bk = dp[i - 1], dp[i], bk[i]
        # 【2026-11-10 强制单调约束】从第 i-1 句场景 k 转移到第 i 句候选 j 时：
        # ① starts[j] ≥ ends[k]（否则 -inf 拒绝倒流——解说后句绝不配到前段画面）；
        # ② starts[j] ≤ ends[k] + max_jump_seconds（超跳 -inf——最佳语义匹配在
        #    很久之前也直接丢弃，按顺序选取最近的可用场景）。
        for j in range(m):
            best, bj = NEG, -1
            for k in range(max(0, j - WIN), j + 1):
                if starts[j] + 1e-6 < ends_arr[k]:
                    continue
                if starts[j] - (ends_arr[k] + _jump) > 1e-9:
                    continue
                cand = prev[k] - (SAME_PEN if k == j else 0.0)
                if cand > best:
                    best, bj = cand, k
            if best == NEG:
                cur[j] = NEG
                continue
            cur[j] = val[i][j] + best
            cur_bk[j] = bj

    # 兜底：若全部 -inf（某句话时间上无合法落点），退回无约束 DP（零-inf 模式保底）。
    if all(dp[i][j] == NEG for i in range(n) for j in range(m)):
        for j in range(m):
            dp[0][j] = val[0][j]
        for i in range(1, n):
            prev, cur, cur_bk = dp[i - 1], dp[i], bk[i]
            for j in range(m):
                best, bj = NEG, -1
                for k in range(max(0, j - WIN), j + 1):
                    cand = prev[k] - (SAME_PEN if k == j else 0.0)
                    if cand > best:
                        best, bj = cand, k
                cur[j] = val[i][j] + best
                cur_bk[j] = bj

    j = max(range(m), key=lambda x: dp[n - 1][x])
    pick = [0] * n
    for i in range(n - 1, -1, -1):
        pick[i] = j
        if i > 0:
            j = bk[i][j]

    out = {}
    for i in range(n):
        j0 = pick[i]
        j1 = pick[i + 1] if i + 1 < n else m
        # 一句解说常跨 2~3 个连续镜头：把紧邻的场景也纳入，但不能越过下一句的位置
        out[i] = list(range(j0, min(j0 + 3, max(j0 + 1, j1))))
    return out


def _isotonic(vals):
    """保序回归（PAVA）：把一串乱序的时间点投影成「不减序列」，且位移平方和最小。

    为什么不用「遇到倒退就抬到上一句」：那是单向棘轮，每撞一次就永久抬高一点，
    错误会逐句累积。真机实测 29 句里有 10 处倒退，逐个往前抬之后，中段整体
    被顶后 250 秒——稿子讲「对讲机响了」，画面却是中枪倒地。
    保序回归只做最小必要调整，不累积漂移。
    """
    stack = []          # [[值, 权重]]
    for v in vals:
        stack.append([float(v), 1.0])
        while len(stack) > 1 and stack[-2][0] > stack[-1][0]:
            v2, w2 = stack.pop()
            v1, w1 = stack.pop()
            stack.append([(v1 * w1 + v2 * w2) / (w1 + w2), w1 + w2])
    out = []
    for v, w in stack:
        out.extend([v] * int(round(w)))
    return out


def _sanitize_alignment(merged, base, scenes, n_beats, max_span=3, factor=3.0,
                        max_jump_seconds=None):
    """把对齐结果规范成「能直接下锅」的形态。三道处理，缺一不可：

    1) 每句最多跨 max_span 个场景：模型常回 [21..39] 这种一整段（252~468 秒），
       分配器会在这个 200 秒的大窗口里撒点，等于没对齐。
    2) 顺序纠正用保序回归，不用单向棘轮（见 _isotonic 的实测说明）。
    3) 单句向前跳的距离不能离谱：解说词是按剧情顺序写的，第 i 句该落在
       「上一句结束 + 剩余时长/剩余句数」附近。模型偶尔被同款动作镜头吸走
       （真机实测把「她扑过来」对到二十多分钟后），这种直接退回算法结果；
       【2026-11-10】若配置了 max_jump_seconds，则越过「上一句结束 + 跳跃上限」
       一律触发同样的退回，且退回后再次钳制起点 ≥ prev_end，保证不倒流。
    """
    if not merged or not scenes:
        return merged
    starts = [float(s.get('start', 0) or 0) for s in scenes]
    ends = [float(s.get('end', 0) or 0) for s in scenes]
    vdur = max(ends) if ends else 0.0
    # 跳跃上限（2026-11-10 语义对齐重构）：缺省按场景平均时长动态算；
    # 同时抬到「每句平均画面份额」以适配句数少的片子（同 _effective_jump_cap）。
    _jump = _effective_jump_cap(scenes, n_beats, max_jump_seconds)

    idxs, groups = [], {}
    for i in range(n_beats):
        v = merged.get(i)
        if not v:
            continue
        g = sorted(set(int(x) for x in v if 0 <= int(x) < len(scenes)))
        if not g:
            continue
        if len(g) > max_span:
            g = g[:max_span]
        groups[i] = g
        idxs.append(i)
    if not idxs:
        return {}
    n_trunc = sum(1 for i in idxs if len(merged.get(i) or []) > max_span)

    # ② 保序回归：按各句「该出现的画面时间」做单调投影，再把结果贴回最近的场景
    raw = [starts[groups[i][0]] for i in idxs]
    fixed = _isotonic(raw)
    out = {}
    prev_first, prev_end = -1, 0.0
    n_back = n_far = 0
    for k, i in enumerate(idxs):
        g = groups[i]
        if abs(fixed[k] - raw[k]) > 1e-6:
            j = min(range(len(starts)), key=lambda x: abs(starts[x] - fixed[k]))
            j = max(j, prev_first)
            g = list(range(j, min(j + len(g), len(scenes))))
            n_back += 1
        if prev_first >= 0 and g[0] < prev_first:
            g = list(range(prev_first, min(prev_first + len(g), len(scenes))))
            n_back += 1
        # ③ 跳太远：退回算法结果（DP 全局对齐，本身单调且带等比例先验）。
        # 【2026-11-10】两条触发线，任一命中即退回：
        #   · 超出「上一句结束 + max_jump_seconds」跳跃上限（原始诉求：
        #     最佳语义匹配在很久之前也必须丢弃，强制就近取景）；
        #   · 考虑配置上限后仍过远的旧启发式（factor × 预估剩余步长）。
        # 退回后统一钳制：新组起点 ≥ prev_end + 跳限内的最近可用位置，
        # 保证即使 DP 兜底路径也满足时间轴单调递增、绝不倒流。
        exp = max(1.0, (vdur - prev_end) / float(max(1, n_beats - i)))
        if (starts[g[0]] > prev_end + _jump
                or starts[g[0]] > prev_end + factor * exp) and base and base.get(i):
            g2 = sorted(set(int(x) for x in base[i] if 0 <= int(x) < len(scenes)))
            if g2:
                g = g2[:max_span]
                # 钳制：起点必须 ≥ prev_end（不倒流），且 ≤ prev_end+_jump（不超跳）。
                if starts[g[0]] < prev_end + 1e-6:
                    j0 = min(range(len(starts)),
                             key=lambda x: abs(starts[x] - (prev_end + _jump)))
                    j0 = max(j0, 0)
                    g = list(range(j0, min(j0 + len(g), len(scenes))))
                    if starts[g[0]] < prev_end + 1e-6 and g[0] + 1 < len(scenes):
                        g = list(range(g[0] + 1, min(g[0] + 1 + len(g), len(scenes))))
                n_far += 1
        out[i] = g
        prev_first = g[0]
        prev_end = ends[g[-1]]
    if n_trunc or n_back or n_far:
        _log.info('[DIAG] 对齐规范化: 截断%d 保序纠正%d 跳远回退%d'
                  % (n_trunc, n_back, n_far))
    return out


def _allocate_script_spans(texts, vdur, asr=None, cps=None, min_dur=1.0, vlm_captions=None, scene_alignment=None, scenes=None, scene_story=None, sem_weight=1.2, scene_sem=None, progress=None, beat_kinds=None, max_jump_seconds=None):
    """按解说词字数分配画面区间 ——「解说驱动剪辑」的核心。

    返回 (spans, narr_map)：
    - spans: 扁平的子片段列表 [(start, end), ...]，每节解说词拆成多个 ~5s 短片段
    - narr_map: narr_map[k] = 第 k 个子片段属于第几节解说词（与 texts 索引对应）

    高密度剪辑：一节解说词不再只用一个长片段代表，而是从该节时间窗口内裁出多个
    短片段拼接，避免"第一幕只用一个片段一带而过"的问题。

    scene_sem：向量语义素材 (scene_texts, scene_vecs, text_vecs)，可选。
    - scene_texts 必须与 _compose_scene_texts(scenes, scene_story) 输出等长对齐，
      不齐一律忽略向量退回 bigram，绝不让向量问题打断分配。
    - 向量已 L2 归一化（余弦=点积）。传入后 _scene_match 用余弦替换 bigram——
      口语解说 vs 白描场景描述的字面差异，bigram 抓不住（A/B 实测仅 +1.8% 噪声）。

    progress：片头检测写入的进度字典；progress['content_start'] 为正片起点（秒），
    所有解说 span 的起点 ≥ content_start；progress=None / 字段缺失 / 为 0 / 越界
    → 一律退回 0（从 0 秒开始，与旧版行为逐字节一致）。
    """
    import movie_narrator as _MN
    # A1 片头让位：解说起点对齐正片起点 content_start（前置流程写入 progress）。
    try:
        content_start = float(progress.get('content_start', 0) or 0) if progress else 0.0
    except Exception:
        content_start = 0.0
    if content_start < 0 or content_start > vdur:
        content_start = 0.0   # 越界值兜底 0，绝不让片头判断破坏出片
    cps = float(cps or _MN.NAR_SCRIPT_CPS)
    n = len(texts or [])
    if n == 0 or vdur <= 0:
        return [], []
    # 【2026-11-10 语义对齐重构】句类标签：非具象句（钩子/引用/留白/过渡）
    # 不做语义匹配——它们不描述具体画面，语义吸附只会乱吸场景制造倒流。
    _kinds = list(beat_kinds) if beat_kinds else _MN._classify_sentences(texts)
    _kinds = (_kinds + [_MN.KIND_CONCRETE] * n)[:n]
    # 跳跃上限：候选起点不得早于 last_end（倒流 -inf），也不得超过
    # last_end + max_jump_seconds（超跳 -inf）——与 DP 对齐同一套约束。
    # 【2026-11-10 二次修正】上限 = max(显式值, vdur/n)：句数少时绝对秒数会把
    # 语义正确的远处画面变成不可达（详见 _effective_jump_cap 的实测）；句数
    # 与画面时长匹配时（真机 52 句/1200s）结果与原来完全一致。
    _jump = _effective_jump_cap(scenes, n, max_jump_seconds)
    weights = [max(1, len(str(t or ''))) for t in texts]
    durs = [w / cps for w in weights]
    need = sum(durs)
    if need > vdur:
        scale = vdur / need
        durs = [max(min_dur * 0.5, d * scale) for d in durs]
        need = sum(durs)
        if need > vdur:
            k = vdur / need
            durs = [max(0.4, d * k) for d in durs]
            need = sum(durs)

    dens, step = _MN._asr_density(asr, vdur)
    pref = [0.0] * (len(dens) + 1)
    for i, v in enumerate(dens):
        pref[i + 1] = pref[i] + v

    def _score(t0, t1):
        i0 = max(0, int(t0 / step))
        i1 = min(len(dens), max(i0 + 1, int(t1 / step)))
        return pref[min(len(pref) - 1, i1)] - pref[min(len(pref) - 1, i0)]

    # 解说词与 ASR 文本的字符级匹配：片段内台词与解说词共享多少字
    # 解决"只看台词密度导致片段内容和字幕对不上"的问题
    def _bigrams(text):
        text = str(text or '')
        return set(text[i:i+2] for i in range(len(text)-1) if text[i:i+2].strip())

    def _text_match(beat_text, t0, t1):
        if not asr or not beat_text:
            return 0.0
        qbg = _bigrams(beat_text)
        if not qbg:
            return 0.0
        overlap = 0
        total_bg = 0
        for x in asr:
            try:
                s0, s1 = float(x['start']), float(x['end'])
            except Exception:
                continue
            if s1 <= t0 or s0 >= t1:
                continue
            txt = str(x.get('text') or '')
            bg = _bigrams(txt)
            total_bg += len(bg)
            overlap += len(qbg & bg)
        if total_bg == 0:
            return 0.0
        return overlap / total_bg

    # 解说词与 VLM 画面描述的字符级匹配：片段附近的画面内容是否和解说词相关
    def _visual_match(beat_text, t0, t1):
        if not vlm_captions or not beat_text:
            return 0.0
        beat_chars = set(beat_text)
        overlap = 0
        total = 0
        # 查找片段时间范围内及前后各5秒的 VLM 描述
        for ct, cap in vlm_captions:
            if ct < t0 - 5.0 or ct > t1 + 5.0:
                continue
            total += len(cap)
            overlap += sum(1 for c in cap if c in beat_chars)
        if total == 0:
            return 0.0
        return overlap / total

    # ---- 语义匹配素材（2026-11-06「解说↔画面对得上」主攻点）----
    # 把每个场景的 summary+event+keywords(+剧情含义) 拼成可检索文本，
    # 在已匹配的场景窗口内，优先挑「场景内容与这句解说语义最贴合」的起点。
    # 只在窗口内精修（窗口本身来自 DP 全局对齐），不动单调性结构。
    # scene_story/scenes 缺席时 scene_texts 为空 → 行为与旧版逐字节一致。
    scene_texts = _MN._compose_scene_texts(scenes, scene_story) if (scenes and sem_weight) else []

    # 向量语义素材对齐校验：scene_sem=(scene_texts, scene_vecs, text_vecs)。
    # 场景文本必须与上面 _compose_scene_texts 的输出严格等长才可信；
    # 不齐/缺向量 → 忽略向量，行为退回 bigram（与未传 scene_sem 完全一致）。
    _sem_scene_vecs, _sem_text_vecs = None, None
    if scene_sem and sem_weight and scene_texts:
        try:
            _st0, _sv, _tv = scene_sem
            if (_sv and _tv and len(_st0) == len(scene_texts)
                    and len(_sv) == len(_st0) and len(_tv) == len(texts or [])):
                _sem_scene_vecs, _sem_text_vecs = list(_sv), list(_tv)
        except Exception:
            _sem_scene_vecs, _sem_text_vecs = None, None

    def _scene_match(i, t0, t1):
        """候选画面范围 [t0,t1) 与第 i 句解说的语义重合度（0~1）。

        有向量素材：句向量与覆盖场景向量的**最大余弦**（L2 已归一化=点积）——
        口语解说与白描场景之间的同义改写只有向量抓得住；无向量：退回
        bigram 字面重叠（旧行为，逐字节一致）。"""
        if not scene_texts or i >= len(texts or []):
            return 0.0
        beat_text = str(texts[i] or '')
        if not beat_text:
            return 0.0
        if _sem_scene_vecs is not None and i < len(_sem_text_vecs or []):
            qv = _sem_text_vecs[i]
            best = 0.0
            for k, (st, en, _stx) in enumerate(scene_texts):
                if en <= t0:
                    continue
                if st >= t1:
                    break   # 已按 start 排序，后面的场景更晚
                cos = 0.0
                for a, b in zip(qv, _sem_scene_vecs[k]):
                    cos += a * b
                if cos > best:
                    best = cos
            return best   # 负值归 0：不相关不罚，只奖励贴合
        qbg = _bigrams(beat_text)
        if not qbg:
            return 0.0
        best = 0.0
        for st, en, stx in scene_texts:
            if en <= t0:
                continue
            if st >= t1:
                break   # 已按 start 排序，后面的场景更晚
            sbg = _bigrams(stx)
            if not sbg:
                continue
            ov = len(qbg & sbg)
            if ov:
                best = max(best, ov / max(1, len(qbg)))
        return best

    spans = []
    narr_map = []
    # A1：last_end 兼作单调起点游标，首段 lo = max(lo, last_end) 即起点 ≥ content_start
    last_end = content_start

    for i, d in enumerate(durs):
        # 每节拆成多个子片段：节时长 / 目标片段时长，至少 1 个
        n_sub = max(1, int(round(d / _MN.NAR_SEG_TARGET)))
        sub_d = d / n_sub

        # 搜索窗口：优先用场景对齐结果（在匹配的场景范围内选片段），
        # 没有对齐时退回按节数均匀分布（旧行为）。
        if scene_alignment and scenes and i in scene_alignment:
            matched = scene_alignment[i]
            sc_starts = [scenes[s]['start'] for s in matched if 0 <= s < len(scenes)]
            sc_ends = [scenes[s]['end'] for s in matched if 0 <= s < len(scenes)]
            if sc_starts and sc_ends:
                lo = max(0.0, min(sc_starts))
                hi = min(vdur - sub_d, max(sc_ends))
            else:
                center = (i + 0.5) * vdur / n
                half_win = max(d * 2.5, vdur / n * 0.9)
                lo = max(0.0, center - half_win)
                hi = min(vdur - sub_d, center + half_win)
        else:
            center = (i + 0.5) * vdur / n
            half_win = max(d * 2.5, vdur / n * 0.9)
            lo = max(0.0, center - half_win)
            hi = min(vdur - sub_d, center + half_win)
        lo = max(lo, last_end)  # 不与前一节最后一个片段重叠
        if hi < lo:
            # 对齐窗口被前面几句吃光了（多句共用一个高分场景时会发生）。
            # 【不能】退回等比例位置——那会从 228 秒直接跳到几百秒外；
            # 正确做法是从窗口末端就近向后延伸（最多 2 个子片段长），画面保持连续推进。
            #
            # 【必须夹住 last_end】旧写法 min(max(lo,0), vdur-sub_d) 会把起点
            # 拉到 last_end 之前：实测片尾兜底把最后一段从 1195.0 拉回 1192.9，
            # 画面倒着走 2.1 秒。起点只许前进，不许回退。
            lo = max(last_end, min(max(lo, 0.0), max(0.0, vdur - sub_d)))
            hi = min(vdur, lo + sub_d * 2)
            if hi <= lo:
                # 片尾已没有完整 sub_d 的空间：给一小段，画面仍然只向前
                hi = min(vdur, max(lo + 0.2, last_end + 0.2))
                lo = max(0.0, min(lo, hi - 0.2))
            hi = min(max(hi, lo + 0.2), vdur)

        # 窗口均分成 n_sub 个子区，每个子区选台词密度最高的起点。
        win_len = max(0.0, hi - lo)
        # 【多子片段要靠前打包】一节拆多段时若铺满整个窗口，会把窗口尾部
        # 占掉，下一句的对齐窗口被 last_end 堵死、只能往前跳（真机实测：
        # 「女孩转身」的两个子片段落到 230/263，把后面「扑过来/枪响/倒地」
        # 4 句全部顶出 240-258 的扑咬镜头，画面错到汉堡戏）。
        # n_sub=1 仍用全窗口；n_sub>1 只用前 60%，尾部留给后面的句子。
        _usable = win_len if n_sub == 1 else win_len * 0.6
        for j in range(n_sub):
            sub_lo = lo + _usable * j / n_sub
            sub_hi = lo + _usable * (j + 1) / n_sub
            sub_hi = min(sub_hi, vdur - sub_d)
            if spans:
                sub_lo = max(sub_lo, spans[-1][1])  # 不与前一个子片段重叠
            if sub_hi < sub_lo:
                sub_lo = last_end if not spans else spans[-1][1]
                sub_hi = min(vdur - sub_d, sub_lo + sub_d)

            scan_step = max(step, (sub_hi - sub_lo) / 15.0) if sub_hi > sub_lo else step
            beat_txt = str(texts[i] or '') if i < len(texts) else ''

            # 【2026-11-10 强制单调约束 + 局部滑窗/跳跃上限】
            # 候选起点必须落在 [last_end, last_end + max_jump_seconds] 内：
            #   · t < last_end  → 倒流（画面往回走），直接 -inf；
            #   · t > last_end + _jump → 超跳（最佳语义匹配在很久之前也必须
            #     丢弃），直接 -inf，按顺序取最近可用位置。
            t_min = max(sub_lo, last_end)
            t_max = min(sub_hi, last_end + _jump)
            if t_max < t_min:
                # 本子片段窗口整体超出跳跃上限 → 强制裁退到上限边界（最近可用）
                t_min = max(last_end, min(last_end + _jump, vdur - sub_d))
                t_max = t_min
            if _kinds[i] != _MN.KIND_CONCRETE:
                # 非具象句（钩子/引用/留白/过渡）：不描述具体画面，无语义扫描，
                # 直接取最近可用位置——它们参与语义匹配正是画面乱跳的根源。
                best_t, best_s = t_min, -1.0
            else:
                best_t, best_s = t_min, -1.0
                t = t_min
                while t <= t_max + 1e-6:
                    t1 = min(vdur, t + sub_d)
                    density = _score(t, t1)
                    t_match = _text_match(beat_txt, t, t1)
                    # 语义精修：窗口内的候选起点，优先落在「场景内容贴合这句解说」的
                    # 位置（additive 精修项，不压制台词密度主信号；只在 DP 已匹配的
                    # 窗口内做精修）。向量版=余弦相似度（scene_sem），无向量=bigram 保底
                    v_match = _scene_match(i, t, t1)
                    s = density * (1.0 + t_match * 2.0) + sem_weight * v_match
                    if s > best_s + 1e-9:
                        best_s, best_t = s, t
                    t += scan_step
            s0 = min(max(0.0, best_t), max(0.0, vdur - sub_d))
            s1 = min(vdur, s0 + sub_d)
            spans.append((round(s0, 3), round(s1, 3)))
            narr_map.append(i)
            last_end = s1

    return spans, narr_map


_QUOTE_RE = re.compile(r'[\u201c"\u300c\u300e]([^\u201d"\u300d\u300f]{2,80})[\u201d"\u300d\u300f]')


_SPEAK_RE = re.compile(r'(说|问|喊|叫|答|应|道|回|念|嘟囔|低语|开口|写道|写着)[^，。；]{0,8}[：:]?\s*$')


def _quote_is_dialogue(line, pos):
    """引号里是不是「说的话」。

    稿子里的引号有两种用途：引用台词（“我不想交朋友。”）和标注称谓
    （“惩罚者”的制服、“厨房爱尔兰帮”最后一个幸存者）。只有前者该转原声，
    后者转过去会把画面切到八竿子打不着的镜头。判据：引号前紧跟冒号或说话动词。
    """
    head = str(line or '')[:pos]
    tail = head[-14:]
    if '：' in tail or ':' in tail:
        return True
    return bool(_SPEAK_RE.search(tail))


def _quote_texts(line, only_dialogue=True):
    """取出解说句里引用的台词（中文弯引号 / 直角引号 / 英文双引号）。"""
    out = []
    for m in _QUOTE_RE.finditer(str(line or '')):
        t = m.group(1).strip(' ，。、！？…—-')
        if len(t) < 2:
            continue
        if only_dialogue and not _quote_is_dialogue(line, m.start()):
            continue
        out.append(t)
    return out


def _norm_cn(s):
    return re.sub(r'[^\u4e00-\u9fa5A-Za-z0-9]', '', str(s or ''))


def _literal_score(a, b):
    from difflib import SequenceMatcher
    x, y = _norm_cn(a), _norm_cn(b)
    if len(x) < 2 or len(y) < 2:
        return 0.0
    return SequenceMatcher(None, x, y).ratio()


def _score_block(qtext, block):
    """让模型逐条判「是否表达了同一意思」，返回命中条目。

    返回 `None` 表示**这次没有任何可用的模型输出**（本地文字模型与视觉模型的文字
    通道都不可用 / 超时 / 报错），与「问到了但一条都不像」的 `[]` 严格区分。
    调用方据此可以「后端不可用就别再逐块问下去」：判据放宽后（[2.6]）只配了视觉
    模型但服务没起 / 模型没拉的机器会被放行进来，若不分这两种情况就会白等满 8 块。

    【为什么不是「从这些里挑一个」】实测 8B 模型面对「挑一个」时总会硬选一个：
    给 12 条（都不对）它能挑出「每个人都以为你是智障」；给 60 条又过度保守全判 0。
    换成逐条判 0/1 并写死判据（同一件事/同一个态度，措辞沾边不算）后校准正常：
    正确区间命中「但我不是在找同伴」，错误区间返回空。
    """
    import movie_narrator as _MN
    listing = '\n'.join('%d. %s' % (i + 1, c[2]) for i, c in enumerate(block))
    prompt = ('解说稿用一句话概括了某句台词的意思，作者写的是：「%s」\n\n'
              '下面是对白候选。请逐条判断：这一条**是否表达了同样的意思**。\n'
              '判据：说的是同一件事、同一个态度；只是情绪相近或措辞沾边都不算。\n'
              '输出 JSON：{"hit": [命中序号...]}；一条都不像就输出 {"hit": []}。\n\n'
              '候选：\n%s' % (qtext, listing))
    r = _MN._llm_text(prompt, timeout=120)
    if not r:
        return None          # 后端无输出（不是「没有命中」）：让调用方据此早停
    try:
        m = re.search(r'\{.*\}', r, re.S)
        hits = json.loads(m.group(0) if m else r).get('hit') or []
    except Exception:
        return []
    out = []
    for h in hits:
        try:
            i = int(h)
        except Exception:
            continue
        if 1 <= i <= len(block):
            out.append(block[i - 1])
    return out


def _pick_quote_span(qtext, asr, near=None, window=240.0, block_sec=60.0,
                     max_blocks=8, src_dur=None):
    """在台词时间轴里找这句话对应哪一条。返回 (start, end, text, how) 或 None。

    两级匹配，因为稿子是**意译**、字幕是官方译法：
      · 字面：SequenceMatcher 去标点后比对，「我死了，柯蒂斯，你没听说吗？」
        对「我是死了，科提斯，你没听说吗？」能到 0.8+，直接命中；
      · 语义：字面救不了「我不想交朋友」对「但我不是在找同伴」（实词重合 0），
        交给可用的 chat 后端**按 60 秒一块、由近及远**逐块判分，第一块有命中就停。
    宁可漏（照常念稿），不可错（播错画面比念稿更伤）。

    [2.6] 语义档的入口判据必须与 `_llm_text` 的真实能力对齐：`_llm_text`（:1490）自己
    就会在本地文字模型不可用时回退**视觉模型的文字通道**（`_w.vlm_text`），所以只装了
    视觉模型的机器本来也能跑语义匹配。旧判据只认 `_local_model_available()`，把这些机器
    静默挡在门外（实测 `quote_slots` 恒为空 = 功能看着有、其实永不生效）。
    现在改成「任一后端可用即可」，与 `_llm_text` 一致。
    """
    import movie_narrator as _MN
    cands = []
    for a in (asr or []):
        try:
            s0 = float(a.get('start') or 0)
            e0 = float(a.get('end') or 0)
        except Exception:
            continue
        t = str(a.get('text') or '').strip()
        if e0 <= s0 or not t:
            continue
        if src_dur and e0 > float(src_dur) + 0.5:
            continue          # 台词在片外（片源只截了一段）→ 不能引用
        if near is not None and not (near - window <= s0 <= near + window):
            continue
        cands.append((s0, e0, t))
    if not cands:
        return None
    best = max(cands, key=lambda c: _literal_score(qtext, c[2]))
    if _literal_score(qtext, best[2]) >= 0.55:
        return best[0], best[1], best[2], 'literal'
    if not (_MN._local_model_available() or _w.vlm_enabled()):
        return None
    anchor = near if near is not None else cands[len(cands) // 2][0]
    # 由近及远：0, +1, -1, +2, -2 …（单位 block_sec）
    order = [0]
    for k in range(1, max_blocks):
        order += [k, -k]
    order = order[:max_blocks]
    # [2.6] 语义阶段总闸：每次 `_score_block` 都会起一次模型调用（timeout=120s），
    # 一块不中就会继续试下一块。最坏 `max_blocks` 次 × 120s 会把整条合成流程拖到
    # 几十分钟；而搜索是**由近及远**的，越靠后的块越不可能命中，所以给这一句的语义
    # 搜索设一道墙钟预算：超时就停，照常念稿（宁可漏，不可错，也不可让用户干等）。
    # 默认 100s 的取值理由：单次调用最长会烧掉 120s 的 timeout，所以
    #   · 模型活着、几秒就答（正常情况）→ 100s 足够搜满 max_blocks=8 块，语义档不缩水；
    #   · 模型挂住/一直超时（病态情况）→ 第一次 120s 之后就该放弃，而不是再罚 7 次。
    # `NARRATE_QUOTE_BUDGET=0` 可关闭该闸（回到逐块搜到底的旧行为）。
    _budget = 100.0
    try:
        _budget = float(os.environ.get('NARRATE_QUOTE_BUDGET', '100') or 100)
    except Exception:
        _budget = 100.0
    _deadline = (time.time() + _budget) if _budget > 0 else None
    for k in order:
        if _deadline is not None and time.time() > _deadline:
            _log.info('[DIAG] 引用语义匹配：预算 %.0fs 用尽，停止搜索（照常念稿）' % _budget)
            break
        b0 = anchor + k * block_sec
        block = [c for c in cands if b0 <= c[0] < b0 + block_sec]
        if not block:
            continue
        block.sort(key=lambda c: c[0])
        hits = _MN._score_block(qtext, block)
        if hits is None:
            # 后端这次完全没输出（服务没起 / 模型没拉 / 超时）：判据只是配置层
            # （`vlm_enabled()` 不探活），所以这条路会走到这里。再问下一块只是白等，
            # 直接放弃这一句的语义让位，照常念稿。
            _log.info('[DIAG] 引用语义匹配：无可用文本后端，跳过语义让位（照常念稿）')
            return None
        if not hits:
            continue
        hits.sort(key=lambda c: (-_literal_score(qtext, c[2]), abs(c[0] - anchor)))
        pick = hits[0]
        _log.info('[DIAG] 引用语义匹配：「%s」→ 「%s」（%d 条命中）'
                  % (qtext, pick[2][:30], len(hits)))
        return pick[0], pick[1], pick[2], 'semantic'
    return None


def _detect_quote_slots(narr, segs, asr, progress=None, params=None,
                        narr_map=None, src_dur=None):
    """把「引用台词」的解说句标成原声段（整句让位）。

    用户 2026-11-09 定下的口径：稿子里引用台词的那一句**整句不念**，
    直接播原片对应画面与原声，字幕换成该台词的中文——比旁白转述有力。
    返回 {句序号: {text, start, end, how, quote}}，匹配不上就不含该句（照常念稿）。
    """
    slots = {}
    if not asr or not narr:
        return slots
    for i, line in enumerate(narr):
        for q in _quote_texts(line):
            near = None
            try:
                if narr_map and len(narr_map) == len(segs):
                    _idxs = [j for j, g in enumerate(narr_map) if g == i]
                    near = float(segs[_idxs[0]][0]) if _idxs else None
                elif i < len(segs):
                    near = float(segs[i][0])
            except Exception:
                near = None
            hit = _pick_quote_span(q, asr, near=near, src_dur=src_dur)
            if not hit:
                continue
            s0, e0, txt, how = hit
            slots[i] = {'text': txt, 'start': round(s0, 2), 'end': round(e0, 2),
                        'how': how, 'quote': q}
            _log.info('[DIAG] 引用让位 第%d句（%s）:「%s」→ %.1f-%.1fs「%s」'
                      % (i + 1, how, q[:20], s0, e0, txt[:30]))
            break
    if slots and progress is not None:
        progress['quote_slots'] = slots
        progress['phase'] = '引用台词转原声 %d 处' % len(slots)
    return slots


def _event_anchored_spans(segs, narr, asr, voice_durs, tail=0.35, cap=1.15):
    """v1.4.86 句级事件锚点：画面窗口从「这句解说描述的事件时刻」开始。

    此前窗口=整场戏从头起播：解说到工地砸墙时，画面还在演前一场的勒人
    （2026-11-14 主人实测「解说词都对不上画面」的收尾一块）。DP 只保证
    句子↔场景段对应，段内的具体时刻由本函数定：在段内找「与这句解说文本
    最匹配的字幕台词 cue」（字幕是地面真值），窗口从那条台词的时刻起播——
    解说提到狙击收尾，画面就从狙击那一刻演。

    窗口长度 = 配音×cap（cap=1.15 与 ±15% 微变速窗一致）+ 气口余量由既有
    补长遍兜底。无 cue/无匹配/无配音的段保持原窗口起点；相邻窗口重叠时
    按顺序右移；锚点贴近段尾时窗口向前收（保留段尾的事件时刻）。

    segs: [(start, end)] 场景段（源片时间轴）；narr: 每段解说词；
    asr: 字幕台词段（地面真值）；voice_durs: {句序号: 配音秒数}。
    返回新的 segs（不原地改）。任何异常由调用方兜底沿用 fit 结果。
    """
    cues = []
    for s in (asr or []):
        try:
            cs = float(s.get('start', 0) or 0)
            ce = float(s.get('end', 0) or 0)
            ct = str(s.get('text', '') or '')
        except Exception:
            continue
        if ct and ce > cs:
            cues.append((cs, ce, ct))
    out = []
    prev_end = 0.0
    for i, (a, b) in enumerate(list(segs or [])):
        a, b = float(a), float(b)
        v = float((voice_durs or {}).get(i) or 0.0)
        txt = str((narr or [''])[i] or '') if i < len(narr or []) else ''
        if v <= 0 or not txt.strip() or not cues:
            out.append((a, b))
            prev_end = max(prev_end, b)
            continue
        # 段内候选台词：与本句解说文本最像的那条（_literal_score 即 DP 同款打分）
        best_start, best_s = None, 0.0
        for cs, ce, ct in cues:
            if cs < a - 0.5 or cs >= b:
                continue
            s = _literal_score(txt, ct)
            if s > best_s:
                best_s, best_start = s, cs
        win = max(v * cap, v + 0.1)
        start = a
        if best_start is not None and best_s >= 0.12:
            start = min(max(best_start, a), max(a, b - 0.5))
        end = start + win
        # 相邻窗口重叠：按顺序右移（打乱即重叠，重叠=两句叠声）
        if start < prev_end:
            start = prev_end
            end = start + win
        # 锚点贴近段尾：窗口向前收，保留段尾的事件时刻
        if end > b and b - a >= win:
            start, end = b - win, b
        elif end > b:
            end = min(b, start + max(0.5, v))
        out.append((round(start, 3), round(end, 3)))
        prev_end = end
    return out
