# poc/pi_endpoint — Phase B standalone O3 video endpoint (Pi 4)

The Pi **replaces the phone**: it presents as USB accessory `18d1:2d01` to the
Goggles 2, answers the identity DUML the gold natural-flow capture showed, deframes
live video (`55 CC 4A 57` → H.264), and presents on HDMI.

## Shipping path (2026-07-15+)

| Piece | Path / setting |
|---|---|
| Runtime endpoint | **`raw_endpoint.py`** (raw-gadget; FunctionFS kernel not available on this Pi) |
| Field player | **`O3_HDMI_PLAYER=atomic`** → `atomic_drm_present.py` |
| Idle screen | **`O3_IDLE_SPLASH=1`** → `idle_splash.py` (“waiting for live FPV feed”) |
| Decode | **SW `avdec_h264`** (`gstreamer1.0-libav`) — HW `v4l2h264dec` chroma-wrong |
| Canvas | Fixed DRM 1920×1080; `videoscale add-borders` letterboxes any coded size |
| Dual HDMI | **Parked** — `docs/DUAL_HDMI_PARKED.md` |
| Boot | `o3-hdmi.service` → `scripts/o3_hdmi_run.sh` |
| Oops auto-reboot | `o3-oops-watchdog.service` → `scripts/o3_oops_watchdog.sh` |
| Preflight | `scripts/event_health.sh --preflight` |
| Soak test | `scripts/o3_soak.sh` (default 5m single-battery) |
| Operator card | `docs/FIELD_TEST_CARD.md` |
| Event hardening | `docs/EVENT_PRODUCTION_QA.md` |

mpv remains in code as a historical fallback only — **do not use for field/events**.

## What is capture-derived (not guessed)

| Item | Source |
|---|---|
| Video chunk magic `55 CC 4A 57` + LE32 length | gold MITM `2026-07-13_103110` |
| Codec H.264 High 1920×1080 | offline decode of same capture |
| AOA control magic `55 CC 30 75` | earlier AOA sniffer work |
| Identity answers `cs=0/0x81` → `00 APP…`, `0x82` → `00` | gold capture app→goggles |
| Accessory VID/PID `18d1:2d01` | phone after ACCESSORY_START |

**Not required for steady-state FPV in the gold session:** SPECIAL `cs=1/0x01` I-frame
packs (absent while video flowed). No fabricated START trigger is sent.

## Files

| File | Role |
|---|---|
| `raw_endpoint.py` | **shipping** AOA accessory + deframe + HDMI player control |
| `atomic_drm_present.py` | present-per-frame KMS player (SW decode) |
| `idle_splash.py` | static DRM “waiting for live FPV feed” while idle |
| `gold_app_seq.py` | gold arm + light sustain packet sequences |
| `endpoint.py` | FunctionFS path (legacy; not used on this Pi) |
| `aoa_transport.py` | AOA wrap/unwrap + identity response builders |
| `ffs_descriptors.py` | FunctionFS descriptor blobs (bulk IN/OUT) |
| `../../analysis/video_deframe.py` | hardened chunk→H.264 deframer |
| `../../scripts/gadget_o3_accessory.sh` | configfs + FunctionFS mount |

## Run on the Pi

```bash
# once per boot
sudo pkill -f usb-proxy || true          # free the UDC
sudo ./scripts/gadget_o3_accessory.sh start

# session (goggles NOT plugged yet)
sudo python3 poc/pi_endpoint/endpoint.py \
  --ffs /dev/ffs-o3acc \
  --bind-udc fe980000.usb \
  --out /tmp/o3live.h264 \
  --play -v

# plug goggles → Pi USB-C (VBUS isolated; Pi on GPIO power)
# Ctrl-C to stop; then:
ffplay -f h264 /tmp/o3live.h264
sudo ./scripts/gadget_o3_accessory.sh stop
```

### Offline check (no hardware)

```bash
python3 poc/pi_endpoint/endpoint.py \
  --offline-test captures/pi_pull/2026-07-13_103110_pi-mitm_natural_payload.log \
  --out captures/pi_pull/video_extract/phaseb
```

## Architecture

```
goggles (USB host)
    │  USB-C, VBUS isolated
    ▼
Pi dwc2 gadget  18d1:2d01  (FunctionFS bulk OUT + bulk IN)
    │
    ├─ bulk OUT (host→Pi): video chunks 55 CC 4A 57 + DUML/AOA control
    │       → StreamingVideoDeframer → H.264 ES → file / ffplay
    │
    └─ bulk IN  (Pi→host): AOA-wrapped DUML answers (0x81 / 0x82)
```

## Known gaps / next iterations

1. **Cold-start without phone may need more DUML** than identity probes (version,
   subscribe, etc.). Compare a Phase-B dump to the gold capture and add answer
   templates the same way as `0x81`/`0x82`.
2. **AOA GET_PROTOCOL / SEND_STRING / START** from the goggles: presenting already as
   `18d1:2d01` skips the MTP→accessory re-enum. If the goggles refuse a “pre-accessory”
   device, extend FunctionFS ep0 to implement device-side AOA then re-bind — natural-flow
   taught us session state matters.
3. **Deframer polish:** USB 4 KiB chunk boundaries can still glitch frames; see
   `analysis/video_deframe.py`.
4. **Latency floor:** remaining FPV lag is mostly encode + air link + our ~0.2s
   demuxer cache. Do not chase lag with `--untimed` / `--video-sync=desync` /
   lavf `nobuffer` — those regressed to slow-mo (see below).

## Status (bench 2026-07-13, end of day)

| Item | Result |
|---|---|
| FunctionFS | **Unavailable** on Pi kernel (`CONFIG_USB_FUNCTIONFS is not set`) |
| raw-gadget path | **Works** — use `raw_endpoint.py` (not FunctionFS `endpoint.py`) |
| AOA handshake → accessory re-enum | **PASS** |
| Live video bulk + deframe | **PASS** — ~27–28 Mbit/s H.264 ES on disk |
| HDMI live view (Shinobi) | **PASS** — clean, no breakup, usable lag |

### HDMI player baseline (LOCKED 2026-07-13 — do not regress delivery flags)

Live path in `raw_endpoint.py` → `HdmiPlayer` / `default_player_cmd()`:

- **Zero-copy (single port):** `--vo=gpu --gpu-context=drm --hwdec=v4l2m2m` (~8–12%
  CPU; the old `--vo=drm --hwdec=v4l2m2m-copy` was ~290% and caused mid-frame drops).
- **Eager drain, shallow cache:** `--cache=yes --demuxer-readahead-secs=0.1
  --cache-secs=0.1 --demuxer-max-bytes=1048576` — demuxer pulls stdin so our
  hand-off queue does not drop mid-frame, without banking multi-second lag.
- **PTS-less 60 fps:** `--correct-pts=no --container-fps-override=60` (stream has
  no PTS; lavf otherwise invents 25 fps → **slow-mo**). Prefer
  `--video-sync=display-vdrop` + `--framedrop=vo` over `display-desync`.
- **Never** `--profile=low-latency` (forces `--cache=no` → just-in-time reads →
  constant breakup under USB bursts).
- **Never** combine `--untimed` + `--video-sync=desync` + lavf `nobuffer` on this
  HW path — benched as slight breakup + **slow-motion / much worse lag**.
- Hand-off queue `max_chunks=128` drop-oldest (never block USB).
- Unbuffered player pipe (`bufsize=0`) + batch drain.
- **Phantom HDMI:** auto mode requires EDID ≥128 B. Connected + VESA modes with
  0-byte EDID must not select dual (that forced openh264 software → slow-mo).
- **Dual decoder:** prefer `v4l2h264dec` after SPS level patch L5.2→L4.2; drop
  intermediate frames so present never plays catch-up.
- **Presentation quality (single-port mpv, 2026-07-13 evening):** GLSL-only
  `--scale=ewa_lanczossharp` + mild `--deband` + `--sharpen=0.45` on the imported
  DMABUF (still zero-copy). Do **not** add `--vf lavfi=…` unsharp — forces CPU
  download and re-breaks live delivery. Source ES is ~7 Mbit/s 1080p60; player
  can only recover so much.

Proven by A/B: recorded `.h264` plays flawlessly on the same mpv path; live breakup
was 100% delivery, not decode. Eyes-on Shinobi confirmed clean after the baseline.

### Dual HDMI (either port, or both)

`--drm-connector=auto` (service default). Dual path is **`dual_drm_mirror.py`**
(one DRM master, one H.264 decode, per-CRTC dumb FB).

| Connected | Backend | Notes |
|---|---|---|
| one of HDMI-A-1 / HDMI-A-2 | **mpv** zero-copy quality path | best picture |
| both + `O3_DUAL_HDMI=1` | **`dual_drm_mirror`** EDID 1080p both | eyes-on full-frame 2026-07-13 |
| both + `O3_DUAL_HDMI=0` | **mpv** on best usable only | dual disabled |
| none | wait | hotplug watcher starts player when a cable appears |

**Hard lessons (do not re-ship):**

| Approach | Result |
|---|---|
| Dual independent `mpv` | DRM master Permission denied on second |
| `decodebin ! tee ! kmssink×2` | hangs PAUSED / console both |
| `openh264 ! BGRx ! tee ! kmssink×2` | SetPlane Permission denied / modeset fail |
| `fbdevsink` shared FB | both paint, but A-1 often stuck on **bogus 1024×768** (not in EDID) → top-left crop |
| HW plane scale-to-fit into 1024×768 | kmsprint looked OK; eyes still wrong |
| **`dual_drm_mirror` EDID 1080p only** | **both full-frame eyes-on** |

Never modeset a CRTC to a mode that is not in that connector’s EDID list (fbcon
may report active 1024×768 on A-1 even though modes only list 1080p/720p/…).

**Offline dual prove:**

```bash
# UNPLUG goggles first, then:
sudo systemctl stop o3-hdmi
# plug BOTH micro-HDMI monitors
python3 ~/o3test/poc/pi_endpoint/dual_drm_mirror.py --from-es \
  --es-file ~/o3test/captures/pi_pull/*_phaseB_standalone.h264
# or: ~/o3test/scripts/prove_dual_hdmi.sh
```

Env: `O3_DUAL_HDMI=1`, `O3_DUAL_MODE=drm` (default). `O3_DUAL_MODE=fbdev|kms` = legacy.

Hotplug: plug/unplug either micro-HDMI any time; player restarts for the new set
within ~2s.
### Run (raw-gadget — preferred on this Pi)

**All-day / boot autostart (recommended):**

```bash
# from laptop (after code changes):
scripts/push_to_pi.sh
# on Pi — idempotent install + enable + start:
sudo ~/o3test/scripts/install_hdmi_service.sh
# watch:
journalctl -u o3-hdmi -f
# plug goggles when air unit shows live video
```

Power-cycle friendly: service starts on boot and waits; goggles unplug/power-off
re-arms in-process; next plug runs AOA again. Pi power off/on brings the service
back automatically. HDMI-only (no disk fill).

```bash
sudo systemctl status o3-hdmi
sudo systemctl stop o3-hdmi     # ⚠ UNPLUG goggles FIRST
sudo ~/o3test/scripts/install_hdmi_service.sh --disable
```

**Manual bench** (records a timestamped `.h264` under `captures/pi_pull/`):

```bash
sudo ~/o3test/scripts/phaseB_standalone.sh -v --hdmi
# UNPLUG goggles BEFORE Ctrl-C (dwc2 oops if raw-gadget dies while attached)
```
