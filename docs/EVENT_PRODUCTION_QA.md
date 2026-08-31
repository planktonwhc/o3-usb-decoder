# Event / production readiness — QA backlog

**Context (2026-07-16):** Field picture quality and latency are owner-confirmed
**excellent** — close enough to run live FPV HDMI at events. This document is the
**quality-assurance and production-hardening backlog**: what still separates a
successful bench/field unit from a box you can leave unattended next to a
projector for a full day.

Source of truth for current behavior: `README.md`, `docs/SUPPORTED_HARDWARE.md`.
Operator card: `docs/FIELD_TEST_CARD.md`.

---

## What is already “good enough” for demo / short events

| Capability | Evidence |
|---|---|
| Phone-free live path | AOA raw-gadget + gold arm + sustain |
| Smooth presentation | Atomic present-per-frame ~50 fps, owner-confirmed |
| Correct color | SW `avdec_h264` after HW chroma bug |
| Odd aspect ratios | Default crop-to-fill 16:9 (`O3_HDMI_ASPECT=crop`); `letterbox` = full FOV + bars |
| Boot-to-video | `o3-hdmi` systemd, waits for plug |
| Safe deploy workflow | `push_to_pi.sh` + `install_hdmi_service.sh` + safe_stop |
| Documented gotchas | VBUS isolate, gotcha #4, undervoltage |

If you need one clean flight + projector for an afternoon with an operator nearby,
you are largely there.

---

## Priority matrix (QA view)

### P0 — prevent black-screen disasters during a show

These are the failure modes that turn a “great feed” into a dead wall for minutes.

0. **Air-unit / battery hot-swap with goggles still on Pi** — **DONE** (owner-confirmed 2026-07-16; reconfirmed 2026-08-31).  
   - Drone power off → HDMI idle splash; drone power on → HDMI re-arms in-process.  
   - See `docs/SESSION_CLOSE_2026-07-16_hotswap_rearm.md`.

1. **Kernel oops auto-recovery (raw_gadget / dwc2 race)** — **mitigated (watchdog deployed)**  
   - Symptom: goggles blip or unlucky phantom re-arm → `raw_gadget.ko` NULL deref →
     process in **D-state** → `systemctl restart` useless → only full reboot works.  
   - **Deployed:** `o3-oops-watchdog` auto-reboots (~8s confirm). Field false-positive check pending.  
   - Optional: UDC quiescence before `os.close(fd)` to reduce race exposure.

2. **Cold-boot and first-plug reliability checklist** — **scripted (`event_health.sh`)**  
   - `scripts/event_health.sh --preflight` — service, throttle, EDID, deps, D-state.  
   - Prove GREEN on booth setup each event day.

3. **Operator-proof stop/start** — **DONE (2026-07-16 phantom UDC probe)**  
   - Wrong `systemctl stop` under a *live* host can still wedge the UDC (gotcha #4).  
   - **Shipped:** `scripts/o3_host_probe.sh` multi-signal probe; `o3_hdmi_safe_stop.sh`
     refuses only on LIVE_HOST (`au_feed`/ES), never on `udc=configured` alone.
     Wired into install + `event_health.sh`. See
     `docs/SESSION_CLOSE_2026-07-16_phantom_udc_probe.md`.  
   - Optional later: systemd `ExecStop` wrapper that always calls safe_stop.

4. **Power integrity**  
   - Undervoltage wedges the gadget. For events: dedicated 5 V supply (or PoE
     HAT with known headroom), cable strain relief, no sharing with high-draw
     loads. Log `throttled` periodically to journal.

### P1 — stay up for multi-hour soaks

5. **Multi-hour soak tests (with metrics)**  
   - 2 h / 4 h / 8 h continuous: journal `au_feed`, presenter fps, drop counters,
     CPU temp, RSS, resyncs, respawn count.  
   - Pass criteria (suggested): zero full blackouts; respawns ≤ N/hour; no RSS
     climb; no D-state; HDMI sync never needs monitor power-cycle.

6. **Reconnect / chaos tests**  
   - Scripted matrix: goggles unplug/replug (10×), air-unit off/on, Pi reboot with
     goggles already plugged (document expected behavior), HDMI cable yank,
     brief USB disconnect.  
   - Each case: time-to-video, whether reboot required.

7. **Thermal / CPU headroom under SW decode**  
   - SW decode is fine (~2× real-time headroom) but events = hot tents / sun.
     Measure temp + fps under load; optional heatsink/fan policy; throttle alert.

8. **Stall / player death recovery**  
   - Atomic already has stall watchdog + bus watch → exit → endpoint respawn.  
   - **QA ask:** prove recovery path under induced stalls (kill presenter, pause
     ES feed, corrupt NAL midstream) recovers without console stuck forever.

### P2 — show-quality polish

9. **Start-of-stream UX**  
   - Black/console during cold GStreamer import is confusing on a big screen.  
   - Optional: static “waiting for goggles / arming…” splash until first decoded
     frame; never modeset until ready (rule already exists — extend messaging).

10. **Known resolution modes**  
    - Handle 1920×1080 and 1440×1080 already. Document or probe other FOV/aspect
      modes the goggles may emit; keep fixed-canvas crop/letterbox path. Root-cause 4:3 switch if
      event organizers care about exact framing.

11. **Dual HDMI (only if the venue needs two outputs)**  
    - Parked for good reasons. Stage 2: same YU12 buffer, modeset **once** per
      CRTC, page-flip both. Do not re-enable dual without `docs/DUAL_HDMI_PARKED.md`.

12. **Network re-stream (optional product feature)**  
    - SRT/RTSP/WebRTC secondary path for stream.gg / OBS without a capture card.
      Keep HDMI path primary and isolated so re-stream bugs cannot kill the wall.

### P3 — productization / ops

13. **Image / flash reproducibility**  
    - One script or golden SD image: packages, dtoverlays (`dwc2`, raw_gadget),
      service enabled, atomic drop-in, hostname, known-good kernel note.  
    - Reflash test: blank card → working HDMI without tribal knowledge.

14. **Health endpoint / LED / OSD**  
    - Tiny HTTP `GET /health` or blink GPIO: arm state, fps, last ES age, throttled.
      Lets a second laptop monitor the box from the booth without SSH spelunking.

15. **Firmware pin + change control**  
    - Record goggles + air-unit firmware versions that were field-proven.  
    - Regression protocol after any DJI update (arm seq may drift).

16. **Default cleanup**  
    - Make `O3_HDMI_PLAYER=atomic` the unit/code default (not only a drop-in).  
    - Align `o3-hdmi.service` comments (“hardware-decode” is outdated).  
    - Remove or quarantine dead mpv defaults from “field path” docs (history stays
      in session closes).

17. **Observability pack for events**  
    - One-shot `scripts/event_health.sh`: journal slice, UDC state, DRM EDID,
      throttled, temps, last fps line.  
    - Optional continuous metrics file under `/var/log/o3/` (not `/tmp` — wiped
      on reboot).

18. **Spare / failover plan**  
    - Second SD + second Pi pre-imaged; known cable set; printed recovery card
      (this field card + “if D-state → reboot”).  
    - For critical shows: hot spare powered, monitor switcher, or capture card
      off goggles DVR as emergency (if product allows).

19. **Legal / open-source packaging**  
    - LICENSE, CONTRIBUTING, sanitized capture samples, “how to build” without
      private firmware dumps. Not required to run an event, required to ship
      community release.

20. **Security hygiene (booth network)**  
    - If the Pi is on event Wi‑Fi/LAN: change default passwords, disable unused
      services, no open SSH from the floor if avoidable, or key-only + firewall.

---

## Suggested acceptance test plan (before calling it “event-ready”)

| Test | Pass |
|---|---|
| Cold boot → plug → video | ≤ 15 s to first frame after AOA (tune as measured) |
| 2 h soak | No blackout; no manual intervention |
| 10× unplug/replug | Each returns video without reboot |
| Air unit off | Picture stops promptly |
| High-contrast chroma scene | No green bar / cyan ghosts |
| 4:3 and 16:9 sources | Full frame, correct bars |
| Induced presenter kill | Auto-respawn, video returns |
| Simulated kernel-wedge (if reproducible) | Watchdog reboots ≤ 60 s **or** documented manual reboot |
| Power: solid 5 V | `throttled=0x0` entire soak |
| Deploy from clean tree | `push` + `install_hdmi_service` alone brings unit up |

---

## Explicit non-goals (for now)

- Matching onboard DVR sharpness (~18–26 Mbit/s) on the live USB path — product
  ceiling is ~7–8 Mbit/s unless a new quality-delta capture appears.
- Dual HDMI as default field config.
- Replacing Cosmo feature-for-feature (recording matrix, multi-protocol CDN, etc.)
  before the single-port unit is unattended-reliable.

---

## Recommended next engineering slice (smallest high ROI)

1. **Oops watchdog + reboot** (P0) — ✅ **built 2026-07-16**.  
2. **`event_health.sh` + preflight** (P0/P1) — ✅ **built 2026-07-16**.  
3. **2 h soak with logged counters** (P1) — ✅ **built 2026-07-16**.  
4. Flip Python `selected_player()` default to atomic + splash “waiting…” (P2/P3).  

Items 1–3 convert “looks excellent when an engineer is nearby” into “survives a
real event day” *once field-proven on the Pi*.

---

## Built tooling (2026-07-16)

### 1) Kernel-oops auto-reboot — `o3-oops-watchdog`

| Piece | Path |
|---|---|
| Script | `scripts/o3_oops_watchdog.sh` |
| Unit | `poc/pi_endpoint/o3-oops-watchdog.service` |
| Install | via `scripts/install_hdmi_service.sh` (enabled + started with o3-hdmi) |
| Logs | journal `o3-oops-wd`; durable `/var/log/o3/oops-watchdog.log` |
| Breadcrumb | `/var/log/o3/last-oops-reboot.txt` written just before reboot |

**Detects (after ~8s confirm, ignores first 45s of boot):**
- New kernel lines matching `raw_event_queue_add` / `raw_gadget` Oops / related stacks
- o3-related process stuck in **D-state** (uninterruptible)

**Action:** `systemctl reboot` (disable with `O3_OOPS_REBOOT=0` for dry-run).

```bash
journalctl -u o3-oops-watchdog -f
# dry-run on a bench Pi:
sudo systemctl edit o3-oops-watchdog   # Environment=O3_OOPS_REBOOT=0
```

### 2) Preflight / booth health — `event_health.sh`

```bash
scripts/event_health.sh              # human report
scripts/event_health.sh --preflight  # stricter (monitor + service required)
scripts/event_health.sh --json       # machine-readable
scripts/event_health.sh --quiet      # exit code only (0 green / 1 yellow / 2 red)
```

Checks: o3-hdmi + oops-watchdog active, `throttled=0x0`, temp, raw-gadget, UDC,
DRM connector + EDID, `avdec_h264`, python-gi, player=atomic, D-state, journal
STALL/ERROR, disk free, last-oops breadcrumb.

### 3) Multi-hour soak — `o3_soak.sh`

```bash
# default 2 hours, 30s samples → /var/log/o3/soak_<stamp>/
sudo scripts/o3_soak.sh
# short trial:
sudo scripts/o3_soak.sh --duration 10m --interval 15
# require live ES for >50% of samples:
O3_SOAK_REQUIRE_STREAM=1 sudo scripts/o3_soak.sh
```

Writes `samples.csv`, `summary.txt` (PASS/FAIL), journal slice, health start/end.
**FAIL** if any D-state sample, too many service restarts, or non-zero throttled
(tunable via `O3_SOAK_*` env — see script header).

### Field deploy order

```bash
# laptop
scripts/push_to_pi.sh
# Pi (goggles UNPLUGGED)
ssh pi@192.168.1.57 'sudo ~/o3test/scripts/install_hdmi_service.sh'
ssh pi@192.168.1.57 '~/o3test/scripts/event_health.sh --preflight'
# plug goggles when air unit live, then optional:
ssh pi@192.168.1.57 'sudo ~/o3test/scripts/o3_soak.sh'   # 2h; leave running
```
