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

DPI_CHOICES = ['75', '150 (downsampled)', '300', '600', '1200 (unsupported)']
MODES = ['color', 'gray', 'lineart']
DEPTHS = [8, 16]
FORMATS = ['png', 'tif', 'jpg', 'pdf', 'raw']


class App:
    def __init__(self, root):
        self.root = root
        root.title('scan8000f')
        root.geometry('760x560')
        self.q = queue.Queue()
        self.scanning = False
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
        ttk.Combobox(top, textvariable=self.depth, values=DEPTHS, width=6,
                     state='readonly').grid(row=1, column=2, padx=4)

        fmt = ttk.LabelFrame(self.root, text='Export formats')
        fmt.pack(fill='x', **pad)
        self.fmt = {}
        for i, f in enumerate(FORMATS):
            v = tk.BooleanVar(value=(f == 'png'))
            self.fmt[f] = v
            ttk.Checkbutton(fmt, text=f.upper(), variable=v).grid(row=0, column=i, padx=10, pady=4)

        outf = ttk.Frame(self.root); outf.pack(fill='x', **pad)
        ttk.Label(outf, text='Save to').pack(side='left')
        self.outdir = tk.StringVar(value=os.path.expanduser('~/Desktop'))
        ttk.Entry(outf, textvariable=self.outdir).pack(side='left', fill='x', expand=True, padx=6)
        ttk.Button(outf, text='Browse', command=self._browse).pack(side='left')
        ttk.Label(outf, text='Name').pack(side='left', padx=(10, 2))
        self.name = tk.StringVar(value='scan')
        ttk.Entry(outf, textvariable=self.name, width=16).pack(side='left')

        btn = ttk.Frame(self.root); btn.pack(fill='x', **pad)
        self.scan_btn = ttk.Button(btn, text='Scan', command=self._scan)
        self.scan_btn.pack(side='left')
        self.prog = ttk.Progressbar(btn, mode='determinate', maximum=100)
        self.prog.pack(side='left', fill='x', expand=True, padx=8)

        body = ttk.Frame(self.root); body.pack(fill='both', expand=True, **pad)
        self.canvas = tk.Label(body, background='#222', text='(preview)', foreground='#888')
        self.canvas.pack(side='left', fill='both', expand=True)
        self.logbox = tk.Text(body, width=34, height=10, background='#111', foreground='#4c4')
        self.logbox.pack(side='right', fill='y')

        if not imaging.HAVE_PIL:
            self._log('Pillow not found - only RAW export.\n`pip install pillow numpy` for images.')

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.outdir.get())
        if d: self.outdir.set(d)

    def _log(self, msg):
        self.logbox.insert('end', msg + '\n'); self.logbox.see('end')

    def _scan(self):
        if self.scanning: return
        fmts = [f for f, v in self.fmt.items() if v.get()]
        if not fmts:
            messagebox.showwarning('No format', 'Pick at least one export format.'); return
        self.scanning = True
        self.scan_btn.config(state='disabled')
        self.prog['value'] = 0
        self.logbox.delete('1.0', 'end')
        dpi = int(self.dpi.get().split()[0])
        args = (dpi, self.mode.get(), self.depth.get(),
                self.outdir.get(), self.name.get(), fmts)
        threading.Thread(target=self._worker, args=args, daemon=True).start()

    def _worker(self, dpi, mode, depth, outdir, name, fmts):
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
                                        preview=_prev)
            base = os.path.join(os.path.expanduser(outdir), name)
            use = fmts if imaging.HAVE_PIL else ['raw']
            written = imaging.export(raw, meta, base, use)
            self.q.put(('log', 'saved:'))
            for p in written:
                self.q.put(('log', '  ' + os.path.basename(p)))
            self.q.put(('preview', (raw, meta)))
        except Exception as e:
            self.q.put(('log', 'ERROR: %s' % e))
            for ln in traceback.format_exc().splitlines()[-4:]:
                self.q.put(('log', ln))
        finally:
            try: driver.close_device()
            except Exception: pass
            self.q.put(('done', None))

    def _pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == 'log':
                    self._log(payload)
                elif kind == 'prog':
                    self._log(payload.strip())
                    if '%' in payload:
                        try: self.prog['value'] = int(payload.split('%')[0].split()[-1])
                        except Exception: pass
                elif kind == 'preview_img':
                    self._show_image(payload)
                elif kind == 'preview':
                    self._preview(*payload)
                elif kind == 'done':
                    self.scanning = False
                    self.scan_btn.config(state='normal')
                    self.prog['value'] = 100
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    def _show_image(self, im):
        try:
            from PIL import ImageTk
            im = im.convert('RGB')
            w = self.canvas.winfo_width() or 400
            h = self.canvas.winfo_height() or 400
            im.thumbnail((max(w, 100), max(h, 100)))
            self._tkimg = ImageTk.PhotoImage(im)
            self.canvas.config(image=self._tkimg, text='')
        except Exception as e:
            self._log('preview failed: %s' % e)

    def _preview(self, raw, meta):
        if not imaging.HAVE_PIL:
            return
        try:
            self._show_image(imaging.to_image(raw, meta, bits=8))
        except Exception as e:
            self._log('preview failed: %s' % e)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
