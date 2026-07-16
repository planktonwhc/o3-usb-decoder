# Supported hardware and software

This is the matrix the **shipping** path (`o3-hdmi` + atomic presenter) has been
field-validated against. Anything outside it may work, but is **best-effort**
until someone reports success.

## Field-validated (primary)

| Piece | Value |
|---|---|
| Host computer | **Raspberry Pi 4 Model B** (**2GB** field unit — confirmed working; more RAM fine) |
| OS | Raspberry Pi OS **64-bit Bookworm** (Lite or Desktop) |
| USB role | Pi USB-C as **gadget** (`dtoverlay=dwc2,dr_mode=peripheral`) |
| Gadget stack | **raw-gadget** (`/dev/raw-gadget`), UDC `fe980000.usb` |
| Goggles | **DJI Goggles 2** |
| Air unit | **DJI O3 Air Unit** (live FPV must already show **in the goggles**) |
| Video path | AOA bulk framing `55 CC 4A 57` → **H.264** ES (~5–8 Mbit/s, often ~50 AU/s) |
| HDMI out | Single monitor on **HDMI-A-2** (far micro-HDMI), 1920×1080 panel class |
| Player | `O3_HDMI_PLAYER=atomic` → SW decode `avdec_h264` + atomic KMS |
| Power | Pi on **GPIO 5V or PoE**, not USB-C data; goggles cable **VBUS-isolated** |

## Known-good behaviours (shipping)

- Cold boot → idle splash → plug goggles → live HDMI
- Unplug goggles → splash; replug → live again
- Air-unit / battery power cycle with goggles still on Pi → re-arm (field-confirmed)
- Non-16:9 coded sizes (e.g. 1440×1080) letterboxed onto fixed 1920×1080 canvas
- Correct chroma at high-contrast edges (SW decode; do not switch back to Pi HW H.264 by default)

## Explicitly not shipping / parked

| Item | Status |
|---|---|
| Dual HDMI | Parked — `docs/DUAL_HDMI_PARKED.md` |
| mpv as field player | Retired (historical fallback only) |
| Network re-stream (RTMP/NDI/etc.) | Not started |
| FunctionFS gadget path | Kernel on this Pi lacked `CONFIG_USB_FUNCTIONFS`; raw-gadget is the path |
| PC-as-USB-host RNDIS “diagnostic” personality | Explored; **not** the DJI Fly video path |

## Untested / community-needed

Please open an issue with results if you try these:

- Goggles **Integra**, **Goggles 3**, **Goggles N3**
- **O4** / **O4 Pro** air units (may change bitrate, resolution, or arming)
- Raspberry Pi **5** (different UDC / DRM names likely)
- Non-Bookworm kernels (rebuild `raw_gadget.ko` for your `uname -r`)
- Other DRM cards/connectors (`DRM_DEVICE`, `DRM_CONNECTOR`)

## Firmware

Pin and report **goggles + air-unit firmware versions** that work for you.
Upstream firmware updates can change arming or framing without notice. This
project does not modify goggle or air-unit firmware.

## Capture phone (research only)

Phase A MITM (goggles → Pi → phone + DJI Fly) was used once to obtain a gold
natural-flow capture. **Day-to-day HDMI does not need a phone.** MITM tooling
under `poc/pi_mitm/` and `poc/usb_proxy_o3/` is optional research infrastructure.
