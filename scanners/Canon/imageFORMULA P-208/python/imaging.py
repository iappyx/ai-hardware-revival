#!/usr/bin/env python3
"""Post-processing for P-208 scans.

Nothing here touches the scanner. Everything operates on a finished image, so
it can be tested, tuned and reordered without hardware - which is the point of
keeping it out of driver.py.

Images are what driver returns: (h, w, 3) uint8 colour, (h, w) uint8 gray, or
(h, w) bool lineart with True meaning ink.
"""
import numpy as np


# ---- analogue front end arithmetic ---------------------------------------
#
# This belongs with the image processing rather than the command layer: these
# functions read captured references and produce register values, and the
# command layer only writes them.
#
# The device measures in 12 bits; the bulk stream carries the top 8, so a
# reading of n is n*16 in these units.

ADC_BITS = 12
DARK_TARGET = 96        # of 4096  ->   6.0 in 8-bit
WHITE_TARGET = 2730     # of 4096  -> 170.6 in 8-bit, two thirds of full scale
GAIN_MAX = 63
OFFSET_MAX = 255
EXPOSURE_MAX = 0x1fff

# The gain stage behaves as A(gain) = k / (GAIN_POLE - gain) over gain 0..63,
# so its range is 79/16 = 4.94x end to end. Both servos below are just that
# law rearranged, which is why (GAIN_POLE - gain) shows up in each.
GAIN_POLE = 79

# One offset step moves the dark reading by OFFSET_SERVO / (GAIN_POLE - gain)
# ADC counts - about 3.9 counts at minimum gain and 19.5 at maximum, since the
# gain stage amplifies the pedestal along with the signal. offset_step inverts
# that to get the step needed for a given error.
#
# NOT measured on this unit. Two dark reads at two different offsets would give
# it directly; until that is done it is a servo constant that converges, not a
# calibrated figure.
OFFSET_SERVO = 311.2937


def offset_step(min12, gain, cur_offset):
    """One offset servo step.

    The correction is proportional to the error and scaled by
    (GAIN_POLE - gain), because the gain stage amplifies the pedestal.
    """
    delta = (min12 - DARK_TARGET) * (GAIN_POLE - gain) / OFFSET_SERVO
    return max(0, min(OFFSET_MAX, int(round(cur_offset - delta))))


def gain_step(max12, cur_gain, target=WHITE_TARGET):
    """One gain servo step; unchanged when the measurement is on target.

    A(new)/A(cur) has to be target/max12, which by the gain law above means
    (GAIN_POLE - new) = (GAIN_POLE - cur) * max12 / target.
    """
    v = GAIN_POLE - int((float(max12) / target) * (GAIN_POLE - cur_gain))
    return max(0, min(GAIN_MAX, v))


def exposure_step(meas12, cur_exposure, target=WHITE_TARGET):
    """One exposure step. Exposure is per channel, so it is what balances the
    channels against each other; gain and offset are per side and cannot."""
    if meas12 < 1:
        meas12 = 1
    return max(1, min(EXPOSURE_MAX, int(cur_exposure * target / meas12)))


def _luminance(img):
    if img.dtype == bool:
        return np.where(img, 0.0, 255.0)
    if img.ndim == 3:
        return img.astype(np.float32).mean(axis=2)
    return img.astype(np.float32)


def sheet_bounds(img, flat_tol=None, margin=2):
    """Trim the scanned window down to the SHEET, not to the content.

    Cropping to where the ink is would discard the page's own margins, which is
    wrong for a document scanner: an A4 sheet should come out A4.

    Measured on this unit, the backing and the paper sit at the *same*
    brightness - both around 230 after shading - so level cannot separate them.
    What differs is flatness:

        backing      std ~0.8   (uniform)
        paper        std ~3.1   (texture, content)
        sheet edge   std ~80    (a dark shadow line, mean drops to ~126)

    So a row or column counts as sheet when it is not flat. The threshold for
    "flat" is DERIVED, not fixed: what counts as flat moves with the pipeline.
    With the factory light curve on, its per-pixel gain varies a few percent
    across the page, which lifts the backing's own row deviation from about
    1.4 to about 6.7 - past the 2.5 that used to be hardcoded here, so the
    backing stopped reading as backing and the top of the page was never
    trimmed. The separation itself is never in doubt (4.8 against 78 on that
    same scan, a factor of 16); only the absolute number moves. Pass
    `flat_tol` to override. The shadow the leading edge casts shows up as a
    large spike and falls inside the sheet either way.

    Hardware crop would be better, but this unit rejects SET SCAN MODE 2
    (opcode 0xe5) and does not answer the page-size query, so the edge has to be
    found here.

    Returns (top, bottom, left, right), or None if the frame is too small to
    judge. Both `autocrop` and `crop_to_size` work from this: one trims to the
    sheet it finds, the other only needs where the sheet STARTS.
    Note that `flat_tol`, if given, is compared against the deviation left
    AFTER the backing profile is subtracted, where backing sits near 0.4 rather
    than the ~1.4 of the raw frame. A value carried over from before that
    change will not mean what it used to.
    """
    lum = _luminance(img)
    h, w = lum.shape
    if h < 64 or w < 64:
        return None
    lum = _luminance(img)
    h, w = lum.shape
    if h < 64 or w < 64:
        return None

    # The backing profile comes from the leading rows - the scan starts before
    # the sheet reaches the sensor, so they are empty belt - but only from the
    # rows that REALLY are backing. A fixed fraction of the frame does not do:
    # if it reaches past the leading edge it takes in paper and print, the
    # reference reads 2.1 instead of 0.3, the threshold triples with it, and
    # blank paper stops counting as sheet. A page with an unprinted foot then
    # gets cropped where its text ends - measured at 7.78 in of an 10.88 in
    # page, three inches thrown away.
    #
    # So find where the backing ends: walk down while rows stay as flat as the
    # first few, and use only those.
    raw = lum.std(axis=1)
    seed = float(np.median(raw[:8])) if h >= 16 else float(raw[0])
    limit = max(3.0 * seed, seed + 0.5)
    lead_n = 8
    for r in range(8, min(h // 3, 400)):
        if raw[r] > limit:
            break
        lead_n = r + 1
    profile = np.median(lum[:lead_n], axis=0)
    flat = lum - profile

    rows = flat.std(axis=1)
    cols = flat.std(axis=0)
    if flat_tol is None:
        back = float(np.median(rows[:lead_n]))
        # Measured after profile subtraction: backing sits near 0.4, blank
        # paper near 3, content far above. Three times the backing separates
        # them with room to spare, and the floor keeps a pathologically clean
        # frame from setting the bar at nothing.
        flat_tol = max(0.8, 3.0 * back)

    def extent(std, n, tol):
        idx = np.where(std > tol)[0]
        if idx.size < n * 0.02:
            return 0, n
        return idx[0], idx[-1] + 1

    r0, r1 = extent(rows, h, flat_tol)

    # Columns need their own threshold. A column outside the sheet is backing
    # for its whole height, and over that distance the backing level drifts a
    # little, so its deviation sits above the row threshold while still being
    # backing. Take the reference from the outermost columns: if the sheet is
    # narrower than the window they are backing, and if it fills the window
    # they are sheet, the threshold comes out high and nothing is trimmed -
    # which is the safe way to be wrong.
    edge_n = max(4, w // 200)
    side = float(np.median(np.concatenate([cols[:edge_n], cols[-edge_n:]])))
    c0, c1 = extent(cols, w, max(flat_tol, 2.5 * side))

    return (max(0, r0 - margin), min(h, r1 + margin),
            max(0, c0 - margin), min(w, c1 + margin))


def autocrop(img, flat_tol=None, margin=2, min_frac=0.15):
    """Trim the scanned window down to the sheet. See `sheet_bounds`."""
    b = sheet_bounds(img, flat_tol=flat_tol, margin=margin)
    if b is None:
        return img
    r0, r1, c0, c1 = b
    h, w = img.shape[:2]
    if (r1 - r0) < h * min_frac or (c1 - c0) < w * min_frac:
        return img
    return img[r0:r1, c0:c1]


def is_blank(img, ink_frac=0.004, drop=50, border=0.03):
    """True if the page carries essentially no content.

    Matters most in duplex: single-sided originals produce a blank back for
    every sheet. The test is the fraction of pixels meaningfully darker than
    the page's own background, so it does not care whether the paper is white,
    cream or grey.

    Two things had to be measured rather than assumed, because with the
    obvious settings every blank back was kept:

    A margin is excluded. The trim leaves a rim of the sheet's own shadow edge,
    which is genuinely dark - on a measured blank back the outer ring ran 23%
    "ink" against 2% inside it, and the bottom edge alone 45%. That rim alone
    was enough to call every page content.

    And `drop` is 50, not the 28 it was. What survives on a blank back is
    show-through - the other side's print, faintly visible through the paper -
    together with creases. Measured on blank backs against their own fronts:

        drop   blank back   printed front
        28     0.014        0.079
        50     0.000        0.064

    28 counts show-through as content; 50 does not, while printed text is far
    above either. The cost is that genuinely faint marking - light pencil - may
    now read as blank, which is why skipping stays opt-in.
    """
    lum = _luminance(img)
    if lum.size == 0:
        return True
    h, w = lum.shape
    by, bx = int(h * border), int(w * border)
    core = lum[by:h - by, bx:w - bx] if (by and bx and h > 2 * by and w > 2 * bx) else lum
    if core.size == 0:
        return True
    bg = np.percentile(core, 92)
    return float((core < bg - drop).mean()) < ink_frac


def to_gray(img):
    """Colour to gray, using luma weights rather than a flat mean."""
    if img.dtype == bool:
        return np.where(img, 0, 255).astype(np.uint8)
    if img.ndim == 2:
        return img
    w = np.array([0.299, 0.587, 0.114], dtype=np.float32)
    return np.clip(img.astype(np.float32) @ w, 0, 255).astype(np.uint8)


def binarize(img, block=64, offset=12):
    """Gray or colour to bitonal, returning a bool array where True is ink.

    This unit has no hardware bitonal mode - INQUIRY reports no monochrome
    capability and it rejects a bitonal window - so binarisation happens here,
    in the imaging layer rather than the command layer.

    Adaptive rather than a single global threshold, because a sheet-fed page is
    rarely lit evenly end to end: compare each pixel against the local
    background estimated over a coarse grid.
    """
    g = to_gray(img).astype(np.float32)
    h, w = g.shape

    # coarse background: the bright quantile of each block, then bilinear-ish
    # expansion back to full size
    by = max(1, h // block)
    bx = max(1, w // block)
    ys = np.linspace(0, h, by + 1).astype(int)
    xs = np.linspace(0, w, bx + 1).astype(int)
    coarse = np.empty((by, bx), np.float32)
    for i in range(by):
        for j in range(bx):
            tile = g[ys[i]:ys[i + 1], xs[j]:xs[j + 1]]
            coarse[i, j] = np.percentile(tile, 80) if tile.size else 255.0

    from PIL import Image
    bg = np.asarray(Image.fromarray(coarse).resize((w, h), Image.BILINEAR),
                    dtype=np.float32)
    return g < (bg - offset)



def tone(img, strength=1.743):
    """Lift the midtones without letting the highlights saturate.

    Shading against the internal white strip alone puts paper past full scale,
    so the factory curve is applied to buy headroom - which leaves plain paper
    around 200 of 255 rather than the 253 it used to read. This wins the
    brightness back:

        out = 1 - (1 - x) ** strength          on x in [0, 1]

    Black stays black, white stays white, and the curve approaches the top
    asymptotically, so bright areas compress instead of clipping. The default
    strength puts white paper near 237. Raising it goes brighter and squeezes
    the highlights harder; 1.0 is a no-op.

    This is the opposite trade from normalising the factory curve, which
    reaches the same brightness by discarding everything above the ceiling -
    measured at 98.5% of a white sheet.
    """
    if strength == 1.0:
        return img
    if img.dtype == bool:
        return img
    x = img.astype(np.float32) / 255.0
    out = (1.0 - (1.0 - x) ** float(strength)) * 255.0
    return np.clip(out, 0, 255).astype(np.uint8)


# Sheet sizes in inches, portrait. The scanner's own maximum width is 8.5 in
# (2552 px at 300 dpi), so anything wider is trimmed by the window, not here.
PAGE_SIZES = {
    'a4':        (8.27, 11.69),
    'a5':        (5.83, 8.27),
    'a6':        (4.13, 5.83),
    'b5':        (6.93, 9.84),
    'letter':    (8.50, 11.00),
    'legal':     (8.50, 14.00),
    'businesscard': (3.54, 2.17),
}


def crop_to_size(img, size, dpi=300, origin='sheet'):
    """Crop to a known sheet size instead of detecting the edges.

    `autocrop` finds the sheet by flatness, which is right when the size is
    unknown but can be fooled - by a sheet running past the window, or one with
    no margin. When the size IS known this is exact.

    The crop is anchored to where the SHEET starts, not to the corner of the
    scanned window. The scan begins before the page reaches the sensor, so the
    window opens with empty belt: measured on an A4 scan the sheet started 173
    rows down, and cropping from the window corner returned 173 rows of backing
    and lost 173 rows off the bottom of the page. `--deskew` makes it worse,
    since rotating moves the sheet further from the corner.

    `size` is a key of PAGE_SIZES or an (inches_wide, inches_tall) pair.
    `origin` of 'sheet' anchors to the detected top-left, 'center' keeps the
    same top edge but centres the width on the sheet, and 'window' restores the
    old corner behaviour. The image is never padded: a page smaller than the
    size asked for comes back as it is, because inventing pixels is worse than
    a short crop.
    """
    dims = PAGE_SIZES.get(size) if isinstance(size, str) else size
    if dims is None:
        raise ValueError('unknown page size %r; known: %s'
                         % (size, ', '.join(sorted(PAGE_SIZES))))
    ih, iw = img.shape[:2]
    want_w = min(int(round(dims[0] * dpi)), iw)
    want_h = min(int(round(dims[1] * dpi)), ih)

    r0 = c0 = 0
    if origin != 'window':
        b = sheet_bounds(img)
        if b is not None:
            r0, _r1, c0, c1 = b
            if origin == 'center':
                c0 = max(0, c0 + ((c1 - c0) - want_w) // 2)

    # Never run off the end: if the sheet sits so low that the full height will
    # not fit below it, take what is there rather than an out-of-range slice.
    r0 = max(0, min(r0, ih - want_h))
    c0 = max(0, min(c0, iw - want_w))
    return img[r0:r0 + want_h, c0:c0 + want_w]


def downsample(img, factor):
    """Average away an integer factor, box-filter style.

    Used to reach resolutions the scanner will not produce properly itself.
    Averaging rather than dropping pixels: the samples being thrown away are
    real signal, and keeping them halves the noise instead of discarding it.
    """
    n = int(factor)
    if n <= 1:
        return img
    h, w = img.shape[:2]
    h, w = (h // n) * n, (w // n) * n
    a = img[:h, :w]
    if a.dtype == bool:
        a = a.astype(np.float32)
        shaped = a.reshape(h // n, n, w // n, n).mean(axis=(1, 3))
        return shaped > 0.5
    if a.ndim == 3:
        shaped = a.reshape(h // n, n, w // n, n, a.shape[2]).mean(axis=(1, 3))
    else:
        shaped = a.reshape(h // n, n, w // n, n).mean(axis=(1, 3))
    return np.clip(shaped, 0, 255).astype(np.uint8)


def rescale_xy(img, fx, fy):
    """Resample the axes independently.

    The scanner can be asked for a different vertical resolution than
    horizontal, so a page often needs shrinking in one axis and not the other -
    resampling an axis that is already right only softens it.
    """
    if abs(fx - 1.0) < 1e-6 and abs(fy - 1.0) < 1e-6:
        return img
    from PIL import Image
    h, w = img.shape[:2]
    ow = max(1, int(round(w * fx)))
    oh = max(1, int(round(h * fy)))
    if img.dtype == bool:
        pil = Image.fromarray((~img).astype(np.uint8) * 255, mode='L')
        return np.asarray(pil.resize((ow, oh), Image.BOX)) < 128
    return np.asarray(Image.fromarray(img).resize((ow, oh), Image.BOX))


def rescale(img, factor):
    """Resample by an arbitrary factor, for resolutions the scanner will not
    produce correctly itself.

    An exact integer ratio is averaged in blocks, which is both faster and
    cleaner than a general filter. Anything else goes through PIL's box filter,
    which is the right one for shrinking - it averages the pixels that fall in
    each output cell rather than sampling a few of them.
    """
    if abs(factor - 1.0) < 1e-6:
        return img
    n = int(round(1.0 / factor))
    if factor < 1 and abs(1.0 / factor - n) < 1e-6:
        return downsample(img, n)

    from PIL import Image
    h, w = img.shape[:2]
    ow, oh = max(1, int(round(w * factor))), max(1, int(round(h * factor)))
    if img.dtype == bool:
        pil = Image.fromarray((~img).astype(np.uint8) * 255, mode='L')
        out = np.asarray(pil.resize((ow, oh), Image.BOX))
        return out < 128
    pil = Image.fromarray(img)
    return np.asarray(pil.resize((ow, oh), Image.BOX))


def brightness_contrast(img, brightness=0, contrast=0):
    """Shift and stretch the tone scale.

    `brightness` is added directly, -100..100 mapping to -128..128 levels.
    `contrast` pivots about mid grey by the same scale, so the pivot stays put
    and only the spread changes. Both are 0 by default, which is a no-op.
    """
    if img.dtype == bool or (not brightness and not contrast):
        return img
    a = img.astype(np.float32)
    if contrast:
        k = 1.0 + float(contrast) / 100.0
        a = (a - 128.0) * k + 128.0
    if brightness:
        a = a + float(brightness) * 1.28
    return np.clip(a, 0, 255).astype(np.uint8)


def gamma(img, value=1.0, channels=None):
    """Apply a gamma curve.

    `value` above 1 lightens the midtones, below 1 darkens them; 1.0 is a
    no-op. `channels` optionally gives a per-channel mapping, e.g.
    {0: 1.1, 2: 0.95} to lift red and pull blue - which is how a colour cast
    is corrected without touching the black and white points, since a gamma
    curve pins both ends.
    """
    if img.dtype == bool:
        return img
    if channels and img.ndim != 3:
        # A single-channel image has no channels to treat separately. Saying so
        # beats accepting the argument and quietly doing nothing, which is how
        # --gamma-rgb behaved on a greyscale scan.
        raise ValueError('per-channel gamma needs a colour image')
    if channels and img.ndim == 3:
        out = img.copy()
        for ch, g in channels.items():
            if g and g != 1.0 and 0 <= ch < img.shape[2]:
                lut = (np.linspace(0, 1, 256) ** (1.0 / float(g)) * 255.0)
                out[..., ch] = np.clip(lut, 0, 255).astype(np.uint8)[img[..., ch]]
        return out
    if value == 1.0:
        return img
    lut = np.clip(np.linspace(0, 1, 256) ** (1.0 / float(value)) * 255.0,
                  0, 255).astype(np.uint8)
    return lut[img]


def rotate(img, degrees=0):
    """Rotate by a multiple of 90 degrees, clockwise as seen on screen.

    Only the right angles: they are exact, needing no resampling, so nothing
    is lost. Arbitrary angles are `deskew`'s job.
    """
    d = int(degrees) % 360
    if d == 0:
        return img
    if d not in (90, 180, 270):
        raise ValueError('rotate takes 0, 90, 180 or 270')
    k = {90: 3, 180: 2, 270: 1}[d]        # np.rot90 turns anticlockwise
    return np.ascontiguousarray(np.rot90(img, k))


def dither(img, drop=28):
    """Floyd-Steinberg error diffusion to 1 bit.

    `binarize` thresholds against a local background, which is the right tool
    for text: it keeps strokes solid. Diffusion instead carries each pixel's
    rounding error into its neighbours, so continuous tone survives as a
    pattern of dots. Better for photographs, worse for small text, which is
    why both exist rather than one replacing the other.

    The diffusion itself is PIL's: error diffusion is sequential in both axes,
    so in Python it would be tens of millions of dependent steps per page.
    `drop` biases the image first, matching `binarize`'s sense - a larger
    value needs a darker pixel before it becomes ink.

    Returns a bool array, True where ink is, matching `binarize`.
    """
    from PIL import Image

    g = to_gray(img).astype(np.int16) + int(drop)
    pil = Image.fromarray(np.clip(g, 0, 255).astype(np.uint8), mode='L')
    return np.asarray(pil.convert('1', dither=Image.FLOYDSTEINBERG)) == 0


def find_skew(img, step=8, span=15, run=5, min_cols=24):
    """Estimate the sheet's skew angle in degrees, positive clockwise.

    Measured from the LEADING edge. The side edges are the better geometry
    when they are visible - they span the whole sheet, so a pixel of edge noise
    buys a much smaller angular error. On this
    unit they usually are not visible: the window is 2552 px (216 mm) against
    A4's 210 mm, so a sheet needs to sit within 3 mm of centre for both sides
    to land inside the frame, and any skew consumes that. Measured on a
    crooked A4: extreme columns read 223-227 against a 230 backing, i.e. no
    edge at all. The leading edge is always in frame, because the scan starts
    before the sheet arrives.

    The edge is found by ROUGHNESS, not brightness. After shading, paper and
    backing sit at the same level - both about 230 - so brightness finds only
    ink. Backing is flat and paper is not: measured across the transition,
    vertical roughness steps from 0.06 on the backing to 1.36 on paper, a 20x
    change, so the threshold has wide margin either side. It is still derived
    from the frame rather than fixed, since a different backing or gain moves
    the floor.
    """
    lum = _luminance(img)
    h, w = lum.shape
    if h < 200 or w < 200:
        return 0.0

    rough = np.abs(np.diff(lum, axis=0))
    floor = float(np.percentile(rough, 5))
    tol = min(1.5, max(0.25, floor * 6.0))

    half = span // 2
    kern = np.ones(run) / run
    xs, ys = [], []
    for x in range(0, w, step):
        col = rough[:, max(0, x - half):x + half + 1].mean(axis=1)
        # demand a sustained run: one speck of dust is not a paper edge
        hits = np.convolve((col > tol).astype(np.float64), kern, mode='same')
        idx = np.where(hits >= 0.99)[0]
        if idx.size:
            xs.append(x)
            ys.append(idx[0])
    if len(xs) < min_cols:
        return 0.0
    xs = np.asarray(xs, dtype=np.float64)
    ys = np.asarray(ys, dtype=np.float64)

    # A sheet already at the top of the frame has no leading edge to fit.
    if np.median(ys) < 4:
        return 0.0

    # Robust fit. Content touching the edge, and the odd artefact column,
    # pull a plain least-squares line badly; reject and refit.
    keep = np.ones(xs.size, dtype=bool)
    for _ in range(6):
        m, c = np.polyfit(xs[keep], ys[keep], 1)
        resid = np.abs(ys - (m * xs + c))
        nxt = resid < max(3.0, 2.5 * np.median(resid[keep]))
        if nxt.sum() < min_cols or np.array_equal(nxt, keep):
            break
        keep = nxt
    if keep.sum() < max(min_cols, 0.4 * xs.size):
        return 0.0
    m, c = np.polyfit(xs[keep], ys[keep], 1)      # refit on the final inliers

    # Accept on either of two counts, because one absolute limit cannot serve
    # both ends of the range. A steep edge sweeps hundreds of rows across the
    # frame, so several pixels of scatter is still a good line; a near-flat
    # edge sweeps almost none, and there only a tight fit means anything.
    res = ys[keep] - (m * xs[keep] + c)
    rms = float(np.sqrt(np.mean(res ** 2)))
    ss_tot = float(np.sum((ys[keep] - ys[keep].mean()) ** 2))
    r2 = 1.0 - float(np.sum(res ** 2)) / ss_tot if ss_tot > 0 else 0.0
    if rms > 4.0 and r2 < 0.95:
        return 0.0

    angle = np.degrees(np.arctan(m))
    return float(angle) if abs(angle) <= 30.0 else 0.0


def deskew(img, angle=None, max_angle=30.0, trim=True):
    """Rotate the sheet upright. Returns the image unchanged below 0.1 degrees,
    which is under the noise floor of the edge fit and not worth resampling.

    `trim` removes the wedges of fill that rotation leaves in the corners. They
    are not merely untidy: the boundary between fill and page is a hard edge,
    and binarising turns it into black triangles - measured at 21 to 27 per cent
    ink in the corners of a page averaging 11.

    The sheet's true size is recoverable. A rectangle (a, b) turned by t has a
    bounding box of (a cos t + b sin t, a sin t + b cos t), and the image being
    straightened IS that bounding box, because the trim ran first and returned
    exactly it. Inverting the pair gives (a, b) back, and cropping to it about
    the centre leaves the page and nothing else.
    """
    if angle is None:
        angle = find_skew(img)
    if abs(angle) < 0.1 or abs(angle) > max_angle:
        return img

    from PIL import Image
    pil = to_pil(img)
    fill = 255 if pil.mode == '1' else int(np.percentile(_luminance(img), 90))
    if pil.mode == 'RGB':
        # PIL reads a bare int as (v, 0, 0) on an RGB image, so a grey fill of
        # 230 came out pure red - a red border round every straightened colour
        # page, and worse, the red then replaced the backing that `autocrop`
        # measures its profile from, so the page stopped being trimmed at all.
        fill = (fill, fill, fill)
    out = pil.rotate(angle, resample=Image.BILINEAR, expand=True, fillcolor=fill)
    a = np.asarray(out)
    if img.dtype == bool:
        a = (a == 0)

    if trim:
        a = _trim_rotation(a, img.shape[:2], angle)
    return a


def _trim_rotation(rotated, box, angle):
    """Crop a straightened page back to the sheet, dropping the fill corners."""
    t = abs(np.radians(angle))
    c, s = np.cos(t), np.sin(t)
    denom = c * c - s * s                     # cos(2t); 45 degrees is the limit
    if denom < 1e-3:
        return rotated
    bh, bw = float(box[0]), float(box[1])
    w = (bw * c - bh * s) / denom
    h = (bh * c - bw * s) / denom
    rh, rw = rotated.shape[:2]
    w = int(round(min(max(w, 1.0), rw)))
    h = int(round(min(max(h, 1.0), rh)))
    x = max(0, (rw - w) // 2)
    y = max(0, (rh - h) // 2)
    return rotated[y:y + h, x:x + w]


def to_pil(img):
    """Convert to a PIL image, including the 1-bit case."""
    from PIL import Image
    if img.dtype == bool:
        return Image.fromarray((~img).astype(np.uint8) * 255).convert('1')
    return Image.fromarray(img)


def save(img, path, dpi=300, quality=92):
    im = to_pil(img)
    fmt = path.rsplit('.', 1)[-1].lower()
    fmt = {'tif': 'TIFF', 'tiff': 'TIFF', 'jpg': 'JPEG', 'jpeg': 'JPEG',
           'png': 'PNG', 'pdf': 'PDF'}[fmt]
    kw = {}
    if fmt in ('PNG', 'TIFF', 'JPEG'):
        kw['dpi'] = (dpi, dpi)
    if fmt == 'JPEG':
        kw['quality'] = quality
        if im.mode not in ('L', 'RGB'):
            im = im.convert('L')
    im.save(path, fmt, **kw)
    return path


# How hard to compress a PDF. Named settings rather than bare numbers, because
# the useful range is narrow and the names say what they are for.
#
#   max        no loss at all - every pixel survives (FlateDecode)
#   high       archival: differences are not visible on paper
#   balanced   the default; what document scanners generally ship with
#   small      email-sized; text stays crisp, photographs soften
PDF_QUALITY = {'max': None, 'high': 92, 'balanced': 80, 'small': 60}
DEFAULT_PDF_QUALITY = 'balanced'


def _pdf_quality(quality):
    """Accept a name or a plain 1..100 number."""
    if isinstance(quality, str):
        if quality not in PDF_QUALITY:
            raise ValueError('pdf quality must be a number or one of %s'
                             % ', '.join(sorted(PDF_QUALITY)))
        return PDF_QUALITY[quality]
    q = int(quality)
    if not 1 <= q <= 100:
        raise ValueError('pdf quality must be between 1 and 100')
    return q


def save_pdf(images, path, dpi=300, quality=DEFAULT_PDF_QUALITY):
    """One multi-page PDF from a list of images.

    The encoding is chosen per page from what the page actually is, which is
    the same policy the mainstream PDF tools use: JPEG for colour and
    greyscale, CCITT Group 4 for black-and-white. G4 is both lossless and
    dramatically smaller on text - a bitonal page saved as G4 measured 127 KB
    against 1374 KB for the same page pushed through RGB first.

    `quality` is a name from PDF_QUALITY or a number from 1 to 100. 'max'
    writes every pixel losslessly and is much larger.
    """
    q = _pdf_quality(quality)
    ims = [to_pil(i) for i in images]
    if not ims:
        raise ValueError('no pages to save')

    if q is None:
        return _save_pdf_lossless(ims, path, dpi)

    # Modes are left alone on purpose: converting a bitonal page to RGB first
    # costs an order of magnitude and gains nothing.
    #
    # A bitonal page goes out as CCITT G4, which is lossless, so `quality` has
    # nothing to act on - and Pillow refuses the argument outright on that
    # path. A document is normally all one kind; in the rare mixed case the
    # bitonal pages are lifted to greyscale so one setting covers the file.
    kinds = {i.mode for i in ims}
    if kinds == {'1'}:
        # Black-and-white cannot be compressed lossily, so quality has nothing
        # to act on and the only question is which lossless scheme is smaller.
        # Measured on a scanned page: zlib 388 KB against CCITT G4's 577 KB.
        # G4 is a fax format from the 1980s and loses to zlib on real pages, so
        # bitonal always takes the lossless path.
        return _save_pdf_lossless(ims, path, dpi)
    if '1' in kinds:
        ims = [i.convert('L') if i.mode == '1' else i for i in ims]
    ims[0].save(path, 'PDF', save_all=True, append_images=ims[1:],
                resolution=dpi, quality=q)
    return path


def _save_pdf_lossless(ims, path, dpi):
    """A PDF whose pages are stored with zlib, so no pixel is altered.

    Written out directly because Pillow always reaches for JPEG on colour and
    greyscale, and there is no way to ask it for a lossless colour page.
    """
    import zlib

    objs = []                       # object bodies, 1-based on output

    def add(body):
        objs.append(body)
        return len(objs)

    catalog = add(b'')              # placeholders, filled in once ids are known
    pages = add(b'')

    kids = []
    for im in ims:
        if im.mode not in ('1', 'L', 'RGB'):
            im = im.convert('RGB')
        w, h = im.size
        if im.mode == '1':
            cs, bpc = b'/DeviceGray', 1
        elif im.mode == 'L':
            cs, bpc = b'/DeviceGray', 8
        else:
            cs, bpc = b'/DeviceRGB', 8

        data = zlib.compress(im.tobytes(), 9)
        img_id = add(
            b'<</Type/XObject/Subtype/Image/Width %d/Height %d/ColorSpace %s'
            b'/BitsPerComponent %d/Filter/FlateDecode/Length %d>>stream\n'
            % (w, h, cs, bpc, len(data)) + data + b'\nendstream'
        )

        # points, at 72 to the inch
        pw = w * 72.0 / dpi
        ph = h * 72.0 / dpi
        content = b'q %.2f 0 0 %.2f 0 0 cm /Im0 Do Q' % (pw, ph)
        cont_id = add(b'<</Length %d>>stream\n' % len(content) + content + b'\nendstream')

        page_id = add(
            b'<</Type/Page/Parent %d 0 R/MediaBox[0 0 %.2f %.2f]'
            b'/Resources<</XObject<</Im0 %d 0 R>>>>/Contents %d 0 R>>'
            % (pages, pw, ph, img_id, cont_id)
        )
        kids.append(page_id)

    objs[catalog - 1] = b'<</Type/Catalog/Pages %d 0 R>>' % pages
    objs[pages - 1] = (b'<</Type/Pages/Count %d/Kids[' % len(kids)
                       + b' '.join(b'%d 0 R' % k for k in kids) + b']>>')

    out = bytearray(b'%PDF-1.4\n')
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b'%d 0 obj' % i + body + b'endobj\n'

    xref = len(out)
    out += b'xref\n0 %d\n' % (len(objs) + 1)
    out += b'0000000000 65535 f \n'
    for off in offsets:
        out += b'%010d 00000 n \n' % off
    out += (b'trailer<</Size %d/Root %d 0 R>>\nstartxref\n%d\n%%%%EOF\n'
            % (len(objs) + 1, catalog, xref))

    with open(path, 'wb') as f:
        f.write(bytes(out))
    return path
