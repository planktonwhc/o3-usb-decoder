# Phase 1 capture rig setup — non-root Android + DJI Fly

CLAUDE.md's original Phase 1 plan called for a rooted phone + `tcpdump`. We hit FRP
(factory reset protection) on the handed-down Samsung meant for rooting, so this
session uses the **non-root path** on a regular daily-driver Android phone instead:
**PCAPdroid** in VpnService mode captures system-wide traffic without root, including
traffic over a USB-attached RNDIS interface (the goggles' link).

Trade-off vs. rooted `tcpdump`: PCAPdroid captures at the IP layer via a local VPN
tunnel, not raw framing off the interface itself — slightly less low-level, but still
gives a full `.pcap` of every packet to/from the goggles, which is all Phase 2
(`analysis/pcap_harness.py`, `analysis/duml.py`) needs.

## 1. Install apps on the phone
- **DJI Fly** — Play Store.
- **PCAPdroid** — try F-Droid first (fully-featured, GPL, no paywall); Play Store
  version works too but double-check pcap export isn't gated behind a paid unlock
  before relying on it.

## 2. Reduce noise before capturing
Turn on **Airplane Mode**, no need to re-enable Wi-Fi/cellular — the USB/RNDIS link to
the goggles works independently. This keeps the capture limited to goggles traffic
instead of mixing in every other app's chatter.

## 3. Configure PCAPdroid
- Open the app, start a capture once to accept Android's VPN permission dialog ("This
  app wants to set up a VPN connection..." — expected and required; it's a fully local
  loopback, nothing leaves the phone). Stop that test capture.
- In Settings: capture format = pcap. Confirm there's no setting restricting capture to
  Wi-Fi only — PCAPdroid is supposed to see all system traffic including a
  USB-tethered/RNDIS interface, but this hasn't been verified on this exact phone/Android
  version yet. **Sanity check before relying on a real capture:** connect to the
  goggles first (step 4), start a PCAPdroid capture, and just open a browser or ping
  the goggles IP from a terminal app — confirm PCAPdroid's live connections list shows
  traffic to the goggles' IP (should be in the same family as `192.168.60.x`, but
  confirm on this specific goggles unit — don't assume it matches the Linux recon
  session's IP). If it does NOT show up, the VpnService route may be excluding the
  local USB subnet — see "If PCAPdroid doesn't see the traffic" below.

## 4. Connect phone to goggles
- Goggles powered on, linked to the air unit, showing video as normal.
- USB-C to USB-C cable, phone to goggles (OTG adapter only if the phone doesn't do
  native USB-C host mode over a straight cable — most modern phones don't need one).
- Watch for a "USB device attached" notification on the phone. If `adb` is enabled
  (Settings > Developer Options > USB debugging), you can confirm the interface + IP
  from a PC with `adb shell ip addr` — not required, just a convenience.

## 5. Capture
1. Start the PCAPdroid capture (do this BEFORE opening DJI Fly, to catch the earliest
   possible handshake bytes).
2. Open DJI Fly. The exact UI path to get goggles video over USB is not yet known —
   this is part of what Phase 1 is for. Things to try:
   - Sign in / let it detect the USB-attached device automatically.
   - Look in DJI Fly's settings for anything like "Goggles", "USB Display", "Monitor
     Mode", "Screen Mirror", "Live View".
   - Also check the **goggles' own on-screen menu** — some DJI devices need the output
     mode enabled on the source device itself, not just requested by the client app.
3. Once video is flowing, let it run **30-60 seconds** of steady state (not just the
   first couple seconds) so Phase 2 has a clean chunk of the actual video flow to
   analyze, not just the handshake.
4. Stop the PCAPdroid capture, export/save the `.pcap`.

## 6. Get the file onto the Linux recon box
- Easiest: PCAPdroid's share/export sheet → save, then transfer however's convenient
  (adb pull, cloud, cable to PC).
- If `adb` is enabled: `adb pull /storage/emulated/0/Download/<filename>.pcap ./captures/`
- **Rename per the project's append-only convention**:
  `YYYY-MM-DD_dji-fly-usb-session_fw<goggles_firmware_version>.pcap` — check the
  goggles firmware version in their on-screen settings menu before renaming, and note
  the DJI Fly app version too (both belong in the filename or a sidecar note; firmware
  drift is a known gotcha — see CLAUDE.md "Risks & gotchas").

## 7. Analyze
```
.venv/bin/python3 analysis/pcap_harness.py captures/<your_file>.pcap --goggles-ip <ip from step 4>
```
This ranks conversations by bandwidth, flags the likely video flow, scans every top
conversation for embedded DUML packets (see `analysis/duml.py` — confirmed present on
this hardware over USB interface 4, tagged `cmd_set=OFDM`), and dumps the video flow's
payload for NAL-start-code inspection.

## If PCAPdroid doesn't see the USB/RNDIS traffic
Some Android versions exclude a locally-routed interface (like USB tethering) from a
VpnService's capture scope even with a 0.0.0.0/0 route configured. If the sanity check
in step 3 shows no goggles traffic:
- Check PCAPdroid's settings for anything mentioning "capture Ethernet/USB" or similar
  and make sure it's enabled.
- As a fallback, revisit rooting a device after all (Magisk + `tcpdump`, the original
  CLAUDE.md plan) — this guarantees interface-level capture regardless of Android's
  VPN routing quirks.
