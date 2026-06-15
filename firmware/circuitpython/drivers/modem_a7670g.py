# modem_a7670g.py - SIMCom A7670G 4G module driver
# CircuitPython Ver
#
# 跟 YunDTU 不同, A7670G 不支持 +++/AT+ENTM 数据透传模式,
# 全程在 AT 模式下用 SIMCom 原生 CMQTT* 套件做 MQTT 收发:
#
#   connect 阶段 (~13 条 AT 主路径):
#     ATE0 / AT+CMEE=2 / AT+CPIN? / AT+CSQ
#     AT+CGDCONT=1,"IP","<apn>" / AT+CGATT=1
#     AT+CEREG? 轮询 → stat=1 或 5
#     AT+CGACT=1,1 / AT+NETOPEN
#     (清场) AT+CMQTTDISC/REL/STOP 静默
#     AT+CMQTTSTART / AT+CMQTTACCQ / AT+CMQTTCONNECT
#
#   publish 阶段 (三段式):
#     AT+CMQTTTOPIC=0,<len> → 等 ">" → raw write topic → 等 "OK"
#     AT+CMQTTPAYLOAD=0,<len> → 等 ">" → raw write payload → 等 "OK"
#     AT+CMQTTPUB=0,1,60 → 等 "+CMQTTPUB: 0,0"
#
#   下行订阅 (远程配置 cirpy-info/<cid> + srv_ack):
#     AT+CMQTTSUBTOPIC=0,<len>,1 → 等 ">" → raw write topic → 等 "OK"
#     AT+CMQTTSUB=0 → 等 "+CMQTTSUB: 0,0"
#     收到时模块主动推 +CMQTTRXSTART/TOPIC/PAYLOAD/END URC 帧, payload 为 JSON,
#     read_command 用大括号配平从 URC 流里抽出 JSON (与 YunDTU 透传同一出队语义)。
#
#   deinit 阶段:
#     AT+CMQTTDISC/REL/STOP/AT+NETCLOSE → power_off
#
# 对外接口契约与 drivers/modem_4g.py (YunDTU) 对齐 (connect/publish/read_command/
# get_network_time/get_signal/is_connected/deinit), code.py 业务无感切换。

import busio
import digitalio
import time
import json
import pins


def _extract_first_json_span(s):
    """从一段文本里抽出第一个完整的 JSON 对象 (大括号配平, 跳过 +CMQTTRX* 框架行/AT 残留).

    返回 (obj, end_idx): end_idx = 该 JSON 之后的下一个字符位置, 供调用方消费缓冲.
    收不齐 (还在累积) 返回 (None, 0); 解析失败返回 (None, end_idx) 让调用方跳过坏段.
    (与 modem_4g.py 同实现; 两驱动相互独立, 故各留一份不交叉 import。)
    """
    start = s.find("{")
    if start < 0:
        return (None, len(s))   # 全是噪声/URC 框架, 整段可丢
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


class ModemA7670G:
    """SIMCom A7670G AT command driver"""

    BAUD = 115200

    def __init__(self, config, log_func=print):
        self.config = config
        self.log = log_func
        self.apn = config.get("network.4g.apn", "cmnet")
        self.mqtt_broker = config.get("network.mqtt_broker", "")
        self.mqtt_port = config.get("network.mqtt_port", 1883)
        device_id = config.get("system.id", "ESP32_Gateway")
        self.mqtt_client_id = config.get("network.mqtt_client_id", str(device_id))
        self.mqtt_username = config.get("network.mqtt_user", "")
        self.mqtt_password = config.get("network.mqtt_pass", "")
        # 下行指令订阅 topic: 留空时按 cirpy-info/<cid> 自动算 (与 YunDTU 一致)
        self.mqtt_sub_topic = config.get("network.mqtt_sub_topic", "") or f"cirpy-info/{device_id}"

        # receiver_buffer_size 加大: 下行 retained 指令 (cirpy-info 地址表) 可达 1KB+,
        # URC 帧一次性推过来, 默认 64B 会溢出丢字节 → 地址表损坏 (同 YunDTU 教训)。
        self.uart = busio.UART(
            pins.MODEM_TX, pins.MODEM_RX,
            baudrate=self.BAUD,
            timeout=0.1,
            receiver_buffer_size=4096,
        )

        self.pwr_pin = digitalio.DigitalInOut(pins.MODEM_PWR)
        self.pwr_pin.direction = digitalio.Direction.OUTPUT
        self.pwr_pin.value = False

        self._connected = False
        self._net_opened = False
        self._pwr_on = False
        self._cached_csq = ""
        # 下行持久缓冲: 一个窗口里可能先后到达多条 JSON (retained 指令 + srv_ack),
        # 每次 read_command 只消费一条, 剩余留给下次调用, 不丢字节 (同 YunDTU)。
        self._rx_text = ""

    # ── 电源 ─────────────────────────────────────────────────────

    def power_on(self):
        self.pwr_pin.value = True
        self._pwr_on = True
        time.sleep(8)  # A7670G 启动到 AT 可响应大约需要 6~8s
        self.log("[A7670G] power on, wait 8s")

    def power_off(self):
        self.pwr_pin.value = False
        self._pwr_on = False
        self._connected = False
        self._net_opened = False
        self.log("[A7670G] power off")

    # ── 底层 AT 收发 ────────────────────────────────────────────

    def _drain_rx(self):
        if self.uart.in_waiting:
            self.uart.read(self.uart.in_waiting)

    def _wait_for(self, pattern, timeout_ms):
        """等指定模式出现, 同时识别 ERROR
        Returns: (ok: bool, response: str)
        """
        start = time.monotonic()
        response = ""
        while (time.monotonic() - start) < (timeout_ms / 1000.0):
            if self.uart.in_waiting:
                chunk = self.uart.read(self.uart.in_waiting)
                if chunk:
                    response += chunk.decode("utf-8", "ignore")
                    if pattern in response:
                        return (True, response)
                    if "ERROR" in response:
                        return (False, response)
            time.sleep(0.01)
        return (False, response)

    def _send_at(self, cmd, timeout_ms=1000, expect="OK"):
        """发 AT 命令并等待 expect, 失败返回 (False, response)"""
        self._drain_rx()
        self.uart.write((cmd + "\r\n").encode())
        return self._wait_for(expect, timeout_ms)

    def _send_at_silent(self, cmd, timeout_ms=1000):
        """发 AT, 任何结果都不报错 (用于清场)"""
        try:
            self._drain_rx()
            self.uart.write((cmd + "\r\n").encode())
            self._wait_for("OK", timeout_ms)
        except Exception:
            pass

    def _raw_write(self, data_bytes):
        self.uart.write(data_bytes)

    def _absorb_rx(self, max_ms=2000):
        """订阅后抢收 retained 下行: 把 URC 数据搬进 _rx_text (Python 侧缓冲),
        避免被随后 publish 里 _send_at 的 _drain_rx 清掉硬件缓冲时一起丢掉。
        收到 +CMQTTRXEND (一条完整消息到齐) 即提前返回; 没下行则等满 max_ms。
        """
        start = time.monotonic()
        while (time.monotonic() - start) < (max_ms / 1000.0):
            if self.uart.in_waiting:
                chunk = self.uart.read(self.uart.in_waiting)
                if chunk:
                    self._rx_text += chunk.decode("utf-8", "ignore")
                    if "+CMQTTRXEND" in self._rx_text:
                        return
            else:
                time.sleep(0.02)

    def _extract_value(self, response, prefix):
        """从响应里抽 +PREFIX: VALUE 中的 VALUE 部分"""
        for line in response.split("\n"):
            line = line.strip()
            if line.startswith(prefix):
                return line[len(prefix):].strip()
        return ""

    # ── connect 子步骤 ──────────────────────────────────────────

    def _verify_alive(self):
        """AT 验活, 最多 3 次"""
        for i in range(3):
            ok, _ = self._send_at("AT", timeout_ms=1000)
            if ok:
                return True
            time.sleep(1)
        self.log("[A7670G] AT 无响应")
        return False

    def _basic_config(self):
        """关回显 + 详细错误码"""
        self._send_at("ATE0", timeout_ms=1000)
        self._send_at("AT+CMEE=2", timeout_ms=1000)
        return True

    def _check_sim(self):
        ok, resp = self._send_at("AT+CPIN?", timeout_ms=2000, expect="+CPIN:")
        if not ok:
            self.log("[A7670G] SIM 检查失败")
            return False
        if "READY" not in resp:
            self.log(f"[A7670G] SIM 未就绪: {resp.strip()}")
            return False
        return True

    def _read_csq(self):
        ok, resp = self._send_at("AT+CSQ", timeout_ms=1000, expect="+CSQ:")
        if ok:
            val = self._extract_value(resp, "+CSQ:")
            self._cached_csq = val
            self.log(f"[A7670G] CSQ: {val}")
        return ok

    def _config_pdp(self):
        ok, _ = self._send_at(
            f'AT+CGDCONT=1,"IP","{self.apn}"', timeout_ms=2000
        )
        if not ok:
            self.log(f"[A7670G] CGDCONT APN={self.apn} 失败")
            return False
        self.log(f"[A7670G] APN: {self.apn}")

        ok, _ = self._send_at("AT+CGATT=1", timeout_ms=5000)
        if not ok:
            self.log("[A7670G] CGATT 失败")
            return False
        return True

    def _wait_registration(self, timeout_s=30):
        """轮询 AT+CEREG?, stat=1(本地) 或 5(漫游) 视为成功"""
        for i in range(timeout_s):
            ok, resp = self._send_at("AT+CEREG?", timeout_ms=1000, expect="+CEREG:")
            if ok:
                val = self._extract_value(resp, "+CEREG:")
                # format: <n>,<stat>[,...]
                parts = val.split(",")
                if len(parts) >= 2:
                    stat = parts[1].strip()
                    if stat in ("1", "5"):
                        self.log(f"[A7670G] 已注册 (stat={stat})")
                        return True
                    if stat == "3":
                        self.log("[A7670G] 注册被拒绝")
                        return False
            if i % 5 == 0:
                self.log(f"[A7670G] 等待 LTE 注册... {i}/{timeout_s}")
            time.sleep(1)
        self.log("[A7670G] 注册超时")
        return False

    def _activate_pdp(self):
        ok, _ = self._send_at("AT+CGACT=1,1", timeout_ms=10000)
        if not ok:
            self.log("[A7670G] CGACT 激活失败")
            return False

        ok, resp = self._send_at("AT+NETOPEN", timeout_ms=10000, expect="+NETOPEN:")
        if not ok:
            # errcode=4 (已经开过) 也算成功
            if "+NETOPEN: 4" in resp or "Network is already opened" in resp:
                self.log("[A7670G] NETOPEN 已开 (沿用)")
                self._net_opened = True
                return True
            self.log("[A7670G] NETOPEN 失败")
            return False

        val = self._extract_value(resp, "+NETOPEN:")
        if val == "0":
            self._net_opened = True
            self.log("[A7670G] NETOPEN OK")
            return True
        self.log(f"[A7670G] NETOPEN 异常: {val}")
        return False

    def _mqtt_cleanup(self):
        """清场: 防止上次 session 残留, ERROR 静默吞"""
        self._send_at_silent("AT+CMQTTDISC=0,60", timeout_ms=2000)
        self._send_at_silent("AT+CMQTTREL=0", timeout_ms=1000)
        self._send_at_silent("AT+CMQTTSTOP", timeout_ms=2000)

    def _mqtt_connect(self):
        ok, _ = self._send_at("AT+CMQTTSTART", timeout_ms=5000, expect="+CMQTTSTART: 0")
        if not ok:
            self.log("[A7670G] CMQTTSTART 失败")
            return False

        ok, _ = self._send_at(
            f'AT+CMQTTACCQ=0,"{self.mqtt_client_id}",0', timeout_ms=2000
        )
        if not ok:
            self.log(f"[A7670G] CMQTTACCQ 失败 (client_id={self.mqtt_client_id})")
            return False

        cmd = (
            f'AT+CMQTTCONNECT=0,"tcp://{self.mqtt_broker}:{self.mqtt_port}",'
            f'60,1,"{self.mqtt_username}","{self.mqtt_password}"'
        )
        ok, resp = self._send_at(cmd, timeout_ms=15000, expect="+CMQTTCONNECT: 0,0")
        if not ok:
            self.log(f"[A7670G] CMQTTCONNECT 失败: {resp.strip()}")
            return False

        self.log(f"[A7670G] MQTT 连上 {self.mqtt_broker}:{self.mqtt_port}")
        return True

    def _mqtt_subscribe(self):
        """订阅下行 topic (cirpy-info/<cid>): 收 retained 远程配置 + 服务器 srv_ack。
        best-effort — 失败只记日志, 不影响上传 (上传仍可用, 仅丢远程控制能力)。
        要在 publish 数据之前调: srv_ack 非 retained, 订阅晚了会错过。
        SIMCom 两段式: CMQTTSUBTOPIC=<idx>,<len>,<qos> → '>' → raw topic → CMQTTSUB=<idx>
        """
        if not self.mqtt_sub_topic:
            return
        topic_bytes = self.mqtt_sub_topic.encode("utf-8")

        ok, _ = self._send_at(
            f"AT+CMQTTSUBTOPIC=0,{len(topic_bytes)},1", timeout_ms=2000, expect=">"
        )
        if not ok:
            self.log("[A7670G] SUBTOPIC prompt 超时, 跳过订阅 (上传不受影响)")
            return
        self._raw_write(topic_bytes)
        ok, _ = self._wait_for("OK", timeout_ms=2000)
        if not ok:
            self.log("[A7670G] SUBTOPIC 写入未确认, 跳过订阅")
            return

        ok, resp = self._send_at("AT+CMQTTSUB=0", timeout_ms=5000, expect="+CMQTTSUB: 0,0")
        if not ok:
            self.log(f"[A7670G] CMQTTSUB 失败: {resp.strip()} (上传不受影响)")
            return
        self.log(f"[A7670G] subscribed: {self.mqtt_sub_topic}")
        # 抢收订阅后 broker 立即推的 retained cirpy-info, 防被后续 publish drain 掉
        self._absorb_rx(2000)

    # ── connect 主流程 ──────────────────────────────────────────

    def connect(self, force_reconfigure=False, light=False):
        """完整连接流程: 上电 → 基础配置 → SIM → 信号 → PDP → 注册 → 拨号 → MQTT

        force_reconfigure / light: 与 Modem4G 接口对齐。原生 AT 每次都全新建连+订阅,
        本就能收 retained, 故这两个参数无操作。
        """
        self.power_on()

        if not self._verify_alive():
            return False
        self._basic_config()

        if not self._check_sim():
            return False
        self._read_csq()  # 不强制, 弱信号也试

        if not self._config_pdp():
            return False
        if not self._wait_registration(timeout_s=30):
            return False
        if not self._activate_pdp():
            return False

        self._mqtt_cleanup()
        if not self.mqtt_broker:
            self.log("[A7670G] 未配置 broker, 跳过 MQTT")
            self._connected = False
            return True  # 网络通了, 但没 broker 可连

        if not self._mqtt_connect():
            return False

        self._connected = True
        # 订阅下行 (远程配置 + srv_ack) — 必须在 publish 之前, best-effort
        self._mqtt_subscribe()
        self.log("[A7670G] ready to publish")
        return True

    # ── 数据发送 ─────────────────────────────────────────────────

    def publish(self, topic, message):
        """发布: CMQTTTOPIC → raw topic → CMQTTPAYLOAD → raw payload → CMQTTPUB
        每次约 1.5s, 失败不重连
        """
        if not self._connected:
            return False

        if isinstance(topic, str):
            topic_bytes = topic.encode("utf-8")
        else:
            topic_bytes = topic
        if isinstance(message, str):
            msg_bytes = message.encode("utf-8")
        else:
            msg_bytes = message

        # ── stage A: topic ──
        ok, _ = self._send_at(
            f"AT+CMQTTTOPIC=0,{len(topic_bytes)}", timeout_ms=1000, expect=">"
        )
        if not ok:
            self.log("[A7670G] pub: TOPIC prompt 超时")
            return False
        self._raw_write(topic_bytes)
        ok, _ = self._wait_for("OK", timeout_ms=2000)
        if not ok:
            self.log("[A7670G] pub: TOPIC 写入未确认")
            return False

        # ── stage B: payload ──
        ok, _ = self._send_at(
            f"AT+CMQTTPAYLOAD=0,{len(msg_bytes)}", timeout_ms=1000, expect=">"
        )
        if not ok:
            self.log("[A7670G] pub: PAYLOAD prompt 超时")
            return False
        self._raw_write(msg_bytes)
        ok, _ = self._wait_for("OK", timeout_ms=2000)
        if not ok:
            self.log("[A7670G] pub: PAYLOAD 写入未确认")
            return False

        # ── stage C: publish (qos=1, expiry=60s) ──
        ok, resp = self._send_at(
            "AT+CMQTTPUB=0,1,60", timeout_ms=8000, expect="+CMQTTPUB: 0,0"
        )
        if not ok:
            self.log(f"[A7670G] pub: PUB 未收到 ack ({resp.strip()})")
            return False

        return True

    # ── 下行指令接收 ───────────────────────────────────────────
    #
    # 订阅 (connect 末尾 _mqtt_subscribe) 后, broker 把 cirpy-info/<cid> 的 retained
    # 指令和 srv_ack 以 +CMQTTRX* URC 帧推过来. payload 是 JSON, 框架行
    # (+CMQTTRXSTART/TOPIC/PAYLOAD/END) 不含 '{' 故被大括号配平自然跳过。

    def read_command(self, timeout_ms=3000):
        """读一条下行指令 (cirpy-info/<cid> retained JSON 或 srv_ack), 返回 dict 或 None.

        持久缓冲: 一次只出队一条 JSON, 剩余留给下次调用 (与 YunDTU read_command 契约一致)。
        app/remote_cmd.handle_remote 用 hasattr(modem,"read_command") 鉴别 — 有了它,
        A7670G 也能收远程配置 + srv_ack (上报真到服务器的证据)。
        """
        if not self._connected:
            return None
        start = time.monotonic()
        while True:
            if self.uart.in_waiting:
                chunk = self.uart.read(self.uart.in_waiting)
                if chunk:
                    self._rx_text += chunk.decode("utf-8", "ignore")
            # 缓冲里已有完整 JSON 就直接出队 (含订阅时 _absorb_rx 抢收的 retained)
            while self._rx_text:
                obj, end = _extract_first_json_span(self._rx_text)
                if end == 0:
                    break  # 还在累积, 继续等
                self._rx_text = self._rx_text[end:]
                if obj is not None:
                    return obj
                # obj None 且 end>0 = 坏段已跳过, 继续找下一条
            if len(self._rx_text) > 8192:
                self._rx_text = self._rx_text[-4096:]  # 防噪声撑爆内存
            if (time.monotonic() - start) >= (timeout_ms / 1000.0):
                return None
            time.sleep(0.02)

    # (HTTP GET / OTA 下载已移除 — 改走 BLE 现场推送)

    # ── 状态查询 ─────────────────────────────────────────────────

    def is_connected(self):
        return self._connected

    def get_network_time(self):
        """A7670G 通常拿不到 NITZ, 返回空字符串 (跟 YunDTU 兼容)"""
        return ""

    def get_signal(self):
        """返回最近一次缓存的 CSQ 值"""
        return self._cached_csq

    # ── 生命周期 ─────────────────────────────────────────────────

    def enter_psm(self):
        """A7670G PSM 暂未实现, fallback 到 deinit 断电"""
        self.log("[A7670G] PSM not implemented, falling back to deinit")
        self.deinit()
        return False

    def deinit(self):
        """优雅断开: MQTT 退出 → NETCLOSE → 断电"""
        try:
            if self._connected:
                self._send_at_silent("AT+CMQTTDISC=0,60", timeout_ms=2000)
                self._send_at_silent("AT+CMQTTREL=0", timeout_ms=1000)
                self._send_at_silent("AT+CMQTTSTOP", timeout_ms=2000)
            if self._net_opened:
                self._send_at_silent("AT+NETCLOSE", timeout_ms=3000)
        except Exception as e:
            self.log(f"[A7670G] deinit AT cleanup err: {e}")

        self.power_off()
        try:
            self.uart.deinit()
        except Exception:
            pass
        try:
            self.pwr_pin.deinit()
        except Exception:
            pass
