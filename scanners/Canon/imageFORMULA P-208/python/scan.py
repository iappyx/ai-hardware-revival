#!/usr/bin/env python3
"""Scan on the Canon imageFORMULA P-208.

    scan.py                                 300 dpi colour, front only
    scan.py --mode gray --duplex            greyscale, both sides
    scan.py --batch --format pdf            feed the stack into one PDF
    scan.py --batch --duplex --skip-blank   drop blank backs
"""
import argparse
import os
import sys
import time

import imaging
from driver import P208, ScannerError, NotFound, Busy

EXTS = ('png', 'tif', 'tiff', 'jpg', 'jpeg', 'pdf')


DROPOUT = {'none': 0, 'red': 1, 'green': 2, 'blue': 3}


def main():
    ap = argparse.ArgumentParser(description='Scan on the Canon imageFORMULA P-208')
    ap.add_argument('--dpi', type=int, default=300,
                    choices=(150, 200, 300, 400, 600))
    ap.add_argument('--mode', default='color', choices=('color', 'gray'),
                    help='what the scanner acquires (default color)')
    ap.add_argument('--bitonal', action='store_true',
                    help='convert to black and white afterwards; this unit has '
                         'no hardware bitonal mode')
    ap.add_argument('--duplex', action='store_true', help='scan both sides')
    ap.add_argument('--single', action='store_true',
                    help='scan only the top sheet instead of the whole stack')
    # Accepted and ignored: feeding the stack is what happens by default now.
    ap.add_argument('--batch', action='store_true',
                    help=argparse.SUPPRESS)
    ap.add_argument('--skip-blank', action='store_true',
                    help='drop pages with essentially no content')
    ap.add_argument('--deskew', action='store_true',
                    help='straighten a sheet that fed crooked')
    ap.add_argument('--no-crop', action='store_true',
                    help='keep the full scanned window instead of trimming to the sheet')
    ap.add_argument('--no-cal', action='store_true', help='skip calibration')
    ap.add_argument('--brightness', type=int, default=0, metavar='N',
                    help='-100..100, added to every level (default 0)')
    ap.add_argument('--contrast', type=int, default=0, metavar='N',
                    help='-100..100, stretched about mid grey (default 0)')
    ap.add_argument('--gamma', type=float, default=1.0, metavar='G',
                    help='above 1 lightens midtones, below 1 darkens; the '
                         'black and white points stay put (default 1.0)')
    ap.add_argument('--gamma-rgb', metavar='R,G,B',
                    help='per-channel gamma, e.g. 1.0,1.0,0.95 to pull blue '
                         'back; colour scans only')
    ap.add_argument('--rotate', type=int, default=0, choices=(0, 90, 180, 270),
                    help='rotate by a right angle after scanning')
    ap.add_argument('--dither', action='store_true',
                    help='error diffusion instead of the adaptive threshold. '
                         'Implies --bitonal, since diffusing to a dot pattern '
                         'IS the black and white conversion. Better for '
                         'photographs, worse for small text.')
    ap.add_argument('--page-size', metavar='SIZE',
                    help='crop to a known size instead of detecting edges: '
                         + ', '.join(sorted(imaging.PAGE_SIZES)))
    ap.add_argument('--no-autosize', action='store_true',
                    help='do not let the scanner detect the page size itself; '
                         'it scans the whole window instead')
    ap.add_argument('--pdf-quality', default=imaging.DEFAULT_PDF_QUALITY,
                    help='how hard to compress a PDF: max (lossless), high, '
                         'balanced, small, or a number from 1 to 100. '
                         'Black-and-white pages are always lossless. '
                         '(default: %(default)s)')
    ap.add_argument('--continuous', action='store_true',
                    help='scan the whole stack as ONE long image instead of '
                         'separate pages - for a receipt or a document that '
                         'should stay in one piece')
    ap.add_argument('--enhance', choices=('none', 'red', 'green', 'blue'),
                    default='none',
                    help='emphasise a colour instead of dropping it - it comes '
                         'out dark rather than pale. Greyscale, like --dropout, '
                         'and the two cannot both be set.')
    ap.add_argument('--dropout', choices=('none', 'red', 'green', 'blue'),
                    default='none',
                    help='EXPERIMENTAL: drop a colour out in the scanner. The '
                         'result is greyscale, since the device sends one '
                         'channel when this is on. Shading is wrong in this '
                         'mode - the white strip is measured through the same '
                         'filter, so the page saturates and does not crop.')
    ap.add_argument('--no-tone', action='store_true',
                    help='skip the tone curve that lifts midtones after '
                         'shading; the page will look darker but nothing '
                         'is lost either way')
    ap.add_argument('--tone-strength', type=float, default=1.743,
                    help='how hard to lift (1.0 = off, higher = brighter and '
                         'more highlight compression; default 1.743)')
    ap.add_argument('--normalize-curve', action='store_true',
                    help='divide the factory table by its own mean, keeping '
                         'its shape but dropping the headroom; brighter, but '
                         'saturates white paper (expert)')
    ap.add_argument('--no-light-curve', action='store_true',
                    help="do not fold the unit's factory gain table into the "
                         "shading references; without it plain white paper "
                         "saturates")
    ap.add_argument('--out', default='scan', help='output name without extension')
    ap.add_argument('--format', default='png', help='comma separated: %s' % ', '.join(EXTS))
    ap.add_argument('--quiet', action='store_true')
    a = ap.parse_args()

    formats = [f.strip().lower() for f in a.format.split(',') if f.strip()]
    bad = [f for f in formats if f not in EXTS]
    if bad:
        sys.exit('unknown format(s): %s' % ', '.join(bad))

    def say(*m):
        if not a.quiet:
            print(*m)

    t0 = time.time()
    try:
        with P208() as s:
            inq = s.inquiry()
            say('  %s %s rev %s' % (inq['vendor'], inq['product'], inq['revision']))
            # Check the tray before spending several seconds on calibration.
            # The sensor is only readable outside a scan session, so this is
            # the one moment it can be asked.
            sen = s.sensors()
            if sen is not None and not sen['paper']:
                sys.exit('  no paper in the feeder')
            if not a.no_cal:
                say('  calibrating...')
            kw = dict(dpi=a.dpi, duplex=a.duplex, mode=a.mode,
                      calibrate=not a.no_cal,
                      light_curve=not a.no_light_curve,
                      dropout=(DROPOUT[a.dropout], DROPOUT[a.dropout]),
                      autosize=not a.no_autosize,
                      continuous=a.continuous,
                      enhance=(DROPOUT[a.enhance], DROPOUT[a.enhance]),
                      curve_normalize=a.normalize_curve)
            # The batch path handles one sheet as happily as twenty, stopping
            # when the tray empties, so it is the default and --single is the
            # exception rather than the other way round.
            if a.single:
                pages = [s.scan(**kw)]
            else:
                pages = list(s.scan_batch(**kw))
                if not pages:
                    sys.exit('  no sheets fed')
    except (NotFound, Busy) as e:
        sys.exit('  %s' % e)
    except ScannerError as e:
        sys.exit('  scan failed: %s' % e)

    # post-processing: nothing below here touches the scanner
    dropped = 0
    out = []
    for imgs in pages:
        keep = []
        for img in imgs:
            # Geometry first, and on the levels the scanner produced: the tone
            # curve compresses highlights, which flattens blank paper's own
            # texture and makes the page read as shorter than it is.
            # Trim BEFORE straightening. The trim finds the sheet by
            # comparing the frame against the backing in its leading rows, and
            # rotating first destroys exactly that: the corners fill with a
            # flat colour and rotated content lands in those rows, so the
            # reference is meaningless and the crop collapses - measured at
            # 5.27 x 13.34 in for an A4 page. Cropping first cannot lose the
            # corners, because what it returns is the sheet's bounding box,
            # which contains them by definition.
            if a.page_size:
                img = imaging.crop_to_size(img, a.page_size, dpi=a.dpi)
            elif not a.no_crop:
                img = imaging.autocrop(img)
            if a.deskew:
                img = imaging.deskew(img)
            # Then tone, so what follows sees the levels that will be written.
            if not a.no_tone:
                img = imaging.tone(img, a.tone_strength)
            if a.gamma != 1.0:
                img = imaging.gamma(img, a.gamma)
            if a.gamma_rgb:
                parts = [float(v) for v in a.gamma_rgb.split(',')]
                img = imaging.gamma(img, channels=dict(enumerate(parts)))
            if a.brightness or a.contrast:
                img = imaging.brightness_contrast(img, a.brightness, a.contrast)
            if a.rotate:
                img = imaging.rotate(img, a.rotate)
            # Diffusion is a black and white conversion, so --dither implies
            # --bitonal rather than silently doing nothing without it.
            if a.bitonal or a.dither:
                img = imaging.dither(img) if a.dither else imaging.binarize(img)
            if a.skip_blank and imaging.is_blank(img):
                dropped += 1
                continue
            keep.append(img)
        if keep:
            out.append(keep)

    if not out:
        sys.exit('  every page was blank')

    flat = [img for page in out for img in page]
    if 'pdf' in formats and len(flat) > 1:
        path = imaging.save_pdf(flat, '%s.pdf' % a.out, dpi=a.dpi,
                                quality=a.pdf_quality)
        say('  %s  %d page(s)' % (path, len(flat)))
        formats = [f for f in formats if f != 'pdf']

    for pno, imgs in enumerate(out):
        for si, img in enumerate(imgs):
            parts = [a.out]
            if len(out) > 1:
                parts.append('%03d' % (pno + 1))
            if len(imgs) > 1:
                parts.append(['front', 'back'][si])
            for f in formats:
                path = imaging.save(img, '%s.%s' % ('_'.join(parts), f), dpi=a.dpi)
                say('  %s  %dx%d' % (path, img.shape[1], img.shape[0]))

    if dropped:
        say('  %d blank page(s) skipped' % dropped)
    say('  %d sheet(s), %.1f s' % (len(out), time.time() - t0))


if __name__ == '__main__':
    main()
