# Dual HDMI — parked (2026-07-13 night)

**Status:** dual work is **parked**. Field path is **single HDMI → mpv** only
(the “decent / excellent” recovery from earlier today). Do not re-enable dual in
the boot service until a dedicated dual session re-proves live stability.

---

## Why parked

| Field symptom | Cause |
|---|---|
| Both screens console while goggles live | No USB attach, or player never modesets |
| Lag / choppy / slow-mo | Dual openh264 too slow; phantom 2nd port forced dual; mpv crash-loop |
| “Really bad” broken video | mpv dying ~1 Hz + orphan DRM masters (two mpv) |
| Far OK few seconds then black after HDMI replug | Hotplug respawn + DRM fight mid-stream |
| Live dual `v4l2h264dec` | Often `not-negotiated` until SPS level L5.2→L4.2 patch; still flaky live |

Single-port **mpv zero-copy** was eyes-on good when dual was off and only the
**far micro-HDMI (HDMI-A-2)** was used.

---

## What we built (keep the code)

| Path | Role |
|---|---|
| `poc/pi_endpoint/dual_drm_mirror.py` | One DRM master, per-CRTC dumb FB, EDID-only modeset |
| `HdmiPlayer` multi branch + `O3_DUAL_HDMI` / `O3_DUAL_MODE` | Wire-in when dual allowed |
| SPS `level_idc` patch L5.2→L4.2 | Needed for Pi `v4l2h264dec` caps |
| Phantom EDID filter (usable = EDID ≥128 B) | Don’t treat VESA-only “connected” as real |
| Offline prove | `scripts/prove_dual_hdmi.sh` |

### Env knobs (dual — **do not set for field**)

```bash
O3_DUAL_HDMI=1          # allow dual when ≥2 usable connectors
O3_DUAL_MODE=drm        # dual_drm_mirror (default dual backend)
O3_DUAL_MODE=fbdev      # shared fbdev — crop trap, debug only
O3_DUAL_MODE=kms        # dual kmssink tee — known broken on this Pi
O3_GST_DECODER=auto     # v4l2h264dec if present else openh264dec
O3_H264_LEVEL_PATCH=1   # SPS level rewrite for v4l2
```

### Offline dual prove (research only)

```bash
# UNPLUG goggles first
sudo systemctl stop o3-hdmi
# both monitors, real EDIDs
python3 ~/o3test/poc/pi_endpoint/dual_drm_mirror.py --from-es \
  --es-file ~/o3test/captures/pi_pull/*_phaseB_standalone.h264
```

Offline eyes-on (2026-07-13): both ports **full-frame** with EDID 1080p (not
fbcon 1024×768). Live dual never stayed field-stable.

---

## Known dual dead ends (do not re-ship)

| Approach | Result |
|---|---|
| Dual independent `mpv` | DRM master Permission denied on second |
| `decodebin ! tee ! kmssink×2` | Hangs PAUSED / console both |
| `openh264 ! BGRx ! tee ! kmssink×2` | SetPlane / modeset fail |
| `fbdevsink` shared `/dev/fb0` | A-1 top-left crop (bogus 1024×768 not in EDID) |
| Modeset non-EDID mode | Permanent crop / wrong geometry |
| Live openh264 1080p60 dual | Too slow → slow-mo |
| Fast player respawn without reaping orphans | Crash-loop + black screens |

**Never modeset a mode that is not in that connector’s EDID list.**

---

## Dual resume checklist (next dual session)

1. Single-port field path green for a full session (no crash-loop).  
2. Both ports show **edid≥128** in sysfs before enabling dual.  
3. Offline dual prove still passes.  
4. Live: `O3_DUAL_HDMI=1`, journal `dual-drm` + `v4l2h264dec` (or openh264 with
   drop-to-latest only as last resort).  
5. Success: both live FPV, no multi-minute lag, unplug one HDMI other stays,
   goggles unplug/replug re-arms.  
6. Only then flip service default dual back on.

Session notes already written:

- `docs/SESSION_2026-07-13_dual_hdmi.md`
- `docs/SESSION_CLOSE_2026-07-13_dual_hdmi.md`
- `docs/SESSION_2026-07-13_lag_fix.md`

---

## Field single path (active — do not break)

See also `docs/SESSION_CLOSE_2026-07-13_field_hdmi.md`.

```text
O3_HDMI_PLAYER=mpv
O3_DUAL_HDMI=0
DRM_CONNECTOR=HDMI-A-2    # far micro-HDMI; leave near unplugged
```

- mpv: `--vo=gpu --gpu-context=drm --hwdec=v4l2m2m`
- `--demuxer-max-bytes=1572864` (integer), `--cache-secs=0.15`, `--framedrop=vo`
- `--no-correct-pts --container-fps-override=60`
- **Never** `display-desync` / `display-vdrop` / low-latency profile / untimed+nobuffer
- Lazy start on first SPS

**Gotcha #4:** unplug goggles before `systemctl stop/restart o3-hdmi`.
