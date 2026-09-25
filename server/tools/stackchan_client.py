#!/usr/bin/env python3
"""
StackChan 模拟客户端 v4 — 线程级键盘 + 阻塞式录音 + 全链路日志

彻底修复:
  1. 键盘: 独立 daemon 线程 + select.select + os.read (不依赖 loop.add_reader)
  2. Ctrl-C: 双重路径 — \\x03 字符退出 + Python SIGINT handler
  3. 音频: sd.rec(blocking=True) in executor, RMS 实时打印
  4. 日志: 每个操作都打印 DEBUG 日志
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import select
import signal
import sys
import termios
import threading
import time
import tty
import wave
from dataclasses import dataclass
from typing import Optional

import numpy as np

# =============================================================================
# 依赖
# =============================================================================
try:
    import websockets
except ImportError:
    print("pip install websockets")
    sys.exit(1)

try:
    import sounddevice as sd
    _HAS_SD = True
except ImportError:
    _HAS_SD = False
    print("[WARN] sounddevice 未安装，麦克风和播放不可用。pip install sounddevice")

# =============================================================================
# 常量
# =============================================================================
SR_IN = 16000               # 固件协议: RT_REC_SAMPLE_RATE=16000, 服务端 STT 期望 16kHz
SR_OUT = 24000
CHUNK = 2000               # samples
CHUNK_MS = 125.0           # ms (WAV 模式发送节奏)
CHUNK_BYTES = CHUNK * 2    # bytes
MIC_CHUNK_MS = 250.0       # 录音 callback 块时长 (250ms, callback 频率 4 次/秒 → 降低 overflow)
VAD_THR = 200.0
SILENCE_TIMEOUT_MS = 30000.0  # 静音累计提交阈值 (30 秒)
MAX_SILENT = 240           # 兼容保留 (不再使用块数判断)


@dataclass
class Stats:
    first_token_ms: float = 0.0
    first_audio_ms: float = 0.0
    total_ms: float = 0.0
    commit_at: float = 0.0
    texts: int = 0
    audios: int = 0

    def reset(self):
        self.__init__()


# =============================================================================
# 键盘 — 独立线程 + select + os.read
# =============================================================================

class Kbd:
    """
    完全独立的 daemon 线程读取键盘。
    不依赖 asyncio loop.add_reader（在某些环境下不可靠）。
    """

    def __init__(self):
        self._q: asyncio.Queue[str] = asyncio.Queue()
        self._fd = sys.stdin.fileno()
        self._old = None
        self._running = False
        self._thread: Optional[threading.Thread] = None

    @property
    def input_queue(self) -> asyncio.Queue[str]:
        return self._q

    def start(self) -> None:
        if self._running:
            return

        if not sys.stdin.isatty():
            print("[KBD] stdin 不是 TTY，键盘输入不可用")
            return

        self._old = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name="kbd")
        self._thread.start()
        print("[KBD] 键盘线程已启动 (cbreak + select)")

    def stop(self) -> None:
        self._running = False
        if self._old:
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)
            except Exception:
                pass
            self._old = None
        print("[KBD] 键盘线程已停止，终端已恢复")

    def _run(self) -> None:
        """线程主循环: select → os.read → queue"""
        while self._running:
            try:
                r, _, _ = select.select([self._fd], [], [], 0.1)
                if r:
                    data = os.read(self._fd, 16)
                    for b in data:
                        ch = chr(b) if 32 <= b < 127 else (
                            "\x1b" if b == 27 else
                            "\x03" if b == 3 else
                            "\x04" if b == 4 else
                            chr(b).lower() if b >= 128 else ""
                        )
                        if ch:
                            try:
                                self._q.put_nowait(ch)
                            except asyncio.QueueFull:
                                pass
            except Exception:
                pass


# =============================================================================
# 客户端
# =============================================================================

class Client:

    def __init__(self, host="127.0.0.1", port=8080, dev="sim-001",
                 wav=None, out_wav=None, mic_device=None):
        self.host = host
        self.port = port
        self.dev = dev
        self.wav_file = wav
        self.out_wav = out_wav
        self.mic_device = mic_device  # None=默认, int=索引, str=名称子串

        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.kbd = Kbd()
        self.stats = Stats()
        self.obuf = bytearray()
        self.running = True
        # 录音流 (callback 模式, 无 executor 线程)
        self._mic_stream = None
        self._mic_queue: Optional[asyncio.Queue] = None
        self._out_stream = None           # 播放流 (显式管理, 不用 sd.play/sd.stop)
        # 发送解耦: 主循环入队, 后台 sender 顺序发送 (WebSocket 背压不阻塞消费)
        self._send_queue: Optional[asyncio.Queue] = None
        self._sender_task: Optional[asyncio.Task] = None
        # task 引用
        self._recv_task = None
        self._mic_task = None
        self._wav_task = None

    # ==================================================================
    # 入口
    # ==================================================================

    async def run(self):
        signal.signal(signal.SIGINT, self._on_sig)
        signal.signal(signal.SIGTERM, self._on_sig)

        url = f"ws://{self.host}:{self.port}/v1/realtime?model=gpt-realtime"
        log(f"连接: {url}")
        self.kbd.start()

        try:
            async with websockets.connect(
                url, ping_interval=30, ping_timeout=10,
                close_timeout=5, max_size=2 ** 23,
            ) as ws:
                self.ws = ws
                await self._send_json({
                    "type": "session.update",
                    "session": {
                        "type": "realtime", "model": "gpt-realtime",
                        "output_modalities": ["audio"],
                        "audio": {
                            "input": {"format": {"type": "audio/pcm", "rate": SR_IN},
                                      "turn_detection": {"type": "semantic_vad"}},
                            "output": {"format": {"type": "audio/pcm", "rate": SR_OUT},
                                       "voice": "alloy"},
                        },
                        "instructions": "You are Stack-chan.",
                        "tools": [], "tool_choice": "auto", "temperature": 0.8,
                    },
                })
                log("[SEND] session.update")

                if self.wav_file:
                    self._recv_task = asyncio.create_task(self._recv(), name="recv")
                    self._wav_task = asyncio.create_task(self._wav_mode(), name="wav")
                    await asyncio.wait([self._wav_task], return_when=asyncio.FIRST_COMPLETED)
                else:
                    self._recv_task = asyncio.create_task(self._recv(), name="recv")
                    self._mic_task = asyncio.create_task(self._mic_mode(), name="mic")
                    await asyncio.wait([self._mic_task], return_when=asyncio.FIRST_COMPLETED)

                await self._wait(20)
                # 收尾 task: 消费取消避免 "exception never retrieved" 警告
                for t in (self._recv_task, self._mic_task, self._wav_task):
                    if t and not t.done():
                        t.cancel()
                for t in (self._recv_task, self._mic_task, self._wav_task):
                    if t:
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass

        except websockets.ConnectionClosed as e:
            log(f"WS 关闭: {e}")
        except Exception as e:
            log(f"异常: {type(e).__name__}: {e}")
        finally:
            self.running = False
            self._close_mic_stream()
            self._stop_playback()
            self.kbd.stop()
            if self.out_wav and self.obuf:
                _wav_save(self.out_wav, bytes(self.obuf), SR_OUT)
                log(f"保存: {self.out_wav}")
            self._stat()

    def _on_sig(self, *args):
        log("收到信号，退出")
        self.running = False
        # 线程安全地触发 shutdown（强制关闭 WS 让 _recv 和 _wait 立即退出）
        try:
            loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(self._force_shutdown)
        except Exception as e:
            log(f"Shutown ws exception: {e}")

    def _force_shutdown(self):
        """在 event loop 线程中执行：关闭 WS + 取消所有 task"""
        self.running = False
        for attr in ("_recv_task", "_mic_task", "_wav_task"):
            t = getattr(self, attr, None)
            if t and not t.done():
                t.cancel()
        if self.ws:
            asyncio.create_task(self._close_ws())

    async def _close_ws(self):
        try:
            await self.ws.close()
        except Exception as e:
            log(f"Shutown ws exception: {e}")

    # ==================================================================
    # 设备选择
    # ==================================================================

    def _resolve_mic(self):
        """
        解析 mic_device 参数，返回 (device_id, native_sample_rate)。

        None → 系统默认 (无效时自动选第一个有效输入设备)
        int  → 直接使用 (越界或非输入设备时回退)
        str  → 按名称子串匹配（大小写不敏感）

        Returns:
            (device_id: Optional[int], native_rate: int)
            native_rate 取自设备 default_samplerate (自适应)，
            录音用 native_rate，发送前重采样到 SR_IN (16kHz, 协议要求)。
        """
        devs = sd.query_devices()

        # PulseAudio/PipeWire 别名设备 — PortAudio 打开后数据不流动 (假活), 优先排除
        _ALIAS_NAMES = ("default", "pulse", "pipewire", "combined", "simultaneous", "null")

        def _is_real_input(i: int) -> bool:
            d = devs[i]
            if d['max_input_channels'] <= 0:
                return False
            name = d['name'].strip().lower()
            return not any(a in name for a in _ALIAS_NAMES)

        # 所有有效输入设备索引 (真实物理设备优先)
        input_idxs = [i for i, d in enumerate(devs) if d['max_input_channels'] > 0]
        real_idxs = [i for i in input_idxs if _is_real_input(i)]

        def _get_rate(di: Optional[int]) -> int:
            """设备原生采样率。无效值 (<=0) 回退 SR_IN"""
            if di is None or di < 0 or di >= len(devs):
                return SR_IN
            r = int(devs[di].get('default_samplerate', 0) or 0)
            return r if r > 0 else SR_IN

        def _is_input(di: Optional[int]) -> bool:
            return di is not None and 0 <= di < len(devs) and devs[di]['max_input_channels'] > 0

        # 优先真实物理设备, 没有才退回别名设备
        preferred = real_idxs if real_idxs else input_idxs

        # --- 无输入设备 ---
        if not input_idxs:
            log("错误: 系统中没有任何输入设备 (max_input_channels=0)")
            log("提示: 检查 PulseAudio/PipeWire 配置，或运行 --list-devices 查看")
            return None, SR_IN

        # --- 默认设备 ---
        if self.mic_device is None:
            di = sd.default.device[0]
            # 默认设备是别名 (假活风险) → 改用真实设备
            if _is_input(di) and di in real_idxs:
                rate = _get_rate(di)
                log(f"麦克风: [默认] #{di} {devs[di]['name']} @ {rate}Hz")
                return di, rate
            di = preferred[0]
            rate = _get_rate(di)
            log(f"警告: 默认设备 #{sd.default.device[0]} 是别名或不可用，改用 #{di} {devs[di]['name']} @ {rate}Hz")
            return di, rate

        # --- 指定索引 ---
        if isinstance(self.mic_device, int):
            di = self.mic_device
            if _is_input(di):
                if di not in real_idxs:
                    log(f"警告: #{di} {devs[di]['name']} 是 PulseAudio 别名设备，录音可能假活")
                rate = _get_rate(di)
                log(f"麦克风: [指定索引] #{di} {devs[di]['name']} @ {rate}Hz")
                return di, rate
            log(f"错误: 设备 #{di} 不存在或不是输入设备，自动回退")
            di = preferred[0]
            rate = _get_rate(di)
            log(f"麦克风: [回退] #{di} {devs[di]['name']} @ {rate}Hz")
            return di, rate

        # --- 名称子串匹配 ---
        keyword = self.mic_device.lower()
        for i in preferred:
            if keyword in devs[i]['name'].lower():
                rate = _get_rate(i)
                log(f"麦克风: [匹配] #{i} {devs[i]['name']} @ {rate}Hz")
                return i, rate

        log(f"警告: 未找到匹配 '{self.mic_device}' 的输入设备，自动选择第一个")
        di = preferred[0]
        rate = _get_rate(di)
        log(f"麦克风: [回退] #{di} {devs[di]['name']} @ {rate}Hz")
        return di, rate

    def _resolve_mic_candidates(self) -> list[tuple[int, int]]:
        """
        返回候选设备列表 [(device_id, native_rate), ...]，按优先级排序。

        排序: 用户指定 > 默认设备(真实) > 其余真实设备 > 别名设备
        供 _mic_mode 依次尝试，全静音/假活设备自动跳过。
        """
        devs = sd.query_devices()
        _ALIAS_NAMES = ("default", "pulse", "pipewire", "combined", "simultaneous", "null")

        input_idxs = [i for i, d in enumerate(devs) if d['max_input_channels'] > 0]
        real_idxs = [i for i in input_idxs
                     if not any(a in devs[i]['name'].strip().lower() for a in _ALIAS_NAMES)]
        preferred = real_idxs if real_idxs else input_idxs

        def _get_rate(di: int) -> int:
            r = int(devs[di].get('default_samplerate', 0) or 0)
            return r if r > 0 else SR_IN

        def _is_input(di) -> bool:
            return isinstance(di, int) and 0 <= di < len(devs) and devs[di]['max_input_channels'] > 0

        candidates: list[tuple[int, int]] = []

        # 1. 用户显式指定
        if isinstance(self.mic_device, int) and _is_input(self.mic_device):
            candidates.append((self.mic_device, _get_rate(self.mic_device)))
        elif isinstance(self.mic_device, str):
            kw = self.mic_device.lower()
            for i in preferred:
                if kw in devs[i]['name'].lower():
                    candidates.append((i, _get_rate(i)))
                    break

        # 2. 系统默认 (真实设备)
        di = sd.default.device[0]
        if _is_input(di) and di in real_idxs:
            candidates.append((di, _get_rate(di)))

        # 3. 其余真实设备
        for i in preferred:
            if all(i != c[0] for c in candidates):
                candidates.append((i, _get_rate(i)))

        # 4. 别名设备兜底
        for i in input_idxs:
            if all(i != c[0] for c in candidates):
                candidates.append((i, _get_rate(i)))

        if not candidates:
            log("错误: 系统中没有任何输入设备")
        return candidates

    # ==================================================================
    # 麦克风模式
    # ==================================================================

    async def _mic_mode(self):
        log("[MODE] 麦克风模式")
        if not _HAS_SD:
            log("sounddevice 未安装，仅键盘可用")
            while self.running:
                ch = await self._kbd_get()
                if ch in ("q", "\x1b", "\x03"):
                    self.running = False
                await asyncio.sleep(0.1)
            return

        # 候选设备列表 (用户指定 > 默认 > 真实设备 > 别名)
        candidates = self._resolve_mic_candidates()
        if not candidates:
            log("无可用麦克风设备，仅键盘可用")
            while self.running:
                ch = await self._kbd_get()
                if ch in ("q", "\x1b", "\x03"):
                    self.running = False
                await asyncio.sleep(0.1)
            return

        # 依次尝试候选设备: 假活/全静音自动跳过
        mic_id = None
        native_rate = SR_IN
        vad_thr = VAD_THR  # 默认值, 探测成功后覆盖为自适应阈值
        for cand_id, cand_rate in candidates:
            cand_chunk = int(cand_rate * MIC_CHUNK_MS / 1000)
            log(f"尝试设备 #{cand_id} @ {cand_rate}Hz...")
            r, i = self._open_mic_stream(cand_id, cand_rate, cand_chunk)
            if self._mic_stream is None:
                continue

            # 监控 2.5 秒: 检测真实信号 (RMS 峰值)
            peak_rms = await self._probe_signal(timeout=2.5)
            if peak_rms > 30.0:
                mic_id, native_rate = i, r
                # 自适应 VAD 阈值: 峰值的 40% (下限 40, 上限 500)
                # 硬编码 200 在低增益麦克风上会漏触发 (本次 RMS=160 < 200 没发送)
                vad_thr = min(max(peak_rms * 0.4, 40.0), 500.0)
                log(f"✅ 设备 #{cand_id} 有真实信号 (峰值 RMS={peak_rms:.0f}, VAD 阈值={vad_thr:.0f})")
                break
            log(f"⚠️  设备 #{cand_id} 全静音 (峰值 RMS={peak_rms:.0f})，切换下一候选")
            self._close_mic_stream()

        if mic_id is None:
            log("所有候选设备均为静音 — 请检查系统麦克风 (amixer / pactl)")
            log("提示: amixer -c 0 sset Capture 80% 或检查 PulseAudio 输入源")
            while self.running:
                ch = await self._kbd_get()
                if ch in ("q", "\x1b", "\x03"):
                    self.running = False
                await asyncio.sleep(0.1)
            return

        # 计算按原生采样率的 chunk 大小 (250ms, 降低 callback 频率防 overflow)
        native_chunk = int(native_rate * MIC_CHUNK_MS / 1000)
        log(f"录音参数: device={mic_id} rate={native_rate}Hz chunk={native_chunk} samples ({MIC_CHUNK_MS:.0f}ms) → 重采样到 {SR_IN}Hz 发送")

        rec = False
        silent_ms = 0.0
        chunks_sent = 0
        stream_started = time.time()
        got_data = False
        warned_fake = False

        # 发送解耦: 后台 sender task, 主循环只入队不阻塞
        self._send_queue = asyncio.Queue(maxsize=64)
        self._sender_task = asyncio.create_task(self._sender_loop(), name="sender")

        # overflow 报告状态
        last_report = time.time()
        prev_overflow = 0
        prev_dropped = 0
        last_rms_hint = 0.0

        def _queue_put(kind: str, payload) -> bool:
            """入队 (非阻塞), 满则计数丢弃"""
            try:
                self._send_queue.put_nowait((kind, payload))
                return True
            except asyncio.QueueFull:
                return False

        log("等待语音... (Ctrl-C/q 退出, i 打断)")
        try:
            while self.running:
                ch = await self._kbd_get()
                if ch:
                    log(f"[KEY] {repr(ch)}")
                if ch in ("q", "\x1b", "\x03"):
                    # 退出前提交未发送的音频 (commit 入队, 保序)
                    if rec:
                        log(f"[VAD] 退出前提交 ({chunks_sent} 块)")
                        _queue_put("commit", None)
                    self.running = False
                    break
                if ch in ("i",):
                    # 打断: 清空待发送音频, cancel 立即直发 (优先级最高)
                    while not self._send_queue.empty():
                        try:
                            self._send_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    await self._interrupt()
                    rec = False; silent_ms = 0.0

                # 从 callback 队列取音频 (无阻塞录音)
                try:
                    data = await asyncio.wait_for(self._mic_queue.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    # watchdog: 启动 3 秒仍无数据 → 设备假活
                    if not got_data and not warned_fake and time.time() - stream_started > 3.0:
                        warned_fake = True
                        log("⚠️  录音流 3 秒无数据 — 设备可能假活 (PulseAudio 别名) 或系统静音")
                        log("   建议: python tools/stackchan_client.py --list-devices")
                        log("   选择名称不是 'default' 的真实物理设备 (如 'Built-in Audio')")
                    # 低频报告 overflow
                    if time.time() - last_report > 5.0:
                        await self._report_overflow(prev_overflow, prev_dropped)
                        prev_overflow = self._mic_status_counts.get("input_overflow", 0)
                        prev_dropped = self._mic_dropped
                        last_report = time.time()
                    continue

                if not got_data:
                    got_data = True
                    log(f"✅ 首个音频块到达 (启动后 {time.time() - stream_started:.2f}s)，录音正常")

                # 重采样到协议采样率 16kHz
                if native_rate != SR_IN:
                    data = _resample(data, native_rate, SR_IN)

                r = _rms(data)
                chunks_sent += 1

                if r > vad_thr:
                    if not rec:
                        rec = True; self.stats.reset()
                        log(f"[VAD] 开始 RMS={r:.0f} (阈值 {vad_thr:.0f})")
                    silent_ms = 0.0
                    _queue_put("audio", data)
                    if chunks_sent % 10 == 0:
                        log(f"[MIC] 已发送 {chunks_sent} 块 RMS={r:.0f}")
                elif rec:
                    # 静音按时间累计 (块长 250ms, 与块数解耦)
                    silent_ms += MIC_CHUNK_MS
                    _queue_put("audio", data)
                    if silent_ms >= SILENCE_TIMEOUT_MS:
                        log(f"[VAD] 静音 {silent_ms/1000:.0f}s → 提交 ({chunks_sent} 块)")
                        _queue_put("commit", None)
                        rec = False; silent_ms = 0.0; chunks_sent = 0
                else:
                    # 未触发录音: 低频提示信号强度 (诊断麦克风是否太弱)
                    if time.time() - last_rms_hint > 3.0:
                        last_rms_hint = time.time()
                        log(f"[MIC] 信号 RMS={r:.0f} (触发阈值 {vad_thr:.0f}) — "
                            f"{'请靠近麦克风或说话' if r < vad_thr else ''}")

                # 低频报告 overflow
                if time.time() - last_report > 5.0:
                    await self._report_overflow(prev_overflow, prev_dropped)
                    prev_overflow = self._mic_status_counts.get("input_overflow", 0)
                    prev_dropped = self._mic_dropped
                    last_report = time.time()
        finally:
            # 等待发送队列排空 (最多 3 秒), 保证 commit 到达服务器
            deadline = time.time() + 3.0
            while time.time() < deadline and not self._send_queue.empty():
                await asyncio.sleep(0.05)
            if self._sender_task:
                self._sender_task.cancel()
                try:
                    await self._sender_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._sender_task = None
            self._close_mic_stream()

    # ==================================================================
    # 发送队列 (解耦 WebSocket 背压)
    # ==================================================================

    async def _sender_loop(self) -> None:
        """
        后台发送任务 — 从 send_queue 顺序取出并发送。

        主循环只入队 (put_nowait, 无网络 I/O)，
        WebSocket 慢时背压只阻塞本 task，不影响录音消费。
        """
        while True:
            try:
                kind, payload = await self._send_queue.get()
            except asyncio.CancelledError:
                return

            if kind == "audio":
                try:
                    await self._send_audio(payload)
                except Exception as e:
                    log(f"[SEND] 音频发送失败: {e}")
            elif kind == "commit":
                self.stats.commit_at = time.time()
                try:
                    await self._send_json({"type": "input_audio_buffer.commit"})
                    log("[SEND] audio.commit")
                except Exception as e:
                    log(f"[SEND] commit 失败: {e}")

    async def _report_overflow(self, prev_overflow: int, prev_dropped: int) -> None:
        """低频报告录音流溢出状态"""
        ovf = self._mic_status_counts.get("input_overflow", 0)
        drp = self._mic_dropped
        parts = []
        if ovf > prev_overflow:
            parts.append(f"input_overflow +{ovf - prev_overflow} (总 {ovf})")
        if drp > prev_dropped:
            parts.append(f"队列满丢弃 +{drp - prev_dropped} (总 {drp})")
        if parts:
            log(f"[MIC] ⚠️  {', '.join(parts)} — 音频有丢失")

    async def _probe_signal(self, timeout: float = 2.5) -> float:
        """
        探测录音流是否有真实信号。

        消费 mic_queue 中的音频块，计算 RMS 峰值。
        返回峰值 RMS (全静音设备返回 ~0)。
        """
        peak = 0.0
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data = await asyncio.wait_for(self._mic_queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue
            r = _rms(data)
            if r > peak:
                peak = r
        return peak

    # ==================================================================
    # 录音流管理 (callback 模式, 无 executor 阻塞)
    # ==================================================================

    def _open_mic_stream(self, mic_id: int, native_rate: int, block_samples: int):
        """
        打开 callback 录音流。失败自动降级采样率重试。

        callback 在 PortAudio 实时线程运行:
          - 禁止任何 I/O (print/log 会导致 input overflow!)
          - 仅计数 + put_nowait 到 asyncio.Queue (线程安全)
        无 executor 线程 → asyncio.run 退出时不会因阻塞线程卡死。

        Returns:
            (native_rate, mic_id) 实际使用的参数
        """
        self._mic_queue = asyncio.Queue(maxsize=1024)
        self._mic_status_counts: dict = {}   # status 计数 (主循环低频报告)
        self._mic_dropped: int = 0           # 队列满丢弃块数

        # CallbackFlags 属性 → 统一计数键
        _FLAG_NAMES = ("input_overflow", "input_underflow",
                       "output_underflow", "output_overflow", "priming_output")

        def _cb(indata, frames, time_info, status):
            # 实时线程: 禁止 I/O，只做原子操作
            if status:
                for name in _FLAG_NAMES:
                    if getattr(status, name, False):
                        try:
                            self._mic_status_counts[name] = self._mic_status_counts.get(name, 0) + 1
                        except Exception:
                            pass
            try:
                # bytes(indata) 直接构造 (C 级转换, 比 copy().tobytes() 快)
                self._mic_queue.put_nowait(bytes(indata))
            except asyncio.QueueFull:
                try:
                    self._mic_dropped += 1
                except Exception:
                    pass

        # 尝试列表: [设备原生率, 44100, 16000] 去重
        rates = []
        for r in (native_rate, 44100, SR_IN):
            if r not in rates:
                rates.append(r)

        for rate in rates:
            block = int(rate * MIC_CHUNK_MS / 1000)
            try:
                stream = sd.InputStream(
                    samplerate=rate, channels=1, dtype="int16",
                    blocksize=block, device=mic_id, callback=_cb,
                    latency="high",   # 更大的 PortAudio 内部缓冲 → 降低 input overflow
                )
                stream.start()
                self._mic_stream = stream
                log(f"录音流已启动: device={mic_id} @ {rate}Hz (block={block}={MIC_CHUNK_MS:.0f}ms, latency=high)")
                return rate, mic_id
            except Exception as e:
                log(f"[MIC] 打开 device={mic_id} @ {rate}Hz 失败: {e}")

        self._mic_stream = None
        return native_rate, mic_id

    def _close_mic_stream(self) -> None:
        """关闭录音流 (幂等, abort 立即终止不阻塞)"""
        if self._mic_stream is not None:
            try:
                self._mic_stream.abort()  # 立即终止, 不等待缓冲排空
            except Exception:
                try:
                    self._mic_stream.stop()
                except Exception:
                    pass
            try:
                self._mic_stream.close()
            except Exception:
                pass
            self._mic_stream = None
            log("[MIC] 录音流已关闭")

    def _play_pcm(self, pcm: bytes) -> None:
        """播放 PCM (显式 OutputStream, 与录音流互不干扰)"""
        if not _HAS_SD or len(pcm) < 4:
            return
        try:
            if self._out_stream is None:
                self._out_stream = sd.OutputStream(
                    samplerate=SR_OUT, channels=1, dtype="int16",
                )
                self._out_stream.start()
            self._out_stream.write(np.frombuffer(pcm, dtype=np.int16))
        except Exception as e:
            log(f"[AUDIO] 播放错误: {e}")

    def _stop_playback(self) -> None:
        """停止播放流 (幂等)"""
        if self._out_stream is not None:
            try:
                self._out_stream.abort()
            except Exception:
                try:
                    self._out_stream.stop()
                except Exception:
                    pass
            try:
                self._out_stream.close()
            except Exception:
                pass
            self._out_stream = None

    # ==================================================================
    # WAV 模式
    # ==================================================================

    async def _wav_mode(self):
        log(f"[MODE] WAV: {self.wav_file}")
        pcm, sr = _wav_read(self.wav_file)
        ms = len(pcm) / (sr * 2) * 1000
        log(f"[WAV] {len(pcm)} B {sr}Hz {ms:.0f}ms")
        self.stats.reset()

        for i in range(0, len(pcm), CHUNK_BYTES):
            if not self.running:
                break
            await self._send_audio(pcm[i:i + CHUNK_BYTES])
            await asyncio.sleep(CHUNK_MS / 1000)

        log("[WAV] 发送完毕 → commit")
        await self._commit()

    # ==================================================================
    # 接收
    # ==================================================================

    async def _recv(self):
        log("[RECV] 开始")
        try:
            async for raw in self.ws:
                await self._on_msg(raw)
        except websockets.ConnectionClosed as e:
            log(f"[RECV] 关闭: {e}")
            raise
        except Exception as e:
            log(f"[RECV] 异常: {e}")
        log("[RECV] 结束")

    async def _on_msg(self, raw):
        if isinstance(raw, bytes):
            raw = raw.decode(errors="replace")
        try:
            m = json.loads(raw)
        except json.JSONDecodeError:
            return
        t = m.get("type", "")

        if t == "session.updated":
            log("[<-] session.updated → 就绪")

        elif t == "input_audio_buffer.speech_started":
            log("[<-] speech_started")

        elif t == "input_audio_buffer.speech_stopped":
            log("[<-] speech_stopped")

        elif t == "input_audio_buffer.committed":
            log("[<-] committed → 服务端处理中")

        elif t == "response.output_audio_transcript.delta":
            d = m.get("delta", "")
            if self.stats.texts == 0:
                self.stats.first_token_ms = (time.time() - self.stats.commit_at) * 1000
                log(f"[LAT] 首Token: {self.stats.first_token_ms:.0f}ms")
            self.stats.texts += 1
            sys.stdout.write(f"\r[ASR] {d}")
            sys.stdout.flush()

        elif t == "response.output_audio.delta":
            b64 = m.get("delta", "")
            if not b64:
                return
            try:
                pcm = base64.b64decode(b64)
            except Exception:
                return
            if self.stats.audios == 0:
                self.stats.first_audio_ms = (time.time() - self.stats.commit_at) * 1000
                log(f"\n[LAT] 首帧音频: {self.stats.first_audio_ms:.0f}ms")
            self.stats.audios += 1
            self.obuf.extend(pcm)
            if self.stats.audios % 10 == 0:
                log(f"[AUDIO] {self.stats.audios} 块 ({len(self.obuf)} B)")
            self._play_pcm(pcm)

        elif t == "response.output_audio.done":
            log("[<-] audio.done")

        elif t == "response.done":
            self.stats.total_ms = (time.time() - self.stats.commit_at) * 1000
            log(f"[<-] response.done → 总轮次: {self.stats.total_ms:.0f}ms")

        elif t == "error":
            log(f"[<-] ERROR: {m.get('error', {}).get('message', str(m))}")

    # ==================================================================
    # 发送
    # ==================================================================

    async def _send_json(self, o):
        await self.ws.send(json.dumps(o))

    async def _send_audio(self, data):
        await self._send_json({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(data).decode(),
        })

    async def _commit(self):
        self.stats.commit_at = time.time()
        await self._send_json({"type": "input_audio_buffer.commit"})
        log("[SEND] audio.commit")

    async def _interrupt(self):
        await self._send_json({"type": "response.cancel"})
        self._stop_playback()  # 只停播放流, 不影响录音流
        log("[SEND] response.cancel (打断)")

    # ==================================================================
    # 工具
    # ==================================================================

    async def _kbd_get(self) -> str:
        try:
            return await asyncio.wait_for(self.kbd.input_queue.get(), timeout=0.15)
        except asyncio.TimeoutError:
            return ""

    async def _wait(self, timeout):
        last = self.stats.audios
        idle = 0
        waited = 0.0
        while waited < timeout and self.running:
            if self.stats.audios != last:
                last = self.stats.audios; idle = 0
            else:
                idle += 1
            if idle > 20:
                break
            await asyncio.sleep(0.15)
            waited += 0.15

    def _stat(self):
        print(f"\n===== 延迟 =====")
        print(f"  首Token: {self.stats.first_token_ms:.0f}ms")
        print(f"  首帧音频: {self.stats.first_audio_ms:.0f}ms")
        print(f"  总轮次: {self.stats.total_ms:.0f}ms")
        print(f"  音频块: {self.stats.audios}")
        print(f"  输出: {len(self.obuf)} B ({len(self.obuf)/SR_OUT/2:.1f}s)")
        print(f"================")


# =============================================================================
# 音频工具
# =============================================================================

def _rms(d: bytes) -> float:
    if len(d) < 2:
        return 0.0
    s = np.frombuffer(d, dtype=np.int16).astype(np.float64)
    return float(np.sqrt(np.mean(s ** 2)))


def _wav_read(path: str) -> tuple[bytes, int]:
    with wave.open(path, "rb") as f:
        sr = f.getframerate()
        ch = f.getnchannels()
        data = f.readframes(f.getnframes())
    s = np.frombuffer(data, dtype=np.int16)
    if ch > 1:
        s = s.reshape(-1, ch).mean(axis=1).astype(np.int16)
    if sr != SR_IN:
        ratio = SR_IN / sr
        nl = int(len(s) * ratio)
        s = np.interp(np.linspace(0, len(s) - 1, nl), np.arange(len(s)), s).astype(np.int16)
    return s.tobytes(), SR_IN


def _resample(data: bytes, src_rate: int, dst_rate: int) -> bytes:
    """PCM 16-bit 线性插值重采样"""
    if src_rate == dst_rate:
        return data
    s = np.frombuffer(data, dtype=np.int16).astype(np.float64)
    ratio = dst_rate / src_rate
    nl = int(len(s) * ratio)
    s = np.interp(np.linspace(0, len(s) - 1, nl), np.arange(len(s)), s)
    return s.astype(np.int16).tobytes()


def _wav_save(path: str, data: bytes, sr: int):
    expected_bytes = len(data)
    expected_frames = expected_bytes // 2

    with wave.open(path, "wb") as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(sr)
        f.writeframes(data)
        # 强制刷新到磁盘，防止缓冲区数据未写完
        f._file.flush()
        os.fsync(f._file.fileno())

    # 验证磁盘文件完整性 (比对实际文件大小，而非仅读 header)
    actual_size = os.path.getsize(path)
    header_size = 44
    expected_size = header_size + expected_bytes
    if actual_size != expected_size:
        print(f"⚠️  WAV 大小不匹配: 期望 {expected_size}B, 磁盘 {actual_size}B (差值 {expected_size - actual_size}B)")
        # 文件截断 — 重写为实际数据长度
        print(f"    修复: 按实际数据 {actual_size - header_size}B 重写")
        actual_data = data[:(actual_size - header_size)] if actual_size > header_size else b""
        with wave.open(path, "wb") as f:
            f.setnchannels(1); f.setsampwidth(2); f.setframerate(sr)
            f.writeframes(actual_data)
            f._file.flush()
            os.fsync(f._file.fileno())
    else:
        # 双重验证: readframes 确认可读出全部数据
        with wave.open(path, "rb") as f:
            readback = f.readframes(expected_frames)
        if len(readback) != expected_bytes:
            print(f"⚠️  WAV readframes 不完整: 期望 {expected_bytes}B, 读到 {len(readback)}B")
        else:
            print(f"    WAV 验证通过: 1ch 16bit {sr}Hz {expected_frames} frames ({expected_frames/sr:.2f}s) [{actual_size}B on disk]")


# =============================================================================
# 日志
# =============================================================================

def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# =============================================================================
# main
# =============================================================================

def _cmd_list_devices():
    """列出所有输入设备"""
    if not _HAS_SD:
        print("sounddevice 未安装")
        return
    devs = sd.query_devices()
    _ALIAS = ("default", "pulse", "pipewire", "combined", "simultaneous", "null")
    print(f"\n{'#':<4} {'输入':<6} {'名称':<40} {'采样率':<10}  备注")
    print("-" * 74)
    for i, d in enumerate(devs):
        if d['max_input_channels'] > 0:
            marks = []
            if i == sd.default.device[0]:
                marks.append("默认")
            name_l = d['name'].strip().lower()
            if any(a in name_l for a in _ALIAS):
                marks.append("⚠️别名(可能假活)")
            mark = " ← " + ", ".join(marks) if marks else ""
            print(f"{i:<4} {d['max_input_channels']:<6} {d['name']:<40} {int(d['default_samplerate']):<10} {mark}")


def _cmd_test_mic(mic_device=None, duration: float = 3.0, save_path: Optional[str] = None):
    """
    测试麦克风: 录制 N 秒 → 播放 → 保存 → 报告统计。

    Args:
        mic_device: None=默认, int=设备索引, str=名称子串
        duration: 录制时长 (秒)
        save_path: 保存路径 (None=不保存)
    """
    if not _HAS_SD:
        print("❌ sounddevice 未安装。pip install sounddevice")
        return

    # 解析设备 — 与 Client._resolve_mic 一致: 过滤有效输入设备
    devs = sd.query_devices()
    input_idxs = [i for i, d in enumerate(devs) if d['max_input_channels'] > 0]

    if not input_idxs:
        print("❌ 系统中没有任何输入设备")
        return

    def _get_device_rate(di: int) -> int:
        r = int(devs[di].get('default_samplerate', 0) or 0)
        return r if r > 0 else 16000

    def _valid(di: Optional[int]) -> bool:
        return di is not None and 0 <= di < len(devs) and devs[di]['max_input_channels'] > 0

    dev_id = None
    native_rate = 16000
    if isinstance(mic_device, int):
        if _valid(mic_device):
            dev_id = mic_device
            native_rate = _get_device_rate(dev_id)
        else:
            print(f"⚠️  设备 #{mic_device} 不存在或不是输入设备，使用第一个有效输入设备")
    elif isinstance(mic_device, str):
        keyword = mic_device.lower()
        for i in input_idxs:
            if keyword in devs[i]['name'].lower():
                dev_id = i
                native_rate = _get_device_rate(i)
                break
        if dev_id is None:
            print(f"⚠️  未找到匹配 '{mic_device}' 的输入设备")
    else:
        di = sd.default.device[0]
        if _valid(di):
            dev_id = di
            native_rate = _get_device_rate(dev_id)
        else:
            print(f"⚠️  默认设备 #{di} 不可用，使用第一个有效输入设备")

    # 兜底: 第一个有效输入设备
    if dev_id is None:
        dev_id = input_idxs[0]
        native_rate = _get_device_rate(dev_id)
    dev_info = devs[dev_id]
    print(f"\n{'='*50}")
    print(f"  麦克风测试")
    print(f"{'='*50}")
    print(f"  设备: #{dev_id} {dev_info['name']}")
    print(f"  设备原生采样率: {native_rate} Hz")
    print(f"  录制时长: {duration} 秒")
    print(f"  格式: mono, int16")
    print(f"{'='*50}")

    # 录制 — 使用设备原生采样率 (全程一维 int16)
    total_samples = int(native_rate * duration)
    print(f"\n🔴 录制中 ({duration}s @ {native_rate}Hz)...  对着麦克风说话或制造声音")
    print(f"   进度: ", end="", flush=True)

    audio = np.zeros(total_samples, dtype=np.int16)
    chunk = native_rate // 10  # 每 100ms 一块
    n_chunks = total_samples // chunk

    for i in range(n_chunks):
        start = i * chunk
        end = start + chunk
        try:
            block = sd.rec(chunk, samplerate=native_rate, channels=1,
                           dtype="int16", device=dev_id, blocking=True)
            audio[start:end] = block.flatten()
        except Exception as e:
            print(f"\n❌ 录制错误 (chunk {i}): {e}")
            return
        # 实时 RMS
        r = float(np.sqrt(np.mean(block.astype(np.float64) ** 2)))
        bar = "█" * min(int(r / 100), 20)
        print(f"\r   进度: {bar} RMS={r:.0f}  ", end="", flush=True)

    print(f"\n✅ 录制完成: {len(audio)} samples ({len(audio)/native_rate:.2f}s)")

    # 确保连续内存
    audio = np.ascontiguousarray(audio)

    # 统计
    flat = audio.astype(np.float64)
    rms_total = float(np.sqrt(np.mean(flat ** 2)))
    peak = int(np.max(np.abs(flat)))
    peak_ratio = peak / 32767 * 100
    clipped = int(np.sum(np.abs(flat) >= 32700))

    print(f"\n{'='*50}")
    print(f"  录音统计")
    print(f"{'='*50}")
    print(f"  RMS (整体):     {rms_total:.0f}")
    print(f"  峰值:           {peak} / 32767 ({peak_ratio:.1f}%)")
    print(f"  削波样本数:     {clipped} {'⚠️  信号过强!' if clipped > 10 else ''}")
    print(f"  静音占比:       {int(np.sum(np.abs(flat) < 200) / len(flat) * 100)}%")
    wav_bytes = len(audio.tobytes())
    wav_dur = wav_bytes / (native_rate * 2)
    print(f"  WAV预估:        {wav_bytes} bytes ({wav_dur:.2f}s)")
    status = "✅ 正常" if rms_total > 50 and peak_ratio > 1.0 and clipped < 10 else "⚠️  请检查"
    print(f"  诊断:           {status}")
    print(f"{'='*50}")

    # 保存 WAV (一维 int16 → tobytes 保证连续)
    raw = audio.tobytes()
    if save_path:
        _wav_save(save_path, raw, native_rate)
        print(f"\n💾 已保存: {save_path}")
    else:
        default_path = f"mic_test_{time.strftime('%Y%m%d_%H%M%S')}.wav"
        _wav_save(default_path, raw, native_rate)
        print(f"\n💾 自动保存: {default_path}")

    # 播放 — 先强制释放录音流，消除 PortAudio 状态冲突
    sd.stop()  # 确保所有录音流已关闭
    try:
        print("\n🔊 播放录音...")
        sd.play(audio, samplerate=native_rate, blocking=True)
        print("✅ 播放完成")
    except Exception as e:
        print(f"⚠️  播放失败: {e}")
        print(f"   提示: 可手动播放保存的 WAV 文件验证录音")


def main():
    ap = argparse.ArgumentParser(description="StackChan Client v4")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--dev", default="sim-001")
    ap.add_argument("--wav", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--list-devices", action="store_true", help="列出所有输入设备并退出")
    ap.add_argument("--mic", default=None, help="麦克风设备 (索引号 或 名称子串, 如 'Yeti' 或 '0')")
    ap.add_argument("--test-mic", type=float, const=3.0, nargs="?", default=None,
                    help="测试麦克风 (可选时长秒数, 默认 3s)")
    a = ap.parse_args()

    if a.list_devices:
        _cmd_list_devices()
        return

    if a.test_mic is not None:
        mic = a.mic
        if mic and mic.isdigit():
            mic = int(mic)
        _cmd_test_mic(mic_device=mic, duration=a.test_mic)
        return

    # mic 参数解析: 纯数字 → int, 否则 str
    mic = a.mic
    if mic and mic.isdigit():
        mic = int(mic)

    c = Client(host=a.host, port=a.port, dev=a.dev,
               wav=a.wav, out_wav=a.out, mic_device=mic)
    try:
        asyncio.run(c.run())
    except KeyboardInterrupt:
        print("\n退出")


if __name__ == "__main__":
    main()
