#!/usr/bin/env python3
"""Decode a CanoScan 8000F raw scan and export to png / tif / jpg / pdf / raw.

Raw export needs nothing.  Image formats need Pillow (`pip install pillow`);
numpy is used when present for speed and falls back to a pure-Python path.

meta dict keys: dpi, width, lines, channels, depth, lineart, stride, mode
"""
import os, struct

# Device colorimetry for reflective mode: scanner RGB -> CIE XYZ.
_M_SCN = [[0.6360, 0.2783, 0.0361],
          [0.3294, 0.6920, -0.0214],
          [0.0174, -0.0400, 1.1115]]
# sRGB(D65) primaries inverse * scanner -> combined scanner->linear-sRGB matrix
_C = [[1.5460, -0.1419, -0.4043],
      [0.0024,  1.0267, -0.0290],
      [-0.0134, -0.1680, 1.1812]]

def _have(mod):
    try:
        __import__(mod); return True
    except Exception:
        return False

HAVE_PIL = _have('PIL')
HAVE_NP = _have('numpy')

def save_raw(raw, meta, path):
    with open(path, 'wb') as f:
        f.write(raw)
    with open(path + '.meta', 'w') as f:
        f.write(' '.join('%s=%s' % (k, meta[k]) for k in
                ('dpi', 'scandpi', 'width', 'lines', 'channels', 'depth', 'lineart',
                 'stride', 'mode', 'out_width', 'out_lines')
                if k in meta and meta[k] is not None))
    return path

def _decode_float(raw, meta):
    """Decode -> (arr, mode) where arr is a numpy float array.
    colour: (H,W,3) sRGB-encoded 0..1 ; gray: (H,W) 0..1 (linear sensor)."""
    import numpy as np
    W = meta['width']; ch = meta['channels']; depth = meta['depth']
    if depth == 16:
        d = np.frombuffer(raw, '<u2').astype(np.float32) / 65535.0
    else:
        d = np.frombuffer(raw, np.uint8).astype(np.float32) / 255.0
    stride = W * ch
    L = d.size // stride
    a = d[:L * stride].reshape(L, stride)
    if ch == 1:
        return np.flipud(np.fliplr(a)), 'L'
    l0, l1, l2 = a[:, 0::3], a[:, 1::3], a[:, 2::3]
    dv = lambda x: np.diff(x, axis=0)
    # Inter-channel vertical realignment. The CCD's R/G/B rows are physically
    # offset, so each colour lane lands a fixed number of scan-lines apart and
    # must be shifted back together (three lane buffers) before interleaving.
    # That spacing scales with the Y pitch: ~8/16
    # lines (G/B vs R) at 600-class, but ~16/32 at res-class-0 (native 1200).
    # The search window must clear the largest offset or the correlation locks
    # onto a spurious near peak (measured: B lands at -32 at 1200, but a lim=24
    # window mis-picked -13, leaving the blue channel ~19 lines out -> the heavy
    # colour fringing that read as a "stretched" 1200 scan). Widen the window
    # only for res-class-0; <=600 keeps the original 24 exactly.
    lim = 48 if meta.get('scandpi', meta.get('dpi', 300)) >= 1200 else 24
    lim = min(lim, max(0, L - 2))          # can't search a larger shift than we have lines
    def bshift(A, B, lim=lim):
        if lim <= 0 or A.shape[0] <= 1:
            return 0
        nrm = lambda x: (x - x.mean()) / (x.std() + 1e-9)
        best = (-2.0, 0)
        for s in range(-lim, lim + 1):
            c = (nrm(A[s:]) * nrm(B[:B.shape[0]-s])).mean() if s >= 0 else (nrm(A[:s]) * nrm(B[-s:])).mean()
            if c > best[0]: best = (c, s)
        return best[1]
    s1 = bshift(dv(l0), dv(l1)); s2 = bshift(dv(l0), dv(l2))
    pad = max(0, s1, s2); n = L - pad - max(0, -s1, -s2)
    img = np.stack([l0[pad:pad+n], l1[pad-s1:pad-s1+n], l2[pad-s2:pad-s2+n]], -1)
    C = np.array(_C, np.float32)
    s = np.clip(img @ C.T, 0, 1)
    s = np.where(s <= 0.0031308, 12.92*s, 1.055*np.power(s, 1/2.4) - 0.055)
    s = np.flipud(np.fliplr(s))
    return s, 'RGB'

def to_image(raw, meta, bits=8):
    """Decode raw -> Pillow Image. bits: 8 or 16 (16 only for tiff/png)."""
    if not HAVE_PIL:
        raise RuntimeError("Pillow is required for image export: pip install pillow")
    from PIL import Image
    W = meta['width']; depth = meta['depth']; lineart = meta['lineart']
    if lineart or depth == 1:
        rowb = (W + 7) // 8
        L = len(raw) // rowb
        if HAVE_NP:
            import numpy as np
            a = np.frombuffer(raw[:L * rowb], np.uint8).reshape(L, rowb)
            bitsarr = np.unpackbits(a, axis=1)[:, :W]
            g = np.flipud(np.fliplr((bitsarr * 255).astype(np.uint8)))
            return Image.fromarray(g, 'L').convert('1')
        im = Image.frombytes('1', (rowb * 8, L), bytes(raw[:L * rowb]))
        return im.crop((0, 0, W, L)).transpose(Image.ROTATE_180)
    if not HAVE_NP:
        return _decode_py(raw, meta)   # 8-bit only
    import numpy as np
    arr, mode = _decode_float(raw, meta)
    # Pillow has a single-channel 16-bit mode (I;16) but no 48-bit RGB mode, so 16-bit
    # is preserved here only for GRAY. True 16-bit COLOUR is written straight from the
    # array by export()'s TIFF path; a 16-bit colour request that reaches to_image
    # (PNG/JPEG/PDF/preview) is delivered as 8-bit rather than crashing.
    if bits == 16 and depth == 16 and mode == 'L':
        u = (np.clip(arr, 0, 1) * 65535.0).astype(np.uint16)
        return Image.fromarray(u, 'I;16')
    u = (np.clip(arr, 0, 1) * 255.0).astype(np.uint8)
    return Image.fromarray(u, mode)

def to_array16(raw, meta):
    """Decode to a 16-bit uint array: (H,W,3) 'RGB' or (H,W) 'L' — same sRGB-encoded
    values as the 8-bit path, at full precision. Requires numpy."""
    import numpy as np
    arr, mode = _decode_float(raw, meta)
    return (np.clip(arr, 0, 1) * 65535.0).astype(np.uint16), mode

def _resample_rgb16(u16, tw, th):
    """LANCZOS-resample a uint16 (H,W,3)/(H,W) array via Pillow 'F' mode per band."""
    import numpy as np
    from PIL import Image
    bands = [u16] if u16.ndim == 2 else [u16[:, :, i] for i in range(u16.shape[2])]
    out = [np.clip(np.asarray(
        Image.fromarray(b.astype(np.float32), 'F').resize((tw, th), Image.LANCZOS)),
        0, 65535).astype(np.uint16) for b in bands]
    return out[0] if u16.ndim == 2 else np.stack(out, -1)

def _save_tiff16(u16, path, dpi):
    """Write a minimal baseline (uncompressed, little-endian) 16-bit TIFF from a
    uint16 (H,W,3) RGB or (H,W) gray array. Dependency-free (struct only), so 16-bit
    colour works without tifffile on the target machine."""
    import struct, numpy as np
    if u16.ndim == 2:
        h, w = u16.shape; spp = 1; photo = 1
    else:
        h, w, spp = u16.shape; photo = 2
    data = np.ascontiguousarray(u16).astype('<u2').tobytes()
    SHORT, LONG, RATIONAL = 3, 4, 5
    ntags = 12
    ifd_start = 8
    pos = ifd_start + 2 + ntags * 12 + 4      # first free byte after the IFD
    blobs = b''
    def add(b):
        nonlocal pos, blobs
        off = pos; blobs += b; pos += len(b); return off
    bps = add(struct.pack('<%dH' % spp, *([16] * spp))) if spp > 1 else 16
    xres = add(struct.pack('<II', int(dpi), 1))
    yres = add(struct.pack('<II', int(dpi), 1))
    strip = pos
    def e(t, typ, cnt, val): return struct.pack('<HHII', t, typ, cnt, val)
    ifd = struct.pack('<H', ntags)
    ifd += e(256, LONG, 1, w) + e(257, LONG, 1, h)
    ifd += e(258, SHORT, spp, bps) + e(259, SHORT, 1, 1) + e(262, SHORT, 1, photo)
    ifd += e(273, LONG, 1, strip) + e(277, SHORT, 1, spp)
    ifd += e(278, LONG, 1, h) + e(279, LONG, 1, len(data))
    ifd += e(282, RATIONAL, 1, xres) + e(283, RATIONAL, 1, yres) + e(296, SHORT, 1, 2)
    ifd += struct.pack('<I', 0)
    with open(path, 'wb') as fh:
        fh.write(struct.pack('<2sHI', b'II', 42, ifd_start))
        fh.write(ifd); fh.write(blobs); fh.write(data)
    return path

def _target_dims(w, h, ow, ol, ds):
    if ow and ol: return int(ow), int(ol)
    if ds and ds > 1: return max(1, w // ds), max(1, h // ds)
    return None, None

def _decode_py(raw, meta):
    """Pure-Python 8-bit fallback (no numpy)."""
    from PIL import Image
    W = meta['width']; ch = meta['channels']; depth = meta['depth']
    if depth == 16:
        vals = struct.unpack('<%dH' % (len(raw)//2), raw[:len(raw)//2*2])
        data = bytes(min(255, v >> 8) for v in vals)
    else:
        data = bytes(raw)
    stride = W * ch
    L = len(data) // stride
    data = data[:L * stride]
    mode = 'L' if ch == 1 else 'RGB'
    return Image.frombytes(mode, (W, L), data).transpose(Image.ROTATE_180)

_EXT = {'png': 'PNG', 'tif': 'TIFF', 'tiff': 'TIFF', 'jpg': 'JPEG',
        'jpeg': 'JPEG', 'pdf': 'PDF'}

def export(raw, meta, out_base, formats):
    """Write out_base.<fmt> for each fmt. Returns list of written paths."""
    written = []
    formats = [f.lower().lstrip('.') for f in formats]
    if 'raw' in formats:
        written.append(save_raw(raw, meta, out_base + '.raw'))
        formats = [f for f in formats if f != 'raw']
    if not formats:
        return written
    if not raw:
        return written  # empty capture: nothing to encode (raw already handled above)
    dpi = meta['dpi']
    want16 = (meta['depth'] == 16)
    is_color = meta.get('channels', 3) == 3
    ow = meta.get('out_width'); ol = meta.get('out_lines'); ds = meta.get('downscale', 1)
    # Region scans crop both axes in software, in the decoded (final/preview) frame —
    # exactly where the user drew the box — before any resample. Full scans no-op.
    cx0 = meta.get('crop_x0'); cx1 = meta.get('crop_x1')
    cy0 = meta.get('crop_y0'); cy1 = meta.get('crop_y1')
    caplines = meta.get('lines')
    def _crop(im):
        # crop_y* are in the CAPTURED frame; the decode trims channel-realign rows off the
        # top, so shift the Y crop up by that trim (= captured lines - decoded height).
        trim = (caplines - im.height) if caplines else 0
        x0 = min(max(cx0 or 0, 0), im.width)
        x1 = min(cx1 if cx1 is not None else im.width, im.width)
        y0 = min(max((cy0 or 0) - trim, 0), im.height)
        y1 = min((cy1 - trim) if cy1 is not None else im.height, im.height)
        if x1 <= x0 or y1 <= y0:              # truncated capture / degenerate box -> no crop
            return im
        if (x0, y0, x1, y1) != (0, 0, im.width, im.height):
            return im.crop((x0, y0, x1, y1))
        return im
    im8 = None; imgray16 = None
    for f in formats:
        pil = _EXT.get(f)
        if not pil:
            continue
        path = '%s.%s' % (out_base, f)
        # True 16-bit COLOUR only round-trips through TIFF (Pillow has no 48-bit RGB
        # mode) — write it straight from the array. 16-bit GRAY uses Pillow's I;16.
        # 16-bit colour to PNG/JPEG/PDF is delivered as 8-bit (no silent crash).
        if want16 and is_color and pil == 'TIFF' and HAVE_NP:
            u16, _m = to_array16(raw, meta)
            h0, w0 = u16.shape[0], u16.shape[1]
            trim = (meta.get('lines') - h0) if meta.get('lines') else 0
            xa = min(max(cx0 or 0, 0), w0); xb = min(cx1 if cx1 is not None else w0, w0)
            ya = min(max((cy0 or 0) - trim, 0), h0); yb = min((cy1 - trim) if cy1 is not None else h0, h0)
            if xb > xa and yb > ya and (xa, ya, xb, yb) != (0, 0, w0, h0):
                u16 = u16[ya:yb, xa:xb]
            tw, th = _target_dims(u16.shape[1], u16.shape[0], ow, ol, ds)
            if tw and (tw, th) != (u16.shape[1], u16.shape[0]):
                u16 = _resample_rgb16(u16, tw, th)
            _save_tiff16(u16, path, dpi); written.append(path); continue
        use16 = want16 and pil in ('TIFF', 'PNG') and not is_color   # 16-bit GRAY only
        if use16:
            if imgray16 is None: imgray16 = _crop(to_image(raw, meta, bits=16))
            img = imgray16
        else:
            if im8 is None: im8 = _crop(to_image(raw, meta, bits=8))
            img = im8
        # Resample to the requested output size. `out_width`/`out_lines` (exact
        # target dims) are set when the requested dpi is not a native rung and was
        # scanned at the next rung up (e.g. 400 dpi scanned at 600, resampled ×2/3).
        # `downscale` (integer factor) is the older path, still honoured.
        from PIL import Image as _I
        ow = meta.get('out_width'); ol = meta.get('out_lines')
        ds = meta.get('downscale', 1)
        if ow and ol:
            tw, th = int(ow), int(ol)
        elif ds and ds > 1:
            tw, th = max(1, img.width // ds), max(1, img.height // ds)
        else:
            tw = th = None
        if tw and (tw, th) != (img.width, img.height):
            if img.mode == '1':   # 1-bit: resample in L, then hard re-threshold (no dither)
                img = img.convert('L').resize((tw, th), _I.LANCZOS).convert('1', dither=_I.NONE)
            else:
                img = img.resize((tw, th), _I.LANCZOS)
        if pil == 'JPEG' and img.mode == '1': img = img.convert('L')
        if pil == 'PDF' and img.mode not in ('RGB', 'L', '1'): img = img.convert('RGB')
        kw = {'dpi': (dpi, dpi)}
        if pil == 'TIFF': kw['compression'] = 'tiff_adobe_deflate'
        if pil == 'JPEG': kw['quality'] = 92
        try:
            img.save(path, pil, **kw)
        except Exception:
            img.convert('RGB' if img.mode not in ('L','1') else img.mode).save(path, pil)
        written.append(path)
    return written


def preview_image(raw, meta, target_w=460):
    """Fast, rough live-preview thumbnail of the lines received so far, rendered
    into the FULL page frame so the aspect ratio is correct and the image builds
    up from the bottom (matching the scan direction). Heavily subsampled to stay
    fast enough not to stall the read pipe."""
    if not HAVE_PIL or not HAVE_NP:
        return None
    import numpy as np
    from PIL import Image
    W = meta['width']; ch = meta.get('channels', 3); depth = meta.get('depth', 8)
    lineart = meta.get('lineart', 0)
    total = meta.get('total_lines') or 0

    # bytes per line
    if lineart or depth == 1:
        stride = (W + 7) // 8
    else:
        stride = W * ch * (2 if depth == 16 else 1)
    L = len(raw) // stride
    if L < 2:
        return None
    Hfull = max(total, L)
    step = max(1, Hfull // 700)          # scale off FULL height -> stable, no rescaling as it grows
    cstep = max(1, W // target_w)
    Lsub = max(1, L // step)

    # decode the partial rows [0:L] (subsampled) into an 8-bit array `part`
    if lineart or depth == 1:
        rowb = stride
        a = np.frombuffer(raw, np.uint8, count=L * rowb).reshape(L, rowb)[:Lsub * step:step]
        part = (np.unpackbits(a, axis=1)[:, :W][:, ::cstep] * 255).astype(np.uint8)
        color = False
    elif ch == 1:
        if depth == 16:
            d = np.frombuffer(raw, '<u2', count=L * W).reshape(L, W)[:Lsub*step:step][:, ::cstep].astype(np.float32) / 257.0
        else:
            d = np.frombuffer(raw, np.uint8, count=L * W).reshape(L, W)[:Lsub*step:step][:, ::cstep].astype(np.float32)
        part = np.clip(d, 0, 255).astype(np.uint8)
        color = False
    else:
        if depth == 16:
            a = np.frombuffer(raw, '<u2', count=L * W * 3).reshape(L, W * 3)[:Lsub*step:step].astype(np.float32) / 65535.0
        else:
            a = np.frombuffer(raw, np.uint8, count=L * stride).reshape(L, W * 3)[:Lsub*step:step].astype(np.float32) / 255.0
        img = np.stack([a[:, 0::3][:, ::cstep], a[:, 1::3][:, ::cstep], a[:, 2::3][:, ::cstep]], -1)
        s = np.clip(img @ np.array(_C, np.float32).T, 0, 1)
        s = np.where(s <= 0.0031308, 12.92 * s, 1.055 * np.power(s, 1 / 2.4) - 0.055)
        part = (s * 255).astype(np.uint8)
        color = True

    Lsub = part.shape[0]
    Wsub = part.shape[1]
    Hsub = max(Lsub, Hfull // step)
    # full-page frame, scanned rows at the TOP in raw order; ROTATE_180 then puts
    # them at the BOTTOM (correct final orientation) so it fills upward.
    if color:
        frame = np.zeros((Hsub, Wsub, 3), np.uint8)
    else:
        frame = np.zeros((Hsub, Wsub), np.uint8)
    frame[:Lsub] = part
    im = Image.fromarray(frame, 'RGB' if color else 'L').transpose(Image.ROTATE_180)
    w, h = im.size
    if w != target_w:
        im = im.resize((target_w, max(1, h * target_w // w)))
    return im
