#ifndef _SD_UTIL_H
#define _SD_UTIL_H

#include <Arduino.h>

// ---------------------------------------------------------------------------
// 原始扇区探测开关:
//   取消注释启用 SPI 原始读扇区文件系统探测。
//   默认关闭 — 手动 CMD 序列可能给部分 SD 卡留下脏状态,
//   干扰后续 SD.begin 初始化时序 (表现为挂载失败)。
// ---------------------------------------------------------------------------
// #define SD_RAW_PROBE_ENABLED

int read_sd_file(const char* filename, char* buf, int buf_size);
int read_line_from_buf(char* buf, char* out);
bool copySDFileToSPIFFS(const char *path, bool forced);
size_t copySDFileToRAM(const char *path, uint8_t *out, int bufSize);

// =============================================================================
// 强化 SD 卡挂载 (v2): 文件系统格式识别 + 多频率降级重试 + 详细诊断
// =============================================================================

/// 文件系统格式枚举
typedef enum {
    SD_FS_UNKNOWN = 0,   // 无法识别 (可能是新卡/损坏/无分区信息)
    SD_FS_FAT12,
    SD_FS_FAT16,
    SD_FS_FAT32,
    SD_FS_EXFAT,
    SD_FS_NTFS,          // NTFS: ESP32 FatFs 无法挂载, 需重新格式化
    SD_FS_RAW,           // 无文件系统 (RAW)
    SD_FS_NOT_PRESENT,   // 卡不存在 (SPI 无响应)
} sd_fs_type_t;

/**
 * @brief 检测 SD 卡文件系统格式 (在 SD.begin 之前用 SPI 原始读扇区)
 *
 * 实现: SPI CMD17 读扇区 0 → MBR 分区表 / 引导扇区 BPB 解析。
 * 结束后释放 SPI 总线, 不影响后续 SD.begin 的正常初始化。
 *
 * @param csPin   CS 引脚 (CoreS3 = GPIO_NUM_4)
 * @param freq    SPI 频率 (Hz), 建议 400kHz 低速探测
 * @param outType 输出: 检测到的文件系统格式
 * @param outSizeMB 输出: 卡容量 (MB), 失败为 0
 * @return true 卡存在 (格式可能为 UNKNOWN/RAW), false 卡无响应
 */
bool sdDetectFileSystem(uint8_t csPin, sd_fs_type_t* outType, uint32_t* outSizeMB);

/**
 * @brief 强化挂载: 多频率降级重试 + 诊断输出
 *
 * 依次尝试 25MHz → 16MHz → 10MHz → 4MHz。
 * 很多 SD 卡在高频下读写不稳定, 降频后可正常挂载。
 *
 * @param csPin    CS 引脚
 * @param freqKHz  首选频率 (kHz), 0 = 默认 25000
 * @return true 挂载成功
 */
bool sdBeginEx(uint8_t csPin, uint32_t freqKHz = 25000);

/**
 * @brief 打印 SD 卡诊断信息 (卡类型/容量/文件系统格式)
 */
void sdPrintSdInfo(void);

/// 格式枚举转字符串
const char* sdFsTypeToString(sd_fs_type_t t);

#endif // _SD_UTIL_H
