# Reachy Demo 轻量待机动作记录（2026-09-10）

## 目标

替代官方 recorded move 高频播放，降低 `reachy-mini-daemon` 长时间运行后的动作 API 压力：

- 待机时每 20 秒做一次小幅头部/触角动作。
- OVA 正在播放讲解或 TTS 时每 10 秒做一次小幅动作。
- OVA 播完后自动回到 20 秒节奏。
- 保留当前状态标签和远端备份，方便回退。

## 原因判断

现场日志显示，旧版 `/opt/reachy-demo/demo.py` 在运行约 1 天 4 小时后仍然存活，但调用
`http://localhost:8000/api/move/play/recorded-move-dataset/...` 持续 15 秒超时。
同时 daemon 日志中有大量 `IK error: Collision detected or head pose not achievable!`。

因此问题更像是官方 recorded move 高频播放让 daemon 动作 API/IK/动作队列长期承压，而不是整机休眠、
内存耗尽或 demo 进程退出。

## 新方案

- `deploy/reachy-demo/demo.py`
  - 不再播放官方 recorded move dataset。
  - 只调用 `/api/move/goto` 做小幅度姿态：
    - yaw 约 `-7..7` 度
    - pitch 约 `-4..2` 度
    - roll 约 `-2..2` 度
    - 触角小幅摆动
  - 每次动作后回到中位，降低姿态累积风险。
  - 连续 3 次动作 API 失败后尝试请求 `/api/daemon/restart` 并重新等待 backend。

- `src/ova/dialogue.py`
  - 播放长讲解或 TTS 时写 `/tmp/ova_speaking.state`：
    - `speaking=true`：demo 使用 10 秒节奏。
    - `speaking=false`：demo 使用 20 秒节奏。

## 回退点

- Git 标签：`reachy-demo-before-lite-motion-20260910`
- 远端部署前应备份：
  - `/opt/reachy-demo/demo.py`
  - `/etc/systemd/system/reachy-demo.service`
  - `/home/pollen/ova/src/ova/dialogue.py`
  - `/home/pollen/ova/src/ova/wake.py`

## 验收日志

```text
lite motion yaw=... pitch=... roll=... speaking=False
lite motion yaw=... pitch=... roll=... speaking=True
```

待机时两条 `lite motion` 日志间隔约 20 秒；讲解/TTS 播放时约 10 秒。
