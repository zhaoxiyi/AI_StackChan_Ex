/**
 * @file RealtimeAudioStream.h
 * @brief 全双工音频流管理 — 采集/播放独立任务, 播放环形缓冲
 *
 * 职责:
 *   1. 采集任务 (高优先级): 持续 Mic.record → AEC 处理 → 回调上传
 *   2. 播放路径: 解码 PCM → AEC 参考注入 → 环形缓冲 → 播放任务消费
 *   3. 打断: 一键停止播放 + 清空缓冲 (与状态机配合)
 *
 * 与原有架构的关系:
 *   替代 RealtimeLLMBase::webSocketProcess() 里"录音→发送"的串行逻辑和
 *   streamAudioDelta() 里"等待播放结束"的阻塞逻辑。
 *   webSocketLoopTask 仅保留 webSocket.loop() 收发职责。
 */
#ifndef _REALTIME_AUDIO_STREAM_H
#define _REALTIME_AUDIO_STREAM_H

#include <Arduino.h>
#include "RealtimeAEC.h"

// 播放环形缓冲: 约 100ms 音频 @24kHz (2400 samples × 2B × 2 段)
#define PLAY_RING_SAMPLES   (2400 * 2)
// 采集块 (沿用固件 125ms @16kHz)
#define CAP_CHUNK_SAMPLES   (2000)
#define CAP_SAMPLE_RATE     (16000)
#define PLAY_SAMPLE_RATE    (24000)

/** 采集回调: AEC 处理后的干净 PCM (16kHz), 由采集任务在独立上下文调用 */
typedef void (*AudioCapturedCb)(const int16_t* pcm, size_t samples, void* ctx);

/** 状态变化回调 (打断/静音检测, 供状态机使用) */
typedef void (*SpeechStateCb)(bool speech_active, void* ctx);

class RealtimeAudioStream {
public:
    RealtimeAudioStream();
    ~RealtimeAudioStream();

    /**
     * @brief 初始化音频流
     * @param capturedCb 采集回调 (AEC 处理后调用)
     * @param speechCb   VAD 状态回调 (人声开始/结束)
     * @param ctx        回调用户参数
     * @param aecCfg     AEC 参数 (nullptr 用默认)
     */
    bool begin(AudioCapturedCb capturedCb, SpeechStateCb speechCb,
               void* ctx, const AecConfig* aecCfg = nullptr);

    /** @brief 启动采集任务 (FreeRTOS, 优先级高于网络任务) */
    void startCapture();

    /** @brief 停止采集任务 */
    void stopCapture();

    /**
     * @brief 提交播放数据 (替代原 streamAudioDelta 的阻塞播放)
     *
     * 先注入 AEC 参考信号, 再入播放环形缓冲, 播放任务异步消费。
     * 不等待 Speaker.isPlaying(), 因此不阻塞音频流。
     *
     * @param pcm   int16 PCM
     * @param samples 采样数
     * @param sample_rate 采样率 (24kHz)
     */
    void submitPlayback(const int16_t* pcm, size_t samples, int sample_rate);

    /** @brief 立即停止播放 + 清空缓冲 (打断用) */
    void stopPlayback();

    /** @brief 播放缓冲是否为空 */
    bool isPlaybackIdle() const;

    /** @brief 当前播放缓冲占用 (samples), 用于拥塞检测 */
    size_t playbackBuffered() const;

    /** @brief AEC 模块引用 (调试/统计) */
    RealtimeAEC& aec() { return _aec; }

private:
    // 采集任务入口 (静态 → this)
    static void _captureTaskEntry(void* arg);
    void _captureLoop();

    // 播放任务入口
    static void _playTaskEntry(void* arg);
    void _playLoop();

    // 采集块 → AEC 帧 (2000 samples → 7×256 + 余 208)
    void _processCaptureChunk(int16_t* chunk, size_t samples);

private:
    RealtimeAEC  _aec;

    // 回调
    AudioCapturedCb _capturedCb;
    SpeechStateCb   _speechCb;
    void*           _cbCtx;

    // 任务句柄
    TaskHandle_t _captureTaskH;
    TaskHandle_t _playTaskH;

    // 播放环形缓冲 (静态分配, ~10KB)
    int16_t  _playRing[PLAY_RING_SAMPLES];
    volatile uint32_t _ringHead;   // 写位置 (播放提交)
    volatile uint32_t _ringTail;   // 读位置 (播放消费)
    volatile bool     _playRunning;

    // 采集状态
    volatile bool _captureRunning;
    int16_t* _chunkBuf;             // 2000 samples 采集块
    int16_t  _frameCarry[AEC_FRAME_SAMPLES];  // 跨块帧余量 (最大 AEC_FRAME_SAMPLES-1)
    size_t   _frameCarryLen;

    // VAD 状态 (回调去抖)
    bool _lastSpeechState;
};

#endif // _REALTIME_AUDIO_STREAM_H
