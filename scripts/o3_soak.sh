#!/usr/bin/env bash
# o3_soak.sh — soak test with CSV metrics + pass/fail summary (default one 5-min flight).
#
# Run ON THE Pi while goggles are streaming (or idle → re-plug mid-test).
# Does not stop o3-hdmi. Safe alongside the live path.
#
#   sudo scripts/o3_soak.sh                     # 5 min, 15s samples (one battery / flight)
#   sudo scripts/o3_soak.sh --duration 10m      # longer trial
#   sudo scripts/o3_soak.sh --duration 2h --interval 30 --max-respawns 20
#   sudo scripts/o3_soak.sh --duration 7200 --out /var/log/o3/soak_manual
#
# Default duration is 5m: the realistic MAX continuous stream on most DJI FPV batteries
# is ~5 min of flight, so one battery = one 5-min soak. For a multi-battery/2h soak, pass
# --duration 2h and bump --max-respawns (hot-swaps between batteries count as respawns).
#
# Writes:
#   <out>/samples.csv   per-interval metrics
#   <out>/summary.txt   pass/fail + aggregates
#   <out>/journal_slice.txt  o3-hdmi journal for the soak window
#
# Pass criteria (defaults — override with env or flag):
#   O3_SOAK_MAX_RESPAWNS     default 3 for a 5m single-flight (service NRestarts delta);
#                            a clean continuous flight should be ~0. Raise for long/hot-swap soaks.
#   O3_SOAK_MAX_DSTATE       default 0 (any D-state sample fails)
#   O3_SOAK_REQUIRE_STREAM   default 0; set 1 to require es_mbit>0 for >50% samples
#   O3_SOAK_MAX_THROTTLE     default 0 (any non-zero throttled fails)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
UDC="${UDC:-fe980000.usb}"
DURATION_RAW="5m"
INTERVAL=15
OUT=""
MAX_RESPAWNS_ARG=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --duration|-d) DURATION_RAW="$2"; shift 2 ;;
    --interval|-i) INTERVAL="$2"; shift 2 ;;
    --out|-o) OUT="$2"; shift 2 ;;
    --max-respawns) MAX_RESPAWNS_ARG="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,26p' "$0"
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

parse_duration() {
  # 2h | 10m | 30s | 7200 → seconds
  local s="$1"
  if [[ "$s" =~ ^([0-9]+)h$ ]]; then echo $((BASH_REMATCH[1] * 3600)); return; fi
  if [[ "$s" =~ ^([0-9]+)m$ ]]; then echo $((BASH_REMATCH[1] * 60)); return; fi
  if [[ "$s" =~ ^([0-9]+)s$ ]]; then echo "${BASH_REMATCH[1]}"; return; fi
  if [[ "$s" =~ ^[0-9]+$ ]]; then echo "$s"; return; fi
  echo "bad duration: $s (use 2h, 10m, 30s, or seconds)" >&2
  exit 2
}

DURATION="$(parse_duration "$DURATION_RAW")"
[[ "$INTERVAL" -ge 5 ]] || { echo "interval must be >=5s" >&2; exit 2; }

STAMP="$(date +%Y%m%d_%H%M%S)"
if [[ -z "$OUT" ]]; then
  if mkdir -p /var/log/o3 2>/dev/null; then
    OUT="/var/log/o3/soak_${STAMP}"
  else
    OUT="$REPO/logs/soak_${STAMP}"
  fi
fi
mkdir -p "$OUT"

MAX_RESPAWNS="${MAX_RESPAWNS_ARG:-${O3_SOAK_MAX_RESPAWNS:-3}}"
MAX_DSTATE="${O3_SOAK_MAX_DSTATE:-0}"
REQUIRE_STREAM="${O3_SOAK_REQUIRE_STREAM:-0}"
MAX_THROTTLE="${O3_SOAK_MAX_THROTTLE:-0}"  # 0 = only 0x0 allowed

log() { echo "[o3-soak $(date -Is)] $*"; }

nrestarts() {
  local n raw
  raw="$(systemctl show o3-hdmi -p NRestarts --value 2>/dev/null || true)"
  n="$(printf '%s\n' "$raw" | head -1 | tr -d '\r')"
  [[ "$n" =~ ^[0-9]+$ ]] || n=0
  echo "$n"
}

throttled_hex() {
  local t
  t="$(vcgencmd get_throttled 2>/dev/null | sed -n 's/.*throttled=//p' | tr -d '\r' || echo)"
  echo "${t:-?}"
}

cpu_temp() {
  vcgencmd measure_temp 2>/dev/null | sed -n 's/temp=\([0-9.]*\).*/\1/p' || echo ""
}

udc_state() {
  cat /sys/class/udc/"$UDC"/state 2>/dev/null || echo "?"
}

# RSS of endpoint + presenter (kB sum)
rss_kb() {
  ps -eo rss=,args= 2>/dev/null | awk '
    /raw_endpoint\.py|atomic_drm_present\.py/ { sum+=$1 }
    END { print sum+0 }'
}

dstate_count() {
  ps -eo stat=,args= 2>/dev/null | awk '
    $1 ~ /^D/ {
      cmd=$0; sub(/^[^ ]+[[:space:]]+/, "", cmd)
      if (cmd ~ /raw_endpoint|atomic_drm|o3_hdmi|phaseB|raw.gadget/) c++
    }
    END { print c+0 }'
}

service_active() {
  local raw
  raw="$(systemctl is-active o3-hdmi 2>/dev/null || true)"
  raw="$(printf '%s\n' "$raw" | head -1 | tr -d '\r')"
  echo "${raw:-inactive}"
}

# Parse latest useful lines from o3-hdmi journal (last ~40 lines)
parse_journal_metrics() {
  # sets: J_MBIT J_RESYNCS J_FPS J_ES_BYTES J_FRAMES (empty if missing)
  J_MBIT=""; J_RESYNCS=""; J_FPS=""; J_ES_BYTES=""; J_FRAMES=""
  local j
  j="$(journalctl -u o3-hdmi -n 40 --no-pager -o cat 2>/dev/null || true)"
  # [*] video chunks=… es_bytes=N ~X.XX Mbit/s … resyncs=N
  local line
  line="$(echo "$j" | grep -E 'es_bytes=.*Mbit/s' | tail -1 || true)"
  if [[ -n "$line" ]]; then
    J_ES_BYTES="$(echo "$line" | sed -n 's/.*es_bytes=\([0-9]*\).*/\1/p')"
    J_MBIT="$(echo "$line" | sed -n 's/.*~\([0-9.]*\) Mbit\/s.*/\1/p')"
    J_RESYNCS="$(echo "$line" | sed -n 's/.*resyncs=\([0-9]*\).*/\1/p')"
  fi
  # [atomic-drm] frames=N ~X.X fps
  line="$(echo "$j" | grep -E '\[atomic-drm\].*frames=.*fps' | tail -1 || true)"
  if [[ -n "$line" ]]; then
    J_FRAMES="$(echo "$line" | sed -n 's/.*frames=\([0-9]*\).*/\1/p')"
    J_FPS="$(echo "$line" | sed -n 's/.*~\([0-9.]*\) fps.*/\1/p')"
  fi
}

START_EPOCH="$(date +%s)"
START_ISO="$(date -Is)"
START_RESTARTS="$(nrestarts)"
JOURNAL_BOOT_CURSOR=""
if command -v journalctl >/dev/null 2>&1; then
  JOURNAL_BOOT_CURSOR="$(journalctl -u o3-hdmi -n 0 --show-cursor 2>/dev/null \
    | sed -n 's/^-- cursor: //p' || true)"
fi

CSV="$OUT/samples.csv"
echo "ts_iso,elapsed_s,service,udc,throttled,temp_c,rss_kb,dstate,nrestarts_delta,es_bytes,mbit_s,resyncs,present_frames,present_fps" >"$CSV"

log "start duration=${DURATION}s interval=${INTERVAL}s out=$OUT"
log "pass: max_respawns=$MAX_RESPAWNS max_dstate=$MAX_DSTATE require_stream=$REQUIRE_STREAM max_throttle_nonzero=$MAX_THROTTLE"
log "pre: service=$(service_active) udc=$(udc_state) throttled=$(throttled_hex) restarts=$START_RESTARTS"

# also drop a copy of event_health at start
if [[ -x "$REPO/scripts/event_health.sh" ]]; then
  "$REPO/scripts/event_health.sh" >"$OUT/health_start.txt" 2>&1 || true
fi

samples=0
dstate_hits=0
throttle_hits=0
stream_hits=0
service_down_hits=0
max_rss=0
max_temp=""
min_fps=""
max_fps=""
sum_fps=0
fps_n=0
sum_mbit=0
mbit_n=0

END_EPOCH=$((START_EPOCH + DURATION))

cleanup() {
  local ec=$?
  log "sampling stopped (exit=$ec); writing summary…"
  # journal slice
  if [[ -n "$JOURNAL_BOOT_CURSOR" ]]; then
    journalctl -u o3-hdmi --after-cursor="$JOURNAL_BOOT_CURSOR" --no-pager \
      >"$OUT/journal_slice.txt" 2>/dev/null || true
  else
    journalctl -u o3-hdmi --since="$START_ISO" --no-pager \
      >"$OUT/journal_slice.txt" 2>/dev/null || true
  fi
  if [[ -x "$REPO/scripts/event_health.sh" ]]; then
    "$REPO/scripts/event_health.sh" >"$OUT/health_end.txt" 2>&1 || true
  fi

  local end_restarts delta_restarts
  end_restarts="$(nrestarts)"
  delta_restarts=$((end_restarts - START_RESTARTS))
  [[ "$delta_restarts" -lt 0 ]] && delta_restarts=0

  local avg_fps="" avg_mbit=""
  if [[ "$fps_n" -gt 0 ]]; then
    avg_fps="$(awk "BEGIN{printf \"%.2f\", $sum_fps/$fps_n}")"
  fi
  if [[ "$mbit_n" -gt 0 ]]; then
    avg_mbit="$(awk "BEGIN{printf \"%.3f\", $sum_mbit/$mbit_n}")"
  fi

  local stream_pct=0
  if [[ "$samples" -gt 0 ]]; then
    stream_pct="$(awk "BEGIN{printf \"%.1f\", 100*$stream_hits/$samples}")"
  fi

  local pass=1
  local reasons=()

  if [[ "$dstate_hits" -gt "$MAX_DSTATE" ]]; then
    pass=0
    reasons+=("dstate_hits=$dstate_hits > max=$MAX_DSTATE (kernel wedge — reboot path)")
  fi
  if [[ "$delta_restarts" -gt "$MAX_RESPAWNS" ]]; then
    pass=0
    reasons+=("service_restarts_delta=$delta_restarts > max=$MAX_RESPAWNS")
  fi
  if [[ "$throttle_hits" -gt "$MAX_THROTTLE" ]]; then
    pass=0
    reasons+=("throttle_nonzero_samples=$throttle_hits (PSU/thermal)")
  fi
  if [[ "$REQUIRE_STREAM" == "1" && "$samples" -gt 0 ]]; then
    # require >50% samples with mbit>0
    if awk "BEGIN{exit !($stream_hits*2 < $samples)}"; then
      pass=0
      reasons+=("stream only ${stream_pct}% of samples (REQUIRE_STREAM=1)")
    fi
  fi
  if [[ "$service_down_hits" -gt 0 && "$dstate_hits" -eq 0 ]]; then
    # soft fail if service dropped without d-state (still interesting)
    reasons+=("note: service_inactive_samples=$service_down_hits")
  fi

  {
    echo "O3 soak summary"
    echo "==============="
    echo "start:    $START_ISO"
    echo "end:      $(date -Is)"
    echo "duration: ${DURATION}s (requested $DURATION_RAW)"
    echo "interval: ${INTERVAL}s"
    echo "samples:  $samples"
    echo "out:      $OUT"
    echo
    echo "service_restarts_delta: $delta_restarts (start=$START_RESTARTS end=$end_restarts)"
    echo "dstate_hits:            $dstate_hits"
    echo "throttle_nonzero:       $throttle_hits"
    echo "service_inactive:       $service_down_hits"
    echo "stream_samples:         $stream_hits (${stream_pct}%)"
    echo "rss_kb_max:             $max_rss"
    echo "temp_c_max:             ${max_temp:-n/a}"
    echo "present_fps min/avg/max: ${min_fps:-n/a} / ${avg_fps:-n/a} / ${max_fps:-n/a}"
    echo "es_mbit  avg:            ${avg_mbit:-n/a}"
    echo
    if [[ "$pass" -eq 1 ]]; then
      echo "RESULT: PASS"
    else
      echo "RESULT: FAIL"
    fi
    if [[ "${#reasons[@]}" -gt 0 ]]; then
      echo "notes:"
      for r in "${reasons[@]}"; do
        echo "  - $r"
      done
    fi
    echo
    echo "files:"
    echo "  $CSV"
    echo "  $OUT/journal_slice.txt"
    echo "  $OUT/health_start.txt"
    echo "  $OUT/health_end.txt"
  } | tee "$OUT/summary.txt"

  log "done → $OUT/summary.txt"
  # exit 0 on pass, 1 on fail (for CI/automation)
  if [[ "$pass" -eq 1 ]]; then
    exit 0
  else
    exit 1
  fi
}

trap cleanup EXIT INT TERM

while true; do
  now="$(date +%s)"
  elapsed=$((now - START_EPOCH))
  if [[ "$elapsed" -ge "$DURATION" ]]; then
    break
  fi

  parse_journal_metrics
  thr="$(throttled_hex)"
  temp="$(cpu_temp)"
  rss="$(rss_kb)"
  ds="$(dstate_count)"
  svc="$(service_active)"
  nr_now="$(nrestarts)"
  nr_delta=$((nr_now - START_RESTARTS))
  [[ "$nr_delta" -lt 0 ]] && nr_delta=0
  udc="$(udc_state)"

  samples=$((samples + 1))
  [[ "$ds" -gt 0 ]] && dstate_hits=$((dstate_hits + 1))
  [[ "$svc" != "active" ]] && service_down_hits=$((service_down_hits + 1))
  if [[ "$thr" != "0x0" && "$thr" != "?" && -n "$thr" ]]; then
    throttle_hits=$((throttle_hits + 1))
  fi
  if [[ -n "$J_MBIT" ]] && awk "BEGIN{exit !($J_MBIT > 0)}"; then
    stream_hits=$((stream_hits + 1))
    sum_mbit="$(awk "BEGIN{print $sum_mbit + $J_MBIT}")"
    mbit_n=$((mbit_n + 1))
  fi
  if [[ -n "$J_FPS" ]]; then
    sum_fps="$(awk "BEGIN{print $sum_fps + $J_FPS}")"
    fps_n=$((fps_n + 1))
    if [[ -z "$min_fps" ]] || awk "BEGIN{exit !($J_FPS < $min_fps)}"; then min_fps="$J_FPS"; fi
    if [[ -z "$max_fps" ]] || awk "BEGIN{exit !($J_FPS > $max_fps)}"; then max_fps="$J_FPS"; fi
  fi
  if [[ "$rss" -gt "$max_rss" ]]; then max_rss="$rss"; fi
  if [[ -n "$temp" ]]; then
    if [[ -z "$max_temp" ]] || awk "BEGIN{exit !($temp > $max_temp)}"; then max_temp="$temp"; fi
  fi

  ts="$(date -Is)"
  echo "$ts,$elapsed,$svc,$udc,$thr,${temp},$rss,$ds,$nr_delta,${J_ES_BYTES},${J_MBIT},${J_RESYNCS},${J_FRAMES},${J_FPS}" >>"$CSV"

  # progress line every sample (compact)
  log "t=${elapsed}s svc=$svc udc=$udc thr=$thr temp=${temp:-?} rss=${rss}kB dstate=$ds restartsΔ=$nr_delta mbit=${J_MBIT:--} fps=${J_FPS:--} resyncs=${J_RESYNCS:--}"

  # Early abort on D-state: still write summary via trap
  if [[ "$ds" -gt "$MAX_DSTATE" ]]; then
    log "FAIL early: d-state detected — kernel likely wedged; reboot required"
    # leave trap to summarize
    exit 1
  fi

  # sleep but don't overshoot end much
  now2="$(date +%s)"
  remain=$((END_EPOCH - now2))
  if [[ "$remain" -le 0 ]]; then
    break
  fi
  sl="$INTERVAL"
  [[ "$remain" -lt "$sl" ]] && sl="$remain"
  sleep "$sl"
done

# normal completion → trap cleanup
exit 0
