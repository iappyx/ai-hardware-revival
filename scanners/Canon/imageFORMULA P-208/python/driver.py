#!/usr/bin/env python3
"""Driver for the Canon imageFORMULA P-208 sheet-fed duplex scanner.

Pure Python over libusb. The scanner speaks a SCSI command set carried on the
two bulk endpoints, with a 12-byte header in front of each CDB:

    00 00 00 LL  00 01 90 00  00 00 00 00 | CDB...
             ^^ total length  ^^^^^^^^^^^ signature   ^ CDB starts here

Status comes back as a 4-byte read on the IN endpoint. Data phases follow the
command on whichever endpoint the direction implies.

The device only presents a scanner interface when the Auto Start switch on the
back is OFF; with it ON the unit enumerates as USB mass storage (a different
product id) and no scanner interface exists at all.
"""

import struct
import time

import imaging

VID = 0x1083
PID_SCANNER = 0x164c   # Auto Start OFF
PID_STORAGE = 0x164e   # Auto Start ON - mass storage, not scannable

HDR_SIG = b'\x00\x01\x90\x00'

# SCSI opcodes this device answers
TEST_UNIT_READY = 0x00
REQUEST_SENSE   = 0x03
INQUIRY         = 0x12
MODE_SELECT     = 0x15
RESERVE_UNIT    = 0x16
RELEASE_UNIT    = 0x17
SET_WINDOW      = 0x24
GET_WINDOW      = 0x25
READ10          = 0x28
OBJECT_POSITION = 0x31
GET_MEMORY      = 0x3b   # read the unit's own memory
ADJUST_DATA     = 0xe1   # analog front-end gain/offset/exposure
GET_SCAN_MODE   = 0xd5   # reads back the 0xd6 mode pages
STOP_BATCH      = 0xd8   # abort a running feed
ERROR_CLEAR     = 0xc7   # clear a latched error
SCANNER_STATUS  = 0xc5   # eight bytes of device status
SET_SCAN_MODE   = 0xd6   # source/buffer mode, incl. the duplex bit
# 0xe5 was once used here for a 'hardware enhancement' page. The device
# rejects it and nothing has ever been seen to use it - it was never a real
# command. Removed.

TIMEOUT_MS = 30000


class ScannerError(RuntimeError):
    pass


class ShortRead(ScannerError):
    """The device sent less than was asked for, and that is not an error.

    Status 2 with sense key 0 means a short transfer: the residue is in the
    sense INFORMATION field, gated on ILI. The partial buffer has to be kept
    and the read reissued; treating it as the end of the page stops mid-sheet
    and loses whatever had not arrived yet.
    """

    def __init__(self, data, residue):
        super().__init__('short transfer, %d bytes short' % residue)
        self.data = data
        self.residue = residue


class PageComplete(ScannerError):
    """ASC 8101: the device has finished the sheet. The real end of a page."""

    def __init__(self, data=b''):
        super().__init__('page complete')
        self.data = data


class NoMedium(ScannerError):
    """ASC 3A: nothing in the feeder. How a batch ends."""
    pass


class NotFound(ScannerError):
    pass


class Busy(ScannerError):
    pass


def _backend():
    import usb.backend.libusb1
    for path in ('/opt/homebrew/lib/libusb-1.0.dylib',
                 '/usr/local/lib/libusb-1.0.dylib',
                 '/usr/lib/x86_64-linux-gnu/libusb-1.0.so.0'):
        try:
            b = usb.backend.libusb1.get_backend(find_library=lambda x, p=path: p)
            if b:
                return b
        except Exception:
            pass
    try:
        import libusb_package
        return libusb_package.get_libusb1_backend()
    except Exception:
        return None


class P208:
    """One scanner session. Open, use, close."""

    def __init__(self):
        self.dev = None
        self._intf = None
        self._in_sense = False
        self.last_sense = None
        self.last_ili = False
        self.last_info = 0
        self.ep_in = None
        self.ep_out = None
        self._light_curve = None      # factory table, read once per session
        self.dropout = (0, 0)         # colour drop-out, (front, back)
        self.enhance = (0, 0)         # colour emphasis, the exclusive partner
        self.scan_dpi_y = None        # vertical dpi, when it differs from x
        self.autosize = True          # let the device detect the page size

    # ---- connection ------------------------------------------------------

    def open(self):
        import usb.core
        import usb.util

        b = _backend()
        self.dev = usb.core.find(idVendor=VID, idProduct=PID_SCANNER, backend=b)
        if self.dev is None:
            if usb.core.find(idVendor=VID, idProduct=PID_STORAGE, backend=b):
                raise NotFound(
                    'P-208 found in mass-storage mode. Set the Auto Start switch '
                    'on the back of the scanner to OFF and replug the cable.')
            raise NotFound('no P-208 on the bus')

        try:
            self.dev.set_configuration()
        except Exception as e:
            raise Busy('cannot claim the scanner (another process may hold it): %s' % e)

        cfg = self.dev.get_active_configuration()
        intf = cfg[(0, 0)]
        self._intf = intf.bInterfaceNumber
        try:
            usb.util.claim_interface(self.dev, self._intf)
        except Exception as e:
            raise Busy('cannot claim interface %d: %s' % (self._intf, e))
        for ep in intf:
            if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN:
                self.ep_in = ep.bEndpointAddress
            else:
                self.ep_out = ep.bEndpointAddress
        if self.ep_in is None or self.ep_out is None:
            raise ScannerError('expected one bulk IN and one bulk OUT endpoint')

        # A previous session that died mid-transfer leaves the pipes halted and
        # the device stalls everything until they are cleared. Clear them, then
        # prove the link with a command that has no data phase.
        self.clear_halts()
        try:
            self.test_unit_ready()
        except Exception:
            self.clear_halts()
            self.test_unit_ready()

        # A scan left running by a previous session blocks the next SCAN and the
        # device stalls the pipe. Releasing costs nothing when none is pending.
        try:
            self.object_position(0)
        except Exception:
            self.clear_halts()

        try:
            self.reserve_unit()
        except Exception:
            pass
        return self

    def close(self):
        if self.dev is not None:
            try:
                self.release_unit()
            except Exception:
                pass
            try:
                import usb.util
                if getattr(self, '_intf', None) is not None:
                    usb.util.release_interface(self.dev, self._intf)
                usb.util.dispose_resources(self.dev)
            except Exception:
                pass
        self.dev = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    # ---- command layer ---------------------------------------------------

    @staticmethod
    def _wrap(cdb):
        """12-byte header + CDB, as the device expects on the OUT endpoint.

        The length field counts everything after itself, i.e. record length - 4:
        a 24-byte command record carries 0x14, a 64-byte data record 0x3c."""
        return struct.pack('>I', 8 + len(cdb)) + HDR_SIG + b'\x00' * 4 + bytes(cdb)

    def clear_halts(self):
        try:
            self.dev.clear_halt(self.ep_in)
            self.dev.clear_halt(self.ep_out)
        except Exception:
            pass

    def _status(self):
        """Every command completes with a 4-byte status read."""
        st = bytes(self.dev.read(self.ep_in, 4, TIMEOUT_MS))
        if len(st) != 4:
            raise ScannerError('short status (%d bytes)' % len(st))
        return struct.unpack('>I', st)[0]

    def cmd(self, cdb, data_out=None, read_len=0):
        """Issue one command. Returns the data phase, or b'' if there is none.

        Any USB-level failure clears the halted endpoints before propagating.
        A rejected or unsupported command halts the pipe, and an uncleared halt
        makes every later command fail too - which looks like a wedged scanner
        and survives closing the process, so it is worth handling in one place
        rather than at each call site.
        """
        try:
            self.dev.write(self.ep_out, self._wrap(cdb), TIMEOUT_MS)

            if data_out is not None:
                payload = struct.pack('>I', 8 + len(data_out)) + b'\x00\x02\xb0\x00' \
                          + b'\x00' * 4 + bytes(data_out)
                self.dev.write(self.ep_out, payload, TIMEOUT_MS)

            got = b''
            if read_len:
                got = bytes(self.dev.read(self.ep_in, read_len, TIMEOUT_MS))

            st = self._status()
        except ScannerError:
            raise
        except Exception as exc:
            # A stalled data phase surfaces as a bare USB pipe error, which
            # says nothing useful - an open cover and an empty feeder look
            # identical. The device still has the sense data, so fetch it and
            # report the actual condition.
            try:
                self.clear_halts()
            except Exception:
                pass
            cond = None
            if not self._in_sense:
                try:
                    cond = self._clear_condition()
                except Exception:
                    cond = None
            if cond:
                raise ScannerError('command 0x%02x failed: %s'
                                   % (cdb[0], cond)) from exc
            raise

        if st != 0:
            # SCSI holds a contingent-allegiance condition after a check
            # condition and stalls every later command until the sense data is
            # read, so the sense must be fetched whatever the outcome.
            cond = self._clear_condition()
            key, asc, ascq = self.last_sense or (None, None, None)

            # Status 2 covers three different things, and telling them apart is
            # the whole game. Reading them all as "the page is over" is what cut
            # long sheets short: the device says "less than you asked for" far
            # more often than it says "finished".
            if asc == 0x81 and ascq == 0x01:
                raise PageComplete(got)
            if asc == 0x3a:
                raise NoMedium(cond or 'no documents in the feeder')
            if key == 0:
                # The residue is how much of the request was NOT filled, so
                # only the first (requested - residue) bytes are image. The
                # tail is whatever was already in the buffer; keeping it puts
                # a block of noise into the page and makes the sheet look as
                # though it runs to the end of the frame.
                good = max(0, len(got) - self.last_info)
                raise ShortRead(got[:good], self.last_info)

            raise ScannerError('command 0x%02x returned status 0x%08x%s'
                               % (cdb[0], st, (': ' + cond) if cond else ''))
        return got

    # ---- device memory ----------------------------------------------------

    GET_MEMORY_CHUNK = 0x2000

    def get_memory(self, addr, length):
        """Read `length` bytes from the unit's own memory at `addr`.

        Opcode 0x3b is WRITE BUFFER in the SCSI standard, but this unit uses it
        to read: the address is big-endian in bytes 2..5 and the transfer
        length is a 24-bit big-endian count in bytes 6..8. The region is read
        in 0x2000-byte commands rather than asked for all at once.
        """
        out = bytearray()
        while len(out) < length:
            n = min(self.GET_MEMORY_CHUNK, length - len(out))
            cdb = bytearray(12)
            cdb[0] = GET_MEMORY
            cdb[2:6] = struct.pack('>I', addr + len(out))
            cdb[6:9] = struct.pack('>I', n)[1:]
            got = self.cmd(cdb, read_len=n)
            if not got:
                break
            out += got
        return bytes(out)

    # ---- the factory light curve -----------------------------------------

    LIGHT_CURVE_ADDR  = 0x10080000
    LIGHT_CURVE_BYTES = 0x80000
    LIGHT_CURVE_MAGIC = 0x51232c37

    def read_light_curve(self):
        """The unit's per-pixel factory gain table, read from its own memory.

        This is per-unit data measured at the factory. It cannot be shipped as
        a constant: it describes this scanner. Returns
        {(side, channels, dpi): (curve, scale)} where `curve` is a
        (channels, width) array and `scale` is the full-scale the values are
        expressed against, or {} if the table is missing or unrecognised.

        Layout: a 12-byte file header whose third word is the record count,
        then that many records of a 34-byte header followed by
        `channels * width` uint16. The region past the last record is 0xff,
        i.e. erased flash.
        """
        import numpy as np

        blob = self.get_memory(self.LIGHT_CURVE_ADDR, self.LIGHT_CURVE_BYTES)
        if len(blob) < 12:
            return {}
        magic, _, count = struct.unpack_from('<3I', blob, 0)
        if magic != self.LIGHT_CURVE_MAGIC:
            return {}

        out, pos = {}, 12
        for _ in range(count):
            if pos + 34 > len(blob):
                break
            kind, side, channels, _bpp = struct.unpack_from('<4H', blob, pos)
            if kind != 34:
                break
            scale, width, _, nbytes = struct.unpack_from('<4I', blob, pos + 8)
            dpi, = struct.unpack_from('<H', blob, pos + 24)
            if pos + 34 + nbytes > len(blob) or channels * width * 2 != nbytes:
                break
            data = np.frombuffer(blob, dtype='<u2', count=nbytes // 2,
                                 offset=pos + 34).astype(np.float64)
            out[(side, channels, dpi)] = (data.reshape(channels, width), float(scale))
            pos += 34 + nbytes
        return out

    def light_curve_for(self, dpi=300, mode='color', side=0):
        """The curve matching one acquisition, flattened to match a planar
        line, or None when nothing can be had for it.

        The table only carries 300 and 600 dpi. Without a curve there is no
        headroom, and that is not a small loss: at 150 dpi 94.6% of a page came
        back clipped at the shading target, which made blank paper perfectly
        flat and therefore indistinguishable from the backing - so the trim read
        the blank foot of a page as empty belt and cut it off. The symptom
        looked like a cropping bug and was a calibration one.

        A lower resolution that divides a stored one is derived from it by
        averaging each group of samples, which is what scanning at that
        resolution does to the sensor anyway.
        """
        import numpy as np

        if self._light_curve is None:
            try:
                self._light_curve = self.read_light_curve()
            except ScannerError:
                self._light_curve = {}
        channels = 3 if mode == 'color' else 1

        rec = self._light_curve.get((side, channels, dpi))
        if rec is None:
            rec = self._derive_curve(side, channels, dpi)
        if rec is None:
            return None
        curve, scale = rec
        return curve.reshape(-1), scale

    def _derive_curve(self, side, channels, dpi):
        """Build a curve for `dpi` from a stored one at a whole multiple."""
        import numpy as np

        for have in sorted({k[2] for k in self._light_curve}):
            if have <= dpi or have % dpi:
                continue
            rec = self._light_curve.get((side, channels, have))
            if rec is None:
                continue
            curve, scale = rec
            step = have // dpi
            width = curve.shape[1]
            if width % step:
                continue
            small = curve.reshape(curve.shape[0], width // step, step).mean(axis=2)
            return small, scale
        return None

    @staticmethod
    def apply_light_curve(dark, white, curve, scale, normalize=False):
        """Correct a measured white reference with the factory curve.

        The measured span is rescaled about the dark level:

            white = dark + (white - dark) * curve / scale

        Our references are per-cell means rather than a full 16-bit image,
        but the correction is linear and scale-free, so it carries over
        unchanged. Cells where the span is not positive are dead and are left
        alone rather than being given a synthetic span.

        `normalize` divides the curve by its own mean first. The table's values
        average about 1.3, so applied as they stand they enlarge every span by
        a third and the page comes out that much darker - and the level cannot
        be recovered through the shading target, which would have to exceed 255.
        Normalising keeps what varies (roughly 6% across the page, 1% between
        channels, which is the part that changes the picture) and drops the
        constant, so enabling the curve no longer costs a third of the output
        range. The mean is taken across all channels together so the
        channel-relative correction survives.
        """
        import numpy as np

        dark = np.asarray(dark, dtype=np.float64)
        white = np.asarray(white, dtype=np.float64)
        if curve is None or curve.size != white.size:
            return white
        gain = np.asarray(curve, dtype=np.float64) / scale
        if normalize:
            m = gain.mean()
            if m > 0:
                gain = gain / m
        span = white - dark
        good = span > 0
        out = white.copy()
        out[good] = dark[good] + span[good] * gain[good]
        return out

    # ---- the handful of commands needed to identify the unit -------------

    def test_unit_ready(self):
        self.cmd(bytes(12))

    def inquiry(self, length=0x24):
        cdb = bytes([INQUIRY, 0, 0, 0, length]) + bytes(7)
        d = self.cmd(cdb, read_len=length)
        return {
            'peripheral': d[0],
            'version': d[2],
            'vendor': d[8:16].decode('ascii', 'replace').strip(),
            'product': d[16:32].decode('ascii', 'replace').strip(),
            'revision': d[32:36].decode('ascii', 'replace').strip(),
            'raw': d,
        }


    # ---- scan window -----------------------------------------------------

    # Geometry is in 1/1200 inch, set explicitly with MODE SELECT page 3
    # before any window is programmed.
    UNITS_PER_INCH = 1200

    SIDE_FRONT = 0
    SIDE_BACK = 1

    # SET WINDOW image composition. The hardware produces each of these itself,
    # so the mode changes what arrives on the wire, not just how it is decoded.
    MODE_GRAY = 0x02         # 1 byte per pixel
    MODE_COLOR = 0x05        # 3 bytes per pixel, planar per line

    # This unit reports no monochrome capability (INQUIRY byte 0x1c bit 1 is
    # clear) and rejects a bitonal window outright, so bitonal output is
    # produced in software - see imaging.binarize().
    MODES = {'gray': MODE_GRAY, 'color': MODE_COLOR}

    def capabilities(self):
        """What the device says it can do: INQUIRY EVPD page 0xf0.

        Limits are taken from here rather than assumed. Lengths come back as
        pixel counts at the maximum resolution, so they convert to 1/1200 inch
        by 1200 / that resolution.
        """
        cdb = bytes([INQUIRY, 0x01, 0xf0, 0, 0x30]) + bytes(7)
        d = self.cmd(cdb, read_len=0x30)
        if len(d) < 0x1c:
            return None
        be16 = lambda o: int.from_bytes(d[o:o + 2], 'big')
        be32 = lambda o: int.from_bytes(d[o:o + 4], 'big')
        xdpi, ydpi = be16(5), be16(7)
        return {
            'max_dpi_x': xdpi, 'max_dpi_y': ydpi,
            'min_dpi_x': be16(14), 'min_dpi_y': be16(16),
            'max_width_px': be32(20), 'max_length_px': be32(24),
            'max_width_units': be32(20) * 1200 // max(xdpi, 1),
            'max_length_units': be32(24) * 1200 // max(ydpi, 1),
            'flags': int.from_bytes(d[29:32], 'big'),
            'ultrasonic_dfd': bool((int.from_bytes(d[29:32], 'big') >> 6) & 1),
        }

    def stop_batch(self):
        """Abort a running feed (opcode 0xd8).

        Without it the only way out of a started scan is to let it run to the
        end or to wedge the endpoint.
        """
        self.cmd(bytes([STOP_BATCH]) + bytes(11))

    def mode_select_units(self):
        """Page 3: measurement units = 1200 per inch."""
        cdb = bytes([MODE_SELECT, 0x10, 0, 0, 0x0c]) + bytes(7)
        body = bytes(4) + bytes([0x03, 0x06, 0, 0]) + struct.pack('>H', self.UNITS_PER_INCH) + bytes(2)
        self.cmd(cdb, data_out=body)

    # Unit counts, not inches converted on the way in. 8.5 in is 10200 units
    # while the sensor is 10208, so a window derived from inches came out eight
    # units narrower than the pixel count computed from it. That divides evenly
    # at 300 and 600 dpi and not at the rest, which is why only those two were
    # ever right.
    WIN_WIDTH_UNITS = 10208          # 8.51 in
    WIN_LENGTH_UNITS = 17736         # 14.78 in

    # Requested dpi -> the (dpi_x, dpi_y) actually scanned. Only 300 and 600
    # come out right on both axes, so every other resolution is scanned at one
    # of them and resampled. The two axes fail for different reasons:
    #
    #   X must be 300 or 600. The sensor is 10208 units wide at 1200 per inch,
    #   a whole number of pixels only at 150, 300 and 600; at 200 it is 1701.33
    #   and the third of a pixel lost per line walks the image sideways -
    #   measured 275 columns of drift over 1400 rows, on a sheet whose own skew
    #   was 0.166 degrees and accounts for none of it.
    #
    #   Y may be anything except 150. The device fixes the scanned length at
    #   12.32 in and lets the line count follow dpi_y, but at 150 it delivers
    #   10.95 in and cuts mid-line when its buffer runs dry, by which point the
    #   sheet has already been ejected. Measured:
    #
    #       300 x 300   3697 lines   12.32 in
    #       300 x 200   2465 lines   12.32 in
    #       300 x 150   1643 lines   10.95 in   <- short
    #       150 x 150   1643 lines   10.95 in   <- short
    #
    #   dpi_y above dpi_x is refused outright, so no combination yields 150
    #   lines per inch and a whole page: 150 is scanned at 300 and halved.
    #
    # 150 was scanned 300x300 here until short transfers were handled properly.
    # Y=150 short-transfers more than any other setting, which is what made it
    # look broken.
    SCAN_PLAN = {150: (300, 150), 200: (300, 200), 240: (300, 240),
                 400: (600, 400)}

    def set_window(self, side, dpi_x=300, dpi_y=300,
                   ul_x=0, ul_y=-468, width_units=None, length_units=None,
                   mode='color', threshold=0x80):
        """One window descriptor per side.

        ul_y is negative: the window opens before the paper's leading edge, so
        the scan is already running when the sheet arrives. Sizes are in
        1/1200 inch, as the device wants them, rather than inches rounded on
        the way in.
        """
        w = int(self.WIN_WIDTH_UNITS if width_units is None else width_units)
        l = int(self.WIN_LENGTH_UNITS if length_units is None else length_units)

        d = bytearray(52)
        struct.pack_into('>H', d, 6, 44)          # window descriptor length
        d[8] = side                                # window identifier
        struct.pack_into('>H', d, 10, dpi_x)
        struct.pack_into('>H', d, 12, dpi_y)
        struct.pack_into('>i', d, 14, ul_x)
        struct.pack_into('>i', d, 18, ul_y)
        struct.pack_into('>I', d, 22, w)
        struct.pack_into('>I', d, 26, l)
        composition = self.MODES[mode]
        d[30] = 0x80                               # brightness, neutral
        d[31] = threshold                          # only used for lineart
        d[32] = 0x80                               # contrast
        d[33] = composition
        d[34] = 8                                  # bits per sample
        d[37] = 0x10
        d[50] = 0x88

        cdb = bytes([SET_WINDOW, 0, 0, 0, 0, 0, 0, 0, len(d)]) + bytes(3)
        self.cmd(cdb, data_out=bytes(d))

    def get_window(self, length=52):
        cdb = bytes([GET_WINDOW, 0, 0, 0, 0, 0, 0, 0, length]) + bytes(3)
        return self.cmd(cdb, read_len=length)



    # ---- scanning --------------------------------------------------------

    # READ(10) byte 2 selects what is being read.
    DT_IMAGE = 0x00
    DT_PIXELSIZE = 0x80      # the page size the scanner itself detected
    DT_PANEL = 0x84
    DT_SENSORS = 0x8b
    DT_COUNTERS = 0x8c

    def object_position(self, function):
        """1 = feed/position a sheet, 0 = release it."""
        cdb = bytes([OBJECT_POSITION, function]) + bytes(10)
        self.cmd(cdb)

    def scan_start(self, windows=(0, 1)):
        """SCAN. The data phase is the list of window ids to acquire, so one
        entry is simplex and two is duplex."""
        ids = bytes(windows)
        cdb = bytes([0x1b, 0, 0, 0, len(ids)]) + bytes(7)
        self.cmd(cdb, data_out=ids)

    def read_data(self, length, dtype=DT_IMAGE):
        cdb = bytes([READ10, 0, dtype, 0, 0, 0,
                     (length >> 16) & 0xff, (length >> 8) & 0xff, length & 0xff]) + bytes(3)
        return self.cmd(cdb, read_len=length)

    # ---- sensors, counters -------------------------------------------------

    def sensors_raw(self):
        """The sensor byte, exactly as the device returns it.

        READ(10) with data type 0x8b and a length of one.
        """
        d = self.read_data(1, dtype=self.DT_SENSORS)
        return d[0] if d else None

    def counters_raw(self):
        """The 128-byte counter block (READ(10), data type 0x8c)."""
        return self.read_data(0x80, dtype=self.DT_COUNTERS)

    def sensors(self):
        """Decoded sensor byte.

        Only bit 0 is identified: it is set when there is paper in the feeder,
        measured by reading with and without a sheet. The other bits stayed
        zero throughout, so they are reported raw rather than guessed at.

        With the cover open the device stalls this read instead of answering,
        so a cover check is `request_sense`, not a bit here.
        """
        b = self.sensors_raw()
        if b is None:
            return None
        return {'raw': b, 'paper': bool(b & 0x01)}

    def counters(self):
        """The counter block: the unit's serial and its running totals.

        The serial is eight ASCII characters at offset 0x6c. The counts are
        big-endian 32-bit words from the start of the block; which is which is
        not established, so they are returned in order rather than given names
        that might be wrong. On the unit this was read from, several words
        repeat, which is consistent with per-side totals alongside a grand
        total.
        """
        d = self.counters_raw()
        if not d or len(d) < 0x74:
            return None
        return {
            'raw': d,
            'serial': d[0x6c:0x74].decode('ascii', 'replace').strip('\x00').strip(),
            'values': [int.from_bytes(d[i:i + 4], 'big') for i in range(0, 0x28, 4)],
        }

    def scanner_status(self, length=8):
        """Eight bytes of device status, opcode 0xc5.

        The device answers, but the individual bytes have never been pinned to
        a known state, so treat the contents as unverified.
        """
        cdb = bytes([SCANNER_STATUS, 0, 0, 0, length]) + bytes(7)
        return self.cmd(cdb, read_len=length)

    MODE_PAGES = (0x30, 0x32, 0x36)

    def scan_mode_page(self, page, length=0x14):
        """Read back one 20-byte mode page via opcode 0xd5.

        The read counterpart of SET SCAN MODE (0xd6) - the same page numbers,
        and page 0x32 is the one carrying the duplex bit. Byte 2 of the CDB
        selects the page and byte 4 the length. 0x30, 0x32 and 0x36 are the
        pages this device implements.
        """
        cdb = bytes([GET_SCAN_MODE, 0, page, 0, length]) + bytes(7)
        return self.cmd(cdb, read_len=length)

    def read_page_size(self, dpi=300):
        """Ask the scanner for the size of the sheet it just fed.

        Returns (width_px, height_px) at the given dpi, or None if the device
        does not answer. Far better than inferring the page edge from the
        image: this is the scanner's own measurement.

        The reply is 16 bytes with two 4-byte big-endian values in 1/1200 inch.
        """
        cdb = bytearray(12)
        cdb[0] = READ10
        cdb[2] = self.DT_PIXELSIZE
        cdb[5] = 0x02
        cdb[6], cdb[7], cdb[8] = 0, 0, 16
        try:
            d = self.cmd(bytes(cdb), read_len=16)
        except Exception:
            # A rejected query halts the pipe; clearing it here keeps a failed
            # size read from breaking everything that follows.
            try:
                self.clear_halts()
                self.request_sense()
            except Exception:
                pass
            return None
        if len(d) < 16:
            return None
        w = int.from_bytes(d[8:12], 'big')
        h = int.from_bytes(d[12:16], 'big')
        if w <= 0 or h <= 0:
            return None
        return (int(round(w * dpi / 1200.0)), int(round(h * dpi / 1200.0)))

    def read_status_page(self, dtype, length):
        return self.read_data(length, dtype=dtype)


    # ---- analog front end ------------------------------------------------

    # Special window ids: scanning these acquires the internal references
    # rather than the paper path, so no sheet is needed.
    WIN_DARK = 0xff
    WIN_WHITE = 0xfe

    def write_afe(self, gain, offset, exposure):
        """Program the analog front end.

        gain/offset are one value per side; exposure is three per side. The
        payload is two symmetric 20-byte blocks - gain(3+pad), offset(3+pad),
        exposure(3 x u16), pad - front then back.
        """
        body = bytearray(40)
        for side in (self.SIDE_FRONT, self.SIDE_BACK):
            b = side * 20
            g, o = gain[side] & 0xff, offset[side] & 0xff
            body[b + 0] = body[b + 1] = body[b + 2] = g
            body[b + 4] = body[b + 5] = body[b + 6] = o
            for c in range(3):
                struct.pack_into('>H', body, b + 8 + c * 2, exposure[side][c] & 0xffff)

        cdb = bytes([ADJUST_DATA, 0, 0, 0, 0, 0x03, 0, 0, len(body)]) + bytes(3)
        self.cmd(cdb, data_out=bytes(body))


    def drain(self, chunk=0x100000, cap=128):
        """Read a started scan to end-of-data and discard it.

        Leaving a scan part-read wedges the device in a way that survives
        clearing the pipes, OBJECT POSITION 0 and even a USB reset - only a
        power cycle recovers it. Every path that starts a scan must finish it.
        """
        for _ in range(cap):
            try:
                d = self.read_data(chunk)
            except ScannerError as e:
                if '0x00000002' in str(e):
                    return
                return
            except Exception:
                return
            if not d:
                return

    CAL_STREAM_LINES = 48       # one strip is 48 stream lines at any dpi

    def calibration_scan(self, window, dpi=300, sides=1, lines=None, mode='color'):
        """Acquire one internal reference strip.

        One fixed-size block - 48 stream lines, 367488 bytes at 300 dpi - then
        release with OBJECT POSITION 0. Reading to end-of-data instead pulls
        61 MB and leaves the device unable to start another scan.
        """
        n = (lines or self.CAL_STREAM_LINES) * self.line_bytes(dpi, mode=mode)
        self.scan_start((window,) * sides)
        try:
            try:
                return self.read_data(n)
            except ShortRead as e:
                # A reference strip can short-transfer like any other read.
                # What came back is still valid signal; the servo works on
                # percentiles, so a few missing lines change nothing.
                return e.data
            except PageComplete as e:
                return e.data
        finally:
            try:
                self.object_position(0)
            except Exception:
                self.clear_halts()

    @staticmethod
    def pixels(dpi, width_units=None):
        # Same constant the window is programmed with, so the pixel count and
        # the window can never drift apart again.
        if width_units is None:
            width_units = P208.WIN_WIDTH_UNITS
        return int(round(width_units * dpi / 1200.0))

    @staticmethod
    def line_bytes(dpi, width_units=None, mode='color'):
        """Bytes per scan line.

        colour   planar R|G|B, one byte per channel per pixel
        gray     one byte per pixel
        """
        w = P208.pixels(dpi, width_units)
        return w * 3 if mode == 'color' else w


    # ---- shading (fine) calibration --------------------------------------

    def measure_references(self, dpi=300, sides=1):
        """Acquire the internal dark and white strips and reduce each to one
        value per sensor cell. Returns (dark, white) as lists of ints."""
        import numpy as np

        line = self.line_bytes(dpi)
        out = []
        for window in (self.WIN_DARK, self.WIN_WHITE):
            raw = self.calibration_scan(window, dpi=dpi, sides=sides)
            a = np.frombuffer(raw, dtype=np.uint8)
            n = a.size // line
            if not n:
                raise ScannerError('reference scan returned no complete lines')
            out.append(a[:n * line].reshape(n, line).mean(axis=0))
        return out[0], out[1]

    @staticmethod
    def apply_shading(raw, dark, white, dpi=300, target=230):
        """Per-cell shading: (pixel - dark) * target / (white - dark).

        target defaults to 240, matching the headroom both reference
        implementations leave: paper is often brighter than the internal white
        strip, and mapping the strip to 255 clips whatever exceeds it.

        Applied on the planar line as it arrives, so `dark` and `white` are
        indexed the same way - all red cells, then green, then blue.
        """
        import numpy as np

        line = dark.size
        a = np.frombuffer(raw, dtype=np.uint8)
        n = a.size // line
        a = a[:n * line].reshape(n, line).astype(np.float32)

        span = np.maximum(white - dark, 1.0)
        corrected = (a - dark) * (float(target) / span)
        return np.clip(corrected, 0, 255).astype(np.uint8)


    # ---- coarse (analog front end) calibration ---------------------------
    #
    # The device measures in 12 bits; the bulk stream gives us the top 8, so a
    # reading of n here is n*16 in the units the servo constants assume.
    #
    #   dark target   96 of 4096   ->   6.0 in 8-bit
    #   white target 2730 of 4096  -> 170.6 in 8-bit, two thirds of full scale
    #
    # The white target leaves a third of the range as headroom, which is why
    # aiming near full scale over-exposes.

    ADC_BITS = 12
    DARK_TARGET_12 = 96
    WHITE_TARGET_12 = 2730
    GAIN_MAX = 63          # register saturates here
    EXPOSURE_MAX = 0x1fff
    OFFSET_MAX = 255

    def calibrate_afe(self, dpi=300, sides=1, passes=8, verbose=False):
        """Derive the analog operating point instead of assuming one.

        Alternates dark and white references, servoing offset then gain, until
        both readings sit on target. Exposure is left at the value the caller
        programmed - it is per channel and interacts with gain, so it is tuned
        separately once this converges.
        """
        import numpy as np

        gain = {i: 1 for i in range(2)}
        offset = {i: 128 for i in range(2)}
        exposure = {i: (0x0725, 0x0b7d, 0x0a16) for i in range(2)}

        for p in range(passes):
            self.write_afe(gain, offset, exposure)

            dark = self.calibration_scan(self.WIN_DARK, dpi=dpi, sides=sides)
            a = np.frombuffer(dark, dtype=np.uint8)
            # A plain min() over the whole strip latches onto one dark outlier
            # and never moves. Use a low percentile so a few dead cells cannot
            # pin the result.
            min12 = int(round(np.percentile(a, 5))) * 16 if a.size else 0

            for i in range(sides):
                offset[i] = imaging.offset_step(min12, gain[i], offset[i])

            self.write_afe(gain, offset, exposure)

            white = self.calibration_scan(self.WIN_WHITE, dpi=dpi, sides=sides)
            b = np.frombuffer(white, dtype=np.uint8)
            max12 = int(round(np.percentile(b, 95))) * 16 if b.size else 0

            for i in range(sides):
                gain[i] = imaging.gain_step(max12, gain[i])

            if verbose:
                print('    pass %d: dark %4d/%4d  white %4d/%4d  offset %3d  gain %3d'
                      % (p, min12, self.DARK_TARGET_12, max12,
                         self.WHITE_TARGET_12, offset[0], gain[0]))

            if abs(min12 - self.DARK_TARGET_12) <= 32 and \
               abs(max12 - self.WHITE_TARGET_12) <= 128:
                break

        # Exposure is per channel, so it is what balances the channels against
        # each other; offset and gain are single values per side and cannot.
        # Servo each channel's exposure toward the same white target.
        for _ in range(4):
            white = self.calibration_scan(self.WIN_WHITE, dpi=dpi, sides=sides)
            b = np.frombuffer(white, dtype=np.uint8)
            line = self.line_bytes(dpi)
            n = b.size // line
            if not n:
                break
            planes = b[:n * line].reshape(n, 3, line // 3)

            worst = 0
            for c in range(3):
                meas = int(round(np.percentile(planes[:, c, :], 95))) * 16
                if meas < 1:
                    meas = 1
                worst = max(worst, abs(meas - self.WHITE_TARGET_12))
                for i in range(2):
                    e = imaging.exposure_step(meas, exposure[i][c])
                    exposure[i] = tuple(e if k == c else exposure[i][k]
                                        for k in range(3))
            self.write_afe(gain, offset, exposure)
            if verbose:
                print('    exposure %s' % (exposure[0],))
            if worst <= 160:
                break

        self.write_afe(gain, offset, exposure)
        return gain, offset, exposure


    # ---- scan mode (opcode 0xd6) -----------------------------------------
    #
    # Page 0x32 is the buffer/source page. Byte 6 bit 1 selects duplex; without
    # it the device refuses a two-window scan and stalls the pipe.

    PAGE_BUFFER = 0x32

    def set_scan_mode(self, page, page_data):
        """SET SCAN MODE. Payload is a 4-byte head length, page code, page
        length, then the page itself."""
        body = bytes([0x13, 0, 0, 0, page, len(page_data)]) + bytes(page_data)
        cdb = bytes([SET_SCAN_MODE, 0x10, 0, 0, len(body)]) + bytes(7)
        self.cmd(cdb, data_out=body)

    PAGE_30 = 0x30
    PAGE_36 = 0x36

    # Page 0x36 carries colour drop-out and its mutually exclusive partner,
    # each as a 0-3 enum, per side. Decoded on the device rather than guessed:
    # values above 3 are refused with ASC 26 (invalid field in parameter list),
    # data[5] with 1 and 2 produced red- and green-selective output, and the
    # device refuses data[5] together with data[7], or data[6] with data[8] -
    # the two features cannot both be on for one side. data[5]/data[6] are
    # front/back of one, data[7]/data[8] front/back of the other.
    #
    # With drop-out active the device sends ONE channel per pixel, not three
    # planes: decoded as planar RGB the picture comes out tinted and a third
    # of its proper height, and decoded as gray it is clean.
    DROPOUT_NONE, DROPOUT_RED, DROPOUT_GREEN, DROPOUT_BLUE = 0, 1, 2, 3

    # Test overrides; None means "all zeros". Page 0x30's fields are not
    # decoded, so it stays zero.
    PAGE_30_DATA = None
    PAGE_36_DATA = None

    def _reduce_mode(self, mode):
        """Drop-out and enhance are reductions to a single channel, so they
        only exist for a gray or binary page; their mode page has to be zeroed
        whenever the output is RGB.

        Asking for colour AND drop-out is a contradiction. Left half-applied it
        is worse than either: the page gets zeroed so the device sends three
        planes, while the decode has already switched to one. So the request is
        resolved here, once, in favour of what was actually asked for.
        """
        if (any(self.dropout) or any(self.enhance)) and mode == 'color':
            return 'gray'
        return mode

    def _out_mode(self, mode):
        """What the STREAM looks like, which is not what the window asks for.

        With drop-out on the device acquires in colour - it has to, or there is
        no colour to drop - but sends one channel per pixel. So the window keeps
        `mode` while the references, the line stride and the decode all switch
        to gray. Forcing the window to gray as well is a contradiction and the
        device rejects SCAN with ASC 26.
        """
        return 'gray' if (any(self.dropout) or any(self.enhance)) else mode

    @staticmethod
    def dropout_page(front=0, back=0, enhance_front=0, enhance_back=0):
        """Build page 0x36 for colour drop-out.

        `front`/`back` are DROPOUT_*; the enhance pair is the exclusive
        partner feature. Setting both for the same side is refused by the
        device, so it is refused here too rather than sent and rejected.
        """
        if (front and enhance_front) or (back and enhance_back):
            raise ValueError('drop-out and enhance cannot both be set for one side')
        for v in (front, back, enhance_front, enhance_back):
            if not 0 <= v <= 3:
                raise ValueError('page 0x36 fields are 0..3')
        page = bytearray(14)
        page[5], page[6] = front, back
        page[7], page[8] = enhance_front, enhance_back
        return page

    # Page 0x32 data[4]:
    #   0x40  continuous / batch feeding
    #   0x20  automatic page-size detection
    #   0x08  accepted, but never seen set and no effect found for it
    FEED_CONTINUOUS = 0x40
    FEED_AUTOSIZE = 0x20

    def _buffer_page(self, on, b4):
        page = bytearray(14)
        page[0] = 0x02 if on else 0x00      # bit 1 = duplex
        page[1] = 0x01
        page[4] = b4
        return page

    def _feed_flags(self, base=None):
        """The page 0x32 feed byte, honouring `autosize`.

        Automatic page-size detection is the caller's choice, so 0x20 goes on
        only when it is asked for. Both bits used to be hardcoded on every
        scan.
        """
        flags = self.FEED_CONTINUOUS if base is None else base
        if self.autosize:
            flags |= self.FEED_AUTOSIZE
        else:
            flags &= ~self.FEED_AUTOSIZE
        return flags

    def prepare(self, dpi=300, duplex=True, full=True, mode='color'):
        """Program the device for a scan, in the order it insists on.

        `full` is the CALIBRATION preparation: windows, the three mode pages,
        the buffer page a second time with 0x40, then the windows again.

        A page scan uses the short form instead - windows, then the buffer page
        with 0x60. Applying the calibration form before a page changes the
        stream the device produces.
        """
        sides = (self.SIDE_FRONT, self.SIDE_BACK) if duplex else (self.SIDE_FRONT,)

        dy = self.scan_dpi_y or dpi
        for side in sides:
            self.set_window(side, dpi_x=dpi, dpi_y=dy, mode=mode)

        self.set_scan_mode(self.PAGE_BUFFER,
                           self._buffer_page(duplex, self._feed_flags(0x60)))
        if not full:
            return

        self.set_scan_mode(self.PAGE_30,
                           bytearray(self.PAGE_30_DATA or bytearray(14)))
        # Page 0x36 is forced to zero for a colour scan: drop-out and colour
        # enhance only mean anything on a gray or binary page. Setting them on
        # an RGB window is what made drop-out scans come out saturated and
        # uncroppable.
        if mode == 'color':
            front = back = efront = eback = 0
        else:
            front, back = self.dropout
            efront, eback = self.enhance
        self.set_scan_mode(self.PAGE_36,
                           bytearray(self.PAGE_36_DATA
                                     or self.dropout_page(
                                         front, back if duplex else 0,
                                         efront, eback if duplex else 0)))
        # 0x40 here, not 0x60: the calibration form of this page is a distinct
        # value and the autosize bit does not belong in it. Folding it in makes
        # the device refuse SCAN with ASC 26.
        self.set_scan_mode(self.PAGE_BUFFER, self._buffer_page(duplex, 0x40))

        for side in sides:
            self.set_window(side, dpi_x=dpi, dpi_y=dy, mode=mode)

    def set_duplex(self, on=True):
        self.set_scan_mode(self.PAGE_BUFFER, self._buffer_page(on, 0x60))


    # ---- duplex ----------------------------------------------------------

    def scan_page(self, dpi=300, duplex=False, chunk=None, cap=64,
                  mode='color', on_chunk=None):
        """Feed one sheet and return its raw bulk stream.

        In duplex the device returns both sides in one stream; separating them
        is the caller's job (see split_duplex).
        """
        sides = (self.SIDE_FRONT, self.SIDE_BACK) if duplex else (self.SIDE_FRONT,)

        # Order matters: windows first, then the scan mode, then start.
        # Setting the mode first stalls the pipe.
        self.prepare(dpi=dpi, duplex=duplex, full=False, mode=mode)
        return self._acquire(sides, chunk=chunk, cap=cap, on_chunk=on_chunk)

    def wait_ready(self, timeout=20.0, interval=0.15):
        """Poll TEST UNIT READY until the device answers.

        After a sheet is ejected the scanner is briefly busy, and starting the
        next scan into that window fails at the USB level rather than with a
        sense code, so every page starts with this poll.
        """
        deadline = time.time() + timeout
        while True:
            try:
                self.test_unit_ready()
                return True
            except ScannerError:
                pass
            except Exception:
                self.clear_halts()
            if time.time() >= deadline:
                return False
            time.sleep(interval)

    def _feed_next(self):
        """Feed the next sheet inside an open scan session.

        Returns False when the tray is empty. An empty tray does not answer
        with a clean sense code - the feed fails at the USB level - so the pipes
        are cleared first and the sense read afterwards to tell "out of paper"
        (ASC 3A, medium not present) from a real fault.
        """
        try:
            self.object_position(1)
            return True
        except ScannerError:
            return 'no documents' not in self.request_sense()['text']
        except Exception:
            try:
                self.clear_halts()
                cond = self.request_sense()['text']
            except Exception:
                return False
            if 'no documents' in cond or 'medium' in cond:
                return False
            raise ScannerError('feed failed: %s' % cond)

    # 2 MiB in one synchronous transfer works at 300 dpi and stalls the
    # endpoint at dpi_y 150, where lines arrive at half the rate: the first
    # read fails outright and the sheet is ejected. A request that large needs
    # its own thread and pacing to be safe. 1 MiB is proven at every resolution
    # we scan.
    READ_CHUNK = 0x100000

    def _feed_and_read(self, chunk=None, cap=64, on_chunk=None):
        """Feed one sheet inside an already-started scan session and read it.

        Draining is conditional on purpose. Issuing a READ when the scan has
        already reported end-of-data halts the bulk IN endpoint, and a halted
        endpoint is what looks like a wedged scanner - it survives closing the
        process and a USB reset. Only drain when the read loop was cut short.
        """
        chunk = chunk or self.READ_CHUNK
        raw = b''
        finished = False
        try:
            try:
                self.object_position(1)
            except ScannerError:
                finished = True
                raise ScannerError('feed failed: %s' % self.request_sense()['text'])

            empty = 0
            for _ in range(cap):
                try:
                    d = self.read_data(chunk)
                except PageComplete as e:
                    raw += e.data                # the sheet is genuinely over
                    finished = True
                    break
                except NoMedium:
                    finished = True
                    break
                except ShortRead as e:
                    # Less than asked for, which is normal: the paper is still
                    # moving and the buffer had not caught up. Keep what came
                    # and ask again. Stopping here is what cut long sheets off.
                    d = e.data
                    if not d:
                        empty += 1
                        if empty >= 8:
                            finished = True
                            break
                        self.wait_ready(timeout=2.0)
                        continue
                    empty = 0
                except ScannerError:
                    # The tray running dry surfaces two ways: as a clean status
                    # with ASC 3A, and as a stalled data phase whose sense says
                    # the same thing. Both mean the sheet has gone through and
                    # the page is complete, not that the scan failed. This
                    # clause must stay BELOW the three specific ones: they are
                    # all ScannerError subclasses and would be swallowed here.
                    if (self.last_sense or (0, 0, 0))[1] == 0x3a:
                        finished = True
                        break
                    raise
                else:
                    empty = 0
                    if not d:
                        finished = True
                        break
                raw += d
                if on_chunk is not None:
                    # Best-effort: a preview must never be able to break a scan.
                    try:
                        on_chunk(raw)
                    except Exception:
                        pass
        finally:
            if not finished:
                try:
                    self.clear_halts()
                except Exception:
                    pass
                self.drain()
        return raw

    def _acquire(self, sides, chunk=None, cap=64, on_chunk=None):
        """Start a scan on already-prepared windows, feed a sheet and read it.

        Kept separate from scan_page because a batch must prepare once: the
        device rejects a re-prepare after a completed scan.
        """
        self.wait_ready()
        self.scan_start(sides)
        self.request_sense()
        return self._feed_and_read(chunk=chunk, cap=cap, on_chunk=on_chunk)


    @staticmethod
    def mirror_line(a, mode='color'):
        """Reverse each line left-to-right, respecting the pixel layout."""
        import numpy as np
        if mode == 'color':
            w = a.shape[1] // 3
            return a.reshape(-1, 3, w)[:, :, ::-1].reshape(a.shape)
        return a[:, ::-1]

    @staticmethod
    def split_duplex(raw, dpi=300, mirror_back=True, mode='color'):
        """Separate the two sides of a duplex stream.

        The sides alternate BYTE by byte, not line by line: every even byte
        belongs to the front, every odd byte to the back. Splitting at line
        granularity produces two images that each contain half of both sides.

        The back sensor images the sheet from the opposite side, so its lines
        arrive left-right reversed. Mirroring is per colour plane, since the
        line is planar R|G|B rather than pixel-interleaved.
        """
        import numpy as np

        a = np.frombuffer(raw, dtype=np.uint8)
        if a.size % 2:
            a = a[:-1]
        front, back = a[0::2], a[1::2]

        line = P208.line_bytes(dpi, mode=mode)
        w = line // 3
        out = []
        for half in (front, back):
            n = half.size // line
            out.append(half[:n * line].reshape(n, line))

        if mirror_back:
            out[1] = P208.mirror_line(out[1], mode=mode)

        return out[0], out[1]


    # ---- status and paper handling ---------------------------------------

    SENSE_KEYS = {0x0: 'no sense', 0x1: 'recovered', 0x2: 'not ready',
                  0x3: 'medium error', 0x4: 'hardware error', 0x5: 'illegal request',
                  0x6: 'unit attention', 0xb: 'aborted'}

    # (sense key, ASC, ASCQ) -> what it means on this scanner
    CONDITIONS = {
        # 0x80/0x01 is what this unit reports with the cover open: measured
        # by opening it, watching a READ that had just worked start failing,
        # and watching it work again on closing. It was labelled 'paper jam'
        # here before, which was wrong. The code for an actual jam is unknown -
        # inducing one to find out has not seemed worth it.
        (0x3, 0x80, 0x01): 'cover open',
        (0x3, 0x80, 0x02): 'cover open or jam (unverified)',
        (0x3, 0x80, 0x03): 'no documents in the feeder',
        (0x3, 0x80, 0x04): 'double feed detected',
        (0x3, 0x80, 0x07): 'double feed detected',
        (0x2, 0x00, 0x00): 'not ready',
        # ASC 3A is MEDIUM NOT PRESENT: the feeder has run out, which is how a
        # batch ends rather than an error.
        (0x2, 0x3a, 0x00): 'no documents in the feeder',
        (0x0, 0x3a, 0x00): 'no documents in the feeder',
        # Seen at the end of a batch when a feed was attempted on an empty
        # tray: the position succeeds and the following READ fails with this.
        (0x5, 0x2c, 0x00): 'command sequence error (usually an empty feeder)',
    }

    def _clear_condition(self):
        """Read and decode the pending sense, clearing the stall it causes.

        Re-entrant safe: REQUEST SENSE itself must never recurse into this.
        """
        if getattr(self, '_in_sense', False):
            return ''
        self._in_sense = True
        try:
            # 14 bytes is enough to reach the residue. Byte 2 carries the
            # key in its low nibble, ILI in bit 5 and EOM in bit 6; bytes 3..6
            # are the INFORMATION field, which holds the residue of a short
            # transfer when ILI is set.
            cdb = bytes([REQUEST_SENSE, 0, 0, 0, 0x0e]) + bytes(7)
            self.dev.write(self.ep_out, self._wrap(cdb), TIMEOUT_MS)
            d = bytes(self.dev.read(self.ep_in, 0x0e, TIMEOUT_MS))
            self._status()
            if len(d) < 14:
                return ''
            key, asc, ascq = d[2] & 0x0f, d[12], d[13]
            self.last_sense = (key, asc, ascq)
            self.last_ili = bool(d[2] & 0x20)
            self.last_info = int.from_bytes(d[3:7], 'big') if self.last_ili else 0
            # ASC 3A is MEDIUM NOT PRESENT - the tray is empty. This unit
            # reports it under more than one sense key, so match on the ASC
            # alone rather than the full triple.
            if asc == 0x3a:
                return 'no documents in the feeder'
            return self.CONDITIONS.get((key, asc, ascq),
                   self.SENSE_KEYS.get(key, '?') + ' asc %02x/%02x' % (asc, ascq))
        except Exception:
            return ''
        finally:
            self._in_sense = False

    def reserve_unit(self):
        """Hold the device for this session; released when the session ends."""
        self.cmd(bytes([RESERVE_UNIT]) + bytes(11))

    def release_unit(self):
        try:
            self.cmd(bytes([RELEASE_UNIT]) + bytes(11))
        except Exception:
            pass

    def request_sense(self):
        """Ask why the last command failed. Returns a dict, never raises."""
        try:
            cdb = bytes([REQUEST_SENSE, 0, 0, 0, 0x12]) + bytes(7)
            d = self.cmd(cdb, read_len=0x12)
        except Exception:
            return {'key': None, 'text': 'sense unavailable'}
        if len(d) < 14:
            return {'key': None, 'text': 'short sense'}
        key, asc, ascq = d[2] & 0x0f, d[12], d[13]
        # ASC 3A is MEDIUM NOT PRESENT - an empty tray. This unit reports it
        # under more than one sense key, so match on the ASC alone.
        text = ('no documents in the feeder' if asc == 0x3a
                else self.CONDITIONS.get((key, asc, ascq),
                     '%s (asc %02x/%02x)' % (self.SENSE_KEYS.get(key, '?'), asc, ascq)))
        return {'key': key, 'asc': asc, 'ascq': ascq, 'text': text}

    # TEST UNIT READY succeeds with an empty feeder, so it cannot be used to
    # tell whether a sheet is loaded. READ(10) data type 0x8b can - bit 0 is
    # set when paper is in the tray - but ONLY outside a scan session. Once
    # SCAN has opened one, that READ is a sequence violation and the device
    # answers ASC 2C, which is exactly the error it was meant to avoid. So the
    # batch loop cannot poll it; it feeds and interprets the failure instead.


    # ---- high level ------------------------------------------------------

    def references_for(self, dpi=300, duplex=False, mode='color',
                       light_curve=True, curve_normalize=False,
                       out_mode=None):
        """Dark and white references, per side.

        In duplex the reference strips arrive byte-interleaved exactly like a
        page, so they are split the same way. The back is NOT mirrored here:
        shading is applied to the line as it arrives, before mirroring.
        """
        import numpy as np

        sides = (self.SIDE_FRONT, self.SIDE_BACK) if duplex else (self.SIDE_FRONT,)
        refs = {}

        # Prepare ONCE: windows and mode pages a single time, then alternate
        # AFE writes and SCANs. Re-preparing between scans makes the device
        # reject the next SET WINDOW.
        self.prepare(dpi=dpi, duplex=duplex, mode=mode)
        out_mode = out_mode or mode

        for kind, window in (('dark', self.WIN_DARK), ('white', self.WIN_WHITE)):
            raw = self.calibration_scan(window, dpi=dpi, sides=len(sides),
                                        mode=out_mode)

            if duplex:
                f, b = self.split_duplex(raw, dpi=dpi, mirror_back=False,
                                         mode=out_mode)
                halves = (f, b)
            else:
                line = self.line_bytes(dpi, mode=out_mode)
                a = np.frombuffer(raw, dtype=np.uint8)
                n = a.size // line
                halves = (a[:n * line].reshape(n, line),)

            for side, h in zip(sides, halves):
                refs[(kind, side)] = h.mean(axis=0)

        # Fold in the unit's factory curve. On by default, because without it
        # plain white paper saturates: measured on a white A4, 98.7% of the
        # sheet clipped in at least one channel with the curve off and 0.0%
        # with it on. The table says true white is about 1.3x the internal
        # strip, and that factor is the headroom - the strip is not as bright
        # as paper, so shading against it alone puts paper past full scale.
        if light_curve:
            for side in sides:
                got = self.light_curve_for(dpi=dpi, mode=out_mode, side=side)
                if got is None:
                    continue
                curve, scale = got
                refs[('white', side)] = self.apply_light_curve(
                    refs[('dark', side)], refs[('white', side)], curve, scale,
                    normalize=curve_normalize)

        return refs


    def scan_batch(self, dpi=300, duplex=False, calibrate=True,
                   max_pages=100, mode='color', light_curve=True,
                   curve_normalize=False, dropout=(0, 0), on_preview=None,
                   autosize=True, enhance=(0, 0)):
        """Feed sheets until the tray empties, yielding one list of images per
        sheet (front first).

        Calibration runs once for the whole batch, not per sheet - the operating
        point does not change between pages, and re-deriving it would add several
        seconds to every sheet.
        """
        import numpy as np

        self.dropout = tuple(dropout)
        self.autosize = bool(autosize)
        self.enhance = tuple(enhance)
        mode = self._reduce_mode(mode)
        out_mode = self._out_mode(mode)
        want = dpi
        dpi, dy = self.SCAN_PLAN.get(dpi, (dpi, dpi))
        self.scan_dpi_y = dy
        fx, fy = want / float(dpi), want / float(dy)
        self.mode_select_units()

        if calibrate:
            # Drop-out off while the operating point is derived - see scan().
            held, self.dropout = self.dropout, (0, 0)
            try:
                self.prepare(dpi=dpi, duplex=False)
                self.calibrate_afe(dpi=dpi, sides=1)
            finally:
                # Restore even when calibration throws, or drop-out stays off
                # for the rest of the session with nothing to show why.
                self.dropout = held

        refs = self.references_for(dpi=dpi, duplex=duplex, mode=mode,
                                   light_curve=light_curve,
                                   curve_normalize=curve_normalize,
                                   out_mode=out_mode)

        sides = (self.SIDE_FRONT, self.SIDE_BACK) if duplex else (self.SIDE_FRONT,)
        # One SCAN opens a session for the whole stack; sheets are separated
        # inside it by OBJECT POSITION. The device rejects a second SCAN, and
        # rejects SET WINDOW once a session is open, so both happen once here.
        self.prepare(dpi=dpi, duplex=duplex, full=False, mode=mode)
        self.wait_ready()
        self.scan_start(sides)
        self.request_sense()

        peek = self._previewer(on_preview, refs, dpi, duplex, out_mode)

        for page in range(max_pages):
            if not self._feed_next():
                return

            raw = b''
            retried = False
            empty = 0
            while True:
                try:
                    d = self.read_data(self.READ_CHUNK)
                except PageComplete as e:
                    raw += e.data                  # end of this sheet
                    break
                except ShortRead as e:
                    d = e.data                     # not the end - keep reading
                    if not d:
                        empty += 1
                        if empty >= 8:
                            break
                        self.wait_ready(timeout=2.0)
                        continue
                    empty = 0
                    raw += d
                    if peek is not None:
                        try:
                            peek(raw)
                        except Exception:
                            pass
                    continue
                except ScannerError as e:
                    if '0x00000002' in str(e):     # end of this sheet
                        break
                    # End of the stack. ASC 3A is medium-not-present; ASC 2C,
                    # a command sequence error, is what arrives when the tray
                    # is empty but OBJECT POSITION reported success anyway, so
                    # the failure surfaces here on the following READ instead.
                    asc = (self.last_sense or (0, 0, 0))[1]
                    if asc in (0x3a, 0x2c):
                        if raw:
                            break
                        # A sheet WAS fed to get here, so an immediate 2C on an
                        # empty read can also just mean the page has not
                        # reached the sensor yet. Giving up on the first one
                        # ends the batch while the device carries on feeding,
                        # and the remaining sheets come out of the scanner with
                        # no image to show for them. Let it settle and ask
                        # once more before concluding the stack is done.
                        if asc == 0x2c and not retried:
                            retried = True
                            # Settling is best-effort. This runs while an
                            # exception is already being handled, so letting
                            # wait_ready raise here would replace the real
                            # condition with a second one and abort the batch -
                            # the very thing the retry exists to prevent.
                            try:
                                self.wait_ready()
                            except Exception:
                                pass
                            continue
                        return
                    raise
                if not d:
                    break
                raw += d
                if peek is not None:
                    try:
                        peek(raw)
                    except Exception:
                        pass

            if not raw:
                return
            pages = self._decode(raw, refs, dpi=dpi, duplex=duplex,
                                 mode=out_mode)
            yield [imaging.rescale_xy(p, fx, fy) for p in pages]

    def _previewer(self, on_preview, refs, dpi, duplex, mode):
        """Wrap a caller's preview callback so it receives pictures, not bytes.

        Returns None when nobody is watching, so the read loop does no work at
        all in the common case.
        """
        if on_preview is None:
            return None

        def feed(raw):
            img = self.preview_from_raw(raw, refs, dpi=dpi, duplex=duplex,
                                        mode=mode)
            if img is not None:
                on_preview(img)
        return feed

    def preview_from_raw(self, raw, refs, dpi=300, duplex=False, mode='color',
                         step=6):
        """A rough, cheap picture of a page that is still arriving.

        Deliberately not `_decode`: that one is full resolution, and calling it
        on every chunk would redo all the work already done for a page that
        keeps growing - quadratic, on tens of megabytes. This takes every
        `step`-th line and every `step`-th pixel first, so the cost stays flat
        and small however long the page turns out to be.

        Front side only, no mirroring, no cropping. It exists to show that the
        sheet is moving, not to be looked at closely.
        """
        import numpy as np

        a = np.frombuffer(raw, dtype=np.uint8)
        if duplex:
            if a.size % 2:
                a = a[:-1]
            a = a[0::2]
        line = self.line_bytes(dpi, mode=mode)
        n = a.size // line
        if n < step * 8:
            return None

        rows = a[:n * line].reshape(n, line)[::step]
        dark = np.asarray(refs[('dark', self.SIDE_FRONT)], dtype=np.float32)
        white = np.asarray(refs[('white', self.SIDE_FRONT)], dtype=np.float32)
        span = np.maximum(white - dark, 1.0)
        img = np.clip((rows.astype(np.float32) - dark) * 230.0 / span,
                      0, 255).astype(np.uint8)

        if mode == 'color':
            w = line // 3
            img = np.stack([img[:, :w], img[:, w:2 * w], img[:, 2 * w:3 * w]],
                           axis=2)
            return np.ascontiguousarray(img[:, ::step, :])
        return np.ascontiguousarray(img[:, ::step])

    def _decode(self, raw, refs, dpi=300, duplex=False, mode='color'):
        """Split, shade, mirror and de-planarise one sheet's raw stream.

        Returns one array per side: (h, w, 3) for colour, (h, w) for gray,
        (h, w) bool for lineart. Cropping and any other cosmetic work belongs
        to imaging.py, not here.
        """
        import numpy as np

        line = self.line_bytes(dpi, mode=mode)
        if duplex:
            halves = list(self.split_duplex(raw, dpi=dpi, mirror_back=False,
                                            mode=mode))
            sides = (self.SIDE_FRONT, self.SIDE_BACK)
        else:
            a = np.frombuffer(raw, dtype=np.uint8)
            n = a.size // line
            halves = [a[:n * line].reshape(n, line)]
            sides = (self.SIDE_FRONT,)

        w = self.pixels(dpi)
        images = []
        for side, half in zip(sides, halves):
            arr = self.apply_shading(half.tobytes(), refs[('dark', side)],
                                     refs[('white', side)], dpi=dpi)

            if side == self.SIDE_BACK:
                arr = self.mirror_line(arr, mode=mode)

            if mode == 'color':
                img = np.dstack([arr[:, 0:w], arr[:, w:2 * w], arr[:, 2 * w:3 * w]])
            else:
                img = arr[:, :w]
            images.append(img)
        return images

    def scan(self, dpi=300, duplex=False, calibrate=True, mode='color',
             light_curve=True, curve_normalize=False, dropout=(0, 0),
             on_preview=None, autosize=True, enhance=(0, 0)):
        """Acquire one sheet, corrected, as one array per side.

        Returns a list of (height, width, 3) uint8 arrays - front first.
        """
        import numpy as np

        self.dropout = tuple(dropout)
        self.autosize = bool(autosize)
        self.enhance = tuple(enhance)
        mode = self._reduce_mode(mode)
        out_mode = self._out_mode(mode)
        want = dpi
        dpi, dy = self.SCAN_PLAN.get(dpi, (dpi, dpi))
        self.scan_dpi_y = dy
        fx, fy = want / float(dpi), want / float(dy)
        self.mode_select_units()

        if calibrate:
            # The AFE payload carries both sides in one write, so the operating
            # point is derived from a simplex reference.
            #
            # Derive it with drop-out OFF. calibrate_afe reads the strips as
            # colour, and with drop-out on the device sends one channel, so it
            # would be servoing on a stream it is misreading - which lands on a
            # wrong gain and offset and drives the whole page to 255. The
            # references below are then measured with drop-out on, so they do
            # travel the same path as the page.
            held, self.dropout = self.dropout, (0, 0)
            try:
                self.prepare(dpi=dpi, duplex=False)
                self.calibrate_afe(dpi=dpi, sides=1)
            finally:
                # Restore even when calibration throws, or drop-out stays off
                # for the rest of the session with nothing to show why.
                self.dropout = held

        refs = self.references_for(dpi=dpi, duplex=duplex, mode=mode,
                                   light_curve=light_curve,
                                   curve_normalize=curve_normalize,
                                   out_mode=out_mode)
        raw = self.scan_page(dpi=dpi, duplex=duplex, mode=mode,
                             on_chunk=self._previewer(on_preview, refs, dpi,
                                                      duplex, out_mode))

        pages = self._decode(raw, refs, dpi=dpi, duplex=duplex, mode=out_mode)
        return [imaging.rescale_xy(p, fx, fy) for p in pages]


def main():
    with P208() as s:
        s.test_unit_ready()
        inq = s.inquiry()
        print('  peripheral type : %d %s' % (
            inq['peripheral'], '(scanner)' if inq['peripheral'] == 6 else ''))
        print('  vendor / product: %s / %s' % (inq['vendor'], inq['product']))
        print('  revision        : %s' % inq['revision'])

        s.mode_select_units()
        print('  measurement units set to 1/%d inch' % s.UNITS_PER_INCH)
        for side in (s.SIDE_FRONT, s.SIDE_BACK):
            s.set_window(side)
        print('  windows programmed for both sides')
        w = s.get_window()
        print('  GET WINDOW echo: %s' % ' '.join('%02x' % b for b in w[:16]))

        sen = s.sensors()
        if sen is not None:
            print('  feeder          : %s (sensor byte 0x%02x)'
                  % ('paper loaded' if sen['paper'] else 'empty', sen['raw']))
        c = s.counters()
        if c is not None:
            print('  serial          : %s' % c['serial'])
            print('  counters        : %s' % ', '.join(str(v) for v in c['values'][1:]))


if __name__ == '__main__':
    main()
