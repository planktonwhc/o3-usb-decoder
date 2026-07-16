# poc/pi_mitm — Raspberry Pi 4 USB MITM (Phase A: capture the trigger)

The Pi sits between the goggles (USB host) and the phone running DJI Fly (USB
accessory), forwarding and recording the USB traffic so we can finally read the exact
live-view START command. This is the approach that sidesteps `libAppGuard.so` (which
defeated on-phone Frida) — the tap is on the wire, not on the phone.

**Full runbook (hardware, power, wiring, capture, analysis):** `docs/pi4_mitm_setup.md`.

## Capture (on the Pi)

```bash
# Install / rebuild the O3-patched usb-proxy (once per source change):
./scripts/apply_usb_proxy_o3.sh

# PRIMARY — natural flow (phone starts as MTP; goggles drive AOA through proxy):
./scripts/phaseA_capture.sh
# optional: --vid 04e8 --pid 6860 --iframe

# FALLBACK — old pre-trigger recipe (if natural re-enum fails on bench):
./scripts/phaseA_capture.sh --pretrigger
```

Analyze:
```bash
python3 analysis/aoa_capture_parse.py captures/<ts>_pi-mitm_natural_payload.log
python3 analysis/gofly_transition.py   captures/<ts>_pi-mitm_natural_payload.log
```

## Files here
- `aoa_trigger.py` — drive the phone into DJI-Fly accessory mode from the Pi by sending
  the AOA handshake with DJI's identity strings (`manufacturer=DJI`,
  `model=com.dji.logiclink`, …). Uses: (1) standalone sanity check; (2) **fallback only**
  for `phaseA_capture.sh --pretrigger`. Prefer natural-flow (default) — the proxy now
  follows MTP→accessory re-enum itself (`--follow_aoa_reenum`).
- `iframe_inject.py` / `iframe_flip.lua` — build or in-flight flip the capture-derived
  I-frame SPECIAL pack (cs=1/0x01, pack[5] bit5).

## The heavy lifting
- **usb-proxy** (AristoChen) + **raw-gadget** (xairy), with O3 patches in
  `poc/usb_proxy_o3/` (payload log, AOA re-enum follow, I-frame inject, no-reset fixes).
- **Capture** = `USBPROXY_PAYLOAD_LOG` (primary; full bytes) + optional usbmon text backup.
- **Analysis** = `analysis/aoa_capture_parse.py` + `analysis/gofly_transition.py`.

## One-liner recap
```
goggles(host) ──USB-C, 5V cut──▶ Pi[dwc2 gadget ─usb-proxy─ USB-A host] ──▶ phone(DJI Fly)
                                        │
                              USBPROXY_PAYLOAD_LOG → captures/*_payload.log
                                        │
              aoa_capture_parse.py / gofly_transition.py → trigger / gvg status
```
