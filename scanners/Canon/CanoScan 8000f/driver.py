#!/usr/bin/env python3
"""CanoScan 8000F native driver (pyusb/libusb) - clean scan pipeline.

Pure Python. No Canon software, no emulator, no replayed capture data: device
init, lamp warm-up, AFE gain/offset calibration, shading, motor tables and the
scan register program are all generated from reverse-engineered CNQL2403.DLL
logic. See the *_SPEC.md docs.

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
TRAVEL_STEPS = 1310   # OEM imaging-move carriage travel (steps); see scan()
_NATIVE = {'pwm': 800}

# module-level device state (single scanner)
dev = None; ep_in = None; ep_out = None
shadow = [0] * 0x80
ostat = {'w': 0, 'r': 0}
_MASTER_NS = motor_tables.MASTER_NS   # 1179-entry ns ramp (from motor_tables)

def log(*a):
    msg = ' '.join(str(x) for x in a)
    if _PROGRESS: _PROGRESS(msg)
    else: print(msg)
_PROGRESS = None
def set_progress(cb): 
    global _PROGRESS; _PROGRESS = cb

def open_device(backend_hint=True):
    """Open the CanoScan 8000F. Raises RuntimeError if not found."""
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

_MODEMAP = {'color': 2, 'colour': 2, 'gray': 1, 'grey': 1, 'lineart': 0, 'bw': 0}

def scan(dpi=300, mode='color', depth=8, progress=None, preview=None):
    """Run a full native scan. Returns (raw_bytes, meta). mode: color|gray|lineart."""
    global _PROGRESS
    if progress is not None: _PROGRESS = progress
    m = _MODEMAP.get(str(mode).lower(), 2)
    lineart = (m == 0); color = (m == 2)
    if lineart: depth = 1
    requested_dpi = dpi
    # Native hardware resolutions are 75/150/300/600/1200 — the vendor's motor
    # generator (CNQL2403.DLL) has slope + home-decel tables for *exactly* these.
    # ScanGear's other listed resolutions (100/200/400/800) are not driven on the
    # hardware; it scans the next native rung up and resamples in software. Each is
    # 2/3 of a native rung: 100=⅔·150, 200=⅔·300, 400=⅔·600, 800=⅔·1200. We do the
    # same — drive the hardware natively, then LANCZOS-resample to the requested
    # size on export. (Driving the motor at a non-native rate has no tail table and
    # would over-run the carriage, the same failure mode as the old native-150 bug.)
    _RESAMPLE_FROM = {100: 150, 200: 300, 400: 600, 800: 1200}
    out_width = out_lines = None
    if dpi in _RESAMPLE_FROM:
        out_width = int(round(620 * dpi / 75.0))
        out_lines = int(round(876 * dpi / 75.0))
        dpi = _RESAMPLE_FROM[dpi]          # program the hardware at the native rung
    downscale = 1
    width = int(round(620 * dpi / 75.0))
    lines = int(round(876 * dpi / 75.0))
    expo  = 0x5400 if dpi >= 1200 else 0x2a00
    motor_expo = expo
    # Motor travel = the OEM geometry value (~1310 steps), confirmed against a real
    # ScanGear 150-dpi USB capture: its slope + home-decel tail are byte-exact with
    # build_slope(dpi, travel=TRAVEL_STEPS) / build_hometail(dpi). Do NOT reduce it -
    # the home-decel tail is a fixed ramp that assumes the carriage stops at this
    # position; a shorter travel makes the tail over-drive the carriage past home.
    travel = TRAVEL_STEPS
    if lineart:   stride = (width + 7) // 8
    elif color:   stride = width * 3 * (2 if depth == 16 else 1)
    else:         stride = width * (2 if depth == 16 else 1)
    target = lines * stride
    if out_width:
        log('scan %d dpi (native %d + resample)  %s  %d-bit  ->  %dx%d px out  (%d bytes captured)'
            % (requested_dpi, dpi, mode, depth, out_width, out_lines, target))
    else:
        log('scan %d dpi  %s  %d-bit  ->  %dx%d  (%d bytes)' % (dpi, mode, depth, width, lines, target))

    native_init()
    _NATIVE['pwm'] = native_warmup()
    native_calibrate()
    lamp_on(0x320, _NATIVE['pwm'])
    wbit(0x01, 5, 1, 1)
    wr(0x08, 0x01)
    # reg09/0a and reg0b/0c = CCD line-period, and they scale with the exposure:
    # expo>>4  (0x2a00>>4 = 0x2a0 for <=600 dpi; 0x5400>>4 = 0x540 at 1200). This was
    # hardcoded 0x2a0, which is only correct for the <=600 exposure - at 1200 (expo
    # 0x5400) it left the line period at half, so the CCD clocked lines twice as fast
    # as the carriage advanced: the motor covered only ~40% of the bed by the time the
    # line count was reached, and the rest of the frame came out blank/stretched.
    # Verified against the ScanGear 1200 capture (reg09/0a = 0x0540 there).
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
    emit_motor_tables(dpi, exposure=motor_expo, travel=travel, lines=lines)
    feed = emit_scan_program(dpi, dpi, width, lines, m, depth=depth)
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
    tq = time.monotonic()
    while time.monotonic() - tq < 20.0:
        if move_done(): break
        time.sleep(0.05)
    wr(0x02, 0x00)
    _reactive_home_quiet()
    teardown()          # clear scan-mode bits so the ASIC idles cleanly (stops the status-LED flicker)
    meta = dict(dpi=requested_dpi, scandpi=dpi, downscale=downscale,
                out_width=out_width, out_lines=out_lines,
                width=width, lines=lines, channels=3 if color else 1,
                depth=depth, lineart=1 if lineart else 0, stride=stride, mode=mode)
    log('done: %d bytes' % len(image))
    return bytes(image), meta

def _reactive_home_quiet(timeout_s=25.0):
    wr(0x02, 0x00); wr(0x02, 0x80); wr(0x02, 0xa0)
    t0 = time.monotonic(); homed = False; seen = False
    while time.monotonic() - t0 < timeout_s:
        s = rd(0x03)
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

def rd(addr):
    for _ in range(4):
        sel(addr)
        try:
            r = dev.ctrl_transfer(0xc0, REQ_REG, V_RDREG, 0, 1, timeout=2000)
        except Exception:
            time.sleep(0.02); continue
        if r is not None and len(r):
            ostat['r'] += 1; return bytes(r)[0]
        time.sleep(0.02)
    ostat['r'] += 1; return 0

def wbit(addr, start, width, val):
    mask=((1<<width)-1)<<start
    shadow[addr]=(shadow[addr] & ~mask) | ((val<<start)&mask)
    wr(addr, shadow[addr])

def commit(): sel(0x24)

def afe(afe_addr, val):                              # FUN_10006140: 0x25=addr, 0x26=data
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
    off=0
    while off<size:
        n=dev.write(ep_out.bEndpointAddress, payload[off:off+0xf000], timeout=3000)
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
    # DLL-verified end-of-scan quiesce (FUN_10009da0): clear scan-mode bits so the ASIC goes
    # fully idle and the status LED stops flickering (no power-cycle needed between scans).
    try:
        wbit(0x2f,7,1,0)     # FUN_100062e0(0): reg0x2f bit7
        wbit(0x02,1,1,0)     # FUN_10005b70(0): reg0x02 bit1 (GO off)
        wbit(0x07,4,1,0)     # FUN_10005dc0(0):  reg0x07 bit4
        wbit(0x03,2,1,0)     # FUN_10005bd0(0):  reg0x03 bit2 (scan-enable off)
        wbit(0x60,0,1,0); wbit(0x60,1,1,0)               # FUN_10006700/66c0(0): reg0x60 bits0-1
        wbit(0x48,1,3,0); wbit(0x48,5,3,0)               # FUN_10006500/64e0(0): reg0x48 fields
        wbit(0x49,1,3,0); wbit(0x49,5,3,0)               # FUN_10006540/6520(0): reg0x49 fields
        wbit(0x02,7,1,1)     # FUN_10005ac0(1): reg0x02 bit7 (home direction)
        wr(0x02,0x00)        # motor power off
    except Exception as e:
        print('  teardown warn:', e)

def w16r(lo,hi,v): wr(lo,v&0xff); wr(hi,(v>>8)&0xff)

def sdram(addr):
    wr(0x21,addr&0xff); wr(0x22,(addr>>8)&0xff); wr(0x23,(addr>>16)&0xff); commit()

def afe_defaults():
    # FUN_100071e0: setup + offsets mid-scale + unity gains
    for a,v in ((0x04,0x00),(0x01,0x23),(0x02,0x2c),(0x03,0x1f),
                (0x20,0x80),(0x21,0x80),(0x22,0x80),(0x28,0x4b),(0x29,0x4b),(0x2a,0x4b)):
        afe(a,v)

def res_class(k):
    # FUN_10008180: AFE reg3 + CCD phase registers per resolution class
    if k==0:   afe(0x03,0x1f); vals=(0x0c,0x0e,0x10,0x12)
    elif k==1: afe(0x03,0x2f); vals=(0x0c,0x14,0x16,0x00)
    else:      afe(0x03,0x2f); vals=(0x0f,0x01,0x13,0x15)
    for r,v in zip((0x52,0x53,0x54,0x55),vals): wr(r,v)

def identity_matrix():
    for v in (0x2000,0,0,0,0x2000,0,0,0,0x2000):
        wr(0x37,v&0xff); wr(0x38,(v>>8)&0xff)

def lamp_on(pwm_a=0x320,pwm_b=0x320):
    # FUN_10006230(a,b) + reg 0x29.b4=1
    wr(0x2b,pwm_a&0xff); wr(0x2c,pwm_b&0xff)
    wr(0x2d,((pwm_a>>4)&0x30)|((pwm_b>>8)&3))
    wbit(0x29,4,1,1)

def lamp_off():
    # FUN_10007bc0
    wbit(0x60,1,1,0); wbit(0x29,4,1,0); wbit(0x2a,4,1,0)

def counters(w16=False):
    # FUN_10009ae0: pulse GO, wait line-counter nonzero, read per-bank max/min
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
    # FUN_10009960: 8-bit colour static measurement program (no motor)
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
    if status04()==0x07: wr(0x04,8)
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
    return pwm

def gain_code(peak):
    g=GAIN_K/max(peak,1)
    if g<1.0: return 0x4b
    if g>=7.4: return 0xff
    return int(283.0-208.0/g)&0xff

def offset_search(dpi,log=print):
    # FUN_10008c10: black-level binary search on 16-bit MIN counters, lamp OFF
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
    # FUN_1000f060: static measurement, return green 8-bit max
    static_meas(0,0x2968,False)
    wbit(0x02,4,2,3)
    s=counters()
    set_1b(3000); motor_stop(); wbit(0x20,4,2,1)
    motor_go(0); wbit(0x02,4,2,0)
    return s[1][0]

def _calib_capture(isWhite,W,E,log=print):
    # FUN_1000c850 core: 20-line 3ch/16-bit static capture at calibration class.
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
    # CCD window: FUN_100096c0(0x52d0, 0) constants
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

def native_calibrate(log=print):
    # DoCalibration flatbed, 600-class (c120): serves every scan <=600 dpi (pure python)
    W=0x14b4; E=0x2a00
    # ---- WHITE: gains first (FUN_10009180) ----
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
    offset_search(600,log)
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
    g0=green_peak()                              # FUN_1000f060: green peak, lamp on
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
    _bulk_out(bytes(out))
    wbit(0x2f,7,1,0)
    log('  shading uploaded: %d bytes (W=%d)'%(len(out),W))
    return dict(gains=gains,white=white,dark=dsm)

def emit_motor_tables(xdpi=75, exposure=0x2a00, travel=1310, lines=None):
    # Native replacement for capture ops 3460-3479: three generated tables,
    # byte-exact vs the capture. master->0x7fffff, slope->0x803fff, home-decel->0x801fff.
    master=motor_tables.build_master_ramp()
    slope =motor_tables.build_slope(xdpi,exposure=exposure,travel=travel,lines=lines)
    tail  =motor_tables.build_hometail(xdpi,exposure=exposure)
    wr(0x06,0x30); wr(0x02,0x80); rd(0x03)
    # reg20 bits[5:4] is resolution-tiered: 0x50 for 75/300/600 (hardware-proven -
    # 0x60 skews those and cuts the plate short), 0x60 for 150 AND 1200 (both match
    # their ScanGear captures; 150's over-run and 1200's motor are on this tier).
    wr(0x20, 0x60 if xdpi in (150, 1200) else 0x50); wr(0x36,0x89)
    sdram(0x7fffff); _bulk_out(master)
    sdram(0x803fff); _bulk_out(slope)
    sdram(0x801fff); _bulk_out(tail)
    return len(master),len(slope),len(tail)

def emit_scan_program(xdpi, ydpi, width, lines, mode, depth=8, ytop=0, matrix=None):
    """PARAMETRIC imaging program - FULL_PROGRAM_SPEC.md section 5 steps 32-51,
    verified byte-exact against the ScanGear capture (75x75 flatbed USB2.0).
    Emits register writes directly. Steps 12-30 (exposure, phases, slope upload)
    must already be in effect (from the replayed calibration prefix).
    mode: 0 gray, 1 mono-variant, 2 COLOR. matrix: 9 Q13 ints row-major or None=identity."""
    CF66 = 278                                  # device geometry const (derived: feed=18 @ytop=0,ydpi=75)
    w16 = lambda lo,hi,v: (wr(lo,v&0xff), wr(hi,(v>>8)&0xff))
    w16(0x12,0x13,width)                        # step 32  WIDTH px
    feed = (ytop//2 + 10 + CF66)//(1200//ydpi)  # step 33  feed/start count
    w16(0x10,0x11,feed)
    wbit(0x06,4,2,1 if depth==8 else 3)         # step 34  depth code (USB2)
    # Y step divider. Res-class 1 (<=600 dpi): 600/ydpi. Res-class 0 (>=1200 dpi):
    # divider 1 - verified against the ScanGear 1200-dpi USB capture, whose full-page
    # pass writes reg07=0x01. The old 2400/ydpi formula gave 2 at 1200 and desynced
    # the line clock (0 bytes captured).
    ydiv = 1 if ydpi>=1200 else (0xf if ydpi==400 else 600//ydpi)
    wbit(0x07,0,4,ydiv)                         # step 35  Y step divider
    wbit(0x07,4,1,0)                            # step 36
    w16(0x1b,0x1c,lines)                        # step 37  capture line count
    avg = 0x40 if xdpi==4800 else 0x20//(2400//xdpi)
    wr(0x14,avg)                                # step 38  pixel average
    t = {2400:0x88,4800:0x88,1200:0x90,600:(0x60 if ydpi<1200 else 0x30),300:0x90}.get(xdpi,0xc0)
    wr(0x1e,t); wr(0x1f,t)                      # step 39
    for v in (0x46e,0x469,0x447): w16(0x33,0x34,v)   # step 40  R,G,B exposure gates
    if mode==2:                                 # step 41  COLOR: [1:0]=1, [7:6] untouched
        wbit(0x05,0,2,1)
    elif mode==1:
        wbit(0x05,0,2,2); wbit(0x05,6,2,1)
    else:
        wbit(0x05,0,2,0); wr(0x15,0x80); wr(0x16,0x80); wbit(0x05,6,2,1)
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
    # Res-class 0 (>=1200 dpi): run-mode 0 - verified against the ScanGear 1200-dpi
    # USB capture, whose full-page pass launches with reg02=0x82 (bit5 CLEAR). Leaving
    # bit5 set at 1200 moved the carriage but streamed 0 bytes.
    wbit(0x02,4,2,0 if xdpi>=1200 else 2)
    wbit(0x2f,7,1,1)
    wbit(0x02,1,1,1)                            # GO
    commit()                                    # SEL 0x24 strobe
    return feed

def stage_init():
    # Model-based ASIC bring-up (COMPLETE_DRIVER_MODEL section 3 / FUN_1000a9c0). NO motor run.
    print('\n--- INIT (ASIC bring-up, no motor) ---')
    motor_rst(0)                       # clear reg0x02 bit0
    time.sleep(0.1)
    # readiness poll: reg0x03 bit3, abort if reg0x04 == 0x84
    for _ in range(50):
        if move_done(): break
        if status04()==0x84: print('  !! reg0x04=0x84 fatal during readiness'); break
        time.sleep(0.02)
    if status04()==0x07: wr(0x04, 8)   # FUN_1000da60: reg0x04 0x07 -> 8
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
