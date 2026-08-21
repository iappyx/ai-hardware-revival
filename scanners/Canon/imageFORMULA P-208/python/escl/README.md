# eSCL bridge

Makes the P-208 a driverless network scanner. Run this and the scanner appears
by itself in Image Capture, Preview, Printers & Scanners, iOS and any Mopria
client - nothing to install on the machine that scans.

    pip install zeroconf                 # discovery; optional
    python3 escl_bridge.py               # 0.0.0.0:8090, advertised over Bonjour
    python3 escl_bridge.py --port 9000
    python3 escl_bridge.py --no-mdns     # HTTP only, for curl
    python3 escl_bridge.py --fake        # synthetic pages, scanner untouched

## What is different from the flatbed bridge

The 8000F is a flatbed and this is not, which changes two things throughout.

**The feeder, not a platen.** The capabilities advertise `Adf`, simplex and
duplex, with a feeder capacity and `DetectPaperLoaded`. `ScannerStatus` carries
an `AdfState`, taken from the paper sensor, so a client can see there is nothing
loaded before starting. A request naming `Platen` is refused with 400 rather
than quietly feeding a sheet - this hardware has no bed, and pretending
otherwise would scan the wrong thing.

**A job is a stack, not a page.** A flatbed answers one `NextDocument` and then
404s. Here the client keeps asking and gets a page each time until the tray runs
out, and a 404 is what ends the job. Pages are handed over **as they are
scanned**, not after the whole stack finishes, so a ten-sheet batch starts
arriving while the rest is still feeding.

There is no scan region: what is scanned is whatever sheet is fed. Trimming,
straightening and tone happen on this side, the same defaults the CLI uses.

## Details worth knowing

Resolutions come from the unit's own capability page - 100 to 600 dpi - and a
client asking for something else is snapped to the nearest supported value
rather than refused. Colour, greyscale and 1-bit are all offered; 1-bit is
acquired in colour and converted here, since the scanner has no lineart mode.

The scanner is exclusive: a second job while one is running gets 503 with
`Retry-After`. The daemon holds no state across jobs.

## Testing without hardware

`--fake` serves synthetic pages and never opens the device, which separates
protocol problems from scanning problems:

    python3 escl_bridge.py --fake --no-mdns --port 8099
    curl -s localhost:8099/eSCL/ScannerCapabilities
    curl -s localhost:8099/eSCL/ScannerStatus
    LOC=$(curl -si -X POST localhost:8099/eSCL/ScanJobs --data @settings.xml \
          | awk '/^Location:/{print $2}' | tr -d '\r')
    curl -s -o page1.jpg -w '%{http_code}\n' "$LOC/NextDocument"
    curl -s -o page2.jpg -w '%{http_code}\n' "$LOC/NextDocument"   # 404 ends it
