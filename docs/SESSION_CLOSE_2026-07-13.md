# Session close — 2026-07-13 (Grok)

## For Claude / next session

Read in order: `CLAUDE.md` → `docs/protocol_notes.md` (changelog top) →
`docs/GROK_STATE.md` → this file.

## Wins this session

1. **Natural-flow Phase A works** — live FPV on phone + goggles through Pi MITM
   (no AOA pre-trigger). Capture:
   - `captures/pi_pull/2026-07-13_103110_pi-mitm_natural_payload.log` (160 MB)
   - `…_usbproxy.stdout`, `…_GOLD_ANALYSIS.txt`
2. **Video framing cracked offline** — `55 CC 4A 57` + LE32 length → H.264 High
   1920×1080. Tools: `analysis/video_deframe.py`, `analysis/extract_video_stream.py`.
   Playable: `captures/pi_pull/video_extract/hardened.mp4` / `.from_sps.h264`.
3. **usb-proxy AOA re-enum follow** — `--follow_aoa_reenum` / `USBPROXY_FOLLOW_AOA=1`.
   Apply: `scripts/apply_usb_proxy_o3.sh`. Capture default: `scripts/phaseA_capture.sh`.
4. **Phase B skeleton on bench** — `poc/pi_endpoint/raw_endpoint.py` (raw-gadget).
   Goggles enumerate Pi, bulk DUML flows, we answer 0x81/0x82. No video yet.

## Do not relitigate

- Pre-trigger bridge for primary captures (use natural-flow).
- ProductType / Mini-3 misID (false lead).
- FunctionFS on this Pi kernel (disabled).
- Fabricating SPECIAL I-frame trigger for steady-state (absent in gold FPV).

## Next code task

Replay more of the gold **app→goggles** DUML sequence after SET_CONFIGURATION in
`raw_endpoint.py` until bulk shows `55 CC 4A 57`. Source of truth: first ~5–15 s of
IN/TXG in the gold payload log (version storms already seeded; need camera/link
subscribe-style commands that precede video in the MITM session).

## Pi access

- `ssh pi@o3pi.local` or `pi@192.168.1.57`
- user `pi` / password `o3drone` (key auth installed)
- Repo: `~/o3test/` — rsync from laptop after code changes
- Endpoint **stopped** at close; `/dev/raw-gadget` free

## Hardware

Safe to power down goggles, air unit, phone, and Pi.
