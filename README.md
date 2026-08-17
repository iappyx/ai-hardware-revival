# ai-hardware-revival

Bringing hardware back to life on modern machines, when the vendor's software no
longer runs and nobody is writing drivers for it any more.

Each device gets a clean-room driver: the hardware's behaviour is worked out from
first principles and reimplemented from scratch, so it runs natively — no vendor
runtime, no Windows, no emulator, no virtual machine.

## Devices

| Device | Status | Where |
|---|---|---|
| **CanoScan 8000F** flatbed scanner (USB `04a9:220f`) | **Working** — reflective scanning end-to-end, all resolutions, 8/16-bit, plus a driverless network bridge | [`scanners/Canon/CanoScan 8000f/`](scanners/Canon/CanoScan%208000f/) |

## CanoScan 8000F

A 2003 flatbed with no macOS support since PowerPC. It now scans on Apple Silicon
from pure Python:

- 75–1200 dpi, colour / grayscale / line-art, 8- or 16-bit
- PNG, TIFF, JPEG, PDF, RAW
- Area selection — the CCD window is set in hardware, so scanning a small
  selection moves far fewer bytes than a full pass
- An **eSCL / AirScan bridge**, so the scanner appears in macOS Image Capture,
  Preview and iOS as a driverless network scanner with nothing installed
- A native **SANE backend** in C, for `scanimage`, XSane, simple-scan, GIMP and
  anything else that speaks SANE

The interesting part is the motor. The scanner has no motion controller: the host
uploads tables of step intervals into the ASIC's SDRAM and the ASIC walks them,
firing one motor step per entry. Those tables are **computed from a derived
acceleration law** — every entry, nothing stored.

Reflective (flatbed) scanning only. The transparency unit for film and slides is
not supported yet.

→ [Device overview](scanners/Canon/CanoScan%208000f/README.md) ·
  [Driver, CLI and GUI](scanners/Canon/CanoScan%208000f/python/README.md) ·
  [SANE backend](scanners/Canon/CanoScan%208000f/sane/README.md)

## Licence

MIT. Not affiliated with or endorsed by any hardware vendor; trademarks are used
only to identify the hardware each driver drives. Produced by clean-room analysis
for interoperability.
