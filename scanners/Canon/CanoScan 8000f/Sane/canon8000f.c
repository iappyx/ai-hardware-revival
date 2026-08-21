/* SANE backend for the Canon CanoScan 8000F.
 *
 * The scan pipeline is a C transliteration of the reverse-engineered
 * pure-Python driver (driver.py / imaging.py in this project). USB is done with
 * libusb-1.0 directly - the same control/bulk sequences pyusb issues.
 *
 * STATUS (stage 1/2): SANE plumbing + option model + USB device detection are
 * complete and compilable. sane_read currently emits a synthetic test pattern so
 * the SANE integration can be validated end-to-end (scanimage etc.) before
 * the hardware pipeline is ported in. Pipeline work is marked TODO(pipeline).
 *
 * Not affiliated with or endorsed by Canon. "CanoScan" is a Canon trademark used
 * only to identify the hardware this backend drives.
 */
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <math.h>
#include <unistd.h>
#include <time.h>

#include <sane/sane.h>
#include <sane/saneopts.h>
#include <libusb-1.0/libusb.h>

#include "canon8000f_tables.h"

#define CANON_VID 0x04a9
#define CANON_PID 0x220f

#define BED_W_MM 209.97   /* 620 px / 75 dpi * 25.4 */
#define BED_H_MM 296.69   /* 876 px / 75 dpi * 25.4 */

#define DBG(...) do { if (getenv("CANON8000F_DEBUG")) fprintf(stderr, "[canon8000f] " __VA_ARGS__); } while (0)

/* ---- options ------------------------------------------------------------- */

enum Canon_Option {
    OPT_NUM_OPTS = 0,
    OPT_MODE_GROUP,
    OPT_MODE,
    OPT_RESOLUTION,
    OPT_DEPTH,
    OPT_GEOMETRY_GROUP,
    OPT_TL_X, OPT_TL_Y, OPT_BR_X, OPT_BR_Y,
    OPT_PREVIEW,
    NUM_OPTIONS
};

static const SANE_Word dpi_list[]   = { 9, 75, 100, 150, 200, 300, 400, 600, 800, 1200 };
static const SANE_Word depth_list[] = { 2, 8, 16 };
static const SANE_String_Const mode_list[] = {
    SANE_VALUE_SCAN_MODE_COLOR,
    SANE_VALUE_SCAN_MODE_GRAY,
    SANE_VALUE_SCAN_MODE_LINEART,
    0
};
static const SANE_Range range_x = { SANE_FIX(0.0), SANE_FIX(BED_W_MM), 0 };
static const SANE_Range range_y = { SANE_FIX(0.0), SANE_FIX(BED_H_MM), 0 };

/* ---- per-handle state ---------------------------------------------------- */

typedef struct Canon_Scanner {
    SANE_Device sane;                       /* name/vendor/model/type strings */
    SANE_Option_Descriptor opt[NUM_OPTIONS];

    SANE_Int   resolution;                  /* dpi */
    SANE_Int   depth;                       /* 8 / 16 (1 for lineart) */
    char       mode[32];                    /* Color / Gray / Lineart */
    SANE_Fixed tl_x, tl_y, br_x, br_y;      /* mm */
    SANE_Bool  preview;

    libusb_device_handle *usb;
    int ep_in, ep_out, iface;
    unsigned char shadow[0x80];             /* register write-shadow (driver.py `shadow`) */
    int lamp_pwm;                           /* set by warmup, used by calibrate/scan */

    /* Sticky fault latches. C cannot raise, so rd() records the failure here and
       the pipeline checks it at each stage boundary instead. Once set, they stay
       set for the session: a link that has dropped four consecutive transfers is
       not trusted again without a reopen. */
    int comm_failed;                        /* a register read could not complete */
    int fatal;                              /* device reported reg0x04 == 0x84    */

    SANE_Bool scanning;
    SANE_Parameters params;
    SANE_Byte *frame;                       /* decoded frame buffer */
    size_t frame_len, frame_pos;
} Canon_Scanner;

/* ---- module globals ------------------------------------------------------ */

static libusb_context *g_usb;
static const SANE_Device **g_devlist;       /* NULL-terminated, for sane_get_devices */
/* Device name WITHOUT the backend prefix: SANE's dll layer prepends "canon8000f:"
   itself, so naming this "canon8000f:0" yields "canon8000f:canon8000f:0". */
static char g_devname[] = "0";              /* single-device backend for now */
static SANE_Auth_Callback g_auth;

static int is_color(Canon_Scanner *s);
static int is_lineart(Canon_Scanner *s);

/* ========================================================================= *
 *  USB register primitives + fixed sub-programs (ported from driver.py).      *
 * ========================================================================= */
#define REQ_REG    0x0c
#define REQ_BUF    0x04
#define V_SETADDR  0x83
#define V_WRVAL    0x85
#define V_RDREG    0x84
#define V_BUF      0x82
#define CTRL_TMO   2000

static void ctrl_out(Canon_Scanner *s, int wValue, const unsigned char *d, int len)
{ libusb_control_transfer(s->usb, 0x40, REQ_REG, wValue, 0, (unsigned char *)d, len, CTRL_TMO); }

static void sel(Canon_Scanner *s, int addr) { unsigned char b = addr; ctrl_out(s, V_SETADDR, &b, 1); }

static void wr(Canon_Scanner *s, int addr, int val)
{ val &= 0xff; s->shadow[addr] = val; sel(s, addr); { unsigned char b = val; ctrl_out(s, V_WRVAL, &b, 1); } }

/* Read one ASIC register. Returns the byte, or -1 if it cannot be read.
 *
 * It must NOT substitute a value, because 0 is legal for every register we poll
 * and at_home() inverts dangerously: (rd(0x64) & 0x40) == 0 means "home", so a
 * substituted 0 reads as "carriage IS home" - the worst thing to be wrong about
 * on a scanner whose motor is stepped from the line clock. Only reachable after
 * four consecutive failed transfers, i.e. when the link is already broken.
 *
 * sel() is inside the retry too: it is the same kind of control transfer and can
 * fail the same way. */
static int rd(Canon_Scanner *s, int addr)
{
    unsigned char b = 0;
    for (int i = 0; i < 4; i++) {
        sel(s, addr);
        if (libusb_control_transfer(s->usb, 0xc0, REQ_REG, V_RDREG, 0, &b, 1, CTRL_TMO) == 1)
            return b;
        usleep(20000);
    }
    DBG("rd: register 0x%02x unreadable after 4 attempts - scanner "
        "disconnected or unresponsive\n", addr);
    s->comm_failed = 1;
    return -1;
}

static void wbit(Canon_Scanner *s, int addr, int start, int width, int val)
{
    int mask = ((1 << width) - 1) << start;
    s->shadow[addr] = (s->shadow[addr] & ~mask) | ((val << start) & mask);
    wr(s, addr, s->shadow[addr]);
}
static void commit(Canon_Scanner *s) { sel(s, 0x24); }
static void __attribute__((unused)) afe(Canon_Scanner *s, int a, int v) { wr(s, 0x25, a & 0x3f); wr(s, 0x26, v & 0xff); }
static void w16r(Canon_Scanner *s, int lo, int hi, int v) { wr(s, lo, v & 0xff); wr(s, hi, (v >> 8) & 0xff); }
static void sdram(Canon_Scanner *s, int a)
{ wr(s, 0x21, a & 0xff); wr(s, 0x22, (a >> 8) & 0xff); wr(s, 0x23, (a >> 16) & 0xff); commit(s); }
static void set_1b(Canon_Scanner *s, int v)      { wr(s, 0x1b, v & 0xff); wr(s, 0x1c, (v >> 8) & 0xff); }
static void __attribute__((unused)) set_lincnt10(Canon_Scanner *s, int v){ wr(s, 0x10, v & 0xff); wr(s, 0x11, (v >> 8) & 0x7f); }
static void __attribute__((unused)) set_cnt12(Canon_Scanner *s, int v)   { wr(s, 0x12, v & 0xff); wr(s, 0x13, (v >> 8) & 0x3f); }
static void __attribute__((unused)) motor_go(Canon_Scanner *s, int v)    { wbit(s, 0x02, 1, 1, v); }
static void motor_rst(Canon_Scanner *s, int v)   { wbit(s, 0x02, 0, 1, v); }
static void nothome(Canon_Scanner *s, int v)     { wbit(s, 0x02, 7, 1, v); }
/* Both predicates fail SAFE on an unreadable register: move_done reports "still
   moving" (so callers keep waiting rather than launching) and at_home reports
   "not home" (so callers do not assume a parked carriage). status04 passes -1
   through; callers compare against specific values, and -1 matches none. */
static int  move_done(Canon_Scanner *s)          { int v = rd(s, 0x03); return v >= 0 && (v & 0x08) != 0; }
static int  at_home(Canon_Scanner *s)            { int v = rd(s, 0x64); return v >= 0 && (v & 0x40) == 0; }
static int  status04(Canon_Scanner *s)           { return rd(s, 0x04); }
static void lamp_on(Canon_Scanner *s, int a, int b)
{ wr(s, 0x2b, a & 0xff); wr(s, 0x2c, b & 0xff); wr(s, 0x2d, ((a >> 4) & 0x30) | ((b >> 8) & 3)); wbit(s, 0x29, 4, 1, 1); }

static int bulk_out(Canon_Scanner *s, const unsigned char *data, int size)
{
    unsigned char arm[8] = { 1, 0, 0x82, 0,
        size & 0xff, (size >> 8) & 0xff, (size >> 16) & 0xff, (size >> 24) & 0xff };
    libusb_control_transfer(s->usb, 0x40, REQ_BUF, V_BUF, 0, arm, 8, CTRL_TMO);
    int off = 0;
    while (off < size) {
        int chunk = size - off; if (chunk > 0xf000) chunk = 0xf000;
        int done = 0;
        int r = libusb_bulk_transfer(s->usb, s->ep_out, (unsigned char *)data + off, chunk, &done, 8000);
        if (done <= 0) { if (r != 0) break; else continue; }
        off += done;
    }
    return off;
}
static void bulk_out_chunked(Canon_Scanner *s, const unsigned char *data, int size)
{ for (int k = 0; k < size; k += 61440) { int c = size - k; if (c > 61440) c = 61440; bulk_out(s, data + k, c); } }

/* ---- native_init (driver.py native_init / CALIBRATION_SPEC section 2) ----- */

static void native_init(Canon_Scanner *s)
{
    DBG("native_init\n");
    nothome(s, 1); motor_rst(s, 0); usleep(50000);
    for (int i = 0; i < 50; i++) { if (move_done(s)) break; usleep(20000); }
    /* Do not start a scan on a device that has already reported a hard fault.
       Without this the fault is carried silently into the scan and surfaces
       later as unexplained bad data. */
    { int st = status04(s);
      if (st == 0x84) {
          DBG("native_init: reg0x04=0x84 - scanner reported a fatal condition\n");
          s->fatal = 1;
          return;
      }
      if (st == 0x07) wr(s, 0x04, 8); }
    wbit(s, 0x60, 7, 1, 1); wbit(s, 0x60, 3, 1, 1);
    { static const int rv[4][2] = {{0x52,0x0f},{0x53,0x11},{0x54,0x13},{0x55,0x15}};
      for (int i = 0; i < 4; i++) wr(s, rv[i][0], rv[i][1]); }
    wbit(s, 0x01, 0, 1, 1); wbit(s, 0x60, 4, 2, 2);
    wbit(s, 0x05, 5, 1, 0);
    wbit(s, 0x48, 1, 3, 0); wbit(s, 0x48, 5, 3, 0); wbit(s, 0x49, 1, 3, 0); wbit(s, 0x49, 5, 3, 0);
    lamp_on(s, 0x320, 0x320);
    wbit(s, 0x29, 0, 4, 3); wbit(s, 0x2a, 0, 4, 3);

    /* gamma bank A (0x10000 x BE16 = i) and bank B (0x4000 x BE16 = (i*4)&0xffff) */
    { int na = 0x10000, nb = 0x4000;
      unsigned char *A = malloc((size_t)na * 2), *B = malloc((size_t)nb * 2);
      if (A && B) {
          for (int i = 0; i < na; i++) { A[i*2] = (i >> 8) & 0xff; A[i*2+1] = i & 0xff; }
          for (int i = 0; i < nb; i++) { int v = (i * 4) & 0xffff; B[i*2] = (v >> 8) & 0xff; B[i*2+1] = v & 0xff; }
          int aA[4] = {0x83ffff,0x81ffff,0x83ffff,0x85ffff};
          int aB[4] = {0x80ffff,0x807fff,0x80ffff,0x817fff};
          for (int i = 0; i < 4; i++) { sdram(s, aA[i]); bulk_out_chunked(s, A, na * 2); }
          for (int i = 0; i < 4; i++) { sdram(s, aB[i]); bulk_out_chunked(s, B, nb * 2); }
      }
      free(A); free(B);
    }
    wbit(s,0x20,6,2,1); wbit(s,0x01,2,1,1); wbit(s,0x20,4,2,1); wbit(s,0x05,2,1,0); wbit(s,0x06,3,1,1);
    { int bb[4] = {3,4,7,6}; for (int i = 0; i < 4; i++) wbit(s, 0x01, bb[i], 1, 0); }
    wr(s, 0x08, 0xa8); w16r(s, 0x09, 0x0a, 8); w16r(s, 0x0b, 0x0c, 8);
    { int rr[6] = {0x70,0x71,0x72,0x73,0x74,0x75}; for (int i = 0; i < 6; i++) wr(s, rr[i], 0); }
    wbit(s, 0x64, 0, 1, 0);
    for (int b = 2; b < 8; b++) wbit(s, 0x50, b, 1, 0);
    sdram(s, 0x7fffff); bulk_out(s, MASTER_RAMP, (int)sizeof(MASTER_RAMP));
    wbit(s, 0x2f, 0, 6, 0x0a); wbit(s, 0x03, 2, 1, 1);
    wr(s, 0x48, 0); wr(s, 0x49, 0); wr(s, 0x2e, 0x28); set_1b(s, 20000); wbit(s, 0x20, 4, 2, 1);
    motor_rst(s, 1); usleep(1000000); motor_rst(s, 0);
    for (int i = 0; i < 100; i++) { if (move_done(s)) break; usleep(20000); }
    wr(s, 0x2e, 0xff);
    DBG("native_init done: reg03=0x%02x reg04=0x%02x home=%s\n",
        rd(s, 0x03), rd(s, 0x04), at_home(s) ? "YES" : "NO");
}

/* ========================================================================= *
 *  Stage 3-5: warm-up, calibration, scan program, streaming, decode.          *
 * ========================================================================= */
#define GAIN_K 210.0

static double now_s(void)
{ struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec / 1e9; }

static void motor_stop(Canon_Scanner *s) { wbit(s, 0x02, 4, 2, 0); }

static int patient_bulk_in(Canon_Scanner *s, unsigned char *buf, int size, double quiet_s)
{
    unsigned char arm[8] = { 0, 0, 0x82, 0,
        size & 0xff, (size >> 8) & 0xff, (size >> 16) & 0xff, (size >> 24) & 0xff };
    if (libusb_control_transfer(s->usb, 0x40, REQ_BUF, V_BUF, 0, arm, 8, CTRL_TMO) < 0) return 0;
    int got = 0; double last = now_s();
    while (got < size && now_s() - last < quiet_s) {
        int want = size - got; if (want > 0xf000) want = 0xf000;
        int done = 0;
        libusb_bulk_transfer(s->usb, s->ep_in, buf + got, want, &done, 800);
        if (done > 0) { got += done; last = now_s(); }
    }
    return got;
}

/* ---- fixed sub-programs (driver.py) -------------------------------------- */

static void res_class(Canon_Scanner *s, int k)
{
    int vals[4];
    if (k == 0)      { afe(s, 0x03, 0x1f); vals[0]=0x0c; vals[1]=0x0e; vals[2]=0x10; vals[3]=0x12; }
    else if (k == 1) { afe(s, 0x03, 0x2f); vals[0]=0x0c; vals[1]=0x14; vals[2]=0x16; vals[3]=0x00; }
    else             { afe(s, 0x03, 0x2f); vals[0]=0x0f; vals[1]=0x01; vals[2]=0x13; vals[3]=0x15; }
    int rg[4] = {0x52,0x53,0x54,0x55};
    for (int i = 0; i < 4; i++) wr(s, rg[i], vals[i]);
}
static void afe_defaults(Canon_Scanner *s)
{
    int pv[10][2] = {{0x04,0x00},{0x01,0x23},{0x02,0x2c},{0x03,0x1f},
                     {0x20,0x80},{0x21,0x80},{0x22,0x80},{0x28,0x4b},{0x29,0x4b},{0x2a,0x4b}};
    for (int i = 0; i < 10; i++) afe(s, pv[i][0], pv[i][1]);
}
static void identity_matrix(Canon_Scanner *s)
{
    int m[9] = {0x2000,0,0,0,0x2000,0,0,0,0x2000};
    for (int i = 0; i < 9; i++) { wr(s, 0x37, m[i] & 0xff); wr(s, 0x38, (m[i] >> 8) & 0xff); }
}
static void lamp_off(Canon_Scanner *s) { wbit(s,0x60,1,1,0); wbit(s,0x29,4,1,0); wbit(s,0x2a,4,1,0); }

static void mini_slope(Canon_Scanner *s, int E, int div)
{
    unsigned int a = ((unsigned int)(E * 0x18 / div)) | 0x80000000u, b = 0x80000000u, c = 0;
    unsigned char buf[12] = {
        (a>>24)&0xff,(a>>16)&0xff,(a>>8)&0xff,a&0xff,
        (b>>24)&0xff,(b>>16)&0xff,(b>>8)&0xff,b&0xff,
        (c>>24)&0xff,(c>>16)&0xff,(c>>8)&0xff,c&0xff };
    sdram(s, 0x803fff); bulk_out(s, buf, 12);
}

/* counters(): pulse GO, wait line-counter, read per-bank (max,min). w16=16-bit reads. */
static void counters(Canon_Scanner *s, int w16, int mx[3], int mn[3])
{
    motor_go(s, 1);
    double t0 = now_s();
    /* Compose the 16-bit line counter only from two good reads. A -1 folded into
       either half would make the OR non-zero and break the wait immediately,
       reporting a counter that was never actually read. */
    while (now_s() - t0 < 2.0) {
        int hi = rd(s, 0x1a), lo = rd(s, 0x19);
        if (hi < 0 || lo < 0) break;                 /* link down: stop the motor below */
        if (((hi & 0x1f) << 8) | lo) break;
        usleep(10000);
    }
    motor_go(s, 0);
    for (int bank = 0; bank < 3; bank++) {
        wbit(s, 0x30, 0, 2, bank);
        int a, b, c, d;
        if (w16) { a = rd(s,0x35); b = rd(s,0x34); c = rd(s,0x37); d = rd(s,0x36); }
        else     { a = rd(s,0x35); b = 0;         c = rd(s,0x37); d = 0;          }
        if (a < 0 || b < 0 || c < 0 || d < 0) {      /* leave the AFE untuned rather
                                                        than tune it to garbage */
            mx[bank] = mn[bank] = 0;
            continue;
        }
        if (w16) { mx[bank] = (a << 8) | b; mn[bank] = (c << 8) | d; }
        else     { mx[bank] = a;            mn[bank] = c;            }
    }
}

static void static_meas(Canon_Scanner *s, int feed, int width, int afedef)
{
    wbit(s,0x64,0,1,0);
    wbit(s,0x05,3,2,0); wbit(s,0x05,2,1,0);
    res_class(s, 0);
    wbit(s,0x01,2,1,1);
    { int rr[6]={0x70,0x71,0x72,0x73,0x74,0x75}; for (int i=0;i<6;i++) wr(s,rr[i],0); }
    wr(s,0x08,0xa8); w16r(s,0x09,0x0a,8); w16r(s,0x0b,0x0c,8);
    nothome(s,0); wbit(s,0x07,4,1,0); wbit(s,0x07,0,4,1); set_1b(s,1);
    mini_slope(s, 0x3e80, 4);
    wbit(s,0x06,7,1,0); identity_matrix(s); w16r(s,0x19,0x1a,1);
    if (afedef) afe_defaults(s);
    wr(s,0x14,0); wbit(s,0x06,6,1,0);
    wbit(s,0x05,0,2,1);
    wbit(s,0x06,4,2,1);
    wbit(s,0x20,4,2,0);
    set_lincnt10(s,feed); set_cnt12(s,width);
    wbit(s,0x02,4,2, afedef?3:0); wbit(s,0x02,6,1,0);
    wbit(s,0x01,5,1,0);
}

/* ---- warm-up (driver.py native_warmup) ----------------------------------- */

static SANE_Status native_warmup(Canon_Scanner *s)
{
    int mx[3], mn[3];
    wbit(s,0x2f,7,1,1);
    lamp_on(s,0x320,0x320);
    static_meas(s,0,0x2968,1);
    usleep(4000000);
    counters(s,0,mx,mn);
    if (!(mx[0]>0x77||mx[1]>0x77||mx[2]>0x77)) { usleep(4000000); counters(s,0,mx,mn); }
    double t0 = now_s();
    for (;;) {
        if (mx[0]<8 && mx[1]<8 && mx[2]<8) return SANE_STATUS_IO_ERROR;   /* lamp dead */
        if ((mx[0]>0xd1 && mx[1]>0xd1 && mx[2]>0xd1) || now_s()-t0 > 5.0) break;
        usleep(300000); counters(s,0,mx,mn);
    }
    int pwm = 0, step = 0x200;
    for (int i = 0; i < 10; i++) {
        pwm = pwm + step; if (pwm > 0x7fff) pwm = 0x7fff;
        wr(s,0x2b,0); wr(s,0x2c,pwm&0xff); wr(s,0x2d,(pwm>>8)&3);
        usleep(300000); counters(s,0,mx,mn);
        if (mx[0]>200||mx[1]>200||mx[2]>200) pwm -= step;
        step >>= 1;
    }
    if (pwm > 800) pwm = 800;
    wr(s,0x2b,0); wr(s,0x2c,pwm&0xff); wr(s,0x2d,(pwm>>8)&3);
    DBG("warmup: lamp PWM = %d\n", pwm);
    counters(s,0,mx,mn);
    if (mx[0]<8 && mx[1]<8 && mx[2]<8) return SANE_STATUS_IO_ERROR;
    /* stability: 3 consecutive seconds, per-channel spread <= 2 */
    int hist[3][3], hn = 0, ok = 0; t0 = now_s();
    while (ok < 3 && now_s()-t0 < 10.0) {
        usleep(1000000); counters(s,0,mx,mn);
        if (mx[0]<8 && mx[1]<8 && mx[2]<8) return SANE_STATUS_IO_ERROR;
        hist[hn%3][0]=mx[0]; hist[hn%3][1]=mx[1]; hist[hn%3][2]=mx[2]; hn++;
        int stable = (hn>=3);
        if (stable) for (int c=0;c<3;c++) for (int a=0;a<3;a++) for (int b=0;b<3;b++)
            if (abs(hist[a][c]-hist[b][c])>2) stable = 0;
        ok = stable ? ok+1 : 0;
    }
    DBG("warmup: stable=%d peaks %d %d %d\n", ok>=3, mx[0],mx[1],mx[2]);
    set_1b(s,3000); motor_stop(s); wbit(s,0x20,4,2,1);
    nothome(s,1);
    wr(s,0x04,0x86);
    wbit(s,0x2f,7,1,0);
    s->lamp_pwm = pwm;
    return SANE_STATUS_GOOD;
}

/* ---- calibration (driver.py) --------------------------------------------- */

static int gain_code(int peak)
{
    double g = GAIN_K / (peak < 1 ? 1 : peak);
    if (g < 1.0) return 0x4b;
    if (g >= 7.4) return 0xff;
    return (int)(283.0 - 208.0 / g) & 0xff;
}

static void offset_search(Canon_Scanner *s, int dpi)
{
    if (dpi < 0x4b0) { wbit(s,0x05,2,1,1); res_class(s,1); }
    else             { wbit(s,0x05,2,1,0); res_class(s,0); }
    lamp_off(s);
    wbit(s,0x06,6,1,0); wr(s,0x14,0); wbit(s,0x05,0,2,1); wbit(s,0x06,4,2,3);
    wbit(s,0x06,7,1,0); identity_matrix(s); w16r(s,0x19,0x1a,1); wbit(s,0x05,3,2,0);
    { unsigned char sl[8] = {0x80,0x00,0x03,0x80,0x80,0x00,0x00,0x00};
      sdram(s,0x803fff); bulk_out(s,sl,8); }
    set_1b(s,1); wbit(s,0x06,3,1,1);
    { int bb[4]={4,3,7,6}; for (int i=0;i<4;i++) wbit(s,0x01,bb[i],1,0); }
    wbit(s,0x20,4,2,1); set_lincnt10(s,0); wbit(s,0x07,0,4,1);
    wbit(s,0x02,4,2,3); wbit(s,0x02,6,1,0);
    int off[3]={0x80,0x80,0x80}, step[3]={0x40,0x40,0x40}, done[3]={0,0,0}, e0[3]={0,0,0};
    for (int it = 0; it < 8; it++) {
        for (int c=0;c<3;c++) if (!done[c]) afe(s,0x20+c, off[c]&0xff);
        int mx[3], mn[3]; counters(s,1,mx,mn);
        for (int c=0;c<3;c++) {
            if (done[c]) continue;
            int e = mn[c] - 0x400;
            if (it == 0) {
                e0[c] = e;
                if (abs(e) <= 0x100) done[c]=1;
                else if (e <= 0) off[c]=0x3f; else off[c]=0xc0;
            } else {
                if (abs(e) <= 0x100) done[c]=1;
                else { if (e0[c]>0 && e<0) off[c]-=step[c]; else if (e0[c]<0 && e>0) off[c]+=step[c]; }
                step[c] >>= 1;
                if (!done[c]) { if (e<0) off[c]-=step[c]; if (e>0) off[c]+=step[c]; }
            }
        }
        if (done[0]&&done[1]&&done[2]) break;
    }
    for (int c=0;c<3;c++) afe(s,0x20+c, off[c]&0xff);
    lamp_on(s,0,s->lamp_pwm);
    motor_go(s,0); motor_stop(s);
}

static int green_peak(Canon_Scanner *s)
{
    static_meas(s,0,0x2968,0);
    wbit(s,0x02,4,2,3);
    int mx[3], mn[3]; counters(s,0,mx,mn);
    set_1b(s,3000); motor_stop(s); wbit(s,0x20,4,2,1);
    motor_go(s,0); wbit(s,0x02,4,2,0);
    return mx[1];
}

/* 20-line 16-bit static capture. Fills rows[nrows*W*3] uint16 BE-decoded; returns n. */
static int calib_capture(Canon_Scanner *s, int isWhite, int W, int E, unsigned short *rows)
{
    wbit(s,0x01,5,1,0); wbit(s,0x2f,7,1,1);
    wbit(s,0x64,0,1,0);
    for (int i=0;i<3;i++) w16r(s,0x33,0x34,0x400);
    { int rr[6]={0x70,0x71,0x72,0x73,0x74,0x75}; for (int i=0;i<6;i++) wr(s,rr[i],0); }
    wr(s,0x08,0xa8); w16r(s,0x09,0x0a,8); w16r(s,0x0b,0x0c,8); wbit(s,0x01,2,1,1);
    { int bb[4]={4,3,7,6}; for (int i=0;i<4;i++) wbit(s,0x01,bb[i],1,0); }
    wbit(s,0x05,2,1,0);
    for (int i=0;i<50;i++){ if (move_done(s)) break; usleep(20000); }
    wbit(s,0x06,7,1,0); wbit(s,0x06,3,1,1);
    nothome(s,1);
    wbit(s,0x06,4,2,3);
    wbit(s,0x07,0,4,1); wbit(s,0x07,4,1,0); wbit(s,0x05,3,2,0);
    w16r(s,0x09,0x0a,4); w16r(s,0x0b,0x0c,4);
    wbit(s,0x05,2,1,1); res_class(s,1);
    wbit(s,0x06,6,1,0); identity_matrix(s); wr(s,0x14,0);
    wbit(s,0x20,0,4, isWhite?0xb:0);
    { int t[4][3]={{0x48,1,3},{0x48,5,3},{0x49,1,3},{0x49,5,3}}; for (int i=0;i<4;i++) wbit(s,t[i][0],t[i][1],t[i][2],0); }
    wbit(s,0x20,0,4,5); mini_slope(s,E,4);
    set_1b(s,0x14);
    wbit(s,0x20,4,2,1);
    if (isWhite) lamp_on(s,0,s->lamp_pwm); else lamp_off(s);
    wbit(s,0x05,0,2,1);
    set_lincnt10(s,0); set_cnt12(s,W);
    wbit(s,0x01,5,1,1);
    w16r(s,0x40,0x41,0x80); w16r(s,0x42,0x43,0x5a9); w16r(s,0x44,0x45,0xad3);
    w16r(s,0x46,0x47,0); w16r(s,0x17,0x18,0x3e); w16r(s,0x19,0x1a,0);
    wbit(s,0x64,0,1,0);
    wbit(s,0x02,4,2, isWhite?0:3);
    motor_go(s,1); commit(s);
    int rowbytes = W * 6;
    unsigned char *buf = malloc((size_t)rowbytes * 20);
    int total = 0;
    if (buf) {
        for (int i = 0; i < 20; i++) total += patient_bulk_in(s, buf + total, rowbytes, 1.5);
    }
    motor_go(s,0);
    int n = buf ? total / rowbytes : 0;
    for (int k = 0; k < n; k++)
        for (int i = 0; i < W*3; i++) {
            unsigned char *p = buf + (size_t)k*rowbytes + i*2;
            rows[(size_t)k*W*3 + i] = (p[0] << 8) | p[1];   /* big-endian */
        }
    free(buf);
    DBG("calib_capture %s: %d/20 lines\n", isWhite?"white":"dark", n);
    return n;
}

static SANE_Status native_calibrate(Canon_Scanner *s, int dpi)
{
    int W = (dpi >= 1200) ? 0x2968 : 0x14b4;
    int E = 0x2a00, NC = W * 3;
    lamp_on(s,0,s->lamp_pwm);
    wbit(s,0x01,5,1,0); wbit(s,0x2f,7,1,1);
    for (int i=0;i<3;i++) w16r(s,0x33,0x34,0x400);
    wbit(s,0x05,2,1,0);
    wbit(s,0x06,7,1,0); identity_matrix(s); w16r(s,0x19,0x1a,1); wbit(s,0x06,3,1,1);
    { int bb[4]={4,3,7,6}; for (int i=0;i<4;i++) wbit(s,0x01,bb[i],1,0); }
    wbit(s,0x06,6,1,0); wr(s,0x14,0);
    wbit(s,0x05,0,2,1); wbit(s,0x06,4,2,1); wbit(s,0x05,3,2,0); res_class(s,0); wbit(s,0x20,4,2,1);
    set_lincnt10(s,0); set_cnt12(s,0x2968);
    wbit(s,0x02,4,2,3); wbit(s,0x02,6,1,0);
    afe_defaults(s);
    int mx[3], mn[3]; counters(s,0,mx,mn);
    if (mx[0]<8 && mx[1]<8 && mx[2]<8) return SANE_STATUS_IO_ERROR;
    for (int c=0;c<3;c++) afe(s, 0x28+c, gain_code(mx[c]));
    w16r(s,0x09,0x0a,4); w16r(s,0x0b,0x0c,4);
    offset_search(s, dpi >= 1200 ? dpi : 600);

    unsigned short *rows = malloc((size_t)20 * NC * sizeof(short));
    double *white = calloc(NC, sizeof(double));
    double *dsm   = calloc(NC, sizeof(double));
    if (!rows || !white || !dsm) { free(rows); free(white); free(dsm); return SANE_STATUS_NO_MEM; }

    int nL = calib_capture(s, 1, W, E, rows);
    if (nL >= 20) {
        for (int i = 0; i < NC; i++) {
            int col[20];
            for (int k = 0; k < nL; k++) col[k] = rows[(size_t)k*NC + i];
            for (int a=0;a<nL-1;a++) for (int b=a+1;b<nL;b++) if (col[b]>col[a]) { int t=col[a];col[a]=col[b];col[b]=t; }
            double sum = 0; for (int k = 2; k < 18; k++) sum += col[k];
            white[i] = sum / 16.0;
        }
    } else if (nL > 0) {
        for (int i = 0; i < NC; i++) { double sm=0; for (int k=0;k<nL;k++) sm+=rows[(size_t)k*NC+i]; white[i]=sm/nL; }
    }

    int g0 = green_peak(s);
    int nD = calib_capture(s, 0, W, E, rows);
    int nd = nD < 1 ? 1 : nD;
    double *dark = calloc(NC, sizeof(double));
    if (dark && nD > 0)
        for (int i = 0; i < NC; i++) { double sm=0; for (int k=0;k<nD;k++) sm+=rows[(size_t)k*NC+i]; dark[i]=sm/nd; }
    /* smooth dark: 100-px forward mean, +/-0x14 guard, bias -0x100 */
    for (int c = 0; c < 3; c++) {
        double *cum = malloc((W+1) * sizeof(double));
        cum[0] = 0;
        for (int x = 0; x < W; x++) cum[x+1] = cum[x] + (dark ? dark[x*3+c] : 0);
        for (int x = 0; x < W; x++) {
            int n100 = W - x < 100 ? W - x : 100;
            double fm = (cum[x+n100] - cum[x]) / n100;
            double v = dark ? dark[x*3+c] : 0;
            double out = (fabs(fm - v) > 0x14 ? v : fm) - 0x100;
            dsm[x*3+c] = out > 0 ? out : 0;
        }
        free(cum);
    }
    free(dark);

    lamp_on(s,0,s->lamp_pwm);
    double t0 = now_s(); int g = 0;
    while (now_s()-t0 < 30.0) { g = green_peak(s); if (g0 && g >= (int)(0.8*g0)) break; usleep(200000); }
    DBG("lamp recovered: green %d/%d\n", g, g0);

    /* shading table: per column 3 gains (BE16) + 3 darks (BE16), pad 8 every 0x200 */
    long K = 0x7d000000L;
    size_t cap = (size_t)W * 12 + (W/85 + 16) * 8;
    unsigned char *out = malloc(cap);
    size_t len = 0;
    for (int x = 0; x < W; x++) {
        for (int c = 0; c < 3; c++) {
            int w = (int)white[x*3+c], d = (int)dsm[x*3+c];
            int span = (w > d) ? (w - d) : 1;
            int gn = (int)(K / span); if (gn > 0x1fffe) gn = 0x1fffe; gn = (gn + 1) >> 1;
            out[len++] = (gn >> 8) & 0xff; out[len++] = gn & 0xff;
        }
        for (int c = 0; c < 3; c++) {
            int d = (int)dsm[x*3+c] & 0xffff;
            out[len++] = (d >> 8) & 0xff; out[len++] = d & 0xff;
        }
        if ((len & 0x1ff) == 0x1f8) { for (int z = 0; z < 8; z++) out[len++] = 0; }
    }
    sdram(s, 0xffffff); bulk_out_chunked(s, out, (int)len);
    wbit(s,0x2f,7,1,0);
    DBG("shading uploaded: %zu bytes (W=%d)\n", len, W);
    free(out); free(rows); free(white); free(dsm);
    return SANE_STATUS_GOOD;
}

/* ---- motor tables + scan program ----------------------------------------- */

static void emit_motor_tables(Canon_Scanner *s, int rung)
{
    wr(s,0x06,0x30); wr(s,0x02,0x80); rd(s,0x03);
    wr(s,0x20, (rung==150||rung==1200)?0x60:0x50); wr(s,0x36,0x89);
    sdram(s,0x7fffff); bulk_out(s, MASTER_RAMP, (int)sizeof(MASTER_RAMP));
    int sl, tl; const unsigned char *sp = slope_for(rung, &sl); const unsigned char *tp = tail_for(rung, &tl);
    sdram(s,0x803fff); bulk_out(s, sp, sl);
    sdram(s,0x801fff); bulk_out(s, tp, tl);
}

static void emit_scan_program(Canon_Scanner *s, int xdpi, int ydpi, int width, int lines, int mode, int depth)
{
    w16r(s,0x12,0x13,width);
    int feed = (0 + 10 + 278) / (1200 / ydpi);
    w16r(s,0x10,0x11,feed);
    wbit(s,0x06,4,2, depth==8 ? 1 : 3);
    int ydiv = (ydpi >= 1200) ? 1 : 600 / ydpi;
    wbit(s,0x07,0,4,ydiv);
    wbit(s,0x07,4,1,0);
    w16r(s,0x1b,0x1c,lines);
    int avg = (xdpi==4800) ? 0x40 : 0x20 / (2400 / xdpi);
    wr(s,0x14,avg);
    int t;
    if (xdpi==2400||xdpi==4800) t=0x88; else if (xdpi==1200) t=0x90;
    else if (xdpi==600) t=(ydpi<1200)?0x60:0x30; else if (xdpi==300) t=0x90; else t=0xc0;
    wr(s,0x1e,t); wr(s,0x1f,t);
    { int gg[3]={0x46e,0x469,0x447}; for (int i=0;i<3;i++) w16r(s,0x33,0x34,gg[i]); }
    if (mode==2) wbit(s,0x05,0,2,1);
    else if (mode==1) { wbit(s,0x05,0,2,2); wbit(s,0x05,6,2,1); }
    else { wbit(s,0x05,0,2,0); wr(s,0x15,0x80); wr(s,0x16,0x80); wbit(s,0x05,6,2,1); }
    wbit(s,0x03,6,2,3);
    if (xdpi >= 1200) wbit(s,0x05,6,1,1);
    wbit(s,0x01,3,1,1); wbit(s,0x01,4,1,1);
    wbit(s,0x01,7,1, depth!=16 ? 1 : 0);
    wbit(s,0x01,6,1,0);
    int n = (avg*width + 0x400) >> 10;
    int mm = (0xf7f - 3*n) / 3;
    w16r(s,0x40,0x41,0x80); w16r(s,0x42,0x43,mm+0x80); w16r(s,0x44,0x45,n+0x80+2*mm);
    w16r(s,0x46,0x47,0);
    int v17 = (mm<<10)/width - 1; if (v17 > 0xfff) v17 = 0xfff;
    w16r(s,0x17,0x18,v17);
    w16r(s,0x19,0x1a,0);
    wr(s,0x1d,0x10);
    int kk = (xdpi < 150) ? 0 : 4;
    wbit(s,0x49,1,3,kk); wbit(s,0x48,5,3,kk);
    { int M[9]={0x2000,0,0,0,0x2000,0,0,0,0x2000};
      int stream[9]={M[0],M[3],M[6],M[1],M[4],M[7],M[2],M[5],M[8]};
      for (int i=0;i<9;i++){ wr(s,0x37,stream[i]&0xff); wr(s,0x38,(stream[i]>>8)&0xff); } }
    wbit(s,0x02,4,2, xdpi>=1200 ? 0 : 2);
    wbit(s,0x2f,7,1,1);
    wbit(s,0x02,1,1,1);
    commit(s);
}

static void reactive_home_quiet(Canon_Scanner *s)
{
    wr(s,0x02,0x00); wr(s,0x02,0x80); wr(s,0x02,0xa0);
    double t0 = now_s(); int seen = 0;
    while (now_s()-t0 < 25.0) {
        int st = rd(s,0x03);
        if (st < 0) break;              /* link down - fall through to the stop below
                                           rather than keep pulsing the motor blind */
        if ((st & 0x08) == 0) seen = 1;
        if (seen && (st & 0x08)) break;
        wr(s,0x02,0xa2); wr(s,0x02,0xa0); usleep(20000);
    }
    wr(s,0x02,0x00);
}
static void teardown(Canon_Scanner *s)
{
    wbit(s,0x2f,7,1,0); wbit(s,0x02,1,1,0); wbit(s,0x07,4,1,0); wbit(s,0x03,2,1,0);
    wbit(s,0x60,0,1,0); wbit(s,0x60,1,1,0);
    wbit(s,0x48,1,3,0); wbit(s,0x48,5,3,0); wbit(s,0x49,1,3,0); wbit(s,0x49,5,3,0);
    wbit(s,0x02,7,1,1); wr(s,0x02,0x00);
}

/* ---- decode (imaging.py) ------------------------------------------------- */

/* scanner -> linear sRGB (imaging._C) */
static const double CMTX[3][3] = {
    { 1.5460, -0.1419, -0.4043 },
    { 0.0024,  1.0267, -0.0290 },
    { -0.0134, -0.1680, 1.1812 }
};
static double srgb_enc(double v)
{
    if (v < 0) v = 0;
    if (v > 1) v = 1;
    return v <= 0.0031308 ? 12.92*v : 1.055*pow(v, 1.0/2.4) - 0.055;
}
static double smp(const unsigned char *raw, long idx, int depth)
{
    if (depth == 16) { long p = idx*2; return ((raw[p]) | (raw[p+1] << 8)) / 65535.0; }  /* LE */
    return raw[idx] / 255.0;
}
/* full 2D vertical-diff plane for channel c: out[k*W+x] = smp(k+1,x)-smp(k,x) */
static void diff_plane(const unsigned char *raw, int L, int W, int depth, int c, float *out)
{
    for (int k = 0; k < L-1; k++)
        for (int x = 0; x < W; x++)
            out[(long)k*W + x] = (float)(smp(raw,(long)(k+1)*W*3+x*3+c,depth)
                                       - smp(raw,(long)k*W*3+x*3+c,depth));
}
/* imaging.bshift over the FULL 2D diff arrays (exact): normalized cross-correlation,
 * best integer row-shift in [-lim,lim]. Matches imaging.py's per-slice normalisation. */
static int bshift2d(const float *A, const float *B, int R, int W, int lim)
{
    if (lim <= 0 || R <= 1) return 0;
    double bestc = -2; int bests = 0;
    for (int sh = -lim; sh <= lim; sh++) {
        int m = R - abs(sh);
        if (m <= 0) continue;
        const float *Ap = sh >= 0 ? A + (long)sh*W : A;
        const float *Bp = sh >= 0 ? B : B - (long)sh*W;
        long cnt = (long)m * W;
        double sa=0, sb=0, saq=0, sbq=0, sab=0;
        for (long i = 0; i < cnt; i++) { double a=Ap[i], b=Bp[i]; sa+=a; sb+=b; saq+=a*a; sbq+=b*b; sab+=a*b; }
        double ma=sa/cnt, mb=sb/cnt;
        double va=saq/cnt-ma*ma, vb=sbq/cnt-mb*mb;
        double sda=sqrt(va>0?va:0)+1e-9, sdb=sqrt(vb>0?vb:0)+1e-9;
        double corr=(sab/cnt - ma*mb)/(sda*sdb);
        if (corr > bestc) { bestc = corr; bests = sh; }
    }
    return bests;
}

/* Build s->frame (SANE format) from raw. rung native dims full_w x full_l. */
static SANE_Status decode_to_frame(Canon_Scanner *s, const unsigned char *raw, long rawlen,
                                   int rung, int m, int depth)
{
    int W = (int)lround(620.0 * rung / 75.0);
    int color = (m == 2), lineart = (m == 0);
    int ch = color ? 3 : 1;
    int stride = lineart ? (W+7)/8 : W * ch * (depth==16 ? 2 : 1);
    int L = stride ? (int)(rawlen / stride) : 0;
    if (L < 2) return SANE_STATUS_IO_ERROR;

    /* channel realignment (colour only) */
    int s1 = 0, s2 = 0, pad = 0, nrows = L;
    if (color) {
        long R = L - 1;
        float *A  = malloc((size_t)R * W * sizeof(float));
        float *Bc = malloc((size_t)R * W * sizeof(float));
        if (!A || !Bc) { free(A); free(Bc); return SANE_STATUS_NO_MEM; }
        int lim = (rung >= 1200) ? 48 : 24; if (lim > L-2) lim = L-2;
        diff_plane(raw, L, W, depth, 0, A);
        diff_plane(raw, L, W, depth, 1, Bc); s1 = bshift2d(A, Bc, R, W, lim);
        diff_plane(raw, L, W, depth, 2, Bc); s2 = bshift2d(A, Bc, R, W, lim);
        free(A); free(Bc);
        pad = 0; if (s1 > pad) pad = s1; if (s2 > pad) pad = s2;
        int neg = 0; if (-s1 > neg) neg = -s1; if (-s2 > neg) neg = -s2;
        nrows = L - pad - neg;
        DBG("decode: s1=%d s2=%d pad=%d nrows=%d\n", s1, s2, pad, nrows);
        if (nrows < 2) return SANE_STATUS_IO_ERROR;
    }
    int Himg = color ? nrows : L;    /* height of the final (rotated) native image */

    /* crop rectangle in final-image native coords, from the SANE geometry (mm) */
    double tlx = SANE_UNFIX(s->tl_x), tly = SANE_UNFIX(s->tl_y);
    double brx = SANE_UNFIX(s->br_x), bry = SANE_UNFIX(s->br_y);
    if (brx < tlx) { double t=brx; brx=tlx; tlx=t; }
    if (bry < tly) { double t=bry; bry=tly; tly=t; }
    int cx0 = (int)lround(tlx/25.4*rung), cx1 = (int)lround(brx/25.4*rung);
    int cy0 = (int)lround(tly/25.4*rung), cy1 = (int)lround(bry/25.4*rung);
    if (cx0 < 0) cx0 = 0;
    if (cx1 > W) cx1 = W;
    if (cx1 <= cx0) { cx0 = 0; cx1 = W; }
    if (cy0 < 0) cy0 = 0;
    if (cy1 > Himg) cy1 = Himg;
    if (cy1 <= cy0) { cy0 = 0; cy1 = Himg; }

    SANE_Parameters *p = &s->params;
    int OW = p->pixels_per_line, OH = p->lines;
    size_t flen = (size_t)p->bytes_per_line * OH;
    s->frame = malloc(flen);
    if (!s->frame) return SANE_STATUS_NO_MEM;
    s->frame_len = flen; s->frame_pos = 0;

    /* final-image pixel (X,Y) -> value(s). Rotate180: final(Y,X) = source(Himg-1-Y, W-1-X). */
    #define CLAMPI(v,hi) ((v)<0?0:((v)>(hi)?(hi):(v)))
    for (int oy = 0; oy < OH; oy++) {
        double fy = cy0 + (oy + 0.5) * (double)(cy1 - cy0) / OH - 0.5;
        int Y0 = (int)floor(fy); double wy = fy - Y0;
        int Y1 = Y0 + 1; Y0 = CLAMPI(Y0, Himg-1); Y1 = CLAMPI(Y1, Himg-1);
        SANE_Byte *orow = s->frame + (size_t)oy * p->bytes_per_line;
        if (lineart) memset(orow, 0, p->bytes_per_line);
        for (int ox = 0; ox < OW; ox++) {
            double fx = cx0 + (ox + 0.5) * (double)(cx1 - cx0) / OW - 0.5;
            int X0 = (int)floor(fx); double wx = fx - X0;
            int X1 = X0 + 1; X0 = CLAMPI(X0, W-1); X1 = CLAMPI(X1, W-1);
            if (color) {
                double acc[3] = {0,0,0};
                int YS[2] = {Y0,Y1}, XS[2] = {X0,X1};
                double wY[2] = {1-wy, wy}, wX[2] = {1-wx, wx};
                for (int iy=0; iy<2; iy++) for (int ix=0; ix<2; ix++) {
                    double wgt = wY[iy]*wX[ix]; if (wgt == 0) continue;
                    int Y = YS[iy], X = XS[ix];
                    int r = nrows-1-Y, x = W-1-X;
                    int y0 = pad+r, y1 = pad-s1+r, y2 = pad-s2+r;
                    y0 = CLAMPI(y0, L-1); y1 = CLAMPI(y1, L-1); y2 = CLAMPI(y2, L-1);
                    double R = smp(raw, (long)y0*W*3 + x*3 + 0, depth);
                    double G = smp(raw, (long)y1*W*3 + x*3 + 1, depth);
                    double B = smp(raw, (long)y2*W*3 + x*3 + 2, depth);
                    double lr = R*CMTX[0][0]+G*CMTX[0][1]+B*CMTX[0][2];
                    double lg = R*CMTX[1][0]+G*CMTX[1][1]+B*CMTX[1][2];
                    double lb = R*CMTX[2][0]+G*CMTX[2][1]+B*CMTX[2][2];
                    acc[0]+=wgt*srgb_enc(lr); acc[1]+=wgt*srgb_enc(lg); acc[2]+=wgt*srgb_enc(lb);
                }
                if (depth == 16) {
                    unsigned short *q = (unsigned short *)(orow + ox*6);
                    for (int c=0;c<3;c++){ int v=(int)(acc[c]*65535+0.5); q[c]=v<0?0:(v>65535?65535:v); }
                } else {
                    for (int c=0;c<3;c++){ int v=(int)(acc[c]*255+0.5); orow[ox*3+c]=v<0?0:(v>255?255:v); }
                }
            } else if (lineart) {
                int Y = (wy < 0.5 ? Y0 : Y1), X = (wx < 0.5 ? X0 : X1);
                int y = L-1-Y, xx = W-1-X; int rowb = (W+7)/8;
                int bit = (raw[(long)y*rowb + (xx>>3)] >> (7-(xx&7))) & 1;
                if (bit) orow[ox>>3] |= 0x80 >> (ox & 7);
            } else { /* gray (linear sensor value) */
                double acc = 0;
                int YS[2]={Y0,Y1}, XS[2]={X0,X1}; double wY[2]={1-wy,wy}, wX[2]={1-wx,wx};
                for (int iy=0;iy<2;iy++) for (int ix=0;ix<2;ix++) {
                    double wgt=wY[iy]*wX[ix]; if (wgt==0) continue;
                    int y=L-1-YS[iy], xx=W-1-XS[ix];
                    acc += wgt * smp(raw, (long)y*W + xx, depth);
                }
                if (depth == 16) { unsigned short *q=(unsigned short*)(orow+ox*2); int v=(int)(acc*65535+0.5); q[0]=v<0?0:(v>65535?65535:v); }
                else { int v=(int)(acc*255+0.5); orow[ox]=v<0?0:(v>255?255:v); }
            }
        }
    }
    #undef CLAMPI
    return SANE_STATUS_GOOD;
}

/* ---- full scan orchestration (driver.py scan) ---------------------------- */

static SANE_Status run_scan(Canon_Scanner *s)
{
    int reqdpi = s->resolution;
    int rung = reqdpi;
    if (reqdpi==100) rung=150; else if (reqdpi==200) rung=300;
    else if (reqdpi==400) rung=600; else if (reqdpi==800) rung=1200;
    int m = is_lineart(s) ? 0 : (is_color(s) ? 2 : 1);
    int depth = is_lineart(s) ? 1 : s->depth;
    int W = (int)lround(620.0 * rung / 75.0);
    int Ln = (int)lround(876.0 * rung / 75.0);
    int expo = (rung >= 1200) ? 0x5400 : 0x2a00;
    int stride = (m==0) ? (W+7)/8 : W * (m==2?3:1) * (depth==16?2:1);
    long target = (long)Ln * stride;

    memset(s->shadow, 0, sizeof(s->shadow));
    s->comm_failed = 0; s->fatal = 0;     /* fresh session: clear the fault latches */
    native_init(s);
    /* Stage boundary: refuse to drive the motor if init reported a hard fault or
       the link dropped. This is where C substitutes for the reference driver's
       exception - past this point the carriage moves. */
    if (s->fatal) {
        DBG("sane_start: aborting - scanner reported a fatal condition\n");
        return SANE_STATUS_IO_ERROR;
    }
    if (s->comm_failed) {
        DBG("sane_start: aborting - register reads failing, link unreliable\n");
        return SANE_STATUS_IO_ERROR;
    }
    SANE_Status st = native_warmup(s);   if (st != SANE_STATUS_GOOD) return st;
    if (s->comm_failed) return SANE_STATUS_IO_ERROR;
    st = native_calibrate(s, rung);       if (st != SANE_STATUS_GOOD) return st;
    if (s->comm_failed) return SANE_STATUS_IO_ERROR;

    lamp_on(s,0x320,s->lamp_pwm);
    wbit(s,0x01,5,1,1);
    wr(s,0x08,0x01);
    w16r(s,0x09,0x0a, expo>>4); w16r(s,0x0b,0x0c, expo>>4);
    { int rr[6]={0x70,0x71,0x72,0x73,0x74,0x75}; for (int i=0;i<6;i++) wr(s,rr[i],0); }
    wbit(s,0x01,2,1,1);
    if (rung >= 1200) { wbit(s,0x05,2,1,0); res_class(s,0); } else { wbit(s,0x05,2,1,1); res_class(s,1); }
    wbit(s,0x06,0,1,0); wbit(s,0x03,2,1,1);
    wbit(s,0x20,4,2,1); wbit(s,0x06,6,1,0); wbit(s,0x06,3,1,0);
    nothome(s,1);
    for (int i=0;i<100;i++){ if (move_done(s)) break; usleep(50000); }
    wbit(s,0x20,0,4,0);
    emit_motor_tables(s, rung);
    emit_scan_program(s, rung, rung, W, Ln, m, depth);

    unsigned char *raw = malloc(target);
    if (!raw) return SANE_STATUS_NO_MEM;
    long got = 0; double t0 = now_s();
    double wd = target / 2.0e5; if (wd < 150.0) wd = 150.0;
    while (got < target && now_s()-t0 < wd) {
        long want = target - got; if (want > 0x10000) want = 0x10000;
        int n = patient_bulk_in(s, raw + got, (int)want, 1.2);
        if (n <= 0) { if (now_s()-t0 > 3) break; else continue; }
        got += n;
    }
    double tq = now_s();
    while (now_s()-tq < 20.0) { if (move_done(s)) break; usleep(50000); }
    wr(s,0x02,0x00);
    reactive_home_quiet(s);
    teardown(s);
    DBG("scan streamed %ld/%ld bytes\n", got, target);

    st = decode_to_frame(s, raw, got, rung, m, depth);
    free(raw);
    return st;
}

/* ---- USB device discovery / open ---------------------------------------- */

static SANE_Status
usb_open(Canon_Scanner *s)
{
    s->usb = libusb_open_device_with_vid_pid(g_usb, CANON_VID, CANON_PID);
    if (!s->usb) return SANE_STATUS_IO_ERROR;

    libusb_set_auto_detach_kernel_driver(s->usb, 1);
    s->iface = 0;
    if (libusb_claim_interface(s->usb, s->iface) != 0) {
        libusb_close(s->usb); s->usb = NULL;
        return SANE_STATUS_DEVICE_BUSY;
    }
    /* locate the bulk IN/OUT endpoints on interface 0, alt-setting 0 */
    struct libusb_config_descriptor *cfg = NULL;
    s->ep_in = s->ep_out = 0;
    if (libusb_get_active_config_descriptor(libusb_get_device(s->usb), &cfg) == 0 && cfg) {
        const struct libusb_interface_descriptor *id = &cfg->interface[0].altsetting[0];
        for (int e = 0; e < id->bNumEndpoints; e++) {
            const struct libusb_endpoint_descriptor *ep = &id->endpoint[e];
            if ((ep->bmAttributes & 3) == LIBUSB_TRANSFER_TYPE_BULK) {
                if (ep->bEndpointAddress & 0x80) s->ep_in = ep->bEndpointAddress;
                else s->ep_out = ep->bEndpointAddress;
            }
        }
        libusb_free_config_descriptor(cfg);
    }
    if (!s->ep_in || !s->ep_out) {
        libusb_release_interface(s->usb, s->iface);
        libusb_close(s->usb); s->usb = NULL;
        return SANE_STATUS_IO_ERROR;
    }
    DBG("usb open: ep_in=0x%02x ep_out=0x%02x\n", s->ep_in, s->ep_out);
    return SANE_STATUS_GOOD;
}

static void
usb_close(Canon_Scanner *s)
{
    if (s->usb) {
        libusb_release_interface(s->usb, s->iface);
        libusb_close(s->usb);
        s->usb = NULL;
    }
}

static SANE_Bool
device_present(void)
{
    if (getenv("CANON8000F_FAKE")) return SANE_TRUE;  /* test the SANE path w/o hardware */
    libusb_device **list;
    ssize_t n = libusb_get_device_list(g_usb, &list);
    SANE_Bool found = SANE_FALSE;
    for (ssize_t i = 0; i < n; i++) {
        struct libusb_device_descriptor d;
        if (libusb_get_device_descriptor(list[i], &d) == 0 &&
            d.idVendor == CANON_VID && d.idProduct == CANON_PID) { found = SANE_TRUE; break; }
    }
    if (n >= 0) libusb_free_device_list(list, 1);
    return found;
}

/* ---- option table -------------------------------------------------------- */

static void
init_options(Canon_Scanner *s)
{
    SANE_Option_Descriptor *o = s->opt;

    o[OPT_NUM_OPTS].name  = SANE_NAME_NUM_OPTIONS;
    o[OPT_NUM_OPTS].title = SANE_TITLE_NUM_OPTIONS;
    o[OPT_NUM_OPTS].desc  = SANE_DESC_NUM_OPTIONS;
    o[OPT_NUM_OPTS].type  = SANE_TYPE_INT;
    o[OPT_NUM_OPTS].cap   = SANE_CAP_SOFT_DETECT;
    o[OPT_NUM_OPTS].size  = sizeof(SANE_Word);

    o[OPT_MODE_GROUP].title = "Scan mode";
    o[OPT_MODE_GROUP].type  = SANE_TYPE_GROUP;

    o[OPT_MODE].name  = SANE_NAME_SCAN_MODE;
    o[OPT_MODE].title = SANE_TITLE_SCAN_MODE;
    o[OPT_MODE].desc  = SANE_DESC_SCAN_MODE;
    o[OPT_MODE].type  = SANE_TYPE_STRING;
    o[OPT_MODE].size  = 32;
    o[OPT_MODE].cap   = SANE_CAP_SOFT_SELECT | SANE_CAP_SOFT_DETECT;
    o[OPT_MODE].constraint_type = SANE_CONSTRAINT_STRING_LIST;
    o[OPT_MODE].constraint.string_list = mode_list;

    o[OPT_RESOLUTION].name  = SANE_NAME_SCAN_RESOLUTION;
    o[OPT_RESOLUTION].title = SANE_TITLE_SCAN_RESOLUTION;
    o[OPT_RESOLUTION].desc  = SANE_DESC_SCAN_RESOLUTION;
    o[OPT_RESOLUTION].type  = SANE_TYPE_INT;
    o[OPT_RESOLUTION].unit  = SANE_UNIT_DPI;
    o[OPT_RESOLUTION].size  = sizeof(SANE_Word);
    o[OPT_RESOLUTION].cap   = SANE_CAP_SOFT_SELECT | SANE_CAP_SOFT_DETECT;
    o[OPT_RESOLUTION].constraint_type = SANE_CONSTRAINT_WORD_LIST;
    o[OPT_RESOLUTION].constraint.word_list = dpi_list;

    o[OPT_DEPTH].name  = SANE_NAME_BIT_DEPTH;
    o[OPT_DEPTH].title = SANE_TITLE_BIT_DEPTH;
    o[OPT_DEPTH].desc  = SANE_DESC_BIT_DEPTH;
    o[OPT_DEPTH].type  = SANE_TYPE_INT;
    o[OPT_DEPTH].unit  = SANE_UNIT_BIT;
    o[OPT_DEPTH].size  = sizeof(SANE_Word);
    o[OPT_DEPTH].cap   = SANE_CAP_SOFT_SELECT | SANE_CAP_SOFT_DETECT;
    o[OPT_DEPTH].constraint_type = SANE_CONSTRAINT_WORD_LIST;
    o[OPT_DEPTH].constraint.word_list = depth_list;

    o[OPT_GEOMETRY_GROUP].title = "Geometry";
    o[OPT_GEOMETRY_GROUP].type  = SANE_TYPE_GROUP;

    o[OPT_TL_X].name = SANE_NAME_SCAN_TL_X; o[OPT_TL_X].title = SANE_TITLE_SCAN_TL_X; o[OPT_TL_X].desc = SANE_DESC_SCAN_TL_X;
    o[OPT_TL_Y].name = SANE_NAME_SCAN_TL_Y; o[OPT_TL_Y].title = SANE_TITLE_SCAN_TL_Y; o[OPT_TL_Y].desc = SANE_DESC_SCAN_TL_Y;
    o[OPT_BR_X].name = SANE_NAME_SCAN_BR_X; o[OPT_BR_X].title = SANE_TITLE_SCAN_BR_X; o[OPT_BR_X].desc = SANE_DESC_SCAN_BR_X;
    o[OPT_BR_Y].name = SANE_NAME_SCAN_BR_Y; o[OPT_BR_Y].title = SANE_TITLE_SCAN_BR_Y; o[OPT_BR_Y].desc = SANE_DESC_SCAN_BR_Y;
    for (int i = OPT_TL_X; i <= OPT_BR_Y; i++) {
        o[i].type = SANE_TYPE_FIXED;
        o[i].unit = SANE_UNIT_MM;
        o[i].size = sizeof(SANE_Word);
        o[i].cap  = SANE_CAP_SOFT_SELECT | SANE_CAP_SOFT_DETECT;
        o[i].constraint_type = SANE_CONSTRAINT_RANGE;
        o[i].constraint.range = (i == OPT_TL_X || i == OPT_BR_X) ? &range_x : &range_y;
    }

    o[OPT_PREVIEW].name  = SANE_NAME_PREVIEW;
    o[OPT_PREVIEW].title = SANE_TITLE_PREVIEW;
    o[OPT_PREVIEW].desc  = SANE_DESC_PREVIEW;
    o[OPT_PREVIEW].type  = SANE_TYPE_BOOL;
    o[OPT_PREVIEW].size  = sizeof(SANE_Word);
    o[OPT_PREVIEW].cap   = SANE_CAP_SOFT_SELECT | SANE_CAP_SOFT_DETECT;

    /* defaults */
    s->resolution = 300;
    s->depth = 8;
    strcpy(s->mode, SANE_VALUE_SCAN_MODE_COLOR);
    s->tl_x = SANE_FIX(0.0); s->tl_y = SANE_FIX(0.0);
    s->br_x = SANE_FIX(BED_W_MM); s->br_y = SANE_FIX(BED_H_MM);
    s->preview = SANE_FALSE;
}

/* ---- parameter computation ---------------------------------------------- */

static int
is_color(Canon_Scanner *s) { return strcmp(s->mode, SANE_VALUE_SCAN_MODE_COLOR) == 0; }
static int
is_lineart(Canon_Scanner *s) { return strcmp(s->mode, SANE_VALUE_SCAN_MODE_LINEART) == 0; }

static void
compute_params(Canon_Scanner *s)
{
    double tlx = SANE_UNFIX(s->tl_x), tly = SANE_UNFIX(s->tl_y);
    double brx = SANE_UNFIX(s->br_x), bry = SANE_UNFIX(s->br_y);
    if (brx < tlx) { double t = brx; brx = tlx; tlx = t; }
    if (bry < tly) { double t = bry; bry = tly; tly = t; }
    int dpi = s->resolution;
    int px = (int)lround((brx - tlx) / 25.4 * dpi);
    int ln = (int)lround((bry - tly) / 25.4 * dpi);
    if (px < 1) px = 1;
    if (ln < 1) ln = 1;

    SANE_Parameters *p = &s->params;
    p->last_frame = SANE_TRUE;
    p->lines = ln;
    p->pixels_per_line = px;
    if (is_lineart(s)) {
        p->format = SANE_FRAME_GRAY;
        p->depth = 1;
        p->bytes_per_line = (px + 7) / 8;
    } else if (is_color(s)) {
        p->format = SANE_FRAME_RGB;
        p->depth = s->depth;
        p->bytes_per_line = px * 3 * (s->depth == 16 ? 2 : 1);
    } else {
        p->format = SANE_FRAME_GRAY;
        p->depth = s->depth;
        p->bytes_per_line = px * (s->depth == 16 ? 2 : 1);
    }
}

/* ---- stage-1 synthetic frame (replaced by the real pipeline) ------------- */

static SANE_Status
build_test_frame(Canon_Scanner *s)
{
    /* TODO(pipeline): replace with open->init->warmup->calibrate->scan->decode.
     * For now: a gradient / checkerboard so the SANE data path is exercised. */
    SANE_Parameters *p = &s->params;
    size_t len = (size_t)p->bytes_per_line * p->lines;
    s->frame = malloc(len);
    if (!s->frame) return SANE_STATUS_NO_MEM;
    s->frame_len = len; s->frame_pos = 0;

    int W = p->pixels_per_line, H = p->lines;
    for (int y = 0; y < H; y++) {
        SANE_Byte *row = s->frame + (size_t)y * p->bytes_per_line;
        if (p->depth == 1) {
            for (int b = 0; b < p->bytes_per_line; b++) row[b] = 0;
            for (int x = 0; x < W; x++)
                if (((x >> 4) ^ (y >> 4)) & 1) row[x >> 3] |= 0x80 >> (x & 7);
        } else if (p->format == SANE_FRAME_RGB) {
            for (int x = 0; x < W; x++) {
                int r = x * 255 / W, g = y * 255 / H, bl = 128;
                if (p->depth == 16) {
                    SANE_Byte *q = row + x * 6;
                    q[0]=r; q[1]=r; q[2]=g; q[3]=g; q[4]=bl; q[5]=bl;   /* 8->16 by repeat */
                } else {
                    row[x*3+0]=r; row[x*3+1]=g; row[x*3+2]=bl;
                }
            }
        } else { /* gray */
            for (int x = 0; x < W; x++) {
                int v = (x * 255 / W + y * 255 / H) / 2;
                if (p->depth == 16) { row[x*2]=v; row[x*2+1]=v; }
                else row[x] = v;
            }
        }
    }
    return SANE_STATUS_GOOD;
}

/* ========================================================================= *
 *  SANE API                                                                   *
 * ========================================================================= */

SANE_Status
sane_init(SANE_Int *version_code, SANE_Auth_Callback authorize)
{
    g_auth = authorize;
    if (version_code) *version_code = SANE_VERSION_CODE(1, 0, 0);
    if (libusb_init(&g_usb) != 0) return SANE_STATUS_INVAL;
    DBG("sane_init\n");
    return SANE_STATUS_GOOD;
}

void
sane_exit(void)
{
    if (g_devlist) { free((void *)g_devlist); g_devlist = NULL; }
    if (g_usb) { libusb_exit(g_usb); g_usb = NULL; }
}

SANE_Status
sane_get_devices(const SANE_Device ***device_list, SANE_Bool local_only)
{
    (void)local_only;
    static SANE_Device dev;
    static const SANE_Device *list[2];
    if (g_devlist) { free((void *)g_devlist); g_devlist = NULL; }

    if (!device_present()) {
        list[0] = NULL;
        *device_list = list;
        return SANE_STATUS_GOOD;
    }
    dev.name   = g_devname;
    dev.vendor = "Canon";
    dev.model  = "CanoScan 8000F";
    dev.type   = "flatbed scanner";
    list[0] = &dev; list[1] = NULL;
    *device_list = list;
    return SANE_STATUS_GOOD;
}

SANE_Status
sane_open(SANE_String_Const name, SANE_Handle *handle)
{
    (void)name;
    Canon_Scanner *s = calloc(1, sizeof(*s));
    if (!s) return SANE_STATUS_NO_MEM;
    s->sane.name = g_devname;
    s->sane.vendor = "Canon"; s->sane.model = "CanoScan 8000F"; s->sane.type = "flatbed scanner";
    init_options(s);
    *handle = s;
    DBG("sane_open\n");
    return SANE_STATUS_GOOD;
}

void
sane_close(SANE_Handle handle)
{
    Canon_Scanner *s = handle;
    if (!s) return;
    usb_close(s);
    free(s->frame);
    free(s);
}

const SANE_Option_Descriptor *
sane_get_option_descriptor(SANE_Handle handle, SANE_Int option)
{
    Canon_Scanner *s = handle;
    if (option < 0 || option >= NUM_OPTIONS) return NULL;
    return &s->opt[option];
}

SANE_Status
sane_control_option(SANE_Handle handle, SANE_Int option, SANE_Action action,
                    void *value, SANE_Int *info)
{
    Canon_Scanner *s = handle;
    if (option < 0 || option >= NUM_OPTIONS) return SANE_STATUS_INVAL;
    if (info) *info = 0;
    if (s->scanning) return SANE_STATUS_DEVICE_BUSY;

    if (action == SANE_ACTION_GET_VALUE) {
        switch (option) {
        case OPT_NUM_OPTS:   *(SANE_Word *)value = NUM_OPTIONS; return SANE_STATUS_GOOD;
        case OPT_MODE:       strcpy(value, s->mode); return SANE_STATUS_GOOD;
        case OPT_RESOLUTION: *(SANE_Word *)value = s->resolution; return SANE_STATUS_GOOD;
        case OPT_DEPTH:      *(SANE_Word *)value = is_lineart(s) ? 1 : s->depth; return SANE_STATUS_GOOD;
        case OPT_TL_X:       *(SANE_Word *)value = s->tl_x; return SANE_STATUS_GOOD;
        case OPT_TL_Y:       *(SANE_Word *)value = s->tl_y; return SANE_STATUS_GOOD;
        case OPT_BR_X:       *(SANE_Word *)value = s->br_x; return SANE_STATUS_GOOD;
        case OPT_BR_Y:       *(SANE_Word *)value = s->br_y; return SANE_STATUS_GOOD;
        case OPT_PREVIEW:    *(SANE_Word *)value = s->preview; return SANE_STATUS_GOOD;
        }
        return SANE_STATUS_INVAL;
    }
    if (action == SANE_ACTION_SET_VALUE) {
        switch (option) {
        case OPT_MODE:
            strncpy(s->mode, (char *)value, sizeof(s->mode) - 1);
            s->mode[sizeof(s->mode) - 1] = 0;
            if (info) *info |= SANE_INFO_RELOAD_PARAMS | SANE_INFO_RELOAD_OPTIONS;
            return SANE_STATUS_GOOD;
        case OPT_RESOLUTION:
            s->resolution = *(SANE_Word *)value;
            if (info) *info |= SANE_INFO_RELOAD_PARAMS;
            return SANE_STATUS_GOOD;
        case OPT_DEPTH:
            s->depth = *(SANE_Word *)value;
            if (info) *info |= SANE_INFO_RELOAD_PARAMS;
            return SANE_STATUS_GOOD;
        case OPT_TL_X: s->tl_x = *(SANE_Word *)value; if (info) *info |= SANE_INFO_RELOAD_PARAMS; return SANE_STATUS_GOOD;
        case OPT_TL_Y: s->tl_y = *(SANE_Word *)value; if (info) *info |= SANE_INFO_RELOAD_PARAMS; return SANE_STATUS_GOOD;
        case OPT_BR_X: s->br_x = *(SANE_Word *)value; if (info) *info |= SANE_INFO_RELOAD_PARAMS; return SANE_STATUS_GOOD;
        case OPT_BR_Y: s->br_y = *(SANE_Word *)value; if (info) *info |= SANE_INFO_RELOAD_PARAMS; return SANE_STATUS_GOOD;
        case OPT_PREVIEW: s->preview = *(SANE_Word *)value; return SANE_STATUS_GOOD;
        }
        return SANE_STATUS_INVAL;
    }
    return SANE_STATUS_UNSUPPORTED;
}

SANE_Status
sane_get_parameters(SANE_Handle handle, SANE_Parameters *params)
{
    Canon_Scanner *s = handle;
    if (!s->scanning) compute_params(s);
    if (params) *params = s->params;
    return SANE_STATUS_GOOD;
}

SANE_Status
sane_start(SANE_Handle handle)
{
    Canon_Scanner *s = handle;
    if (s->scanning) return SANE_STATUS_DEVICE_BUSY;
    if (!device_present()) return SANE_STATUS_IO_ERROR;

    compute_params(s);
    free(s->frame); s->frame = NULL;

    SANE_Status st;
    if (getenv("CANON8000F_FAKE")) {
        st = build_test_frame(s);           /* no-hardware data-path check */
    } else {
        st = usb_open(s);
        if (st != SANE_STATUS_GOOD) return st;
        st = run_scan(s);                   /* stages 2-5: full pipeline -> s->frame */
        usb_close(s);
    }
    if (st != SANE_STATUS_GOOD) { usb_close(s); return st; }

    s->scanning = SANE_TRUE;
    return SANE_STATUS_GOOD;
}

SANE_Status
sane_read(SANE_Handle handle, SANE_Byte *buf, SANE_Int max_len, SANE_Int *len)
{
    Canon_Scanner *s = handle;
    *len = 0;
    if (!s->scanning) return SANE_STATUS_CANCELLED;
    if (s->frame_pos >= s->frame_len) {
        s->scanning = SANE_FALSE;
        return SANE_STATUS_EOF;
    }
    size_t avail = s->frame_len - s->frame_pos;
    size_t n = (size_t)max_len < avail ? (size_t)max_len : avail;
    memcpy(buf, s->frame + s->frame_pos, n);
    s->frame_pos += n;
    *len = (SANE_Int)n;
    return SANE_STATUS_GOOD;
}

void
sane_cancel(SANE_Handle handle)
{
    Canon_Scanner *s = handle;
    s->scanning = SANE_FALSE;
    usb_close(s);
}

SANE_Status
sane_set_io_mode(SANE_Handle handle, SANE_Bool non_blocking)
{
    (void)handle;
    return non_blocking ? SANE_STATUS_UNSUPPORTED : SANE_STATUS_GOOD;
}

SANE_Status
sane_get_select_fd(SANE_Handle handle, SANE_Int *fd)
{
    (void)handle; (void)fd;
    return SANE_STATUS_UNSUPPORTED;
}

#ifdef CANON_DECODE_TEST
/* Standalone decode validator: reads a raw color-8bit frame, decodes NATIVE (no
 * resample), writes PPM. Build: gcc -DCANON_DECODE_TEST canon8000f.c -o dtest $(usb) -lm */
int main(int argc, char **argv)
{
    if (argc < 4) { fprintf(stderr, "usage: dtest raw W L\n"); return 1; }
    FILE *f = fopen(argv[1], "rb"); if (!f) return 1;
    int W = atoi(argv[2]), L = atoi(argv[3]);
    long rawlen = (long)W * L * 3;
    unsigned char *raw = malloc(rawlen);
    if (fread(raw, 1, rawlen, f) != (size_t)rawlen) { fprintf(stderr,"short read\n"); return 1; }
    fclose(f);
    Canon_Scanner s; memset(&s, 0, sizeof(s));
    /* compute nrows exactly as decode does, to force native (no-resample) output */
    double *d0=malloc((L-1)*sizeof(double)),*d1=malloc((L-1)*sizeof(double)),*d2=malloc((L-1)*sizeof(double));
    row_mean_diff(raw,L,W,8,0,d0); row_mean_diff(raw,L,W,8,1,d1); row_mean_diff(raw,L,W,8,2,d2);
    int lim=24; if(lim>L-2)lim=L-2;
    int s1=bshift(d0,d1,L-1,lim), s2=bshift(d0,d2,L-1,lim);
    int pad=0; if(s1>pad)pad=s1; if(s2>pad)pad=s2; int neg=0; if(-s1>neg)neg=-s1; if(-s2>neg)neg=-s2;
    int nrows=L-pad-neg;
    fprintf(stderr,"s1=%d s2=%d nrows=%d\n",s1,s2,nrows);
    strcpy(s.mode, SANE_VALUE_SCAN_MODE_COLOR); s.resolution=75; s.depth=8;
    s.tl_x=SANE_FIX(0.0); s.tl_y=SANE_FIX(0.0); s.br_x=SANE_FIX(BED_W_MM); s.br_y=SANE_FIX(BED_H_MM);
    s.params.format=SANE_FRAME_RGB; s.params.depth=8;
    s.params.pixels_per_line=W; s.params.lines=nrows; s.params.bytes_per_line=W*3;
    if (decode_to_frame(&s, raw, rawlen, 75, 2, 8) != SANE_STATUS_GOOD) { fprintf(stderr,"decode fail\n"); return 1; }
    FILE *o=fopen("/tmp/c_decode.ppm","wb");
    fprintf(o,"P6\n%d %d\n255\n",W,nrows);
    fwrite(s.frame,1,s.frame_len,o); fclose(o);
    fprintf(stderr,"wrote /tmp/c_decode.ppm %dx%d\n",W,nrows);
    return 0;
}
#endif

/* ---- backend-prefixed entry points ---------------------------------------
 *
 * SANE's `dll` meta-backend dlsym()s BACKEND-PREFIXED symbols
 * (sane_canon8000f_init, ...) and does NOT fall back to the plain names: every
 * operation silently becomes "unsupported" and the device never appears in
 * `scanimage -L`.
 *
 * The usual route is sanei_backend.h, which #defines the plain names away. We
 * keep both instead: the plain names stay exported so test_harness (and any
 * direct linker) can call them, and these thin wrappers give dll what it wants.
 * Adding an entry point means adding it here too.
 */
SANE_Status sane_canon8000f_init(SANE_Int *v, SANE_Auth_Callback cb) { return sane_init(v, cb); }
void        sane_canon8000f_exit(void)                               { sane_exit(); }
SANE_Status sane_canon8000f_get_devices(const SANE_Device ***dl, SANE_Bool local)
                                                                     { return sane_get_devices(dl, local); }
SANE_Status sane_canon8000f_open(SANE_String_Const name, SANE_Handle *h)
                                                                     { return sane_open(name, h); }
void        sane_canon8000f_close(SANE_Handle h)                     { sane_close(h); }
const SANE_Option_Descriptor *sane_canon8000f_get_option_descriptor(SANE_Handle h, SANE_Int n)
                                                                     { return sane_get_option_descriptor(h, n); }
SANE_Status sane_canon8000f_control_option(SANE_Handle h, SANE_Int n, SANE_Action a, void *val, SANE_Int *info)
                                                                     { return sane_control_option(h, n, a, val, info); }
SANE_Status sane_canon8000f_get_parameters(SANE_Handle h, SANE_Parameters *p)
                                                                     { return sane_get_parameters(h, p); }
SANE_Status sane_canon8000f_start(SANE_Handle h)                     { return sane_start(h); }
SANE_Status sane_canon8000f_read(SANE_Handle h, SANE_Byte *buf, SANE_Int maxlen, SANE_Int *len)
                                                                     { return sane_read(h, buf, maxlen, len); }
void        sane_canon8000f_cancel(SANE_Handle h)                    { sane_cancel(h); }
SANE_Status sane_canon8000f_set_io_mode(SANE_Handle h, SANE_Bool nb) { return sane_set_io_mode(h, nb); }
SANE_Status sane_canon8000f_get_select_fd(SANE_Handle h, SANE_Int *fd)
                                                                     { return sane_get_select_fd(h, fd); }
