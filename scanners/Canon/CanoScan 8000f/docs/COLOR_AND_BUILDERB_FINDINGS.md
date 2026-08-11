# Color mode + Builder B — resolved findings (DLL-derived)

## COLOR (the fix)
reg 0x05 fields (CNQL2403 FUN_1000ada0 @10016-10035):
- [1:0] channel mode: gray=0, color=1, mono-variant=2
- [7:6] CHANNEL-COMBINE: mono modes set =1 (FUN_10005ca0); COLOR leaves =0.
- Mono imaging capture value = 0x46 ([7:6]=1 combine, [2]=1, [1:0]=2).
- TRUE COLOR value = 0x05 ([7:6]=0, [2]=1, [1:0]=1).
- Empirical: 0x45 (kept [7:6]=1) -> [A,B,A] 2-channel degenerate. 0x05 -> 3 indep channels.
- DAT_1004c130 = 3 (color) / 1 (gray) = CCD channel count; divides pixel-clock reg 0x19/0x1a (FUN_100096c0 @8333), host stride _DAT_1004c092 = 3*(bpp/8)*width (@8315).
- PIXEL FORMAT (host, FUN_10008930 @7602): color 8-bit = 3 bytes/pixel pixel-INTERLEAVED R,G,B, one line/row. 16-bit = 6 bytes/pixel [Rhi,Rlo,Ghi,Glo,Bhi,Blo]. Gray = 1 byte/pixel.

## LAUNCH (FUN_1000ada0 @10077-10090)
run_mode = 2 if dpi<1200 else 0; W(0x02[4:5],run_mode); W(0x2f.bit7,1); W(0x02.bit1=GO,1); SEL(0x24).

## Builder B (FUN_1000dc90 @11331) — motor slope, per dpi + step_count
target cruise period (ticks), exposure=16000:
  if ca4c==0: target=(exp*24)//(4800//dpi)
  elif dpi<4800: target=(exp*24)//(4800//(dpi*2))
  else: target=exp*48 ; if ca20: target>>=1
  flatbed: ca00=0(div=1), ca20=(dpi>149), ca4c=(dpi>299)
  values: 150->6000,300->24000,600->48000,1200->96000,2400->192000,4800->384000
LONG branch (all dpi>=150):
  accel: master[0..1178] BE32(trunc(ns/div/20.8)); buf[0]|=0x80, last accel|=0x80
  crossover: thr=trunc(target*20.8); f=count of master fast-end < thr; fw=(f>>2)*4-1 (ca20)
  cruise word (travel, NO 0x80): (step_count - (fw+1)//4 - const)&0xffffffff ; const=0x13a flatbed
  decel: fw entries master fast->slow BE32(trunc(ns/div/20.8))
  flat block (flatbed): BE32(target|0x80),BE32(0x14),BE32(target|0x80),BE32(0x80),BE32(target|0x80)
  dpi tail (read backward, emit=trunc(trunc(ns*gain)/20.8), gain=c0e8*(ca4c?2:1)), last|=0x80:
    1200:0x1002a268 x7 ; 600:0x1002a248 x111 ; 300:0x1002a088 x305 ; 150/75:0x1002b454 x1146
    (2400/4800: copy BE32(target) x19)
  upload -> SDRAM 0x803fff.
Builder A (master ramp, FUN_10006ef0): master[1..1178] BE32(trunc(ns/20.8)), first/last|=0x80,
  reg 0x36 = 1178//8 - 10, upload -> 0x7fffff. 4712 bytes, first=0x8003f8fb (byte-exact).
Master table VA 0x1002a26c (file off 0x2a26c), 1179 int32 ns, first=200000000, last=83200.

## TRAVEL vs CAPTURE
- Motor travel = cruise word from param_2=DAT_1004c118 (carriage geometry + scan-area Y start).
- Captured length = reg 0x1b/0x1c = height*ydpi/2400 (+4@2400,+8@4800). For full page raise BOTH consistently (cruise steps >= captured lines + ramp).

===========================================================================
UPDATE 2026-08-10 - NATIVE COLOR ACHIEVED (hardware-verified)
===========================================================================

THE MISSING PIECE WAS THE 3x3 CHANNEL MATRIX (regs 0x37/0x38), NOT reg 0x05.

Mechanism (verified on hardware):
- The ASIC has a 3x3 channel-mixing matrix, streamed as 9 Q13 fixed-point
  entries via reg 0x37 (lo byte) / 0x38 (hi byte), COLUMN-MAJOR.
- Every calibration phase in the mono capture programs IDENTITY:
  [2000 0 0 / 0 2000 0 / 0 0 2000]  (0x2000 = 1.0 in Q13).
- The final MONO imaging program instead streams the LUMA matrix:
  0980 0980 0980 / 1300 1300 1300 / 0300 0300 0300 (column-major)
  = every output row [0.297, 0.594, 0.094] -> all 3 output lanes = luma.
  This is what made lanes 0/2 bit-identical in every color attempt that
  only changed reg 0x05. (The DLL analysis missed it because the matrix
  values come from HOST parameters, not from the color flag DAT_1004c140.)

WORKING COLOR RECIPE (delta vs the mono capture replay, in-place, pre-GO):
1. reg 0x05: 0x46 -> 0x05  ([1:0]=1 color, bit2 res<1200 kept, [7:6]=0)
   substituted at the capture's own write sites (ops 3498/3499); writes
   after GO (reg2=0xa2) may be ignored - always substitute in place.
2. regs 0x37/0x38: replace the 9-pair luma stream (ops 3519-3536) with
   IDENTITY [0x2000,0,0, 0,0x2000,0, 0,0,0x2000].
3. Read 3x the mono byte count (1769472 for the 620px window).

WIRE FORMAT: pixel-interleaved [R,G,B], 3 B/px, 1860 B/line @620px (USB 2.0).
CCD LINE PITCH: channels are 11 rows apart at this Y sampling
  (l0[r] ~ l1[r+11] ~ l2[r+22]; verified by derivative cross-correlation
  peaks at -11/-11/-22). Align: R=l0[r], G=l1[r+11], B=l2[r+22].
Lane order is R,G,B (verified: yellow logo renders yellow = R+G high, B low).
White balance after shading correction is ~unity (1.01/0.99/1.01).
Renderer: imaging.py.

reg 0x15/0x16 forcing to 0x80 is NOT needed (tested both ways, no effect
on color; power-on value 0x02 works).

===========================================================================
UPDATE 2026-08-10 (2) - FULLY NATIVE DRIVER MILESTONE
===========================================================================
stage 'scannative' produces a correct, neutrally-balanced full-page colour
scan with NO sniffed state except the three imaging motor tables
(master ramp / slope / tail, ops 3460-3479 of the capture).

Hardware-debugged findings on top of CALIBRATION_SPEC.md:
1. The scan program's auto-return-home is executed from the slope TAIL table
   (SDRAM 0x801fff). Omitting it strands the carriage at end of travel.
   reg 0x36 (ramp length code, 0x89 for this move) must accompany the tables.
2. Lamp PWM has two words (reg 0x2b=A, 0x2c=B, 0x2d=hi bits). Calibration
   and all static measurements run with A=0 (FUN_10006230(0,b99c)); the
   MOVING scan requires both restored (FUN_10006230(b99e=0x320, b99c)).
3. reg 0x01 bit5 = USB-2.0 data mode. Measurement programs (FUN_10009960)
   set it 0; it MUST be set back to 1 before imaging or the bulk stream is
   constant garbage (~0x19 bytes).
4. reg 0x60 bit3 belongs set (capture init value 0xa8); reg 4=0x86 is the
   warm-state acknowledge after warm-up.
5. Native calibration measured gains 97/95/a4 vs the Windows sniff's
   96/95/a4 on the same hardware - the gain formula g=208/(283-code) and
   K=210 target are confirmed end to end.
6. RESULT: fresh calibration at scan time yields neutral background
   (G/R=1.017 vs 1.27 with replayed stale calibration) - the "green glare"
   was stale-calibration + lamp state, not colour processing.
Offline register-state diff (simulate writes into a shadow file, diff vs
the capture's shadow at launch) found #3/#4 without hardware runs - use it
before every hardware test.

REMAINING for 100% native: Builder A/B motor table generation (FUN_10006ef0
/ FUN_1000dc90) for arbitrary dpi, and 600-class calibration at true
600/1200 dpi scanning (calibration capture already runs at the 600 class).
