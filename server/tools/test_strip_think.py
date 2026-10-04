"""think 剥离逻辑验证 (临时, 验证后可删)"""
import re

def strip_think(text: str) -> str:
    text = re.sub(r"<think\b[^>]*>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<think\b[^>]*>", "", text)
    text = re.sub(r"</think>", "", text)
    return text.strip()

ok = 0; fail = 0
def check(name, got, want):
    global ok, fail
    if got == want:
        ok += 1; print(f"  PASS {name}: {got!r}")
    else:
        fail += 1; print(f"  FAIL {name}: got {got!r} want {want!r}")

print("== think 剥离 ==")
check("典型形态(闭合在中间)", strip_think("<think>推理过程</think>\n你好呀"), "你好呀")
check("未闭合时保留内容(量化模型常见)", strip_think("<think>\n好的，我来帮你检查一下修改后的语音。"), "好的，我来帮你检查一下修改后的语音。")
check("多块", strip_think("<think>a</think>正文<think>b</think>结尾"), "正文结尾")
check("未闭合混在正文后", strip_think("正文<think>c"), "正文c")
check("带属性标签", strip_think("<think reasoning_effort=\"low\">推理</think>回答"), "回答")
check("孤立闭合标签", strip_think("回答</think>"), "回答")
check("换行空白残留", strip_think("<think>推理</think>\n\n回答\n"), "回答")
check("纯思考块", strip_think("<think>只有推理</think>"), "")
check("未闭合纯推理(保底为正文)", strip_think("<think>没写完"), "没写完")
check("无标签不动", strip_think("今天天气不错"), "今天天气不错")
check("带空格的think变体", strip_think("<think >推理</think>正文"), "正文")

print(f"\n{'ALL PASS' if fail == 0 else f'{fail} FAILED'} (pass={ok} fail={fail})")
raise SystemExit(1 if fail else 0)
