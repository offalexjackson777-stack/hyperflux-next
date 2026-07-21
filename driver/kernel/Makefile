obj-m += hid-hyperflux-next.o

hid-hyperflux-next-y := \
	hyperflux-next-core.o \
	hyperflux-next-observation.o \
	hyperflux-next-profile.o \
	hyperflux-next-session.o \
	hyperflux-next-transport.o

KDIR ?= /lib/modules/$(shell uname -r)/build

.PHONY: all clean

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
