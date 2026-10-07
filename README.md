**English readers: please refer to the [English version (README_en.md)](README_en.md).**

# AI Stack-chan Ex（全双工语音 · 本地服务端版）

本仓库 fork 自 [ronron-gh/AI_StackChan_Ex](https://github.com/ronron-gh/AI_StackChan_Ex)，该上游项目又基于
[robo8080/AIｽﾀｯｸﾁｬﾝ](https://github.com/robo8080/AI_StackChan2) 扩展而来。
本 fork 聚焦一个方向：**低延迟全双工语音对话 + 本地化服务端网关 + 可扩展的对话编排**。

> ｽﾀｯｸﾁｬﾝ（Stack-chan）是由[ししかわ（Shishikawa）](https://x.com/stack_chan)开发并公开的、
> 手掌大小的超可爱交流机器人。
> - [本家仓库](https://github.com/stack-chan/stack-chan)
> - [Discord](https://discord.com/channels/1095725099925110847/1097878659966173225)
> - [ScrapBox](https://scrapbox.io/stack-chan/)

---

## 目录

- [项目来源与演进](#项目来源与演进)
- [设计思路（本 fork 的改造）](#设计思路本-fork-的改造)
- [实现途径](#实现途径)
- [快速开始](#快速开始)
- [开发环境](#开发环境)
- [其他功能：用户应用程序（MOD）](#其他功能用户应用程序mod)
- [已知问题与后续计划](#已知问题与后续计划)
- [贡献指南](#贡献指南)
- [文档列表](#文档列表)

---

## 项目来源与演进

本项目的代码谱系与各阶段的关注点如下：

1. **robo8080/AIｽﾀｯｸﾁｬﾝ（AI_StackChan2）**
   基础版本：通过 LLM / STT / TTS 云 API 组合实现 AI 语音对话，是社区中流传最广的 Arduino 版实现之一。

2. **ronron-gh/AI_StackChan_Ex（上游）**
   在基础版之上做了三类扩展：
   - 支持 M5Stack 的 **Module LLM** 扩展模块，将 LLM / STT / TTS 全部本地化；
   - 支持 **Realtime API**（语音直进直出，显著降低响应延迟）；
   - 适配 [stackchan-arduino 库](https://github.com/mongonta0716/stackchan-arduino)（YAML 初始化配置、串行舵机），
     并引入易扩展的用户应用程序（MOD）框架。

3. **本仓库（zhaoxiyi fork）**
   在上游基础上继续改造，核心工作围绕 **全双工语音链路** 与 **自研本地服务端**：
   - 固件支持"边播边听"的全双工音频流（双 I2S + 自研 NLMS 回声消除 + 自动延迟对齐）；
   - 对话编排从固件迁移到本地 Python 服务端（STT / LLM / TTS 可替换，逻辑可插拔）；
   - 新增联网信息助手、双级打断策略、复读治理等对话层能力；
   - 提供预编译发布版本（`release/`）与调参/设计文档（`doc/`）。

感谢上游作者与社区的开源工作，本仓库的成果同样以开源方式回馈社区。

## 设计思路（本 fork 的改造）

### 1. 为什么把对话搬进本地服务端

上游的 Realtime API 方案由固件直连云端（OpenAI 等）。本 fork 改为：

```
固件 (M5Stack) ⇄ WebSocket ⇄ 本地服务端 (server/) ⇄ STT / LLM / TTS
```

- **AI自编成实现**：当前程序基本都为 CodeWhale 自编成实现，用于尝试 WibeCoding 对终端设备编程语言的能力与边缘设备软件设计的影响力。当前全部模型都集中于 server 端使用 AI 自己决定的，当前看起来效果较好，我曾经干预过让 AI 评估将 TTS 留在硬件固件层，也许这样会有更好的语音播报效果（目前语音播报效果并不完美，仅是可用，有些无法评估是 TTS 模型问题还是这种架构设计的必然弊端），但 AI 给出的方案能够更好的与不同的 TTS 模型，或是未来与 Omni 模型进行对接，Omni 模型对于未来扩展视频，音频与语言协调工作能力更有利。在整个尝试中最需要决策的其实是全双工消音ACE能力究竟应该使用 NLMS 还是 ESP32 自带的 ESP-RS，实际验证的结果是 ESP-RS 并不能在 StackChan 上运行，这与多个 AI 自判断并不一致。因此需要花大量额外时间来验证。此外在调试 ACE 回音消除调试阶段，AI 给出了相当有效的辅助，但所有的调试过程由于涉及到与 StackChan 硬件设备的持续交互与设备日志采集验证，因此过程比较耗时，也消耗了不少 Token 。但整体还是能够节省大量时间的。
- **可替换**：本 fork 所使用的所有模型层均可以基于本地服务端（server/）的config.yaml 进行配置，这样可以最大化配置灵活度，在不调整硬件 firmware 的前提下完成大部分模型的动态替换，或者多模型组合能力。当前环境中，STT 用 SenseVoice（Server 端 CPU ONNX）、LLM 用 vLLM（但前为8G显存显卡配置了 qwen3-8b，设计中考虑了动态升级为 DeepSeek但当前未启用）、 TTS 用 edge-tts，全部可配置替换，需要注意的是，如果需要更换为Omni模型，那么实现途径会有不同，需要改造 Server 端实现。当前测试环境资源不足，没有在这个方面进一步尝试；
- **可扩展**：当前已经在尝试的能力包括复读治理、双级打断、联网助手，联网助手的逻辑还需要进一步调整。未来还规划了记忆系统 / 实时翻译 / DeepSeek Harness 协作 / MCP 客户端协作（原程序的MCP在 StackChan 硬件层配置，当前没有放弃这部分代码，但也没有使用这部分功能）等功能，这些功能的能力都作为服务端的编排层能力+ LLM Prompt 治理能力来实现，固件保持轻量，且保尽量保持固件无需重启，能耗尽量低；
- **可调试**：全链路日志、缓存落盘（`server/cache/`），问题可追溯，且大部分可以从 server 端直接调试解决，降低对固件程序的修改与重置传输要求。而且很多功能最终可以落实到对 LLM 模型 Prompt 的优化协调上，这与 AI 应用设计理论更加一致；
- **协议兼容**：固件与服务端之间使用 OpenAI Realtime 事件协议的子集
  （`session.update` / `input_audio_buffer.*` / `response.*`），便于理解和迁移。
  协议细节见 `server/stackchan_server/protocol/`。

### 2. 全双工语音（边播边听）

- 固件侧：采集与播放使用独立 I2S 通道并行工作；自研 **NLMS 回声消除**（逐样本滑窗能量归一化，
  解决了帧内冻结与帧级归一化发散两个根本缺陷）与 **auto_align 自动延迟对齐**（实测回声路径延迟约 124ms）；
- 服务端侧：能量门槛 + 静音断句（SenseVoice 连续识别）；
- 效果：设备播报期间持续上传 AEC 后的上行音频，用户可**随时开口打断**。

### 3. 双级打断策略（hard / soft）

- **hard（直接打断）**：上行能量显著超过"当前下行回声水平"（相对判据
  `thr = down × echo_factor × ratio + floor`），连续命中确认后立即停播并进入聆听；
- **soft（追问确认）**：命中较弱级别时先追问"嗯，是在跟我说什么吗？"并暂停原播报，
  观察窗内用户继续说话则正式打断，无回应则自动继续原播报；
- 抗误触发设计：5 中 3 块命中确认、下行参考取"设备当前正在播的内容"（覆盖环形缓冲的播放滞后）、
  追问合成走后台任务（不阻塞音频接收循环）；
- 关键参数与调参记录（含 AEC 能量门槛）见 [doc/duplex_voice_tuning.md](doc/duplex_voice_tuning.md)。

### 4. 复读治理（输出侧）

- **复读用户**：回复开头复述用户问句 → 自动剔除；（此功能仍需优化，我个人根倾向于基于 Cache Prifix 来优化这个问题，也就是通过 Prompt 的高度重复来解决这个问题，且可以面临相同问题的微小上下文变化）
- **自我复读**：与近期回复重复（含"完全相同"快速通道）→ 自动重生成；
- **打断恢复**：被打断的部分回复以"（被打断，部分内容未播报…）"标注进入上下文，
  用户后续问起时可由 LLM 补充播报。

### 5. 联网信息助手（后台异步搜索）

- 识别到联网意图后转**后台异步搜索**，不阻塞当前对话；（此功能仍需优化，特别是 Prompt 治理，还没有完善机制）
- 搜索进行中通过 Prompt 槽位告知模型"还在查"，严禁编造结果；
- 用户说"把查出来的内容播报一下"即可回流播报；结果落盘缓存（`server/cache/web_queries/`）备查，后续会在 Server 的 /panel UI界面中增加对搜索结果的管理功能；
- 搜索源可配置（默认 BingCN，支持 DuckDuckGo / SearXNG，这些搜索为AI选择 ）。

### 6. Prompt 分层组合（prefix cache 友好）

发送给 LLM 的消息按如下层次组装（恒定内容在前、动态内容靠后），这部分还没有进行干预优化，目前为 AI 自设计形成，但这部分应该充分考虑 LLM Model 的 Cache Prifix 与长时间沟通的历史上下文关系等组合因素：

```
[1] system 恒定引导（永不变化 → 始终命中 vLLM 前缀缓存）
[2] 历史消息（窗口滑动）
[3] system 动态背景块（更早摘要 → 用户更正 → 后台任务槽位）
[4] user 当前问题
```

实现见 `server/stackchan_server/orchestration/session.py` 的 `ConversationHistory.build_messages()`。

## 实现途径

### 目录结构

- `firmware/` —— 固件工程（PlatformIO）：全双工音频、AEC、Realtime 会话、MOD 应用框架
- `server/` —— 本地服务端（Python / FastAPI）：对话编排、STT / LLM / TTS 引擎、协议层
- `Copy-to-SD/` —— SD 卡配置文件（YAML 初始化、应用配置）
- `release/` —— 预编译发布版本（见下方"快速开始"）
- `doc/` —— 使用文档与设计文档（见文末列表）

### 固件侧要点

- 目标设备：**M5Stack CoreS3**（主要验证）、M5Stack Core2、AtomS3R + Atomic Echo Base；
- PlatformIO 环境：`m5stack-cores3-realtime`（全双工模式）；
  诊断环境 `m5stack-cores3-realtime-diag`（`-DAEC_DIAG`，输出 `[FD] aec2s:` 能量诊断行）；
- 关键模块：
  - `src/llm/RealtimeAudioStream.cpp` —— 全双工采集 / 播放任务
  - `src/llm/RealtimeAEC.cpp/.h` —— NLMS 回声消除（含 auto_align）
  - `src/llm/RealtimeLLMBase.cpp` / `src/llm/ChatGPT/RealtimeChatGPT.cpp` —— 会话主循环与事件分发
  - `src/mod/` —— MOD 应用框架（见下文）

### 服务端侧要点

- Python 3.11+，FastAPI + Uvicorn（WebSocket）；
- 引擎：STT = SenseVoice（本地 ONNX，可换 whisper）、LLM = vLLM / DeepSeek、TTS = edge-tts；
- 关键模块：
  - `orchestration/session.py` —— 对话编排（Prompt 组合 / 打断 / 保活 / 流水线）
  - `orchestration/response_guard.py` —— 复读治理
  - `orchestration/web_query.py` —— 联网信息助手
  - `orchestration/state_machine.py` —— 会话状态机（IDLE / LISTENING / REASONING / SPEAKING）
  - `protocol/` —— Realtime 事件协议（与固件对接）

## 快速开始

### 1. 启动服务端

```bash
cd server
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# 按需编辑 config.yaml：vLLM 地址（vllm.base_url）、STT 模型路径、TTS 音色等
.venv/bin/python -m stackchan_server.main
```

服务默认监听 `0.0.0.0:8080`（WebSocket 端点 `/v1/realtime`）。

### 2. 准备 SD 卡与固件

- 将 `Copy-to-SD/` 下的内容复制到 SD 卡根目录（Wi-Fi、伺服等初始化配置在 YAML 中完成）；
- 编译烧录：

```bash
cd firmware
~/.platformio/penv/bin/pio run -e m5stack-cores3-realtime -t upload
```

- 或直接使用预编译版本：`release/AiStackChanEx-FullDuplexOnly-v0.6/`
  （含合并镜像与分文件、SHA256SUMS，详见该目录内 README）。

### 3. 开始使用

- 触摸屏幕开始 / 结束录音；
- 播报过程中可随时开口打断（hard 直接打断 / soft 追问确认）；
- 联网类问题自动转后台搜索，答复"播报一下"即可听取结果。

## 开发环境

- 目标设备：M5Stack Core2 / M5Stack CoreS3 / AtomS3R + Atomic Echo Base
- 开发 PC：Windows 11 或 Linux；VSCode + PlatformIO
- 服务端：Python 3.11+，可访问本地 vLLM（或配置 DeepSeek API Key）

## 其他功能：用户应用程序（MOD）

参考 moddable 版本家ｽﾀｯｸﾁｬﾝ的 MOD 设计，本仓库支持创建与切换用户应用程序。
源代码位于 `firmware/src/mod/`，基本都是保留原有项目内容。已内置以下应用：

| No. | 应用名 | 说明（用法） | 备注 |
| --- | --- | --- | --- |
| 1 | AIｽﾀｯｸﾁｬﾝ | 本仓库的主应用（AI 语音对话） | |
| 2 | 番茄钟 | 25 分钟与 5 分钟闹钟交替循环。<br>按钮A：开始/停止；按钮C：解除/设置静音 | 初始为静音模式 |
| 3 | 数码相框 | 显示 SD 卡目录 `/app/AiStackChanEx/photo` 中的 JPEG 图片。<br>按钮A：下一张；按钮C：开始幻灯片 | JPEG 需预先调整为 320×240；<br>曾出现 SD 卡不可挂载需重新格式化的问题，请备份数据 |
| 4 | 状态监视器 | 显示各类系统信息 | |
| 5 | ESP-NOW 遥控 | 使用 M5Stack 官方ｽﾀｯｸﾁｬﾝ选配摇杆手柄的姿势数据控制舵机。<br>接收 channel 固定为 1 | ESP-NOW 使用期间 Wi-Fi 断开；切换其他 Mod 后会自动尝试重连 |

按下述方式注册多个应用，运行时在 LCD 上左右滑动即可切换（[演示视频](https://x.com/motoh_tw/status/1841867660746789052)）：

```c++
[main.cpp]
ModBase* init_mod(void)
{
  ModBase* mod;
  add_mod(new AiStackChanMod());      // AI Stack-chan
  add_mod(new PomodoroMod());         // 番茄钟
  add_mod(new StatusMonitorMod());    // 状态监视器
  mod = get_current_mod();
  mod->init();
  return mod;
}
```

## 已知问题与后续计划

### 已知问题

- **全双工回声残余**：播报期间上行仍存在一定量级的 AEC 残余能量，个别场景会影响打断判定
  （表现为误触发或打不断），根因仍在排查中；现有调参记录与诊断方法见
  [doc/duplex_voice_tuning.md](doc/duplex_voice_tuning.md)。

### 后续计划（详见 [doc/next_stage_design.md](doc/next_stage_design.md)）

1. **实时翻译模式**：持续收听并逐段翻译（随收听内容累积不断修正），翻译结果实时落盘并展示；
2. **服务端 MCP 客户端**：将工具调用从固件迁移到服务端（vLLM function calling），接入 MCP 生态；
3. **长期记忆**：跨重启记住近期对话内容；
4. **联网信息助手增强**：多任务并行搜索、结果回唤（结合记忆系统）；
5. **外部 Agent / Harness 调度接口预留**（需显式授权后启用）。

## 贡献指南

欢迎 issue 与 pull request。如有问题或改进建议，请先通过 issue 联系。
如果愿意以 Sponsor 形式支持本项目，将用于日常问题排查、咨询响应与新硬件适配等工作。

### 本仓库的方针

- **追求 AI 语音助手的实用性能**：围绕实时语音对话、低延迟与长期记忆持续演进，让ｽﾀｯｸﾁｬﾝ更接近实用级 AI 助手；
- **作为派生开发的基座**：保持简单、通用的设计，让本仓库的成果能服务于更多人的 AIｽﾀｯｸﾁｬﾝ开发；
- **跟进新硬件**：在不影响上述方针的范围内，覆盖 Module LLM 等有发展潜力的新硬件；
- **回馈本家社区**：尊重并借鉴本家（ししかわ氏）与本仓库上游的设计，
  并希望将成果用于本家ｽﾀｯｸﾁｬﾝ的发展。

## 文档列表

### 基础使用与配置

- [basic_usage.md](doc/basic_usage.md) / [English](doc/basic_usage_en.md) —— 基本使用方法（LLM / STT / TTS API 联动）
- [module_llm.md](doc/module_llm.md) / [English](doc/module_llm_en.md) —— 使用 Module LLM 实现完全本地化对话
- [realtime_api.md](doc/realtime_api.md) / [English](doc/realtime_api_en.md) —— Realtime API 使用方法
- [custom_endpoint.md](doc/custom_endpoint.md) / [English](doc/custom_endpoint_en.md) —— 自定义 API 端点
- [sd_updater.md](doc/sd_updater.md) / [English](doc/sd_updater_en.md) —— SD 卡更新方法
- [atoms3r.md](doc/atoms3r.md) —— AtomS3R + Atomic Echo Base 使用说明
- [tts_aquestalk.md](doc/tts_aquestalk.md) —— AquesTalk TTS 说明
- [wakeword_simple_vox.md](doc/wakeword_simple_vox.md) / [English](doc/wakeword_simple_vox_en.md) —— 唤醒词（SimpleVox）

### 功能扩展

- [mcp.md](doc/mcp.md) / [English](doc/mcp_en.md) —— MCP（Model Context Protocol）集成
- [function_calling.md](doc/function_calling.md) / [English](doc/function_calling_en.md) —— 函数调用
- [scheduler.md](doc/scheduler.md) —— 定时任务（Scheduler）
- [gpt4o_cores3camera.md](doc/gpt4o_cores3camera.md) / [English](doc/gpt4o_cores3camera_en.md) —— CoreS3 摄像头 + GPT-4o

### 本 fork 新增（全双工 / 服务端）

- [duplex_voice_tuning.md](doc/duplex_voice_tuning.md) —— 全双工语音调优记录
  （AEC 缺陷修复、auto_align、打断策略与能量门槛的关键参数说明）
- [next_stage_design.md](doc/next_stage_design.md) —— 下一阶段设计
  （联网信息助手 / 实时翻译模式 / Harness 调度接口）
