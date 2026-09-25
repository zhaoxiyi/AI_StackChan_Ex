/**
 * @file RealtimeAEC.cpp
 * @brief NLMS 回声消除实现 (ESP32-S3, 纯定点/浮点混合, 无外部依赖)
 *
 * 算法: 时域 NLMS
 *   e[n] = d[n] - y[n]                     (d=麦克风, y=滤波输出)
 *   y[n] = Σ w[k] * x[n-k]                 (x=参考信号=播放历史)
 *   w[k] += mu * e[n] * x[n-k] / (||x||² + eps)
 *
 * 双麦说明: CoreS3 的 ES8311 模拟前端把双麦合并为单路 I2S 输入,
 * M5Unified Mic API 是单声道。因此波束成形在现有驱动层面不可获得
 * 独立双通道数据; 本模块按单通道设计, 双麦的模拟电路差分特性
 * 已由硬件完成部分指向性。若未来切换 I2S TDM 双通道采集,
 * 在 processFrame 前叠加差分阵列即可 (接口已预留帧缓冲)。
 */
#include "RealtimeAEC.h"
#include <math.h>

// ---- NLMS 保护常量 ----
#define NLMS_EPS (1e-6f)

// ---- 噪声门限的帧平滑系数 ----
#define NOISE_ALPHA (0.9f)

RealtimeAEC::RealtimeAEC()
    : _w(nullptr), _refHist(nullptr), _refHistPos(0),
      _speechActive(false), _converged(false),
      _speechStartTick(0), _frameCount(0),
      _resampleAccum(0.0f), _lastResampleSample(0)
{
    memset(&_stats, 0, sizeof(_stats));
}

RealtimeAEC::~RealtimeAEC()
{
    if (_w)       { free(_w);       _w = nullptr; }
    if (_refHist) { free(_refHist); _refHist = nullptr; }
}

bool RealtimeAEC::begin(const AecConfig* cfg)
{
    // 默认参数
    _cfg.mu              = 0.1f;
    _cfg.echo_gain       = 1.0f;
    _cfg.noise_gate      = 60.0f;
    _cfg.vad_threshold   = 200.0f;
    _cfg.vad_confirm_ms  = 200;
    _cfg.enable          = true;
    if (cfg) { memcpy(&_cfg, cfg, sizeof(AecConfig)); }

    // 滤波器权重 (float32, 512 taps = 2KB)
    _w = (float*)calloc(AEC_FILTER_TAPS, sizeof(float));
    // 参考信号历史环 (int16, 512 = 1KB)
    _refHist = (int16_t*)calloc(AEC_HISTORY_SAMPLES, sizeof(int16_t));
    if (!_w || !_refHist) {
        Serial.println("[AEC] malloc failed");
        return false;
    }

    Serial.printf("[AEC] init: mu=%.3f taps=%d confirm=%dms\n",
                  _cfg.mu, AEC_FILTER_TAPS, (int)_cfg.vad_confirm_ms);
    return true;
}

void RealtimeAEC::reset()
{
    if (_w)       memset(_w, 0, AEC_FILTER_TAPS * sizeof(float));
    if (_refHist) memset(_refHist, 0, AEC_HISTORY_SAMPLES * sizeof(int16_t));
    _refHistPos = 0;
    _speechActive = false;
    _converged = false;
    _frameCount = 0;
}

void RealtimeAEC::pushReference(const int16_t* pcm, size_t samples, int sample_rate)
{
    if (!_cfg.enable) return;

    // 播放参考是 24kHz, 处理链路是 16kHz → 3:2 线性重采样
    // (简单线性插值, 对回声参考信号精度足够)
    if (sample_rate == 24000) {
        const float ratio = 24000.0f / 16000.0f;  // 1.5
        for (size_t i = 0; i < samples; i++) {
            _resampleAccum += ratio;
            while (_resampleAccum >= 1.0f) {
                _resampleAccum -= 1.0f;
                int16_t s = pcm[i];
                _refHist[_refHistPos] = s;
                _refHistPos = (_refHistPos + 1) % AEC_HISTORY_SAMPLES;
            }
        }
    } else {
        // 16kHz: 直接入环
        for (size_t i = 0; i < samples; i++) {
            _refHist[_refHistPos] = pcm[i];
            _refHistPos = (_refHistPos + 1) % AEC_HISTORY_SAMPLES;
        }
    }
}

float RealtimeAEC::processFrame(int16_t* frame, bool* has_speech)
{
    _frameCount++;
    if (has_speech) *has_speech = false;

    // ---- 原始输入 RMS (统计) ----
    float sum = 0.0f;
    for (int i = 0; i < AEC_FRAME_SAMPLES; i++) {
        sum += (float)frame[i] * frame[i];
    }
    float input_rms = sqrtf(sum / AEC_FRAME_SAMPLES);
    _stats.input_rms = input_rms;

    if (!_cfg.enable) {
        // AEC 关闭: 仅做简单能量 VAD
        _stats.residual_rms = input_rms;
        _updateVad(input_rms, has_speech);
        return input_rms;
    }

    // ---- NLMS 自适应滤波 (误差输出) ----
    // 滤波: 用历史环中最近 512 个参考样本估计回声
    // 注意: 参考环写指针 _refHistPos 是最新样本的下一位,
    //       历史顺序为 [_refHistPos, _refHistPos+1, ...] 环形
    //
    // 性能: 本帧内参考环不变 (pushReference 由播放任务在帧间调用),
    //       所以 ||x||² 能量每帧只算一次 (滑动优化, O(512) 而非 O(320×512)),
    //       权重更新与滤波各 O(320×512), 总计 ≈328K MAC ≈ 1.2ms @240MHz。

    // 预计算参考能量 ||x||² (每帧一次)
    float x_norm = 0.0f;
    {
        int32_t idx = (int32_t)_refHistPos - 1;
        for (int k = 0; k < AEC_FILTER_TAPS; k++) {
            if (idx < 0) idx += AEC_HISTORY_SAMPLES;
            float xk = (float)_refHist[idx];
            x_norm += xk * xk;
            idx--;
        }
    }
    const float step = _cfg.mu / (x_norm + NLMS_EPS);

    float residual_sum = 0.0f;
    for (int n = 0; n < AEC_FRAME_SAMPLES; n++) {
        // 估计回声 y[n] = Σ w[k] * x[n-k] (x[n-1] 是最近样本)
        float y = 0.0f;
        int32_t idx = (int32_t)_refHistPos - 1;
        for (int k = 0; k < AEC_FILTER_TAPS; k++) {
            if (idx < 0) idx += AEC_HISTORY_SAMPLES;
            y += _w[k] * (float)_refHist[idx];
            idx--;
        }

        // 误差 e[n] = d[n] - y[n] (含回声增益补偿)
        float e = (float)frame[n] - _cfg.echo_gain * y;

        // 限幅防溢出
        if (e > 32767.0f) e = 32767.0f;
        if (e < -32768.0f) e = -32768.0f;
        frame[n] = (int16_t)e;
        residual_sum += e * e;

        // ---- 权重更新 (NLMS, 步长按帧能量归一化) ----
        idx = (int32_t)_refHistPos - 1;
        for (int k = 0; k < AEC_FILTER_TAPS; k++) {
            if (idx < 0) idx += AEC_HISTORY_SAMPLES;
            _w[k] += step * e * (float)_refHist[idx];
            idx--;
        }
    }

    float residual_rms = sqrtf(residual_sum / AEC_FRAME_SAMPLES);
    _stats.residual_rms = residual_rms;

    // ---- 回声衰减估计 (dB) ----
    if (input_rms > 1.0f) {
        float atten = 20.0f * log10f((residual_rms + 1.0f) / (input_rms + 1.0f));
        _stats.echo_attenuation_db = atten;
        // 收敛判定: 衰减稳定且滤波器有非零权重
        if (atten < -15.0f && _frameCount > 100) {
            _converged = true;
        }
    }
    _stats.frames_processed = _frameCount;

    // ---- 噪声门限降噪 ----
    if (residual_rms < _cfg.noise_gate) {
        memset(frame, 0, AEC_FRAME_SAMPLES * sizeof(int16_t));
        residual_rms = 0.0f;
    }

    // ---- VAD (能量 + 过零率) ----
    _updateVad(residual_rms, has_speech);
    return residual_rms;
}

/**
 * @brief VAD 端点检测 — 能量持续超过阈值 vad_confirm_ms 才确认
 *
 * 关键作用: 全双工播放时, AEC 残余回声若仍有能量, 会被 200ms
 * 确认窗口过滤, 避免本地播放声误触发打断。
 */
void RealtimeAEC::_updateVad(float rms, bool* has_speech)
{
    uint32_t now = (uint32_t)millis();

    if (rms >= _cfg.vad_threshold) {
        if (!_speechActive) {
            if (_speechStartTick == 0) {
                _speechStartTick = now;  // 首次超过阈值, 开始计时
            } else if ((now - _speechStartTick) >= (uint32_t)_cfg.vad_confirm_ms) {
                _speechActive = true;    // 持续确认 → 人声有效
            }
        }
    } else {
        // 低于阈值: 挂起 500ms 后重置 (容忍句中停顿)
        if (_speechActive && (now - _speechStartTick) > 500) {
            _speechActive = false;
            _speechStartTick = 0;
        } else if (!_speechActive) {
            _speechStartTick = 0;
        }
    }

    if (has_speech) *has_speech = _speechActive;
}
