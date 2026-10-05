/**
 * @file RealtimeAEC.cpp
 * @brief 声学回声消除实现 (ESP32-S3)
 *
 * 后端 (默认 esp-sr 官方 esp_aec; 自研 NLMS 保留回退):
 *   esp_aec: 频域自适应滤波 + NLP 非线性处理, 16ms/256 样本帧,
 *            filter 1200 (75ms 回声尾), bench 实测显著优于自研 NLMS。
 *   NLMS:    时域归一化 LMS, 与 esp_aec 同接口, 供对照/回退。
 *
 * 播放期对齐 (auto_align, 两后端共用):
 *   参考环只记录"提交播放的 PCM", 真实出声时刻滞后于写入时刻
 *   (DMA 缓冲 + 功放/扬声器 + 声学传播 + ADC)。回波延迟 _k0 由
 *   互相关估计: 高能量候选帧对参考环做粗搜 (bin=1ms) 互相关累加,
 *   每 ~400ms 找峰值并精搜 (±1ms), 峰均比 >1.6 才更新, 多帧
 *   累加抑制近端人声。AEC 处理前按 _k0 从参考环取对齐参考帧,
 *   ref[n] ≈ play[t - k0 + n], 与 mic[n] 的声学回波时间同步 —
 *   esp_aec 要求 ref 与 mic 严格对齐, 对齐误差 ±1 样本。
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

RealtimeAEC::RealtimeAEC()
    : _w(nullptr), _espAec(nullptr), _espRef(nullptr), _espOut(nullptr),
      _refHist(nullptr), _alignLastFrame(nullptr), _alignAccum(nullptr),
      _refHistPos(0),
      _k0(0), _alignCount(0), _alignUpdates(0), _alignPendingK0(-1), _lastRefPushMs(0),
      _aligned(false),
      _speechActive(false), _converged(false),
      _speechStartTick(0), _frameCount(0),
      _resampleAccum(0.0f), _lastResampleSample(0)
{
    memset(&_stats, 0, sizeof(_stats));
}

RealtimeAEC::~RealtimeAEC()
{
    if (_espAec)       { aec_destroy(_espAec);    _espAec = nullptr; }
    if (_w)            { free(_w);                _w = nullptr; }
    if (_espRef)       { free(_espRef);           _espRef = nullptr; }
    if (_espOut)       { free(_espOut);           _espOut = nullptr; }
    if (_refHist)      { free(_refHist);          _refHist = nullptr; }
    if (_alignLastFrame){ free(_alignLastFrame);  _alignLastFrame = nullptr; }
    if (_alignAccum)   { free(_alignAccum);       _alignAccum = nullptr; }
}

bool RealtimeAEC::begin(const AecConfig* cfg)
{
    // 重复调用 (会话重启) 安全: 先释放旧实例与缓冲, 防泄漏与二次创建冲突
    if (_espAec)         { aec_destroy(_espAec);   _espAec = nullptr; }
    if (_w)              { free(_w);               _w = nullptr; }
    if (_espRef)         { free(_espRef);          _espRef = nullptr; }
    if (_espOut)         { free(_espOut);          _espOut = nullptr; }
    if (_refHist)        { free(_refHist);         _refHist = nullptr; }
    if (_alignLastFrame) { free(_alignLastFrame);  _alignLastFrame = nullptr; }
    if (_alignAccum)     { free(_alignAccum);      _alignAccum = nullptr; }

    // 默认参数
    _cfg.mu              = 0.1f;    // NLMS 步长 (0.1: 逐样本归一化下稳定; 时间常数 ~0.2s)
    _cfg.echo_gain       = 1.0f;
    _cfg.noise_gate      = 90.0f;   // 软衰减门限: 底噪(环境/残余回声)衰减÷4 到 <120 (服务端静音阈值), 轻声人声保留
    _cfg.vad_threshold   = 200.0f;
    _cfg.vad_confirm_ms  = 200;
    _cfg.enable          = true;
    _cfg.backend         = AEC_BACKEND_NLMS;  // 默认 NLMS: esp-sr 实测与 16ms 流式不兼容 (见头文件)
    _cfg.auto_align      = true;
    _cfg.ref_delay_samples = 0;
    if (cfg) { memcpy(&_cfg, cfg, sizeof(AecConfig)); }
#ifdef AEC_FORCE_ESP_SR
    _cfg.backend = AEC_BACKEND_ESP_SR;   // 实验: esp-sr 后端 (需自行承担崩溃风险, 见头文件说明)
#endif
#ifdef AEC_FORCE_NLMS
    _cfg.backend = AEC_BACKEND_NLMS;     // 兼容旧开关
#endif

    // 共用缓冲: 参考历史环 (8KB) + 对齐辅助
    _refHist = (int16_t*)calloc(AEC_HISTORY_SAMPLES, sizeof(int16_t));
    _alignLastFrame = (int16_t*)calloc(AEC_FRAME_SAMPLES, sizeof(int16_t));
    _alignAccum = (float*)calloc(AEC_ALIGN_BINS, sizeof(float));
    _espRef = (int16_t*)calloc(AEC_FRAME_SAMPLES, sizeof(int16_t));
    _espOut = (int16_t*)calloc(AEC_FRAME_SAMPLES, sizeof(int16_t));
    if (!_refHist || !_alignLastFrame || !_alignAccum || !_espRef || !_espOut) {
        Serial.println("[AEC] malloc failed");
        return false;
    }

    // 后端初始化
    if (_cfg.backend == AEC_BACKEND_ESP_SR) {
        // 内存诊断 + 门禁: 采用 SPIRAM 变体 (mode2/4, 内部分配走 PSRAM);
        // 仅需内部 RAM 放小结构 + 本模块缓冲
        size_t free_int = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
        size_t largest = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
        size_t free_ps = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
        Serial.printf("[AEC] mem: internal free=%uKB largest=%uKB | psram free=%uKB\n",
                      (unsigned)(free_int / 1024), (unsigned)(largest / 1024),
                      (unsigned)(free_ps / 1024));
        if (largest < (size_t)AEC_ESP_MIN_LARGEST_KB * 1024
                || free_ps < (size_t)AEC_ESP_MIN_PSRAM_KB * 1024) {
            Serial.printf("[AEC] 内存门禁未过 (内部大块<%dKB 或 PSRAM<%dKB) → 回退 NLMS\n",
                          AEC_ESP_MIN_LARGEST_KB, AEC_ESP_MIN_PSRAM_KB);
            _cfg.backend = AEC_BACKEND_NLMS;
        } else {
            // 堆探针: 按 speex 内部典型分配尺寸逐级试探 (与库同 calloc 路径)。
            // 若崩溃停在某级 → 该 size class 链表损坏 (决定性定位信息);
            // 全部通过而 create 崩 → 库特定分配序列问题。
            static const size_t probe_sizes[] = { 124, 640, 4096, 8192, 16384, 32768, 65536 };
            for (size_t i = 0; i < sizeof(probe_sizes) / sizeof(probe_sizes[0]); i++) {
                void* p = calloc(1, probe_sizes[i]);
                Serial.printf("[AEC] probe %uB: %s\n",
                              (unsigned)probe_sizes[i], p ? "OK" : "NULL");
                if (p) free(p);
            }

            // mode 级联 (SPIRAM 变体优先):
            //   实测: mode1 (internal) → esp_aec3_init 内部 RAM 不足(*150KB 连续需求),
            //         分配返回 NULL 但库不检查 → 填表 NULL 崩;
            //         mode5 (728 加速内核) → 处理时越界写任务栈 canary;
            //         aec_create (speex) → 创建时堆崩; 均已排除。
            //   mode2/4 = SPIRAM 变体: 内部分配走 PSRAM(7.7MB) → 不受内部 RAM 限制。
            Serial.println("[AEC] try aec_pro_create(256,1,2) [SPIRAM medium]...");
            _espAec = aec_pro_create(AEC_FRAME_SAMPLES, 1, 2);
            if (!_espAec) {
                Serial.println("[AEC] mode2 NULL → try mode4 [SPIRAM aggressive]...");
                _espAec = aec_pro_create(AEC_FRAME_SAMPLES, 1, 4);
            }
            if (!_espAec) {
                Serial.println("[AEC] esp_aec 创建失败 → 回退 NLMS");
                _cfg.backend = AEC_BACKEND_NLMS;
            }
        }
    }
    if (_cfg.backend == AEC_BACKEND_NLMS) {
        _w = (float*)calloc(AEC_FILTER_TAPS, sizeof(float));
        if (!_w) {
            Serial.println("[AEC] NLMS malloc failed");
            return false;
        }
    }

    // 初始延迟补偿 (auto_align 时作为估计起点; 手动模式固定生效)
    uint32_t k0_max = (uint32_t)(AEC_HISTORY_SAMPLES
                                 - ((_cfg.backend == AEC_BACKEND_ESP_SR)
                                    ? AEC_ESP_FILTER : AEC_FILTER_TAPS));
    _k0 = (_cfg.ref_delay_samples > 0) ? (uint32_t)_cfg.ref_delay_samples : 0;
    if (_k0 > k0_max) _k0 = k0_max;

    Serial.printf("[AEC] init: backend=%s auto_align=%d k0=%u confirm=%dms\n",
                  (_cfg.backend == AEC_BACKEND_ESP_SR) ? "esp-sr" : "nlms",
                  (int)_cfg.auto_align, (unsigned)_k0, (int)_cfg.vad_confirm_ms);
    return true;
}

void RealtimeAEC::reset()
{
    if (_cfg.backend == AEC_BACKEND_ESP_SR) {
        if (_espAec) { aec_destroy(_espAec); _espAec = nullptr; }
        _espAec = aec_create(16000, AEC_FRAME_SAMPLES, AEC_ESP_FILTER);
        if (!_espAec) {
            Serial.println("[AEC] reset: esp_aec 重建失败 → 回退 NLMS");
            _cfg.backend = AEC_BACKEND_NLMS;
            if (!_w) _w = (float*)calloc(AEC_FILTER_TAPS, sizeof(float));
        }
    }
    if (_w)             memset(_w, 0, AEC_FILTER_TAPS * sizeof(float));
    if (_refHist)       memset(_refHist, 0, AEC_HISTORY_SAMPLES * sizeof(int16_t));
    if (_alignLastFrame)memset(_alignLastFrame, 0, AEC_FRAME_SAMPLES * sizeof(int16_t));
    if (_alignAccum)    memset(_alignAccum, 0, AEC_ALIGN_BINS * sizeof(float));
    _refHistPos = 0;
    _alignCount = 0;
    _alignPendingK0 = -1;
    // _k0 保留: 硬件回波延迟跨会话不变, 下次会话直接沿用
    _speechActive = false;
    _converged = false;
    _frameCount = 0;
}

void RealtimeAEC::pushReference(const int16_t* pcm, size_t samples, int sample_rate)
{
    if (!_cfg.enable) return;

    _lastRefPushMs = (uint32_t)millis();   // 播放活跃标记 (对齐估计仅在播放期有意义)

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

    // 初始化不完整防御 (begin 失败/部分分配): 直通 + VAD, 不得空指针解引用
    if (!_refHist || !_alignLastFrame || !_alignAccum) {
        _stats.residual_rms = input_rms;
        _updateVad(input_rms, has_speech);
        return input_rms;
    }

    // ---- 自动延迟对齐 (AEC 处理前, 用原始麦克风帧) ----
    _autoAlign(frame, input_rms);

    // ---- AEC 后端 ----
    if (_cfg.backend == AEC_BACKEND_ESP_SR) {
        _processEsp(frame);
    } else {
        _processNlms(frame, input_rms);
    }

    float residual_rms = _stats.residual_rms;

    // ---- 回声衰减估计 (dB, 诊断/收敛判定) ----
    if (input_rms > 1.0f) {
        float atten = 20.0f * log10f((residual_rms + 1.0f) / (input_rms + 1.0f));
        _stats.echo_attenuation_db = atten;
        if (atten < -8.0f && _frameCount > 150) {
            _converged = true;
        }
    }
    _stats.frames_processed = _frameCount;

    // ---- 噪声门限降噪 (软衰减, 不清零) ----
    // 低于门限直接置零会香掉轻声说话/句中停顿 → STT 收不到
    if (_cfg.noise_gate > 0.0f && residual_rms < _cfg.noise_gate) {
        for (int i = 0; i < AEC_FRAME_SAMPLES; i++) {
            frame[i] = frame[i] >> 2;          // /4 衰减 (保留信号连续性)
        }
        residual_rms *= 0.25f;
    }

    // ---- VAD (能量) ----
    _updateVad(residual_rms, has_speech);
    return residual_rms;
}

// ---------------------------------------------------------------------------
// esp-sr 后端
// ---------------------------------------------------------------------------

/**
 * @brief 从参考环按当前延迟补偿 k0 取对齐参考帧
 *
 * 语义: ref[n] = play[t - k0 + n] (与 mic[n] 的声学回波时间同步,
 * 偏差 ≤1 样本)。环中 pos-1 为最新样本, 帧起点 = pos - FRAME - k0。
 */
void RealtimeAEC::_fetchAlignedRef(int16_t* out)
{
    int32_t base = (int32_t)_refHistPos - AEC_FRAME_SAMPLES - (int32_t)_k0;
    for (int n = 0; n < AEC_FRAME_SAMPLES; n++) {
        int32_t i2 = base + n;
        if (i2 < 0) i2 += AEC_HISTORY_SAMPLES;
        else if (i2 >= AEC_HISTORY_SAMPLES) i2 -= AEC_HISTORY_SAMPLES;
        out[n] = _refHist[i2];
    }
}

/**
 * @brief esp_aec 帧处理: aec_process(mic, 对齐ref, out) → 原地输出
 */
void RealtimeAEC::_processEsp(int16_t* frame)
{
    if (!_espAec) {        // 实例丢失 (内存异常) → 尝试重建, 失败则降级 NLMS
        _espAec = aec_create(16000, AEC_FRAME_SAMPLES, AEC_ESP_FILTER);
        if (!_espAec) {
            _cfg.backend = AEC_BACKEND_NLMS;
            if (!_w) _w = (float*)calloc(AEC_FILTER_TAPS, sizeof(float));
            if (!_w) {                  // 连回退权重都失败: 本帧直通
                _stats.residual_rms = _stats.input_rms;
                return;
            }
            _processNlms(frame, _stats.input_rms);
            return;
        }
    }

    _fetchAlignedRef(_espRef);
    aec_process(_espAec, frame, _espRef, _espOut);
    memcpy(frame, _espOut, AEC_FRAME_SAMPLES * sizeof(int16_t));

    float sum = 0.0f;
    for (int i = 0; i < AEC_FRAME_SAMPLES; i++) {
        sum += (float)frame[i] * frame[i];
    }
    _stats.residual_rms = sqrtf(sum / AEC_FRAME_SAMPLES);
}

// ---------------------------------------------------------------------------
// NLMS 后端 (回退/对照)
// ---------------------------------------------------------------------------

/**
 * @brief 时域 NLMS 滤波: y[n] = Σ w[k] * x[n-k0-k]; 参考起点偏移 k0
 *
 * 双讲检测 (收敛后启用): 收敛前必须自由学习回声路径, 否则
 * 未收敛时残余≈输入 → 误判双讲 → 冻结适应 → 永不收敛的死锁。
 * 收敛后近端人声主导时冻结, 防削人声。发散保护: 残余能量异常
 * 高于输入 (滤波器自激) → 重置权重。
 */
void RealtimeAEC::_processNlms(int16_t* frame, float input_rms)
{
    if (!_w) {                          // 兜底: 权重未分配 (理论上不应发生)
        _stats.residual_rms = input_rms;
        return;
    }
    const int32_t k0 = (int32_t)_k0;

    // 帧首样本 (frame[0]) 在参考环中的位置:
    //   mic[n] 的回波源自时刻 (帧首 t 前 k0 样本 + n) → 环位置 = pos-FRAME-k0+n
    //   关键: 参考索引必须随 n 滑动; 整帧冻结为单一位置会使权重更新
    //   变成 Σ_n e[n]×常量 ≈ 0 (交流相消), 永远学不到回声路径
    int32_t frameBase = (int32_t)_refHistPos - AEC_FRAME_SAMPLES - k0;
    while (frameBase < 0) frameBase += AEC_HISTORY_SAMPLES;

    bool near_end_talk = _converged &&
                         (_stats.input_rms > 150.0f) &&
                         (_stats.residual_rms > _stats.input_rms * 0.6f);

    // 滑窗能量 ||x||² : 每帧全量重算初窗 (重置 float 累积误差), 帧内增量 O(1)
    //   pow(n)   = Σ_{k<TAPS} x[base+n-k]²
    //   pow(n+1) = pow(n) - x[base+n-TAPS+1]² + x[base+n+1]²
    float pow_n = 0.0f;
    {
        int32_t idx = frameBase;
        for (int k = 0; k < AEC_FILTER_TAPS; k++) {
            if (idx < 0) idx += AEC_HISTORY_SAMPLES;
            float xk = (float)_refHist[idx];
            pow_n += xk * xk;
            idx--;
        }
    }

    float residual_sum = 0.0f;
    for (int n = 0; n < AEC_FRAME_SAMPLES; n++) {
        float y = 0.0f;
        int32_t idx = frameBase + n;              // ← 随样本 n 滑动
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

        if (!near_end_talk && pow_n > 1.0f) {   // 静音保护: 参考能量过低时不更新
            const float step_n = _cfg.mu / (pow_n + NLMS_EPS);
            idx = frameBase + n;          // 同样滑动
            for (int k = 0; k < AEC_FILTER_TAPS; k++) {
                if (idx < 0) idx += AEC_HISTORY_SAMPLES;
                _w[k] += step_n * e * (float)_refHist[idx];
                idx--;
            }
        }

        // 增量更新窗能量 (供下一采样)
        if (n + 1 < AEC_FRAME_SAMPLES) {
            int32_t add = frameBase + n + 1;
            while (add >= AEC_HISTORY_SAMPLES) add -= AEC_HISTORY_SAMPLES;
            int32_t sub = frameBase + n - (AEC_FILTER_TAPS - 1);
            while (sub < 0) sub += AEC_HISTORY_SAMPLES;
            float xa = (float)_refHist[add];
            float xs = (float)_refHist[sub];
            pow_n += xa * xa - xs * xs;
        }
    }

    float residual_rms = sqrtf(residual_sum / AEC_FRAME_SAMPLES);
    _stats.residual_rms = residual_rms;

    // 发散保护
    if (input_rms > 50.0f && residual_rms > input_rms * 3.0f) {
        memset(_w, 0, AEC_FILTER_TAPS * sizeof(float));
        _converged = false;
        if (_frameCount % 100 == 0) {
            Serial.printf("[AEC] diverge detected (in=%.0f res=%.0f) → reset\n",
                          input_rms, residual_rms);
        }
    }
}

// ---------------------------------------------------------------------------
// 自动延迟对齐 (两后端共用)
// ---------------------------------------------------------------------------

/**
 * @brief 候选帧互相关累加
 *
 * 物理意义: 回波路径延迟 L 时, 麦克风帧与参考环中
 * 位置 (写指针 - 帧长 - L) 附近的参考段相关性最高。
 * 高能量帧 (播放/人声活跃) 每帧累加粗搜互相关
 * (bin=1ms, 2x 下采样), 近端人声帧与参考不相关,
 * 多帧累加后随机相位抵消、真峰值增强。
 */
void RealtimeAEC::_autoAlign(const int16_t* frame, float input_rms)
{
    if (!_cfg.auto_align) return;

    // 候选帧: 输入能量高 (静默帧互相关无意义)
    if (input_rms <= 400.0f) return;

    // 仅当播放活跃 (最近 500ms 内有参考推送) 才估计:
    // 纯人声期没有回声, 互相关是噪声峰, 会污染 k0 并引发无意义的跳变重置
    if ((uint32_t)(millis() - _lastRefPushMs) > 500) return;

    memcpy(_alignLastFrame, frame, AEC_FRAME_SAMPLES * sizeof(int16_t));

    // 互相关 corr(b) = Σ_n frame[n] * ref[base - b*STEP + n]
    //   base = 写指针 - 帧长 (当前帧起始样本前一位), ref 索引随 n 同步
    //   递增 — 帧内时间对齐; 延迟 b*STEP 假设下各 n 贡献同号相长。
    //   (索引随 n 递减会跨 bin 模糊, 峰值偏移一个帧长)
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

/**
 * @brief 延迟评估: 粗搜峰值 → 精搜 ±1ms → 更新 _k0
 *
 * 峰均比 <1.6 时数据不足以判定, 保持当前 _k0。
 * 估计显著跳变 (>2ms) 或首次建立时重置自适应状态:
 * 旧状态基于旧延迟, 已失去意义 (esp_aec 重建实例, NLMS 清权重)。
 */
void RealtimeAEC::_updateDelay()
{
    // ---- 粗搜峰值 + 峰均比 ----
    float best = 0.0f;
    int32_t best_b = 0;
    float mean = 0.0f;
    for (int b = 0; b < AEC_ALIGN_BINS; b++) {
        float v = fabsf(_alignAccum[b]);
        mean += v;
        if (v > best) { best = v; best_b = b; }
    }
    mean /= (float)AEC_ALIGN_BINS;
    if (mean < 1e-6f || best < mean * 1.6f) return;    // 峰值不显著 → 保持

    // ---- 精搜: 峰值 bin 附近 ±1ms, 步长 1 样本 (与粗搜同窗口方式) ----
    int32_t base = (int32_t)_refHistPos - AEC_FRAME_SAMPLES;
    if (base < 0) base += AEC_HISTORY_SAMPLES;
    int32_t center = best_b * AEC_ALIGN_STEP;
    int32_t hi_max = (int32_t)(AEC_HISTORY_SAMPLES
                               - ((_cfg.backend == AEC_BACKEND_ESP_SR)
                                  ? AEC_ESP_FILTER : AEC_FILTER_TAPS));
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

    // ---- 更新 _k0 (稳定性策略: 平滑吸收小变化; 显著变化需连续两次确认) ----
    uint32_t new_k0 = (uint32_t)best_lag;
    if (new_k0 > (uint32_t)hi_max) new_k0 = (uint32_t)hi_max;

    if (!_aligned) {
        // 首次建立: 直接接受 + 重置自适应状态
        _k0 = new_k0;
        _aligned = true;
        _alignPendingK0 = -1;
        _resetAdaptation();
    } else {
        int32_t diff = (int32_t)new_k0 - (int32_t)_k0;
        if (diff <= 64 && diff >= -64) {
            // 小变化 (≤4ms): EMA 平滑吸收, 不重置权重 (taps 窗口可兜住)
            _k0 = (uint32_t)((3 * (int32_t)_k0 + (int32_t)new_k0) / 4);
            _alignPendingK0 = -1;
        } else {
            // 显著变化: 需连续两次估计相近 (±32) 才接受 (确认真实漂移而非噪声)
            int32_t p = _alignPendingK0;
            if (p >= 0 && (int32_t)new_k0 - p <= 32 && (int32_t)new_k0 - p >= -32) {
                _k0 = new_k0;
                _alignPendingK0 = -1;
                _resetAdaptation();
            } else {
                _alignPendingK0 = (int32_t)new_k0;
            }
        }
    }
    _alignUpdates++;

    if (_alignUpdates <= 8 || _alignUpdates % 10 == 0) {
        Serial.printf("[AEC] delay aligned: k0=%u (%.1fms) in=%.0f res=%.0f\n",
                      (unsigned)_k0, (float)_k0 * 1000.0f / 16000.0f,
                      _stats.input_rms, _stats.residual_rms);
    }
}

/**
 * @brief 重置自适应状态 (延迟变更时)
 *
 * NLMS: 清权重; esp 实验路径: 重建实例 (无 reset API)。
 */
void RealtimeAEC::_resetAdaptation()
{
    if (_cfg.backend == AEC_BACKEND_ESP_SR && _espAec) {
        aec_destroy(_espAec);
        _espAec = aec_create(16000, AEC_FRAME_SAMPLES, AEC_ESP_FILTER);
        if (!_espAec) {
            _cfg.backend = AEC_BACKEND_NLMS;
            if (!_w) _w = (float*)calloc(AEC_FILTER_TAPS, sizeof(float));
            Serial.println("[AEC] 延迟跳变重建失败 → 回退 NLMS");
        }
    }
    if (_w) memset(_w, 0, AEC_FILTER_TAPS * sizeof(float));
    _converged = false;
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
