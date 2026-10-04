/**
 * @file main.cpp
 * @brief CoreS3 I2S 全双工验证 (v3 — 单通道帧格式修复)
 *
 * 进展:
 *   v1: 录音通路正常 (ES7210 ✓), 播放无声 (AW88298)
 *   v2: 对照实验定位 — M5Unified (tone) 有声, 我的 duplex 无声;
 *       I2C 回读全部正常 (电源位/I2SEN/档位/音量都写入成功)
 *   v3: 根因 = 帧格式频道数。AW88298 采样率档位编码的是期望 BCK 频率
 *       (16 × fs); M5Unified 用 I2S_CHANNEL_FMT_ONLY_RIGHT (单通道,
 *       16 BCK/帧)。之前用 RIGHT_LEFT (stereo, 32 BCK/帧) → BCK 翻倍
 *       → AW88298 时序错位 → 无声。
 *
 *       修复: channel_format = ONLY_RIGHT (与 M5Unified 一致)。
 *       duplex 单实例 TX/RX 共享帧格式 → ES7210 也按单通道读取,
 *       正好符合语音助手的单声道需求 (服务端 STT 本就是单声道处理)。
 *
 * 串口命令:
 *   r — 录 2 秒 → 回放
 *   d — 全双工: 录 A → 边回放 A 边录 B → 回放 B
 *   t — 440Hz 正弦波播放 1 秒 (播放通路)
 *   s — 循环切换帧格式 ONLY_RIGHT → ONLY_LEFT → STEREO (对比实验)
 *   m — M5Unified 对照 (Speaker.tone)
 *   i — 回读 codec 寄存器状态
 *   ? — 帮助
 */
#include <Arduino.h>
#include <M5Unified.h>
#include <driver/i2s.h>
#include <soc/i2s_struct.h>      // 直接寄存器修正 (对齐 M5Unified 的 S3 时钟处理)
#include "RealtimeAEC.h"        // 回声消除 (与主固件同一模块)
#include <esp_aec.h>            // 官方 esp-sr AEC (对照实验)

// ---- CoreS3 I2S 引脚 (对齐 M5Unified 硬件定义) ----
#define PIN_I2S_MCK   0
#define PIN_I2S_BCK   34
#define PIN_I2S_WS    33
#define PIN_I2S_DOUT  13     // → AW88298
#define PIN_I2S_DIN   14     // ← ES7210
#define I2S_PORT      I2S_NUM_1

// ---- I2C 从机地址 (对齐 M5Unified) ----
#define ES7210_ADDR   0x40
#define AW88298_ADDR  0x36
#define AW9523_ADDR   0x58

#define SAMPLE_RATE   16000
#define REC_SECONDS   2
#define REC_FRAMES    (SAMPLE_RATE * REC_SECONDS)
#define BUF_BYTES     (REC_FRAMES * 2 * 4)   // 预分配按 stereo 4B/帧

#define DMA_DESC_NUM  6
#define DMA_FRAME_NUM 240

static int16_t* g_buf_a = nullptr;
static int16_t* g_buf_b = nullptr;
static int g_i2c_fail = 0;
static int g_rate = SAMPLE_RATE;             // 当前 duplex 采样率 (可切 16k/48k)
static RealtimeAEC g_aec;                     // 回声消除

// 全双工双任务 (播放/采集并行流水线)
static volatile bool g_hd_play_done = true;
static volatile bool g_hd_cap_done = true;
static volatile size_t g_hd_got_b = 0;
static void hdPlaybackTask(void* arg);
static void hdCaptureTask(void* arg);

// 前向声明 (定义在文件后部)
static void patch_rx_path(int fs);

// 当前帧格式: ONLY_RIGHT (与 M5Unified 一致, 16 BCK/帧) 为默认
static i2s_channel_fmt_t g_ch_fmt = I2S_CHANNEL_FMT_ONLY_RIGHT;

static inline int frame_bytes(void)
{
    return (g_ch_fmt == I2S_CHANNEL_FMT_RIGHT_LEFT) ? 4 : 2;
}

static const char* fmt_name(void)
{
    switch (g_ch_fmt) {
        case I2S_CHANNEL_FMT_ONLY_RIGHT: return "ONLY_RIGHT (mono, 16BCK/frame)";
        case I2S_CHANNEL_FMT_ONLY_LEFT:  return "ONLY_LEFT  (mono, 16BCK/frame)";
        default:                         return "RIGHT_LEFT (stereo, 32BCK/frame)";
    }
}

// =============================================================================
// I2C codec helpers
// =============================================================================

static bool es7210_write_reg(uint8_t reg, uint8_t value)
{
    bool ok = M5.In_I2C.writeRegister(ES7210_ADDR, reg, &value, 1, 400000);
    if (!ok) g_i2c_fail++;
    return ok;
}

static bool aw88298_write_reg(uint8_t reg, uint16_t value)
{
    bool ok = M5.In_I2C.writeRegister(AW88298_ADDR, reg, (const uint8_t*)&value, 2, 400000);
    if (!ok) g_i2c_fail++;
    return ok;
}

static uint16_t aw88298_read_reg(uint8_t reg)
{
    uint8_t buf[2] = {0, 0};
    if (!M5.In_I2C.readRegister(AW88298_ADDR, reg, buf, 2, 400000)) return 0xFFFF;
    return (uint16_t)buf[0] | ((uint16_t)buf[1] << 8);
}

static uint8_t es7210_read_reg(uint8_t reg)
{
    uint8_t v = 0;
    if (!M5.In_I2C.readRegister(ES7210_ADDR, reg, &v, 1, 400000)) return 0xFF;
    return v;
}

static uint8_t aw9523_read_reg(uint8_t reg)
{
    uint8_t v = 0;
    if (!M5.In_I2C.readRegister(AW9523_ADDR, reg, &v, 1, 400000)) return 0xFF;
    return v;
}

// =============================================================================
// codec 初始化 (对齐 M5Unified)
// =============================================================================

static void es7210_init(void)
{
    g_i2c_fail = 0;
    es7210_write_reg(0x00, 0xFF);
    struct __attribute__((packed)) { uint8_t reg; uint8_t value; } seq[] = {
        { 0x00, 0x41 }, { 0x01, 0x1f }, { 0x06, 0x00 }, { 0x07, 0x20 },
        { 0x08, 0x10 }, { 0x09, 0x30 }, { 0x0A, 0x30 },
        { 0x20, 0x0a }, { 0x21, 0x2a }, { 0x22, 0x0a }, { 0x23, 0x2a },
        { 0x02, 0xC1 }, { 0x04, 0x01 }, { 0x05, 0x00 }, { 0x11, 0x60 },
        { 0x40, 0x42 }, { 0x41, 0x70 }, { 0x42, 0x70 },
        { 0x43, 0x1B }, { 0x44, 0x1B }, { 0x45, 0x00 }, { 0x46, 0x00 },
        { 0x47, 0x00 }, { 0x48, 0x00 }, { 0x49, 0x00 }, { 0x4A, 0x00 },
        { 0x4B, 0x00 }, { 0x4C, 0xFF },
        { 0x01, 0x14 },
    };
    for (auto& d : seq) { es7210_write_reg(d.reg, d.value); }
    Serial.printf("[codec] ES7210 init (i2c_fail=%d)\n", g_i2c_fail);
}

static void aw88298_init(void)
{
    g_i2c_fail = 0;
    bool ok = M5.In_I2C.bitOn(AW9523_ADDR, 0x02, 0b00000100, 400000);
    Serial.printf("[codec] AW9523 P0 bit2 (功放电源) = %s\n", ok ? "OK" : "FAIL");
    delay(10);

    // 采样率档位按 M5Unified rate_tbl 计算 (值 = fs/2205 的档位)
    static const uint8_t tbl[] = {4,5,6,8,10,11,15,20,22,44};
    size_t rate = (size_t)((g_rate + 1102) / 2205);
    int idx = 0;
    while (idx < (int)sizeof(tbl) - 1 && rate > tbl[idx]) idx++;

    aw88298_write_reg(0x61, 0x0673);                  // boost mode disabled
    aw88298_write_reg(0x04, 0x4040);                  // I2SEN=1 AMPPD=0 PWDN=0
    aw88298_write_reg(0x05, 0x0008);                  // RMSE=0 HAGCE=0 HDCCE=0 HMUTE=0
    aw88298_write_reg(0x06, (uint16_t)(0x14C0 | idx));// 档位 (fs=%d)
    aw88298_write_reg(0x0C, 0x0020);                  // volume 降为 1/3 (原 0x64=full): 防近场回声饱和

    Serial.printf("[codec] AW88298 init (i2c_fail=%d, fs=%d, idx=%d) | rb 0x04=0x%04X 0x06=0x%04X\n",
                  g_i2c_fail, g_rate, idx, aw88298_read_reg(0x04), aw88298_read_reg(0x06));
}

// =============================================================================
// 全双工 I2S
// =============================================================================

static bool duplex_i2s_init(void)
{
    i2s_config_t cfg = {};
    cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX | I2S_MODE_RX);
    cfg.sample_rate = g_rate;
    cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
    cfg.channel_format = g_ch_fmt;                    // ← 关键: 单通道对齐 AW88298
    cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
    cfg.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
    cfg.dma_buf_count = DMA_DESC_NUM;
    cfg.dma_buf_len = DMA_FRAME_NUM;
    cfg.use_apll = false;
    cfg.tx_desc_auto_clear = true;

    esp_err_t err = i2s_driver_install(I2S_PORT, &cfg, 0, nullptr);
    if (err != ESP_OK) {
        Serial.printf("[i2s] driver_install FAILED: %d\n", (int)err);
        return false;
    }
    i2s_pin_config_t pins = {};
    pins.mck_io_num   = PIN_I2S_MCK;
    pins.bck_io_num   = PIN_I2S_BCK;
    pins.ws_io_num    = PIN_I2S_WS;
    pins.data_out_num = PIN_I2S_DOUT;
    pins.data_in_num  = PIN_I2S_DIN;
    err = i2s_set_pin(I2S_PORT, &pins);
    if (err != ESP_OK) {
        Serial.printf("[i2s] set_pin FAILED: %d\n", (int)err);
        return false;
    }
    i2s_zero_dma_buffer(I2S_PORT);
    Serial.printf("[i2s] duplex installed | fmt=%s | fs=%d | BCK=%uHz\n",
                  fmt_name(), g_rate, (unsigned)(g_rate * 16 * (frame_bytes() / 2)));
    return true;
}

// =============================================================================
// 音频工具 (按当前帧格式处理)
// =============================================================================

static void print_rms(const char* tag, const int16_t* buf, size_t frames)
{
    const int fb = frame_bytes();
    double sum = 0;
    int16_t peak = 0;
    for (size_t i = 0; i < frames; i++) {
        // 单通道: 取每帧第一个样本; stereo: 分别统计 L (报告 L/2 与 R/2 和值)
        int16_t v = buf[i * (fb / 2) * 2 / 2];   // 帧首样本 (mono 即全部; stereo 为 L)
        sum += (double)v * v;
        if (abs(v) > peak) peak = abs(v);
    }
    float rms = sqrtf((float)(sum / frames));
    Serial.printf("[%s] 帧=%dB RMS=%.0f Peak=%d\n", tag, fb, rms, peak);
}

static bool record(int16_t* buf, size_t frames)
{
    size_t want = frames * frame_bytes();
    size_t got = 0;
    esp_err_t err = i2s_read(I2S_PORT, buf, want, &got, pdMS_TO_TICKS(3000));
    if (err != ESP_OK || got != want) {
        Serial.printf("[rec] read err=%d got=%u/%u\n", (int)err, (unsigned)got, (unsigned)want);
        return false;
    }
    return true;
}

static bool play(const int16_t* buf, size_t frames)
{
    size_t want = frames * frame_bytes();
    size_t written = 0;
    esp_err_t err = i2s_write(I2S_PORT, buf, want, &written, pdMS_TO_TICKS(3000));
    if (err != ESP_OK || written != want) {
        Serial.printf("[play] write err=%d wrote=%u/%u\n", (int)err, (unsigned)written, (unsigned)want);
        return false;
    }
    return true;
}

static void play_and_drain(const int16_t* buf, size_t frames)
{
    play(buf, frames);
    delay((frames * 1000) / g_rate + 120);
}

// =============================================================================
// 测试命令
// =============================================================================

static void cmd_record_play(void)
{
    Serial.println("=== [r] 录音 2 秒, 请说话 ===");
    if (!record(g_buf_a, REC_FRAMES)) return;
    print_rms("recA", g_buf_a, REC_FRAMES);
    Serial.println("=== 回放 ===");
    play_and_drain(g_buf_a, REC_FRAMES);
    Serial.println("=== [r] 完成 ===");
}

static void cmd_duplex(void)
{
    Serial.println("=== [d] 第一步: 录 A (2 秒, 请说 \"测试全双工 A\") ===");
    if (!record(g_buf_a, REC_FRAMES)) return;
    print_rms("recA", g_buf_a, REC_FRAMES);
    delay(300);

    Serial.println("=== [d] 第二步: 边回放 A 边录 B — 现在对设备说一句新话 ===");
    const size_t CHUNK_BYTES = (g_rate / 10) * frame_bytes();   // 100ms
    size_t total = REC_FRAMES * frame_bytes();
    size_t off = 0;
    while (off + CHUNK_BYTES <= total) {
        size_t w = 0, r = 0;
        i2s_write(I2S_PORT, (uint8_t*)g_buf_a + off, CHUNK_BYTES, &w, pdMS_TO_TICKS(1000));
        i2s_read(I2S_PORT, (uint8_t*)g_buf_b + off, CHUNK_BYTES, &r, pdMS_TO_TICKS(1000));
        off += CHUNK_BYTES;
    }
    print_rms("recB", g_buf_b, REC_FRAMES);
    delay(300);

    Serial.println("=== [d] 第三步: 回放 B (应能听到你刚才说的新话 + 少量回声) ===");
    play_and_drain(g_buf_b, REC_FRAMES);
    Serial.println("=== [d] 完成 ===");
}

static void cmd_tone(void)
{
    Serial.println("=== [t] 440Hz 正弦波 1 秒 ===");
    const int n = g_rate;
    const int fb = frame_bytes();
    uint8_t* p = (uint8_t*)g_buf_a;
    for (int i = 0; i < n; i++) {
        int16_t v = (int16_t)(8000.0f * sinf(2.0f * PI * 440.0f * i / g_rate));
        memcpy(p + i * fb, &v, 2);                    // 帧首样本
        if (fb == 4) { memcpy(p + i * fb + 2, &v, 2); }  // stereo 填右槽
    }
    play_and_drain(g_buf_a, n);
    Serial.println("=== [t] 完成 (应听到 1 秒蜂鸣) ===");
}

/** s: 循环切换帧格式并重装 duplex */
static void cmd_switch_format(void)
{
    switch (g_ch_fmt) {
        case I2S_CHANNEL_FMT_ONLY_RIGHT: g_ch_fmt = I2S_CHANNEL_FMT_ONLY_LEFT; break;
        case I2S_CHANNEL_FMT_ONLY_LEFT:  g_ch_fmt = I2S_CHANNEL_FMT_RIGHT_LEFT; break;
        default:                         g_ch_fmt = I2S_CHANNEL_FMT_ONLY_RIGHT; break;
    }
    i2s_driver_uninstall(I2S_PORT);
    delay(80);
    if (duplex_i2s_init()) {
        aw88298_init();   // 重装后确保功放配置在位
    }
    Serial.printf("[fmt] 已切换, 现在按 t 测试播放 / r 测试录放\n");
}

static void cmd_m5_compare(void)
{
    Serial.println("=== [m] M5Unified 对照实验 (卸载 duplex → Speaker.tone) ===");
    i2s_driver_uninstall(I2S_PORT);
    delay(100);

    if (M5.Speaker.begin()) {
        M5.Speaker.setVolume(200);
        Serial.println("[m] Speaker.begin OK, tone 1kHz 0.8s ...");
        M5.Speaker.tone(1000, 800);
        delay(1300);
        M5.Speaker.end();
        Serial.println("[m] tone 已播放 — 若无声 = AW88298 通路本身有问题");
    } else {
        Serial.println("[m] Speaker.begin FAILED");
    }
    delay(100);

    if (duplex_i2s_init()) {
        aw88298_init();
    }
    Serial.println("=== [m] 完成 (duplex 已恢复) ===");
}

/** 2: M5Unified 播放 16kHz 正弦 — 验证 16k 是否在 AW88298 上可用 */
static void cmd_m5_tone16k(void)
{
    Serial.println("=== [2] M5Unified playRaw 16kHz 正弦 ===");
    i2s_driver_uninstall(I2S_PORT);
    delay(100);
    if (M5.Speaker.begin()) {
        M5.Speaker.setVolume(200);
        const int n = 16000;
        for (int i = 0; i < n; i++) {
            g_buf_a[i] = (int16_t)(8000.0f * sinf(2.0f * PI * 440.0f * i / 16000.0f));
        }
        Serial.println("[2] playRaw(16000Hz) 1s ...");
        M5.Speaker.playRaw(g_buf_a, n, 16000);
        delay(1500);
        M5.Speaker.end();
        Serial.println("[2] 完成 — 若无声 = 16k 在 AW88298 上不可用");
    } else {
        Serial.println("[2] Speaker.begin FAILED");
    }
    delay(80);
    if (duplex_i2s_init()) aw88298_init();
}

/** 3: M5Unified 播放录音缓冲 A — 验证录音数据本身 */
static void cmd_m5_play_rec(void)
{
    Serial.println("=== [3] M5Unified 播放录音缓冲 (请先用 r 录一段) ===");
    i2s_driver_uninstall(I2S_PORT);
    delay(100);
    if (M5.Speaker.begin()) {
        M5.Speaker.setVolume(200);
        Serial.printf("[3] playRaw(%dHz, %d samples) ...\n", g_rate, (int)REC_FRAMES);
        M5.Speaker.playRaw(g_buf_a, REC_FRAMES, g_rate);
        delay((REC_FRAMES * 1000) / g_rate + 500);
        M5.Speaker.end();
        Serial.println("[3] 完成 — 若无声 = 录音数据/采样率不兼容");
    } else {
        Serial.println("[3] Speaker.begin FAILED");
    }
    delay(80);
    if (duplex_i2s_init()) aw88298_init();
}

/** n: duplex 切 48kHz 播放 — 验证 duplex 在 48k 是否工作 */
static void cmd_duplex_48k(void)
{
    Serial.println("=== [n] duplex 48kHz + 正弦 ===");
    i2s_driver_uninstall(I2S_PORT);
    delay(80);
    g_rate = 48000;
    if (duplex_i2s_init()) {
        aw88298_init();
        const int n = 48000;
        const int fb = frame_bytes();
        uint8_t* p = (uint8_t*)g_buf_a;
        for (int i = 0; i < n; i++) {
            int16_t v = (int16_t)(8000.0f * sinf(2.0f * PI * 440.0f * i / 48000.0f));
            memcpy(p + i * fb, &v, 2);
            if (fb == 4) { memcpy(p + i * fb + 2, &v, 2); }
        }
        size_t w = 0;
        i2s_write(I2S_PORT, g_buf_a, (size_t)n * fb, &w, pdMS_TO_TICKS(2000));
        delay(1300);
        Serial.println("[n] 完成 — 有声 = duplex 结构 OK, 之前无声是 16k 时钟问题");
    }
}

/** o: duplex 回到 16kHz */
static void cmd_duplex_16k(void)
{
    i2s_driver_uninstall(I2S_PORT);
    delay(80);
    g_rate = 16000;
    if (duplex_i2s_init()) aw88298_init();
    Serial.println("[o] duplex 已回到 16kHz");
}

/** 通用: TX-only 安装 (对齐 M5Unified speaker 安装方式) */
static bool tx_only_install(bool with_mclk, i2s_channel_fmt_t fmt, int rate)
{
    i2s_driver_uninstall(I2S_PORT);
    delay(80);
    i2s_config_t cfg = {};
    cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX);   // TX only
    cfg.sample_rate = rate;
    cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
    cfg.channel_format = fmt;
    cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
    cfg.intr_alloc_flags = ESP_INTR_FLAG_LEVEL1;
    cfg.dma_buf_count = DMA_DESC_NUM;
    cfg.dma_buf_len = DMA_FRAME_NUM;
    cfg.tx_desc_auto_clear = true;
    esp_err_t err = i2s_driver_install(I2S_PORT, &cfg, 0, nullptr);
    if (err != ESP_OK) { Serial.printf("[txonly] install FAILED %d\n", (int)err); return false; }
    i2s_pin_config_t pins = {};
    pins.mck_io_num   = with_mclk ? PIN_I2S_MCK : I2S_PIN_NO_CHANGE;
    pins.bck_io_num   = PIN_I2S_BCK;
    pins.ws_io_num    = PIN_I2S_WS;
    pins.data_out_num = PIN_I2S_DOUT;
    pins.data_in_num  = I2S_PIN_NO_CHANGE;
    i2s_set_pin(I2S_PORT, &pins);
    i2s_zero_dma_buffer(I2S_PORT);
    Serial.printf("[txonly] installed | mclk=%s fmt=%s rate=%d\n",
                  with_mclk ? "yes" : "no", fmt_name(), rate);
    return true;
}

/** 生成并播放正弦 (按当前帧格式 + 指定采样率) */
static void play_sine(int rate, int ms)
{
    const int n = rate * ms / 1000;
    const int fb = frame_bytes();
    uint8_t* p = (uint8_t*)g_buf_a;
    for (int i = 0; i < n; i++) {
        int16_t v = (int16_t)(8000.0f * sinf(2.0f * PI * 440.0f * i / rate));
        memcpy(p + i * fb, &v, 2);
        if (fb == 4) { memcpy(p + i * fb + 2, &v, 2); }
    }
    size_t w = 0;
    i2s_write(I2S_PORT, g_buf_a, (size_t)n * fb, &w, pdMS_TO_TICKS(3000));
    delay(ms + 300);
}

/** 恢复 duplex 环境 (TX-only 实验后调用) */
static void restore_duplex(void)
{
    if (duplex_i2s_init()) aw88298_init();
}

/**
 * 复刻 M5Unified 在 S3 上的 TX 寄存器修正:
 *   - tx_bck_div_num = div_m - 1  (无 MCLK: div_m=2→1; 有 MCLK: div_m=8→7)
 *     BCK 频率的关键! IDF 驱动默认值与此不同 → BCK 错误 → 无声
 *   - mono 模式: tx_chan_equal + tx_mono (硬件把单声道样本复制到左右槽)
 */
static void patch_tx_regs(bool with_mclk, bool mono_copy)
{
    i2s_dev_t* dev = (I2S_PORT == I2S_NUM_0) ? &I2S0 : &I2S1;
    dev->tx_conf1.tx_bck_div_num = with_mclk ? 7 : 1;   // div_m - 1
    if (mono_copy) {
        dev->tx_conf.tx_mono = 1;
        dev->tx_conf.tx_chan_equal = 1;
    }
    Serial.printf("[patch] tx_bck_div_num=%d mono_copy=%d\n",
                  (int)dev->tx_conf1.tx_bck_div_num, mono_copy ? 1 : 0);
}

/** 7: TX-only 无 MCLK stereo + bck_div 修正 */
static void cmd_patch_txonly(void)
{
    Serial.printf("=== [7] TX-only 无MCLK stereo @%d + tx_bck_div_num=1 ===\n", g_rate);
    if (tx_only_install(false, I2S_CHANNEL_FMT_RIGHT_LEFT, g_rate)) {
        delay(50);
        patch_tx_regs(false, false);
        play_sine(g_rate, 1000);
        Serial.println("[7] 完成 — 有声 = 根因确认: tx_bck_div_num");
    }
    delay(80);
    restore_duplex();
}

/** 8: duplex 有 MCLK stereo + bck_div 修正 */
static void cmd_patch_duplex(void)
{
    Serial.printf("=== [8] duplex 有MCLK stereo @%d + tx_bck_div_num=7 ===\n", g_rate);
    i2s_driver_uninstall(I2S_PORT);
    delay(80);
    g_ch_fmt = I2S_CHANNEL_FMT_RIGHT_LEFT;
    if (duplex_i2s_init()) {
        delay(50);
        patch_tx_regs(true, false);
        aw88298_init();
        play_sine(g_rate, 1000);
        Serial.println("[8] 完成 — 有声 = duplex+MCLK 下补丁生效");
    }
}

/** 9: duplex 有 MCLK mono + 完整复刻 (tx_chan_equal + bck_div) */
static void cmd_patch_duplex_full(void)
{
    Serial.printf("=== [9] duplex 有MCLK mono @%d + 完整复刻 ===\n", g_rate);
    i2s_driver_uninstall(I2S_PORT);
    delay(80);
    g_ch_fmt = I2S_CHANNEL_FMT_ONLY_RIGHT;
    if (duplex_i2s_init()) {
        delay(50);
        patch_tx_regs(true, true);
        aw88298_init();
        play_sine(g_rate, 1000);
        Serial.println("[9] 完成 — 有声 = 完整复刻方案成立 (最终方案)");
    }
}

/** 4: TX-only, 无 MCLK, stereo — 完全对齐 M5Unified speaker 安装 */
static void cmd_txonly_nomck(void)
{
    Serial.printf("=== [4] TX-only 无 MCLK stereo @%d ===\n", g_rate);
    if (tx_only_install(false, I2S_CHANNEL_FMT_RIGHT_LEFT, g_rate)) {
        play_sine(g_rate, 1000);
        Serial.println("[4] 完成 — 有声 = duplex/MCLK 是问题源");
    }
    delay(80);
    restore_duplex();
}

/** 5: TX-only, 有 MCLK, stereo — 隔离 MCLK 变量 */
static void cmd_txonly_mck(void)
{
    Serial.printf("=== [5] TX-only 有 MCLK stereo @%d ===\n", g_rate);
    if (tx_only_install(true, I2S_CHANNEL_FMT_RIGHT_LEFT, g_rate)) {
        play_sine(g_rate, 1000);
        Serial.println("[5] 完成 — 与 [4] 对比可确认 MCLK 影响");
    }
    delay(80);
    restore_duplex();
}

/** 6: 借道实验 — 在 M5Unified 已初始化的 I2S 上直接 i2s_write */
static void cmd_write_via_m5(void)
{
    Serial.println("=== [6] 借道: M5.Speaker.begin 后直接 i2s_write ===");
    i2s_driver_uninstall(I2S_PORT);
    delay(100);
    if (M5.Speaker.begin()) {
        M5.Speaker.setVolume(200);
        // M5.Speaker 安装的是 mono (ONLY_RIGHT), 默认 48k → 写 mono 48k 数据
        const int n = 48000;
        const int fb = 2;   // mono 帧 = 2 字节
        uint8_t* p = (uint8_t*)g_buf_a;
        for (int i = 0; i < n; i++) {
            int16_t v = (int16_t)(8000.0f * sinf(2.0f * PI * 440.0f * i / 48000.0f));
            memcpy(p + i * fb, &v, 2);
        }
        size_t w = 0;
        esp_err_t err = i2s_write(I2S_PORT, g_buf_a, (size_t)n * fb, &w, pdMS_TO_TICKS(3000));
        Serial.printf("[6] i2s_write → M5Unified通道: err=%d wrote=%u/%u\n",
                      (int)err, (unsigned)w, (unsigned)(n * fb));
        delay(1300);
        M5.Speaker.end();
        Serial.println("[6] 完成 — 有声 = 数据/write OK, 问题在我的 I2S 安装参数");
    } else {
        Serial.println("[6] Speaker.begin FAILED");
    }
    delay(80);
    restore_duplex();
}

/**
 * 复刻 M5Unified speaker 安装参数 (逐字段对齐):
 *   mode=MASTER|TX[|RX]  sample_rate=48000(dummy)  ONLY_RIGHT(mono)
 *   intr_alloc_flags=0   dma 8×256   tx_desc_auto_clear=true
 */
static bool mimic_install(bool with_rx, bool with_mclk, int dummy_rate)
{
    i2s_driver_uninstall(I2S_PORT);
    delay(80);
    i2s_config_t cfg;
    memset(&cfg, 0, sizeof(cfg));
    cfg.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX | (with_rx ? I2S_MODE_RX : 0));
    cfg.sample_rate = dummy_rate;
    cfg.bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT;
    cfg.channel_format = I2S_CHANNEL_FMT_ONLY_RIGHT;
    cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
    cfg.intr_alloc_flags = 0;
    cfg.dma_buf_count = 8;      // M5Unified Speaker 默认
    cfg.dma_buf_len = 256;
    cfg.tx_desc_auto_clear = true;
    esp_err_t err = i2s_driver_install(I2S_PORT, &cfg, 0, nullptr);
    if (err != ESP_OK) { Serial.printf("[mimic] install FAILED %d\n", (int)err); return false; }
    i2s_pin_config_t pins;
    memset(&pins, 0, sizeof(pins));
    pins.mck_io_num   = with_mclk ? PIN_I2S_MCK : I2S_PIN_NO_CHANGE;
    pins.bck_io_num   = PIN_I2S_BCK;
    pins.ws_io_num    = PIN_I2S_WS;
    pins.data_out_num = PIN_I2S_DOUT;
    pins.data_in_num  = with_rx ? PIN_I2S_DIN : I2S_PIN_NO_CHANGE;
    i2s_set_pin(I2S_PORT, &pins);
    i2s_zero_dma_buffer(I2S_PORT);
    Serial.printf("[mimic] installed rx=%d mclk=%d rate=%d (dma 8x256, intr=0, MONO)\n",
                  with_rx, with_mclk, dummy_rate);
    return true;
}

/** 播放 mono 正弦 (直接 i2s_write, 数据速率须匹配实际 I2S 时钟) */
static void play_sine_mono(int rate, int ms)
{
    const int n = rate * ms / 1000;
    for (int i = 0; i < n; i++) {
        g_buf_a[i] = (int16_t)(8000.0f * sinf(2.0f * PI * 440.0f * i / rate));
    }
    size_t w = 0;
    esp_err_t err = i2s_write(I2S_PORT, g_buf_a, (size_t)n * 2, &w, pdMS_TO_TICKS(3000));
    Serial.printf("[play] mono write err=%d w=%u/%u\n", (int)err, (unsigned)w, (unsigned)(n * 2));
    delay(ms + 300);
}

/** a: 复刻 M5Unified install (TX-only 无MCLK dummy48k) + 48k 数据 */
static void cmd_mimic_tx(void)
{
    Serial.println("=== [a] 复刻 M5Unified install: TX-only 无MCLK dummy48k ===");
    if (mimic_install(false, false, 48000)) {
        delay(50);
        play_sine_mono(48000, 1000);
        Serial.println("[a] 完成 — 有声 = install 参数就是根因");
    }
    delay(80);
    i2s_driver_uninstall(I2S_PORT); delay(50);
    g_rate = 16000;
    if (duplex_i2s_init()) aw88298_init();
}

/** b: 复刻 install + RX + MCLK (duplex, dummy48k) + 48k 数据 */
static void cmd_mimic_duplex(void)
{
    Serial.println("=== [b] 复刻 install + RX + MCLK (duplex dummy48k) ===");
    if (mimic_install(true, true, 48000)) {
        delay(50);
        g_rate = 48000;
        aw88298_init();          // 档位按 48k
        play_sine_mono(48000, 1000);
        Serial.println("[b] 完成 — 有声 = 全双工最终方案成立 (48k)");
    }
}

/** c: 复刻 install + i2s_set_clk(16000) → 验证 16k 切换 */
static void cmd_mimic_16k(void)
{
    Serial.println("=== [c] 复刻 install + i2s_set_clk(16000) ===");
    if (mimic_install(true, true, 48000)) {
        delay(50);
        i2s_set_clk(I2S_PORT, 16000, I2S_BITS_PER_SAMPLE_16BIT, I2S_CHANNEL_MONO);
        g_rate = 16000;
        aw88298_init();
        play_sine_mono(16000, 1000);
        Serial.println("[c] 完成 — 有声 = 16k 切换可用");
    }
}

/** ===== M5Unified calcClockDiv 完整复制 (S3 时钟分数分频计算) ===== */
static void calcClockDiv(uint32_t* div_a, uint32_t* div_b, uint32_t* div_n, uint32_t baseClock, uint32_t targetFreq)
{
    if (baseClock <= targetFreq << 1) {
        *div_n = 2; *div_a = 1; *div_b = 0;
        return;
    }
    uint32_t save_n = 255, save_a = 63, save_b = 62;
    if (targetFreq) {
        float fdiv = (float)baseClock / targetFreq;
        uint32_t n = (uint32_t)fdiv;
        if (n < 256) {
            fdiv -= n;
            float check_base = baseClock;
            while ((int32_t)targetFreq >= 0) { targetFreq <<= 1; check_base *= 2; }
            float check_target = targetFreq;
            uint32_t save_diff = UINT32_MAX;
            if (n < 255) {
                save_a = 1; save_b = 0; save_n = n + 1;
                save_diff = abs((int)(check_target - check_base / (float)save_n));
            }
            for (uint32_t a = 1; a < 64; ++a) {
                uint32_t b = roundf(a * fdiv);
                if (a <= b) { continue; }
                uint32_t diff = abs((int)(check_target - ((check_base * a) / (n * a + b))));
                if (save_diff <= diff) { continue; }
                save_diff = diff;
                save_a = a; save_b = b; save_n = n;
                if (!diff) { break; }
            }
        }
    }
    *div_n = save_n; *div_a = save_a; *div_b = save_b;
}

/**
 * 完整复刻 M5Unified 在 S3 上的 I2S TX 时钟配置:
 *   - PLL_240M 时钟源 (tx_clk_sel=1) + calcClockDiv 精确分数分频
 *   - tx_bck_div_num = div_m - 1  (无 MCLK:2→1; 有 MCLK:8→7)
 *   - mono 时 tx_chan_equal + tx_mono (样本复制到左右槽)
 *   - tx_update 使配置生效
 *   目标频率 = div_m × 16 × fs  (无 MCLK: 32fs; 有 MCLK: 128fs)
 */
static void patch_i2s_clock(int fs, bool with_mclk, bool mono_copy)
{
    constexpr uint32_t PLL_D2_CLK = 120 * 1000 * 1000;   // S3: 240MHz/2
    const uint32_t bits = 16;
    uint32_t div_m = with_mclk ? 8 : 2;                  // 32/bits = 2; 有 MCLK 时 8
    uint32_t div_a, div_b, div_n;
    calcClockDiv(&div_a, &div_b, &div_n, PLL_D2_CLK, div_m * bits * (uint32_t)fs);

    i2s_dev_t* dev = (I2S_PORT == I2S_NUM_0) ? &I2S0 : &I2S1;
    dev->tx_conf1.tx_bck_div_num = div_m - 1;
    if (mono_copy) {
        dev->tx_conf.tx_mono = 1;
        dev->tx_conf.tx_chan_equal = 1;
    }

    bool yn1 = (div_b > (div_a >> 1));
    if (yn1) { div_b = div_a - div_b; }
    int div_y = 1, div_x = 0;
    if (div_b) {
        div_x = div_a / div_b - 1;
        div_y = div_a % div_b;
        if (div_y == 0) { div_y = 1; div_b = 511; }
    }

    dev->tx_clkm_div_conf.tx_clkm_div_x = div_x;
    dev->tx_clkm_div_conf.tx_clkm_div_y = div_y;
    dev->tx_clkm_div_conf.tx_clkm_div_z = div_b;
    dev->tx_clkm_div_conf.tx_clkm_div_yn1 = yn1;
    dev->tx_clkm_conf.tx_clkm_div_num = div_n;
    dev->tx_clkm_conf.tx_clk_sel = 1;      // PLL_240M_CLK
    dev->tx_clkm_conf.clk_en = 1;
    dev->tx_clkm_conf.tx_clk_active = 1;

    dev->tx_conf.tx_update = 1;
    dev->tx_conf.tx_update = 0;

    Serial.printf("[clkpatch] fs=%d mclk=%d div_m=%u div_n=%u div_a=%u div_b=%u -> TX clk=%uHz\n",
                  fs, with_mclk, (unsigned)div_m, (unsigned)div_n,
                  (unsigned)div_a, (unsigned)div_b,
                  (unsigned)(div_m * bits * fs));
}

/** a2: TX-only 无MCLK + 完整时钟复刻 (mono 16k) */
static void cmd_clockpatch_tx(void)
{
    Serial.println("=== [a2] TX-only 无MCLK + 时钟复刻 @16k ===");
    if (mimic_install(false, false, 48000)) {
        delay(30);
        patch_i2s_clock(16000, false, true);
        g_rate = 16000;
        delay(20);
        play_sine_mono(16000, 1000);
        Serial.println("[a2] 完成 — 有声 = 时钟复刻就是最后一块拼图");
    }
    delay(80);
    i2s_driver_uninstall(I2S_PORT); delay(50);
    g_rate = 16000;
    if (duplex_i2s_init()) aw88298_init();
}

/** b2: duplex 有MCLK + 完整时钟复刻 (最终方案候选) */
static void cmd_clockpatch_duplex(void)
{
    Serial.println("=== [b2] duplex 有MCLK + 时钟复刻 @16k ===");
    if (mimic_install(true, true, 48000)) {
        delay(30);
        patch_i2s_clock(16000, true, true);
        g_rate = 16000;
        aw88298_init();
        delay(20);
        play_sine_mono(16000, 1000);
        Serial.println("[b2] 完成 — 有声 = 全双工方案成立!");
    }
}

/** g: M5.Speaker.begin (MCLK模式) + 直接 i2s_write/i2s_read 探测 */
static void cmd_m5_duplex_probe(void)
{
    Serial.println("=== [g] M5.Speaker.begin (MCLK=0) + 直接读写探测 ===");
    i2s_driver_uninstall(I2S_PORT);
    delay(100);

    // 让 M5Unified 用 MCLK 模式: div_m=8 时钟结构 + GPIO0 输出 MCLK
    auto spk_cfg = M5.Speaker.config();
    spk_cfg.pin_mck = (gpio_num_t)PIN_I2S_MCK;
    M5.Speaker.config(spk_cfg);

    if (M5.Speaker.begin()) {
        M5.Speaker.setVolume(200);
        delay(100);
        patch_rx_path(16000);      // ★ 补齐 RX 路径 (TXRX 均 16k)
        delay(50);

        // 1) 播放: 直接 i2s_write 16k mono
        Serial.println("[g] 播放: 直接 i2s_write 16k 正弦 ...");
        const int n = 16000;
        for (int i = 0; i < n; i++) {
            g_buf_a[i] = (int16_t)(8000.0f * sinf(2.0f * PI * 440.0f * i / 16000.0f));
        }
        size_t w = 0;
        esp_err_t err = i2s_write(I2S_PORT, g_buf_a, (size_t)n * 2, &w, pdMS_TO_TICKS(3000));
        Serial.printf("[g] write err=%d w=%u (应能听到蜂鸣)\n", (int)err, (unsigned)w);
        delay(1300);

        // 2) 录音: 直接 i2s_read
        Serial.println("[g] 录音: 直接 i2s_read 2 秒, 请说话 ...");
        size_t r = 0;
        err = i2s_read(I2S_PORT, g_buf_b, (size_t)16000 * 2 * 2, &r, pdMS_TO_TICKS(5000));
        Serial.printf("[g] read err=%d r=%u\n", (int)err, (unsigned)r);
        if (r >= 16) {
            size_t frames = r / 2;
            double sum = 0; int16_t peak = 0;
            for (size_t i = 0; i < frames; i++) {
                sum += (double)g_buf_b[i] * g_buf_b[i];
                if (abs(g_buf_b[i]) > peak) peak = abs(g_buf_b[i]);
            }
            Serial.printf("[g] rec RMS=%.0f Peak=%d (frames=%u)\n",
                          sqrtf((float)(sum / frames)), peak, (unsigned)frames);
            Serial.println("[g] RMS 随说话变化 = RX 可行! 相反则需改库装 duplex");
        }
        M5.Speaker.end();
    } else {
        Serial.println("[g] Speaker.begin FAILED");
    }
    delay(80);
    // 恢复 pin_mck 默认
    auto cfg2 = M5.Speaker.config();
    cfg2.pin_mck = (gpio_num_t)-1;
    M5.Speaker.config(cfg2);
    i2s_driver_uninstall(I2S_PORT);
    delay(50);
    g_rate = 16000;
    if (duplex_i2s_init()) aw88298_init();
    Serial.println("=== [g] 完成 ===");
}

/**
 * 完整复刻 M5Unified Mic_Class 在 S3 上的 RX 路径配置:
 *   - rx_tdm_en = 1 (I2S 模式, 非 PDM)
 *   - rx_conf1.rx_bck_div_num = div_m - 1  (div_m=8, CoreS3 mic 精度要求)
 *   - RX 时钟重建 (PLL_240M + calcClockDiv)
 *   - rx_update 使配置生效
 * 缺少这些时 RX 时钟与 TX 不匹配 → 录音数据错位成噪音
 */
static void patch_rx_path(int fs)
{
    constexpr uint32_t PLL_D2_CLK = 120 * 1000 * 1000;
    const uint32_t bits = 16;
    const uint32_t div_m = 8;                    // CoreS3 mic 固定 8 (M5Unified 注释)
    uint32_t div_a, div_b, div_n;
    calcClockDiv(&div_a, &div_b, &div_n, PLL_D2_CLK, div_m * bits * (uint32_t)fs);

    i2s_dev_t* dev = (I2S_PORT == I2S_NUM_0) ? &I2S0 : &I2S1;

    // 1) RX 模式位: TDM (非 PDM) + bck 分频
    dev->rx_conf.rx_pdm_en = 0;
    dev->rx_conf.rx_tdm_en = 1;
    dev->rx_conf.rx_update = 1;
    dev->rx_conf1.rx_bck_div_num = div_m - 1;

    // 2) RX 时钟重建 (PLL_240M 分数分频)
    bool yn1 = (div_b > (div_a >> 1));
    if (yn1) { div_b = div_a - div_b; }
    int div_y = 1, div_x = 0;
    if (div_b) {
        div_x = div_a / div_b - 1;
        div_y = div_a % div_b;
        if (div_y == 0) { div_y = 1; div_b = 511; }
    }
    dev->rx_clkm_div_conf.rx_clkm_div_x = div_x;
    dev->rx_clkm_div_conf.rx_clkm_div_y = div_y;
    dev->rx_clkm_div_conf.rx_clkm_div_z = div_b;
    dev->rx_clkm_div_conf.rx_clkm_div_yn1 = yn1;
    dev->rx_clkm_conf.rx_clkm_div_num = div_n;
    dev->rx_clkm_conf.rx_clk_sel = 1;      // PLL_240M_CLK
    dev->tx_clkm_conf.clk_en = 1;          // (M5Unified 原文如此)
    dev->rx_clkm_conf.rx_clk_active = 1;
    dev->rx_conf.rx_update = 1;
    dev->rx_conf.rx_update = 0;

    Serial.printf("[rxpatch] fs=%d div_m=%u n=%u a=%u b=%u bck_div=%u\n",
                  fs, (unsigned)div_m, (unsigned)div_n, (unsigned)div_a,
                  (unsigned)div_b, (unsigned)dev->rx_conf1.rx_bck_div_num);
}

/** h: 全双工核心验证 — M5.Speaker 通道上边播边录 */
static void cmd_m5_fullduplex(void)
{
    Serial.println("=== [h] 全双工: 边回放边录音 ===");
    i2s_driver_uninstall(I2S_PORT);
    delay(100);

    auto spk_cfg = M5.Speaker.config();
    spk_cfg.pin_mck = (gpio_num_t)PIN_I2S_MCK;
    spk_cfg.sample_rate = 16000;  // ★★ 关键! spk_task 按此重建 TX 时钟 (默认 48000 会让播放快 3 倍)
    spk_cfg.dma_buf_len = 128;    // DMA: 4×128 帧 = 32ms 延迟 (AEC 48ms 窗覆盖)
    spk_cfg.dma_buf_count = 4;
    M5.Speaker.config(spk_cfg);

    if (!M5.Speaker.begin()) {
        Serial.println("[h] Speaker.begin FAILED");
        return;
    }
    M5.Speaker.setVolume(200);
    delay(100);
    patch_rx_path(16000);      // ★ 补齐 RX 路径 (TDM + 时钟重建)
    delay(50);

    const size_t CHUNK = 1600 * 2;       // 100ms mono 16k = 3200 字节
    const size_t TOTAL = 16000 * 3 * 2;  // 3 秒 = 96000 字节 (mono 16k, 2B/样本)
    const size_t TOTAL_B = 16000 * 4 * 2; // 4 秒 = 128000 字节
    const int BLOCKS_A = TOTAL / CHUNK;
    const int BLOCKS_B = TOTAL_B / CHUNK;

    // 1) 录 A (分块读取 — 避开大块 i2s_read 提前返回问题)
    Serial.println("[h] 第一步: 录 A (3秒, 说 \"测试 A\") ...");
    size_t got_a = 0;
    int short_a = 0;
    for (int b = 0; b < BLOCKS_A; b++) {
        size_t rr = 0;
        i2s_read(I2S_PORT, (uint8_t*)g_buf_a + got_a, CHUNK, &rr, pdMS_TO_TICKS(1500));
        got_a += rr;
        if (rr < CHUNK) short_a++;
    }
    {
        size_t frames = got_a / 2;
        double sum = 0; int16_t peak = 0;
        for (size_t i = 0; i < frames; i++) { sum += (double)g_buf_a[i]*g_buf_a[i]; if (abs(g_buf_a[i]) > peak) peak = abs(g_buf_a[i]); }
        Serial.printf("[h] recA 读 %u/%u 字节 (短读块=%d) RMS=%.0f Peak=%d\n",
                      (unsigned)got_a, (unsigned)TOTAL, short_a,
                      frames ? sqrtf((float)(sum/frames)) : 0.0f, peak);
    }

    // 2) 边回放 A 边录 B (双任务并行: 播放连续不断, 采集同步进行, AEC 处理)
    Serial.println("[h] 第二步: 回放 A 的同时录 B — 现在说一句新话!");
    g_aec.begin(nullptr);
    g_aec.reset();
    g_hd_play_done = false;
    g_hd_cap_done = false;
    g_hd_got_b = 0;
    xTaskCreatePinnedToCore(hdPlaybackTask, "hdPlay", 4096, NULL, 6, NULL, 1);
    xTaskCreatePinnedToCore(hdCaptureTask, "hdCap", 6144, NULL, 5, NULL, 0);
    while (!(g_hd_play_done && g_hd_cap_done)) delay(50);
    delay(300);
    {
        const AecStats& st = g_aec.stats();
        size_t got_b = g_hd_got_b;
        size_t frames = got_b / 2;
        double sum = 0; int16_t peak = 0;
        for (size_t i = 0; i < frames; i++) { sum += (double)g_buf_b[i]*g_buf_b[i]; if (abs(g_buf_b[i]) > peak) peak = abs(g_buf_b[i]); }
        Serial.printf("[h] recB(AEC后) 读 %u 字节 RMS=%.0f Peak=%d | AEC: 输入=%.0f 残余=%.0f 衰减=%.1fdB frames=%u\n",
                      (unsigned)got_b, frames ? sqrtf((float)(sum/frames)) : 0.0f, peak,
                      st.input_rms, st.residual_rms, st.echo_attenuation_db,
                      (unsigned)st.frames_processed);
    }

    // 3) 回放 B — 分块写入 (与播放任务同节奏)
    Serial.println("[h] 第三步: 回放 B");
    {
        size_t off = 0;
        size_t written = 0;
        size_t got_b = g_hd_got_b;
        uint32_t t0 = millis();
        while (off < got_b) {
            size_t take = (got_b - off < 3200) ? (got_b - off) : 3200;
            size_t w = 0;
            i2s_write(I2S_PORT, (uint8_t*)g_buf_b + off, take, &w, pdMS_TO_TICKS(1000));
            written += w;
            off += take;
        }
        uint32_t play_ms = millis() - t0;
        Serial.printf("[h] 回放 B: 写入 %u/%u 字节, 耗时 %ums (4秒数据, 应≈4000ms)\n",
                      (unsigned)written, (unsigned)got_b, (unsigned)play_ms);
        delay(play_ms + 400);
    }

    M5.Speaker.end();
    delay(80);
    auto cfg2 = M5.Speaker.config();
    cfg2.pin_mck = (gpio_num_t)-1;
    M5.Speaker.config(cfg2);
    i2s_driver_uninstall(I2S_PORT);
    delay(50);
    g_rate = 16000;
    if (duplex_i2s_init()) aw88298_init();
    Serial.println("=== [h] 完成 — recB 有新话 = 全双工成立! ===");
}

// ---- 全双工双任务 (函数定义) ----

/** 播放任务: 连续 i2s_write A (3秒), 参考信号同步注入 */
static void hdPlaybackTask(void* arg)
{
    const size_t TOTAL_A = 16000 * 3 * 2;
    size_t off = 0;
    size_t written = 0;
    uint32_t t0 = millis();
    while (off < TOTAL_A) {
        int16_t* p = (int16_t*)((uint8_t*)g_buf_a + off);
        g_aec.pushReference(p, 1600, 16000);        // 100ms 参考 (与 write 同步)
        size_t w = 0;
        i2s_write(I2S_PORT, p, 3200, &w, pdMS_TO_TICKS(1000));
        written += w;
        off += 3200;
    }
    Serial.printf("[h] 播放任务完成: 写入 %u/%u 字节, 耗时 %ums (3秒数据, 应≈3000ms)\n",
                  (unsigned)written, (unsigned)TOTAL_A, (unsigned)(millis() - t0));
    g_hd_play_done = true;
    vTaskDelete(NULL);
}

/** 采集任务: 连续 i2s_read (4秒) + AEC 逐帧处理 */
static void hdCaptureTask(void* arg)
{
    const size_t TOTAL_B = 16000 * 4 * 2;
    size_t got = 0;
    while (got < TOTAL_B) {
        size_t rr = 0;
        i2s_read(I2S_PORT, (uint8_t*)g_buf_b + got, 3200, &rr, pdMS_TO_TICKS(1500));
        int16_t* base = (int16_t*)((uint8_t*)g_buf_b + got);
        for (int i = 0; i + 320 <= (int)(rr / 2); i += 320) {
            bool hs = false;
            g_aec.processFrame(base + i, &hs);
            vTaskDelay(1);      // ★ 让出 CPU 给 IDLE (防 Task WDT)
        }
        got += rr;
    }
    g_hd_got_b = got;
    g_hd_cap_done = true;
    vTaskDelete(NULL);
}

static void cmd_inspect(void)
{
    Serial.println("=== [i] codec 寄存器状态 ===");
    Serial.printf("  AW9523  P0_out=0x%02X P1_out=0x%02X P0_dir=0x%02X P1_dir=0x%02X\n",
                  aw9523_read_reg(0x02), aw9523_read_reg(0x03),
                  aw9523_read_reg(0x04), aw9523_read_reg(0x05));
    Serial.printf("  AW88298 0x04=0x%04X  0x06=0x%04X  0x0C=0x%04X  0x61=0x%04X\n",
                  aw88298_read_reg(0x04), aw88298_read_reg(0x06),
                  aw88298_read_reg(0x0C), aw88298_read_reg(0x61));
    Serial.printf("  ES7210  0x00=0x%02X  0x01=0x%02X  0x08=0x%02X\n",
                  es7210_read_reg(0x00), es7210_read_reg(0x01), es7210_read_reg(0x08));
    Serial.printf("  帧格式 = %s\n", fmt_name());
    Serial.println("=== [i] 完成 ===");
}

static void cmd_help(void)
{
    Serial.println("===== CoreS3 Duplex Test =====");
    Serial.println(" h : 全双工验证 (录A→边放A边录B→放B) ★");
    Serial.println(" a : AEC 对照台架 NLMS vs esp_aec (安静 45s) ★");
    Serial.println(" b : 同上但仅测 NLMS (esp_aec 崩溃排查用)");
    Serial.println(" p/q/r : AEC 探针 mode=5/2/0 (逐个试, 崩溃就重启试下一个)");
    Serial.println(" g : 基础读写探测");
    Serial.println(" m : M5Unified tone 基准");
    Serial.println(" i : codec 寄存器回读");
    Serial.println(" ? : 帮助");
    Serial.println("==============================");
}

// =============================================================================
// [AECB] 自研 NLMS vs 官方 esp_aec 对照实验 (参考延迟扫描 0~256ms)
//
// 目的: 全双工扬声器回声残余是当前最大音频问题。用确定性"类语音"信号
//       循环回放 + 同步采集, 两套 AEC 并行处理同一批音频, 扫描参考延迟,
//       对比各自残余 RMS / 衰减(dB)。ESP 显著更优 → 采用方案一替换。
// 用法: 重新上电后串口输入 'a', 保持环境安静 ~45 秒
// =============================================================================
#define AECB_FRAME      256                 // esp_aec 帧长 (16ms @16k)
#define AECB_BATCH      1280                // 批长 = 4×320 (NLMS) = 5×256 (ESP)
#define AECB_HIST       16384               // 播放历史环 (1s @16k)
#define AECB_SIG_SECS   4
#define AECB_SIG_LEN    (SAMPLE_RATE * AECB_SIG_SECS)
#define AECB_DSTEP      256                 // 扫描步进 (16ms)
#define AECB_DMAX       4096                // 扫描上限 (256ms)
#define AECB_BATCHES    32                  // 每档批数 (2.56s)
#define AECB_WARMUP     12                  // 每档丢弃批数 (收敛期)

static int16_t*     g_aecb_sig = nullptr;
static int16_t      g_aecb_hist[AECB_HIST];
static uint32_t     g_aecb_tx = 0;
static uint32_t     g_aecb_rx = 0;
static aec_handle_t g_esp_aec = nullptr;
static int16_t      g_aecb_txbuf[AECB_BATCH];
static int16_t      g_aecb_mic[AECB_BATCH];
static int16_t      g_aecb_micc[AECB_BATCH];
static int16_t      g_aecb_ref[AECB_BATCH];
static int16_t      g_aecb_eout[AECB_BATCH];
static uint32_t     g_aecb_tx_ok = 0;           // 完整写入的批数
static uint32_t     g_aecb_tx_fail = 0;         // 写入不足的批数
static uint32_t     g_aecb_rx_ok = 0;           // 完整读取的批数
static uint32_t     g_aecb_rx_fail = 0;         // 短读批数

/** 生成类语音测试信号 (180Hz 基频 8 谐波 + 音节包络 + 停顿), 一次性生成到 PSRAM */
static void aecb_gen_signal(void)
{
    if (g_aecb_sig) return;
    g_aecb_sig = (int16_t*)heap_caps_malloc(AECB_SIG_LEN * 2, MALLOC_CAP_SPIRAM);
    if (!g_aecb_sig) { Serial.println("[AECB] 信号分配失败"); return; }
    const float PI2 = 6.2831853f;
    for (int n = 0; n < AECB_SIG_LEN; n++) {
        float t  = (float)n / (float)SAMPLE_RATE;
        float f0 = 180.0f + 25.0f * sinf(PI2 * 0.7f * t);
        float s  = 0.0f;
        for (int h = 1; h <= 8; h++) s += sinf(PI2 * f0 * (float)h * t) / (float)h;
        float ph  = fmodf(t, 0.6f);
        float env = (ph < 0.45f) ? (0.5f - 0.5f * cosf(PI2 * ph / 0.45f)) : 0.0f;
        g_aecb_sig[n] = (int16_t)(s * env * 4000.0f);
    }
    Serial.printf("[AECB] 类语音测试信号 %d 样本 (%.0fs)\n", AECB_SIG_LEN, (float)AECB_SIG_SECS);
    Serial.flush();
}

/** 写一批 (1280 帧) 到 TX, 同步记录播放历史 */
static void aecb_tx_batch(void)
{
    for (int i = 0; i < AECB_BATCH; i++) {
        int16_t v = g_aecb_sig[(g_aecb_tx + i) % AECB_SIG_LEN];
        g_aecb_txbuf[i] = v;
        g_aecb_hist[(g_aecb_tx + i) % AECB_HIST] = v;
    }
    size_t sent = 0;
    while (sent < AECB_BATCH) {
        size_t w = 0;
        i2s_write(I2S_PORT, g_aecb_txbuf + sent, (AECB_BATCH - sent) * 2, &w, pdMS_TO_TICKS(1000));
        if (w == 0) break;
        sent += w / 2;
    }
    g_aecb_tx += sent;
    if (sent >= AECB_BATCH) g_aecb_tx_ok++; else g_aecb_tx_fail++;
}

/** 读一批 (1280 帧), 循环补读应对短读 */
static size_t aecb_rx_batch(void)
{
    size_t got = 0;
    uint32_t t0 = millis();
    while (got < AECB_BATCH) {
        size_t rr = 0;
        i2s_read(I2S_PORT, g_aecb_mic + got, (AECB_BATCH - got) * 2, &rr, pdMS_TO_TICKS(500));
        got += rr / 2;
        if (rr == 0 && millis() - t0 > 2000) break;
    }
    if (got >= AECB_BATCH) g_aecb_rx_ok++; else g_aecb_rx_fail++;
    return got;
}

/** [AECB] 主实验: 延迟扫描 + 双 AEC 对比 (需环境安静); use_esp=false 时仅测 NLMS */
static void cmd_aec_bench(bool use_esp)
{
    Serial.println("\n=== [AECB] NLMS vs esp_aec 对照 — 参考延迟扫描 0~256ms ===");
    Serial.println("[AECB] 请保持安静约 45 秒 (不要说话)");
    if (g_rate != 16000 || g_ch_fmt != I2S_CHANNEL_FMT_ONLY_RIGHT) {
        Serial.println("[AECB] 警告: I2S 非 16k mono, 结果可能异常; 建议重新上电后直接运行");
    }
    M5.Lcd.fillScreen(TFT_BLACK);
    M5.Lcd.setCursor(6, 6);
    M5.Lcd.println("AEC bench");
    M5.Lcd.println("keep quiet!");

    // ★ 采用主固件验证过的播放路径 (裸 duplex_i2s_init + 手动补丁在此硬件上始终无声):
    //   M5.Speaker.begin 安装 (patched: TX+RX 同端口) → patch_rx_path → 直接 i2s_write
    M5.Speaker.end();
    i2s_driver_uninstall(I2S_PORT);
    delay(80);
    {
        auto spk_cfg = M5.Speaker.config();
        spk_cfg.pin_mck = (gpio_num_t)0;      // ES7210 MCLK
        spk_cfg.sample_rate = 16000;
        M5.Speaker.config(spk_cfg);
        if (!M5.Speaker.begin()) { Serial.println("[AECB] Speaker.begin 失败"); return; }
        delay(50);
        patch_rx_path(16000);                  // RX 时钟重建 (对齐主固件 fd_patch_rx_path)
        aw88298_init();                         // 功放重配 (确保 16k 档位 + 音量)
        Serial.println("[AECB] I2S 经 M5.Speaker 安装 (TX+RX), 直接 i2s_write 播放");
        delay(50);
    }

    aecb_gen_signal();
    if (!g_aecb_sig) return;

    if (use_esp && !g_esp_aec) {
        Serial.println("[AECB] stage 1: aec_create...");
        Serial.flush();
        delay(50);
        g_esp_aec = aec_create(16000, AECB_FRAME, 1200);
        Serial.printf("[AECB] stage 2: aec_create ret=%p\n", (void*)g_esp_aec);
        Serial.flush();
        if (!g_esp_aec) { Serial.println("[AECB] esp_aec aec_create 失败 (内存?)"); return; }
    }

    Serial.println("[AECB] stage 3: nlms begin...");
    Serial.flush();
    g_aec.begin(nullptr);
    Serial.println("[AECB] stage 4: nlms ready");
    g_aecb_tx = 0;
    g_aecb_rx = 0;
    g_aecb_tx_ok = g_aecb_tx_fail = 0;
    g_aecb_rx_ok = g_aecb_rx_fail = 0;
    memset(g_aecb_hist, 0, sizeof(g_aecb_hist));
    Serial.println("[AECB] stage 5: prefill TX DMA...");
    Serial.flush();
    for (int i = 0; i < 3; i++) aecb_tx_batch();
    Serial.println("[AECB] stage 6: scan start");
    Serial.flush();

    float    best_n = 1e9f, best_e = 1e9f;
    uint32_t best_nd = 0, best_ed = 0;

    for (uint32_t delay = 0; delay <= AECB_DMAX; delay += AECB_DSTEP) {
        double s_in = 0, s_n = 0, s_e = 0;
        int used = 0;
        for (int b = 0; b < AECB_BATCHES; b++) {
            if ((b & 7) == 0) {
                Serial.printf("[AECB] scan delay=%ums batch=%d/%d\n",
                              (unsigned)(delay * 1000 / SAMPLE_RATE), b, AECB_BATCHES);
                Serial.flush();
            }
            aecb_tx_batch();
            if (aecb_rx_batch() < AECB_BATCH) { Serial.println("[AECB] 采集短读!"); continue; }
            uint32_t rx_base = g_aecb_rx;
            g_aecb_rx += AECB_BATCH;

            memcpy(g_aecb_micc, g_aecb_mic, sizeof(g_aecb_mic));
            for (int i = 0; i < AECB_BATCH; i++) {
                g_aecb_ref[i] = g_aecb_hist[(rx_base + AECB_HIST + i - delay) % AECB_HIST];
            }
            if (b >= AECB_WARMUP) {
                for (int i = 0; i < AECB_BATCH; i++) s_in += (double)g_aecb_micc[i] * g_aecb_micc[i];
            }
            // esp_aec: 每 256 帧
            if (use_esp) {
                for (int off = 0; off < AECB_BATCH; off += AECB_FRAME) {
                    aec_process(g_esp_aec, g_aecb_micc + off, g_aecb_ref + off, g_aecb_eout + off);
                }
            }
            // 自研 NLMS: 每 320 帧
            for (int off = 0; off < AECB_BATCH; off += 320) {
                g_aec.pushReference(g_aecb_ref + off, 320, 16000);
                g_aec.processFrame(g_aecb_mic + off, nullptr);
            }
            if (b >= AECB_WARMUP) {
                for (int i = 0; i < AECB_BATCH; i++) {
                    s_e += (double)g_aecb_eout[i] * g_aecb_eout[i];
                    s_n += (double)g_aecb_mic[i] * g_aecb_mic[i];
                }
                used++;
            }
            vTaskDelay(1);          // 让出 CPU 给 IDLE (防 task WDT)
        }
        if (used == 0) continue;
        double n = (double)used * AECB_BATCH;
        float in_rms = sqrtf((float)(s_in / n));
        float n_rms  = sqrtf((float)(s_n / n));
        float e_rms  = sqrtf((float)(s_e / n));
        float n_db = 20.0f * log10f((n_rms + 1e-3f) / (in_rms + 1e-3f));
        float e_db = 20.0f * log10f((e_rms + 1e-3f) / (in_rms + 1e-3f));
        if (use_esp) {
            Serial.printf("[AECB] delay=%3ums in=%6.0f | NLMS=%6.0f(%+5.1fdB) | ESP=%6.0f(%+5.1fdB) | tx=%u/%u rx=%u/%u\n",
                          (unsigned)(delay * 1000 / SAMPLE_RATE), in_rms, n_rms, n_db, e_rms, e_db,
                          (unsigned)g_aecb_tx_ok, (unsigned)(g_aecb_tx_ok + g_aecb_tx_fail),
                          (unsigned)g_aecb_rx_ok, (unsigned)(g_aecb_rx_ok + g_aecb_rx_fail));
        } else {
            Serial.printf("[AECB] delay=%3ums in=%6.0f | NLMS=%6.0f(%+5.1fdB) | tx=%u/%u rx=%u/%u\n",
                          (unsigned)(delay * 1000 / SAMPLE_RATE), in_rms, n_rms, n_db,
                          (unsigned)g_aecb_tx_ok, (unsigned)(g_aecb_tx_ok + g_aecb_tx_fail),
                          (unsigned)g_aecb_rx_ok, (unsigned)(g_aecb_rx_ok + g_aecb_rx_fail));
        }
        if (in_rms > 100.0f && n_db < best_n) { best_n = n_db; best_nd = delay; }
        if (in_rms > 100.0f && e_db < best_e) { best_e = e_db; best_ed = delay; }
    }
    Serial.printf("[AECB] BEST  NLMS: delay=%ums  %.1fdB\n", (unsigned)(best_nd * 1000 / SAMPLE_RATE), best_n);
    if (best_n > 1e8f) Serial.println("[AECB] (无有效数据: 回声信号太弱, 检查扬声器/音量)");
    if (use_esp) {
        Serial.printf("[AECB] BEST  ESP : delay=%ums  %.1fdB\n", (unsigned)(best_ed * 1000 / SAMPLE_RATE), best_e);
    }
    Serial.println("[AECB] 完成 — 请把以上 [AECB] 行发回");
    M5.Lcd.println("done, see log");
}

/** [PROBE] AEC 初始化探针: 扫描 aec_pro_create 各 mode 与 aec_create 不同 filter */
static void cmd_aec_probe(void)
{
    Serial.println("\n=== [PROBE] AEC 初始化探针 ===");
    Serial.printf("[PROBE] free int=%u psram=%u dma=%u\n",
        (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
        (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
        (unsigned)heap_caps_get_free_size(MALLOC_CAP_DMA));
    Serial.flush(); delay(200);

    const int modes[] = {1, 2, 3, 4, 5, 0};
    for (int i = 0; i < 6; i++) {
        Serial.printf("[PROBE] aec_pro_create(256,1,%d)...\n", modes[i]);
        Serial.flush(); delay(100);
        aec_handle_t h = aec_pro_create(256, 1, modes[i]);
        Serial.printf("[PROBE] mode=%d ret=%p\n", modes[i], (void*)h);
        Serial.flush();
        if (h) aec_destroy(h);
        delay(200);
    }
    const int fls[] = {200, 600, 1200};
    for (int i = 0; i < 3; i++) {
        Serial.printf("[PROBE] aec_create(16000,256,%d)...\n", fls[i]);
        Serial.flush(); delay(100);
        aec_handle_t h = aec_create(16000, 256, fls[i]);
        Serial.printf("[PROBE] fl=%d ret=%p\n", fls[i], (void*)h);
        Serial.flush();
        if (h) aec_destroy(h);
        delay(200);
    }
    Serial.println("[PROBE] 探针完成");
}

/** [PROBE1] 单个 pro mode 探针 (崩溃则重启即可, 每次只试一个) */
static void cmd_aec_probe1(int mode)
{
    Serial.printf("\n=== [PROBE1] aec_pro_create(256,1,%d) ===\n", mode);
    Serial.printf("[PROBE1] free int=%u psram=%u\n",
        (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
        (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
    Serial.println("[PROBE1] creating...");
    Serial.flush(); delay(200);
    aec_handle_t h = aec_pro_create(256, 1, mode);
    Serial.printf("[PROBE1] ret=%p\n", (void*)h);
    Serial.flush();
    if (h) {
        static int16_t m[256], r[256], o[256];
        memset(m, 0, sizeof(m)); memset(r, 0, sizeof(r)); memset(o, 0, sizeof(o));
        Serial.println("[PROBE1] aec_process test...");
        Serial.flush();
        aec_process(h, m, r, o);
        Serial.println("[PROBE1] aec_process OK");
        Serial.flush();
        aec_destroy(h);
        Serial.println("[PROBE1] destroy OK — 此 mode 可用!");
    }
    Serial.println("[PROBE1] 完成");
}

// =============================================================================
// setup / loop
// =============================================================================

void setup()
{
    auto cfg = M5.config();
    M5.begin(cfg);
    delay(200);
    Serial.begin(115200);
    delay(300);
    Serial.println("\n\n===== CoreS3 I2S Duplex Test v3 =====");

    g_buf_a = (int16_t*)heap_caps_malloc(BUF_BYTES, MALLOC_CAP_SPIRAM);
    g_buf_b = (int16_t*)heap_caps_malloc(BUF_BYTES, MALLOC_CAP_SPIRAM);
    if (!g_buf_a || !g_buf_b) {
        Serial.println("[init] PSRAM buffer alloc FAILED");
        M5.Lcd.println("PSRAM alloc failed");
        while (1) delay(1000);
    }

    es7210_init();
    if (!duplex_i2s_init()) {
        M5.Lcd.println("I2S duplex init failed");
        while (1) delay(1000);
    }
    aw88298_init();      // ★ I2S 之后再配功放 (时钟活跃时锁存采样率, 对齐 'h' 验证过的顺序)

    M5.Lcd.setTextSize(2);
    M5.Lcd.println("Duplex v3");
    M5.Lcd.println("r/d/t/s/m/i");
    cmd_help();
    cmd_inspect();
}

void loop()
{
    M5.update();
    if (Serial.available()) {
        int c = Serial.read();
        switch (c) {
            case 'h': case 'H': cmd_m5_fullduplex(); break;   // ★ 全双工核心验证
            case 'g': case 'G': cmd_m5_duplex_probe(); break;  // 基础读写探测
            case 'a': case 'A': cmd_aec_bench(true); break;   // ★ AEC 对照 (NLMS+esp)
            case 'b': case 'B': cmd_aec_bench(false); break;  // 仅 NLMS (esp 崩溃排查)
            case 'p': case 'P': cmd_aec_probe1(5); break;     // 探针 mode 5 (S3 加速)
            case 'q': case 'Q': cmd_aec_probe1(2); break;     // 探针 mode 2 (SPIRAM)
            case 'r': case 'R': cmd_aec_probe1(0); break;     // 探针 mode 0 (mild)
            case 'm': case 'M': cmd_m5_compare(); break;       // M5Unified 基准
            case 'i': case 'I': cmd_inspect(); break;          // 寄存器回读
            case '?': cmd_help(); break;
            default: break;
        }
    }
    delay(50);
}
