# scanp208

Driver, command line and GUI for the imageFORMULA P-208 portable sheet-fed
scanner. Pure Python over libusb — no vendor software, no kernel extension.

## Features

- 150 / 200 / 300 / 400 / 600 dpi, colour, greyscale or black-and-white
- Duplex, and batch feeding of a whole stack in one pass
- Trim to the sheet, straighten a crooked feed, drop blank sides
- Brightness, contrast, gamma (overall or per channel), rotation
- Colour drop-out and colour enhance
- PNG, TIFF, JPEG and PDF out, with a PDF size/quality setting
- A whole stack as one long image, for a receipt or a folded document
- An eSCL bridge, so the scanner appears as a driverless network device

## Install

    pip install pyusb pillow numpy
    pip install zeroconf            # only for the eSCL bridge

libusb must be present: `brew install libusb` on macOS, `libusb-1.0` from your
package manager on Linux. No driver to install and nothing to unload.

## Usage

    scan.py                                 300 dpi colour, the whole stack
    scan.py --mode gray --duplex            greyscale, both sides
    scan.py --format pdf                    one PDF
    scan.py --duplex --skip-blank           drop blank backs
    scan.py --single                        just the top sheet
    scan.py --deskew                        straighten a crooked feed
    scan.py --page-size a4                  crop to a known size
    scan.py --mode gray --dropout red       drop a colour out
    scan.py --mode gray --enhance red       or emphasise it instead
    scan.py --bitonal --dither              error diffusion, good for photos
    scan.py --format pdf --pdf-quality max  a PDF with nothing thrown away
    scan.py --format pdf --pdf-quality small  an email-sized PDF
    scan.py --continuous                    the stack as ONE long image

`scan.py --help` lists the rest: brightness, contrast, gamma, rotation, output
folder and format, and the switches for the tone curve, the factory calibration
curve and automatic page sizing.

Feeding the whole stack is the default; `--single` is the exception.

## GUI

    python3 gui.py

Resolution, mode, duplex, trimming, straightening, page size, rotation,
brightness, contrast, gamma and drop-out, with a live preview of the page as it
feeds. Scanned pages can be rotated or deleted individually before saving.

## Native network scanner (eSCL bridge)

    cd escl && python3 escl_bridge.py

The scanner is then advertised over Bonjour and appears by itself in Image
Capture, Preview, iOS and any Mopria client, with nothing to install on the
machine that scans. It offers the feeder with duplex, and hands pages over as
they are scanned rather than after the whole stack finishes. See
[`escl/README.md`](escl/README.md).

## Project layout

| File | What it is |
|------|------------|
| `driver.py` | The device: transport, commands, calibration, duplex, batch |
| `imaging.py` | Everything that looks at pixels: trimming, straightening, tone, binarisation, export |
| `scan.py` | Command line |
| `gui.py` | Tkinter front end |
| `selftest.py` | Offline checks — run before testing against hardware |
| `escl/` | The eSCL bridge |

## How it works

Every scan starts with a short calibration pass before the first sheet moves.
The driver measures the scanner's internal dark and white reference strips and
settles the analog gain and offset against them, then reads a correction table
out of the unit's own memory and folds it in.

That table is specific to your scanner. Nothing is shipped as a constant, so
the colour and brightness you get are corrected for your machine rather than an
average one — and a driver built the other way clips white paper badly.

Each sheet is scanned in its own session, which is what lets the scanner mark
where one page ends and the next begins. The device also offers a mode that
streams the whole stack as a single unbroken image with no page breaks at all -
that is `--continuous`, useful for a long receipt, and wrong for everything
else.

PDFs store each page the way that suits it, which is what the mainstream PDF
tools do: JPEG for colour and greyscale, and a lossless encoding for
black-and-white, since a bitonal page cannot be compressed lossily anyway. On a
scanned A4 page that works out at roughly 1.2 MB at the default setting, 740 KB
at `small`, and 11 MB at `max`, which keeps every pixel.

## License

See [`../LICENSE`](../LICENSE). Not affiliated with or endorsed by Canon;
written from independent analysis of the hardware, for interoperability.
