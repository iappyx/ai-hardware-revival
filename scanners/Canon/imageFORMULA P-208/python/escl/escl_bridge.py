#!/usr/bin/env python3
"""eSCL / AirScan bridge for the Canon imageFORMULA P-208.

Presents the driver as a *driverless* network scanner. macOS, iOS and Linux
discover eSCL scanners without installing anything, so once this runs the P-208
appears natively in Image Capture, Preview, Printers & Scanners and any Mopria
client - the "driver" is this daemon translating eSCL HTTP into driver.scan().

    python3 escl_bridge.py                 # 0.0.0.0:8090, advertised over Bonjour
    python3 escl_bridge.py --port 9000
    python3 escl_bridge.py --no-mdns       # HTTP only, test with curl
    python3 escl_bridge.py --fake          # synthetic pages, scanner untouched

Needs pyusb (via driver.py) and pillow (via imaging.py); `zeroconf` for
discovery. Without zeroconf it still serves eSCL, it just is not advertised.

WHAT DIFFERS FROM A FLATBED BRIDGE
----------------------------------
This scanner has no bed. Two consequences run through the whole file:

  * The input source is the feeder, so the capabilities advertise Adf rather
    than Platen, with a duplex variant. A client that asks for Platen is asking
    for something this hardware does not have.

  * A job produces MANY pages. A flatbed bridge answers one NextDocument and
    then 404s; here the client keeps asking and gets a page each time until the
    tray runs out. Pages are handed over as they are scanned rather than after
    the stack finishes, so a ten-sheet batch starts arriving immediately.

There is no scan region either: what gets scanned is whatever sheet is fed, and
the trimming happens on this side.
"""
import argparse
import io
import os
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

# escl/ lives one level below the driver it wraps.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import imaging                                            # noqa: E402
from driver import P208, ScannerError, NotFound, Busy     # noqa: E402

MODEL = "Canon imageFORMULA P-208"
HOSTNAME = "canonp208.local."
UUID = str(uuid.uuid5(uuid.NAMESPACE_DNS, "canonp208.local"))

# 300 and 600 are scanned natively; the driver maps the rest onto one of those
# and resamples, because the sensor width is a whole number of pixels only at
# 150, 300 and 600, and dpi_y 150 needs the short transfer handling to come
# back whole. 100 is not a supported resolution and is not offered here.
DPIS = [150, 200, 300, 400, 600]

# Sheet limits in eSCL units, which are 1/300 inch. The window is 2552 px at
# 300 dpi, and the feeder takes up to Legal.
MAX_W_300 = 2552
MAX_H_300 = 4200
MIN_300 = 300

_MODE = {"RGB24": "color", "Grayscale8": "gray", "BlackAndWhite1": "lineart"}

SCAN_NS = "http://schemas.hp.com/imaging/escl/2011/05/03"
PWG_NS = "http://www.pwg.org/schemas/2010/12/sm"

_LOCK = threading.Lock()        # serialises hardware access
_STATE = threading.Lock()       # guards _JOB and its transitions
_JOB = None                     # the one current job; the scanner is exclusive
FAKE = False
DEBUG_DIR = None                # --debug-dir: keep what each stage produced

# Whether to look at the paper sensor at all. Reading it means opening and
# claiming the USB device, and clients poll ScannerStatus about once a second.
# Caching that down to once per six seconds was not enough: this scanner is bus
# powered, and repeatedly claiming and releasing it around a scan twice ended
# with the device dropping off the bus mid-feed. Nothing needs this - a client
# that starts a job with an empty tray gets a clear error either way - so the
# default is to claim the device only when actually scanning, and report the
# feeder as ready. --probe-adf turns the real reading back on.
PROBE_ADF = False
_ADF_TTL = 15.0
_adf_cache = {"state": "ScannerAdfLoaded", "at": 0.0}
_MAX_BODY = 256 * 1024


def _log(*a):
    print("[escl]", *a, file=sys.stderr, flush=True)


class Job:
    """One eSCL job, which for a sheet-fed scanner means a whole stack."""

    def __init__(self, jid, dpi, mode, duplex, ctype):
        self.id = jid
        self.dpi = dpi
        self.mode = mode
        self.duplex = duplex
        self.ctype = ctype
        self.autosize = True
        self.state = "Pending"          # Pending -> Processing -> Completed/Aborted
        self.pages = []                 # encoded pages, waiting to be collected
        self.sent = 0
        self.scanned = 0
        self.error = None
        self.created = time.time()
        self.ready = threading.Condition(_STATE)

    def add(self, data):
        with self.ready:
            self.pages.append(data)
            self.scanned += 1
            self.ready.notify_all()

    def finish(self, state, error=None):
        with self.ready:
            if self.state != "Aborted":
                self.state = state
            self.error = error or self.error
            self.ready.notify_all()


def _encode(img, dpi, ctype):
    """One page as bytes in the format the client asked for."""
    im = imaging.to_pil(img)
    if im.mode != "RGB":
        im = im.convert("RGB")          # JPEG cannot hold 1-bit, PDF prefers RGB
    buf = io.BytesIO()
    if ctype == "application/pdf":
        im.save(buf, "PDF", resolution=float(dpi))
    else:
        im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


_DEBUG_N = [0]


def _finish_page(img, mode):
    """The same post-processing the CLI does by default."""
    raw = img
    # Trim first, then straighten. Rotating first fills the corners and puts
    # rotated content into the leading rows that the trim measures the backing
    # from, which collapsed an A4 page to 5.27 x 13.34 in. A bounding box
    # cannot lose the corners it is drawn around.
    img = imaging.autocrop(img)
    cropped = img
    img = imaging.deskew(img)
    straight = img
    img = imaging.tone(img)
    if mode == "lineart":
        img = imaging.binarize(img)
    if DEBUG_DIR:
        _DEBUG_N[0] += 1
        n = _DEBUG_N[0]
        try:
            for tag, stage in (("1raw", raw), ("2crop", cropped),
                               ("3straight", straight), ("4final", img)):
                imaging.save(stage, os.path.join(DEBUG_DIR,
                                                 "page%02d_%s.png" % (n, tag)))
            _log("page %d: raw %s -> crop %s -> straight %s  (skew %+.3f)"
                 % (n, raw.shape[:2], cropped.shape[:2], straight.shape[:2],
                    imaging.find_skew(raw)))
        except Exception as e:
            _log("debug save failed: %s" % e)
    return img


def _fake_pages(job):
    from PIL import Image, ImageDraw
    for n in range(2):
        w = int(8.27 * job.dpi / 4)
        h = int(11.69 * job.dpi / 4)
        im = Image.new("RGB", (w, h), (245, 245, 240))
        d = ImageDraw.Draw(im)
        d.rectangle([4, 4, w - 5, h - 5], outline=(120, 120, 120))
        d.text((w // 4, h // 2), "page %d" % (n + 1), fill=(30, 30, 30))
        buf = io.BytesIO()
        im.save(buf, "PDF", resolution=float(job.dpi)) if job.ctype == \
            "application/pdf" else im.save(buf, "JPEG", quality=90)
        job.add(buf.getvalue())
        time.sleep(0.4)


def _run_job(job):
    job.state = "Processing"
    _log("job %s: %ddpi %s%s -> %s%s"
         % (job.id[:8], job.dpi, job.mode, " duplex" if job.duplex else "",
            job.ctype, "  [FAKE]" if FAKE else ""))
    try:
        if FAKE:
            _fake_pages(job)
            job.finish("Completed")
            _log("job %s: FAKE done, %d page(s)" % (job.id[:8], job.scanned))
            return

        acquire = "color" if job.mode == "lineart" else job.mode
        with _LOCK:
            with P208() as s:
                for sheet in s.scan_batch(dpi=job.dpi, duplex=job.duplex,
                                          mode=acquire,
                                          autosize=job.autosize):
                    for img in sheet:
                        if job.state == "Aborted":
                            raise RuntimeError("canceled by client")
                        # Handed over per page, not per stack: the client can
                        # start writing page one while page two is still being
                        # pulled through the feeder.
                        job.add(_encode(_finish_page(img, job.mode),
                                        job.dpi, job.ctype))
        if not job.scanned:
            raise RuntimeError("no sheets fed")
        job.finish("Completed")
        _log("job %s: done, %d page(s)" % (job.id[:8], job.scanned))
    except (NotFound, Busy, ScannerError, RuntimeError) as e:
        job.finish("Aborted", str(e))
        _log("job %s: FAILED: %s" % (job.id[:8], e))
    except Exception as e:                       # noqa: BLE001 - daemon must survive
        job.finish("Aborted", str(e))
        _log("job %s: FAILED (unexpected): %s" % (job.id[:8], e))


def caps_xml():
    res = "".join(
        "<scan:DiscreteResolution>"
        "<scan:XResolution>%d</scan:XResolution>"
        "<scan:YResolution>%d</scan:YResolution>"
        "</scan:DiscreteResolution>" % (d, d) for d in DPIS)
    profile = (
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
        '</scan:SettingProfile></scan:SettingProfiles>' % res)
    caps = ('<scan:MinWidth>%d</scan:MinWidth><scan:MaxWidth>%d</scan:MaxWidth>'
            '<scan:MinHeight>%d</scan:MinHeight>'
            '<scan:MaxHeight>%d</scan:MaxHeight>'
            '<scan:MaxScanRegions>1</scan:MaxScanRegions>'
            '%s'
            '<scan:MaxOpticalXResolution>600</scan:MaxOpticalXResolution>'
            % (MIN_300, MAX_W_300, MIN_300, MAX_H_300, profile))
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<scan:ScannerCapabilities xmlns:scan="%s" xmlns:pwg="%s">'
            '<pwg:Version>2.6</pwg:Version>'
            '<pwg:MakeAndModel>%s</pwg:MakeAndModel>'
            '<scan:UUID>%s</scan:UUID>'
            '<scan:Adf>'
            '<scan:AdfSimplexInputCaps>%s</scan:AdfSimplexInputCaps>'
            '<scan:AdfDuplexInputCaps>%s</scan:AdfDuplexInputCaps>'
            '<scan:FeederCapacity>10</scan:FeederCapacity>'
            '<scan:AdfOptions>'
            '<scan:AdfOption>DetectPaperLoaded</scan:AdfOption>'
            '<scan:AdfOption>Duplex</scan:AdfOption>'
            '</scan:AdfOptions>'
            '</scan:Adf>'
            '</scan:ScannerCapabilities>'
            % (SCAN_NS, PWG_NS, escape(MODEL), UUID, caps, caps))


def _adf_state(job):
    """Whether paper is loaded, read sparingly.

    Never while a job is live - the scanner is exclusive, and a sensor read
    inside an open scan session is a command-sequence violation anyway - and at
    most once every few seconds otherwise.
    """
    if FAKE or not PROBE_ADF:
        return "ScannerAdfLoaded"
    if job is not None and job.state in ("Pending", "Processing"):
        return _adf_cache["state"]
    now = time.time()
    if now - _adf_cache["at"] < _ADF_TTL:
        return _adf_cache["state"]
    try:
        with _LOCK:
            with P208() as s:
                sen = s.sensors()
        if sen is not None:
            _adf_cache["state"] = ("ScannerAdfLoaded" if sen["paper"]
                                   else "ScannerAdfEmpty")
    except Exception as e:
        _log("sensor read failed, keeping last state: %s" % e)
    _adf_cache["at"] = now
    return _adf_cache["state"]


def status_xml():
    j = _JOB
    # AdfState is what tells a client whether it is worth starting a job at
    # all. It is read outside a scan only; asking the scanner mid-job would be
    # a command-sequence violation, so a running job simply reports loaded.
    adf = _adf_state(j)
    jobs = ""
    if j is not None:
        with _STATE:
            waiting = len(j.pages)
            done = j.sent
            state = j.state
        if state == "Aborted":
            jstate, reason = "Canceled", "JobCanceledByUser"
        elif state == "Completed" and not waiting:
            jstate, reason = "Completed", "JobCompletedSuccessfully"
        else:
            jstate, reason = "Processing", "JobScanningAndTransferring"
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
                % (j.id, j.id, int(time.time() - j.created), done,
                   max(waiting, 1) if jstate == "Processing" else 0,
                   jstate, reason))
    busy = j is not None and j.state in ("Pending", "Processing")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<scan:ScannerStatus xmlns:scan="%s" xmlns:pwg="%s">'
            '<pwg:Version>2.6</pwg:Version>'
            '<pwg:State>%s</pwg:State>'
            '<scan:AdfState>%s</scan:AdfState>'
            '<scan:Jobs>%s</scan:Jobs>'
            '</scan:ScannerStatus>'
            % (SCAN_NS, PWG_NS, "Processing" if busy else "Idle", adf, jobs))


def _tag(el):
    return el.tag.rsplit("}", 1)[-1]


def parse_settings(body):
    """dpi, mode, duplex, content-type from a ScanSettings body."""
    import xml.etree.ElementTree as ET

    low = body.lower()
    if b"<!doctype" in low or b"<!entity" in low:
        raise ValueError("DTD/entities not allowed in ScanSettings")
    root = ET.fromstring(body)
    vals = {}
    for el in root.iter():
        if el.text and el.text.strip():
            vals[_tag(el)] = el.text.strip()
    dpi = int(vals.get("XResolution", "300"))
    if dpi not in DPIS:                       # snap to the nearest we support
        dpi = min(DPIS, key=lambda d: abs(d - dpi))
        _log("resolution %s not offered, using %d"
             % (vals.get("XResolution"), dpi))
    mode = _MODE.get(vals.get("ColorMode", "RGB24"), "color")
    source = vals.get("InputSource", "Feeder")
    duplex = vals.get("Duplex", "false").lower() == "true"
    ctype = "image/jpeg"
    fmt = vals.get("DocumentFormatExt") or vals.get("DocumentFormat")
    if fmt and fmt.lower() == "application/pdf":
        ctype = "application/pdf"
    # eSCL has no notion of the scanner detecting the page itself; a client
    # either names a region or does not. Treat "no region given" as asking the
    # device to size the page, which is what a sheet-fed scanner should do.
    autosize = not any(k in vals for k in ("Width", "Height"))
    if source == "Platen":
        # There is no platen. Saying so is better than quietly feeding a sheet.
        raise ValueError("this scanner has no flatbed; use the feeder")
    return dpi, mode, duplex, ctype, autosize


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def log_request(self, code="-", size="-"):
        _log("%s %s -> %s" % (self.command, self.path, code))

    def _xml(self, body, code=200):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _empty(self, code, headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
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
        if "/eSCL/ScanJobs/" not in self.path:
            return self._empty(404)
        jid = self.path.split("?", 1)[0].rstrip("/").split(
            "/eSCL/ScanJobs/", 1)[1].split("/", 1)[0]
        with _STATE:
            ok = _JOB is not None and _JOB.id == jid
            if ok and _JOB.state != "Aborted":
                _JOB.state = "Aborted"
                _JOB.error = "canceled by client"
                _JOB.ready.notify_all()
        return self._empty(200 if ok else 404)

    def _create_job(self):
        global _JOB
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except (ValueError, TypeError):
            return self._empty(400)
        if n < 0 or n > _MAX_BODY:
            return self._empty(400)
        body = self.rfile.read(n) if n else b""
        try:
            dpi, mode, duplex, ctype, autosize = parse_settings(body)
        except Exception as e:
            _log("bad ScanSettings: %s" % e)
            return self._empty(400)
        jid = uuid.uuid4().hex
        with _STATE:
            if _JOB is not None and _JOB.state in ("Pending", "Processing"):
                return self._empty(503, {"Retry-After": "5"})
            _JOB = Job(jid, dpi, mode, duplex, ctype)
            _JOB.autosize = autosize
            job = _JOB
        threading.Thread(target=_run_job, args=(job,), daemon=True).start()
        host = self.headers.get("Host", "localhost")
        return self._empty(201, {"Location": "http://%s/eSCL/ScanJobs/%s"
                                             % (host, jid)})

    def _next_document(self, path):
        jid = path.split("/eSCL/ScanJobs/", 1)[1].split("/", 1)[0]
        job = _JOB
        if job is None or job.id != jid:
            return self._empty(404)

        # Wait for a page rather than for the stack: pages go out as they come
        # off the scanner. A 404 here is how eSCL says "no more pages", which is
        # what ends the job, so it must only be sent once the job really is over.
        deadline = time.time() + 300
        with job.ready:
            while (not job.pages and job.state in ("Pending", "Processing")
                   and time.time() < deadline):
                job.ready.wait(0.5)
            if job.pages:
                data = job.pages.pop(0)
                job.sent += 1
                more = True
            else:
                data, more = None, False
            aborted = job.state == "Aborted"

        if not more:
            if aborted:
                _log("NextDocument: job aborted -> 500")
                return self._empty(500)
            _log("NextDocument: stack finished after %d page(s) -> 404"
                 % job.sent)
            return self._empty(404)

        _log("NextDocument: page %d, %d bytes (%s)"
             % (job.sent, len(data), job.ctype))
        self.send_response(200)
        self.send_header("Content-Type", job.ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def advertise(port):
    try:
        from zeroconf import ServiceInfo, Zeroconf
    except ImportError:
        _log("zeroconf not installed - discovery off (pip install zeroconf). "
             "HTTP still serves.")
        return None
    ip = _lan_ip()
    info = ServiceInfo(
        "_uscan._tcp.local.",
        "%s._uscan._tcp.local." % MODEL,
        addresses=[socket.inet_aton(ip)], port=port,
        properties={
            "txtvers": "1", "vers": "2.6", "ty": MODEL, "rs": "eSCL",
            "pdl": "image/jpeg,application/pdf",
            "cs": "color,grayscale,binary",
            "is": "adf",                 # feeder, not platen
            "duplex": "T",
            "uuid": UUID, "representation": "", "note": "",
        },
        server=HOSTNAME)
    zc = Zeroconf()
    zc.register_service(info)
    _log("advertising _uscan._tcp at %s:%d (uuid %s)" % (ip, port, UUID))
    return zc


def main():
    ap = argparse.ArgumentParser(description="eSCL bridge for the Canon P-208")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--no-mdns", action="store_true",
                    help="serve HTTP only, skip Bonjour")
    ap.add_argument("--probe-adf", action="store_true",
                    help="read the paper sensor for ScannerStatus. Off by "
                         "default: it claims the USB device on a timer, and "
                         "this scanner is bus powered")
    ap.add_argument("--debug-dir", metavar="DIR",
                    help="write each page at every stage, to see what the "
                         "post-processing actually did")
    ap.add_argument("--fake", action="store_true",
                    help="synthetic pages, scanner untouched (diagnostic)")
    a = ap.parse_args()
    global FAKE, DEBUG_DIR, PROBE_ADF
    FAKE = a.fake
    PROBE_ADF = a.probe_adf
    DEBUG_DIR = a.debug_dir
    if DEBUG_DIR:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        _log("keeping stage images in %s" % DEBUG_DIR)
    if FAKE:
        _log("FAKE mode: synthetic pages, scanner not touched")
    zc = None if a.no_mdns else advertise(a.port)
    httpd = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    _log("eSCL HTTP on 0.0.0.0:%d (root /eSCL/) - Ctrl-C to stop" % a.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if zc:
            zc.close()
        httpd.server_close()
        _log("stopped")


if __name__ == "__main__":
    main()
