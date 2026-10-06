"""barge-in 判定回归 (2026-10-06 第三轮: 二轮实测定系 + 播放窗参考)

判据现状 (session.py `_barge_in_activity`):
  - down 参考 = [0.8, 2.5]s 前下行片峰值 (设备"当前正在播"的内容;
    发送超前播放 ~0.8-2.5s), 空窗回退最近片
  - thr = down × echo_factor(2.0) × ratio + floor(400)
  - hard 综合 2.7x / soft 综合 2.3x
    (2026-10 二轮实测: 残余比 ~1.85-2.5, 用户说话 up/down ≈ 2.6-4.0;
     3.2x 时代需要"靠近 Mic"才触发 soft、hard 始终不触发)
  - 确认: 最近 5 块中 ≥3 块命中且当前块命中

覆盖:
  1. 残余水平不误触发 (≈2.0x down)
  2. 用户说话命中 (up 显著高于残余)
  3. 5中3 允许 1-2 块断裂
  4. 断裂超限 (5中2) 不触发
  5. 清零后重新累积
  6. 静默期 (down=0) floor 兜底
  7. down 时间窗语义: <0.8s 回退最近片 / >2.5s 视为无下行
"""
import sys
import time
import array
import random

sys.path.insert(0, "/home/xiyzhao/Workspaces/AI_StackChan_Ex/server")
import stackchan_server.main  # noqa: F401
from stackchan_server.orchestration.session import ConversationSession

sess = ConversationSession(session_id="t")


def rms_audio(rms_val, n=2000):
    """交替 ±rms_val 的方波: RMS 精确等于 rms_val (高斯噪声裁剪到 int16
    会低估 RMS, 导致边界断言不稳定)"""
    buf = array.array("h", [0] * n)
    for i in range(n):
        buf[i] = rms_val if (i & 1) == 0 else -rms_val
    return buf.tobytes()


def reset(down_rms=None, age_s=1.0):
    """清判定状态; 可选预置一条下行记录 (age_s 秒前)"""
    sess._barge_hist.clear()
    sess._probe_hist.clear()
    sess._downlink_recent = (
        [(time.time() - age_s, down_rms)] if down_rms is not None else []
    )


def seq(kind, rms_val, n):
    return [sess._barge_in_activity(rms_audio(rms_val), kind) for _ in range(n)]


# 1. 残余水平不误触发: down=6000 → thr_soft=14200 / thr_hard=16600,
#    up=12000 (≈2.0x 残余) 不应命中
reset(6000, 1.0)
assert not any(seq("soft", 12000, 5)), "残余 2.0x 不应命中 soft"
reset(6000, 1.0)
assert not any(seq("hard", 12000, 5)), "残余 2.0x 不应命中 hard"
print("PASS 残余水平不误触发")

# 2. 用户说话命中: down=6896 (实测样本) → thr_hard=19019, thr_soft=16261
reset(6896, 1.0)
assert any(seq("hard", 20356, 5)), "实测样本 up 20356 应命中 hard"
reset(6896, 1.0)
assert any(seq("soft", 20356, 5)), "实测样本 up 20356 应命中 soft"
print("PASS 用户说话命中")

# 3. 5中3 允许断裂: [h,m,h,h] → 第4块触发
reset(6000, 1.0)
r = [sess._barge_in_activity(rms_audio(v), "hard")
     for v in (25000, 5000, 25000, 25000)]
assert r == [False, False, False, True], f"5中3 应第4块触发: {r}"
print("PASS 5中3 允许断裂")

# 4. 断裂超限不触发: [h,m,m,m,h] → 5中2
reset(6000, 1.0)
r = [sess._barge_in_activity(rms_audio(v), "hard")
     for v in (25000, 5000, 5000, 5000, 25000)]
assert not any(r), f"5中2 不应触发: {r}"
print("PASS 断裂超限不触发")

# 5. 清零后重新累积
reset(6000, 1.0)
assert not any(seq("soft", 25000, 2)), "前2块不应触发"
assert sess._barge_in_activity(rms_audio(25000), "soft"), "第3块应触发 (3/3)"
sess._probe_hist.clear()        # 调用方 (handle_audio_chunk) 在触发分支清零
assert not sess._barge_in_activity(rms_audio(25000), "soft"), "清零后不应立即再触发"
print("PASS 计数清零语义")

# 6. 静默期 floor 兜底: down 滑窗为空 → thr=floor(400)
reset(None)
assert any(seq("hard", 5000, 3)), "无播报期强语音应命中 (floor 兜底)"
print("PASS 静默期 floor 兜底")

# 7. down 时间窗语义
#    a) 0.3s 前 (刚发, 播放尚未开始) → 回退最近片, 参考值=6000
reset(6000, 0.3)
assert not any(seq("hard", 12000, 5)), "回退最近片: 残余不应命中"
#    b) 3.0s 前 (超出播放滞后窗) → 视为无下行, thr=floor(400)
reset(6000, 3.0)
assert any(seq("hard", 5000, 3)), "超窗: 应退化为 floor 判据"
print("PASS down 时间窗语义")

print("BARGE-IN FIX ALL PASS")
