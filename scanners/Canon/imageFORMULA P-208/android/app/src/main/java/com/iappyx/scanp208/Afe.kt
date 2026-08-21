package com.iappyx.scanp208

import kotlin.math.roundToInt

/**
 * Analogue front end arithmetic.
 *
 * These functions read captured references and produce register values; the
 * command layer only writes them. The device measures in 12 bits and the bulk
 * stream carries the top 8, so a reading of n is n*16 in these units.
 */
object Afe {

    const val DARK_TARGET = 96        // of 4096  ->   6.0 in 8-bit
    const val WHITE_TARGET = 2730     // of 4096  -> 170.6 in 8-bit, two thirds of full scale
    const val GAIN_MAX = 63
    const val OFFSET_MAX = 255
    const val EXPOSURE_MAX = 0x1fff

    /**
     * The gain stage behaves as A(gain) = k / (GAIN_POLE - gain) over gain
     * 0..63, so its range is 79/16 = 4.94x end to end. Both servos are that
     * law rearranged, which is why (GAIN_POLE - gain) appears in each.
     */
    const val GAIN_POLE = 79

    /**
     * One offset step moves the dark reading by OFFSET_SERVO / (GAIN_POLE -
     * gain) ADC counts - about 3.9 counts at minimum gain and 19.5 at maximum,
     * since the gain stage amplifies the pedestal along with the signal.
     *
     * Kept in step with driver.py deliberately: the two implementations must
     * derive the same operating point, so this value is not tuned separately.
     */
    const val OFFSET_SERVO = 311.2937

    /** One offset servo step. */
    fun offsetStep(min12: Int, gain: Int, curOffset: Int): Int {
        val delta = (min12 - DARK_TARGET) * (GAIN_POLE - gain) / OFFSET_SERVO
        return (curOffset - delta).roundToInt().coerceIn(0, OFFSET_MAX)
    }

    /** One gain servo step; unchanged when the measurement is on target. */
    fun gainStep(max12: Int, curGain: Int, target: Int = WHITE_TARGET): Int {
        val v = GAIN_POLE - (max12.toDouble() / target * (GAIN_POLE - curGain)).toInt()
        return v.coerceIn(0, GAIN_MAX)
    }

    /**
     * One exposure step. Exposure is per channel, so it is what balances the
     * channels against each other; gain and offset are per side and cannot.
     */
    fun exposureStep(meas12: Int, curExposure: Int, target: Int = WHITE_TARGET): Int {
        val m = if (meas12 < 1) 1 else meas12
        return (curExposure.toLong() * target / m).toInt().coerceIn(1, EXPOSURE_MAX)
    }

    /**
     * Percentile of 8-bit samples, via a 256-bin histogram.
     *
     * A plain min() over a reference strip latches onto one dead cell and never
     * moves, so the servo works on percentiles instead.
     */
    fun percentileU8(data: ByteArray, pct: Double, count: Int = data.size): Int {
        if (count <= 0) return 0
        val hist = IntArray(256)
        for (i in 0 until count) hist[data[i].toInt() and 0xff]++
        val rank = ((count - 1) * pct / 100.0).roundToInt().coerceIn(0, count - 1)
        var seen = 0
        for (v in 0..255) {
            seen += hist[v]
            if (seen > rank) return v
        }
        return 255
    }
}
