```markdown
# Uni-Controller Android App User Guide

**Version**: 2026.02.02  
**Supported Device**: ESP32-S3-based flexible inclinometer controller  
**Package Name**: `com.rasber.controller`  
**Connection Method**: Bluetooth Low Energy (BLE)

---

## 1. Installation and Environment Requirements

### Phone Requirements

- Android 8.0 (API 26) or later
- Bluetooth Low Energy (BLE) support

### Installation Steps

1. Copy the installation package `UniController.apk` to the phone.
2. Open it to install. If prompted about "unknown sources", allow installation in system settings.
3. When the app is opened for the first time, it will request the following permissions. Allow all of them:
   - **Bluetooth**: for scanning and connecting to devices
   - **Location**: required by Android for BLE scanning

### Pre-Launch Checklist

- Phone Bluetooth is turned on
- Phone Location is turned on at the system level, not only for the app
- The target device is powered on and BLE advertising is active, with the default device name `UniControl`

---

## 2. Interface Overview

The app has 4 tabs at the bottom, from left to right:

| Tab | Purpose |
|---|---|
| **Connection** | Scan and connect to devices, synchronize time, disconnect |
| **Configuration** | Batch sensor address writing and model setting for production scenarios |
| **Data** | Address scanning, data reading, and model management for debugging/inspection |
| **Settings** | Device parameters, network configuration, and system control |

**Key Rule**: Before entering the Configuration, Data, or Settings tab, you must first complete Bluetooth connection on the Connection tab. Otherwise, operations will not take effect.

---

## 3. Connection Tab

### Scan for Devices

1. Tap **Scan**.
2. The scan window lasts 15 seconds, and nearby BLE devices will appear in the list in real time.
3. Find the target device, usually named `UniControl` or a custom name, and tap it to connect.

### Connection Status

- The top area displays the connection indicator. Green means connected.
- When the device disconnects, a **red banner** appears at the top.

### Time Synchronization

After a successful connection, tap **Sync Time**. The app will send the phone’s current time, including time zone, to the device RTC.

Use this when deploying a new device for the first time, when a device comes back online after being offline for a long time, or when module time synchronization is abnormal.

### Disconnect

Use **Disconnect** to disconnect manually. Switching away from the app will usually also disconnect automatically.

---

## 4. Configuration Tab (Production Scenario)

This page is used for factory setup and batch deployment. Its main workflow is assigning addresses and setting models for batches of sensors.

### Workflow A: Broadcast Read (A4) — Initial Discovery

Tap **A4 Broadcast Read** → select the COM port (COM1 / COM2) → start reading.

The device broadcasts a read command to all sensors on the bus and returns:

- AutoID, the factory unique ID
- A / B / Z-axis calibration coefficients

The results are filled into the address input boxes for the following steps, avoiding manual copying.

### Workflow B: Single-Point Address Change (A6 / A7)

- **A6 (one-to-one address change)**: Set a new address for a single sensor, with optional model setting.
  - The model dropdown includes 8 options, such as 3-axis, 2-axis, and fixed 3-axis.
  - Tap **Address +1** to automatically increment the new address, useful for continuous programming.
- **A7 (rename address)**: Change an existing old address to a new address, with read-back verification.

### Workflow C: Modbus ID Setting

Set the Modbus ID for sensors one by one by address.

### Workflow D: Batch Address Writing

Suitable for production line scenarios:

1. Set the AutoID scan range (start/end).
2. Set the starting address and maximum address.
3. Set the delay between devices to prevent bus conflicts.
4. Tap Start. The app scans AutoIDs and assigns a decreasing address whenever a device is matched.
5. The progress bar runs from 0 to 1024, and the result table shows the latest 20 records.

You can also **read models during batch processing** and **write one model to all devices**.

---

## 5. Data Tab (Debugging / Inspection)

### COM Port Switching

Switch COM1 / COM2 at the top. All operations apply only to the currently selected port.

### Read Addresses vs Scan Addresses

- **Read Addresses**: Pulls the **registered** sensor list from the device configuration. This usually takes seconds.
- **Scan Addresses**: Probes actual online sensors on the bus **from 0 to 1024**. This may take minutes and includes a progress bar.

Usually, use **Read Addresses** first to check the configuration. If it does not match the actual hardware, use **Scan Addresses** to confirm the bus.

### Read Data

- **Poll All**: Tap **Read Data** to read A/B/Z data from all sensors on the current COM port one by one.
- **Single-Point Polling**: Tap an address row in the table to read only that sensor.

### Data Table

Columns are: Address (clickable) · Status · AutoID · A(X) · B(Y) · Z · Model.

Status icons:

- `✅` Read successful
- `📍` Found but not read yet
- `❌` Read failed: offline, checksum error, or address conflict
- `🔧` Model read successful
- `⏳` Processing

### Model Management

- **Read All Models**: Reads the model from every sensor.
- **Set All Models**: Select a model and push it to all sensors with one tap.

---

## 6. Settings Tab

### Device Identity

- **Device ID**: Used by the cloud platform to identify the device. Editable.
- **Free Memory**: Shows runtime RAM available on the device.
- **Firmware Version**: Read-only firmware version of the current device.

### System Parameters

| Item | Description |
|---|---|
| Interval preset | 1=5 min, 2=10 min, 3=15 min, 4=30 min, 5=1 h, 6=2 h, 7=4 h, 8=12 h, 9=24 h, 99=custom, 0=standby |
| Custom interval | Effective when preset=99, in minutes |
| Sleep mode | `light` light sleep for faster response / `deep` deep sleep for lower power consumption |
| RS485 expansion | Whether to enable SC16IS752 expansion ports (COM3/COM4) |
| Merge segments | Whether to merge multiple segments when uploading data |

### Network Configuration

#### WiFi

- SSID + password
- Enable/disable switch

#### 4G

- **APN**: Carrier access point
  - China Mobile: `cmnet`
  - China Unicom: `3gnet` or `uninet`
  - China Telecom: `ctnet`
  - For overseas carriers, enter the carrier-specific APN
- **COPS**: Carrier selection, `0` = automatic
- **Module model**: `A7670C`, `A7670G`, `SIM7672E`, etc.
- **Protocol**: `yundtu` for Cloud DTU encapsulation / `simcom` for native AT commands on bare modules

> If unsure, choose according to the actual hardware: Cloud DTU → `yundtu`; bare A7670G/SIM module → `simcom`.

#### MQTT

- Broker IP and port
- Publish topic
- Username and password
- Enable/disable switch

### Local Storage

- When enabled, data is cached to the device flash first if the network is unavailable.
- Storage period: `month` or `day` cleanup granularity.

### Control Buttons

| Button | Description |
|---|---|
| **Read Configuration** | Pull the current configuration from the device into the app UI |
| **Write Configuration** | Push the UI configuration to the device and persist it to device NVM |
| **Import Address Table** | Import a CSV sensor address table from phone storage |
| **Load Default Configuration** | Restore factory defaults with one tap. Use with caution |
| **USB Read/Write Switch** | Switch between daily mode and flash mode. Takes effect after restart |
| **Restart Device** | Soft restart |
| **Collect** | Immediately trigger one round of data acquisition and upload, without waiting for the scheduled interval |

---

## 7. Common Operation Flow Quick Reference

### Flow 1: First Deployment of a New Device

1. Power on the device and open the app.
2. Go to **Connection** → Scan → connect to the target device.
3. Go to **Connection** → Sync Time.
4. Go to **Settings** → enter Device ID, network settings (WiFi or 4G), and MQTT → Write Configuration.
5. Go to **Data** → Scan Addresses → confirm that sensors are online.
6. Go to **Settings** → Collect → check whether the MQTT backend receives data.

### Flow 2: Replacing the 4G Module, for Example A7670C → A7670G

1. Connect to the device.
2. Go to **Settings** → Network → 4G section, set module model to `A7670G` and protocol to `simcom`.
3. Tap **Write Configuration**.
4. Tap **Restart Device**.

### Flow 3: Factory Batch Address Writing

1. Connect to the device.
2. Go to **Configuration** → A4 Broadcast Read → identify which sensors are online on the bus.
3. Go to **Configuration** → Batch Address Writing → set parameters → start.
4. After completion, go to **Data** → Scan Addresses to verify.

### Flow 4: On-Site Troubleshooting for "Cannot Upload to Cloud"

1. Connect to the device. Go to **Settings** → Read Configuration and check the MQTT address, port, and account.
2. Go to **Settings** → Sync Time. TLS/certificate validation depends on correct time.
3. Go to **Settings** → Collect and check log feedback.
4. If the device is offline, use serial USB CDC to check `[4G]` / `[Upload]` logs.

---

## 8. Troubleshooting

### Cannot Find the Device

- Is Bluetooth turned on? Is Location turned on?
- The device may be too far from the phone or not powered on.
- The device may already be connected to another phone. BLE is one-to-one.
- Restart the app and restart phone Bluetooth.

### Disconnects Immediately After Connecting

- The device may be busy with another task, such as batch writing. Try again later.
- The signal may be weak due to distance.

### Configuration Write Failed

- Check field formats. IP address and port must be valid, and SSID should avoid special characters.
- Device NVM space may be insufficient. Remove some non-essential fields and try again.

### Data Table Is All Red `❌`

- The wrong COM port may be selected.
- Sensors may be unpowered or RS485 wiring may be reversed.
- Baud rate may not match. Check the RS485 baud rate on the Settings page.

### Abnormal Time Display, Such as Year 1933

- Firmware time parsing may have encountered a module default value such as 2070-01-01, causing RTC overflow.
- Solution: power-cycle the device physically → connect with the app → synchronize time.

---

## 9. Notes

- **Bluetooth connection is one-to-one**: only one phone can connect to one device at a time.
- **Fields that do not take effect immediately after writing configuration**: USB read/write mode and module model switching. A restart is required.
- **Default configuration clears sensor addresses**: export the address table before tapping **Load Default Configuration**.
- **Do not disconnect during batch writing**: disconnecting midway may cause incorrect addresses on some devices, requiring A4 discovery again.
```
