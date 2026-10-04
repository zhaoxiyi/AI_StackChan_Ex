"""
WebSocket 会话管理

单个 Realtime API 连接的生命周期管理:
  - 连接建立 → session.update → 就绪
  - 音频流接收 → VAD 检测 → 提交识别
  - 响应播放 → 状态切换 → 重新收听

状态同步: 会话状态与固件端状态机的对应关系
  固件: Connecting → Please touch → Listening → Speaking → Please touch
  服务: CONNECTING  → READY       → LISTENING → SPEAKING → READY
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional, Callable, Awaitable

from fastapi import WebSocket

from .messages import (
    SessionConfig,
    ServerEvent,
    build_session_updated,
    build_speech_started,
    build_speech_stopped,
    build_audio_buffer_committed,
    build_error_event,
)
from .parser import FrameParser, ParsedEvent, ParsedEventType, EventSerializer
from ..logging_config import get_session_logger


# =============================================================================
# 会话状态
# =============================================================================

class SessionState(Enum):
    """会话生命周期 - 对齐固件端状态"""
    CONNECTING = auto()       # WebSocket 刚建立
    HANDSHAKE = auto()        # 等待 session.update
    READY = auto()            # 就绪，等待用户触摸
    LISTENING = auto()        # 录音中（收到 speech_started）
    PROCESSING = auto()       # STT + LLM 推理中
    SPEAKING = auto()         # TTS 播放中
    CLOSING = auto()          # 正在关闭
    CLOSED = auto()           # 已关闭


# =============================================================================
# 事件处理器类型
# =============================================================================

EventHandler = Callable[["RealtimeSession", ParsedEvent], Awaitable[None]]


# =============================================================================
# 会话对象
# =============================================================================

@dataclass
class RealtimeSession:
    """Realtime API 协议会话"""

    # 标识
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)

    # 连接
    websocket: Optional[WebSocket] = None
    state: SessionState = SessionState.CONNECTING

    # 协议配置
    session_config: Optional[SessionConfig] = None

    # 解析器
    parser: FrameParser = field(default_factory=FrameParser)

    # 音频缓冲区（PCM 16-bit 累积）
    audio_buffer: bytearray = field(default_factory=bytearray)
    is_speech_active: bool = False
    speech_start_time: float = 0.0

    # 取消
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)

    # 事件处理器注册表
    _handlers: dict[ParsedEventType, EventHandler] = field(default_factory=dict, repr=False)

    # 发送串行锁 (防并发 send 交错消息帧)
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    # 日志
    _logger: "loguru.Logger" = field(default=None, repr=False)

    def __post_init__(self):
        self._logger = get_session_logger(self.session_id)

    @property
    def slog(self):
        return self._logger

    # --- 事件处理器注册 ---

    def on(self, event_type: ParsedEventType, handler: EventHandler) -> None:
        """注册事件处理器"""
        self._handlers[event_type] = handler

    # --- 消息入口 ---

    async def feed_text(self, raw_json: str) -> None:
        """
        处理文本帧。

        流程: JSON 解析 → ParsedEvent → 路由到注册的处理器
        """
        event = self.parser.parse_text_frame(raw_json)
        if event is None:
            self.slog.debug(f"无法解析的文本帧: {raw_json[:100]}")
            return

        self.slog.debug(f"事件: {event.raw_event_type}")

        # 路由到注册处理器
        handler = self._handlers.get(event.type)
        if handler:
            await handler(self, event)
        else:
            await self._default_handler(event)

    async def feed_binary(self, raw_bytes: bytes) -> None:
        """
        处理二进制帧（Opus 音频预留）。
        """
        event = self.parser.parse_binary_frame(raw_bytes)
        handler = self._handlers.get(event.type)
        if handler:
            await handler(self, event)

    async def feed_any(self, data: str | bytes) -> None:
        """自动识别帧类型"""
        if isinstance(data, str):
            await self.feed_text(data)
        elif isinstance(data, bytes):
            await self.feed_binary(data)

    # --- 默认处理器（编排层可覆盖）---

    async def _default_handler(self, event: ParsedEvent) -> None:
        """默认事件处理"""
        if event.type == ParsedEventType.SESSION_UPDATE:
            await self._handle_session_update(event)

        elif event.type == ParsedEventType.AUDIO_APPEND_PCM:
            await self._handle_audio_append(event)

        elif event.type == ParsedEventType.AUDIO_COMMIT:
            await self._handle_audio_commit(event)

        elif event.type == ParsedEventType.AUDIO_CLEAR:
            await self._handle_audio_clear(event)

        elif event.type == ParsedEventType.RESPONSE_CANCEL:
            await self._handle_response_cancel(event)

    # --- 默认事件实现 ---

    async def _handle_session_update(self, event: ParsedEvent) -> None:
        """处理 session.update → 发送 session.updated"""
        self.session_config = event.session_config or SessionConfig()
        self.state = SessionState.READY

        self.slog.info(
            f"会话配置: model={self.session_config.model}, "
            f"output_modalities={self.session_config.output_modalities}"
        )

        await self.send_json(
            build_session_updated(self.session_id, self.session_config)
        )

    async def _handle_audio_append(self, event: ParsedEvent) -> None:
        """音频追加到缓冲区 + 简易 VAD"""
        if not event.audio_data:
            return

        self.audio_buffer.extend(event.audio_data)

        # 简易 VAD: 能量检测
        if not self.is_speech_active and self._detect_speech(event.audio_data):
            self.is_speech_active = True
            self.speech_start_time = time.time()
            self.state = SessionState.LISTENING
            await self.send_json(build_speech_started())
            self.slog.debug("VAD: 语音开始")

    async def _handle_audio_commit(self, event: ParsedEvent) -> None:
        """音频提交 → 触发识别"""
        self.slog.info(f"音频提交: {len(self.audio_buffer)} bytes")

        if self.is_speech_active:
            await self.send_json(build_speech_stopped())

        self.is_speech_active = False
        self.state = SessionState.PROCESSING
        await self.send_json(build_audio_buffer_committed())

    async def _handle_audio_clear(self, event: ParsedEvent) -> None:
        """清空缓冲区"""
        self.audio_buffer.clear()
        self.is_speech_active = False

    async def _handle_response_cancel(self, event: ParsedEvent) -> None:
        """用户打断"""
        self.slog.info("收到 response.cancel (打断)")
        self.cancel_event.set()
        self.state = SessionState.READY

    # --- 发送方法 ---

    async def send_json(self, data: dict) -> None:
        """发送 JSON 消息 — 连接断开时静默跳过。

        串行化发送: dispatcher 上下文与异步处理 task 可能并发发送
        (speech_started/committed vs audio.delta), 无锁并发会让
        Starlette WebSocket 的消息帧交错, 固件端 JSON 解析失败
        (表现为 Unknown event / 静默丢消息)。"""
        if not self.websocket or self.state in (SessionState.CLOSED, SessionState.CLOSING):
            return
        async with self._send_lock:
            try:
                # 连接状态检查: Starlette 用 WebSocketState.CONNECTED,
                # python-websockets 用 State.OPEN — 两者都要接受
                if hasattr(self.websocket, 'client_state'):
                    cs = self.websocket.client_state
                    cs_name = getattr(cs, 'name', str(cs))
                    if cs_name not in ('CONNECTED', 'OPEN'):
                        return
                await self.websocket.send_text(EventSerializer.serialize(data))
            except Exception as e:
                if "close message" not in str(e) and "not connected" not in str(e).lower():
                    self.slog.error(f"发送失败: {e}")

    async def send_error(self, error_type: str, message: str) -> None:
        """发送错误"""
        await self.send_json(build_error_event(error_type, message))

    async def close(self) -> None:
        """关闭会话"""
        self.state = SessionState.CLOSING
        self.cancel_event.set()
        if self.websocket:
            try:
                await self.websocket.close()
            except Exception:
                pass
        self.state = SessionState.CLOSED
        self.slog.info("会话关闭")

    # --- 辅助方法 ---

    def _detect_speech(self, audio_bytes: bytes) -> bool:
        """简易 RMS 能量检测"""
        if len(audio_bytes) < 4:
            return False
        import array
        samples = array.array("h", audio_bytes)
        rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
        return rms > 200

    def drain_audio(self) -> bytes:
        """取出并清空音频缓冲区"""
        data = bytes(self.audio_buffer)
        self.audio_buffer.clear()
        return data
