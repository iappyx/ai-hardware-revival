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

## Native macOS scanner (eSCL bridge)

`escl_bridge.py` turns this driver into a **driverless network scanner**. macOS
(and iOS/Linux) support the eSCL / AirScan protocol out of the box, so once the
bridge is running the 8000F appears as a normal scanner in **Image Capture,
Preview, and Printers & Scanners** — with preview, area select and scan — and no
Canon software or third-party app installed. The "driver" is just this daemon
translating eSCL on one side to `driver.scan()` over USB on the other.

Run it manually (it only serves while the terminal is open — no autostart):

```
pip3 install zeroconf          # one-time, for Bonjour discovery
python3 escl/escl_bridge.py         # binds 0.0.0.0:8090, advertises over Bonjour
python3 escl/escl_bridge.py --port 9000
python3 escl/escl_bridge.py --no-mdns   # HTTP only, no advertising (test with curl)
python3 escl/escl_bridge.py --fake      # instant synthetic image, scanner untouched (diagnostic)
```

**How it works.** The bridge is a small HTTP server implementing the four eSCL
endpoints and advertising an `_uscan._tcp` Bonjour service so macOS discovers it:

- `GET /eSCL/ScannerCapabilities` — resolutions, colour modes, bed size, JPEG/PDF.
- `GET /eSCL/ScannerStatus` — Idle/Processing plus per-job state. macOS decides to
  fetch a page only when this reports `ImagesToTransfer >= 1` with the job-state
  elements in the `pwg:` namespace — getting that right is what makes the scan
  actually come through.
- `POST /eSCL/ScanJobs` — macOS posts a `ScanSettings` (resolution, colour mode,
  format, and a crop region in eSCL's 1/300-inch units, mapped to our `region`
  coords); the bridge starts the scan and returns a job URL.
- `GET …/ScanJobs/{id}/NextDocument` — blocks through the scan, then returns the
  JPEG/PDF and `404`s (the flatbed is one page).

Each scan currently pays a full lamp warm-up + calibration (~20 s) before the
image lands — macOS waits through it. See **To do** for the warm-session speedup.

## Project layout

| File              | Purpose                                              |
|-------------------|------------------------------------------------------|
| `scan8000f.py`    | CLI entry point + GUI launcher                       |
| `gui.py`          | Tkinter graphical interface                          |
| `driver.py`       | USB driver + native scan pipeline                    |
| `imaging.py`      | Decode raw → image, multi-format export              |
| `escl_bridge.py`  | eSCL/AirScan bridge → native driverless macOS scanner |
| `motor_tables.py` | Motor ramp/slope/home tables — generated, no vendor data |


## How it works

The scanner has no on-board intelligence; the host programs the ASIC registers
directly over USB, and this project reconstructs that entire sequence.

**Motor control.** The scanner has no motion controller either: the host uploads
tables of step intervals into the ASIC's SDRAM and the ASIC walks them, firing
one motor step per entry on a 20.8 ns tick. `motor_tables.py` generates those
tables from a recovered acceleration law — the carriage accelerates at a
constant **+20 steps/s per motor step**, i.e. `ns(n) = round(5e7 / (n + 8.2332183))`
— followed by a phase that is linear in tick space, described by nineteen
`(slope, run-length)` pairs. Nothing is stored; all 1178 ramp entries are
computed. The per-resolution decel tail is a reversed window of that same ramp.
Full derivation and the measured safety envelope are in the module docstring.

The scanner has five **native** hardware resolutions — 75, 150, 300, 600, 1200
dpi — the only ones the vendor firmware carries motor slope/home-decel tables
for. The other listed resolutions (100, 200, 400, 800) are the next native rung
up, resampled in software (each is exactly ⅔ of a native one). This driver does
the same: it drives the motor only at a native rung, then LANCZOS-resamples to
the requested size on export. All resolutions are colour/gray/line-art, 8/16-bit.

## To do

- **Warm-scanner session (speed up the eSCL bridge).** Each scan re-runs the full
  lamp warm-up + calibration (~20 s). Split the driver into a prepare-once
  (`warm_up`: init + warmup + calibrate, device left open) and a per-scan pass
  (`scan_prepared`), so the second and later scans in a session are ~3 s instead of
  ~20 s. Additive API — the CLI/GUI keep the current cold-scan path. Care points:
  recalibrate when a scan crosses the ≤600↔1200 res-class boundary, add an idle TTL
  so cached calibration doesn't drift as the lamp ages, and invalidate the warm
  state on any USB/motor error. Needs a hardware pass to confirm warm scans match
  cold ones.

## License

MIT — see [LICENSE](LICENSE).
