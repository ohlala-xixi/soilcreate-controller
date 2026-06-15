# modem_4g.py - YunDTU 4G module driver (飞思创 YunDTU AT 指令集)
# CircuitPython Ver
#
# YunDTU 工作原理:
# - 配置通过 AT 指令完成, AT+S 保存后永久生效
# - WKMOD=MQTT 时, DTU 自动管理 MQTT 连接
# - 数据模式下写串口 = 自动 publish 到配置好的 topic
# - +++ 进入 AT 模式, AT+ENTM 回到数据模式
#
# 本驱动策略:
# 1. 上电后进 AT 模式, 检查配置是否已正确
# 2. 如果配置已正确 → 直接 AT+ENTM 回数据模式 (秒级)
# 3. 如果配置不对 → 配置 + AT+S 保存重启 (首次或配置变更时)
# 4. 不使用 admin 超级命令 (MQTT 模式下会泄漏为数据)

import busio
import digitalio
import time
import json
import pins


def _extract_first_json_span(s: str):
    """从一段文本里抽出第一个完整的 JSON 对象 (大括号配平, 跳过前导噪声/AT 残留).

    返回 (obj, end_idx): end_idx = 该 JSON 之后的下一个字符位置, 供调用方消费缓冲.
    收不齐 (还在累积) 返回 (None, 0); 解析失败返回 (None, end_idx) 让调用方跳过坏段.
    """
    start = s.find("{")
    if start < 0:
        # 全是噪声, 整段可丢
        return (None, len(s))
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return (json.loads(s[start:i + 1]), i + 1)
                    except Exception:
                        return (None, i + 1)
    return (None, 0)


def _extract_first_json(s: str):
    """兼容旧接口: 只返回第一个 JSON 对象或 None"""
    obj, _ = _extract_first_json_span(s)
    return obj


class Modem4G:
    """YunDTU 4G module AT command driver (飞思创 AT 指令集)"""

    def __init__(self, config, log_func=print):
        self.config = config
        self.log = log_func
        self.apn = config.get("network.4g.apn", "CMNET")
        self.mqtt_broker = config.get("network.mqtt_broker", "")
        self.mqtt_port = config.get("network.mqtt_port", 1883)
        # client_id 用设备 ID 避免冲突
        device_id = config.get("system.id", "ESP32_Gateway")
        self.mqtt_client_id = config.get("network.mqtt_client_id", device_id)
        self.mqtt_username = config.get("network.mqtt_user", "")
        self.mqtt_password = config.get("network.mqtt_pass", "")
        self.mqtt_pub_topic = config.get("network.mqtt_topic", "")
        # 下行指令订阅 topic: 留空时按 cirpy-info/<cid> 自动算 (cid = 设备 id)
        self.mqtt_sub_topic = config.get("network.mqtt_sub_topic", "") or f"cirpy-info/{device_id}"

        # 初始化 UART
        # receiver_buffer_size 必须够大: 下行 retained 指令 (cirpy-info) 可达 1KB+,
        # 且 connect() 末尾有 sleep(5/15s) 不读 UART, DTU 这期间把整条消息推过来 —
        # 默认 64B 缓冲会溢出丢字节 → 地址表损坏 (实测 85 地址只剩 8 个还 mangled)。
        self.uart = busio.UART(
            pins.MODEM_TX, pins.MODEM_RX,
            baudrate=115200,
            timeout=0.1,
            receiver_buffer_size=4096,
        )

        # Power ctrl
        self.pwr_pin = digitalio.DigitalInOut(pins.MODEM_PWR)
        self.pwr_pin.direction = digitalio.Direction.OUTPUT
        self.pwr_pin.value = False

        self._connected = False
        self._in_at_mode = False
        self._cached_time = ""
        # 下行持久缓冲: 一个窗口里可能先后到达多条 JSON (retained 指令 + srv_ack),
        # 每次 read_command 只消费一条, 剩余留给下次调用, 不丢字节.
        self._rx_text = ""

    def power_on(self):
        self.pwr_pin.value = True
        time.sleep(6)  # YunDTU 上电后需要几秒启动
        self.log("[4G] power on, wait 6s")

    def power_off(self):
        self.pwr_pin.value = False
        self._connected = False
        self._in_at_mode = False
        self.log("[4G] power off")

    # ── 底层 AT 收发 ─────────────────────────────────────────────

    def _drain_rx(self):
        """清空 UART 接收缓冲区"""
        if self.uart.in_waiting:
            self.uart.read(self.uart.in_waiting)

    def send_at(self, command, timeout_ms=1000, expect="OK"):
        """发送 AT 命令并等待响应 (需在 AT 模式下)"""
        self._drain_rx()
        self.uart.write((command + "\r\n").encode())

        start = time.monotonic()
        response = ""

        while (time.monotonic() - start) < (timeout_ms / 1000.0):
            if self.uart.in_waiting:
                chunk = self.uart.read(self.uart.in_waiting)
                if chunk:
                    response += chunk.decode("utf-8", "ignore")
                    if expect in response:
                        return (True, response.strip().split("\n"))
                    if "ERROR" in response or "ERR:" in response:
                        return (False, response.strip().split("\n"))
            time.sleep(0.01)

        return (False, response.strip().split("\n") if response else [])

    # ── 模式切换 ───────────────────────────────────────────────

    def _exit_passthrough(self):
        """退出数据模式, 进入 AT 指令模式 (+++, 前后静默)"""
        self._drain_rx()
        time.sleep(1)
        self.uart.write(b"+++")
        time.sleep(1.5)
        self._drain_rx()

    def _enter_data_mode(self):
        """退出 AT 模式, 回到数据模式"""
        self.send_at("AT+ENTM", 1000)
        self._in_at_mode = False
        time.sleep(0.5)
        self.log("[4G] → data mode (AT+ENTM)")

    def ensure_at_mode(self):
        """确保模块处于 AT 指令模式
        Why: YunDTU 上电默认在数据透传模式, 如果先 send_at("AT") 验活,
        那条 "AT\r\n" 会被当 payload 透传到 MQTT broker, 收到端会看到一条 "AT"
        污染消息。所以直接先 +++ 切 AT 模式, 已在 AT 模式时 +++ 也无害。
        """
        self._exit_passthrough()
        for i in range(3):
            ok, resp = self.send_at("AT", 2000)
            if ok:
                self._in_at_mode = True
                return True
            time.sleep(0.5)

        self.log("[4G] 模块无响应")
        return False

    # ── 配置检查 ───────────────────────────────────────────────

    def _get_at_value(self, cmd, prefix):
        """发送 AT 查询命令, 提取 +PREFIX:VALUE 中的 VALUE

        用 split(":", 1) 只切第一个冒号: CCLK 的值 "26/05/29,17:55:56+32"
        本身含冒号, split(":")[1] 会在 17 处截断 → 时间解析失败.
        """
        ok, resp = self.send_at(cmd, 1000)
        for line in resp:
            if prefix in line:
                return line.split(":", 1)[1].strip()
        return ""

    def _check_config_matches(self):
        """检查 DTU 当前配置是否与 config.json 一致
        
        如果一致, 不需要重新配置 + AT+S 重启
        """
        ch = 1

        # 检查 WKMOD
        wkmod = self._get_at_value(f"AT+WKMOD{ch}", f"+WKMOD{ch}:")
        if wkmod != "MQTT":
            self.log(f"[4G] config mismatch: WKMOD={wkmod}, need MQTT")
            return False

        # 检查 MQTT 服务器
        if self.mqtt_broker:
            sv = self._get_at_value(f"AT+MQTTSV{ch}", f"+MQTTSV{ch}:")
            expected_sv = f"{self.mqtt_broker},{self.mqtt_port}"
            if expected_sv not in sv:
                self.log(f"[4G] config mismatch: MQTTSV={sv}")
                return False

        # 检查 pub topic
        if self.mqtt_pub_topic:
            pub = self._get_at_value(f"AT+MQTTPUB{ch}", f"+MQTTPUB{ch}:")
            if self.mqtt_pub_topic not in pub:
                self.log(f"[4G] config mismatch: PUB={pub}")
                return False

        # 检查 sub topic (下行指令订阅)
        # ★ 关键: 不查这条, 已配好 pub 的现役板永远不会因"缺订阅"触发重配 (AT+S 重启),
        #   就永远收不到 cirpy-info/<cid> 的下行指令. 缺则返回 False 强制重配.
        if self.mqtt_sub_topic:
            sub = self._get_at_value(f"AT+MQTTSUB{ch}", f"+MQTTSUB{ch}:")
            if self.mqtt_sub_topic not in sub:
                self.log(f"[4G] config mismatch: SUB={sub}, need {self.mqtt_sub_topic}")
                return False

        self.log("[4G] config OK, skip reconfigure")
        return True

    # ── 连接流程 ───────────────────────────────────────────────

    def _connect_light(self, wait_s=20):
        """干净连接 (实验证实可收 retained 且零 flash 磨损):

        PEN 冷启动 → 等 DTU 在透传模式自己连 MQTT + 订阅, **全程不 +++ / 不 AT+S / 不 AT+ENTM**。
        关键: `+++` 进 AT 模式会打断 DTU 开机后的自动连 MQTT+订阅, 导致 broker 不重投 retained;
        干净冷启动(只 PEN low→high + 等)则让它正常重订阅 → 既能 publish 又能收 cirpy-info 的 retained。
        前提: DTU 配置已保存 (WKMOD=MQTT/MQTTSV/MQTTPUB/MQTTSUB), 即之前 heavy 连接已 AT+S 过。
        """
        self.log("[4G] light connect: PEN 冷启动 (不 +++)")
        self.pwr_pin.value = False
        time.sleep(3)
        self.pwr_pin.value = True
        self.log(f"[4G] PEN high, 等 {wait_s}s 自连 MQTT + 订阅...")
        time.sleep(wait_s)
        self._connected = True
        self.log("[4G] light connect ready (透传已自连, 可 publish + 收 retained)")
        return True

    def connect(self, force_reconfigure=False, light=False):
        """连接网络

        light=True: 走 _connect_light — 干净 PEN 冷启动, 不 +++, 既上传又能收 retained, 零 flash。
                    日常采集周期用它 (前提 DTU 已配好)。
        否则 heavy 流程:
        1. 上电 → +++ 进 AT 模式
        2. 检查 SIM/信号/注网
        3. 检查 MQTT 配置是否已正确保存 (不对则配置 + AT+S 重启)
        4. AT+ENTM 回数据模式
        heavy 用于: 首次配 DTU / 对时(读 CCLK) / 配置变更。
        force_reconfigure=True: 即使配置已正确也强制 AT+S 重启 (heavy 内)。
        """
        if light:
            return self._connect_light()
        self.power_on()

        if not self.ensure_at_mode():
            return False

        self.send_at("AT+E=OFF", 1000)

        # 查询固件版本
        ver = self._get_at_value("AT+VER", "+VER:")
        if ver:
            self.log(f"[4G] firmware: {ver}")

        # SIM 卡
        iccid = self._get_at_value("AT+ICCID", "+ICCID:")
        if not iccid or "not inserted" in iccid.lower():
            self.log("[4G] SIM 卡未插入!")
            return False
        self.log(f"[4G] ICCID: {iccid}")

        # APN
        self.send_at(f"AT+APN={self.apn.upper()},,,0", 1000)

        # 信号
        csq = self._get_at_value("AT+CSQ", "+CSQ:")
        self.log(f"[4G] CSQ: {csq}")

        # 网络注册
        for i in range(30):
            creg = self._get_at_value("AT+CREG", "+CREG:")
            if creg == "1":
                self._connected = True
                self.log("[4G] 网络已注册")
                break
            if i % 5 == 0:
                self.log(f"[4G] 等待注册... {i}/30")
            time.sleep(1)

        if not self._connected:
            self.log("[4G] 网络注册失败")
            return False

        # ── 检查是否需要重新配置 ──
        if not force_reconfigure and self._check_config_matches():
            # 在 AT 模式下缓存网络时间 (避免数据模式下再 +++ 泄漏)
            self._cached_time = self._get_at_value("AT+CCLK", "+CCLK:").strip('"')
            if self._cached_time:
                self.log(f"[4G] time: {self._cached_time}")

            # 配置已正确, 直接回数据模式, DTU 自动连 MQTT
            self._enter_data_mode()
            self.log("[4G] waiting MQTT connect (config saved)...")
            time.sleep(5)
        else:
            # 首次配置或配置变更
            if self.mqtt_broker:
                self._configure_mqtt()

            self.log("[4G] AT+S saving & restarting...")
            self.send_at("AT+S", 5000)
            self._in_at_mode = False

            # 等待 DTU 重启 + 注网 + MQTT 连接
            self.log("[4G] waiting for DTU restart + MQTT connect...")
            time.sleep(15)

        self.log("[4G] ready to publish")
        return True

    def _configure_mqtt(self):
        """配置 YunDTU 的 MQTT 参数 (通道 1)"""
        ch = 1

        self.send_at(f"AT+WKMOD{ch}=MQTT", 1000)
        self.log(f"[4G] CH{ch} → MQTT")

        ok, _ = self.send_at(
            f"AT+MQTTSV{ch}={self.mqtt_broker},{self.mqtt_port}", 1000
        )
        self.log(f"[4G] server: {self.mqtt_broker}:{self.mqtt_port} → {'OK' if ok else 'FAIL'}")

        ok, _ = self.send_at(
            f"AT+MQTTCONN{ch}={self.mqtt_client_id},{self.mqtt_username},"
            f"{self.mqtt_password},60,1", 1000
        )
        self.log(f"[4G] conn params → {'OK' if ok else 'FAIL'}")

        if self.mqtt_pub_topic:
            ok, _ = self.send_at(
                f"AT+MQTTPUB{ch}={self.mqtt_pub_topic},0,0", 1000
            )
            self.log(f"[4G] pub: {self.mqtt_pub_topic} → {'OK' if ok else 'FAIL'}")

        if self.mqtt_sub_topic:
            # QoS1: broker 对离线设备保留投递; 配合 retained, 醒来订阅即收最新指令
            ok, _ = self.send_at(
                f"AT+MQTTSUB{ch}={self.mqtt_sub_topic},1", 1000
            )
            self.log(f"[4G] sub: {self.mqtt_sub_topic} → {'OK' if ok else 'FAIL'}")

    # ── 数据发送 ───────────────────────────────────────────────

    def publish(self, topic, message):
        """发送数据 — 直接写串口, DTU 自动 publish"""
        if not self._connected:
            return False

        self.uart.write(message.encode())
        time.sleep(0.3)
        return True

    # ── 下行指令接收 ───────────────────────────────────────────
    #
    # YunDTU 单通道透传: 订阅到的下行消息直接出现在 UART (无 topic 框),
    # 和我们 publish 写出去的数据共用这根串口. 数据模式下读 UART 即可收指令.

    def read_command(self, timeout_ms=3000):
        """数据模式下读一条下行指令 (cirpy-info/<cid> 的 retained JSON), 返回 dict 或 None.

        在 connect() 订阅后, broker 会把 topic 上的 retained 消息推过来落在 UART RX.
        通常 upload 完时它已在缓冲里, 这里再兜一个窗口等它到齐.
        """
        if not self._connected:
            return None
        # 注意: 不能"没字节就早退" — DTU 重连 MQTT、broker 推 retained 可能要好几秒
        # (尤其 config OK 快速路径), 早退会在消息到达前就返回 None。读满 timeout 为止。
        start = time.monotonic()
        while True:
            if self.uart.in_waiting:
                chunk = self.uart.read(self.uart.in_waiting)
                if chunk:
                    self._rx_text += chunk.decode("utf-8", "ignore")
            # 缓冲里已有完整 JSON 就直接出队 (含上次调用剩下的)
            while self._rx_text:
                obj, end = _extract_first_json_span(self._rx_text)
                if end == 0:
                    break  # 还在累积, 继续等
                self._rx_text = self._rx_text[end:]
                if obj is not None:
                    return obj
                # obj None 且 end>0 = 坏段已跳过, 继续找下一条
            if len(self._rx_text) > 8192:
                self._rx_text = self._rx_text[-4096:]  # 防止噪声撑爆内存
            if (time.monotonic() - start) >= (timeout_ms / 1000.0):
                return None
            time.sleep(0.02)

    def is_connected(self):
        return self._connected

    # (SimCom HTTP / OTA 下载已移除 — 改走 BLE 现场推送)

    # ── 查询 (返回 connect 时缓存的值, 不做 +++ 避免泄漏) ────

    def get_network_time(self):
        """返回 connect() 时缓存的网络时间"""
        return self._cached_time

    def get_signal(self):
        """信号查询 — 数据模式下不可用, 返回空"""
        return ""

    # ── 生命周期 ───────────────────────────────────────────────

    def deinit(self):
        """释放资源, 不修改 DTU 配置"""
        self.power_off()
        self.uart.deinit()
        self.pwr_pin.deinit()
