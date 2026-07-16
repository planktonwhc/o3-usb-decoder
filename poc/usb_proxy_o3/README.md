# usb-proxy patches for DJI O3 / Goggles 2 AOA bridging

Vendored O3 sources that apply on top of **AristoChen/usb-proxy** base commit
`a08301d21d6ba1036cddbcb1a4314578cbc4a274`. Makes usb-proxy sit between the
DJI Goggles 2 (USB host) and an Android phone running DJI Fly (USB accessory)
on a Raspberry Pi 4 dwc2 gadget.

## Apply + build (on the Pi)

```bash
# from the o3test repo root (or wherever it was rsynced to the Pi):
./scripts/apply_usb_proxy_o3.sh
# installs sources into ~/o3/usb-proxy and rebuilds
```

`scripts/pi_setup.sh` calls that apply script automatically when present.

## What the O3 sources change

1. **`config.json`: `reset_device_before_proxy: false`**
   Startup `libusb_reset_device()` re-enums the phone out of a stable state.

2. **`proxy.cpp` EP0 RESET/DISCONNECT: guard with `set_configuration_done_once`**
   Skip phone reset during the goggles' *initial* enumeration (pre-config).

3. **`device-libusb.cpp` `set_configuration()`: skip if already configured**
   Avoids knocking an AOA-accessory phone back to MTP on SET_CONFIGURATION.

4. **Payload log `USBPROXY_PAYLOAD_LOG` + IN/OUT/TXG/TXP direction tokens**
   Full-fidelity bulk log (usbmon binary broken on Pi 6.18; text truncates at 32B).
   Parsed by `analysis/aoa_capture_parse.py` (delivery audit: IN vs TXG).

5. **I-frame inject (`iframe_inject.h`, env `USBPROXY_IFRAME=1`)**
   Flips SPECIAL cs=1/0x01 pack[5] bit5 + recomputes XOR-8 and DUML CRC16 on the
   app→goggles path. Capture-derived; off by default.

6. **Natural-flow AOA re-enum follow (`--follow_aoa_reenum` / `USBPROXY_FOLLOW_AOA=1`)**
   **(2026-07-13, first ticket)** — the primary unblocking change:

   - Start proxy against the phone in **MTP/charging** mode (e.g. `04e8:6860`).
   - Goggles drive the real AOA handshake through the proxy
     (GET_PROTOCOL / SEND_STRING×6 / **ACCESSORY_START**).
   - On ACCESSORY_START (or phone DEVICE_LEFT while follow is armed):
     - do **not** kill the process (exiting mid-session oopses raw_gadget — gotcha #4)
     - close the raw_gadget fd (goggles see a clean disconnect)
     - wait for phone to reappear as Google accessory `18d1:2d0x`
     - rebuild gadget descriptors from the accessory
     - re-init raw_gadget and continue ep0
   - Goal: gvg activation (`cs=0/0xdd`) happens in its native AOA session context,
     which the pre-trigger recipe was hypothesized to desync
     (see `docs/gofly_transition_findings.md`).

## Status

| Date | State |
|---|---|
| 2026-07-12 | Bridge stable with pre-trigger; sustained DUML both ways |
| 2026-07-13 | Trigger bytes proven (cs=1/0x01 bit5); inject delivered; FPV still not arming through pre-trigger bridge |
| 2026-07-13 | **Natural-flow re-enum follow implemented + compiles**; needs bench run |

## Capture recipes

```bash
# PRIMARY (natural flow) — phone must start as MTP, not accessory:
./scripts/phaseA_capture.sh
# optional: --vid 04e8 --pid 6860 --iframe

# FALLBACK (old pre-trigger path):
./scripts/phaseA_capture.sh --pretrigger
```

After capture:
```bash
python3 analysis/aoa_capture_parse.py captures/<ts>_pi-mitm_natural_payload.log
python3 analysis/gofly_transition.py   captures/<ts>_pi-mitm_natural_payload.log
```

Success signals: gvg (`cs=0/0xdd`) **answered**, I-frame bit5 set in SPECIAL packs,
video-scale bandwidth jump.
