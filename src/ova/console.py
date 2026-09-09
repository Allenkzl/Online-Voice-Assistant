#!/usr/bin/env python3
"""Online Voice Assistant pipeline debug console (stdlib only).

Serves a web page at http://<host>:8080 with six independently testable
cards (wake word / capture+VAD / ASR / LLM request / LLM reply / TTS)
plus a full-pipeline timeline, so each stage of the voice dialogue can
be verified and timed in isolation.

Endpoints:
  GET  /                    web UI (embedded HTML)
  GET  /api/status          pipeline config summary
  GET  /api/events          SSE stream of live events
  POST /api/wake/start|stop live wake-score monitor
  POST /api/vad/record      record N seconds, replay raw audio
  POST /api/asr/dictate     one utterance -> text (auto VAD)
  POST /api/asr/continuous  keep dictating segments until stopped
  POST /api/llm             text -> full request/reply/tool trace
  POST /api/tts             text -> synthesize + play on the robot
  POST /api/full            speak one question -> whole chain once
  GET  /api/audio/<id>.wav  replay stored audio
Run: python3 console_server.py [--port 8080]  (uses ./venv modules)
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import queue
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.parse

import numpy as np

ROOT = Path(__file__).resolve().parent
AUDIO_DIR = Path(tempfile.gettempdir()) / "hjw_console"
AUDIO_DIR.mkdir(exist_ok=True)

from ova.wake import (
    AlsaBackend, DEFAULTS, capture_utterance, load_model,
    mono, score_frame,
)
from ova.asr import LocalAsr
from ova import llm, tts
from ova.tools import WEATHER_TOOL, load_tool_calls, query_weather

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("console")

# ---------------------------------------------------------------- events

class Bus:
    """Fan-out to SSE subscribers + ring buffer for late joiners."""

    def __init__(self, maxlen: int = 400):
        self.lock = threading.Lock()
        self.subs: set[queue.Queue] = set()
        self.history: list[str] = []

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self.lock:
            self.subs.add(q)
            for line in self.history:
                q.put(line)
        return q

    def unsubscribe(self, q) -> None:
        with self.lock:
            self.subs.discard(q)

    def emit(self, card: str, level: str, msg: str, **extra) -> None:
        ev = {"t": time.strftime("%H:%M:%S"), "card": card,
              "level": level, "msg": msg, **extra}
        line = json.dumps(ev, ensure_ascii=False)
        with self.lock:
            self.history.append(line)
            del self.history[:-400]
            dead = []
            for q in self.subs:
                try:
                    q.put_nowait(line)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self.subs.discard(q)
        LOG.info("[%s] %s %s", card, level, msg)


BUS = Bus()

# ---------------------------------------------------------------- workers

backend = None
backend_lock = threading.Lock()


def get_backend() -> AlsaBackend:
    global backend
    with backend_lock:
        if backend is None:
            backend = AlsaBackend(DEFAULTS)
        return backend


class Worker(threading.Thread):
    """Runs one test request; kills itself when the client disconnects."""

    def __init__(self, name, fn, *args, timeout=None):
        super().__init__(name=name, daemon=True)
        self._fn = fn
        self._args = args
        self._stop_evt = threading.Event()
        self._timeout = timeout

    def stop(self):
        self._stop_evt.set()

    def stopped(self) -> bool:
        return self._stop_evt.is_set()

    def _guard(self):
        try:
            self._fn(*self._args)
        except Exception as exc:  # surface any stage failure to the page
            BUS.emit("system", "error", f"{self.name}: {type(exc).__name__}: {exc}")

    def run(self):
        if not self._timeout:
            self._guard()
            return
        t = threading.Thread(target=self._guard, daemon=True)
        t.start()
        t.join(self._timeout)
        if t.is_alive():
            BUS.emit("system", "error",
                     f"{self.name} 超过 {self._timeout:.0f}s 未结束，已放弃本次任务"
                     "（旧线程驻留后台，可再次点击重试）")


ACTIVE: dict[str, Worker] = {}
ACTIVE_LOCK = threading.Lock()


TIMEOUTS = {"llm": 45.0, "tts": 120.0, "full": 300.0,
            "asr": 60.0, "asr_cont": None, "vad": 20.0, "wake": None}


def launch(name: str, fn, *args) -> bool:
    with ACTIVE_LOCK:
        if name in ACTIVE and ACTIVE[name].is_alive():
            return False
        w = Worker(name, fn, *args, timeout=TIMEOUTS.get(name))
        ACTIVE[name] = w
        w.start()
        return True


def stop_all() -> None:
    with ACTIVE_LOCK:
        for w in ACTIVE.values():
            w.stop()


def save_audio(samples_stereo: np.ndarray, tag: str) -> str:
    """Persist raw stereo int16 as a wav; returns its filename id."""
    stamp = int(time.time() * 1000)
    fname = f"{tag}_{stamp}.wav"
    path = AUDIO_DIR / fname
    wave_open(path, samples_stereo)
    return fname


def wave_open(path: Path, samples: np.ndarray) -> None:
    import wave
    with wave.open(str(path), "w") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.ascontiguousarray(samples, dtype="<i2").tobytes())


# --- wake monitor ------------------------------------------------------

WAKE_MODEL = None
WAKE_MODEL_LOCK = threading.Lock()


def wake_model():
    global WAKE_MODEL
    with WAKE_MODEL_LOCK:
        if WAKE_MODEL is None:
            WAKE_MODEL = load_model(Path(DEFAULTS["model_dir"]))
            BUS.emit("wake", "info", f"模型加载完成 {next(iter(WAKE_MODEL.models))}")
        return WAKE_MODEL


def wake_monitor():
    from ova.wake import Capture, FRAME
    model = wake_model()
    backend = get_backend()
    BUS.emit("wake", "info", "开始实时监测，请喊 Hey Jarvis（点“停止监测”结束）")
    hits = 0
    with Capture(backend) as cap:
        warmup_end = time.monotonic() + 0.4
        while not ACTIVE.get("wake").stopped():
            samples = mono(cap.read(), DEFAULTS["channel"])
            if time.monotonic() < warmup_end:
                continue
            score = score_frame(model, samples)
            hits = hits + 1 if score >= DEFAULTS["threshold"] else 0
            BUS.emit("wake", "score", "score", score=round(score, 4),
                     hits=hits, threshold=DEFAULTS["threshold"])
            if hits >= DEFAULTS["hits"]:
                BUS.emit("wake", "wake", f"唤醒命中! score={score:.3f}")
                hits = 0
    BUS.emit("wake", "info", "监测已停止")


# --- capture + vad -----------------------------------------------------

def _seed_threshold(seed_rms, busy, emit):
    baseline = float(np.percentile(seed_rms, 10))
    threshold = max(baseline * 5.0, 300.0)
    emit("vad", "info", f"VAD基线 {baseline:.0f} → 阈值 {threshold:.0f}")
    return threshold


def record_seconds(seconds: float):
    backend = get_backend()
    BUS.emit("vad", "info", f"开始录音 {seconds:.0f}s，请说话…")
    from ova.wake import Capture
    blocks = []
    with Capture(backend) as cap:
        end = time.monotonic() + seconds
        while time.monotonic() < end and not ACTIVE.get("vad").stopped():
            block = cap.read()
            blocks.append(block)
            rms = float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))
            BUS.emit("vad", "level", "level", rms=round(rms, 1))
    data = np.concatenate(blocks)
    fname = save_audio(data, "rec")
    BUS.emit("vad", "ok", f"录音完成 {len(data)/16000:.1f}s",
             audio=fname)


# --- asr ---------------------------------------------------------------

ASR = None
ASR_LOCK = threading.Lock()


def get_asr() -> LocalAsr:
    global ASR
    with ASR_LOCK:
        if ASR is None:
            import os
            model_dir = os.getenv("WAKE_ASR_MODEL_DIR") or DEFAULTS["asr_model_dir"]
            ASR = LocalAsr(Path(model_dir))
        return ASR


def _capture_segment(bus_emit, max_s=15.0, end_silence_s=2.0,
                     min_speech_s=0.35, stop_evt=None):
    """One utterance via the shared echo-safe VAD (wake_service)."""
    return capture_utterance(
        get_backend(),
        channel=DEFAULTS["channel"],
        end_silence_s=end_silence_s,
        max_s=max_s,
        min_speech_s=min_speech_s,
        delay_s=0.3,
        on_level=lambda rms: bus_emit("vad", "level", "level", rms=round(rms, 1)),
        stop_check=stop_evt,
    )


def asr_dictate():
    get_asr()
    BUS.emit("asr", "info", "请说一句话（说完停顿约2秒即识别）…")
    got = _capture_segment(BUS.emit)
    if got is None:
        BUS.emit("asr", "warn", "没有检测到语音")
        return
    samples = got
    t0 = time.monotonic()
    text = ASR.transcribe(samples)
    ms = (time.monotonic() - t0) * 1000
    fname = save_audio(np.repeat(samples, 2), "asr")
    BUS.emit("asr", "ok", f"识别({ms:.0f}ms): {text or '（空）'}",
             text=text, audio=fname)


def asr_continuous():
    get_asr()
    BUS.emit("asr", "info", "连续听写模式：随意说话，每句说完自动上屏（点“停止听写”结束）")
    w = ACTIVE.get("asr_cont")
    while w and not w.stopped():
        got = _capture_segment(BUS.emit, stop_evt=lambda: w.stopped())
        if got is None:
            continue
        samples = got
        t0 = time.monotonic()
        text = ASR.transcribe(samples)
        ms = (time.monotonic() - t0) * 1000
        fname = save_audio(np.repeat(samples, 2), "asr")
        BUS.emit("asr", "ok", f"识别({ms:.0f}ms): {text or '（空）'}",
                 text=text, audio=fname)
    BUS.emit("asr", "info", "听写已停止")


# --- llm ---------------------------------------------------------------

def llm_test(text: str):
    BUS.emit("llm", "info", f"发送给千问: {text}")
    req0 = {
        "model": llm.CHAT_MODEL,
        "messages": [
            {"role": "system", "content": llm.SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "tools": [WEATHER_TOOL],
    }
    BUS.emit("llm", "raw", "请求#1(带工具)", payload=req0)
    t0 = time.monotonic()
    first = llm.chat_once(
        [{"role": "system", "content": llm.SYSTEM_PROMPT},
         {"role": "user", "content": text}],
        tools=[WEATHER_TOOL])
    ms1 = (time.monotonic() - t0) * 1000
    BUS.emit("llm", "raw", f"响应#1 ({ms1:.0f}ms)", payload=first)
    calls = load_tool_calls(first)
    if calls:
        BUS.emit("llm", "info",
                 f"模型请求调用工具: {[c[0] for c in calls]} 参数={[c[1] for c in calls]}")
        messages = [
            {"role": "system", "content": llm.SYSTEM_PROMPT},
            {"role": "user", "content": text},
            first,
        ]
        for name, args, call_id in calls:
            if name == "query_weather":
                t1 = time.monotonic()
                try:
                    result = query_weather(args.get("city_slug", "Hangzhou"))
                    ms_w = (time.monotonic() - t1) * 1000
                except Exception as exc:
                    result = f"天气查询失败: {exc}"
                    ms_w = (time.monotonic() - t1) * 1000
                BUS.emit("llm", "raw", f"天气工具结果 ({ms_w:.0f}ms)", payload=result)
                messages.append({"role": "tool", "tool_call_id": call_id,
                                 "content": result})
        BUS.emit("llm", "raw", "请求#2(带工具结果)", payload=messages)
        t2 = time.monotonic()
        second = llm.chat_once(messages)
        ms2 = (time.monotonic() - t2) * 1000
        BUS.emit("llm", "raw", f"响应#2 ({ms2:.0f}ms)", payload=second)
        reply = (second.get("content") or "").strip()
        BUS.emit("llm", "ok", f"最终答复 ({ms1 + ms2:.0f}ms): {reply}", text=reply)
        return reply
    reply = (first.get("content") or "").strip()
    BUS.emit("llm", "ok", f"答复 ({ms1:.0f}ms): {reply}", text=reply)
    return reply


# --- tts ---------------------------------------------------------------

def tts_test(text: str, play: bool = True):
    BUS.emit("tts", "info", f"合成: {text[:60]}")
    t0 = time.monotonic()
    wav = tts.synthesize(text)
    synth_ms = (time.monotonic() - t0) * 1000
    fname = save_audio_bytes(wav, "tts")
    import wave as _w
    with _w.open(io.BytesIO(wav)) as w:
        dur = w.getnframes() / w.getframerate()
    BUS.emit("tts", "ok", f"合成完成 {synth_ms:.0f}ms，音频 {dur:.1f}s",
             audio=fname)
    if play:
        backend = get_backend()
        t1 = time.monotonic()
        backend.play_file(AUDIO_DIR / fname, timeout=120)
        BUS.emit("tts", "ok", f"播放完成 ({(time.monotonic()-t1)*1000:.0f}ms)")
    return fname


def save_audio_bytes(wav: bytes, tag: str) -> str:
    stamp = int(time.time() * 1000)
    fname = f"{tag}_{stamp}.wav"
    (AUDIO_DIR / fname).write_bytes(wav)
    return fname


# --- full pipeline -----------------------------------------------------

def full_test():
    BUS.emit("system", "info", "=== 全链路测试开始：请直接说一句话（天气/攻略/闲聊皆可）===")
    get_asr()
    got = _capture_segment(BUS.emit)
    if got is None:
        BUS.emit("system", "warn", "没有听到语音，测试中止")
        return
    samples = got
    t0 = time.monotonic()
    text = ASR.transcribe(samples)
    asr_ms = (time.monotonic() - t0) * 1000
    fname = save_audio(np.repeat(samples, 2), "full")
    BUS.emit("asr", "ok", f"[全链路] ASR {asr_ms:.0f}ms: {text or '（空）'}",
             text=text, audio=fname)
    if not text:
        BUS.emit("system", "warn", "ASR 结果为空，中止")
        return
    reply = llm_test(text)
    tts_test(reply)
    BUS.emit("system", "ok", "=== 全链路测试完成 ===")


# ---------------------------------------------------------------- http

PAGE = """<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>OVA · 语音链路调试台</title><style>
:root{--bg:#0f172a;--card:#1e293b;--line:#334155;--tx:#e2e8f0;--mut:#94a3b8;
--ok:#4ade80;--err:#f87171;--warn:#fbbf24;--acc:#38bdf8}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);
font:14px/1.5 ui-sans-serif,system-ui,"PingFang SC",sans-serif;padding:16px}
h1{font-size:18px;margin:0 0 4px}h2{font-size:13px;color:var(--mut);font-weight:400;margin:0 0 14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.card h3{margin:0 0 8px;font-size:14px;display:flex;justify-content:space-between;align-items:center}
.tag{font-size:11px;color:var(--mut);border:1px solid var(--line);padding:1px 6px;border-radius:999px}
button{background:#0ea5e9;color:#fff;border:0;border-radius:6px;padding:5px 12px;font-size:13px;cursor:pointer}
button:disabled{opacity:.45;cursor:default}button.ghost{background:transparent;border:1px solid var(--line);color:var(--tx)}
textarea,input[type=text]{width:100%;background:#0f172a;border:1px solid var(--line);color:var(--tx);
border-radius:6px;padding:6px;font:13px/1.4 ui-monospace,monospace}
textarea{height:52px}
.log{font:12px/1.5 ui-monospace,monospace;color:var(--mut);white-space:pre-wrap;word-break:break-all;
background:#0f172a;border-radius:6px;padding:8px;margin-top:8px;max-height:180px;overflow:auto}
.big{font:22px ui-monospace,monospace;margin:4px 0}
audio{width:100%;margin-top:6px;height:30px}
pre{font:11px/1.4 ui-monospace,monospace;white-space:pre-wrap;background:#0f172a;
border-radius:6px;padding:8px;max-height:200px;overflow:auto;margin:6px 0 0}
.lv{display:flex;align-items:center;gap:8px;margin:4px 0}
.bar{flex:1;height:10px;background:#0f172a;border-radius:5px;overflow:hidden}
.fill{height:100%;width:0%;background:linear-gradient(90deg,#22d3ee,#4ade80)}
.row{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0}
.spark{display:flex;align-items:flex-end;gap:1px;height:44px;margin-top:6px}
.spark i{flex:1;background:var(--acc);opacity:.8;min-height:1px}
.tl{font:12px/1.6 ui-monospace,monospace;max-height:220px;overflow:auto;
background:#0f172a;border-radius:8px;padding:8px}
.tl b{color:var(--acc)}.tl .ok{color:var(--ok)}.tl .err{color:var(--err)}.tl .warn{color:var(--warn)}
.banner{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
</style></head><body>
<div class=banner><div><h1>🎙 Online Voice Assistant · 语音链路调试台</h1>
<h2>每张卡片可独立测试；底部时间线展示全链路事件与耗时 <span class=tag id=ver></span></h2></div>
<div id=st></div></div>
<div class=grid>
 <div class=card><h3>① 唤醒词识别 <span class=tag>本地 · hey_jarvis ONNX</span></h3>
  <p>实时模型打分：喊 “Hey Jarvis” 看分数跳动（阈值 0.2 / 连续3帧）。</p>
  <div class=row><button id=wStart>开始监测</button><button id=wStop class=ghost disabled>停止</button></div>
  <div class=big id=wScore>--</div><div id=wHits></div><div class=spark id=wSpark></div>
  <div class=log id=wLog></div></div>
  <div class=card><h3>② 拾音 + ASR 听写 <span class=tag>本地 · VAD + Paraformer-zh</span></h3>
   <p>点击按钮后直接说话，停顿约2秒自动转文字（原始录音可回放）。按钮会循环：蓝(待命)→红(聆听)→识别→恢复蓝色。</p>
   <button id=micBtn style="width:100%;padding:14px;font-size:16px;background:#0ea5e9">
   🎤 点我开始（相当于喊“Hey Jarvis”）</button>
   <p style="font-size:12px;color:var(--mut)">聆听中再点一次可取消；说完停顿约2秒自动识别</p>
   <div class=lv><div class=bar><div class=fill id=vFill></div></div><span id=vRms>0</span></div>
   <div class=big id=aText style="color:var(--acc)">（识别结果）</div>
   <audio id=vAudio controls style=display:none></audio>
   <div class=log id=aLog></div></div>
 <div class=card><h3>③ 文字 → LLM（请求） <span class=tag>在线 · qwen-flash</span></h3>
  <p>输入文字点发送：展示实际发出的请求（模型/提示词/工具）。</p>
  <textarea id=lIn placeholder="例如：杭州今天天气怎么样？"></textarea>
  <div class=row><button id=lSend>发送给千问</button></div><pre id=lReq>（请求内容将显示在这里）</pre></div>
 <div class=card><h3>④ LLM 响应 <span class=tag>在线 · DashScope</span></h3>
  <p>展示返回内容与耗时；若走天气工具会显示工具调用全过程。</p>
  <div class=log id=lLog></div><pre id=lRaw>（响应/工具调用过程将显示在这里）</pre></div>
 <div class=card><h3>⑤ TTS 合成 + 播放 <span class=tag>在线 qwen3-tts · Cherry</span></h3>
  <p>输入文字→合成并让机器人开口播放；可回听合成结果。</p>
  <textarea id=tIn placeholder="例如：杭州今天小雨，气温23度，出门记得带伞哦。"></textarea>
  <div class=row><button id=tSend>合成并播放</button><button id=tOnly class=ghost>仅合成</button></div>
  <audio id=tAudio controls style=display:none></audio><div class=log id=tLog></div></div>
</div>
<div class=card style=margin-top:12px><h3>⏱ 全链路时间线 <span class=tag>唤醒后直接说话即可测（本页不含唤醒词）</span></h3>
 <div class=row><button id=fRun>▶ 全链路测试（说完话自动执行）</button><button id=fStop class=ghost>停止录音</button></div>
 <div class=tl id=tl>（等待事件…）</div></div>
<script>
const $=id=>document.getElementById(id);
const log=(el,msg,cls='')=>{const d=document.createElement('div');d.className=cls;
 d.textContent=`[${new Date().toLocaleTimeString()}] ${msg}`;el.prepend(d);};
const fmt=o=>JSON.stringify(o,null,1);
let spark=[];const pushSpark=v=>{spark.push(v);if(spark.length>80)spark.shift();
 $('wSpark').innerHTML=spark.map(x=>`<i style="height:${Math.max(2,x*80)}%"></i>`).join('');};
const audioOf=(el,id)=>el.src=`/api/audio/${id}`;
async function post(path,body={}){const r=await fetch(path,{method:'POST',
 headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 const d=await r.json();if(!r.ok)throw new Error(d.error||r.status);return d;}
async function go(path,body={}){let d;
 try{d=await post(path,body);}catch(err){const tl=$('tl');const l=document.createElement('div');
  l.className='err';l.innerHTML=`<b>[系统]</b> 请求失败: ${err.message}`;tl.prepend(l);throw err;}
 if(d.started===false){const tl=$('tl');const l=document.createElement('div');
  l.className='warn';l.innerHTML='<b>[系统]</b> ⚠ 上一个同类任务还在运行，本次点击已被忽略（稍等几秒再试）';tl.prepend(l);}
 return d;}
function setBusy(name,on){const m={w:$('wStart'),v:$('vRecord'),a:$('aOnce'),a2:$('aCont'),f:$('fRun')};
 const b=m[name];if(b){b.disabled=on;}}
// wake monitor
$('wStart').onclick=async()=>{setBusy('w',true);$('wStop').disabled=false;
 log($('wLog'),'监测中…');await go('/api/wake/start');};
$('wStop').onclick=async()=>{$('wStop').disabled=true;await go('/api/wake/stop');};
const MB=$('micBtn');let micBusy=false;
function micSet(st){micBusy=st!=='idle';MB.disabled=false;
 if(st==='listen'){MB.style.background='#ef4444';MB.textContent='🔴 聆听中…请说话（停2秒自动识别，再点取消）';}
 else if(st==='work'){MB.style.background='#f59e0b';MB.textContent='⏳ 识别中…';}
 else{MB.style.background='#0ea5e9';MB.textContent='🎤 点我开始（相当于喊“Hey Jarvis”）';}}
MB.onclick=async()=>{if(!micBusy){micSet('listen');try{await go('/api/asr/dictate');}catch(e){micSet('idle');}}
 else{micSet('work');try{await go('/api/stop');}catch(e){}micSet('idle');}};
$('lSend').onclick=async()=>{const t=$('lIn').value.trim();if(!t)return;setBusy('l',true);
 $('lReq').textContent='等待响应…';await go('/api/llm',{text:t});setBusy('l',false);};
$('tSend').onclick=async()=>{const t=$('tIn').value.trim();if(!t)return;setBusy('t',true);
 await go('/api/tts',{text:t});setBusy('t',false);};
$('tOnly').onclick=async()=>{const t=$('tIn').value.trim();if(!t)return;setBusy('t',true);
 await go('/api/tts',{text:t,play:false});setBusy('t',false);};
$('fRun').onclick=async()=>{setBusy('f',true);$('fStop').disabled=false;await go('/api/full');setBusy('f',false);};
$('fStop').onclick=async()=>{$('fStop').disabled=true;await go('/api/stop');};
// sse
const es=new EventSource('/api/events');
es.onmessage=e=>{const d=JSON.parse(e.data);const t=`${d.t} [${d.card}] ${d.msg}`;
 const tl=$('tl');
 if(d.level==='level'||d.level==='score'){return;}
 while(tl.children.length>300)tl.lastChild.remove();
 const line=document.createElement('div');
 line.className=d.level==='error'?'err':d.level==='warn'?'warn':d.level==='ok'?'ok':'';
 line.innerHTML=`<b>[${d.card}]</b> ${d.msg}`;
 tl.prepend(line);
 // route per card
 const logEl={wake:$('wLog'),asr:$('aLog'),llm:$('lLog'),tts:$('tLog')}[d.card];
 if(logEl&&d.level!=='level'&&d.level!=='raw')log(logEl,d.msg);
 if(d.card==='wake'&&d.level==='score'){$('wScore').textContent=d.score.toFixed(3);
   $('wHits').textContent=`连续 ${d.hits}/${d.threshold}`;pushSpark(d.score);}
 if(d.card==='wake'&&d.level==='wake'){$('wScore').textContent='🎉 '+d.msg;log($('wLog'),d.msg,'ok');}
 if(d.card==='vad'&&d.level==='level'){$('vFill').style.width=Math.min(100,d.rms/9000*100)+'%';
   $('vRms').textContent=d.rms.toFixed(0);}
 if(d.card==='asr'&&d.level==='ok'&&d.text){$('aText').textContent=d.text;micSet('idle');}
 if(d.card==='asr'&&d.level==='warn')micSet('idle');
 if(d.card==='asr'&&d.level==='info'&&d.msg.indexOf('请说一句话')>=0)micSet('listen');
 if(d.card==='system'&&d.level==='error'&&micBusy)micSet('idle');
 if(d.audio){const el=(d.card==='asr'||d.card==='vad')?$('vAudio'):d.card==='tts'?$('tAudio'):null;if(el){el.style.display='block';audioOf(el,d.audio);}}
 if(d.card==='llm'&&d.level==='ok'&&d.text)$('lRaw').textContent=d.text;};
es.onerror=()=>{};
$('ver').textContent='v3';fetch('/api/status').then(r=>r.json()).then(s=>{$('st').innerHTML=
 `<span class=tag>${s.chat_model}</span> <span class=tag>${s.tts_model}/${s.tts_voice}</span>
 <span class=tag>wake ${s.threshold}/${s.hits}帧</span> <span class=tag>dialogue=${s.dialogue}</span>`});
</script></body></html>"""


def sse_line(q: queue.Queue):
    try:
        line = q.get(timeout=15)
        return f"data: {line}\n\n"
    except queue.Empty:
        return ": ping\n\n"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # silence request spam
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(PAGE.encode("utf-8"))
            return
        if path == "/api/status":
            self._json(200, {
                "chat_model": llm.CHAT_MODEL,
                "tts_model": tts.TTS_MODEL,
                "tts_voice": tts.TTS_VOICE,
                "threshold": DEFAULTS["threshold"],
                "hits": DEFAULTS["hits"],
                "dialogue": os_dialogue(),
            })
            return
        if path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            q = BUS.subscribe()
            try:
                while True:
                    self.wfile.write(sse_line(q).encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                BUS.unsubscribe(q)
            return
        if path.startswith("/api/audio/"):
            name = urllib.parse.unquote(path.split("/")[-1])
            p = AUDIO_DIR / name
            if p.is_file():
                self._send(200, p.read_bytes(), "audio/wav")
            else:
                self._send(404, {"error": "not found"})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        LOG.info("REQ_POST %s", path)
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {}
        try:
            if path == "/api/wake/start":
                self._json(200, {"started": launch("wake", wake_monitor)})
            elif path == "/api/wake/stop":
                stop_worker("wake")
                self._json(200, {"ok": True})
            elif path == "/api/vad/record":
                self._json(200, {"started": launch("vad", record_seconds, 8.0)})
            elif path == "/api/asr/dictate":
                self._json(200, {"started": launch("asr", asr_dictate)})
            elif path == "/api/asr/continuous":
                self._json(200, {"started": launch("asr_cont", asr_continuous)})
            elif path == "/api/llm":
                text = body.get("text", "")
                self._json(200, {"started": launch("llm", llm_test, text)})
            elif path == "/api/tts":
                text = body.get("text", "")
                play = body.get("play", True)
                self._json(200, {"started": launch("tts", tts_test, text, play)})
            elif path == "/api/full":
                self._json(200, {"started": launch("full", full_test)})
            elif path == "/api/stop":
                stop_all()
                self._json(200, {"ok": True})
            else:
                self._json(404, {"error": "unknown path"})
        except Exception as exc:
            import traceback as _tb
            LOG.error("do_POST %s failed:\n%s", path, _tb.format_exc())
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})


def stop_worker(name: str) -> None:
    with ACTIVE_LOCK:
        w = ACTIVE.get(name)
        if w:
            w.stop()


def os_dialogue() -> str:
    import os
    return os.getenv("WAKE_DIALOGUE", "0")


def svc_tailer() -> None:
    """Publish wake-service events (shared jsonl) into the web timeline."""
    import os as _os
    path = _os.getenv("HJV_EVENT_FILE", "")
    if not path:
        BUS.emit("system", "warn", "未设置 HJV_EVENT_FILE，不显示主服务事件")
        return
    offset = 0
    BUS.emit("system", "info", f"主服务事件桥已开启: {path}")
    while True:
        try:
            with open(path, "r", encoding="utf-8") as f:
                f.seek(offset)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                        BUS.emit(ev.get("card", "svc"), ev.get("level", "info"),
                                 ev.get("msg", ""), text=ev.get("text", ""))
                    except Exception:
                        pass
                offset = f.tell()
        except FileNotFoundError:
            pass
        except Exception:
            pass
        time.sleep(0.4)


def console_main(argv=None) -> int:
    import os
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.getenv("CONSOLE_PORT", "8080")))
    parser.add_argument("--host", default=os.getenv("CONSOLE_HOST", "0.0.0.0"))
    args = parser.parse_args(argv)
    BUS.emit("system", "info", f"调试台启动 {args.host}:{args.port}")
    threading.Thread(target=svc_tailer, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    LOG.info("console on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(console_main())
