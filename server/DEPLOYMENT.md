# StackChan Server 部署指南

> 面向运维和开发者双视角的完整部署文档。

## 目录

- [1. 环境要求](#1-环境要求)
- [2. 快速部署 (5分钟)](#2-快速部署-5分钟)
- [3. vLLM 模型部署](#3-vllm-模型部署)
- [4. 配置文件指南](#4-配置文件指南)
- [5. 固件端对接](#5-固件端对接)
- [6. 启动与运维](#6-启动与运维)
- [7. 常见问题排查](#7-常见问题排查)
- [8. 性能参考](#8-性能参考)
- [9. 扩展路线图](#9-扩展路线图)

---

## 1. 环境要求

| 组件 | 最低要求 | 推荐配置 |
|------|---------|---------|
| Python | 3.10+ | 3.12+ |
| 内存 | 2 GB | 8 GB |
| 磁盘 | 1 GB | 10 GB (含 Whisper 模型) |
| GPU (可选) | - | NVIDIA 8GB+ VRAM |
| 网络 | 可访问 DeepSeek API | 低延迟宽带 |

---

## 2. 快速部署 (5分钟)

```bash
# 1. 进入服务端目录
cd server

# 2. 安装核心依赖
pip install -r requirements.txt

# 3. 配置 API 密钥 (环境变量)
export DEEPSEEK_API_KEY="sk-your-key-here"
export OPENAI_API_KEY="sk-your-key-here"

# 4. (可选) 安装本地语音识别
pip install faster-whisper silero-vad

# 5. (可选) 安装 TTS 引擎
pip install edge-tts

# 6. 启动服务
python -m stackchan_server.main --port 8080

# 7. 验证
curl http://localhost:8080/health
# → {"status":"ok","version":"0.2.0","service":"stackchan-server"}
```

---

## 3. vLLM 模型部署

### 3.1 模型选型建议

| 模型 | 显存 | 速度 | 日语能力 | 推荐场景 |
|------|------|------|---------|---------|
| Qwen2.5-7B-Instruct | 6 GB | 快 | ★★★ | 通用推荐 |
| Qwen2.5-14B-Instruct | 12 GB | 中 | ★★★★ | 复杂任务 |
| gemma-2-9b-it | 6 GB | 快 | ★★★ | 英文优先 |
| Llama-3.1-8B-Instruct | 6 GB | 快 | ★★ | 备选 |
| Qwen2.5-3B-Instruct | 3 GB | 极快 | ★★ | 低配设备 |

### 3.2 vLLM 启动命令

```bash
# === 推荐配置 (Qwen2.5-7B, 8GB VRAM) ===
python -m vllm.entrypoints.openai.api_server \
    --model Qwen/Qwen2.5-7B-Instruct \
    --host 0.0.0.0 \
    --port 8000 \
    --max-model-len 4096 \
    --gpu-memory-utilization 0.90 \
    --dtype auto \
    --enable-prefix-caching

# === 低显存 (Qwen2.5-3B, 4GB VRAM) ===
python -m vllm.entrypoints.openai.api_server \
    --model Qwen/Qwen2.5-3B-Instruct \
    --host 0.0.0.0 \
    --port 8000 \
    --max-model-len 2048 \
    --gpu-memory-utilization 0.85 \
    --dtype half

# === 高配 (Qwen2.5-14B, 16GB+ VRAM) ===
python -m vllm.entrypoints.openai.api_server \
    --model Qwen/Qwen2.5-14B-Instruct \
    --host 0.0.0.0 \
    --port 8000 \
    --max-model-len 8192 \
    --gpu-memory-utilization 0.92 \
    --tensor-parallel-size 1 \
    --enable-prefix-caching

# === AWQ 量化 (节省 50% 显存) ===
python -m vllm.entrypoints.openai.api_server \
    --model Qwen/Qwen2.5-7B-Instruct-AWQ \
    --host 0.0.0.0 \
    --port 8000 \
    --quantization awq \
    --max-model-len 4096 \
    --gpu-memory-utilization 0.90
```

### 3.3 验证 vLLM 服务

```bash
curl http://localhost:8000/v1/models
# → {"object":"list","data":[{"id":"Qwen/Qwen2.5-7B-Instruct",...}]}

curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen2.5-7B-Instruct","messages":[{"role":"user","content":"Hello"}],"max_tokens":10}'
```

---

## 4. 配置文件指南

### 4.1 最小配置 (server/config.yaml)

```yaml
server:
  host: "0.0.0.0"
  port: 8080

routing:
  default_model: "vllm"
  models:
    vllm:
      enabled: true
      model_name: "Qwen/Qwen2.5-7B-Instruct"
      base_url: "http://localhost:8000/v1"
      api_key: "not-needed"
    deepseek:
      enabled: true
      api_key: "${DEEPSEEK_API_KEY}"
      model_name: "deepseek-chat"
      base_url: "https://api.deepseek.com/v1"

conversation:
  system_prompt: |
    You are Stack-chan, a friendly desktop robot assistant.
    Keep responses short (1-3 sentences).
```

### 4.2 环境变量覆盖

所有配置项都可以通过环境变量覆盖（`STACKCHAN_` 前缀）：

```bash
export STACKCHAN_SERVER_PORT=9090
export STACKCHAN_ROUTING_DEFAULT_MODEL=deepseek
export STACKCHAN_ROUTING_MODELS_VLLM_BASE_URL=http://gpu-server:8000/v1
```

### 4.3 DeepSeek 余额告警

```yaml
routing:
  models:
    deepseek:
      billing:
        warning_threshold_cny: 10.0    # 余额 < 10 CNY → 日志告警
        critical_threshold_cny: 2.0    # 余额 < 2 CNY → 自动降级 vLLM
        check_interval_minutes: 60     # 每小时查询一次
```

---

## 5. 固件端对接

### 5.1 修改固件代码 (2 行改动)

编辑 `firmware/src/llm/ChatGPT/RealtimeChatGPT.cpp`：

```cpp
// 第 307-312 行: 修改连接地址

// 原代码:
// webSocket.beginSslWithCA("api.openai.com", 443,
//     "/v1/realtime?model=gpt-realtime", root_ca_openai);
// String auth = "Bearer " + param.api_key;
// webSocket.setAuthorization(auth.c_str());

// 新代码:
webSocket.begin("192.168.1.100", 8080, "/v1/realtime?model=gpt-realtime");
String auth = "Bearer stackchan-local";
webSocket.setAuthorization(auth.c_str());
```

### 5.2 编译环境

在 VSCode + PlatformIO 中选择 `env:m5stack-cores3-realtime`（或对应硬件），编译烧录。

### 5.3 验证连通

烧录后固件屏幕显示流程：
```
Connecting... → Please touch → (触摸额头) → Listening... → (听到回复) → Please touch
```

---

## 6. 启动与运维

### 6.1 生产环境启动

```bash
# Docker 部署 (推荐)
docker run -d --name stackchan-server \
    -p 8080:8080 \
    -v $(pwd)/config.yaml:/app/config.yaml \
    -e DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY \
    stackchan-server:latest

# systemd 服务
sudo cp server/stackchan-server.service /etc/systemd/system/
sudo systemctl enable --now stackchan-server
```

### 6.2 监控端点

| 端点 | 说明 |
|------|------|
| `GET /health` | 基础健康检查 |
| `GET /status` | 引擎状态 + 在线会话数 |
| `GET /sessions` | 所有会话详情 |
| `GET /balance` | DeepSeek 余额查询 |

### 6.3 日志

```bash
# 查看实时日志
tail -f server/logs/stackchan_server.log

# 按会话过滤
grep "session_id=stackchan-abc123" server/logs/stackchan_server.log

# 性能指标搜索
grep "LLM(first=" server/logs/stackchan_server.log
```

---

## 7. 常见问题排查

### 7.1 固件显示 "Connecting..." 不动

**原因**：WebSocket 连接失败。

**检查**：
- 服务端是否在运行：`curl http://server-ip:8080/health`
- 固件 IP 是否正确：确认 `192.168.1.100` 可被固件 ping 通
- 防火墙：确认 8080 端口未被拦截
- WiFi 连接：确认固件已连上 WiFi

### 7.2 触摸后无反应 / 卡顿

**原因**：STT 引擎未加载或模型太大。

**检查**：
- 服务端日志：`grep "STT" logs/stackchan_server.log`
- 如果看到 "STT 不可用"：安装 `pip install faster-whisper`
- 如果 GPU 内存不足：在 `config.yaml` 中设 `stt.faster_whisper.device: "cpu"` 或 `model_size: "tiny"`

### 7.3 回复无声音

**原因**：TTS 引擎失败。

**检查**：
- 服务端日志：`grep "TTS" logs/stackchan_server.log`
- 安装 edge-tts：`pip install edge-tts`
- 验证：`curl http://localhost:8080/status` 看 TTS healthy 是否为 true

### 7.4 打断失效

**原因**：固件端 VAD 和服务端不同步。

**检查**：
- 固件编译标志中确认 `-DREALTIME_API` 已启用
- 确认 `webSocketProcess()` 中的录音循环正常运行
- 服务端日志搜索 "打断" 或 "INTERRUPTED"

### 7.5 DeepSeek 不可用

**检查**：
- 余额：`curl http://localhost:8080/balance`
- API Key：确认环境变量 `DEEPSEEK_API_KEY` 已设置
- 网络：确认服务器可访问 `https://api.deepseek.com`
- 降级行为：余额不足时应自动切到 vLLM，并 TTS 播放提示话术

---

## 8. 性能参考

| 指标 | 本地 vLLM (7B) | DeepSeek 云 | 说明 |
|------|---------------|------------|------|
| STT (small) | 200-500ms | - | GPU |
| STT (tiny) | 100-300ms | - | CPU 也可 |
| LLM 首 Token | 300-800ms | 800-2000ms | Qwen2.5-7B |
| LLM 总推理 | 1-3s | 2-5s | 短回复 |
| TTS 首帧 | 200-500ms | - | edge-tts |
| 总轮次 | 2-5s | 4-10s | 端到端 |

---

## 9. 扩展路线图

### 9.1 新增 STT 引擎

1. 在 `engines/stt/` 下创建 `my_stt_engine.py`
2. 继承 `BaseSTT`，实现 `start()` / `feed()` / `cancel()` / `reset()`
3. 在 `main.py` 的 `lifespan` 中将新引擎注入 `set_global_engines()`
4. 在 `config.yaml` 的 `stt` 节添加配置

```python
# engines/stt/my_stt_engine.py
from ..base import BaseSTT, STTResult, EngineConfig

class MySTTEngine(BaseSTT):
    async def start(self, config): ...
    async def feed(self, audio_chunk): ...
    async def cancel(self): ...
    async def reset(self): ...
    async def health_check(self): ...
```

### 9.2 新增 TTS 引擎

同 STT，继承 `BaseTTS`，实现四方法后在 `main.py` 注入。

### 9.3 接入 Function Calling (工具调用)

1. 在 `orchestration/session.py` 的 `_commit_audio()` 中 LLM 完成后检查 `tool_calls`
2. 执行工具逻辑 → 构建 `tool` role 消息 → 追加到 history → 再次调用 LLM
3. 固件端的 `conversation.item.create` (function_call_output) 信令已支持

### 9.4 对接 MCP 生态 (Model Context Protocol)

1. 创建 `engines/tools/mcp_client.py`，实现 MCP 客户端
2. 在服务启动时连接 MCP Server，获取 tools 列表
3. 将 tools 注入 `session.update` 的 `tools` 字段
4. LLM 返回 `tool_calls` 后，路由到 MCP 执行

### 9.5 长期记忆

1. 在 `orchestration/session.py` 的 `ConversationHistory` 中实现 `summarize()` 方法
2. 超长对话触发摘要 → 调用 LLM 生成摘要 → 替换早期消息
3. 持久化到 SQLite / JSON 文件 → 重启后恢复

### 9.6 Web 管理界面

1. 新增 `/admin` 路由 → FastAPI 静态文件
2. 实时 WebSocket 推送会话状态
3. 对话日志查看、余额告警、模型切换
