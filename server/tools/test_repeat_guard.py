"""LLM 输出复读治理 (ResponseGuard) 回归验证

独立运行 (仅标准库):  python3 server/tools/test_repeat_guard.py
"""
import importlib.util
import sys
from pathlib import Path

_MOD_PATH = Path(__file__).resolve().parent.parent / \
    "stackchan_server" / "orchestration" / "response_guard.py"
_spec = importlib.util.spec_from_file_location("response_guard", _MOD_PATH)
_rg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_rg)
ResponseGuard = _rg.ResponseGuard

ok = 0
fail = 0


def check(name, got, want):
    global ok, fail
    if got == want:
        ok += 1
        print(f"  PASS {name}: {got!r}")
    else:
        fail += 1
        print(f"  FAIL {name}: got {got!r} want {want!r}")


g = ResponseGuard()   # 默认参数: min6 / ratio0.5 / kept6 / sim0.8 / win3 / short40

print("== user_echo_intervals (复读用户区间检测) ==")
check("前缀复读", g.user_echo_intervals("今天天气怎么样?今天天气不错", "今天天气怎么样"), [(0, 7)])
check("嵌入复读", g.user_echo_intervals("你问今天天气怎么样,我看挺晴", "今天天气怎么样"), [(2, 9)])
check("句末标点差异", g.user_echo_intervals("今天天气怎么样。", "今天天气怎么样"), [(0, 7)])
check("短寒暄不触发(<min6)", g.user_echo_intervals("你好呀,今天想聊点什么?", "你好"), [])
check("无重合", g.user_echo_intervals("我记得明天有雨", "今天天气怎么样"), [])
check("同一片段多次出现取首处", g.user_echo_intervals("今天天气怎么样,今天天气怎么样", "今天天气怎么样"), [(0, 7)])

print("== similarity (bigram Jaccard) ==")
check("同句不同标点", g.similarity("好的呢。", "好的呢!"), 1.0)
check("完全无关", g.similarity("今天天气不错", "帮我写一段代码"), 0.0)
s = g.similarity("今天天气不错", "今天天气很好")
check("近似句中等相似", s > 0.3 and s < 0.8, True)

print("== guard 判定 ==")
r = g.guard("今天天气不错,适合出门散步。", "今天天气怎么样", [])
check("正常回复 pass", (r.action, r.cleaned), ("pass", "今天天气不错,适合出门散步。"))

r = g.guard("今天天气怎么样?今天天气不错,适合出门。", "今天天气怎么样", [])
check("前缀复读 → strip", (r.action, r.cleaned), ("strip", "今天天气不错,适合出门。"))

r = g.guard("你问今天天气怎么样,我看今天挺晴的,适合出门。", "今天天气怎么样", [])
check("引导词+复读(start=2) → strip", (r.action, r.cleaned), ("strip", "你问,我看今天挺晴的,适合出门。"))

r = g.guard("好的，我来检查一下语音状况。听起来一切正常。", "检查一下语音状况。", [])
check("呼应引用(中段出现用户原话) → 不剔除", (r.action, r.cleaned),
      ("pass", "好的，我来检查一下语音状况。听起来一切正常。"))

r = g.guard("我今天看了一下，语音状况没问题哦，一切正常。", "检查一下语音状况。", [])
check("语义丰富回应 → pass", r.action, "pass")

r = g.guard("今天天气怎么样呢?", "今天天气怎么样", [])
check("纯复读 → retry", r.action, "retry")
check("纯复读 retry 带提示", len(r.retry_hint) > 10, True)

r = g.guard("好的呢。", "随便说点什么吧", ["好的呢。", "好的呀。"])
check("自我复读短句 → retry", r.action, "retry")

_LONG = ("好的，我来检查一下语音状况。听起来一切正常哦，"
         "可以随时和你聊天啦！有什么想说的吗？")
r = g.guard(_LONG, "停婷。", [_LONG])
check("长回复逐字重复 → retry", r.action, "retry")

r = g.guard("好的呀，语音状态我看过了，挺正常的。还有什么想聊的吗？",
            "停婷。", [_LONG])
check("长回复小改写 → pass", r.action, "pass")

r = g.guard(_LONG, "停婷。",
            [_LONG + " (被打断, 部分内容未播报; 若用户后续问及此内容, "
                     "可用'我之前想说这个来着'开头补充)"])
check("带被打断标注的重复回复 → 仍拦截", r.action, "retry")

r = g.guard("好的呢。", "随便说点什么吧", ["今天天气不错,适合出门散步。"])
check("与近期回复无关 → pass", r.action, "pass")

r = g.guard("好的呢好的呢好的呢", "随便说点什么吧", ["好的呢好的呢"])
check("自我复读填充多遍句 → retry", r.action, "retry")

r = g.guard("这是一个比较长的回复,包含了具体的信息和新的内容,不会与之前的短回复相似度过高,应该直接放行通过。",
           "讲点新的", ["好的呢。"])
check("长回复自我相似不触发 → pass", r.action, "pass")

r = g.guard("今天天气怎么样?好的呢。", "今天天气怎么样", ["好的呢。"])
check("复读+自我复读双命中 → retry", r.action, "retry")

r = g.guard("", "今天天气怎么样", [])
check("空回复 pass", r.action, "pass")

# ---- 完全相同 (规范化后) → 不受长度豁免影响, 直接拦截 ----
# (现场案例: 53 字长回复连续多轮一字不差, 纯相似度路径依赖阈值余量)
r = g.guard(_LONG, "讲个故事吧。", [_LONG])
check("长回复完全相同 → 直接拦截", r.action, "retry")

r = g.guard(_LONG + "  ", "讲个故事吧。", [" " + _LONG])
check("完全相同(仅空白差异) → 直接拦截", r.action, "retry")

g2 = ResponseGuard(enabled=False)
r = g2.guard("今天天气怎么样?", "今天天气怎么样", [])
check("关闭时 pass", (r.action, r.cleaned), ("pass", "今天天气怎么样?"))

print(f"\n{'ALL PASS' if fail == 0 else f'{fail} FAILED'} (pass={ok} fail={fail})")
sys.exit(1 if fail else 0)
