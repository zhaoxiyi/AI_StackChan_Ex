"""授权流程 + 路由修复验证 (临时)"""
import sys
sys.path.insert(0, "/home/xiyzhao/Workspaces/AI_StackChan_Ex/server")
import stackchan_server.main  # noqa: F401
from stackchan_server.orchestration.session import IntentRouter, AuthorizationManager

r = IntentRouter()
am = AuthorizationManager()

print("== 路由验证 ==")
for t in ["检查一下刚才人原音状况。", "你好呀", "帮我写一段代码", "搜一下明天的天气"]:
    print(f"  route({t!r}) = {r.route(t)}")

print("== 授权提示 ==")
print("  prompt:", am.request_authorization("complex"))

print("== 授权响应 (中文/带标点) ==")
for t in ["可以。", "好的", "不行", "いいえ"]:
    am._pending_intent = "complex"      # 重置待授权状态
    print(f"  handle_response({t!r}) = {am.handle_response(t)}")
