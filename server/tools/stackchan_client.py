#!/usr/bin/env python3
"""
StackChan 模拟客户端 — 用于无硬件调试 + 自动化回归测试

用法:
    # 实时麦克风交互模式
    python tools/stackchan_client.py --host 127.0.0.1 --port 8080

    # WAV 文件输入模式 (自动化测试)
    python tools/stackchan_client.py --host 127.0.0.1 --port 8080 --wav test_input.wav

    # 保存接收的音频到文件
    python tools/stackchan_client.py --host 127.0.0.1 --port 8080 --save-output out.wav

键盘控制 (交互模式):
    i     → 打断当前回复
    q/ESC → 退出

依赖:
    pip install websockets sounddevice numpy

协议对齐说明:
    本客户端完全模拟 AI_StackChan_Ex 固件端的 Realtime API 行为:
      - 录音: PCM 16-bit, 16kHz, mono, 2000 samples/chunk (~125ms)
      - 上传: JSON 文本帧 {"type":"input_audio_buffer.append","audio":"<base64>"}
      - 接收: JSON 文本帧 {"type":"response.output_audio.delta","delta":"<base64>"}
      - 接收: 24kHz PCM 输出 (服务端合成)
      - 信令: session.update → session.updated → audio.commit → audio.delta → response.done
      - 打断: response.cancel 文本帧
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import signal
import sys
import time
import wave
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

# =============================================================================
# 依赖检查 (可选依赖，缺失时优雅降级)
# =============================================================================

try:
    import websockets
except ImportError:
    print("请安装 websockets: pip install websockets")
    sys.exit(1)

try:
    import sounddevice as sd
    _HAS_AUDIO = True
except ImportError:
    print("警告: sounddevice 未安装，音频采集/播放功能不可用")
    print("安装: pip install sounddevice numpy")
    _HAS_AUDIO = False


# =============================================================================
# 协议常量 — 严格对齐固件端
# =============================================================================

# 录音参数 (对齐 RealtimeLLMBase.h)
SAMPLE_RATE_INPUT: int = 16000          # 固件 RT_REC_SAMPLE_RATE
CHUNK_SAMPLES: int = 2000               # 固件 RT_REC_LENGTH
CHUNK_DURATION_MS: float = 125.0        # 2000/16000*1000

# 播放参数 (对齐 streamAudioDelta)
SAMPLE_RATE_OUTPUT: int = 24000         # 固件 playRaw 24kHz

# 录音超时 (对齐 REALTIME_RECORD_TIMEOUT)
RECORD_TIMEOUT_S: float = 30.0

# VAD 静音阈值 (RMS)
VAD_THRESHOLD: float = 200.0


# =============================================================================
# 延迟统计
# =============================================================================

@dataclass
class LatencyStats:
    """延迟统计"""
    first_token_ms: float = 0.0          # 首 token 延迟 (commit → 第一个 text delta)
    first_audio_ms: float = 0.0          # 首帧音频延迟 (commit → 第一个 audio delta)
    total_turn_ms: float = 0.0           # 总轮次延迟 (commit → response.done)
    commit_time: float = 0.0             # 记录 commit 发送时间
    text_count: int = 0
    audio_count: int = 0

    def reset(self) -> None:
        self.__init__()


# =============================================================================
# 音频工具
# =============================================================================

def pcm_to_base64(pcm: bytes) -> str:
    return base64.b64encode(pcm).decode("ascii")


def base64_to_pcm(b64: str) -> bytes:
    return base64.b64decode(b64)


def rms(data: bytes) -> float:
    """计算 PCM 16-bit 数据的 RMS 能量"""
    if len(data) < 2:
        return 0.0
    samples = np.frombuffer(data, dtype=np.int16).astype(np.float64)
    return float(np.sqrt(np.mean(samples ** 2)))


def read_wav(path: str) -> tuple[bytes, int]:
    """
    读取 WAV 文件，返回 (PCM数据, 采样率)。

    对齐固件: 如果不是 16kHz mono 16-bit，自动转换。
    """
    with wave.open(path, "rb") as wf:
        sr = wf.getframerate()
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
        data = wf.readframes(wf.getnframes())

    # 转换为 16kHz mono 16-bit
    if sr != SAMPLE_RATE_INPUT or ch != 1 or sw != 2:
        samples = np.frombuffer(data, dtype=np.int16).reshape(-1, ch)
        if ch > 1:
            samples = samples.mean(axis=1).astype(np.int16)
        if sr != SAMPLE_RATE_INPUT:
            ratio = SAMPLE_RATE_INPUT / sr
            new_len = int(len(samples) * ratio)
            samples = np.interp(
                np.linspace(0, len(samples) - 1, new_len),
                np.arange(len(samples)), samples,
            ).astype(np.int16)
        data = samples.tobytes()

    return data, SAMPLE_RATE_INPUT


def write_wav(path: str, data: bytes, sample_rate: int = SAMPLE_RATE_OUTPUT) -> None:
    """保存 PCM 数据为 WAV 文件"""
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(data)


# =============================================================================
# 麦克风采集
# =============================================================================

class MicrophoneCapture:
    """异步麦克风采集器 — 对齐固件端 mic.record() 行为"""

    def __init__(self, device_id: Optional[int] = None):
        self._device = device_id
        self._stream: Optional[sd.InputStream] = None
        self._running = False

    async def start(self) -> None:
        if not _HAS_AUDIO:
            return
        self._running = True

    async def stop(self) -> None:
        self._running = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    async def read_chunk(self) -> Optional[bytes]:
        """
        读取一帧音频 (2000 samples, ~125ms)。

        对齐固件: M5.Mic.record(rtRecBuf, rtRecLength, rtRecSamplerate)
        每次读取 2000 samples，16kHz，16-bit，mono。
        """
        if not _HAS_AUDIO or not self._running:
            return None

        loop = asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(
                None,
                lambda: sd.rec(
                    CHUNK_SAMPLES,
                    samplerate=SAMPLE_RATE_INPUT,
                    channels=1,
                    dtype="int16",
                    blocking=True,
                )
            )
            return data.tobytes()
        except Exception:
            return None


# =============================================================================
# 音频播放
# =============================================================================

class AudioPlayer:
    """流式音频播放器 — 支持立即截断"""

    def __init__(self):
        self._stream: Optional[sd.OutputStream] = None
        self._playing = False

    def start(self) -> None:
        """打开播放流"""
        if not _HAS_AUDIO:
            return
        self._stream = sd.OutputStream(
            samplerate=SAMPLE_RATE_OUTPUT,
            channels=1,
            dtype="int16",
        )
        self._stream.start()
        self._playing = True

    def stop(self) -> None:
        """
        立即停止播放 (对应打断)。

        对齐固件: 固件端在收到新音频时停止当前播放，
        调用 M5.Speaker.end() 然后 M5.Mic.begin()。
        """
        self._playing = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def feed(self, pcm_chunk: bytes) -> None:
        """喂入音频块，立即播放"""
        if not self._playing or not self._stream:
            return
        try:
            samples = np.frombuffer(pcm_chunk, dtype=np.int16)
            self._stream.write(samples)
        except Exception:
            pass


# =============================================================================
# 键盘输入 (非阻塞)
# =============================================================================

class KeyboardReader:
    """
    非阻塞键盘读取。

    在 Unix 上通过 sys.stdin + asyncio 实现。
    按 i → 打断，按 q → 退出。
    """

    def __init__(self):
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    async def start(self) -> None:
        """启动键盘监听"""
        loop = asyncio.get_event_loop()

        if sys.platform == "win32":
            # Windows: 使用 msvcrt
            import msvcrt
            async def _reader():
                while True:
                    if msvcrt.kbhit():
                        ch = msvcrt.getch().decode("utf-8", errors="replace").lower()
                        await self._queue.put(ch)
                    await asyncio.sleep(0.05)
        else:
            # Unix: 使用 sys.stdin
            import termios, tty
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            tty.setcbreak(fd)

            async def _reader():
                try:
                    while True:
                        ch = await loop.run_in_executor(None, sys.stdin.read, 1)
                        if ch:
                            await self._queue.put(ch.lower())
                finally:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old)

        asyncio.create_task(_reader())

    async def get(self) -> str:
        """获取一个按键 (非阻塞等待)"""
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=0.1)
        except asyncio.TimeoutError:
            return ""


# =============================================================================
# StackChan 模拟客户端
# =============================================================================

class StackChanClient:
    """
    StackChan 模拟客户端。

    完全模拟固件端行为，用于:
      1. 无硬件时调试服务端
      2. 自动化回归测试 (WAV 模式)
      3. 延迟性能测量
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8080,
        device_id: str = "sim-001",
        wav_file: Optional[str] = None,
        save_output: Optional[str] = None,
        verbose: bool = True,
    ):
        self._host = host
        self._port = port
        self._device_id = device_id
        self._wav_file = wav_file
        self._save_output = save_output
        self._verbose = verbose

        # 组件
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._mic = MicrophoneCapture()
        self._player = AudioPlayer()
        self._keyboard = KeyboardReader()
        self._stats = LatencyStats()

        # 输出音频累积 (用于保存)
        self._output_buffer: bytearray = bytearray()

        # 状态
        self._running = False
        self._session_ready = False

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """运行客户端"""
        self._running = True

        ws_url = f"ws://{self._host}:{self._port}/v1/realtime?model=gpt-realtime"
        self._log(f"连接: {ws_url}")
        self._log(f"设备ID: {self._device_id}")
        self._log("=" * 56)
        self._log("按键: i=打断 q=退出")
        self._log("")

        try:
            async with websockets.connect(ws_url, ping_interval=20) as ws:
                self._ws = ws

                # 1. 发送 session.update (固件连接后第一条消息)
                await self._send_session_update()
                self._log("[SEND] session.update → 等待 session.updated...")

                # 2. 启动键盘监听
                await self._keyboard.start()

                # 3. 根据模式启动
                if self._wav_file:
                    await self._run_wav_mode()
                else:
                    await self._run_mic_mode()

        except websockets.ConnectionClosed as e:
            self._log(f"连接关闭: {e}")
        except Exception as e:
            self._log(f"错误: {e}")
        finally:
            self._running = False
            self._player.stop()
            await self._mic.stop()

            # 保存输出音频
            if self._save_output and self._output_buffer:
                write_wav(self._save_output, bytes(self._output_buffer))
                self._log(f"输出音频已保存: {self._save_output} ({len(self._output_buffer)} bytes)")

            self._print_stats()

    # ------------------------------------------------------------------
    # 麦克风交互模式
    # ------------------------------------------------------------------

    async def _run_mic_mode(self) -> None:
        """实时麦克风采集 → 发送 → 接收播放"""
        self._log("[MODE] 实时麦克风模式")
        self._log("开始录音 (检测到语音自动发送, 30s 超时静音停止)")
        self._log("")

        await self._mic.start()
        self._player.start()

        recording = False
        silent_chunks = 0
        max_silent = int(RECORD_TIMEOUT_S * 1000 / CHUNK_DURATION_MS)  # ~240 chunks

        recv_task = asyncio.create_task(self._recv_loop())

        try:
            while self._running:
                # 键盘检查
                key = await self._keyboard.get()
                if key in ("q", "\x1b"):  # q or ESC
                    self._running = False
                    break
                if key == "i":
                    await self._send_interrupt()

                # 音频采集
                chunk = await self._mic.read_chunk()
                if chunk is None:
                    await asyncio.sleep(0.01)
                    continue

                # VAD 检测
                energy = rms(chunk)
                if energy > VAD_THRESHOLD:
                    if not recording:
                        recording = True
                        self._stats.reset()
                        self._log(f"[VAD] 语音开始 (RMS={energy:.0f})")
                    silent_chunks = 0
                    await self._send_audio(chunk)

                    # 小延迟模拟实时流
                    await asyncio.sleep(CHUNK_DURATION_MS / 1000 * 0.8)
                elif recording:
                    silent_chunks += 1
                    await self._send_audio(chunk)

                    if silent_chunks > max_silent:
                        self._log(f"[VAD] 静音 {RECORD_TIMEOUT_S}s, 提交识别")
                        await self._commit_audio()
                        recording = False
                        silent_chunks = 0

        finally:
            recv_task.cancel()
            try:
                await recv_task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # WAV 文件模式
    # ------------------------------------------------------------------

    async def _run_wav_mode(self) -> None:
        """WAV 文件输入 → 发送 → 接收播放 (自动化测试)"""
        self._log(f"[MODE] WAV 文件模式: {self._wav_file}")

        pcm_data, sr = read_wav(self._wav_file)
        total_ms = len(pcm_data) / (sr * 2) * 1000
        self._log(f"[WAV] {len(pcm_data)} bytes, {sr}Hz, {total_ms:.0f}ms")
        self._log("")

        self._player.start()
        self._stats.reset()

        recv_task = asyncio.create_task(self._recv_loop())

        try:
            # 按 2000 samples 分片发送
            chunk_size = CHUNK_SAMPLES * 2
            for i in range(0, len(pcm_data), chunk_size):
                if not self._running:
                    break

                chunk = pcm_data[i:i + chunk_size]
                await self._send_audio(chunk)

                # 模拟实时速率 (可加速)
                await asyncio.sleep(CHUNK_DURATION_MS / 1000)

            # 提交
            self._log(f"[WAV] 文件发送完毕, 提交识别")
            await self._commit_audio()

            # 等待接收完成 (最多 60s)
            await asyncio.sleep(60)

        finally:
            recv_task.cancel()
            try:
                await recv_task
            except asyncio.CancelledError:
                pass

    # ------------------------------------------------------------------
    # 协议方法
    # ------------------------------------------------------------------

    async def _send_session_update(self) -> None:
        """发送 session.update (对齐固件)"""
        msg = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": "gpt-realtime",
                "output_modalities": ["audio"],
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": SAMPLE_RATE_INPUT},
                        "turn_detection": {"type": "semantic_vad"},
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": SAMPLE_RATE_OUTPUT},
                        "voice": "alloy",
                    },
                },
                "instructions": "You are Stack-chan, a friendly robot assistant.",
                "tools": [],
                "tool_choice": "auto",
                "temperature": 0.8,
            },
        }
        await self._ws.send(json.dumps(msg))

    async def _send_audio(self, pcm: bytes) -> None:
        """发送音频块"""
        msg = {
            "type": "input_audio_buffer.append",
            "audio": pcm_to_base64(pcm),
        }
        await self._ws.send(json.dumps(msg))

    async def _commit_audio(self) -> None:
        """提交音频缓冲区"""
        self._stats.commit_time = time.time()
        msg = {"type": "input_audio_buffer.commit"}
        await self._ws.send(json.dumps(msg))
        self._log(f"[SEND] audio.commit (缓冲 {self._stats.commit_time:.3f})")

    async def _send_interrupt(self) -> None:
        """发送打断"""
        self._player.stop()
        msg = {"type": "response.cancel"}
        await self._ws.send(json.dumps(msg))
        self._log("[SEND] response.cancel (打断)")

    # ------------------------------------------------------------------
    # 接收循环
    # ------------------------------------------------------------------

    async def _recv_loop(self) -> None:
        """接收服务端消息"""
        try:
            async for raw in self._ws:
                await self._handle_message(raw)
        except websockets.ConnectionClosed:
            pass

    async def _handle_message(self, raw: str) -> None:
        """处理服务端消息"""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = msg.get("type", "")

        if msg_type == "session.updated":
            self._session_ready = True
            self._log("[RECV] session.updated → 会话就绪")

        elif msg_type == "input_audio_buffer.speech_started":
            self._log("[RECV] speech_started")

        elif msg_type == "input_audio_buffer.speech_stopped":
            self._log("[RECV] speech_stopped")

        elif msg_type == "input_audio_buffer.committed":
            self._log("[RECV] audio.committed → 服务端开始处理")

        elif msg_type == "response.output_audio_transcript.delta":
            delta = msg.get("delta", "")
            if self._stats.text_count == 0:
                self._stats.first_token_ms = (time.time() - self._stats.commit_time) * 1000
                self._log(f"[LATENCY] 首Token: {self._stats.first_token_ms:.0f}ms")
            self._stats.text_count += 1
            if self._verbose:
                print(f"\r[ASR] {delta}", end="", flush=True)

        elif msg_type == "response.output_audio.delta":
            b64 = msg.get("delta", "")
            if not b64:
                return
            pcm = base64_to_pcm(b64)

            if self._stats.audio_count == 0:
                self._stats.first_audio_ms = (time.time() - self._stats.commit_time) * 1000
                self._log(f"\n[LATENCY] 首帧音频: {self._stats.first_audio_ms:.0f}ms")

            self._stats.audio_count += 1
            self._output_buffer.extend(pcm)

            if self._verbose:
                print(f"\r[AUDIO] 块#{self._stats.audio_count} ({len(pcm)} bytes)", end="", flush=True)

            self._player.feed(pcm)

        elif msg_type == "response.output_audio.done":
            self._log("\n[RECV] audio.done")

        elif msg_type == "response.done":
            self._stats.total_turn_ms = (time.time() - self._stats.commit_time) * 1000
            self._log(f"[RECV] response.done")
            self._log(f"[LATENCY] 总轮次: {self._stats.total_turn_ms:.0f}ms")

        elif msg_type == "error":
            err = msg.get("error", {})
            self._log(f"[RECV] ERROR: {err.get('message', str(err))}")

        elif msg_type == "rate_limits.updated":
            pass  # 忽略

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _log(self, msg: str) -> None:
        t = time.strftime("%H:%M:%S")
        print(f"[{t}] {msg}", flush=True)

    def _print_stats(self) -> None:
        """打印延迟统计"""
        print()
        print("=" * 40)
        print("  延迟统计")
        print("=" * 40)
        print(f"  首Token延迟:  {self._stats.first_token_ms:.0f} ms")
        print(f"  首帧音频延迟: {self._stats.first_audio_ms:.0f} ms")
        print(f"  总轮次延迟:   {self._stats.total_turn_ms:.0f} ms")
        print(f"  文本块数:     {self._stats.text_count}")
        print(f"  音频块数:     {self._stats.audio_count}")
        print(f"  输出音频:     {len(self._output_buffer)} bytes "
              f"({len(self._output_buffer)/SAMPLE_RATE_OUTPUT/2:.1f}s)")
        print("=" * 40)


# =============================================================================
# CLI 入口
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="StackChan 模拟客户端 — 无硬件调试 + 自动化测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 实时麦克风交互
  python tools/stackchan_client.py --host 192.168.1.100 --port 8080

  # WAV 文件测试
  python tools/stackchan_client.py --wav test_input.wav --save-output out.wav

  # 静默模式 (仅打印延迟统计)
  python tools/stackchan_client.py --wav test_input.wav --quiet

测试建议:
  1. 准备一段 3-5 秒的 WAV 语音文件 (16kHz mono)
  2. 运行服务端: python -m stackchan_server.main
  3. 运行客户端: python tools/stackchan_client.py --wav hello.wav
  4. 检查输出: 首Token延迟 < 2s, 首帧音频延迟 < 3s
        """,
    )
    parser.add_argument("--host", default="127.0.0.1", help="服务端地址")
    parser.add_argument("--port", type=int, default=8080, help="服务端端口")
    parser.add_argument("--device-id", default="sim-001", help="模拟设备 ID")
    parser.add_argument("--wav", default=None, help="WAV 文件路径 (输入)")
    parser.add_argument("--save-output", default=None, help="保存接收音频到 WAV 文件")
    parser.add_argument("--quiet", action="store_true", help="静默模式 (减少日志)")
    args = parser.parse_args()

    client = StackChanClient(
        host=args.host,
        port=args.port,
        device_id=args.device_id,
        wav_file=args.wav,
        save_output=args.save_output,
        verbose=not args.quiet,
    )

    asyncio.run(client.run())


if __name__ == "__main__":
    main()
