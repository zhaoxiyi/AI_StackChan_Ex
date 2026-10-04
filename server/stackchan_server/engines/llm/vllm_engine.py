"""
vLLM 本地引擎 - 基于 BaseLLM 实现
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import httpx

from ..base import BaseLLM, LLMMessage, EngineConfig, EngineState, EngineError, EngineTimeoutError
from ...config import get_config
from ...logging_config import get_session_logger


class VLLMEngine(BaseLLM):
    """本地 vLLM 模型引擎（OpenAI 兼容流式接口）"""

    def __init__(self):
        super().__init__()
        self._config: EngineConfig | None = None
        self._http: httpx.AsyncClient | None = None
        self._cancel = asyncio.Event()

    async def start(self, config: EngineConfig) -> None:
        self._config = config
        self._cancel.clear()
        self._http = httpx.AsyncClient(
            base_url=config.endpoint,
            timeout=httpx.Timeout(60, connect=10),
            headers={"Content-Type": "application/json"},
        )
        self._state = EngineState.READY

    async def feed(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.0,   # 0 = 用 config
        max_tokens: int = 0,        # 0 = 用 config (关键: 模型窗口适配, 不能硬编码)
    ) -> AsyncIterator[str]:
        if not self._http:
            raise EngineError("引擎未初始化", "vllm")

        # 从 config.extra 解析实际参数 (模型窗口适配, 不能硬编码)
        if max_tokens <= 0:
            max_tokens = (self._config.extra.get("max_tokens") or 512) if self._config else 512
        if temperature <= 0.0:
            temperature = (self._config.extra.get("temperature") or 0.7) if self._config else 0.7

        self._state = EngineState.RUNNING
        self._cancel.clear()

        body = {
            "model": self._config.model_name if self._config else "default",
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        try:
            endpoint = f"{self._config.endpoint.rstrip('/')}/chat/completions" if self._config else ""
            async with self._http.stream("POST", endpoint, json=body) as resp:
                if resp.status_code != 200:
                    raise EngineError(f"HTTP {resp.status_code}", "vllm")

                async for line in resp.aiter_lines():
                    if self._cancel.is_set():
                        await resp.aclose()
                        self._state = EngineState.CANCELLED
                        return

                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                        delta = chunk["choices"][0]["delta"].get("content", "")
                        if delta:
                            yield delta
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

            self._state = EngineState.READY
        except httpx.TimeoutException as e:
            self._state = EngineState.ERROR
            raise EngineTimeoutError("vLLM 超时", "vllm", e)

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
