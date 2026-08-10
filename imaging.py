#!/usr/bin/env python3
"""Decode a CanoScan 8000F raw scan and export to png / tif / jpg / pdf / raw.

Raw export needs nothing.  Image formats need Pillow (`pip install pillow`);
numpy is used when present for speed and falls back to a pure-Python path.

meta dict keys: dpi, width, lines, channels, depth, lineart, stride, mode
"""
import os, struct

# Canon factory colorimetry (CNS24G.ICC "8000F Scanner Reflective")
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
                ('dpi', 'width', 'lines', 'channels', 'depth', 'lineart', 'stride', 'mode')
                if k in meta))
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
    def bshift(A, B, lim=24):
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
    if bits == 16 and depth == 16:
        u = (np.clip(arr, 0, 1) * 65535.0).astype(np.uint16)
        if mode == 'L':
            return Image.fromarray(u, 'I;16')
        return Image.fromarray(u, 'RGB')          # 16-bit/chan RGB (TIFF)
    u = (np.clip(arr, 0, 1) * 255.0).astype(np.uint8)
    return Image.fromarray(u, mode)

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
    im8 = None; im16 = None
    for f in formats:
        pil = _EXT.get(f)
        if not pil:
            continue
        path = '%s.%s' % (out_base, f)
        # 16-bit preserved for TIFF/PNG; JPEG/PDF are 8-bit
        use16 = want16 and pil in ('TIFF', 'PNG')
        if use16:
            if im16 is None: im16 = to_image(raw, meta, bits=16)
            img = im16
        else:
            if im8 is None: im8 = to_image(raw, meta, bits=8)
            img = im8
        ds = meta.get('downscale', 1)
        if ds and ds > 1 and img.mode != '1':
            from PIL import Image as _I
            img = img.resize((max(1, img.width // ds), max(1, img.height // ds)), _I.LANCZOS)
        elif ds and ds > 1:  # 1-bit: downscale via L then re-threshold
            from PIL import Image as _I
            img = img.convert('L').resize((max(1, img.width // ds), max(1, img.height // ds)), _I.LANCZOS).convert('1')
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
