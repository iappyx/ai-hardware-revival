# scan8000f

A clean-room, pure-Python driver for the **Canon CanoScan 8000F** flatbed
scanner on Apple Silicon Macs (and any host with libusb). No Canon software, no
Windows, no emulator — device init, lamp warm-up, calibration, motor control and
the scan program are all generated from firmware logic recovered by reverse-
engineering the vendor driver, with a small CLI and GUI on top.

> Not affiliated with or endorsed by Canon. "CanoScan" is a Canon trademark,
> used here only to identify the hardware this project drives. The driver was
> produced by clean-room analysis for interoperability.

## Features

| Setting     | Values                                             |
|-------------|----------------------------------------------------|
| Resolution  | 75, 100, 150, 200, 300, 400, 600, 800, 1200 dpi    |
| Colour mode | `color` (24-bit), `gray` (8/16-bit), `lineart` (1-bit) |
| Bit depth   | 8, or 16 (preserved in PNG/TIFF/RAW; JPEG/PDF are 8-bit) |
| Export      | PNG, TIFF, JPEG, PDF, RAW                           |

Colour uses the scanner's factory colorimetry; calibration runs fresh on every
scan, so whites stay neutral as the lamp ages.

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

# 1200 dpi colour PNG
python3 scan8000f.py scan --dpi 1200 --mode color --format png --out ~/Desktop/photo

# 600 dpi 16-bit grayscale archival TIFF
python3 scan8000f.py scan --dpi 600 --mode gray --depth 16 --format tif --out ~/Desktop/archive

# black & white document → PDF
python3 scan8000f.py scan --dpi 300 --mode lineart --format pdf --out ~/Desktop/doc

# scan only a sub-area (x0,y0,x1,y1 in 0..1 bed coordinates)
python3 scan8000f.py scan --dpi 1200 --region 0.25,0.1,0.75,0.6 --out ~/Desktop/crop
```

**Area selection.** In the GUI, click **Prescan** for a quick full-bed preview,
then drag a box over the part you want — you get just that rectangle, and the scan
is shortened to match. The carriage scans from its start edge only up through your
selection, so a box toward the **bottom** of the page saves the most time (e.g. a
bottom strip scans ~15% of the bed); a box at the very **top** still scans the full
bed (it's the last thing the carriage reaches). On the CLI, pass
`--region x0,y0,x1,y1` in 0..1 coordinates. **Full bed** clears the selection.

Add `--trace` (CLI) or tick **USB trace log** (GUI) to write a full USB transfer
log to `last_scan_trace.txt` — a diagnostic, off by default.

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

The scanner has no on-board intelligence; the host programs the ASIC registers
directly over USB. This project reconstructs that entire sequence — see
`docs/STATUS.md` for the architecture and the per-scan register/motor program,
and the other `docs/*.md` for the detailed specs. The motor tables
(`motor_tables.py`) are generated in Python and verified byte-for-byte against
the vendor's own uploaded tables.

The scanner has five **native** hardware resolutions — 75, 150, 300, 600, 1200
dpi — the only ones the vendor firmware carries motor slope/home-decel tables
for. The other listed resolutions (100, 200, 400, 800) are the next native rung
up, resampled in software (each is exactly ⅔ of a native one). This driver does
the same: it drives the motor only at a native rung, then LANCZOS-resamples to
the requested size on export. All resolutions are colour/gray/line-art, 8/16-bit.

## License

MIT — see [LICENSE](LICENSE).
