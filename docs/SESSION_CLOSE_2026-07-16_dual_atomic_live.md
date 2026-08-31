# Session close — 2026-07-16 dual atomic LIVE (owner-confirmed)

## Result

**✅ Live dual HDMI PASS.** Owner: “working well, both show identically.”

Same FPV feed on **HDMI-A-1 + HDMI-A-2**, atomic same-buffer page-flip, ~50 AU/s.

## Architecture (shipping dual path)

```
stdin H.264 ES → avdec_h264 → I420 1920×1080 (letterbox)
  → one YU12 dumb buffer pool
  → drmModeSetCrtc once per CRTC
  → drmModePageFlip same fb_id onto both CRTCs
```

| Piece | Path |
|---|---|
| Presenter | `poc/pi_endpoint/atomic_drm_present.py` multi-output |
| Endpoint | `O3_DUAL_HDMI=1` + `DRM_CONNECTOR=auto` → dual-atomic |
| Splash | `idle_splash.py` multi connector list |
| Legacy | `dual_drm_mirror.py` kept; **not** live dual backend |

## Field default (unit)

```ini
# o3-hdmi.service (shipping)
Environment=DRM_CONNECTOR=auto
Environment=O3_DUAL_HDMI=1
Environment=O3_HDMI_PLAYER=atomic
```

Single-port override: drop-in with `O3_DUAL_HDMI=0` + `DRM_CONNECTOR=HDMI-A-2`
(see `docs/DUAL_HDMI_PARKED.md`).

## Journal tokens (healthy live dual)

```
multi-HDMI dual-atomic page-flip → HDMI-A-1, HDMI-A-2
dual atomic targets: HDMI-A-1 … HDMI-A-2 … mode=1920x1080@60 (same-buffer page-flip)
initial modeset done on 2 output(s)
au_feed≈50
IDLE_SPLASH showing … on HDMI-A-1, HDMI-A-2   # after stall/EOS
```

Offline prove earlier same day: both ports full-frame from ES file (~42 fps paced).

## Still optional / polish

- COLOR_ENCODING set at setup may log `-13` before DRM master (harmless if chroma OK).
- Hotplug one HDMI mid-stream; air-unit hot-swap under dual; 5m soak dual.
- Single-port override: drop-in `O3_DUAL_HDMI=0` + `DRM_CONNECTOR=HDMI-A-2`.

## ⚠ Next session — phantom UDC (P0 ops bug)

**Symptom this session:** agent saw `udc=configured` and **refused** to
`systemctl restart o3-hdmi` (gotcha #4). **Owner: goggles were NOT attached.**

Known issue: after unplug, UDC often stays `configured` (phantom host) — does
**not** prove a real USB host is present. `scripts/o3_hdmi_safe_stop.sh` already
documents this; agent ops must not gate on UDC alone.

**Next session fix:** use safe_stop / multi-signal host detection; restart dual-
default unit cleanly when unplugged-but-phantom.

## Dead ends (still never re-ship)

Dual mpv, tee×kmssink, fbdev crop, live openh264 dual_drm — see older park notes.
