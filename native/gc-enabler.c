/*
 * gc-enabler — NSO GameCube Controller USB Initializer
 *
 * Sends the two USB bulk transfers required to activate HID input reports
 * on the Nintendo Switch Online GameCube controller (VID 057E, PID 2073).
 * After initialization the controller behaves as a standard HID gamepad
 * and this program can exit — no resident process is needed.
 *
 * Build:
 *   gcc -o gc-enabler gc-enabler.c -lusb-1.0
 *   (Windows: link against libusb-1.0.lib)
 *
 * Usage:
 *   gc-enabler          # init first matching controller and exit
 *   gc-enabler --wait   # poll until a controller appears, then init
 *
 * License: GPLv3 (same as parent project)
 */

#include <libusb-1.0/libusb.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef _WIN32
#include <windows.h>
#define usleep(us) Sleep((us) / 1000)
#else
#include <unistd.h>
#endif

#define VENDOR_ID   0x057E
#define INTERFACE   1

static const uint16_t PRODUCT_IDS[] = {0x2073};
#define NUM_PIDS (sizeof(PRODUCT_IDS) / sizeof(PRODUCT_IDS[0]))

static const unsigned char DEFAULT_REPORT_DATA[] = {
    0x03, 0x91, 0x00, 0x0d, 0x00, 0x08,
    0x00, 0x00, 0x01, 0x00, 0xFF, 0xFF,
    0xFF, 0xFF, 0xFF, 0xFF
};

static const unsigned char SET_LED_DATA[] = {
    0x09, 0x91, 0x00, 0x07, 0x00, 0x08,
    0x00, 0x00, 0x01, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00
};

static int find_bulk_out_endpoint(libusb_device_handle *handle, uint8_t *ep_out)
{
    libusb_device *dev = libusb_get_device(handle);
    struct libusb_config_descriptor *config;
    if (libusb_get_config_descriptor(dev, 0, &config) != 0)
        return -1;

    for (int i = 0; i < config->bNumInterfaces; i++) {
        const struct libusb_interface *iface = &config->interface[i];
        for (int j = 0; j < iface->num_altsetting; j++) {
            const struct libusb_interface_descriptor *alt = &iface->altsetting[j];
            if (alt->bInterfaceNumber != INTERFACE)
                continue;
            for (int k = 0; k < alt->bNumEndpoints; k++) {
                const struct libusb_endpoint_descriptor *ep = &alt->endpoint[k];
                if ((ep->bmAttributes & LIBUSB_TRANSFER_TYPE_MASK) == LIBUSB_TRANSFER_TYPE_BULK &&
                    (ep->bEndpointAddress & LIBUSB_ENDPOINT_DIR_MASK) == LIBUSB_ENDPOINT_OUT) {
                    *ep_out = ep->bEndpointAddress;
                    libusb_free_config_descriptor(config);
                    return 0;
                }
            }
        }
    }
    libusb_free_config_descriptor(config);
    return -1;
}

static int init_controller(libusb_device_handle *handle)
{
    int rc;

#ifdef __APPLE__
    if (libusb_kernel_driver_active(handle, INTERFACE) == 1)
        libusb_detach_kernel_driver(handle, INTERFACE);
#endif

    rc = libusb_claim_interface(handle, INTERFACE);
    if (rc != 0) {
        fprintf(stderr, "Cannot claim interface %d: %s\n", INTERFACE, libusb_error_name(rc));
        return -1;
    }

    uint8_t ep_out = 0;
    if (find_bulk_out_endpoint(handle, &ep_out) != 0) {
        fprintf(stderr, "No bulk OUT endpoint found on interface %d\n", INTERFACE);
        libusb_release_interface(handle, INTERFACE);
        return -1;
    }

    int transferred;
    rc = libusb_bulk_transfer(handle, ep_out,
                              (unsigned char *)DEFAULT_REPORT_DATA,
                              sizeof(DEFAULT_REPORT_DATA),
                              &transferred, 1000);
    if (rc != 0) {
        fprintf(stderr, "Failed to send init report: %s\n", libusb_error_name(rc));
        libusb_release_interface(handle, INTERFACE);
        return -1;
    }

    rc = libusb_bulk_transfer(handle, ep_out,
                              (unsigned char *)SET_LED_DATA,
                              sizeof(SET_LED_DATA),
                              &transferred, 1000);
    if (rc != 0) {
        fprintf(stderr, "Failed to send LED report: %s\n", libusb_error_name(rc));
        libusb_release_interface(handle, INTERFACE);
        return -1;
    }

    libusb_release_interface(handle, INTERFACE);
    return 0;
}

int main(int argc, char *argv[])
{
    int wait_mode = 0;
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--wait") == 0)
            wait_mode = 1;
    }

    libusb_context *ctx = NULL;
    if (libusb_init(&ctx) != 0) {
        fprintf(stderr, "libusb_init failed\n");
        return 1;
    }

    libusb_device_handle *handle = NULL;
    uint16_t found_pid = 0;

    do {
        for (size_t i = 0; i < NUM_PIDS; i++) {
            handle = libusb_open_device_with_vid_pid(ctx, VENDOR_ID, PRODUCT_IDS[i]);
            if (handle) {
                found_pid = PRODUCT_IDS[i];
                break;
            }
        }
        if (!handle && wait_mode)
            usleep(500 * 1000);
    } while (!handle && wait_mode);

    if (!handle) {
        fprintf(stderr, "No NSO GameCube controller found (VID %04X)\n", VENDOR_ID);
        libusb_exit(ctx);
        return 1;
    }

    printf("Found controller (PID %04X), initializing...\n", found_pid);

    int ret = init_controller(handle);
    libusb_close(handle);
    libusb_exit(ctx);

    if (ret == 0)
        printf("Controller enabled. You can now use it as a standard HID gamepad.\n");

    return ret == 0 ? 0 : 1;
}
