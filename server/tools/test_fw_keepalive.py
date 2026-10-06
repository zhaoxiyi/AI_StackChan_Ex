"""固件录音超时保活验证 (2026-10-06)

固件 REALTIME_RECORD_TIMEOUT=30s: 长时间监听 (持续杂音/无断句提交)
会因无 committed/speech_started 下发而 stopRealtimeRecord → "Please touch"
静默退出实时模式 (实测: 杂音持续 30s 后 12:36:43 触发)。

修复: handle_audio_chunk 入口每 20s 补发 speech_started (固件仅重置录音计时)。
"""
import sys
import time
import asyncio

sys.path.insert(0, "/home/xiyzhao/Workspaces/AI_StackChan_Ex/server")
import stackchan_server.main  # noqa: F401
from stackchan_server.orchestration.session import ConversationSession


class FakeProto:
    def __init__(self):
        self.sent = []

    async def send_json(self, msg):
        self.sent.append(msg)


sess = ConversationSession(session_id="t")
proto = FakeProto()
sess._protocol_session = proto

# 1. 距今 25s 未保活 → 音频块到达时应补发 speech_started
sess._fw_keepalive_ts = time.time() - 25.0
asyncio.run(sess.handle_audio_chunk(b"\x00\x00" * 100))
types = [m.get("type", "") for m in proto.sent]
assert any("speech_started" in t for t in types), f"应补发 speech_started: {types}"
print("PASS 超时前保活补发")

# 2. 刚保活过 → 20s 内不重复发送
proto.sent.clear()
asyncio.run(sess.handle_audio_chunk(b"\x00\x00" * 100))
assert not proto.sent, f"20s 内不应重复保活: {proto.sent}"
print("PASS 保活间隔抑制")

# 3. 再过一个周期 → 再次触发
sess._fw_keepalive_ts = time.time() - 21.0
proto.sent.clear()
asyncio.run(sess.handle_audio_chunk(b"\x00\x00" * 100))
types = [m.get("type", "") for m in proto.sent]
assert any("speech_started" in t for t in types), f"应再次保活: {types}"
print("PASS 周期保活")

print("FW KEEPALIVE ALL PASS")
