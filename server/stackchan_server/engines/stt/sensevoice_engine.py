"""
SenseVoice STT 引擎 — 纯 CPU 内嵌式，ModelScope 模型，ONNX Runtime 推理

模型: danieldong/sensevoice-small-onnx-quant (ModelScope)
  文件: model.int8.onnx (237MB) + tokens.txt
  特性: 中文/粤语/日语/英语/韩语 自动检测，情感识别，ITN 归一化

运行时: sherpa-onnx (onnxruntime 后端)，纯 CPU 可运行
  安装: pip install sherpa-onnx

识别策略: 与 CpuSTTEngine 一致的累积式整体识别
  1. 静音断句 (silence_break_ms)
  2. 过长截断 (max_audio_ms)
  3. finalize() 最终提交

输出解析: SenseVoice 原始输出含特殊标记
  <|zh|><|NEUTRAL|><|Speech|><|woitn|>今天天气真好
  → 解析出语言 (zh) + 纯文本 (今天天气真好)

继承: BaseSTT (engines/base.py)
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Optional

import numpy as np

from ..base import BaseSTT, STTResult, EngineConfig, EngineState, EngineError
from ...logging_config import get_session_logger


# SenseVoice 输出中的语言标记
_LANG_MAP = {
    "zh": "zh", "en": "en", "yue": "yue", "ja": "ja", "ko": "ko",
    "nospeech": None,
}

# 清理所有 <|...|> 特殊标记
_TAG_RE = re.compile(r"<\|[^|]*\|>")


# =============================================================================
# protobuf wire format 工具 (零依赖 metadata 修补)
# =============================================================================

def _proto_varint(n: int) -> bytes:
    """protobuf varint 编码"""
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _scan_onnx_vocab_size(model_path: str) -> Optional[int]:
    """
    零依赖扫描 ONNX 模型的 token embedding 大小 (模型真实 vocab)。

    ONNX protobuf 结构:
      ModelProto{ field 7 = graph → GraphProto{ field 5 = initializer* } }
      TensorProto{ field 1 = dims (repeated int64) }
    SenseVoiceSmall 的 token embedding 是 [vocab, 512] 的 2D 权重，
    找到 dims=[N, 512] 且 24000 < N < 26000 的 initializer → N 即 vocab。
    """
    try:
        with open(model_path, "rb") as f:
            data = f.read()
    except Exception:
        return None

    def read_varint(buf, pos):
        result = 0
        shift = 0
        while pos < len(buf):
            b = buf[pos]
            pos += 1
            result |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        return result, pos

    def read_field(buf, pos):
        """读取一个 protobuf 字段 → (field_num, wire_type, value, new_pos)"""
        tag, pos = read_varint(buf, pos)
        field_num = tag >> 3
        wire_type = tag & 7
        if wire_type == 0:  # varint
            val, pos = read_varint(buf, pos)
            return field_num, wire_type, val, pos
        elif wire_type == 2:  # length-delimited
            length, pos = read_varint(buf, pos)
            val = buf[pos:pos + length]
            return field_num, wire_type, val, pos + length
        else:
            return field_num, wire_type, None, pos  # skip-able 类型

    # 1. 顶层 ModelProto → field 7 (graph)
    graph_bytes = None
    pos = 0
    while pos < len(data):
        fn, wt, val, pos = read_field(data, pos)
        if fn == 7 and wt == 2:
            graph_bytes = val
            break

    if graph_bytes is None:
        return None

    # 2. GraphProto → field 5 (initializer TensorProto)
    pos = 0
    while pos < len(graph_bytes):
        fn, wt, val, pos = read_field(graph_bytes, pos)
        if fn != 5 or wt != 2:
            continue

        # 3. TensorProto → field 1 (dims, packed 或 unpacked)
        tp = val
        dims: list[int] = []
        p = 0
        while p < len(tp):
            tfn, twt, tval, p = read_field(tp, p)
            if tfn != 1:
                continue
            if twt == 2:  # packed repeated int64
                d = 0
                while d < len(tval):
                    dv, d = read_varint(tval, d)
                    dims.append(dv)
            elif twt == 0:  # unpacked
                dims.append(tval)
            if len(dims) > 2:
                break

        if len(dims) == 2 and 24000 <= dims[0] <= 26000 and dims[1] == 512:
            return dims[0]

    return None


def _proto_has_key(blob: bytes, key: str) -> bool:
    """
    精确检测 ONNX 字节流中是否存在 metadata key。

    StringStringEntryProto.key = field 1 (tag 0x0A) + varint(len) + key_bytes。
    用完整编码模式匹配，避免 "version" 误匹配 "producer_version"。
    """
    kb = key.encode()
    pattern = b"\x0A" + _proto_varint(len(kb)) + kb
    return pattern in blob


class SenseVoiceSTTEngine(BaseSTT):
    """
    SenseVoice 内嵌 STT 引擎 (sherpa-onnx 后端)。

    config.extra 支持:
      - model_name: ModelScope 模型 ID (默认 danieldong/sensevoice-small-onnx-quant)
      - use_int8: True 使用 model.int8.onnx (默认 True, 237MB)
                  False 使用 model.onnx (937MB, 精度更高)
      - num_threads: onnxruntime 线程数 (默认 4)
      - use_itn: 逆文本归一化 (默认 True, 数字日期自动转写)
      - silence_break_ms: 静音断句阈值 (默认 800ms)
      - max_audio_ms: 音频最长累积 (默认 15000ms)
    """

    _DEFAULT_MODEL = "danieldong/sensevoice-small-onnx-quant"

    def __init__(self):
        super().__init__()
        self._config: Optional[EngineConfig] = None
        self._recognizer = None
        self._buffer: bytearray = bytearray()
        self._accumulated: str = ""
        self._cancel = asyncio.Event()
        self._silence_ms: float = 0.0
        self._detected_lang: str = ""
        # recognizer 创建参数 (用于失败时降级重建)
        self._rec_args: dict = {}
        # 运行时自愈标志
        self._tried_no_itn: bool = False
        self._map_error_count: int = 0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self, config: EngineConfig) -> None:
        slog = get_session_logger("system")
        self._config = config
        self._cancel.clear()
        self._buffer.clear()
        self._accumulated = ""
        self._silence_ms = 0.0

        # 1. 检查依赖
        try:
            import sherpa_onnx
        except ImportError:
            self._state = EngineState.ERROR
            slog.warning("sherpa-onnx 未安装。pip install sherpa-onnx")
            return

        # 2. 下载模型 (source: modelscope | huggingface | local)
        model_source = config.extra.get("model_source", "modelscope")
        if model_source == "huggingface":
            model_dir = await self._download_official_model(slog)
        else:
            model_dir = await self._download_model(slog)
        if model_dir is None:
            self._state = EngineState.ERROR
            slog.warning("SenseVoice 模型下载失败")
            return

        # 3. 选择模型文件 (含 LFS 指针检测)
        use_int8 = config.extra.get("use_int8", True)
        model_path, onnx_file = self._pick_onnx_file(model_dir, use_int8, slog)
        tokens_path = os.path.join(model_dir, "tokens.txt")

        if model_path is None:
            # 格式不兼容 (PyTorch 权重) 时重下无意义, 直接跳过
            is_pytorch = self._has_pytorch_weights(model_dir)
            if is_pytorch:
                slog.warning("[SenseVoice] 模型是 PyTorch 格式，无法转换，跳过重下")
            else:
                # 自动修复: 清理损坏缓存 (LFS 指针残留) 后重新下载一次
                slog.warning("[SenseVoice] 模型文件无效，清理缓存并重新下载...")
                model_dir = await self._redownload_after_cleanup(slog)
                if model_dir is not None:
                    model_path, onnx_file = self._pick_onnx_file(model_dir, use_int8, slog)
                    tokens_path = os.path.join(model_dir, "tokens.txt")

        if model_path is None:
            self._state = EngineState.ERROR
            slog.warning("SenseVoice 模型文件缺失 (onnx 不存在或 LFS 未拉取)")
            slog.warning(
                "手动修复: pip install modelscope && "
                "modelscope download --model danieldong/sensevoice-small-onnx-quant"
            )
            return
        if not os.path.exists(tokens_path):
            self._state = EngineState.ERROR
            slog.warning(f"tokens.txt 缺失: {tokens_path}")
            return

        # 4. tokens.txt 格式规范化 (按第二列类型智能判断)
        #    官方 sherpa-onnx tokens: "token id" 两列, id 是 int → 保留原样
        #    (老版本 ReadTokens 需要 id 列; 新版本两列/单列都兼容)
        #    danieldong 社区 tokens: "token score" 两列, score 是 float
        #    → 老版本解析失败, 需转单列
        tokens_path = self._normalize_tokens_file(tokens_path, slog)

        # 5. 修补 ONNX metadata (仅社区模型需要; 官方模型已完整)
        #    danieldong 社区模型缺 sherpa-onnx 需要的 metadata 字段:
        #    "offline-sense-voice-model.cc:Init 'vocab_size' does not exist in the metadata"
        if model_source != "huggingface":
            model_path = self._fix_sensevoice_metadata(model_path, tokens_path, slog)

        # 6. 加载 sherpa-onnx recognizer (耗时操作放线程池)
        num_threads = config.extra.get("num_threads", 4)
        use_itn = config.extra.get("use_itn", True)
        language = config.extra.get("language", "auto") or "auto"
        slog.info(f"[SenseVoice] 加载模型: {onnx_file} (threads={num_threads}, lang={language})")

        # 保存 recognizer 创建参数 (用于失败时降级重建)
        self._rec_args = {
            "sherpa_onnx": sherpa_onnx,
            "model_path": model_path,
            "tokens_path": tokens_path,
            "num_threads": num_threads,
            "use_itn": use_itn,
            "language": language,
        }

        try:
            self._recognizer = await asyncio.to_thread(
                self._create_recognizer, sherpa_onnx, model_path, tokens_path,
                num_threads, use_itn, language,
            )
        except Exception as e:
            self._state = EngineState.ERROR
            slog.warning(f"SenseVoice 加载失败: {e}")
            return

        # 7. vocab 对齐检测: 模型 embedding 大小 vs tokens 表行数
        #    不匹配时 CTC 输出 id 越界 → sym_table.at(id) → "_Map_base::at"
        #    官方模型跳过 (发布前已验证对齐)
        model_vocab = None if model_source == "huggingface" else _scan_onnx_vocab_size(model_path)
        if model_vocab is None:
            slog.info("[SenseVoice] 跳过 vocab 扫描 (官方模型或扫描失败)")
        else:
            slog.info(f"[SenseVoice] 模型 embedding vocab = {model_vocab}")
            tokens_vocab = sum(1 for _ in open(tokens_path, encoding="utf-8"))
            slog.info(f"[SenseVoice] tokens 表行数 = {tokens_vocab}")
            if model_vocab > tokens_vocab:
                missing = model_vocab - tokens_vocab
                slog.warning(
                    f"[SenseVoice] ⚠️ vocab 不匹配: 模型={model_vocab} tokens={tokens_vocab} "
                    f"(缺 {missing} 个) — 这是 _Map_base::at 的根因，自动补齐"
                )
                tokens_path = await asyncio.to_thread(
                    self._pad_tokens_file, tokens_path, missing, slog
                )
                # 用补齐后的 tokens 重建 recognizer
                try:
                    self._recognizer = await asyncio.to_thread(
                        self._create_recognizer, sherpa_onnx, model_path,
                        tokens_path, num_threads, use_itn, language,
                    )
                    slog.info("[SenseVoice] 已用补齐后的 tokens 重建 recognizer")
                except Exception as e:
                    self._state = EngineState.ERROR
                    slog.warning(f"SenseVoice 重建失败: {e}")
                    return
            elif model_vocab < tokens_vocab:
                slog.warning(
                    f"[SenseVoice] ⚠️ tokens 表({tokens_vocab}) 比模型 vocab({model_vocab}) 多, "
                    f"检查规范化是否跳过了空 token 行"
                )
            else:
                slog.info("[SenseVoice] ✅ vocab 对齐 (识别异常若仍出现则与 vocab 无关)")

        self._state = EngineState.READY
        slog.info(f"[SenseVoice] 就绪 (model={onnx_file})")

    @staticmethod
    def _pick_onnx_file(model_dir: str, use_int8: bool, slog) -> tuple[Optional[str], str]:
        """
        选择可用的 ONNX 模型文件，检测 Git LFS 指针。

        该仓库的 onnx 是 LFS 大文件:
          - model.int8.onnx  ≈ 237MB (量化)
          - model_quant.onnx ≈ 241MB (动态量化)
          - model.onnx       ≈ 937MB (fp32)
        LFS 未拉取时只有几百字节的指针文件，os.path.exists 通过但无法加载。

        返回 (模型路径, 文件名)；所有候选无效返回 (None, "")。
        """
        # 候选: 优先 int8, 依次回退
        candidates = (
            ["model.int8.onnx", "model_quant.onnx", "model.onnx"]
            if use_int8 else
            ["model.onnx", "model.int8.onnx", "model_quant.onnx"]
        )

        # 最小合法大小: 真实 ONNX > 10MB; LFS 指针 < 1KB
        MIN_VALID_SIZE = 10 * 1024 * 1024

        for name in candidates:
            path = os.path.join(model_dir, name)
            if not os.path.exists(path):
                slog.info(f"[SenseVoice] {name} 不存在")
                continue
            size = os.path.getsize(path)
            if size < MIN_VALID_SIZE:
                # 检查是否为 LFS 指针 (文本格式 "version https://git-lfs...")
                try:
                    with open(path, "r", errors="ignore") as f:
                        head = f.read(128)
                    if "git-lfs" in head or head.startswith("version "):
                        slog.warning(f"[SenseVoice] {name} 是 Git LFS 指针 ({size}B)，大文件未拉取")
                    else:
                        slog.warning(f"[SenseVoice] {name} 异常小 ({size}B)")
                except Exception:
                    slog.warning(f"[SenseVoice] {name} 异常小 ({size}B)")
                continue
            slog.info(f"[SenseVoice] 选择 {name} ({size/1024/1024:.0f}MB)")
            return path, name

        # 诊断: 列出目录实际内容, 帮助判断模型是否兼容
        try:
            files = sorted(os.listdir(model_dir))
            slog.warning(f"[SenseVoice] 目录内容 ({model_dir}):")
            for fn in files[:20]:
                fp = os.path.join(model_dir, fn)
                if os.path.isfile(fp):
                    slog.warning(f"    {fn} ({os.path.getsize(fp)/1024/1024:.1f}MB)")
                else:
                    slog.warning(f"    {fn}/")
            if any(fn.endswith(".pt") or fn.endswith(".pth") or fn.endswith(".safetensors") for fn in files):
                slog.warning(
                    "[SenseVoice] ⚠️ 检测到 PyTorch 权重文件 (.pt/.pth) — "
                    "这是 Whisper 架构的 PyTorch 模型，不是 SenseVoice ONNX 格式，无法加载!"
                )
                slog.warning(
                    "[SenseVoice] 如需使用该模型请切换引擎: stt.engine: \"cpu_stt\" (faster-whisper)"
                )
                slog.warning(
                    "[SenseVoice] 或恢复 SenseVoice 模型: model_name: \"danieldong/sensevoice-small-onnx-quant\""
                )
        except Exception:
            pass

        return None, ""

    @staticmethod
    def _pad_tokens_file(tokens_path: str, missing: int, slog) -> str:
        """
        补齐 tokens 表: 追加 <|pad|> 占位 token 使行数与模型 vocab 一致。

        danieldong 导出模型时 vocab 常比官方 tokens.txt 多 1-2 个
        (padding 预留), 模型输出这些 id 时 sym 表越界 → _Map_base::at。
        """
        dst = tokens_path + ".padded"
        try:
            with open(tokens_path, encoding="utf-8") as f:
                lines = f.read()
            with open(dst, "w", encoding="utf-8") as f:
                f.write(lines)
                if not lines.endswith("\n"):
                    f.write("\n")
                for i in range(missing):
                    f.write(f"<|pad_{i}|>\n")
        except Exception as e:
            slog.warning(f"[SenseVoice] tokens 补齐失败: {e}")
            return tokens_path
        slog.info(f"[SenseVoice] tokens 已补齐 {missing} 个占位: {dst}")
        return dst

    @staticmethod
    def _normalize_tokens_file(src_path: str, slog) -> str:
        """
        规范化 tokens.txt (按第二列类型智能判断)。

        两种两列格式:
          1. "token id"   — 官方 sherpa-onnx 格式, id 是 int
             → 保留原样 (老版本 ReadTokens 用 id 列; 新版本兼容)
          2. "token score" — danieldong 社区格式, score 是 float
             → 老版本 ReadTokens 解析失败 ("Error: ▁the -3.22101")
             → 转为单列 (行号即 id)

        单列格式原样返回。
        """
        try:
            with open(src_path, encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            slog.warning(f"[SenseVoice] tokens.txt 读取失败: {e}")
            return src_path

        # 检测两列格式及第二列类型
        has_two_cols = False
        second_is_int = True
        for line in lines[:20]:
            parts = line.strip().split()
            if len(parts) >= 2:
                has_two_cols = True
                try:
                    int(parts[1])
                except ValueError:
                    second_is_int = False
                break

        if not has_two_cols:
            return src_path  # 单列, 原样

        if second_is_int:
            # 官方 "token id" 两列: 保留 (老版本需要 id 列)
            slog.debug("[SenseVoice] tokens 是官方 'token id' 两列格式，保留原样")
            return src_path

        # 社区 "token score" 两列: 转单列 (行号即 id)
        dst_path = src_path + ".clean"
        try:
            with open(dst_path, "w", encoding="utf-8") as f:
                for line in lines:
                    stripped = line.strip()
                    if not stripped:
                        continue  # 跳过空行 (sherpa-onnx 同样跳过)
                    token = stripped.split()[0]  # 取第一列
                    f.write(token + "\n")
        except Exception as e:
            slog.warning(f"[SenseVoice] tokens 规范化失败: {e}")
            return src_path

        slog.info(f"[SenseVoice] tokens.txt 是 'token score' 两列 → 已转换为单列: {dst_path}")
        return dst_path

    @staticmethod
    def _fix_sensevoice_metadata(model_path: str, tokens_path: str, slog) -> str:
        """
        修补 ONNX metadata — danieldong 社区模型缺少 sherpa-onnx 必需字段。

        参考 sherpa-onnx 官方导出脚本 (scripts/sense-voice/export-onnx.py):
          meta_data = {
            lfr_window_size, lfr_window_shift, normalize_samples,
            neg_mean (逗号分隔浮点串, 来自 am.mvn <LearnRateCoef> 第一块),
            inv_stddev (同上, 第二块),
            model_type="sense_voice_ctc", version="2",
            vocab_size, comment,
            lang_auto/zh/en/yue/ja/ko/nospeech (token id),
            with_itn/without_itn (textnorm token id),
          }

        返回修补后的模型路径 (写 .fixed.onnx, 原文件不动)。
        """
        model_dir = os.path.dirname(model_path)

        # vocab_size = tokens 行数
        try:
            with open(tokens_path, encoding="utf-8") as f:
                vocab_size = sum(1 for line in f if line.strip())
        except Exception:
            vocab_size = 25045  # SenseVoice 官方默认值

        # 特殊 token id — 官方硬编码值 (SenseVoice model.py)
        # 模型内部 embed 表只有 16 行 (id 0-15)，id 必须用官方定义，
        # 不能从 tokens 文件查找行号 (danieldong tokens 排序与官方 vocab 不同，
        # 查找会得到 25016 之类的大 id，导致 Gather 越界:
        #   "indices element out of data bounds, idx=25016 must be within [-16, 15]")
        lang_ids = {
            "lang_auto": 0,
            "lang_zh": 3,
            "lang_en": 4,
            "lang_yue": 7,
            "lang_ja": 11,
            "lang_ko": 12,
            "lang_nospeech": 13,
            "with_itn": 14,       # textnorm_dict["withitn"]
            "without_itn": 15,    # textnorm_dict["woitn"]
        }

        # neg_mean / inv_stddev 从 am.mvn 解析 (逗号分隔浮点串)
        neg_mean, inv_stddev = SenseVoiceSTTEngine._parse_am_mvn(model_dir, slog)

        updates = {
            "model_type": "sense_voice_ctc",
            "comment": "iic/SenseVoiceSmall",
            "maintainer": "k2-fsa",
            "language": "auto",
            "vocab_size": str(vocab_size),
            "lfr_window_size": "7",
            "lfr_window_shift": "1",
            "subsampling_factor": "4",
            "normalize_samples": "0",
            "version": "2",                    # QUInt8 量化版本标记
            "model_author": "iic",
            "fixed_revision": "2",             # 修补逻辑版本 (变更时递增 → 缓存失效)
            **{k: str(v) for k, v in lang_ids.items()},
        }
        if neg_mean:
            updates["neg_mean"] = neg_mean
        if inv_stddev:
            updates["inv_stddev"] = inv_stddev

        # 修补缓存: 存在、新于原文件、且包含全部必需字段 → 复用
        fixed_path = model_path + ".fixed.onnx"
        if os.path.exists(fixed_path) and os.path.getmtime(fixed_path) >= os.path.getmtime(model_path):
            try:
                with open(fixed_path, "rb") as f:
                    blob = f.read()
                missing = [k for k in updates if not _proto_has_key(blob, k)]
                if not missing:
                    slog.debug(f"[SenseVoice] 复用已修补模型: {fixed_path}")
                    return fixed_path
                slog.info(
                    f"[SenseVoice] 修补缓存缺少字段 ({', '.join(missing)})，重新修补"
                )
            except Exception as e:
                slog.debug(f"[SenseVoice] 修补缓存检查失败: {e}")

        try:
            import onnx
            has_onnx_lib = True
        except ImportError:
            has_onnx_lib = False

        if has_onnx_lib:
            return SenseVoiceSTTEngine._fix_metadata_with_onnx(
                model_path, fixed_path, updates, slog
            )
        # 零依赖回退: 纯 Python protobuf 操作 (不需要 pip install onnx)
        slog.info("[SenseVoice] onnx 库未安装，使用零依赖 protobuf 修补")
        return SenseVoiceSTTEngine._fix_metadata_raw_proto(
            model_path, fixed_path, updates, slog
        )

    @staticmethod
    def _parse_am_mvn(model_dir: str, slog) -> tuple[Optional[str], Optional[str]]:
        """
        解析 Kaldi am.mvn 文件 → (neg_mean, inv_stddev)。

        格式 (Kaldi nnet3):
          <AddShift> 560 560
          <LearnRateCoef> 0 [ -8.31 -8.60 ... ]     ← neg_mean (560 floats)
          <Rescale> 560 560
          <LearnRateCoef> 0 [ 0.35 0.34 ... ]       ← inv_stddev

        与官方 export-onnx.py load_cmvn() 一致:
          每行 <LearnRateCoef> split()[3:-1] 取浮点段, 逗号拼接。
        """
        mvn_path = os.path.join(model_dir, "am.mvn")
        if not os.path.exists(mvn_path):
            slog.warning(f"[SenseVoice] am.mvn 不存在: {mvn_path}")
            return None, None

        neg_mean: Optional[str] = None
        inv_stddev: Optional[str] = None
        try:
            with open(mvn_path, encoding="utf-8") as f:
                for line in f:
                    if not line.startswith("<LearnRateCoef>"):
                        continue
                    values = line.split()[3:-1]  # 跳过 "<LearnRateCoef> 0 [" 和尾部 "]"
                    if neg_mean is None:
                        neg_mean = ",".join(values)
                    else:
                        inv_stddev = ",".join(values)
        except Exception as e:
            slog.warning(f"[SenseVoice] am.mvn 解析失败: {e}")
            return None, None

        if neg_mean:
            slog.info(
                f"[SenseVoice] am.mvn 解析: neg_mean={len(neg_mean.split(','))} 值, "
                f"inv_stddev={'有' if inv_stddev else '无'}"
            )
        return neg_mean, inv_stddev

    @staticmethod
    def _fix_metadata_with_onnx(model_path, fixed_path, updates, slog) -> str:
        """用 onnx 库修补 (路径1, 需 pip install onnx)"""
        import onnx
        try:
            model = onnx.load(model_path)
        except Exception as e:
            slog.warning(f"[SenseVoice] onnx.load 失败: {e}")
            return model_path

        existing = {p.key for p in model.metadata_props}
        added = []
        for key, value in updates.items():
            if key not in existing:
                model.metadata_props.append(
                    onnx.StringStringEntryProto(key=key, value=value)
                )
                added.append(key)

        if not added:
            slog.debug("[SenseVoice] metadata 已完整，无需修补")
            return model_path

        try:
            onnx.save(model, fixed_path)
        except Exception as e:
            slog.warning(f"[SenseVoice] metadata 修补保存失败: {e}")
            return model_path

        slog.info(f"[SenseVoice] 已修补 metadata ({len(added)} 字段): {', '.join(added)}")
        return fixed_path

    @staticmethod
    def _fix_metadata_raw_proto(model_path, fixed_path, updates, slog) -> str:
        """
        零依赖修补: 纯 Python protobuf wire format 操作。

        ONNX 文件 = ModelProto 序列化字节流。metadata_props 是 field 14
        (repeated StringStringEntryProto)，StringStringEntryProto 的
        key=field 1, value=field 2。protobuf 字段顺序无关，把新条目
        追加到消息末尾即可，onnxruntime 能正确解析。
        """
        try:
            with open(model_path, "rb") as f:
                data = bytearray(f.read())
        except Exception as e:
            slog.warning(f"[SenseVoice] 模型读取失败: {e}")
            return model_path

        existing = {key for key in updates if _proto_has_key(bytes(data), key)}

        added = []
        for key, value in updates.items():
            if key in existing:
                continue
            # StringStringEntryProto: field1=key, field2=value
            entry = bytearray()
            kb = key.encode()
            entry.append(0x0A)  # field 1, wire type 2 (length-delimited)
            entry += _proto_varint(len(kb))
            entry += kb
            vb = value.encode()
            entry.append(0x12)  # field 2, wire type 2
            entry += _proto_varint(len(vb))
            entry += vb
            # ModelProto field 14: metadata_props, tag = (14<<3)|2 = 0x72
            data.append(0x72)
            data += _proto_varint(len(entry))
            data += entry
            added.append(key)

        if not added:
            slog.debug("[SenseVoice] metadata 已完整，无需修补")
            return model_path

        try:
            with open(fixed_path, "wb") as f:
                f.write(data)
        except Exception as e:
            slog.warning(f"[SenseVoice] metadata 修补写入失败: {e}")
            return model_path

        slog.info(f"[SenseVoice] 已修补 metadata ({len(added)} 字段): {', '.join(added)}")
        return fixed_path

    @staticmethod
    def _has_pytorch_weights(model_dir: str) -> bool:
        """检测目录中是否只有 PyTorch 权重 (Whisper 格式, 非 SenseVoice ONNX)"""
        try:
            files = os.listdir(model_dir)
            has_pt = any(fn.endswith((".pt", ".pth", ".safetensors")) for fn in files)
            has_onnx = any(fn.endswith(".onnx") for fn in files)
            return has_pt and not has_onnx
        except Exception:
            return False

    @staticmethod
    def _create_recognizer(sherpa_onnx, model_path: str, tokens_path: str,
                           num_threads: int, use_itn: bool, language: str = "auto"):
        """
        创建 sherpa-onnx SenseVoice recognizer (在线程池中运行)。

        关键: 必须显式传 language="auto"。
        不传时 C++ 端 language 为空字符串, 识别时
        lang2id.at("") 抛 std::out_of_range → "_Map_base::at" 异常。
        """
        try:
            # 新版 API: use_itn + language
            return sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=model_path,
                tokens=tokens_path,
                num_threads=num_threads,
                use_itn=use_itn,
                language=language,
                debug=False,
            )
        except TypeError:
            try:
                # 中间版本: 有 language 无 use_itn
                return sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=model_path,
                    tokens=tokens_path,
                    num_threads=num_threads,
                    language=language,
                    debug=False,
                )
            except TypeError:
                # 旧版本: 均无 (language 由内部默认)
                return sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=model_path,
                    tokens=tokens_path,
                    num_threads=num_threads,
                    debug=False,
                )

    async def _download_official_model(self, slog) -> Optional[str]:
        """
        下载 sherpa-onnx 官方发布模型 (metadata 完整, ITN/homophone 兼容)。

        仓库: csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17
        文件: model.onnx (fp32) + tokens.txt
        通过 hf-mirror.com 镜像下载 (国内可达)。
        """
        import httpx

        repo = os.environ.get(
            "SENSEVOICE_OFFICIAL_REPO",
            "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
        )
        base = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com").rstrip("/")
        cache_dir = os.path.expanduser(
            f"~/.cache/stackchan/sensevoice-official/{repo.replace('/', '--')}"
        )
        os.makedirs(cache_dir, exist_ok=True)

        files = ["model.onnx", "tokens.txt"]
        for fn in files:
            dst = os.path.join(cache_dir, fn)
            # 已存在且大小合理 → 跳过
            if os.path.exists(dst) and os.path.getsize(dst) > 1000:
                continue
            url = f"{base}/{repo}/resolve/main/{fn}"
            slog.info(f"[SenseVoice] 下载官方模型: {url}")
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(300, connect=30), follow_redirects=True
                ) as client:
                    r = await client.get(url)
                    if r.status_code != 200:
                        slog.warning(
                            f"[SenseVoice] 官方模型下载失败: HTTP {r.status_code} ({fn})"
                        )
                        return None
                    with open(dst, "wb") as f:
                        f.write(r.content)
                slog.info(f"[SenseVoice] {fn} 下载完成 ({os.path.getsize(dst)/1024/1024:.0f}MB)")
            except Exception as e:
                slog.warning(f"[SenseVoice] 官方模型下载异常: {e}")
                return None

        return cache_dir

    async def _download_model(self, slog) -> Optional[str]:
        """从 ModelScope 下载 SenseVoice 模型"""
        model_name = self._config.extra.get("model_name", self._DEFAULT_MODEL) if self._config else self._DEFAULT_MODEL
        model_name = os.environ.get("MODELSCOPE_SENSEVOICE_MODEL", model_name)

        slog.info(f"[SenseVoice] ModelScope 下载: {model_name}")
        try:
            from modelscope import snapshot_download
            local = await asyncio.wait_for(
                asyncio.to_thread(snapshot_download, model_name),
                timeout=600.0,  # 237MB, 给足时间
            )
            slog.info(f"[SenseVoice] 下载完成: {local}")
            return local
        except ImportError:
            slog.warning("modelscope 未安装。pip install modelscope")
            return None
        except asyncio.TimeoutError:
            slog.warning("ModelScope 下载超时 (600s)")
            return None
        except Exception as e:
            slog.warning(f"ModelScope 下载失败: {e}")
            return None

    async def _redownload_after_cleanup(self, slog) -> Optional[str]:
        """
        清理损坏的模型缓存后重新下载。

        LFS 指针残留的典型场景: 旧版本 modelscope 下载的目录未拉取大文件，
        snapshot_download 看到目录已存在就跳过，导致永远拿到指针文件。
        """
        model_name = self._config.extra.get("model_name", self._DEFAULT_MODEL) if self._config else self._DEFAULT_MODEL
        model_name = os.environ.get("MODELSCOPE_SENSEVOICE_MODEL", model_name)

        # ModelScope 缓存路径: ~/.cache/modelscope/hub/models/<org>--<name>/
        cache_base = os.path.expanduser("~/.cache/modelscope/hub/models")
        org_name = model_name.replace("/", "--")
        cache_dir = os.path.join(cache_base, org_name)

        try:
            import shutil
            if os.path.isdir(cache_dir):
                slog.info(f"[SenseVoice] 删除损坏缓存: {cache_dir}")
                await asyncio.to_thread(shutil.rmtree, cache_dir, True)
        except Exception as e:
            slog.warning(f"[SenseVoice] 缓存清理失败: {e}")

        return await self._download_model(slog)

    # ------------------------------------------------------------------
    # 流式接口 (与 CpuSTTEngine 相同的累积式策略)
    # ------------------------------------------------------------------

    async def feed(self, audio_chunk: bytes) -> Optional[STTResult]:
        """
        喂入 PCM 16-bit 16kHz mono 音频。

        累积式整体识别: 静音断句 / 过长截断 触发识别。
        """
        if not self._recognizer:
            if not getattr(self, "_warned_not_ready", False):
                self._warned_not_ready = True
                self._slog("SenseVoice 未就绪 (模型未加载，检查启动日志)")
            return None

        self._cancel.clear()

        # 块计数 (低频累积日志)
        self._feed_count = getattr(self, "_feed_count", 0) + 1

        # RMS 静音检测
        rms_val = float(np.sqrt(np.mean(
            np.frombuffer(audio_chunk, dtype=np.int16).astype(np.float32) ** 2
        )))

        chunk_ms = len(audio_chunk) / 32.0  # 16kHz * 2B = 32 B/ms
        if rms_val < 120.0:
            self._silence_ms += chunk_ms
        else:
            self._silence_ms = 0.0

        self._buffer.extend(audio_chunk)
        buf_ms = len(self._buffer) / 32.0

        # 每 10 块打印累积状态 (250ms 块 → 每 2.5 秒一次)
        if self._feed_count % 10 == 0:
            self._slog(
                f"累积: {self._feed_count} 块, buffer={buf_ms/1000:.1f}s, "
                f"静音={self._silence_ms:.0f}ms, 本块RMS={rms_val:.0f}"
            )

        silence_break = self._config.extra.get("silence_break_ms", 800) if self._config else 800
        max_audio = self._config.extra.get("max_audio_ms", 15000) if self._config else 15000

        trigger = None
        if self._silence_ms >= silence_break and buf_ms > 400:
            trigger = f"静音断句 ({self._silence_ms:.0f}ms)"
        elif buf_ms >= max_audio:
            trigger = f"过长截断 ({buf_ms:.0f}ms)"

        if trigger is None:
            return None

        self._slog(f"触发识别: {trigger} buf={buf_ms:.1f}s RMS={rms_val:.0f}")
        return await self._recognize(is_final=False)

    async def finalize(self) -> Optional[STTResult]:
        """最终识别 — 编排层在 audio.commit 时调用"""
        if len(self._buffer) < 3200:  # < 100ms
            self._buffer.clear()
            self._silence_ms = 0.0
            return None
        self._slog(f"最终识别: buf={len(self._buffer)/32.0:.1f}s")
        return await self._recognize(is_final=True)

    async def _recognize(self, is_final: bool) -> Optional[STTResult]:
        """对累积音频执行 SenseVoice 识别"""
        self._state = EngineState.RUNNING
        pcm = np.frombuffer(bytes(self._buffer), dtype=np.int16).astype(np.float32) / 32768.0
        self._buffer.clear()
        self._silence_ms = 0.0
        return await self._recognize_pcm(pcm, is_final)

    async def _recognize_pcm(self, pcm: np.ndarray, is_final: bool) -> Optional[STTResult]:
        """
        对给定 PCM 执行识别 (可重试复用同一音频)。

        分离出来是因为降级重试时 self._buffer 已被清空,
        必须复用当前 pcm 而非重新读取缓冲区。
        """
        try:
            result = await asyncio.to_thread(self._run_recognition, pcm)
            text, lang = self._parse_result(result)
            self._accumulated = text
            self._state = EngineState.READY

            if text:
                self._detected_lang = lang
                return STTResult(
                    text=text, is_partial=not is_final,
                    confidence=1.0, language=lang or None,
                )
            return None

        except Exception as e:
            self._map_error_count += 1
            self._slog(
                f"识别异常 #{self._map_error_count} ({type(e).__name__}): {e}"
            )

            # 运行时自愈: std::map 异常疑似 ITN 后处理崩溃
            # → 自动降级 use_itn=False 重建 recognizer 重试一次 (同一 pcm)
            if not self._tried_no_itn and self._rec_args:
                self._tried_no_itn = True
                self._slog("疑似 ITN 后处理崩溃，降级 use_itn=False 重建并重试...")
                try:
                    await self._rebuild_recognizer(use_itn=False)
                    return await self._recognize_pcm(pcm, is_final)
                except Exception as rb_err:
                    self._slog(f"降级重建失败: {rb_err}")

            # 保持 READY: 单个坏音频块不应杀死引擎
            self._state = EngineState.READY
            return None

    async def _rebuild_recognizer(self, use_itn: Optional[bool] = None) -> None:
        """
        用保存的参数重建 recognizer (可覆盖 use_itn)。

        用于运行时自愈: ITN 崩溃时降级重建。
        """
        args = dict(self._rec_args)
        if use_itn is not None:
            args["use_itn"] = use_itn
        sn = args.pop("sherpa_onnx")
        self._recognizer = await asyncio.to_thread(
            self._create_recognizer,
            sn,
            args["model_path"],
            args["tokens_path"],
            args["num_threads"],
            args["use_itn"],
            args["language"],
        )
        self._rec_args["use_itn"] = args["use_itn"]
        self._slog(f"recognizer 已重建 (use_itn={args['use_itn']})")

    def _run_recognition(self, samples: np.ndarray) -> str:
        """sherpa-onnx 同步推理 (线程池中运行)"""
        stream = self._recognizer.create_stream()
        stream.accept_waveform(16000, samples)
        self._recognizer.decode_stream(stream)
        return stream.result.text

    def _parse_result(self, raw: str) -> tuple[str, str]:
        """
        解析 SenseVoice 输出。

        输入: <|zh|><|NEUTRAL|><|Speech|><|woitn|>今天天气真好
        输出: ("今天天气真好", "zh")
        """
        if not raw:
            return "", ""

        # 提取语言标记
        lang = ""
        m = re.match(r"<\|([a-zA-Z]+)\|>", raw)
        if m:
            lang = _LANG_MAP.get(m.group(1), m.group(1))

        # 移除所有特殊标记
        text = _TAG_RE.sub("", raw).strip()
        return text, lang

    # ------------------------------------------------------------------
    # 控制接口
    # ------------------------------------------------------------------

    async def cancel(self) -> None:
        self._cancel.set()
        self._state = EngineState.CANCELLED

    async def reset(self) -> None:
        self._cancel.clear()
        self._buffer.clear()
        self._accumulated = ""
        self._silence_ms = 0.0
        self._feed_count = 0
        self._map_error_count = 0  # 每轮重置计数; _tried_no_itn 保留 (降级持久)
        # 仅在模型已加载时恢复 READY，否则保持原状态 (避免假 READY)
        if self._recognizer is not None:
            self._state = EngineState.READY

    async def health_check(self) -> bool:
        return self._recognizer is not None

    def _slog(self, msg: str) -> None:
        get_session_logger("system").info(f"[SenseVoice] {msg}")
