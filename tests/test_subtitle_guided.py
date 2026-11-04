# -*- coding: utf-8 -*-
"""字幕导入（SRT）驱动画面抽样与台词轨 —— 契约测试。

【这个功能的两个价值，也是这两组断言的由来】

1. **台词轨变准**：人工字幕是校对过的原文，比 Whisper 听写的准。落成与视频同名的
   字幕文件后，ai_providers 既有逻辑「字幕优先于 ASR」自动接管，不用改配音/对齐/写稿。

2. **画面抽样落点变对**：这是真正的突破点。旧抽样是固定网格（每 interval 秒一个点），
   网格与剧情没有因果关系——抽到黑屏、过场、空镜就是「空无一人」，写出来的剧情
   自然对不上。有字幕时间轴后，每个网格点会被**吸附**到附近的台词起点，
   同样多的帧，看的地方全变了。

【两条必须钉死的约束】
A. 没有字幕时，输出必须与改动前**逐点一致**（吸附是纯增益，不许改既有行为）。
B. 吸附不许降低覆盖率：点数不变、最大空档不显著变大。
   初版写成「每个台词起点都抽一帧」，真机一测就废——台词密集时点数暴涨（VLM 阶段
   从 3 分钟变 20 分钟），台词稀疏（一条字幕覆盖全片）时点数反掉到个位数，
   **覆盖率比原来还差**。这里用断言把那次回归钉住。
"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import webui_server as W  # noqa: E402
import movie_narrator as M  # noqa: E402
import ai_providers as A  # noqa: E402


# --------------------------------------------------------------------------
# 一、字幕吸附：纯函数层（不碰 ffmpeg / 模型）
# --------------------------------------------------------------------------

def _grid(vdur, interval):
    out, t = [], interval / 2.0
    while t < vdur:
        out.append(t)
        t += interval
    return out


def test_no_subtitle_returns_empty_so_grid_is_used():
    """没有台词 → 返回空，调用方用纯网格。这是「零影响」的第一道闸门。"""
    assert M._dialogue_anchor_times([], 120.0, 12.0, True) == ([], '')
    # 有 seg 但文本全空（ASR 常产出这种静默段）
    blank = [{'start': 1.0, 'end': 3.0, 'text': ''}, {'start': 5.0, 'end': 6.0, 'text': '   '}]
    assert M._dialogue_anchor_times(blank, 120.0, 12.0, True) == ([], '')


def test_anchor_count_matches_grid_count():
    """**核心约束 A**：点数必须与网格一致，VLM 成本与耗时不因加字幕而上涨。

    初版「每个台词起点都抽一帧」在台词密集时会产出上百个点，就是这里拦下的。
    """
    vdur, interval = 600.0, 12.0
    dense = [{'start': i * 2.0, 'end': i * 2.0 + 1.8, 'text': '台词%d' % i}
             for i in range(280)]
    times, tag = M._dialogue_anchor_times(dense, vdur, interval, True)
    n_grid = len(_grid(vdur, interval))
    assert tag == 'snap'
    assert abs(len(times) - n_grid) <= 1, \
        '台词密集时点数应≈网格数(%d)，实际 %d —— 抽样成本失控' % (n_grid, len(times))


def test_sparse_subtitle_does_not_shrink_coverage():
    """**核心约束 B**：一条字幕覆盖全片时，不许退化成个位数点。

    这是初版最严重的缺陷：稀疏台词下点数掉到 2 个，全片只看两帧。
    """
    vdur, interval = 600.0, 12.0
    sparse = [{'start': 0.0, 'end': 600.0, 'text': '全片一句台词'}]
    times, _tag = M._dialogue_anchor_times(sparse, vdur, interval, True)
    n_grid = len(_grid(vdur, interval))
    assert len(times) >= n_grid - 1, \
        '稀疏字幕下点数(%d)显著少于网格(%d)，覆盖率倒退' % (len(times), n_grid)


def test_anchors_land_on_dialogue_starts():
    """有台词的地方，吸附后必须更靠近台词起点（否则等于没吸附）。

    注意断言的是「**半径内**有点可吸附的网格点」，不是全部：片尾没人说话的那几秒
    必须留在网格原位（空镜也得看一眼），强行吸到远处的台词上反而会错位。
    """
    vdur, interval = 240.0, 12.0
    # 台词起点故意放在网格点之间（网格是 6/18/30/42...，台词在 9/21/33/45...）
    asr = [{'start': 6.0 + i * 12.0 + 3.0, 'end': 6.0 + i * 12.0 + 8.0, 'text': '第%d句' % i}
           for i in range(18)]
    times, _tag = M._dialogue_anchor_times(asr, vdur, interval, True)
    grid = _grid(vdur, interval)
    starts = [s['start'] for s in asr]

    def _nearest(ts):
        return min((abs(ts - s), s) for s in starts)

    moved = sum(1 for g, t in zip(grid, times) if abs(g - t) > 1e-6)
    assert moved > len(grid) * 0.5, '超过一半的点没有吸附到台词，功能等于没生效'

    # 只要求「半径内有台词的网格点」吸附成功
    rad = interval * 0.45
    for g, t in zip(grid, times):
        if _nearest(g)[0] <= rad:
            assert _nearest(t)[0] <= 1.0, \
                '网格点 %.1fs 附近有台词却未被吸附（实际落在 %.1fs，最近台词 %.1fs）' \
                % (g, t, _nearest(t)[1])


def test_max_gap_does_not_blow_up():
    """吸附后最大空档不得明显大于网格间隔（否则会漏掉整段剧情）。"""
    vdur, interval = 600.0, 12.0
    # 只有前后两段有台词，中间大段静默
    asr = [{'start': 5.0, 'end': 40.0, 'text': '开头台词'},
           {'start': 560.0, 'end': 595.0, 'text': '结尾台词'}]
    times, _tag = M._dialogue_anchor_times(asr, vdur, interval, True)
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert max(gaps) <= interval * 1.6, \
        '最大空档 %.1fs 超过网格 %.1fs 的 1.6 倍，会漏看整段画面' % (max(gaps), interval)


def test_anchors_are_sorted_within_bounds():
    """升序、去重、全部落在 (0, vdur) 内 —— 越界会让 ffmpeg seek 失败。"""
    vdur, interval = 100.0, 12.0
    messy = [{'start': 90.0, 'end': 99.0, 'text': '末尾'},
             {'start': 3.0, 'end': 6.0, 'text': '开头'},
             {'start': 50.0, 'end': 52.0, 'text': '中间'}]
    times, _tag = M._dialogue_anchor_times(messy, vdur, interval, True)
    assert times == sorted(times), '采样点未排序'
    assert len(times) == len(set(times)), '采样点有重复'
    assert all(0 < t < vdur for t in times), '采样点越界: %r' % (times,)


def test_short_video_falls_back_to_grid_safely():
    """极短素材（interval > vdur）：网格为空时吸附返回空，交给调用方退回网格。

    这是刻意的分工：`_vlm_sample_timeline` 在算 interval 时已有短片段保护
    （interval >= vdur 时把它压成 vdur/4），吸附函数不该重复造点。
    这里锁的是「返回空、不抛异常」，而不是「必须产出点」——如果吸附函数自己
    硬造点，会和短片段保护打架，产出两套时间网格。
    """
    times, tag = M._dialogue_anchor_times(
        [{'start': 0.5, 'end': 2.0, 'text': '你好'}], 3.0, 12.0, True)
    assert times == [] and tag == '', 'interval>vdur 时应返回空，交由调用方保护'

    # 短片段保护把 interval 压到 vdur/4 之后，吸附必须正常工作
    times2, tag2 = M._dialogue_anchor_times(
        [{'start': 0.5, 'end': 2.0, 'text': '你好'}], 3.0, 0.75, True)
    assert times2 and tag2 == 'snap', '短片段保护生效后吸附应产出点'
    assert all(0 < t < 3.0 for t in times2)


# --------------------------------------------------------------------------
# 二、字幕落盘：与视频同名同目录，让既有「字幕优先」逻辑自动接管
# --------------------------------------------------------------------------

SRT = """1
00:00:01,000 --> 00:00:03,500
第一句台词

2
00:00:05,000 --> 00:00:07,200
第二句台词
"""


def test_save_subtitle_writes_sibling_file(tmp_path):
    """落成 src.srt（与视频同名），而不是随便一个名字。"""
    vid = tmp_path / 'src.mp4'
    vid.write_bytes(b'\x00' * 32)
    dst = W._save_user_subtitle({'name': 'whatever.srt', 'text': SRT}, str(vid))
    assert dst, '有效字幕居然没落盘'
    assert os.path.basename(dst) == 'src.srt', '字幕必须与视频同名，否则自动识别找不到'
    assert os.path.isfile(dst)


def test_saved_subtitle_is_picked_up_by_asr_layer(tmp_path):
    """**端到端闸门**：落盘后，ai_providers 的字幕优先逻辑必须真的找到它。

    这一条如果不成立，整个功能就是空转——用户传了字幕，跑的却还是 Whisper。
    """
    vid = tmp_path / 'src.mp4'
    vid.write_bytes(b'\x00' * 32)
    W._save_user_subtitle({'name': 'x.srt', 'text': SRT}, str(vid))
    segs = A._external_subtitle_segments(str(vid))
    assert segs, 'ASR 层没找到刚落盘的字幕 —— 字典优先链路断裂'
    assert len(segs) == 2
    assert segs[0]['text'] == '第一句台词'
    assert abs(segs[0]['start'] - 1.0) < 0.01


def test_gbk_subtitle_is_decoded(tmp_path):
    """国内字幕大量是 GBK。当成 utf-8 读会全是乱码 —— 等于没有字幕。"""
    vid = tmp_path / 'src.mp4'
    vid.write_bytes(b'\x00' * 32)
    gbk = SRT.encode('gb18030')
    dst = W._save_user_subtitle({'name': 'a.srt', 'data': __import__('base64')
                                 .b64encode(gbk).decode()}, str(vid))
    assert dst, 'GBK 字幕解码失败'
    segs = A.parse_subtitle_file(dst)
    assert segs and '第一句台词' in segs[0]['text'], 'GBK 字幕解出来是乱码: %r' % segs


def test_garbage_subtitle_is_rejected_and_removed(tmp_path):
    """纯文本/坏文件：不许落盘残留，否则会顶掉正常的 ASR 台词轨。"""
    vid = tmp_path / 'src.mp4'
    vid.write_bytes(b'\x00' * 32)
    dst = W._save_user_subtitle({'name': 'bad.srt', 'text': '这里只是普通文字，没有时间轴'},
                                str(vid))
    assert dst is None
    assert not os.path.exists(str(tmp_path / 'src.srt')), '坏字幕残留在了磁盘上'


def test_no_subtitle_object_is_noop(tmp_path):
    """没传字幕 → 什么都不做（零影响契约）。"""
    vid = tmp_path / 'src.mp4'
    vid.write_bytes(b'\x00' * 32)
    assert W._save_user_subtitle(None, str(vid)) is None
    assert W._save_user_subtitle({}, str(vid)) is None
    assert not os.path.exists(str(tmp_path / 'src.srt'))


def test_ass_subtitle_keeps_ass_extension(tmp_path):
    """ASS 保留 .ass 后缀：解析器按内容分支（Dialogue: 行），改名成 srt 也不会错，
    但保留原后缀更符合「同目录外挂字幕」的直觉。"""
    vid = tmp_path / 'src.mp4'
    vid.write_bytes(b'\x00' * 32)
    ass = ("[Script Info]\nTitle: t\n\n[Events]\n"
           "Format: Layer, Start, End, Style, Name, ML, MR, MV, Effect, Text\n"
           "Dialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,你好世界\n")
    dst = W._save_user_subtitle({'name': 'a.ass', 'text': ass}, str(vid))
    assert dst and dst.endswith('.ass'), 'ASS 后缀被改掉了: %r' % dst
    segs = A.parse_subtitle_file(dst)
    assert segs and '你好世界' in segs[0]['text']


# --------------------------------------------------------------------------
# 三、字段名契约：subtitle 已被字幕样式占用，字幕文件必须走 subtitle_file
# --------------------------------------------------------------------------

def test_subtitle_field_does_not_collide_with_style_params():
    """params.subtitle 是**字幕样式**（字号/颜色/位置），不是字幕文件。

    历史包袱：这两个概念都叫 subtitle。用同一个键会让「导入字幕」把样式覆盖掉，
    表现为「传了字幕，字幕变成默认样式」。这条断言防止有人图省事合并两者。
    """
    src = open(os.path.join(ROOT, 'workflows.py'), encoding='utf-8').read()
    assert "_save_user_subtitle(req.get('subtitle_file')" in src, \
        '字幕文件必须从 subtitle_file 取，不能与 subtitle(样式) 混用'
    assert "_save_user_subtitle(req.get('subtitle')" not in src, \
        '有人把字幕文件接到 subtitle(样式) 上了，会覆盖用户字幕样式'


# --------------------------------------------------------------------------
# 四、字幕-视频「版本一致性」校验
#
# 【为什么需要这一节】字幕路线不幻觉，但它有一个专属失效模式：**错版**。
# 内容全对、时间对不上。典型三种：
#   · 视频无删减、字幕是删减版 → 后半段没台词
#   · 视频是 WEB 版（片头有平台 logo+片头曲）、字幕是 BD 版 → 全部台词整体偏移
#   · 帧率不同（25 vs 23.976）→ 越往后错得越多（本地查不出来，不做假装）
# 前两种能靠数值抓出来。抓不住的表现就是「成片后半段莫名其妙 / 旁白与画面错位」，
# 而界面上只显示一句 ✅「台词来源：外挂字幕（比语音识别更准）」。
#
# 阈值必须两头兼顾：漏判会让成片错，误报会让人习惯性无视警告（比不报更糟）。
# 所以「片尾正常长空镜」必须静默，「整 90 秒偏移」必须报警。
# --------------------------------------------------------------------------

VD = 560.0   # 与 _e2e_slice.mp4 实测时长一致（560.18s）


def _mk(n, step=3.0, off=0.0, dur=2.4):
    return [{'start': off + i * step, 'end': off + i * step + dur, 'text': '台词%d' % i}
            for i in range(n)]


@pytest.fixture()
def real_video():
    """真实视频路径：SDK 里那个 9 分 20 秒的切片。不存在就跳过。"""
    p = os.path.join(ROOT, '_e2e_slice.mp4')
    if not os.path.isfile(p):
        pytest.skip('缺少 _e2e_slice.mp4（真实时长探测用）')
    return p


def test_normal_subtitle_is_silent(real_video):
    """正常字幕必须完全静默 —— 误报会让用户习惯性无视警告。"""
    level, msgs, _g = A.subtitle_sync_report(_mk(186), real_video)
    assert level == 'ok', '正常字幕被误报为 %s: %r' % (level, msgs)
    assert not msgs


def test_normal_tail_gap_is_silent(real_video):
    """片尾长空镜（无人声镜头/字幕表/片尾曲）是常态，差 20 多秒不许报。"""
    level, msgs, gap = A.subtitle_sync_report(_mk(180), real_video)
    assert gap > 15, '构造前提不成立：应有一段片尾缺口，实际 %.1fs' % gap
    assert level == 'ok', '片尾正常空档被误报: %r' % (msgs,)


def test_truncated_subtitle_is_bad(real_video):
    """删减版字幕：只覆盖前半段 → 必须报 bad，且文案要指出「后半段没台词依据」。"""
    level, msgs, _g = A.subtitle_sync_report(_mk(90), real_video)
    assert level == 'bad'
    assert any('后半段' in m and '删减' in m for m in msgs), \
        '报错文案没点明「删减版」，用户不知道该怎么办: %r' % (msgs,)


def test_missing_opening_is_bad(real_video):
    """片头缺失（字幕从 90s 才开始）→ 整体偏移，必须报 bad。

    整 90 秒恰好是最典型的偏移量，卡在边界上漏判等于这道检查白做，
    所以断言用的是「>= 90」，测试也照着边界造数据。
    """
    level, msgs, _g = A.subtitle_sync_report(_mk(150, off=90.0), real_video)
    assert level == 'bad', '片头缺 90 秒被判 %s，最危险的错版被放过了' % level
    assert any('整体偏移' in m for m in msgs), '文案没点明「整体偏移」: %r' % (msgs,)


def test_head_shift_does_not_double_report_tail(real_video):
    """头部偏移会让片尾自然提前。同一病根不许报两条（用户会以为是两个问题）。"""
    _level, msgs, _g = A.subtitle_sync_report(_mk(150, off=90.0), real_video)
    tail_msgs = [m for m in msgs if '早结束' in m]
    assert not tail_msgs, '头部偏移时又重复报了尾部缺口: %r' % (tail_msgs,)


def test_broken_timeline_is_warned(real_video):
    """结束≤开始 的脏数据 → 提示但不算致命（解析仍可用，只是这些锚点不可信）。"""
    segs = [{'start': 10, 'end': 5, 'text': '坏A'}, {'start': 20, 'end': 25, 'text': '好'},
            {'start': 30, 'end': 30, 'text': '坏B'}] + _mk(180)
    level, msgs, _g = A.subtitle_sync_report(segs, real_video)
    assert level in ('warn', 'bad')
    assert any('时间异常' in m for m in msgs), '脏时间轴未被提示: %r' % (msgs,)


def test_subtitle_longer_than_video_is_bad(real_video):
    """未删减版字幕配删减版视频：成批台词起点落在视频结束之后，必须报 bad。

    【这条是端到端跑出来才发现的】原实现只有 `tail_gap = vdur - last` 一个方向，
    字幕比视频长时 tail_gap 变负数，①②③④ 四个分支一个都不进 —— 完全静默。
    而这正是错版的镜像情形：对白与画面的对应关系整体错位，界面却显示 ✅。
    """
    level, msgs, _g = A.subtitle_sync_report(_mk(220), real_video)   # 到 657s，超 97s
    assert level == 'bad', '字幕比视频长被判 %s，这一类错版被完全放过了' % level
    assert any('比视频长' in m for m in msgs), '文案没点明「字幕比视频长」: %r' % (msgs,)


def test_few_stray_lines_beyond_video_are_silent(real_video):
    """末尾一两条时间轴乱写的译者鸣谢（如 99:59:59）不许把正常字幕判成错版。"""
    segs = _mk(186)
    segs.append({'start': 99 * 3600.0, 'end': 99 * 3600.0 + 4, 'text': '本字幕由某某制作'})
    level, msgs, _g = A.subtitle_sync_report(segs, real_video)
    assert level == 'ok', '单条野数据把正常字幕误判成 %s: %r' % (level, msgs)


def test_no_video_duration_means_no_judgement(tmp_path):
    """拿不到视频时长时不瞎判 —— 宁可不说，也不要凭猜测给出「字幕不匹配」。"""
    bogus = str(tmp_path / 'nope.mp4')
    msg_segs = _mk(50)
    level, msgs, _g = A.subtitle_sync_report(msg_segs, bogus)
    assert level == 'ok' and not msgs, '探测不到时长却给了结论: %r' % (msgs,)


def test_empty_segments_are_safe(real_video):
    """空台词不许炸（ASR 失败、字幕全空都会走到这里）。"""
    assert A.subtitle_sync_report([], real_video)[0] == 'ok'
    assert A.subtitle_sync_report(None, real_video)[0] == 'ok'


def test_sync_report_reaches_task_warnings(monkeypatch, real_video):
    """校验结果必须真的进到任务 warnings —— 否则用户永远看不到，等于没做。"""
    got = []
    monkeypatch.setattr(A, '_note_progress_warning', lambda m: got.append(m))
    A._report_subtitle_sync(_mk(90), real_video)          # 删减版字幕
    assert got, '错版字幕没有产生任何用户可见提示（静默失败）'
    assert any('后半段' in m for m in got)

    got.clear()
    A._report_subtitle_sync(_mk(186), real_video)         # 正常字幕
    assert not got, '正常字幕产生了噪音提示: %r' % (got,)
