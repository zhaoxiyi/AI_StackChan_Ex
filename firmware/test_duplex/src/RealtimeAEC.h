/**
 * @file RealtimeAEC.h
 * @brief 轻量软件声学回声消除 + VAD 模块 (ESP32-S3 适配, test_duplex 测量版)
 *
 * 与主固件 RealtimeAEC 同源; 差异:
 *   - AEC_FILTER_TAPS = 768 (48ms 回声尾, 配合 bench 延迟扫描)
 *   - noise_gate 默认 60 (低于门限置零, 便于量化残余)
 *
 * 播放期对齐 (auto_align, 与主固件一致):
 *   回波路径延迟 (i2s_write → DMA → 扬声器 → 麦克风 → i2s_read)
 *   未知且随硬件/驱动变化, 硬编码补偿一旦不符 → NLMS 无法收敛。
 *   高能量候选帧对参考环粗搜互相关 (bin=1ms) 累加, 每 ~500ms
 *   找峰并精搜 ±1ms, 更新滤波起点 _k0。bench 延迟扫描时关闭
 *   auto_align, 保留人工扫描语义。
 */
#ifndef _REALTIME_AEC_H
#define _REALTIME_AEC_H

#include <Arduino.h>
#include <stdint.h>

// ---- 帧参数 (与 20ms 分片对齐) ----
#define AEC_FRAME_SAMPLES    (320)     // 20ms @ 16kHz
#define AEC_FILTER_TAPS      (768)     // 48ms 回声尾 — 配合 bench 延迟扫描
#define AEC_HISTORY_SAMPLES  (4096)    // 256ms 参考历史环 (延迟对齐搜索空间 + 回声尾)

// ---- 自动延迟对齐参数 ----
#define AEC_ALIGN_STEP       (16)      // 粗搜步长 (1ms @16kHz)
#define AEC_ALIGN_BINS       ((AEC_HISTORY_SAMPLES - AEC_FILTER_TAPS) / AEC_ALIGN_STEP)
#define AEC_ALIGN_EVERY      (25)      // 每 25 帧 (~500ms) 评估一次延迟

/**
 * AEC 可调参数
 */
typedef struct AecConfig {
    float   mu;                 // NLMS 步长
    float   echo_gain;          // 回声补偿增益
    float   noise_gate;         // 降噪门限 (RMS, 默认 60; 低于此值视为噪声置零)
    float   vad_threshold;      // VAD 触发能量 (RMS)
    int32_t vad_confirm_ms;     // VAD 确认时长
    bool    enable;             // AEC 总开关
    bool    auto_align;         // 自动延迟对齐 (互相关估计回波延迟, 默认 true)
    int32_t ref_delay_samples;  // auto_align=false: 固定延迟补偿; auto_align=true: 初始估计起点
} AecConfig;

/**
 * AEC 统计指标 (调试用)
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

    bool begin(const AecConfig* cfg = nullptr);
    void pushReference(const int16_t* pcm, size_t samples, int sample_rate);
    float processFrame(int16_t* frame, bool* has_speech);
    bool isSpeechActive() const { return _speechActive; }
    void reset();
    const AecStats& stats() const { return _stats; }
    bool isConverged() const { return _converged; }
    uint32_t refDelay() const { return _k0; }
    bool isAligned() const { return _aligned; }

private:
    void _updateVad(float rms, bool* has_speech);
    void _autoAlign(const int16_t* frame, float input_rms, float residual_rms);
    void _updateDelay();

    float*   _w;                       // 滤波器权重 (AEC_FILTER_TAPS)
    int16_t* _refHist;                 // 参考信号历史环 (AEC_HISTORY_SAMPLES)
    int16_t* _alignLastFrame;          // 最近候选帧 (精搜互相关用)
    float*   _alignAccum;              // 粗搜互相关累加 (AEC_ALIGN_BINS)
    uint32_t _refHistPos;

    AecConfig _cfg;
    AecStats  _stats;
    uint32_t  _k0;                     // 当前回波延迟补偿 (参考环起点偏移, 样本)
    uint32_t  _alignCount;
    uint32_t  _alignUpdates;
    bool      _aligned;
    bool      _speechActive;
    bool      _converged;
    uint32_t  _speechStartTick;
    uint32_t  _frameCount;

    // 内部: 重采样辅助 (24kHz 播放参考 → 16kHz 处理)
    float    _resampleAccum;
    int16_t  _lastResampleSample;
};

#endif // _REALTIME_AEC_H
