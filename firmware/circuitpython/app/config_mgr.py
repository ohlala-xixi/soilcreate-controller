# config_mgr.py - NVM 配置管理器
# CircuitPython Ver — 所有配置存储在 microcontroller.nvm (20KB)

import json
import microcontroller
import struct


class ConfigManager:
    """NVM 配置管理器

    NVM 全局布局 (与 app/ota_nvm.py / safemode.py / code.py 共享, 改这里务必同步):
      nvm[0]      = USB 模式标志 (boot.py 独立管理)
      nvm[1:16]   = OTA / safemode / boot 阶段等系统 flag (详见 app/ota_nvm.py)
      nvm[16:32]  = 预留
      nvm[32:34]  = uint16 big-endian 配置长度头
      nvm[34:]    = 配置 JSON UTF-8 字符串
    ★ 配置区从 32 起, 避开前 32 字节的系统 flag 区。旧版从 10 起会被
      OTA(nvm[10/11/12]) / safemode(nvm[14]) / boot 阶段(nvm[15]) 写入污染,
      每次启动改坏 config JSON → 参数存不上, 已修复。
    NVM 为空时从 /config.json 兜底读取（固件烧录时附带的初始配置）。
    """

    NVM_OFFSET = 32   # 配置数据起始偏移 (0..31 留给系统 flag)
    HEADER_SIZE = 2   # uint16 big-endian 长度头
    NVM_MAX = len(microcontroller.nvm) - NVM_OFFSET - HEADER_SIZE

    def __init__(self):
        self.config = {}

        if self._nvm_has_valid_config():
            print("[ConfigMgr] loaded from NVM")
        else:
            print("[ConfigMgr] NVM empty, reading /config.json (点APP导入配置后会写入NVM)")
            self.config = self._load_from_file()
    
    def _nvm_has_valid_config(self) -> bool:
        """检查 NVM 是否有有效 JSON 配置，并加载"""
        try:
            o = self.NVM_OFFSET
            length = struct.unpack(">H", microcontroller.nvm[o:o+2])[0]
            if length == 0 or length > self.NVM_MAX:
                return False
            json_bytes = bytes(microcontroller.nvm[o+2:o+2 + length])
            data = json.loads(json_bytes.decode("utf-8"))
            if isinstance(data, dict) and "system" in data:
                self.config = data
                print(f"[ConfigMgr] NVM valid: {length} bytes")
                return True
            return False
        except Exception as e:
            print(f"[ConfigMgr] NVM check failed: {e}")
            return False
    
    def load(self) -> bool:
        """从 NVM 加载配置"""
        try:
            o = self.NVM_OFFSET
            length = struct.unpack(">H", microcontroller.nvm[o:o+2])[0]
            if length == 0 or length > self.NVM_MAX:
                print("[ConfigMgr] NVM data invalid")
                self.config = self._load_from_file()
                return False

            json_bytes = bytes(microcontroller.nvm[o+2:o+2 + length])
            self.config = json.loads(json_bytes.decode("utf-8"))
            print(f"[ConfigMgr] loaded from NVM: {length} bytes")
            return True
        except Exception as e:
            print(f"[ConfigMgr] NVM load failed: {e}")
            self.config = self._load_from_file()
            return False
    
    def save(self) -> bool:
        """将完整配置写入 NVM"""
        try:
            json_str = json.dumps(self.config)
            json_bytes = json_str.encode("utf-8")
            length = len(json_bytes)
            
            if length > self.NVM_MAX:
                print(f"[ConfigMgr] config too large: {length} > {self.NVM_MAX}")
                return False
            
            # 写长度头 + JSON 数据 (偏移 10)
            o = self.NVM_OFFSET
            microcontroller.nvm[o:o+2] = struct.pack(">H", length)
            microcontroller.nvm[o+2:o+2 + length] = json_bytes
            
            print(f"[ConfigMgr] saved to NVM: {length} bytes")
            return True
        except Exception as e:
            print(f"[ConfigMgr] NVM save failed: {e}")
            return False
    
    def get(self, key: str, default=None):
        """获取配置值，支持点分隔 key
        e.g.: get("network.mqtt_broker") -> config["network"]["mqtt_broker"]
        """
        value = self.config
        for part in key.split("."):
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                return default
        return value
    
    def set(self, key: str, value) -> bool:
        """设置配置值，支持点分隔 key"""
        parts = key.split(".")
        target = self.config
        for part in parts[:-1]:
            if part not in target:
                target[part] = {}
            target = target[part]
        target[parts[-1]] = value
        return True
    
    def get_all(self) -> dict:
        """获取完整配置"""
        return self.config.copy()
    
    def get_section(self, section: str) -> dict:
        """获取指定配置段"""
        if section in self.config:
            value = self.config[section]
            return value if isinstance(value, dict) else {"value": value}
        return {}
    
    def set_all(self, new_config: dict) -> bool:
        """替换完整配置并保存"""
        self.config = new_config
        return self.save()
    
    def merge(self, partial: dict) -> bool:
        """递归合并部分配置并保存"""
        self._recursive_merge(self.config, partial)
        return self.save()
    
    def _recursive_merge(self, base: dict, update: dict):
        for key, value in update.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._recursive_merge(base[key], value)
            else:
                base[key] = value
    
    def import_address_list(self, filepath: str = "/address_list.csv") -> dict:
        """从 address_list.csv 导入传感器地址列表到配置
        
        CSV 格式 (统一 3 列):
            com,baud,protocol
            1,9600,PRIVATE_V2026
            26130201,,
            26130202,,
            2,9600,PRIVATE_V2026
            26130301,,
        
        - 3 列都有值 = COM 口声明
        - 第 2/3 列为空 = 传感器地址
        
        Returns:
            {"com1": count1, "com2": count2, ...} 或 {"error": "..."}
        """
        try:
            with open(filepath, "r") as f:
                lines = f.readlines()
        except Exception as e:
            return {"error": f"file read failed: {e}"}
        
        com_data = {}
        cur_com = None
        cur_baud = 9600
        cur_protocol = "PRIVATE_V2026"
        
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or line.lower().startswith("com,"):
                continue
            
            parts = [p.strip() for p in line.split(",")]
            
            # 补齐到 3 列
            while len(parts) < 3:
                parts.append("")
            
            if parts[1] and parts[2]:
                # COM 口声明: com,baud,protocol (3 列都有值)
                try:
                    cur_com = int(parts[0])
                    cur_baud = int(parts[1])
                    cur_protocol = parts[2]
                    if cur_com not in com_data:
                        com_data[cur_com] = {"baud": cur_baud, "protocol": cur_protocol, "sensors": []}
                except (ValueError, IndexError):
                    continue
            else:
                # 地址行: addr,, (第 2/3 列为空)
                if cur_com is None:
                    continue
                try:
                    addr = int(parts[0])
                    com_data[cur_com]["sensors"].append({"addr": addr})
                except ValueError:
                    continue
        
        # 先清空所有 COM 口的 sensors (避免残留旧地址)
        for key in list(self.config.keys()):
            if key.startswith("rs485_") and isinstance(self.config[key], dict):
                self.config[key]["sensors"] = []
        
        # 写入新地址
        result = {}
        for com, data in com_data.items():
            self._save_com_config(com, data["baud"], data["protocol"], data["sensors"])
            result[f"com{com}"] = len(data["sensors"])
        
        if result or com_data == {}:
            self.save()
            print(f"[ConfigMgr] address_list imported: {result}")
        
        return result
    
    def _save_com_config(self, com: int, baud: int, protocol: str, sensors: list):
        """更新指定 COM 口的配置"""
        key = f"rs485_{com}"
        if key not in self.config:
            self.config[key] = {}
        self.config[key]["baud"] = baud
        self.config[key]["protocol"] = protocol
        self.config[key]["sensors"] = sensors
        self.config[key]["enabled"] = True
    
    def _load_from_file(self) -> dict:
        """NVM 空时兜底: 从 /config.json 读取 (固件烧录附带的初始配置)"""
        try:
            with open("/config.json", "r") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                print(f"[ConfigMgr] /config.json loaded: {len(cfg)} top-level keys")
                return cfg
            print("[ConfigMgr] /config.json 不是 JSON 对象")
        except Exception as e:
            print(f"[ConfigMgr] /config.json read failed: {e}")
        return {}
