## Dual HDMI + presentation quality (2026-07-13 evening)

- Live USB ES measured **H.264 High 1920×1080 @ ~60 fps, ~7 Mbit/s** (gold + standalone
  captures agree). Earlier "~27–30 Mbit/s" was USB bulk framing rate, not ES bitrate.
  Presentation polish cannot invent missing encode detail.
- **Single HDMI:** mpv zero-copy path + GLSL `ewa_lanczossharp` / mild deband / sharpen.
- **Dual HDMI:** GStreamer `decodebin ! tee ! kmssink×2` (one process). Two mpv cannot
  both be DRM master. `--drm-connector=auto` + ~2s hotplug watcher.
- Service default `DRM_CONNECTOR=auto`. Deploy: `scripts/push_to_pi.sh` then
  `sudo ~/o3test/scripts/install_hdmi_service.sh`.

## USB live-view bitrate hunt (2026-07-13 night) — likely product ceiling

**Question:** can the AOA/USB consumer path exceed ~7 Mbit/s H.264 ES?

**Tool:** `analysis/bitrate_hunt.py` over full gold MITM payload
(`captures/pi_pull/2026-07-13_103110_pi-mitm_natural_payload.log`, ~87s, phone FPV OK).

### Measured ES (not bulk framing)

| Source | File | Duration | ES |
|---|---|---|---|
| Phone path (gold extract) | `video_extract/hardened.from_sps.h264` | ~42.0 s | **~6.94 Mbit/s** |
| Phase B standalone | `…_phaseB_STANDALONE_clean.h264` | ~25.7 s | **~6.91 Mbit/s** |

Phone-with-DJI-Fly and standalone replay land on the **same** encode rate (±0.03).
Missing arming bytes are not the reason standalone looks soft.

### Gold full-log DUML (beyond 6s `gold_app_seq` window)

| Finding | Detail |
|---|---|
| **App→goggles HDLnk (cs=9) REQs** | **0** in entire working FPV session |
| Device→app HDLnk | Telemetry only: `9/0x08` (air+goggles), `9/0x75`, `9/0x43`, rare `9/0x11` |
| Focus app SETs (cs=2/7/8/9) | All first-seen in **first ~2.5 s** arming; then only `2/0xeb` + `2/0xe8` |
| Late “new” uniques | Almost all churn: cs=17 `0x43`/`0x4a`, FC `3/0x20`, file `0/0x2a` — not quality SETs |
| `2/0xeb` payload | Subscribe/keepalive; byte at offset 2 rotates; fixed tail `…00030008007517d107` after channel-3 form |
| `2/0xe8` | Empty-payload camera keepalive (~2 Hz), not a config SET |
| `2/0x80` | Camera→app status push 60 B, fields **constant** for whole capture (no mid-session mode change) |

**Conclusion from gold:** DJI Fly does **not** send a post-arm bitrate/quality SET on the
USB path. Steady state is keepalives + telemetry. Prior blind HDLnk `9/0x34` / `9/0x39`
accepts (`0x00`) never started video and are **not present** in a working phone session
either — low EV as a “raise bitrate” lever without a new capture that actually shows them.

### External corroboration (CosmoStreamer)

CosmoStreamer (commercial Goggles 2 USB→HDMI product) has stated publicly that
**Goggles 2+ USB video out is ~8–10 Mbps and “far too low”** for impressive picture —
same order of magnitude as our ~7 Mbit/s ES. That is independent evidence the
**consumer USB encode is product-capped**, not something our gold_app_seq is missing.

Onboard DVR still does **~18–26 Mbit/s** H.264 (different pipeline; SD card). Radio link
can also be much fatter. Neither is the AOA phone/USB consumer encode.

### Remaining experiments (optional, low EV / needs hardware)

1. **Goggles UI toggle while MITM recording** — if any transmission/liveview quality
   control exists, flip it mid-session; re-run `bitrate_hunt.py --diff` + measure ES.
   (O3 “channel quality” may only affect RF, not USB re-encode.)
2. **Post-stream HDLnk inject** only if a capture shows a new payload; do not spam
   fabricated `9/0x39` without a delta.
3. Treat **~7 Mbit/s @ 1080p60** as the field baseline for open-source USB out unless
   (1) produces a higher ES.

```bash
python3 analysis/bitrate_hunt.py \
  captures/pi_pull/2026-07-13_103110_pi-mitm_natural_payload.log \
  --es captures/pi_pull/video_extract/hardened.from_sps.h264
```

# Protocol notes — DJI Goggles 2 (O3) USB/RNDIS video-out

This is the living doc of what we've actually confirmed on our hardware vs. what's
still assumed from community research. Update it as each phase in CLAUDE.md lands
results. This doc is the contribution — keep it accurate.

## Hardware identity (CONFIRMED 2026-07-03)
- USB: `2ca3:0020 DJI Technology Co., Ltd. e3t`, USB 2.10, High Speed (480 Mbps),
  bus-powered. Full descriptor dump: `docs/lsusb_dji_e3t.txt`.
- **The device is a Linux USB composite gadget**, not a bespoke protocol stack. The
  `iConfiguration` string is literally `rndis,mass_storage,bulk,acm` — the standard
  naming pattern for a Linux configfs/functionfs gadget. This means the goggles run
  embedded Linux and expose their functions as generic gadget endpoints.
- 8 USB interfaces total:
  | # | Class | Function | Endpoints | Kernel driver bound |
  |---|---|---|---|---|
  | 0 | Wireless/RF, RNDIS | RNDIS control | 1x interrupt IN (8B) | `rndis_host` |
  | 1 | CDC Data | RNDIS Ethernet Data | bulk IN/OUT (512B) | `rndis_host` |
  | 2 | Mass Storage, SCSI, Bulk-Only | `File-Stor_Gadget` (SD card) | bulk IN/OUT (512B) | `usb-storage` |
  | 3 | Vendor-specific (0xFF, subclass 67) | "BULK Interface" #1 | bulk IN(EP4)/OUT(EP3), 512B | **none — free for libusb** |
  | 4 | Vendor-specific (0xFF, subclass 67) | "BULK Interface" #2 | bulk IN(EP5)/OUT(EP4), 512B | **none — free for libusb** |
  | 5 | Vendor-specific (0xFF, subclass 67) | "BULK Interface" #3 | bulk IN(EP6)/OUT(EP5), 512B | **none — free for libusb** |
  | 6 | Vendor-specific (0xFF, subclass 67) | "BULK Interface" #4 | bulk IN(EP7)/OUT(EP6), 512B | **none — free for libusb** |
  | 7 | Vendor-specific (0xFF, subclass 67) | "BULK Interface" #5 | bulk IN(EP8)/OUT(EP7), 512B | **none — free for libusb** |

**NEW LEAD, not in original plan:** CLAUDE.md assumed the old V1/V2 raw-bulk-endpoint
method "does NOT apply" to O3. That's true for the *specific* fpv-wtf magic-number
protocol, but this hardware still exposes 5 unclaimed vendor-specific bulk pipes
(interfaces 3–7) with no kernel driver attached.

**Probed read-only 2026-07-03** (claimed each interface with pyusb, listened on the
bulk IN endpoint only — no writes to any OUT endpoint):
- **Interface 4 (IN ep 0x85): active, unsolicited.** Emits a small fixed-format packet
  roughly twice a second, 14 bytes, e.g. `55 0e 04 66 0e 2a 3a 01 00 09 43 03 4c cc` —
  constant 6-byte header (`55 0e 04 66 0e 2a`), then a byte that increments each packet
  (0x3a, 0x3b, 0x3c, ...) — a sequence counter, then a constant 5-byte tail (`01 00 09
  43 03`), then 2 bytes that change each time (likely checksum/CRC over the payload).
  Reads as a **heartbeat/keepalive**, not video (14 bytes, ~2 Hz — nowhere near video
  bitrate). This is a completely separate low-level heartbeat from the RNDIS-side
  telemetry ports (8901-8904) — the device is alive/pinging on multiple layers at once.
- **Interfaces 3, 5, 6, 7: silent** — no unsolicited data in a 6s listen window on their
  IN endpoints. They may only respond to a specific OUT write, or be reserved/inactive
  in the current mode. **Did not write to any OUT endpoint** — sending arbitrary bytes
  to an unknown vendor-specific interface on this hardware is exactly the kind of
  action to clear with the hardware owner first (unknown blast radius — could be a
  firmware/bootloader/diagnostic path). Next step if we want to keep pulling this
  thread: try a plausible query on the OUT endpoint (e.g. mirror what the RNDIS-side
  888xx telemetry connections looked like) only with explicit go-ahead.

**Protocol identified: this is DUML** (DJI Universal Message Language — DJI's internal
system-bus protocol, partially documented by the open-source `o-gs/dji-firmware-tools`
project). Verified byte-exact: computed CRC8 (seed 0x77) and CRC16 (seed 0x3692) over
the captured interface-4 packets match the transmitted checksums exactly, for all 3
samples captured. Decoded header of the heartbeat packet:
`55 0e 04 66 0e 2a 3a 01 00 09 43 03 4c cc` →
version=1, length=14, sender_type=14, receiver_type=10/idx=1, seq=314 (increments per
packet), packet_type=REQUEST, cmd_set=9 (**OFDM** — the O3 image-transmission/link
module), cmd_id=0x43, payload=`03`. Confirms this bulk pipe taps live internal DUML bus
traffic to/from the OFDM (video link) subsystem specifically — a very promising thread,
though this particular packet is a low-rate heartbeat, not the video payload itself.

A reusable, tested codec now lives at `analysis/duml.py` (CRC tables + `build()` /
`parse()` / `scan()`), and `analysis/pcap_harness.py` now scans every top conversation
in a capture for embedded DUML packets automatically, in addition to the NAL-code scan.

**Tested, did NOT find DUML on the network side (2026-07-03):**
- Sent a well-formed, standard, read-only DUML `GENERAL/VersionInquiry` request
  (cmd_set=0, cmd_id=0x01, receiver=ANY) to TCP port 1415 — no response within 3s.
  Either 1415 isn't a DUML endpoint, needs a different receiver_type/index than `ANY`,
  or needs a session/auth step first.
- Captured ~8KB from each of 8901/8902/8903/8904 and ran `duml.scan()` (CRC-validated,
  not just a magic-byte guess) over each — **zero valid DUML packets found in any of
  them.** These four ports are running a different, still-unidentified framing.
  (First bytes don't start with `0x55` and no valid packet was found at any offset,
  so it's not just a stream-alignment issue — this really looks like a separate
  protocol from DUML.)

## Onboard SD card (mass storage interface, CONFIRMED)
- Interface 2 enumerates as `/dev/sdX`, `ID_MODEL=File-Stor_Gadget`, exFAT, label
  `SD_Card`. No partition table — whole-disk filesystem (standard for DJI SD cards).
- Contents: `DCIM/100MEDIA/DJI_00XX.MOV` (502 files, ~66GB on the card we checked) +
  `MISC/dji_finfo.db` (sqlite, likely media index) + `MISC/THM` (thumbnails).
- **DVR recording codec is H.264, not H.265** (checked several files via `ffprobe`):
  1920x1080, 50/60fps, ~18-26 Mbit/s, `codec_name=h264`. This does NOT necessarily mean
  the live low-latency USB video-out stream is also H.264 — the onboard DVR and the O3
  live transmission are plausibly different encode paths — but it's a real discrepancy
  from CLAUDE.md's assumed-H.265 fact and worth re-checking once we capture the live
  stream itself. **Do not assume live == H.265 anymore; verify directly in Phase 2.**
- This mass-storage path is a much easier way to pull `MISC/dji_finfo.db` or look for
  firmware/config files than the FTP side-quest — worth a look before spending time on
  FTP credential recovery.
- Mount read-only only (`mount -o ro`) — this card holds the owner's real flight
  footage; treat as personal data, don't need to enumerate/view video content itself
  beyond technical metadata.

## Network setup (CONFIRMED 2026-07-03, on Dell Latitude recon rig)
| Item | Status | Value |
|---|---|---|
| USB interface type | CONFIRMED | RNDIS (`rndis_host` driver, `enx<mac>` naming) |
| Host subnet | CONFIRMED (this session) | **192.168.60.0/24** — NOT 192.168.42.x as historically assumed elsewhere. Subnet may be per-device/per-firmware; don't hardcode 42.x. |
| Host IP | CONFIRMED | 192.168.60.1 (DHCP-assigned by goggles) |
| Goggles IP | CONFIRMED | 192.168.60.2 |
| Address assignment | CONFIRMED | DHCP, served by goggles, ~4s lease negotiation |
| Gotcha | CONFIRMED | Host may have multiple `usb*/enx*` interfaces (stale ones from unrelated devices) — must pick the one that's actually `LOWER_UP`, not just the first match. Fixed in `phase0_recon.sh`. |
| Gotcha | CONFIRMED | Goggles does not respond to ICMP ping on this link even when up — use ARP/nmap `-sn`, not `ping`, for host discovery. |

## Open ports (USB/RNDIS link) — CONFIRMED via full TCP scan + top-100 UDP scan
| Port | Service (wifi scan label) | Present on USB link? |
|---|---|---|
| 21 | FTP | **YES** — confirmed open, banner `220 Hello!` |
| 1415 | dbstar | **YES** — confirmed open, but silent: doesn't send a banner and doesn't respond to naive text/null probes. Needs the real DJI binary handshake (Phase 1 capture required). |
| 1561 | facilityview | **NO** — open on wifi scan per community notes, but NOT present on the USB/RNDIS link on this hardware. Confirms CLAUDE.md's suspicion that wifi vs USB port sets can differ. |
| 8901 | jmb-cds2 | **YES** — open, pushes data unsolicited immediately on connect (see below) |
| 8902 | unknown | **YES** — same behavior |
| 8903 | unknown | **YES** — same behavior |
| 8904 | unknown | **YES** — same behavior |
| UDP top-100 | — | none showed as definitively `open` (all `open\|filtered` or `closed`) — inconclusive, a stateless UDP video stream wouldn't necessarily respond to nmap's probe anyway. |

## Ports 8901-8904: unsolicited push channels, CONFIRMED not the video flow
Surprising and useful finding: connecting to any of 8901/8902/8903/8904 with a bare TCP
socket — **no handshake, no request sent** — immediately gets a stream of structured
binary data pushed at us. Looks like small fixed/semi-fixed-size records with
incrementing counters (telemetry/OSD/state-ish), not compressed video (no entropy
profile of compressed H.26x, no NAL start codes found in a quick scan of the payload).

Throughput measured over 5s per port (rules these out as the ~20-30Mbit/s video flow):
| Port | Bytes/5s | Throughput |
|---|---|---|
| 8901 | 10,240 | ~0.016 Mbit/s |
| 8902 | 61,440 | ~0.090 Mbit/s |
| 8903 | 381,176 | ~0.588 Mbit/s (highest of the four — maybe a richer telemetry/log stream) |
| 8904 | 51,928 | ~0.080 Mbit/s |

None are remotely close to video bitrate. **Conclusion: the actual video flow is
gated behind a start-stream trigger** (as CLAUDE.md's Phase 2 plan already assumed) —
it is not one of these always-on push channels. These four ports are more likely
telemetry/OSD/logging/status channels that run continuously regardless of streaming
state. Worth capturing/decoding on their own merits (flight telemetry without needing
the video handshake at all) but they are a side quest, not the main video target.

Next step to find the real video port: **Phase 1 capture is required** — start a real
DJI Fly session and watch for a new port/connection that lights up only once "start
streaming" happens, or watch 1415/890x for a request right before a NEW high-bandwidth
flow appears.

## FTP (port 21)
- Banner: `220 Hello!`
- Anonymous login: rejected (`530 Login failed`).
- Tried a short list of obvious default creds (root/root, root/blank, dji/dji,
  admin/admin, guest/guest, root/Cypress, root/DJI123456) — all rejected. Not pursuing
  further brute-force; real creds most likely surface in a Phase 1 capture or in
  firmware/config strings, not by guessing. Deprioritized in favor of the mass-storage
  path above for pulling files off the device.

## Control channel
- Port: still TO VERIFY which of 1415/890x (if any) is actually "control" for video —
  890x now look more like telemetry pushes than a request/response control channel.
  1415 is silent until spoken to correctly — the more likely control-channel candidate.
- Handshake / stream-start trigger: TO VERIFY (needs Phase 1 capture)
- Challenge/response auth present?: TO VERIFY
- Keepalive cadence + payload: TO VERIFY

## Video channel
- Port: TO VERIFY — ruled out 8901-8904 as continuously-streaming video (see above);
  most likely appears dynamically after a stream-start request, possibly even on a
  port not in the known set. Needs Phase 1 capture to identify.
- Direction: goggles -> host (one-way, high bandwidth) — expected, not yet confirmed
- Framing: TO VERIFY — raw Annex-B NAL start codes vs. length-prefixed vs. RTP-like vs.
  custom wrapper. Use `analysis/pcap_harness.py` on a real capture to check.
- Codec: TO VERIFY directly — do NOT assume H.265 anymore. The onboard DVR recording
  (a different pipeline) is confirmed H.264; the live O3 transmission may or may not
  match. Check VPS/SPS/PPS (HEVC) vs SPS/PPS (AVC) NAL types once captured.
- Bitrate: DVR recording measured at ~18-26 Mbit/s on this hardware (not necessarily
  the same as the live transmission bitrate, but a reasonable ballpark).

## Firmware
- Goggles firmware version at time of this recon: not yet read off the device (check
  goggles on-screen menu or `MISC/dji_finfo.db` on the SD card next time it's mounted).

## Also worth investigating (not yet started)
- Interfaces 3, 5, 6, 7 (vendor bulk) — silent without a write; need a deliberate,
  approved probe on the OUT endpoint to see if they respond.
- Whether DUML flows over 1415 with a different receiver_type/index, or requires a
  prior session-setup step (e.g. a magic byte, or auth, before it'll parse a DUML frame).
- What protocol 8901-8904 actually use, since it's confirmed NOT DUML.
- `MISC/dji_finfo.db` on the SD card — sqlite file, unopened so far.
- Whether cmd_set=OFDM traffic (confirmed on USB interface 4) also appears, in the same
  DUML framing, inside the eventual Phase 1 video-flow capture — would help correlate
  the two transports.

## MAJOR REVISION 2026-07-04: the phone/DJI Fly path is NOT RNDIS at all

Everything in the sections above was recorded with the goggles plugged into a **Linux
PC acting as USB host** — that's one valid mode of this hardware, but it is **not** how
DJI Fly actually talks to the goggles. Confirmed today by plugging the goggles into an
Android phone (ASUS ASUS_I006D) running DJI Fly, with wireless `adb` open to inspect
what happened on the phone side (no root used/needed — this is all standard Android
diagnostics):

- `adb shell dumpsys usb` showed: `current_functions=ACCESSORY`,
  `current_accessory={ manufacturer=DJI, model=com.dji.logiclink, description=DJI glass,
  version=v0.0.0.0, uri=www.dji.com }`, `host_connected=false`.
- **The goggles act as the USB HOST here, and the phone is the USB peripheral**, using
  **Android Open Accessory (AOA)** protocol — the exact opposite USB role arrangement
  from the Linux-PC-as-host session. This is a dual-role/OTG-capable device: it behaves
  differently depending on what it's plugged into.
- `adb logcat` confirmed the flow: goggles attach → Android's kernel switches its USB
  gadget into `accessory` function via configfs → Android fires
  `USB_ACCESSORY_ATTACHED` → launches **`dji.go.v5` (DJI Fly's package name)** directly
  into `com.dji.component.application.activity.DJIAoaActivity` → ~5s later,
  `com.dji.component.fpv.FpvComponentActivity` starts (**Fpv = First Person View** —
  almost certainly the live video component).
- No IP network interface of any kind appears on the phone for this connection (checked
  via `adb shell ip addr` before/after — identical). **This fully explains why an
  earlier PCAPdroid capture found zero packets to any 192.168.x.x address**: there was
  never an IP layer to capture. PCAPdroid (or any network-layer capture tool) cannot
  see this traffic — it isn't IP traffic. That capture attempt is dead-ended, not a
  misconfiguration.
- This means: RNDIS + the 5 vendor bulk USB interfaces + DUML-over-USB-interface-4
  (all confirmed in the sections above) is a **separate, secondary personality** this
  hardware exposes to a generic PC host — useful for diagnostics/recon, but **not** the
  path DJI Fly / the real video pipeline uses. The real video goes over the AOA
  accessory pipe (`/dev/usb_accessory` on the Android side), likely still DUML-framed
  underneath (same "logiclink" bus concept — everything on this device seems to speak
  DUML at some layer) but wrapped in Google's AOA protocol instead of IP or a raw
  vendor-class USB descriptor.

**Practical consequence:** capturing this needs either (a) root on the Android phone to
read `/dev/usb_accessory` directly (e.g. via `su -c strace` or a kernel-level tap), or
(b) a small custom Android app that also declares an AOA intent-filter matching
`manufacturer=DJI, model=com.dji.logiclink` — Android will offer a chooser between it
and DJI Fly when the goggles attach, and our app can open the accessory fd itself and
log every byte to a file. Option (b) sacrifices DJI Fly's own video display for the
duration of that capture (only one app can hold the accessory fd at a time) but needs
no root and is a small, well-understood technique (this is exactly how car-head-unit/
IoT integrators normally reverse-engineer AOA protocols). Not yet built — next step to
discuss with the hardware owner before investing the build effort.

## AOA/logiclink protocol — fully framed, live command round-trip confirmed (2026-07-04)

Built `poc/aoa_sniffer/` — a small sideloadable Android app (no root, manual aapt2/d8/
apksigner build pipeline, no Gradle — see `poc/aoa_sniffer/build.sh`) that registers the
same AOA intent-filter as DJI Fly (`manufacturer=DJI, model=com.dji.logiclink`). Android
offers a chooser between it and DJI Fly on connect; picking our app hands us the raw
accessory pipe (sacrificing DJI Fly's own video for that session).

### Outer transport framing (CONFIRMED, 100% match across thousands of frames)
Every message on this pipe is: `55 CC 30 75` (4-byte magic) + 4-byte little-endian
length + a raw DUML packet (see `analysis/duml.py`) of exactly that length. Verified by
scanning two separate captures (176KB / 60KB) — **100% of frames parsed as valid,
CRC-correct DUML packets**, zero exceptions. This is DUML tunneled over Android Open
Accessory, DJI's own transport-framing on top of a raw byte-stream pipe.

### Passive capture: cmd_set breakdown (no interaction, ~176KB / 75s)
| cmd_set | count | note |
|---|---|---|
| OFDM (9) | 1731 | link/telemetry, matches the interface-4 heartbeat found via USB-host recon |
| CAMERA (2) | 1230 | all `cmd_id=128`, ~80-byte packets — periodic status, NOT video (too small) |
| GENERAL (0) | 435 | |
| RADIO (6) | 123 | |

Confirms: this baseline traffic is pure telemetry regardless of whether video is
streaming. No video-scale bandwidth (~20-30 Mbit/s) ever appeared passively — genuinely
requires *some* trigger, not just an open connection.

### Live command round-trip: CONFIRMED working, wrong command chosen
Extended the app to also write to the accessory (see `sendStartVideoCandidate()` in
`poc/aoa_sniffer/src/.../MainActivity.java`). Sent a hand-built, CRC-correct DUML
packet: `sender=MOBILE_APP(2), receiver=CAMERA(1), cmd_set=1 (SPECIAL), cmd_id=0x21
("SDK Ctrl Camera Start Video Ctrl" per o-gs/dji-firmware-tools' dji-dumlv1-proto.lua
dissector), ack_type=ACK_AFTER_EXEC, empty payload`.

**Got a direct, matching response**: `sender=CAMERA(1), receiver=MOBILE_APP(2), seq=1
(matches our request exactly), packet_type=RESPONSE, cmd_set=1, cmd_id=0x21,
payload=[0xE0]`. This is a huge confirmation — the real firmware parsed our
hand-crafted packet correctly and routed a targeted reply. But `0xE0` is a non-zero
status/error byte (`dji-dumlv1-proto.lua` documents "0 means success" for this style of
operation-status field elsewhere in the protocol) — so the command was recognized but
rejected/errored, not accepted.

**Root cause, found via native-library strings (see below): wrong command.** `0x21` is
very likely "start recording to SD card," a distinct feature from live-view streaming.

### DJI Fly APK: dex is packed (dead end), native libraries are not (goldmine)
Pulled DJI Fly's APK (`dji.go.v5`, ~590MB) via `adb`, decompiled `classes.dex` with
`jadx` (`analysis/dji_fly_apk/decompiled/`) — it's tiny (1.8MB) and contains **no
`com.dji.component.*` classes at all**, despite `logcat` proving those classes run
(`DJIAoaActivity`, `FpvComponentActivity`). The APK ships `libzeus_direct_dex.so` /
`libzeusflipped.so` / `libAppGuard.so` — signatures of commercial APK packing (real code
decrypted into memory only at runtime). Defeating that needs root + Frida-style memory
dumping; not pursued.

**However, native library *symbol names* are not obfuscated** (`strings` on the raw
`.so` works great). Extracted from `libdjisdk_jni.so`/`libDJIVideoJNI.so`/
`libdjivideo.so` (kept in `analysis/dji_fly_apk/libs_extracted/`):
- `SpecialCommandManager::RequestIFrameForLiveView` /
  `RequestIFrameForChannelOneLiveView` / `RequestIFrameForChannelTwoLiveView` — the
  class name `SpecialCommandManager` maps directly to `cmd_set=1 (SPECIAL)`. This is
  very likely the *actual* live-view trigger — requesting an I-frame (keyframe) to
  (re)start decode — completely different from the "Start Video Ctrl" we guessed.
- `ModuleMediator::StartLiveStreaming`, `dji::sdk::start_live_streaming` — higher-level
  entry points above the raw DUML command.
- `PigeonLiveViewLogic`, `VideoPreviewer::start()` — "Pigeon" appears to be DJI's
  internal codename for this system.
- `SetGlassWiFiConnect` — confirms "Glass" (matches the AOA `description=DJI glass`
  we observed) is DJI's internal name for the goggles.

### Static RE of libdjisdk_jni.so — attempted, blocked (2026-07-04)
Tried to extract the exact cmd_set/cmd_id for `RequestIFrameForLiveView` statically. Blocked:
- The `.so` is **deliberately section-obfuscated**: section headers are zeroed/garbage
  (`readelf -S` yields junk names), so symbol/section tooling can't navigate. Program
  headers are intact (one big 0x0–0x3fb2930 R-X segment where file-offset == vaddr).
- The mangled names we found via `strings` (`RequestIFrameForLiveView`, etc.) live in a
  **dense embedded symbol-name table** (~945 `_ZN3dji…` names per 200 KB) that is DJI's
  crash-symbolication blob — **not exported symbols, and not referenced by nearby code.**
  A full ARM64 ADRP+ADD cross-reference sweep of all 16.7M code words (see
  `/tmp` scratch scripts, logic mirrored below) found **zero** code references into that
  name table → the names are indexed by a separate symbolizer at crash time, so there's
  no per-name literal to pin the function address from. Dead end for the fast xref trick.
- Full Ghidra auto-analysis is **not viable on this machine**: 70 MB ARM64 lib vs. only
  ~2.9 GB free RAM, no swap → would thrash/OOM.

Conclusion: pulling the exact command **and its payload** out of this obfuscated blob
statically is disproportionately hard here. The reliable route is to **observe DJI Fly
sending the real bytes while video actually works** — which needs one of:
  (a) a **rootable Android device** → Frida-hook the accessory `write()` and log the exact
      command+payload sequence in situ (trivial once root is available), or
  (b) a **Raspberry Pi 4 as a USB MITM** between goggles and phone (passive tap of the
      real AOA handshake). The Pi is the eventual deployment device anyway.
Absent either, the stopgap is **curated live probing** over our working AOA channel
(we can send and get real responses), limited by having to guess payloads — an I-frame
*request* likely takes a small/empty payload, so it's a more tractable guess than a
"Config Set" struct.

### Static RE — reopened & largely cracked, but a hard wall on the outer cmd_id (2026-07-07)
The 2026-07-04 "blocked" conclusion above was only half right. Two things changed: (1) the
**log format strings are code-referenced via ADRP+ADD** (unlike the mangled crash-symbol
table, which really has zero xrefs) and land you *inside* the real functions; (2) the
capstone/numpy scans this needs run fine on the recon laptop — only *Ghidra auto-analysis*
OOMs here, a narrower limit than assumed. Tools + method live in `handover/relib.py`
(ADRP+ADD xref + capstone window disasm; first-LOAD file-offset == vaddr, so `byte = file[V]`).

**Trigger call chain (traced, high confidence):**
- `SpecialCommandManager::RequestIFrameForLiveView(datalink_id, device_id)` — public entry
  **@ 0x1483eb0** (arg1→[this+0x4c], device_id u16→[this+0xb4]; dispatches per-device vtable).
- `SpecialCommandOneDeviceImpl::RequestIFrameForLiveView()` **@ 0x1484480** — sets pending
  state only: `[obj+0x70]=2` and `[obj+0x5e] |= 0x20`. Does NOT build a packet inline.
- `SpecialCommandOneDeviceImpl::SendSpecialControllPack()` **@ 0x14879a4** — periodic flush;
  reads `obj[0x5e]`, builds an **11-byte "special control pack"** at `obj[0x59..0x63]`,
  transmits via `bl 0xfaec20`.

**The 11-byte special-control-pack payload (what IS statically extractable):**
| pack byte | field @obj | meaning |
|---|---|---|
| [5] | 0x5e | request flags — **bit 5 (0x20) = I-frame for live view**, bit 6 (0x40) = ch1 reset, bit 7 (0x80) = ch2 reset |
| [9] | 0x62 | XOR-8 checksum of bytes [0..8] |
| [10] | 0x63 | bit-packed sub-command field (ShootPhoto etc. set it; I-frame leaves default) |
| [0..4],[6..8] | 0x59.. | device / datalink identifier bytes (runtime values) |

So the I-frame trigger is signaled purely by **bit 5 of pack byte 5**, inside an
otherwise device-populated pack. (Hermes's abandoned `obj+0x63` cmd_id guess was the wrong
field for this command.)

**THE WALL — outer DUML cmd_set/cmd_id is NOT statically extractable from this .so.**
The transmit `bl 0xfaec20` is a **PLT stub → GOT slot 0x4178cb0 → PLT0 resolver 0xf729e0**
(verified `stp x16,x30; adrp; ldr x17,[GOT+0xb98]; br x17`). The packer stripped DYNAMIC to
just DT_NEEDED/DT_SONAME (no DT_RELA/JMPREL); GOT data slots read 0 in-file → relocations are
applied at runtime and the transmit target lives in **another module**. The call site passes
no cmd_set/cmd_id immediates (only this+0x30, device_id@obj[0x48], &payload, 2 runtime-vtable
ctx objects). **⇒ more RAM / Ghidra on this file alone won't yield the outer cmd_id either.**

**Re-scoped next step (higher-EV than more static RE on this file):** the payload is now
solved; only the 1-byte outer cmd_id (cmd_set=1 SPECIAL) is unknown. **Live-enumerate
cmd_set=1 cmd_ids over the working `poc/aoa_sniffer` AOA channel with this exact 11-byte
payload attached (byte 5 = 0x20)** — a few dozen tries with a video-bandwidth jump as the
success signal, tractable and AppGuard-free (unlike the old blind cmd_id×payload search).
Alternative: RE the DUML-transport module `0xfaec20` binds to (APK candidates:
`libilink2.so` / `libilink_protobuf.so` / `libDJIProtobuf.so`) — a fresh lib, new effort.
Caveat: payload semantics are read from disassembly, not yet confirmed against hardware —
verify the byte5/bit5 encoding on the live AOA channel before trusting it.

### ✅ TRIGGER CONFIRMED ON LIVE HARDWARE (2026-07-13) — cmd_set=1 / cmd_id=0x01, XOR-8 verified
Analysing the golden Pi-MITM capture `captures/pi_pull/2026-07-13_063725_pi-mitm_payload.log`
(the run where DJI Fly recognised the goggles through the bridge) closed the "outer cmd_id
unknown" wall above **from the wire instead of the disassembly**:
- DJI Fly emits **cmd_set=1 (SPECIAL), cmd_id=0x01** packets, app(sender=2) → **receiver 14**
  (OFDM-ground/goggles), packet_type=req, **ack_type=0** (fire-and-forget periodic flush =
  `SendSpecialControllPack`). Only two distinct 11-byte payloads in the whole capture:
  `0000000000040000000401` (66×) and `0300000000040000000701` (18×, pack[0]=0x03 = 2nd channel).
- **The 11-byte "special control pack" layout from static RE is CORRECT on hardware:** pack[9]
  is the **XOR-8 of pack[0..8]** — verified byte-exact on both live payloads (0x04→0x04, and
  03⊕04→0x07). This independently proves cmd_id=0x01 is the special-control channel (it was
  only a blocklisted guess before; live enum of 0x06–0xFF had come back empty precisely because
  the real id is **0x01**, in the low range that was skipped for flight-safety).
- **Why no video yet:** pack[5]=**0x04** in every observed pack → **bit5 (0x20 = I-frame for
  live view) is NEVER set**. DJI Fly is stuck in the wrong (Mini-3-style) product UI and never
  calls `RequestIFrameForLiveView`, so it flushes the pack without the I-frame bit. Setting bit5
  is the whole trigger.
- **Artifacts (all CRC-correct, nothing fabricated — derived from the captured packs):**
  - I-frame packs: `0000000000240000002401` and `0300000000240000002701` (pack[5]|=0x20, pack[9]
    XOR-8 recomputed).
  - Full replay DUML (Phase B, no phone): `55180420...240000002401 30c7` etc. — see
    `poc/pi_mitm/iframe_inject.py` (self-parse verified; `duml.build` reproduces a real captured
    cs=1 frame byte-identically given its seq).
  - In-flight flip (Phase A, reuse DJI Fly's transport): `poc/pi_mitm/iframe_flip.lua` — a
    usb-proxy `bulk_out` Lua transform that sets bit5 + recomputes XOR-8 **and DUML CRC16** on
    every cs=1/0x01 pack, **seq-agnostic** (a static pattern/replace can't work: seq varies →
    CRC16 varies). Logic verified byte-exact via a Python mirror against a real captured frame.
- **NEXT (bench):** apply either approach, watch for the video-scale bandwidth jump
  (`analysis/aoa_capture_parse.py` flags it). Also try pack[5]=0x20 alone and repeated ~5–10 Hz
  sends. **Open risk:** the goggles may gate SPECIAL commands behind the still-unanswered
  `cmd_set=0/cmd_id=0xdd` "gvg"+48-byte-blob activation handshake to **receiver 18** (sent 4×,
  retried, zero response in the capture — looks like DJI activation/binding auth). If injection
  is ignored, that handshake — and the ProductType it likely gates — is the next thing to solve.

### Live probing session (2026-07-04) — bus mapped, command surface mapped, no video yet
Upgraded `poc/aoa_sniffer` into an interactive probe (send arbitrary DUML via
`adb shell am broadcast -a com.o3re.aoasniffer.CMD --es cmdset 0x.. --es cmdid 0x.. \
[--es receiver N] [--es payload HEX]`; auto-parses responses; flags large/video frames;
rolling byte-rate; optional TCP forward for `ffplay`). Ran curated, SAFE batteries
(`poc/aoa_sniffer/probe_battery.sh`) — no flight-control/arm commands. Key results:

**Device / bus map (via GENERAL/VersionInquiry cmd_set=0 cmd_id=0x01, all ret=0x00):**
| device_type | responds? | name in version payload | note |
|---|---|---|---|
| 1  CAMERA        | yes | (blank) | the camera/encoder |
| 9  LB_MCU_SKY    | yes | `DJI P1 CP` | the O3 **air unit** (sky side) |
| 14 OFDM_GROUND   | yes | `zv900 gl` | the **goggles** themselves ("gl"=glass, model ZV900) |
| 3,4,6,7,8,10,11,13,17 | no response | — | absent on this bus |

Firmware/version bytes are in each response (record for pinning; raw examples):
- dev9:  `00 12 44 4a 49 20 50 31 20 43 50 00…00 1b 02 00 25 bb 1f 00 00 05 00 00 00`
- dev14: `00 12 7a 76 39 30 30 20 67 6c 00…00 3e 04 01 1e bc 1f 00 00 43 02 00 00`

**DUML response-code semantics (learned):**
- `0x00` = success/accepted.  `0xE0` = unknown/unsupported command.
- `0xE3` = command recognized but bad params/state (a *warmer* signal than 0xE0).

**Command-surface probe matrix (what the firmware accepts):**
| cmd_set/id | target | payload | resp | meaning |
|---|---|---|---|---|
| 1/0x21 (SPECIAL StartVideoCtrl) | cam/air/goggles | — | 0xE0 | unsupported here |
| 1/0x23,0x24,0x25 | camera | — | 0xE0 | unsupported |
| 2/0x7c (CAMERA)  | camera | `01 00…` | **0x00** | accepted (0xE0/0xE3 with wrong payload) |
| 9/0x34 (HDLnk Liveview **Mode** Set) | **air unit (9)** | any 1-byte mode | **0x00** | accepted (0xE0 to goggles) |
| 9/0x39 (HDLnk Liveview **Config** Set) | air unit (9) **and** goggles (14) | various | **0x00** | accepted |
| 9/0x36,0x0c,0x14 | 9/14 | — | 0xE0 | unsupported |

**Outcome:** we can now send commands the live-view subsystem *accepts* (`0x00`), but
**none triggered a video-scale bandwidth jump** — byte-rate stayed flat at ~11-18 kbit/s
(telemetry only) across every combination tried. So the accepted commands (Mode Set /
Config Set) set parameters but are **not** the "start pushing video to this consumer"
switch, or that switch needs a structured payload / prior subscribe step we can't guess.

**Assessment:** blind cmd_id×payload×device search has plateaued exactly as predicted.
We've mapped the device bus, the error semantics, and the accepted-command surface — real,
reusable knowledge — but nailing the exact video trigger now clearly needs **observation
of DJI Fly's real byte sequence** (Frida on a rooted device, or a Pi-4 USB MITM). That is
the decisive next step; further guessing is low-EV.

### No-root Frida attempt (2026-07-04) — injection works, AppGuard defeats it
Tried to observe DJI Fly's real live-view command without root, by injecting **frida-gadget**
into DJI Fly via the `patchelf --add-needed` method (make `libc++_shared.so` depend on the
gadget so it loads at startup — no apktool, no smali/resource rebuild). Tooling + recipe
saved in `poc/frida_repack/` (`repack.sh`, `hook.js`). Plan was to hook libc `write`/`writev`
and log any buffer starting with the AOA magic `55 CC 30 75` — i.e. capture the exact
start-video command, immune to the dex packing (we hook below the Java layer).

Result:
- The re-signed, gadget-injected APK **installed and the gadget loaded**: logcat showed
  `Frida : Listening on 127.0.0.1 TCP port 27042`. So the injection mechanism works.
- But **DJI Fly's `libAppGuard.so` anti-tamper detected Frida and killed the process in a
  tight restart loop** (39 process deaths / 25 gadget starts in a single launch; app never
  reached a usable state, `frida-ps` could never attach). A SELinux `avc: denied { ioctl }
  for path="/proc/<pid>/maps"` in the log is the tell — AppGuard scans `/proc/self/maps`
  (and almost certainly thread names like `gum-js-loop`/`frida-*` and the 27042 port) to
  detect Frida.
- Genuine DJI-signed DJI Fly (`base.apk`, sig `a16899bf`) was reinstalled afterward to
  leave the phone with a working app.

**Conclusion:** the injection path is sound, but beating AppGuard **non-root** is a real
arms race — it would need a string-patched "stealth" gadget (rename its threads/soname off
`frida`/`gum`), a non-default port, and an on-load script that neuters AppGuard's detection
routines before they run (which first requires RE-ing those routines). High effort, uncertain
payoff against a commercial protector. This is exactly the battle that **root makes tractable**
(Magisk + Zygisk + a hiding module like Shamiko, or hooking AppGuard from a privileged spot).
So the recommendation stands and is reinforced: **rooted device** (cleanest) or **Pi-4 USB
MITM** (sidesteps the app entirely). Non-root Frida is a dead end for this hardened app.

### ⚠️ ARCHITECTURAL CONCLUSION (2026-07-04): a plain laptop CANNOT be the O3 video endpoint
Researched how the working commercial solution (CosmoStreamer) pulls the O3 feed on a Pi.
Its own DIY docs (cosmostreamer.com/products/djigoggles2/diy) are decisive:
- "plug the USB cable into the goggles and the other side into the **Raspberry Pi USB-C
  port**"; "**cut the red (5V) wire**" in the cable; "Raspberry Pi 4 must be powered **only
  from GPIO or PoE**." Those three details are the unmistakable signature of the **Pi
  running in USB DEVICE / gadget mode** (dwc2 on the USB-C): the goggles are USB **host**
  and provide VBUS (hence cut the 5V wire to avoid back-powering the Pi, and power the Pi
  separately via GPIO).
- Meaning: **O3 serves video only when the goggles are USB host and the peer is a USB
  device** — exactly the goggles-as-host / AOA personality we characterised on the phone.
  This is the same architecture, whether the device peer is a phone or a Pi-in-gadget-mode.

Consequence: **a normal laptop/PC (USB host-only, no UDC) can never receive the O3 feed.**
Plugged into a laptop, the goggles see a host and fall back to the RNDIS/vendor-bulk
personality, which (per all our probing) carries telemetry/config but **not** video. This
is *also* the deeper reason the old V1/V2 laptop trick (voc-poc, goggles-as-device bulk)
does not port to O3, and why WTFOS explicitly drops O3 support. The original project brief
("use a laptop instead of an AX12") is therefore **not physically achievable for O3** — the
receiver must be able to act as a USB *device* (Pi gadget mode, an Android radio, or
CosmoStreamer hardware). HDMI-out of a laptop is trivial, but the laptop can't get the feed
onto its screen in the first place over USB **directly**.

**BUT the laptop CAN still be the display endpoint via a cheap USB-gadget bridge.** The
receiver-must-be-a-USB-device rule only constrains the box that plugs into the goggles — it
doesn't have to be the box that decodes/displays. Architecture:
```
goggles (USB host) ──► [tiny USB-device bridge] ──(WiFi/net)──► laptop ──► ffmpeg ──► HDMI-out
```
The bridge just needs a UDC (be a USB device to the goggles) + a network link to forward the
raw stream. **An Android phone already IS this** — it's Linux, a USB device, and speaks AOA;
our `poc/aoa_sniffer` app already receives the AOA stream and has a TCP-forward mode. So the
spare Android + our app = a zero-extra-cost bridge; the laptop does decode + HDMI. A ~£15 Pi
Zero 2 W (USB-OTG gadget + WiFi) is the equivalent dedicated dumb-bridge if you don't want to
tie up a phone — note it need NOT decode (no HEVC-decode requirement), it's just a pipe.
The ONE remaining blocker is unchanged: the start-video command (bridge can forward video the
instant it flows; it just won't flow without the trigger). Root-Frida on the spare Android to
capture that command is the enabling step.

### Next steps (not yet done)
- **HERMES HANDOVER #2 RETURNED 2026-07-07 (`dji-fpv-research(1).zip`) — verified, and a
  likely POSSIBLE ANSWER found, but it's in the blocklisted range and NOT yet live-tested.**
  Summary:
  - `libDJIVideoJNI.so`'s `requestIFrame` lead (from the handover) is a **confirmed dead
    end** — `VideoStreamActionFeature::sendRequestNewIFrame` calls through a null vtable
    slot populated from outside the lib (i.e. it's the SDK-side hook dispatcher, not a
    network send). No further work needed there.
  - Back in `libdjisdk_jni.so`, Hermes traced further: `SendSpecialControllPack` (`0x14879a4`,
    matches our own independent finding) reads `cmd_id` from `[obj+0xb0]`/`sub` from
    `[obj+0xb1]`, both set by `SpecialCommandOneDeviceImpl::UpdateReceiver()` (`0x1485d3c`).
    **Independently verified byte-for-byte against our own copy of the file** (not just
    trusting the report) — the function and the specific instructions Hermes cited are real.
  - `UpdateReceiver` has (at least) two branches on a "datalink type" field at `[obj+0x4c]`:
    - datalink `0x70`('p') or `0x4c`('L') → `cmd_id = 9` or `0xe`, depending on a flag byte
      (Hermes got this branch right, both id values match our own re-derivation).
    - datalink `0x8d`/`0x8f`/`0x90` → **we independently re-derived this as `cmd_id = 0x01`**
      (Hermes's report said `0x09` here — we believe Hermes misread the `csinc`/`csel`
      conditional-select pair backwards; verified our reading twice against ARM64 CSINC/CSEL
      semantics). The "no match" fallback is `cmd_id = 9`. Hermes also mentioned a third
      "else → 0x0a" case we have NOT located (may be further in the function, past
      `0x1485e94`/`bl #0xf91020`/`0x148614c`, not yet disassembled by us).
  - **Significance: `cmd_id = 0x01` is inside `SPECIAL_BLOCKLIST` — we have NEVER live-tested
    it** (the exhaustive sweep deliberately only covered 0x06-0xFF). Per the real
    `dji-firmware-tools` table, `0x01` = "Old Special App Control" — plausible as a generic
    envelope command where the actual operation is selected by the 11-byte payload's flag
    byte (byte5, already known: bit5=I-frame) rather than by varying `cmd_id`. That would
    cleanly explain why the 0x06-0xFF sweep found nothing: the real id was always in the
    excluded low range.
  - **NOT understood yet:** what the `[obj+0x4c]` "datalink type" field actually is/means,
    and whether any of our reachable devices (camera=1/air-unit=9/goggles=14) would ever
    cause it to take the value `0x8d`/`0x8f`/`0x90` in practice — this isn't obviously the
    same thing as the `receiver` device-type byte we've been using in `probe_battery.sh`.
    Until this is understood, we don't know how to construct a payload/send that would
    actually exercise this code path for real.
  - **Do NOT live-test `cmd_id = 0x01` without: (1) resolving the disassembly discrepancy
    with more certainty than a single manual re-derivation, (2) understanding the datalink
    field, and (3) explicit human sign-off** — same rule as the rest of `SPECIAL_BLOCKLIST`.
  - `all_native_libs_status.txt` (the 213-lib sweep worklist) was **not updated** by Hermes
    this round ("pending one final pass") — treat it as stale, still ~186 unswept.
  - **Hermes follow-up round 2 (same day):** sent a narrow correction request (re-verify
    the csinc/csel read, trace `[obj+0x4c]`'s writer, find the mystery "0x0a" case).
    Hermes self-corrected fully: confirmed `cmd_id=0x01` for the `{0x8d,0x8f,0x90}` match
    (**independently re-verified — raw bytes at `0x1485e5c` match byte-for-byte against our
    own copy**), found the real third case (not `0x0a` — that was Hermes's own earlier
    error; the real third path is at `0x14860c8`/`0x14860e0`: cmd_id is either read from
    the datalink string itself, or defaults to `0x0e`/sub `0xff` if a `"wifi_"` prefix
    search fails), and traced `[obj+0x4c]`'s only writer to `0x1483ee4` in the public
    `RequestIFrameForLiveView` (`0x1483eb0`), concluding the `0x8d/0x8f/0x90` branch is
    "dead code" since that function's dispatch only proceeds past the write for `w1 ∈
    {0x70, 0x4c}`.
    **We independently checked this "dead code" claim and think it's premature.** The
    `str w1, [x19, #0x4c]` write happens *unconditionally*, before the `0x70`/`0x4c`
    dispatch check (confirmed via our own disasm of `0x1483ee0-0x1484000`) — so `[obj+0x4c]`
    holds whatever value *this function's own caller* passes as `w1`. Hermes checked only
    this one call path; it did not check (a) what values other callers of
    `RequestIFrameForLiveView` might pass, or (b) whether `UpdateReceiver` (`0x1485d3c`) is
    invoked from any other context entirely (it reads like a general "sync receiver info"
    method, not obviously exclusive to the I-frame-request chain). **"Dead code" is not yet
    established — only "unreached via the one path traced so far."**
  - **Hermes follow-up round 3 (2026-07-07, same day) — the bounded reachability scan,
    and the static-RE lane's natural stopping point.** Asked for exactly two things: every
    writer of `[obj+0x4c]` lib-wide, and every caller of `UpdateReceiver`. Result:
    - Only 2 real `SpecialCommandOneDeviceImpl` writers of `[obj+0x4c]` exist (filtered
      from 23 raw `str w,[x19,#0x4c]` sites by proximity to the `[obj+0x5e]` field write):
      the constructor (`0x14873d0`, writes sentinel `0xffff` — **independently
      re-verified, matches exactly**: `mov w8,#0xffff` @ `0x14873c8` /
      `str w8,[x19,#0x4c]` @ `0x14873d0`) and the public `RequestIFrameForLiveView` entry
      (`0x1483ee4`, writes caller-supplied `w1`, already verified in round 2).
    - `UpdateReceiver` has **zero direct callers** (no `BL`/`B`, no `ADRP+ADD` targeting
      it) — it's invoked through a runtime function-pointer/`std::function` chain, plausibly
      from `SpecialCommandOneDeviceImpl::OnProductTypeChanged(ProductType, uint32)` or
      `::ObserverGlassReceiver()` (mangled names for both genuinely exist in the binary —
      **independently confirmed via `strings`** — though per the established convention
      these mangled names aren't code-referenced/nameable via xref, so their bodies aren't
      locatable this way).
    - **Honest final answer (not rounded up either direction):** every *statically
      visible* writer of `[obj+0x4c]` only ever writes `0xffff`/`0x70`/`0x4c` — never
      `0x8d`/`0x8f`/`0x90` — but `UpdateReceiver`'s runtime-only call graph means a writer
      reachable via `OnProductTypeChanged`/`ObserverGlassReceiver` (or any other path not
      resolvable via static xref) can't be ruled out. Hermes's own recommendation, which we
      agree with: **don't probe `cmd_id=0x01` on live hardware on the strength of this
      alone** — the evidence is "no writer found," not "proven unreachable."
  - **Net position, static-RE lane closed for now:** three rounds in, this is about as far
    as static analysis on this binary can honestly take the question — Hermes's own
    conclusion this round (properly hedged, unlike rounds 1-2) matches what a full
    reachability proof would require (decompiling `OnProductTypeChanged`'s runtime
    dispatch), which isn't tractable without live instrumentation. **Next step is dynamic,
    not more static RE**: LSPosed/Zygisk (see `dji-fly-frida-hooking` memory) remains the
    untried path to a definitive answer — it would show the actual bytes DJI Fly sends,
    settling this without more inference.
- ~~Statically disassemble `RequestIFrameForLiveView` to read the cmd_id~~ — DONE
  2026-07-07 (see "Static RE — reopened" section above): call chain + 11-byte payload
  cracked, but the **outer DUML cmd_id is not statically extractable from this .so**
  (runtime-bound transmit into another module). Superseded by the two options below.
- ~~Live-enumerate `cmd_set=1 (SPECIAL)` cmd_ids 0x06-0xFF over `poc/aoa_sniffer` with the
  known 11-byte payload attached~~ — DONE 2026-07-07, **came back empty** (see change log
  below): all 750 sends (3 receivers × 250 ids) returned `0xE0`. Ruled out `cmd_set=1`
  entirely for cmd_ids **0x06-0xFF** on camera(1)/air-unit(9)/goggles(14) with this payload.
- **PRIMARY next step, re-scoped:** the only unexplored part of the `cmd_set=1` id space is
  the blocklisted **0x00-0x05** range (skipped for flight-safety — see `SPECIAL_BLOCKLIST` in
  `probe_battery.sh`). Given 0x06-0xFF is now a confirmed dead end, the real cmd_id plausibly
  sits in that low range — **before touching it, read what `o-gs/dji-firmware-tools`
  documents for SPECIAL 0x00-0x05 specifically** (are they genuinely arm/disarm/FLYCTRL-only,
  or do some of them gate camera/image-link "SDK control mode" and are safe to send toward
  camera/air-unit/goggles but not FLYCTRL?). Do NOT send any of 0x00-0x05 to hardware without
  that check and explicit user sign-off — this needs a human decision, not an automated retry.
- Or RE the transport module `0xfaec20` binds to (`libilink2` / `libilink_protobuf` /
  `libDJIProtobuf` in the APK) to read the DUML framing/cmd_id directly.
- Investigate whether `0xE0` decodes to a documented DJI error code anywhere (not
  found in the tools checked so far).
- The `poc/aoa_sniffer` app is a reusable tool for all of this — rebuild via
  `poc/aoa_sniffer/build.sh`, `adb install -r aoa_sniffer.apk`, replug goggles, pick it
  in the chooser. `probe_battery.sh <serial> iframe` now sends the known I-frame payload
  across a sweep; `RX_LIST="1 9 14"` env var overrides the receiver list without editing it.

## Change log
- 2026-07-13 evening (**HDMI polish + boot autostart — field-ready cutover**):
  - **Live HDMI path locked** after A/B on Shinobi: zero-copy
    `--vo=gpu --gpu-context=drm --hwdec=v4l2m2m` (~8–12% CPU vs ~290% for
    `v4l2m2m-copy`). Live breakup was **not** decode CPU: recorded file on the same
    mpv path = flawless; live + `--profile=low-latency` (`--cache=no`) + tiny queue
    (64) = mid-frame drops. **Baseline:** `--cache=yes` + `0.2s` readahead/cache,
    queue 512, unbuffered stdin. **Do not** recombine `--untimed` +
    `--video-sync=desync` + lavf `nobuffer` (slow-mo + worse lag, slight breakup).
  - **systemd `o3-hdmi.service`** installed + **enabled** on `o3pi.local`:
    `scripts/o3_hdmi_run.sh` (wait for UDC/DRM, free stale raw-gadget, HDMI-only),
    `scripts/install_hdmi_service.sh` (idempotent). Verified streaming under the
    service (~30 Mbit/s class, `throttled=0x0`, ~40°C).
  - **Reconnect re-arm** in `raw_endpoint.py`: on real DISCONNECT, stop I/O threads,
    reset AOA mode to normal, restart mpv, `RawGadget.reinit` for next plug. Ignores
    bus cycle during intentional ACCESSORY_START re-enum.
  - **Dual HDMI:** Pi4 KMS can drive both ports independently (`max_framebuffers=2`);
    **no** free kernel mirror. Service pins **HDMI-A-1** only.
  - OSD note: BF OSD switchable by pilot; DJI system HUD has no known full-off toggle
    on Goggles 2; DVR "camera view recording" is record-only, not live goggles blanking.
- 2026-07-13 (**✅✅ PHASE B COMPLETE — clean, continuous standalone 1080p on hardware, NO phone**):
  `poc/pi_endpoint/raw_endpoint.py` pulls **live O3 FPV H.264 1920×1080 with no phone/DJI Fly**,
  continuous (past the goggles' ~40s watchdog) and full-quality (1 decode error / 22 MB; razor-
  sharp frame). Six fixes across 6 bench rounds this session:
  4. **IN-endpoint write lock** — once the GIL was released, the driver thread and the reply-
     sender raced on the single raw-gadget IN request slot → EBUSY, dropping the periodic
     cs=2/0xeb camera subscribe (video keepalive) → stream stopped. `AccessorySession._ep_in_write`
     serializes with a lock + retries EBUSY.
  5. **Heartbeat sustain** — sustaining only the high-frequency subset dropped the cs=0/0x51
     app-alive heartbeat → goggles' watchdog killed video at ~40s. Now the sustain loop replays
     the WHOLE gold burst on repeat (`gold_app_seq.APP_SEQ`), heartbeat included.
  6. **Reader throughput** — `_on_bulk_out` rebuilt the entire elementary stream per 4 KB chunk
     (`elementary_from_sps` O(n²)) AND ran DUML on video chunks → reader fell behind, kernel
     dropped URBs (~600 kbit/s of ~7-10). Now writes deframer.feed()'s new chunks incrementally
     (`_write_video`, join-once at SPS) and gates DUML on `AOA_MAGIC in data`. → ~3.3 Mbit/s,
     clean decode. Also: blocking ioctls need the ctypes-GIL-release fix (below) for this to run.
  The first three blockers (same session):
  1. **AOA accessory bring-up** — present normal `18d1:4ee0`, answer the goggles' AOA handshake
     (GET_PROTOCOL 0x33 / SEND_STRING 0x34 ×6 / ACCESSORY_START 0x35), re-enumerate to
     `18d1:2d01` (`RawGadget.reinit`). Cold-2d01 = goggles suspend+disconnect (only converse
     with an accessory they brought up). Goggles' SEND_STRING gives the real DJI identity.
  2. **Command replay** — thin version-inquiry seed is insufficient; goggles need DJI Fly's real
     burst (CAMERA cs=2/0xeb+0xe8 subscribes → r1) to arm USB video-out. Captured stream in
     `poc/pi_endpoint/gold_app_seq.py` (218 pkts, first 6s of gold IN packet_type=0), replayed
     with fresh rolling seq + periodic subset looped. Capture-derived, nothing fabricated.
  3. **GIL bug** — `raw_gadget.py` blocking ioctls used `fcntl.ioctl` (HOLDS the GIL), so the
     main thread in `event_fetch` starved the command-driver thread (verified via /proc stacks:
     workers stuck on the GIL futex) → subscribes never sent → telemetry-only. Fixed with a
     ctypes `_libc.ioctl` helper (`RawGadget._ioctl`) that releases the GIL for
     event_fetch/ep_read/ep_write/ep0_*. Non-blocking ioctls stay on fcntl.
  Bench harness: `scripts/push_to_pi.sh` + `scripts/phaseB_standalone.sh` (see
  `docs/phaseB_bench_runbook.md`). Remaining = hardening: live H.264→HDMI, trim the arming
  set, reconnect robustness, package/open-source.
- 2026-07-13 (**Phase B blocker diagnosed + AOA accessory bring-up implemented — offline**):
  Diffed the gold app→goggles DUML against what `raw_endpoint.py` sent, then read the bench
  log and gold `usbproxy.stdout`. Findings:
  - **Video arms at t+1.553s**, only ~0.3s after DJI Fly's first DUML and **before** the
    goggles' identity probes (0x81/0x82/0x88) and **before** gvg is ever answered → live
    video is **near-autonomous** once the accessory link is up + air unit live; it is NOT
    gated on a large command sequence. (Confirms the gold analysis.)
  - **AOA control tag is per-channel, not a health signal:** gold carries all of
    `55 CC 4A 57` (video, 20740×), `55 CC 49 57` (control, 7666×) and `55 CC 30 75`
    (control, 7150×). The app's own DUML rides `55 CC 49 57`, not the `30 75` the parser
    hardcoded — that's why an earlier IN-stream scan found zero packets.
  - **Real Phase B blocker (from the bench log, not DUML content):** presenting `2d01`
    cold makes the goggles enumerate + SET_CONFIG the Pi, then **SUSPEND and DISCONNECT
    without sending any bulk** (ep_read EAGAIN throughout). The goggles only converse with
    an accessory **they themselves brought up** via the AOA handshake
    (`GET_PROTOCOL 0x33 → SEND_STRING 0x34 ×6 → ACCESSORY_START 0x35`, seen driving the
    phone in gold `usbproxy.stdout`).
  - **Implemented** in `poc/pi_endpoint/`: `raw_endpoint.py` now starts in NORMAL mode
    (`PID_NORMAL 18d1:4ee0`), answers the goggles' AOA handshake, and on ACCESSORY_START
    re-enumerates (`RawGadget.reinit`) as the accessory (`18d1:2d01`) before seeding/reading
    bulk. `--cold-2d01` keeps the old direct-present path for A/B. Responder extended to
    cs=0/`0x88` and all three replies (0x81/0x82/0x88) now use the **exact gold bytes**
    (the prior 0x81 template had `051c` at the wrong offset). Responder + descriptor
    self-tests pass offline; **needs a bench run** (raw_gadget re-enum ≈ gotcha #4 risk —
    ACCESSORY_START is the quiescent moment for the close/reopen).
- 2026-07-13 (**GOLD — natural-flow Phase A WORKS on hardware**): live FPV video on
  phone **and** goggles through the Pi MITM with `--follow_aoa_reenum` / no pre-trigger.
  Capture: `captures/pi_pull/2026-07-13_103110_pi-mitm_natural_payload.log` (~160MB, ~87s)
  + analysis notes in `captures/pi_pull/2026-07-13_103110_GOLD_ANALYSIS.txt`.
  Confirmed on the wire:
  - AOA recycle path: ACCESSORY_START → open `18d1:2d01` → gadget presents accessory.
  - Goggles→app bulk ~7–10 Mbit/s from t+~1.5s (video-scale).
  - Video framing magic **`55 CC 4A 57` + LE32 length** (payload only; total = 8+len)
    (distinct from AOA control `55 CC 30 75`). Concatenated payloads are **H.264 High
    1920×1080** annex-B: SPS `0x67` + PPS `0x68` live in ~39-byte frames; slices in
    larger chunks. Deframer: `analysis/video_deframe.py` + CLI
    `analysis/extract_video_stream.py` → `*.from_sps.h264` / `*.mp4`. Phase B code:
    `poc/pi_endpoint/` (FunctionFS accessory 18d1:2d01, gold-derived 0x81/0x82
    answers, live deframe). **Bench:** prefer `raw_endpoint.py` (FunctionFS unavailable
    on this Pi kernel). Live result: full USB enum + bulk DUML + 0x81/0x82 answers;
    video chunks `55 CC 4A 57` not yet emitted without phone — expand gold handshake
    replay next.
  - Healthy session answers cs=0/`0x81`/`0x82` identity probes (failed bridge did not).
  - gvg (`0xdd`) still has no DUML response — yet FPV works → not a hard gate (or
    non-DUML). SPECIAL cs=1/`0x01` I-frame packs **absent** in this steady-state FPV
    capture (that path was from the failed Mini-3-UI session).
  Pre-trigger recipe is now demoted to fallback only; natural flow is primary.
- 2026-07-13 (natural-flow Phase A — code ready, needs bench): implemented in-process
  AOA re-enum follow in the O3-patched usb-proxy so the goggles can drive the real
  AOA handshake through the MITM (no `aoa_trigger.py` pre-trigger). On ACCESSORY_START
  the proxy closes raw_gadget cleanly, waits for phone `18d1:2d0x`, rebuilds gadget
  descriptors, and continues — avoids the raw_gadget-oops exit path (gotcha #4).
  Hypothesis (from `docs/gofly_transition_findings.md`): pre-trigger desynced the gvg
  (`cs=0/0xdd`) activation; natural flow should let it complete. Artifacts:
  `poc/usb_proxy_o3/` (device-libusb + usb-proxy main recycle loop + `--follow_aoa_reenum`),
  `scripts/apply_usb_proxy_o3.sh`, `scripts/phaseA_capture.sh` (default = natural;
  `--pretrigger` keeps the old recipe). **Validated on hardware same day — see GOLD entry above.**
- 2026-07-10 (PIVOT to Pi-4 USB MITM — prep staged, hardware inbound): hardware owner
  bought a Raspberry Pi 4 Model B (field unit turned out **2GB** after a wrong-item
  delivery; works) and directed a full pivot to the Pi as the
  man-in-the-middle. Rationale is the convergence of every prior dead-end: the live-view
  trigger is not statically extractable (runtime-bound transmit), the live cmd_id space
  is exhausted (0x06-0xFF) with the low range blocked, and on-phone Frida is beaten by
  AppGuard — but a Pi-4 USB proxy taps the wire *below the app*, where AppGuard is blind,
  and the Pi's dwc2 USB-C is a real UDC so the goggles will serve it video (the reason a
  host-only laptop never could). **Deployment target is now the Pi itself** (supersedes
  the laptop-endpoint plan). Prep built while the Pi ships, all verified locally where
  possible:
  - `docs/pi4_mitm_setup.md` — full runbook: parts checklist, headless flash, VBUS/power
    isolation, Phase-A capture procedure (usb-proxy + usbmon), Phase-B endpoint outline,
    troubleshooting.
  - `scripts/pi_setup.sh` — idempotent Pi bring-up: apt deps, `dtoverlay=dwc2,dr_mode=
    peripheral`, clone+build xairy/raw-gadget + AristoChen/usb-proxy, persist raw_gadget.
  - `poc/pi_mitm/aoa_trigger.py` — pyusb AOA handshake with DJI's exact identity strings
    (`manufacturer=DJI`, `model=com.dji.logiclink`, …) to drive the phone into DJI-Fly
    accessory mode; doubles as the fallback if usb-proxy can't follow the phone's
    mid-session re-enumeration into `18d1:2d0x`. (syntax-checked; not yet run on hardware)
  - `analysis/aoa_capture_parse.py` — ingests a usbmon pcap *or* a hex/byte dump,
    reframes AOA (`55 CC 30 75` + LE32 len) → DUML (reuses `duml.py`), finds the
    video-bandwidth jump, and prints the app→goggles (sender=MOBILE_APP=2) command(s)
    just before it. **Tested end-to-end on a synthetic AOA-framed capture** — correctly
    isolated the planted trigger out of 400+ noise packets.
  - `poc/pi_endpoint/` — Phase-B scaffold (accessory emulation → send trigger → deframe
    → HDMI decode). Refuses to run on unset trigger constants — no fabricated bytes.
  Tooling chosen: **AristoChen/usb-proxy** (raw-gadget + libusb MITM) is purpose-built
  for exactly this and runs on the Pi 4's dwc2 UDC; we don't reimplement it. Open risk to
  debug on first contact: whether usb-proxy survives the phone's AOA re-enumeration
  mid-session (fallback = pre-trigger with `aoa_trigger.py`), and the VBUS-isolation /
  forced-peripheral-pullup interaction on the goggles→Pi USB-C link.
- 2026-07-07 (static RE of ilink2/protobuf, negative + Frida instability): extracted
  `libilink2.so`, `libilink_live.so`, `libilink_protobuf.so`, `libDJIProtobuf.so`,
  `libilink_network.so` from `base.apk` (all unhardened — 1981-01-01 zip-default mtime vs.
  `libdjisdk_jni.so`'s real 2024-12-18 mtime, so full intact `readelf -d` symbol tables, no
  ADRP+ADD trick needed). Grepped every exported symbol/string for DUML/cmd_set/liveview
  keywords: **zero hits.** "ilink2"/"ilink_live" turned out to be DJI's *cloud* stack
  (account auth/OAuth via `DartAuthManager`, RTMP-style social live-broadcast via
  `CreateIlinkLive`) — unrelated to the local USB video-out. Independently re-confirmed
  (not just trusting the earlier handover) that `libdjisdk_jni.so`'s DYNAMIC section really
  has no SYMTAB/STRTAB/RELA/JMPREL at all (only NEEDED/SONAME) — there is no static way to
  name the `0xfaec20` thunk's target from this file, and it may not even leave the file
  (could be a hidden region of the same 70MB binary rather than another module).
  Tried the obvious dynamic follow-up instead: read the resolved GOT pointer at runtime via
  a passive (zero-hook) Frida script, on the theory that not touching the FPV path would
  avoid AppGuard entirely. **Also failed** — both `spawn()`+load-before-resume (native
  tombstone crash in `libdatajar.so` within ~6s, reproduced twice; a Frida-free control
  launch was stable) and attach-to-an-already-idle-instance (the attach itself timed out
  and left the process wedged in `D`-state) broke the app before any script code ran. Full
  detail in the `dji-fly-frida-hooking` project memory's 2026-07-07 section. **Both the
  live-cmd-id and the static-RE-of-siblings leads are now exhausted; only two directions
  remain:** LSPosed/Zygisk (different injection mechanism, not yet tried) or a slower,
  from-scratch static sweep of the APK's other native libs beyond the ones grepped so far.
- 2026-07-07 (FLYCTRL probe, negative result): found the actual `o-gs/dji-firmware-tools`
  SPECIAL (cmd_set=1) command table via its Wireshark dissector
  (`dji-dumlv1-proto.lua`/`SPECIAL_UART_CMD_TEXT`):
  `0x00`=Sdk Ctrl Mode Open/Close Nav, `0x01`/`0x03`=Old/New Special App Control,
  `0x02`/`0x04`=Old/New Special Remote Control, `0x05`=SDK Ctrl Mode Arm/Disarm,
  `0x1a`/`0x1b`=SDK Ctrl Gimbal Speed/Angle Ctrl, `0x20`=SDK Ctrl Camera Shot Ctrl,
  **`0x21`=SDK Ctrl Camera Start Video Ctrl**, `0x22`=SDK Ctrl Camera Stop Video Ctrl,
  `0xff`=UAV Loopback. Confirms the blocklist (0x00-0x05) was correctly scoped — genuine
  nav-authority/arm-disarm commands, not a red herring, and they remain permanently
  off-limits regardless of target device.
  This also explains why 0x21 (which we'd already tried against camera/air-unit/goggles
  and got `0xE0`) might target **FLYCTRL** instead — "SDK Ctrl" commands in DJI's Onboard
  SDK architecture typically route through the flight controller, which then commands
  camera/gimbal internally. With explicit user sign-off, tried a canary (`0xff` loopback,
  diagnostic/harmless) then `0x21` (empty payload, then with the known I-frame payload)
  against **receiver=3 (FLYCTRL)** for the first time this project has targeted it.
  **Result: total silence — zero responses to all three sends, not even `0xE0`.** This
  matches (and reconfirms) the Phase 0 bus-map finding that FLYCTRL (device 3) simply does
  not respond to anything on this USB/AOA link (see the "no response" row in the bus-map
  table above) — it is not reachable here at all, regardless of cmd_set/cmd_id. Closes off
  the FLYCTRL-routing hypothesis for THIS transport.
  **State of the live-enumeration lead now: exhausted.** Every reachable device
  (camera=1, air-unit=9, goggles=14) rejects cmd_set=1 cmd_ids 0x06-0xFF outright; the one
  unreachable device (FLYCTRL=3) can't be probed further on this link; 0x00-0x05 stay
  blocked. No more live cmd_id combinations left to try with the current payload/transport
  understanding.
  **Re-scoped next step:** static RE of the actual transport module the
  `SendSpecialControllPack` PLT thunk (`0xfaec20`, see "Static RE — reopened" section)
  binds to at runtime — candidates in the DJI Fly APK: `libilink2` / `libilink_protobuf` /
  `libDJIProtobuf`. The live-cmd_id dead-end plus the runtime-bound transmit target both
  point the same direction: O3's actual live-view trigger may not be a classic single DUML
  SPECIAL command at all, but wrapped/routed by a newer ilink2/protobuf layer not covered
  by the (Mavic-era) `dji-firmware-tools` command table. This is now the only unexplored
  lead.
- 2026-07-07 (live sweep, negative result): installed `poc/aoa_sniffer` on the rooted
  capture phone (was not yet installed this session), connected it to the goggles as the
  AOA accessory (had to `force-stop dji.go.v5` first — it was holding the accessory fd from
  an earlier launch, which made `UsbManager.openAccessory()` return null for our app until
  DJI Fly was killed and the cable replugged). Extended `probe_battery.sh` with an `iframe`
  mode that attaches the known 11-byte SpecialControllPack payload (byte5=0x20 I-frame flag,
  byte9=XOR-8 checksum=0x20, rest zero) instead of an empty one, and widened the sweep to
  all three addressable receivers (camera=1, air unit=9, goggles=14). Result: **all 750
  sends (250 non-blocklisted cmd_ids × 3 receivers) came back `0xE0`** — zero accepted
  responses, zero LARGE FRAME hits, byte-rate stayed at baseline telemetry throughout. This
  rules out `cmd_set=1` cmd_ids **0x06-0xFF** on all three devices, with this payload, as the
  live-view trigger. Narrows the remaining unknown to the flight-safety-blocklisted
  **0x00-0x05** range — not probed yet, needs the `o-gs/dji-firmware-tools` SPECIAL table
  checked (and explicit sign-off) before ever sending one of those to hardware, since it may
  legitimately include arm/disarm/FLYCTRL-adjacent ids.
- 2026-07-07 (static RE): reopened static disasm of `libdjisdk_jni.so` after prepping a
  handover (`handover/`) for an off-machine agent (Hermes/minimax-m3), which returned tooling
  but no answer. Using log-string ADRP+ADD xrefs (the mangled-name table has zero xrefs),
  traced the full trigger chain: `RequestIFrameForLiveView` @0x1483eb0 → per-device impl
  @0x1484480 (sets pending flag bit5 of pack byte 5) → `SendSpecialControllPack` @0x14879a4
  (builds an 11-byte special-control pack, XOR-8 checksum at byte 9). Decoded the payload
  (byte5 bit5=I-frame, bit6/7=channel resets; byte10=sub-cmd; rest=device ids). **Hit a hard
  wall on the OUTER DUML cmd_id:** transmit is a PLT→GOT→PLT0-resolver runtime-bound import
  into another module; DYNAMIC is stripped of reloc tables, so it's not statically resolvable
  from this file (Ghidra RAM wouldn't help). Re-scoped to live cmd_id enumeration WITH the now-
  known payload. Full detail in "Static RE — reopened & largely cracked" section above.
- 2026-07-04 (frida): injected frida-gadget into DJI Fly (no root, patchelf --add-needed
  method; `poc/frida_repack/`). Gadget loaded & listened, but `libAppGuard.so` detected
  Frida (via /proc/self/maps scan) and killed the app in a restart loop — non-root Frida
  defeated by the app's anti-tamper. Restored genuine DJI Fly. Reinforces the root / Pi-MITM
  recommendation. Detail in "No-root Frida attempt" section above.
- 2026-07-04 (probing): upgraded `poc/aoa_sniffer` into an interactive DUML probe
  (broadcast-driven send, response parsing, rate/large-frame detection, TCP forward).
  Mapped the device bus (camera=1, air unit=9 "DJI P1 CP", goggles=14 "zv900 gl"),
  learned response codes (0x00 ok / 0xE0 unknown-cmd / 0xE3 bad-param), and found
  accepted live-view commands (HDLnk Mode Set 9/0x34 @ air unit, Config Set 9/0x39 @
  air unit & goggles, CAMERA 2/0x7c). None started video — byte-rate stayed at telemetry
  levels for every combination. Blind probing plateaued; exact trigger needs observing
  DJI Fly (Frida/root or Pi MITM). Full detail in "Live probing session" section above.
- 2026-07-04 (later): built `poc/aoa_sniffer` (sideloadable, no-root Android app),
  confirmed the outer AOA transport frame (`55 CC 30 75` + LE length + DUML packet,
  100% match across two captures), sent a hand-built DUML command and got a real
  targeted response back (wrong command chosen — `cmd_set=1/cmd_id=0x21` errored with
  `0xE0`), and found unobfuscated native-library symbol evidence pointing at
  `SpecialCommandManager::RequestIFrameForLiveView` as the likely real trigger. See
  "AOA/logiclink protocol" section above for full detail.
- 2026-07-04: attempted a non-root PCAPdroid capture from an Android phone connected to
  the goggles via USB-C while running DJI Fly. The pcap contained zero goggles traffic
  (only normal phone internet chatter — airplane mode had not fully blocked Wi-Fi/
  cellular, a secondary issue). Root-caused via `adb` (wireless, no phone root needed):
  the phone is NOT a USB host here — it's an AOA accessory, goggles are host. No IP
  layer exists on this path at all, so no network-layer capture tool could ever have
  worked. See "MAJOR REVISION" section above for full detail. This is why our earlier
  RNDIS/vendor-bulk/DUML findings (from the Linux-PC-as-host session) don't directly
  describe the DJI-Fly video path — that hardware exposes a different USB personality
  depending on which end initiates host role.
- 2026-07-03: doc scaffolded, no hardware captures yet.
- 2026-07-03 (same day, hardware session): goggles plugged in and live. Ran full
  Phase 0 recon: fixed an interface-detection bug in `phase0_recon.sh` (host had a
  second, stale, unrelated `enx*` interface that the naive "first match" heuristic
  picked instead of the live one), confirmed real subnet/IPs/ports on this hardware,
  discovered the 5 unclaimed vendor bulk USB interfaces, mounted+inspected the onboard
  SD card (read-only), found DVR recordings are H.264 (not H.265 as assumed),
  fingerprinted ports 8901-8904 as low-bandwidth unsolicited push channels (not video),
  ruled out FTP anonymous/common-default-credential access, and passively probed the 5
  unclaimed vendor bulk USB interfaces (found a heartbeat on interface 4; 3/5/6/7 silent
  without a write, which we deliberately did not attempt yet). Identified the interface-4
  heartbeat as genuine DUML (byte-exact CRC8/CRC16 match against the public
  dji-firmware-tools reference) — cmd_set=OFDM. Wrote a reusable `analysis/duml.py`
  codec, wired it into `pcap_harness.py`. Tried (and did not get a response from) a
  standard DUML VersionInquiry over TCP 1415. Confirmed via CRC-validated scan that
  8901-8904 do NOT carry DUML — a genuinely different, still-unidentified protocol.
