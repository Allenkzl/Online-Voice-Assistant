# GLM-4-Voice Phase 0 实测记录（2026-09-10）

> 用 `scripts/glm_voice_poc.py` 真调智谱 GLM-4-Voice，回答"这条路能不能走"的关键问题。
> 测试音频：项目 `tests/asr_*.wav`（macOS `say` 合成，16 kHz 单声道）+ 实验室的英文噪声/空域样本。
> 环境：本机（Apple Silicon）→ 公网 → `open.bigmodel.cn`；每次一个独立请求，非流式。

---

## 1. 结论速览

| 问题 | 实测答案 |
|---|---|
| 接口形态 | ✅ 一次 Chat-Completions 调用即可（音频进 / 音频出），**不需要 WebSocket** |
| 输出音频格式 | ✅ 裸 PCM **44.1 kHz / 单声道 / 16 bit**（用回听校验反证：中文回听几乎逐字一致，若是其它采样率会整体变调） |
| 请求延迟 | ✅ **1.1~1.7 s**（短回复）；与输出音频长度近似 `0.5 + 0.3×音频秒数` |
| 端到端总延迟（推算，CM4） | **约 2.5~3 s**（VAD 1.2s + 请求 1.2~1.7s + 转码/aplay），优于现在的 3~8 s |
| 成本 | ✅ ¥0.014~0.021/轮（173~268 tokens @ ¥80/百万） |
| 回复长度可控性 | ⚠️ 人设必须显式限制，否则一次回复 5~11 秒音频（见 §3） |
| **中文发音** | ✅ **清晰可懂**（本地 ASR 回听近乎逐字） |
| **英文发音** | ❌ **可懂度差**——两句纯英文被两个独立 ASR 都判为不可懂（见 §4） |
| 语言跟随 | ⚠️ 不稳定：4 次英文输入里有 1 次用中文回答 |
| 密钥/额度 | 账号需为 GLM-4-Voice 单独充值或买资源包（`code 1113`）；`glm-4-flash` 有免费额度可用来先验证 key |

---

## 2. 延迟 / 用量 / 成本（同一人设下的连续测试）

| 输入 | 输出音频 | 请求耗时 | tokens | 成本 |
|---|---|---|---|---|
| 英文 "Welcome to the Smart Retail area."（1.92s） | 2.04s | 1.29s | 173 | ¥0.0138 |
| 中文 "智慧零售区，欢迎参观。"（2.56s） | 2.00s | 1.16s | 175 | ¥0.0140 |
| 中英混说（3.63s） | 4.12s | 1.61s | 258 | ¥0.0206 |
| 中英混说（未收紧人设） | 11.07s | 4.00s | 455 | ¥0.0364 |

**规律**：请求耗时与输出音频长度强相关（生成 N 秒语音 ≈ 0.3N 秒），所以**控制回复长度同时省延迟和钱**。

---

## 3. 人设（persona）长度控制：必须显式约束

| 人设 | 中文回复 | 英文回复 |
|---|---|---|
| "控制在40字以内"（初版） | 40 字 → **5.27s** 音频 | 混说时飙到 **11.07s** |
| **"中文不超过25个字，英文不超过12个单词"（采用）** | "智慧零售区，欢迎参观！" → **2.00s** | "Welcome to the smart retail area!" → **2.04s** |

采用的人设已写入 `src/ova/engines/glm_voice.py::DEFAULT_PERSONA`，也可用 `glm_voice_persona` / `WAKE_GLM_VOICE_PERSONA` 覆盖。

---

## 4. 英文发音问题（本次最重要的发现）

把返回音频用**两个独立 ASR** 回听（本地 SenseVoice 中英模型 + Whisper-base 强制英文）：

| 模型说的英文 | SenseVoice 回听 | Whisper 回听 | 判定 |
|---|---|---|---|
| "Welcome to the smart retail area!" | `我 the什么呀。`（语种判成 zh） | `Welcome to the SMAPYCALL-E!` | ❌ 关键词完全不可懂 |
| "Of course, I understand English sentences! How can I assist you today?" | `Of course I English how do I.` | `I press R on each door and close the door…` | ❌ 不可懂 |
| "Welcome to the Smart Retail Zone! Here, you can explore innovative零售技术 and solutions." | `Welcome to the smart retail zone. here. you can explore any of video technologies and solutions.` | `Welcome to the Slight Reconphone! …` | ⚠️ 部分可懂（开场句可懂，专名不可懂） |

**对照中文**（同一批测试，同一套回听方式）：

| 模型说的中文 | SenseVoice 回听 | 判定 |
|---|---|---|
| "智慧零售区，欢迎参观！" | `智慧零售区欢迎他们。` | ✅ 基本逐字 |
| "这里是充满关怀的主动关怀区域。" | `这里是充满关怀的主动关怀。` | ✅ 几乎逐字 |

**说明**：ASR 回听只是客观代理指标，人耳对带口音的英文容忍度更高——**务必自己听一遍**：

```bash
open /tmp/glm_voice_poc_short2      # 收紧人设后的中英样本
open /tmp/glm_voice_poc_en          # 纯英文问答样本
```

**影响与建议**：
- 中文访客：GLM-4-Voice 可用（延迟与清晰度都够）。
- 英文访客：建议仍走 **pipeline 引擎**（qwen3-tts 的英文），或后续评估千问 Omni / GLM-Realtime 的英文表现。
- 需要做一次 **A/B 客观对比**：同一句英文分别用 `qwen3-tts-flash` 和 GLM-4-Voice 合成，用同一套 ASR 回听打分（需要机器人上的 `DASHSCOPE_API_KEY`）。

---

## 5. 语言跟随

| 输入语言 | 回复语言 | 是否符合预期 |
|---|---|---|
| 中文 | 中文 | ✅ |
| 英文（smart retail） | 英文 | ✅ |
| 英文（hello / English sentences） | 英文 | ✅ |
| 英文（smart space, proactive care） | **中文** | ❌（人设里明确要求英文问→英文答） |

→ 语言跟随**大约 3/4 稳定**。若展厅需要严格跟随，建议在 persona 里加更强的约束，或按本地 ASR 的 `lang`（SenseVoice 会给 `<|zh|>`/`<|en|>`）决定用哪个引擎。

---

## 6. 音频格式与转换链路（已验证）

```
GLM 返回 message.audio.data (base64)
   → 解码得裸 PCM 44.1k/单声道/16bit（无 WAV 头）
   → device_wav_bytes(): scipy resample_poly 44.1k→16k（160/441）+ repeat 成双声道
   → 16 kHz / 2ch / 16bit WAV（与线上 aplay 要求一致）
```

回听校验证据：中文回复经上述链路后，本地 ASR 几乎逐字还原 → **采样率假设与重采样实现都正确**。
`glm_voice_pcm_rate` 可配置，出现变调时可改（听感判断）。

---

## 7. 复现命令

```bash
export ZHIPUAI_API_KEY=<智谱 key>

# 三语言基线（默认人设）
python3 scripts/glm_voice_poc.py tests/asr_en_smart_retail.wav \
        tests/asr_zh_smart_retail.wav tests/asr_mixed_smart_retail.wav

# 指定人设 + 单独输出目录
python3 scripts/glm_voice_poc.py tests/asr_mixed_smart_retail.wav \
        --persona "回答只用一句话：中文不超过25个字，英文不超过12个单词。" \
        --out-dir /tmp/poc --log /tmp/poc.jsonl
```

结果同时写入 JSONL（默认 `/tmp/glm_voice_poc.jsonl`），便于多次对比。

---

## 8. 仍未验证

- **CM4 真机延迟**（本机是 Apple Silicon，比 CM4 快；影响的是上传/下载与转码，可忽略，但要把端到端总延迟实测一遍）
- 展厅远场噪声下的可懂度（本批是近讲合成音频）
- 英文发音的人耳最终判定（需你听）
- 连续多轮/并发（V0 并发上限 5）
