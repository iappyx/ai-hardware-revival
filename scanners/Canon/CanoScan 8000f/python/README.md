# scan8000f

A clean-room, pure-Python driver for the **Canon CanoScan 8000F** flatbed
scanner on Apple Silicon Macs (and any host with libusb). No Canon software, no
Windows, no emulator — device init, lamp warm-up, calibration, motor control and
the scan program are all generated from the hardware's own behaviour, recovered by
clean-room analysis for interoperability, with a small CLI and GUI on top.

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

**Reflective (flatbed) scanning only.** The transparency unit in the lid — film
strips and slides — is **not supported yet**: `--mode` covers reflective colour,
gray and line-art, and there is no film or TPU source.

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

The first scan of a session pays a full lamp warm-up; later scans skip it while the
lamp is still warm (see **Performance** below). Calibration still runs per scan —
see **To do** for the prepare-once split.

**One process at a time.** The driver takes an exclusive lock for the duration of a
session, so the bridge and the GUI cannot drive the scanner simultaneously. A job
that arrives while the other holds it waits briefly, then fails with
`scanner is in use by pid N` rather than colliding on the USB device.

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
dpi — the only ones with motor slope and home-decel tables. The other listed
resolutions (100, 200, 400, 800) are each exactly ⅔ of a native rung, so they are
served by driving the motor at the rung above and LANCZOS-resampling to the
requested size on export. Driving the motor at a non-native rate has no matching
decel table and would over-run the carriage, so any resolution that is not a rung
is snapped up to the next one, and anything above 1200 dpi is rejected outright
rather than reaching the hardware. All resolutions are colour/gray/line-art,
8/16-bit.

## Performance

Lamp warm-up dominates a cold scan. Measured at 300 dpi, a full pass breaks down as
roughly 18 s warm-up, 12 s read loop (motor-paced — that part is physics), and ~3 s
each for init and calibration.

The driver keeps a reference of the lamp's measured output at the settled PWM, so a
scan that starts while the lamp is still warm skips the settle and the PWM search
and goes straight to imaging: **~36 s cold, ~19 s warm** at 300 dpi, with output
indistinguishable from a full warm-up.

The fast path only ever *skips waiting*, never checking. It requires two readings
taken 0.5 s apart to agree, to be unsaturated, and to match the stored reference;
the reference is written only by a full warm-up, so it cannot drift by being
re-derived from itself, and it expires after 15 minutes. Any check that fails falls
through to the full warm-up. Set `WARM_LAMP_FASTPATH = False` at the top of
`driver.py` to disable it.

## To do

- **Prepare-once session (speed up the eSCL bridge).** Every scan still re-runs
  init and calibration (~6 s combined). Split the driver into a prepare-once
  (`warm_up`: init + calibrate, device left open) and a per-scan pass
  (`scan_prepared`). Additive API — the CLI/GUI keep the current cold-scan path.
  Care points: recalibrate when a scan crosses the ≤600↔1200 res-class boundary,
  add an idle TTL so cached calibration doesn't drift as the lamp ages, and
  invalidate the warm state on any USB/motor error. Needs a hardware pass to
  confirm prepared scans match cold ones.

## License

MIT — see [LICENSE](LICENSE).
