#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Motor tables for the CanoScan 8000F — carriage motion profile generation.

The scanner has no on-board motion control. Before every scan the host uploads
tables of *step intervals* into the ASIC's SDRAM; the ASIC walks a table, firing
one motor step and waiting the number of timer ticks given by the next entry.
One tick is 20.8 ns. The tables therefore *are* the carriage motion: how it
accelerates from rest, how fast it runs while imaging, and how it stops.

Three tables are uploaded per scan:

    master accel ramp   SDRAM 0x7fffff   spin up from standstill
    imaging slope       SDRAM 0x803fff   the scan pass: accel, cruise, decel, tail
    home-decel tail     SDRAM 0x801fff   brake and return to home

Everything here is computed. No table data is stored in this file.

## The acceleration law

The carriage accelerates at a constant **+20.0 steps/s per motor step**, which in
interval terms is a hyperbola:

    v(n)  = 20 * (n + b) = 20n + 164.664          [steps/s]
    ns(n) = round(50_000_000 / (n + b))           b = 8.2332183

The numerator is exactly 5e7, which is what makes the velocity slope exactly 20.
Because the interval table holds integers, each entry constrains ``ns`` to a
half-open interval; intersecting those constraints across the ramp leaves a
narrow feasible window for ``b``, and its centre satisfies every entry of phase 1
exactly.

Past entry 333 the profile leaves the hyperbola and becomes a linear sequence in
*tick* space, its slope stepping through the integers -21..-1 — the intervals
fall by exact whole numbers of ticks. Nineteen ``(slope, run-length)`` pairs
describe all 845 remaining entries.

## The decel tail

The per-dpi tail is the last section of the imaging slope, so it runs after the
cruise phase during which the image lines are captured. Its job is to bring the
carriage to rest; it does not shape the image.

At 75 and 150 dpi the tail is a reversed window of the master ramp. The same
construction is used at 300/600/1200: start at the master entry whose effective
interval matches that resolution's cruise period, then walk the ramp backward to
its slowest entry. That gives a monotonic deceleration from cruise speed down to
the ramp floor of ~185 steps/s, braking at a constant 20 steps/s per step — the
same rate the profile already uses in this speed band.

Hardware-tested at 75/150/300/600/1200 dpi, full bed and short region.
"""

import struct

# --------------------------------------------------------------------------
# Timer constants
# --------------------------------------------------------------------------
# The ASIC's step timer runs on a 20.8 ns tick. The reciprocal is the IEEE-754
# double immediately below 1/20.8; that exact value matters, because it is what
# makes a few entries truncate one tick low, which is what the hardware expects.
TICK_RECIP = struct.unpack('<d', bytes.fromhex('d8899dd8899da83f'))[0]
CROSS_MUL = 20.8

assert TICK_RECIP == 0.04807692307692307
assert CROSS_MUL == 20.8


# --------------------------------------------------------------------------
# Fixed-point helpers
# --------------------------------------------------------------------------
# Interval-to-tick conversion truncates toward zero as a 32-bit value. Defined
# before the ramp because the table is built through them.
def _ftol(x):
    """Truncate toward zero to a 32-bit value."""
    return int(x) & 0xFFFFFFFF


def _tick(ns, div):
    """One accel/decel tick: trunc((ns / div) * (1/20.8))."""
    return _ftol((float(ns) / float(div)) * TICK_RECIP)


def _tick_gain(ns, gain):
    """Tail tick: the interval is scaled by `gain`, then converted, each step
    truncated in turn — trunc(trunc(ns * gain) * (1/20.8))."""
    return _ftol(float(_ftol(float(ns) * gain)) * TICK_RECIP)


# --------------------------------------------------------------------------
# The acceleration ramp
# --------------------------------------------------------------------------
RAMP_N = 1179               # entries the ASIC expects
FIRST_DWELL_NS = 200000000  # entry 0: a 200 ms standing start

# ---- phase 1: entries 1..333, constant +20.0 steps/s per step -------------
PHASE1_K = 50000000.0
PHASE1_B = 8.2332183
PHASE1_END = 333

# ---- phase 2: entries 334..1178, linear in tick space ---------------------
# The interval falls by an exact whole number of ticks per step, that number
# stepping through the integers -21..-1. Nineteen (slope, run) pairs cover all
# 845 entries; intervals are reconstructed as round(ticks * 20.8).
PHASE2_ANCHOR = 7045        # tick value at n = 333
PHASE2_SEGMENTS = [
    (-21, 1), (-18, 4), (-17, 6), (-16, 6), (-15, 7), (-14, 8), (-13, 9),
    (-12, 10), (-11, 12), (-10, 13), (-9, 16), (-8, 20), (-7, 23), (-6, 30),
    (-5, 42), (-4, 58), (-3, 91), (-2, 189), (-1, 300),
]


def _phase2_ticks():
    """Tick values for n = 333..1178 from the segment description."""
    out = [PHASE2_ANCHOR]
    for slope, run in PHASE2_SEGMENTS:
        for _ in range(run):
            out.append(out[-1] + slope)
    return out


_PHASE2 = _phase2_ticks()


def ramp_ns(n):
    """Interval before step `n`, in nanoseconds.

    n == 0 is the standing-start dwell. 1..333 follow the hyperbola; 334..1178
    are reconstructed from the tick segments.

    Always an integer, as the ASIC's table is. This matters: `_tick_gain`
    truncates the value a second time, so a float here would round differently
    and throw the 75/150 dpi tails off by a tick.
    """
    if n == 0:
        return FIRST_DWELL_NS
    if n <= PHASE1_END:
        return round(PHASE1_K / (n + PHASE1_B))
    return round(_PHASE2[n - PHASE1_END] * CROSS_MUL)


def _build_master_ns():
    return [FIRST_DWELL_NS] + [ramp_ns(n) for n in range(1, RAMP_N)]


MASTER_NS = _build_master_ns()

# Deepest ramp entry each resolution actually walks — bounded by the cruise clamp
# in build_hometail, and by the accel phase reaching cruise speed.
RAMP_DEPTH = {1200: 29, 600: 66, 300: 140, 150: 1144, 75: 1144}


def _master_at(idx):
    if 0 <= idx < RAMP_N:
        return MASTER_NS[idx]
    raise IndexError("master ramp index %d out of range" % idx)


# --------------------------------------------------------------------------
# Builder A : master accel ramp
# --------------------------------------------------------------------------
def build_master_ramp():
    """Master accel ramp bytes uploaded to SDRAM 0x7fffff (1178 BE u32)."""
    n = 0x49a
    vals = [_tick(_master_at(1 + i), 1) for i in range(n)]
    vals[0] |= 0x80000000
    vals[-1] |= 0x80000000
    return b''.join(struct.pack('>I', v & 0xFFFFFFFF) for v in vals)


MASTER_RAMP_REG36 = (0x49a // 8) - 10   # = 0x89, the ramp-length code in reg 0x36


# --------------------------------------------------------------------------
# Builder B : cruise period + imaging slope
# --------------------------------------------------------------------------
def cruise_period(xdpi, exposure, ca20, ca4c):
    """Cruise (fastest) motor period, in timer ticks.

        exp24 = exposure * 24
        p = exp24 // (4800 // xdpi)          (ca4c == 0)
          | exp24 // (4800 // (xdpi*2))      (ca4c, xdpi < 4800)
          | exp24 * 2                        (ca4c, xdpi >= 4800)
        p >>= 1  if ca20

    4800 is the CCD's base optical resolution.
    """
    exp24 = (exposure * 0x18) & 0xFFFFFFFF
    if ca4c == 0:
        p = exp24 // (0x12c0 // xdpi)
    elif xdpi < 0x12c0:
        p = exp24 // (0x12c0 // (xdpi * 2))
    else:
        p = exp24 * 2
    if ca20:
        p >>= 1
    return p & 0xFFFFFFFF


# At 75/150 dpi the tail is a reversed window of the master ramp: 1146 entries
# read downward from entry 1146.
LOWRES_TAIL_START = 1146
LOWRES_TAIL_COUNT = 1146
LOWRES_TAIL_DPI = (75, 150)


def generated_tail(xdpi, gain):
    """Decel tail for 300/600/1200 — a reversed window of the master ramp.

    Start at the master entry whose *effective* interval matches this
    resolution's cruise period (the tail is emitted through
    `_tick_gain(ns, gain)`, so the effective interval is `ns * gain`), then walk
    the ramp backward to its slowest entry. The result is a monotonic
    deceleration from cruise speed down to the ramp floor of ~185 steps/s.

    Braking is a constant 20 steps/s per step, the same rate the 75/150 tail
    uses in this speed band. `tools/verify.py` re-checks that envelope,
    monotonicity and continuity with cruise on every run.
    """
    target = cruise_period(xdpi, 0x2a00,
                           1 if xdpi > 149 else 0,
                           1 if xdpi > 299 else 0) * CROSS_MUL / float(gain)
    n_hi = PHASE1_END
    for n in range(1, PHASE1_END + 1):
        if ramp_ns(n) <= target:
            n_hi = n
            break
    return [ramp_ns(n) for n in range(n_hi, 0, -1)]


def _tail_source_values(xdpi, gain=1.0):
    """Source intervals for the dpi tail."""
    if xdpi in LOWRES_TAIL_DPI:
        return [_master_at(LOWRES_TAIL_START - j) for j in range(LOWRES_TAIL_COUNT)]
    return generated_tail(xdpi, gain)


def build_slope(xdpi, exposure=0x2a00, travel=1310,
                ca00=0, ca20=None, ca4c=None,
                d1b8=0, chan_gain=1.0):
    """Imaging slope table (SDRAM 0x803fff).

    Layout: accel | cruise-count | decel | flat | cruise words | dpi tail.
    """
    if ca20 is None:
        ca20 = 1 if xdpi > 149 else 0
    if ca4c is None:
        ca4c = 1 if xdpi > 299 else 0
    ca00 = 1 if ca00 else 0
    ca20 = 1 if ca20 else 0
    ca4c = 1 if ca4c else 0
    div = ca00 + 1
    gain = chan_gain * (2.0 if ca4c else 1.0)

    rung = xdpi & 0xFFFF
    p2 = travel & 0xFFFF
    cruise = cruise_period(xdpi, exposure, ca20, ca4c)

    if travel == 0:
        out = bytearray()
        out += struct.pack('>I', cruise | 0x80000000)
        out += struct.pack('>I', 0x80000000)
        return bytes(out)

    if (p2 < 0x514) and (ca20 == 0):
        raise NotImplementedError("short reposition branch not needed for imaging")

    buf = bytearray()
    # accel: the full ramp, divided by (ca00 + 1)
    accel = [_tick(_master_at(i), div) for i in range(0x49b)]
    accel[-1] |= 0x80000000
    for v in accel:
        buf += struct.pack('>I', v & 0xFFFFFFFF)

    # crossover: the first ramp entry already faster than cruise
    thr = _ftol(float(cruise) * CROSS_MUL)
    cx = 0
    while cx < 0x49b:
        if _master_at(1178 - cx) >= thr:
            break
        cx += 1
    if cx == 0:
        fw = 0
    elif ca20:
        fw = ((cx >> 2) * 4 - 1) & 0xFFFFFFFF
    else:
        fw = cx - 1 if (cx & 1) == 0 else cx

    if ca20 == 0:
        half = ((fw & 0xFFFF) + 1) // 2
        const = 0x24d if d1b8 else 0x261
    else:
        half = ((fw & 0xFFFF) + 1) >> 2
        const = 0x126 if d1b8 else 0x13a
    buf += struct.pack('>I', (p2 - half - const) & 0xFFFFFFFF)

    for j in range(fw & 0xFFFF):
        buf += struct.pack('>I', _tick(_master_at(1178 - j), div))

    if d1b8 == 0:
        buf += struct.pack('>I', cruise | 0x80000000)
        buf += struct.pack('>I', 0x00000014)
    buf += struct.pack('>I', cruise | 0x80000000)
    buf += struct.pack('>I', 0x80000000)
    buf += struct.pack('>I', cruise | 0x80000000)

    if rung in (0x960, 0x12c0):            # 2400 / 4800: copy the cruise word
        for _ in range(0x13):
            buf += struct.pack('>I', cruise & 0xFFFFFFFF)
    else:
        for ns in _tail_source_values(xdpi, gain):
            buf += struct.pack('>I', _tick_gain(ns, gain) & 0xFFFFFFFF)
    _or_last_bit31(buf)
    return bytes(buf)


def _or_last_bit31(buf):
    v = struct.unpack('>I', bytes(buf[-4:]))[0] | 0x80000000
    buf[-4:] = struct.pack('>I', v)


# --------------------------------------------------------------------------
# Home-decel tail
# --------------------------------------------------------------------------
def build_hometail(xdpi, exposure=0x2a00, ca20=None, ca4c=None, ca00=0):
    """End-of-scan decel / return-home ramp (SDRAM 0x801fff).

    Every entry is clamped up to the cruise floor, which is why the higher
    resolutions depend only on the first few dozen ramp entries — see RAMP_DEPTH.
    """
    if ca20 is None:
        ca20 = 1 if xdpi > 149 else 0
    if ca4c is None:
        ca4c = 1 if xdpi > 299 else 0
    div = (1 if ca00 else 0) + 1
    cruise = cruise_period(xdpi, exposure, 1 if ca20 else 0, 1 if ca4c else 0)
    N = 1147
    out = bytearray()
    for j in range(N):
        out += struct.pack('>I', max(_tick(_master_at(N - j), div), cruise) & 0xFFFFFFFF)
    out[0:4] = struct.pack('>I', struct.unpack('>I', out[0:4])[0] | 0x80000000)
    out[-4:] = struct.pack('>I', struct.unpack('>I', out[-4:])[0] | 0x80000000)
    return bytes(out)


def reference_values(exposure=16000):
    rows = []
    for xdpi in (75, 150, 300, 600, 1200, 2400):
        ca20 = 1 if xdpi > 149 else 0
        ca4c = 1 if xdpi > 299 else 0
        rows.append((xdpi, ca20, ca4c, cruise_period(xdpi, exposure, ca20, ca4c)))
    return rows


if __name__ == '__main__':
    print("motor tables — fully generated")
    print("  phase 1  entries 1..%d    ns(n) = %.1f / (n + %.7f)"
          % (PHASE1_END, PHASE1_K, PHASE1_B))
    print("           v(n) = %.5f*n + %.4f steps/s  (+20 per step)"
          % (1e9 / PHASE1_K, 1e9 / PHASE1_K * PHASE1_B))
    print("  phase 2  entries %d..%d  linear in ticks, %d segments"
          % (PHASE1_END + 1, RAMP_N - 1, len(PHASE2_SEGMENTS)))
    print()
    for dpi in (75, 150, 300, 600, 1200):
        kind = "reversed master window" if dpi in LOWRES_TAIL_DPI else "generated"
        print("  %4d dpi  ramp depth %-5d  tail: %s" % (dpi, RAMP_DEPTH[dpi], kind))
