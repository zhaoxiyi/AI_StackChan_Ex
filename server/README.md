# StackChan Server - 全双工 AI 对话上位机

> AI StackChan 桌面机器人的本地私有化全双工语音对话上位机服务。

## 概述

StackChan Server 是一个完全兼容 [AI_StackChan_Ex](https://github.com/ronron-gh/AI_StackChan_Ex) 固件端 **OpenAI Realtime API 协议**的本地服务，替代云端 Realtime API，实现私有化部署的全双工语音对话。

### 核心特性

- **固件零修改**：完全兼容固件原生 Realtime 协议，仅需修改配置地址即可对接
- **双模型路由**：日常闲聊走本地 vLLM，复杂任务走 DeepSeek 云端
- **全双工流式对话**：边听边识别、边生成边播放、随时打断
- **DeepSeek 余额检测**：两级阈值预警，余额不足自动降级并语音通知
- **完整的对话状态机**：idle / listening / processing / speaking / interrupted
- **会话级全链路追踪**：基于 loguru，按 session_id 追踪每个会话

## 系统架构

严格四层解耦架构，层间通过抽象接口交互，禁止跨层调用：

```
┌─────────────────────────────────────────────────────┐
│                 固件端 (AI_StackChan_Ex)              │
│          M5Stack CoreS3 / Core2 / AtomS3R            │
└────────────────────┬────────────────────────────────┘
                     │ WebSocket (Realtime API Protocol)
                     │ base64 PCM 16kHz → base64 PCM 24kHz
┌────────────────────┴────────────────────────────────┐
│ Layer 3: 接入协议层 (protocol/)                       │
│  - WebSocket 连接管理                                 │
│  - Realtime API JSON 协议解析/生成                     │
│  - 会话映射、消息路由                                   │
└────────────────────┬────────────────────────────────┘
                     │
┌────────────────────┴────────────────────────────────┐
│ Layer 2: 会话编排层 (orchestration/)                   │
│  - Loop Engine: STT → LLM → TTS 流水线编排             │
│  - 对话状态机: idle/listening/processing/speaking      │
│  - 中断管理、模型路由、上下文维护                         │
└──────┬──────────┬──────────┬────────────────────────┘
       │          │          │
┌──────┴──┐ ┌────┴───┐ ┌───┴─────────┐
│  STT    │ │  LLM   │ │    TTS      │
│ Engine  │ │ Engine │ │   Engine    │
└──────┬──┘ └────┬───┘ └───┬─────────┘
       │         │         │
┌──────┴─────────┴─────────┴──────────────────────────┐
│ Layer 1: 能力引擎层 (engines/)                         │
│  - LLM: VLLMEngine, DeepSeekEngine                    │
│  - STT: FasterWhisperEngine                            │
│  - TTS: OpenAITTSEngine                                │
│  纯能力封装，支持 cancel 中断，无业务逻辑                 │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│ Layer 4: 设备抽象层 (devices/)                         │
│  统一硬件指令格式，屏蔽固件版本差异                      │
└─────────────────────────────────────────────────────┘
```

## 快速开始

### 1. 环境要求

- Python 3.10+
- 本地 vLLM 服务（推荐，可用其他 OpenAI 兼容接口替代）
- 可选：NVIDIA GPU（用于本地 Faster-Whisper STT）

### 2. 安装

```bash
cd server
pip install -r requirements.txt

# 可选：安装本地语音识别引擎
pip install faster-whisper silero-vad
```

### 3. 配置

编辑 `server/config.yaml`：

```yaml
routing:
  models:
    vllm:
      base_url: "http://localhost:8000/v1"   # 指向你的 vLLM 服务
      model_name: "Qwen/Qwen2.5-7B-Instruct"
    deepseek:
      enabled: true
      api_key: "${DEEPSEEK_API_KEY}"          # 通过环境变量设置

stt:
  engine: "faster_whisper"                   # 本地语音识别
  # engine: "openai_whisper"                 # 云端语音识别

tts:
  engine: "openai_tts"                       # OpenAI TTS
  # engine: "voicevox"                       # 日语 VOICEVOX
```

### 4. 启动

```bash
cd server

# 设置环境变量
export DEEPSEEK_API_KEY="sk-xxxxxx"
export OPENAI_API_KEY="sk-xxxxxx"

# 启动服务
python -m stackchan_server.main
```

### 5. 固件端配置

修改固件的 Realtime API 地址为本机服务地址：

在 `RealtimeChatGPT.cpp` 第 307 行，修改连接地址：

```cpp
// 原始代码:
// webSocket.beginSslWithCA("api.openai.com", 443, "/v1/realtime?model=gpt-realtime", root_ca_openai);

// 修改为（如果使用 HTTP，需改 SSL 设置）:
webSocket.begin("192.168.1.100", 8080, "/v1/realtime?model=gpt-realtime");
```

> 注意：固件端改为非 SSL 连接需要移除 `beginSslWithCA` 并处理 TLS 相关代码。
> 后续版本将通过固件的 `customEndpoint` 配置直接支持 HTTP 连接。

## 协议兼容性

### 支持的 Realtime API 事件

| 客户端 → 服务器 | 服务器 → 客户端 | 状态 |
|---------------|---------------|------|
| `session.update` | `session.updated` | ✅ |
| `input_audio_buffer.append` | `input_audio_buffer.speech_started` | ✅ |
| `input_audio_buffer.commit` | `input_audio_buffer.speech_stopped` | ✅ |
| `input_audio_buffer.clear` | `input_audio_buffer.committed` | ✅ |
| `response.create` | `response.output_audio.delta` | ✅ |
| `response.cancel` (interrupt) | `response.output_audio.done` | ✅ |
| `conversation.item.create` | `response.output_audio_transcript.delta` | ✅ |
| - | `response.done` | ✅ |
| - | `error` | ✅ |
| - | `rate_limits.updated` | ✅ |

### 音频格式

| 参数 | 固件 → 服务器 | 服务器 → 固件 |
|------|-------------|-------------|
| 编码 | PCM 16-bit | PCM 16-bit |
| 采样率 | 16000 Hz (实际) | 24000 Hz |
| 声道 | 单声道 | 单声道 |
| 传输 | Base64 编码 | Base64 编码 |
| 分片 | 2000 samples (125ms) | 可变大小 |

## API 端点

### WebSocket

```
ws://host:8080/v1/realtime?model=gpt-realtime
```

### HTTP

| 端点 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 健康检查 |
| `/status` | GET | 详细状态（含引擎健康） |
| `/models` | GET | 可用模型列表 |
| `/balance` | GET | DeepSeek 余额查询 |

## DeepSeek 余额管理

两级阈值预警机制：

```yaml
billing:
  balance_warning_threshold: 10.0    # 低于此值发出警告
  balance_critical_threshold: 2.0    # 低于此值自动降级到本地模型
  check_interval_minutes: 60         # 余额检查间隔
```

当余额低于 critical 阈值时，系统自动将所有请求路由到本地 vLLM 模型，并通过 TTS 语音通知用户。

## 项目结构

```
server/
├── config.yaml                 # 主配置文件
├── requirements.txt            # Python 依赖
├── README.md                   # 本文档
└── stackchan_server/
    ├── __init__.py
    ├── main.py                 # FastAPI 入口
    ├── config.py               # 配置管理 (pydantic + yaml)
    ├── logging_config.py       # 日志配置 (loguru)
    ├── api/                    # API 接入层
    │   ├── ws_handler.py       # WebSocket 端点
    │   └── http_routes.py      # HTTP 端点
    ├── protocol/               # Layer 3: 接入协议层
    │   ├── messages.py         # 协议消息定义
    │   └── session.py          # 会话管理
    ├── orchestration/          # Layer 2: 会话编排层
    │   ├── state_machine.py    # 对话状态机 + 模型路由
    │   └── loop_engine.py      # Loop Engine 主循环
    ├── engines/                # Layer 1: 能力引擎层
    │   ├── base.py             # 抽象接口定义
    │   ├── llm/
    │   │   ├── vllm_engine.py      # 本地 vLLM 引擎
    │   │   └── deepseek_engine.py  # DeepSeek 云端引擎
    │   ├── stt/
    │   │   └── whisper_engine.py   # Faster-Whisper 引擎
    │   └── tts/
    │       └── openai_tts_engine.py # OpenAI TTS 引擎
    ├── devices/                # Layer 4: 设备抽象层
    │   └── stackchan_device.py # StackChan 设备抽象
    └── utils/
        └── audio_utils.py      # 音频工具函数
```

## 开发计划

### Phase 1 (当前)
- [x] 完全兼容固件原生 Realtime 协议
- [x] 双模型路由 (vLLM + DeepSeek)
- [x] 全双工流式对话 + 打断
- [x] DeepSeek 余额检测
- [x] 完整对话状态机

### Phase 2 (规划)
- [ ] 固件端 customEndpoint 原生支持 HTTP
- [ ] Function Calling 完整实现
- [ ] MCP 集成
- [ ] 长期记忆（对话摘要持久化）
- [ ] 多会话管理
- [ ] 伺服控制指令下发

### Phase 3 (规划)
- [ ] Web 管理界面
- [ ] 语音角色实时切换
- [ ] 声纹识别（多用户）
- [ ] 本地 Opus 编解码
- [ ] Gemini Live API 兼容

## 许可证

MIT License - 与 AI_StackChan_Ex 项目保持一致。
