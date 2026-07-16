#!/usr/bin/env bash
# Idempotent install + enable of the O3 → HDMI boot service on the Pi.
# Also installs the raw_gadget oops watchdog (auto-reboot on kernel wedge).
# Run ON THE Pi:
#   sudo scripts/install_hdmi_service.sh           # install + enable + start
#   sudo scripts/install_hdmi_service.sh --no-start  # install + enable only
#   sudo scripts/install_hdmi_service.sh --disable   # disable + stop (leave unit files)
#
# After install:
#   systemctl status o3-hdmi o3-oops-watchdog
#   journalctl -u o3-hdmi -f
#   scripts/event_health.sh --preflight
#   sudo scripts/o3_hdmi_safe_stop.sh --restart   # clear phantom UDC + restart
#   sudo scripts/o3_soak.sh                       # 2h soak (optional)
#
# Gotcha #4: don't tear down raw-gadget while a *real* USB host is attached.
# UDC state=configured alone is NOT proof of a host (phantom after unplug is common).
#
# Power-cycle usage:
#   - Pi power on  → service starts, waits for goggles
#   - Plug goggles (air unit ON, live video in goggles) → AOA + HDMI
#   - Unplug / power-off goggles → service re-arms in-process (no restart needed)
#   - Plug again → stream returns
#   - Pi power off → whole box down; next boot re-arms automatically
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_SRC="$REPO/poc/pi_endpoint/o3-hdmi.service"
UNIT_DST=/etc/systemd/system/o3-hdmi.service
WD_SRC="$REPO/poc/pi_endpoint/o3-oops-watchdog.service"
WD_DST=/etc/systemd/system/o3-oops-watchdog.service
RUNNER="$REPO/scripts/o3_hdmi_run.sh"
DO_START=1
DO_DISABLE=0

for a in "$@"; do
  case "$a" in
    --no-start) DO_START=0 ;;
    --disable)  DO_DISABLE=1 ;;
    -h|--help)
      sed -n '2,24p' "$0"
      exit 0
      ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "!! need root: sudo $0 $*" >&2
  exit 1
fi
[ -f "$UNIT_SRC" ] || { echo "!! unit not found: $UNIT_SRC" >&2; exit 1; }
[ -x "$RUNNER" ] || chmod +x "$RUNNER"
chmod +x \
  "$REPO/scripts/o3_hdmi_run.sh" \
  "$REPO/scripts/o3_hdmi_safe_stop.sh" \
  "$REPO/scripts/o3_oops_watchdog.sh" \
  "$REPO/scripts/event_health.sh" \
  "$REPO/scripts/o3_soak.sh" \
  2>/dev/null || true

install_unit() {
  # $1=src $2=dst — install and rewrite /home/pi/o3test → $REPO if needed
  local src="$1" dst="$2"
  install -m 0644 "$src" "$dst"
  if [ "$REPO" != "/home/pi/o3test" ]; then
    tmp="$(mktemp)"
    sed "s|/home/pi/o3test|$REPO|g" "$dst" >"$tmp"
    install -m 0644 "$tmp" "$dst"
    rm -f "$tmp"
  fi
}

if [ "$DO_DISABLE" -eq 1 ]; then
  echo "== disable o3-hdmi + o3-oops-watchdog =="
  # UDC=configured is often a *phantom* after unplug — use safe_stop, don't hard-refuse.
  if [ -r /sys/class/udc/fe980000.usb/state ]; then
    st="$(cat /sys/class/udc/fe980000.usb/state 2>/dev/null || true)"
    if [ "$st" = "configured" ]; then
      echo "!! UDC is 'configured' (may be phantom — does not prove goggles are plugged)."
      echo "   clearing via o3_hdmi_safe_stop.sh (confirm goggles are unplugged)."
    fi
  fi
  if [ -x "$REPO/scripts/o3_hdmi_safe_stop.sh" ]; then
    "$REPO/scripts/o3_hdmi_safe_stop.sh" || true
  else
    systemctl stop o3-hdmi 2>/dev/null || true
  fi
  systemctl stop o3-oops-watchdog 2>/dev/null || true
  systemctl disable o3-hdmi 2>/dev/null || true
  systemctl disable o3-oops-watchdog 2>/dev/null || true
  echo "disabled (unit files left in /etc/systemd/system/)"
  exit 0
fi

echo "== install o3-hdmi from $REPO =="
chmod +x "$RUNNER"
install_unit "$UNIT_SRC" "$UNIT_DST"
if [ "$REPO" != "/home/pi/o3test" ]; then
  echo "   (rewrote paths for REPO=$REPO)"
fi

if [ -f "$WD_SRC" ]; then
  echo "== install o3-oops-watchdog =="
  install_unit "$WD_SRC" "$WD_DST"
  mkdir -p /var/log/o3
  echo "   log dir /var/log/o3 (last-oops-reboot.txt on trigger)"
else
  echo "!! watchdog unit missing: $WD_SRC" >&2
fi

# GStreamer + PyGObject for the atomic-KMS presenter (appsink path). Idempotent.
# Field player is atomic-KMS + SW avdec_h264, not mpv / not v4l2h264dec.
export DEBIAN_FRONTEND=noninteractive
if ! command -v gst-launch-1.0 >/dev/null 2>&1 || \
   ! gst-inspect-1.0 kmssink >/dev/null 2>&1; then
  echo "   installing gstreamer..."
  apt-get install -y -qq \
    gstreamer1.0-tools \
    gstreamer1.0-plugins-base \
    gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad \
    >/dev/null
  echo "   gstreamer ready: $(gst-launch-1.0 --version 2>/dev/null | head -1 || echo ok)"
else
  echo "   gstreamer kmssink: present"
fi

# atomic presenter decodes with avdec_h264 (SW) — bcm2835-codec (v4l2h264dec, HW)
# reconstructs chroma wrong at high-contrast edges (2026-07-15, see
# atomic_drm_present.py module docstring). gstreamer1.0-libav provides avdec_h264.
if ! gst-inspect-1.0 avdec_h264 >/dev/null 2>&1; then
  echo "   installing gstreamer1.0-libav (avdec_h264, SW H.264 decode)..."
  apt-get install -y -qq gstreamer1.0-libav >/dev/null
  echo "   avdec_h264: $(gst-inspect-1.0 avdec_h264 >/dev/null 2>&1 && echo ok || echo MISSING)"
else
  echo "   avdec_h264 (gstreamer1.0-libav): present"
fi

# PyGObject + Gst introspection (atomic_drm_present.py imports gi.repository.Gst).
if ! python3 -c "import gi; gi.require_version('Gst','1.0'); from gi.repository import Gst" 2>/dev/null; then
  echo "   installing python3-gi + gstreamer introspection..."
  apt-get install -y -qq \
    python3-gi \
    python3-gst-1.0 \
    gir1.2-gstreamer-1.0 \
    gir1.2-gst-plugins-base-1.0 \
    >/dev/null
  if python3 -c "import gi; gi.require_version('Gst','1.0'); from gi.repository import Gst" 2>/dev/null; then
    echo "   python-gi Gst: ok"
  else
    echo "   !! python-gi Gst still missing after install" >&2
  fi
else
  echo "   python-gi Gst: present"
fi

# Boot console quieting so the Linux getty/cmdline doesn't flash on HDMI before
# o3-hdmi owns the plane with the idle "waiting for live FPV feed" splash.
# Idempotent: only appends tokens that are missing. Does not rewrite the whole line.
quiet_boot_cmdline() {
  local cmdfile=""
  for c in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
    if [ -f "$c" ]; then
      cmdfile="$c"
      break
    fi
  done
  if [ -z "$cmdfile" ]; then
    echo "   boot cmdline: not found (skip quiet tokens)"
    return 0
  fi
  local line
  line="$(tr -d '\n' <"$cmdfile")"
  local need=()
  for tok in quiet logo.nologo vt.global_cursor_default=0; do
    case " $line " in
      *" $tok "*) ;;
      *) need+=("$tok") ;;
    esac
  done
  if [ "${#need[@]}" -eq 0 ]; then
    echo "   boot cmdline: quiet tokens already present ($cmdfile)"
    return 0
  fi
  # Backup once per install run.
  cp -a "$cmdfile" "${cmdfile}.o3bak.$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
  printf '%s %s\n' "$line" "${need[*]}" >"$cmdfile"
  echo "   boot cmdline: added ${need[*]} → $cmdfile (reboot to take effect)"
}
quiet_boot_cmdline

systemctl daemon-reload
systemctl enable o3-hdmi.service
echo "   o3-hdmi enabled (WantedBy=multi-user.target)"
if [ -f "$WD_DST" ]; then
  systemctl enable o3-oops-watchdog.service
  echo "   o3-oops-watchdog enabled (auto-reboot on raw_gadget wedge)"
fi
echo "   DRM_CONNECTOR=${DRM_CONNECTOR:-HDMI-A-2}  player=atomic  dual=parked  idle_splash=1"

if [ "$DO_START" -eq 1 ]; then
  # Always go through safe_stop: clears phantom UDC=configured, reaps leftover
  # endpoint/player, then we start clean. Do not refuse solely on UDC state.
  if [ -r /sys/class/udc/fe980000.usb/state ]; then
    st="$(cat /sys/class/udc/fe980000.usb/state 2>/dev/null || true)"
    if [ "$st" = "configured" ]; then
      echo "   UDC was 'configured' (often phantom after unplug) — safe-stopping first"
    fi
  fi
  if [ -x "$REPO/scripts/o3_hdmi_safe_stop.sh" ]; then
    "$REPO/scripts/o3_hdmi_safe_stop.sh" || true
  else
    systemctl stop o3-hdmi 2>/dev/null || true
    pkill -9 -f 'poc/pi_endpoint/raw_endpoint.py|phaseB_standalone' 2>/dev/null || true
    pkill -x mpv 2>/dev/null || true
    sleep 1
  fi
  systemctl start o3-hdmi.service
  if [ -f "$WD_DST" ]; then
    systemctl restart o3-oops-watchdog.service 2>/dev/null || systemctl start o3-oops-watchdog.service
  fi
  sleep 2
  systemctl --no-pager --full status o3-hdmi.service || true
  systemctl --no-pager --full status o3-oops-watchdog.service 2>/dev/null || true
  echo
  echo "== ready =="
  echo "   journal:   journalctl -u o3-hdmi -f"
  echo "   watchdog:  journalctl -u o3-oops-watchdog -f"
  echo "   preflight: $REPO/scripts/event_health.sh --preflight"
  echo "   soak 2h:   sudo $REPO/scripts/o3_soak.sh"
  echo "   HDMI: far micro-HDMI (HDMI-A-2); dual parked"
  echo "   plug goggles (VBUS-isolated USB-C) when air unit shows live video"
  echo "   stuck udc=configured with goggles unplugged:"
  echo "     sudo $REPO/scripts/o3_hdmi_safe_stop.sh --restart"
else
  echo "installed + enabled (not started)."
  echo "   start:  sudo systemctl start o3-hdmi o3-oops-watchdog"
  echo "   or:     sudo $REPO/scripts/o3_hdmi_safe_stop.sh --restart"
  echo "           sudo systemctl start o3-oops-watchdog"
fi
