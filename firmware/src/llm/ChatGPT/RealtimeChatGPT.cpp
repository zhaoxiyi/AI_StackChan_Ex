#if defined(REALTIME_API)

#include <Arduino.h>
#include <M5Unified.h>
#include <Avatar.h>
#include "share/Mutex.h"
//#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include "rootCA/rootCACertificate.h"
#include <ArduinoJson.h>
#include "SpiRamJsonDocument.h"
#include "RealtimeChatGPT.h"
#include "FunctionCall.h"
#include "MCPClient.h"
#include "Robot.h"

#include <base64.h>
#include "libb64/cdecode.h"
#include <WebSocketsClient.h>
#include "share/SDUtil.h"   // read_sd_file: 读取上位机地址配置
#ifdef REALTIME_FULL_DUPLEX
#include <driver/i2s.h>
#include <soc/i2s_struct.h>   // RX 时钟重建 (对齐 M5Unified S3 处理)

static portMUX_TYPE fd_ring_mux = portMUX_INITIALIZER_UNLOCKED;   // 播放环跨核锁 (core0 写 / core1 读)
#endif

using namespace m5avatar;
extern Avatar avatar;

static const char session_update[] =
      "{"
        "\"type\": \"session.update\","
        "\"session\": {"
          "\"type\": \"realtime\","
          "\"model\": \"gpt-realtime\","
#ifdef REALTIME_API_WITH_TTS
          "\"output_modalities\": [\"text\"],"
#else
          "\"output_modalities\": [\"audio\"],"
#endif
          "\"audio\": {"
            "\"input\": {"
              "\"format\": {"
                "\"type\": \"audio/pcm\","
                "\"rate\": 16000"
              "},"
              "\"turn_detection\": {"
                "\"type\": \"semantic_vad\""
              "}"
            "},"
            "\"output\": {"
              "\"format\": {"
                "\"type\": \"audio/pcm\","
                "\"rate\": 16000"
              "},"
              //"\"voice\": \"sage\""
              "\"voice\": \"marin\""
            "}"
          "},"
          "\"instructions\": \"You are an AI robot named Stack-chan. Please speak in Japanese.\","
          "\"tools\":[]"
        "}"
      "}";


static const char input_audio_append[] =
        "{"
          "\"type\": \"input_audio_buffer.append\","
          "\"audio\": \"REPLACE_TO_AUDIO_BASE64\""
        "}";

// for function calling
//
static const char conversation_item_create[] =
        "{"
            "\"type\": \"conversation.item.create\","
            "\"item\": {"
                "\"type\": \"function_call_output\","
                "\"call_id\": \"REPLACE_TO_CALL_ID\","
                "\"output\": \"{\\\"result\\\":\\\"REPLACE_TO_OUTPUT\\\"}\""
            "}"
        "}";

static const char response_create[] =
        "{"
            "\"type\": \"response.create\""
        "}";

// WebSocketのコールバック関数としてクラスメソッドを渡せないので、コールバック関数を
// 通常の関数にして静的変数を経由してクラスのthisポインタを渡す。
static RealtimeChatGPT* p_this;

#ifdef REALTIME_FULL_DUPLEX
static bool fd_init_duplex();   // 定义在文件后部 (全双工实现块)
#endif
static void webSocketEvent(WStype_t type, uint8_t * payload, size_t length) {
    String msgType, delta;
    DeserializationError error;

	switch(type) {
		case WStype_DISCONNECTED:
			Serial.printf("[WSc] Disconnected!\n");
			break;
		case WStype_CONNECTED:
			Serial.printf("[WSc] Connected to url: %s\n", payload);

            /*
             * session.updateでAPIの振る舞いをカスタマイズする
             */
            {
                SpiRamJsonDocument sessionUpdateDoc(1024*10);
                DeserializationError error = deserializeJson(sessionUpdateDoc, session_update);
                if (error) {
                    Serial.println("webSocketEvent: JSON deserialization error (session_update)");
                }

                // instructionsにロール、前回会話の要約を設定
                //
                Serial.printf("role: %s\n", p_this->role.c_str());
                Serial.printf("sysRole: %s\n", p_this->systemRole.c_str());
                Serial.printf("userInfo: %s\n", p_this->userInfo.c_str());
                sessionUpdateDoc["session"]["instructions"] = p_this->role + " "
                                                              + p_this->systemRole + " "
                                                              + p_this->userInfo;

                // MCP tools listをfunctionとして挿入
                //
                for(int s=0; s < p_this->param.llm_conf.nMcpServers; s++){
                    if(true == p_this->param.llm_conf.mcpServer[s].disabled){
                        continue;
                    }
                    if(!p_this->mcpClient[s]->isConnected()){
                        continue;
                    }

                    for(int t=0; t < p_this->mcpClient[s]->nTools; t++){
                        sessionUpdateDoc["session"]["tools"].add(p_this->mcpClient[s]->toolsListDoc["result"]["tools"][t]);
                        sessionUpdateDoc["session"]["tools"][t]["type"] = "function";
                    }
                }

                // FunctionCall.cppで定義したfunctionをsession.updateに挿入
                //
                SpiRamJsonDocument functionsDoc(1024*10);
                error = deserializeJson(functionsDoc, json_Functions.c_str());
                if (error) {
                    Serial.println("FunctionCall: JSON deserialization error");
                }

                int nFuncs = functionsDoc.size();
                int nMcpFuncs = sessionUpdateDoc["session"]["tools"].size();
                for(int i=0; i<nFuncs; i++){
                    sessionUpdateDoc["session"]["tools"].add(functionsDoc[i]);
                    sessionUpdateDoc["session"]["tools"][nMcpFuncs + i]["type"] = "function";
                }

                String sessionUpdateStr;
                serializeJson(sessionUpdateDoc, sessionUpdateStr);
                String jsonPretty;
                serializeJsonPretty(sessionUpdateDoc, jsonPretty);
                Serial.printf("[WSc] session update json: %s\n", jsonPretty.c_str());
                p_this->webSocket.sendTXT(sessionUpdateStr.c_str());
            }
			break;
		case WStype_TEXT:
			//Serial.printf("[WSc] get text: %s\n", payload);

            error = deserializeJson(p_this->msgDoc, payload);
            if (error) {
                Serial.printf("WebSocket Event: JSON deserialization error %d\n", error.code());
            }

            msgType = p_this->msgDoc["type"].as<String>();
            // 音频 delta 每片到达 (一句回复 ~100 片), 逐片打印会阻塞 USB CDC →
            // 不开监视器时拖慢 WebSocket 收包与播放。仅打印非音频事件。
            if (!msgType.equals("response.output_audio.delta")) {
                Serial.printf("[WSc] text size: %d type: %s\n", (int)strlen((char*)payload), msgType.c_str());
            }

            if(msgType.equals("session.updated")){
                Serial.printf("[WSc] payload: %s\n", payload);
                avatar.setSpeechText("Please touch");
            }
            else if(msgType.equals("input_audio_buffer.speech_started")){
                p_this->resetRealtimeRecordStartTime();
            }
            else if(msgType.equals("input_audio_buffer.committed")){
                Serial.printf("[WSc] input audio committed\n");
#ifdef REALTIME_FULL_DUPLEX
                // 全双工: 录音不停止 (采集任务持续运行, 边播边听),
                // 清空播放环 — 打断时旧回复的尾音立即停止
                p_this->fd_ring_head = p_this->fd_ring_tail;
                p_this->speaking = true;
                p_this->resetRealtimeRecordStartTime();
#else
                p_this->stopRealtimeRecord();
#ifndef REALTIME_API_WITH_TTS
                enterMutexAudio();
                M5.Mic.end();
                M5.Speaker.begin();
                p_this->speaking = true;
#else
                p_this->speaking = true;
#endif
#endif
            }
#ifndef REALTIME_API_WITH_TTS            
            else if(msgType.equals("response.output_audio_transcript.delta")){
                delta = p_this->msgDoc["delta"].as<String>();
                Serial.printf("[WSc] delta: %s\n", delta.c_str());
            }
            else if(msgType.equals("response.output_audio.delta")){
                delta = p_this->msgDoc["delta"].as<String>();
                p_this->streamAudioDelta(delta);
            }
#else
            else if(msgType.equals("response.output_text.delta")){
                p_this->outputText += p_this->msgDoc["delta"].as<String>();

                // 区切り文字を検出したらテキストをキューに追加
                int idx = p_this->search_delimiter(p_this->outputText);
                if(idx > 0){
                    String inputText = p_this->outputText.substring(0, idx);
                    Serial.printf("[WSc] Push text: %s\n", inputText.c_str());
                    p_this->outputTextQueue.push_back(inputText);
                    p_this->outputText = p_this->outputText.substring(idx + strlen("。"), p_this->outputText.length());
                }
            }
#endif
            else if(msgType.equals("response.done")){
                Serial.printf("[WSc] response.done payload: %s\n", payload);
                int outputNum = p_this->msgDoc["response"]["output"].size();
                Serial.printf("output num: %d\n", outputNum);
                bool isFuncCall = false;
                for(int i = 0; i < outputNum; i++){
                    String outputType = p_this->msgDoc["response"]["output"][i]["type"].as<String>();
                    if(outputType.equals("function_call")){
                        //Serial.printf("[WSc] function call payload: %s\n", payload);
                        isFuncCall = true;
                        const char* name = p_this->msgDoc["response"]["output"][i]["name"];
                        const char* args = p_this->msgDoc["response"]["output"][i]["arguments"];
                        const char* call_id = p_this->msgDoc["response"]["output"][i]["call_id"];
                        Serial.printf("name: %s, args: %s\n", name, args);

                        //avatar.setSpeechFont(&fonts::efontJA_12);
                        //avatar.setSpeechText(name);
                        String response = p_this->fnCall->exec_calledFunc(name, args);
                        response.replace("\"", "\\\"");     //JSON内の文字列を囲む"にエスケープ(\)を付ける

                        String json(conversation_item_create);
                        json.replace("REPLACE_TO_CALL_ID", call_id);
                        json.replace("REPLACE_TO_OUTPUT", response.c_str());
                        Serial.printf("[WSc] function output: %s\n", json.c_str());
                        p_this->webSocket.sendTXT(json);
                        p_this->webSocket.sendTXT(response_create);
                    }
                }

                if(!isFuncCall){
#ifdef REALTIME_FULL_DUPLEX
                    // 全双工: Mic/Speaker 保持工作, 无设备切换;
                    // 环形缓冲中的尾音自然播完, 采集持续上传
                    p_this->speaking = false;
#else
#ifndef REALTIME_API_WITH_TTS
                    while (M5.Speaker.isPlaying()) { vTaskDelay(1); }
                    M5.Speaker.end();
                    M5.Mic.begin();
                    exitMutexAudio();
                    p_this->startRealtimeRecord();

                    for(int i=0; i<2; i++){
                        memset(p_this->audioBuf[i], 0, 100 * 1024);
                    }
                    p_this->speaking = false;
#else
                    p_this->response_done = true;
#endif
#endif
                }
            }
            else if(msgType.equals("rate_limits.updated")){
                //Serial.printf("[WSc] payload: %s\n", payload);
            }
            else if(msgType.equals("error")){
                Serial.printf("[WSc] payload: %s\n", payload);
            }

			break;
		case WStype_BIN:
			Serial.printf("[WSc] get binary length: %u\n", length);
			p_this->hexdump(payload, length);
			break;
		case WStype_ERROR:
		case WStype_FRAGMENT_TEXT_START:
		case WStype_FRAGMENT_BIN_START:
		case WStype_FRAGMENT:
		case WStype_FRAGMENT_FIN:
 			Serial.printf("[WSc] payload: %s\n", payload);		
            break;
        default:
			Serial.printf("[WSc] Unknown event\n");
            //Serial.printf("[WSc] payload: %s\n", payload);
            break;
	}

}


RealtimeChatGPT::RealtimeChatGPT(llm_param_t param)
  : RealtimeLLMBase(param),
    role(""),
    userInfo("User Info: "),
    systemRole("")
#ifdef REALTIME_FULL_DUPLEX
    , fd_ready(false)
    , fd_capturing(false)
    , fd_up_idx(0)
    , fd_up_len(0)
    , fd_up_ready{false, false}
    , fd_pend_len(0)
    , fd_playing(false)
    , fd_ring_head(0)
    , fd_ring_tail(0)
    , fd_stat_t(0)
    , fd_stat_bytes(0)
    , fd_stat_write_ms(0)
    , fd_stat_cap_bytes(0)
    , fd_stat_flush(0)
    , fd_stat_try(0)
    , fd_stat_ok(0)
    , fd_stat_fail(0)
#endif
{
  p_this = this;    //コールバック関数に静的変数経由でthisポインタを渡す
  msgDoc = SpiRamJsonDocument(1024*150);
  
  initMcpClientList(mcpClient, param.llm_conf.mcpServer, param.llm_conf.nMcpServers);
  fnCall = new FunctionCall(param, this, mcpClient);
  //fnCall->init_func_call_settings(robot->m_config);

  enableMemory(param.llm_conf.enableMemory);
  if(enableMemory()){
    Serial.println("Memory is enabled");
    M5.Lcd.println("Memory is enabled");
  }
  load_role();


  // WebSocket connect
  //
  avatar.setSpeechText("Connecting...");

  // 本地上位机 (HTTP) 连接地址:
  //   从 SD 卡 /yaml/SC_SecConfig.yaml 的 realtimeServer 节读取:
  //     realtimeServer:
  //       host: "192.168.1.100"
  //       port: 8080
  //   读取失败时使用默认值 192.168.1.100:8080
  // 上位机服务端是明文 ws:// 协议, 不再使用 SSL 连接 api.openai.com
  String rtHost = "192.168.1.100";
  uint16_t rtPort = 8080;
  {
    char buf[2048] = {0};
    if (read_sd_file("/yaml/SC_SecConfig.yaml", buf, sizeof(buf)) > 0) {
      // 极简 YAML 解析: 定位 realtimeServer 节, 提取 host/port
      const char* p = strstr(buf, "realtimeServer:");
      if (p) {
        const char* h = strstr(p, "host:");
        if (h) {
          const char* q1 = strchr(h, '"');
          const char* q2 = q1 ? strchr(q1 + 1, '"') : nullptr;
          if (q1 && q2 && (q2 - q1) > 1) {
            rtHost = String(h).substring(q1 - h + 1, q2 - h);
          }
        }
        const char* pt = strstr(p, "port:");
        if (pt) {
          int prt = atoi(pt + 5);
          if (prt > 0 && prt < 65536) rtPort = (uint16_t)prt;
        }
      }
    }
  }
  Serial.printf("[RT] 上位机: ws://%s:%u/v1/realtime?model=gpt-realtime\n",
                rtHost.c_str(), rtPort);
  webSocket.begin(rtHost.c_str(), rtPort, "/v1/realtime?model=gpt-realtime");
  // 原云端 SSL 连接 (保留注释, 如需切回云端取消注释):
  // webSocket.beginSslWithCA("api.openai.com", 443, "/v1/realtime?model=gpt-realtime", root_ca_openai);

  // event handler
  p_this = this;    //コールバック関数に静的変数経由でthisポインタを渡す
  webSocket.onEvent(webSocketEvent);
  String auth = "Bearer " + param.api_key;
  webSocket.setAuthorization(auth.c_str());

  // try ever 5000 again if connection has failed
  webSocket.setReconnectInterval(5000);

#ifdef REALTIME_FULL_DUPLEX
  // 全双工 (CoreS3 duplex I2S): TX/RX 统一 16kHz mono
  // spk_cfg 三个关键配置 + RX 路径重建, 方案已在 test_duplex 工程验证
  fd_ready = fd_init_duplex();
  if (fd_ready) {
    Serial.println("[FD] duplex I2S ready (16kHz mono, TX+RX)");
  } else {
    Serial.println("[FD] duplex init FAILED - fallback half-duplex");
  }
#endif

}


void RealtimeChatGPT::load_role(){
  Serial.println("Load role from SPIFFS.");
  if(enableMemory()){
    systemRole = systemRole_memory;
  }else{
    systemRole = systemRole_noMemory;
  }
  systemRole += " " + systemRole_realtimeAvatarExpression;

  if(load_system_prompt_from_spiffs()){
    role = String((const char*)systemPrompt["messages"][SYSTEM_PROMPT_INDEX_USER_ROLE]["content"]);
    //Serial.printf("role length: %d\n", role.length());
    if (role == "") {
      Serial.println("SPIFFS user role is empty. set default role.");
      role = defaultRole;
    }

    userInfo = String((const char*)systemPrompt["messages"][SYSTEM_PROMPT_INDEX_USER_INFO]["content"]);
    //Serial.println(userInfo);
    int idx = userInfo.indexOf("User Info");
    if(idx < 0 || !enableMemory()){
      userInfo = "User Info: ";
    }
  }else{
    // load_system_prompt_from_spiffs()内でSPIFFSからの取得失敗かつ
    // デフォルトのシステムプロンプト設定に失敗した場合（通常起こり得ない）。
    role = defaultRole;
    userInfo = "User Info: ";
  }
}

String& RealtimeChatGPT::buildInputAudioJson(String& jsonBuf, String& base64)
{
    jsonBuf.concat(input_audio_append);
    jsonBuf.replace("REPLACE_TO_AUDIO_BASE64", base64);
    //Serial.println(jsonBuf);
    return jsonBuf;
}

#ifdef REALTIME_FULL_DUPLEX
// =============================================================================
// 全双工实现 — CoreS3 duplex I2S (方案已在 test_duplex 工程逐项验证)
//   M5.Speaker.begin() 安装 TX+RX 同一 I2S 实例 (patched M5Unified)
//   spk_cfg: pin_mck=0 / sample_rate=16000 / dma 4×128
//   patch_rx_path: RX 时钟重建 → TX/RX 统一 16kHz mono
// =============================================================================

#define FD_I2S_PORT  I2S_NUM_1
static SemaphoreHandle_t fd_i2s_mtx = nullptr;   // I2S 调用互斥 (防 IDF 驱动读写并发死锁)

/** M5Unified calcClockDiv 复制 (S3 时钟分数分频) */
static void fd_calcClockDiv(uint32_t* div_a, uint32_t* div_b, uint32_t* div_n,
                            uint32_t baseClock, uint32_t targetFreq)
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

/** RX 路径重建 (复刻 M5Unified Mic_Class 的 S3 RX 配置: TDM + bck_div + PLL_240M) */
static void fd_patch_rx_path(int fs)
{
    constexpr uint32_t PLL_D2_CLK = 120 * 1000 * 1000;
    const uint32_t bits = 16;
    const uint32_t div_m = 8;                     // CoreS3 mic 精度要求 (M5Unified 注释)
    uint32_t div_a, div_b, div_n;
    fd_calcClockDiv(&div_a, &div_b, &div_n, PLL_D2_CLK, div_m * bits * (uint32_t)fs);

    i2s_dev_t* dev = &I2S1;
    dev->rx_conf.rx_pdm_en = 0;
    dev->rx_conf.rx_tdm_en = 1;
    dev->rx_conf.rx_update = 1;
    dev->rx_conf1.rx_bck_div_num = div_m - 1;

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
    dev->rx_clkm_conf.rx_clk_sel = 1;             // PLL_240M_CLK
    dev->tx_clkm_conf.clk_en = 1;
    dev->rx_clkm_conf.rx_clk_active = 1;
    dev->rx_conf.rx_update = 1;
    dev->rx_conf.rx_update = 0;
    Serial.printf("[FD] rx clock patched: fs=%d div_n=%u div_a=%u div_b=%u\n",
                  fs, (unsigned)div_n, (unsigned)div_a, (unsigned)div_b);
}

/** ES7210 麦克风阵列初始化 (照抄 M5Unified 验证序列; 全双工下主动配置) */
static void fd_es7210_init(void)
{
    const uint8_t ES7210 = 0x40;
    auto wr = [&](uint8_t reg, uint8_t val) {
        M5.In_I2C.writeRegister(ES7210, reg, &val, 1, 400000);
    };
    wr(0x00, 0xFF);   // reset
    struct __attribute__((packed)) { uint8_t r; uint8_t v; } seq[] = {
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
    for (auto& d : seq) { wr(d.r, d.v); }
    Serial.println("[FD] ES7210 initialized (MIC1/2, gain 0x1B)");
}

/** duplex I2S 初始化 (构造函数调用) */
static bool fd_init_duplex()
{
    auto spk_cfg = M5.Speaker.config();
    spk_cfg.pin_mck = (gpio_num_t)0;        // ES7210 MCLK
    spk_cfg.sample_rate = 16000;            // ★ spk_task 按此重建 TX 时钟 (默认 48k 会让播放快 3 倍)
    // DMA 保持 M5Unified 默认 (8×256 帧 = 128ms): 深缓冲抗调度抖动, 播放不断续
    // (回声延迟由播放任务的参考延迟补偿解决, 见 fd_play_task)
    M5.Speaker.config(spk_cfg);

    if (!M5.Speaker.begin()) {
        Serial.println("[FD] M5.Speaker.begin failed");
        return false;
    }
    fd_patch_rx_path(16000);
    // 注: 不主动初始化 ES7210 (出厂默认配置与 mono 读取兼容)
    fd_i2s_mtx = xSemaphoreCreateMutex();   // I2S 调用互斥

    // AW88298 音量覆盖: M5Unified cb 默认 full (0x64), 近场回声会饱和 → 降至 0x20
    uint16_t vol = 0x0020;
    M5.In_I2C.writeRegister(0x36, 0x0C, (const uint8_t*)&vol, 2, 400000);
    return true;
}

// --- 播放环形缓冲 ---
void RealtimeChatGPT::_fd_ring_write(const int16_t* pcm, size_t frames)
{
    portENTER_CRITICAL(&fd_ring_mux);
    for (size_t i = 0; i < frames; i++) {
        uint32_t next = (fd_ring_head + 1) % FD_RING_FRAMES;
        if (next == fd_ring_tail) {
            fd_ring_tail = (fd_ring_tail + 1) % FD_RING_FRAMES;   // 满: 丢最旧
        }
        fd_ring[fd_ring_head] = pcm[i];
        fd_ring_head = next;
    }
    portEXIT_CRITICAL(&fd_ring_mux);
}

size_t RealtimeChatGPT::_fd_ring_read(int16_t* out, size_t max_frames)
{
    size_t n = 0;
    while (n < max_frames && fd_ring_tail != fd_ring_head) {
        out[n++] = fd_ring[fd_ring_tail];
        fd_ring_tail = (fd_ring_tail + 1) % FD_RING_FRAMES;
    }
    return n;
}

// --- 环形缓冲: 窥视 (不推进 tail) / 消费 (推进 tail) ---
size_t RealtimeChatGPT::_fd_ring_peek(int16_t* out, size_t max_frames)
{
    size_t n = 0;
    portENTER_CRITICAL(&fd_ring_mux);
    uint32_t t = fd_ring_tail;
    while (n < max_frames && t != fd_ring_head) {
        out[n++] = fd_ring[t];
        t = (t + 1) % FD_RING_FRAMES;
    }
    portEXIT_CRITICAL(&fd_ring_mux);
    return n;
}

void RealtimeChatGPT::_fd_ring_advance(size_t frames)
{
    portENTER_CRITICAL(&fd_ring_mux);
    fd_ring_tail = (fd_ring_tail + frames) % FD_RING_FRAMES;
    portEXIT_CRITICAL(&fd_ring_mux);
}

// --- 单音频任务 (core1): 交替播放/采集, 短超时, 无跨任务 I2S 并发 ---
// 设计: 之前的双任务架构存在 IDF 驱动读写并发问题 (无锁卡死 / 有锁断流),
// 单任务串行化调用彻底消除; 短超时 + 部分写入消费保证两边都不长时间阻塞。
void RealtimeChatGPT::fd_audio_task(void* arg)
{
    RealtimeChatGPT* self = (RealtimeChatGPT*)arg;
    static int16_t play_buf[800];
    static int16_t ref_out[800];
    static int16_t cap_buf[1600];
    static int16_t ref_delay_line[2048];     // 参考延迟线 (DMA 128ms 补偿)
    static size_t  ref_pos = 0;

    while (self->fd_playing) {
        // ---- 1) 播放 (短超时; 写多少消费多少) ----
        size_t n = self->_fd_ring_peek(play_buf, 800);
        if (n > 0) {
            size_t w = 0;
            uint32_t t_w = millis();
            i2s_write(FD_I2S_PORT, play_buf, n * 2, &w, pdMS_TO_TICKS(5));
            size_t written = (w & ~(size_t)1) / 2;      // 实际写入帧数 (取偶)
            if (written > 0) {
                // 参考延迟补偿: 延迟 2048 帧(128ms) 对齐 DMA 出声时刻;
                // 只推送实际写入的数据, 参考与输出严格一致 (AEC 收敛前提)
                for (size_t i = 0; i < written; i++) {
                    int16_t old = ref_delay_line[ref_pos];
                    ref_delay_line[ref_pos] = play_buf[i];
                    ref_pos = (ref_pos + 1) % 2048;
                    ref_out[i] = old;
                }
                self->fd_aec.pushReference(ref_out, written, 16000);
            }
            self->_fd_ring_advance(written);            // 只消费已写入部分
            self->fd_stat_bytes += w;
            self->fd_stat_write_ms += millis() - t_w;
        }

        // ---- 2) 采集 (短超时; 累加凑帧: 每轮读取量不定, 不足 20ms 帧时缓存待拼) ----
        if (self->fd_capturing) {
            size_t rr = 0;
            i2s_read(FD_I2S_PORT, cap_buf, sizeof(cap_buf), &rr, pdMS_TO_TICKS(5));
            self->fd_stat_cap_bytes += rr;              // 采集诊断 (每 2 秒报)
            if (rr > 0) {
                size_t frames = rr / 2;
                size_t off = 0;
                while (off < frames) {
                    // 先填满拼帧缓存 (AEC 固定 320 样本/帧)
                    size_t need = AEC_FRAME_SAMPLES - self->fd_pend_len;
                    size_t take = (frames - off < need) ? (frames - off) : need;
                    memcpy(&self->fd_pend_buf[self->fd_pend_len],
                           cap_buf + off, take * sizeof(int16_t));
                    self->fd_pend_len += take;
                    off += take;
                    if (self->fd_pend_len < AEC_FRAME_SAMPLES) break;   // 未凑够, 等下次读取

                    // ---- 完整 20ms 帧: AEC 处理 → 写入上传缓冲 ----
                    bool hs = false;
#ifdef AEC_DIAG
                    for (int k = 0; k < AEC_FRAME_SAMPLES; k++)
                        self->fd_diag_in2 += (double)self->fd_pend_buf[k] * self->fd_pend_buf[k];
                    self->fd_diag_n += AEC_FRAME_SAMPLES;
#endif
                    self->fd_aec.processFrame(self->fd_pend_buf, &hs);
#ifdef AEC_DIAG
                    for (int k = 0; k < AEC_FRAME_SAMPLES; k++)
                        self->fd_diag_out2 += (double)self->fd_pend_buf[k] * self->fd_pend_buf[k];
#endif
                    size_t src = 0;
                    while (src < AEC_FRAME_SAMPLES) {
                        int idx = self->fd_up_idx;
                        size_t room = (size_t)RT_REC_LENGTH - self->fd_up_len;
                        size_t take2 = (AEC_FRAME_SAMPLES - src < room)
                                     ? (AEC_FRAME_SAMPLES - src) : room;
                        memcpy(&self->fd_up_buf[idx][self->fd_up_len],
                               self->fd_pend_buf + src, take2 * sizeof(int16_t));
                        self->fd_up_len += take2;
                        src += take2;
                        if (self->fd_up_len >= RT_REC_LENGTH) {
                            self->fd_up_ready[idx] = true;
                            self->fd_up_idx = idx ^ 1;      // 换缓冲 (旧块留给 wsTick)
                            self->fd_up_len = 0;
                        }
                    }
                    self->fd_pend_len = 0;
                }
            }
        }

        // ---- 3) 统计 (每 2 秒) ----
        uint32_t now = millis();
        if (now - self->fd_stat_t >= 2000) {
            size_t av = (self->fd_ring_head >= self->fd_ring_tail)
                      ? (self->fd_ring_head - self->fd_ring_tail)
                      : (FD_RING_FRAMES - self->fd_ring_tail + self->fd_ring_head);
            Serial.printf("[FD] play2s: ring=%ufr wrote=%uB w_ms=%ums cap=%uB flush=%u try=%u ok=%u fail=%u\n",
                          (unsigned)av, (unsigned)self->fd_stat_bytes,
                          (unsigned)self->fd_stat_write_ms,
                          (unsigned)self->fd_stat_cap_bytes,
                          (unsigned)self->fd_stat_flush,
                          (unsigned)self->fd_stat_try,
                          (unsigned)self->fd_stat_ok,
                          (unsigned)self->fd_stat_fail);
            self->fd_stat_t = now;
            self->fd_stat_bytes = 0;
            self->fd_stat_write_ms = 0;
            self->fd_stat_cap_bytes = 0;
            self->fd_stat_flush = 0;
            self->fd_stat_try = 0;
            self->fd_stat_ok = 0;
            self->fd_stat_fail = 0;
#ifdef AEC_DIAG
            if (self->fd_diag_n > 0) {
                float in_rms  = sqrtf((float)(self->fd_diag_in2 / self->fd_diag_n));
                float out_rms = sqrtf((float)(self->fd_diag_out2 / self->fd_diag_n));
                Serial.printf("[FD] aec2s: in=%6.0f out=%6.0f atten=%6.1fdB  (in>1000 即播报期回声, 看此时衰减)\n",
                              in_rms, out_rms,
                              20.0f * log10f((out_rms + 1e-3f) / (in_rms + 1e-3f)));
                self->fd_diag_in2 = 0.0;
                self->fd_diag_out2 = 0.0;
                self->fd_diag_n = 0;
            }
#endif
        }

        vTaskDelay(1);      // 让出 CPU (任务在 core1, 不与 WiFi 核竞争)
    }
    vTaskDelete(NULL);
}

// --- webSocketLoopTask 上下文: 发送积压采集数据 (sendTXT 单线程) ---
void RealtimeChatGPT::_fd_upload_flush()
{
    if (!fd_ready) return;
    fd_stat_flush++;
    for (int i = 0; i < 2; i++) {
        if (fd_up_ready[i]) {
            fd_up_ready[i] = false;
            fd_stat_try++;
            String b64 = base64::encode((u8*)fd_up_buf[i],
                                        RT_REC_LENGTH * sizeof(int16_t));
            String jsonBuf("");
            uint32_t t0 = millis();
            bool ok = webSocket.sendTXT(buildInputAudioJson(jsonBuf, b64));
            uint32_t dt = millis() - t0;
            if (ok) fd_stat_ok++; else fd_stat_fail++;
            if (dt > 50) {
                Serial.printf("[FD] tx slow: %ums b64=%uB ok=%d\n",
                              (unsigned)dt, (unsigned)b64.length(), (int)ok);
            }
        }
    }
}

void RealtimeChatGPT::onRecordingStart()
{
    if (!fd_ready) return;
    if (!fd_capturing) {
        fd_capturing = true;
        fd_up_idx = 0;
        fd_up_len = 0;
        fd_pend_len = 0;
        fd_up_ready[0] = fd_up_ready[1] = false;
        fd_ring_head = fd_ring_tail = 0;
        fd_aec.begin(nullptr);      // 会话开始 → AEC 初始化/复位
        Serial.println("[FD] capture start (single-task mode)");
    }
    if (!fd_playing) {
        fd_playing = true;
        // 单音频任务 (core1): 交替播放/采集, 无跨任务 I2S 并发
        xTaskCreatePinnedToCore(fd_audio_task, "fdAudio", 8192, this, 5, NULL, 1);
        Serial.println("[FD] audio task started");
    }
}

void RealtimeChatGPT::onRecordingStop()
{
    if (fd_capturing) {
        fd_capturing = false;       // 任务自删
        Serial.println("[FD] capture task stopped");
    }
}

void RealtimeChatGPT::onWsTick()
{
    _fd_upload_flush();
}
#endif  // REALTIME_FULL_DUPLEX

// 流式播放: 全双工时解码 PCM → 播放环 (非阻塞); 否则回退基类半双工阻塞播放
void RealtimeChatGPT::streamAudioDelta(String& delta)
{
#ifdef REALTIME_FULL_DUPLEX
    if (fd_ready) {
        int len = base64_decode(delta.c_str(), delta.length(), (char*)audioBuf[nextBufIdx]);
        if (len > 0) {
            _fd_ring_write((int16_t*)audioBuf[nextBufIdx], len / 2);
        }
        nextBufIdx ^= 1;
        return;
    }
#endif
    RealtimeLLMBase::streamAudioDelta(delta);
}


#endif  //REALTIME_API
