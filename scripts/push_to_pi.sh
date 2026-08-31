#!/usr/bin/env bash
# Push the working repo from the laptop to the Pi before a bench run.
# Runs on the LAPTOP. Pi = o3pi.local or 192.168.1.57, user pi, key auth + passwordless sudo.
#
#   scripts/push_to_pi.sh                 # rsync code → pi:~/o3test/
#   scripts/push_to_pi.sh --restart       # rsync + safe restart o3-hdmi on Pi
#   scripts/push_to_pi.sh --restart --force
#   PI=pi@192.168.1.57 scripts/push_to_pi.sh
#
# Excludes the big/append-only stuff so we never clobber the Pi's captures or ship the
# 3GB firmware zip / venv. Captures flow the OTHER way (Pi → laptop) and are append-only.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DO_RESTART=0
FORCE=0

for a in "$@"; do
  case "$a" in
    --restart) DO_RESTART=1 ;;
    --force|--i-confirm-unplugged) FORCE=1 ;;
    -h|--help)
      sed -n '2,14p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $a" >&2
      exit 2
      ;;
  esac
done

resolve_pi() {
  if [[ -n "${PI:-}" ]]; then
    echo "$PI"
    return
  fi
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

PI="$(resolve_pi)"

echo "== rsync $REPO/ → $PI:~/o3test/ =="
rsync -az --info=stats1,progress2 \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude 'captures/' \
  --exclude 'handover*/' \
  --exclude '*.zip' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  "$REPO/" "$PI:o3test/"

echo
if [[ "$DO_RESTART" -eq 1 ]]; then
  echo "== safe restart o3-hdmi on $PI =="
  # Prefer the canonical remote restart helper (probe + safe_stop --restart).
  export PI
  if [[ "$FORCE" -eq 1 ]]; then
    "$REPO/scripts/restart_o3_hdmi.sh" --force
  else
    "$REPO/scripts/restart_o3_hdmi.sh"
  fi
  exit 0
fi

echo "== done. Next: =="
echo "   # one-command safe restart (probe + clear phantom UDC + start):"
echo "   $REPO/scripts/restart_o3_hdmi.sh"
echo "   # or: $0 --restart"
echo "   # host probe only:"
echo "   $REPO/scripts/restart_o3_hdmi.sh --probe-only"
echo "   # full unit reinstall:"
echo "   ssh $PI 'sudo ~/o3test/scripts/install_hdmi_service.sh'"
