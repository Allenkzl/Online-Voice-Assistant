# 播放中打断功能记录（2026-09-10）

## 目标

机器人播放长讲解或普通 TTS 回答时，用户可以再次说 `Hey Jarvis` 打断当前播放。打断后进入一条新的指令窗口：

- 说“停止 / 停一下 / 停下 / 别说了 / stop”：结束当前回合，回到待唤醒。
- 说“继续 / 接着 / continue”：从刚才被打断的位置附近继续播放。
- 说“切换介绍智慧空间 / 介绍一下智慧零售 / 介绍一下应急救灾”：切换到新的展厅讲解。
- 说普通问题：按普通对话走 Qwen + TTS 回答。

## 状态机

```text
播放长音频
  ├─ 播放完毕 -> 回到待唤醒
  └─ 播放中听到 Hey Jarvis -> 终止 aplay -> 监听下一句指令
       ├─ 停止 -> 回到待唤醒
       ├─ 继续 -> 从中断点前约 0.6s 继续播放原音频
       ├─ 命中展厅主题 -> 播放新主题音频
       └─ 普通问题 -> Qwen + TTS 回答
```

## 参数

播放中打断使用独立唤醒参数，避免机器人自己的播报声造成误打断：

```text
barge_in = true
barge_in_threshold = 0.45
barge_in_hits = 4
barge_in_max_command_s = 6.0
barge_in_listen_delay_s = 0.2
barge_in_resume_rewind_s = 0.6
```

普通待机唤醒仍使用：

```text
threshold = 0.30
hits = 4
```

## 实现位置

- `src/ova/dialogue.py`
  - `play_interruptible()`：用 `Popen(aplay)` 播放音频，同时启动唤醒监听线程。
  - `_barge_monitor()`：播放中用 openWakeWord 监听 `Hey Jarvis`。
  - `listen_command_after_barge_in()`：打断后听一条新指令。
  - `_playback_from_text()`：把初始指令和打断后指令统一路由到停止、继续、展厅主题或普通对话。
- `src/ova/wake.py`
  - 新增 `WAKE_BARGE_IN*` 环境变量和默认配置。
- `config/hardware/reachy-mini.json`
  - 记录 Reachy Mini 当前推荐参数。

## 日志验收

播放中打断成功时，主服务日志应出现：

```text
PLAYBACK_INTERRUPTED name=... score=... elapsed=...
BARGE_COMMAND text=...
```

如果切换到新讲解，随后应出现：

```text
SOLUTION_INTRO id=smart_space lang=zh file=...
```

如果用户说停止，应出现：

```text
BARGE_STOP text=...
收到停止指令，回到待唤醒
```

## 现场测试建议

1. 唤醒后说“介绍一下智慧零售”。
2. 播放中说 `Hey Jarvis`。
3. 停住后说“切换介绍智慧空间”，确认改播智慧空间。
4. 再播放中说 `Hey Jarvis`。
5. 停住后说“继续”，确认从刚才附近继续。
6. 再播放中说 `Hey Jarvis`。
7. 停住后说“停止”，确认回到待唤醒、不继续说话。

## 已知限制

- 当前依赖麦克风在机器人播报声中仍能听到用户的 `Hey Jarvis`。如果展厅音量太大，可能需要把 `barge_in_threshold` 降到 0.40，或增加麦克风/扬声器物理隔离。
- `继续` 通过裁剪 WAV 从中断时间附近恢复，不是 sample-perfect 的播放器级暂停；当前会从中断点前约 0.6 秒继续，保证听感连贯。
