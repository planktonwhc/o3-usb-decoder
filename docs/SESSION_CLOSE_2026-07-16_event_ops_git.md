# Session close — 2026-07-16 (event ops + deploy + git; air-unit hot-swap FAIL filed)

## Read first (next session)
1. **This file** (esp. “#1 Air-unit hot-swap” + “Verify the 3 event tools”)
2. `CLAUDE.md` CURRENT STATUS (2026-07-16)
3. `docs/GROK_STATE.md`
4. `docs/EVENT_PRODUCTION_QA.md` (built tooling section)
5. Code: `poc/pi_endpoint/raw_endpoint.py` — `_seed_handshake` / `SUSTAIN_SEQ` / `HdmiPlayer.feed` / stall path in `atomic_drm_present.py`

## Owner signals this session
- Picture path “excellent / almost event-ready.”
- Built + deployed event ops (watchdog, health, soak); git repo.
- **Field fail after deploy:** drone power-off with **goggles still on Pi** → HDMI black; drone power-on again → **HDMI stays black**; **goggles internal FPV recovers**. Hot-swap battery is a **hard product requirement**.

---

## #1 NEXT: Air-unit hot-swap (major — not fixed)

### Observed (owner field test, 2026-07-16)
```
goggles ↔ Pi USB stays plugged the whole time
flight / live HDMI OK
→ power OFF air unit (drone)
→ HDMI goes black (goggles still on bench, still plugged into Pi)
→ power ON air unit (fresh battery)
→ goggles show live FPV again
→ HDMI stays BLACK  ❌
```

### Desired
```
air unit OFF → HDMI blanks/idles (OK)
air unit ON  → HDMI live returns without unplugging goggles or restarting the service
```

### What the stack is *supposed* to do (and where it fails)
| Phase | Intended | Reality / likely gap |
|---|---|---|
| Air unit off | ES stops; atomic stall ~12s → player exits; DRM released; USB/AOA **stays** up; `SUSTAIN_SEQ` keepalives continue | HDMI black is expected |
| Air unit on | Goggles resume encode; ES over USB again; `feed()` sees SPS → lazy-start player | **Does not happen for owner** — HDMI stays black while goggles themselves recover |

### First diagnostic (do this before coding)
With `journalctl -u o3-hdmi -f`, run the same power cycle and answer:

**A. After air unit ON, does USB ES resume?**
- Look for `~X.XX Mbit/s` climbing again, `es_bytes=` increasing, video chunk lines.
- **If Mbit stays ~0** while goggles show FPV → goggles are **not** restarting **USB video-out** on an already-armed AOA session. Fix is likely **re-arm / re-subscribe** (replay gold `APP_SEQ` or a measured subset, or a camera/liveview SET) when ES has been silent for N seconds but host is still alive — **not** a full AOA unplug.
- **If Mbit climbs** but HDMI stays black → ES is back but **presenter path** is stuck (`feed()` / `_need_sps` / spawn gap / DRM). Fix is player/recovery logic.

**B. Did we phantom-rearm or disconnect?**
- Lines: `phantom unplug`, `re-arming for next plug`, `DISCONNECT`, `gadget re-init`.
- If re-arm fired mid-bench, recovery model is different (AOA from scratch while cable still in — may need goggles to re-enumerate).

**C. Player side**
- `STALL: no decoded frame` → `HDMI player exited — display released` is normal on air-unit off.
- After air unit on: want `HDMI: ES aligned to SPS` / `atomic-kms-present` / `frames=… ~50 fps`. Absence + rising Mbit ⇒ spawn/feed bug; absence + flat Mbit ⇒ arm/stream bug.

### Likely fix directions (pick after A/B/C)
1. **Video-silence re-arm (most likely if A = flat Mbit):** while `configured` and host live, if `es` rate ~0 for e.g. 5–15s after having streamed, re-run `APP_SEQ` once (or a smaller “resume” subset) without tearing down AOA. Do **not** loop full APP_SEQ every 1s (that caused chop 2026-07-14).
2. **Presenter recovery (if A = Mbit up):** ensure dead player + new SPS always `_spawn()`; check `_min_spawn_gap` / stall exit not leaving a wedged state; log clearly.
3. **Avoid false phantom rearm** during air-unit-only silence if goggles still TX keepalives but we mis-classify — only if logs show phantom.

### Acceptance test (product)
| Step | Pass |
|---|---|
| Live HDMI | OK |
| Air unit off ≥30s, goggles still plugged | HDMI idle/black; service still active; no D-state |
| Air unit on, goggles show FPV | HDMI live within a few seconds **without** cable unplug or `systemctl restart` |
| Repeat 5× battery swaps | All recover |

---

## Verify the 3 event-ops changes (next session, can run same outing)

All three are **deployed and active** on `o3pi` as of this session close. They were **not** fully field-proven with goggles/soak.

### 1) `o3-oops-watchdog` (auto-reboot on kernel wedge)

| Check | How you know it works |
|---|---|
| Running | `systemctl is-active o3-oops-watchdog` → `active` |
| Heartbeat | `journalctl -u o3-oops-watchdog -n 20` → `start poll=…` + `kmsg cursor ready` |
| Dry-run safe | Leave `O3_OOPS_REBOOT=1` in field; do **not** deliberately oops unless owner wants |
| Real trigger (optional, disruptive) | Only if wedge occurs naturally: D-state python / oops in dmesg → within ~8–15s Pi reboots; after boot `/var/log/o3/last-oops-reboot.txt` exists with reason |
| False positive | Idle service with goggles unplugged should **not** reboot |

**Pass:** unit active; no surprise reboots in normal use; if a real wedge happens, unattended reboot within ~30s.

### 2) `event_health.sh` (preflight)

```bash
ssh pi@192.168.1.57 '~/o3test/scripts/event_health.sh --preflight'
```

| Want | Meaning |
|---|---|
| Exit **0** / GREEN | Booth ready (monitor plugged on HDMI-A-2, services up, `throttled=0x0`, no D-state, atomic player) |
| Exit **1** / YELLOW | Usable with care (read warnings) |
| Exit **2** / RED | Do not start show — fix crits |

**How you know it works:** With monitor on A-2 and services up, all checks `ok` (except stream-hint idle until goggles). Unplug monitor → `monitor` goes crit under `--preflight`. Stop watchdog → warn. Simulate nothing destructive.

### 3) `o3_soak.sh` (2h / short trial)

```bash
# short proof
ssh pi@192.168.1.57 'sudo ~/o3test/scripts/o3_soak.sh --duration 10m --interval 15'
# full event proof once short passes + hot-swap fixed
ssh pi@192.168.1.57 'sudo ~/o3test/scripts/o3_soak.sh'   # 2h default
```

| Artifact | Location |
|---|---|
| Samples | `/var/log/o3/soak_<stamp>/samples.csv` |
| Verdict | `…/summary.txt` → `RESULT: PASS` or `FAIL` |

**How you know it works:** While streaming, CSV rows show non-zero `mbit_s` / `present_fps` ~50, `dstate=0`, `throttled=0x0`. Summary PASS. (Until hot-swap is fixed, soak during **one continuous** air-unit-on period only.)

---

## What shipped this session (code — already on Pi)

| Item | Path | Deployed |
|---|---|---|
| Oops watchdog | `scripts/o3_oops_watchdog.sh` + `o3-oops-watchdog.service` | ✅ active |
| Preflight | `scripts/event_health.sh` | ✅ on Pi |
| Soak | `scripts/o3_soak.sh` | ✅ on Pi |
| Unit default atomic | `poc/pi_endpoint/o3-hdmi.service` | ✅ |
| Git | `main` (see `git log`) | laptop |

## Pi at close
| Item | Value |
|---|---|
| Host | `o3pi` / `192.168.1.57` |
| `o3-hdmi` | leave **active** |
| `o3-oops-watchdog` | leave **active** |
| Hot-swap | **BROKEN** (filed above) — do not call event-ready until fixed |

## Next session order of work
1. **Diagnose hot-swap** with journal A/B/C during one air-unit off→on cycle (goggles stay plugged).
2. **Implement resume** (likely silence-triggered re-arm of gold seq / subscribe; or player recovery).
3. **Acceptance:** 5× battery swaps without cable touch.
4. **Then** short soak 10m + preflight GREEN proof; optional 2h soak.
5. Optional cleanup: Python `selected_player()` default → atomic; UDC quiescence harden.

## Git at close
See `git log -3` on laptop. Push handoff commits with session close if not already on remote (no remote required).
