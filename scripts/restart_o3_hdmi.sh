#!/usr/bin/env bash
# Canonical o3-hdmi restart path — laptop OR Pi.
#
# This is the one command agents/operators should use. It never gates on
# udc=configured alone (phantom after unplug is common). Under the hood:
#   o3_host_probe.sh  →  o3_hdmi_safe_stop.sh --restart
# LIVE_HOST (live au_feed/ES) refuses unless --force.
#
# Usage:
#   scripts/restart_o3_hdmi.sh                 # auto: local if on Pi, else SSH
#   scripts/restart_o3_hdmi.sh --force         # owner confirms unplugged
#   scripts/restart_o3_hdmi.sh --probe-only    # print host probe, do not restart
#   scripts/restart_o3_hdmi.sh --status        # probe + service status only
#   PI=pi@192.168.1.57 scripts/restart_o3_hdmi.sh
#
# From laptop after code push:
#   scripts/push_to_pi.sh --restart
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
# Prefer mDNS; fall back to field LAN IP (mDNS often flaky from this host).
PI="${PI:-}"
FORCE=0
PROBE_ONLY=0
STATUS_ONLY=0
EXTRA_ARGS=()

for a in "$@"; do
  case "$a" in
    --force|--i-confirm-unplugged) FORCE=1; EXTRA_ARGS+=(--force) ;;
    --probe-only) PROBE_ONLY=1 ;;
    --status) STATUS_ONLY=1 ;;
    -h|--help)
      sed -n '2,22p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $a" >&2
      exit 2
      ;;
  esac
done

resolve_pi() {
  if [[ -n "${PI}" ]]; then
    echo "$PI"
    return
  fi
  # Prefer explicit IP when mDNS is broken (common on this laptop).
  if [[ -n "${O3_PI:-}" ]]; then
    echo "$O3_PI"
    return
  fi
  if getent hosts o3pi.local >/dev/null 2>&1 || \
     ping -c1 -W1 o3pi.local >/dev/null 2>&1; then
    echo "pi@o3pi.local"
    return
  fi
  echo "pi@192.168.1.57"
}

on_pi() {
  # Running on the field Pi (has the UDC + o3-hdmi unit).
  [[ -r /sys/class/udc/fe980000.usb/state ]] || return 1
  command -v systemctl >/dev/null 2>&1 || return 1
  systemctl cat o3-hdmi >/dev/null 2>&1 || return 1
  return 0
}

run_local() {
  local probe="$REPO/scripts/o3_host_probe.sh"
  local safe="$REPO/scripts/o3_hdmi_safe_stop.sh"
  chmod +x "$probe" "$safe" 2>/dev/null || true

  echo "== restart_o3_hdmi (local) =="
  if [[ "$PROBE_ONLY" -eq 1 || "$STATUS_ONLY" -eq 1 ]]; then
    "$probe" || true
    if [[ "$STATUS_ONLY" -eq 1 ]]; then
      systemctl --no-pager --full status o3-hdmi o3-oops-watchdog 2>/dev/null || true
      echo "udc=$(cat /sys/class/udc/fe980000.usb/state 2>/dev/null || echo '?')"
    fi
    return 0
  fi

  if [[ "$(id -u)" -ne 0 ]]; then
    exec sudo "$REPO/scripts/restart_o3_hdmi.sh" "$@"
  fi

  if [[ ${#EXTRA_ARGS[@]} -gt 0 ]]; then
    "$safe" --restart "${EXTRA_ARGS[@]}"
  else
    "$safe" --restart
  fi
  echo
  echo "== post-check =="
  systemctl is-active o3-hdmi o3-oops-watchdog 2>/dev/null || true
  echo "udc=$(cat /sys/class/udc/fe980000.usb/state 2>/dev/null || echo '?')"
  systemctl show o3-hdmi -p Environment --value 2>/dev/null \
    | tr ' ' '\n' | grep -E '^(O3_DUAL_HDMI|DRM_CONNECTOR|O3_HDMI_PLAYER)=' || true
  journalctl -u o3-hdmi -n 8 --no-pager 2>/dev/null || true
}

run_remote() {
  local target
  target="$(resolve_pi)"
  local remote_repo='~/o3test'
  echo "== restart_o3_hdmi (remote $target) =="

  # shellcheck disable=SC2029
  if [[ "$PROBE_ONLY" -eq 1 ]]; then
    ssh -o ConnectTimeout=8 -o BatchMode=yes "$target" \
      "chmod +x $remote_repo/scripts/o3_host_probe.sh 2>/dev/null; $remote_repo/scripts/o3_host_probe.sh"
    return $?
  fi

  if [[ "$STATUS_ONLY" -eq 1 ]]; then
    ssh -o ConnectTimeout=8 -o BatchMode=yes "$target" bash -s <<'EOS'
set -euo pipefail
R=~/o3test
chmod +x "$R/scripts/o3_host_probe.sh" 2>/dev/null || true
"$R/scripts/o3_host_probe.sh" || true
echo
systemctl --no-pager --full status o3-hdmi o3-oops-watchdog 2>/dev/null || true
echo "udc=$(cat /sys/class/udc/fe980000.usb/state 2>/dev/null || echo '?')"
systemctl show o3-hdmi -p Environment --value 2>/dev/null \
  | tr ' ' '\n' | grep -E '^(O3_DUAL_HDMI|DRM_CONNECTOR|O3_HDMI_PLAYER)=' || true
EOS
    return $?
  fi

  local force_flag=""
  [[ "$FORCE" -eq 1 ]] && force_flag="--force"

  # Quote carefully for remote sudo.
  ssh -o ConnectTimeout=8 -o BatchMode=yes "$target" bash -s -- "$force_flag" <<'EOS'
set -euo pipefail
FORCE_FLAG="${1:-}"
R=~/o3test
chmod +x \
  "$R/scripts/o3_host_probe.sh" \
  "$R/scripts/o3_hdmi_safe_stop.sh" \
  "$R/scripts/restart_o3_hdmi.sh" \
  2>/dev/null || true

echo "-- host probe (pre) --"
"$R/scripts/o3_host_probe.sh" || true
echo

if [[ -n "$FORCE_FLAG" ]]; then
  sudo "$R/scripts/o3_hdmi_safe_stop.sh" --restart --force
else
  sudo "$R/scripts/o3_hdmi_safe_stop.sh" --restart
fi

echo
echo "-- post-check --"
systemctl is-active o3-hdmi o3-oops-watchdog 2>/dev/null || true
echo "udc=$(cat /sys/class/udc/fe980000.usb/state 2>/dev/null || echo '?')"
systemctl show o3-hdmi -p Environment --value 2>/dev/null \
  | tr ' ' '\n' | grep -E '^(O3_DUAL_HDMI|DRM_CONNECTOR|O3_HDMI_PLAYER)=' || true
journalctl -u o3-hdmi -n 10 --no-pager 2>/dev/null || true
EOS
}

if on_pi; then
  run_local "$@"
else
  run_remote
fi
