#!/usr/bin/env python3
"""CanoScan 8000F native driver (pyusb/libusb) - clean scan pipeline.

Pure Python. No vendor software, no emulator, no replayed capture data: device
init, lamp warm-up, AFE gain/offset calibration, shading, motor tables and the
scan register program are all generated.

Public API:
    open_device()                      -> opens the USB device (call once)
    scan(dpi, mode, depth, progress)   -> (raw_bytes, meta_dict)
    close_device()
mode: 'color' | 'gray' | 'lineart'   depth: 8 | 16  (lineart is always 1-bit)
"""
import os, sys, time, struct
_struct = struct
import usb.core, usb.util
import importlib.util as _ilu

_HERE = os.path.dirname(os.path.abspath(__file__))
_mtspec = _ilu.spec_from_file_location('motor_tables', os.path.join(_HERE, 'motor_tables.py'))
motor_tables = _ilu.module_from_spec(_mtspec); _mtspec.loader.exec_module(motor_tables)

VID, PID = 0x04a9, 0x220f
REQ_REG, REQ_BUF = 0x0c, 0x04
V_SETADDR, V_WRVAL, V_RDREG, V_GPIO, V_BUF = 0x83, 0x85, 0x84, 0x8a, 0x82

GAIN_K = 210.0

# Horizontal CCD-window origin, in pixels at the scan resolution. The decode
# rotates the image 180 deg, so the window is addressed from the right-hand edge:
#
#     final_x_px = _X_ORIGIN(dpi) - feed - width
#
# 2559 px at 300 dpi, measured on hardware and reproducing every high-confidence
# check to within 7 px. It exceeds the 2480 px plate width by the unscanned right
# margin. Scales with the rung, being a pixel count.
def _X_ORIGIN(dpi):
    return int(round(2559 * dpi / 300.0))

TRAVEL_STEPS = 1302   # imaging-move carriage travel, in motor steps
_NATIVE = {'pwm': 800}

# module-level device state (single scanner)
dev = None; ep_in = None; ep_out = None
shadow = [0] * 0x80
ostat = {'w': 0, 'r': 0}
_MASTER_NS = motor_tables.MASTER_NS   # 1179-entry ns ramp (from motor_tables)

# ---- one-process-at-a-time device lock -------------------------------------
# Only one process may drive the ASIC at a time. The eSCL bridge and the CLI are
# both normal ways to use this driver and are easily running at once; without a
# lock each opens the device and they interleave writes into one shared register
# file, corrupting both scans. The failure is ugly too - the second process dies
# inside set_configuration() with "No such device" rather than waiting.
#
# Advisory lock file: the kernel drops it when the fd closes or the process
# dies, so it can never go stale and need manual clearing.
#
# Acquire EXACTLY ONCE per process (open_device -> close_device). Two flock()
# calls from one process use separate open file descriptions and conflict with
# each other, so a second acquire would deadlock against our own.
_LOCK_NAME = 'canoscan-8000f.lock'
_lock_fd = None

class ScannerBusyError(RuntimeError):
    """Another process is already using the scanner."""

def _lock_path():
    base = os.environ.get('XDG_RUNTIME_DIR') or os.environ.get('TMPDIR') or '/tmp'
    return os.path.join(base, _LOCK_NAME)

def _acquire_device_lock(timeout_s=8.0):
    global _lock_fd
    if _lock_fd is not None: return          # already held by this process
    try: import fcntl
    except ImportError: return               # non-POSIX host: degrade to no locking
    fd = os.open(_lock_path(), os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); break
        except OSError:
            if time.monotonic() >= deadline:
                try: holder = open(_lock_path()).read().strip() or '?'
                except Exception: holder = '?'
                os.close(fd)
                raise ScannerBusyError('scanner is in use by pid %s (waited %.0fs)'
                                       % (holder, timeout_s))
            time.sleep(0.1)
    try:                                     # record the holder for the next waiter
        os.ftruncate(fd, 0); os.write(fd, ('%d\n' % os.getpid()).encode())
    except Exception: pass
    _lock_fd = fd

# ---- warm-lamp fast path ----------------------------------------------------
# Measured phase profile of a 300 dpi scan: warm-up was 17.84 s of 35.29 s -
# over half - and it ran in full even when the lamp had been hot seconds
# earlier. Everything that time buys is WAITING: a fixed 4 s settle, up to 5 s
# for the peaks to rise, ~3 s of PWM search, up to 10 s of stability sampling.
#
# None of that waiting is needed on a lamp already at brightness. The CHECKING
# still is, so the fast path below skips only the sleeps and keeps every test,
# adding one: the current peaks must match the peaks a full warm-up last
# settled on at that PWM. That asks "is the lamp in the state we measured
# properly?" instead of inventing absolute brightness thresholds, which would
# be lamp- and age-specific.
#
# The reference is written ONLY by the full path, so it can never drift by
# being re-derived from itself, and any failure falls through to the full
# warm-up. Set WARM_LAMP_FASTPATH = False to disable entirely.
WARM_LAMP_FASTPATH   = True
WARM_LAMP_TTL_S      = 900.0   # lamp output drifts as the tube ages and cools
WARM_LAMP_STEADY_TOL = 3       # two reads must agree within this
WARM_LAMP_READ_GAP_S = 0.5     # ...and be this far apart - see below
WARM_LAMP_PEAK_TOL   = 10      # ...and match the measured reference within this
WARM_LAMP_MAX_PEAK   = 200     # the PWM search's own saturation criterion

def _lamp_state_path():
    base = (os.environ.get('XDG_CACHE_HOME')
            or os.path.join(os.path.expanduser('~'), '.cache'))
    d = os.path.join(base, 'scan8000f')
    try: os.makedirs(d, exist_ok=True)
    except Exception: pass
    return os.path.join(d, 'lamp_state.json')

def _lamp_state_save(pwm, peaks):
    """Record what a FULL warm-up settled on. Never raises."""
    try:
        import json
        p = _lamp_state_path()
        with open(p + '.tmp', 'w') as f:
            json.dump({'v': 1, 'pwm': int(pwm),
                       'peaks': [int(v) for v in peaks],
                       'at': time.time()}, f)
        os.replace(p + '.tmp', p)
    except Exception:
        pass

def _lamp_state_load():
    """Return {'pwm','peaks','age'} or None. Never raises."""
    try:
        import json
        with open(_lamp_state_path()) as f:
            m = json.load(f)
        if m.get('v') != 1: return None
        peaks = m.get('peaks') or []
        if len(peaks) != 3: return None
        age = time.time() - float(m.get('at', 0))
        if age > WARM_LAMP_TTL_S: return None
        return {'pwm': int(m['pwm']), 'peaks': [int(v) for v in peaks], 'age': age}
    except Exception:
        return None

def _release_device_lock():
    global _lock_fd
    if _lock_fd is None: return
    try:
        import fcntl
        os.ftruncate(_lock_fd, 0); fcntl.flock(_lock_fd, fcntl.LOCK_UN)
    except Exception: pass
    try: os.close(_lock_fd)
    except Exception: pass
    _lock_fd = None

# ---- diagnostics -----------------------------------------------------------
# USB-transfer trace: when _XLOG is a list, every control/bulk transfer the
# driver actually makes is appended (register program + motor-table bytes), so
# a real scan can be diffed against a reference transfer log. scan() turns it
# on and dumps it to <scan8000f>/last_scan_trace.txt at the end of the run.
_XLOG = None
def _xlog(s):
    if _XLOG is not None:
        _XLOG.append(s)


def log(*a):
    msg = ' '.join(str(x) for x in a)
    if _PROGRESS: _PROGRESS(msg)
    else: print(msg)
_PROGRESS = None
def set_progress(cb): 
    global _PROGRESS; _PROGRESS = cb

def open_device(backend_hint=True):
    """Open the CanoScan 8000F. Raises RuntimeError if not found.

    Takes the one-process device lock before any USB traffic and holds it until
    close_device(). It has to be acquired here rather than around scan(),
    because opening already claims the device - locking later means a second
    process collides during set_configuration() instead of waiting its turn.
    Raises ScannerBusyError, naming the holding pid, if someone else has it."""
    global dev, ep_in, ep_out, shadow, ostat
    _acquire_device_lock()
    try:
        return _open_device(backend_hint)
    except Exception:
        _release_device_lock()   # never strand the lock on a failed open
        raise

def _open_device(backend_hint=True):
    global dev, ep_in, ep_out, shadow, ostat
    backend = None
    if backend_hint:
        try:
            import libusb_package; backend = libusb_package.get_libusb1_backend()
        except Exception: backend = None
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=backend)
    if dev is None:
        raise RuntimeError('CanoScan 8000F (04a9:220f) not found. Is it powered on and connected?')
    try:
        if dev.is_kernel_driver_active(0): dev.detach_kernel_driver(0)
    except Exception: pass
    dev.set_configuration()
    cfg = dev.get_active_configuration(); intf = cfg[(0, 0)]
    ep_in = usb.util.find_descriptor(intf, custom_match=lambda e:
        usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN and
        usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK)
    ep_out = usb.util.find_descriptor(intf, custom_match=lambda e:
        usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT and
        usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK)
    shadow = [0] * 0x80; ostat = {'w': 0, 'r': 0}
    _flush_pipes()
    return dict(ep_in=ep_in.bEndpointAddress if ep_in else 0,
                ep_out=ep_out.bEndpointAddress if ep_out else 0)

def _flush_pipes():
    """Clear a stale state left by a previous scan: reset endpoint halts and drain any
    bytes still queued on the bulk-IN pipe. This is what makes a 2nd scan reliable -
    otherwise leftover data corrupts the next calibration read."""
    for ep in (ep_in, ep_out):
        try:
            if ep is not None: dev.clear_halt(ep.bEndpointAddress)
        except Exception:
            pass
    if ep_in is None:
        return
    # drain: read until the pipe is empty (short timeout), bounded so we never hang
    for _ in range(64):
        try:
            c = dev.read(ep_in.bEndpointAddress, 0xf000, timeout=120)
        except Exception:
            break
        if not c:
            break

def close_device():
    global dev
    try:
        if dev is not None: usb.util.dispose_resources(dev)
    except Exception: pass
    dev = None
    _release_device_lock()      # session over: let the next process in

_MODEMAP = {'color': 2, 'colour': 2, 'gray': 1, 'grey': 1, 'lineart': 0, 'bw': 0}

def scan(dpi=300, mode='color', depth=8, progress=None, preview=None, trace=False,
         region=None):
    """Run a full native scan. Returns (raw_bytes, meta). mode: color|gray|lineart.
    trace=True writes every USB transfer of this scan to <scan8000f>/last_scan_trace.txt
    (diagnostic; off by default).
    region=(x0,y0,x1,y1) in normalised [0,1] bed coordinates returns just that rectangle.
    X is windowed in HARDWARE (only the selected columns are digitised or transferred);
    Y uses a reduced line count, so the carriage scans from its start edge up THROUGH the
    selection and the leading rows are cropped in software. Measured on a 30%-wide
    selection: 84% fewer bytes and 25% less time than a full pass. Selections toward the
    document bottom (scanned first) save the most in Y; one touching the very top still
    travels the full bed, though it still saves the full X window. None = full bed
    (unchanged, and the hardware window is not used)."""
    global _PROGRESS, _XLOG
    if progress is not None: _PROGRESS = progress
    _XLOG = [] if trace else None   # capture the real USB transfer trace only when asked
    m = _MODEMAP.get(str(mode).lower(), 2)
    lineart = (m == 0); color = (m == 2)
    if lineart: depth = 1
    requested_dpi = dpi
    # Native hardware resolutions are 75/150/300/600/1200 — the motor needs a
    # slope + home-decel table pair, and only these rungs have one.
    # The other advertised resolutions (100/200/400/800) are not driven on the
    # hardware; the next native rung up is scanned and resampled in software. Each is
    # 2/3 of a native rung: 100=⅔·150, 200=⅔·300, 400=⅔·600, 800=⅔·1200. We do the
    # same — drive the hardware natively, then LANCZOS-resample to the requested
    # size on export. (Driving the motor at a non-native rate has no tail table and
    # would over-run the carriage, the same failure mode as the old native-150 bug.)
    # Any resolution that is not a native rung is served by scanning at the next
    # rung UP and resampling down, which generalises the old
    # {100:150, 200:300, 400:600, 800:1200} map and reproduces it exactly. It
    # also stops an undrivable value reaching the hardware: 900 dpi used to fall
    # through to `600 // ydpi` == 0 and raise ZeroDivisionError, and 250 dpi
    # silently produced wrong geometry. That matters because escl_bridge takes
    # the resolution straight from a network client, so these are reachable from
    # outside the CLI's own choices.
    _NATIVE_RUNGS = (75, 150, 300, 600, 1200)
    try:
        dpi = int(dpi)
    except (TypeError, ValueError):
        raise ValueError('resolution must be an integer, got %r' % (requested_dpi,))
    if dpi < 1:
        raise ValueError('resolution must be positive, got %d' % dpi)
    if dpi not in _NATIVE_RUNGS:
        _higher = [r for r in _NATIVE_RUNGS if r > dpi]
        if not _higher:
            raise ValueError('resolution %d is above the highest drivable rung %d'
                             % (dpi, _NATIVE_RUNGS[-1]))
        dpi = _higher[0]                   # program the hardware at the native rung
    ratio = requested_dpi / float(dpi)     # 1.0 native, <1 for the resampled rungs
    downscale = 1
    full_w = int(round(620 * dpi / 75.0))  # full-bed native dimensions
    full_l = int(round(876 * dpi / 75.0))
    # Region scan. The carriage scans from raw row 0 (= the document BOTTOM in the final
    # image; the decode rotates 180° and the preview fills bottom-up) upward. In Y we use
    # the LINE-COUNT mechanism (the carriage start itself is not repositioned) to capture
    # just raw rows
    # [0 .. selection top + margin], i.e. from the scanned edge THROUGH the selection.
    # That genuinely shortens the pass (real time/data saving; biggest for selections
    # toward the document bottom, which are scanned first). The leading rows below the
    # selection, and the X range, are then cropped losslessly in software. The MARGIN
    # (native rows) covers the decode's channel-realignment trim so the selection top is
    # never eaten. A full-bed scan (region=None) is completely unaffected.
    _MARGIN = 64
    lines = full_l
    width = full_w
    xfeed = None                       # None = the driver's own baseline X start
    crop_x0, crop_x1 = 0, full_w
    crop_y0, crop_y1 = 0, full_l
    if region:
        x0, y0, x1, y1 = region
        x0, x1 = sorted((min(max(x0, 0.0), 1.0), min(max(x1, 0.0), 1.0)))
        y0, y1 = sorted((min(max(y0, 0.0), 1.0), min(max(y1, 0.0), 1.0)))
        crop_x0 = int(round(x0 * full_w)); crop_x1 = max(crop_x0 + 1, int(round(x1 * full_w)))
        fy0 = int(round(y0 * full_l)); fy1 = int(round(y1 * full_l))
        region_h = max(1, fy1 - fy0)
        lines = min(full_l, full_l - fy0 + _MARGIN)     # capture edge -> selection top
        crop_y0 = max(0, lines - full_l + fy0)          # selection top in the captured frame
        crop_y1 = crop_y0 + region_h
        # X is windowed in HARDWARE. reg 0x10/0x11 is the horizontal start of the
        # CCD window (1 unit = 1 pixel at the scan resolution, measured exact over
        # a 600-unit sweep), so only the selected columns are ever digitised or
        # transferred. That is a straight saving in USB bytes and decode time,
        # proportional to the width dropped, and it involves no motor movement.
        #
        # The decode rotates 180 deg, so X is mirrored: a LARGER feed moves content
        # left. Hence feed counts back from the right-hand origin.
        xwidth = crop_x1 - crop_x0
        xfeed = _X_ORIGIN(dpi) - xwidth - crop_x0
        if xfeed >= 0:
            width = xwidth
            crop_x0, crop_x1 = 0, xwidth   # already windowed; don't crop X again
        else:
            xfeed = None                   # selection runs past the window origin
    crop_w = crop_x1 - crop_x0
    crop_h = crop_y1 - crop_y0
    # Export target size AFTER the software crop: resample by `ratio` for the non-native
    # rungs; a native full-bed scan needs neither (out_* stay None).
    if region or ratio != 1.0:
        out_width = int(round(crop_w * ratio))
        out_lines = int(round(crop_h * ratio))
    else:
        out_width = out_lines = None
    expo  = 0x5400 if dpi >= 1200 else 0x2a00   # CCD line-period exposure (reg09/0a/0b/0c)
    # ---- MOTOR slope-table parameters -------------------------------------------------
    # res-class-0 / native 1200: motor exposure 0x2a00, travel 1302, tail gain 0.5.
    # The key one is the exposure: the motor is driven from the BASE exposure 0x2a00, NOT
    # the CCD's doubled 0x5400. That sets the cruise-period word to 0x8000fc00 (64512)
    # instead of 0x8001f800 (129024) - our value was exactly 2x too large, so the motor
    # stepped half as often and the carriage reached only ~half the plate (image squeezed
    # into the top half, ~2x vertical stretch). res-class-1 (<=600) uses the CCD
    # exposure directly, with gain 1.0.
    if dpi >= 1200:
        motor_expo, travel, motor_gain = 0x2a00, TRAVEL_STEPS, 0.5
    else:
        motor_expo, travel, motor_gain = expo, TRAVEL_STEPS, 1.0
    if lineart:   stride = (width + 7) // 8
    elif color:   stride = width * 3 * (2 if depth == 16 else 1)
    else:         stride = width * (2 if depth == 16 else 1)
    target = lines * stride
    if out_width:
        log('scan %d dpi (native %d + resample)  %s  %d-bit  ->  %dx%d px out  (%d bytes captured)'
            % (requested_dpi, dpi, mode, depth, out_width, out_lines, target))
    else:
        log('scan %d dpi  %s  %d-bit  ->  %dx%d  (%d bytes)' % (dpi, mode, depth, width, lines, target))

    try:
        native_init()
        _NATIVE['pwm'] = native_warmup()
        native_calibrate(dpi)      # dpi = native rung; picks res-class-0 (10600px) shading at 1200
    except Exception:
        _safe_quiesce()            # a failed warm-up/calibration must not leave the lamp on
        raise
    lamp_on(0x320, _NATIVE['pwm'])
    wbit(0x01, 5, 1, 1)
    wr(0x08, 0x01)
    # reg09/0a and reg0b/0c = CCD line-period, and they scale with the exposure:
    # expo>>4  (0x2a00>>4 = 0x2a0 for <=600 dpi; 0x5400>>4 = 0x540 at 1200). It must
    # track the exposure: a fixed 0x2a0 leaves the line period at half at 1200, so the
    # CCD clocks lines twice as fast as the carriage advances - the motor covers only
    # ~40% of the bed by the time the line count is reached and the rest of the frame
    # comes out blank/stretched.
    _lp = expo >> 4
    w16r(0x09, 0x0a, _lp); w16r(0x0b, 0x0c, _lp)
    for r in (0x70, 0x71, 0x72, 0x73, 0x74, 0x75): wr(r, 0)
    wbit(0x01, 2, 1, 1)
    if dpi >= 1200:
        wbit(0x05, 2, 1, 0); res_class(0)
    else:
        wbit(0x05, 2, 1, 1); res_class(1)
    wbit(0x06, 0, 1, 0); wbit(0x03, 2, 1, 1)
    wbit(0x20, 4, 2, 1); wbit(0x06, 6, 1, 0); wbit(0x06, 3, 1, 0)
    nothome(1)
    for _ in range(100):
        if move_done(): break
        time.sleep(0.05)
    wbit(0x20, 0, 4, 0)
    emit_motor_tables(dpi, exposure=motor_expo, travel=travel, chan_gain=motor_gain)
    feed = emit_scan_program(dpi, dpi, width, lines, m, depth=depth, xfeed=xfeed)
    log('scanning... (streaming %d bytes)' % target)
    pmeta = dict(width=width, channels=3 if color else 1, depth=depth,
                 lineart=1 if lineart else 0, total_lines=lines)
    image = bytearray(); t0 = time.monotonic(); wd = max(150.0, target / 2.0e5)
    last_prev = t0
    while len(image) < target and time.monotonic() - t0 < wd:
        chunk = _patient_bulk_in(min(0x10000, target - len(image)), quiet_s=1.2)
        if not chunk:
            if time.monotonic() - t0 > 3: break
            continue
        image += bytes(chunk)
        if progress: progress('  %d%% (%d / %d)' % (100 * len(image) // target, len(image), target))
        # live preview: throttled so we never stall the read pipe for long
        if preview and time.monotonic() - last_prev > 2.0 and len(image) > stride * 8:
            try: preview(image, pmeta)
            except Exception: pass
            last_prev = time.monotonic()
    # Stop the carriage BEFORE waiting on move-done. The motor is stepped from
    # the line clock, so polling move-done first leaves the clock running and
    # the carriage travelling for the whole wait - measured at 112 polls, about
    # 5-6 s, on a full-bed 300 dpi scan. Killing the clock and dropping GO first
    # can only ever stop the motor earlier.
    #
    # The read loop above is deliberately untouched: its no-data bail is what
    # bounds carriage travel during the scan itself, and lengthening it is the
    # known cause of past over-runs.
    try:
        wbit(0x2f, 7, 1, 0)     # line clock off -> the motor stops stepping
        wbit(0x02, 1, 1, 0)     # GO off
    except Exception as e:
        log('  warn: could not stop the line clock: %s' % e)
    tq = time.monotonic()
    # Each step is guarded on its own: a read failure while polling must not
    # skip the motor power-off below it.
    try:
        while time.monotonic() - tq < 20.0:
            if move_done(): break
            time.sleep(0.05)
    except ScannerCommError as e:
        log('  warn: move-done poll failed (%s) - powering the motor off anyway' % e)
    try:
        wr(0x02, 0x00)
    except Exception as e:
        log('  warn: motor power-off failed: %s' % e)
    try:
        _reactive_home_quiet()
    except ScannerCommError as e:
        log('  warn: homing aborted: %s' % e)
    teardown()          # clear scan-mode bits so the ASIC idles cleanly (stops the status-LED flicker)
    meta = dict(dpi=requested_dpi, scandpi=dpi, downscale=downscale,
                out_width=out_width, out_lines=out_lines,
                crop_x0=crop_x0, crop_x1=crop_x1, crop_y0=crop_y0, crop_y1=crop_y1,
                width=width, lines=lines, channels=3 if color else 1,
                depth=depth, lineart=1 if lineart else 0, stride=stride, mode=mode)
    log('done: %d bytes' % len(image))
    # optional USB transfer trace (diagnostic; enabled by trace=True)
    if _XLOG is not None:
        try:
            _tp = os.path.join(_HERE, 'last_scan_trace.txt')
            with open(_tp, 'w') as _tf:
                _tf.write('# scan trace: dpi=%d(native %d) mode=%s depth=%d\n'
                          % (requested_dpi, dpi, mode, depth))
                _tf.write('# W=write R=read SEL=commit BO=bulk-out(motor/shading table)\n')
                _tf.write('\n'.join(_XLOG))
                _tf.write('\n')
            log('USB trace written: %s (%d transfers)' % (_tp, len(_XLOG)))
        except Exception as _e:
            log('trace write failed: %s' % _e)
        _XLOG = None
    return bytes(image), meta

def _reactive_home_quiet(timeout_s=25.0):
    # Success is decided by the HOME SENSOR (reg 0x64 bit6), not only by catching
    # the move-done bit go clear-then-set. That transition is a poll race: each
    # loop is ~25-30 ms (two USB round-trips for the read, four for the GO pulse,
    # plus the sleep), so a return move that finishes inside one interval is never
    # observed "moving", `seen` stays False, and the routine spins to the full
    # timeout reporting failure while the carriage is physically home. It shows up
    # intermittently after short/region scans, which end nearer home and so make
    # the return move brief. Observed on hardware; the sensor check removes it.
    wr(0x02, 0x00); wr(0x02, 0x80); wr(0x02, 0xa0)
    t0 = time.monotonic(); homed = False; seen = False
    while time.monotonic() - t0 < timeout_s:
        s = rd(0x03)
        if at_home():                       # authoritative: we are there
            homed = True; break
        if (s & 0x08) == 0: seen = True
        if seen and (s & 0x08): homed = True; break
        wr(0x02, 0xa2); wr(0x02, 0xa0); time.sleep(0.02)
    wr(0x02, 0x00)
    log('home %s (%.1fs)' % ('ok' if homed else 'timeout - power-cycle to re-home', time.monotonic() - t0))
    return homed

# ============================ native pipeline (verified) ============================

def sel(addr):  dev.ctrl_transfer(0x40, REQ_REG, V_SETADDR, 0, bytes([addr]), timeout=2000)

def wr(addr, val):
    val&=0xff; shadow[addr]=val
    sel(addr); dev.ctrl_transfer(0x40, REQ_REG, V_WRVAL, 0, bytes([val]), timeout=2000); ostat['w']+=1
    _xlog('W %02x %02x' % (addr, val))

class ScannerCommError(RuntimeError):
    """A register could not be read: the scanner is unplugged or wedged."""

class ScannerFatalError(RuntimeError):
    """The ASIC reported a hard fault; do not scan on it."""

def rd(addr):
    """Read one ASIC register. Raises ScannerCommError if it cannot be read.

    It must raise rather than return a value, because 0 is legal for every
    register we poll and the two callers invert dangerously: at_home() is
    (rd(0x64) & 0x40) == 0, so a substituted 0 reads as "carriage IS home" -
    the worst thing to be wrong about on a scanner whose motor is stepped from
    the line clock. (move_done() at least fails safe, reporting "still
    moving".) Only reachable after four consecutive failed transfers, i.e. when
    the link is already broken.

    sel() is inside the retry too: it is the same kind of control transfer and
    can fail the same way."""
    for _ in range(4):
        try:
            sel(addr)
            r = dev.ctrl_transfer(0xc0, REQ_REG, V_RDREG, 0, 1, timeout=2000)
        except Exception:
            time.sleep(0.02); continue
        if r is not None and len(r):
            ostat['r'] += 1; _xlog('R %02x %02x' % (addr, bytes(r)[0])); return bytes(r)[0]
        time.sleep(0.02)
    _xlog('R %02x FAILED' % addr)
    raise ScannerCommError('register 0x%02x unreadable after 4 attempts - '
                           'scanner disconnected or unresponsive' % addr)

def wbit(addr, start, width, val):
    mask=((1<<width)-1)<<start
    shadow[addr]=(shadow[addr] & ~mask) | ((val<<start)&mask)
    wr(addr, shadow[addr])

def commit(): sel(0x24); _xlog('SEL 24')

def afe(afe_addr, val):                              # AFE indirect write: 0x25=addr, 0x26=data
    wr(0x25, afe_addr & 0x3f); wr(0x26, val & 0xff)

def motor_run():   wbit(0x02, 4, 2, 3)

def motor_stop():  wbit(0x02, 4, 2, 0)

def nothome(v):    wbit(0x02, 7, 1, v)

def motor_go(v):   wbit(0x02, 1, 1, v)

def motor_rst(v):  wbit(0x02, 0, 1, v)

def set_lincnt10(v): wr(0x10, v&0xff); wr(0x11, (v>>8)&0x7f)

def set_cnt12(v):    wr(0x12, v&0xff); wr(0x13, (v>>8)&0x3f)

def set_1b(v):       wr(0x1b, v&0xff); wr(0x1c, (v>>8)&0xff)

def at_home():   return (rd(0x64) & 0x40) == 0

def move_done(): return (rd(0x03) & 0x08) != 0

def status04():  return rd(0x04)

def _bulk_out(payload):
    # arm a bulk-OUT (dir=1) then write payload on ep_out. Verified struct: [1,0,0x82,0,size32].
    size=len(payload)
    arm=bytes([1,0,0x82,0, size&0xff,(size>>8)&0xff,(size>>16)&0xff,(size>>24)&0xff])
    dev.ctrl_transfer(0x40, REQ_BUF, V_BUF, 0, arm, timeout=2000)
    _xlog('BO %d head=%s tail=%s' % (size, payload[:24].hex(), payload[-8:].hex()))
    off=0; retries=0
    while off<size:
        try:
            n=dev.write(ep_out.bEndpointAddress, payload[off:off+0xf000], timeout=8000)
        except usb.core.USBError:
            retries+=1
            if retries>3: raise
            continue
        if not n: break
        off+=n
    return off

def _bulk_in(size):
    arm=bytes([0,0,0x82,0, size&0xff,(size>>8)&0xff,(size>>16)&0xff,(size>>24)&0xff])
    dev.ctrl_transfer(0x40, REQ_BUF, V_BUF, 0, arm, timeout=2000)
    got=0
    while got<size:
        try: c=dev.read(ep_in.bEndpointAddress, min(size-got,0xf000), timeout=400)
        except usb.core.USBError: break
        if not c: break
        got+=len(c)
    return got

def _patient_bulk_in(size, quiet_s):
    # arm one bulk-IN of `size`, read until we have it OR the pipe stays quiet for quiet_s.
    arm=bytes([0,0,0x82,0, size&0xff,(size>>8)&0xff,(size>>16)&0xff,(size>>24)&0xff])
    try: dev.ctrl_transfer(0x40,REQ_BUF,V_BUF,0,arm,timeout=2000)
    except Exception: return b''
    out=b''; last=time.monotonic()
    while len(out)<size and time.monotonic()-last<quiet_s:
        try: c=dev.read(ep_in.bEndpointAddress, min(size-len(out),0xf000), timeout=800)
        except usb.core.USBError: c=b''
        if c: out+=bytes(c); last=time.monotonic()
    return out

def teardown():
    # End-of-scan quiesce: clear the scan-mode bits so the ASIC goes fully idle and
    # the status LED stops flickering (no power-cycle needed between scans).
    try:
        wbit(0x2f,7,1,0)     # reg0x2f bit7: scan enable off
        wbit(0x02,1,1,0)     # reg0x02 bit1: GO off
        wbit(0x07,4,1,0)     # reg0x07 bit4: Y-step mode off
        wbit(0x03,2,1,0)     # reg0x03 bit2: scan-enable off
        wbit(0x60,0,1,0); wbit(0x60,1,1,0)               # reg0x60 bits0-1: lamp/power rails
        wbit(0x48,1,3,0); wbit(0x48,5,3,0)               # reg0x48 scan-area fields
        wbit(0x49,1,3,0); wbit(0x49,5,3,0)               # reg0x49 scan-area fields
        wbit(0x02,7,1,1)     # reg0x02 bit7: home direction
        wr(0x02,0x00)        # motor power off
    except Exception as e:
        print('  teardown warn:', e)

def _safe_quiesce():
    # Best-effort recovery after a FAILED scan (e.g. lamp warm-up / calibration raised):
    # turn the lamp off and clear scan-mode bits so the device isn't left powered/dirty
    # for the next scan. Never raises. Not on the success path (which calls teardown()).
    try: lamp_off()
    except Exception: pass
    try: teardown()
    except Exception: pass

def w16r(lo,hi,v): wr(lo,v&0xff); wr(hi,(v>>8)&0xff)

def sdram(addr):
    wr(0x21,addr&0xff); wr(0x22,(addr>>8)&0xff); wr(0x23,(addr>>16)&0xff); commit()

def afe_defaults():
    # AFE power-on defaults: offsets at mid-scale, unity gains
    for a,v in ((0x04,0x00),(0x01,0x23),(0x02,0x2c),(0x03,0x1f),
                (0x20,0x80),(0x21,0x80),(0x22,0x80),(0x28,0x4b),(0x29,0x4b),(0x2a,0x4b)):
        afe(a,v)

def res_class(k):
    # AFE reg3 + CCD phase registers, per resolution class
    if k==0:   afe(0x03,0x1f); vals=(0x0c,0x0e,0x10,0x12)
    elif k==1: afe(0x03,0x2f); vals=(0x0c,0x14,0x16,0x00)
    else:      afe(0x03,0x2f); vals=(0x0f,0x01,0x13,0x15)
    for r,v in zip((0x52,0x53,0x54,0x55),vals): wr(r,v)

def identity_matrix():
    for v in (0x2000,0,0,0,0x2000,0,0,0,0x2000):
        wr(0x37,v&0xff); wr(0x38,(v>>8)&0xff)

def lamp_on(pwm_a=0x320,pwm_b=0x320):
    # lamp PWM duty (reg 0x2b/0x2c/0x2d) then enable via reg 0x29 bit4
    wr(0x2b,pwm_a&0xff); wr(0x2c,pwm_b&0xff)
    wr(0x2d,((pwm_a>>4)&0x30)|((pwm_b>>8)&3))
    wbit(0x29,4,1,1)

def lamp_off():
    # drop the lamp enable and both power-rail bits
    wbit(0x60,1,1,0); wbit(0x29,4,1,0); wbit(0x2a,4,1,0)

def counters(w16=False):
    # pulse GO, wait for the line counter to advance, read per-bank max/min
    motor_go(1)
    t0=time.monotonic()
    while time.monotonic()-t0<2.0:
        if (((rd(0x1a)&0x1f)<<8)|rd(0x19)): break
        time.sleep(0.01)
    motor_go(0)
    out=[]
    for bank in (0,1,2):
        wbit(0x30,0,2,bank)
        if w16: out.append(((rd(0x35)<<8)|rd(0x34),(rd(0x37)<<8)|rd(0x36)))
        else:   out.append((rd(0x35),rd(0x37)))
    return out

def mini_slope(E,div):
    # 12-byte static slope: BE32(E*0x18/div | 0x80000000), BE32(0x80000000), BE32(0)
    sdram(0x803fff)
    _bulk_out(_struct.pack('>III',(E*0x18//div)|0x80000000,0x80000000,0))

def static_meas(feed=0,width=0x2968,afedef=True):
    # 8-bit colour static measurement program (no motor movement)
    wbit(0x64,0,1,0)
    wbit(0x05,3,2,0); wbit(0x05,2,1,0)
    res_class(0)
    wbit(0x01,2,1,1)
    for r in (0x70,0x71,0x72,0x73,0x74,0x75): wr(r,0)
    wr(0x08,0xa8); w16r(0x09,0x0a,8); w16r(0x0b,0x0c,8)
    nothome(0); wbit(0x07,4,1,0); wbit(0x07,0,4,1); set_1b(1)
    mini_slope(0x3e80,4)
    wbit(0x06,7,1,0); identity_matrix(); w16r(0x19,0x1a,1)
    if afedef: afe_defaults()
    wr(0x14,0); wbit(0x06,6,1,0)
    wbit(0x05,0,2,1)                 # colour channels
    wbit(0x06,4,2,1)                 # 8-bit
    wbit(0x20,4,2,0)
    set_lincnt10(feed); set_cnt12(width)
    wbit(0x02,4,2,3 if afedef else 0); wbit(0x02,6,1,0)
    wbit(0x01,5,1,0)

def _gamma_bankA(): return b''.join(_struct.pack('>H',i) for i in range(0x10000))

def _gamma_bankB(): return b''.join(_struct.pack('>H',(i*4)&0xffff) for i in range(0x4000))

def _master_ramp():
    t=[int(x*(1/20.8)) for x in _MASTER_NS[1:]]
    t[0]|=0x80000000; t[-1]|=0x80000000
    return b''.join(_struct.pack('>I',v) for v in t)

def _bulk_out_chunked(data,chunk=61440):
    for k in range(0,len(data),chunk): _bulk_out(data[k:k+chunk])

def native_init():
    # InitializeScanner per CALIBRATION_SPEC section 2 (flatbed, USB 2.0)
    print('\n--- NATIVE INIT (spec section 2) ---')
    nothome(1); motor_rst(0); time.sleep(0.05)
    for _ in range(50):
        if move_done(): break
        time.sleep(0.02)
    # Do not start a scan on a device that has already reported a hard fault.
    # stage_init() checks reg0x04 for this, but native_init() - the path a real
    # scan actually takes - did not, so the fault was carried silently into the
    # scan and showed up later as unexplained bad data.
    _s04 = status04()
    if _s04 == 0x84:
        raise ScannerFatalError('reg0x04=0x84: scanner reported a fatal '
                                'condition during initialisation')
    if _s04==0x07: wr(0x04,8)
    wbit(0x60,7,1,1); wbit(0x60,3,1,1)
    for r,v in ((0x52,0x0f),(0x53,0x11),(0x54,0x13),(0x55,0x15)): wr(r,v)
    wbit(0x01,0,1,1); wbit(0x60,4,2,2)
    wbit(0x05,5,1,0)
    wbit(0x48,1,3,0); wbit(0x48,5,3,0); wbit(0x49,1,3,0); wbit(0x49,5,3,0)
    lamp_on(0x320,0x320)
    wbit(0x29,0,4,3); wbit(0x2a,0,4,3)          # lamp intensity nibble default
    A=_gamma_bankA(); B=_gamma_bankB()
    for addr in (0x83ffff,0x81ffff,0x83ffff,0x85ffff):
        sdram(addr); _bulk_out_chunked(A)
    for addr in (0x80ffff,0x807fff,0x80ffff,0x817fff):
        sdram(addr); _bulk_out_chunked(B)
    wbit(0x20,6,2,1); wbit(0x01,2,1,1); wbit(0x20,4,2,1); wbit(0x05,2,1,0); wbit(0x06,3,1,1)
    for b in (3,4,7,6): wbit(0x01,b,1,0)
    wr(0x08,0xa8); w16r(0x09,0x0a,8); w16r(0x0b,0x0c,8)
    for r in (0x70,0x71,0x72,0x73,0x74,0x75): wr(r,0)
    wbit(0x64,0,1,0)
    for b in range(2,8): wbit(0x50,b,1,0)
    sdram(0x7fffff); _bulk_out(_master_ramp())
    wbit(0x2f,0,6,0x0a); wbit(0x03,2,1,1)
    # power-on settle / home (soft-reset pulse as in stage_init)
    wr(0x48,0); wr(0x49,0); wr(0x2e,0x28); set_1b(20000); wbit(0x20,4,2,1)
    motor_rst(1); time.sleep(1.0); motor_rst(0)
    for _ in range(100):
        if move_done(): break
        time.sleep(0.02)
    wr(0x2e,0xff)
    print('  native init done. reg0x03=0x%02x reg0x04=0x%02x  home=%s'%(
        rd(0x03),rd(0x04),'YES' if at_home() else 'NO'))

def native_warmup(log=print):
    # CALIBRATION_SPEC §3 (flatbed): brightness gate + PWM search + stability
    wbit(0x2f,7,1,1)
    lamp_on(0x320,0x320)
    static_meas(0,0x2968,True)
    if WARM_LAMP_FASTPATH:
        _hit = _lamp_state_load()
        if _hit:
            _pwm = _hit['pwm']
            wr(0x2b,0); wr(0x2c,_pwm&0xff); wr(0x2d,(_pwm>>8)&3)
            _s1 = [m for m,_ in counters()]
            if all(v<8 for v in _s1):
                raise RuntimeError('lamp dead (all peaks <8)')
            # The two stability reads MUST be separated in time. A lamp that is
            # still warming passes THROUGH the settled brightness on its way up,
            # so two back-to-back reads can both sit inside tolerance and look
            # steady while the lamp is in fact still climbing - the one case
            # this gate could wrongly pass. The full path samples across 3 s,
            # which is what catches it.
            time.sleep(WARM_LAMP_READ_GAP_S)
            _s2 = [m for m,_ in counters()]
            _steady = all(abs(_s1[c]-_s2[c]) <= WARM_LAMP_STEADY_TOL for c in range(3))
            _unsat  = all(v <= WARM_LAMP_MAX_PEAK for v in _s1 + _s2)
            _match  = all(abs(_s1[c]-_hit['peaks'][c]) <= WARM_LAMP_PEAK_TOL
                          for c in range(3))
            if _steady and _unsat and _match:
                log('  warmup: lamp already warm at pwm=%d, peaks=%s (ref %s, %.0fs) '
                    '- skipping settle' % (_pwm, _s1, _hit['peaks'], _hit['age']))
                set_1b(3000); motor_stop(); wbit(0x20,4,2,1)
                nothome(1)
                wr(0x04,0x86)      # warm-state acknowledge, same as the full path
                wbit(0x2f,7,1,0)
                return _pwm
            log('  warmup: lamp not settled (steady=%s unsaturated=%s match=%s) '
                '- full warm-up' % (_steady, _unsat, _match))
    log('  warmup: lamp on, settling 4s...')
    time.sleep(4.0)
    s=[m for m,_ in counters()]
    log('  warmup: peaks %s'%s)
    if not any(v>0x77 for v in s):
        time.sleep(4.0); s=[m for m,_ in counters()]
    t0=time.monotonic()
    while True:
        if all(v<8 for v in s): raise RuntimeError('lamp dead (all peaks <8)')
        if all(v>0xd1 for v in s) or time.monotonic()-t0>5.0: break
        time.sleep(0.3); s=[m for m,_ in counters()]
    # lamp PWM binary search: largest pwm<=800 with peak<=200
    pwm=0; step=0x200
    for _ in range(10):
        pwm=min(pwm+step,0x7fff)
        wr(0x2b,0); wr(0x2c,pwm&0xff); wr(0x2d,(pwm>>8)&3)
        time.sleep(0.3)
        s=[m for m,_ in counters()]
        if any(v>200 for v in s): pwm-=step
        step>>=1
    pwm=min(pwm,800)
    wr(0x2b,0); wr(0x2c,pwm&0xff); wr(0x2d,(pwm>>8)&3)
    log('  warmup: lamp PWM = %d'%pwm)
    s=[m for m,_ in counters()]
    if all(v<8 for v in s): raise RuntimeError('lamp dead after PWM search')
    # stability: 3 consecutive seconds with all pairwise per-channel diffs <= 2
    hist=[]; ok=0; t0=time.monotonic()
    while ok<3 and time.monotonic()-t0<10.0:
        time.sleep(1.0)
        s=[m for m,_ in counters()]
        if all(v<8 for v in s): raise RuntimeError('lamp died during stability wait')
        hist.append(s); hist=hist[-3:]
        if len(hist)==3 and all(abs(a[c]-b[c])<=2 for c in range(3)
                                for a in hist for b in hist): ok+=1
        else: ok=0
    log('  warmup: stable=%s last peaks %s (%.1fs)'%(ok>=3,s,time.monotonic()-t0))
    set_1b(3000); motor_stop(); wbit(0x20,4,2,1)
    nothome(1)
    wr(0x04,0x86)          # warm-state acknowledge (spec section 3.5)
    wbit(0x2f,7,1,0)
    # Record what this FULL warm-up settled on, for the fast path above. Only
    # written here, never on the fast path, so the reference cannot drift by
    # being re-derived from itself.
    if WARM_LAMP_FASTPATH:
        _lamp_state_save(pwm, s)
    return pwm

def gain_code(peak):
    g=GAIN_K/max(peak,1)
    if g<1.0: return 0x4b
    if g>=7.4: return 0xff
    return int(283.0-208.0/g)&0xff

def offset_search(dpi,log=print):
    # black-level binary search on the 16-bit MIN counters, lamp OFF
    if dpi<0x4b0: wbit(0x05,2,1,1); res_class(1); width=0x14b4
    else:         wbit(0x05,2,1,0); res_class(0); width=0x2968
    lamp_off()
    wbit(0x06,6,1,0); wr(0x14,0); wbit(0x05,0,2,1); wbit(0x06,4,2,3)
    wbit(0x06,7,1,0); identity_matrix(); w16r(0x19,0x1a,1); wbit(0x05,3,2,0)
    sdram(0x803fff); _bulk_out(bytes.fromhex('8000038080000000'))
    set_1b(1); wbit(0x06,3,1,1)
    for b in (4,3,7,6): wbit(0x01,b,1,0)
    wbit(0x20,4,2,1); set_lincnt10(0); wbit(0x07,0,4,1)
    wbit(0x02,4,2,3); wbit(0x02,6,1,0)
    off=[0x80]*3; step=[0x40]*3; done=[False]*3; e0=[0]*3
    for it in range(8):
        for c in range(3):
            if not done[c]: afe(0x20+c,off[c]&0xff)
        r=[mn for _,mn in counters(w16=True)]
        for c in range(3):
            if done[c]: continue
            e=r[c]-0x400
            if it==0:
                e0[c]=e
                if abs(e)<=0x100: done[c]=True
                elif e<=0: off[c]=0x3f
                else: off[c]=0xc0
            else:
                if abs(e)<=0x100: done[c]=True
                else:
                    if e0[c]>0 and e<0: off[c]-=step[c]
                    elif e0[c]<0 and e>0: off[c]+=step[c]
                step[c]>>=1
                if not done[c]:
                    if e<0: off[c]-=step[c]
                    if e>0: off[c]+=step[c]
        if all(done): break
    for c in range(3): afe(0x20+c,off[c]&0xff)
    log('  offsets: %s (done=%s)'%(['%02x'%(o&0xff) for o in off],done))
    lamp_on(0,_NATIVE['pwm'])
    motor_go(0); motor_stop()
    return off

def green_peak():
    # static measurement; returns the green channel's 8-bit max
    static_meas(0,0x2968,False)
    wbit(0x02,4,2,3)
    s=counters()
    set_1b(3000); motor_stop(); wbit(0x20,4,2,1)
    motor_go(0); wbit(0x02,4,2,0)
    return s[1][0]

def _calib_capture(isWhite,W,E,log=print):
    # 20-line 3-channel 16-bit static capture at the calibration res-class.
    # Section-6 COMMON setup (the part before the class config):
    wbit(0x01,5,1,0); wbit(0x2f,7,1,1)          # scan enable (was missing -> 0 lines)
    wbit(0x64,0,1,0)
    for _ in range(3): w16r(0x33,0x34,0x400)    # calibration exposure gates
    for r in (0x70,0x71,0x72,0x73,0x74,0x75): wr(r,0)
    wr(0x08,0xa8); w16r(0x09,0x0a,8); w16r(0x0b,0x0c,8); wbit(0x01,2,1,1)
    for b in (4,3,7,6): wbit(0x01,b,1,0)
    wbit(0x05,2,1,0)
    for _ in range(50):
        if move_done(): break
        time.sleep(0.02)
    wbit(0x06,7,1,0); wbit(0x06,3,1,1)
    nothome(1)
    wbit(0x06,4,2,3)                    # 16-bit
    wbit(0x07,0,4,1); wbit(0x07,4,1,0); wbit(0x05,3,2,0)
    # class c120 (600): W=0x14b4, E>>=1, exposure 4
    w16r(0x09,0x0a,4); w16r(0x0b,0x0c,4)
    wbit(0x05,2,1,1); res_class(1)
    wbit(0x06,6,1,0); identity_matrix(); wr(0x14,0)
    wbit(0x20,0,4,0xb if isWhite else 0)
    for r,s,wd in ((0x48,1,3),(0x48,5,3),(0x49,1,3),(0x49,5,3)): wbit(r,s,wd,0)
    wbit(0x20,0,4,5); mini_slope(E,4)   # 600-class: 0x20[3:0]=5, div=4
    set_1b(0x14)
    wbit(0x20,4,2,1)
    if isWhite: lamp_on(0,_NATIVE['pwm'])
    else: lamp_off()
    wbit(0x05,0,2,1)                    # colour
    set_lincnt10(0); set_cnt12(W)
    wbit(0x01,5,1,1)                    # USB 2.0
    # CCD window constants for the calibration capture
    w16r(0x40,0x41,0x80); w16r(0x42,0x43,0x5a9); w16r(0x44,0x45,0xad3)
    w16r(0x46,0x47,0); w16r(0x17,0x18,0x3e); w16r(0x19,0x1a,0)
    wbit(0x64,0,1,0)
    wbit(0x02,4,2,0 if isWhite else 3)
    motor_go(1); commit()
    lines=[]
    for _ in range(20):
        chunk=_patient_bulk_in(W*6,quiet_s=1.5)
        lines.append(bytes(chunk))
    motor_go(0)
    data=b''.join(lines)
    n=len(data)//(W*6)
    rows=[_struct.unpack('>%dH'%(W*3),data[k*W*6:(k+1)*W*6]) for k in range(n)]
    if n:
        r0=rows[0]
        means=[int(sum(r0[c::3])/(len(r0)//3)) for c in range(3)]
    else:
        means=[-1]*3
    log('  capture %s: %d/20 lines, ch sample %s'%('white' if isWhite else 'dark',n,means))
    return rows,W

def native_calibrate(dpi=300, log=print):
    # DoCalibration flatbed (pure python). The calibration/shading WIDTH is res-class
    # dependent (CALIBRATION_SPEC: 0x14b4 for the 600 class, 0x2968 for the 1200 class).
    # 0x2968 (10600) is ~2x 0x14b4 because res-class 0 calibrates BOTH CCD pixel arrays;
    # the 600-class 0x14b4 only calibrates one, so a 1200 scan's second array gets no
    # shading and comes out black past ~col 5000. 75-600 stay on 0x14b4, unchanged.
    W = 0x2968 if dpi >= 1200 else 0x14b4
    E = 0x2a00
    # ---- WHITE: gains first ----
    lamp_on(0,_NATIVE['pwm'])
    wbit(0x01,5,1,0); wbit(0x2f,7,1,1)          # scan enable for counter latching
    for _ in range(3): w16r(0x33,0x34,0x400)
    wbit(0x05,2,1,0)
    wbit(0x06,7,1,0); identity_matrix(); w16r(0x19,0x1a,1); wbit(0x06,3,1,1)
    for b in (4,3,7,6): wbit(0x01,b,1,0)
    wbit(0x06,6,1,0); wr(0x14,0)
    wbit(0x05,0,2,1); wbit(0x06,4,2,1); wbit(0x05,3,2,0); res_class(0); wbit(0x20,4,2,1)
    set_lincnt10(0); set_cnt12(0x2968)
    wbit(0x02,4,2,3); wbit(0x02,6,1,0)
    afe_defaults()
    peaks=[m for m,_ in counters()]
    log('  unity-gain white peaks: %s'%peaks)
    if all(v<8 for v in peaks): raise RuntimeError('lamp failure in calibration')
    gains=[gain_code(p) for p in peaks]
    for c,g in enumerate(gains): afe(0x28+c,g)
    log('  AFE gains: %s'%['%02x'%g for g in gains])
    # ---- offsets (lamp off inside) ----
    w16r(0x09,0x0a,4); w16r(0x0b,0x0c,4)
    offset_search(dpi if dpi>=1200 else 600, log)
    # ---- white capture ----
    rows,W2=_calib_capture(True,W,E,log)
    nL=len(rows); NC=W*3
    white=[0.0]*NC
    if nL>=20:
        for i in range(NC):
            col=sorted((r[i] for r in rows),reverse=True)
            white[i]=sum(col[2:18])/16.0         # trimmed mean (drop 2 hi, 2 lo)
    elif nL:
        for i in range(NC):
            white[i]=sum(r[i] for r in rows)/float(nL)
    # ---- dark capture ----
    g0=green_peak()                              # green peak with the lamp on
    drows,_=_calib_capture(False,W,E,log)
    nD=max(1,len(drows))
    dark=[sum(r[i] for r in drows)/float(nD) if drows else 0.0 for i in range(NC)]
    # smooth (100-px forward mean per channel, +/-0x14 guard) then bias -0x100
    dsm=[0.0]*NC
    for c in range(3):
        v=[dark[x*3+c] for x in range(W)]
        cum=[0.0]
        for t in v: cum.append(cum[-1]+t)
        for x in range(W):
            n100=min(100,W-x)
            fm=(cum[x+n100]-cum[x])/n100
            out=(v[x] if abs(fm-v[x])>0x14 else fm)-0x100
            dsm[x*3+c]=out if out>0 else 0.0
    # lamp back on + recovery to 80% of pre-dark green peak
    lamp_on(0,_NATIVE['pwm'])
    t0=time.monotonic(); g=0
    while time.monotonic()-t0<30.0:
        g=green_peak()
        if g0 and g>=int(0.8*g0): break
        time.sleep(0.2)
    log('  lamp recovered: green %d/%d (%.1fs)'%(g,g0,time.monotonic()-t0))
    # ---- shading build + upload ----
    K=0x7d000000
    out=bytearray()
    for x in range(W):
        for c in range(3):
            w=int(white[x*3+c]); d=int(dsm[x*3+c])
            span=(w-d) if w>d else 1
            gn=min(K//span,0x1fffe); gn=(gn+1)>>1
            out+=_struct.pack('>H',gn)
        for c in range(3):
            out+=_struct.pack('>H',int(dsm[x*3+c])&0xffff)
        if (len(out)&0x1ff)==0x1f8: out+=b'\x00'*8
    sdram(0xffffff)
    # Chunked OUT (SDRAM auto-increments): a single OUT transfer has a ceiling the
    # 600-class table (64608 B) clears but the 1200-class table (~129216 B) does not,
    # so a one-shot _bulk_out times out. Same mechanism the gamma uploads already use.
    _bulk_out_chunked(bytes(out))
    wbit(0x2f,7,1,0)
    log('  shading uploaded: %d bytes (W=%d)'%(len(out),W))
    return dict(gains=gains,white=white,dark=dsm)

def emit_motor_tables(xdpi=75, exposure=0x2a00, travel=1310, chan_gain=1.0):
    # Three generated tables: master->0x7fffff, slope->0x803fff, home-decel->0x801fff.
    master=motor_tables.build_master_ramp()
    slope =motor_tables.build_slope(xdpi,exposure=exposure,travel=travel,chan_gain=chan_gain)
    tail  =motor_tables.build_hometail(xdpi,exposure=exposure)
    wr(0x06,0x30); wr(0x02,0x80); rd(0x03)
    # reg20 bits[5:4] is resolution-tiered: 0x50 for 75/300/600 (hardware-proven -
    # 0x60 skews those and cuts the plate short), 0x60 for 150 AND 1200 (150's
    # over-run and 1200's motor are on this tier). 2400 is on the same high-res tier
    # as 1200 (0x60) - DERIVED, and needs hardware confirmation of carriage travel.
    wr(0x20, 0x60 if xdpi in (150, 1200, 2400) else 0x50); wr(0x36,0x89)
    sdram(0x7fffff); _bulk_out(master)
    sdram(0x803fff); _bulk_out(slope)
    sdram(0x801fff); _bulk_out(tail)
    return len(master),len(slope),len(tail)

def emit_scan_program(xdpi, ydpi, width, lines, mode, depth=8, ytop=0, matrix=None,
                      xfeed=None):
    """Parametric imaging program for a flatbed scan over USB 2.0, hardware-verified
    at every native rung. Emits register writes directly. The
    exposure, CCD phase and slope upload must already be in effect (they are set
    by the calibration prefix that runs before this).
    mode: 0 gray, 1 mono-variant, 2 COLOR. matrix: 9 Q13 ints row-major or None=identity."""
    CF66 = 278                                  # device geometry const (derived: feed=18 @ytop=0,ydpi=75)
    w16 = lambda lo,hi,v: (wr(lo,v&0xff), wr(hi,(v>>8)&0xff))
    w16(0x12,0x13,width)                        # step 32  WIDTH px
    # step 33  horizontal start of the CCD window. `xfeed` is the region path's
    # explicit window start; without it the baseline below applies, which is what
    # a full-bed scan has always used.
    feed = (ytop//2 + 10 + CF66)//(1200//ydpi) if xfeed is None else xfeed
    w16(0x10,0x11,feed)
    wbit(0x06,4,2,1 if depth==8 else 3)         # step 34  depth code (USB2)
    # Y-step divider: res-class-0 (>=1200) = 1 (reg07=0x01); res-class-1 = 600/ydpi.
    # A divider of 2 at 1200 desyncs the line clock and streams 0 bytes. ydpi here is
    # always a native rung (75/150/300/600/1200) — the resampled rungs 100/200/400/800
    # are remapped to their native rung in scan() before this runs.
    ydiv = 1 if ydpi>=1200 else 600//ydpi
    wbit(0x07,0,4,ydiv)                         # step 35  Y step divider
    wbit(0x07,4,1,0)                            # step 36
    w16(0x1b,0x1c,lines)                        # step 37  capture line count
    avg = 0x40 if xdpi==4800 else 0x20//(2400//xdpi)
    wr(0x14,avg)                                # step 38  pixel average
    t = {2400:0x88,4800:0x88,1200:0x90,600:(0x60 if ydpi<1200 else 0x30),300:0x90}.get(xdpi,0xc0)
    wr(0x1e,t); wr(0x1f,t)                      # step 39
    for v in (0x46e,0x469,0x447): w16(0x33,0x34,v)   # step 40  R,G,B exposure gates
    if mode==2:                                 # step 41  COLOR: [1:0]=1
        wbit(0x05,0,2,1)
    elif mode==1:
        wbit(0x05,0,2,2); wbit(0x05,6,2,1)
    else:
        wbit(0x05,0,2,0); wr(0x15,0x80); wr(0x16,0x80); wbit(0x05,6,2,1)
    # reg03[7:6]=11 -> reg03=0xc4 (with bit2 already set). This is the ASIC's power-on
    # shadow default (shadow byte 3 = 0xc0), which must be carried through every
    # reg03 RMW at EVERY resolution; our register image starts at 0, so we set it
    # explicitly. At 1200 these bits are load-bearing for the res-class-0 motor
    # Y-scale; at <=600 they are carried through as well.
    wbit(0x03,6,2,3)
    if xdpi>=1200:
        # Res-class 0 only: reg05 bit6 = dual-array channel-combine (FULL_PROGRAM_SPEC
        # reg5[7:6]).
        wbit(0x05,6,1,1)
    wbit(0x01,3,1,1); wbit(0x01,4,1,1)          # step 42
    wbit(0x01,7,1,1 if depth!=16 else 0)        # step 43
    wbit(0x01,6,1,0)                            # step 44
    n=(avg*width+0x400)>>10                     # step 45  CCD window + pixel timing (USB2)
    m=(0xf7f-3*n)//3
    w16(0x40,0x41,0x80); w16(0x42,0x43,m+0x80); w16(0x44,0x45,n+0x80+2*m)
    w16(0x46,0x47,0)
    w16(0x17,0x18,min(0xfff,(m<<10)//width-1))
    w16(0x19,0x1a,0)
    wr(0x1d,0x10)                               # step 46
    k=0 if xdpi<150 else 4                      # step 48
    wbit(0x49,1,3,k); wbit(0x48,5,3,k)
    M = matrix if matrix else [0x2000,0,0, 0,0x2000,0, 0,0,0x2000]   # step 49 (col-major stream)
    stream=[M[0],M[3],M[6],M[1],M[4],M[7],M[2],M[5],M[8]]
    for v in stream: wr(0x37,v&0xff); wr(0x38,(v>>8)&0xff)
    # step 51  LAUNCH. Res-class 1 (<=600 dpi): run-mode 2 (reg02 bits[5:4]=10).
    # Res-class 0 (>=1200 dpi): run-mode 0 - the full-page pass launches with
    # reg02=0x82 (bit5 CLEAR). Leaving bit5 set at 1200 moves the carriage but
    # streams 0 bytes.
    wbit(0x02,4,2,0 if xdpi>=1200 else 2)
    wbit(0x2f,7,1,1)
    wbit(0x02,1,1,1)                            # GO
    commit()                                    # SEL 0x24 strobe
    return feed

def stage_init():
    # ASIC bring-up: register defaults and a soft-reset pulse. NO motor run.
    print('\n--- INIT (ASIC bring-up, no motor) ---')
    motor_rst(0)                       # clear reg0x02 bit0
    time.sleep(0.1)
    # readiness poll: reg0x03 bit3, abort if reg0x04 == 0x84
    for _ in range(50):
        if move_done(): break
        if status04()==0x84: print('  !! reg0x04=0x84 fatal during readiness'); break
        time.sleep(0.02)
    if status04()==0x07: wr(0x04, 8)   # clear the 0x07 power-on status
    wr(0x48,0); wr(0x49,0)              # scan-area fields = 0
    wr(0x2e, 0x28)                     # reg0x2e = 0x28
    set_1b(20000)                     # reg0x1b/0x1c = 0x4e20
    wbit(0x20, 4, 2, 1)               # reg0x20 bits4-5 = 1
    # soft-reset pulse: reg0x02 bit0 = 1, wait ~1s, = 0
    motor_rst(1); time.sleep(1.0); motor_rst(0)
    for _ in range(100):
        if move_done(): break
        time.sleep(0.02)
    wr(0x2e, 0xff)                    # reg0x2e = 0xff
    print('  init done. reg0x03=0x%02x reg0x04=0x%02x reg0x64=0x%02x  totals=%s'
          % (rd(0x03), rd(0x04), rd(0x64), ostat))
    print('  home now:', 'AT HOME' if at_home() else 'NOT at home')
