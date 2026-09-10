# ASR 中英双语模型评估（2026-09-10）

> 触发问题：现状 ASR 听不懂英文。本文给出**结论、候选模型对比、实测数据、推荐方案与改动清单**。
> 实测环境：macOS (Apple Silicon) + sherpa-onnx 1.12.40，`num_threads=4`，输入 16k mono int16，
> 与线上一致的 AGC（`target_rms=0.10 / max_gain=8`）；测试音频为 `say` 合成的中英文与中英混说。
> ⚠️ **RTF 为同机相对值**，用于横向排序，不能直接当 CM4 绝对值；CM4 绝对值需上机实测（见 §6）。

---

## 1. 结论（TL;DR）

1. **是的，现在听不懂英文**：`model.int8.onnx` 是 Paraformer-zh（中文模型），英文进来只会被拆成
   一串字母，例如 "Welcome to the Smart Retail area" → `w e c o e t t t h h m a r e e t a l e a`；
   句中的英文词还会被音译成汉字（LoRa → "喽拉"）。这是模型能力问题，调阈值/AGC 都救不了。
2. 可用的中英双语模型里，**推荐换 SenseVoice-Small int8**：模型体积与现在几乎一样（228MB vs 227MB），
   中英文与中英混说都识别正确，自带标点，附带语种标签（`<|zh|>`/`<|en|>`），12dB 噪声下仍稳定，
   代码改动约 10 行，**保留 paraformer 作为回退**。
3. **备选 X-ASR-zh-en（0.16B，Apache-2.0，1M 小时训练）**：英文公开基准优于 SenseVoice（官方自评），且有
   **真流式版本（160/480/960/1920ms）**，是后续把"说完→开口 3~8s"压下来的最优路径；
   但需要机器人升级 sherpa-onnx（**≥1.13.3**），且其标点版在 1.12.40 上加载报错（已验证）。
4. **排除**：Dolphin-base（英文几乎输出空/乱码）、Whisper-base（需预先指定语种，中文误识别 + 噪声下
   幻觉，CM4 上算力约 5~7 倍于现状，不可用）、FireRedASR2 / Qwen3-ASR / FunASR-Nano（体积/算力超出 CM4 4GB）。

---

## 2. 现状：为什么听不懂英文（实测证据）

| 输入音频 | 现在（paraformer-zh-int8）的输出 |
|---|---|
| "Welcome to the Smart Retail area." | `w e c o e t t t h h m a r e e t a l e a` |
| "This is the smart space zone with proactive care." | `l i s t h e s a t s p a c e s o w t p r o o a c t v c a a r` |
| "Hey Jarvis, introduce the smart retail solution please." | `h a j j a v v i s i i t t r o d u c e t h e s m r r r e e e e e l e l u s p l a s` |
| "这个是 smart retail 展区，请介绍一下。" | `这个是 s m r t r e a a l e 展区请介绍一下` |
| "这个区域用 LoRa 回传数据。" | `这个区域用喽拉回传数据` |
| "智慧零售区，欢迎参观。" | `智慧零售区欢迎参观` ✅ |

中文没问题，英文全废；同时 `config/solutions.json` 里的 `aliases_en`（`smart retail` /
`emergency response` …）**永远不可能被命中**，所以英文讲解分支实际上是不可达代码。

---

## 3. 候选模型对比（sherpa-onnx 官方 asr-models release，可直接下载）

| 模型 | 语种 | 磁盘体积 | Python API | 许可 | 一句话评价 |
|---|---|---|---|---|---|
| **SenseVoice-Small int8**<br>`sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17` | 中/英/日/韩/粤 | 228MB（tar 163MB） | `from_sense_voice()` | FunASR 模型许可（自定义许可：可自由使用/修改/分享，需署名并保留模型名；与现有 paraformer 同族） | **首选**：非自回归，CPU 上比 Whisper 快 5~15 倍；自带标点/ITN 与语种标签 |
| **X-ASR-zh-en int8（offline）**<br>`sherpa-onnx-x-asr-zipformer-transducer-zh-en-int8-2026-06-03` | 中/英 | 168MB（enc154+dec11+joi2.5） | `from_transducer()` | **Apache-2.0** | 0.16B，1M 小时；官方（自评）基准英文优于 SenseVoice、中文基准略逊；全大写、无标点（punct 版需 sherpa-onnx ≥1.13.3） |
| X-ASR-zh-en 流式版<br>`…-streaming-zipformer-transducer-zh-en-int8-2026-06-05`（480ms 等 4 档） | 中/英 | 每档 134MB | `OnlineRecognizer` | Apache-2.0 | **真流式**，边说边出字，可压缩体感延迟；架构改造较大，作为二期 |
| Whisper-base（int8）<br>`sherpa-onnx-whisper-base` | 99 语种 | 153MB(int8)/278MB | `from_whisper()` | MIT | 需**预先指定语种**，中英双语场景要额外做语种判别；噪声下幻觉；CM4 算力不够 |
| Dolphin-base int8<br>`sherpa-onnx-dolphin-base-ctc-multi-lang-int8-2025-04-02` | 40 东方语种 + 22 方言 | 99MB | `from_dolphin_ctc()` | **Apache-2.0** | 体积最小，但**英文实测失败**（空输出/西里尔乱码），中文可用；不推荐双语 |
| Whisper-small / medium / turbo | 99 语种 | 640MB / 1.9GB / 560MB | `from_whisper()` | MIT | CM4 上太慢，直接排除 |
| FireRedASR2（CTC/AED int8） | 中/英 | 520MB / 838MB | `from_fire_red_asr()` | Apache-2.0 | 精度强但体积/算力超预算 |
| Qwen3-ASR-0.6B int8 | 多语种 | 879MB(tar) | `from_qwen3_asr()` | Apache-2.0 | LLM 式解码，CM4 上不可接受 |
| FunASR-Nano int8 | 中/英/日 + 方言口音 | 842MB(tar) | `from_funasr_nano()` | FunASR 许可 | x86 CPU 上仅 3.6x 实时，CM4 太慢 |
| paraformer-trilingual-zh-cantonese-en | 中/粤/英 | 1.0GB（**未发布 int8**） | `from_paraformer()` | FunASR 许可 | 无 int8，CM4 上偏重 |

> 上游参考：FunASR 审计过的历史 CPU 基准里，SenseVoice-Small RTF 0.058（17.2x 实时）**略快于**
> Paraformer-Large 的 0.064（15.6x），CER 7.81% 优于 10.18%；X-ASR 官方公开基准
> （LibriSpeech clean/other、GigaSpeech、WenetSpeech）上 X-ASR-zh-en(0.16B) 均优于 SenseVoice-small(234M)。

---

## 4. 实测结果（同一批音频，同机同线程）

### 4.1 识别质量

| 音频 | paraformer（现状） | **SenseVoice** | X-ASR (offline) | Whisper-base |
|---|---|---|---|---|
| Welcome to the Smart Retail area. | `w e c o e t t t h h m a r e e t a l e a` | `Welcome to the Smart retail area.` ✅ | `WELCOME TO THE SMART RETAIL AREA` ✅ | `Welcome to the Smart Retail Area.` ✅ |
| Hello, can you understand English sentences? | `h e l l o c a n d y u n d d e s d a n d n l l l i i h s e n t o m m s i s` | `Hello, can you understand English sentences?` ✅ | `HELLO CAN YOU UNDERSTAND ENGLISH SENTENCES` ✅ | ✅ |
| 智慧零售区，欢迎参观。 | ✅ | ✅（带句号） | `智 慧 零 售 区 欢 迎 参 观`（字间空格） | `手區歡迎參觀`（繁体+错字） |
| 这个是 smart retail 展区，请介绍一下。 | `这个是 s m r t r e a a l e 展区请介绍一下` | `这个是smart retail展区，请介绍一下。` ✅ | `这 个 是 SMART RETAIL 展 区 请 介 绍 一 下` ✅ | `This is Smart Retail展區, please introduce` |
| 这个区域用 LoRa 回传数据。 | `这个区域用喽拉回传数据` | `这个区域用low拉回传数据。`（专名仍错） | `这 个 区 域 用 了 回 传 数 据`（丢了 LoRa） | `This area is used to restore the transmission`（幻觉） |
| 英文 + 12dB 白噪声 | 全乱 | ✅ 完整正确 | ✅ 完整正确 | ✅（语种设为 en 时） |
| 中文音频 + Whisper 语种设 en | — | — | — | `The Council of the Ministry of Justice…`（幻觉） |

### 4.2 速度 / 内存（Apple Silicon，4 线程，相对值）

| 模型 | 短句 RTF | 长句 RTF | 加载 | 峰值 RSS |
|---|---|---|---|---|
| paraformer-zh-int8（现状） | 0.013~0.019 | 0.013~0.015 | 1.56s | 447MB |
| **SenseVoice int8** | 0.020~0.023 | 0.017~0.018 | 0.74s | 498MB |
| **X-ASR offline int8** | 0.021~0.026 | 0.018~0.019 | 1.11s | 397MB |
| Whisper-base int8 | 0.076~0.097 | 0.079~0.089 | 0.48s | 504MB |

结论：SenseVoice / X-ASR 与现状**同一量级**（约 1.2~1.4 倍耗时、内存相当，X-ASR 内存更省）；
Whisper 是现状的 **5~7 倍**，CM4 上必然超时。

### 4.3 现成可用性

- `from_sense_voice()` / `from_dolphin_ctc()` / `from_transducer()` 在 sherpa-onnx **1.12.40** 上均可用。
- X-ASR 的 **punct（标点版）在 1.12.40 上直接报错**：
  `RuntimeError: Unexpected input data type. Actual: (tensor(int64)), expected: (tensor(int32))`
  → 需要升级到 **≥1.13.3**（changelog：1.13.3 才加入 X-ASR 支持）。非标点版在 1.12.40 可跑。

---

## 5. 推荐方案

### 方案 A（推荐，低风险）：SenseVoice-Small int8 替换 paraformer

- 改动：`src/ova/asr.py` 增加 `from_sense_voice()` 分支（按目录内容自动判别模型类型）+
  `scripts/download_models.sh` 增加 `sensevoice` 选项 + `config/hardware/reachy-mini.json` 的
  `asr_model_dir` 指向新目录；**旧 paraformer 目录保留，改配置即可回退**。
- 额外收益：`stream.result.lang` 给出语种（`<|zh|>`/`<|en|>`），可用于**自动决定中文/英文回复**；
  自带标点，日志与调试台可读性更好（`solutions.py::_normalise` 会去掉标点/空格/大小写，
  所以 `Smart Retail`、全大写、字间空格都能正常命中现有别名）。
- 风险：CM4 上单句耗时预计 2.5s → **3~4s**（同量级但要实测确认）；专名（LoRa）仍会错，
  展厅触发词本身没问题（"智慧零售/smart retail/emergency response"都识别正确）。

### 方案 B（精度优先）：X-ASR-zh-en offline

- 优点：Apache-2.0；英文公开基准更好；内存更省；有配套流式版本可用于二期做"边说边识别"。
- 代价：机器人需 `pip install -U "sherpa-onnx>=1.13.3"`（aarch64 wheel）并用 punct 版才拿到标点与大小写；
  模型较新（2026-06），社区验证少。中文输出字间有空格（对现有 `_normalise` 匹配无影响，但显示不美观）。

### 方案 C（先不换模型的小补丁）

若短期不能动模型：把英文指令改成"**让中控/网页按钮触发英文讲解**"，或提示访客用中文说
"用英文介绍智慧零售"——即用中文指令走英文音频。但这不解决"听明白英文"的根本需求。

### 配套（不在本次范围，但会影响"听懂英文"的体感）

1. `src/ova/llm.py` 的 `SYSTEM_PROMPT` 强制"用中文回答"，英文提问会得到中文答复；
   建议改为"用户说英文就用英文回答"（或按 SenseVoice 的 `lang` 决定）。
2. `TTS_VOICE=Cherry` 的英文发音需实测确认；必要时英文走另一音色。

---

## 6. 上机实测清单（CM4 上确认，10 分钟）

```bash
# 0) 机器人当前 sherpa-onnx 版本（决定 X-ASR punct 版能不能用）
ssh pollen@reachy-mini.local '/home/pollen/ova/.venv/bin/python -c "import sherpa_onnx;print(sherpa_onnx.__version__)"'

# 1) 拉模型（在机器人上；先不覆盖旧模型）
cd /home/pollen/ova
curl -fLO https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
tar -xjf sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2 -C models

# 2) 用生产参数实测耗时（替换 WAKE_ASR_MODEL_DIR 即可，不动代码先验证）
WAKE_ASR_MODEL_DIR=models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17 \
  .venv/bin/python -m ova calibrate   # 或调试台 ② 卡片听写，看 ASR 耗时日志

# 3) 决策依据：连续 5 句中文/英文/中英混说，记录 ASR_RESULT 与耗时（目标 ≤4s/句）
journalctl -u ova-wake -f | grep -E "ASR_RESULT|ASR_READY"
```

判定标准：**英文可识别 + 单句 ≤4s + 内存无压力** → 落地方案 A；若耗时 >5s，考虑
`paraformer-zh-small + SenseVoice` 双模型或走方案 B 的流式版本。

---

## 7. 复现本次实测

```bash
# 目录：/tmp/ova_asrlab（不入库）
#   setup.sh  ← 建 venv、下模型、用 macOS say 生成中英文测试音频
#   prep2.py  ← 生成中英混说与 12dB 噪声音频
#   bench2.py ← 单模型逐条：加载时间、峰值 RSS、RTF、识别文本
# 注：本机 pip 访问 PyPI 有 SSL 问题，改用系统 python3（已含 sherpa-onnx 1.12.40 + numpy）
python3 bench2.py sensevoice
```

---

## 8. 实施记录（方案 A 已落地到测试分支）

分支 `feat/asr-sensevoice-bilingual`（未合并 `dev`，待机器人实测确认）：

| 文件 | 改动 |
|---|---|
| `src/ova/asr.py` | 模型类型自动判别 `sense_voice / paraformer / transducer`；SenseVoice 走 `from_sense_voice(language="auto", use_itn=True)`；日志改为 `ASR_RESULT lang=zh|en text=…`；`ASR_READY` 增加 type 与加载耗时 |
| `scripts/download_models.sh` | 新增模式 `sensevoice`（默认）\|`small`\|`full`，下载后统一改名为 `models/asr_sense_voice_zh_en_int8` 等短名 |
| `config/hardware/reachy-mini.json`、`config/example.json`、`src/ova/wake.py` | `asr_model_dir` 默认值改为 `models/asr_sense_voice_zh_en_int8`（回退中文只需改回 `models/asr_paraformer_zh_int8`） |
| `tests/test_asr_bilingual.py` + 3 个 wav | 模型判别单测（无需模型即可跑）+ 真实识别 + **英文/混说指令 → 命中 smart_retail 英文讲解**（回退模型也测） |
| `README.md`、`MODEL_NOTICE.md`、`docs/architecture.md`、`AGENTS.md`、`console.py` 标签 | 文档与界面同步；SenseVoice 许可（FunASR 模型许可 v1.1，需署名）写入 MODEL_NOTICE |

本地验证（macOS，真实模型文件）：

```bash
OVA_ASR_MODEL_DIR=/path/to/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17 \
OVA_ASR_LEGACY_MODEL_DIR=/path/to/sherpa-onnx-paraformer-zh-int8-2025-10-07 \
python3 tests/test_asr_bilingual.py        # OK: 6/6 passed（pytest 下同样 6 passed）
```

识别结果：英文 `Welcome to the Smart retail area.`(lang=en)、中文 `智慧零售区欢迎参观。`(lang=zh)、
混说 `这个是smart retail展区，请介绍一下。`(lang=zh)；三者都成功路由到 `smart_retail` 讲解
（英文/混说命中 `aliases_en` → 英文音频）；中文回退模型仍能识别中文。
**CM4 上的耗时仍需上机实测**（见 §6），这是决定是否合并 `dev` 的关键指标。

### 静音幻觉防护（实测后新增）

`sherpa-onnx` 的 SenseVoice 在**纯静音/噪声**上会吐出一个韩文字符 `'그.'`；顺带实测现有
Paraformer 也有同类问题（静音→`'嗯'`，噪声→`'嗯好的'`）。空唤醒时这会变成一条假指令送进千问。
`sherpa-onnx` 1.12.40 的 `ys_log_probs` 为空、`event` 恒为 `<|Speech|>`，因此改用**字数护栏**：
识别结果去掉空格/标点后 ≤1 个字符即判为"没说话"，返回 `""` 并记
`ASR_RESULT … (dropped: no speech)`（对话流程本来就把空结果当作没听到）。
短指令不受影响，实测 `停止`→`停止。`、`继续`→`继续。`、`switch to smart space`→`Switch to smartspace.`。
