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
 │  [VAD] 回声防护延时 listen_delay → 安静帧基线 → 静音 end_silence 判定说完           │
 │      │  录音(≤max_question_s)                                                    │
 │      ▼                                                                           │
 │  [ASR] sherpa-onnx Paraformer(本地 int8) ─ AGC音量归一 ─ 文字                      │
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
| `vad.py` | 语音活动检测（单句采集） | wake(audio) |
| `asr.py` | 本地识别 LocalAsr（模型目录可换 small/int8） | — |
| `tools.py` | 外部工具（天气 wttr.in，重试+超时） | — |
| `api.py` | DashScope HTTP 客户端/异常 | — |
| `llm.py` | 千问 chat/chat_once（OpenAI 兼容 + tools） | api |
| `tts.py` | 合成（原生接口→OSS→重采样16k wav） | api |
| `dialogue.py` | 单轮对话编排：听→识→想→说→兑底 | wake/vad/asr/llm/tts/tools |
| `console.py` | 网页调试台（SSE 事件流 + 分环节测试 + 主服务事件桥） | 全部 |
| `calibrate.py` | 录音校准：给出建议阈值 | wake/asr |
| `__main__.py` | `python -m ova wake|console|calibrate` | 全部 |

## 设计要点（踩坑沉淀）

1. **唤醒词必须本地离线**（延迟与可用性）；识别用本地 Paraformer（在线
   qwen3-asr-flash 需网关支持，当前账户不可用，故做 `local|remote` 预留位）。
2. **麦克风共享入口**（ALSA dsnoop）：唤醒服务、调试台、daemon 可并存；
   独占 hw 设备会导致其它进程无法采集。多读方有 CPU 开销，正式运行只留一个常驻读方。
3. **回声是最大敌人**：自己播完应答立刻听，余响会把 VAD 基线抬高数倍，
   导致“听不懂”。对策：应答后 `listen_delay` 丢弃回声尾；基线取“最安静帧”分位；
   音量 AGC 归一后再识别。
4. **应答池与兑底音隔离**：兑底 wav 放 `assets/fallback/` 子目录，避免被随机应答误播。
5. **连续帧确认（hits）**：真人喊词持续 ≥0.5s，噪声尖峰为单帧——要求连续 N 帧
   超阈值即可在低阈值(0.2)下同时拿到高召回与低误报。
6. **线程命名别踩雷**：自定义类属性勿用 `_stop`（覆盖 threading.Thread 私有方法）。
7. **可移植性**：所有设备相关项（采集/播放设备、模型目录、应答目录、检测参数）
   均可配置；`auto` 设备回退链 `reachymini_* → default → plughw:0,0`。

## 延迟预算（实测参考，CM4/RPi5 级）

说完话 → 判定结束 ~1.2s；ASR 0.6~3s（模型与句长相关）；千问 0.6~1.5s；
TTS 合成 1~4s；总计 3~8s 开口，另加“嗯，好的”缓冲应答先出声（感知提速）。

## 事件流（调试台时间线）

主服务写入 `HJV_EVENT_FILE`(JSONL)，console 尾随并转发 SSE：
`wake/唤醒命中 → asr/识别:… → llm/千问回答:… → tts/播放 → dialog/本轮完成`。
