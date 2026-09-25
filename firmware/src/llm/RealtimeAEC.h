/**
 * @file RealtimeAEC.h
 * @brief 轻量软件声学回声消除 + VAD 模块 (ESP32-S3 适配)
 *
 * 背景: ES8311 是音频 Codec (ADC/DAC/ALC), 没有硬件 AEC 引擎。
 * 回声消除必须在 ESP32 软件实现。本模块基于 NLMS 自适应滤波
 * (Normalized Least Mean Square), 以"即将播放的 PCM"作为参考信号,
 * 从麦克风采集信号中减去扬声器回声的估计。
 *
 * 处理链路 (20ms 帧, 320 samples @16kHz):
 *   麦克风帧 d[n] ──→ [NLMS 自适应滤波, 参考 = 播放历史 x[n]] ──→ 误差 e[n]
 *   e[n] ──→ [噪声门限降噪] ──→ [VAD 能量+过零率] ──→ 输出干净人声
 *
 * 性能预算 (ESP32-S3 @240MHz):
 *   NLMS: 320 taps × 512 阶 = 163,840 MAC/帧 ≈ 0.7ms (esp-dsp SIMD 加速后 <0.4ms)
 *   总处理延迟 < 2ms (远低于 10ms 要求), 不拖慢 20ms 上传节奏
 *   RAM: 滤波器 512×float32(2KB) + 参考环 512×int16(1KB) + 帧缓冲 ≈ 8KB
 */
#ifndef _REALTIME_AEC_H
#define _REALTIME_AEC_H

#include <Arduino.h>
#include <stdint.h>

// ---- 帧参数 (与 20ms 分片对齐) ----
#define AEC_FRAME_SAMPLES    (320)     // 20ms @ 16kHz
#define AEC_FILTER_TAPS      (512)     // 32ms 回声尾 (近场小扬声器足够)
#define AEC_HISTORY_SAMPLES  (AEC_FILTER_TAPS)

/**
 * AEC 可调参数 (SD 配置 / 宏默认值)
 */
typedef struct AecConfig {
    float   mu;             // NLMS 步长 (0.01~0.5, 默认 0.1; 越大收敛快但噪声大)
    float   echo_gain;      // 回声补偿增益 (0.5~1.5, 默认 1.0)
    float   noise_gate;     // 降噪门限 (RMS, 默认 60; 低于此值视为噪声置零)
    float   vad_threshold;  // VAD 触发能量 (RMS, 默认 200; 对齐服务端阈值)
    int32_t vad_confirm_ms; // VAD 确认时长 (默认 200ms, 防短暂噪音误触发)
    bool    enable;         // AEC 总开关
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
     * @brief 初始化滤波器与缓冲区
     * @param cfg 参数 (nullptr 使用默认值)
     * @return true 成功
     */
    bool begin(const AecConfig* cfg = nullptr);

    /**
     * @brief 推送一段即将播放的 PCM 到参考信号历史环
     *
     * 必须在 Speaker.playRaw() 之前调用, 保证参考信号与
     * 麦克风实际听到的回声对齐。
     *
     * @param pcm   int16 PCM (24kHz 播放数据; 内部重采样到 16kHz 或直接按 16kHz 处理)
     * @param samples 采样数
     * @param sample_rate 采样率 (24000 播放用)
     */
    void pushReference(const int16_t* pcm, size_t samples, int sample_rate);

    /**
     * @brief 处理一帧麦克风采集数据 (20ms 帧, 阻塞 <2ms)
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

    /** @brief 复位自适应状态 (会话切换/重连时调用) */
    void reset();

    /** @brief 获取统计指标 */
    const AecStats& stats() const { return _stats; }

    /** @brief 当前滤波器是否收敛 */
    bool isConverged() const { return _converged; }

private:
    /** @brief VAD 端点检测状态机 (200ms 确认防误触发) */
    void _updateVad(float rms, bool* has_speech);
    // NLMS 滤波器系数 (float 定点运算, 用 int16 存储系数量化)
    float*   _w;                       // 滤波器权重 (AEC_FILTER_TAPS)
    int16_t* _refHist;                 // 参考信号历史环 (AEC_HISTORY_SAMPLES)
    uint32_t _refHistPos;

    // 状态
    AecConfig _cfg;
    AecStats  _stats;
    bool      _speechActive;
    bool      _converged;
    uint32_t  _speechStartTick;        // 人声起始 tick (VAD 确认)
    uint32_t  _frameCount;

    // 内部: 重采样辅助 (24kHz 播放参考 → 16kHz 处理)
    float    _resampleAccum;
    int16_t  _lastResampleSample;
};

#endif // _REALTIME_AEC_H
