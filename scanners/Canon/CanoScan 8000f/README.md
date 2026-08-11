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
| Resolution  | 75, 300, 600 dpi (supported); 150 & 1200 dpi work-in-progress |
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

Resolution support:

- **75 / 300 / 600 dpi** — fully native and hardware-verified (colour, gray,
  line-art, 8/16-bit).
- **150 dpi** — *work in progress.* Native 150 has an unresolved motor-geometry
  quirk (the carriage over-runs the bed), so the app currently scans at 300 dpi
  and downsamples to 150. Output is correct; it is not yet a true native pass.
- **1200 dpi** — *work in progress / unsupported.* The carriage moves but the
  capture stops early (0 bytes). Not usable yet.

Cracking native 150 and 1200 needs a USB capture of the vendor driver doing
those resolutions, to recover the exact motor geometry (as the 75-dpi capture
did for the rest).

## License

MIT — see [LICENSE](LICENSE).
