"""
Faster-Whisper 本地 STT 引擎 - 基于 BaseSTT 实现
"""

from __future__ import annotations

import asyncio
from typing import Optional

import numpy as np

from ..base import BaseSTT, STTResult, EngineConfig, EngineState, EngineError
from ...config import get_config
from ...logging_config import get_session_logger


class FasterWhisperEngine(BaseSTT):
    """本地 Faster-Whisper 语音识别引擎"""

    def __init__(self):
        super().__init__()
        self._config: EngineConfig | None = None
        self._model = None
        self._buffer: bytearray = bytearray()
        self._cancel = asyncio.Event()

    async def start(self, config: EngineConfig) -> None:
        self._config = config
        self._cancel.clear()
        self._buffer.clear()

        try:
            from faster_whisper import WhisperModel
        except ImportError:
            self._state = EngineState.ERROR
            get_session_logger("system").warning(
                "faster-whisper 未安装，STT 不可用。安装: pip install faster-whisper"
            )
            return

        model_size = config.extra.get("model_size", "small")
        device = config.extra.get("device", "cuda")
        compute_type = config.extra.get("compute_type", "float16")

        self._model = await asyncio.to_thread(
            WhisperModel, model_size, device=device, compute_type=compute_type
        )
        self._state = EngineState.READY

    async def feed(self, audio_chunk: bytes) -> Optional[STTResult]:
        if not self._model:
            raise EngineError("引擎未初始化", "faster_whisper")

        self._buffer.extend(audio_chunk)

        # 足够长度的音频触发识别（~2 秒）
        min_bytes = 16000 * 2 * 2  # 16kHz * 2s * 2 bytes/sample
        if len(self._buffer) < min_bytes:
            return None

        self._state = EngineState.RUNNING

        pcm = np.frombuffer(bytes(self._buffer), dtype=np.int16).astype(np.float32) / 32768.0
        self._buffer.clear()

        try:
            segments, info = await asyncio.to_thread(
                self._model.transcribe,
                pcm,
                language=self._config.extra.get("language") if self._config else None,
                beam_size=self._config.extra.get("beam_size", 5) if self._config else 5,
            )

            text = "".join(seg.text for seg in segments).strip()
            self._state = EngineState.READY

            return STTResult(text=text, language=info.language, confidence=info.language_probability)
        except Exception as e:
            self._state = EngineState.ERROR
            raise EngineError(f"识别失败: {e}", "faster_whisper", e)

    async def cancel(self) -> None:
        self._cancel.set()
        self._state = EngineState.CANCELLED

    async def reset(self) -> None:
        self._cancel.clear()
        self._buffer.clear()
        self._state = EngineState.IDLE

    async def health_check(self) -> bool:
        return self._model is not None
