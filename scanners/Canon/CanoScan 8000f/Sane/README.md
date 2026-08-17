# SANE backend — CanoScan 8000F

A native [SANE](http://www.sane-project.org/) backend (`libsane-canon8000f`) for
the CanoScan 8000F flatbed scanner (USB `04a9:220f`), so it works with any SANE
frontend — `scanimage`, XSane, simple-scan, GIMP and others.

It is the **C implementation** of this project's driver, following the reference
implementation in `../python/` (`driver.py` / `imaging.py`): the same USB register
sequences, motor tables and decode, here through `libusb-1.0` and the SANE API.

## Status

**Scans on real hardware.** A 150 dpi colour full-bed scan through `scanimage`
produced correct geometry (1240×1752), no truncation, **zero blank rows** — the
carriage travelled the full bed and streamed throughout — and 0.10% saturation.

Working now:
- Full SANE API, option model (resolution 75–1200, mode Color/Gray/Lineart,
  bit depth 8/16, geometry tl/br in mm, preview), parameter computation.
- USB device detection (`04a9:220f`), interface claim, bulk-endpoint discovery.
- End-to-end SANE data path, validated with a no-hardware test pattern.
- On a real device, `sane_start` runs the **full pipeline**: `native_init` →
  `native_warmup` (lamp PWM + stability) → `native_calibrate` (white/dark shading)
  → motor tables → scan program → bulk stream → decode. Set `CANON8000F_DEBUG=1`
  to watch each stage.
- Decode (channel realignment, scanner→sRGB matrix, sRGB gamma, 16-bit, 180°
  rotation), cross-validated against `../python/imaging.py` — colour within
  ±1 LSB rounding, gray exact.
- `CANON8000F_FAKE=1` returns a synthetic image for no-hardware checks.
- **Fault handling.** `rd()` returns `-1` and latches a fault rather than
  substituting `0`; `at_home()` and `move_done()` fail safe (report "not home" and
  "still moving"); `native_init` aborts on `reg0x04 == 0x84`. `sane_start` clears
  the latches per session and returns `SANE_STATUS_IO_ERROR` at each stage
  boundary — the C stand-in for the reference driver's exceptions.

Remaining: a like-for-like comparison against the reference driver (`../python/`)
at matched settings, the other resolutions and modes, and these gaps:

- **X hardware windowing** — region scans crop in software and still transfer the
  full width; the reference driver windows the CCD (reg 0x10/0x11) for ~84% fewer
  bytes on a narrow selection.
- **Resolution policy** — the rung map is hardcoded (`100→150`, …); the reference
  snaps any non-rung value up to the next one and rejects anything above 1200
  rather than letting it reach the hardware.
- **Warm-lamp fast path** — every scan pays the full ~18 s warm-up.
- **Device lock** — no cross-process lock, so this backend and the Python
  CLI/GUI/eSCL bridge can collide on the USB device.

## Build

```
make            # -> libsane-canon8000f.so.1  (needs libsane-dev, libusb-1.0-0-dev)
make test       # no-hardware check: builds and scans a test pattern to PPM
```

On Debian/Ubuntu: `sudo apt install libsane-dev libusb-1.0-0-dev build-essential`.

On macOS (Homebrew): `brew install sane-backends libusb`, then override the dirs,
e.g. `make LIBDIR=$(brew --prefix)/lib`.

**Apple Silicon with both Homebrews installed.** If an Intel Homebrew also exists
in `/usr/local`, the build picks its x86_64 `libusb` and fails at link time with
`symbol(s) not found for architecture arm64`. The cause is `pkg-config`: Homebrew
does not install one under `/opt/homebrew/bin`, so the Intel binary is used and it
searches Intel paths. Point it at the arm64 `.pc` files:

```
PKG_CONFIG_PATH=/opt/homebrew/lib/pkgconfig make test
```

Check the result with `file libsane-canon8000f.so.1` — it should report `arm64`,
matching `uname -m`. A mismatched backend links but cannot be loaded by a native
`scanimage`.

## Install

```
sudo make install     # copies the .so into SANE's backend dir + adds it to dll.conf
scanimage -L          # should list:  device `canon8000f:0' is a Canon CanoScan 8000F
scanimage --device canon8000f:0 --resolution 300 --mode Color -o scan.png
```

On Homebrew (no sudo needed, the prefix is user-writable):

```
PKG_CONFIG_PATH=/opt/homebrew/lib/pkgconfig make install
```

Two things the install has to get right, both of which fail silently:

- **The dll backend dlsym()s `libsane-<name>.<ver>.so`** — version *before* `.so`,
  not the `libsane-<name>.so.<ver>` soname layout the build produces. Installing
  under the build name loads nothing and the device never appears.
- **It searches the libdir compiled into sane-backends**, which on Homebrew is
  inside the Cellar, not the `/opt/homebrew/lib` symlink farm. `make install`
  asks `pkg-config` for both, so don't override `LIBDIR` by hand.

If `scanimage -L` still finds nothing, `SANE_DEBUG_DLL=255 scanimage -L 2>&1 |
grep canon8000f` shows exactly which path and symbols it tried.

## Scanning

`scanimage` infers the output format from the file extension, so a PNG needs no
conversion step:

```
scanimage --device canon8000f:0 --resolution 150 --mode Color -o ~/Desktop/scan.png
```

`scan.sh` wraps that with sensible defaults and a timestamped name on the Desktop:

```
./scan.sh              # 300 dpi colour
./scan.sh 150          # 150 dpi colour
./scan.sh 600 Gray     # 600 dpi grayscale
./scan.sh 150 Color p1 # -> ~/Desktop/p1.png
```

It pins the arm64 `scanimage` explicitly, which is the failure that costs the most
time to diagnose by hand: an Intel binary earlier on `PATH` cannot load an arm64
backend, and the only symptom is `open of device ... failed: Invalid argument`.

A no-hardware end-to-end check through the real frontend:

```
CANON8000F_FAKE=1 scanimage -L
CANON8000F_FAKE=1 scanimage --device canon8000f:0 --resolution 150 --mode Color -o /tmp/t.png
```

`CANON8000F_FAKE=1` works with any frontend, not just `scanimage`: device
detection reports present and scans return the test pattern, so a GUI can be
exercised with no scanner attached.

`make uninstall` removes the backend and its `dll.conf` entry.

## Roadmap (pipeline port)

Ported stage by stage against the Python reference, each verified before the next:

1. ✅ SANE skeleton, option model, USB detect/open, data path.
2. ✅ USB register primitives + `native_init` (home, gamma upload, motor reset).
3. ✅ `native_warmup` (lamp PWM search + stability) and `native_calibrate`
   (white/dark shading; res-class-1 ≤600, res-class-0 at 1200).
4. ✅ Motor tables + scan program + bulk stream.
5. ✅ Decode: channel realignment, scanner→sRGB matrix, sRGB gamma, 16-bit,
   180° rotation; cross-validated against the Python reference.
6. ✅ Geometry→region crop + bilinear resample (100/200/400/800), 1200 dpi.
7. ✅ Fault handling (fail-safe reads, fatal-status abort, IO_ERROR at stage
   boundaries) + first on-hardware scan through `scanimage`.
8. Like-for-like comparison against the Python reference across all resolutions
   and modes, then the four gaps listed under **Status**.

## Files

| File             | Purpose                                             |
|------------------|-----------------------------------------------------|
| `canon8000f.c`   | The backend (SANE API, options, USB, pipeline)      |
| `canon8000f_tables.h` | Motor tables generated by `../python/motor_tables.py` |
| `Makefile`       | build / test / install / uninstall                  |
| `test_harness.c` | standalone SANE client for the no-hardware check    |
| `scan.sh`        | convenience wrapper: scan straight to a PNG         |

Not affiliated with or endorsed by Canon. "CanoScan" is a Canon trademark, used
only to identify the hardware this backend drives. Produced by clean-room analysis
for interoperability.
