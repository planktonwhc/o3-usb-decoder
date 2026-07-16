# Session close — 2026-07-13 dual HDMI offline (Grok)

Offline dual-HDMI **eyes-on PASS** (both full-frame). Live dual with goggles not
run this session. Service left ready for live dual field test.

---

## For next session (priority: live dual field confirm)

Read in order:

1. `CLAUDE.md` (CURRENT STATUS)
2. **This file**
3. `docs/SESSION_2026-07-13_dual_hdmi.md` (failure table + root cause)
4. `poc/pi_endpoint/README.md` (single + dual rules)
5. `docs/GROK_STATE.md`

### Next session goal

1. **Live dual field test** — air unit ON → plug goggles → both monitors show live FPV  
2. Hotplug: unplug one HDMI, other stays; replug  
3. Goggles unplug/replug re-arm (gotcha #4)  
4. If live dual CPU/lag bad → `v4l2h264dec` or quality trade-off notes  
5. After dual live green: network re-stream / arming trim / packaging

### Live dual success criteria

| Check | Pass |
|---|---|
| Both ports show live FPV within ~5s of plug | yes |
| Journal: `dual-drm-scale-to-fit → HDMI-A-1, HDMI-A-2` (or equivalent) | yes |
| No multi-minute lag / slow-mo | yes |
| Unplug one HDMI → other keeps video | yes |
| Goggles unplug → re-arm → both work again | yes |
| Single port (unplug one cable) falls back to mpv | yes |

### Suggested first commands

```bash
ssh pi@o3pi.local
systemctl is-active o3-hdmi
journalctl -u o3-hdmi -f
# Both monitors plugged; air unit live; then plug goggles (VBUS-isolated)
# Expect dual_drm_mirror on first SPS

# After code push:
# scripts/push_to_pi.sh
# UNPLUG goggles first, then:
# ssh pi@o3pi.local 'sudo ~/o3test/scripts/install_hdmi_service.sh'
```

### Offline re-prove (if dual live fails)

```bash
# UNPLUG goggles first
sudo systemctl stop o3-hdmi
python3 ~/o3test/poc/pi_endpoint/dual_drm_mirror.py --from-es \
  --es-file ~/o3test/captures/pi_pull/2026-07-13_133046_phaseB_standalone.h264
```

---

## Wins this session

| Item | Result |
|---|---|
| Dual offline full-frame both ports | ✅ owner eyes-on |
| A-1 crop root cause | ✅ bogus fbcon **1024×768 not in EDID** |
| `dual_drm_mirror.py` | ✅ one DRM master, per-CRTC EDID 1080p FB |
| Dual kmssink tee | ❌ modeset / SetPlane denied (documented dead) |
| fbdev shared FB | ❌ crop on A-1 (documented dead for dual quality) |
| Service dual defaults | `O3_DUAL_HDMI=1` `O3_DUAL_MODE=drm` |
| Single-port mpv path | ✅ unchanged when one connector |

### Root cause (A-1 crop) — do not forget

HDMI-A-1 EDID lists 1920×1080 / 1280×720 / … — **no 1024×768**.  
fbcon left A-1 “active” at 1024×768 while FB was 1920×1080 → top-left crop.  
**Never modeset a mode not in that connector’s EDID list.**  
`dual_drm_mirror.pick_mode()` enforces this (prefer EDID 1080p).

### Working dual config

- Process: `python3 dual_drm_mirror.py --from-es` (stdin = H.264 ES)
- Modes: EDID 1920×1080@60 both CRTCs, separate dumb FBs
- Decode: openh264dec → BGRx (not zero-copy; dual trade-off vs single mpv)
- Wire-in: `HdmiPlayer` when `O3_DUAL_HDMI=1` and ≥2 usable connectors

### Env knobs

| Env | Meaning |
|---|---|
| `O3_DUAL_HDMI=1` | allow dual path when 2+ usable HDMI |
| `O3_DUAL_MODE=drm` | `dual_drm_mirror` (default / proven) |
| `O3_DUAL_MODE=fbdev` | shared fbdev (crop risk — debug only) |
| `O3_DUAL_MODE=kms` | dual kmssink tee (broken — debug only) |
| `O3_HDMI_PLAYER=mpv` | single-port field path |

---

## Pi state at close

| Item | Value |
|---|---|
| Host | `o3pi.local` / `192.168.1.57`, user `pi` |
| Service | **`o3-hdmi` enabled + active** (leave running) |
| Env | `O3_DUAL_HDMI=1` `O3_DUAL_MODE=drm` `O3_HDMI_PLAYER=mpv` `DRM_CONNECTOR=auto` |
| Monitors | **both** A-1 + A-2 connected (edid=256, modes=21) at close |
| Goggles | unplugged (`UDC=not attached`) |
| HDMI armed | lazy start on first SPS → both connectors |
| Deploy | `scripts/push_to_pi.sh` then unplug → `sudo ~/o3test/scripts/install_hdmi_service.sh` |

**Gotcha #4:** unplug goggles **before** `systemctl stop/restart o3-hdmi`.  
**Stuck UDC:** unplugged but `state=configured` → force stop + start (confirm unplug first).  
**UDC phantom:** this session — owner confirmed unplugged while sysfs said `configured`.

---

## Files touched this session

| Path | Role |
|---|---|
| `poc/pi_endpoint/dual_drm_mirror.py` | **new** dual CRTC presenter |
| `poc/pi_endpoint/raw_endpoint.py` | dual path → dual_drm; labels; EDID notes |
| `poc/pi_endpoint/o3-hdmi.service` | `O3_DUAL_HDMI=1` `O3_DUAL_MODE=drm` |
| `poc/pi_endpoint/README.md` | dual docs / failure table |
| `scripts/prove_dual_hdmi.sh` | offline prove helper |
| `docs/SESSION_2026-07-13_dual_hdmi.md` | narrative + root cause |
| `docs/SESSION_CLOSE_2026-07-13_dual_hdmi.md` | **this handoff** |
| `docs/GROK_STATE.md` | snapshot |
| `CLAUDE.md` | CURRENT STATUS |

---

## Do not relitigate / do not regress

- Dual independent **mpv** on same DRM card  
- Ship dual **kmssink tee** without re-prove (known broken on this Pi)  
- Modeset connector to mode **not in its EDID** (esp. A-1 1024×768)  
- `fbdevsink` dual as “quality” path (crop trap)  
- `--video-sync=display-desync` / untimed+desync+nobuffer on live mpv  
- Stop service while goggles attached  
- Trust UDC `configured` without owner confirm / lsusb check  

---

## After live dual works

- Dual CPU: try `v4l2h264dec` if openh264 pegs cores  
- Optional: presentation quality on single-port mpv only  
- Network re-stream; arming-set trim; open-source packaging  
- Bitrate still ~7 Mbit/s USB ceiling unless new capture shows quality SET  
