#!/usr/bin/env python3
"""eSCL / AirScan bridge for the CanoScan 8000F.

Presents the reverse-engineered USB driver as a *driverless* (eSCL) network
scanner. macOS / iOS / Linux discover eSCL scanners with no driver install, so
once this is running the 8000F shows up natively in Image Capture, Preview,
Printers & Scanners, etc. — the "driver" is just this daemon translating between
eSCL HTTP on one side and driver.scan() over USB on the other.

Manual start (no autostart):

    python3 escl_bridge.py               # binds 0.0.0.0:8090, advertises over Bonjour
    python3 escl_bridge.py --port 9000
    python3 escl_bridge.py --no-mdns     # HTTP only (test with curl), no advertising

Needs: pyusb/libusb (via driver.py), pillow (via imaging.py) and, for discovery,
`zeroconf`  (pip install zeroconf).  Without zeroconf it still serves eSCL; you
just won't get automatic discovery.
"""
import argparse, io, os, socket, sys, tempfile, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

import driver, imaging

# ---- device description ------------------------------------------------------

MODEL   = "Canon CanoScan 8000F"
# Stable per-install UUID (eSCL clients key the scanner identity off this).
UUID    = str(uuid.uuid5(uuid.NAMESPACE_DNS, "canoscan8000f.local"))
# Flatbed size in eSCL units (1/300 inch). The native bed is 620x876 px @75dpi
# = 8.27 x 11.68 in  ->  x300.
BED_W_300 = 2480
BED_H_300 = 3504
DPIS    = [75, 100, 150, 200, 300, 400, 600, 800, 1200]

# eSCL ColorMode <-> our driver mode
_MODE = {"RGB24": "color", "Grayscale8": "gray", "BlackAndWhite1": "lineart"}
_FMT  = {"image/jpeg": "jpg", "application/pdf": "pdf"}

SCAN_NS = "http://schemas.hp.com/imaging/escl/2011/05/03"
PWG_NS  = "http://www.pwg.org/schemas/2010/12/sm"

# ---- job state ---------------------------------------------------------------

class Job:
    def __init__(self, jid, dpi, mode, region, ctype):
        self.id = jid; self.dpi = dpi; self.mode = mode
        self.region = region; self.ctype = ctype
        self.state = "Pending"          # Pending -> Processing -> Completed/Aborted
        self.data = None                # encoded page bytes when done
        self.delivered = False
        self.error = None
        self.created = time.time()

_LOCK = threading.Lock()               # serialise hardware access
_JOB  = None                           # the single current job (hardware is exclusive)
FAKE  = False                          # --fake: instant synthetic image, no hardware

def _log(*a):
    print("[escl]", *a, file=sys.stderr, flush=True)

# ---- the actual scan (runs on a worker thread) -------------------------------

def _fake_page(job):
    """Instant synthetic image (no hardware) to isolate protocol vs. scan latency."""
    from PIL import Image, ImageDraw
    w = max(64, min(1240, int(8.27 * job.dpi)))
    h = max(64, min(1754, int(11.68 * job.dpi)))
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(h):
        for x in range(0, w, 4):
            c = (x * 255 // w, y * 255 // h, 128)
            for dx in range(4):
                if x + dx < w: px[x + dx, y] = c
    ImageDraw.Draw(im).rectangle([8, 8, w - 8, h - 8], outline=(0, 0, 0), width=3)
    buf = io.BytesIO()
    if job.ctype == "application/pdf":
        im.save(buf, "PDF", resolution=job.dpi)
    else:
        im.save(buf, "JPEG", quality=90)
    return buf.getvalue()

def _run_job(job):
    job.state = "Processing"
    _log("job %s: scan %ddpi %s -> %s%s" % (job.id[:8], job.dpi, job.mode, job.ctype,
                                            "  [FAKE]" if FAKE else ""))
    tmp = None
    try:
        if FAKE:
            job.data = _fake_page(job)
            job.state = "Completed"
            _log("job %s: FAKE done, %d bytes" % (job.id[:8], len(job.data)))
            return
        with _LOCK:
            driver.open_device()
            try:
                raw, meta = driver.scan(dpi=job.dpi, mode=job.mode, depth=8,
                                        region=job.region,
                                        progress=lambda s: None)
            finally:
                driver.close_device()
        if not raw:
            raise RuntimeError("scan produced no data (carriage may not have moved)")
        tmp = tempfile.mkdtemp(prefix="escl_")
        fmt = _FMT[job.ctype]
        written = imaging.export(raw, meta, os.path.join(tmp, "page"), [fmt])
        with open(written[0], "rb") as f:
            job.data = f.read()
        job.state = "Completed"
        _log("job %s: done, %d bytes" % (job.id[:8], len(job.data)))
    except Exception as e:
        job.error = str(e); job.state = "Aborted"
        _log("job %s: FAILED: %s" % (job.id[:8], e))
    finally:
        if tmp:
            try:
                for n in os.listdir(tmp): os.remove(os.path.join(tmp, n))
                os.rmdir(tmp)
            except Exception:
                pass

# ---- eSCL XML ----------------------------------------------------------------

def caps_xml():
    res = "".join(
        "<scan:DiscreteResolution>"
        "<scan:XResolution>%d</scan:XResolution>"
        "<scan:YResolution>%d</scan:YResolution>"
        "</scan:DiscreteResolution>" % (d, d) for d in DPIS)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
        '<scan:ScannerCapabilities xmlns:scan="%s" xmlns:pwg="%s">'
        '<pwg:Version>2.6</pwg:Version>'
        '<pwg:MakeAndModel>%s</pwg:MakeAndModel>'
        '<scan:UUID>%s</scan:UUID>'
        '<scan:Platen><scan:PlatenInputCaps>'
        '<scan:MinWidth>16</scan:MinWidth><scan:MaxWidth>%d</scan:MaxWidth>'
        '<scan:MinHeight>16</scan:MinHeight><scan:MaxHeight>%d</scan:MaxHeight>'
        '<scan:MaxScanRegions>1</scan:MaxScanRegions>'
        '<scan:SettingProfiles><scan:SettingProfile>'
        '<scan:ColorModes>'
        '<scan:ColorMode>BlackAndWhite1</scan:ColorMode>'
        '<scan:ColorMode>Grayscale8</scan:ColorMode>'
        '<scan:ColorMode>RGB24</scan:ColorMode>'
        '</scan:ColorModes>'
        '<scan:DocumentFormats>'
        '<pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat>'
        '<pwg:DocumentFormat>application/pdf</pwg:DocumentFormat>'
        '<scan:DocumentFormatExt>image/jpeg</scan:DocumentFormatExt>'
        '<scan:DocumentFormatExt>application/pdf</scan:DocumentFormatExt>'
        '</scan:DocumentFormats>'
        '<scan:SupportedResolutions><scan:DiscreteResolutions>%s'
        '</scan:DiscreteResolutions></scan:SupportedResolutions>'
        '</scan:SettingProfile></scan:SettingProfiles>'
        '<scan:MaxOpticalXResolution>1200</scan:MaxOpticalXResolution>'
        '</scan:PlatenInputCaps></scan:Platen>'
        '</scan:ScannerCapabilities>'
        % (SCAN_NS, PWG_NS, escape(MODEL), UUID, BED_W_300, BED_H_300, res))

def status_xml():
    j = _JOB
    # A page is "waiting to be transferred" from job creation until macOS has
    # fetched it via NextDocument. macOS keys its decision to call NextDocument on
    # ImagesToTransfer>=1, and wants the job-state elements in the pwg namespace.
    pending = j is not None and not j.delivered and j.state != "Aborted"
    jobs = ""
    if j is not None:
        if j.delivered:
            jstate, reason, to_xfer, done = "Completed", "JobCompletedSuccessfully", 0, 1
        elif j.state == "Aborted":
            jstate, reason, to_xfer, done = "Canceled", "JobCanceledByUser", 0, 0
        else:
            jstate, reason, to_xfer, done = "Processing", "JobScanningAndTransferring", 1, 0
        jobs = ('<scan:JobInfo>'
                '<pwg:JobUri>/eSCL/ScanJobs/%s</pwg:JobUri>'
                '<pwg:JobUuid>%s</pwg:JobUuid>'
                '<scan:Age>%d</scan:Age>'
                '<pwg:ImagesCompleted>%d</pwg:ImagesCompleted>'
                '<pwg:ImagesToTransfer>%d</pwg:ImagesToTransfer>'
                '<pwg:JobState>%s</pwg:JobState>'
                '<pwg:JobStateReasons><pwg:JobStateReason>%s</pwg:JobStateReason>'
                '</pwg:JobStateReasons>'
                '</scan:JobInfo>'
                % (j.id, j.id, int(time.time() - j.created), done, to_xfer, jstate, reason))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
        '<scan:ScannerStatus xmlns:scan="%s" xmlns:pwg="%s">'
        '<pwg:Version>2.6</pwg:Version>'
        '<pwg:State>%s</pwg:State>'
        '<scan:Jobs>%s</scan:Jobs>'
        '</scan:ScannerStatus>'
        % (SCAN_NS, PWG_NS, "Processing" if pending else "Idle", jobs))

# ---- ScanSettings parsing ----------------------------------------------------

def _tag(el):        # strip namespace
    return el.tag.rsplit("}", 1)[-1]

def parse_settings(body):
    import xml.etree.ElementTree as ET
    root = ET.fromstring(body)
    vals = {}
    region = {}
    for el in root.iter():
        t = _tag(el)
        if el.text and el.text.strip():
            vals[t] = el.text.strip()
        if t in ("Height", "Width", "XOffset", "YOffset"):
            region[t] = int(float(el.text))
    dpi  = int(vals.get("XResolution", "300"))
    mode = _MODE.get(vals.get("ColorMode", "RGB24"), "color")
    ctype = "image/jpeg"
    fmt = vals.get("DocumentFormatExt") or vals.get("DocumentFormat")
    if fmt and fmt.lower() == "application/pdf":
        ctype = "application/pdf"
    # region -> normalised 0..1 bed coords (None if full bed)
    reg = None
    if {"Width", "Height"} <= region.keys():
        x0 = region.get("XOffset", 0); y0 = region.get("YOffset", 0)
        w  = region["Width"]; h = region["Height"]
        if not (x0 == 0 and y0 == 0 and w >= BED_W_300 and h >= BED_H_300):
            reg = (x0 / BED_W_300, y0 / BED_H_300,
                   (x0 + w) / BED_W_300, (y0 + h) / BED_H_300)
    return dpi, mode, reg, ctype

# ---- HTTP handler ------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a): pass          # quiet default
    def log_request(self, code="-", size="-"):
        _log("%s %s -> %s" % (self.command, self.path, code))

    def _xml(self, body, code=200):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def _empty(self, code, headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        p = self.path.split("?", 1)[0].rstrip("/")
        if p.endswith("/eSCL/ScannerCapabilities"):
            return self._xml(caps_xml())
        if p.endswith("/eSCL/ScannerStatus"):
            return self._xml(status_xml())
        if "/eSCL/ScanJobs/" in p and p.endswith("/NextDocument"):
            return self._next_document(p)
        return self._empty(404)

    def do_POST(self):
        p = self.path.split("?", 1)[0].rstrip("/")
        if p.endswith("/eSCL/ScanJobs"):
            return self._create_job()
        return self._empty(404)

    def do_DELETE(self):
        global _JOB
        if "/eSCL/ScanJobs/" in self.path and _JOB is not None:
            _JOB.state = "Aborted"; _JOB.error = "canceled by client"
            return self._empty(200)
        return self._empty(404)

    # -- job lifecycle --
    def _create_job(self):
        global _JOB
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n) if n else b""
        if _JOB is not None and _JOB.state in ("Pending", "Processing"):
            return self._empty(503, {"Retry-After": "5"})   # scanner busy
        try:
            dpi, mode, reg, ctype = parse_settings(body)
        except Exception as e:
            _log("bad ScanSettings: %s" % e)
            return self._empty(400)
        _log("ScanSettings: dpi=%d mode=%s fmt=%s region=%s" % (dpi, mode, ctype, reg))
        jid = uuid.uuid4().hex
        _JOB = Job(jid, dpi, mode, reg, ctype)
        threading.Thread(target=_run_job, args=(_JOB,), daemon=True).start()
        host = self.headers.get("Host", "localhost")
        loc = "http://%s/eSCL/ScanJobs/%s" % (host, jid)
        return self._empty(201, {"Location": loc})

    def _next_document(self, path):
        jid = path.split("/eSCL/ScanJobs/", 1)[1].split("/", 1)[0]
        job = _JOB
        if job is None or job.id != jid:
            return self._empty(404)
        # block until the scan finishes (eSCL clients poll patiently)
        t0 = time.time()
        while job.state in ("Pending", "Processing") and time.time() - t0 < 300:
            time.sleep(0.25)
        if job.state == "Aborted":
            return self._empty(500)
        if job.delivered or job.data is None:
            _log("NextDocument: nothing more to send -> 404")
            return self._empty(404)          # flatbed: one page, then done
        job.delivered = True
        _log("NextDocument: sending %d bytes (%s)" % (len(job.data), job.ctype))
        self.send_response(200)
        self.send_header("Content-Type", job.ctype)
        self.send_header("Content-Length", str(len(job.data)))
        self.end_headers(); self.wfile.write(job.data)

# ---- discovery (Bonjour) -----------------------------------------------------

def _lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80)); return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()

def advertise(port):
    try:
        from zeroconf import Zeroconf, ServiceInfo
    except ImportError:
        _log("zeroconf not installed - discovery off (pip install zeroconf). HTTP still serves.")
        return None
    ip = _lan_ip()
    info = ServiceInfo(
        "_uscan._tcp.local.",
        "%s._uscan._tcp.local." % MODEL,
        addresses=[socket.inet_aton(ip)], port=port,
        properties={
            "txtvers": "1", "vers": "2.6", "ty": MODEL, "rs": "eSCL",
            "pdl": "image/jpeg,application/pdf",
            "cs": "color,grayscale,binary", "is": "platen",
            "uuid": UUID, "representation": "", "note": "",
        },
        server="canoscan8000f.local.")
    zc = Zeroconf(); zc.register_service(info)
    _log("advertising _uscan._tcp at %s:%d (uuid %s)" % (ip, port, UUID))
    return zc

# ---- main --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="eSCL bridge for CanoScan 8000F")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--no-mdns", action="store_true", help="serve HTTP only, skip Bonjour")
    ap.add_argument("--fake", action="store_true",
                    help="return an instant synthetic image, no hardware (diagnostic)")
    a = ap.parse_args()
    global FAKE
    FAKE = a.fake
    if FAKE: _log("FAKE mode: instant synthetic images, scanner not touched")
    zc = None if a.no_mdns else advertise(a.port)
    httpd = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    _log("eSCL HTTP on 0.0.0.0:%d  (root /eSCL/) - Ctrl-C to stop" % a.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if zc: zc.close()
        httpd.server_close()
        _log("stopped")

if __name__ == "__main__":
    main()
