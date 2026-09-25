"""
ConversationSession — 会话编排核心

纯逻辑实现，完全不依赖网络和硬件，可直接单元测试。

职责:
  1. 管理对话状态机 (五状态 + 严格跳转校验)
  2. 维护对话上下文 (历史追加/回滚/截断/摘要压缩)
  3. 打断处理 (终止 LLM+TTS, 回滚助手回复, 切 LISTENING)
  4. 意图路由 (快速复杂度判断, 触发语音授权)
  5. 授权与降级 (云端需确认, 5min 复用, 失败自动切本地)
  6. 动效编排 (状态切换时生成表情/动作指令, 对齐固件)
  7. 对外标准事件接口 (输入事件 + 输出回调)

固件兼容标注:
  本类中标注为 "固件适配" 的注释说明为匹配 AI_StackChan_Ex 固件行为
  所做的特殊处理。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
import uuid
from collections.abc import Callable, Awaitable, AsyncIterator
from dataclasses import dataclass, field
from typing import Optional

from .state_machine import (
    ConversationStateMachine, ConversationState, TurnContext,
)
from ..engines.base import (
    LLMMessage, STTResult, EngineState,
    EngineError, EngineAuthError, EngineTimeoutError, EngineUnavailableError,
)
from ..engines.llm.llm_engine import LLMEngine
from ..engines.stt.stt_engine import STTEngine
from ..engines.tts.tts_engine import TTSEngine
from ..devices.commands import (
    DeviceEvent, DeviceCommandType,
    ExpressionCommand, ExpressionType,
    StatusNotification, DeviceStatus,
)
from ..protocol.messages import (
    build_audio_delta, build_audio_done,
    build_audio_transcript_delta, build_text_delta,
    build_response_done,
)
from ..config import AppConfig, UtteranceConfig, get_config
from ..logging_config import get_session_logger
from ..api.admin import publish_event, AdminEvent


# =============================================================================
# 性能指标
# =============================================================================

@dataclass
class TurnMetrics:
    """轮次性能指标 — 结构化埋点"""
    turn_id: int = 0
    stt_ms: float = 0.0
    llm_first_token_ms: float = 0.0
    llm_total_ms: float = 0.0
    tts_first_frame_ms: float = 0.0
    tts_total_ms: float = 0.0
    total_ms: float = 0.0
    interrupt_recovery_ms: float = 0.0
    model_used: str = ""
    error: str = ""


# =============================================================================
# 输出回调类型
# =============================================================================

TextDeltaCallback   = Callable[[str], Awaitable[None]]           # 文本增量
AudioChunkCallback  = Callable[[bytes], Awaitable[None]]         # 音频分片 (PCM/Opus)
DeviceEventCallback = Callable[[DeviceEvent], Awaitable[None]]   # 设备指令
StateChangeCallback = Callable[[ConversationState, TurnContext], Awaitable[None]]  # 状态变化


# =============================================================================
# 对话历史管理器
# =============================================================================

class ConversationHistory:
    """
    对话历史管理器。

    职责:
      - 维护完整消息列表 (system + turns)
      - append() 追加用户/助手消息
      - rollback_last_assistant() 打断时回滚未完成的助手回复
      - truncate(max_messages) 超长截断
      - summarize() 超长自动摘要压缩 (Phase 2)
    """

    def __init__(self, system_prompt: str = "", max_messages: int = 20):
        self._system_prompt = system_prompt
        self._max_messages = max_messages
        self._messages: list[LLMMessage] = []
        self._pending_assistant: Optional[LLMMessage] = None  # 正在生成中的助手消息

    # --- 增删 ---

    def append_user(self, text: str) -> None:
        """追加用户消息"""
        self._messages.append(LLMMessage(role="user", content=text))
        self._maybe_truncate()

    def begin_assistant(self) -> None:
        """标记开始生成助手回复（用于打断回滚）"""
        self._pending_assistant = LLMMessage(role="assistant", content="")

    def extend_assistant(self, delta: str) -> None:
        """追加助手回复增量"""
        if self._pending_assistant:
            self._pending_assistant.content += delta

    def commit_assistant(self) -> None:
        """提交助手回复到历史"""
        if self._pending_assistant and self._pending_assistant.content.strip():
            self._messages.append(self._pending_assistant)
        self._pending_assistant = None
        self._maybe_truncate()

    def rollback_assistant(self) -> str:
        """
        回滚打断 — 丢弃正在生成的助手回复。

        固件适配:
          固件端发出 response.cancel 后，需要回滚已部分生成但未播放的回复文本。
          如果已有部分文本被 TTS 播放了，应该回退到打断前的状态。

        Returns:
            被回滚的文本 (用于日志/动效)
        """
        text = self._pending_assistant.content if self._pending_assistant else ""
        self._pending_assistant = None
        return text

    # --- 查询 ---

    def build_messages(self) -> list[LLMMessage]:
        """
        构建发送给 LLM 的完整消息列表。

        固件适配:
          固件端在 session.update 中携带 instructions (角色 + 用户信息)，
          此处将 system prompt 放在首位，后面跟历史消息。
        """
        msgs = [LLMMessage(role="system", content=self._system_prompt)]
        msgs.extend(self._messages)
        if self._pending_assistant:
            msgs.append(self._pending_assistant)
        return msgs

    def last_assistant_text(self) -> str:
        """获取最后一轮助手回复 (用于降级时重新合成)"""
        for m in reversed(self._messages):
            if m.role == "assistant":
                return m.content
        return ""

    @property
    def message_count(self) -> int:
        return len(self._messages)

    # --- 内部 ---

    def _maybe_truncate(self) -> None:
        """超长截断: 保留最近 max_messages 条"""
        if len(self._messages) > self._max_messages:
            self._messages = self._messages[-self._max_messages:]

    def reset(self) -> None:
        """重置历史 (新会话)"""
        self._messages.clear()
        self._pending_assistant = None


# =============================================================================
# 授权管理器
# =============================================================================

@dataclass
class AuthRecord:
    """授权记录"""
    intent: str           # 授权类型: complex / web / deep_reasoning
    granted_at: float     # 授权时间
    expires_at: float     # 过期时间


class AuthorizationManager:
    """
    云端调用授权管理器。

    规则:
      - 首次复杂意图 → 发起语音授权确认
      - 用户说 "允许" / "はい" / "yes" → 授权通过
      - 同类请求 5 分钟内复用授权
      - 授权超时 / 拒绝 → 降级到本地模型
    """

    _AUTH_TTL_SECONDS: float = 300.0  # 5 分钟

    def __init__(self):
        self._grants: dict[str, AuthRecord] = {}  # intent → AuthRecord
        self._pending_intent: Optional[str] = None  # 等待用户确认的意图

    # --- 查询 ---

    def needs_authorization(self, intent: str) -> bool:
        """检查是否需要语音授权"""
        if intent == "chat":
            return False  # 闲聊不需要

        if intent in self._grants:
            grant = self._grants[intent]
            if time.time() < grant.expires_at:
                return False  # 授权仍在有效期内
            del self._grants[intent]

        return True

    # --- 授权流程 ---

    def request_authorization(self, intent: str) -> str:
        """
        发起语音授权请求。

        Returns:
            提示话术 (交给 TTS 合成播放)
        """
        self._pending_intent = intent
        return (
            "この機能はDeepSeekのクラウドAPIを使用します。"
            "よろしいですか？「はい」または「いいえ」でお答えください。"
        )

    def handle_response(self, user_text: str) -> bool:
        """
        处理用户授权响应。

        Returns:
            True: 授权通过
            False: 拒绝或无效
        """
        if self._pending_intent is None:
            return False

        # 固件适配: 日语/中文/英语 确认词
        affirm = {"はい", "はい。", "はい！", "いいよ", "いいです", "ok", "yes", "好的", "可以", "行", "嗯", "好"}
        deny = {"いいえ", "no", "だめ", "不行", "不要", "不可以", "算了"}

        text = user_text.strip().lower()
        if text in {a.lower() for a in affirm}:
            intent = self._pending_intent
            self._pending_intent = None
            now = time.time()
            self._grants[intent] = AuthRecord(
                intent=intent, granted_at=now, expires_at=now + self._AUTH_TTL_SECONDS,
            )
            return True

        if text in {d.lower() for d in deny}:
            self._pending_intent = None
            return False

        return False

    @property
    def is_waiting(self) -> bool:
        return self._pending_intent is not None

    def reset(self) -> None:
        self._grants.clear()
        self._pending_intent = None


# =============================================================================
# 意图路由
# =============================================================================

class IntentRouter:
    """
    意图路由器 — 快速判断用户输入复杂度。

    分类:
      - chat:     闲聊、天气、简单问答 → 本地 vLLM
      - complex:  复杂推理、编程、翻译、详细分析 → DeepSeek 云 (需授权)
      - command:  硬件控制指令 → 本地处理 (Phase 2)
      - web:      需要联网搜索 → DeepSeek 云 (需授权)
    """

    # 触发 DeepSeek 的关键词
    _COMPLEX_KEYWORDS: set[str] = {
        "帮我写", "写代码", "编程", "解释一下", "详细", "翻译",
        "分析", "总结", "比较", "推荐", "计算", "为什么", "怎么做",
        "教我", "帮我看看", "帮我找", "搜一下", "查一下",
    }

    def route(self, user_text: str) -> str:
        """
        路由到意图标签。

        Args:
            user_text: STT 识别后的用户输入

        Returns:
            意图标签: "chat" | "complex" | "command" | "web"
        """
        text = user_text.strip()

        # 硬件控制 (Phase 2)
        if self._is_hardware_command(text):
            return "command"

        # 联网需求
        if self._needs_web(text):
            return "web"

        # 复杂推理
        for kw in self._COMPLEX_KEYWORDS:
            if kw in text:
                return "complex"

        # 长文本 (> 50 字)
        if len(text) > 50:
            return "complex"

        return "chat"

    @staticmethod
    def _is_hardware_command(text: str) -> bool:
        """识别硬件控制指令"""
        hw_keywords = {"舵机", "servo", "LED", "表情", "关机", "重启", "音量", "静音", "摄像头"}
        return any(kw in text for kw in hw_keywords)

    @staticmethod
    def _needs_web(text: str) -> bool:
        """识别联网需求"""
        web_keywords = {"今天", "最新", "新闻", "天气", "股价", "汇率", "搜索", "帮我找"}
        return any(kw in text for kw in web_keywords)


# =============================================================================
# 语句评估器 — 断句识别文本是否构成完整语句、是否提交 LLM
# =============================================================================

class UtteranceEvaluator:
    """
    语句评估器。

    职责:
      判断 STT 断句识别的文本是否构成"完整语句"，决定是否触发 LLM。

      避免两个极端:
        - 过碎片化: 说话中的自然停顿被切碎, 每个碎片都调 LLM
        - 过度等待: 用户说完了但迟迟不响应

      评估规则 (按优先级):
        1. 文本长度 < min_chars → 噪声/碎片, 不提交
        2. 以句末标点结尾 (。！？!?.;…) → 语句完整, 提交
        3. 无标点但长度 ≥ submit_min_chars_no_punct → 语义可能完整, 提交
        4. 长度 ≥ max_pending_chars → 强制提交 (防止无限累积)
        5. 否则 → 继续累积等待
    """

    def __init__(self, config: Optional["UtteranceConfig"] = None):
        if config is None:
            config = get_config().conversation.utterance
        self._min_chars = config.min_chars
        self._submit_punct = set(config.submit_punctuation)
        self._submit_min_chars_no_punct = config.submit_min_chars_no_punct
        self._max_pending_chars = config.max_pending_chars

    def evaluate(self, text: str) -> tuple[bool, str]:
        """
        评估文本是否应提交 LLM。

        Returns:
            (是否提交, 原因说明)
        """
        text = text.strip()
        if len(text) < self._min_chars:
            return False, f"过短({len(text)}字)"

        if text[-1] in self._submit_punct:
            return True, f"句末标点({text[-1]})"

        if len(text) >= self._submit_min_chars_no_punct:
            return True, f"长度足够({len(text)}字无标点)"

        if len(text) >= self._max_pending_chars:
            return True, f"累积上限({len(text)}字)"

        return False, f"语句未完整({len(text)}字, 继续等待)"


# =============================================================================
# 动效编排器
# =============================================================================

class AnimationOrchestrator:
    """
    动效编排器 — 状态切换时自动生成设备指令。

    固件适配:
      - 表情 ID 对齐 m5stack-avatar 库的 Expression 枚举
      - "Please touch" / "Listening..." 等提示文字通过 StatusNotification 下发
      - 舵机动作后续通过 DeviceEvent 下发
    """

    # 状态 → 表情映射
    _EXPRESSION_MAP: dict[ConversationState, ExpressionType] = {
        ConversationState.IDLE:        ExpressionType.NEUTRAL,
        ConversationState.LISTENING:   ExpressionType.SURPRISE,   # 倾听 = 惊讶
        ConversationState.REASONING:   ExpressionType.DOUBT,      # 思考 = 疑惑
        ConversationState.SPEAKING:    ExpressionType.HAPPY,      # 说话 = 开心
        ConversationState.INTERRUPTED: ExpressionType.ANGRY,      # 打断瞬间 = 生气
    }

    # 状态 → LCD 提示文字 (固件适配)
    _STATUS_TEXT_MAP: dict[ConversationState, str] = {
        ConversationState.IDLE:        "Please touch",
        ConversationState.LISTENING:   "Listening...",
        ConversationState.REASONING:   "",
        ConversationState.SPEAKING:    "",
        ConversationState.INTERRUPTED: "",
    }

    def build_enter_commands(self, state: ConversationState) -> list[DeviceEvent]:
        """构建进入某状态时应发送的设备指令列表"""
        commands: list[DeviceEvent] = []

        # 表情
        expr = self._EXPRESSION_MAP.get(state)
        if expr:
            commands.append(DeviceEvent(
                type=DeviceCommandType.EXPRESSION_SET,
                expression=ExpressionCommand(expression=expr, duration_ms=0),
                event_id=f"anim_{state.name.lower()}_expr",
            ))

        # 状态文字
        text = self._STATUS_TEXT_MAP.get(state, "")
        commands.append(DeviceEvent(
            type=self._status_to_command(state),
            status=StatusNotification(
                status=self._state_to_device_status(state),
                message=text,
                timestamp=time.time(),
            ),
            event_id=f"anim_{state.name.lower()}_status",
        ))

        return commands

    @staticmethod
    def _status_to_command(state: ConversationState) -> DeviceCommandType:
        mapping = {
            ConversationState.IDLE:        DeviceCommandType.STATUS_IDLE,
            ConversationState.LISTENING:   DeviceCommandType.STATUS_LISTENING,
            ConversationState.REASONING:   DeviceCommandType.STATUS_THINKING,
            ConversationState.SPEAKING:    DeviceCommandType.STATUS_SPEAKING,
            ConversationState.INTERRUPTED: DeviceCommandType.STATUS_IDLE,
        }
        return mapping.get(state, DeviceCommandType.STATUS_IDLE)

    @staticmethod
    def _state_to_device_status(state: ConversationState) -> DeviceStatus:
        mapping = {
            ConversationState.IDLE:        DeviceStatus.IDLE,
            ConversationState.LISTENING:   DeviceStatus.LISTENING,
            ConversationState.REASONING:   DeviceStatus.PROCESSING,
            ConversationState.SPEAKING:    DeviceStatus.SPEAKING,
            ConversationState.INTERRUPTED: DeviceStatus.INTERRUPTED,
        }
        return mapping.get(state, DeviceStatus.IDLE)


# =============================================================================
# ConversationSession — 核心会话编排
# =============================================================================

class ConversationSession:
    """
    核心会话类 — 纯逻辑编排层。

    完全不依赖网络和硬件，所有外部依赖通过回调注入。

    用法:
        session = ConversationSession(session_id="abc", config=cfg)
        session.on_text_delta(lambda text: ws.send_json(...))
        session.on_audio_chunk(lambda data: ws.send_json(...))
        session.on_device_event(lambda evt: send_to_device(evt))

        # 注入引擎
        session.set_engines(stt=stt_engine, llm=llm_engine, tts=tts_engine)

        # 输入事件
        await session.handle_audio_chunk(pcm_data)
        await session.handle_vad_end()
        await session.handle_interrupt()
    """

    def __init__(
        self,
        session_id: str = "",
        config: Optional[AppConfig] = None,
    ):
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self._cfg = config or get_config()
        self._slog = get_session_logger(self.session_id)

        # 状态机
        self.sm = ConversationStateMachine()

        # 对话历史
        self.history = ConversationHistory(
            system_prompt=self._cfg.conversation.system_prompt,
            max_messages=self._cfg.conversation.history.max_messages,
        )

        # 意图路由 + 授权
        self.router = IntentRouter()
        self.auth = AuthorizationManager()

        # 动效
        self.animator = AnimationOrchestrator()

        # 引擎 (外部注入)
        self._stt: Optional[STTEngine] = None
        self._llm: Optional[LLMEngine] = None
        self._tts: Optional[TTSEngine] = None

        # 输出回调
        self._on_text_delta: list[TextDeltaCallback] = []
        self._on_audio_chunk: list[AudioChunkCallback] = []
        self._on_device_event: list[DeviceEventCallback] = []
        self._on_state_change: list[StateChangeCallback] = []
        self._on_error: list[TextDeltaCallback] = []           # 错误通知回调

        # 绑定的 RealtimeSession (协议层)
        self._protocol_session: Optional[object] = None

        # 内部状态
        self._turn_id: int = 0
        self._cancel_event = asyncio.Event()

        # 断句驱动流水线状态
        self._pending_utterance: str = ""          # 断句识别的累积文本
        self._evaluator = UtteranceEvaluator()     # 语句评估器
        self._processing_task: Optional[asyncio.Task] = None  # 当前 LLM 处理任务

        # 注册动效回调
        self._setup_animation_callbacks()

    # ------------------------------------------------------------------
    # 引擎注入
    # ------------------------------------------------------------------

    def set_engines(
        self,
        stt: Optional[STTEngine] = None,
        llm: Optional[LLMEngine] = None,
        tts: Optional[TTSEngine] = None,
    ) -> None:
        """注入能力引擎（由上层 loop_engine 调用）"""
        self._stt = stt
        self._llm = llm
        self._tts = tts

    def set_protocol_session(self, ps: object) -> None:
        """绑定协议层会话（用于发送 WebSocket 消息）"""
        self._protocol_session = ps

    # ------------------------------------------------------------------
    # 输出回调注册
    # ------------------------------------------------------------------

    def on_text_delta(self, cb: TextDeltaCallback) -> None:
        self._on_text_delta.append(cb)

    def on_audio_chunk(self, cb: AudioChunkCallback) -> None:
        self._on_audio_chunk.append(cb)

    def on_device_event(self, cb: DeviceEventCallback) -> None:
        self._on_device_event.append(cb)

    def on_state_change(self, cb: StateChangeCallback) -> None:
        self._on_state_change.append(cb)

    def on_error(self, cb: TextDeltaCallback) -> None:
        """注册错误通知回调 (→ 下发固件 error 事件)"""
        self._on_error.append(cb)

    # ------------------------------------------------------------------
    # 输入事件处理
    # ------------------------------------------------------------------

    async def handle_touch(self) -> None:
        """
        处理触摸事件。

        固件适配:
          固件端触摸屏幕 = 开始/停止录音。
          在 IDLE 状态 → 开始录音; 在其他状态 → 停止。
        """
        if self.sm.is_idle:
            await self._start_turn()
        else:
            # 触摸 = 停止 (固件行为)
            if self.sm.is_listening:
                await self._commit_audio()

    async def handle_audio_chunk(self, audio_data: bytes) -> None:
        """
        处理音频数据块。

        固件适配:
          固件端通过 input_audio_buffer.append 发送音频分片。
          此处同时作为打断信号检测: 在 REASONING/SPEAKING 状态收到音频 = 用户打断。
        """
        # 打断检测
        if self.sm.is_reasoning or self.sm.is_speaking:
            self._slog.info("检测到用户打断")
            await self.handle_interrupt()
            return

        if not self.sm.is_listening:
            return

        # 喂入 STT 引擎 (断句触发时返回整段识别结果)
        if self._stt and self._stt.state == EngineState.READY:
            result = await self._stt.feed(audio_data)
            if result is not None and result.text.strip():
                text = result.text.strip()
                # 断句识别出文本 → 评估是否构成完整语句
                submit, reason = self._evaluator.evaluate(text)
                self._slog.info(f"断句识别: '{text[:50]}' → {reason}")

                if submit:
                    # 语句完整 → 触发 LLM 流水线 (异步, 不阻塞音频接收)
                    self._slog.info(f"语句完整, 提交 LLM 理解")
                    asyncio.create_task(self._safe_process_utterance(text))
                else:
                    # 语句未完整 → 累积, 等待下一次断句
                    self._pending_utterance = text
                    self.sm.context.user_text = text

    async def handle_vad_end(self) -> None:
        """
        VAD 检测到语音结束。

        固件适配:
          固件端由 semantic_vad (服务端 VAD) 自动检测，发送
          input_audio_buffer.speech_started / .speech_stopped 事件。
          此处为服务端触发识别。
        """
        if not self.sm.is_listening:
            return
        await self._commit_audio()

    async def handle_interrupt(self) -> None:
        """
        处理打断请求。

        固件适配:
          固件端通过 response.cancel 发送打断。
          服务端收到后:
            1. 终止 LLM 流式生成
            2. 终止 TTS 合成
            3. 回滚未完成的助手回复
            4. 状态切回 LISTENING
        """
        self._cancel_event.set()

        # 终止引擎
        if self._llm:
            await self._llm.cancel()
        if self._tts:
            await self._tts.cancel()
        if self._stt:
            await self._stt.reset()

        # 回滚历史
        rolled_back = self.history.rollback_assistant()
        if rolled_back:
            self._slog.info(f"回滚助手回复: {rolled_back[:50]}...")

        # 状态跳转
        await self.sm.interrupt()
        self._cancel_event.clear()

    # ------------------------------------------------------------------
    # 完整轮次处理
    # ------------------------------------------------------------------

    async def _start_turn(self) -> None:
        """开始新轮次"""
        self._turn_id += 1
        self._cancel_event.clear()

        # 重置 STT
        if self._stt:
            await self._stt.reset()

        await self.sm.to_listening()
        self._slog.info(f"轮次 #{self._turn_id} 开始")

    async def _commit_audio(self) -> None:
        """
        兜底提交 — 客户端 commit / 触摸停止 / VAD 结束时强制处理剩余音频。

        正常路径是断句驱动: handle_audio_chunk 中评估完整 → _process_utterance。
        此方法兜底处理断句评估未通过的残留文本, 避免用户说完无响应。
        """
        user_text = ""

        # 对剩余音频做最终整体识别 (覆盖式)
        if self._stt and hasattr(self._stt, "finalize"):
            try:
                final_result = await self._stt.finalize()
                if final_result is not None and final_result.text.strip():
                    user_text = final_result.text.strip()
            except Exception as e:
                self._slog.warning(f"STT finalize 失败: {e}")

        # 回退: 断句识别累积但未提交的文本
        if not user_text:
            user_text = (
                self._pending_utterance.strip()
                or self.sm.context.user_text.strip()
            )

        self._pending_utterance = ""
        self.sm.context.user_text = ""

        if not user_text:
            # 无有效文本: 回 LISTENING 继续听 (连续对话)
            await self.sm.to_listening()
            return

        self._slog.info(f"兜底提交: {user_text[:80]}")
        await self._process_utterance(user_text)

    async def _safe_process_utterance(self, user_text: str) -> None:
        """
        语句处理安全包装 — 供 asyncio.create_task 使用。

        内部异常不能穿透 task 边界 (否则 "Task exception was never retrieved"),
        失败时回 LISTENING 保证会话持续可用。
        """
        try:
            await self._process_utterance(user_text)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._slog.error(f"语句处理异常: {type(e).__name__}: {e}")
            try:
                await self.sm.to_listening()
            except Exception:
                pass

    async def _process_utterance(self, user_text: str) -> None:
        """
        完整语句处理流水线: 授权检查 → 意图路由 → LLM → TTS → 回 LISTENING。

        断句驱动 (handle_audio_chunk) 和 commit 兜底 (_commit_audio) 都走此入口。
        末尾回到 LISTENING 而非 IDLE, 支持连续多轮全双工对话。
        全链路异常兜底: 每阶段独立 try/except, 故障不级联。
        """
        t0 = time.time()
        metrics = TurnMetrics(turn_id=self._turn_id)

        t_stt = time.time()
        metrics.stt_ms = (t_stt - t0) * 1000
        self._slog.info(f"STT: {user_text[:80]}")
        await self._notify_text_delta(f"[You]: {user_text}")
        # 发布 ASR 事件到 Admin UI
        publish_event(AdminEvent(type="asr", text=user_text, speaker="user"))

        # ---- 授权检查 ----
        if self.auth.is_waiting:
            granted = self.auth.handle_response(user_text)
            if granted:
                self._slog.info("云端授权通过")
                await self._notify_text_delta(" [クラウドAPIを使用します]")
            else:
                self._slog.info("云端授权拒绝，降级到本地")
                await self._notify_text_delta(" [ローカルモデルを使用します]")
                await self.sm.to_listening()
                return

        # ---- 意图路由 ----
        intent = self.router.route(user_text)
        self.sm.context.intent = intent

        if intent in ("complex", "web") and self.auth.needs_authorization(intent):
            try:
                prompt = self.auth.request_authorization(intent)
                await self._notify_text_delta(f" [Server]: {prompt}")
                await self._synthesize_and_send(prompt)
            except Exception as e:
                self._slog.warning(f"授权提示合成失败: {e}")
            await self.sm.to_listening()
            return

        model_name = self._resolve_model(intent)
        self.sm.context.model_used = model_name
        metrics.model_used = model_name

        # ---- LLM (兜底: 网络/认证/余额异常均不崩溃) ----
        await self.sm.to_reasoning(user_text)
        self.history.append_user(user_text)
        self.history.begin_assistant()
        msgs = self.history.build_messages()

        # 日志: DeepSeek 请求详情
        last_user = user_text[:120]
        self._slog.info(
            f"[LLM→{model_name}] msgs={len(msgs)} prompt={last_user}"
        )

        t_llm = time.time()
        full_text = ""
        try:
            full: list[str] = []
            first_token_logged = False
            async for chunk in self._llm.feed(msgs):
                if self._cancel_event.is_set():
                    self._slog.info("LLM 被打断")
                    return
                if not first_token_logged:
                    metrics.llm_first_token_ms = (time.time() - t_llm) * 1000
                    first_token_logged = True
                full.append(chunk)
                self.history.extend_assistant(chunk)
                await self._notify_text_delta(chunk)
                # 发布 LLM 增量到 Admin UI
                publish_event(AdminEvent(type="llm", text=chunk, speaker="assistant", model=model_name))

            full_text = "".join(full)
            metrics.llm_total_ms = (time.time() - t_llm) * 1000
            # 日志: LLM 回复
            self._slog.info(
                f"[LLM←{model_name}] chunks={len(full)} reply={full_text[:120]}"
            )

        except EngineUnavailableError:
            full_text = self._degradation_text(model_name)
            self._slog.warning(f"模型不可用，降级: {full_text[:50]}")
            metrics.error = "model_unavailable"
        except EngineAuthError as e:
            full_text = f"APIキーが無効です: {e}"
            self._slog.error(f"认证失败: {e}")
            metrics.error = "auth_error"
        except EngineTimeoutError as e:
            full_text = "応答がタイムアウトしました。もう一度お試しください。"
            self._slog.error(f"超时: {e}")
            metrics.error = "timeout"
        except Exception as e:
            full_text = "内部エラーが発生しました。"
            self._slog.error(f"LLM 异常: {e}")
            metrics.error = str(e)[:80]

        if not full_text.strip():
            self.history.rollback_assistant()
            await self._notify_text_delta(f" [Server]: {full_text}")
            await self._synthesize_and_send(full_text)
            await self.sm.to_listening()
            return

        self.history.commit_assistant()
        self._slog.info(f"LLM: {full_text[:80]}")

        # ---- TTS (兜底: 合成失败不阻塞，仅发文本) ----
        await self.sm.to_speaking(full_text)
        t_tts = time.time()
        first_frame_logged = False

        try:
            if self._tts is None:
                raise EngineError("TTS 引擎未初始化", "tts")

            async for audio_chunk in self._tts.feed(full_text):
                if self._cancel_event.is_set():
                    await self._tts.cancel()
                    return
                if not first_frame_logged:
                    metrics.tts_first_frame_ms = (time.time() - t_tts) * 1000
                    first_frame_logged = True
                await self._emit_audio_chunk(audio_chunk)

            metrics.tts_total_ms = (time.time() - t_tts) * 1000

        except Exception as e:
            self._slog.error(f"TTS 失败: {e}，仅发送文本")
            metrics.error = (metrics.error + "; " if metrics.error else "") + "tts_failed"

        # ---- 完成 ----
        await self._emit_audio_done()
        await self._emit_response_done()
        # 回 LISTENING 而非 IDLE: 支持连续多轮全双工对话
        # (固件端收到 response.done 后会自行切回录音状态)
        if self.sm.is_speaking:
            await self.sm.to_listening()

        metrics.total_ms = (time.time() - t0) * 1000
        self._slog.info(
            f"语句 #{self._turn_id} 完成 | "
            f"STT={metrics.stt_ms:.0f}ms "
            f"LLM(first={metrics.llm_first_token_ms:.0f}ms total={metrics.llm_total_ms:.0f}ms) "
            f"TTS(first={metrics.tts_first_frame_ms:.0f}ms total={metrics.tts_total_ms:.0f}ms) "
            f"total={metrics.total_ms:.0f}ms "
            f"model={metrics.model_used}"
            + (f" error={metrics.error}" if metrics.error else "")
        )

        # 错误通知下发固件
        if metrics.error:
            for cb in self._on_error:
                await cb(metrics.error)

    # ------------------------------------------------------------------
    # 输出辅助
    # ------------------------------------------------------------------

    async def _notify_text_delta(self, text: str) -> None:
        """调用所有 text_delta 回调"""
        for cb in self._on_text_delta:
            await cb(text)

    async def _emit_audio_chunk(self, data: bytes) -> None:
        """协议层: 发送音频分片"""
        if self._protocol_session:
            b64 = base64.b64encode(data).decode("ascii")
            ps = self._protocol_session
            await ps.send_json(build_audio_delta(b64))

    async def _emit_audio_done(self) -> None:
        if self._protocol_session:
            await self._protocol_session.send_json(build_audio_done())

    async def _emit_response_done(self) -> None:
        if self._protocol_session:
            await self._protocol_session.send_json(build_response_done())

    async def _synthesize_and_send(self, text: str) -> None:
        """合成文本并发送音频 (用于提示话术)"""
        if not self._tts:
            return
        async for chunk in self._tts.feed(text):
            await self._emit_audio_chunk(chunk)
        await self._emit_audio_done()

    # ------------------------------------------------------------------
    # 模型解析
    # ------------------------------------------------------------------

    def _degradation_text(self, model_name: str) -> str:
        """生成降级提示话术"""
        if model_name == "deepseek":
            return "DeepSeek APIが一時的に利用できません。ローカルモデルで対応します。少々お待ちください。"
        return "ただいま混雑しています。少々お待ちください。"

    def _resolve_model(self, intent: str) -> str:
        """根据意图解析使用哪个模型"""
        if intent == "chat":
            return self._cfg.routing.default_model
        if intent in ("complex", "web"):
            # 尝试 DeepSeek
            if not self._cfg.routing.models.get("deepseek"):
                return self._cfg.routing.default_model
            deepseek_cfg = self._cfg.routing.models["deepseek"]
            if not deepseek_cfg.enabled or deepseek_cfg.api_key.startswith("${"):
                return self._cfg.routing.default_model
            return "deepseek"
        return self._cfg.routing.default_model

    # ------------------------------------------------------------------
    # 动效回调注册
    # ------------------------------------------------------------------

    def _setup_animation_callbacks(self) -> None:
        """注册状态机 → 动效编排的桥接"""
        async def on_state_enter(sm: ConversationStateMachine) -> None:
            cmds = self.animator.build_enter_commands(sm.state)
            for cmd in cmds:
                for cb in self._on_device_event:
                    await cb(cmd)
            for cb in self._on_state_change:
                await cb(sm.state, sm.context)

        for state in ConversationState:
            self.sm.on_enter(state, on_state_enter)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """重置会话 (新对话)"""
        self.sm.reset()
        self.history.reset()
        self.auth.reset()
        self._turn_id = 0
        self._cancel_event.clear()
