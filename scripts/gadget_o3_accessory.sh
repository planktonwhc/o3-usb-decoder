#!/usr/bin/env bash
# Bring up a USB gadget on the Pi 4 dwc2 UDC that presents as Google AOA accessory
# 18d1:2d01 (accessory + ADB product id — what the phone becomes after ACCESSORY_START).
#
# Uses configfs + FunctionFS. Userspace (poc/pi_endpoint/endpoint.py) must open the
# FunctionFS mount and write descriptors before the UDC is bound.
#
# Usage (on the Pi, as root):
#   ./scripts/gadget_o3_accessory.sh start
#   ./scripts/gadget_o3_accessory.sh stop
#   ./scripts/gadget_o3_accessory.sh status
set -euo pipefail

GADGET=o3acc
GDIR="/sys/kernel/config/usb_gadget/${GADGET}"
FFS_DIR="/dev/ffs-o3acc"
UDC_DEVICE="${UDC_DEVICE:-fe980000.usb}"

log() { printf '\033[1;36m==> %s\033[0m\n' "$*"; }

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "!! run as root (sudo)" >&2
    exit 1
  fi
}

cmd_stop() {
  need_root
  if [ -d "$GDIR" ]; then
    log "stopping gadget $GADGET"
    echo "" > "$GDIR/UDC" 2>/dev/null || true
    # shellcheck disable=SC2115
    rm -f "$GDIR/configs/c.1/"* 2>/dev/null || true
    rmdir "$GDIR/configs/c.1/strings/0x409" 2>/dev/null || true
    rmdir "$GDIR/configs/c.1" 2>/dev/null || true
    rmdir "$GDIR/functions/ffs.o3acc" 2>/dev/null || true
    rmdir "$GDIR/strings/0x409" 2>/dev/null || true
    rmdir "$GDIR" 2>/dev/null || true
  fi
  if mountpoint -q "$FFS_DIR" 2>/dev/null; then
    umount "$FFS_DIR" || true
  fi
  rmdir "$FFS_DIR" 2>/dev/null || true
  log "stopped"
}

cmd_start() {
  need_root
  cmd_stop 2>/dev/null || true

  if [ ! -d /sys/kernel/config/usb_gadget ]; then
    modprobe libcomposite
  fi
  if [ ! -d /sys/class/udc/"$UDC_DEVICE" ] && [ ! -e /sys/class/udc/"$UDC_DEVICE" ]; then
    echo "!! UDC $UDC_DEVICE not present. ls /sys/class/udc/:" >&2
    ls /sys/class/udc/ >&2 || true
    echo "   ensure dtoverlay=dwc2,dr_mode=peripheral and reboot" >&2
    exit 1
  fi

  log "creating FunctionFS mount $FFS_DIR"
  mkdir -p "$FFS_DIR"
  # functionfs instance name must match functions/ffs.<name>
  mount -t functionfs o3acc "$FFS_DIR"

  log "creating configfs gadget $GDIR"
  mkdir -p "$GDIR"
  # Google accessory VID/PID (phone after ACCESSORY_START = 18d1:2d01)
  echo 0x18d1 > "$GDIR/idVendor"
  echo 0x2d01 > "$GDIR/idProduct"
  echo 0x0200 > "$GDIR/bcdUSB"
  echo 0x0100 > "$GDIR/bcdDevice"
  # per-interface class (FunctionFS provides interfaces)
  echo 0x00 > "$GDIR/bDeviceClass"
  echo 0x00 > "$GDIR/bDeviceSubClass"
  echo 0x00 > "$GDIR/bDeviceProtocol"

  mkdir -p "$GDIR/strings/0x409"
  echo "DJI" > "$GDIR/strings/0x409/manufacturer"
  echo "com.dji.logiclink" > "$GDIR/strings/0x409/product"
  echo "o3-pi-endpoint" > "$GDIR/strings/0x409/serialnumber"

  mkdir -p "$GDIR/functions/ffs.o3acc"
  mkdir -p "$GDIR/configs/c.1/strings/0x409"
  echo "DJI glass" > "$GDIR/configs/c.1/strings/0x409/configuration"
  echo 500 > "$GDIR/configs/c.1/MaxPower"
  ln -s "$GDIR/functions/ffs.o3acc" "$GDIR/configs/c.1/"

  log "gadget config ready — NOT bound to UDC yet"
  log "next: run poc/pi_endpoint/endpoint.py (it writes FFS descriptors, then binds UDC)"
  echo "FFS_DIR=$FFS_DIR"
  echo "GDIR=$GDIR"
  echo "UDC=$UDC_DEVICE"
}

cmd_bind() {
  need_root
  if [ ! -d "$GDIR" ]; then
    echo "!! gadget not created; run start first" >&2
    exit 1
  fi
  # Only bind if FunctionFS ep0 has been configured by userspace
  if [ ! -e "$FFS_DIR/ep0" ]; then
    echo "!! $FFS_DIR/ep0 missing — start endpoint.py first so it opens FunctionFS" >&2
    exit 1
  fi
  log "binding UDC $UDC_DEVICE"
  echo "$UDC_DEVICE" > "$GDIR/UDC"
  cat "$GDIR/UDC"
}

cmd_status() {
  echo "UDC list: $(ls /sys/class/udc/ 2>/dev/null | tr '\n' ' ')"
  if [ -d "$GDIR" ]; then
    echo "gadget: $GDIR"
    echo "  UDC bound: $(cat "$GDIR/UDC" 2>/dev/null || echo none)"
    echo "  idVendor=$(cat "$GDIR/idVendor" 2>/dev/null) idProduct=$(cat "$GDIR/idProduct" 2>/dev/null)"
  else
    echo "gadget: not created"
  fi
  if mountpoint -q "$FFS_DIR" 2>/dev/null; then
    echo "ffs: mounted at $FFS_DIR ($(ls "$FFS_DIR" 2>/dev/null | tr '\n' ' '))"
  else
    echo "ffs: not mounted"
  fi
}

case "${1:-}" in
  start)  cmd_start ;;
  stop)   cmd_stop ;;
  bind)   cmd_bind ;;
  status) cmd_status ;;
  *)
    echo "usage: $0 {start|stop|bind|status}"
    exit 1
    ;;
esac
