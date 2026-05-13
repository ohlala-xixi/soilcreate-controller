# voltage.py - ADC Volt/Current 监测
# CircuitPython Ver
# 基于原理图 SCH_Schematic1_2026-05-11

import analogio
import pins


class VoltageMonitor:
    """多通道 ADC 电压 + 电流监测"""

    VREF = 3.3

    # 分压比 (硬件决定): 实际信号 = ADC读数 * ratio
    # VIN_S:  VIN → R100(5.1M) + R99(5.1M) → R98(1M) → GND, 实测标定 5.0
    # V485_*: VCC → R(100k) → tap → R(10k) → GND, ratio = (100+10)/10 = 11
    DIVIDER_RATIO = {
        "vin":    5.0,
        "v485_4": 11.0,
        "v485_3": 11.0,
    }

    # ACS712-05B-T 参数 (05-11 新板新增)
    # ACS712 VIOUT (5V 域) → R30(20k) → tap → R29(30k) → GND
    # → MCU ADC 看到 VIOUT * 30/(20+30) = 0.6 * VIOUT
    ACS_DIVIDER = 0.6           # ADC 电压 / ACS712 输出电压
    ACS_ZERO_V  = 2.5           # ACS712 零点 (VCC/2 = 2.5V @ 5V 供电)
    ACS_SENS_V_PER_A = 0.185    # 灵敏度: 185 mV/A (±5A 量程)

    def __init__(self):
        self.adcs = {}
        self._init_adcs()

    def _init_adcs(self):
        """初始化 ADC 通道"""
        for name, pin in pins.ADC_CHANNELS.items():
            try:
                self.adcs[name] = analogio.AnalogIn(pin)
                print(f"[Voltage] ADC {name} init OK")
            except Exception as e:
                print(f"[Voltage] ADC {name} init FAIL: {e}")

    def _read_adc_v(self, channel: str) -> float:
        """读 ADC 通道的原始电压 (0~3.3V)"""
        adc = self.adcs.get(channel)
        if not adc:
            return 0.0
        try:
            return (adc.value / 65535.0) * self.VREF
        except Exception as e:
            print(f"[Voltage] read {channel} fail: {e}")
            return 0.0

    def read(self, channel: str) -> float:
        """读电压通道 (vin / v485_3 / v485_4), 返回实际信号电压 (V)"""
        v_adc = self._read_adc_v(channel)
        ratio = self.DIVIDER_RATIO.get(channel, 1.0)
        return round(v_adc * ratio, 2)

    def read_current(self) -> int:
        """读系统总电流 (mA, 整数). 注意: 调用前需先把 pins.CURCTR 拉高打开 ME6214 LDO,
        否则 ACS712 没电, 读出来是 0."""
        v_adc = self._read_adc_v("current")
        if v_adc <= 0:
            return 0
        v_acs = v_adc / self.ACS_DIVIDER
        i_a = (v_acs - self.ACS_ZERO_V) / self.ACS_SENS_V_PER_A
        return int(i_a * 1000)

    def read_all(self) -> dict:
        """读所有通道, 返回 dict (电压 + 电流)"""
        result = {
            "vin":    self.read("vin"),
            "v485_4": self.read("v485_4"),
            "v485_3": self.read("v485_3"),
        }
        if "current" in self.adcs:
            result["current_ma"] = self.read_current()
        return result

    def read_raw(self, channel: str) -> int:
        """读原始 ADC 值 (0~65535)"""
        adc = self.adcs.get(channel)
        if adc:
            return adc.value
        return 0

    def get_vin_status(self) -> str:
        """输入电压状态"""
        vin = self.read("vin")
        if vin < 10.0:
            return "LOW"
        elif vin > 28.0:
            return "HIGH"
        return "OK"

    def deinit(self):
        """Release resources"""
        for adc in self.adcs.values():
            try:
                adc.deinit()
            except:
                pass
