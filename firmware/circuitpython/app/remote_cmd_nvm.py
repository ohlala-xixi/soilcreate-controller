# remote_cmd_nvm.py - 远程配置下发的 verify/rollback 状态在 microcontroller.nvm 的存储
#
# 远程 retained 指令 (topic cirpy-info/<cid>) 应用后, 用这套状态机做"失败回滚":
# 坏配置 (如错 broker/APN) 不会让板子崩溃, safemode 抓不到, 所以用
# "连续 N 个周期上报失败 → 回滚旧配置" 的计数器, 独立于 OTA 的 safemode 回滚.
#
# NVM 全局布局 (整个项目共享, 改这里务必同步 app/ota_nvm.py / safemode.py / config_mgr.py):
#   nvm[13]     远程配置 verify flag (0x01=验证中, 0x00=空闲)   ← 原 ota_nvm 标"预留"
#   nvm[18:22]  last_applied_rev (4 字节 little-endian uint32)   ← 原"预留"区
#   nvm[22]     连续上报失败计数 (uint8)
#   nvm[23]     cfg_state 码 (0=none 1=verifying 2=rolled_back 3=committed)
#
# rev 去重: last_applied_rev 持久跨深睡重启. 收到 retained 指令 rev<=last 即丢弃,
# 保证同一条指令只执行一次 (retained 每次醒来重订阅都会重收, 但不重复执行).

import microcontroller

_OFF_VERIFY = 13
_OFF_APPLIED_REV = 18   # 18..21
_OFF_FAIL_CNT = 22
_OFF_CFG_STATE = 23

ROLLBACK_THRESHOLD = 3   # 连续 N 个周期上报失败 → 回滚

CFG_STATE_NONE = 0
CFG_STATE_VERIFYING = 1
CFG_STATE_ROLLED_BACK = 2
CFG_STATE_COMMITTED = 3

_STATE_NAMES = {
    CFG_STATE_NONE: "none",
    CFG_STATE_VERIFYING: "verifying",
    CFG_STATE_ROLLED_BACK: "rolled_back",
    CFG_STATE_COMMITTED: "committed",
}


def _read_u32(off: int) -> int:
    try:
        b = bytes(microcontroller.nvm[off:off + 4])
        return b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)
    except Exception:
        return 0


def _write_u32(off: int, val: int):
    val &= 0xFFFFFFFF
    microcontroller.nvm[off:off + 4] = bytes([
        val & 0xFF, (val >> 8) & 0xFF, (val >> 16) & 0xFF, (val >> 24) & 0xFF,
    ])


# ── last_applied_rev (rev 去重) ──────────────────────────────────

def read_applied_rev() -> int:
    """已应用过的最大 rev; 0xFFFFFFFF (空 NVM) 视为 0"""
    v = _read_u32(_OFF_APPLIED_REV)
    return 0 if v == 0xFFFFFFFF else v


def write_applied_rev(rev: int):
    _write_u32(_OFF_APPLIED_REV, int(rev))


def is_new_rev(rev) -> bool:
    """rev 是否比已应用的更新 (用于去重: 旧的/重复的 retained 丢弃)"""
    try:
        return int(rev) > read_applied_rev()
    except Exception:
        return False


# ── verify / fail 计数 ───────────────────────────────────────────

def is_verifying() -> bool:
    try:
        return microcontroller.nvm[_OFF_VERIFY] == 0x01
    except Exception:
        return False


def begin_verify(rev: int):
    """应用了一条带 config 的新指令 → 进入验证态, 等下个周期上报成功才 commit"""
    write_applied_rev(rev)
    microcontroller.nvm[_OFF_VERIFY] = 0x01
    microcontroller.nvm[_OFF_FAIL_CNT] = 0
    microcontroller.nvm[_OFF_CFG_STATE] = CFG_STATE_VERIFYING


def commit():
    """本周期上报成功 → 坐实新配置, 退出验证态"""
    microcontroller.nvm[_OFF_VERIFY] = 0x00
    microcontroller.nvm[_OFF_FAIL_CNT] = 0
    microcontroller.nvm[_OFF_CFG_STATE] = CFG_STATE_COMMITTED


def note_fail() -> int:
    """本周期上报失败 → 失败计数 +1, 返回当前计数"""
    try:
        n = microcontroller.nvm[_OFF_FAIL_CNT]
        if n == 0xFF:
            n = 0
        n += 1
        microcontroller.nvm[_OFF_FAIL_CNT] = n & 0xFF
        return n
    except Exception:
        return 0


def should_rollback() -> bool:
    """是否已达到回滚阈值 (验证中 且 连续失败 >= 阈值)"""
    if not is_verifying():
        return False
    try:
        n = microcontroller.nvm[_OFF_FAIL_CNT]
        if n == 0xFF:
            n = 0
        return n >= ROLLBACK_THRESHOLD
    except Exception:
        return False


def mark_committed_state():
    """纯 action 指令应用后调用: 状态直接标 committed (action 即时执行无需验证),
    让 bridge 的闭环能清掉该条 retained. 正在 verify 配置时不要调 (会误标)."""
    microcontroller.nvm[_OFF_CFG_STATE] = CFG_STATE_COMMITTED


def mark_rolled_back():
    """回滚执行后: 退出验证态, 状态标 rolled_back (last_applied_rev 保持不动,
    这样坏的那条 rev 不会被重新应用; 服务器需推 rev+1 修正)"""
    microcontroller.nvm[_OFF_VERIFY] = 0x00
    microcontroller.nvm[_OFF_FAIL_CNT] = 0
    microcontroller.nvm[_OFF_CFG_STATE] = CFG_STATE_ROLLED_BACK


# ── 上报用 ───────────────────────────────────────────────────────

def get_cfg_state_name() -> str:
    try:
        return _STATE_NAMES.get(microcontroller.nvm[_OFF_CFG_STATE], "none")
    except Exception:
        return "none"
