#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sane/sane.h>
int main(void){
  SANE_Int ver; SANE_Handle h; const SANE_Device **devs;
  if (sane_init(&ver,0)) { puts("init fail"); return 1; }
  sane_get_devices(&devs,1);
  printf("devices: %s\n", devs[0]?devs[0]->name:"(none)");
  if (!devs[0]) return 2;
  if (sane_open(devs[0]->name,&h)) { puts("open fail"); return 3; }
  /* set 150 dpi color, a 100x150mm region */
  SANE_Word w; SANE_Int info;
  w=150; sane_control_option(h,3,SANE_ACTION_SET_VALUE,&w,&info);      /* resolution */
  SANE_Fixed f;
  f=SANE_FIX(0.0);   sane_control_option(h,6,SANE_ACTION_SET_VALUE,&f,&info); /* tl_x */
  f=SANE_FIX(0.0);   sane_control_option(h,7,SANE_ACTION_SET_VALUE,&f,&info); /* tl_y */
  f=SANE_FIX(100.0); sane_control_option(h,8,SANE_ACTION_SET_VALUE,&f,&info); /* br_x */
  f=SANE_FIX(150.0); sane_control_option(h,9,SANE_ACTION_SET_VALUE,&f,&info); /* br_y */
  SANE_Parameters p; sane_get_parameters(h,&p);
  printf("params: fmt=%d depth=%d ppl=%d bpl=%d lines=%d\n",p.format,p.depth,p.pixels_per_line,p.bytes_per_line,p.lines);
  if (sane_start(h)) { puts("start fail"); return 4; }
  FILE*out=fopen("/tmp/canon_test.ppm","wb");
  fprintf(out,"P6\n%d %d\n255\n",p.pixels_per_line,p.lines);
  SANE_Byte buf[65536]; SANE_Int n; long total=0;
  while (sane_read(h,buf,sizeof buf,&n)==SANE_STATUS_GOOD){ fwrite(buf,1,n,out); total+=n; }
  fclose(out);
  printf("read %ld bytes (expected %d)\n", total, p.bytes_per_line*p.lines);
  sane_close(h); sane_exit();
  return 0;
}
