package com.iappyx.scanp208

import android.graphics.Bitmap
import java.io.ByteArrayOutputStream
import java.io.OutputStream
import java.util.zip.Deflater

/**
 * A PDF writer that stores each page as a JPEG.
 *
 * Android's own PdfDocument draws into a page content stream, which ends up
 * zlib-compressed raw pixels - several megabytes for a scanned A4. A PDF image
 * is just a stream plus a /Filter saying how to decode it, and /DCTDecode means
 * the stream IS a JPEG, embedded verbatim. So the page is compressed once, as a
 * JPEG, and those exact bytes are dropped in: no re-encoding, no second loss,
 * and a fraction of the size.
 *
 * /FlateDecode is used when the caller asks for no loss at all.
 */
object PdfWriter {

    /** Named settings, matching the desktop driver so both produce like files. */
    val QUALITY = linkedMapOf(
        "max" to null,          // lossless
        "high" to 92,
        "balanced" to 80,
        "small" to 60
    )
    const val DEFAULT_QUALITY = "balanced"

    fun write(out: OutputStream, pages: List<Bitmap>, dpi: Int, quality: Int?) {
        val objs = ArrayList<ByteArray>()
        fun add(body: ByteArray): Int { objs.add(body); return objs.size }

        val catalog = add(ByteArray(0))     // placeholders until ids are known
        val pagesId = add(ByteArray(0))
        val kids = ArrayList<Int>()

        for (bmp in pages) {
            val w = bmp.width
            val h = bmp.height
            val body = ByteArrayOutputStream()

            val header: String
            if (quality == null) {
                val raw = rgbBytes(bmp)
                val packed = deflate(raw)
                header = "<</Type/XObject/Subtype/Image/Width $w/Height $h" +
                    "/ColorSpace/DeviceRGB/BitsPerComponent 8" +
                    "/Filter/FlateDecode/Length ${packed.size}>>stream\n"
                body.write(header.toByteArray(Charsets.ISO_8859_1))
                body.write(packed)
            } else {
                val jpeg = ByteArrayOutputStream()
                bmp.compress(Bitmap.CompressFormat.JPEG, quality, jpeg)
                val j = jpeg.toByteArray()
                header = "<</Type/XObject/Subtype/Image/Width $w/Height $h" +
                    "/ColorSpace/DeviceRGB/BitsPerComponent 8" +
                    "/Filter/DCTDecode/Length ${j.size}>>stream\n"
                body.write(header.toByteArray(Charsets.ISO_8859_1))
                body.write(j)
            }
            body.write("\nendstream".toByteArray(Charsets.ISO_8859_1))
            val imgId = add(body.toByteArray())

            val pw = w * 72.0 / dpi
            val ph = h * 72.0 / dpi
            val content = "q %.2f 0 0 %.2f 0 0 cm /Im0 Do Q".format(pw, ph)
            val contId = add(
                ("<</Length ${content.length}>>stream\n$content\nendstream")
                    .toByteArray(Charsets.ISO_8859_1)
            )
            val pageId = add(
                ("<</Type/Page/Parent $pagesId 0 R/MediaBox[0 0 %.2f %.2f]".format(pw, ph) +
                    "/Resources<</XObject<</Im0 $imgId 0 R>>>>/Contents $contId 0 R>>")
                    .toByteArray(Charsets.ISO_8859_1)
            )
            kids.add(pageId)
        }

        objs[catalog - 1] = "<</Type/Catalog/Pages $pagesId 0 R>>".toByteArray(Charsets.ISO_8859_1)
        objs[pagesId - 1] = ("<</Type/Pages/Count ${kids.size}/Kids[" +
            kids.joinToString(" ") { "$it 0 R" } + "]>>").toByteArray(Charsets.ISO_8859_1)

        val buf = ByteArrayOutputStream()
        buf.write("%PDF-1.4\n".toByteArray(Charsets.ISO_8859_1))
        val offsets = IntArray(objs.size)
        objs.forEachIndexed { i, body ->
            offsets[i] = buf.size()
            buf.write("${i + 1} 0 obj".toByteArray(Charsets.ISO_8859_1))
            buf.write(body)
            buf.write("endobj\n".toByteArray(Charsets.ISO_8859_1))
        }
        val xref = buf.size()
        buf.write("xref\n0 ${objs.size + 1}\n".toByteArray(Charsets.ISO_8859_1))
        buf.write("0000000000 65535 f \n".toByteArray(Charsets.ISO_8859_1))
        for (o in offsets) buf.write("%010d 00000 n \n".format(o).toByteArray(Charsets.ISO_8859_1))
        buf.write(("trailer<</Size ${objs.size + 1}/Root $catalog 0 R>>\n" +
            "startxref\n$xref\n%%EOF\n").toByteArray(Charsets.ISO_8859_1))

        out.write(buf.toByteArray())
        out.flush()
    }

    /** Packed RGB, three bytes per pixel, which is what /DeviceRGB expects. */
    private fun rgbBytes(bmp: Bitmap): ByteArray {
        val w = bmp.width
        val h = bmp.height
        val out = ByteArray(w * h * 3)
        val row = IntArray(w)
        var o = 0
        for (y in 0 until h) {
            bmp.getPixels(row, 0, w, 0, y, w, 1)
            for (x in 0 until w) {
                val p = row[x]
                out[o++] = (p shr 16).toByte()
                out[o++] = (p shr 8).toByte()
                out[o++] = p.toByte()
            }
        }
        return out
    }

    private fun deflate(data: ByteArray): ByteArray {
        val d = Deflater(Deflater.BEST_COMPRESSION)
        d.setInput(data); d.finish()
        val out = ByteArrayOutputStream(data.size / 4)
        val tmp = ByteArray(1 shl 16)
        while (!d.finished()) out.write(tmp, 0, d.deflate(tmp))
        d.end()
        return out.toByteArray()
    }
}
