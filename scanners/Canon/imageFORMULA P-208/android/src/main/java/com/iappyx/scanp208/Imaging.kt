package com.iappyx.scanp208

import android.graphics.Bitmap
import android.graphics.Matrix
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.min

/**
 * Everything that looks at pixels after the driver has produced a page.
 *
 * Detection runs on a downsampled copy - the sheet edge is a large feature and
 * finding it at full resolution costs a lot for no extra accuracy.
 */
object Imaging {

    private const val DETECT_MAX = 900     // longest edge used for edge finding

    private fun detectScale(bmp: Bitmap): Int {
        val longest = max(bmp.width, bmp.height)
        var s = 1
        while (longest / s > DETECT_MAX) s++
        return s
    }

    private class Luma(val v: FloatArray, val w: Int, val h: Int) {
        operator fun get(y: Int, x: Int) = v[y * w + x]
    }

    private fun luma(bmp: Bitmap, step: Int): Luma {
        val w = (bmp.width + step - 1) / step
        val h = (bmp.height + step - 1) / step
        val out = FloatArray(w * h)
        val row = IntArray(bmp.width)
        var yi = 0
        var y = 0
        while (y < bmp.height) {
            bmp.getPixels(row, 0, bmp.width, 0, y, bmp.width, 1)
            var xi = 0
            var x = 0
            while (x < bmp.width) {
                val p = row[x]
                out[yi * w + xi] =
                    (((p shr 16) and 0xff) + ((p shr 8) and 0xff) + (p and 0xff)) / 3f
                xi++; x += step
            }
            yi++; y += step
        }
        return Luma(out, w, h)
    }

    /**
     * The sheet's bounding box, in full-resolution pixels, or null if no sheet
     * stands out from the backing.
     *
     * The scan always opens before the paper arrives, so the first rows are
     * pure backing and make a reference profile. Subtracting it flattens the
     * lamp's own shading, which otherwise reads as content. The number of lead
     * rows is found by walking down until the row deviation jumps, rather than
     * fixed - a fixed count overran the lead-in and threw away real page.
     */
    fun sheetBounds(bmp: Bitmap): android.graphics.Rect? {
        val step = detectScale(bmp)
        val l = luma(bmp, step)
        if (l.h < 16 || l.w < 16) return null

        val rowDev = FloatArray(l.h)
        for (y in 0 until l.h) {
            var mean = 0f
            for (x in 0 until l.w) mean += l[y, x]
            mean /= l.w
            var s = 0f
            for (x in 0 until l.w) { val d = l[y, x] - mean; s += d * d }
            rowDev[y] = kotlin.math.sqrt(s / l.w)
        }

        val seed = rowDev.copyOfRange(0, min(8, l.h)).sorted()[min(8, l.h) / 2]
        val limit = max(3f * seed, seed + 0.5f)
        var leadN = min(8, l.h)
        for (r in leadN until min(l.h / 3, 400)) {
            if (rowDev[r] > limit) break
            leadN = r + 1
        }

        val profile = FloatArray(l.w)
        for (x in 0 until l.w) {
            var s = 0f
            for (y in 0 until leadN) s += l[y, x]
            profile[x] = s / leadN
        }

        // Deviation of each row and column from the backing reference.
        var backing = 0f
        for (y in 0 until leadN) for (x in 0 until l.w) backing += abs(l[y, x] - profile[x])
        backing /= (leadN * l.w)
        val thresh = max(3f * backing, 6f)

        var top = -1; var bottom = -1
        for (y in 0 until l.h) {
            var s = 0f
            for (x in 0 until l.w) s += abs(l[y, x] - profile[x])
            if (s / l.w > thresh) { if (top < 0) top = y; bottom = y }
        }
        if (top < 0) return null

        var left = -1; var right = -1
        for (x in 0 until l.w) {
            var s = 0f
            for (y in top..bottom) s += abs(l[y, x] - profile[x])
            if (s / (bottom - top + 1) > thresh) { if (left < 0) left = x; right = x }
        }
        if (left < 0) return null

        val m = 2
        return android.graphics.Rect(
            max(0, (left - m) * step), max(0, (top - m) * step),
            min(bmp.width, (right + 1 + m) * step), min(bmp.height, (bottom + 1 + m) * step)
        )
    }

    fun autocrop(bmp: Bitmap): Bitmap {
        val r = sheetBounds(bmp) ?: return bmp
        if (r.width() < 32 || r.height() < 32) return bmp
        if (r.width() >= bmp.width && r.height() >= bmp.height) return bmp
        val out = Bitmap.createBitmap(bmp, r.left, r.top, r.width(), r.height())
        if (out !== bmp) bmp.recycle()
        return out
    }

    fun rotate(bmp: Bitmap, degrees: Int): Bitmap {
        val d = ((degrees % 360) + 360) % 360
        if (d == 0) return bmp
        val m = Matrix().apply { postRotate(d.toFloat()) }
        val out = Bitmap.createBitmap(bmp, 0, 0, bmp.width, bmp.height, m, true)
        if (out !== bmp) bmp.recycle()
        return out
    }

    /** True when the page carries essentially nothing, ignoring the borders. */
    fun isBlank(bmp: Bitmap, drop: Int = 50): Boolean {
        val step = detectScale(bmp)
        val l = luma(bmp, step)
        val bx = l.w / 20
        val by = l.h / 20
        if (l.w - 2 * bx < 8 || l.h - 2 * by < 8) return false
        var ink = 0
        var total = 0
        for (y in by until l.h - by) for (x in bx until l.w - bx) {
            total++
            if (l[y, x] < 255 - drop) ink++
        }
        return total > 0 && ink * 1000L / total < 2      // under 0.2% marked
    }

    fun thumbnail(bmp: Bitmap, maxEdge: Int = 400): Bitmap {
        val longest = max(bmp.width, bmp.height)
        if (longest <= maxEdge) return bmp.copy(Bitmap.Config.ARGB_8888, false)
        val f = maxEdge.toFloat() / longest
        return Bitmap.createScaledBitmap(
            bmp, max(1, (bmp.width * f).toInt()), max(1, (bmp.height * f).toInt()), true
        )
    }
}
