package com.iappyx.scanp208

import android.graphics.Bitmap
import kotlin.math.abs
import kotlin.math.roundToInt

enum class ScanMode(val wire: Int, val label: String) {
    COLOR(0x05, "Colour"),
    GRAY(0x02, "Greyscale");
    val bytesPerPixel: Int get() = if (this == COLOR) 3 else 1
}

enum class Dropout(val value: Int, val label: String) {
    NONE(0, "None"), RED(1, "Red"), GREEN(2, "Green"), BLUE(3, "Blue")
}

data class ScanSettings(
    val dpi: Int = 300,
    val mode: ScanMode = ScanMode.COLOR,
    val duplex: Boolean = false,
    val autosize: Boolean = true,
    val dropout: Dropout = Dropout.NONE,
    val enhance: Dropout = Dropout.NONE,
    val lightCurve: Boolean = true,
    /** Stream the whole stack as ONE long image instead of separate pages. */
    val continuous: Boolean = false
)

/**
 * The Canon imageFORMULA P-208, over [UsbTransport].
 *
 * A scan is: program the window, derive the analogue operating point, measure
 * the dark and white references, then open one session and feed sheets through
 * it. A batch is a single session with sheets separated inside it - not a loop
 * around the single-sheet path.
 */
class ScannerDriver(private val t: UsbTransport) {

    companion object {
        private const val SET_WINDOW = 0x24
        private const val READ10 = 0x28
        private const val MODE_SELECT = 0x15
        private const val SET_SCAN_MODE = 0xd6
        private const val ADJUST_DATA = 0xe1
        private const val INQUIRY = 0x12
        private const val SCAN = 0x1b

        const val UNITS_PER_INCH = 1200

        // Unit counts, not inches converted on the way in: 8.5 in is 10200
        // units while the sensor is 10208, and a window derived from inches
        // came out eight units narrower than the pixel count computed from it.
        const val WIN_WIDTH_UNITS = 10208
        const val WIN_LENGTH_UNITS = 17736

        private const val WIN_DARK = 0xff
        private const val WIN_WHITE = 0xfe
        private const val SIDE_FRONT = 0
        private const val SIDE_BACK = 1

        private const val PAGE_BUFFER = 0x32
        private const val PAGE_30 = 0x30
        private const val PAGE_36 = 0x36
        private const val FEED_CONTINUOUS = 0x40
        private const val FEED_AUTOSIZE = 0x20

        private const val CAL_STREAM_LINES = 48

        // 2 MiB in one synchronous transfer stalls the endpoint at dpi_y 150,
        // where lines arrive at half the rate. 1 MiB is proven at every
        // resolution we scan.
        // Same as driver.py. Android splits it into 16 KB transfers underneath,
        // but the device is asked for exactly what the Python driver asks for.
        private const val READ_CHUNK = 0x100000

        val DPIS = listOf(150, 200, 300, 400, 600)

        /**
         * Requested dpi -> the (dpi_x, dpi_y) actually scanned. Only 300 and
         * 600 are right on both axes, so everything else is scanned at one of
         * them and resampled.
         *
         *   X must be 300 or 600. The sensor is a whole number of pixels only
         *   at 150, 300 and 600; at 200 it is 1701.33 and the third of a pixel
         *   lost per line walks the image sideways.
         *
         *   Y may be anything except 150, where the device delivers 10.95 in
         *   of a 12.32 in frame and cuts mid-line.
         */
        val SCAN_PLAN = mapOf(
            150 to Pair(300, 150), 200 to Pair(300, 200),
            240 to Pair(300, 240), 400 to Pair(600, 400)
        )

        fun pixels(dpi: Int): Int = (WIN_WIDTH_UNITS.toDouble() * dpi / 1200.0).roundToInt()
        fun lineBytes(dpi: Int, mode: ScanMode): Int = pixels(dpi) * mode.bytesPerPixel
    }

    private var scanDpiY = 300
    private var autosize = true
    private var continuous = false
    private var dropout = Dropout.NONE
    private var enhance = Dropout.NONE

    // ---- identity --------------------------------------------------------

    data class Identity(val vendor: String, val product: String, val revision: String)

    fun inquiry(): Identity {
        val d = t.cmd(byteArrayOf(INQUIRY.toByte(), 0, 0, 0, 0x24) + ByteArray(7), readLen = 0x24)
        fun s(a: Int, b: Int) = String(d, a, b - a, Charsets.US_ASCII).trim()
        return if (d.size >= 0x24) Identity(s(8, 16), s(16, 32), s(32, 36))
        else Identity("CANON", "P-208", "")
    }

    /** The paper sensor: true when a sheet is in the feeder. */
    fun paperPresent(): Boolean = try {
        val d = t.cmd(byteArrayOf(READ10.toByte(), 0, 0x8b.toByte(), 0, 0, 0, 0, 0, 1) + ByteArray(3), readLen = 1)
        d.isNotEmpty() && (d[0].toInt() and 0x01) != 0
    } catch (_: Exception) { false }

    // ---- programming the device -----------------------------------------

    private fun modeSelectUnits() {
        val body = ByteArray(12)
        body[4] = 0x03; body[5] = 0x06
        body[8] = (UNITS_PER_INCH ushr 8).toByte(); body[9] = UNITS_PER_INCH.toByte()
        t.cmd(byteArrayOf(MODE_SELECT.toByte(), 0x10, 0, 0, 0x0c) + ByteArray(7), dataOut = body)
    }

    /**
     * One window descriptor per side.
     *
     * ulY is negative: the window opens before the paper's leading edge, so the
     * scan is already running when the sheet arrives.
     */
    private fun setWindow(side: Int, dpiX: Int, dpiY: Int, mode: ScanMode, ulY: Int = -468) {
        val d = ByteArray(52)
        fun be16(off: Int, v: Int) { d[off] = (v ushr 8).toByte(); d[off + 1] = v.toByte() }
        fun be32(off: Int, v: Int) {
            d[off] = (v ushr 24).toByte(); d[off + 1] = (v ushr 16).toByte()
            d[off + 2] = (v ushr 8).toByte(); d[off + 3] = v.toByte()
        }
        be16(6, 44)                    // window descriptor length
        d[8] = side.toByte()           // window identifier
        be16(10, dpiX); be16(12, dpiY)
        be32(14, 0); be32(18, ulY)
        be32(22, WIN_WIDTH_UNITS); be32(26, WIN_LENGTH_UNITS)
        d[30] = 0x80.toByte()          // brightness, neutral
        d[31] = 0x80.toByte()          // threshold, lineart only
        d[32] = 0x80.toByte()          // contrast
        d[33] = mode.wire.toByte()
        d[34] = 8                      // bits per sample
        d[37] = 0x10
        d[50] = 0x88.toByte()
        t.cmd(byteArrayOf(SET_WINDOW.toByte(), 0, 0, 0, 0, 0, 0, 0, d.size.toByte()) + ByteArray(3), dataOut = d)
    }

    private fun setScanMode(page: Int, pageData: ByteArray) {
        val body = byteArrayOf(0x13, 0, 0, 0, page.toByte(), pageData.size.toByte()) + pageData
        t.cmd(byteArrayOf(SET_SCAN_MODE.toByte(), 0x10, 0, 0, body.size.toByte()) + ByteArray(7), dataOut = body)
    }

    private fun bufferPage(duplex: Boolean, b4: Int): ByteArray {
        val p = ByteArray(14)
        p[0] = if (duplex) 0x02 else 0x00     // bit 1 = duplex
        p[1] = 0x01
        p[4] = b4.toByte()
        return p
    }

    /**
     * The page 0x32 feed byte.
     *
     * 0x40 makes the device stream the ENTIRE stack as one unbroken image: it
     * never marks a sheet boundary. Measured with the bit set, a single sheet
     * ran past 26 in and was still going; with it clear the device ended the
     * sheet by itself at 12.98 in. Off unless the caller wants one long image.
     */
    private fun feedFlags(base: Int = 0): Int {
        var f = base
        f = if (continuous) f or FEED_CONTINUOUS else f and FEED_CONTINUOUS.inv()
        f = if (autosize) f or FEED_AUTOSIZE else f and FEED_AUTOSIZE.inv()
        return f
    }

    /**
     * Page 0x36: colour drop-out and its mutually exclusive partner, per side.
     * Setting both for the same side is refused by the device.
     */
    private fun dropoutPage(front: Int, back: Int, eFront: Int, eBack: Int): ByteArray {
        require(!((front != 0 && eFront != 0) || (back != 0 && eBack != 0))) {
            "drop-out and enhance cannot both be set for one side"
        }
        val p = ByteArray(14)
        p[5] = front.toByte(); p[6] = back.toByte()
        p[7] = eFront.toByte(); p[8] = eBack.toByte()
        return p
    }

    /**
     * Program the device for a scan, in the order it insists on.
     *
     * [full] is the CALIBRATION preparation. A page scan uses the short form -
     * windows, then the buffer page with 0x60. Applying the calibration form
     * before a page changes the stream the device produces.
     */
    private fun prepare(dpi: Int, duplex: Boolean, full: Boolean, mode: ScanMode) {
        val sides = if (duplex) intArrayOf(SIDE_FRONT, SIDE_BACK) else intArrayOf(SIDE_FRONT)
        for (s in sides) setWindow(s, dpi, scanDpiY, mode)
        setScanMode(PAGE_BUFFER, bufferPage(duplex, feedFlags(0x60)))
        if (!full) return

        setScanMode(PAGE_30, ByteArray(14))
        // Forced to zero for a colour scan: drop-out and colour enhance only
        // mean anything on a gray or binary page, and setting them on an RGB
        // window makes the page come out saturated and uncroppable.
        val (f, b, ef, eb) = if (mode == ScanMode.COLOR) {
            listOf(0, 0, 0, 0)
        } else {
            listOf(dropout.value, if (duplex) dropout.value else 0,
                   enhance.value, if (duplex) enhance.value else 0)
        }.let { Quad(it[0], it[1], it[2], it[3]) }
        setScanMode(PAGE_36, dropoutPage(f, b, ef, eb))
        // 0x40 here, not 0x60: the calibration form of this page is a distinct
        // value and the autosize bit does not belong in it. Folding it in makes
        // the device refuse SCAN with ASC 26.
        setScanMode(PAGE_BUFFER, bufferPage(duplex, 0x40))
        for (s in sides) setWindow(s, dpi, scanDpiY, mode)
    }

    /**
     * A growable byte sink whose buffer can be read in place.
     *
     * ByteArrayOutputStream.toByteArray() duplicates the whole page - 55 MB for
     * a colour duplex sheet - purely to hand it over. At eight sheets that is
     * what exhausts the heap.
     */
    private class Sink(initial: Int = 1 shl 20) {
        var buf = ByteArray(initial); private set
        var size = 0; private set
        fun write(b: ByteArray, n: Int = b.size) {
            if (size + n > buf.size) {
                var cap = buf.size
                while (cap < size + n) cap = cap shl 1
                buf = buf.copyOf(cap)
            }
            b.copyInto(buf, size, 0, n)
            size += n
        }
        fun clear() { buf = ByteArray(0); size = 0 }
    }

    private data class Quad(val a: Int, val b: Int, val c: Int, val d: Int)
    private operator fun Quad.component1() = a
    private operator fun Quad.component2() = b
    private operator fun Quad.component3() = c
    private operator fun Quad.component4() = d

    private fun writeAfe(gain: IntArray, offset: IntArray, exposure: Array<IntArray>) {
        val body = ByteArray(40)
        for (side in 0..1) {
            val b = side * 20
            val g = (gain[side] and 0xff).toByte()
            val o = (offset[side] and 0xff).toByte()
            body[b] = g; body[b + 1] = g; body[b + 2] = g
            body[b + 4] = o; body[b + 5] = o; body[b + 6] = o
            for (c in 0..2) {
                val e = exposure[side][c] and 0xffff
                body[b + 8 + c * 2] = (e ushr 8).toByte()
                body[b + 9 + c * 2] = e.toByte()
            }
        }
        t.cmd(byteArrayOf(ADJUST_DATA.toByte(), 0, 0, 0, 0, 0x03, 0, 0, body.size.toByte()) + ByteArray(3), dataOut = body)
    }

    private fun scanStart(windows: IntArray) {
        val ids = ByteArray(windows.size) { windows[it].toByte() }
        t.cmd(byteArrayOf(SCAN.toByte(), 0, 0, 0, ids.size.toByte()) + ByteArray(7), dataOut = ids)
    }

    private fun readImage(length: Int): ByteArray =
        t.cmd(
            byteArrayOf(
                READ10.toByte(), 0, 0, 0, 0, 0,
                ((length ushr 16) and 0xff).toByte(),
                ((length ushr 8) and 0xff).toByte(),
                (length and 0xff).toByte()
            ) + ByteArray(3), readLen = length
        )

    private fun waitReady(timeoutMs: Long = 20000): Boolean {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (true) {
            try { t.testUnitReady(); return true } catch (_: Exception) { t.clearHalts() }
            if (System.currentTimeMillis() >= deadline) return false
            Thread.sleep(150)
        }
    }

    /**
     * Acquire one internal reference strip.
     *
     * One fixed-size block, then release with OBJECT POSITION 0. Reading to
     * end-of-data instead pulls 61 MB and leaves the device unable to start
     * another scan.
     */
    private fun calibrationScan(window: Int, dpi: Int, sides: Int, mode: ScanMode): ByteArray {
        val n = CAL_STREAM_LINES * lineBytes(dpi, mode)
        scanStart(IntArray(sides) { window })
        return try {
            var attempt = 0
            var out = ByteArray(0)
            while (true) {
                try {
                    out = readImage(n); break
                } catch (e: ShortRead) {
                    out = e.data    // still valid signal; the servo uses percentiles
                    break
                } catch (e: PageComplete) {
                    out = e.data; break
                } catch (e: NotReady) {
                    if (++attempt >= 30) break
                    Thread.sleep(150)
                }
            }
            out
        } finally {
            try { t.objectPosition(0) } catch (_: Exception) { t.clearHalts() }
        }
    }

    /**
     * Derive the analogue operating point instead of assuming one.
     *
     * Alternates dark and white references, servoing offset then gain, until
     * both sit on target. Exposure is per channel, so it is tuned afterwards -
     * it is what balances the channels against each other.
     */
    private fun calibrateAfe(dpi: Int, onProgress: (String) -> Unit) {
        val gain = intArrayOf(1, 1)
        val offset = intArrayOf(128, 128)
        val exposure = arrayOf(intArrayOf(0x0725, 0x0b7d, 0x0a16), intArrayOf(0x0725, 0x0b7d, 0x0a16))

        for (pass in 0 until 8) {
            writeAfe(gain, offset, exposure)
            val dark = calibrationScan(WIN_DARK, dpi, 1, ScanMode.COLOR)
            val min12 = if (dark.isNotEmpty()) Afe.percentileU8(dark, 5.0) * 16 else 0
            offset[0] = Afe.offsetStep(min12, gain[0], offset[0]); offset[1] = offset[0]

            writeAfe(gain, offset, exposure)
            val white = calibrationScan(WIN_WHITE, dpi, 1, ScanMode.COLOR)
            val max12 = if (white.isNotEmpty()) Afe.percentileU8(white, 95.0) * 16 else 0
            gain[0] = Afe.gainStep(max12, gain[0]); gain[1] = gain[0]

            onProgress("Calibrating… pass ${pass + 1}")
            if (abs(min12 - Afe.DARK_TARGET) <= 32 && abs(max12 - Afe.WHITE_TARGET) <= 128) break
        }

        // Balance the channels against each other with per-channel exposure.
        val line = lineBytes(dpi, ScanMode.COLOR)
        val w = line / 3
        for (round in 0 until 4) {
            val white = calibrationScan(WIN_WHITE, dpi, 1, ScanMode.COLOR)
            val n = white.size / line
            if (n == 0) break
            var worst = 0
            for (c in 0..2) {
                val plane = ByteArray(n * w)
                for (y in 0 until n) System.arraycopy(white, y * line + c * w, plane, y * w, w)
                var meas = Afe.percentileU8(plane, 95.0) * 16
                if (meas < 1) meas = 1
                worst = maxOf(worst, abs(meas - Afe.WHITE_TARGET))
                val e = Afe.exposureStep(meas, exposure[0][c])
                exposure[0][c] = e; exposure[1][c] = e
            }
            writeAfe(gain, offset, exposure)
            if (worst <= 160) break
        }
        writeAfe(gain, offset, exposure)
    }

    // ---- references and shading -----------------------------------------

    /** Column means of the dark and white strips, per side. */
    private class Refs(val dark: Array<FloatArray?>, val white: Array<FloatArray?>)

    private fun columnMeans(data: ByteArray, line: Int): FloatArray {
        val n = data.size / line
        val acc = LongArray(line)
        for (y in 0 until n) {
            val base = y * line
            for (x in 0 until line) acc[x] += (data[base + x].toInt() and 0xff).toLong()
        }
        return FloatArray(line) { if (n > 0) acc[it].toFloat() / n else 0f }
    }

    private fun deinterleave(raw: ByteArray): Pair<ByteArray, ByteArray> {
        val half = raw.size / 2
        val front = ByteArray(half)
        val back = ByteArray(half)
        var i = 0
        for (k in 0 until half) { front[k] = raw[i]; back[k] = raw[i + 1]; i += 2 }
        return Pair(front, back)
    }

    private fun referencesFor(dpi: Int, duplex: Boolean, mode: ScanMode): Refs {
        val line = lineBytes(dpi, mode)
        val dark = arrayOfNulls<FloatArray>(2)
        val white = arrayOfNulls<FloatArray>(2)
        prepare(dpi, duplex, full = true, mode = mode)

        for ((kind, window) in listOf("dark" to WIN_DARK, "white" to WIN_WHITE)) {
            val raw = calibrationScan(window, dpi, if (duplex) 2 else 1, mode)
            val halves: List<ByteArray> = if (duplex) {
                // The strips arrive byte-interleaved exactly like a page. The
                // back is NOT mirrored here: shading is applied to the line as
                // it arrives, before mirroring.
                deinterleave(raw).toList()
            } else {
                listOf(raw)
            }
            halves.forEachIndexed { side, h ->
                val m = columnMeans(h, line)
                if (kind == "dark") dark[side] = m else white[side] = m
            }
        }
        return Refs(dark, white)
    }

    // ---- decoding --------------------------------------------------------

    /**
     * Shade, mirror and de-planarise one side straight into a Bitmap.
     *
     * Done in one pass on purpose: a full-page intermediate would be three
     * large allocations before the Bitmap even exists.
     */
    private fun toBitmap(data: ByteArray, avail: Int, sideOff: Int, stride: Int,
                         dark: FloatArray, white: FloatArray,
                         dpi: Int, mode: ScanMode, mirror: Boolean, target: Float = 230f): Bitmap? {
        val line = lineBytes(dpi, mode)
        // Bytes belonging to this side, without extracting them: in duplex the
        // two sides alternate byte by byte, so side s is every stride-th byte
        // starting at sideOff.
        val mine = (avail - sideOff + stride - 1) / stride
        val h = mine / line
        if (h <= 0) return null
        val w = pixels(dpi)

        val scale = FloatArray(line) { target / maxOf(white[it] - dark[it], 1f) }
        val row = IntArray(w)
        val bmp = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888)

        fun shade(v: Int, i: Int): Int {
            val f = ((v and 0xff) - dark[i]) * scale[i]
            return if (f <= 0f) 0 else if (f >= 255f) 255 else f.toInt()
        }

        // index of byte i of this side within the shared buffer
        fun idx(i: Int) = sideOff + i * stride

        for (y in 0 until h) {
            val base = y * line
            if (mode == ScanMode.COLOR) {
                for (x in 0 until w) {
                    val sx = if (mirror) w - 1 - x else x
                    val r = shade(data[idx(base + sx)].toInt(), sx)
                    val g = shade(data[idx(base + w + sx)].toInt(), w + sx)
                    val b = shade(data[idx(base + 2 * w + sx)].toInt(), 2 * w + sx)
                    row[x] = (0xff shl 24) or (r shl 16) or (g shl 8) or b
                }
            } else {
                for (x in 0 until w) {
                    val sx = if (mirror) w - 1 - x else x
                    val v = shade(data[idx(base + sx)].toInt(), sx)
                    row[x] = (0xff shl 24) or (v shl 16) or (v shl 8) or v
                }
            }
            bmp.setPixels(row, 0, w, 0, y, w, 1)
        }
        return bmp
    }

    private fun rescale(bmp: Bitmap, fx: Float, fy: Float): Bitmap {
        if (abs(fx - 1f) < 1e-6f && abs(fy - 1f) < 1e-6f) return bmp
        val ow = maxOf(1, (bmp.width * fx).roundToInt())
        val oh = maxOf(1, (bmp.height * fy).roundToInt())
        val out = Bitmap.createScaledBitmap(bmp, ow, oh, true)
        if (out !== bmp) bmp.recycle()
        return out
    }

    // ---- the scan --------------------------------------------------------

    /** What the caller sees while a stack is going through. */
    interface Listener {
        fun onStatus(text: String)
        fun onPage(index: Int, side: Int, bitmap: Bitmap)
        fun onProgressBytes(bytes: Long)
    }

    /**
     * Feed sheets until the tray empties.
     *
     * Calibration runs once for the whole batch - the operating point does not
     * change between pages, and re-deriving it would add seconds to every
     * sheet. One SCAN opens a session for the whole stack; sheets are separated
     * inside it by OBJECT POSITION, because the device rejects a second SCAN
     * and rejects SET WINDOW once a session is open.
     */
    fun scanBatch(settings: ScanSettings, maxPages: Int = 100, listener: Listener) {
        dropout = settings.dropout
        enhance = settings.enhance
        autosize = settings.autosize
        continuous = settings.continuous

        // Drop-out and enhance are reductions to a single channel, so they only
        // exist for a gray page; asking for colour as well is a contradiction.
        var mode = settings.mode
        val reducing = dropout != Dropout.NONE || enhance != Dropout.NONE
        if (reducing && mode == ScanMode.COLOR) mode = ScanMode.GRAY
        // With drop-out on the device acquires in colour - it has to, or there
        // is no colour to drop - but sends one channel per pixel.
        val outMode = if (reducing) ScanMode.GRAY else mode

        val want = settings.dpi
        val plan = SCAN_PLAN[want] ?: Pair(want, want)
        val dpi = plan.first
        scanDpiY = plan.second
        val fx = want.toFloat() / dpi
        val fy = want.toFloat() / scanDpiY

        Diag.d("scanBatch: want=${settings.dpi} scan=${dpi}x$scanDpiY mode=$mode out=$outMode duplex=${settings.duplex} autosize=$autosize")
        modeSelectUnits()
        Diag.d("mode select units ok")

        listener.onStatus("Warming up…")
        // Drop-out off while the operating point is derived.
        val held = dropout; dropout = Dropout.NONE
        try {
            prepare(dpi, duplex = false, full = true, mode = ScanMode.COLOR)
            calibrateAfe(dpi) { listener.onStatus(it) }
        } finally {
            dropout = held
        }

        Diag.d("AFE calibration done")
        listener.onStatus("Measuring references…")
        val refs = referencesFor(dpi, settings.duplex, outMode)
        Diag.d("references done: line=${lineBytes(dpi, outMode)} bytes")

        val sides = if (settings.duplex) intArrayOf(SIDE_FRONT, SIDE_BACK) else intArrayOf(SIDE_FRONT)
        // In continuous mode one SCAN covers the whole stack. Otherwise each
        // sheet gets its own session, which is what gives the device somewhere
        // to end a page - re-issuing SCAN and SET WINDOW per sheet is accepted.
        if (continuous) {
            prepare(dpi, settings.duplex, full = false, mode = mode)
            waitReady()
            scanStart(sides)
            t.requestSense()
            Diag.d("continuous session open, sides=${sides.size}")
        }

        val line = lineBytes(dpi, outMode)
        var pageIndex = 0
        var totalBytes = 0L

        for (page in 0 until maxPages) {
            listener.onStatus(if (pageIndex == 0) "Feeding…" else "Sheet ${pageIndex + 1}…")

            if (!continuous) {
                try {
                    prepare(dpi, settings.duplex, full = false, mode = mode)
                    waitReady()
                    scanStart(sides)
                    t.requestSense()
                } catch (e: NoMedium) {
                    Diag.d("stack finished after $pageIndex page(s)"); return
                } catch (e: ScannerError) {
                    // An empty tray does not always answer politely. On the
                    // desktop SCAN reports "no documents"; here the same
                    // condition arrives as a bare transport failure, because
                    // the device stops answering and even the sense read times
                    // out. Anything that stops a NEW sheet's session, once the
                    // stack has already produced pages, is the stack ending -
                    // reporting it as a failure paints an error over a scan
                    // that worked.
                    val m = e.message ?: ""
                    val asc = t.lastSense.second
                    val done = m.contains("no documents") || m.contains("medium") ||
                        asc == 0x3a || asc == 0x2c || pageIndex > 0
                    runCatching { t.clearHalts() }
                    if (done) {
                        Diag.d("stack finished after $pageIndex page(s): $m")
                        return
                    }
                    throw e
                }
            }

            if (!feedNext()) { Diag.d("feed: tray empty after $pageIndex page(s)"); return }
            Diag.d("feed: sheet ${pageIndex + 1} accepted")

            val buf = Sink()
            var empty = 0
            var retried = false
            // No length backstop: the device ends each sheet itself now that
            // continuous feed is off. In continuous mode the stack is meant to
            // run long, so a limit would be wrong there too.
            loop@ while (true) {
                val d: ByteArray
                try {
                    d = readImage(READ_CHUNK)
                } catch (e: PageComplete) {
                    buf.write(e.data)                       // end of this sheet
                    break@loop
                } catch (e: ShortRead) {
                    // Not the end - the paper is still moving and the buffer
                    // had not caught up. Keep what came and ask again.
                    if (e.data.isEmpty()) {
                        if (++empty >= 16) { Diag.d("page: 16 empty reads, stopping"); break@loop }
                        waitReady(2000)
                        continue@loop
                    }
                    empty = 0
                    buf.write(e.data)
                    totalBytes += e.data.size
                    listener.onProgressBytes(totalBytes)
                    continue@loop
                } catch (e: NotReady) {
                    // The device has nothing buffered yet. libusb would sit on
                    // the NAKs; here we wait ourselves. The paper moves at a
                    // fixed rate, so this costs nothing but patience.
                    if (++empty >= 60) { Diag.d("page: device quiet 60x, stopping"); break@loop }
                    Thread.sleep(150)
                    continue@loop
                } catch (e: NoMedium) {
                    if (buf.size > 0) break@loop
                    return
                } catch (e: ScannerError) {
                    // A bare status 2 is how this sheet ends. driver.py checks
                    // for exactly this and breaks; leaving it out means nothing
                    // ever marks a sheet boundary and the read runs on into the
                    // next sheet, which is what put every page out of phase.
                    if (e.message?.contains("0x00000002") == true) {
                        Diag.d("page: end of sheet (status 2)")
                        break@loop
                    }
                    val asc = t.lastSense.second
                    if (asc == 0x3a || asc == 0x2c) {
                        if (buf.size > 0) break@loop
                        // A sheet WAS fed to get here, so an immediate 2C on an
                        // empty read can also mean the page has not reached the
                        // sensor yet. Giving up on the first one ends the batch
                        // while the device carries on feeding.
                        if (asc == 0x2c && !retried) {
                            retried = true
                            runCatching { waitReady() }
                            continue@loop
                        }
                        return
                    }
                    throw e
                }
                if (d.isEmpty()) break@loop
                empty = 0
                buf.write(d)
                totalBytes += d.size
                listener.onProgressBytes(totalBytes)
            }

            Diag.d("sheet ${pageIndex + 1}: ${buf.size} bytes read")
            if (buf.size == 0) { Diag.d("sheet produced no data - stopping"); return }

            val stride = if (settings.duplex) 2 else 1
            val nSides = if (settings.duplex) 2 else 1
            for (sideIdx in 0 until nSides) {
                val d = refs.dark[sideIdx] ?: refs.dark[0]!!
                val w = refs.white[sideIdx] ?: refs.white[0]!!
                var bmp = try {
                    toBitmap(
                        buf.buf, buf.size, sideIdx, stride, d, w, dpi, outMode,
                        mirror = sideIdx == SIDE_BACK
                    )
                } catch (e: OutOfMemoryError) {
                    Diag.e("out of memory decoding side $sideIdx", e)
                    null
                }
                if (bmp != null) {
                    bmp = rescale(bmp, fx, fy)
                    listener.onPage(pageIndex, sideIdx, bmp)
                }
            }
            buf.clear()          // release before the next sheet is fed
            pageIndex++
            if (!continuous) {
                // Close this sheet's session before opening the next one.
                runCatching { t.objectPosition(0) }.onFailure { t.clearHalts() }
            }
        }
    }

    /**
     * Feed the next sheet inside an open session.
     *
     * An empty tray does not answer with a clean sense code - the feed fails at
     * the USB level - so the pipes are cleared and the sense read afterwards to
     * tell "out of paper" from a real fault.
     */
    private fun feedNext(): Boolean {
        return try {
            t.objectPosition(1)
            true
        } catch (_: NoMedium) {
            false
        } catch (_: ScannerError) {
            val cond = t.requestSense() ?: return false
            !(cond.contains("no documents") || cond.contains("medium"))
        }
    }

    /** Abort a running feed (opcode 0xd8). */
    fun stopBatch() {
        runCatching { t.cmd(byteArrayOf(0xd8.toByte()) + ByteArray(11)) }
    }
}
