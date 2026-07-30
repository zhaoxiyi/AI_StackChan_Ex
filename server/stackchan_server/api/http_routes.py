"""
HTTP REST 端点 — 健康检查 + 简易管理接口
"""

from __future__ import annotations

import time
from fastapi import APIRouter

from ..config import get_config
from .ws_handler import get_session_manager, _global_llm_pool, _global_stt, _global_tts

http_router = APIRouter()
_start = time.time()


# =============================================================================
# 健康检查
# =============================================================================

@http_router.get("/health")
async def health():
    """基础健康检查 — 负载均衡器 / Docker healthcheck 使用"""
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - _start),
        "version": "0.2.0",
        "service": "stackchan-server",
    }


# =============================================================================
# 服务状态
# =============================================================================

@http_router.get("/status")
async def status():
    """详细服务状态 — 含引擎健康、会话数"""
    cfg = get_config()
    mgr = get_session_manager()

    # 引擎状态
    engines = {}
    for name, eng in _global_llm_pool.items():
        engines[name] = {
            "state": eng.state.name,
            "healthy": await eng.health_check(),
            "model": cfg.routing.models.get(name, type(None)) and cfg.routing.models[name].model_name,
        }
    engines["stt"] = {
        "state": _global_stt.state.name if _global_stt else "none",
        "healthy": await _global_stt.health_check() if _global_stt else False,
    }
    engines["tts"] = {
        "state": _global_tts.state.name if _global_tts else "none",
        "healthy": await _global_tts.health_check() if _global_tts else False,
    }

    return {
        "server": {"host": cfg.server.host, "port": cfg.server.port},
        "routing": {"default_model": cfg.routing.default_model},
        "engines": engines,
        "sessions": mgr.status(),
    }


# =============================================================================
# 会话列表
# =============================================================================

@http_router.get("/sessions")
async def list_sessions():
    """列出所有会话"""
    mgr = get_session_manager()
    return mgr.status()


# =============================================================================
# DeepSeek 余额
# =============================================================================

@http_router.get("/balance")
async def balance():
    """查询 DeepSeek 余额"""
    ds = _global_llm_pool.get("deepseek")
    if ds and hasattr(ds, "get_balance_status"):
        return await ds.get_balance_status()
    return {"balance": None, "message": "DeepSeek 引擎未启用"}
