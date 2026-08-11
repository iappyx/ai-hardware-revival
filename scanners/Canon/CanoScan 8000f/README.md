# scan8000f

A clean-room, pure-Python driver for the **Canon CanoScan 8000F** flatbed
scanner on Apple Silicon Macs (and any host with libusb). No Canon software, no
Windows, no emulator — device init, lamp warm-up, calibration, motor control and
the scan program are all generated from firmware logic recovered by
reverse-engineering, with a small CLI and GUI on top.

> Not affiliated with or endorsed by Canon. "CanoScan" is a Canon trademark,
> used here only to identify the hardware this project drives. The driver was
> produced by clean-room analysis for interoperability.

## Features

| Setting     | Values                                             |
|-------------|----------------------------------------------------|
| Resolution  | 75, 100, 150, 200, 300, 400, 600 dpi; 800 & 1200 dpi (experimental) |
| Colour mode | `color` (24-bit), `gray` (8/16-bit), `lineart` (1-bit) |
| Bit depth   | 8 or 16 (16-bit preserved in PNG/TIFF)             |
| Export      | PNG, TIFF, JPEG, PDF, RAW                           |

Colour uses the scanner's factory colorimetry; calibration runs fresh on every
scan, so whites stay neutral as the CCFL lamp ages. Film/slide (TPU) support is
planned.

## Install

```
pip3 install -r requirements.txt
```

That pulls `pyusb`, `libusb-package` (USB access), `pillow` (image export) and
`numpy` (fast decode + true 16-bit). `tkinter` (for the GUI) ships with the
standard Python installer on macOS. Only `pyusb`/`libusb-package` are strictly
required — without Pillow you can still export RAW.

## Usage

Graphical interface:

```
python3 scan8000f.py gui
```

Command line:

```
# 300 dpi colour → PNG + TIFF on the Desktop
python3 scan8000f.py scan --dpi 300 --mode color --format png,tif --out ~/Desktop/scan

# 600 dpi 16-bit grayscale archival TIFF
python3 scan8000f.py scan --dpi 600 --mode gray --depth 16 --format tif --out ~/Desktop/archive

# black & white document → PDF
python3 scan8000f.py scan --dpi 300 --mode lineart --format pdf --out ~/Desktop/doc
```

## Project layout

| File              | Purpose                                              |
|-------------------|------------------------------------------------------|
| `scan8000f.py`    | CLI entry point + GUI launcher                       |
| `gui.py`          | Tkinter graphical interface                          |
| `driver.py`       | USB driver + native scan pipeline                    |
| `imaging.py`      | Decode raw → image, multi-format export              |
| `motor_tables.py` | Motor ramp/slope/home tables (generated, byte-exact) |
| `docs/`           | Reverse-engineering specifications                   |

## How it works

The scanner has no on-board intelligence; the host driver programs the ASIC
registers directly over USB. This project reconstructs that entire sequence:

- **`docs/FULL_PROGRAM_SPEC.md`** — the per-scan register program.
- **`docs/CALIBRATION_SPEC.md`** — init, lamp warm-up, AFE gain/offset search,
  shading table build.
- **`docs/BUILDER_B_SPEC.md`** — the stepper-motor slope-table generator.
- **`docs/COLOR_AND_BUILDERB_FINDINGS.md`** — colour pipeline + mode notes.

The motor tables (`motor_tables.py`) are generated in Python and verified
byte-for-byte against the real firmware's output.

## Status

Experimental. Code is still being tuned. Contributions and test reports
welcome.

The scanner has exactly **five native hardware resolutions — 75, 150, 300, 600,
1200 dpi**. This isn't a limitation we chose: the vendor firmware (CNQL2403.DLL)
carries motor slope + home-decel tables for precisely those five and no others.
ScanGear's other listed resolutions (100, 200, 400, 800) are **not** driven on the
hardware — the vendor scans the next native rung up and resamples in software
(each extra dpi is exactly ⅔ of a native one: 100 = ⅔·150, 200 = ⅔·300,
400 = ⅔·600, 800 = ⅔·1200). This driver does the same: it drives the motor only at
a native rung, then LANCZOS-resamples to the requested size on export.

- **75 / 300 / 600 dpi** — native, hardware-verified (colour, gray, line-art,
  8/16-bit).
- **150 dpi** — native; the old motor over-run was fixed by recovering the true
  motor travel from a real ScanGear 150-dpi USB capture (slope + home-decel tables
  match byte-for-byte). Pending a final hardware sign-off.
- **100 / 200 / 400 dpi** — resampled from 150 / 300 / 600 (all native/verified),
  so output is solid.
- **1200 dpi** — *experimental.* A ScanGear 1200-dpi capture pinned the two wrong
  registers (Y-step divider, launch run-mode); the per-scan program now matches
  the vendor trace register-for-register. Needs one confirming hardware run.
- **800 dpi** — *experimental,* because it resamples from 1200, which is itself
  awaiting hardware confirmation.

Every native path was cracked the same way: capture the vendor driver over USB,
parse its register/motor program, and match ours to it exactly.

## License

MIT — see [LICENSE](LICENSE).
