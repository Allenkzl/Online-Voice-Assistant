# Online Voice Assistant

唤醒词 → 语音对话的全链路语音助手，**本地唤醒/识别 + 在线大模型/TTS**。

- **唤醒词**：openWakeWord（本地 ONNX，离线，~25ms/帧）
- **拾音 + VAD**：ALSA（dsnoop 共享入口 / 自动探测）+ Silero VAD（sherpa-onnx，本地 ONNX，可回退能量阈值）
- **ASR 语音转文字**：sherpa-onnx 本地识别，默认 SenseVoice 中英双语（中/英/粤/日/韩，带标点），
  可回退中文 Paraformer（`small`/`int8`）；模型类型按目录自动识别
- **LLM**：通义千问（DashScope OpenAI 兼容接口，`qwen-flash`），带实时天气工具
- **TTS**：qwen3-tts-flash（在线合成，本地 16k 重采样播放）
- **对话引擎（可切换）**：`engine=pipeline` 半在线（本地 ASR → 千问 LLM → 千问 TTS）<br>
  `engine=e2e` 端到端（录音直发 GLM-4-Voice，音频直出）——两条链路共用唤醒/VAD/播放/打断/调试台，
  切换只改一个配置项，见 [docs/dual-engine-architecture.md](docs/dual-engine-architecture.md)
- **调试台**：网页分环节测试（唤醒打分/听写/请求/响应/TTS/全链路时间线）
- **外部文本入口**：`ova-wake` 内置只监听本机的 HTTP 入口（默认 `127.0.0.1:8090`），
  展厅按键或脚本可注入一句话、或触发一次唤醒，走完全相同的路由与打断机制
  （见 [docs/external-input-inject-2026-09-15.md](docs/external-input-inject-2026-09-15.md)）

架构图与各环节说明见 [docs/architecture.md](docs/architecture.md)。

## 快速开始（Linux + 麦克风/音箱）

```bash
# 1. 依赖（Python 3.10+，需要 arecord/aplay）
python3 -m venv .venv
.venv/bin/pip install -e .

# 2. 下载模型（含 openWakeWord、Silero VAD、默认 SenseVoice 中英双语 ASR）
./scripts/download_models.sh            # Silero VAD + 中英双语 SenseVoice int8
# ./scripts/download_models.sh full     # 中文回退：paraformer-zh-int8（更准）
# ./scripts/download_models.sh small    # 中文回退：paraformer-zh-small（更快）

# 3. 配置（可用环境变量/命令行覆盖；参考 config/example.json）
cp config/hardware/generic-usb.json config.json   # 或按你的硬件改

# 4. 试运行（先只测唤醒应答）
.venv/bin/python -m ova wake --threshold 0.2 --hits 3

# 5. 开对话模式（需 DASHSCOPE_API_KEY）
export DASHSCOPE_API_KEY=sk-xxxx
export WAKE_DIALOGUE=1
.venv/bin/python -m ova wake
```

> 模型文件不入库：`scripts/download_models.sh` 负责下载并校验；
> 许可声明见 [MODEL_NOTICE.md](MODEL_NOTICE.md)（商业使用前请自行确认）。

## 两条对话链路

```bash
# 半在线（默认）：本地 ASR → 千问 LLM → 千问 TTS
export DASHSCOPE_API_KEY=sk-xxxx

# 端到端：录音直接送 GLM-4-Voice，返回音频直接播
export ZHIPUAI_API_KEY=xxxx
export WAKE_ENGINE=e2e            # 或 config.json 里 "engine": "e2e"

# 离线先验证端到端（不需要机器人，一个 wav 进一个 wav 出）
python3 scripts/glm_voice_poc.py tests/asr_en_smart_retail.wav
```

切换/回退只改 `engine` 一项，不需要改代码或切分支。端到端模式的限制（无工具调用、非流式、
访客语音整体上云）见 [docs/dual-engine-architecture.md](docs/dual-engine-architecture.md)。

## 外部文本输入入口（展厅按键 / 本机脚本）

除麦克风外，`ova-wake` 还提供一个只监听本机的 HTTP 入口，让展厅键盘（button-bridge 项目）
或本机脚本把文本当成"用户说完的一句话"送进来，走**完全相同**的路由
（三主题讲解 / 停止 / 继续 / 普通问答）与打断机制（复用 `play_interruptible()`，
不另杀 `aplay`）。

```bash
# 文本 = 一句识别结果；正在播放时先打断当前播放再路由
curl -s -X POST http://127.0.0.1:8090/inject -d '{"text":"介绍一下智慧零售"}'
curl -s -X POST http://127.0.0.1:8090/inject -d '{"text":"停止"}'
curl -s -X POST http://127.0.0.1:8090/inject -d '{"text":"今天天气怎么样"}'
curl -s -X POST http://127.0.0.1:8090/inject -d '{"text":"introduce smart retail","lang":"en"}'

# 唤醒 = 一次 Hey Jarvis 命中：进入一轮“听访客说话 → ASR → 回答”
curl -s -X POST http://127.0.0.1:8090/wake

# 返回 {"ok":true,"routed":"solution_intro|stop|continue|chat|idle","detail":"..."}
```

监听地址/端口由 `WAKE_INJECT_HOST`（默认 `127.0.0.1`）与 `WAKE_INJECT_PORT`
（默认 `8090`，**设 `0` 关闭该入口**）控制。参数语义、日志与已知限制见
[docs/external-input-inject-2026-09-15.md](docs/external-input-inject-2026-09-15.md)。

## 换硬件适配

1. `arecord -l` / `aplay -l` 查看设备号
2. 复制 `config/hardware/generic-usb.json` 为 `config.json`，填 `input_device` / `output_device`（`auto` 会自动尝试 `default` → `plughw:0,0`）
3. 校准阈值与拾音质量：
   - `.venv/bin/python -m ova calibrate --record 30`（提示音同步录音→自动给出建议阈值）
   - 或起调试台 `.venv/bin/python -m ova console` → 打开 `http://<主机>:8080`，② 卡片逐句听写验证
4. 安装为系统服务（开机自启）：
   ```bash
   sudo scripts/install_service.sh wake /opt/ova myuser
   sudo scripts/install_service.sh console /opt/ova myuser
   ```

已知参考硬件：`config/hardware/reachy-mini.json`（Reachy Mini：共享
dsnoop/dmix 入口、Silero VAD、SenseVoice int8 ASR、对话模式默认开）。

## 调试台（强烈推荐先用它分环节验证）

```bash
.venv/bin/python -m ova console     # 默认 http://0.0.0.0:8080
```

页面卡片：① 唤醒词实时打分 / ② 拾音+听写（单按钮循环） / ③ 文字→LLM 请求 /
④ LLM 响应(含工具调用过程) / ⑤ TTS 合成播放 / ⑥ 端到端（GLM-4-Voice，录音直进音频直出） /
底部全链路时间线。
主服务事件（唤醒/识别文本/千问回答/播放/端到端延迟与用量）也会汇入时间线（设 `HJV_EVENT_FILE=/tmp/ova_events.jsonl`）。

## 部署到机器人（Reachy Mini）

```bash
# 一键部署 + 切引擎（幂等；自动备份代码与 /etc/ova.env，不会覆盖 models/assets）
ZHIPUAI_API_KEY=xxx ./scripts/deploy_to_robot.sh              # 切到端到端 e2e
ENGINE=pipeline ./scripts/deploy_to_robot.sh                  # 回退半在线 pipeline

# 只同步代码不改开关
DEPLOY_CODE=1 ENGINE=e2e ./scripts/deploy_to_robot.sh

# 默认会写入展厅当前试运行参数：WAKE_THRESHOLD=0.28、WAKE_HITS=4、
# WAKE_ACK_BEFORE_REPLY=0、WAKE_VAD_BACKEND=silero、
# WAKE_ASR_MODEL_DIR=models/asr_sense_voice_zh_en_int8；可用同名环境变量临时覆盖。
```

线上要点（详见 [PROJECT_HISTORY.md §1](docs/PROJECT_HISTORY.md)）：

| 项 | 值 |
|---|---|
| 项目路径 / 服务 | `/home/pollen/ova`；`ova-wake`、`ova-console`（:8080） |
| 配置位置 | **没有 config.json**，全部在 `/etc/ova.env`（600） |
| 部署方式 | 目录非 git 仓库 → 用 rsync 覆盖（脚本已封装） |
| 运行用户 | `pollen`（因此能用 `~/.asoundrc` 里的 `reachymini_*` 设备别名） |
| 回退 | `ENGINE=pipeline ./scripts/deploy_to_robot.sh`（10 秒） |

## 对话模式关键参数（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `WAKE_DIALOGUE` | `0` | `1` = 唤醒应答后自动进入一轮 听→识别→千问→TTS |
| `WAKE_ENGINE` | `pipeline` | 对话引擎：`pipeline`=本地ASR→千问LLM→千问TTS（半在线）<br>`e2e`=录音直发端到端语音模型（GLM-4-Voice，需 `ZHIPUAI_API_KEY`） |
| `WAKE_ACK_BEFORE_REPLY` | `0` | 出声前是否先播“嗯，好的”缓冲音；展厅模式默认关闭 |
| `WAKE_THRESHOLD` / `WAKE_HITS` | `0.2` / `3` | 唤醒灵敏度：分数阈值 / 连续帧数 |
| `WAKE_VAD_BACKEND` | `energy` | VAD 后端：`silero`=本地 Silero VAD 模型，`energy`=旧自适应能量阈值 |
| `WAKE_VAD_MODEL_PATH` | `models/silero_vad.onnx` | Silero VAD ONNX 模型路径 |
| `WAKE_VAD_THRESHOLD` | `0.50` | Silero VAD 语音概率阈值 |
| `WAKE_END_SILENCE_S` | `1.2` | 判定“说完了”的静音时长 |
| `WAKE_LISTEN_DELAY_S` | `0.6` | 应答后等回声消散再开始听 |
| `WAKE_ASR_MODEL_DIR` | `models/asr_sense_voice_zh_en_int8` | ASR 模型目录（可指向 paraformer 中文模型目录回退） |
| `WAKE_INJECT_HOST` / `WAKE_INJECT_PORT` | `127.0.0.1` / `8090` | 外部文本入口的监听地址/端口（`POST /inject` 注入文本、`POST /wake` 触发一轮对话）；`0` = 关闭入口 |
| `CHAT_MODEL` | `qwen-flash` | 千问模型 |
| `TTS_MODEL` / `TTS_VOICE` | `qwen3-tts-flash` / `Cherry` | 合成模型/音色 |
| `HJV_EVENT_FILE` | 空 | 写事件 JSONL 供调试台时间线显示 |

## 目录结构

```
src/ova/                  # 包：config/audio/vad/wake/asr/tools/llm/tts/dialogue/console/calibrate + cli
src/ova/engines/          # 对话引擎：base(接口) / pipeline(半在线) / glm_voice(端到端)
config/                   # 参数示例 + hardware/ 设备档案
scripts/                  # 模型下载、服务安装、机器人部署、端到端离线 PoC
deploy/                   # systemd 单元模板
assets/                   # 应答/兑底音频（16k 立体声 wav）
models/                   # 模型目录（.onnx 不入库，下载脚本填充；含 SHA256SUMS）
tests/                    # 正/负样本 wav 与自测（60 项，无需硬件/网络）
docs/
├── architecture.md                 # 架构与延迟预算
├── dual-engine-architecture.md     # 双引擎设计与切换方式（先看这个）
├── PROJECT_HISTORY.md              # 全历程：里程碑、决策、实测、踩坑
├── asr-bilingual-models-2026-09-10.md   # ASR 中英双语选型
├── glm-voice-poc-2026-09-10.md          # 端到端实测（含采样率/音频链踩坑）
├── wake-tuning-2026-09-10.md            # 唤醒阈值调优
├── showroom-intros-2026-09-10.md        # 三主题展厅讲解
├── barge-in-playback-2026-09-10.md      # 播放中打断
├── external-input-inject-2026-09-15.md  # 外部文本/唤醒入口（展厅按键）
└── reachy-demo-official-watchdog-2026-09-10.md  # 待机动作与看门狗
```

## License

- Code: MIT (see LICENSE). Models: see MODEL_NOTICE.md.
