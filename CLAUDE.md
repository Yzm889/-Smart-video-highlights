# 一帧成片 (FrameCut) — AI 工作守则

**每个 AI 每轮工作开始前必读 `AI_CHANGELOG.md`，结束后必更新它。**

## 铁律

1. **读**：开工先读 `AI_CHANGELOG.md`（更新清单 + 升级路线 + 已知坑 + 已拍板决策）
2. **写**：每轮做完（哪怕只改一行），在 `AI_CHANGELOG.md`「四、更新清单」**顶部**追加一条：
   `- 日期 v版本 (commit) 一句话改动；踩了什么坑`
   然后随本轮代码一起 commit
3. **验证**：改动必须实测（`node --check` + 8899 验证实例 + 截图/断言），"证据说话"
4. **三原则**：不开倒车 / 不乱做 / 证据说话
5. **已拍板不改清单**在 AI_CHANGELOG.md 第五节，别推翻

## 快速事实

- 桌面版是产品主线（static/desktop/），网页版已冻结（只修不改）
- 验证实例端口 8899（`_verify_env_8899.py` 现成）；8765 是主人的实例，别占
- 打包构建：`.venv_build/Scripts/python.exe -m PyInstaller FrameCut.spec --noconfirm`
- 开发用 `py -3.13`；打包 venv 是 Python 3.11
