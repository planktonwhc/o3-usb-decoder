#!/usr/bin/env bash
# Offline dual-HDMI prove for Pi 4 vc4.
#
# Proven shape (2026-07-13):
#   dual tee×kmssink  → FAIL (modeset / drmModeSetPlane Permission denied)
#   single kmssink    → PLAYING on either port alone
#   fbdevsink RGB16   → PLAYING; console shares one FB on both CRTCs → mirror
#
# Default prove mode = fbdev (shared /dev/fb0). Optional PROVE_MODE=kms for the
# known-broken dual kmssink shape.
#
# Do NOT run while o3-hdmi holds DRM. Gotcha #4: unplug goggles BEFORE stop.
#
# Usage (on Pi, both monitors plugged):
#   ~/o3test/scripts/prove_dual_hdmi.sh
#   PROVE_MODE=kms ~/o3test/scripts/prove_dual_hdmi.sh
#   PROVE_ALLOW_SINGLE=1 ~/o3test/scripts/prove_dual_hdmi.sh   # one port smoke

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
H264="${1:-}"
PROVE_SECS="${PROVE_SECS:-12}"
DECODER="${PROVE_DECODER:-openh264dec}"
CARD="${DRM_CARD:-card1}"
DRIVER="${KMS_DRIVER:-vc4}"
MODE="${PROVE_MODE:-fbdev}"   # fbdev | kms

log() { echo "[prove-dual] $*"; }
die() { echo "[prove-dual] ERROR: $*" >&2; exit 1; }

if systemctl is-active --quiet o3-hdmi 2>/dev/null; then
  die "o3-hdmi is active (holds DRM). Unplug goggles first, then: sudo systemctl stop o3-hdmi"
fi
if pgrep -x mpv >/dev/null 2>&1; then
  die "mpv already running — kill it before offline prove"
fi
if pgrep -x gst-launch-1.0 >/dev/null 2>&1; then
  die "gst-launch already running — kill it before offline prove"
fi
UDC_STATE="$(cat /sys/class/udc/*/state 2>/dev/null || true)"
if echo "$UDC_STATE" | grep -q configured; then
  log "WARNING: UDC still 'configured' — confirm goggles unplugged (phantom UDC is common)"
fi

if [[ -z "$H264" ]]; then
  H264=""
  for cand in "$ROOT"/captures/pi_pull/*_phaseB_standalone.h264; do
    [[ -f "$cand" ]] && H264="$cand"
  done
fi
[[ -n "$H264" && -f "$H264" ]] || die "no .h264 sample; pass path as arg"
log "ES file: $H264 ($(du -h "$H264" | awk '{print $1}'))"
log "mode=$MODE decoder=$DECODER"

declare -a NAMES=()
declare -a IDS=()
for ent in /sys/class/drm/"$CARD"-HDMI-A-*; do
  [[ -d "$ent" ]] || continue
  name="${ent##*/}"
  name="${name#"$CARD"-}"
  status="$(cat "$ent/status" 2>/dev/null || echo unknown)"
  edid_len="$(wc -c <"$ent/edid" 2>/dev/null | tr -d '[:space:]' || echo 0)"
  modes_n="$(grep -c . "$ent/modes" 2>/dev/null || echo 0)"
  cid="$(cat "$ent/connector_id" 2>/dev/null || true)"
  log "sysfs $name status=$status edid=${edid_len}B modes=$modes_n id=${cid:-?}"
  [[ "$status" == "connected" ]] || continue
  if [[ "${edid_len:-0}" -ge 128 || "${modes_n:-0}" -ge 1 ]]; then
    if [[ -n "$cid" ]]; then
      NAMES+=("$name")
      IDS+=("$cid")
    fi
  else
    log "  (skip phantom — no EDID/modes)"
  fi
done

n="${#IDS[@]}"
ALLOW_SINGLE="${PROVE_ALLOW_SINGLE:-0}"
if [[ "$n" -lt 1 ]]; then
  die "no usable HDMI connectors. Plug at least one micro-HDMI."
fi
if [[ "$n" -lt 2 && "$ALLOW_SINGLE" != "1" && "$MODE" != "fbdev" ]]; then
  die "need 2 usable HDMI for kms dual; found $n. Plug both, or PROVE_MODE=fbdev / PROVE_ALLOW_SINGLE=1"
fi
# fbdev mirror only needs 2 connected for a *dual* claim; still run if 1 with allow
if [[ "$n" -lt 2 && "$MODE" == "fbdev" && "$ALLOW_SINGLE" != "1" ]]; then
  die "fbdev dual claim needs 2 connected ports (found $n). Plug both micro-HDMI."
fi
log "targets: ${NAMES[*]:-none} → ids ${IDS[*]:-none}"

command -v gst-launch-1.0 >/dev/null || die "gst-launch-1.0 missing"
gst-inspect-1.0 "$DECODER" >/dev/null 2>&1 || die "decoder $DECODER missing"

if [[ "$MODE" == "fbdev" ]]; then
  gst-inspect-1.0 fbdevsink >/dev/null 2>&1 || die "fbdevsink missing"
  GST_ARGS=(
    filesrc "location=$H264"
    ! h264parse "config-interval=-1"
    ! "$DECODER"
    ! videoconvert "n-threads=2"
    ! "video/x-raw,format=RGB16"
    ! fbdevsink sync=false
  )
elif [[ "$MODE" == "kms" ]]; then
  gst-inspect-1.0 kmssink >/dev/null 2>&1 || die "kmssink missing"
  [[ "$n" -ge 2 ]] || die "kms mode needs 2 connectors"
  GST_ARGS=(
    filesrc "location=$H264"
    ! h264parse "config-interval=-1"
    ! "$DECODER"
    ! videoconvert "n-threads=2"
    ! "video/x-raw,format=BGRx"
    ! tee name=t
  )
  for cid in "${IDS[@]}"; do
    GST_ARGS+=(
      t. ! queue "max-size-buffers=2" leaky=downstream
      ! kmssink "driver-name=$DRIVER" "connector-id=$cid"
        sync=false force-modesetting=true skip-vsync=true
    )
  done
else
  die "unknown PROVE_MODE=$MODE (use fbdev or kms)"
fi

log "run ${PROVE_SECS}s"
log "cmd: gst-launch-1.0 -e ${GST_ARGS[*]}"

set +e
gst-launch-1.0 -e "${GST_ARGS[@]}" > /tmp/prove_dual_hdmi.log 2>&1 &
GPID=$!
set -e

for ((i = 0; i < PROVE_SECS * 2; i++)); do
  sleep 0.5
  if ! kill -0 "$GPID" 2>/dev/null; then
    wait "$GPID" || true
    log "pipeline exited early — last log lines:"
    tail -40 /tmp/prove_dual_hdmi.log || true
    exit 1
  fi
  if [[ $i -eq 6 ]]; then
    log "still running at 3s"
  fi
done

ok_enabled=0
for name in "${NAMES[@]}"; do
  en="$(cat /sys/class/drm/"$CARD"-"$name"/enabled 2>/dev/null || echo unknown)"
  log "post-run $name enabled=$en"
  if [[ "$en" == "enabled" ]]; then
    ok_enabled=$((ok_enabled + 1))
  fi
done

# Shared-FB check (fbdev path): both CRTCs should still scan out same fbcon FB
if [[ "$MODE" == "fbdev" ]] && [[ -r /sys/kernel/debug/dri/1/state || -r /sys/kernel/debug/dri/0/state ]]; then
  stf=""
  for cand in /sys/kernel/debug/dri/1/state /sys/kernel/debug/dri/0/state; do
    if [[ -r "$cand" ]]; then stf="$cand"; break; fi
  done
  if [[ -n "$stf" ]]; then
    shared="$(sudo grep -A2 'crtc=pixelvalve' "$stf" 2>/dev/null | grep -E '^\s*fb=' | sort -u | wc -l | tr -d ' ')"
    log "unique fb ids on pixelvalve CRTCs during/after: $shared (1 = shared mirror)"
  fi
fi

kill -INT "$GPID" 2>/dev/null || true
sleep 1
kill -KILL "$GPID" 2>/dev/null || true
wait "$GPID" 2>/dev/null || true

log "--- gst log (tail) ---"
tail -25 /tmp/prove_dual_hdmi.log || true
log "---"

need_enabled=2
if [[ "$n" -lt 2 ]]; then need_enabled=1; fi

if [[ "$ok_enabled" -ge "$need_enabled" ]]; then
  if [[ "$MODE" == "fbdev" && "$n" -ge 2 ]]; then
    log "PASS (pipeline): survived ${PROVE_SECS}s, $ok_enabled connectors enabled (fbdev mirror)"
    log "EYES-ON REQUIRED: confirm **both** monitors show the sample (not console only)."
    log "If eyes-on OK → O3_DUAL_HDMI=1 O3_DUAL_MODE=fbdev + reinstall service."
  elif [[ "$n" -ge 2 ]]; then
    log "PASS: kms dual survived ${PROVE_SECS}s (unexpected win on this Pi!)"
  else
    log "PASS (single smoke): survived ${PROVE_SECS}s"
  fi
  exit 0
fi

if ! grep -qiE 'error|failed|not-negotiated' /tmp/prove_dual_hdmi.log 2>/dev/null; then
  log "SOFT: survived but enabled=$ok_enabled (need $need_enabled) — check eyes-on"
  exit 2
fi
log "FAIL: dual pipeline did not prove (enabled=$ok_enabled/$need_enabled)"
exit 1
