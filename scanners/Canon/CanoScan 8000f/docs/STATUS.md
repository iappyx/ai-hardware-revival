# scan8000f — development status & findings

Living handoff doc. Snapshot of what works, what doesn't, every fix applied so
far, and where to pick up. Read this first when resuming.

Last updated: 2026-08-11.

---

## 1. What this is

A clean-room, pure-Python (pyusb/libusb) driver for the Canon CanoScan 8000F on
Apple Silicon. No Canon software, no emulator, no replayed capture data at run
time — device init, lamp warm-up, AFE calibration, shading, motor tables and the
per-scan register program are all **generated** from firmware logic recovered by
reverse-engineering CNQL2403.DLL. A CLI (`scan8000f.py`) and a Tkinter GUI
(`gui.py`) sit on top.

The deep register/algorithm specs are in this `docs/` folder:
`FULL_PROGRAM_SPEC.md`, `CALIBRATION_SPEC.md`, `BUILDER_B_SPEC.md`,
`COLOR_AND_BUILDERB_FINDINGS.md`. This file is the *current state + how the code
is wired*, on top of those.

---

## 2. Status matrix (hardware-verified unless noted)

Native hardware rungs = **75 / 150 / 300 / 600 / 1200** (the DLL's motor generator
has slope+tail tables for exactly these — see §5d). The intermediate ScanGear
resolutions (100/200/400/800) are **software-resampled** from the next native rung
(each is ⅔ of a native one), exactly as the vendor does it.

| dpi  | colour | gray | line-art | notes |
|------|--------|------|----------|-------|
| 75   | ✅     | ✅   | ✅       | native, full bed, homes clean |
| 100  | ✅     | ✅   | ✅       | resample of native 150 (×⅔) |
| 150  | ✅     | ✅   | ✅       | **native, hardware-verified.** Overshoot fixed by `emit_motor_tables` reg20 0x50→0x60 (see fix 0). |
| 200  | ✅     | ✅   | ✅       | resample of native 300 (×⅔) |
| 300  | ✅     | ✅   | ✅       | native, verified end-to-end |
| 400  | ✅     | ✅   | ✅       | resample of native 600 (×⅔) |
| 600  | ✅     | ✅   | ✅       | native (104 MB colour) |
| 800  | 🧪     | 🧪   | 🧪       | resample of 1200 (×⅔) — inherits 1200's experimental status |
| 1200 | 🧪     | 🧪   | 🧪       | **native fix applied from the ScanGear 1200 capture** (reg07 divider 2→1, launch run-mode 2→0). Full imaging program now matches the capture register-for-register. Needs a hardware test run to confirm data streams. |

Bit depth: **8 and 16** both work (16-bit is little-endian on the wire;
preserved in PNG/TIFF, downconverted for JPEG/PDF).
Export: **PNG, TIFF, JPEG, PDF, RAW** all work. Colour uses Canon factory
colorimetry (CNS24G.ICC matrix baked into `imaging._C`).

Recommended/solid lineup: **75 / 150 / 300 / 600 native; 1200 experimental (fix applied, awaiting hardware confirmation).**

---

## 3. Program architecture

```
scan8000f.py   CLI ("scan" subcommand) + GUI launcher ("gui")
gui.py         Tkinter: dropdowns, format checkboxes, live preview, bg thread
driver.py      USB + native pipeline. Public API: open_device(), scan(), close_device()
imaging.py     decode raw -> PIL, multi-format export, preview_image()
motor_tables.py  build_master_ramp() / build_slope() / build_hometail() / imaging_travel()
```

`driver.scan(dpi, mode, depth, progress=None, preview=None) -> (raw_bytes, meta)`
runs the whole pipeline:
`native_init → native_warmup → native_calibrate → lamp on → imaging registers →
emit_motor_tables → emit_scan_program(GO) → stream → wait home → reactive home`.

`meta` keys: `dpi` (requested), `scandpi`, `downscale`, `width`, `lines`,
`channels`, `depth`, `lineart`, `stride`, `mode`. `.raw.meta` sidecar mirrors it
so the renderer is self-describing.

The native pipeline is **self-contained** — no `scan3_full.txt` / `tables3full/`
dependency (all replaced by generators). Everything old lives in `../backup/`.

---

## 4. Fixes applied THIS session (most recent first)

0. **Motor overshoot at 150 dpi — FIXED, hardware-confirmed.** `emit_motor_tables`
   wrote `reg20 = 0x50` (bits[5:4]=1) just before uploading the slope table to
   SDRAM; the real ScanGear driver writes `0x60` (bits[5:4]=2) there, confirmed in
   BOTH the 150- and 1200-dpi captures. This register gates the SDRAM/motor DMA;
   with the wrong value the carriage over-ran the bed (needed a power-cycle) at
   150 (and 100, which resamples from 150). Found by an OFFLINE FULL-TRACE DIFF:
   a harness (`/tmp/harness.py`) mocks the USB layer + fast-forwards time, runs the
   real `driver.scan()`, records every register op, and diffs the emitted program
   against the parsed captures. Every motor-relevant register matched EXCEPT reg20.
   The motor tables themselves (master/slope/home-tail) were already byte-identical
   to the capture — the bug was this one setup register, not the tables. Fix:
   `wr(0x20, 0x60)`. NOTE: the effect is resolution-dependent (75/300/600 tolerated
   0x50; 150 did not), so this same fix is expected to also cure the 1200/800
   "partial + stretched" symptom — confirm on hardware.

0a. **Native 1200 dpi — cracked from the ScanGear 1200 capture.** A real
    `USBlyzer` trace of the vendor driver at 1200 dpi (buffer-truncated, but the
    register/motor program at the start is intact) was parsed the same way as the
    75/150 captures. The full-page imaging pass (the *last* GO in the trace,
    reg02=0x82, width=9921, lines≈14031) was diffed against our
    `emit_scan_program` output. Every register already matched byte-for-byte
    (exposure gates 0x33/0x34, CCD timing 0x40–0x47, 0x17, 0x48/0x49, 0x1e/0x1f,
    0x1d, reg2f=0x8a launch) **except two**:
    - **reg 0x07 (Y-step divider).** Capture writes `0x01`; our `2400//ydpi`
      formula gave `0x02` at 1200 → line clock desynced → 0 bytes. Fixed:
      `ydiv = 1 if ydpi>=1200 else (0xf if ydpi==400 else 600//ydpi)`.
    - **reg 0x02 launch run-mode.** Capture's full-page pass launches with
      `reg02=0x82` (run-mode bits [5:4] = 0). Our code forced run-mode 2 (bit5
      set → 0xA2) at every dpi. That bit5 is why last session's "run-mode 2 fix"
      moved the carriage but streamed 0 bytes. Fixed: run-mode 2 for ≤600,
      **run-mode 0 for ≥1200** (`wbit(0x02,4,2, 0 if xdpi>=1200 else 2)`).
    The identity vs. hardware 3×3 colour matrix (reg 0x37/0x38) still differs by
    design — we do colour in software, ScanGear does it in the ASIC. Needs one
    hardware run to confirm; program is now capture-faithful.

0b. **Native 150 dpi — cracked from the ScanGear 150 capture.** Root cause of the
    bed over-run: our old `imaging_travel` reduced motor travel to ~685 cruise
    (total 3046 steps) vs ScanGear's ~980 cruise (total ~3341); the fixed
    home-decel tail then over-drove the carriage on the return. The 150 capture's
    slope + tail are byte-exact with `build_slope(dpi, travel=1310)` /
    `build_hometail(dpi)`. Fix: `TRAVEL_STEPS = 1310` (OEM geometry constant) used
    for every dpi; the 150→300 downscale fallback removed. 150 is now a true
    native pass.

1. **Live preview (GUI).** `imaging.preview_image()` decodes a fast, heavily
   subsampled thumbnail of the lines-so-far. `driver.scan(preview=cb)` calls it
   throttled (every ~2 s) *inside* the read loop — must stay fast (<~80 ms) so it
   never stalls the USB drain (the scanner buffer must be kept emptied while
   streaming). Preview renders into the **full-page frame** (correct aspect) and
   **fills from the bottom up** (place partial rows at the top of the frame, then
   ROTATE_180 → they land at the bottom, matching scan direction + final
   orientation). Scale is computed off the *full* height so it doesn't rescale as
   it grows.

2. **Second-scan reliability.** `driver._flush_pipes()` (called in
   `open_device`) clears endpoint halts and drains stale bytes left in the
   bulk-IN pipe by the previous scan — that leftover data was corrupting the next
   calibration read (intermittent). GUI worker now closes the device in a
   `finally` so a failed scan can't leak the handle.

3. **150 dpi fallback.** ~~Native 150 over-runs the bed; map 150 → 300 + ×2
   downscale.~~ **SUPERSEDED by fix 0b** — 150 is now native.

4. **1200 dpi run-mode.** ~~Changed launch run-mode to 2 for all dpi so the
   carriage moves.~~ **SUPERSEDED by fix 0a** — the correct value at ≥1200 is
   run-mode **0**; the earlier "run-mode 2 everywhere" is exactly what kept 1200
   at 0 bytes (bit5 set).

5. **Empty-capture guard.** `imaging.export` returns cleanly on 0 bytes (no
   "cannot write empty image" crash); CLI prints a clear message.

6. **usb import + read hardening.** `import usb.core, usb.util` moved back to
   module top (a refactor had hidden it inside `open_device`, so the bulk-read
   `except usb.core.USBError` raised NameError when a read hiccuped). `rd()` now
   retries on empty/None instead of subscript-crashing.

7. **Mode mapping corrected.** reg 0x05[1:0]: **0 = 1-bit line-art, 1 =
   grayscale, 2 = colour**. (Earlier "gray" was wrongly pointed at mode 0 =
   line-art, giving width/8 packed output — the "8× repeat" bug.)

8. **16-bit is little-endian** on the wire (`<u2`), not big-endian. Verified via
   adjacent-row correlation (0.99 LE vs 0.23 BE).

9. **Motor tables fully generated + byte-exact.** `motor_tables.build_slope`,
   `build_master_ramp`, `build_hometail` reproduce the captured 75-dpi tables
   byte-for-byte; self-contained (embedded master ramp + pre-ramp blob, no DLL at
   run time). `imaging_travel(dpi)` solves motor travel so total commanded steps
   ≈ the 75-dpi full-bed value (3046) — this is the current (imperfect) travel
   model; see §5.

10. **Program renamed** canoscan → scan8000f; repo has README, LICENSE (© iappyx),
    .gitignore, requirements.txt. Root reorganised: `scan8000f/` + `backup/`.

---

## 5. Open problems + hypotheses

### 5a. Native 150 dpi over-runs the bed — ✅ RESOLVED (fix 0b)

The ScanGear 150 capture settled it: our motor travel was too short, so the
fixed home-decel tail over-drove the carriage on the return. Its slope + tail are
byte-exact with `build_slope(dpi, travel=1310)` / `build_hometail(dpi)`. Fix:
`TRAVEL_STEPS = 1310`. History (equal-total-steps and momentum/cruise theories,
both rejected before the capture arrived) is in git; the capture made all of it
moot. Awaiting final hardware sign-off but the tables are ground-truth-matched.

### 5b. Native 1200 dpi captures 0 bytes — ✅ FIX APPLIED (fix 0a), hardware test pending

The ScanGear 1200 capture proved the cause was **two wrong registers**, not the
streaming loop or a different scan mechanism:
- reg 0x07 must be `0x01` at 1200 (we wrote `0x02`) — the Y-step divider desynced
  the line clock so the ASIC latched no lines.
- reg 0x02 launch must be run-mode **0** (`0x82`) at 1200 (we wrote run-mode 2,
  `0xA2`). The old "run-mode 2 everywhere" guess is exactly what produced the
  0-byte symptom — the carriage moved but the pipeline never armed for data.

Everything else (exposure, CCD timing, line count 14031≈our 14016, width
9921≈our 9920, res_class 0, EXPO 0x5400) already matched the capture. The read
loop is the same generic chunked drain that works at 300/600, so once the ASIC
produces lines it will drain normally. **Remaining:** one hardware run at 1200 to
confirm bytes stream and the image is clean. (Note: the vendor arms the bulk-IN
in fixed 0x80000 (512 KB) chunks and re-GOes per band; our single-target chunked
read is functionally equivalent and already proven at 300/600, so no change
needed there — but if 1200 still misbehaves, matching the 512 KB re-arm cadence
is the next thing to try.)

### 5d. Only five native hardware resolutions — 100/200/400/800 are resampled

Confirmed from CNQL2403.DLL (the motor-slope/home-decel generator, function at
`0x1000e6xx`): the per-resolution tail selector has explicit cases for **only**
`0x4b`/`0x96`/`0x12c`/`0x258`/`0x4b0` (= 75/150/300/600/1200) plus `0x960` (2400,
film). The Y-step divider (`0x1000bf75`) resolves cleanly only for divisors of 600
(≤600 class) or of 1200 (≥1200 class). 100/200/400/800 are **not** integer rungs
of that grid — and each is exactly **⅔ of a native rung** (100=⅔·150, 200=⅔·300,
400=⅔·600, 800=⅔·1200). So ScanGear does not drive the motor at those rates; it
scans the native rung above and resamples in software.

`driver.scan()` mirrors this: for a requested dpi in `{100,200,400,800}` it sets
`scandpi` to the native rung, programs the hardware natively, and records
`out_width`/`out_lines` (the true requested dimensions) in `meta`.
`imaging.export()` LANCZOS-resamples the decoded image to those exact dims
(colour, gray, 16-bit preserved, 1-bit re-thresholded). Driving the motor at a
non-native rate would hit the DLL's tail-less `else` branch → no home-decel ramp →
the same carriage over-run that plagued native 150. **Do not** add
100/200/400/800 to `build_slope`/`_TAIL_SRC` — they have no hardware tables by
design.

### 5c. Width register ceiling (design limit, not a bug)

reg 0x12/0x13 width is 14-bit (max 16383 px). Full-bed width: 1200 dpi = 9920 ✓,
2400 dpi = 19840 ✗. So full-width scanning tops out at 1200 dpi; 2400+ would need
a narrower strip. 2400 in X is the CCD optical limit anyway; higher is interp.

---

## 6. Key verified technical facts (quick reference; details in the spec docs)

- **USB register I/O:** select-addr `ctrl(0x40,0x0c,0x83,0,[reg])`, write
  `…0x85…[val]`, read `ctrl(0xc0,0x0c,0x84,0,1)`. Bulk arm
  `ctrl(0x40,0x04,0x82,0,[dir,0,0x82,0,size32LE])`. Commit/latch = select 0x24.
- **Home sensor** = reg 0x64 bit6 (clear = home). **Move-complete** = reg 0x03
  bit3 (set = done). Motor: reg 0x02 [5:4] run(3=RUN,0=STOP), b7 NOTHOME, b1 GO.
- **Colour** = reg 0x05[1:0]=1, matrix regs 0x37/0x38 = **identity** (mono
  program loads a luma matrix there → the original "all channels identical" bug).
  Wire is pixel-interleaved R,G,B. Channels come from 3 CCD lines offset by ~11
  rows at low dpi; renderer auto-detects the offset.
- **Calibration** (fresh every scan): warm-up state machine (brightness gate →
  PWM binary search → 3× stable) → AFE **gain** `code = 283 − 208/(210/peak)`
  (reproduces sniffed 96/95/a4) → AFE **offset** 8-step binary search vs 16-bit
  black counter (lamp off) → 20-line trimmed-mean white + smoothed dark −0x100 →
  12-byte/px shading (K=0x7d000000) → upload SDRAM 0xffffff.
- **Init tables generated byte-exact:** gamma banks (identity ramps), master
  motor ramp. See `motor_tables.py` + `driver.native_init`.
- **Motor slope** = accel(1179 master ticks) + cruise-count word + decel/tail;
  `imaging_travel(dpi)` picks travel. Byte-exact vs capture at 75 dpi.

---

## 7. How to run / debug

```
pip3 install -r requirements.txt
python3 scan8000f.py gui
python3 scan8000f.py scan --dpi 300 --mode color --format png,tif --out ~/Desktop/scan
```

- GUI log panel shows the full warm-up/calibration trace + last-4-lines of any
  traceback (stdout is redirected into it during the scan).
- If the carriage parks out after a failure: power-cycle to re-home.
- Offline register-diff harness (very useful before hardware runs): mock
  `driver.wr/rd/wbit/sel/commit/afe/_bulk_*` and run `driver.scan(...)` to catch
  code errors without the scanner (see git history / earlier sessions for the
  snippet). Also mock counters()/green_peak() to converge calibration.

---

## 8. Next steps (suggested order)

1. **Hardware-confirm 1200 dpi** (fix 0a). Run `--dpi 1200 --mode color` on the
   real scanner; verify bytes stream and the image is clean + correctly homed. If
   it still 0-bytes, switch the read loop to the vendor's 512 KB re-arm-per-band
   cadence (§5b).
2. **Hardware-confirm 150 dpi** (fix 0b) end-to-end (over-run gone, clean home).
3. **Propagate both fixes to the Android port** (`scan8000f-android`): regenerate
   the `slope_150.bin` / `tail_150.bin` assets, remove the 150→300 downscale in
   `MotorTables.normalizeDpi` / `ScannerDriver.kt`, and apply the reg07=1 +
   run-mode-0 launch change for the 1200 path.
4. Film/TPU support (positive/negative) — DLL has the full path; needs the lid
   adapter + film to test. ICC profiles CNS24H (positive) / CNS24I (negative)
   ship with the driver.
5. Optional polish: progress bar accuracy, cancel button, multi-page/batch,
   per-scan colour/exposure controls.
