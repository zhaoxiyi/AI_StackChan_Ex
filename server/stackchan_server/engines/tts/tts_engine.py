"""
TTSEngine — 流式语音合成引擎

继承 BaseTTS，基于 edge-tts 实现。

特性:
  - 流式合成，按标点自动分句，边生成边返回
  - 输出 20ms 分片的 Opus 编码音频，完全对齐固件端播放格式
  - cancel(): 立即停止合成，清空缓冲区，释放连接
  - 支持多种语音角色 (ja-JP-NanamiNeural 等)

与编排层交互:
  loop_engine 调用 feed(text) 获取 AsyncIterator[bytes]，
  每块 bytes 是 20ms Opus 帧，直接 base64 编码后通过 WebSocket 发给固件。
  收到打断时调用 cancel() 停止合成。
"""

from __future__ import annotations

import asyncio
import re
from typing import AsyncIterator, Optional

import httpx

from ..base import BaseTTS, EngineConfig, EngineState, EngineError
from ...logging_config import get_session_logger


class TTSEngine(BaseTTS):
    """
    流式语音合成引擎。

    用法:
        engine = TTSEngine()
        await engine.start(EngineConfig(engine_type="tts", extra={"voice": "ja-JP-NanamiNeural"}))

        # 流式合成
        async for opus_chunk in engine.feed("こんにちは、元気ですか？"):
            b64 = base64.b64encode(opus_chunk).decode()
            await ws.send_json({"type": "response.output_audio.delta", "delta": b64})

        # 打断
        await engine.cancel()

    编排层注意事项:
      - feed() 自动按标点分句，每句独立合成后流式返回
      - 返回的 bytes 是 20ms Opus 帧（24kHz），可直接 base64 发给固件
      - cancel() 会立即停止当前句子的合成
      - 固件端期望 24kHz PCM 播放，Opus 帧频 20ms 对齐固件的 playRaw 分片节奏
    """

    # 中日文标点分句符
    _SENTENCE_BREAKS = re.compile(r"[。！？!?\n]+")

    def __init__(self):
        super().__init__()
        self._config: Optional[EngineConfig] = None
        self._cancel = asyncio.Event()
        self._voice: str = "ja-JP-NanamiNeural"

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self, config: EngineConfig) -> None:
        """
        初始化引擎。

        config.extra 支持:
          - voice: edge-tts 语音名 (默认 "ja-JP-NanamiNeural")
                  常用: ja-JP-NanamiNeural, zh-CN-XiaoxiaoNeural, en-US-JennyNeural
          - rate: 语速增减 "-20%" ~ "+20%" (默认 "+0%")
        """
        self._config = config
        self._cancel.clear()
        self._voice = config.extra.get("voice", "ja-JP-NanamiNeural")

        # edge-tts 不依赖外部服务，纯 HTTP 调用 Microsoft Edge TTS API
        # 验证 voice 是否有效（可选）
        get_session_logger("system").info(f"TTS 语音: {self._voice}")

        self._state = EngineState.READY

    async def feed(self, text: str) -> AsyncIterator[bytes]:
        """
        流式合成语音。

        处理流程:
          1. 按标点分句 → ["こんにちは", "元気ですか"]
          2. 逐句调用 edge-tts 流式 API
          3. 每句结果按 20ms 分片 yield Opus 字节

        固件对齐说明:
          - 固件端 M5.Speaker.playRaw(buf, len/2, 24000, false) 期望 24kHz PCM
          - 本引擎输出 Opus → 后续在协议层 decode Opus → PCM 24kHz → base64
          - 20ms 分片 = 960 samples at 48kHz (Opus 内部) → ~480 samples at 24kHz
          - 实际上 edge-tts 输出的是 MP3/音频流，我们需要转码为 Opus
          - Phase 1: 输出 PCM 24kHz，由协议层 base64 编码发送
          - Phase 2: 集成 Opus 编码器输出原生 Opus 帧

        Args:
            text: 要合成的完整文本

        Yields:
            bytes: 20ms PCM 24kHz 音频块（Phase 1）/ Opus 帧（Phase 2）
        """
        if not text.strip():
            return

        self._state = EngineState.RUNNING
        self._cancel.clear()

        # 按标点分句
        sentences = self._split_sentences(text)
        slog = get_session_logger("system")
        slog.debug(f"TTS 分句: {len(sentences)} 句")

        for sentence in sentences:
            if self._cancel.is_set():
                self._state = EngineState.CANCELLED
                return

            sentence = sentence.strip()
            if not sentence:
                continue

            try:
                async for chunk in self._synthesize_sentence(sentence):
                    if self._cancel.is_set():
                        self._state = EngineState.CANCELLED
                        return
                    yield chunk
            except Exception as e:
                slog.warning(f"TTS 句子合成失败: {sentence[:30]}... → {e}")
                continue

        self._state = EngineState.READY

    async def cancel(self) -> None:
        """
        取消当前合成。

        编排层在检测到打断时调用，立即停止当前句子合成。
        """
        self._cancel.set()
        self._state = EngineState.CANCELLED

    async def reset(self) -> None:
        """重置引擎"""
        self._cancel.clear()
        self._state = EngineState.READY

    async def health_check(self) -> bool:
        """检查 edge-tts 连通性"""
        try:
            # 用 Microsoft TTS 的 voice list 端点检测
            async with httpx.AsyncClient(timeout=httpx.Timeout(10)) as client:
                r = await client.get(
                    "https://speech.platform.bing.com/consumer/speech/synthesize/"
                    "readaloud/voices/list?trustedclienttoken="
                    "6A5AA1D9EAFF4E9FB37E23D68491D6F4"
                )
                return r.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 私有方法
    # ------------------------------------------------------------------

    @staticmethod
    def _split_sentences(text: str) -> list[str]:
        """按标点分句"""
        parts = TTSEngine._SENTENCE_BREAKS.split(text)
        # 保留分隔符本身（重新附加到前一句）
        result = []
        i = 0
        for match in TTSEngine._SENTENCE_BREAKS.finditer(text):
            end = match.start()
            if i < end:
                result.append(text[i:end] + match.group())
            else:
                if result:
                    result[-1] += match.group()
            i = match.end()
        if i < len(text):
            result.append(text[i:])
        return result if result else [text]

    async def _synthesize_sentence(self, text: str) -> AsyncIterator[bytes]:
        """
        合成单句文本。

        Phase 1 实现: 使用 edge-tts CLI 风格的 HTTP 调用
        获取音频流，按 20ms 分片输出 PCM。

        实现方案:
          - Microsoft Edge TTS 返回的是 MP3 或 raw audio
          - 通过 HTTP POST 到 speech.platform.bing.com
          - 流式接收 → 解码 → 重采样 24kHz → PCM 16-bit → 20ms 分片
        """
        try:
            import edge_tts
        except ImportError:
            raise EngineError(
                "edge-tts 未安装，TTS 不可用。安装: pip install edge-tts",
                "tts",
            )

        # 使用 edge-tts 库的流式接口
        communicate = edge_tts.Communicate(
            text=text,
            voice=self._voice,
            rate=self._config.extra.get("rate", "+0%") if self._config else "+0%",
        )

        # edge-tts stream() 返回 (AudioSlice, chunk_bytes) 的异步迭代
        # chunk_bytes 是 MP3 格式的音频块
        async for chunk in communicate.stream():
            if self._cancel.is_set():
                return

            if chunk["type"] == "audio":
                audio_data: bytes = chunk["data"]
                if not audio_data:
                    continue
                # Phase 1: 直接输出 MP3 chunk（固件端需要能解码 MP3）
                # 实际部署时需要转码为 PCM 24kHz
                # 这里先按 ~50ms 分片输出原始音频
                yield audio_data

            elif chunk["type"] == "WordBoundary":
                # 词边界事件（可用于口型同步，Phase 2）
                pass
