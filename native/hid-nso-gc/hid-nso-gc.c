// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * HID driver for the Nintendo Switch Online GameCube Controller (NSO GC)
 *
 *   VID 0x057E  PID 0x2073
 *
 * The NSO GC adapter multiplexes up to 4 independent GameCube controllers
 * over a single USB HID device.  Before it will emit input reports the host
 * must send two USB bulk transfers on interface 1 (the vendor-specific
 * interface).  After that, the device sends 64-byte HID input reports on
 * interface 0 containing the state of one controller.
 *
 * Report layout (after init, 64 bytes, report ID stripped by HID core):
 *   byte  2       — controller index (0-3, only one per report)
 *   byte  3       — buttons byte 0 (B A Y X R Z Start)
 *   byte  4       — buttons byte 1 (DDown DRight DLeft DUp L ZL)
 *   byte  5       — buttons byte 2 (Home Capture GR GL Chat)
 *   bytes 6-7     — left  stick X (12-bit LE, unsigned, center ~2048)
 *   bytes 7-8     — left  stick Y (12-bit, packed)
 *   bytes 9-10    — right stick X (12-bit LE)
 *   bytes 10-11   — right stick Y (12-bit, packed)
 *   byte  13      — left  trigger (0-255)
 *   byte  14      — right trigger (0-255)
 *
 * This driver:
 *   1. Sends the initialization bulk transfers in probe() via usb_bulk_msg.
 *   2. Creates up to 4 input devices (one per GC port).
 *   3. Parses raw_event() to dispatch inputs to the correct sub-device.
 *
 * Copyright (C) 2026  NSO-GameCube-Controller-Pairing-App contributors
 */

#include <linux/device.h>
#include <linux/hid.h>
#include <linux/input.h>
#include <linux/module.h>
#include <linux/slab.h>
#include <linux/usb.h>

#define NSO_GC_VENDOR_ID    0x057E
#define NSO_GC_PRODUCT_ID   0x2073
#define NSO_GC_MAX_PORTS    4
#define NSO_GC_INTF_INIT    1       /* vendor interface for init commands */

/* Initialization payloads — identical to the ones used by the userspace app */
static const u8 init_default_report[] = {
	0x03, 0x91, 0x00, 0x0d, 0x00, 0x08,
	0x00, 0x00, 0x01, 0x00, 0xFF, 0xFF,
	0xFF, 0xFF, 0xFF, 0xFF,
};
static const u8 init_led_report[] = {
	0x09, 0x91, 0x00, 0x07, 0x00, 0x08,
	0x00, 0x00, 0x01, 0x00, 0x00, 0x00,
	0x00, 0x00, 0x00, 0x00,
};

struct nso_gc_port {
	struct input_dev *input;
	bool present;
};

struct nso_gc {
	struct hid_device *hdev;
	struct nso_gc_port ports[NSO_GC_MAX_PORTS];
};

/* ── Input device setup ─────────────────────────────────────────── */

static int nso_gc_create_input(struct nso_gc *gc, int port)
{
	struct input_dev *input;
	int ret;

	input = input_allocate_device();
	if (!input)
		return -ENOMEM;

	input->name = "NSO GameCube Controller";
	input->phys = gc->hdev->phys;
	input->uniq = gc->hdev->uniq;
	input->id.bustype = BUS_USB;
	input->id.vendor  = NSO_GC_VENDOR_ID;
	input->id.product = NSO_GC_PRODUCT_ID;
	input->id.version = port;
	input->dev.parent = &gc->hdev->dev;

	/* Buttons */
	set_bit(EV_KEY, input->evbit);
	set_bit(BTN_A, input->keybit);
	set_bit(BTN_B, input->keybit);
	set_bit(BTN_X, input->keybit);
	set_bit(BTN_Y, input->keybit);
	set_bit(BTN_TL, input->keybit);      /* L shoulder */
	set_bit(BTN_TR, input->keybit);      /* R shoulder */
	set_bit(BTN_TR2, input->keybit);     /* Z */
	set_bit(BTN_TL2, input->keybit);     /* ZL */
	set_bit(BTN_START, input->keybit);
	set_bit(BTN_MODE, input->keybit);    /* Home */
	set_bit(BTN_SELECT, input->keybit);  /* Capture */
	set_bit(BTN_THUMBL, input->keybit);  /* GL */
	set_bit(BTN_THUMBR, input->keybit);  /* GR */

	/* Axes */
	set_bit(EV_ABS, input->evbit);
	/* Left stick (12-bit, 0-4095, center ~2048) */
	input_set_abs_params(input, ABS_X,  0, 4095, 16, 128);
	input_set_abs_params(input, ABS_Y,  0, 4095, 16, 128);
	/* Right stick */
	input_set_abs_params(input, ABS_RX, 0, 4095, 16, 128);
	input_set_abs_params(input, ABS_RY, 0, 4095, 16, 128);
	/* Triggers (0-255) */
	input_set_abs_params(input, ABS_Z,  0, 255, 0, 0);
	input_set_abs_params(input, ABS_RZ, 0, 255, 0, 0);
	/* D-Pad */
	input_set_abs_params(input, ABS_HAT0X, -1, 1, 0, 0);
	input_set_abs_params(input, ABS_HAT0Y, -1, 1, 0, 0);

	ret = input_register_device(input);
	if (ret) {
		input_free_device(input);
		return ret;
	}

	gc->ports[port].input = input;
	gc->ports[port].present = false;
	return 0;
}

/* ── USB initialization ──────────────────────────────────────────── */

static int nso_gc_send_init(struct hid_device *hdev)
{
	struct usb_device *udev = hid_to_usb_dev(hdev);
	struct usb_host_interface *alt;
	struct usb_interface *intf;
	int ep_out = -1;
	int actual_len;
	int ret;
	int i;
	u8 *buf;

	/* Find interface 1 (vendor-specific, bulk OUT) */
	intf = usb_ifnum_to_if(udev, NSO_GC_INTF_INIT);
	if (!intf) {
		hid_warn(hdev, "Interface %d not found, skipping init\n",
			 NSO_GC_INTF_INIT);
		return 0;
	}

	alt = intf->cur_altsetting;
	for (i = 0; i < alt->desc.bNumEndpoints; i++) {
		struct usb_endpoint_descriptor *ep = &alt->endpoint[i].desc;

		if (usb_endpoint_is_bulk_out(ep)) {
			ep_out = usb_endpoint_num(ep);
			break;
		}
	}

	if (ep_out < 0) {
		hid_warn(hdev, "No bulk OUT endpoint on interface %d\n",
			 NSO_GC_INTF_INIT);
		return 0;
	}

	buf = kmalloc(max(sizeof(init_default_report),
			  sizeof(init_led_report)), GFP_KERNEL);
	if (!buf)
		return -ENOMEM;

	/* Send DEFAULT_REPORT_DATA */
	memcpy(buf, init_default_report, sizeof(init_default_report));
	ret = usb_bulk_msg(udev,
			   usb_sndbulkpipe(udev, ep_out),
			   buf, sizeof(init_default_report),
			   &actual_len, 1000);
	if (ret)
		hid_warn(hdev, "Init report failed: %d\n", ret);

	/* Send SET_LED_DATA */
	memcpy(buf, init_led_report, sizeof(init_led_report));
	ret = usb_bulk_msg(udev,
			   usb_sndbulkpipe(udev, ep_out),
			   buf, sizeof(init_led_report),
			   &actual_len, 1000);
	if (ret)
		hid_warn(hdev, "LED report failed: %d\n", ret);

	kfree(buf);
	return ret;
}

/* ── HID raw_event parsing ───────────────────────────────────────── */

static int nso_gc_raw_event(struct hid_device *hdev,
			    struct hid_report *report,
			    u8 *data, int size)
{
	struct nso_gc *gc = hid_get_drvdata(hdev);
	struct input_dev *input;
	int port;
	u16 lx, ly, rx, ry;
	u8 b0, b1, b2;

	if (size < 15)
		return 0;

	port = data[2];
	if (port < 0 || port >= NSO_GC_MAX_PORTS)
		return 0;

	input = gc->ports[port].input;
	if (!input)
		return 0;

	b0 = data[3];
	b1 = data[4];
	b2 = data[5];

	/* 12-bit packed stick values */
	lx = data[6] | ((data[7] & 0x0F) << 8);
	ly = ((data[7] >> 4) & 0x0F) | (data[8] << 4);
	rx = data[9] | ((data[10] & 0x0F) << 8);
	ry = ((data[10] >> 4) & 0x0F) | (data[11] << 4);

	/* Buttons — byte 3 */
	input_report_key(input, BTN_B,     !!(b0 & 0x01));
	input_report_key(input, BTN_A,     !!(b0 & 0x02));
	input_report_key(input, BTN_Y,     !!(b0 & 0x04));
	input_report_key(input, BTN_X,     !!(b0 & 0x08));
	input_report_key(input, BTN_TR,    !!(b0 & 0x10));  /* R digital */
	input_report_key(input, BTN_TR2,   !!(b0 & 0x20));  /* Z */
	input_report_key(input, BTN_START, !!(b0 & 0x40));

	/* Buttons — byte 4 */
	input_report_abs(input, ABS_HAT0Y,
			 (!!(b1 & 0x01)) - (!!(b1 & 0x08)));  /* down - up */
	input_report_abs(input, ABS_HAT0X,
			 (!!(b1 & 0x02)) - (!!(b1 & 0x04)));  /* right - left */
	input_report_key(input, BTN_TL,    !!(b1 & 0x10));  /* L digital */
	input_report_key(input, BTN_TL2,   !!(b1 & 0x20));  /* ZL */

	/* Buttons — byte 5 */
	input_report_key(input, BTN_MODE,   !!(b2 & 0x01)); /* Home */
	input_report_key(input, BTN_SELECT, !!(b2 & 0x02)); /* Capture */
	input_report_key(input, BTN_THUMBR, !!(b2 & 0x04)); /* GR */
	input_report_key(input, BTN_THUMBL, !!(b2 & 0x08)); /* GL */

	/* Sticks */
	input_report_abs(input, ABS_X,  lx);
	input_report_abs(input, ABS_Y,  ly);
	input_report_abs(input, ABS_RX, rx);
	input_report_abs(input, ABS_RY, ry);

	/* Triggers */
	input_report_abs(input, ABS_Z,  data[13]);
	input_report_abs(input, ABS_RZ, data[14]);

	input_sync(input);
	return 0;
}

/* ── Probe / Remove ──────────────────────────────────────────────── */

static int nso_gc_probe(struct hid_device *hdev,
			const struct hid_device_id *id)
{
	struct nso_gc *gc;
	int ret, i;

	gc = devm_kzalloc(&hdev->dev, sizeof(*gc), GFP_KERNEL);
	if (!gc)
		return -ENOMEM;

	gc->hdev = hdev;
	hid_set_drvdata(hdev, gc);

	ret = hid_parse(hdev);
	if (ret) {
		hid_err(hdev, "HID parse failed: %d\n", ret);
		return ret;
	}

	ret = hid_hw_start(hdev, HID_CONNECT_HIDRAW);
	if (ret) {
		hid_err(hdev, "HID hw start failed: %d\n", ret);
		return ret;
	}

	ret = hid_hw_open(hdev);
	if (ret) {
		hid_err(hdev, "HID hw open failed: %d\n", ret);
		goto stop;
	}

	/* Send USB init commands */
	nso_gc_send_init(hdev);

	/* Create per-port input devices */
	for (i = 0; i < NSO_GC_MAX_PORTS; i++) {
		ret = nso_gc_create_input(gc, i);
		if (ret) {
			hid_err(hdev, "Failed to create input for port %d: %d\n",
				i, ret);
			goto destroy_inputs;
		}
	}

	hid_info(hdev, "NSO GameCube Controller initialized (%d ports)\n",
		 NSO_GC_MAX_PORTS);
	return 0;

destroy_inputs:
	for (i = 0; i < NSO_GC_MAX_PORTS; i++) {
		if (gc->ports[i].input)
			input_unregister_device(gc->ports[i].input);
	}
	hid_hw_close(hdev);
stop:
	hid_hw_stop(hdev);
	return ret;
}

static void nso_gc_remove(struct hid_device *hdev)
{
	struct nso_gc *gc = hid_get_drvdata(hdev);
	int i;

	for (i = 0; i < NSO_GC_MAX_PORTS; i++) {
		if (gc->ports[i].input)
			input_unregister_device(gc->ports[i].input);
	}

	hid_hw_close(hdev);
	hid_hw_stop(hdev);
}

/* ── Module boilerplate ──────────────────────────────────────────── */

static const struct hid_device_id nso_gc_devices[] = {
	{ HID_USB_DEVICE(NSO_GC_VENDOR_ID, NSO_GC_PRODUCT_ID) },
	{ }
};
MODULE_DEVICE_TABLE(hid, nso_gc_devices);

static struct hid_driver nso_gc_driver = {
	.name       = "hid-nso-gc",
	.id_table   = nso_gc_devices,
	.probe      = nso_gc_probe,
	.remove     = nso_gc_remove,
	.raw_event  = nso_gc_raw_event,
};
module_hid_driver(nso_gc_driver);

MODULE_LICENSE("GPL");
MODULE_AUTHOR("NSO-GameCube-Controller-Pairing-App contributors");
MODULE_DESCRIPTION("HID driver for the Nintendo Switch Online GameCube Controller");
