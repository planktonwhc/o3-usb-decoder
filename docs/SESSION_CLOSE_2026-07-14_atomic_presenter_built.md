# Session close — 2026-07-14 (atomic-KMS presenter BUILT + validated offline; owner eyes-on pending)

## Read first (next session)
1. `CLAUDE.md` (CURRENT STATUS)
2. **This file** — what got built, what's proven, and the exact owner test to run next
3. `docs/SESSION_CLOSE_2026-07-14_gst_atomic_pivot.md` — the decision + arc that led here
4. `poc/pi_endpoint/atomic_drm_present.py` — the new presenter
5. Memory: `o3-atomic-presenter-built.md`, `o3-chop-is-mpv-not-usb.md`

## ⚡ UPDATE (same session): FLIPPED TO ATOMIC, field-tested, corruption bug found+fixed
Owner captures Pi HDMI via a **Cam Link** (not the Atomos). Flipped the service to
`O3_HDMI_PLAYER=atomic` (drop-in `10-atomic.conf`) after proving offline it modesets once and
the connector survives (stayed connected + 21 modes). Owner verdict on the live offline run:
**"smoothness looks excellent but picture is broken."** Cam Link capture measured **~42 unique
fps** (vs mpv's ~14 chop) → present-per-frame architecture works, chop gone.

**Root cause of "picture broken":** the pre-decoder `queue leaky=downstream` dropped compressed
**reference NALs** under the display's vblank backpressure → macroblock corruption accumulating
to the next keyframe. The headless `--probe` pulled frames fast with no backpressure, so it
never leaked → looked clean → masked the bug (a real validation-gap lesson).

**FIX (deployed):** pre-decoder queue is now **non-leaky** (`max-size-bytes=4MB`, no `leaky`);
we stay live by dropping **decoded** frames at the appsink (`drop=true`) only. Backpressure
propagates to stdin → the endpoint drops at SPS/GOP boundaries (`_enqueue_units` clears +
realigns — clean, never stalls the USB reader). Verified: decoded frames dumped during a real
paced present run (`O3_ATOMIC_DUMP_DIR`) are **pixel-clean** (full-frame, sharp OSD). The
endpoint execs the presenter fresh each goggles-plug, so the fix is live with **no restart**.

**RULE for this pipeline: never leak compressed ES — only ever drop decoded frames.**

**✅ CONFIRMED:** after the fix, re-ran the offline present live — owner: **"it looked
excellent."** Smooth AND clean; the mpv chop is solved. Connector stayed connected + 21 modes.

Still pending: **live 100 fps flight through goggles** (only the offline bench clip is
confirmed so far), then dual-CRTC (Stage 2). mpv remains the code default; atomic is active via
the `10-atomic.conf` drop-in with instant revert.

## ⚠️ COLD-BOOT BUG (owner rebooted to confirm) — root-caused + fixed
Owner rebooted the Pi to confirm; **HDMI stuck on console, no video on goggle plug.** Diagnosis:
- Video WAS flowing (au_feed 42/s, resyncs=0) — AOA fine. But the endpoint was in **permanent
  back-pressure**, feeding **39-byte "aligned to SPS" fragments** (SPS split from its IDR in the
  raw USB stream). The atomic presenter cold-starts slowly (**GStreamer/PyGObject import ~3-5s
  from cold disk cache**); during that window the endpoint dropped + re-aligned, fragmenting the
  first keyframe → the HW decoder never got a complete frame → never produced output → **the
  presenter's `run_present` looped forever** (it didn't watch the GStreamer bus and had no stall
  watchdog) → and it had grabbed **DRM master at startup** → **frozen console** + a zombie
  holding the card (which also blocked every later attempt).

**Three fixes (all deployed + validated offline):**
1. **Endpoint warm-up grace for the atomic player** (`raw_endpoint.py`): during the slow
   cold-start, buffer a deep window (`O3_ATOMIC_WARMUP_SECS`, default 8s) instead of
   dropping/re-aligning — so the first keyframe stays intact and the decoder starts cleanly.
   Validated with a simulated 5s cold-start (`O3_ATOMIC_STARTUP_DELAY`): **zero back-pressure,
   clean modeset, ~45 fps** (vs the un-fixed run which back-pressured + fragmented).
2. **Deferred DRM acquisition** (`atomic_drm_present.py`): `open()/setup()` (SetMaster + modeset)
   now happen on the **first decoded frame**, not at startup. A presenter that never decodes a
   frame **never grabs DRM master → can never freeze the console.** Validated: stuck presenter
   (stdin open, no data) → exits via watchdog, **card1 stays free**.
3. **Stall watchdog + GStreamer bus watch** (`atomic_drm_present.py`): exit (non-zero → endpoint
   respawns) if no decoded frame for `O3_ATOMIC_STALL_SECS` (default 12s, > warm-up 8s so a cold
   first spawn that stalls simply respawns **warm** and succeeds), and surface/act on pipeline
   ERROR/EOS instead of hanging silently. No more zombie holding master.

**Reproduction method (kept):** `poc/pi_endpoint` + a harness that drives the real endpoint's
`HdmiPlayer` with the bench file at realtime AU rate (`O3_HDMI_PLAYER=atomic`) — reproduces the
endpoint↔presenter back-pressure offline without goggles. `O3_ATOMIC_STARTUP_DELAY` simulates
the cold gi import.

**State:** service restarted with the fixed endpoint code, `O3_HDMI_PLAYER=atomic`, armed. The
fix survives reboot (on disk). Worst case now is a clean respawn, never a frozen console.
**Still needs the real cold-boot goggle-plug to confirm** (the exact USB SPS/IDR split couldn't
be reproduced from a file), but the mechanism is fixed and the failure mode is now safe.

## ✅✅ FINAL: LIVE + OWNER-CONFIRMED "test was successful" — chop SOLVED
After a **cold reboot** (owner's real test) the presenter stayed on console. Root-caused from a
**live ES capture** (endpoint-side tap): the O3 stream is **H.264 L5.2 and repeats an SPS with
every keyframe** (~55 SPS in a 15s capture, all L5.2). The endpoint patched only the **first**
SPS, so the Pi HW decoder (`v4l2h264dec`, max **L5.1**) threw **`not-negotiated (-4)`** the moment
the 2nd keyframe's L5.2 SPS arrived → presenter died → respawn loop → console. mpv had tolerated
the L5.2 SPS; the strict gst decoder does not — which is exactly why this was **atomic + live
only** (the bench file was pre-patched on every SPS, so it always worked).

**FIX (validated by replaying the exact captured L5.2 stream through the real endpoint):** patch
**every** SPS → **L5.1** in `feed()` (not just the first; 5.1 not 4.2 so high-fps 1080p fits).
Result live: *"patched → 5.1 on every AU"* → first frame → modeset once → **steady ~50.3 fps
sustained** (2500+ frames), no chop, no errors. Owner: **"test was successful."**

En route this cold-boot debug also fixed: **deferred DRM** (grab master only on the first decoded
frame → a stalled presenter never freezes the console), **stall watchdog + GStreamer bus watch**
(exit+respawn instead of hanging silently), **endpoint warm-up grace** (`O3_ATOMIC_WARMUP_SECS`
8s — don't fragment the first keyframe during the slow gi cold-start), and **spawn gap 5s** (don't
reap a presenter still importing). Debug knobs left in (all off by default): `O3_ATOMIC_STARTUP_
DELAY`, `O3_ATOMIC_STALL_SECS`, `O3_ATOMIC_WARMUP_SECS`, `O3_ATOMIC_DUMP_DIR`, `/tmp/o3_tee_es`.

**Three RULES banked:** never leak compressed ES (only drop decoded frames); a player must never
hold DRM master with nothing to show; patch EVERY SPS not just the first (O3 repeats L5.2 SPS per
keyframe). **Owner directive: never mpv again** — atomic is the player.

**Next session:** dual-CRTC (Stage 2) — same decoded buffer page-flipped onto both connectors.

## One-line handoff
Built the atomic-KMS present-per-frame presenter (`atomic_drm_present.py`) and wired it into
the endpoint as `O3_HDMI_PLAYER=atomic`. HW decode + frame layout + `AddFB2(YU12)` + connector
selection are **all validated headlessly on the Pi**; only the live `SetCrtc` modeset +
page-flip need the **owner's eyes-on** (they touch the monitor, which I can't see). mpv stays
the committed default — the atomic path is fully opt-in, zero regression risk.

---

## What got built

### `poc/pi_endpoint/atomic_drm_present.py` (new)
Single-HDMI present-per-frame presenter. Architecture (revised from the pivot doc — see "Why
not zero-copy dmabuf" below):
- **Decode (in-process GStreamer, HW):** `fdsrc ! queue(leaky) ! h264parse ! v4l2h264dec !
  video/x-raw,format=I420 ! appsink` via the `gi` (PyGObject) bindings.
- **Present (custom DRM, ctypes reused from `dual_drm_mirror.py`):** keep a 2-buffer pool of
  DRM **YU12 (YUV420) dumb buffers** — the vc4 display plane scans YU12 out natively — memcpy
  each decoded frame into the free buffer and **`drmModePageFlip`** it onto the connector's
  CRTC. **`drmModeSetCrtc` (the modeset) runs exactly ONCE** on the first frame; everything
  after is a page-flip. A page-flip is *not* a modeset, so it can never drop the Atomos's HDMI
  sync the way kmssink's per-spawn `force-modesetting` did. Double-buffer + wait-per-flip =
  tear-free, paced to the panel refresh.
- Modes: `--probe` (decode + report layout/fps, **no DRM, no monitor**) and default (full
  present). Input: stdin ES (live) or `--es-file PATH` with a built-in real-time byte-pacer
  (`--pace-fps N`, no `pv` on the Pi).

### `poc/pi_endpoint/raw_endpoint.py` (integrated)
- New `selected_player()` → `mpv` | `gst` | `atomic`; `prefer_mpv_player()` derives from it.
- `default_atomic_cmd()` + `_atomic_present_path()`; `_single_player_cmd()` helper picks the
  player for both single-target build paths.
- `_spawn` labels the atomic player, elevates it with `nice -n -10`, and `_reap_orphan_players`
  now also `pkill -f atomic_drm_present.py` (it holds DRM master; a leftover would block the
  next spawn's SetMaster).
- `_player_is_mpv` stays **False** for atomic → fps-adapt + mpv IPC stats correctly stay
  mpv-only. Stdin is the endpoint's already-level-patched (L5.2→L4.2) ES; atomic does **not**
  re-patch stdin (only its offline `--es-file` feeder patches).

### New Pi dependency (INSTALLED this session)
`python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 python3-gst-1.0` — the appsink
path needs PyGObject introspection. Already `apt-get install`ed on `o3pi`. **On a reflash,
reinstall these** or the atomic player won't import (mpv is unaffected).

---

## What's PROVEN (headless, on `o3pi`, this session)
- **HW decode + frame layout:** decoded the bench file (`captures/pi_pull/
  2026-07-14_100fps_readtest_L51.h264`), rendered a frame to PNG and eyeballed it — **clean
  1080p FPV frame, DJI OSD legible** (ACRO / BATT<FULL / 7.8V / 39 Mbps / DISARMED). Layout
  solver: I420 **stride 1920, padded height 1088, offsets Y=0 / U=2088960 / V=2611200** —
  matches the visual. Decode ceiling **~71 fps** (>50/60, headroom).
- **appsink sample → raw frame:** `try_pull_sample` + `map` works; single contiguous memory,
  3133440 bytes/frame.
- **DRM present-half plumbing (no modeset):** as root while the service was idle — `open` +
  `drmSetMaster` OK, connector resolve → **HDMI-A-2 = connector 44, crtc 114, mode
  1920x1080@60**, created 2 YU12 dumb buffers, **`drmModeAddFB2(YU12)` succeeded** (fb_ids
  724/725), mmap writable. vc4 card1 plane formats include YU12/YV12/NV12 (confirmed).
- **Integration:** `O3_HDMI_PLAYER=atomic` builds the right command; mpv/gst unchanged.
- **Offline byte-pacer:** `--pace-fps 45` → ~40 fps effective (vs 71 unthrottled) — throttles.

## What is NOT yet proven (needs owner — touches the monitor)
- The single live `drmModeSetCrtc` modeset onto the connected Atomos.
- `drmModePageFlip` cadence + page-flip event draining under a real live feed.
- Whether the picture is actually smooth / chop-free vs mpv (the whole point).
I deliberately did **not** run a live modeset from an automated session: the monitor state is
unknown to me and the pivot doc warns a modeset to a disconnected/mismatched Atomos wedged it
to "0 modes" (needed a power-cycle). That step is the owner's eyes-on.

## Why NOT zero-copy dmabuf (deviation from the pivot doc's "recommended architecture")
The pivot doc proposed appsink → DMABuf fd → `drmPrimeFDToHandle`. On this Pi that path
**does not work through appsink**: `v4l2h264dec` hands appsink `allocatorsysmem0` (plain system
memory, `is_dmabuf_memory=False`, fd=-1) even with `capsfilter memory:DMABuf` and
`capture-io-mode=dmabuf`. Reason: appsink does **not** drive the GST allocation query that
makes the decoder export dmabuf (kmssink does, via `propose_allocation` — that's why kmssink
got zero-copy). Rather than depend on the exotic `unixfdsink` fd-passing wire protocol or an
untestable dmabuf import, I use HW-decode → **one ~3 MB/frame memcpy** into a YU12 dumb buffer.
On a Pi 4 (multi-GB/s memory bandwidth) that's ~150–300 MB/s = a few % of one core — negligible,
and every step is offline-verifiable. The chop fix is **present-per-frame + modeset-once**, not
zero-copy specifically. Zero-copy dmabuf can be revisited later (kmssink-style allocation query)
if the memcpy ever shows up in a profile — it won't at these rates.

---

## OWNER TEST — do this next (needs the monitor connected)

**Offline first (no drone), on the far micro-HDMI (HDMI-A-2), monitor powered ON:**
```bash
ssh pi@192.168.1.57
# 1) make sure no other DRM player is up (service idle / goggles unplugged)
cd ~/o3test/poc/pi_endpoint
# 2) run the presenter against the bench file, paced ~real-time at content rate (50)
sudo python3 atomic_drm_present.py \
    --es-file ~/o3test/captures/pi_pull/2026-07-14_100fps_readtest_L51.h264 \
    --connector HDMI-A-2 --pace-fps 50
# EXPECT: journal-ish stderr "target HDMI-A-2 ... mode 1920x1080@60",
#         "allocated 2 YU12 dumb buffers", "initial modeset done — page-flips only",
#         "frames=… ~NN fps". On screen: the clip playing, smooth, modeset ONCE.
# VERIFY: the Atomos does NOT lose sync / drop to 0 modes. Ctrl-C to stop.
```
If the offline run looks right and the monitor stays healthy, go **live**:
```bash
# deploy the new code the canonical way (files already scp'd but do it properly):
scripts/push_to_pi.sh
# select the atomic player for the service (drop-in), then, GOGGLES UNPLUGGED, restart:
sudo mkdir -p /etc/systemd/system/o3-hdmi.service.d
printf '[Service]\nEnvironment=O3_HDMI_PLAYER=atomic\n' | \
    sudo tee /etc/systemd/system/o3-hdmi.service.d/10-atomic.conf
sudo systemctl daemon-reload
ssh pi@192.168.1.57 'sudo ~/o3test/scripts/o3_hdmi_safe_stop.sh --restart'   # goggles OFF first
journalctl -u o3-hdmi -f
# then: air unit ON, live video in goggles, plug goggles → Pi USB-C. Fly 100 fps.
# A/B the smoothness vs the mpv baseline (revert = remove the drop-in + safe restart).
```
**To revert to mpv instantly:** `sudo rm /etc/systemd/system/o3-hdmi.service.d/10-atomic.conf`
+ daemon-reload + safe restart (goggles unplugged). mpv is untouched and remains the default.

## Gotchas carried forward
- Rapid re-modeset kills the Atomos — the presenter modesets **once**; keep it that way.
- Needs DRM master → run the offline test with `sudo` (the service runs as root already).
- `python3-gi` + gst introspection is now a runtime dep of the atomic path (reinstall on reflash).
- Gotcha #4 unchanged: unplug goggles before any `o3-hdmi` stop/restart.
- Level patch L5.2→L4.2 mandatory for `v4l2h264dec`; the endpoint does it for stdin, the atomic
  `--es-file` feeder does it for offline files (`--no-level-patch` to disable).

## Files touched
- `poc/pi_endpoint/atomic_drm_present.py` — NEW (presenter + probe + offline pacer).
- `poc/pi_endpoint/raw_endpoint.py` — `selected_player`, `default_atomic_cmd`,
  `_single_player_cmd`, atomic label + nice + orphan-reap.
- `docs/SESSION_CLOSE_2026-07-14_atomic_presenter_built.md` — this file.
- Memory `o3-atomic-presenter-built.md`.
- Pi: `apt-get install python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 python3-gst-1.0`.
