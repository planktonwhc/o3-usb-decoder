#!/usr/bin/env bash
# event_health.sh — preflight / booth health for the O3 → HDMI unit.
#
# Run on the Pi (SSH or local). Safe read-only checks; does not stop services.
#
#   scripts/event_health.sh              # human report + exit code
#   scripts/event_health.sh --preflight  # stricter (monitor + service required)
#   scripts/event_health.sh --json       # machine-readable one object
#   scripts/event_health.sh --quiet      # exit code only
#
# Exit codes:
#   0  all required checks green
#   1  warnings only (usable with care)
#   2  critical failure (do not open doors / do not start the show)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
UDC="${UDC:-fe980000.usb}"
DRM_DEVICE="${DRM_DEVICE:-/dev/dri/card1}"
DRM_CONNECTOR="${DRM_CONNECTOR:-HDMI-A-2}"
MODE="human"   # human | json | quiet
PREFLIGHT=0

for a in "$@"; do
  case "$a" in
    --preflight) PREFLIGHT=1 ;;
    --json) MODE=json ;;
    --quiet|-q) MODE=quiet ;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

# results: name|level|detail  level=ok|warn|crit
RESULTS=()
CRIT=0
WARN=0

add() {
  local level="$1" name="$2" detail="$3"
  RESULTS+=("${level}|${name}|${detail}")
  case "$level" in
    crit) CRIT=$((CRIT + 1)) ;;
    warn) WARN=$((WARN + 1)) ;;
  esac
}

# --- checks ------------------------------------------------------------------

# 1) o3-hdmi service
is_active() {
  # Avoid `cmd | head || echo` under pipefail: SIGPIPE makes the pipeline fail and
  # appends a second "inactive". Capture fully, then take first line.
  local raw
  raw="$(systemctl is-active "$1" 2>/dev/null || true)"
  raw="$(printf '%s\n' "$raw" | head -1 | tr -d '\r')"
  echo "${raw:-inactive}"
}
if command -v systemctl >/dev/null 2>&1; then
  st="$(is_active o3-hdmi)"
  st="${st:-inactive}"
  if [[ "$st" == "active" ]]; then
    add ok o3-hdmi "active"
  elif [[ "$PREFLIGHT" -eq 1 ]]; then
    add crit o3-hdmi "not active ($st)"
  else
    add warn o3-hdmi "not active ($st)"
  fi

  wd="$(is_active o3-oops-watchdog)"
  wd="${wd:-inactive}"
  if [[ "$wd" == "active" ]]; then
    add ok oops-watchdog "active"
  else
    if [[ "$PREFLIGHT" -eq 1 ]]; then
      add warn oops-watchdog "not active ($wd) — kernel wedge will not auto-reboot"
    else
      add warn oops-watchdog "not active ($wd)"
    fi
  fi
else
  add warn systemd "systemctl not available"
fi

# 2) power / throttle
if command -v vcgencmd >/dev/null 2>&1; then
  thr="$(vcgencmd get_throttled 2>/dev/null | tr -d '\r' || echo unknown)"
  # throttled=0x0 is healthy; sticky bits mean undervoltage/throttling happened
  if [[ "$thr" == "throttled=0x0" ]]; then
    add ok power "$thr"
  elif [[ "$thr" == throttled=0x* ]]; then
    # any non-zero is a problem for gadget stability
    add crit power "$thr (want throttled=0x0 — fix PSU before events)"
  else
    add warn power "$thr"
  fi
  temp="$(vcgencmd measure_temp 2>/dev/null | tr -d '\r' || echo n/a)"
  # flag hot but not always fatal
  if [[ "$temp" =~ temp=([0-9.]+) ]]; then
    tnum="${BASH_REMATCH[1]}"
    if awk "BEGIN{exit !($tnum >= 80)}"; then
      add warn thermal "$temp (>=80C)"
    else
      add ok thermal "$temp"
    fi
  else
    add ok thermal "$temp"
  fi
else
  add warn power "vcgencmd missing"
fi

# 3) UDC + raw-gadget
if [[ -e /dev/raw-gadget ]]; then
  add ok raw-gadget "/dev/raw-gadget present"
else
  add crit raw-gadget "/dev/raw-gadget missing (modprobe raw_gadget?)"
fi

if [[ -e /sys/class/udc/$UDC ]]; then
  udc_st="$(cat /sys/class/udc/$UDC/state 2>/dev/null || echo '?')"
  # UDC alone is never a host-present proof — annotate phantom risk.
  if [[ "$udc_st" == "configured" ]]; then
    add ok udc "$UDC state=configured (≠ host present; phantom after unplug is common)"
  else
    add ok udc "$UDC state=$udc_st"
  fi
else
  add crit udc "$UDC missing"
fi

# 3b) multi-signal host presence (never UDC alone)
if [[ -x "$REPO/scripts/o3_host_probe.sh" ]]; then
  set +e
  host_json="$("$REPO/scripts/o3_host_probe.sh" --json 2>/dev/null)"
  host_rc=$?
  set -e
  host_verdict="$(printf '%s' "$host_json" | sed -n 's/.*"verdict":"\([^"]*\)".*/\1/p' | head -1)"
  host_verdict="${host_verdict:-?}"
  case "$host_rc" in
    0) add ok host-probe "SAFE_STOP ($host_verdict) — ok to safe_stop/restart" ;;
    1) add warn host-probe "LIVE_HOST — unplug before stop/restart (gotcha #4)" ;;
    2) add ok host-probe "UNCERTAIN — prefer safe_stop; do not refuse on UDC alone" ;;
    *) add warn host-probe "probe exit=$host_rc verdict=$host_verdict" ;;
  esac
else
  add warn host-probe "o3_host_probe.sh missing — install scripts from repo"
fi

# 4) DRM / monitor
if [[ -e "$DRM_DEVICE" ]]; then
  add ok drm-device "$DRM_DEVICE"
else
  add crit drm-device "$DRM_DEVICE missing"
fi

card="$(basename "$DRM_DEVICE")"
# connector dir is e.g. card1-HDMI-A-2
conn_dir=""
for d in /sys/class/drm/${card}-*; do
  [[ -e "$d/status" ]] || continue
  base="$(basename "$d")"
  # match HDMI-A-2 etc.
  if [[ "$base" == *"${DRM_CONNECTOR}"* ]] || [[ "$base" == "${card}-${DRM_CONNECTOR}" ]]; then
    conn_dir="$d"
    break
  fi
done
# fallback: any path ending with connector name
if [[ -z "$conn_dir" ]]; then
  for d in /sys/class/drm/${card}-*; do
    [[ -e "$d/status" ]] || continue
    [[ "$(basename "$d")" == *"$DRM_CONNECTOR" ]] && conn_dir="$d" && break
  done
fi

if [[ -n "$conn_dir" ]]; then
  cstat="$(cat "$conn_dir/status" 2>/dev/null || echo '?')"
  edid_n=0
  if [[ -r "$conn_dir/edid" ]]; then
    edid_n="$(wc -c <"$conn_dir/edid" 2>/dev/null | tr -d ' ' || echo 0)"
  fi
  if [[ "$cstat" == "connected" && "$edid_n" -ge 128 ]]; then
    add ok monitor "${DRM_CONNECTOR} connected edid=${edid_n}B"
  elif [[ "$cstat" == "connected" && "$edid_n" -lt 128 ]]; then
    add warn monitor "${DRM_CONNECTOR} connected but edid=${edid_n}B (phantom?)"
  else
    if [[ "$PREFLIGHT" -eq 1 ]]; then
      add crit monitor "${DRM_CONNECTOR} status=$cstat edid=${edid_n}B (need real monitor)"
    else
      add warn monitor "${DRM_CONNECTOR} status=$cstat edid=${edid_n}B"
    fi
  fi
else
  if [[ "$PREFLIGHT" -eq 1 ]]; then
    add crit monitor "connector $DRM_CONNECTOR not found under $card"
  else
    add warn monitor "connector $DRM_CONNECTOR not found under $card"
  fi
fi

# 5) deps for atomic path
if command -v gst-inspect-1.0 >/dev/null 2>&1 && gst-inspect-1.0 avdec_h264 >/dev/null 2>&1; then
  add ok avdec_h264 "present"
else
  add crit avdec_h264 "missing (apt install gstreamer1.0-libav)"
fi

if python3 -c "import gi; gi.require_version('Gst','1.0'); from gi.repository import Gst" 2>/dev/null; then
  add ok python-gi "Gst import ok"
else
  add crit python-gi "python3-gi / Gst introspection missing"
fi

# player env from service
player_env="?"
if command -v systemctl >/dev/null 2>&1; then
  player_env="$(systemctl show o3-hdmi -p Environment --value 2>/dev/null \
    | tr ' ' '\n' | sed -n 's/^O3_HDMI_PLAYER=//p' | tail -1)"
  player_env="${player_env:-unset}"
fi
if [[ "$player_env" == "atomic" ]]; then
  add ok player "O3_HDMI_PLAYER=atomic"
elif [[ "$player_env" == "unset" || -z "$player_env" ]]; then
  add ok player "O3_HDMI_PLAYER unset (code defaults to atomic)"
else
  add warn player "O3_HDMI_PLAYER=$player_env (field expects atomic)"
fi

# idle splash (owner request 2026-07-16) — script must exist; env defaults on
if [[ -f "$REPO/poc/pi_endpoint/idle_splash.py" ]]; then
  splash_env="$(systemctl show o3-hdmi -p Environment --value 2>/dev/null \
    | tr ' ' '\n' | sed -n 's/^O3_IDLE_SPLASH=//p' | tail -1)"
  splash_env="${splash_env:-1}"
  if [[ "$splash_env" == "0" || "$splash_env" == "false" || "$splash_env" == "off" ]]; then
    add warn idle-splash "script present but O3_IDLE_SPLASH=$splash_env (console-on-idle)"
  else
    add ok idle-splash "script present, O3_IDLE_SPLASH=$splash_env"
  fi
else
  add warn idle-splash "idle_splash.py missing under $REPO/poc/pi_endpoint"
fi

# 6) D-state / wedge
dlines="$(ps -eo pid=,stat=,args= 2>/dev/null | awk '
  $2 ~ /^D/ {
    cmd=$0; sub(/^[[:space:]]*[0-9]+[[:space:]]+[^ ]+[[:space:]]+/, "", cmd)
    if (cmd ~ /raw_endpoint|atomic_drm|o3_hdmi|phaseB|raw.gadget/) print
  }' || true)"
if [[ -n "${dlines// }" ]]; then
  add crit d-state "o3 process in D-state — REBOOT required (restart will not help): $(echo "$dlines" | head -1 | cut -c1-120)"
else
  add ok d-state "none"
fi

# 7) recent journal health (best-effort)
if command -v journalctl >/dev/null 2>&1; then
  jn="$(journalctl -u o3-hdmi -n 80 --no-pager -o cat 2>/dev/null || true)"
  # Intentional in-process idle splash logs "STALL: … in-process idle splash" — not a fault.
  # Only warn on real pipeline/modeset failures or unexplained stalls.
  bad_j="$(printf '%s\n' "$jn" | grep -E 'pipeline ERROR|drmModeSetCrtc \(initial modeset\) failed|initial modeset kept failing|STALL:' \
    | grep -v 'in-process idle splash' || true)"
  if [[ -n "${bad_j// }" ]]; then
    add warn journal "recent o3-hdmi log contains ERROR/modeset-fail/bad-STALL (see journalctl -u o3-hdmi -n 80)"
  else
    add ok journal "no ERROR/modeset-fail in last 80 lines (idle-hold STALL ignored)"
  fi
  if echo "$jn" | grep -Eq 'es_bytes=[1-9]|frames=[1-9]'; then
    last="$(echo "$jn" | grep -E 'es_bytes=|frames=' | tail -1 | cut -c1-160)"
    add ok stream-hint "$last"
  else
    add ok stream-hint "no live ES/fps lines yet (idle until goggles plug is OK)"
  fi
fi

# 8) last oops reboot breadcrumb
if [[ -f /var/log/o3/last-oops-reboot.txt ]]; then
  when="$(head -1 /var/log/o3/last-oops-reboot.txt 2>/dev/null || true)"
  add warn last-oops-reboot "present ($when) — review /var/log/o3/last-oops-reboot.txt"
fi

# 9) disk for logs
if [[ -d /var/log ]]; then
  avail_kb="$(df -Pk /var/log 2>/dev/null | awk 'NR==2{print $4}')"
  if [[ -n "$avail_kb" ]] && [[ "$avail_kb" -lt 102400 ]]; then
    add warn disk "/var/log has <100MB free (${avail_kb}KB)"
  else
    add ok disk "/var/log free_kb=${avail_kb:-?}"
  fi
fi

# --- output ------------------------------------------------------------------
exit_code=0
[[ "$WARN" -gt 0 ]] && exit_code=1
[[ "$CRIT" -gt 0 ]] && exit_code=2

if [[ "$MODE" == "quiet" ]]; then
  exit "$exit_code"
fi

if [[ "$MODE" == "json" ]]; then
  echo -n '{"preflight":'"$PREFLIGHT"',"crit":'"$CRIT"',"warn":'"$WARN"',"exit":'"$exit_code"',"checks":['
  first=1
  for r in "${RESULTS[@]}"; do
    level="${r%%|*}"
    rest="${r#*|}"
    name="${rest%%|*}"
    detail="${rest#*|}"
    # escape quotes in detail
    detail_esc="${detail//\\/\\\\}"
    detail_esc="${detail_esc//\"/\\\"}"
    [[ $first -eq 1 ]] || echo -n ','
    first=0
    echo -n "{\"level\":\"$level\",\"name\":\"$name\",\"detail\":\"$detail_esc\"}"
  done
  echo ']}'
  exit "$exit_code"
fi

# human
echo "=== O3 event health $(date -Is) ==="
echo "repo=$REPO  udc=$UDC  drm=$DRM_DEVICE  connector=$DRM_CONNECTOR  preflight=$PREFLIGHT"
echo
printf '%-6s %-16s %s\n' "LEVEL" "CHECK" "DETAIL"
printf '%-6s %-16s %s\n' "-----" "-----" "------"
for r in "${RESULTS[@]}"; do
  level="${r%%|*}"
  rest="${r#*|}"
  name="${rest%%|*}"
  detail="${rest#*|}"
  # color if tty
  tag="$level"
  if [[ -t 1 ]]; then
    case "$level" in
      ok)   tag=$'\033[32mok\033[0m' ;;
      warn) tag=$'\033[33mwarn\033[0m' ;;
      crit) tag=$'\033[31mcrit\033[0m' ;;
    esac
  fi
  printf '%-6s %-16s %s\n' "$tag" "$name" "$detail"
done
echo
echo "summary: crit=$CRIT warn=$WARN → exit $exit_code"
if [[ "$exit_code" -eq 0 ]]; then
  echo "verdict: GREEN — ok for booth / event (still plug goggles only when air unit live)"
elif [[ "$exit_code" -eq 1 ]]; then
  echo "verdict: YELLOW — usable with care; fix warnings before unattended show if possible"
else
  echo "verdict: RED — do not start the show; fix critical items (or reboot if d-state)"
fi
echo
echo "tips:"
echo "  journal:  journalctl -u o3-hdmi -f"
echo "  watchdog: journalctl -u o3-oops-watchdog -f"
echo "  host:     $REPO/scripts/o3_host_probe.sh"
echo "  soak:     sudo $REPO/scripts/o3_soak.sh"
echo "  restart:  sudo $REPO/scripts/o3_hdmi_safe_stop.sh --restart"
echo "            (runs host probe; refuses only on LIVE_HOST / live au_feed —"
echo "             udc=configured alone is NOT a reason to refuse)"

exit "$exit_code"
