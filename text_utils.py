# -*- coding: utf-8 -*-
"""text_utils: 纯文本处理工具（无状态，无全局依赖）。"""
import re

_TTS_OPEN_RE = re.compile(r'\{(?:情绪|停顿|慢|快|高音|低音|大声|小声)[^}]*\}')
_TTS_CLOSE_RE = re.compile(r'\{/(?:情绪|停顿|慢|快|高音|低音|大声|小声)\}')
_WS_RE = re.compile(r'\s+')
_META_BRACKET_RE = re.compile(r'[（(]\s*(?:画面|镜头|结尾金句|开场|钩子|旁白|字幕|音效|转场)[^）)]{0,60}[）)]')
_SPLIT_RE = re.compile(r'(?<=[。！？，；：、\n\r])')
_CJK_BREAK_RE = re.compile(r'[。！？；]')
_MID_BREAK_RE = re.compile(r'[，、,;:]')
_WS_BREAK_RE = re.compile(r'\s')

def _strip_tts_markup(text):
    """剥离TTS控制标记：{停顿:0.3} {情绪:激动} {慢} {/情绪} 等，供不支持SSML的引擎使用。"""
    if not text:
        return text
    t = _TTS_OPEN_RE.sub('', text)
    t = _TTS_CLOSE_RE.sub('', t)
    return _WS_RE.sub(' ', t).strip()

def _split_long_text(text, max_len=200):
    """将长文本按中文标点切分为子句列表，每个子句 ≤ max_len 字符。
    保留标点在子句末尾。避免 < 5 字符的碎片（合并到前一个子句）。"""
    parts = _SPLIT_RE.split(text)

    clauses = []

    for p in parts:

        p = p.strip()

        if not p:

            continue

        if clauses and len(clauses[-1]) + len(p) <= max_len:

            # 碎片合并到前一个子句

            clauses[-1] += p

        else:

            clauses.append(p)

    # 仍然超长的子句（标点在 max_len 之后）在 max_len 处硬切

    final = []

    for c in clauses:

        while len(c) > max_len:

            # 在 max_len 范围内的最后一个标点处切

            cut = max_len

            for i in range(max_len, max(max_len - 50, 0), -1):

                if c[i - 1] in '。！？，；：、 ':

                    cut = i

                    break

            final.append(c[:cut].strip())

            c = c[cut:].strip()

        if c:

            final.append(c)

    return final

def _clamp_line(text, max_chars):
    """把解说词按【句读】截断到 max_chars 字以内，绝不把句子切在半截词中间。
    优先在句号/感叹/疑问/分号处断开；没有句读时退到逗号/顿号；都没有才硬切。
    超长解说若不截断，配音会被 atrim 在段末腰斩——听众听到一半就没了。"""
    t = (text or '').strip()
    if max_chars is None or max_chars <= 0 or len(t) <= max_chars:
        return t
    window = t[:max_chars]
    for pat in (_CJK_BREAK_RE, _MID_BREAK_RE, _WS_BREAK_RE):
        hits = list(pat.finditer(window))

        if hits:

            cut = hits[-1].end()

            # 断点太靠前（丢掉超过 40% 内容）就不值得断，宁可硬切保留更多信息

            if cut >= max_chars * 0.6:

                return window[:cut].strip()

    return window.strip()

def _clean_caption(text):
    """清洗单条字幕文案：去首尾空白/引号、把内部换行替换为空格、合并多余空格。
    LLM/模板输出偶尔带换行或引号，若原样写入 SRT 会破坏字幕时间轴格式。
    同时剥掉模型误输出的元信息括号（如「（画面：绿色田野+蓝天）」「（结尾金句）」）——
    这类注释一旦进配音，观众会听到「画面绿色田野」，非常出戏；（留白）是功能标记，保留。"""
    if not text:
        return ''
    t = _WS_RE.sub(' ', str(text)).strip()
    if t in ('（留白）', '(留白)'):
        return t
    t = _META_BRACKET_RE.sub('', t)
    t = _TTS_OPEN_RE.sub('', t)
    t = _WS_RE.sub(' ', t).strip()

    # 去掉首尾成对的引号（含中文弯引号）

    for a, b in (('"', '"'), ("'", "'"), ('“', '”'), ('‘', '’')):

        if len(t) >= 2 and t[0] == a and t[-1] == b:

            t = t[1:-1].strip()

    return t

