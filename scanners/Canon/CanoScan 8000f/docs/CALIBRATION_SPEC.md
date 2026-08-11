# CanoScan 8000F (CNQL2403.DLL) — COMPLETE NATIVE CALIBRATION SPEC

Source of truth: the decompiled scanner firmware (Ghidra output, 479 functions, ImageBase 0x10000000). The decompiled binary is NOT included in this repo.
Every claim cites FUN_xxxxxxxx and a decompiled line number ("@NNNN"). FPU constants that
Ghidra dropped were recovered by disassembling CNQL2403.DLL directly (section table: .text
RVA==file offset; .rdata @0x18000; .data @0x1a000 — all 1:1 file/RVA below 0x2d000).
Register setters are the ones in FULL_PROGRAM_SPEC.md §1. Anything not derivable is
marked **UNKNOWN**. All multi-byte register values are written via the setters
(lo-byte reg first), all SDRAM uploads are `reg 0x21/0x22/0x23 = addr; SEL 0x24; bulk-OUT`.

KEY DISCOVERIES (vs prior specs):
1. **The AFE gain formula**: `code = trunc(283.0 − 208.0/g)` (recovered from the FPU code at
   file 0x7fdb–0x7ff0 / 0x9360–0x936c: `fdivr [10018148]=208.0; fsubr [10018140]=283.0; call __ftol`).
   Inverse: `g = 208/(283−code)`. Self-validating: 0x4b→1.000×, 0xb3→exactly 2.000×.
   The sniffed end-state gains 96/95/a4 = 1.564/1.552/1.748× ⇔ white peaks 134/135/120 (8-bit)
   measured at unity gain against target 210 (see §7).
2. **Lamp warm-up IS in the DLL** — a polled state machine `FUN_1000f0e0` (@12070) driven by the
   `SetTPUMode` export (ScanGear calls it repeatedly until success). It includes brightness‑
   stability sampling at 1 Hz (three consecutive triples within ±2 counts), a lamp-PWM binary
   search (`FUN_10009b80` @8528), and — on flatbed completion — the 0xb3-gain lamp edge-find
   `FUN_1000ea90`. InitializeScanner itself contains **no** warm-up wait. See §3.
3. Warm-up state is **persisted in scanner SDRAM** ('M'/'B' bytes at 0x803eff, read at 0x803f00),
   so a warm scanner skips the whole wait. See §3.5.

---------------------------------------------------------------------------
## 0. GLOBAL FLAGS USED THROUGHOUT (names kept as DAT_*)

| global | meaning | set where |
|---|---|---|
| DAT_1006d1b8 | TPU mode (0 = flatbed) | DoCalibration @10187-10197, SetTPUMode |
| DAT_1002ca44 | TPU submode (3 = film/negative) | @10191,@10195 |
| DAT_1004c070 | film-calibration pass | @10197 |
| DAT_1004c198 | current capture is 2400-class (W=0x52d0) | @10175 |
| DAT_1002ca00 | quarter-res class (calib res < 600) | @10177-10178 |
| DAT_1004c120 | half-res class (600 ≤ calib res < 1200) | @10180-10181 |
| DAT_1004c104 | "2400 calibration done" (enables AFE reuse cache) | @10172-10174 |
| DAT_1002ca1c | AFE gains were overridden by host/TPU → recalibrate offsets at scan exposure | 0 @9732/@8698; 1 @9751,@9827 |
| DAT_1002ca04 | TPU batch continuation (skip homing) | 0 @10238 |
| DAT_1002ca58 | multi-strip batch counter — DoCalibration is a NO-OP while ≠0 (@10139) | ++ @9363, =0 @8690,@10091 |
| DAT_1002b994 | 1 = planar wire format (USB1.1), 0 = pixel-interleaved (USB2.0) | @10683 from DAT_1004c0e4 |
| DAT_1002b998 | "lamp is settled" latch (checked by FUN_1000f970) | =1 @11072, =0 on captures |
| DAT_1002b99c / b99e | flatbed / TPU lamp PWM word; **.data initial 0x320 (800)** (file 0x2b99c) | FUN_10009b80, warm-up |
| DAT_1004c07c | exposure word (default 16000 @8440) | FUN_10009960 |
| DAT_1004c19c | shading table byte length | FUN_1000d7d0 |
| DAT_1004c100/108 | persistent WHITE / DARK planar buffers, 0x1f400 B each (@7343-7344) | FUN_10008330 |
| DAT_1004c110/1b0 | film WHITE / DARK buffers | FUN_10008330 |

Hardware measurement counters (per-channel MIN/MAX peak detectors in the ASIC):
- `reg 0x30[1:0]` = channel bank select (0=R,1=G,2=B) — FUN_10006300 @5127.
- `FUN_100063a0(w)` @5173: w=0 → read **reg 0x35 only** (8-bit, high byte); w=1 → reg 0x35<<8 | reg 0x34
  (16-bit). This counter tracks the **white/maximum** level (used for lamp brightness/gain).
- `FUN_100063e0(w)` @5192: same layout from **reg 0x37 / 0x36**. Tracks the **black/minimum**
  level (used by the offset search, target 0x400). (Max/min identification is inferred from
  usage: 63a0 read with lamp on against bright thresholds, 63e0 with lamp off against 0x400;
  the silicon-level definition is **UNKNOWN**.)
- `FUN_10009ae0(pMaxR,pMaxG,pMaxB,pMinR,pMinG,pMinB,w)` @8489: one-shot measurement primitive:
  `GO=1 (reg2.b1); poll FUN_10005fa0()!=0 (regs 0x19/0x1a readback, 13-bit @4852) while GO set;
  GO=0; for bank 0,1,2: reg 0x30=bank; read 63a0 → pMax[k]; read 63e0 → pMin[k]` — it captures
  one line with the currently loaded register program and reads the peak counters,
  **no bulk data transfer**.
- `FUN_100069e0(ms)` @5651 = busy-wait on GetTickCount (the DLL's sleep).
- `FUN_10009cb0()` @8597 = wait-home: poll reg3.b3 (FUN_10005bc0) every 20 ms up to 1000×.
- `FUN_10009d20()` @8633 = wait-idle: poll reg 0x64.b6 ("not busy" when bit==1, FUN_10006770
  @5470; note: always short-circuits "idle" while DAT_1004c12c==1) every 20 ms up to 1000×.

Static-program builder `FUN_10009960(feed,width,afe)` @8416 (used by warm-up, lamp checks and
the TPU purge). Register writes in order:
```
FUN_10006d20(3)          reg 0x64.b0=0 (paper-edge sensor off)
reg5[4:3]=0 ; reg5.b2=0 ; FUN_10008180(0)  (AFE[3]=0x1f; 0x52..55=0x0c,0x0e,0x10,0x12)
reg1.b2=1 ; 0x70/71=0 ; 0x72/73=0 ; 0x74/75=0 ; reg5.b2=0 ; c120=0
reg8 = 0xa8 ; 0x09/0x0a = 8 ; 0x0b/0x0c = 8
reg2.b7=0 ; reg7.b4=0 ; reg7[3:0]=1 ; 0x1b/0x1c = 1
c07c=16000 ; c0e8=1.0f ; Builder-B FUN_1000dc90(600,0) → SDRAM 0x803fff
reg6.b7=0 ; identity matrix FUN_100070a0 (0x37/0x38 ×9: 2000,0,0,0,2000,0,0,0,2000)
0x19/0x1a = 1 ; if afe: FUN_100071e0()   (AFE 04=00,01=23,02=2c,03=1f,20..22=80,28..2a=4b)
0x14 = 0 ; reg6.b6=0 ; reg5[1:0]=1 (color) ; reg6[5:4]=1 (8-bit) ; 0x20[5:4]=0
0x10/0x11 = feed ; 0x12/0x13 = width
reg2[5:4] = afe? 3 : 0 ; reg2.b6=0 ; reg1.b5=0 ; b994=1
```
`FUN_10009aa0` @8472 (restore-after-measurement): `0x1b/0x1c=3000; reg2[5:4]=0; 0x20[5:4]=1`.

---------------------------------------------------------------------------
## 1. CALIBRATION READ WIRE FORMAT (FUN_1000c850 @10393, de-interleavers @10707-10770)

All calibration image captures are **3-channel, 16-bit** (`FUN_10005d10()` reg5[1:0]=1 @10671;
`FUN_10005d60(3)` reg6[5:4]=3 @10557; DAT_1004c1ac=0x10, DAT_1004c130=3 @10675-10676).
Data is fetched with SDRAM address 0 selected implicitly (no 0x21-0x23 write; the launch SEL
0x24 @10697 arms readout) via `_UsbReadScanner`, **one line per bulk read** of `W*6` bytes
(@10699-10702), 20 lines total (`0x1b/0x1c = 0x14` @10644, loop `while(line<0x14)` @10774).

- **USB 2.0** (DAT_1002b994==0, branch @10707-10726): line = W pixels ×
  `[Ahi,Alo,Bhi,Blo,Chi,Clo]` — pixel-interleaved, **big-endian u16 per channel**;
  bytes 0-1→channel0 buffer, 2-3→channel1, 4-5→channel2 (@10714-10720).
- **USB 1.1** (b994==1, branch @10729-10770): line = planar thirds:
  W big-endian u16 of ch0, then W of ch1, then W of ch2.
- Channel order is the CCD tap order; the verified imaging lane order is R,G,B
  (COLOR_AND_BUILDERB_FINDINGS.md), so ch0=R, ch1=G, ch2=B.

W (pixels per calibration line) = `0x52d0` for the 2400-class capture, else `0x2968`
(@10454: `uVar20 = (c198 ? 0x2968+0x2968 : 0x2968)`), overridden to `0x14b4` for the 600-class
(@10566) and `0xa5a` for the <600 class (@10577). NOTE: `0x12/0x13` (width) is written with W
(@10673), but the CCD window `FUN_100096c0(0x52d0, 0)` is **always computed for 0x52d0 px,
pixel-average 0** (@10684). On USB 2.0 that yields (formulas @8317-8347):
```
n=(0*0x52d0+0x400)>>10=1 ; m=(0xf7f−3*1)/3=0x529
0x40/41=0x0080  0x42/43=0x05a9  0x44/45=0x0ad3  0x46/47=0x0000
0x17/0x18 = min(0xfff,(0x529<<10)/0x52d0 − 1) = 0x3e     ← matches capture strobes 3271-3478
0x19/0x1a = 0
```
(USB1.1 branch divides by chan=3 as in FULL_PROGRAM_SPEC §5.45.)

---------------------------------------------------------------------------
## 2. InitializeScanner (export @75 → thunk_FUN_100076e0 @6546 = FUN_100076e0 @6389)

Export wrapper (@77-190): re-entry guard; `reg2.b0=0` (@123); Sleep(100); wait-home
`FUN_10007cf0` (@125, skipped when mid-batch ca58≠0 @6910); if first-open flag DAT_1006d190:
poll reg 0x64.b6 idle up to 30 s (@133-157); then **FUN_100076e0**; then `FUN_1000da60` (@168,
fn @10375: if reg4 status ==7 → write reg4=0x08 — power-on acknowledge); if that reported
power-on (or first init): `FUN_1000fa90` (@171, clears all mode/warm flags incl. d1b8=0) and
`FUN_1000a9c0` home-find (@172; fn @9063: `0x48/0x49 fields=0; reg 0x2e=0x28; 0x1b/0x1c=20000;
0x20[5:4]=1; short-move table FUN_10007030 → 0x7fffff; wait home; reg2.b0=1; sleep 1000;
reg2.b0=0; wait reg3.b3 ≤2 s; master ramp FUN_10007010; reg 0x2e=0xff`).

FUN_100076e0 body, in write order (@6405-6537):
```
reg 0x60.b7=1                                          @6405
regs 0x52,0x53,0x54,0x55 = 0x0f,0x11,0x13,0x15         FUN_100076c0 @6407
reg1.b0=1 ; reg 0x60[5:4]=2                            @6408-6409
USB probe: type1 → c0e4=0 (USB1.1); else c0e4=1 and _WriteRegister_8(0x10,4)  @6410-6417
_WriteRegister_8(0x10,4) ; _WriteRegister_8(1,0xf)     (USB bridge regs)      @6418-6419
DAT_1004c12c=1 (never cleared anywhere — grep exhaustive)                     @6420
reg5.b5=0 ; 0x48[3:1]=0 ; 0x48[7:5]=0 ; 0x49[3:1]=0 ; 0x49[7:5]=0             @6422-6426
LAMP ON: flatbed: read 0x29.b4 (FUN_10007db0); FUN_1000f970 = { if b998:
   re-check 0x29.b4, if it dropped write state 'B' (FUN_10006c10);
   FUN_10006230(b99e,b99c) → 0x2b=b99e.lo8, 0x2c=b99c.lo8,
   0x2d=(b99e>>4)&0x30|(b99c>>8)&3  (defaults 0x320,0x320 → 0x2b=0x20,0x2c=0x20,0x2d=0x33);
   reg 0x29.b4=1 (auto-clears 0x2a.b4, FUN_10006170 @4998) }                  @6427-6449
   if lamp was OFF: additionally clear warm flags ca3c/ca40 and write SDRAM state
   bytes 'B','B' → 0x803eff (FUN_10006c10 @5752: byte0 = ca3c?'M':'B', byte1 = ca40?'M':'B')
lamp-intensity file %TEMP%\CNQ2403C.shd: read 1 byte (default 0x0c if absent),
   FUN_10007c00: re-save; reg 0x29[3:0] = reg 0x2a[3:0] = byte>>2 (default 3)  @6451-6497
FUN_10007b30: all host gain words = 0x1000 (1.0 in 4.12 fixed point)          @6498
GAMMA uploads: FUN_10007250 (identity u16 ramp ×4 → 0x83ffff,0x81ffff,0x83ffff,0x85ffff)
   and FUN_10007490 (second bank → 0x80ffff,0x807fff,0x80ffff,0x817fff)       @6499-6505
reg 0x20[7:6]=1 ; reg1.b2=1 ; 0x20[5:4]=1 ; reg5.b2=0 ; c120=0 ; reg6.b3=1
reg1.b3=0 ; b4=0 ; b7=0 ; b6=0                                               @6507-6516
reg8=0xa8 ; 0x09/0x0a=8 ; 0x0b/0x0c=8 ; 0x70..0x75=0                          @6517-6522
FUN_10006d20(3): reg 0x64.b0=0                                                @6523
reg 0x50 bits b7..b2 = 0                                                      @6524-6529
master motor ramp upload FUN_10007010 → 0x7fffff                              @6530
reg 0x2f[5:0] = (1&7)<<3 | 2 = 0x0a ; reg3.b2=1                               @6531-6532
```
**NO AFE writes and NO warm-up wait happen here.** `FUN_100071e0` (AFE defaults:
`04=00, 01=0x23, 02=0x2c, 03=0x1f, 20/21/22=0x80 offsets, 28/29/2a=0x4b gains` @6068-6077)
is called ONLY from FUN_10009180 (@8126, gain determination) and from
FUN_10009960(..,..,afe=1) (@8446, warm-up/purge programs). The only waits in init are the
export wrapper's idle/home polls above.

---------------------------------------------------------------------------
## 3. LAMP WARM-UP (the complete DLL-side logic)

Driver-side warm-up is real and lives in `FUN_1000f0e0` @12070, wrapped by
`FUN_1000aaa0` @9110 (flatbed) / `FUN_1000ee90` @11924 (TPU), both called from the
**SetTPUMode export** (@631/@740 flatbed, @847 TPU). SetTPUMode returns 0 with status
0xa4 ("warming, call again") until the machine finishes; ScanGear polls it. Each call
advances at most one state. Timeline (flatbed; param_2=0):

1. **State ca64=0 — start** (@12118-12147): `0x2f.b7=1`; t0=GetTickCount; PWM words
   b99c=b99e=800; wait-home; lamp on `FUN_1000f970` + `FUN_10006230(0x320,0x320)`;
   b998=1; build static measurement program `FUN_10009960(0, 0x2968, 1)` — this is where
   the **AFE defaults (FUN_100071e0) get programmed** on a cold start; run mode 3 (static).
2. Status check each call: reg4 (FUN_10005c30) must be 0x86, 7 or 8, else error 0x9b (@12149-12153).
3. **ca68 — first fixed wait**: 4000 ms since t0 (@12154-12163).
4. **ca6c — pre-check** (@12164-12173): one FUN_10009ae0 sample (8-bit max counters);
   if any channel > 0x77 (119) the lamp is already bright → skip state 5.
5. **ca70 — second fixed wait**: another 4000 ms (@12174-12183).
6. **ca74 — brightness gate, ≤5000 ms** (@12184-12219): sample; if all three < 8 →
   fatal error 0xb2 "lamp dead"; flatbed: if all three > 0xd1 (209) advance, else sleep
   300 ms and re-check until 5 s elapsed, then advance anyway. (TPU dim path instead sets a
   long hold DAT_1002ba18 = 180000 ms if all <0x15, 20000 if any >0x1e, else 80000, and
   waits it out in state ca84 @12221-12229.)
7. **ca78 — lamp PWM search** `FUN_10009b80` @8528 (@12230-12242): b99c=0; step=0x200;
   10 iterations: `b99c += step; FUN_10006230(0,b99c) (0x2b=0,0x2c=lo8,0x2d[1:0]=hi2);
   sleep 300; FUN_10009ae0; if any max8 > 200 → b99c −= step; step >>= 1`. Clamp b99c ≤ 800.
   Save geometry scratch → SDRAM 0x803dff (FUN_10006a10 @5674: 14 bytes cf67,cf66,
   c124.be16, c082.be16, c11c.be16, c11e.be16, b99c.be16, b99e.be16). I.e. the PWM is set to
   the largest value (≤800) whose peak stays ≤200/255 at unity gain, exposure 16000 — this is
   the lamp intensity calibration. Then a post-check sample (all<8 → error 0xb2).
8. **ca7c=0 — STABILITY LOOP, 1 Hz, ≤10 s** (@12283-12362): each call sleeps 1000 ms then
   samples the three 8-bit max counters. All<8 → error 0xb2. It keeps the last three
   triples and requires **every pairwise per-channel difference < 3 counts**
   (|s_t−s_{t−1}|≤2 AND |s_t−s_{t−2}|≤2 AND |s_{t−1}−s_{t−2}|≤2, all channels, @12321-12345)
   for **3 consecutive iterations** (ca8a>2 @12346-12351) → stable. On success or on 10 s
   timeout (@12286): `FUN_10009aa0` (0x1b/1c=3000, mode 0, 0x20[5:4]=1) and advance.
9. **Final** (@12244-12276): `reg2.b7=1` park (TPU also waits home and clears the GO shadow
   bit); **flatbed only: `FUN_1000ea90()` lamp edge-find (§4)**; reset all state flags;
   `0x2f.b7=0`; report done (*param_1=1).

3.5 **Warm-state persistence**: on completion FUN_1000aaa0 writes reg4=0x86, sets ca3c=1 and
writes state bytes `'M','?'` to SDRAM 0x803eff (@9167-9172, FUN_10006c10). On the next
session, FUN_1000aaa0 reads 0x803f00 (FUN_10006c90 @5780; skipped when reg4 status==7 =
fresh power-on) and if byte0=='M' and the lamp is still on, **warm-up is skipped entirely**
(@9145-9156). TestScanner(4) additionally provides a lamp health check `FUN_1000db80` @11277:
lamp on; up to 10× { FUN_10009960(0,0x2968,1); FUN_10009ae0; FUN_10009aa0; done if any
channel > 99; else sleep 1000 }.

**There are no repeated WHITE-image reads for warm-up** — all stability checks use the
hardware max-level counters (reg 0x35 per bank), never bulk data.

---------------------------------------------------------------------------
## 4. LAMP EDGE-FIND FUN_1000ea90 @11741 (home-position geometry calibration)

Runs once at the end of flatbed warm-up (@12258-12259). It scans 200 lines while MOVING and
finds where the white calibration strip starts in X and Y. Register program (after
`FUN_10009960(0,0,0)` base):
```
short-move table FUN_10007030 → 0x7fffff                     @11773
reg1.b5=0 ; reg7[3:0]=4 ; reg2[5:4]=0 (moving run mode)      @11774-11776
reg5[1:0]=2 (mono-variant, FUN_10005d00) ; reg5[7:6]=1 (combine, FUN_10005ca0)  @11777-11778
reg6[5:4]=1 (8-bit) ; 0x1b/0x1c=200 ; 0x10/0x11=0 ; 0x20[5:4]=1                 @11779-11782
0x12/0x13 = 0xe6 (230 px)                                     @11784
AFE 0x28=0x29=0x2a = 0xb3   (= exactly 2.0× gain, §7 formula) @11785-11787
host globals: depth 8, chan 1, mode 1, planar (b994=1)        @11788-11798
reg2.b7=1 (park first) ; CCD window FUN_100096c0(0xe6,0)      @11799-11800
GO ; SEL 0x24 ; read 200 lines × 230 B via host reader FUN_10008930 ; GO=0  @11801-11804
```
Analysis (@11805-11866): `mx` = max byte of line 0. X edge: for lines 1..4 count leading
pixels ≤ mx/3, average → `xe`. If xe ≥ 2 and mx > 0x27: for lines 5.. (≤184 more): mean of
pixels [xe/2−1 .. xe); the first run of **4 consecutive** lines whose window-mean > mx/3 at
line index L (< 0xb8 total iterations) gives:
```
_DAT_1002cf66 = xe*4 + 0x5e          (X margin, 1200 dpi units)
_DAT_1004c082 = L*8  + 0x874         (Y margin, 2400 dpi units; L counts from 4)
```
else defaults `cf66=0x128, c082=0x9ea` (@11865-11866). Then (@11868-11871):
`_DAT_1004c11e = c082 + 0x1490 ; _DAT_1004c11c = cf66 + 0xd96 ; c124 = 0`; persist to SDRAM
scratch 0x803dff (FUN_10006a10) and to `%TEMP%\CNQ2403w.shd` (5 u16 records @11908-11914);
master ramp re-uploaded (FUN_10007010 @11873). These feed feed/travel math of every scan
(FULL_PROGRAM_SPEC §5.10) and the calibration slope (`c11e>>1` @10641).

---------------------------------------------------------------------------
## 5. DoCalibration EXPORT (@1668) → FUN_1000c2f0(kind, res) @10109

Export mapping (@1676-1726): kind 0 = flatbed with `res` = 0x960 (2400) when host res
==2400 (or forced), else 0x4b0 (1200) when host res==1200, else 0x258 (600) (@1677-1682);
kind 1 = TPU (`FUN_1000c2f0(1,0x960)`); kind 2 = film (`FUN_1000c2f0(2,0x960)`).

FUN_1000c2f0 flow (flatbed, from cold):
```
if ca58 != 0 (mid-batch): return 1 — calibration skipped                     @10139
wait home FUN_10009cb0                                                        @10142
ca10=1 ; ca14=0                                                               @10146-10148
res < 0x960 : white/dark = temp VirtualAlloc(80000) each                      @10149-10158
res == 0x960: FUN_10008330 → persistent DAT_1004c100/108/110/1b0 (0x1f400 ea) @10161
shading build buffer = VirtualAlloc(280000)                                   @10167
c104=1 & c198=1 if res==0x960 ; c120=0 ; res<600→ca00=1 ; 600≤res<0x4b0→c120=1 @10172-10183
b98c=0 ; c070=0 ; kind→ d1b8/ca44/c070 (film: FUN_10007d30 film-lamp + FUN_1000efa0
   restore CNQ2403w.shd geometry)                                            @10184-10199
ca28=1                                                                        @10200
WHITE:  FUN_10007f10(whiteBuf) = FUN_1000c850(1, whiteBuf)     (§6)          @10203/@10215
DARK:   FUN_10007f30(darkBuf)  = FUN_1000c850(0, darkBuf)      (§6)          @10205/@10217
SHADING: FUN_1000d7d0(whiteBuf, darkBuf, buildBuf)              (§9)          @10220
UPLOAD:  FUN_100080e0(buildBuf, DAT_1004c19c) → SDRAM 0xffffff  (§9)          @10221
if res==0x960: persist whiteBuf+darkBuf (0x1f0e0 B each) → %TEMP%\CNQ2403.shd @10266-10305
   (film: CNQ2403I.shd from c110/c1b0 @10323-10361)
cleanup: free buffers (2400: keep the persistent ones, FUN_10008410 only on failure path;
   @10230-10236 frees temp buffers for <2400)
TPU/film only (@10237-10259): ca04=0; reg2.b7=1; wait home; purge read:
   FUN_10009960(0,0,0); reg2[5:4]=3; 0x1b/0x1c=1; 0x12/0x13=2; GO; SEL;
   UsbRead 10 bytes; wait idle; clear GO shadow (b9a2 &= 0xfd)
```
Note the calibration is ALWAYS 3-channel/16-bit regardless of the scan's color mode, and the
white capture precedes dark — AFE gains and offsets are determined inside the white capture.

---------------------------------------------------------------------------
## 6. THE CAPTURE ENGINE FUN_1000c850(isWhite, outBuf) @10393

Common setup (in exact write order; flatbed values shown, ca1c==0 "fresh calibration" path):
```
(dark only, flatbed) FUN_1000f060(&G0): record green max8 with lamp still ON  @10448-10450
   (FUN_1000f060 @12042: FUN_10009960(0,0x2968,0); reg2[5:4]=3; FUN_10009ae0;
    keep G max8; FUN_10009aa0; GO=0; reg2[5:4]=0)
reg1.b5=0 ; b994=1 (temporarily) ; 0x2f.b7=1                                  @10451-10453
W = c198 ? 0x52d0 : 0x2968  (line buffer W*6 B; 3 channel buffers W*0x28 B)   @10454-10466
FUN_10006d20(3) ; exposure gates 0x33/0x34 = 0x400 ×3 (FUN_10006af0)          @10483-10484
ca1c==0: ca20=0; 0x70..0x75=0; reg8=0xa8; 0x09/0x0a=8; 0x0b/0x0c=8; reg1.b2=1;
         E=0x5400                                                             @10485-10494
ca1c!=0: FUN_10008c10(scanYdpi<601 ? 600 : 0x4b0) (§8) ; E=DAT_1004c07c       @10496-10504
film(c070): E=0xd200; 0x09/0x0a=0x0b/0x0c=0x14; reg8=0xa8; reg1.b2=1; 0x70..75=0 @10506-10514
reg1.b4=0 ; b3=0 ; b7=0 ; b6=0 ; reg5.b2=0                                    @10516-10520
if !ca04: wait home                                                           @10521
WHITE ONLY (isWhite):                                                         @10524-10550
   FUN_10006b20 (read geometry scratch ← SDRAM 0x803e00)
   lamp on FUN_1000f970 ; then FUN_10006230(0, b99c)  → 0x2b=0x00, 0x2c=b99c.lo8,
       0x2d=(b99c>>8)&3   (TPU: FUN_1000ac20 then FUN_10006230(b99e,0); film: FUN_10007d30)
   FUN_10009180()  — AFE GAIN DETERMINATION (§7); includes FUN_100071e0 AFE defaults
   offset search: c120 ? { 0x09/0x0a=4; 0x0b/0x0c=4; FUN_10008c10(600) }
                       : FUN_10008c10(0x4b0)          (§8)
reg6.b7=0 ; reg6.b3=1 ; if !ca04: reg2.b7=1 (park)                            @10552-10556
reg6[5:4]=3 (16-bit) ; reg7[3:0]=1 ; reg7.b4=0 ; reg5[4:3]=(c198)             @10557-10560
resolution class:                                                             @10561-10586
   1200/2400 (c120==0,ca00==0): FUN_10008180(0)  [AFE3=0x1f; 0x52..55=0c,0e,10,12]
   600 (c120): W=0x14b4 ; E>>=1 ; ca1c==0: 0x09/0x0a=4, 0x0b/0x0c=4 ;
               reg5.b2=1 ; FUN_10008180(1)       [AFE3=0x2f; 0x52..55=0c,14,16,00]
   <600 (ca00): W=0xa5a ; E>>=2 ; ca1c==0: 0x09/0x0a=2, 0x0b/0x0c=2 ;
               reg5[4:3]=1 ; FUN_10008180(2) ; reg5.b2=1  [AFE3=0x2f; 52..55=0f,01,13,15]
reg6.b6=0 ; identity matrix FUN_100070a0 ; 0x14=0 (no pixel averaging)        @10587-10589
0x20[3:0]=0 ; if isWhite: 0x20[3:0]=0xb (transient)                           @10590-10593
MOTOR SLOPE  (flatbed, or dark, or ca04):                                     @10594-10628
   0x48[3:1]=0; 0x48[7:5]=0; 0x49[3:1]=0; 0x49[7:5]=0
   class 1200: 0x20[3:0]=3, div=6 | class 2400: 0x20[3:0]=1, div=0xc
   | class 600/<600: 0x20[3:0]=5, div=4
   12-byte mini table → SDRAM 0x803fff: BE32(E*0x18/div | 0x80000000),
     BE32(0x80000000), BE32(0)      (E per class above; e.g. fresh 1200-class:
     0x5400*24/6=0x15000; 2400: 0xA800; 600: 0x2a00*24/4=0xFC00; <600: 0x7E00)
   (TPU white instead: c07c=E; FUN_10006b20; c0e8 = 2.0f (6.0f if 2400-class, 12.0f film);
    Builder-B FUN_1000dc90(600, _DAT_1004c11e>>1) ; 0x20[3:0]=3)              @10629-10642
0x1b/0x1c = 0x14 (20 lines)                                                   @10644
0x20[5:4] = (TPU white && !ca04) ? 1 : 0 ; then =1 anyway because c12c==1     @10645-10655
LAMP: dark → FUN_10007bc0() = { reg 0x60.b1=0 ; reg 0x29.b4=0 ; reg 0x2a.b4=0 } — LAMP OFF
      white flatbed → FUN_1000f970 (lamp on, again) ; TPU/film variants       @10657-10669
reg5[1:0]=1 (color) ; 0x10/0x11 = 0 (NO feed) ; 0x12/0x13 = W                 @10671-10673
c1ac=0x10 ; c130=3 ; reg1.b5 = (USB2.0) ; b994 = (USB1.1)                     @10674-10683
CCD window FUN_100096c0(0x52d0, 0)   → §1 values (0x17/0x18=0x3e on USB2.0)   @10684
FUN_10006d20(3) ; reg2[5:4] = dark ? 3 : 0                                    @10685-10692
(2400 TPU: 0x1d=0x10)                                                         @10693-10695
GO reg2.b1=1 ; SEL 0x24                                                       @10696-10697
READ 20 × (W*6) bytes, de-interleave per §1 into 3 planar u16 buffers         @10698-10774
lamp restore: FUN_1000f970 / FUN_1000ac20 / FUN_10007d30                      @10775-10785
```
Post-processing:
- **Dark (flatbed)** — lamp-recovery wait (@10787-10797): up to 150×
  `{ g = FUN_1000f060(); if g ≥ trunc(0.8 * G0) break; sleep 200ms }`
  (0.8 = double at 0x10018180, recovered from `fild; fmul qword [10018180]` at file 0xd016).
  I.e. after re-lighting the lamp it waits (≤30 s) until the green peak is back to 80 % of
  its pre-dark value. **This is the only in-calibration lamp re-stabilization wait.**
- **White (flatbed & TPU)** — per-pixel per-channel **descending selection sort** of the 20
  line values (@10800-10886), then **trimmed mean of rows 2..17** (drop the 2 brightest and
  2 darkest; divisor 0x10, start row 2 — @10881-10883 `psVar14=0x10; iVar8=2`).
- **Dark** — plain mean of all 20 rows (@10895-10896 `iVar8=0; psVar14=0x14`), no sort.
  (TPU dark adds a 6000 ms settle when ca14&&ca0c @10891-10893.)
- Averages are written to `outBuf` as **planar native-endian u16**: `[ch0 ×W][ch1 ×W][ch2 ×W]`
  (@10907-10954; ch0→outBuf[x], ch1→outBuf[W+x], ch2→outBuf[2W+x]), and mirrored to scratch
  globals DAT_10041ac0/10037520/1002cf80.
- **Dark smoothing + bias (flatbed dark, or ca08)** (@10956-11049): with sub-lane count
  `S = (c198?2:1)` (2400-class pixels alternate two CCD taps → smooth even/odd separately):
  for each sub-lane, per channel: `v'[i] = mean(v[i .. i+min(100, N−i)−1])`; keep `v[i]`
  unchanged if `|v'−v| > 0x14`; finally store `v_final = v' − 0x100` (**subtract 256**,
  @11036-11038). The −256 rides on the offset-search black target of 0x400 (§8) — the stored
  dark is deliberately biased 256 counts below the measured black. Rationale beyond that:
  **UNKNOWN**.
- Teardown (@11051-11076): `0x20[3:0]=0`; free buffers; clear GO shadow bit
  (`b9a2 &= 0xfd`); (TPU white: wait idle); GO=0; wait home (unless ca04); `0x2f.b7=0`;
  b998=1.

---------------------------------------------------------------------------
## 7. AFE GAIN DETERMINATION — FUN_10009180 @8059 (and FUN_10007f90 @7134)

Called once per calibration, at the start of the WHITE capture (@10542). Program:
```
reg5.b2=0                                                     @8092
(TPU: short table + backfeed FUN_10007f50)                    @8093-8098
reg6.b7=0 ; identity matrix ; 0x19/0x1a=1 ; reg6.b3=1
reg1.b4=0 ; b3=0 ; b7=0 ; b6=0 ; reg6.b6=0 ; 0x14=0           @8100-8109
reg5[1:0]=1 ; reg6[5:4]=1 (8-bit) ; reg5[4:3]=0 ; FUN_10008180(0) ; 0x20[5:4]=1  @8110-8114
0x10/0x11=0 ; 0x12/0x13=0x2968   (film: feed 0x1086, width 0x758)                @8115-8123
reg2[5:4]=3 (static) ; reg2.b6=0                              @8124-8125
FUN_100071e0()  ← AFE DEFAULTS: offsets 0x80, gains 0x4b (=1.0×)                 @8126
FUN_10009ae0(&mR,&mG,&mB, …, w=0)  — one line, read 8-bit max counters           @8127
```
If any peak ≥ 0xff (saturated even at 1.0×): run the lamp-PWM search `FUN_10009b80`
(§3 step 7), sleep 500 ms, re-measure (@8129-8135). Flatbed: if all peaks < 8 → error
0xb2 lamp failure (@8140-8142).

**Gain computation** (@8139-8190 + disasm 0x9326-0x9424):
```
K = 210.0 (float @0x10018168) flatbed ; 120.0 (@0x10018164) film
for ch in R,G,B:
    g = K / max8[ch]                      # desired analog gain
    if g <  1.0  (dbl @0x10018158): code = 0x4b
    elif g >= 7.4 (dbl @0x10018150): code = 0xff
    else:          code = trunc(283.0 − 208.0/g)     # dbl @0x10018140 / @0x10018148
AFE[0x28]=code_R ; AFE[0x29]=code_G ; AFE[0x2a]=code_B        @8266-8268
```
So the AFE PGA transfer is `g = 208/(283−code)`; check-points: code 0x4b ⇒ 1.000×,
0xb3 ⇒ 2.000×, 0xff ⇒ 7.43×. **The sniffed gains 96/95/a4 mean the unity-gain white peaks
were ≈134/135/120 out of 255 and the DLL scaled each channel to hit 210/255** (≈82 % FS).
Gains are cached for reuse when c104: `DAT_1004c089/c090/c08a = codes` (@8269-8273).
(TPU also derives normalized per-channel gain ratios and stores film/TPU gain sets
@8192-8264 — not used on flatbed.)
Then GO=0, reg2[5:4]=0, and — for TPU or first-time flatbed — `0x1d=0x10; reg2.b7=1; wait
home` (@8274-8285).

**When is 0x4b used vs computed?** 0x4b (unity) is the power-on default written by
FUN_100071e0 and the floor of the formula; computed codes replace it during every
calibration's white pass. `FUN_10007f90` @7134 is the host-override variant used only on the
TPU path with a host calibration struct (@9746-9749): identical piecewise formula with
`g = hostGainWord/4096` (fmul 1/4096 float @0x10018160; disasm 0x7fa1-0x7ff0). The scan
program itself normally re-writes nothing (FULL_PROGRAM_SPEC §5.18): flatbed scans inherit
the calibration's AFE state.

---------------------------------------------------------------------------
## 8. AFE OFFSET BINARY SEARCH — FUN_10008c10(dpi) @7762

Called with 0x4b0 (1200-class) or 600 (during white capture @10544-10549; also from the scan
build when ca1c @9789-9795, and with the pre-class value @10497-10503). Program:
```
dpi < 0x4b0: reg5.b2=1 ; FUN_10008180(1) ; width=0x14b4
else:        reg5.b2=0 ; FUN_10008180(0) ; width=0x2968       @7797-7807
FUN_10007bc0()  ← LAMP OFF (0x60.b1=0, 0x29.b4=0, 0x2a.b4=0)  @7808
reg6.b6=0 ; 0x14=0 ; reg5[1:0]=1 ; reg6[5:4]=3 (16-bit) ; reg6.b7=0 ; identity matrix
0x19/0x1a=1 ; reg5[4:3]=0                                      @7809-7816
8-byte slope → 0x803fff: bytes 80 00 03 80 80 00 00 00 (=BE32 0x80000380, 0x80000000) @7817-7827
0x1b/0x1c=1 (ONE line) ; reg6.b3=1 ; reg1.b4=0;b3=0;b7=0;b6=0 ; 0x20[5:4]=1
0x10/0x11=0 ; reg7[3:0]=1 ; reg2[5:4]=3 (static) ; reg2.b6=0   @7828-7838
```
Search (@7839-8050). State: `off[3] = {0x80,0x80,0x80}`, `step[3] = {0x40,0x40,0x40}`,
`done[3] = false`, target T=0x400, tolerance ±0x100, **8 iterations**:
```
for it in 0..7:
    for ch: if not done[ch]: AFE[0x20+ch] = off[ch]            @7851-7859
    GO=1 ; wait until reg 0x19/0x1a readback !=0 or reg 0x17/0x18 readback !=0 ; GO=0  @7860-7867
    for ch: reg 0x30 = ch ; r[ch] = FUN_100063e0(1)   (16-bit MIN counter, regs 0x37/0x36) @7868-7873
    if it == 0:                                                 @7874-7920
        e0[ch] = r[ch] − 0x400                 # remembered FIRST error, never updated
        if |e0[ch]| ≤ 0x100: done[ch]=true
        elif e0[ch] ≤ 0:     off[ch] = 0x3f    # reading below target
        else:                off[ch] = 0x40 + 0x80 = 0xc0
    else:                                                       @7921-8013
        e = r[ch] − 0x400
        if |e| ≤ 0x100: done[ch]=true
        else:
            # sign-flip correction against the FIRST error, using the un-halved step:
            if e0[ch] > 0 and e < 0:  off[ch] −= step[ch]
            elif e0[ch] < 0 and e > 0: off[ch] += step[ch]
        step[ch] >>= 1                                          @7991-7994
        if not done[ch]:
            if e < 0: off[ch] −= step[ch]                       @7995-8003
            if e > 0: off[ch] += step[ch]                       @8004-8012
final: AFE[0x20]=off_R ; AFE[0x21]=off_G ; AFE[0x22]=off_B      @8016-8018
```
(The per-channel e in later iterations is held in uVar14/local_14/local_10 as written at
iteration 0 for the flip test; the current-sign moves use the fresh reading. Transcribed
byte-exactly from the decompile; byte arithmetic wraps mod 256.)
Persistence (@8019-8032): film/TPU-3 → DAT_1002cf65/cf64/cf68; if c104 (2400 calib) →
reuse cache DAT_1004c1ae/1c0/1c1; always DAT_1004c080/081/074 = current offsets.
If ca1c==0 the lamp is turned back ON before returning (@8033-8045); GO=0; reg2[5:4]=0.

Semantics: black target = 0x400/65535 (≈1.6 % FS) on the 16-bit min-level counter, tolerance
±0x100; offset code 0x80 is mid-scale, codes >0x80 push the black level down (first
correction for "reading too high" is 0x80→0xc0). The sniffed offsets 90/88/88 are ordinary
converged values ≈ +0x10/+0x08 from mid-scale.

---------------------------------------------------------------------------
## 9. SHADING TABLE — FUN_1000d7d0(white, dark, out) @11083, upload FUN_100080e0 @7192

W selection (@11096-11106): c198→0x52d0 ; ca00→0xa5a ; c120→0x14b4 ; else 0x2968.
Inputs are the planar u16 buffers of §6 (channel stride W). Per pixel x (record layout,
12 bytes, all **big-endian u16**):
```
for ch in 0,1,2:
    w = white[ch*W + x] ; d = dark[ch*W + x]
    span = (ca1c==0) ? (w > d ? w − d : 1)        # normal calibration      @11120-11139
                     : (w != 0 ? w : 1)           # gain-override variant: white only @11141-11155
    K = 0x7d000000 flatbed ; 0x5f000000 film(ca44==3) ; 0x78000000 TPU      @11157-11171
    gain = min(K // span, 0x1fffe) ; gain = (gain + 1) >> 1                 @11172-11187
out[0..5]  = gainR.be16, gainG.be16, gainB.be16
out[6..11] = darkR.be16, darkG.be16, darkB.be16   (the −256-biased darks from §6)  @11191-11196
len += 12 ; if (len & 0x1ff) == 0x1f8: len += 8   # 42 records per 512-B page, 8-B pad @11197-11200
```
DAT_1004c19c = final length. Gain scale: with span = full-scale 0xff00, gain ≈
K/0xff00/2 ≈ 0x4000 → unity is 0x4000 (Q14); K=0x7d000000 makes a pixel at span 0x7d00
(≈49 % FS) get exactly gain 1.0 ⇒ the table normalizes white to ≈2× the measured span
ceiling-capped at 0xffff (≈4×).

Upload `FUN_100080e0(buf,len)` @7192: `reg 0x21/0x22/0x23 = 0xffffff ; SEL 0x24 ;
_UsbWriteScanner(buf, len)`. One table serves all modes; it is re-uploaded per calibration
and again at scan time only on the ca1c rebuild path (§11).

---------------------------------------------------------------------------
## 10. PER-YDPI RESAMPLER — FUN_10008470(ydpi) @7393

Used at scan time to derive shading for the scan's ydpi from the **cached 2400-class
buffers** (DAT_1004c100/108, or c110/1b0 for film) without rescanning: called from
FUN_1000ada0 @9846 (ca1c rebuild) and @9478 (TPU). Output W (@7415-7420):
ydpi≥0x4b1 → 0x52d0 ; else c120 ? 0x14b4 : 0x2968.
- **ydpi == 0x960 (2400)** (@7441-7484): straight copy of white and dark (W*6 bytes each,
  source channel stride = W) → FUN_1000d7d0 → upload.
- **ydpi == 0x4b0 (1200)** (@7486-7539): decimate ×2: for ch, for x in 0..W−1:
  `dst[ch*W + x] = src[ch*2W + 0x44/2… ]` — precisely: source index starts at u16 offset
  **0x44 (=68 pixels)** and steps 2 (`local_10=0x44; uVar5+=2` @7489-7495); source channel
  stride 2W (=0x52d0). Dark is copied the same way only when ca1c==0 (@7506-7508).
- **600-class (c120)** (@7544-7579): decimate ×4 with pair-averaging: source start u16
  offset **0x3c (=60 px)**, step 4; `dst = (src[i]>>1) + (src[i+2]>>1)`; source channel
  stride 4W = 0x52d0. Same for dark when ca1c==0.
- other ydpi with c120==0 falls into the copy path @7443 (LAB_1000852a).
- ca1c!=0 (white-only variants): dark source buffer is swapped in wholesale for the
  FUN_1000d7d0 dark argument (@7518-7539).
Then FUN_1000d7d0 + FUN_100080e0(DAT_1004c19c) (@7587-7589). The 0x44/0x3c starting offsets
are fixed x-phase corrections between resolutions (values are what they are; derivation
**UNKNOWN**).

---------------------------------------------------------------------------
## 11. WHAT PERSISTS, AND WHAT A FROM-COLD CALIBRATION MUST PRODUCE

State surviving between scans inside one driver session (and what the scan program consumes):
1. **AFE register state in the chip** — offsets 0x20-0x22 (from §8), gains 0x28-0x2a (from
   §7). A flatbed scan without override/reuse writes NO AFE gain/offset (FULL_PROGRAM_SPEC
   §5.18) — it depends on calibration having left them programmed. Cached copies:
   offsets DAT_1004c080/081/074 (+ c1ae/1c0/1c1 when c104), gains c089/c090/c08a (when c104);
   the reuse-calibration path FUN_1000dc30 @11313 rewrites all six from that cache.
2. **Shading table in SDRAM 0xffffff** (+ host copy of white/dark planar buffers c100/c108
   for 2400 calibs; %TEMP%\CNQ2403.shd 2×0x1f0e0 B for cross-session reuse).
   DAT_1004c19c length.
3. **Resolution-class flags** ca00/c120/c198/c104 — the scan program keys reg5 bits,
   FUN_10008180 class and W off them (they must match the calibration performed).
4. **Lamp PWM** b99c (≤800, from §3 step 7) — re-written into 0x2b-0x2d by every lamp-on.
5. **Geometry margins** cf66/c082/c11c/c11e/c124 from the edge-find (§4) — feed and motor
   travel of every scan; persisted in SDRAM 0x803dff/0x803e00 and CNQ2403w.shd.
6. **Warm-state 'M'/'B' bytes** in SDRAM 0x803eff/0x803f00.
7. Exposure word c07c (=16000 default; scan sets its own @9703).
   Flatbed calibration computes **no exposure adjustment** (0x70-0x75 stay 0; the TPU-only
   ftol block @9803-9810 writes host gain words c0a8/c0c8/c0ca/c0cc — floats **UNKNOWN**).
Scan-time rebuild path: if a scan overrides AFE gains (ca1c=1: TPU or host gain override),
it re-runs the offset search at the scan class (@9789-9795), re-captures DARK only
(FUN_10007f30 @9842), resamples shading via FUN_10008470(ydpi) (@9846) and re-uploads.

**From-cold native calibration recipe (flatbed, USB 2.0) — execution order:**
```
1  InitializeScanner sequence (§2)                     — lamp ON, gamma, motor ramp
2  Warm-up (§3): static program (FUN_10009960(0,0x2968,1) incl. AFE defaults);
   4 s; sample; (4 s); brightness gate ≥0xd2 (≤5 s); PWM search to peak≤200;
   1 Hz stability ±2 ×3; edge-find with gains 0xb3 (§4)  [skippable if SDRAM says 'M'
   and lamp already on]
3  DoCalibration(res) (§5):
   a WHITE ENTRY: lamp on, PWM(0,b99c); FUN_10009180: AFE defaults → measure max8 →
     gains = trunc(283−208/(210/max8)) (§7)
   b OFFSETS: FUN_10008c10(0x4b0|600): lamp OFF, 1-line static reads of min-counter,
     8-iteration search to 0x400±0x100 → offsets 0x20-0x22 (§8); lamp back ON
   c WHITE capture: static 20-line 16-bit color read at W (§6 program), trimmed mean 16/20
   d DARK capture: record G peak; lamp OFF; 20-line read; mean of 20; lamp ON;
     wait G ≥ 0.8×pre (≤30 s); smooth (100-px window, ±0x14 guard); dark −= 0x100
   e Shading: 12-B/px records, K=0x7d000000, span=white−dark, 512-B page pad (§9);
     upload → 0xffffff
   f 2400 only: keep white/dark buffers for FUN_10008470 reuse (§10)
Outputs: AFE 0x20-0x22, 0x28-0x2a; shading @0xffffff (+len); b99c; class flags; margins.
```

---------------------------------------------------------------------------
## 12. UNKNOWNS (explicit)

- reg 0x08 semantics (0xa8 throughout calibration/init, 0x01 for scans).
- Silicon meaning of the min/max counters (regs 0x34/0x35, 0x36/0x37) beyond observed use;
  whether they latch per-line peaks or something subtler.
- Why the dark values get −0x100 before the shading table (§6); interaction with the 0x400
  offset target is consistent but undocumented.
- The 0x44 / 0x3c resampling phase offsets (§10) — fixed constants, derivation unknown.
- reg 0x20[3:0] codes (0/1/3/5/0xb) — motor/clock divider family, exact meaning unknown.
- TPU exposure-adjust floats @9803-9810 (Ghidra dropped the FPU expressions).
- FUN_10008c10's flip-correction uses the iteration-0 error for all later iterations
  (faithfully transcribed); whether that is intent or a benign quirk is unknowable from
  the binary.
- The white capture runs with run-mode reg2[5:4]=0 and feed 0 (§6): whether the carriage
  physically creeps during the 20 lines (motor GO with mini-slope loaded) or stays parked
  is not determinable from the DLL alone; dark uses mode 3 (static) explicitly.
