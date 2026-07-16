#!/usr/bin/env bash
#
# Phase 0 recon: identify the DJI Goggles 2 RNDIS USB-network interface, bring up the
# link, confirm the goggles' IP, and enumerate open ports on the USB link.
#
# Prereqs: goggles powered on, linked to the air unit, showing video, plugged into this
# host over USB. Run as root (needed for `ip addr` config and `nmap -p-` SYN scan).
#
# Usage: sudo ./scripts/phase0_recon.sh [goggles_ip]
#   If goggles_ip is omitted, the script tries to discover it via DHCP lease / ARP on
#   whatever interface looks like the RNDIS device, falling back to the historical
#   192.168.42.2 default.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "This script needs root (interface config + nmap -p-). Re-run with sudo." >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${REPO_ROOT}/captures/recon"
mkdir -p "$LOG_DIR"
TS="$(date +%Y%m%d_%H%M%S)"
LOG_FILE="${LOG_DIR}/${TS}_phase0_recon.log"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

log "=== Phase 0 recon starting ==="

log "--- dmesg | grep -i rndis (last 50 matching lines) ---"
dmesg | grep -i rndis | tail -n 50 | tee -a "$LOG_FILE" || log "(no rndis lines in dmesg — is the device plugged in / powered / linked?)"

log "--- ip addr (full) ---"
ip addr | tee -a "$LOG_FILE"

# Try to find the RNDIS interface. It usually shows up as usbN or enxMACADDR. A host can
# have multiple usb*/enx* interfaces (stale ones from other devices) — prefer one that's
# actually link-up (LOWER_UP) over a dead/NO-CARRIER leftover.
CANDIDATES="$(ip -o link show | awk -F': ' '{print $2}' | grep -E '^(usb|enx)' || true)"

IFACE="$(ip -o link show | grep -E '^\S+: (usb|enx)\S+.*LOWER_UP' | awk -F': ' '{print $2}' | head -n1 || true)"
if [[ -z "$IFACE" ]]; then
  IFACE="$(echo "$CANDIDATES" | head -n1)"
fi

if [[ -z "$IFACE" ]]; then
  log "WARNING: could not auto-detect a usb*/enx* interface. Listing all interfaces above —"
  log "set IFACE manually and re-run, or extend this script's detection heuristic."
  exit 1
fi
if [[ "$(echo "$CANDIDATES" | wc -l)" -gt 1 ]]; then
  log "Multiple usb*/enx* interfaces present: $(echo "$CANDIDATES" | tr '\n' ' ')"
fi
log "Detected candidate RNDIS interface: ${IFACE}"

log "--- bringing up ${IFACE} ---"
ip link set "$IFACE" up
sleep 1
ip addr show "$IFACE" | tee -a "$LOG_FILE"

# Try DHCP first (goggles historically act as DHCP server on 192.168.42.x).
if command -v dhclient >/dev/null 2>&1; then
  log "Attempting dhclient on ${IFACE} (5s timeout)..."
  timeout 5 dhclient -v "$IFACE" 2>&1 | tee -a "$LOG_FILE" || log "dhclient did not complete in time / failed — will check for existing/static address"
fi

HOST_IP="$(ip -4 -o addr show "$IFACE" | awk '{print $4}' | cut -d/ -f1 | head -n1 || true)"

if [[ -z "$HOST_IP" ]]; then
  log "No DHCP address obtained. Falling back to historical static config: host=192.168.42.3/24"
  ip addr add 192.168.42.3/24 dev "$IFACE" || log "(address may already be assigned)"
  HOST_IP="192.168.42.3"
fi
log "Host IP on ${IFACE}: ${HOST_IP}"

GOGGLES_IP="${1:-}"
if [[ -z "$GOGGLES_IP" ]]; then
  SUBNET_PREFIX="$(echo "$HOST_IP" | cut -d. -f1-3)"
  CANDIDATE="${SUBNET_PREFIX}.2"
  log "No goggles IP given — trying historical default ${CANDIDATE}"
  if ping -c1 -W2 "$CANDIDATE" >/dev/null 2>&1; then
    GOGGLES_IP="$CANDIDATE"
  elif command -v nmap >/dev/null 2>&1; then
    # NOTE: on real hardware the goggles have not always responded to ICMP even when up
    # (confirmed 2026-07-03) — an ARP-based sweep is more reliable than ping here.
    log "Default candidate ${CANDIDATE} did not respond to ping. ARP-sweeping ${SUBNET_PREFIX}.0/24 on ${IFACE}..."
    GOGGLES_IP="$(nmap -sn -e "$IFACE" "${SUBNET_PREFIX}.0/24" -oG - 2>/dev/null \
      | awk -v host="$HOST_IP" '/Up$/{print $2}' | grep -v "^${HOST_IP}$" | head -n1 || true)"
  fi
fi

if [[ -z "$GOGGLES_IP" ]]; then
  log "ERROR: could not determine goggles IP automatically. Re-run with the IP as an argument,"
  log "e.g. sudo ./scripts/phase0_recon.sh 192.168.42.2"
  exit 1
fi
log "Using goggles IP: ${GOGGLES_IP}"

log "--- ping check ---"
ping -c3 "$GOGGLES_IP" | tee -a "$LOG_FILE" || log "(no ping response — continuing to nmap anyway, some devices block ICMP)"

NMAP_OUT="${LOG_DIR}/${TS}_nmap_${GOGGLES_IP}.txt"
log "--- nmap -p- ${GOGGLES_IP} (full TCP port scan, this can take a few minutes) ---"
nmap -p- -T4 -Pn -e "$IFACE" "$GOGGLES_IP" -oN "$NMAP_OUT" | tee -a "$LOG_FILE"
log "Full nmap output saved to ${NMAP_OUT}"

log "--- also checking common ports over UDP (video/control may be UDP) ---"
UDP_OUT="${LOG_DIR}/${TS}_nmap_udp_${GOGGLES_IP}.txt"
nmap -sU -T4 -Pn -e "$IFACE" --top-ports 20 "$GOGGLES_IP" -oN "$UDP_OUT" | tee -a "$LOG_FILE" || log "(UDP scan needs root / may be slow — see ${UDP_OUT})"

log "=== Phase 0 recon complete. Log: ${LOG_FILE} ==="
log "Next: cross-check open ports above against the known wifi-scan list (21, 1415, 1561,"
log "8901-8904) in CLAUDE.md, then move to Phase 1 (capture DJI Fly's working session)."
