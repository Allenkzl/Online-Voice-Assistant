# 架构说明

## 全景

```
 ┌────────────────────────── 设备（任意 Linux + ALSA 声卡）──────────────────────────┐
 │                                                                                  │
 │  [唤醒词常驻]  openWakeWord ONNX ── 每80ms打分 ── threshold+hits ── 命中?          │
 │      │ 是                                                                         │
 │      ▼                                                                           │
 │  [应答] aplay assets/*.wav（“在呢”，随机，根目录应答池与兑底音隔离）                │
 │      ▼  (可选 WAKE_DIALOGUE=1)                                                   │
 │  [VAD] 回声防护延时 listen_delay → Silero VAD(可回退 energy) → 判定说完           │
 │      │  录音(≤max_question_s)                                                    │
 │      ▼                                                                           │
 │  [ASR] sherpa-onnx 本地识别(SenseVoice 中英/可回退 Paraformer) ─ AGC ─ 文字        │
 │      ▼                                                                           │
 │  [LLM] 千问 qwen-flash ─ tools(query_weather) 可选 ─ 回复文字                     │
 │      ▼                                                                           │
 │  [TTS] qwen3-tts-flash 在线合成 → 重采样16k立体声 → aplay 播放                    │
 │      ▼                                                                           │
 │  回到待唤醒（单轮交互，再次对话需重新唤醒）                                       │
 └──────────────────────────────────────────────────────────────────────────────────┘
```

## 模块边界（src/ova）

| 模块 | 职责 | 依赖 |
|---|---|---|
| `config.py` | 常量、默认参数、事件桥 | 无 |
| `audio.py` | ALSA 采集/播放后端抽象（未来 PortAudio 换实现不换接口） | config |
| `wake.py` | 唤醒引擎入口：模型加载、打分、应答、cli main（含对话编排钩子） | audio/config |
| `vad.py` | 语音活动检测入口（当前实现仍在 `wake.py`，支持 energy/silero） | wake(audio) |
| `asr.py` | 本地识别 LocalAsr（模型目录可换 small/int8） | — |
| `tools.py` | 外部工具（天气 wttr.in，重试+超时） | — |
| `api.py` | DashScope HTTP 客户端/异常 | — |
| `llm.py` | 千问 chat/chat_once（OpenAI 兼容 + tools） | api |
| `tts.py` | 合成（原生接口→OSS→重采样16k wav） | api |
| `engines/` | **对话引擎抽象**：`base.py`(Engine/Reply/工厂) + `pipeline.py`(本地ASR→千问LLM→千问TTS) + `glm_voice.py`(录音直发 GLM-4-Voice) | llm/tts/tools/api |
| `dialogue.py` | 单轮对话编排：听→路由→引擎→说→打断→兑底（与引擎解耦） | wake/vad/asr/engines |
| `console.py` | 网页调试台（SSE 事件流 + 分环节测试 + 主服务事件桥） | 全部 |
| `calibrate.py` | 录音校准：给出建议阈值 | wake/asr |
| `__main__.py` | `python -m ova wake|console|calibrate` | 全部 |

## 设计要点（踩坑沉淀）

1. **唤醒词必须本地离线**（延迟与可用性）；识别用本地 sherpa-onnx 模型（在线
   qwen3-asr-flash 需网关支持，当前账户不可用，故做 `local|remote` 预留位）。
   现在默认 **SenseVoice 中英双语**，中文 Paraformer 保留为回退（换 `asr_model_dir` 即可，
   `asr.py` 按目录内容自动判别 `sense_voice|paraformer|transducer`）；选型与实测见
   `docs/asr-bilingual-models-2026-09-10.md`。
2. **麦克风共享入口**（ALSA dsnoop）：唤醒服务、调试台、daemon 可并存；
   独占 hw 设备会导致其它进程无法采集。多读方有 CPU 开销，正式运行只留一个常驻读方。
3. **回声是最大敌人**：自己播完应答立刻听，余响会干扰 VAD 与 ASR。
   对策：应答后 `listen_delay` 丢弃回声尾；Reachy Mini 默认用 Silero VAD 判断语音段；
   旧 energy VAD 仍可回退，音量 AGC 归一后再识别。
4. **应答池与兑底音隔离**：兑底 wav 放 `assets/fallback/` 子目录，避免被随机应答误播。
5. **连续帧确认（hits）**：真人喊词持续 ≥0.5s，噪声尖峰为单帧——要求连续 N 帧
   超阈值即可在低阈值(0.2)下同时拿到高召回与低误报。
6. **播放中打断**：长讲解/TTS 播放时用独立监听线程继续跑唤醒模型；命中 `Hey Jarvis`
   后终止当前 `aplay`，再听一条新指令。`停止` 回待唤醒，`继续` 从中断点附近恢复，
   新主题关键词则切换到对应讲解。
7. **线程命名别踩雷**：自定义类属性勿用 `_stop`（覆盖 threading.Thread 私有方法）。
8. **可移植性**：所有设备相关项（采集/播放设备、模型目录、应答目录、检测参数）
   均可配置；`auto` 设备回退链 `reachymini_* → default → plughw:0,0`。

## 延迟预算（CM4 真机实测）

**共同前置**：说完话 → Silero VAD 判定结束（`end_silence_s` 默认 1.2s）；随后按 `engine` 走不同链路。
`e2e` 引擎在服务启动时初始化/预热，不在唤醒后加载本地 ASR；唤醒提示音结束后直接进入 VAD。
本地 ASR 只在 `pipeline` 主链路或播放中打断后的短指令识别中使用。

| 引擎 | 链路 | 分段耗时 | 说完→开口 |
|---|---|---|---|
| `pipeline`（半在线） | 本地 ASR → 千问 LLM → 千问 TTS | ASR 0.6~3s（模型与句长相关）+ 千问 0.6~1.5s + TTS 1~4s | **3~8s** |
| `e2e`（端到端） | 录音直发 GLM-4-Voice，音频直出 | 请求 1.1~1.9s（p50 1.4s，见过 6.95s 卡顿；超时 10s）+ 转码 ~0.05s | **约 2.5~3s** |

展厅模式默认关闭正式回答前的缓冲应答（`ack_before_reply=false`），避免唤醒提示音和正式回答之间插入
"嗯，好的"或等待提示音；如果更看重感知等待时间，可临时重新打开。
端到端细节（采样率 24kHz、音频规整链、人设长度、成本）见
[dual-engine-architecture.md](dual-engine-architecture.md) 与 [glm-voice-poc-2026-09-10.md](glm-voice-poc-2026-09-10.md)。

## 事件流（调试台时间线）

主服务写入 `HJV_EVENT_FILE`(JSONL)，console 尾随并转发 SSE。两种引擎的事件并集：

```
pipeline: wake/唤醒命中 → asr/识别:… → llm/千问回答:… → tts/播放 → dialog/本轮完成
e2e:      wake/唤醒命中 → e2e/端到端回复(时长,延迟)+用量 → dialog/e2e 回复就绪 → dialog/本轮完成
两者都会出现：dialog/提示音、dialog/被打断、dialog/引擎失败（含兜底音）
```

日志关键字（排查时先 grep 这些）：`VAD_READY`、`VAD_SILERO_END`、`ENGINE name=`、`ASR_READY`、`E2E_READY`、`E2E_REPLY`、
`E2E_LATENCY`、`E2E_USAGE`、`E2E_LEVEL`、`REPLY_READY`、`PLAYBACK_DONE`、`BARGE_LISTENING`。
