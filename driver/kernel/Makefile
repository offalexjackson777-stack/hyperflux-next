obj-m += hid-hyperflux-next.o

hid-hyperflux-next-y := \
	hyperflux-next-core.o \
	hyperflux-next-observation.o \
	hyperflux-next-profile.o \
	hyperflux-next-session.o \
	hyperflux-next-transport.o

KDIR ?= /lib/modules/$(shell uname -r)/build
KERNEL_RELEASE ?= $(shell $(MAKE) --no-print-directory -s -C $(KDIR) kernelrelease)
MO ?= $(abspath $(CURDIR)/../../build/kernel/$(KERNEL_RELEASE))

.PHONY: all clean

all:
	@mkdir -p $(MO)
	$(MAKE) -C $(KDIR) M=$(CURDIR) MO=$(MO) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
	$(RM) -r $(MO)
