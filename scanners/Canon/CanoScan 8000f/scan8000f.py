#!/usr/bin/env python3
"""CanoScan 8000F - native scanner tool (CLI + GUI launcher).

  # scan and export
  python3 scan8000f.py scan --dpi 300 --mode color --depth 8 \\
      --format png,tif --out ~/Desktop/scan

  # launch the graphical interface
  python3 scan8000f.py gui

Modes : color | gray | lineart      Depths: 8 | 16 (lineart is always 1-bit)
DPI   : 75 150 300 600 1200         Formats: png tif jpg pdf raw
Pure Python / pyusb - no Canon software.  Image export needs Pillow.
"""
import sys, os, argparse, time

def _run_scan(a):
    import driver, imaging
    info = driver.open_device()
    print('scanner ready (bulk-IN 0x%02x / OUT 0x%02x)' % (info['ep_in'], info['ep_out']))
    t0 = time.time()
    region = None
    if getattr(a, 'region', None):
        try:
            region = tuple(float(v) for v in a.region.split(','))
            assert len(region) == 4 and all(0.0 <= v <= 1.0 for v in region)
        except Exception:
            print('bad --region: need x0,y0,x1,y1 as four 0..1 values, e.g. 0.25,0.1,0.75,0.6')
            return
    raw, meta = driver.scan(dpi=a.dpi, mode=a.mode, depth=a.depth,
                            progress=lambda s: print(s, end='\r', flush=True),
                            trace=a.trace, region=region)
    print()
    out = os.path.expanduser(a.out)
    fmts = [f.strip() for f in a.format.split(',') if f.strip()]
    if not imaging.HAVE_PIL and any(f != 'raw' for f in fmts):
        print('note: Pillow not installed - only raw export available. `pip install pillow numpy` for images.')
        fmts = ['raw']
    if a.depth == 16:
        eightbit = [f for f in fmts if f in ('jpg', 'jpeg', 'pdf')]
        if eightbit:
            print('note: %s cannot hold 16-bit; %s will be written 8-bit (use tif for 16-bit%s).'
                  % (', '.join(eightbit), 'they' if len(eightbit) > 1 else 'it',
                     ' colour' if a.mode == 'color' else ''))
        if a.mode == 'color' and 'png' in fmts:
            print('note: 16-bit colour PNG is written 8-bit (Pillow has no 48-bit RGB); use tif for true 16-bit colour.')
    if len(raw) == 0:
        driver.close_device()
        print('ERROR: scan produced no data (0 bytes). The carriage may not have moved - '
              'power-cycle the scanner and retry; if it persists at this dpi, try a lower one.')
        return
    written = imaging.export(raw, meta, out, fmts)
    driver.close_device()
    print('scan complete in %.1fs (%d dpi %s %d-bit):' % (time.time() - t0, a.dpi, a.mode, a.depth))
    for p in written:
        print('  %s (%d KB)' % (p, os.path.getsize(p) // 1024))

def main():
    p = argparse.ArgumentParser(prog='scan8000f', description='CanoScan 8000F native scanner')
    sub = p.add_subparsers(dest='cmd')

    s = sub.add_parser('scan', help='scan and export')
    s.add_argument('--dpi', type=int, default=300,
                   choices=[75, 100, 150, 200, 300, 400, 600, 800, 1200])
    s.add_argument('--mode', default='color', choices=['color', 'gray', 'lineart'])
    s.add_argument('--depth', type=int, default=8, choices=[8, 16])
    s.add_argument('--format', default='png', help='comma list: png,tif,jpg,pdf,raw')
    s.add_argument('--out', default='scan', help='output path without extension')
    s.add_argument('--trace', action='store_true',
                   help='write a USB transfer log to last_scan_trace.txt (diagnostic)')
    s.add_argument('--region', default=None, metavar='x0,y0,x1,y1',
                   help='scan only a sub-rectangle, in 0..1 bed coords (e.g. 0.25,0.1,0.75,0.6)')

    sub.add_parser('gui', help='launch the graphical interface')

    a = p.parse_args()
    if a.cmd == 'scan':
        _run_scan(a)
    elif a.cmd == 'gui':
        import gui; gui.main()
    else:
        p.print_help()

if __name__ == '__main__':
    main()
