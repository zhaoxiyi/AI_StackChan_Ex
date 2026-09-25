# StackChan Server — 模型配置指南

## STT 引擎 (语音识别)

### 引擎类型

| engine | 说明 | 依赖 |
|--------|------|------|
| `sensevoice` | **推荐** — 阿里 SenseVoice，ModelScope 下载，中/粤/日/英/韩自动检测，情感识别 | `pip install sherpa-onnx modelscope` |
| `cpu_stt` | faster-whisper 纯 CPU 版 | `pip install faster-whisper` |
| `faster_whisper` | GPU 加速版 | `pip install faster-whisper` (需 CUDA) |

### SenseVoice 配置 (默认引擎)

```yaml
stt:
  engine: "sensevoice"
  sensevoice:
    model_name: "danieldong/sensevoice-small-onnx-quant"  # ModelScope 模型 ID
    use_int8: true               # True=model.int8.onnx (237MB), False=model.onnx (937MB)
    num_threads: 4               # onnxruntime 线程数
    use_itn: true                # 逆文本归一化 (数字/日期自动转写)
    silence_break_ms: 800        # 静音断句阈值
    max_audio_ms: 15000          # 音频最长累积
```

安装依赖:
```bash
pip install sherpa-onnx modelscope
```

SenseVoice 特点:
- 模型 237MB (int8 量化)，纯 CPU 即可实时
- 自动语言检测: 中文/粤语/日语/英语/韩语
- 情感识别 + 事件检测 (原始输出含 `<|zh|><|NEUTRAL|><|Speech|>` 标记，引擎自动清理)
- 输出解析: 提取语言标记 + 纯文本

### 通用配置 (`config.yaml`)

```yaml
stt:
  engine: "cpu_stt"
  cpu_stt:
    # --- 模型选型 ---
    model_source: "huggingface"    # huggingface | modelscope | local
    model_size: "small"            # tiny | base | small | medium
    model_name: ""                 # ModelScope 模型 ID 或 local 路径

    # --- 运行时 ---
    device: "auto"                 # auto | cpu | cuda
    compute_type: "auto"           # auto | float16 | int8
    language: null                 # null=自动检测, "zh", "ja", "en"
    beam_size: 5
    use_vad: true
```

### 模型大小对比

| model_size | 参数量 | 磁盘 | 速度 (CPU) | 准确率 | 适用 |
|-----------|--------|------|-----------|--------|------|
| `tiny` | 39M | ~75MB | 极快 | ★★ | 快速测试 |
| `base` | 74M | ~145MB | 快 | ★★★ | 低配设备 |
| `small` | 244M | ~480MB | 中 | ★★★★ | **推荐** |
| `medium` | 769M | ~1.5GB | 慢 | ★★★★★ | 高精度需求 |

### model_source 说明

#### 1. huggingface (默认)

自动从 HuggingFace 下载 `Systran/faster-whisper-{model_size}`。国内自动走 `hf-mirror.com` 镜像。

```yaml
cpu_stt:
  model_source: "huggingface"
  model_size: "small"
```

#### 2. modelscope

从 ModelScope 下载 CTranslate2 格式模型。

```yaml
cpu_stt:
  model_source: "modelscope"
  model_name: "org/model-id"      # ModelScope 模型 ID
```

模型 ID 来源（三选一，按优先级）:
1. **环境变量**: `export MODELSCOPE_STT_MODEL="org/model-id"`
2. **config.yaml** 的 `model_name` 字段
3. 支持逗号分隔多个候选 ID，依次尝试: `"id-1,id-2,id-3"`

**如何找到可用的模型 ID**:
1. 打开 https://modelscope.cn 搜索 `faster-whisper`
2. 打开模型主页确认文件列表包含 `model.bin`（CTranslate2 格式标志）
3. 将 `org/模型名` 填入配置

**格式校验**: 下载后引擎会自动检查 `model.bin` 是否存在：
- 存在 → CTranslate2 格式，直接加载
- 缺失（如 `iic/SenseVoiceSmall` 是 PyTorch 格式）→ 自动尝试下一候选

**下载失败自动回退**: 所有 ModelScope 候选失败（404/超时/格式不兼容）时，
自动回退 HuggingFace（走 `hf-mirror.com` 国内镜像），服务不中断。

#### 3. local

使用本地已下载的模型目录。

```yaml
cpu_stt:
  model_source: "local"
  model_name: "/home/user/models/faster-whisper-small"
```

### compute_type 说明

| compute_type | 适用设备 | 精度 | 速度 |
|-------------|---------|------|------|
| `auto` | GPU/CPU | 自动 | 自动 |
| `float16` | GPU only | 高 | 快 |
| `int8_float16` | GPU | 中高 | 很快 |
| `int8` | CPU/GPU | 中 | 快 |

> CPU 推荐 `auto` 或 `int8`。`int8` 量化后准确率略有下降但速度显著提升。

---

## TTS 引擎 (语音合成)

### 引擎类型

| engine | 说明 | 依赖 |
|--------|------|------|
| `cpu_tts` | 纯 CPU 内嵌式，零部署 | `pip install edge-tts` |
| `openai_tts` | OpenAI 云端 TTS | `OPENAI_API_KEY` |

### 配置

```yaml
tts:
  engine: "cpu_tts"
  cpu_tts:
    voice: "zh-CN-XiaoxiaoNeural"  # 语音角色
    rate: "+0%"                    # 语速 -20% ~ +20%
    pitch: "+0Hz"                  # 音调 -20Hz ~ +20Hz
```

### edge-tts 语音角色

| 语言 | voice | 风格 |
|------|-------|------|
| 中文 (女) | `zh-CN-XiaoxiaoNeural` | 温柔 |
| 中文 (男) | `zh-CN-YunxiNeural` | 沉稳 |
| 中文 (男) | `zh-CN-YunyangNeural` | 新闻播音 |
| 日语 (女) | `ja-JP-NanamiNeural` | 活泼 |
| 日语 (男) | `ja-JP-KeitaNeural` | 青年 |
| 英语 (女) | `en-US-JennyNeural` | 自然 |
| 英语 (男) | `en-US-GuyNeural` | 成熟 |

---

## LLM 引擎

### 配置

```yaml
routing:
  default_model: "vllm"
  models:
    vllm:
      enabled: true
      base_url: "http://localhost:8000/v1"
      model_name: "Qwen/Qwen2.5-7B-Instruct"
      max_tokens: 2048
    deepseek:
      enabled: false
      api_key: "${DEEPSEEK_API_KEY}"
      model_name: "deepseek-chat"
      base_url: "https://api.deepseek.com/v1"
```

---

## 环境变量

| 变量 | 作用 | 默认值 |
|------|------|--------|
| `DEEPSEEK_API_KEY` | DeepSeek API 密钥 | - |
| `OPENAI_API_KEY` | OpenAI TTS 密钥 | - |
| `HF_ENDPOINT` | HuggingFace 镜像 | `https://hf-mirror.com` |
| `STACKCHAN_CONFIG` | 配置文件路径 | `server/config.yaml` |
| `STACKCHAN_SERVER_PORT` | 服务端口 | `8080` |
