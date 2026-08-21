package com.iappyx.scanp208

import android.content.Context
import android.util.Log
import java.io.File
import java.io.PrintWriter
import java.io.StringWriter

/**
 * A scan runs with the phone's only USB port occupied by the scanner, so there
 * is no adb cable attached while it happens and nothing can be watched live.
 * Everything therefore goes to a file and to an in-app panel as well as logcat.
 */
object Diag {

    private const val TAG = "P208"
    private const val MAX_LINES = 400

    private var file: File? = null
    private val lines = ArrayDeque<String>()
    private var t0 = System.currentTimeMillis()

    @Synchronized
    fun init(ctx: Context) {
        if (file != null) return
        file = File(ctx.filesDir, "scan.log")
    }

    @Synchronized
    fun reset() {
        lines.clear()
        t0 = System.currentTimeMillis()
        runCatching { file?.writeText("") }
    }

    @Synchronized
    fun d(msg: String) {
        val line = "%6.2fs  %s".format((System.currentTimeMillis() - t0) / 1000.0, msg)
        Log.d(TAG, line)
        lines.addLast(line)
        while (lines.size > MAX_LINES) lines.removeFirst()
        runCatching { file?.appendText(line + "\n") }
    }

    @Synchronized
    fun e(msg: String, t: Throwable? = null) {
        val sw = StringWriter()
        t?.printStackTrace(PrintWriter(sw))
        val line = "%6.2fs  !! %s%s".format(
            (System.currentTimeMillis() - t0) / 1000.0, msg,
            if (t != null) "\n" + sw.toString().take(2000) else ""
        )
        Log.e(TAG, line, t)
        lines.addLast(line)
        while (lines.size > MAX_LINES) lines.removeFirst()
        runCatching { file?.appendText(line + "\n") }
    }

    @Synchronized
    fun snapshot(): List<String> = lines.toList()

    @Synchronized
    fun text(): String = lines.joinToString("\n")
}
