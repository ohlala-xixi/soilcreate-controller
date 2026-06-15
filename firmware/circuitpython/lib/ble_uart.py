# ble_uart.py - BLE UART NUS 服务
# uses adafruit_ble high级lib实现

try:
    from adafruit_ble import BLERadio
    from adafruit_ble.advertising.standard import ProvideServicesAdvertisement
    from adafruit_ble.services.nordic import UARTService
    from adafruit_ble.characteristics.stream import StreamIn
    from adafruit_ble.uuid import VendorUUID
    _HAS_ADAFRUIT_BLE = True

    class _OtaUARTService(UARTService):
        # 覆盖 RX buffer 512→2048。OTA 一窗 = 4 帧 × (244 payload + 6 帧头) = 1000B,
        # 之前 1024 只剩 24B 余量, 板子写 flash 暂停时下一窗易溢出截断 → 解析卡死。
        # 2048 能整窗缓冲, 扛得住 flash 擦除暂停。(__init__ 不收 buffer_size, 用子类覆盖特征)
        _server_rx = StreamIn(
            uuid=VendorUUID("6E400002-B5A3-F393-E0A9-E50E24DCCA9E"),
            timeout=1.0,
            buffer_size=2048,
        )
except ImportError:
    _HAS_ADAFRUIT_BLE = False
    print("[BLE] WARN: adafruit_ble library missing")


class BLEUART:
    """
    uses adafruit_ble 实现的 Nordic UART Service (NUS)
    """
    
    def __init__(self, name: str = "ESP32_Ctrl"):
        self._name = name
        self._connected = False
        self._ble = None
        self._uart = None
        self._advertisement = None
        self._initialized = False
        self._poll_count = 0
        self._rx_buffer = ""
        
        if not _HAS_ADAFRUIT_BLE:
            print("[BLE] init FAIL: missing adafruit_ble lib")
            return
        
        try:
            self._ble = BLERadio()
            self._ble.name = name
            # 用子类把 RX buffer 调到 1024 (BLE OTA 窗口数据帧需要), TX 沿用默认 512
            self._uart = _OtaUARTService()
            self._advertisement = ProvideServicesAdvertisement(self._uart)
            self._initialized = True
            print(f"[BLE] init done: {name}")
        except Exception as e:
            print(f"[BLE] init FAIL: {e}")
    
    def start_advertising(self):
        """advertising"""
        if not self._initialized or not self._ble:
            print("[BLE] not init，cannot advertise")
            return
        
        try:
            if not self._ble.advertising:
                self._ble.start_advertising(self._advertisement)
                print(f"[BLE] advertising: {self._name}")
        except Exception as e:
            print(f"[BLE] advertise failed: {e}")
    
    def poll(self) -> str:
        """
        轮询recvdata (notblocking)
        """
        if not self._initialized:
            return None
        
        self._poll_count += 1
        
        try:
            is_connected = self._ble.connected
            
            if is_connected:
                if not self._connected:
                    self._connected = True
                    print("[BLE] Connected!")
                
                # checkdata
                waiting = self._uart.in_waiting
                if waiting > 0:
                    # uses read 而notis readline
                    data = self._uart.read(waiting)
                    if data:
                        raw = data.decode("utf-8", errors="ignore")
                        self._rx_buffer += raw
                
                # Check buffer for complete line
                if "\n" in self._rx_buffer:
                    lines = self._rx_buffer.split("\n")
                    self._rx_buffer = lines[-1]  # 保留notdonepartial
                    for line in lines[:-1]:
                        line = line.strip()
                        if line:
                            print(f"[BLE RX] {line[:80]}{'...' if len(line) > 80 else ''}")
                            return line
            else:
                if self._connected:
                    self._connected = False
                    print("[BLE] Disconnected")
                    self.start_advertising()
        except Exception as e:
            print(f"[BLE] poll error: {e}")
        
        return None
    
    def send(self, data: str):
        """senddata（分片sendlargedata）"""
        if not self._initialized or not self._ble.connected:
            print(f"[BLE TX] skip (not connected)")
            return
        
        try:
            encoded = data.encode("utf-8")
            print(f"[BLE TX] send {len(encoded)} byte")
            
            # 分片send，每片 20 byte
            chunk_size = 20
            for i in range(0, len(encoded), chunk_size):
                chunk = encoded[i:i+chunk_size]
                self._uart.write(chunk)
                if i + chunk_size < len(encoded):
                    import time
                    time.sleep(0.02)  # 20ms delay
            
            print(f"[BLE TX] done")
        except Exception as e:
            print(f"[BLE] send failed: {e}")
    
    # ── 原始字节接口 (BLE OTA 收帧用, 绕过 poll() 的行缓冲) ──────────
    @property
    def in_waiting(self) -> int:
        """NUS RX 环形缓冲里待读字节数"""
        if not self._initialized:
            return 0
        try:
            return self._uart.in_waiting
        except Exception:
            return 0

    def read_raw(self, n: int):
        """读最多 n 个原始字节, 返回 bytes (无则 None)。OTA 二进制分帧用。"""
        if not self._initialized or n <= 0:
            return None
        try:
            return self._uart.read(n)
        except Exception:
            return None

    def send_raw(self, data_bytes):
        """发原始字节 (不分片不延时, 给 OTA 控制帧/ack 用; data CDC/BLE 链路自处理)"""
        if not self._initialized or not self._ble.connected:
            return
        try:
            self._uart.write(data_bytes)
        except Exception as e:
            print(f"[BLE] send_raw failed: {e}")

    def is_connected(self) -> bool:
        """checkconnectionstatus"""
        if not self._initialized or not self._ble:
            return False
        return self._ble.connected
    
    def disconnect(self):
        """断开所有连接"""
        if not self._initialized or not self._ble:
            return
        try:
            for connection in self._ble.connections:
                connection.disconnect()
        except:
            pass
        self._connected = False
    
    def stop_advertising(self):
        """停止 BLE 广播"""
        if not self._initialized or not self._ble:
            return
        try:
            self._ble.stop_advertising()
            print("[BLE] advertising stopped")
        except Exception as e:
            print(f"[BLE] stop_advertising failed: {e}")
