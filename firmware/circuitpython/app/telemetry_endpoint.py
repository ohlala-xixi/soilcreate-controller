# telemetry_endpoint.py — 遥测固定端点 (叶子模块, 单一来源, 不 import 任何项目文件)
#
# 遥测/设备报告"钉死"在此处, 不受 app/CDC 配置 (network.mqtt_*) 影响:
#   - 数据上行 + 远程控制 走 config 里可配的 broker/topic (用户/客户可改)。
#   - 遥测一切情况都回这个固定服务器/topic。
# A7670G: 独立 MQTT client_index 1 连这里; WiFi: device_reporter 单独连这里。
# (YunDTU 透传暂分不开 broker, 见 modem_4g.py — 后续可用多通道实现。)

TELEMETRY_BROKER = "***"
TELEMETRY_PORT = 1883
TELEMETRY_USER = "***"
TELEMETRY_PASS = "***"
TELEMETRY_TOPIC = "controller-manager"
