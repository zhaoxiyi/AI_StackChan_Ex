"""
联网信息助手 — 后台异步搜索 + LLM 提炼 + 落盘 + 播报回流

设计 (doc/next_stage_design.md §1):
  - 用户语句命中"需联网"意图 → 注册后台任务 (不阻塞当前对话);
  - worker: search → LLM 提炼 → 落盘 cache/web_queries/<ts>_<slug>.json;
  - Prompt 槽位 ([后台任务]) 描述进行中任务 (防 LLM 虚构结果);
    "已完成待播报"由编排层在下一轮注入并播报; 播报后槽位移除,
    只留播报内容作为正常对话历史;
  - 超时任务标记 expired (落盘保留, 供回顾)。

本模块零内部依赖 (httpx 延迟导入), 可独立测试。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional


def _slug(text: str, maxlen: int = 24) -> str:
    t = re.sub(r"[^\w\u4e00-\u9fff]+", "-", text or "").strip("-")
    return t[:maxlen] or "query"


@dataclass
class WebQueryTask:
    """单次后台查询任务"""
    query: str
    session_id: str = ""
    status: str = "running"          # running | done | failed | expired
    created: float = field(default_factory=time.time)
    finished: float = 0.0
    summary: str = ""
    sources: list = field(default_factory=list)   # [{title,url,snippet}]
    error: str = ""
    broadcast: bool = False          # 是否已播报 (播报后从槽位移除)
    broadcast_ts: float = 0.0
    done_event: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def age_s(self) -> float:
        return max(0.0, time.time() - self.created)

    def to_dict(self) -> dict:
        return {
            "ts": self.created, "session": self.session_id, "query": self.query,
            "status": self.status, "finished": self.finished,
            "sources": self.sources, "summary": self.summary, "error": self.error,
            "broadcast": self.broadcast, "broadcast_ts": self.broadcast_ts,
        }

    def write_json(self, cache_dir: str) -> Optional[str]:
        """落盘 (时间戳命名, 供回顾)"""
        try:
            d = os.path.join(cache_dir, "web_queries")
            os.makedirs(d, exist_ok=True)
            name = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.created)) \
                + f"_{_slug(self.query)}.json"
            path = os.path.join(d, name)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, ensure_ascii=False, indent=1)
            return path
        except Exception:
            return None


# ---------------------------------------------------------------------------
# 搜索源
# ---------------------------------------------------------------------------

class SearXNGProvider:
    """SearXNG (自建/内网, JSON API)"""
    name = "searxng"

    def __init__(self, endpoint: str, timeout: float = 8.0, proxy: str = ""):
        self._endpoint = endpoint
        self._timeout = timeout
        self._proxy = proxy

    async def search(self, query: str, k: int) -> list[dict]:
        import httpx
        kw = {"proxy": self._proxy} if self._proxy else {}
        async with httpx.AsyncClient(timeout=self._timeout, **kw) as c:
            r = await c.get(self._endpoint,
                            params={"q": query, "format": "json"})
            r.raise_for_status()
            data = r.json()
        out = []
        for it in (data.get("results") or [])[:max(1, k)]:
            out.append({"title": it.get("title", ""),
                        "url": it.get("url", ""),
                        "snippet": it.get("content", "")})
        return out


class DuckDuckGoProvider:
    """DuckDuckGo HTML 端点 (无需 key 的兜底; 结构变化时返回空)"""
    name = "ddg"

    def __init__(self, timeout: float = 8.0, proxy: str = ""):
        self._timeout = timeout
        self._proxy = proxy

    async def search(self, query: str, k: int) -> list[dict]:
        import httpx
        headers = {"User-Agent": "Mozilla/5.0 (compatible; StackchanBot/0.6)"}
        kw = {"proxy": self._proxy} if self._proxy else {}
        async with httpx.AsyncClient(timeout=self._timeout, headers=headers,
                                     follow_redirects=True, **kw) as c:
            r = await c.get("https://html.duckduckgo.com/html/",
                            params={"q": query})
            r.raise_for_status()
            html = r.text
        titles = re.findall(
            r'<a[^>]*result__a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S)
        snippets = re.findall(
            r'class="result__snippet"[^>]*>(.*?)</a>', html, re.S)

        def _clean(x: str) -> str:
            return re.sub(r"<[^>]+>", "", x or "").strip()

        out = []
        for i, (url, title) in enumerate(titles[:max(1, k)]):
            out.append({"title": _clean(title), "url": url,
                        "snippet": _clean(snippets[i]) if i < len(snippets) else ""})
        return out


class BingCNProvider:
    """Bing 国内版 (cn.bing.com, 无需 key; 境外受限环境下可用)"""
    name = "bingcn"

    def __init__(self, timeout: float = 8.0, proxy: str = ""):
        self._timeout = timeout
        self._proxy = proxy

    async def search(self, query: str, k: int) -> list[dict]:
        import httpx
        headers = {"User-Agent": "Mozilla/5.0 (compatible; StackchanBot/0.6)"}
        kw = {"proxy": self._proxy} if self._proxy else {}
        async with httpx.AsyncClient(timeout=self._timeout, headers=headers,
                                     follow_redirects=True, **kw) as c:
            r = await c.get("https://cn.bing.com/search", params={"q": query})
            r.raise_for_status()
            html = r.text
        return _parse_bing_html(html, k)


def _parse_bing_html(html: str, k: int) -> list[dict]:
    """解析 Bing 搜索结果页 (b_algo 条目 → h2/a 标题与 <p> 摘要)"""
    out: list[dict] = []
    for block in re.findall(
            r'<li class="b_algo".*?(?=<li class="b_algo"|</ol>)', html, re.S):
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                      block, re.S)
        if not m:
            continue
        url = m.group(1)
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        snippet = ""
        for p in re.findall(r"<p[^>]*>(.*?)</p>", block, re.S):
            t = re.sub(r"<[^>]+>", "", p).strip()
            if t:
                snippet = t
                break
        if title:
            out.append({"title": title, "url": url, "snippet": snippet})
        if len(out) >= max(1, k):
            break
    return out


def make_provider(cfg: Any):
    proxy = getattr(cfg, "proxy", "") or ""
    name = getattr(cfg, "provider", "bingcn")
    if name == "searxng" and getattr(cfg, "endpoint", ""):
        return SearXNGProvider(cfg.endpoint, proxy=proxy)
    if name == "ddg":
        return DuckDuckGoProvider(proxy=proxy)
    return BingCNProvider(proxy=proxy)


# ---------------------------------------------------------------------------
# 会话级管理器
# ---------------------------------------------------------------------------

@dataclass
class _Msg:
    """轻量消息 (与引擎 LLMMessage duck-type 兼容; 避免内部依赖)"""
    role: str
    content: str


class WebQueryManager:
    """会话级后台查询管理器 (注册/回执/槽位/落盘)"""

    def __init__(self, cfg: Any, session_id: str = "",
                 llm_getter=None, provider=None, log_fn=None):
        self._cfg = cfg
        self._session = session_id
        self._llm_getter = llm_getter        # callable -> LLM 引擎 (可 None)
        self._provider = provider or make_provider(cfg)
        self._log = log_fn or (lambda m: None)   # 日志回调 (诊断搜索链路)
        self._tasks: list[WebQueryTask] = []

    # ---- 查询 ----

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._cfg, "enabled", False))

    @property
    def inline_wait_s(self) -> float:
        return float(getattr(self._cfg, "inline_wait_s", 2.5))

    # ---- 任务生命周期 ----

    def register(self, query: str) -> WebQueryTask:
        """注册后台查询 (worker 异步执行, 不阻塞调用方)"""
        t = WebQueryTask(query=(query or "").strip(), session_id=self._session)
        self._tasks.append(t)
        try:
            asyncio.get_running_loop().create_task(self._worker(t))
        except RuntimeError:
            pass          # 无运行事件循环 (同步测试场景): 由测试显式跑 worker
        return t

    async def wait_inline(self, task: WebQueryTask, timeout: float) -> bool:
        """本轮内联等待 (快查询直接带答案)"""
        try:
            await asyncio.wait_for(task.done_event.wait(), timeout=max(0.0, timeout))
            return True
        except asyncio.TimeoutError:
            return False

    def done_pending(self) -> list[WebQueryTask]:
        """已完成/失败/超时但未播报的任务 (顺带过期清理)"""
        self._expire_stale()
        return [t for t in self._tasks
                if t.status in ("done", "failed", "expired") and not t.broadcast]

    def prompt_slot(self) -> str:
        """[后台任务] Prompt 槽位文本 — 只描述"进行中"任务

        (防止 LLM 在无结果时虚构; 已完成待播报由编排层注入优先级更高的提示)
        """
        self._expire_stale()
        running = [t for t in self._tasks if t.status == "running"]
        if not running:
            return ""
        lines = ["[后台任务 (进行中; 严禁编造结果 — 只能告知用户'还在查')]"]
        for t in running:
            lines.append(f"- 搜索中: {t.query} (已 {t.age_s:.0f}s)")
        return "\n".join(lines)

    def mark_task_broadcast(self, task: WebQueryTask) -> None:
        if task and task.status in ("done", "failed", "expired") and not task.broadcast:
            task.broadcast = True
            task.broadcast_ts = time.time()
            task.write_json(getattr(self._cfg, "cache_dir", "cache"))

    def mark_pending_broadcast(self, tasks: list[WebQueryTask]) -> None:
        for t in tasks:
            self.mark_task_broadcast(t)

    def _expire_stale(self) -> None:
        now = time.time()
        timeout_s = float(getattr(self._cfg, "timeout_s", 120.0))
        for t in self._tasks:
            if t.status == "running" and now - t.created > timeout_s:
                t.status = "expired"
                t.finished = now
                t.done_event.set()
                t.write_json(getattr(self._cfg, "cache_dir", "cache"))

    # ---- worker ----

    async def _worker(self, task: WebQueryTask) -> None:
        # 各任务为独立 asyncio 任务 → 天然并行, 互不阻塞;
        # wait_for 防止单个源端 hang 卡住任务自身 (不影响其他任务)
        try:
            self._log(f"web_query 搜索开始: {task.query[:60]}")
            sources = await asyncio.wait_for(
                self._provider.search(task.query,
                                      int(getattr(self._cfg, "max_results", 5))),
                timeout=25.0)
            self._log(f"web_query 搜索返回 {len(sources)} 条: {task.query[:40]}")
            task.sources = sources or []
            task.summary = await self._summarize(task.query, task.sources)
            task.status = "done"
            self._log(f"web_query 完成: {task.query[:40]} → {task.summary[:60]}")
        except Exception as e:
            task.status = "failed"
            task.error = f"{type(e).__name__}: {e}"
            self._log(f"web_query 失败: {task.query[:40]} → {task.error}")
        finally:
            task.finished = time.time()
            task.write_json(getattr(self._cfg, "cache_dir", "cache"))
            task.done_event.set()

    async def _summarize(self, query: str, sources: list[dict]) -> str:
        if not sources:
            return "未检索到相关信息。"
        blob = "\n".join(
            f"[{i + 1}] {s.get('title', '')}: {s.get('snippet', '')}"
            for i, s in enumerate(sources[:5]))
        llm = self._llm_getter() if self._llm_getter else None
        if llm is None:
            return blob[:400]
        msgs = [
            _Msg("system", "你是信息提炼助手。根据搜索结果, 用简体中文给出简短"
                           "要点式回答(80字内), 只依据搜索结果, 不臆造。"),
            _Msg("user", f"问题: {query}\n搜索结果:\n{blob}"),
        ]
        out: list[str] = []

        async def _run():
            async for chunk in llm.feed(
                    msgs, max_tokens=int(getattr(self._cfg, "summary_max_tokens", 256)),
                    temperature=0.3):
                out.append(chunk)

        try:
            await asyncio.wait_for(_run(), timeout=30.0)
        except Exception:
            return blob[:400]
        text = "".join(out).strip()
        return text or blob[:400]
