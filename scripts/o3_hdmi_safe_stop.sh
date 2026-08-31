#!/usr/bin/env bash
# Safe stop/clear for o3-hdmi when UDC is stuck at 'configured' with goggles UNPLUGGED.
#
# Background: after a real unplug, dwc2/raw-gadget often leaves
#   /sys/class/udc/fe980000.usb/state == configured
# without delivering USB_RAW_EVENT_DISCONNECT. That "phantom host" used to:
#   - keep gold sustain writing → EAGAIN storm
#   - make install_hdmi_service.sh / operators refuse to stop (false "goggles attached")
#
# Gotcha #4 still applies when a *real* host is attached (live ES / au_feed).
# This script is the default field recovery + deploy restart path.
#
# Multi-signal host detection (scripts/o3_host_probe.sh) decides LIVE vs SAFE:
#   - NEVER refuse solely because udc=configured (phantom is common).
#   - REFUSE only when journal shows live au_feed / ES / bulk (unless --force).
#
# Usage (on Pi, as root):
#   sudo scripts/o3_hdmi_safe_stop.sh              # stop + clear phantom UDC
#   sudo scripts/o3_hdmi_safe_stop.sh --restart    # stop, clear, start o3-hdmi
#   sudo scripts/o3_hdmi_safe_stop.sh --force      # skip LIVE_HOST guard
#   sudo scripts/o3_hdmi_safe_stop.sh --restart --force
#   sudo scripts/o3_hdmi_safe_stop.sh --probe-only # run host probe and exit
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
UDC="${UDC:-fe980000.usb}"
PROBE="$REPO/scripts/o3_host_probe.sh"
DO_RESTART=0
FORCE=0
PROBE_ONLY=0

for a in "$@"; do
  case "$a" in
    --restart) DO_RESTART=1 ;;
    --force|--i-confirm-unplugged) FORCE=1 ;;
    --probe-only) PROBE_ONLY=1 ;;
    -h|--help)
      sed -n '2,28p' "$0"
      exit 0
      ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" -ne 0 ] && [ "$PROBE_ONLY" -eq 0 ]; then
  echo "!! need root: sudo $0 $*" >&2
  exit 1
fi

state() { cat /sys/class/udc/"$UDC"/state 2>/dev/null || echo "?"; }

# --- multi-signal host probe -------------------------------------------------
probe_rc=0
probe_verdict="?"
if [ -x "$PROBE" ]; then
  # Capture human report for the log; ignore its exit until we branch.
  set +e
  probe_out="$("$PROBE" 2>&1)"
  probe_rc=$?
  set -e
  probe_verdict="$(printf '%s\n' "$probe_out" | sed -n 's/^verdict: \([^ ]*\).*/\1/p' | head -1)"
  probe_verdict="${probe_verdict:-?}"
else
  echo "!! o3_host_probe.sh missing ($PROBE) — falling back to UDC-agnostic stop" >&2
  probe_rc=0
  probe_verdict="SAFE_STOP"
  probe_out="(probe missing; assuming SAFE_STOP so phantom UDC cannot block ops)"
fi

if [ "$PROBE_ONLY" -eq 1 ]; then
  printf '%s\n' "$probe_out"
  exit "$probe_rc"
fi

echo "== o3_hdmi_safe_stop (udc=$(state) probe=$probe_verdict) =="
echo "   NOTE: udc=configured does NOT prove goggles are plugged (phantom is common)."
printf '%s\n' "$probe_out" | sed 's/^/   | /'
echo

if [ "$probe_rc" -eq 1 ] && [ "$FORCE" -eq 0 ]; then
  # LIVE_HOST — strong evidence of real traffic. Gotcha #4.
  echo "!! REFUSING stop/restart: host probe = LIVE_HOST (live ES/au_feed)." >&2
  echo "   Gotcha #4: tearing down raw-gadget under a live host can oops dwc2." >&2
  echo "   1) Unplug goggles from Pi USB-C" >&2
  echo "   2) Re-run: sudo $0 ${DO_RESTART:+--restart}" >&2
  echo "   Or if owner confirms unplugged (probe false positive): sudo $0 --force ${DO_RESTART:+--restart}" >&2
  exit 1
fi

if [ "$probe_rc" -eq 1 ] && [ "$FORCE" -eq 1 ]; then
  echo "   --force: overriding LIVE_HOST guard (owner confirms unplugged / emergency)"
fi

if [ "$probe_rc" -eq 2 ]; then
  echo "   probe=UNCERTAIN — proceeding with safe_stop (phantom/idle recovery)."
  echo "   If goggles are actually plugged, unplug first (gotcha #4)."
fi

if [ "$probe_rc" -eq 0 ]; then
  echo "   probe=SAFE_STOP — proceeding (idle / not attached / phantom configured)."
fi

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
  # Keep oops watchdog up (install enables it; restart path should not leave it dead).
  if systemctl cat o3-oops-watchdog >/dev/null 2>&1; then
    systemctl start o3-oops-watchdog 2>/dev/null || true
  fi
  sleep 2
  systemctl --no-pager --full status o3-hdmi || true
  echo "   journal: journalctl -u o3-hdmi -f"
  echo "   expect: idle splash dual/single until goggles plug; then AOA → atomic"
  # Env fingerprint so agents can confirm dual-default unit is what is running.
  echo "   env:"
  systemctl show o3-hdmi -p Environment --value 2>/dev/null \
    | tr ' ' '\n' | grep -E '^(O3_DUAL_HDMI|DRM_CONNECTOR|O3_HDMI_PLAYER|O3_IDLE_SPLASH)=' \
    | sed 's/^/     /' || true
  # Re-probe after start for operator confidence.
  if [ -x "$PROBE" ]; then
    echo "== post-start host probe =="
    "$PROBE" || true
  fi
  # Hard fail if service did not come up (caller can retry / inspect journal).
  if ! systemctl is-active --quiet o3-hdmi; then
    echo "!! o3-hdmi failed to become active after restart" >&2
    journalctl -u o3-hdmi -n 40 --no-pager >&2 || true
    exit 1
  fi
  echo "   o3-hdmi: active  oops-watchdog: $(systemctl is-active o3-oops-watchdog 2>/dev/null || echo n/a)"
fi

echo "== done =="
