"""TTS 服务端音频对照: 用与服务端相同的链路合成一段语音, 存 wav 供人耳试听。

链路: edge-tts (MP3 24k) -> miniaudio decode + resample 16k -> wav

用法: cd server && source .venv/bin/activate && python3 tools/tts_check.py
输出: server/tts_check_16k.wav  (本地播放试听)
"""
import asyncio
import pathlib
import wave

import edge_tts
import miniaudio

TEXT = (
    "从前有一只小猫咪，它特别喜欢在晚上偷偷溜出去探险。"
    "它遇到了一只兔子，一起去森林里寻找会发光的石头。"
)
OUT = pathlib.Path(__file__).resolve().parent.parent / "tts_check_16k.wav"


async def main() -> None:
    communicate = edge_tts.Communicate(text=TEXT, voice="zh-CN-XiaoxiaoNeural")
    mp3 = bytearray()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            mp3.extend(chunk["data"])
    print("mp3 bytes:", len(mp3))

    decoded = miniaudio.decode(
        bytes(mp3),
        output_format=miniaudio.SampleFormat.SIGNED16,
        nchannels=1,
        sample_rate=16000,
    )
    data = memoryview(decoded.samples).tobytes()
    with wave.open(str(OUT), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(data)
    print("pcm samples:", len(decoded.samples))
    print("duration_s:", round(len(decoded.samples) / 16000, 2))
    print("saved:", OUT)


asyncio.run(main())
