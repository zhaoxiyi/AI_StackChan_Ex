"""
OpenAI Realtime API 协议消息定义

完整对齐 OpenAI Realtime API 的信令格式。
固件端 (AI_StackChan_Ex) 基于这些事件类型进行状态切换。

参考来源:
  - firmware/src/llm/ChatGPT/RealtimeChatGPT.cpp (webSocketEvent 函数)
  - OpenAI Realtime API 官方文档

关键设计说明:
  1. 固件端录制 16kHz PCM，但 session.update 声明 24kHz
     → 服务器应取 session.update 中的 rate，但实际以 16kHz 处理音频
  2. 固件端 output_modalities 为 ["audio"]（默认）或 ["text"]（TTS 模式）
     → 服务器据此决定是发 audio.delta 还是 text.delta
  3. semantic_vad 是服务端 VAD，固件依赖 speech_started/committed 事件
     → 服务器需要自行实现 VAD 并在检测到语音边界时发送这两个事件
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any


# =============================================================================
# 信令类型 - 客户端 → 服务器
# =============================================================================

class ClientEvent(str, Enum):
    """客户端（固件）→ 服务器 的事件类型
    
    固件端发送这些事件，服务器解析后转换为内部事件传给编排层。
    """
    SESSION_UPDATE = "session.update"
    INPUT_AUDIO_BUFFER_APPEND = "input_audio_buffer.append"
    INPUT_AUDIO_BUFFER_COMMIT = "input_audio_buffer.commit"
    INPUT_AUDIO_BUFFER_CLEAR = "input_audio_buffer.clear"
    CONVERSATION_ITEM_CREATE = "conversation.item.create"
    CONVERSATION_ITEM_TRUNCATE = "conversation.item.truncate"
    CONVERSATION_ITEM_DELETE = "conversation.item.delete"
    RESPONSE_CREATE = "response.create"
    RESPONSE_CANCEL = "response.cancel"


# =============================================================================
# 信令类型 - 服务器 → 客户端
# =============================================================================

class ServerEvent(str, Enum):
    """服务器 → 客户端（固件）的事件类型
    
    这些事件必须严格按照 OpenAI Realtime API 格式发送。
    固件端在 webSocketEvent() 中根据 type 字段分发处理。
    """
    ERROR = "error"
    SESSION_CREATED = "session.created"
    SESSION_UPDATED = "session.updated"
    INPUT_AUDIO_BUFFER_SPEECH_STARTED = "input_audio_buffer.speech_started"
    INPUT_AUDIO_BUFFER_SPEECH_STOPPED = "input_audio_buffer.speech_stopped"
    INPUT_AUDIO_BUFFER_COMMITTED = "input_audio_buffer.committed"
    CONVERSATION_ITEM_CREATED = "conversation.item.created"
    CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_COMPLETED = (
        "conversation.item.input_audio_transcription.completed"
    )
    CONVERSATION_ITEM_INPUT_AUDIO_TRANSCRIPTION_FAILED = (
        "conversation.item.input_audio_transcription.failed"
    )
    RESPONSE_CREATED = "response.created"
    RESPONSE_DONE = "response.done"
    RESPONSE_OUTPUT_ITEM_ADDED = "response.output_item.added"
    RESPONSE_OUTPUT_ITEM_DONE = "response.output_item.done"
    RESPONSE_CONTENT_PART_ADDED = "response.content_part.added"
    RESPONSE_CONTENT_PART_DONE = "response.content_part.done"
    RESPONSE_AUDIO_TRANSCRIPT_DELTA = "response.output_audio_transcript.delta"
    RESPONSE_AUDIO_TRANSCRIPT_DONE = "response.output_audio_transcript.done"
    RESPONSE_AUDIO_DELTA = "response.output_audio.delta"
    RESPONSE_AUDIO_DONE = "response.output_audio.done"
    RESPONSE_TEXT_DELTA = "response.output_text.delta"
    RESPONSE_TEXT_DONE = "response.output_text.done"
    RESPONSE_FUNCTION_CALL_ARGUMENTS_DELTA = "response.function_call_arguments.delta"
    RESPONSE_FUNCTION_CALL_ARGUMENTS_DONE = "response.function_call_arguments.done"
    RATE_LIMITS_UPDATED = "rate_limits.updated"


# =============================================================================
# 音频格式配置（对齐 session.update 中的 audio 节）
# =============================================================================

@dataclass
class AudioFormat:
    """音频格式
    
    协议兼容: 固件端 session.update 中:
      "audio": {
        "input":  {"format": {"type": "audio/pcm", "rate": 24000}},
        "output": {"format": {"type": "audio/pcm", "rate": 24000}}
      }
    实际固件录制 16kHz，服务器应以 16kHz 处理输入，
    输出 24kHz PCM 给固件播放。
    """
    type: str = "audio/pcm"
    rate: int = 24000


@dataclass
class SessionConfig:
    """会话配置（session.update 事件体）
    
    协议兼容: 固件端在 WebSocket 连接成功后立即发送 session.update，
    服务器在收到后回复 session.updated。
    固件据此认为连接就绪，开始显示 "Please touch" 并等待触摸。
    """
    model: str = "gpt-realtime"
    modalities: list[str] = field(default_factory=lambda: ["text", "audio"])
    output_modalities: list[str] = field(default_factory=lambda: ["audio"])
    audio: dict = field(default_factory=dict)
    instructions: str = ""
    voice: str = "alloy"
    input_audio_format: str = "pcm16"
    output_audio_format: str = "pcm16"
    input_audio_transcription: Optional[dict] = None
    turn_detection: Optional[dict] = field(
        default_factory=lambda: {"type": "semantic_vad"}
    )
    tools: list[dict] = field(default_factory=list)
    tool_choice: str = "auto"
    temperature: float = 0.8
    max_response_output_tokens: Optional[int] = None


# =============================================================================
# 消息工厂方法 - 服务器 → 固件
# =============================================================================

def build_session_updated(session_id: str, cfg: SessionConfig) -> dict:
    """构建 session.updated 确认消息 (极简版)

    协议兼容: 固件端收到此事件后仅执行 avatar.setSpeechText("Please touch"),
    不解析 session 内容。因此不回显 tools/instructions — 固件端 session.update
    携带 14 个 tools + 超长 instructions (约 8KB), 完整回显会超过固件
    WebSockets 库的接收缓冲, 卡坏接收流导致后续所有消息 (committed /
    audio.delta) 无法接收 (表现为 Unknown event + 无声)。
    极简回显 (< 500B) 保证接收流畅通。
    """
    return {
        "type": ServerEvent.SESSION_UPDATED.value,
        "session_id": session_id,
        "session": {
            "type": "realtime",
            "model": cfg.model,
            "modalities": cfg.modalities,
            "audio": cfg.audio,        # 小 (格式声明, 无 tools)
            "tools": [],               # 不回显 (关键: 避免大消息)
            "tool_choice": "auto",
            "temperature": cfg.temperature,
            "instructions": "",        # 不回显 (关键: 避免大消息)
        },
    }


def build_speech_started() -> dict:
    """构建 speech_started 事件
    
    协议兼容: 固件端收到后调用 resetRealtimeRecordStartTime() 重置超时计时器。
    """
    return {"type": ServerEvent.INPUT_AUDIO_BUFFER_SPEECH_STARTED.value}


def build_speech_stopped() -> dict:
    """构建 speech_stopped 事件"""
    return {"type": ServerEvent.INPUT_AUDIO_BUFFER_SPEECH_STOPPED.value}


def build_audio_buffer_committed() -> dict:
    """构建 committed 事件
    
    协议兼容: 固件端收到后:
      1. 调用 stopRealtimeRecord() 停止录音
      2. 调用 M5.Mic.end(); M5.Speaker.begin() 切换音频硬件
      3. 设置 speaking = true，开始等待音频输出
    """
    return {"type": ServerEvent.INPUT_AUDIO_BUFFER_COMMITTED.value}


def build_audio_delta(base64_audio: str) -> dict:
    """构建音频增量事件
    
    协议兼容: 固件端收到后调用 streamAudioDelta() 解码 base64 → PCM16，
    然后通过 M5.Speaker.playRaw(buf, len/2, 24000, false) 播放。
    """
    return {
        "type": ServerEvent.RESPONSE_AUDIO_DELTA.value,
        "delta": base64_audio,
    }


def build_audio_done() -> dict:
    """构建音频完成事件"""
    return {"type": ServerEvent.RESPONSE_AUDIO_DONE.value}


def build_audio_transcript_delta(text: str) -> dict:
    """构建转录文本增量"""
    return {
        "type": ServerEvent.RESPONSE_AUDIO_TRANSCRIPT_DELTA.value,
        "delta": text,
    }


def build_text_delta(text: str) -> dict:
    """构建文本增量（TTS 模式）
    
    协议兼容: 当固件编译了 REALTIME_API_WITH_TTS 标志时，
    期望接收 text delta 而非 audio delta。
    """
    return {
        "type": ServerEvent.RESPONSE_TEXT_DELTA.value,
        "delta": text,
    }


def build_response_done(response_id: str = "resp_1") -> dict:
    """构建 response.done 事件
    
    协议兼容: 固件端收到后:
      1. 等待 M5.Speaker 播放完毕
      2. 调用 M5.Speaker.end(); M5.Mic.begin() 切回录音
      3. 调用 startRealtimeRecord() 重新开始录音
      4. 设置 speaking = false
    这个事件是对话轮次切换的关键。
    """
    return {
        "type": ServerEvent.RESPONSE_DONE.value,
        "response": {
            "id": response_id,
            "object": "realtime.response",
            "status": "completed",
            "status_details": None,
            "output": [],
            "usage": {
                "total_tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
            },
        },
    }


def build_error_event(error_type: str, message: str, code: str = "server_error") -> dict:
    """构建 error 事件
    
    协议兼容: 固件端 webSocketEvent 中处理 "error" 类型，
    打印错误 payload 但不中断连接。
    """
    return {
        "type": ServerEvent.ERROR.value,
        "event_id": f"evt_{abs(hash(message)) & 0xFFFFFFFF:08x}",
        "error": {
            "type": error_type,
            "code": code,
            "message": message,
            "param": None,
        },
    }


def build_rate_limits_updated() -> dict:
    """构建速率限制更新事件
    
    协议兼容: 固件端收到 rate_limits.updated 后仅打印日志，
    不改变行为。发送此事件可让固件确认服务端正常运行。
    """
    return {
        "type": ServerEvent.RATE_LIMITS_UPDATED.value,
        "rate_limits": [
            {"name": "requests", "limit": 100, "remaining": 99, "reset_seconds": 1.0},
            {"name": "tokens", "limit": 50000, "remaining": 49990, "reset_seconds": 1.0},
        ],
    }
