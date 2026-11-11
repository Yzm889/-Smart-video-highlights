# -*- coding: utf-8 -*-
"""画面索引（台词吸附抽样 + 批量 VLM 场景理解 + 断点续跑）——由 movie_narrator.py 拆出（v1.4.41）。

搬运范围（原 2845-3525 行，逐字搬运、零逻辑改动）：
    _dialogue_anchor_times / NARR_TIERS / NARR_TIER_DEFAULT / NARR_TIER_ORDER /
    narr_tier / _TIER_FIELD_SPEC / _tier_field_spec / _detect_opening /
    _vlm_sample_timeline / _vlm_sample_captions

约定与 movie_narrator 一致：宿主符号（vlm_enabled/vlm_ping/OUTDIR/vlm_chat）经
_w.<name> 晚绑定（本文件照抄 _host() 模式，拿到的是同一个 webui_server 模块对象）；
公共符号由 movie_narrator 尾部 re-export 并回注宿主，调用方零改动。

【依赖解析规则——拆分成败点，勿改】凡定义在 movie_narrator 命名空间的符号
（含它 from ai_providers / cache_utils / ffmpeg_utils 按值 import 的名字），一律经
函数内惰性 `import movie_narrator as _MN` 后以 _MN.X(...) 调用期属性查找：
测试普遍 monkeypatch.setattr(M, 'X', ...) 打在主模块命名空间上，函数搬家后
__globals__ 换成了本模块的 dict，裸名会让全部 patch 静默打空。
**特例 NARR_TIERS**：test_vlm_resume 会整体重绑 M.NARR_TIERS（改 sim 阈值），
档位表必须走 _MN 属性查找才能看到重绑——这是行为钉，不是风格偏好。
"""
import logging, os, sys, re, time

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


def _dialogue_anchor_times(asr, vdur, interval, quality):
    """把固定网格的抽样点**吸附到台词起点**，让 VLM 永远看到「剧情在推进」的那一刻。

    【为什么这是突破点】旧抽样是纯网格（每 interval 秒一个点），网格与剧情毫无因果
    关系：抽到黑屏、过场、空镜就是「空无一人」，抽到长静默就是「无台词区间」。
    VLM 拿这些当素材写出来的剧情必然对不上。而字幕/ASR 的 start 起点，恰好是
    「有人开口、剧情推进」的时刻——同样多的帧，看的地方完全不一样。

    【为什么是「吸附」而不是「加密」】最初的写法是「每个台词起点都抽一帧」，
    真机一测就废：台词密集时点数暴涨（VLM 阶段从 3 分钟变 20 分钟），而台词稀疏
    （一条字幕覆盖全片）时点数反而掉到个位数，**覆盖率比原来还差**。
    吸附法两个毛病都没有：
      - 点数与网格完全相同 → VLM 成本不变、耗时不变；
      - 每个点只在 ±0.45*interval 内移动 → **最大空档与网格一致，覆盖率不倒退**；
      - 落点却从「随机位置」变成「有人说话的时刻」→ 画面信息量本质提升。

    返回 (times, tag)。无台词 / 无有效起点时返回 ([], '')，调用方退回纯网格。
    """
    if not asr:
        return [], ''
    starts = []
    for seg in asr:
        try:
            st = float(seg.get('start', 0) or 0)
            txt = str(seg.get('text', '') or '').strip()
        except (ValueError, TypeError):
            continue
        if txt and 0 <= st < vdur:
            starts.append(st)
    if not starts:
        return [], ''
    starts.sort()

    grid = []
    t = interval / 2.0
    while t < vdur:
        grid.append(t)
        t += interval
    if not grid:
        # 极短素材（interval > vdur）：网格为空就没得吸附。
        # 退回调用方的短片段保护逻辑（会按 vdur/4 重新算间隔），不在这里硬造点。
        return [], ''

    # 吸附半径：半个网格的 90%。留 10% 余量，避免两个相邻点抢同一个台词起点。
    rad = interval * 0.45
    min_gap = max(1.0, interval * 0.5)
    taken = set()
    out = []
    for g in grid:
        cand = [s for s in starts if abs(s - g) <= rad]
        pick = None
        for s in sorted(cand, key=lambda x: abs(x - g)):
            # +0.4s：台词起点那一帧往往还是上一个镜头，稍等让画面切过去
            ts = round(min(float(vdur) - 0.05, s + 0.4), 3)
            if ts in taken:
                continue
            pick = ts
            break
        if pick is None:
            pick = round(g, 3)          # 附近没人说话 → 留在网格原位（空镜也得看）
        if out and (pick - out[-1]) < min_gap:
            pick = round(g, 3)          # 吸附后与上一点太近 → 退回网格，保证间距
        if out and (pick - out[-1]) < min_gap:
            continue                    # 仍太近（网格本身很密）→ 丢弃，宁可少不可叠
        out.append(pick)
        taken.add(pick)
    return [x for x in out if 0 < x < vdur], 'snap'


# ============================================================================
# 画面理解四档（用户 2026-11-09 定：给时间/质量一个明确的档位选择）
# ----------------------------------------------------------------------------
# 决定成片观感的关键不是「看了多少帧」，而是两件事：
#   ① 有没有覆盖**没有台词的镜头**（打戏、对视、吻戏）——台词-only 必然把它们剪掉；
#   ② 每帧让模型写多少字——decode 是瓶颈，写 5 个字段比写 1 个字段贵 3 倍。
# 所以档位之间的差别是「覆盖 × 字量」的组合，不是简单的帧数加减。
#
# 各档实际耗时（20 分钟素材、RTX 3060 实测口径）：
#   极速 ~2 分钟 / 平衡 ~8 分钟 / 质量 ~23 分钟 / 极致 ~40 分钟
# 注意：配音恒为 7 分钟地板（37 句 × 12 秒），四档都一样。
# ============================================================================
NARR_TIERS = {
    # 只看台词附近：画面先不管，用来快速调文案与配音
    'speed':    {'label': '极速', 'interval': 20.0, 'max_points': 40,
                 'width': 448, 'batch': 8, 'fields': 1, 'tok_per_frame': 70,
                 'story': False, 'dialogue_filter': True,  'action_weight': 1.6,
                 'sim': 0.60},
    # 全片覆盖 + 精简字段：静默段一个不漏，日常出片档
    'balanced': {'label': '平衡', 'interval': 12.0, 'max_points': 300,
                 'width': 512, 'batch': 6, 'fields': 3, 'tok_per_frame': 110,
                 'story': True,  'dialogue_filter': False, 'action_weight': 1.6,
                 'sim': 0.85},
    # 更密抽样 + 5 字段：动作节奏快的片子
    'quality':  {'label': '质量', 'interval': 8.0,  'max_points': 300,
                 'width': 640, 'batch': 4, 'fields': 5, 'tok_per_frame': 220,
                 'story': True,  'dialogue_filter': False, 'action_weight': 1.6,
                 'sim': 0.93},
    # 5 秒密度 + 动作匹配权重翻倍：要发出去的成片
    'ultra':    {'label': '极致', 'interval': 5.0,  'max_points': 300,
                 'width': 720, 'batch': 4, 'fields': 5, 'tok_per_frame': 220,
                 'story': True,  'dialogue_filter': False, 'action_weight': 2.0,
                 'sim': 0.97},
}
NARR_TIER_DEFAULT = 'balanced'
NARR_TIER_ORDER = ('speed', 'balanced', 'quality', 'ultra')


def narr_tier(params=None):
    """解析当前档位：params.narrTier > 环境变量 NARRATE_TIER > 默认 balanced。"""
    import movie_narrator as _MN
    v = str((params or {}).get('narrTier') or '').strip().lower()
    if v in _MN.NARR_TIERS:
        return v
    v = str(os.environ.get('NARRATE_TIER') or '').strip().lower()
    if v in _MN.NARR_TIERS:
        return v
    return _MN.NARR_TIER_DEFAULT


# 每档让模型写哪些字段。**event 任何档位都不能省**：它是唯一能匹配
# 「他们深情地吻在了一起」这类解说词的动作信号（location 只是地点，characters 只是人名）。
_TIER_FIELD_SPEC = {
    1: 'event（人物在做什么动作、什么情绪；不要写景别，不要复述台词）',
    3: 'location（地点）、characters（画面里的人物）、'
       'event（人物在做什么动作、什么情绪；不要复述台词）',
    5: 'location（地点）、characters（主要人物）、event（正在发生什么事）、'
       'dialogue（这段台词的核心内容，没有则留空）、summary（画面整体概括，不超过30字）',
}


def _tier_field_spec(fields):
    return _TIER_FIELD_SPEC.get(int(fields or 5)) or _TIER_FIELD_SPEC[5]


def _detect_opening(scene_descs, asr, vdur, opening_window=90.0):
    """A1 片头识别：识别「片头 logo/主题曲」区段，返回正片起点 content_start。

    只检查前 opening_window(90) 秒，三条同时成立才算片头：
      条件① 无成句对白（去标点后 ≥6 字才算成句）；
      条件② 相邻采样点 VLM 描述相似度均值 > 0.95（logo 定格反复同描）；
      条件③ 描述里命中 "logo/片头/title" 等特征词。
    三条件全满足 → 返回最后一个片头采样点 end 之后的时间点；否则返回 0（不跳过）。
    检测过程任何异常 → 返回 0，绝不让片头判断破坏出片主流程。
    """
    import difflib as _dl
    try:
        if not scene_descs or not vdur or vdur <= 0:
            return 0.0
        # 只看前 90 秒内的采样点
        _win = [s for s in scene_descs if float(s.get('start', 0)) < opening_window]
        if len(_win) < 2:
            return 0.0   # 片头窗口采样点不足，无法可靠判定 → 不跳过

        # 条件①：前 90 秒无成句对白（去标点后 <6 字的碎片不算「成句」）
        def _sentence(txt):
            _t = re.sub(r'[^\w]', '', str(txt or ''))
            return len(_t) >= 6
        for seg in (asr or []):
            try:
                if float(seg.get('start', 0)) < opening_window and _sentence(seg.get('text')):
                    return 0.0   # 有对白 → 不是纯片头
            except (ValueError, TypeError):
                continue

        # 条件②：相邻 VLM 描述相似度均值 > 0.95
        def _desc(s):
            return ('%s %s' % (s.get('event', ''), s.get('summary', ''))).strip()
        _sims = []
        for a, b in zip(_win, _win[1:]):
            da, db = _desc(a), _desc(b)
            if not da or not db:
                _sims.append(0.0)   # 描述缺失按不相似处理，避免空串撞出高相似
                continue
            _sims.append(_dl.SequenceMatcher(None, da, db).ratio())
        if not _sims or (sum(_sims) / len(_sims)) <= 0.95:
            return 0.0

        # 条件③：命中片头特征词
        _kw = ('logo', '片头', 'title', '标题', 'intro', '字幕')
        _joined = ' '.join(_desc(s) for s in _win).lower()
        if not any(k in _joined for k in _kw):
            return 0.0

        # 三条件全满足：正片起点 = 最后一个片头采样点之后
        return max(0.0, max(float(s.get('end', 0)) for s in _win))
    except Exception:
        return 0.0   # 检测失败默认不跳过


def _vlm_sample_timeline(video_path, vdur, asr, run_dir, progress=None, interval=28.0,
                         cancel_check=None, quality=None, tier=None):
    """时间轴驱动建立画面索引，跳过场景检测。

    【抽样策略】有台词时间轴时**按台词驱动**（见 _dialogue_anchor_times）：
    每个说话时刻都看一眼，长静默补点。没有台词时退回旧的固定网格。
    这样「画面理解」看到的是「剧情正在推进的瞬间」，而不是随机网格上的空镜。

    【质量模式·默认】全片覆盖：每个抽样点都送 VLM（台词只作为附加信息，不再是
    「要不要看这一帧」的开关），间隔收紧到 ≥12s，模型常驻显存。
    【快速模式】NARRATE_FAST=1：仅分析 ±12 秒内有台词的抽样点，间隔 ≥15s。
    返回格式与 _vlm_describe_scenes 完全一致，下游无需改动。

    【断点续跑】每批分析完立即原子写入 run_dir/vlm_progress.json；
    重新开始时按「视频指纹+模型+抽样间隔」校验后跳过已完成的场景，
    全部完成并落缓存后删除进度文件。中途取消不丢已分析结果。"""
    import movie_narrator as _MN
    if not _w.vlm_enabled():
        _log.warning('[A1] VLM 未启用（ai_config.json 的 vlm.enabled=false 或未配置），画面索引为空；'
                     '解说配画面将退化为纯位置均匀兜底，声画可能系统性错位。请在 AI 配置里启用本地视觉模型。')
        return []
    try:
        _ping = _w.vlm_ping()
        if not _ping[0]:
            _log.warning('[A1] VLM 探活失败（%s），画面索引为空；解说配画面将退化为纯位置均匀兜底。',
                         (_ping[1] if len(_ping) > 1 else '探活未通过'))
            return []
    except Exception as _e:
        _log.warning('[A1] VLM 探活异常（%s），画面索引为空；解说配画面将退化为纯位置均匀兜底。', _e)
        return []

    # 路径修复：progress_state 里的 run_dir 可能是相对路径，转成绝对路径
    # （与 _generate_all_tts 同样处理），否则进度文件会写到项目根目录。
    if run_dir and not os.path.isabs(run_dir):
        run_dir = os.path.join(_w.OUTDIR, run_dir)

    # === 档位（见 NARR_TIERS）===
    # quality=False 是旧布尔入口（NARRATE_FAST=1），等价于「只看台词附近」。
    _tier = tier or narr_tier()
    if quality is False and tier is None:
        _tier = 'speed'
    if _tier not in _MN.NARR_TIERS:
        _tier = _MN.NARR_TIER_DEFAULT
    _T = _MN.NARR_TIERS[_tier]
    quality = not _T['dialogue_filter']      # 旧变量：True 表示全片覆盖

    # 抽样间隔：档位基准间隔与「总点数上限」取较大者。
    # 上限的作用是别让超长片把 VLM 拖死（2 小时片在 quality 档会到 900 点）。
    interval = max(float(_T['interval']), float(vdur) / float(_T['max_points']))
    # 短片段保护：interval 大于片长会让抽样点数为 0，画面理解直接空手而归。
    if interval >= float(vdur):
        interval = max(1.0, float(vdur) / 4.0)
    _fw = int(_T['width'])

    # 抽样点规划必须**早于缓存检查**：抽样策略本身（台词吸附 vs 固定网格）
    # 决定产出结构，缓存键要带上它，否则换了策略仍会命中旧缓存、改动等于没做。
    # 吸附法点数与网格一致（见 _dialogue_anchor_times 的说明），无需再封顶抽稀。
    sample_times, _anchor_tag = _dialogue_anchor_times(asr, vdur, interval, quality)
    _anchor_mode = bool(sample_times)
    if not _anchor_mode:
        t = interval / 2.0  # 从中间开始，避免片头黑屏
        while t < vdur:
            sample_times.append(t)
            t += interval
    else:
        _log.info('[DIAG] 台词吸附抽样: %d个时间点（来源=%s）'
                  % (len(sample_times), _anchor_tag))

    # 缓存检查
    try:
        vlm_model = _MN.vlm_cfg().get('model', 'default')
    except Exception:
        vlm_model = 'default'
    # 键里必须带抽样策略：'grid' 与 'sub' 产出的时间点集合完全不同，
    # 共用键会让「改用台词对齐」这个改动被旧缓存整个短路掉。
    _plan_tag = 'sub' if _anchor_mode else 'grid'
    # 缓存键必须带档位：不同档位的复用阈值/字段集不同，
    # 只按「模型+间隔+抽样方式」会互相串用（极速与平衡间隔接近时尤其危险）。
    cache_key = _MN._video_cache_key(
        video_path, f'vlm_sample_{vlm_model}_{int(interval)}_{_plan_tag}_{_tier}')
    cached = _MN._cache_load(cache_key)
    if cached:
        _log.info(f'[DIAG] VLM抽样命中缓存: {len(cached)}个时间点')
        if progress:
            progress['phase'] = '画面索引（缓存命中）'
            progress['pct'] = 46
        return cached

    # [P0-1 方案1.5] 抽帧产物缓存目录：按视频指纹+间隔复用（原 frame_dir = run_dir/sample_frames，换文案会重新抽帧）
    frame_dir = _MN._sample_frame_cache_dir(video_path, interval, width=_fw)
    results = []
    # 字段随档位变化：极速只写动作，质量档才要 5 项。
    # event 一律强调「动作 + 情绪」——「他们深情地吻在了一起」这类解说词
    # 只能靠动作词匹配上；写成「两名男子在室内」就永远匹配不到。
    _fspec = _tier_field_spec(_T['fields'])
    sys_prompt = ('你是影视场景分析助手。根据画面和提供的台词，用JSON格式结构化描述这个时间点的画面。'
                  '字段：%s。只输出JSON。' % _fspec)

    # 抽样点已在缓存检查前规划完毕（见上方 sample_times / _anchor_mode）。

    # 预计算每个抽样点附近是否有台词（±12秒窗口）
    def _near_dialogue(ts):
        if not asr:
            return True
        for seg in asr:
            try:
                t0 = float(seg.get('start', 0))
                t1 = float(seg.get('end', t0 + 1))
                txt = str(seg.get('text', '')).strip()
                if txt and (t0 - 12 <= ts <= t1 + 12):
                    return True
            except (ValueError, TypeError):
                continue
        return False

    # 第一遍：按「视频指纹+间隔」检查抽帧产物缓存，未命中则单次 ffmpeg 管道连续抽帧
    need_vlm = []  # [(idx, ts, fp)]
    skip_count = 0
    total_samples = len(sample_times)

    if progress:
        _ph = ('抽取画面帧（按台词对齐，共%d点）' % total_samples) if _anchor_mode \
            else ('抽取画面帧（单次管道，共%d点）' % total_samples)
        progress['phase'] = _ph
        progress['pct'] = 40
    if _MN._sample_frame_cache_ready(frame_dir, total_samples):
        # [P0-1 方案1.5] 抽帧产物缓存命中：直接复用帧文件，免 ffmpeg
        _log.info(f'[DIAG] 抽帧产物缓存命中: {total_samples}帧直接复用(免ffmpeg)')
    else:
        # [P0-1 方案1.1] 原实现为每个有台词抽样点独立 ffmpeg -ss -frames:v 1（约60次子进程）；
        # 后改为一次 `fps=1/interval` 连续抽帧。从 interval/2 起按网格输出，
        # 第 k 帧对应 t=interval/2+k*interval 即 sample_times[k]（与原抽样点集合一致）。
        # VFR 时间戳偏差 ≤0.5s，台词窗口±12s（_near_dialogue）可完全容忍。
        #
        # 【台词驱动时不能用 fps 网格】锚点时间不均匀（台词密集处间隔 1.6s、静默处
        # 30s+），fps 网格抽出来的帧和 sample_times 对不上，VLM 会把 A 镜头的话
        # 安到 B 镜头上——比不抽更糟。此时走逐点 seek 抽帧（点数已封顶，代价可控）。
        if os.path.isdir(frame_dir):
            for _fn in os.listdir(frame_dir):
                try:
                    os.remove(os.path.join(frame_dir, _fn))
                except OSError:
                    pass
        os.makedirs(frame_dir, exist_ok=True)
        if _anchor_mode:
            for idx, ts in enumerate(sample_times):
                fp = os.path.join(frame_dir, 'sample_%04d.jpg' % idx)
                _MN.ffmpeg_run(['-y', '-ss', '%.3f' % ts, '-i', video_path,
                                '-frames:v', '1', '-vf', 'scale=min(iw\\,%d):-2' % _fw,
                                '-q:v', '4', '-an', fp])
            n_got = sum(1 for i in range(total_samples)
                        if os.path.exists(os.path.join(frame_dir, 'sample_%04d.jpg' % i)))
            rc = 0 if n_got == total_samples else 1
        else:
            rc, _o, _e = _MN.ffmpeg_run(['-y', '-ss', '%.3f' % (interval / 2.0), '-i', video_path,
                                         '-vf', 'fps=1/%.6f,scale=min(iw\\,%d):-2' % (interval, _fw),
                                         '-q:v', '4', '-an', '-start_number', '0',
                                         os.path.join(frame_dir, 'sample_%04d.jpg')])
            n_got = sum(1 for i in range(total_samples)
                        if os.path.exists(os.path.join(frame_dir, 'sample_%04d.jpg' % i)))
        # 清理末尾多余帧（fps 网格若在片尾多输出一帧，从 0..N-1 之外移除，保持帧集与抽样点一一对应）
        for i in range(total_samples, total_samples + 8):
            _extra = os.path.join(frame_dir, 'sample_%04d.jpg' % i)
            if os.path.exists(_extra):
                try:
                    os.remove(_extra)
                except OSError:
                    pass
        if rc != 0 or n_got != total_samples:
            # 管道抽帧异常（失败或帧数不符，可能发生 seek 错位）时回退逐点抽帧，
            # 保证抽样点全覆盖与功能等价（回退路径按帧序号逐点覆盖，时间点与 sample_times 严格一致）
            for idx in range(total_samples):
                ts = sample_times[idx]
                fp = os.path.join(frame_dir, 'sample_%04d.jpg' % idx)
                r2, _o2, _e_raw = _MN.ffmpeg_run(['-y', '-ss', '%.3f' % ts, '-i', video_path,
                                               '-frames:v', '1', '-vf', 'scale=min(iw\\,%d):-2' % _fw,
                                               '-q:v', '4', '-an', fp])
        _MN._sample_frame_cache_mark(frame_dir, total_samples)
        _MN._sample_frame_cache_trim()

    # 时间窗：网格模式保持旧公式（ts ± interval/2），锚点模式取相邻采样点的中点，
    # 保证区间首尾相接、不重叠也不错位（重叠会让同一句台词被两段"看见"，
    # 空隙会让中间的剧情从未被描述）。
    def _win(ts, idx):
        if not _anchor_mode:
            return max(0, ts - interval / 2), min(vdur, ts + interval / 2)
        lo = (sample_times[idx - 1] + ts) / 2.0 if idx > 0 else max(0.0, ts - interval / 2)
        hi = (ts + sample_times[idx + 1]) / 2.0 if idx + 1 < len(sample_times) \
            else min(float(vdur), ts + interval / 2)
        return max(0.0, lo), min(float(vdur), hi)

    # 台词过滤：
    #   质量模式 → 不做过滤，每个抽样点都进 need_vlm（台词仅随 prompt 附带）。
    #              理由：动作戏/空镜/表情特写往往没有台词，但恰恰是解说最需要讲的部分；
    #              以「有没有台词」决定「要不要看画面」，会让全片一半以上的画面从未被理解。
    #   快速模式 → 保留旧行为，仅分析 ±12 秒内有台词的抽样点。
    for idx, ts in enumerate(sample_times):
        _has_dlg = bool(_near_dialogue(ts))
        if not _has_dlg and not quality:
            skip_count += 1
            _ws, _we = _win(ts, idx)
            results.append({'start': _ws, 'end': _we,
                            'location': '', 'characters': '', 'event': '',
                            'dialogue': '', 'summary': '无台词区间'})
        else:
            fp = os.path.join(frame_dir, 'sample_%04d.jpg' % idx)
            if os.path.exists(fp):
                need_vlm.append((idx, ts, fp))
            else:
                _ws, _we = _win(ts, idx)
                results.append({'start': _ws, 'end': _we,
                                'location': '', 'characters': '', 'event': '',
                                'dialogue': '', 'summary': ''})

    # === 帧间差分聚类：相似帧只分析一次，结果广播复用 ===
    # 实现：按时间序比较相邻帧的灰度缩略图相似度；同组只取代表帧做VLM，
    # 分析结果复用到组内所有帧（start/end保留各自帧的时间窗口）。
    def _frame_similarity(fp1, fp2):
        """快速灰度相似度 0-1（1=完全相同）。失败返回0强制重分析。"""
        try:
            from PIL import Image
            sz = (64, 64)
            im1 = Image.open(fp1).resize(sz).convert('L')
            im2 = Image.open(fp2).resize(sz).convert('L')
            p1, p2 = im1.getdata(), im2.getdata()
            diff = sum(abs(a - b) for a, b in zip(p1, p2)) / (sz[0] * sz[1])
            return max(0.0, 1.0 - diff / 255.0)
        except Exception:
            return 0.0

    # 相似帧复用阈值：**这才是「覆盖 vs 效率」真正的旋钮**。
    # 相邻帧相似度 ≥ 阈值就复用上一帧的描述，不另花一次 VLM 调用。
    # 0.85 很省（实测 20 分钟片 100 个点只发了 4 次请求），但静态对话/片头动画
    # 会把**细微动作**一起抹掉——一个接吻、一个眼神变化就此消失，正是用户要的
    # 「打戏、吻戏」被漏掉的原因。档位越高阈值越严：0.60 → 0.85 → 0.93 → 0.97。
    _SIM_THRESHOLD = float(_T.get('sim', 0.85))
    _reuse = {}          # idx -> leader_idx（当前帧复用leader的分析结果）
    _key_frames = []     # 需要VLM分析的关键帧列表（子集 of need_vlm）
    for _item in need_vlm:
        _idx, _ts, _fp = _item
        if _key_frames and _frame_similarity(_key_frames[-1][2], _fp) >= _SIM_THRESHOLD:
            _reuse[_idx] = _key_frames[-1][0]  # 复用前一个关键帧的结果
        else:
            _reuse[_idx] = _idx  # 自己是leader
            _key_frames.append(_item)
    _vlm_saved = len(need_vlm) - len(_key_frames)
    _log.info('[DIAG] 画面索引开始: %d个抽样点 → %d个关键帧（复用省%d帧）'
              % (len(sample_times), len(_key_frames), _vlm_saved))

    # === 断点续跑标识：用「场景起始时间」而非索引，视频/间隔变化都不会串号 ===
    def _start_key_of(ts, idx=None):
        if _anchor_mode and idx is not None:
            return '%.3f' % _win(ts, idx)[0]
        return '%.3f' % max(0.0, float(ts) - interval / 2.0)

    _key_of_idx = {idx: _start_key_of(ts, idx) for idx, ts in enumerate(sample_times)}

    # 第二遍：批量VLM调用
    #  质量模式：4帧/批（单批输出更短、更稳），模型常驻显存 30 分钟，超时给足。
    #            keep_alive 是这里的命门——之前每次调用 keep_alive=0 会把 6GB 权重卸掉，
    #            下一批重新加载 >80s，而超时只有 30s，于是「几乎每批必失败」。
    #  快速模式：沿用 8 帧/批，但超时从 30s 提到 90s（30s 对 8 帧在 3060 上本就不现实）。
    # 【v1.4.38】快速模式同样常驻 '30m'：keep_alive=0 意味着**每一批**都重新从磁盘
    #  加载 6GB 权重再卸掉（v1.4.34 真机取证 43 个 Ollama 事件的主要来源）。清理不靠
    #  keep_alive——调用方（_narrate_by_plot）的 finally vlm_unload() 兜底，无论正常
    #  结束、取消还是异常都会把 VLM 请出去，不会占着显存拖累后面的写稿阶段。
    vlm_count = 0
    batch_size = int(_T['batch'])
    _VLM_KEEP = '30m'
    _VLM_TO_MULTI = 300 if quality else 90
    _VLM_TO_SINGLE = 180 if quality else 60

    def _batch_prompt(n):
        return ('你是影视场景分析助手。以下按时间顺序给出%d张画面帧。'
                '请对每张帧分别用JSON描述，帧之间用---分隔。'
                '每个JSON字段：%s。'
                '没有台词的帧仍须描述画面里人物在做什么动作。'
                '只输出JSON和---分隔符。' % (n, _fspec))

    def _call_frames(_frames):
        """对一组帧发起一次 VLM 调用，返回与 _frames 等长的 dict 列表；失败抛异常。"""
        if len(_frames) == 1:
            r = _w.vlm_chat(_frames[0],
                            '请用JSON描述这个画面，字段：%s' % _fspec,
                            system=sys_prompt, timeout=_VLM_TO_SINGLE, keep_alive=_VLM_KEEP)
            return [_MN._extract_json_obj(r) or {}]
        r = _MN.vlm_chat_multi(_frames, _batch_prompt(len(_frames)), system=sys_prompt,
                               timeout=_VLM_TO_MULTI, keep_alive=_VLM_KEEP,
                               max_tokens=max(240, int(_T['tok_per_frame']) * len(_frames)))
        parts = (r or '').split('---')
        objs = [_MN._extract_json_obj(p) or {} for p in parts[:len(_frames)]]
        while len(objs) < len(_frames):
            objs.append({})
        return objs

    progress_file = os.path.join(run_dir, 'vlm_progress.json') if run_dir else None
    _analyzed = {}   # start_key -> result_fields（不含start/end）
    _failed = set()  # 本轮调用抛异常的帧：不落进度文件，下次续跑会重试

    # --- 读取上次未完成的进度（指纹校验：换视频/换模型/换间隔则作废）---
    _resumed = 0
    if progress_file and os.path.exists(progress_file):
        _p = _MN._load_json_file(progress_file) or {}
        if str(_p.get('fingerprint', '')) == str(cache_key):
            for k, v in (_p.get('results') or {}).items():
                if isinstance(v, dict):
                    _analyzed[str(k)] = {
                        'location': str(v.get('location', ''))[:50],
                        'characters': str(v.get('characters', ''))[:80],
                        'event': str(v.get('event', ''))[:100],
                        'dialogue': str(v.get('dialogue', ''))[:100],
                        'summary': str(v.get('summary', ''))[:60],
                    }
            _resumed = len(_analyzed)
            if _resumed:
                _log.info('[DIAG] VLM断点续跑: 恢复%d个已完成场景，不再重跑' % _resumed)
        else:
            _log.info('[DIAG] VLM断点续跑: 指纹不匹配（换了视频/模型/抽样间隔），忽略旧进度')

    _pending_frames = [f for f in _key_frames if _key_of_idx.get(f[0]) not in _analyzed]
    total_key = len(_key_frames)
    _resume_tag = ('（断点续跑，已完成%d）' % _resumed) if _resumed else ''
    _all_done = True
    # 熔断：连续 2 批全失败说明 VLM 已不可用（OOM / 服务挂掉 / 网络断）。
    # 此时必须放弃逐帧重试，否则每批再多跑 8 次 180s 超时，整任务会被拖成几小时。
    _consec_fail = 0
    _RETRY_MAX_CONSEC = 2
    _est_batches = (len(_pending_frames) + batch_size - 1) // batch_size
    for bi in range(0, len(_pending_frames), batch_size):
        # 协作式取消：用户点「停止」时立即退出，已分析的部分保留在进度文件里
        if _MN._aborted():
            _all_done = False
            _log.info('[DIAG] VLM断点续跑: 收到取消信号，已保留%d个场景进度' % len(_analyzed))
            break
        # S1: 预热让位钩子——正式任务接管时放弃剩余批次（进度保留在进度文件，下次可续跑）
        if cancel_check is not None and cancel_check():
            _all_done = False
            _log.info('[DIAG] VLM断点续跑: 预热让位，已保留%d个场景进度' % len(_analyzed))
            break
        batch = _pending_frames[bi:bi + batch_size]
        t_start = batch[0][1]
        t_end = batch[-1][1]
        def _fmt_t(s):
            m, s = divmod(int(s), 60)
            return '%02d:%02d' % (m, s)
        if progress:
            progress['phase'] = '场景理解 %d/%d（%s-%s，VLM推理中…）%s' % (
                len(_analyzed), total_key, _fmt_t(t_start), _fmt_t(t_end), _resume_tag)
            progress['pct'] = 44 + int(6 * len(_analyzed) / max(1, total_key))
        frames = [b[2] for b in batch]
        vlm_count += 1
        if vlm_count % 5 == 1:
            _log.info('[DIAG] 场景理解 %d/%d 批（已完成%d帧）'
                      % (vlm_count, _est_batches, len(_analyzed)))
        objs = None
        try:
            objs = _call_frames(frames)
            _consec_fail = 0
        except Exception as _e1:
            _log.info('[DIAG] VLM批量失败(%d帧): %s' % (len(frames), _e1))
            _consec_fail += 1
            # 批次失败降级：逐帧重试。单帧 prompt 更短、显存压力更小，
            # 常能在批调用超时的情况下拿到结果；仍失败的帧记为 None 交给 _failed。
            # 但连续多批全失败说明 VLM 已不可用，此时不再逐帧重试（否则每批多跑
            # len(frames) 次超时，任务会被拖成几小时）。
            if len(frames) > 1 and not _MN._aborted() and _consec_fail <= _RETRY_MAX_CONSEC:
                objs = []
                for _f in frames:
                    if _MN._aborted():
                        objs.extend([None] * (len(frames) - len(objs)))
                        break
                    try:
                        objs.extend(_call_frames([_f]))
                    except Exception as _e2:
                        _log.info('[DIAG] VLM单帧重试失败: %s' % _e2)
                        objs.append(None)
                _n_ok = sum(1 for o in objs if o)
                _log.info('[DIAG] VLM逐帧重试: %d/%d 成功' % (_n_ok, len(frames)))
                if _n_ok:
                    _consec_fail = 0
            else:
                if _consec_fail == _RETRY_MAX_CONSEC + 1:
                    _log.info('[DIAG] VLM 连续%d批不可用，后续失败批次不再逐帧重试' % _consec_fail)
                objs = [None] * len(frames)
        if objs:
            for j, (idx, ts, fp) in enumerate(batch):
                obj = objs[j] if j < len(objs) else None
                if not obj:
                    # 失败或空结果：不写入 _analyzed，下次续跑会重试
                    _failed.add(_key_of_idx[idx])
                    continue
                _analyzed[_key_of_idx[idx]] = {
                    'location': str(obj.get('location', '')).strip()[:50],
                    'characters': str(obj.get('characters', '')).strip()[:80],
                    'event': str(obj.get('event', '')).strip()[:100],
                    'dialogue': str(obj.get('dialogue', '')).strip()[:100],
                    'summary': str(obj.get('summary', '')).strip()[:60],
                }
        # 每批分析完立即原子落盘，中途取消 / 崩溃都不丢已分析结果
        if progress_file:
            _MN._atomic_json_dump(progress_file, {
                'version': 1,
                'fingerprint': cache_key,
                'updated_at': time.time(),
                'completed': sorted(_analyzed.keys()),
                'results': _analyzed,
            })
        if progress:
            progress['phase'] = '场景理解 %d/%d%s' % (len(_analyzed), total_key, _resume_tag)
            progress['pct'] = 44 + int(6 * len(_analyzed) / max(1, total_key))

    # 按 need_vlm 原顺序回填结果：dedup帧复用leader字段，start/end保留各自帧的时间窗口
    _EMPTY_F = {'location': '', 'characters': '', 'event': '', 'dialogue': '', 'summary': ''}
    for idx, ts, fp in need_vlm:
        leader = _reuse.get(idx, idx)
        lk = _key_of_idx.get(leader, '')
        base = _analyzed.get(lk)
        if base is None:
            base = dict(_EMPTY_F)
            if lk in _failed:
                base['summary'] = 'VLM超时跳过'
        _ws, _we = _win(ts, idx)
        results.append({
            'start': _ws, 'end': _we,
            'location': base['location'], 'characters': base['characters'],
            'event': base['event'], 'dialogue': base['dialogue'], 'summary': base['summary'],
        })

    results.sort(key=lambda x: x['start'])
    _log.info(f'[DIAG] VLM均匀抽样(批量+差分复用): {len(sample_times)}个点, VLM {vlm_count}次(原需{len(need_vlm)}次, 复用省{_vlm_saved}帧), 跳过{skip_count}个无台词')
    # [S1 缺陷修复] 仅全部完成且无失败批次时写缓存：
    # 取消/失败写部分结果会污染缓存，后续任务命中后缺口永久化、断点续跑被短路。
    if _all_done and not _failed:
        _MN._cache_save(cache_key, results)
    # 全部完成并已落缓存 → 清理断点进度文件（下次走缓存，不再需要续跑）
    # 中途取消 / 有失败批次时不删：取消留给下次续跑恢复，失败留给下次重试。
    if _all_done and not _failed and progress_file and os.path.exists(progress_file):
        try:
            os.remove(progress_file)
            _log.info('[DIAG] VLM断点续跑: 已全部完成，清理进度文件')
        except OSError:
            pass
    # A1 片头识别：检测正片起点写回 progress，供下游「解说起点对齐」使用。
    # 检测失败/无片头时 content_start=0，下游不跳过（行为与旧版完全一致）。
    try:
        _content_start = _detect_opening(results, asr, vdur)
        if progress is not None:
            progress['content_start'] = _content_start
        if _content_start > 0:
            _log.info('[DIAG] 检测到片头，正片起点 content_start=%.1fs' % _content_start)
    except Exception:
        pass   # 片头检测异常不影响出片
    return results


def _vlm_sample_captions(video_path, vdur, run_dir, n_samples=24, progress=None):
    """均匀采样全片 N 帧，VLM 描述画面内容，返回 [(time_sec, caption), ...]。

    用于「台词匹配 + 画面匹配」融合评分：选片段时不只看台词是否提到关键词，
    还要看画面内容是否和解说词相关。VLM 不可用时返回 []，退化为纯台词匹配。
    """
    import movie_narrator as _MN
    if not _w.vlm_enabled():
        return []
    try:
        if not _w.vlm_ping()[0]:
            return []
    except Exception:
        return []

    frame_dir = os.path.join(run_dir, 'vlm_samples')
    os.makedirs(frame_dir, exist_ok=True)

    # 均匀采样时间点：首尾各留 2% 余量，避免黑帧
    times = [vdur * (0.02 + 0.96 * i / max(1, n_samples - 1)) for i in range(n_samples)]
    captions = []
    for idx, t in enumerate(times):
        if progress:
            progress['phase'] = '画面采样 %d/%d' % (idx + 1, n_samples)
            progress['pct'] = 44 + int(6 * idx / n_samples)
        fp = os.path.join(frame_dir, 'sample_%03d.jpg' % idx)
        rc, _o, _e = _MN.ffmpeg_run(['-y', '-ss', '%.3f' % t, '-i', video_path,
                                     '-frames:v', '1', '-vf', 'scale=min(iw\\,640):-2',
                                     '-q:v', '4', '-an', fp])
        if rc != 0 or not os.path.exists(fp):
            continue
        try:
            cap = _w.vlm_chat(fp, '用一句话描述这个画面里的人物、场景、动作和关键物品。只描述画面，不要推测剧情。',
                           system='你是影视画面分析助手。', timeout=30)
            cap = (cap or '').strip().replace('\n', ' ')[:200]
            if cap:
                captions.append((round(t, 1), cap))
        except Exception:
            continue
    return captions
