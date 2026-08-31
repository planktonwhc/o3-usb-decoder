#!/usr/bin/env bash
# Multi-signal USB-host presence probe for the O3 AOA gadget path.
#
# WHY THIS EXISTS
#   Agents and operators used to treat
#     cat /sys/class/udc/fe980000.usb/state   →  configured
#   as "goggles are plugged" and refused systemctl restart (gotcha #4).
#   That is WRONG: after a real unplug, dwc2/raw-gadget often leaves the UDC
#   stuck at 'configured' with no host (phantom). Owner field case 2026-07-16:
#   udc=configured, goggles NOT attached, restart blocked for no reason.
#
# RULE
#   Never gate stop/restart solely on UDC state.
#   Cross-check: endpoint journal (still-waiting / au_feed / ES), session
#   configured flag, bulk/ES silence, service state. Owner word overrides.
#
# NOTE ON lsusb
#   On the live AOA path the *goggles are the USB host* and the Pi is the
#   gadget — lsusb on the Pi does NOT list the goggles. Do not use "lsusb has
#   no DJI device" as host-absent proof for AOA. (lsusb only helps PC-as-host
#   RNDIS mode, which is not the HDMI path.)
#
# Usage (on Pi):
#   scripts/o3_host_probe.sh              # human report
#   scripts/o3_host_probe.sh --json       # machine-readable
#   scripts/o3_host_probe.sh --quiet      # exit code only
#   scripts/o3_host_probe.sh --window 45  # journal lookback seconds (default 40)
#
# Exit codes:
#   0  SAFE_STOP  — no real-host evidence (idle / not attached / phantom OK to stop)
#   1  LIVE_HOST  — strong evidence goggles are live (gotcha #4 applies)
#   2  UNCERTAIN  — mixed/weak signals; default allow safe_stop with warning
#   3  usage / hard error
set -euo pipefail

UDC="${UDC:-fe980000.usb}"
WINDOW_S="${O3_HOST_PROBE_WINDOW_S:-40}"
MODE=human   # human | json | quiet

while [[ $# -gt 0 ]]; do
  case "$1" in
    --json) MODE=json; shift ;;
    --quiet|-q) MODE=quiet; shift ;;
    --window)
      WINDOW_S="${2:-}"
      if [[ -z "$WINDOW_S" ]]; then
        echo "!! --window needs a seconds value" >&2
        exit 3
      fi
      shift 2
      ;;
    --window=*)
      WINDOW_S="${1#--window=}"
      shift
      ;;
    -h|--help)
      sed -n '2,40p' "$0"
      exit 0
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 3
      ;;
  esac
done

# --- gather signals ----------------------------------------------------------

udc_state="?"
if [[ -r /sys/class/udc/$UDC/state ]]; then
  udc_state="$(tr -d '\r\n' </sys/class/udc/$UDC/state 2>/dev/null || echo '?')"
fi

udc_function=""
if [[ -r /sys/class/udc/$UDC/function ]]; then
  udc_function="$(tr -d '\r\n' </sys/class/udc/$UDC/function 2>/dev/null || true)"
fi

svc_active="inactive"
if command -v systemctl >/dev/null 2>&1; then
  svc_active="$(systemctl is-active o3-hdmi 2>/dev/null | head -1 | tr -d '\r' || echo inactive)"
  svc_active="${svc_active:-inactive}"
fi

raw_holders="none"
if command -v fuser >/dev/null 2>&1 && [[ -e /dev/raw-gadget ]]; then
  raw_holders="$(fuser /dev/raw-gadget 2>/dev/null | tr -s ' ' | sed 's/^ *//' || true)"
  raw_holders="${raw_holders:-none}"
fi

# Recent o3-hdmi journal (boot-scoped lookback by time when possible).
journal_txt=""
if command -v journalctl >/dev/null 2>&1; then
  journal_txt="$(journalctl -u o3-hdmi --since "${WINDOW_S} seconds ago" --no-pager -o cat 2>/dev/null || true)"
  if [[ -z "$journal_txt" ]]; then
    # fallback: last N lines if clock skew / empty since-filter
    journal_txt="$(journalctl -u o3-hdmi -n 120 --no-pager -o cat 2>/dev/null || true)"
  fi
fi

# Parse journal signals (CHRONOLOGICAL — most recent wins) --------------------
# Field case 2026-07-16: au_feed=50 then phantom unplug within the window must
# NOT count as LIVE_HOST (stale au_feed after host gone).
au_feed_live=0
au_feed_last=""
es_live=0
bulk_live=0
presenter_live=0
still_waiting=0
still_waiting_cfg_false=0
still_waiting_line=""
phantom_rearm=0
idle_splash=0
connect_seen=0
accessory_mode=0

# Line indices (1-based) of the most recent live vs dead-host evidence.
last_live_idx=0
last_dead_idx=0
last_live_why=""
last_dead_why=""
line_idx=0

while IFS= read -r line || [[ -n "$line" ]]; do
  [[ -z "$line" ]] && continue
  line_idx=$((line_idx + 1))

  # --- dead-host / idle markers (supersede earlier live) ---
  if [[ "$line" == *'phantom unplug'* ]] || [[ "$line" == *'phantom host'* ]]; then
    phantom_rearm=1
    last_dead_idx=$line_idx
    last_dead_why="phantom_unplug"
  fi
  if [[ "$line" == *'still waiting for goggles USB video'* ]]; then
    still_waiting=1
    still_waiting_line="$line"
    if [[ "$line" == *'configured=False'* || "$line" == *'configured=false'* ]]; then
      still_waiting_cfg_false=1
      last_dead_idx=$line_idx
      last_dead_why="still_waiting_cfg_false"
    fi
  fi
  if [[ "$line" == *'video silent'* && "$line" == *'no resume'* ]] || \
     [[ "$line" == *'stopping I/O + re-arm'* ]]; then
    last_dead_idx=$line_idx
    last_dead_why="video_silent_or_rearm"
  fi
  if [[ "$line" == *'IDLE_SPLASH'* || "$line" == *'idle splash'* || \
        "$line" == *'in-process idle splash'* || "$line" == *'waiting for live FPV feed'* ]]; then
    idle_splash=1
    # Splash alone is weak; only mark dead if no concurrent feed on same line.
    if [[ "$line" != *'au_feed='* ]]; then
      last_dead_idx=$line_idx
      last_dead_why="idle_splash"
    fi
  fi

  # --- live markers ---
  if [[ "$line" =~ au_feed[=≈~]+([0-9]+(\.[0-9]+)?) ]]; then
    au_feed_last="${BASH_REMATCH[1]}"
    if awk -v v="$au_feed_last" 'BEGIN{exit !(v+0 >= 0.5)}'; then
      last_live_idx=$line_idx
      last_live_why="au_feed=${au_feed_last}"
    fi
  fi
  if [[ "$line" =~ es_bytes=([1-9][0-9]*) ]]; then
    last_live_idx=$line_idx
    last_live_why="es_bytes=${BASH_REMATCH[1]}"
  fi
  if [[ "$line" =~ bulk\ OUT\ ([1-9][0-9]{2,})B ]]; then
    last_live_idx=$line_idx
    last_live_why="bulk_out"
  fi
  # Steady presenter cadence (atomic stats), not the word "present" alone.
  if [[ "$line" =~ present[=≈]([1-9][0-9]*(\.[0-9]+)?) ]] || \
     [[ "$line" =~ vf_fps[=≈]([1-9]) ]]; then
    presenter_live=1
    last_live_idx=$line_idx
    last_live_why="presenter_fps"
  fi

  if [[ "$line" == *'CONNECT (mode='* ]]; then
    connect_seen=1
  fi
  if [[ "$line" == *'mode=accessory'* || "$line" == *'ACCESSORY_START'* || \
        "$line" == *'configured in accessory'* ]]; then
    accessory_mode=1
  fi
done <<<"$journal_txt"

# Live only if most recent evidence is live (not superseded by dead/phantom).
if [[ "$last_live_idx" -gt 0 && "$last_live_idx" -gt "$last_dead_idx" ]]; then
  au_feed_live=1
  es_live=1
  # bulk/presenter already reflected in last_live_why
  if [[ "$last_live_why" == bulk_out* ]]; then bulk_live=1; fi
else
  au_feed_live=0
  es_live=0
  bulk_live=0
  presenter_live=0
  # If we had feed earlier but dead won, clear "live" flags; keep au_feed_last for notes.
fi

# --- decision ----------------------------------------------------------------
# Strong LIVE evidence (gotcha #4 applies — do not tear down raw-gadget):
reasons_live=()
reasons_safe=()
reasons_note=()

if [[ "$last_live_idx" -gt 0 && "$last_live_idx" -gt "$last_dead_idx" ]]; then
  reasons_live+=("latest_journal=${last_live_why} (line ${last_live_idx}>dead ${last_dead_idx})")
fi

# Strong SAFE evidence:
if [[ "$udc_state" == "not attached" || "$udc_state" == "default" ]]; then
  reasons_safe+=("udc=${udc_state}")
fi
if [[ "$still_waiting_cfg_false" -eq 1 && "$last_dead_idx" -ge "$last_live_idx" ]]; then
  reasons_safe+=("still_waiting configured=False es idle")
fi
if [[ "$phantom_rearm" -eq 1 && "$last_dead_idx" -ge "$last_live_idx" ]]; then
  reasons_safe+=("phantom_rearm after/without live (dead_line=${last_dead_idx} live_line=${last_live_idx})")
fi
if [[ "$svc_active" != "active" && "$last_live_idx" -eq 0 ]]; then
  reasons_safe+=("o3-hdmi not active")
fi
if [[ "$idle_splash" -eq 1 && "$last_dead_idx" -ge "$last_live_idx" ]]; then
  reasons_safe+=("idle_splash_no_current_es")
fi
# Stale au_feed superseded by phantom/dead is the classic false LIVE_HOST case.
if [[ "$last_dead_idx" -gt "$last_live_idx" && "$last_dead_idx" -gt 0 ]]; then
  reasons_safe+=("latest_journal=${last_dead_why} supersedes older live")
  reasons_note+=("stale_live_ignored=${last_live_why:-none}@${last_live_idx}")
fi

# UDC configured alone is NEVER a live reason — annotate only.
if [[ "$udc_state" == "configured" ]]; then
  reasons_note+=("udc=configured (NOT proof of host — phantom common after unplug)")
fi
if [[ -n "$udc_function" ]]; then
  reasons_note+=("udc_function=${udc_function}")
fi
if [[ "$raw_holders" != "none" ]]; then
  reasons_note+=("raw-gadget holders: ${raw_holders}")
fi
if [[ "$still_waiting" -eq 1 && "$still_waiting_cfg_false" -eq 0 ]]; then
  reasons_note+=("still_waiting (configured may be True — check ES)")
fi
if [[ "$connect_seen" -eq 1 && "$last_live_idx" -le "$last_dead_idx" ]]; then
  reasons_note+=("CONNECT seen but no current au_feed (idle/handshake/phantom)")
fi

verdict="UNCERTAIN"
exit_code=2

if [[ ${#reasons_live[@]} -gt 0 ]]; then
  verdict="LIVE_HOST"
  exit_code=1
elif [[ ${#reasons_safe[@]} -gt 0 ]]; then
  # If we only have "idle splash" while udc=configured and no still_waiting, still SAFE
  # for stop — that is exactly the phantom case operators hit.
  verdict="SAFE_STOP"
  exit_code=0
else
  # No strong signals either way.
  # Special case: udc configured + zero *current* live stream evidence → SAFE_STOP
  # (phantom recovery path). This is the fix for the agent false-refuse.
  if [[ "$udc_state" == "configured" && "$last_live_idx" -le "$last_dead_idx" ]]; then
    verdict="SAFE_STOP"
    exit_code=0
    reasons_safe+=("udc=configured but no current ES/au_feed → treat as phantom/idle")
  elif [[ "$udc_state" == "not attached" || "$udc_state" == "default" || "$udc_state" == "?" ]]; then
    verdict="SAFE_STOP"
    exit_code=0
    reasons_safe+=("udc=${udc_state} + no live signals")
  else
    verdict="UNCERTAIN"
    exit_code=2
    reasons_note+=("mixed/weak; prefer safe_stop with owner confirm if unplugged")
  fi
fi

# Join helpers
join_by() {
  local IFS="$1"; shift; echo "$*"
}
live_s="$(join_by '; ' "${reasons_live[@]:-}")"
safe_s="$(join_by '; ' "${reasons_safe[@]:-}")"
note_s="$(join_by '; ' "${reasons_note[@]:-}")"

# Recommendation for agents/operators
case "$verdict" in
  LIVE_HOST)
    action="REFUSE raw systemctl stop/restart (gotcha #4). Unplug goggles first, then: sudo scripts/o3_hdmi_safe_stop.sh --restart. Override only with --force if owner confirms unplugged."
    ;;
  SAFE_STOP)
    action="OK to: sudo scripts/o3_hdmi_safe_stop.sh --restart  (do NOT refuse solely because udc looks configured)."
    ;;
  UNCERTAIN)
    action="Prefer: ask owner if goggles plugged; if unplugged → sudo scripts/o3_hdmi_safe_stop.sh --restart. Never hard-refuse on UDC alone."
    ;;
esac

if [[ "$MODE" == "quiet" ]]; then
  exit "$exit_code"
fi

if [[ "$MODE" == "json" ]]; then
  # minimal JSON (no jq dependency)
  esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
  printf '{'
  printf '"verdict":"%s",' "$(esc "$verdict")"
  printf '"exit":%s,' "$exit_code"
  printf '"udc_state":"%s",' "$(esc "$udc_state")"
  printf '"udc_function":"%s",' "$(esc "$udc_function")"
  printf '"svc_active":"%s",' "$(esc "$svc_active")"
  printf '"raw_holders":"%s",' "$(esc "$raw_holders")"
  printf '"au_feed_live":%s,' "$au_feed_live"
  printf '"au_feed_last":"%s",' "$(esc "${au_feed_last:-}")"
  printf '"es_live":%s,' "$es_live"
  printf '"still_waiting":%s,' "$still_waiting"
  printf '"still_waiting_cfg_false":%s,' "$still_waiting_cfg_false"
  printf '"window_s":%s,' "$WINDOW_S"
  printf '"reasons_live":"%s",' "$(esc "$live_s")"
  printf '"reasons_safe":"%s",' "$(esc "$safe_s")"
  printf '"notes":"%s",' "$(esc "$note_s")"
  printf '"action":"%s"' "$(esc "$action")"
  printf '}\n'
  exit "$exit_code"
fi

# human
echo "=== O3 host probe $(date -Is) ==="
echo "udc=$UDC state=$udc_state function=${udc_function:-∅}  o3-hdmi=$svc_active  window=${WINDOW_S}s"
echo "raw-gadget holders: $raw_holders"
echo
echo "signals:"
echo "  au_feed_last=${au_feed_last:-∅}  current_live=$(( last_live_idx > last_dead_idx ? 1 : 0 ))  live_idx=$last_live_idx ($last_live_why)  dead_idx=$last_dead_idx ($last_dead_why)"
echo "  still_waiting=$still_waiting cfg_false=$still_waiting_cfg_false  phantom_rearm=$phantom_rearm  idle_splash=$idle_splash"
echo "  accessory_mode=$accessory_mode  connect_seen=$connect_seen"
if [[ -n "$still_waiting_line" ]]; then
  echo "  last_wait: ${still_waiting_line:0:160}"
fi
echo
echo "verdict: $verdict  (exit $exit_code)"
[[ -n "$live_s" ]] && echo "  live evidence:  $live_s"
[[ -n "$safe_s" ]] && echo "  safe evidence:  $safe_s"
[[ -n "$note_s" ]] && echo "  notes:          $note_s"
echo
echo "action: $action"
echo
echo "reminder: udc=configured ≠ goggles plugged (phantom after unplug is common)."
echo "          prefer: sudo $(cd "$(dirname "$0")" && pwd)/o3_hdmi_safe_stop.sh --restart"

exit "$exit_code"
