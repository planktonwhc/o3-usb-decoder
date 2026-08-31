# Dual HDMI — status (updated 2026-07-16)

**Status (2026-07-16):** **live dual atomic PASS** — owner “working well, both show
identically.” **Unit default is dual** (`O3_DUAL_HDMI=1`, `DRM_CONNECTOR=auto`).

Full arc: `docs/SESSION_CLOSE_2026-07-16_dual_atomic_live.md`.

---

## Defaults / single-port override

Shipping unit (`o3-hdmi.service`):

```bash
DRM_CONNECTOR=auto
O3_DUAL_HDMI=1
O3_HDMI_PLAYER=atomic
```

```bash
# DISABLE → single far port only
# UNPLUG goggles first (gotcha #4)
sudo mkdir -p /etc/systemd/system/o3-hdmi.service.d
sudo tee /etc/systemd/system/o3-hdmi.service.d/single.conf <<'EOF'
[Service]
Environment=DRM_CONNECTOR=HDMI-A-2
Environment=O3_DUAL_HDMI=0
EOF
sudo systemctl daemon-reload
sudo systemctl restart o3-hdmi

# back to dual default
sudo rm -f /etc/systemd/system/o3-hdmi.service.d/single.conf
sudo systemctl daemon-reload
# UNPLUG goggles first
sudo systemctl restart o3-hdmi
```

Expect journal: `dual-atomic-page-flip → HDMI-A-1,HDMI-A-2` and
`initial modeset done on 2 output(s)`.

### Env knobs

```bash
O3_DUAL_HDMI=1              # allow dual when ≥2 usable connectors
DRM_CONNECTOR=auto          # or HDMI-A-1,HDMI-A-2 (pin A-2 alone = single)
O3_HDMI_PLAYER=atomic       # dual backend = atomic multi (not dual_drm)
O3_DUAL_MODE=drm|fbdev|kms  # legacy non-atomic only; ignore for field
```

---

## What ships for dual

| Path | Role |
|---|---|
| `atomic_drm_present.py` | Multi-output: same YU12 FB, modeset once/CRTC, page-flip both |
| `raw_endpoint.py` | `O3_DUAL_HDMI=1` → dual-atomic cmd; splash multi list |
| `idle_splash.py` | Multi connector splash (same XRGB FB) |
| `dual_drm_mirror.py` | Offline/legacy ctypes helper; **not** live dual player |

Offline prove:

```bash
# UNPLUG goggles first
sudo systemctl stop o3-hdmi
sudo python3 ~/o3test/poc/pi_endpoint/atomic_drm_present.py \
  --device=/dev/dri/card1 \
  --connector=HDMI-A-1,HDMI-A-2 \
  --es-file ~/o3test/captures/pi_pull/2026-07-14_100fps_readtest_L51.h264 \
  --pace-fps 50
```

---

## Why the old dual path was parked (2026-07-13) — do not re-ship

| Approach | Result |
|---|---|
| Dual independent `mpv` | DRM master Permission denied on second |
| `decodebin ! tee ! kmssink×2` | Hangs PAUSED / console both |
| `openh264 ! BGRx ! tee ! kmssink×2` | SetPlane / modeset fail |
| `fbdevsink` shared `/dev/fb0` | A-1 top-left crop (bogus 1024×768) |
| Live openh264 `dual_drm_mirror` | Too slow → lag / slow-mo |
| Phantom EDID as usable | Forced dual with one real monitor |

**Never modeset a mode that is not in that connector’s EDID list.**

---

## Optional next

- Flip unit default dual on if venue always has two monitors.
- Dual soak / air-unit hot-swap eyes-on under dual.
- Fix COLOR_ENCODING apply after master acquired (setup may log -13).
