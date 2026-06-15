# code.py - ESP32-S3 柔性测斜仪控制器主程序
# CircuitPython 同步版本 (不使用 asyncio)

import time
import gc
import usb_cdc
import board
import microcontroller
import supervisor

# 禁用 auto-reload，防止 macOS 写入元数据文件触发重启
supervisor.runtime.autoreload = False

# ============================================================
# Log function
# ============================================================

def log(msg: str):
    """Output to serial and Data CDC"""
    print(msg)
    if usb_cdc.data:
        try:
            usb_cdc.data.write((msg + "\r\n").encode())
        except:
            pass

# 等待 USB 稳定
time.sleep(0.5)
log("=== CircuitPython 启动中 ===")

# ============================================================
# importmod
# ============================================================

try:
    from app.config_mgr import ConfigManager
    log("[BOOT] ConfigManager OK")
    from app.data_formatter import DataFormatter
    log("[BOOT] DataFormatter OK")
    from app.upload_counter import UploadCounter
    log("[BOOT] UploadCounter OK")
    
    from drivers.led import LEDDriver
    log("[BOOT] LEDDriver OK")
    from drivers.voltage import VoltageMonitor
    log("[BOOT] VoltageMonitor OK")
    
    from lib.private_v2026 import PrivateProtocolV2026
    log("[BOOT] PrivateProtocolV2026 OK")
    from lib.modbus_rtu import ModbusRTU
    log("[BOOT] ModbusRTU OK")
    from lib.modbus_level_jk import ModbusLevelJK
    log("[BOOT] ModbusLevelJK OK")

    # 协议注册表: name -> class. 加新协议在这里加一行 (子类自己声明 NAME / ADDR_MIN / ADDR_MAX / SCAN_MAX)
    PROTOCOL_REGISTRY = {
        PrivateProtocolV2026.NAME: PrivateProtocolV2026,
        ModbusRTU.NAME: ModbusRTU,
        ModbusLevelJK.NAME: ModbusLevelJK,
    }

    def make_protocol(name, driver):
        cls = PROTOCOL_REGISTRY.get(name)
        if cls is None:
            log(f"[Protocol] 未知协议 '{name}', 退回 PRIVATE_V2026")
            cls = PrivateProtocolV2026
        return cls(driver)
    
    log("[启动] 模块加载完成")
except Exception as e:
    log(f"[启动错误] {e}")
    while True:
        time.sleep(1)

# ============================================================
# Verinfo
# ============================================================

FIRMWARE_VERSION = "2026.06.04"  # fallback, 实际从 config.json 读取

# ============================================================
# Helper functions
# ============================================================

def file_exists(path):
    import os
    try:
        os.stat(path)
        return True
    except OSError:
        return False

# ── 4G PEN(GPIO14 / V4G_CTRL) 空闲驱低 ─────────────────────────────
# 假设高=开 / 低=关 (M5V 常供, GPIO14 只是 PEN 使能)。4G 不在用时把 PEN 驱低并占住,
# 模块就不会因引脚浮空被持续使能 (空转 / 网络灯一直闪)。真要连 4G 时 _make_modem
# 先释放占用, 让 modem 驱动接管 PEN 驱高上电。
# 注: 2026-06-15 起深睡 hold PEN(GPIO14) 低 (preserve_dios, 见深睡段) → 4G 灯灭+省电;
#     唤醒靠重建 DigitalInOut 释放 hold。若实测 hold 唤醒解不开 (4G 拉不起), 退回硬件:
#     V4G_CTRL 加下拉电阻 (浮空=低=关)。
_modem_pwr_idle = None

def modem_pwr_idle_low():
    """占住 PEN(GPIO14) 并驱低 = 关 4G 模块。4G 不用时调; 已被 modem 驱动占用则静默跳过。
    返回当前驱低的 DigitalInOut (供深睡 preserve_dios 用), 失败/无法占用时返回 None。"""
    global _modem_pwr_idle
    if _modem_pwr_idle is not None:
        return _modem_pwr_idle
    try:
        import digitalio
        import pins as _pins
        _modem_pwr_idle = digitalio.DigitalInOut(_pins.MODEM_PWR)
        _modem_pwr_idle.direction = digitalio.Direction.OUTPUT
        _modem_pwr_idle.value = False
    except Exception as e:
        log(f"[4G] PEN 空闲驱低占用失败 (可能正被 modem 用): {e}")
    return _modem_pwr_idle

def modem_pwr_release_for_use():
    """释放 PEN 占用, 交给 modem 驱动自己驱高上电。连 4G / 进 4g_test 前调 (幂等)。"""
    global _modem_pwr_idle
    if _modem_pwr_idle is not None:
        try:
            _modem_pwr_idle.deinit()
        except Exception:
            pass
        _modem_pwr_idle = None

def _make_modem(config, log_func):
    """4G 驱动 factory — 按 config 选 A7670C_yundtu / A7670G"""
    modem_pwr_release_for_use()   # 让 modem 驱动接管 PEN 上电 (释放空闲驱低占用)
    kind = config.get("network.4g.modem", "A7670C_yundtu")
    if kind == "A7670G":
        from drivers.modem_a7670g import ModemA7670G
        return ModemA7670G(config, log_func)
    from drivers.modem_4g import Modem4G
    return Modem4G(config, log_func)

def get_interval_seconds(preset, custom_min=60):
    """getInterval (s)

    preset=0 : 待命, 不采集只等待
    preset=99: 自定义, 用 custom_min 分钟 (来自 system.interval_custom_min)
    其它     : 固定档位
    """
    if preset == 99:
        try:
            return max(int(custom_min), 1) * 60
        except (TypeError, ValueError):
            return 60 * 60
    preset_map = {
        0: 0,  # 待命
        1: 5 * 60, 2: 10 * 60, 3: 15 * 60, 4: 30 * 60, 5: 60 * 60,
        6: 120 * 60, 7: 240 * 60, 8: 720 * 60, 9: 1440 * 60,
    }
    return preset_map.get(preset, 60 * 60)

def get_aligned_scheduled_time(interval_sec):
    """获取当前周期对齐的准点时间
    
    例: interval=3600 (1h), 当前 12:52 → 返回 12:00 的 epoch
    例: interval=900 (15min), 当前 12:17 → 返回 12:15 的 epoch
    """
    if interval_sec <= 0:
        return int(time.time())
    now = int(time.time())
    return now - (now % interval_sec)

def get_sleep_until_next_boundary(interval_sec):
    """计算到下一个准点的秒数
    
    例: interval=3600 (1h), 当前 12:52:30 → 返回 450 (到 13:00)
    例: interval=900 (15min), 当前 12:17:00 → 返回 480 (到 12:30 的 13min)
    """
    if interval_sec <= 0:
        return 0
    now = int(time.time())
    next_boundary = now - (now % interval_sec) + interval_sec
    remaining = next_boundary - now
    return max(remaining, 30)  # 至少 30 秒

# 时间同步
_last_send_day = -1  # 上次发送的日期 (yday)，内存中记录
_last_upload_ok = False  # do_network_upload 最近一次是否真的发上去了 (远程配置 verify/commit 用)
_wifi_downlink = None    # WiFi 上传成功后保留的 WiFiDriver (读 cirpy-info 下行/srv_ack, 用完 disconnect)
# 设备当地时区偏移 (秒). RTC 存的是网络当地时间, 上传时 time = 当地 - 偏移 = 真 UTC (全球通用)。
# 默认 +8h (北京); 4G 对时会用基站给的实际时区覆盖 (try_time_sync); WiFi/NTP 无时区信息保持默认。
_device_tz_offset_s = 8 * 3600
_force_collect_now = False  # BLE read_sensors 触发: 跳过 ble.is_connected() 中断检查, 强制完成本轮采集+上传
_force_collect_com = None   # CDC #read [com] 触发: None=全采, 1/2=本轮只采该 COM, 采完清零
_live_read_active = False   # CDC #read_live 触发: 连续读会话进行中 → 启动交互窗口不因 60s 空闲超时去采集/睡眠
                            # (否则采集循环 boost_release 会断掉 #read_live 保持的 12V); #read_live_off 清零

# 05-11 新硬件: SPWALLON (12V boost EN) + CURCTR (电流sense LDO EN) + 共享 VoltageMonitor 句柄
# 在 main() 初始化, 各 cmd handler 通过 module-level 引用 (避免改 process_commands 签名)
_boost_en_pin = None
_curctr_pin = None
_v33_pin = None    # CTRL_3V3 (GPIO13) 句柄 — 唤醒驱高恢复 V3.3 (+释放深睡 hold), 深睡前驱低 hold 省电
_voltage_monitor = None
_rs485mod = None   # drivers.rs485 模块引用 (main() 注入 set_boost 后, 采集循环用它 boost_acquire/release)
# CTRL_3V3 (GPIO13) → LDO2(ME6211) CE: 高=开 V3.3 外设电源 (485收发器/W5500)。
# 【2026-06-15 改】之前运行时不驱动 (靠 R17 默认上电); 现为支持深睡 hold 省电, 改成 _init_new_hw_pins
# 唤醒第一时间驱高 (恢复 V3.3 + 释放上周期深睡 hold), 深睡前驱低 + hold (关 485/W5500 省电) — 见 deep sleep 段。

def _init_new_hw_pins():
    """初始化 05-11 新硬件 GPIO (CTRL_3V3 / BOOST_EN / CURCTR) — 由 main() 调用一次 (RS485 init 前)"""
    global _boost_en_pin, _curctr_pin, _v33_pin
    import digitalio
    import pins
    # ★ 必须第一时间做: 唤醒后立刻重建 CTRL_3V3(GPIO13) 并驱高 = 恢复 V3.3(485收发器/W5500)
    #   + 释放上个周期深睡留下的 hold。务必在 RS485 init / 任何读传感器之前。
    #   首次上电 (非深睡醒来): GPIO13 本就 R17 上拉=高, 驱高无变化。
    try:
        _v33_pin = digitalio.DigitalInOut(pins.CTRL_3V3)
        _v33_pin.direction = digitalio.Direction.OUTPUT
        _v33_pin.value = True   # 高 = 开 V3.3
        log("[HW] V3.3(GPIO13) 驱高 = 开 (恢复+释放深睡 hold)")
    except Exception as e:
        log(f"[HW] V3.3(GPIO13) init fail: {e}")
    try:
        _boost_en_pin = digitalio.DigitalInOut(pins.BOOST_EN)
        _boost_en_pin.direction = digitalio.Direction.OUTPUT
        _boost_en_pin.value = False
    except Exception as e:
        log(f"[HW] BOOST_EN init fail: {e}")
    try:
        _curctr_pin = digitalio.DigitalInOut(pins.CURCTR)
        _curctr_pin.direction = digitalio.Direction.OUTPUT
        _curctr_pin.value = False
    except Exception as e:
        log(f"[HW] CURCTR init fail: {e}")


def set_boost(enabled: bool):
    """打开/关闭 12V boost (SPWALLON)"""
    if _boost_en_pin is None:
        return
    _boost_en_pin.value = bool(enabled)


def read_system_current() -> int:
    """读系统总电流 (mA) — 自动开 CURCTR LDO, 读完关掉省电"""
    if _voltage_monitor is None or _curctr_pin is None:
        return 0
    import time
    _curctr_pin.value = True
    time.sleep(0.02)   # 给 LDO + ACS712 上电稳定时间
    i_ma = _voltage_monitor.read_current()
    _curctr_pin.value = False
    return i_ma


def _parse_gsm_time(time_str):
    """解析 GSM 时间 'YY/MM/DD,HH:MM:SS±zz' → (struct_time_当地, 时区偏移秒数 或 None)

    ±zz 是 3GPP 时区, 单位 15 分钟 (quarter-hour): +32 = +8h = UTC+8 (北京)。
    基站在哪个国家就给哪个时区 → 全球自动正确。
    返回的 struct_time 是**网络当地时间**; 偏移用于算 UTC (UTC = 当地 - 偏移)。
    日期用 '/' 分隔, 所以任何 '+'/'-' 都是时区符号。
    两位年份 pivot=70: yy∈[00,69]→20yy, yy∈[70,99]→19yy; 模块未对时默认 70/01/01.
    解析失败返回 (None, None); 无时区字段返回 (struct_time, None)。
    """
    try:
        time_str = time_str.strip('"')
        offset_qh = None
        if '+' in time_str:
            core, zz = time_str.rsplit('+', 1)
            try:
                offset_qh = int(zz)
            except Exception:
                pass
        elif '-' in time_str:
            core, zz = time_str.rsplit('-', 1)
            try:
                offset_qh = -int(zz)
            except Exception:
                pass
        else:
            core = time_str
        date_part, time_part = core.split(',')
        yy, mm, dd = map(int, date_part.split('/'))
        if yy >= 100:
            year = yy
        elif yy >= 70:
            year = 1900 + yy
        else:
            year = 2000 + yy
        hh, mi, ss = map(int, time_part.split(':'))
        st = time.struct_time((year, mm, dd, hh, mi, ss, 0, -1, -1))
        off_s = offset_qh * 900 if offset_qh is not None else None   # 15min = 900s
        return (st, off_s)
    except:
        return (None, None)

def _set_rtc_time(t):
    """设置 RTC 时间"""
    import rtc
    rtc.RTC().datetime = t
    now = time.localtime()
    log(f"[时间] 已同步: {now.tm_year}/{now.tm_mon:02d}/{now.tm_mday:02d} {now.tm_hour:02d}:{now.tm_min:02d}:{now.tm_sec:02d}")

def try_time_sync(config, force=False):
    """发送数据前对时，优先级: 4G模块时间 > WiFi NTP > ETH NTP
    触发条件: 年份 < 2026 或 当前日期 != 上次发送日期
    只尝试已启用的方式
    
    Returns: (synced: bool, modem_instance_or_None)
      如果通过 4G 对时成功，返回已连接的 modem 实例供后续 upload 复用
      避免重复创建 UART 导致 IO44 in use
    """
    global _last_send_day
    now = time.localtime()
    
    needs_sync = force
    if now.tm_year < 2026:
        log(f"[时间] 年份 {now.tm_year} < 2026，需要对时")
        needs_sync = True
    elif _last_send_day >= 0 and now.tm_yday != _last_send_day:
        log(f"[时间] 日期变更 (上次:{_last_send_day} 今天:{now.tm_yday})，需要对时")
        needs_sync = True
    
    if not needs_sync:
        return (False, None)
    
    # 1. 4G 模块时间 — 成功后保留 modem 实例供 upload 复用
    if config.get("network.4g.enabled", False):
        modem = None
        try:
            modem = _make_modem(config, log)
            if modem.connect():
                gsm_time = modem.get_network_time()
                if gsm_time:
                    parsed, tz_off = _parse_gsm_time(gsm_time)
                    # 年份必须在 [2026, 2037] 内: CircuitPython time.time() 使用 32 位有符号,
                    # 2038-01-19 之后溢出; 同时过滤模块未对时的默认值 (70/01/01 = 1970)
                    if parsed and 2026 <= parsed.tm_year <= 2037:
                        _set_rtc_time(parsed)   # RTC = 网络当地时间 (设备日志/clock 显示当地)
                        # 抓到当地时区偏移 → 后续算真 UTC 上传 (全球通用)
                        if tz_off is not None:
                            global _device_tz_offset_s
                            _device_tz_offset_s = tz_off
                            log(f"[时间] 4G 当地时区 UTC{tz_off // 3600:+d}h (上传转 UTC)")
                        log("[时间] 4G 模块对时成功")
                        # 不 deinit，返回 modem 供 upload 复用
                        return (True, modem)
                    else:
                        log(f"[时间] 4G 模块时间无效 (year={parsed.tm_year if parsed else '?'})，尝试 NTP")
                # connect 成功但对时失败，仍然返回 modem 供 upload 用
                return (False, modem)
            # connect() 返回 False: 必须释放 — 否则 PEN 挂 HIGH (模块整周期全功率)
            # 且 UART 占用导致后续 do_network_upload 建 UART 报 "in use", 4G 整周期报废
            log("[时间] 4G 连接失败, 释放 modem")
            try:
                modem.deinit()
            except:
                pass
            modem = None
        except Exception as e:
            log(f"[时间] 4G 对时失败: {e}")
            if modem:
                try:
                    modem.deinit()
                except:
                    pass
    
    # 2. WiFi NTP
    if config.get("network.wifi.enabled", False):
        try:
            import wifi
            import socketpool
            import adafruit_ntp
            
            ssid = config.get("network.wifi.ssid", "")
            pwd = config.get("network.wifi.password", "")
            if ssid:
                if not wifi.radio.connected:
                    wifi.radio.connect(ssid, pwd)
                pool = socketpool.SocketPool(wifi.radio)
                ntp = adafruit_ntp.NTP(pool, tz_offset=8)
                _set_rtc_time(ntp.datetime)
                log("[时间] WiFi NTP 对时成功")
                return (True, None)
        except Exception as e:
            log(f"[时间] WiFi NTP 失败: {e}")
    
    # 3. 有线网络 NTP (仅 N16R2)
    if config.get("network.ethernet.enabled", False) and not pins.IS_OCTAL_PSRAM:
        eth = None
        try:
            from drivers.ethernet import EthernetDriver
            import adafruit_ntp
            import adafruit_wiznet5k.adafruit_wiznet5k_socket as socket

            eth = EthernetDriver(config)
            if eth.connect() and eth._eth:
                # Wiznet5k 需要用自己的 socket 模块
                socket.set_interface(eth._eth)
                ntp = adafruit_ntp.NTP(socket, tz_offset=8)
                _set_rtc_time(ntp.datetime)
                log("[时间] ETH NTP 对时成功")
                return (True, None)
        except Exception as e:
            log(f"[时间] ETH NTP 失败: {e}")
        finally:
            # 释放 SPI/CS/RST — 不释放则本次运行后续 EthernetDriver 实例化必 "in use"
            if eth is not None:
                try:
                    eth.deinit()
                except Exception:
                    pass
    
    log("[时间] 所有对时方式失败，使用内部时钟")
    return (False, None)

def update_last_send_day():
    """记录当前日期为上次发送日期"""
    global _last_send_day
    _last_send_day = time.localtime().tm_yday

def do_address_scan(rs485_drivers: dict, rs485_protocols: dict, config=None):
    """ScanallCH的sensoraddr (AutoID 0-1023)
    
    per inclinometer_client:
    - timeout: 300ms
    - Scaninterval: 200ms
    
    Scan结果会save到 config.json
    """
    log("[Scan] scanning AutoID 0-1023...")

    # byCH分group — 统一走 do_scan_channel (A1 广播 + SCAN 引脚完整时序, 与 BLE 扫描一致;
    # 旧实现缺 A1/SCAN, 协议要求 SCAN 高才响应 A2 时会漏检)
    found_by_channel = {}

    for ch, driver in rs485_drivers.items():
        protocol = rs485_protocols[ch]
        found_by_channel[ch] = do_scan_channel(ch, driver, protocol)

    # save到 config
    total_found = 0
    if config:
        for ch, sensors in found_by_channel.items():
            if sensors:
                config.set(f"rs485_{ch}.sensors", sensors)
                total_found += len(sensors)
        if total_found > 0:
            config.save()
            log(f"[Scan] saved {total_found}  sensors to config.json")
    
    # 输out结果
    log(f"[Scan] done! totalfound {total_found}  sensors")
    
    # 输out JSON 结果到 CDC
    if usb_cdc.data and total_found > 0:
        import json
        # 扁平化输out
        all_devices = []
        for ch, sensors in found_by_channel.items():
            for s in sensors:
                all_devices.append({"channel": ch, "addr": s["addr"]})
        result_json = json.dumps({
            "type": "scan_result",
            "count": total_found,
            "devices": all_devices
        })
        usb_cdc.data.write((result_json + "\r\n").encode())

def do_batch_write_addr(rs485_drivers: dict, rs485_protocols: dict, max_addr: int, config=None, timeout_ms: int = 300):
    """批量wroteaddr (from max_addr start，每ok一addr -1)
    
    流程:
    1. 遍历 AutoID 0-1023
    2. hasrsp的设备wrote当beforeaddr
    3. wroteokafteraddr -1
    
    wrote结果会save到 config.json
    """
    log(f"[WriteAddr] batch write, max_addr: {max_addr}, timeout: {timeout_ms}ms")
    
    # byCH分group
    success_by_channel = {}
    current_addr = max_addr
    
    for ch, driver in rs485_drivers.items():
        protocol = rs485_protocols[ch]
        log(f"[WriteAddr] CH {ch}...")
        
        success_by_channel[ch] = []
        
        # pwr on
        driver.power_on()
        time.sleep(0.3)
        
        for auto_id in range(1024):
            # trywroteaddr
            if protocol.write_address_by_autoid(auto_id, current_addr, timeout_ms=timeout_ms):
                log(f"  [CH{ch}] AutoID {auto_id} -> wroteaddr {current_addr} ok")
                success_by_channel[ch].append({"addr": current_addr})
                current_addr -= 1  # addr -1
            
            # 每 100 输out进度
            if (auto_id + 1) % 100 == 0:
                total_written = sum(len(s) for s in success_by_channel.values())
                log(f"  [CH{ch}] 进度 {auto_id + 1}/1024, donewrote {total_written} ")
            
            # 200ms interval
            time.sleep(0.2)
        
        log(f"[WriteAddr] CH {ch} done，wrote {len(success_by_channel[ch])} ")
        driver.power_off()
    
    # save到 config
    total_written = 0
    if config:
        for ch, sensors in success_by_channel.items():
            if sensors:
                config.set(f"rs485_{ch}.sensors", sensors)
                total_written += len(sensors)
        if total_written > 0:
            config.save()
            log(f"[WriteAddr] saved {total_written}  sensors to config.json")
    
    # 输out结果
    log(f"[WriteAddr] done! totalwrote {total_written}  sensors")
    
    # 输out JSON 结果到 CDC
    if usb_cdc.data and total_written > 0:
        import json
        # 扁平化输out
        all_devices = []
        for ch, sensors in success_by_channel.items():
            for s in sensors:
                all_devices.append({"channel": ch, "addr": s["addr"]})
        result_json = json.dumps({
            "type": "write_addr_result",
            "count": total_written,
            "devices": all_devices
        })
        usb_cdc.data.write((result_json + "\r\n").encode())

def do_read_sensors(ch: int, driver, protocol, sensors: list, timeout_ms: int = 5000, 
                    interval_ms: int = 150, reverse: bool = True,
                    progress_callback=None) -> list:
    """统一的传感器数据读取函数
    
    供 BLE 和 CDC 命令共用，保证一致的行为。
    日志格式与主循环完全一致。
    
    Args:
        ch: 通道号
        driver: RS485 驱动
        protocol: 协议对象
        sensors: 传感器配置列表 [{"addr": xxx, "model": xxx}, ...]
        timeout_ms: 单个传感器超时时间 (默认 5000ms，参考 inclinometer_client)
        interval_ms: 传感器间读取间隔 (默认 150ms)
        reverse: 是否倒序读取 (从顶部开始，默认 True)
        progress_callback: 进度回调 fn(index, total, addr, data_or_none)
        
    Returns:
        读取结果列表 [{"addr", "a", "b", "z", "status", ...}, ...]
    """
    all_data = []

    log(f"[CH{ch}] reading {len(sensors)}  sensors...")

    # 电源开启 + 传感器开机稳定
    #  ★ 2026-06-15: 冷启 (深睡醒来/首次, VOUTCTR 之前是关的) 给足 1.5s — 实测 (传感器读数 tab /
    #  #12v_on) 这批传感器开机要 ~1.5s, 只等 0.3s 会读不到 → 全 W。之前深睡醒来"全 W"误判成
    #  "V3.3 hold 没释放", 其实多半是这个上电时序: 死收发器和没启动的传感器症状一样 (都 no resp)。
    #  已上电 (幂等 power_on 没真上电) 只需短稳定。
    _was_cold = not driver._power_on
    driver.power_on()
    time.sleep(1.5 if _was_cold else 0.3)
    
    # 按顺序读取
    sensor_list = list(reversed(sensors)) if reverse else sensors
    success_count = 0
    fail_count = 0
    
    for i, sensor_cfg in enumerate(sensor_list):
        addr = sensor_cfg.get("addr", 0)
        model = sensor_cfg.get("model", 0)
        
        try:
            log(f"  [CH{ch}] readaddr {addr}...")
            data = protocol.read_data(addr, timeout_ms=timeout_ms)
            if data:
                data["channel"] = ch
                data["model"] = model
                all_data.append(data)
                success_count += 1
                log(f"  [CH{ch}] addr {addr}: A={data.get('a', 0):.2f}, B={data.get('b', 0):.2f}")
            else:
                fail_count += 1
                # 失败的传感器，status 标记为 W
                all_data.append({
                    "addr": addr,
                    "address": addr,
                    "channel": ch,
                    "model": model,
                    "a": 0, "b": 0, "z": 0,
                    "status": "W"
                })
                log(f"  [CH{ch}] addr {addr}: no resp")
        except Exception as e:
            fail_count += 1
            all_data.append({
                "addr": addr,
                "address": addr,
                "channel": ch,
                "model": model,
                "a": 0, "b": 0, "z": 0,
                "status": "W"
            })
            log(f"  [CH{ch}] addr {addr} Err: {e}")
        
        # 进度回调
        if progress_callback:
            progress_callback(i, len(sensors), addr, all_data[-1] if all_data else None)
        
        # 间隔延时
        time.sleep(interval_ms / 1000.0)
    
    # 电源关闭
    driver.power_off()
    log(f"[CH{ch}] done: ok {success_count}, fail {fail_count}")
    
    return all_data

def do_scan_channel(ch: int, driver, protocol, timeout_ms: int = 300, 
                    interval_ms: int = 200, progress_callback=None) -> list:
    """统一的单通道地址扫描函数
    
    供 BLE 和 CDC 命令共用。
    流程：power_on → 遍历A2 → power_off
    """
    found = []
    
    log(f"[Scan] CH {ch}...")
    driver.power_on()
    time.sleep(0.3)
    
    # A1 广播：让所有传感器重新计算 AutoID
    a1_frame = protocol._build_frame(0x00A1)
    driver.send(a1_frame)
    time.sleep(0.5)  # A1 无响应，等 500ms 让传感器处理
    
    # SCAN 拉高，整个扫描期间保持
    driver.set_address_scan(True)
    time.sleep(0.01)
    
    for auto_id in range(1024):
        result = protocol.scan_address(auto_id, timeout_ms=timeout_ms)
        if result:
            addr = result["fixed_addr"]
            found.append({"addr": addr})
            log(f"  [CH{ch}] AutoID {auto_id} -> addr {addr}")
        
        # 进度回调
        if progress_callback:
            progress_callback(auto_id, found)
        
        # 每 100 个输出进度
        if (auto_id + 1) % 100 == 0:
            log(f"  [CH{ch}] 进度 {auto_id + 1}/1024, found {len(found)} ")
        
        time.sleep(interval_ms / 1000.0)
    
    # 扫描全部完成后再关 SCAN
    driver.set_address_scan(False)
    time.sleep(0.01)
    
    driver.power_off()
    log(f"[Scan] CH {ch} done, found {len(found)} ")
    
    return found

def do_network_upload(config, segments: list, existing_modem=None):
    """通过netUploaddata (4G > WiFi > Ethernet)
    
    by优先级trysend，ok则return.
    Args:
        existing_modem: 已有的 4G modem 实例（从 try_time_sync 传入），
                       避免重复创建 UART 导致 IO44 in use
    Returns: 4G modem instance if used (for PSM), else None
    """
    global _last_upload_ok, _wifi_downlink
    _last_upload_ok = False  # 本次默认失败, 任一通道发成功再置 True
    _wifi_downlink = None    # WiFi 上传成功时保留 driver 供下行读取 (cirpy-info/srv_ack)
    if not segments:
        if existing_modem:
            try:
                existing_modem.deinit()
            except:
                pass
        return None

    topic = config.get("network.mqtt_topic", "controllerdata-cirpy")

    # try 4G
    if config.get("network.4g.enabled", False):
        modem = existing_modem  # 复用已有实例
        try:
            if modem is None:
                modem = _make_modem(config, log)
                # light 连接: 干净 PEN 冷启动, 不 +++ → 这趟既上传又能收 retained (零 flash)。
                # (heavy 的对时连接走 existing_modem 复用, 不进这分支)
                log("[Upload] 4G connecting (light)...")
                if not modem.connect(light=True):
                    log("[Upload] 4G connect failed")
                    modem.deinit()
                    modem = None

            if modem and modem._connected:
                log("[Upload] 4G sending...")
                success = True
                for seg in segments:
                    if not modem.publish(topic, seg):
                        success = False
                        break
                if success:
                    log(f"[Upload] 4G sent {len(segments)} seg")
                    _last_upload_ok = True
                    return modem  # 返回 modem 实例，供下行读取 (handle_remote)
                else:
                    log("[Upload] 4G send fail")
        except Exception as e:
            log(f"[Upload] 4G err: {e}")
        # 任何没把 modem 作为返回值交出的路径 (publish 失败/异常) 都必须释放 —
        # 否则 PEN 挂 HIGH 整个休眠期 + UART 占用导致下周期 "IO44 in use"。
        # (旧代码 finally 只在 "not connected" 时释放, 已连接的失败路径全漏)
        if modem:
            try:
                modem.deinit()
            except:
                pass
            modem = None

    # try WiFi
    if config.get("network.wifi.enabled", False):
        try:
            from drivers.wifi import WiFiDriver
            wifi = WiFiDriver(config)
            log("[Upload] try WiFi...")
            if wifi.connect():
                log("[Upload] WiFi+MQTT connected!")
                # 先订阅下行: 服务器收到数据立刻回 srv_ack (非 retained), 订阅晚了错过
                wifi.enable_downlink()
                success = True
                for seg in segments:
                    if not wifi.publish(topic, seg):
                        success = False
                        break
                if success:
                    log(f"[Upload] WiFi sent {len(segments)} seg")
                    _last_upload_ok = True
                    _wifi_downlink = wifi  # 保留连接, 调用方读完下行后 disconnect
                    return None
                else:
                    log("[Upload] WiFi send fail")
                    wifi.disconnect()
            else:
                log(f"[Upload] WiFi fail: {wifi.last_error}")
        except Exception as e:
            log(f"[Upload] WiFi err: {e}")
    
    # try Ethernet (双重保险: 板型 + config)
    if config.get("network.ethernet.enabled", False):
        if pins.IS_OCTAL_PSRAM:
            log("[Upload] N16R8 板型, ETH 引脚不可用, 跳过")
        else:
            eth = None
            try:
                from drivers.ethernet import EthernetDriver
                eth = EthernetDriver(config)
                if eth.connect():
                    log("[Upload] usesETH...")
                    success = True
                    for seg in segments:
                        if not eth.publish(topic, seg):
                            success = False
                            break
                    if success:
                        log(f"[Upload] ETHsent {len(segments)} seg")
                        _last_upload_ok = True
                    else:
                        log("[Upload] ETHsend fail")
            except Exception as e:
                log(f"[Upload] ETHerr: {e}")
            finally:
                # 无论成败都释放 SPI/CS/RST (W5500 不支持下行读取, 无保留价值;
                # 不释放则下次实例化 "in use" + 深睡前 CS/RST 反灌 V3.3)
                if eth is not None:
                    try:
                        eth.deinit()
                    except Exception:
                        pass
            if _last_upload_ok:
                return None

    log("[Upload] noneoknet，仅通过 CDC 输out")
    return None

# 待命模式下隔多久重连 4G 查一次远程配置 (秒)
STANDBY_REMOTE_CHECK_SEC = 900   # 15 分钟

def fetch_remote_in_standby(config, rs485_drivers, heavy=False):
    """待命模式 (interval=0) 下主动上线拉一次远程配置 (4G 优先, 无 4G 走 WiFi).

    待命 = 刚配置完, 不进采集/上传循环 → 不主动连这一下就永远上不了线、
    收不到 cirpy-info/<cid> 的 retained 下发. 连上 (订阅) → 读下行指令应用
    → 上报一次状态 → 等服务器 srv_ack → 释放. 返回 handle_remote 的 sig (或 None)。

    heavy=True (首次): 4G 走 AT+S heavy 连接, 确保 DTU 配置已存进模块 flash (新板必需)。
    heavy=False (之后周期性): 走 light 干净冷启动收 retained, 零 flash 磨损。
    """
    use_4g = config.get("network.4g.enabled", False)
    use_wifi = config.get("network.wifi.enabled", False)
    if not use_4g and not use_wifi:
        log("[待命] 4G/WiFi 均未启用, 跳过远程配置拉取")
        return None
    modem = None
    try:
        if use_4g:
            modem = _make_modem(config, log)
            if heavy:
                log("[待命] 连 4G 拉远程配置 (heavy, 确保 DTU 配置)...")
                _ok = modem.connect(force_reconfigure=True)
            else:
                log("[待命] 连 4G 拉远程配置 (light, 零 flash)...")
                _ok = modem.connect(light=True)
        else:
            # WiFi-only 设备: 连 AP + MQTT, WiFiDriver.read_command 与 modem 同接口
            from drivers.wifi import WiFiDriver
            log("[待命] 连 WiFi 拉远程配置...")
            modem = WiFiDriver(config)
            _ok = modem.connect()
            if _ok:
                modem.enable_downlink()
        if not _ok:
            log("[待命] 连接失败, 稍后重试")
            return None
        from app import remote_cmd as _rc
        from app import remote_cmd_nvm as _rcn
        # 待命拉取给足 12s: 刚重连, retained 可能几秒后才推过来
        sig = _rc.handle_remote(config, modem, rs485_drivers, log, timeout_ms=12000)
        # 上线报一次状态 (让服务器看到设备在线 + applied_rev)
        report_ok = False
        try:
            from app.device_reporter import send_report_via_modem, send_report_via_wifi
            if use_4g:
                report_ok = send_report_via_modem(config, modem)
            else:
                report_ok = send_report_via_wifi(config)
        except Exception as _e:
            log(f"[待命] report err: {_e}")
        # 验证中 → 等服务器对刚才报告的 srv_ack: 这是双向连通的真实证据
        # (4G 透传 publish 写串口必"成功", report_ok 不能作数)
        if _rcn.is_verifying():
            _acked = _rc.last_ack_seen()
            if not _acked and report_ok:
                _c2, _acked = _rc.read_downlink(modem, log, timeout_ms=6000)
                # 等 ack 的窗口里若又来一条新指令, 不丢 — 下个 15min 周期 retained 还在会重收
            if _acked:
                _rc.commit_applied(log)
            else:
                log("[待命] 未收到 srv_ack, 配置保持验证中 (下次拉取再确认)")
        return sig
    except Exception as e:
        log(f"[待命] 远程拉取异常: {e}")
        return None
    finally:
        if modem:
            try:
                if hasattr(modem, "disconnect"):
                    modem.disconnect()   # WiFiDriver
                else:
                    modem.deinit()       # Modem4G
            except Exception:
                pass

# ============================================================
# 4G 调试透传模式 (#4g_test)
# ============================================================
# 启动交互窗口里发 #4g_test → 固件停业务循环, 把 USB CDC 和 4G UART 直连透传,
# 供 PC 工具「网卡调试」tab 直接对 4G 模块发 AT / MQTT 命令现场排错。
# 自包含: 只用 pins/busio/digitalio, 不碰任何业务模块 (排除死锁路径)。永不返回, 退出靠 #reboot。
# 新板 (05-11): MODEM_PWR=GPIO14(V4G_CTRL/PEN), MODEM_TX=GPIO43, MODEM_RX=GPIO44, 115200。

def enter_4g_test_mode(rs485_drivers=None):
    import digitalio
    import busio
    import pins

    def w(s):
        """非阻塞写 CDC data, 失败 drop"""
        try:
            if usb_cdc.data:
                usb_cdc.data.write((s + "\r\n").encode("utf-8"))
        except Exception:
            pass

    supervisor.runtime.autoreload = False
    if usb_cdc.data:
        try:
            usb_cdc.data.timeout = 0
            usb_cdc.data.write_timeout = 0
        except Exception:
            pass

    w("===== 4G 调试透传模式 =====")
    # 释放 RS485 (它们占着 COM UART/电源, 透传期间不需要 — 失败无所谓, 引脚不冲突)
    if rs485_drivers:
        try:
            for _ch in list(rs485_drivers.keys()):
                try:
                    rs485_drivers[_ch].deinit()
                except Exception:
                    pass
            w("[4G_TEST] RS485 已释放")
        except Exception:
            pass

    # 4G 上电 (PEN 拉高; M5V 常供, 这只是唤醒/使能)
    modem_pwr_release_for_use()   # 先释放空闲驱低占用, 否则下面建 DigitalInOut 报 "in use"
    try:
        pwr = digitalio.DigitalInOut(pins.MODEM_PWR)
        pwr.direction = digitalio.Direction.OUTPUT
        pwr.value = True
        w("[4G] PEN HIGH (GPIO14 / V4G_CTRL)")
    except Exception as e:
        w(f"[4G_TEST] PEN 初始化失败: {e} (可能已被占用, 发 #reboot 重来)")
        return

    time.sleep(2)

    try:
        uart = busio.UART(
            pins.MODEM_TX, pins.MODEM_RX,
            baudrate=115200,
            timeout=0,
            receiver_buffer_size=1024,
        )
        w("[4G] UART OK (TX=43 RX=44 115200)")
    except Exception as e:
        w(f"[4G_TEST] UART 初始化失败: {e} (发 #reboot 重来)")
        return

    w("透传开始: 任意行=AT (自动\\r\\n)")
    w("特殊: #reboot/##reboot=ESP32 软复位退出; ##4greboot=只断电重启 4G 模块")
    w("      ##raw <text>=不加换行; ##plus=退数据模式(+++)")
    w("      ##sleep <ms>; ##pwr <0|1>")

    t0 = time.monotonic()
    last_beat = t0
    line_buf = ""

    def handle_line(line):
        if not line:
            return
        low = line.lower()
        if low in ("#reboot", "##reboot"):
            w("[4G_TEST] reboot...")
            time.sleep(0.3)
            microcontroller.reset()
            return
        if low in ("#4greboot", "##4greboot"):
            w("[4G_TEST] 4G 模块断电 1.5s...")
            try:
                pwr.value = False
                time.sleep(1.5)
                pwr.value = True
                w("[4G_TEST] 4G 重新上电, 等 ~10s 启动")
            except Exception as e:
                w(f"[4G_TEST] 4g reboot err: {e}")
            return
        if line.startswith("##"):
            rest = line[2:].strip()
            sp = rest.find(" ")
            op = (rest if sp < 0 else rest[:sp]).lower()
            arg = "" if sp < 0 else rest[sp + 1:]
            if op == "plus":
                try:
                    time.sleep(1.0)
                    uart.write(b"+++")
                    time.sleep(1.5)
                    if uart.in_waiting:
                        uart.read(uart.in_waiting)
                    w("[4G_TEST] +++ done")
                except Exception as e:
                    w(f"[4G_TEST] +++ err: {e}")
                return
            if op == "sleep":
                try:
                    time.sleep(int(arg) / 1000.0)
                    w(f"[4G_TEST] slept {arg}ms")
                except Exception:
                    pass
                return
            if op == "pwr":
                try:
                    pwr.value = bool(int(arg))
                    w(f"[4G_TEST] PEN={pwr.value}")
                except Exception:
                    pass
                return
            if op == "raw":
                try:
                    uart.write(arg.encode("utf-8"))
                    w(f"[4G_TEST] raw {len(arg)}B")
                except Exception as e:
                    w(f"[4G_TEST] raw err: {e}")
                return
            if op == "rawhex":
                try:
                    data = bytes.fromhex(arg.replace(" ", ""))
                    uart.write(data)
                    w(f"[4G_TEST] rawhex {len(data)}B")
                except Exception as e:
                    w(f"[4G_TEST] rawhex err: {e}")
                return
            w(f"[4G_TEST] unknown: {line}")
            return
        try:
            uart.write((line + "\r\n").encode("utf-8"))
        except Exception as e:
            w(f"[4G_TEST] write err: {e}")

    while True:
        now = time.monotonic()
        if now - last_beat >= 5.0:
            w(f"[ALIVE] t={int(now - t0)}s pen={pwr.value}")
            last_beat = now

        # 4G UART → CDC (限流 256B, 每行加 [4G] 前缀供 PC 工具识别)
        try:
            n = uart.in_waiting
            if n:
                if n > 256:
                    n = 256
                chunk = uart.read(n)
                if chunk and usb_cdc.data:
                    try:
                        text = chunk.decode("utf-8", "replace")
                    except Exception:
                        text = "".join(chr(b) if 32 <= b < 127 else "?" for b in chunk)
                    pieces = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
                    out = ""
                    for j, p in enumerate(pieces):
                        if j < len(pieces) - 1:
                            out += "[4G] " + p + "\r\n"
                        elif p:
                            out += "[4G] " + p
                    try:
                        usb_cdc.data.write(out.encode("utf-8"))
                    except Exception:
                        pass
        except Exception:
            pass

        # CDC → 4G UART (按行)
        try:
            if usb_cdc.data and usb_cdc.data.in_waiting:
                chunk = usb_cdc.data.read(usb_cdc.data.in_waiting)
                if chunk:
                    line_buf += chunk.decode("utf-8", "replace")
                    while True:
                        i_n = line_buf.find("\n")
                        i_r = line_buf.find("\r")
                        idxs = [i for i in (i_n, i_r) if i >= 0]
                        if not idxs:
                            break
                        i = min(idxs)
                        line = line_buf[:i]
                        end = i + 1
                        if end < len(line_buf):
                            nx = line_buf[end]
                            cu = line_buf[i]
                            if nx in "\r\n" and nx != cu:
                                end += 1
                        line_buf = line_buf[end:]
                        handle_line(line.strip())
        except Exception:
            pass

        time.sleep(0.005)


# 全局cmdbuffer
_cdc_buffer = ""

def check_cdc_commands():
    """check CDC ifhaspendingprocesscmd，returncmdor None"""
    global _cdc_buffer
    
    if not usb_cdc.data:
        return None
    
    waiting = usb_cdc.data.in_waiting
    if waiting == 0:
        return None
    
    try:
        chunk = usb_cdc.data.read(waiting)
        _cdc_buffer += chunk.decode()
        
        if "\n" in _cdc_buffer or "\r" in _cdc_buffer:
            lines = _cdc_buffer.replace("\r", "\n").split("\n")
            _cdc_buffer = lines[-1]  # 保留not完整的partial
            
            for line in lines[:-1]:
                # 不能整行 lower: #set_wifi/#set_mqtt 的 SSID/密码/topic 大小写敏感,
                # 只在 process_commands 里对命令名 (parts[0]) lower
                cmd = line.strip()
                if cmd:
                    return cmd
    except:
        pass
    
    return None

def process_commands(rs485_drivers, rs485_protocols, config=None):
    """processallpendingprocess的 CDC cmd，returnifneeds立即采集
    
    cmdfmt:
    - 操作: #scan, #write_addr, #read
    - query: #get_id, #get_interval, #get_sensors, #get_mqtt, #get_wifi, #get_4g
    - set: #set_id, #set_interval, #set_mqtt, #set_wifi, #set_4g_apn
    - onoff: #enable_4g, #disable_4g, #enable_wifi, #disable_wifi
    - SYS: #status, #help, #reboot, #reload, #version
    """
    start_requested = False
    
    while True:
        cmd = check_cdc_commands()
        if not cmd:
            break
        
        parts = cmd.split()
        cmd_name = parts[0].lower()

        # ========== noneparamcmd ==========
        if cmd_name == "#status":
            gc.collect()
            free_kb = gc.mem_free() // 1024
            device_id = config.get("system.id", "notset") if config else "notset"
            interval = config.get("system.interval_preset", 0) if config else 0
            sensors_ch1 = len(config.get("rs485_1.sensors", [])) if config else 0
            sensors_ch2 = len(config.get("rs485_2.sensors", [])) if config else 0
            # netstatus
            wifi_enabled = config.get("network.wifi.enabled", False) if config else False
            g4_enabled = config.get("network.4g.enabled", False) if config else False
            eth_enabled = config.get("network.ethernet.enabled", False) if config else False
            # 存储status
            storage_enabled = config.get("local_storage.enabled", False) if config else False
            usb_msc = config.get("system.usb_msc_enabled", True) if config else True
            # 电源/电流 (05-11 新硬件)
            boost_on = _boost_en_pin.value if _boost_en_pin else False
            current_ma = read_system_current() if _voltage_monitor else 0

            log("========== SYSstatus ==========")
            log(f"设备ID: {device_id}")
            log(f"Interval: {interval}")
            log(f"Mem剩余: {free_kb} KB")
            log(f"CH1sensor: {sensors_ch1} ")
            log(f"CH2sensor: {sensors_ch2} ")
            log("--- net功能 ---")
            log(f"WiFi: {'on' if wifi_enabled else 'off'}")
            log(f"4G: {'on' if g4_enabled else 'off'}")
            log(f"ETH: {'on' if eth_enabled else 'off'}")
            log("--- 电源 ---")
            log(f"12V boost: {'ON' if boost_on else 'OFF'}")
            log(f"系统电流: {current_ma} mA")
            log("--- 存储功能 ---")
            log(f"storage: {'on' if storage_enabled else 'off'}")
            usb_rw = config.get("system.usb_rw", False) if config else False
            log(f"USB RW(USB_RW): {'enabled-flash' if usb_rw else 'disabled-daily'}")
            log("==============================")
            continue
        
        elif cmd_name == "#version":
            log(f"[Ver] {FIRMWARE_VERSION}")
            continue

        elif cmd_name == "#reboot":
            # 硬复位: 整片数字域复位 + boot.py 重跑。切 USB 模式等"必须重跑 boot.py"的场景用它。
            # 注意: USB 连着时主机会卡一下 (原生 USB 在软件 reset 时拔得不干净)。
            #       只想重启程序、不想卡 USB → 用 #reload。
            log("[SYS] Rebooting (hard reset)...")
            time.sleep(0.5)
            import microcontroller
            microcontroller.reset()

        elif cmd_name == "#reload":
            # 软复位: 只重启 code.py (supervisor.reload), 不复位芯片 / 不碰原生 USB → 不卡主机。
            # 代价: 不重跑 boot.py, 所以切 USB 模式之类必须用 #reboot (硬复位)。
            log("[SYS] Reloading (soft)...")
            time.sleep(0.3)
            supervisor.reload()

        elif cmd_name in ("#4g_test", "#4g_debug"):
            # 进入 4G 调试透传模式: USB CDC ↔ 4G UART 直连, 供 PC「网卡调试」tab 排错。
            # 独占设备永不返回, 退出靠 #reboot。建议在启动交互窗口里发。
            log("[SYS] 进入 4G 调试透传模式 (退出发 #reboot)...")
            enter_4g_test_mode(rs485_drivers)
            continue  # 正常到不了 — enter_4g_test_mode 不返回

        elif cmd_name == "#enable_usb_rw":
            # flash mode: 设 nvm[0], 发 #reboot (硬复位) 让 boot.py 重跑生效
            import microcontroller
            microcontroller.nvm[0] = 0  # 非 17 = flash mode
            log(f"[USB] flash mode 已置位 (nvm[0]={microcontroller.nvm[0]}), 发 #reboot 生效")
            continue

        elif cmd_name == "#disable_usb_rw":
            # daily mode: 设 nvm[0], 发 #reboot (硬复位) 让 boot.py 重跑生效
            import microcontroller
            microcontroller.nvm[0] = 17  # 17 = daily mode
            log(f"[USB] daily mode 已置位 (nvm[0]={microcontroller.nvm[0]}), 发 #reboot 生效")
            continue
        
        elif cmd_name == "#usb_rw_status":
            # 查看当beforeset
            import microcontroller
            nvm_val = microcontroller.nvm[0]
            enabled = (nvm_val != 17)
            log(f"[USB] USB RW: {'enabled-flash' if enabled else 'disabled-daily'} (nvm[0]={nvm_val})")
            continue

        elif cmd_name == "#sync_config":
            # 从 /config.json 合并进 NVM (配置权威源是 NVM, 此命令把文件同步过去)
            if not config:
                log("[SyncCfg] no config manager")
                continue
            try:
                import json as _json
                with open("/config.json", "r") as _f:
                    file_cfg = _json.load(_f)
                if not isinstance(file_cfg, dict):
                    log("[SyncCfg] /config.json 不是 JSON 对象")
                    continue
                ok = config.merge(file_cfg)
                if ok:
                    log(f"[SyncCfg] merged {len(file_cfg)} top-level keys -> NVM")
                    log("[SyncCfg] send #reboot or Ctrl+D 生效")
                else:
                    log("[SyncCfg] merge 失败 (可能超出 NVM 容量)")
            except OSError as e:
                log(f"[SyncCfg] read /config.json failed: {e}")
            except ValueError as e:
                log(f"[SyncCfg] JSON 解析失败: {e}")
            except Exception as e:
                log(f"[SyncCfg] err: {e}")
            continue

        elif cmd_name == "#reset_rev":
            # 重置远程指令 rev 去重状态: last_applied_rev=0, 清验证/失败/cfg_state
            # 用于重新从 rev=1 开始下发 (PC 工具计数器与设备 applied_rev 漂移后归零)
            try:
                import app.remote_cmd_nvm as _rcn
                old = _rcn.read_applied_rev()
                _rcn.write_applied_rev(0)
                import microcontroller as _mc
                _mc.nvm[13] = 0x00   # verify flag
                _mc.nvm[22] = 0x00   # fail counter
                _mc.nvm[23] = 0x00   # cfg_state -> none
                log(f"[ResetRev] applied_rev {old} -> 0, verify/fail/cfg_state 已清零")
                log("[ResetRev] 现在可从 rev=1 重新下发")
                log("[ResetRev] ⚠️ 先在 PC 工具清掉 broker 上该设备的 retained —")
                log("[ResetRev]    残留的旧高 rev 指令会被当新指令重新执行 (含 reboot 等动作)")
            except Exception as e:
                log(f"[ResetRev] err: {e}")
            continue

        elif cmd_name == "#help":
            log("========== CDC cmdhelp ==========")
            log("--- 操作cmd ---")
            log("#scan [com_port]        - Scansensoraddr")
            log("#write_addr [com_port] addr [timeoutms] - 批量wroteaddr")
            log("#read [com_port]        - 立即采集并Upload")
            log("#read_temp_and_model [com_port] - readtempandmodel")
            log("#12v_on [com]       - 开 12V (boost+VOUTCTR 两重) 给本通道传感器供电 (连续读前先开)")
            log("#12v_off [com]      - 关 12V (断本通道供电)")
            log("#read_a3 [com] addr - 读单个传感器 XYZ (0xA3 读后休眠, 已验证; 需先 #12v_on)")
            log("#read_5a [com] addr - 读单个传感器 XYZ (0x5A 不休眠; 部分传感器不支持)")
            log("--- querycmd ---")
            log("#get_id                 - get设备ID")
            log("#get_interval           - getInterval")
            log("#get_sensors [com_port] - getsensorlist")
            log("#get_mqtt               - getMQTTCfg")
            log("#get_wifi               - getWiFiCfg")
            log("#get_4g                 - get4GCfg")
            log("#get_sleep              - getSleep")
            log("--- setcmd (autosave) ---")
            log("#set_id 2026750001      - set设备ID")
            log("#set_interval 5         - setInterval(0=Standby)")
            log("#set_mqtt IP port topic  - setMQTT")
            log("#set_wifi SSID password     - setWiFi")
            log("#set_4g_apn cmnet       - set4G APN")
            log("#set_4g_cops 0          - set运营商(0=auto)")
            log("#set_sleep light|deep   - setSleep")
            log("#write_model [com_port] model - 批量writemodel")
            log("#enable_4g / #disable_4g")
            log("#enable_wifi / #disable_wifi")
            log("#enable_eth / #disable_eth")
            log("#enable_storage / #disable_storage - storageonoff")
            log("#enable_rs485_log / #disable_rs485_log - RS485 TX/RX hex log")
            log("--- 电源/电流 (05-11 新硬件) ---")
            log("#enable_boost / #disable_boost - 12V boost on/off")
            log("#get_current            - 读系统总电流 (A)")
            log("#diag_com <1|2>         - VCC 通电诊断")
            log("--- SYScmd ---")
            log("#status                 - 查看status(含net/存储)")
            log("#version                - 固件Ver")
            log("#reboot                 - 硬复位重启 (reset, 重跑 boot.py, USB 会卡一下)")
            log("#reload                 - 软复位重启 (reload, 只重跑程序, 不卡 USB)")
            log("#4g_test                - 进入 4G 调试透传 (CDC↔4G UART 直连, 退出发 #reboot)")
            log("#enable_usb_rw          - 电脑可readwrite(flash mode,重启生效)")
            log("#disable_usb_rw         - 设备可readwrite(daily mode,重启生效)")
            log("#usb_rw_status          - 查看USBreadwrite模式")
            log("#sync_config            - 从 /config.json 合并进 NVM")
            log("#reset_rev              - 远程指令 rev 归零 (从 rev=1 重新下发)")
            log("==================================")
            continue
        
        # ========== querycmd ==========
        elif cmd_name == "#get_id":
            device_id = config.get("system.id", "notset") if config else "notset"
            log(f"[ID] {device_id}")
            continue
        
        elif cmd_name == "#get_interval":
            interval = config.get("system.interval_preset", 0) if config else 0
            preset_names = {0: "Standby", 1: "5分", 2: "10分", 3: "15分", 4: "30分", 
                          5: "1时", 6: "2时", 7: "4时", 8: "12时", 9: "24时", 99: "custom"}
            name = preset_names.get(interval, "not知")
            log(f"[Interval] {interval} ({name})")
            continue
        
        elif cmd_name == "#get_mqtt":
            if config:
                broker = config.get("network.mqtt_broker", "")
                port = config.get("network.mqtt_port", 1883)
                topic = config.get("network.mqtt_topic", "")
                log(f"[MQTT] {broker}:{port} topic:{topic}")
            continue
        
        elif cmd_name == "#get_wifi":
            if config:
                enabled = config.get("network.wifi.enabled", False)
                ssid = config.get("network.wifi.ssid", "")
                log(f"[WiFi] {'on' if enabled else 'off'} SSID:{ssid}")
            continue
        
        elif cmd_name == "#get_4g":
            if config:
                enabled = config.get("network.4g.enabled", False)
                apn = config.get("network.4g.apn", "cmnet")
                cops = config.get("network.4g.cops", "0")
                modem = config.get("network.4g.modem", "A7670C_yundtu")
                log(f"[4G] {'on' if enabled else 'off'} APN:{apn} COPS:{cops} MODEM:{modem}")
            continue
        
        elif cmd_name == "#get_sleep":
            if config:
                mode = config.get("system.sleep_mode", "deep")
                log(f"[Sleep] {mode}")
            continue
        
        # ========== onoffcmd ==========
        elif cmd_name == "#enable_4g":
            if config:
                config.set("network.4g.enabled", True)
                config.save()
                log("[4G] doneon")
            continue
        
        elif cmd_name == "#disable_4g":
            if config:
                config.set("network.4g.enabled", False)
                config.save()
                log("[4G] doneoff")
            continue
        
        elif cmd_name == "#enable_wifi":
            if config:
                config.set("network.wifi.enabled", True)
                config.save()
                log("[WiFi] doneon")
            continue
        
        elif cmd_name == "#disable_wifi":
            if config:
                config.set("network.wifi.enabled", False)
                config.save()
                log("[WiFi] doneoff")
            continue
        
        elif cmd_name == "#enable_eth":
            if config:
                config.set("network.ethernet.enabled", True)
                config.save()
                log("[ETH] doneon (W5500)")
            continue
        
        elif cmd_name == "#disable_eth":
            if config:
                config.set("network.ethernet.enabled", False)
                config.save()
                log("[ETH] doneoff")
            continue

        # ========== 电源/电流 (05-11 新硬件) ==========
        elif cmd_name == "#enable_boost":
            set_boost(True)
            log("[BOOST] 12V boost ON")
            continue

        elif cmd_name == "#disable_boost":
            set_boost(False)
            log("[BOOST] 12V boost OFF")
            continue

        elif cmd_name == "#get_current":
            i_ma = read_system_current()
            log(f"[CURRENT] {i_ma} mA")
            continue

        elif cmd_name == "#diag_com":
            # 用法: #diag_com 1   ← 验证 COM1 VCC 通断
            if len(parts) < 2 or parts[1] not in ("1", "2"):
                log("[DIAG] 用法: #diag_com <1|2>")
                continue
            ch = int(parts[1])
            if ch not in rs485_drivers:
                log(f"[DIAG] COM{ch} 未初始化, 检查 config.json")
                continue
            v_key = "v485_4" if ch == 1 else "v485_3"
            driver = rs485_drivers[ch]
            # 断电读基线
            driver.power_off()
            time.sleep(0.1)
            v_off = _voltage_monitor.read(v_key) if _voltage_monitor else 0.0
            # 通电读
            driver.power_on()
            time.sleep(0.2)   # 等 12V 稳定
            v_on = _voltage_monitor.read(v_key) if _voltage_monitor else 0.0
            # 恢复断电
            driver.power_off()
            ok = v_on >= 9.0 and v_off < 2.0
            log(f"[DIAG] COM{ch}: VCC off={v_off:.2f}V, on={v_on:.2f}V → {'OK' if ok else 'FAIL'}")
            continue

        elif cmd_name == "#enable_storage":
            if config:
                config.set("local_storage.enabled", True)
                config.save()
                log("[storage] doneon")
            continue
        
        elif cmd_name == "#disable_storage":
            if config:
                config.set("local_storage.enabled", False)
                config.save()
                log("[storage] doneoff")
            continue
        
        # ========== setcmd (needsparam) ==========
        elif cmd_name == "#set_id":
            if len(parts) < 2:
                log("[err] fmt: #set_id 2026750001")
                continue
            new_id = parts[1]
            if config:
                config.set("system.id", new_id)
                config.save()
                log(f"[ID] set to: {new_id}")
            continue
        
        elif cmd_name == "#set_interval":
            if len(parts) < 2:
                log("[err] fmt: #set_interval 5")
                continue
            try:
                interval = int(parts[1])
                if config:
                    config.set("system.interval_preset", interval)
                    config.save()
                    log(f"[Interval] set to: {interval}")
                    return "reload_config"  # 通知主循环立即重新加载间隔
            except:
                log("[err] intervalrequiredis数word")
            continue
        
        elif cmd_name == "#set_mqtt":
            if len(parts) < 4:
                log("[err] fmt: #set_mqtt IP port topic")
                continue
            try:
                broker = parts[1]
                port = int(parts[2])
                topic = parts[3]
                if config:
                    config.set("network.mqtt_broker", broker)
                    config.set("network.mqtt_port", port)
                    config.set("network.mqtt_topic", topic)
                    config.save()
                    log(f"[MQTT] set: {broker}:{port} topic:{topic}")
            except:
                log("[err] portrequiredis数word")
            continue
        
        elif cmd_name == "#set_wifi":
            if len(parts) < 3:
                log("[err] fmt: #set_wifi SSID password")
                continue
            ssid = parts[1]
            password = parts[2]
            if config:
                config.set("network.wifi.ssid", ssid)
                config.set("network.wifi.password", password)
                config.save()
                log(f"[WiFi] set: {ssid}")
            continue
        
        elif cmd_name == "#set_4g_apn":
            if len(parts) < 2:
                log("[err] fmt: #set_4g_apn cmnet")
                continue
            apn = parts[1]
            if config:
                config.set("network.4g.apn", apn)
                config.save()
                log(f"[4G APN] set: {apn}")
            continue
        
        elif cmd_name == "#set_4g_cops":
            if len(parts) < 2:
                log("[err] fmt: #set_4g_cops 0")
                log("[提示] 0=auto, 46000=move, 46001=联通, 46011=电信")
                continue
            cops = parts[1]
            if config:
                config.set("network.4g.cops", cops)
                config.save()
                log(f"[4G COPS] set: {cops}")
            continue

        elif cmd_name == "#set_4g_modem":
            if len(parts) < 2:
                log("[err] fmt: #set_4g_modem A7670C_yundtu | A7670G")
                continue
            modem = parts[1]
            if modem not in ("A7670C_yundtu", "A7670G"):
                log(f"[err] modem 必须是 A7670C_yundtu 或 A7670G, 收到: {modem}")
                continue
            if config:
                config.set("network.4g.modem", modem)
                config.save()
                log(f"[4G modem] set: {modem}")
            continue

        elif cmd_name == "#set_sleep":
            if len(parts) < 2:
                log("[err] fmt: #set_sleep light|deep")
                continue
            mode = parts[1].lower()
            if mode not in ("light", "deep"):
                log("[err] 模式requiredis light or deep")
                continue
            if config:
                config.set("system.sleep_mode", mode)
                config.save()
                log(f"[Sleep] set: {mode}")
            continue
        
        elif cmd_name == "#get_sensors":
            if len(parts) < 2:
                log("[err] fmt: #get_sensors com1")
                continue
            com_str = parts[1].lower()
            if com_str in ("com1", "1"):
                sensors = config.get("rs485_1.sensors", []) if config else []
            elif com_str in ("com2", "2"):
                sensors = config.get("rs485_2.sensors", []) if config else []
            else:
                log("[err] invalid COM 口")
                continue
            log(f"[sensor] total {len(sensors)} :")
            # 每排显示5
            row = []
            for s in sensors:
                row.append(str(s.get('addr', 0)))
                if len(row) == 5:
                    log("  " + "  ".join(row))
                    row = []
            if row:  # 输out剩余的
                log("  " + "  ".join(row))
            continue
        
        # ========== needs COM param的操作cmd ==========
        if len(parts) < 2:
            log(f"[CDC] unknown cmd: {cmd}")
            log("[help] send #help 查看cmdlist")
            continue
        
        com_str = parts[1].lower()
        
        # parse com 口号 (com1 -> 1, com2 -> 2)
        if com_str in ("com1", "1"):
            target_ch = 1
        elif com_str in ("com2", "2"):
            target_ch = 2
        else:
            log(f"[CDC] invalid COM 口: {com_str}")
            continue
        
        # checkCHifexist
        if target_ch not in rs485_drivers:
            log(f"[CDC] CH {target_ch} noton")
            continue
        
        # Create单CHdict
        single_driver = {target_ch: rs485_drivers[target_ch]}
        single_protocol = {target_ch: rs485_protocols[target_ch]}
        
        if cmd_name == "#scan":
            log(f"[CDC] ScanCH {target_ch}")
            _start = time.monotonic()
            do_address_scan(single_driver, single_protocol, config)
            log(f"[done] 耗时 {time.monotonic() - _start:.1f} s")
        elif cmd_name == "#read":
            global _force_collect_com
            _force_collect_com = target_ch
            log(f"[CDC] trigger立即采集 (只采 COM{target_ch})")
            start_requested = True  # letMain loop执row完整采集andup报流程
        elif cmd_name == "#write_addr":
            # parseaddrparam: #write_addr com1 2026020100 [timeoutms]
            if len(parts) < 3:
                log("[CDC] missingaddrparam")
                log("[help] fmt: #write_addr com1 addr [timeoutms]")
                continue
            try:
                max_addr = int(parts[2])
                timeout_ms = int(parts[3]) if len(parts) > 3 else 300
                log(f"[CDC] WriteAddrCH {target_ch}，maxaddr: {max_addr}, timeout: {timeout_ms}ms")
                _start = time.monotonic()
                do_batch_write_addr(single_driver, single_protocol, max_addr, config, timeout_ms=timeout_ms)
                log(f"[done] 耗时 {time.monotonic() - _start:.1f} s")
            except Exception as e:
                log(f"[CDC] paramerr: {e}")
        elif cmd_name == "#read_temp_and_model":
            # readallsensor的tempandmodel
            log(f"[CDC] readCH {target_ch} tempandmodel...")
            _start = time.monotonic()
            driver = single_driver[target_ch]
            protocol = single_protocol[target_ch]
            sensors = config.get(f"rs485_{target_ch}.sensors", []) if config else []
            
            if not sensors:
                log("[CDC] 没hasdoneCfg的sensor，请先 #scan")
                continue
            
            driver.power_on()
            import time as tm
            tm.sleep(0.3)
            
            for s in sensors:
                addr = s.get("addr", 0)
                result = protocol.read_temp(addr, timeout_ms=5000)  # PC端use5000ms
                if "error" not in result:
                    log(f"  addr {addr}: temp={result['temp']:.1f}°C model={result['model']}")
                else:
                    log(f"  addr {addr}: readfail")
                tm.sleep(0.1)  # 100msintervallike PC端
            
            driver.power_off()
            log(f"[done] 耗时 {time.monotonic() - _start:.1f} s")
        elif cmd_name == "#write_model":
            # 批量wrotemodel: #write_model com1 106
            if len(parts) < 3:
                log("[err] fmt: #write_model com1 model")
                continue
            try:
                model = int(parts[2])
            except:
                log("[err] modelrequiredis数word")
                continue
            
            log(f"[CDC] 批量wrotemodel {model} 到CH {target_ch}...")
            _start = time.monotonic()
            driver = single_driver[target_ch]
            protocol = single_protocol[target_ch]
            sensors = config.get(f"rs485_{target_ch}.sensors", []) if config else []
            
            if not sensors:
                log("[CDC] 没hasdoneCfg的sensor，请先 #scan")
                continue
            
            driver.power_on()
            import time as tm
            tm.sleep(0.3)
            
            success = 0
            fail = 0
            for s in sensors:
                addr = s.get("addr", 0)
                protocol.write_model(addr, model, timeout_ms=300)
                tm.sleep(1.0)  # PC端Wait1slet设备process
                
                # verifyread (usesA8cmd, returnpacket含model)
                verify = protocol.read_temp(addr, timeout_ms=300)
                if verify and verify.get("model") == model:
                    log(f"  addr {addr}: wrotemodel {model} ✓")
                    success += 1
                else:
                    actual = verify.get("model", "?") if verify else "?"
                    log(f"  addr {addr}: verifyfail (期望{model}, 实际{actual})")
                    fail += 1
            
            driver.power_off()
            log(f"[done] ok{success} fail{fail}, 耗时 {time.monotonic() - _start:.1f} s")
        elif cmd_name == "#12v_on":
            # 开 12V (两重开关: boost SPWALLON + 本通道 VOUTCTR) 给本通道传感器供电, 并等启动稳定。
            # 连续读前先开; 之后用 #read_a3/#read_5a 反复读; 读完 #12v_off 关。fmt: #12v_on com1
            global _live_read_active
            _live_read_active = True       # 防 60s 空闲超时去采集 (会断掉这里保持的 12V)
            driver = single_driver[target_ch]
            _just_powered = not driver._power_on
            driver.power_on()              # 幂等: boost_acquire(SPWALLON 两重之一) + VOUTCTR(另一重)
            if _just_powered:
                log("[12V] CH{} ON, 等传感器启动稳定...".format(target_ch))
                time.sleep(1.5)            # boost 软启动只等 0.3s, 不够传感器开机
            # 读 VCC 反馈, 确认 12V 真到该通道: 偏低=供电/接线没通; 够 12V 仍读不到=传感器/地址/波特率
            _vk = "v485_4" if target_ch == 1 else "v485_3"
            _vcc = _voltage_monitor.read(_vk) if _voltage_monitor else 0.0
            _vw = "" if _vcc >= 9.0 else "  ⚠偏低! 12V 没到传感器(查供电/接线)"
            log("[12V] CH{} ON, VCC实测 {:.2f}V{}".format(target_ch, _vcc, _vw))
            continue
        elif cmd_name in ("#12v_off", "#read_live_off"):
            # 关 12V: 断本通道 VOUTCTR(P-FET 隔离) + 释放 boost 占用。fmt: #12v_off com1
            _live_read_active = False      # 恢复 60s 空闲超时自动采集/睡眠
            single_driver[target_ch].power_off()
            log("[12V] CH{} OFF".format(target_ch))
            continue
        elif cmd_name in ("#read_a3", "#read_5a", "#read_live"):
            # 读单个传感器 XYZ。
            #   #read_a3 / #read_5a: 纯读, 不碰电源 (需先 #12v_on)。a3=0xA3 读后休眠(已验证), 5a=0x5A 不休眠。
            #   #read_live: PC「传感器读数」tab 用 — 幂等开12V+首次稳定 + 读; 读法由第4参 a3/5a 定(默认 a3)。
            #   fmt: #read_a3 com1 <addr>  |  #read_live com1 <addr> [a3|5a]
            if len(parts) < 3:
                log("[err] fmt: {} com1 <addr>".format(cmd_name))
                continue
            try:
                live_addr = int(parts[2])
            except Exception:
                log("[err] addr 必须是数字")
                continue
            driver = single_driver[target_ch]
            protocol = single_protocol[target_ch]
            if cmd_name == "#read_5a":
                _use_awake = True
            elif cmd_name == "#read_a3":
                _use_awake = False
            else:  # #read_live: tab 一体化 (幂等开 12V + 首次稳定), 读法看第4参
                _use_awake = len(parts) >= 4 and parts[3].lower() == "5a"
                _live_read_active = True
                _just_powered = not driver._power_on
                driver.power_on()
                if _just_powered:
                    log("[LIVE] CH{} 12V 已开, 等传感器启动稳定...".format(target_ch))
                    time.sleep(1.5)
            if _use_awake and hasattr(protocol, "read_data_awake"):
                d = protocol.read_data_awake(live_addr)             # 0x5A 不休眠
            else:
                d = protocol.read_data(live_addr, timeout_ms=3000)  # 0xA3 读后休眠 (已验证)
            if not d or (isinstance(d, dict) and "error" in d):
                log("[LIVE] addr={} ERR no_response".format(live_addr))
            else:
                log("[LIVE] addr={} x={:.3f} y={:.3f} z={:.3f} v={} ver={:.2f} st={} raw=0x{:02X} axis={}".format(
                    d.get("address", live_addr), d.get("a", 0.0), d.get("b", 0.0), d.get("z", 0.0),
                    d.get("voltage", 0), d.get("version", 0.0), d.get("status", "?"),
                    d.get("status_raw", 0), 2 if d.get("is_2axis") else 3))
            continue
        elif cmd_name == "#enable_rs485_log":
            for d in rs485_drivers.values():
                d.cdc_log = True
            log("[RS485] TX/RX log enabled")
            continue
        
        elif cmd_name == "#disable_rs485_log":
            for d in rs485_drivers.values():
                d.cdc_log = False
            log("[RS485] TX/RX log disabled")
            continue
        
        elif cmd_name.strip():
            log(f"[CDC] unknown cmd: {cmd_name}")
    
    return start_requested

# ============================================================
# BLE cmdprocess
# ============================================================

def process_ble_command(ble, config, rs485_drivers=None, rs485_protocols=None) -> bool:
    """process BLE JSON cmd，returnifneeds立即采集"""
    if not ble or not ble._initialized or not ble._ble.connected:
        return False
    
    # read BLE data
    waiting = ble._uart.in_waiting
    if waiting > 0:
        data = ble._uart.read(waiting)
        if data:
            # errors="ignore": OTA 中止后残留的 0xA5 二进制帧若流到这里,
            # 严格解码会抛 UnicodeError 一路打穿到 [FATAL]
            raw = data.decode("utf-8", "ignore")
            if not hasattr(ble, '_cmd_buffer'):
                ble._cmd_buffer = ""
            ble._cmd_buffer += raw
    
    # checkifhas完整cmd
    if not hasattr(ble, '_cmd_buffer') or "\n" not in ble._cmd_buffer:
        return False
    
    lines = ble._cmd_buffer.split("\n")
    ble._cmd_buffer = lines[-1]
    
    cmd_json = None
    for line in lines[:-1]:
        line = line.strip()
        if line and line.startswith("{"):
            cmd_json = line
            break
    
    if not cmd_json:
        return False
    
    log(f"[BLE] 收到: {cmd_json[:60]}...")
    
    try:
        import json
        cmd = json.loads(cmd_json)
        cmd_type = cmd.get("cmd", "")
        
        if cmd_type == "status":
            # return设备status
            gc.collect()
            response = {
                "cmd": "status",
                "id": config.get("system.id", ""),
                "interval": config.get("system.interval_preset", 0),
                "sleep_mode": config.get("system.sleep_mode", "idle"),
                "free_mem": gc.mem_free() // 1024
            }
            ble.send(json.dumps(response) + "\n")
            log("[BLE] donesend status")
            
        elif cmd_type == "read" or cmd_type == "get_all":
            # 返回完整配置（直接从 NVM 读取）
            import microcontroller as _mc
            full_config = config.get_all()
            response = {"cmd": "config"}
            response.update(full_config)
            # 附加实际 USB 模式和传感器计数
            response["_usb_rw"] = (_mc.nvm[0] != 17)  # True=flash mode(电脑可写)
            response["_sensors_summary"] = {
                "com1_count": len(config.get("rs485_1.sensors", [])),
                "com2_count": len(config.get("rs485_2.sensors", []))
            }
            ble.send(json.dumps(response) + "\n")
            log("[BLE] sent full config from NVM")
            
        elif cmd_type == "get_section":
            # return指定Cfgseg
            section = cmd.get("section", "")
            if section:
                data = config.get_section(section)
                ble.send(json.dumps({
                    "type": "config_section",
                    "section": section,
                    "data": data
                }) + "\n")
                log(f"[BLE] donesendCfgseg: {section}")
            
        elif cmd_type == "set":
            # 通usesetCfgitem
            key = cmd.get("key", "")
            value = cmd.get("value")
            if key:
                config.set(key, value)
                config.save()
                ble.send(json.dumps({"cmd": "set", "ok": True, "key": key}) + "\n")
                log(f"[BLE] set: {key}")
        
        elif cmd_type == "set_id":
            # set设备ID
            value = cmd.get("value", "")
            if value:
                config.set("system.id", value)
                config.save()
                ble.send(json.dumps({"cmd": "set_id", "ok": True, "value": value}) + "\n")
                log(f"[BLE] IDset: {value}")
                
        elif cmd_type == "set_interval":
            # setInterval
            value = cmd.get("value", 0)
            config.set("system.interval_preset", int(value))
            # ifiscustominterval(99)，savecustommin数
            if int(value) == 99:
                custom_min = cmd.get("custom_min", 60)
                config.set("system.interval_custom_min", int(custom_min))
                log(f"[BLE] setcustominterval: {custom_min} min")
            save_result = config.save()
            log(f"[BLE] setinterval: {value}, save结果: {save_result}")
            ble.send(json.dumps({"cmd": "set_interval", "ok": True, "value": value}) + "\n")
            return "reload_config"  # 通知Main loopreload cfg
            
        elif cmd_type == "set_sleep":
            # setSleep
            value = cmd.get("value", "idle")
            config.set("system.sleep_mode", value)
            config.save()
            ble.send(json.dumps({"cmd": "set_sleep", "ok": True, "value": value}) + "\n")
            log(f"[BLE] Sleep: {value}")
            return "reload_config"  # 通知Main loopreload cfg
            
        elif cmd_type == "set_mqtt":
            # setMQTTCfg
            broker = cmd.get("broker", "")
            port = cmd.get("port", 1883)
            topic = cmd.get("topic", "")
            if broker:
                config.set("network.mqtt_broker", broker)
                config.set("network.mqtt_port", int(port))
                if topic:
                    config.set("network.mqtt_topic", topic)
                config.save()
                ble.send(json.dumps({"cmd": "set_mqtt", "ok": True}) + "\n")
                log(f"[BLE] MQTTset: {broker}:{port}")
                
        elif cmd_type == "set_wifi":
            # setWiFiCfg
            ssid = cmd.get("ssid", "")
            password = cmd.get("password", "")
            if ssid:
                config.set("network.wifi.ssid", ssid)
                config.set("network.wifi.password", password)
                config.save()
                ble.send(json.dumps({"cmd": "set_wifi", "ok": True}) + "\n")
                log(f"[BLE] WiFiset: {ssid}")
                
        elif cmd_type == "set_4g":
            # set4GCfg
            apn = cmd.get("apn", "cmnet")
            cops = cmd.get("cops", "0")
            modem = cmd.get("modem", "A7670C_yundtu")
            config.set("network.4g.apn", apn)
            config.set("network.4g.cops", str(cops))   # 统一存 str (CDC/远程下发同为 str)
            config.set("network.4g.modem", modem)
            config.save()
            ble.send(json.dumps({"cmd": "set_4g", "ok": True}) + "\n")
            log(f"[BLE] 4Gset: APN={apn} modem={modem}")
            
        elif cmd_type == "enable_wifi":
            config.set("network.wifi.enabled", True)
            config.save()
            ble.send(json.dumps({"cmd": "enable_wifi", "ok": True}) + "\n")
            
        elif cmd_type == "disable_wifi":
            config.set("network.wifi.enabled", False)
            config.save()
            ble.send(json.dumps({"cmd": "disable_wifi", "ok": True}) + "\n")
            
        elif cmd_type == "enable_4g":
            config.set("network.4g.enabled", True)
            config.save()
            ble.send(json.dumps({"cmd": "enable_4g", "ok": True}) + "\n")
            
        elif cmd_type == "disable_4g":
            config.set("network.4g.enabled", False)
            config.save()
            ble.send(json.dumps({"cmd": "disable_4g", "ok": True}) + "\n")
                
        elif cmd_type == "save":
            # saveCfg
            config.save()
            ble.send(json.dumps({"cmd": "save", "ok": True}) + "\n")
            log("[BLE] cfg saved")
        
        elif cmd_type == "write_config":
            # 块写入完整配置到 NVM
            new_cfg = cmd.get("config")
            if new_cfg and isinstance(new_cfg, dict):
                # 合并写入：保留未传输的字段
                config.merge(new_cfg)
                ble.send(json.dumps({"cmd": "write_config", "ok": True}) + "\n")
                log("[BLE] write_config: NVM saved")
                return "reload_config"
            else:
                ble.send(json.dumps({"cmd": "write_config", "ok": False, "error": "missing config"}) + "\n")
        
        elif cmd_type == "import_address_list":
            # 从 /address_list.csv 导入传感器地址
            filepath = cmd.get("path", "/address_list.csv")
            result = config.import_address_list(filepath)
            if "error" in result:
                ble.send(json.dumps({"cmd": "import_address_list", "ok": False, "error": result["error"]}) + "\n")
                log(f"[BLE] import_address_list failed: {result['error']}")
            else:
                ble.send(json.dumps({"cmd": "import_address_list", "ok": True, "result": result}) + "\n")
                log(f"[BLE] import_address_list: {result}")
                return "reload_config"
        
        elif cmd_type == "sync_config":
            # 从 /config.json 合并进 NVM (与 CDC #sync_config 等价)
            try:
                with open("/config.json", "r") as f:
                    file_cfg = json.load(f)
                if not isinstance(file_cfg, dict):
                    ble.send(json.dumps({"cmd": "sync_config", "ok": False, "error": "not a JSON object"}) + "\n")
                    log("[BLE] sync_config: /config.json 不是 JSON 对象")
                else:
                    ok = config.merge(file_cfg)
                    ble.send(json.dumps({"cmd": "sync_config", "ok": ok, "keys": len(file_cfg)}) + "\n")
                    log(f"[BLE] sync_config: merged {len(file_cfg)} top-level keys -> NVM (ok={ok})")
                    if ok:
                        return "reload_config"
            except Exception as e:
                ble.send(json.dumps({"cmd": "sync_config", "ok": False, "error": str(e)}) + "\n")
                log(f"[BLE] sync_config failed: {e}")

        elif cmd_type == "get_sensors":
            # getsensoraddrlist + 当前协议名
            com = cmd.get("com", "1")
            sensors = config.get(f"rs485_{com}.sensors", []) if config else []
            addrs = [s.get("addr", 0) for s in sensors]
            proto_name = config.get(f"rs485_{com}.protocol", "PRIVATE_V2026") if config else "PRIVATE_V2026"
            ble.send(json.dumps({"cmd": "get_sensors", "com": com, "addrs": addrs, "protocol": proto_name}) + "\n")
            log(f"[BLE] sensorlist COM{com}: {len(addrs)} ({proto_name})")

        elif cmd_type == "list_protocols":
            # 上报所有可用协议元数据 (APP 用于校验地址范围 + 填充协议下拉)
            protos = []
            for name, cls in PROTOCOL_REGISTRY.items():
                protos.append({
                    "name": name,
                    "addr_min": cls.ADDR_MIN,
                    "addr_max": cls.ADDR_MAX,
                    "scan_max": cls.SCAN_MAX,
                })
            ble.send(json.dumps({"cmd": "list_protocols", "protocols": protos}) + "\n")
            log(f"[BLE] list_protocols: {len(protos)}")
            
        elif cmd_type == "scan":
            # 扫描传感器地址 - 使用统一函数
            com_str = cmd.get("com", "1")
            com = int(com_str)
            if not rs485_drivers or com not in rs485_drivers:
                ble.send(json.dumps({"cmd": "scan", "error": "invalidCOM口"}) + "\n")
            else:
                log(f"[BLE] scan COM{com} (unified)")
                ble.send(json.dumps({"cmd": "scan_start", "com": com_str}) + "\n")
                
                # 定义进度回调，实时发送 BLE 数据
                def ble_scan_progress(auto_id, found_list):
                    # 如果刚发现新设备，发送结果
                    if found_list and len(found_list) > 0:
                        last_found = found_list[-1]
                        # 只在发现新设备时发送（通过检查是否是刚添加的）
                        # 这里简化处理，通过比较 auto_id 和找到数量来判断
                        pass  # 在回调外处理
                    # 每 100 个发送进度
                    if (auto_id + 1) % 100 == 0:
                        ble.send(json.dumps({"cmd": "scan_progress", "com": com_str, "progress": auto_id + 1}) + "\n")
                
                driver = rs485_drivers[com]
                protocol = rs485_protocols[com]
                
                # 手动调用扫描以便能发送实时结果
                found = []
                log(f"[Scan] CH{com} power on...")
                driver.power_on()
                time.sleep(0.3)
                
                for auto_id in range(1024):
                    result = protocol.scan_address(auto_id, timeout_ms=300)
                    if result:
                        addr = result["fixed_addr"]
                        found.append({"addr": addr})
                        ble.send(json.dumps({"cmd": "scan_result", "com": com_str, "auto_id": auto_id, "addr": addr}) + "\n")
                        log(f"[Scan] CH{com} AutoID {auto_id} -> addr {addr}")
                    
                    if (auto_id + 1) % 100 == 0:
                        ble.send(json.dumps({"cmd": "scan_progress", "com": com_str, "progress": auto_id + 1}) + "\n")
                        log(f"[Scan] CH{com} progress {auto_id + 1}/1024, found {len(found)}")
                    
                    time.sleep(0.2)
                
                driver.power_off()
                log(f"[Scan] CH{com} done, found {len(found)} sensors")
                
                # 保存结果
                if found and config:
                    config.set(f"rs485_{com}.sensors", found)
                    config.save()
                ble.send(json.dumps({"cmd": "scan_complete", "com": com_str, "count": len(found)}) + "\n")
                log(f"[BLE] scan done COM{com}: {len(found)}")
                
        elif cmd_type == "poll":
            # 读取单个传感器
            com_str = cmd.get("com", "1")
            com = int(com_str)
            addr = cmd.get("addr", 0)
            if not rs485_drivers or com not in rs485_drivers or addr == 0:
                ble.send(json.dumps({"cmd": "sensor_data", "com": com_str, "addr": addr, "ok": False}) + "\n")
            else:
                driver = rs485_drivers[com]
                protocol = rs485_protocols[com]
                driver.power_on()
                time.sleep(0.3)
                try:
                    data = protocol.read_data(addr, timeout_ms=5000)
                    if data:
                        ble.send(json.dumps({
                            "cmd": "sensor_data", "com": com_str, "addr": addr,
                            "a": round(data.get("a", 0), 2),
                            "b": round(data.get("b", 0), 2),
                            "z": round(data.get("z", 0), 2),
                            "ok": True
                        }) + "\n")
                        log(f"[BLE] poll addr {addr}: A={data.get('a', 0):.2f}")
                    else:
                        ble.send(json.dumps({"cmd": "sensor_data", "com": com_str, "addr": addr, "ok": False}) + "\n")
                        log(f"[BLE] poll addr {addr}: no resp")
                except Exception as e:
                    ble.send(json.dumps({"cmd": "sensor_data", "com": com_str, "addr": addr, "ok": False}) + "\n")
                    log(f"[BLE] poll addr {addr} err: {e}")
                driver.power_off()

        elif cmd_type == "read_data":
            # 读取传感器数据 - 使用统一函数
            com_str = cmd.get("com", "1")
            com = int(com_str)
            if not rs485_drivers or com not in rs485_drivers:
                ble.send(json.dumps({"cmd": "read_data", "error": "invalidCOM口"}) + "\n")
            else:
                sensors = config.get(f"rs485_{com}.sensors", []) if config else []
                if not sensors:
                    ble.send(json.dumps({"cmd": "read_data", "error": "nonesensorCfg"}) + "\n")
                else:
                    log(f"[BLE] read COM{com} {len(sensors)} sensors (unified)")
                    ble.send(json.dumps({"cmd": "read_start", "com": com_str, "count": len(sensors)}) + "\n")
                    
                    # 定义进度回调，实时发送 BLE 数据
                    def ble_progress(index, total, addr, data):
                        if data and data.get("status") != "W":
                            ble.send(json.dumps({
                                "cmd": "sensor_data", "com": com_str, "addr": addr,
                                "a": data.get("a", 0), "b": data.get("b", 0), "z": data.get("z", 0),
                                "ok": True
                            }) + "\n")
                        else:
                            ble.send(json.dumps({
                                "cmd": "sensor_data", "com": com_str, "addr": addr, "ok": False
                            }) + "\n")
                    
                    # 调用统一读取函数
                    driver = rs485_drivers[com]
                    protocol = rs485_protocols[com]
                    results = do_read_sensors(com, driver, protocol, sensors,
                                              timeout_ms=5000, interval_ms=150, reverse=True,
                                              progress_callback=ble_progress)
                    
                    ok_count = sum(1 for r in results if r.get("status") != "W")
                    fail_count = len(results) - ok_count
                    log(f"[BLE] read done COM{com}: ok={ok_count}, fail={fail_count}")
                    ble.send(json.dumps({"cmd": "read_complete", "com": com_str}) + "\n")
            
        elif cmd_type == "read_model":
            # 逐个读取型号 — 完全复刻 ref/tab_batch.py _do_read_model
            com_str = cmd.get("com", "1")
            com = int(com_str)
            if not rs485_drivers or com not in rs485_drivers:
                ble.send(json.dumps({"cmd": "read_model", "error": "invalidCOM口"}) + "\n")
            else:
                sensors = config.get(f"rs485_{com}.sensors", []) if config else []
                if not sensors:
                    ble.send(json.dumps({"cmd": "read_model", "error": "nonesensorCfg"}) + "\n")
                else:
                    log(f"[BLE] readmodel COM{com} {len(sensors)} sensors...")
                    ble.send(json.dumps({"cmd": "read_model_start", "com": com_str, "count": len(sensors)}) + "\n")
                    driver = rs485_drivers[com]
                    protocol = rs485_protocols[com]
                    driver.power_on()
                    time.sleep(0.3)
                    import struct as _struct
                    for sensor in reversed(sensors):
                        addr = sensor.get("addr", 0)
                        # build_frame(CMD_READ_RANGE, addr_bytes)
                        addr_bytes = _struct.pack('>I', addr)
                        frame = protocol._build_frame(0x00C8, addr_bytes)
                        # send_and_receive(frame, timeout_ms=2000)
                        response = driver.send_and_receive(frame, response_size=30, timeout_ms=2000)
                        # parse_response(response)
                        ok = False
                        if response and len(response) >= 6:
                            start_idx = -1
                            for i in range(len(response)):
                                if response[i] == 0xDD:
                                    start_idx = i
                                    break
                            if start_idx >= 0:
                                resp = response[start_idx:]
                                if len(resp) >= 6 and resp[-1] == 0xEE:
                                    payload = resp[4:-2]
                                    if len(payload) >= 5:
                                        model_val = payload[4]
                                        ble.send(json.dumps({"cmd": "model_data", "com": com_str, "addr": addr, "model": model_val, "ok": True}) + "\n")
                                        log(f"  addr={addr} model={model_val}")
                                        ok = True
                        if not ok:
                            ble.send(json.dumps({"cmd": "model_data", "com": com_str, "addr": addr, "ok": False}) + "\n")
                            log(f"  addr={addr} read_model failed")
                        # 延时 100ms（与 ref 一致）
                        time.sleep(0.1)
                    driver.power_off()
                    ble.send(json.dumps({"cmd": "read_model_complete", "com": com_str}) + "\n")
                    log(f"[BLE] readmodel done COM{com}")
                    
        elif cmd_type == "set_model":
            # 批量setmodel (C7 cmd)
            com_str = cmd.get("com", "1")
            com = int(com_str)  # 转换为整数，匹配 rs485_drivers 的 key
            model = cmd.get("model", 0)
            if not rs485_drivers or com not in rs485_drivers:
                ble.send(json.dumps({"cmd": "set_model", "error": "invalidCOM口"}) + "\n")
            else:
                sensors = config.get(f"rs485_{com}.sensors", []) if config else []
                if not sensors:
                    ble.send(json.dumps({"cmd": "set_model", "error": "nonesensorCfg"}) + "\n")
                else:
                    log(f"[BLE] setmodel COM{com} model={model} {len(sensors)} ...")
                    ble.send(json.dumps({"cmd": "set_model_start", "com": com_str, "model": model, "count": len(sensors)}) + "\n")
                    driver = rs485_drivers[com]
                    protocol = rs485_protocols[com]
                    driver.power_on()
                    time.sleep(0.3)
                    success_count = 0
                    for sensor in reversed(sensors):
                        addr = sensor.get("addr", 0)
                        import struct
                        # C7 cmd: addr(4) + model(1)
                        data = struct.pack(">I", addr) + bytes([model])
                        cmd_frame = protocol._build_frame(0xC7, data)
                        driver.send(cmd_frame)
                        time.sleep(1.0)  # C7需要1s等待Flash写入完成
                        # C7 没hasrsp，sendafterWait即可
                        ble.send(json.dumps({"cmd": "set_model_result", "com": com_str, "addr": addr, "model": model}) + "\n")
                        success_count += 1
                    time.sleep(1.0)  # 最后一个传感器额外等待，确保Flash写入完成
                    driver.power_off()
                    ble.send(json.dumps({"cmd": "set_model_complete", "com": com_str, "count": success_count}) + "\n")
                    log(f"[BLE] setmodeldone COM{com}: {success_count} ")
        
        elif cmd_type == "read_all_a4":
            # A4 单次读取 - 广播命令，不需要地址
            com_str = cmd.get("com", "1")
            com = int(com_str)
            if not rs485_drivers or com not in rs485_drivers:
                ble.send(json.dumps({"cmd": "a4_single_result", "ok": False, "error": "invalid COM"}) + "\n")
            else:
                driver = rs485_drivers[com]
                protocol = rs485_protocols[com]
                driver.power_on()
                time.sleep(0.3)
                result = protocol.read_all_data(timeout_ms=3000)
                driver.power_off()
                if result:
                    ble.send(json.dumps({
                        "cmd": "a4_single_result", "ok": True, "com": com_str,
                        "auto_id": result.get("auto_id", 0),
                        "addr": result.get("address", 0),
                        "a": round(result.get("a", 0), 2),
                        "b": round(result.get("b", 0), 2),
                        "z": round(result.get("z", 0), 2)
                    }) + "\n")
                    log(f"[BLE] A4 read: AutoID={result.get('auto_id',0)} fixed={result.get('address',0)}")
                else:
                    ble.send(json.dumps({"cmd": "a4_single_result", "ok": False}) + "\n")
                    log(f"[BLE] A4 read: no response")
        
        elif cmd_type == "update_addr_a6":
            # A6 一对一更新地址 (fire-and-forget) + 可选 C7/C8 型号写验证
            com_str = cmd.get("com", "1")
            com = int(com_str)
            new_addr = cmd.get("new_addr", 0)
            model = cmd.get("model", -1)  # -1 表示不修改型号
            if not rs485_drivers or com not in rs485_drivers or new_addr == 0:
                ble.send(json.dumps({"cmd": "update_addr_result", "ok": False, "error": "invalid params"}) + "\n")
            else:
                driver = rs485_drivers[com]
                protocol = rs485_protocols[com]
                driver.power_on()
                time.sleep(0.3)
                # A6: fire-and-forget
                protocol.update_address(new_addr, timeout_ms=500)
                log(f"[BLE] A6 update addr -> {new_addr}")
                
                model_ok = None
                read_model = -1
                if model >= 0:
                    # 等待设备处理地址更新
                    time.sleep(1.0)
                    # C7 写型号
                    protocol.write_model(new_addr, model, timeout_ms=500)
                    log(f"[BLE] C7 write model={model} to addr={new_addr}")
                    # 等待设备处理
                    time.sleep(1.0)
                    # C8 读回验证
                    result = protocol.read_model(new_addr, timeout_ms=500)
                    if result:
                        read_model = result.get("model", -1)
                        model_ok = (read_model == model)
                        log(f"[BLE] C8 verify: expect={model} read={read_model} {'ok' if model_ok else 'FAIL'}")
                    else:
                        model_ok = False
                        log(f"[BLE] C8 verify: no response")
                
                driver.power_off()
                resp = {"cmd": "update_addr_result", "ok": True, "new_addr": new_addr}
                if model >= 0:
                    resp["model"] = model
                    resp["model_ok"] = model_ok
                    resp["read_model"] = read_model
                ble.send(json.dumps(resp) + "\n")
        
        elif cmd_type == "scan_all_a4":
            # A2 扫描 + A3 读取 (替代 A4 遍历，A4 是广播命令不能按 AutoID 迭代)
            com_str = cmd.get("com", "1")
            com = int(com_str)
            start_id = cmd.get("start", 0)
            end_id = cmd.get("end", 960)
            if not rs485_drivers or com not in rs485_drivers:
                ble.send(json.dumps({"cmd": "a4_complete", "error": "invalid COM"}) + "\n")
            else:
                driver = rs485_drivers[com]
                protocol = rs485_protocols[com]
                driver.power_on()
                time.sleep(0.3)
                log(f"[BLE] A2+A3 scan COM{com} AutoID {start_id}-{end_id}")
                ble.send(json.dumps({"cmd": "a4_start", "com": com_str}) + "\n")
                found_count = 0
                for auto_id in range(start_id, end_id + 1):
                    # A2: 按 AutoID 扫描固定地址
                    scan_result = protocol.scan_address(auto_id, timeout_ms=300)
                    if scan_result:
                        fixed_addr = scan_result.get("fixed_addr", 0)
                        # A3: 按固定地址读取轴数据
                        data_result = protocol.read_data(fixed_addr, timeout_ms=500)
                        found_count += 1
                        resp = {
                            "cmd": "a4_result", "com": com_str,
                            "auto_id": auto_id,
                            "addr": fixed_addr,
                            "a": round(data_result.get("a", 0), 2) if data_result else 0,
                            "b": round(data_result.get("b", 0), 2) if data_result else 0,
                            "z": round(data_result.get("z", 0), 2) if data_result else 0
                        }
                        ble.send(json.dumps(resp) + "\n")
                        log(f"  A2+A3 found: AutoID={auto_id} addr={fixed_addr}")
                    # progress every 100
                    if auto_id % 100 == 0:
                        ble.send(json.dumps({
                            "cmd": "a4_progress", "current": auto_id,
                            "total": end_id - start_id + 1
                        }) + "\n")
                driver.power_off()
                ble.send(json.dumps({"cmd": "a4_complete", "com": com_str, "count": found_count}) + "\n")
                log(f"[BLE] A2+A3 scan done COM{com}: found {found_count}")
        
        elif cmd_type == "write_addr":
            # A7 一对一修改地址
            com_str = cmd.get("com", "1")
            com = int(com_str)
            old_addr = cmd.get("old_addr", 0)
            new_addr = cmd.get("new_addr", 0)
            if not rs485_drivers or com not in rs485_drivers or old_addr == 0 or new_addr == 0:
                ble.send(json.dumps({"cmd": "write_addr_result", "ok": False, "error": "invalid params"}) + "\n")
            else:
                driver = rs485_drivers[com]
                protocol = rs485_protocols[com]
                driver.power_on()
                time.sleep(0.3)
                ok = protocol.write_address(old_addr, new_addr, timeout_ms=500)
                driver.power_off()
                ble.send(json.dumps({
                    "cmd": "write_addr_result", "ok": ok,
                    "old_addr": old_addr, "new_addr": new_addr
                }) + "\n")
                log(f"[BLE] write_addr {old_addr}->{new_addr}: {'ok' if ok else 'fail'}")
        
        elif cmd_type == "modify_addr_a7":
            # A7 修改地址 (A7→7B，有响应，不需要额外验证)
            com_str = cmd.get("com", "1")
            com = int(com_str)
            old_addr = cmd.get("old_addr", 0)
            new_addr = cmd.get("new_addr", 0)
            if not rs485_drivers or com not in rs485_drivers or old_addr == 0 or new_addr == 0:
                ble.send(json.dumps({"cmd": "modify_addr_a7_result", "ok": False, "error": "invalid params"}) + "\n")
            else:
                driver = rs485_drivers[com]
                protocol = rs485_protocols[com]
                driver.power_on()
                time.sleep(0.3)
                ok = protocol.write_address(old_addr, new_addr, timeout_ms=1000)
                driver.power_off()
                ble.send(json.dumps({
                    "cmd": "modify_addr_a7_result", "ok": ok,
                    "old_addr": old_addr, "new_addr": new_addr
                }) + "\n")
                log(f"[BLE] A7 modify addr: {old_addr}->{new_addr}: {'ok' if ok else 'fail'}")
        
        elif cmd_type == "write_model_single":
            # C7 一对一修改型号
            com_str = cmd.get("com", "1")
            com = int(com_str)
            addr = cmd.get("addr", 0)
            model = cmd.get("model", 0)
            if not rs485_drivers or com not in rs485_drivers or addr == 0:
                ble.send(json.dumps({"cmd": "write_model_result", "ok": False}) + "\n")
            else:
                driver = rs485_drivers[com]
                protocol = rs485_protocols[com]
                driver.power_on()
                time.sleep(0.3)
                ok = protocol.write_model(addr, model, timeout_ms=500)
                driver.power_off()
                ble.send(json.dumps({
                    "cmd": "write_model_result", "ok": ok,
                    "addr": addr, "model": model
                }) + "\n")
                log(f"[BLE] write_model addr={addr} model={model}: {'ok' if ok else 'fail'}")
        
        elif cmd_type == "set_modbus_id":
            # AB 设置 Modbus ID — 复刻 ref/tab_batch.py BatchWorker
            com_str = cmd.get("com", "1")
            com = int(com_str)
            addr = cmd.get("addr", 0)
            modbus_id = cmd.get("modbus_id", 0)
            if not rs485_drivers or com not in rs485_drivers or addr == 0:
                ble.send(json.dumps({"cmd": "set_modbus_result", "ok": False}) + "\n")
            else:
                driver = rs485_drivers[com]
                protocol = rs485_protocols[com]
                driver.power_on()
                time.sleep(0.3)
                import struct as _struct
                addr_bytes = _struct.pack('>I', addr)
                frame = protocol._build_frame(0x00AB, addr_bytes + bytes([modbus_id]))
                log(f"  TX: {frame.hex().upper()}")
                response = driver.send_and_receive(frame, response_size=15, timeout_ms=2000)
                log(f"  RX: {response.hex().upper() if response else 'None'}")
                # 参考客户端只检查: response and len(response) >= 10
                ok = bool(response and len(response) >= 10)
                driver.power_off()
                ble.send(json.dumps({
                    "cmd": "set_modbus_result", "ok": ok,
                    "addr": addr, "modbus_id": modbus_id
                }) + "\n")
                log(f"[BLE] set_modbus addr={addr} id={modbus_id}: {'ok' if ok else 'fail'}")
        
        elif cmd_type == "batch_addr_write":
            # 批量地址写入 (匹配 inclinometer_client/tab_addr_write.py 逻辑)
            com_str = cmd.get("com", "1")
            com = int(com_str)
            start_autoid = cmd.get("start_autoid", 0)
            end_autoid = cmd.get("end_autoid", 960)
            max_addr = cmd.get("max_addr", 0)
            delay_ms = cmd.get("delay", 300)
            if not rs485_drivers or com not in rs485_drivers or max_addr == 0:
                ble.send(json.dumps({"cmd": "batch_complete", "error": "invalid params"}) + "\n")
            else:
                driver = rs485_drivers[com]
                protocol = rs485_protocols[com]
                driver.power_on()
                time.sleep(0.3)
                log(f"[BLE] batch write COM{com} AutoID {start_autoid}-{end_autoid} maxAddr={max_addr}")
                ble.send(json.dumps({"cmd": "batch_start", "com": com_str}) + "\n")
                current_addr = max_addr
                success_count = 0
                total = end_autoid - start_autoid + 1
                for auto_id in range(start_autoid, end_autoid + 1):
                    ok = protocol.write_address_by_autoid(auto_id, current_addr, timeout_ms=delay_ms)
                    if ok:
                        success_count += 1
                        ble.send(json.dumps({
                            "cmd": "batch_result",
                            "auto_id": auto_id,
                            "addr": current_addr,
                            "ok": True
                        }) + "\n")
                        log(f"  batch: AutoID {auto_id} -> addr {current_addr} ok")
                        current_addr -= 1
                    # progress
                    if (auto_id - start_autoid) % 50 == 0:
                        ble.send(json.dumps({
                            "cmd": "batch_progress",
                            "current": auto_id - start_autoid + 1,
                            "total": total
                        }) + "\n")
                driver.power_off()
                ble.send(json.dumps({
                    "cmd": "batch_complete", "com": com_str,
                    "success": success_count
                }) + "\n")
                log(f"[BLE] batch done COM{com}: {success_count} written")
        
        elif cmd_type == "set_storage":
            # setstorage
            enabled = cmd.get("enabled")
            period = cmd.get("period")
            if enabled is not None:
                config.set("local_storage.enabled", enabled)
            if period in ("month", "day"):
                config.set("local_storage.period", period)
            config.save()
            ble.send(json.dumps({
                "cmd": "set_storage",
                "ok": True,
                "enabled": config.get("local_storage.enabled", False),
                "period": config.get("local_storage.period", "day")
            }) + "\n")
            log(f"[BLE] storageset: enabled={enabled}, period={period}")
            
        elif cmd_type == "set_rs485_ext":
            # set485extended模式（4CH/2CH）
            enabled = cmd.get("enabled", False)
            config.set("system.rs485_ext", enabled)
            config.save()
            ble.send(json.dumps({
                "cmd": "set_rs485_ext",
                "ok": True,
                "enabled": enabled
            }) + "\n")
            log(f"[BLE] 485extendedset: {enabled}")
            
        elif cmd_type == "set_merge_segments":
            # setmergemessage模式
            enabled = cmd.get("enabled", False)
            config.set("system.merge_segments", enabled)
            config.save()
            ble.send(json.dumps({
                "cmd": "set_merge_segments",
                "ok": True,
                "enabled": enabled
            }) + "\n")
            log(f"[BLE] mergemessageset: {enabled}")
            
        elif cmd_type == "get_storage":
            # getstorageset
            ble.send(json.dumps({
                "cmd": "get_storage",
                "enabled": config.get("local_storage.enabled", False),
                "period": config.get("local_storage.period", "day")
            }) + "\n")
            
        elif cmd_type == "list_files":
            # columnoutdatafile
            try:
                from lib.local_storage import LocalStorage
                storage = LocalStorage(config, log)
                files = storage.list_files()
                ble.send(json.dumps({
                    "cmd": "list_files",
                    "files": files
                }) + "\n")
            except Exception as e:
                ble.send(json.dumps({"cmd": "list_files", "error": str(e)}) + "\n")
                
        elif cmd_type == "delete_file":
            # deletedatafile
            filename = cmd.get("filename", "")
            if filename:
                try:
                    from lib.local_storage import LocalStorage
                    storage = LocalStorage(config, log)
                    ok = storage.delete_file(filename)
                    ble.send(json.dumps({"cmd": "delete_file", "filename": filename, "ok": ok}) + "\n")
                except Exception as e:
                    ble.send(json.dumps({"cmd": "delete_file", "error": str(e)}) + "\n")
            else:
                ble.send(json.dumps({"cmd": "delete_file", "error": "filename required"}) + "\n")
            
        elif cmd_type == "read_sensors":
            # 触发一次完整采集 + MQTT 上传 (走 do_network_upload: 4G → WiFi → Ethernet)
            # 设置 force flag, 让主循环跳过 "BLE 连着就中断采集" 的检查
            global _force_collect_now
            _force_collect_now = True
            ble.send(json.dumps({"cmd": "read_sensors", "ok": True}) + "\n")
            log("[BLE] read_sensors: 触发采集+上传 (强制完成, 不被 BLE 中断)")
            return True
        
        elif cmd_type == "set_usb_rw":
            # USB RW 模式（nvm[0]，重启后生效）
            # nvm[0] = 17: daily mode（设备writable）
            # nvm[0] = 其他: flash mode（电脑writable）
            import microcontroller
            enabled = cmd.get("enabled", False)
            if enabled:
                microcontroller.nvm[0] = 0  # flash mode
            else:
                microcontroller.nvm[0] = 17  # daily mode
            ble.send(json.dumps({
                "cmd": "set_usb_rw", 
                "ok": True, 
                "enabled": enabled,
                "note": "重启后生效"
            }) + "\n")
            log(f"[BLE] USB_RW set: nvm[0]={microcontroller.nvm[0]}")
        
        elif cmd_type == "get_usb_rw":
            import microcontroller
            nvm_value = microcontroller.nvm[0]
            enabled = (nvm_value != 17)
            ble.send(json.dumps({
                "cmd": "get_usb_rw",
                "enabled": enabled,
                "mode": "flash mode" if enabled else "daily mode"
            }) + "\n")
            
        elif cmd_type == "set_time":
            # 手机发送当前时间到控制器（Unix timestamp, 必须在 32 位有符号范围, 即 < 2038-01-19）
            timestamp = cmd.get("timestamp", 0)
            # 1735689600 = 2025-01-01, 2147483647 = 2038-01-19 03:14:07 UTC
            if 1735689600 <= timestamp < 2147483648:
                import rtc
                rtc.RTC().datetime = time.localtime(timestamp)
                now = time.localtime()
                time_str = f"{now.tm_year}/{now.tm_mon:02d}/{now.tm_mday:02d} {now.tm_hour:02d}:{now.tm_min:02d}:{now.tm_sec:02d}"
                log(f"[BLE] 手机对时成功: {time_str}")
                ble.send(json.dumps({"cmd": "set_time", "ok": True, "time": time_str}) + "\n")
            else:
                log(f"[BLE] set_time 拒绝: timestamp={timestamp} 超出有效范围")
                ble.send(json.dumps({"cmd": "set_time", "ok": False, "error": "invalid timestamp (must be 2025..2038)"}) + "\n")
            
        elif cmd_type == "reboot":
            # 重启设备 — 软复位 (supervisor.reload): 不复位芯片 → BLE 控制器不被热复位,
            # 避免"带活动连接热复位 → 下次 BLERadio() 卡死、板子不再广播"那个问题。
            # (切 USB 模式之类需硬复位的, 走 CDC #reboot / 按 RESET 键)
            ble.send(json.dumps({"cmd": "reboot", "ok": True}) + "\n")
            log("[BLE] 正在重启 (soft reload)...")
            time.sleep(0.5)  # 等待 BLE 响应发送完成
            supervisor.reload()

        elif cmd_type == "ota_begin":
            # BLE 现场固件升级 — 进入 OTA 收帧模式 (独占 BLE 收帧循环)
            # 正常返回=会话结束/中止, 回正常 BLE 循环; 不返回=已 _apply_update reset
            log("[BLE] 进入 OTA 接收模式")
            try:
                from app.ble_ota import run_ble_ota
                run_ble_ota(ble, config, cmd, log=log)
            except Exception as _ota_e:
                log(f"[BLE] OTA 异常退出: {_ota_e}")
            # 清场: 会话中止后手机可能还在发 0xA5 数据帧, 残留二进制不能
            # 流回普通命令解析 (decode 出乱码/吞掉下一条命令)
            try:
                _n = ble._uart.in_waiting
                if _n:
                    ble._uart.read(_n)
                ble._cmd_buffer = ""
            except Exception:
                pass
            return False

        else:
            log(f"[BLE] unknown cmd: {cmd_type}")
            
    except Exception as e:
        log(f"[BLE] parse err: {e}")
    
    return False

# ============================================================
# 主程序
# ============================================================

def _boot_stage(n):
    """往 nvm[15] 记 boot 阶段, 定位热复位卡死点 (任何 USB 模式都能写)"""
    try:
        import microcontroller as _m
        _m.nvm[15] = n & 0xFF
    except Exception:
        pass


def main():
    global _force_collect_now, _force_collect_com, _wifi_downlink
    log("=" * 50)
    log("  ESP32-S3 柔性测斜仪控制器 - 同步版")
    log("=" * 50)

    # ★ Boot 阶段追踪 — 读上次到达阶段 (上次若热复位卡死, 这里能看到卡在哪步)
    #   阶段: 1=入口 2=config 3=OTA_resume 4=硬件init 5=RS485 6=BLE前 7=BLE后 8=CDC窗口 100=进主循环(成功)
    try:
        import microcontroller as _mc0
        _prev_stage = _mc0.nvm[15]
        log(f"[BOOT-STAGE] 上次启动到达 stage={_prev_stage}  (100=上次正常跑完, 其它=卡在该阶段)")
    except Exception as _bse:
        log(f"[BOOT-STAGE] read err: {_bse}")
    _boot_stage(1)

    # boot count: nvm[16:18] uint16 LE, 每次启动 +1 (telemetry, device_reporter 读取)
    try:
        import microcontroller as _mcbc
        _bc = _mcbc.nvm[16] | (_mcbc.nvm[17] << 8)
        _bc = 0 if _bc >= 0xFFFF else _bc + 1   # 0xFFFF=未初始化 NVM, 归零
        _mcbc.nvm[16] = _bc & 0xFF
        _mcbc.nvm[17] = (_bc >> 8) & 0xFF
        log(f"[BOOT] boot_count={_bc}")
    except Exception as _bce:
        log(f"[BOOT] boot_count err: {_bce}")

    # 加载配置（从 NVM，若空则用默认值）
    config = ConfigManager()
    log(f"[配置] 设备ID: {config.get('system.id')}")
    _boot_stage(2)

    # 注: 4G OTA resume 块已移除 (YunDTU 不支持 HTTP, 改走 BLE 现场升级).
    #     原来这里做 flash→daily 切换后的 4G 下载续传, 现不再需要.
    _boot_stage(3)

    interval_preset = config.get("system.interval_preset", 5)
    interval_sec = get_interval_seconds(interval_preset, config.get("system.interval_custom_min", 60))
    sleep_mode = config.get("system.sleep_mode", "light")
    log(f"[配置] 采集间隔: {interval_sec//60}分钟, 休眠: {sleep_mode}")
    
    # 打印当前时间
    now = time.localtime()
    log(f"[时间] {now.tm_year}/{now.tm_mon:02d}/{now.tm_mday:02d} {now.tm_hour:02d}:{now.tm_min:02d}:{now.tm_sec:02d}")
    
    # 初始化硬件
    led = LEDDriver()
    voltage = VoltageMonitor()
    counter = UploadCounter()
    fw_version = config.get("system.firmware_version", FIRMWARE_VERSION)
    formatter = DataFormatter(config, counter, fw_version)

    # OTA 首次启动验证模式
    # 切换后第一次启动时 NVM first_boot_flag=1, 本 cycle 成功上报即 commit (清 backup),
    # 失败/崩溃则 safemode.py 触发 rollback 回旧版本
    _ota_verifying = False  # default, 防 try 抛后 cycle 内 UnboundLocal
    try:
        from app import ota_nvm as _ota_nvm_mod
        _ota_verifying = _ota_nvm_mod.is_first_boot_after_ota()
        if _ota_verifying:
            log("[OTA] ⚠️ 验证模式 (first_boot_after_ota=1): 本 cycle 上报成功即 commit, 失败将触发 rollback")
    except Exception as _ota_init_err:
        log(f"[OTA] verify init err: {_ota_init_err}")

    # 05-11 新硬件: 初始化 SPWALLON / CURCTR + 全局暴露 voltage 供 cmd handler 使用
    global _voltage_monitor, _rs485mod
    _voltage_monitor = voltage
    _init_new_hw_pins()
    # 12V boost(SPWALLON) 按采集时序开关: 采集开始开 → 读完所有通道关 (用户指定)。
    # 把 set_boost 注入 RS485 driver, 用引用计数: 采集循环外层 boost_acquire() hold 一个,
    # 每通道 power_on/off 再加减一个 → 整个采集 SPWALLON 只开一次、读完关一次; VOUTCTR 每通道开关。
    # 手动 #read/#scan/#write_addr 经 driver.power_on/off 也自动开关 SPWALLON。
    set_boost(False)
    from drivers import rs485 as _rs485mod
    _rs485mod.set_boost_control(set_boost)
    log("[HW] 12V boost(SPWALLON): 默认关; 采集时开→读→关 (VOUTCTR 每通道开关)")
    _boot_stage(4)
    
    # 初始化本地存储
    from lib.local_storage import LocalStorage
    storage = LocalStorage(config, log)

    # 离线补传缓存: 上传失败的周期把 segments 存 /data/, 下次上传成功时补发
    # (daily 模式盘可写才生效; flash 模式写失败静默, 等同没缓存)
    from app.data_logger import DataLogger
    datalogger = DataLogger()

    # 读取电压
    voltages = voltage.read_all()
    log(f"[电压] vin={voltages.get('vin', 0):.2f}V")
    
    # 内存状态
    gc.collect()
    free_mem = gc.mem_free()
    log(f"[内存] 空闲: {free_mem // 1024} KB")
    
    # 初始化 RS485 驱动和协议
    from drivers.rs485 import RS485Driver
    
    rs485_drivers = {}
    rs485_protocols = {}
    rs485_sensors = {}
    
    for ch in [1, 2]:
        if config.get(f"rs485_{ch}.enabled", False):
            sensors = config.get(f"rs485_{ch}.sensors", [])
            if sensors:
                baud = config.get(f"rs485_{ch}.baud", 9600)
                proto_name = config.get(f"rs485_{ch}.protocol", "PRIVATE_V2026")
                driver = RS485Driver(ch, baud)
                protocol = make_protocol(proto_name, driver)
                rs485_drivers[ch] = driver
                rs485_protocols[ch] = protocol
                rs485_sensors[ch] = sensors
                log(f"[RS485] 通道{ch}: {len(sensors)}个传感器, {baud} baud, 协议={proto_name}")
    
    total_sensors = sum(len(s) for s in rs485_sensors.values())
    log("=" * 50)
    log(f"  初始化完成，共 {total_sensors} 个传感器")
    log("  发送 @scan 扫描地址, #help 查看帮助")
    log("=" * 50)
    _boot_stage(5)

    # 初始化 BLE
    ble = None
    log("[BLE] 开始初始化...")
    if config.get("ble.enabled", True):
        try:
            log("[BLE] 导入 adafruit_ble 库...")
            from lib.ble_uart import BLEUART, _HAS_ADAFRUIT_BLE
            log(f"[BLE] adafruit_ble 可用: {_HAS_ADAFRUIT_BLE}")

            ble_name = f"UniControl_{config.get('system.id', '0000')}"
            log(f"[BLE] 创建 BLEUART: {ble_name}")
            _boot_stage(6)
            ble = BLEUART(name=ble_name)
            _boot_stage(7)

            if ble._initialized:
                log("[BLE] 初始化成功，开始广播...")
                ble.start_advertising()
                _boot_stage(8)
                log(f"[BLE] ✓ 广播已启动: {ble_name}")
            else:
                log("[BLE] ✗ 初始化失败")
                ble = None
        except ImportError as e:
            log(f"[BLE] ✗ 库导入失败: {e}")
            ble = None
        except Exception as e:
            log(f"[BLE] ✗ 初始化异常: {e}")
            import sys
            sys.print_exception(e)
            ble = None
    else:
        log("[BLE] 已禁用 (配置: ble.enabled=false)")
    
    # ============================================================
    # 智能启动逻辑
    # Phase 1: 3 秒 CDC 检测窗口
    # Phase 2: 有 CDC → 交互模式等 #read (60s 无输入自动开始)
    #          无 CDC → 直接开始采集
    # ============================================================
    _boot_stage(9)
    log("[启动] 3 秒 CDC 检测窗口...")
    cdc_detected = False
    detect_start = time.monotonic()
    while (time.monotonic() - detect_start) < 3.0:
        # ★ 先查输入: 任何字节都算"有人接管" → 进交互模式。必须在 process_commands 之前,
        #   否则 process_commands 会先把 in_waiting drain 光 (PC 工具探针是空行 \r\n,
        #   不是命令, 返回假但已读掉) → 永远检测不到。
        if usb_cdc.data and usb_cdc.data.in_waiting > 0:
            cdc_detected = True
        cdc_result = process_commands(rs485_drivers, rs485_protocols, config)
        if cdc_result == "reload_config":
            cdc_detected = True
            interval_preset = config.get("system.interval_preset", 0)
            interval_sec = get_interval_seconds(interval_preset, config.get("system.interval_custom_min", 60))
            log(f"[配置更新] 间隔已更新为: {interval_sec}秒")
        elif cdc_result:
            cdc_detected = True
            log("[启动] 收到采集命令，开始工作")
            break
        if ble:
            ble_result = process_ble_command(ble, config, rs485_drivers, rs485_protocols)
            if ble_result == True:
                break
            elif ble_result == "reload_config":
                interval_preset = config.get("system.interval_preset", 0)
                interval_sec = get_interval_seconds(interval_preset, config.get("system.interval_custom_min", 60))
                log(f"[配置更新] 间隔已更新为: {interval_sec}秒")
        time.sleep(0.1)
    
    # Phase 2: CDC 交互模式
    if cdc_detected:
        log("[启动] CDC 已检测到，进入交互模式 (发送 #read 开始采集, 60s 无输入自动开始)")
        last_input_time = time.monotonic()
        while True:
            cdc_result = process_commands(rs485_drivers, rs485_protocols, config)
            if cdc_result == "reload_config":
                last_input_time = time.monotonic()
                interval_preset = config.get("system.interval_preset", 0)
                interval_sec = get_interval_seconds(interval_preset, config.get("system.interval_custom_min", 60))
                log(f"[配置更新] 间隔已更新为: {interval_sec}秒")
            elif cdc_result:
                log("[启动] 收到采集命令，开始工作")
                break
            
            # 检测新的 CDC 输入，刷新超时
            if usb_cdc.data and usb_cdc.data.in_waiting > 0:
                last_input_time = time.monotonic()
            
            # BLE 命令
            if ble:
                ble_result = process_ble_command(ble, config, rs485_drivers, rs485_protocols)
                if ble_result == True:
                    break
                elif ble_result == "reload_config":
                    interval_preset = config.get("system.interval_preset", 0)
                    interval_sec = get_interval_seconds(interval_preset, config.get("system.interval_custom_min", 60))
                    log(f"[配置更新] 间隔已更新为: {interval_sec}秒")
                # 手机 BLE 连着就不超时 — 否则 OTA 操作期间 (连接/选版本) 板子会自己去采集/睡眠,
                # BLE 断开导致 OTA 没法开始。一旦 ota_begin 进来, run_ble_ota 独占循环, 更不受此影响。
                if ble.is_connected():
                    last_input_time = time.monotonic()

            # 60s 无输入超时
            idle_sec = time.monotonic() - last_input_time
            if idle_sec >= 60:
                # #read_live 连续读会话进行中且 USB 仍连着 → 不超时去采集 (否则采集循环会断掉 12V);
                # 一旦 PC 拔线 (usb_cdc.data.connected=False) 即放行, 恢复正常采集/睡眠, 不会卡死野外设备。
                if _live_read_active and usb_cdc.data and usb_cdc.data.connected:
                    last_input_time = time.monotonic()
                else:
                    log("[启动] 60s 无 CDC 输入，自动开始采集")
                    break
            
            time.sleep(0.3)
    else:
        log("[启动] 无 CDC 输入，直接开始采集")
    
    # 主循环
    _boot_stage(100)   # boot 完整成功
    cycle = 0
    while True:
        # 周期开头默认把 4G PEN 驱低 = 模块关; 真要连时 _make_modem 会释放占用并驱高。
        # 覆盖"采集期间不联网"那段醒着时间, 模块不再空转/闪灯。
        modem_pwr_idle_low()
        # 如果 interval_sec == 0，不自动采集，只等待命令
        if interval_sec == 0:
            led.set_mode("idle")
            log("[待命] 等待命令... (启动连 4G 抓远程配置; 发 #help 查看帮助)")
            # 待命启动第一次走 heavy (确保 DTU 配置已存进模块, 新板必需); 之后周期性走 light (零 flash)
            _sig = fetch_remote_in_standby(config, rs485_drivers, heavy=True)
            modem_pwr_idle_low()   # 初次拉完远程配置, modem 已 deinit, 驱低关模块再等命令
            if _sig and _sig.get("config_applied"):
                log("[待命] 远程配置已应用, 软重启生效")
                time.sleep(0.3)
                supervisor.reload()
            _last_remote_check = time.monotonic()
            while True:
                cdc_result = process_commands(rs485_drivers, rs485_protocols, config)
                # 周期性重连 4G 查远程配置 (默认 15 min, light 零 flash), 应用了就软重启生效
                if (time.monotonic() - _last_remote_check) > STANDBY_REMOTE_CHECK_SEC:
                    _last_remote_check = time.monotonic()
                    _sig = fetch_remote_in_standby(config, rs485_drivers)
                    modem_pwr_idle_low()   # 拉完远程配置, modem 已 deinit, 重新驱低关模块
                    if _sig and _sig.get("config_applied"):
                        log("[待命] 远程配置已应用, 软重启生效")
                        time.sleep(0.3)
                        supervisor.reload()
                if cdc_result == "reload_config":
                    interval_preset = config.get("system.interval_preset", 0)
                    interval_sec = get_interval_seconds(interval_preset, config.get("system.interval_custom_min", 60))
                    log(f"[配置更新] 间隔已更新为: {interval_sec}秒")
                    if interval_sec > 0:
                        break
                elif cdc_result:
                    break
                # BLE 命令处理
                if ble:
                    ble_result = process_ble_command(ble, config, rs485_drivers, rs485_protocols)
                    if ble_result == True:
                        break
                    elif ble_result == "reload_config":
                        # 重新加载间隔配置
                        interval_preset = config.get("system.interval_preset", 0)
                        interval_sec = get_interval_seconds(interval_preset, config.get("system.interval_custom_min", 60))
                        log(f"[配置更新] 间隔已更新为: {interval_sec}秒")
                        if interval_sec > 0:  # 如果不再是待命模式，退出待命循环
                            break
                    if ble.is_connected():
                        time.sleep(0.3)
                    else:
                        time.sleep(0.5)
                else:
                    time.sleep(0.5)
            # 重新加载传感器 (可能被 scan/write_addr 更新)
            for ch in rs485_sensors.keys():
                rs485_sensors[ch] = config.get(f"rs485_{ch}.sensors", [])
        
        cycle += 1
        cycle_start = time.monotonic()
        remote_skip_sleep = False  # 远程 read_now 置位: 本周期结束跳过休眠立即再采
        log(f"[Trace] 进入周期 {cycle}, interval_sec={interval_sec}, time.time()={int(time.time())}")
        # 对时必须在 scheduled_time 计算/采集 之前 — 否则冷启动首周期的时间戳是 1970。
        # 平时 (RTC 有效且当天已对过) try_time_sync 内部直接返回, 零开销;
        # 需要对时时返回的 modem 实例留给本周期 upload 复用 (避免 IO44 重复创建)。
        _sync_modem = None
        try:
            _synced, _sync_modem = try_time_sync(config)
        except Exception as _tse:
            log(f"[时间] 对时异常: {_tse}")
        # 计算本周期对齐的准点时间 (用于数据时间戳)
        if interval_sec > 0:
            try:
                scheduled_time = get_aligned_scheduled_time(interval_sec)
                st = time.localtime(scheduled_time)
                log(f"\n[周期 {cycle}] 准点 {st.tm_hour:02d}:{st.tm_min:02d}:00, 开始采集...")
            except OverflowError as e:
                log(f"[周期 {cycle}] 时间戳溢出 ({e}), RTC 异常, 用 monotonic 兜底, 开始采集...")
                scheduled_time = 0
        else:
            scheduled_time = int(time.time())
            log(f"\n[周期 {cycle}] 开始采集...")
        led.set_mode("transmit")
        
        # 采集sensordata (sync)
        all_data = []
        ble_interrupted = False

        # read_sensors 强制采集时, 通过 BLE 实时上报进度给 APP
        ble_progress_active = _force_collect_now and ble and ble.is_connected()
        if ble_progress_active:
            try:
                total = sum(len(rs485_sensors.get(ch, [])) for ch in rs485_sensors.keys())
                ble.send(json.dumps({"cmd": "read_sensors_start", "cycle": cycle, "total": total}) + "\n")
            except Exception:
                pass

        log(f"[DEBUG] rs485_sensors keys: {list(rs485_sensors.keys())}")

        # 采集开始: 开 SPWALLON(12V) 并 hold 住整个采集 —— 每通道 VOUTCTR 进出不会把它关掉,
        # 读完所有通道再 boost_release() 关 SPWALLON。12V 一个采集周期只拉一次。
        _rs485mod.boost_acquire()

        for ch, sensors in rs485_sensors.items():
            if _force_collect_com is not None and ch != _force_collect_com:
                log(f"[CH{ch}] skip (CDC #read 指定只采 COM{_force_collect_com})")
                continue
            driver = rs485_drivers[ch]
            protocol = rs485_protocols[ch]

            log(f"[CH{ch}] reading {len(sensors)}  sensors...")

            # pwr on
            driver.power_on()
            time.sleep(0.3)  # pwr on延时 300ms

            # read from top to bottom (config.json order)
            success_count = 0
            fail_count = 0
            for sensor_cfg in sensors:
                addr = sensor_cfg.get("addr", 0)
                model = sensor_cfg.get("model", 0)
                try:
                    log(f"  [CH{ch}] readaddr {addr}...")
                    # timeout 5000ms per参考client
                    data = protocol.read_data(addr, timeout_ms=5000)
                    if data:
                        data["channel"] = ch
                        data["model"] = model
                        all_data.append(data)
                        success_count += 1
                        log(f"  [CH{ch}] addr {addr}: A={data.get('a', 0):.2f}, B={data.get('b', 0):.2f}")
                        if ble_progress_active:
                            try:
                                ble.send(json.dumps({
                                    "cmd": "sensor_data", "com": str(ch), "addr": addr,
                                    "a": data.get("a", 0), "b": data.get("b", 0), "z": data.get("z", 0),
                                    "ok": True
                                }) + "\n")
                            except Exception:
                                pass
                    else:
                        fail_count += 1
                        all_data.append({
                            "addr": addr,
                            "address": addr,
                            "channel": ch,
                            "model": model,
                            "a": 0, "b": 0, "z": 0,
                            "status": "W"
                        })
                        log(f"  [CH{ch}] addr {addr}: no resp")
                        if ble_progress_active:
                            try:
                                ble.send(json.dumps({"cmd": "sensor_data", "com": str(ch), "addr": addr, "ok": False}) + "\n")
                            except Exception:
                                pass
                except Exception as e:
                    fail_count += 1
                    all_data.append({
                        "addr": addr,
                        "address": addr,
                        "channel": ch,
                        "model": model,
                        "a": 0, "b": 0, "z": 0,
                        "status": "W"
                    })
                    log(f"  [CH{ch}] addr {addr} Err: {e}")
                    if ble_progress_active:
                        try:
                            ble.send(json.dumps({"cmd": "sensor_data", "com": str(ch), "addr": addr, "ok": False}) + "\n")
                        except Exception:
                            pass

                time.sleep(0.15)  # sensorinterval 150ms

                # check BLE conn，ifconn则INT采集 (read_sensors 强制采集时跳过)
                if ble and ble.is_connected() and not _force_collect_now:
                    log(f"[BLE] connection detected，interrupt, standby")
                    ble_interrupted = True
                    break
            
            # if BLE INT，跳outCHloop
            if ble_interrupted:
                driver.power_off()
                break
            
            log(f"[CH{ch}] done: ok {success_count}, fail {fail_count}")

            # pwr off
            driver.power_off()

        # 所有通道读完: 关 SPWALLON(12V) (释放外层 hold; 计数归零真正驱低 GPIO39)
        _rs485mod.boost_release()

        log(f"[采集] done，read {len(all_data)} ")
        
        # if BLE INT，skipup报，进in BLE cmdprocessloop
        if ble_interrupted:
            # 对时连接的 modem 不再被本周期使用, 释放 (防 PEN 挂高 + UART 占用)
            if _sync_modem:
                try:
                    _sync_modem.deinit()
                except Exception:
                    pass
                _sync_modem = None
            all_data.clear()  # 丢弃不完整数据
            log("[BLE] 进inStandby模式，Wait BLE cmd或断开...")
            led.set_mode("idle")
            while ble and ble.is_connected():
                # 同时process CDC and BLE cmd
                process_commands(rs485_drivers, rs485_protocols, config)
                # read_sensors 等强制命令返回 True → 退出 standby 立即重启采集
                if process_ble_command(ble, config, rs485_drivers, rs485_protocols):
                    log("[BLE Standby] 收到强制采集命令, 退出 standby")
                    break
                time.sleep(0.3)
            log("[BLE] standby 退出, resume 采集")
            continue
        
        # fmt化data
        voltages = voltage.read_all()
        modem_instance = None  # 4G 上传连接 (下行读取后释放)
        _upload_evidence = False  # 本周期"上报真的到了服务器"的证据 (verify/OTA commit 用)
        _was_verifying = None     # handle_remote 之前采样的验证态 (None=本周期没采样过)
        if all_data:
            segments = formatter.format_segments(all_data, voltages, scheduled_time=scheduled_time, tz_offset_s=_device_tz_offset_s)

            # send到 CDC (完整 JSON，and 4G Uploadfmt一致)
            log(f"[CDC] send {len(segments)} seg...")
            for seg in segments:
                if usb_cdc.data:
                    usb_cdc.data.write((seg + "\r\n").encode())

            # send到net (4G > WiFi > Ethernet)
            # 对时已在周期开头完成, _sync_modem (若有) 传给 upload 复用
            if ble_progress_active:
                try:
                    ble.send(json.dumps({"cmd": "upload_start", "segments": len(segments)}) + "\n")
                except Exception:
                    pass
            # 离线补传: 之前失败周期缓存的 segments 排在本次之前一起发
            # (老数据自带原始 time 字段, 服务器按设备时间入库 + 唯一索引去重)
            _pending_files = []
            _upload_segments = segments
            try:
                _pending_files = datalogger.get_pending_files()[:3]  # 一次最多补 3 个周期
                if _pending_files:
                    _old_segs = []
                    for _pf in _pending_files:
                        with open(_pf, "r") as _pff:
                            for _obj in json.load(_pff):
                                _old_segs.append(json.dumps(_obj))
                    _upload_segments = _old_segs + segments
                    log(f"[补传] {len(_pending_files)} 个缓存文件 ({len(_old_segs)} seg) 随本次一起发")
            except Exception as _ple:
                log(f"[补传] 读缓存失败: {_ple}")
                _pending_files = []
                _upload_segments = segments
            modem_instance = do_network_upload(config, _upload_segments, existing_modem=_sync_modem)
            _sync_modem = None  # 所有权已移交 do_network_upload (返回/释放二选一)
            update_last_send_day()
            # 上传成功 → 删已补传的缓存; 失败 → 本周期 segments 落盘 (下次补)
            try:
                if _last_upload_ok:
                    for _pf in _pending_files:
                        datalogger.delete_file(_pf)
                    if _pending_files:
                        log(f"[补传] 完成, 清掉 {len(_pending_files)} 个缓存文件")
                else:
                    if datalogger.log_segments(segments):
                        log("[补传] 本周期数据已缓存, 待网络恢复补发")
            except Exception as _ce:
                log(f"[补传] 缓存处理失败: {_ce}")

            if ble_progress_active:
                try:
                    # do_network_upload 返回 modem 仅在 4G 上传时, WiFi/Eth 也可能成功但返回 None,
                    # 所以这里只通知 "完成", 成功与否看 MQTT broker
                    ble.send(json.dumps({"cmd": "upload_done"}) + "\n")
                except Exception:
                    pass

            # 发送设备状态报告 (独立连接到硬编码 controller-manager broker)
            try:
                if modem_instance:
                    from app.device_reporter import send_report_via_modem
                    send_report_via_modem(config, modem_instance)
                elif config.get("network.wifi.enabled", False):
                    from app.device_reporter import send_report_via_wifi
                    send_report_via_wifi(config)
                log("[Report] device status sent")
            except Exception as e:
                log(f"[Report] err: {e}")

            # ── 远程下行指令 + srv_ack: 4G 用 modem, WiFi 用保留的 MQTT 连接 ──
            # 服务器 (mqtt_bridge) 收到数据/报告后会往 cirpy-info/<cid> 回 srv_ack
            # (非 retained) — 这是"上报真到了服务器"的唯一可靠证据:
            # YunDTU 透传 publish() 只是写串口, 必返回 True, 不能当上报成功用。
            _downlink_src = modem_instance or _wifi_downlink
            _sig = None
            if _downlink_src:
                try:
                    from app import remote_cmd as _rc
                    import app.remote_cmd_nvm as _rcn
                    # 必须在 handle_remote 之前记录验证态: 本周期刚应用的新配置不能
                    # 用本周期 (旧配置跑出来的) 的 ack 立即 commit, 要留到下周期验证
                    _was_verifying = _rcn.is_verifying()
                    # 验证/OTA 自检窗口给足 10s 等 ack; 平时 3s 读 retained 指令
                    _dl_timeout = 10000 if (_was_verifying or _ota_verifying) else 3000
                    _sig = _rc.handle_remote(config, _downlink_src, rs485_drivers, log,
                                             timeout_ms=_dl_timeout)
                    _upload_evidence = _rc.last_ack_seen()
                    if _upload_evidence:
                        log("[Remote] 收到 srv_ack (服务器确认收到上报)")
                except Exception as _he:
                    log(f"[Remote] handle 异常: {_he}")
                    _sig = None
            else:
                # 无下行通道 (ETH-only / 全部失败): 退回 _last_upload_ok 作为证据
                _upload_evidence = _last_upload_ok

            # OTA 自检 commit — 上报有证据才算自检通过 (避免断网状态下空 commit)
            if _ota_verifying and _upload_evidence:
                try:
                    from app.ota_updater import commit_after_selftest
                    commit_after_selftest()
                    _ota_verifying = False
                    log("[OTA] 自检通过, 已 commit 新版本")
                except Exception as _e:
                    log(f"[OTA] commit err: {_e}")

            if _sig:
                    # 智能生效: 能 live 改的不重启; 需重建的才 reload (deep 睡靠下周期自然重启)
                    if _sig["interval_changed"]:
                        interval_preset = config.get("system.interval_preset", 5)
                        interval_sec = get_interval_seconds(interval_preset, config.get("system.interval_custom_min", 60))
                        log(f"[Remote] 间隔 live 更新 → {interval_sec}s")
                    if _sig["sleep_changed"]:
                        sleep_mode = config.get("system.sleep_mode", "light")
                        log(f"[Remote] 休眠模式 live 更新 → {sleep_mode}")
                    for _ch in _sig["live_sensor_channels"]:
                        if _ch in rs485_sensors:
                            rs485_sensors[_ch] = config.get(f"rs485_{_ch}.sensors", [])
                            log(f"[Remote] CH{_ch} 地址表 live 重读 ({len(rs485_sensors[_ch])} 个)")
                    _acts = _sig["actions"]
                    if "read_now" in _acts:
                        remote_skip_sleep = True
                        log("[Remote] action read_now: 跳过本次休眠, 立即再采一次")
                    if "reboot" in _acts:
                        log("[Remote] action reboot, 应用完重启...")
                        time.sleep(0.5)
                        microcontroller.reset()
                    if ("reload" in _acts) or (_sig["need_reload"] and sleep_mode != "deep"):
                        _why = "action reload" if "reload" in _acts else "配置需重建"
                        log(f"[Remote] {_why}, 软重启生效...")
                        time.sleep(0.3)
                        supervisor.reload()

            # 释放 4G modem (释放 GPIO43/44 UART 引脚, 避免下一 cycle IO44 in use)
            if modem_instance:
                try:
                    modem_instance.deinit()
                    log("[4G] modem released")
                except Exception as e:
                    log(f"[4G] deinit err: {e}")
                modem_instance = None
            # 释放 WiFi 下行连接 (断 MQTT + 关射频)
            if _wifi_downlink:
                try:
                    _wifi_downlink.disconnect()
                    log("[WiFi] downlink released")
                except Exception:
                    pass
                _wifi_downlink = None

            # save到storage
            if storage.enabled:
                try:
                    # 转换as存储fmt (local_storage 用 "address" key)
                    storage_readings = []
                    for d in all_data:
                        storage_readings.append({
                            "address": d.get("address", d.get("addr", 0)),
                            "a": d.get("a", 0),
                            "b": d.get("b", 0),
                            "z": d.get("z") if d.get("z") is not None else None
                        })
                    storage.save_readings(storage_readings)
                except Exception as _ste:
                    log(f"[Storage] save err: {_ste}")
        else:
            # 没有任何数据可上传 (地址表空/坏配置) — 本周期没有上报证据
            _upload_evidence = False
            if _sync_modem:
                try:
                    _sync_modem.deinit()
                except Exception:
                    pass
                _sync_modem = None

        # ── 远程配置 verify/commit/rollback (在 if all_data: 之外! 坏配置导致
        #    采不到任何数据时也必须计一次失败, 否则永远卡 verifying, retained 永不清) ──
        # 证据 = 服务器 srv_ack (4G/WiFi) 或 _last_upload_ok (无下行通道时);
        # _was_verifying 在 handle_remote 之前采样, 本周期新应用的配置不会被立即 commit.
        try:
            import app.remote_cmd_nvm as _rcn
            if _was_verifying is None:
                _was_verifying = _rcn.is_verifying()
            if _was_verifying:
                if _upload_evidence:
                    from app import remote_cmd as _rc
                    _rc.commit_applied(log)
                else:
                    _fail_n = _rcn.note_fail()
                    log(f"[Remote] 配置验证: 本周期上报无服务器确认 ({_fail_n}/{_rcn.ROLLBACK_THRESHOLD})")
                    if _rcn.should_rollback():
                        from app import remote_cmd as _rc
                        _rc.do_rollback(config, log)
                        log("[Remote] 已回滚, 重启加载旧配置...")
                        time.sleep(0.5)
                        microcontroller.reset()
        except Exception as _rcerr:
            log(f"[Remote] verify 处理异常: {_rcerr}")

        log(f"[Cycle {cycle}] read done")

        # 清除 read_sensors 强制采集 flag (一次性, 仅本轮生效)
        if _force_collect_now:
            _force_collect_now = False
            log("[BLE] read_sensors 本轮已完成, 清除 force flag")
            if ble and ble.is_connected():
                try:
                    ble.send(json.dumps({"cmd": "read_sensors_complete", "cycle": cycle}) + "\n")
                except Exception:
                    pass

        # 清除 #read [com] 单 COM force (一次性, 仅本轮生效)
        if _force_collect_com is not None:
            log(f"[CDC] #read COM{_force_collect_com} 本轮已完成, 清除 force flag")
            _force_collect_com = None

        # 注: 4G/WiFi HTTP OTA 检查已移除 — YunDTU 透传模块不支持 HTTP, 改走 BLE 现场升级.
        #     固件切换/备份/回滚核心 (ota_updater._apply_update/commit/rollback) 保留供 BLE OTA 复用.

        # Memstatus
        gc.collect()
        free_mem = gc.mem_free()
        log(f"[Mem] free: {free_mem // 1024} KB")
        
        # if interval_sec == 0，回到Standby模式
        if interval_sec == 0:
            continue
        
        # 远程 read_now: 跳过休眠立即再采一次
        if remote_skip_sleep:
            log("[Remote] read_now: 跳过休眠, 立即再采一次")
            continue

        # processpendingprocess的 CDC cmd
        if process_commands(rs485_drivers, rs485_protocols, config):
            log("[skipSleep] 收到 #read cmd")
            continue  # skipSleep，立即采集
        
        # ============================================================
        # Deep Sleep 模式: 真正的深度休眠
        # BLE 连接时跳过深度休眠，保持交互
        # ============================================================
        if sleep_mode == "deep" and interval_sec > 0:
            # BLE 连接中 → 不进深度休眠，轻休眠等待命令
            if ble and ble._initialized and ble._ble.connected:
                ble_remaining = get_sleep_until_next_boundary(interval_sec)
                log(f"[Deep Sleep] BLE 已连接，等待命令 {ble_remaining}s...")
                wait_start = time.monotonic()
                while (time.monotonic() - wait_start) < ble_remaining:
                    if process_ble_command(ble, config, rs485_drivers, rs485_protocols):
                        log("[Trace] BLE wait: process_ble_command 返回 True, break")
                        break
                    process_commands(rs485_drivers, rs485_protocols, config)
                    time.sleep(0.5)
                    if not (ble._ble.connected):
                        log("[BLE] 已断开，进入深度休眠")
                        break
                else:
                    log("[Trace] BLE wait: 超时, 进入下一周期")
                    continue  # 时间到，下一个采集周期
                # BLE 断开后走正常深度休眠流程
                log(f"[Trace] BLE wait 退出, ble.connected={ble._ble.connected}")
                if ble._ble.connected:
                    log("[Trace] continue → 触发新采集周期")
                    continue  # BLE 命令触发了新采集
                log("[Trace] BLE 断开, fall through 进入深度休眠流程")
            # 1. (原 "4G PSM" 段已删: modem 在上传后必已 deinit, 这里永远拿不到实例;
            #    且 YunDTU(Modem4G) 无 PSM. 4G 关断靠 modem_pwr_idle_low() 把 PEN(GPIO14) 驱低
            #    (高开低关), 且 2026-06-15 起深睡 hold 住 (见下方第 5 段) → 深睡灯灭。)
            # 2. 关闭 BLE 广播
            if ble:
                try:
                    ble.stop_advertising()
                    log("[BLE] 已停止广播")
                except:
                    pass
            
            # 3. 关闭所有 RS485 驱动 (VCC/DE/ADDR/UART GPIO)
            for ch, driver in rs485_drivers.items():
                try:
                    driver.deinit()  # power_off + uart.deinit + gpio.deinit
                    log(f"[RS485] CH{ch} 已释放")
                except:
                    pass
            
            # 4. 关闭 LED
            try:
                led.off()
                led.deinit()
                log("[LED] 已关闭")
            except:
                pass

            # 4.5 关 12V boost
            try:
                set_boost(False)   # 驱低 GPIO39
                log("[Deep Sleep] 12V boost 已关")
            except Exception as e:
                log(f"[Deep Sleep] boost off err: {e}")

            # 5. 【实验 2026-06-15】深睡 hold 省电脚 (preserve_dios) — 让这些脚穿过深睡保持低,
            #    不再浮空/被重新使能。三根全靠"唤醒重建 DigitalInOut 释放 hold"成立:
            #      • PEN(GPIO14) 低 → 关 4G (M5V 常供); 唤醒由 modem_pwr_idle_low/_make_modem 重建。
            #      • BOOST_EN(GPIO39) 低 → boost 停振; 唤醒由 _init_new_hw_pins 重建。
            #      • CTRL_3V3(GPIO13) 低 → 断 V3.3(485收发器/W5500, 深睡耗电大头); 唤醒由
            #        _init_new_hw_pins 第一时间驱高恢复 (★ 必须, 否则就是当年 V3.3 锁死)。
            #    ⚠风险: ESP32-S3 RTC 脚 hold 唤醒若解不开 → V3.3/boost 卡关 = 读不到传感器、
            #    4G 拉不起来。台架可刷机恢复。验证点见下: 醒来读传感器 + 4G 上传是否正常。
            _holds = []
            _pen_keep = modem_pwr_idle_low()   # PEN 驱低 (幂等)
            if _pen_keep is not None:
                _holds.append(_pen_keep)
                log("[Deep Sleep] hold PEN(GPIO14) 低 → 4G 关")
            else:
                log("[Deep Sleep] ⚠PEN 占用失败, 不 hold (4G 灯可能仍亮)")
            if _boost_en_pin is not None:
                try:
                    _boost_en_pin.value = False
                    _holds.append(_boost_en_pin)
                    log("[Deep Sleep] hold BOOST_EN(GPIO39) 低 → boost 停")
                except Exception as e:
                    log(f"[Deep Sleep] BOOST_EN hold err: {e}")
            if _v33_pin is not None:
                try:
                    _v33_pin.value = False
                    _holds.append(_v33_pin)
                    log("[Deep Sleep] hold V3.3(GPIO13) 低 → 断 485/W5500 (省电大头, 实验)")
                except Exception as e:
                    log(f"[Deep Sleep] V3.3 hold err: {e}")

            # 6. 进入深度休眠 (设备会完全重启; 仅 hold 上面 _holds, 唤醒 _init_new_hw_pins 第一时间恢复)
            from drivers.power import PowerManager
            pwr = PowerManager()
            sleep_to_next = get_sleep_until_next_boundary(interval_sec)
            sleep_ms = sleep_to_next * 1000
            nxt = time.localtime(int(time.time()) + sleep_to_next)
            log(f"[Deep Sleep] 休眠 {sleep_to_next}s, 下次醒来 {nxt.tm_hour:02d}:{nxt.tm_min:02d}")
            log(f"[Deep Sleep] hold {len(_holds)} 脚, 醒来从 boot.py 重启")
            time.sleep(0.3)
            if _holds:
                pwr.deep_sleep(sleep_ms, preserve_dios=_holds)
            else:
                pwr.deep_sleep(sleep_ms)
            # ← 不会返回到这里，设备从 boot.py 重新启动
        
        # ============================================================
        # Light Sleep 模式: 轮询等待，保持 CDC/BLE 响应
        # ============================================================
        led.set_mode("idle")
        
        sleep_to_next = get_sleep_until_next_boundary(interval_sec)
        nxt = time.localtime(int(time.time()) + sleep_to_next)
        log(f"[Sleep] 休眠 {sleep_to_next}s, 下次 {nxt.tm_hour:02d}:{nxt.tm_min:02d} (send #read 可立即采集)...")
        sleep_start = time.monotonic()
        while (time.monotonic() - sleep_start) < sleep_to_next:
            cdc_result = process_commands(rs485_drivers, rs485_protocols, config)
            if cdc_result == "reload_config":
                interval_preset = config.get("system.interval_preset", 5)
                interval_sec = get_interval_seconds(interval_preset, config.get("system.interval_custom_min", 60))
                log(f"[cfg update] interval updated to: {interval_sec}s")
            elif cdc_result:
                log("[INTSleep] 收到 #read cmd")
                break
            # BLE cmdprocess (始终轮询)
            if ble:
                ble_result = process_ble_command(ble, config, rs485_drivers, rs485_protocols)
                if ble_result == True:
                    log("[INTSleep] 收到 BLE read_sensors cmd")
                    break
                elif ble_result == "reload_config":
                    # 重newloadintervalCfg
                    interval_preset = config.get("system.interval_preset", 5)
                    interval_sec = get_interval_seconds(interval_preset, config.get("system.interval_custom_min", 60))
                    log(f"[cfg update] interval updated to: {interval_sec}s")
                if ble.is_connected():
                    time.sleep(0.3)  # BLE conn时fast速轮询
                else:
                    time.sleep(1.0)  # normal每scheck一times
            else:
                time.sleep(1.0)

# ============================================================
# Entry
# ============================================================

log("Starting main()...")
try:
    main()
except Exception as e:
    log(f"[FATAL] {type(e).__name__}: {e}")
    # OTA 新固件首启抛普通异常 (典型: 漏推依赖的 ImportError) 不会进 safemode —
    # 在这里兜底回滚旧版本, 否则坏固件留在现场无人能救
    try:
        from app import ota_nvm as _onvm
        if _onvm.is_first_boot_after_ota():
            log("[FATAL] OTA 新固件首启异常 → 回滚旧版本")
            from app.ota_updater import rollback_from_backup, restore_usb_mode_after_ota
            _n = rollback_from_backup()
            log(f"[FATAL] 已回滚 {_n} 个文件, 恢复 USB 模式后重启")
            restore_usb_mode_after_ota()   # 模式不同会自行 reset 不返回
            import microcontroller as _mc
            time.sleep(1)
            _mc.reset()
    except Exception as _oe:
        log(f"[FATAL] OTA 回滚失败: {_oe}")
    # CircuitPython 的 traceback object 只有 tb_lineno / tb_next, 没有 tb_frame
    _tb_lines = [f"[FATAL] {type(e).__name__}: {e}"]
    try:
        _tb = e.__traceback__
        while _tb is not None:
            _tb_lines.append(f"  -> code.py:{_tb.tb_lineno}")
            _tb = _tb.tb_next
    except Exception as _e2:
        _tb_lines.append(f"  (tb walk err: {_e2})")
    # 备用: traceback.format_exception (CP 10.x 应有)
    try:
        import traceback as _trbk
        for _ln in _trbk.format_exception(e):
            for _sub in _ln.rstrip().split("\n"):
                _tb_lines.append(f"  | {_sub}")
    except Exception as _e3:
        _tb_lines.append(f"  (traceback module fail: {_e3})")
    # 输出到 CDC
    for _ln in _tb_lines:
        log(_ln)
    # 尝试写盘 (daily mode 才能成功)
    try:
        with open("/traceback.txt", "w") as _ftb:
            _ftb.write("\n".join(_tb_lines) + "\n")
        log("[FATAL] traceback dumped to /traceback.txt")
    except Exception as _e4:
        log(f"[FATAL] dump skipped: {_e4}")
    # 在 daily mode 切回 flash 让 host 救; 已在 flash mode 就不重启避免 boot loop
    try:
        import microcontroller
        if microcontroller.nvm[0] != 0:
            microcontroller.nvm[0] = 0
            log("[FATAL] 切回 flash mode, 5s 后 reset")
            time.sleep(5)
            microcontroller.reset()
    except Exception as _e5:
        log(f"[FATAL] mode switch err: {_e5}")
    # 死循环里仍响应 #reboot / #clear_safe / #help
    # 野外无人值守保护: 10 分钟无 USB 主机连接 → 断电外设 + 深睡 10 分钟再重启重试,
    # 不然 FATAL 全速轮询 + boost/V3.3 全开会把电池烧干
    log("[FATAL] 进入恢复模式: #reboot 重启 / #clear_safe 清 nvm[14] / 任何输入会被回显")
    log("[FATAL] 10 分钟无 USB 连接将断电深睡后自动重试")
    _fatal_start = time.monotonic()
    while True:
        try:
            if usb_cdc.data and usb_cdc.data.in_waiting > 0:
                _fatal_start = time.monotonic()   # 有人在操作, 刷新计时
                _cmd = usb_cdc.data.readline().decode("utf-8", "ignore").strip()
                if _cmd == "#reboot":
                    log("[FATAL] reboot")
                    time.sleep(0.5)
                    import microcontroller
                    microcontroller.reset()
                elif _cmd == "#clear_safe":
                    import microcontroller
                    microcontroller.nvm[14] = 0xFF
                    log("[FATAL] nvm[14]=0xFF, send #reboot to retry")
                elif _cmd:
                    log(f"[FATAL] echo: {_cmd}  (仅 #reboot / #clear_safe 有效)")
            _usb_alive = False
            try:
                _usb_alive = bool(usb_cdc.data and usb_cdc.data.connected)
            except Exception:
                pass
            if not _usb_alive and (time.monotonic() - _fatal_start) > 600:
                log("[FATAL] 无 USB, 断电外设 + 深睡 600s 后重试")
                try:
                    set_boost(False)
                except Exception:
                    pass
                try:
                    # FATAL 恢复路径保守起见不 hold 任何脚 (出错时各脚状态不确定); 普通深睡即可。
                    # (正常深睡路径已 hold PEN/V3.3/boost 省电, 见上方第 5 段。)
                    import alarm as _alarm
                    _ta = _alarm.time.TimeAlarm(monotonic_time=time.monotonic() + 600)
                    _alarm.exit_and_deep_sleep_until_alarms(_ta)
                except Exception as _se:
                    log(f"[FATAL] 深睡失败: {_se}, 继续恢复循环")
                    _fatal_start = time.monotonic()
        except Exception:
            pass
        time.sleep(0.2)
