# Session close — 2026-08-31: 0.02s arm FAILED; gold PSU; plug/replug + drone off/on WORKED

## Read first (next session)
1. This file.
2. **Do not** set `O3_ARM_GAP_CAP_S=0.02` (encoder reset → USB SUSPEND). Default is **0.08**.
3. **Do not** `close(/dev/raw-gadget)` on a real USB DISCONNECT (`raw_gadget` oops → watchdog reboot, twice today). Keep the accessory bind.
4. Gold 5V PSU is required. 20V PD + 5V extension = `throttled=0x50005` / ARM 600 MHz / seconds of lag.

## TL;DR

2026-08-30 faster-arm (0.02s gap + never-ES 6s/8s re-arm) **failed eyes-on**. Rolled back. After switching to the **original gold PSU** (`throttled=0x0`, ARM **1.8 GHz**):

| Test | Result |
|---|---|
| Wait screen already up → USB plug | Picture ~**12s** (gold 4s + goggles ~3s + first pipeline ~5s). Idle-first **did** skip gi import. |
| Cable unplug / replug | **Worked** (same PID, no oops). This run was **phantom** unplug (no DISCONNECT). |
| Drone power off / on (goggles stay on Pi) | **Worked** — 4× APP_SEQ, in-process splash, HDMI back. |

Owner: plug/replug worked; drone off/on worked. First connect slower than they wanted.

## Leave-state (Pi)

| | |
|---|---|
| Host | `o3pi` / `192.168.1.57` — gold PSU, `throttled=0x0`, ARM 1.8 GHz |
| HDMI | HDMI-A-2 connected |
| Services | `o3-hdmi` + `o3-oops-watchdog` **active**. Same PID since 11:45 restart (`NRestarts=0`) |
| Probe at close | **SAFE_STOP** (phantom `udc=configured`, no current ES) |
| Unit | `O3_HDMI_PLAYER=atomic` `O3_DUAL_HDMI=1` `DRM_CONNECTOR=auto` `O3_HDMI_ASPECT=crop` `O3_IDLE_SPLASH=1` |
| Code | Deployed via `push_to_pi.sh --restart` 11:45 BST |

Restart: unplug first, then `scripts/restart_o3_hdmi.sh`. Never raw `systemctl` under LIVE_HOST.

## What shipped (on `o3pi`)

1. **`O3_ARM_GAP_CAP_S` default 0.08** again (0.02 opt-in only).
2. **Never-ES re-arm** still exists (USB first, drone later) but **20s settle**, not 6s/8s.
3. Idle-first **kept**. Helps only if HDMI is up *before* USB plug.
4. Pre-decoder queue **256 KiB** (`O3_ATOMIC_ES_QUEUE_BYTES`); presenter nice **+5** (`O3_ATOMIC_NICE`); stall splash **8s**.
5. **DISCONNECT does not close raw-gadget** — keep accessory bind. Phantom unplug still re-inits (that path succeeded 12:49).
6. No APP_SEQ for 15s after USB SUSPEND (unplug in progress, not drone hot-swap).
7. `o3_hdmi_run.sh` logs ARM clock and WARNs on live under-voltage.

## Do not repeat

- **0.02s gold cap** — 8× APP_SEQ in 70s, half-rate ES, SUSPEND.
- **Closing `/dev/raw-gadget` on DISCONNECT** — `raw_release` → `dwc2_hsotg_udc_stop` → `gadget_resume` → `raw_event_queue_add` NULL deref. Watchdog reboot. Evidence: `/var/log/o3/last-oops-reboot.txt`.
- **`soft_connect` nudge during unbind** — made the oops more likely. Removed.
- **Presenter `nice -10`** — SW decode starves USB reader under throttle.
- **4 MiB gst ES queue** — banks seconds of lag when avdec is slow.

## First-picture budget (wait screen already up)

~12s at 12:48:53→12:49:05 is **not** gi import:

| Slice | ~time |
|---|---|
| Gold APP_SEQ `gap_cap=0.080s` | ~4s |
| Goggles start USB ES | ~3s |
| First `avdec_h264` pipeline after SIGUSR2 | ~5s |

Cold boot with goggles already plugged still pays gi import (~8s) because idle-first races USB. Monitor must be up first.

## Control panel later (do not build next unless asked)

1. Boot monitor = owner-chosen image (not Linux console/rainbow).
2. Waiting/idle screen = changeable image (not “waiting for live FPV feed” text).

`docs/GROK_STATE.md`.

## Next

1. First-picture: pre-build decoder pipeline during idle-hold, or live with ~12s cold plug.
2. DISCONNECT (true USB event) reconnect — keep-bind **not** eyes-on; today’s successful replug was **phantom**.
3. Git remote. 5m soak. Public `v0.1.1`.
4. Control panel images when that work starts.

## Files

| Path | Change |
|---|---|
| `poc/pi_endpoint/raw_endpoint.py` | 0.08 gap; never-ES 20s; no close on DISCONNECT; SUSPEND mute; phantom reinit retries |
| `poc/pi_endpoint/atomic_drm_present.py` | 256 KiB ES queue; stall 8s |
| `poc/pi_endpoint/o3-hdmi.service` | comment: 0.08 rollback |
| `scripts/o3_hdmi_run.sh` | ARM clock + under-voltage WARN |
