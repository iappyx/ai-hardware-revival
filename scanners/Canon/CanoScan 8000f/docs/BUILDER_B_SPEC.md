# Builder B — CanoScan 8000F imaging MOTOR SLOPE table (FUN_1000dc90)

Byte-level-accurate specification, recovered from `CNQL2403.DLL`
(PE32, ImageBase 0x10000000; `.text` RVA==file-offset).
Function `FUN_1000dc90 @ file 0xdc90 / VA 0x1000dc90`.

Generator `motor_tables.py` reproduces both vendor tables byte-exact:
the 75-dpi preview table (2362 BE uint32) and the 1200-dpi table (2339
BE uint32), 0 residual differences. All float math was recovered by
disassembling the raw machine code with capstone (the Ghidra decompile
drops every x87 argument into bare `__ftol()`).

---
## 0. Recovered constants (with file offsets)

| symbol | file off / VA | raw bytes (LE) | value | role |
|---|---|---|---|---|
| `DAT_10018138` | 0x18138 / 0x10018138 | `d8 89 9d d8 89 9d a8 3f` | **0.04807692307692307** | tick reciprocal `1/20.8` |
| `DAT_10018188` | 0x18188 / 0x10018188 | `cd cc cc cc cc cc 34 40` | **20.8** | crossover multiplier |

`DAT_10018138` is the IEEE-754 double **immediately below** the true `1/20.8`.
Multiplying instead of dividing truncates a handful of ramp entries one tick
low (e.g. `master[1178]=83200`: `83200*0.048076923… = 3999.99… → 3999 = 0xf9f`,
not 4000). This must be reproduced exactly.

The master "nanoseconds" ramp is a single 1179-entry uint32 LE table at
`DAT_1002a26c` (file 0x2a26c): `master[0]=200000000 … master[1178]=83200`.
**Every table Builder B touches is a window or reversal of this one array:**

| DLL pointer | file off | == master index | used for |
|---|---|---|---|
| `&DAT_1002a26c` | 0x2a26c | `master[0]`   | accel source (forward) |
| `DAT_1002b4d4`  | 0x2b4d4 | `master[1178]`| decel source + crossover search (backward) |
| `DAT_1002b454`  | 0x2b454 | `master[1146]`| 75/150 dpi tail source (backward, 0x47a=1146) |
| `DAT_1002a268`  | 0x2a268 | `master[-1]`  | 1200 dpi tail source (backward, 7) |
| `DAT_1002a248`  | 0x2a248 | `master[-9]`  | 600 dpi tail source (backward, 0x6f=111) |
| `DAT_1002a088`  | 0x2a088 | `master[-121]`| 300 dpi tail source (backward, 0x131=305) |

(The 300/600/1200 tail sources read a few entries just *before* `master[0]`, so
they live in the preceding `.data` blob; the 75/150 tail is entirely inside the
embedded ramp.)

Runtime flags (`.data`/`Shared`, all default 0 in file, set by the scan program):
`DAT_1002ca00`, `DAT_1002ca20`, `DAT_1002ca4c`, `DAT_1006d1b8`,
exposure `DAT_1004c07c`, channel gain `DAT_1004c0e8`.

---
## 1. THE KEY MOTOR FINDING — which exposure feeds the cruise period

The cruise (fastest) period is computed from an **exposure** word, and the two
resolution classes feed the motor DIFFERENT exposures:

- **Res-class 1 (≤600 dpi, single CCD array):** motor exposure = **0x2a00**
  (same as the CCD base exposure). Slope params: exposure 0x2a00, travel 1302,
  tail gain 1.0 (byte-exact vs the 150 capture). (The 75-dpi *preview* self-test
  table below uses travel 1310 — a different, lower-res scan mode.)
- **Res-class 0 (1200 dpi, two staggered CCD arrays read together):** the CCD
  line-period exposure is **0x5400** (reg 0x09/0x0a = 0x0540), but the MOTOR
  still uses the **BASE exposure 0x2a00**, NOT 0x5400. Slope params: exposure
  0x2a00, travel 1302, tail gain 0.5 (`chan_gain=0.5`).

Feeding the motor 0x5400 yields cruise-period word `0x8001f800` (129024) —
exactly double the correct `0x8000fc00` (64512) — which makes the carriage step
half as often, covering only half the plate (image stretched ~2×). With the
correct base exposure, `build_slope(1200, exposure=0x2a00, travel=1302,
chan_gain=0.5)` reproduces the vendor's uploaded 1200-dpi slope table
byte-for-byte (all 2339 words, zero differences), verified against a real
ScanGear 1200-dpi USB capture.

---
## 2. Cruise (minimum / fastest) period — `f(xdpi, exposure, ca20, ca4c)`

Disassembly `@1000dcf4 – 1000dd6d` (pure integer arithmetic). `0x12c0 == 4800`
is the CCD base optical dpi:

```
exp24 = exposure * 0x18                 ; *24
if   ca4c == 0:            p = exp24 // (4800 //  xdpi)
elif xdpi < 4800:         p = exp24 // (4800 // (xdpi*2))
else:                     p = exp24 * 2
if ca20:                  p >>= 1
```

Flag derivation (from scan program FUN_1000ada0, unchanged here):
`ca00 = 0` for ydpi<1200 (ramp divisor `div = ca00+1 = 1`),
`ca20 = (xdpi > 149)`, `ca4c = (xdpi > 299)`.

### Cruise-period VALUES (exposure = 0x2a00, the flatbed base)

Preview capture xdpi=75: `p = (10752*24)//(4800//75) = 258048//64 = 4032 = 0xfc0`.
1200-dpi capture: `p = 64512 = 0xfc00` (ca20=ca4c=1; see §1).

For reference, at the alternate exposure 16000 the period scales linearly:

| xdpi | ca20 | ca4c | period (dec, exp=16000) | hex |
|---|---|---|---|---|
| 75   | 0 | 0 | 6000   | 0x1770 |
| 150  | 1 | 0 | 6000   | 0x1770 |
| 300  | 1 | 1 | 24000  | 0x5dc0 |
| 600  | 1 | 1 | 48000  | 0xbb80 |
| 1200 | 1 | 1 | 96000  | 0x17700 |
| 2400 | 1 | 1 | 192000 | 0x2ee00 |
| 4800 | 1 | 1 | 384000 | 0x5dc00 |

Higher period = slower stepping = more Y samples per inch. The period scales
directly with exposure and inversely with the `4800/xdpi` optical divisor.

### Preview dpi is 75 (not 150)

`ca20 = (xdpi>149) = 0`: the captured cruise-count word uses constant `0x261`
and the "make-odd" fw adjustment, both ca20==0 code paths (`@1000e3d4`); the dpi
tail has 0x47a=1146 entries (the `uVar5=0x4b(75)` branch). 150 dpi would set
ca20=1 and use `0x13a`/`0x96`.

---
## 3. Native resolutions

Native motor rungs are **75/150/300/600/1200**. 100/200/400/800 are produced by
software resampling (each is ⅔ of the next native rung) — the hardware is never
driven at those rates.

---
## 4. Table layout (LONG / imaging branch)

The long branch is taken when `travel >= 0x514 (1300)` **or** `ca20 != 0`
(`@1000dd81`). Emission order and preview (75-dpi, travel=1310) indices:

| words | idx range | contents |
|---|---|---|
| 1179 | 0..1178 | **accel**: `BE32( ftol(master[i]/div * 1/20.8) )`; last word (idx1178) OR 0x80000000 |
| 1 | 1179 | **cruise-count** word (travel, no marker) |
| fw | 1180..1180+fw-1 | **decel**: `BE32( ftol(master[1178-j]/div * 1/20.8) )` |
| 2 | +.. | **flat block** (only if `d1b8==0`): `BE32(cruise\|0x80)`, `BE32(0x14)` |
| 3 | +.. | **final block** (always): `BE32(cruise\|0x80)`, `BE32(0x80000000)`, `BE32(cruise\|0x80)` |
| 1146 | ..2361 | **dpi tail**: `BE32( ftol( ftol(master[k]*gain) * 1/20.8 ) )`, k=1146..1; last OR 0x80000000 |

Preview markers (bit31 set): **{1178, 1211, 1213, 1214, 1215, 2361}** — last
accel word, flat/final cruise words, the `0x80000000` sentinel, final
return-home word. Total 2362 words.

### 4.1 Accel emission — `@1000e1e2 – 1000e25f`
Loop 1179 times: `inner = master[i]/div`; `BE32(ftol(inner * 1/20.8))`.
`__ftol` = `FUN @0x100116d4`, truncation toward zero. `div = ca00+1` (=1 for
flatbed). Last accel word `|= 0x80000000`. Because `1/20.8` is stored slightly
low the products round identically to the DLL (0 residual errors).

### 4.2 Crossover search — `@1000e287 – 1000e2be`
```
thr = ftol(cruise * 20.8)                       ; DAT_10018188
scan master backward from master[1178]; cx = first i where master[1178-i] >= thr
```
Preview: `thr = ftol(4032*20.8) = 83865`; first entry ≥ 83865 is
`master[1146]=83866` → **cx = 32**.

`fw` adjust (`@1000e2c0 – 1000e2fc`):
```
if cx==0:        fw = 0
elif ca20:       fw = (cx>>2)*4 - 1
else:            fw = cx-1 if cx even else cx      ; force odd
```
Preview (ca20==0, cx=32 even) → **fw = 31**.

### 4.3 Cruise-count word — `@1000e300 – 1000e48e`
The **cruise STEP COUNT** (how many motor steps run at cruise period); scales
scan travel and must be exact. Written at idx1179.
```
ca20==0 : half = (fw+1)//2 ; word = travel - half - CONST
          CONST = 0x261 (d1b8==0)  |  0x24d (d1b8!=0)
ca20!=0 : half = (fw+1)>>2 ; word = travel - half - CONST
          CONST = 0x13a (d1b8==0)  |  0x126 (d1b8!=0)
```
Preview: `685 = 1310 − (31+1)//2 − 0x261` → **0x2ad**, exact.

### 4.4 Decel emission — `@1000e49c – 1000e50d`
The accel ramp read backward from its fast end (`master[1178]` down), `fw`
entries: `BE32(ftol(master[1178-j]/div * 1/20.8))`.

### 4.5 Flat + final blocks — `@1000e5a0 – 1000e59f`
```
if d1b8==0:  write cruise|0x80 , 0x00000014          ; "flat" pair
always   :  write cruise|0x80 , 0x80000000 , cruise|0x80
```
`cruise|0x80` = bit31 set on the 32-bit cruise period → preview `0x80000fc0`.

### 4.6 dpi tail — `@1000e6f9 – 1000e9e3` (switch on `uVar5=xdpi`)
Two `__ftol`s per entry (gain then tick), reading the master ramp backward from
the dpi-specific pointer:
```
inner = ftol(master[k] * gain)      ; gain = DAT_1004c0e8 * (ca4c ? 2 : 1)
BE32( ftol(inner * 1/20.8) )
```

| xdpi (uVar5) | source ptr | master start idx | count |
|---|---|---|---|
| 75 (0x4b) / 150 (0x96) | 0x1002b454 | 1146 | 0x47a = 1146 |
| 300 | 0x1002a088 | −121 | 0x131 = 305 |
| 600 | 0x1002a248 | −9 | 0x6f = 111 |
| 1200 (0x4b0) | 0x1002a268 | −1 | 7 |
| 2400 (0x960) / 4800 (0x12c0) | — | — | copy `cruise` ×0x13 (19) |

`c0e8` is the tail gain: **1.0** for res-class 1 (75–600), **0.5** for res-class
0 (1200); with `ca4c` it is doubled. The final tail word gets bit31 set
(return-home terminator). Preview tail = `master[1146..1]` → 1146 words, first
`0xfc0`, last `0x8003f8fb` (idx2361).

### 4.7 Trailing bookkeeping
`FUN_10007050(buf, param_1)` uploads to SDRAM 0x803fff; `FUN_10006ea0` handles a
tail slice for the descriptor. `param_1` is a descriptor length, not table bytes.

---
## 5. Builder A recap (master accel ramp) — byte-exact
`FUN_10006ef0` via `FUN_10007010`: source `DAT_1002a270 == &master[1]`,
count `0x49a = 1178`, `BE32(ftol(ns/20.8))`, first & last words OR 0x80000000.
First word = **0x8003f8fb**, length 4712 bytes. `reg 0x36 = 1178//8 − 10 = 0x89`.

---
## 6. How period and exposure keep speed correct across dpi
- The cruise **period** carries the resolution: proportional to `exposure` and
  to `xdpi/4800` (via the `4800/xdpi` divisor), so 8× the dpi ⇒ ~8× the period ⇒
  ~8× slower carriage ⇒ ~8× more Y samples/inch.
- `ca20` (xdpi>149) halves the period — it pairs with reg 0x07[3:0], the Y-step
  divider (ASIC issues 2 micro-steps per table step), so the table period is
  halved while physical travel/step is unchanged.
- `ca4c` (xdpi>299) switches to the `4800/(xdpi*2)` divisor (and doubles the
  dpi-tail gain); for xdpi≥4800 the period is `exposure*48`.
- `DAT_1004c07c` (exposure) is the CCD integration time in the same tick units.
  For the motor, res-class 1 and res-class 0 both feed the **base** 0x2a00 (§1),
  keeping motor step rate = line rate even though the CCD line period at 1200 dpi
  is 0x5400.

`d1b8` (`DAT_1006d1b8`, 0 for all flatbed scans) toggles the alternate
cruise-count constants (0x24d/0x126) and drops the flat pair; it is a
build/variant flag.
