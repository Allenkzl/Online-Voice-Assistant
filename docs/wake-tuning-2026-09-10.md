# Reachy Mini 唤醒误触发调优记录（2026-09-10）

## 背景

Reachy Mini 放在展厅长时间待机时，出现未说唤醒词也触发的问题。目标是降低误触发，同时保持用户说 “Hey Jarvis” 后快速响应。

本次只调 openWakeWord 的触发门槛，不改唤醒模型、不改 ASR、不改方案讲解逻辑。

## 线上基线

- 设备：`reachy-mini.local`
- 项目路径：`/home/pollen/ova`
- 服务：`ova-wake`
- 模型：`models/hey_jarvis_v0.1.onnx`
- 旧参数：`threshold=0.20 / hits=3 / cooldown=3.0`
- 启动方式：`/home/pollen/ova/.venv/bin/python -m ova wake --threshold 0.2 --hits 3`

触发逻辑：音频每 80ms 打一帧分数，连续 `hits` 帧分数不低于 `threshold` 才认为唤醒。

## 历史日志统计

统计范围：`journalctl -u ova-wake --since '2026-09-09 12:00:00'`

监听窗口：

- `LISTENING` 样本数：1760
- `peak_score` p50：0.0205
- `peak_score` p75：0.0548
- `peak_score` p90：0.1208
- `peak_score` p95：0.1817
- `peak_score` p97：0.2489
- `peak_score` p98：0.3126
- `peak_score` p99：0.4027
- `peak_score` max：0.9798

旧阈值下的高分窗口数量：

- `peak >= 0.20`：80
- `peak >= 0.25`：50
- `peak >= 0.30`：39
- `peak >= 0.35`：32
- `peak >= 0.40`：19

`WAKE_DETECTED`：

- 触发数：24
- 分数：0.9798, 0.2462, 0.9698, 0.2265, 0.2810, 0.2188, 0.7050, 0.2996, 0.3153, 0.2420, 0.3353, 0.2418, 0.4290, 0.3927, 0.3872, 0.2176, 0.2893, 0.2579, 0.2925, 0.2575, 0.3295, 0.2596, 0.3601, 0.7211
- 最低：0.2176
- 中位数：0.2960
- 最高：0.9798

判断：旧参数 `0.20 / 3` 过于贴近展厅噪声尖峰和弱唤醒的交界，容易误触发。

## 现场校准

### 背景样本

命令：录制 40 秒展厅背景音，不说唤醒词。

文件：`/tmp/ova_bg_calib.wav`

分析结果：

- channel 0：0 个疑似唤醒事件，global peak 0.0117
- channel 1：0 个疑似唤醒事件，global peak 0.0117
- 在 `0.20, 0.25, 0.28, 0.30, 0.32, 0.35, 0.40` 阈值下均为 0 次

注意：这段 40 秒没有复现误触发，只说明当时背景很干净，不能代表所有展厅时段。

### 唤醒样本

命令：录制 35 秒，用户每 2-3 秒说一次 “Hey Jarvis”。

文件：`/tmp/hjw_calib.wav`

分析结果：

- channel 0/1 一致
- 7 个疑似事件
- 正常 6 次峰值约为 0.9426 到 0.9970
- 另 1 次峰值 0.1970，疑似说得不完整、太轻或距离较远

按峰值看，`threshold=0.20` 到 `0.40` 都能保留 6 次正常唤醒。

## 连续帧模拟

使用实际模型按服务逻辑模拟 `threshold` 与 `hits` 组合。

背景样本：

| hits | 0.20 | 0.25 | 0.28 | 0.30 | 0.32 | 0.35 | 0.40 | 0.45 | 0.50 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 3 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 4 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 5 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

唤醒样本：

| hits | 0.20 | 0.25 | 0.28 | 0.30 | 0.32 | 0.35 | 0.40 | 0.45 | 0.50 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 3 | 6 | 6 | 6 | 6 | 6 | 6 | 6 | 6 | 6 |
| 4 | 6 | 6 | 6 | 5 | 5 | 5 | 5 | 5 | 4 |
| 5 | 5 | 4 | 4 | 4 | 4 | 4 | 4 | 4 | 4 |

判断：

- `hits=5` 开始明显变钝，不适合作为第一档。
- `threshold=0.30 / hits=4` 只比旧参数多等 1 帧，理论响应慢约 80ms，但能明显降低 0.20 附近噪声尖峰误触发。
- 如果后续测试发现远距离/轻声漏唤醒，优先退到 `threshold=0.28 / hits=4`。
- 如果后续仍误触发，优先升到 `threshold=0.35 / hits=4`，暂不建议直接上 `hits=5`。

## 本次调整

新试运行参数：

```text
threshold = 0.30
hits = 4
cooldown = 3.0
```

### 2026-09-11 追加调整

现场反馈 `0.30 / 4` 唤醒成功率下降，需要叫多声才中一次。本次按本文原定回退路径先试较温和档：

```text
threshold = 0.28
hits = 4
cooldown = 3.0
```

同时保持播放中打断参数不变（`barge_in_threshold=0.30 / barge_in_hits=4`），避免机器人播报时误打断升高。
如果 `0.28 / 4` 仍明显漏唤醒，下一档试 `0.25 / 4`；如果误触发回升，则退回 `0.30 / 4`。

同步修改：

- `deploy/ova-wake.service`
- `config/hardware/reachy-mini.json`
- 线上 `/etc/systemd/system/ova-wake.service`

线上改完后需执行：

```bash
sudo systemctl daemon-reload
sudo systemctl restart ova-wake
systemctl status ova-wake --no-pager -l
journalctl -u ova-wake -n 20 --no-pager
```

2026-09-10 预期日志：

```text
READY model_dir=models responses_dir=assets channel=0 threshold=0.30 hits=4 cooldown=3.0s dialogue=on offline=false
```

2026-09-11 试运行档位的预期日志：

```text
READY model_dir=models responses_dir=assets channel=0 threshold=0.28 hits=4 cooldown=3.0s dialogue=on offline=false
```

## 回退方法

如果后续测试发现 `0.30 / 4` 漏唤醒明显，可以先回退到较温和的 `0.28 / 4`；如果需要恢复本次调优前参数，则回到 `0.20 / 3`。

临时回退线上服务参数。如果服务已经改成从 `/etc/ova.env` 读取参数，优先改环境文件；如果仍是旧的
`ExecStart=... --threshold ... --hits ...`，再改 systemd 命令行参数。

```bash
sudo sed -i 's/^WAKE_THRESHOLD=.*/WAKE_THRESHOLD=0.20/; s/^WAKE_HITS=.*/WAKE_HITS=3/' /etc/ova.env
sudo systemctl daemon-reload
sudo systemctl restart ova-wake
journalctl -u ova-wake -n 20 --no-pager
```

本次远端备份目录：

```text
/home/pollen/ova/backups/codex-20260910-wake-tuning
```

## 下次调优流程

1. 先看误触发发生时段日志，不直接猜参数：
   ```bash
   journalctl -u ova-wake --since 'YYYY-MM-DD HH:MM:SS' --no-pager | egrep 'LISTENING|WAKE_DETECTED|RESPONSE_START|ASR_RESULT'
   ```
2. 统计 `WAKE_DETECTED` 分数和 `LISTENING peak_score` 分布。
3. 录一段真实展厅背景音，覆盖容易误触发的环境。
4. 录一段正常唤醒词样本，包含近距离、远距离、正常音量、轻声。
5. 优先在这些候选档位中选：
   - 召回优先：`0.28 / 4`
   - 当前平衡：`0.30 / 4`
   - 防误触发优先：`0.35 / 4`
6. 每次只改一档，重启服务后做两项验收：
   - 连续正常唤醒 10 次，记录成功次数和体感延迟。
   - 静置 20-60 分钟，记录是否误触发。

## 验收记录模板

```text
日期：
参数：
展厅环境：
正常唤醒测试：__/10
平均距离：
误触发观察时长：
误触发次数：
代表性日志：
结论：
下一步：
```
