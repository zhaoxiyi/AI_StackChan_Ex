"""
StackChan Server v0.2.0 — 主入口

用法:
    cd server
    python -m stackchan_server.main                        # 默认 config.yaml
    python -m stackchan_server.main --config my_config.yaml # 指定配置文件
    python -m stackchan_server.main --port 9090            # 覆盖端口

固件端对接 (零代码修改方案):
    将固件 RealtimeChatGPT.cpp 第 307 行:
      webSocket.beginSslWithCA("api.openai.com", 443, "/v1/realtime?model=gpt-realtime", root_ca_openai);
    修改为:
      webSocket.begin("192.168.1.100", 8080, "/v1/realtime?model=gpt-realtime");
    注意: 移除 SSL (beginSslWithCA → begin) 是因为本服务默认使用 HTTP。
    如需保留 SSL，请配置反向代理 (nginx) 提供 HTTPS 终端。
"""

from __future__ import annotations

import argparse
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from stackchan_server.config import get_config, load_config
from stackchan_server.logging_config import setup_logging
from stackchan_server.api.ws_handler import (
    ws_router,
    init_session_manager,
    set_global_engines,
)
from stackchan_server.api.http_routes import http_router
from stackchan_server.api.admin import admin_router
from stackchan_server.engines.llm.llm_engine import LLMEngine
from stackchan_server.engines.stt.stt_engine import STTEngine
from stackchan_server.engines.stt.cpu_stt_engine import CpuSTTEngine
from stackchan_server.engines.stt.sensevoice_engine import SenseVoiceSTTEngine
from stackchan_server.engines.tts.tts_engine import TTSEngine
from stackchan_server.engines.tts.cpu_tts_engine import CpuTTSEngine
from stackchan_server.engines.base import EngineConfig


# =============================================================================
# 应用生命周期
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    服务生命周期:
      启动 → 初始化日志 → 初始化引擎 → 初始化 SessionManager
      关闭 → 清理引擎 → 清理 SessionManager
    """
    cfg = get_config()

    # 1. 日志
    setup_logging(
        level=cfg.logging.level,
        file_enabled=cfg.logging.file_enabled,
        file_path=cfg.logging.file_path,
        file_rotation=cfg.logging.file_rotation,
        file_retention=cfg.logging.file_retention,
    )
    from loguru import logger

    logger.info("=" * 56)
    logger.info("StackChan Server v0.2.0 启动")
    logger.info(f"  WebSocket : ws://{cfg.server.host}:{cfg.server.port}{cfg.server.ws_path}")
    logger.info(f"  健康检查 : http://{cfg.server.host}:{cfg.server.port}{cfg.server.health_path}")
    logger.info(f"  会话管理 : http://{cfg.server.host}:{cfg.server.port}/sessions")

    # 2. 初始化引擎
    logger.info("初始化引擎...")
    llm_pool: dict[str, LLMEngine] = {}

    for name, entry in cfg.routing.models.items():
        if not entry.enabled:
            continue
        try:
            eng = LLMEngine()
            extra: dict = {}
            if name == "deepseek":
                extra["backend"] = "deepseek"
                if not entry.api_key or entry.api_key.startswith("${"):
                    logger.warning(f"DeepSeek API Key 未设置，跳过")
                    continue
            await eng.start(EngineConfig(
                engine_type="llm",
                model_name=entry.model_name,
                endpoint=entry.base_url,
                api_key=entry.api_key,
                extra=extra,
            ))
            llm_pool[name] = eng
            logger.info(f"  LLM {name}: {entry.model_name} → {entry.base_url}")
        except Exception as e:
            logger.warning(f"  LLM {name} 启动失败: {e}")

    # STT — 根据配置选择引擎
    stt_cfg = cfg.stt
    if stt_cfg.engine == "sensevoice":
        stt = SenseVoiceSTTEngine()
        try:
            c = stt_cfg.sensevoice
            await stt.start(EngineConfig(
                engine_type="stt",
                extra={
                    "model_source": c.model_source,
                    "model_name": c.model_name,
                    "use_int8": c.use_int8,
                    "num_threads": c.num_threads,
                    "use_itn": c.use_itn,
                    "silence_break_ms": c.silence_break_ms,
                    "max_audio_ms": c.max_audio_ms,
                },
            ))
            logger.info(f"  STT: sensevoice (source={c.model_source}, model={c.model_name})")
        except Exception as e:
            logger.warning(f"  STT 启动失败: {e}")
    elif stt_cfg.engine == "cpu_stt":
        stt = CpuSTTEngine()
        try:
            c = stt_cfg.cpu_stt
            await stt.start(EngineConfig(
                engine_type="stt",
                extra={
                    "model_size": c.model_size,
                    "device": c.device,
                    "compute_type": c.compute_type,
                    "language": c.language,
                    "beam_size": c.beam_size,
                    "use_vad": c.use_vad,
                    "model_source": c.model_source,
                    "model_name": c.model_name,
                    "silence_break_ms": c.silence_break_ms,
                    "max_audio_ms": c.max_audio_ms,
                },
            ))
            logger.info(f"  STT: cpu_stt ({c.model_size} on {c.device}, 源={c.model_source})")
        except Exception as e:
            logger.warning(f"  STT 启动失败: {e}")
    else:
        stt = STTEngine()
        try:
            await stt.start(EngineConfig(
                engine_type="stt",
                extra={
                    "model_size": stt_cfg.faster_whisper.model_size,
                    "device": stt_cfg.faster_whisper.device,
                },
            ))
            logger.info(f"  STT: {stt_cfg.engine} ({stt_cfg.faster_whisper.model_size})")
        except Exception as e:
            logger.warning(f"  STT 启动失败: {e}")

    # TTS — 根据配置选择引擎
    tts_cfg = cfg.tts
    if tts_cfg.engine == "cpu_tts":
        tts = CpuTTSEngine()
        try:
            c = tts_cfg.cpu_tts
            await tts.start(EngineConfig(
                engine_type="tts",
                extra={"voice": c.voice, "rate": c.rate, "pitch": c.pitch},
            ))
            logger.info(f"  TTS: cpu_tts ({c.voice})")
        except Exception as e:
            logger.warning(f"  TTS 启动失败: {e}")
    else:
        tts = TTSEngine()
        try:
            await tts.start(EngineConfig(
                engine_type="tts",
                extra={"voice": tts_cfg.openai_tts.voice},
            ))
            logger.info(f"  TTS: {tts_cfg.engine} ({tts_cfg.openai_tts.voice})")
        except Exception as e:
            logger.warning(f"  TTS 启动失败: {e}")

    # 注入全局引擎
    set_global_engines(llm_pool, stt, tts)

    # 3. 初始化 SessionManager + 启动后台清理
    mgr = await init_session_manager(llm_pool, stt, tts)
    logger.info(f"SessionManager 就绪 (重连窗口={mgr.RECONNECT_WINDOW}s, 心跳超时={mgr.HEARTBEAT_TIMEOUT}s)")

    logger.info("=" * 56)
    logger.info("服务就绪，等待固件连接...")

    yield

    # 关闭
    logger.info("正在关闭...")
    await mgr.stop_cleanup()


# =============================================================================
# FastAPI 应用
# =============================================================================

app = FastAPI(
    title="StackChan Server",
    description="AI StackChan 全双工对话上位机 — 兼容 OpenAI Realtime API",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ws_router)
app.include_router(admin_router)
app.include_router(http_router)


# =============================================================================
# CLI 入口
# =============================================================================

def main():
    """命令行启动入口"""
    parser = argparse.ArgumentParser(
        description="StackChan Server — AI 全双工对话上位机",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python -m stackchan_server.main
  python -m stackchan_server.main --config my_config.yaml
  python -m stackchan_server.main --port 9090
  STACKCHAN_SERVER_PORT=9090 python -m stackchan_server.main
        """,
    )
    parser.add_argument("--config", "-c", default=None, help="配置文件路径 (默认 server/config.yaml)")
    parser.add_argument("--port", "-p", type=int, default=None, help="服务端口 (覆盖配置文件)")
    parser.add_argument("--host", default=None, help="监听地址 (覆盖配置文件)")
    args = parser.parse_args()

    # 加载配置
    cfg = load_config(args.config)

    # CLI 参数覆盖
    if args.port:
        cfg.server.port = args.port
    if args.host:
        cfg.server.host = args.host

    # 环境变量覆盖 (STACKCHAN_SERVER_PORT 等已在 load_config 中处理)
    # 启动
    uvicorn.run(
        "stackchan_server.main:app",
        host=cfg.server.host,
        port=cfg.server.port,
        log_level="info",
        ws="auto",
    )


if __name__ == "__main__":
    main()
