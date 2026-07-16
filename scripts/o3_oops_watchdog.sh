#!/usr/bin/env bash
# o3_oops_watchdog.sh — detect raw_gadget / dwc2 kernel wedge and auto-reboot.
#
# Field bug (2026-07-15): phantom-unplug re-arm can race goggles reconnect traffic
# and NULL-deref inside raw_gadget.ko (raw_event_queue_add / gadget_resume). The
# process wedges in D-state; systemctl restart does NOT recover — only a full
# reboot does. This watchdog makes "reboot always fixes it" hands-off for events.
#
# Triggers (any, after confirm window):
#   1) NEW kernel log lines matching the known oops signatures
#   2) An o3-related process stuck in uninterruptible sleep (D / D+)
#
# Usage (normally via systemd o3-oops-watchdog.service):
#   sudo scripts/o3_oops_watchdog.sh
#   O3_OOPS_REBOOT=0 sudo scripts/o3_oops_watchdog.sh   # detect + log only
#
# Env:
#   O3_OOPS_REBOOT=1|0        actually reboot (default 1)
#   O3_OOPS_POLL_SECS=5       poll interval
#   O3_OOPS_CONFIRM_SECS=8    require signal to persist this long before reboot
#   O3_OOPS_MIN_UPTIME_SECS=45  ignore detections this long after boot (settle)
#   O3_OOPS_LOG_DIR=/var/log/o3
#   O3_REPO=/home/pi/o3test   (unused; for install path rewrite consistency)
set -euo pipefail

POLL="${O3_OOPS_POLL_SECS:-5}"
CONFIRM="${O3_OOPS_CONFIRM_SECS:-8}"
MIN_UPTIME="${O3_OOPS_MIN_UPTIME_SECS:-45}"
REBOOT_OK="${O3_OOPS_REBOOT:-1}"
LOG_DIR="${O3_OOPS_LOG_DIR:-/var/log/o3}"
CURSOR_FILE="${O3_OOPS_CURSOR:-/run/o3-oops-wd.cursor}"
STATE_FILE="${O3_OOPS_STATE:-/run/o3-oops-wd.state}"

# Kernel oops / stack signatures from the 2026-07-15 field incident + close variants.
# Keep tight — broad "Oops" alone would false-positive on unrelated kernel bugs.
KMSG_PAT='raw_event_queue_add|raw_gadget.*(Oops|BUG|NULL pointer|unable to handle)|Oops:.*raw_gadget|BUG:.*raw_gadget|dwc2_hsotg_udc_stop.*Oops|Call Trace:.*raw_gadget|in raw_event_queue_add|in gadget_resume'

log() {
  # systemd captures stdout; also append a durable file when possible.
  local line="[o3-oops-wd $(date -Is)] $*"
  echo "$line"
  if [[ -d "$LOG_DIR" ]] || mkdir -p "$LOG_DIR" 2>/dev/null; then
    echo "$line" >>"$LOG_DIR/oops-watchdog.log" 2>/dev/null || true
  fi
}

uptime_secs() {
  local u
  u="$(cut -d. -f1 /proc/uptime 2>/dev/null || echo 99999)"
  echo "${u:-99999}"
}

# --- kernel log: only NEW messages after we start (watermark / cursor) -------
# Prefer dmesg (fast, local ring). Everything external is hard-timeout'd so a
# wedged journald / stuck dmesg can never block the reboot path.
run_to() {
  # run_to SECS cmd args...
  local secs="$1"; shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "$secs" "$@" 2>/dev/null || true
  else
    "$@" 2>/dev/null || true
  fi
}

dmesg_out() {
  run_to 2 dmesg
}

jctl() {
  run_to 3 journalctl "$@"
}

init_kmsg_cursor() {
  # dmesg watermark (primary)
  DMESG_MARK="$(dmesg_out | wc -l | tr -d ' ')"
  DMESG_MARK="${DMESG_MARK:-0}"
  # journal cursor (secondary) — seed so historical lines do not trip us
  if command -v journalctl >/dev/null 2>&1; then
    jctl -k -n 0 --cursor-file="$CURSOR_FILE" >/dev/null || true
    if [[ ! -s "$CURSOR_FILE" ]]; then
      jctl -k -n 1 --show-cursor -o cat \
        | sed -n 's/^-- cursor: //p' >"$CURSOR_FILE" || true
    fi
  fi
}

check_kmsg_hit() {
  local hit="" new_j new_d n
  # Primary: dmesg ring growth since watermark
  n="$(dmesg_out | wc -l | tr -d ' ')"
  n="${n:-0}"
  if [[ "$n" -gt "${DMESG_MARK:-0}" ]]; then
    new_d="$(dmesg_out | tail -n +"$((DMESG_MARK + 1))" || true)"
    DMESG_MARK="$n"
    if [[ -n "$new_d" ]] && echo "$new_d" | grep -Eiq -- "$KMSG_PAT"; then
      hit="$(echo "$new_d" | grep -Ei -- "$KMSG_PAT" | tail -3 | tr '\n' ' ' | cut -c1-240)"
      echo "dmesg:$hit"
      return 0
    fi
  else
    # ring may have wrapped (n < mark) — resync watermark, still scan last 80 lines
    if [[ "$n" -lt "${DMESG_MARK:-0}" ]]; then
      DMESG_MARK="$n"
      new_d="$(dmesg_out | tail -80 || true)"
      if [[ -n "$new_d" ]] && echo "$new_d" | grep -Eiq -- "$KMSG_PAT"; then
        hit="$(echo "$new_d" | grep -Ei -- "$KMSG_PAT" | tail -3 | tr '\n' ' ' | cut -c1-240)"
        echo "dmesg-wrap:$hit"
        return 0
      fi
    else
      DMESG_MARK="$n"
    fi
  fi
  # Secondary: journal since cursor
  if command -v journalctl >/dev/null 2>&1 && [[ -f "$CURSOR_FILE" ]]; then
    new_j="$(jctl -k --cursor-file="$CURSOR_FILE" -o cat --no-pager || true)"
    if [[ -n "$new_j" ]] && echo "$new_j" | grep -Eiq -- "$KMSG_PAT"; then
      hit="$(echo "$new_j" | grep -Ei -- "$KMSG_PAT" | tail -3 | tr '\n' ' ' | cut -c1-240)"
      echo "kmsg:$hit"
      return 0
    fi
  fi
  return 1
}

# --- D-state o3 processes ----------------------------------------------------
# STAT can be D, Ds, D+ (uninterruptible). Match first char D only.
dstate_o3_procs() {
  # pid stat cmd — only o3 / raw-gadget related
  ps -eo pid=,stat=,args= 2>/dev/null | awk '
    $2 ~ /^D/ {
      line=$0
      # strip pid+stat for match on args
      cmd=line; sub(/^[[:space:]]*[0-9]+[[:space:]]+[^ ]+[[:space:]]+/, "", cmd)
      if (cmd ~ /raw_endpoint\.py|atomic_drm_present\.py|dual_drm_mirror\.py|o3_hdmi_run\.sh|phaseB_standalone|raw-gadget|raw_gadget/) {
        print $1, $2, cmd
      }
    }'
}

check_dstate_hit() {
  local lines
  lines="$(dstate_o3_procs || true)"
  if [[ -n "${lines// }" ]]; then
    echo "dstate:$(echo "$lines" | head -3 | tr '\n' ';' | cut -c1-240)"
    return 0
  fi
  return 1
}

# --- reboot path -------------------------------------------------------------
do_reboot() {
  local reason="$1"
  log "WEDGE CONFIRMED — $reason"
  log "system will reboot in 3s (O3_OOPS_REBOOT=$REBOOT_OK)"
  # Durable breadcrumb across reboot (survives if /var is mounted)
  if mkdir -p "$LOG_DIR" 2>/dev/null; then
    {
      echo "ts=$(date -Is)"
      echo "reason=$reason"
      echo "uptime=$(uptime_secs)"
      echo "udc=$(cat /sys/class/udc/*/state 2>/dev/null | tr '\n' ' ')"
      echo "throttled=$(vcgencmd get_throttled 2>/dev/null || echo n/a)"
      dstate_o3_procs || true
      echo "--- last kernel ---"
      dmesg 2>/dev/null | tail -40 || true
    } >"$LOG_DIR/last-oops-reboot.txt" 2>/dev/null || true
  fi
  sync || true
  if [[ "$REBOOT_OK" == "1" || "$REBOOT_OK" == "yes" || "$REBOOT_OK" == "true" ]]; then
    # The system is WEDGED by definition here (raw_gadget oops + a D-state task holding
    # the UDC), so a GRACEFUL reboot is exactly what will NOT work: systemd blocks trying
    # to stop the un-killable o3-hdmi unit. Field 2026-07-16: `systemctl --no-block reboot`
    # returned success immediately (queued a graceful job), then that job hung ~90s behind
    # the D-state task until systemd's own job-timeout finally forced it — far slower than
    # the ~15s this watchdog promises. So FORCE the reboot without waiting for unit
    # shutdown (`reboot -ff` / `reboot -f` both call reboot(2) directly), and arm a
    # kernel-level sysrq last-resort in the background in case even those are blocked.
    log "forcing reboot (system wedged — not waiting for unit shutdown)"
    ( sleep 10
      echo 1 >/proc/sys/kernel/sysrq 2>/dev/null || true
      echo b >/proc/sysrq-trigger 2>/dev/null || true ) &
    if command -v systemctl >/dev/null 2>&1; then
      systemctl reboot -ff 2>/dev/null || reboot -f 2>/dev/null || /sbin/reboot -f
    else
      reboot -f 2>/dev/null || /sbin/reboot -f
    fi
  else
    log "DRY-RUN: reboot suppressed (set O3_OOPS_REBOOT=1 to enable)"
  fi
}

# --- main loop ---------------------------------------------------------------
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  sed -n '2,35p' "$0"
  exit 0
fi

log "start poll=${POLL}s confirm=${CONFIRM}s min_uptime=${MIN_UPTIME}s reboot=${REBOOT_OK}"
init_kmsg_cursor
log "kmsg cursor ready (dmesg_mark=${DMESG_MARK:-0})"

# pending confirmation state: first_seen_epoch + reason
PENDING_SINCE=0
PENDING_REASON=""

while true; do
  sleep "$POLL"

  up="$(uptime_secs)"
  if [[ "$up" -lt "$MIN_UPTIME" ]]; then
    continue
  fi

  reason=""
  if r="$(check_kmsg_hit)"; then
    reason="$r"
  elif r="$(check_dstate_hit)"; then
    reason="$r"
  fi

  now="$(date +%s)"
  if [[ -n "$reason" ]]; then
    if [[ "$PENDING_SINCE" -eq 0 ]]; then
      PENDING_SINCE="$now"
      PENDING_REASON="$reason"
      log "signal seen (confirming ${CONFIRM}s): $reason"
      echo "since=$PENDING_SINCE reason=$PENDING_REASON" >"$STATE_FILE" 2>/dev/null || true
    else
      # still pending — update reason text, keep first_seen
      PENDING_REASON="$reason"
      elapsed=$((now - PENDING_SINCE))
      if [[ "$elapsed" -ge "$CONFIRM" ]]; then
        do_reboot "confirmed ${elapsed}s: $PENDING_REASON"
        # If dry-run, reset so we do not spam every poll forever at full rate;
        # re-arm after a cool-down equal to confirm window.
        PENDING_SINCE=0
        PENDING_REASON=""
        rm -f "$STATE_FILE" 2>/dev/null || true
        sleep "$CONFIRM"
      fi
    fi
  else
    if [[ "$PENDING_SINCE" -ne 0 ]]; then
      log "signal cleared before confirm (was: $PENDING_REASON)"
    fi
    PENDING_SINCE=0
    PENDING_REASON=""
    rm -f "$STATE_FILE" 2>/dev/null || true
  fi
done
