# Reachy Demo 官方动作库 + 看门狗记录（2026-09-10）

## 目标

恢复官方 recorded move 动作库，让展厅里的 Reachy Mini 头部动作更灵动；同时新增独立看门狗，在官方动作库再次长时间卡住时自动恢复。

## 背景

前一版轻量待机动作使用 `/api/move/goto` 做小幅头部/触角运动，稳定但现场观感太弱，用户反馈“几乎都不会动”。本次恢复官方动作库，但保留长稳保护。

## 当前方案

- `deploy/reachy-demo/demo.py`
  - 使用 `pollen-robotics/reachy-mini-emotions-library` 官方动作库。
  - 只挑选相对小幅、适合展厅循环的动作：
    - `inquiring2`
    - `enthusiastic1`
    - `dance1`
    - `understanding2`
    - `thoughtful2`
    - `laughing2`
    - `attentive2`
    - `displeased1`
    - `thoughtful1`
    - `come1`
  - 待机节奏：约 20 秒一轮。
  - 说话节奏：约 10 秒一轮，通过 `/tmp/ova_speaking.state` 判断 OVA 是否正在播放讲解或 TTS。
  - 每个官方动作播放完后尝试回到中位，降低长时间偏头停住的概率。

- `deploy/reachy-demo/watchdog.py`
  - 独立于 demo 进程运行，避免 demo 自己卡住时无法自救。
  - 每 15 秒检查一次：
    - `reachy-mini-daemon` 是否 active；
    - `reachy-demo` 是否 active；
    - `http://localhost:8000/api/state/full` 是否连续可用；
    - `/api/move/running` 是否存在超过 90 秒的卡住动作；
    - `/opt/reachy-demo/demo.log` 是否超过 180 秒没有新日志。
  - 恢复顺序：
    1. 卡住的是单个 move：先调用 `/api/move/stop`。
    2. demo 没心跳或 move 卡住：重启 `reachy-demo`。
    3. daemon API 连续 3 次失败：重启 `reachy-mini-daemon`，再重启 `reachy-demo`。
  - 重启有冷却：
    - `reachy-demo` 最短 180 秒重启一次。
    - `reachy-mini-daemon` 最短 600 秒重启一次。

## 关键日志

正常动作日志：

```text
playing official move=<name> speaking=False
playing official move=<name> speaking=True
```

看门狗日志：

```text
watchdog started: interval=15s stuck_move=90s log_stale=180s
stuck move detected; stopped=<n> keys=[...]
restarting reachy-demo
restarting reachy-mini-daemon and reachy-demo
```

## 回退方式

回到前一版轻量动作：

1. 恢复 Git 标签或远端备份：
   - Git 标签：`reachy-demo-before-official-watchdog-20260910`
   - 远端备份目录：`/home/pollen/ova/backups/codex-20260910-before-official-watchdog`
2. 恢复 `/opt/reachy-demo/demo.py` 和 `/etc/systemd/system/reachy-demo.service`。
3. 停用 `reachy-demo-watchdog.service`。
4. 执行：

```bash
sudo systemctl daemon-reload
sudo systemctl restart reachy-demo
sudo systemctl disable --now reachy-demo-watchdog
```

## 后续观察

如果再次出现头部不动，优先查看：

```bash
tail -n 120 /opt/reachy-demo/demo.log
tail -n 120 /opt/reachy-demo/watchdog.log
systemctl status reachy-demo reachy-demo-watchdog reachy-mini-daemon --no-pager
curl -sS http://127.0.0.1:8000/api/move/running
```

判断标准：

- demo 日志仍有 `playing official move=...`：官方动作仍在发起。
- watchdog 日志出现 `stuck move detected`：官方动作队列曾卡住，已尝试停掉。
- watchdog 日志出现 `restarting reachy-mini-daemon`：daemon API 曾连续失败，已做较重恢复。
