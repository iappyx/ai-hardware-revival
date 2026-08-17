# SANE backend for the Canon CanoScan 8000F (out-of-tree build).
#
#   make            build libsane-canon8000f.so.1
#   make test       build + run the no-hardware data-path check (CANON8000F_FAKE)
#   make install    install the .so and register it in dll.conf  (needs sudo)
#   make uninstall
#
# Deps: libsane-dev, libusb-1.0-0-dev, a C compiler.

NAME    := canon8000f
LIB     := libsane-$(NAME).so.1
SRC     := canon8000f.c

CC      ?= cc
CFLAGS  ?= -O2 -Wall -Wextra -fPIC
USBFLAGS := $(shell pkg-config --cflags libusb-1.0)
USBLIBS  := $(shell pkg-config --libs libusb-1.0)

# SANE backend + dll.conf locations.
#
# The dll meta-backend searches the libdir COMPILED INTO sane-backends and
# dlsym()s `libsane-<name>.<ver>.so` - note the version sits BEFORE `.so`, which
# is not the soname layout we build. Ask pkg-config for both rather than guessing;
# on Homebrew these resolve into the Cellar, not the /opt/homebrew symlink farm.
LIBDIR  ?= $(shell pkg-config --variable=libdir sane-backends 2>/dev/null || echo /usr/lib/$(shell gcc -dumpmachine))
SANELIBDIR ?= $(LIBDIR)/sane
# sane-backends.pc does not always export sysconfdir; fall back to $prefix/etc,
# which is where the dll backend looks, then to /etc.
SANE_PREFIX := $(shell pkg-config --variable=prefix sane-backends 2>/dev/null)
SYSCONFDIR ?= $(or $(shell pkg-config --variable=sysconfdir sane-backends 2>/dev/null),\
                   $(if $(SANE_PREFIX),$(SANE_PREFIX)/etc),/etc)
DLLCONF ?= $(SYSCONFDIR)/sane.d/dll.conf
DLLNAME := libsane-$(NAME).1.so

$(LIB): $(SRC) canon8000f_tables.h
	$(CC) -shared $(CFLAGS) $(USBFLAGS) $(SRC) -o $(LIB) $(USBLIBS) -lm
	ln -sf $(LIB) libsane-$(NAME).so


.PHONY: test install uninstall clean
test: $(LIB)
	$(CC) $(CFLAGS) test_harness.c $(LIB) -o test_harness -Wl,-rpath,'$$ORIGIN'
	CANON8000F_FAKE=1 ./test_harness

install: $(LIB)
	install -d $(SANELIBDIR)
	install -m 0755 $(LIB) $(SANELIBDIR)/$(DLLNAME)
	ln -sf $(DLLNAME) $(SANELIBDIR)/libsane-$(NAME).so
	grep -qx $(NAME) $(DLLCONF) || echo $(NAME) >> $(DLLCONF)
	@echo "installed to $(SANELIBDIR)/$(DLLNAME); test with:  scanimage -L"

uninstall:
	rm -f $(SANELIBDIR)/$(DLLNAME) $(SANELIBDIR)/$(LIB) $(SANELIBDIR)/libsane-$(NAME).so
	sed -i.bak '/^$(NAME)$$/d' $(DLLCONF) 2>/dev/null && rm -f $(DLLCONF).bak || true

clean:
	rm -f $(LIB) libsane-$(NAME).so test_harness
