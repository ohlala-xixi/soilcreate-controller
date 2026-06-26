# fw_version.py — 固件版本号唯一权威来源 (叶子模块, 不 import 任何项目文件)
#
# 版本号是"这份代码是谁"的身份, 随代码走, 不存 NVM/config.json。
# OTA 换了这个文件 → 版本自动跟着变, 永不与运行的代码漂移。
# code.py 与 app/device_reporter.py 都从这里读, 避免互相 import 成环。

FIRMWARE_VERSION = "2026.06.18-cirpy"
