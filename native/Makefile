CC ?= gcc
CFLAGS ?= -Wall -Wextra -O2
LDFLAGS ?=
LIBS := $(shell pkg-config --libs libusb-1.0 2>/dev/null || echo -lusb-1.0)
INCLUDES := $(shell pkg-config --cflags libusb-1.0 2>/dev/null)
PREFIX ?= /usr/local

gc-enabler: gc-enabler.c
	$(CC) $(CFLAGS) $(INCLUDES) -o $@ $< $(LDFLAGS) $(LIBS)

install: gc-enabler
	install -Dm755 gc-enabler $(DESTDIR)$(PREFIX)/bin/gc-enabler

clean:
	rm -f gc-enabler

.PHONY: install clean
