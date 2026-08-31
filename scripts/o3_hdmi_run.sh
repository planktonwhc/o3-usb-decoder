#!/usr/bin/env bash
# Long-running O3 → HDMI worker for systemd (o3-hdmi.service).
# Idempotent bring-up: load modules, wait for UDC/DRM, free any stale holder,
# then exec the accessory endpoint (HDMI-only — no all-day disk fill).
#
# Invoked by systemd as root. Do not run this by hand unless debugging; prefer:
#   sudo systemctl start o3-hdmi
#   journalctl -u o3-hdmi -f
set -euo pipefail

REPO="${O3_REPO:-/home/pi/o3test}"
UDC="${UDC:-fe980000.usb}"
DRM_DEVICE="${DRM_DEVICE:-/dev/dri/card1}"
# Field default: far micro-HDMI only (atomic presenter). Dual is parked.
DRM_CONNECTOR="${DRM_CONNECTOR:-auto}"
PY="${O3_PYTHON:-/usr/bin/python3}"
ENDPOINT="$REPO/poc/pi_endpoint/raw_endpoint.py"

log() { echo "[o3-hdmi] $*"; }

# --- modules -----------------------------------------------------------------
modprobe raw_gadget 2>/dev/null || true
modprobe dwc2 2>/dev/null || true

# --- wait for hardware (boot races: DRM + UDC can lag multi-user.target) -----
deadline=$((SECONDS + 90))
while true; do
  ok=1
  [[ -e /dev/raw-gadget ]] || ok=0
  [[ -e /sys/class/udc/$UDC ]] || ok=0
  [[ -e "$DRM_DEVICE" ]] || ok=0
  if [[ $ok -eq 1 ]]; then
    break
  fi
  if (( SECONDS >= deadline )); then
    log "timeout waiting for raw-gadget / UDC $UDC / $DRM_DEVICE"
    ls -la /dev/raw-gadget /sys/class/udc/"$UDC" "$DRM_DEVICE" 2>&1 || true
    exit 1
  fi
  sleep 1
done

# --- free stale holder of /dev/raw-gadget (prior crash / manual bench) --------
if command -v fuser >/dev/null 2>&1 && fuser /dev/raw-gadget >/dev/null 2>&1; then
  log "freeing stale /dev/raw-gadget holder"
  fuser -k /dev/raw-gadget 2>/dev/null || true
  sleep 1
fi

# --- free stale DRM players (prior bench left mpv / gst holding the CRTCs) ----
pkill -x mpv 2>/dev/null || true
pkill -x gst-launch-1.0 2>/dev/null || true

# --- undervoltage heads-up (does not abort; gadget is fragile when throttled) -
if command -v vcgencmd >/dev/null 2>&1; then
  thr="$(vcgencmd get_throttled 2>/dev/null || true)"
  arm_hz="$(vcgencmd measure_clock arm 2>/dev/null || true)"
  log "preflight $thr  $arm_hz  udc=$(cat /sys/class/udc/$UDC/state 2>/dev/null || echo '?')  drm=$DRM_DEVICE connector=$DRM_CONNECTOR"
  # Current flags live in the low nibble (bit0 under-voltage, bit2 throttled).
  # 0x50000 is historical only; 0x50005 is starving NOW. SW decode + dwc2 both
  # fall apart at 600 MHz (2026-08-31: seconds of HDMI lag, USB SUSPEND).
  low="${thr##*x}"
  case "${low: -1}" in
    0) ;;
    *) log "WARN under-voltage/throttle active — use a stiffer 5V supply (GPIO/PoE, not the USB-C data port)" ;;
  esac
fi

# Log which HDMI ports look connected right now (player also hotplug-tracks).
# Include EDID size — 0 B with status=connected is a known phantom that used to
# force dual GStreamer and leave both screens on the Linux console.
if [[ -d /sys/class/drm ]]; then
  card="$(basename "$DRM_DEVICE")"
  for s in /sys/class/drm/${card}-HDMI-*/status; do
    [[ -e "$s" ]] || continue
    dir="$(dirname "$s")"
    name="$(basename "$dir")"
    st="$(cat "$s" 2>/dev/null || echo '?')"
    edid_bytes=0
    if [[ -r "$dir/edid" ]]; then
      edid_bytes="$(wc -c <"$dir/edid" 2>/dev/null | tr -d ' ' || true)"
      [[ -n "$edid_bytes" ]] || edid_bytes=0
    fi
    modes_n=0
    if [[ -r "$dir/modes" ]]; then
      # grep -c exits 1 on zero matches — don't append a second "0" via || echo.
      modes_n="$(grep -c . "$dir/modes" 2>/dev/null || true)"
      [[ -n "$modes_n" ]] || modes_n=0
    fi
    log "drm $name=$st edid=${edid_bytes}B modes=${modes_n}"
  done
fi

log "starting endpoint (hdmi-only, AOA handshake → 2d01, connector=$DRM_CONNECTOR)"
# --out '' = no recording (HDMI-only). Endpoint reconnects on goggles unplug/replug
# without exiting; non-zero exit → systemd Restart=on-failure.
# Dual atomic default: O3_DUAL_HDMI=1 + DRM_CONNECTOR=auto (both micro-HDMI).
exec "$PY" "$ENDPOINT" \
  --driver="$UDC" \
  --device="$UDC" \
  --hdmi \
  --out '' \
  --drm-device "$DRM_DEVICE" \
  --drm-connector "$DRM_CONNECTOR" \
  -v
