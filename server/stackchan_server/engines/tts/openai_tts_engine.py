"""
OpenAI TTS 引擎 - 基于 BaseTTS 实现
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import httpx

from ..base import BaseTTS, EngineConfig, EngineState, EngineError
from ...config import get_config
from ...logging_config import get_session_logger


class OpenAITTSEngine(BaseTTS):
    """OpenAI TTS 语音合成引擎"""

    def __init__(self):
        super().__init__()
        self._config: EngineConfig | None = None
        self._http: httpx.AsyncClient | None = None
        self._cancel = asyncio.Event()

    async def start(self, config: EngineConfig) -> None:
        self._config = config
        self._cancel.clear()
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(60, connect=10),
            headers={"Authorization": f"Bearer {config.api_key}"},
        )
        self._state = EngineState.READY

    async def feed(self, text: str) -> AsyncIterator[bytes]:
        if not self._http:
            raise EngineError("引擎未初始化", "openai_tts")

        self._state = EngineState.RUNNING
        self._cancel.clear()

        voice = self._config.extra.get("voice", "alloy") if self._config else "alloy"
        endpoint = f"{self._config.endpoint.rstrip('/')}/audio/speech" if self._config else ""

        body = {
            "model": "tts-1",
            "input": text,
            "voice": voice,
            "response_format": "pcm",
            "speed": 1.0,
        }

        try:
            resp = await self._http.post(endpoint, json=body)
            if resp.status_code != 200:
                raise EngineError(f"HTTP {resp.status_code}", "openai_tts")

            pcm = resp.content
            # 分块输出（~50ms 每块，对齐固件播放节奏）
            chunk_size = 24000 * 2 // 20
            for i in range(0, len(pcm), chunk_size):
                if self._cancel.is_set():
                    self._state = EngineState.CANCELLED
                    return
                yield pcm[i:i + chunk_size]

            self._state = EngineState.READY
        except Exception as e:
            self._state = EngineState.ERROR
            raise EngineError(f"TTS 失败: {e}", "openai_tts", e)

    async def cancel(self) -> None:
        self._cancel.set()
        self._state = EngineState.CANCELLED

    async def reset(self) -> None:
        self._cancel.clear()
        self._state = EngineState.IDLE

    async def health_check(self) -> bool:
        try:
            if not self._http:
                return False
            r = await self._http.get(f"{self._config.endpoint.rstrip('/')}/models")
            return r.status_code == 200
        except Exception:
            return False
