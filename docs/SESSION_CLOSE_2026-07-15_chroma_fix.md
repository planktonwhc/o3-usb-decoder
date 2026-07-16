# Session close — 2026-07-15 (chroma bug root-caused + fixed — owner-confirmed "looks excellent now")

## Read first (next session)
1. `CLAUDE.md` (CURRENT STATUS, 2026-07-15 entry)
2. **This file** — the full diagnostic arc, the two false leads, and the fix
3. `poc/pi_endpoint/atomic_drm_present.py` — updated module docstring has the same story inline
4. Memory: `o3-colorspace-bug-atomic-presenter.md`, `feedback-dont-settle-for-hw-limitation.md`

## What happened
Owner sent a field-test recording (first extended flight since mpv was fully retired). Lag was
"acceptable" and fps/quality "excellent" — but two visual defects: a solid bright green bar at
the bottom of the screen (under the goggles' own OSD text), and cyan/blue rectangular "phantom"
ghosting on high-chroma content, worst around a TV/monitor in view.

## Dead end 1 — colorspace properties (ruled out)
Parsed the SPS VUI from a captured ES: `colour_primaries=1, transfer=1, matrix_coefficients=1,
video_full_range_flag=0` → BT.709, limited range. Hypothesized `atomic_drm_present.py` never
told the vc4 primary plane this, so it fell back to a mismatched default. Added code to set
`COLOR_ENCODING`/`COLOR_RANGE` plane properties explicitly and deployed — **zero visual change**.
Checked `/sys/kernel/debug/dri/1/state` live: every plane already defaulted to
`ITU-R BT.709 YCbCr` / `YCbCr limited range`. The property set was a no-op. Left the code in
(harmless, matches measured truth) but documented it is NOT the fix.

## Dead end 2 — firmware/eeprom update (ruled out)
Owner asked to just try this path. `rpi-eeprom-update` reported the bootloader already current;
`raspi-firmware` (the package that actually ships `start4.elf`, the VideoCore GPU/codec
firmware) was already at the latest version apt had. Nothing to gain there — didn't touch the
unrelated general system package updates that were also pending (out of scope, own small risk,
left for the owner to decide separately).

## Owner pushback (important)
At this point the fix was framed as "the HW decode block just has this limitation, live with it
or accept a speed tradeoff for software decode." Owner explicitly rejected that: *"we are
replacing DJI Fly and CosmoStreamer, combined we have everything we need to compete. Think
harder."* That was the right call — see `feedback-dont-settle-for-hw-limitation.md`.

## Real root cause — proven via byte-level A/B
Enabled the presenter's existing debug hooks live: `/tmp/o3_tee_es` (tees the raw stdin ES to a
file) and `O3_ATOMIC_DUMP_DIR` (dumps raw decoded I420 frames before any DRM/display code runs).
Had the owner fly briefly, pulled both down.

Decoded the *identical* tee'd bytes a second, independent way — ffmpeg's software H.264 decoder
— and diffed chroma planes pixel-for-pixel against what the Pi's HW decoder
(`v4l2h264dec`/`bcm2835-codec`, `/dev/video10`) produced:
- At the OSD bar: HW decode gives chroma **V=0** (clipped/extreme) → converts to solid
  saturated green. Software decode of the exact same bytes gives **V≈128** (neutral/correct) —
  every sampled frame, including the very first keyframe.
- The single largest chroma divergence *outside* the OSD bar sits right at the top edge of a
  bright monitor in frame — exactly where the visible cyan ghosting was.

Ruled out the SPS level-patch (`O3_H264_LEVEL_PATCH`, L5.2→L5.1, needed because `v4l2h264dec`
caps at L5.1) as the actual cause: reverse-patched a captured ES back to L5.2 and confirmed
`v4l2h264dec` still flat-out rejects it ("not-negotiated") on the current kernel
(6.18.34+rpt, already fairly fresh) — so the workaround is still necessary for HW decode, but
patching to L5.1 vs L4.2 produced **byte-identical corruption either way**, proving the declared
level value isn't what's driving the bug. Checked the PPS for anything unusual (CABAC,
`transform_8x8_mode_flag=1`, no scaling matrices, `chroma_qp_index_offset=0`) — nothing exotic,
just ordinary High-profile 1080p. Conclusion: `bcm2835-codec` itself has a genuine chroma
reconstruction bug at sharp/high-contrast edges — a firmware bug in old fixed-function
VideoCore4 silicon, not a misconfiguration on our side.

## The fix
Before accepting software decode as a "correctness vs. speed" tradeoff, benchmarked it for real
on this exact Pi 4 (rather than reasoning abstractly about whether it would be fast enough):
`gst-inspect-1.0` showed `openh264dec` available but slow (~32 fps, insufficient). Installed
`gstreamer1.0-libav` (provides `avdec_h264`, FFmpeg's decoder) and benchmarked it against the
real captured live footage: **120-125 fps** — more than 2x the ~50-60 fps actually needed. Zero
speed tradeoff.

Switched `atomic_drm_present.py`'s GStreamer pipeline from `v4l2h264dec` (HW) to `avdec_h264`
(SW) in both the normal and debug-tee pipeline strings. Verified pixel-for-pixel on the Pi
itself (not just desktop ffmpeg) that `avdec_h264` gives correct chroma (V≈128) at the same
coordinates HW decode gave V=0, and rendered a PNG to confirm visually clean at both trouble
spots. `avdec_h264` also has no level ceiling, so it decodes the native L5.2 stream directly —
the level-patch is no longer required for this presenter (left in place; harmless, not worth the
churn to remove).

Deployed via `scripts/push_to_pi.sh` + `scripts/o3_hdmi_safe_stop.sh --restart` (checked goggles
were unplugged first each time, per gotcha #4). Added the new dependency to
`scripts/install_hdmi_service.sh`'s idempotent apt-install block so a future reflash picks it up
automatically (`gst-inspect-1.0 avdec_h264` check → `apt-get install gstreamer1.0-libav`).

**Owner, after re-flying past the same TV: "confirmed, looks excellent now."**

## Diagnostic technique worth reusing
The debug hooks already built into `atomic_drm_present.py` (`/tmp/o3_tee_es`,
`O3_ATOMIC_DUMP_DIR`) made this whole investigation possible without needing new instrumentation
— they let us capture ground truth (exact bytes in, exact decoded pixels out) from a live flight
and then replay/diff offline at the byte level. When a visual defect shows up again, reach for
these first rather than guessing from the rendered output alone.

## Remaining / next
- Dual-CRTC (Stage 2) — unchanged from before, see `CLAUDE.md` Immediate next tasks.
- Flip `O3_HDMI_PLAYER` code default from mpv to atomic (owner never wants mpv) — safe cleanup,
  not yet done.
- Optional: strip the now-unnecessary SPS level-patch (low priority, harmless as-is).
- Everything else in `CLAUDE.md`'s Immediate next tasks list is unaffected by this session.
