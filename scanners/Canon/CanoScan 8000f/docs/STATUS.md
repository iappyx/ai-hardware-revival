# scan8000f — status & architecture

Current state of the driver and how it's wired. Deep register/algorithm specs
are in the other `docs/*.md`; this file is the map on top of them.

## What this is

A clean-room, pure-Python (pyusb/libusb) driver for the Canon CanoScan 8000F. No
Canon software, no emulator, no replayed capture data at run time — device init,
lamp warm-up, AFE calibration, shading, motor tables and the per-scan register
program are all **generated** from logic recovered by reverse-engineering the
vendor driver (CNQL2403.DLL). A CLI (`scan8000f.py`) and a Tkinter GUI (`gui.py`)
sit on top.

## Status

All resolutions work in colour / gray / line-art, 8- and 16-bit.

Native hardware rungs are **75 / 150 / 300 / 600 / 1200** — the only resolutions
the vendor firmware has motor slope + home-decel tables for. **100 / 200 / 400 /
800** are software-resampled from the next native rung (each is exactly ⅔ of a
native one), the same way ScanGear produces them: the motor is driven at the
native rung, then the image is LANCZOS-resampled to the requested size on export.

16-bit is preserved in PNG / TIFF / RAW; JPEG and PDF are 8-bit (the GUI greys
them out at 16-bit depth). Colour uses Canon factory colorimetry (matrix baked
into `imaging._C`); calibration runs fresh every scan.

## Architecture

```
scan8000f.py     CLI ("scan" subcommand) + GUI launcher ("gui")
gui.py           Tkinter: dropdowns, format checkboxes, live preview, bg thread
driver.py        USB + native pipeline. API: open_device(), scan(), close_device()
imaging.py       decode raw -> PIL, multi-format export, preview_image()
motor_tables.py  build_master_ramp() / build_slope() / build_hometail()
```

`driver.scan(dpi, mode, depth, progress=None, preview=None, trace=False)
-> (raw_bytes, meta)` runs the whole pipeline:

```
native_init → native_warmup → native_calibrate → lamp on → imaging registers →
emit_motor_tables → emit_scan_program(GO) → stream → wait home → reactive home
```

`meta` keys: `dpi` (requested), `scandpi` (native rung actually driven), `width`,
`lines`, `channels`, `depth`, `lineart`, `stride`, `mode`, `out_width`/`out_lines`
(resample target for 100/200/400/800). The `.raw.meta` sidecar mirrors it so the
renderer is self-describing. `trace=True` writes every USB transfer to
`last_scan_trace.txt` (diagnostic; off by default).

## The two resolution classes

The 8000F has two scan classes, and almost everything resolution-dependent keys
off which one you're in:

- **Res-class 1 (≤600 dpi)** — single CCD array. Y-step divider `reg07 = 600/ydpi`,
  launch run-mode 2, `reg20 = 0x50` (0x60 for 150), shading width `0x14b4` (5300 px),
  motor + CCD both on base exposure `0x2a00`.
- **Res-class 0 (1200 dpi)** — two staggered CCD arrays read together. `reg07 = 1`,
  launch run-mode 0 (`reg02 = 0x82`), `reg20 = 0x60`, reg05 bit6 (dual-array
  combine), shading width
  `0x2968` (10600 px, both arrays). The CCD integrates at exposure `0x5400`
  (reg09/0a = `0x0540`) for fine detail, but the **motor is driven from the base
  exposure `0x2a00`** (see below).

800 dpi resamples from 1200, so it uses res-class 0.

`reg03 = 0xc4` at the imaging launch at **every** resolution — bits [7:6]=11 are the
ASIC power-on shadow default (byte 3 = `0xc0`) that the vendor preserves through
every reg03 RMW; the driver sets them explicitly since its register image starts at 0.

## The 1200/800 motor fix (the hard one)

Symptom: the 1200 carriage covered only ~half the plate and the image came out
stretched ~2× vertically. Cause, found by extracting the vendor's **actual
uploaded motor slope table** from a ScanGear 1200 USB capture and comparing bytes:
our motor cruise-period word was `0x8001f800` (129024) where the vendor's is
`0x8000fc00` (64512) — exactly double. A motor that steps half as often covers
half the plate.

The cruise period is computed from an exposure. We were feeding it the CCD's 1200
exposure `0x5400`; the vendor feeds it the **base exposure `0x2a00`** (the sensor
integrates twice as long per line for detail, but the motor keeps stepping at the
600-dpi rate). Fix, in `driver.scan()`, for res-class 0 only:

```
motor exposure 0x2a00   (not the CCD's 0x5400)
travel 1302             (carriage travel; byte-exact vs the 150 and 1200 captures)
tail gain 0.5           (final decel ramp)
```

With these three, `motor_tables.build_slope(1200, exposure=0x2a00, travel=1302,
chan_gain=0.5)` reproduces the vendor's uploaded slope table **byte-for-byte
(all 2339 words)**. Res-class 1 (≤600) is unchanged.

Colour also needs the three CCD colour rows realigned: at 1200 they land 16 scan-
lines apart (32 for R↔B), so `imaging._decode_float` cross-correlates and shifts
them back together — the search window is widened for res-class 0 to clear the
larger offset.

## Key USB / register facts

- **Register I/O:** select-addr `ctrl(0x40,0x0c,0x83,0,[reg])`, write
  `…0x85…[val]`, read `ctrl(0xc0,0x0c,0x84,0,1)`; bulk arm
  `ctrl(0x40,0x04,0x82,0,[dir,0,0x82,0,size32LE])`; commit/latch = select `0x24`.
- **Status:** home sensor = reg `0x64` bit6 (clear = home); move-complete = reg
  `0x03` bit3 (set = done). Motor: reg `0x02` [5:4] run-mode, b7 NOTHOME, b1 GO.
- **Colour:** reg `0x05`[1:0] = 1; wire is pixel-interleaved R,G,B. Matrix regs
  `0x37/0x38` = identity (we do colour in software).
- **16-bit** is little-endian on the wire (`<u2`).
- **Motor tables** = master accel ramp (SDRAM `0x7fffff`) + imaging slope
  (`0x803fff`) + home-decel tail (`0x801fff`); all generated, byte-exact vs the
  vendor captures.

## Run / debug

```
pip3 install -r requirements.txt
python3 scan8000f.py gui
python3 scan8000f.py scan --dpi 1200 --mode color --format png --out ~/Desktop/scan
```

- GUI log panel shows the full warm-up/calibration trace and any traceback.
- If the carriage parks out after a failure, power-cycle to re-home.
- `--trace` / the GUI checkbox writes `last_scan_trace.txt` for diffing against a
  vendor capture.

## Known deviations from the vendor (intentionally not "fixed")

A full three-way audit (decompiled DLL + raw captures + driver) found four places
where the driver differs from CNQL2403.DLL. All are benign — verified either
byte-for-byte equivalent in effect, or absorbed downstream — and were left as-is
because matching them carries regression risk with no output benefit. Documented
here so they aren't re-discovered as "bugs":

1. **Feed / top-margin (reg 0x10/0x11).** Off by 1–2 motor steps vs the vendor
   (288 vs 286 at 1200; 36 vs 37 at 150) — a sub-line (~0.04 mm) shift. It derives
   from the same carriage-geometry computation as the travel value; an exact match
   needs the full geometry (c082/c124) reversed. Cosmetic.
2. **AFE offset-search step direction.** The DLL keys the base step on the *first*
   iteration's black-counter error; the driver keys it on the *current* error.
   Differs on ~8% of response curves, but the following per-pixel dark-frame
   subtraction absorbs any residual, so scan output is identical. The decompiled
   logic has ambiguous variable aliasing, so a "match" risks the calibration.
3. **reg05 bit6 at res-class 1 (≤600).** The vendor's reg05 is 0x45 (bit6 set) at
   ≤600 vs our 0x05. That bit is an unintentional leftover from the mono
   calibration passes that the vendor never clears, and it is a don't-care in
   colour mode ([1:0]=1). Not replicated (it's an artifact, not a setting).
4. **1200 calibration capture runs the 600-class program.** At 1200 the white/dark
   shading capture uses res-class-1 CCD-phase/AFE/motor settings while gain and
   offset use res-class-0. Self-normalizing (shading = K/(white−dark) with matched
   white/dark settings, so exposure/gain cancel), which is why 1200 scans are
   correct. A latent inconsistency, not an active defect.

## Possible next steps

- **Both-sided region savings (feed the carriage to the selection).** Region scan
  today only shortens the *tail*: it always starts the carriage at the home edge
  and stops early once it's past the selection, then software-crops. So a box near
  the **bottom** of the bed saves the most time and a box at the **top** saves
  nothing (it's the last thing the carriage reaches — see the README note). The
  ideal is to also skip the lead-in — fast-feed the carriage straight to the
  selection's start edge, then scan only the selection height, for savings at any
  position. The vendor has a reposition move for this (`FUN_10007110`: reg1b/1c =
  step count, reg1d = 0x10, reg20[5:4] = 1, poll reg03 bit3 for move-done), but a
  faithful clean-room replica did **not** drive the carriage in two on-hardware
  tests — the motor stayed at 0. The missing piece isn't in the decompile. Cracking
  it cleanly needs a **ScanGear USB capture of an actual marquee/region scan**; all
  captures we have are full-bed, so none of them show the reposition sequence. Until
  then the current tail-only version is the safe, working behaviour.
- **Film / negative / slide scanning (TPU).** The 8000F's lid holds a transparency
  unit with its own lamp for scanning negatives, slides and film strips. The vendor
  driver has the TPU light/backlight path; wiring it up needs the lid adapter and
  film on hand to reverse and test the lamp-select + shading against a clear frame.
- Propagate the 1200 motor fix to the Android port.
- Polish: progress-bar accuracy, cancel button, batch/multi-page.
