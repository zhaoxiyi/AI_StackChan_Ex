"""全双工打断 VAD 验证 (临时, 验证后可删)"""
import sys
import array
import random

sys.path.insert(0, "/home/xiyzhao/Workspaces/AI_StackChan_Ex/server")
import stackchan_server.main  # noqa: F401
from stackchan_server.orchestration.session import ConversationSession

sess = ConversationSession(session_id="t")


def rms_audio(rms_val, n=2000):
    noise = array.array("h", (int(random.gauss(0, rms_val)) for _ in range(n)))
    return noise.tobytes()


# 静音/低回声: 不应触发
for _ in range(5):
    assert not sess._voice_activity(rms_audio(80)), "低回声误触发!"
print("PASS 低回声不触发")

# 强语音连续3块: 触发
assert not sess._voice_activity(rms_audio(600)), "第1块不应触发"
assert not sess._voice_activity(rms_audio(600)), "第2块不应触发"
assert sess._voice_activity(rms_audio(600)), "第3块应触发"
print("PASS 连续3块强语音触发")

# 中途噪声重置计数
assert not sess._voice_activity(rms_audio(20)), "噪声后应重置"
assert not sess._voice_activity(rms_audio(600))
assert not sess._voice_activity(rms_audio(600))
assert sess._voice_activity(rms_audio(600))
print("PASS 噪声重置去抖")

print("VAD ALL PASS")
