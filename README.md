# dji-o3-usb-decoder

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Open-source reverse-engineering of the **DJI Goggles 2 (O3)** USB video-out path.
No phone, no DJI Fly, no paid license: a **Raspberry Pi 4** pretends to be the
phone (Android Open Accessory), pulls live **H.264**, and paints it on **HDMI**.

> **Not affiliated with DJI.** Independent interoperability project for hardware
> you own. Trademarks belong to their owners. See [`NOTICE`](NOTICE).

```
Goggles 2 (USB host) ──VBUS-isolated USB-C──▶ Pi 4 dwc2 (AOA accessory)
                                                    │
                                                    ▼
                                         deframe 55 CC 4A 57 → H.264 ES
                                                    │
                                                    ▼
                                    SW decode (avdec_h264) + atomic-KMS
                                                    │
                                                    ▼
                              both micro-HDMI ports (same feed; crop to 16:9)
```

**Status (2026-08-31):** field-confirmed — smooth ~50 fps, software decode
(correct chroma), 4:3 center-cropped to 16:9, **dual HDMI default**, idle splash,
air-unit hot-swap recovery, boot service + kernel-oops watchdog.

| Doc | Purpose |
|---|---|
| **This README** | Pi install → first live video |
| [`docs/SUPPORTED_HARDWARE.md`](docs/SUPPORTED_HARDWARE.md) | What is validated vs best-effort |
| [`docs/protocol_notes.md`](docs/protocol_notes.md) | Protocol findings |
| [`docs/FIELD_TEST_CARD.md`](docs/FIELD_TEST_CARD.md) | Laminated field checklist |
| [`docs/OPEN_SOURCE.md`](docs/OPEN_SOURCE.md) | How this repo is published cleanly |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | PRs, scope, what not to send |

---

## From empty Pi to live FPV HDMI

This is the path from a brand-new Raspberry Pi to the shipping setup. Do the
steps in order. Total time is roughly one evening once the parts are on the desk.

### What you need

| Item | Notes |
|---|---|
| **Raspberry Pi 4 Model B** | Field unit is **2GB** (works). Higher RAM is fine; not required. |
| **microSD 16GB+** | 32GB comfortable. Decent A1/A2 brand. |
| **Case + cooling** | Fan + heatsinks recommended for all-day SW decode. |
| **Pi power (not USB-C data)** | GPIO 5V (or PoE). Official USB-C PSU is OK **only until** goggles are plugged. |
| **VBUS isolator** | **Required.** Blocks 5V on goggles→Pi USB-C, passes data. e.g. PortaPow *Power* Blocker (+ adapters) or a DIY cut of the red wire. |
| **USB-C data cable** | Goggles → Pi USB-C (through the isolator). |
| **Monitor** | micro-HDMI → display. Default is **both** Pi ports (`DRM_CONNECTOR=auto`). One monitor on **HDMI-A-2** (far) is enough. |
| **Laptop on same LAN** | SSH + `rsync` for deploy. |
| **Goggles 2 + O3 air unit** | Air unit must show live video **in the goggles** before the Pi can show it. |

**Critical electrical rules (read before any goggles plug):**

1. **Never power the Pi from the USB-C data port while goggles are connected.**  
   Goggles are the USB host; Pi USB-C is the **gadget** data port.
2. **Isolate VBUS** on the goggles→Pi cable so two 5V supplies never fight.
3. Keep a solid 5V rail: `vcgencmd get_throttled` should report `throttled=0x0`.

More wiring detail (GPIO pins, PD trigger, fan): [`docs/pi4_mitm_setup.md`](docs/pi4_mitm_setup.md) §4.

---

### Step 1 — Flash Raspberry Pi OS

Use **Raspberry Pi OS (64-bit)**. **Lite** is enough (we talk to DRM/KMS directly;
no desktop required). Full desktop is fine if you prefer a GUI for setup.

With [Raspberry Pi Imager](https://www.raspberrypi.com/software/):

1. OS → **Raspberry Pi OS (64-bit) Lite** (or Desktop).
2. Storage → your microSD.
3. **Edit settings** before write:
   - Hostname: e.g. `o3pi`
   - Enable **SSH** (password or public key)
   - Username: `pi` (or your own — remember it for later path rewrites)
   - Wi‑Fi SSID/password + country (headless first boot)
4. Write, insert SD, boot the Pi.

First boot can use the **bundled/official USB-C PSU**. Switch to **GPIO / PoE**
before you attach the goggles.

From the laptop:

```bash
ssh pi@o3pi.local
# or use the IP from your router
```

---

### Step 2 — Put this repo on the Pi

From a laptop that has the repo (or after `git clone` of this project):

```bash
# on the laptop, from the repo root
scripts/push_to_pi.sh
# override host if needed:
#   PI=pi@192.168.1.57 scripts/push_to_pi.sh
```

`push_to_pi.sh` rsyncs code to `~/o3test/` and **skips** large/append-only trees
(`.git`, `captures/`, zips, etc.).

One-shot clone **on the Pi** also works if the repo is public and you prefer that:

```bash
# on the Pi
git clone <this-repo-url> ~/o3test
```

Expected layout on the Pi: `/home/pi/o3test/` (scripts, `poc/`, `analysis/`, …).

---

### Step 3 — Kernel gadget bring-up (`pi_setup.sh`)

The Pi must present a USB **device** controller to the goggles. That needs the
**dwc2** overlay in peripheral mode and the **raw_gadget** kernel module.

```bash
ssh pi@o3pi.local
cd ~/o3test
chmod +x scripts/pi_setup.sh
./scripts/pi_setup.sh
```

What it does (idempotent):

1. Installs build tools, kernel headers, `libusb`, `tshark`, `ffmpeg`, etc.
2. Adds `dtoverlay=dwc2,dr_mode=peripheral` to boot config.
3. Clones/builds [xairy/raw-gadget](https://github.com/xairy/raw-gadget) into `~/o3/raw-gadget`.
4. Installs `raw_gadget.ko` + `/etc/modules-load.d/raw_gadget.conf`.
5. Builds usb-proxy under `~/o3/usb-proxy` (MITM tooling; not required for day-to-day HDMI).

**First run almost always requires a reboot** (dwc2):

```bash
sudo reboot
```

After reboot, verify:

```bash
ls /sys/class/udc/        # expect: fe980000.usb
ls -l /dev/raw-gadget     # expect: character device
```

If `/sys/class/udc/` is empty, dwc2 did not load — re-check
`/boot/firmware/config.txt` (or `/boot/config.txt`) for the overlay line and reboot again.

---

### Step 4 — Hardware: power + VBUS + HDMI

Do this **before** the first goggles plug.

1. **Power Pi from GPIO or PoE** (not USB-C data).  
   Typical GPIO: pin **4 = 5V**, pin **6 = GND**. Meter ~5.0–5.2 V and correct
   polarity **before** connecting if you use a PD trigger board.
2. Build the **VBUS-isolated** goggles link, e.g.:
   ```
   Goggles USB-C → C-to-A adapter → PortaPow Power Blocker → A-to-C → Pi USB-C
   ```
3. Plug the monitor into the **far** micro-HDMI port relative to USB-C
   (**HDMI-A-2** on the Pi 4). Leave the near port free for the shipping single-HDMI path.
4. Confirm power health once the Pi is on its real supply:

```bash
vcgencmd get_throttled   # want: throttled=0x0
```

---

### Step 5 — Install the HDMI boot service

Still on the Pi, with the repo at `~/o3test`:

```bash
sudo ~/o3test/scripts/install_hdmi_service.sh
```

Idempotent. It will:

| Action | Result |
|---|---|
| Install systemd units | `o3-hdmi`, `o3-oops-watchdog` |
| Enable + start them | Wait for goggles; auto-reboot on rare `raw_gadget` wedge |
| Install GStreamer + libav | `avdec_h264` SW decode (HW decode has wrong chroma on Pi) |
| Install PyGObject / Gst GI | `python3-gi` for `atomic_drm_present.py` |
| Quiet boot cmdline | `quiet logo.nologo vt.global_cursor_default=0` (less console flash; reboot once to apply) |

Check:

```bash
systemctl status o3-hdmi o3-oops-watchdog
journalctl -u o3-hdmi -f
```

Service defaults (shipping):

| Env | Value | Meaning |
|---|---|---|
| `O3_HDMI_PLAYER` | `atomic` | present-per-frame KMS player |
| `DRM_CONNECTOR` | `auto` | all usable HDMI ports |
| `O3_DUAL_HDMI` | `1` | both micro-HDMI (same feed) |
| `O3_IDLE_SPLASH` | `1` | “waiting for live FPV feed” instead of console |
| `UDC` | `fe980000.usb` | Pi 4 dwc2 UDC name |
| `DRM_DEVICE` | `/dev/dri/card1` | vc4 DRM card on Bookworm |

If your user/home is not `/home/pi/o3test`, the install script rewrites unit paths
automatically from the real repo location.

---

### Step 6 — First live video

1. Pi powered (GPIO/PoE), services running.
2. Monitor(s) on either/both micro-HDMI — idle splash should show **“waiting for live FPV feed”**
   (after quiet-boot tokens apply, less Linux text flash). Dual is default.
3. Power the **air unit**; confirm **live FPV inside the goggles**.
4. Plug goggles → Pi USB-C **through the VBUS isolator**.
5. Within a few seconds, HDMI should show the same live feed (~1080p-class H.264,
   typically ~50 fps, ~5–8 Mbit/s — softer than onboard DVR, that is normal).

```bash
# preflight (want GREEN / exit 0)
~/o3test/scripts/event_health.sh --preflight

# live log
journalctl -u o3-hdmi -f
```

**Done.** Cold boots after this: power Pi → wait for splash → air unit ON → plug goggles.

---

## Day-to-day operation

### Normal use

| Action | How |
|---|---|
| Start of day | Power Pi; wait for idle splash; air unit ON; plug goggles |
| Air-unit / battery swap | Leave goggles on Pi; power drone off/on — HDMI should re-arm on its own |
| Unplug goggles | HDMI returns to idle splash; service stays up |
| End of day | **Unplug goggles first**, then power off Pi if desired |

### Deploy code updates (from laptop)

```bash
# laptop
scripts/push_to_pi.sh

# Pi — reinstall units/deps if needed, safe restart
ssh pi@o3pi.local
# ⚠ UNPLUG goggles first
sudo ~/o3test/scripts/install_hdmi_service.sh
# or restart only:
sudo ~/o3test/scripts/o3_hdmi_safe_stop.sh --restart
```

### Safe stop / restart

**Gotcha #4:** never tear down raw-gadget while goggles are **actually streaming**
(live `au_feed` / ES) — that can oops `dwc2` and wedge the UDC until a full reboot.

**`udc=configured` alone is NOT proof the goggles are plugged** (phantom after
unplug is common). Always use the multi-signal probe + safe_stop path:

```bash
# Canonical one-command path (laptop or Pi):
scripts/restart_o3_hdmi.sh
# After code changes:
scripts/push_to_pi.sh --restart

# Multi-signal host check (exit 0=SAFE_STOP, 1=LIVE_HOST, 2=UNCERTAIN):
~/o3test/scripts/o3_host_probe.sh
# On Pi only:
sudo ~/o3test/scripts/o3_hdmi_safe_stop.sh --restart
# LIVE_HOST → unplug first, or: scripts/restart_o3_hdmi.sh --force

# Disable entirely:
sudo ~/o3test/scripts/install_hdmi_service.sh --disable
```

### Event checks

```bash
~/o3test/scripts/event_health.sh --preflight   # doors-open
sudo ~/o3test/scripts/o3_soak.sh               # multi-minute soak → /var/log/o3/soak_*/
systemctl status o3-oops-watchdog
# after an auto-reboot from a kernel wedge:
cat /var/log/o3/last-oops-reboot.txt
```

Laminated field checklist: [`docs/FIELD_TEST_CARD.md`](docs/FIELD_TEST_CARD.md).  
Production backlog: [`docs/EVENT_PRODUCTION_QA.md`](docs/EVENT_PRODUCTION_QA.md).

### Manual bench (records `.h264`)

```bash
# unplug goggles first if o3-hdmi is running
sudo ~/o3test/scripts/o3_hdmi_safe_stop.sh
sudo ~/o3test/scripts/phaseB_standalone.sh -v --hdmi
# Ctrl-C only after unplugging goggles
```

---

## How it works (short)

1. **raw-gadget** binds to the Pi’s UDC and enumerates as AOA accessory `18d1:2d01`
   (what DJI Fly presents as).
2. **`gold_app_seq`** replays the captured arming sequence once, then a light
   **sustain** loop so the goggles keep the session alive.
3. Goggles push framed video chunks (`55 CC 4A 57` + length); **`StreamingVideoDeframer`**
   yields Annex-B H.264.
4. **`atomic_drm_present.py`** SW-decodes with `avdec_h264`, center-crops 4:3 to
   16:9 (`O3_HDMI_ASPECT=crop`; set `letterbox` for full FOV + bars), scales onto a
   fixed 1920×1080 DRM canvas, modesets once, then page-flips each frame.
5. Idle / stream loss paints an in-process splash without flashing the Linux console
   when possible; video silence can re-arm the gold sequence after air-unit power loss.

Primary code: `poc/pi_endpoint/raw_endpoint.py`, `atomic_drm_present.py`,
`gold_app_seq.py`, `analysis/video_deframe.py`. Boot path: `scripts/o3_hdmi_run.sh`
→ `o3-hdmi.service`.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| No `/sys/class/udc/` | dwc2 overlay missing or no reboot after `pi_setup.sh` |
| No `/dev/raw-gadget` | `modprobe raw_gadget`; re-run `pi_setup.sh` after a kernel update (rebuild `.ko` for new `uname -r`) |
| Preflight RED: `avdec_h264` / `python-gi` | Re-run `sudo ~/o3test/scripts/install_hdmi_service.sh` |
| Plug goggles, no video | Air unit must already show live **in goggles**; check `journalctl -u o3-hdmi -n 80` |
| Black forever / service crash-loop | Look for `SetCrtc` / ENOSPC; ensure code has letterbox path. If `python3` is in **D-state**, full reboot (or wait for oops watchdog ~8–15s) |
| Green OSD bar / cyan edge ghosts | Old HW-decode path — ensure `O3_HDMI_PLAYER=atomic` + `gstreamer1.0-libav` |
| `throttled` not `0x0` | Fix power supply before chasing software |
| Stopped service with goggles plugged | May need **full Pi reboot** (gotcha #4) |
| Wrong HDMI port | Default is both ports (`DRM_CONNECTOR=auto`). Single display: use **HDMI-A-2** or set `DRM_CONNECTOR` |

```bash
# Quick health dump
~/o3test/scripts/event_health.sh --preflight
systemctl is-active o3-hdmi o3-oops-watchdog
vcgencmd get_throttled
cat /sys/class/udc/*/state 2>/dev/null
ls -l /dev/raw-gadget
journalctl -u o3-hdmi -n 60 --no-pager
dmesg | tail -40
```

---

## Repo layout

| Path | Role |
|---|---|
| `poc/pi_endpoint/` | **Shipping** AOA endpoint + atomic presenter + idle splash + systemd units |
| `analysis/` | Deframer, DUML helpers, capture parsers |
| `scripts/` | Pi setup, install, push, safe-stop, preflight, soak |
| `docs/` | Protocol notes, session closes, field/event cards |
| `captures/` | Append-only captures (do not overwrite) |
| `poc/pi_mitm/`, `poc/usb_proxy_o3/` | Phase A MITM (phone in the middle) — not needed for daily HDMI |

Endpoint-level notes: [`poc/pi_endpoint/README.md`](poc/pi_endpoint/README.md).

---

## What this is *not*

- Not a stock DJI product feature and not an official SDK path.
- Not the old FPV Goggles V1/V2 bulk/`0x524d5654` protocol (that is [voc-poc](https://github.com/fpv-wtf/voc-poc); different hardware).
- Not CosmoStreamer (commercial closed-source). This project is the open alternative.
- Dual HDMI is the **default** (atomic same-buffer on both micro-HDMI ports). Single-port override: [`docs/DUAL_HDMI_PARKED.md`](docs/DUAL_HDMI_PARKED.md).
- Not a redistributor of DJI Fly, firmware, or proprietary native libraries.

This work is interoperability on hardware we own: reading our own drone’s video
feed on our own machines.

## Built units

The software here is free and always will be. If you would rather not flash a
Pi, solder a power feed and chase down a VBUS isolator, I build and test these
to order.

**What you get:** a Pi 4 running this code — flashed, assembled, soak-tested —
with the isolated goggles cable and the power side already sorted. Plug it in,
get dual HDMI.

**What it does NOT do. Read this before asking.**

| | |
|---|---|
| Goggles | **DJI Goggles 2 only.** Not V1/V2, Integra, Goggles 3 or N3 |
| Air unit | **O3 Air Unit only.** Not O4 / O4 Pro |
| Output | Two micro-HDMI, same feed, 1080p-class, typically ~50 fps |
| Network restream | **No.** No RTSP, SRT, RTMP, RTP, NDI |
| Telemetry overlay | **No** |
| Recording | **No** |
| Camera settings from the box | **No** |

Live USB video runs ~5–8 Mbit/s — visibly softer than the goggles' own DVR
recording. That is the protocol ceiling, not the build.

Arming replays a captured DJI Fly session. **A DJI firmware update can break it
without warning.** I will fix it when I can. There is no SLA.

**Not taking orders yet** — open an issue saying which goggles and air unit you
have, and I will tell you the price and lead time when I have them.

If you want the things above marked "No", buy CosmoStreamer. It does all of them
and it is a good product. This is the open, narrower one.


## License

[MIT](LICENSE) — see also [`NOTICE`](NOTICE) (trademarks, third-party components, no warranty).

## Contributing / publishing

- Want to help? → [`CONTRIBUTING.md`](CONTRIBUTING.md)
- Maintainers cutting a public GitHub tree from a private lab checkout →
  [`docs/OPEN_SOURCE.md`](docs/OPEN_SOURCE.md) and `scripts/export_public_tree.sh`
