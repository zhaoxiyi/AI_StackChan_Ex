"""上下文保持逻辑单元测试 (临时, 验证后可删)"""
import sys
sys.path.insert(0, "/home/xiyzhao/Workspaces/AI_StackChan_Ex/server")

# 按 main 的导入顺序初始化包, 避免测试脚本触发循环导入路径
import stackchan_server.main  # noqa: F401

from stackchan_server.orchestration.utterance import UserTextCondenser, CorrectionMemory

c = UserTextCondenser()
ok = 0; fail = 0
def check(name, got, want):
    global ok, fail
    if got == want:
        ok += 1; print(f"  PASS {name}: {got!r}")
    else:
        fail += 1; print(f"  FAIL {name}: got {got!r} want {want!r}")

print("== UserTextCondenser ==")
check("整串周期重复(无标点)", c.condense("测试一下语音测试一下语音"), "测试一下语音")
check("整串周期重复(带句号)", c.condense("测试一下语音测试一下语音。"), "测试一下语音。")
check("三连重复", c.condense("Yeah Yeah Yeah"), "Yeah")
check("前缀重复", c.condense("再测试一遍语音再测试一遍语音"), "再测试一遍语音")
check("不重复不误伤", c.condense("今天天气怎么样"), "今天天气怎么样")
check("词级连续重复", c.condense("hello hello world"), "hello world")
check("填充词清除", c.condense("嗯嗯那个测试一下语音啊"), "测试一下语音")
check("日语填充词", c.condense("えーとあの今日は晴れ"), "今日は晴れ")

print("== 跨轮重复检测 ==")
check("完全相同", c.is_duplicate("测试一下语音。", "测试一下语音。"), True)
check("仅标点差异", c.is_duplicate("测试一下语音", "测试一下语音。"), True)
check("语义不同", c.is_duplicate("帮我查天气", "帮我查明天的天气"), False)
check("空文本", c.is_duplicate("", "x"), False)
check("短词不误判", c.is_duplicate("嗯", "嗯"), True)

print("== CorrectionMemory ==")
m = CorrectionMemory(max_entries=3)
check("不是X是Y", m.extract("不是星期三，是星期四。"), {"old": "星期三", "new": "星期四"})
check("X不对应该是Y", m.extract("北京不对，应该是上海。"), {"old": "北京", "new": "上海"})
check("应该是Y不是X", m.extract("应该是后天，不是明天。"), {"old": "明天", "new": "后天"})
check("纠正Y", m.extract("纠正：我叫小王。"), {"old": "", "new": "我叫小王"})
check("日语じゃなくて", m.extract("水じゃなくてお茶ください。"), {"old": "水", "new": "お茶ください"})
check("无更正返回None", m.extract("今天天气不错。"), None)

m.record("星期三", "星期四"); m.record("北京", "上海"); m.record("明天", "后天")
check("覆盖更新old相同", None if m.extract("不是星期三，是星期五。") is None else "covered", "covered")
m.record("星期三", "星期五")
suffix = m.to_system_suffix()
print("  suffix:", suffix.replace("\n", " / "))
check("注入文本包含最新值", "星期五" in suffix and "星期四" not in suffix, True)
m.record("甲", "乙")  # 第4条, 超上限3
check("上限截断", m.count, 3)

print("== ConversationHistory 摘要保留 ==")
from stackchan_server.orchestration.session import ConversationHistory
h = ConversationHistory(system_prompt="你是Stack-chan。", max_messages=20,
                        enable_summary=True, summary_budget_chars=200)
h.append_user("第一轮问题")
h.commit_assistant() if False else None
h._messages.append(type(h._messages[0])(role="assistant", content="第一轮回答内容比较长"))
h.append_user("第二轮问题")
h._messages.append(type(h._messages[0])(role="assistant", content="第二轮回答"))
h.append_user("第三轮问题，这是最新的一轮")
msgs = h.build_messages(max_chars=40)
sys_text = msgs[0].content
print("  system:", sys_text.replace("\n", " / "))
check("旧消息生成摘要而非丢弃", "更早对话摘要" in sys_text, True)
check("最近消息保留", any("第三轮问题" in m.content for m in msgs), True)
check("更正注入", None, None)

# 更正注入 system
h2 = ConversationHistory(system_prompt="SYS", enable_summary=False)
h2.set_correction_suffix("[更正] 关于'星期三'，用户的正确说法是：'星期五'")
h2.append_user("问题")
msgs2 = h2.build_messages(max_chars=0)
check("更正注入system", "[更正]" in msgs2[0].content, True)

print(f"\n{'ALL PASS' if fail == 0 else f'{fail} FAILED'} (pass={ok} fail={fail})")
sys.exit(1 if fail else 0)
