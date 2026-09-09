# Online Voice Assistant

唤醒词 → 语音对话的全链路语音助手，**本地唤醒/识别 + 在线大模型/TTS**。

- **唤醒词**：openWakeWord（本地 ONNX，离线，~25ms/帧）
- **拾音 + VAD**：ALSA（dsnoop 共享入口 / 自动探测），回声防护 + 音量归一化
- **ASR 语音转文字**：sherpa-onnx Paraformer 中文（本地，可换 small/int8 模型）
- **LLM**：通义千问（DashScope OpenAI 兼容接口，`qwen-flash`），带实时天气工具
- **TTS**：qwen3-tts-flash（在线合成，本地 16k 重采样播放）
- **调试台**：网页分环节测试（唤醒打分/听写/请求/响应/TTS/全链路时间线）

架构图与各环节说明见 [docs/architecture.md](docs/architecture.md)。

## 快速开始（Linux + 麦克风/音箱）

```bash
# 1. 依赖（Python 3.10+，需要 arecord/aplay）
python3 -m venv .venv
.venv/bin/pip install -e .

# 2. 下载模型（默认小 ASR；加 full 参数换更准的 int8 大模型）
./scripts/download_models.sh        # 或 ./scripts/download_models.sh full

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
dsnoop/dmix 入口、int8 ASR、对话模式默认开）。

## 调试台（强烈推荐先用它分环节验证）

```bash
.venv/bin/python -m ova console     # 默认 http://0.0.0.0:8080
```

页面卡片：① 唤醒词实时打分 / ② 拾音+听写（单按钮循环） / ③ 文字→LLM 请求 /
④ LLM 响应(含工具调用过程) / ⑤ TTS 合成播放 / 底部全链路时间线。
主服务事件（唤醒/识别文本/千问回答/播放）也会汇入时间线（设 `HJV_EVENT_FILE=/tmp/ova_events.jsonl`）。

## 对话模式关键参数（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `WAKE_DIALOGUE` | `0` | `1` = 唤醒应答后自动进入一轮 听→识别→千问→TTS |
| `WAKE_THRESHOLD` / `WAKE_HITS` | `0.2` / `3` | 唤醒灵敏度：分数阈值 / 连续帧数 |
| `WAKE_END_SILENCE_S` | `1.2` | 判定“说完了”的静音时长 |
| `WAKE_LISTEN_DELAY_S` | `0.6` | 应答后等回声消散再开始听 |
| `WAKE_ASR_MODEL_DIR` | `models/asr_paraformer_zh_small` | ASR 模型目录 |
| `CHAT_MODEL` | `qwen-flash` | 千问模型 |
| `TTS_MODEL` / `TTS_VOICE` | `qwen3-tts-flash` / `Cherry` | 合成模型/音色 |
| `HJV_EVENT_FILE` | 空 | 写事件 JSONL 供调试台时间线显示 |

## 目录结构

```
src/ova/           # 包：config/audio/vad/wake/asr/tools/llm/tts/dialogue/console/calibrate + cli
config/            # 参数示例 + hardware/ 设备档案
scripts/           # 模型下载、服务安装
deploy/            # systemd 单元模板
assets/            # 应答/兑底音频（16k 立体声 wav）
models/            # 模型目录（.onnx 不入库，下载脚本填充；含 SHA256SUMS）
tests/             # 正/负样本 wav 与自测
docs/architecture.md
```

## License

- Code: MIT (see LICENSE). Models: see MODEL_NOTICE.md.
