# Phase B standalone bench runbook (2026-07-13)

Goal: get **live O3 video with NO phone** — the Pi emulates the phone's AOA accessory so
the goggles converse + stream, and we deframe `55 CC 4A 57` → H.264. This is the last mile;
protocol + video decode are already proven (see `docs/protocol_notes.md`, gold capture).

## What changed since last bench (why this run has a real shot)
Presenting `18d1:2d01` cold made the goggles enumerate then **suspend/disconnect without
sending bulk** — they only converse with an accessory *they* brought up. `raw_endpoint.py`
now does the full AOA handshake (normal `18d1:4ee0` → `GET_PROTOCOL`/`SEND_STRING`×6/
`ACCESSORY_START` → re-enumerate to `18d1:2d01`), and answers cs=0/`0x81`/`0x82`/`0x88`
with exact gold bytes. All offline self-tests pass; this needs hardware to confirm.

## Hardware preconditions
- Air unit **powered ON**, goggles showing **live video** (silent camera = no session).
- Goggles → **VBUS-isolated** USB-C cable (PortaPow + OTG adapter) → Pi USB-C (dwc2 gadget).
- Pi powered from **GPIO/PoE**, never the USB-C data port. Watch for undervoltage.
- Pi: `o3pi.local` / `192.168.1.57`, user `pi` / `o3drone`, key auth + passwordless sudo.

## Steps
```bash
# 1. LAPTOP — push the code
scripts/push_to_pi.sh                 # rsync repo → pi:~/o3test/ (excludes captures/venv/zip)

# 2. Pi — run the standalone endpoint (default = AOA handshake path)
ssh pi@o3pi.local
sudo ~/o3test/scripts/phaseB_standalone.sh -v
#   → preflights raw-gadget/UDC/undervoltage, then waits.
# 3. PLUG GOGGLES INTO Pi USB-C when prompted.
```

### Success ladder (watch the log, in order)
1. `[*] CONNECT (mode=normal)` — goggles enumerated us as a normal device
2. `AOA SEND_STRING[0..5] = ...` — goggles driving the AOA handshake
3. `[*] AOA ACCESSORY_START ... re-enumerating` → `[*] CONNECT (mode=accessory)`
4. `bulk OUT <n>B head=55cc4a57...` — **VIDEO** (the win); H.264 lands in the `.h264` out file

### Teardown (gotcha #4 — non-negotiable)
**UNPLUG THE GOGGLES BEFORE Ctrl-C.** Stopping raw-gadget (or the re-enum) while the goggles
are attached oopses dwc2 → UDC wedges → only `sudo systemctl reboot -i` clears it (verify via
`/proc/uptime`; plain `sudo reboot` has silently no-op'd before).

### Validate
```bash
ffprobe -hide_banner <ts>_phaseB_standalone.h264      # expect h264 1920x1080
ffplay -f h264 <ts>_phaseB_standalone.h264            # or rsync to laptop first
```

## If it stalls (branch by where the ladder stops)
- **No `SEND_STRING` after `CONNECT (mode=normal)` / SET_CONFIG** → the goggles aren't probing
  AOA on our normal device. Capture their EP0 control requests (add logging / usbmon) and see
  what they expect; may need a different normal-mode PID or descriptor shape.
- **Handshake completes, re-enum OK, but no bulk OUT** → the goggles may validate a specific
  `2d01` string or echo of a `SEND_STRING`. Diff their requests-to-us vs what the phone got.
- **`bulk OUT` but not `55cc4a57`** → it's control DUML; the responder answers 0x81/0x82/0x88.
  If they gate video on another query, add it (see `analysis/aoa_capture_parse.py` on the log).
- **A/B sanity:** `--cold-2d01` reproduces the old suspend/disconnect (no handshake) to confirm
  the wedge is the bring-up, not wiring.
- **UDC wedged / battery died mid-run** → reboot the Pi (see teardown); the script preflights
  for a stuck function and refuses to run on a wedged UDC.
