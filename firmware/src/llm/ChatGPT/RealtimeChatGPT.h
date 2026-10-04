#if defined(REALTIME_API)

#ifndef _REALTIME_CHAT_GPT_H
#define _REALTIME_CHAT_GPT_H

#include <Arduino.h>
#include <M5Unified.h>
#include "StackchanExConfig.h"
#include "SpiRamJsonDocument.h"
#include "../ChatHistory.h"
#include "../RealtimeLLMBase.h"
#include "ChatGPT.h"
#include <WebSocketsClient.h>
#ifdef REALTIME_FULL_DUPLEX
#include "../RealtimeAEC.h"      // 回声消除 (CoreS3 duplex 方案)
#endif


class RealtimeChatGPT: public RealtimeLLMBase{
public:   //本当はprivateにしたいところだがコールバック関数にthisポインタを渡して使うためにpublicとした
    MCPClient* mcpClient[LLM_N_MCP_SERVERS_MAX];
    FunctionCall* fnCall;
    
    String role;
    String userInfo;
    String systemRole;

#ifdef REALTIME_FULL_DUPLEX
    // ==================================================================
    // 全双工 (CoreS3 duplex I2S 方案, 已在 test_duplex 验证):
    //   M5.Speaker.begin() 安装 TX+RX 同一 I2S 实例 (patched M5Unified)
    //   spk_cfg: pin_mck=0, sample_rate=16000, dma 4×128
    //   patch_rx_path(16000) 重建 RX 时钟 → TX/RX 统一 16kHz mono
    //   采集任务: i2s_read → AEC → 上传队列
    //   播放任务: 环形缓冲 → pushReference + i2s_write
    // ==================================================================
    RealtimeAEC fd_aec;                       // 回声消除
    bool        fd_ready;                     // duplex I2S 就绪

    // --- 采集 (录音任务) ---
    volatile bool fd_capturing;
    int16_t       fd_cap_chunk[1600];         // 100ms 采集块 @16k
    int16_t       fd_up_buf[2][RT_REC_LENGTH];// 上传双缓冲 (A/B, 2000 samples/块)
    volatile int  fd_up_idx;                  // 采集写入索引
    volatile int  fd_up_len;                  // 当前写入长度 (samples)
    volatile bool fd_up_ready[2];             // 待发标记
    int16_t       fd_pend_buf[AEC_FRAME_SAMPLES]; // 拼帧缓存 (凑满 20ms 帧再处理)
    volatile size_t fd_pend_len;              // 拼帧缓存中样本数 (0..320)

    // --- 播放 (环形缓冲 + 播放任务) ---
    volatile bool fd_playing;
    static constexpr size_t FD_RING_FRAMES = 16000;  // 1s @16k 播放环 (容量余量抗服务端突发)
    int16_t       fd_ring[FD_RING_FRAMES];
    volatile uint32_t fd_ring_head;
    volatile uint32_t fd_ring_tail;

    // 播放诊断统计 (每 2 秒串口输出)
    volatile uint32_t fd_stat_t;
    volatile uint32_t fd_stat_bytes;
    volatile uint32_t fd_stat_write_ms;
    volatile uint32_t fd_stat_cap_bytes;   // 窗口内采集字节数
    volatile uint32_t fd_stat_flush;       // 窗口内 flush 调用次数
    volatile uint32_t fd_stat_try;         // 窗口内待发块命中次数
    volatile uint32_t fd_stat_ok;          // 窗口内 sendTXT 成功次数
    volatile uint32_t fd_stat_fail;        // 窗口内 sendTXT 失败次数
#ifdef AEC_DIAG
    // AEC 诊断 (diag 环境): 真实场景 AEC 前/后能量对比
    double   fd_diag_in2 = 0.0;            // AEC 前平方和
    double   fd_diag_out2 = 0.0;           // AEC 后平方和
    uint32_t fd_diag_n = 0;                // 样本数
#endif

    static void fd_audio_task(void* arg);      // 单任务: 交替读/写 (无并发问题)
    size_t _fd_ring_peek(int16_t* out, size_t max_frames);   // 窥视 (不推进 tail)
    void   _fd_ring_advance(size_t frames);                  // 消费已写入帧数
    void   _fd_ring_write(const int16_t* pcm, size_t frames);
    size_t _fd_ring_read(int16_t* out, size_t max_frames);
    void   _fd_upload_flush();                // webSocketLoopTask 上下文发送
#endif

public:
    RealtimeChatGPT(llm_param_t param);

    virtual void chat(String text, const char *base64_buf = NULL) {};   //dummy
    virtual String& buildInputAudioJson(String& jsonBuf, String& base64);
    virtual void load_role();
    virtual void streamAudioDelta(String& delta);

#ifdef REALTIME_FULL_DUPLEX
    virtual void onRecordingStart();
    virtual void onRecordingStop();
    virtual void onWsTick();
#endif
};


#endif  //_REALTIME_CHAT_GPT_H

#endif  //REALTIME_API