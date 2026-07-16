#!/usr/bin/env bash
# Apply O3 MITM patches into a local AristoChen/usb-proxy checkout and rebuild.
#
# Default checkout: ~/o3/usb-proxy  (created by scripts/pi_setup.sh)
# Override: USB_PROXY_DIR=/path/to/usb-proxy ./scripts/apply_usb_proxy_o3.sh
#
# Base commit expected: a08301d21d6ba1036cddbcb1a4314578cbc4a274
# (we overwrite key sources with the vendored O3 copies — no fragile patch apply.)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$REPO_ROOT/poc/usb_proxy_o3"
DST="${USB_PROXY_DIR:-$HOME/o3/usb-proxy}"
BASE_COMMIT="a08301d21d6ba1036cddbcb1a4314578cbc4a274"

log() { printf '\033[1;36m==> %s\033[0m\n' "$*"; }

if [ ! -d "$SRC" ]; then
  echo "!! missing $SRC" >&2; exit 1
fi

if [ ! -d "$DST/.git" ]; then
  log "cloning AristoChen/usb-proxy → $DST"
  mkdir -p "$(dirname "$DST")"
  git clone https://github.com/AristoChen/usb-proxy.git "$DST"
fi

cd "$DST"
log "checking out base $BASE_COMMIT"
git fetch --all --quiet 2>/dev/null || true
git checkout -f "$BASE_COMMIT" 2>/dev/null || git checkout -f a08301d

log "installing O3 sources from $SRC"
# Full replacements (carry all O3 fixes + natural-flow AOA re-enum).
cp -f "$SRC/device-libusb.cpp" "$DST/device-libusb.cpp"
cp -f "$SRC/device-libusb.h"   "$DST/device-libusb.h"
cp -f "$SRC/proxy.cpp"         "$DST/proxy.cpp"
cp -f "$SRC/usb-proxy.cpp"     "$DST/usb-proxy.cpp"
cp -f "$SRC/iframe_inject.h"   "$DST/iframe_inject.h"
# Keep config.json reset_device_before_proxy=false
if [ -f "$DST/config.json" ]; then
  python3 - <<'PY' || true
import json, pathlib
p = pathlib.Path("config.json")
cfg = json.loads(p.read_text())
cfg["reset_device_before_proxy"] = False
p.write_text(json.dumps(cfg, indent=4) + "\n")
print("config.json: reset_device_before_proxy = false")
PY
fi

log "building usb-proxy"
make -C "$DST" clean
make -C "$DST" -j"$(nproc 2>/dev/null || echo 2)"

log "OK — binary: $DST/usb-proxy"
"$DST/usb-proxy" --help 2>&1 | head -20 || true
echo
echo "Natural-flow flags: --follow_aoa_reenum  (or USBPROXY_FOLLOW_AOA=1)"
echo "Payload log:        USBPROXY_PAYLOAD_LOG=/path/to.log"
echo "I-frame inject:     USBPROXY_IFRAME=1"
