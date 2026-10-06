"""联网信息助手 (WebQueryManager) 回归验证

独立运行 (仅标准库):  python3 server/tools/test_web_query.py
"""
import asyncio
import importlib.util
import json
import os
import tempfile
import time
from pathlib import Path

_MOD = Path(__file__).resolve().parent.parent / \
    "stackchan_server" / "orchestration" / "web_query.py"
_spec = importlib.util.spec_from_file_location("web_query", _MOD)
wq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wq)

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


class FakeCfg:
    enabled = True
    provider = "ddg"
    endpoint = ""
    max_results = 3
    inline_wait_s = 0.5
    timeout_s = 60.0
    summary_max_tokens = 64
    cache_dir = ""


class FakeProvider:
    def __init__(self):
        self.calls = []

    async def search(self, query, k):
        self.calls.append((query, k))
        return [{"title": "T1", "url": "u1", "snippet": "S1"},
                {"title": "T2", "url": "u2", "snippet": "S2"}]


class FakeLLM:
    async def feed(self, msgs, **kw):
        yield "摘要内容"


async def main():
    tmp = tempfile.mkdtemp()
    cfg = FakeCfg()
    cfg.cache_dir = tmp
    prov = FakeProvider()
    mgr = wq.WebQueryManager(cfg, "test", llm_getter=lambda: FakeLLM(), provider=prov)

    print("== 注册 / 内联完成 / 提炼 ==")
    t = mgr.register("今天北京天气")
    got = await mgr.wait_inline(t, 1.0)
    check("内联等待完成", got, True)
    check("状态 done", t.status, "done")
    check("LLM 摘要", t.summary, "摘要内容")
    check("provider 调用参数", prov.calls, [("今天北京天气", 3)])

    print("== 待播报 / 标记 ==")
    pend = mgr.done_pending()
    check("待播报 1 条", len(pend), 1)
    mgr.mark_pending_broadcast(pend)
    check("播报后清空", len(mgr.done_pending()), 0)
    check("槽位无进行中时为空", mgr.prompt_slot(), "")

    print("== 槽位 (进行中) ==")
    t2 = mgr.register("上海天气")
    slot = mgr.prompt_slot()
    check("槽位含进行中任务", ("上海天气" in slot) and ("进行中" in slot), True)

    print("== 落盘 ==")
    files = sorted(os.listdir(os.path.join(tmp, "web_queries")))
    check("落盘文件存在", len(files) >= 1, True)
    data = json.load(open(os.path.join(tmp, "web_queries", files[0]), encoding="utf-8"))
    check("落盘字段完整", all(k in data for k in ("query", "status", "summary", "sources")), True)

    print("== 过期 ==")
    mgr2 = wq.WebQueryManager(cfg, "t2", llm_getter=lambda: None, provider=FakeProvider())
    t3 = wq.WebQueryTask(query="慢查询", created=time.time() - 999)
    mgr2._tasks.append(t3)
    mgr2.done_pending()          # 触发 _expire_stale
    check("过期标记", t3.status, "expired")

    print("== Bing 解析器 ==")
    _bing_html = (
        '<ol id="b_results">'
        '<li class="b_algo"><h2><a href="https://a.example/1">标题一</a></h2>'
        "<p>摘要一</p></li>"
        '<li class="b_algo"><h2><a href="https://b.example/2">标题二</a></h2>'
        "<p>摘要二</p></li>"
        "</ol>"
    )
    parsed = wq._parse_bing_html(_bing_html, 5)
    check("Bing 解析条目数", len(parsed), 2)
    check("Bing 解析字段", (parsed[0]["title"], parsed[0]["snippet"]),
          ("标题一", "摘要一"))
    check("Bing 解析链接", parsed[1]["url"], "https://b.example/2")

    print("== 失败任务待播报 ==")

    class _FailProvider:
        async def search(self, query, k):
            raise RuntimeError("net down")

    mgr4 = wq.WebQueryManager(cfg, "t4", llm_getter=lambda: None, provider=_FailProvider())
    t5 = mgr4.register("失败查询")
    await mgr4.wait_inline(t5, 1.0)
    check("失败状态", t5.status, "failed")
    pend4 = mgr4.done_pending()
    check("失败任务进入待播报", len(pend4), 1)
    mgr4.mark_pending_broadcast(pend4)
    check("失败播报后清空", len(mgr4.done_pending()), 0)

    print("== 无 LLM 兜底 ==")
    mgr3 = wq.WebQueryManager(cfg, "t3", llm_getter=lambda: None, provider=FakeProvider())
    t4 = mgr3.register("兜底查询")
    await mgr3.wait_inline(t4, 1.0)
    check("无 LLM 用原始摘要兜底", ("T1" in t4.summary) or ("S1" in t4.summary), True)

    print(f"\n{'ALL PASS' if fail == 0 else f'{fail} FAILED'} (pass={ok} fail={fail})")
    return 1 if fail else 0


raise SystemExit(asyncio.run(main()))
