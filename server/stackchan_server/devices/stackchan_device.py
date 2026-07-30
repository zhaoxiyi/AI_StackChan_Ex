"""
设备抽象层

统一硬件指令格式，屏蔽不同固件版本差异。
目前主要为了 Phase 1 的固件兼容性占位，
后续扩展时定义伺服控制、LED、表情等指令。
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class DeviceCommandType(str, Enum):
    """设备指令类型"""
    # 伺服控制
    SERVO_MOVE = "servo.move"
    SERVO_STOP = "servo.stop"
    SERVO_RESET = "servo.reset"
    # 表情控制
    EXPRESSION_SET = "expression.set"
    # LED 控制
    LED_SET = "led.set"
    # 音频控制
    AUDIO_VOLUME = "audio.volume"
    # 系统
    SYSTEM_REBOOT = "system.reboot"
    SYSTEM_SLEEP = "system.sleep"


@dataclass
class ServoPosition:
    """伺服位置"""
    x: float = 90.0  # 0-180 度
    y: float = 90.0


@dataclass
class DeviceCommand:
    """设备抽象指令"""
    type: DeviceCommandType
    params: dict = field(default_factory=dict)
    # 指令优先级（0-255，越大越优先）
    priority: int = 128
    # 唯一 ID，用于去重
    command_id: str = ""

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "params": self.params,
            "priority": self.priority,
            "command_id": self.command_id,
        }


class StackChanDevice:
    """
    StackChan 设备抽象。

    提供统一的设备控制接口，与具体固件版本解耦。
    """

    # 固件版本
    FIRMWARE_V1 = "1.0"  # AI_StackChan_Ex baseline
    FIRMWARE_V2 = "2.0"  # 后续扩展

    def __init__(self, firmware_version: str = FIRMWARE_V1):
        self.firmware_version = firmware_version

    def build_servo_move(self, x: float, y: float, speed: float = 1.0) -> DeviceCommand:
        """构建伺服移动指令"""
        return DeviceCommand(
            type=DeviceCommandType.SERVO_MOVE,
            params={"x": x, "y": y, "speed": speed},
        )

    def build_servo_stop(self) -> DeviceCommand:
        """构建伺服停止指令"""
        return DeviceCommand(type=DeviceCommandType.SERVO_STOP)

    def build_expression(self, expression: str) -> DeviceCommand:
        """构建表情指令"""
        return DeviceCommand(
            type=DeviceCommandType.EXPRESSION_SET,
            params={"expression": expression},
        )

    async def send_command(self, command: DeviceCommand) -> bool:
        """
        发送设备指令。

        Phase 1: 占位实现。
        Phase 2: 通过 WebSocket 发送指令给固件。
        """
        # TODO: 实现指令发送
        return True
