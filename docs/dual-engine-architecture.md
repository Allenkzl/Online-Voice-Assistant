# 双引擎架构：半在线链路 + 端到端链路（2026-09-10）

> 一个仓库、一套唤醒/VAD/播放/打断/调试台，两条可切换的"对话大脑"：
> **pipeline**（本地 ASR → 千问 LLM → 千问 TTS）和 **e2e**（录音直发 GLM-4-Voice → 音频直出）。
> 切换只改一个配置项，不改代码、不切 git 分支。

---

## 1. 结论先行

| 问题 | 结论 |
|---|---|
| 两条链路会冲突吗？ | **不会**。它们共用"耳朵和嘴"（唤醒、录音、回声防护、播放、打断、事件、调试台、部署），只有中间那一步不同 |
| 该怎么共存？ | 抽 `Engine` 接口 + 运行时开关 `engine: pipeline|e2e`，**不是一个功能一个 git 分支** |
| 部署时怎么切？ | 改 `config.json` 的 `engine` → 重启 `ova-wake`（或 `WAKE_ENGINE=e2e`）。两条链路的依赖可以共存在同一个 venv |
| 为什么不建议两条 git 分支？ | 共享代码占 90%：分叉后改一个 VAD/打断的 bug 要改两遍，现场切换还要 checkout+重装；本项目 `dev` 与 `feat` 已经分叉过一次 |
| 成本对比 | pipeline ≈¥0.01/轮；e2e ≈¥0.02~0.05/轮（**端到端不省钱，买的是延迟与自然度**） |
| 最大风险 | GLM-4-Voice 是**非流式**的，要等整段音频生成完才返回 —— 延迟必须实测（`scripts/glm_voice_poc.py`） |

---

## 2. 架构

```
                      ┌─ Engine: pipeline ───────────────────────────────┐
                      │  本地 ASR(SenseVoice) → 千问 LLM(tools) → 千问 TTS │→ 16k 立体声 WAV
唤醒(openWakeWord)     │  产出：文本回复 + 合成音频                         │
   ↓                  └──────────────────────────────────────────────────┘
VAD 录音(回声防护)  →  路由层(引擎能力感知)                                       ↓
   ↓                  ┌─ Engine: e2e ────────────────────────────────────┐   统一播放层
   └───────────────→  │  录音 → GLM-4-Voice(音频进/音频出) → 裸PCM转设备格式 │→ play_interruptible()
                      │  产出：文本回复 + 端到端音频                         │   + 打断监听
                      └──────────────────────────────────────────────────┘    + 停止/继续
                                                                                  ↓
                                        事件日志(JSONL) → 网页调试台时间线 → 运维观测
```

### 2.1 共享层（两条链路完全相同）

| 能力 | 位置 |
|---|---|
| 唤醒词（本地 openWakeWord ONNX） | `wake.py` |
| VAD 录音 + 回声防护 + 音量归一 | `wake.py::capture_utterance` / `dialogue.py::listen_question` |
| 播放（本地文件 → `aplay`） | `dialogue.py::play_interruptible` |
| 播放中打断（继续监听唤醒词） | `dialogue.py::_barge_monitor` |
| 打断后指令（停止 / 继续 / 切换讲解） | `dialogue.py::listen_command_after_barge_in` + 本地 ASR |
| 固定讲解路由（关键词 → 预制 WAV） | `solutions.py`（**仅对 pipeline 生效**，见 §3） |
| 兜底音频、事件日志、调试台、systemd 部署 | `assets/fallback/`、`config.svc_event`、`console.py`、`deploy/` |

### 2.2 引擎接口

```python
# src/ova/engines/base.py
@dataclass
class Reply:
    audio_path: Path          # 已落盘的 16 kHz 立体声 WAV，直接交给播放层
    text: str = ""            # 回复文本（日志/调试台显示）
    transcript: str = ""      # 用户说了什么（e2e 模式为空）
    lang: str = ""
    timeout_s: float = 90.0   # 播放看门狗
    temporary: bool = True    # 播完删除
    meta: dict = ...          # 延迟分解 / tokens / 成本 / provider

class Engine(Protocol):
    name: str
    needs_transcript: bool    # True → 编排层先跑本地 ASR；False → 直接送音频
    def respond(self, samples, text: str, cfg: dict) -> Reply: ...
```

`needs_transcript` 是省时间的关键：e2e 模式完全不调用本地 ASR（CM4 上省 2.5~4s）。

### 2.3 目录

```
src/ova/
├── engines/
│   ├── __init__.py      # 导出 Engine/Reply/EngineError/build_engine
│   ├── base.py          # 接口 + 别名表 + 工厂
│   ├── pipeline.py      # 半在线：本地ASR文本 → 千问(工具) → 千问TTS
│   └── glm_voice.py     # 端到端：录音 → GLM-4-Voice → 设备格式音频
├── dialogue.py          # 编排：听 → 路由 → 引擎 → 播 → 打断（与引擎解耦）
├── audio.py             # ALSA + WAV/重采样工具（两条链路共用）
└── ...
scripts/glm_voice_poc.py # Phase 0 离线验证（wav → wav，不需要机器人）
```

---

## 3. 两条链路对照

| 维度 | pipeline（半在线） | e2e（端到端） |
|---|---|---|
| 中间环节 | 3 步：本地 ASR → 千问 LLM → 千问 TTS | 1 步：GLM-4-Voice |
| 上云内容 | **只有文本** | **访客整段语音** |
| 延迟（CM4，含 VAD 1.2s） | 3~8s（ASR 2.5~4s + LLM 0.6~1.5s + TTS 1~4s） | 理论 2~4s（省掉 ASR 与 TTS），**实测待 Phase 0** |
| 成本/轮 | ≈¥0.010（TTS 占 96%） | ≈¥0.02~0.05（按 token，实测为准） |
| 工具调用 | ✅ `query_weather`（wttr.in） | ❌ GLM-4-Voice 无 Function Calling |
| 关键词路由/固定讲解 | ✅ 逐字命中（`config/solutions.json`） | ❌ 无本地文本，路由层跳过 |
| 打断 | 停止播放 + 本地 ASR 听"停止/继续/切换" | 停止播放 + 同样走本地 ASR 听指令 |
| 真·实时打断 | ❌（TTS 非流式） | ❌（GLM-4-Voice 非流式）；要真打断需换 GLM-Realtime |
| 音色 | qwen3-tts 多音色可选（当前 Cherry） | 模型自带声音，**不可选**（可调情感/语速/方言） |
| 文案可控性 | 高（固定讲解走预制 WAV） | 低（大模型自由发挥） |
| 本地依赖 | sherpa-onnx + 228MB 模型（ASR 必需） | ASR 仅用于打断指令；更省内存也能省模型 |
| 密钥 | `DASHSCOPE_API_KEY` | `ZHIPUAI_API_KEY` |

---

## 4. 怎么切换

### 4.1 配置（推荐）

```bash
# 方式一：改 config.json
"engine": "e2e"        # pipeline(默认) | e2e
sudo systemctl restart ova-wake

# 方式二：环境变量（/etc/ova.env）
WAKE_ENGINE=e2e
```

优先级与其它参数一致：**命令行 > 环境变量 > config.json > 代码默认值**。

### 4.2 两个 systemd 服务（可选）

需要两种模式各自不同的参数/开机默认时，可建 `ova-wake-pipeline` / `ova-wake-e2e` 两个 unit，
用 `systemctl enable --now` 切换。

⚠️ **绝对不能同时启动**：ALSA 是共享入口（`dsnoop`/`dmix`），两个进程会抢麦克风、抢 CPU、互相打断。

### 4.3 为什么不建议 git 分支切换

- 共享代码分叉 → 一个 bug 改两遍，合并冲突常态化
- 现场切换要 `git checkout` + 可能重装依赖 + 重启，容易忘切/丢改动
- 两条链路的依赖不冲突（sherpa-onnx 与 urllib 共存无碍），没有分叉的技术理由

---

## 5. 端到端引擎实现要点（易错清单）

| 点 | 事实 | 处理 |
|---|---|---|
| 接口形态 | `POST /api/paas/v4/chat/completions`，`model=glm-4-voice`，标准 Chat Completions（**不需要 WebSocket**） | `engines/glm_voice.py::_post` |
| 请求体 | `content=[{text: 人设}, {input_audio: {data: base64(wav), format: "wav"}}]` | 上传 **16 kHz 单声道 WAV**（`audio.mono16k_wav_bytes`） |
| 返回音频 | `message.audio['data']` = base64 **裸 PCM：24 kHz / 单声道 / 16 bit，无 WAV 头** | 补头 + 去直流/淡入淡出 + 压缩峰值 + 响度对齐 + `resample_poly` 24k→16k + 复制双声道（`audio.device_wav_bytes`） |
| 返回文本 | `message.content`（回复文本，用于日志/调试台/语言判定） | `Reply.text` |
| 计费 | ¥80/百万 tokens，输入音频 12.5 token/秒；响应含 `usage` | `Reply.meta.tokens/cost_cny`，日志 `E2E_USAGE` |
| 上限 | 上下文 8K（约 20 轮）、输出 4K tokens（约 5 分钟音频）、并发 V0=5 | 单轮模式不受影响 |
| 无 Function Calling | 天气等工具只能留在 pipeline | 文档与代码注释都标注，避免以后误接 |
| 非流式 | 必须等整段生成完 | 展厅默认关闭 `ack_think.wav` 缓冲音，避免正式回答前插入“好的/嘟嘟”；延迟实测走 PoC |
| 静音输入 | 上云前由本地 VAD 门控，不会把静音发出去 | 唤醒 + VAD 双重门控，也避免按秒计费浪费 |
| 错误分类 | 无 key / HTTP 4xx5xx / 网络 / 非法 base64 / 缺 audio 字段 / 响应结构异常 | 全部转 `EngineError` → 播放 `fallback_net.wav` 并记 `dialog` 事件 |
| 采样率（**踩过坑**） | **官方示例写的 44100 是错的，实际 24 kHz**；按 44.1k 播会加速 1.84 倍、音调拉高，现场听感"叽里咕噜" | 默认 `glm_voice_pcm_rate=24000`；判据用音节速率 + 频谱带边 + 人耳盲听（详见 glm-voice-poc §6.1） |

---

## 6. 测试策略（无需硬件、无需网络）

| 测试文件 | 覆盖 | 数量 |
|---|---|---|
| `tests/test_engines.py` | 工厂/别名/未知引擎、`clip`、天气工具往返、Reply 结构、CloudError→EngineError | 12 |
| `tests/test_dialogue_routing.py` | 停播/讲解/继续路由、ack 开关、e2e 跳过路由并拿到音频、引擎失败兜底、round 级只在该转写时才转写 | 14 |
| `tests/test_glm_voice_engine.py` | 请求体形状（base64 16k 单声道 WAV + 人设）、裸 PCM 解码、重采样到设备格式、usage/成本、6 类错误映射、音频工具 | 16 |
| `tests/test_asr_bilingual.py` | ASR 模型类型判别 + 中英识别 + 触发词路由 + 静音护栏（需模型文件，缺失则 SKIP） | 8 |

```bash
python3 -m pytest tests/ -q          # 50 passed
```

---

## 7. 部署与运维

```bash
# 密钥（不要进 git）
echo 'DASHSCOPE_API_KEY=sk-xxx' | sudo tee -a /etc/ova.env   # pipeline 用
echo 'ZHIPUAI_API_KEY=xxx'      | sudo tee -a /etc/ova.env   # e2e 用
sudo chmod 600 /etc/ova.env

# 切到端到端
#   config.json: "engine": "e2e"
sudo systemctl restart ova-wake
journalctl -u ova-wake -f | grep -E "ENGINE|E2E_|REPLY_READY|PLAYBACK"
```

**观测要点**

| 日志/事件 | 含义 |
|---|---|
| `ENGINE name=… needs_transcript=…` | 本轮用的哪个引擎 |
| `E2E_READY`（启动时一次） | 模型/URL/超时/采样率 |
| `E2E_LATENCY encode/request/convert/total/audio` | 延迟分解 |
| `E2E_USAGE … cost=¥…` | 用量与单轮成本 |
| `REPLY_READY engine=… file=…` | 引擎返回、准备播放 |
| `dialog … 引擎失败(e2e): …` + 兜底音 | 失败路径（不会静默） |

**回退**：把 `engine` 改回 `pipeline` 重启即可；线上不需要动代码。

---

## 8. 后续路线

1. **Phase 0（必须先做）**：`python3 scripts/glm_voice_poc.py tests/asr_en_smart_retail.wav`
   → 拿到真实请求耗时、usage、输出采样率、中英效果；**延迟不达标就不接线**
2. **KWS 关键词路由**：常驻 sherpa-onnx 关键词检测（~3M 模型，边说边判 <0.5s），
   让 e2e 模式也能享受"固定讲解走预制 WAV"（文案可控 + 零成本）
3. **流式（真打断）**：换 GLM-Realtime（WebSocket，¥0.18/分钟，**支持 Function Calling**、
   可打断、可带视频帧）——按通话时长计费，只适合"唤醒后连、答完断"
4. **多轮**：GLM-4-Voice 8K 上下文约 20 轮；需设计会话结束条件（静默 N 秒断开）
5. **千问端到端 provider**：`qwen3-omni-flash-realtime`（音频入 ¥18.9/M、出 ¥75.1/M，无 FC）
   或非实时 `qwen3-omni-flash`（¥15.8/¥62.6）——注意千问没有"一次 HTTP 调用"的简单形态

---

## 9. 已知限制 / 未验证项

> **2026-09-10 Phase 0 已实测**（完整数据见 `docs/glm-voice-poc-2026-09-10.md`）：
> **真机实测（CM4，2026-09-10/11）**：请求延迟 1.1~1.9s（p50 ≈1.4s，见过一次 6.95s 卡顿）、
> **说完→开口约 2.5~3s**、成本 ¥0.014~0.025/轮、回复音频 3.5~7.9s（人设压不住长回答）、
> 中文发音清晰可懂；**英文发音可懂度差**（两个独立 ASR 均无法还原关键词），语言跟随不稳。
> 采样率实测为 **24 kHz**（官方示例的 44100 会导致播放加速 1.84 倍，见 glm-voice-poc §6.1）。


- **英文质量是最大短板**：GLM-4-Voice 中文好、英文差。建议英文访客仍走 pipeline（qwen3-tts），
  或先做“同句英文双引擎合成 + ASR 回听”的客观对比再决定（需机器人上的 DashScope key）。
- **真机延迟已确认**（2026-09-10/11）：请求 p50 ≈1.4s、说完→开口 ≈2.5~3s，达到目标；仍需观察长稳与卡顿比例
- **音色不可选**：需人耳判断是否适合展厅调性
- **远场噪声 + 中英混说**未在展厅环境验证
- **隐私**：e2e 模式把访客语音整体上传第三方，展厅需要提示与内部合规确认
- **本地 ASR 仍会加载**：e2e 模式下用于打断后的"停止/继续"指令；若完全不需要，可后续加开关跳过加载
