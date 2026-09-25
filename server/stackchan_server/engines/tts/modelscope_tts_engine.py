"""
ModelScope TTS 引擎 — 内嵌式、中文优化、模型从 ModelScope 下载

基于 ModelScope speech_tts pipeline，特性:
  - 模型自动从 ModelScope 下载缓存，国内高速
  - 纯 CPU 可运行 (PyTorch)
  - 中文优化 (Sambert-HifiGAN 16kHz)
  - 流式分片返回 PCM 音频
  - 支持 cancel() / reset()
  - 零手动部署 (pip install modelscope torch)

继承: BaseTTS (engines/base.py)
"""

from __future__ import annotations

import asyncio
import io
import re
from typing import AsyncIterator, Optional

import numpy as np

from ..base import BaseTTS, EngineConfig, EngineState, EngineError
from ...logging_config import get_session_logger


class ModelScopeTTSEngine(BaseTTS):
    """
    基于 ModelScope 的内嵌式中文 TTS 引擎。

    config.extra 支持:
      - model: ModelScope 模型 ID
        "damo/speech_sambert-hifigan_tts_zh-cn_16k" (默认, 女声)
        "damo/speech_sambert-hifigan_tts_zh-cn_multispk_16k" (多说话人)
      - speaker: 说话人 (multispk 模型可用)，如 "zhitian_emo"
      - speed: 语速 0.5 ~ 2.0 (默认 1.0)
    """

    _DEFAULT_MODEL = "damo/speech_sambert-hifigan_tts_zh-cn_16k"
    _SENTENCE_BREAKS = re.compile(r"[。！？!?\n；;]+")

    def __init__(self):
        super().__init__()
        self._config: Optional[EngineConfig] = None
        self._pipeline = None
        self._cancel = asyncio.Event()
        self._sample_rate: int = 16000

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self, config: EngineConfig) -> None:
        slog = get_session_logger("system")
        self._config = config
        self._cancel.clear()

        try:
            from modelscope.pipelines import pipeline
            from modelscope.utils.constant import Tasks
        except ImportError:
            self._state = EngineState.ERROR
            slog.warning("modelscope 未安装。pip install modelscope torch")
            return

        model_id = config.extra.get("model", self._DEFAULT_MODEL)
        slog.info(f"[ModelScopeTTS] 下载模型: {model_id} (首次自动缓存到 ~/.cache/modelscope/)")

        try:
            self._pipeline = await asyncio.wait_for(
                asyncio.to_thread(
                    pipeline,
                    task=Tasks.text_to_speech,
                    model=model_id,
                ),
                timeout=180.0,  # 首次下载最多 3 分钟
            )
        except asyncio.TimeoutError:
            self._state = EngineState.ERROR
            slog.warning("ModelScope 模型下载超时 (180s)。请检查网络。")
            return
        except Exception as e:
            self._state = EngineState.ERROR
            slog.warning(f"ModelScope TTS 初始化失败: {e}")
            return

        self._state = EngineState.READY
        slog.info(f"[ModelScopeTTS] 就绪 ({model_id})")

    async def feed(self, text: str) -> AsyncIterator[bytes]:
        """
        流式合成语音。

        流程: 按标点分句 → 逐句调用 ModelScope TTS → yield PCM 16-bit 16kHz。

        Yields:
            bytes: PCM 16-bit 16kHz mono 音频块
        """
        if not text.strip() or not self._pipeline:
            return

        self._state = EngineState.RUNNING
        self._cancel.clear()

        sentences = self._split_sentences(text)
        speaker = self._config.extra.get("speaker", None) if self._config else None
        speed = self._config.extra.get("speed", 1.0) if self._config else 1.0

        for sentence in sentences:
            if self._cancel.is_set():
                self._state = EngineState.CANCELLED
                return

            sentence = sentence.strip()
            if not sentence:
                continue

            try:
                # ModelScope TTS 调用 (asyncio.to_thread 不阻塞事件循环)
                kwargs = {"input": sentence}
                if speaker:
                    kwargs["speaker"] = speaker
                result = await asyncio.to_thread(self._pipeline, **kwargs)

                # 提取 PCM 音频数据
                pcm_data = self._extract_pcm(result)
                if pcm_data:
                    # 按 ~50ms 分片输出
                    chunk_size = self._sample_rate * 2 // 20  # 50ms at 16kHz
                    for i in range(0, len(pcm_data), chunk_size):
                        if self._cancel.is_set():
                            return
                        yield pcm_data[i:i + chunk_size]

            except Exception as e:
                get_session_logger("system").warning(
                    f"[ModelScopeTTS] 句子合成失败: {sentence[:30]}... → {e}"
                )
                continue

        self._state = EngineState.READY

    async def cancel(self) -> None:
        self._cancel.set()
        self._state = EngineState.CANCELLED

    async def reset(self) -> None:
        self._cancel.clear()
        self._state = EngineState.READY

    async def health_check(self) -> bool:
        return self._pipeline is not None

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        result = []
        i = 0
        for m in ModelScopeTTSEngine._SENTENCE_BREAKS.finditer(text):
            end = m.start()
            if i < end:
                result.append(text[i:end] + m.group())
            elif result:
                result[-1] += m.group()
            i = m.end()
        if i < len(text):
            result.append(text[i:])
        return result if result else [text]

    def _extract_pcm(self, result) -> bytes:
        """
        从 ModelScope pipeline 输出中提取 PCM 16-bit 音频。

        ModelScope TTS pipeline 返回格式:
          {"output_wav": numpy.ndarray (float32, shape=(samples,))}
        或
          {"output_pcm": bytes}
        """
        if isinstance(result, dict):
            # 优先 wav 格式
            wav = result.get("output_wav")
            if wav is not None and isinstance(wav, np.ndarray):
                # float32 [-1,1] → int16
                wav = np.clip(wav, -1.0, 1.0)
                return (wav * 32767).astype(np.int16).tobytes()

            # 其次 pcm 格式
            pcm = result.get("output_pcm")
            if pcm is not None:
                if isinstance(pcm, bytes):
                    return pcm
                if isinstance(pcm, np.ndarray):
                    return pcm.astype(np.int16).tobytes()

        # numpy 数组直接返回
        if isinstance(result, np.ndarray):
            result = np.clip(result, -1.0, 1.0)
            return (result * 32767).astype(np.int16).tobytes()

        get_session_logger("system").warning(
            f"[ModelScopeTTS] 无法提取音频: type={type(result).__name__}"
        )
        return b""
