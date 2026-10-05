# 全双工语音链路调试阶段总结 (2026-10)

> 本阶段完成了 Stack-chan 全双工语音链路从"频繁崩溃/无声"到"可正常通话 + 打断可用"的
> 调试与修复, 核心是三块: **AEC (回声消除)**、**STT 触发能量门槛**、**语音打断策略 (barge-in v1)**。
> 本文汇总关键结论与参数; 详细实现见对应源码注释。

---

## 1. AEC (回声消除) 调试结果

### 1.1 后端选型结论

| 后端 | 状态 | 说明 |
|------|------|------|
| **NLMS (自研, 默认)** | ✅ 采用 | `RealtimeAEC` 时域 NLMS + auto_align; 完全可控 |
| esp-sr (官方预编译库) | ⛔ 停用 | 实测不兼容 (见 1.5); 编译宏 `-DAEC_FORCE_ESP_SR` 可实验性启用 |

### 1.2 NLMS 的两个根本性缺陷与修复 (本阶段最关键)

1. **参考信号"帧内冻结"** (修复前一直存在):
   滤波/权重更新使用单一固定参考位置, 而一帧有 256 个样本。
   数学后果: 更新量 ∝ `Σ e[n] × 常数 ≈ 0` (交流信号相消) → **权重永远学不到回声路径, AEC 从未真正工作**。
   **修复**: 参考索引随样本滑动 (`frameBase + n`), `frameBase = refHistPos - FRAME - k0`。

2. **帧级常数能量归一化在语音包络/静音段步长失控** (修 1 后暴露):
   **修复**: 标准 NLMS **逐样本滑窗能量归一化**, 且为 O(1) 增量式:
   ```
   pow(n+1) = pow(n) - x[base+n-255]² + x[base+n+1]²   (每帧初窗全量重算一次, 防浮点漂移)
   静音保护: pow_n ≤ 1.0 时跳过更新
   ```

### 1.3 关键参数 (firmware/src/llm/RealtimeAEC.h)

| 参数 | 值 | 说明 |
|------|----|------|
| `AEC_FRAME_SAMPLES` | 256 | 16ms @16kHz 帧长 (esp_aec 兼容帧长) |
| `AEC_FILTER_TAPS` | 256 | NLMS 回声尾 16ms (桌面近场直达+早期反射足够) |
| `AEC_HISTORY_SAMPLES` | 4096 | 256ms 参考历史环 |
| `mu` | 0.1 | NLMS 步长 (逐样本归一化下稳定; 时间常数 ~0.2s) |
| `noise_gate` | 90 | 低于此 RMS 软衰减 ÷4 (不清零, 保轻声) |
| `vad_threshold` / `vad_confirm_ms` | 200 / 200ms | VAD 触发 |

### 1.4 参考延迟自动对齐 (auto_align)

- **原理**: 高能量帧对参考环做粗搜互相关 (bin=1ms) 累加, 每 ~400ms (`AEC_ALIGN_EVERY=25` 帧) 找峰值精搜 ±1ms, 更新 `k0`。
- **稳定性策略** (实机调优后):
  - **仅在"播放活跃"时估计** (最近 500ms 内有参考推送) — 纯人声期的互相关是噪声峰, 会污染 k0;
  - 小变化 (≤64 样本/4ms) 用 EMA 平滑吸收, 不重置权重;
  - 显著变化需**连续两次估计相近 (±32)** 才接受 (确认真实漂移而非噪声), 接受时才重置自适应状态。
- **实测**: 回波延迟 `k0 ≈ 124ms` (≈ M5Unified Speaker 128ms DMA 队列), 估计稳定收敛 (误差 ±1 样本)。
- **已知遗留**: 播放期 `atten` 仍不足 (部分时段失配) — 相关线索: 播放环深度波动 (0~3648 帧) 使"写入→出声"延迟时变; 后续方向: 稳定播放环深度/预填 DMA。
- **处理耗时** (诊断行 `[FD] play2s ... aec=xxxxus`): 静默期 ~5.2ms/帧, 播放期 ~11-13ms/帧 (16ms 帧预算内)。

### 1.5 esp-sr 库评估结论 (取证级)

预编译库 (`libesp_audio_processor.a`, "aishare" 构建) 存在**多个内部缺陷**, 与 16ms/256 样本流式用法不兼容:
- `aec_create` (speex 路径) → 创建期堆 free-list 崩溃 (确定性);
- `aec_pro_create(mode1)` → `esp_aec3_init` 连续分配 ~150KB+ 超内部 RAM, 分配失败返回 NULL 但库不检查 → 向 NULL 填表崩溃;
- `aec_pro_create(mode5)` → 创建成功, 但 728 加速内核处理时向调用者 out 缓冲写 16 倍数据量 (库按 4096 样本/帧内部维度), 越界直至命中任务栈 canary。
**结论**: 调用语义不兼容 (库按 ~256ms 大帧工作), 调用侧无法小改适配; 未来如需重试, 需更换官方新版 esp-sr / 适配的 SDK。

### 1.6 ES7210 麦克风主动初始化 (重要)

- 全双工模式为 I2S 互斥跳过 `M5.Mic.begin()`, ES7210 无人配置;
- 芯片上电默认态不可靠 (**掉电后实测采集全 0**);
- **修复**: `fd_init_duplex()` 中调用 `fd_es7210_init()` (序列与 test_duplex 验证工程一致, 增益 0x1B);
- 日志标志: `[FD] ES7210 initialized (MIC1/2, gain 0x1B)`。

---

## 2. STT 触发能量门槛 (防垃圾段)

**问题**: 安静段 (实测 RMS=157) 被"静音断句"送进 SenseVoice, 被"编"出无意义文本
(如 "你让大家都都。"), 白触发一轮完整 LLM 对话 (~17 秒)。

**设计** (server `sensevoice_engine.feed`, 触发识别前判定):
```
段平均 RMS < min_speech_rms (默认 180) 且 段内最大块 RMS < 500
  → 丢弃不识别 (清缓冲, 日志 "段能量过低 → 丢弃不识别")
```

**参数**:

| 参数 | 值 | 位置 | 说明 |
|------|----|------|------|
| `min_speech_rms` | 180 | config.yaml `stt.sensevoice` | 段平均能量门槛 |
| 最大块条件 | 500 | 代码常量 | 防"短促噪声+大量静音"拉低均值误伤 |
| `silence_rms` | 250 | 代码常量 | 逐块静音判定 (用于静音断句计时) |
| `silence_break_ms` | 1200 | config.yaml | 静音断句阈值 |

**参考值**: 人声段 avg 典型 300-2000, 不受影响; 轻声耳语可能被丢弃 (可接受)。

**评估结论 (是否换更强 STT / 做角色区分)**:
- 不做独立说话人分离 (diarization) — 重、慢、对单主人场景收益低;
- "主人 vs 旁人/电视"用近场能量假设 + 触发策略 (本节门槛 + 播放期不喂 STT + 打断判据) 近似解决;
- 换更大 STT 模型列为最后选项; 优先"数据面"优化 (门槛 + 可选短指令纠错表)。

---

## 3. 语音打断策略 (barge-in v1)

### 3.1 设计原理

- **难点**: AEC 未收敛期扬声器回声使上行 RMS 达 10000+, **绝对阈值无法区分插话**;
- **关键观察**: 回声幅度随下行内容同步波动 → 用**相对下行回声水平**的判据:

```
thr = down_rms × echo_factor × barge_in_ratio + barge_in_floor   (且不低于 speech_rms_threshold)
上行连续 speech_confirm_blocks 块 ≥ thr → 判定插话 → 打断
```

- **尾音保护**: 距最近一次下行分片 < `tail_guard_s` 内用相对判据 (播放中/刚播完);
  超过后回落到原绝对判据 (普通场景行为不变)。
- **最简化原则**: 打断不要求"硬切静音" — 固件 DMA 内已写入的最后 ~128ms 音频自然播完即可。

### 3.2 关键参数 (config.yaml `conversation.interruption`)

| 参数 | 默认 | 说明 |
|------|------|------|
| `enabled` | true | 总开关 |
| `speech_rms_threshold` | 400 | 绝对判据阈值 (播完后时段) |
| `speech_confirm_blocks` | 3 | 连续超阈值块数 (~375ms; 调 2 更灵敏) |
| `tail_guard_s` | 0.4 | 下行停止后尾音保护时长 |
| `echo_factor` | 0.8 | 回声水平估计系数 (下行片 RMS × 系数) |
| `barge_in_ratio` | 1.6 | 上行需超过回声估计的倍数 |
| `barge_in_floor` | 400 | 判据绝对底线 (防下行小时误触发) |

### 3.3 调参指引

- **误触发多** (机器人自己打断自己): 调大 `barge_in_ratio` (如 2.0) 或 `barge_in_floor`;
- **不灵敏** (喊了不停): 调低 `speech_confirm_blocks` (2) 或 `barge_in_ratio` (1.3);
- **诊断日志**: 插话候选时打印 `插话候选: up=? down=? thr=? hits=?` (标定依据)。

---

## 4. 其它本阶段成果

### 4.1 复读治理 (server `response_guard.py`)

- **复读用户话语**: 剔除仅在"复读片段从回复开头开始" (允许 ≤2 字语气词前缀) 时执行 —
  中段出现用户原话属正常"呼应/引用", 不再挖洞破坏播报 (实锤场景: "好的，我来检查一下语音状况。");
- **自我复读**: 短回复 (≤40 字) 相似度 ≥0.8 重生成; **长回复逐字重复 (≥0.95) 也拦截**;
- 参数: `user_echo_min_chars=6`、`user_echo_ratio=0.5`、`min_kept_chars=6`、`max_retries=1`。

### 4.2 上下文分层 (prefix cache 友好)

```
[1] system: 恒定引导 (角色/语言/风格/规则)   ← 永不变化 → 始终命中前缀缓存
[2] 历史 messages                            ← 窗口滑动, 自然复用
[3] system: 动态背景块 (更早摘要 → 用户更正)  ← 变化只失效其后的极短片段
[4] user: 当前问题                            ← 每轮新内容
```
原则: **"变的靠后放、不变的靠前放"**。

### 4.3 连接稳定性与诊断

- WebSocket 心跳: `enableHeartbeat(10s ping / 15s pong 超时 / 3 次)` —
  过紧参数 (3s×2) 在 server 繁忙时误判断线, 已放宽;
- 断连日志带计数与时间: `[WSc] Disconnected! #N (uptime=Xs, last_conn_ago=Ys)`;
- MCPClient 单次操作超时 8s (防"TCP 连上但 SSE 无响应"死循环卡死启动);
- 固件诊断: `[FD] play2s ... aec=xxxxus hwm=` (AEC 帧耗时/栈余量)、`[FD] audio task: stack~... outBuf=...`。

### 4.4 仿真验证工具

`firmware/test/aec_align_sim.py` — 延迟估计 6 场景 (12/50/128ms、人声干扰、弱回声、大噪声) +
端到端 NLMS 验证 (**宽带噪声型信号, 判别力完整: 无对齐不消/对齐 -45.5dB**;
注: 早期线谱信号会因 FIR 频域拟合特性产生假 PASS, 已修正判据为"相对输入的绝对衰减")。

---

## 5. 本阶段涉及文件

**固件**: `src/llm/RealtimeAEC.{h,cpp}`、`src/llm/ChatGPT/RealtimeChatGPT.{h,cpp}`、
`src/llm/ChatGPT/MCPClient.cpp`、`src/llm/RealtimeAudioStream.{h,cpp}`、
`platformio.ini`、`test/aec_align_sim.py`
**服务端**: `stackchan_server/orchestration/session.py`、`orchestration/response_guard.py`、
`engines/stt/sensevoice_engine.py`、`config.py`、`config.yaml`、`tools/test_repeat_guard.py`
