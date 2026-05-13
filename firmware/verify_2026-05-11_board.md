# 新板 (SCH_Schematic1_2026-05-11) 固件验证表

## 背景
板子从 03-13 改到 05-11, RS485 从 4 路砍到 2 路, GPIO 大洗牌, 新增 SPWALLON / CURRENT / V485_3S。固件 (pins.py / boot.py / drivers / code.py / data_formatter.py / config.json) 已同步重写。本表用于烧固件后逐项验证。

## 准备
- 一块 05-11 新板, 已烧 CircuitPython 10.1.0-beta.1 (n8r2 XTAL-enabled)
- `cd firmware && bash deploy_circuitpython.sh` 部署 Python 代码
- 至少一个 RS485 传感器接到 CN3 (COM1) 上, 地址写进 `config.json` 的 `rs485_1.sensors`
- 万用表 (测 12V 输出排针)
- PC 工具开着 (`cd pc_tool && python3 main.py`)

按表格顺序跑, 任一项失败先停下来定位再继续。

## 验证表

| # | 测试项 | 操作 | 期望 | 失败诊断方向 | 结果 |
|---|---|---|---|---|---|
| 1 | **上电不挂** | 烧固件 → 插 USB → 等 5s | CIRCUITPY 盘出现, CDC 串口能看到 `[BOOT] N8R2 GPIO 安全初始化: 10/10` + `[BOOT] GPIO 安全初始化完成` | 任一脚 init fail → `pins.py` GPIO 写错; 整个挂 → `pins.py` 语法/import 错 | ☐ |
| 2 | **CDC 命令通** | `#help` | 列出所有命令组, 含 "电源/电流" 段, 无 `enable_expansion` 字样 | 见不到电源段 → `code.py` help 文本没改对 | ☐ |
| 3 | **状态正确** | `#status` | 显示 CH1/CH2 sensor 数, `12V boost: ON`, `系统电流: ~xxx mA`, 无 `扩展板:` 那行 | `boost: OFF` → `power.boost_auto` 没读到 / pin init 失败 | ☐ |
| 4 | **boost 软关断** | `#disable_boost` → 万用表测 12V 输出排针 | 12V → 0V (D5/D6 后的 VOUT12V 节点) | 不掉 → `BOOST_EN=GPIO39` 接错或 XL6019 内部上拉过强 | ☐ |
| 5 | **boost 软开启** | `#enable_boost` → 万用表测 | 0 → 12V 回来 | — | ☐ |
| 6 | **电流读数** | RS485 通电中 `#get_current` | `50-300 mA` 量级 (空板 ~10 mA) | 一直 `0 mA` → `CURCTR` pin 没拉高或 ACS712/ME6214 没供电; 负值大 → ACS712 零点偏 (校准 `voltage.py: ACS_ZERO_V`) | ☐ |
| 7 | **W5500 (INT/RST 互换验证)** | `#enable_eth` → `#reboot` → 等 10s → `#status` | 能拿到 IP, ETH 显示 on | 拿不到 IP → `ETH_INT (GPIO46)` / `ETH_RST (GPIO3)` 接错; 板子 hang → RST 拉错方向 | ☐ |
| 8 | **4G (V4G_CTRL 新位置)** | `#enable_4g` → `#reboot` → 等 30s → 看 log | 4G 模块上电, `CSQ` 信号正常, 拨号成功 | 不上电 → `MODEM_PWR (GPIO14)` 接错; 上电不通信 → TX/RX (GPIO43/44) 仍应不变 | ☐ |
| 9 | **COM1 RS485 (新 GPIO 全套)** | `#scan 1` | 扫到 `config.json` 里 `rs485_1.sensors` 列的地址 | 没响应 → TX/RX/DE/VCC 任一脚错; 数据乱 → DE 方向反 (`rs485_invert`) | ☐ |
| 10 | **COM2 RS485** | `#scan 2` (如有传感器) | 同上 | — | ☐ |
| 11 | **COM3/COM4 已死** | 手动发 `#scan 3` | 回 "通道未初始化" 类的错, 不应崩 | 崩 → driver 还在尝试初始化老通道 | ☐ |
| 12 | **VCC 通断诊断** | COM1 接传感器 → `#diag_com 1` | `VCC off=0.0V, on=~12V → OK` | `on` 不到 9V → V485_4 分压比错 (11.0) 或 VOUTCTR4 没起来; `off` 不到 2V → VCC4 关不掉 (P-MOS 坏或 R31 失效) | ☐ |
| 13 | **数据上报含 current** | 正常采集一轮 → 看 MQTT 收到的 JSON | header 含 `"current": <整数 mA>` 字段, `V1`/`V2` 是 v485_4 / v485_3 真实电压 | 缺字段 → `data_formatter.py` 没生效 | ☐ |
| 14 | **PC 工具按钮** | 打开 PC 工具 → 切到"电源"分类 | 4 个按钮: `#enable_boost` / `#disable_boost` / `#get_current` / `#diag_com*` | 缺 → `commands.py` 没改对 / `main_window.py` CATEGORIES 渲染漏了"电源" | ☐ |
| 15 | **deep sleep 自动关 boost** (可选) | 配 `system.sleep_mode=deep` + 短 interval → 进 sleep 时观察 12V | sleep 期间 12V → 0V (省电); 醒来恢复 12V | 不掉 → sleep 入口没调 `set_boost(False)` — **当前实现没加, 是个 TODO** | ☐ |

## 已知遗漏
- **第 15 项**: plan 提到 "deep sleep 前 disable boost" 但 `code.py` 的 sleep 路径没加 `set_boost(False)` 调用。如果要省电效果, 需要补 (找 `deep_sleep` / `light_sleep` 进入点, 调用前加 `set_boost(False)`)。不阻塞功能验证。
- **ACS712 校准**: `voltage.py` 里 `ACS_DIVIDER = 0.6` / `ACS_ZERO_V = 2.5` / `ACS_SENS_V_PER_A = 0.185` 是理论值。如果第 6 项实测偏差大, 用已知电流 (比如恒流源 100 mA) 反推校准。
- **V485 分压比**: 11.0 是按 R44=100k / R43=10k 算的。如果第 12 项的 `on` 电压不准, 可能板上电阻实际值偏差, 调 `voltage.py: DIVIDER_RATIO`。

## 改动文件清单 (本次)
- `firmware/circuitpython/pins.py` (重写)
- `firmware/circuitpython/boot.py` (重写 `_pin_list`)
- `firmware/circuitpython/drivers/rs485.py` (删 SC16IS752 分支)
- `firmware/circuitpython/drivers/voltage.py` (加 `read_current` + V485_3/4)
- `firmware/circuitpython/code.py` (删 expansion + 加 4 个新命令 + #status 加电源/电流)
- `firmware/circuitpython/app/data_formatter.py` (header 加 `current` + V1/V2 接 v485)
- `firmware/circuitpython/config.json` (加 `power.boost_auto` + `current.alarm_threshold_a`)
- `pc_tool/commands.py` (删 expansion + 加电源分类 4 按钮)
