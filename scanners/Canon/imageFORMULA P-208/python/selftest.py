#!/usr/bin/env python3
"""Offline checks. No scanner, no paper.

These exist because a run of bugs was introduced in one sitting and each one
cost a sheet of paper and a feed to find: a flag helper that corrupted the
calibration page, a read size the device could not keep up with, and an
`except` clause ordered above the subclass it was meant to let through. All
three were decidable without hardware. Run this before asking for a sheet.

    python3 selftest.py
"""
import sys

import imaging
import numpy as np
from driver import NoMedium, P208, PageComplete, ScannerError, ShortRead

PASS, FAIL = [], []


def check(name, got, want):
    (PASS if got == want else FAIL).append((name, got, want))


class Fake(P208):
    """A P208 whose reads replay a script, so the loops can be driven dry."""

    def __init__(self, script):
        self.script = list(script)
        self.last_sense, self.last_ili, self.last_info = (0, 0, 0), False, 0
        self.scan_dpi_y, self.dropout, self.autosize = None, (0, 0), True
        self.continuous = False
        self.fed = 0

    def object_position(self, fn):
        if fn == 1:
            self.fed += 1

    def read_data(self, length, dtype=0):
        if not self.script:
            raise NoMedium('script exhausted')
        kind, n = self.script.pop(0)
        if kind == 'full':
            return b'\xaa' * n
        if kind == 'short':
            raise ShortRead(b'\xbb' * n, length - n)
        if kind == 'empty':
            raise ShortRead(b'', length)
        if kind == 'done':
            raise PageComplete(b'\xcc' * n)
        if kind == 'nomed':
            raise NoMedium('no documents in the feeder')
        if kind == 'stall':                      # stalled pipe, sense says 3A
            self.last_sense = (5, 0x3a, 0)
            raise ScannerError('command 0x28 failed: no documents in the feeder')
        if kind == 'boom':                       # a real fault
            self.last_sense = (4, 0x44, 0)
            raise ScannerError('command 0x28 failed: hardware error')
        raise AssertionError(kind)

    def wait_ready(self, timeout=20.0, interval=0.15):
        pass

    def drain(self, chunk=None, cap=128):
        pass

    def _status(self):
        return 0


M = 1 << 20


def read_loop():
    """A short transfer is not the end of a page. Getting this wrong cut every
    long sheet off wherever the device first fell behind."""
    for name, script, want in (
            ('full page then stalled pipe',
             [('full', M), ('full', M), ('short', 900000), ('stall', 0)], 2 * M + 900000),
            ('short in the middle keeps going',
             [('full', M), ('short', 500000), ('full', M), ('done', 1000)],
             2 * M + 500000 + 1000),
            ('page complete ends it', [('full', M), ('done', 5000)], M + 5000),
            ('no medium ends it', [('full', M), ('nomed', 0)], M),
            ('empty shorts give up eventually',
             [('full', M)] + [('empty', 0)] * 12, M)):
        check(name, len(Fake(script)._feed_and_read(chunk=M, cap=40)), want)

    try:
        Fake([('full', M), ('boom', 0)])._feed_and_read(chunk=M, cap=40)
        check('a real fault still raises', 'returned', 'raised')
    except ScannerError as e:
        check('a real fault still raises', type(e) is ScannerError, True)


def exception_order():
    """ShortRead, PageComplete and NoMedium all subclass ScannerError, so a
    general clause placed above them silently swallows the lot."""
    import inspect
    import re
    for fn in (P208._feed_and_read, P208.scan_batch):
        src = inspect.getsource(fn)
        # Only the read loop matters; the feed above it has its own try block
        # whose ScannerError clause is unrelated.
        cut = src.find('range(cap)')
        if cut < 0:
            cut = src.find('while True')
        order = re.findall(r'except (\w+)', src[cut:] if cut > 0 else src)
        specific = [i for i, n in enumerate(order)
                    if n in ('ShortRead', 'PageComplete', 'NoMedium')]
        general = [i for i, n in enumerate(order) if n == 'ScannerError']
        late = [g for g in general if specific and g < max(specific)
                and any(s > g for s in specific)]
        check('%s: general clause after the specific ones' % fn.__name__,
              late, [])


def feed_flags():
    """Continuous feed (0x40) makes the device stream the whole stack as one
    unbroken image - it never marks a sheet boundary, so eight sheets arrive as
    one page. It must stay OFF unless explicitly asked for."""
    s = Fake([])
    cases = (
        (False, True,  0x20),   # normal: autosize on, continuous off
        (False, False, 0x00),
        (True,  True,  0x60),   # one long image, page size still detected
        (True,  False, 0x40),
    )
    for cont, auto, want in cases:
        s.continuous, s.autosize = cont, auto
        check('feed flags continuous=%s autosize=%s' % (cont, auto),
              s._feed_flags(0x60), want)

    s.continuous, s.autosize = False, True
    check('continuous is off unless asked for',
          s._feed_flags(0x60) & P208.FEED_CONTINUOUS, 0)


def scan_plan():
    """X is only ever 300 or 600 - anything else shears, because the sensor
    width is not a whole number of pixels at those resolutions."""
    for want, (dx, dy) in P208.SCAN_PLAN.items():
        check('%d dpi scans X at 300 or 600' % want, dx in (300, 600), True)
        check('%d dpi Y is not 150 unless X is 300' % want,
              dy != 150 or dx == 300, True)
    for dpi in (150, 300, 600):
        check('%d dpi is a whole number of pixels' % dpi,
              (10208 * dpi) % 1200, 0)


def geometry():
    """Trimming must survive a page whose foot is blank, and must not depend on
    a fixed fraction of the frame being backing."""
    rng = np.random.default_rng(0)
    page = np.full((3000, 2552), 230, np.uint8)
    page[:120] = 177                                    # lead-in, flat backing
    page[120:1200] = rng.integers(90, 240, (1080, 2552))  # print
    page[1200:2900] = np.clip(230 + rng.normal(0, 3, (1700, 2552)), 0, 255)  # blank paper
    page[2900:] = 177                                   # backing again
    b = imaging.sheet_bounds(page)
    check('blank foot is kept', b is not None and b[1] > 2500, True)


def main():
    for fn in (read_loop, exception_order, feed_flags, scan_plan, geometry):
        fn()
    for name, got, want in PASS:
        print('  ok    %s' % name)
    for name, got, want in FAIL:
        print('  FAIL  %s: got %r, wanted %r' % (name, got, want))
    print('  %d passed, %d failed' % (len(PASS), len(FAIL)))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
