"""
StackChan Server - AI 全双工对话上位机服务

四层解耦架构:
  Layer 1: engines/    能力引擎层 - LLM/STT/TTS 纯能力封装
  Layer 2: orchestration/  会话编排层 - 状态机、打断、路由
  Layer 3: protocol/   接入协议层 - WebSocket 帧解析、Realtime 信令
  Layer 4: devices/    设备抽象层 - 表情/舵机/状态指令
"""

__version__ = "0.2.0"
