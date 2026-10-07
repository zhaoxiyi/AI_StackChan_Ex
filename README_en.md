[中文](README.md)

# AI Stack-chan Ex (Full-Duplex Voice · Local Server Edition)

This repository is a fork of [ronron-gh/AI_StackChan_Ex](https://github.com/ronron-gh/AI_StackChan_Ex), which in turn
extends [robo8080/AIｽﾀｯｸﾁｬﾝ](https://github.com/robo8080/AI_StackChan2).
This fork focuses on one direction: **low-latency full-duplex voice conversation + a local server gateway +
extensible conversation orchestration**.

> ｽﾀｯｸﾁｬﾝ (Stack-chan) is a palm-sized, super-cute communication robot developed and open-sourced by
> [Shishikawa](https://x.com/stack_chan).
> - [Original repository](https://github.com/stack-chan/stack-chan)
> - [Discord](https://discord.com/channels/1095725099925110847/1097878659966173225)
> - [ScrapBox](https://scrapbox.io/stack-chan/)

---

## Table of Contents

- [Origin and Evolution](#origin-and-evolution)
- [Design Rationale (Changes in This Fork)](#design-rationale-changes-in-this-fork)
- [Implementation](#implementation)
- [Quick Start](#quick-start)
- [Development Environment](#development-environment)
- [Other Features: User Applications (MOD)](#other-features-user-applications-mod)
- [Known Issues and Roadmap](#known-issues-and-roadmap)
- [Contributing](#contributing)
- [Documentation Index](#documentation-index)

---

## Origin and Evolution

The code lineage and the focus of each stage:

1. **robo8080/AIｽﾀｯｸﾁｬﾝ (AI_StackChan2)**
   The base version: AI voice conversation built from LLM / STT / TTS cloud APIs — one of the most widely used
   Arduino-based implementations in the community.

2. **ronron-gh/AI_StackChan_Ex (upstream)**
   Three extensions on top of the base version:
   - Support for M5Stack's **Module LLM** add-on, fully localizing LLM / STT / TTS;
   - Support for the **Realtime API** (speech in, speech out, greatly reduced response latency);
   - Adaptation to the [stackchan-arduino library](https://github.com/mongonta0716/stackchan-arduino)
     (YAML-based initialization, serial servos), plus an easy-to-extend user-application (MOD) framework.

3. **This repository (zhaoxiyi fork)**
   Continued evolution on top of upstream, centered on the **full-duplex voice pipeline** and a
   **self-built local server**:
   - Firmware supports a "listen-while-speaking" full-duplex audio stream (dual I2S + self-developed
     NLMS echo cancellation + automatic delay alignment);
   - Conversation orchestration moved from firmware to a local Python server (swappable STT / LLM / TTS,
     pluggable logic);
   - New conversation-layer capabilities: web information assistant, dual-level barge-in, repetition guard;
   - Prebuilt release images (`release/`) and tuning/design documents (`doc/`).

Thanks to the upstream authors and the community for their open-source work — the results of this repository
are likewise contributed back to the community.

## Design Rationale (Changes in This Fork)

### 1. Why move the conversation to a local server

Upstream's Realtime API approach connects the firmware directly to the cloud (OpenAI etc.). This fork changes
that to:

```
Firmware (M5Stack) ⇄ WebSocket ⇄ Local server (server/) ⇄ STT / LLM / TTS
```

- **Written by AI (a Vibe Coding experiment)**: The current programs were mostly written by CodeWhale itself,
  as an experiment to explore what Vibe Coding can achieve for embedded programming and edge-device software
  design. All model layers run on the server side — a decision made by the AI, and it has worked well so far.
  I once intervened and asked the AI to evaluate keeping TTS in the firmware layer, which might have produced
  better speech playback (playback quality is currently not perfect — merely usable — and it is hard to tell
  whether that stems from the TTS model or is an inherent downside of this architecture). However, the AI's
  proposal (TTS on the server) makes it easier to swap TTS models and to connect to future Omni models, which
  would be better for coordinated video/audio/language capabilities later on. The most critical decision in
  this experiment was whether full-duplex echo cancellation should use NLMS or the ESP32's built-in ESP-SR;
  actual testing showed ESP-SR cannot run on the Stack-chan, contradicting several AI self-assessments — so a
  lot of extra time went into verification. During AEC debugging the AI provided quite effective assistance,
  but every debugging round involved continuous interaction with the Stack-chan hardware and device-log
  collection, which was time-consuming and consumed a fair number of tokens — yet overall it still saved a
  lot of time.
- **Swappable**: All model layers in this fork are configured via `server/config.yaml`, maximizing
  flexibility — most models can be swapped or combined without touching the firmware. In the current setup:
  STT = SenseVoice (CPU ONNX on the server), LLM = vLLM (qwen3-8b on an 8GB-VRAM GPU; a dynamic upgrade to
  DeepSeek was considered in the design but is not enabled yet), TTS = edge-tts — all configurable. Note
  that switching to an Omni model would take a different path and require reworking the server
  implementation; the current test environment lacks the resources to explore this further.
- **Extensible**: Capabilities already in progress include the repetition guard, dual-level barge-in and the
  web assistant (the web-assistant logic still needs refinement). Planned: memory system / real-time
  translation / DeepSeek Harness collaboration / MCP client collaboration (the original MCP runs at the
  firmware layer; that code has been kept but is currently unused). All of these are implemented as
  server-side orchestration + LLM prompt governance, keeping the firmware lightweight, ideally avoiding
  firmware restarts and keeping power consumption low.
- **Debuggable**: End-to-end logs and cache persistence (`server/cache/`) make issues traceable, and most
  problems can be fixed directly on the server side, reducing firmware modification and re-flashing. Many
  features ultimately come down to refining LLM prompts, which is more aligned with AI application design
  practice.
- **Protocol compatibility**: Firmware and server speak a subset of the OpenAI Realtime event protocol
  (`session.update` / `input_audio_buffer.*` / `response.*`), which keeps things easy to understand and
  migrate. See `server/stackchan_server/protocol/` for details.

### 2. Full-Duplex Voice (listen while speaking)

- Firmware side: capture and playback run in parallel on independent I2S channels; self-developed
  **NLMS echo cancellation** (per-sample sliding-window energy normalization, fixing two fundamental defects:
  intra-frame freezing and frame-level normalization divergence) plus **auto_align** delay alignment
  (measured echo path delay ≈ 124 ms);
- Server side: energy gate + silence-based segmentation (continuous SenseVoice recognition);
- Result: while the device is speaking, AEC-processed uplink audio keeps streaming — the user can
  **interrupt at any time**.

### 3. Dual-Level Barge-in (hard / soft)

- **hard (direct interrupt)**: uplink energy clearly exceeds the "current downlink echo level" (relative
  criterion `thr = down × echo_factor × ratio + floor`); after consecutive hits are confirmed, playback
  stops immediately and the device starts listening;
- **soft (probe & confirm)**: on a weaker hit level, the assistant first asks "Hmm, were you saying
  something to me?" and pauses the original playback; if the user keeps talking within the observation
  window, the interrupt is confirmed, otherwise playback resumes automatically;
- Anti-false-trigger design: 5-of-3 block confirmation; the downlink reference is taken from "what the
  device is currently playing" (covering ring-buffer playback lag); probe synthesis runs as a background
  task (never blocking the audio receive loop);
- Key parameters and tuning notes (including AEC energy thresholds) are in
  [doc/duplex_voice_tuning.md](doc/duplex_voice_tuning.md).

### 4. Repetition Guard (output side)

- **Echoing the user**: when a reply starts by restating the user's question → automatically stripped;
  (still needs refinement; I personally lean towards solving this via cache prefixing — i.e., relying on
  highly repeated prompts, which can also tolerate small context variations around the same question)
- **Self-repetition**: replies that repeat recent ones (including an "exact match" fast path) →
  automatically regenerated;
- **Interrupt recovery**: partially spoken replies are kept in context annotated with
  "(interrupted, some content not spoken…)"; if the user asks about it later, the LLM can fill in the rest.

### 5. Web Information Assistant (async background search)

- Web-intent queries are forwarded to a **background async search** without blocking the ongoing
  conversation; (still needs refinement, especially prompt governance — no complete mechanism yet)
- While searching, a prompt slot tells the model "still searching" and strictly forbids fabricating results;
- Saying "read out what you found" replays the results; results are cached on disk
  (`server/cache/web_queries/`) for later review, and search-result management will later be added to the
  server's `/panel` UI;
- Search providers are configurable (BingCN by default; DuckDuckGo / SearXNG supported — chosen by the AI).

### 6. Layered Prompt Composition (prefix-cache friendly)

Messages sent to the LLM are assembled in layers (constant content first, dynamic content last). No manual
optimization has been applied yet — this is the AI's own design; ideally it should fully account for the
LLM's cache prefix and long-history context composition:

```
[1] system: constant guidance (never changes → always hits the vLLM prefix cache)
[2] history messages (sliding window)
[3] system: dynamic background block (earlier summary → user corrections → background task slot)
[4] user: current question
```

Implemented in `ConversationHistory.build_messages()` in `server/stackchan_server/orchestration/session.py`.

## Implementation

### Directory Layout

- `firmware/` — firmware project (PlatformIO): full-duplex audio, AEC, Realtime session, MOD app framework
- `server/` — local server (Python / FastAPI): conversation orchestration, STT / LLM / TTS engines,
  protocol layer
- `Copy-to-SD/` — SD card configuration files (YAML initialization, app configuration)
- `release/` — prebuilt release images (see "Quick Start")
- `doc/` — user and design documentation (see the index at the end)

### Firmware Highlights

- Target devices: **M5Stack CoreS3** (primary), M5Stack Core2, AtomS3R + Atomic Echo Base;
- PlatformIO environments: `m5stack-cores3-realtime` (full-duplex mode); diagnostics environment
  `m5stack-cores3-realtime-diag` (`-DAEC_DIAG`, prints `[FD] aec2s:` energy diagnostic lines);
- Key modules:
  - `src/llm/RealtimeAudioStream.cpp` — full-duplex capture / playback tasks
  - `src/llm/RealtimeAEC.cpp/.h` — NLMS echo cancellation (with auto_align)
  - `src/llm/RealtimeLLMBase.cpp` / `src/llm/ChatGPT/RealtimeChatGPT.cpp` — session loop and event dispatch
  - `src/mod/` — MOD application framework (see below)

### Server Highlights

- Python 3.11+, FastAPI + Uvicorn (WebSocket);
- Engines: STT = SenseVoice (local ONNX, whisper swappable), LLM = vLLM / DeepSeek, TTS = edge-tts;
- Key modules:
  - `orchestration/session.py` — conversation orchestration (prompt composition / barge-in / keepalive /
    pipeline)
  - `orchestration/response_guard.py` — repetition guard
  - `orchestration/web_query.py` — web information assistant
  - `orchestration/state_machine.py` — session state machine (IDLE / LISTENING / REASONING / SPEAKING)
  - `protocol/` — Realtime event protocol (firmware-facing)

## Quick Start

### 1. Start the server

```bash
cd server
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# edit config.yaml as needed: vLLM base URL (vllm.base_url), STT model path, TTS voice, ...
.venv/bin/python -m stackchan_server.main
```

The server listens on `0.0.0.0:8080` by default (WebSocket endpoint `/v1/realtime`).

### 2. Prepare the SD card and firmware

- Copy the contents of `Copy-to-SD/` to the SD card root (Wi-Fi, servo and other initialization settings
  live in YAML files);
- Build and flash:

```bash
cd firmware
~/.platformio/penv/bin/pio run -e m5stack-cores3-realtime -t upload
```

- Or use the prebuilt release: `release/AiStackChanEx-FullDuplexOnly-v0.6/`
  (merged image, individual binaries, SHA256SUMS — see the README inside that directory).

### 3. Start using

- Touch the screen to start / stop recording;
- You can interrupt at any time while the device speaks (hard = direct interrupt / soft = probe & confirm);
- Web-type questions are automatically forwarded to background search — say "read that out" to hear the
  result.

## Development Environment

- Target devices: M5Stack Core2 / M5Stack CoreS3 / AtomS3R + Atomic Echo Base
- Development PC: Windows 11 or Linux; VSCode + PlatformIO
- Server: Python 3.11+, with access to a local vLLM (or a configured DeepSeek API key)

## Other Features: User Applications (MOD)

Inspired by the MOD design of the moddable version of Stack-chan, this repository supports creating and
switching user applications. The source lives in `firmware/src/mod/` (mostly retained from the upstream
project). The following apps are built in:

| No. | App | Description (usage) | Notes |
| --- | --- | --- | --- |
| 1 | AIｽﾀｯｸﾁｬﾝ | Main app of this repository (AI voice conversation) | |
| 2 | Pomodoro Timer | Alternates 25-minute and 5-minute alarms.<br>Button A: start/stop; Button C: mute on/off | Starts muted |
| 3 | Digital Photo Frame | Displays JPEGs from SD card folder `/app/AiStackChanEx/photo`.<br>Button A: next photo; Button C: start slideshow | JPEGs must be pre-resized to 320×240;<br>SD cards occasionally failed to mount and needed reformatting — back up your data |
| 4 | Status Monitor | Shows system information | |
| 5 | ESP-NOW Remote | Controls servos with pose data from the official Stack-chan joystick controller.<br>Receive channel fixed at 1 | Wi-Fi disconnects while ESP-NOW is active; reconnection is attempted after switching to another MOD |

Register multiple apps as shown below; swipe left/right on the LCD at runtime to switch
([demo video](https://x.com/motoh_tw/status/1841867660746789052)):

```c++
[main.cpp]
ModBase* init_mod(void)
{
  ModBase* mod;
  add_mod(new AiStackChanMod());      // AI Stack-chan
  add_mod(new PomodoroMod());         // Pomodoro Timer
  add_mod(new StatusMonitorMod());    // Status Monitor
  mod = get_current_mod();
  mod->init();
  return mod;
}
```

## Known Issues and Roadmap

### Known Issues

- **Full-duplex echo residue**: during playback the uplink still carries a certain level of AEC residual
  energy, which in some scenarios affects barge-in decisions (false triggers or missed interrupts). The
  root cause is still under investigation; existing tuning notes and diagnostics are in
  [doc/duplex_voice_tuning.md](doc/duplex_voice_tuning.md).

### Roadmap (see [doc/next_stage_design.md](doc/next_stage_design.md) for details)

1. **Real-time translation mode**: continuously listen and translate segment by segment (revising earlier
   segments as more audio accumulates); results persisted to disk and displayed in real time;
2. **Server-side MCP client**: move tool calling from firmware to the server (vLLM function calling),
   connecting to the MCP ecosystem;
3. **Long-term memory**: remember recent conversations across restarts;
4. **Web assistant enhancements**: parallel multi-task search, result recall (combined with the memory
   system);
5. **Reserved interfaces for external Agent / Harness scheduling** (enabled only with explicit
   authorization).

## Contributing

Issues and pull requests are welcome. If you have a problem or an improvement idea, please start with an
issue. Sponsorship is also greatly appreciated — it supports day-to-day troubleshooting, responding to
questions, and adapting new hardware.

### Repository Policy

- **Pursue the practical performance of an AI voice assistant**: keep evolving around real-time voice
  conversation, low latency and long-term memory, bringing Stack-chan closer to a practical AI assistant;
- **Serve as a base for derivative development**: keep the design simple and general, so the results of
  this repository can serve more people building AI Stack-chan projects;
- **Follow new hardware**: cover promising new hardware such as Module LLM, as long as it does not conflict
  with the policies above;
- **Give back to the original community**: respect and draw on the designs of the original Stack-chan
  (Shishikawa) and this repository's upstream, and use the results to help the original Stack-chan evolve.

## Documentation Index

### Basics & Configuration

- [basic_usage.md](doc/basic_usage.md) (Japanese) / [English](doc/basic_usage_en.md) — Basic usage
  (LLM / STT / TTS API integration)
- [module_llm.md](doc/module_llm.md) (Japanese) / [English](doc/module_llm_en.md) — Fully local conversation
  with Module LLM
- [realtime_api.md](doc/realtime_api.md) (Japanese) / [English](doc/realtime_api_en.md) — Using the
  Realtime API
- [custom_endpoint.md](doc/custom_endpoint.md) (Japanese) / [English](doc/custom_endpoint_en.md) — Custom
  API endpoints
- [sd_updater.md](doc/sd_updater.md) (Japanese) / [English](doc/sd_updater_en.md) — SD card update
  procedure
- [atoms3r.md](doc/atoms3r.md) (Japanese) — AtomS3R + Atomic Echo Base guide
- [tts_aquestalk.md](doc/tts_aquestalk.md) (Japanese) — AquesTalk TTS notes
- [wakeword_simple_vox.md](doc/wakeword_simple_vox.md) (Japanese) /
  [English](doc/wakeword_simple_vox_en.md) — Wake word (SimpleVox)

### Feature Extensions

- [mcp.md](doc/mcp.md) (Japanese) / [English](doc/mcp_en.md) — MCP (Model Context Protocol) integration
- [function_calling.md](doc/function_calling.md) (Japanese) / [English](doc/function_calling_en.md) —
  Function calling
- [scheduler.md](doc/scheduler.md) (Japanese) — Scheduled tasks (Scheduler)
- [gpt4o_cores3camera.md](doc/gpt4o_cores3camera.md) (Japanese) /
  [English](doc/gpt4o_cores3camera_en.md) — CoreS3 camera + GPT-4o

### Added in This Fork (full-duplex / server)

- [duplex_voice_tuning.md](doc/duplex_voice_tuning.md) (Chinese) — Full-duplex voice tuning notes
  (AEC defect fixes, auto_align, barge-in strategy and key energy-threshold parameters)
- [next_stage_design.md](doc/next_stage_design.md) (Chinese) — Next-stage design
  (web information assistant / real-time translation / Harness scheduling interface)
