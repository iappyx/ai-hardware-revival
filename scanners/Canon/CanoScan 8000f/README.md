# CanoScan 8000F

A clean-room revival of the CanoScan 8000F flatbed scanner (USB `04a9:220f`),
running natively on modern machines. Device init, lamp warm-up, AFE calibration,
motor control, the scan program and image decode are all implemented from
scratch.

## What's here

| Folder     | What it is                                                                 |
|------------|----------------------------------------------------------------------------|
| `python/`  | **The driver** (source of truth): pure-Python driver, CLI and GUI. Runs on any libusb host (macOS/Linux/…). Contains `escl/`, the eSCL bridge. |
| `python/escl/` | eSCL / AirScan bridge — presents the scanner as a **native driverless** device (macOS Image Capture, iOS, Linux). Runs on the same driver. |
| `sane/`    | A native **SANE backend** (`libsane-canon8000f`) in C — works with `scanimage`, XSane, simple-scan, GIMP and other SANE frontends. |

## Which one do I want?

- **macOS, native scanning UI** → `python/escl/` (run the bridge; the scanner
  appears in Image Capture / Preview with no app to install).
- **Linux, or any SANE frontend** → `sane/` (build, `make install`, then
  `scanimage`).
- **A command-line / GUI tool, or to work on the driver itself** → `python/`.

`python/` is the reference implementation; `sane/` follows it. When the driver
changes it changes in `python/` first. The SANE backend currently lags in four
places — hardware X windowing, resolution policy, the warm-lamp fast path and the
device lock — all listed in [`sane/README.md`](sane/README.md).

## Resolutions & modes

75 / 100 / 150 / 200 / 300 / 400 / 600 / 800 / 1200 dpi, colour / gray / line-art,
8- or 16-bit. Native hardware rungs are 75/150/300/600/1200; the others are
resampled from the next native rung.

**Reflective (flatbed) scanning only.** The 8000F's transparency unit — film
strips and slides, using the lamp in the lid — is **not supported yet**.

---

Not affiliated with or endorsed by Canon. "CanoScan" is a Canon trademark, used
only to identify the hardware this project drives. Produced by clean-room analysis
for interoperability.
