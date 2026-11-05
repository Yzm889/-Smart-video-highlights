# -*- coding: utf-8 -*-
"""电影解说模块（由 webui_server.py 拆出）。

剧情理解、详略规划、解说稿生成、镜头对齐、TTS 配音、电影合成。
约定与 beat_analysis.py / bili_downloader.py 一致：宿主符号经 _w.<name> 晚绑定，
本模块公共符号在文件末尾注入回宿主命名空间。
"""
import logging, os, sys, json, math, random, re, shutil, subprocess, threading, time, base64, itertools, tempfile

from ai_providers import (load_ai_config, _aborted, _note_progress_warning, asr_segments,
    local_llm_cfg, local_llm_chat, local_llm_light_chat, vlm_cfg, vlm_chat_multi,
    vlm_unload, whisper_model_name, whisper_language)
from cache_utils import (ANALYSIS_VERSION, _file_fp, _analysis_cache_load, _analysis_cache_save,
    _cache_load, _cache_save, _video_cache_key,
    _sample_frame_cache_dir, _sample_frame_cache_mark, _sample_frame_cache_ready,
    _sample_frame_cache_trim)
from ffmpeg_utils import (AbortError, PROGRESS, RUN_PROCS, _PROC_LOCK, _TLS,
    ffmpeg_exe, ffmpeg_run, probe_audio_len, _has_audio_track)
from text_utils import _clean_caption
from video_render import (_target_chars, _render_narrate, _cut_video_by_spans,
    _compose_narration_video, attach_importance)
from beat_analysis import _cached_scene_cuts
from tts_engines import (local_tts_speak, edge_tts_speak, edge_tts_available,
    edge_tts_dead_reason,
    _edge_internal, has_tts_markup, _enhance_tts_markup, strip_tts_markup)

_log = logging.getLogger('framecut.narrator')

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

# 单次喂给视觉模型的帧数。不是越大越好：每张图都要编码成 vision token，
# 12GB 显存下一次 8~10 张是安全区；再多既挤爆上下文，模型也会「忘记」中间的图。
VLM_BATCH_FRAMES = 8

# 剧情理解最多参考的段数上限（防止 2 小时视频把文字模型上下文撑爆）。
PLOT_SCAN_MAX_SEG = 120


def _scan_full_visual(frames, per_seg, params, progress=None):
    """全段画面扫描（事实层）：把**每一段**的中间帧按时间顺序分批交给视觉模型，
    逐段输出「画面里实际可见的内容」。返回 {段下标: 描述}；视觉模型不可用返回 {}。

    【为什么必须全扫——这是「AI 理解不够」的根因】
    旧实现在剧情理解时只用 8 帧代表全片：20 分钟视频切成 48 段时，等于
    **每 171 秒才看 1 帧**，画面信息利用率仅 17%，83% 的画面被丢掉。
    而「剧情」恰恰是全片最高层的理解任务，喂给它的信息反而最少——
    甚至低于它下属的逐段描述（14 帧）。这种**信息倒挂**使模型无法从事实里
    归纳出剧情，只能靠语言先验去「编」（幻觉）。参数怎么调都救不了，
    因为输入的信息量本身就不够。

    分工修正：
      · 视觉模型 → 只做它擅长的事：**描述看见了什么**（低幻觉、可在这一帧里验证）
      · 文字模型 → 做它擅长的事：**按时间顺序把事实串成因果剧情**（长文本推理）

    这样画面利用率从 17% 提到 100%，且剧情每一步都有画面/台词事实作依据。
    """
    idxs = [i for i in sorted(frames.keys()) if frames.get(i)]
    if not idxs:
        return {}
    n = len(idxs)
    if n > PLOT_SCAN_MAX_SEG:
        # 超长视频再均匀抽一次（此时每段已很短，抽掉一半损失可控）
        keep = [idxs[int(i * (n - 1) / (PLOT_SCAN_MAX_SEG - 1))] for i in range(PLOT_SCAN_MAX_SEG)]
        idxs = sorted(set(keep))
    caps = {}
    batches = [idxs[i:i + VLM_BATCH_FRAMES] for i in range(0, len(idxs), VLM_BATCH_FRAMES)]
    import re as _re
    for bi, batch in enumerate(batches):
        if progress is not None:
            try:
                progress['phase'] = ('画面全扫 %d/%d 批（看懂每一段，不再只看片头片尾）'
                                     % (bi + 1, len(batches)))
            except Exception:
                pass
        sys_ = '你是视频画面描述助手，只描述画面里实际可见的内容。'
        prompt = ('下面是同一段视频按时间顺序抽取的各镜头画面：第1张对应第1段，第2张对应第2段……依此类推。\n'
                  '请严格按顺序逐段输出每段画面里实际可见的内容（人物/动作/场景/画面上的文字）。\n'
                  '每段一行，格式严格为「第k段: 画面内容」，k 从 1 开始递增。\n'
                  '只描述看到的，不要推测剧情、不要总结、不要遗漏任何一段。')
        if bi > 0:
            # 跨批 continuity：告诉模型这只是接在前面的后续镜头，别当成新片子的开头
            prompt = '下面接的是上一个视频片段的后续镜头（时间上承前一批）。\n' + prompt
        try:
            out = vlm_chat_multi([frames[i] for i in batch], prompt, system=sys_, timeout=300) or ''
        except Exception as ex:
            if bi == 0:
                # 第一批就失败 = 视觉模型整体不可用，交给调用方走台词路线
                raise
            _note_progress_warning('画面全扫第%d批失败（%s），已跳过后续批次'
                                   % (bi + 1, str(ex)[:80]))
            break
        for l in out.splitlines():
            m = _re.match(r'^\s*第(\d+)段[:：]\s*(.+?)\s*$', l.strip())
            if not m:
                continue
            k = int(m.group(1)) - 1
            if 0 <= k < len(batch):
                caps[batch[k]] = m.group(2)
        if progress is not None:
            try:
                progress['pct'] = int(30 + 25 * (bi + 1) / max(1, len(batches)))
            except Exception:
                pass
    return caps


def _plot_brief_from_scan(caps, per_seg, params, sys_prompt=None):
    """基于「全段画面描述 + 全段台词」重建剧情（文字模型）。

    这是剧情理解的主力路径：画面事实由视觉模型逐段扫出来（见 _scan_full_visual），
    这里只做因果串联与概括。相比让视觉模型直接从稀疏抽帧猜剧情，这条路
    有事实依据、可回溯、幻觉显著更少。
    """
    lines = []
    for i, (s0, s1, txt) in enumerate(per_seg):
        cap = (caps.get(i) or '').strip()
        dlg = (txt or '').strip()
        if not cap and not dlg:
            continue
        piece = '%d分%02d秒-%d分%02d秒' % (int(s0) // 60, int(s0) % 60, int(s1) // 60, int(s1) % 60)
        if cap:
            piece += '【画面】' + cap[:180]
        if dlg:
            piece += '【台词】' + dlg[:180]
        lines.append(piece)
    if not lines:
        return None
    theme = (params.get('theme') or '').strip()
    name = (params.get('name') or '').strip()
    prompt = ('下面是同一段视频按时间先后排列的逐段记录（画面内容已由视觉识别得出，台词可能含识别误差）。\n'
              '请据此重构这段视频的完整剧情，回答：\n'
              '1) 这是一段什么内容（影视剧片段 / 纪录片 / 口播 / 风光等）；\n'
              '2) 若明显来自某部影视作品，点出片名与年代背景；不确定就写「无法确认片名」，不要编造；\n'
              '3) 按时间顺序讲清楚剧情：人物是谁、身在何处、发生了什么冲突、结果如何；\n'
              '4) 最后用 4~6 句话概括剧情（供后续解说使用）。\n'
              '要求：只依据上面给的记录推理，不要补充画面与台词之外的情节。\n')
    if name:
        prompt += '\n视频文件名：' + name
    if theme:
        prompt += '\n用户提供的主题/梗概：' + theme
    prompt += '\n\n逐段记录：\n' + '\n'.join(lines)
    sys = sys_prompt or '你是资深影视解说编辑，擅长依据画面与台词记录还原完整剧情。'
    # 剧情串写交给文字模型：它不是生成"看见什么"，而是推理因果，
    # 文字模型处理长文本与保持时序一致性明显强于多模态。
    try:
        if _local_model_available():
            out = local_llm_chat(prompt, system=sys, timeout=420, max_tokens=1500)
        else:
            out = _w.vlm_text(prompt, system=sys, timeout=420)
    except Exception:
        return None
    return (out or '').strip()[:1400] or None


def _plot_brief(frames, per_seg, params):
    """剧情理解（阶段一）：把整片关键帧（按时间顺序）+ 各段台词一次性喂给 VLM，
    判断片段类型 / 识别影视作品 / **结合台词重构具体剧情事件**，返回剧情梗概文本。失败返回 None。
    这是「剧情解说」与「画面描述」的分水岭：先让模型看懂整个故事，再写旁白。"""
    if not frames:
        return None
    _prog = params.get('_progress') if isinstance(params, dict) else None
    # —— 首选：全段画面扫描 → 文字模型串剧情（有全片事实依据，幻觉最少）——
    try:
        _caps = _scan_full_visual(frames, per_seg, params, progress=_prog)
    except Exception:
        _caps = {}
    if _caps:
        _note_progress_warning('已逐段扫描全部画面（%d 段），剧情基于全片事实重建' % len(_caps))
        _out = _plot_brief_from_scan(_caps, per_seg, params)
        if _out:
            if isinstance(_prog, dict):
                _prog['plot_brief'] = _out[:800]
                _prog['plot_source'] = '全段画面理解'
            return _out
        _note_progress_warning('剧情串写失败，改用视觉模型直接概览')
    # —— 兜底：视觉模型直接概览全片（信息稀疏，但总比没有剧情强）——
    idxs = sorted(frames.keys())
    # 均匀采样 8 帧：首尾必采、中间等距，确保长视频后面的画面也被模型看到
    # （旧实现 idxs[::step][:8] 只取前 8 个采样点，长视频末尾 10%+ 的画面完全缺失）
    n_pick = min(8, len(idxs))
    if n_pick > 1:
        pick_idx = [idxs[int(i * (len(idxs) - 1) / (n_pick - 1))] for i in range(n_pick)]
    else:
        pick_idx = [idxs[0]]
    picked = [frames[i] for i in pick_idx]
    if not picked:
        return None
    # 台词也均匀采样覆盖全片（最多 12 段），首尾必采；旧实现只取前 12 段，后面剧情的台词全部丢失
    n_dlg = min(12, len(per_seg))
    if n_dlg > 1:
        dlg_idx = [int(i * (len(per_seg) - 1) / (n_dlg - 1)) for i in range(n_dlg)]
    else:
        dlg_idx = [0]
    dlg = []
    _srcs = []
    if isinstance(params, dict):
        _srcs = params.get('_dlg_src') or []
    for j in dlg_idx:
        s0, s1, txt = per_seg[j]
        t = (txt or '').strip()
        # 来源标注只进提示词，不进 per_seg 数据流（下游 20+ 处直接解包 3 元组）
        _src = _srcs[j] if j < len(_srcs) else 'asr'
        _label = '画面字幕（可能含错字，仅作辅助）' if _src == 'onscreen' else '台词'
        dlg.append('%s-%s秒 %s：%s' % (int(s0), int(s1), _label,
                                      t[:100] if t else '(无台词画面)'))
    theme = (params.get('theme') or '').strip()
    name = (params.get('name') or '').strip()
    sys = '你是资深影视解说编辑，擅长从若干关键画面与台词重构一段视频的完整剧情。'
    prompt = ('以下是同一段视频中按时间先后排列的关键画面，以及对应时段的台词。\n'
              '台词分两类：「台词」来自语音识别（较可靠）；「画面字幕」来自画面识别（可信度低）。\n'
              '请回答：\n'
              '1) 这是一段什么内容（影视剧片段 / 纪录片 / 口播 / 风光等）；\n'
              '2) 如果明显来自某部影视作品，请点出片名与年代背景；无法确定时务必写明“无法确认片名”，不要编造；\n'
              '3) 重点：结合台词与画面，把这段讲的具体剧情事件写清楚——人物是谁、身在何处、在做什么、'
              '台词里透露了哪些冲突或信息（例如借钱/赶路/争吵/求助等），结果如何；\n'
              '4) 标注「画面字幕」的文本可能含台标错字或臆造，可信度低于「台词」；'
              '只作辅助参考，不得据此推断片名、年代、人物关系或剧情事实；\n'
              '5) 最后用 4~6 句话完整概括这段视频的剧情（供后续解说使用）。\n')
    if name:
        prompt += '\n视频文件名：' + name
    if theme:
        prompt += '\n用户提供的主题/梗概：' + theme
    prompt += '\n\n各时段台词：\n' + '\n'.join(dlg)
    try:
        out = vlm_chat_multi(picked, prompt, system=sys, timeout=300)
    except Exception as ex:
        _note_progress_warning('视觉剧情理解失败（%s: %s），正在改用台词重建剧情'
                               % (type(ex).__name__, str(ex)[:120]))
        # 自动补救①：只用台词（文字）重建剧情。文字模型比多模态稳得多，
        # 且台词本身就是剧情最可靠的线索——宁可剧情粗一点，也别没有剧情（没有剧情就只剩模板=乱剪）。
        return _plot_brief_from_text(per_seg, params, sys, prompt)
    out = (out or '').strip()
    if not out:
        _note_progress_warning('视觉剧情理解返回空内容，正在改用台词重建剧情')
        return _plot_brief_from_text(per_seg, params, sys, prompt)
    # 让"AI 到底理解成了什么剧情"可见：用户要能核对，否则无法判断剪得对不对
    _prog = params.get('_progress') if isinstance(params, dict) else None
    if isinstance(_prog, dict):
        _prog['plot_brief'] = out[:800]
        _prog['plot_source'] = '视觉理解'
    return out[:1200]


def _plot_brief_from_text(per_seg, params, sys_prompt=None, prompt_full=None):
    """台词版剧情重建（不依赖视觉模型）。

    视觉模型不可用/失败时的**自动补救**：把各时段台词喂给文字模型（qwen3 等），
    让它重构剧情。台词有识别误差，但仍比"没有剧情"强得多——
    没有剧情时后续只能套通用模板（等于乱剪），这是用户明确反馈的问题。
    """
    if not per_seg:
        return None
    try:
        texts = [((t or '').strip()) for _s0, _s1, t in per_seg]
        spoken = [(i, t) for i, t in enumerate(texts) if t]
        if len(spoken) < 2:
            _note_progress_warning('台词太少，无法重建剧情（本次按通用模板生成，剧情走向可能不准）')
            return None
        # 均匀采样最多 24 段台词，覆盖全片
        if len(spoken) > 24:
            step = (len(spoken) - 1) / 23.0
            spoken = [spoken[int(i * step)] for i in range(24)]
        lines = '\n'.join('%s. %s' % (i + 1, t[:120]) for i, t in spoken)
        name = (params.get('name') or '').strip()
        theme = (params.get('theme') or '').strip()
        p = ('以下是同一部视频按时间先后排列的台词（由语音识别得到，可能有少量错字）。\n'
             '请据此重构这段视频的剧情：人物是谁、在哪里、发生了什么、冲突是什么、结果如何。\n'
             '最后用 4~6 句话完整概括剧情（供后续写解说词用）。\n'
             '不确定的地方要写明"无法确认"，不要编造。\n')
        if name:
            p += '\n视频名：' + name
        if theme:
            p += '\n用户给的主题：' + theme
        p += '\n\n台词：\n' + lines
        try:
            out = local_llm_chat(p, system=sys_prompt or '你是资深影视解说编辑，擅长从台词重构剧情。',
                                 timeout=300)
        except Exception as ex:
            _note_progress_warning('台词重建剧情失败（%s: %s）；本次按通用模板生成，剧情走向可能不准'
                                   % (type(ex).__name__, str(ex)[:120]))
            return None
        out = (out or '').strip()
        if not out:
            _note_progress_warning('台词重建剧情返回空内容；本次按通用模板生成，剧情走向可能不准')
            return None
        _note_progress_warning('已用台词重建剧情（视觉模型不可用），解说稿基于台词理解生成')
        _prog = params.get('_progress') if isinstance(params, dict) else None
        if isinstance(_prog, dict):
            _prog['plot_brief'] = out[:800]
            _prog['plot_source'] = '台词重建'
        return out[:1200]
    except Exception:
        return None


def _try_parse_json(text):
    """从模型输出中尽力提取 JSON 对象。返回 dict 或 None。

    模型有时会一次吐出多个 JSON 对象（实测 qwen3 输出 3 组草稿，用换行隔开）。
    旧实现用贪婪正则匹配首尾花括号，会把 3 个对象连成一大块非法 JSON
    → 整段解析失败 → 已经生成好的草稿被全部丢弃，
    阶段①报 llm_parse_fail，编辑台这一段一个字都没有。
    现在改为逐个提取：第一个对象成功就返回；都失败才返回 None。
    """
    if not text:
        return None
    import json as _json
    import re as _re
    try:
        obj = _json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    # 逐个提取「平衡的 JSON 对象」：从每个 '{' 起，按括号深度找配对
    for m in _re.finditer(r'\{', text):
        start = m.start()
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == '\\':
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    try:
                        obj = _json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except Exception:
                        pass
                    break
    return None


def _beat_plan(per_seg, plot, params):
    """详略规划（文字模型）：基于剧情理解 + 各环节台词，判断每段在剧情中的作用与重要性
    （key 关键/转折/高光 → 展开讲；advance 推进 → 正常讲；transition 过渡/铺垫 → 一句带过；
    mood 氛围/情绪 → 简略渲染）。返回 {summary, beats:[{i,importance,role}]}。
    这是「详略有当、像写作文一样」的关键，也是「理解场景含义」的落点。失败回退默认全部 advance。"""
    n = len(per_seg)
    default = {'summary': '', 'beats': [{'i': i + 1, 'importance': 'advance', 'role': ''} for i in range(n)]}
    if not per_seg:
        return default
    ctx = []
    if plot:
        ctx.append('【视觉剧情理解】' + plot)
    for i, (s0, s1, txt) in enumerate(per_seg):
        ctx.append('环节%d(%s-%s秒)：%s' % (i + 1, int(s0), int(s1), (txt or '').strip()[:90] or '(无台词画面)'))
    sys_ = '你是资深电影解说导演，擅长把一段视频拆成有详有略的剧情节奏。'
    prompt = ('下面是这段视频的整体剧情理解与各时间环节。请像写作文一样为每个环节标注「详略级别」和「剧情作用」。\n'
              'importance 只允许这 4 个取值：\n'
              '- key：剧情关键 / 转折 / 高光，需要展开讲透；\n'
              '- advance：推进剧情，正常讲；\n'
              '- transition：过渡 / 铺垫，一句带过即可；\n'
              '- mood：氛围 / 情绪镜头，简略渲染即可。\n'
              'role：用一句话说明本段在剧情推进中的作用——人物此时在做什么/处境或事态怎么变化/为后文做什么铺垫。'
              '讲事实与因果，不要强行升华时代、社会或抽象意义。\n'
              '另给出 summary：整段视频的剧情梗概（4~6 句）。\n'
              '只输出 JSON，不要其他任何文字：\n'
              '{"summary":"...","beats":[{"i":1,"importance":"advance","role":"..."}]}\n\n'
              + '\n'.join(ctx))
    out = None
    try:
        out = _llm_light(prompt, system=sys_, timeout=240)
    except Exception:
        out = None
    data = _try_parse_json(out)
    if not data or not isinstance(data.get('beats'), list):
        if data and data.get('summary'):
            default['summary'] = str(data['summary']).strip()
        return default
    beats = []
    for i in range(n):
        b = data['beats'][i] if i < len(data['beats']) and isinstance(data['beats'][i], dict) else {}
        imp = str(b.get('importance', 'advance') or 'advance')
        if imp not in ('key', 'advance', 'transition', 'mood'):
            imp = 'advance'
        beats.append({'i': i + 1, 'importance': imp, 'role': str(b.get('role', '') or '').strip()})
    return {'summary': str(data.get('summary', '') or '').strip(), 'beats': beats}


def _split_nar_lines(text):
    """把模型输出按行切分，去掉空行 / 编号前缀 / 引号。"""
    import re as _re
    if not text:
        return []
    lines = []
    for raw in _re.split(r'[\r\n]+', text):
        l = raw.strip().strip('"').strip()
        if not l:
            continue
        l = _re.sub(r'^(?:第?\d+[\.、)．:：]|\[\d+\]|（\d+）)\s*', '', l)
        if l:
            lines.append(l)
    return lines


def _split_nar_sentences(line):
    """把一个长行按句切分（。！？!?），用于行数不足时补齐镜头。"""
    import re as _re
    parts = _re.split(r'(?<=[。！？!?])', line)
    return [p.strip() for p in parts if p.strip()]


def _split_nar_clauses(line):
    """二级切分：句内再按逗号/分号/顿号切小句（句子数仍不够镜头数时兜底）。"""
    import re as _re
    parts = _re.split(r'(?<=[，,；;、])', line)
    return [p.strip() for p in parts if p.strip()]


def _split_into_k(text, k):
    """把一段文字尽量均分成 k 份，优先在标点处断开（极端兜底：只有一句话却要覆盖多个镜头）。"""
    if k <= 1 or not text:
        return [text]
    n = len(text)
    cuts, prev = [], 0
    for i in range(1, k):
        want = prev + max(1, round((n - prev) / (k - i + 1)))
        pos = None
        for j in range(min(want + 6, n - 1), max(prev + 1, want - 6), -1):
            if text[j - 1] in '。！？!?，,；;、':
                pos = j
                break
        if pos is None:
            pos = min(n - 1, max(prev + 1, want))
        cuts.append(pos)
        prev = pos
    out, last = [], 0
    for c in cuts:
        out.append(text[last:c].strip())
        last = c
    out.append(text[last:].strip())
    return [x for x in out if x] or [text]


def _distribute_sents(sents, n):
    """把 m 个短句按镜头数 n 均匀分布拼接（每镜头至少一句，多句的合并）。"""
    m = len(sents)
    out = []
    for i in range(n):
        a = int(round(i * m / n))
        b = int(round((i + 1) * m / n))
        chunk = sents[a:b]
        out.append(''.join(chunk) if chunk else sents[min(i, m - 1)])
    return out


def _map_lines_to_segs(lines, n):
    """把整稿行映射回 n 个镜头：行数相等直接一一对应；不足时按句 → 小句 → 字数逐级拆细后均匀分布；过多则线性就近取行。

    ⚠️ 关键不变量：**绝不把同一句原文复制给所有镜头**。旧实现在「模型只输出 1 行且句数不足」
    时会走进兜底循环，让 n 段拿到完全相同的解说——表现为「整片解说只有一句话反复出现」。
    现改为逐级拆细：按句 → 按逗号小句 → 按字数均分，尽量让每段拿到不同内容。"""
    if n <= 0:
        return []
    m = len(lines)
    if m == 0:
        return ['' for _ in range(n)]
    if m == n:
        return list(lines)

    # ① 句子池
    sents = []
    for l in lines:
        sents.extend(_split_nar_sentences(l))

    # ② 句数不够 → 再按小句（逗号/分号/顿号）拆
    if len(sents) < n:
        clauses = []
        for s in sents:
            clauses.extend(_split_nar_clauses(s))
        if len(clauses) > len(sents):
            sents = clauses

    # ③ 仍不够 → 反复把最长的可断条目在中点附近的标点处劈开，直到凑够 n 条。
    #    只在标点处断开、绝不硬切字符——否则会把「便利店」劈成「便利」+「店想」这类半个词。
    guard = 0
    while len(sents) < n and guard < 256:
        guard += 1
        best = None
        for i, s in enumerate(sents):
            if len(s) < 12:
                continue
            half = len(s) // 2
            for j in range(half, len(s) - 3):
                if s[j] in '，,；;、。！？!?':
                    if best is None or len(s) > len(sents[best[0]]):
                        best = (i, j + 1)
                    break
        if best is None:
            break
        i, cut = best
        a, b = sents[i][:cut].strip(), sents[i][cut:].strip()
        if not a or not b:
            break
        sents[i:i + 1] = [a, b]

    if len(sents) >= n:
        return _distribute_sents(sents, n)

    # ④ 极端兜底：只剩一句。若这句够长就按字数均分（每段 >=8 字才切，避免切成「这是一/个男子在」
    #    这种读不通的碎片）；太短则原样重复——宁可重复，也不产出看不懂的半截话。
    if len(sents) == 1 and len(sents[0]) >= max(8 * n, 16):
        return _distribute_sents(_split_into_k(sents[0], n), n)

    out = []
    for i in range(n):
        out.append(sents[i % len(sents)])
    return out


# 台标 / 水印 / 推广语：VLM 读画面字幕时极易把这些当成「台词」抄回来。
# 它们会与真实 ASR 台词同质合并进 per_seg，污染剧情理解、分镜规划与声画对齐。
# 只拦高置信度伪字幕，宁可漏拦也不误删真实对话（硬字幕片仍要靠这一路拿台词）。
_ONSCREEN_BLACKLIST = (
    '点点关注', '图灵影视', '字幕组', '请关注', '关注我', '加关注', '点赞', '订阅',
    '更多精彩', '下集更精彩', '未完待续', '影视剪辑', '禁止转载', '版权所有',
    '高清资源', '微信公众号', 'QQ群', '本视频由', '素材来自', '仅供学习',
)


def _clean_onscreen_text(txt, extra=None):
    """清洗 VLM 从画面抄回的「字幕」：滤掉台标/水印/推广语与臆造残句。

    返回清洗后的文本；判定为垃圾则返回 ''。extra 可传 ai_config.json 里的
    onscreen_blacklist（用户自定义词表），不配则只用内置表。
    """
    t = str(txt or '').strip()
    if not t or t in ('(无字幕)', '（无字幕）', '无字幕', 'N/A', '无', 'none'):
        return ''
    if len(t) < 2:
        return ''
    # 纯符号 / 纯数字（含全角数字）不算对话
    core = ''.join(ch for ch in t if ch.isalnum())
    if not core or core.isdigit():
        return ''
    for w in tuple(_ONSCREEN_BLACKLIST) + tuple(extra or []):
        if w and w in t:
            return ''
    return t


def _read_onscreen_subtitles(frames, per_seg, max_seg=12):
    """让 VLM 读出画面里**烧录的字幕文字** → {段下标: 字幕文本}。失败返回 {}。

    【解决硬字幕】字幕烧进画面像素后无法提取（要 OCR），而这类片子常常**没有可识别的
    语音**（原声/音乐/外语），于是 ASR 空 → 剧情理解与写稿都失去台词依据。
    但 VLM 本来就在看这些帧，顺带读一下画面上的字几乎零额外成本。

    【为什么不引入 OCR】OCR 引擎（paddleocr/easyocr）要带几百 MB 模型与额外依赖，
    且对影视字幕这种艺术字识别率并不比多模态模型高；这里用已有的 VLM 更划算。
    """
    import re as _re
    n = len(per_seg)
    if not frames or n == 0:
        return {}
    idxs = [i for i in range(n) if frames.get(i)]
    if not idxs:
        return {}
    if len(idxs) > max_seg:
        step = (len(idxs) - 1) / float(max_seg - 1)
        idxs = sorted(set(idxs[int(i * step)] for i in range(max_seg)))
    img_list = [frames[i] for i in idxs]
    sys_ = '你是视频字幕识别助手，只抄录画面里出现的对话字幕文字。'
    prompt = ('下面是同一段视频按时间顺序抽取的画面（第1张对应第1段，依此类推）。\n'
              '只抄录画面**底部中央字幕带**的对话字幕（白色描边字、对话气泡）。\n'
              '四角台标、片名水印、滚动推广条、点赞关注、UI 按钮等文字一律忽略，'
              '这些不是台词，遇到就写「(无字幕)」。\n'
              '严格按顺序每段一行，格式严格为「第k段: 字幕文字」，k 从 1 开始递增。\n'
              '该段没有字幕就写「第k段: (无字幕)」；不要描述画面、不要推测台词、不要编造。')
    try:
        out = vlm_chat_multi(img_list, prompt, system=sys_, timeout=240) or ''
    except Exception:
        return {}
    try:
        _extra = load_ai_config().get('onscreen_blacklist') or []
    except Exception:
        _extra = []
    got = {}
    for l in out.splitlines():
        m = _re.match(r'^第(\d+)段[:：]\s*(.+)$', l.strip())
        if not m:
            continue
        k = int(m.group(1))
        if not (1 <= k <= len(idxs)):
            continue
        txt = _clean_onscreen_text(m.group(2).strip(), extra=_extra)
        if txt:
            got[idxs[k - 1]] = txt
    if got:
        _note_progress_warning('已从画面字幕读出 %d 段台词（已过滤台标/水印；'
                               '硬字幕无法直接提取，由视觉模型读出）' % len(got))
    return got


def _seg_visual_captions(frames, per_seg, params, max_seg=14, progress=None):
    """逐段画面描述：把各段的中间帧按时间顺序一次性交给 VLM，
    要求按「第k段: 画面内容」逐段输出——这是解说稿贴合画面的地基：
    写稿模型拿到每段「实际可见的内容」后，才不会把后面的事件提前讲到前面。
    返回 {段下标(0基): 画面描述}；VLM 不可用/失败返回 {}。"""
    import re as _re
    n = len(per_seg)
    # 优先选择有台词的片段跑VLM（无台词的纯画面场景用ASR匹配就行，不用VLM描述）
    # 有台词的片段才需要画面描述来辅助写稿，纯画面/空镜描述了也没用
    has_dialogue = [i for i, (_s0, _s1, txt) in enumerate(per_seg) if (txt or '').strip()]
    no_dialogue = [i for i in range(n) if i not in set(has_dialogue)]
    if len(has_dialogue) >= max_seg:
        # 有台词的片段足够，均匀采样覆盖全片
        pick_idx = sorted(set(has_dialogue[int(i * (len(has_dialogue) - 1) / (max_seg - 1))] for i in range(max_seg)))
    else:
        # 有台词的不够，全部选上，再用无台词的补齐（均匀采样）
        pick_idx = list(has_dialogue)
        remaining = max_seg - len(pick_idx)
        if remaining > 0 and no_dialogue:
            extra = sorted(set(no_dialogue[int(i * (len(no_dialogue) - 1) / max(1, remaining - 1))] for i in range(remaining)))
            pick_idx = sorted(set(pick_idx + extra))
    # 首尾必采（保证覆盖全片）
    if 0 not in pick_idx and frames.get(0):
        pick_idx.insert(0, 0)
    if n - 1 not in pick_idx and frames.get(n - 1):
        pick_idx.append(n - 1)
    pick_idx = sorted(set(pick_idx))
    imgs = [(i, frames[i]) for i in pick_idx if frames.get(i)]
    if not imgs:
        return {}
    img_list = [p for _, p in imgs]
    sys_ = '你是视频画面描述助手，只描述画面里实际可见的内容。'
    prompt = ('下面是同一段视频按时间顺序抽取的各镜头画面：第1张对应第1段，第2张对应第2段……依此类推。'
              '请严格按顺序逐段输出每段画面里实际可见的内容（人物/动作/场景/屏幕文字）。'
              '每段一行，格式严格为「第k段: 画面内容」，k 从 1 开始递增。'
              '只描述看到的，不要推测剧情、不要总结、不要遗漏任何一段。')
    # 进度提示：让用户知道正在做什么，避免看起来像卡死
    if progress:
        try:
            progress['phase'] = '画面理解 %d帧（VLM推理中，约30秒…）' % len(img_list)
        except Exception:
            pass
    try:
        out = vlm_chat_multi(img_list, prompt, system=sys_, timeout=240) or ''
    except Exception:
        return {}
    caps = {}
    for l in out.splitlines():
        l = l.strip()
        m = _re.match(r'^第(\d+)段[:：]\s*(.+)$', l)
        if m:
            k = int(m.group(1))
            if 1 <= k <= len(imgs):
                orig_i = imgs[k - 1][0]
                caps[orig_i] = m.group(2).strip()[:70]
    return caps


# ---------------------------------------------------------------------------
# 题材化模板：六套标准解说结构（悬疑/爱情/恐怖/动作/喜剧/历史）。
# 解说工业化流水线：拿到任何片子先定型，再按模板的钩子公式/结构节奏/升华方向填细节。
# 来源：用户提供的实战方法论（无损压缩注入 prompt）。
# ---------------------------------------------------------------------------
NARR_STYLES = {
    'movie':    {'label': '电影解说', 'system': '你是坐拥百万粉丝的资深影视解说博主——拼的是认知差与情绪共振，带观众用更高视角看故事，口气自然像唠嗑。'},
    'science':  {'label': '科普讲解', 'system': '你是深入浅出的科普讲解者——用通俗语言讲清原理和逻辑，数据准确，不夸张不煽情，让观众听懂并记住。'},
    'funny':    {'label': '搞笑吐槽', 'system': '你是幽默风趣的吐槽博主——语言轻松有梗，节奏明快，用调侃和反差制造笑点，但不低俗不冒犯。'},
    'suspense': {'label': '悬疑解读', 'system': '你是擅长制造悬念的解说者——节奏沉稳，层层铺垫，留钩子引导观众思考，语气克制有张力。'},
}
DETAIL_LEVELS = {'detailed': 1.30, 'balanced': 1.0, 'concise': 0.70}


GENRE_TEMPLATES = {
    'suspense': {'name': '悬疑/烧脑/反转',
        'focus': '讲诡计的设计逻辑与视角的欺骗性，不做凶手是谁的复读机',
        'hook': '结果前置 + 灵魂拷问。公式例：如果告诉你，主角在开场第 X 分钟就死了，你信吗？别急——你亲眼看见的真相全是假的。',
        'structure': '铺垫期快速讲清规则（循环/密室/狼人杀规则，不讲细枝末节）；搅局期抛第一个反常点（为什么只有主角记得昨天）；破局期强行剧透式反转（镜头回放第 3 遍你会发现，导演早就在背景的报纸上写了答案）',
        'ending': '升维到诡计背后的动机（为了爱？为了阶层跨越？）。金句方向：比悬疑更难的，是算尽人心；比算尽人心更难的，是承认自己也在局中。',
        'tone': '冷静克制，突出设计感'},
    'romance': {'name': '爱情/青春/文艺',
        'focus': '不说剧情，说情绪颗粒度——把情节翻译成观众共情的恋爱瞬间',
        'hook': '痛点代入 + 细节特写。公式例：你上一次删掉聊天记录是什么时候？这部片里的男女主，删了整整 7 年。他们删掉的不是争吵，而是……',
        'structure': '相遇期极简交代相识，重点写第一次心动的小动作（慢、轻）；拉扯期不讲大冲突，讲错过的时间差（他想复合时她刚死心）；结局期不强求圆满、放大遗憾（BE 就放慢，解说停一下补一句轻话）',
        'ending': '金句要像朋友圈文案。方向：爱情经得起风雨，却经不起平凡。',
        'tone': '慢、轻、克制'},
    'horror': {'name': '恐怖/惊悚/心理阴暗',
        'focus': '放弃高能预警，讲无处可逃的日常感',
        'hook': '环境沉浸 + 反常理行为。公式例：关掉灯，戴上耳机。接下来的 3 分钟，你会觉得你家的衣柜、床底都有东西在盯着你——这部片的恐怖在于：鬼从来不敲门。',
        'structure': '日常崩坏期极平淡地介绍主角的普通生活（越平淡后续越瘆人）；异常累积期用短促断句造压迫（他动了。她没醒。墙上的照片，眼睛闭上了。）；源头揭秘期讲心理创伤的外化（鬼是内疚的化身），不说鬼怪长相',
        'ending': '吓完要治愈或警告。方向：比鬼更可怕的，是走不出的心魔；比心魔更可怕的，是你明明醒着，却动不了。',
        'tone': '短促断句、压迫感'},
    'action': {'name': '动作/科幻/超英/灾难',
        'focus': '不讲打斗过程，讲世界观设定和代价',
        'hook': '脑洞设定 + 极致假设。公式例：如果给你一件战甲，但每穿一次就少活一天，你穿还是不穿？这部片告诉你什么叫能力越大，债务越多。',
        'structure': '设定期用最通俗的比喻讲清世界观；升级期跳过所有小喽啰打斗，只讲 BOSS 战的破局逻辑（为什么打不过？找到了什么漏洞？）；代价期强调牺牲（赢了，但失去了什么）',
        'ending': '热血燃向。方向：所谓英雄，不是不会害怕，而是双腿发抖，却依然挡在普通人前面。',
        'tone': '热血、节奏快'},
    'comedy': {'name': '喜剧/荒诞/黑色幽默',
        'focus': '把画面里的尴尬翻译成屏幕前的哈哈大笑，语调欢脱、带点阴阳怪气',
        'hook': '高能名场面截取。公式例：建议别看这部片，真的会笑到邻居来敲门——尤其男主在殡仪馆点外卖那段，把葬礼整成了相声现场。',
        'structure': '人设期用夸张标签给主角贴标签（抠门鬼/倒霉蛋）；连环期讲多米诺骨牌式连锁倒霉，语速加快用排比（刚躲过追债的，撞上前女友；刚甩掉前女友，亲爹把他存款捐了）；笑中带泪，结尾突然收住笑，点出小人物心酸',
        'ending': '方向：所谓喜剧，就是把人生的烂摊子，扎成一束捧花。',
        'tone': '欢脱、嘴碎'},
    'history': {'name': '历史/战争/史诗/传记',
        'focus': '放弃宏观大词，凝视大时代下的小人物——不谈战役意义，谈那场雪有多冷、那封信有多重',
        'hook': '个人命运与时代冲撞。公式例：历史书上那行冰冷的伤亡数字，在这部片里，变成了一个母亲等了 20 年的空碗。这部战争片没有英雄，只有一群想活命的普通人。',
        'structure': '背景期仅 1 句交代年代，极速切入主角日常（种地/织布/上课）；浪潮期讲抉择的被动性（不是他们选择了战争，是战争闯进了家），解说沉重；长镜头期找一个最经典的无言画面，此处全篇不给一句解说词，只放背景音让画面说话',
        'ending': '必须拔高立意、关联当下和平。方向：我们并非生在一个和平的年代，只是生在一个和平的国家。忘了历史，就是第二次背叛。',
        'tone': '沉重、凝视'},
}

def _detect_genre(plot):
    """根据剧情梗概判断题材类型（一次轻量文字调用）；失败返回空串。"""

    if not plot or not _w.local_llm_enabled():
        return ''
    names = '、'.join(v['name'] for v in GENRE_TEMPLATES.values())
    try:
        out = _llm_light('这段视频的剧情梗概：' + str(plot)[:600] + '。它最适合按哪类题材解说？只回答其中一个类型名（' + names + ' 之一），不要任何其他内容。',
                         system='只输出类型名。', timeout=60)
    except Exception:
        return ''
    out = (out or "").strip()
    for k, v in GENRE_TEMPLATES.items():
        if v['name'] in out or k in out:
            return k
    return ''

def _genre_template_block(key):
    """按题材 key 生成写稿 prompt 的模板规则块；未知/空返回空串。"""

    t = GENRE_TEMPLATES.get(key or "")
    if not t:
        return ""
    return chr(10).join([
        '【题材模板·' + t['name'] + '】本片按以下结构写（模板优先于默认写法）：',
        '- 侧重点：' + t['focus'] + '；',
        '- 开篇钩子公式：' + t['hook'] + '；',
        '- 结构节奏：' + t['structure'] + '；',
        '- 结尾升华方向：' + t['ending'] + '；',
        '- 语调：' + t['tone'] + '。',
    ])


def local_vlm_narrate(per_seg, frames, params, plot=None, beat_outline=None):
    """本地真解说（连贯真人版 · 整稿生成 + 少升华 + 前置要求 + 自优化）：
    ① _plot_brief 视觉理解；② _beat_plan 详略规划（重要镜头展开、过渡镜头带过）；
    ③ 一次生成「像真人一样从头讲到尾」的连贯解说整稿（一行对应一个镜头、自然衔接、
       重点讲剧情本身、非高光不升华），并让模型自查润色一遍（自优化）；
    ④ 按行切分回各镜头；整稿失败则逐段回退（少升华 + 承接上文）。返回 (lines, True)。"""
    templates = [
        '镜头缓缓推进，故事就此展开。', '画面一转，新的转折正在发生。',
        '气氛渐起，关键情节悄然铺开。', '人物登场，冲突拉开了序幕。',
        '悬念浮现，让人忍不住屏息。', '节奏陡然加快，高潮正在靠近。',
        '真相逼近，谜底即将揭晓。', '余波未平，故事仍在继续。',
    ]
    theme = (params.get('theme') or '').strip()
    name = (params.get('name') or '').strip()
    req = (params.get('req') or '').strip()
    if plot is None and frames:
        plot = _plot_brief(frames, per_seg, params)
    if beat_outline is not None:
        # 复用分段层已规划好的主线/过渡，避免重复调用模型
        beats = [{'i': i + 1, 'importance': o.get('importance', 'advance'), 'role': ''}
                 for i, o in enumerate(beat_outline)]
        summary = ''
    else:
        beat = _beat_plan(per_seg, plot, params)
        summary = beat.get('summary', '')
        beats = beat.get('beats', [])
    use_local_text = _local_model_available()

    def _write(p, s_, timeout=300):
        if _aborted():
            raise AbortError('用户取消了任务')
        if use_local_text:
            try:
                return local_llm_chat(p, system=s_, timeout=timeout)
            except Exception:
                pass
        try:
            return _w.vlm_text(p, system=s_, timeout=timeout)
        except Exception:
            return None

    n = len(per_seg)
    _style = NARR_STYLES.get(params.get('narr_style', 'movie'), NARR_STYLES['movie'])
    sys_ = _style['system']
    req_line = '- 你的额外要求：%s\n' % req if req else ''

    # —— 整稿生成：像真人一样把故事从头讲到尾 ——
    # 逐段画面描述（写稿地基）：让模型知道每一段画面里实际有什么，
    # 否则它只能按剧情梗概想象——事件会漂移，把后面发生的事提前讲到前面
    seg_vis = _seg_visual_captions(frames, per_seg, params, progress=params.get('_progress') if isinstance(params, dict) else None)
    seg_brief = []
    for i, (s0, s1, txt) in enumerate(per_seg):
        b = beats[i] if i < len(beats) else {}
        imp = b.get('importance', 'advance')
        tag = {'key': '（关键/高光，可展开）', 'transition': '（过渡）', 'mood': '（氛围）'}.get(imp, '')
        t = (txt or '').strip()[:80]
        # 带上「建议字数」：模型按画面时长决定写多长，配音才念得完也填得满（贴合时间轴）
        lo, hi = _target_chars((s1 - s0) * DETAIL_LEVELS.get(params.get('detail_level', 'balanced'), 1.0))
        vis = ('画面：' + seg_vis[i]) if i in seg_vis else ''
        seg_brief.append('第%d段 %s[建议%d~%d字] %s %s'
                         % (i + 1, tag, lo, hi, ('台词：' + t if t else '无台词画面'), vis))
    # 题材模板：用户显式选择优先；自动则按剧情梗概轻量判型（失败则不套模板）
    genre = (params.get('genre') or 'auto').strip()
    if genre not in GENRE_TEMPLATES or genre == 'auto':
        genre = _detect_genre(plot) or ''
    genre_block = _genre_template_block(genre)
    ctx = []
    if genre_block:
        ctx.append(genre_block)
    if name:
        ctx.append('视频：' + name)
    if theme:
        ctx.append('主题/梗概：' + theme)
    if summary:
        ctx.append('【整段剧情梗概】' + summary)
    if plot:
        ctx.append('【视觉剧情理解】' + plot)
    prompt = ('下面是这段视频的整体剧情理解与各镜头环节（括号内是该镜头的详略提示）。\n'
              + '\n'.join(ctx + seg_brief)
              + ('\n\n请写【%d 行】连贯的中文电影解说词，一行对应一个镜头，从上到下依次是第1、第2…段：\n' % n)
              + '- 【必须】恰好输出 %d 行，一行对应一个镜头：不要合并镜头、不要把多个镜头写成一行；\n' % n
              + '- 【黄金7秒·钩子开篇】第 1 行是生死线：用反常理悬念或人性拷问破题'
                '（示范："如果你回到1939年，敢不敢向整个德国撒谎？"），'
                '严禁"今天讲一部关于XX的电影"式平淡开场；\n'
              + '- 【认知提炼·拒绝流水账】解说≠复述：只讲推动人物命运转折的情节、揭示人性底色的细节，'
                '过场动作全部砍掉；每一行都要给观众一个"他们自己看不出来的信息差"或情绪点；\n'
              + '- 【画面为准，顺序不漂移】每段都标了该段「画面：」的实际内容——这一行的解说必须以这段画面为基础展开；'
              + '- 【万能衔接】想不出过渡时可用（每片最多 1 次）：然而，命运没打算放过他……/就在所有人以为结束时，真正的修罗场才刚刚开始……/但导演的镜头一转，揭示了最残酷的真相……；\n'
              + '整体梗概只用于衔接语气，绝不允许把后面段落的事件提前到前面段落讲；'
              + '- 【口语化讲述感】短句、多动词、少形容词，单句超过 20 字必须停顿；用「我们/你我」的唠嗑感；'
                '关键处可插入一句主观吐槽（例："说实话，换我早跪了"）拉近距离；\n'
              + '- 【留白】若某段是情感爆发点（痛哭/决裂/无言以对），该行只写「（留白）」三个字：解说闭嘴，让原片声音飞——文字不给满，情绪才溢得出来；\n'
              + '- 【详略有当】标“关键/高光”的多讲（可两句），过渡镜头一句带过，不要平均用力、不要每段一样长；\n'
              + '- 【字数严格对应时长】每段标注了“建议N~M字”，这是该镜头的配音容量：\n'
                '  写太长会念不完被截断，写太短画面会空着。请让每行字数落在该区间内（允许 ±10% 浮动）；\n'
              + '- 【中段克制·结尾升华】中间各行不总结、不升华，只推进剧情与情绪；'
                '最后一段承担全片金句收尾：把故事映射到现实共鸣（职场/婚姻/原生家庭/阶层），'
                '用 ≤2 句散文诗式总结点题——这是观众收藏转发的理由；\n'
              + '- 【红线】涉及暴力用"清理/离世"等温和词、侧重心理而非过程；主角若违法，'
                '最后一段必须点出"违法行为终将受到法律制裁"，不美化犯罪动机；\n'
              + '- 台词只转述大意，不原样照搬；不编造剧情里没有的事实；不堆"高潮/悬念/震撼"等空泛词。\n'
              + req_line
              + '直接输出 %d 行解说词，不要编号、不要解释。\n' % n
              + '- 【格式】必须用换行分隔成 %d 行，**不要写成一整段话**；每行只讲对应镜头的内容。' % n)
    out = _write(prompt, sys_)
    lines = _split_nar_lines(out)

    # —— 行数不足时重试一次：模型常把整稿写成一整段（无换行），
    #    旧逻辑会直接把这一句复制给所有镜头 → 整片解说只剩一句话。
    if len(lines) < n and n >= 2:
        retry = ('你刚才输出的内容没有按行分开（只解析出 %d 行），但需要恰好 %d 行，'
                 '一行对应一个镜头。\n请把下面的解说稿**原样拆成 %d 行**并输出：'
                 '保持原有文字与顺序，只做换行拆分，不要新增、不要删减、不要改写，'
                 '不要编号、不要解释。\n\n' % (len(lines), n, n)
                 + '\n'.join(lines))
        out_r = _write(retry, sys_, timeout=300)
        if out_r and out_r.strip():
            lines_r = _split_nar_lines(out_r)
            if len(lines_r) > len(lines):
                lines = lines_r

    # —— 自优化：让模型自查衔接/重复/详略并输出优化稿（整稿成形且用强文字模型时）——
    if use_local_text and len(lines) >= max(2, (n + 1) // 2):
        polish = ('下面是电影解说稿草稿（一行对应一个镜头）。\n' + '\n'.join(lines)
                  + '\n\n请以资深电影解说编辑的身份审阅并优化，输出【改进后的完整稿】，让整篇像真人电影解说一样流畅自然：'
                    '①镜头之间衔接更顺，有“接着讲下去”的连贯感；②删掉重复、套话和空泛的升华（「（留白）」标记与结尾金句收尾必须原样保留）；'
                    '③保持详略得当（重要镜头多讲、过渡镜头短）；④每行对应一个镜头、行数不变。'
                    '直接输出优化后的完整稿，不要编号、不要解释。')
        out2 = _write(polish, '你是专业电影解说编辑。', timeout=300)
        if out2 and out2.strip():
            lines2 = _split_nar_lines(out2)
            if len(lines2) >= max(2, (n + 1) // 2):
                lines = lines2

    # —— 行 → 镜头 映射 ——
    if lines:
        mapped = _map_lines_to_segs(lines, n)
    else:
        # 整稿失败：逐段回退（少升华 + 承接上一段结尾，保持连贯）
        mapped = []
        prev_tail = ''
        LEN = {
            'key': '这段是剧情关键/转折/高光，可展开讲透：2~3 句、70~110 字。',
            'advance': '这段推进剧情，正常讲：1~2 句、40~70 字，聚焦本段实际发生的事。',
            'transition': '这段是过渡/铺垫，一句带过即可：20~40 字。',
            'mood': '这段是氛围/情绪镜头，简略渲染即可：20~40 字。',
        }
        for i, (s0, s1, txt) in enumerate(per_seg):
            b = beats[i] if i < len(beats) else {}
            imp = b.get('importance', 'advance')
            role_desc = b.get('role', '')
            ctx2 = []
            if i == 0:
                if name:
                    ctx2.append('视频：' + name)
                if theme:
                    ctx2.append('主题/梗概：' + theme)
                if summary:
                    ctx2.append('【整段剧情梗概】' + summary)
                if plot:
                    ctx2.append('【视觉剧情理解】' + plot)
            if role_desc:
                ctx2.append('【本段剧情作用】' + role_desc)
            ctx2.append('本段(%s-%s秒) %s' % (int(s0), int(s1), ('台词：' + txt.strip()[:80] if txt.strip() else '无明显台词')))
            if prev_tail:
                ctx2.append('【上一段结尾，接着往下讲】' + prev_tail)
            p2 = ('你是电影解说博主，正在给观众连续地讲这段视频的剧情。请为本段写中文解说旁白，'
                  '【接着上一段自然往下讲】。\n'
                  + ('第 1 段是开场：请写一句有吸引力的开场旁白引入剧情（能确认片名则点出片名/年代/背景；'
                     '未确认时用“故事从……”/“镜头对准……”自然引入）。\n' if i == 0 else '')
                  + '要求：' + LEN.get(imp, LEN['advance'])
                  + '围绕本段实际发生的剧情向前推进，讲清人物做了什么、事态怎么变，像讲故事；'
                    '除非这是真正的转折/高光，否则不要总结“这反映了/象征着/揭示了”这类意义升华；'
                    '不要描述画面本身；开头不要用“然而/但”等接续词；台词转述、不要原样引用；'
                    '不要编造剧情里没有的事实。'
                  + ('额外要求：' + req + '。' if req else '')
                  + '直接输出旁白，不要编号/引号/解释。\n\n' + '\n'.join(ctx2))
            o = _write(p2, sys_)
            seg_text = (o or '').strip().strip('"').strip()[:160] \
                or (txt.strip()[:40] if txt.strip() else templates[i % len(templates)])
            mapped.append(seg_text)
            prev_tail = seg_text[-18:]

    for i, l in enumerate(mapped):
        if not l or not l.strip():
            mapped[i] = (per_seg[i][2] or '').strip()[:40] or templates[i % len(templates)]
    return mapped[:n], True


_local_model_cache = {'t': 0, 'ok': False}

def _local_model_available():
    """检测本地「文字模型」是否真实可用（enabled 且对应 model 已在服务端列表里）。
    缓存 60s，避免每次解说都探活。这是「写解说稿的主力」——部署了 qwen2.5:14b 等文字模型，
    解说质量才会上一个台阶（qwen2.5vl 只负责看图，不负责写稿）。"""
    cfg = local_llm_cfg()
    if not (cfg['enabled'] and cfg['base_url']):
        return False
    now = time.time()
    if now - _local_model_cache['t'] < 60:
        return _local_model_cache['ok']
    ok = False
    try:
        import urllib.request, json as _json
        url = cfg['base_url'].rstrip('/') + '/models'
        req = urllib.request.Request(url, method='GET')
        if cfg['api_key']:
            req.add_header('Authorization', 'Bearer ' + cfg['api_key'])
        with urllib.request.urlopen(req, timeout=6) as r:
            data = _json.loads(r.read().decode('utf-8'))
        models = [m.get('id', '') for m in (data.get('data') or [])]
        target = cfg['model'] or ''
        ok = bool(target) and any(target in m or m in target for m in models)
    except Exception:
        ok = False
    _local_model_cache.update(t=now, ok=ok)
    return ok


def _is_weak_vlm(model):
    """判断视觉模型是否偏弱（只适合“看懂画面”，不擅长写剧情解说）。
    qwen2.5vl:latest / 3b / 4b / 7b 视为弱；14b / 32b 及以上的大视觉模型视为可用。"""
    m = (model or '').lower()
    if 'vl' not in m:
        return False
    return any(x in m for x in ('3b', '4b', '7b', 'latest'))


def _installed_local_models():
    """探测 Ollama / OpenAI 兼容端点已安装的模型名列表（供前端展示与推荐判断）。"""
    out, seen = [], set()
    vb = (vlm_cfg().get('base_url') or '').rstrip('/')
    lb = (local_llm_cfg().get('base_url') or '').rstrip('/')
    urls = []
    if vb and 'v1' not in vb:
        urls.append(vb + '/api/tags')
    if lb and lb.endswith('/v1'):
        urls.append(lb + '/models')
    import urllib.request, json as _json
    for url in urls:
        try:
            req = urllib.request.Request(url, method='GET')
            with urllib.request.urlopen(req, timeout=5) as r:
                data = _json.loads(r.read().decode('utf-8'))
            items = [m.get('name', '') for m in (data.get('models') or [])] \
                or [m.get('id', '') for m in (data.get('data') or [])]
            for it in items:
                if it and it not in seen:
                    seen.add(it); out.append(it)
        except Exception:
            pass
    return out


def _model_narr_guide():
    """解说模型引导：检测当前 VLM 是否偏弱、本地是否已部署可用的文字模型（写解说稿主力），
    返回给前端做「qwen2.5vl 局限提示 + 部署标准解说模型引导」。"""
    vc = vlm_cfg()
    lc = local_llm_cfg()
    guide = {
        'weak_vlm': _is_weak_vlm(vc.get('model')),
        'vlm_model': vc.get('model') or '',
        'local_ok': _local_model_available(),
        'local_model': lc.get('model') or '',
        'installed': _installed_local_models(),
        'recommend': '',
        'recommend_note': '',
    }
    if guide['local_ok']:
        guide['recommend'] = ''
        guide['recommend_note'] = '本地文字模型 %s 已就绪，解说稿走它生成，质量最佳。' % guide['local_model']
    else:
        # 有本地视觉模型但缺文字模型 → 引导补装文字模型（写解说稿的主力）
        # 【2026-10-26】按本机显存给推荐：原先固定推 qwen2.5:14b(9.3GB)，在 12GB 卡上
        # 实测与视觉模型抢显存 → 单次调用 47~215 秒、甚至 llama-server 崩溃 → 整机卡死。
        _tag, _note = _w.recommend_text_model(_w.gpu_vram_gb(), with_vlm=bool(guide['vlm_model']))
        guide['recommend'] = 'ollama pull %s' % _tag
        guide['recommend_note'] = ('当前只配置了视觉模型（%s）：它只负责“看懂画面”，写剧情解说词很弱。'
                                   '请部署一个文字模型（推荐 %s：%s），'
                                   '并在「AI 配置 → ③ 本地模型」里把模型填成它。'
                                   % (guide['vlm_model'], _tag, _note))
    return guide


def _segment_timeline(video_path, max_seg=25.0, seg_cap=60):
    """解说分段：场景切分优先，无切点则按 max_seg 均分。返回 [(start,end)]。
    max_seg 服务端钳制到 4~600 秒：API/指令路径可绕过前端 min=8 的限制，
    极端值（如 0.5s）会切出成百上千个碎段，直接拖垮解说稿生成与配音。
    seg_cap 是段数硬顶：长视频（如 98 分钟）按场景切点会切出 200+ 段，
    缩略图/草稿/配音逐段串行，阶段①直接不可用。超出后贪心合并最短相邻段，
    把段数压到硬顶以内（见 _enforce_seg_cap）。"""
    max_seg = min(600.0, max(4.0, float(max_seg or 25.0)))
    sc = _cached_scene_cuts(video_path, threshold=0.25)   # 与卡点共享场景切点缓存
    vdur = probe_audio_len(video_path) or 0.0
    if vdur <= 0:
        return []
    if sc:
        tl = [0.0]
        for c in sc:
            if c - tl[-1] >= 4.0 and vdur - c >= 3.0:
                tl.append(c)
        tl.append(vdur)
    else:
        n = max(1, int(vdur / max_seg))
        tl = [i * vdur / n for i in range(n + 1)]
    raw_segs = []
    for i in range(len(tl) - 1):
        if tl[i + 1] - tl[i] >= 1.5:
            raw_segs.append((tl[i], tl[i + 1]))
    # 场景合并：相邻短场景（<3秒）合并到下一段，减少碎段数量
    # 1小时视频可从300+段降到150-200段，VLM调用减半
    MERGE_THRESHOLD = 3.0
    segs = []
    i = 0
    while i < len(raw_segs):
        s0, s1 = raw_segs[i]
        # 如果当前段短于阈值，且后面还有段，合并
        while (s1 - s0) < MERGE_THRESHOLD and i + 1 < len(raw_segs):
            i += 1
            s1 = raw_segs[i][1]
        segs.append((s0, s1))
        i += 1
    # 长视频段数硬顶：超出后贪心合并最短相邻段，保证阶段①可用。
    if seg_cap and len(segs) > seg_cap:
        segs = _enforce_seg_cap(segs, seg_cap)
    # 有效但极短的视频（如 0.3s 片段）：上面按「≥1.5s」过滤会切出 0 段 →
    # 阶段①会报「镜头切分失败或视频时长为 0」，但时长明明不是 0（文案误导）。
    # 这里给一段覆盖全片：文件是好的就该有结果，不该整段报错。
    if not segs and vdur > 0:
        segs = [(0.0, vdur)]
    return segs


def _enforce_seg_cap(segs, cap):
    """段数硬顶执行：反复合并「最短段」与其较短的相邻段，直到 len<=cap。

    只改变相邻边界，不丢时间轴总覆盖（首段起点与末段终点保持不变）。
    段数上限 cap 通常远小于原始段数，贪心合并复杂度 O(n*cap) 足够快。"""
    segs = [tuple(s) for s in segs]
    while len(segs) > cap and len(segs) > 1:
        mi = min(range(len(segs)), key=lambda k: segs[k][1] - segs[k][0])
        left_d = (segs[mi][1] - segs[mi][0]) if mi > 0 else float('inf')
        right_d = (segs[mi + 1][1] - segs[mi + 1][0]) if mi + 1 < len(segs) else float('inf')
        if mi > 0 and left_d <= right_d:
            segs[mi - 1] = (segs[mi - 1][0], segs[mi][1])
            del segs[mi]
        else:
            segs[mi] = (segs[mi][0], segs[mi + 1][1])
            del segs[mi + 1]
    return segs


def _local_narrate(per_seg, params, plot=None):
    """省流 + 本地模型：用本地 qwen/ollama 等离线生成/改写每段解说词（0 元、不调云端）。
    返回 (lines, True)。行数不足时按上文口吻续写（见 _fill_missing_lines），不回填模板。"""
    theme = (params.get('theme') or '').strip()
    name = (params.get('name') or '').strip()
    brief = '\n'.join(
        f'[{s0:.1f}-{s1:.1f}s] {"台词:" + txt if txt else "(无台词画面)"}'
        for s0, s1, txt in per_seg)
    ctx = []
    if name:
        ctx.append('视频文件名：' + name)
    if theme:
        ctx.append('视频主题/梗概：' + theme)
    # 旧提示词只说「讲发生了什么」，模型于是自由发挥成抒情散文
    # （实测产出「像举着半透明的糖纸」「油菜花的呼吸声」这类文艺腔），
    # 这里给出明确结构 + 禁用词表，把风格压回「讲故事的口播稿」。
    prompt = ('你是一个电影解说文案助手。下面是一段视频的分段时间轴与台词。\n'
              + ('补充信息：\n' + '\n'.join(ctx) + '\n' if ctx else '')
              + '请生成一段面向观众的连贯中文解说稿：\n'
                '- 第 1 段开场：点出主题（不确定片名就用“故事从……”自然引入），抛出一个抓人的悬念。\n'
                '- 中间每段：承接上文，讲清楚「谁 + 做了什么 + 为什么」，'
                '用口语推进节奏（没想到／就在这时／结果／也正是这时）。\n'
                '- 最后一段：收束或升华一句。\n'
                '- 每段 40~90 字，口播风格，句子要能直接念出来。\n'
                '- 严禁文艺腔与抒情排比；严禁出现这些词：画面里、镜头中、我们看到、仿佛、像极了、藏着。\n'
                '- 严格按顺序每段一行输出，不要编号、不要引号、不要括号、不要解释。\n\n' + brief)
    _style = NARR_STYLES.get(params.get('narr_style', 'movie'), NARR_STYLES['movie'])
    _detail = DETAIL_LEVELS.get(params.get('detail_level', 'balanced'), 1.0)
    _lo, _hi = int(40 * _detail), int(90 * _detail)
    prompt = prompt.replace('每段 40~90 字', '每段 %d~%d 字' % (_lo, _hi))
    text = local_llm_chat(prompt, system=_style['system'])
    lines = [l.strip().strip('"').strip() for l in text.splitlines() if l.strip()]
    if len(lines) < len(per_seg):
        # 行数不足：按上文口吻续写缺失镜头（不再回填模板，避免风格断裂/内容错配）
        filled = _fill_missing_lines(lines, per_seg[len(lines):], params, plot=plot)
        lines = lines + filled
    if len(lines) < len(per_seg):
        # 终极兜底：把已有行按镜头数分布（不复制、不模板）
        lines = _map_lines_to_segs(lines, len(per_seg))
    return lines[:len(per_seg)], True


def _fill_missing_lines(existing, remaining, params, plot=None):
    """解说行数不足时，按上文口吻补写剩余镜头的解说（避免回填模板导致风格断裂/内容错配）。
    返回补写的行列表（长度 ≤ len(remaining)）；模型不可用返回 []。"""
    if not remaining:
        return []
    name = (params.get('name') or '').strip()
    theme = (params.get('theme') or '').strip()
    req = (params.get('req') or '').strip()
    ctx = []
    if name:
        ctx.append('视频：' + name)
    if theme:
        ctx.append('主题/梗概：' + theme)
    if plot:
        ctx.append('【剧情理解】' + plot)
    prompt = ('你正在为一段视频写中文电影解说稿，已经写好了前面的段落（请保持口吻一致）：\n'
              + ('\n'.join('·' + l for l in existing) if existing else '（前面还没有内容）') + '\n\n'
              '请继续为下面 %d 个镜头环节各写一句解说，承接上文、自然衔接、讲剧情不描述画面：\n' % len(remaining)
              + '\n'.join('%d. %s-%ss %s' % (i + 1, int(s0), int(s1),
                                            ('台词：' + t[:80] if t else '无台词画面'))
                          for i, (s0, s1, t) in enumerate(remaining))
              + '\n严格按镜头顺序输出 %d 行，不要编号、不要解释。' % len(remaining))
    sys_ = '你是资深电影解说博主，口气自然、像真人聊天讲故事一样。'
    if req:
        prompt += '\n【额外要求】' + req
    try:
        out = _llm_text(prompt, system=sys_, timeout=180)
    except Exception:
        return []
    if not out:
        return []
    lines = _split_nar_lines(out)
    if not lines:
        return []
    return lines[:len(remaining)] if len(lines) >= len(remaining) else lines


def generate_narration(segs, asr, params, frames=None, plot=None, beat_outline=None):
    """返回 (narr_list, used_local)。
    离线(省流)优先级：本地 VLM(看图+台词+梗概→真解说) > 本地文本模型(台词改写) > 真实台词/模板。
    智能(云端)：有视觉端点则附画面描述，否则纯台词交给 DeepSeek 写解说。"""
    frames = frames or {}
    templates = [
        '镜头缓缓推进，故事就此展开。',
        '画面一转，新的转折正在发生。',
        '气氛渐起，关键情节悄然铺开。',
        '人物登场，冲突拉开了序幕。',
        '悬念浮现，让人忍不住屏息。',
        '节奏陡然加快，高潮正在靠近。',
        '真相逼近，谜底即将揭晓。',
        '余波未平，故事仍在继续。',
    ]
    # 每段聚合台词：按台词中点归到唯一镜头段（bisect 二分）。
    # 旧条件「整句须落在段内」会把骑跨段边界的台词两段都分不到而丢失，模型拿到的剧情信息变少。
    import bisect as _bisect
    seg_starts = [s0 for (s0, _s1) in segs]
    seg_txt = ['' for _ in segs]
    for x in asr:
        mid = (x['start'] + x['end']) / 2.0
        j = _bisect.bisect_right(seg_starts, mid) - 1
        j = max(0, min(len(segs) - 1, j))
        seg_txt[j] += (' ' if seg_txt[j] else '') + x['text']
    per_seg = [(s0, s1, t.strip()) for (s0, s1), t in zip(segs, seg_txt)]
    # 自动选路（不再区分省流/智能）：① 本地模型（免费）优先 ② 配置了云端 key 才用云端 ③ 台词/模板兜底
    if _w.vlm_enabled():
        # ① 本地 VLM 真解说（看画面，从「复读」变「真解说」）
        try:
            return local_vlm_narrate(per_seg, frames, params, plot=plot, beat_outline=beat_outline)
        except Exception:
            pass
    if _w.local_llm_enabled():
        # ② 本地文本模型改写（无画面理解）
        try:
            return _local_narrate(per_seg, params, plot=plot)
        except Exception:
            pass
    if not _w.ai_enabled('chat'):
        # ③ 真实台词 / 模板兜底（未部署任何模型时保底出片）
        out = []
        for i, (s0, s1, txt) in enumerate(per_seg):
            out.append(txt[:40] if txt else templates[i % len(templates)])
        return out, False
    # ④ 云端 LLM 生成「连贯剧情解说稿」（在 AI 配置里填了 key = 明确同意使用；先整体理解剧情，再分段输出）
    try:
        import urllib.request, json as _json
        cfg = _w.chat_cfg()
        use_vision = _w.ai_enabled('vision')
        plot_ctx = ''
        if frames and use_vision:
            try:
                idxs = sorted(frames.keys())
                step = max(1, (len(idxs) + 4) // 5)
                descs = []
                for i in idxs[::step][:5]:
                    d = _w.ai_describe_image(frames[i], '')
                    if d:
                        descs.append(d)
                if descs:
                    plot_ctx = '整体画面线索：' + ('；'.join(descs))
            except Exception:
                plot_ctx = ''
        lines_brief = []
        for i, (s0, s1, txt) in enumerate(per_seg):
            lines_brief.append(f'[{s0:.1f}-{s1:.1f}s] {"台词:" + txt if txt else "(无台词画面)"}')
        brief = '\n'.join(lines_brief)
        req_txt = (params.get('req') or '').strip()
        instr = ('下面是一段视频的分段时间轴、台词与画面线索。请生成一段面向观众的连贯中文电影解说稿'
                 '（剧情解说，不是画面描述）：\n'
                 '- 像真人解说一样把故事从头讲到尾、一气呵成：镜头之间自然衔接、层层递进'
                 '（可用“此时/紧接着/可没想到/而另一边/偏偏这时候”等承接），不要每段都另起炉灶；\n'
                 '- 第 1 段：开场即钩子（黄金 7 秒）：用反常理悬念或人性拷问破题，'
                 '严禁“今天讲一部关于XX的电影”式平淡开场；若画面能确认影视作品，点出片名/年代/背景（不确定不要编造片名）；\n'
                 '- 后续每段：承接上文，叙述本段剧情本身（人物做了什么/事态怎么变），像讲故事；'
                 '除非这段真是剧情转折/高光，否则不要总结“这反映了/象征着/揭示了”这类意义升华；\n'
                 '- 详略有当：关键/转折/高光段展开讲（2~3 句），过渡/铺垫段一句带过，不要平均用力；\n'
                 '- 口语化讲述感：短句、多动词少形容词，单句别超过 20 字不停顿，可用「我们/你我」的唠嗑感；\n'
                 '- 【中段克制·结尾升华】中间各行不总结不升华，只推进剧情与情绪；最后一段金句收尾：'
                 '把故事映射到现实共鸣（职场/婚姻/原生家庭/阶层），≤2 句散文诗式总结；\n'
                 '- 【红线】涉暴力用温和词、侧重心理而非过程；主角若违法，须点出“违法行为终将受到法律制裁”；\n'
                 '- 每段 20~120 字，口播风格、有推进感；台词转述、不要原样引用对话；'
                 '不编造剧情外事实；不堆“高潮/悬念/震撼”等空泛词；\n'
                 '- 严格按顺序每段一行输出，不要编号、不要引号、不要解释。\n\n')
        genre = (params.get('genre') or '').strip()
        if genre and genre != 'auto':
            g_block = _genre_template_block(genre)
            if g_block:
                instr = instr + g_block + '\n\n'
        if req_txt:
            instr += '【额外要求】' + req_txt + '\n\n'
        payload = {
            'model': cfg.get('model'),
            'messages': [{'role': 'user', 'content': instr + (plot_ctx + '\n\n' if plot_ctx else '') + brief}],
            'max_tokens': 1800,
            'temperature': 0.5,
        }
        url = (cfg.get('base_url', '').rstrip('/')) + '/chat/completions'
        req = urllib.request.Request(url, data=_json.dumps(payload).encode('utf-8'),
                                     headers={'Content-Type': 'application/json',
                                              'Authorization': 'Bearer ' + cfg.get('api_key', '')})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = _json.loads(resp.read().decode('utf-8'))
        lines = [l.strip().strip('"').strip() for l in data['choices'][0]['message']['content'].splitlines() if l.strip()]
        if len(lines) < len(per_seg):
            # 行数不足：按上文口吻续写缺失镜头（不再回填模板，避免风格断裂/内容错配）
            filled = _fill_missing_lines(lines, per_seg[len(lines):], params, plot=plot)
            lines = lines + filled
        if len(lines) < len(per_seg):
            # 终极兜底：把已有行按镜头数分布（不复制、不模板）
            lines = _map_lines_to_segs(lines, len(per_seg))
        return lines[:len(per_seg)], False
    except Exception:
        return [templates[i % len(templates)] for i in range(len(per_seg))], False


def _merge_segs(segs, max_keep=None):
    """合并过碎/过多镜头段到「剧情环节」。
    影视片段常被场景切分切成很多 4~8 秒的短镜头，逐段解说必然重复；
    按总时长均分成少量环节后，每环节一句解说、连贯推进，配合字幕/配音更接近真人电影解说。
    max_keep 缺省按时长自适应（约每 10 秒一个环节，4~14 个）：固定 ≤6 会让长视频
    一行解说扛 20~40 秒画面——字幕密度不够，且内容晚于段首出现，观感像"时间轴错位"。"""
    if not segs:
        return segs
    n = len(segs)
    if max_keep is None:
        max_keep = max(4, min(14, int(segs[-1][1] / 10)))
    if n <= max_keep:
        return segs
    vdur = segs[-1][1]
    target = vdur / max_keep
    merged = []
    cur_s, cur_e, acc = None, None, 0.0
    for s, e in segs:
        if cur_s is None:
            cur_s, cur_e, acc = s, e, (e - s)
        elif (acc + (e - s) <= target * 1.5) or (len(merged) + 1 >= max_keep):
            cur_e = e
            acc += (e - s)
        else:
            merged.append((cur_s, cur_e))
            cur_s, cur_e, acc = s, e, (e - s)
    if cur_s is not None:
        merged.append((cur_s, cur_e))
    return merged


# ---------------------------------------------------------------------------
# 内容感知 · 主线剪辑与解说密度调控（解决「密度异常 / 时间轴错位 / 不剪主线」）
# ---------------------------------------------------------------------------
_IMP_ORDER = {'transition': 0, 'mood': 1, 'advance': 2, 'key': 3}

def _max_imp(a, b):
    return a if _IMP_ORDER.get(a, 1) >= _IMP_ORDER.get(b, 1) else b

def _cap_seg_duration(segs, cap):
    """把超过 cap 秒的镜头段在内部均分切开，避免单条解说词扛过长画面导致时间轴错位。"""
    out = []
    for (s, e) in segs:
        if e - s <= cap + 1e-6:
            out.append((s, e)); continue
        n = max(2, int(-(-(e - s) // cap)) or 2)  # 向上取整，保证每片 <= cap
        for j in range(n):
            out.append((s + (e - s) * j / n, s + (e - s) * (j + 1) / n))
    return out

def _split_unit_at_gaps(fine_segs, members, cap):
    """把总时长超 cap 的成员组合，按内部成员边界切成 ≤cap 的子段（保留场景切点，不硬切）。"""
    out = []
    cur_s = fine_segs[members[0]][0]; cur_e = cur_s; cur_dur = 0.0
    for m in members:
        s, e = fine_segs[m]
        if cur_dur + (e - s) > cap and cur_dur > 0:
            out.append((cur_s, cur_e)); cur_s = s; cur_dur = 0.0
        cur_e = e; cur_dur += (e - s)
    if cur_dur > 0:
        out.append((cur_s, cur_e))
    return out or [(fine_segs[members[0]][0], fine_segs[members[-1]][1])]

def _condense_segs(fine_segs, asr, params, plot=None, beat_plan=None, frames=None):
    """把细粒度场景段浓缩为「解说环节」：

    - 有模型时：依 _beat_plan 的重要性，把连续的 transition/mood（且无台词）并入相邻主线段，
      实现「密度正常 + 聚焦主线」——过渡段不再被平等解说；纯填充微段(<2.5s 无台词、非主线)标记 keep=False 供剪辑主干时剪除。
    - 所有段按重要性施加时长上限（key/advance ≤14s，过渡 ≤20s），保证解说词贴合当前画面（时间轴匹配）。
    - 无模型时退化为 _merge_segs + 时长上限（纯离线也不至于单条解说扛 30s）。

    返回 (condensed_segs, outline)；outline 为 [{start,end,importance,keep}]，长度 == len(condensed)。"""
    if not fine_segs:
        return [], []
    n = len(fine_segs)
    has_dlg = [bool(_asr_text_in(asr, s, e).strip()) for (s, e) in fine_segs]
    if beat_plan is None and (_local_model_available() or _w.vlm_enabled()):
        per_seg = [(s0, s1, _asr_text_in(asr, s0, s1)) for (s0, s1) in fine_segs]
        # 台词来源映射：asr=语音识别 / onscreen=画面字幕 / none=无。
        # 与 per_seg 等长平行维护，**不改 3 元组结构**（下游 20+ 处直接解包）。
        _dlg_src = ['asr' if (t or '').strip() else 'none' for _s0, _s1, t in per_seg]
        # 硬字幕兜底：ASR 台词覆盖不足一半时（常见于字幕烧进画面、语音不可识别），
        # 让 VLM 从画面里读字幕补进台词轨——否则剧情理解与写稿都缺台词依据（=乱剪）。
        if frames:
            _with_dlg = sum(1 for t in per_seg if (t[2] or '').strip())
            if _with_dlg * 2 < len(per_seg):
                try:
                    _subs = _read_onscreen_subtitles(frames, per_seg)
                except Exception:
                    _subs = {}
                if _subs:
                    per_seg = [(s0, s1, (t or '').strip() or _subs.get(i, ''))
                               for i, (s0, s1, t) in enumerate(per_seg)]
                    # 只有原本无台词、被画面字幕补位的段才标 onscreen；ASR 永远优先
                    for i in range(len(per_seg)):
                        if _dlg_src[i] == 'none' and (per_seg[i][2] or '').strip():
                            _dlg_src[i] = 'onscreen'
        # 来源只用于提示词可信度标注，不污染业务参数语义（本函数后半段只用这两个调用）
        params = dict(params or {})
        params['_dlg_src'] = _dlg_src
        if plot is None and frames:
            plot = _plot_brief(frames, per_seg, params)
        beat_plan = _beat_plan(per_seg, plot, params)
    if not beat_plan or not isinstance(beat_plan.get('beats'), list):
        # 离线兜底：时间均分 + 时长上限
        merged = _merge_segs(fine_segs)
        capped = _cap_seg_duration(merged, 16.0)
        return capped, [{'start': s, 'end': e, 'importance': 'advance', 'keep': True} for (s, e) in capped]
    imp = []
    for i in range(n):
        b = beat_plan['beats'][i] if i < len(beat_plan['beats']) and isinstance(beat_plan['beats'][i], dict) else {}
        v = str(b.get('importance', 'advance') or 'advance')
        imp.append(v if v in _IMP_ORDER else 'advance')
    # 分组：长过渡(无台词)并入相邻主线段（密度正常、聚焦主线）；
    # 纯填充微段(<2.5s 无台词、非主线)单独成段并标记 keep=False（剪辑主线时从成片剪除）。
    groups = []          # (members, unit_imp, keep)
    cur, cur_imp = None, None
    for i in range(n):
        dur = fine_segs[i][1] - fine_segs[i][0]
        is_filler = imp[i] in ('transition', 'mood')
        is_micro = is_filler and not has_dlg[i] and dur < 2.5
        if is_micro:
            if cur is not None:
                groups.append((list(cur), cur_imp, True))
            groups.append(([i], imp[i], False))   # 可剪：渲染时不进成片
            cur, cur_imp = None, None
            continue
        merge = cur is not None and is_filler and not has_dlg[i]
        if merge:
            cur.append(i); cur_imp = _max_imp(cur_imp, imp[i])
        else:
            if cur is not None:
                groups.append((list(cur), cur_imp, True))
            cur, cur_imp = [i], imp[i]
    if cur is not None:
        groups.append((list(cur), cur_imp, True))
    condensed, outline = [], []
    for members, uimp, keep in groups:
        s0 = fine_segs[members[0]][0]; s1 = fine_segs[members[-1]][1]
        cap = 14.0 if uimp in ('key', 'advance') else 20.0
        if s1 - s0 > cap:
            sub = _split_unit_at_gaps(fine_segs, members, cap)
        else:
            sub = [(s0, s1)]
        for (a, b) in sub:
            condensed.append((a, b))
            outline.append({'start': a, 'end': b, 'importance': uimp, 'keep': keep})
    return condensed, outline


def _narrate_candidate_shots(video_path, params):
    """解说「候选镜头」＝未合并的细粒度场景段（供用户改完解说词后重新匹配分镜用）。
    场景切点命中缓存，重复调用几乎零成本。若切点太少（长镜头视频），再按 maxSeg 细分，
    保证重匹配时有足够的可组合粒度。"""
    segs = _segment_timeline(video_path, max_seg=float((params or {}).get('maxSeg', 25)))
    if not segs:
        return []
    return segs


def _llm_call(fn, prompt, system, timeout, max_tokens):
    """调用 llm 函数，且**只在显式给了 max_tokens 时才传该参数**。

    兼容旧签名：这些 llm 入口会被测试/扩展用 lambda 替换（形如
    `lambda prompt, system='', timeout=180: ...`），若无条件多传一个
    max_tokens 关键字 → TypeError → 静默回退主模型，旧行为被破坏。
    """
    if max_tokens is None:
        return fn(prompt, system=system, timeout=timeout)
    return fn(prompt, system=system, timeout=timeout, max_tokens=max_tokens)


def _llm_text(prompt, system='', timeout=180, max_tokens=None):
    """本地文字模型优先（写稿主力），不可用/失败时回退视觉模型的文字通道；都不可用返回 None。

    max_tokens：批量任务（一次出多段草稿）需要更大输出预算，可显式指定。"""
    if _local_model_available():
        try:
            r = _llm_call(local_llm_chat, prompt, system, timeout, max_tokens)
            if r and r.strip():
                return r
        except Exception:
            pass
    try:
        return _llm_call(_w.vlm_text, prompt, system, timeout, max_tokens)
    except Exception:
        return None


def _llm_light(prompt, system='', timeout=180, max_tokens=None):
    # S4: 辅助任务优先走轻量模型（local.light_model）；未配置/失败回退主模型 _llm_text。
    # NARRATE_LIGHT_LLM=0 可整体关闭（辅助任务直接走主模型）。
    if os.environ.get('NARRATE_LIGHT_LLM') != '0':
        try:
            r = _llm_call(local_llm_light_chat, prompt, system, timeout, max_tokens)
            if r and r.strip():
                return r
        except Exception:
            pass
    return _llm_call(_llm_text, prompt, system, timeout, max_tokens)


def _asr_text_in(asr, s, e):
    """取时间窗 [s,e) 内的台词（按台词中点归窗，与解说稿聚合口径一致）。"""
    parts = []
    for x in (asr or []):
        try:
            mid = (float(x.get('start', 0)) + float(x.get('end', 0))) / 2.0
        except Exception:
            continue
        if s <= mid < e:
            t = (x.get('text') or '').strip()
            if t:
                parts.append(t)
    return ' '.join(parts)[:120]


def _algo_align_shots(shots, lines):
    """算法兜底：按解说词字数权重把镜头顺序分配给各句（不调模型，离线可用）。
    返回 bounds：第 i 句解说对应的最后一个镜头编号（1-based）。"""
    m, k = len(shots), len(lines)
    if m <= 0 or k <= 0:
        return []
    w = [max(1, len(str(l))) for l in lines]
    tot = float(sum(w))
    bounds, acc = [], 0
    for i in range(k):
        acc += w[i]
        b = int(round(acc * m / tot))
        # 每句至少 1 个镜头，且给后面的句子留够（避免最后几句分不到镜头）
        b = max(i + 1, min(m - (k - 1 - i), b))
        bounds.append(b)
    return bounds


def _algo_align_beats_to_scenes(beats, scenes, asr=None, scene_story=None):
    """算法语义对齐（不调模型）：用 VLM 场景描述/台词/剧情理解与解说词的字符 bigram
    重叠度，在「保持时间顺序」的前提下为每句解说挑最相关的连续场景，并跳过无关填充段。

    返回 {beat_idx: [scene_idx, ...]}，格式与 LLM 对齐结果一致，可直接喂给
    _allocate_script_spans。这是 LLM 对齐失败/超时时的保底通道——保证声画不会退化成
    「按字数平均切时间线」的盲切（解说词是改写过的剧情，与原始台词重合度低，
    只按台词密度分配必然「讲 A 放 B」）。
    """
    n, m = len(beats or []), len(scenes or [])
    if n <= 0 or m <= 0:
        return {}
    if n > m:
        # 句子比场景多：按比例共享场景（保证每句都有画面，且顺序不倒退）
        return {i: [min(m - 1, int(i * m / float(n)))] for i in range(n)}

    def _bg(s):
        s = str(s or '')
        return set(s[i:i + 2] for i in range(len(s) - 1) if s[i:i + 2].strip())

    scene_bg = []
    for j, sc in enumerate(scenes):
        parts = [_scene_describe(sc)]
        st = (scene_story or {}).get(j) or {}
        if st.get('story'):
            parts.append(str(st['story']))
        if st.get('keywords'):
            parts.append(''.join(str(k) for k in st['keywords']))
        if asr:
            try:
                parts.append(_asr_text_in(asr, float(sc.get('start', 0) or 0),
                                          float(sc.get('end', 0) or 0)))
            except Exception:
                pass
        scene_bg.append(_bg(' '.join(p for p in parts if p)))
    beat_bg = [_bg(b) for b in beats]

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
        q, s = beat_bg[i], scene_bg[j]
        if not q or not s:
            return 0.0
        shared = q & s
        if not shared:
            return 0.0
        got = sum(_w(g) for g in shared)
        # 分母用「解说词自己的信息量」：长句不会因为场景描述长而占便宜
        den = math.sqrt(sum(_w(g) for g in q) or 1.0)
        return got / den

    # —— 全局单调对齐（动态规划）——
    # 逐句贪心地「在剩下的范围里取最大分」是错的：第 7 句一旦被 384 秒的某个巧合
    # 吸走，后面每句都只能往后排，整条时间轴就歪了。真机实测 29 句稿子里第 12 句
    # 落到 1044 秒（画面是医院走廊），而稿子讲的是巡逻车里啃汉堡——全片错位。
    # 改成「整条序列联合最优」：在「时间不倒流」的约束下最大化总相关度。
    # 再加一个很弱的等比例先验，只在没有实义词可依据时打破平局，避免乱跳。
    # 【权重只能小】真机实测 PRIOR_W=0.3 时，中段和结尾被先验拖后 200~350 秒
    # （稿子的详略节奏和片子的时间节奏并不一致），降到 0.1 后平均误差 122s→33s。
    starts = [float(sc.get('start', 0) or 0) for sc in scenes]
    vdur = max([float(sc.get('end', 0) or 0) for sc in scenes] or [0.0])

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


def _sanitize_alignment(merged, base, scenes, n_beats, max_span=3, factor=3.0):
    """把对齐结果规范成「能直接下锅」的形态。三道处理，缺一不可：

    1) 每句最多跨 max_span 个场景：模型常回 [21..39] 这种一整段（252~468 秒），
       分配器会在这个 200 秒的大窗口里撒点，等于没对齐。
    2) 顺序纠正用保序回归，不用单向棘轮（见 _isotonic 的实测说明）。
    3) 单句向前跳的距离不能离谱：解说词是按剧情顺序写的，第 i 句该落在
       「上一句结束 + 剩余时长/剩余句数」附近。模型偶尔被同款动作镜头吸走
       （真机实测把「她扑过来」对到二十多分钟后），这种直接退回算法结果。
    """
    if not merged or not scenes:
        return merged
    starts = [float(s.get('start', 0) or 0) for s in scenes]
    ends = [float(s.get('end', 0) or 0) for s in scenes]
    vdur = max(ends) if ends else 0.0

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
        # ③ 跳太远：退回算法结果（DP 全局对齐，本身单调且带等比例先验）
        exp = max(1.0, (vdur - prev_end) / float(max(1, n_beats - i)))
        if starts[g[0]] > prev_end + factor * exp and base and base.get(i):
            g2 = sorted(set(int(x) for x in base[i] if 0 <= int(x) < len(scenes)))
            if g2:
                g = g2[:max_span]
                n_far += 1
        out[i] = g
        prev_first = g[0]
        prev_end = ends[g[-1]]
    if n_trunc or n_back or n_far:
        _log.info('[DIAG] 对齐规范化: 截断%d 保序纠正%d 跳远回退%d'
                  % (n_trunc, n_back, n_far))
    return out


def _model_align_shots(shots, lines, asr=None, params=None):
    """让模型按解说词语义把镜头分配给各句（asr 仅作语义依据，可缺省）。
    返回 bounds(list[int])；失败/结果不合法返回 None，由调用方回退到 _algo_align_shots。"""
    m, k = len(shots), len(lines)
    if m <= 0 or k <= 0:
        return None
    shot_lines = []
    for i, (s, e) in enumerate(shots, 1):
        t = _asr_text_in(asr, s, e)
        shot_lines.append('%d [%.1f-%.1fs] %s' % (i, s, e, ('台词：' + t) if t else '（无台词画面）'))
    prompt = ('下面是视频的镜头清单（编号 / 时间 / 该镜头里的台词），以及用户改写后的解说词。\n'
              '请把每个镜头分配给【内容最贴合】的那句解说。\n\n'
              '【镜头清单】\n' + '\n'.join(shot_lines)
              + '\n\n【解说词】共 %d 句\n' % k
              + '\n'.join('%d. %s' % (i + 1, l) for i, l in enumerate(lines))
              + '\n\n要求：\n'
              '- 必须按镜头顺序分配：不能颠倒、不能跳号、不能遗漏，每个镜头只能属于一句解说；\n'
              '- 一句解说可以对应一个或多个【连续】镜头；\n'
              '- 输出一个 JSON 数组，共 %d 个整数，第 i 个数 = 第 i 句解说对应的最后一个镜头编号；\n' % k
              + '- 数组最后一个数必须等于 %d（总镜头数），保证全部镜头都被覆盖。\n' % m
              + '只输出这个 JSON 数组，不要解释、不要其他文字。')
    out = _llm_text(prompt, '你是影视剪辑师，擅长把解说词与画面对位。', timeout=180)
    if not out:
        return None
    import re as _re, json as _json
    m2 = _re.search(r'\[[^\]]*\]', out, _re.S)
    if not m2:
        return None
    try:
        arr = _json.loads(m2.group(0))
    except Exception:
        return None
    if not isinstance(arr, list) or len(arr) != k:
        return None
    bounds = []
    for v in arr:
        try:
            bounds.append(int(round(float(v))))
        except Exception:
            return None
    # 合法化：单调递增、每句至少 1 个镜头、末值 = 总镜头数
    for i in range(k):
        lo, hi = i + 1, m - (k - 1 - i)
        if bounds[i] < lo:
            bounds[i] = lo
        elif bounds[i] > hi:
            bounds[i] = hi
    for i in range(1, k):
        if bounds[i] < bounds[i - 1]:
            return None      # 出现倒序 → 判定模型输出不可用，交给算法兜底
    if bounds[-1] != m:
        bounds[-1] = m
    return bounds


def _expand_shots(shots, k):
    """候选镜头数少于解说句数时，把每个镜头按时间均分成若干子段，
    保证每句解说至少能分到一个画面单元（子段仍在原镜头内，不跨镜头）。
    否则「每句至少 1 个镜头」的约束无解，会导致索引越界。"""
    m = len(shots)
    if m <= 0 or k <= 0 or m >= k:
        return shots
    per = -(-k // m)          # 整数向上取整，免 import math
    out = []
    for (s, e) in shots:
        span = (e - s) / float(per)
        for j in range(per):
            out.append((s + span * j, s + span * (j + 1)))
    return out


def _align_shots_to_lines(shots, lines, asr=None, params=None, use_model=True):
    """把候选镜头按（用户改写后的）解说词重新分配，产出新的分镜段。
    模型语义匹配优先；模型不可用/输出不合法时回退按字数权重的算法分配。
    返回 (segs, source)；segs 为 [(start, end)]，长度 = len(lines)。"""
    if not shots or not lines:
        return [], 'none'
    # 句数多于镜头数时先细分镜头，保证分配有解（否则每句至少 1 镜头不可满足 → 越界崩溃）
    shots = _expand_shots(shots, len(lines))
    bounds = None
    if use_model:
        bounds = _model_align_shots(shots, lines, asr, params)
    src = 'model'
    if not bounds:
        bounds = _algo_align_shots(shots, lines)
        src = 'algo'
    if not bounds:
        return [], 'none'
    segs, prev = [], 0
    for b in bounds:
        if prev >= len(shots):      # 防御：镜头已分完，剩余句子复用最后一段
            segs.append(segs[-1] if segs else (float(shots[-1][0]), float(shots[-1][1])))
            continue
        b = max(prev + 1, min(len(shots), b))
        segs.append((float(shots[prev][0]), float(shots[b - 1][1])))
        prev = b
    return segs, src


def _narrate_analysis(video_path, params, run_dir, progress=None):
    """解说分析公共层：分段→ASR台词→(可选)关键帧→内容感知主线浓缩→解说稿。
    「人机协同分析(/api/plan)」与「直接生成解说(narrate_video)」共用此流程，
    避免两份逐行重复的实现各自漂移。
    返回 (segs, narr, asr, frames, mode, outline)；outline 为 [{start,end,importance,keep}]，
    标记每个解说环节是主线(key/advance)还是过渡/氛围(transition/mood)，供「剪辑主线」使用。"""
    def up(ph, pct):
        if progress:
            progress['phase'] = ph; progress['pct'] = pct
    # 同上：分段 / Whisper / 抽帧 / 生成解说稿都是无 ffmpeg 的长阶段，靠协作式取消响应「⏹ 停止」
    up('场景分段', 4)
    if _aborted():
        raise AbortError('用户取消了任务')
    fine = _segment_timeline(video_path, max_seg=float(params.get('maxSeg', 25)))
    if not fine:
        raise RuntimeError('无法分析视频时长')
    up('识别台词(本地Whisper)', 10)
    if _aborted():
        raise AbortError('用户取消了任务')
    # 传入 progress：十几分钟的长视频转写要跑很久，没有逐段进度就是「看着像卡死」
    asr = asr_segments(video_path, progress=progress, pct_range=(10, 16))
    need_frames = _w.vlm_enabled() or _w.ai_enabled('vision')   # 任一视觉能力可用就抽帧（自动选路）
    frames = {}
    if need_frames:
        up('抽取关键帧(供视觉理解)', 16)
        frames = _w.extract_segment_frames(video_path, fine, os.path.join(run_dir, 'frames'))
    # 内容感知主线浓缩：复用视觉理解 + 节拍规划，按重要性合并过渡段、剪纯填充微段、按时长上限防时间轴错位
    up('规划主线与解说密度', 18)
    per_seg = [(s0, s1, _asr_text_in(asr, s0, s1)) for (s0, s1) in fine]
    plot = _plot_brief(frames, per_seg, params) if frames else None
    beat_plan = _beat_plan(per_seg, plot, params) if (_local_model_available() or _w.vlm_enabled()) else None
    segs, outline = _condense_segs(fine, asr, params, plot=plot, beat_plan=beat_plan, frames=frames)
    if not segs:
        segs = fine
        outline = [{'start': s, 'end': e, 'importance': 'advance', 'keep': True} for (s, e) in fine]
    # 剪辑主线：剪除纯填充微段（keep=False），让成片聚焦主线、密度正常。
    # 【透明化】被剪掉的段不能悄悄消失：先收集成 _excluded，稍后随 cut_plan 一并落盘，
    # 供前端「剪辑方案」展示（用户可看到剪掉了哪些画面、为什么）。
    _excluded = [(s, o) for s, o in zip(segs, outline) if not o.get('keep', True)]
    kept = [(s, o) for s, o in zip(segs, outline) if o.get('keep', True)]
    if kept:
        segs = [s for s, _ in kept]
        outline = [o for _, o in kept]
    if _aborted():
        raise AbortError('用户取消了任务')
    up('生成解说稿', 22)
    # 注入progress供VLM画面描述阶段显示子进度
    if isinstance(params, dict):
        params['_progress'] = progress
    narr, used_local = generate_narration(segs, asr, params, frames=frames, plot=plot, beat_outline=outline)
    mode = None
    if _w.vlm_enabled() and frames:
        mode = 'vlm'
    elif used_local:
        mode = 'local'
    # ---- 剪辑方案 cut_plan 落盘（透明化的核心中间产物）----
    # 包含全部保留段（带解说词）与被剪段（keep=False + 原因），按时间排序。
    # 前端据此展示「哪些画面保留/剪掉、解说词与画面怎么对应」；渲染阶段也据此
    # 决定是否真剪（有被剪段 → 拼接保留段，绝不静默跳过剪辑）。
    try:
        _src_dur = probe_audio_len(video_path) or 0.0
        _imp_name = {'key': '关键转折', 'advance': '主线推进',
                     'transition': '过渡衔接', 'mood': '氛围渲染'}
        _cp = []
        for _i, (_s0, _s1) in enumerate(segs):
            _o = outline[_i] if _i < len(outline) else {}
            _cap = narr[_i] if _i < len(narr) else ''
            _cp.append({'i': len(_cp), 'start': round(float(_s0), 2), 'end': round(float(_s1), 2),
                        'keep': True,
                        'importance': str(_o.get('importance', 'advance')),
                        'reason': _imp_name.get(str(_o.get('importance', 'advance')), '主线'),
                        'caption': (_cap or '').strip(),
                        'asr': _asr_text_in(asr, float(_s0), float(_s1))[:60]})
        for (_s0, _s1), _o in _excluded:
            _cp.append({'i': len(_cp), 'start': round(float(_s0), 2), 'end': round(float(_s1), 2),
                        'keep': False,
                        'importance': str(_o.get('importance', 'transition')),
                        'reason': '过渡/氛围填充段（无台词微段，自动剪除）',
                        'caption': '', 'asr': ''})
        _cp.sort(key=lambda x: x['start'])
        _cov = (sum(b - a for a, b in segs) / _src_dur) if _src_dur else 0.0
        if progress:
            progress['cut_plan'] = {
                'src_dur': round(_src_dur, 2),
                'coverage': round(_cov * 100, 1),
                'kept': sum(1 for x in _cp if x['keep']),
                'removed': sum(1 for x in _cp if not x['keep']),
                'removed_sec': round(sum((x['end'] - x['start']) for x in _cp if not x['keep']), 1),
                'segments': _cp,
            }
    except Exception as _e:
        _log.info(f'[DIAG] cut_plan 构建失败(不影响主流程): {_e}')
    return segs, narr, asr, frames, mode, outline


def _analyze_narrate(video_path, params, run_dir, progress=None):
    """解说分析阶段：分段→ASR台词→解说稿→(可选)关键帧。返回 (segs, narr, asr, diag, mode, outline)。
    拆出供「人机协同」复用：用户可在预览界面编辑每段解说词/删除段后再渲染。"""
    segs, narr, asr, frames, mode, outline = _narrate_analysis(video_path, params, run_dir, progress)
    diag = {'segments': len(segs), 'asr_lines': len(asr), 'narration': narr}
    return segs, narr, asr, diag, mode, outline



def narrate_video(video_path, params, run_dir, progress=None, music_path=None):
    """电影解说主流程：分段→ASR→解说稿→SAPI/MiMo配音→混音→字幕→成片。
    music_path: 可选背景音乐，混入成品（按 Phase 2「配乐」要求）。"""
    segs, narr, asr, frames, mode, _outline = _narrate_analysis(video_path, params, run_dir, progress)
    if progress and mode:
        progress['mode'] = mode
    auto_cut = params.get('autoCut', True)
    final, vc, cut_info = _render_narrate(video_path, segs, narr, params, run_dir, progress=progress,
                                          music_path=music_path,
                                          mode=progress.get('mode') if progress else None,
                                          auto_cut=auto_cut)
    diag = {'segments': len(segs), 'asr_lines': len(asr), 'voice_clips': vc,
            'narration': narr, 'cut': cut_info}
    return final, diag


# ---------------------------------------------------------------------------
# Phase 3 · 联网搜索 + 全自动剧情解说
# ---------------------------------------------------------------------------
def web_search(query, max_results=6):
    """免费联网搜索（多源容灾：百度/必应优先，DuckDuckGo 兜底，无需 API Key）。
    返回 [(title, snippet, url)]，失败返回 []。任一源成功即返回，避免单一源被墙导致整功能挂掉。"""
    import urllib.request, urllib.parse, re as _re
    q = urllib.parse.quote(query)
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
               'Accept-Language': 'zh-CN,zh;q=0.9'}
    _strip = lambda h: _re.sub(r'<[^>]+>', '', h or '').strip()

    def _get(url):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode('utf-8', 'ignore')
        except Exception:
            return ''

    def _parse_baidu(html):
        out = []
        # 新版百度：每个结果是一个 c-container 容器；标题在容器内 h3>a，摘要在 c-abstract / content-right
        blocks = [b for b in _re.split(r'<div[^>]*class="[^"]*result[^"]*c-container', html)][1:]
        for blk in blocks:
            mh = _re.search(r'<h3[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', blk, _re.S)
            if not mh:
                continue
            url, title = mh.group(1), _strip(mh.group(2))
            if not title:
                continue
            ma = (_re.search(r'class="c-abstract"[^>]*>(.*?)</(?:div|span)>', blk, _re.S)
                  or _re.search(r'class="content-right[^"]*"[^>]*>(.*?)</span>', blk, _re.S))
            snip = _strip(ma.group(1)) if ma else ''
            if not snip:
                # 摘要有时以 c-span-len / 富摘要形式出现，兜底取容器内 p 文本
                mp = _re.search(r'<p[^>]*>(.*?)</p>', blk, _re.S)
                snip = _strip(mp.group(1)) if mp else ''
            out.append((title, snip, url))
        return out

    def _parse_bing(html):
        out = []
        blocks = _re.findall(r'<li class="b_algo"[^>]*>(.*?)</li>', html, _re.S)
        for b in blocks:
            m = _re.search(r'<h2><a[^>]*href="([^"]+)"[^>]*>(.*?)</a></h2>', b, _re.S)
            if not m:
                continue
            url, title = m.group(1), _strip(m.group(2))
            p = _re.search(r'<p[^>]*>(.*?)</p>', b, _re.S)
            snip = _strip(p.group(1)) if p else ''
            if title:
                out.append((title, snip, url))
        return out

    def _parse_ddg(html):
        out = []
        titles = _re.findall(r'class="result-link"[^>]*>(.*?)</a>', html, _re.S)
        if not titles:
            titles = _re.findall(r'class="result__a"[^>]*>(.*?)</a>', html, _re.S)
        for t in titles:
            out.append((_strip(t), '', ''))
        snippets = _re.findall(r'class="result-snippet"[^>]*>(.*?)</td>', html, _re.S)
        if not snippets:
            snippets = _re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, _re.S)
        for i in range(len(out)):
            if i < len(snippets):
                out[i] = (out[i][0], _strip(snippets[i]), out[i][2])
        return out

    # 大陆优先百度/必应；境外 DuckDuckGo 兜底。任一源成功即用，保证可用性。
    sources = [
        ('baidu', f'https://www.baidu.com/s?wd={q}&rn=10', _parse_baidu),
        ('bing', f'https://www.bing.com/search?q={q}&setlang=zh-CN', _parse_bing),
        ('ddg', f'https://lite.duckduckgo.com/lite/?q={q}', _parse_ddg),
        ('ddg', f'https://html.duckduckgo.com/html/?q={q}', _parse_ddg),
    ]
    try:
        for name, url, parser in sources:
            html = _get(url)
            if not html:
                continue
            res = parser(html)
            if res:
                seen, uniq = set(), []
                for t, s, u in res:
                    if t and t not in seen:
                        seen.add(t); uniq.append((t, s, u))
                if uniq:
                    return uniq[:max_results]
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# 影视剧情解说稿（「解说驱动剪辑」的核心）
#
# 旧实现 llm_movie_script 的三个硬伤，正是「怎么调都达不到普遍解说效果」的根因：
#   1. 每句被硬截断到 30/40 字（_split_sentences 的 l[:30]、_parse_events 的 desc[:40]）
#      → 解说碎片化，讲不成故事，甚至出现「随后追」这种半截话。
#   2. 每个事件各自独立成句、互不衔接 → 没有开场钩子、没有因果推进、没有结尾升华。
#   3. 事件条数由模型随意给 6-12 条，与画面段数无关 → 剧情只能往画面上「贴」，
#      段少了剧情被丢弃、段多了剧情被复制铺满，等于没剪。
#
# llm_movie_full_script 改为一次产出完整的、可直接口播的解说稿；画面反过来去迁就它。
# ---------------------------------------------------------------------------
NAR_BEAT_MIN = 40       # 每节解说词最少字数：少于此讲不成完整的一句
NAR_BEAT_MAX = 90       # 每节最多字数：超过则口播发赶、字幕一行放不下
NAR_SCRIPT_CPS = 5.2    # 影视解说话速（字/秒）：比普通解说略快，更贴近 B 站观感
NAR_SEG_TARGET = 5.0    # 每个视频子片段目标时长（秒）：一节解说词拆成多个短片段，提升剪辑密度

SCRIPT_STYLE_MOVIE = (
    '你是资深影视解说博主（B站/抖音影视解说风格）。请根据片名与剧情资料，'
    '写一篇可以直接照着念的完整解说稿。\n\n'
    '【结构】三段，缺一不可：\n'
    '1. hook 开场：1-2 句，点出片名，抛出全片最抓人的悬念或反差，但不要剧透结局。\n'
    '2. beats 主体：按剧情时间顺序分成若干节，每节 40-90 字，讲清楚人物、动机、'
    '冲突与转折，让没看过的人也能听懂。\n'
    '3. outro 结尾：1-2 句，点题或升华，给出回味。\n\n'
    '【每节写法】\n'
    '- 讲「谁 + 做了什么 + 为什么」，不要描述画面（禁止出现：画面里、镜头中、我们看到）。\n'
    '- 人物第一次出现要带上姓名与身份，例如「副警长瑞克」「搭档肖恩」。\n'
    '- 用口语衔接词推进节奏：没想到、就在这时、结果、也正是这时、可他不知道的是。\n'
    '- 每节末尾留一点钩子，让观众想继续看。\n\n'
    # 【坑】这里绝对不能出现「...」占位符：旧版写成 {情绪:欢快}...{/情绪}，
    # 模型会把省略号当正文原样抄成 {情绪:紧张}…{/情绪}，配音时会念出省略号。
    # 同理不能出现任何真实片名/人名/数字（旧版是《赌圣》4132万港币，被抄进 hook）。
    '【配音标记】可选，每节最多 1-2 处，只在关键处用；必须是成对或单点的标签：\n'
    '- 情绪：把要念的话包在 {情绪:XX} 与 {/情绪} 之间，XX 取 欢快/悲伤/严肃/紧张/温柔。\n'
    '- 停顿：在句中写 {停顿:0.5}（0.3-1.0 秒），它是单个标记，没有闭合标签。\n'
    '- 语速：包在 {慢} 与 {/慢} 之间放慢，包在 {快} 与 {/快} 之间加快。\n'
    '- 标签内部必须是真正要念出来的台词，不得只放标点或占位符；拿不准就整节不加标记。\n\n'
    '【禁止】编号、引号、括号注释、markdown、代码块、任何解释性文字。\n\n'
    '只输出如下 JSON：\n'
    '{"title":"片名","hook":"开场白","beats":[{"text":"第1节解说词",'
    '"keywords":["关键词1","关键词2"],"importance":"key|advance|transition"}],"outro":"结尾"}\n\n'
    'importance：key＝主线关键情节点，advance＝一般推进，transition＝过渡铺垫。\n'
    '大致比例 key 三成、advance 五成、transition 两成，不要全标 key。\n'
    '【最重要】素材没写到的事一律不许写：角色的死亡、生死结局、婚姻最终结果、身世秘密、'
    '追悼会或任何「后来怎样」的结论。素材停在哪儿，稿子就停在哪儿，'
    '宁可用「情况不明」「没人知道接下来会怎样」收尾，也不要替观众编一个结局。\n'
)


def _script_from_obj(obj, movie_name=''):
    """把 LLM 返回的 JSON 对象规范化成解说稿 dict；结构不对返回 None。"""
    if not isinstance(obj, dict):
        return None
    beats = []
    for it in (obj.get('beats') or []):
        if isinstance(it, dict) and it.get('text'):
            kw = it.get('keywords') or []
            if isinstance(kw, str):
                kw = [kw]
            imp = str(it.get('importance') or 'advance').lower()
            if imp not in ('key', 'advance', 'transition'):
                imp = 'advance'
            beats.append({'text': str(it['text']).strip(),
                          'keywords': [str(k) for k in kw][:6],
                          'importance': imp})
        elif isinstance(it, str) and it.strip():
            beats.append({'text': it.strip(), 'keywords': [], 'importance': 'advance'})
    if not beats:
        return None
    return {'title': str(obj.get('title') or movie_name or '').strip(),
            'hook': str(obj.get('hook') or '').strip(),
            'beats': beats,
            'outro': str(obj.get('outro') or '').strip()}


# ============================================================================
# 稿件「去镜头化」后处理
# ----------------------------------------------------------------------------
# 实测（RTX 3060 12GB + qwen3:8b，同一部 22 分钟片）写出来的稿子通篇是：
#   8. 警察仔细检查事故现场，每一处细节都不放过，眼神专注，仿佛遗漏一个细节…
#  10. 男子在停车场手持红色油桶，步伐坚定，神情似乎藏着某种目的，令人不禁猜测…
#  25. 警车在乡村公路上疾驰，远处山丘隐约可见，无线电里传来「Highway 18 eastbound,
#      GTA ADW 217-243. Advise extreme caution.」
# 三种毛病：① 复述画面而不是讲事；② 用「仿佛/似乎/令人」补空，信息量为零；
# ③ 把 ASR 原声（尤其英文与无线电代码）直接抄进中文旁白。
# 提示词只能减弱、不能消除，所以这里再做一层确定性清洗——不依赖模型。
# ============================================================================
_SPEC_CLUES = ('仿佛', '似乎', '好像', '像是', '好似', '如同',
               '令人', '让人', '不禁', '或许', '也许', '无疑是')

# 英文单词串：两个及以上连续词元（含 GTA ADW 217-243 这类呼号），允许空格/逗号分隔
_ASCII_RUN = re.compile(r"[A-Za-z][A-Za-z'\-]*"
                        r"(?:[\s,]+[A-Za-z0-9][A-Za-z0-9'\-]*)+")
# 夹在中文里的单个英文词（Shane？/Win 这种抄台词残留）
_ASCII_SOLO = re.compile(r"(?<=[\u4e00-\u9fff，。！？；：、])\s*[A-Za-z][A-Za-z'\-]{2,}"
                         r"(?=[\u4e00-\u9fff，。！？；：、{])")
# 包住英文的引号/书名号整体
_QUOTED = re.compile(r"['\"“”‘’「」『』]([^'\"“”‘’「」『』]{1,80})['\"“”‘’「」『』]")

_QUOTE_PAIRS = {'‘': '’', '“': '”', '「': '」', '『': '』', '（': '）', '(': ')'}


def _split_clauses(text):
    """按标点切分句，但不动 {} 配音标记与引号内部的标点。

    【为什么要在意引号】「无线电里传来‘高度戒备，Proceed with extreme caution.’」
    若在引号内的逗号处切开，后半截就变成一段孤立的英文，既删不干净也留残句。
    返回 [(分句, 尾随标点), ...]。
    """
    out, buf, depth, quote = [], [], 0, ''
    for ch in str(text or ''):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth = max(0, depth - 1)
        if quote:
            if ch == _QUOTE_PAIRS.get(quote):
                quote = ''
            buf.append(ch)
            continue
        if ch in _QUOTE_PAIRS:
            quote = ch
            buf.append(ch)
            continue
        if depth == 0 and ch in '，,。；;！!？?':
            out.append((''.join(buf), ch))
            buf = []
            continue
        buf.append(ch)
    if buf:
        out.append((''.join(buf), ''))
    return out


def _tidy_tail(body):
    """收尾标点整理：删句后留下的「，」「，{停顿:0.3}」统一补成句号。"""
    if not body:
        return body
    body = re.sub(r'([，,；;、])((?:\{[^}]*\})*)\s*$', r'。\2', body)
    # 删词后可能留下「？？」「，。」，折叠掉
    body = re.sub(r'([，。！？；：])\1+', r'\1', body)
    body = re.sub(r'[，、；]\s*(?=[。！？])', '', body)
    return body.strip()



def _drop_speculation(text):
    """删掉纯猜测的分句（「仿佛…」「似乎…」「令人不禁…」），只留能确定的事实。

    只在一句话还有 ≥2 个分句时才删，避免删空；带 {} 标记的分句只有在
    「去掉标记后本身就是在猜」时才删（否则会破坏 {情绪:xx}…{/情绪} 的配对）。
    收尾标点会从逗号补成句号。
    """
    if not text or not any(w in text for w in _SPEC_CLUES):
        return text
    parts = _split_clauses(text)
    if len(parts) < 3:
        return text
    kept = []
    for cl, sp in parts:
        core = re.sub(r'\{[^}]*\}', '', cl).strip()
        if core and any(w in core for w in _SPEC_CLUES):
            # 整个分句就是在猜（「仿佛每一句话都藏着秘密」「似乎已经逃离」）→ 删。
            # 但若这句的主语就在这个分句里（「他像是被什么触动」），删掉会变成
            # 「突然，猛地冲向走廊尽头」——主语没了。这种保留原句。
            if core.startswith(('他', '她', '它')):
                kept.append((cl, sp))
                continue
            if len(core) <= 16 or core.startswith(tuple(_SPEC_CLUES)):
                continue
        if not core and kept:
            # 纯标记分句（删掉上半句后剩下的 {/情绪}）跟着一起丢
            if cl.strip().startswith('{/'):
                continue
        kept.append((cl, sp))
    if len(kept) < 2 or len(kept) == len(parts):
        return text
    body = ''.join(cl + sp for cl, sp in kept).strip()
    return _tidy_tail(body)


# 只剩半句的尾巴：「无线电里传来」「牌子上写着」——英文台词被删后就成这种残句
_DANGLING_TAIL = re.compile(r'(传来|写着|喊道|叫着|念着|说道|听到|听见|播报|重复着|'
                            r'说了一句|报出)\s*[：:，,、]?\s*[。！？]?$')

# 报告动词后面没内容：「受伤的同伴虚弱地提到，」——念出来就是断句
_BROKEN_TAIL = re.compile(r'(提到|说道|表示|指出|声称|写着|传来|喊道|问道|回答|'
                          r'说出|讲到|补充|解释)$')


def _drop_broken_clause(text):
    """删掉语意不完整的残句（报告动词后面没有宾语）。"""
    if not text:
        return text
    parts = _split_clauses(text)
    if len(parts) < 2:
        return text
    kept = [(cl, sp) for cl, sp in parts
            if not _BROKEN_TAIL.search(re.sub(r'\{[^}]*\}', '', cl).strip())]
    if not kept or len(kept) == len(parts):
        return text
    return _tidy_tail(''.join(c + s for c, s in kept))



def _strip_foreign_clause(cl):
    """去掉分句里的英文/外语原声，返回清洗后的分句。"""
    s = _QUOTED.sub(lambda m: '' if len(re.findall(r'[A-Za-z]', m.group(1))) >= 3 else m.group(0), cl)
    s = _ASCII_RUN.sub('', s)
    s = _ASCII_SOLO.sub('', s)
    s = re.sub(r'\s{2,}', ' ', s)
    s = re.sub(r'([，,。；;])\s*(?=[，,。；;])', '', s)
    s = re.sub(r'([！？!?])\1+', r'\1', s)
    s = re.sub(r'^[，,。；;、\s]+', '', s).strip()
    return s


def _strip_foreign(text):
    """去掉抄进来的原声台词（英文原文、无线电代码），只保留中文叙述。

    分句粒度处理：某个分句的英文被删掉后只剩半句（「无线电里传来」「牌子上写着」），
    整句丢弃——留着比删掉更破坏听感。
    【只在中文旁白里做】判断依据是汉字比字母多：英文片/英文稿原样返回，
    否则会把整段英文解说清空。
    """
    if not text or not re.search(r'[A-Za-z]', text):
        return text
    _cjk = len(re.findall(r'[\u4e00-\u9fff]', text))
    if _cjk < 2:
        return text                      # 这段本来就是英文/代码，别动它
    parts = _split_clauses(text)
    kept = []
    for cl, sp in parts:
        cl2 = _strip_foreign_clause(cl)
        if cl2 != cl:
            core = re.sub(r'\{[^}]*\}', '', cl2).strip()
            # 整句是外语 → 丢；删完只剩残句（「无线电里传来」）→ 丢
            if len(core) < 8 or _DANGLING_TAIL.search(core):
                continue
        kept.append((cl2, sp))
    if not kept:
        return ''
    body = ''.join(c + s for c, s in kept).strip()
    # 兜底：仍夹在中文里的零散英文词（Shane）也去掉——中文旁白里不该出现
    body = re.sub(r"(?<![\u4e00-\u9fff])[A-Za-z][A-Za-z'\-]{1,}(?![\u4e00-\u9fff])", '', body)
    return _tidy_tail(body)




def _beat_sim(a, b):
    """两句的字符 bigram Jaccard 相似度，用来判断「同一件事是不是写了两遍」。"""
    try:
        ga, gb = _char_bigrams(a), _char_bigrams(b)
    except Exception:
        return 0.0
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / float(len(ga | gb))


def _beat_contain(a, b):
    """bigram 包含度 = 交集 / 较短那句的 bigram 数。

    【为什么 Jaccard 不够】同一件事换个说法，Jaccard 会掉到 0.1 以下：
      「肖恩虚弱地躺在病床上，身体几乎无法动弹」vs「他不知道自己仍虚弱地躺在病床上，
        连呼吸都显得吃力」——Jaccard 0.186，包含度 0.333。
    实测重复对包含度 0.25~0.60，独立句对 ≤0.08，用包含度好判断得多。
    """
    try:
        ga, gb = _char_bigrams(a), _char_bigrams(b)
    except Exception:
        return 0.0
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / float(min(len(ga), len(gb)))


def _beat_dup(a, b, jac_th=0.52, con_th=0.45):
    """b 是否与已保留的 a 在讲同一件事。

    - 几乎同文：Jaccard ≥ 0.52（换几个字的重复）
    - 紧挨着的下句把上句重新说一遍：bigram 包含度 ≥ 0.45
    【注意】包含度只能用来比「相邻两句」。放到全篇两两比会把正常解说全吃掉：
      中文短句之间共享「他/的/着/，」等 bigram，实测 62 句会被误删到 12 句。
    """
    return _beat_sim(a, b) >= jac_th or _beat_contain(a, b) >= con_th



def _beats_scrub(beats, dup_th=0.52, min_len=10):
    """对写完的节拍做确定性清洗：去原声台词 → 去猜测定语 → 去重复事件。

    去重实测依据：素材里「他从医院走出」在不同时间窗被描述了 4 次，稿子就跟着写了
    4 节（55/57/59），观众听感是「卡带」。同一事件只讲一次。
    另：整稿以中文为主时，整句英文的节拍直接丢弃（那是抄来的台词，不是旁白）；
    英文稿不受影响。
    """
    beats = list(beats or [])
    # 判断这份稿子是不是中文稿：中文节拍占七成以上才做「整句英文丢弃」
    _n_cn = sum(1 for b in beats
                if len(re.findall(r'[\u4e00-\u9fff]', str(b.get('text') or ''))) >= 2)
    _zh_script = bool(beats) and _n_cn >= max(1, int(len(beats) * 0.7))
    out = []
    for b in beats:
        raw = str(b.get('text') or '').strip()
        if _zh_script and len(re.findall(r'[\u4e00-\u9fff]', raw)) < 2:
            continue
        t = _strip_foreign(raw)
        t = _drop_speculation(t)
        t = _drop_broken_clause(t)
        if len(re.sub(r'\{[^}]*\}', '', t)) < min_len:
            continue          # 清洗后没内容了，丢掉（比留一句废话好）
        if out and _beat_dup(t, out[-1].get('text') or '', jac_th=dup_th):
            continue          # 同一事件重复（相邻两句）
        if any(_beat_sim(t, o.get('text') or '') >= dup_th for o in out):
            continue          # 换几个字的重复（全篇比对）
        nb = dict(b)
        nb['text'] = t
        out.append(nb)
    return out


def _scrub_texts(texts):
    """终稿清洗：只改写、不删句。

    【为什么不能删】texts 的下标与 narr_map、节拍重要度严格对齐，删一句会让
    后面的解说词全部配错画面。所以这里只做「就地改写」，改空了就退回原句。
    【为什么要在终稿再洗一遍】实测润色环节会把写稿时已经清掉的毛病重新写回来：
    22 分钟片润色后出现 6 处「仿佛」+ 3 段夹着英文原声（"Okay, shh"、
    "I hope you left with that spoon collection."）。写稿时洗一次不够。
    """
    out, changed = [], 0
    for t in (texts or []):
        raw = str(t or '')
        try:
            new = _tidy_tail(_drop_broken_clause(_drop_speculation(_strip_foreign(raw))))
        except Exception:
            new = raw
        if not re.sub(r'\{[^}]*\}', '', new).strip():
            new = raw                     # 洗空了就保原句，索引不能断
        if new != raw:
            changed += 1
        out.append(new)
    return out, changed


def _extract_json_obj(content):
    """从 LLM 输出里抠出第一个完整 JSON 对象（容忍前后废话与 ```json 包裹）。"""
    import json as _json
    s = content.find('{')
    if s < 0:
        return None
    depth, end = 0, -1
    for i in range(s, len(content)):
        if content[i] == '{':
            depth += 1
        elif content[i] == '}':
            depth -= 1
            if depth == 0:
                end = i
                break
    if end <= s:
        return None
    try:
        return _json.loads(content[s:end + 1])
    except Exception:
        return None


def _asr_density(asr, vdur, step=0.5):
    """台词密度表：每 step 秒一个格子，值为该格子内的台词字数。
    用于判断「哪段画面有内容」——解说驱动剪辑靠它决定跳过哪些空镜与过场。"""
    n = max(1, int(vdur / step) + 1)
    d = [0.0] * n
    for x in (asr or []):
        try:
            s0, s1 = float(x['start']), float(x['end'])
        except Exception:
            continue
        i0 = max(0, int(s0 / step))
        i1 = min(n, max(i0 + 1, int(s1 / step) + 1))
        txt = str(x.get('text') or '')
        for i in range(i0, i1):
            d[i] += len(txt) / max(1.0, (s1 - s0) / step)
    return d, step


def _build_scenes(cuts, vdur, min_len=3.0):
    """把场景切点转成场景段列表，过短的场景合并到相邻段。
    返回 [(start, end), ...]，覆盖 [0, vdur]。"""
    pts = [0.0] + [float(c) for c in (cuts or []) if 0 < float(c) < vdur] + [vdur]
    pts = sorted(set(round(p, 3) for p in pts))
    raw = [(pts[i], pts[i+1]) for i in range(len(pts)-1) if pts[i+1] - pts[i] > 0.1]
    # 合并过短场景
    merged = []
    for s, e in raw:
        if merged and (e - s < min_len or merged[-1][1] - merged[-1][0] < min_len):
            ps, pe = merged.pop()
            merged.append((ps, e))
        else:
            merged.append((s, e))
    return merged


def _atomic_json_dump(path, obj):
    """原子写 JSON：先写同目录临时文件再 os.replace，避免写到一半崩溃留下损坏文件。

    Windows 上 os.replace 对同卷目标是原子替换，读侧永远看到完整文件。
    写失败返回 False，不抛异常（断点进度丢了最坏只是重跑一遍，不能拖垮主流程）。"""
    d = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.tmp_', suffix='.json', dir=d)
    except OSError:
        return False
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def _load_json_file(path):
    """读取 JSON，文件不存在或损坏一律返回 None（不抛异常）。"""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


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


def _vlm_sample_timeline(video_path, vdur, asr, run_dir, progress=None, interval=28.0,
                         cancel_check=None, quality=None):
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
    if not _w.vlm_enabled():
        return []
    try:
        if not _w.vlm_ping()[0]:
            return []
    except Exception:
        return []

    # 路径修复：progress_state 里的 run_dir 可能是相对路径，转成绝对路径
    # （与 _generate_all_tts 同样处理），否则进度文件会写到项目根目录。
    if run_dir and not os.path.isabs(run_dir):
        run_dir = os.path.join(_w.OUTDIR, run_dir)

    # 质量模式判定：默认质量；NARRATE_FAST=1 才走旧的「稀疏 + 台词过滤」。
    if quality is None:
        quality = (os.environ.get('NARRATE_FAST') != '1')

    # 抽样间隔
    #  质量模式：全片覆盖优先。≥12s，同时按总点数封顶 300 点（2 小时片 ≈ 24s/点），
    #            避免超长片把 VLM 阶段拖到不可接受。
    #  快速模式：旧策略，1 小时约 60 点。
    if quality:
        interval = max(12.0, float(vdur) / 300.0)
        # 短片段保护：interval 大于片长会让抽样点数为 0，画面理解直接空手而归。
        # 5 秒素材按 12s 间隔抽不到任何帧，这里退化为「至少 4 个点」。
        if interval >= float(vdur):
            interval = max(1.0, float(vdur) / 4.0)
    else:
        interval = max(15.0, float(vdur) / 60.0)

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
        vlm_model = vlm_cfg().get('model', 'default')
    except Exception:
        vlm_model = 'default'
    # 键里必须带抽样策略：'grid' 与 'sub' 产出的时间点集合完全不同，
    # 共用键会让「改用台词对齐」这个改动被旧缓存整个短路掉。
    _plan_tag = 'sub' if _anchor_mode else 'grid'
    cache_key = _video_cache_key(
        video_path, f'vlm_sample_{vlm_model}_{int(interval)}_{_plan_tag}')
    cached = _cache_load(cache_key)
    if cached:
        _log.info(f'[DIAG] VLM抽样命中缓存: {len(cached)}个时间点')
        if progress:
            progress['phase'] = '画面索引（缓存命中）'
            progress['pct'] = 46
        return cached

    # [P0-1 方案1.5] 抽帧产物缓存目录：按视频指纹+间隔复用（原 frame_dir = run_dir/sample_frames，换文案会重新抽帧）
    frame_dir = _sample_frame_cache_dir(video_path, interval)
    results = []
    sys_prompt = ('你是影视场景分析助手。根据画面和提供的台词，用JSON格式结构化描述这个时间点的画面。'
                  '字段：location(地点)，characters(主要人物)，'
                  'event(正在发生什么事，一句话)，'
                  'dialogue(这段台词的核心内容，没有则留空)，'
                  'summary(画面整体概括，不超过30字)。只输出JSON。')

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
    if _sample_frame_cache_ready(frame_dir, total_samples):
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
                ffmpeg_run(['-y', '-ss', '%.3f' % ts, '-i', video_path,
                            '-frames:v', '1', '-vf', 'scale=min(iw\\,640):-2',
                            '-q:v', '4', '-an', fp])
            n_got = sum(1 for i in range(total_samples)
                        if os.path.exists(os.path.join(frame_dir, 'sample_%04d.jpg' % i)))
            rc = 0 if n_got == total_samples else 1
        else:
            rc, _o, _e = ffmpeg_run(['-y', '-ss', '%.3f' % (interval / 2.0), '-i', video_path,
                                     '-vf', 'fps=1/%.6f,scale=min(iw\\,640):-2' % interval,
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
                r2, _o2, _e2 = ffmpeg_run(['-y', '-ss', '%.3f' % ts, '-i', video_path,
                                           '-frames:v', '1', '-vf', 'scale=min(iw\\,640):-2',
                                           '-q:v', '4', '-an', fp])
        _sample_frame_cache_mark(frame_dir, total_samples)
        _sample_frame_cache_trim()

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

    _SIM_THRESHOLD = 0.85  # [P0-1 低风险优化] 0.92→0.88→0.85，持续放宽以复用更多相似帧（省约10%~25% VLM调用）
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
    vlm_count = 0
    batch_size = 4 if quality else 8
    _VLM_KEEP = '30m' if quality else 0
    _VLM_TO_MULTI = 300 if quality else 90
    _VLM_TO_SINGLE = 180 if quality else 60

    def _batch_prompt(n):
        return ('你是影视场景分析助手。以下按时间顺序给出%d张画面帧。'
                '请对每张帧分别用JSON描述，帧之间用---分隔。'
                '每个JSON字段：location/characters/event/dialogue/summary。'
                '无台词的帧 dialogue 留空字符串，但仍须描述画面正在发生什么。'
                '只输出JSON和---分隔符。' % n)

    def _call_frames(_frames):
        """对一组帧发起一次 VLM 调用，返回与 _frames 等长的 dict 列表；失败抛异常。"""
        if len(_frames) == 1:
            r = _w.vlm_chat(_frames[0],
                            '请用JSON描述这个画面：location/characters/event/dialogue/summary',
                            system=sys_prompt, timeout=_VLM_TO_SINGLE, keep_alive=_VLM_KEEP)
            return [_extract_json_obj(r) or {}]
        r = vlm_chat_multi(_frames, _batch_prompt(len(_frames)), system=sys_prompt,
                           timeout=_VLM_TO_MULTI, keep_alive=_VLM_KEEP,
                           max_tokens=max(600, 220 * len(_frames)))
        parts = (r or '').split('---')
        objs = [_extract_json_obj(p) or {} for p in parts[:len(_frames)]]
        while len(objs) < len(_frames):
            objs.append({})
        return objs

    progress_file = os.path.join(run_dir, 'vlm_progress.json') if run_dir else None
    _analyzed = {}   # start_key -> result_fields（不含start/end）
    _failed = set()  # 本轮调用抛异常的帧：不落进度文件，下次续跑会重试

    # --- 读取上次未完成的进度（指纹校验：换视频/换模型/换间隔则作废）---
    _resumed = 0
    if progress_file and os.path.exists(progress_file):
        _p = _load_json_file(progress_file) or {}
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
    for bi in range(0, len(_pending_frames), batch_size):
        # 协作式取消：用户点「停止」时立即退出，已分析的部分保留在进度文件里
        if _aborted():
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
            if len(frames) > 1 and not _aborted() and _consec_fail <= _RETRY_MAX_CONSEC:
                objs = []
                for _f in frames:
                    if _aborted():
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
            _atomic_json_dump(progress_file, {
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
        _cache_save(cache_key, results)
    # 全部完成并已落缓存 → 清理断点进度文件（下次走缓存，不再需要续跑）
    # 中途取消 / 有失败批次时不删：取消留给下次续跑恢复，失败留给下次重试。
    if _all_done and not _failed and progress_file and os.path.exists(progress_file):
        try:
            os.remove(progress_file)
            _log.info('[DIAG] VLM断点续跑: 已全部完成，清理进度文件')
        except OSError:
            pass
    return results


def _vlm_sample_captions(video_path, vdur, run_dir, n_samples=24, progress=None):
    """均匀采样全片 N 帧，VLM 描述画面内容，返回 [(time_sec, caption), ...]。

    用于「台词匹配 + 画面匹配」融合评分：选片段时不只看台词是否提到关键词，
    还要看画面内容是否和解说词相关。VLM 不可用时返回 []，退化为纯台词匹配。
    """
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
        rc, _o, _e = ffmpeg_run(['-y', '-ss', '%.3f' % t, '-i', video_path,
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


_SEMANTIC_ACT_VALUES = {'铺垫', '发展', '冲突', '转折', '高潮', '结局', '过场', '闪回', '引入', '收束'}


def _scene_describe(sc):
    """把单个 VLM 场景窗压缩成一行文本（过滤『无台词区间』这类占位）。"""
    parts = []
    for k, lab in (('location', '地点'), ('characters', '人物'), ('event', '事件'),
                   ('dialogue', '台词'), ('summary', '画面')):
        v = str(sc.get(k) or '').strip()
        if v and v not in ('无台词区间', 'VLM超时跳过'):
            parts.append(lab + ':' + v)
    return '；'.join(parts)


def _scene_dialogue(asr, start, end, limit=220):
    """取 [start,end]（含±1s 容差）内的真实台词，最长 limit 字。"""
    parts = []
    for x in (asr or []):
        try:
            a0 = float(x.get('start', 0))
            a1 = float(x.get('end', a0 + 1))
        except (ValueError, TypeError):
            continue
        if a1 < start - 1.0 or a0 > end + 1.0:
            continue
        t = str(x.get('text') or '').strip()
        if t:
            parts.append(t)
    return (' / '.join(parts))[:limit]


def _story_parse_chunk(text):
    """解析一段剧情理解输出：每行一个 JSON 对象（JSONL），容忍前后废话与 ```json 包裹。
    返回 {场景下标: {story, who, act, keywords}}；整段失败返回 {}。"""
    out = {}
    if not text:
        return out
    for line in (text or '').split('\n'):
        obj = _extract_json_obj(line)
        if not isinstance(obj, dict):
            continue
        i = obj.get('i')
        if i is None:
            i = obj.get('场景') or obj.get('idx') or obj.get('scene')
        try:
            i = int(i)
        except (TypeError, ValueError):
            continue
        story = str(obj.get('story') or obj.get('含义') or '').strip()
        if not story:
            continue
        who = str(obj.get('who') or obj.get('人物') or '').strip()
        act = str(obj.get('act') or obj.get('作用') or obj.get('role') or '').strip()
        if act not in _SEMANTIC_ACT_VALUES:
            act = ''
        kw = obj.get('keywords') or obj.get('关键词') or []
        if isinstance(kw, str):
            kw = [k.strip() for k in re.split(r'[,，、;；/| ]+', kw) if k.strip()]
        elif not isinstance(kw, list):
            kw = []
        kw = [str(k).strip() for k in kw if str(k).strip()][:5]
        out[i] = {'story': story[:60], 'who': who[:50], 'act': act, 'keywords': kw}
    return out


def _char_bigrams(text):
    text = str(text or '')
    return set(text[i:i + 2] for i in range(len(text) - 1) if text[i:i + 2].strip())


def _scene_dialogue_of(sc, asr):
    try:
        st0 = float(sc.get('start', 0) or 0)
        st1 = float(sc.get('end', st0 + 5) or st0 + 5)
    except (ValueError, TypeError):
        return ''
    return _scene_dialogue(asr, st0, st1)


def _infer_scene_story(scenes, asr=None, movie_name='', progress=None, window=10, plot=''):
    """剧情理解层核心：按时间窗把「有内容的场景」分批让 LLM 推断叙事含义。

    只推断 VLM 真实分析过（有画面内容）的场景窗；『无台词区间』等占位窗跳过。
    plot：可选的剧情资料。有它时模型知道人物是谁、主线是什么，推断出来的是
      「这段在剧情里起什么作用」，而不是「这段画面里有什么」——这正是写稿最需要的。
    返回 {scene_idx: {story, who, act, keywords}}；模型不可用/输出不可解析时返回 {}。
    """
    if not scenes:
        return {}
    candidates = [i for i, sc in enumerate(scenes) if _scene_describe(sc)]
    if not candidates:
        return {}
    # 文字 LLM 不存在时直接降级（对齐/润色仍走旧逻辑）
    try:
        if not _local_model_available():
            return {}
    except Exception:
        return {}

    def up(ph, pct):
        if progress:
            progress['phase'] = ph
            progress['pct'] = pct

    def _fmt(x):
        try:
            m, sec = divmod(int(float(x) or 0), 60)
        except (TypeError, ValueError):
            return '--:--'
        return '%02d:%02d' % (m, sec)

    prev_summary = ''   # 上一批的收尾情节，作为下一批的前情，帮助角色/因果连贯
    result = {}
    total = len(candidates)
    n_call = 0
    for bi in range(0, total, window):
        if _aborted():
            raise AbortError('用户取消了任务')
        batch = candidates[bi:bi + window]
        n_call += 1
        up('剧情理解 %d/%d（推断画面背后的故事…）' % (n_call, (total + window - 1) // window), 46)
        lines = []
        for i in batch:
            sc = scenes[i]
            base = _scene_describe(sc)
            dlg = _scene_dialogue_of(sc, asr)
            rec = '场景%d [%s-%s] %s' % (i, _fmt(sc.get('start')), _fmt(sc.get('end')), base)
            if dlg:
                rec += '。窗内台词：' + dlg
            lines.append(rec)
        head = ('这是某部影视片中一段连续画面的记录（按时间顺序，每条是一个约几十秒的时间窗：画面理解+窗内台词）。'
                '请像看片人一样结合台词、画面与上下文，推断每一段『在讲什么』——即该段在剧情里的含义，'
                '而不是复述画面。只输出 JSON 数组，每个元素独占一行，字段：\n'
                '{"i": 场景编号, "story": "该段在剧情里的含义，一句话且≤45字",'
                ' "who": "画面人物在本段剧情中的身份/关系，如『主角·新娘与新郎』，看不出则省略",'
                ' "act": "本段对故事的作用，只能取：铺垫/发展/冲突/转折/高潮/结局/过场/闪回/引入/收束之一",'
                ' "keywords": ["2-4个剧情关键词"]}\n'
                '要求：1)判断依据只能是画面与台词证据，不确定就保守概括，禁止编造画面里没有的情节；'
                '2)关键词应选能代表这段剧情、便于与解说词比对的实词；'
                '3)story 要写「谁做了什么、结果如何」，不要写「气氛紧张」「仿佛藏着秘密」这类空话，'
                '也不要出现「画面」「镜头」。\n')
        if movie_name:
            head = '电影片名：《%s》\n' % movie_name + head
        _plot_ctx = str(plot or '').strip()
        if _plot_ctx:
            head += '【已知剧情资料（用于确认人物身份，不足处按画面推断）】\n%s\n' % _plot_ctx[:1500]
        if prev_summary:
            head += '【已发生的前情】\n' + prev_summary + '\n'
        prompt = head + '\n【场景记录】\n' + '\n'.join(lines)
        try:
            resp = _llm_light(prompt, '你是影视剧情分析师，擅长从画面与台词还原剧情。', timeout=240)
        except Exception:
            resp = None
        parsed = _story_parse_chunk(resp or '') if resp else {}
        if not parsed:
            # 整批失败：跳过该批，不硬凑，避免把脏数据喂给下游
            continue
        for i, st in parsed.items():
            if i in result:
                continue
            result[i] = st
        if result:
            prev_summary = '\n'.join(
                ('——%s %s' % (_fmt(scenes[i].get('start')), result[i]['story']))
                for i in batch[-2:] if result.get(i) and result[i].get('story'))
    _log.info(f'[DIAG] 剧情理解层: 推断 {len(result)}/{total} 个场景窗含义, 共{n_call}批')
    return result


# ============================================================================
# 【故事线构建】从「逐窗画面含义」提炼出「整部片讲了什么」
# ----------------------------------------------------------------------------
# 为什么必须多加这一层：只把逐窗画面含义丢给写稿，8B 写出来的必然是镜头清单
#   「他走进走廊 / 他拿起篮子 / 他站在门口」——因为素材本身就是按镜头排列的。
# 解说词要讲的是「故事」：谁和谁什么关系、为什么这么做、结果怎样。
# 所以先在时间轴上把逐窗含义合成「幕」，再把幕合成「主线 + 人物表」，
# 写稿拿到的就是一条完整故事，而不是 110 段画面说明。
# ============================================================================

def _group_scene_acts(scene_story, scenes, n_acts=12):
    """把逐窗剧情含义按时间顺序等分成 n_acts 组（幕）。返回 [{start,end,items}]。"""
    if not scene_story or not scenes:
        return []
    items = []
    for k in sorted(scene_story.keys()):
        try:
            idx = int(k)
        except (TypeError, ValueError):
            continue
        st = scene_story[k] or {}
        story = str(st.get('story') or '').strip()
        if not story:
            continue
        sc = scenes[idx] if 0 <= idx < len(scenes) else {}
        try:
            t0 = float(sc.get('start') or 0)
            t1 = float(sc.get('end') or t0 + 5)
        except (TypeError, ValueError):
            t0, t1 = 0.0, 5.0
        items.append((t0, t1, story, str(st.get('who') or '').strip()))
    if not items:
        return []
    items.sort(key=lambda x: x[0])
    n_acts = max(2, min(int(n_acts), len(items)))
    step = len(items) / float(n_acts)
    groups = []
    for a in range(n_acts):
        chunk = items[int(round(a * step)):int(round((a + 1) * step))]
        if not chunk:
            continue
        groups.append({'start': chunk[0][0], 'end': chunk[-1][1],
                       'items': [(c[0], c[2], c[3]) for c in chunk]})
    return groups


def _fmt_tc(x):
    try:
        m, sec = divmod(int(float(x) or 0), 60)
    except (TypeError, ValueError):
        return '--:--'
    return '%02d:%02d' % (m, sec)


def _infer_acts(scene_story, scenes, movie_name='', plot='', progress=None, n_acts=12, asr=None):
    """逐幕推断剧情：每幕输入该时间段的逐窗含义 + 该段的台词，输出「这一幕发生了什么」。

    输出是**故事**而不是画面清单：谁做了什么、为什么、结果如何。
    为什么要把台词也喂进去：很多剧情信息只在台词里（「别把这事告诉洛莉」），
    只看画面描述只能得出「两个人站在车边」。台词是讲故事最硬的证据。
    返回 [{start,end,summary}]；整幕失败就跳过该幕（不硬凑脏数据喂给写稿）。
    """
    groups = _group_scene_acts(scene_story, scenes, n_acts=n_acts)
    if not groups:
        return []
    try:
        if not _local_model_available():
            return []
    except Exception:
        return []

    def up(ph, pct):
        if progress:
            progress['phase'] = ph
            progress['pct'] = pct

    acts = []
    prev = ''       # 上一幕摘要：幕与幕之间的因果靠它接上
    for ai, g in enumerate(groups):
        if _aborted():
            raise AbortError('用户取消了任务')
        up('剧情理解·分幕 %d/%d' % (ai + 1, len(groups)), 46)
        lines = ['[%s] %s%s' % (_fmt_tc(t), s, ('（%s）' % w) if w else '')
                 for t, s, w in g['items']]
        dlg = ''
        if asr:
            try:
                dlg = _asr_text_in(asr, float(g['start']), float(g['end']))[:600]
            except Exception:
                dlg = ''
        head = ('你是影视剧情分析师。下面按时间顺序列出了这部电影里一段连续的内容记录，'
                '每条是「时间码 + 这段画面在讲什么」，后面附上这一段的台词。\n'
                '请把这一整段概括成**剧情**：谁、做了什么、为什么这么做、结果怎样。\n'
                '要求：\n'
                '- 90-150 字，一段连贯叙述，不是画面清单。\n'
                '- 台词里交代的事要写进去（谁要谁做什么、谁瞒着谁），这往往才是剧情。\n'
                '- 不要写「画面」「镜头」「他看到」「仿佛」。\n'
                '- 只看这些记录能确定的事，不确定就不写。\n'
                '- 直接输出这段剧情，不要标题、不要编号。\n\n')
        if movie_name:
            head = '片名：《%s》\n' % movie_name + head
        if prev:
            head += '【前面已经发生】%s\n\n' % prev
        prompt = head + '【本段记录（%s - %s）】\n%s' % (
            _fmt_tc(g['start']), _fmt_tc(g['end']), '\n'.join(lines))
        if dlg:
            prompt += '\n【本段台词（原声转录）】\n' + dlg
        try:
            resp = _llm_light(prompt, '你是影视剧情分析师，只输出剧情概括。',
                              timeout=200, max_tokens=500)
        except AbortError:
            raise
        except Exception:
            resp = None
        txt = ''
        for line in str(resp or '').splitlines():
            line = re.sub(r'^\s*[\-\*\d\.、\)）【】]+\s*', '', line).strip()
            if len(line) >= 10:
                txt = line if not txt else (txt + line)
            if len(txt) >= 90:
                break
        if not txt:
            continue
        # 幕摘要也要清洗：8B 爱写「疑似…」「暗示他可能…」，写稿会把它当成事实放大成
        # 「他决定毒杀同伴」这种凭空捏造的情节。不清洗就是给写稿喂谣言。
        txt = _drop_speculation(txt)
        acts.append({'start': g['start'], 'end': g['end'], 'summary': txt[:200]})
        prev = txt[:120]
    _log.info('[DIAG] 分幕剧情: %d/%d 幕成功' % (len(acts), len(groups)))
    return acts


def _build_storyline(acts, movie_name='', plot=''):
    """把各幕剧情合成「人物表 + 主线」，这是写解说稿的骨架。

    一次调用输入全部幕摘要（约 1200 字），输出 300-500 字。这样写稿时
    每一句都能挂在这条主线上，前后才有因果联系。
    """
    if not acts:
        return None
    body = '\n'.join('%d. [%s-%s] %s' % (i + 1, _fmt_tc(a['start']), _fmt_tc(a['end']),
                                         a['summary'])
                     for i, a in enumerate(acts))
    prompt = ('下面是这部电影按时间顺序的分幕剧情。请整理成写解说稿用的故事线。\n\n'
              '【输出格式，两段，不要其它内容】\n'
              '【人物】主要人物及身份关系，一句话说清谁是谁（如「肖恩：小镇牧师，兼任警员」）。\n'
              '【主线】200-300 字，按因果讲清整部片：起因是什么、主角要做什么、'
              '中间遇到什么阻力、最后怎么样了。不要分点，不要写「画面」。\n\n'
              '【分幕剧情】\n%s\n' % (('%s\n' % movie_name if movie_name else '') + body))
    try:
        resp = _llm_light(prompt, '你是影视剧情剪辑师，只输出人物与主线两段。',
                          timeout=240, max_tokens=900)
    except AbortError:
        raise
    except Exception:
        return None
    txt = str(resp or '').strip()
    if len(txt) < 40:
        return None
    chars, mainline = '', ''
    m1 = re.search(r'【?人物】?\s*[:：]?\s*(.+?)(?=【?主线】|$)', txt, re.S)
    m2 = re.search(r'【?主线】?\s*[:：]?\s*(.+)$', txt, re.S)
    if m1:
        chars = re.sub(r'\s+', ' ', m1.group(1)).strip(' ：:')
    if m2:
        mainline = re.sub(r'\s+', ' ', m2.group(1)).strip(' ：:')
    if not mainline:
        mainline = re.sub(r'\s+', ' ', txt)[:600]
    chars = _drop_speculation(chars)
    mainline = _drop_speculation(mainline)
    _log.info('[DIAG] 故事线: 人物%d字 主线%d字' % (len(chars), len(mainline)))
    return {'characters': chars[:400], 'mainline': mainline[:800]}


def _align_from_story_fill(beats, scenes, alignment, scene_story):
    """LLM 没对齐到的解说词，若与某个场景的「剧情含义」有强词面重叠则补对齐。
    纯加分项：宁可漏对齐，不引入低置信映射。返回新 alignment。"""
    if not scene_story or not beats:
        return alignment
    bg_by_scene = {}
    for i, st in scene_story.items():
        terms = ' '.join(x for x in (st.get('story'), st.get('who'),
                                     ' '.join(st.get('keywords') or [])) if x)
        bg = _char_bigrams(terms)
        if bg:
            bg_by_scene[i] = bg
    if not bg_by_scene:
        return alignment
    out = dict(alignment)
    for bi, t in enumerate(beats):
        if bi in out:
            continue
        qbg = _char_bigrams(t)
        if not qbg:
            continue
        best, best_score = None, 0
        for i, sbg in bg_by_scene.items():
            ov = len(qbg & sbg)
            if ov >= 2 and ov * 4 >= len(qbg):   # 命中≥2 个二字词，且覆盖≥25%
                if ov > best_score:
                    best, best_score = i, ov
        if best is not None:
            out[bi] = [best]
    if len(out) != len(alignment):
        _log.info(f'[DIAG] 剧情语义兜底对齐: 补齐 {len(out) - len(alignment)} 节（基于剧情含义词面重叠）')
    return out


def _parse_alignment_obj(obj, scenes):
    """把 LLM 返回的对齐对象规范成 {beat_idx: [scene_idx,...]}。"""
    alignment = {}
    items = obj.get('对齐') or obj.get('alignment') or []
    if isinstance(items, list):
        for item in items:
            bi_raw = item.get('解说词')
            if bi_raw is None:
                bi_raw = item.get('beat')
            sids_raw = item.get('场景')
            if sids_raw is None:
                sids_raw = item.get('scenes')
            # 模型常把编号写成字符串（"18" 而不是 18），真机实测整批 0 命中就是它
            if isinstance(bi_raw, str) and bi_raw.strip().lstrip('-').isdigit():
                bi_raw = int(bi_raw.strip())
            if isinstance(sids_raw, str) and sids_raw.strip().lstrip('[').rstrip(']').isdigit():
                sids_raw = [sids_raw]
            if isinstance(bi_raw, int) and isinstance(sids_raw, list):
                _sids = []
                for x in sids_raw:
                    try:
                        _sids.append(int(float(x)))
                    except (TypeError, ValueError):
                        continue
                alignment[bi_raw] = _sids
    for bi in list(alignment.keys()):
        alignment[bi] = [x for x in alignment[bi] if 0 <= x < len(scenes)]
        if not alignment[bi]:
            del alignment[bi]
    return alignment


def _llm_align_beats_to_scenes(beats, scenes, movie_name='', scene_story=None):
    """用LLM把解说词和场景做语义对齐，返回 {beat_idx: [scene_idx, ...]}，失败返回 {}。
    scene_story（剧情理解层产物）存在时，场景侧展示的是『这段在讲什么』的剧情含义，
    让对齐依据从浅层画面文字升级为叙事语义。"""
    if not beats or not scenes:
        return {}
    scene_lines = []
    for i, sc in enumerate(scenes):
        st = (scene_story or {}).get(i) or {}
        if st.get('story'):
            parts = []
            parts.append('剧情:' + st['story'])
            if st.get('who'):
                parts.append('人物:' + st['who'])
            if st.get('act'):
                parts.append('作用:' + st['act'])
            if st.get('keywords'):
                parts.append('关键词:' + ','.join(st['keywords']))
            line = '；'.join(parts)
        else:
            line = _scene_describe(sc)
        scene_lines.append('场景%d(%.0f-%.0fs): %s' % (i, sc.get('start', 0) or 0, sc.get('end', 0) or 0, line))
    beat_lines = ['解说词%d: %s' % (i, t[:120]) for i, t in enumerate(beats)]
    prompt = '你是影视剪辑师。根据场景描述，把每段解说词对齐到最相关的场景序号。\n'
    prompt += '输出JSON格式：{"对齐":[{"解说词":0,"场景":[1,2]},...]}\n'
    prompt += '只输出JSON，不要其他文字。\n\n'
    if movie_name:
        prompt += '电影：%s\n\n' % movie_name
    prompt += '【场景列表】\n' + '\n'.join(scene_lines[:80])
    prompt += '\n\n【解说词列表】\n' + '\n'.join(beat_lines[:60])
    # 优先本地LLM
    try:
        if _w.local_llm_enabled() and _w.local_llm_ping()[0]:
            resp = _llm_light(prompt, timeout=240)
            obj = _extract_json_obj(resp) or {}
            alignment = _parse_alignment_obj(obj, scenes)
            return _align_from_story_fill(beats, scenes, alignment, scene_story)
    except Exception:
        pass
    # 云端兜底
    try:
        if _w.ai_enabled('chat'):
            cfg = _w.chat_cfg()
            import urllib.request, json as _json
            payload = {'model': cfg.get('model', 'gpt-4o-mini'), 'messages': [
                {'role': 'system', 'content': '你是影视剪辑师，输出JSON。'},
                {'role': 'user', 'content': prompt}], 'temperature': 0.3}
            req = urllib.request.Request(cfg['base_url'].rstrip('/') + '/chat/completions',
                                         data=_json.dumps(payload).encode('utf-8'),
                                         headers={'Content-Type': 'application/json',
                                                  'Authorization': 'Bearer ' + cfg.get('api_key', '')})
            with urllib.request.urlopen(req, timeout=60) as r:
                resp = (_json.loads(r.read()).get('choices') or [{}])[0].get('message', {}).get('content', '')
            obj = _extract_json_obj(resp) or {}
            alignment = _parse_alignment_obj(obj, scenes)
            return _align_from_story_fill(beats, scenes, alignment, scene_story)
    except Exception:
        pass
    return {}


def _llm_refine_beats_with_scenes(beats, scenes, alignment, asr, movie_name=''):
    """剧情理解层：用VLM画面描述+ASR台词润色每段解说词，让解说更贴合实际画面。
    不改变剧情主线，只让描述更准确、更有画面感。失败返回原beats。"""
    if not beats or not scenes or not alignment:
        return beats
    beat_contexts = []
    for bi, beat in enumerate(beats):
        scene_idxs = alignment.get(bi, [])
        if not scene_idxs:
            beat_contexts.append(None)
            continue
        si = scene_idxs[0]
        if si >= len(scenes):
            beat_contexts.append(None)
            continue
        sc = scenes[si]
        scene_desc = sc.get('event') or sc.get('location') or sc.get('description') or ''
        t0 = float(sc.get('start', 0))
        t1 = float(sc.get('end', t0 + 5))
        scene_asr = []
        for a in asr:
            try:
                a0 = float(a.get('start', 0))
                if t0 <= a0 <= t1:
                    scene_asr.append(str(a.get('text', '')))
            except (ValueError, TypeError):
                continue
        asr_text = ' / '.join(scene_asr[:3]) if scene_asr else ''
        beat_contexts.append({
            'beat_idx': bi,
            'original': str(beat.get('text') or beat if isinstance(beat, dict) else str(beat)),
            'scene': scene_desc[:100],
            'dialogue': asr_text[:150]
        })
    to_refine = [c for c in beat_contexts if c]
    if not to_refine:
        return beats
    prompt = f'你是电影解说文案润色专家。下面是电影《{movie_name or "未知"}》的解说稿片段，'
    prompt += '请根据每段对应的画面描述和角色台词，把解说词改得更贴合画面、更有画面感。\n'
    prompt += '规则：1)不改变剧情主线和因果关系 2)只润色描述，不添加画面里没有的情节 3)保持口语化解说风格 4)每段30-60字 5)不要加标题或序号\n\n'
    for c in to_refine:
        prompt += f'【第{c["beat_idx"]+1}段】\n'
        prompt += f'原解说：{c["original"]}\n'
        prompt += f'画面描述：{c["scene"]}\n'
        if c['dialogue']:
            prompt += f'角色台词：{c["dialogue"]}\n'
        prompt += '\n'
    prompt += '请按顺序输出润色后的解说词，每行一段，不要加其他文字。'
    try:
        resp = local_llm_chat(prompt, system='你是专业电影解说文案编辑，擅长让解说词与画面精准对应。')
        if not resp:
            return beats
        lines = [l.strip() for l in resp.split('\n') if l.strip() and not l.strip().startswith(('【', '#', '第'))]
        if not lines:
            return beats
        refined_beats = list(beats)
        for i, c in enumerate(to_refine):
            if i < len(lines) and lines[i]:
                bi = c['beat_idx']
                if isinstance(refined_beats[bi], dict):
                    refined_beats[bi]['text'] = lines[i]
                else:
                    refined_beats[bi] = lines[i]
        _log.info(f'[DIAG] 剧情理解润色: {len([l for l in lines if l])}/{len(to_refine)}段已润色')
        return refined_beats
    except Exception as e:
        _log.info(f'[DIAG] 剧情理解润色失败: {e}')
        return beats


def _llm_align_and_refine(beats, scenes, scene_story, asr, movie_name=''):
    # [S3 合并轮次] 语义对齐 + 剧情润色合并为一次 LLM 调用。
    # 输入：解说稿 beats、画面索引 scenes、剧情理解 scene_story（可选）、台词 asr。
    # 输出：- (alignment, refined) 对齐与润色都可用；
    #       - (alignment, None)   只有对齐（润色缺失，调用方回退独立润色轮）；
    #       - (None, refined)     只有润色（对齐缺失，调用方回退独立对齐轮）；
    #       - None                整体失败（调用方回退旧分步路径）。
    # 仅走本地 LLM（云端行为不变，由旧路径的云端兜底覆盖）。
    if not beats or not scenes:
        return None
    # 场景侧：有剧情理解则展示叙事含义，否则回退画面描述
    scene_lines = []
    for i, sc in enumerate(scenes):
        st = (scene_story or {}).get(i) or {}
        if st.get('story'):
            parts = ['剧情:' + st['story']]
            if st.get('who'):
                parts.append('人物:' + st['who'])
            if st.get('act'):
                parts.append('作用:' + st['act'])
            if st.get('keywords'):
                parts.append('关键词:' + ','.join(st['keywords']))
            line = '；'.join(parts)
        else:
            line = _scene_describe(sc)
        scene_lines.append('场景%d(%.0f-%.0fs): %s' % (i, sc.get('start', 0) or 0, sc.get('end', 0) or 0, line))
    beat_lines = ['解说词%d: %s' % (i, t[:120]) for i, t in enumerate(beats)]
    prompt = ('你是影视剪辑师与解说文案编辑。任务分两步：\n'
              '1) 把每段解说词对齐到最相关的场景序号（可多选，按相关度排序；对不上就空数组）；\n'
              '2) 根据对应场景的画面与台词，把每段解说词润色得更贴合画面——不改变剧情主线与因果关系，'
              '不添加画面里没有的情节，保持口语化解说风格，每段30-60字。\n'
              # 【坑】不要给「第0段润色后」这种字面样例：模型会原样当台词返回。
              '只输出JSON，两个键：'
              '"对齐" 是数组，每项 {"解说词": 解说词序号, "场景": [场景序号,...]}；'
              '"润色" 是字符串数组，与解说词顺序一一对应，每项是润色后的完整解说词正文'
              '（必须是真台词内容，不能是「第N段」这类占位文字）。\n\n')
    if movie_name:
        prompt += '电影：%s\n\n' % movie_name
    prompt += '【场景列表】\n' + '\n'.join(scene_lines[:80])
    prompt += '\n\n【解说词列表】\n' + '\n'.join(beat_lines[:60])
    try:
        if _w.local_llm_enabled() and _w.local_llm_ping()[0]:
            resp = local_llm_chat(prompt,
                                  system='你是影视剪辑师，擅长解说词与画面的语义对齐与贴合润色，只输出JSON。',
                                  timeout=300)
        else:
            return None
    except Exception as e:
        _log.info('[DIAG] S3合并对齐+润色调用失败: %s' % e)
        return None
    obj = _extract_json_obj(resp or '') or {}
    if not obj:
        return None
    # 对齐：解析失败/为空 → None（触发调用方回退独立对齐轮）
    alignment = _parse_alignment_obj(obj, scenes)
    if alignment:
        alignment = _align_from_story_fill(beats, scenes, alignment, scene_story)
    else:
        alignment = None
    # 润色：缺字段/行数不符 → None（触发调用方回退独立润色轮）
    refined = None
    refined_raw = obj.get('润色') or obj.get('refined') or []
    if isinstance(refined_raw, list) and refined_raw:
        lines = [str(x).strip() for x in refined_raw if str(x) is not None and str(x).strip()]
        if len(lines) == len(beats):
            refined = lines
    if refined is not None:
        _log.info('[DIAG] S3合并对齐+润色: 对齐%d节, 润色%d段' %
                  (len(alignment or {}), len(refined)))
    return alignment, refined


def _cloud_chat_text(prompt, system='', timeout=120):
    """云端 chat（仅当配置了云端模型）。润色优先走云端：不占显存、文案上限更高。
    未配置或失败返回 None，由调用方回退本地模型。"""
    try:
        if not _w.ai_enabled('chat'):
            return None
        cfg = _w.chat_cfg() or {}
        if not cfg.get('base_url') or not cfg.get('model'):
            return None
        import urllib.request
        import json as _json
        msgs = []
        if system:
            msgs.append({'role': 'system', 'content': system})
        msgs.append({'role': 'user', 'content': prompt})
        payload = {'model': cfg.get('model'), 'messages': msgs, 'temperature': 0.6}
        req = urllib.request.Request(
            (cfg.get('base_url') or '').rstrip('/') + '/chat/completions',
            data=_json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json',
                     'Authorization': 'Bearer ' + (cfg.get('api_key') or '')})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = _json.loads(r.read().decode('utf-8'))
        return ((data.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
    except Exception as e:
        _log.info('[DIAG] 云端调用失败（回退本地）: %s' % e)
        return None


def _llm_align_batched(beats, scenes, base=None, scene_story=None, asr=None,
                       movie_name='', batch=6, timeout=150, progress=None,
                       use_cloud=None, max_tokens=700):
    """分批语义对齐：每批只让模型处理 batch 句，输出短、成功率高。
    失败的批次保留 base（调用方传入的算法对齐结果）→ 任何情况下都不会比算法对齐更差。
    返回合并后的 {beat_idx: [scene_idx, ...]}。"""
    base = dict(base or {})
    n = len(beats or [])
    if n == 0 or not scenes:
        return base
    if use_cloud is None:
        use_cloud = bool(_w.ai_enabled('chat'))
    _local_ok = False
    try:
        _local_ok = bool(_w.local_llm_enabled()) and bool(_w.local_llm_ping()[0])
    except Exception:
        _local_ok = False
    scene_lines = []
    for i, sc in enumerate(scenes):
        st = (scene_story or {}).get(i) or {}
        if st.get('story'):
            line = str(st['story'])
            if st.get('who'):
                line += '；人物:' + str(st['who'])
        else:
            line = _scene_describe(sc)
        scene_lines.append('场景%d(%.0f-%.0fs): %s' % (
            i, float(sc.get('start', 0) or 0), float(sc.get('end', 0) or 0), line))
    scene_block = '\n'.join(scene_lines[:80])
    for start in range(0, n, batch):
        if _aborted():
            raise AbortError('用户取消了任务')
        idxs = list(range(start, min(n, start + batch)))
        beat_block = '\n'.join('解说词%d: %s' % (i, str(beats[i])[:120]) for i in idxs)
        prompt = ('你是影视剪辑师。根据场景描述，把下面这些解说词各自对齐到最相关的场景序号。\n'
                  '要求：按剧情时间顺序；同一句可对齐 1~3 个连续场景；对不上就给空数组。\n'
                  '只输出 JSON：{"对齐":[{"解说词":<编号>,"场景":[<场景序号>...]},...]}\n'
                  '注意：解说词编号必须使用【原文给出的绝对编号】，不要重新从 0 开始编号。\n\n')
        if movie_name:
            prompt += '电影：%s\n\n' % movie_name
        prompt += '【场景列表】\n' + scene_block + '\n\n【本批解说词】\n' + beat_block
        resp = None
        if use_cloud:
            resp = _cloud_chat_text(prompt, system='你是影视剪辑师，只输出JSON。', timeout=timeout)
        if not resp and _local_ok:
            try:
                resp = _llm_light(prompt, system='你是影视剪辑师，只输出JSON。',
                                  timeout=timeout, max_tokens=max_tokens)
            except Exception as e:
                _log.info('[DIAG] 对齐第 %d-%d 句失败: %s' % (idxs[0], idxs[-1], e))
                resp = None
        al = _parse_alignment_obj(_extract_json_obj(resp or '') or {}, scenes) or {}
        _hit = 0
        for k, v in al.items():
            if not v:
                continue
            if k in idxs:
                base[k] = v
                _hit += 1
            elif 0 <= k < len(idxs):
                base[idxs[k]] = v
                _hit += 1
        if _hit == 0 and _local_ok and not _aborted():
            # 0 命中几乎都不是「模型不会」，而是「本次生成没吐出可用 JSON」：
            # 真机实测这批耗时 14 秒（正常批 4 秒），正是 700 token 用光后断在半句。
            # 重试时把场景清单缩到这批句子大致该在的范围（算法对齐给的位置前后各一段），
            # 提示词从 9700 字降到 2000 字左右，命中率显著提高。
            _log.info('[DIAG] 对齐批次 %d-%d 无命中，缩窗重试（原始返回: %s）'
                      % (idxs[0], idxs[-1], (resp or '')[:110].replace('\n', ' ')))
            _seeds = [min(base[i]) for i in idxs if base.get(i)]
            if _seeds:
                _l2, _h2 = max(0, min(_seeds) - 8), min(len(scene_lines), max(_seeds) + 16)
                _blk2 = '\n'.join(scene_lines[_l2:_h2])
            else:
                _blk2 = scene_block
            _p2 = ('你是影视剪辑师。根据场景描述，把下面这些解说词各自对齐到最相关的场景序号。\n'
                   '要求：按剧情时间顺序；同一句对齐 1~3 个连续场景；对不上就给空数组。\n'
                   '只输出 JSON，不要解释、不要多余文字：'
                   '{"对齐":[{"解说词":<编号>,"场景":[<场景序号>...]}]}\n'
                   '解说词编号用【原文的绝对编号】，不要从 0 重新编号。\n\n')
            if movie_name:
                _p2 += '电影：%s\n\n' % movie_name
            _p2 += '【场景列表】\n' + _blk2 + '\n\n【本批解说词】\n' + beat_block
            try:
                _r2 = _llm_light(_p2, system='你是影视剪辑师，只输出JSON。',
                                 timeout=timeout, max_tokens=1400)
            except Exception as _e:
                _log.info('[DIAG] 对齐重试失败: %s' % _e)
                _r2 = None
            _al2 = _parse_alignment_obj(_extract_json_obj(_r2 or '') or {}, scenes) or {}
            for k, v in _al2.items():
                if not v:
                    continue
                if k in idxs:
                    base[k] = v
                    _hit += 1
                elif 0 <= k < len(idxs):
                    base[idxs[k]] = v
                    _hit += 1
        _log.info('[DIAG] 对齐批次 %d-%d: %d/%d 句命中' % (idxs[0], idxs[-1], _hit, len(idxs)))
        if progress is not None:
            progress['phase'] = '语义对齐（%d/%d 句）' % (idxs[-1] + 1, n)
    return base


def _looks_like_placeholder(text):
    """判断一段文本是不是「提示词里的结构样例」而不是真台词。

    实测模型会把润色提示词里的样例原样返回（『第4段润色后』），
    这类文本一旦进入配音就会被当场念出来。宁可保留原稿，也不能念占位符。
    """
    s = str(text or '').strip()
    if not s:
        return True
    # 整段就是一个「第N段…」的结构样例
    if re.match(r'^第\s*\d+\s*段\s*(?:润色|改写|优化|输出|结果|内容)?\s*(?:后|之后)?\s*$', s):
        return True
    if len(s) <= 24 and '润色后' in s and not re.search(r'[，。！？；：]', s):
        return True
    # 整段被尖括号/方括号/书名号包住（如 <润色结果>、【】）
    if re.match(r'^[<【\[][^<【\[\]>】\]]{0,16}[>】\]]$', s):
        return True
    # 去掉标点/符号后几乎没有实际文字（如「…」「---」）
    # 阈值取 2：只拦空壳，不拦「改写一」这类短句——短句该由长度要求去管，
    # 占位符守卫误杀真台词比漏放更糟。
    core = re.sub(r'[\s\W_]+', '', s, flags=re.UNICODE)
    return len(core) < 2


def _refine_batched(beats, alignment, scenes, scene_story=None, asr=None,
                    movie_name='', batch=4, timeout=150, progress=None,
                    use_cloud=None, max_tokens=900):
    """分批润色：每批 3~5 段，只带该段对齐到的画面描述与台词，输出短、必成功。
    失败的批次保留原稿。返回与 beats 等长的列表（绝不丢段、绝不空段）。"""
    out = list(beats or [])
    n = len(out)
    if n == 0 or not scenes:
        return out
    if use_cloud is None:
        use_cloud = bool(_w.ai_enabled('chat'))
    _local_ok = False
    try:
        _local_ok = bool(_w.local_llm_enabled()) and bool(_w.local_llm_ping()[0])
    except Exception:
        _local_ok = False
    for start in range(0, n, batch):
        if _aborted():
            raise AbortError('用户取消了任务')
        idxs = list(range(start, min(n, start + batch)))
        parts = []
        for i in idxs:
            scs = [scenes[j] for j in (alignment.get(i) or []) if 0 <= j < len(scenes)]
            desc = ' / '.join(_scene_describe(sc) for sc in scs) or '（未对齐到具体画面）'
            line = '解说词%d: %s\n对应画面: %s' % (i, str(beats[i])[:200], desc)
            if asr and scs:
                try:
                    t = ' '.join(_asr_text_in(asr, float(sc.get('start', 0) or 0),
                                              float(sc.get('end', 0) or 0)) for sc in scs).strip()
                except Exception:
                    t = ''
                if t:
                    line += '\n画面台词: ' + t[:160]
            parts.append(line)
        prompt = ('你是影视解说文案编辑。下面每段解说词后面是它在成片里对应的画面描述与台词。\n'
                  '请把每段润色得更贴合画面、更口语、更有画面感：\n'
                  '- 不改变剧情主线与因果关系，不添加画面里没有的情节/人物/数字；\n'
                  '- 保持解说口吻，每段 30~60 字；\n'
                  '- 不许添加猜测词（仿佛、似乎、好像、像是、令人、不禁）；\n'
                  '- 不许把画面台词原文抄进解说词：外语台词用一句中文说清它讲了什么，'
                  '旁白里不出现英文单词、无线电呼号；\n'
                  '- 输出顺序必须与输入一一对应。\n'
                  # 【坑】这里绝不能写「第%d段润色后」这类字面占位——实测会被模型
                  # 原样当成台词返回，成片里会念出「第4段润色后」。只描述结构，不给样例文本。
                  '只输出 JSON 对象：键名固定为 "润色"，值是一个字符串数组，'
                  '长度必须正好 %d；数组第 i 个元素是第 i 段润色后的完整解说词，'
                  '必须是真的台词内容，不能是「第N段」这类占位文字。\n\n'
                  % len(idxs))
        if movie_name:
            prompt += '电影：%s\n\n' % movie_name
        prompt += '\n\n'.join(parts)
        resp = None
        if use_cloud:
            resp = _cloud_chat_text(prompt, system='你是影视解说文案编辑，只输出JSON。', timeout=timeout)
        if not resp and _local_ok:
            try:
                resp = _llm_light(prompt, system='你是影视解说文案编辑，只输出JSON。',
                                  timeout=timeout, max_tokens=max_tokens)
            except Exception as e:
                _log.info('[DIAG] 润色第 %d-%d 段失败: %s' % (idxs[0], idxs[-1], e))
                resp = None
        obj = _extract_json_obj(resp or '') or {}
        lines_raw = obj.get('润色') or obj.get('refined') or []
        if isinstance(lines_raw, list) and len(lines_raw) == len(idxs):
            _ok_n = 0
            for k, i in enumerate(idxs):
                t = str(lines_raw[k] or '').strip()
                # 占位符拒收：模型偶尔把提示词里的结构样例当台词返回
                # （实测产出过「第4段润色后」，会被直接念出来）。这类结果一律丢弃，
                # 保留该段原稿——少一点润色远好过念一段占位符。
                if t and not _looks_like_placeholder(t):
                    out[i] = t
                    _ok_n += 1
            _log.info('[DIAG] 润色批次 %d-%d: %d/%d 段成功' % (idxs[0], idxs[-1], _ok_n, len(idxs)))
        else:
            _log.info('[DIAG] 润色批次 %d-%d 失败/长度不符，保留原稿' % (idxs[0], idxs[-1]))
        if progress is not None:
            progress['phase'] = '润色（%d/%d 段）' % (idxs[-1] + 1, n)
    return out


def _score_segments(segs, scene_descs, narr_map, asr=None, scene_alignment=None):
    """为每个视频段打分：场景对齐权重×事件信息量×台词量。返回 [(score, seg), ...]。
    用于 clipRatio 智能剪辑：铺设率过高时优先保留信息量最大的段。
    scene_alignment（LLM语义对齐结果）是最强信号：对齐的片段说明LLM认为
    该画面和解说词匹配，必须优先保留。"""
    # 预计算对齐的场景索引集合
    aligned_scenes = set()
    if scene_alignment:
        for sc_idxs in scene_alignment.values():
            if isinstance(sc_idxs, (list, tuple, set)):
                aligned_scenes.update(int(s) for s in sc_idxs)
    scored = []
    for i, (a, b) in enumerate(segs):
        s = 0.0
        nm = narr_map[i] if i < len(narr_map) else i
        # 0) LLM场景对齐：最强信号，对齐的片段大幅加分
        if nm in aligned_scenes:
            s += 50
        # 1) 画面信息量
        desc = scene_descs[nm] if nm < len(scene_descs) else {}
        if isinstance(desc, dict):
            event = str(desc.get('event', ''))
            chars = str(desc.get('characters', ''))
            summ = str(desc.get('summary', ''))
            s += len(event) * 0.8 + len(chars) * 0.3 + len(summ) * 0.2
            if event:
                s += 10
        # 2) 时长适中偏好
        dur = b - a
        if 3 <= dur <= 15:
            s += 5
        elif dur > 20:
            s -= 3
        # 3) 有台词加分
        if asr:
            txt = _asr_text_in(asr, a, b)
            if txt:
                s += len(txt) * 0.1
        scored.append((s, (a, b)))
    return scored

def _imp_rank(imp):
    """剧情重要度排序：key(3) > advance(2) > transition(0)，复用 _IMP_ORDER。"""
    return _IMP_ORDER.get(str(imp or 'advance'), 2)


def _decide_keep_beats(beat_imp, beat_dur, target_sec, vdur):
    """【剧情驱动·留/剪决策】给定每节拍重要度与时长，按目标时长决定保留哪些节拍。

    返回 set(beat_index)。规则：
      - 目标时长≈全片（或无效）→ 全保留（与旧默认行为一致，不破坏现有成片）；
      - 否则进入「精剪」：按 key>advance>transition 保高光、丢填充，凑到目标时长；
        同重要度内短节拍优先（保更多样），至少留一半节拍防剪太狠。
    纯函数，不触碰任何模型/IO，便于单测。"""
    if not beat_imp:
        return set()
    if not (0 < (target_sec or 0) < 0.98 * vdur):
        return set(range(len(beat_imp)))
    _budget = float(target_sec)
    _order = sorted(beat_dur.keys(),
                    key=lambda j: (_imp_rank(beat_imp[j] if 0 <= j < len(beat_imp) else 'advance'),
                                   -beat_dur[j]),
                    reverse=True)
    _kept = set()
    _acc = 0.0
    # 保底：至少留 key 关键节拍 + 少量（约 35%），避免目标过短把故事剪得支离破碎；
    # 但不过度（旧版 50% 会让「约 1 分钟」实际变成 3~4 分钟，违背目标时长本意）。
    _min = max(2, int(0.35 * len(beat_dur)))
    for _j in _order:
        _imp = beat_imp[_j] if 0 <= _j < len(beat_imp) else 'advance'
        if _acc >= _budget and _imp_rank(_imp) < 3:
            if _imp_rank(_imp) == 2 and _acc < _budget * 1.1:
                _kept.add(_j); _acc += beat_dur[_j]; continue
            break
        _kept.add(_j); _acc += beat_dur[_j]
    if len(_kept) < _min:
        for _j in _order:
            if _j not in _kept:
                _kept.add(_j)
                if len(_kept) >= _min:
                    break
    return _kept


def _allocate_script_spans(texts, vdur, asr=None, cps=None, min_dur=1.0, vlm_captions=None, scene_alignment=None, scenes=None, scene_story=None, sem_weight=1.2):
    """按解说词字数分配画面区间 ——「解说驱动剪辑」的核心。

    返回 (spans, narr_map)：
    - spans: 扁平的子片段列表 [(start, end), ...]，每节解说词拆成多个 ~5s 短片段
    - narr_map: narr_map[k] = 第 k 个子片段属于第几节解说词（与 texts 索引对应）

    高密度剪辑：一节解说词不再只用一个长片段代表，而是从该节时间窗口内裁出多个
    短片段拼接，避免"第一幕只用一个片段一带而过"的问题。
    """
    cps = float(cps or NAR_SCRIPT_CPS)
    n = len(texts or [])
    if n == 0 or vdur <= 0:
        return [], []
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

    dens, step = _asr_density(asr, vdur)
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
    scene_texts = []
    if scenes and sem_weight:
        for _si, _sc in enumerate(scenes or []):
            try:
                _st = float(_sc.get('start', 0) or 0)
                _en = float(_sc.get('end', 0) or 0)
            except Exception:
                continue
            if _en <= _st:
                continue
            _sto = (scene_story or {}).get(_si) or (scene_story or {}).get(str(_si)) or {}
            _parts = [str(_sc.get('summary') or ''), str(_sc.get('event') or '')]
            _kws = _sto.get('keywords') or []
            if _kws:
                _parts.append(''.join(str(k) for k in _kws))
            if _sto.get('story'):
                _parts.append(str(_sto['story']))
            scene_texts.append((_st, _en, ''.join(_parts)))
        scene_texts.sort(key=lambda x: x[0])

    def _scene_match(beat_text, t0, t1):
        """候选画面范围与解说句的语义重合度（覆盖场景的 bigram 重叠，0~1）。"""
        if not scene_texts or not beat_text:
            return 0.0
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
    last_end = 0.0

    for i, d in enumerate(durs):
        # 每节拆成多个子片段：节时长 / 目标片段时长，至少 1 个
        n_sub = max(1, int(round(d / NAR_SEG_TARGET)))
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
            lo = min(max(lo, 0.0), max(0.0, vdur - sub_d))
            hi = min(vdur - sub_d, lo + sub_d * 2)
            if hi < lo:
                hi = min(vdur - sub_d, lo + sub_d)

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
            best_t, best_s = sub_lo, -1.0
            beat_txt = str(texts[i] or '') if i < len(texts) else ''
            t = sub_lo
            while t <= sub_hi + 1e-6:
                t1 = min(vdur, t + sub_d)
                density = _score(t, t1)
                t_match = _text_match(beat_txt, t, t1)
                # 语义精修：窗口内的候选起点，优先落在「场景内容贴合这句解说」的
                # 位置（additive 精修项，不压制台词密度主信号；旧版把 VLM 字符
                # 重叠权重设 0 是因为裸字符重叠是噪声——现在用场景 summary/
                # keywords 的 bigram，且只在 DP 已匹配的窗口内做精修）
                v_match = _scene_match(beat_txt, t, t1)
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


def _fallback_full_script(movie_name, plot_text, target_sec=None):
    """离线兜底：把剧情文本按句切分并合并成 40-90 字的解说节。
    不再硬截断到 30 字——宁可保留整句，也不要「随后追」这种半截话。"""
    import re as _re
    raw = []
    for l in _re.split(r'[\n。！？!?]', plot_text or ''):
        l = l.strip()
        if len(l) <= 4:
            continue
        l = _re.sub(r'^(?:第?\d+[\.、)．:：]|\[\d+\]|（\d+）)\s*', '', l).strip()
        if len(l) > 4:
            raw.append(l)
    if not raw:
        return None
    # 短句合并到 NAR_BEAT_MIN 以上、不超过 NAR_BEAT_MAX
    merged, buf = [], ''
    for s in raw:
        if not buf:
            buf = s
        elif len(buf) + len(s) + 1 <= NAR_BEAT_MAX:
            buf = buf + '，' + s
        else:
            merged.append(buf)
            buf = s
    if buf:
        merged.append(buf)
    # 过长的单句按逗号再切一刀（避免整段 300 字撑爆一个镜头）
    final = []
    for m in merged:
        while len(m) > NAR_BEAT_MAX:
            cut = m.rfind('，', 0, NAR_BEAT_MAX)
            cut = cut if cut > NAR_BEAT_MIN // 2 else NAR_BEAT_MAX
            final.append(m[:cut].strip('，'))
            m = m[cut:].strip('，')
        if m:
            final.append(m)
    if not final:
        return None
    name = (movie_name or '').strip()
    hook = ('今天要讲的这部电影是《%s》。' % name) if name else ''
    return {'title': name,
            'hook': hook,
            'beats': [{'text': t, 'keywords': [], 'importance': 'advance'} for t in final],
            'outro': ''}


def _strip_md(text):
    """去掉 Markdown 装饰（**加粗**、#标题、> 引用、`代码`），保留正文。"""
    s = str(text or '')
    s = re.sub(r'\*\*(.+?)\*\*', r'\1', s)
    s = re.sub(r'__(.+?)__', r'\1', s)
    s = re.sub(r'(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])', r'\1', s)
    s = re.sub(r'`(.+?)`', r'\1', s)
    s = re.sub(r'^\s{0,3}#{1,6}\s*', '', s, flags=re.M)
    s = re.sub(r'^\s{0,3}>\s?', '', s, flags=re.M)
    s = re.sub(r'^\s*[-*+]\s+', '', s, flags=re.M)
    s = re.sub(r'\[(.*?)\]\((.*?)\)', r'\1', s)
    return s


_SENT_PAIRS = {'“': '”', '‘': '’', '「': '」', '『': '』', '（': '）', '(': ')'}


def _split_sentences(line):
    """按句末标点切句，但不切开引号里的内容（对话被劈成两半，配音会很怪）。"""
    out, buf, quote = [], [], ''
    for ch in str(line or ''):
        if quote:
            buf.append(ch)
            if ch == _SENT_PAIRS.get(quote):
                quote = ''
            continue
        if ch in _SENT_PAIRS:
            quote = ch
            buf.append(ch)
            continue
        buf.append(ch)
        if ch in '。！？!?…':
            out.append(''.join(buf).strip())
            buf = []
    if ''.join(buf).strip():
        out.append(''.join(buf).strip())
    return [x for x in out if x]


def _script_to_texts(raw, min_len=18, max_len=78):
    """把用户提供的解说稿切成适合配音/字幕的句子序列。

    解说稿是人写的：有换行、有 Markdown 加粗、有长句、有对白。配音引擎需要的是
    长度均匀的句子（太短会一顿一顿，太长一条字幕塞不下）。规则：
      1. 去 Markdown，统一标点，按 。！？…换行 切句；引号内的句子不切开；
      2. 过短的句子并到下一句，过长的句子在 ，、 处再切；
      3. 只做切分，不改一个字——用户的稿子按原样念。
    """
    s = _strip_md(raw)
    s = re.sub(r'[ \t\u3000]+', ' ', s)
    segs = []
    for line in s.split('\n'):
        line = line.strip()
        if not line:
            continue
        segs.extend(_split_sentences(line))
    # 过长再切：在逗号处贪心合并
    out = []
    for p in segs:
        if len(p) <= max_len:
            out.append(p)
            continue
        buf = ''
        for piece in re.findall(r'[^，,；;：:]+[，,；;：:]*', p):
            if not piece:
                continue
            if buf and len(buf) + len(piece) > max_len:
                out.append(buf)
                buf = piece
            else:
                buf += piece
        if buf:
            out.append(buf)
    # 过短合并
    merged = []
    for p in out:
        if merged and len(merged[-1]) < min_len:
            merged[-1] = merged[-1] + p
        else:
            merged.append(p)
    if len(merged) >= 2 and len(merged[-1]) < min_len:
        merged[-2] = merged[-2] + merged[-1]
        merged.pop()
    return [x for x in merged if x.strip()]


def _fallback_hook(material, beats=None):
    """钩子生成失败时的兜底：取「全片主线」的第一句当开场。

    【为什么不写模板句】「这部片一开场就…」这类模板对任何片子都成立，等于没有钩子，
    还会让观众听出是机器拼的。主线第一句本身就是全片悬念的浓缩，直接拿来更实在；
    连主线都没有就返回空——宁可从正片第一句起，也不要一句假钩子。
    """
    m = re.search(r'【全片主线】\s*\n?(.+)', str(material or ''))
    if m:
        s = re.split(r'[。！？\n]', m.group(1).strip())[0].strip()
        if 12 <= len(s) <= 60:
            return s + '。'
    return ''


def _llm_script_sectioned(movie_name, plot_text, n_beats, material=None, style='movie',
                          batch=6):
    """分批写解说稿：① 出纲要 → ② 按批写正文 → ③ 补开场钩子与结尾。失败返回 None。

    【为什么必须分批 —— 实测依据（RTX 3060 Laptop 12GB + qwen3:8b）】
      要求一次写 60 节时，模型写到 2956 字就被 num_predict=1500 截断，JSON 不闭合，
      _extract_json_obj 解析失败 → 三层兜底一路降到「把剧情原文切句」。
      也就是说：**长稿从来没有真正写成过**，用户看到的「解说」一直是剧情原文拼接，
      没有钩子、没有因果推进、没有悬念——这正是「不像市面解说」的根因。
    分批之后每次只输出 6 节的短 JSON（约 700 字），必可解析；某批失败只少这几节。
    """
    src = (material or plot_text or '').strip()
    if not src:
        return None
    n_beats = max(4, int(n_beats))

    # ① 纲要：按时间顺序把素材拆成「谁+做了什么+结果」的推进点
    out_prompt = (
        '你是影视解说编剧。下面是这部电影的【故事素材】（人物、主线、分幕剧情），'
        '请按剧情时间顺序拆出「推进点」，供后面逐节写解说词。\n'
        '要求：\n'
        '- 每个推进点单独一行，18-35 字，写成「谁 + 做了什么 + 为什么/结果」，'
        '要讲剧情事件和因果，不要写「气氛紧张」「仿佛藏着秘密」这类空话。\n'
        '- 一条推进点可以覆盖分幕剧情里的多段内容：把连续发生的同一件事合并成一条，'
        '不要一段素材写一条（那样稿子会变成流水账）。\n'
        '- 按「起因 → 发展 → 转折 → 高潮 → 结局」的顺序推进，后一条要能接上前一条'
        '（谁因为什么做了什么，导致下一步）。\n'
        '- 同一件事在素材里出现多次，只留一条。\n'
        '- 禁止描述镜头本身：不要出现「画面中」「镜头里」「我们看到」。\n'
        '- 禁止用猜测词补空：仿佛、似乎、好像、像是、令人、让人、不禁。\n'
        '- 只写素材里确实有的内容，不得编造人物、情节、结局；也不要把英文台词、'
        '无线电代码当成情节点。\n'
        '- 素材能支撑几点就写几点，最多 %d 点；不够就少写，绝不凑数。\n'
        '- 不要开场白、不要结尾升华、不要编号或项目符号。\n\n'
        '【故事素材】\n%s\n' % (n_beats, src[:7000]))
    try:
        raw = _llm_light(out_prompt, '你是影视剧情拆解师，只输出推进点清单。',
                         timeout=180, max_tokens=1200)
    except Exception:
        return None
    points = []
    for line in str(raw or '').splitlines():
        t = re.sub(r'^\s*[\-\*\d\.、\)）]+\s*', '', line).strip()
        if t and len(t) >= 4:
            points.append(t[:40])
    if len(points) < 3:
        return None
    points = points[:n_beats]

    # ② 分批写正文：每批 6 节，带前情，顺序锁定在纲要上
    beats = []
    for bi in range(0, len(points), batch):
        if _aborted():
            raise AbortError('用户取消了任务')
        chunk = points[bi:bi + batch]
        numbering = '\n'.join('%d. %s' % (bi + k + 1, p) for k, p in enumerate(chunk))
        prev = ''
        if beats:
            # 前情给最近 2 节：只给 1 节时模型接不上因果，容易每节各写各的
            _tail2 = [str(b.get('text') or '').strip() for b in beats[-2:]]
            prev = '【前情】前面已经讲到：%s\n' % '；'.join(t[:80] for t in _tail2 if t)
        prompt = (
            '你在写一部影视解说稿的第 %d-%d 节（全稿共 %d 节）。\n'
            '这是给观众口播的**剧情解说**：观众要听的是「这部片讲了什么事」，'
            '不是「画面上有什么」。\n\n'
            '【本节要讲的剧情推进点，必须按顺序一一对应】\n%s\n\n'
            '%s'
            '【写法】\n'
            '- 每节 40-90 字，像给朋友讲这部片：谁、做了什么、为什么、结果怎样。\n'
            '- 第一节要顺着前情往下讲，用因果词接住（所以、于是、可就在这时、结果）。'
            '整篇读下来必须是一条连贯的故事，不是一段一段的画面说明。\n'
            '- 可以有解说员的态度和判断，但不能编造情节。\n'
            '- 每节末尾留一点钩子，让人想继续看。\n\n'
            '【硬约束，违反就整篇作废】\n'
            '- 禁止写成动作流水账：不许一句接一句地写「他走进…」「他拿起…」「他看向…」'
            '「他站在…」这类纯动作句。讲事，不讲动作。\n'
            '- 每节只能写上面那个推进点里已经写明的人物和动作，不得新增任何情节。\n'
            '- 不许替角色编动机：不写「为了…」「决定…」「企图…」这类推出来的意图，'
            '也不许把「暗示/可能/疑似」当成事实。素材只写了行为，你就只写行为。\n'
            '- 严禁写素材里没有的：角色死亡、重伤不治、临终遗言、婚姻结局、身世秘密、追悼会、'
            '「后来怎样」的结论。素材停在哪儿，稿子就停在哪儿。\n'
            '- 不许用猜测词：仿佛、似乎、好像、像是、令人、让人、不禁、或许。'
            '只看得出「他站在那儿」，就写他站在那儿，不要替他补心情和动机。\n'
            '- 不许照抄台词：旁白是转述，不是念台词。禁止出现英文单词、无线电呼号、'
            '警用电台代码；台词是外语时，用一句中文说清它讲了什么。\n'
            '- 同一件事只讲一次：素材里同一件事被多次描述，合并进第一次出现的那一节。\n'
            '- 不许出现「画面」「镜头」「我们看到」「观众」「这一幕」。\n'
            '- 配音标记每节最多 1 处，写成 {情绪:紧张}要念出来的那句话{/情绪}；'
            '标签里必须是台词本身，不能只放省略号或占位符。整批可以完全不用标记。\n'
            '- importance 大致 key 三成、advance 五成、transition 两成，不要全标 key。\n\n'
            '只输出 JSON：{"beats":[{"text":"解说词","keywords":["2-3个关键词"],'
            '"importance":"key|advance|transition"}]}\n'
            '本批必须正好 %d 节。' % (bi + 1, bi + len(chunk), len(points), numbering,
                                    prev, len(chunk)))
        try:
            resp = _llm_light(prompt, '你是资深影视解说博主，只输出 JSON。',
                              timeout=240, max_tokens=1500)
            obj = _extract_json_obj(resp)
        except AbortError:
            raise
        except Exception:
            obj = None
        got = _script_from_obj(obj, movie_name) if obj else None
        if not got or not got.get('beats'):
            # 某一批失败：跳过，不硬凑（后续节数变少，但整篇仍可用）
            _log.info('[DIAG] 写稿第 %d-%d 节失败，跳过' % (bi + 1, bi + len(chunk)))
            continue
        beats.extend(got['beats'][:len(chunk)])
    if not beats:
        return None

    # 写完之后再清洗一遍（模型只能减弱这几种毛病，不能消除）：
    #   去英文台词/无线电代码 → 去「仿佛/似乎」类猜测定语 → 同事件去重
    n_before = len(beats)
    beats = _beats_scrub(beats)
    if not beats:
        return None
    if len(beats) != n_before:
        _log.info('[DIAG] 稿件清洗: %d 节 → %d 节（去重复/去猜测定语/去英文台词）'
                  % (n_before, len(beats)))

    # ③ 开场钩子与结尾：短调用，允许失败
    hook, outro, title = '', '', (movie_name or '')
    for _try in range(2):        # 实测有一次直接返回空（模型抽风），补一次就够
        try:
            head = ' '.join(beats[0].get('text', '') for _ in [0])[:200]
            tail = (beats[-1].get('text') or '')[:200]
            hp = ('这是一部影视解说稿的开头和结尾，正文已经写好。请补写开场和结尾。\n\n'
                  '【片名】%s\n【正文第一段】%s\n【正文最后一段】%s\n\n'
                  '要求：\n'
                  '- hook：1-2 句，抛出全片最抓人的悬念或反差，不要剧透结局，不要照抄示例。\n'
                  '- outro：1-2 句，点题或升华，给观众回味。\n'
                  '- 只用上面出现过的信息，不得编造。\n'
                  '- 不要写「画面里」「镜头中」「我们看到」。\n\n'
                  '只输出 JSON：{"title":"片名","hook":"开场白","outro":"结尾"}'
                  % (movie_name or '（未提供）', head, tail))
            hobj = _extract_json_obj(_llm_light(hp, '你是资深影视解说博主，只输出 JSON。',
                                                timeout=180, max_tokens=600))
            if hobj:
                hook = str(hobj.get('hook') or '').strip()
                outro = str(hobj.get('outro') or '').strip()
                title = str(hobj.get('title') or title).strip()
            if hook:
                break
        except AbortError:
            raise
        except Exception:
            continue

    # 兜底：钩子拿不到时不能空着（市面解说第一句就是钩子），用主线/首节拼一句
    if not hook:
        hook = _fallback_hook(material or plot_text, beats)
    if not outro and beats:
        outro = ''        # 结尾宁缺勿滥：没有就不加，避免生成空话

    # 开场/结尾同样过一遍清洗（英文台词、猜测定语在 hook 里更刺眼）
    hook = _drop_speculation(_strip_foreign(hook)) if hook else hook
    outro = _drop_speculation(_strip_foreign(outro)) if outro else outro

    _log.info('[DIAG] 分批写稿完成: 纲要%d点 → 正文%d节 + hook%d字/outro%d字'
              % (len(points), len(beats), len(hook), len(outro)))
    return {'title': title, 'hook': hook, 'beats': beats, 'outro': outro}


def _script_material(plot, scene_story=None, scenes=None, max_chars=6500,
                     acts=None, storyline=None):
    """拼写稿素材。**主体是故事，不是画面。**

    【为什么改】旧版把 110 条逐窗画面含义（聚合到 40 条）当素材主体，写稿出来的
    必然是镜头清单：「他走进走廊 / 他拿起篮子 / 他站在门口」——素材本身就是按镜头
    排的，模型只是把它抄成句子。解说要讲的是剧情，所以素材主体换成
    「人物表 + 主线 + 分幕剧情」，画面信息只保留时间码，用来定位。
    """
    parts = []
    plot_txt = str(plot or '').strip()
    if plot_txt:
        parts.append('【剧情资料（用户提供，以此为准）】\n' + plot_txt)
    if storyline and (storyline.get('mainline') or storyline.get('characters')):
        if storyline.get('characters'):
            parts.append('【人物】\n' + storyline['characters'])
        if storyline.get('mainline'):
            parts.append('【全片主线】\n' + storyline['mainline'])
    if acts:
        lines = ['%02d. [%s-%s] %s' % (i + 1, _fmt_tc(a['start']), _fmt_tc(a['end']),
                                       a['summary'])
                 for i, a in enumerate(acts)]
        parts.append('【分幕剧情（按时间顺序，括号是这一幕在片中的时间）】\n' + '\n'.join(lines))
    if parts:
        return '\n\n'.join(parts)[:max_chars]

    # 兜底：拿不到分幕/故事线时，退回旧的逐场画面理解（宁可写得像镜头清单，也不能没素材）
    lines = []
    if scene_story and scenes:
        items = []
        for i in sorted(scene_story.keys()):
            try:
                idx = int(i)
            except (TypeError, ValueError):
                continue
            st = scene_story[i] or {}
            story = str(st.get('story') or '').strip()
            if not story:
                continue
            sc = scenes[idx] if 0 <= idx < len(scenes) else {}
            try:
                t = int(float(sc.get('start') or 0))
            except (TypeError, ValueError):
                t = 0
            items.append((t, story, str(st.get('who') or '').strip(),
                          str(st.get('act') or '').strip()))
        if items:
            step = max(1, int(math.ceil(len(items) / 40.0)))
            for k in range(0, len(items), step):
                group = items[k:k + step]
                t = group[0][0]
                stories = []
                for _t, _s, _w, _a in group:
                    if _s not in stories:
                        stories.append(_s)
                line = '[%02d:%02d] %s' % (t // 60, t % 60, '；'.join(stories[:3]))
                if group[0][2]:
                    line += '（%s）' % group[0][2]
                lines.append(line)
    if lines:
        parts.append('【画面逐场理解（按时间顺序，来自全片画面分析）】\n' + '\n'.join(lines))
    return '\n\n'.join(parts)[:max_chars]



def llm_movie_full_script(movie_name, plot_text, economy=False, target_sec=None, style='movie',
                          material=None):
    """生成完整影视解说稿（解说驱动剪辑的起点）。

    返回 {title, hook, beats:[{text, keywords, importance}], outro}；三层兜底保证绝不空返回：
      云端/本地 LLM → 剧情切句合并 → 片名模板。
    target_sec：期望成片时长，用来估算该写多少节（节数 = 时长×语速÷每节字数）。
    material：优先使用的素材文本（通常是「剧情资料 + 逐场画面剧情要点」）。
      给定时优先于 plot_text——写成什么样，取决于素材有多具体，而不是片名。"""
    n_beats = None
    if target_sec:
        try:
            total_chars = float(target_sec) * NAR_SCRIPT_CPS
            n_beats = max(4, min(60, int(round(total_chars / ((NAR_BEAT_MIN + NAR_BEAT_MAX) / 2.0)))))
        except Exception:
            n_beats = None
    want = ('\n\n这一版请写成约 %d 节。' % n_beats) if n_beats else ''
    # 严格约束：不添加剧情资料里没有的情节、人物、台词、数字
    strict = ('\n\n【严格约束】\n'
              '- 只使用上面【片名与剧情资料】里出现过的内容，严禁编造未提及的人物、情节、台词、数字、结局。\n'
              '- 资料里没有的细节宁可跳过，也不要自行脑补或拓展。\n'
              '- 可以调整叙述顺序和表达方式，但不能改变事实。\n'
              '- 如果资料不足，少写几节也没关系，不要凑字数。')

    # ① 本地模型（免费优先）
    if _w.local_llm_enabled() and not economy:
        try:
            if _w.local_llm_ping()[0]:
                # 【质量模式】长稿必须分批写：
                #   实测（3060 12GB + qwen3:8b）要求 60 节时，模型输出 2956 字就被
                #   num_predict=1500 截断 → JSON 不闭合 → 解析失败 → 一路降级到
                #   「剧情原文切句」兜底。这正是「解说不像解说」的根因：从没写成功过。
                #   改法：先出纲要（短输出、必成功），再按批写正文（每批 6 节），
                #   每批输出短且可独立解析，任一批失败只少几节，不会整篇报废。
                if n_beats and n_beats >= 12:
                    sc = _llm_script_sectioned(movie_name, plot_text, n_beats,
                                               material=material, style=style)
                    if sc and sc.get('beats'):
                        return sc
                brief = ((movie_name or '') + '\n' + (material or plot_text or ''))[:8000]
                prompt = SCRIPT_STYLE_MOVIE + want + strict + '\n\n【片名与剧情资料】\n' + brief
                # 输出预算按节数给足：每节约 150 token（含标记/关键词/JSON 结构）
                _budget = 1500 if not n_beats else min(6000, max(1500, int(n_beats * 150) + 600))
                obj = _extract_json_obj(local_llm_chat(prompt, timeout=300, max_tokens=_budget))
                sc = _script_from_obj(obj, movie_name)
                if sc and sc['beats']:
                    sc['beats'] = _beats_scrub(sc['beats'])
                    if sc['beats']:
                        return sc
        except Exception:
            pass
    # ② 云端 chat
    if _w.ai_enabled('chat') and not economy:
        try:
            import urllib.request
            import json as _json
            brief = ((movie_name or '') + '\n' + (material or plot_text or ''))[:8000]
            prompt = SCRIPT_STYLE_MOVIE + want + strict + '\n\n【片名与剧情资料】\n' + brief
            cfg = _w.chat_cfg()
            payload = {'model': cfg.get('model'),
                       'messages': [{'role': 'user', 'content': prompt}],
                       'max_tokens': 3000, 'temperature': 0.75}
            url = (cfg.get('base_url', '').rstrip('/')) + '/chat/completions'
            req = urllib.request.Request(
                url, data=_json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json',
                         'Authorization': 'Bearer ' + cfg.get('api_key', '')})
            with urllib.request.urlopen(req, timeout=180) as r:
                data = _json.loads(r.read().decode('utf-8'))
            content = data['choices'][0]['message']['content']
            sc = _script_from_obj(_extract_json_obj(content), movie_name)
            if sc and sc['beats']:
                sc['beats'] = _beats_scrub(sc['beats'])
                if sc['beats']:
                    return sc
        except Exception:
            pass
    # ③ 离线兜底：剧情切句（整句保留，不再 30 字截断）
    sc = _fallback_full_script(movie_name, plot_text, target_sec)
    if sc:
        return sc
    # ④ 片名模板：保证任何情况下都有稿
    name = (movie_name or '').strip()
    tpl = [
        '故事从一场不寻常的相遇悄然展开。',
        '主角登场，命运的齿轮开始转动。',
        '平静之下暗流涌动，冲突一触即发。',
        '转折来临，局面陡然扑朔迷离。',
        '真相浮出水面，结局出人意料。',
    ]
    return {'title': name,
            'hook': ('今天要讲的这部电影是《%s》。' % name) if name else '',
            'beats': [{'text': t, 'keywords': [], 'importance': 'advance'} for t in tpl],
            'outro': ''}


def llm_movie_script(movie_name, plot_text, economy=False):
    """根据片名 + 剧情文本，让 LLM 产出结构化解说事件列表 [{desc, keywords}]。
    兜底优先级（保证离线/断网也能出稿，绝不因缺剧情而空返回）：
      剧情切句 → 本地模型(若可用) → 片名模板。"""
    import re as _re
    def _split_sentences(text):
        """把剧情文本按句拆成解说事件；去掉「1. 2.」等分幕编号前缀，避免解说词带序号。"""
        lines = []
        for l in _re.split(r'[\n。！？!?]', text or ''):
            l = l.strip()
            if len(l) <= 4:
                continue
            l = _re.sub(r'^(?:第?\d+[\.、)．:：]|\[\d+\]|（\d+）)\s*', '', l).strip()
            if len(l) <= 4:
                continue
            lines.append({'desc': l[:30], 'keywords': list(set(l))[:5]})
        return lines[:12]

    def _parse_events(content):
        """从 LLM 文本中提取并规范化 JSON 解说事件数组；失败返回 []。"""
        import json as _json
        s = content.find('['); e = content.rfind(']')
        if s < 0 or e <= s:
            return []
        try:
            arr = _json.loads(content[s:e + 1])
        except Exception:
            return []
        out = []
        for it in arr:
            if isinstance(it, dict) and it.get('desc'):
                kw = it.get('keywords') or []
                if isinstance(kw, str):
                    kw = [kw]
                out.append({'desc': str(it['desc'])[:40], 'keywords': [str(k) for k in kw][:6]})
        return out

    def _local_script(name, plot):
        """本地模型(Ollama 等)离线生成解说事件：断网也能用，失败返回 []。"""
        if not _w.local_llm_enabled():
            return []
        try:
            if not _w.local_llm_ping()[0]:
                return []
        except Exception:
            return []
        brief = ((name or '') + '\n' + (plot or ''))[:4000]
        prompt = ('你是电影/动漫解说编剧。下面是一部作品的片名'
                  + ('与剧情梗概。' if plot else '（没有剧情资料，请根据片名合理发挥）。') + '\n'
                  '请输出一个 JSON 数组，每条解说事件形如 {"desc":"一句≤30字中文解说词",'
                  '"keywords":["关键词1","关键词2"]}，按时间顺序覆盖主要情节，共 6-12 条。'
                  '只输出 JSON，不要解释、不要代码块标记。\n\n' + brief)
        try:
            return _parse_events(local_llm_chat(prompt, timeout=90))
        except Exception:
            return []

    def _template_script(name):
        """最终兜底：基于片名生成通用解说事件（0 元、0 依赖，保证断网也能出稿）。"""
        name = (name or '').strip()
        if name:
            lines = [
                '今天要讲的这部电影是《%s》。' % name,
                '故事从一场不寻常的相遇悄然展开。',
                '主角登场，命运的齿轮开始转动。',
                '平静之下暗流涌动，冲突一触即发。',
                '转折来临，局面陡然扑朔迷离。',
                '悬念层层叠加，让人屏住呼吸。',
                '高潮将至，所有线索开始交汇。',
                '真相浮出水面，结局出人意料。',
            ]
        else:
            lines = [
                '今天要讲的这部电影，故事从一场不寻常的相遇悄然展开。',
                '主角登场，命运的齿轮开始转动。',
                '平静之下暗流涌动，冲突一触即发。',
                '转折来临，局面陡然扑朔迷离。',
                '悬念层层叠加，让人屏住呼吸。',
                '高潮将至，所有线索开始交汇。',
                '真相浮出水面，结局出人意料。',
            ]
        return [{'desc': l[:30], 'keywords': list(set(l))[:5]} for l in lines]

    # 省流模式：直接离线切句，不调任何付费接口
    if economy or not _w.ai_enabled('chat'):
        ev = _split_sentences(plot_text)
        if ev:
            return ev
        # 无剧情文本时：优先本地模型(若有) → 片名模板，保证不空
        return _local_script(movie_name, plot_text) or _template_script(movie_name)
    brief = ((movie_name or '') + '\n' + (plot_text or ''))[:4000]
    prompt = ('你是电影/动漫解说编剧。下面是一部作品的片名与剧情梗概。\n'
              '请输出一个 JSON 数组，每条解说事件形如 {"desc":"一句≤30字中文解说词",'
              '"keywords":["关键词1","关键词2"]}，按时间顺序覆盖主要情节，共 6-12 条。'
              '只输出 JSON，不要解释、不要代码块标记。\n\n' + brief)
    try:
        import urllib.request, json as _json
        cfg = _w.chat_cfg()
        payload = {'model': cfg.get('model'),
                   'messages': [{'role': 'user', 'content': prompt}],
                   'max_tokens': 1500, 'temperature': 0.7}
        url = (cfg.get('base_url', '').rstrip('/')) + '/chat/completions'
        req = urllib.request.Request(url, data=_json.dumps(payload).encode('utf-8'),
                                     headers={'Content-Type': 'application/json',
                                              'Authorization': 'Bearer ' + cfg.get('api_key', '')})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = _json.loads(resp.read().decode('utf-8'))
        ev = _parse_events(data['choices'][0]['message'].get('content', ''))
        if ev:
            return ev
    except Exception:
        pass
    # 兜底：剧情切句 → 本地模型 → 片名模板（保证离线也能出稿）
    ev = _split_sentences(plot_text)
    if ev:
        return ev
    return _local_script(movie_name, plot_text) or _template_script(movie_name)


def align_script_to_segments(events, segs, asr):
    """把解说事件对齐到电影镜头段：时序保持的加权匹配（核心技术难点）。
    - 事件按剧情顺序、镜头段按时间顺序，单调分配：一旦事件 k 命中段 i，事件 k+1 只在段 > i 中找，
      避免「第3段解说词错配到更早画面」的乱序问题。
    - 匹配分 = 台词字符 bigram Jaccard；未命中阈值的事件按顺序补到空闲段，保证全覆盖。
    返回 [(start, desc)]（按 start 升序）。"""
    seg_text = []
    for (s0, s1) in segs:
        txt = ' '.join(x['text'] for x in asr if x['start'] >= s0 - 0.5 and x['end'] <= s1 + 0.5)
        seg_text.append(txt)
    seg_bg = [_char_bigrams(t) for t in seg_text]

    def _score(qbg, bg):
        if not qbg or not bg:
            return 0.0
        return len(qbg & bg) / (len(qbg | bg) or 1)

    assigned = []          # [(seg_index, desc)]
    used = set()
    seg_ptr = 0            # 单调指针：后续事件只在 seg_ptr 之后找
    pending = []           # 未命中的事件，留作补位
    for ev in events:
        q = (ev.get('desc', '') + ' ' + ' '.join(ev.get('keywords') or []))
        qbg = _char_bigrams(q)
        best_i, best_score = -1, 0.0
        for i in range(seg_ptr, len(segs)):
            if i in used:
                continue
            sc = _score(qbg, seg_bg[i])
            if sc > best_score:
                best_score, best_i = sc, i
        if best_i >= 0 and best_score > 0.04:
            used.add(best_i)
            assigned.append((best_i, ev.get('desc', '')))
            seg_ptr = best_i + 1
        else:
            pending.append(ev)
    # 补位：未命中的事件按序填进剩余空闲段
    free = [i for i in range(len(segs)) if i not in used]
    fi = 0
    for ev in pending:
        if fi < len(free):
            i = free[fi]; fi += 1
            assigned.append((i, ev.get('desc', '')))
    # 按镜头段出现顺序输出
    assigned.sort(key=lambda x: x[0])
    return [(segs[i][0], desc) for i, desc in assigned]


def _narrate_by_plot(video_path, plot, params, run_dir, progress=None, movie_name='', events=None):
    """🎭 剧情驱动核心（解说驱动剪辑）：
        完整解说稿 → 按每句字数分配画面 → 返回「解说词与画面一一对应」的区间。

    返回 (segs, narr, asr, frames, mode, events)。供 narrate_movie 与 规划分析(narrate 剧情模式)复用。

    【与旧实现的根本区别】旧版是「先按画面切换分段，再把剧情句贴上去」：
      - 段少 → 剧情被丢弃（实测 3 条剧情只用 1 条）；段多 → 剧情被复制铺满
      - 每句被硬截断到 30/40 字，讲到一半就断
      - 画面段由场景检测决定，与剧情结构无关 → 成片＝原片，谈不上剪辑
    新版反过来：先一次写完完整解说稿（开场钩子 / 因果推进 / 结尾升华），
    再按每句字数算出它需要多少秒画面，从原片里挑「有内容」的区间取用。
    解说不需要的画面自然被跳过，成片因此明显短于原片 —— 这才是普遍解说的做法。"""
    def up(ph, pct):
        if progress:
            progress['phase'] = ph; progress['pct'] = pct

    # 每个长阶段前检查取消：Whisper/LLM 都不走 ffmpeg，靠协作式中断响应「⏹ 停止」
    up('探测片长', 22)
    if _aborted():
        raise AbortError('用户取消了任务')
    vdur = probe_audio_len(video_path) or 0.0
    if vdur <= 0:
        raise RuntimeError('无法分析视频时长')
    up('识别台词(本地Whisper)', 32)
    if _aborted():
        raise AbortError('用户取消了任务')
    # S1: 上传后的后台预热可能正在跑同一视频的分析 —— 等它完成（命中缓存），
    # 超时则让预热让位，正式任务自行分析（S7 的 _gpu_slot 保证两者不互抢 GPU）
    try:
        _w._warmup_wait(video_path)
    except Exception:
        pass
    # ASR缓存：同视频同模型不重复转写
    whisper_model = whisper_model_name()
    # 缓存键必须带上「识别语言」与版本：旧键只有模型名，改了语言策略后仍会命中
    # 之前用错误语言转写出来的脏台词（实测：默认 zh 时代把英文片转成中文乱码，
    # 修好语言后依然命中旧缓存，等于修复不生效）。asr2 = 转写策略版本号。
    _asr_lang = whisper_language()
    asr_cache_key = _video_cache_key(video_path, f'asr2_{whisper_model}_{_asr_lang}')
    asr = _cache_load(asr_cache_key)
    if asr:
        _log.info(f'[DIAG] ASR命中缓存: {len(asr)}段台词')
        if progress:
            progress['phase'] = '台词识别（缓存命中）'
            progress['pct'] = 40
    else:
        asr = asr_segments(video_path, progress=progress, pct_range=(32, 42))
        if asr:
            _cache_save(asr_cache_key, asr)
    if _aborted():
        raise AbortError('用户取消了任务')

    # === 串行：先VLM建立画面索引，再写解说稿 ===
    # 注意：12GB显存无法同时加载VLM(6.5GB)+LLM(9GB)，并行会导致Ollama反复换模型，
    # 反而比串行慢2-3倍且容易卡死。TTS(网络/CPU)+裁剪(GPU编码NVENC)仍可并行，不冲突。
    up('建立画面索引（均匀抽样）', 44)
    # VLM缓存：同视频同模型不重复分析（最耗时的步骤，1小时视频约20分钟）
    vlm_model = (load_ai_config().get('vlm') or {}).get('model', 'default')
    # 质量模式判定提前到缓存之前：画面理解的「覆盖策略」变了（全片覆盖 vs 台词过滤），
    # 旧缓存是缺一半画面的脏数据，必须换 key，否则改良后的分析会被旧缓存短路。
    _q_param = params.get('fast', params.get('fast_mode', params.get('fastMode')))
    _quality_mode = (os.environ.get('NARRATE_FAST') != '1') if _q_param is None else (not bool(_q_param))
    vlm_cache_key = _video_cache_key(
        video_path, 'vlm_%s_%s' % (vlm_model, 'q2' if _quality_mode else 'f2'))
    scene_descs = _cache_load(vlm_cache_key)
    if scene_descs:
        _log.info(f'[DIAG] VLM命中缓存: {len(scene_descs)}个时间点')
        if progress:
            progress['phase'] = '画面索引（缓存命中）'
            progress['pct'] = 46
    else:
        scene_descs = _vlm_sample_timeline(video_path, vdur, asr, run_dir,
                                           progress=progress, quality=_quality_mode)
        # 画面理解结束后立刻卸载 VLM：12GB 卡放不下 VLM(6.1GB) + 文本模型(5.0GB)，
        # 不让位会让后续写稿/对齐/润色每次调用都触发换入换出。
        if vlm_unload():
            _log.info('[DIAG] 画面理解完成，已释放 VLM 显存（让位给文本模型）')
        if scene_descs:
            _cache_save(vlm_cache_key, scene_descs)
            _log.info(f'[DIAG] 画面索引: {len(scene_descs)}个时间点已建立并缓存')
    # 保存scene_descs到run_dir，供AI候选推荐和手动调整页使用
    if scene_descs and run_dir:
        try:
            import json as _jd
            _jd.dump(scene_descs, open(os.path.join(run_dir, 'scene_descs.json'), 'w', encoding='utf-8'),
                     ensure_ascii=False, indent=1)
        except Exception:
            pass

    # =====================================================================
    # 【质量路线·顺序修正】必须先看懂、再写稿。
    # 旧顺序是「写稿 → 剧情理解 → 对齐」，写稿时手里只有片名和剧情简介，
    # 画面里真实发生的事一点没用上，写出来的自然是通用影评而不是这部片的解说。
    # 现在把剧情理解层提到写稿之前，逐场剧情要点直接作为写稿素材。
    # =====================================================================
    # =====================================================================
    # 【故事线构建】逐窗含义 → 分幕剧情 → 人物/主线
    # 只给逐窗画面含义，8B 写出来的必然是镜头清单；先在时间轴上合成「幕」，
    # 再把幕合成一条主线，写稿拿到的才是「故事」而不是 110 段画面说明。
    # =====================================================================
    scene_story = {}
    _acts, _storyline = [], None
    # 用户自带稿子时，一切「写稿」环节都不需要：不推断剧情、不分幕、不写稿、不润色。
    # 稿子是成品，工具只负责「按稿子找画面」。
    _user_script = str(params.get('user_script') or params.get('userScript')
                       or params.get('script') or '').strip()
    _has_user_script = len(_user_script) >= 20
    if _quality_mode and scene_descs and any(s.get('event') or s.get('location')
                                             for s in scene_descs):
        up('剧情理解（推断每段画面在讲什么）', 43)
        # 剧情理解结果缓存：这一层是 LLM 批量推断，两次跑结果不一样，
        # 直接影响对齐质量（真机实测两轮跑出的对齐差 90 秒）。缓存住
        # 既省 100 秒，又保证同一视频的解说产出可复现。
        _story_cache_key = _video_cache_key(video_path, 'scene_story_v1')
        _cached_story = _cache_load(_story_cache_key)
        if _cached_story:
            scene_story = _cached_story
            _log.info('[DIAG] 剧情理解命中缓存: %d 个场景' % len(scene_story))
        else:
            try:
                scene_story = _infer_scene_story(scene_descs, asr=asr, movie_name=movie_name,
                                                 progress=progress, plot=plot)
                _log.info('[DIAG] 剧情理解: %d 个场景拿到剧情含义' % len(scene_story))
                if scene_story:
                    _cache_save(_story_cache_key, scene_story)
            except AbortError:
                raise
            except Exception as e:
                _log.info('[DIAG] 剧情理解层失败(不影响出片): %s' % e)
                scene_story = {}
        # 分幕 + 故事线：这是「解说讲剧情而不是描述画面」的关键一步。
        # 用户自带稿子时不需要——那是给写稿用的素材，稿子已经有了。
        if scene_story and not _has_user_script:
            try:
                _acts = _infer_acts(scene_story, scene_descs, movie_name=movie_name,
                                    plot=plot, progress=progress, asr=asr,
                                    n_acts=max(8, min(20, int(round(len(scene_story) / 6.0)))))
                if _acts:
                    _storyline = _build_storyline(_acts, movie_name=movie_name, plot=plot)
                    if progress:
                        progress['phase'] = '故事线（人物与主线）'
                    for _ai, _a in enumerate(_acts):
                        _log.info('[DIAG] 第%d幕 %s-%s %s' % (
                            _ai + 1, _fmt_tc(_a['start']), _fmt_tc(_a['end']),
                            _a['summary'][:90]))
            except AbortError:
                raise
            except Exception as e:
                _log.info('[DIAG] 分幕/故事线失败(退回逐场素材): %s' % e)
                _acts, _storyline = [], None

    # 目标时长：写稿时决定写多少节，留剪时决定保多少段——两条路都要用，必须在分支外算。
    target_sec = params.get('targetSec')
    try:
        target_sec = float(target_sec) if target_sec else None
    except Exception:
        target_sec = None
    if not target_sec:
        target_sec = vdur

    if _has_user_script:
        # ===== 用户稿子路线：一个字的写稿模型都不碰 =====
        # 稿子是用户自己写的成品，工具只做两件事：切句 → 按句找画面。
        # 之所以要单独一条路，是因为本地 8B 写出来的东西达不到可用的水平，
        # 而强行让模型「润色」用户稿只会把它改坏。
        up('读取用户稿子', 45)
        _uts = _script_to_texts(_user_script)
        if len(_uts) < 2:
            raise RuntimeError('稿子太短或切不出句子，请检查粘贴的内容')
        texts = list(_uts)
        # 用户稿子没有 AI 标注的重要度：全部按 advance，留剪按目标时长自行计算；
        # 目标时长缺省=全片，等于不额外剪，尊重用户的稿子。
        script = {'title': movie_name or '', 'hook': '', 'outro': '',
                  'beats': [{'text': t, 'keywords': [], 'importance': 'advance'}
                            for t in texts]}
        _log.info('[DIAG] 使用用户稿子: %d 句 / %d 字（跳过写稿与润色）'
                  % (len(texts), sum(len(t) for t in texts)))
    else:
        up('写完整解说稿', 42)
        # economy=False：本地模型是免费的，必须让它先写稿；云端用不用由 _w.ai_enabled('chat') 决定。
        # （历史坑：这里曾传 economy=not _w.ai_enabled('chat')，未配云端时 economy=True 会直接跳过
        #   本地模型走切句兜底，产出的是「剧情原文拼接」而不是解说稿——正是解说不像解说的原因。）
        # material：剧情资料 + 人物/主线/分幕（拿不到分幕时退回逐场画面理解）
        _material = _script_material(plot, scene_story, scene_descs,
                                     acts=_acts, storyline=_storyline)
        _log.info('[DIAG] 写稿素材: %d 字（剧情 + %d 幕 + 故事线%s）'
                  % (len(_material), len(_acts), '有' if _storyline else '无'))
        script = llm_movie_full_script(movie_name, plot, economy=False, target_sec=target_sec,
                                       material=_material)
        if not script or not script.get('beats'):
            raise RuntimeError('无法从剧情生成解说稿（请检查剧情文本或本地模型）')

        # hook + 各节拍 + outro 串成可直接口播的解说词序列
        texts = []
        if script.get('hook'):
            texts.append(script['hook'])
        for b in script['beats']:
            if str(b.get('text') or '').strip():
                texts.append(str(b['text']).strip())
        if script.get('outro'):
            texts.append(script['outro'])
    if not texts:
        raise RuntimeError('解说稿为空（请检查剧情文本或本地模型）')

    # TTS 标记后处理：检查标记完整性，自动补全停顿/情绪（本地LLM可能忽略标记指导）
    # 用户稿子不加标记：稿子是成品，多一个 {停顿} 都可能改掉人写的节奏。
    if _has_user_script:
        _log.info('[DIAG] 用户稿子：保留原文，不插入配音标记')
    else:
        texts = _enhance_tts_markup(texts)
    n_marked = sum(1 for t in texts if has_tts_markup(t))
    _log.info(f'[DIAG] TTS标记: {n_marked}/{len(texts)}节含标记')

    if _aborted():
        raise AbortError('用户取消了任务')

    # 【质量模式·默认】质量优先、不限时间；NARRATE_FAST=1 或 params.fast=true 可切快速。
    # 设计要点：不是「砍掉理解环节」，而是把「一次长调用」拆成「多次短调用」——
    # 12GB 卡上让 8B 一次吐「对齐 + 15 段润色」的长 JSON 要 3 分钟以上并超时；拆成每批
    # 4~6 句后每次输出短、必成功。任何一批失败都保留算法对齐/原稿，所以质量模式同样能出片。
    _fast_param = params.get('fast', params.get('fast_mode', params.get('fastMode')))
    _fast = (os.environ.get('NARRATE_FAST') == '1') if _fast_param is None else bool(_fast_param)

    scene_alignment = {}
    # ① 算法语义对齐（两种模式都做）：用 VLM 描述/台词/实体与解说词做 bigram 重叠，
    #    在时间顺序约束下为每句挑最相关场景。这是「LLM 失败也不会声画错位」的保底通道。
    if scene_descs:
        # scene_story 必传：剧情理解层的『这段在讲什么』比 VLM 的浅层画面描述
        # 更贴解说词。此前漏传，等于把最贵的语义信息白扔，只剩「警察/车内」这类
        # 通用词在打分，句句对错位。
        scene_alignment = _algo_align_beats_to_scenes(texts, scene_descs, asr=asr,
                                                      scene_story=scene_story)
        _log.info('[DIAG] 算法语义对齐(保底): %d/%d 句' % (len(scene_alignment), len(texts)))

    if _fast:
        up('快速出片（仅算法对齐，不调模型）', 46)
        _log.info('[DIAG] 快速模式：跳过 LLM 语义对齐/润色（算法对齐与剧情理解仍生效）')
    elif _has_user_script:
        # 用户稿子路线：只用 DP 全局对齐，不再调 LLM 批次对齐。
        # 实测 LLM 各批独立回答、批与批之间位置互相打架（顺序纠正要动 10 句），
        # 累积出的漂移比 DP 差一个量级（中段 +250 秒）；而 DP 拿着同一份
        # scene_story 做全局最优，真机 29 句实测平均误差 33 秒。少调 5 轮模型还更快。
        up('解说词-场景对齐（DP）', 49)
        _log.info('[DIAG] 用户稿子：跳过 LLM 批次对齐，直接用 DP 全局对齐')
    else:
        # ② 剧情理解层已在写稿之前跑完（见上方「顺序修正」），这里直接复用 scene_story。
        # ③ 分批语义对齐：每批 6 句，LLM 成功则覆盖算法结果，失败保留算法结果
        if scene_descs and any(s.get('event') or s.get('location') for s in scene_descs):
            up('解说词-场景语义对齐', 49)
            try:
                # 分批对齐是「原地覆盖」base，先留一份纯算法结果给跳跃闸兜底。
                _algo_base = dict(scene_alignment)
                scene_alignment = _llm_align_batched(
                    texts, scene_descs, base=scene_alignment, scene_story=scene_story,
                    asr=asr, movie_name=movie_name, batch=6,
                    timeout=int(params.get('align_timeout') or 150), progress=progress,
                    use_cloud=bool(params.get('align_cloud', False)))
                scene_alignment = _sanitize_alignment(scene_alignment, _algo_base,
                                                      scene_descs, len(texts))
                _log.info('[DIAG] 分批语义对齐完成: %d/%d 句' % (len(scene_alignment), len(texts)))
            except AbortError:
                raise
            except Exception as e:
                _log.info('[DIAG] 分批对齐异常(保留算法对齐): %s' % e)
        # ④ 分批润色：每批 4 段，只带该段对齐到的画面与台词；失败保留原稿
        if (not _has_user_script) and params.get('plotRefine', params.get('plot_refine', True)) \
                and scene_alignment and scene_descs:
            up('剧情润色（让解说贴合画面）', 52)
            try:
                _refined = _refine_batched(
                    texts, scene_alignment, scene_descs, scene_story=scene_story,
                    asr=asr, movie_name=movie_name, batch=4,
                    timeout=int(params.get('refine_timeout') or 150), progress=progress,
                    use_cloud=bool(params.get('refine_cloud', True)))
                if _refined and len(_refined) == len(texts):
                    texts = _enhance_tts_markup(_refined)
                    _log.info('[DIAG] 分批润色完成: %d/%d 段' % (len(_refined), len(texts)))
            except AbortError:
                raise
            except Exception as e:
                _log.info('[DIAG] 分批润色异常(保留原稿): %s' % e)

    # 落盘到 progress：合成阶段会写进 tts_state，出问题可离线复算对齐，不用重跑。
    # 放在整个 if/elif/else 之后——用户稿子路线走 elif，赋值放在分支里会被跳过。
    if progress is not None:
        progress['scene_story'] = {str(k): v for k, v in (scene_story or {}).items()}
        progress['scene_alignment'] = {str(k): list(v)
                                       for k, v in (scene_alignment or {}).items()}

    # ⑤ 终稿清洗（必做）：润色/对齐会把英文原声与「仿佛/似乎」类猜测定语重新写回来，
    #    写稿阶段的清洗不覆盖这里。只改写不删句，索引不变。
    try:
        if not _has_user_script:          # 用户稿子一个字都不改
            texts, _n_fix = _scrub_texts(texts)
            if _n_fix:
                _log.info('[DIAG] 终稿清洗: 改写 %d/%d 句（去英文台词/去猜测定语）'
                          % (_n_fix, len(texts)))
    except Exception as e:
        _log.info('[DIAG] 终稿清洗异常(保留原稿): %s' % e)

    # 阶段3：在对齐的场景内选片段（_allocate_script_spans 内部处理），
    # 对齐失败时自动退回台词bigram匹配保底
    up('按解说分配画面', 52)
    segs, narr_map = _allocate_script_spans(texts, vdur, asr=asr, vlm_captions=None,
                                             scene_alignment=scene_alignment, scenes=scene_descs,
                                             scene_story=scene_story,
                                             # 语义精修 A/B 实测（惩罚者 79 句）：bigram 重合度仅 +1.8%
                                             # （口语化解说 vs 白描场景，字面抓不住 paraphrase），
                                             # 不默认启用——待接入向量语义（Ollama embedding）后
                                             # 用 tools/ab_semantic_match.py 重测再开
                                             sem_weight=0.0)
    if not segs:
        raise RuntimeError('画面分配失败（视频可能过短）')

    # =====================================================================
    # 【剧情驱动·自动留/剪】把 LLM 给每个节拍标的剧情重要度（key/advance/transition）
    # 真正接进「保留/剪除」决策。目标时长明显短于全片时按重要度保高光、丢填充；
    # 目标时长≈全片则保留全部（不破坏现有成片）。后端不直接删段——只把低重要度段
    # 的 keep 标 False，渲染按用户开关默认剪、用户可在预览勾回，既自动又可控。
    # =====================================================================
    # 1) 每个解说节拍的重要度（与 texts 顺序严格对齐：hook + beats + outro）
    _beat_imp = []
    if script.get('hook'):
        _beat_imp.append('key')
    for _b in (script.get('beats') or []):
        if str(_b.get('text') or '').strip():
            _beat_imp.append(str(_b.get('importance') or 'advance'))
    if script.get('outro'):
        _beat_imp.append('key')

    # 2) 每个 span 所属节拍 + 时长聚合，然后做留/剪决策
    _beat_dur = {}
    for _k, (_a, _b) in enumerate(segs):
        _j = narr_map[_k] if _k < len(narr_map) else -1
        if _j < 0:
            continue
        _beat_dur[_j] = _beat_dur.get(_j, 0.0) + (_b - _a)
    _kept_beats = _decide_keep_beats(_beat_imp, _beat_dur, target_sec, vdur)

    # 3) 生成每个 span 的 keep / importance + 透明化 cut_plan（后端不删段，渲染按开关处理）
    def _strip(t):
        import re as _re
        return _re.sub(r'[\[\]][a-zA-Z0-9_,;:（）()\u4e00-\u9fa5]*[\[\]]', '', (t or '')).strip()
    try:
        _outline = []
        _cp2 = []
        _kept_sec = 0.0
        for _k, (_s0, _s1) in enumerate(segs):
            _j = narr_map[_k] if _k < len(narr_map) else -1
            _imp = _beat_imp[_j] if 0 <= _j < len(_beat_imp) else 'advance'
            _keep = True if _j < 0 else (_j in _kept_beats)
            if _keep:
                _kept_sec += (_s1 - _s0)
            _bi = _j if 0 <= _j < len(texts) else min(_k, len(texts) - 1)
            _cap = texts[_bi] if 0 <= _bi < len(texts) else ''
            if _keep:
                _why = ('剧情关键镜头（必留）' if _imp == 'key'
                        else '剧情推进镜头' if _imp == 'advance'
                        else '过渡/氛围镜头（精剪保留）')
            else:
                _why = '剧情重要度低（%s），精剪精简' % ('过渡' if _imp == 'transition' else '次要')
            _outline.append({'start': round(float(_s0), 2), 'end': round(float(_s1), 2),
                             'importance': _imp, 'keep': _keep})
            _cp2.append({'i': len(_cp2), 'start': round(float(_s0), 2), 'end': round(float(_s1), 2),
                         'keep': _keep, 'importance': _imp, 'reason': _why,
                         'caption': _strip(_cap)[:120], 'asr': ''})
        _cp2.sort(key=lambda x: x['start'])
        _cov2 = (_kept_sec / vdur) if vdur else 0.0
        if progress is not None:
            progress['outline'] = _outline
            progress['cut_plan'] = {
                'src_dur': round(vdur, 2),
                'coverage': round(_cov2 * 100, 1),
                'kept': sum(1 for x in _cp2 if x['keep']),
                'removed': sum(1 for x in _cp2 if not x['keep']),
                'removed_sec': round(sum((x['end'] - x['start']) for x in _cp2 if not x['keep']), 1),
                'segments': _cp2,
            }
            _log.info('[DIAG] 剧情留/剪: 目标%.0fs 实际保留%.0fs(%.0f%%) 保留%d/剪除%d段'
                      % (target_sec if target_sec else vdur, _kept_sec, _cov2 * 100,
                         sum(1 for x in _cp2 if x['keep']), sum(1 for x in _cp2 if not x['keep'])))
    except Exception as _e2:
        _log.info(f'[DIAG] 剧情 cut_plan 构建失败(不影响主流程): {_e2}')

    # events 保持旧格式返回，供 diag 与「按解说词重新匹配分镜」复用
    events = [{'desc': t[:40], 'keywords': []} for t in texts]
    return segs, texts, asr, {}, 'movie', events, narr_map


def _tts_concurrency():
    """TTS 并发路数：环境变量 TTS_CONCURRENCY 覆盖，默认 3，钳制 1~5。
    仅「本地引擎 + edge-tts 可用」时生效（网络等待可重叠）；mimo/本地 CPU 引擎保持串行。"""
    try:
        v = int(os.environ.get('TTS_CONCURRENCY', '') or 3)
    except ValueError:
        v = 3
    return max(1, min(5, v))


def _generate_all_tts(narr, run_dir, progress=None):
    """逐段生成所有配音，返回 [(index, clip_path), ...]。不做视频裁剪。
    断点续跑：已存在且有效的 narr%d.mp3 直接跳过，只生成缺失的。
    【并发（S6）】默认路径（本地引擎 + edge-tts 可用）用 TTS_CONCURRENCY 路并发：
      - 每段是独立 edge-tts CLI 子进程，线程安全；
      - 并发下首选引擎全部锁定为 edge（local_tts_speak 的锁定语义），音色一致；
      - 并发数可用环境变量 TTS_CONCURRENCY 覆盖（=1 即完全回退原串行行为）；
      - mimo（云端）/ 本地 CPU 引擎（cosyvoice/chattts/sherpa/sapi）保持串行，避免抢资源/限流。
    【熔断探测恢复】并发下探测文件（.edge_probe.mp3）用任务级锁互斥；
    各 worker 按自己处理的段数每 3 段检查一次，edge-tts 成功后内部自动复位熔断。
    【诊断日志】逐段记录 {idx, engine, ok, error, duration_ms} 到 tts_log.json。"""
    import time as _time
    import concurrent.futures as _cf
    # 路径修复：progress_state 中的 run_dir 可能是相对路径（如 "run-1-..."），
    # 转成绝对路径（基于 _w.OUTDIR）避免 TTS 文件写到项目根目录导致"有日志无文件"。
    if run_dir and not os.path.isabs(run_dir):
        run_dir = os.path.join(_w.OUTDIR, run_dir)
    results = []
    _tcfg = load_ai_config().get('tts') or {}
    use_mimo = bool(_tcfg.get('api_key')) and bool(_tcfg.get('model'))
    skipped = 0
    _tts_diag = []   # 诊断日志：[{idx, engine, ok, error, duration_ms, source}]
    _n_workers = _tts_concurrency()
    _parallel = (not use_mimo) and bool(edge_tts_available()) and _n_workers > 1
    _prog_lock = threading.Lock()
    _probe_lock = threading.Lock()   # 并发下 .edge_probe.mp3 是共享路径，探测必须互斥
    _probe_state = {'n': 0}

    # === 断点续跑：主线程统一检查，只提交缺失段 ===
    pending = []   # [(idx, txt)]
    for i, txt in enumerate(narr):
        if not (txt or '').strip():
            continue
        existing = os.path.join(run_dir, 'narr%d.mp3' % i)
        if os.path.exists(existing) and os.path.getsize(existing) > 100:
            results.append((i, existing))
            skipped += 1
            _tts_diag.append({'idx': i, 'engine': 'cache', 'ok': True, 'error': '',
                              'duration_ms': 0, 'source': 'cache'})
        else:
            pending.append((i, txt))
    total = len(pending)
    done = 0
    _log.info('[DIAG] TTS开始: 共%d段, 需生成%d, 跳过%d, 并发%d路' % (
        len(narr), total, skipped, _n_workers if _parallel else 1))

    def _update_progress(phase, pct):
        if progress:
            with _prog_lock:
                progress['phase'] = phase
                progress['pct'] = pct

    def _synth_one(item):
        """合成单段；返回 (idx, clip, eng, err, dur_ms, is_abort)。"""
        i, txt = item
        if _aborted():
            return (i, None, None, '', 0, True)
        # 熔断探测恢复（根因 1 改良）：每 3 段检查，被熔断则探测
        _probe_state['n'] += 1
        if _probe_state['n'] % 3 == 0 and edge_tts_dead_reason():
            with _probe_lock:
                if edge_tts_dead_reason():
                    _edge_probe_recover(run_dir)
        _t0 = _time.time()
        clip = None
        _eng = None
        _err = ''
        if use_mimo:
            np_ = os.path.join(run_dir, 'narr%d.mp3' % i)
            if _w.ai_tts(txt, np_):
                clip = np_
                _eng = 'mimo'
            else:
                _err = 'mimo_failed'
        if clip is None:
            ok, _eng, lp = local_tts_speak(txt, os.path.join(run_dir, 'narr%d.mp3' % i))
            if ok:
                clip = lp
            else:
                _err = _eng or 'all_engines_failed'
                _eng = _eng or 'none'
        _dur = int((_time.time() - _t0) * 1000)
        return (i, clip, _eng, _err, _dur, False)

    def _apply(result, source):
        """汇总一段结果并推进进度。返回 True 表示收到取消信号。"""
        nonlocal done
        i, clip, _eng, _err, _dur, is_abort = result
        if is_abort:
            return True
        if clip is not None:
            results.append((i, clip))
            _tts_diag.append({'idx': i, 'engine': _eng, 'ok': True, 'error': '',
                              'duration_ms': _dur, 'source': source})
        else:
            _tts_diag.append({'idx': i, 'engine': _eng, 'ok': False, 'error': _err,
                              'duration_ms': _dur, 'source': source})
        done += 1
        processed = skipped + done
        if _parallel:
            _update_progress('逐段配音 %d/%d（并发%d路）' % (processed, len(narr), _n_workers),
                             55 + int(25 * processed / max(1, len(narr))))
        else:
            _update_progress('逐段配音 %d/%d' % (processed, len(narr))
                             + ('（断点续跑，已跳过%d段）' % skipped if skipped else ''),
                             55 + int(25 * processed / max(1, len(narr))))
        return False

    # === 第一轮：并发或串行生成 ===
    if _parallel and total > 0:
        with _cf.ThreadPoolExecutor(max_workers=_n_workers) as _ex:
            _futs = [_ex.submit(_synth_one, it) for it in pending]
            for _f in _cf.as_completed(_futs):
                try:
                    _r = _f.result()
                except Exception as _e:
                    _log.info('[DIAG] TTS并发worker异常: %s' % _e)
                    continue
                if _apply(_r, 'fresh'):
                    break   # 协作式取消：收到信号即停止汇总（worker 内自行快速返回）
    else:
        for _item in pending:
            if _aborted():
                break
            _r = _synth_one(_item)
            if _apply(_r, 'fresh'):
                break

    if skipped:
        _log.info('[DIAG] TTS断点续跑: 跳过%d段已生成配音' % skipped)
    _log.info('[DIAG] TTS第一轮完成: %d/%d段' % (len(results), len(narr)))
    # 失败重试：第一轮没成功的段落再试一次（可能是瞬时网络抖动或熔断恢复）
    success_idx = set(i for i, _ in results)
    failed = [i for i in range(len(narr)) if i not in success_idx and (narr[i] or '').strip()]
    if failed:
        _log.info('[DIAG] TTS重试 %d 个失败段落' % len(failed))
        _time.sleep(1.0)  # 等熔断恢复
        for i in failed:
            if _aborted(): break
            _r = _synth_one((i, narr[i]))
            if _r[5]:
                break
            _apply(_r, 'retry')
            if progress:
                with _prog_lock:
                    progress['phase'] = '配音重试 %d/%d' % (len([r for r in results if r[0] in failed]), len(failed))
    results.sort(key=lambda x: x[0])
    # 写入诊断日志
    _log_path = os.path.join(run_dir, 'tts_log.json')
    try:
        _bad = [e for e in _tts_diag if not e.get('ok')]
        _jd = {'segments': _tts_diag,
               'summary': {'total': len(narr), 'success': len(results),
                           'failed': len(narr) - len(results) - skipped,   # [2.2] 计数（兼容旧字段）
                           'skipped': skipped,
                           'engines_used': list(set(e.get('engine', '') for e in _tts_diag if e.get('engine'))),
                           'broken_until': _edge_internal().get('dead_until', 0.0),  # [2.4] 线程级熔断状态
                           'failed_segments': [{'idx': e.get('idx'), 'engine': e.get('engine'),
                                                'error': e.get('error', '')} for e in _bad]}}  # [2.2] 结构化失败原因
        with open(_log_path, 'w', encoding='utf8') as _f:
            import json as _json
            _json.dump(_jd, _f, ensure_ascii=False, indent=1)
        if progress:
            # [2.2] 失败段对前端可见（手动调整/进度页可展示「第X段配音失败，已用Y替换/该段静音」）
            progress['tts_failures'] = [{'idx': e.get('idx'), 'engine': e.get('engine'),
                                         'error': e.get('error', '')} for e in _bad]
    except Exception as _e:
        _log.info(f'[DIAG] TTS日志写入失败: {_e}')
    _warn_tts_engine_degraded(_tts_diag, progress)
    _log.info('[DIAG] TTS最终完成: %d/%d段, 日志%s' % (len(results), len(narr), _log_path))
    return results


def _warn_tts_engine_degraded(diag, progress):
    """配音引擎被降级时，必须让用户知道——否则「不静默」就是一句空话。

    实测场景：edge-tts（晓晓/云希，自然）连不上微软服务时，管线静悄悄全部
    落到 SAPI（系统自带，机械感重），tts_log 里 success=4、无一条错误，
    用户拿到的成片音色断崖下降却毫不知情，只以为「配音就这样」。
    这里在进度里写明实际用的引擎，前端进度条旁边直接可见。
    """
    # 注意用 `is None`：progress 早期常是空字典，`not {}` 为真会把告警整个吞掉
    if progress is None or not diag:
        return
    used = [e.get('engine', '') for e in diag if e.get('ok') and e.get('engine')
            and e.get('engine') not in ('cache',)]
    if not used:
        return
    fallback_engines = ('sapi', 'sapi_fallback')
    if any(e in fallback_engines for e in used):
        progress['warn'] = (
            (progress.get('warn') + '；' if progress.get('warn') else '')
            + '配音引擎已降级到系统自带 SAPI（机械感较重）：edge-tts 当前不可用，'
              '多为网络连不上微软朗读服务。检查网络/代理后可恢复。')
        progress['tts_engine_degraded'] = True
    progress['tts_engine'] = max(set(used), key=used.count)


def _edge_probe_recover(run_dir):
    """edge-tts 熔断探测恢复。用极短文本做一次轻量合成测试。
    edge_tts_speak 内部成功即复位熔断（update fails=0, dead_until=0）；
    失败则不影响熔断状态。探测文件自动清理。"""
    probe_text = '你好'
    probe_out = os.path.join(run_dir, '.edge_probe.mp3')
    try:
        edge_tts_speak(probe_text, probe_out)
    except Exception:
        pass
    finally:
        # 清理探测产物（mp3 和可能的 .ssml）
        for _p in (probe_out, probe_out + '.ssml'):
            if _p and os.path.exists(_p):
                try:
                    os.unlink(_p)
                except Exception:
                    pass


def compose_movie_from_tts(run_dir, progress=None, music_path=None, adjusted_items=None, skip=None, user_params=None):
    """Phase 2：加载已保存的配音状态，裁剪视频+合成。用户确认配音后调用。
    adjusted_items: 用户调整后的片段列表 [{index, text, audio}]，None则用原始状态
    skip: 要跳过的片段索引列表
    """
    import json as _json
    state_path = os.path.join(run_dir, 'tts_state.json')
    if not os.path.exists(state_path):
        raise RuntimeError('未找到配音状态文件，请先生成配音')
    state = _json.load(open(state_path, encoding='utf-8'))
    # 恢复第一步的剪辑方案：渲染据此决定是否真剪（removed>0 → 必须剪）
    if progress is not None and state.get('cut_plan'):
        progress['cut_plan'] = state['cut_plan']
    video_path = state['video_path']
    if not video_path or not os.path.exists(video_path):
        # 源片副本可能已被磁盘清扫回收（超过 cleanup_src_days 未使用）——
        # 给出明确指引而非让 ffmpeg 报一堆找不到文件
        raise RuntimeError('源片副本已被磁盘清理（超过保留期未使用）。'
                           '请重新上传源视频生成解说配音，或在 AI 配置页把 cleanup_src_days 调大。')
    segs = [tuple(s) for s in state['segs']]
    narr = state['narr']
    narr_map = state.get('narr_map') or []
    params = state.get('params') or {}
    if user_params:
        params.update(user_params)  # 用户在手动调整页设置的参数（如字幕样式）覆盖默认值
    # 应用用户调整：跳过指定段，用调整后的文本/音频
    skip_set = set(skip or [])
    if adjusted_items:
        tts_results = []
        new_narr = []
        new_narr_map = []
        # 重建索引：只保留未跳过的段
        old_to_new = {}
        new_idx = 0
        for item in adjusted_items:
            old_i = item.get('index', 0)
            if old_i in skip_set:
                continue
            old_to_new[old_i] = new_idx
            new_narr.append(item.get('text', ''))
            if item.get('audio'):
                tts_results.append((new_idx, item['audio']))
            new_idx += 1
        # 收集用户调整的视频时间范围和配音偏移（按原始索引）
        user_video_spans = {}
        user_audio_offsets = {}
        user_orig_volumes = {}  # 每段原视频音量（0-100）
        user_narr_volumes = {}  # 每段解说音量（0-200 百分比，None=默认 100%）
        for item in adjusted_items:
            old_i = item.get('index', 0)
            if old_i in skip_set:
                continue
            vs = item.get('video_start')
            ve = item.get('video_end')
            if vs is not None and ve is not None and ve > vs:
                user_video_spans[old_i] = (float(vs), float(ve))
            ao = item.get('audio_offset')
            if ao is not None and abs(float(ao)) > 0.01:
                user_audio_offsets[old_i] = float(ao)
            ov = item.get('orig_volume')
            if ov is not None:
                user_orig_volumes[old_i] = max(0, min(100, float(ov)))
            nv = item.get('narr_volume')
            if nv is not None:
                # 每段解说音量（百分比 0~200，混音时 ÷100 成倍率）
                user_narr_volumes[old_i] = max(0, min(200, float(nv)))
        # 重建segs和narr_map（只保留未跳过的段对应的画面）
        if narr_map and len(narr_map) == len(segs):
            new_segs = []
            new_narr_map = []
            for k in range(len(segs)):
                bi = narr_map[k]
                if bi in old_to_new:
                    new_segs.append(segs[k])
                    new_narr_map.append(old_to_new[bi])
            segs = new_segs
            narr_map = new_narr_map
        narr = new_narr
    else:
        tts_results = [(i, p) for i, p in state.get('tts_results', []) if i not in skip_set]
        if skip_set:
            # 跳过段后重建索引
            old_to_new = {}
            new_idx = 0
            for i in range(len(narr)):
                if i not in skip_set:
                    old_to_new[i] = new_idx
                    new_idx += 1
            new_narr = [narr[i] for i in range(len(narr)) if i not in skip_set]
            new_tts = [(old_to_new[i], p) for i, p in tts_results if i in old_to_new]
            if narr_map and len(narr_map) == len(segs):
                new_segs = []
                new_narr_map = []
                for k in range(len(segs)):
                    bi = narr_map[k]
                    if bi in old_to_new:
                        new_segs.append(segs[k])
                        new_narr_map.append(old_to_new[bi])
                segs = new_segs
                narr_map = new_narr_map
            narr = new_narr
            tts_results = new_tts
    def up(ph, pct):
        if progress:
            progress['phase'] = ph; progress['pct'] = pct

    src_video = video_path
    cut_info = {'cut_sec': 0.0, 'src_dur': round(probe_audio_len(video_path) or 0.0, 2)}
    # 保存原始narr_map用于剪辑后聚合（剪辑前不聚合，避免把不连续片段间的空白也保留）
    orig_narr_map = list(narr_map) if narr_map and len(narr_map) == len(segs) else None
    orig_segs_count = len(segs)
    # 注意：这里不聚合！用原始独立segs剪辑，空白段会被真正剪掉。
    # 剪辑后再按beat聚合（此时视频已无空白，聚合不会包含多余内容）。
    # 应用用户手动调整的视频时间范围（必须在所有seg处理之后，用old_to_new映射覆盖）
    if adjusted_items and user_video_spans:
        video_dur = probe_audio_len(video_path) or 0
        _log.info('[DIAG] 用户手动调整了%d段画面时间，视频时长%.1f秒，共%d段画面' % (len(user_video_spans), video_dur, len(segs)))
        for old_i, (vs, ve) in user_video_spans.items():
            new_i = old_to_new.get(old_i, old_i)
            if 0 <= new_i < len(segs):
                if ve > vs and vs >= 0 and (video_dur == 0 or ve <= video_dur + 1):
                    segs[new_i] = (vs, min(ve, video_dur) if video_dur else ve)
                    _log.info('[DIAG] 第%d段(原%d)画面已覆盖为: %.1f-%.1f秒' % (new_i + 1, old_i + 1, vs, ve))
                else:
                    _log.info('[DIAG] 第%d段(原%d)画面时间不合理(%.1f-%.1f)，保留自动范围%.1f-%.1f' % (new_i + 1, old_i + 1, vs, ve, segs[new_i][0], segs[new_i][1]))
            else:
                _log.info('[DIAG] 第%d段(原%d)索引越界(共%d段)，跳过' % (new_i + 1, old_i + 1, len(segs)))
    # === B-roll 支持：插入无配音的纯画面段 ===
    if adjusted_items:
        broll_items = [(i, it) for i, it in enumerate(adjusted_items) if it.get('isBroll')]
        if broll_items:
            _log.info('[DIAG] 检测到%d个B-roll段，插入到画面序列' % len(broll_items))
            # 按顺序重建segs和narr，插入B-roll段
            new_segs = []
            new_narr = []
            # 建立 old_index -> new_index 映射（非B-roll项）
            non_broll = [it for it in adjusted_items if not it.get('isBroll') and it.get('index', -1) not in skip_set]
            old_to_pos = {}
            for pos, it in enumerate(non_broll):
                old_to_pos[it.get('index', 0)] = pos
            # 遍历adjusted_items，按顺序插入
            seg_idx = 0
            for it in adjusted_items:
                if it.get('index', -1) in skip_set:
                    continue
                if it.get('isBroll'):
                    vs = float(it.get('video_start', 0))
                    ve = float(it.get('video_end', 0))
                    if ve > vs:
                        new_segs.append((vs, ve))
                        new_narr.append('')  # B-roll无解说词
                        _log.info('[DIAG] B-roll段插入: %.1f-%.1f秒' % (vs, ve))
                else:
                    if seg_idx < len(segs):
                        new_segs.append(segs[seg_idx])
                    else:
                        new_segs.append((0.0, 1.0))
                    if seg_idx < len(narr):
                        new_narr.append(narr[seg_idx])
                    else:
                        new_narr.append('')
                    seg_idx += 1
            segs = new_segs
            narr = new_narr
            # 重建tts_results索引：B-roll项无配音，非B-roll项索引已偏移
            if tts_results:
                old_to_new_tts = {}
                new_tts_idx = 0
                for it in adjusted_items:
                    if it.get('index', -1) in skip_set:
                        continue
                    if not it.get('isBroll'):
                        old_i = it.get('index', 0)
                        old_to_new_tts[old_i] = new_tts_idx
                        new_tts_idx += 1
                new_tts_results = []
                for old_i, audio in tts_results:
                    if old_i in old_to_new_tts:
                        new_tts_results.append((old_to_new_tts[old_i], audio))
                tts_results = new_tts_results
                _log.info('[DIAG] B-roll后重建配音索引: %d段配音' % len(tts_results))
            _log.info('[DIAG] B-roll插入后: %d个画面段, %d段解说词' % (len(segs), len(narr)))
    # 再裁剪（segs现在是每节一个时间范围，数量=解说词段数，TTS索引直接对应）
    if params.get('autoCut', True):
        up('按分镜剪辑画面', 60)
        src_video, segs, cut_sec = _cut_video_by_spans(video_path, segs, run_dir, progress)
        cut_info['cut_sec'] = cut_sec
    # 剪辑后聚合：把同属一个beat的多个seg在剪辑后的时间轴上合并（此时无空白，聚合安全）
    if orig_narr_map and len(orig_narr_map) == orig_segs_count and len(segs) == orig_segs_count:
        beat_ranges = []
        for bi in range(len(narr)):
            bsegs = [segs[k] for k in range(len(segs)) if orig_narr_map[k] == bi]
            if bsegs:
                beat_ranges.append((bsegs[0][0], bsegs[-1][1]))
            else:
                beat_ranges.append((0.0, 0.0))
        segs = beat_ranges
        _log.info('[DIAG] 剪辑后聚合: %d节 -> %d个片段（已跳过空白）' % (len(narr), len(segs)))
    cut_info['out_dur'] = round(probe_audio_len(src_video) or cut_info['src_dur'], 2)
    # 计算voice_spans和tts_paths（segs[i]就是第i节解说词对应的画面范围）
    tts_paths = []
    voice_spans = {}
    # 建立new_idx -> old_idx的反向映射（用于查找audio_offset）
    _rev_map = {}
    if adjusted_items:
        for _item in adjusted_items:
            _old_i = _item.get('index', 0)
            if _old_i in skip_set: continue
            if _old_i in old_to_new:
                _rev_map[old_to_new[_old_i]] = _old_i
    for i, clip in tts_results:
        seg_span = segs[i] if i < len(segs) else (0.0, 10.0)
        # 应用用户调整的配音偏移
        _offset = 0.0
        _orig_vol = None
        _narr_vol = None
        if adjusted_items and i in _rev_map:
            _offset = user_audio_offsets.get(_rev_map[i], 0.0)
            _orig_vol = user_orig_volumes.get(_rev_map[i])
            if _rev_map[i] in user_narr_volumes:
                _narr_vol = user_narr_volumes[_rev_map[i]] / 100.0
        if _offset != 0:
            _log.info('[DIAG] 第%d段配音偏移%.1f秒' % (i+1, _offset))
        if _orig_vol is not None:
            _log.info('[DIAG] 第%d段原片音量%.0f%%' % (i+1, _orig_vol))
        if _narr_vol is not None:
            _log.info('[DIAG] 第%d段解说音量%.0f%%' % (i+1, _narr_vol * 100))
        tts_paths.append((clip, seg_span[0] + _offset, seg_span[1] + _offset, _orig_vol, _narr_vol))
        v_len = probe_audio_len(clip) or max(0.5, seg_span[1] - seg_span[0])
        voice_spans[i] = (seg_span[0] + _offset, min(seg_span[1] + _offset, seg_span[0] + _offset + v_len + 0.35))
    _log.info('[DIAG] 合成阶段: %d段配音, %d个画面片段' % (len(tts_paths), len(segs)))
    up('混音+烧字幕+配乐', 80)
    # 字幕必须去掉 TTS 标记：narr 里带 {情绪:紧张}/{停顿:0.5}，直接烧进字幕
    # 会在画面里显示成「{/情绪}{/停顿}」（实测成片即如此）。
    narr_srt = ['' if (t or '').strip() in ('（留白）', '(留白)')
                else _clean_caption(strip_tts_markup(t)) for t in narr]
    # [P0-2] 配乐情绪包络：把第一步算好的剧情重要度带进混音阶段。
    params = attach_importance(params, progress, segs)
    final = _compose_narration_video(src_video, segs, narr_srt, tts_paths, run_dir, params,
                                     music_path=music_path, voice_spans=voice_spans)
    if progress:
        progress['done'] = True; progress['pct'] = 100
        progress['file'] = os.path.relpath(final, _w.OUTDIR).replace('\\', '/') if final else ''
    return final


def narrate_movie(movie_name, plot, video_path, params, run_dir, progress=None, music_path=None, tts_only=False):
    """Phase 3 主流程：联网搜索剧情 → LLM 生成解说稿 → (上传电影时)ASR+语义对齐 → 配音+字幕+配乐成片。
    未上传视频时只产出解说稿（progress['script']）。"""
    def up(ph, pct):
        if progress:
            progress['phase'] = ph; progress['pct'] = pct
    up('联网搜索剧情', 4)
    if not plot:
        hits = web_search((movie_name or '') + ' 剧情 简介 豆瓣 梗概 分幕')
        plot = '\n'.join(t for t, _, _ in hits) or ''
    if not video_path:
        # 仅解说稿：直接按剧情产出事件（不分析视频）
        events = llm_movie_script(movie_name, plot, economy=not _w.ai_enabled('chat'))
        if not events:
            raise RuntimeError('无法生成解说稿（请检查网络，或在指令里粘贴剧情文本）')
        if progress:
            progress['done'] = True; progress['pct'] = 100
            progress['file'] = ''; progress['script'] = events
            progress['mode'] = _w.compute_mode(params, needs_chat=True)
        return None, {'events': events, 'no_video': True}
    # 有视频：剧情驱动（解说稿 + 画面分配都在 _narrate_by_plot 内一次完成）。
    # 此处不再单独调一次 llm_movie_script——那会白白多跑一轮模型，且旧接口只产出
    # 30 字一句的碎片事件，与完整解说稿不是一回事。
    segs, narr, asr, _frames, mode, events, narr_map = _narrate_by_plot(
        video_path, plot, params, run_dir, progress, movie_name=movie_name)

    # === 两步走模式：先生成配音，等用户确认后再合成 ===
    if tts_only:
        up('逐段生成配音', 55)
        tts_results = _generate_all_tts(narr, run_dir, progress=progress)
        import json as _json
        tts_state = {
            'video_path': os.path.abspath(video_path),
            'segs': [[float(s), float(e)] for s, e in segs],
            'narr': list(narr),
            'narr_map': list(narr_map) if narr_map else [],
            'params': params,
            'tts_results': [[i, p] for i, p in tts_results],
            'movie_name': movie_name,
            'asr': asr if asr else [],
            # 剪辑方案随状态保存：第二步合成时据此决定「是否真剪」，
            # 避免被剪段信息丢失后按覆盖判定跳过剪辑（剪辑失效根因）。
            'cut_plan': (progress or {}).get('cut_plan'),
            # 对齐与剧情理解结果一并落盘：出问题时可以离线复算「画面为什么配到这里」，
            # 不必每次重跑三分钟的 VLM/对齐；也是断点续跑的依据。
            'scene_story': (progress or {}).get('scene_story') or {},
            'scene_alignment': (progress or {}).get('scene_alignment') or {},
        }
        # [P0-1] 状态文件必须原子写：写到一半被打断（断电/kill/磁盘满）会留半截 JSON，
        # 下次 json.load 抛异常 → 进度文件报废、用户白跑。已有 _atomic_json_dump 帮手。
        _atomic_json_dump(os.path.join(run_dir, 'tts_state.json'), tts_state)
        if progress:
            progress['done'] = True
            progress['pct'] = 100
            if not tts_results:
                progress['error'] = '配音生成失败（所有段落TTS均未成功），请检查TTS引擎配置或网络后重试'
                progress['phase'] = '❌ 配音生成失败'
                progress['done'] = True
                progress['pct'] = 100
            else:
                progress['phase'] = '配音已生成（%d/%d段），请确认后合成' % (len(tts_results), len([n for n in narr if n.strip()]))
            progress['tts_list'] = [{'index': i, 'text': narr[i] if i < len(narr) else '',
                                      'audio': os.path.relpath(p, _w.OUTDIR).replace('\\', '/'),
                                      'duration': round(probe_audio_len(p) or 0, 1)}
                                     for i, p in tts_results]
            progress['run_dir'] = os.path.basename(run_dir)
            # 源片真实文件名（可能是 .mkv/.webm 等任意容器）：手动调整页的片段
            # 预览按此拼 /media/ 地址——曾写死 src.mp4，4K MKV 预览全 404。
            progress['video_file'] = os.path.relpath(video_path, _w.OUTDIR).replace('\\', '/')
            progress['script'] = events
            progress['diag'] = {'segments': len(segs), 'asr_lines': len(asr) if asr else 0,
                                'voice_clips': len(tts_results), 'narration': narr,
                                'awaiting_confirm': True}
            try:
                cover_path = os.path.join(run_dir, 'cover.jpg')
                ffmpeg_run(['-y', '-i', video_path, '-vframes', '1', '-q:v', '3', cover_path])
                if os.path.exists(cover_path):
                    # 封面仅作展示用（progress['cover']），【不】写入 progress['file']：
                    # file 语义是「成品视频」，第一步只是配音待确认，若把封面塞进 file，
                    # _record_history 会把封面当成成品写进 ⑨记录（图片探测时长失败又被静默跳过），
                    # 导致「今天明明生成了配音，记录里却什么都没有」。
                    progress['cover'] = os.path.relpath(cover_path, _w.OUTDIR).replace('\\', '/')
            except Exception:
                pass
        diag = {'narr': len(narr), 'tts_ok': len(tts_results), 'tts_total': len(narr),
                'awaiting_confirm': True, 'run_dir': os.path.basename(run_dir),
                'segments': len(segs), 'asr_lines': len(asr) if asr else 0,
                'voice_clips': len(tts_results)}
        return None, diag

    # ✂ 真剪辑：只保留解说覆盖到的镜头段（此前整链路不剪切，成片恒等于原片时长）
    src_video = video_path
    cut_info = {'cut_sec': 0.0,
                'src_dur': round(probe_audio_len(video_path) or 0.0, 2),
                'out_dur': None, 'segs': len(segs)}

    # === 并行优化：TTS配音在后台线程跑，主线程同时做视频裁剪 ===
    # TTS音频生成只依赖解说文本(narr)，不依赖裁剪结果；裁剪完成后再算voice_spans
    import threading as _th_tts
    _tts_results = []  # [(index, clip_path)]
    _tts_error = [None]
    _tcfg2 = load_ai_config().get('tts') or {}
    use_mimo = bool(_tcfg2.get('api_key')) and bool(_tcfg2.get('model'))
    def _tts_worker():
        try:
            for i, txt in enumerate(narr):
                if _aborted():
                    return
                if not txt.strip():
                    continue
                clip = None
                if use_mimo:
                    np_ = os.path.join(run_dir, f'narr{i}.mp3')
                    if _w.ai_tts(txt, np_):
                        clip = np_
                if clip is None:
                    ok, _eng, lp = local_tts_speak(txt, os.path.join(run_dir, f'narr{i}.mp3'))
                    if ok:
                        clip = lp
                if clip is not None:
                    _tts_results.append((i, clip))
                if progress:
                    progress['phase'] = f'逐段配音（并行）{i+1}/{len(narr)}'
        except Exception as e:
            _tts_error[0] = e
    _tts_thread = _th_tts.Thread(target=_tts_worker, daemon=True, name='tts-worker')
    _tts_thread.start()
    _log.info('[DIAG] TTS配音已启动（与裁剪并行）')

    up('逐段配音+裁剪（并行）', 58)
    # 先聚合：同属一个beat的多个seg合并成一个时间范围（用原始segs，不依赖裁剪后数量）
    if narr_map and len(narr_map) == len(segs):
        beat_ranges = []
        for bi in range(len(narr)):
            bsegs = [segs[k] for k in range(len(segs)) if narr_map[k] == bi]
            if bsegs:
                beat_ranges.append((bsegs[0][0], bsegs[-1][1]))
            else:
                beat_ranges.append((0.0, 0.0))
        segs = beat_ranges
        _log.info(f'[DIAG] 高密度聚合: {len(narr)}节 -> {len(segs)}个片段')
    # 主线程继续做裁剪（TTS在后台跑，segs现在是每节一个范围）
    if params.get('autoCut', True):
        up('按分镜剪辑画面', 54)
        # 第一步已剔除被剪段（cut_plan.removed>0）→ 强制真剪，杜绝「没剪」静默发生
        _cp3 = (progress or {}).get('cut_plan') or {}
        src_video, segs, cut_sec = _cut_video_by_spans(
            video_path, segs, run_dir, progress,
            skip_no_cut=bool(int(_cp3.get('removed') or 0)))
        cut_info['cut_sec'] = cut_sec
        cut_info['segs'] = len(segs)
    cut_info['out_dur'] = round(probe_audio_len(src_video) or cut_info['src_dur'], 2)
    # 等待TTS线程完成
    _tts_thread.join()
    if _tts_error[0]:
        raise _tts_error[0]
    # 用裁剪后的segs计算voice_spans和tts_paths
    tts_paths = []
    voice_spans = {}
    for i, clip in _tts_results:
        seg_span = segs[i] if i < len(segs) else (0.0, 10.0)
        tts_paths.append((clip, seg_span[0], seg_span[1]))
        v_len = probe_audio_len(clip) or max(0.5, seg_span[1] - seg_span[0])
        voice_spans[i] = (seg_span[0], min(seg_span[1], seg_span[0] + v_len + 0.35))
    _log.info(f'[DIAG] TTS并行完成: {len(tts_paths)}/{len(narr)}段配音成功')
    up('混音+烧字幕+配乐', 70)
    # 字幕必须去掉 TTS 标记：narr 里带 {情绪:紧张}/{停顿:0.5}，直接烧进字幕
    # 会在画面里显示成「{/情绪}{/停顿}」（实测成片即如此）。
    narr_srt = ['' if (t or '').strip() in ('（留白）', '(留白)')
                else _clean_caption(strip_tts_markup(t)) for t in narr]
    # [P0-2] 配乐情绪包络：把第一步算好的剧情重要度带进混音阶段。
    params = attach_importance(params, progress, segs)
    final = _compose_narration_video(src_video, segs, narr_srt, tts_paths, run_dir, params,
                                     music_path=music_path, voice_spans=voice_spans)
    if progress:
        progress['done'] = True; progress['pct'] = 100
        progress['file'] = os.path.relpath(final, _w.OUTDIR).replace('\\', '/') if final else ''
        progress['script'] = events
        progress['mode'] = _w.compute_mode(params, needs_chat=True)
    # 保存中间状态（用于增量重生成：改某段解说词只重生成该段）
    try:
        _state = {
            'src_video': os.path.abspath(src_video),
            'segs': [[float(s), float(e)] for s, e in segs],
            'narr': list(narr),
            'tts_paths': [[p, float(s), float(e)] for p, s, e in tts_paths],
            'voice_spans': {str(k): [float(v[0]), float(v[1])] for k, v in voice_spans.items()},
            'music_path': os.path.abspath(music_path) if music_path else None,
            'params': params,
            'final': os.path.abspath(final) if final else None,
        }
        # [P0-1] 增量状态原子写：长视频增量重生成路径若写到一半打断，下一次会从空状态重启
        import json as _json
        _atomic_json_dump(os.path.join(run_dir, 'state.json'), _state)
        _log.info(f'[DIAG] 增量状态已保存: {len(narr)}段')
    except Exception as _e:
        _log.info(f'[DIAG] 增量状态保存失败: {_e}')
    # 剪辑质量自检：对比每段解说词和对应时间段的台词，标记可能不匹配的片段
    quality_report = []
    try:
        for i, txt in enumerate(narr):
            if not txt.strip():
                quality_report.append({'seg': i, 'score': 1.0, 'flag': 'empty'})
                continue
            seg_span = segs[i] if i < len(segs) else (0, 0)
            # 找该时间段内的ASR台词
            seg_asr = [a.get('text', '') for a in asr if a.get('start', 0) >= seg_span[0] - 2
                       and a.get('end', 0) <= seg_span[1] + 2]
            seg_text = ' '.join(seg_asr)
            # 简单关键词重叠度：解说词中的词在台词中出现的比例
            txt_words = set(re.findall(r'[\u4e00-\u9fa5]{2,}|[a-zA-Z]{3,}', txt))
            seg_words = set(re.findall(r'[\u4e00-\u9fa5]{2,}|[a-zA-Z]{3,}', seg_text))
            if txt_words and seg_words:
                overlap = len(txt_words & seg_words) / len(txt_words)
            elif txt_words and not seg_text:
                overlap = 0.0  # 该片段无台词，纯画面
            else:
                overlap = 1.0
            flag = 'ok' if overlap >= 0.1 else ('no_dialogue' if not seg_text else 'mismatch')
            quality_report.append({'seg': i, 'score': round(overlap, 2), 'flag': flag,
                                   'narration': txt[:50], 'asr': seg_text[:50]})
        mismatch_count = sum(1 for q in quality_report if q['flag'] == 'mismatch')
        _log.info(f'[DIAG] 质量自检: {len(quality_report)}段, {mismatch_count}段可能不匹配')
        # 保存质量报告
        _atomic_json_dump(os.path.join(run_dir, 'quality.json'), quality_report)
    except Exception as _qe:
        _log.info(f'[DIAG] 质量自检失败: {_qe}')
    diag = {'events': len(events), 'segments': len(segs), 'asr_lines': len(asr),
            'aligned': sum(1 for x in narr if x.strip()), 'voice_clips': len(tts_paths),
            'narration': narr, 'cut': cut_info,
            'quality': {'mismatch': sum(1 for q in quality_report if q['flag'] == 'mismatch'),
                        'total': len(quality_report), 'report': quality_report}}
    if not final:
        _log.info(f'[DIAG] narrate_movie final为None! video_path={video_path}, tts_only={tts_only}, segs={len(segs)}, narr={len(narr)}, tts_paths={len(tts_paths)}')
    return final, diag


# ---- 公共符号回注宿主 ----
for _sym in (
    # Region 1: 剧情理解 + 解说稿生成
    '_plot_brief', '_try_parse_json', '_beat_plan',
    '_split_nar_lines', '_split_nar_sentences', '_split_nar_clauses',
    '_split_into_k', '_distribute_sents', '_map_lines_to_segs',
    '_seg_visual_captions', '_detect_genre', '_genre_template_block',
    'local_vlm_narrate', '_local_model_available', '_is_weak_vlm',
    '_installed_local_models', '_model_narr_guide',
    'NARR_STYLES', 'DETAIL_LEVELS', 'GENRE_TEMPLATES', '_local_model_cache',
    # Region 2: 解说分段 + 镜头对齐 + 电影合成
    '_segment_timeline', '_local_narrate', '_fill_missing_lines',
    'generate_narration', '_merge_segs', '_max_imp', '_cap_seg_duration',
    '_split_unit_at_gaps', '_condense_segs', '_narrate_candidate_shots',
    '_llm_text', '_asr_text_in', '_algo_align_shots', '_model_align_shots',
    '_expand_shots', '_align_shots_to_lines', '_narrate_analysis',
    '_analyze_narrate', 'narrate_video', 'web_search',
    '_script_from_obj', '_extract_json_obj', '_asr_density',
    '_build_scenes', '_atomic_json_dump', '_load_json_file',
    '_vlm_sample_timeline', '_vlm_sample_captions',
    '_scene_describe', '_scene_dialogue', '_story_parse_chunk',
    '_char_bigrams', '_scene_dialogue_of', '_infer_scene_story',
    '_align_from_story_fill', '_parse_alignment_obj',
    '_llm_align_beats_to_scenes', '_llm_refine_beats_with_scenes',
    '_allocate_script_spans', '_fallback_full_script',
    'llm_movie_full_script', 'llm_movie_script',
    'align_script_to_segments',
    '_narrate_by_plot', '_generate_all_tts', '_edge_probe_recover',
    'compose_movie_from_tts', 'narrate_movie',
    '_IMP_ORDER', 'NAR_BEAT_MIN', 'NAR_BEAT_MAX', 'NAR_SCRIPT_CPS',
    'NAR_SEG_TARGET', 'SCRIPT_STYLE_MOVIE', '_SEMANTIC_ACT_VALUES',
):
    setattr(_w, _sym, globals()[_sym])
