# -*- coding: utf-8 -*-
"""硬字幕 / 水印污染防护回归（分册 B · P0）。

守三条契约：
  1. VLM 从画面抄回的台标 / 水印 / 推广语 / 臆造残句不得进入台词轨；
  2. 抄录提示词必须明确「只抄底部字幕带、忽略台标水印、不要编造」；
  3. 剧情理解提示词必须区分「语音台词」与「画面字幕」的可信度；
     来源标注只进提示词，不破坏 per_seg 的 3 元组结构（下游 20+ 处直接解包）。

不用真实 VLM / 模型，全部 monkeypatch，纯 CPU 秒级。
"""
import webui_server as S  # noqa: F401  必须先导入宿主，规避循环导入
import movie_narrator as mn


def test_clean_onscreen_text_keeps_dialogue_drops_junk():
    """正常对话保留；台标/推广语/纯符号/过短/无字幕标记一律丢弃。"""
    assert mn._clean_onscreen_text('你到底是谁？') == '你到底是谁？'
    assert mn._clean_onscreen_text('小姑娘？别怕，我是警察。') == '小姑娘？别怕，我是警察。'
    for junk in ('点点关注', '图灵影视 出品', '欢迎点赞订阅', '更多精彩',
                 '（无字幕）', '(无字幕)', '无', '？', '12345', '   ', '', None):
        assert mn._clean_onscreen_text(junk) == '', junk
    # 用户自定义词表（ai_config.json 的 onscreen_blacklist）
    assert mn._clean_onscreen_text('自定义台标', extra=['自定义台标']) == ''
    assert mn._clean_onscreen_text('正常对话', extra=['自定义台标']) == '正常对话'


def test_read_onscreen_prompt_forbids_watermarks(monkeypatch):
    """抄录提示词必须约束位置与内容，否则 VLM 会把四角台标也抄成台词。"""
    seen = {}

    def fake_vlm(images, prompt, **k):
        seen['prompt'] = prompt
        return '第1段: (无字幕)'

    monkeypatch.setattr(mn, 'vlm_chat_multi', fake_vlm)
    monkeypatch.setattr(mn, 'load_ai_config', lambda: {})
    mn._read_onscreen_subtitles({0: b'x'}, [(0, 10, '')])
    p = seen['prompt']
    assert '底部中央字幕带' in p
    assert '台标' in p and '一律忽略' in p
    assert '不要编造' in p


def test_read_onscreen_subtitles_drops_watermark_lines(monkeypatch):
    """解析出口要拦住水印行，只把真实对话写进台词轨。"""
    monkeypatch.setattr(
        mn, 'vlm_chat_multi',
        lambda images, prompt, **k:
        '第1段: 点点关注不迷路\n第2段: 你到底是谁？\n第3段: 图灵影视')
    monkeypatch.setattr(mn, 'load_ai_config', lambda: {})
    got = mn._read_onscreen_subtitles(
        {0: b'a', 1: b'b', 2: b'c'},
        [(0, 10, ''), (10, 20, ''), (20, 30, '')])
    assert got == {1: '你到底是谁？'}


def test_read_onscreen_subtitles_honors_config_blacklist(monkeypatch):
    """用户自定义黑名单生效，且 load_ai_config 抛错时不影响出片。"""
    monkeypatch.setattr(mn, 'vlm_chat_multi',
                        lambda images, prompt, **k: '第1段: 某某影视')
    monkeypatch.setattr(mn, 'load_ai_config', lambda: {'onscreen_blacklist': ['某某影视']})
    assert mn._read_onscreen_subtitles({0: b'a'}, [(0, 10, '')]) == {}

    def _boom():
        raise RuntimeError('config missing')
    monkeypatch.setattr(mn, 'load_ai_config', _boom)
    monkeypatch.setattr(mn, 'vlm_chat_multi',
                        lambda images, prompt, **k: '第1段: 正常对话')
    assert mn._read_onscreen_subtitles({0: b'a'}, [(0, 10, '')]) == {0: '正常对话'}


def test_plot_brief_marks_onscreen_source(monkeypatch):
    """剧情理解提示词要标注来源并压低画面字幕的可信度。"""
    seen = {}

    def fake_vlm(images, prompt, **k):
        seen['prompt'] = prompt
        return '这是一段剧情梗概。'

    monkeypatch.setattr(mn, 'vlm_chat_multi', fake_vlm)
    monkeypatch.setattr(mn, '_scan_full_visual', lambda *a, **k: {})
    per_seg = [(0, 10, '你到底是谁？'), (10, 20, '别开枪')]
    params = {'_dlg_src': ['onscreen', 'asr'], '_progress': None}
    mn._plot_brief({0: b'a', 1: b'b'}, per_seg, params)
    p = seen['prompt']
    assert '画面字幕（可能含错字，仅作辅助）' in p
    assert '不得据此推断片名' in p
    assert '0-10秒 画面字幕' in p
    assert '10-20秒 台词' in p
    # per_seg 结构未被改写
    assert all(len(x) == 3 for x in per_seg)


def test_condense_segs_marks_source_without_breaking_3tuple(monkeypatch):
    """ASR 永远优先；只有被画面字幕补位的段标 onscreen；per_seg 仍是 3 元组。"""
    captured = {}
    monkeypatch.setattr(mn, '_local_model_available', lambda: True)
    monkeypatch.setattr(mn, '_read_onscreen_subtitles',
                        lambda frames, per_seg: {0: '正常对话字幕'})
    monkeypatch.setattr(mn, '_scan_full_visual', lambda *a, **k: {})

    def fake_plot_brief(frames, per_seg, params):
        captured['per_seg'] = list(per_seg)
        captured['src'] = list(params.get('_dlg_src') or [])
        return '剧情梗概'

    monkeypatch.setattr(mn, '_plot_brief', fake_plot_brief)
    monkeypatch.setattr(
        mn, '_beat_plan',
        lambda per_seg, plot, params:
        {'summary': '', 'beats': [{'i': i + 1, 'importance': 'advance', 'role': ''}
                                  for i in range(len(per_seg))]})
    mn._condense_segs([(0.0, 10.0), (10.0, 20.0)], [], {},
                      frames={0: b'a', 1: b'b'})
    assert captured['per_seg'], '未走到剧情理解分支'
    assert all(len(x) == 3 for x in captured['per_seg']), 'per_seg 结构被破坏'
    assert captured['per_seg'][0][2] == '正常对话字幕'
    assert captured['src'] == ['onscreen', 'none']
