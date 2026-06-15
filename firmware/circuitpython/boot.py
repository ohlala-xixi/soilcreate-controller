# boot.py - ESP32-S3
# uses NVM 控制 USB readwrite模式
# nvm[0] = 17: 设备可readwrite(daily mode)，电脑readonly
# nvm[0] = 其他value: 电脑可readwrite(flash mode)，设备readonly

import usb_cdc
import usb_hid
import usb_midi
import storage
import microcontroller
import digitalio

# ============================================================
# 启动时立即设置所有输出 GPIO 到安全电平
# 防止深度休眠醒来后 GPIO 浮空导致外设误动作
# 基于原理图 SCH_Schematic1_2026-05-11
# ============================================================

def _init_safe_gpio():
    """将所有输出引脚设为安全电平 (在 code.py 驱动初始化前)
    每个引脚独立 try/except，避免单个引脚冲突导致启动失败
    """
    _safe_pins = []

    _pin_list = [
        # RS485 VCC (VOUTCTR) → LOW (传感器断电, 上电默认全断)
        (microcontroller.pin.GPIO37, "COM1_VCC(VOUTCTR4)"),
        (microcontroller.pin.GPIO35, "COM2_VCC(VOUTCTR3)"),
        # RS485 DE → LOW (接收模式)
        (microcontroller.pin.GPIO38, "COM1_DE(485CTR4)"),
        (microcontroller.pin.GPIO45, "COM2_DE(485CTR3)"),
        # RS485 SCAN → LOW (各通道独立)
        (microcontroller.pin.GPIO36, "COM1_SCAN(SCAN4)"),
        (microcontroller.pin.GPIO48, "COM2_SCAN(SCAN3)"),
        # 注: W5500 ETH_RST = GPIO3 是 ESP32-S3 strapping 脚 (JTAG 源), 这里故意**不**驱动 —
        #     不在 boot 早期碰 strapping 脚; W5500 复位交给 drivers/ethernet.py 初始化时的
        #     标准脉冲 (低→0.1s→高→0.5s) 处理。以太网默认禁用, ETH_RST 悬空无害。
        # 4G 电源 → LOW (默认关闭, V4G_CTRL 旧板 GPIO42 → 新板 GPIO14)
        (microcontroller.pin.GPIO14, "4G_PWR(V4G_CTRL)"),
        # 12V boost 使能 → LOW (默认关, 省电; 用前 code.py 再开)
        (microcontroller.pin.GPIO39, "BOOST_EN(SPWALLON)"),
        # 电流 sense LDO 使能 → LOW (默认关, 读电流前再开)
        (microcontroller.pin.GPIO40, "CURCTR"),
    ]

    ok_count = 0
    for pin, name in _pin_list:
        try:
            p = digitalio.DigitalInOut(pin)
            p.direction = digitalio.Direction.OUTPUT
            p.value = False
            _safe_pins.append(p)
            ok_count += 1
        except Exception as e:
            print(f"[BOOT] GPIO {name} 跳过: {e}")

    # 释放所有引脚 (code.py 的驱动会重新初始化)
    for p in _safe_pins:
        p.deinit()

    print(f"[BOOT] N8R2 GPIO 安全初始化: {ok_count}/{len(_pin_list)}")

_init_safe_gpio()
print("[BOOT] GPIO 安全初始化完成")

# ============================================================
# 深睡唤醒后恢复 V3.3 供电 (释放 CTRL_3V3/GPIO13 的深睡 hold) — 最早一步, 防御性
# ★ 深睡前 code.py 把 GPIO13 驱低+hold 关了 V3.3 (485 收发器/W5500 电源) 省电。GPIO13 是 RTC 脚,
#   hold 芯片级、会延续到唤醒之后; 这里第一时间创建 DigitalInOut 释放 hold + 驱高恢复 V3.3。
#   (code.py 的 _init_new_hw_pins 也会再驱高管理之, 这里是更早的双保险。)
#   解法: 创建 GPIO13 的 DigitalInOut 即释放 hold, 驱高恢复 V3.3, 再 deinit 让 R17 维持 +
#         留给 code.py 深睡时重新驱低。上电首启时这步无害 (本就该开 V3.3)。
# ⚠注: 当年"深睡醒来全 W"曾归因于"此 hold 没释放→V3.3 锁关", 但 2026-06-15 重判: 更可能是
#   传感器上电时序 (冷传感器开机要 ~1.5s, 旧采集只等 0.6s; 死收发器和没启动的传感器同样 no resp,
#   当年没实测 V3.3 电压)。此释放仍保留作防御; 真要复现请先确保足够 settle 再下结论。
# ============================================================
try:
    _v33 = digitalio.DigitalInOut(microcontroller.pin.GPIO13)  # 构造即释放深睡 hold
    _v33.direction = digitalio.Direction.OUTPUT
    _v33.value = True            # 高 = 开 V3.3 (ME6211 CE 高有效)
    _v33.deinit()                # 释放对象, R17 上拉维持 V3.3 高; code.py 深睡可再拿此脚
    print("[BOOT] V3.3(CTRL_3V3/GPIO13) 已恢复 (释放深睡 hold)")
except Exception as _e33:
    print(f"[BOOT] CTRL_3V3 恢复失败: {_e33}")

# ============================================================
# OTA 切换中断电的兜底回滚
# _apply_update 把旧文件挪去 /_ota_backup 再放新文件, 中间断电 → / 上可能没有
# code.py → CircuitPython 安静掉 REPL, safemode 不触发 → 只有 boot.py 必跑,
# 在这里检测 marker 且 first_boot 标志未置 (= 切换没走完) 就地恢复 backup。
# (marker 在 + nvm[5]=0x01 = 切换已完成在自检, 不动 — 自检失败由 code.py/safemode 回滚)
# ============================================================

def _ota_rollback_guard():
    import os
    _MARKER = "/_ota_in_progress"
    _BACKUP = "/_ota_backup"
    _NEW = "/_ota_new"
    try:
        os.stat(_MARKER)
    except OSError:
        return  # 无 marker, 正常启动
    if microcontroller.nvm[5] == 0x01:
        return  # 切换已完成, 处于首启自检窗口, 放行

    print("[BOOT] OTA 切换中断电! 回滚 /_ota_backup → /")
    try:
        storage.remount("/", readonly=False)
    except Exception as e:
        print(f"[BOOT] remount rw 失败: {e}, 无法回滚")
        return

    restored = 0

    def _walk(rel):
        nonlocal restored
        bk = _BACKUP + rel
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
                # 确保目标目录存在
                d = dst.rsplit("/", 1)[0]
                if d:
                    sub = ""
                    for p in d.strip("/").split("/"):
                        sub += "/" + p
                        try:
                            os.stat(sub)
                        except OSError:
                            try:
                                os.mkdir(sub)
                            except OSError:
                                pass
                try:
                    os.remove(dst)
                except OSError:
                    pass
                try:
                    os.rename(bk_path, dst)
                    restored += 1
                except OSError as e:
                    print(f"[BOOT] restore {dst} fail: {e}")

    def _rmtree(path):
        try:
            st = os.stat(path)
        except OSError:
            return
        if st[0] & 0x4000:
            try:
                for name in os.listdir(path):
                    _rmtree(path + "/" + name)
                os.rmdir(path)
            except OSError:
                pass
        else:
            try:
                os.remove(path)
            except OSError:
                pass

    try:
        _walk("")
        _rmtree(_BACKUP)
        _rmtree(_NEW)
        try:
            os.remove(_MARKER)
        except OSError:
            pass
        # 恢复 OTA 前的 USB 模式 (nvm[11]/[12] 与 app/ota_nvm.py 一致)
        if microcontroller.nvm[11] != 0xFF:
            microcontroller.nvm[0] = microcontroller.nvm[11]
            microcontroller.nvm[11] = 0xFF
            microcontroller.nvm[12] = 0x00
        print(f"[BOOT] OTA 回滚完成: {restored} 文件")
    finally:
        # 还原默认挂载 (设备只读/host 可写); daily 模式在下方按 nvm[0] 重新 remount
        try:
            storage.remount("/", readonly=True)
        except Exception:
            pass

_ota_rollback_guard()

# ============================================================
# USB 配置
# ============================================================

usb_hid.disable()
usb_midi.disable()
usb_cdc.enable(console=False, data=True)

# NVM 布局 (全局, 详见 app/ota_nvm.py 与 app/config_mgr.py):
#   nvm[0]      = USB 模式标志 (17=daily, 其他=flash)
#   nvm[1:16]   = OTA / safemode / boot 阶段等系统 flag
#   nvm[16:32]  = 预留
#   nvm[32:]    = ConfigManager 数据 (长度头 + JSON)
nvm_value = microcontroller.nvm[0]
daily_mode = (nvm_value == 17)  # 17 = daily mode（设备writable）

if daily_mode:
    # daily mode：设备可readwrite，电脑readonly
    storage.remount("/", readonly=False)
    print(f"[BOOT] daily mode(nvm={nvm_value}) - 设备可readwrite，电脑readonly")
    print("[BOOT] Data CDC enabled. USB mass storage read only.")
else:
    # flash mode：电脑可readwrite，设备readonly
    print(f"[BOOT] flash mode(nvm={nvm_value}) - 电脑可readwrite，设备readonly")
    print("[BOOT] Data CDC enabled. USB mass storage read & write.")
