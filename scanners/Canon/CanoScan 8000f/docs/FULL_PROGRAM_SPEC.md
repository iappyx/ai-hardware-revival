# CanoScan 8000F (CNQL2403.DLL) — FULL REGISTER-PROGRAM SPEC, MONO vs COLOR

Source of truth: the decompiled scanner firmware (Ghidra output, 479 functions, ImageBase 0x10000000). The decompiled binary is NOT included in this repo.
Every claim cites FUN_xxxxxxxx and a decompiled line number ("@NNNN").
Register-file initial values are quoted from the DLL binary itself (.data at VA 0x1002b9a0).

---------------------------------------------------------------------------
## 0. MONO vs COLOR — SUMMARY

On USB 2.0 the color register program differs from the mono program (same
dpi/depth/area) in exactly two register areas, plus host-side handling:

- **reg 0x05**: `[1:0]` = 1 (color) vs 0/2 (mono); `[7:6]` = 0 (color) vs 1
  (mono "channel-combine"). Color imaging value 0x05, mono 0x46.
- **regs 0x37/0x38** (3×3 channel matrix): **identity** for color, **luma** for
  mono (streamed from the host-supplied matrix, DAT_1002ca2c). Color
  reconstruction — white balance and CCD line-pitch alignment — is done in
  software (see COLOR_AND_BUILDERB_FINDINGS.md).
- Host-side: stride = 3× (@8314) and 3-lane de-interleave (§8).

Everything else is computed with no dependence on the color flag (proof: the
color flag DAT_1004c140 is read in exactly 4 places — see §2/§6):
- **The AFE is never reprogrammed between mono and color.** AFE setup regs are
  written once at InitializeScanner (`FUN_100071e0` @6065: idx 0x04=0x00,
  0x01=0x23, 0x02=0x2c, 0x03=0x1f), and AFE reg 0x03 is re-written per
  *resolution class* by `FUN_10008180` (@7232): 0x2f for <1200 classes, 0x1f for
  1200/2400. The AFE always runs 3-channel; mono channel reduction is done in the
  ASIC via reg 0x05[7:6]=1.
- Exposure, lamp, CCD-phase, CCD-window, pixel-timing, count, motor-table,
  shading and gamma programming: all color-flag-independent.
- Shading and gamma are always 3-channel, same SDRAM addresses/sizes, both modes
  (§7).
- On USB 1.1 only, reg 0x19/0x1a is divided by channel count (3 in color) in
  FUN_100096c0 @8333; on USB 2.0 that register is 0 and even this difference
  disappears.

### Two resolution classes (final observed reg values)

| item | res-class 1 (≤600 dpi) | res-class 0 (1200 dpi) |
|---|---|---|
| CCD arrays | single | two staggered, read together |
| reg 0x07 (Y-step divider) | 600/ydpi | 1 |
| launch run-mode (reg 0x02[5:4]) | 2 | 0 (reg 0x02 = 0x82 at GO) |
| reg 0x20 | 0x50 (0x60 at 150 dpi) | 0x60 |
| reg 0x03 | — | 0xc4 |
| shading width | 0x14b4 (5300 px) | 0x2968 (10600 px, both arrays) |
| motor exposure (Builder B) | 0x2a00 | 0x2a00 (base — NOT the CCD value) |
| CCD line-period exposure | 0x2a00 | 0x5400 (reg 0x09/0x0a = 0x0540) |

Native rungs are 75/150/300/600/1200; 100/200/400/800 are software-resampled
(⅔ of the next native rung) — the hardware is never driven at those rates.

---------------------------------------------------------------------------
## 1. WRITE PRIMITIVES AND SHADOW REGISTER FILE

- `FUN_10005830(reg,bit,v)` — RMW single bit (@4215). `FUN_100058f0(reg,shift,width,v,whole)`
  — field write; whole=1 replaces the whole byte (@4271). Both do
  `_WriteAddressPort_4(reg)` then `_GLWriteEPPDataPort_4(shadow[reg])` and keep a
  **shadow register file at DAT_1002b9a0** (one byte per register).
- `FUN_100059b0(reg)` read; `FUN_100058a0(reg,bit)` read-bit.
- `FUN_10006130()` = `FUN_10004160(0x24)` — SELECT register 0x24 with no data write:
  the latch/execute strobe (used before every bulk transfer and to launch).
- `FUN_10006140(idx,val)` (@4986) — **AFE indirect write**: reg 0x25 = idx&0x3f,
  reg 0x26 = val.
- **Initial shadow image (baked into the DLL .data at VA 0x1002b9a0, file off 0x2b9a0):**
  ```
  00: 00 00 80 c0 07 01 00 01  a9 04 00 04 00 00 00 00
  10: 00 00 00 00 00 80 80 00  00 00 00 ff 3f f0 ff ff
  20: 50 00 00 00 00 00 00 00  00 18 08 4d 4d 11 7f 23
  30: 00 00 00 00 04 00 20 00  00 00 00 00 00 00 00 00
  40: 80 00 d4 00 c0 03 44 09  00 00 00 00 00 00 00 00
  50: fc 2a 10 12 15 18 00 00  00 00 00 00 00 00 00 00
  60: 08 4d 4d 11 00 00 00 00  00 00 00 00 00 00 00 00
  ```
  Note reg 0x05 initial = **0x01** ([7:6]=0), reg 0x15/0x16 initial = 0x80/0x80,
  reg 0x50=0xfc, 0x51=0x2a (0x51 has NO setter — never written by the DLL).
  RMW writes merge into this image, so bits the DLL never touches keep these values.

### Setter map (reg → helper)
reg1: b7=FUN_100059e0 b6=FUN_10005a00 b5=FUN_10005a20 b4=FUN_10005a60 b3=FUN_10005a80 b2=FUN_10005a40 b0=FUN_10005aa0
reg2: b7=FUN_10005ac0(home/park) b6=FUN_10005ae0 [5:4]=FUN_10005b00(run mode) b3=FUN_10005b20 b2=FUN_10005b40 b1=FUN_10005b70(GO) b0=FUN_10005ba0
reg3: b2=FUN_10005bd0 b0=FUN_10005bf0(error stop) ; **b3 read = move-done** (FUN_10005bc0)
reg4: whole byte=FUN_10005c10 (values written: 0x86 @9168,@11948; 0x08 @11223); read=FUN_10005c30 (status: 7/8/0x84=fault)
reg5: [7:6]=FUN_10005c40 (FUN_10005ca0 => =1, "channel-combine") ; b5=FUN_10005c60 ; [4:3]=FUN_10005c80 ; b2=FUN_10005cb0 ; [1:0]=FUN_10005cd0 (FUN_10005cf0=>0 gray, FUN_10005d00=>2 mono-var, FUN_10005d10=>1 color)
reg6: b7=FUN_10005d20 b6=FUN_10005d40 [5:4]=FUN_10005d60(depth code) b3=FUN_10005d80 b0=FUN_10005da0
reg7: b4=FUN_10005dc0 [3:0]=FUN_10005de0 (Y step divider)
reg8: byte=FUN_10005e00 (scan=0x01, calib/init=0xa8; initial 0xa9; semantics **UNKNOWN**)
reg 0x09/0x0a=FUN_10005e20 (base exposure>>4) ; 0x0b/0x0c=FUN_10005e50
reg 0x10/0x11=FUN_10005e80 (feed/start count) ; 0x12/0x13=FUN_10005eb0 (WIDTH px)
reg 0x14=FUN_10005ee0 (pixel-average) ; 0x15=FUN_10005f00 ; 0x16=FUN_10005f20 (gray-mode value)
reg 0x17/0x18=FUN_10005f40 ; 0x19/0x1a=FUN_10005f70 ; reads FUN_10005fd0/FUN_10005fa0
reg 0x1b/0x1c=FUN_10006000 (line count) ; 0x1d=FUN_10006030 ; 0x1e=FUN_10006050 ; 0x1f=FUN_10006070
reg 0x20: [7:6]=FUN_10006090 [5:4]=FUN_100060b0 [3:0]=FUN_100060d0
reg 0x21/0x22/0x23=FUN_100060f0 (SDRAM address ptr, latched by SEL 0x24=FUN_10006130)
reg 0x25/0x26=FUN_10006140 (AFE idx/data)
reg 0x29: b4=FUN_10006170 (flatbed lamp ON; mutually exclusive w/ 0x2a.b4) [3:0]=FUN_100061b0 (lamp intensity nibble)
reg 0x2a: b4=FUN_100061d0 (TPU lamp relay) [3:0]=FUN_10006210
reg 0x2b/0x2c/0x2d=FUN_10006230(a,b): 2b=a.lo8, 2c=b.lo8, 2d=(a>>4)&0x30|(b>>8)&3 (lamp PWM pair)
reg 0x2e=FUN_10006290 ; reg 0x2f: [5:0]=FUN_100062b0(hi3,lo3) b7=FUN_100062e0 (scan enable)
reg 0x30 [1:0]=FUN_10006300 (counter bank select for reads via 0x34/0x35,0x36/0x37)
reg 0x33/0x34=FUN_10006320 (11-bit; written 3× back-to-back = per-channel exposure-gate triple)
reg 0x36=FUN_10006350 (ramp len code) ; 0x37/0x38=FUN_10006370 (3x3 color matrix stream, 9 writes)
reg 0x40/41,0x42/43,0x44/45,0x46/47 = FUN_10006420/6450/6480/64b0 (CCD pixel window)
reg 0x48 [7:5]=FUN_100064e0 [3:1]=FUN_10006500 ; reg 0x49 [7:5]=FUN_10006520 [3:1]=FUN_10006540
reg 0x50 bits b7..b2 = FUN_10006560/6580/65a0/65c0/65e0/6600 (all 0 at init; never set in scan)
reg 0x52/0x53/0x54/0x55 = FUN_10006620/6640/6660/6680 (CCD clock phases; set by FUN_10008180 per res class, and FUN_100076c0 at init: 0xf,0x11,0x13,0x15)
reg 0x60: b7=FUN_100066a0 [5:4]=FUN_100066e0 b1=FUN_100066c0 (TPU lamp) b0=FUN_10006700
reg 0x61/0x62/0x63=FUN_10006720 (TPU lamp PWM pair)
reg 0x64: **b6 read = home/busy sensor** (FUN_10006770) [4:3]=FUN_100067c0 b2=FUN_100067e0 b1=FUN_10006800 b0=FUN_10006820
reg 0x65..0x6d = FUN_10006840/6870/6890/68c0/68f0 (paper-edge/sensor block, used by FUN_10006d20 at init only)
reg 0x70/71,0x72/73,0x74/75 = FUN_10006950/6980/69b0 (per-channel extra exposure)

SDRAM addresses (via reg 0x21-0x23 + SEL 0x24): 0x7fffff master ramp; 0x803fff slope;
0x801fff slope tail; 0x803dff/0x803e00 14-byte geometry scratch (FUN_10006a10/FUN_10006b20);
0x803eff/0x803f00 2-byte 'M'/'B' state flags; gamma 0x81ffff/0x83ffff/0x85ffff (bank A)
and 0x807fff/0x80ffff/0x817fff (bank B); 0xffffff shading; 0 = data readout.

---------------------------------------------------------------------------
## 2. SCAN PARAMETER BLOCK AND THE COLOR FLAG

13 dwords copied to DAT_1004c140.. (FUN_1000ada0 @9338). Filled by FUN_10010240 (@12940):

| param | global | meaning | source |
|---|---|---|---|
| [0].lo | DAT_1004c140 | **colorMode: 0=gray, 1=mono-variant, 2=COLOR** | host pixeltype DAT_1006d028: 0→0, 2→1, 4→2 (@12958-12964) |
| [0].hi | DAT_1004c142 | Y resolution | |
| [1].lo | DAT_1004c144 | X resolution | |
| [1].hi | c144.hi | Y top (1200-dpi units) | |
| [2].lo | DAT_1004c148.lo | Y coordinate | |
| [2].hi | DAT_1004c148.hi | WIDTH in pixels (NOT /3 for color) | |
| [3].lo | DAT_1004c14c | line count → reg 0x1b/0x1c | |
| [3].hi | DAT_1004c14e | bit depth per channel; host bpp/3 for color (`_DAT_1002ce8e /= 3` @12963) | |
| [5].lo | DAT_1004c154 | → reg6 bit0 (@9881) | DAT_1006d0b8 |
| [6].lo | DAT_1004c158 | gray-mode reg 0x15/0x16 value (default 0x80) | DAT_1006d0c0==1 ? DAT_1006d0d9 : 0x80 (@12975) |
| [7] | DAT_1004c15c | TPU enable → DAT_1006d1b8 | |
| [8] | DAT_1004c160 | TPU submode (DAT_1002ca44: 0,2,3) | |
| [9] | DAT_1004c164 | gain override → gain=2.0 | |
| [10] | DAT_1004c168/c16a | batch/multi-strip + reuse-calibration flags | |
| [11] | DAT_1004c16c | → reg6 bit3 (@9911) | |
| [12] | DAT_1004c170 | ptr to calibration/exposure struct | |

**DAT_1004c140 (the color flag) is read in exactly four places:**
1. @9378 `DAT_1004c130 = (sVar3!=2) ? 1 : 3` — channel count.
2. @10016-10035 — the reg 0x05 mode writes (§5.41).
3. @10045 `_DAT_1004c12e = sVar3` — stored for the host read path.
4. @8314 (FUN_100096c0) `if (DAT_1004c140==2) _DAT_1004c092 = (depth>>3)*width*3` — host stride.
Plus DAT_1004c130 consumers: FUN_100096c0 @8333 (USB-1.1-only reg 0x19/0x1a divide),
calibration forces c130=3 (@10676), lamp-edge-find forces c130=1 & mode=1 (@11790-11791).

USB speed flag: `_UsbGetConnectionType_4` (@6410): type 1 → DAT_1004c0e4=0 (USB 1.1),
else c0e4=1 (USB 2.0). Per scan (@9365-9374): `b = (c0e4 != 1)`; reg1 bit5 = !b (1 on
USB 2.0); DAT_1002b994 = b; DAT_1004c150 = b. b994==0 (USB 2.0) → color wire format is
pixel-interleaved; b994==1 (USB 1.1) → planar thirds per line.

---------------------------------------------------------------------------
## 3. MOTOR TABLES (recap, unchanged by color)

Builder A `FUN_10006ef0` (@5906): fixed table &DAT_1002a270 ×0x49a, BE32(trunc(ns/20.8)),
first/last |=0x80, reg 0x36 = 0x49a/8−10, upload → 0x7fffff. Called via FUN_10007010.
Builder B `FUN_1000dc90(Yres, step_count=DAT_1004c118)` (@11331): accel/cruise/decel/tail,
upload → 0x803fff (see BUILDER_B_SPEC.md — byte-exact verified for 75 and 1200 dpi).
Short-move table FUN_10007030: &DAT_1002478c ×0xd0 → 0x7fffff (repositioning).
None of this reads the color flag.

---------------------------------------------------------------------------
## 4. ONE-TIME PROGRAMS AROUND THE SCAN

### 4.1 InitializeScanner (FUN_100076e0 @6386 / thunk @6545)
reg 0x60.b7=1; FUN_100076c0: regs 0x52..0x55 = 0x0f,0x11,0x13,0x15; reg1.b0=1;
reg 0x60[5:4]=2; USB type probe (§2); bridge writes `_WriteRegister_8(0x10,4)`, `(1,0xf)`
(USB-bridge regs, not the scan ASIC); reg5.b5=0; reg 0x48[3:1]=0; 0x48[7:5]=0;
0x49[3:1]=0; 0x49[7:5]=0; lamp on (FUN_1000f970: reg 0x2b/0x2c/0x2d ← (DAT_1002b99e,
DAT_1002b99c), reg 0x29.b4=1); state bytes at 0x803f00/0x803eff; lamp-intensity file
CNQ2403C.shd → reg 0x29[3:0]=0x2a[3:0]=byte>>2 (FUN_10007c00 @6835);
FUN_10007b30: default gains 0x1000 (=1.0 in 4.12); **gamma uploads** (§7.2);
reg 0x20[7:6]=1; reg1.b2=1; reg 0x20[5:4]=1; reg5.b2=0; reg6.b3=1; reg1.b3=0; b4=0;
b7=0; b6=0; reg8=0xa8; reg 0x09/0x0a=8; 0x0b/0x0c=8; regs 0x70-0x75=0;
paper-edge block FUN_10006d20(3); reg 0x50 bits b7..b2=0; master ramp upload
(FUN_10007010); reg 0x2f[5:0]=(1<<3)|2=0x0a; reg3.b2=1.
**AFE full init `FUN_100071e0` runs from the lamp-warm/first-contact paths**
(@8126 in FUN_10009180's chain, @8446 in FUN_10009960 when param_3!=0):
`AFE[0x04]=0x00, AFE[0x01]=0x23, AFE[0x02]=0x2c, AFE[0x03]=0x1f, AFE[0x20..0x22]=0x80
(offset DACs), AFE[0x28..0x2a]=0x4b (PGA gains)`.

### 4.2 Calibration (FUN_1000c2f0 @10107 → captures via FUN_1000c850 @10390)
Calibration ALWAYS runs in **color** (FUN_10005d10 @10671, DAT_1004c130=3 @10676,
16-bit @10675) regardless of the scan's color mode — the shading data always
contains 3 real channels. AFE offset DACs (0x20/0x21/0x22) are auto-tuned by binary
search in FUN_10008c10 (@7760, start 0x40, mid 0x80). White capture FUN_10007f10 /
dark capture FUN_10007f30 (→ FUN_1000c850(1/0)). Buffers DAT_1004c100 (white) /
DAT_1004c108 (dark), 0x1f400 bytes each = 3ch × up-to-0x52d0 px × 2B, persisted to
%TEMP%\CNQ2403.shd. (Full detail in CALIBRATION_SPEC.md.)

### 4.3 Teardown FUN_10009da0 (@8666)
reg 0x2f.b7=0; conditional GO=0 + park (reg2.b7=1); shadow reg2 GO bit cleared
directly (`DAT_1002b9a2 &= 0xfd`); reg7.b4=0; reg 0x60.b0=0; 0x60.b1=0;
regs 0x48[3:1]/[7:5]=0, 0x49[3:1]/[7:5]=0; TPU lamp restore.

---------------------------------------------------------------------------
## 5. THE SCAN PROGRAM — FUN_1000ada0(param*) IN ORDER

Pseudocode parametric in (xdpi=c144.lo, ydpi=c142, ytop=c144.hi, width=c148.hi,
lines=c14c, depth=c14e, mode=c140, TPU=c15c/c160, batch=c168/c16a). uVar4=xdpi,
uVar15=ydpi. Flatbed path; TPU deltas inline. WRITE ORDER preserved.

```
 1  copy 13 params → 0x1004c140..                                    @9338
 2  dpi_fudge = {75/150:0x445, 300:0x131, 600:0x6f, 1200:0, else:7}  @9346-9361
 3  W reg1.b5   = (USB2.0)                    FUN_10005a20           @9367
 4  chan = (mode==2) ? 3 : 1  → DAT_1004c130                         @9378
 5  gain_override→DAT_1004c084; TPU flags; alloc shading bufs FUN_10008330 @9391
 6  first time: FUN_10009d20 wait ASIC ready (reg 0x64.b6 poll)      @9452
 7  ca00=0 if ydpi<1200; c120=(ydpi<1200)                            @9460-9464
 8  FUN_10006b20: read 14-byte geometry scratch ← SDRAM 0x803e00     @9519
 9  LAMP: flatbed FUN_1000f970: W 0x2b/0x2c/0x2d ← (b99e,b99c); W 0x29.b4=1
    TPU: FUN_1000ac20: W 0x2b/2c/2d swapped; W 0x2a.b4=1; W 0x60.b1=0 @9521-9535
10  travel DAT_1004c118 = (c082 + c124)/2  (device geometry)         @9537-9543
    feed  = per handoff: ydpi==2400: 2*CF66+ytop+0x43 ; 1200: ytop/2+CF66 ;
            else (ytop/2+10+CF66) / (1200/ydpi)                      @9545-9572
    batch/multi-strip repositioning (c168): may run FUN_10007110 backfeed
    (0x48/0x49 fields=0, reg2.b6, 0x1b/1c, 0x1d=0x40, 0x20[5:4]=1, reg2.b3,
     reg2.b2 pulse) and adjust feed/travel                           @9573-9607
11  channel gains c0e8/c0f4/c0fc/c0f8: 1.0 flatbed (2.0 if override)
    (TPU: param12 table or 2.5 film)                                 @9609-9689
12  W reg8 = 0x01                              FUN_10005e00          @9691
13  base = 0x5400; if ydpi<1200 base=0x2a00; if ca00 base=0x1500     @9692-9700
    ca20 = (xdpi>149)                                                @9702
    exposure: c07c = ftol(f(gain,base)) & ~0x1f                      @9703-9704
    per-ch extras c138/c1a8/c1a4 = ftol(per-channel delta terms)     @9705-9710
14  W reg1.b2  = (all extras==0)               FUN_10005a40          @9717
15  W 0x09/0x0a = c07c>>4                      FUN_10005e20          @9718
16  W 0x0b/0x0c = extras all 0 ? c07c>>4 : (ydpi<=1200 ? base>>4 : base*2>>4) @9719-9728
17  W 0x70/71=c138>>4; 0x72/73=c1a8>>4; 0x74/75=c1a4>>4              @9729-9731
18  AFE gain/offset refresh — CONDITIONAL, resolution/TPU/override-keyed,
    NOT color-keyed                                                  @9733-9827
      flatbed, no override, not reuse: NOTHING WRITTEN (AFE keeps calibration
        state: offsets from FUN_10008c10 search, gains 0x4b or stored)
      override (c084): AFE[0x28]=DAT_1002cf69, [0x29]=DAT_1004c1c2,
        [0x2a]=DAT_1004c088 ; ca1c=1                                 @9821-9827
      reuse-calib flatbed (c16a==1): FUN_1000dc30: AFE[0x20..0x22]=
        DAT_1004c1ae/c1c0/c1c1, AFE[0x28..0x2a]=c089/c090/c08a       @9486,@11316
      TPU paths: stored per-submode gain triples (+film offsets)     @9767-9817
    if AFE was touched (ca1c) and not batch-continuation: re-run dark capture
      FUN_10007f30 + rebuild&upload shading FUN_10008470(ydpi)       @9838-9851
19  VirtualAlloc 1MB line buffer                                     @9856
20  W reg5.b2 = (ydpi<1200)                    FUN_10005cb0          @9863
21  resolution class:                                                @9864-9880
      ca00 (quarter-res, only from a <600dpi calibration):
           W reg5[4:3]=1; W reg5.b2=1; FUN_10008180(2)
      res-class 0 (ydpi>=1200): W reg5[4:3]=(ydpi==2400); W reg5.b2=0; FUN_10008180(0)
      res-class 1 (else):       W reg5[4:3]=0;  W reg5.b2=1; FUN_10008180(1)
    FUN_10008180(k) @7232:  k=2: AFE[3]=0x2f; W 0x52=0x0f,0x53=0x01,0x54=0x13,0x55=0x15
                            k=1: AFE[3]=0x2f; W 0x52=0x0c,0x53=0x14,0x54=0x16,0x55=0
                            k=0: AFE[3]=0x1f; W 0x52=0x0c,0x53=0x0e,0x54=0x10,0x55=0x12
22  W reg6.b0 = (param5!=0)                    FUN_10005da0          @9881
23  TPU: recompute stored gain words c0b4/c0b8 (ftol)                @9882-9901
24  W reg3.b2 = 1                              FUN_10005bd0          @9902
25  W 0x20[5:4] per res class → reg 0x20 byte 0x50 (0x60 at 150 dpi) for
    res-class 1, 0x60 for res-class 0 (see §0 table)                 @9903-9909
26  W reg6.b6 = 0                              FUN_10005d40          @9910
27  W reg6.b3 = (param11!=0)                   FUN_10005d80          @9911
28  W reg2.b7 = (not batch-continuation)       FUN_10005ac0          @9913
    if fresh: wait home FUN_10009cb0 (poll reg3.b3)                  @9914
29  W 0x20[3:0]=0; if xdpi>=300 && flatbed && USB1.1: W 0x20[3:0]=1  @9919-9923
30  FUN_100070f0(xdpi): Builder A upload (incl. W reg 0x36) then
    Builder B FUN_1000dc90(xdpi, travel) upload → 0x803fff           @9924
31  if ydpi>=2400: feed = (feed&0xffff)/2*2 (round even)             @9927
32  W 0x12/0x13 = width                        FUN_10005eb0          @9930
33  W 0x10/0x11 = feed                         FUN_10005e80          @9931
34  W reg6[5:4] = depth code                   FUN_10005d60          @9932-9948
      USB2 (c10c==0): depth 8→1 ; 14/16→3.  USB1.1: depth 8→0 ; 14/16→3
35  W reg7[3:0] = Y step divider               FUN_10005de0          @9949-9968
      res-class 0 (1200 dpi): reg 0x07 = 1  (see §0 table)
      res-class 1: 600/ydpi (ca00 quarter-res: 300/ydpi)
36  W reg7.b4 = 0                              FUN_10005dc0          @9969
37  lines += 4 if xdpi==2400, += 8 if xdpi==4800
    W 0x1b/0x1c = lines                        FUN_10006000          @9970-9976
38  avg = (xdpi==4800) ? 0x40 : 0x20/(2400/xdpi)
    W 0x14 = avg                               FUN_10005ee0          @9977-9987
39  t = {2400/4800:0x88, 1200:0x90, 600: (ydpi<1200?0x60:0x30), 300:0x90, else:0xc0}
    W 0x1e = t ; W 0x1f = t                    FUN_10006050/6070     @9988-10007
40  exposure gates: flatbed W 0x33/0x34 = 0x46e, then 0x469, then 0x447 (R,G,B triple)
    TPU: 0x400,0x400,0x400 (FUN_10006af0)                            @10008-10015
41  ***COLOR-MODE SWITCH*** (reg 0x05 mode bits)                     @10016-10035
      mode==0 (gray):   W reg5[1:0]=0 ; W 0x15=v ; W 0x16=v  (v=param6 or 0x80)
                        W reg5[7:6]=1
      mode==1 (mono):   W reg5[1:0]=2 ; W reg5[7:6]=1
      mode==2 (COLOR):  W reg5[1:0]=1          (reg5[7:6] NOT written; stays 0)
42  W reg1.b3=1 ; W reg1.b4=1                  FUN_10005a80/a60      @10036-10037
43  W reg1.b7 = (depth != 16)                  FUN_100059e0          @10038
44  W reg1.b6 = (TPU)                          FUN_10005a00          @10039
45  FUN_100096c0(width, avg)  → CCD window + timing                  @10040, fn @8299
      host stride if mode==2: c092 = (depth/8)*width*3               @8314
      USB2.0 (b994==0):
        n = (avg*width + 0x400) >> 10 ; m = (0xf7f − 3n)/3
        W 0x40/41 = 0x80 ; 0x42/43 = m+0x80 ; 0x44/45 = n+0x80+2m
        W 0x46/47 = 0 ; W 0x17/18 = min(0xfff, (m<<10)/width − 1) ; W 0x19/1a = 0
      USB1.1 (b994!=0):
        n = (avg*width + 0x400) >> 10 ; m = (0xf7f − 3n)/6
        W 0x40/41=0x80 ; 0x42/43=m+0x80 ; 0x44/45=n+0x80+2m ; 0x46/47=3(n+m)+0x80
        W 0x17/18 = min(0xfff, (m<<10)/width − 3)
        W 0x19/1a = min(0x1fff, (0x7fb000 − 0x800*(3(n+m)+0x80)) / (chan * bytesPerLinePerChan) − 1)
        *** chan = DAT_1004c130 = 3 for color — the only chan-dependent register, USB1.1 only ***  @8333
46  W 0x1d = 0x10                              FUN_10006030          @10042
47  store c12e=mode, c192=depth, ca18=ydpi (host globals)            @10043-10045
48  k = (xdpi<150) ? 0 : 4 ; W 0x49[3:1]=k ; W 0x48[7:5]=k           @10046-10054
49  color matrix regs 0x37/0x38 (9 Q13 words, column-major):        @10055-10060
      COLOR: identity FUN_100070a0 (0x2000,0,0, 0,0x2000,0, 0,0,0x2000)
      MONO: host-supplied luma matrix via FUN_10008250 (DAT_1002ca2c)
      (matrix is written in both modes; the mono luma matrix collapses all 3
       output lanes to luma — this is why color needs identity, see
       COLOR_AND_BUILDERB_FINDINGS.md)
50  TPU film: lamp settle waits + FUN_10006720 lamp PWM              @10061-10075
51  LAUNCH:                                                          @10077-10086
      W reg2[5:4] = (xdpi<1200) ? 2 : 0        FUN_10005b00
        → run-mode 2 for res-class 1, run-mode 0 for res-class 0 (reg 0x02 = 0x82 at GO)
      W 0x2f.b7 = 1                            FUN_100062e0
      W reg2.b1 = 1 (GO)                       FUN_10005b70
      SEL 0x24                                 FUN_10006130
52  batch first-strip: sleep 1s, W reg2.b7=1, SEL 0x24              @10087-10092
53  free shading bufs; c0ec=1 if xdpi>=2400                          @10093-10096
```

---------------------------------------------------------------------------
## 6. MONO vs COLOR DIFF TABLE (the complete set)

| # | item | MONO (mode 0 / 1) | COLOR (mode 2) | where |
|---|------|-------------------|----------------|-------|
| 1 | reg 0x05 [1:0] | 0 (gray) / 2 (mono-var) | 1 | @10016-10035 |
| 2 | reg 0x05 [7:6] | =1 (combine) | 0 (stays initial) | @10021/10026/10031 vs @10034 |
| 3 | reg 0x15 / 0x16 | mode 0 only: v=param6\|0x80 | not written | @10018-10025 |
| 4 | regs 0x37/0x38 matrix | luma (host-supplied) | identity | @10055-10060 |
| 5 | reg 0x19/0x1a | USB1.1 only: /(chan=1) | /(chan=3) | @8333; USB2.0: both 0 |
| 6 | DAT_1004c130 (SW) | 1 | 3 | @9378 |
| 7 | host stride c092 (SW) | (depth/8)*width | (depth/8)*width*3 | @8314 |
| 8 | host demux (SW) | 1 lane copy | 3-lane interleave/merge | FUN_10008930 @7668-7744, FUN_10009f20 |

Registers explicitly checked identical between modes: reg1, reg2, reg3, reg4,
reg5[5:2] (resolution-keyed), reg6, reg7, reg8, 0x09-0x0c, 0x10-0x13, 0x14,
0x17/0x18, 0x1b-0x1f, 0x20, 0x2b-0x2d, 0x33/0x34 triple, 0x36, 0x40-0x49,
0x52-0x55, 0x70-0x75, all AFE registers, motor tables, gamma, shading.

---------------------------------------------------------------------------
## 7. TABLES: SHADING & GAMMA (mono vs color: NO DIFFERENCE)

### 7.1 Shading (upload → SDRAM 0xffffff, length DAT_1004c19c)
Built by FUN_1000d7d0 (@11081) from white buf (param_1) & dark buf (param_2), both
ALWAYS 3-channel (§4.2). Per-pixel record = **12 bytes: gainR,gainG,gainB,darkR,
darkG,darkB, each big-endian u16**, laid out from 3 planar channel arrays (ch stride
= W px):
```
for x in 0..W-1:
  for ch in R,G,B:
     span = (dark_present && white>dark) ? white−dark : max(white,1)   # ca1c picks variant
     K    = 0x7d000000 flatbed ; 0x5f000000 TPU-film(submode3) ; 0x78000000 TPU
     gain = min(0x1fffe, K/span) ; gain = (gain+1)>>1                  # u16 BE out
  then 3 dark u16 BE
  out += 12 ;  if (out & 0x1ff) == 0x1f8: out += 8 more (skip to next 512B page) @11198
W = 0x52d0 (2400), 0x2968 (res-class 0 = 1200), 0x14b4 (res-class 1 = 600/c120), 0xa5a (ca00) @11095-11105
```
Per-resolution resampling of the calibration buffers first: FUN_10008470 (@7391):
1200/2400 copy 1:1; 600 subsample ×2 from offset 0x44 with per-channel strides;
300 average pairs from offset 0x3c. Upload via FUN_100080e0: addr ptr 0xffffff +
SEL 0x24 + bulk OUT (@7192).

### 7.2 Gamma (identity, uploaded at init, never per-scan)
FUN_10007250 (@6083): 0x10000-entry u16 ramp 0..0xffff, uploaded 4× via FUN_100073b0
(0x20000 B each) to {0→0x83ffff, 1→0x81ffff, 2→0x83ffff, 3→0x85ffff}.
FUN_10007490 (@6227): second bank via FUN_100075f0, length>>2, to
{0→0x80ffff, 1→0x807fff, 2→0x80ffff, 3→0x817fff}. Host-supplied curves go through
the same two writers (@13878). Both modes: all tables uploaded, identical content.

---------------------------------------------------------------------------
## 8. HOST-SIDE PIXEL PATH

FUN_10008930 (@7602) per line: FUN_10009f20 fills up to 3 lane buffers, then:
- mode 0: pack 1-bit (width+6)/8 bytes
- mode 1: depth 14/16 → byte-swap u16 per px; depth 8 → 1 byte per px
- mode 2: depth 14/16 → 6 B/px [Rhi,Rlo,Ghi,Glo,Bhi,Blo]; depth 8 → 3 B/px [R,G,B]
FUN_10009f20 (@8731): at ydpi==2400 merges strip pairs via DAT_1002cf58/5c/60
buffers; USB1.1 (b994=1) reads planar thirds, USB2.0 pixel-interleaved.

Colour reconstruction (lane order R,G,B; CCD line-pitch alignment; white balance)
is done in software — see COLOR_AND_BUILDERB_FINDINGS.md.

---------------------------------------------------------------------------
## 9. AFE — FULL PICTURE

Access: ASIC reg 0x25 (index, &0x3f) / 0x26 (data), FUN_10006140. Register usage
matches the Wolfson WM814x family map: the DLL binary-searches 0x20-0x22 against the
black level (FUN_10008c10) → **0x20-0x22 = per-channel OFFSET DACs**, and writes
0x28-0x2a with 0x4b default / 0xb3 for the lamp-edge scan → **0x28-0x2a =
per-channel PGA GAINS**.

Complete list of AFE writes:
| site | writes | when |
|---|---|---|
| FUN_100071e0 @6065 | 04=00, 01=0x23, 02=0x2c, 03=0x1f, 20/21/22=0x80, 28/29/2a=0x4b | init / lamp-warm prep |
| FUN_10008180 @7232 | 03 = 0x2f (res class 1,2) / 0x1f (res-class 0 = 1200/2400) | every program build, keyed on RESOLUTION |
| FUN_10008c10 @7847-7858 | 20/21/22 binary search | calibration |
| FUN_10007f90 @7182 | 28/29/2a from host-supplied gains ×0x4b | TPU w/ host calib struct |
| FUN_10008250-area @8266 | 28/29/2a computed | host color-adjust path |
| FUN_1000ada0 @9755-9826 | 28/29/2a (+20-22 film) restore from stored calib | TPU / override / reuse only |
| FUN_1000dc30 @11316 | 20/21/22 + 28/29/2a restore | flatbed reuse-calibration |
| FUN_1000ea90 @11785 | 28/29/2a = 0xb3 | lamp edge-find |

There is NO AFE mode write keyed on the scan's color mode. AFE setup bytes are
constants; the AFE always runs 3-channel. Mono is made in the ASIC (reg 0x05[7:6]=1
combine + [1:0] mode). Exact bit meaning of 0x23/0x2c/0x1f/0x2f: **UNKNOWN** (only
the resolution-keyed 0x1f↔0x2f bit4 toggle is observed).

---------------------------------------------------------------------------
## 10. UNKNOWNS (explicit)

- reg 0x08 semantics (0x01 scan / 0xa8 calib+init / 0xa9 initial).
- reg 0x15/0x16 semantics (gray-mode value, default 0x80).
- Exact float expressions feeding the exposure `__ftol`s @9703-9710 (Ghidra dropped
  the FPU args); structure and defaults are known (16000 base, extras=0 when channel
  gains equal).
- AFE setup-byte bit meanings (0x23/0x2c/0x1f/0x2f) beyond the observed toggles.
- reg 0x05[1:0] CCD-level difference between 0 (gray) and 2 (mono-variant); both set
  combine=1; mode 0 additionally programs 0x15/0x16.
