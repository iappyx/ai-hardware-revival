# imageFORMULA P-208

An independent revival of the imageFORMULA P-208 portable sheet-fed scanner
(USB `1083:164c`), running natively on modern machines. Transport, calibration,
duplex, batch feeding, the scan program and image decode are all implemented
from scratch.

It scans from a computer over libusb, appears on the network as a driverless
scanner, and runs from an Android phone over USB-C with no computer involved at
all — the scanner is bus-powered, so the phone drives it unaided.

## What's here

| Folder | What it is |
|--------|------------|
| `python/` | **The driver** (source of truth): pure-Python driver, CLI and GUI. Runs on any libusb host (macOS/Linux/…). Contains `escl/`, the eSCL bridge. |
| `python/escl/` | eSCL / AirScan bridge — presents the scanner as a **native driverless** device (macOS Image Capture and Preview, iOS, Linux, any Mopria client). Runs on the same driver. |
| `android/` | **Android app** (Kotlin, Jetpack Compose) — drives the scanner straight from a phone over USB-C, no computer involved. |

## Which one do I want?

- **macOS or iOS, native scanning UI** → `python/escl/` (run the bridge; the
  scanner appears in Image Capture / Preview with nothing to install).
- **A command-line or GUI tool, or to work on the driver itself** → `python/`.
- **Scanning with nothing but a phone** → `android/` (USB-C straight to the
  scanner; it needs no hub).

`python/` is the reference implementation; both the eSCL bridge and the Android
app follow it. A native SANE backend, as the CanoScan 8000F has, is not written
yet.

## Resolutions & modes

150 / 200 / 300 / 400 / 600 dpi, colour / greyscale / black-and-white, simplex
or duplex. 300 and 600 are scanned natively; the rest are scanned at one of
those and resampled, because the sensor is a whole number of pixels wide only
at 150, 300 and 600.

The Android app covers the same resolutions and modes, minus deskew.

Sheet-fed only — there is no flatbed. Pages are trimmed to the sheet, can be
straightened, and blank sides can be dropped, all optional. Colour drop-out and
its opposite, colour enhance, are available in greyscale.

---

Not affiliated with or endorsed by Canon. "imageFORMULA" is a Canon trademark,
used only to identify the hardware this project drives. Written from
independent analysis of the hardware, for interoperability.
