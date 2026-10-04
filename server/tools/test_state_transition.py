"""状态机转移验证 (临时, 验证后可删)"""
import sys
import asyncio

sys.path.insert(0, "/home/xiyzhao/Workspaces/AI_StackChan_Ex/server")
import stackchan_server.main  # noqa: F401
from stackchan_server.orchestration.state_machine import (
    ConversationStateMachine, ConversationState,
)


async def main():
    sm = ConversationStateMachine()
    await sm.to_listening()
    await sm.to_reasoning("x")
    assert sm.state == ConversationState.REASONING
    await sm.to_listening()  # 新增合法转移
    assert sm.state == ConversationState.LISTENING
    print("REASONING → LISTENING OK")

    # 回归: 原有转移不受影响
    sm2 = ConversationStateMachine()
    await sm2.to_listening()
    await sm2.to_reasoning("y")
    await sm2.to_speaking("回复")
    assert sm2.state == ConversationState.SPEAKING
    print("REASONING → SPEAKING OK")
    print("STATE_MACHINE ALL PASS")


asyncio.run(main())
