#!/usr/bin/env python3
"""Tkinter front end for the Canon imageFORMULA P-208.

A sheet-fed scanner has no bed to preview and no region to select, so this is
deliberately not the flatbed GUI: the controls are what you set before feeding
paper, and the canvas shows what came out.
"""
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk

import imaging
from driver import P208, ScannerError, NotFound, Busy

# 150, 200 and 400 are scanned at 300 or 600 and resampled - the device
# truncates at dpi_y 150 and shears at 200 and 400. See SCAN_PLAN in
# driver.py. 100 is not a supported resolution and is left out.
DPIS = (150, 200, 300, 400, 600)

# One restrained palette, used everywhere. Flat colour with a lot of white and
# a single accent reads as calm; boxes inside boxes read as a control panel.
INK        = '#1d1d1f'
INK_SOFT   = '#6e6e73'
INK_FAINT  = '#8e8e93'
LINE       = '#e3e3e6'
PANEL      = '#ffffff'
STAGE      = '#f0f0f3'
ACCENT     = '#0071e3'
ACCENT_DIM = '#4a9bea'
DANGER     = '#d7263d'


class RoundButton(tk.Canvas):
    """A pill button. Tk's own button cannot be tinted on macOS, and an app
    with no primary action reads as a form rather than a tool."""

    def __init__(self, parent, text, command, kind='primary', width=132,
                 height=34, **kw):
        bg = kw.pop('bg', PANEL)
        super().__init__(parent, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, **kw)
        self.command, self.kind, self._on = command, kind, True
        self.w, self.h, self.text = width, height, text
        self.bind('<Button-1>', self._hit)
        self.bind('<Enter>', lambda _e: self._draw(True))
        self.bind('<Leave>', lambda _e: self._draw(False))
        self._draw(False)

    def _pill(self, x0, y0, x1, y1, r, fill, outline=''):
        self.create_arc(x0, y0, x0 + 2 * r, y1, start=90, extent=180,
                        fill=fill, outline=outline or fill)
        self.create_arc(x1 - 2 * r, y0, x1, y1, start=270, extent=180,
                        fill=fill, outline=outline or fill)
        self.create_rectangle(x0 + r, y0, x1 - r, y1, fill=fill,
                              outline=outline or fill)

    def _draw(self, hover):
        self.delete('all')
        r = self.h // 2
        if self.kind == 'primary':
            fill = (ACCENT_DIM if hover else ACCENT) if self._on else '#c7c7cc'
            fg = 'white'
        else:
            fill = '#e8e8ed' if hover and self._on else '#f2f2f5'
            fg = INK if self._on else '#b0b0b5'
        self._pill(1, 1, self.w - 1, self.h - 1, r, fill)
        self.create_text(self.w // 2, self.h // 2, text=self.text, fill=fg,
                         font=('', 13, 'bold' if self.kind == 'primary' else 'normal'))

    def _hit(self, _e):
        if self._on and self.command:
            self.command()

    def enable(self, on=True):
        self._on = bool(on)
        self._draw(False)

    def set_text(self, t):
        self.text = t
        self._draw(False)
FORMATS = ('png', 'tif', 'jpg', 'pdf')
MODES = ('color', 'gray')


class App:
    def __init__(self, root):
        self.root = root
        root.title('P-208')
        root.minsize(1000, 700)
        self.q = queue.Queue()
        self.pages = []          # [[front, back], ...]
        self.dropped = set()     # (sheet, side) pairs excluded from the save
        self.turned = {}         # (sheet, side) -> extra rotation, degrees
        self.picked = None       # selected side of the sheet on screen
        self.live = None         # partial page, while a sheet is being read
        self.shown = 0
        self.busy = False

        style = ttk.Style()
        try:
            style.theme_use('clam')           # the only theme that takes colour
        except tk.TclError:
            pass
        root.configure(bg=PANEL)
        style.configure('.', background=PANEL, foreground=INK,
                        fieldbackground=PANEL, borderwidth=0, focuscolor=PANEL)
        style.configure('TCheckbutton', background=PANEL, foreground=INK,
                        font=('', 13))
        style.map('TCheckbutton',
                  background=[('active', PANEL)],
                  indicatorcolor=[('selected', ACCENT), ('!selected', '#ffffff')])
        style.configure('TCombobox', fieldbackground='#f2f2f5',
                        background='#f2f2f5', arrowcolor=INK_SOFT,
                        bordercolor=LINE, lightcolor='#f2f2f5',
                        darkcolor='#f2f2f5', padding=4)
        style.map('TCombobox', fieldbackground=[('readonly', '#f2f2f5')])
        style.configure('TEntry', fieldbackground='#f2f2f5', bordercolor=LINE,
                        lightcolor='#f2f2f5', darkcolor='#f2f2f5', padding=5)
        style.configure('Horizontal.TScale', background=PANEL,
                        troughcolor='#e3e3e6')
        style.configure('Section.TLabel', foreground=INK_FAINT,
                        background=PANEL, font=('', 10, 'bold'))
        style.configure('Field.TLabel', foreground=INK, background=PANEL,
                        font=('', 13))
        style.configure('Value.TLabel', foreground=INK_FAINT, background=PANEL,
                        font=('', 11))
        style.configure('Status.TLabel', foreground=INK_SOFT, background=PANEL,
                        font=('', 12))
        style.configure('Sep.TFrame', background=LINE)

        self.dpi = tk.IntVar(value=300)
        self.duplex = tk.BooleanVar(value=True)
        self.crop = tk.BooleanVar(value=True)
        self.skip_blank = tk.BooleanVar(value=False)
        self.bitonal = tk.BooleanVar(value=False)
        self.deskew = tk.BooleanVar(value=False)
        self.light_curve = tk.BooleanVar(value=True)
        self.tone = tk.BooleanVar(value=True)
        self.dropout = tk.StringVar(value='none')
        self.brightness = tk.IntVar(value=0)
        self.contrast = tk.IntVar(value=0)
        self.gamma = tk.DoubleVar(value=1.0)
        self.rotate = tk.StringVar(value='0')
        self.dither = tk.BooleanVar(value=False)
        self.page_size = tk.StringVar(value='auto')
        self.autosize = tk.BooleanVar(value=True)
        self.continuous = tk.BooleanVar(value=False)
        self.pdf_quality = tk.StringVar(value=imaging.DEFAULT_PDF_QUALITY)
        self.mode = tk.StringVar(value='color')
        self.fmt = tk.StringVar(value='png')
        self.basename = tk.StringVar(value='scan')
        self.outdir = tk.StringVar(value=os.path.expanduser('~/Desktop'))

        # ---- sidebar -------------------------------------------------------
        side = tk.Frame(root, bg=PANEL, width=308)
        side.grid(row=0, column=0, sticky='ns')
        side.grid_propagate(False)
        inner = tk.Frame(side, bg=PANEL)
        inner.pack(fill='both', expand=True, padx=24, pady=22)
        inner.columnconfigure(1, weight=1)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)
        self._r = 0

        def section(title, first=False):
            if not first:
                sep = ttk.Frame(inner, height=1, style='Sep.TFrame')
                sep.grid(row=self._r, column=0, columnspan=3, sticky='ew',
                         pady=(18, 0))
                self._r += 1
            ttk.Label(inner, text=title.upper(), style='Section.TLabel').grid(
                row=self._r, column=0, columnspan=3, sticky='w',
                pady=(14 if not first else 0, 8))
            self._r += 1

        def combo(label, var, values, width=10):
            ttk.Label(inner, text=label, style='Field.TLabel').grid(
                row=self._r, column=0, sticky='w', pady=3)
            ttk.Combobox(inner, textvariable=var, values=values, width=width,
                         state='readonly').grid(row=self._r, column=1,
                                                columnspan=2, sticky='e', pady=3)
            self._r += 1

        def check(label, var, indent=0):
            w = ttk.Checkbutton(inner, text=label, variable=var)
            w.grid(row=self._r, column=0, columnspan=3, sticky='w', pady=3,
                   padx=(indent, 0))
            self._r += 1
            return w

        def scale(label, var, lo, hi, fmt='%d'):
            ttk.Label(inner, text=label, style='Field.TLabel').grid(
                row=self._r, column=0, sticky='w', pady=(4, 0))
            val = ttk.Label(inner, text=fmt % var.get(), style='Value.TLabel',
                            width=5, anchor='e')
            val.grid(row=self._r, column=2, sticky='e', pady=(4, 0))
            ttk.Scale(inner, from_=lo, to=hi, variable=var, orient='horizontal',
                      command=lambda _v, l=val, v=var, f=fmt:
                          l.config(text=f % v.get())).grid(
                row=self._r, column=1, sticky='ew', padx=(10, 8), pady=(4, 0))
            self._r += 1

        section('Scan', first=True)
        combo('Resolution', self.dpi, DPIS, 8)
        combo('Colour', self.mode, MODES, 8)
        check('Both sides', self.duplex)
        check('Skip blank pages', self.skip_blank)

        section('Page')
        check('Trim to sheet', self.crop)
        check('Scanner detects size', self.autosize)
        check('Stack as one long image', self.continuous)
        combo('Size', self.page_size,
              ('auto',) + tuple(sorted(imaging.PAGE_SIZES)), 12)
        check('Straighten', self.deskew)
        combo('Rotate', self.rotate, ('0', '90', '180', '270'), 8)

        section('Image')
        check('Brighten', self.tone)
        check('Factory curve', self.light_curve)
        scale('Bright', self.brightness, -100, 100)
        scale('Contrast', self.contrast, -100, 100)
        scale('Gamma', self.gamma, 0.4, 2.5, '%.2f')
        combo('Drop colour', self.dropout, ('none', 'red', 'green', 'blue'), 8)
        check('Black & white', self.bitonal)
        # Dither only means anything once the page is going to one bit, and a
        # checkbox that silently does nothing is worse than one you cannot
        # reach - so it follows Black & white rather than sitting beside it.
        self.dither_box = check('Dither (photos)', self.dither, indent=18)

        def _dither_state(*_a):
            on = self.bitonal.get()
            self.dither_box.state(['!disabled'] if on else ['disabled'])
            if not on:
                self.dither.set(False)
        self.bitonal.trace_add('write', _dither_state)
        _dither_state()

        section('Save')
        ttk.Label(inner, text='Name', style='Field.TLabel').grid(
            row=self._r, column=0, sticky='w', pady=3)
        ttk.Entry(inner, textvariable=self.basename, width=14).grid(
            row=self._r, column=1, columnspan=2, sticky='e', pady=3)
        self._r += 1
        combo('Format', self.fmt, FORMATS, 8)
        # Only meaningful for PDF; black-and-white pages are lossless whatever
        # this says, because they cannot be compressed lossily at all.
        combo('PDF quality', self.pdf_quality,
              ('max', 'high', 'balanced', 'small'), 10)
        dest = tk.Frame(inner, bg=PANEL)
        dest.grid(row=self._r, column=0, columnspan=3, sticky='ew', pady=(3, 0))
        dest.columnconfigure(0, weight=1)
        ttk.Entry(dest, textvariable=self.outdir).grid(row=0, column=0, sticky='ew')
        RoundButton(dest, '\u2026', self.pick, kind='ghost', width=34,
                    height=28).grid(row=0, column=1, padx=(6, 0))
        self._r += 1

        inner.rowconfigure(self._r, weight=1)
        self._r += 1
        act = tk.Frame(inner, bg=PANEL)
        act.grid(row=self._r, column=0, columnspan=3, sticky='ew', pady=(18, 0))
        act.columnconfigure(0, weight=1)
        act.columnconfigure(1, weight=1)
        self.scan_btn = RoundButton(act, 'Scan', self.start, 'primary', width=124)
        self.scan_btn.grid(row=0, column=0, sticky='ew', padx=(0, 5))
        self.save_btn = RoundButton(act, 'Save', self.save, 'ghost', width=124)
        self.save_btn.grid(row=0, column=1, sticky='ew', padx=(5, 0))
        self.save_btn.enable(False)

        # ---- stage ---------------------------------------------------------
        right = tk.Frame(root, bg=STAGE)
        right.grid(row=0, column=1, sticky='nsew')
        right.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)

        self.canvas = tk.Canvas(right, width=780, height=640, bg=STAGE,
                                highlightthickness=0, bd=0)
        self.canvas.grid(row=0, column=0, sticky='nsew')

        bar = tk.Frame(right, bg=PANEL, height=52)
        bar.grid(row=1, column=0, sticky='ew')
        bar.grid_propagate(False)
        strip = tk.Frame(bar, bg=PANEL)
        strip.pack(fill='both', expand=True, padx=18)
        self.prev_btn = RoundButton(strip, '\u2039', lambda: self.step(-1),
                                    kind='ghost', width=36, height=28)
        self.prev_btn.pack(side='left', pady=12)
        self.page_lbl = ttk.Label(strip, text='', style='Status.TLabel',
                                  width=8, anchor='center')
        self.page_lbl.pack(side='left', padx=8)
        self.next_btn = RoundButton(strip, '\u203a', lambda: self.step(1),
                                    kind='ghost', width=36, height=28)
        self.next_btn.pack(side='left')
        self.ccw_btn = RoundButton(strip, '\u21ba', lambda: self.turn(-90),
                                   kind='ghost', width=36, height=28)
        self.ccw_btn.pack(side='left', padx=(16, 0))
        self.cw_btn = RoundButton(strip, '\u21bb', lambda: self.turn(90),
                                  kind='ghost', width=36, height=28)
        self.cw_btn.pack(side='left', padx=(4, 0))
        self.drop_btn = RoundButton(strip, 'Delete', self.toggle_current,
                                    kind='ghost', width=92, height=28)
        self.drop_btn.pack(side='left', padx=(12, 0))
        self.status = ttk.Label(strip, text='ready', style='Status.TLabel')
        self.status.pack(side='left', padx=(18, 0))

        self.canvas.bind('<Configure>', lambda _e: self.show())
        self.canvas.bind('<Button-1>', self.on_click)
        self.root.after(50, self.show)
        self.root.after(100, self.pump)

    # ---- actions ---------------------------------------------------------

    def pick(self):
        d = filedialog.askdirectory(initialdir=self.outdir.get())
        if d:
            self.outdir.set(d)

    def start(self):
        if self.busy:
            return
        self.busy = True
        self.scan_btn.enable(False)
        self.save_btn.enable(False)
        self.say('scanning...')
        threading.Thread(target=self.work, daemon=True).start()

    def work(self):
        dpi, duplex = self.dpi.get(), self.duplex.get()
        crop, skip, mode = self.crop.get(), self.skip_blank.get(), self.mode.get()
        bitonal, deskew = self.bitonal.get(), self.deskew.get()
        curve, tone = self.light_curve.get(), self.tone.get()
        drop = {'none': 0, 'red': 1, 'green': 2, 'blue': 3}[self.dropout.get()]
        autosz, cont = self.autosize.get(), self.continuous.get()
        bright, contr = int(self.brightness.get()), int(self.contrast.get())
        gam, rot = float(self.gamma.get()), int(self.rotate.get())
        dith, psize = self.dither.get(), self.page_size.get()
        try:
            with P208() as s:
                self.q.put(('status', 'calibrating...'))
                # Always the batch path. It handles one sheet as happily as
                # twenty - it stops when the tray empties - so there is nothing
                # for a second mode to buy, and a mode you can forget to set
                # only ever costs you the rest of the stack.
                self.q.put(('status', 'feeding...'))
                pages = []
                for imgs in s.scan_batch(dpi=dpi, duplex=duplex, mode=mode,
                                         light_curve=curve,
                                         dropout=(drop, drop),
                                         autosize=autosz,
                                         continuous=cont,
                                         on_preview=self.on_preview):
                    pages.append(imgs)
                    self.q.put(('status', '%d sheet(s)...' % len(pages)))
                if not pages:
                    raise ScannerError('no sheets fed')
            # post-processing is off-device, so it happens here rather than
            # in the driver.
            out = []
            for imgs in pages:
                keep = []
                for img in imgs:
                    # geometry on the scanner's own levels, and trimmed
                    # before straightening - see scan.py for why that order
                    if psize != 'auto':
                        img = imaging.crop_to_size(img, psize, dpi=dpi)
                    elif crop:
                        img = imaging.autocrop(img)
                    if deskew:
                        img = imaging.deskew(img)
                    if tone:
                        img = imaging.tone(img)
                    if abs(gam - 1.0) > 0.01:
                        img = imaging.gamma(img, gam)
                    if bright or contr:
                        img = imaging.brightness_contrast(img, bright, contr)
                    if rot:
                        img = imaging.rotate(img, rot)
                    if bitonal:
                        img = imaging.dither(img) if dith else imaging.binarize(img)
                    if skip and imaging.is_blank(img):
                        continue
                    keep.append(img)
                if keep:
                    out.append(keep)
            self.q.put(('done', out))
        except (NotFound, Busy, ScannerError) as e:
            self.q.put(('error', str(e)))
        except Exception as e:
            self.q.put(('error', '%s: %s' % (type(e).__name__, e)))

    def save(self):
        if not self.pages:
            return
        d, fmt = self.outdir.get(), self.fmt.get()
        keep = self.kept()
        if not keep:
            self.say('every page is deleted - nothing to save')
            return
        base = (self.basename.get() or 'scan').strip()
        # keep it a filename, not a path
        base = ''.join(c for c in base if c not in '/\\:') or 'scan'
        flat = [img for _pi, _si, img in keep]
        try:
            if fmt == 'pdf':
                path = imaging.save_pdf(flat, os.path.join(d, base + '.pdf'),
                                        quality=self.pdf_quality.get(),
                                        dpi=self.dpi.get())
                self.say('saved %s (%d page(s))' % (path, len(flat)))
                return
            n = 0
            for pi, si, img in keep:
                parts = [base]
                if len(self.pages) > 1:
                    parts.append('%03d' % (pi + 1))
                if len(self.pages[pi]) > 1:
                    parts.append(['front', 'back'][si])
                imaging.save(img, os.path.join(d, '_'.join(parts) + '.' + fmt),
                             dpi=self.dpi.get())
                n += 1
            dropped = len(self.dropped)
            self.say('saved %d file(s) to %s%s'
                     % (n, d, ' (%d deleted)' % dropped if dropped else ''))
        except Exception as e:
            self.say('save failed: %s' % e)

    # ---- display ---------------------------------------------------------

    def toggle(self, sheet, side):
        """Drop a side from the save, or put it back.

        Nothing is thrown away - the pages stay in memory and the preview keeps
        showing them, greyed. Deleting by removing entries would mean either
        losing a page to a mis-click or building an undo stack for it; marking
        costs neither.
        """
        key = (sheet, side)
        self.dropped.discard(key) if key in self.dropped else self.dropped.add(key)
        self.show()

    def toggle_current(self):
        """Delete or restore the selected side, or the whole sheet."""
        if not self.pages:
            return
        page = self.pages[self.shown]
        keys = ({(self.shown, self.picked)} if self.picked is not None
                else {(self.shown, i) for i in range(len(page))})
        if keys <= self.dropped:
            self.dropped -= keys
        else:
            self.dropped |= keys
        self.show()

    def on_click(self, ev):
        """Select a side, or clear the selection by clicking the stage.

        Selecting rather than acting: Delete and the two rotate buttons then
        apply to one side or, with nothing selected, to the whole sheet. A
        click that deleted outright meant a mis-click cost a page, and left
        no way to rotate one side of a duplex sheet.
        """
        for (x0, y0, x1, y1), side in getattr(self, '_hit', ()):
            if x0 <= ev.x <= x1 and y0 <= ev.y <= y1:
                self.picked = None if self.picked == side else side
                self.show()
                return
        self.picked = None
        self.show()

    def turn(self, degrees):
        """Rotate the sheet on screen, or just one side of it.

        A side that has been clicked is the one that turns; otherwise the whole
        sheet does. Feeding a page in sideways turns both sides sideways, so
        the sheet is the usual case, but a folded or mixed original needs the
        sides handled apart.

        Nothing is resampled: the rotation is stored and applied as a multiple
        of 90 when the page is drawn and when it is saved, so turning back and
        forth costs no quality.
        """
        if not self.pages:
            return
        page = self.pages[self.shown]
        sides = [self.picked] if self.picked is not None else range(len(page))
        for si in sides:
            key = (self.shown, si)
            self.turned[key] = (self.turned.get(key, 0) + degrees) % 360
        self.show()

    def oriented(self, sheet, side, img):
        """The image as it should be seen and saved."""
        return imaging.rotate(img, self.turned.get((sheet, side), 0))

    def kept(self):
        """(sheet index, side index, image) for everything still wanted."""
        return [(pi, si, self.oriented(pi, si, img))
                for pi, page in enumerate(self.pages)
                for si, img in enumerate(page)
                if (pi, si) not in self.dropped]

    def step(self, d):
        if not self.pages:
            return
        self.shown = max(0, min(len(self.pages) - 1, self.shown + d))
        self.picked = None
        self.show()

    def show(self):
        """Draw the current sheet: each side as a page on the stage."""
        from PIL import Image, ImageDraw, ImageFilter, ImageTk

        self.canvas.delete('all')
        cw = self.canvas.winfo_width() or 780
        ch = self.canvas.winfo_height() or 640

        if not self.pages:
            self.canvas.create_text(cw // 2, ch // 2 - 8, fill='#a8a8ad',
                                    font=('', 16), text='No pages yet')
            self.canvas.create_text(cw // 2, ch // 2 + 16, fill='#bcbcc1',
                                    font=('', 12),
                                    text='Load paper and press Scan')
            self.page_lbl.config(text='')
            for b in (self.prev_btn, self.next_btn, self.drop_btn,
                      self.ccw_btn, self.cw_btn):
                b.enable(False)
            return

        page = self.pages[self.shown]
        gap, pad = 26, 34
        each = (cw - gap * (len(page) + 1)) // max(1, len(page))
        stage = tuple(int(STAGE[i:i + 2], 16) for i in (1, 3, 5))
        self._tk, self._hit = [], []

        for i, arr in enumerate(page):
            gone = (self.shown, i) in self.dropped
            im = imaging.to_pil(self.oriented(self.shown, i, arr)).convert('RGB')
            sc = min(each / im.width, (ch - 2 * pad) / im.height)
            im = im.resize((max(1, int(im.width * sc)),
                            max(1, int(im.height * sc))), Image.LANCZOS)
            if gone:
                im = Image.blend(im.convert('L').convert('RGB'),
                                 Image.new('RGB', im.size, STAGE), 0.55)

            # A page is white and the stage is nearly white, so without a
            # shadow the sheet has no edge and the eye cannot find it. Drawn,
            # not faked with outlines: a blurred dark rectangle offset
            # downward, which is what gives it weight.
            blur, off, m = 12, 7, 26
            card = Image.new('RGB', (im.width + 2 * m, im.height + 2 * m), stage)
            sh = Image.new('L', card.size, 0)
            ImageDraw.Draw(sh).rectangle(
                [m, m + off, m + im.width, m + im.height + off], fill=70)
            card.paste(Image.new('RGB', card.size, (0, 0, 0)),
                       (0, 0), sh.filter(ImageFilter.GaussianBlur(blur)))
            card.paste(im, (m, m))
            ph = ImageTk.PhotoImage(card)
            self._tk.append(ph)

            x = gap + i * (each + gap) + (each - card.width) // 2
            y = (ch - card.height) // 2
            self.canvas.create_image(x, y, anchor='nw', image=ph)
            self._hit.append(((x + m, y + m, x + m + im.width, y + m + im.height), i))
            if self.picked == i:
                self.canvas.create_rectangle(x + m - 3, y + m - 3,
                                             x + m + im.width + 2,
                                             y + m + im.height + 2,
                                             outline=ACCENT, width=3)

            if gone:
                self.canvas.create_text(x + card.width // 2, y + card.height // 2,
                                        text='DELETED', fill=DANGER,
                                        font=('', 20, 'bold'))
            if len(page) > 1:
                self.canvas.create_text(
                    x + card.width // 2, y + card.height - m + 14,
                    text=('Front', 'Back')[i], fill=INK_FAINT, font=('', 11))

        kept = len(self.kept())
        total = sum(len(p) for p in self.pages)
        self.page_lbl.config(text='%d of %d' % (self.shown + 1, len(self.pages)))
        self.say_count(kept, total)
        if self.picked is not None:
            sel = {(self.shown, self.picked)}
        else:
            sel = {(self.shown, i) for i in range(len(page))}
        self.drop_btn.set_text('Restore' if sel <= self.dropped else 'Delete')
        self.drop_btn.enable(True)
        self.ccw_btn.enable(True)
        self.cw_btn.enable(True)
        self.prev_btn.enable(self.shown > 0)
        self.next_btn.enable(self.shown < len(self.pages) - 1)

    def say_count(self, kept, total):
        what = ('Front', 'Back')[self.picked] if self.picked is not None else None
        scope = what if what else 'this sheet'
        tail = 'rotate or delete %s' % scope.lower()
        if kept < total:
            self.status.config(text='%d of %d pages kept \u00b7 %s'
                                    % (kept, total, tail))
        elif total:
            self.status.config(text='%d page%s \u00b7 click a page to select it '
                                    '\u00b7 %s'
                                    % (total, '' if total == 1 else 's', tail))

    def on_preview(self, img):
        """Called from the scanning thread as each chunk lands.

        Only the newest frame matters, so an older one still queued is dropped
        rather than drawn: the page grows several times a second and painting
        every step would put the display behind the scanner.
        """
        self.q.put(('preview', img))

    def draw_live(self, img):
        """Paint a partial page, top-aligned, as it arrives."""
        from PIL import Image, ImageTk

        self.canvas.delete('all')
        cw = self.canvas.winfo_width() or 780
        ch = self.canvas.winfo_height() or 640
        im = imaging.to_pil(img).convert('RGB')
        # The page is only as tall as what has arrived, so scale by the width
        # it will end up being, not by what is there now - otherwise the image
        # shrinks as the sheet feeds, which looks like a fault.
        sc = min((cw - 120) / im.width, (ch - 80) / (im.width * 1.45))
        sc = max(sc, 0.05)
        im = im.resize((max(1, int(im.width * sc)),
                        max(1, int(im.height * sc))), Image.BILINEAR)
        self._live_tk = ImageTk.PhotoImage(im)
        x = (cw - im.width) // 2
        y = max(24, (ch - int(im.width * 1.45)) // 2)
        self.canvas.create_rectangle(x - 1, y - 1, x + im.width, y + im.height,
                                     outline='#d0d0d5')
        self.canvas.create_image(x, y, anchor='nw', image=self._live_tk)

    def say(self, t):
        self.status.config(text=t)

    def pump(self):
        """Drain the worker's queue and repaint, ten times a second.

        The live frames are collected here rather than drawn as they arrive:
        the page grows faster than it is worth painting, and only the newest
        frame is ever of interest.
        """
        fresh = None
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == 'preview':
                    fresh = payload
                elif kind == 'status':
                    self.say(payload)
                elif kind == 'done':
                    self.live = None
                    self.pages = payload
                    self.dropped = set()
                    self.turned = {}
                    self.picked = None
                    self.shown = 0
                    self.show()
                    self.say('%d sheet(s)' % len(payload))
                    self.busy = False
                    self.scan_btn.enable(True)
                    self.save_btn.enable(True)
                elif kind == 'error':
                    self.live = None
                    self.say(payload)
                    self.busy = False
                    self.scan_btn.enable(True)
                    self.show()
        except queue.Empty:
            pass

        if fresh is not None and self.busy:
            self.live = fresh
            self.draw_live(fresh)
        self.root.after(100, self.pump)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
