# Online Voice Assistant

## 项目简介

给展厅 Reachy Mini 提供一套语音助手：本地 Hey Jarvis 唤醒，加**两条可切换的对话链路**——
半在线（本地 ASR → 千问 LLM → 千问 TTS）与端到端（录音直发 GLM-4-Voice），以及网页调试台。
当前重点是让机器人能在展厅按固定文案介绍方案。

## 技术栈

- Python 3.10+
- ALSA `arecord` / `aplay`
- openWakeWord ONNX
- sherpa-onnx 本地 ASR：默认 SenseVoice 中英双语（中/英/粤/日/韩），可回退中文 Paraformer
- DashScope 千问 `qwen-flash` 与 `qwen3-tts-flash`（pipeline 引擎）
- 智谱 GLM-4-Voice（e2e 引擎，音频进/音频出）
- systemd 部署到 Reachy Mini `/home/pollen/ova`

## 目录结构

- `src/ova/`：唤醒、VAD、ASR、LLM、TTS、对话编排、调试台
- `config/`：示例配置、硬件档案、展厅方案讲解配置
- `assets/`：唤醒反馈音、兜底音频、预生成方案讲解音频
- `deploy/`：systemd 服务模板
- `scripts/`：模型下载与服务安装脚本
- `docs/`：架构、项目历史、会话日志

## 当前状态

### 活跃分支

- `dev`：替换唤醒反馈音、调优展厅唤醒误触发、上线三主题中英文展厅讲解，并开发播放中打断。

### 已完成功能

- 本地唤醒后播放 Home Assistant 的 `wake_word_triggered` 提示音，不再播放“在呢”。
- 三主题展厅讲解：识别到“智慧零售/智慧空间/应急救灾”或对应英文关键词后，直接播放本地预生成中英文讲解音频。
- 展厅唤醒误触发调优：线上从 `threshold=0.20 / hits=3` 调整为 `threshold=0.30 / hits=4`。
- 播放中打断：长讲解或 TTS 播放期间继续监听 `Hey Jarvis`，命中后停止当前播放，支持“停止/继续/切换介绍智慧空间”等后续指令。
- 中英双语 ASR：ASR 从中文 Paraformer 换成 SenseVoice int8（中/英/中英混说、带标点，`ASR_RESULT` 附带 `lang=zh|en`），中文 Paraformer 保留为回退模型。
- **双引擎架构**：对话大脑抽成 `src/ova/engines/`（`Engine`/`Reply`/`build_engine`），
  `engine=pipeline`（半在线）与 `engine=e2e`（GLM-4-Voice 音频直进直出）共用唤醒/VAD/播放/打断/事件/调试台；
  切换只改一个配置项。设计说明见 `docs/dual-engine-architecture.md`。

## 重要约定

- Reachy Mini 线上路径是 `/home/pollen/ova`，服务名是 `ova-wake` 与 `ova-console`。
- ASR 模型目录由 `asr_model_dir` 决定，`src/ova/asr.py` 按目录内容自动判别
  `sense_voice|paraformer|transducer`；默认 `models/asr_sense_voice_zh_en_int8`（中英双语），
  回退中文模型只需改这一项。下载用 `scripts/download_models.sh [sensevoice|small|full]`。
  选型与实测见 `docs/asr-bilingual-models-2026-09-10.md`。
- 方案讲解应优先播放本地预生成 WAV，避免展厅现场依赖实时 TTS 或让大模型改写固定文案。
- **引擎开关**：`engine=pipeline|e2e`（`WAKE_ENGINE`）。两条链路共用唤醒/VAD/播放/打断/事件/调试台；
  `e2e` 需要 `ZHIPUAI_API_KEY`，返回的是 44.1kHz 单声道**裸 PCM**（代码补 WAV 头并重采样到 16k 立体声），
  **没有 Function Calling**（天气工具只在 pipeline），且**非流式**（无"边说边播"，打断只能停播放）。
  固定讲解的关键词路由只对 pipeline 生效；本地 ASR 在 e2e 下仍用于打断后的"停止/继续"指令。
  切换/回退只改配置，不改代码；设计与实测见 `docs/dual-engine-architecture.md`。
- **两条链路不要并行起两个服务**：ALSA 是共享入口（dsnoop/dmix），同时运行会抢麦克风、抢 CPU、互相打断。
- `assets/*.wav` 会被唤醒应答随机池扫描；兜底音和备份音频应放入子目录。
- 方案讲解音频路径由 `config/solutions.json` 管理，当前为 `smart_retail`、`smart_space`、`emergency_response` 三个主题，各有 `zh/en` 两套 WAV。
- 当前正式中文触发词使用“智慧空间”，不把“智慧家居”作为别名触发。
- 唤醒调优记录见 `docs/wake-tuning-2026-09-10.md`；再次调优时先复核历史 `WAKE_DETECTED` 分数、背景样本、唤醒样本，再决定是否改 `threshold`/`hits`。
- 播放中打断记录见 `docs/barge-in-playback-2026-09-10.md`；打断检测使用独立参数，当前建议 `barge_in_threshold=0.30 / barge_in_hits=4`，并用 `BARGE_LISTENING` 日志观察播放期间峰值。
- 头部待机动作当前恢复为官方 recorded move 库，并新增独立看门狗；记录见 `docs/reachy-demo-official-watchdog-2026-09-10.md`。前一版轻量小幅 `/api/move/goto` 记录见 `docs/reachy-demo-lite-motion-2026-09-10.md`，可作为回退方案。

## 变更日志

| 日期 | 分支 | 说明 |
|---|---|---|
| 2026-09-09 | `dev` | 替换唤醒反馈音，并新增方案一固定文案讲解试点。 |
| 2026-09-10 | `dev` | 基于现场日志与校准录音，将展厅唤醒参数调整为 `threshold=0.30 / hits=4`，降低误触发。 |
| 2026-09-10 | `dev` | 将展厅讲解从“方案一/二/三”改为“智慧零售/智慧空间/应急救灾”三主题，并生成中英文预制音频。 |
| 2026-09-10 | `dev` | 新增播放中 `Hey Jarvis` 打断：可停止、继续或切换到新的展厅讲解。 |
| 2026-09-10 | `dev` | 将播放中打断阈值从 `0.45/4` 调低到 `0.30/4`，补充 `BARGE_LISTENING` 峰值日志用于现场调优。 |
| 2026-09-10 | `dev` | 新增轻量展厅待机动作：待机 20 秒一次，说话时 10 秒一次，替代高频官方 recorded moves。 |
| 2026-09-10 | `feat/asr-sensevoice-bilingual` | ASR 换为 SenseVoice 中英双语（模型类型自动识别，Paraformer 可回退），英文触发词因此真正可用。 |
| 2026-09-10 | `feat/asr-sensevoice-bilingual` | 待机动作恢复官方 recorded move 库，并新增 `reachy-demo-watchdog` 自动处理卡住动作和服务恢复。 |
| 2026-09-10 | `dev` | 分支合并回 `dev`（ASR 双语 + 待机动作 watchdog 两个提交）。 |
| 2026-09-10 | `dev` | 抽出可插拔对话引擎（`src/ova/engines/`）：现有 ASR→千问→TTS 路径包成 `pipeline` 引擎，行为不变。 |
| 2026-09-10 | `dev` | 新增 `e2e` 引擎（GLM-4-Voice 录音直进/音频直出）+ 调试台卡片⑥ + 离线 PoC 脚本；两条链路用一个配置项切换。 |
