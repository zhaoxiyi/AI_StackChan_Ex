"""
LLMEngine — 双后端统一大语言模型引擎

继承 BaseLLM，对外暴露完全一致的流式聊天接口。

后端:
  - vLLM (本地):   POST /v1/chat/completions (OpenAI 兼容流式)
  - DeepSeek (云): POST /v1/chat/completions (官方流式)

特性:
  - 双后端统一封装，调用方无需感知具体后端
  - DeepSeek 余额检测: 每小时缓存，预警阈值 + 耗尽阈值
  - cancel(): 主动关闭 HTTP 连接，立即终止生成
  - 统一异常类型，上层只捕获 EngineError 及其子类

与编排层交互:
  loop_engine 调用 feed(messages) 获取 AsyncIterator[str]，
  收到打断时调用 cancel()，错误统一通过 EngineError 向上抛。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncIterator, Optional

import httpx

from ..base import (
    BaseLLM, LLMMessage, EngineConfig, EngineState,
    EngineError, EngineTimeoutError, EngineAuthError, EngineUnavailableError,
)
from ...logging_config import get_session_logger


# =============================================================================
# DeepSeek 余额管理器
# =============================================================================

class BalanceChecker:
    """
    DeepSeek 余额检测器。

    特性:
      - 每小时缓存一次余额，避免频繁 API 调用
      - 两级阈值: warning_threshold / critical_threshold
      - 临界阈值触发后 is_depleted=True，所有请求自动降级
    """

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "https://api.deepseek.com",
        warning_threshold: float = 10.0,
        critical_threshold: float = 2.0,
        cache_ttl: float = 3600.0,
    ):
        self._api_key = api_key
        self._base_url = base_url
        self._warning = warning_threshold
        self._critical = critical_threshold
        self._cache_ttl = cache_ttl

        self._balance: Optional[float] = None
        self._last_check: float = 0.0
        self._depleted: bool = False

    @property
    def is_depleted(self) -> bool:
        return self._depleted

    @property
    def balance(self) -> Optional[float]:
        return self._balance

    async def check(self) -> dict:
        """
        查询余额，自动缓存。

        Returns:
            {"balance": float|None, "status": "ok"|"warning"|"critical"|"unknown", "message": str}
        """
        now = time.time()
        if self._balance is not None and (now - self._last_check) < self._cache_ttl:
            return self._build_status()

        slog = get_session_logger("system")
        self._last_check = now

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(15, connect=5),
                headers={"Authorization": f"Bearer {self._api_key}"},
            ) as client:
                r = await client.get(f"{self._base_url.rstrip('/')}/user/balance")
                if r.status_code == 200:
                    data = r.json()
                    infos = data.get("balance_infos", [])
                    if infos:
                        total = float(infos[0].get("total_balance", "0"))
                    else:
                        total = float(data.get("total_balance", "0"))
                    self._balance = total
                    slog.info(f"[DeepSeek] 余额: {total} CNY")
                else:
                    slog.warning(f"[DeepSeek] 余额查询 HTTP {r.status_code}")
                    self._balance = None
        except Exception as e:
            slog.warning(f"[DeepSeek] 余额查询失败: {e}")
            self._balance = None

        # 阈值判定
        if self._balance is not None and self._balance <= self._critical:
            self._depleted = True

        return self._build_status()

    def _build_status(self) -> dict:
        if self._balance is None:
            return {"balance": None, "status": "unknown", "message": "无法查询余额"}
        if self._balance <= self._critical:
            return {"balance": self._balance, "status": "critical", "message": f"余额极低 ({self._balance} CNY)，已自动降级"}
        if self._balance <= self._warning:
            return {"balance": self._balance, "status": "warning", "message": f"余额偏低 ({self._balance} CNY)，建议充值"}
        return {"balance": self._balance, "status": "ok", "message": f"余额充足 ({self._balance} CNY)"}


# =============================================================================
# LLMEngine
# =============================================================================

class LLMEngine(BaseLLM):
    """
    双后端统一 LLM 引擎。

    用法:
        engine = LLMEngine()
        await engine.start(EngineConfig(...))

        # 流式生成
        async for text_chunk in engine.feed(messages):
            await send_to_tts(text_chunk)

        # 打断
        await engine.cancel()

    编排层注意事项:
      - feed() 返回的是文本增量 (str)，编排层负责拼接和分句
      - cancel() 幂等，重复调用安全
      - EngineUnavailableError 表示后端不可用（余额不足等），编排层应降级
    """

    def __init__(self):
        super().__init__()
        self._config: Optional[EngineConfig] = None
        self._http: Optional[httpx.AsyncClient] = None
        self._cancel_event: asyncio.Event = asyncio.Event()
        self._balance_checker: Optional[BalanceChecker] = None
        # 后端类型: "vllm" | "deepseek"
        self._backend: str = "vllm"

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self, config: EngineConfig) -> None:
        """
        初始化引擎。

        config.extra 支持:
          - backend: "vllm" | "deepseek" (默认 "vllm")
          - warning_threshold_cny: DeepSeek 预警阈值
          - critical_threshold_cny: DeepSeek 耗尽阈值
        """
        self._config = config
        self._cancel_event.clear()
        self._backend = config.extra.get("backend", "vllm")

        # 构建通用 HTTP 客户端
        headers = {"Content-Type": "application/json"}
        if config.api_key and config.api_key != "not-needed":
            headers["Authorization"] = f"Bearer {config.api_key}"

        self._http = httpx.AsyncClient(
            base_url=config.endpoint,
            timeout=httpx.Timeout(120.0, connect=15.0),
            headers=headers,
        )

        # DeepSeek 余额检测
        if self._backend == "deepseek":
            self._balance_checker = BalanceChecker(
                api_key=config.api_key,
                base_url=config.endpoint,
                warning_threshold=config.extra.get("warning_threshold_cny", 10.0),
                critical_threshold=config.extra.get("critical_threshold_cny", 2.0),
            )
            # 启动时检查一次余额
            await self._balance_checker.check()

        self._state = EngineState.READY

    async def feed(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> AsyncIterator[str]:
        """
        流式生成对话回复。

        Args:
            messages: 完整对话历史（含 system prompt）
            temperature: 采样温度
            max_tokens: 最大生成 token 数

        Yields:
            str: 文本增量块

        Raises:
            EngineUnavailableError: 后端不可用（余额耗尽 / 服务离线）
            EngineAuthError: API Key 无效
            EngineTimeoutError: 请求超时
            EngineError: 其他错误
        """
        if not self._http:
            raise EngineError("引擎未初始化", self._backend)

        # DeepSeek 余额预检
        if self._balance_checker and self._balance_checker.is_depleted:
            raise EngineUnavailableError(
                f"DeepSeek 余额耗尽 (≤{self._balance_checker._critical} CNY)，请充值",
                self._backend,
            )

        self._state = EngineState.RUNNING
        self._cancel_event.clear()

        body = {
            "model": self._config.model_name if self._config else "",
            "messages": self._serialize_messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }

        endpoint = f"{self._config.endpoint.rstrip('/')}/chat/completions" if self._config else ""

        try:
            async with self._http.stream("POST", endpoint, json=body) as resp:
                await self._check_response(resp)
                async for line in resp.aiter_lines():
                    # 取消检查
                    if self._cancel_event.is_set():
                        await resp.aclose()
                        self._state = EngineState.CANCELLED
                        return

                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:].strip()
                    if data_str == "[DONE]":
                        break

                    chunk = self._parse_chunk(data_str)
                    if chunk:
                        yield chunk

            self._state = EngineState.READY

        except httpx.TimeoutException as e:
            self._state = EngineState.ERROR
            raise EngineTimeoutError(f"{self._backend} 请求超时", self._backend, e)
        except (EngineError, EngineAuthError, EngineUnavailableError, EngineTimeoutError):
            self._state = EngineState.ERROR
            raise
        except Exception as e:
            self._state = EngineState.ERROR
            raise EngineError(f"{self._backend} 未知错误: {e}", self._backend, e)

    async def cancel(self) -> None:
        """
        取消当前生成。

        实现: 设置取消标志 → HTTP 流的 aiter_lines 循环检测到 → 关闭连接。
        编排层应在检测到打断时立即调用此方法。
        """
        self._cancel_event.set()
        self._state = EngineState.CANCELLED

    async def reset(self) -> None:
        """重置引擎到就绪状态（清理取消标志）"""
        self._cancel_event.clear()
        self._state = EngineState.READY if self._http else EngineState.IDLE

    async def health_check(self) -> bool:
        """通过 /v1/models 端点检查后端连通性"""
        if not self._http:
            return False
        try:
            r = await self._http.get(f"{self._config.endpoint.rstrip('/')}/models")
            return r.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # DeepSeek 余额查询（编排层可主动调用）
    # ------------------------------------------------------------------

    async def get_balance_status(self) -> dict:
        """
        查询 DeepSeek 余额状态。

        编排层可用此方法获取余额信息，决定是否路由到 DeepSeek。

        Returns:
            {"balance": float|None, "status": "ok"|"warning"|"critical"|"unknown", "message": str}
        """
        if self._balance_checker is None:
            return {"balance": None, "status": "unknown", "message": "非 DeepSeek 后端"}
        return await self._balance_checker.check()

    # ------------------------------------------------------------------
    # 私有方法
    # ------------------------------------------------------------------

    @staticmethod
    def _serialize_messages(messages: list[LLMMessage]) -> list[dict]:
        """将 LLMMessage 列表转为 OpenAI 兼容的 dict 列表"""
        return [
            {"role": m.role, "content": m.content}
            for m in messages
        ]

    async def _check_response(self, resp: httpx.Response) -> None:
        """检查 HTTP 响应状态，抛对应的 EngineError 子类"""
        if resp.status_code == 200:
            return
        if resp.status_code in (401, 403):
            raise EngineAuthError(f"{self._backend} 认证失败 (HTTP {resp.status_code})", self._backend)
        if resp.status_code == 402:
            if self._balance_checker:
                self._balance_checker._depleted = True
            raise EngineUnavailableError(f"{self._backend} 余额不足 (HTTP 402)", self._backend)
        if resp.status_code == 429:
            raise EngineUnavailableError(f"{self._backend} 请求限流 (HTTP 429)", self._backend)
        body = await resp.aread()
        raise EngineError(
            f"{self._backend} HTTP {resp.status_code}: {body.decode(errors='replace')[:300]}",
            self._backend,
        )

    @staticmethod
    def _parse_chunk(data_str: str) -> Optional[str]:
        """解析 SSE chunk，提取 delta content"""
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            return None
        choices = data.get("choices", [])
        if not choices:
            return None
        delta = choices[0].get("delta", {})
        return delta.get("content")  # None 或 str
