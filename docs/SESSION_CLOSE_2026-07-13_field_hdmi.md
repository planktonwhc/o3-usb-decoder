# Session close — 2026-07-13 field HDMI recovery (Grok)

Owner field-tested dual/single HDMI after evening dual-gst ship. Session recovered
**single-port usable video**; dual with quality is **next session goal**.

---

## For next session (priority: dual HDMI, no quality loss, seamless)

Read in order:

1. `CLAUDE.md` (CURRENT STATUS — re-read after this file)
2. **This file** (field failures + working config + dual plan)
3. `poc/pi_endpoint/README.md` — single-port delivery rules (do not regress)
4. `docs/GROK_STATE.md`

**Next session goal:** both micro-HDMI ports show the same live FPV feed with:

- **No quality loss** vs today’s single-port mpv (zero-copy / sharp presentation if possible)
- **Seamless:** boot → plug goggles → both screens work; hotplug; no console stuck; no multi-minute lag / slow-mo
- **Stable** under goggles unplug/replug (gotcha #4 still applies)

### Dual HDMI — known failures (must not re-ship blindly)

| Approach | Result on this Pi (2026-07-13 field) |
|---|---|
| Dual `mpv` (two processes) | DRM master **Permission denied** on second |
| Dual `gst` `decodebin ! tee ! kmssink×2` | Pipeline **hangs at PAUSED**; both screens stay **console** |
| Single `gst` `decodebin → I420 → kmssink` | Never modesets; plane stays **fbcon**; ES flows, **console only** |
| Single `gst` `openh264dec → BGRx → kmssink` | Offline file **PLAYING** OK; live path with empty-stdin start **prerolled forever** |
| `mpv` + `--video-sync=display-desync` | **Slow-mo + lag stacking** (do not reintroduce) |
| `mpv` without integer `demuxer-max-bytes` | Default **150 MiB** demuxer ≈ **minutes** of lag |

### Dual HDMI — ranked approaches next session

**A. Shared-DRM / single-process compositor (highest EV for quality + dual)**  
One process owns DRM master; mirrors to both CRTCs:

- Research: DRM atomic dual-CRTC with one dumb/dmabuf buffer, or `kmscube`-style dual, or custom small C/Python drm mirror
- Or: **mpv on primary** + hardware/writeback mirror if vc4 supports (unlikely free mirror)
- Or: **wlroots / tiny compositor** too heavy for field

**B. GStreamer dual, fixed (medium EV)**  
Single process, proven offline BGRx path, live-hardened:

1. **Lazy start only on first SPS** (already in `HdmiPlayer` — use it for gst too)
2. Pipeline shape that modesets:
   ```text
   fdsrc ! queue leaky ! h264parse ! openh264dec
     ! videoconvert ! video/x-raw,format=BGRx
     ! tee name=t
       t. ! queue leaky ! kmssink connector-id=A sync=false force-modesetting=true
       t. ! queue leaky ! kmssink connector-id=B sync=false force-modesetting=true
   ```
3. Prove offline dual **PLAYING** with two monitors **before** wiring live
4. Accept openh264 CPU cost initially; then try `v4l2h264dec` + convert if caps work
5. Quality: openh264 + BGRx is **not** zero-copy GLSL quality — plan quality recovery after dual works

**C. Primary mpv + secondary “good enough” (pragmatic)**  
Keep **mpv quality** on best connector (A-2); second port gets gst BGRx or fbdev mirror.  
Seamless dual without dual-master fight; asymmetric quality may be OK for “spotter + pilot”.

**D. Two DRM leases / atomic (research)**  
If kernel supports leasing planes to two clients — historically hard on Pi vc4.

### Dual success criteria

| Check | Pass |
|---|---|
| Both ports show live FPV within ~5s of plug | yes |
| Unplug near/far cable → other stays; replug ≤2s | yes |
| No console-only when stream is flowing | yes |
| No multi-minute lag / slow-mo | yes |
| Subjective quality ≥ single-port mpv *or* documented trade-off | yes |
| Goggles unplug → re-arm → both work again | yes |

### Suggested first commands next session

```bash
# State
ssh pi@o3pi.local   # or 192.168.1.57
systemctl is-active o3-hdmi
journalctl -u o3-hdmi -n 40 --no-pager
# UNPLUG goggles before stop
sudo systemctl stop o3-hdmi

# Offline dual prove (both monitors plugged)
# connector-ids: A-1=35 A-2=44 (re-check sysfs)
gst-launch-1.0 filesrc location=.../standalone.h264 ! h264parse ! openh264dec \
  ! videoconvert ! video/x-raw,format=BGRx ! tee name=t \
  t. ! queue ! kmssink driver-name=vc4 connector-id=35 sync=false force-modesetting=true \
  t. ! queue ! kmssink driver-name=vc4 connector-id=44 sync=false force-modesetting=true
```

If offline dual PLAYING works → wire into `default_gst_cmd` + lazy start + `O3_DUAL_HDMI=1` or auto multi.  
If not → path A/C.

---

## Wins this session (field recovery)

| Item | Result |
|---|---|
| Core AOA → H.264 | ✅ still works under field plug |
| Single-port video | ✅ restored; owner: “decent again, excellent” |
| Far HDMI (A-2) | video port for field |
| Near HDMI (A-1) | leave unplugged unless dual work |
| Dual gst (as shipped evening) | ❌ console both ports |
| Phantom / stuck UDC | saw `configured` after unplug; force restart cleared |
| Lag / slow-mo on mpv | integer `demuxer-max-bytes` + no `display-desync` |
| Lazy player start | spawn on first SPS (avoid empty-pipe preroll) |

### Working single-port config (do not break)

- Service **`o3-hdmi`**: enabled; `O3_HDMI_PLAYER=mpv`, `O3_DUAL_HDMI=0`, `DRM_CONNECTOR=auto`
- Player: **mpv** zero-copy DRM, **no** quality GLSL stack (stripped for field stability)
- Caps: `--demuxer-max-bytes=1572864` (integer), `--cache-secs=0.15`, `--framedrop=vo`
- **Lazy start** on first SPS; SPS re-align after respawn
- Connector: best usable = **HDMI-A-2** when only far port plugged
- Code: `poc/pi_endpoint/raw_endpoint.py`, unit `poc/pi_endpoint/o3-hdmi.service`

### Code / env knobs

| Env | Meaning |
|---|---|
| `O3_HDMI_PLAYER=mpv` | field default (display works) |
| `O3_HDMI_PLAYER=gst` | experimental single gst live (BGRx path) |
| `O3_DUAL_HDMI=1` | experimental multi kmssink (broken until offline dual proven) |

---

## Pi state at close

| Item | Value |
|---|---|
| Host | `o3pi.local` / `192.168.1.57`, user `pi` |
| Service | **`o3-hdmi` enabled + active** (leave running) |
| Player | **mpv → HDMI-A-2** (lazy SPS start) |
| Monitors | A-1 disconnected (recommended); A-2 connected |
| Goggles | may be attached (`UDC` check before any stop) |
| Deploy | `scripts/push_to_pi.sh` then unplug → `sudo ~/o3test/scripts/install_hdmi_service.sh` |

**Gotcha #4:** unplug goggles **before** `systemctl stop/restart o3-hdmi`.  
**Stuck UDC:** if unplugged but `state=configured` and EBUSY spam → force stop + restart (owner confirmed unplug first).

---

## Do not relitigate / do not regress

- Dual independent **mpv** on same DRM card
- Ship dual gst without offline dual PLAYING proof
- Start player on empty stdin for gst (preroll death)
- `--video-sync=display-desync` / untimed+desync+nobuffer on live mpv
- Rely on `4MiB` string for demuxer-max-bytes (use **integer**)
- Stop service while goggles attached
- Blind HDLnk bitrate chase (ceiling already documented)

---

## Field narrative (compressed)

1. Dual-auto selected gst → both screens **console**.
2. Forced single mpv on A-2 → video but **minutes lag** / later **slow-mo** (desync experiment).
3. Gst live switch → ES flowing, plane still **fbcon** (I420/empty preroll).
4. Recovery: **mpv + integer demuxer cap + lazy SPS start + single far port** → owner OK.

---

## After dual works

- Re-add presentation quality on primary path only if zero-copy preserved  
- Optional: network re-stream, arming-set trim, packaging  
- Bitrate: still ~7 Mbit/s USB ceiling unless new capture shows quality SET  
