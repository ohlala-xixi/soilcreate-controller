package com.rasber.controller.ota

import android.os.Handler
import android.os.Looper
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.rasber.controller.ble.BleManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

private const val DEFAULT_BASE = "http://***:8000/esp32-api/api"

/**
 * BLE 固件升级对话框 — 自包含。
 * 流程: 查服务器版本 → 选版本 → 下载固件 → BLE 推送 (含 daily 切换重连) → 板子切换重启。
 */
@Composable
fun OtaDialog(ble: BleManager, onDismiss: () -> Unit) {
    val scope = rememberCoroutineScope()
    val ui = remember { Handler(Looper.getMainLooper()) }

    var baseUrl by remember { mutableStateOf(DEFAULT_BASE) }
    var versions by remember { mutableStateOf<List<OtaVersion>>(emptyList()) }
    var selected by remember { mutableStateOf<String?>(null) }
    var running by remember { mutableStateOf(false) }
    var progress by remember { mutableStateOf(0f) }
    var status by remember { mutableStateOf("") }
    val logs = remember { mutableStateListOf<String>() }
    var verExpanded by remember { mutableStateOf(false) }

    fun log(line: String) = ui.post {
        logs.add(line); if (logs.size > 200) logs.removeAt(0)
        android.util.Log.i("OTA", line)   // 同时打到 logcat (adb logcat -s OTA)
    }
    fun setProgress(sent: Long, total: Long) = ui.post {
        progress = if (total > 0) (sent.toFloat() / total) else 0f
        status = "推送中 ${sent / 1024}/${total / 1024} KB"
    }

    AlertDialog(
        onDismissRequest = { if (!running) onDismiss() },
        title = { Text("固件升级 (BLE OTA)") },
        text = {
            Column(modifier = Modifier.fillMaxWidth()) {
                OutlinedTextField(
                    value = baseUrl,
                    onValueChange = { baseUrl = it },
                    label = { Text("服务器 API 地址") },
                    singleLine = true,
                    enabled = !running,
                    modifier = Modifier.fillMaxWidth()
                )
                Spacer(Modifier.height(8.dp))

                Row(verticalAlignment = Alignment.CenterVertically) {
                    OutlinedButton(
                        enabled = !running,
                        onClick = {
                            scope.launch {
                                status = "查询版本..."
                                try {
                                    val vs = withContext(Dispatchers.IO) { OtaApi(baseUrl).listVersions() }
                                    versions = vs
                                    status = if (vs.isEmpty()) "无可用版本" else "共 ${vs.size} 个版本"
                                    if (vs.isNotEmpty() && selected == null) selected = vs.last().version
                                } catch (e: Exception) {
                                    status = "查询失败: ${e.message}"
                                }
                            }
                        }
                    ) { Text("查询版本") }

                    Spacer(Modifier.width(8.dp))

                    Box {
                        OutlinedButton(enabled = !running && versions.isNotEmpty(), onClick = { verExpanded = true }) {
                            Text(selected ?: "选择版本")
                        }
                        DropdownMenu(expanded = verExpanded, onDismissRequest = { verExpanded = false }) {
                            versions.forEach { v ->
                                DropdownMenuItem(
                                    text = { Text("${v.version}  (${v.fileCount}文件 ${v.totalSize / 1024}KB)") },
                                    onClick = { selected = v.version; verExpanded = false }
                                )
                            }
                        }
                    }
                }

                Spacer(Modifier.height(8.dp))
                if (running || progress > 0f) {
                    LinearProgressIndicator(progress = progress, modifier = Modifier.fillMaxWidth())
                    Spacer(Modifier.height(4.dp))
                }
                Text(status, fontSize = 12.sp)

                Spacer(Modifier.height(8.dp))
                // 日志
                SelectionContainer {
                    Column(
                        modifier = Modifier
                            .fillMaxWidth()
                            .height(180.dp)
                            .verticalScroll(rememberScrollState())
                    ) {
                        logs.takeLast(60).forEach { Text(it, fontSize = 11.sp, fontFamily = FontFamily.Monospace) }
                    }
                }
            }
        },
        confirmButton = {
            TextButton(
                enabled = !running && ble.isConnected && selected != null,
                onClick = {
                    val ver = selected ?: return@TextButton
                    running = true; progress = 0f; logs.clear()
                    scope.launch {
                        val ok = runOta(ble, baseUrl, ver, ::log) { s, t -> setProgress(s, t) }
                        ui.post {
                            running = false
                            status = if (ok) "✅ 升级成功, 板子重启中" else "❌ 升级失败 (看日志)"
                            if (ok) progress = 1f
                        }
                    }
                }
            ) { Text(if (running) "升级中..." else "开始升级") }
        },
        dismissButton = {
            TextButton(enabled = !running, onClick = onDismiss) { Text("关闭") }
        }
    )
}

/** 完整 OTA: 下载 → BLE 会话 (含 daily 切换重连)。返回是否成功。 */
private suspend fun runOta(
    ble: BleManager, baseUrl: String, version: String,
    onLog: (String) -> Unit, onProgress: (Long, Long) -> Unit,
): Boolean {
    val api = OtaApi(baseUrl)
    // 1. manifest + 下载
    val manifest = try {
        onLog("拉取 manifest $version ...")
        withContext(Dispatchers.IO) { api.getManifest(version) }
    } catch (e: Exception) { onLog("manifest 失败: ${e.message}"); return false }

    val bytes = HashMap<String, ByteArray>()
    for (f in manifest.files) {
        try {
            onLog("下载 ${f.path} (${f.size}B)...")
            bytes[f.path] = withContext(Dispatchers.IO) { api.downloadFile(version, f.path) }
        } catch (e: Exception) { onLog("下载 ${f.path} 失败: ${e.message}"); return false }
    }
    onLog("下载完成, 共 ${manifest.files.size} 文件")

    // 2. BLE 会话 (NeedReconnect → 重连重试)
    val client = BleOtaClient(ble)
    client.attach()
    try {
        var attempts = 0
        while (true) {
            val r = withContext(Dispatchers.IO) {
                client.runSession(version, manifest.files, bytes, onProgress, onLog)
            }
            when (r) {
                is BleOtaClient.Result.Success -> { onLog("✅ ota_commit, 板子切换重启"); return true }
                is BleOtaClient.Result.Failed -> { onLog("❌ ${r.msg}"); return false }
                is BleOtaClient.Result.NeedReconnect -> {
                    if (++attempts > 2) { onLog("❌ 重连次数超限"); return false }
                    onLog("板子切 daily 重启, 等待重新广播...")
                    ble.disconnect()
                    delay(6000)   // 板子热复位 → boot → BLE init → 重新广播, 给足时间
                    // 多次重试: 每次 connectGatt 各等 ~25s, 共最多 3 轮 (Android connectGatt 自身也有超时)
                    var connected = false
                    for (tryN in 1..3) {
                        onLog("重连尝试 $tryN/3 ...")
                        ble.reconnect()
                        var waited = 0
                        while (!ble.isConnected && waited < 25000) { delay(500); waited += 500 }
                        if (ble.isConnected) { connected = true; break }
                        onLog("第 $tryN 次未连上, 重试...")
                        ble.disconnect(); delay(3000)
                    }
                    if (!connected) { onLog("❌ 重连失败 (板子可能没起来/没广播)"); return false }
                    delay(3000)   // 等服务发现 + CCCD 写完 + 稳定
                    onLog("重连成功, 重发 ota_begin")
                }
            }
        }
    } finally { client.detach() }
}
