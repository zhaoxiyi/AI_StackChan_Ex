# 麦克风管理指南

> StackChan 模拟客户端 v4 — 麦克风设备选择、测试、采样率适配完整说明

---

## 目录

- [1. 命令行参数](#1-命令行参数)
- [2. 列出可用设备](#2-列出可用设备)
- [3. 选择指定麦克风](#3-选择指定麦克风)
- [4. 麦克风功能测试](#4-麦克风功能测试)
- [5. 采样率自动适配](#5-采样率自动适配)
- [6. 完整使用示例](#6-完整使用示例)
- [7. 故障排查](#7-故障排查)

---

## 1. 命令行参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `--list-devices` | flag | - | 列出所有输入设备并退出 |
| `--mic` | str/int | 系统默认 | 选择麦克风 (索引号 或 名称子串) |
| `--test-mic [N]` | float | 3.0 | 测试麦克风录制 N 秒 (不指定 N 时默认 3s) |

---

## 2. 列出可用设备

```bash
python tools/stackchan_client.py --list-devices
```

**输出示例：**

```
#    输入    名称                                      采样率
--------------------------------------------------------------
0    2       MacBook Pro 麦克风                         48000     ← 默认
1    1       Yeti Stereo Microphone                     44100
2    1       USB Audio Device                           48000
```

**字段说明：**
- `#` — 设备索引号，用于 `--mic` 参数
- `输入` — 最大输入通道数 (>0 表示可用作麦克风)
- `名称` — 操作系统识别的设备名称
- `采样率` — 设备原生默认采样率 (Hz)
- `← 默认` — 标记当前系统默认输入设备

---

## 3. 选择指定麦克风

### 3.1 按索引号

```bash
python tools/stackchan_client.py --mic 1
```

### 3.2 按名称子串（大小写不敏感）

```bash
python tools/stackchan_client.py --mic "Yeti"
python tools/stackchan_client.py --mic "USB"
python tools/stackchan_client.py --mic "macbook"
```

### 3.3 不指定（使用系统默认）

```bash
python tools/stackchan_client.py
```

**匹配逻辑 (源码 `_resolve_mic`)：**

```
--mic 参数
  ├─ None           → 用 sd.default.device[0]
  ├─ int (如 "1")   → 直接用索引 1
  └─ str (如 "Yeti")→ 遍历所有 input_channels>0 的设备
                     匹配 d['name'].lower() 包含该子串的第一个
```

---

## 4. 麦克风功能测试

### 4.1 基础测试（录制 3 秒）

```bash
python tools/stackchan_client.py --test-mic
```

### 4.2 自定义时长

```bash
python tools/stackchan_client.py --test-mic 5
```

### 4.3 指定设备测试

```bash
python tools/stackchan_client.py --test-mic --mic 1
python tools/stackchan_client.py --test-mic 3 --mic "Yeti"
```

### 4.4 测试流程

```
选择设备 → 显示设备信息
         → 🔴 录制 N 秒 (100ms 分块, 实时 RMS 功率条)
         → ✅ 录制完成
         → 统计分析 (RMS / 峰值 / 削波 / 静音占比 / 诊断)
         → 💾 自动保存 WAV 文件
         → 🔊 本地回放
```

### 4.5 输出示例

```
==================================================
  麦克风测试
==================================================
  设备: #1 Yeti Stereo Microphone
  设备原生采样率: 44100 Hz
  录制时长: 3.0 秒
  格式: mono, int16
==================================================

🔴 录制中 (3.0s @ 44100Hz)...  对着麦克风说话或制造声音
   进度: ████████████████ RMS=1850
✅ 录制完成: 132300 samples (3.00s)

==================================================
  录音统计
==================================================
  RMS (整体):     1245
  峰值:           24500 / 32767 (74.8%)
  削波样本数:     0
  静音占比:       12%
  诊断:           ✅ 正常
==================================================

💾 自动保存: mic_test_20260810_120000.wav

🔊 播放录音...
✅ 播放完成
```

### 4.6 诊断指标说明

| 指标 | 正常范围 | 异常含义 |
|------|---------|---------|
| RMS | > 50 | < 50 表示信号太弱 / 麦克风静音 |
| 峰值占比 | 1% ~ 95% | < 1% 无有效信号; 接近 100% 信号过强 |
| 削波样本 | 0 ~ 10 | > 10 表示增益过高，音频失真 |
| 静音占比 | < 50% | > 50% 表示大部分时间是静音 |

---

## 5. 采样率自动适配

### 5.1 设计原理

```
                    ┌─────────────────────┐
                    │  sounddevice 录音    │
                    │  设备原生采样率       │
                    │  (44100/48000 Hz)    │
                    └─────────┬───────────┘
                              │ PCM 16-bit raw
                              ▼
                    ┌─────────────────────┐
                    │  _resample()        │
                    │  线性插值重采样      │
                    │  native → 16000 Hz  │
                    └─────────┬───────────┘
                              │ PCM 16-bit 16kHz
                              ▼
                    ┌─────────────────────┐
                    │  base64 编码        │
                    │  → 发送到服务端      │
                    │  (对齐固件协议)      │
                    └─────────────────────┘
```

### 5.2 关键实现

```python
def _resolve_mic(self):
    """返回 (device_id, native_sample_rate)"""
    devs = sd.query_devices()
    # ...
    rate = max(16000, int(devs[di]['default_samplerate']))
    return di, rate

def _resample(data: bytes, src_rate: int, dst_rate: int) -> bytes:
    """PCM 16-bit 线性插值重采样"""
    s = np.frombuffer(data, dtype=np.int16).astype(np.float64)
    ratio = dst_rate / src_rate
    nl = int(len(s) * ratio)
    s = np.interp(np.linspace(0, len(s) - 1, nl), np.arange(len(s)), s)
    return s.astype(np.int16).tobytes()
```

### 5.3 调用链

| 场景 | 录音采样率 | 重采样 | 保存/发送采样率 |
|------|-----------|--------|----------------|
| `_mic_mode` (实时交互) | 设备原生 | → 16000 Hz | 16000 Hz (服务端协议) |
| `_cmd_test_mic` (测试) | 设备原生 | 不重采样 | 设备原生 (保持音质) |

---

## 6. 完整使用示例

```bash
# 1. 先查看可用设备
python tools/stackchan_client.py --list-devices

# 2. 测试指定设备是否正常工作
python tools/stackchan_client.py --test-mic --mic "Yeti"

# 3. 用指定设备连接服务端
python tools/stackchan_client.py --host 192.168.1.100 --port 8080 --mic "Yeti"

# 4. WAV 模式 + 指定设备 (不需要麦克风)
python tools/stackchan_client.py --host 192.168.1.100 --wav hello.wav --out reply.wav
```

---

## 7. 故障排查

| 症状 | 可能原因 | 解决 |
|------|---------|------|
| `--list-devices` 无输出 | sounddevice 未安装 | `pip install sounddevice` |
| 录音全静音 (RMS ~0) | 系统权限未授权麦克风 | macOS: 系统设置→隐私→麦克风→允许终端 |
| | 默认设备不是麦克风 | `--list-devices` 查看，用 `--mic N` 指定 |
| | 设备被其他程序占用 | 关闭 Zoom/Teams 等应用 |
| 削波样本很多 (>10) | 麦克风增益过高 | 降低系统音量/增益 |
| | 距离太近 | 距离麦克风 15-30cm |
| 重采样后音质差 | 线性插值在高倍率转换时损失高频 | 正常现象，语音识别不受影响 |
| `sd.rec()` 报错 | 设备索引无效 | 用 `--list-devices` 确认索引号存在 |
