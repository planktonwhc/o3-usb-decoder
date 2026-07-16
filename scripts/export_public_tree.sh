#!/usr/bin/env bash
# Build a clean public source tree for GitHub (or a tarball), without
# third-party copyrighted binaries, Frida/app RE tooling, or bulk captures.
#
# Usage (from monorepo root or any cwd):
#   scripts/export_public_tree.sh
#   OUT=/tmp/dji-o3-usb-decoder-public scripts/export_public_tree.sh
#   scripts/export_public_tree.sh --check   # dry-run: list what would be copied
#
# See docs/OPEN_SOURCE.md.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${OUT:-$(cd "$REPO/.." && pwd)/dji-o3-usb-decoder-public}"
CHECK_ONLY=0

for a in "$@"; do
  case "$a" in
    --check) CHECK_ONLY=1 ;;
    -h|--help)
      sed -n '2,14p' "$0"
      exit 0
      ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

# rsync filter: first match wins. Include shipping + docs + analysis tools;
# exclude research dumps and third-party binaries.
FILTER=(
  # --- never ---
  --exclude '.git/'
  --exclude '.venv/'
  --exclude '.claude/'
  --exclude '__pycache__/'
  --exclude '*.py[cod]'
  --exclude '.DS_Store'
  --exclude 'node_modules/'
  --exclude 'analysis/dji_fly_apk/'
  --exclude 'handover/'
  --exclude 'handover2/'
  --exclude 'handover.zip'
  --exclude 'handover2.zip'
  --exclude 'SAMFW*.zip'
  --exclude '*_fac.zip'
  --exclude 'poc/frida_repack/'
  --exclude 'poc/aoa_sniffer/build/'
  --exclude 'poc/aoa_sniffer/*.apk'
  --exclude 'poc/aoa_sniffer/*.apk.idsig'
  --exclude 'poc/aoa_sniffer/debug.keystore'
  --exclude '*.MOV'
  --exclude '*.mov'
  --exclude '*.mp4'
  --exclude '*.h264'
  --exclude '2026-07-14*.mp4'
  --exclude 'DJI_*.MOV'
  --exclude 'captures/aoa/'
  --exclude 'captures/aoa_frida*'
  --exclude 'captures/*.pcap'
  --exclude 'captures/pi_pull/*_payload.log'
  --exclude 'captures/pi_pull/*_natural_payload.log'
  --exclude 'captures/pi_pull/*usbproxy.stdout'
  --exclude 'captures/pi_pull/*usbproxy.log'
  --exclude 'captures/pi_pull/video_extract/'
  --exclude 'captures/pi_pull/device-libusb.cpp'
  --exclude 'captures/pi_pull/proxy.cpp'
  --exclude 'dist/'
  --exclude 'dji-o3-usb-decoder-public/'

  # --- always include curated roots ---
  --include '/LICENSE'
  --include '/NOTICE'
  --include '/README.md'
  --include '/CONTRIBUTING.md'
  --include '/CLAUDE.md'
  --include '/.gitignore'
  --include '/analysis/'
  --include '/analysis/***'
  --include '/docs/'
  --include '/docs/***'
  --include '/scripts/'
  --include '/scripts/***'
  --include '/poc/'
  --include '/poc/pi_endpoint/'
  --include '/poc/pi_endpoint/***'
  --include '/poc/pi_mitm/'
  --include '/poc/pi_mitm/***'
  --include '/poc/usb_proxy_o3/'
  --include '/poc/usb_proxy_o3/***'
  --include '/poc/aoa_sniffer/'
  --include '/poc/aoa_sniffer/***'
  --include '/poc/replay.py'
  --include '/captures/'
  --include '/captures/README.md'
  --include '/captures/.gitkeep'
  --include '/captures/pi_pull/'
  --include '/captures/pi_pull/*GOLD_ANALYSIS*'
  --include '/captures/pi_pull/*phaseB*.log'
  --include '/captures/pi_pull/*100fps*.log'
  --include '/captures/recon/'
  --include '/captures/recon/***'

  # drop everything else at repo root (media, zips, etc.)
  --exclude '/*'
  --exclude '/***'
)

echo "== export public tree =="
echo "   monorepo: $REPO"
echo "   out:      $OUT"

if [ "$CHECK_ONLY" -eq 1 ]; then
  rsync -a --dry-run --itemize-changes "${FILTER[@]}" "$REPO/" "$OUT/" | sed -n '1,200p'
  echo "(dry-run; no files written)"
  exit 0
fi

mkdir -p "$OUT"
# Delete stale public-only files when re-exporting (keeps OUT a pure mirror of the filter).
rsync -a --delete --delete-excluded "${FILTER[@]}" "$REPO/" "$OUT/"

# Sanity: refuse to ship obvious proprietary / bulk blobs.
bad=0
while IFS= read -r -d '' f; do
  echo "!! refuse: unexpected binary/blob in public tree: $f" >&2
  bad=1
done < <(find "$OUT" \( \
  -iname '*.apk' -o -iname '*.apk.idsig' -o -name 'debug.keystore' -o \
  -iname '*.so' -o -iname 'SAMFW*' -o -path '*/dji_fly_apk/*' -o \
  -path '*/frida_repack/*' -o -path '*/handover/*' -o \
  -iname '*.MOV' -o -iname '*.h264' \
  \) -print0 2>/dev/null)

if [ "$bad" -ne 0 ]; then
  echo "!! export failed safety check" >&2
  exit 1
fi

# Summarize
echo
echo "== public tree ready =="
echo "   path: $OUT"
echo "   size: $(du -sh "$OUT" | awk '{print $1}')"
echo "   files: $(find "$OUT" -type f | wc -l)"
echo
echo "Next:"
echo "  cd \"$OUT\""
echo "  git init && git add . && git commit -m 'Initial public release'"
echo "  # then add GitHub remote and push (see docs/OPEN_SOURCE.md)"
echo
echo "Review:"
echo "  find \"$OUT\" -type f | sort"
