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

## 3. 关键技术参数（实测值，改前先看这里）

| 参数 | 值 | 说明 |
|---|---|---|
| 唤醒 | threshold 0.30 / hits 4 / cooldown 3s | 2026-09-10 展厅误触发调优：正常唤醒 0.94~0.997，旧 0.2/3 过于贴近噪声尖峰；详见 wake-tuning-2026-09-10.md |
| VAD | end_silence 1.2s(对话)/2.0s(调试卡)；listen_delay 0.6s；max 15s | 回声防护+安静帧基线+AGC |
| ASR | paraformer-zh-int8，num_threads 4，加载 ~15s，识别 ~2.5s/句 | AGC target_rms 0.1，max_gain 8 |
| LLM | qwen-flash（enable_thinking=False），system 提示 ≤60字 | 天气必须走 query_weather 工具 |
| TTS | qwen3-tts-flash / voice=Cherry，合成 1~4s | 24k→16k 线性重采样 |
| 展厅讲解 | 三主题：智慧零售 / 智慧空间 / 应急救灾，中英文预制 WAV | 关键词路由见 `config/solutions.json`，音频位于 `assets/solutions/*_{zh,en}.wav` |
| 播放中打断 | `barge_in_threshold=0.30 / barge_in_hits=4` | 长音频播放时监听 `Hey Jarvis`，打断后支持停止、继续、切换讲解；每 2s 记录 `BARGE_LISTENING peak_score/rms` 便于调优 |
| 延迟 | 说完→开口 3~8s（含缓冲音后感知 ~1s） | CM4 实测 |
| 事件桥 | `HJV_EVENT_FILE`=/tmp/ova_events.jsonl | 调试台时间线数据源 |

## 4. 排障方法论（本项目反复验证有效）

1. **先看日志/事件，不猜**：每一步都有埋点（WAKE→VAD→ASR_RESULT→USER_SAID→QWEN_REPLY→REPLY），`journalctl -u ova-wake -f` + 调试台时间线。
2. **分环节隔离测试**：调试台卡片 ①~⑤ 单测，先定位环节再动手。
3. **音频问题先听原始录音**：调试台可回放机器人听到的原始 wav。
4. **校准代替猜阈值**：提示音同步录音 → 逐帧打分 → 按数据定参。
5. 记录命令速查：
   ```bash
   systemctl status ova-wake ova-console          # 服务
   journalctl -u ova-wake -f                       # 主服务日志
   curl -s http://localhost:8080/api/status        # 调试台健康
   tail -f /tmp/ova_events.jsonl                   # 事件桥
   cd /home/pollen/ova && .venv/bin/python -m ova wake --test-wav tests/hey_jarvis.wav
   ```

## 5. 当前状态 / 已知限制 / 待办

**当前**：机器人全链路可用；GitHub 仓库维护中；daemon 媒体已关（动作控制与 demo 不受影响，声卡采集仅 ova-wake 在读）。展厅讲解已从“方案一”试点升级为“智慧零售 / 智慧空间 / 应急救灾”三主题中英文预制音频，并支持播放中喊 `Hey Jarvis` 打断后停止、继续或切换讲解。

**限制**：
- 单轮对话（每轮需重新唤醒）；无多轮记忆
- 千问无实时资讯（天气已接 wttr.in 工具，其余常识性回答）
- 在线 ASR 不可用（账户网关限制）；wttr.in 依赖外网可达
- 模型许可：唤醒模型 CC BY-NC-SA、Paraformer 见各归档内许可——**商业/展厅使用前自行确认**（MODEL_NOTICE.md）
- 调试台/服务默认中文语音内容（"在呢"等），换语种需自备应答 wav（16k 立体声）

**待办/方向**（按用户意向）：
1. 全链路验收后长稳观察（误唤醒率、卡死恢复；当前试运行 `threshold=0.30 / hits=4`）
2. 换硬件实测（ReSpeaker 等）：clone→下载模型→设备档案→calibrate
3. 在线 ASR 对比（待服务商提供 `/v1/audio/transcriptions` 类入口，代码已预留 local/remote 位）
4. 动作联动（调用 daemon :8000 move API）、多轮对话、展厅知识库
5. README 与新/旧仓库互链
