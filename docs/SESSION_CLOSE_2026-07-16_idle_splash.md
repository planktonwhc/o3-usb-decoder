# Session close — 2026-07-16: idle “waiting for live FPV feed” splash

## TL;DR — ✅ DONE, owner-confirmed + Pi rebooted
Owner request: **no Linux console on HDMI when idle** — large **"waiting for live FPV feed"**
at boot / unplug / feed gaps, replaced by live video when a feed arrives.

**Shipped and field-confirmed this session.** Owner: “seems all good”; Pi **rebooted** with
quiet boot tokens live. Services left **active** on `o3pi`.

| Check | Result |
|---|---|
| Idle splash on HDMI-A-2 | ✅ owner eyes-on OK |
| Cold reboot → splash (not console dump) | ✅ reboot done; cmdline has `quiet logo.nologo vt.global_cursor_default=0` |
| `o3-hdmi` + `o3-oops-watchdog` | ✅ active post-reboot |
| Splash process under service | ✅ `idle_splash.py … --text waiting for live FPV feed` |
| Plug → video / unplug → splash | ✅ covered by “all good” (owner acceptance) |

## What was built
- **`poc/pi_endpoint/idle_splash.py`** — pure-Python DRM splash (XRGB8888 dumb FB + embedded
  5×7 font, auto-scaled/wrapped). Modeset once, hold until SIGTERM, DropMaster on exit.
  No GStreamer deps.
- **`HdmiPlayer` wiring** (`raw_endpoint.py`): boot / unplug / player-exit / hotplug /
  stream-restart / no-connector → splash; video `_spawn` → hide splash then atomic presenter.
- **Env (unit defaults):** `O3_IDLE_SPLASH=1`,
  `Environment="O3_IDLE_SPLASH_TEXT=waiting for live FPV feed"` (**quotes required** —
  systemd splits unquoted values on spaces).
- **`O3_IDLE_SPLASH=0`** restores console-on-idle (field kill-switch).
- **Install script** idempotently adds `quiet logo.nologo vt.global_cursor_default=0` to
  `/boot/firmware/cmdline.txt`.
- **event_health** checks for the splash script; **safe_stop** reaps `idle_splash.py`.

## Architecture
```
boot / unplug / drone-off gap
    HdmiPlayer._enter_idle_display()
        → spawn idle_splash.py (DRM master, static text)
first SPS / resumed ES
    HdmiPlayer._spawn()
        → _hide_idle_splash()  # SIGTERM → DropMaster
        → reap orphans
        → atomic_drm_present.py
```

| Env | Default | Meaning |
|---|---|---|
| `O3_IDLE_SPLASH` | `1` | master enable; `0` = old console-on-idle |
| `O3_IDLE_SPLASH_TEXT` | `waiting for live FPV feed` | on-screen string (quote in unit file) |
| `O3_IDLE_SPLASH_RETRY_S` | `2` | splash reconnect poll if monitor missing |

## Bugs found while deploying (fixed)
1. **systemd Environment spaces** — unquoted `TEXT=waiting for live…` became just `waiting`.
   Fixed with quotes in `o3-hdmi.service`.
2. **`_crtc_for` use-after-free** — `drmModeFreeEncoder` then re-read `enc.crtc_id` → garbage
   CRTC → `drmModeSetCrtc` **-ENOENT (-2)** forever. Capture fields **before** free.

## Pi left-state at close
| | |
|---|---|
| Host | `o3pi` / `192.168.1.57` — `ssh pi@192.168.1.57` |
| Services | `o3-hdmi` + `o3-oops-watchdog` **active** |
| UDC | `not attached` (goggles unplugged OK) |
| Splash | running under `o3-hdmi` |
| Boot cmdline | quiet tokens present (post-reboot) |
| Git | commit on `main` (this session) |

⚠ **Gotcha #4:** unplug goggles before any `systemctl stop/restart o3-hdmi`.

## Next session
1. **`o3_soak.sh` 5m PASS** (still open — needs cool continuous drone stream):
   ```bash
   ssh pi@192.168.1.57
   # air unit ON, goggles plugged, live HDMI good, cool drone:
   sudo ~/o3test/scripts/o3_soak.sh
   cat /var/log/o3/soak_*/summary.txt   # want RESULT: PASS
   ```
2. Backlog: dual-CRTC; re-stream; packaging; long-idle stale endpoint re-arm on real plug.

## Files touched
- `poc/pi_endpoint/idle_splash.py` **(new)**
- `poc/pi_endpoint/raw_endpoint.py` — splash lifecycle
- `poc/pi_endpoint/o3-hdmi.service` — env defaults (quoted text)
- `scripts/install_hdmi_service.sh` — quiet boot cmdline
- `scripts/o3_hdmi_safe_stop.sh` — reap splash/atomic
- `scripts/event_health.sh` — idle-splash check
- `poc/pi_endpoint/README.md`, `CLAUDE.md`, `docs/GROK_STATE.md`
