"""
DeepSeek 云端引擎 - 基于 BaseLLM 实现，含余额检测
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncIterator

import httpx

from ..base import (
    BaseLLM, LLMMessage, EngineConfig, EngineState,
    EngineError, EngineTimeoutError, EngineAuthError, EngineUnavailableError,
)
from ...config import get_config
from ...logging_config import get_session_logger


class DeepSeekEngine(BaseLLM):
    """DeepSeek 云端引擎"""

    def __init__(self):
        super().__init__()
        self._config: EngineConfig | None = None
        self._http: httpx.AsyncClient | None = None
        self._cancel = asyncio.Event()
        self._balance_depleted = False

    async def start(self, config: EngineConfig) -> None:
        self._config = config
        self._cancel.clear()
        self._http = httpx.AsyncClient(
            base_url=config.endpoint,
            timeout=httpx.Timeout(120, connect=15),
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
        )
        self._state = EngineState.READY

    @property
    def is_available(self) -> bool:
        return not self._balance_depleted

    async def check_balance(self) -> float | None:
        """查询余额 (CNY)"""
        if not self._http:
            return None
        try:
            r = await self._http.get(f"{self._config.endpoint.rstrip('/')}/user/balance")
            if r.status_code == 200:
                data = r.json()
                infos = data.get("balance_infos", [])
                if infos:
                    return float(infos[0].get("total_balance", "0"))
                return float(data.get("total_balance", "0"))
        except Exception:
            pass
        return None

    async def feed(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> AsyncIterator[str]:
        if self._balance_depleted:
            raise EngineUnavailableError("DeepSeek 余额不足", "deepseek")

        if not self._http:
            raise EngineError("引擎未初始化", "deepseek")

        self._state = EngineState.RUNNING
        self._cancel.clear()

        body = {
            "model": self._config.model_name if self._config else "deepseek-chat",
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        try:
            endpoint = f"{self._config.endpoint.rstrip('/')}/chat/completions" if self._config else ""
            async with self._http.stream("POST", endpoint, json=body) as resp:
                if resp.status_code in (401, 403):
                    raise EngineAuthError(f"认证失败", "deepseek")
                if resp.status_code == 402:
                    self._balance_depleted = True
                    raise EngineUnavailableError("余额不足", "deepseek")
                if resp.status_code != 200:
                    raise EngineError(f"HTTP {resp.status_code}", "deepseek")

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
            raise EngineTimeoutError("DeepSeek 超时", "deepseek", e)
        except (EngineError, EngineAuthError, EngineUnavailableError):
            self._state = EngineState.ERROR
            raise

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
