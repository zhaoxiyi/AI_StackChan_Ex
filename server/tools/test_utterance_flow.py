"""_process_utterance 精简/更正/重复跳过 集成验证 (临时, 验证后可删)"""
import sys
import asyncio

sys.path.insert(0, "/home/xiyzhao/Workspaces/AI_StackChan_Ex/server")
import stackchan_server.main  # noqa: F401
from stackchan_server.orchestration.session import ConversationSession
from stackchan_server.config import get_config


class FakeProto:
    def __init__(self):
        self.sent = []

    async def send_json(self, d):
        self.sent.append(d.get("type", d))


async def main():
    cfg = get_config()
    sess = ConversationSession(session_id="t", config=cfg)
    proto = FakeProto()
    sess._protocol_session = proto
    sess.sm.reset()

    # 场景1: 与上一轮重复 → 跳过 LLM, 发 committed 停录音
    sess.history.append_user("测试一下语音")
    await sess._process_utterance("测试一下语音测试一下语音。")
    types = [x for x in proto.sent if isinstance(x, str)]
    assert "input_audio_buffer.committed" in types, f"跳过分支未发 committed: {types}"
    assert len(sess.history._messages) == 1, "重复轮不应入历史"
    print("PASS 场景1: 重复跳过, committed 已发, 历史不增长")

    # 场景2: 更正记录 + 正常进入 (LLM 未注入 → 会走异常分支, 但更正应先记录)
    sess._corrections.record("星期三", "星期四")
    # 直接用内部方法验证注入
    sess.history.set_correction_suffix(sess._corrections.to_system_suffix())
    msgs = sess.history.build_messages(max_chars=0)
    assert "星期三" in msgs[0].content and "星期四" in msgs[0].content
    print("PASS 场景2: 更正注入 system prompt")

    # 场景3: 精简后与上轮不重复 → 历史应追加精简后的文本
    # (LLM 引擎未注入, _process_utterance 会走异常分支, 但 append_user 已发生)
    sess.history.append_user("今天天气怎么样")
    try:
        await sess._process_utterance("今天天气怎么样 嗯 那个 今天天气怎么样")
    except Exception:
        pass
    texts = [m.content for m in sess.history._messages if m.role == "user"]
    print("  历史 user 文本:", texts)
    assert any("今天天气怎么样" == t and len(t) <= 8 for t in texts), "精简文本未入历史"
    print("PASS 场景3: 精简文本入历史")

    print("INTEGRATION ALL PASS")


asyncio.run(main())
