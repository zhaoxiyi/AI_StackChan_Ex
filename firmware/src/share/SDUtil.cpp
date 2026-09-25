
#include <Arduino.h>
#include <M5Unified.h>
#include <stdio.h>
#include <SD.h>
#include <SPIFFS.h>
#include "SDUtil.h"

int read_sd_file(const char* filename, char* buf, int buf_size)
{
  int ret;
  auto fs = SD.open(filename, FILE_READ);
  
  if(fs) {
    size_t sz = fs.size();
    if(sz >= buf_size){
      Serial.printf("SD file size (%d) exceeds buf size (%d).\n", sz, buf_size);
      ret = 0;
    }
    else{
      fs.read((uint8_t*)buf, sz);
      buf[sz] = 0;
      fs.close();
      ret = sz;
    }
  }
  else{
    Serial.println("Failed to open SD.");
    ret = 0;
  }
  return ret;
}


int read_line_from_buf(char* buf, char* out)
{
  static int n_total = 0;
  static char* buf_ = nullptr;
  int n, ret;

  if(buf != nullptr){
    buf_ = buf;
  }

  if(buf_ == nullptr){
    Serial.println("read_line_from_buf: buffer is nullptr.");
    return 0;
  }

  ret = sscanf(buf_, "%s%n", out, &n);
  buf_ += n;

  return ret;
}


// SDカードのファイルをSPIFFSにコピー
bool copySDFileToSPIFFS(const char *path, bool forced) {
  
  Serial.println("Copy SD File to SPIFFS.");

  if(!SPIFFS.begin(true)){
    Serial.println("Failed to mount SPIFFS.");
    return false;
  }

  if (SPIFFS.exists(path) && !forced) {
	  Serial.println("File already exists in SPIFFS.");
	  return true;
  }

  if (!SD.begin(GPIO_NUM_4, SPI, 25000000)) {
    Serial.println("Failed to mount SD.");
    return false;
  }

  File fsrc = SD.open(path, FILE_READ);
  File fdst = SPIFFS.open(path, FILE_WRITE);
  if(!fsrc || !fdst) {
    Serial.println("Failed to open file.");
    return false;
  }

  uint8_t *buf = new uint8_t[4096];
  if (!buf) {
	  Serial.println("Failed to allocate buffer.");
	  return false;
  }

  size_t len, size, ret;
  size = len = fsrc.size();
  while (len) {
	  size_t s = len;
	  if (s > 4096){
		  s = 4096;
    }  

	  fsrc.read(buf, s);
	  if ((ret = fdst.write(buf, s)) < s) {
		  Serial.printf("write failed: %d - %d\n", ret, s);
		  return false;
	  }
	  len -= s;
	  Serial.printf("%d / %d\n", size - len, size);
  }
 
  delete[] buf;
  fsrc.close();
  fdst.close();

  if (!SPIFFS.exists(path)) {
	  Serial.println("no file in SPIFFS.");
	  return false;
  }
  fdst = SPIFFS.open(path);
  len = fdst.size();
  fdst.close();
  if (len != size) {
	 Serial.println("size not match.");
	 return false;
  }
  Serial.println("*** Done. ***\r\n");
  return true;
}

// =============================================================================
// 强化 SD 卡挂载: 文件系统格式识别 + 多频率降级重试 + 详细诊断
// =============================================================================

const char* sdFsTypeToString(sd_fs_type_t t)
{
  switch (t) {
    case SD_FS_FAT12:  return "FAT12";
    case SD_FS_FAT16:  return "FAT16";
    case SD_FS_FAT32:  return "FAT32";
    case SD_FS_EXFAT:  return "exFAT";
    case SD_FS_NTFS:   return "NTFS (不支持, 请格式化为 FAT32)";
    case SD_FS_RAW:    return "RAW (无文件系统, 请格式化)";
    case SD_FS_NOT_PRESENT: return "无卡 (SPI 无响应)";
    case SD_FS_UNKNOWN:
    default:           return "未知";
  }
}

// ---------------------------------------------------------------------------
// SPI 原始扇区读取 (SD 卡 CMD17 READ_SINGLE_BLOCK)
// 用于在 SD.begin 之前探测文件系统格式; 结束后释放总线。
// ---------------------------------------------------------------------------

// 发送一个 SD 命令 (6 字节: 0x40|cmd, arg[4], crc), 返回 R1 响应
static uint8_t sdSpiCommand(uint8_t cmd, uint32_t arg, uint8_t crc)
{
  SPI.transfer(0x40 | cmd);
  SPI.transfer((arg >> 24) & 0xFF);
  SPI.transfer((arg >> 16) & 0xFF);
  SPI.transfer((arg >> 8) & 0xFF);
  SPI.transfer(arg & 0xFF);
  SPI.transfer(crc);
  // 等待响应 (最多 8 字节, 响应非 0xFF 即到达)
  uint8_t r1 = 0xFF;
  for (int i = 0; i < 8; i++) {
    r1 = SPI.transfer(0xFF);
    if (r1 != 0xFF) break;
  }
  return r1;
}

static bool sdSpiReadSector(uint8_t csPin, uint32_t sector, uint8_t* out512)
{
  // ⚠️ 重要: 不要调用 SPI.begin()!
  // SPI 总线已由 M5Unified 用 CoreS3 的 SD 卡槽专用引脚初始化,
  // 无参 SPI.begin() 会把引脚重置为 ESP32 默认值 (SCK=18/MISO=19/MOSI=23),
  // 导致后续 SD.begin() 用错引脚挂载失败。
  // 这里直接复用已有总线配置, 低速 400kHz 探测。
  SPI.beginTransaction(SPISettings(400000, MSBFIRST, SPI_MODE0));
  pinMode(csPin, OUTPUT);
  digitalWrite(csPin, HIGH);

  // 上电初始化: CS 拉低, 发 80 个时钟 (>74 个)
  digitalWrite(csPin, LOW);
  for (int i = 0; i < 10; i++) SPI.transfer(0xFF);
  digitalWrite(csPin, HIGH);
  SPI.transfer(0xFF);

  // CMD0: GO_IDLE_STATE
  digitalWrite(csPin, LOW);
  uint8_t r1 = sdSpiCommand(0, 0, 0x95);
  if (r1 != 0x01) {
    digitalWrite(csPin, HIGH);
    SPI.endTransaction();
    return false;  // 卡无响应
  }

  // CMD8: 接口条件 (区分 SDv2)
  sdSpiCommand(8, 0x1AA, 0x87);
  uint8_t r7[4] = {0};
  for (int i = 0; i < 4; i++) r7[i] = SPI.transfer(0xFF);

  // ACMD41: 初始化 (SDSC/SDHC 兼容, 最多重试 200 次)
  bool isSdhc = (r7[2] == 0x01 && r7[3] == 0xAA);
  uint8_t initOk = 0x01;
  for (int retry = 0; retry < 200; retry++) {
    sdSpiCommand(55, 0, 0x65);                       // CMD55 (APP_CMD 前缀)
    initOk = sdSpiCommand(41, isSdhc ? 0x40000000 : 0, 0x77);  // ACMD41
    if (initOk == 0x00) break;
    delay(10);
  }
  if (initOk != 0x00) {
    digitalWrite(csPin, HIGH);
    SPI.endTransaction();
    return false;  // 初始化超时
  }

  // CMD17: READ_SINGLE_BLOCK (SDHC 用块地址, SDSC 用字节地址)
  uint32_t addr = isSdhc ? sector : sector * 512;
  r1 = sdSpiCommand(17, addr, 0x15);
  if (r1 != 0x00) {
    digitalWrite(csPin, HIGH);
    SPI.endTransaction();
    return false;
  }

  // 等数据令牌 0xFE
  uint8_t token = 0xFF;
  uint16_t wait = 0;
  while (token != 0xFE && wait++ < 2000) {
    token = SPI.transfer(0xFF);
  }
  if (token != 0xFE) {
    digitalWrite(csPin, HIGH);
    SPI.endTransaction();
    return false;
  }

  // 读 512 字节 + 2 字节 CRC
  for (int i = 0; i < 512; i++) out512[i] = SPI.transfer(0xFF);
  SPI.transfer(0xFF);  // CRC 高字节 (忽略)
  SPI.transfer(0xFF);  // CRC 低字节 (忽略)

  digitalWrite(csPin, HIGH);
  SPI.endTransaction();
  return true;
}

// 解析引导扇区 BPB → 文件系统类型
static sd_fs_type_t parseBpb(const uint8_t* sec)
{
  // exFAT: 引导扇区偏移 3 有 "EXFAT   " 签名
  if (memcmp(&sec[3], "EXFAT   ", 8) == 0) return SD_FS_EXFAT;
  // NTFS: 偏移 3 "NTFS    "
  if (memcmp(&sec[3], "NTFS    ", 8) == 0) return SD_FS_NTFS;
  // FAT12/16/32: 偏移 54 "FAT12   "/"FAT16   ", 偏移 82 "FAT32   "
  if (memcmp(&sec[82], "FAT32   ", 8) == 0) return SD_FS_FAT32;
  if (memcmp(&sec[54], "FAT16   ", 8) == 0) return SD_FS_FAT16;
  if (memcmp(&sec[54], "FAT12   ", 8) == 0) return SD_FS_FAT12;
  return SD_FS_RAW;
}

bool sdDetectFileSystem(uint8_t csPin, sd_fs_type_t* outType, uint32_t* outSizeMB)
{
  if (outType) *outType = SD_FS_NOT_PRESENT;
  if (outSizeMB) *outSizeMB = 0;

  static uint8_t sector[512];  // 静态分配, 避免栈溢出 (ESP32 主栈通常足够但稳妥)
  memset(sector, 0, sizeof(sector));

  // 读扇区 0 (MBR 或 superfloppy 引导扇区)
  if (!sdSpiReadSector(csPin, 0, sector)) {
    Serial.println("[SD] 探测失败: 卡无响应 (检查接触/供电)");
    return false;
  }

  // MBR 签名检查 (0x55 0xAA)
  bool isMbr = (sector[510] == 0x55 && sector[511] == 0xAA);

  // 先尝试 superfloppy (无分区表, 引导扇区直接在扇区 0)
  sd_fs_type_t fsType = parseBpb(sector);
  uint32_t firstPartLba = 0;

  if (fsType == SD_FS_RAW && isMbr) {
    // 有 MBR: 解析第一个分区条目 (偏移 446, 每条 16 字节)
    //   [4] = 分区类型, [8..11] = 分区起始 LBA (LE)
    uint8_t partType = sector[446 + 4];
    firstPartLba = sector[446 + 8]
                 | ((uint32_t)sector[446 + 9] << 8)
                 | ((uint32_t)sector[446 + 10] << 16)
                 | ((uint32_t)sector[446 + 11] << 24);

    Serial.printf("[SD] MBR 分区类型: 0x%02X", partType);
    switch (partType) {
      case 0x01: Serial.print(" (FAT12)");    break;
      case 0x04:
      case 0x06:
      case 0x0E: Serial.print(" (FAT16)");    break;
      case 0x0B:
      case 0x0C: Serial.print(" (FAT32)");    break;
      case 0x07: Serial.print(" (exFAT/NTFS)"); break;
      case 0xEE: Serial.print(" (GPT — ESP32 FatFs 可能不兼容!)"); break;
      default:   Serial.print(" (其他)");      break;
    }
    Serial.printf(", 起始扇区 %u\n", firstPartLba);

    // 读分区起始扇区, 解析 BPB
    if (firstPartLba > 0) {
      memset(sector, 0, sizeof(sector));
      if (sdSpiReadSector(csPin, firstPartLba, sector)) {
        fsType = parseBpb(sector);
      }
    }
  }

  if (outType) *outType = fsType;

  // 容量估算: 从 BPB 的 total sectors 字段 (偏移 32, FAT32 用偏移 32 的 DWORD)
  if (outSizeMB && (fsType == SD_FS_FAT16 || fsType == SD_FS_FAT32)) {
    uint32_t totalSectors = sector[19] | ((uint32_t)sector[20] << 8);
    if (totalSectors == 0) {
      totalSectors = sector[32] | ((uint32_t)sector[33] << 8)
                   | ((uint32_t)sector[34] << 16) | ((uint32_t)sector[35] << 24);
    }
    *outSizeMB = (uint32_t)((uint64_t)totalSectors * 512 / 1024 / 1024);
  }

  return true;
}

bool sdBeginEx(uint8_t csPin, uint32_t freqKHz)
{
  // 1. 文件系统探测 (可选, 默认关闭 — 见 SDUtil.h 的 SD_RAW_PROBE_ENABLED)
  sd_fs_type_t fsType = SD_FS_UNKNOWN;
  uint32_t sizeMB = 0;
  bool cardPresent = false;
#ifdef SD_RAW_PROBE_ENABLED
  cardPresent = sdDetectFileSystem(csPin, &fsType, &sizeMB);
  if (cardPresent) {
    Serial.printf("[SD] 检测到卡: %s, 容量 ≈ %u MB\n",
                  sdFsTypeToString(fsType), sizeMB);
    if (fsType == SD_FS_NTFS || fsType == SD_FS_RAW) {
      Serial.println("[SD] ⚠️ 文件系统不兼容, 请用 SD Card Formatter 格式化为 FAT32");
    }
  }
#else
  Serial.println("[SD] 跳过原始探测 (默认关闭, 避免干扰初始化时序)");
#endif

  // 2. 多频率降级重试挂载
  static const uint32_t freqTable[] = {25000, 16000, 10000, 4000};  // kHz
  bool haveCustom = (freqKHz > 0 && freqKHz != 25000);
  uint32_t freqs[5];
  uint8_t nFreqs = 0;
  if (haveCustom) freqs[nFreqs++] = freqKHz;
  for (uint32_t f : freqTable) {
    if (!haveCustom || f != freqKHz) freqs[nFreqs++] = f;
  }

  for (uint8_t i = 0; i < nFreqs; i++) {
    uint32_t f = freqs[i];
    Serial.printf("[SD] 尝试挂载 @ %d MHz...\n", (int)(f / 1000));
    if (SD.begin(csPin, SPI, f * 1000)) {
      Serial.printf("[SD] ✓ 挂载成功 @ %d MHz\n", (int)(f / 1000));
      sdPrintSdInfo();
      return true;
    }
    // 注意: 不调用 SD.end() — ESP32 Arduino SD.begin 失败时内部已自行
    // 清理 (esp_vfs_fat_sdspi_unmount), 外部再 end 可能破坏 SPI 状态,
    // 导致后续频率重试全部失败。仅延迟后重试。
    delay(200);
  }

  // 3. 全部失败: 输出诊断建议
  Serial.println("[SD] ✗ 挂载失败 (所有频率)");
  if (!cardPresent) {
    Serial.println("[SD] 建议: 检查卡槽接触 / 供电 / 换卡测试");
  } else if (fsType == SD_FS_EXFAT) {
    Serial.println("[SD] 建议: exFAT 卡请用 SD Card Formatter 重新格式化为 FAT32");
  } else {
    Serial.println("[SD] 建议: 卡可能损坏, 尝试重新格式化 (FAT32, 簇 32KB)");
  }
  return false;
}

void sdPrintSdInfo(void)
{
  sdcard_type_t ct = SD.cardType();
  const char* cardTypeStr = "?";
  switch (ct) {
    case CARD_NONE: cardTypeStr = "无";    break;
    case CARD_MMC:  cardTypeStr = "MMC";   break;
    case CARD_SD:   cardTypeStr = "SDSC";  break;
    case CARD_SDHC: cardTypeStr = "SDHC";  break;
    default:        cardTypeStr = "未知";  break;
  }
  uint64_t totalMB = SD.totalBytes() / (1024 * 1024);
  uint64_t usedMB  = SD.usedBytes()  / (1024 * 1024);
  Serial.printf("[SD] 卡类型: %s, 容量: %llu MB, 已用: %llu MB\n",
                cardTypeStr, totalMB, usedMB);
}

