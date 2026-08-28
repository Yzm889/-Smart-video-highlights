# 项目进度存档（2026-08-28 更新）

> **当前状态：全部完成、48 测试通过、可正常出片。解说已切到本地 14B 模型。**

---

## ✅ 第二轮（本轮）已完成

### A. 🚀 网页拉取「加速通道」（用户要求：记住拉取方案并优先使用）
- **后端 `webui_server.py`**：新增 `FAST_GGUF_SOURCES`（`qwen2.5:14b`/`qwen2.5:7b`/`qwen2.5:latest` → bartowski 非 split 单文件 GGUF 直链）+ `_fast_pull_local()`（aria2c 多线程下载 → 进度写入 `LOCAL_PULL` → `ollama create` 导入 → 清理临时文件）。`_local_pull_thread` 开头先尝试加速通道，**失败自动回退官方 `ollama pull`**。
- 前端按钮文案提示"自动优先国内加速通道"。README 补手动导入教程 + 警告勿用 Qwen 官方分片。
- 实现要点（勿丢）：aria2c 参数 `-c -x 8 -s 8 -k 1M --max-tries=0 --retry-wait=3 --timeout=60`；`ollama create` 用 `cmd /c` 规避 PowerShell 对 stderr 进度条的误报；Modelfile 用绝对路径 `FROM C:/.../xxx.gguf`；下载用 `_dl/` 临时目录，结束后删除。

### B. 🎬 解说 v3「连贯真人化」（用户要求：连续起来 + 别每段升华含义）
- **重写 `local_vlm_narrate`**：
  - **整稿生成**：一次让模型写完整连贯解说稿（像真人一样从头讲到尾、镜头间自然承接），一行对应一个镜头；prompt 强制"必须恰好 N 行、不要合并镜头"。
  - **少升华**：prompt 明确"除非真转折/高光，否则不要总结'这反映了/象征着/揭示了'"；`_beat_plan` 的 role 改讲"剧情推进作用"而非"含义升华"。
  - **自优化**：整稿生成后让模型自查润色一遍（改衔接/删重复套话/保持详略），仅当整稿成形且用强文字模型时执行。
  - **前置要求**：`params['req']` 注入所有 prompt（前端「解说要求/风格」输入框）。
  - **映射兜底修复**：`_split_nar_sentences` + `_distribute_sents`——行数不足时按句切分均匀分布，杜绝"多镜头复制同一句"（实测曾出现 5 段全同，已修复）。
  - 整稿失败回退逐段生成（少升华 + 承接上一段结尾）。
- 云端 `generate_narration` instr 同步：整稿连贯 + 少升华 + 额外要求。
- 前端：解说卡片新增「解说要求/风格」输入框 `narReq` → 传给 `params.req`。
- **实测（14B，同《赌圣》片段，req=接地气）**：5 段完全不同、衔接自然、接地气（"这小子一脸懵逼""保安看得津津有味""急得直跺脚"）、无意义升华。约 50s/5 段。

## ✅ 本轮已完成（解说升级 · 理解含义 + 合理延伸 + 详略得当 + 14B 部署）

### 0. 🎯 14B 模型部署完成（重点，已成功并实测）
- **本机已装 `qwen2.5:14b`（9.0GB，bartowski Q4_K_M 完整单文件）**，`ai_config.json` 的 `local.model` 已设为 `qwen2.5:14b`，解说自动走 14B。
- **最终落地方案**（踩坑后的正确路径，供以后参考）：
  1. 官方 `ollama pull qwen2.5:14b`（registry.ollama.ai）国内慢/卡 → 弃用；
  2. 1ms.run 是 Docker 镜像加速，**不适用** ollama 模型；
  3. Qwen 官方分片 GGUF（HF/ModelScope `qwen2.5-14b-instruct-q4_k_m-0000N-of-00003.gguf`）是**非标准 split**（分片 1/2 无 tokenizer，token_type 被切分 152068<304132），`ollama create` 报 `split GGUF ... has 1 shards, expected 3`，手工合并也会因 tokenizer 不完整导致加载失败（`toktypes (152068 < 304132)`）→ **别用 Qwen 官方分片**；
  4. **正确方案**：下载 **bartowski 非 split 单文件** GGUF + `ollama create` 导入：
     ```
     # aria2c 多连接从 hf-mirror 下载（约 3 分钟，远快于单连接）
     aria2c -c -x 8 -s 8 -k 1M --max-tries=0 --retry-wait=3 --timeout=60 -o qwen2.5-14b-Q4_K_M.gguf "https://hf-mirror.com/bartowski/Qwen2.5-14B-Instruct-GGUF/resolve/main/Qwen2.5-14B-Instruct-Q4_K_M.gguf"
     # Modelfile: FROM C:/.../qwen2.5-14b-Q4_K_M.gguf
     cmd /c "ollama create qwen2.5:14b -f Modelfile"   # cmd /c 规避 PowerShell 对进度条 stderr 的误报
     ```
  5. 验证：`ollama list` 出现 `qwen2.5:14b`；`POST localhost:11434/api/generate` 能返回结果。
- **14B vs 7B 实测（同《赌圣》买汽水片段）**：
  - 14B 语言更有解说感、含义更深（"悄然埋下两地交流初期的障碍"、"售货机的拒绝象征着异国他乡的经济隔阂"）；
  - 详略结构好：79/67/103/91/52 字，关键段展开、过渡段收束；
  - 每段独立成句已加"开头不用接续词"约束。
- 注意：14B 单段生成约 12s，完整解说（视觉+规划+5段）约 100s，比 7B 慢但质量高一档。

### 1-5. 解说管线升级（同上一轮，见下方"已完成"清单）

### 1. 后端：解说词生成管线重写（webui_server.py）
- 新增 `_try_parse_json`：从模型输出稳健提取 JSON。
- 新增 `_beat_plan`：**详略规划**。用文字模型把每段视频标成 4 级——
  `key`(关键/转折/高光，展开讲) / `advance`(推进，正常讲) / `transition`(过渡，一句带过) / `mood`(氛围，简略渲染)，
  并给每段写"本段在剧情中的作用"（人物处境/动机/事件含义/前后因果，可合理延伸但不编造）。
- 重写 `local_vlm_narrate`：逐段按重要性分配篇幅（key 80~120 字 / advance 40~70 / transition·mood 20~40）；
  五条铁律：
  1. 讲"这段剧情在讲什么、意味着什么"，不是描述画面；
  2. 含义延伸只在 key 段展开，避免每段升华时代背景；
  3. 除非剧情理解里确有转折，禁止堆砌"转折/高潮/关键"等空泛词（防过度理解）；
  4. 台词只作参考转述，不原样引用（防照本宣科）；
  5. 每段强制"推进新内容、不与前文重复"（防重复）。
  整体剧情理解（plot/summary）只喂给第 1 段做开场衔接。
- `generate_narration` 云端智能分支 instr 同步升级（详略 + 含义 + 转述）。

### 2. 后端：解说模型引导检测
- `local_llm_enabled()` / `_local_model_available()` / `_installed_local_models()`（缓存 60s）：
  自动探测本机 Ollama 已装模型，解说写稿优先用**本地文字模型**（qwen2.5:latest 等），VLM 只负责看懂画面。
- `ai_status` 新增 `narr_guide` 字段：`weak_vlm`(qwen2.5vl 是弱视觉模型) / `vlm_model` / `local_ok` / `local_model` / `installed` / `recommend`。

### 3. 前端：在项目内提示 qwen2.5vl 局限 + 引导部署标准解说模型
- `static/index.html`：解说卡片顶部新增 `<div id="narrGuide">` 动态引导条容器；
  AI 配置「⑤ VLM」区新增静态黄色提示框（qwen2.5vl 只"看懂画面"、写稿主力是文字模型 qwen2.5:14b）。
- `static/app.js`：新增 `renderNarrGuide(s)`，按 `narr_guide` 状态显示——
  有文字模型=绿条；只有弱 VLM=黄条（附 `ollama pull qwen2.5:14b` 命令+已装列表）；都没配=蓝条。
- `static/style.css`：`.narr-guide` 样式（ok/warn 两态）。

### 4. 文档
- `README.md`：新增「理解场景含义 + 详略得当」设计说明；新增「🧠 本地部署"标准解说模型"」指南（双模型分工表 + 部署步骤）。

### 5. 实测（qwen2.5:latest / 7B，《赌圣》买汽水片段，5 段）
改前：每段重复"货币流通不便+无奈挣扎"，两段几乎一字不差。
改后（79/37/46/51/35 字，详略分明、有推进、含义只在开场点出）：
```
[0 79字] 故事从这男子无奈的表情中展开……更是当时货币兑换不便的生动写照。
[1 37字] 人民币换不了港币，这让男子陷入困境。他无奈地向一位路人求助，故事由此转折。
[2 46字] 男子不断恳求机器供应他渴望的汽水……这份坚持预示着即将发生的关键转折。
[3 51字] 男子一遍遍恳求……也预示着即将发生的突破性转折。
[4 35字] 男子的困境愈发明显……预示着即将迎来关键转折。
```

---

## 测试 & 回归
- `python -m pytest tests -q` → 48 passed（约 21s）
- `python -c "import ast; ast.parse(open('webui_server.py',encoding='utf-8').read())"` → py OK
- `node --check static/app.js` → js OK

## 临时文件
- 已清理 `_beat_test.py` / `_beat_test2.py` / `_text_test.py` / `_scan.py` / `_check.py` / `webui_output/_t`、`_t2`、`_dl14b`。
- 保留了 4 个用户成片：`webui_output/20260827-{103807,112405,125858,130200}`。
