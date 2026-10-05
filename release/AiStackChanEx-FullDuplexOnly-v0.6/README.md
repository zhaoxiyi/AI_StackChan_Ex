# AiStackChanEx — FullDuplexOnly v0.6 (全双工语音对话基础版)

**版本定位**: 仅含全双工语音对话基础功能的阶段性固件版本 — **不含**后续规划中的
联网信息查询 / 实时翻译 / Harness 调度等扩展。供其它 Stack-chan (M5Stack CoreS3)
直接烧录使用。

- **构建环境**: `m5stack-cores3-realtime` (PlatformIO, espressif32@6.3.2)
- **构建日期**: 2026-10-06
- **上游提交**: `9ff05c3` (Add duplex voice tuning: NLMS AEC fixes, STT energy gate, barge-in v1)
- **分支**: `full-duplex-only-v0.6`
- **内置能力**: 全双工 I2S 采集/播放、NLMS AEC + 自动延迟对齐、WebSocket 心跳保活、
  ES7210 麦克风初始化、AEC 耗时诊断

## 文件清单

| 文件 | 烧录地址 | 说明 |
|------|---------|------|
| `AiStackChanEx-FullDuplexOnly-v0.6_merged.bin` | **0x0** | 合并镜像 (推荐, 单文件) |
| `bootloader.bin` | 0x0 | 引导程序 |
| `partitions.bin` | 0x8000 | 分区表 (16MB 配置) |
| `boot_app0.bin` | 0xe000 | OTA 数据初始化 |
| `firmware.bin` | 0x10000 | 应用固件 |

校验和见 `SHA256SUMS`。

## 烧录方法

### 方式 A — 合并镜像 (推荐)

```bash
esptool.py --chip esp32s3 --port <PORT> --baud 460800 \
  write_flash 0x0 AiStackChanEx-FullDuplexOnly-v0.6_merged.bin
```

### 方式 B — 分文件

```bash
esptool.py --chip esp32s3 --port <PORT> --baud 460800 write_flash \
  0x0 bootloader.bin 0x8000 partitions.bin 0xe000 boot_app0.bin 0x10000 firmware.bin
```

### 方式 C — PlatformIO (源码构建)

```bash
cd firmware && pio run -e m5stack-cores3-realtime -t upload
```

## 使用前提

1. **上位机服务**: 需在局域网内运行 `server/` 的 stackchan_server
   (STT/LLM/TTS 管道), 并在 SD 卡配置 (`Copy-to-SD/yaml/SC_SecConfig.yaml`)
   中填好服务端地址;
2. 首次上电后点触摸屏开始会话;对讲链路可用后按需调参
   (见 `doc/duplex_voice_tuning.md` 的 AEC/打断参数表);
3. 如需 AEC 诊断输出, 使用 `m5stack-cores3-realtime-diag` 环境构建 (带 `-DAEC_DIAG`)。

## 备注

- 分区表为 `my_cores3_16MB.csv` (16MB Flash);
- Flash 参数: QIO 80MHz, 16MB;
- 本版本 AEC 采用自研 NLMS (esp-sr 预编译库实测不兼容, 已停用, 详见调试文档)。
