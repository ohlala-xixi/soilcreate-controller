# ble_ota.py - BLE 现场固件升级 收帧状态机
#
# 由 code.py process_ble_command 收到 {"cmd":"ota_begin",...} 时调用 run_ble_ota()。
# 进入后独占 BLE 收帧循环, 直到会话结束 (中止/超时/断连) 或 _apply_update() reset (不返回)。
#
# 协议 (详见 docs/ble_ota_design.md §4):
#   控制帧: {...}\n         JSON 行 (双向)
#   数据帧: [0xA5][0x10][seq u16 LE][len u16 LE][payload]   (手机→板, Write-Without-Response)
#   同一条 NUS RX 双解析: 首字节 0xA5=数据帧, 0x7B('{')=控制行, 其它=噪声丢弃
#
# 复用 ota_updater 核心 (下载完成后的所有逻辑一行不改):
#   _cleanup_dir / _makedirs / _ensure_dir / _file_sha256 / _apply_update

import time
import json
import hashlib
import microcontroller

from app import ota_nvm
from app import ota_updater

_OTA_NEW_DIR = "/_ota_new"
_FRAME_SYNC = 0xA5
_FRAME_TYPE_DATA = 0x10
_DEFAULT_CHUNK = 244
_DEFAULT_WINDOW = 4
_IDLE_TIMEOUT_S = 30.0          # 无帧超时 → 清场退出
_DAILY_MODE = 17                # nvm[0]==17 = 设备可写


def _u16le(b, off):
    return b[off] | (b[off + 1] << 8)


def _hexdigest(h):
    # CircuitPython hashlib Hash 只有 digest(), 没有 hexdigest() — 手动转十六进制
    return "".join("%02x" % b for b in h.digest())


def run_ble_ota(ble, config, begin_cmd, log=print):
    """进入 BLE OTA 接收模式。begin_cmd = 已解析的 ota_begin dict。
    正常返回 = 会话结束/中止 (调用方回正常 BLE 循环); 不返回 = 已 _apply_update reset。
    """
    try:
        _BleOta(ble, config, log).run(begin_cmd)
    except Exception as e:
        log("[BLE-OTA] 致命错误: %s" % e)
        try:
            ble.send(json.dumps({"cmd": "ota_error", "msg": str(e)[:80]}) + "\n")
        except Exception:
            pass
        _cleanup()
        _restore_mode_if_pending(log)


def _cleanup():
    try:
        ota_updater._cleanup_dir(_OTA_NEW_DIR)
    except Exception:
        pass


def _restore_mode_if_pending(log=print):
    """OTA 中途放弃 (abort/超时/断连/异常) 时, 若之前为了 OTA 从 flash 切到了 daily,
    必须把模式切回去 — 否则 nvm[11/12] 永远挂着, 板子永久卡 daily (host 无法写盘).
    restore_usb_mode_after_ota 内部: 模式不同则切回 + reset (不返回), 相同则清状态返回."""
    try:
        if ota_nvm.is_ota_resume() or ota_nvm.get_ota_orig_mode() is not None:
            log("[BLE-OTA] 会话中止, 恢复 OTA 前 USB 模式")
            ota_updater.restore_usb_mode_after_ota()
    except Exception as e:
        log("[BLE-OTA] 模式恢复失败: %s" % e)


class _BleOta:
    def __init__(self, ble, config, log):
        self.ble = ble
        self.config = config
        self.log = log
        self.rxbuf = bytearray()
        self.chunk = _DEFAULT_CHUNK
        self.window = _DEFAULT_WINDOW
        self.version = "?"
        self.files = []             # manifest: [{"path","size","sha256"}]
        # per-file 收帧状态
        self.fidx = -1
        self.fh = None
        self.sha = None
        self.expect_seq = 0
        self.recv_bytes = 0
        self.win_count = 0
        self.last_rx = time.monotonic()

    def _ctl(self, obj):
        try:
            self.ble.send(json.dumps(obj) + "\n")
        except Exception as e:
            self.log("[BLE-OTA] ctl send err: %s" % e)

    def _connected(self):
        try:
            return self.ble.is_connected()
        except Exception:
            return False

    def run(self, begin_cmd):
        # 1. 解析 manifest
        self.version = begin_cmd.get("ver", "?")
        self.chunk = int(begin_cmd.get("chunk", _DEFAULT_CHUNK))
        self.window = int(begin_cmd.get("window", _DEFAULT_WINDOW))
        self.files = begin_cmd.get("files", [])
        if not self.files:
            self._ctl({"cmd": "ota_error", "msg": "no files in manifest"})
            return
        self.log("[BLE-OTA] begin ver=%s files=%d chunk=%d window=%d" %
                 (self.version, len(self.files), self.chunk, self.window))

        # 2. daily 模式检查 — 必须 nvm[0]==17 才能写自身文件系统
        if microcontroller.nvm[0] != _DAILY_MODE:
            orig = microcontroller.nvm[0]
            self.log("[BLE-OTA] 当前非 daily (nvm0=%d), 切 daily 并重启" % orig)
            ota_nvm.set_ota_resume(orig)        # 存原始模式 nvm[11] + resume 标记 (OTA 后切回)
            microcontroller.nvm[0] = _DAILY_MODE
            self._ctl({"cmd": "ota_reboot", "reason": "to_daily"})
            time.sleep(0.6)
            microcontroller.reset()             # 不返回; 手机重连后重发 ota_begin
            return

        # 3. daily 就绪 → 清暂存区 → ota_ready
        ota_updater._cleanup_dir(_OTA_NEW_DIR)
        ota_updater._makedirs(_OTA_NEW_DIR)
        self._ctl({"cmd": "ota_ready"})
        self.last_rx = time.monotonic()

        # 4. 主收帧循环
        while True:
            if not self._connected():
                self.log("[BLE-OTA] 断连, 清场退出")
                self._abort()
                return
            if (time.monotonic() - self.last_rx) > _IDLE_TIMEOUT_S:
                self.log("[BLE-OTA] 超时无帧, 清场退出")
                self._ctl({"cmd": "ota_error", "msg": "timeout"})
                self._abort()
                return

            n = self.ble.in_waiting
            if n > 0:
                data = self.ble.read_raw(n)
                if data:
                    self.rxbuf.extend(data)
                    self.last_rx = time.monotonic()

            r = self._parse()
            if r == "end":
                self._commit()          # _apply_update reset, 不返回
                return
            if r == "abort":
                self._abort()
                return

            time.sleep(0.005)

    # ── 双解析: 消化 rxbuf, 返回 None / "end" / "abort" ──
    def _parse(self):
        # 注: CircuitPython bytearray 不支持 del slice, 用切片重新赋值消费 (切片返回 bytearray)
        while self.rxbuf:
            b0 = self.rxbuf[0]
            if b0 == _FRAME_SYNC:
                if len(self.rxbuf) < 6:
                    return None                 # 头不全, 等
                length = _u16le(self.rxbuf, 4)
                if len(self.rxbuf) < 6 + length:
                    return None                 # payload 不全, 等
                frame = bytes(self.rxbuf[:6 + length])
                self.rxbuf = self.rxbuf[6 + length:]
                self._on_data(frame)
            elif b0 == 0x7B:                     # '{'
                nl = bytes(self.rxbuf).find(b"\n")
                if nl < 0:
                    return None                 # 行不全, 等
                line = bytes(self.rxbuf[:nl])
                self.rxbuf = self.rxbuf[nl + 1:]
                r = self._on_ctl(line)
                if r:
                    return r
            else:
                self.rxbuf = self.rxbuf[1:]     # 噪声, 丢一字节重对齐
        return None

    def _on_data(self, frame):
        if self.fh is None:
            return
        if frame[1] != _FRAME_TYPE_DATA:
            return
        seq = _u16le(frame, 2)
        length = _u16le(frame, 4)
        payload = frame[6:6 + length]
        if seq != self.expect_seq:
            # 断档 → nak, 要求从 expect_seq 重发该窗
            self.win_count = 0
            self._ctl({"cmd": "ota_nak", "idx": self.fidx, "expect": self.expect_seq})
            return
        try:
            self.fh.write(payload)
            self.sha.update(payload)
        except Exception as e:
            self._ctl({"cmd": "ota_file_err", "idx": self.fidx, "msg": "write %s" % e})
            return
        self.expect_seq += 1
        self.recv_bytes += length
        self.win_count += 1
        if self.win_count >= self.window:
            self.win_count = 0
            self._ctl({"cmd": "ota_ack", "idx": self.fidx,
                       "seq": self.expect_seq - 1, "recv": self.recv_bytes})

    def _on_ctl(self, line):
        try:
            cmd = json.loads(line.decode("utf-8", "ignore"))
        except Exception:
            return None
        c = cmd.get("cmd", "")
        if c == "ota_file":
            self._open_file(cmd)
        elif c == "ota_file_end":
            self._verify_file()
        elif c == "ota_end":
            return "end"
        elif c == "ota_abort":
            self.log("[BLE-OTA] 手机 abort")
            return "abort"
        return None

    def _open_file(self, cmd):
        self._close_handle()
        self.fidx = int(cmd.get("idx", 0))
        self.expect_seq = 0
        self.recv_bytes = 0
        self.win_count = 0
        self.sha = hashlib.new("sha256")
        path = cmd.get("path", "")
        dst = _OTA_NEW_DIR + "/" + path
        try:
            ota_updater._ensure_dir(dst)
            self.fh = open(dst, "wb")
        except Exception as e:
            self.fh = None
            self._ctl({"cmd": "ota_file_err", "idx": self.fidx, "msg": "open %s" % e})
            return
        self._ctl({"cmd": "ota_file_ready", "idx": self.fidx})

    def _verify_file(self):
        self._close_handle()
        fi = self.files[self.fidx] if 0 <= self.fidx < len(self.files) else None
        if fi is None:
            self._ctl({"cmd": "ota_file_err", "idx": self.fidx, "msg": "no manifest entry"})
            return
        # 用收帧时累积的流式 sha (省去再读一遍文件); CircuitPython 无 hexdigest 故手动转
        got = _hexdigest(self.sha).lower()
        want = (fi.get("sha256", "") or "").lower()
        if want and got != want:
            self.log("[BLE-OTA] file %d sha mismatch got=%s want=%s" % (self.fidx, got[:12], want[:12]))
            self._ctl({"cmd": "ota_file_err", "idx": self.fidx, "msg": "sha mismatch"})
        else:
            self.log("[BLE-OTA] file %d ok (%dB)" % (self.fidx, self.recv_bytes))
            self._ctl({"cmd": "ota_file_ok", "idx": self.fidx})

    def _close_handle(self):
        if self.fh is not None:
            try:
                self.fh.close()
            except Exception:
                pass
            self.fh = None

    def _commit(self):
        self._close_handle()
        self._ctl({"cmd": "ota_commit"})
        self.log("[BLE-OTA] 全部完成 → _apply_update + reset")
        time.sleep(0.4)
        ota_updater._apply_update(self.config, self.version, self.files)  # reset, 不返回

    def _abort(self):
        self._close_handle()
        _cleanup()
        _restore_mode_if_pending(self.log)   # 模式不同会 reset 不返回
