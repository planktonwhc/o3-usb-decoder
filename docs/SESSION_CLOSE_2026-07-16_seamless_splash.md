# Session close — 2026-07-16: seamless splash ↔ video (no console flash)

## TL;DR — ✅ DONE, owner-confirmed “solid on both counts”

Owner request: when live video breaks (drone power cycle) and the idle splash returns — and
when splash yields to video again (USB plug **or** drone re-power) — **never flash the Linux
text console**. First process-swap attempt failed field eyes-on; second architecture (keep DRM
master / hand off only when a frame is ready) **passed both directions**.

| Transition | Result |
|---|---|
| Live → drone power-off → splash | ✅ seamless (owner) |
| Splash → USB-C plug → live | ✅ seamless (owner) |
| Splash → drone re-power → live | ✅ seamless (owner) |
| Deploy on `o3pi` | ✅ `o3-hdmi` + splash active |

**Owner:** “yep its solid on both counts, wonderful work.”

## Why the first fix failed

v1 tried: unbind vtconsole → pre-start `idle_splash.py` → kill presenter. Process-swap still
opens a DRM gap: the presenter destroys FBs on exit; splash cold-starts (Python import) only
then. Journal also showed thrash (spawn player → immediate stream-restart kill → console).

**Rule learned:** *you cannot hand off DRM master across two processes without a gap unless the
outgoing client paints the next frame before releasing, or the incoming client only takes
master when it already has a frame ready.*

## What shipped (v2 + v3)

### A. Live → splash (in-process, keep master)

`atomic_drm_present.py`:

- On **stall** (~3s no frames, `O3_ATOMIC_STALL_SECS`), pipeline ERROR/EOS, or **SIGUSR1**:
  stop decode, paint XRGB “waiting for live FPV feed” via shared `paint_splash()`,
  **`SetCrtc` while still master**, hold until SIGTERM/SIGUSR2.
- Logs `IDLE_SPLASH showing …` for endpoint bookkeeping.
- Endpoint `notify_stream_restart` / idle: prefer **SIGUSR1** (no kill) over kill+external splash.

### B. Splash → video on drone re-power (in-process resume)

- Idle-hold listens for **SIGUSR2** → return `RC_RESUME_VIDEO` **without** `DropMaster`.
- `main()` rebuilds GStreamer pipeline; same `AtomicPresenter` keeps master + splash FB.
- First decoded frame: `present()` destroys idle FB and **SetCrtc YU12** (still master).
- Endpoint on next SPS after idle-hold: enqueue ES then `SIGUSR2` (not kill+respawn).

### C. Splash → video on USB plug (external splash held until first frame)

- `_spawn` for atomic: **do not** hide `idle_splash.py` up front; `reap` keeps splash.
- Atomic cold-starts + decodes first frame → logs **`NEED_MASTER`**.
- Endpoint `_drain_stderr` sees it → `_hide_idle_splash()` → atomic `ensure_master` (~50ms)
  + alloc + modeset. Gap is one master handoff tick, not multi-second import.

### D. Supporting

- `idle_splash.py`: tight `ensure_master` spin + stdout `READY` (boot path).
- Faster player-exit poll (0.2s while live); external splash remains boot/fallback only.

## Architecture (final)

```
LIVE (atomic, DRM master, YU12 page-flips)
  │ stall / SIGUSR1 / EOS
  ▼
IDLE-HOLD (same process, XRGB splash FB, master held)
  │ SIGUSR2 + new SPS ES
  ▼
RESUME (rebuild pipeline, SetCrtc YU12, master never dropped)

BOOT / no player:
  idle_splash.py holds master
  │ first SPS → spawn atomic (splash STAYS)
  │ atomic first frame → NEED_MASTER
  ▼
  hide splash → atomic SetMaster + modeset  (~50ms)
```

## Signals / env

| | |
|---|---|
| SIGUSR1 | enter in-process idle splash (endpoint → atomic) |
| SIGUSR2 | resume video after idle-hold (endpoint → atomic) |
| `O3_ATOMIC_STALL_SECS` | default **3** (paint splash; was 12 exit-only) |
| `O3_IDLE_SPLASH` / `TEXT` | unchanged boot/fallback external splash |

## Files

- `poc/pi_endpoint/atomic_drm_present.py` — in-process splash, NEED_MASTER, SIGUSR2 resume
- `poc/pi_endpoint/raw_endpoint.py` — SIGUSR1/2 orchestration, keep splash until NEED_MASTER
- `poc/pi_endpoint/idle_splash.py` — READY + ensure_master (boot handoff)

## Pi left-state

| | |
|---|---|
| Host | `o3pi` / `192.168.1.57` |
| Services | `o3-hdmi` + `o3-oops-watchdog` **active** (deployed this session) |
| Code | rsynced; commit on `main` (this close) |

⚠ **Gotcha #4:** unplug goggles before `systemctl stop/restart o3-hdmi`.

## Next session

1. **`o3_soak.sh` 5m PASS** (still open — cool continuous drone stream):
   ```bash
   ssh pi@192.168.1.57
   sudo ~/o3test/scripts/o3_soak.sh
   cat /var/log/o3/soak_*/summary.txt   # want RESULT: PASS
   ```
2. Backlog: dual-CRTC; re-stream; packaging; long-idle stale re-arm on real plug.

## Dead ends (do not repeat)

- Kill presenter then spawn external splash for feed→idle (always console gap).
- Hide splash before atomic spawn for splash→video (console for whole cold-start).
- Kill+respawn atomic on hot-swap resume when idle-hold already owns the plane.
