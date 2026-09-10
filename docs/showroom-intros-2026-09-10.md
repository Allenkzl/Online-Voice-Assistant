# 展厅讲解三主题路由记录（2026-09-10）

## 目标

将原来的“方案一/方案二/方案三”式讲解，改成面向展厅区域名称的固定讲解：

- 智慧零售
- 智慧空间
- 应急救灾

用户用中文提问时播放中文预制音频；用户用英文关键词提问时播放英文预制音频。讲解文本不交给大模型改写，保证展厅口径稳定。

## 当前配置

配置文件：`config/solutions.json`

有效主题：

| id | 中文名 | 英文名 | 中文音频 | 英文音频 |
|---|---|---|---|---|
| `smart_retail` | 智慧零售 | Smart Retail | `assets/solutions/smart_retail_zh.wav` | `assets/solutions/smart_retail_en.wav` |
| `smart_space` | 智慧空间 | Smart Space | `assets/solutions/smart_space_zh.wav` | `assets/solutions/smart_space_en.wav` |
| `emergency_response` | 应急救灾 | Emergency Response | `assets/solutions/emergency_response_zh.wav` | `assets/solutions/emergency_response_en.wav` |

旧的 `solution_1.wav` 和“方案一/方案1/第一套方案/一号方案”路由已退出有效配置。

## 触发词

中文触发词：

- 智慧零售：`智慧零售`、`智能零售`、`零售区`
- 智慧空间：`智慧空间`、`空间区`
- 应急救灾：`应急救灾`、`应急救援`、`救灾区`、`应急区`

英文触发词：

- Smart Retail：`smart retail`、`retail area`、`retail zone`
- Smart Space：`smart space`、`space zone`、`smart space zone`
- Emergency Response：`emergency response`、`disaster relief`、`emergency rescue`、`emergency zone`

注意：当前正式中文入口是“智慧空间”，不把“智慧家居”作为别名触发。

## 音频生成

使用 Reachy Mini 上 `/etc/ova.env` 的 DashScope 配置，通过项目内 `ova.tts.synthesize()` 生成，输出统一为 16 kHz、双声道、16-bit WAV。

生成结果：

| 音频 | 时长 |
|---|---:|
| `smart_retail_zh.wav` | 40.88s |
| `smart_retail_en.wav` | 51.20s |
| `smart_space_zh.wav` | 45.20s |
| `smart_space_en.wav` | 51.76s |
| `emergency_response_zh.wav` | 32.64s |
| `emergency_response_en.wav` | 40.64s |

## 实现位置

- `src/ova/solutions.py`：加载三主题配置，按中英文别名匹配，返回对应语言的 `SolutionIntro`
- `src/ova/dialogue.py`：ASR 后优先匹配展厅讲解；命中后直接播放本地 WAV，并在日志中记录 `SOLUTION_INTRO id=<id> lang=<zh|en>`
- `config/solutions.json`：主题、触发词、固定文案、音频路径

## 验收方法

中文验收建议：

```text
Hey Jarvis
介绍一下智慧零售

Hey Jarvis
介绍一下智慧空间

Hey Jarvis
介绍一下应急救灾
```

英文验收建议：

```text
Hey Jarvis
introduce smart retail

Hey Jarvis
tell me about smart space

Hey Jarvis
introduce emergency response
```

日志验收：

```bash
journalctl -u ova-wake -f
```

预期链路：

```text
WAKE_DETECTED
ASR_RESULT text=...
USER_SAID text=...
SOLUTION_INTRO id=smart_retail lang=zh file=...
LISTENING resumed
```

## 已知限制

当前 ASR 仍是中文 Paraformer，中文触发会更稳。英文触发词已经在路由层支持，但英文口语输入是否能稳定被当前 ASR 转写，需要现场实测；如果英文触发不稳定，下一步再评估加入英文 ASR 或专门的英文关键词识别。
