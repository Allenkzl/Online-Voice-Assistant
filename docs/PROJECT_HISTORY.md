# 项目全历程记录（Project History）

> 这份文档面向**接手人/协作者**：按时间线记录本项目从 0 到全链路跑通的关键节点、
> 决策理由、实测数据与踩坑经验，帮助快速建立全局认知。技术架构见
> [architecture.md](architecture.md)，操作手册见仓库 README。

---

## 0. 项目一句话

给展厅机器人（Reachy Mini）做一套 **"Hey Jarvis 唤醒 → 本地拾音/识别 → 千问在线大模型 → 语音回复"** 的语音助手，并封装成可迁移到任意 Linux+USB 声卡硬件的标准项目（GitHub: `Allenkzl/Online-Voice-Assistant`）。

## 1. 硬件与环境基线（2026-09-08 实测）

| 项 | 值 |
|---|---|
| 设备 | Reachy Mini（无线版），树莓派 CM4 4GB，Debian 13 (trixie) aarch64 |
| 系统 | ReachyMiniOS v0.2.3（2026-01-15 官方镜像），Python 3.12(daemon venv)/3.13(系统)/3.11(uv) |
| 声卡 | USB "Reachy Mini Audio"；ALSA 共享入口 `reachymini_audio_src`(dsnoop) / `reachymini_audio_sink`(dmix)，16kHz S16_LE 双声道（`~/.asoundrc` 定义，勿独占 hw:0,0） |
| 开机服务 | reachy-mini-daemon(:8000 动作API / :8443 WebRTC / :7447 zenoh)、reachy-demo(展示动作)、reachy-health、gpio-shutdown、bluetooth、SSH 等 |
| 关键事实 | daemon 进程曾常驻摄像头+麦克风 GStreamer 管线（无人观看也在编码，~40-70% CPU） |
| 线上配置位置 | **机器人上没有 `config.json`**：配置全部走 `/etc/ova.env`（600）——`DASHSCOPE_API_KEY` / `WAKE_ASR_MODEL_DIR` / `HJV_EVENT_FILE` / `WAKE_DIALOGUE` / `WAKE_ENGINE` / `ZHIPUAI_API_KEY`。优先级：命令行 > 环境变量 > config.json > 代码默认值 |
| 项目目录 | `/home/pollen/ova` **不是 git 仓库**（历史原因是打包拷贝部署）→ 更新代码用 `rsync`，不走 `git pull` |
| 权限 | `pollen` 可免密 `sudo`；服务 `ova-wake` 以 `User=pollen` 运行（因此能用 `~/.asoundrc` 里的 `reachymini_*` 设备别名，root 看不到这些别名） |
| 依赖版本 | venv Python 3.11.14；`sherpa-onnx 1.13.7`；numpy 1.26.4；scipy 1.13.1（重采样用 `resample_poly`） |
| 资源 | 4 核 / 3.8GB RAM（可用 ~2.2GB）；根分区 14G 已用 8.5G；空载 load ≈1.7 |

## 2. 时间线（按阶段）

### 阶段一：服务盘点与瘦身（09-08）
- 摸清机器人全部服务职责（详见上表）；发现"69% CPU"来自 daemon 的 WebRTC **自环推流**（127.0.0.1:8443 两端同一进程，无人观看）。
- 用户决策：展厅自用 → 关 daemon 音视频媒体、关 ModemManager 等杂项、保 demo/health。
- ⚠️ 中途被新需求打断，瘦身只完成了排查未落手（**阶段五才真正关掉 daemon 媒体**）。

### 阶段二：唤醒词闭环（09-09，旧仓 Hey-Jarvis-Wake）
- 接手半成品分支 `feature/hey-jarvis-wake`：代码躺在工作区未提交，机器人其实已部署一版（服务 active）。
- 离线验证链：md5 一致性 → 模型 SHA256 → 正样本 hey_jarvis.wav(0.9993) → 负样本 silence(0.0001) → 实声。
- **准确率调优（关键方法论）**：实声命中率仅 1~2/5。做"提示音同步录音校准"（机器人先"滴"再录 30s，逐帧打分分析）发现：
  - 真人喊词得分在 **0.2~0.97 大幅波动**（音量/距离/口音），阈值 0.5 只能抓 ~1/3；
  - 环境静音时底噪得分仅 0.003~0.13 → 存在大安全区间。
  - 最终：`threshold=0.20 + 连续 3 帧确认(hits=3)`（真人喊词持续 ≥0.5s≈8 帧，对真命中无损；噪声尖峰多为单帧）→ 实测 **4/4 命中、静置无误唤醒**。两声道数据完全相同，声道选择无关。
- Bug：兑底音（"网络开小差"）曾放 assets 根目录被应答随机池误播 → 拆 `assets/fallback/` 子目录隔离。
- 提交分支：`29c0f7f`（唤醒+在呢闭环）。

### 阶段三：在线语音对话（09-09，本阶段核心）
- **API 能力探测结论（重要，少走弯路）**：
  - ✅ 千问对话：`/compatible-mode/v1/chat/completions`（OpenAI 兼容）
  - ✅ TTS：原生 `POST /api/v1/services/aigc/multimodal-generation/generation`（qwen3-tts-flash → OSS wav 24kHz mono → 本地重采样 16k 立体声播放）
  - ❌ **ASR 全家不可用**：qwen3-asr-flash 在兼容 REST(404)、实时 WS(400/404)、原生文件转写(要求公网URL且无上传通道)、uploads API(405) 全部失败 → 结论：**当前账户网关未开放音频输入**，在线 ASR 需换服务商（已给用户对比：OpenAI兼容/百度/讯飞/腾讯/阿里NLS）。
  - ✅ 天气：wttr.in（免费，`?format=...&lang=zh`），经千问 function calling（`query_weather`，城市 slug 如 Hangzhou）→ 实测"深圳天气"全链 1.6s。
- 因在线 ASR 不可达 → 本地 ASR：sherpa-onnx Paraformer。
- **回声是最大敌人（重要教训）**：应答"在呢"播完后立即开听，自己声音余响把 VAD 基线抬高 5 倍（基线 188→阈值 938，正常 40~90），导致用户的话被当噪声截掉（表现为"听不懂/只识别到半句"）。对策：`listen_delay_s` 丢弃回声尾 + 基线取**最安静帧**分位 + 音量 AGC 归一。
- 体验调优链：qwen3.8-max→**qwen-flash**(0.6s)；回答限长 60~80 字；先播"嗯，好的"缓冲音（说完 ~1s 即有声音，感知提速）；静音判定 1.2s。
- 关键修车记录（每个都是坑）：
  1. 唤醒后随机播到兑底音（应答池隔离，见阶段二）
  2. 网页调试台 `save_audio` 把 `wave_open` 当上下文管理器（NoneType）→ 录音保存崩
  3. 按钮点击"卡死"：全局替换把 `go()` 内部改成调用自身 → 无限递归
  4. "第一个任务成功第二个必炸"：自定义线程属性 `_stop` **覆盖了 threading.Thread 私有方法 `_stop()`** → 改名 `_stop_evt`
  5. 天气工具必失败：URL 中文字段未百分号编码 → `ascii codec` 错误（curl 自动编码掩盖了它）
  6. Mac 打包 tar 混入 `._*` AppleDouble 文件被当 wav → 应答随机选中即崩（清理 + respond 跳过坏文件防护）
  7. 包化后 wake.py 懒加载仍是旧扁平路径 → ModuleNotFoundError
- 调试台（web console）从 6 卡片演进：分环节独立测试 + SSE 时间线 + 主服务事件桥（`HJV_EVENT_FILE` JSONL），最终 ② 卡片 = **单按钮循环**（模拟唤醒：蓝→点→红聆听→停2s识别→恢复蓝）。
- 09-09 上午：daemon 加 `--deactivate-audio` 重启（launcher.sh 已备份）→ 媒体线程归零、**麦克风独占给唤醒服务**；注意 daemon 停止耗时较长（要摆睡眠姿态），demo 需手动拉起。

### 阶段四：ASR 本地升级（09-09）
- small(78MB,~0.6s/句) → **paraformer-zh-int8 全尺寸(228MB, ~2.5s/句)** + **AGC 音量归一**（15% 低音量仍 100% 识别）。
- 换法：`scripts/download_models.sh full` → 改 `WAKE_ASR_MODEL_DIR`。旧 small 保留可回退。
- 用户结论：本地先顶着用，线上模型等整体流程跑通后再对比优化。

### 阶段五：工程化封装发布（09-09）
- 决策（用户确认）：**新仓库为主** `Allenkzl/Online-Voice-Assistant`；旧 `Hey-Jarvis-Wake` 留档；公开+MIT；**完整 src/ova 包化**；**先机器人回归验证再推送**。
- 迁移要点：
  - 模块→包：config/audio/vad/wake/asr/tools/api/llm/tts/dialogue/console/calibrate + `python -m ova wake|console|calibrate`
  - 硬件档案 `config/hardware/{reachy-mini,generic-usb}.json`；设备 auto 回退链 `reachymini_*→default→plughw:0,0`
  - 机器人部署：项目放 `/home/pollen/ova`；venv **软链**旧 `.venv`（包用 `pip install -e . --no-build-isolation` 装入，避免 pip 网络不稳）；密钥与配置入 `/etc/ova.env`(600)
  - 服务：`ova-wake.service`（dialogue=on）/ `ova-console.service`(:8080)，模板 `deploy/*.service`，`scripts/install_service.sh wake|console`
  - 提交：`432af15` 全量；`57cb165` 修复（懒加载包路径/坏wav防护/品牌名）
- 迁移后机器人回归：**全链路实测通过**（唤醒→在呢→问天气→语音回答，网页时间线可见每步）。

### 阶段六：展厅能力建设与线上调优（09-10 白天）

这一天把项目从"能对话"推到"能在展厅用"，每件事都有独立专题文档：

| 里程碑 | 结果 | 记录 |
|---|---|---|
| 唤醒误触发调优 | 线上 `threshold 0.20/3 → 0.30/4`（正常唤醒 0.94~0.997，旧值贴近噪声尖峰） | `wake-tuning-2026-09-10.md` |
| 三主题展厅讲解 | 智慧零售 / 智慧空间 / 应急救灾，各中英双语预制 WAV，关键词路由 | `showroom-intros-2026-09-10.md` |
| 播放中打断 | 播放期间继续监听 `Hey Jarvis`，命中后可"停止 / 继续 / 切换讲解" | `barge-in-playback-2026-09-10.md` |
| 待机动作 | 官方 recorded move 库 + `reachy-demo-watchdog`（自动处理卡住 move / demo 无心跳 / daemon API 连续失败） | `reachy-demo-official-watchdog-2026-09-10.md`（轻量 `goto` 版留档） |
| **中英双语 ASR** | 中文 Paraformer → **SenseVoice int8**（中/英/粤/日/韩，带标点与 `lang` 标签），Paraformer 保留为回退；`asr.py` 按目录内容自动判别 `sense_voice|paraformer|transducer` | `asr-bilingual-models-2026-09-10.md` |

**ASR 换型的核心数据**（本地实测 + 官方基准）：

- 旧模型对英文只能输出字母串（"Welcome to the Smart Retail area" → `w e c o e t t t h h m a r e e t a l e a`），句中英文词被音译（LoRa → "喽拉"）→ 英文触发词实际不可用
- SenseVoice 与 X-ASR/Whisper/Dolphin 横向对比后选定：体积与旧模型相同（228MB）、算力同量级、中英混说与 12dB 噪声下均正确、带语种标签
- 新增**静音幻觉护栏**：SenseVoice 在纯静音上会吐韩文 `'그.'`（旧 Paraformer 会吐"嗯"/"嗯好的"）→ 去标点后 ≤1 个字符即判为"没说话"，返回空串
- 分支治理：`feat/asr-sensevoice-bilingual` 合并回 `dev`（**保留两个独立提交**：ASR 双语、待机动作 watchdog），已合并分支删除；`main` 是 `dev` 祖先无需合并

### 阶段七：双引擎架构 + 端到端模型上线（09-10 下午，本次重点）

**目标**：让同一条机器人既能跑"半在线"链路（本地 ASR → 千问 LLM → 千问 TTS），也能跑"端到端"链路
（录音直发 GLM-4-Voice，音频直出），并且**部署时能随意切换**。

**关键决策：不用两条 git 分支，用一个配置开关**

两条链路共享唤醒、VAD 采集、回声防护、播放、播放中打断、事件日志、调试台、systemd 部署
（约 90% 的代码），只有"中间那一步"不同。分两条 git 分支会导致共享层双份维护、现场切换要
`git checkout`，而两条链路的依赖并不冲突（一个 venv 装得下）。于是：

```
src/ova/engines/
├── base.py       # Engine 协议(name/needs_transcript/respond) + Reply + EngineError + build_engine()
├── pipeline.py   # 半在线：本地 ASR 文本 → 千问(含天气工具) → 千问 TTS（原路径原样搬入，日志/事件不变）
└── glm_voice.py  # 端到端：录音 → GLM-4-Voice → 音频规整 → 设备格式 WAV
```

- `dialogue.py` 只负责"听 → 本地命令路由 → 问引擎 → 播 → 打断"，与引擎解耦；
  切换用 `engine=pipeline|e2e`（或 `WAKE_ENGINE`），**回退不改代码**
- `needs_transcript` 是关键：`e2e` 为 False → **完全不跑本地 ASR**（CM4 上省 2.5~4s）
- 调试台新增**卡片⑥**（录音直进端到端，显示回复文本/延迟分解/tokens/成本，可回放）
- 离线 PoC `scripts/glm_voice_poc.py`：wav → GLM → wav，不需要机器人即可验证
- 提交：`9bad38f`（抽引擎，行为不变）、`f9b4cdc`（e2e 引擎）、`fe0d3f1`（架构文档）

**线上实测（智谱 GLM-4-Voice，机器人真机）**

| 项 | 实测值 |
|---|---|
| 请求延迟 | 1.13 / 1.34 / 1.40 / 1.67 s（p50 ≈ 1.4s），见过一次 6.95s 卡顿 |
| 端到端总延迟 | 约 2.5~3s（VAD 1.2s + 请求 1.4s + 转码/aplay）→ 优于半在线的 3~8s |
| 回复音频 | 收紧人设后常见 3.5~4.0s，但**人设压不住长回答**：实测出现过 6.1s / 7.9s（22 字） |
| 成本 | ¥0.014~0.025/轮（173~306 tokens @ ¥80/百万）；逐轮明细见 `glm-voice-poc-2026-09-10.md` 附录 A |
| 内容正确性 | 本地 ASR 回听逐字一致；中文发音清晰 |
| 已知短板 | 英文发音可懂度差（两个独立 ASR 均无法还原 "smart retail"）；语言跟随不稳；12.5 token/s 编解码器有"电子味" |

**上线踩坑：一条"听不清"背后叠了 4 层原因（逐层剥离）**

1. **采样率假设错（主因，也是最贵的坑）**：官方示例写 `setframerate(44100)`，实际输出是 **24kHz**。
   按 44.1k 播 = **加速 1.84 倍 + 音调拉高**，语速从正常的 3.3 音节/秒变成 6.6 音节/秒 → 现场反馈"像机器说的叽里咕噜"
2. **开头爆音/直流跳变**：第 0 个采样点 0.350（语音主体峰值才 0.41）→ 每句开头一声机械"咔"
3. **响度比千问 TTS 低 5~7dB**（RMS 0.041 vs 0.080）→ 又轻又薄
4. **峰值过高（峰均比 22dB）**：单纯提音量会削波，必须先把峰值压下来

**修好的音频链**：`裸PCM → 去直流+首尾8ms淡入淡出 → 一极前馈压缩器 → 响度对齐 0.09 → 峰值上限 0.97 → 44.1k/24k→16k 重采样 → 双声道 WAV`

**方法论教训（写进 §4）**：
- 判断采样率**不要**用"让模型复述、比较时长"（隐含假设模型保持输入语速，实际不成立），要用
  **音节速率**（正常 4~6 音节/秒）+ **频谱带边**（按 44.1k 解释时 18–22kHz 仍有 -24dB 能量 = 真实 9.8–12kHz，
  正是 24k 编解码器带限）+ **人耳盲听**三选一确认
- ASR 回听正确 **不等于** 人耳能听懂（采样率错 1.84 倍时 ASR 仍能靠上下文认出字）
- "像机器人"要先分清是**电平/爆音**（可修）还是**音色**（模型固有，后处理无解）

**部署方式（机器人无 config.json，非 git 仓库）**

```bash
# 代码同步（不动 models/assets）
rsync -az --exclude '__pycache__' src/ova/ pollen@reachy-mini.local:/home/pollen/ova/src/ova/
# 配置：往 /etc/ova.env 追加 WAKE_ENGINE=e2e 与 ZHIPUAI_API_KEY=...（600）
sudo systemctl restart ova-wake
# 回退：把 /etc/ova.env 的 WAKE_ENGINE 改回 pipeline 再重启（10 秒）
```

> 长期建议：机器人项目目录改成 git 仓库（`git init` + 远端），避免"rsync 覆盖 + 手动备份"的运维方式。

### 阶段八：现场听感微调（09-11，用户现场反馈驱动）

端到端上线后按“用起来别扭”的三条真实反馈做的调整：

| 调整 | 原因 | 现场做法 |
|---|---|---|
| 唤醒 `0.30/4 → 0.28/4` | 现场要喊两三次才醒，偏钝 | 试运行；仍钝下一档 `0.25/4`，误触发回升退回 `0.30/4` |
| **默认关闭正式回答前的缓冲音**（`ack_before_reply=false`） | 说完一句后先插“好的/嘟嘟”再播回答，观感割裂 | 唤醒成功音 `assets/response.wav` 保留；说完直接静默等待答案 |
| e2e 不再在唤醒后加载本地 ASR | 首轮唤醒后要等 ASR 加载（实测约 13s）才进 VAD | 引擎启动时预热；e2e 下本地 ASR 只在“播放中被打断后的停止/继续短指令”里懒加载（`dialogue._get_command_asr`） |

**权衡记录（重要）**：关掉缓冲音后，“说完→听到声音”的**感知延迟**从 ~1s 变成完整的 2.5~3s（中间没有任何声音填充）。
当前判断是“不插话 > 少等 1 秒”；若现场觉得等待难熬，可重新打开 `ack_before_reply`，或换成更短更轻的提示音。

### 阶段九：Silero VAD + SenseVoice 上线收口（09-11，用户确认效果良好）

在现场确认“叫醒率、断句、正式回复前不插入好的/嘟嘟”效果都比较好后，将试运行状态整理为可合并版本。

| 节点 | 记录 |
|---|---|
| 回滚标签 | `before-silero-sensevoice-20260911`（指向 `cc67e63`） |
| 合并前提交 | `1e5139a feat: use silero vad and sensevoice on reachy` |
| 线上部署 | `/home/pollen/ova` 通过 `scripts/deploy_to_robot.sh` rsync 同步；`ova-wake`、`ova-console` 均为 active |
| 当前主链路 | `WAKE_ENGINE=pipeline`，即本地 ASR → 千问 LLM → 千问 TTS |
| 唤醒参数 | `WAKE_THRESHOLD=0.28`、`WAKE_HITS=4`、`WAKE_ACK_BEFORE_REPLY=0` |
| VAD | `WAKE_VAD_BACKEND=silero`，模型 `models/silero_vad.onnx`（629KB），`WAKE_VAD_THRESHOLD=0.50`、`WAKE_VAD_MIN_SPEECH_S=0.25` |
| ASR | `WAKE_ASR_MODEL_DIR=models/asr_sense_voice_zh_en_int8`，`model.int8.onnx` 229MB，日志 `ASR_READY type=sense_voice threads=4 load=6.8s` |
| KWS | openWakeWord 只加载 `hey_jarvis_v0.1`；`silero_vad.onnx` 已加入 `FEATURE_MODELS` 排除列表，避免被误当作唤醒模型 |
| 存储 | Reachy Mini 根分区约 14GB；部署后约 8.8GB 已用、4.5GB 可用（67%） |

**踩坑记录**：第一次把 Silero 模型放进 `models/` 后，openWakeWord 的模型扫描也读到了
`silero_vad.onnx`，随后在推理时报 `Required inputs (['h', 'c']) are missing from input feed (['x'])`。
修复方式是把 `silero_vad.onnx` 加进 `FEATURE_MODELS` 非唤醒模型排除列表；远端自检确认
`wake_models ['hey_jarvis_v0.1']`、`silero_ok ... window_size=512`、`asr_ok sense_voice`。


| 参数 | 值 | 说明 |
|---|---|---|
| 唤醒 | threshold 0.28 / hits 4 / cooldown 3s | 2026-09-11 因 `0.30 / 4` 现场唤醒变钝，按回退路径先试 `0.28 / 4`；若仍漏唤醒试 `0.25 / 4`，若误触发回升退回 `0.30 / 4`。详见 wake-tuning-2026-09-10.md |
| VAD | **Silero VAD**（`models/silero_vad.onnx`）；threshold 0.50；min_speech 0.25s；end_silence 1.2s(对话)/2.0s(调试卡)；listen_delay 0.6s；max 15s | sherpa-onnx 本地模型判定说话开始/结束，旧自适应能量阈值保留为 `WAKE_VAD_BACKEND=energy` 回退 |
| ASR | 默认 **SenseVoice int8**（中/英/粤/日/韩，带标点与语种标签）；回退 paraformer-zh-int8。num_threads 4，SenseVoice 模型目录 `models/asr_sense_voice_zh_en_int8` | AGC target_rms 0.1，max_gain 8；静音幻觉护栏：去标点后 ≤1 字符判为没说话。pipeline 模式主链路使用本地 ASR；e2e 模式下 ASR 只服务打断后的“停止/继续”短指令 |
| 对话引擎 | `engine=pipeline`(默认) \| `e2e` | pipeline=本地ASR→千问LLM→千问TTS；e2e=录音直发 GLM-4-Voice（不跑本地 ASR）。两条链路共用唤醒/VAD/播放/打断/事件/调试台，一个配置项切换 |
| 端到端音频 | PCM **24kHz** 单声道 → 去直流+淡入淡出 → 压缩峰值 → 响度对齐 0.09 → 16k 立体声 | 官方示例写的 44100 是错的（会加速 1.84 倍）；参数见 `glm_voice_pcm_rate` / `glm_voice_target_rms` |
| 端到端超时/人设 | `glm_voice_timeout_s=10` / 人设"不超过10个字" | 实测 p50 1.4s；超时即播兜底音；端到端模型没有硬性长度控制，人设只能压低 |
| LLM | qwen-flash（enable_thinking=False），system 提示 ≤60字 | 天气必须走 query_weather 工具 |
| TTS | qwen3-tts-flash / voice=Cherry，合成 1~4s | 24k→16k 线性重采样 |
| 展厅讲解 | 三主题：智慧零售 / 智慧空间 / 应急救灾，中英文预制 WAV | 关键词路由见 `config/solutions.json`，音频位于 `assets/solutions/*_{zh,en}.wav` |
| 播放中打断 | `barge_in_threshold=0.30 / barge_in_hits=4` | 长音频播放时监听 `Hey Jarvis`，打断后支持停止、继续、切换讲解；每 2s 记录 `BARGE_LISTENING peak_score/rms` 便于调优 |
| 待机动作 | 官方 recorded move 库，待机 20s / 说话 10s，外置看门狗 | 现场观感比轻量 `goto` 更灵动；看门狗自动处理卡住 move、demo 无心跳和 daemon API 连续失败 |
| 延迟 | pipeline：说完→开口 3~8s；**e2e：约 2.5~3s**（VAD 1.2s + 请求 1.1~1.9s + 转码） | CM4 真机实测；2026-09-11 起展厅默认关闭"嗯，好的"缓冲音，唤醒提示音后直接等待正式回答 |
| 事件桥 | `HJV_EVENT_FILE`=/tmp/ova_events.jsonl | 调试台时间线数据源 |

## 4. 排障方法论（本项目反复验证有效）

1. **先看日志/事件，不猜**：每一步都有埋点（WAKE→VAD→ASR_RESULT→USER_SAID→QWEN_REPLY→REPLY），`journalctl -u ova-wake -f` + 调试台时间线。
2. **分环节隔离测试**：调试台卡片 ①~⑤ 单测，先定位环节再动手。
3. **音频问题先听原始录音**：调试台可回放机器人听到的原始 wav。
4. **校准代替猜阈值**：提示音同步录音 → 逐帧打分 → 按数据定参。
5. **音频"听不清"要分层定位**（09-10 端到端上线实战总结，顺序别跳）：
   ① **采样率** → 用音节速率（正常 4~6 音节/秒）+ 频谱带边 + 人耳盲听三选一确认；
   ⚠️ 不要用"让模型复述再比较时长"（隐含假设模型保持输入语速，实际不成立，本次据此得出了错误结论）；
   ② **开头爆音/直流** → 看第 0 个采样点幅值，与语音主体峰值对比；
   ③ **响度** → 对比参照物（本项目用千问 TTS：RMS ≈0.08）的 RMS 与峰值；
   ④ **峰均比** → >20dB 说明单纯提音量必削波，要先压缩峰值；
   ⑤ 以上都排除后剩下的才是**音色**（模型固有，后处理无解）。
6. **ASR 回听 ≠ 人耳可懂**：采样率错 1.84 倍时，ASR 仍能靠上下文把字认出来（本次就骗过了我们）；
   回听只能证明"内容没跑偏"，不能证明"人听着舒服"。
7. **判断模型行为要用多次试验**：同一句中文本地测是中文答、机器人上测是英文混答 → 说明是提示词措辞在
   诱导（人设里出现"英文…"就会带偏），不是环境差异。
8. 记录命令速查：
   ```bash
   systemctl status ova-wake ova-console          # 服务
   journalctl -u ova-wake -f                       # 主服务日志
   curl -s http://localhost:8080/api/status        # 调试台健康
   tail -f /tmp/ova_events.jsonl                   # 事件桥
   cd /home/pollen/ova && .venv/bin/python -m ova wake --test-wav tests/hey_jarvis.wav
   ```

## 5. 当前状态 / 已知限制 / 待办

**当前（2026-09-10 收尾状态）**：

- 机器人全链路可用；`dev` 分支为唯一工作分支，全部改动已合并（ASR 双语 + 双引擎 + 端到端上线修复）
- **线上正在跑端到端引擎**：`/etc/ova.env` 里 `WAKE_ENGINE=e2e`（GLM-4-Voice）。实测"说完→开口"约 2.5~3s、
  ¥0.014~0.025/轮；用户现场听感确认为"可以了，后续再微调"
- 展厅讲解三主题中英文预制 WAV + 播放中 `Hey Jarvis` 打断（停止/继续/切换）均在，待机动作为官方
  recorded move 库 + `reachy-demo-watchdog`
- 调试台卡片 ①~⑥ 全可用（⑥ 为端到端验证入口）；`HJV_EVENT_FILE=/tmp/hjw_svc.jsonl`
- 回退一条配置：`WAKE_ENGINE=pipeline` → 重启 `ova-wake`

**限制**：
- 单轮对话（每轮需重新唤醒）；无多轮记忆
- 千问无实时资讯（天气已接 wttr.in 工具，其余常识性回答）
- 在线 ASR 不可用（账户网关限制）；wttr.in 依赖外网可达
- 模型许可：唤醒模型 CC BY-NC-SA、Paraformer 见各归档内许可——**商业/展厅使用前自行确认**（MODEL_NOTICE.md）
- 调试台/服务默认中文语音内容（"在呢"等），换语种需自备应答 wav（16k 立体声）
- **端到端模式（e2e）特有**：
  - **固定讲解的关键词路由失效**——没有本地文本，"介绍智慧零售"由 GLM 临场发挥，不播审核过的预制 WAV
    （可选方案：用端到端返回的**文本**再判一次意图、命中就改播本地 WAV，约 15 行）
  - **英文回中文**：人设里出现"英文…"会把中文问句也带成英文答（实测 3/3），因此人设不提英文；
    代价是英文输入常得到中文回复。要严格跟随需加轻量语种识别（LID，~100ms）
  - **英文发音可懂度差**：两个独立 ASR 都无法还原 "smart retail"，英文访客体验差于中文
  - **音色偏"电子"**：12.5 token/s 神经编解码器特性，后处理无法消除；对比千问 TTS 更"合成"
  - **无 Function Calling**（天气工具只在 pipeline）；**非流式**（没有"边说边播"，打断只能停播放）
  - 请求偶发卡顿（见过 6.95s）→ 超时 10s 后播兜底音

**待办/方向**（按用户意向排序）：
1. **端到端体验微调**（用户原话"后面再微调一下"）：听感/音色是否可接受、回复长度、是否恢复固定讲解路由
2. **e2e 模式下恢复固定讲解**：用返回文本做意图路由 → 命中则改播本地预制 WAV（保留"固定文案铁律"）
3. **英文能力**：轻量 LID 决定语种 + 评估千问 Omni / GLM-Realtime 的英文发音，或英文走回 pipeline
4. **长稳观察**：端到端链路连续多轮稳定性、请求卡顿比例、误唤醒率（当前试运行 `threshold=0.28 / hits=4`）
5. 机器人项目目录改为 git 仓库（去掉 rsync 覆盖式部署）
6. 换硬件实测（ReSpeaker 等）：clone→下载模型→设备档案→calibrate
7. 动作联动（调用 daemon :8000 move API）、多轮对话（GLM 8K 上下文约 20 轮）、展厅知识库
8. README 与新/旧仓库互链
