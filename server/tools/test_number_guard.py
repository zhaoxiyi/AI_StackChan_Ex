"""数字误伤回归验证 (临时)"""
import sys
sys.path.insert(0, "/home/xiyzhao/Workspaces/AI_StackChan_Ex/server")
import stackchan_server.main  # noqa: F401
from stackchan_server.orchestration.utterance import UserTextCondenser

c = UserTextCondenser()
cases = [
    ("2020。", "2020。"),           # 数字不误伤
    ("2020年", "2020年"),
    ("123456。", "123456。"),
    ("测试一下语音测试一下语音", "测试一下语音"),   # 短语重复仍压缩
    ("Yeah Yeah Yeah", "Yeah"),
    ("再测试一遍语音再测试一遍语音", "再测试一遍语音"),
]
ok = 0
for src, want in cases:
    got = c.condense(src)
    status = "PASS" if got == want else "FAIL"
    ok += (got == want)
    extra = "" if got == want else f" (want {want!r})"
    print(f"  {status} {src!r} -> {got!r}{extra}")
print(f"{ok}/{len(cases)} PASS")
sys.exit(0 if ok == len(cases) else 1)
