# -*- coding: utf-8 -*-
"""视频渲染核心（由 webui_server.py 拆出）。

包含：解说渲染管线（剪辑→配音→混音→烧字幕→配乐）的全部函数与常量。
约定与 workflows.py / handler.py 一致：宿主符号经 _w.<name> 晚绑定，
本模块公共符号在文件末尾注入回宿主命名空间。
"""
import logging, os, sys

# ---- 直接导入：已拆出的工具模块 ----
from ffmpeg_utils import ffmpeg_run, probe_audio_len, _has_audio_track, AbortError, _TLS
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


def _render_narrate(video_path, segs, narr, params, run_dir, progress=None, music_path=None, mode=None,
                    auto_cut=True, narr_map=None):
    """解说渲染阶段：按分镜剪辑(可选)→逐段配音→混音→烧字幕→配乐。

    auto_cut=True 时先按保留段真剪辑（剪掉未勾选/无解说的画面），字幕与配音自动对齐到
    剪辑后的新时间轴。返回 (final, voice_clips, cut_info)。"""
    def up(ph, pct):
        if progress:
            progress['phase'] = ph; progress['pct'] = pct

    # ---- 第一步：真剪辑（此前缺失，导致成片恒等于原片时长，「剪辑解说」名不副实）----
    src_video = video_path
    cut_info = {'cut_sec': 0.0, 'src_dur': round(probe_audio_len(video_path) or 0.0, 2),
                'out_dur': None, 'segs': len(segs)}
    if auto_cut:
        up('按分镜剪辑画面', 26)
        src_video, segs, cut_sec = _w._cut_video_by_spans(video_path, segs, run_dir, progress)
        cut_info['cut_sec'] = cut_sec
        cut_info['segs'] = len(segs)
    cut_info['out_dur'] = round(probe_audio_len(src_video) or cut_info['src_dur'], 2)
    # 高密度剪辑：一节解说词对应多个子片段，按 narr_map 聚合
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
    _log.info(f'[DIAG] auto_cut后: segs={len(segs)} 总时长={sum(b-a for a,b in segs):.1f}s 视频时长={cut_info["out_dur"]}s narr={len(narr)}')

    # 长度对齐保护：narr 与 segs 必须一一对应。模型偶尔多输出/少输出行，
    # 不修正会导致越界（i >= len(segs) 时全部堆在 0-10s）或后面段无解说。
    if len(narr) > len(segs):
        narr = narr[:len(segs)]
    elif len(narr) < len(segs):
        narr = list(narr) + [''] * (len(segs) - len(narr))

    up('逐段配音', 30)
    tts_paths = []
    voice_spans = {}   # seg_idx -> (start, end)：字幕窗口跟随配音（有声才显字、念完即收）
    # 只在云端 TTS 真正配了 api_key+model 时才走云端；否则直接用本地免费引擎
    # 旧写法 _tts_available() 会把本地引擎也算进去，导致每段先试云端（无key必失败）再回退本地，长视频严重拖慢甚至后面超时失声
    _tcfg = load_ai_config().get('tts') or {}
    use_mimo = bool(_tcfg.get('api_key')) and bool(_tcfg.get('model'))
    # [2.3] 开跑前对首选引擎做一次「首段试点」：成功按当前引擎顺序走；
    #       失败立即在本 run 内降权切 sherpa/sapi（见 _ping_preferred_engine），
    #       避免几十段每段都先试错一次 edge（既省时间又避免无谓累计失败触发降权）。
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
        if clip is not None:
            # 配音时长自适应：念不完就用 atempo 适度提速贴合镜头，避免跨段重叠/腰斩
            v_len = probe_audio_len(clip) or max(0.5, span_len)
            fit = _fit_voice(v_len, span_len)
            if fit['speed'] > _NAR_MIN_SPEED:
                # 中间态改用 PCM WAV：TTS 源多为 mp3，再用 libmp3lame 重编码等于二次有损，
                # 人声会变闷。中间态无损，最终由成片统一编码一次即可。
                fast = os.path.join(run_dir, f'narr{i}_fit.wav')
                rc, _o, _e = ffmpeg_run(['-y', '-i', clip, '-vn',
                                         '-filter:a', 'atempo=%.3f' % fit['speed'],
                                         '-c:a', 'pcm_s16le', fast])
                if rc == 0 and os.path.exists(fast):
                    clip = fast
                    v_len = probe_audio_len(clip) or (v_len / fit['speed'])
            tts_paths.append((clip, seg_span[0], seg_span[1]))
            # 字幕只在「这句话正在被念」时显示：一行字挂满整个镜头段会让后段才发生的
            # 画面内容提前出现在段首，观感像字幕与时间轴错位
            voice_spans[i] = (seg_span[0], min(seg_span[1], seg_span[0] + v_len + 0.35))
    _log.info(f'[DIAG] 配音完成: tts_paths={len(tts_paths)}/{len([t for t in narr if t and t.strip()])} voice_spans={len(voice_spans)}')
    up('混音+烧字幕+配乐', 60)
    narr_srt = ['' if (t or '').strip() in ('（留白）', '(留白)') else strip_tts_markup(t) for t in narr]
    final = _w._compose_narration_video(src_video, segs, narr_srt, tts_paths, run_dir, params,
                                     music_path=music_path, voice_spans=voice_spans)
    if progress:
        progress['done'] = True
        progress['pct'] = 100
        progress['file'] = os.path.relpath(final, _w.OUTDIR).replace('\\', '/')
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


def _cut_video_by_spans(video_path, spans, run_dir, progress=None):
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
    raw = [(a, b) for a, b in raw if b - a > 0.02]
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
        if b - a > 0.02:
            _fixed.append((a, b))
    raw = _fixed
    if not raw:
        return video_path, raw, 0.0

    # 剪切用的区间做合并（重叠/紧邻不重复切），但**返回的时间轴必须逐段等长**：
    # 调用方 segs 与 narr 是一一对应的，这里少返回一段就会让字幕与配音整体错位。
    spans = _w._merge_spans(raw)
    if not spans:
        return video_path, raw, 0.0

    keep = sum(b - a for a, b in spans)
    gap = vdur - keep
    # 连续覆盖全片（中间没有实质空隙）→ 没有可剪的内容，直接跳过，省一次全片重编码
    covered_gap = sum(max(0.0, spans[i + 1][0] - spans[i][1]) for i in range(len(spans) - 1))
    if spans[0][0] <= 0.05 and vdur - spans[-1][1] <= 0.05 and covered_gap <= 0.25:
        return video_path, raw, 0.0

    cut_dir = os.path.join(run_dir, 'cuts')
    os.makedirs(cut_dir, exist_ok=True)
    has_audio = _has_audio_track(video_path)
    pieces = []
    try:
        for i, (s0, s1) in enumerate(spans):
            up('✂ 剪辑片段 %d/%d' % (i + 1, len(spans)), 34 + int(20 * i / max(1, len(spans))))
            p = os.path.join(cut_dir, 'cut%03d.mp4' % i)
            # -ss 放 -i 前走快 seek（重编码时仍精确到帧）；-t 用段长，避免 -to 语义混淆
            # 编码档用 interim（ultrafast+crf16）：这段还会被成片再编码一次，
            # 此处若用有损档会造成二次质量累积（详见 webui_server.video_encode_args_interim）
            cmd = ['-y', '-ss', '%.3f' % s0, '-i', video_path, '-t', '%.3f' % (s1 - s0)]
            cmd += _w.video_encode_args_interim()
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

        out = os.path.join(run_dir, 'cut.mp4')
        concat_txt = os.path.join(cut_dir, 'concat.txt')
        with open(concat_txt, 'w', encoding='utf-8') as f:
            for p in pieces:
                f.write("file '%s'\n" % p.replace('\\', '/').replace("'", "'\\''"))
        up('拼接保留片段', 56)
        rc, _o, e = ffmpeg_run(['-y', '-f', 'concat', '-safe', '0', '-i', concat_txt,
                                '-c', 'copy', '-movflags', '+faststart', out])
        if rc != 0 or not os.path.exists(out):
            # 各段编码参数不一致时 copy 会失败 → 兜底 filter concat（重编码，慢但稳）
            inputs = []
            for p in pieces:
                inputs += ['-i', p]
            fc = ''.join('[%d:v]' % i for i in range(len(pieces)))
            if has_audio:
                fc += ''.join('[%d:a]' % i for i in range(len(pieces)))
                fc += 'concat=n=%d:v=1:a=1[vout][aout]' % len(pieces)
                cmd = ['-y'] + inputs + ['-filter_complex', fc, '-map', '[vout]', '-map', '[aout]']
                cmd += _w.video_encode_args_interim() + ['-c:a', 'aac', '-b:a', '160k', '-threads', '0', out]
            else:
                fc += 'concat=n=%d:v=1:a=0[vout]' % len(pieces)
                cmd = ['-y'] + inputs + ['-filter_complex', fc, '-map', '[vout]']
                cmd += _w.video_encode_args_interim() + ['-threads', '0', out]
            rc, _o, e = ffmpeg_run(cmd)
        if rc != 0 or not os.path.exists(out):
            raise RuntimeError('拼接失败: ' + e.decode('utf-8', 'ignore')[-300:])
    except Exception:
        # 剪辑属增强项：失败就退回原片，保证「能出片」优先于「剪得漂亮」
        return video_path, raw, 0.0

    # 拼接后的新时间轴：按原始段逐段累计（段数与输入严格一致，保证与解说词一一对应）
    new_spans, cur = [], 0.0
    for a, b in raw:
        new_spans.append((round(cur, 3), round(cur + (b - a), 3)))
        cur += (b - a)
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


def _build_subtitle_style(params):
    """从params构建ffmpeg force_style字幕样式字符串。支持用户自定义。"""
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
    font_name = sub.get('fontName', 'Microsoft YaHei')
    return (f'FontName={font_name},FontSize={font_size},'
            f'PrimaryColour={primary},OutlineColour={outline_c},Outline={outline_w},'
            f'BorderStyle=1,Shadow=1,'
            f'Alignment={alignment},MarginV={margin_v}')


def _compose_narration_video(video_path, segs, narr, tts_paths, run_dir, params, music_path=None,
                             voice_spans=None):
    """把解说配音按时间轴混入原视频，烧录解说字幕，可选叠加背景音乐，输出 final.mp4。
    - narr[i] 对应 segs[i]（镜头段时间轴），作为该段字幕与配音文案。
    - tts_paths: [(audio_path, start_sec)]，按各自起始时间对齐到时间轴。
    - voice_spans: 可选 {seg_idx: (start,end)}，字幕窗口跟随配音（有声才显字、念完即收）；
      缺省整段显示（兼容旧行为）。
    - music_path: 可选 BGM，循环铺底、低音量。"""
    vdur = probe_audio_len(video_path) or 10.0
    # 1) 烧字幕：解说词按段显示；有配音时间窗时字随声走
    srt = os.path.join(run_dir, 'narr.srt')
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
    esc = srt.replace('\\', '/').replace(':', '\\:')
    vsub = os.path.join(run_dir, 'vsub.mp4')
    sub_style = _build_subtitle_style(params)
    # 导出选项：分辨率缩放 + 码率
    vf_parts = [f"subtitles='{esc}':force_style='{sub_style}'"]
    scale_filter = _w.build_video_filter(params)
    if scale_filter:
        vf_parts.append(scale_filter)
    vf_str = ','.join(vf_parts)
    bitrate = params.get('bitrate') or None
    rc, o, e = ffmpeg_run(['-y', '-i', video_path,
                           '-vf', vf_str,
                           ] + _w.video_encode_args(bitrate=bitrate) + ['-threads', '0', '-an', vsub])
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
    def _tt_span(item):
        # item: (path, start, end, orig_volume_or_None)
        if len(item) >= 4:
            return item[0], item[1], item[2], item[3]
        if len(item) >= 3:
            return item[0], item[1], item[2], None
        return item[0], item[1], None, None

    inputs = ['-y', '-i', base_video]   # 0 = 字幕视频(画面)
    fparts = []
    mixin = ''
    next_idx = 1                        # 下一个音频输入索引（显式记录，避免 count("-i") 脆弱）
    if has_orig_audio:
        inputs += ['-i', video_path]    # 1 = 原视频(音频)
        if tts_paths:
            # 解说配音存在时做 ducking：解说段内原声按用户设置音量，缝隙还原 0.5
            expr = '0.5'
            for item in tts_paths:
                np_, od, _oe, _ovol = _tt_span(item)
                dur = probe_audio_len(np_) or 3.0
                # 用户设置了原片音量则用用户值（0-100 -> 0-1.0），否则默认0.08
                vol = (_ovol / 100.0) if (_ovol is not None and _ovol > 0) else 0.08
                expr = "if(between(t,%.2f,%.2f),%.3f,%s)" % (od, od + dur, vol, expr)
            # 注意：表达式含逗号，必须用单引号包裹，否则 ffmpeg 会把逗号当作滤镜链分隔符导致解析失败
            fparts.append("[1:a]volume='%s':eval=frame[orig]" % expr)
        else:
            fparts.append('[1:a]volume=0.5[orig]')
        mixin = '[orig]'
        next_idx += 1
    for k2, item in enumerate(tts_paths):
        np_, od, oe, _ov = _tt_span(item)
        inputs += ['-i', np_]
        if oe is not None and oe > od:
            # 限制配音时长不超过本镜头段，杜绝「这段解说拖到下一段画面」的重叠
            dmax = max(0.05, oe - od)
            fparts.append(f'[{next_idx + k2}:a]aresample=44100,adelay={int(od * 1000)}|{int(od * 1000)},'
                          f'atrim=0:{dmax:.2f},apad=whole_dur={vdur:.2f}[t{k2}]')
        else:
            fparts.append(f'[{next_idx + k2}:a]aresample=44100,adelay={int(od * 1000)}|{int(od * 1000)},'
                          f'apad=whole_dur={vdur:.2f}[t{k2}]')
        mixin += f'[t{k2}]'
    if use_music:
        # 背景乐：单次输入 + atrim 截到视频时长 + apad 补到视频时长。
        # 不要用 `-stream_loop -1` 无限循环：apad(whole_dur) 需要读到输入 EOF 才会输出，
        # 无限循环流没有 EOF，会导致 ffmpeg 永久挂起（实测解说+配乐必卡死）。
        # 【任务4】配乐淡入淡出：默认 3 秒，视频短于 6 秒时长减半但不低于 0.5s，避免硬切。
        _fade = 3.0 if vdur >= 6.0 else max(0.5, vdur / 4.0)
        _fade_out_st = max(0.0, vdur - _fade)
        inputs += ['-i', music_path]
        fparts.append(f'[{next_idx + len(tts_paths)}:a]aresample=44100,volume=0.16,'
                      f'atrim=0:{vdur:.2f},apad=whole_dur={vdur:.2f},'
                      f'afade=t=in:st=0:d={_fade:.2f},'
                      f'afade=t=out:st={_fade_out_st:.2f}:d={_fade:.2f}[bgm]')
        mixin += '[bgm]'
    n_mix = len(tts_paths) + (1 if has_orig_audio else 0) + (1 if use_music else 0)
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
                        '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', final]
        rc, o, e = ffmpeg_run(cmd)
        if rc == 0 and os.path.exists(final):
            return final
        _log.info(f'[DIAG] 音频链 {_label} 失败，尝试下一级降级')
    # 兜底：丢弃配音，仅保留原声/画面的简单封装（保留旧行为以保证「能出片」）
    fb = os.path.join(run_dir, 'final.mp4')
    rc2, o2, e2 = ffmpeg_run(['-y', '-i', base_video, '-c:v', 'copy', '-c:a', 'aac', '-b:a', '160k', fb])
    if rc2 == 0 and os.path.exists(fb):
        return fb
    raise RuntimeError('混音失败: ' + e.decode('utf-8', 'ignore')[-300:])


# ---------------------------------------------------------------------------
# 回注宿主命名空间：webui_server._render_narrate 等旧入口继续可用
# ---------------------------------------------------------------------------
for _sym in ('_NAR_CPS', '_NAR_MIN_CHARS', '_NAR_MAX_CHARS', '_NAR_MAX_SPEED', '_NAR_MIN_SPEED',
             '_target_chars', '_fit_voice', '_render_narrate',
             '_merge_spans', '_cut_video_by_spans',
             '_build_subtitle_style', '_compose_narration_video'):
    setattr(_w, _sym, globals()[_sym])
