"""「解说驱动剪辑」回归测试。

背景：旧管线是「先按画面切换分段，再把剧情句贴上去」，导致三个硬伤——
  1. 每句硬截断 30/40 字，讲到一半就断（出现「随后追」这种半截话）
  2. 剧情条数与画面段数无关：段少→剧情被丢弃，段多→剧情被复制铺满
  3. 成片恒等于原片时长，谈不上剪辑

新管线反过来：先写完整解说稿（钩子/推进/升华），再按每句字数分配画面。
本文件锁定这些契约，防止改回去。
"""

import pytest

import webui_server as S


# ---------------- 画面分配：解说决定剪辑 ----------------
def test_allocate_longer_text_gets_longer_screen():
    """讲得多的地方必须给更多画面：画面时长 ∝ 解说字数。"""
    spans = S._allocate_script_spans(['短。' * 10, '长' * 80], 60.0)
    assert len(spans) == 2
    d_short = spans[0][1] - spans[0][0]
    d_long = spans[1][1] - spans[1][0]
    assert d_long > d_short * 2, '80 字的节应明显长于 10 字的节：%s' % (spans,)


def test_allocate_count_matches_texts():
    """返回区间数必须等于解说句数，否则字幕与配音会整体错位。"""
    texts = ['第%d句话，大概二十个字左右。' % i for i in range(7)]
    spans = S._allocate_script_spans(texts, 120.0)
    assert len(spans) == len(texts)


def test_allocate_skips_when_script_shorter_than_video():
    """解说总时长短于原片时，成片必须短于原片（这就是自动剪辑）。"""
    # 2 句话约 30 字 → 需要约 6 秒；原片 60 秒 → 应只取约 6 秒
    spans = S._allocate_script_spans(['这是一句十字的解说。', '这是第二句十字的解说。'], 60.0)
    kept = sum(b - a for a, b in spans)
    assert kept < 20.0, '应跳过大部分原片，实际保留 %.1fs' % kept


def test_allocate_compresses_when_script_longer_than_video():
    """原片不够长时整体压缩，且绝不越界（否则 ffmpeg 会失败）。"""
    texts = ['这是一段大约三十个字的解说词内容测试用。' * 2] * 10
    spans = S._allocate_script_spans(texts, 30.0)
    assert len(spans) == 10
    assert spans[-1][1] <= 30.0 + 1e-6, '不得超出原片时长：%s' % (spans[-1],)
    for a, b in spans:
        assert b > a, '压缩后区间长度必须为正：%s' % ((a, b),)


def test_allocate_prefers_regions_with_dialogue():
    """画面应优先取自「有台词」的区间，空镜与过场被跳过。"""
    asr = [{'start': 40.0, 'end': 50.0, 'text': '这里有人在说话说了很长一段台词'}]
    spans = S._allocate_script_spans(['一句解说。', '另一句解说。'], 60.0, asr=asr)
    # 两节都应落在台词区附近，而不是从 0 秒的静默段开始
    assert spans[0][0] > 5.0, '应跳过开头无台词的区间，实际从 %.1fs 开始' % spans[0][0]


def test_allocate_handles_empty():
    assert S._allocate_script_spans([], 60.0) == []
    assert S._allocate_script_spans(['x'], 0) == []


# ---------------- 解说稿：不再硬截断 ----------------
def test_fallback_script_keeps_whole_sentences():
    """兜底切句不得把句子砍成 30 字的半截话（历史 bug：「…随后追」）。"""
    plot = ('副警长瑞克和搭档肖恩在巡逻车里闲聊，随后追击逃犯时中枪昏迷。'
            '瑞克在医院醒来，发现医院空无一人。')
    sc = S._fallback_full_script('行尸走肉', plot)
    assert sc and sc['beats']
    for b in sc['beats']:
        assert not b['text'].endswith('，'), '不应以逗号结尾（说明被截断）'
        assert len(b['text']) <= S.NAR_BEAT_MAX + 10, '单节过长：%d 字' % len(b['text'])
    joined = ''.join(b['text'] for b in sc['beats'])
    assert '瑞克和搭档肖恩在巡逻车里闲聊' in joined, '整句内容应保留'


def test_extract_json_obj_tolerates_surrounding_text():
    """模型常在 JSON 前后加废话或 ```json 包裹，必须能抠出来。"""
    obj = S._extract_json_obj('好的，如下：\n```json\n{"a": 1, "b": [1,2]}\n```\n希望满意')
    assert obj == {'a': 1, 'b': [1, 2]}
    assert S._extract_json_obj('完全没有 JSON') is None


def test_script_from_obj_normalizes_beats():
    sc = S._script_from_obj({
        'title': 'X',
        'hook': '开场钩子',
        'beats': [{'text': '第一节', 'keywords': '单个关键词', 'importance': '未知值'},
                  {'text': '第二节', 'keywords': ['a', 'b'], 'importance': 'key'}],
        'outro': '结尾',
    })
    assert sc['hook'] == '开场钩子' and sc['outro'] == '结尾'
    assert sc['beats'][0]['keywords'] == ['单个关键词']
    assert sc['beats'][0]['importance'] == 'advance', '非法 importance 应归一'
    assert sc['beats'][1]['importance'] == 'key'
    assert S._script_from_obj({'beats': []}) is None


def test_full_script_uses_model_json(monkeypatch):
    """模型返回合法 JSON 时，必须用它，而不是回退到剧情切句。"""
    model_json = ('{"title":"行尸走肉","hook":"开场钩子内容","beats":'
                  '[{"text":"第一节解说词内容够长","keywords":["瑞克"],"importance":"key"}],'
                  '"outro":"结尾升华"}')
    monkeypatch.setattr(S, 'local_llm_enabled', lambda: True)
    monkeypatch.setattr(S, 'local_llm_ping', lambda: (True, 'ok'))
    monkeypatch.setattr(S, 'local_llm_chat', lambda *a, **k: model_json)
    sc = S.llm_movie_full_script('行尸走肉', '一段剧情')
    assert sc['hook'] == '开场钩子内容'
    assert sc['beats'][0]['text'] == '第一节解说词内容够长'
    assert sc['outro'] == '结尾升华'


def test_full_script_falls_back_when_model_breaks(monkeypatch):
    """模型吐垃圾时走切句兜底，且必须仍然出稿（不能抛异常）。"""
    monkeypatch.setattr(S, 'local_llm_enabled', lambda: True)
    monkeypatch.setattr(S, 'local_llm_ping', lambda: (True, 'ok'))
    monkeypatch.setattr(S, 'local_llm_chat', lambda *a, **k: '抱歉，我无法完成这个请求')
    sc = S.llm_movie_full_script('行尸走肉', '瑞克中枪昏迷。瑞克在医院醒来。')
    assert sc and sc['beats'], '兜底也必须出稿'


def test_clean_caption_strips_meta_annotations():
    """模型常把「（画面：绿色田野+蓝天）」这类拍摄说明写进正文。
    它一旦进配音，观众就会听到「画面绿色田野」，必须剥掉；「（留白）」是功能标记要保留。"""
    assert S._clean_caption('他走出门。（画面：远景·雨夜）外面下着雨。') == '他走出门。外面下着雨。'
    assert S._clean_caption('（结尾金句）有些风景终究要穿过荒芜。') == '有些风景终究要穿过荒芜。'
    assert S._clean_caption('（镜头：特写）他握紧了拳头。') == '他握紧了拳头。'
    assert S._clean_caption('（留白）') == '（留白）', '留白是功能标记，不能洗掉'
    assert S._clean_caption('普通的一句解说词。') == '普通的一句解说词。'


def test_no_hard_30char_truncation_in_beat_length():
    """新标准：单节应在 40~90 字区间，而不是旧的 30/40 字硬截断。"""
    assert S.NAR_BEAT_MIN >= 40, '旧实现把每句砍到 30 字，讲不成故事'
    assert S.NAR_BEAT_MAX >= 90


@pytest.mark.parametrize('target,lo,hi', [(60, 4, 12), (300, 12, 40), (None, None, None)])
def test_target_sec_controls_beat_count(monkeypatch, target, lo, hi):
    """目标时长应反馈到「写多少节」，避免 3 分钟片只写 3 句或写 200 句。"""
    seen = {}

    def fake_chat(prompt, *a, **k):
        seen['prompt'] = prompt
        return '{"beats":[{"text":"一" * 50}]}'

    monkeypatch.setattr(S, 'local_llm_enabled', lambda: True)
    monkeypatch.setattr(S, 'local_llm_ping', lambda: (True, 'ok'))
    monkeypatch.setattr(S, 'local_llm_chat', fake_chat)
    S.llm_movie_full_script('X', '剧情', target_sec=target)
    if target is None:
        assert '这一版请写成约' not in seen.get('prompt', '')
    else:
        import re
        m = re.search(r'这一版请写成约 (\d+) 节', seen.get('prompt', ''))
        assert m, '目标时长应折算成节数：%s' % seen.get('prompt', '')[-120:]
        assert lo <= int(m.group(1)) <= hi, '节数 %s 不在合理区间' % m.group(1)
