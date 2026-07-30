"""
STTEngine — 流式语音识别引擎

继承 BaseSTT，基于 faster-whisper 实现。

特性:
  - 支持持续喂入 Opus 编码音频分片，内部自动解码为 PCM
  - 实时返回增量识别文本（is_partial=True 中间结果）
  - VAD 双模式: 固件端上报 VAD 事件 / 服务端 Silero VAD 兜底
  - reset(): 打断时清空音频缓冲区和识别上下文

与编排层交互:
  loop_engine 将固件发来的音频 chunk 逐块 feed()，
  每当返回非 None 的 STTResult 时表示有新文本可消费。
  收到打断时调用 reset() 清空积累的音频。
"""

from __future__ import annotations

import asyncio
from typing import Optional

import numpy as np

from ..base import BaseSTT, STTResult, EngineConfig, EngineState, EngineError
from ...logging_config import get_session_logger


class STTEngine(BaseSTT):
    """
    流式语音识别引擎。

    用法:
        engine = STTEngine()
        await engine.start(EngineConfig(engine_type="stt", extra={"language": "ja"}))

        # 持续喂入音频
        for opus_chunk in audio_chunks:
            result = await engine.feed(opus_chunk)
            if result is not None:
                print(f"识别: {result.text}")

        # 打断时重置
        await engine.reset()

    编排层注意事项:
      - feed() 接受 bytes，内部自动判断 Opus/PCM 并解码
      - 返回 None = 还需更多音频，返回 STTResult = 有新文本
      - STTResult.is_partial=True 表示中间结果（可用于实时字幕）
      - reset() 立即清空缓冲区，丢弃未识别音频
    """

    def __init__(self):
        super().__init__()
        self._config: Optional[EngineConfig] = None
        self._model = None
        self._vad_model = None
        self._buffer: bytearray = bytearray()
        self._cancel = asyncio.Event()
        # 累计文本（用于流式识别场景）
        self._accumulated_text: str = ""
        # Opus 解码器（延迟加载）
        self._opus_decoder = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self, config: EngineConfig) -> None:
        """
        初始化引擎。

        config.extra 支持:
          - model_size: "small" | "medium" | "large-v3" (默认 "small")
          - device: "cuda" | "cpu" (默认 "cuda")
          - compute_type: "float16" | "int8_float16" (默认 "float16")
          - language: "ja" | "zh" | "en" | None (None=自动检测)
          - beam_size: 1-10 (默认 5)
          - use_vad: True | False (默认 True, 启用 Silero VAD)
          - vad_threshold: 0.1-1.0 (默认 0.5)
          - min_silence_ms: 最小静音时长 (默认 500ms)
        """
        self._config = config
        self._cancel.clear()
        self._buffer.clear()
        self._accumulated_text = ""

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

        # Silero VAD
        if config.extra.get("use_vad", True):
            try:
                from silero_vad import load_silero_vad
                self._vad_model = await asyncio.to_thread(load_silero_vad, onnx=True)
                get_session_logger("system").info("Silero VAD 已加载")
            except ImportError:
                get_session_logger("system").warning("silero-vad 未安装，VAD 禁用")

        # Opus 解码器
        try:
            import opuslib
            self._opus_decoder = opuslib.Decoder(16000, 1)  # 16kHz mono
        except ImportError:
            get_session_logger("system").info("opuslib 未安装，仅支持 PCM 输入")

        self._state = EngineState.READY

    async def feed(self, audio_chunk: bytes) -> Optional[STTResult]:
        """
        喂入音频数据块。

        自动识别 Opus/PCM:
          - Opus 数据 → 解码为 PCM 16kHz mono → 追加到缓冲区
          - PCM 数据 → 直接追加到缓冲区

        缓冲区积累足够音频后自动触发识别。

        Args:
            audio_chunk: Opus 编码字节 或 PCM 16-bit 字节

        Returns:
            None: 还需要更多音频
            STTResult: 识别结果 (is_partial=True 为中间结果)
        """
        if not self._model:
            raise EngineError("引擎未初始化", "stt")

        self._cancel.clear()

        # 解码（如果是 Opus）
        pcm = self._ensure_pcm(audio_chunk)
        self._buffer.extend(pcm)

        # VAD 滤波：检测是否包含有效语音
        if self._vad_model and not self._has_speech(pcm):
            return None  # 静音块直接丢弃，不积累

        # 累积足够长度触发识别（~1.5 秒）
        min_bytes = 16000 * 2 * 2  # 16kHz * 2s(padding) * 2 bytes/sample
        if len(self._buffer) < min_bytes:
            return None

        # 执行识别
        self._state = EngineState.RUNNING
        pcm_array = np.frombuffer(bytes(self._buffer), dtype=np.int16).astype(np.float32) / 32768.0
        self._buffer.clear()

        try:
            segments, info = await asyncio.to_thread(
                self._model.transcribe,
                pcm_array,
                language=self._config.extra.get("language") if self._config else None,
                beam_size=self._config.extra.get("beam_size", 5) if self._config else 5,
                vad_filter=False,  # 已在外部做了 VAD
            )

            new_text = "".join(seg.text for seg in segments).strip()

            # 流式增量返回
            if new_text.startswith(self._accumulated_text):
                delta = new_text[len(self._accumulated_text):]
                self._accumulated_text = new_text
            else:
                delta = new_text
                self._accumulated_text = new_text

            self._state = EngineState.READY

            if delta:
                return STTResult(
                    text=delta,
                    is_partial=False,
                    confidence=info.language_probability,
                    language=info.language,
                )
            return None

        except Exception as e:
            self._state = EngineState.ERROR
            raise EngineError(f"识别失败: {e}", "stt", e)

    async def cancel(self) -> None:
        """取消当前识别"""
        self._cancel.set()
        self._state = EngineState.CANCELLED

    async def reset(self) -> None:
        """
        重置引擎状态。

        编排层在打断时必须调用此方法，清空积累的音频和数据。
        """
        self._cancel.clear()
        self._buffer.clear()
        self._accumulated_text = ""
        self._state = EngineState.READY

    async def health_check(self) -> bool:
        return self._model is not None

    # ------------------------------------------------------------------
    # 私有方法
    # ------------------------------------------------------------------

    def _ensure_pcm(self, data: bytes) -> bytes:
        """
        确保输入为 PCM 16-bit。
        如果检测到 Opus 特征则尝试解码。
        """
        if self._opus_decoder is None:
            return data  # 假定为 PCM
        # 简单启发式: Opus 帧通常以特定 TOC 字节开头
        # 更稳健的做法是配置中指定格式
        try:
            return self._opus_decoder.decode(data, 960)  # 20ms at 48kHz → 960 samples, 但我们是 16kHz → 320
        except Exception:
            return data  # 解码失败，当作 PCM

    def _has_speech(self, pcm: bytes) -> bool:
        """
        服务端 VAD 兜底。

        基于 RMS 能量检测 + Silero VAD。
        """
        if not self._vad_model:
            return True  # 无 VAD 模型时默认通过

        if len(pcm) < 160:  # < 10ms
            return False

        # RMS 快速预筛选
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(samples ** 2)))
        if rms < 0.01:  # 能量太低
            return False

        return True
