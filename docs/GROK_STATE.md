# Grok session handoff — DJI O3 USB decoder

> **Audience:** Grok sessions only.  
> **Source of truth:** `CLAUDE.md`, `README.md`, session closes.  
> **Rules:** capture-derived bytes only; `captures/` append-only; flight-safety SPECIAL only with owner sign-off.

**Last updated:** 2026-07-16 evening — seamless splash ↔ video **DONE, owner “solid on both counts”**.  
**Handoff:** `docs/SESSION_CLOSE_2026-07-16_seamless_splash.md` ← **read first**

---

## Mission
Goggles 2 + O3 → Pi 4 HDMI, no phone / DJI Fly / Cosmo. Event-ready live FPV wall.

---

## Status

| Layer | Status |
|---|---|
| Live picture (steady flight) | ✅ atomic + SW decode; owner “excellent” |
| Air-unit hot-swap (goggles stay on Pi) | ✅ SOLVED (owner-confirmed 2×) |
| Event ops (watchdog / health) | ✅ built + verified (oops force-reboot deployed) |
| Idle “waiting for live FPV feed” splash | ✅ DONE (owner “all good” + reboot) |
| Seamless splash ↔ video (no console flash) | ✅ **DONE** (live↔off, plug, re-power) |
| 5m soak `RESULT: PASS` | ⏳ needs cool drone continuous stream |
| Dual HDMI | ⏸ PARKED |
| Git | ✅ `main` (this session commit) |

### Locked rules
Never leak compressed ES; modeset once then page-flip; letterbox any aspect; unplug goggles before service stop; VBUS isolate; solid 5 V; **never drop DRM master for idle without painting splash first** (in-process idle-hold); **never hide external splash before atomic has a frame** (`NEED_MASTER` handoff); SIGUSR2 resume after idle-hold (do not kill+respawn for drone re-power).

---

## Next session (strict order)

1. **5m soak** with cool drone: `ssh pi@192.168.1.57` → live HDMI → `sudo ~/o3test/scripts/o3_soak.sh` → `RESULT: PASS`.  
2. Optional backlog: dual / re-stream / packaging / long-idle stale re-arm.

---

## Pi
| | |
|---|---|
| Host | `o3pi` / `192.168.1.57` — `ssh pi@192.168.1.57` |
| Services | `o3-hdmi` + `o3-oops-watchdog` **active** — leave running |
| Idle UI | boot: `idle_splash.py`; live gaps: atomic in-process splash; kill-switch `O3_IDLE_SPLASH=0` |
| Deploy | `scripts/push_to_pi.sh` then unplug goggles → `sudo ~/o3test/scripts/o3_hdmi_safe_stop.sh --restart` |

---

## Key paths
| Role | Path |
|---|---|
| Endpoint | `poc/pi_endpoint/raw_endpoint.py` |
| Atomic presenter + in-process splash | `poc/pi_endpoint/atomic_drm_present.py` |
| Boot external splash | `poc/pi_endpoint/idle_splash.py` |
| This session close | `docs/SESSION_CLOSE_2026-07-16_seamless_splash.md` |
