# Session close — 2026-08-30: cut USB-plug → HDMI wait; drop HW decode

## Read first (next session)
1. This file (what shipped, what still needs eyes-on).
2. Rollback knobs if plug-in arming gets flaky: `O3_ATOMIC_IDLE_FIRST=0`, `O3_ARM_GAP_CAP_S=0.08`.
3. Prior field path: `docs/SESSION_CLOSE_2026-07-16_seamless_splash.md` (SIGUSR2 resume — reused).

## TL;DR

Owner: ~10s every time goggles are plugged in; people waiting. **On `o3pi` now** (deployed 20:48 BST, goggles unplugged, no HDMI monitor attached at that moment). Eyes-on is the remaining step.

| Item | Result |
|---|---|
| Plug-in wait | **Built:** presenter pre-starts at boot (idle-first); gold arming gap cap 0.08s → 0.02s (~3.3s sleep → ~1.5s) |
| USB first, drone later (cold boot) | **Was not proven; code would skip re-arm.** Now re-arms after initial APP_SEQ even if ES never flowed |
| HW decode in presenter | **Removed.** `avdec_h264` only. `O3_HDMI_DECODE` ignored. Uncommitted hw-nv12 discarded |
| Pi deploy | **Done** 2026-08-30 20:48 BST — `o3-hdmi` + oops-watchdog active. **No HDMI monitor attached** at deploy (`HDMI-A-1/A-2 disconnected`), so idle-first presenter waits for monitor hotplug then pre-spawns. Goggles unplugged (`udc=not attached`). `throttled=0x50000` (under-voltage *has occurred* — not introduced this session). |

Expected plug-in time after deploy (goggles already showing FPV): USB/AOA + ~1.5s arm + first keyframe decode, **not** +3–5s GStreamer import. Roughly **~2–4s** vs ~10s. Not zero — AOA handshake is still goggles-side.

## Why it was ~10s

USB plug (external splash up, presenter **not** running):

1. USB enum + AOA handshake (goggles, ~1s+)
2. `APP_SEQ` 218 packets with `min(dt, 0.08)` sleeps ≈ **3.3s** (+0.3s seed delay)
3. First SPS → spawn `atomic_drm_present.py` → **gi/GStreamer import 3–5s**
4. First decoded frame → `NEED_MASTER` → modeset ~50ms

The 2026-08-22 warmup *backlog* cap (`O3_ATOMIC_WARMUP_BACKLOG_S=1.5`) fixed **lagged video after picture appeared**, not **time to first picture**.

Drone re-power already had the fast path: same process, in-process splash, SIGUSR2, no import. USB plug did not.

## What shipped

### 1. Idle-first presenter (the 3–5s)

Boot: external splash immediately, then spawn atomic. Child imports gi (paid while the box sits on the waiting screen), takes DRM master (`NEED_MASTER`), paints in-process splash, **idle-holds with no pipeline**. USB plug / first SPS: enqueue ES, `SIGUSR2`, build pipeline (plugins already registered), first frame uses the existing resume path (splash FB → YU12, master held).

Same architecture as owner-confirmed drone-re-power (`SESSION_CLOSE_2026-07-16_seamless_splash.md`). SIGUSR2 is never sent before `IDLE_SPLASH` (default SIGUSR2 would kill the process during import).

- Presenter: `_boot_idle_first()` in `atomic_drm_present.py`
- Endpoint: `HdmiPlayer.start()` calls `_spawn()` for atomic
- Off: `O3_ATOMIC_IDLE_FIRST=0` (old lazy-start on first SPS)

### 2. Faster gold arming (the ~3.3s sleep)

`O3_ARM_GAP_CAP_S` default **0.02** (was hardcoded 0.08). Camera subscribe `2/0xeb` is early in the burst. Rollback `0.08` if the goggles stop arming.

### 3. USB-first / drone-later from cold boot

Evidence **before this session:**

- Goggles stay plugged, drone off → on: **yes**, owner-confirmed (video-silence re-arm, 2026-07-16)
- Cold boot, plug goggles **with drone already on**: the 10s path, works
- Cold boot, plug goggles **then** power drone: **not proven**. `maybe_request_video_rearm` required `_es_written > 0`, so a session that never got video never replayed `APP_SEQ`. SUSTAIN keepalives do not re-subscribe the camera.

Now: after the initial gold burst finishes, re-arm on silence even if ES never started. Cadence unchanged (6s detect / 8s period / 20s settle).

### 4. HW decode dropped from the presenter

Owner: drop it unless convinced otherwise. Not convinced — five bench files, firmware check negative, hw-nv12 content-dependent. Working-tree nv12 (2026-08-22 sessions 2–3) was **discarded** (`git checkout HEAD` then stripped the committed `O3_HDMI_DECODE=hw` branch too). Historical write-up kept: `docs/SESSION_CLOSE_2026-08-22_hw_nv12_built_falsified.md` (still untracked until someone commits it).

`avdec_h264` is the only decode element. Setting `O3_HDMI_DECODE=hw` logs that it is ignored.

## Files

| Path | Change |
|---|---|
| `poc/pi_endpoint/atomic_drm_present.py` | idle-first; HW decode removed |
| `poc/pi_endpoint/raw_endpoint.py` | pre-spawn; faster arm cap; re-arm without prior ES |
| `poc/pi_endpoint/o3-hdmi.service` | comment on the new knobs |

## Deploy (owner / next session)

Unplug goggles first (gotcha #4). Then:

```
scripts/push_to_pi.sh --restart
```

Eyes-on:

1. Pi already booted, waiting screen up, **then** plug goggles (drone already live in goggles). Want first picture in a few seconds, not ~10.
2. Cold: Pi + goggles USB up, **then** power the air unit. Want HDMI to come up on its own (re-arm).
3. Existing hot-swap (goggles stay plugged, drone off/on) must still recover.

Journal: `pre-starting atomic presenter`, `idle-hold (boot)`, `SIGUSR2 in-process resume`, `gap_cap=0.020s`.

## Rollback

Unit drop-in or env:

```
O3_ATOMIC_IDLE_FIRST=0
O3_ARM_GAP_CAP_S=0.08
```

Then `scripts/restart_o3_hdmi.sh` (unplug first).

## Not done (owner second priority — see chat)

- Control panel / USB playback / Pi hotspot — real feature, not this session
- Public `v0.1.1` export — no remote on this repo; needs owner GitHub push
- Rust rewrite — idle-first removes the plug-in import tax; rewrite is not the fix for “people waiting”
- 5m soak, dual soak eyes-on, git remote
