# data_formatter.py - dataformat化器
# CircuitPython Ver

import json
import time

class DataFormatter:
    """willsensordataformat化as分seg JSON"""
    
    def __init__(self, config, counter, firmware_version="0.0"):
        self.config = config
        self.counter = counter
        self.firmware_version = firmware_version
    
    def _format_clock(self, timestamp: int) -> str:
        """format化timeas M/DD/YYYY HH:MM:SS"""
        try:
            t = time.localtime(timestamp)
            return f"{t.tm_mon}/{t.tm_mday}/{t.tm_year} {t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d}"
        except:
            return ""
    
    def _get_model_for_address(self, address: int) -> int:
        """fromCfgcenter查找addressyes应的model (配置里 key 是 "addr")"""
        for ch in [1, 2]:
            sensors = self.config.get(f"rs485_{ch}.sensors", [])
            for s in sensors:
                if s.get("addr") == address:
                    return s.get("model", 0)
        return 0  # default三axis
    
    def format_segments(self, sensor_data: list, voltages: dict, signal: str = "", scheduled_time: int = 0, tz_offset_s: int = 0) -> list:
        """
        willsensordataformat化as分seg JSON
        
        Args:
            sensor_data: sensordatalist [{address, a, b, z, status, temp, model, channel}, ...]
            voltages: Voltdata {vin, V5V, V4G, V1, V2}
            signal: Signal strength str (如 "CSQ:31,99")
        
        Returns:
            JSON string list [header_json, segment1_json, ...]
        """
        device_id = self.config.get("system.id", "2026750001")
        try:
            cid_num = int(device_id)
        except (TypeError, ValueError):
            cid_num = 0   # 非数字 id 不能让整个采集周期 FATAL, 用 0 兜底并靠服务器侧发现
        max_per_seg = self.config.get("system.max_sensors_per_seg", 15)
        interval_preset = self.config.get("system.interval_preset", 5)
        
        # 输入数据为顶→底顺序 (主循环按 config.json 正序读取)
        # 上传协议要求底→顶 (seg 1/n data[0] = 底部传感器)，所以反转
        sorted_data = list(reversed(sensor_data))
        
        # 计算分seg数
        total_sensors = len(sorted_data)
        num_segments = (total_sensors + max_per_seg - 1) // max_per_seg if total_sensors > 0 else 0
        
        # getUploadserial number (sdt)
        sdt = self.counter.get_next()
        
        # 使用准点时间 (如果提供了) — 这是设备 RTC 的当地时间 epoch
        current_time = scheduled_time if scheduled_time > 0 else int(time.time())
        # time 字段上传**真 UTC** (= 当地 - 时区偏移), 全球通用, 服务器直接存 UTC。
        # clock 字段仍用当地时间 (人看的, 北京设备显示北京)。
        time_utc = current_time - tz_offset_s
        
        # 统计各CHsensor数
        ch1_count = len([s for s in sorted_data if s.get("channel") == 1])
        ch2_count = len([s for s in sorted_data if s.get("channel") == 2])
        
        # 统计Verrorange
        versions = [s.get("version", 0) for s in sorted_data if s.get("version")]
        vmin = round(min(versions), 2) if versions else 0
        vmax = round(max(versions), 2) if versions else 0
        
        segments = []
        
        # 手动构建 header JSON 确保 key 顺序匹配协议规范
        # V1 / V2 沿用协议字段名, 数据源是 v485_4 (COM1 VCC4) / v485_3 (COM2 VCC3)
        # 注: time 字段是设备本地时区 (UTC+8) 的"伪 epoch" (NTP/GSM 对时都带 +8),
        #     服务器 mqtt_bridge 入库时按 DEVICE_TZ_OFFSET 换算回 UTC — 改这里务必同步
        clock_str = self._format_clock(current_time)
        header_str = (
            f'{{"cid":{cid_num},'
            f'"v":"{self.firmware_version}",'
            f'"sdt":"{sdt}",'
            f'"V4G":{int(voltages.get("V4G", 0) * 100)},'
            f'"vin":{round(voltages.get("vin", 0), 2)},'
            f'"V5V":{int(voltages.get("V5V", 0) * 100)},'
            f'"V1":{round(voltages.get("v485_4", 0), 2)},'
            f'"V2":{round(voltages.get("v485_3", 0), 2)},'
            f'"current":{int(voltages.get("current_ma", 0))},'
            f'"clock":"{clock_str}",'
            f'"time":{time_utc},'
            f'"hib":{interval_preset},'
            f'"signal":"{signal}",'
            f'"vmin":{vmin},'
            f'"vmax":{vmax},'
            f'"sid1num":{ch1_count},'
            f'"sid2num":{ch2_count},'
            f'"seg":"0/{num_segments}"}}'
        )
        segments.append(header_str)
        
        # dataseg (seg: 1/n ~ n/n)
        # format: [address, Aaxis, Baxis, status, temp*10, model, Zaxis]
        # Note: tempandmodelfrom sensor_mgr cacheorCfgget (A3 responsenotpacket含)
        for seg_idx in range(num_segments):
            start = seg_idx * max_per_seg
            end = min(start + max_per_seg, total_sensors)
            seg_data = sorted_data[start:end]
            
            # 转换asarrayformat
            # format: [address, Aaxis, Baxis, status, 1, Zaxis, channel]
            # 第 7 位 channel (COM1/COM2) 为后加: 两路接同地址传感器时服务器侧
            # 必须靠它区分; 老服务器只读前 6 位, 向后兼容
            data_arrays = []
            for s in seg_data:
                data_arrays.append([
                    s.get("address", s.get("addr", 0)),
                    round(s.get("a", 0), 2),
                    round(s.get("b", 0), 2),
                    s.get("status", "C"),
                    1,  # Fixed value
                    round(s.get("z", 0), 2),
                    s.get("channel", 0)
                ])

            # 手动构建 JSON 确保 key 顺序: cid, time, seg, data
            data_json = json.dumps(data_arrays)
            segment_str = f'{{"cid":{cid_num},"time":{time_utc},"seg":"{seg_idx + 1}/{num_segments}","data":{data_json}}}'
            segments.append(segment_str)
        
        return segments
    
    def format_single_response(self, data: dict) -> str:
        """format化单 sensorsresponse"""
        return json.dumps(data)
    
    def format_status_response(self, info: dict) -> str:
        """format化statusresponse"""
        return json.dumps(info)
