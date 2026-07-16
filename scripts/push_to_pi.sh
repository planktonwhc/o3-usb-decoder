#!/usr/bin/env bash
# Push the working repo from the laptop to the Pi before a bench run.
# Runs on the LAPTOP. Pi = o3pi.local (192.168.1.57), user pi, key auth + passwordless sudo.
#
#   scripts/push_to_pi.sh                 # rsync code → pi:~/o3test/
#   PI=pi@192.168.1.57 scripts/push_to_pi.sh
#
# Excludes the big/append-only stuff so we never clobber the Pi's captures or ship the
# 3GB firmware zip / venv. Captures flow the OTHER way (Pi → laptop) and are append-only.
set -euo pipefail

PI="${PI:-pi@o3pi.local}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

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
echo "== done. On the Pi, run the standalone bench: =="
echo "   ssh $PI"
echo "   sudo ~/o3test/scripts/phaseB_standalone.sh -v"
