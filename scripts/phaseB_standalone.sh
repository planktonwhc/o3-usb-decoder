#!/usr/bin/env bash
# Phase B — standalone O3 video endpoint on the Pi (NO phone, NO DJI Fly).
# Runs ON THE Pi. Emulates the phone's AOA accessory so the goggles converse + stream,
# then deframes 55 CC 4A 57 → H.264. See poc/pi_endpoint/raw_endpoint.py.
#
#   sudo scripts/phaseB_standalone.sh -v            # AOA handshake → 2d01 (default)
#   sudo scripts/phaseB_standalone.sh -v --hdmi     # + live decode → micro-HDMI monitor
#   sudo scripts/phaseB_standalone.sh -v --cold-2d01  # old direct-2d01 path (A/B compare)
#
# --hdmi decodes the live stream to the Pi's HDMI (mpv → DRM/KMS, no X) while still
# recording the .h264 below. For headless boot autostart, see scripts/install_hdmi_service.sh.
#
# ┌─ TEARDOWN ORDER (gotcha #4 — do NOT skip) ──────────────────────────────┐
# │  UNPLUG THE GOGGLES **BEFORE** you Ctrl-C this script.                    │
# │  Stopping raw-gadget while the goggles are attached oopses dwc2 and wedges │
# │  the UDC → only a reboot clears it. Also true of the re-enum step below.  │
# └──────────────────────────────────────────────────────────────────────────┘
#
# Preconditions: air unit powered ON, goggles showing live video, VBUS-isolated
# goggles→Pi USB-C cable, Pi powered from GPIO/PoE (not the USB-C data port).
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
UDC="${UDC:-fe980000.usb}"
CAPDIR="${CAPDIR:-$REPO/captures/pi_pull}"
TS="$(date +%Y-%m-%d_%H%M%S)"
H264="$CAPDIR/${TS}_phaseB_standalone.h264"
LOG="$CAPDIR/${TS}_phaseB_standalone.log"

if [ "$(id -u)" -ne 0 ]; then
  echo "!! need root:  sudo $0 $*" >&2
  exit 1
fi
mkdir -p "$CAPDIR"

echo "== Phase B standalone bench  ($TS) =="

# --- preflight 1: raw-gadget + dwc2 present -------------------------------------
modprobe raw_gadget 2>/dev/null || true
modprobe dwc2 2>/dev/null || true
if [ ! -e /dev/raw-gadget ]; then
  echo "!! /dev/raw-gadget missing — is CONFIG_USB_RAW_GADGET / raw_gadget loaded?" >&2
  echo "   try: sudo modprobe raw_gadget ; and ensure dwc2 overlay in /boot config" >&2
  exit 1
fi

# --- preflight 2: UDC not wedged ------------------------------------------------
STATE_F="/sys/class/udc/$UDC/state"
if [ -r "$STATE_F" ]; then
  STATE="$(cat "$STATE_F" 2>/dev/null || echo '?')"
  echo "   UDC $UDC state: $STATE  (expect 'not attached' until goggles plug in)"
  echo "   NOTE: state=configured alone ≠ goggles plugged (phantom after unplug is common)."
  # A function bound but stuck usually means a prior wedge / leftover o3-hdmi.
  if [ -e "/sys/class/udc/$UDC/function" ]; then
    FN="$(cat "/sys/class/udc/$UDC/function" 2>/dev/null || true)"
    if [ -n "$FN" ]; then
      echo "!! UDC already has a function bound ('$FN') — clear with safe_stop first:" >&2
      echo "     sudo scripts/o3_hdmi_safe_stop.sh" >&2
      echo "   If still stuck after that, reboot." >&2
      exit 1
    fi
  fi
else
  echo "   (no $STATE_F — cannot read UDC state; continuing)"
fi

# --- preflight 3: no stale endpoint / proxy holding raw-gadget ------------------
# Use -x (exact binary) so we don't self-match this script's command line.
if pgrep -x usb-proxy >/dev/null 2>&1; then
  echo "!! usb-proxy is running — it holds raw-gadget. Unplug goggles, then: sudo pkill -x usb-proxy" >&2
  exit 1
fi
if fuser /dev/raw-gadget >/dev/null 2>&1; then
  echo "!! something already holds /dev/raw-gadget:"; fuser -v /dev/raw-gadget 2>&1 || true
  echo "   unplug goggles and stop it before retrying." >&2
  exit 1
fi

# --- preflight 4: undervoltage warning (Pi gotcha) ------------------------------
if command -v vcgencmd >/dev/null 2>&1; then
  THR="$(vcgencmd get_throttled 2>/dev/null | sed 's/throttled=//')"
  echo "   throttled=$THR  (0x0=OK; bit0/0x1=active undervoltage → gadget wedges easily)"
fi

cat <<'EOF'

  ┌──────────────────────────────────────────────────────────────┐
  │  UNPLUG GOGGLES **BEFORE** Ctrl-C.  (dwc2 oops → reboot)        │
  └──────────────────────────────────────────────────────────────┘
  Watch the log for, in order:
    [*] CONNECT (mode=normal)         goggles enumerated us as a normal device
    AOA SEND_STRING[0..5] = ...       goggles driving the AOA handshake
    [*] AOA ACCESSORY_START ...       → re-enumerating as accessory 18d1:2d01
    [*] CONNECT (mode=accessory)      goggles re-enumerated the accessory
    bulk OUT <n>B head=55cc4a57...    VIDEO flowing  ← the win
  If it stalls after SET_CONFIGURATION in normal mode with no SEND_STRING, the
  goggles aren't probing AOA — capture their control requests and revisit.

EOF

echo "== starting endpoint. PLUG GOGGLES INTO Pi USB-C (VBUS-isolated) NOW. =="
echo "   H.264 → $H264"
echo "   log   → $LOG"
echo

# stdbuf -oL keeps the tee'd log live over SSH.
stdbuf -oL -eL python3 "$REPO/poc/pi_endpoint/raw_endpoint.py" \
  --driver="$UDC" --device="$UDC" --out="$H264" "$@" 2>&1 | stdbuf -oL tee "$LOG"

echo
echo "== endpoint exited =="
ls -la "$H264" "$LOG" 2>/dev/null || true
if [ -s "$H264" ]; then
  echo
  echo "Validate the capture:"
  echo "  ffprobe -hide_banner $H264"
  echo "  ffplay  -f h264 $H264            # or pull to laptop first"
  echo "  python3 $REPO/analysis/extract_video_stream.py $H264   # if further peel needed"
else
  echo "  (no video bytes written — check the log above against the expected sequence)"
fi
echo "Pull to laptop:  rsync -az $USER@\$(hostname).local:$H264 ./"
