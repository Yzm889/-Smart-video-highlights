# -*- coding: utf-8 -*-
"""S4 辅助任务降级 8B 轻量模型：local_llm_light_cfg/chat 配置与调用、
_llm_light 路由（轻量→主模型回退）、辅助任务（判型/详略/剧情理解/对齐）走轻量、
写稿主通道不降级。"""
import pytest
import webui_server as S  # noqa: F401  先建立 webui 入口，再导入 movie_narrator（避免循环依赖）
_S_ENTRY = S  # 显式引用：仅依赖 import webui_server 的副作用保证模块加载顺序
import ai_providers as A
import movie_narrator as M


class TestLightCfg:
    def test_cfg_none_when_unset(self, monkeypatch):
        """未配置 light_model → None（调用方回退主模型，零行为变化）。"""
        monkeypatch.setattr(A, 'load_ai_config',
                            lambda: {'local': {'enabled': True, 'model': 'qwen3:14b'}})
        assert A.local_llm_light_cfg() is None

    def test_cfg_reads_light_model(self, monkeypatch):
        """配置 light_model 后返回独立端点配置，主模型字段不受影响。"""
        monkeypatch.setattr(A, 'load_ai_config',
                            lambda: {'local': {'model': 'qwen3:14b', 'light_model': 'qwen3:8b'}})
        c = A.local_llm_light_cfg()
        assert c is not None
        assert c['model'] == 'qwen3:8b'
        assert c['base_url'].endswith('/v1')

    def test_light_chat_raises_when_unset(self, monkeypatch):
        """未配置轻量模型时调用必须显式抛错（而非静默用主模型，保持调用方回退语义）。"""
        monkeypatch.setattr(A, 'load_ai_config', lambda: {})
        with pytest.raises(RuntimeError):
            A.local_llm_light_chat('hi')


class TestLlmLightRouter:
    def test_light_success_used(self, monkeypatch):
        """轻量模型可用 → 直接使用轻量结果。"""
        calls = []
        monkeypatch.setattr(M, 'local_llm_light_chat',
                            lambda prompt, system=None, timeout=180: calls.append(1) or '轻量结果')
        monkeypatch.setattr(M, '_llm_text', lambda prompt, system='', timeout=180: '主模型结果')
        assert M._llm_light('p') == '轻量结果'
        assert len(calls) == 1

    def test_light_failure_falls_back(self, monkeypatch):
        """轻量模型调用抛异常 → 回退主模型 _llm_text。"""
        def boom(prompt, system=None, timeout=180):
            raise RuntimeError('light down')
        monkeypatch.setattr(M, 'local_llm_light_chat', boom)
        monkeypatch.setattr(M, '_llm_text', lambda prompt, system='', timeout=180: '主模型结果')
        assert M._llm_light('p') == '主模型结果'

    def test_light_blank_falls_back(self, monkeypatch):
        """轻量模型返回空白 → 视为不可用，回退主模型。"""
        monkeypatch.setattr(M, 'local_llm_light_chat',
                            lambda prompt, system=None, timeout=180: '   ')
        monkeypatch.setattr(M, '_llm_text', lambda prompt, system='', timeout=180: '主模型结果')
        assert M._llm_light('p') == '主模型结果'

    def test_env_switch_off(self, monkeypatch):
        """NARRATE_LIGHT_LLM=0 整体关闭：辅助任务直接走主模型，不发起轻量调用。"""
        monkeypatch.setenv('NARRATE_LIGHT_LLM', '0')
        try:
            calls = []
            monkeypatch.setattr(M, 'local_llm_light_chat',
                                lambda prompt, system=None, timeout=180: calls.append(1) or '轻量结果')
            monkeypatch.setattr(M, '_llm_text', lambda prompt, system='', timeout=180: '主模型结果')
            assert M._llm_light('p') == '主模型结果'
            assert len(calls) == 0
        finally:
            monkeypatch.delenv('NARRATE_LIGHT_LLM', raising=False)


class TestAuxTasksUseLight:
    def test_detect_genre_via_light(self, monkeypatch):
        """题材判型走轻量通道。"""
        monkeypatch.setattr(M._w, 'local_llm_enabled', lambda: True)
        monkeypatch.setattr(M, '_llm_light',
                            lambda prompt, system='', timeout=180: '悬疑/烧脑/反转')
        assert M._detect_genre('一段关于密室逃生的故事') == 'suspense'

    def test_detect_genre_light_failure_empty(self, monkeypatch):
        """判型轻量失败 → 返回空串（不套题材模板，不阻断流程）。"""
        monkeypatch.setattr(M._w, 'local_llm_enabled', lambda: True)
        def boom(prompt, system='', timeout=180):
            raise RuntimeError('down')
        monkeypatch.setattr(M, '_llm_light', boom)
        assert M._detect_genre('剧情') == ''

    def test_beat_plan_via_light(self, monkeypatch):
        """详略规划走轻量通道，输出正确解析为 beats。"""
        monkeypatch.setattr(M, '_local_model_available', lambda: True)
        monkeypatch.setattr(M, '_llm_light',
                            lambda prompt, system='', timeout=240:
                            '{"summary":"梗概","beats":[{"i":1,"importance":"advance","role":"推进"}]}')
        per_seg = [(0.0, 5.0, '对白A'), (5.0, 10.0, '')]
        b = M._beat_plan(per_seg, '剧情', {})
        assert b['summary'] == '梗概'
        assert b['beats'][0]['importance'] == 'advance'
        assert b['beats'][0]['role'] == '推进'

    def test_infer_scene_story_via_light(self, monkeypatch):
        """剧情理解分批走轻量通道，JSONL 解析入库。"""
        monkeypatch.setattr(M, '_local_model_available', lambda: True)
        chunk = ('{"i":0,"story":"主角登场","who":"主角","act":"引入","keywords":["登场"]}\n'
                 '{"i":1,"story":"冲突升级","who":"反派","act":"冲突","keywords":["对抗"]}')
        monkeypatch.setattr(M, '_llm_light',
                            lambda prompt, system='', timeout=240: chunk)
        scenes = [
            {'location': '街道', 'characters': '主角', 'event': '登场', 'start': 0.0, 'end': 10.0},
            {'location': '天台', 'characters': '反派', 'event': '对峙', 'start': 10.0, 'end': 20.0},
        ]
        r = M._infer_scene_story(scenes, asr=None, window=10)
        assert r[0]['story'] == '主角登场'
        assert r[1]['act'] == '冲突'

    def test_align_beats_to_scenes_via_light(self, monkeypatch):
        """语义对齐走轻量通道（本地优先分支），对齐映射正确解析。"""
        monkeypatch.setattr(M._w, 'local_llm_enabled', lambda: True)
        monkeypatch.setattr(M._w, 'local_llm_ping', lambda: (True, 'ok'))
        monkeypatch.setattr(M, '_llm_light',
                            lambda prompt, timeout=120:
                            '{"对齐":[{"解说词":0,"场景":[0]}]}')
        beats = ['第一句解说词']
        scenes = [{'location': '街道', 'characters': '主角', 'event': '登场', 'start': 0.0, 'end': 10.0}]
        r = M._llm_align_beats_to_scenes(beats, scenes)
        assert 0 in r


class TestWriteChannelNotDegraded:
    def test_script_write_stays_on_main_model(self, monkeypatch):
        """写稿主通道不降级：local_vlm_narrate 全程不触发轻量调用（light_calls == 0）。"""
        light_calls = []
        monkeypatch.setattr(M, 'local_llm_light_chat',
                            lambda prompt, system=None, timeout=180: light_calls.append(1) or '轻量')
        monkeypatch.setattr(M, 'local_llm_chat',
                            lambda prompt, system=None, timeout=300: chr(10).join(['第一句', '第二句']))
        monkeypatch.setattr(M, '_local_model_available', lambda: True)
        monkeypatch.setattr(M, '_seg_visual_captions',
                            lambda frames, per_seg, params, progress=None: {0: '画面A', 1: '画面B'})
        monkeypatch.setattr(M, '_plot_brief', lambda frames, per_seg, params: '剧情梗概')
        monkeypatch.setattr(M, '_beat_plan', lambda per_seg, plot, params: {
            'summary': '', 'beats': [{'i': i + 1, 'importance': 'advance', 'role': ''} for i in range(2)]})
        lines, used = M.local_vlm_narrate([(0.0, 5.0, ''), (5.0, 10.0, '')],
                                          {0: 'f0.jpg', 1: 'f1.jpg'}, {})
        assert used is True
        assert len(lines) == 2
        assert len(light_calls) == 0
