"""SessionManager 静默保留逻辑验证 (临时, 验证后可删)"""
import sys
import time
import asyncio

sys.path.insert(0, "/home/xiyzhao/Workspaces/AI_StackChan_Ex/server")
import stackchan_server.main  # noqa: F401  # 按 main 路径初始化包
from stackchan_server.api.session_manager import SessionManager

mgr = SessionManager()
s = asyncio.run(mgr.get_or_create("dev1"))
s.history.append_user("你好")

# 模拟 90 秒静默 (超过心跳 60s)
e = mgr._entries["dev1"]
e.last_active = time.time() - 90
asyncio.run(mgr._cleanup())
e = mgr._entries.get("dev1")
assert e is not None, "会话被误删!"
assert e.disconnected_at is not None and e.ws_disconnected is False, "未标记静默"
print("静默 90s: 会话保留, 历史 =", len(s.history._messages), "条")

# WS 仍活着, 用户再次说话 → touch 恢复在线
mgr.touch("dev1")
e = mgr._entries["dev1"]
assert e.disconnected_at is None, "touch 未恢复在线"
print("touch 后恢复在线, 历史仍在 =", len(s.history._messages), "条")

# 静默超 600s → 清理
e.disconnected_at = time.time() - 700
asyncio.run(mgr._cleanup())
assert "dev1" not in mgr._entries, "超长静默未清理"
print("静默超 600s: 正确清理")

# WS 真断开: 30s 窗口
s2 = asyncio.run(mgr.get_or_create("dev2"))
mgr.mark_disconnected("dev2")
e2 = mgr._entries["dev2"]
assert e2.ws_disconnected is True
e2.disconnected_at = time.time() - 31
asyncio.run(mgr._cleanup())
assert "dev2" not in mgr._entries
print("WS 断开超 30s: 正确清理")

print("SESSION_MGR ALL PASS")
