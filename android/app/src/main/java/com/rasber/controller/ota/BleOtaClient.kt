package com.rasber.controller.ota

import android.util.Log
import com.rasber.controller.ble.BleManager
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * BLE OTA 编排 (手机端) — 对应固件 app/ble_ota.py。
 *
 * 协议 (docs/ble_ota_design.md §4):
 *   控制帧: {...}\n  JSON (经 BleManager.sendCommand 发 / otaControlListener 收)
 *   数据帧: [0xA5][0x10][seq u16 LE][len u16 LE][payload]  (BleManager.writeNoResponse)
 *
 * 一次 runSession 是一个"会话尝试": 板子若需切 daily 会回 ota_reboot → 本方法返回
 * NeedReconnect, 由上层重连后再调一次。
 */
class BleOtaClient(private val ble: BleManager) {

    companion object {
        private const val TAG = "BleOtaClient"
        const val SYNC: Byte = 0xA5.toByte()
        const val TYPE_DATA: Byte = 0x10
        const val FRAME_HDR = 6        // 数据帧头: [A5][10][seq u16 LE][len u16 LE]
        const val MAX_CHUNK = 244      // payload 上限 (板子默认)
        const val MIN_CHUNK = 20       // 低于此值视为 MTU 未协商好, 拒绝开始 (避免极慢/截断)
        const val WINDOW = 4
        private const val ACK_TIMEOUT = 12000L
        private const val READY_TIMEOUT = 20000L
    }

    sealed class Result {
        object Success : Result()          // 板子已 commit + reset
        object NeedReconnect : Result()    // 板子切 daily 中, 上层重连后重试
        data class Failed(val msg: String) : Result()
    }

    private val ctrl = LinkedBlockingQueue<JSONObject>()

    fun attach() {
        ble.otaControlListener = { line ->
            try { ctrl.put(JSONObject(line)) } catch (_: Exception) {}
        }
    }

    fun detach() {
        ble.otaControlListener = null
        ctrl.clear()
    }

    /**
     * 等待期望控制帧。
     * @param expectIdx >=0 时校验帧内 idx == expectIdx (板子的 ota_ack/ota_nak/ota_file_* 都带 idx),
     *        不匹配视为上个文件的残留帧, 丢弃继续等; ota_error 不带 idx, 始终放行。
     */
    private fun await(cmds: Set<String>, timeoutMs: Long, expectIdx: Int = -1): JSONObject? {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (true) {
            val left = deadline - System.currentTimeMillis()
            if (left <= 0) return null
            val o = ctrl.poll(left, TimeUnit.MILLISECONDS) ?: return null
            val cmd = o.optString("cmd")
            if (cmd !in cmds) continue          // 非期望帧忽略 (继续等)
            if (expectIdx >= 0 && cmd != "ota_error") {
                val fidx = o.optInt("idx", -1)
                if (fidx != expectIdx) {
                    Log.w(TAG, "丢弃残留控制帧 (idx=$fidx != $expectIdx): $o")
                    continue
                }
            }
            return o
        }
    }

    private fun sendCtrl(obj: JSONObject) {
        ble.sendCommand(obj.toString())
    }

    private fun dataFrame(seq: Int, payload: ByteArray, len: Int): ByteArray {
        val out = ByteArray(6 + len)
        out[0] = SYNC
        out[1] = TYPE_DATA
        out[2] = (seq and 0xFF).toByte()
        out[3] = ((seq shr 8) and 0xFF).toByte()
        out[4] = (len and 0xFF).toByte()
        out[5] = ((len shr 8) and 0xFF).toByte()
        System.arraycopy(payload, 0, out, 6, len)
        return out
    }

    /**
     * 执行一次 OTA 会话。
     * @param fileBytes path → 文件原始字节
     * @param onProgress (已发字节, 总字节)
     * @param onLog 日志行
     */
    fun runSession(
        version: String,
        files: List<OtaFile>,
        fileBytes: Map<String, ByteArray>,
        onProgress: (Long, Long) -> Unit,
        onLog: (String) -> Unit,
    ): Result {
        val totalBytes = files.sumOf { it.size }
        var sentBytes = 0L

        // 0. 按协商 MTU 定 chunk: 单次 BLE 写 ≤ mtu-3 (ATT 头), 数据帧 = 6B 头 + payload
        val mtu = ble.mtu
        val chunk = minOf(MAX_CHUNK, mtu - 3 - FRAME_HDR)
        if (chunk < MIN_CHUNK) {
            return Result.Failed("MTU 过小 (mtu=$mtu → chunk=$chunk), MTU 可能未协商成功, 请断开重连后重试")
        }
        onLog("数据帧 chunk=$chunk (mtu=$mtu)")

        // 残留控制帧清掉 (上次会话/重连前的)
        ctrl.clear()

        // 1. ota_begin (带 manifest)
        val manifest = JSONArray()
        for (f in files) {
            manifest.put(JSONObject().apply {
                put("path", f.path); put("size", f.size); put("sha256", f.sha256)
            })
        }
        val begin = JSONObject().apply {
            put("cmd", "ota_begin"); put("ver", version)
            put("chunk", chunk); put("window", WINDOW)
            put("total", totalBytes); put("files", manifest)
        }
        onLog("→ ota_begin ver=$version files=${files.size} total=${totalBytes}B")
        sendCtrl(begin)

        // 2. 等 ota_ready / ota_reboot / ota_error
        val r0 = await(setOf("ota_ready", "ota_reboot", "ota_error"), READY_TIMEOUT)
            ?: return Result.Failed("等 ota_ready 超时")
        when (r0.optString("cmd")) {
            "ota_reboot" -> {
                onLog("← ota_reboot (板子切 daily 模式), 需重连重发")
                return Result.NeedReconnect
            }
            "ota_error" -> return Result.Failed("板子: ${r0.optString("msg")}")
            "ota_ready" -> onLog("← ota_ready, 开始推文件")
        }

        // 3. 逐文件推送
        for ((idx, f) in files.withIndex()) {
            val bytes = fileBytes[f.path] ?: return Result.Failed("缺文件字节: ${f.path}")
            ctrl.clear()    // 清掉上个文件可能残留的 ota_ack/ota_nak
            onLog("→ ota_file[$idx] ${f.path} (${f.size}B)")
            sendCtrl(JSONObject().apply {
                put("cmd", "ota_file"); put("idx", idx); put("path", f.path); put("size", f.size)
            })
            val ready = await(setOf("ota_file_ready", "ota_file_err", "ota_error"), READY_TIMEOUT, idx)
                ?: return Result.Failed("等 ota_file_ready[$idx] 超时")
            if (ready.optString("cmd") != "ota_file_ready") {
                return Result.Failed("file[$idx]: ${ready.optString("msg")}")
            }

            val res = sendOneFile(idx, bytes, chunk, sentBytes, totalBytes, onProgress, onLog)
            if (res != null) return res     // 非 null = 失败
            sentBytes += f.size
            onProgress(sentBytes, totalBytes)
            onLog("← ota_file_ok[$idx]")
        }

        // 4. ota_end → ota_commit
        onLog("→ ota_end")
        sendCtrl(JSONObject().apply { put("cmd", "ota_end") })
        val commit = await(setOf("ota_commit", "ota_error"), READY_TIMEOUT)
            ?: return Result.Failed("等 ota_commit 超时")
        if (commit.optString("cmd") != "ota_commit") {
            return Result.Failed("commit 失败: ${commit.optString("msg")}")
        }
        onLog("← ota_commit, 板子即将切换并重启 ✓")
        return Result.Success
    }

    /** 推一个文件的所有数据帧 (窗口 + ack/nak)。成功返回 null, 失败返回 Result.Failed。 */
    private fun sendOneFile(
        idx: Int, bytes: ByteArray, chunk: Int, baseBytes: Long, totalBytes: Long,
        onProgress: (Long, Long) -> Unit, onLog: (String) -> Unit,
    ): Result? {
        val total = bytes.size
        val totalFrames = (total + chunk - 1) / chunk
        var seq = 0
        var sinceAck = 0
        var fileEndSent = false

        while (true) {
            if (seq < totalFrames) {
                val off = seq * chunk
                val len = minOf(chunk, total - off)
                val frame = dataFrame(seq, bytes.copyOfRange(off, off + len), len)
                // 同步带响应写 (board 收不到无响应写); writeFrame 内部已等 onCharacteristicWrite
                if (!ble.writeFrame(frame)) {
                    Thread.sleep(30)
                    if (!ble.writeFrame(frame)) return Result.Failed("file[$idx] 写帧失败 @seq=$seq (帧 ${frame.size}B, mtu=${ble.mtu})")
                }
                seq++; sinceAck++

                if (sinceAck >= WINDOW && seq < totalFrames) {
                    val r = await(setOf("ota_ack", "ota_nak", "ota_file_err", "ota_error"), ACK_TIMEOUT, idx)
                        ?: return Result.Failed("file[$idx] 等 ota_ack 超时 @seq=$seq")
                    when (r.optString("cmd")) {
                        "ota_ack" -> { sinceAck = 0; onProgress(baseBytes + (seq.toLong() * chunk).coerceAtMost(total.toLong()), totalBytes) }
                        "ota_nak" -> { seq = r.optInt("expect", 0); sinceAck = 0; onLog("← ota_nak expect=$seq, 回退重发") }
                        else -> return Result.Failed("file[$idx]: ${r.optString("msg")}")
                    }
                }
                continue
            }

            // 所有帧已发
            if (!fileEndSent) {
                // 末尾若正好凑满一窗, 板子会发 ack, 先 drain
                if (sinceAck >= WINDOW) {
                    val r = await(setOf("ota_ack", "ota_nak", "ota_file_err", "ota_error"), ACK_TIMEOUT, idx)
                        ?: return Result.Failed("file[$idx] 等末窗 ota_ack 超时")
                    when (r.optString("cmd")) {
                        "ota_ack" -> sinceAck = 0
                        "ota_nak" -> { seq = r.optInt("expect", 0); sinceAck = 0; continue }
                        else -> return Result.Failed("file[$idx]: ${r.optString("msg")}")
                    }
                }
                sendCtrl(JSONObject().apply { put("cmd", "ota_file_end"); put("idx", idx) })
                fileEndSent = true
            }

            val r = await(setOf("ota_file_ok", "ota_file_err", "ota_nak", "ota_error"), READY_TIMEOUT, idx)
                ?: return Result.Failed("file[$idx] 等 ota_file_ok 超时")
            when (r.optString("cmd")) {
                "ota_file_ok" -> return null
                "ota_nak" -> { seq = r.optInt("expect", 0); sinceAck = 0; fileEndSent = false }   // 回退重发
                else -> return Result.Failed("file[$idx]: ${r.optString("msg")}")
            }
        }
    }
}
