# Controller Configuration Android App

An Android configuration app for the ESP32 flexible inclinometer controller, built with Kotlin + Jetpack Compose.

## ✨ Features

### Connection Management

- **BLE Scanning**: Automatically discovers UniControl devices
- **PIN Pairing**: Secure Bluetooth connection
- **Connection Status**: Displays connection status in real time

### Sensor Operations

- **Address Scan**: Automatically scans sensors on the RS485 bus
- **Read Addresses**: Retrieves the configured sensor list from the device
- **Read Data**: Collects real-time sensor data across the A/B/Z axes
- **Read Models**: Batch reads all sensor models
- **Set Models**: Batch sets sensor models

### Sensor Configuration (ConfigTab)

- **A4 Full Scan**: Scans all sensors on the COM port and automatically fills in addresses
- **Address/Model Modification**: Updates sensor address and model one-to-one (A7/C7)
- **Set Modbus ID**: Assigns Modbus addresses to sensors (AB)
- **Batch Address Writing**: Scans by AutoID range and writes fixed addresses (A0)

### Device Configuration

- **Device ID**: Sets the unique device identifier
- **Acquisition Interval**: Selects the automatic acquisition cycle (5 minutes to 24 hours)
- **Sleep Mode**: Switches between deep sleep and light sleep

### Network Configuration

- **4G Settings**: APN and carrier selection
- **WiFi Settings**: SSID and password configuration
- **MQTT Settings**: Server, port, and topic configuration

### Advanced Settings

- **RS485 Expansion**: Switches between 2-channel and 4-channel modes
- **Merged Packet**: Merges full-channel data into a single packet
- **Local Storage**: Saves data locally by day/month
- **USB Drive Mode**: Switches computer/device read-write permissions

## 📱 Channel Support

| Channel | Description | Notes |
|------|------|------|
| COM1 | Hardware UART1 | Enabled by default |
| COM2 | Hardware UART2 | Enabled by default |
| COM3 | SC16IS752 Expansion | Requires RS485 expansion to be enabled |
| COM4 | SC16IS752 Expansion | Requires RS485 expansion to be enabled |

## 🔧 USB Drive Mode Control

Implemented based on NVM (non-volatile memory), and can be switched in any mode:

| Mode | Switch Status | Device | Computer |
|------|----------|------|------|
| Daily Mode | Off | **Read/write** | Read-only |
| Flashing Mode | On | Read-only | **Read/write** |

> ⚠️ The device must be restarted for changes to take effect.

## 🛠 Build

```bash
# Debug version
./gradlew assembleDebug

# Release version
./gradlew assembleRelease
```

## 📦 Dependencies

- Kotlin 1.9+
- Jetpack Compose
- Nordic BLE Library
- Material 3

## 📖 BLE Protocol

See the BLE protocol section in the [Firmware README](../firmware/README.md).
```
