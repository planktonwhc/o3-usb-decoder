# Session close — 2026-07-14 (chop root-caused → HW render proven → kmssink failed → atomic pivot)

## Read first (next session)
1. `CLAUDE.md` (CURRENT STATUS)
2. **This file** — the arc + the atomic-presenter build spec below
3. Memory: `o3-chop-is-mpv-not-usb.md`
4. `poc/pi_endpoint/dual_drm_mirror.py` — existing DRM ctypes foundation to extend
5. `docs/DUAL_HDMI_PARKED.md` (Stage-2 context)

## Next session mission (decided): **build the atomic-KMS present-per-frame presenter**

---

## The arc this session (what we proved)

### 1. The HDMI chop is mpv's fault — PROVEN, not the goggles/USB/reader
Owner clips (`~/Downloads/2026-07-14 14-05-20.mp4` Pi HDMI vs `DJI_0519.MOV` goggles DVR):
Pi HDMI = **~14 unique fps**, flat. So we captured the **raw USB ES on the Pi** (goggles at
100fps flight, `raw_endpoint.py --out`, NO `--hdmi` → pure read+deframe, mpv uncoupled):
`captures/pi_pull/2026-07-14_145004_100fps_readtest.h264` (+ `_L51.h264` level-patched copy).
- Raw stream = **2464 coded frames / ~49s = ~51 fps**, **0% dup/skip frames** (median 12.7KB),
  **2104 unique (mpdecimate) = ~43 unique fps**. resyncs=0, badlen=0. Cleaner than the 07-13
  "excellent" 50fps baseline.
- Live 14:05 journal: `au_feed=50.2`, `dec_drop=0`, `resyncs=0` → reader/decoder clean.
- **Conclusion:** goggles + AOA reader + deframer deliver clean full-motion ~43-50fps even at
  100fps flight. The ~14fps chop is **mpv presentation** (container-fps guess + framedrop
  eating 2/3 of frames). DJI Fly gets the same stream and is smooth → the Pi can match it.
- Goggles 2 / O3 spec: transmits **1080p60** (40ms) or **1080p100** (30ms) [PAL: 50/100].
  DVR is a separate pipeline (records 50 regardless). Owner **must** fly 100fps (non-negotiable).

### 2. HW zero-copy render path VALIDATED (owner eyes-on: "the REAL DEAL, smooth")
On this Pi's GStreamer, `v4l2h264dec ! kmssink` negotiates **true zero-copy**:
`v4l2h264dec` src = `video/x-raw(memory:DMABuf), format=DMA_DRM, drm-format=YU12`; kmssink
imports that DMABuf straight as a DRM framebuffer — no videoconvert, no CPU copy (~4% CPU).
- HW decode ceiling ~**79 fps** (>50, fine). Old gst path used **software** `openh264dec`
  (why gst was shelved); the real unlock was DMA_DRM DMABuf negotiation (newer GStreamer).
- **Requires stream level ≤ 5.1.** Native O3 stream is **L5.2**; endpoint's O3_H264_LEVEL_PATCH
  (L5.2→L4.2) satisfies it. Bench: patch `67 64 00 34`→`67 64 00 33` (5.2→5.1), 83 SPS/file.
- Cards: **card0 = v3d (render, no KMS)**, **card1 = vc4 (display, connector HDMI-A-2 = id 44)**.

### 3. Live A/B FAILED — kmssink is the wrong vehicle
Deployed `default_gst_live_cmd` rewritten to the HW path + `O3_HDMI_PLAYER=gst` drop-in, flew
100fps. Result: **gst crash-looped every ~4s** — `kmssink: Could not get allowed GstCaps of
device / driver does not provide mode settings configuration`. Far HDMI stayed on **console**.
- My eyes-on had used `filesrc + sync=true`; the **live** `fdsrc + sync=false` config was never
  actually on-screen (validation gap). But the real problem is **kmssink's per-spawn
  `force-modesetting`**: it re-grabs/re-modesets the display every spawn, fails to acquire
  cleanly under the systemd service, and the rapid modeset churn **made the Atomos Shinobi drop
  HDMI sync → connector went `disconnected` (0 modes)**, which a reboot did NOT restore
  (dmesg "Cannot find any crtc or sizes"). Needed a **monitor power-cycle + cable re-seat**.
- Explicit `fd=`, `bus-id=gpu`, auto-connector all still failed once the monitor was gone
  (0 modes = same error) — that masked clean retesting. mpv keeps working because it modesets
  **once** via explicit `--drm-device=/dev/dri/card1` and holds it.
- **Reverted to mpv; rig fully restored** (monitor back 21 modes; mpv 8s playback clean).

---

## THE PLAN: atomic-KMS present-per-frame presenter (converges Stage 1 + Stage 2)

**Why:** kmssink's modeset churn is toxic (crash-loop + killed the monitor). mpv coexists fine
because it opens the DRM device explicitly and **modesets exactly once**. Build a presenter
that does the same: **open `/dev/dri/card1`, become DRM master, atomic modeset ONCE on
connector 44, hold it, then page-flip each decoded frame.** No per-frame/per-spawn re-modeset.
This kills the chop (present-per-frame, no fps guess) AND is the foundation dual-HDMI needs
(same buffer flipped onto both CRTCs → Stage 2).

### Recommended architecture (pragmatic split)
- **Decode half — GStreamer `appsink`:** `fdsrc fd=0 ! queue(leaky) ! h264parse ! v4l2h264dec !
  appsink` (caps `video/x-raw(memory:DMABuf),DMA_DRM`). Each sample carries a **DMABuf fd** +
  drm-format (YU12) + stride/offset. Robust HW decode, no hand-rolled V4L2 ioctls.
- **Present half — custom DRM (extend `dual_drm_mirror.py`):** it already has libdrm ctypes
  bindings + CRTC/plane modeset. But it uses **dumb buffers + software openh264 + memcpy** —
  replace that with: import each appsink DMABuf via `drmPrimeFDToHandle` → `drmModeAddFB2`
  (or AddFB2WithModifiers) → atomic/legacy **page-flip** onto the CRTC's plane. Modeset once at
  start (pick 1920x1080@60 from EDID), then only flips. Recycle FBs for the decoder pool.
- Present-per-frame: on each new appsink sample, flip its FB. sync to vblank (or not, for
  lowest latency). No re-modeset ever after the first.

### Fallback / alternative
Full custom V4L2 M2M (`/dev/video10`) + DRM (no GStreamer) — more code, max control. Only if
appsink DMABuf import proves fiddly. Not preferred first.

### Integration
- New player kind (e.g. `O3_HDMI_PLAYER=atomic`) selected in `_build_player_cmd` /
  `prefer_mpv_player()`. Fed the SAME stdin ES (endpoint's coalesce/queue/level-patch/prebuffer
  all reusable; keep `self._player_is_mpv` gating so fps-adapt/IPC stay mpv-only).
- **Must modeset ONCE.** Never loop force-modesetting (that's what killed the Atomos).
- Keep mpv as the fallback/committed default until the presenter is field-proven.

### Test loop (offline, no drone)
Bench file on Pi (persistent): `captures/pi_pull/2026-07-14_100fps_readtest_L51.h264`
(level-patched, ~2464 frames ~49s). Recreate `/tmp` copy if needed. Pace it real-time into the
presenter (no `pv` on Pi — use a small python byte-pacer, see below). Verify: modeset once,
smooth present, and the Atomos stays connected (no sync drops). Monitor MUST be connected.

### Gotchas learned (do not repeat)
- **Rapid re-modeset kills the Atomos Shinobi** (drops HDMI, survives reboot until power-cycle).
  Modeset once and hold.
- kmssink `driver-name=vc4` / `force-modesetting` is unreliable under systemd — avoid; go atomic.
- `/tmp` is wiped on reboot (lost the L51 file once) — bench copy now persisted in captures/.
- Level patch is mandatory for the HW decoder (L5.2 native → must be ≤5.1).
- Gotcha #4 still applies: unplug goggles before any service stop/restart.

---

## Pi state at close
| Item | Value |
|---|---|
| Host | `o3pi` / `192.168.1.57` |
| Service | `o3-hdmi` **active + enabled**, `O3_HDMI_PLAYER=mpv` (drop-in removed) |
| Endpoint | updated (`default_gst_live_cmd`=HW path; `_player_is_mpv` gating) md5 d270f055 |
| Monitor | HDMI-A-2 **connected**, 21 modes, edid 256B |
| UDC | not attached; throttled=0x0; no orphan gst/mpv |
| Bench file | `captures/pi_pull/2026-07-14_100fps_readtest_L51.h264` (persistent) |
| Reverting gst try | already reverted; committed unit is mpv |

## Files touched this session
- `poc/pi_endpoint/raw_endpoint.py` — `default_gst_live_cmd` → HW zero-copy (v4l2h264dec→kmssink);
  `self._player_is_mpv` flag; fps-adapt/IPC gated mpv-only. (Deployed on Pi.)
- Memory `o3-chop-is-mpv-not-usb.md` (chop root cause + gst fail + atomic pivot).
- This doc.

## One-line handoff
Chop is mpv (raw USB is clean ~43-50fps); HW zero-copy render is smooth (eyes-on); kmssink
crash-looped live and killed the monitor sync → **next session build a custom atomic-KMS
present-per-frame presenter (GStreamer appsink decode → DMABuf → drmModeAddFB2 → page-flip,
modeset ONCE), extending `dual_drm_mirror.py`; mpv stays the working default until proven.**
