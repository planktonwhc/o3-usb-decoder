# Session close — 2026-07-16 phantom UDC multi-signal host probe

## Result

**✅ Ops bug fixed and deployed.** Agents/operators must never refuse `o3-hdmi`
stop/restart solely because `/sys/class/udc/fe980000.usb/state == configured`.

That state is a known **phantom** after unplug (no `USB_RAW_EVENT_DISCONNECT`).
Owner field case earlier same day: goggles **not** attached, agent blocked restart
citing gotcha #4. Wrong gate.

**Owner decision (end of session):** leave as **ops-only** — no new background
auto-restart. Runtime phantom recovery was already in-process (host-liveness
watchdog → re-arm). This session only fixed the **manual/deploy restart path**.

## What shipped

| Piece | Path | Role |
|---|---|---|
| Host probe | `scripts/o3_host_probe.sh` | Multi-signal verdict: SAFE_STOP / LIVE_HOST / UNCERTAIN |
| Safe stop | `scripts/o3_hdmi_safe_stop.sh` | Runs probe; refuses only LIVE_HOST unless `--force` |
| Canonical restart | `scripts/restart_o3_hdmi.sh` | One command laptop **or** Pi (SSH or local) |
| Push + restart | `scripts/push_to_pi.sh --restart` | rsync then safe restart (IP fallback if mDNS flaky) |
| Install | `scripts/install_hdmi_service.sh` | Pre-start probe; restart via safe_stop |
| Health | `scripts/event_health.sh` | `host-probe` check; idle-hold STALL not a warn |

## Auto vs manual (do not confuse)

| Situation | Automatic? | How |
|---|---|---|
| Phantom unplug while `o3-hdmi` healthy | **Yes** | In-process host-liveness WD → `signal_phantom_unplug` → gadget reinit / re-arm |
| Air-unit / battery hot-swap | **Yes** | Video-resume re-arm (prior session) |
| Deploy / pick up new code | **No** | `scripts/push_to_pi.sh --restart` or `restart_o3_hdmi.sh` |
| Agent needs clean service restart | **No** | Same — never raw `systemctl restart` gated on UDC alone |

## Probe signals (never UDC alone)

**LIVE_HOST (exit 1)** — gotcha #4 applies:
- **Most recent** journal evidence is live (`au_feed` ≥ 0.5, ES, bulk, presenter fps)
- Not superseded by a later phantom/idle marker

**SAFE_STOP (exit 0)** — OK to safe_stop/restart:
- `udc=not attached` / `default`
- Endpoint `still waiting … configured=False` + idle ES
- Idle splash with no *current* feed
- **`udc=configured` with no current ES/au_feed** → phantom/idle
- **Chronological fix (field):** older `au_feed=50` then `phantom unplug` → SAFE_STOP
  (stale feed must not false-LIVE; hit while finishing the restart path this session)

**UNCERTAIN (exit 2)** — still proceed with safe_stop + warning; never hard-refuse on UDC.

### Not a signal on AOA path
`lsusb` does **not** list the goggles (they are the USB *host*; Pi is the gadget).

## Operator / agent commands

```bash
# Canonical (laptop or Pi):
scripts/restart_o3_hdmi.sh
scripts/push_to_pi.sh --restart          # code push + restart
scripts/restart_o3_hdmi.sh --probe-only
scripts/restart_o3_hdmi.sh --status
scripts/restart_o3_hdmi.sh --force       # owner confirms unplugged

# On Pi only:
~/o3test/scripts/o3_host_probe.sh
sudo ~/o3test/scripts/o3_hdmi_safe_stop.sh --restart
```

Prefer `PI=pi@192.168.1.57` when mDNS (`o3pi.local`) fails.

## Dual default + clean restart

Unit ships `O3_DUAL_HDMI=1`, `DRM_CONNECTOR=auto`, `O3_HDMI_PLAYER=atomic`.
Clean restart via the new path this session — dual idle splash on both ports confirmed.

## Pi leave-state (end of session)

| Item | Value |
|---|---|
| Host | `o3pi` / `192.168.1.57` |
| `o3-hdmi` | **active** |
| `o3-oops-watchdog` | **active** |
| UDC | **`not attached`** (goggles unplugged / idle wait) |
| Dual | splash on HDMI-A-1 + HDMI-A-2; env dual=1 / connector=auto / atomic |
| Journal | `still waiting for goggles USB video … configured=False` |

Leave services running. Do not stop without the safe path if a host is live.

## Git note

Changes are on local `main` **uncommitted** (includes this session’s scripts/docs
plus earlier same-day dual-atomic tree edits). Next session may commit if owner wants.

## Still open (next session)

1. **#1 `o3_soak.sh` 5m PASS** — cool drone continuous stream → `RESULT: PASS`
2. Dual soak / dual hot-swap eyes-on under unit default
3. Public export after monorepo fixes: `scripts/export_public_tree.sh` → `dr00min/o3-usb-decoder`
4. Backlog: re-stream; arming trim; SD image; COLOR_ENCODING after DRM master

## Dead ends avoided

- Gating on UDC sysfs alone
- Treating lsusb as AOA host presence
- Raw `systemctl restart o3-hdmi` as the documented path
- New auto-restart daemon (owner: leave as is)
