#!/usr/bin/env bash
#
# DUML probe battery for the DJI Goggles 2 over the AOA/logiclink pipe.
# Requires: the aoa_sniffer app connected+holding the accessory (replug goggles, pick app),
# and `adb` reachable. Fires each command via `am broadcast`, spaced so responses are easy
# to attribute in logcat.
#
# Usage:
#   ./probe_battery.sh <adb-serial> reference  # direct no-Pi version-query baseline (2026-07-13)
#   ./probe_battery.sh <adb-serial>            # curated SAFE battery (Phase A + B) — default
#   ./probe_battery.sh <adb-serial> sweep      # full cmd_set=1 (SPECIAL) cmd_id sweep, EMPTY payload (Phase C)
#   ./probe_battery.sh <adb-serial> iframe     # full cmd_set=1 sweep, KNOWN 11-byte I-frame
#                                               # SpecialControllPack payload attached (Phase D)
#   ./probe_battery.sh <adb-serial> all        # A + B + C
#
# Run logcat separately (recommended) to watch responses live:
#   adb -s <serial> logcat -v time -s AoaSniffer:V
# The sweep mode ALSO auto-dumps + summarises logcat itself at the end (see Phase C).
#
# SAFETY: only sends read-only version queries and image-transmission / liveview control,
# always toward the CAMERA / image-link / goggles subsystems — never the flight controller.
# Deliberately AVOIDS cmd_set=3 (Flight Control) entirely, and blocklists the SPECIAL
# arm/disarm/SDK-control-mode ids (0x00-0x05) even in the full sweep. See SPECIAL_BLOCKLIST.
#
# DUML device types (receiver): 1=CAMERA 2=MOBILE_APP 3=FLYCTRL 4=GIMBAL 6=RC 7=WIFI
#   8=LB_DM3XX_SKY 9=LB_MCU_SKY 10=PC 11=BATTERY 13=DM368_GROUND 14=OFDM_GROUND 17=GPS
# cmd_set: 0=GENERAL 1=SPECIAL 2=CAMERA 9=HD_LINK/OFDM

set -euo pipefail
SERIAL="${1:?usage: probe_battery.sh <adb-serial> [battery|sweep|all]}"
MODE="${2:-battery}"
A=com.o3re.aoasniffer.CMD
GAP=1.5          # curated battery pacing
SWEEP_GAP=0.8    # sweep pacing (256 sends -> ~3.5 min)

# Where to write the correlated sweep log. captures/ is append-only by convention.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CAP_DIR="$REPO_ROOT/captures/aoa"

# --- adb wrapper: some rigs need sudo for the usb/adb link (see protocol_notes) ---
ADB() { sudo adb -s "$SERIAL" "$@"; }

send() {  # send cmdset cmdid receiver [payloadhex]
  local cs="$1" ci="$2" rx="$3" pl="${4:-}"
  echo ">>> cmdset=$cs cmdid=$ci recv=$rx payload=${pl:-<none>}"
  ADB shell am broadcast -a "$A" \
    --es cmdset "$cs" --es cmdid "$ci" --es receiver "$rx" ${pl:+--es payload "$pl"} \
    >/dev/null 2>&1 || true
  sleep "$GAP"
}

run_curated() {
  echo "===== PHASE A: bus map — GENERAL/VersionInquiry (cmd_set=0 cmd_id=0x01), read-only ====="
  for rx in 1 3 4 6 7 8 9 10 11 13 14 17; do
    send 0x00 0x01 "$rx"
  done

  echo "===== PHASE B: liveview / I-frame candidates (speculative, image-transmission only) ====="
  # Baseline repro (we know this returns 0xE0):
  send 0x01 0x21 1
  # SPECIAL neighbors of Start-Video-Ctrl (empty payload), to camera:
  send 0x01 0x23 1
  send 0x01 0x24 1
  send 0x01 0x25 1
  # HD_LINK liveview-mode/config to sky link + camera, small payloads:
  send 0x09 0x34 8 01
  send 0x09 0x34 1 01
  send 0x09 0x39 8
  send 0x09 0x36 8
  send 0x09 0x35 8
  # CAMERA cmd_set — request-ish ids toward the camera:
  send 0x02 0x7c 1
  send 0x02 0x7f 1

  echo "===== battery complete — inspect logcat for RESP lines, rate jumps, LARGE FRAME ====="
}

# ---------------------------------------------------------------------------
# PHASE C: full cmd_set=1 (SPECIAL) cmd_id sweep.
# Rationale: SpecialCommandManager::RequestIFrameForLiveView (found in DJI Fly's native
# libs) maps to cmd_set=1. We've only tried 0x21/0x23/0x24/0x25 by hand. This walks the
# whole 0x00-0xFF id space with an empty payload, then correlates firmware responses from
# logcat so accepted (non-0xE0) ids and any video-scale LARGE FRAME jump to the top.
#
# NOTE (expectation-setting): RequestIFrameForLiveView reads as "re-request a keyframe on
# an ALREADY-flowing stream", so a bare I-frame request may not, by itself, start video —
# there may be a prior StartLiveStreaming/subscribe step. Treat any id that returns 0x00
# (accepted) as a candidate to follow up with payloads / as step 2 of a pair, not as a
# guaranteed one-shot trigger. A bandwidth jump on any id is the jackpot.
# ---------------------------------------------------------------------------

# SPECIAL ids we will NOT send, ever, in the sweep (flight-safety / mode-control).
# 0x00-0x05 cover SDK-control-mode open/close + arm/disarm-adjacent control per the
# dji-firmware-tools SPECIAL table; skip them even though we target the camera, not FLYCTRL.
SPECIAL_BLOCKLIST=(0x00 0x01 0x02 0x03 0x04 0x05)

# Receivers to sweep. CAMERA(1) is the safe primary target (never FLYCTRL(3)).
# Override with RX_LIST="1 9 14" env var (space-separated) without editing this file.
# 2026-07-07: a camera-only (1) pass of both the empty-payload and known-iframe-payload
# sweeps came back 100% 0xE0 for all 250 non-blocklisted ids — camera doesn't implement
# cmd_set=1 at all on this path. Known-accepted cmd_set=9 commands (HDLnk Mode Set 0x34,
# Config Set 0x39) targeted air unit(9)/goggles(14), not camera — so default now includes
# those two as well.
if [[ -n "${RX_LIST:-}" ]]; then
  read -ra SWEEP_RECEIVERS <<< "$RX_LIST"
else
  SWEEP_RECEIVERS=(1 9 14)
fi

is_blocked() { local x; for x in "${SPECIAL_BLOCKLIST[@]}"; do [[ "$1" == "$x" ]] && return 0; done; return 1; }

# ---------------------------------------------------------------------------
# PHASE D: same cmd_id sweep, but with the KNOWN 11-byte SpecialControllPack
# payload attached (see docs/protocol_notes.md "Static RE — reopened" +
# handover/README_HERMES.md). Static RE of SendSpecialControllPack() @0x14879a4
# in libdjisdk_jni.so recovered the payload the app builds for an I-frame-for-
# liveview request; the ONE thing that static RE could NOT recover is the outer
# DUML cmd_id (transmit is a runtime-bound PLT/GOT thunk into another module,
# not resolvable from this .so). So: attach the known-correct payload, sweep
# the unknown cmd_id space around it.
#
# Payload layout (11 bytes, indices 0-10):
#   byte5      = request-flags bitfield; bit5 (0x20) = I-frame-for-liveview
#                (bit6=ch1 reset, bit7=ch2 reset — NOT set here)
#   byte9      = XOR-8 checksum over bytes[0..8]
#   byte10     = sub-cmd/param field; 0x00 = default (I-frame path leaves it default)
#   bytes 0-4,6-8 = device/datalink-id fields — populated at runtime from live
#                session state on the real device; UNKNOWN here, so sent as 0x00.
#                If the whole sweep comes back silent/rejected, this is the first
#                thing to revisit (real datalink_id may be required, not zero).
# With bytes 0-8 all zero except byte5=0x20, XOR-8 checksum = 0x20.
IFRAME_PAYLOAD="0000000000200000002000"

run_sweep() {  # run_sweep <payload-hex-or-empty> <label> <logfile-suffix>
  local payload="$1" label="$2" suffix="$3"
  mkdir -p "$CAP_DIR"
  local ts; ts="$(date +%Y%m%d_%H%M%S)"
  local logf="$CAP_DIR/${ts}_${suffix}_logcat.txt"

  echo "===== $label: full cmd_set=1 (SPECIAL) sweep, receivers=[${SWEEP_RECEIVERS[*]}] ====="
  echo "     payload=${payload:-<none>}"
  echo "     blocklist (never sent): ${SPECIAL_BLOCKLIST[*]}"
  echo "     correlated log -> $logf"
  echo "     (also watch live: adb -s $SERIAL logcat -v time -s AoaSniffer:V)"

  # Clear the log buffer so the dump at the end contains only this sweep's SEND/RESP lines.
  ADB logcat -c >/dev/null 2>&1 || true

  local rx ci
  for rx in "${SWEEP_RECEIVERS[@]}"; do
    for i in $(seq 0 255); do
      ci="$(printf '0x%02x' "$i")"
      if is_blocked "$ci"; then
        echo "--- skip (blocklist) cmdset=0x01 cmdid=$ci recv=$rx"
        continue
      fi
      echo ">>> cmdset=0x01 cmdid=$ci recv=$rx payload=${payload:-<none>}"
      ADB shell am broadcast -a "$A" \
        --es cmdset 0x01 --es cmdid "$ci" --es receiver "$rx" ${payload:+--es payload "$payload"} \
        >/dev/null 2>&1 || true
      sleep "$SWEEP_GAP"
    done
  done

  # Give the last responses time to land, then dump + correlate.
  sleep 1.0
  ADB logcat -v time -d -s AoaSniffer:V > "$logf" 2>/dev/null || true

  echo
  echo "===== sweep complete — correlating $logf ====="

  echo "--- LARGE FRAME hits (possible VIDEO) ---"
  grep -F "LARGE FRAME" "$logf" || echo "  (none — no video-scale frame appeared)"

  echo "--- ACCEPTED responses (cmd_set=1, payload NOT starting 0xE0/0xE3) ---"
  # RESP line format (see MainActivity.handleFrame):
  #   RESP seq=.. src=.. dst=.. cmdset=0x1 cmdid=0x.. payload=xx xx ..
  # Surface only SPECIAL responses whose first payload byte isn't the unknown/bad-param codes.
  if grep -E "RESP .*cmdset=0x1 " "$logf" \
       | grep -Ev "payload=(e0|e3)( |$)" \
       | sort -u; then :; else
    echo "  (no accepted SPECIAL responses — every reply was 0xE0/0xE3 or silent)"
  fi

  echo "--- all SPECIAL responses seen (for reference) ---"
  grep -E "RESP .*cmdset=0x1 " "$logf" | sort -u || echo "  (none)"

  echo
  echo "Next: any cmdid above that returned 0x00 is a live-view candidate — re-run it with"
  echo "small payloads, or send it as step 2 after a StartLiveStreaming-style command, and"
  echo "watch the app's rolling byte-rate for a jump from telemetry (~11-18 kbit/s) to video."
}

# ---------------------------------------------------------------------------
# REFERENCE (2026-07-13): direct no-Pi baseline for the delivery-drop hypothesis.
# Fires cmd_set=0/0x01 VersionInquiry straight at the goggles via aoa_sniffer (no Pi,
# no DJI Fly) and records which receivers answer. In the BRIDGED Pi capture DJI Fly's
# 0x01 queries got only 0-3% answered; if the goggles answer HERE (direct), the Pi
# bridge's app->goggles relay is the confirmed drop point. Pairs with the DELIVERY
# AUDIT (TXG/TXP) in analysis/aoa_capture_parse.py. Receivers include the exact low-5-bit
# receiver_types DJI Fly was seen querying (0, 2, 14=OFDM_GND, 31) plus the standard bus.
# ---------------------------------------------------------------------------
REFERENCE_RECEIVERS=(0 2 14 31 1 3 4 6 7 8 9 10 11 13 17)

run_reference() {
  mkdir -p "$CAP_DIR"
  local ts; ts="$(date +%Y%m%d_%H%M%S)"
  local logf="$CAP_DIR/${ts}_reference_logcat.txt"
  echo "===== REFERENCE: direct (no-Pi) version-query baseline ====="
  echo "     cmd_set=0/0x01 VersionInquiry -> receivers: ${REFERENCE_RECEIVERS[*]}"
  echo "     correlated log -> $logf"
  echo "     (also watch live: adb -s $SERIAL logcat -v time -s AoaSniffer:V)"
  ADB logcat -c >/dev/null 2>&1 || true
  local pass rx
  for pass in 1 2 3; do
    echo "--- pass $pass ---"
    for rx in "${REFERENCE_RECEIVERS[@]}"; do
      send 0x00 0x01 "$rx"
    done
  done
  sleep 1.0
  ADB logcat -v time -d -s AoaSniffer:V > "$logf" 2>/dev/null || true
  echo
  echo "===== reference complete — auditing $logf ====="
  if ! python3 "$REPO_ROOT/analysis/reference_audit.py" "$logf"; then
    echo "(analyzer unavailable; raw cmd_set=0 responses:)"
    grep -E "RESP .*cmdset=0x0 " "$logf" | sort -u || echo "  (no cmd_set=0 responses seen)"
  fi
  echo
  echo "INTERPRET: goggles answered directly here vs 0-3% through the Pi bridge =>"
  echo "the bridge's gadget IN-endpoint relay is dropping app->goggles commands."
}

case "$MODE" in
  reference) run_reference ;;
  battery) run_curated ;;
  sweep)   run_sweep "" "PHASE C" "special_sweep" ;;
  iframe)  run_sweep "$IFRAME_PAYLOAD" "PHASE D (known I-frame payload)" "iframe_sweep" ;;
  all)     run_curated; echo; run_sweep "" "PHASE C" "special_sweep"; echo; run_sweep "$IFRAME_PAYLOAD" "PHASE D (known I-frame payload)" "iframe_sweep" ;;
  *) echo "unknown mode '$MODE' (want: battery | sweep | iframe | all)" >&2; exit 2 ;;
esac
