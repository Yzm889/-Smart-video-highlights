# -*- coding: utf-8 -*-
"""视频渲染核心（由 webui_server.py 拆出）。

包含：解说渲染管线（剪辑→配音→混音→烧字幕→配乐）的全部函数与常量。
约定与 workflows.py / handler.py 一致：宿主符号经 _w.<name> 晚绑定，
本模块公共符号在文件末尾注入回宿主命名空间。
"""
import logging, os, sys

# ---- 直接导入：已拆出的工具模块 ----
from ffmpeg_utils import ffmpeg_run, probe_audio_len, _has_audio_track, AbortError, _TLS, \
    probe_is_hdr, tonemap_chain, tonemap_available, BT709_TAGS
from text_utils import _clamp_line, _clean_caption, _strip_tts_markup
from tts_engines import _ping_preferred_engine, local_tts_speak, strip_tts_markup, sapi_tts
from ai_providers import _aborted, load_ai_config

_log = logging.getLogger('framecut.video_render')

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
# 常量
# ---------------------------------------------------------------------------
_NAR_CPS = 4.6          # 中文口播经验语速：字/秒（SAPI 与云端 TTS 实测的折中值）
_NAR_MIN_CHARS = 12     # 极短镜头也至少说满一句话，避免只剩半句
_NAR_MAX_CHARS = 95     # 单段解说上限，避免长镜头堆字导致语速被迫过快
_NAR_MAX_SPEED = 1.35   # atempo 最大加速倍率，超过会有明显失真
_NAR_MIN_SPEED = 1.03   # 低于此倍率听不出差别，不必重编码

# P1-1 单遍合并开关：默认开启（视频只编码 1 次）；单遍失败时自动回退两遍管线。
# 设环境变量 FRAMECUT_SINGLE_PASS=0 可强制走旧两遍管线。
_SINGLE_PASS_CUT = os.environ.get('FRAMECUT_SINGLE_PASS', '1') != '0'


def _target_chars(dur):
    """把画面时长换算成解说词目标字数区间 (lo, hi)。

    配音时长 ≈ 字数 / _NAR_CPS；让字数贴合时长，解说才不会溢出到下一个镜头
    （溢出会被 atrim 腰斩）也不会念完还剩大片空窗。"""
    try:
        dur = float(dur)
    except Exception:
        dur = 5.0
    if dur <= 0:
        dur = 5.0
    base = max(_NAR_MIN_CHARS * 1.0, min(float(_NAR_MAX_CHARS), dur * _NAR_CPS))
    return (int(round(base * 0.80)), int(round(base * 1.05)))


def _fit_voice(voice_len, span_len):
    """给出让配音贴合画面时长的策略。

    返回 {'speed': 建议 atempo 倍率, 'trim': 加速后是否仍需截断, 'over': 溢出秒数(负=空窗)}。
    - 配音长于画面：适度加速（上限 _NAR_MAX_SPEED），仍超则标记 trim 交给下游裁剪。
    - 配音短于画面：不加速（宁可留白也不拖慢口播），over 为负数表示空窗时长。"""
    try:
        voice_len = float(voice_len); span_len = float(span_len)
    except Exception:
        return {'speed': 1.0, 'trim': False, 'over': 0.0}
    if span_len <= 0.3:
        return {'speed': 1.0, 'trim': False, 'over': voice_len}
    over = voice_len - span_len
    if over <= 0:
        return {'speed': 1.0, 'trim': False, 'over': over}
    need = voice_len / span_len
    if need <= _NAR_MAX_SPEED:
        return {'speed': round(need, 3), 'trim': False, 'over': over}
    return {'speed': _NAR_MAX_SPEED, 'trim': True, 'over': over}


def _display_factor(seg_len, v_len):
    """画面微变速因子 f（供 setpts=f*PTS 使用）。P1-2 核心。

    让每段画面时长 D 贴合该段真实配音时长，从而「画面随声音走」，彻底消除
    「话说完画面还杵着」与「话没说完被腰斩」两类尴尬。

    D = clamp(v_len + 0.35, 0.85*seg_len, 1.15*seg_len)
      · v_len+0.35：画面比配音多 0.35s 收尾定格（念完即收，不突兀）。
      · ±15% 夹紧：超出此范围的变速观众可察觉（快进/慢动作感），故画面只在此带内微调，
        更极端的偏差交给配音 atempo（见 _render_narrate），二者配合覆盖全范围且不破音。
    返回 f = D / seg_len；seg_len 非法或无声段返回 1.0（原样保留）。"""
    try:
        seg_len = float(seg_len); v_len = float(v_len)
    except Exception:
        return 1.0
    if seg_len <= 0.02 or v_len is None or v_len <= 0:
        return 1.0
    D = v_len + 0.35
    D = max(0.85 * seg_len, min(1.15 * seg_len, D))
    if D <= 0:
        return 1.0
    return D / seg_len


def attach_importance(params, progress, segs):
    """把分析阶段落盘的剧情重要度接进 params['_importance']，供 BGM 情绪包络使用。

    只在长度严格一致时采用：outline 是「镜头段」粒度、narr 是「解说节」粒度，
    高密度剪辑聚合后两者长度不同，硬套索引会让配乐情绪整体错位——宁可不带。
    返回新的 params（不原地改，避免污染调用方共享的字典）。
    """
    if (params or {}).get('_importance'):
        return params
    ol = (progress or {}).get('outline') or []
    if not ol or len(ol) != len(segs or []):
        return params
    p = dict(params or {})
    p['_importance'] = [str(o.get('importance') or 'advance') for o in ol]
    return p


def _render_narrate(video_path, segs, narr, params, run_dir, progress=None, music_path=None, mode=None,
                    auto_cut=True, narr_map=None):
    """解说渲染阶段：按分镜剪辑(可选)→逐段配音→混音→烧字幕→配乐。

    auto_cut=True 时先按保留段真剪辑（剪掉未勾选/无解说的画面），字幕与配音自动对齐到
    剪辑后的新时间轴。返回 (final, voice_clips, cut_info)。

    P1-2 画面微变速：配音必须先于剪辑生成——只有拿到每段真实配音时长，才能在剪辑阶段用
    setpts 把画面时长「掰」成贴合配音的值（±15% 内观众无感），从而彻底消除
    「话说完画面还杵着」与「话没说完被腰斩」两类尴尬。"""
    def up(ph, pct):
        if progress:
            progress['phase'] = ph; progress['pct'] = pct

    src_video = video_path
    cut_info = {'cut_sec': 0.0, 'src_dur': round(probe_audio_len(video_path) or 0.0, 2),
                'out_dur': None, 'segs': len(segs)}

    # [P1-2] 高密度剪辑聚合必须放在剪辑之前：聚合后的段才是「一节解说词」对应的真实画面区间，
    #         剪辑与画面微变速都基于它计算；放剪辑后会因 new_spans 替换 segs 而错位。
    if narr_map and len(narr_map) == len(segs):
        groups = {}
        for k, bi in enumerate(narr_map):
            if bi not in groups:
                groups[bi] = (segs[k][0], segs[k][1])
            else:
                groups[bi] = (groups[bi][0], segs[k][1])
        beat_ranges = [groups.get(bi, (0.0, 0.0)) for bi in range(len(narr))]
        segs = beat_ranges
        _log.info(f'[DIAG] 高密度剪辑: {len(narr)}节 -> {len(narr_map)}个片段')

    # 长度对齐保护：narr 与 segs 必须一一对应。
    if len(narr) > len(segs):
        narr = narr[:len(segs)]
    elif len(narr) < len(segs):
        narr = list(narr) + [''] * (len(segs) - len(narr))

    # ---- 第一阶段：先逐段配音，拿到每段真实时长（P1-2 画面适配声音的前提）----
    up('逐段配音', 22)
    tts_clips = {}     # seg_idx -> 配音文件路径
    voice_durs = {}    # seg_idx -> 真实配音时长（秒）
    # 只在云端 TTS 真正配了 api_key+model 时才走云端；否则直接用本地免费引擎
    _tcfg = load_ai_config().get('tts') or {}
    use_mimo = bool(_tcfg.get('api_key')) and bool(_tcfg.get('model'))
    if not use_mimo and not getattr(_TLS, 'tts_engine', None):
        _ping_preferred_engine(run_dir)
    for i, txt in enumerate(narr):
        if _aborted():
            raise AbortError('用户取消了任务')
        if not (txt and txt.strip()):
            continue
        if txt.strip() in ('（留白）', '(留白)'):
            continue   # 留白段：不配音不出字幕，让原片声音飞（第五原则·留白意识）
        seg_span = segs[i] if i < len(segs) else (0.0, 10.0)
        span_len = max(0.0, seg_span[1] - seg_span[0])
        # 配音前先按画面时长做字数硬上限兜底：给足 _NAR_MAX_SPEED 的加速余量，
        # 超出的部分宁可精简，也不要让配音被 atrim 在段末腰斩（听众只听到半句）
        hard_cap = int(round(_target_chars(span_len)[1] * _NAR_MAX_SPEED))
        spoken = _clamp_line(txt, hard_cap) or txt
        clip = None
        if use_mimo:
            np_ = os.path.join(run_dir, f'narr{i}.mp3')
            if _w.ai_tts(spoken, np_):
                clip = np_
        if clip is None:
            # 本地免费配音：edge-tts（免 Key）→ 离线模型（sherpa-onnx）→ 系统 SAPI 兜底
            ok, _eng, lp = local_tts_speak(spoken, os.path.join(run_dir, f'narr{i}.mp3'))
            if ok:
                clip = lp
            else:
                # [2.2] 显式降级：local_tts_speak 全部引擎失败 → 用清洗后的文本
                #       （复用 _strip_tts_markup，剥掉 {停顿:1.0} 等标记避免被念出来）
                #       再走一次系统 SAPI，保证该段至少有声音而不是静默丢段。
                _log.info(f'[DIAG] TTS失败 seg={i} 字数={len(spoken)} 文本前20字={spoken[:20]}，改走 SAPI 兜底')
                try:
                    _sapi_out = os.path.join(run_dir, f'narr{i}_sapi.wav')
                    _clean_spoken = _strip_tts_markup(spoken)
                    if _clean_spoken and sapi_tts(_clean_spoken, _sapi_out):
                        clip = _sapi_out
                        _eng = 'sapi'   # 兜底成功：该段由 SAPI 替换
                except Exception as _e:
                    _log.info(f'[DIAG] SAPI兜底异常 seg={i}: {_e}')
                if clip is None:
                    # [2.2] 仍失败：写结构化失败原因到 progress（前端可见），杜绝静默丢段。
                    _log.info(f'[DIAG] TTS彻底失败 seg={i}（该段将静音，失败原因已记录）')
                    if progress:
                        _fl = progress.get('tts_failures') or []
                        _fl.append({'idx': i, 'engine': _eng or 'sapi',
                                    'error': 'all_engines_failed'})
                        progress['tts_failures'] = _fl
        if clip is None:
            continue
        v_len = probe_audio_len(clip) or max(0.5, span_len)
        # [P1-2] 画面微变速策略：
        #   · auto_cut 时优先「画面去适配声音」——只有配音明显偏长(>1.15×段长)才用 atempo 压缩配音；
        #     其余交给剪辑阶段的 setpts 把画面掰到贴合配音，人声零失真。
        #   · 非 auto_cut（不剪辑）时维持旧行为：配音超长即 atempo 提速。
        fit = _fit_voice(v_len, span_len)
        _need_atempo = fit['speed'] > _NAR_MIN_SPEED and (not auto_cut or (v_len / span_len) > 1.15)
        if _need_atempo:
            # 中间态改用 PCM WAV：TTS 源多为 mp3，再用 libmp3lame 重编码等于二次有损，人声会变闷。
            fast = os.path.join(run_dir, f'narr{i}_fit.wav')
            rc, _o, _e = ffmpeg_run(['-y', '-i', clip, '-vn',
                                     '-filter:a', 'atempo=%.3f' % fit['speed'],
                                     '-c:a', 'pcm_s16le', fast])
            if rc == 0 and os.path.exists(fast):
                clip = fast
                v_len = probe_audio_len(clip) or (v_len / fit['speed'])
        tts_clips[i] = clip
        voice_durs[i] = v_len

    # ---- 字幕文案：剥 TTS 标记、留白置空（P1-1 单遍合并需在剪辑前烧字幕，两遍管线共用）----
    narr_srt = ['' if (t or '').strip() in ('（留白）', '(留白)') else strip_tts_markup(t) for t in narr]

    # ---- 第二阶段：按分镜真剪辑，并应用 P1-2 画面微变速（setpts）----
    _single_pass = False
    if auto_cut:
        up('按分镜剪辑画面', 30)
        # 【剪辑失效根因修复】分析阶段可能已经把 keep=False / 低信息密度段剔除了
        # （cut_plan.removed>0）。此时若仍按「首尾覆盖+无空隙」判定 no_cut 会跳过剪辑，
        # 成片=原片全部画面 —— 用户看到「没剪」。有被剪段 → 强制真剪（拼接保留段）。
        _cp_removed = int(((progress or {}).get('cut_plan') or {}).get('removed') or 0)
        _skip_no_cut = bool(_cp_removed)
        if _SINGLE_PASS_CUT:
            try:
                src_video, segs, cut_sec = _cut_and_burn_video(
                    video_path, segs, voice_durs, run_dir, params, narr_srt, tts_clips, progress,
                    skip_no_cut=_skip_no_cut)
                _single_pass = True
                _log.info('[DIAG] P1-1 单遍合并成功：视频仅编码 1 次')
            except Exception as _ce:
                _log.warning('[DIAG] P1-1 单遍合并失败，回退两遍管线: %s' % _ce)
                _warn(progress, 'P1-1 单遍合并失败，已回退两遍管线：%s' % _ce)
                src_video, segs, cut_sec = _w._cut_video_by_spans(
                    video_path, segs, run_dir, progress, voice_durs=voice_durs, skip_no_cut=_skip_no_cut)
        else:
            src_video, segs, cut_sec = _w._cut_video_by_spans(
                video_path, segs, run_dir, progress, voice_durs=voice_durs, skip_no_cut=_skip_no_cut)
        cut_info['cut_sec'] = cut_sec
        cut_info['segs'] = len(segs)
    cut_info['out_dur'] = round(probe_audio_len(src_video) or cut_info['src_dur'], 2)
    _log.info(f'[DIAG] 剪辑后: segs={len(segs)} 总时长={sum(b-a for a,b in segs):.1f}s 视频时长={cut_info["out_dur"]}s narr={len(narr)}')

    # ---- 第三阶段：按（剪辑后的）新时间轴定位配音 + 字幕窗口 ----
    up('混音+烧字幕+配乐', 60)
    # [P0-2] 把「每段剧情重要度」带进混音阶段，供 BGM 情绪包络使用。
    params = attach_importance(params, progress, segs)
    tts_paths = []
    voice_spans = _build_voice_spans(segs, voice_durs, tts_clips)
    for i in sorted(tts_clips.keys()):
        if i >= len(segs):
            continue
        clip = tts_clips[i]
        sp = segs[i]
        od = sp[0]
        tts_paths.append((clip, od, sp[1]))
    _log.info(f'[DIAG] 配音完成: tts_paths={len(tts_paths)}/{len([t for t in narr if t and t.strip()])} voice_spans={len(voice_spans)}')
    final = _w._compose_narration_video(src_video, segs, narr_srt, tts_paths, run_dir, params,
                                     music_path=music_path, voice_spans=voice_spans, burned=_single_pass)
    # ---- 附赠产物：独立的纯解说音轨 ----
    # 分层音轨与单独导出共用这一条 WAV（一次生成，两处用）。
    # 失败一律不影响成片：这是附赠物，不是主链路。
    _voice_wav = None
    if tts_paths and (_voice_track_enabled(params) or _audio_layers_enabled(params)
                      or _narr_out_enabled(params)):
        _vdur = probe_audio_len(final) or cut_info.get('out_dur') or 0.0
        try:
            _voice_wav = build_voice_track(tts_paths, run_dir, _vdur)
        except Exception as _ve:
            _log.info('[DIAG] 解说音轨导出异常（不影响成片）: %s', _ve)

    # 成片分层音轨：A1 = 完整混音（默认播放轨，保证任何播放器打开都能听到解说），
    #                A2 = 纯解说（剪辑软件里可静音 A1 后单独用，或做校对/替换）。
    # 用二次 mux（-c:v copy -c:a:0 copy）而不是改主混音链：主链里有 asplit
    # 与 sidechaincompress，动错一个标签就会整链降级到「丢弃配音」（真机踩过）。
    # 二次 mux 失败 → 保留原成片，只是没有 A2，用户仍有单独导出的 WAV。
    if _audio_layers_enabled(params) and _voice_wav and os.path.isfile(_voice_wav):
        try:
            _layered = os.path.join(run_dir, 'final_layered.mp4')
            _rc, _o, _e = ffmpeg_run([
                '-y', '-i', final, '-i', _voice_wav,
                '-map', '0:v:0', '-map', '0:a:0', '-map', '1:a:0',
                '-c:v', 'copy', '-c:a:0', 'copy', '-c:a:1', 'aac', '-b:a:1', '192k',
                '-disposition:a:0', 'default', '-disposition:a:1', '0',
                '-movflags', '+faststart', _layered])
            if _rc == 0 and os.path.isfile(_layered) and os.path.getsize(_layered) > 0:
                final = _layered
            else:
                _log.info('[DIAG] 分层音轨 mux 失败，保留单轨成片: %s',
                          _e.decode('utf-8', 'ignore')[-200:])
        except Exception as _me:
            _log.info('[DIAG] 分层音轨 mux 异常，保留单轨成片: %s', _me)
    if progress:
        progress['done'] = True
        progress['pct'] = 100
        progress['file'] = os.path.relpath(final, _w.OUTDIR).replace('\\', '/')
        # 字幕文件随任务一起交付（前端给下载链接）。
        # 不管烧不烧都暴露：烧了的话用户也能拿到 srt 去别的软件复用/校对；
        # 不烧的时候它就是唯一载体，必须给。
        try:
            _srt_path = os.path.join(run_dir, 'narr.srt')
            if os.path.isfile(_srt_path) and os.path.getsize(_srt_path) > 0:
                progress['subtitle_file'] = os.path.relpath(_srt_path, _w.OUTDIR).replace('\\', '/')
                progress['subtitle_burned'] = bool(_burn_enabled(params))
        except Exception:
            pass
        # 独立的纯解说音轨：勾了「单独导出」或「成片分层音轨」都给下载链接。
        # 它是 pcm_s16le WAV（无损、无固定编码延迟），剪辑软件里拖进去
        # 就是一条独立的 A2，0 秒对齐成片 0 秒。
        try:
            if _voice_wav and os.path.isfile(_voice_wav) and os.path.getsize(_voice_wav) > 0:
                progress['voice_file'] = os.path.relpath(_voice_wav, _w.OUTDIR).replace('\\', '/')
                progress['voice_track_layered'] = bool(_audio_layers_enabled(params))
        except Exception:
            pass
        if mode:
            progress['mode'] = mode
    return final, len(tts_paths), cut_info


def _merge_spans(spans, eps=0.05):
    """合并重叠/紧邻的区间并按时序排序，避免剪辑时同一段画面被重复拼接。"""
    out = []
    for s0, s1 in sorted((float(a), float(b)) for a, b in (spans or [])
                         if float(b) - float(a) > 0.02):
        if out and s0 <= out[-1][1] + eps:
            out[-1] = (out[-1][0], max(out[-1][1], s1))
        else:
            out.append((s0, s1))
    return out


def _warn(progress, msg):
    """把降级/失败信息写入 progress.warnings（前端展示），杜绝「静默降级」。
    任何「本应剪辑/配音/合成，但走了兜底」的路径都必须调用。"""
    if progress is not None:
        try:
            wl = progress.get('warnings') or []
            if msg not in wl:
                wl.append(str(msg))
                progress['warnings'] = wl
        except Exception:
            pass


def _cut_video_by_spans(video_path, spans, run_dir, progress=None, voice_durs=None, skip_no_cut=False):
    """按保留区间真剪辑：只留 spans 覆盖的画面，顺序拼成新片，并给出新时间轴。

    这是「剧情驱动剪辑」名副其实的关键。历史实现里解说链路只做「烧字幕 + 混音」，
    成片时长恒等于原片，用户在预览里取消勾选的段落画面照样留在成片里 —— 等于没剪。

    返回 (cut_path, new_spans, cut_seconds)：
    - new_spans[i] 是 spans[i] 在拼接后新片里的 (start, end)，字幕与配音必须按它对齐
    - cut_seconds 为被剪掉的总时长（0 表示未发生剪辑）
    剪辑失败时安全降级为 (video_path, spans, 0)：宁可不剪，也不因剪辑把出片搞崩。
    """
    def up(ph, pct):
        if progress is not None:
            progress['phase'] = ph
            progress['pct'] = pct

    vdur = probe_audio_len(video_path) or 0.0
    # 夹到 [0, vdur]，避免分析阶段给出的切点越界导致 ffmpeg 报错
    raw = [(max(0.0, min(vdur, float(a))), max(0.0, min(vdur, float(b))))
           for a, b in (spans or [])]
    # 过短段给最小 0.5s 而不是丢弃：本函数的契约是「段数与输入严格一致」，
    # 调用方 render_pipeline 用 enumerate(new_spans) 同时索引解说词与段 ID，
    # 少一段就会让后面所有段整体错位（成片解说词配错画面）。
    # 只有「输入本身为空」才允许返回空。
    _MIN_SEG = 0.5
    raw = [(a, min(vdur, a + _MIN_SEG) if b - a <= 0.02 else b) for a, b in raw]
    if not raw or vdur <= 0:
        return video_path, raw, 0.0
    # 修正重叠段：分析阶段/用户微调可能产生重叠（后段 start < 前段 end），
    # 若不修正，_merge_spans 会合并重叠区，但 new_spans 仍按原始段长累计 →
    # new_spans 总时长 > 剪辑后实际视频时长 → 后半段字幕/配音落在视频结束点之后，用户看不到听不到。
    # 修正方式：后段 start 移到前段 end（重叠画面只出现一次，归属前段）。
    _fixed = []
    for a, b in raw:
        if _fixed and a < _fixed[-1][1]:
            a = _fixed[-1][1]
        # 修正后过短的片段给最小 0.5 秒时长，绝不过滤——过滤会导致 narr 与 segs 长度不匹配，
        # 后面的解说词无对应画面段，配音丢失、字幕错位。
        if b - a <= 0.02:
            b = min(vdur, a + 0.5)
        _fixed.append((a, b))   # 不过滤：段数必须与输入一致
    raw = _fixed
    if not raw:
        return video_path, raw, 0.0

    keep = sum(b - a for a, b in raw)
    gap = vdur - keep
    # 连续覆盖全片（中间没有实质空隙）→ 没有可剪的内容，直接跳过，省一次全片重编码
    covered_gap = sum(max(0.0, raw[i + 1][0] - raw[i][1]) for i in range(len(raw) - 1))
    # 【修复】当铺满整片时 covered_gap≈0 会触发 no_cut，用户感到「没剪辑」。
    # 收严到「所有片段间完全无空隙且首尾完全覆盖」才算无剪辑——有任何可见空隙一律真剪。
    # skip_no_cut=True（分析阶段已剔除被剪段）时，即使保留段首尾覆盖也执行真剪，
    # 否则「剪辑失效」：被剪段信息被拿走后又按覆盖判定跳过剪辑，成片保留全部画面。
    _no_cut = (not skip_no_cut) and raw and raw[0][0] <= 0.05 and vdur - raw[-1][1] <= 0.05 and covered_gap <= 0.0

    if _no_cut:
        if skip_no_cut:
            _warn(progress, '剪辑跳过（保留段已覆盖全片，无空隙可剪）')
        return video_path, raw, 0.0

    cut_dir = os.path.join(run_dir, 'cuts')
    os.makedirs(cut_dir, exist_ok=True)
    has_audio = _has_audio_track(video_path)
    # HDR → SDR（v1.4.10）：整条管线的第一道编码就在这里，所以调色必须放在这儿——
    # 每段映射一次，产出即 SDR，后面的拼接 / 烧字幕 / 混音全部继承 bt709 标记。
    # 不调色的后果：输出是「3840x2160 yuv420p + bt2020nc/bt2020/smpte2084」，
    # 贴了 HDR 标签却只有 8bit，识别 HDR 的播放器上偏暗发灰、还容易出色带。
    _tm_chain = ''
    try:
        if probe_is_hdr(video_path) and tonemap_available(video_path):
            _tm_chain = tonemap_chain()
    except Exception:
        _tm_chain = ''
    pieces = []
    piece_durs = []   # 每段剪辑后的实际时长（含 P1-2 画面微变速），用于新时间轴累计
    try:
        for i, (s0, s1) in enumerate(raw):
            up('✂ 剪辑片段 %d/%d' % (i + 1, len(raw)), 34 + int(20 * i / max(1, len(raw))))
            p = os.path.join(cut_dir, 'cut%03d.mp4' % i)
            seg_len = s1 - s0
            # [P1-2] 画面微变速：setpts 把画面时长掰到贴合配音（D=clamp(v_len+0.35, 0.85~1.15×段长)），
            #         同一段的场景原声同步 atempo=1/f，保证画面与原声不脱节。无声段(factor≈1)原样保留。
            f = _display_factor(seg_len, (voice_durs or {}).get(i)) if voice_durs else 1.0
            cmd = ['-y', '-ss', '%.3f' % s0, '-i', video_path, '-t', '%.3f' % seg_len]
            _vf = []
            if abs(f - 1.0) > 0.005:
                _vf.append('setpts=%.6f*PTS' % f)
            if _tm_chain:
                _vf.append(_tm_chain)
            if _vf:
                cmd += ['-vf', ','.join(_vf)]
                if has_audio and abs(f - 1.0) > 0.005:
                    cmd += ['-af', 'atempo=%.6f' % (1.0 / f)]
            cmd += _w.video_encode_args_interim()
            if _tm_chain:
                cmd += list(BT709_TAGS)
            cmd += ['-threads', '0']
            if has_audio:
                cmd += ['-c:a', 'aac', '-b:a', '160k', '-ar', '44100', '-ac', '2']
            else:
                cmd += ['-an']
            cmd += [p]
            rc, _o, e = ffmpeg_run(cmd)
            if rc != 0 or not os.path.exists(p):
                raise RuntimeError('片段 %d 剪切失败: %s' % (i, e.decode('utf-8', 'ignore')[-200:]))
            pieces.append(p)
            piece_durs.append(seg_len * f if abs(f - 1.0) > 0.005 else seg_len)

        out = os.path.join(run_dir, 'cut.mp4')
        concat_txt = os.path.join(cut_dir, 'concat.txt')
        with open(concat_txt, 'w', encoding='utf-8') as f:
            for p in pieces:
                # 【必须绝对路径】concat 分离器把清单里的相对路径按「清单所在目录」解析，
                # 若 run_dir 本身是相对路径，就会拼成 cuts/webui_output/.../cuts/cut000.mp4
                # （路径重复两遍）→ 拼接必然失败 → 静默回退原片 → 成片根本没剪。
                # 实测：22 分钟片 105 段全部按上述方式失败，成片时长仍等于原片。
                _ap = os.path.abspath(p)
                f.write("file '%s'\n" % _ap.replace('\\', '/').replace("'", "'\\''"))
        up('拼接保留片段', 56)
        rc, _o, e = ffmpeg_run(['-y', '-f', 'concat', '-safe', '0', '-i', concat_txt,
                                '-c', 'copy', '-movflags', '+faststart', out])
        if rc != 0 or not os.path.exists(out):
            # 兜底：copy 拼接失败（多为各段编码参数/时间戳不一致）时，
            # 改用 concat demuxer + 重编码——仍走 concat.txt 清单（单 -i），
            # 绝不用 N 路 -i 的单条 filter_complex 命令：段数多时 ffmpeg 会因命令行 /
            # 滤镜复杂度炸掉，导致整段剪辑回退原片、成片完全没剪。重编码慢但稳，
            # 且只在兜底路径触发。
            fb = (['-y', '-f', 'concat', '-safe', '0', '-i', concat_txt]
                  + _w.video_encode_args_interim())
            if has_audio:
                fb += ['-c:a', 'aac', '-b:a', '160k', '-ar', '44100', '-ac', '2']
            fb += ['-movflags', '+faststart', out]
            rc, _o, e = ffmpeg_run(fb)
        if rc != 0 or not os.path.exists(out):
            raise RuntimeError('拼接失败: ' + e.decode('utf-8', 'ignore')[-300:])
    except Exception as _ce:
        # 剪辑属增强项：失败就退回原片，保证「能出片」优先于「剪得漂亮」。
        # 【不静默】明确写入 progress.warnings，前端展示，用户知道「没有剪成功」而不是以为剪了。
        _log.warning('[DIAG] 剪辑失败回退原片: %s' % _ce)
        _warn(progress, '剪辑失败，成片将保留全部画面（未剪辑）：%s' % str(_ce)[:120])
        return video_path, raw, 0.0

    # 拼接后的新时间轴：按每段实际时长（含 P1-2 画面微变速）逐段累计，
    # 段数与输入严格一致，保证与解说词一一对应。
    new_spans, cur = [], 0.0
    for a, b in raw:
        _d = piece_durs[len(new_spans)] if len(new_spans) < len(piece_durs) else (b - a)
        new_spans.append((round(cur, 3), round(cur + _d, 3)))
        cur += _d
    # -c copy 拼接 VFR 视频时，各片段实际时长可能与 -t 指定的有偏差，
    # 导致拼接后视频实际时长 != new_spans 总时长，后面的配音/字幕落在视频结束点之后。
    # 修复：ffprobe 检查实际时长，偏差>0.5s 时按比例缩放 new_spans，保证与视频对齐。
    actual_dur = probe_audio_len(out) or cur
    expected_dur = cur
    if actual_dur > 0 and abs(actual_dur - expected_dur) > 0.5:
        scale = actual_dur / expected_dur
        _log.info(f'[DIAG] 拼接时长偏差: 预期={expected_dur:.1f}s 实际={actual_dur:.1f}s 缩放={scale:.3f}')
        new_spans = [(round(s * scale, 3), round(min(actual_dur, e * scale), 3)) for s, e in new_spans]
    # 被剪掉的总时长 = 原片时长 - 保留时长（gap 就是这个值，别再扣一次段间空隙）
    return out, new_spans, round(max(0.0, gap), 3)


def _ass_enabled(params):
    """字幕淡入淡出开关：默认开；subtitle.ass=False 或 FRAMECUT_ASS=0 回退纯 SRT。"""
    sub = (params or {}).get('subtitle') or {}
    if sub.get('ass') is False:
        return False
    return os.environ.get('FRAMECUT_ASS') != '0'


def _burn_enabled(params):
    """字幕是否**烧进画面**。默认 True（老行为，逐字节一致）。

    False = 只出干净成片 + 附带一个 .srt 字幕文件，让用户拿去剪映/PR/FCP
    自己调位置、字体、字号。这不是"少做一步"，而是把字幕的控制权交回去：
      · 硬烧之后想改字幕位置只能整片重渲染（几分钟起步）；
      · 很多人的实际工作流是「先出片 → 在剪辑软件里统一调字幕样式」；
      · 烧录还会把字幕永久压在画面上，想再改文案就只能回源。
    代价是成片没有字幕，所以前端必须明确提示"这次不带字幕"。

    参数位置：params.subtitle.burn（与 fontSize/color 这些样式项同处一支，
    不新增顶层键 —— 顶层已有 subtitle_file 表示"导入的字幕文件"，别混）。"""
    sub = (params or {}).get('subtitle') or {}
    return sub.get('burn') is not False


def _ass_ts(sec):
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    return '%d:%02d:%05.2f' % (h, m, sec % 60)


def _write_narr_ass(rows, params, path):
    """文本 / 时间轴与 SRT 逐条一致，只给每句加 120ms 淡入淡出。"""
    try:
        rect = _w.effective_frame_rect(params)
    except Exception:
        rect = None
    pw, ph = (rect[0], rect[1]) if rect else (1280, 720)
    head = (
        '[Script Info]\nScriptType: v4.00+\n'
        'PlayResX: %d\nPlayResY: %d\nWrapStyle: 0\n\n'
        '[V4+ Styles]\n'
        'Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, '
        'OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, '
        'ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, '
        'MarginL, MarginR, MarginV, Encoding\n'
        'Style: Default,Microsoft YaHei,22,&H00FFFFFF,&H000000FF,&H00000000,'
        '&H00000000,0,0,0,0,100,100,0,0,1,2,1,2,10,10,50,1\n\n'
        '[Events]\n'
        'Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'
    ) % (pw, ph)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(head)
        for w0, w1, cap in rows:
            text = cap
            # 竖屏字号放大后一行放不下，长句按标点折行，否则字幕左右会被裁掉
            if ph > pw and len(text) > 15:
                parts, rest = [], text
                while len(rest) > 15:
                    cut = -1
                    for ch in '，。！？；、':
                        idx = rest.find(ch, 8, 17)
                        if idx > 0:
                            cut = idx + 1
                            break
                    if cut < 0:
                        cut = 15
                    parts.append(rest[:cut])
                    rest = rest[cut:]
                if rest:
                    parts.append(rest)
                text = '\\N'.join(parts)
            f.write('Dialogue: 0,%s,%s,Default,,0,0,0,,{\\fad(120,120)}%s\n'
                    % (_ass_ts(w0), _ass_ts(w1), text))
    return path


def _build_narr_srt(segs, narr, run_dir, voice_spans=None, params=None):
    """把解说词按新时间轴写成 narr.srt 供字幕烧录（单遍/两遍管线共用）。

    同时生成 narr.ass：每句 120ms 淡入淡出，文本与时间轴和 SRT 逐条一致。
    启用且生成成功时返回 .ass 路径，否则回退返回 .srt —— 出片链路零风险。
    """
    srt = os.path.join(run_dir, 'narr.srt')
    rows = []
    with open(srt, 'w', encoding='utf-8') as f:
        def ts(sec):
            hh = int(sec // 3600); mm = int((sec % 3600) // 60); ss = int(sec % 60); ms = int((sec % 1) * 1000)
            return f'{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}'
        seq = 0
        for i, (s0, s1) in enumerate(segs):
            cap = _clean_caption(narr[i] if i < len(narr) else '')
            if not cap:
                continue
            w0, w1 = s0, s1
            vs = (voice_spans or {}).get(i)
            if vs:
                w0 = max(s0, vs[0])
                w1 = min(s1, max(vs[1], w0 + 0.8))   # 至少显示 0.8s，避免一闪而过
            seq += 1
            f.write(f'{seq}\n{ts(w0)} --> {ts(w1)}\n{cap}\n\n')
            rows.append((w0, w1, cap))
    if rows and _ass_enabled(params):
        try:
            return _write_narr_ass(rows, params or {}, os.path.join(run_dir, 'narr.ass'))
        except Exception:
            pass
    return srt


def _audio_layers_enabled(params):
    """成片是否输出**两条独立 AAC 音轨**：默认 False（老行为，单轨混合）。

    True = A1 原声+BGM 混合（成片默认播放轨），A2 纯解说（剪辑软件里可单独
    静音/调音量/替换/校对）。MP4 支持多音频流，PR/FCP/剪映导入后会看到两条轨。
    """
    a = (params or {}).get('audio') or {}
    return a.get('layers') is True


def _voice_track_enabled(params):
    """是否单独导出一条纯解说音轨文件（.wav）。默认 False。"""
    a = (params or {}).get('audio') or {}
    return a.get('exportVoice') is True


def _narr_out_enabled(params):
    """成片是否**不含解说音轨**（audio.layers == 2，前端「成片不含解说」档）。

    该档位下：成片 = 原声/配乐（无解说、无 ducking），解说只经 build_voice_track
    出成 .wav——供用户在剪映/PR 自己铺解说（2026-11-06 主人需求：素材包式交付）。
    """
    a = (params or {}).get('audio') or {}
    return a.get('layers') == 2


def build_voice_track(tts_paths, run_dir, vdur, out_name='narration_voice.wav'):
    """把各段配音按**成片时间轴**铺成一条纯解说音轨（不含原声/BGM）。

    为什么要单独导出：现有 amix 把原声+配音+BGM 全压进一条 AAC，导入 PR 后
    只有一条音轨，想单独调解说音量、替换某句、或对齐字幕都得整轨重做。
    给一条干净的 WAV，剪辑软件里拖进去就是独立 A2，随便剪。

    时间轴对齐规则必须与 _compose_narration_video 里的配音链**完全一致**：
      · 先 atrim 再 adelay（顺序反了会把整句删掉，见下方原链里的实测注释）；
      · adelay 用 seg 的起点 od；总长用 apad=whole_dur 撑到成片时长，
        这样拖进 PR 时 0 秒就是 0 秒，不用手工找偏移。
    失败一律返回 None 而不是抛异常：这是「附赠产物」，不能把主成片拖垮。
    """
    if not tts_paths or vdur <= 0:
        return None
    out = os.path.join(run_dir, out_name)
    inputs, fparts, labels, idx = [], [], [], 0
    for item in tts_paths:
        path = item[0]
        od = float(item[1] or 0)
        oe = item[2] if len(item) > 2 else None
        # 每段解说音量（5 元组的第 5 位；None=不介入）：.wav 必须与主混音听到
        # 同样的音量设定，否则用户在时间轴调了音量、拖进 PR 又对不上
        nv = item[4] if len(item) > 4 else None
        _nv_f = ('volume=%.3f,' % max(0.0, min(2.0, float(nv)))) if nv is not None else ''
        # 每段淡入/淡出（7 元组第 6/7 位）：与主混音链同样 afade（atrim 后、adelay 前），
        # 保证 .wav 与成片听到同样的包络
        _fade_f = ''
        if len(item) >= 7:
            try:
                _fi_v = max(0.0, min(5.0, float(item[5] or 0.0)))
                _fo_v = max(0.0, min(5.0, float(item[6] or 0.0)))
            except Exception:
                _fi_v = _fo_v = 0.0
            if _fi_v > 0 or _fo_v > 0:
                _adur = (oe - od) if (oe is not None and oe > od) else (probe_audio_len(path) or 0.0)
                if _adur > 0.05:
                    if _fi_v > 0:
                        _fi_c = min(_fi_v, _adur / 2.0)
                        _fade_f += 'afade=t=in:st=0:d=%.2f,' % _fi_c
                    if _fo_v > 0:
                        _fo_c = min(_fo_v, _adur / 2.0)
                        _fade_f += 'afade=t=out:st=%.2f:d=%.2f,' % (max(0.0, _adur - _fo_c), _fo_c)
        if not os.path.isfile(path):
            continue
        dmax = max(0.05, (oe - od) if (oe is not None and oe > od) else 0.05)
        inputs += ['-i', path]
        # 与主混音链同样的处理：aresample → (volume) → atrim → (afade) → adelay → apad
        fparts.append(f'[{idx}:a]aresample=44100,{_nv_f}'
                      f'atrim=start=0:end={dmax:.2f},{_fade_f}'
                      f'adelay={int(od * 1000)}|{int(od * 1000)},'
                      f'apad=whole_dur={vdur:.2f}[vt{idx}]')
        labels.append(f'[vt{idx}]')
        idx += 1
    if not labels:
        return None
    fparts.append(f'{"".join(labels)}amix=inputs={len(labels)}:normalize=0,'
                  'aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,'
                  'alimiter=limit=0.95,'
                  f'atrim=0:{vdur:.2f}[aout]')
    cmd = ['-y'] + inputs + ['-filter_complex', ';'.join(fparts),
                             '-map', '[aout]', '-c:a', 'pcm_s16le',
                             '-ar', '44100', '-ac', '2', out]
    rc, _o, e = ffmpeg_run(cmd)
    if rc != 0 or not os.path.isfile(out):
        _log.info('[DIAG] 解说音轨导出失败（不影响成片）: %s',
                  e.decode('utf-8', 'ignore')[-200:])
        return None
    return out


def _build_voice_spans(segs, voice_durs, tts_clips):
    """按（剪辑后的）新时间轴计算字幕窗口：字随声走、念完即收（+0.35s 收尾定格）。"""
    vs = {}
    for i in sorted(tts_clips.keys()):
        if i >= len(segs):
            continue
        v_len = voice_durs.get(i, 1.0)
        sp = segs[i]
        od = sp[0]
        vs[i] = (od, min(sp[1], od + v_len + 0.35))
    return vs


def _cut_and_burn_video(video_path, segs, voice_durs, run_dir, params, narr, tts_clips, progress=None, skip_no_cut=False):
    """P1-1 单遍合并：剪切 + 画面微变速(setpts) + 烧字幕 + 缩放，全部在【一次】视频编码里完成。

    与两遍管线(_cut_video_by_spans 出 cut.mp4 + _compose_narration_video 内烧字幕步)输出等价，
    但视频只编码 1 次（直接以最终画质编码，省掉中间的 interim 重编码），画质更好、出片更快。
    新时间轴 new_spans 在编码前按 P1-2 公式解析算出（与两遍管线 piece_durs 完全一致），字幕 SRT
    据此定位；编码后再用 ffprobe 校验实际时长做 ≤0.5s 的缩放补偿（同两遍管线）。
    任何失败都抛异常，由 _render_narrate 自动回退两遍管线。

    段落边界转场(P2-2)：params['transition']=='crossfade' 时，各段之间用 200ms xfade/acrossfade 交叉淡化
    （硬切保留于过短片）。交叉淡化会让时间轴整体压缩 (n-1)*0.2s，new_spans 同步压缩，配音/字幕自动跟随
    （_render_narrate 会把 segs 接管为压缩后的 new_spans）。默认 'hard' 走原 concat 硬切，零影响。"""
    def up(ph, pct):
        if progress is not None:
            progress['phase'] = ph; progress['pct'] = pct

    vdur = probe_audio_len(video_path) or 0.0
    raw = [(max(0.0, min(vdur, float(a))), max(0.0, min(vdur, float(b)))) for a, b in (segs or [])]
    raw = [(a, b) for a, b in raw if b - a > 0.02]
    if not raw or vdur <= 0:
        return video_path, raw, 0.0
    # 修正重叠段：与两遍管线一致，避免 new_spans 总时长 > 实际视频时长导致字幕/配音错位
    _fixed = []
    for a, b in raw:
        if _fixed and a < _fixed[-1][1]:
            a = _fixed[-1][1]
        if b - a <= 0.02:
            b = min(vdur, a + 0.5)
        if b - a > 0.02:
            _fixed.append((a, b))
    raw = _fixed
    if not raw:
        return video_path, raw, 0.0

    keep = sum(b - a for a, b in raw)
    gap = vdur - keep
    covered_gap = sum(max(0.0, raw[i + 1][0] - raw[i][1]) for i in range(len(raw) - 1))
    # 【修复】当铺满整片时 covered_gap≈0 会触发 no_cut，用户感到「没剪辑」。
    # 收严到「所有片段间完全无空隙且首尾完全覆盖」才算无剪辑——有任何可见空隙一律真剪。
    _no_cut = (not skip_no_cut) and (raw[0][0] <= 0.05 and vdur - raw[-1][1] <= 0.05 and covered_gap <= 0.0)

    has_audio = _has_audio_track(video_path)
    out = os.path.join(run_dir, 'vburn.mp4')
    sub_style = _build_subtitle_style(params)
    # 是否把字幕烧进画面（默认 True）。False = 出干净成片 + 单独 .srt 交付。
    _burn = _burn_enabled(params)
    scale_filter = _w.build_video_filter(params)
    # P0-1 统一调色：放在「拼接之后、烧字幕之前」——只算一次，全片色调一致；
    # 且在字幕之前，白色字幕不会被 colorbalance 染上蓝/红色偏。
    # 默认 '原片' 返回 None → 滤镜串与改动前逐字符一致（零影响）。
    grade = _w.grade_filter(params)
    bitrate = params.get('bitrate') or None
    tier = params.get('qualityTier') or None
    transition = params.get('transition') or 'hard'   # 'hard' 硬切(默认) / 'crossfade' 段落边界 200ms 交叉淡化
    _FADE = 0.2                                      # 交叉淡化时长(s)

    if _no_cut:
        # 全片覆盖：直接在原片上烧字幕+缩放，时长、画面与两遍管线返回原片后烧字幕完全一致
        new_spans = raw
        srt = _build_narr_srt(new_spans, narr, run_dir, _build_voice_spans(new_spans, voice_durs, tts_clips), params)
        # _burn=False：字幕不烧进画面，只留 .srt 交付（见 _burn_enabled 的说明）
        vf = []
        # HDR → SDR：走这条路径说明没有真剪（_no_cut），video_path 仍可能是原始 HDR 片源，
        # 必须在这里补一次映射，否则成片照样是「HDR 标记 + 8bit」。
        _tm_here = ''
        try:
            if probe_is_hdr(video_path) and tonemap_available(video_path):
                _tm_here = tonemap_chain()
        except Exception:
            _tm_here = ''
        if _tm_here:
            vf.append(_tm_here)
        if grade:
            vf.append(grade)
        if _burn:
            esc = srt.replace('\\', '/').replace(':', '\\:')
            vf.append(f"subtitles='{esc}':force_style='{sub_style}'")
        if scale_filter:
            vf.append(scale_filter)
        # vf 可能整个为空（不烧字幕 + 不调色 + 不缩放）：此时 -vf '' 会让 ffmpeg 报错，
        # 直接复制视频流反而更快且零损失。
        if vf:
            cmd = ['-y', '-i', video_path, '-vf', ','.join(vf)]
            cmd += (['-c:a', 'copy'] if has_audio else ['-an'])
            cmd += _w.video_encode_args(bitrate=bitrate, tier=tier)
            if _tm_here:
                cmd += list(BT709_TAGS)
            cmd += ['-threads', '0', '-movflags', '+faststart', out]
        else:
            cmd = ['-y', '-i', video_path, '-map', '0:v:0', '-c:v', 'copy']
            cmd += (['-map', '0:a:0', '-c:a', 'copy'] if has_audio else ['-an'])
            cmd += ['-movflags', '+faststart', out]
        rc, o, e = ffmpeg_run(cmd)
        if rc != 0 or not os.path.exists(out):
            raise RuntimeError('单遍合并(无裁剪)编码失败: ' + e.decode('utf-8', 'ignore')[-300:])
        return out, new_spans, 0.0

    # ---- 有实质剪辑：解析计算新时间轴（与两遍管线 piece_durs 一致）----
    new_spans, cur, piece_durs = [], 0.0, []
    for _i, (a, b) in enumerate(raw):
        seg_len = b - a
        f = _display_factor(seg_len, (voice_durs or {}).get(_i)) if voice_durs else 1.0
        d = seg_len * f if abs(f - 1.0) > 0.005 else seg_len
        piece_durs.append(d)
    n = len(raw)
    # 交叉淡化：仅当显式开启且每段都足够长（≥0.6s，避免极短片叠加淡入淡出产生闪烁/错位）才启用
    use_xfade = (transition == 'crossfade' and n >= 2 and all(d >= 0.6 for d in piece_durs))
    if use_xfade:
        # 每段（除末段）尾随 _FADE 与下段首 _FADE 重叠混合，时间轴整体压缩 (n-1)*_FADE。
        # 字幕/配音 SRT 必须按压缩后的时间轴定位，否则整体偏移 —— 这里压缩 new_spans，
        # 后续 _render_narrate 会把 segs 接管为 new_spans，配音与字幕窗口自动跟随。
        _starts = [0.0]
        for i in range(1, n):
            _starts.append(round(_starts[-1] + piece_durs[i - 1] - _FADE, 3))
        for i in range(n):
            _eff = piece_durs[i] - _FADE if i < n - 1 else piece_durs[i]
            new_spans.append((_starts[i], round(_starts[i] + _eff, 3)))
        cur = round(_starts[-1] + piece_durs[-1], 3)
        _log.info(f'[DIAG] 启用交叉淡化: 段数={n} FADE={_FADE}s 压缩后时长={cur:.2f}s')
    else:
        for a, b in raw:
            _d = piece_durs[len(new_spans)]
            new_spans.append((round(cur, 3), round(cur + _d, 3)))
            cur += _d
    cut_sec = round(max(0.0, gap), 3)

    # 字幕 SRT 基于新时间轴 new_spans 定位（与两遍管线 _compose_narration_video 内一致）
    srt = _build_narr_srt(new_spans, narr, run_dir, _build_voice_spans(new_spans, voice_durs, tts_clips), params)
    esc = srt.replace('\\', '/').replace(':', '\\:')

    # ---- 构造单遍滤镜图 ----
    # 视频链：每段 [0:v]trim → setpts 复位并施加微变速(f) → (xfade 交叉淡化 | concat 硬切) → 烧字幕 → 缩放
    # 音频链：每段 [0:a]atrim → asetpts 复位 → atempo(1/f 与画面同步) → (acrossfade | concat)
    vparts, aparts = [], []
    for i, (a, b) in enumerate(raw):
        seg_len = b - a
        f = _display_factor(seg_len, (voice_durs or {}).get(i)) if voice_durs else 1.0
        # 复位+微变速一步到位；f≈1 时仍是「复位到 0」以满足 concat 拼接要求
        vparts.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts={f:.6f}*(PTS-STARTPTS)[sv{i}]")
        if has_audio:
            af = (1.0 / f) if abs(f - 1.0) > 0.005 else 1.0
            # 微变速因子在 ±15% 内，1/f 落在 atempo 安全区间 [0.5,2]，无需链式
            aparts.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,atempo={af:.6f}[sa{i}]")
    # 视频拼接：交叉淡化用 xfade 链，硬切用 concat
    if use_xfade:
        # 第 k 次融合（累积段 X 与 seg_k）offset = 累积前 k 段时长 - k*_FADE，恰好落在 X 末尾前 _FADE 处
        _vlabel = 'sv0'
        for k in range(1, n):
            _cum = sum(piece_durs[:k])
            _off = round(_cum - k * _FADE, 3)
            _nxt = 'vxf' if k == n - 1 else f'x{k}'
            vparts.append(f"[{_vlabel}][sv{k}]xfade=transition=fade:duration={_FADE}:offset={_off:.3f}[{_nxt}]")
            _vlabel = _nxt
    else:
        vlabels = ''.join(f'[sv{i}]' for i in range(n))
        vparts.append(f"{vlabels}concat=n={n}:v=1:a=0[vmid]")
        _vlabel = 'vmid'
    # HDR → SDR：整片拼好后统一映射一次（一次运算、全片一致）。
    # 这条是「有实质剪辑」的单遍管线，直接吃原始片源，不调色就会把 PQ 标记带到成品。
    _tm_v = ''
    try:
        if probe_is_hdr(video_path) and tonemap_available(video_path):
            _tm_v = tonemap_chain()
    except Exception:
        _tm_v = ''
    if _tm_v:
        vparts.append(f"[{_vlabel}]{_tm_v}[vtm]")
        _vlabel = 'vtm'
    # P0-1 调色：整片拼好后统一上一遍（一次运算、全片一致）；默认 native 不加，串完全不变
    if grade:
        vparts.append(f"[{_vlabel}]{grade}[vgrade]")
        _vlabel = 'vgrade'
    if _burn:
        vparts.append(f"[{_vlabel}]subtitles='{esc}':force_style='{sub_style}'[vsub]")
        _vlabel = 'vsub'
    # 不烧字幕时不能让 'vsub' 这个标签悬空（scale_filter 那段引用了它），
    # 所以直接把当前标签名接力下去，而不是伪造一个 subtitles 滤镜。
    if scale_filter:
        vparts.append(f"[{_vlabel}]{scale_filter}[vout]")
    else:
        vparts.append(f"[{_vlabel}]copy[vout]")
    parts = list(vparts)
    if has_audio:
        if use_xfade:
            _alabel = 'sa0'
            for k in range(1, n):
                _nxt = 'xfa' if k == n - 1 else f'xa{k}'
                aparts.append(f"[{_alabel}][sa{k}]acrossfade=d={_FADE:.3f}[{_nxt}]")
                _alabel = _nxt
        else:
            alabels = ''.join(f'[sa{i}]' for i in range(n))
            aparts.append(f"{alabels}concat=n={n}:v=0:a=1[amid]")
        parts += aparts

    _audio_map = '[xfa]' if (use_xfade and has_audio) else '[amid]'
    cmd = ['-y', '-i', video_path, '-filter_complex', ';'.join(parts), '-map', '[vout]']
    if has_audio:
        cmd += ['-map', _audio_map, '-c:a', 'aac', '-b:a', '160k', '-ar', '44100', '-ac', '2']
    else:
        cmd += ['-an']
    cmd += _w.video_encode_args(bitrate=bitrate)
    if _tm_v:
        cmd += list(BT709_TAGS)
    cmd += ['-threads', '0', '-movflags', '+faststart', out]
    rc, o, e = ffmpeg_run(cmd)
    if rc != 0 or not os.path.exists(out):
        raise RuntimeError('单遍合并编码失败: ' + e.decode('utf-8', 'ignore')[-300:])

    # 拼接时长校验（与两遍管线一致）：实际 vs 预期，偏差>0.5s 按比例缩放 new_spans
    actual_dur = probe_audio_len(out) or cur
    if actual_dur > 0 and abs(actual_dur - cur) > 0.5:
        _scale = actual_dur / cur
        _log.info(f'[DIAG] 单遍合并时长偏差: 预期={cur:.1f}s 实际={actual_dur:.1f}s 缩放={_scale:.3f}')
        new_spans = [(round(s * _scale, 3), round(min(actual_dur, ed * _scale), 3)) for s, ed in new_spans]
    return out, new_spans, cut_sec


# ---------------------------------------------------------------------------
# P0-2 配乐情绪包络：让 BGM 跟着剧情起伏，而不是全程一条平线
# ---------------------------------------------------------------------------
# 为什么可以「零新增推理」：解说段落的情绪本来就已经算出来了（tts_engines.emotion_of
# 与 prosody_for_tts 共用同一张关键词表），重要度标签也已经由 _beat_plan 落盘
# （key/advance/transition/mood）。这里只是把这些现成信号旁路一份给混音用。
#
# 音量档位（相对基线 0.18）的取值不是拍脑袋：BGM 抬到 0.22 已接近人声可懂度阈值，
# 再高就会盖字；压到 0.12 仍然「听得见但让位」。限幅写死在 [0.10, 0.24] 内，
# 配合链尾 alimiter + loudnorm，总响度不会漂。
_BGM_BASE = 0.18          # 基线（与改动前 volume=0.18 一致，保证「包络失效」时回到旧行为）
_BGM_TIER = {
    'hook': 0.225,        # 开场钩子：推起来抓人
    'key': 0.205,         # 关键/高光：推进感
    'tense': 0.200,       # 紧张/激动
    'sad': 0.125,         # 悲伤：让位，留白
    'soft': 0.145,        # 温柔：轻轻垫着
    'transition': 0.140,  # 过渡/铺垫：让位给人声
    'mood': 0.155,        # 氛围
    'advance': 0.180,     # 推进（=基线）
    'close': 0.105,       # 收束：收到最低
}
_BGM_LO, _BGM_HI = 0.10, 0.24   # 包络限幅，防止表达式写错导致忽大忽小/静音
_BGM_RAMP = 0.45                # 换档渐变时长(s)：太短会有「音量台阶」的抽动感
_BGM_HOOK_SEC = 7.0             # 开场钩子窗(s)


def bgm_envelope(segs, narr, importance=None, vdur=0.0, enabled=True):
    """按段落情绪/重要度生成 BGM 音量包络：[{start, end, vol, tier}]。

    - segs: 与 narr 一一对应的 (start, end) 新时间轴（剪辑后的）
    - narr: 每段解说词（可为空串）；情绪从中提取
    - importance: 可选，与 narr 对齐的 'key'/'advance'/'transition'/'mood' 列表
    - 返回按时间排序、已合并相邻同档的区段；enabled=False 或信息不足时返回 []（调用方回退旧行为）
    """
    if not enabled or not segs:
        return []
    n = min(len(segs), len(narr) if narr else len(segs))
    if n <= 0:
        return []
    try:
        from tts_engines import emotion_of
    except Exception:
        emotion_of = None
    _IMP_TIER = {'key': 'key', 'advance': 'advance', 'transition': 'transition', 'mood': 'mood'}

    def _base_tier(i, s0, txt, imp):
        """单段的基础档位（不含开场钩子——钩子只作用于片头那一小段）。"""
        emo = emotion_of(txt) if (emotion_of and txt) else '解说'
        # 优先级：悲伤/温柔（人声最需要让位）> 关键/高光 > 紧张/激动 > 重要度兜底
        if emo == '悲伤':
            return 'sad'
        if emo == '温柔':
            return 'soft'
        if imp == 'key':
            return 'key'
        if emo in ('紧张', '激动'):
            return 'tense'
        return imp or 'advance'

    rows = []
    for i in range(n):
        try:
            s0, s1 = float(segs[i][0]), float(segs[i][1])
        except Exception:
            continue
        if s1 - s0 <= 0.05:
            continue
        txt = ''
        if narr and i < len(narr):
            txt = _strip_tts_markup(narr[i]) or ''
        imp = ''
        if importance and i < len(importance):
            imp = _IMP_TIER.get(str(importance[i] or ''), '')
        tier = _base_tier(i, s0, txt, imp)
        # 开场钩子只覆盖「片头那一小段」：第一段若跨过钩子窗，就在窗口处切开，
        # 不能让一个 15 秒的长开场把中段的高光/紧张一起吞成 hook 档
        # （否则整片 BGM 是一条平线，包络等于没做）。
        if i == 0 and s0 < _BGM_HOOK_SEC:
            if s1 <= _BGM_HOOK_SEC:
                rows.append([s0, s1, _BGM_TIER['hook'], 'hook'])
            else:
                rows.append([s0, _BGM_HOOK_SEC, _BGM_TIER['hook'], 'hook'])
                rows.append([_BGM_HOOK_SEC, s1, _BGM_TIER.get(tier, _BGM_BASE), tier])
            continue
        rows.append([s0, s1, _BGM_TIER.get(tier, _BGM_BASE), tier])
    if not rows:
        return []
    rows.sort(key=lambda r: r[0])
    # 收束：最后一段若已经接近片尾，降到 close 档（留白收尾，避免音乐压着金句走）
    if len(rows) >= 2 and (not vdur or rows[-1][1] >= vdur * 0.90):
        rows[-1][2] = _BGM_TIER['close']
        rows[-1][3] = 'close'
    # 相邻同档合并：表达式里每个台阶都要多一对 clip()，能减就减
    merged = [rows[0]]
    for s0, s1, vol, tier in rows[1:]:
        last = merged[-1]
        if tier == last[3] and abs(s0 - last[1]) < 0.02:
            last[1] = s1
        else:
            merged.append([s0, s1, vol, tier])
    return [{'start': round(a, 3), 'end': round(b, 3), 'vol': round(v, 4), 'tier': t}
            for a, b, v, t in merged]


def bgm_volume_expr(env, base=_BGM_BASE, ramp=_BGM_RAMP):
    """把包络区段转成 ffmpeg 的 volume 表达式（eval=frame）。

    用「逐档叠加 clip 斜坡」而不是 if() 嵌套：
      · clip((t-a)/ramp,0,1) 给出 0→1 的线性斜坡，天然没有台阶爆音；
      · 每一档只是 +Δ / -Δ，表达式长度与段数线性相关，不会像 if() 嵌套那样指数膨胀；
      · ffmpeg 的 volume 表达式支持 clip()，实测可用（见 tests/test_grade_bgm.py）。

    env 为空时返回 None —— 调用方保持旧的 volume=<base> 常量，输出与改动前一致。
    """
    if not env:
        return None
    # 起点用基线，之后每一段在它开始处抬/降到目标值（相对前一档的差值）
    expr = '%.4f' % base
    prev = base
    for seg in env:
        tgt = float(seg['vol'])
        delta = tgt - prev
        if abs(delta) < 1e-4:
            prev = tgt
            continue
        a = max(0.0, float(seg['start']) - (ramp / 2.0))   # 斜坡以换档点为中点，听感更自然
        expr += '%+.4f*clip((t-%.3f)/%.3f,0,1)' % (delta, a, ramp)
        prev = tgt
    return expr


def _build_subtitle_style(params):
    sub = params.get('subtitle') or {}
    font_size = int(sub.get('fontSize', 22))
    # 颜色：用户给#RRGGBB，转成ASS的&HBBGGRR格式
    def _hex_to_ass(h):
        h = h.lstrip('#')
        if len(h) == 6:
            return '&H%s%s%s' % (h[4:6], h[2:4], h[0:2])
        return '&H00FFFFFF'
    primary = _hex_to_ass(sub.get('color', '#FFFFFF'))
    outline_c = _hex_to_ass(sub.get('outlineColor', '#000000'))
    outline_w = float(sub.get('outlineWidth', 2))
    alignment = int(sub.get('alignment', 2))  # 1=左下 2=中下 3=右下 5=左上 8=中上
    margin_v = int(sub.get('marginV', 50))
    # letterbox 有上下黑边时，字幕默认 MarginV 会落进黑边里。按「有效画面区」
    # 把字幕抬回画面内（与 build_video_filter 共用同一份计算，防两侧漂移）。
    try:
        _rect = _w.effective_frame_rect(params)
    except Exception:
        _rect = None
    if _rect:
        # 竖屏/高分辨率导出时按画布高度等比放大，否则 22 号字在 1920 高画布上小得看不清
        if _rect[1] > 720:
            font_size = max(font_size, int(round(font_size * _rect[1] / 720.0)))
        if _rect[5] > 0:
            margin_v += int(_rect[5])
    font_name = sub.get('fontName', 'Microsoft YaHei')
    return (f'FontName={font_name},FontSize={font_size},'
            f'PrimaryColour={primary},OutlineColour={outline_c},Outline={outline_w},'
            f'BorderStyle=1,Shadow=1,'
            f'Alignment={alignment},MarginV={margin_v}')


def _compose_narration_video(video_path, segs, narr, tts_paths, run_dir, params, music_path=None,
                             voice_spans=None, burned=False, quote_spans=None):
    """把解说配音按时间轴混入原视频，烧录解说字幕，可选叠加背景音乐，输出 final.mp4。
    - narr[i] 对应 segs[i]（镜头段时间轴），作为该段字幕与配音文案。
    - tts_paths: [(audio_path, start_sec)]，按各自起始时间对齐到时间轴。
    - voice_spans: 可选 {seg_idx: (start,end)}，字幕窗口跟随配音（有声才显字、念完即收）；
      缺省整段显示（兼容旧行为）。
    - music_path: 可选 BGM，循环铺底、低音量。"""
    vdur = probe_audio_len(video_path) or 10.0
    if burned:
        # P1-1 单遍合并已把字幕+缩放在上一步烧进画面，这里直接复用，不再二次编码
        base_video = video_path
    else:
        # HDR → SDR：正常情况上游已调色（这里拿到的是 SDR 剪辑片）；只有「无需剪辑直接
        # 合成」时 video_path 才是原始 HDR 片源，此时补一次映射，避免输出带 PQ 标签。
        # 放在 srt 构建之前，保持「_build_narr_srt 必须无条件调用」的代码就近可见。
        _tm_comp = ''
        try:
            if probe_is_hdr(video_path) and tonemap_available(video_path):
                _tm_comp = tonemap_chain()
        except Exception:
            _tm_comp = ''
        # 1) 烧字幕：解说词按段显示；有配音时间窗时字随声走
        #    注意 _build_narr_srt 必须无条件调用：不烧进画面时它产出的 narr.srt
        #    就是要交付给用户的那个文件（见 _burn_enabled）。
        srt = _build_narr_srt(segs, narr, run_dir, voice_spans, params)
        vsub = os.path.join(run_dir, 'vsub.mp4')
        sub_style = _build_subtitle_style(params)
        # 导出选项：分辨率缩放 + 码率
        vf_parts = [_tm_comp] if _tm_comp else []
        _grade = _w.grade_filter(params)
        if _grade:
            # 调色在字幕之前：白色字幕不被 colorbalance 染色
            vf_parts.append(_grade)
        if _burn_enabled(params):
            esc = srt.replace('\\', '/').replace(':', '\\:')
            vf_parts.append(f"subtitles='{esc}':force_style='{sub_style}'")
        scale_filter = _w.build_video_filter(params)
        if scale_filter:
            vf_parts.append(scale_filter)
        bitrate = params.get('bitrate') or None
        tier = params.get('qualityTier') or None
        if not vf_parts:
            # 不烧字幕 + 不调色 + 不缩放：无需重编码，直接复制视频流
            # （-vf '' 会被 ffmpeg 判成参数错误；且这一步本来就只是搬运）
            rc, o, e = ffmpeg_run(['-y', '-i', video_path, '-map', '0:v:0', '-c:v', 'copy',
                                   '-an', '-movflags', '+faststart', vsub])
        else:
            rc, o, e = ffmpeg_run(['-y', '-i', video_path,
                                   '-vf', ','.join(vf_parts),
                                   ] + _w.video_encode_args(bitrate=bitrate, tier=tier)
                                  + (list(BT709_TAGS) if _tm_comp else [])
                                  + ['-threads', '0', '-an', vsub])
        base_video = vsub if (rc == 0 and os.path.exists(vsub)) else video_path

    has_orig_audio = _has_audio_track(video_path)
    use_music = bool(music_path) and os.path.exists(music_path or '')

    # 没有任何音频需要混：直接保留画面（含字幕）
    if not tts_paths and not has_orig_audio and not use_music:
        final = os.path.join(run_dir, 'final.mp4')
        cmd = ['-y', '-i', base_video, '-c:v', 'copy', '-an', '-movflags', '+faststart', final]
        rc, o, e = ffmpeg_run(cmd)
        if rc != 0:
            raise RuntimeError('成片失败: ' + e.decode('utf-8', 'ignore')[-300:])
        return final

    # 构建音频滤镜图
    # tts_paths 元素：2 元组 (audio, start) 或 3 元组 (audio, start, end)，后者用于把配音裁剪在本镜头段内，
    # 避免配音时长超过镜头段导致的跨段语音重叠。
    # 「成片不含解说」档（audio.layers==2）：解说不进最终混音（无 ducking、无 [t*]），
    #   成片=原声/配乐；解说只走 build_voice_track 出 .wav（调用方已保证导出）。
    #   混音侧把 tts_paths 视为空即可——四条混音分支全部按 _mix_tts 走。
    _narr_out = _narr_out_enabled(params)
    _mix_tts = [] if _narr_out else (tts_paths or [])
    def _tt_span(item):
        # item: (path, start, end, orig_volume_or_None) 或 5 元组 (..., narr_volume_or_None)
        #      或 7 元组 (..., narr_volume, fade_in, fade_out)（2026-11-06 每段淡入淡出）
        if len(item) >= 7:
            return item[0], item[1], item[2], item[3], item[4], item[5], item[6]
        if len(item) >= 6:
            return item[0], item[1], item[2], item[3], item[4], item[5], None
        if len(item) >= 5:
            return item[0], item[1], item[2], item[3], item[4], None, None
        if len(item) >= 4:
            return item[0], item[1], item[2], item[3], None, None, None
        if len(item) >= 3:
            return item[0], item[1], item[2], None, None, None, None
        return item[0], item[1], None, None, None, None, None

    inputs = ['-y', '-i', base_video]   # 0 = 字幕视频(画面)
    fparts = []
    orig_label = ''                     # 原声那一路的标签（没有原声时为空）
    next_idx = 1                        # 下一个音频输入索引（显式记录，避免 count("-i") 脆弱）
    if has_orig_audio:
        inputs += ['-i', video_path]    # 1 = 原视频(音频)
        if _mix_tts:
            # 解说配音存在时做 ducking：解说段内原声按用户设置音量，缝隙还原 0.5。
            # 原声引用段（quote_spans）优先级最高：这段原声 1.0 全量保留
            #（博主的「原片原声引用」：解说提到的话/精彩片段，原片自己说）
            expr = '0.5'
            for _qs, _qe in (quote_spans or []):
                expr = "if(between(t,%.2f,%.2f),1.0,%s)" % (_qs, _qe, expr)
            for item in _mix_tts:
                np_, od, _oe, _ovol, _nv, _fi_x, _fo_x = _tt_span(item)
                dur = probe_audio_len(np_) or 3.0
                # 用户设置了原片音量则用用户值（0-100 -> 0-1.0），否则默认0.08
                vol = (_ovol / 100.0) if (_ovol is not None and _ovol > 0) else 0.08
                expr = "if(between(t,%.2f,%.2f),%.3f,%s)" % (od, od + dur, vol, expr)
            # 注意：表达式含逗号，必须用单引号包裹，否则 ffmpeg 会把逗号当作滤镜链分隔符导致解析失败
            fparts.append("[1:a]volume='%s':eval=frame[orig]" % expr)
        else:
            # 无解说（纯 B-roll/原声引用）：引用段原声 1.0，其余 0.5
            _expr = '0.5'
            for _qs, _qe in (quote_spans or []):
                _expr = "if(between(t,%.2f,%.2f),1.0,%s)" % (_qs, _qe, _expr)
            fparts.append("[1:a]volume='%s':eval=frame[orig]" % _expr)
        orig_label = '[orig]'
        next_idx += 1
    tt_labels = []
    for k2, item in enumerate(_mix_tts):
        np_, od, oe, _ov, _nv, _fi, _fo = _tt_span(item)
        # 每段解说音量（PR 式右键调节；None=不介入保持 1.0，范围 0~2.0）
        _nv_f = ''
        if _nv is not None:
            _nv_f = 'volume=%.3f,' % max(0.0, min(2.0, float(_nv)))
        # 每段淡入/淡出（2026-11-06 计划#6）：afade 在 atrim 之后、adelay 之前——
        # 此时音频时间轴 0 起点；淡入 st=0，淡出 st=可用时长-淡出秒。可用时长：
        # atrim 段用 dmax，否则用音频实测时长。淡入/淡出各封顶可用时长一半（不叠死）。
        _fade_f = ''
        try:
            _fi_v = max(0.0, min(5.0, float(_fi or 0.0)))
            _fo_v = max(0.0, min(5.0, float(_fo or 0.0)))
        except Exception:
            _fi_v = _fo_v = 0.0
        if _fi_v > 0 or _fo_v > 0:
            _adur = (oe - od) if (oe is not None and oe > od) else (probe_audio_len(np_) or 0.0)
            if _adur > 0.05:
                if _fi_v > 0:
                    _fi_c = min(_fi_v, _adur / 2.0)
                    _fade_f += 'afade=t=in:st=0:d=%.2f,' % _fi_c
                if _fo_v > 0:
                    _fo_c = min(_fo_v, _adur / 2.0)
                    _fade_f += 'afade=t=out:st=%.2f:d=%.2f,' % (max(0.0, _adur - _fo_c), _fo_c)
        inputs += ['-i', np_]
        if oe is not None and oe > od:
            # 限制配音时长不超过本镜头段，杜绝「这段解说拖到下一段画面」的重叠。
            # 【顺序极其重要】必须先 atrim 再 adelay：
            #   adelay=3000 + atrim=0:2 会保留「延迟后」流的 [0,2] 秒——而这段全是静音，
            #   等于把整句配音删掉。实测只有首段（od=0）能出声，第二句起全哑，
            #   成片表现就是「配了第一句之后就没有声音」。
            dmax = max(0.05, oe - od)
            fparts.append(f'[{next_idx + k2}:a]aresample=44100,{_nv_f}'
                          f'atrim=start=0:end={dmax:.2f},{_fade_f}'
                          f'adelay={int(od * 1000)}|{int(od * 1000)},'
                          f'apad=whole_dur={vdur:.2f}[t{k2}]')
        else:
            fparts.append(f'[{next_idx + k2}:a]aresample=44100,{_nv_f}{_fade_f}adelay={int(od * 1000)}|{int(od * 1000)},'
                          f'apad=whole_dur={vdur:.2f}[t{k2}]')
        tt_labels.append(f'[t{k2}]')
    if use_music:
        # 【任务4】配乐淡入淡出：默认 3 秒，视频短于 6 秒时长减半但不低于 0.5s，避免硬切。
        _fade = 3.0 if vdur >= 6.0 else max(0.5, vdur / 4.0)
        _fade_out_st = max(0.0, vdur - _fade)
        # [P1-4] 有限次循环：用 -stream_loop N（N 算好，N≥ceil(vdur/音乐时长)+1），既有 EOF 又不会像
        #        -stream_loop -1 那样让 apad 永久挂起（实测解说+配乐必卡死），保证长视频全程有音乐。
        mdur = probe_audio_len(music_path) or 0.0
        _bgm_in = ['-i', music_path]
        if mdur > 0 and mdur < vdur:
            _loop_n = int(vdur / mdur) + 2   # 多 +1 防浮点误差，atrim 精确截断
            _bgm_in = ['-stream_loop', str(_loop_n)] + _bgm_in
        inputs += _bgm_in
        _bgm_idx = next_idx + len(_mix_tts)
        # [P0-2] 情绪包络：用已算好的段落情绪/重要度生成随剧情起伏的音量曲线。
        #   · 默认开启（params.bgmEnvelope 显式 false 才关），但只在「有分段信息」时生效；
        #   · 生成失败/信息不足 → 回退旧的常量 volume=0.18（与改动前完全一致）；
        #   · 表达式含逗号（clip(a,b,c)），必须用单引号包裹，否则 ffmpeg 会把逗号当链分隔符。
        _env_expr = None
        try:
            if params.get('bgmEnvelope', True):
                _imp = params.get('_importance') or []
                _env = bgm_envelope(segs, narr, importance=_imp, vdur=vdur)
                _env_expr = bgm_volume_expr(_env)
                if _env_expr:
                    _tiers = '/'.join(f"{e['tier']}@{e['start']:.1f}s" for e in _env[:8])
                    _log.info(f'[DIAG] BGM情绪包络 {len(_env)}档: {_tiers}')
        except Exception as _ee:
            _log.info(f'[DIAG] BGM情绪包络生成失败，回退常量音量: {_ee}')
            _env_expr = None
        _bgm_vol = (f"volume='{_env_expr}':eval=frame" if _env_expr
                    else f'volume={_BGM_BASE}')
        # 有包络时淡入要快：3s 的慢淡入会把「开场钩子」推起来的力气直接抹平，
        # 听感上就是「开头没音乐、后面才起来」。压到 1.2s，第一秒内就有声。
        _fade_in = min(_fade, 1.2) if _env_expr else _fade
        # 基线音量 0.18：给侧链闪避留出「压低到更轻」的余量（详见下方 sidechaincompress）
        fparts.append(f'[{_bgm_idx}:a]aresample=44100,{_bgm_vol},'
                      f'atrim=0:{vdur:.2f},'
                      f'afade=t=in:st=0:d={_fade_in:.2f},'
                      f'afade=t=out:st={_fade_out_st:.2f}:d={_fade:.2f}[bgmraw]')
        if _mix_tts:
            # [P1-3] 侧链闪避（sidechain ducking）：用「全部解说配音混成的人声总线」驱动 BGM 增益，
            #         人声一开口 BGM 自动压低，人声停缓升。这是解说视频人声清晰度质的飞跃。
            #
            # 【致命缺陷修复】人声总线必须 asplit 成两条：
            #   侧链压缩要「读」人声做触发信号，最终 amix 也要「读」人声出声音；
            #   而 ffmpeg 的 link label 是**一次性消费**的——同一个 [vbus] 被两处引用，
            #   会直接报 `Stream specifier 't0' ... matches no streams / Error binding
            #   filtergraph inputs/outputs`，整条音频链两级全败，
            #   最后落到「丢弃配音」兜底 → 成片有画面有字幕有原声，唯独没有解说。
            #   实测：开配乐 + 有配音时 100% 触发（只看代码看不出，只有真跑才会撞到）。
            fparts.append(f'{"".join(tt_labels)}amix=inputs={len(tts_paths)}:normalize=0,'
                          f'aformat=sample_fmts=fltp:channel_layouts=stereo,'
                          f'asplit=2[vbus][vmix]')
            fparts.append(f'[bgmraw][vbus]sidechaincompress='
                          f'threshold=0.04:ratio=6:attack=15:release=250:detection=peak[bgm]')
            # 混音侧：人声走 vmix（[t*] 已被 amix 消费，不能再引用），BGM 走 bgm，原声照旧
            mixin = orig_label + '[vmix][bgm]'
        else:
            # 无人声（纯配乐片段）：无需闪避，直通
            fparts.append('[bgmraw]anull[bgm]')
            mixin = orig_label + '[bgm]'
    else:
        # 没有配乐：原声与各段配音各自独立进最终 amix
        mixin = orig_label + ''.join(tt_labels)
    # amix 的输入数必须按**实际接进去的标签**数算，不能按「配音文件数」：
    #   · 无配乐时：[t0]..[tn] 各自独立进 mixin → 算 n 份；
    #   · 有配乐时：人声先被 amix 成一条 vbus，再 asplit 出 vmix 参与最终混音 → 只算 1 份。
    # 算错会直接 `inputs=N` 与实际标签数不符，ffmpeg 报错 → 整链降级到「丢弃配音」。
    # 直接按 mixin 里实际拼出来的标签个数算，杜绝手工累加漂移。
    n_mix = mixin.count('[')
    if n_mix == 0:
        # 「成片不含解说」且原片无音轨、也无配乐：没有可混的输入（amix=inputs=0
        # 非法，会整链报错再降到「丢弃配音」兜底也救不了）。直接交付画面。
        final = os.path.join(run_dir, 'final.mp4')
        rc, o, e = ffmpeg_run(['-y', '-i', base_video, '-c:v', 'copy', '-c:a', 'aac',
                               '-b:a', '160k', '-ar', '44100', '-ac', '2',
                               '-movflags', '+faststart', final])
        if rc == 0 and os.path.exists(final):
            return final
        raise RuntimeError('成片失败（无可混音频输入）: ' + e.decode('utf-8', 'ignore')[-200:])
    # amix 前统一采样率/声道，避免不同来源（SAPI 22k mono / 云端 TTS 24-48k / 原声 44.1k stereo）混音异常或音量失衡
    # 2026-09-08 质量升级：amix 后接 alimiter 防爆音 + loudnorm 统一响度到 -16 LUFS（流媒体标准）。
    # 【安全设计】loudnorm 链路整体失败时，先回退到「只 alimiter 不 loudnorm」（保留防爆音），
    # 再不行才走原有的「丢弃配音」兜底。多一层防线，避免一个滤镜异常把整条配音链路打掉。
    _tail = ',alimiter=limit=0.95'
    _tail_ln = _tail + ',loudnorm=I=-16:TP=-1.5:LRA=11'
    base_audio_filter = f'{mixin}amix=inputs={n_mix}:normalize=0,aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo'
    fparts_loudnorm = fparts + [base_audio_filter + _tail_ln + '[aout]']
    fparts_safety   = fparts + [base_audio_filter + _tail + '[aout]']
    final = os.path.join(run_dir, 'final.mp4')
    for _label, _fp in (('loudnorm', fparts_loudnorm), ('no_loudnorm', fparts_safety)):
        cmd = inputs + ['-filter_complex', ';'.join(_fp), '-map', '0:v:0', '-map', '[aout]',
                        # 【v1.4.7 修】原来这里只写 -c:a aac，采样率跟着滤镜输出漂，
                        # 实测 20 分钟片出片是 96000 Hz（narr 源是 22050，链上明明
                        # aresample=44100，loudnorm 会把内部 192k 协商出来）。
                        # 96k AAC 在电视/车机/部分手机解码器上兼容差，且与同文件
                        # 其它 4 处（453/485/909/714 行）不一致。统一钉死 44.1k 立体声。
                        '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
                        '-ar', '44100', '-ac', '2', '-movflags', '+faststart', final]
        rc, o, e = ffmpeg_run(cmd)
        if rc == 0 and os.path.exists(final):
            return final
        _log.info(f'[DIAG] 音频链 {_label} 失败 rc={rc} stderr_len={len(e)}')
        try:
            # 完整命令与 stderr 落盘：混音失败的根因此前只留一句"失败"，诊断全靠猜
            with open(os.path.join(run_dir, '_audiodebug.txt'), 'a', encoding='utf-8') as _df:
                _df.write('==== %s rc=%s ====\nCMD: %s\nSTDERR:\n%s\n' % (_label, rc, ' '.join(cmd), e.decode('utf-8', 'ignore')))
        except Exception:
            pass
        _log.info(f'[DIAG] 音频链 {_label} 失败，尝试下一级降级')
    # 兜底：丢弃配音，仅保留原声/画面的简单封装（保留旧行为以保证「能出片」）
    fb = os.path.join(run_dir, 'final.mp4')
    rc2, o2, e2 = ffmpeg_run(['-y', '-i', base_video, '-c:v', 'copy', '-c:a', 'aac',
                              '-b:a', '160k', '-ar', '44100', '-ac', '2', fb])
    if rc2 == 0 and os.path.exists(fb):
        # 【防静默掉级】解说配音明明存在，兜底产物却没有音轨（base_video 是烧字幕的
        # -an 产物）——交付无声片是事故不是降级。抛错把两级失败的真实 stderr 带给用户。
        if (tts_paths or []) and not (probe_audio_len(fb) or 0):
            raise RuntimeError('混音两级失败（无声兜底已拦截）: ' + e.decode('utf-8', 'ignore')[-280:])
        return fb
    raise RuntimeError('混音失败: ' + e.decode('utf-8', 'ignore')[-300:])


# ---------------------------------------------------------------------------
# 回注宿主命名空间：webui_server._render_narrate 等旧入口继续可用
# ---------------------------------------------------------------------------
for _sym in ('_NAR_CPS', '_NAR_MIN_CHARS', '_NAR_MAX_CHARS', '_NAR_MAX_SPEED', '_NAR_MIN_SPEED',
             '_target_chars', '_fit_voice', '_render_narrate',
             '_merge_spans', '_cut_video_by_spans',
             '_build_narr_srt', '_build_voice_spans', '_cut_and_burn_video',
             '_build_subtitle_style', '_compose_narration_video',
             '_burn_enabled', '_audio_layers_enabled', '_voice_track_enabled',
             'build_voice_track'):
    setattr(_w, _sym, globals()[_sym])
