"""
CPU TTS 引擎 — 纯 CPU 可运行、内嵌式、流式语音合成

基于 edge-tts (Microsoft Edge TTS API)，特性:
  - 纯 HTTP 调用，无需 GPU，无需本地模型文件
  - 首次运行零部署 (pip install edge-tts 即可)
  - 中文优化默认语音 (zh-CN-XiaoxiaoNeural)，兼顾日/英
  - 按标点自动分句，流式返回音频分片
  - 支持 cancel() 立即停止合成
  - 所有耗时操作 asyncio 原生异步，不阻塞事件循环

继承: BaseTTS (engines/base.py)
"""

from __future__ import annotations

import asyncio
import re
from typing import AsyncIterator, Optional

import httpx

from ..base import BaseTTS, EngineConfig, EngineState, EngineError
from ...logging_config import get_session_logger


class CpuTTSEngine(BaseTTS):
    """
    CPU 友好的流式语音合成引擎。

    config.extra 支持:
      - voice: edge-tts 语音名 (默认 "zh-CN-XiaoxiaoNeural")
        中文: zh-CN-XiaoxiaoNeural, zh-CN-YunxiNeural, zh-CN-YunyangNeural
        日语: ja-JP-NanamiNeural, ja-JP-KeitaNeural
        英语: en-US-JennyNeural, en-US-GuyNeural
      - rate: 语速 "-20%" ~ "+20%" (默认 "+0%")
      - pitch: 音调 "-20Hz" ~ "+20Hz" (默认 "+0Hz")
    """

    # 中/日/英标点分句符
    _SENTENCE_BREAKS = re.compile(r"[。！？!?\n；;]+")

    # 中文优化默认
    _DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

    def __init__(self):
        super().__init__()
        self._config: Optional[EngineConfig] = None
        self._voice: str = self._DEFAULT_VOICE
        self._cancel = asyncio.Event()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self, config: EngineConfig) -> None:
        slog = get_session_logger("system")
        self._config = config
        self._cancel.clear()
        self._voice = config.extra.get("voice", self._DEFAULT_VOICE)

        # 检查依赖
        try:
            import edge_tts
        except ImportError:
            self._state = EngineState.ERROR
            slog.warning("edge-tts 未安装。pip install edge-tts")
            return

        # 验证语音可用性
        try:
            voices = await edge_tts.VoicesManager.create()
            available = {v["ShortName"] for v in voices.voices if v["ShortName"] == self._voice}
            if not available:
                slog.warning(f"语音 {self._voice} 不可用，使用默认 {self._DEFAULT_VOICE}")
                self._voice = self._DEFAULT_VOICE
        except Exception:
            slog.info(f"[CpuTTS] 无法验证语音列表，直接使用 {self._voice}")

        self._state = EngineState.READY
        slog.info(f"[CpuTTS] 就绪 (voice={self._voice})")

    async def feed(self, text: str) -> AsyncIterator[bytes]:
        """
        流式合成语音。

        流程: 按标点分句 → 逐句调用 edge-tts → yield PCM 分片。

        Yields:
            bytes: MP3 音频块 (可直接 base64 编码发给固件)
        """
        if not text.strip():
            return

        self._state = EngineState.RUNNING
        self._cancel.clear()

        import edge_tts

        rate = self._config.extra.get("rate", "+0%") if self._config else "+0%"
        pitch = self._config.extra.get("pitch", "+0Hz") if self._config else "+0Hz"

        # 句子预处理 (空句/日语漂移句过滤, 保证预取索引对齐)
        sentences: list[str] = []
        for s in self._split_sentences(text):
            s = s.strip()
            if not s:
                continue
            # 语言过滤 (兑底): MiniCPM-o 偶发日语漂移 → 含假名的句子跳过
            if re.search(r"[\u3040-\u30ff]", s):
                get_session_logger("system").warning(
                    f"[CpuTTS] 跳过非中文句 (语言漂移): {s[:30]}"
                )
                continue
            sentences.append(s)
        if not sentences:
            self._state = EngineState.READY
            return

        # 预取模式: 当前句发送时并行合成下一句 → 消除句间空隙 (断续主因)
        next_task: Optional[asyncio.Task] = None
        try:
            for idx, sentence in enumerate(sentences):
                if self._cancel.is_set():
                    self._state = EngineState.CANCELLED
                    return
                try:
                    if next_task is not None:
                        pcm = await next_task          # 预取结果 (已并行完成)
                        next_task = None
                    else:
                        pcm = await self._synth_sentence(sentence, rate, pitch)

                    # 启动下一句预取
                    if idx + 1 < len(sentences):
                        next_task = asyncio.create_task(
                            self._synth_sentence(sentences[idx + 1], rate, pitch)
                        )

                    if not pcm:
                        continue

                    # 按 ~100ms 分片 yield (16000Hz × 2B × 0.1s = 3200 bytes)
                    SLICE = 3200
                    for i in range(0, len(pcm), SLICE):
                        if self._cancel.is_set():
                            self._state = EngineState.CANCELLED
                            return
                        yield pcm[i:i + SLICE]
                        # ★ 实时节流: 按音频时长推进 (3200B/2=1600 样本 = 100ms @16k)。
                        #   此前无节流 → 整段瞬间推送 → 固件播放环被满 → 满时丢最旧
                        #   → 播报跳跃/字间叠加。节流后推送速率=播放速率, 边收边播。
                        await asyncio.sleep(SLICE / 2 / 16000)
                except Exception as e:
                    get_session_logger("system").warning(
                        f"[CpuTTS] 句子合成失败: {sentence[:30]}... → {e}"
                    )
                    continue
        finally:
            if next_task is not None and not next_task.done():
                next_task.cancel()

        self._state = EngineState.READY

    async def _synth_sentence(self, sentence: str, rate: str, pitch: str) -> bytes:
        """合成单句 → PCM 16kHz mono (供预取任务并行调用)"""
        import edge_tts
        communicate = edge_tts.Communicate(
            text=sentence,
            voice=self._voice,
            rate=rate,
            pitch=pitch,
        )
        mp3_buf = bytearray()
        async for chunk in communicate.stream():
            if self._cancel.is_set():
                return b""
            if chunk["type"] == "audio" and chunk["data"]:
                mp3_buf.extend(chunk["data"])
        if not mp3_buf:
            return b""
        pcm = await asyncio.to_thread(self._decode_mp3_to_pcm, bytes(mp3_buf))
        return self._apply_fade(pcm)

    @staticmethod
    def _apply_fade(pcm: bytes, fade_samples: int = 128) -> bytes:
        """首尾淡入淡出 (8ms @16k)。

        每句 TTS 独立合成, 首尾波形突变 (0→满幅 / 满幅→截断) 会听成
        "喷"爆音(尤其片段边界)。淡入淡出后波形连续, 边界无突变。
        """
        import array

        n = len(pcm) // 2
        if n < fade_samples * 2:
            return pcm
        a = array.array("h")
        a.frombytes(pcm[: n * 2])
        for i in range(fade_samples):
            g = i / fade_samples
            a[i] = int(a[i] * g)
            a[n - 1 - i] = int(a[n - 1 - i] * g)
        return a.tobytes()

    async def cancel(self) -> None:
        """
        立即停止合成。

        编排层在检测到打断时调用。
        """
        self._cancel.set()
        self._state = EngineState.CANCELLED

    async def reset(self) -> None:
        self._cancel.clear()
        self._state = EngineState.READY

    async def health_check(self) -> bool:
        try:
            import edge_tts
            return True
        except ImportError:
            return False

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_mp3_to_pcm(mp3_bytes: bytes) -> bytes:
        """
        MP3 → PCM 16-bit 24kHz mono (miniaudio 内置解码器, 无外部依赖)。

        固件 M5.Speaker.playRaw 期望 PCM 16-bit 24kHz,
        edge-tts 输出的是 MP3, 必须转码后才能播放。

        Args:
            mp3_bytes: 完整 MP3 音频 (edge-tts 一句的输出)

        Returns:
            PCM 16-bit 字节, 解码失败返回 b""
        """
        try:
            import miniaudio

            decoded = miniaudio.decode(
                mp3_bytes,
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=1,
                sample_rate=16000,          # 重采样到 16k (与固件全双工播放同频)
            )
            # decoded.samples 是 array('h'), 直接转字节
            return memoryview(decoded.samples).tobytes()
        except ImportError:
            get_session_logger("system").warning(
                "[CpuTTS] miniaudio 未安装, 无法解码 MP3。pip install miniaudio"
            )
            return b""
        except Exception as e:
            get_session_logger("system").warning(f"[CpuTTS] MP3 解码失败: {e}")
            return b""

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        """按标点分句，保留分隔符"""
        result = []
        i = 0
        for m in CpuTTSEngine._SENTENCE_BREAKS.finditer(text):
            end = m.start()
            if i < end:
                result.append(text[i:end] + m.group())
            elif result:
                result[-1] += m.group()
            i = m.end()
        if i < len(text):
            result.append(text[i:])
        return result if result else [text]
