# 外部文本输入入口（2026-09-15）

## 目标

给只靠麦克风的对话链路加一条**外部文本入口**：展厅键盘（另一个项目 button-bridge）
或本机脚本可以把一句话、一次唤醒送进 `ova-wake`，语义上等于"ASR 识别出了这句话"
或"用户喊了一次唤醒词"。

- 复用现有全部路由：三主题预生成讲解（`config/solutions.json`）、停止、继续、普通问答。
- 复用现有播放与打断：注入时若正在播放，走 `play_interruptible()` 自己的终止路径停下当前音频。
- 不加旁路、不杀 `aplay`、不新增第三方依赖（stdlib `ThreadingHTTPServer`）。
- 动机：现场唤醒词不一定灵敏，按键讲解/按键唤起是可靠入口；讲解口径仍由应用决定。

链路（新增部分用 `→` 标出）：

```text
唤醒(openWakeWord) ──────────────┐
录音(VAD) → ASR → text ──────────┤
POST /inject {text,lang} → 注入队列 ┤→ text → _playback_from_text(text) → 路由
POST /wake {} → 唤醒标志 ─────────┘        ├─ 停止      → 回待唤醒
                                          ├─ 三主题     → 播预制 WAV
                                          ├─ 继续       → 从断点续播
                                          └─ 普通问答   → 引擎(LLM+TTS)
播放由 play_interruptible() 负责：麦克风打断（Hey Jarvis）与外部注入打断共用一条终止路径
```

## 接口

两个端点都由 `ova-wake` 进程提供，默认只监听 `127.0.0.1:8090`。

### `POST /inject`

```json
请求: {"text": "介绍一下智慧零售", "lang": "zh"}
响应: {"ok": true, "routed": "solution_intro", "detail": "smart_retail:zh"}
```

行为（按序）：

1. 文本入队，并请求停止正在播放的音频（等价于一次 barge-in，`aplay` 被终止）；
2. 由**主循环**（待机时）或**播放循环**（播放中）取出文本，调
   `_playback_from_text()` 路由，随后 `_run_playback_loop()` 播放结果；
3. 响应里回报路由结果，`routed` 取值：

| `routed` | 含义 |
|---|---|
| `solution_intro` | 命中展厅主题，播 `config/solutions.json` 里的预制音频（`detail` 为 `id:lang`） |
| `stop` | 命中停止词，结束本轮回到待唤醒 |
| `continue` | 命中继续词且有上一段播放，从断点前 0.6s 续播 |
| `chat` | 其它文本交给引擎（LLM + TTS），`detail` 为 `engine=<名字>` |
| `idle` | 没有可播内容（引擎失败、讲解音频缺失、或没有循环取走该文本） |

`lang`（可选）：显式传入时，用它选择同一个主题的语言版本（`介绍一下智慧零售`
+ `lang=en` 会播 `smart_retail_en.wav`）；**省略时按文本关键词语言匹配**（中文关键词
→ 中文音频，`introduce smart retail` → 英文音频），与语音链路保持一致。

```bash
curl -s -X POST http://127.0.0.1:8090/inject -d '{"text":"介绍一下智慧零售"}'
curl -s -X POST http://127.0.0.1:8090/inject -d '{"text":"介绍一下应急救灾"}'   # 播放中 = 换主题
curl -s -X POST http://127.0.0.1:8090/inject -d '{"text":"停止"}'
curl -s -X POST http://127.0.0.1:8090/inject -d '{"text":"今天天气怎么样"}'      # 引擎回答
curl -s -X POST http://127.0.0.1:8090/inject -d '{"text":"introduce smart retail","lang":"en"}'
```

### `POST /wake`

```json
请求: {}
响应: {"ok": true}
```

等价于一次唤醒命中：主循环播唤醒提示音后进入一轮"听访客说话 → ASR → 回答"
（`run_dialogue_round()`）。用途：按键代替喊 `Hey Jarvis`。重复请求会合并成一次待处理唤醒。

## 参数

| 配置项 | 环境变量 | 默认 | 说明 |
|---|---|---|---|
| `inject_host` | `WAKE_INJECT_HOST` | `127.0.0.1` | 监听地址；同机按键/脚本用，保持本机即可 |
| `inject_port` | `WAKE_INJECT_PORT` | `8090` | 端口；**`0` = 关闭该入口**（麦克风主链路不受影响） |

入口开关与 `dialogue` 无关：关闭端口就没有监听线程；打开端口后即使 `dialogue=false`，
注入文本也会被路由（讲解/停止/继续仍可用，普通问答会按配置建引擎）。

## 实现位置

- `src/ova/dialogue.py`
  - `InjectControl` / `InjectedTurn` / `INJECT`：注入队列 + 外部打断标志（单一消费者是主线程）。
  - `inject_text(text, lang=None)`：入队并等待路由结果（默认 12s 超时）；`trigger_wake()`：置一次唤醒标志。
  - `play_interruptible()`：主循环里除麦克风打断外，新增检查外部打断；命中即 `proc.terminate()`，
    返回 `PlaybackResult(external=True)`（与麦克风打断区分）。
  - `_run_playback_loop()`：外部打断时先取注入队列里的文本直接路由，不再听麦克风；队列为空才走原路径。
  - `_playback_from_text()`：新增 `lang` 与 `route`（回报 `routed`/`detail`）；`samples is None`
    但文本存在时允许 `engine.respond(None, text, cfg)`——按键注入的普通问答靠这一条。
- `src/ova/wake.py`
  - `DEFAULTS` / `ENV_MAP` / `_to_type`：`inject_host`、`inject_port`。
  - `ensure_dialogue_engine()`、`answer_injected_turn()`：注入文本的引擎懒加载与一轮处理。
  - `main()`：启动时拉起 inject 服务线程；待机循环每帧轮询注入队列与唤醒标志，
    有注入 → 跳过唤醒音与 ASR 直接路由；有 `/wake` → 走正常一轮对话。
- `src/ova/inject.py`（新增）：stdlib `ThreadingHTTPServer` + `/inject`、`/wake` 两个 handler，
  `start_inject_server(cfg)` 在 daemon 线程里 `serve_forever`。
- `config/hardware/reachy-mini.json`、`config/example.json`：记录新参数。

## 日志验收

```text
INJECT_TEXT text=介绍一下智慧零售 lang=-          # 收到注入
SOLUTION_INTRO id=smart_retail lang=zh file=...  # 命中主题（语音/注入共用同一行）
INJECT_ROUTED routed=solution_intro detail=smart_retail:zh
INJECT_INTERRUPT name=智慧零售讲解 elapsed=3.42s  # 注入打断了在播音频
INJECT_NEXT text=介绍一下应急救灾 lang=-          # 播放循环取走下一条注入
BARGE_STOP text=停止                               # 停止词路由
WAKE_TRIGGERED source=external                    # POST /wake 收到
```

注入事件也会写入调试台时间线（`svc_event`，需要 `HJV_EVENT_FILE`）：
卡片 `inject`（收到文本 / 路由结果）、`wake`（外部触发）、`dialog`（被外部注入打断）。

## 现场验收（部署后）

1. `curl -X POST :8090/inject -d '{"text":"介绍一下智慧零售"}'` → 播 `smart_retail_zh.wav`，日志 `SOLUTION_INTRO id=smart_retail lang=zh`。
2. 播放中再 inject 一个主题 → 立即中断改播新主题，日志 `INJECT_INTERRUPT` + `INJECT_NEXT`。
3. inject `{"text":"停止"}` → 停止播放回待唤醒，日志 `BARGE_STOP`。
4. inject `{"text":"今天天气怎么样"}` → 引擎回答（验证无音频文本也能问答）。
5. `curl -X POST :8090/wake` → 进入一轮对话开始听麦克风（日志 `LISTENING`）。
6. 回归：现场唤醒词、喊 `Hey Jarvis` 打断、注入后麦克风仍能正常录音。

## 已知限制

- **不替代麦克风打断机制**：`barge_in=false` 时 `play_interruptible()` 走的是同步
  `backend.play_file()`，此时注入只能等当前播放结束才生效（展厅配置 `barge_in=true`）。
- **输入不做语言识别的兜底**：`routed` 完全由文本关键词决定；文本不含主题词就一定落到引擎（会走云）。
- **不抢占"听麦克风"阶段**：注入文本若在一轮对话的 VAD 录音期间到达，要等这一轮结束后才被取走。
- **`继续` 需要上一段播放**：待机时注入"继续"没有断点，会当普通问题交给引擎（与语音路径一致）。
- **`/inject` 是同步等待路由结果**：普通问答要等引擎（LLM+TTS）返回后才响应；超过 12s 先返回
  `{"routed":"idle","detail":"queued, ..."}`，文本仍在队列里继续处理。
- **依赖播放状态文件**：注入触发的播放同样写 `speaking_state_file`（播放中 `true`，结束 `false`），
  Reachy 待机动作 watchdog 的行为与语音播放一致。
- **无鉴权**：默认只绑 `127.0.0.1`，所以要跨机调用必须显式改 `WAKE_INJECT_HOST`，届时需要自行加网络层保护。