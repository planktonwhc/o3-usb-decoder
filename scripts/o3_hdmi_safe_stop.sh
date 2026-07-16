#!/usr/bin/env bash
# Safe stop/clear for o3-hdmi when UDC is stuck at 'configured' with goggles UNPLUGGED.
#
# Background: after a real unplug, dwc2/raw-gadget often leaves
#   /sys/class/udc/fe980000.usb/state == configured
# without delivering USB_RAW_EVENT_DISCONNECT. That "phantom host" used to:
#   - keep gold sustain writing → EAGAIN storm
#   - make install_hdmi_service.sh / operators refuse to stop (false "goggles attached")
#
# Gotcha #4 still applies when a *real* host is attached. This script is for the
# confirmed-unplugged / phantom case (default field recovery path).
#
# Usage (on Pi, as root):
#   sudo scripts/o3_hdmi_safe_stop.sh           # stop + clear phantom UDC
#   sudo scripts/o3_hdmi_safe_stop.sh --restart # stop, clear, start o3-hdmi
set -euo pipefail

UDC="${UDC:-fe980000.usb}"
DO_RESTART=0
for a in "$@"; do
  case "$a" in
    --restart) DO_RESTART=1 ;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "!! need root: sudo $0 $*" >&2
  exit 1
fi

state() { cat /sys/class/udc/"$UDC"/state 2>/dev/null || echo "?"; }

echo "== o3_hdmi_safe_stop (udc=$(state)) =="
echo "   NOTE: udc=configured does NOT prove goggles are plugged (phantom is common)."

# 1) Stop service + any manual endpoint (SIGINT first for clean Python finally).
systemctl stop o3-hdmi 2>/dev/null || true
pkill -INT -f 'poc/pi_endpoint/raw_endpoint.py' 2>/dev/null || true
pkill -INT -f 'phaseB_standalone' 2>/dev/null || true
sleep 1
pkill -9 -f 'poc/pi_endpoint/raw_endpoint.py' 2>/dev/null || true
pkill -9 -f 'phaseB_standalone' 2>/dev/null || true
pkill -x mpv 2>/dev/null || true
pkill -x gst-launch-1.0 2>/dev/null || true
pkill -f 'dual_drm_mirror.py' 2>/dev/null || true
pkill -f 'atomic_drm_present.py' 2>/dev/null || true
pkill -f 'idle_splash.py' 2>/dev/null || true
# Free raw-gadget node if anything still holds it.
if command -v fuser >/dev/null 2>&1; then
  fuser -k /dev/raw-gadget 2>/dev/null || true
fi
sleep 0.5

# 2) Soft-disconnect to clear stuck 'configured' when no process holds the UDC.
if [ -w /sys/class/udc/"$UDC"/soft_connect ]; then
  echo disconnect > /sys/class/udc/"$UDC"/soft_connect 2>/dev/null || true
  sleep 0.3
  echo connect > /sys/class/udc/"$UDC"/soft_connect 2>/dev/null || true
  sleep 0.2
fi

st="$(state)"
echo "   after clear: udc=$st  raw-holders: $(fuser /dev/raw-gadget 2>/dev/null || echo none)"

if [ "$DO_RESTART" -eq 1 ]; then
  echo "== starting o3-hdmi =="
  systemctl start o3-hdmi
  sleep 2
  systemctl --no-pager --full status o3-hdmi || true
  echo "   journal: journalctl -u o3-hdmi -f"
  echo "   expect: udc=not attached (or idle) until goggles plug; then AOA → atomic"
fi

echo "== done =="
