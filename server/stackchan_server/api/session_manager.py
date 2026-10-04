"""
会话管理器 — 全局设备 ID → Session 映射

职责:
  - 维护 device_id → ConversationSession 的映射表
  - 连接建立时自动创建或恢复会话
  - 断线后保留 30 秒，支持固件重连恢复
  - 心跳超时检测 (60s 无消息)，自动清理僵尸会话

固件适配:
  - 固件端通过 WebSocket 重连机制 (5s 间隔) 自动恢复连接
  - 30s 保留窗口覆盖 6 次重连尝试
  - 重连后恢复对话上下文和引擎状态
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from ..orchestration.session import ConversationSession
from ..config import AppConfig, get_config
from ..logging_config import get_session_logger


@dataclass
class SessionEntry:
    """会话条目 — 包含会话对象和元数据"""
    session: ConversationSession
    device_id: str
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    disconnected_at: Optional[float] = None  # 断线时间 (None=在线)
    # True = WebSocket 真断开 (短窗口, 等固件重连);
    # False = 心跳静默 (WS 仍活, 用户只是暂时没说话 — 历史长保留)
    ws_disconnected: bool = False


class SessionManager:
    """
    全局会话管理器。

    用法:
        mgr = SessionManager(config)
        session = await mgr.get_or_create("m5stack-abc123")
        ...
        await mgr.mark_disconnected("m5stack-abc123")
        session = await mgr.try_reconnect("m5stack-abc123")  # 30s 内可恢复
    """

    # 重连保留窗口 (秒) — WebSocket 真断开后的会话恢复窗口
    RECONNECT_WINDOW: float = 30.0
    # 历史保留时长 (秒) — WS 仍存活但用户静默时, 对话历史保留时长
    # 固件半双工: 用户不说话就不上行, 不能因为静默就丢掉上下文
    HISTORY_RETAIN_SECONDS: float = 600.0
    # 心跳超时 (秒) — 超过此时间无消息视为静默
    HEARTBEAT_TIMEOUT: float = 60.0
    # 清理间隔 (秒)
    CLEANUP_INTERVAL: float = 15.0

    def __init__(self, config: Optional[AppConfig] = None):
        self._cfg = config or get_config()
        self._entries: dict[str, SessionEntry] = {}
        self._cleanup_task: Optional[asyncio.Task] = None
        self._slog = get_session_logger("session_mgr")

    # ------------------------------------------------------------------
    # 会话获取
    # ------------------------------------------------------------------

    async def get_or_create(self, device_id: str) -> ConversationSession:
        """
        获取或创建会话。

        固件适配:
          - 固件首次连接 → 创建新会话
          - 固件重连 (30s 内) → 恢复已有会话，保持对话上下文
          - 超时重连 → 视为新设备，创建新会话
        """
        entry = self._entries.get(device_id)

        if entry is not None:
            if entry.disconnected_at is not None:
                # 断线重连
                elapsed = time.time() - entry.disconnected_at
                if elapsed <= self._retain_window(entry):
                    self._slog.info(f"设备 {device_id} 重连恢复 (离线 {elapsed:.1f}s)")
                    entry.disconnected_at = None
                    entry.ws_disconnected = False
                    entry.last_active = time.time()
                    return entry.session
                else:
                    # 超时，清理旧会话
                    self._slog.info(f"设备 {device_id} 重连超时 ({elapsed:.1f}s)，创建新会话")
                    del self._entries[device_id]
            else:
                # 已在线的设备重复连接 (固件 bug 或双实例)
                self._slog.warning(f"设备 {device_id} 重复连接，替换旧会话")
                del self._entries[device_id]

        # 创建新会话
        session = ConversationSession(session_id=device_id, config=self._cfg)
        self._entries[device_id] = SessionEntry(
            session=session,
            device_id=device_id,
        )
        self._slog.info(f"设备 {device_id} 会话已创建 (总数: {len(self._entries)})")
        return session

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def mark_disconnected(self, device_id: str) -> None:
        """标记断线 — WebSocket 真断开, 启动 30s 重连窗口"""
        entry = self._entries.get(device_id)
        if entry and entry.disconnected_at is None:
            entry.disconnected_at = time.time()
            entry.ws_disconnected = True
            self._slog.info(f"设备 {device_id} 已断线 (保留 {self.RECONNECT_WINDOW}s)")

    def touch(self, device_id: str) -> None:
        """更新最后活跃时间 (收到任何消息时调用)

        WS 仍存活时若曾被标记静默 (心跳超时), 收到消息即恢复在线,
        对话历史不丢。
        """
        entry = self._entries.get(device_id)
        if entry:
            entry.last_active = time.time()
            if entry.disconnected_at is not None and not entry.ws_disconnected:
                entry.disconnected_at = None

    def get(self, device_id: str) -> Optional[ConversationSession]:
        """获取会话 (不创建)"""
        entry = self._entries.get(device_id)
        return entry.session if entry and entry.disconnected_at is None else None

    # ------------------------------------------------------------------
    # 后台清理
    # ------------------------------------------------------------------

    async def start_cleanup(self) -> None:
        """启动后台清理任务"""
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def stop_cleanup(self) -> None:
        """停止后台清理"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

    async def _cleanup_loop(self) -> None:
        """定期清理僵尸会话"""
        while True:
            await asyncio.sleep(self.CLEANUP_INTERVAL)
            await self._cleanup()

    async def _cleanup(self) -> None:
        """清理逻辑"""
        now = time.time()
        to_remove: list[str] = []

        for device_id, entry in self._entries.items():
            # 1. 断线超时
            if entry.disconnected_at is not None:
                if now - entry.disconnected_at > self._retain_window(entry):
                    to_remove.append(device_id)
                    self._slog.info(f"设备 {device_id} 保留超时，清理会话")
                    continue

            # 2. 心跳静默 (WS 在线但长时间无消息)
            #    不立即清理: 固件半双工, 用户不说话就不上行,
            #    静默 ≠ 下线 — 仅标记进入长保留窗口, 历史不丢。
            if entry.disconnected_at is None:
                if now - entry.last_active > self.HEARTBEAT_TIMEOUT:
                    entry.disconnected_at = now
                    entry.ws_disconnected = False
                    self._slog.info(
                        f"设备 {device_id} 心跳静默 ({self.HEARTBEAT_TIMEOUT}s)，"
                        f"历史保留 {self.HISTORY_RETAIN_SECONDS:.0f}s"
                    )
                    continue

        for device_id in to_remove:
            entry = self._entries.pop(device_id, None)
            if entry:
                entry.session.reset()

    def _retain_window(self, entry: SessionEntry) -> float:
        """保留窗口: WS 真断开用短重连窗口, 静默用长历史保留"""
        return self.RECONNECT_WINDOW if entry.ws_disconnected else self.HISTORY_RETAIN_SECONDS

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    @property
    def active_count(self) -> int:
        """在线会话数"""
        return sum(1 for e in self._entries.values() if e.disconnected_at is None)

    @property
    def total_count(self) -> int:
        """总会话数 (含等待重连)"""
        return len(self._entries)

    def status(self) -> dict:
        """返回管理端点用的状态信息"""
        sessions = []
        for device_id, entry in self._entries.items():
            sessions.append({
                "device_id": device_id,
                "online": entry.disconnected_at is None,
                "created_at": entry.created_at,
                "last_active": entry.last_active,
                "disconnected_at": entry.disconnected_at,
            })
        return {
            "active": self.active_count,
            "total": self.total_count,
            "sessions": sessions,
        }
