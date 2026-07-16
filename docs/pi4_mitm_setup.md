# Raspberry Pi 4 as a USB MITM for DJI O3 — setup & capture runbook

**This is the step-by-step to follow when the Pi 4 (8GB) arrives.** It has two phases:

- **Phase A — capture the trigger (needs the phone).** Put the Pi *between* the goggles
  and the phone running DJI Fly, and record the raw USB traffic. This finally captures
  the exact live-view START command that static RE and blind live-enumeration both
  failed to find. **This is the whole reason the Pi unblocks the project.**
- **Phase B — standalone endpoint (no phone).** Once we have the trigger bytes, the Pi
  *replaces* the phone: it emulates the accessory, sends the captured trigger itself,
  receives the video, and decodes it to HDMI for all-day use.

## Why the Pi works when a laptop can't (one-paragraph recap)
O3 only serves video when the goggles are the USB **host** and the peer is a USB
**device/gadget** (the AOA "logiclink" personality — see `protocol_notes.md`). A laptop
is host-only (no UDC), so the goggles fall back to the RNDIS/telemetry personality and
never send video. The Pi 4's USB-C port has a real USB Device Controller (dwc2), so it
*can* be a device to the goggles. And because the Pi taps the traffic **on the USB
wire**, DJI Fly's `libAppGuard.so` anti-tamper — which killed every on-phone Frida
attempt — cannot see it. The Pi sidesteps the exact wall we've been stuck on.

---

## The MITM topology (Phase A)

```
  ┌────────────┐   USB-C cable        ┌───────── Raspberry Pi 4 ─────────┐   USB-A→phone cable   ┌────────────┐
  │  DJI        │   (5V wire cut!)     │  USB-C (dwc2)          USB-A     │   (Pi powers phone)   │  Phone      │
  │  Goggles 2  │═════════════════════▶│  = GADGET   ◀─usb-proxy─▶ = HOST │══════════════════════▶│  DJI Fly    │
  │  (USB HOST) │   goggles drive VBUS │  (faces goggles)   (faces phone) │   phone = USB device  │  (accessory)│
  └────────────┘                      │         ▲  raw_gadget + libusb    │                       └────────────┘
        supplies its own VBUS ─╳ cut  │         │                        │
                                      │      Pi powered from GPIO 5V / PoE│
                                      └──────────────────────────────────┘
```

- `usb-proxy` (AristoChen) presents the phone's descriptors on the dwc2 gadget port so
  the goggles think they're talking to the phone, and forwards every transfer in both
  directions. We capture the forwarded traffic with `usbmon` on the USB-A (host→phone)
  bus and pull the trigger out with `analysis/aoa_capture_parse.py`.
- The goggles supply VBUS on their USB-C output. The Pi is powered separately (GPIO/PoE)
  and must **not** be back-powered by the goggles → the 5V wire on the goggles→Pi cable
  is isolated (see Power, below). This matches CosmoStreamer's own DIY instructions
  ("cut the red 5V wire; power the Pi only from GPIO or PoE"), which is our strongest
  evidence this topology is correct.

---

## 0. Parts checklist (gather while the Pi ships)

| Item | Notes |
|---|---|
| Raspberry Pi 4 Model B 8GB | ✅ ordered |
| microSD card, 16GB+ (A1/A2, decent brand) | OS + captures. 32GB comfortable. |
| **Case + cooling** — chosen: **GeeekPi acrylic case + 30mm fan + 4 heatsinks + bundled 5V/3.6A USB-C PSU** | Open acrylic layers → **full GPIO access** (easier than the P232's ribbon slot). Simple **2-pin fan (red=5V, black=GND)** + the 4 heatsinks = all-day active cooling. Fan wiring in §4. The bundled PSU is the **first-boot/bench** supply (see §1). The old P232 is now spare / backup bench box. |
| **Pi power — GPIO via a PD trigger** — chosen: **Adafruit HUSB238** (switchable) + a **2-pin DuPont pigtail** | USB-C is the goggles data link, so power the Pi via **GPIO pin 4 (5V) + pin 6 (GND)**. Chain: `USB-C PD source → HUSB238 (DIP set to 5V, tape the switch) → its 2-pin screw terminal → pigtail → GPIO 4/6`. The HUSB238's USB-C input is the common port. **No barrel, no breakout, no fuse** (rely on the PD source's built-in OCP/SCP). |
| **A USB-C PD power source** (5V/3A) | The *running* supply: a **USB-C PD power bank** (must be a genuine PD source). ⚠️ HUSB238 needs a **PD** source — a normal USB-C **PD charger** works as future mains, but neither the official RPi PSU nor the bundled 5V/3.6A PSU do (fixed 5V, not PD → the HUSB238 can't negotiate them). PD's 5V ≈ 5.0V (Pi 4 prefers 5.1V) → keep the pigtail short/thick; watch `vcgencmd get_throttled`. |
| **A cheap DC voltmeter** (~£9.49 basic multimeter) | ⚠️ Safety-critical, not optional. Before wiring HUSB238→GPIO, verify **~5.0–5.2V and correct polarity** at the terminal block. Catches the two instant-kill mistakes: DIP not on 5V (overvoltage) and VBUS/GND swapped (reverse polarity). |
| **VBUS isolator for the goggles→Pi USB-C link** — chosen: **PortaPow USB _Power_ Blocker** (blocks 5V, passes data; NOT the *Data* blocker) | The #1 hardware gotcha — see the VBUS section (§4) for the full why. PortaPow's power blocker is USB-A form, so cable it: goggles-C → C-to-A(host) adapter → PortaPow → A-to-C → Pi-C (all USB 2.0, which is fine). All-USB-C solder alt: two **SparkFun USB-C Breakout** boards back-to-back with `VBUS` left unconnected. |
| USB-C→USB-C cable (goggles → Pi USB-C), data-capable + the C-to-A / A-to-C adapters for the blocker | The link you isolate VBUS on. |
| USB-A→(phone connector) cable (Pi USB-A → phone) | For Phase A only. Pi supplies VBUS here — that's fine. |
| Display — **micro-HDMI → full-size-HDMI cable** to an **Atomos Shinobi 5″ field monitor** | Phase B output. Pi 4 = **micro**-HDMI (use HDMI0, nearest USB-C — careful, *micro* not *mini*); Shinobi = full-size HDMI-in, **self-powered** (own NP-F battery, not on the Pi rail). Full-bitrate, ~1-frame latency, no WiFi; tripod-mount it beside the Pi case. DSI/dual-output deferred — the 2nd micro-HDMI can drive a front-of-house display later if wanted. |
| The rooted S21 Ultra capture phone | Already prepped (see `capture-phone-rooted` memory). Phase A only. |

---

## 1. Flash Raspberry Pi OS (headless)

Use **Raspberry Pi OS (64-bit)**. **Lite** is enough for Phase A and for a headless
all-day Phase B (we render straight to KMS/DRM, no desktop needed); pick the full
desktop image only if you want a GUI.

With **Raspberry Pi Imager** on the laptop:
1. Choose OS → Raspberry Pi OS (64-bit) Lite. Choose your microSD as storage.
2. Click the ⚙️ / "Edit settings" before writing and set:
   - **hostname**: `o3pi`
   - **Enable SSH** → use password or (better) your public key
   - **username/password**: set your own (note them)
   - **WiFi**: your network + country (so you can SSH in headless)
3. Write, then boot the Pi with the microSD (power via the **bundled 5V/3.6A USB-C PSU
   for first boot / bench setup only** — the USB-C data role is free until the goggles
   are connected; once they are, switch to GPIO power per §4).
4. From the laptop: `ssh <user>@o3pi.local` (or find the IP via your router / `nmap -sn`).

## 2. Copy this repo's helper files onto the Pi

From the laptop, in the repo root:
```bash
# just the small text/code we need on the Pi (not the big APK/firmware blobs):
rsync -av --exclude='*.zip' --exclude='.venv' --exclude='analysis/dji_fly_apk' \
      --exclude='handover*' scripts poc analysis docs README.md CLAUDE.md \
      <user>@o3pi.local:~/o3repo/
```

## 3. Run the bring-up script (on the Pi)

```bash
ssh <user>@o3pi.local
cd ~/o3repo
chmod +x scripts/pi_setup.sh
./scripts/pi_setup.sh
```
It installs deps, enables the dwc2 USB-C gadget controller
(`dtoverlay=dwc2,dr_mode=peripheral`), and builds **raw-gadget** + **usb-proxy** under
`~/o3`. **First run asks for a reboot** (to activate dwc2): `sudo reboot`.

After reboot, verify the gadget controller and kernel module exist:
```bash
ls /sys/class/udc/         # expect: fe980000.usb
ls -l /dev/raw-gadget      # expect: the char device
```
If `/sys/class/udc/` is empty, dwc2 didn't come up — re-check the config line and see
Troubleshooting.

## 4. Power & VBUS isolation (do this before plugging in the goggles)

**Power the Pi from GPIO — never from USB-C now** (USB-C is the gadget data link to the
goggles). Chain: `USB-C PD source → Adafruit HUSB238 (DIP set to 5V) → its 2-pin screw
terminal → 2-pin DuPont pigtail → GPIO pin 4 (5V) + pin 6 (GND)`. The HUSB238's USB-C
input is the common port: a **USB-C PD power bank** now, a **USB-C PD charger** later
(⚠️ *not* the official RPi PSU — it's fixed 5.1V, not PD, so the HUSB238 can't negotiate).
No barrel, no breakout, no fuse (the PD source provides overcurrent/short protection).

⚠️ **GPIO 5V has no fuse or reverse-polarity protection, and a Pi 4 dies above 5.25V** —
two mistakes kill it instantly with no warning: (1) the HUSB238 DIP not actually on 5V
(overvoltage); (2) VBUS/GND swapped (reverse polarity). **Before wiring to GPIO, meter the
HUSB238 terminal block: expect ~5.0–5.2V, no minus sign.** Then set-and-forget: tape the
DIP at 5V; wire `VBUS→pin 4`, `GND→pin 6`. PD's 5V ≈ 5.0V (Pi 4 prefers 5.1V) → keep the
pigtail short/thick and check `vcgencmd get_throttled` (want `0x0`).

**GPIO pin map (power pigtail + the GeeekPi 30mm fan share the 5V rail):**
```
   Pin 1 (3V3)   ▮ ▮   Pin 2 (5V)  ◀── FAN red
   Pin 3         ▮ ▮   Pin 4 (5V)  ◀── POWER pigtail 5V (from HUSB238)
   Pin 5         ▮ ▮   Pin 6 (GND) ◀── POWER pigtail GND
   Pin 9 (GND) ─────────────────────── FAN black
```
Your supply feeds pin 4 → the whole 5V rail goes live → the fan on pin 2 runs off the
same source (only when your supply is connected). Keep the two on **different** pins so
they don't collide. If the fan is a rigid 2-pin block that only fits 4+6, flip it (fan on
4+6, split your power leads onto pin 2 + pin 9).

*(Optional dual-input — not yet ordered:* to also power from a **non-PD** 5V supply, add a
second branch to a **USB-A socket** and select between it and the HUSB238 with a
**centre-off DPDT switch** so both are never live at once. See the hardware memory.)

**Isolate VBUS on the goggles→Pi USB-C link** so the goggles' 5V can't fight/back-feed
the Pi's GPIO 5V rail (two supplies on one node = back-current + brown-out; this is
CosmoStreamer's "cut the 5V wire"):
- **Chosen: PortaPow USB _Power_ Blocker** (blocks 5V, passes D+/D-/GND) — the *Power*
  blocker, **not** the *Data* blocker. It's USB-A form, so cable it:
  `goggles-C → C-to-A(host) adapter → PortaPow → A-to-C cable → Pi-C`. All USB 2.0, which
  is exactly what the dwc2 gadget and the goggles negotiate anyway — no loss.
- All-USB-C alternative (a little soldering): two **SparkFun USB-C Breakout** boards
  back-to-back, wiring `GND`/`D+`/`D-`/`CC` and leaving **`VBUS` unconnected**.
- Because `dr_mode=peripheral` is *forced*, the dwc2 UDC keeps its data-line pullup on
  regardless of VBUS sensing, so the goggles should still enumerate the Pi with VBUS
  removed (the CosmoStreamer-proven arrangement).
- **Fallback if — and only if — the goggles won't enumerate the Pi with VBUS removed:**
  restore a VBUS *sense* path via a Schottky diode (goggles→Pi) so the Pi sees ~4.7V but
  reverse current is blocked. Try the clean removal first.

### ✅ First bring-up checkpoint (do this before any capture)
With the Pi powered from GPIO and the VBUS-isolated cable in hand, plug the goggles into
the Pi USB-C and confirm the Pi **enumerates as a device to the goggles**:
```bash
dmesg -w        # watch for the goggles' host driving enumeration of our gadget
ls /sys/class/udc/                       # fe980000.usb present
cat /sys/class/udc/fe980000.usb/state    # look for 'configured' once usb-proxy runs
```
If nothing happens on connect, VBUS-removal killed host detection → apply the Schottky
fallback above. **Don't move on to the capture until this checkpoint passes** — it's the
single most likely first-contact snag.

⚠️ Do not simultaneously feed the Pi 5V from two sources with no isolation — that's how
you brown-out or damage the Pi. GPIO/PoE in, USB-C VBUS out.

---

## Phase A — capture DJI Fly's live-view trigger

Goal: one clean `.pcapng` covering **connect → DJI Fly shows video → ~30s of video →
disconnect**, then run the analyser to read out the trigger command.

### A.1 Wire it up
1. Pi powered from GPIO/PoE, booted, SSH in.
2. Phone (DJI Fly installed, screen unlocked) → **Pi USB-A** port.
3. Goggles (powered, linked to the air unit, **showing live video**) → **Pi USB-C**
   (the VBUS-isolated cable). Don't plug the goggles in until step A.3 says so.

### A.2 Start the capture plumbing (on the Pi, in a first SSH session)
```bash
sudo modprobe usbmon
# find which usbmon bus is the USB-A host the phone is on:
lsusb -t                       # note the bus number of the phone's xHCI host
sudo tshark -D | grep usbmon   # maps usbmonN to buses
# capture that bus (usbmon<BUS>), append-only filename with date + firmware:
mkdir -p ~/o3repo/captures
sudo tshark -i usbmon1 -w ~/o3repo/captures/$(date +%F)_pi-mitm_fwXXXXX.pcapng
```
(Replace `usbmon1` with the phone's bus, and `fwXXXXX` with the goggles firmware from
the on-screen menu. **Never overwrite a capture** — captures/ is append-only.)

### A.3 Start the proxy + the session (second SSH session)
```bash
cd ~/o3
# find the phone's current VID:PID:
lsusb
# start the proxy: gadget side = the dwc2 UDC, host side = the phone by VID/PID
sudo ./usb-proxy/usb-proxy --device=fe980000.usb --driver=fe980000.usb \
     --vendor_id=<PHONE_VID> --product_id=<PHONE_PID> -v \
     |& tee ~/o3repo/captures/$(date +%F)_pi-mitm_usbproxy.log
```
Then **plug the goggles into the Pi USB-C**. Watch for the phone to switch into DJI-Fly
accessory mode and DJI Fly to launch showing the feed. Let it stream ~30s, then stop
(`Ctrl-C` the proxy, then the tshark).

> **Re-enumeration note (the thing most likely to need debugging):** when the AOA
> handshake completes, the phone drops its current VID/PID and reappears as
> `18d1:2d0x`. usb-proxy may or may not follow that mid-session switch cleanly. If the
> proxy loses the device at that moment, use the **pre-trigger fallback** (A.4).

### A.4 Fallback: pre-trigger the phone into accessory mode first
If the mid-session re-enumeration breaks the proxy, put the phone into DJI-Fly accessory
mode **before** starting the proxy, so the proxy only ever sees the stable `18d1:2d0x`:
```bash
lsusb                                            # phone's normal VID:PID
# needs pyusb: `sudo apt install python3-usb` (pi_setup.sh already did this)
sudo python3 ~/o3repo/poc/pi_mitm/aoa_trigger.py --vid 0x<VID> --pid 0x<PID>
# (installs the DJI identity strings; phone re-enumerates as 18d1:2d0x, DJI Fly launches)
lsusb                                            # confirm 18d1:2d0x now present
# now start usb-proxy against the accessory PID:
sudo ./usb-proxy/usb-proxy --device=fe980000.usb --driver=fe980000.usb \
     --vendor_id=18d1 --product_id=2d0x -v |& tee .../usbproxy.log
# then plug in the goggles
```
`aoa_trigger.py` needs pyusb (`python3-usb`, installed by `pi_setup.sh`). Running it
**on its own** (no goggles, no proxy) is also a good first sanity check that the Pi can
drive the phone into DJI-Fly mode at all.

### A.5 Analyse the capture (on the Pi or back on the laptop)
```bash
python3 analysis/aoa_capture_parse.py captures/<the>.pcapng
# or, if usbmon capture was awkward, feed the proxy's verbose byte log:
python3 analysis/aoa_capture_parse.py captures/<the>_usbproxy.log
```
It reassembles the AOA framing (`55 CC 30 75` + LE32 len + DUML), parses every DUML
packet (CRC-validated via `analysis/duml.py`), finds the moment bandwidth jumps to
video scale, and prints the **app→goggles (DUML sender=MOBILE_APP=2) command(s) sent
just before the jump** — the trigger. Record the exact `cmd_set` / `cmd_id` / `receiver`
/ `payload` in `protocol_notes.md`.

✅ **Success = a specific DUML command identified as the thing DJI Fly sends immediately
before video starts.** That's the answer three static-RE rounds and 750 live probes
couldn't get.

---

## Phase B — standalone Pi endpoint (no phone), → HDMI

Once the trigger is known, we don't need the phone or DJI Fly again. The Pi becomes the
accessory itself. Scaffold + plan live in `poc/pi_endpoint/` (built with clearly-marked
TODOs where the captured trigger bytes and the exact video-stream framing plug in —
nothing fabricated). Outline:
1. Pi in gadget mode presents the AOA accessory to the goggles (same dwc2 UDC, but now
   *we* are the endpoint, using FunctionFS/raw-gadget to expose the accessory bulk
   endpoints and answer the goggles' AOA handshake with the DJI identity strings).
2. Replay the captured handshake + **send the trigger** from A.5.
3. Read the video bulk stream, strip the AOA/DUML framing, recover the elementary
   stream (H.264 or H.265 — verify from the capture; DVR is H.264 but live is unconfirmed).
4. Decode → **micro-HDMI (HDMI0) → Atomos Shinobi** field monitor (single HDMI output for
   now). Validate with `mpv --vo=drm -` or `ffplay -`; for all-day, tune Pi 4 hardware
   decode (V4L2 M2M / DRM) so it doesn't cook. (Later options: 2nd micro-HDMI for a
   front-of-house display via `GStreamer tee → 2× kmssink`; WiFi restream as backup.)

Only start Phase B after Phase A gives a confirmed trigger — until then the endpoint has
nothing to send.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `/sys/class/udc/` empty after reboot | dwc2 overlay not active. Check `dtoverlay=dwc2,dr_mode=peripheral` in `/boot/firmware/config.txt`; ensure no `otg_mode=1` line overrides it; reboot. |
| `/dev/raw-gadget` missing | `sudo modprobe raw_gadget` (built by `pi_setup.sh`); if modprobe fails, kernel headers didn't match — re-run `pi_setup.sh` after a full `apt full-upgrade` + reboot so headers and running kernel align. |
| Goggles don't enumerate the Pi at all | VBUS fully cut but forced-peripheral not asserting pullup: try the VBUS-pass-with-diode fallback (§4). Also confirm the USB-C cable carries D+/D- (some charge-only cables don't). |
| Proxy loses the phone when DJI Fly launches | The re-enumeration issue — use the A.4 pre-trigger fallback. |
| `openAccessory` / DJI Fly won't start on the phone | Phone screen must be unlocked; accept the "Open DJI Fly for this USB accessory?" dialog (tick "always"). If our old `aoa_sniffer` app is installed it may grab the accessory instead — uninstall it or don't pick it in the chooser. |
| usbmon shows nothing | Capturing the wrong bus. `lsusb -t` → find the phone's host controller, match it to `usbmonN` via `tshark -D`. The phone is on the USB-A (xHCI) side; the dwc2 gadget side is **not** visible to usbmon — always capture the USB-A bus. |
| Pi throttles / feed stutters (Phase B) | `vcgencmd measure_temp` (thermal — confirm the 30mm fan is spinning; it rides the GPIO 5V rail so it only runs when your supply is on) and `get_throttled` (undervolt — likely PD's 5.0V sagging under load → shorten/thicken the pigtail or move to a 5.1V source). |

## References
- usb-proxy — https://github.com/AristoChen/usb-proxy (raw-gadget + libusb USB MITM)
- raw-gadget — https://github.com/xairy/raw-gadget (`/dev/raw-gadget`, Pi setup doc)
- Pi OTG whitepaper — Raspberry Pi "Using OTG mode on Raspberry Pi SBCs"
- CosmoStreamer O3 DIY (topology corroboration) — cosmostreamer.com/products/djigoggles2/diy
