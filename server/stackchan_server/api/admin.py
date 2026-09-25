"""
Admin UI — 实时监控面板

提供:
  - /admin SSE 事件流
  - /admin/panel 内嵌 HTML 监控面板
  - 全局事件总线，编排层通过 publish() 推送事件

UI 功能:
  - 上方窗口: ASR 识别文本 (用户说了什么)
  - 下方窗口: LLM 回复文本 (助手说了什么)
  - 翻译按钮: 切换双语显示
  - 侧边栏: 连接状态、模型名、token 统计
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from ..logging_config import get_session_logger

admin_router = APIRouter()


# =============================================================================
# 事件总线
# =============================================================================

@dataclass
class AdminEvent:
    """监控事件"""
    type: str = ""            # asr | llm | status | error
    text: str = ""            # 文本内容
    speaker: str = ""         # user | assistant | system
    timestamp: float = field(default_factory=time.time)
    # 状态信息
    model: str = ""
    tokens: int = 0
    sessions: int = 0
    connection: str = ""      # connected | disconnected | idle


class EventBus:
    """全局事件总线 — 单例，线程安全"""

    def __init__(self):
        self._queues: list[asyncio.Queue] = []
        self._history: list[AdminEvent] = []
        self._max_history = 50

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._queues.append(q)
        # 发送历史事件
        for evt in self._history[-20:]:
            q.put_nowait(evt)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._queues:
            self._queues.remove(q)

    def publish(self, evt: AdminEvent) -> None:
        self._history.append(evt)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]
        dead: list[asyncio.Queue] = []
        for q in self._queues:
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._queues.remove(q)


# 全局单例
_bus = EventBus()


def publish_event(evt: AdminEvent) -> None:
    _bus.publish(evt)


# =============================================================================
# SSE 端点
# =============================================================================

@admin_router.get("/events")
async def sse_events(request: Request):
    """Server-Sent Events 流 — 实时推送监控事件"""
    q = _bus.subscribe()
    slog = get_session_logger("admin")

    async def generate():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {json.dumps(_evt_to_dict(evt), ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            _bus.unsubscribe(q)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _evt_to_dict(evt: AdminEvent) -> dict:
    return {
        "type": evt.type,
        "text": evt.text,
        "speaker": evt.speaker,
        "ts": evt.timestamp,
        "model": evt.model,
        "tokens": evt.tokens,
        "sessions": evt.sessions,
        "connection": evt.connection,
    }


# =============================================================================
# HTML 面板 (内嵌单文件)
# =============================================================================

PANEL_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>StackChan Monitor</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#c9d1d9;height:100vh;display:flex}
#main{flex:1;display:flex;flex-direction:column;padding:16px;gap:12px}
.panel{flex:1;border-radius:12px;padding:16px;overflow-y:auto;position:relative;min-height:120px}
.panel-header{font-size:13px;font-weight:600;margin-bottom:8px;display:flex;justify-content:space-between;align-items:center}
.panel-text{font-size:18px;line-height:1.6;white-space:pre-wrap;word-break:break-word}
.panel-translated{font-size:14px;color:#8b949e;margin-top:8px;padding-top:8px;border-top:1px solid #30363d;display:none}
.panel-translated.show{display:block}
#asr-panel{background:#161b22;border:1px solid #30363d}
#asr-panel .panel-header{color:#58a6ff}
#llm-panel{background:#161b22;border:1px solid #30363d}
#llm-panel .panel-header{color:#3fb950}
#sidebar{width:260px;background:#161b22;border-left:1px solid #30363d;padding:16px;display:flex;flex-direction:column;gap:16px}
#sidebar h3{font-size:14px;color:#8b949e;margin-bottom:8px}
.status-dot{width:10px;height:10px;border-radius:50%;display:inline-block;margin-right:6px}
.status-dot.online{background:#3fb950}
.status-dot.offline{background:#f85149}
.status-dot.idle{background:#d29922}
.metric{background:#0d1117;border-radius:8px;padding:12px}
.metric .value{font-size:24px;font-weight:700}
.metric .label{font-size:11px;color:#8b949e;margin-top:2px}
.btn{background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:8px 16px;border-radius:6px;cursor:pointer;font-size:13px;transition:all .15s}
.btn:hover{background:#30363d}
.btn.active{background:#1f6feb;border-color:#1f6feb;color:#fff}
#controls{display:flex;gap:8px;flex-wrap:wrap}
.copy-btn{font-size:11px;padding:2px 8px;background:transparent;border:1px solid #30363d;color:#8b949e;border-radius:4px;cursor:pointer}
.copy-btn:hover{color:#c9d1d9}
</style>
</head>
<body>
<div id="main">
  <div id="asr-panel" class="panel">
    <div class="panel-header"><span>🎤 语音输入 (ASR)</span><button class="copy-btn" onclick="copyText('asr-text')">复制</button></div>
    <div id="asr-text" class="panel-text">等待语音输入...</div>
    <div id="asr-translated" class="panel-translated"></div>
  </div>
  <div id="llm-panel" class="panel">
    <div class="panel-header"><span>🤖 助手回复 (LLM)</span><button class="copy-btn" onclick="copyText('llm-text')">复制</button></div>
    <div id="llm-text" class="panel-text">等待助手回复...</div>
    <div id="llm-translated" class="panel-translated"></div>
  </div>
</div>
<div id="sidebar">
  <h3>📡 连接状态</h3>
  <div class="metric">
    <div><span id="status-dot" class="status-dot idle"></span><span id="status-text">空闲</span></div>
  </div>
  <h3>🧠 模型</h3>
  <div class="metric"><div id="model-name" class="value" style="font-size:14px">-</div></div>
  <h3>📊 Token</h3>
  <div class="metric"><div id="token-count" class="value">0</div><div class="label">本会话累计</div></div>
  <h3>🔗 会话</h3>
  <div class="metric"><div id="session-count" class="value">0</div><div class="label">活跃连接</div></div>
  <h3>🔧 控制</h3>
  <div id="controls">
    <button id="btn-translate" class="btn" onclick="toggleTranslate()">🌐 翻译</button>
    <button class="btn" onclick="clearPanels()">🗑 清屏</button>
  </div>
</div>
<script>
let translateOn = false;
const es = new EventSource('/events');
let totalTokens = 0;
let currentAsr = '';
let currentLlm = '';

es.onmessage = function(e) {
  const d = JSON.parse(e.data);
  if (d.type === 'asr') {
    currentAsr += d.text;
    document.getElementById('asr-text').textContent = currentAsr || '...';
    if (translateOn) translateText(d.text, 'asr-translated', 'auto', 'zh');
  } else if (d.type === 'llm') {
    currentLlm += d.text;
    document.getElementById('llm-text').textContent = currentLlm || '...';
    if (translateOn) translateText(d.text, 'llm-translated', 'auto', 'zh');
  } else if (d.type === 'status') {
    updateStatus(d);
  } else if (d.type === 'commit') {
    currentAsr = '';
    currentLlm = '';
    document.getElementById('asr-text').textContent = '处理中...';
    document.getElementById('llm-text').textContent = '生成中...';
  }
  if (d.tokens) {
    totalTokens += d.tokens;
    document.getElementById('token-count').textContent = totalTokens;
  }
  if (d.model) document.getElementById('model-name').textContent = d.model;
  if (d.sessions !== undefined) document.getElementById('session-count').textContent = d.sessions;
  if (d.connection) {
    const dot = document.getElementById('status-dot');
    dot.className = 'status-dot ' + d.connection;
    document.getElementById('status-text').textContent =
      d.connection === 'connected' ? '已连接' : d.connection === 'idle' ? '空闲' : '断开';
  }
};

function toggleTranslate() {
  translateOn = !translateOn;
  const btn = document.getElementById('btn-translate');
  btn.classList.toggle('active', translateOn);
  if (!translateOn) {
    document.getElementById('asr-translated').classList.remove('show');
    document.getElementById('llm-translated').classList.remove('show');
  }
}

function translateText(text, targetId, from, to) {
  if (!text || !text.trim()) return;
  const el = document.getElementById(targetId);
  el.classList.add('show');
  el.textContent = '[翻译: ' + text.slice(0, 200) + ']';
}

function updateStatus(d) {
  document.getElementById('model-name').textContent = d.model || '-';
  if (d.sessions !== undefined) document.getElementById('session-count').textContent = d.sessions;
  if (d.connection) {
    const dot = document.getElementById('status-dot');
    dot.className = 'status-dot ' + d.connection;
    document.getElementById('status-text').textContent =
      d.connection === 'connected' ? '已连接' : d.connection === 'idle' ? '空闲' : '断开';
  }
}

function clearPanels() {
  currentAsr = ''; currentLlm = '';
  document.getElementById('asr-text').textContent = '等待语音输入...';
  document.getElementById('llm-text').textContent = '等待助手回复...';
}

function copyText(id) {
  const text = document.getElementById(id).textContent;
  navigator.clipboard.writeText(text).then(() => {
    const btn = event.target;
    btn.textContent = '已复制!';
    setTimeout(() => btn.textContent = '复制', 1000);
  });
}

es.onerror = () => document.getElementById('status-text').textContent = '连接中断';
</script>
</body>
</html>"""


@admin_router.get("/panel", response_class=HTMLResponse)
async def admin_panel():
    return PANEL_HTML
