# Color pipeline + Builder B — resolved findings (hardware-verified)

## COLOR

reg 0x05 fields (FUN_1000ada0 @10016-10035):
- `[1:0]` channel mode: gray=0, color=1, mono-variant=2.
- `[7:6]` channel-combine: mono modes set =1 (FUN_10005ca0); COLOR leaves =0.
- Mono imaging value = 0x46 ([7:6]=1, [2]=1, [1:0]=2); COLOR = 0x05 ([7:6]=0,
  [2]=1, [1:0]=1).

The **3×3 channel matrix (regs 0x37/0x38)** is the other color-critical register.
It is streamed as 9 Q13 fixed-point entries via reg 0x37 (lo) / 0x38 (hi),
COLUMN-MAJOR (0x2000 = 1.0):
- **COLOR: identity** `[2000 0 0 / 0 2000 0 / 0 0 2000]`.
- **MONO: luma** `0980 0980 0980 / 1300 1300 1300 / 0300 0300 0300` (each output
  row [0.297, 0.594, 0.094] → all three output lanes = luma). Sourced from host
  parameters (DAT_1002ca2c), not from the color flag, so a color scan MUST
  overwrite it with identity — a mono luma matrix collapses all lanes to luma and
  makes lanes 0/2 bit-identical.

DAT_1004c130 = 3 (color) / 1 (gray) = CCD channel count; host stride
_DAT_1004c092 = 3*(bpp/8)*width (@8315); on USB 1.1 it also divides reg 0x19/0x1a
(FUN_100096c0 @8333).

### Working color recipe (delta vs a mono capture replay, in place, pre-GO)
1. reg 0x05: 0x46 → 0x05 (substitute at the capture's own write sites; writes
   after GO / reg2=0xa2 may be ignored — always substitute in place).
2. regs 0x37/0x38: replace the luma stream with identity.
3. Read 3× the mono byte count.

### Wire format and software reconstruction
- USB 2.0: pixel-interleaved [R,G,B], 3 B/px 8-bit (16-bit = 6 B/px
  [Rhi,Rlo,Ghi,Glo,Bhi,Blo]); 16-bit values are little-endian on the wire.
- CCD line pitch: the three channels are **11 rows apart** at this Y sampling
  (l0[r] ≈ l1[r+11] ≈ l2[r+22]; verified by derivative cross-correlation peaks at
  −11/−11/−22). Align R=l0[r], G=l1[r+11], B=l2[r+22].
- Lane order is R,G,B (verified: yellow renders as R+G high, B low).
- White balance after shading correction is ~unity (1.01/0.99/1.01).
- reg 0x15/0x16 forcing to 0x80 is NOT needed (power-on 0x02 works).
- Colour reconstruction (lane alignment, white balance) is done in software
  (renderer: imaging.py).

## LAUNCH (FUN_1000ada0 @10077-10090)
run_mode = 2 if dpi<1200 (res-class 1) else 0 (res-class 0);
W(0x02[5:4], run_mode); W(0x2f.bit7, 1); W(0x02.bit1 = GO, 1); SEL(0x24).
Status bits: home/busy = reg 0x64 bit6; move-done = reg 0x03 bit3.

## Builder B (FUN_1000dc90 @11331) — see BUILDER_B_SPEC.md for byte-exact detail
- Cruise period from exposure*24 and the `4800/xdpi` optical divisor
  (ca20=dpi>149 halves it; ca4c=dpi>299 switches to `4800/(xdpi*2)` and doubles
  the tail gain). Flatbed: ca00=0 (div=1).
- **Motor exposure = base 0x2a00 for BOTH res classes.** At 1200 dpi the CCD line
  period is 0x5400, but feeding that to the motor doubles the cruise word
  (0x8001f800 vs correct 0x8000fc00) and stretches the image ~2×. Verified
  slope params: res-class 1 exposure 0x2a00, travel 1302, tail gain 1.0;
  res-class 0 exposure 0x2a00, travel 1302, tail gain 0.5. `build_slope(1200,
  exposure=0x2a00, travel=1302, chan_gain=0.5)` reproduces the vendor's 1200-dpi
  table byte-for-byte (2339 words, 0 diffs).
- LONG branch (all dpi≥150): accel = master[0..1178] BE32(trunc(ns/div/20.8));
  cruise word = `step_count − (fw+1)/… − const` (no 0x80 marker); decel = fw
  fast-end entries reversed; flat/final blocks; dpi tail read backward
  (1200:0x1002a268 ×7 ; 600:0x1002a248 ×111 ; 300:0x1002a088 ×305 ;
  150/75:0x1002b454 ×1146). Upload → SDRAM 0x803fff.
- Builder A (master ramp, FUN_10006ef0): master[1..1178] BE32(trunc(ns/20.8)),
  first/last |=0x80, reg 0x36 = 1178//8 − 10, upload → 0x7fffff. 4712 bytes,
  first = 0x8003f8fb (byte-exact). Master table VA 0x1002a26c, 1179 int32 ns,
  first=200000000, last=83200.
- Native rungs 75/150/300/600/1200; 100/200/400/800 are software-resampled (⅔ of
  the next native rung).

## TRAVEL vs CAPTURE
- Motor travel = cruise word from param_2 = DAT_1004c118 (carriage geometry +
  scan-area Y start).
- Captured length = reg 0x1b/0x1c = height*ydpi/2400 (+4 @2400, +8 @4800). For a
  full page, cruise steps ≥ captured lines + ramp.

## Auxiliary hardware-verified facts
1. The scan's auto-return-home is executed from the slope TAIL table (SDRAM
   0x801fff); omitting it strands the carriage at end of travel. reg 0x36 (ramp
   length code, 0x89 for this move) must accompany the tables.
2. Lamp PWM has two words (reg 0x2b=A, 0x2c=B, 0x2d=hi bits). Static measurements
   run with A=0 (FUN_10006230(0,b99c)); the MOVING scan requires both restored
   (FUN_10006230(0x320, b99c)).
3. reg 0x01 bit5 = USB-2.0 data mode. Measurement programs (FUN_10009960) clear
   it; it MUST be set back to 1 before imaging or the bulk stream is constant
   garbage (~0x19 bytes).
4. reg 0x60 bit3 belongs set (capture init 0xa8); reg 4 = 0x86 is the warm-state
   acknowledge after warm-up.
5. AFE gain formula confirmed end to end: g = 208/(283−code), K=210 target
   (native calibration measured 97/95/a4 vs Windows 96/95/a4 on the same
   hardware).
6. A fresh calibration at scan time yields a neutral background (G/R=1.017 vs 1.27
   with replayed stale calibration) — the "green glare" was stale calibration +
   lamp state, not color processing.
