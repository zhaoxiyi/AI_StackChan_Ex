"""
配置管理模块

基于 pydantic + YAML，支持:
  - config.yaml 文件加载
  - 环境变量覆盖 (STACKCHAN_* 前缀)
  - ${VAR_NAME} 占位符解析
  - 嵌套模型验证
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field


# =============================================================================
# 子配置模型
# =============================================================================

class ServerConfig(BaseModel):
    """服务器监听配置"""
    host: str = "0.0.0.0"
    port: int = 8080
    ws_path: str = "/v1/realtime"
    health_path: str = "/health"


class BillingConfig(BaseModel):
    """计费/余额监控"""
    warning_threshold_cny: float = Field(default=10.0, description="余额低于此值发出警告")
    critical_threshold_cny: float = Field(default=2.0, description="余额低于此值自动降级到本地模型")
    check_interval_minutes: int = 60


class ModelEntry(BaseModel):
    """单个模型配置"""
    enabled: bool = True
    model_name: str = ""
    base_url: str = ""
    api_key: str = ""
    max_tokens: int = 2048
    temperature: float = 0.7
    billing: Optional[BillingConfig] = None


class RoutingConfig(BaseModel):
    """模型路由配置"""
    default_model: str = "vllm"
    models: dict[str, ModelEntry] = Field(default_factory=dict)


class STTAudioConfig(BaseModel):
    """STT 音频参数 - 对齐固件实际参数"""
    sample_rate: int = 16000               # 固件实际录制 16kHz
    channels: int = 1
    sample_width: int = 2                  # 16-bit
    chunk_samples: int = 2000              # 固件每块 2000 samples (~125ms)


class FasterWhisperConfig(BaseModel):
    """Faster-Whisper 本地引擎"""
    model_size: str = "small"              # tiny/base/small/medium/large-v3
    device: str = "cuda"
    compute_type: str = "float16"
    language: str = "ja"
    beam_size: int = 5
    vad_filter: bool = True
    vad_threshold: float = 0.5
    min_silence_duration_ms: int = 500


class CpuSTTConfig(BaseModel):
    """纯 CPU 内嵌 STT 引擎 (faster-whisper, 累积式整体识别)"""
    model_size: str = "small"              # tiny/base/small/medium
    device: str = "auto"                   # auto/cpu/cuda
    compute_type: str = "auto"             # auto/float16/int8
    language: Optional[str] = None         # None=自动检测, "zh", "ja", "en"
    beam_size: int = 5
    use_vad: bool = True
    model_source: str = "huggingface"      # huggingface/modelscope/local
    model_name: str = ""                   # ModelScope ID 或 local 路径
    silence_break_ms: int = 800            # 静音断句阈值
    max_audio_ms: int = 15000              # 音频最长累积


class SenseVoiceConfig(BaseModel):
    """SenseVoice 内嵌 STT 引擎 (sherpa-onnx + ModelScope 模型)"""
    # 模型源: huggingface(官方发布, 推荐) | modelscope(社区) | local(本地路径)
    model_source: str = "huggingface"
    model_name: str = "danieldong/sensevoice-small-onnx-quant"
    use_int8: bool = True                  # True=model.int8.onnx (237MB), False=model.onnx (937MB)
    num_threads: int = 4                   # onnxruntime 线程数
    use_itn: bool = True                   # 逆文本归一化
    silence_break_ms: int = 800            # 静音断句阈值
    max_audio_ms: int = 15000              # 音频最长累积


class STTConfig(BaseModel):
    """STT 总配置"""
    engine: str = "sensevoice"             # sensevoice | cpu_stt | faster_whisper | openai_whisper
    sensevoice: SenseVoiceConfig = Field(default_factory=SenseVoiceConfig)
    cpu_stt: CpuSTTConfig = Field(default_factory=CpuSTTConfig)
    faster_whisper: FasterWhisperConfig = Field(default_factory=FasterWhisperConfig)
    audio: STTAudioConfig = Field(default_factory=STTAudioConfig)


class TTSAudioConfig(BaseModel):
    """TTS 音频输出参数 - 对齐固件播放参数"""
    sample_rate: int = 24000               # 固件 playRaw 使用 24kHz


class OpenAITTSConfig(BaseModel):
    """OpenAI TTS 引擎"""
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "tts-1"
    voice: str = "alloy"
    response_format: str = "pcm"
    speed: float = 1.0


class CpuTTSConfig(BaseModel):
    """纯 CPU 内嵌 TTS 引擎 (edge-tts)"""
    voice: str = "zh-CN-XiaoxiaoNeural"   # edge-tts 语音角色
    rate: str = "+0%"                      # 语速 -20% ~ +20%
    pitch: str = "+0Hz"                    # 音调 -20Hz ~ +20Hz


class TTSConfig(BaseModel):
    """TTS 总配置"""
    engine: str = "cpu_tts"                # cpu_tts | openai_tts | voicevox
    cpu_tts: CpuTTSConfig = Field(default_factory=CpuTTSConfig)
    openai_tts: OpenAITTSConfig = Field(default_factory=OpenAITTSConfig)
    audio: TTSAudioConfig = Field(default_factory=TTSAudioConfig)


class TurnConfig(BaseModel):
    """对话轮次检测"""
    silence_timeout_ms: int = 2000
    max_recording_ms: int = 30000          # 对齐固件 REALTIME_RECORD_TIMEOUT
    min_speech_ms: int = 200


class InterruptionConfig(BaseModel):
    """打断策略"""
    enabled: bool = True
    strategy: str = "immediate_stop"       # immediate_stop | fade_out


class HistoryConfig(BaseModel):
    """对话历史"""
    max_messages: int = 20
    enable_summary: bool = True


class UtteranceConfig(BaseModel):
    """
    语句评估配置 — 断句识别的文本是否构成完整语句、是否提交 LLM。

    流程: 音频累积 → 静音断句 → STT 识别 → 文本评估 → (完整) → LLM
    """
    min_chars: int = 2                    # 最短有效文本 (低于此长度视为噪声/碎片)
    submit_punctuation: str = "。！？!?.;…"  # 句末标点 = 语句完整 → 立即提交
    submit_min_chars_no_punct: int = 15   # 无句末标点时累积多少字才提交
    max_pending_chars: int = 100          # 累积上限, 强制提交


class ConversationConfig(BaseModel):
    """对话总配置"""
    system_prompt: str = ""
    turn: TurnConfig = Field(default_factory=TurnConfig)
    interruption: InterruptionConfig = Field(default_factory=InterruptionConfig)
    history: HistoryConfig = Field(default_factory=HistoryConfig)
    utterance: UtteranceConfig = Field(default_factory=UtteranceConfig)


class LoggingConfig(BaseModel):
    """日志配置"""
    level: str = "INFO"
    file_enabled: bool = True
    file_path: str = "logs/stackchan_server.log"
    file_rotation: str = "100 MB"
    file_retention: str = "7 days"
    session_tracing: bool = True


# =============================================================================
# 根配置模型
# =============================================================================

class AppConfig(BaseModel):
    """应用根配置"""
    server: ServerConfig = Field(default_factory=ServerConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    stt: STTConfig = Field(default_factory=STTConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


# =============================================================================
# 配置加载
# =============================================================================

_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _resolve_env_vars(obj: Any) -> Any:
    """递归解析 ${VAR_NAME} 占位符"""
    if isinstance(obj, str):
        return _ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), ""), obj)
    elif isinstance(obj, dict):
        return {k: _resolve_env_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_resolve_env_vars(item) for item in obj]
    return obj


def load_config(config_path: Optional[str] = None) -> AppConfig:
    """
    加载配置。优先级: 环境变量 > YAML > 默认值。

    Args:
        config_path: YAML 文件路径，默认 server/config.yaml
    """
    if config_path is None:
        config_path = os.environ.get(
            "STACKCHAN_CONFIG",
            str(Path(__file__).parent.parent / "config.yaml"),
        )

    raw: dict[str, Any] = {}
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    # 环境变量覆盖
    _apply_env_overrides(raw)

    # 占位符解析
    raw = _resolve_env_vars(raw)

    return AppConfig(**raw)


def _apply_env_overrides(raw: dict[str, Any], prefix: str = "STACKCHAN_") -> None:
    """STACKCHAN_SERVER_PORT → raw["server"]["port"]"""
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        path = key[len(prefix):].lower().split("__")
        if len(path) == 1:
            path = key[len(prefix):].lower().split("_")
        _set_nested(raw, path, value)


def _set_nested(d: dict[str, Any], keys: list[str], value: str) -> None:
    """尽力匹配扁平 key 到嵌套字典"""
    for k, v in d.items():
        if k.lower().replace("_", "") == keys[0].lower().replace("_", ""):
            if len(keys) == 1:
                d[k] = _coerce(v, value)
            elif isinstance(v, dict):
                _set_nested(v, keys[1:], value)
            return
    d["_".join(keys)] = value


def _coerce(original: Any, env_value: str) -> Any:
    """类型强制转换"""
    if isinstance(original, bool):
        return env_value.lower() in ("true", "1", "yes")
    elif isinstance(original, int):
        return int(env_value)
    elif isinstance(original, float):
        return float(env_value)
    return env_value


# =============================================================================
# 全局单例
# =============================================================================

_config: Optional[AppConfig] = None


def get_config() -> AppConfig:
    """获取全局配置"""
    global _config
    if _config is None:
        _config = load_config()
    return _config
