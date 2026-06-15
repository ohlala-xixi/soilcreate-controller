package com.rasber.controller.ota

import com.google.gson.Gson
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.net.HttpURLConnection
import java.net.URL

/** 固件文件条目 (来自服务端 manifest) */
data class OtaFile(val path: String, val sha256: String, val size: Long)

/** 某版本完整 manifest */
data class OtaManifest(val version: String, val files: List<OtaFile>)

/** 版本列表项 */
data class OtaVersion(val version: String, val fileCount: Int, val totalSize: Long)

/**
 * 服务端 OTA HTTP 接口 (BLE OTA: 手机先从服务器下固件, 再经 BLE 推板子)。
 * 用内置 HttpURLConnection, 免加依赖。所有方法阻塞, 在后台线程调用。
 *
 * baseUrl 形如 http://***:8000/api  (末尾不带 /)
 */
class OtaApi(private val baseUrl: String) {

    private val gson = Gson()

    private fun trimmedBase() = baseUrl.trimEnd('/')

    /** GET /ota/versions */
    fun listVersions(): List<OtaVersion> {
        val body = httpGetText("${trimmedBase()}/ota/versions")
        val obj = JSONObject(body)
        val arr = obj.optJSONArray("versions") ?: return emptyList()
        val out = ArrayList<OtaVersion>(arr.length())
        for (i in 0 until arr.length()) {
            val v = arr.getJSONObject(i)
            out.add(OtaVersion(v.getString("version"), v.optInt("file_count", 0), v.optLong("total_size", 0L)))
        }
        return out
    }

    /** GET /ota/manifest/{version} */
    fun getManifest(version: String): OtaManifest {
        val body = httpGetText("${trimmedBase()}/ota/manifest/$version")
        val obj = JSONObject(body)
        val arr = obj.getJSONArray("files")
        val files = ArrayList<OtaFile>(arr.length())
        for (i in 0 until arr.length()) {
            val f = arr.getJSONObject(i)
            files.add(OtaFile(f.getString("path"), f.optString("sha256", ""), f.optLong("size", 0L)))
        }
        return OtaManifest(obj.optString("version", version), files)
    }

    /** GET /ota/download/{version}/{path} → 原始字节 */
    fun downloadFile(version: String, path: String): ByteArray {
        val url = URL("${trimmedBase()}/ota/download/$version/$path")
        val conn = (url.openConnection() as HttpURLConnection).apply {
            requestMethod = "GET"
            connectTimeout = 15000
            readTimeout = 30000
        }
        try {
            val code = conn.responseCode
            if (code != 200) throw RuntimeException("download $path HTTP $code")
            val buf = ByteArrayOutputStream()
            conn.inputStream.use { ins ->
                val tmp = ByteArray(4096)
                while (true) {
                    val n = ins.read(tmp)
                    if (n < 0) break
                    buf.write(tmp, 0, n)
                }
            }
            return buf.toByteArray()
        } finally {
            conn.disconnect()
        }
    }

    private fun httpGetText(urlStr: String): String {
        val conn = (URL(urlStr).openConnection() as HttpURLConnection).apply {
            requestMethod = "GET"
            connectTimeout = 10000
            readTimeout = 15000
        }
        try {
            val code = conn.responseCode
            if (code != 200) throw RuntimeException("GET $urlStr HTTP $code")
            return conn.inputStream.bufferedReader().use { it.readText() }
        } finally {
            conn.disconnect()
        }
    }
}
