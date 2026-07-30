"""
音频工具模块

提供 PCM 音频数据的基础操作:
- Base64 编解码
- 采样率转换
- 音频缓冲区管理
"""

import base64
import struct
from typing import Optional
import numpy as np


def pcm_to_base64(pcm_data: bytes) -> str:
    """
    将 PCM 16-bit 音频数据编码为 Base64。

    Args:
        pcm_data: 原始 PCM 16-bit 音频字节

    Returns:
        Base64 编码字符串
    """
    return base64.b64encode(pcm_data).decode("ascii")


def base64_to_pcm(b64_str: str) -> bytes:
    """
    将 Base64 字符串解码为 PCM 16-bit 音频数据。

    Args:
        b64_str: Base64 编码的音频字符串

    Returns:
        PCM 16-bit 音频字节
    """
    return base64.b64decode(b64_str)


def resample_pcm(
    pcm_data: bytes,
    src_rate: int,
    dst_rate: int,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    """
    PCM 音频重采样。

    使用线性插值进行简单的采样率转换。

    Args:
        pcm_data: 原始 PCM 数据
        src_rate: 源采样率
        dst_rate: 目标采样率
        channels: 声道数
        sample_width: 采样位宽（字节）

    Returns:
        重采样后的 PCM 数据
    """
    if src_rate == dst_rate:
        return pcm_data

    # 解码为 numpy 数组
    dtype = np.int16 if sample_width == 2 else np.int8
    samples = np.frombuffer(pcm_data, dtype=dtype)

    if channels > 1:
        samples = samples.reshape(-1, channels)

    # 计算新长度
    ratio = dst_rate / src_rate
    new_len = int(len(samples) * ratio)

    # 线性插值重采样
    if channels > 1:
        new_samples = np.zeros((new_len, channels), dtype=samples.dtype)
        for ch in range(channels):
            new_samples[:, ch] = np.interp(
                np.linspace(0, len(samples) - 1, new_len),
                np.arange(len(samples)),
                samples[:, ch],
            ).astype(dtype)
    else:
        new_samples = np.interp(
            np.linspace(0, len(samples) - 1, new_len),
            np.arange(len(samples)),
            samples,
        ).astype(dtype)

    return new_samples.tobytes()


def split_pcm_into_chunks(
    pcm_data: bytes,
    chunk_samples: int,
    sample_width: int = 2,
) -> list[bytes]:
    """
    将 PCM 音频数据分割为固定大小的块。

    Args:
        pcm_data: PCM 音频数据
        chunk_samples: 每块的采样数
        sample_width: 采样位宽（字节）

    Returns:
        音频块列表
    """
    chunk_size = chunk_samples * sample_width
    chunks = []
    for i in range(0, len(pcm_data), chunk_size):
        chunk = pcm_data[i:i + chunk_size]
        if len(chunk) < chunk_size:
            # 最后一个不完整的块，用静音填充
            chunk = chunk + b"\x00" * (chunk_size - len(chunk))
        chunks.append(chunk)
    return chunks


def pcm_int16_to_float32(pcm_data: bytes) -> np.ndarray:
    """
    将 PCM 16-bit int 数据转换为 float32 [-1.0, 1.0]。

    Args:
        pcm_data: PCM 16-bit 数据

    Returns:
        float32 numpy 数组
    """
    samples = np.frombuffer(pcm_data, dtype=np.int16)
    return samples.astype(np.float32) / 32768.0


def float32_to_pcm_int16(samples: np.ndarray) -> bytes:
    """
    将 float32 [-1.0, 1.0] 数据转换为 PCM 16-bit int。

    Args:
        samples: float32 numpy 数组

    Returns:
        PCM 16-bit 数据
    """
    samples = np.clip(samples, -1.0, 1.0)
    int_samples = (samples * 32767).astype(np.int16)
    return int_samples.tobytes()


def get_audio_duration_ms(pcm_data: bytes, sample_rate: int, sample_width: int = 2) -> float:
    """
    计算 PCM 音频数据的时长。

    Args:
        pcm_data: PCM 音频数据
        sample_rate: 采样率
        sample_width: 采样位宽（字节）

    Returns:
        时长（毫秒）
    """
    num_samples = len(pcm_data) // sample_width
    return (num_samples / sample_rate) * 1000.0


def concatenate_pcm(*chunks: bytes) -> bytes:
    """
    拼接多个 PCM 音频块。

    Args:
        *chunks: 多个 PCM 数据块

    Returns:
        拼接后的 PCM 数据
    """
    return b"".join(chunks)
