#!/usr/bin/env bash
# Phase A capture — Pi USB MITM between Goggles 2 and the phone running DJI Fly.
#
# DEFAULT (2026-07-13): NATURAL FLOW
#   Phone starts in normal MTP/charging mode (e.g. 04e8:6860). usb-proxy presents
#   those descriptors to the goggles; the goggles drive the real AOA handshake
#   (GET_PROTOCOL / SEND_STRING×6 / ACCESSORY_START) *through* the proxy; the proxy
#   follows the phone's re-enum to 18d1:2d0x without exiting (raw_gadget-safe).
#   Rationale: pre-trigger desyncs the gvg activation (see
#   docs/gofly_transition_findings.md). Natural flow is the primary hypothesis to test.
#
# FALLBACK: --pretrigger
#   Old recipe: force phone into AOA first with aoa_trigger.py, then bridge a
#   stable 18d1:2d0x. Use only if natural-flow re-enum fails on the bench.
#
# Options:
#   --pretrigger     use the old pre-AOA recipe
#   --iframe         set USBPROXY_IFRAME=1 (flip SPECIAL pack bit5 on the wire)
#   --vid HEX --pid HEX   phone's *starting* VID:PID (natural: MTP; pretrigger: either)
#   --phone-timeout N     seconds to wait for accessory after ACCESSORY_START (default 30)
#
# Requires: patched usb-proxy at ~/o3/usb-proxy (run scripts/apply_usb_proxy_o3.sh).
# Captures: ~/o3test/captures/<ts>_pi-mitm_payload.log  (PRIMARY for analysis)
set -uo pipefail

MODE=natural
IFRAME=${IFRAME:-}
PHONE_VID=${PHONE_VID:-04e8}
PHONE_PID=${PHONE_PID:-6860}
PHONE_TIMEOUT=${PHONE_TIMEOUT:-30}
PROXY_DIR="${USB_PROXY_DIR:-$HOME/o3/usb-proxy}"
CAPDIR="${CAPDIR:-$HOME/o3test/captures}"
REPO_HINT="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd || echo "$HOME/o3test")"

while [ $# -gt 0 ]; do
  case "$1" in
    --pretrigger) MODE=pretrigger; shift ;;
    --iframe)     IFRAME=1; shift ;;
    --vid)        PHONE_VID="${2#0x}"; shift 2 ;;
    --pid)        PHONE_PID="${2#0x}"; shift 2 ;;
    --phone-timeout) PHONE_TIMEOUT="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

if [ ! -x "$PROXY_DIR/usb-proxy" ]; then
  echo "!! no usb-proxy at $PROXY_DIR/usb-proxy" >&2
  echo "   on the Pi:  $REPO_HINT/scripts/apply_usb_proxy_o3.sh" >&2
  exit 1
fi
# Natural-flow needs the follow flag; refuse to run natural without it.
# Note: usb-proxy --help exits 1 (usage()), so with pipefail a bare pipe always
# "fails" even when grep matches — capture help text first.
if [ "$MODE" = natural ]; then
  HELP_OUT=$("$PROXY_DIR/usb-proxy" --help 2>&1 || true)
  if ! printf '%s\n' "$HELP_OUT" | grep -q follow_aoa_reenum; then
    echo "!! usb-proxy at $PROXY_DIR lacks --follow_aoa_reenum" >&2
    echo "   re-apply O3 patches:  $REPO_HINT/scripts/apply_usb_proxy_o3.sh" >&2
    exit 1
  fi
fi

mkdir -p "$CAPDIR"
TS=$(date +%F_%H%M%S)
TAG="pi-mitm"
[ "$MODE" = natural ] && TAG="pi-mitm_natural" || TAG="pi-mitm_pretrigger"
[ -n "$IFRAME" ] && TAG="${TAG}_iframe"
PLOG="$CAPDIR/${TS}_${TAG}_payload.log"
PROX="$CAPDIR/${TS}_${TAG}_usbproxy.stdout"
UMON="$CAPDIR/${TS}_${TAG}_usbmon0u.txt"

sudo modprobe usbmon 2>/dev/null || true
sudo modprobe raw_gadget 2>/dev/null || true

echo "============================================================"
echo " Phase A mode : $MODE"
echo " payload log  : $PLOG"
echo " proxy stdout : $PROX"
echo " iframe inject: ${IFRAME:-off}"
echo "============================================================"

find_phone() {
  local vid="$1" pid="$2"
  lsusb | grep -i "${vid}:${pid}" | head -1
}

if [ "$MODE" = pretrigger ]; then
  # --- OLD RECIPE: ensure accessory mode before proxy starts ---------------
  ACC=$(lsusb | grep -oiE '18d1:2d0[0-9]' | head -1 || true)
  if [ -z "$ACC" ]; then
    echo "== phone not in accessory mode; pre-triggering AOA (watch phone) =="
    if [ -z "$(find_phone "$PHONE_VID" "$PHONE_PID")" ]; then
      echo "!! no phone at ${PHONE_VID}:${PHONE_PID} — plug phone into Pi USB-A, unlock it" >&2
      echo "   current lsusb:"; lsusb
      exit 1
    fi
    sudo python3 "$REPO_HINT/poc/pi_mitm/aoa_trigger.py" \
      --vid "0x${PHONE_VID}" --pid "0x${PHONE_PID}" --wait 8 || true
    ACC=$(lsusb | grep -oiE '18d1:2d0[0-9]' | head -1 || true)
  fi
  if [ -z "$ACC" ]; then
    echo "!! phone never entered accessory mode — check phone screen / DJI Fly install" >&2
    exit 1
  fi
  AVID=${ACC%%:*}; APID=${ACC##*:}
  echo "== phone in accessory mode: $AVID:$APID =="
  FOLLOW_ARGS=()
  FOLLOW_ENV=()
else
  # --- NATURAL FLOW: phone must start as MTP/charging, NOT accessory --------
  if lsusb | grep -qiE '18d1:2d0[0-9]'; then
    echo "!! phone is ALREADY in accessory mode (18d1:2d0x)." >&2
    echo "   Natural flow needs the goggles to drive AOA themselves." >&2
    echo "   Unplug/replug the phone so it comes back as MTP (${PHONE_VID}:${PHONE_PID})," >&2
    echo "   or re-run with --pretrigger for the old recipe." >&2
    exit 1
  fi
  if [ -z "$(find_phone "$PHONE_VID" "$PHONE_PID")" ]; then
    echo "!! no phone at ${PHONE_VID}:${PHONE_PID} on Pi USB-A" >&2
    echo "   set --vid/--pid from \`lsusb\`, unlock the phone, disable USB restricted mode." >&2
    echo "   current lsusb:"; lsusb
    exit 1
  fi
  AVID="$PHONE_VID"; APID="$PHONE_PID"
  echo "== phone in MTP/charging mode: $AVID:$APID (goggles will drive AOA) =="
  FOLLOW_ARGS=(--follow_aoa_reenum)
  FOLLOW_ENV=(USBPROXY_FOLLOW_AOA=1)
fi

# backup usbmon text (truncated 32B — not primary)
sudo timeout 180 cat /sys/kernel/debug/usb/usbmon/0u > "$UMON" 2>/dev/null &
UMON_PID=$!

ENV_VARS=(
  "USBPROXY_PAYLOAD_LOG=$PLOG"
)
[ -n "$IFRAME" ] && ENV_VARS+=("USBPROXY_IFRAME=1")
[ "$MODE" = natural ] && ENV_VARS+=("USBPROXY_AOA_TIMEOUT=$PHONE_TIMEOUT")
# shellcheck disable=SC2206
ENV_VARS+=("${FOLLOW_ENV[@]+"${FOLLOW_ENV[@]}"}")

echo "== starting usb-proxy. PLUG GOGGLES INTO Pi USB-C (VBUS-isolated) NOW. =="
echo "   Ctrl-C to stop. After GO FLY + ~30s video (or failure), stop and pull $PLOG"
echo "   Analyze: python3 $REPO_HINT/analysis/aoa_capture_parse.py $PLOG"
echo "            python3 $REPO_HINT/analysis/gofly_transition.py $PLOG"
cd "$PROXY_DIR"
# Build env string for sudo
ENV_EXPORT=""
for e in "${ENV_VARS[@]}"; do ENV_EXPORT="$ENV_EXPORT $e"; done

# stdbuf -oL: line-buffer proxy stdout so the tee'd log is live over SSH/nohup
# (fully buffered pipes otherwise stay empty until exit — painful for bench).
# shellcheck disable=SC2086
sudo env $ENV_EXPORT stdbuf -oL -eL ./usb-proxy \
  --device=fe980000.usb --driver=fe980000.usb \
  --vendor_id="0x${AVID}" --product_id="0x${APID}" \
  --enable_customized_config \
  ${FOLLOW_ARGS[@]+"${FOLLOW_ARGS[@]}"} \
  -v 2>&1 | stdbuf -oL tee "$PROX"

echo "== usb-proxy exited =="
sudo kill "$UMON_PID" 2>/dev/null || true
sudo pkill -f 'cat /sys/kernel/debug/usb/usbmon' 2>/dev/null || true
ls -la "$PLOG" "$PROX" "$UMON" 2>/dev/null || true
echo
echo "Next:"
echo "  python3 $REPO_HINT/analysis/aoa_capture_parse.py $PLOG"
echo "  python3 $REPO_HINT/analysis/gofly_transition.py $PLOG"
echo "Look for: gvg (cs=0/0xdd) ANSWERED, I-frame bit5 set, bandwidth jump."
