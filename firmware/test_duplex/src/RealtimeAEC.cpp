/**
 * @file RealtimeAEC.cpp
 * @brief NLMS 回声消除实现 (test_duplex 测量版)
 *
 * 算法: 时域 NLMS (同主固件)
 *   e[n] = d[n] - y[n];  y[n] = Σ w[k] * x[n-k];  w[k] += mu*e*x/(||x||²+eps)
 *
 * 播放期对齐 (auto_align): 与主固件一致 — 高能量候选帧对参考环
 * 粗搜互相关 (bin=1ms) 累加, 每 ~500ms 找峰精搜 ±1ms, 峰均比
 * >1.6 时更新滤波起点 _k0。bench 延迟扫描时关闭 auto_align。
 *
 * 与主固件差异: 无双讲检测/发散保护; 收敛门槛 -15dB;
 * 噪声门低于 60 RMS 置零 (量化残余, 便于 bench 对比)。
 */
#include "RealtimeAEC.h"
#include <math.h>

// ---- NLMS 保护常量 ----
#define NLMS_EPS (1e-6f)

RealtimeAEC::RealtimeAEC()
    : _w(nullptr), _refHist(nullptr), _alignLastFrame(nullptr), _alignAccum(nullptr),
      _refHistPos(0),
      _k0(0), _alignCount(0), _alignUpdates(0), _aligned(false),
      _speechActive(false), _converged(false),
      _speechStartTick(0), _frameCount(0),
      _resampleAccum(0.0f), _lastResampleSample(0)
{
    memset(&_stats, 0, sizeof(_stats));
}

RealtimeAEC::~RealtimeAEC()
{
    if (_w)             { free(_w);             _w = nullptr; }
    if (_refHist)       { free(_refHist);       _refHist = nullptr; }
    if (_alignLastFrame){ free(_alignLastFrame);_alignLastFrame = nullptr; }
    if (_alignAccum)    { free(_alignAccum);    _alignAccum = nullptr; }
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
    _cfg.auto_align      = true;
    _cfg.ref_delay_samples = 0;
    if (cfg) { memcpy(&_cfg, cfg, sizeof(AecConfig)); }

    _w = (float*)calloc(AEC_FILTER_TAPS, sizeof(float));
    _refHist = (int16_t*)calloc(AEC_HISTORY_SAMPLES, sizeof(int16_t));
    _alignLastFrame = (int16_t*)calloc(AEC_FRAME_SAMPLES, sizeof(int16_t));
    _alignAccum = (float*)calloc(AEC_ALIGN_BINS, sizeof(float));
    if (!_w || !_refHist || !_alignLastFrame || !_alignAccum) {
        Serial.println("[AEC] malloc failed");
        return false;
    }

    _k0 = (_cfg.ref_delay_samples > 0) ? (uint32_t)_cfg.ref_delay_samples : 0;
    if (_k0 > (uint32_t)(AEC_HISTORY_SAMPLES - AEC_FILTER_TAPS)) {
        _k0 = (uint32_t)(AEC_HISTORY_SAMPLES - AEC_FILTER_TAPS);
    }

    Serial.printf("[AEC] init: mu=%.3f taps=%d auto_align=%d k0=%u confirm=%dms\n",
                  _cfg.mu, AEC_FILTER_TAPS, (int)_cfg.auto_align,
                  (unsigned)_k0, (int)_cfg.vad_confirm_ms);
    return true;
}

void RealtimeAEC::reset()
{
    if (_w)             memset(_w, 0, AEC_FILTER_TAPS * sizeof(float));
    if (_refHist)       memset(_refHist, 0, AEC_HISTORY_SAMPLES * sizeof(int16_t));
    if (_alignLastFrame)memset(_alignLastFrame, 0, AEC_FRAME_SAMPLES * sizeof(int16_t));
    if (_alignAccum)    memset(_alignAccum, 0, AEC_ALIGN_BINS * sizeof(float));
    _refHistPos = 0;
    _alignCount = 0;
    _speechActive = false;
    _converged = false;
    _frameCount = 0;
}

void RealtimeAEC::pushReference(const int16_t* pcm, size_t samples, int sample_rate)
{
    if (!_cfg.enable) return;

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

    float sum = 0.0f;
    for (int i = 0; i < AEC_FRAME_SAMPLES; i++) {
        sum += (float)frame[i] * frame[i];
    }
    float input_rms = sqrtf(sum / AEC_FRAME_SAMPLES);
    _stats.input_rms = input_rms;

    if (!_cfg.enable) {
        _stats.residual_rms = input_rms;
        _updateVad(input_rms, has_speech);
        return input_rms;
    }

    const int32_t k0 = (int32_t)_k0;

    // 预计算参考能量 ||x||² (每帧一次, 从 k0 起点)
    float x_norm = 0.0f;
    {
        int32_t idx = (int32_t)_refHistPos - 1 - k0;
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
        float y = 0.0f;
        int32_t idx = (int32_t)_refHistPos - 1 - k0;
        for (int k = 0; k < AEC_FILTER_TAPS; k++) {
            if (idx < 0) idx += AEC_HISTORY_SAMPLES;
            y += _w[k] * (float)_refHist[idx];
            idx--;
        }

        float e = (float)frame[n] - _cfg.echo_gain * y;

        if (e > 32767.0f) e = 32767.0f;
        if (e < -32768.0f) e = -32768.0f;
        frame[n] = (int16_t)e;
        residual_sum += e * e;

        idx = (int32_t)_refHistPos - 1 - k0;
        for (int k = 0; k < AEC_FILTER_TAPS; k++) {
            if (idx < 0) idx += AEC_HISTORY_SAMPLES;
            _w[k] += step * e * (float)_refHist[idx];
            idx--;
        }
    }

    float residual_rms = sqrtf(residual_sum / AEC_FRAME_SAMPLES);
    _stats.residual_rms = residual_rms;

    if (input_rms > 1.0f) {
        float atten = 20.0f * log10f((residual_rms + 1.0f) / (input_rms + 1.0f));
        _stats.echo_attenuation_db = atten;
        if (atten < -15.0f && _frameCount > 100) {
            _converged = true;
        }
    }
    _stats.frames_processed = _frameCount;

    // ---- 自动延迟对齐 (互相关估计回波延迟, 噪声门限前调用) ----
    _autoAlign(frame, input_rms, residual_rms);

    // ---- 噪声门限降噪 (置零, bench 量化残余) ----
    if (residual_rms < _cfg.noise_gate) {
        memset(frame, 0, AEC_FRAME_SAMPLES * sizeof(int16_t));
        residual_rms = 0.0f;
    }

    _updateVad(residual_rms, has_speech);
    return residual_rms;
}

// ---------------------------------------------------------------------------
// 自动延迟对齐 (与主固件一致)
// ---------------------------------------------------------------------------

void RealtimeAEC::_autoAlign(const int16_t* frame, float input_rms, float residual_rms)
{
    if (!_cfg.auto_align) return;

    if (input_rms <= 400.0f) return;

    memcpy(_alignLastFrame, frame, AEC_FRAME_SAMPLES * sizeof(int16_t));

    // 互相关 corr(b) = Σ_n frame[n] * ref[base - b*STEP + n]
    //   ref 索引随 n 同步递增 — 帧内时间对齐 (递减会跨 bin 模糊)
    int32_t base = (int32_t)_refHistPos - AEC_FRAME_SAMPLES;
    if (base < 0) base += AEC_HISTORY_SAMPLES;
    for (int b = 0; b < AEC_ALIGN_BINS; b++) {
        int32_t idx = base - b * AEC_ALIGN_STEP;
        float acc = _alignAccum[b];
        for (int n = 0; n < AEC_FRAME_SAMPLES; n += 2) {
            int32_t i2 = idx + n;
            if (i2 < 0) i2 += AEC_HISTORY_SAMPLES;
            else if (i2 >= AEC_HISTORY_SAMPLES) i2 -= AEC_HISTORY_SAMPLES;
            acc += (float)frame[n] * (float)_refHist[i2];
        }
        _alignAccum[b] = acc;
    }
    _alignCount++;

    if (_alignCount >= AEC_ALIGN_EVERY) {
        _updateDelay();
        _alignCount = 0;
        memset(_alignAccum, 0, AEC_ALIGN_BINS * sizeof(float));
    }
}

void RealtimeAEC::_updateDelay()
{
    float best = 0.0f;
    int32_t best_b = 0;
    float mean = 0.0f;
    for (int b = 0; b < AEC_ALIGN_BINS; b++) {
        float v = fabsf(_alignAccum[b]);
        mean += v;
        if (v > best) { best = v; best_b = b; }
    }
    mean /= (float)AEC_ALIGN_BINS;
    if (mean < 1e-6f || best < mean * 1.6f) return;

    int32_t base = (int32_t)_refHistPos - AEC_FRAME_SAMPLES;
    if (base < 0) base += AEC_HISTORY_SAMPLES;
    int32_t center = best_b * AEC_ALIGN_STEP;
    int32_t hi_max = (int32_t)(AEC_HISTORY_SAMPLES - AEC_FILTER_TAPS);
    int32_t lo = center - AEC_ALIGN_STEP;   if (lo < 0) lo = 0;
    int32_t hi = center + AEC_ALIGN_STEP;   if (hi > hi_max) hi = hi_max;

    float best_f = 0.0f;
    int32_t best_lag = center;
    for (int32_t lag = lo; lag <= hi; lag++) {
        int32_t idx = base - lag;
        float acc = 0.0f;
        for (int n = 0; n < AEC_FRAME_SAMPLES; n += 2) {
            int32_t i2 = idx + n;
            if (i2 < 0) i2 += AEC_HISTORY_SAMPLES;
            else if (i2 >= AEC_HISTORY_SAMPLES) i2 -= AEC_HISTORY_SAMPLES;
            acc += (float)_alignLastFrame[n] * (float)_refHist[i2];
        }
        if (acc > best_f) { best_f = acc; best_lag = lag; }
    }

    uint32_t new_k0 = (uint32_t)best_lag;
    if (new_k0 > (uint32_t)hi_max) new_k0 = (uint32_t)hi_max;

    if (!_aligned || new_k0 > _k0 + 32 || new_k0 + 32 < _k0) {
        memset(_w, 0, AEC_FILTER_TAPS * sizeof(float));
        _converged = false;
    }
    _k0 = new_k0;
    _aligned = true;
    _alignUpdates++;

    if (_alignUpdates <= 3 || _alignUpdates % 10 == 0) {
        Serial.printf("[AEC] delay aligned: k0=%u (%.1fms) in=%.0f res=%.0f\n",
                      (unsigned)_k0, (float)_k0 * 1000.0f / 16000.0f,
                      _stats.input_rms, _stats.residual_rms);
    }
}

/**
 * @brief VAD 端点检测 — 能量持续超过阈值 vad_confirm_ms 才确认
 */
void RealtimeAEC::_updateVad(float rms, bool* has_speech)
{
    uint32_t now = (uint32_t)millis();

    if (rms >= _cfg.vad_threshold) {
        if (!_speechActive) {
            if (_speechStartTick == 0) {
                _speechStartTick = now;
            } else if ((now - _speechStartTick) >= (uint32_t)_cfg.vad_confirm_ms) {
                _speechActive = true;
            }
        }
    } else {
        if (_speechActive && (now - _speechStartTick) > 500) {
            _speechActive = false;
            _speechStartTick = 0;
        } else if (!_speechActive) {
            _speechStartTick = 0;
        }
    }

    if (has_speech) *has_speech = _speechActive;
}
