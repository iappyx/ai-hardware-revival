#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Canon CanoScan 8000F (CNQL2403.DLL) motor slope-table generators.

Byte-for-byte reconstruction of the two firmware table builders:

  * Builder A  FUN_10006ef0 / FUN_10007010  -> master accel ramp (SDRAM 0x7fffff)
  * Builder B  FUN_1000dc90                 -> imaging slope table (SDRAM 0x803fff)

Everything is derived from the DLL machine code (x87 float expressions
recovered by disassembly; see BUILDER_B_SPEC.md).  Pure Python, no numpy.

Key recovered constants (from CNQL2403.DLL .rdata):
  DAT_10018138 = 0.04807692307692307   (== 1/20.8, the "tick" reciprocal)
  DAT_10018188 = 20.8                  (crossover multiplier)
Both are IEEE-754 doubles embedded in the DLL; the tick reciprocal is the
stored double *just below* 1/20.8, which is what makes several accel entries
truncate one tick low (the documented +/-1 float rounding).

The single 1179-entry master "nanoseconds" ramp lives at file offset 0x2a26c
(VA 0x1002a26c).  Every table in Builder B is a window / reversal of it.
"""

import struct
import base64

# --------------------------------------------------------------------------
# Recovered floating-point constants (exact bit patterns from the DLL)
# --------------------------------------------------------------------------
# 0x18138: d8 89 9d d8 89 9d a8 3f  -> 0.04807692307692307   (1/20.8, tick recip)
TICK_RECIP = struct.unpack('<d', bytes.fromhex('d8899dd8899da83f'))[0]
# 0x18188: cd cc cc cc cc cc 34 40  -> 20.8                  (crossover mult)
CROSS_MUL  = struct.unpack('<d', bytes.fromhex('cdcccccccccc3440'))[0]

assert TICK_RECIP == 0.04807692307692307
assert CROSS_MUL  == 20.8

# --------------------------------------------------------------------------
# The master ns ramp  (DAT_1002a26c, 1179 * uint32 little-endian)
# --------------------------------------------------------------------------
# Embedded master ramp (1179 LE uint32).  Base64 of the raw DLL bytes.
MASTER_NS_B64 = """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llsBAIJbAQBtWwEAWFsBAENbAQAuWwEAGlsBAAVbAQDwWgEA21oBAMZaAQCyWgEAnVoBAIhaAQBzWgEAXloBAEpaAQA1WgEAIFoBAAtaAQD2WQEA4lkBAM1ZAQC4WQEAo1kBAI5ZAQB6WQEAZVkBAFBZAQA7WQEAJlkBABJZAQD9WAEA6FgBANNYAQC+WAEAqlgBAJVYAQCAWAEAa1gBAFZYAQBCWAEALVgBABhYAQADWAEA7lcBANpXAQDFVwEAsFcBAJtXAQCGVwEAclcBAF1XAQBIVwEAM1cBAB5XAQAKVwEA9VYBAOBWAQDLVgEAtlYBAKJWAQCNVgEAeFYBAGNWAQBOVgEAOlYBACVWAQAQVgEA+1UBAOZVAQDSVQEAvVUBAKhVAQCTVQEAflUBAGpVAQBVVQEAQFUBACtVAQAWVQEAAlUBAO1UAQDYVAEAw1QBAK5UAQCaVAEAhVQBAHBUAQBbVAEARlQBADJUAQAdVAEACFQBAPNTAQDeUwEAylMBALVTAQCgUwEAi1MBAHZTAQBiUwEATVMBADhTAQAjUwEADlMBAPpSAQDlUgEA0FIBALtSAQCmUgEAklIBAH1SAQBoUgEAU1IBAD5SAQAqUgEAFVIBAABSAQDrUQEA1lEBAMJRAQCtUQEAmFEBAINRAQBuUQEAWlEBAEVRAQAwUQEAG1EBAAZRAQDyUAEA3VABAMhQAQCzUAEAnlABAIpQAQB1UAEAYFABAEtQAQA2UAEAIlABAA1QAQD4TwEA408BAM5PAQC6TwEApU8BAJBPAQB7TwEAZk8BAFJPAQA9TwEAKE8BABNPAQD+TgEA6k4BANVOAQDATgEAq04BAJZOAQCCTgEAbU4BAFhOAQBDTgEALk4BABpOAQAFTgEA8E0BANtNAQDGTQEAsk0BAJ1NAQCITQEAc00BAF5NAQBKTQEANU0BACBNAQALTQEA9kwBAOJMAQDNTAEAuEwBAKNMAQCOTAEAekwBAGVMAQBQTAEAO0wBACZMAQASTAEA/UsBAOhLAQDTSwEAvksBAKpLAQCVSwEAgEsBAGtLAQBWSwEAQksBAC1LAQAYSwEAA0sBAO5KAQDaSgEAxUoBALBKAQCbSgEAhkoBAHJKAQBdSgEASEoBADNKAQAeSgEACkoBAPVJAQDgSQEAy0kBALZJAQCiSQEAjUkBAHhJAQBjSQEATkkBADpJAQAlSQEAEEkBAPtIAQDmSAEA0kgBAL1IAQCoSAEAk0gBAH5IAQBqSAEAVUgBAEBIAQArSAEAFkgBAAJIAQDtRwEA2EcBAMNHAQCuRwEAmkcBAIVHAQBwRwEAW0cBAEZHAQAyRwEAHUcBAAhHAQDzRgEA3kYBAMpGAQC1RgEAoEYBAItGAQB2RgEAYkYBAE1GAQA4RgEAI0YBAA5GAQD6RQEA5UUBANBFAQC7RQEApkUBAJJFAQB9RQEAaEUBAFNFAQA+RQEAKkUBABVFAQAARQEA"""

def _load_master():
    raw = base64.b64decode(MASTER_NS_B64)
    assert len(raw) == 1179 * 4, len(raw)
    return list(struct.unpack('<%dI' % 1179, raw))

MASTER_NS = _load_master()   # 1179 entries; MASTER_NS[0]=200000000 ... [1178]=83200

# Pre-ramp master entries (indices -425..-1), needed by the 300/600/1200 decel tails.
# Extracted from CNQL2403.DLL .data just below DAT_1002a26c so the module is
# fully self-contained (no DLL needed at run time).
_PRERAMP_LO = -425
_PRERAMP_B64 = "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"
_PRERAMP = list(struct.unpack('<%dI'%((len(base64.b64decode(_PRERAMP_B64)))//4), base64.b64decode(_PRERAMP_B64)))

def _master_at(idx):
    if 0 <= idx < 1179:
        return MASTER_NS[idx]
    if _PRERAMP_LO <= idx < 0:
        return _PRERAMP[idx - _PRERAMP_LO]
    raise IndexError("master index %d out of embedded range" % idx)


# --------------------------------------------------------------------------
# x87 helpers
# --------------------------------------------------------------------------
def _ftol(x):
    """Replicate MSVC __ftol(): truncate toward zero to a 32-bit signed int."""
    v = int(x)                 # Python int() truncates toward zero
    return v & 0xFFFFFFFF

def _tick(ns, div):
    """One accel/decel tick:  __ftol( (double)ns / (double)div * (1/20.8) ).

    Mirrors:  fild qword[ns]; fdiv dword[div]; fmul qword[0x10018138]; __ftol
    div is (ca00+1) held as a 32-bit float; for ca00 in {0,1} it is exact.
    """
    return _ftol((float(ns) / float(div)) * TICK_RECIP)

def _tick_gain(ns, gain):
    """dpi-tail tick:  __ftol( __ftol((double)ns*gain) * (1/20.8) ).

    Mirrors the two consecutive __ftol calls in the tail loop:
      fild qword[ns]; fmul dword[esp+0x14(gain)]; __ftol   (-> integer)
      fild that int ; fmul qword[0x10018138]    ; __ftol
    """
    inner = _ftol(float(ns) * gain)          # truncate(ns*gain)
    return _ftol(float(inner) * TICK_RECIP)


# --------------------------------------------------------------------------
# Builder A : master accel ramp  (FUN_10006ef0 via FUN_10007010)
# --------------------------------------------------------------------------
def build_master_ramp():
    """Return the master accel ramp bytes uploaded to SDRAM 0x7fffff.

    Source table DAT_1002a270 == &MASTER_NS[1], count 0x49a (1178).
    Each entry BE32(trunc(ns/20.8)); first and last words get bit31 set.
    reg 0x36 ramp-length code = 1178//8 - 10 = 0x89.
    """
    n = 0x49a  # 1178
    vals = [_tick(MASTER_NS[1 + i], 1) for i in range(n)]
    vals[0]  |= 0x80000000
    vals[-1] |= 0x80000000
    return b''.join(struct.pack('>I', v & 0xFFFFFFFF) for v in vals)

MASTER_RAMP_REG36 = (0x49a // 8) - 10   # = 0x89


# --------------------------------------------------------------------------
# Builder B : imaging slope table  (FUN_1000dc90)
# --------------------------------------------------------------------------
def cruise_period(xdpi, exposure, ca20, ca4c):
    """Recovered cruise (minimum / fastest) motor period, in timer ticks.

    From FUN_1000dc90 @1000dcf4-1000dd6d (all integer arithmetic):
        exp24 = exposure * 0x18                       ; c07c*24
        if ca4c == 0:
            p = exp24 // (0x12c0 // xdpi)
        elif xdpi < 0x12c0:                           ; 4800
            p = exp24 // (0x12c0 // (xdpi*2))
        else:
            p = exp24 * 2                              ; exp24<<1
        if ca20: p >>= 1
    (0x12c0 == 4800 == the CCD's base optical dpi.)
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


# dpi-tail table selection.  Every "table" is a backward window of MASTER_NS.
# start_index = master index the tail begins at (read downward);
# count       = number of tail entries.
# Verified: 0x2b454 == &MASTER_NS[1146]; 0x2a268==&MASTER_NS[-1]; etc.
# 2400 / 4800 do not read the ramp: they copy the cruise word.

# Absolute start offsets (in *entries* relative to MASTER_NS[0]) of each
# dpi-tail source, recovered from the DLL pointers:
#   75/150 : 0x1002b454 -> master +1146   (count 0x47a=1146)
#   300    : 0x1002a088 -> master  -121   (count 0x131= 305)
#   600    : 0x1002a248 -> master   -9    (count 0x6f = 111)
#   1200   : 0x1002a268 -> master   -1    (count 7)
# The 300/600/1200 sources sit *before* MASTER_NS[0] in the same .data blob;
# we read them straight from the DLL if available, else raise.
_DLL_PATH = None
def _set_dll(path):
    global _DLL_PATH
    _DLL_PATH = path

def _read_dll_u32(off):
    with open(_DLL_PATH, 'rb') as f:
        f.seek(off)
        return struct.unpack('<I', f.read(4))[0]

_TAIL_SRC = {
    0x4b:  (0x2b454, 0x47a),   # 75
    0x96:  (0x2b454, 0x47a),   # 150
    300:   (0x2a088, 0x131),   # 300
    600:   (0x2a248, 0x6f),    # 600
    0x4b0: (0x2a268, 0x7),     # 1200
}

def _tail_source_values(xdpi):
    """Yield the `count` source ns values for the dpi tail, read *backward*
    from the recovered file offset.  For 75/150 this is entirely inside the
    embedded master ramp; for 300/600/1200 it reaches slightly before it and
    is read from the DLL file (path via set_dll_path)."""
    off, cnt = _TAIL_SRC[xdpi]
    base = 0x2a26c
    start_idx = (off - base) // 4      # may be negative
    return [_master_at(start_idx - j) for j in range(cnt)]

def set_dll_path(path):
    """Optional: provide CNQL2403.DLL so the 300/600/1200 tails (which read a
    few entries just before the embedded master ramp) can be reproduced."""
    _set_dll(path)


def build_slope(xdpi, exposure=0x2a00, travel=1310,
                ca00=0, ca20=None, ca4c=None,
                d1b8=0, chan_gain=1.0, lines=None):
    """Build the imaging slope table (SDRAM 0x803fff) exactly as FUN_1000dc90.

    Implements the LONG (imaging) branch, taken when travel>=0x514 or ca20!=0.
    Parameters:
      xdpi      : scan X resolution (also selects the dpi tail)
      exposure  : DAT_1004c07c  (default 0x2a00 == the 75-dpi preview value)
      travel    : param_2 == DAT_1004c118 carriage travel step count
      ca00      : DAT_1002ca00 (0 for flatbed <1200dpi)  -> div = ca00+1
      ca20      : DAT_1002ca20; default (xdpi>149)
      ca4c      : DAT_1002ca4c; default (xdpi>299)
      d1b8      : DAT_1006d1b8 (0 in the shipped DLL)
      chan_gain : DAT_1004c0e8 channel gain (1.0 flatbed)
    Returns the raw big-endian byte string uploaded to the ASIC.
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

    uVar5 = xdpi & 0xFFFF
    p2 = travel & 0xFFFF          # uVar1 / param_2 low word
    cruise = cruise_period(xdpi, exposure, ca20, ca4c)
    cb = [cruise & 0xff, (cruise >> 8) & 0xff,
          (cruise >> 16) & 0xff, (cruise >> 24) & 0xff]  # uVar8,uVar2,uVar3,bVar4

    if travel == 0:
        # degenerate: only the two trailing cruise words are written
        return _emit_trailer(b'', cb, uVar5, cruise, gain, xdpi, 0, ca00, div)

    # decide branch
    short_branch = (p2 < 0x514) and (ca20 == 0)
    buf = bytearray()

    if short_branch:
        raise NotImplementedError(
            "short reposition branch (0x1002478c table) not needed for imaging")

    # ---- LONG (imaging) branch ------------------------------------------
    # accel: 1179 entries master[0..1178], div by (ca00+1)
    accel = [_tick(MASTER_NS[i], div) for i in range(0x49b)]
    accel[-1] |= 0x80000000                       # last accel word bit31
    for v in accel:
        buf += struct.pack('>I', v & 0xFFFFFFFF)

    # crossover threshold
    thr = _ftol(float(cruise) * CROSS_MUL)
    # search DAT_1002b4d4 (== &MASTER_NS[1178]) backward for first >= thr
    cx = 0
    while cx < 0x49b:
        if MASTER_NS[1178 - cx] >= thr:
            break
        cx += 1
    # adjust fw
    if cx == 0:
        fw = 0
    elif ca20:
        fw = ((cx >> 2) * 4 - 1) & 0xFFFFFFFF
    else:
        fw = cx - 1 if (cx & 1) == 0 else cx      # make odd

    # cruise-count word  (placed at index 1179)
    if ca20 == 0:
        half = ((fw & 0xFFFF) + 1) // 2
        const = 0x24d if d1b8 else 0x261
    else:
        half = ((fw & 0xFFFF) + 1) >> 2
        const = 0x126 if d1b8 else 0x13a
    count_word = (p2 - half - const) & 0xFFFFFFFF
    buf += struct.pack('>I', count_word)

    # decel: fw entries, MASTER_NS reversed from index 1178
    for j in range(fw & 0xFFFF):
        buf += struct.pack('>I', _tick(MASTER_NS[1178 - j], div))

    # flat block (only when d1b8 == 0)
    if d1b8 == 0:
        buf += struct.pack('>I', cruise | 0x80000000)   # cruise|0x80
        buf += struct.pack('>I', 0x00000014)            # 0x14

    # final two-word cruise block (always)
    buf += struct.pack('>I', cruise | 0x80000000)
    buf += struct.pack('>I', 0x80000000)
    buf += struct.pack('>I', cruise | 0x80000000)

    # dpi tail
    if uVar5 in (0x960, 0x12c0):          # 2400 / 4800 : copy cruise x19
        for _ in range(0x13):
            buf += struct.pack('>I', cruise & 0xFFFFFFFF)
        # last word bit31
        _or_last_bit31(buf)
    else:
        src = _tail_source_values(xdpi)
        tail = [_tick_gain(ns, gain) for ns in src]
        # 300/1200/(75/150) all set bit31 on the final tail word; 600 too.
        for v in tail:
            buf += struct.pack('>I', v & 0xFFFFFFFF)
        _or_last_bit31(buf)

    return bytes(buf)


def _or_last_bit31(buf):
    v = struct.unpack('>I', bytes(buf[-4:]))[0] | 0x80000000
    buf[-4:] = struct.pack('>I', v)


def _emit_trailer(prefix, cb, uVar5, cruise, gain, xdpi, div, ca00, _div):
    b = bytearray(prefix)
    b += struct.pack('>I', cruise | 0x80000000)
    b += struct.pack('>I', 0x80000000)
    b += struct.pack('>I', cruise | 0x80000000)
    return bytes(b)


# --------------------------------------------------------------------------
# Per-dpi reference values (exposure = 16000 unless preview)
# --------------------------------------------------------------------------
def reference_values(exposure=16000):
    rows = []
    for xdpi in (75, 150, 300, 600, 1200, 2400):
        ca20 = 1 if xdpi > 149 else 0
        ca4c = 1 if xdpi > 299 else 0
        cp = cruise_period(xdpi, exposure, ca20, ca4c)
        rows.append((xdpi, ca20, ca4c, cp))
    return rows


# --------------------------------------------------------------------------


def build_hometail(xdpi, exposure=0x2a00, ca20=None, ca4c=None, ca00=0):
    """End-of-scan decel/return-home ramp (SDRAM 0x801fff).
    reversed master ticks clamped to the cruise floor; first & last word bit31.
    Byte-exact vs the captured bo_110 for the 75-dpi preview."""
    if ca20 is None: ca20 = 1 if xdpi > 149 else 0
    if ca4c is None: ca4c = 1 if xdpi > 299 else 0
    div = (1 if ca00 else 0) + 1
    cruise = cruise_period(xdpi, exposure, 1 if ca20 else 0, 1 if ca4c else 0)
    N = 1147
    out = bytearray()
    for j in range(N):
        v = max(_tick(MASTER_NS[N - j], div), cruise)
        out += struct.pack('>I', v & 0xFFFFFFFF)
    out[0:4]   = struct.pack('>I', struct.unpack('>I', out[0:4])[0]   | 0x80000000)
    out[-4:]   = struct.pack('>I', struct.unpack('>I', out[-4:])[0]   | 0x80000000)
    return bytes(out)




def _total_steps(dpi, travel, exposure):
    sl = build_slope(dpi, exposure=exposure, travel=travel)
    w = struct.unpack('>%dI' % (len(sl) // 4), sl)
    return 1179 + w[1179] + (len(w) - 1180)

def imaging_travel(dpi, exposure=0x2a00, target_steps=3046):
    """Motor travel (steps) so the carriage covers the SAME physical bed length
    (== the verified 75-dpi full-bed value) at any dpi. Higher dpi captures more
    lines via a longer cruise period + smaller Y-divider, NOT more travel.
    Equalising total commanded steps prevents the carriage over-running the bed."""
    if dpi <= 75:
        return 1310
    lo, hi, best = 520, 6000, None
    for _ in range(40):
        mid = (lo + hi) // 2
        try:
            t = _total_steps(dpi, mid, exposure)
        except Exception:
            lo = mid + 1; continue
        if best is None or abs(t - target_steps) < abs(best[1] - target_steps):
            best = (mid, t)
        if t < target_steps: lo = mid + 1
        else: hi = mid - 1
        if lo > hi: break
    return best[0]


if __name__ == '__main__':
    import os, sys
    here = os.path.dirname(os.path.abspath(__file__))
    dll = os.path.join(here, '..', 'phase0', 'CNQL2403.DLL')
    if os.path.exists(dll):
        set_dll_path(dll)

    cap_path = os.path.join(here, 'tables3full', 'bo_0109_9448.bin')
    if not os.path.exists(cap_path):
        print("motor_tables self-test: reference capture not present (dev-only). "
              "Module imports fine and the generators are ready to use.")
        sys.exit(0)
    cap = open(cap_path, 'rb').read()
    capw = [struct.unpack_from('>I', cap, 4 * i)[0] for i in range(len(cap) // 4)]

    # ---- captured preview scan parameters (derived, see BUILDER_B_SPEC.md) --
    XDPI, EXPO, TRAVEL = 75, 0x2a00, 1310
    gen = build_slope(XDPI, exposure=EXPO, travel=TRAVEL,
                      ca00=0, ca20=0, ca4c=0)
    genw = [struct.unpack_from('>I', gen, 4 * i)[0] for i in range(len(gen) // 4)]

    print("=== CanoScan 8000F Builder B verification ===")
    print("params: xdpi=%d exposure=0x%x travel=%d ca00=0 ca20=0 ca4c=0"
          % (XDPI, EXPO, TRAVEL))
    print("cruise_period = %d (0x%x)" % (cruise_period(XDPI, EXPO, 0, 0),
                                         cruise_period(XDPI, EXPO, 0, 0)))
    print("generated length = %d words, capture = %d words"
          % (len(genw), len(capw)))
    assert len(genw) == len(capw), "LENGTH MISMATCH"

    # exact match fraction
    exact = sum(1 for a, b in zip(genw, capw) if a == b)
    frac = exact / len(capw)
    # per-entry tick error (compare low 31 bits)
    maxerr = 0
    err_idx = []
    for i, (a, b) in enumerate(zip(genw, capw)):
        da = a & 0x7fffffff
        db = b & 0x7fffffff
        if (a >> 31) != (b >> 31):
            print("  !! marker mismatch at idx", i, hex(a), hex(b))
        e = abs(da - db)
        if e > maxerr:
            maxerr = e
        if a != b:
            err_idx.append(i)
    print("exact word match: %d / %d = %.4f%%"
          % (exact, len(capw), 100.0 * frac))
    print("max per-entry tick error: %d" % maxerr)
    print("mismatch indices (%d): %s%s"
          % (len(err_idx), err_idx[:20], " ..." if len(err_idx) > 20 else ""))

    # ---- the must-be-perfect invariants ---------------------------------
    marks_gen = [i for i, x in enumerate(genw) if x >> 31]
    marks_cap = [i for i, x in enumerate(capw) if x >> 31]
    print("markers gen:", marks_gen)
    print("markers cap:", marks_cap)
    assert marks_gen == marks_cap, "MARKER SET MISMATCH"
    assert genw[1178] == capw[1178] == 0x80000f9f, "cruise-adjacent marker"
    assert (genw[1179] & 0x7fffffff) == 0x2ad, "CRUISE COUNT wrong"
    assert genw[1179] == capw[1179], "cruise count word"
    # cruise-period word in the flat block
    assert genw[1211] == capw[1211] == 0x80000fc0, "CRUISE PERIOD word"
    assert genw[1215] == capw[1215] == 0x80000fc0
    assert genw[-1] == capw[-1], "last (return-home) word"
    print("INVARIANTS OK: length, all markers, cruise period (0xfc0),"
          " cruise count (0x2ad), tail terminator all exact.")

    print()
    print("=== per-dpi cruise values @ exposure=16000 ===")
    print(" xdpi  ca20 ca4c  cruise_period(dec / hex)")
    for xdpi, ca20, ca4c, cp in reference_values(16000):
        print("  %4d   %d    %d    %8d  0x%x" % (xdpi, ca20, ca4c, cp, cp))
    print()
    print("=== master ramp (Builder A) sanity ===")
    ra = build_master_ramp()
    print("ramp len=%d first=0x%08x reg0x36=0x%x"
          % (len(ra), struct.unpack('>I', ra[:4])[0], MASTER_RAMP_REG36))
