# Session — 2026-07-13 field lag / choppy / slow-mo

## Symptom
Live FPV on HDMI was laggy, choppy, and slow-motion — on both screens and with
only one cable.

## Root causes (measured on `o3pi`)

1. **Phantom second HDMI**  
   HDMI-A-2 reported `status=connected`, `edid=0`, modes = VESA
   `1024×768 / 800×600 / …`.  
   Old `connector_is_usable()` accepted “has modes” without a real EDID →
   dual path selected even with one real monitor.

2. **Dual path = software openh264**  
   Cannot sustain 1080p60 on Pi 4 → classic slow-mo / choppy catch-up.

3. **Stream High@L5.2**  
   `v4l2h264dec` only advertises levels ≤5.1 → HW decode caps negotiation fails
   unless SPS `level_idc` is patched to 4.2 (1080p60 only needs 4.2).

4. **mpv timing on PTS-less Annex-B**  
   lavf invents ~25 fps without `--correct-pts=no --container-fps-override=60`.

## Fixes shipped

| Change | File |
|---|---|
| Usable HDMI requires EDID ≥128 B | `raw_endpoint.py` `connector_is_usable` |
| mpv: 0.1s cache, 1 MiB demuxer cap, display-vdrop, explicit 60 fps | `raw_endpoint.py` `default_player_cmd` |
| Smaller drop-oldest hand-off queue (128) | `HdmiPlayer` |
| SPS level patch + prefer `v4l2h264dec` + drop-to-latest present | `dual_drm_mirror.py` |

## Field retest (goggles unplugged before service restart)

```bash
# from laptop
PI=pi@192.168.1.57 scripts/push_to_pi.sh
# UNPLUG goggles first (gotcha #4)
ssh pi@192.168.1.57 'sudo ~/o3test/scripts/install_hdmi_service.sh'
journalctl -u o3-hdmi -f
```

**Expect single monitor (one real EDID):**
```
HDMI player started: mpv → HDMI-A-1   # or A-2
```
Optional phantom log: `ignoring phantom connector(s): HDMI-A-2(edid=0B,…)`

**Expect dual (two real EDIDs ≥128 B):**
```
dual-drm-scale-to-fit → HDMI-A-1, HDMI-A-2
decode: v4l2h264dec (level_patch=True)
```

**Pass criteria:** realtime motion (no slow-mo), lag ~encode+air+~0.1s demuxer,
no multi-second delay, no progressive lag stacking.
