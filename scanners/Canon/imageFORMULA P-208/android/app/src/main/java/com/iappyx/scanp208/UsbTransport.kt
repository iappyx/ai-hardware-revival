package com.iappyx.scanp208

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.usb.UsbConstants
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbDeviceConnection
import android.hardware.usb.UsbEndpoint
import android.hardware.usb.UsbInterface
import android.hardware.usb.UsbManager
import android.os.Build
import kotlin.coroutines.resume
import kotlin.coroutines.suspendCoroutine

/** The device reported a condition rather than data. */
open class ScannerError(message: String) : RuntimeException(message)

/** No scanner on the bus, or it is in the wrong mode. */
class NotFound(message: String) : ScannerError(message)

/** Someone else holds the device. */
class Busy(message: String) : ScannerError(message)

/**
 * The device sent less than was asked for, and that is NOT an error.
 *
 * Status 2 with sense key 0 means a short transfer: the residue arrives in the
 * sense INFORMATION field. The partial buffer has to be kept and the read
 * reissued; treating it as the end of the page stops mid-sheet.
 */
class ShortRead(val data: ByteArray, val residue: Int) :
    ScannerError("short transfer, $residue bytes short")

/** The sheet is genuinely finished (ASC 0x81/0x01). */
class PageComplete(val data: ByteArray) : ScannerError("page complete")

/** The feeder is empty (ASC 0x3A). */
class NoMedium(message: String) : ScannerError(message)

/**
 * The device has nothing to send yet and stalled the endpoint instead of
 * making us wait.
 *
 * libusb simply blocks on the NAKs until data appears, so driver.py never sees
 * this; Android reports it as a failed transfer. It means "ask again shortly",
 * not "the scan is over" - the paper is still moving.
 */
class NotReady(message: String) : ScannerError(message)

/**
 * Raw USB access to the Canon imageFORMULA P-208 (1083:164c).
 *
 * This is the only Android-specific layer. The scanner speaks a SCSI command
 * set carried on two bulk endpoints, with a 12-byte header in front of every
 * CDB, and answers each command with a 4-byte status read.
 */
class UsbTransport(private val context: Context) {

    companion object {
        const val VID = 0x1083
        const val PID_SCANNER = 0x164C     // Auto Start OFF
        const val PID_STORAGE = 0x164E     // Auto Start ON - a USB stick, not a scanner

        private const val ACTION_PERMISSION = "com.iappyx.scanp208.USB_PERMISSION"
        private const val TIMEOUT_MS = 30000
        private const val PROBE_MS = 3000

        private val HDR_SIG = byteArrayOf(0x00, 0x01, 0x90.toByte(), 0x00)
        private val DATA_SIG = byteArrayOf(0x00, 0x02, 0xb0.toByte(), 0x00)

        /**
         * Android's usbfs refuses a single bulk transfer larger than 16 KB on
         * most kernels - it fails outright rather than transferring less - so a
         * data phase is pulled in slices of that size. This is a transport
         * detail only: the device still sees one READ for the full length, and
         * a short transfer is reported by the sense residue, not by the byte
         * count. 16384 is a whole number of 512-byte packets.
         */
        private const val SLICE = 16 * 1024
    }

    private val manager = context.getSystemService(Context.USB_SERVICE) as UsbManager

    private var device: UsbDevice? = null
    private var connection: UsbDeviceConnection? = null
    private var iface: UsbInterface? = null
    private var epIn: UsbEndpoint? = null
    private var epOut: UsbEndpoint? = null

    /**
     * Android will not submit a bulk IN transfer whose buffer is smaller than
     * the endpoint's max packet size - it fails immediately with -1 rather
     * than returning a short packet. Every command here ends with a 4-byte
     * status read, so all short reads go into a full-packet buffer and are
     * trimmed afterwards.
     */
    private var packet: Int = 512

    /** Timeout for the current transfer; lowered during the opening handshake. */
    private var timeout: Int = TIMEOUT_MS

    /** Sense from the last command: (key, asc, ascq). */
    var lastSense: Triple<Int, Int, Int> = Triple(0, 0, 0); private set
    var lastInfo: Int = 0; private set
    private var inSense = false

    val isOpen: Boolean get() = connection != null

    fun findDevice(): UsbDevice? =
        manager.deviceList.values.firstOrNull { it.vendorId == VID && it.productId == PID_SCANNER }

    fun findStorageMode(): UsbDevice? =
        manager.deviceList.values.firstOrNull { it.vendorId == VID && it.productId == PID_STORAGE }

    /** True when the scanner is plugged in but the Auto Start switch is ON. */
    fun isInStorageMode(): Boolean = findDevice() == null && findStorageMode() != null

    suspend fun requestPermission(dev: UsbDevice): Boolean {
        if (manager.hasPermission(dev)) return true
        return suspendCoroutine { cont ->
            val receiver = object : BroadcastReceiver() {
                override fun onReceive(c: Context, intent: Intent) {
                    if (intent.action != ACTION_PERMISSION) return
                    context.unregisterReceiver(this)
                    cont.resume(intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false))
                }
            }
            val filter = IntentFilter(ACTION_PERMISSION)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                context.registerReceiver(receiver, filter, Context.RECEIVER_NOT_EXPORTED)
            } else {
                @Suppress("UnspecifiedRegisterReceiverFlag")
                context.registerReceiver(receiver, filter)
            }
            val flags = PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE
            val pi = PendingIntent.getBroadcast(
                context, 0, Intent(ACTION_PERMISSION).setPackage(context.packageName), flags
            )
            manager.requestPermission(dev, pi)
        }
    }

    fun open() {
        if (isOpen) return
        val dev = findDevice() ?: if (findStorageMode() != null) {
            throw NotFound(
                "The P-208 is in mass-storage mode. Set the Auto Start switch on " +
                    "the back of the scanner to OFF and re-plug the cable."
            )
        } else {
            throw NotFound("No P-208 found. Check the cable and try again.")
        }
        if (!manager.hasPermission(dev)) throw Busy("USB permission was not granted.")

        Diag.d("open: found %04x:%04x %s".format(dev.vendorId, dev.productId, dev.deviceName))
        val conn = manager.openDevice(dev) ?: throw Busy("Could not open the scanner.")
        // pyusb issues SET_CONFIGURATION before claiming and Android does not
        // do it for us. On a device left half-configured by a dead session,
        // this is what makes the endpoints answer again.
        runCatching {
            val ok = conn.setConfiguration(dev.getConfiguration(0))
            Diag.d("open: setConfiguration -> $ok")
        }.onFailure { Diag.d("open: setConfiguration threw ${it.message}") }
        val intf = dev.getInterface(0)
        if (!conn.claimInterface(intf, true)) {
            conn.close()
            throw Busy("Could not claim the scanner interface.")
        }
        for (i in 0 until intf.endpointCount) {
            val ep = intf.getEndpoint(i)
            if (ep.type != UsbConstants.USB_ENDPOINT_XFER_BULK) continue
            if (ep.direction == UsbConstants.USB_DIR_IN) epIn = ep else epOut = ep
        }
        if (epIn == null || epOut == null) {
            conn.releaseInterface(intf); conn.close()
            throw ScannerError("Expected one bulk IN and one bulk OUT endpoint.")
        }

        packet = maxOf(epIn!!.maxPacketSize, 64)
        device = dev; connection = conn; iface = intf
        Diag.d("open: claimed interface, epIn=0x%02x epOut=0x%02x packet=%d"
            .format(epIn!!.address, epOut!!.address, packet))

        // A session that died mid-transfer leaves the pipes halted, and the
        // device stalls everything until they are cleared. Clear them, then
        // prove the link with a command that has no data phase.
        // Probe with a short timeout: a wedged device should be reported in
        // seconds, not after a minute of blocked transfers.
        timeout = PROBE_MS
        try {
            var alive = false
            for (attempt in 1..3) {
                clearHalts()
                try {
                    testUnitReady()
                    Diag.d("open: TEST UNIT READY ok (attempt $attempt)")
                    alive = true
                    break
                } catch (e: Exception) {
                    Diag.d("open: TEST UNIT READY attempt $attempt failed: ${e.message}")
                    if (attempt == 2) {
                        // Re-claim once: a stale claim from a dead session
                        // behaves exactly like an unresponsive device.
                        runCatching {
                            conn.releaseInterface(intf)
                            conn.claimInterface(intf, true)
                            Diag.d("open: re-claimed interface")
                        }
                    }
                    Thread.sleep(250)
                }
            }
            if (!alive) {
                throw ScannerError(
                    "The scanner is not answering. Unplug it, plug it back in, and try again."
                )
            }
        } finally {
            timeout = TIMEOUT_MS
        }
        // A scan left running by a previous session blocks the next one.
        try { objectPosition(0) } catch (_: Exception) { clearHalts() }
        try { cmd(byteArrayOf(0x16) + ByteArray(11)) } catch (_: Exception) { }   // RESERVE UNIT
    }

    fun close() {
        val conn = connection ?: return
        try { cmd(byteArrayOf(0x17) + ByteArray(11)) } catch (_: Exception) { }   // RELEASE UNIT
        try { iface?.let { conn.releaseInterface(it) } } catch (_: Exception) { }
        try { conn.close() } catch (_: Exception) { }
        connection = null; device = null; iface = null; epIn = null; epOut = null
    }

    /**
     * Clear a halted endpoint.
     *
     * Android exposes no clearHalt, so this issues the standard CLEAR_FEATURE
     * (ENDPOINT_HALT) request on endpoint 0 by hand. Without it a single
     * rejected command wedges every later one, which survives closing the app.
     */
    fun clearHalts() {
        val conn = connection ?: return
        for (ep in listOfNotNull(epIn, epOut)) {
            try {
                conn.controlTransfer(0x02, 0x01, 0x00, ep.address, null, 0, 2000)
            } catch (_: Exception) { }
        }
    }

    private fun wrap(cdb: ByteArray): ByteArray {
        val out = ByteArray(12 + cdb.size)
        val total = 8 + cdb.size
        out[0] = (total ushr 24).toByte(); out[1] = (total ushr 16).toByte()
        out[2] = (total ushr 8).toByte();  out[3] = total.toByte()
        HDR_SIG.copyInto(out, 4)
        cdb.copyInto(out, 12)
        return out
    }

    private fun writeAll(buf: ByteArray) {
        val conn = connection ?: throw ScannerError("not open")
        val out = epOut ?: throw ScannerError("not open")
        var off = 0
        while (off < buf.size) {
            val n = minOf(SLICE, buf.size - off)
            val sent = conn.bulkTransfer(out, buf, off, n, timeout)
            if (sent < 0) throw ScannerError("USB write failed (asked $n at $off)")
            off += sent
            if (sent == 0) break
        }
    }

    /** Read up to [length] bytes of a data phase; a short packet ends it. */
    private fun readData(length: Int): ByteArray {
        val conn = connection ?: throw ScannerError("not open")
        val ep = epIn ?: throw ScannerError("not open")
        val out = ByteArray(length)
        var got = 0
        var tmp: ByteArray? = null
        var slices = 0
        while (got < length) {
            val remaining = length - got
            val n: Int
            val asked: Int
            if (remaining >= SLICE) {
                asked = SLICE
                n = conn.bulkTransfer(ep, out, got, SLICE, timeout)
            } else {
                // Tail: ask for whole packets, since a partial request fails
                // outright on Android rather than returning less.
                asked = ((remaining + packet - 1) / packet) * packet
                val buf = tmp ?: ByteArray(SLICE).also { tmp = it }
                n = conn.bulkTransfer(ep, buf, 0, minOf(asked, buf.size), timeout)
                if (n > 0) buf.copyInto(out, got, 0, minOf(n, remaining))
            }
            slices++

            if (n < 0) {
                // The data phase ended earlier than the request. That is not
                // fatal here: the device reports how much it actually filled in
                // the sense residue, so clear the stall and let the status read
                // and sense decide what happened.
                Diag.d("read: transfer failed at $got of $length (slice $slices, asked $asked)")
                // Deliberately NOT clearing the halt here. CLEAR_FEATURE resets
                // the endpoint's data toggle, and if the endpoint was never
                // halted - the device simply had nothing to send yet - that
                // desynchronises host and device and every later transfer is
                // ignored. Only a real error path clears.
                if (got == 0) throw NotReady("no data ready")
                break
            }
            if (n == 0) { Diag.d("read: zero-length packet at $got of $length"); break }
            got += minOf(n, remaining)
            if (n < asked) {
                Diag.d("read: short packet $n/$asked at $got of $length")
                break
            }
        }
        return if (got == length) out else out.copyOfRange(0, got)
    }

    /** Read a small reply into a full-packet buffer, returning what arrived. */
    private fun readShort(want: Int): ByteArray {
        val conn = connection ?: throw ScannerError("not open")
        val ep = epIn ?: throw ScannerError("not open")
        val buf = ByteArray(maxOf(want, packet))
        val n = conn.bulkTransfer(ep, buf, buf.size, timeout)
        if (n < 0) throw ScannerError("bulk IN failed")
        return buf.copyOfRange(0, minOf(n, want))
    }

    private fun status(): Long {
        val saved = timeout
        timeout = PROBE_MS
        val st = try { readShort(4) } finally { timeout = saved }
        if (st.size != 4) throw ScannerError("short status (${st.size} bytes)")
        return ((st[0].toLong() and 0xff) shl 24) or ((st[1].toLong() and 0xff) shl 16) or
            ((st[2].toLong() and 0xff) shl 8) or (st[3].toLong() and 0xff)
    }

    /**
     * Issue one command. Returns the data phase, or an empty array if none.
     *
     * Status 2 covers three different things and telling them apart is the
     * whole game: page complete, feeder empty, or a short transfer that must
     * simply be continued.
     */
    fun cmd(cdb: ByteArray, dataOut: ByteArray? = null, readLen: Int = 0): ByteArray {
        var got = ByteArray(0)
        val st: Long
        try {
            writeAll(wrap(cdb))
            if (dataOut != null) {
                val total = 8 + dataOut.size
                val payload = ByteArray(12 + dataOut.size)
                payload[0] = (total ushr 24).toByte(); payload[1] = (total ushr 16).toByte()
                payload[2] = (total ushr 8).toByte();  payload[3] = total.toByte()
                DATA_SIG.copyInto(payload, 4)
                dataOut.copyInto(payload, 12)
                writeAll(payload)
            }
            if (readLen > 0) got = readData(readLen)
            st = status()
        } catch (e: NotReady) {
            throw e                     // expected mid-page; the caller retries
        } catch (e: ScannerError) {
            // A stalled data phase says nothing useful on its own - an open
            // cover and an empty feeder look identical. The device still holds
            // the sense, so fetch it and report the real condition.
            Diag.e("cmd 0x%02x transport failure: %s".format(cdb[0], e.message))
            clearHalts()
            val cond = if (!inSense) runCatching { clearCondition() }.getOrNull() else null
            if (cond != null) throw ScannerError("command 0x%02x failed: %s".format(cdb[0], cond))
            throw e
        }

        if (st != 0L) {
            val cond = clearCondition()
            val (key, asc, ascq) = lastSense
            if (key < 0) {
                Diag.e("cmd 0x%02x status 0x%08x but sense could not be read".format(cdb[0], st))
                throw ScannerError("status 0x%08x with no sense available".format(st))
            }
            if (asc == 0x81 && ascq == 0x01) throw PageComplete(got)
            if (asc == 0x3a) throw NoMedium(cond ?: "no documents in the feeder")
            if (key == 0) {
                // The residue is how much of the request was NOT filled, so
                // only the first (requested - residue) bytes are image. Keeping
                // the tail puts a block of noise into the page.
                val good = maxOf(0, got.size - lastInfo)
                throw ShortRead(got.copyOfRange(0, good), lastInfo)
            }
            Diag.e("cmd 0x%02x status 0x%08x key=%d asc=%02x/%02x %s".format(
                cdb[0], st, key, asc, ascq, cond ?: ""))
            throw ScannerError(
                "command 0x%02x returned status 0x%08x%s".format(
                    cdb[0], st, if (cond != null) ": $cond" else ""
                )
            )
        }
        return got
    }

    // ---- sense -----------------------------------------------------------

    private val conditions = mapOf(
        Triple(0x3, 0x80, 0x01) to "cover open",
        Triple(0x3, 0x80, 0x02) to "cover open or jam",
        Triple(0x3, 0x80, 0x03) to "no documents in the feeder",
        Triple(0x3, 0x80, 0x04) to "double feed detected",
        Triple(0x3, 0x80, 0x07) to "double feed detected",
        Triple(0x2, 0x00, 0x00) to "not ready",
        Triple(0x2, 0x3a, 0x00) to "no documents in the feeder",
        Triple(0x0, 0x3a, 0x00) to "no documents in the feeder",
        Triple(0x5, 0x2c, 0x00) to "command sequence error (usually an empty feeder)"
    )

    /**
     * Read the pending sense, which also clears the stall it causes.
     *
     * 14 bytes is enough to reach the residue. Byte 2 carries the key in its
     * low nibble and ILI in bit 5; bytes 3..6 are the INFORMATION field, which
     * holds the residue of a short transfer.
     */
    fun requestSense(): String? {
        if (inSense) return null
        inSense = true
        // Invalidate first. If this read fails, the caller must not be able to
        // mistake the PREVIOUS command's sense for this one's - a stale key 0
        // reads as "short transfer, keep going" and the read loop never ends.
        lastSense = Triple(-1, -1, -1)
        lastInfo = 0
        try {
            if (connection == null || epIn == null) return null
            val saved = timeout
            timeout = PROBE_MS
            try {
            writeAll(wrap(byteArrayOf(0x03, 0, 0, 0, 0x0e) + ByteArray(7)))
            val d = readShort(14)
            runCatching { status() }
            if (d.size < 14) return null
            val key = d[2].toInt() and 0x0f
            val asc = d[12].toInt() and 0xff
            val ascq = d[13].toInt() and 0xff
            lastSense = Triple(key, asc, ascq)
            lastInfo = ((d[3].toInt() and 0xff) shl 24) or ((d[4].toInt() and 0xff) shl 16) or
                ((d[5].toInt() and 0xff) shl 8) or (d[6].toInt() and 0xff)
            Diag.d("sense: key=%d asc=%02x/%02x info=%d".format(key, asc, ascq, lastInfo))
            return conditions[Triple(key, asc, ascq)]
                ?: "sense %d/%02x/%02x".format(key, asc, ascq)
            } finally { timeout = saved }
        } catch (_: Exception) {
            return null
        } finally {
            inSense = false
        }
    }

    private fun clearCondition(): String? = requestSense()

    // ---- the two commands open() needs -----------------------------------

    fun testUnitReady() { cmd(ByteArray(12)) }

    fun objectPosition(function: Int) {
        cmd(byteArrayOf(0x31, function.toByte()) + ByteArray(10))
    }
}
