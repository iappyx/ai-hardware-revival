# Builder B — CanoScan 8000F imaging MOTOR SLOPE table (FUN_1000dc90)

Byte-level-accurate specification, recovered from `CNQL2403.DLL`
(PE32, ImageBase 0x10000000; `.text` RVA==file-offset).
Function `FUN_1000dc90 @ file 0xdc90 / VA 0x1000dc90`.
Ground truth: the captured 75-dpi motor slope table (2362 BE uint32; not included in this repo).

**Generator `motor_tables.py` reproduces the captured table 2362/2362
words = 100.000% byte-exact, max per-entry tick error 0.**  All markers,
the cruise period, the cruise-count word, the table length and the return-home
terminator are exact.

The Ghidra decompile drops every x87 argument into bare `__ftol()`.  All float
math below was recovered by disassembling the raw machine code with capstone.

---
## 0. Recovered constants (with file offsets)

| symbol | file off / VA | raw bytes (LE) | value | role |
|---|---|---|---|---|
| `DAT_10018138` | 0x18138 / 0x10018138 | `d8 89 9d d8 89 9d a8 3f` | **0.04807692307692307** | tick reciprocal `1/20.8` |
| `DAT_10018188` | 0x18188 / 0x10018188 | `cd cc cc cc cc cc 34 40` | **20.8** | crossover multiplier |

`DAT_10018138` is the IEEE-754 double **immediately below** the true `1/20.8`.
Multiplying instead of dividing is what makes a handful of ramp entries
truncate one tick low (e.g. `master[1178]=83200`: `83200*0.048076923… = 3999.99… → 3999 = 0xf9f`,
not 4000).  This detail is load-bearing and must be reproduced exactly.

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
| `DAT_10024dec`/`DAT_10024ac0`/`DAT_1002478c` | 0x24dec / 0x24ac0 / 0x2478c | before ramp | **short reposition branch only** (not imaging) |

(The 300/600/1200 tail sources read a few entries just *before* `master[0]`, so
they live in the preceding `.data` blob; the 75/150 tail is entirely inside the
embedded ramp.)

Runtime flags (`.data`/`Shared`, all default 0 in file, set by the scan program):
`DAT_1002ca00`, `DAT_1002ca20`, `DAT_1002ca4c`, `DAT_1006d1b8`,
exposure `DAT_1004c07c`, channel gain `DAT_1004c0e8`.

---
## 1. Cruise (minimum / fastest) period — `f(xdpi, exposure, ca20, ca4c)`

Disassembly `@1000dcf4 – 1000dd6d` (pure integer arithmetic):

```
1000dcf4  mov  ecx,[0x1004c07c]      ; exposure
1000dd03  lea  ecx,[ecx+ecx*2]       ; *3
1000dd06  shl  ecx,3                 ; *8  -> exp24 = exposure*0x18
1000dd09  cmp  edx,esi ; je 1000dd3e ; if ca4c==0 -> branch A
1000dd0d  cmp  ax,0x12c0 ; jae 1000dd35 ; ca4c!=0 && xdpi>=4800 -> branch C
   branch B (ca4c!=0, xdpi<4800):  ebx=0x12c0/(xdpi*2); p = exp24/ebx
   branch A (ca4c==0):             ebx=0x12c0/xdpi;     p = exp24/ebx
   branch C (xdpi>=4800):          p = exp24*2
1000dd5e  mov  eax,[0x1002ca20]
1000dd63  test eax,eax ; je         ; if ca20: p >>= 1
1000dd67  shr  ecx,1
```

`0x12c0 == 4800` is the CCD base optical dpi.  So:

```
exp24 = exposure * 24
if   ca4c == 0:            p = exp24 // (4800 //  xdpi)
elif xdpi < 4800:         p = exp24 // (4800 // (xdpi*2))
else:                     p = exp24 * 2
if ca20:                  p >>= 1
```

Flatbed flag derivation (from the scan program FUN_1000ada0, unchanged here):
`ca00 = 0` for ydpi<1200 (so ramp divisor `div = ca00+1 = 1`),
`ca20 = (xdpi > 149)`, `ca4c = (xdpi > 299)`.

### Cruise-period VALUES

**exposure = 16000** (the 600-class default):

| xdpi | ca20 | ca4c | cruise period (dec) | hex |
|---|---|---|---|---|
| 75   | 0 | 0 | 6000   | 0x1770 |
| 150  | 1 | 0 | 6000   | 0x1770 |
| 300  | 1 | 1 | 24000  | 0x5dc0 |
| 600  | 1 | 1 | 48000  | 0xbb80 |
| 1200 | 1 | 1 | 96000  | 0x17700 |
| 2400 | 1 | 1 | 192000 | 0x2ee00 |
| 4800 | 1 | 1 | 384000 | 0x5dc00 |

**Captured preview scan: xdpi=75, exposure=0x2a00=10752** → cruise = `(10752*24)//(4800//75)` = `258048//64` = **4032 = 0xfc0**.
The `0x2a00` value is the scan program's `base` for a <1200-dpi flatbed scan
(FUN_1000ada0 step 13: `base = 0x2a00` when ydpi<1200), which becomes the
exposure `DAT_1004c07c` here — **not** 16000.  This is why the preview cruise
is 0xfc0 and not 0x1770.

Higher period = slower stepping = more Y samples per inch.  The period scales
directly with exposure and inversely with the `4800/xdpi` optical divisor, so a
600-dpi scan (period 0xbb80) steps 8× slower than the 75-dpi preview class,
matching its 8× finer Y sampling.

---
## 2. True preview dpi

**xdpi = 75** (not 150).  Three independent confirmations:

1. `ca20 = (xdpi>149) = 0`.  The captured cruise-count word uses constant
   `0x261` and the fw "make-odd" adjustment, both of which are the **ca20==0**
   code paths (`@1000e3d4`).  150 dpi would set ca20=1 and use `0x13a`.
2. The dpi tail has 0x47a=1146 entries → the `uVar5 ∈ {0x4b(75), 0x96(150)}`
   branch; combined with ca20=0 it can only be 0x4b = **75**.
3. Cruise period 0xfc0 = `(10752*24)/(4800/75)` reproduces exactly with the
   `4800/xdpi` (ca4c==0) divisor at xdpi=75.

---
## 3. Table layout (LONG / imaging branch)

The long branch is taken when `travel >= 0x514 (1300)` **or** `ca20 != 0`
(`@1000dd81`).  The captured `travel = param_2 = DAT_1004c118 = 1310`.
Emission order and captured indices:

| words | idx range | contents |
|---|---|---|
| 1179 | 0..1178 | **accel**: `BE32( ftol(master[i]/div * 1/20.8) )`; last word (idx1178) OR 0x80000000 |
| 1 | 1179 | **cruise-count** word (travel, no marker) |
| fw | 1180..1180+fw-1 | **decel**: `BE32( ftol(master[1178-j]/div * 1/20.8) )` |
| 2 | +.. | **flat block** (only if `d1b8==0`): `BE32(cruise\|0x80)`, `BE32(0x14)` |
| 3 | +.. | **final block** (always): `BE32(cruise\|0x80)`, `BE32(0x80000000)`, `BE32(cruise\|0x80)` |
| 1146 | ..2361 | **dpi tail**: `BE32( ftol( ftol(master[k]*gain) * 1/20.8 ) )`, k=1146..1; last OR 0x80000000 |

Captured markers (bit31 set): **{1178, 1211, 1213, 1214, 1215, 2361}** — the
last accel word, the flat/final cruise words, the `0x80000000` sentinel, and the
final return-home word.  Total 2362 words.

### 3.1 Accel emission — `@1000e1e2 – 1000e25f`
```
1000e1e2  fild dword[esp+0x44]       ; (float)(ca00+1)
1000e1ee  fstp dword[esp+0x20]       ; div slot (32-bit float)
   loop 0x49b (1179) times, ebp = &master[i]:
1000e201  fild qword[esp+0x34]       ; (double)master[i]
1000e205  fdiv dword[esp+0x20]       ; / div
1000e209  fmul qword[0x10018138]     ; * (1/20.8)
1000e20f  call 0x100116d4            ; __ftol  (truncate toward 0)
   ... store as BE32 ...
1000e271  or   al,0x80               ; last accel word |= bit31 (byte, MSB)
```
`__ftol` = `FUN @0x100116d4`, truncation toward zero.  `div = ca00+1` (=1 for
the preview).  Because `1/20.8` is stored slightly low, e.g.
`master[1173]=83304 → 4004` (a −1 tick vs exact 4005): the documented float
rounding.  With `div=1` these products round identically to the DLL, so the
generated table matches **byte-exact** (0 residual errors on this capture).

### 3.2 Crossover search — `@1000e287 – 1000e2be`
```
1000e287  fild qword[esp+0x34]       ; (double)cruise
1000e28b  fmul qword[0x10018188]     ; * 20.8
1000e291  call 0x100116d4            ; thr = __ftol(cruise*20.8)
   eax=0x1002b4d4 (=&master[1178]); edx=i*4; eax-=edx
1000e2b4  cmp  [eax],thr ; jae break ; first i where master[1178-i] >= thr
           while i<0x49b
```
Preview: `thr = ftol(4032*20.8) = ftol(83865.6) = 83865`; first master entry
≥ 83865 is `master[1146]=83866` → **raw cx = 32**.

`fw` adjust (`@1000e2c0 – 1000e2fc`):
```
if cx==0:        fw = 0
elif ca20:       fw = (cx>>2)*4 - 1
else:            fw = cx-1 if cx even else cx      ; force odd
```
Preview (ca20==0, cx=32 even) → **fw = 31**.

### 3.3 Cruise-count word — `@1000e300 – 1000e48e`
Written at idx1179 (top 3 bytes from `iVar11`, LSB from `cVar10`; both encode the
same 32-bit value).  `uVar1 = travel & 0xffff`.
```
ca20==0 : half = (fw+1)//2 ; word = travel - half - CONST
          CONST = 0x261 (d1b8==0)  |  0x24d (d1b8!=0)
ca20!=0 : half = (fw+1)>>2 ; word = travel - half - CONST
          CONST = 0x13a (d1b8==0)  |  0x126 (d1b8!=0)
```
(`0xfffffd9f = −0x261`, `0xfffffec6 = −0x13a`, etc., quoted directly in the
disasm at 1000e3f6 / 1000e338.)  Preview:
`685 = 1310 − (31+1)//2 − 0x261 = 1310 − 16 − 609` → **cruise count = 0x2ad**, exact.

The cruise-count word is the **cruise STEP COUNT** — how many motor steps run at
the cruise period.  It scales scan travel/speed and must be exact.

### 3.4 Decel emission — `@1000e49c – 1000e50d`
```
   ebx=0x1002b4d4 (=&master[1178]); loop fw times, ebx -= 4:
1000e4b1  fild qword[esp+0x34]       ; (double)master[1178-j]
1000e4b5  fdiv dword[esp+0x20]       ; / div
1000e4b9  fmul qword[0x10018138]     ; * (1/20.8)
1000e4bf  call 0x100116d4            ; __ftol
```
i.e. the accel ramp read backward from its fast end, `fw` entries.

### 3.5 Flat + final blocks — `@1000e5a0 – 1000e59f`
```
if d1b8==0:  write cruise|0x80 , 0x00000014          ; "flat" pair
always   :  write cruise|0x80 , 0x80000000 , cruise|0x80
```
`cruise|0x80` means bit31 set on the 32-bit cruise period → captured `0x80000fc0`.

### 3.6 dpi tail — `@1000e6f9 – 1000e9e3`  (switch on `uVar5=xdpi`)
Two consecutive `__ftol`s per entry (gain then tick), reading the master ramp
backward from the dpi-specific pointer:
```
1000e79d  fild qword[esp+0x34]       ; (double)master[k]
1000e7a1  fmul dword[esp+0x14]       ; * gain   (gain = c0e8 * (ca4c?2:1))
1000e7a5  call 0x100116d4            ; inner = __ftol(master[k]*gain)
1000e7b2  fild qword[esp+0x2c]       ; (double)inner
1000e7b6  fmul qword[0x10018138]     ; * (1/20.8)
1000e7bc  call 0x100116d4            ; __ftol(inner/20.8)
```
`gain` = `DAT_1004c0e8 * (ca4c ? 2 : 1)`, computed once at entry
(`@1000dcea: fld[esp+0x10]; fadd st,st; fstp[esp+0x10]`).  Flatbed `c0e8 = 1.0`,
`ca4c=0` → gain = 1.0, so the tail is simply `ftol(master[k]/20.8)`.

| xdpi (uVar5) | source ptr | master start idx | count |
|---|---|---|---|
| 75 (0x4b) / 150 (0x96) | 0x1002b454 | 1146 | 0x47a = 1146 |
| 300 | 0x1002a088 | −121 | 0x131 = 305 |
| 600 | 0x1002a248 | −9 | 0x6f = 111 |
| 1200 (0x4b0) | 0x1002a268 | −1 | 7 |
| 2400 (0x960) / 4800 (0x12c0) | — | — | copy `cruise` ×0x13 (19) |

The final tail word gets bit31 set (return-home terminator).  Preview: tail =
`master[1146..1]` → 1146 words, first `0xfc0`, last `0x8003f8fb` (= idx2361).

### 3.7 Trailing bookkeeping
`FUN_10007050(buf, param_1)` uploads to SDRAM 0x803fff; `FUN_10006ea0` handles a
tail slice for the descriptor.  `param_1` (the running `_param_2 + offset`) is a
descriptor length, not table bytes.

---
## 4. Builder A recap (master accel ramp) — byte-exact, for completeness
`FUN_10006ef0` via `FUN_10007010`: source `DAT_1002a270 == &master[1]`,
count `0x49a = 1178`, `BE32(ftol(ns/20.8))`, first & last words OR 0x80000000.
First word = **0x8003f8fb**, length 4712 bytes.  `reg 0x36 = 1178//8 − 10 = 0x89`.

---
## 5. How Y-step divider and exposure keep speed correct across dpi
- The cruise **period** already carries the resolution: it is proportional to
  `exposure` and to `xdpi / 4800` (via the `4800/xdpi` divisor), so 8× the dpi ⇒
  ~8× the period ⇒ ~8× slower carriage, i.e. 8× more Y samples/inch.
- `ca20` (set for xdpi>149) halves the period — it corresponds to the
  reg 0x07[3:0] **Y-step divider** being engaged (the ASIC issues 2 micro-steps
  per table step), so the *table* period is halved while physical travel/step is
  unchanged; net feed rate stays matched to the exposure-derived line rate.
- `ca4c` (xdpi>299) switches the cruise formula to the `4800/(xdpi*2)` divisor
  (and doubles the dpi-tail gain), extending the usable period range for the
  high-dpi classes, and for xdpi≥4800 the period is taken as `exposure*48`.
- `DAT_1004c07c` (exposure) is the CCD integration time in the same tick units;
  raising dpi raises exposure (`base` 0x2a00→0x5400 across classes) so each CCD
  line is fully integrated at the slower travel speed. Period and exposure move
  together, keeping line rate = motor step rate.

`d1b8` (`DAT_1006d1b8`, 0 in the shipped DLL) toggles the alternate cruise-count
constants (0x24d/0x126) and drops the flat pair; it is a build/variant flag,
UNKNOWN trigger, but 0 for all observed flatbed scans.
