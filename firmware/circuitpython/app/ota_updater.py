# ota_updater.py - OTA 固件切换核心 (CircuitPython)
#
# 仅保留"下载完成之后"的核心逻辑, 供 BLE OTA (app/ble_ota.py) 复用:
#   - _apply_update():            三阶段原子切换 (备份 → 应用 /_ota_new/ → 标记 NVM → reset)
#   - commit_after_selftest():    新固件首启自检通过后提交 (清 backup + 标记)
#   - rollback_from_backup():     回滚到 /_ota_backup/
#   - restore_usb_mode_after_ota(): 恢复 OTA 期间临时切的 USB 模式
# 入口前置: /_ota_new/<path> 已是完整、校验过的新版本 (由 BLE 收帧填充)。
# (HTTP/4G/WiFi/Ethernet 多通道下载已移除, 改走 BLE 现场推送)

import os
import json
import hashlib
import gc
import time

from app import ota_nvm

# time.time() 阈值: 小于这个值视为时间未对齐, 跳过 OTA 防止把 NVM 写脏
_MIN_VALID_UNIX = 1704067200  # 2024-01-01 00:00:00 UTC

_OTA_NEW_DIR = "/_ota_new"
_OTA_BACKUP_DIR = "/_ota_backup"
_OTA_IN_PROGRESS_MARKER = "/_ota_in_progress"  # 切换中标记, safemode 据此回滚


def _file_sha256(filepath):
    h = hashlib.new("sha256")
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(512)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ── filesystem helpers ──────────────────────────────────────

def _ensure_dir(filepath):
    """确保 filepath 所在目录存在 (递归创建)"""
    parts = filepath.rsplit("/", 1)
    if len(parts) == 2 and parts[0]:
        dir_path = parts[0]
        try:
            os.stat(dir_path)
        except OSError:
            _makedirs(dir_path)


def _makedirs(path):
    """递归创建目录 (CircuitPython 无 os.makedirs)"""
    parts = path.strip("/").split("/")
    current = ""
    for part in parts:
        current += "/" + part
        try:
            os.stat(current)
        except OSError:
            try:
                os.mkdir(current)
            except OSError:
                pass


def _cleanup_dir(path):
    """递归删除整个目录 (CircuitPython 无 shutil.rmtree)"""
    try:
        st = os.stat(path)
    except OSError:
        return
    if st[0] & 0x4000:  # S_IFDIR
        try:
            for name in os.listdir(path):
                _cleanup_dir(f"{path}/{name}")
            os.rmdir(path)
        except OSError as e:
            print(f"[OTA] rmdir err {path}: {e}")
    else:
        try:
            os.remove(path)
        except OSError as e:
            print(f"[OTA] rm err {path}: {e}")


# 注: 版本号已移到 app/fw_version.py (随代码走), OTA 不再回写 config.json。
#     新 code.py/fw_version.py 一应用, 版本即自动生效, 不依赖 config/NVM。


# ── 三阶段原子切换 ───────────────────────────────────────────
#
# 入口前置: /_ota_new/ 下已是完整新版本 (校验通过)
#
# 中途断电恢复路径 (★ 主兜底在 boot.py 的 _ota_rollback_guard, 不是 safemode —
#   备份/应用阶段断电后 / 上可能没有 code.py, CircuitPython 只会安静掉 REPL,
#   不触发 safemode; boot.py 每次启动必跑, 检测 marker+first_boot 未置 即回滚):
#   - 备份/应用阶段断电 → marker 在 + nvm[5] 未置 → boot.py 回滚 backup → 旧版本
#   - 应用完成、写 NVM 前断电 → 同上 (boot.py 回滚, 旧版本; 新版本这次白切, 可重推)
#   - 应用完成、写 NVM 后断电 → marker 在 + nvm[5]=1 → boot.py 放行, 正常进自检;
#     自检失败 → code.py FATAL handler 回滚 (普通异常) / safemode 回滚 (hard fault)

def _apply_update(config, new_version, files):
    """三阶段切换. 不返回 (microcontroller.reset)."""
    import microcontroller

    # 0. 写 in-progress marker
    print(f"[OTA] 阶段 0/3: 标记切换开始")
    try:
        with open(_OTA_IN_PROGRESS_MARKER, "w") as f:
            f.write(new_version)
    except OSError as e:
        print(f"[OTA] 写 marker 失败: {e} — 中止切换")
        _cleanup_dir(_OTA_NEW_DIR)
        return

    # 1. 清旧 backup
    _cleanup_dir(_OTA_BACKUP_DIR)

    # 2. 备份当前工作区中将被替换的文件 → /_ota_backup/
    print(f"[OTA] 阶段 1/3: 备份当前固件到 {_OTA_BACKUP_DIR}")
    backed_up_count = 0
    for fi in files:
        rel = fi["path"]
        src = "/" + rel
        dst = _OTA_BACKUP_DIR + "/" + rel
        if _exists(src):
            _ensure_dir(dst)
            try:
                os.rename(src, dst)
                backed_up_count += 1
            except OSError as e:
                print(f"[OTA] 备份失败 {src}: {e}")
                # 失败不中止 — backup 不完整也能继续, 但 rollback 时这部分丢
    print(f"[OTA] backup: {backed_up_count}/{len(files)} files")

    # 3. 应用新版本: /_ota_new/{path} → /{path}
    print(f"[OTA] 阶段 2/3: 应用新版本到 /")
    applied_count = 0
    for fi in files:
        rel = fi["path"]
        src = _OTA_NEW_DIR + "/" + rel
        dst = "/" + rel
        if not _exists(src):
            print(f"[OTA] 新文件缺失: {src}")
            continue
        _ensure_dir(dst)
        # 目标如果已存在(备份失败的情况), 先删
        try:
            os.remove(dst)
        except OSError:
            pass
        try:
            os.rename(src, dst)
            applied_count += 1
        except OSError as e:
            print(f"[OTA] 应用失败 {dst}: {e}")
    print(f"[OTA] apply: {applied_count}/{len(files)} files")

    # 4. 清空 _ota_new/ 残壳
    _cleanup_dir(_OTA_NEW_DIR)

    # 5. 标记 NVM first_boot_flag (版本号随 code.py/fw_version.py 走, 不写 config)
    print(f"[OTA] 阶段 3/3: 写 NVM first_boot + 重启")
    ota_nvm.set_first_boot_flag(True)
    ota_nvm.reset_download_state()  # 清断点续传状态

    # 6. 重启 — in_progress marker 留到自检通过再清, 是 safemode 的兜底信号
    print(f"[OTA] 切换完成, 重启中...")
    time.sleep(0.5)  # 让日志刷出去
    microcontroller.reset()


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


# ── 提供给 code.py 启动自检 / safemode 用的对外接口 ──────────

def restore_usb_mode_after_ota():
    """OTA 结束后恢复原始 USB 模式 (OTA 期间可能从 flash 临时切了 daily).

    有 pending 原始模式且与当前不同 → 切回 nvm[0] + reboot (不返回).
    无 pending → 清状态后直接返回.
    """
    import microcontroller
    orig = ota_nvm.get_ota_orig_mode()
    ota_nvm.clear_ota_resume()
    ota_nvm.clear_ota_orig_mode()
    if orig is None:
        return
    try:
        if microcontroller.nvm[0] != orig:
            print(f"[OTA] 恢复 USB 模式 nvm0: {microcontroller.nvm[0]} → {orig}, reboot")
            microcontroller.nvm[0] = orig
            time.sleep(0.5)
            microcontroller.reset()
    except Exception as e:
        print(f"[OTA] restore usb mode err: {e}")


def commit_after_selftest():
    """新固件自检通过后调用: 清 first_boot_flag + 删 backup + 删 in-progress marker"""
    print("[OTA] commit: 自检通过, 清理回滚资源")
    ota_nvm.set_first_boot_flag(False)
    _cleanup_dir(_OTA_BACKUP_DIR)
    try:
        os.remove(_OTA_IN_PROGRESS_MARKER)
    except OSError:
        pass
    # 恢复 OTA 前的原始 USB 模式 (从 flash 临时切 daily 的, 这里切回 + reboot)
    restore_usb_mode_after_ota()


def rollback_from_backup():
    """safemode 调用: 把 /_ota_backup/ 内容 mv 回 /

    返回回滚的文件数. 调用方负责后续 reset.
    """
    if not _exists(_OTA_BACKUP_DIR):
        return 0

    print("[OTA] rollback: 从 backup 恢复旧版本")
    restored = 0

    def _walk_restore(rel_root):
        nonlocal restored
        bk_dir = _OTA_BACKUP_DIR + rel_root
        try:
            entries = os.listdir(bk_dir)
        except OSError:
            return
        for name in entries:
            bk_path = bk_dir + "/" + name
            try:
                st = os.stat(bk_path)
            except OSError:
                continue
            if st[0] & 0x4000:  # dir
                _walk_restore(rel_root + "/" + name)
            else:
                dst = "/" + (rel_root + "/" + name).lstrip("/")
                _ensure_dir(dst)
                try:
                    os.remove(dst)
                except OSError:
                    pass
                try:
                    os.rename(bk_path, dst)
                    restored += 1
                except OSError as e:
                    print(f"[OTA] rollback failed {dst}: {e}")

    _walk_restore("")
    _cleanup_dir(_OTA_BACKUP_DIR)
    ota_nvm.set_first_boot_flag(False)
    try:
        os.remove(_OTA_IN_PROGRESS_MARKER)
    except OSError:
        pass
    print(f"[OTA] rollback: {restored} 文件已恢复")
    return restored


def is_in_progress():
    """是否处在切换中 (有 marker 但 first_boot_flag 未置 = 切换被中断)"""
    return _exists(_OTA_IN_PROGRESS_MARKER)
