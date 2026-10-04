"""
对话状态机 — 严格五状态 + 进入/退出回调

状态转换图:
  IDLE ──→ LISTENING ──→ REASONING ──→ SPEAKING ──→ IDLE
    ↑                      │  ↑            │           │
    │                      ↓  │            ↓           │
    └──── INTERRUPTED ←────┘  └── INTERRUPTED ←────────┘

固件对齐:
  固件端通过 LCD 文字和 Avatar 表情反映状态:
    IDLE        → "Please touch" / 默认表情
    LISTENING   → "Listening..." / 监听表情
    REASONING   → 无文字 / 思考表情 (固件无此独立状态, 此处抽象)
    SPEAKING    → 无文字 / 说话表情
    INTERRUPTED → 瞬间状态, 固件不可见
"""

from __future__ import annotations

import time
from collections.abc import Callable, Awaitable
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


# =============================================================================
# 状态定义
# =============================================================================

class ConversationState(Enum):
    """五状态对话状态机"""
    IDLE = auto()          # 空闲，等待用户触摸
    LISTENING = auto()     # 录音中，接收音频流
    REASONING = auto()     # STT 完成，LLM 推理中
    SPEAKING = auto()      # TTS 合成完成，播放音频中
    INTERRUPTED = auto()   # 用户打断，瞬间过渡状态


# 合法状态跳转表
TRANSITIONS: dict[ConversationState, set[ConversationState]] = {
    ConversationState.IDLE:        {ConversationState.LISTENING},
    ConversationState.LISTENING:   {ConversationState.REASONING, ConversationState.IDLE},
    ConversationState.REASONING:   {ConversationState.SPEAKING, ConversationState.INTERRUPTED, ConversationState.IDLE, ConversationState.LISTENING},
    # SPEAKING → LISTENING: 断句驱动的连续对话 (回复完毕直接回到收听, 不等 commit)
    ConversationState.SPEAKING:    {ConversationState.IDLE, ConversationState.INTERRUPTED, ConversationState.LISTENING},
    ConversationState.INTERRUPTED: {ConversationState.LISTENING, ConversationState.IDLE},
}


# =============================================================================
# 回调类型
# =============================================================================

EnterCallback = Callable[["ConversationStateMachine"], Awaitable[None]]
ExitCallback  = Callable[["ConversationStateMachine"], Awaitable[None]]
TransitionCallback = Callable[["ConversationStateMachine", ConversationState, ConversationState], Awaitable[None]]


# =============================================================================
# 轮次上下文
# =============================================================================

@dataclass
class TurnContext:
    """当前轮次上下文 — 在状态间传递"""
    turn_id: str = ""
    user_text: str = ""          # STT 识别结果
    assistant_text: str = ""     # LLM 回复文本 (累积中)
    intent: str = "chat"         # 意图标签: chat / complex / command / web
    model_used: str = "vllm"     # 实际使用的模型
    is_interrupted: bool = False
    state_entered_at: float = 0.0
    latency_stt_ms: float = 0.0
    latency_llm_ms: float = 0.0
    latency_tts_ms: float = 0.0


# =============================================================================
# 状态机
# =============================================================================

class ConversationStateMachine:
    """
    对话状态机 — 纯逻辑，无 I/O。

    用法:
        sm = ConversationStateMachine()
        sm.on_enter(ConversationState.SPEAKING, lambda: set_expression("happy"))
        await sm.transition_to(ConversationState.LISTENING)

    固件适配说明:
      - IDLE 对应固件的 "Please touch" 状态
      - LISTENING 对应固件的录音 + "Listening..." 显示
      - REASONING 是服务器侧概念，固件端表现为沉默等待
      - SPEAKING 对应固件的音频播放状态
      - INTERRUPTED 是瞬间状态，只触发回调，不会停留
    """

    def __init__(self):
        self._state: ConversationState = ConversationState.IDLE
        self.context: TurnContext = TurnContext()

        # 进入/离开回调: {state: [callbacks]}
        self._on_enter: dict[ConversationState, list[EnterCallback]] = {
            s: [] for s in ConversationState
        }
        self._on_exit: dict[ConversationState, list[ExitCallback]] = {
            s: [] for s in ConversationState
        }
        # 全局转换回调: [(callback,)]
        self._on_transition: list[TransitionCallback] = []

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def state(self) -> ConversationState:
        return self._state

    @property
    def is_idle(self) -> bool:
        return self._state == ConversationState.IDLE

    @property
    def is_listening(self) -> bool:
        return self._state == ConversationState.LISTENING

    @property
    def is_reasoning(self) -> bool:
        return self._state == ConversationState.REASONING

    @property
    def is_speaking(self) -> bool:
        return self._state == ConversationState.SPEAKING

    # ------------------------------------------------------------------
    # 回调注册
    # ------------------------------------------------------------------

    def on_enter(self, state: ConversationState, cb: EnterCallback) -> None:
        """注册状态进入回调（可多次注册）"""
        self._on_enter[state].append(cb)

    def on_exit(self, state: ConversationState, cb: ExitCallback) -> None:
        """注册状态离开回调"""
        self._on_exit[state].append(cb)

    def on_transition(self, cb: TransitionCallback) -> None:
        """注册全局状态转换回调"""
        self._on_transition.append(cb)

    # ------------------------------------------------------------------
    # 状态跳转
    # ------------------------------------------------------------------

    async def transition_to(
        self, target: ConversationState, **ctx_updates,
    ) -> bool:
        """
        执行状态跳转。

        Args:
            target: 目标状态
            **ctx_updates: 上下文更新 (如 user_text="你好")

        Returns:
            True 跳转成功

        Raises:
            ValueError: 非法跳转
        """
        if target not in TRANSITIONS.get(self._state, set()):
            msg = f"非法状态跳转: {self._state.name} → {target.name}"
            import logging
            logging.getLogger("state_machine").warning(msg)
            raise ValueError(msg)

        old = self._state

        # 离开回调 (先离开旧状态)
        for cb in self._on_exit[old]:
            try:
                await cb(self)
            except Exception as e:
                import logging
                logging.getLogger("state_machine").warning(f"exit 回调失败: {e}")

        # 状态切换
        self._state = target
        self.context.state_entered_at = time.time()

        # 上下文更新
        for k, v in ctx_updates.items():
            if hasattr(self.context, k):
                setattr(self.context, k, v)

        # 全局转换回调
        for cb in self._on_transition:
            try:
                await cb(self, old, target)
            except Exception as e:
                import logging
                logging.getLogger("state_machine").warning(f"transition 回调失败: {e}")

        # 进入回调 (后进入新状态)
        for cb in self._on_enter[target]:
            try:
                await cb(self)
            except Exception as e:
                import logging
                logging.getLogger("state_machine").warning(f"enter 回调失败: {e}")

        return True

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------

    async def to_listening(self) -> None:
        """
        开始收听 — 从 SPEAKING 进入时自动经过 INTERRUPTED。

        幂等: 已在 LISTENING 时直接返回 (重连恢复场景防御)。
        """
        if self._state == ConversationState.LISTENING:
            return
        if self._state == ConversationState.SPEAKING:
            await self.transition_to(ConversationState.INTERRUPTED, is_interrupted=True)
            await self.transition_to(ConversationState.LISTENING)
        else:
            await self.transition_to(ConversationState.LISTENING)

    async def to_reasoning(self, user_text: str = "") -> None:
        """开始推理"""
        await self.transition_to(ConversationState.REASONING, user_text=user_text)

    async def to_speaking(self, assistant_text: str = "") -> None:
        """开始播放"""
        await self.transition_to(ConversationState.SPEAKING, assistant_text=assistant_text)

    async def to_idle(self) -> None:
        """回到空闲"""
        await self.transition_to(ConversationState.IDLE)

    async def interrupt(self) -> None:
        """
        用户打断 — 从 REASONING 或 SPEAKING 进入 INTERRUPTED。

        固件适配:
          固件端检测到新语音 = 服务端收到 input_audio_buffer.append
          在 reasoning/speaking 状态 → 触发 interrupt
        """
        if self._state in (ConversationState.REASONING, ConversationState.SPEAKING):
            await self.transition_to(ConversationState.INTERRUPTED, is_interrupted=True)
            await self.transition_to(ConversationState.LISTENING)

    def reset(self) -> None:
        """硬重置 (用于异常恢复)"""
        self._state = ConversationState.IDLE
        self.context = TurnContext()
