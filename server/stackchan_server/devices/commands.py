"""
设备指令数据结构

对齐 AI_StackChan_Ex 固件端支持的指令集。

固件端能力分析（基于 RealtimeChatGPT.cpp + Robot.cpp）:
  - 表情: Avatar 表情切换（通过 setExpression 等）
  - 舵机: PWM / SCS 串行舵机 x,y 位置控制
  - 状态: listening / speaking / idle 状态通知
  - 错误: API 错误、网络错误等通知

后续 Phase 2 将扩展 WebSocket 指令通道下发到固件。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# =============================================================================
# 设备指令类型枚举
# =============================================================================

class DeviceCommandType(str, Enum):
    """设备指令类型 - 对齐固件端可接收的指令"""
    # 表情
    EXPRESSION_SET = "expression.set"          # 设置表情
    EXPRESSION_RESET = "expression.reset"      # 重置为默认表情
    # 舵机
    SERVO_MOVE = "servo.move"                  # 移动舵机到指定位置
    SERVO_STOP = "servo.stop"                  # 停止舵机
    SERVO_HOME = "servo.home"                  # 回中
    # 状态
    STATUS_IDLE = "status.idle"                # 空闲
    STATUS_LISTENING = "status.listening"      # 收听中
    STATUS_SPEAKING = "status.speaking"        # 说话中
    STATUS_THINKING = "status.thinking"        # 思考中
    # 错误
    ERROR_NETWORK = "error.network"            # 网络错误
    ERROR_API = "error.api"                    # API 错误
    ERROR_QUOTA = "error.quota"                # 配额/余额不足
    # 系统
    SYSTEM_REBOOT = "system.reboot"            # 重启
    SYSTEM_SLEEP = "system.sleep"              # 休眠


# =============================================================================
# 表情指令
# =============================================================================

class ExpressionType(str, Enum):
    """固件端支持的 Avatar 表情"""
    NEUTRAL = "neutral"          # 默认
    HAPPY = "happy"              # 开心
    SAD = "sad"                  # 难过
    ANGRY = "angry"              # 生气
    SLEEPY = "sleepy"            # 困倦
    DOUBT = "doubt"              # 疑惑
    SURPRISE = "surprise"        # 惊讶
    WINK = "wink"                # 眨眼


@dataclass
class ExpressionCommand:
    """表情指令
    
    协议兼容说明: 固件端 Avatar 库支持 setExpression() 方法，
    对应 m5stack-avatar 库的表情类型枚举。
    后续通过 WebSocket 文本帧下发给固件。
    """
    expression: ExpressionType = ExpressionType.NEUTRAL
    duration_ms: int = 2000                    # 持续时间，0 表示永久


# =============================================================================
# 舵机指令
# =============================================================================

@dataclass
class ServoCommand:
    """舵机指令
    
    协议兼容说明: 固件端支持 PWM 和 SCS 两种舵机类型。
    x/y 范围 0-180 度，speed 0.0-1.0。
    对齐 SC_BasicConfig.yaml 中的 servo 配置节。
    """
    x: float = 90.0                            # X 轴角度 (0-180)
    y: float = 90.0                            # Y 轴角度 (0-180)
    speed: float = 1.0                         # 移动速度 (0.0-1.0)
    servo_type: str = "PWM"                    # "PWM" or "SCS"


# =============================================================================
# 状态通知
# =============================================================================

class DeviceStatus(str, Enum):
    """设备状态 - 对齐固件端状态机"""
    IDLE = "idle"                              # 空闲，等待触摸
    LISTENING = "listening"                    # 录音中
    PROCESSING = "processing"                  # STT + LLM 推理中
    SPEAKING = "speaking"                      # TTS 播放中
    INTERRUPTED = "interrupted"                # 被打断
    ERROR = "error"                            # 错误状态


@dataclass
class StatusNotification:
    """状态通知
    
    协议兼容说明: 固件端在屏幕显示 "Please touch" / "Listening..." / "" 等状态文字，
    同时控制 LED 颜色和 Avatar 表情。这里抽象为统一的状态事件。
    """
    status: DeviceStatus = DeviceStatus.IDLE
    message: str = ""                          # 显示文字
    led_color: Optional[str] = None            # LED 颜色 (#RRGGBB)
    timestamp: float = 0.0


# =============================================================================
# 错误通知
# =============================================================================

class ErrorSeverity(str, Enum):
    """错误严重程度"""
    INFO = "info"                              # 提示（不影响使用）
    WARNING = "warning"                        # 警告（功能降级）
    ERROR = "error"                            # 错误（需要关注）
    FATAL = "fatal"                            # 致命（需重启）


@dataclass
class ErrorNotification:
    """错误通知
    
    协议兼容说明: 固件端在 WebSocket 事件中接收 error 类型消息，
    在屏幕上显示错误信息并可能切换表情。
    对齐 OpenAI Realtime API 的 error 事件格式。
    """
    severity: ErrorSeverity = ErrorSeverity.ERROR
    code: str = ""                             # 错误码（如 "network_timeout"）
    message: str = ""                          # 错误描述
    recoverable: bool = True                   # 是否可恢复
    timestamp: float = 0.0


# =============================================================================
# 统一设备事件
# =============================================================================

@dataclass
class DeviceEvent:
    """统一设备事件 - 编排层与设备层的通信协议
    
    屏蔽固件版本差异，编排层只需发出 DeviceEvent，
    设备层负责转换为具体固件指令格式。
    """
    type: DeviceCommandType
    # 以下字段按需填充，取决于 type
    expression: Optional[ExpressionCommand] = None
    servo: Optional[ServoCommand] = None
    status: Optional[StatusNotification] = None
    error: Optional[ErrorNotification] = None
    # 元数据
    event_id: str = ""
    priority: int = 128                        # 0-255，越大越优先
