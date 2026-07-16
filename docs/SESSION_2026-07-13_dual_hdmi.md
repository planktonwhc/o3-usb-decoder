# Session — 2026-07-13 dual HDMI (scale-to-fit / full-frame)

## Goal
Both micro-HDMI ports show the same live FPV feed, full frame, seamless.

## Offline result (eyes-on owner)
**PASS:** both ports full-frame video via `poc/pi_endpoint/dual_drm_mirror.py`.

## What failed (do not re-ship)

| Approach | Failure |
|---|---|
| Dual mpv | DRM master denied on second process |
| `decodebin ! tee ! kmssink×2` | PAUSED forever / console |
| `openh264 ! BGRx ! tee ! kmssink×2` | modeset / `drmModeSetPlane` Permission denied |
| `fbdevsink` shared `/dev/fb0` | Both paint, but A-1 **top-left crop** |
| HW plane scale 1920×1080 → 1024×768 | kmsprint looked correct; eyes still wrong |
| Modeset A-1 to **1024×768** | Mode **not in A-1 EDID** (fbcon phantom active mode) |

## Root cause of A-1 crop
HDMI-A-1 EDID modes are 1920×1080 / 1280×720 / … — **no 1024×768**.
fbcon left A-1 “active” at 1024×768 while the FB was 1920×1080 → plane scanned
only the top-left 1024×768 of the buffer. Keeping or scaling into that bogus
mode never fixed eyes-on. **Fix: only modeset modes present in the connector’s
EDID list; prefer 1920×1080@60.**

## What works
`dual_drm_mirror.py`:
- One DRM master open on `/dev/dri/card1`
- Per-CRTC dumb FB at EDID 1080p
- H.264 ES → openh264dec → BGRx → present on both FBs
- Live: `HdmiPlayer` when `O3_DUAL_HDMI=1` and 2 usable connectors
- Single connector: still **mpv** zero-copy (unchanged)

## Env / service
```
O3_DUAL_HDMI=1
O3_DUAL_MODE=drm          # default dual path
O3_HDMI_PLAYER=mpv        # single-port path
```

## Live field next
1. Plug both monitors, unplug goggles → reinstall service → plug goggles with live air unit.
2. Confirm dual live; hotplug; unplug/replug goggles (gotcha #4).
3. CPU: openh264 dual may be heavy — optional `v4l2h264dec` later.
4. Quality: dual path is software convert (not mpv zero-copy GLSL).

## Prove command (offline)
```bash
sudo systemctl stop o3-hdmi   # goggles unplugged
python3 ~/o3test/poc/pi_endpoint/dual_drm_mirror.py --from-es \
  --es-file ~/o3test/captures/pi_pull/2026-07-13_133046_phaseB_standalone.h264
```
