/**
 * @file RealtimeAEC.h
 * @brief 软件声学回声消除 + VAD 模块 (ESP32-S3 适配)
 *
 * 后端 (AecConfig.backend):
 *   - AEC_BACKEND_NLMS (默认): 自研时域 NLMS + auto_align 参考对齐
 *   - AEC_BACKEND_ESP_SR: 官方 esp-sr AEC — 2026-10 实机评估结论:
 *       该库与 16ms/256 样本流式调用不兼容 (esp_aec3 内核按 4096 样本/帧
 *       内部维度工作, 处理时向调用者 out 缓冲写入 16 倍数据量, 越界可达
 *       调用者任务栈; 另有创建期分配失败不检查/NULL 写等缺陷)。
 *       默认停用; 编译宏 AEC_FORCE_ESP_SR 可启用 (实验性, 会崩)。
 *   两后端均使用 auto_align 互相关延迟对齐。
 *   esp_aec 创建前做内存门禁 + SPIRAM 变体 (mode2/4) 级联 (仅实验路径)。
 *
 * 参考对齐 (auto_align, 两后端共用):
 *   i2s_write → DMA 出声 → 声学传播 → 麦克风 → i2s_read 的整条回波
 *   路径延迟未知且随硬件/驱动变化, 硬编码补偿一旦与实测不符,
 *   参考与麦克风帧失去相关性 → AEC 失效 (esp_aec 同样要求 ref 与
 *   mic 严格对齐)。本模块以"输入能量高的帧"为候选, 对参考历史环
 *   做粗搜互相关累加 (bin=1ms), 每 ~500ms 找峰值并精搜 ±1ms,
 *   峰均比达标时更新滤波起点 _k0 (回波延迟补偿, 误差 ±1 样本)。
 *   多帧累加抑制近端人声干扰, 硬件/驱动变化无需重新校准。
 *
 * 处理链路 (16ms 帧, 256 samples @16kHz):
 *   麦克风帧 d[n] ──→ [参考对齐取帧] ──→ [AEC 后端] ──→ 误差 e[n]
 *   e[n] ──→ [噪声门限降噪] ──→ [VAD 能量] ──→ 输出干净人声
 *
 * 性能预算 (ESP32-S3 @240MHz):
 *   esp_aec: 16ms 帧频域处理 (官方库, 实测 <1ms/帧)
 *   对齐粗搜: 候选帧 240 bins × 128 (2x 下采样) ≈ 31K MAC/帧 (仅高能量帧)
 *   RAM: 参考环 4096×int16(8KB) + 对齐累加 240×float32(1KB)
 *        + esp_aec 内部 (filter 1200, 官方库分配) + 帧缓冲 ≈ 2KB
 */
#ifndef _REALTIME_AEC_H
#define _REALTIME_AEC_H

#include <Arduino.h>
#include <stdint.h>
#include <esp_aec.h>          // esp-sr 官方 AEC (framework SDK 自带)

// ---- AEC 后端选择 ----
#define AEC_BACKEND_NLMS     (0)     // 自研时域 NLMS (回退/对照)
#define AEC_BACKEND_ESP_SR   (1)     // esp-sr 官方 esp_aec (默认)

// ---- 帧参数 (与 esp_aec 16ms 要求对齐) ----
#define AEC_FRAME_SAMPLES    (256)     // 16ms @ 16kHz (esp_aec 强制帧长; 原 320/20ms 不再适用)
#define AEC_ESP_FILTER       (1200)    // esp_aec 回声尾 (aec_create filter 参数, 75ms)
#define AEC_FILTER_TAPS      (256)     // NLMS 后端回声尾 (16ms; 桌面近场直达+早期反射足够;
                                       // 512 taps 三环计算会偏重, 饿死同核 webSocketLoopTask)
#define AEC_HISTORY_SAMPLES  (4096)    // 256ms 参考历史环 (延迟对齐搜索空间 + 回声尾)

// ---- 自动延迟对齐参数 ----
#define AEC_ALIGN_STEP       (16)      // 粗搜步长 (1ms @16kHz)
#define AEC_ALIGN_BINS       ((AEC_HISTORY_SAMPLES - AEC_ESP_FILTER) / AEC_ALIGN_STEP)
#define AEC_ALIGN_EVERY      (25)      // 每 25 帧 (~400ms) 评估一次延迟

// ---- esp-sr 创建门禁 ----
// 采用 SPIRAM 变体 (mode2/4): 库内部分配走 PSRAM; 内部 RAM 仅需
// 小结构 + 本模块缓冲。门禁不过则明确降级 NLMS (库创建失败
// 路径在实机上有崩溃史: 分配失败返回 NULL 但库不检查)
#define AEC_ESP_MIN_LARGEST_KB  (32)     // 内部 RAM 最大连续块下限
#define AEC_ESP_MIN_PSRAM_KB    (512)    // PSRAM 可用量下限

/**
 * AEC 可调参数 (SD 配置 / 宏默认值)
 */
typedef struct AecConfig {
    float   mu;                 // NLMS 步长 (仅 NLMS 后端)
    float   echo_gain;          // 回声补偿增益 (仅 NLMS 后端)
    float   noise_gate;         // 降噪门限 (RMS, 默认 90; 低于此值软衰减 /4, 不清零)
    float   vad_threshold;      // VAD 触发能量 (RMS, 默认 200; 对齐服务端阈值)
    int32_t vad_confirm_ms;     // VAD 确认时长 (默认 200ms, 防短暂噪音误触发)
    bool    enable;             // AEC 总开关
    int32_t backend;            // AEC_BACKEND_ESP_SR (默认) | AEC_BACKEND_NLMS
    bool    auto_align;         // 自动延迟对齐 (互相关估计回波延迟, 默认 true)
    int32_t ref_delay_samples;  // auto_align=false: 固定延迟补偿; auto_align=true: 初始估计起点
} AecConfig;

/**
 * AEC 统计指标 (调试用, 可通过串口/状态上报输出)
 */
typedef struct AecStats {
    float   echo_attenuation_db;  // 回声衰减估计 (dB)
    float   residual_rms;         // 处理后的残余信号 RMS
    float   input_rms;            // 原始麦克风 RMS
    uint32_t frames_processed;    // 累计处理帧数
} AecStats;

class RealtimeAEC {
public:
    RealtimeAEC();
    ~RealtimeAEC();

    /**
     * @brief 初始化后端与缓冲区
     * @param cfg 参数 (nullptr 使用默认值: esp-sr 后端 + auto_align)
     * @return true 成功
     */
    bool begin(const AecConfig* cfg = nullptr);

    /**
     * @brief 推送一段即将播放的 PCM 到参考信号历史环
     *
     * 播放侧在写 I2S (或调用 playRaw) 时同步调用, 参考环中
     * "最新样本 = 刚提交播放的 PCM"。实际出声时刻与参考环位置
     * 的偏移 (回波延迟) 由 auto_align 互相关估计, 无需手动补偿。
     *
     * @param pcm   int16 PCM (24kHz 播放数据; 内部重采样到 16kHz 或直接按 16kHz 处理)
     * @param samples 采样数
     * @param sample_rate 采样率 (24000 播放用)
     */
    void pushReference(const int16_t* pcm, size_t samples, int sample_rate);

    /**
     * @brief 处理一帧麦克风采集数据 (16ms/256 帧, 阻塞 <3ms)
     *
     * 输出为回声抵消 + 降噪后的干净信号 (原地处理)。
     *
     * @param frame 输入/输出帧 (int16, AEC_FRAME_SAMPLES)
     * @param has_speech 输出: VAD 判定 (持续确认后的结果)
     * @return 输出帧的 RMS
     */
    float processFrame(int16_t* frame, bool* has_speech);

    /**
     * @brief 语音端点检测 (供打断逻辑使用)
     * @return true = 当前有持续人声 (≥ vad_confirm_ms)
     */
    bool isSpeechActive() const { return _speechActive; }

    /** @brief 复位自适应状态 (会话切换/重连时调用; 回波延迟估计保留) */
    void reset();

    /** @brief 获取统计指标 */
    const AecStats& stats() const { return _stats; }

    /** @brief 当前滤波器是否收敛 */
    bool isConverged() const { return _converged; }

    /** @brief 当前回波延迟补偿 (样本 @16kHz, auto_align 估计或手动设定) */
    uint32_t refDelay() const { return _k0; }

    /** @brief 回波延迟估计是否已建立 (auto_align 模式) */
    bool isAligned() const { return _aligned; }

    /** @brief 当前使用的后端 */
    int32_t backend() const { return _cfg.backend; }

    /** @brief 对齐参考帧缓冲地址 (诊断用) */
    const void* outBufAddr() const { return (const void*)_espOut; }

private:
    /** @brief VAD 端点检测状态机 (200ms 确认防误触发) */
    void _updateVad(float rms, bool* has_speech);
    /** @brief 自动延迟对齐: 候选帧互相关累加 */
    void _autoAlign(const int16_t* frame, float input_rms);
    /** @brief 延迟评估: 粗搜峰值 + 精搜 + 更新 k0 */
    void _updateDelay();
    /** @brief 重置自适应状态 (延迟变更时; esp 实验路径重建实例) */
    void _resetAdaptation();
    /** @brief 从参考环按 k0 取当前帧的对齐参考 (供 AEC 后端使用) */
    void _fetchAlignedRef(int16_t* out);
    /** @brief esp-sr 后端帧处理 */
    void _processEsp(int16_t* frame);
    /** @brief NLMS 后端帧处理 (回退/对照) */
    void _processNlms(int16_t* frame, float input_rms);

    // NLMS 后端状态 (backend=NLMS 时使用)
    float*   _w;                       // 滤波器权重 (AEC_FILTER_TAPS)
    // esp-sr 后端状态
    aec_handle_t _espAec;              // esp_aec 实例 (aec_create)
    int16_t* _espRef;                  // 对齐参考帧缓冲 (AEC_FRAME_SAMPLES)
    int16_t* _espOut;                  // esp_aec 输出缓冲 (AEC_FRAME_SAMPLES)

    // 共用状态
    int16_t* _refHist;                 // 参考信号历史环 (AEC_HISTORY_SAMPLES)
    int16_t* _alignLastFrame;          // 最近候选帧 (精搜互相关用)
    float*   _alignAccum;              // 粗搜互相关累加 (AEC_ALIGN_BINS)
    uint32_t _refHistPos;

    AecConfig _cfg;
    AecStats  _stats;
    uint32_t  _k0;                     // 当前回波延迟补偿 (参考环起点偏移, 样本)
    uint32_t  _alignCount;             // 对齐累加帧计数
    uint32_t  _alignUpdates;           // 对齐更新次数 (限频日志)
    int32_t   _alignPendingK0;         // 待确认的显著变化估计 (-1 无)
    uint32_t  _lastRefPushMs;          // 最近一次参考推送时刻 (播放活跃判定)
    bool      _aligned;                // 延迟估计已建立
    bool      _speechActive;
    bool      _converged;
    uint32_t  _speechStartTick;        // 人声起始 tick (VAD 确认)
    uint32_t  _frameCount;

    // 内部: 重采样辅助 (24kHz 播放参考 → 16kHz 处理)
    float    _resampleAccum;
    int16_t  _lastResampleSample;
};

#endif // _REALTIME_AEC_H
