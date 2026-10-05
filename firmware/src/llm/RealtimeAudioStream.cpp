/**
 * @file RealtimeAudioStream.cpp
 * @brief 全双工音频流实现 — 独立采集/播放 FreeRTOS 任务
 *
 * 任务优先级设计 (ESP32-S3 FreeRTOS 数值越大优先级越高):
 *   采集任务 5 > 播放任务 4 > webSocketLoopTask 3
 *   音频实时性优先, 网络收发不抢占音频 DMA。
 *
 * 全双工关键: 采集任务全程运行 (含播放期间), 播放期间采集到的
 * 环境声 + 回声经 AEC 处理后持续上传 — 这就是"边播边听"。
 */
#include "RealtimeAudioStream.h"
#include <M5Unified.h>
#include "share/Mutex.h"   // 沿用原项目音频互斥

RealtimeAudioStream::RealtimeAudioStream()
    : _capturedCb(nullptr), _speechCb(nullptr), _cbCtx(nullptr),
      _captureTaskH(nullptr), _playTaskH(nullptr),
      _ringHead(0), _ringTail(0), _playRunning(false),
      _captureRunning(false), _chunkBuf(nullptr),
      _frameCarryLen(0), _lastSpeechState(false)
{
    memset(_frameCarry, 0, sizeof(_frameCarry));
}

RealtimeAudioStream::~RealtimeAudioStream()
{
    stopCapture();
    stopPlayback();
    if (_chunkBuf) { free(_chunkBuf); _chunkBuf = nullptr; }
}

bool RealtimeAudioStream::begin(AudioCapturedCb capturedCb, SpeechStateCb speechCb,
                                void* ctx, const AecConfig* aecCfg)
{
    _capturedCb = capturedCb;
    _speechCb   = speechCb;
    _cbCtx      = ctx;

    if (!_aec.begin(aecCfg)) {
        return false;
    }

    // 采集块缓冲 (2000 samples = 4KB)
    _chunkBuf = (int16_t*)heap_caps_malloc(CAP_CHUNK_SAMPLES * sizeof(int16_t),
                                           MALLOC_CAP_8BIT);
    if (!_chunkBuf) {
        Serial.println("[AStream] chunk malloc failed");
        return false;
    }

    // 播放任务 (始终运行, 无数据时挂起)
    _playRunning = true;
    xTaskCreatePinnedToCore(_playTaskEntry, "audioPlayTask",
                            4 * 1024, this, 4, &_playTaskH, 1);  // core1
    Serial.println("[AStream] initialized (full-duplex)");
    return true;
}

void RealtimeAudioStream::startCapture()
{
    if (_captureRunning) return;
    _captureRunning = true;
    _aec.reset();  // 新会话开始时复位滤波器
    xTaskCreatePinnedToCore(_captureTaskEntry, "audioCapTask",
                            5 * 1024, this, 5, &_captureTaskH, 0);  // core0
    Serial.println("[AStream] capture task started");
}

void RealtimeAudioStream::stopCapture()
{
    _captureRunning = false;
    if (_captureTaskH) {
        vTaskDelete(_captureTaskH);   // 采集任务无共享资源, 直接删除
        _captureTaskH = nullptr;
    }
}

void RealtimeAudioStream::_captureTaskEntry(void* arg)
{
    ((RealtimeAudioStream*)arg)->_captureLoop();
}

void RealtimeAudioStream::_captureLoop()
{
    while (_captureRunning) {
        // 沿用原固件阻塞式采集 (125ms/块); 采集期间 CPU 让出给播放任务
        enterMutexAudio();
        bool ok = M5.Mic.record(_chunkBuf, CAP_CHUNK_SAMPLES, CAP_SAMPLE_RATE);
        exitMutexAudio();

        if (!ok) {
            Serial.println("[AStream] Mic.record failed");
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }

        // AEC + VAD + 回调上传 (块内切成 256-sample 帧)
        _processCaptureChunk(_chunkBuf, CAP_CHUNK_SAMPLES);

        // 拥塞/速率控制: 网络慢时丢弃旧帧由上层负责,
        // 这里按实时节奏调度 (125ms 一块, 与上传节奏一致)
        vTaskDelay(pdMS_TO_TICKS(1));
    }
    vTaskDelete(NULL);
}

void RealtimeAudioStream::_processCaptureChunk(int16_t* chunk, size_t samples)
{
    // 与上一块余量拼接, 凑成完整 AEC 帧 (16ms/256, esp-aec 帧长)
    size_t avail = _frameCarryLen + samples;
    size_t idx = 0;

    // 先补余量
    if (_frameCarryLen > 0) {
        size_t need = AEC_FRAME_SAMPLES - _frameCarryLen;
        if (avail >= AEC_FRAME_SAMPLES) {
            memcpy(&_frameCarry[_frameCarryLen], chunk, need * sizeof(int16_t));
            bool hasSpeech = false;
            _aec.processFrame(_frameCarry, &hasSpeech);
            if (_capturedCb) _capturedCb(_frameCarry, AEC_FRAME_SAMPLES, _cbCtx);
            if (hasSpeech != _lastSpeechState) {
                _lastSpeechState = hasSpeech;
                if (_speechCb) _speechCb(hasSpeech, _cbCtx);
            }
            idx = need;
            _frameCarryLen = 0;
        } else {
            // 本块还凑不满一帧, 全部进余量
            memcpy(&_frameCarry[_frameCarryLen], chunk, samples * sizeof(int16_t));
            _frameCarryLen += samples;
            return;
        }
    }

    // 整帧处理
    while (idx + AEC_FRAME_SAMPLES <= samples) {
        int16_t* frame = &chunk[idx];
        bool hasSpeech = false;
        _aec.processFrame(frame, &hasSpeech);
        if (_capturedCb) _capturedCb(frame, AEC_FRAME_SAMPLES, _cbCtx);
        if (hasSpeech != _lastSpeechState) {
            _lastSpeechState = hasSpeech;
            if (_speechCb) _speechCb(hasSpeech, _cbCtx);
        }
        idx += AEC_FRAME_SAMPLES;
    }

    // 剩余进余量 (samples - idx < 256)
    if (idx < samples) {
        _frameCarryLen = samples - idx;
        memcpy(_frameCarry, &chunk[idx], _frameCarryLen * sizeof(int16_t));
    }
}

// ---------------------------------------------------------------------------
// 播放路径
// ---------------------------------------------------------------------------

void RealtimeAudioStream::submitPlayback(const int16_t* pcm, size_t samples, int sample_rate)
{
    // 写入环形缓冲 (空间不足时丢弃最旧数据 — 优先实时性)
    // 注意: AEC 参考注入在播放任务实际发声前进行 (见 _playLoop),
    // 保证参考信号与麦克风听到的回声时间对齐 — 若在此处注入,
    // 参考会比实际发声提前一个环形缓冲深度 (~50-100ms)。
    // playRaw 内部缓冲/出声时刻的回波延迟由 RealtimeAEC auto_align
    // 互相关自动估计并补偿 (参考环 256ms, 搜索窗覆盖该延迟)。
    for (size_t i = 0; i < samples; i++) {
        uint32_t next = (_ringHead + 1) % PLAY_RING_SAMPLES;
        if (next == _ringTail) {
            // 环满: 丢弃最旧
            _ringTail = (_ringTail + 1) % PLAY_RING_SAMPLES;
        }
        _playRing[_ringHead] = pcm[i];
        _ringHead = next;
    }
}

void RealtimeAudioStream::stopPlayback()
{
    // 立即清空缓冲 (打断: 不再播放已入队的音频)
    _ringHead = _ringTail;
    if (M5.Speaker.isEnabled()) {
        M5.Speaker.stop();
    }
}

bool RealtimeAudioStream::isPlaybackIdle() const
{
    return (_ringHead == _ringTail) && !M5.Speaker.isPlaying();
}

size_t RealtimeAudioStream::playbackBuffered() const
{
    return (_ringHead >= _ringTail)
        ? (_ringHead - _ringTail)
        : (PLAY_RING_SAMPLES - _ringTail + _ringHead);
}

void RealtimeAudioStream::_playTaskEntry(void* arg)
{
    ((RealtimeAudioStream*)arg)->_playLoop();
}

void RealtimeAudioStream::_playLoop()
{
    // 播放分片: 每片 600 samples @24kHz = 25ms (低于 50ms 首片延迟要求)
    constexpr size_t PLAY_SLICE = 600;
    int16_t slice[PLAY_SLICE];

    while (_playRunning) {
        if (_ringHead == _ringTail) {
            // 缓冲空: 挂起等待 (采集/网络任务不受影响)
            vTaskDelay(pdMS_TO_TICKS(2));
            continue;
        }

        // 从环形缓冲取一片
        size_t avail = playbackBuffered();
        size_t take = avail < PLAY_SLICE ? avail : PLAY_SLICE;
        for (size_t i = 0; i < take; i++) {
            slice[i] = _playRing[_ringTail];
            _ringTail = (_ringTail + 1) % PLAY_RING_SAMPLES;
        }

        // 实际发声前注入 AEC 参考信号 (与播放时间对齐)
        _aec.pushReference(slice, take, PLAY_SAMPLE_RATE);

        // 播放 (异步 DMA, 不阻塞)
        enterMutexAudio();
        M5.Speaker.playRaw(slice, take, PLAY_SAMPLE_RATE, false);
        exitMutexAudio();

        vTaskDelay(pdMS_TO_TICKS(20));  // ≈切片时长, 给 DMA 让路
    }
    vTaskDelete(NULL);
}
