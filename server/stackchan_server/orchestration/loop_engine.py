"""Loop Engine - 会话编排核心"""

from __future__ import annotations

import asyncio
import base64
from typing import Optional

from ..engines.base import (
    BaseLLM, BaseSTT, BaseTTS, LLMMessage,
    EngineConfig, EngineState, EngineError, EngineUnavailableError,
)
from ..engines.llm.llm_engine import LLMEngine
from ..engines.stt.stt_engine import STTEngine
from ..engines.tts.tts_engine import TTSEngine
from ..protocol.session import RealtimeSession
from ..protocol.messages import (
    build_audio_delta, build_audio_done,
    build_audio_transcript_delta, build_text_delta,
    build_response_done,
)
from ..config import get_config, AppConfig
from ..logging_config import get_session_logger
from .state_machine import ConversationStateMachine, ConversationState


class LoopEngine:
    """会话编排引擎 - 串联 STT/LLM/TTS 流水线"""

    def __init__(self, config: AppConfig | None = None):
        self._cfg = config or get_config()
        self._stt: Optional[STTEngine] = None
        self._tts: Optional[TTSEngine] = None
        # LLM 池: {"vllm": LLMEngine, "deepseek": LLMEngine}
        self._llm_pool: dict[str, LLMEngine] = {}
        self._history: list[LLMMessage] = []

    async def initialize(self) -> None:
        """延迟初始化所有引擎（单个失败不影响整体）"""
        slog = get_session_logger("system")

        # --- LLM 池 ---
        for name, entry in self._cfg.routing.models.items():
            if not entry.enabled:
                continue
            try:
                eng = LLMEngine()
                extra = {}
                extra["max_tokens"] = entry.max_tokens      # 模型窗口适配 (如 MiniCPM-o 窗口 2048)
                extra["temperature"] = entry.temperature
                if name == "deepseek":
                    extra["backend"] = "deepseek"
                    if entry.api_key.startswith("${"):
                        slog.warning(f"DeepSeek API Key 未设置（占位符: {entry.api_key}），跳过")
                        continue
                await eng.start(EngineConfig(
                    engine_type="llm",
                    model_name=entry.model_name,
                    endpoint=entry.base_url,
                    api_key=entry.api_key,
                    extra=extra,
                ))
                self._llm_pool[name] = eng
                slog.info(f"LLM {name} ({entry.model_name}) 就绪")
            except Exception as e:
                slog.warning(f"LLM {name} 启动失败: {e}")

        # --- STT ---
        stt_cfg = self._cfg.stt
        self._stt = STTEngine()
        try:
            await self._stt.start(EngineConfig(
                engine_type="stt",
                extra={
                    "model_size": stt_cfg.faster_whisper.model_size,
                    "device": stt_cfg.faster_whisper.device,
                    "compute_type": stt_cfg.faster_whisper.compute_type,
                    "language": stt_cfg.faster_whisper.language,
                    "beam_size": stt_cfg.faster_whisper.beam_size,
                },
            ))
            slog.info("STT 就绪")
        except Exception as e:
            slog.warning(f"STT 启动失败: {e}")

        # --- TTS ---
        tts_cfg = self._cfg.tts
        self._tts = TTSEngine()
        try:
            await self._tts.start(EngineConfig(
                engine_type="tts",
                extra={"voice": tts_cfg.openai_tts.voice},
            ))
            slog.info("TTS 就绪")
        except Exception as e:
            slog.warning(f"TTS 启动失败: {e}")

    async def process_turn(
        self, session: RealtimeSession, sm: ConversationStateMachine,
    ) -> None:
        """处理一个完整对话轮次"""
        slog = get_session_logger(session.session_id)

        await sm.to_listening()
        await asyncio.sleep(0.1)

        pcm_data = session.drain_audio()
        if len(pcm_data) < 1600:
            await sm.to_idle()
            return

        # STT
        if self._stt is None or self._stt.state != EngineState.READY:
            slog.warning("STT 不可用")
            await sm.to_idle()
            return
        await sm.to_reasoning()
        slog.info("STT 识别中...")
        result = await self._stt.feed(pcm_data)
        if result is None or not result.text.strip():
            await sm.to_idle()
            return

        user_text = result.text
        slog.info(f"识别: {user_text[:80]}")

        # 路由
        model_name = self._route(user_text)
        slog.info(f"路由: {model_name}")
        sm.context.model_used = model_name

        llm = self._llm_pool.get(model_name)
        if llm is None:
            await sm.to_idle()
            return

        # LLM
        system = self._cfg.conversation.system_prompt
        messages = [
            LLMMessage(role="system", content=system),
            *self._history,
            LLMMessage(role="user", content=user_text),
        ]

        full = ""
        try:
            async for chunk in llm.feed(messages):
                if sm.state == ConversationState.INTERRUPTED:
                    slog.info("LLM 被打断")
                    await llm.cancel()
                    break
                full += chunk
                if session.session_config and "text" in session.session_config.output_modalities:
                    await session.send_json(build_text_delta(chunk))

            if not full.strip():
                await sm.to_idle()
                return

            self._history.append(LLMMessage(role="user", content=user_text))
            self._history.append(LLMMessage(role="assistant", content=full))
            max_n = self._cfg.conversation.history.max_messages
            if len(self._history) > max_n:
                self._history = self._history[-max_n:]

            slog.info(f"LLM: {full[:80]}")
        except EngineUnavailableError:
            slog.warning("LLM 不可用，降级到 vLLM")
            fallback = self._llm_pool.get("vllm")
            if fallback:
                async for chunk in fallback.feed(messages):
                    full += chunk
                self._history.append(LLMMessage(role="assistant", content=full))

        # TTS + 发送音频
        if self._tts is None or self._tts.state != EngineState.READY:
            slog.warning("TTS 不可用，仅发送文本")
            await session.send_json(build_response_done())
            await sm.to_idle()
            return

        await sm.to_speaking(full)
        slog.info("TTS 合成中...")
        async for pcm_chunk in self._tts.feed(full):
            if sm.state == ConversationState.INTERRUPTED:
                await self._tts.cancel()
                break
            b64 = base64.b64encode(pcm_chunk).decode("ascii")
            await session.send_json(build_audio_delta(b64))

        await session.send_json(build_audio_done())
        await session.send_json(build_response_done())
        await sm.to_idle()
        slog.info("轮次完成")

    def _route(self, text: str) -> str:
        triggers = ["帮我写", "翻译", "代码", "编程", "解释", "分析", "总结", "详细"]
        for t in triggers:
            if t in text:
                ds = self._llm_pool.get("deepseek")
                if ds and ds.state == EngineState.READY:
                    return "deepseek"
                return self._cfg.routing.default_model
        return self._cfg.routing.default_model


# 全局单例
_loop: Optional[LoopEngine] = None

async def get_loop_engine() -> LoopEngine:
    global _loop
    if _loop is None:
        _loop = LoopEngine()
        await _loop.initialize()
    return _loop
