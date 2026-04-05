#!/bin/bash
# Install the hid-nso-gc kernel module via DKMS.
# Requires: dkms, linux-headers for your kernel.
#
# Usage:
#   sudo ./install-dkms.sh          # install + build
#   sudo ./install-dkms.sh remove   # uninstall
set -euo pipefail

MODULE_NAME="hid-nso-gc"
MODULE_VERSION="0.1.0"
SRC_DIR="/usr/src/${MODULE_NAME}-${MODULE_VERSION}"

if [ "${1:-}" = "remove" ]; then
    echo "Removing ${MODULE_NAME} ${MODULE_VERSION}..."
    dkms remove "${MODULE_NAME}/${MODULE_VERSION}" --all 2>/dev/null || true
    rm -rf "${SRC_DIR}"
    echo "Done."
    exit 0
fi

echo "Installing ${MODULE_NAME} ${MODULE_VERSION} via DKMS..."

# Copy sources
mkdir -p "${SRC_DIR}"
cp -f "$(dirname "$0")/hid-nso-gc.c" "${SRC_DIR}/"
cp -f "$(dirname "$0")/Makefile"      "${SRC_DIR}/"
cp -f "$(dirname "$0")/dkms.conf"     "${SRC_DIR}/"

# Register, build, install
dkms add    "${MODULE_NAME}/${MODULE_VERSION}" 2>/dev/null || true
dkms build  "${MODULE_NAME}/${MODULE_VERSION}"
dkms install "${MODULE_NAME}/${MODULE_VERSION}"

echo ""
echo "Module installed. Load it with:"
echo "  sudo modprobe hid-nso-gc"
echo ""
echo "To load automatically at boot, add 'hid-nso-gc' to /etc/modules-load.d/:"
echo "  echo hid-nso-gc | sudo tee /etc/modules-load.d/hid-nso-gc.conf"
