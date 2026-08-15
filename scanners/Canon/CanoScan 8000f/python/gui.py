#!/usr/bin/env python3
"""Simple Tkinter GUI for the CanoScan 8000F native driver.

Runs the scan on a background thread, streams progress, previews the result,
and exports the chosen formats.  Tkinter is in the Python standard library, so
no extra install is needed for the interface (image export still needs Pillow).
"""
import os, sys, time, threading, queue

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import driver, imaging

DPI_CHOICES = ['75', '100', '150', '200', '300', '400', '600', '800', '1200']
MODES = ['color', 'gray', 'lineart']
DEPTHS = [8, 16]
DEPTH16_FORMATS = {'png', 'tif', 'raw'}   # JPEG/PDF are 8-bit only
FORMATS = ['png', 'tif', 'jpg', 'pdf', 'raw']


class App:
    def __init__(self, root):
        self.root = root
        root.title('scan8000f')
        root.geometry('860x640')
        self.q = queue.Queue()
        self.scanning = False
        self.region = None          # normalised (x0,y0,x1,y1) selection, or None = full bed
        self._img_rect = None       # (x,y,w,h) of the displayed image on the canvas
        self._marquee = None        # canvas rectangle id for the current selection
        self._drag0 = None
        self._tkimg = None
        self._disp_img = None       # currently displayed PIL image
        self._selectable = False    # True only while the full-bed prescan is shown
        self._build()
        self.root.after(100, self._pump)

    def _build(self):
        pad = dict(padx=8, pady=4)
        top = ttk.Frame(self.root); top.pack(fill='x', **pad)

        ttk.Label(top, text='Resolution').grid(row=0, column=0, sticky='w')
        self.dpi = tk.StringVar(value='300')
        ttk.Combobox(top, textvariable=self.dpi, values=DPI_CHOICES, width=18,
                     state='readonly').grid(row=1, column=0, padx=4)

        ttk.Label(top, text='Mode').grid(row=0, column=1, sticky='w')
        self.mode = tk.StringVar(value='color')
        ttk.Combobox(top, textvariable=self.mode, values=MODES, width=10,
                     state='readonly').grid(row=1, column=1, padx=4)

        ttk.Label(top, text='Depth').grid(row=0, column=2, sticky='w')
        self.depth = tk.IntVar(value=8)
        depth_cb = ttk.Combobox(top, textvariable=self.depth, values=DEPTHS, width=6,
                                state='readonly')
        depth_cb.grid(row=1, column=2, padx=4)
        depth_cb.bind('<<ComboboxSelected>>', self._on_depth)

        fmt = ttk.LabelFrame(self.root, text='Export formats')
        fmt.pack(fill='x', **pad)
        self.fmt = {}
        self.fmt_btn = {}
        for i, f in enumerate(FORMATS):
            v = tk.BooleanVar(value=(f == 'png'))
            self.fmt[f] = v
            cb = ttk.Checkbutton(fmt, text=f.upper(), variable=v)
            cb.grid(row=0, column=i, padx=10, pady=4)
            self.fmt_btn[f] = cb

        self.trace = tk.BooleanVar(value=False)
        ttk.Checkbutton(fmt, text='USB trace log', variable=self.trace).grid(
            row=0, column=len(FORMATS), padx=(24, 10), pady=4)

        outf = ttk.Frame(self.root); outf.pack(fill='x', **pad)
        ttk.Label(outf, text='Save to').pack(side='left')
        self.outdir = tk.StringVar(value=os.path.expanduser('~/Desktop'))
        ttk.Entry(outf, textvariable=self.outdir).pack(side='left', fill='x', expand=True, padx=6)
        ttk.Button(outf, text='Browse', command=self._browse).pack(side='left')
        ttk.Label(outf, text='Name').pack(side='left', padx=(10, 2))
        self.name = tk.StringVar(value='scan')
        ttk.Entry(outf, textvariable=self.name, width=16).pack(side='left')

        btn = ttk.Frame(self.root); btn.pack(fill='x', **pad)
        self.pre_btn = ttk.Button(btn, text='Prescan', command=self._prescan)
        self.pre_btn.pack(side='left')
        self.scan_btn = ttk.Button(btn, text='Scan', command=self._scan)
        self.scan_btn.pack(side='left', padx=(6, 0))
        self.region_lbl = ttk.Label(btn, text='Area: full bed')
        self.region_lbl.pack(side='left', padx=(12, 0))
        self.clear_btn = ttk.Button(btn, text='Full bed', command=self._clear_region, state='disabled')
        self.clear_btn.pack(side='left', padx=(6, 0))
        self.prog = ttk.Progressbar(btn, mode='determinate', maximum=100)
        self.prog.pack(side='left', fill='x', expand=True, padx=8)

        body = ttk.Frame(self.root); body.pack(fill='both', expand=True, **pad)
        self.canvas = tk.Canvas(body, background='#222', highlightthickness=0, cursor='tcross')
        self.canvas.pack(side='left', fill='both', expand=True)
        self.canvas.create_text(4, 4, anchor='nw', fill='#888', tags='hint',
                                text='(Prescan the bed, then drag a box to select an area)')
        self.canvas.bind('<Button-1>', self._m_press)
        self.canvas.bind('<B1-Motion>', self._m_drag)
        self.canvas.bind('<ButtonRelease-1>', self._m_release)
        self.canvas.bind('<Configure>', lambda e: self._redraw())
        self.logbox = tk.Text(body, width=34, height=10, background='#111', foreground='#4c4')
        self.logbox.pack(side='right', fill='y')

        if not imaging.HAVE_PIL:
            self._log('Pillow not found - only RAW export.\n`pip install pillow numpy` for images.')

    def _on_depth(self, *_):
        # JPEG and PDF can't hold 16-bit; grey them out (and uncheck) at 16-bit depth.
        sixteen = (self.depth.get() == 16)
        for f, btn in self.fmt_btn.items():
            if f in DEPTH16_FORMATS:
                continue
            if sixteen:
                self.fmt[f].set(False)
                btn.state(['disabled'])
            else:
                btn.state(['!disabled'])

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.outdir.get())
        if d: self.outdir.set(d)

    def _log(self, msg):
        self.logbox.insert('end', msg + '\n'); self.logbox.see('end')

    # ---- region selection --------------------------------------------------
    def _clear_region(self):
        self.region = None
        self.region_lbl.config(text='Area: full bed')
        self.clear_btn.config(state='disabled')
        self._redraw()

    def _m_press(self, e):
        if not self._img_rect or self.scanning or not self._selectable: return
        self._drag0 = (e.x, e.y)
        if self._marquee: self.canvas.delete(self._marquee)
        self._marquee = self.canvas.create_rectangle(e.x, e.y, e.x, e.y,
                                                     outline='#33d', width=2, dash=(4, 3))

    def _m_drag(self, e):
        if not self._drag0 or not self._marquee: return
        x, y, w, h = self._img_rect
        cx = min(max(e.x, x), x + w); cy = min(max(e.y, y), y + h)
        self.canvas.coords(self._marquee, self._drag0[0], self._drag0[1], cx, cy)

    def _m_release(self, e):
        if not self._drag0 or not self._img_rect: return
        x, y, w, h = self._img_rect
        x0, y0 = self._drag0; self._drag0 = None
        x1 = min(max(e.x, x), x + w); y1 = min(max(e.y, y), y + h)
        x0 = min(max(x0, x), x + w); y0 = min(max(y0, y), y + h)
        nx0, nx1 = sorted(((x0 - x) / w, (x1 - x) / w))
        ny0, ny1 = sorted(((y0 - y) / h, (y1 - y) / h))
        if (nx1 - nx0) < 0.02 or (ny1 - ny0) < 0.02:      # too small -> treat as clear
            self._clear_region(); return
        self.region = (nx0, ny0, nx1, ny1)
        self.region_lbl.config(text='Area: %d%%×%d%% of bed' %
                               (round((nx1 - nx0) * 100), round((ny1 - ny0) * 100)))
        self.clear_btn.config(state='normal')

    def _prescan(self):
        if self.scanning: return
        self.scanning = True
        self.pre_btn.config(state='disabled'); self.scan_btn.config(state='disabled')
        self.prog['value'] = 0; self.logbox.delete('1.0', 'end')
        self._clear_region()
        threading.Thread(target=self._prescan_worker, daemon=True).start()

    def _prescan_worker(self):
        import contextlib, io, traceback
        class _QW(io.TextIOBase):
            def __init__(s, q): s.q = q; s.buf = ''
            def write(s, t):
                s.buf += t
                while '\n' in s.buf:
                    ln, s.buf = s.buf.split('\n', 1)
                    if ln.strip(): s.q.put(('log', ln.rstrip()))
                return len(t)
        try:
            self.q.put(('log', 'prescan (150 dpi, full bed)...'))
            driver.open_device()
            with contextlib.redirect_stdout(_QW(self.q)):
                raw, meta = driver.scan(dpi=150, mode='color', depth=8,
                                        progress=lambda s: self.q.put(('prog', s)))
            if imaging.HAVE_PIL:
                self.q.put(('prescan_img', imaging.to_image(raw, meta, bits=8)))
            self.q.put(('log', 'prescan done - drag a box to select an area.'))
        except Exception as e:
            self.q.put(('log', 'ERROR: %s' % e))
            for ln in traceback.format_exc().splitlines()[-4:]:
                self.q.put(('log', ln))
        finally:
            try: driver.close_device()
            except Exception: pass
            self.q.put(('done', None))

    def _scan(self):
        if self.scanning: return
        fmts = [f for f, v in self.fmt.items() if v.get()]
        if not fmts:
            messagebox.showwarning('No format', 'Pick at least one export format.'); return
        self.scanning = True
        self.pre_btn.config(state='disabled'); self.scan_btn.config(state='disabled')
        self.prog['value'] = 0
        self.logbox.delete('1.0', 'end')
        dpi = int(self.dpi.get().split()[0])
        args = (dpi, self.mode.get(), self.depth.get(),
                self.outdir.get(), self.name.get(), fmts, self.trace.get(), self.region)
        threading.Thread(target=self._worker, args=args, daemon=True).start()

    def _worker(self, dpi, mode, depth, outdir, name, fmts, trace, region):
        import io, contextlib, traceback
        class _QW(io.TextIOBase):
            def __init__(s, q): s.q = q; s.buf = ''
            def write(s, t):
                s.buf += t
                while '\n' in s.buf:
                    line, s.buf = s.buf.split('\n', 1)
                    if line.strip(): s.q.put(('log', line.rstrip()))
                return len(t)
        try:
            self.q.put(('log', 'opening scanner...'))
            driver.open_device()
            def _prev(rawbuf, pmeta):
                try:
                    im = imaging.preview_image(rawbuf, pmeta)
                    if im is not None: self.q.put(('preview_img', im))
                except Exception:
                    pass
            with contextlib.redirect_stdout(_QW(self.q)):
                raw, meta = driver.scan(dpi=dpi, mode=mode, depth=depth,
                                        progress=lambda s: self.q.put(('prog', s)),
                                        preview=_prev, trace=trace, region=region)
            base = os.path.join(os.path.expanduser(outdir), name)
            use = fmts if imaging.HAVE_PIL else ['raw']
            written = imaging.export(raw, meta, base, use)
            self.q.put(('log', 'saved:'))
            for p in written:
                self.q.put(('log', '  ' + os.path.basename(p)))
            # Decode the final preview HERE (worker thread), not on the Tk main
            # thread — a large-scan decode on the UI thread freezes the window.
            if imaging.HAVE_PIL:
                try:
                    self.q.put(('preview_img', imaging.to_image(raw, meta, bits=8)))
                except Exception:
                    pass
        except Exception as e:
            self.q.put(('log', 'ERROR: %s' % e))
            for ln in traceback.format_exc().splitlines()[-4:]:
                self.q.put(('log', ln))
        finally:
            try: driver.close_device()
            except Exception: pass
            self.q.put(('done', None))

    def _pump(self):
        # Drain the worker->UI queue. Each message is handled in its own try so one
        # bad message can't kill the loop, and the reschedule is in `finally` so the
        # poll always continues (otherwise a single handler exception freezes the UI).
        try:
            while True:
                try:
                    kind, payload = self.q.get_nowait()
                except queue.Empty:
                    break
                try:
                    if kind == 'log':
                        self._log(payload)
                    elif kind == 'prog':
                        self._log(payload.strip())
                        if '%' in payload:
                            try: self.prog['value'] = int(payload.split('%')[0].split()[-1])
                            except Exception: pass
                    elif kind == 'preview_img':
                        self._show_image(payload, selectable=False)
                    elif kind == 'prescan_img':
                        self._show_image(payload, selectable=True)
                    elif kind == 'done':
                        self.scanning = False
                        self.pre_btn.config(state='normal')
                        self.scan_btn.config(state='normal')
                        self.prog['value'] = 100
                except Exception as e:
                    try: self._log('ui error: %s' % e)
                    except Exception: pass
        finally:
            self.root.after(100, self._pump)

    def _show_image(self, im, selectable=False):
        """Store `im` as the displayed image and draw it (fit + centred) on the canvas.
        selectable=True marks it as the full-bed prescan you can draw a selection on;
        scan-result previews pass False (the marquee isn't drawn over a cropped result)."""
        try:
            self._disp_img = im.convert('RGB')
            self._selectable = selectable
            self._redraw()
        except Exception as e:
            self._log('preview failed: %s' % e)

    def _redraw(self):
        im = self._disp_img
        if im is None:
            return
        try:
            from PIL import Image, ImageTk
            cw = self.canvas.winfo_width() or 500
            ch = self.canvas.winfo_height() or 500
            scale = min(cw / im.width, ch / im.height)
            dw, dh = max(1, int(im.width * scale)), max(1, int(im.height * scale))
            disp = im.resize((dw, dh), Image.LANCZOS)
            self._tkimg = ImageTk.PhotoImage(disp)
            ix, iy = (cw - dw) // 2, (ch - dh) // 2
            self._img_rect = (ix, iy, dw, dh)
            self.canvas.delete('all'); self._marquee = None
            self.canvas.create_image(ix, iy, anchor='nw', image=self._tkimg)
            if self.region and self._selectable:
                x0, y0, x1, y1 = self.region
                self._marquee = self.canvas.create_rectangle(
                    ix + x0 * dw, iy + y0 * dh, ix + x1 * dw, iy + y1 * dh,
                    outline='#33d', width=2, dash=(4, 3))
        except Exception as e:
            self._log('draw failed: %s' % e)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
