"""
能力引擎层 - 抽象基类定义

三大引擎统一接口:
  - start(config):  初始化引擎（加载模型、建立连接）
  - feed(data):     喂入数据，返回异步迭代器
  - cancel():       中断当前任务
  - reset():        重置引擎状态

设计原则:
  - 纯能力封装，禁止包含业务逻辑
  - 不依赖网络或硬件细节（依赖注入）
  - 全部支持流式 + 取消
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional, AsyncIterator, Any


# =============================================================================
# 引擎状态枚举
# =============================================================================

class EngineState(Enum):
    """引擎生命周期状态"""
    IDLE = auto()         # 空闲，未初始化
    READY = auto()        # 已初始化，等待输入
    RUNNING = auto()      # 正在执行
    CANCELLED = auto()    # 已被取消
    ERROR = auto()        # 错误状态


# =============================================================================
# 通用数据结构
# =============================================================================

@dataclass
class EngineConfig:
    """引擎初始化配置"""
    engine_type: str = ""                     # llm / stt / tts
    model_name: str = ""                      # 模型名称
    endpoint: str = ""                        # API 端点
    api_key: str = ""                         # API 密钥
    extra: dict[str, Any] = field(default_factory=dict)  # 引擎特定参数


@dataclass
class EngineResult:
    """引擎执行结果"""
    ok: bool = True
    data: Any = None
    error: Optional[str] = None
    tokens_used: int = 0
    latency_ms: float = 0.0


# =============================================================================
# BaseLLM - 大语言模型引擎抽象
# =============================================================================

@dataclass
class LLMMessage:
    """对话消息"""
    role: str                                 # system / user / assistant / tool
    content: str
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[list[dict]] = None


class BaseLLM(ABC):
    """
    大语言模型引擎抽象基类。

    实现类: VLLMEngine, DeepSeekEngine

    生命周期:
        start(config) → [ feed(msgs) → cancel()? ] → reset()
    """

    def __init__(self):
        self._state: EngineState = EngineState.IDLE

    @property
    def state(self) -> EngineState:
        return self._state

    @abstractmethod
    async def start(self, config: EngineConfig) -> None:
        """
        初始化引擎。

        具体实现负责: 加载模型 / 验证 API Key / 建立 HTTP 连接池。

        Args:
            config: 引擎配置（模型名、端点、密钥等）
        """
        ...

    @abstractmethod
    async def feed(
        self,
        messages: list[LLMMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> AsyncIterator[str]:
        """
        流式生成对话回复。

        边生成边 yield 文本增量，调用方可以逐块消费。

        Args:
            messages: 完整对话历史（含 system prompt）
            temperature: 采样温度
            max_tokens: 最大生成 token 数

        Yields:
            str: 文本增量块
        """
        ...

    @abstractmethod
    async def cancel(self) -> None:
        """
        取消当前生成任务。

        实现要点: 设置取消标志 + 关闭 HTTP 流 + 状态置 CANCELLED。
        幂等: 重复调用安全。
        """
        ...

    @abstractmethod
    async def reset(self) -> None:
        """重置引擎到 IDLE 状态，清理中间结果"""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """健康检查，True 表示可用"""
        ...


# =============================================================================
# BaseSTT - 语音识别引擎抽象
# =============================================================================

@dataclass
class STTResult:
    """STT 识别结果"""
    text: str = ""
    is_partial: bool = False                  # True=中间结果, False=最终结果
    confidence: float = 0.0
    language: Optional[str] = None


class BaseSTT(ABC):
    """
    语音识别引擎抽象基类。

    实现类: FasterWhisperEngine

    生命周期:
        start(config) → [ feed(audio_chunk) → cancel()? ] → reset()

    feed() 返回 Optional[str]: None 表示还需要更多音频，非 None 表示识别完成。
    """

    def __init__(self):
        self._state: EngineState = EngineState.IDLE

    @property
    def state(self) -> EngineState:
        return self._state

    @abstractmethod
    async def start(self, config: EngineConfig) -> None:
        """
        初始化引擎（加载模型、VAD 模型等）。
        """
        ...

    @abstractmethod
    async def feed(self, audio_chunk: bytes) -> Optional[STTResult]:
        """
        喂入音频数据块（PCM 16-bit, 16kHz, mono）。

        积累到足够音频后自动触发识别。

        Args:
            audio_chunk: 原始 PCM 16-bit 音频字节

        Returns:
            None: 还需要更多音频
            STTResult: 识别完成，包含最终文本
        """
        ...

    @abstractmethod
    async def cancel(self) -> None:
        """取消当前识别"""
        ...

    @abstractmethod
    async def reset(self) -> None:
        """清空音频缓冲区，重置到 IDLE"""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """健康检查"""
        ...


# =============================================================================
# BaseTTS - 语音合成引擎抽象
# =============================================================================

class BaseTTS(ABC):
    """
    语音合成引擎抽象基类。

    实现类: OpenAITTSEngine

    生命周期:
        start(config) → [ feed(text) → cancel()? ] → reset()

    feed() 返回 AsyncIterator[bytes]: 逐块产出 PCM 音频。
    """

    def __init__(self):
        self._state: EngineState = EngineState.IDLE

    @property
    def state(self) -> EngineState:
        return self._state

    @abstractmethod
    async def start(self, config: EngineConfig) -> None:
        """
        初始化引擎（加载声码器、建立连接等）。
        """
        ...

    @abstractmethod
    async def feed(self, text: str) -> AsyncIterator[bytes]:
        """
        流式合成语音。

        Args:
            text: 要合成的完整文本

        Yields:
            bytes: PCM 16-bit 音频块
        """
        ...

    @abstractmethod
    async def cancel(self) -> None:
        """取消当前合成"""
        ...

    @abstractmethod
    async def reset(self) -> None:
        """重置引擎"""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """健康检查"""
        ...


# =============================================================================
# 引擎异常
# =============================================================================

class EngineError(Exception):
    """引擎通用异常"""

    def __init__(self, message: str, engine_type: str = "", cause: Optional[Exception] = None):
        super().__init__(message)
        self.engine_type = engine_type
        self.cause = cause


class EngineTimeoutError(EngineError):
    """引擎超时"""
    pass


class EngineAuthError(EngineError):
    """认证失败"""
    pass


class EngineUnavailableError(EngineError):
    """引擎不可用（余额不足、网络不通等）"""
    pass
