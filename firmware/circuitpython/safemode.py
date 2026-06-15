# safemode.py - OTA 回滚兜底
#
# CircuitPython 在 hard fault / brownout / watchdog 等异常时执行此文件,
# 然后不执行 code.py. 我们用它检测 OTA 异常并把 /_ota_backup/ 内容恢复到 /.
#
# 回滚触发条件 (满足任一):
#   1. /_ota_in_progress marker 存在 (切换过程中断电)
#   2. nvm[5] == 0x01 (first_boot_after_ota flag, 新 fw 启动后崩了)
#
# 回滚优先用 app.ota_updater.rollback_from_backup (功能完整),
# 失败则内联回滚 (不依赖任何外部模块, 防新固件把 app/ 写坏了)

import os
import time
import microcontroller
import supervisor

print("=" * 50)
print("[SafeMode] enter safe mode")
try:
    print(f"[SafeMode] reason: {supervisor.runtime.safe_mode_reason}")
except Exception:
    pass

_OTA_BACKUP_DIR = "/_ota_backup"
_OTA_NEW_DIR = "/_ota_new"
_OTA_IN_PROGRESS = "/_ota_in_progress"

_NVM_FIRST_BOOT_OFF = 5  # 与 app/ota_nvm.py 保持一致

# 远程配置 verify/rollback (与 app/remote_cmd_nvm.py 保持一致)
_NVM_CFG_VERIFY_OFF = 13
_NVM_CFG_FAIL_OFF = 22
_NVM_CFG_STATE_OFF = 23
_CFG_PREV = "/config_prev.json"
_NVM_CFG_LEN_OFF = 32   # uint16 big-endian 配置长度头 (config_mgr.py)
_NVM_CFG_DATA_OFF = 34


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _is_ota_anomaly():
    if _exists(_OTA_IN_PROGRESS):
        return True
    try:
        if microcontroller.nvm[_NVM_FIRST_BOOT_OFF] == 0x01:
            return True
    except Exception:
        pass
    return False


def _ensure_dir_inline(filepath):
    parts = filepath.rsplit("/", 1)
    if len(parts) != 2 or not parts[0]:
        return
    dir_path = parts[0]
    try:
        os.stat(dir_path)
        return
    except OSError:
        pass
    sub = ""
    for p in dir_path.strip("/").split("/"):
        sub += "/" + p
        try:
            os.stat(sub)
        except OSError:
            try:
                os.mkdir(sub)
            except OSError:
                pass


def _rmtree_inline(path):
    try:
        st = os.stat(path)
    except OSError:
        return
    if st[0] & 0x4000:
        try:
            for name in os.listdir(path):
                _rmtree_inline(path + "/" + name)
            os.rmdir(path)
        except OSError:
            pass
    else:
        try:
            os.remove(path)
        except OSError:
            pass


def _rollback_inline():
    """完全内联的回滚, 不 import 任何 app/* 模块"""
    if not _exists(_OTA_BACKUP_DIR):
        print("[SafeMode] no backup, 无法回滚")
        return 0

    restored = 0

    def _walk(rel):
        nonlocal restored
        bk = _OTA_BACKUP_DIR + rel
        try:
            entries = os.listdir(bk)
        except OSError:
            return
        for name in entries:
            bk_path = bk + "/" + name
            try:
                st = os.stat(bk_path)
            except OSError:
                continue
            if st[0] & 0x4000:
                _walk(rel + "/" + name)
            else:
                dst = "/" + (rel + "/" + name).lstrip("/")
                _ensure_dir_inline(dst)
                try:
                    os.remove(dst)
                except OSError:
                    pass
                try:
                    os.rename(bk_path, dst)
                    restored += 1
                except OSError as e:
                    print(f"[SafeMode] restore {dst} failed: {e}")

    _walk("")
    _rmtree_inline(_OTA_BACKUP_DIR)
    _rmtree_inline(_OTA_NEW_DIR)

    try:
        microcontroller.nvm[_NVM_FIRST_BOOT_OFF] = 0x00
    except Exception:
        pass
    try:
        os.remove(_OTA_IN_PROGRESS)
    except OSError:
        pass

    return restored


def _rollback_config_inline():
    """远程配置验证中崩溃 → 把 /config_prev.json 内联写回 NVM 配置区, 不 import app/*"""
    if not _exists(_CFG_PREV):
        # 无备份, 仅清验证标志避免反复进 safemode
        try:
            microcontroller.nvm[_NVM_CFG_VERIFY_OFF] = 0x00
        except Exception:
            pass
        return False
    try:
        import json
        with open(_CFG_PREV, "r") as f:
            obj = json.load(f)
        b = json.dumps(obj).encode("utf-8")   # 紧凑化省 NVM
        length = len(b)
        # 长度头 uint16 big-endian + JSON, 与 config_mgr.py 格式一致
        microcontroller.nvm[_NVM_CFG_LEN_OFF:_NVM_CFG_LEN_OFF + 2] = bytes(
            [(length >> 8) & 0xFF, length & 0xFF])
        microcontroller.nvm[_NVM_CFG_DATA_OFF:_NVM_CFG_DATA_OFF + length] = b
        microcontroller.nvm[_NVM_CFG_VERIFY_OFF] = 0x00
        microcontroller.nvm[_NVM_CFG_FAIL_OFF] = 0
        microcontroller.nvm[_NVM_CFG_STATE_OFF] = 2   # cfg_state = rolled_back
        try:
            os.remove(_CFG_PREV)
        except OSError:
            pass
        return True
    except Exception as e:
        print(f"[SafeMode] cfg rollback err: {e}")
        try:
            microcontroller.nvm[_NVM_CFG_VERIFY_OFF] = 0x00
        except Exception:
            pass
        return False


# 远程配置验证中崩溃 → 优先回滚配置 (独立于 OTA; 坏配置可能让新 fw 跑崩)
_cfg_verifying = False
try:
    _cfg_verifying = microcontroller.nvm[_NVM_CFG_VERIFY_OFF] == 0x01
except Exception:
    pass
if _cfg_verifying:
    print("[SafeMode] 远程配置验证中崩溃 -> 回滚配置")
    _ok = _rollback_config_inline()
    print(f"[SafeMode] config rollback: {'OK' if _ok else 'no-backup'}")
    print("[SafeMode] reset -> 旧配置")
    time.sleep(1)
    microcontroller.reset()

if _is_ota_anomaly():
    print("[SafeMode] OTA anomaly detected -> rollback")

    # 优先用 ota_updater 的实现 (有更完整的日志)
    n = -1
    try:
        from app.ota_updater import rollback_from_backup
        n = rollback_from_backup()
        print(f"[SafeMode] rollback via ota_updater: {n} files")
    except Exception as e:
        print(f"[SafeMode] ota_updater rollback unavailable ({e}), 用内联回滚")
        n = _rollback_inline()
        print(f"[SafeMode] inline rollback: {n} files")

    # 恢复 OTA 前的原始 USB 模式 (OTA 期间从 flash 临时切了 daily)
    # hardcode nvm[11]/nvm[12] 与 app/ota_nvm.py 的 _OFF_OTA_ORIG_MODE/_OFF_OTA_RESUME 一致
    try:
        _orig = microcontroller.nvm[11]
        if _orig != 0xFF:
            print(f"[SafeMode] 恢复 USB 模式 nvm0 → {_orig}")
            microcontroller.nvm[0] = _orig
            microcontroller.nvm[11] = 0xFF
            microcontroller.nvm[12] = 0x00
    except Exception as _e:
        print(f"[SafeMode] usb mode restore err: {_e}")

    print("[SafeMode] reset → 进入旧固件")
    time.sleep(1)
    microcontroller.reset()
else:
    print("[SafeMode] 非 OTA 异常, 不动作")
