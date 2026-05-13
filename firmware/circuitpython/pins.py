# pins.py - ESP32-S3 柔性测斜仪控制器硬件引脚定义
# CircuitPython 版本
# 基于原理图: SCH_Schematic1_2026-05-11
# 模块: ESP32-S3-WROOM-1 N8R2
#
# ============================================================
# 新板 (05-11) 关键变化 vs 旧板 (03-13):
#   - RS485 从 4 路砍到 2 路 (旧 COM3/COM4 经 SC16IS752 的扩展通道整块移除)
#   - W5500 INT_L (GPIO3) ↔ RST_L (GPIO46) 互换
#   - V4G_CTRL 从 GPIO42 搬到 GPIO14
#   - 3V3CTL 从 GPIO47 搬到 GPIO13
#   - V485_4S 从 GPIO4 搬到 GPIO6 (让位给新增 CURRENT)
#   - RS485 COM1/COM2 的全部 10 个引脚位置都改了 (软件命名保持 COM1/COM2)
#   - 新增 ADC: CURRENT (GPIO4, ACS712 系统电流), V485_3S (GPIO7, COM2 反馈)
#   - 新增 GPIO: SPWALLON (GPIO39, 12V boost EN), CURCTR (GPIO40, ME6214 LDO EN)
#   - SC16IS752 (U1) 还焊在板上, 但 UART 输出悬空, 不再驱; I2C 总线随之取消
# ============================================================
#
# 通道映射:
#   原理图 CH4 (CN3, U11) → 软件 COM1 (硬件 UART, GPIO 直连)
#   原理图 CH3 (CN2, U10) → 软件 COM2 (硬件 UART, GPIO 直连)
#
#  模块Pin | GPIO  | 原理图信号      | 软件功能
# ---------|-------|-----------------|------------------
#     4    | GPIO4 | CURRENT         | ADC 系统电流 (ACS712)
#     5    | GPIO5 | VIN_S           | ADC 输入电压
#     6    | GPIO6 | V485_4S         | ADC COM1 VCC4 反馈
#     7    | GPIO7 | V485_3S         | ADC COM2 VCC3 反馈
#     8    | GPIO15| XTAL_32K_P      | RTC 晶振
#     9    | GPIO16| XTAL_32K_N      | RTC 晶振
#    13    | GPIO19| USB D-          | USB
#    14    | GPIO20| USB D+          | USB
#    15    | GPIO3 | RST_L           | W5500 复位 (旧版 INT_L)
#    16    | GPIO46| INT_L           | W5500 中断 (旧版 RST_L)
#    17    | GPIO9 | SPI_MOSI        | W5500 SPI
#    18    | GPIO10| SPI_MISO        | W5500 SPI
#    19    | GPIO11| SPI_CLK         | W5500 SPI
#    20    | GPIO12| SPI_CS          | W5500 SPI
#    21    | GPIO13| 3V3CTL          | 3.3V 电源控制
#    22    | GPIO14| V4G_CTRL        | 4G 电源控制
#    25    | GPIO48| SCAN3           | COM2 扫描
#    26    | GPIO45| 485CTR3         | COM2 DE/RE
#    27    | GPIO0 | BOOT            | 启动引脚
#    28    | GPIO35| VOUTCTR3        | COM2 电源
#    29    | GPIO36| SCAN4           | COM1 扫描
#    30    | GPIO37| VOUTCTR4        | COM1 电源
#    31    | GPIO38| 485CTR4         | COM1 DE/RE
#    32    | GPIO39| SPWALLON        | 12V boost 使能 (新增)
#    33    | GPIO40| CURCTR          | ME6214 current-sense LDO EN (新增)
#    34    | GPIO41| TX4             | COM1 TX
#    35    | GPIO42| RX4             | COM1 RX
#    36    | GPIO44| U_4G_TX(RXD0)   | 4G UART RX
#    37    | GPIO43| U_4G_RX(TXD0)   | 4G UART TX
#    38    | GPIO2 | TX3             | COM2 TX
#    39    | GPIO1 | RX3             | COM2 RX
#
# 释放出来 (新板不接外设): GPIO17, GPIO18, GPIO8, GPIO21, GPIO47
#

import microcontroller

# ============================================================
# 系统引脚
# ============================================================
XTAL_32K_P = microcontroller.pin.GPIO15   # RTC crystal P
XTAL_32K_N = microcontroller.pin.GPIO16   # RTC crystal N
USB_DN = microcontroller.pin.GPIO19       # USB D-
USB_DP = microcontroller.pin.GPIO20       # USB D+
CTRL_3V3 = microcontroller.pin.GPIO13     # 3.3V 电源总控 (旧版在 GPIO47)

# ============================================================
# ADC 电压/电流监测
# ============================================================
ADC_VIN     = microcontroller.pin.GPIO5   # 输入电压 (VIN_S) - ADC1_CH4
ADC_V485_4  = microcontroller.pin.GPIO6   # COM1 VCC4 反馈 (V485_4S) - ADC1_CH5
ADC_V485_3  = microcontroller.pin.GPIO7   # COM2 VCC3 反馈 (V485_3S) - ADC1_CH6
ADC_CURRENT = microcontroller.pin.GPIO4   # 系统电流 (ACS712 经 R30/R29 分压) - ADC1_CH3

# ============================================================
# RS485 COM1 (原理图 CH4, 硬件 UART)
# ============================================================
RS485_1_TX   = microcontroller.pin.GPIO41   # UART TX (TX4)
RS485_1_RX   = microcontroller.pin.GPIO42   # UART RX (RX4)
RS485_1_DE   = microcontroller.pin.GPIO38   # DE/RE (485CTR4)
RS485_1_VCC  = microcontroller.pin.GPIO37   # 电源 (VOUTCTR4)
RS485_1_SCAN = microcontroller.pin.GPIO36   # 扫描使能 (SCAN4)

# ============================================================
# RS485 COM2 (原理图 CH3, 硬件 UART)
# ============================================================
RS485_2_TX   = microcontroller.pin.GPIO2    # UART TX (TX3)
RS485_2_RX   = microcontroller.pin.GPIO1    # UART RX (RX3)
RS485_2_DE   = microcontroller.pin.GPIO45   # DE/RE (485CTR3)
RS485_2_VCC  = microcontroller.pin.GPIO35   # 电源 (VOUTCTR3)
RS485_2_SCAN = microcontroller.pin.GPIO48   # 扫描使能 (SCAN3)

# ============================================================
# W5500 以太网 SPI
# ============================================================
ETH_MOSI = microcontroller.pin.GPIO9    # SPI MOSI
ETH_MISO = microcontroller.pin.GPIO10   # SPI MISO
ETH_SCK  = microcontroller.pin.GPIO11   # SPI CLK
ETH_CS   = microcontroller.pin.GPIO12   # SPI CS
ETH_INT  = microcontroller.pin.GPIO46   # 中断 (旧版 GPIO3)
ETH_RST  = microcontroller.pin.GPIO3    # 复位 (旧版 GPIO46)

# ============================================================
# 4G 模组
# ============================================================
MODEM_PWR = microcontroller.pin.GPIO14   # PEN 电源使能 (V4G_CTRL, 旧版 GPIO42)
MODEM_TX  = microcontroller.pin.GPIO43   # UART TX (MCU TXD0 → 4G RXD)
MODEM_RX  = microcontroller.pin.GPIO44   # UART RX (4G TXD → MCU RXD0)

# ============================================================
# 新硬件特性 (05-11 新增)
# ============================================================
BOOST_EN = microcontroller.pin.GPIO39    # SPWALLON — XL6019 12V boost 使能 (高=开)
CURCTR   = microcontroller.pin.GPIO40    # ME6214 current-sense LDO EN (高=开, 才能读 ADC_CURRENT)

# ============================================================
# 通道配置表 (驱动通过此表获取引脚)
# ============================================================
RS485_CHANNELS = {
    1: {
        "tx": RS485_1_TX, "rx": RS485_1_RX, "de": RS485_1_DE,
        "vcc": RS485_1_VCC, "scan": RS485_1_SCAN,
        "v_sense": ADC_V485_4,
    },
    2: {
        "tx": RS485_2_TX, "rx": RS485_2_RX, "de": RS485_2_DE,
        "vcc": RS485_2_VCC, "scan": RS485_2_SCAN,
        "v_sense": ADC_V485_3,
    },
}

ADC_CHANNELS = {
    "vin":     ADC_VIN,
    "v485_4":  ADC_V485_4,
    "v485_3":  ADC_V485_3,
    "current": ADC_CURRENT,
}
