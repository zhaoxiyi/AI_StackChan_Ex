"""
CPU STT 引擎 — 纯 CPU 可运行、内嵌式、流式语音识别

基于 faster-whisper，特性:
  - 自动检测 CUDA，有 GPU 自动加速，无 GPU 降级 CPU
  - 模型首次运行自动从 HuggingFace 下载缓存，无需手动部署
  - 中文优化默认模型 (small)，兼顾英文
  - 流式分片 feed()，支持 cancel() / reset()
  - 所有耗时操作 asyncio.to_thread 封装，不阻塞事件循环
  - Silero VAD 集成，过滤静音

继承: BaseSTT (engines/base.py)
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

import numpy as np

from ..base import BaseSTT, STTResult, EngineConfig, EngineState, EngineError
from ...logging_config import get_session_logger


class CpuSTTEngine(BaseSTT):
    """
    CPU 友好的流式语音识别引擎。

    config.extra 支持:
      - model_size: "tiny" | "base" | "small" | "medium" (默认 "small")
        tiny=39M, base=74M, small=244M, medium=769M (越大越准但越慢)
      - language: "zh" | "en" | None (None=自动检测, 默认 None)
      - beam_size: 1-10 (默认 5)
      - device: "auto" | "cpu" | "cuda" (默认 "auto"=自动检测)
      - compute_type: "auto" | "float16" | "int8" | "int8_float16" (默认 "auto")
      - use_vad: True | False (默认 True)
      - silence_break_ms: 静音断句阈值 (默认 800ms)
      - max_audio_ms: 音频最长累积 (默认 15000ms, 超过则强制识别)

    识别策略 (累积式整体识别):
      持续累积所有音频，在以下时机用完整音频整体识别:
        1. 静音断句: 连续静音超过 silence_break_ms
        2. 过长截断: 累积音频超过 max_audio_ms
        3. 最终提交: 编排层调用 finalize() (commit 时)
      相比固定 2.2s 切片，完整音频识别有上下文连续性，准确率大幅提升。
    """

    def __init__(self):
        super().__init__()
        self._config: Optional[EngineConfig] = None
        self._model = None
        self._vad_model = None
        self._buffer: bytearray = bytearray()
        self._accumulated: str = ""
        self._cancel = asyncio.Event()
        # 静音断句状态
        self._silence_ms: float = 0.0          # 连续静音时长
        self._last_rms: float = 0.0            # 上一块 RMS
        self._chunk_ms: float = 125.0          # 单块时长 (默认 125ms)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self, config: EngineConfig) -> None:
        slog = get_session_logger("system")
        self._config = config
        self._cancel.clear()
        self._buffer.clear()
        self._accumulated = ""

        # 1. 检查依赖
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            self._state = EngineState.ERROR
            slog.warning("faster-whisper 未安装。pip install faster-whisper")
            return

        # 2. 设备检测
        device = config.extra.get("device", "auto")
        compute = config.extra.get("compute_type", "auto")
        if device == "auto":
            try:
                import torch
                device = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                device = "cpu"
        if compute == "auto":
            compute = "float16" if device == "cuda" else "int8"

        # 3. 模型配置
        model_size = config.extra.get("model_size", "small")
        model_source = config.extra.get("model_source", "huggingface")  # huggingface | modelscope | local
        model_name = config.extra.get("model_name", "")  # ModelScope 或自定义模型 ID

        slog.info(f"[CpuSTT] 模型: {model_size} source={model_source} device={device} ({compute})")

        # 4. 下载/定位模型
        model_path = await self._download_model(model_size, model_source, model_name, slog)

        # 5. 加载模型
        try:
            self._model = await asyncio.to_thread(
                WhisperModel, model_path, device=device, compute_type=compute
            )
        except Exception as e:
            self._state = EngineState.ERROR
            slog.warning(f"模型加载失败: {e}")
            return

        # 6. VAD
        if config.extra.get("use_vad", True):
            try:
                from silero_vad import load_silero_vad
                self._vad_model = await asyncio.to_thread(load_silero_vad, onnx=True)
                slog.info("[CpuSTT] Silero VAD 已加载")
            except ImportError:
                slog.info("[CpuSTT] silero-vad 未安装，VAD 禁用")

        self._state = EngineState.READY
        slog.info(f"[CpuSTT] 就绪 ({model_size} on {device})")

    async def feed(self, audio_chunk: bytes) -> Optional[STTResult]:
        """
        喂入 PCM 16-bit 16kHz mono 音频。

        累积式整体识别:
          1. 计算本块 RMS，更新静音计时
          2. 音频全部累积到 buffer (不按固定时长切分)
          3. 触发时机:
             a. 连续静音 > silence_break_ms → 断句识别
             b. 累积 > max_audio_ms → 过长截断识别
          4. 识别使用完整累积音频，返回整段文本 (覆盖式)

        Returns:
            None: 继续累积
            STTResult: 完整段识别结果
        """
        if not self._model:
            self._slog("STT 未就绪 (model=None)")
            return None

        self._cancel.clear()

        # 1. 计算 RMS
        rms_val = float(np.sqrt(np.mean(
            np.frombuffer(audio_chunk, dtype=np.int16).astype(np.float32) ** 2
        )))

        # 2. 实际块时长 (从字节数反推)
        chunk_ms = len(audio_chunk) / 32.0  # 16kHz * 2 bytes = 32 bytes/ms
        self._chunk_ms = chunk_ms

        # 3. 更新静音计时
        if rms_val < self._silence_rms_threshold:
            self._silence_ms += chunk_ms
        else:
            self._silence_ms = 0.0

        # 4. 累积音频
        self._buffer.extend(audio_chunk)
        buf_ms = len(self._buffer) / 32.0

        # 5. 触发条件判断
        silence_break = self._config.extra.get("silence_break_ms", 800) if self._config else 800
        max_audio = self._config.extra.get("max_audio_ms", 15000) if self._config else 15000

        trigger = None
        if self._silence_ms >= silence_break and buf_ms > 400:
            trigger = f"静音断句 ({self._silence_ms:.0f}ms)"
        elif buf_ms >= max_audio:
            trigger = f"过长截断 ({buf_ms:.0f}ms)"

        if trigger is None:
            return None

        # 6. 整体识别
        self._slog(f"触发识别: {trigger} buf={buf_ms:.1f}s RMS={rms_val:.0f}")
        return await self._transcribe_buffer(is_final=False)

    async def finalize(self) -> Optional[STTResult]:
        """
        最终识别 — 编排层在 audio.commit 时调用。

        用剩余完整音频做最终识别，返回最终文本。
        """
        if len(self._buffer) < 3200:  # < 100ms 忽略
            self._buffer.clear()
            self._silence_ms = 0.0
            return None
        self._slog(f"最终识别: buf={len(self._buffer)/32.0:.1f}s")
        return await self._transcribe_buffer(is_final=True)

    async def _transcribe_buffer(self, is_final: bool) -> Optional[STTResult]:
        """对当前累积的完整音频执行识别"""
        self._state = EngineState.RUNNING
        pcm = np.frombuffer(bytes(self._buffer), dtype=np.int16).astype(np.float32) / 32768.0
        self._buffer.clear()
        self._silence_ms = 0.0

        try:
            segments, info = await asyncio.to_thread(
                self._model.transcribe,
                pcm,
                language=self._config.extra.get("language") if self._config else None,
                beam_size=self._config.extra.get("beam_size", 5) if self._config else 5,
                vad_filter=False,
            )

            new_text = "".join(seg.text for seg in segments).strip()
            self._slog(f"识别结果: {new_text[:80]} (lang={info.language} p={info.language_probability:.2f})")

            # 整段识别结果 (覆盖式, 不是增量)
            self._accumulated = new_text
            self._state = EngineState.READY

            if new_text:
                return STTResult(
                    text=new_text, is_partial=not is_final,
                    confidence=info.language_probability, language=info.language,
                )
            return None

        except Exception as e:
            self._state = EngineState.ERROR
            self._slog(f"识别异常: {e}")
            raise EngineError(f"识别失败: {e}", "cpu_stt", e)

    @property
    def _silence_rms_threshold(self) -> float:
        """静音 RMS 阈值 (比 VAD 阈值略低, 用于断句而非过滤)"""
        return 120.0

    async def cancel(self) -> None:
        self._cancel.set()
        self._state = EngineState.CANCELLED

    async def reset(self) -> None:
        self._cancel.clear()
        self._buffer.clear()
        self._accumulated = ""
        # 仅在模型已加载时恢复 READY，否则保持原状态 (避免假 READY)
        if self._model is not None:
            self._state = EngineState.READY

    async def health_check(self) -> bool:
        return self._model is not None

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _download_model(self, model_size: str, source: str, model_name: str, slog) -> str:
        """
        下载/定位 faster-whisper CTranslate2 模型。

        source:
          - "huggingface" (默认): 自动下载 Systran/faster-whisper-{model_size}
            通过 HF_ENDPOINT=hf-mirror.com 镜像加速
          - "modelscope": 通过 modelscope.snapshot_download 下载，
            model_name 指定 ModelScope 模型 ID (可逗号分隔多个候选)，
            下载后校验 CTranslate2 格式 (model.bin)，不兼容自动换下一个候选
          - "local": 使用本地路径 model_name (已下载的模型目录)

        Returns: 模型路径 (传给 WhisperModel)
        """
        if source == "local":
            if not model_name:
                raise EngineError("model_name 未指定本地路径", "cpu_stt")
            slog.info(f"[CpuSTT] 本地模型: {model_name}")
            return model_name

        if source == "modelscope":
            local = await self._download_from_modelscope(model_name, slog)
            if local:
                return local
            slog.warning("[CpuSTT] 所有 ModelScope 候选失败，回退 HuggingFace")

        # 默认 HuggingFace
        if not os.environ.get("HF_ENDPOINT"):
            os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
        slog.info(f"[CpuSTT] HuggingFace: Systran/faster-whisper-{model_size}")
        return model_size

    async def _download_from_modelscope(self, model_name: str, slog) -> Optional[str]:
        """
        从 ModelScope 下载 CTranslate2 格式的 faster-whisper 模型。

        特性:
          - 支持逗号分隔的候选 ID 列表，依次尝试
          - 下载后校验 model.bin 是否存在 (CTranslate2 格式特征)
          - 环境变量 MODELSCOPE_STT_MODEL 优先级最高

        Returns:
            本地模型路径，全部失败返回 None
        """
        # 候选列表: 环境变量 > 配置 model_name > 默认候选
        candidates: list[str] = []
        env_id = os.environ.get("MODELSCOPE_STT_MODEL", "").strip()
        if env_id:
            candidates.append(env_id)
        if model_name:
            candidates.extend(m.strip() for m in model_name.split(",") if m.strip())

        if not candidates:
            slog.info("[CpuSTT] 未配置 ModelScope 模型 ID，回退 HuggingFace")
            return None

        try:
            from modelscope import snapshot_download
        except ImportError:
            slog.warning("modelscope 未安装。pip install modelscope")
            return None

        for ms_id in candidates:
            slog.info(f"[CpuSTT] ModelScope 下载: {ms_id}")
            try:
                local = await asyncio.wait_for(
                    asyncio.to_thread(snapshot_download, ms_id),
                    timeout=300.0,
                )
            except asyncio.TimeoutError:
                slog.warning(f"[CpuSTT] {ms_id} 下载超时，尝试下一候选")
                continue
            except Exception as e:
                slog.warning(f"[CpuSTT] {ms_id} 失败: {e}，尝试下一候选")
                continue

            # 校验 CTranslate2 格式 (必须含 model.bin)
            if os.path.exists(os.path.join(local, "model.bin")):
                slog.info(f"[CpuSTT] {ms_id} 下载完成且格式兼容: {local}")
                return local
            slog.warning(
                f"[CpuSTT] {ms_id} 缺少 model.bin (非 CTranslate2 格式)，"
                f"faster-whisper 无法加载，尝试下一候选"
            )

        return None

    def _slog(self, msg: str) -> None:
        get_session_logger("system").info(f"[CpuSTT] {msg}")
