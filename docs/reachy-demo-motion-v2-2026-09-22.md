# Reachy Demo 交互 v2：相位驱动 + daemon 原生人脸跟踪（2026-09-22）

## 背景

v1（`reachy-demo-official-watchdog-2026-09-10.md`）是"随机轮播官方动作"：无语义、幅度偏大、在本机个体上 4 个动作 IK 不可达（已摘除）。daemon 升级到 1.11.0 后具备原生人脸跟踪能力，交互升级为**感知优先**：机器人"看你"而不是"做动作"。

## 抢占问题结论（CM4 资源）

- 基线：ova-wake（openWakeWord+Silero 常驻）47% CPU，daemon 空载 ~65%，load ~2.1——硬件天花板共存，非设计缺陷
- daemon 侧已有让路设计：人脸检测线程 `nice=19`、tracking 关闭时断开相机 feed 零开销
- 实测开 tracking：daemon 仅 +1.3% CPU，ova-wake 无变化 → 不做 OVA 架构重构，走外部协调

## 架构

```
OVA dialogue.py              /tmp/ova_speaking.state (JSON, phase 字段)
  _write_motion_phase ──────► {phase, speaking(旧格式兼容), name, updated_at}
                              长播放每 30s 心跳刷新 updated_at
demo.py v2 每轮读 phase（300s 过期回 idle，容忍 OVA 重启/异常路径）
```

行为表（边沿触发 phase 切换）：

| phase | 行为 |
| --- | --- |
| listening | 开 daemon 人脸跟踪（`POST /api/media/tracking/enable`）注视访客；不可用降级播 `attentive2` |
| thinking | 关 tracking，小幅 look-away（yaw +0.06 rad，0.8s） |
| speaking | 关 tracking，回中位，6 动作池 10s 节奏点缀 |
| idle | 无人：每 ~8s 微幅 gaze 游移（≤±0.05 rad）；有人（`GET /api/media/tracking/face` 的 face_target.detected）：开 tracking 注视，人离开 10s 后关 |

注入路径（按键）：直接 `thinking → speaking → idle`（无 listening，符合"按键即意图"）。

## 开关与降级

- `DEMO_TRACKING=0`：不在 listening/idle 主动启用人脸跟随；thinking/speaking 仍会显式关闭 daemon tracking，避免与头部动作冲突。此开关不会阻止其他入口打开 tracking。
- 从任意相位进入 thinking / speaking 都主动关 daemon tracking，不依赖脚本内的 `tracking_on` 缓存；这覆盖了手动开启跟随后直接按键触发回答的情况。关闭失败时跳过该相位的头部动作，speaking 阶段逐轮重试，不让动作与跟随同时争夺头部。
- tracking 开启失败时 listening 降级播 `attentive2`，不影响对话链路
- 所有 goto 幅度硬上限 ±0.05 rad；watchdog 原样兜底

## Phase 0 实测数据（2026-09-22）

- tracking ON：daemon 68.2%→69.5%，ova-wake 恒 47.1%，load 5 分钟 ≤2.47
- 端到端：`POST :8090/inject {"text":"你好"}` → demo 日志 `phase idle -> thinking -> speaking -> idle`，IK error 0

## 维护

- 改动：`src/ova/dialogue.py`（_write_motion_phase + 4 处调用点 + 播放心跳）、`src/ova/wake.py`（注入路径 thinking）、`deploy/reachy-demo/demo.py`（v2 主循环）
- 备份：机器人 `/opt/reachy-demo.bak-motionv1/`、`dialogue.py.bak-motionv1`、`wake.py.bak-motionv1`
- 现场注意：idle 注视依赖摄像头视野有人脸；夜间/空展厅自动退回 gaze 游移
