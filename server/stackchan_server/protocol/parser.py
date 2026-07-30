"""
Realtime API 协议帧解析器

负责解析 WebSocket 帧，区分二进制和文本帧，转换为标准事件。

帧格式说明（基于固件端 + OpenAI Realtime API 规范）:

  文本帧 → JSON 信令:
    固件端当前将所有数据（含音频）以 JSON 文本帧发送:
      {"type":"input_audio_buffer.append","audio":"<base64_pcm>"}
    同时发送信令:
      {"type":"session.update","session":{...}}
      {"type":"response.cancel"}

  二进制帧 → Opus 音频（Phase 2 优化）:
    OpenAI 原生 Realtime API 使用二进制帧传输 Opus 编码音频，
    固定 20ms 帧长，24kHz。服务器需要：
      1. 解析 Opus 帧头（如有）
      2. 解码 Opus → PCM 16-bit
      3. 重采样到 STT 引擎需要的 16kHz

设计决策:
  - Phase 1: 同时支持文本帧 base64 音频（固件兼容）和二进制帧 Opus（预留）
  - 解析器不关心上层业务逻辑，只做帧→事件转换
  - 所有解析结果统一为 ParsedEvent，编排层只消费 ParsedEvent
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Awaitable
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from .messages import ClientEvent, ServerEvent, SessionConfig


# =============================================================================
# 解析后的标准化事件
# =============================================================================

class ParsedEventType(Enum):
    """解析后的标准化事件类型 - 屏蔽协议差异"""
    # 会话控制
    SESSION_UPDATE = auto()           # 固件发起会话配置
    # 音频输入
    AUDIO_APPEND_PCM = auto()         # 文本帧 base64 → 解码后的 PCM 数据
    AUDIO_APPEND_OPUS = auto()        # 二进制帧 → 原始 Opus 数据（预留）
    AUDIO_COMMIT = auto()             # 音频提交（触发识别）
    AUDIO_CLEAR = auto()              # 清空缓冲区
    # 对话控制
    RESPONSE_CREATE = auto()          # 请求生成回复
    RESPONSE_CANCEL = auto()          # 中断当前回复
    # 对话项
    ITEM_CREATE = auto()              # 创建对话项（function_call_output）
    # 未知
    UNKNOWN = auto()                  # 无法识别的事件


@dataclass
class ParsedEvent:
    """解析后的标准化事件
    
    编排层只消费此类事件，无需关心底层是文本帧还是二进制帧。
    """
    type: ParsedEventType
    # 会话配置（SESSION_UPDATE 时填充）
    session_config: Optional[SessionConfig] = None
    # 音频数据（AUDIO_APPEND_* 时填充）
    audio_data: bytes = b""
    audio_is_opus: bool = False       # True=Opus 编码, False=PCM 16-bit
    # 对话项（ITEM_CREATE 时填充）
    item_data: Optional[dict] = None
    # 原始事件类型（用于日志）
    raw_event_type: str = ""
    # 元数据
    timestamp: float = 0.0


# =============================================================================
# 帧解析器
# =============================================================================

class FrameParser:
    """
    WebSocket 帧解析器。

    职责:
      1. 区分文本帧（JSON 信令）和二进制帧（音频数据）
      2. 文本帧: JSON 解析 → 识别事件类型 → 解码内嵌音频 → ParsedEvent
      3. 二进制帧: 识别为 Opus 音频 → ParsedEvent（AUDIO_APPEND_OPUS）
      4. 过滤无效帧，报错但不中断连接

    协议兼容设计:
      - 文本帧 base64 音频: 固件当前模式，完全兼容
      - 二进制帧 Opus: OpenAI 标准格式，预留但暂不启用

    使用方式:
      parser = FrameParser()
      event = parser.parse_text_frame(json_string)
      event = parser.parse_binary_frame(opus_bytes)
    """

    # 已知的客户端事件类型 → ParsedEventType 映射
    _TEXT_EVENT_MAP: dict[str, ParsedEventType] = {
        ClientEvent.SESSION_UPDATE.value:           ParsedEventType.SESSION_UPDATE,
        ClientEvent.INPUT_AUDIO_BUFFER_APPEND.value: ParsedEventType.AUDIO_APPEND_PCM,
        ClientEvent.INPUT_AUDIO_BUFFER_COMMIT.value: ParsedEventType.AUDIO_COMMIT,
        ClientEvent.INPUT_AUDIO_BUFFER_CLEAR.value:  ParsedEventType.AUDIO_CLEAR,
        ClientEvent.RESPONSE_CREATE.value:           ParsedEventType.RESPONSE_CREATE,
        ClientEvent.RESPONSE_CANCEL.value:           ParsedEventType.RESPONSE_CANCEL,
        ClientEvent.CONVERSATION_ITEM_CREATE.value:   ParsedEventType.ITEM_CREATE,
    }

    def parse_text_frame(self, raw_json: str) -> Optional[ParsedEvent]:
        """
        解析文本帧（JSON 信令）。

        处理流程:
          1. JSON 反序列化
          2. 提取 type 字段，映射到 ParsedEventType
          3. 对 AUDIO_APPEND_PCM 类型，提取 audio 字段并 base64 解码
          4. 对 SESSION_UPDATE，提取 session 配置
          5. 返回 ParsedEvent

        Args:
            raw_json: 原始 JSON 字符串

        Returns:
            ParsedEvent 或 None（无效帧）
        """
        # 1. JSON 解析
        try:
            msg = json.loads(raw_json)
        except json.JSONDecodeError as e:
            # 可能是二进制帧误判，不抛异常
            return None

        if not isinstance(msg, dict):
            return None

        event_type_str = msg.get("type", "")
        if not event_type_str:
            return None

        # 2. 映射事件类型
        parsed_type = self._TEXT_EVENT_MAP.get(event_type_str, ParsedEventType.UNKNOWN)

        event = ParsedEvent(
            type=parsed_type,
            raw_event_type=event_type_str,
        )

        # 3. 按类型提取数据
        if parsed_type == ParsedEventType.SESSION_UPDATE:
            event.session_config = self._parse_session_config(msg.get("session", {}))

        elif parsed_type == ParsedEventType.AUDIO_APPEND_PCM:
            # 固件端以 base64 PCM 发送音频数据
            # 参考: RealtimeChatGPT.cpp buildInputAudioJson()
            # {"type":"input_audio_buffer.append","audio":"<base64>"}
            audio_b64 = msg.get("audio", "")
            if audio_b64:
                try:
                    event.audio_data = base64.b64decode(audio_b64)
                except Exception:
                    event.audio_data = b""
            event.audio_is_opus = False

        elif parsed_type == ParsedEventType.ITEM_CREATE:
            event.item_data = msg.get("item", {})

        return event

    def parse_binary_frame(self, raw_bytes: bytes) -> ParsedEvent:
        """
        解析二进制帧（Opus 音频）。

        协议兼容:
          OpenAI 原生 Realtime API 二进制帧格式:
            - 前若干字节可能是事件类型标记（可选）
            - 后续字节为 Opus 编码音频（24kHz, 20ms 帧长）
        
        Phase 1: 标记为 AUDIO_APPEND_OPUS，暂不解码。
        Phase 2: 加入 Opus → PCM 转换。

        Args:
            raw_bytes: 原始二进制数据

        Returns:
            ParsedEvent (type=AUDIO_APPEND_OPUS)
        """
        return ParsedEvent(
            type=ParsedEventType.AUDIO_APPEND_OPUS,
            audio_data=raw_bytes,
            audio_is_opus=True,
        )

    def parse_any_frame(self, data: str | bytes) -> Optional[ParsedEvent]:
        """
        自动识别帧类型并解析。

        Args:
            data: WebSocket 消息（文本 str 或二进制 bytes）

        Returns:
            ParsedEvent 或 None
        """
        if isinstance(data, str):
            return self.parse_text_frame(data)
        elif isinstance(data, bytes):
            return self.parse_binary_frame(data)
        return None

    # --- 私有方法 ---

    def _parse_session_config(self, session_data: dict) -> SessionConfig:
        """解析 session.update 中的配置数据"""
        audio_data = session_data.get("audio", {})

        return SessionConfig(
            model=session_data.get("model", "gpt-realtime"),
            modalities=session_data.get("modalities", ["text", "audio"]),
            output_modalities=session_data.get("output_modalities", ["audio"]),
            audio=audio_data,
            instructions=session_data.get("instructions", ""),
            voice=session_data.get("voice", "alloy"),
            input_audio_format=session_data.get("input_audio_format", "pcm16"),
            output_audio_format=session_data.get("output_audio_format", "pcm16"),
            input_audio_transcription=session_data.get("input_audio_transcription"),
            turn_detection=session_data.get("turn_detection", {"type": "semantic_vad"}),
            tools=session_data.get("tools", []),
            tool_choice=session_data.get("tool_choice", "auto"),
            temperature=session_data.get("temperature", 0.8),
            max_response_output_tokens=session_data.get("max_response_output_tokens"),
        )


# =============================================================================
# 事件序列化器 - ParsedEvent → JSON（发送给固件）
# =============================================================================

class EventSerializer:
    """
    将编排层产生的 ServerEvent 序列化为 JSON 字符串。

    编排层不需要直接构造 JSON，通过此类转换。
    """

    @staticmethod
    def serialize(event_dict: dict) -> str:
        """将事件字典序列化为 JSON 字符串"""
        return json.dumps(event_dict, ensure_ascii=False)

    @staticmethod
    def serialize_audio_delta(base64_audio: str) -> str:
        """快捷方法: 序列化音频增量"""
        from .messages import build_audio_delta
        return json.dumps(build_audio_delta(base64_audio), ensure_ascii=False)
