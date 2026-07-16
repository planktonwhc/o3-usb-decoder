#!/usr/bin/env bash
# pi_setup.sh — one-shot bring-up of a Raspberry Pi 4 as a USB MITM for DJI O3.
#
# RUN THIS ON THE PI (not the laptop), over SSH, after first boot. Idempotent — safe
# to re-run. It:
#   1. installs build deps + capture tools (usb-proxy, raw-gadget, tshark, ffmpeg)
#   2. enables the dwc2 USB-C gadget controller (dr_mode=peripheral) in config.txt
#   3. clones + builds xairy/raw-gadget and AristoChen/usb-proxy under ~/o3
#   4. loads the raw_gadget module and persists it across reboots
# After it finishes it will tell you whether a reboot is required (first run: yes,
# to activate dwc2).
#
# Full context + wiring/power/capture procedure: docs/pi4_mitm_setup.md
set -euo pipefail

WORK="${HOME}/o3"
# Raspberry Pi OS Bookworm puts boot config here; older releases use /boot/config.txt.
if [ -f /boot/firmware/config.txt ]; then
  BOOTCFG=/boot/firmware/config.txt
elif [ -f /boot/config.txt ]; then
  BOOTCFG=/boot/config.txt
else
  echo "!! cannot find boot config.txt — are you on a Raspberry Pi?" >&2; exit 1
fi

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

log "1/5  apt dependencies"
sudo apt-get update -y
sudo apt-get install -y \
  git build-essential bc bison flex libssl-dev \
  linux-headers-rpi-v8 \
  libusb-1.0-0-dev libjsoncpp-dev pkg-config \
  python3-usb python3-scapy \
  tshark ffmpeg usbutils

log "2/5  enable dwc2 gadget controller (USB-C peripheral mode)"
NEED_REBOOT=0
if ! grep -q '^dtoverlay=dwc2,dr_mode=peripheral' "$BOOTCFG"; then
  # Remove any conflicting dwc2/otg lines first, then add ours.
  sudo sed -i '/^dtoverlay=dwc2/d;/^otg_mode=1/d' "$BOOTCFG"
  echo 'dtoverlay=dwc2,dr_mode=peripheral' | sudo tee -a "$BOOTCFG" >/dev/null
  echo "   added dtoverlay=dwc2,dr_mode=peripheral to $BOOTCFG"
  NEED_REBOOT=1
else
  echo "   dwc2 peripheral overlay already present"
fi

log "3/5  build + load raw-gadget kernel module"
mkdir -p "$WORK"
if [ ! -d "$WORK/raw-gadget" ]; then
  git clone https://github.com/xairy/raw-gadget.git "$WORK/raw-gadget"
fi
make -C "$WORK/raw-gadget/raw_gadget"
# load now if the kernel is already running with headers matching; harmless if it
# needs the post-reboot kernel — we (re)load it below and via /etc/modules-load.d.
if [ -e /dev/raw-gadget ]; then
  echo "   /dev/raw-gadget already present"
else
  sudo insmod "$WORK/raw-gadget/raw_gadget/raw_gadget.ko" 2>/dev/null \
    && echo "   raw_gadget loaded" \
    || echo "   (raw_gadget will load after reboot — kernel/headers mismatch is normal on first run)"
fi
# Persist: install the .ko into the running kernel's extra dir + modules-load.d.
KREL="$(uname -r)"
sudo install -D -m644 "$WORK/raw-gadget/raw_gadget/raw_gadget.ko" \
  "/lib/modules/${KREL}/extra/raw_gadget.ko"
sudo depmod -a || true
echo 'raw_gadget' | sudo tee /etc/modules-load.d/raw_gadget.conf >/dev/null

log "4/5  build usb-proxy + apply O3 MITM patches (AOA natural-flow re-enum)"
# Prefer the repo's apply script when available (full O3 sources + follow_aoa_reenum).
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -x "$REPO_ROOT/scripts/apply_usb_proxy_o3.sh" ]; then
  USB_PROXY_DIR="$WORK/usb-proxy" "$REPO_ROOT/scripts/apply_usb_proxy_o3.sh"
else
  if [ ! -d "$WORK/usb-proxy" ]; then
    git clone https://github.com/AristoChen/usb-proxy.git "$WORK/usb-proxy"
  fi
  make -C "$WORK/usb-proxy"
fi
echo "   usb-proxy binary: $WORK/usb-proxy/usb-proxy"

log "5/5  sanity"
echo -n "   UDC present: "; ls /sys/class/udc/ 2>/dev/null || echo "(none yet — appears after reboot with dwc2)"
echo -n "   /dev/raw-gadget: "; ls -l /dev/raw-gadget 2>/dev/null || echo "(after reboot)"

if [ "$NEED_REBOOT" = "1" ]; then
  log "DONE — REBOOT REQUIRED to activate the USB-C gadget controller:  sudo reboot"
else
  log "DONE — setup complete. See docs/pi4_mitm_setup.md for the capture procedure."
fi
