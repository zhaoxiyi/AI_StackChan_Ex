"""
WebSocket 接入层 — 编排层与协议层的桥梁

上行 (固件 → 服务):
  二进制帧 → Opus/PCM 音频 → Session.handle_audio_chunk()
  文本帧   → JSON 解析 → ParsedEvent → 路由到 Session 方法

下行 (服务 → 固件):
  Session.on_audio_chunk   → base64 编码 → JSON 文本帧发送
  Session.on_text_delta    → response.output_audio_transcript.delta
  Session.on_device_event  → 设备指令 (Phase 2)
  Session.on_state_change  → 状态日志

固件时序保证:
  严格按回调触发顺序发送，不缓存不重排。
  response.done 作为每轮结束信号，固件据此切换 microphone/speaker。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Optional

import numpy as np

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request

from .session_manager import SessionManager
from ..protocol.parser import FrameParser, ParsedEvent, ParsedEventType, EventSerializer
from ..protocol.session import RealtimeSession
from ..protocol.messages import (
    ServerEvent,
    build_session_updated,
    build_speech_started,
    build_speech_stopped,
    build_audio_buffer_committed,
    build_audio_delta,
    build_audio_done,
    build_audio_transcript_delta,
    build_response_done,
    build_error_event,
)
from ..orchestration.session import ConversationSession
from ..orchestration.state_machine import ConversationState
from ..engines.base import EngineState, EngineConfig
from ..engines.llm.llm_engine import LLMEngine
from ..engines.stt.stt_engine import STTEngine
from ..engines.tts.tts_engine import TTSEngine
from ..config import get_config
from ..logging_config import get_session_logger

ws_router = APIRouter()


# =============================================================================
# 全局 SessionManager (在 main.py lifespan 中初始化)
# =============================================================================

_session_manager: Optional[SessionManager] = None


def get_session_manager() -> SessionManager:
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
    return _session_manager


async def init_session_manager(llm_pool: dict, stt_engine, tts_engine) -> SessionManager:
    """在服务启动时初始化 SessionManager (由 main.py lifespan 调用)"""
    global _session_manager
    _session_manager = SessionManager()
    await _session_manager.start_cleanup()
    return _session_manager


# =============================================================================
# WebSocket 端点 — 完全兼容 OpenAI Realtime API 路径
# =============================================================================

@ws_router.websocket("/v1/realtime")
async def realtime_websocket(websocket: WebSocket) -> None:
    """
    主 WebSocket 端点。

    固件端配置:
      webSocket.begin("192.168.x.x", 8080, "/v1/realtime?model=gpt-realtime");

    上行帧格式:
      文本帧 (JSON):
        {"type":"session.update","session":{...}}
        {"type":"input_audio_buffer.append","audio":"<base64_pcm>"}
        {"type":"input_audio_buffer.commit"}
        {"type":"response.cancel"}
      二进制帧 (预留 Opus):
        原始 Opus 编码音频字节
    """
    await websocket.accept()

    # 提取设备 ID (从 query param 或 header)
    device_id = _extract_device_id(websocket)

    mgr = get_session_manager()
    session: ConversationSession = await mgr.get_or_create(device_id)
    parser = FrameParser()
    slog = get_session_logger(device_id)
    slog.info(f"WebSocket 连接建立")

    # 绑定协议会话到编排会话 (用于下行发送)
    proto = RealtimeSession(websocket=websocket, session_id=device_id)
    session.set_protocol_session(proto)

    # 设置引擎 (需要从全局 loop engine 获取)
    await _ensure_engines(session)

    # ---- 注册下行回调 ----
    session.on_audio_chunk(lambda data: _send_audio_delta(proto, data))
    session.on_text_delta(lambda text: _send_text_delta(proto, text))
    session.on_device_event(lambda evt: _log_device_event(slog, evt))
    session.on_state_change(lambda st, ctx: _log_state_change(slog, st))
    # 错误回调 → 下发固件 error 事件
    session.on_error(lambda err: _send_error_to_device(proto, err))

    try:
        # 等待 session.update (固件连接后第一条消息)
        while True:
            raw = await websocket.receive_text()
            mgr.touch(device_id)

            # 解析
            event = parser.parse_text_frame(raw)
            if event is None:
                continue

            slog.debug(f"← {event.raw_event_type}")

            # 路由到编排层 (兜底: dispatch 异常不中断 ws 循环)
            try:
                await _dispatch(session, proto, event, slog)
            except Exception as e:
                slog.error(f"dispatch 异常: {e}")
                await proto.send_json(build_error_event("server_error", str(e)))

    except WebSocketDisconnect:
        slog.info("WebSocket 断开")
        mgr.mark_disconnected(device_id)
    except Exception as e:
        slog.error(f"WebSocket 异常: {e}")
        mgr.mark_disconnected(device_id)
    finally:
        await proto.close()


# =============================================================================
# 上行分发
# =============================================================================

async def _dispatch(
    session: ConversationSession,
    proto: RealtimeSession,
    event: ParsedEvent,
    slog,
) -> None:
    """将 ParsedEvent 分发到编排层对应方法"""

    if event.type == ParsedEventType.SESSION_UPDATE:
        # session.update → 回复 session.updated
        cfg = event.session_config
        await proto.send_json(build_session_updated(proto.session_id, cfg))
        slog.info(f"会话就绪: model={cfg.model if cfg else '?'}")
        # 保存 output_modalities 用于后续决策
        if cfg:
            proto.session_config = cfg
        # 重连恢复: 重置残留状态机 (上一轮可能停在 LISTENING/REASONING)
        from ..orchestration.state_machine import ConversationState
        if session.sm.state != ConversationState.IDLE:
            slog.info(f"状态机残留 ({session.sm.state.name})，重置为 IDLE")
            session.sm.reset()

    elif event.type == ParsedEventType.AUDIO_APPEND_PCM:
        # 音频接收统计 (低频日志, 每 10 块或每 2 秒)
        rx_count = getattr(session, "_audio_rx_count", 0) + 1
        rx_bytes = getattr(session, "_audio_rx_bytes", 0) + len(event.audio_data)
        last_log = getattr(session, "_last_audio_rx_log", 0.0)
        session._audio_rx_count = rx_count
        session._audio_rx_bytes = rx_bytes

        if rx_count % 10 == 0 or time.time() - last_log > 2.0:
            try:
                samples = np.frombuffer(event.audio_data, dtype=np.int16)
                rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
            except Exception:
                rms = 0.0
            slog.info(
                f"AUDIO_RX: 块#{rx_count} 本块={len(event.audio_data)}B "
                f"累计={rx_bytes}B ({rx_bytes/32:.1f}ms) RMS={rms:.0f} "
                f"状态={session.sm.state.name}"
            )
            session._last_audio_rx_log = time.time()

        # 音频追加 → 喂入编排层 + VAD 检测
        if not proto.is_speech_active and proto._detect_speech(event.audio_data):
            proto.is_speech_active = True
            await proto.send_json(build_speech_started())
            # 仅从 IDLE 进入 LISTENING (to_listening 本身幂等, 防止非法跳转)
            if session.sm.is_idle:
                await session.sm.to_listening()
            slog.debug("VAD: 语音开始")

        await session.handle_audio_chunk(event.audio_data)

    elif event.type == ParsedEventType.AUDIO_COMMIT:
        # 音频提交 → 停止录音 → 触发识别
        slog.info(f"AUDIO_COMMIT: 开始处理轮次")
        if proto.is_speech_active:
            await proto.send_json(build_speech_stopped())
        proto.is_speech_active = False
        await proto.send_json(build_audio_buffer_committed())

        # 启动异步轮次处理 (不阻塞 WebSocket 消息循环)
        asyncio.create_task(_process_turn(session, proto, slog))

    elif event.type == ParsedEventType.AUDIO_CLEAR:
        pass  # 忽略

    elif event.type == ParsedEventType.RESPONSE_CANCEL:
        # 打断
        slog.info("收到 response.cancel")
        await session.handle_interrupt()

    elif event.type == ParsedEventType.UNKNOWN:
        slog.debug(f"未处理事件: {event.raw_event_type}")


# =============================================================================
# 轮次处理 (异步)
# =============================================================================

async def _process_turn(
    session: ConversationSession,
    proto: RealtimeSession,
    slog,
) -> None:
    """
    异步处理一个完整对话轮次。

    此方法在独立 asyncio.Task 中运行，不阻塞 WebSocket 主循环。
    固件端在此期间可以发送新的音频 (打断) 或 cancel。
    """
    try:
        # 诊断: 检查引擎状态
        stt_ok = session._stt and session._stt.state.value < 3  # IDLE or READY or RUNNING
        llm_ok = session._llm and session._llm.state.value < 3
        tts_ok = session._tts and session._tts.state.value < 3
        slog.info(f"AUDIO_PROCESS: stt={stt_ok} llm={llm_ok} tts={tts_ok} text_len={len(session.sm.context.user_text)}")

        if not stt_ok:
            slog.warning("STT 引擎不可用，无法处理语音")
            await proto.send_json(build_error_event(
                "stt_unavailable",
                "语音识别引擎未就绪。请检查服务器日志。pip install faster-whisper"
            ))
            await proto.send_json(build_response_done())
            return

        # 触发 VAD 结束 → 内部调用 STT → LLM → TTS → 发送
        await session.handle_vad_end()
    except asyncio.CancelledError:
        slog.info("轮次被取消")
    except Exception as e:
        slog.error(f"轮次异常: {e}")
        await proto.send_json(build_error_event("server_error", str(e)))


# =============================================================================
# 下行发送 (回调 → WebSocket)
# =============================================================================

async def _send_audio_delta(proto: RealtimeSession, data: bytes) -> None:
    """
    音频分片下发。
    """
    import base64
    b64 = base64.b64encode(data).decode("ascii")
    try:
        await proto.send_json(build_audio_delta(b64))
    except Exception as e:
        get_session_logger(proto.session_id).warning(f"音频发送失败: {e}")


async def _send_text_delta(proto: RealtimeSession, text: str) -> None:
    """文本增量下发 (转录文本)"""
    try:
        await proto.send_json(build_audio_transcript_delta(text))
    except Exception:
        pass


async def _log_device_event(slog, evt) -> None:
    """设备事件日志 (Phase 2 会实际发送到固件)"""
    slog.debug(f"设备事件: {evt.type.value}")


async def _send_error_to_device(proto: RealtimeSession, error_msg: str) -> None:
    """错误通知下发固件"""
    await proto.send_json(build_error_event("server_error", error_msg))


async def _log_state_change(slog, state: ConversationState) -> None:
    """状态变化日志"""
    slog.debug(f"状态: {state.name}")


# =============================================================================
# 引擎注入
# =============================================================================

# 全局引擎引用 (由 main.py lifespan 设置)
_global_llm_pool: dict[str, LLMEngine] = {}
_global_stt: Optional[STTEngine] = None
_global_tts: Optional[TTSEngine] = None


def set_global_engines(
    llm_pool: dict[str, LLMEngine],
    stt: Optional[STTEngine],
    tts: Optional[TTSEngine],
) -> None:
    """设置全局引擎引用 (main.py 调用)"""
    global _global_llm_pool, _global_stt, _global_tts
    _global_llm_pool = llm_pool
    _global_stt = stt
    _global_tts = tts


async def _ensure_engines(session: ConversationSession) -> None:
    """确保编排会话已注入引擎"""
    # 为每个会话创建独立的 LLM 引擎实例 (共享底层 HTTP 池)
    llm = _global_llm_pool.get("vllm") or _global_llm_pool.get("deepseek")
    session.set_engines(
        stt=_global_stt,
        llm=llm,
        tts=_global_tts,
    )


# =============================================================================
# 设备 ID 提取
# =============================================================================

def _extract_device_id(websocket: WebSocket) -> str:
    """
    从 WebSocket 连接提取设备标识。

    优先级:
      1. URL 参数 ?device_id=xxx
      2. Header X-Device-ID
      3. 客户端 IP + User-Agent hash (fallback)

    固件适配:
      固件端当前不发送 device_id 参数，因此使用 fallback。
      后续固件版本可在 session.update 中携带设备 ID。
    """
    # URL 参数
    device_id = websocket.query_params.get("device_id", "")
    if device_id:
        return device_id

    # Header
    for key, value in websocket.headers.items():
        if key.lower() == "x-device-id":
            return value

    # Fallback: IP + UA hash
    client_ip = websocket.client.host if websocket.client else "unknown"
    ua = websocket.headers.get("user-agent", "")
    import hashlib
    return f"stackchan-{hashlib.md5(f'{client_ip}:{ua}'.encode()).hexdigest()[:8]}"
