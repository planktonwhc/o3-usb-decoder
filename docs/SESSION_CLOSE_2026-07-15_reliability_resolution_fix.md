# Session close — 2026-07-15, later session (field reliability incident — resolution/letterbox crash-loop fixed, owner-confirmed "excellent"; separate kernel oops found, not fixed)

## Read first (next session)
1. `CLAUDE.md` (CURRENT STATUS, 2026-07-15 "later session" entry)
2. **This file** — the full diagnostic arc, both root causes, the fix, and what's still open
3. `poc/pi_endpoint/atomic_drm_present.py` — `build_pipeline()` docstring has the same story inline
4. Memory: `o3-resolution-mismatch-and-kernel-oops.md`

## What happened
Owner reported HDMI stuck on console after the chroma-fix session closed — streaming errors,
not fixed by multiple reboots including cold boots. This looked at first like a regression from
the chroma fix (HW→SW decoder swap), but turned out to be two unrelated bugs.

## Bug 1 — resolution mismatch crash-loops the presenter every single spawn (FIXED)
Live SSH debugging on `o3pi` (journal + `dmesg`) showed `atomic_drm_present.py` logging
`Layout(1440x1080 ...)` on every spawn, immediately followed by
`drmModeSetCrtc (initial modeset) failed (-28)` repeated ~40 times, then the presenter exiting
for endpoint respawn — forever, in a tight ~6s loop. This is different from earlier sessions'
intermittent chop: it reproduced on **every single frame of every single spawn**, no exceptions.

Ruled out the SPS level-idc patch as the cause (disabled it via `O3_H264_LEVEL_PATCH=0`,
restarted, same 1440x1080 result). Confirmed independently with `ffprobe` on a tapped raw ES
(`/tmp/o3_tee_es` flag file → `atomic_drm_present.py` tees stdin to
`/tmp/o3_live_es_<pid>.h264`) that the SPS itself really does say `width=1440, height=1080` —
not a GStreamer/decoder artifact, the goggles are genuinely encoding 1440x1080 (exactly 4:3)
right now, not the previously-established 1920x1080 (16:9).

Root cause: `AtomicPresenter.setup()` picks the DRM CRTC mode from a *hardcoded* 1920x1080
preference (`--width`/`--height`, default 1920x1080) **before any frame is decoded**, then later
allocates the scanout dumb buffer sized to whatever the *actual* decoded frame turned out to be.
When those two disagree, `drmModeSetCrtc` requires the FB to cover the full mode area and
rejects a too-small FB with ENOSPC. This had presumably never been exercised with a 4:3 stream
before under the atomic presenter (earlier 2026-07-14 confirmation was with a 16:9 stream); the
owner said 4:3 has shown correctly with black side bars before, under whatever pipeline was in
use previously (likely mpv, which auto-letterboxes any input).

**Fix:** added a `videoscale` stage to `build_pipeline()`'s GStreamer pipeline so the appsink
always hands the presenter a fixed target_w x target_h canvas (1920x1080 by default) regardless
of the stream's actual coded resolution — decouples DRM mode choice from whatever the goggles
send. First cut used plain `videoscale ! video/x-raw,width=1920,height=1080` with no PAR
constraint. Deployed, tested live: modeset succeeded immediately, clean ~50fps, zero crash-loop
— but the owner reported the image was **cropped at the edges**, not the full frame (expected:
4:3 content pillarboxed with black bars, like it always had). Plain `videoscale` with hard caps
and no pinned pixel-aspect-ratio was negotiating against the SPS's own declared PAR and
cropping instead of preserving the frame.

**Real fix (same session):** pinned `pixel-aspect-ratio=1/1` on the caps immediately after
`avdec_h264` (before scaling) AND on the final target caps, and added `videoscale
add-borders=true`. This makes `videoscale` reason purely in raw pixel dimensions and pad
(letterbox/pillarbox) rather than crop or stretch to hit the target size — GStreamer bakes the
black bars into the actual frame data, so the DRM buffer-copy code in `AtomicPresenter` needed
zero changes. Deployed, verified live: modeset succeeds on the first attempt, sustained clean
playback, full frame visible with correct aspect ratio. **Owner: "its excellent."**

Still open: **why did the goggles switch to 1440x1080 (4:3)?** Owner says they didn't change
anything and the goggles menu still shows "1080p." Not root-caused this session — worth asking
whether an aspect/FOV toggle exists that could have been bumped, or whether this is a firmware
default. Not blocking — the fix now handles either aspect ratio correctly going forward.

## Bug 2 — genuine kernel oops in raw_gadget.ko (found, NOT fixed)
Separately, while chasing bug 1, found a real kernel-level bug via `dmesg`: a NULL-pointer
dereference oops inside `raw_gadget.ko` (`raw_event_queue_add` called from `gadget_resume`,
itself called via `dwc2_hsotg_udc_stop → dwc2_gadget_exit_clock_gating`, during
`raw_release()`'s `usb_gadget_unregister_driver()` teardown). Trigger: `raw_endpoint.py`'s
phantom-unplug re-arm (`RawGadget.close_fd_only()` / `.reinit()`, both `os.close(fd)`) racing
real bus activity from the goggles reconnecting at the same moment.

Confirmed empirically this is **not recoverable by `systemctl restart`**: the crashed process
wedges in D-state (uninterruptible kernel sleep, un-killable even with SIGKILL), and a freshly
spawned process immediately wedges into D-state too — the whole dwc2/raw-gadget kernel
subsystem is broken for the rest of that boot. Only a full Pi reboot clears it. This
extends the existing "gotcha #4" (previously understood as only firing on a *manual*
`systemctl stop` while goggles are attached) — it turns out the same kernel race can fire
**autonomously in the field**, any time the goggles briefly drop signal and reconnect at an
unlucky instant, with no operator action involved.

**Not fixed.** This is a genuine out-of-tree kernel module race (`raw_gadget.ko` is tainted
`[O]`), not something reliably closeable from userspace. Options discussed with the owner but
deferred:
- Harden `signal_phantom_unplug()`/`reinit()` to check UDC quiescence before closing the fd
  (reduces exposure, doesn't eliminate the race).
- Build a watchdog that tails `dmesg`/journal for this oops signature and auto-`reboot`s the Pi
  within ~30s, so "reboot always fixes it" happens without the owner noticing a black screen.
  Owner said "all good for now" — revisit if this recurs in the field.

## Diagnostic technique worth reusing
- `cat /sys/class/udc/*/state` — quick UDC health check; `not attached` after a crash + a
  D-state `python3` process in `ps aux` means the kernel is wedged, reboot required, don't waste
  time on `systemctl restart`.
- `/tmp/o3_tee_es` flag file + `ffprobe` on the resulting `/tmp/o3_live_es_<pid>.h264` — fastest
  way to get ground-truth stream parameters (resolution, level, profile) independent of
  whatever the GStreamer pipeline is doing, without disrupting the live service.
- Testing a hypothesis by disabling it live (`O3_H264_LEVEL_PATCH=0` via a systemd drop-in) is
  fast and reversible — cheaper than reasoning about byte-offset patch correctness in the
  abstract.

## Remaining / next
- Kernel oops mitigation (watchdog or UDC-quiescence check) — deferred, owner said "all good for
  now."
- Why the goggles are now sending 1440x1080 instead of 1920x1080 — not investigated, ask owner
  about aspect/FOV settings if it recurs or matters.
- Dual-CRTC (Stage 2), mpv default flip, SPS level-patch cleanup — all unchanged from the
  chroma-fix session's next-steps list, unaffected by this session.
