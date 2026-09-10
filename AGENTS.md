# Online Voice Assistant

## 项目简介

给展厅 Reachy Mini 提供一套语音助手：本地 Hey Jarvis 唤醒、本地中文 ASR、在线千问/TTS，以及网页调试台。当前重点是让机器人能在展厅按固定文案介绍方案。

## 技术栈

- Python 3.10+
- ALSA `arecord` / `aplay`
- openWakeWord ONNX
- sherpa-onnx Paraformer 中文 ASR
- DashScope 千问 `qwen-flash` 与 `qwen3-tts-flash`
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

- `dev`：替换唤醒反馈音、试点“方案一”展厅讲解模式，并调优展厅唤醒误触发。

### 已完成功能

- 本地唤醒后播放 Home Assistant 的 `wake_word_triggered` 提示音，不再播放“在呢”。
- 方案一讲解试点：识别到“介绍方案一/方案1/第一套方案/一号方案”后，直接播放本地预生成讲解音频。
- 展厅唤醒误触发调优：线上从 `threshold=0.20 / hits=3` 调整为 `threshold=0.30 / hits=4`。

## 重要约定

- Reachy Mini 线上路径是 `/home/pollen/ova`，服务名是 `ova-wake` 与 `ova-console`。
- 方案讲解应优先播放本地预生成 WAV，避免展厅现场依赖实时 TTS 或让大模型改写固定文案。
- `assets/*.wav` 会被唤醒应答随机池扫描；兜底音和备份音频应放入子目录。
- 方案讲解音频路径由 `config/solutions.json` 管理，当前为 `assets/solutions/solution_1.wav`。
- 唤醒调优记录见 `docs/wake-tuning-2026-09-10.md`；再次调优时先复核历史 `WAKE_DETECTED` 分数、背景样本、唤醒样本，再决定是否改 `threshold`/`hits`。

## 变更日志

| 日期 | 分支 | 说明 |
|---|---|---|
| 2026-09-09 | `dev` | 替换唤醒反馈音，并新增方案一固定文案讲解试点。 |
| 2026-09-10 | `dev` | 基于现场日志与校准录音，将展厅唤醒参数调整为 `threshold=0.30 / hits=4`，降低误触发。 |
