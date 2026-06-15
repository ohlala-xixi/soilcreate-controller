# ota_nvm.py - OTA 状态在 microcontroller.nvm 的存储
#
# NVM 全局布局 (整个项目共享, 改这里务必同步 boot.py / config_mgr.py / safemode.py):
#   nvm[0]      USB RW 模式 (0=flash, 17=daily) — 历史占用, 勿动
#   nvm[1:5]    OTA last_check unix timestamp (4 字节 little-endian uint32)
#               值为 0 或 0xFFFFFFFF 表示从未 check 过 (首次启动)
#   nvm[5]      first_boot_after_ota flag (0x01=新固件首次启动需自检, 0x00=已确认)
#   nvm[6:10]   OTA 当前文件 download offset (断点续传用, 4 字节 little-endian uint32)
#   nvm[10]     OTA 当前下载的 file index (0-255)
#   nvm[11]     OTA 切 daily 前的原始 USB 模式 (0=flash, 17=daily; 0xFF=无 pending)
#   nvm[12]     OTA resume 标志 (0x01=daily 启动后继续下载; 0x00/0xFF=无)
#   nvm[13]     远程配置 verify flag (app/remote_cmd_nvm.py)
#   nvm[14]     safemode/recovery flag (code.py #clear_safe / FATAL 用)
#   nvm[15]     boot 阶段追踪 (code.py _boot_stage 用)
#   nvm[16:18]  boot count (2 字节 little-endian uint16)
#   nvm[18:22]  远程配置 last_applied_rev u32 LE (app/remote_cmd_nvm.py)
#   nvm[22]     远程配置连续失败计数 (app/remote_cmd_nvm.py)
#   nvm[23]     远程配置 cfg_state (app/remote_cmd_nvm.py)
#   nvm[24:32]  预留
#   nvm[32:34]  配置长度头 uint16 BE (config_mgr.py)
#   nvm[34:]    ConfigManager 配置 JSON  ← 配置区从 32 起, 避开系统 flag 区

import microcontroller

# 24 小时 = 86400 秒
OTA_CHECK_INTERVAL_SEC = 86400

_OFF_LAST_CHECK = 1   # 1..4
_OFF_FIRST_BOOT = 5
_OFF_DL_OFFSET = 6    # 6..9
_OFF_DL_FILE_IDX = 10
_OFF_OTA_ORIG_MODE = 11
_OFF_OTA_RESUME = 12

_NEVER = 0xFFFFFFFF


def _read_u32(off: int) -> int:
    """从 nvm[off:off+4] 读 little-endian uint32"""
    try:
        b = bytes(microcontroller.nvm[off:off + 4])
        return b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)
    except Exception:
        return _NEVER


def _write_u32(off: int, val: int):
    """写 little-endian uint32 到 nvm[off:off+4]"""
    val &= 0xFFFFFFFF
    microcontroller.nvm[off:off + 4] = bytes([
        val & 0xFF,
        (val >> 8) & 0xFF,
        (val >> 16) & 0xFF,
        (val >> 24) & 0xFF,
    ])


# ── last_ota_check 时间戳 ────────────────────────────────────────

def read_last_check() -> int:
    """返回上次 OTA check 的 unix timestamp, 或 0 表示从未 check 过"""
    v = _read_u32(_OFF_LAST_CHECK)
    if v == _NEVER:
        return 0
    return v


def write_last_check(ts: int):
    _write_u32(_OFF_LAST_CHECK, int(ts))


def should_check_ota(now_ts: int, interval_sec: int = OTA_CHECK_INTERVAL_SEC) -> bool:
    """是否到了 OTA check 周期

    首次启动 (NVM 无记录) → True
    距上次 >= interval_sec → True
    """
    last = read_last_check()
    if last == 0:
        return True
    return (now_ts - last) >= interval_sec


# ── first_boot_after_ota flag ────────────────────────────────────

def is_first_boot_after_ota() -> bool:
    try:
        return microcontroller.nvm[_OFF_FIRST_BOOT] == 0x01
    except Exception:
        return False


def set_first_boot_flag(on: bool):
    microcontroller.nvm[_OFF_FIRST_BOOT] = 0x01 if on else 0x00


# ── 断点续传 offset / file index (留给后续 task 接入) ────────────

def read_download_offset() -> int:
    v = _read_u32(_OFF_DL_OFFSET)
    if v == _NEVER:
        return 0
    return v


def write_download_offset(off: int):
    _write_u32(_OFF_DL_OFFSET, int(off))


def read_file_index() -> int:
    try:
        v = microcontroller.nvm[_OFF_DL_FILE_IDX]
        if v == 0xFF:
            return 0
        return v
    except Exception:
        return 0


def write_file_index(idx: int):
    microcontroller.nvm[_OFF_DL_FILE_IDX] = int(idx) & 0xFF


def reset_download_state():
    """OTA 成功或彻底放弃后, 清断点续传状态"""
    write_download_offset(0)
    write_file_index(0)


# ── OTA 自动 USB 模式切换状态机 (flash↔daily 跨 reboot) ──────────
#
# 场景: board 平时 flash mode (host 可写盘, 方便开发). OTA 需要 device
# 写文件系统 = daily mode. CircuitPython 运行时不能从 flash 切 device RW,
# 必须经 boot.py = reboot. 所以:
#   flash + 有更新 → 存原始模式 + 标记 resume + 切 daily + reboot
#   daily 启动检测 resume → 继续下载/切换
#   commit/rollback 后 → 恢复原始模式 + reboot 回 flash

def set_ota_resume(orig_mode: int):
    """flash→daily 切换前调用: 存原始 USB 模式 + 标记 reboot 后继续 OTA"""
    microcontroller.nvm[_OFF_OTA_ORIG_MODE] = orig_mode & 0xFF
    microcontroller.nvm[_OFF_OTA_RESUME] = 0x01


def is_ota_resume() -> bool:
    """daily 启动后是否需要继续 OTA 下载"""
    try:
        return microcontroller.nvm[_OFF_OTA_RESUME] == 0x01
    except Exception:
        return False


def get_ota_orig_mode():
    """返回 OTA 前的原始 USB 模式 (0/17), 或 None 表示无 pending"""
    try:
        v = microcontroller.nvm[_OFF_OTA_ORIG_MODE]
        if v == 0xFF:
            return None
        return v
    except Exception:
        return None


def clear_ota_resume():
    microcontroller.nvm[_OFF_OTA_RESUME] = 0x00


def clear_ota_orig_mode():
    microcontroller.nvm[_OFF_OTA_ORIG_MODE] = 0xFF
