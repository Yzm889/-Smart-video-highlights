# -*- coding: utf-8 -*-
"""源码钉：模型下载「状态可见性」专项 P0（v1.4.81-ux）。

主人反馈（2026-11-13）：台词模型区下载/选择挤一行、状态只写在 <option> 里；
配音安装「等了 3 分钟，进度也不知道，速度也不知道，就干等，到时来个错误又重下」。

本文件钉住四条判据：状态可见 / 进度可感知 / 结果可信 / 失败可恢复。
尤其钉住「不假装成功」：没有校验证据时必须显示「待校验」，不能凭目录存在
就写「完整无误」。
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS = os.path.join(ROOT, 'static', 'desktop', 'desktop.js')
HTML = os.path.join(ROOT, 'static', 'desktop', 'index.html')
CSS = os.path.join(ROOT, 'static', 'desktop', 'desktop.css')


def _text(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def test_whisper_section_is_two_separate_cards():
    """下载格与选择格必须各自独立，且保留原有 id 不破坏事件绑定。"""
    h = _text(HTML)
    assert 'wsp-grid' in h and h.count('wsp-card"') >= 2, '台词模型区要拆成两个独立格子'
    for el_id in ('wspDlBadge', 'wspDlModel', 'btnWhisperDl', 'whisperPullWrap',
                  'setWhisperModel', 'wspVerifyBadge', 'btnWhisperVerify',
                  'whisperDlStatus', 'whisperLangHint'):
        assert 'id="%s"' % el_id in h, '缺少必需 DOM 节点：' + el_id
    # 下拉后面直接跟完整性徽标（用户要求「在后面提示」）
    assert h.index('id="setWhisperModel"') < h.index('id="wspVerifyBadge"')
    assert '.wsp-grid' in _text(CSS) and 'grid-template-columns' in _text(CSS)


def test_integrity_badge_never_claims_ok_without_evidence():
    """结果可信：完整无误必须有校验证据；无证据降级为待校验；坏模型弹窗。"""
    s = _text(JS)
    assert 'framecut_wsp_verify' in s, '校验结论要持久化，重启后仍可信'
    assert 'rec && rec.ok === true' in s, '「完整无误」必须来自成功校验记录'
    assert '完整无误' in s and '模型有误' in s and '待校验' in s
    assert '校验中…' in s, '新一轮下载/校验开始后必须撤掉上一次的结论，不能挂着旧的「完整无误」'
    assert '_wspSaveVerify(model, false' in s, '失败也要落证据，不能下次又显示已下载'
    assert "st.ready === true && st.selected === m" in s, '仅当前生效模型可用后端就绪检查作为正面证据'
    assert 'asr_model_broken' in s and '_fcRenderModelIssueNotice' in s, '坏模型必须有弹窗'
    assert '重新下载（续传）' in s, '弹窗要给出可点的解决办法'
    assert '点「保存设置」才会生效' in s, '下载 ≠ 设为生效模型，必须说清楚'
    # 不许再出现「有目录就算完整」的写法
    assert '凭目录存在就写「完整无误」' in s  # 约束写进注释，防止后来人改回去


def test_progress_meta_has_numbers_and_eta():
    """进度可感知：% / 已下·总量 / 速度 / 预计剩余 / 已耗时，缺一不可。"""
    s = _text(JS)
    for key in ('_progressMeta', '_fmtDur', '_speedMB', '_fmtMB',
                '预计剩余', '已耗时', 'dl_speed', 'speed_mb_s'):
        assert key in s, '进度数字缺少：' + key
    assert '阶段进度（按耗时估算）' in s, '安装阶段没有字节进度，不许冒充'
    assert '_progressMeta(s2.download_pct' in s, 'Whisper 也要用统一数字行'
    assert '_progressMeta(pct,' in s, '配音也要用统一数字行'


def test_failure_is_resumable_and_never_says_redo():
    """失败可恢复：明确已下部分保留 + 续传按钮 + 不会从头重下。"""
    s = _text(JS)
    assert '_tts2Failed' in s and '▶ 继续（断点续传）' in s, '失败后按钮要改成续传'
    assert '不会从头重下' in s
    assert '继续下载（续传）' in s
    assert '已下部分保留' in s


def test_whisper_dropdown_is_curated_with_recommendation():
    """主人反馈（2026-11-14）：12 个模型挑花眼、根本不知道下哪个。
    下拉必须只列常用清单 + 已下载的，且带推荐标注和常驻选型提示。"""
    s = _text(JS)
    h = _text(HTML)
    assert '_WSP_POPULAR' in s, '必须有常用清单，不能把后端全表甩给用户'
    # 清单外未下载的模型不许再整批列出（old 行为：Object.keys(valid).map 直接全渲染）
    assert "Object.keys(valid).map(" not in s, '下拉不许再按后端全表无脑全渲染'
    assert '清单外未下载的' in s and '不再列出' in s, '精简意图要写进注释防回退'
    # 推荐标注与选型提示缺一不可
    for tag in ('★推荐 · 中文可用', '仅英文 · 英文片首选'):
        assert tag in s, '下拉要带推荐标注：' + tag
    assert 'wspPickHint' in h and '中文视频选 small' in h, '常驻选型提示要存在'
    # 当前生效模型兜底显示，不让用户以为配置丢了
    assert '!order.includes(cur)' in s


def test_cache_bump_is_paired():
    """改了 JS + CSS + HTML，缓存戳必须成对 bump，且不能留旧版本。"""
    h = _text(HTML)
    m = re.findall(r'desktop\.(?:css|js)\?v=(\d+)', h)
    assert len(m) == 2 and len(set(m)) == 1, 'css/js 缓存戳必须成对且一致'
    assert int(m[0]) >= 59, '本轮必须 bump 到 v59 或更高'
