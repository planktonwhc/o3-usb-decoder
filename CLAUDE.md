# CLAUDE.md — DJI O3 / Goggles 2 USB Video-Out Reverse Engineering

## Mission
Reverse-engineer the USB video protocol of the **DJI Goggles 2 (O3 system)** and build an
**open-source decoder** that pulls the live video feed directly off the goggles over USB —
no phone, no DJI Fly app, no paid license. End goal: a Raspberry Pi 4 that outputs the feed
to **HDMI** (and optionally re-streams over the network) so it can run all day off wall power
or a power bank.

**✅ Core goal achieved (2026-07-13):** Pi 4 standalone AOA accessory → live **H.264 1080p60**
(~7 Mbit/s ES) → micro-HDMI, continuous, no phone. Boot service `o3-hdmi` on `o3pi` /
`192.168.1.57`.
**✅ Field excellent (2026-07-14):** owner **“excellent, very close to ideal.”**
**🔧 Smoothness polish (2026-07-14 evening):** SPS L5.2→L4.2, **0.18s / 1.5 MiB** cushion,
AUD coalesce + soft prebuffer — **deployed**, eyes-on A/B **pending**. Dual parked.
Live USB path is **H.264**, not H.265.
**✅ Chroma bug SOLVED, owner-confirmed (2026-07-15):** field test found a green OSD bar plus
cyan/blue ghosting on high-contrast content (TV/monitor in view). Root cause: the Pi's HW
H.264 decode block (v4l2h264dec/bcm2835-codec) reconstructs chroma wrong at sharp edges,
proven via byte-level A/B against an independent software decode of the identical bytes. Fix:
atomic presenter now decodes with avdec_h264 (SW, gstreamer1.0-libav) instead of HW, measured
120-125 fps on the Pi 4 (need ~50-60), so no speed tradeoff, and chroma is pixel-correct.
Owner: "looks excellent now." New Pi dep gstreamer1.0-libav, now auto-installed by
scripts/install_hdmi_service.sh. Full arc: docs/SESSION_CLOSE_2026-07-15_chroma_fix.md.
**✅ Resolution/aspect crash-loop SOLVED, owner-confirmed (2026-07-15, later same day):** field
report of HDMI stuck on console through multiple reboots. Root cause: goggles switched to
sending 1440x1080 (4:3) instead of 1920x1080; presenter's DRM mode was hardcoded to 1920x1080,
so every decoded frame failed `drmModeSetCrtc` (ENOSPC) — permanent crash-loop, not
intermittent. Fixed with a `videoscale add-borders=true` + pinned `pixel-aspect-ratio=1/1`
stage in the GStreamer pipeline so the presenter always gets a fixed canvas, correctly
letterboxed to the source aspect ratio. Owner: "its excellent." Separately found (but did NOT
fix) a genuine kernel oops in `raw_gadget.ko` that can wedge the UDC until a full reboot —
deferred, see Immediate next tasks. Full arc:
docs/SESSION_CLOSE_2026-07-15_reliability_resolution_fix.md.
**✅ Event ops tooling + git (2026-07-16):** Built and **deployed** `o3-oops-watchdog`,
`event_health.sh --preflight`, `o3_soak.sh`; unit default `O3_HDMI_PLAYER=atomic`; git `main`.
**✅ Air-unit hot-swap SOLVED, owner-confirmed (2026-07-16 later, "excellent, twice in a
row"):** goggles stay on Pi → drone off → HDMI black (OK) → drone on → **HDMI recovers to
smooth ~50 fps on its own**, no cable unplug / no restart. Five stacked fixes (video-silence
re-arm + gentle/settle cadence + deframer rebuild + no-SIGTERM-live-presenter + no-zombie-pump).
Full arc: docs/SESSION_CLOSE_2026-07-16_hotswap_rearm.md.

This is legitimate interoperability work on hardware we own. Everything here is for reading
our own drone's video feed on our own machines. Publish the result openly when it works.

## Why this project exists (context, don't relitigate)
Already evaluated and rejected for our needs:
- **Phone + DJI Fly + HDMI/scrcpy** — works, but a phone dies after a few hours; we need all-day runtime.
- **RadioMaster AX12** (~$270 Android radio w/ HDMI out) — great fit, but out of budget for now.
- **CosmoStreamer** — solves this commercially on a Pi, but it's **closed-source behind a per-device
  license key**. We don't want to pay for a DJI license; we want to build and open-source it.

So the plan is to crack the protocol ourselves and give it to the community.

## The core problem (how O3 differs from the old goggles)
- The **old FPV Goggles V1/V2** used a raw USB **bulk endpoint** with a magic number
  (`0x524d5654`) — solved openly by fpv-wtf/voc-poc. **This does NOT apply to O3/Goggles 2.**
- The **O3 / Goggles 2** enumerate over USB as an **RNDIS USB-network device** on the
  **192.168.42.x** subnet. So this is a **network** RE problem, not a raw-USB one: once the
  interface is up, we talk to the goggles with ordinary TCP/UDP sockets + Wireshark. No libusb needed.
- DJI officially calls the goggles a "closed loop product." No SDK help. Pure RE.
- **fpv-wtf/WTFOS explicitly does NOT support O3, Goggles 2, Integra, Goggles 3, or O4.**
  This is an OPEN, unsolved problem in the open-source space — nobody has published it.
  Cracking it = the "voc-poc for O3" that doesn't exist yet.
- **Encouraging:** CosmoStreamer decodes this on a bare Pi with no DJI keys, which strongly
  implies the stream is **framed/obfuscated, not strongly encrypted**. If they cracked it, we can.

## Established facts vs. things to verify
CONFIRMED (from research / community):
- O3/Goggles 2 present as RNDIS when plugged into a USB host (subnet is NOT reliably
  192.168.42.x — see below, measure per device).
- Old bulk-endpoint magic-number method (fpv-wtf's specific V1/V2 protocol) is irrelevant here.

CONFIRMED on THIS hardware (2026-07-03 recon session — see `docs/protocol_notes.md` for full detail):
- Subnet is **192.168.60.0/24** on this unit, not 192.168.42.x. Goggles = 192.168.60.2,
  host = 192.168.60.1 (DHCP-assigned by goggles). **Don't hardcode 42.x — it's per-device/firmware.**
- Goggles does not answer ICMP ping on this link; use ARP/`nmap -sn` for host discovery.
- Open TCP ports on the USB link: 21 (FTP), 1415 (dbstar, silent until spoken to correctly),
  8901-8904 (all present). Port 1561 (facilityview) from the community wifi scan is **NOT**
  present on the USB link.
- Ports 8901-8904 push structured binary data **unsolicited**, immediately on TCP connect, but
  at telemetry-scale bandwidth (0.016-0.588 Mbit/s) — **confirmed NOT the video flow**. Real
  video is gated behind a start-stream trigger, not yet found.
- The device is a **Linux USB composite gadget** (`iConfiguration` = `rndis,mass_storage,bulk,acm`)
  exposing 8 interfaces: RNDIS (0-1, driver-bound), Mass Storage / SD card (2, driver-bound),
  and **5 vendor-specific bulk interfaces (3-7) with NO kernel driver bound** — unclaimed, free
  to probe with libusb/pyusb. This is a brand-new lead not previously considered — completely
  unexplored. Full descriptor: `docs/lsusb_dji_e3t.txt`.
- Onboard SD card (mass storage interface) mounts read-only as exFAT; DVR recordings
  (`DCIM/100MEDIA/*.MOV`) are **H.264**, 1080p50/60, ~18-26 Mbit/s — NOT H.265 as previously
  assumed. This is the *onboard DVR* pipeline though, which may differ from the *live USB
  video-out* pipeline — don't assume the live stream is also H.264 until captured directly.
- FTP (21): banner `220 Hello!`, anonymous login rejected, a short list of obvious default
  creds all rejected. Deprioritized — mass-storage mount is an easier way to pull files.
- **The unclaimed USB interface 4 speaks DUML** (DJI's internal system-bus protocol,
  documented by `o-gs/dji-firmware-tools`) — verified byte-exact via CRC8/CRC16, tagged
  `cmd_set=OFDM` (the image-link module). A reusable codec is at `analysis/duml.py`.
  Confirmed (via CRC-validated scan, not guessing) that ports 8901-8904 do **NOT** use
  DUML — different, still-unidentified protocol. A standard DUML VersionInquiry sent to
  port 1415 got no response.

RESOLVED for the live video path (see CURRENT STATUS 2026-07-13): video is **AOA bulk**
with framing `55 CC 4A 57` → H.264 1080p, not RNDIS ports. Phase B standalone works.

STILL OPEN:
- **Dual HDMI seamless + no quality loss** (next session — see `docs/SESSION_CLOSE_2026-07-13_field_hdmi.md`).
- Minimal arming subset of gold_app_seq; network re-stream; packaging.
- RNDIS-side curiosities (1415, 890x, FTP) if anyone revisits PC-as-host mode.

## ⚠️ MAJOR CORRECTION (2026-07-04): DJI Fly does NOT use the RNDIS/IP path at all
Everything above this point was measured with **a Linux PC as USB host** — that's a real
mode this hardware supports, but **it is not how DJI Fly talks to the goggles**. Confirmed
by plugging the goggles into an Android phone running DJI Fly and inspecting via `adb`
(no root needed — `dumpsys usb` + `logcat`):
- The goggles act as the **USB host**; the phone is a USB **peripheral** using **Android
  Open Accessory (AOA)** protocol (`manufacturer=DJI, model=com.dji.logiclink,
  description=DJI glass`). USB host/device roles are **reversed** vs. the PC session.
- This launches DJI Fly (`dji.go.v5`) straight into `DJIAoaActivity`, then
  `FpvComponentActivity` (First Person View) ~5s later — the real video component.
- **There is no IP network interface at all on this path.** An earlier PCAPdroid
  (network-layer, no-root) capture attempt found zero goggles traffic — not a
  misconfiguration, there's no IP layer to capture. Confirmed dead-end for this approach.
- So: the original "this is a network RE problem, not a raw-USB one" framing (see "The
  core problem" section above) is **incomplete**. It's true for the PC-diagnostic
  personality of this hardware, but the actual client-app video path is raw-USB (AOA).
- Full detail + next-step options in `docs/protocol_notes.md`'s "MAJOR REVISION" section.
  Capturing the AOA payload needs either root (read `/dev/usb_accessory` directly) or a
  small custom Android app that races DJI Fly for the accessory via a matching
  intent-filter — not yet built, needs a decision with the hardware owner before investing
  the effort.

## ✅ CURRENT STATUS (2026-07-16 evening): **seamless splash ↔ video — owner “solid on both counts”**

> **✅ Owner-confirmed:** no Linux console flash on **live → drone-off → splash**,
> **splash → USB plug → live**, or **splash → drone re-power → live**.
>
> Architecture (DRM master discipline):
> - **Live → splash:** atomic paints XRGB idle text **in-process** (stall ~3s / SIGUSR1);
>   never DropMaster for the handoff. External process-swap always flashed console.
> - **Splash → live (drone re-power):** SIGUSR2 rebuilds GStreamer pipeline in the **same**
>   process; first frame SetCrtc YU12 while master still held.
> - **Splash → live (USB plug / cold):** keep external `idle_splash` until first decoded
>   frame → log `NEED_MASTER` → release splash → atomic modeset (~50ms).
>
> Key files: `atomic_drm_present.py`, `raw_endpoint.py`, `idle_splash.py`.
> Full arc: `docs/SESSION_CLOSE_2026-07-16_seamless_splash.md`.
> **Still open:** **5m soak** with cool drone (`sudo o3_soak.sh` → `RESULT: PASS`).

## PRIOR STATUS (2026-07-16): **idle splash DONE — owner-confirmed + Pi rebooted**

> **✅ Owner: “seems all good”** after deploy + reboot. HDMI idle shows large
> **"waiting for live FPV feed"** (not the Linux console). Boot cmdline quiet tokens live.
> Initial spawn path hid splash before video (console on splash→video — fixed later same day).
> Handoff: `docs/SESSION_CLOSE_2026-07-16_idle_splash.md`.

## PRIOR STATUS (2026-07-16 later): **air-unit hot-swap SOLVED, owner-confirmed ("excellent, twice in a row")**

> **P0 DONE.** Battery/air-unit hot-swap with the goggles staying USB-plugged into the Pi now
> recovers to smooth live HDMI on its own — owner ran it twice back-to-back, ~50 fps each time,
> no cable unplug, no service restart. Took **five stacked fixes**, each found by journal A/B:
> 1. **Video-silence re-arm** — `_seed_handshake` arms USB video ONCE (`APP_SEQ`) then only
>    loops the light `SUSTAIN_SEQ`, which does NOT re-subscribe video after the goggles tear
>    down their camera session on drone-off. New `video-resume-wd` watchdog replays `APP_SEQ`
>    when ES is silent while the AOA host is still alive. **(Confirmed: this resumes the feed.)**
> 2. **Gentle + settle-latched re-arm** — re-arming every 3s reset the encoder (bursty garbage
>    → SUSPEND). Now 6s detect / 8s cadence, and once ES responds, hold `O3_VIDEO_REARM_SETTLE_S`
>    (20s) so the player locks undisturbed.
> 3. **Rebuild the deframer on re-arm** — the long-lived `StreamingVideoDeframer` kept the
>    partial frame from where the old stream cut off mid-frame and spliced it onto the resumed
>    stream → corrupt AUs. Reset it on re-arm (`_deframer_reset_evt`) → clean ~6 Mbit resume.
> 4. **Never SIGTERM a live cold-starting presenter** — `_pump` was killing the resumed player
>    mid `gi`-import (`rc=-15`) on a transient write.
> 5. **No zombie pump** — the retry from #4 misread a killed player as alive and spun clearing
>    the shared queue → 1.5 fps starvation. `_pump` now captures its own proc handle and exits
>    the instant it's superseded. `_kill_proc(who=…)` is tagged for field diagnosis.
>
> Knobs (defaults fine): `O3_VIDEO_REARM=1`, `O3_VIDEO_SILENCE_REARM_S=6`,
> `O3_VIDEO_REARM_PERIOD_S=8`, `O3_VIDEO_REARM_SETTLE_S=20`. Deployed on `o3pi`, git `main`.
> **Full arc (all 5 fixes + every field test):** `docs/SESSION_CLOSE_2026-07-16_hotswap_rearm.md`.

## PRIOR STATUS (2026-07-16 close): **event ops deployed; air-unit hot-swap BROKEN (P0)**

> **Shipped/deployed:** oops watchdog + event_health + o3_soak; atomic unit default; git `main`.
> Pi left with `o3-hdmi` + `o3-oops-watchdog` **active**.
>
> **P0 field fail (owner):** With goggles remaining USB-plugged into the Pi, powering the
> **air unit/drone off** blanks HDMI (expected); powering the drone **back on** restores
> FPV **in the goggles** but **HDMI stays black**. Battery hot-swap is a hard event
> requirement — not optional polish. (Fix built + deployed same day — see CURRENT STATUS.)
> **Handoff:** `docs/SESSION_CLOSE_2026-07-16_event_ops_git.md`.

## PRIOR STATUS (2026-07-15, later session): **resolution-mismatch crash-loop FIXED; kernel oops found**

> **Field report (2026-07-15, after the chroma-fix deploy):** HDMI stuck on console —
> two SEPARATE bugs:
> 1. **✅ FIXED, owner-confirmed:** goggles sent **1440x1080** (4:3); presenter DRM mode
>    hardcoded 1920x1080 → `drmModeSetCrtc` ENOSPC every frame. Fix: `videoscale
>    add-borders=true` + PAR 1/1 → fixed canvas letterbox. Owner: "its excellent."
> 2. **⚠️ Kernel bug (mitigated 2026-07-16 via auto-reboot WD, race not fixed):**
>    `raw_gadget.ko` NULL-deref on phantom re-arm race; D-state; only reboot recovers.
>    Full arc: `docs/SESSION_CLOSE_2026-07-15_reliability_resolution_fix.md`.

## PRIOR STATUS (2026-07-15): **chroma bug SOLVED (SW decode) — chop AND color both fixed, owner-confirmed**

> **✅✅ CONFIRMED LIVE (2026-07-15, this session):** field test surfaced a solid green bar
> under the goggles' own OSD (bottom of screen) and cyan/blue ghosting on high-contrast content
> (a TV/monitor in view). Investigated and ruled out two false leads first: (1) vc4 plane
> `COLOR_ENCODING`/`COLOR_RANGE` — already defaulted correctly, setting them was a no-op; (2)
> firmware/eeprom update — `raspi-firmware`/bootloader already at latest apt-available version.
> **Real root cause, proven via byte-level A/B:** captured the live ES + raw decoded frames,
> decoded the *identical* bytes independently with software (ffmpeg, then GStreamer's
> `avdec_h264`), and diffed chroma planes against the Pi's HW decoder
> (`v4l2h264dec`/`bcm2835-codec`, `/dev/video10`) output — HW decode clips chroma to an extreme
> wrong value at sharp/high-contrast edges (present even on the first keyframe, independent of
> the SPS level-patch value), software decode of the same bytes is correct throughout. **Fix:**
> switched `atomic_drm_present.py` from `v4l2h264dec` (HW) to `avdec_h264` (SW) — benchmarked
> **120-125 fps on this Pi 4** (need ~50-60 fps), so zero speed tradeoff, and chroma is now
> pixel-correct (verified both numerically and by eye). New Pi dep `gstreamer1.0-libav`
> (`avdec_h264`), auto-installed by `scripts/install_hdmi_service.sh`. **Owner, after re-flying
> past the same TV: "confirmed, looks excellent now."** Full arc, all dead ends, and the exact
> diagnostic method: `docs/SESSION_CLOSE_2026-07-15_chroma_fix.md`.

## PRIOR STATUS (2026-07-14): **atomic-KMS presenter LIVE + owner-confirmed — chop SOLVED, mpv retired**

> **✅✅ CONFIRMED LIVE (2026-07-14, this session):** The atomic-KMS present-per-frame presenter
> **`poc/pi_endpoint/atomic_drm_present.py`** (`O3_HDMI_PLAYER=atomic`) is **working live from the
> goggles, owner-confirmed "test was successful"** — steady **~50.3 fps** sustained (present-per-
> frame, no chop; ~42 unique captured vs mpv's ~14). **Owner directive: never mpv again** (memory
> `o3-never-mpv-atomic-only`) — atomic IS the player; mpv is dead code only.
>
> **Bugs found + fixed this session (all live-path — none reproduced offline until forced):**
> 1. **Macroblock corruption** ("smooth but broken"): a `leaky=downstream` pre-decoder queue
>    dropped reference NALs under vblank backpressure → **non-leaky** now (drop only decoded frames;
>    backpressure → endpoint GOP-aligned drop). **RULE: never leak compressed ES.**
> 2. **Frozen console on cold boot**: presenter grabbed DRM master at startup then livelocked →
>    **deferred DRM** (open/modeset only on the first *decoded* frame) + **stall watchdog** + **bus
>    watch** → a stalled presenter exits & respawns, never holds the display. Plus **endpoint
>    warm-up grace** (`O3_ATOMIC_WARMUP_SECS`=8s) so the slow GStreamer/PyGObject cold-start doesn't
>    fragment the first keyframe, and **spawn gap 5s** so a starting presenter isn't reaped mid-import.
>    **RULE: a player must never hold DRM master with nothing to show.**
> 3. **`not-negotiated` (the real blocker)**: the O3 stream is **H.264 L5.2 and repeats an SPS per
>    keyframe**; the endpoint patched only the FIRST SPS, so the Pi HW decoder (max L5.1) rejected
>    the 2nd keyframe. **FIX: patch EVERY SPS → L5.1** in `feed()` (not just the first; 5.1 not 4.2,
>    so high-fps 1080p fits). mpv tolerated L5.2; the strict gst decoder does not — why this was
>    atomic+live-only. **RULE: patch every SPS, not just the first.**
>
> **New Pi dep:** `python3-gi` + gst introspection (reinstall on reflash). Debug knobs (all off by
> default): `O3_ATOMIC_STARTUP_DELAY`, `O3_ATOMIC_STALL_SECS`, `O3_ATOMIC_WARMUP_SECS`,
> `O3_ATOMIC_DUMP_DIR`, `/tmp/o3_tee_es` flag. **Next:** dual-CRTC (Stage 2 — same buffer flipped
> onto both connectors). Full arc + all fixes: **`docs/SESSION_CLOSE_2026-07-14_atomic_presenter_built.md`**.

> **Prior (2026-07-14, this session):** Built the atomic-KMS present-per-frame presenter
> **`poc/pi_endpoint/atomic_drm_present.py`** and wired it in as **`O3_HDMI_PLAYER=atomic`**
> (mpv stays the committed default — fully opt-in, zero regression). Architecture: in-process
> GStreamer **appsink HW decode** (`v4l2h264dec ! I420 ! appsink`) → memcpy each frame into a
> DRM **YU12 dumb buffer** (vc4 scans YU12 natively) → **`drmModePageFlip`**, with
> **`drmModeSetCrtc` (modeset) run exactly ONCE** on the first frame — page-flips afterward
> can't drop the Atomos sync like kmssink's per-spawn `force-modesetting` did.
> **Deviation from the pivot spec:** appsink can't get true zero-copy dmabuf here
> (`v4l2h264dec` hands appsink sysmem — appsink doesn't drive the alloc query kmssink does), so
> we HW-decode + one ~3 MB/frame memcpy (negligible on a Pi 4). **PROVEN headless:** decode +
> layout (I420 stride1920/padded1088, PNG-eyeballed clean), decode ~71fps, connector 44 / crtc
> 114 / 1080p60, **`AddFB2(YU12)` succeeds**. **NOT yet proven (owner eyes-on, touches monitor):**
> the single live `SetCrtc` + page-flip cadence + actual smoothness vs mpv. New Pi dep INSTALLED:
> `python3-gi` + gst introspection (reinstall on reflash). Full handoff + owner test commands:
> **`docs/SESSION_CLOSE_2026-07-14_atomic_presenter_built.md`**. mpv 50fps below remains the
> shipping baseline/fallback.

**What works on hardware (`o3pi` / `192.168.1.57`, Pi 4 Model B 8GB):**
- Goggles (USB host) → Pi dwc2 raw-gadget (AOA accessory) → deframe `55 CC 4A 57` →
  **H.264 High 1920×1080** live USB **~50 AU/s** (not 60), ~5–8 Mbit/s ES.
- **No phone, no DJI Fly.** Continuous past the ~40s watchdog.
- **Single HDMI:** **mpv** zero-copy on **HDMI-A-2 (far)**.
- **Root cause (this session):** forced `container-fps=60` while `au_feed≈50` → either
  ~15 unique fps (`framedrop=vo`) or slow-mo + play-after-drone-off (`framedrop=no`).
  Twin flights: goggles DVR 50 unique fps vs Pi ~15 then buffered mess. SPS VUI often
  **lies** (says 60); **measure `au_feed`**.
- **Fix on Pi (owner formal eyes-on pending):** content **50** + **`framedrop=vo`** +
  cache **0.12s / ~768 KiB** + queue **~1s AUs** + **capped prebuffer** + IPC stats +
  **adaptive FPS** from `au_feed` (goggles 50/60/100/120 changes → respawn mpv).
  Journal ~13:45: `au_feed≈50` `vf_fps=50` prebuffer 4 units (healthy pattern).
- **Dual HDMI:** **PARKED** — see `docs/DUAL_HDMI_PARKED.md`.
- systemd **`o3-hdmi`**: `O3_HDMI_PLAYER=atomic`, `O3_DUAL_HDMI=0`,
  `DRM_CONNECTOR=HDMI-A-2`. Lazy SPS start; unplug → idle (no empty player).

**Session close (this):** `docs/SESSION_CLOSE_2026-07-14_night_fps50_adapt.md`  
**Root-cause detail:** `docs/SESSION_CLOSE_2026-07-14_fps50_live.md`  
**Twin 15 fps analysis:** `docs/SESSION_CLOSE_2026-07-14_chop_15fps.md`  
**Dual park:** `docs/DUAL_HDMI_PARKED.md`

**Day-to-day field use:**
```
# ONE monitor on far micro-HDMI (HDMI-A-2). Leave near unplugged.
# Pi powers on → o3-hdmi waits (console should show; not permanent black)
# Air unit ON, live video in goggles, then:
#   plug goggles → Pi USB-C (VBUS-isolated; Pi powered GPIO/PoE, NOT USB-C data)
journalctl -u o3-hdmi -f
# Prefer IP if mDNS flaky: ssh pi@192.168.1.57
# After code push:
scripts/push_to_pi.sh
# ⚠ UNPLUG goggles BEFORE systemctl stop/restart o3-hdmi (gotcha #4: dwc2 oops / D-state)
ssh pi@192.168.1.57 'sudo ~/o3test/scripts/o3_hdmi_safe_stop.sh --restart'
```

**Key paths:**
| Role | Path |
|---|---|
| Endpoint | `poc/pi_endpoint/raw_endpoint.py` |
| Atomic-KMS presenter (`O3_HDMI_PLAYER=atomic`) | `poc/pi_endpoint/atomic_drm_present.py` |
| Idle splash (`O3_IDLE_SPLASH=1`) | `poc/pi_endpoint/idle_splash.py` |
| Dual HDMI presenter | `poc/pi_endpoint/dual_drm_mirror.py` |
| Gold arm + light sustain | `poc/pi_endpoint/gold_app_seq.py` (`APP_SEQ`, `SUSTAIN_SEQ`) |
| Deframer | `analysis/video_deframe.py` |
| Boot wrapper | `scripts/o3_hdmi_run.sh` |
| Safe stop / phantom UDC | `scripts/o3_hdmi_safe_stop.sh` |
| Install/enable | `scripts/install_hdmi_service.sh` (also oops watchdog) |
| Unit | `poc/pi_endpoint/o3-hdmi.service` → `/etc/systemd/system/` |
| Oops watchdog | `scripts/o3_oops_watchdog.sh` + `o3-oops-watchdog.service` |
| Preflight / booth health | `scripts/event_health.sh` |
| Multi-hour soak | `scripts/o3_soak.sh` → `/var/log/o3/soak_*/` |
| Manual bench (records .h264) | `scripts/phaseB_standalone.sh -v --hdmi` |
| HDMI player notes | `poc/pi_endpoint/README.md` + session close LOCKED |

### HDMI player baseline — DO NOT REGRESS (50 fps + adapt 2026-07-14 night)
- **Zero-copy:** `--vo=gpu --gpu-context=drm --hwdec=v4l2m2m` (`--hwdec-extra-frames=5`).
- **Content rate measured ~50 AU/s** (goggles DVR 50). Start **`--container-fps-override=50`**;
  **adapt** from `au_feed` if goggles change FPS (`O3_MPV_FPS_ADAPT=1`). Lock with
  `O3_MPV_CONTAINER_FPS`. Display panel: `--display-fps-override=60`.
  **Never hardcode content 60** while feed is 50 — banks lag / slow-mo / 15 fps chop.
  SPS VUI is **hint only** (often says 60 incorrectly).
- **Jitter cushion:** `--cache=yes --demuxer-readahead-secs=0.12 --cache-secs=0.12`
  `--demuxer-max-bytes≈786432` @50 (scales with fps). Env: `O3_MPV_CACHE_SECS` /
  `O3_MPV_MAX_BYTES`. **Never** ≥0.35s/4 MiB. Hard clamp ≤0.28s.
- **Framedrop:** `--framedrop=vo` (correct content fps; `no` + wrong 60 = delay line).
- **IPC stats:** `/tmp/o3-mpv.sock` → journal `au_feed` / `vf_fps` / drops.
- **SPS L5.2→L4.2** before mpv (high-fps may need `O3_H264_LEVEL_PATCH=0`).
  AUD coalesce + **capped** soft prebuffer (never dump whole queue).
- **Never** `--profile=low-latency` / `--cache=no`; never `display-desync` / `display-vdrop`.
- Hand-off **~1s of AUs** at current fps (start 48 @50); overflow → **SPS re-align**.
- Pipe **384 KiB**; arm full gold **once** then **`SUSTAIN_SEQ`**.
- Deframer mid-frame SM; peel AOA; `resyncs=0` field.
- Unplug: `idle_until_video`. mpv `nice -n -10`.

### Dual HDMI — PARKED (2026-07-13 night)
Code kept (`dual_drm_mirror.py`, env knobs) but **boot service has dual off**.
Live dual was not field-stable. Full notes + resume checklist:
`docs/DUAL_HDMI_PARKED.md`. Do not re-enable `O3_DUAL_HDMI=1` in the unit without that
checklist. Dead ends: dual-mpv, dual kmssink tee, fbdev crop, non-EDID modeset
(A-1 1024×768 phantom).

### How we got here (compressed history — full detail in `docs/protocol_notes.md`)
1. RNDIS/PC-as-host recon (2026-07-03) — useful personality, **not** the DJI Fly video path.
2. Phone is AOA peripheral, goggles are USB host (2026-07-04).
3. Pi-4 MITM Phase A gold capture natural flow (2026-07-13 morning).
4. Phase B standalone: AOA handshake + gold_app_seq replay + GIL fix + sustain + deframer
   throughput → continuous H.264 (same day).
5. Live HDMI: zero-copy mpv + shallow cache baseline; boot service + reconnect re-arm.
6. Dual HDMI offline full-frame: `dual_drm_mirror` + EDID-only modeset (2026-07-13 evening).

### Remaining (next session)
- **#1 Owner eyes-on** of 50 fps + short buffer + adapt vs goggles / DJI Fly (live? smooth?
  lag? drone-off dies ~1s?). See `docs/SESSION_CLOSE_2026-07-14_night_fps50_adapt.md`.
- Residual polish if needed: occasional 50-chunk back-pressure; climbing `mpv_drop`.
- Optional: change goggles FPS → confirm `content fps adapt` log; 100–120 trial.
- Backlog: console idle; GLSL if metrics green; re-stream; arming trim; packaging.
- **Dual resume** only per `docs/DUAL_HDMI_PARKED.md`.
- Bitrate: ~7–8 Mbit/s is product ceiling unless new quality-delta capture appears.

## 🎯 PRIOR DIRECTION (2026-07-10): Raspberry Pi 4 as USB MITM — DONE
The MITM pivot unblocked the trigger that static RE / Frida / blind enum could not.
- **Phase A — capture:** goggles → Pi[usb-proxy] → phone(DJI Fly). Gold natural-flow
  capture on hardware. Runbook: `docs/pi4_mitm_setup.md`.
- **Phase B — standalone:** `poc/pi_endpoint/raw_endpoint.py` — **working** (see CURRENT STATUS).
- **#1 hardware gotcha (still true):** isolate VBUS on goggles→Pi USB-C; power Pi from
  GPIO/PoE (never the USB-C data port).

## Prior art to read before writing code
- `fpv-wtf/voc-poc` — reference for the V1/V2 approach + NAL handling (different transport, still useful).
- `Amzd/DJI-FPV-Goggles` — RE notes; this is where the RNDIS 192.168.42.x clue surfaced.
- `dji-sdk/Windows-SDK` issue #68 — original RNDIS observation thread.
- fpv.wtf Discord — ask whether anyone has partial O3 captures before duplicating effort.

## Methodology (phases)
**NOTE (2026-07-04): the phases below describe the RNDIS/IP transport, which recon
showed is NOT what DJI Fly actually uses (see MAJOR CORRECTION above) — they still
apply conceptually (capture → analyze → replay → harden) but Phase 1-3 for the *real*
video path now happen over `poc/aoa_sniffer/` (a sideloadable, no-root Android app that
grabs the same AOA accessory as DJI Fly) instead of RNDIS sockets. As of this note:
outer AOA transport framing is fully confirmed, a live DUML command round-trip
(send + get a real response back) works, and the actual live-view trigger command is
narrowed down to `SpecialCommandManager::RequestIFrameForLiveView` (found via native
library strings in DJI Fly's APK) but not yet extracted byte-for-byte. See
`docs/protocol_notes.md`'s "AOA/logiclink protocol" section for full detail and next
steps before repeating this work.**

### Phase 0 — Recon the goggles as a network device
Goggles powered + linked to the air unit and showing video, plugged into the Linux box.
- `dmesg | grep -i rndis` and `ip addr` → identify the usb net interface + subnet.
- Bring up the link (DHCP or static per what we observe), confirm goggles IP.
- `nmap -p- <goggles-ip>` → enumerate open ports on the USB link; note which are present vs the wifi scan.
- Deliver: a repeatable `phase0_recon.sh` that brings up the iface, runs nmap, and logs results with timestamps.

### Phase 1 — Capture DJI Fly's working session
DJI Fly (on a real/rooted Android) is the only client that knows the handshake. Record it working.
- Preferred: rooted Android running DJI Fly + `tcpdump` on the RNDIS interface → full `.pcap`
  (connect → stream start → ~30s streaming → disconnect).
- Cross-check: passive USB capture (`usbmon` on Linux / USBPcap on Windows), let Wireshark dissect RNDIS→IP.
- Save EVERY capture with firmware version + timestamp in the filename. Never overwrite.

### Phase 2 — Analyze the capture
- Split channels in Wireshark:
  - **Control**: small, bidirectional, on one of the service ports (check 890x / 1415 first). The
    request DJI Fly sends right before video flows is the "start stream" trigger.
  - **Video**: the fat one-way flow (goggles→host). Follow it, dump payload.
- Hunt for H.265 NAL start codes (`00 00 00 01`, VPS/SPS/PPS NAL headers). Clean boundaries = thin
  wrapper. If absent, peel the DJI framing (length-prefix? RTP-ish? custom header?).
- Identify keepalives/heartbeats (periodic small packets) we'll need to replay.

### Phase 3 — Replay from scratch (no phone)
- On the Linux box: bring up RNDIS, open a socket to the control port, send the captured handshake,
  answer keepalives, read the video socket, strip framing, write raw `.h265`.
- Validate with `ffplay` (or pipe live). Flight footage on screen with no DJI app = cracked.
- Deliver a minimal Python PoC (sockets are fine — no libusb since it's IP-over-RNDIS).

### Phase 4 — Harden + ship
- Replace captured constants with real parsing, handle reconnects, pin firmware, document, publish
  on GitHub, post to fpv.wtf Discord. Then wire up Pi 4 → HDMI out (all-day, mains/power-bank powered).

## Toolchain (install if missing)
```
sudo apt update && sudo apt install -y wireshark tcpdump nmap ffmpeg python3-pip usbutils net-tools
sudo modprobe usbmon
```
Python analysis: prefer `scapy` / `pyshark` for pcap parsing. `ffplay` (from ffmpeg) for stream validation.

## Immediate next tasks (for next agent / session)
**Handoff:** `docs/SESSION_CLOSE_2026-07-16_seamless_splash.md` ← **read first**.

1. **#1 ✅ DONE — Seamless splash ↔ video** (owner “solid on both counts”, 2026-07-16 evening).
2. **#2 `o3_soak.sh` 5m PASS** (still open — needs cool drone continuous stream):
   ```bash
   ssh pi@192.168.1.57
   # air unit ON, goggles plugged, live HDMI good, cool drone:
   sudo ~/o3test/scripts/o3_soak.sh
   cat /var/log/o3/soak_*/summary.txt   # want RESULT: PASS
   ```
3. ✅ DONE earlier: idle splash; hot-swap; event_health; oops WD; atomic default.
4. Backlog: dual-CRTC; re-stream; packaging; 1440×1080 root cause curiosity; long-idle stale
   endpoint re-arm on real plug.

**Gotchas (atomic path):** decode is now **SW** (`avdec_h264`, not `v4l2h264dec` — HW decode was
dropped 2026-07-15, it reconstructs chroma wrong at high-contrast edges, see status above); new
dep `gstreamer1.0-libav` (auto-installed by `install_hdmi_service.sh`, reinstall on reflash) on
top of the existing `python3-gi`+gst introspection dep; never leak compressed ES (only drop
decoded frames); presenter modesets ONCE + page-flips after; **idle splash is painted
in-process (keep DRM master)** on stall/SIGUSR1 — do not kill+external-splash for feed→idle;
**splash→video:** keep external splash until `NEED_MASTER`, or SIGUSR2 resume after idle-hold
(never hide splash before atomic cold-start); `/tmp` wiped on reboot; unplug goggles before any
service stop; **decoded frame resolution is NOT guaranteed to match the DRM mode** —
`build_pipeline()` now forces a `videoscale add-borders=true` + pinned `pixel-aspect-ratio=1/1`
stage so the presenter always gets a fixed canvas letterboxed to the source aspect (2026-07-15,
field-proven necessary — do not remove without re-verifying against a 4:3 stream); phantom-unplug
re-arm can (rarely) crash the kernel (`raw_gadget.ko`) if it races real bus activity — only a
reboot recovers, mitigated by `o3-oops-watchdog`.

**Shipping baseline:** atomic-KMS + SW decode (this section). Historical mpv 50fps + adaptive
notes remain in `docs/SESSION_CLOSE_2026-07-14_night_fps50_adapt.md` (fallback only).
Do not hardcode content-fps 60. Dual parked: `docs/DUAL_HDMI_PARKED.md`.
Event hardening backlog: `docs/EVENT_PRODUCTION_QA.md`.

## Open questions / unknowns (remaining)
- Minimal subset of `gold_app_seq.APP_SEQ` that still arms video (currently full 218-pkt loop).
- Whether DJI system OSD on goggles can be fully blanked (no known stock toggle; BF OSD is switchable).
- Higher-bitrate USB live-view mode: **unlikely** — gold has no quality SET; phone ES ≈
  standalone ≈ 7 Mbit/s; Cosmo cites ~8–10 Mbps. Optional UI-toggle MITM still open.
- Network re-stream (not started).
- Historical RNDIS/FTP side quests still open; not needed for the live AOA path.

## Risks & gotchas
- **Gotcha #4 (critical):** never stop raw-gadget / `systemctl stop o3-hdmi` while goggles are
  still attached — can oops dwc2 and wedge the UDC until reboot. **Unplug first.**
- **VBUS isolation** on goggles→Pi USB-C; power Pi from GPIO/PoE, not the data USB-C port.
- **Undervoltage** wedges the gadget easily — keep a solid 5V supply (`throttled=0x0`).
- **Firmware drift**: pin goggle firmware once field-stable.
- **Only one video consumer at a time**: onboard DVR stops while USB video-out is active.
- **HDMI player regressions**: see LOCKED baseline above — measured on Shinobi 2026-07-13.

## Working conventions
- Measure, don't assume — every "known" IP/port above is provisional until confirmed on our hardware.
- Preserve all captures; treat `captures/` as append-only.
- Keep the PoC dependency-light and readable so it's easy to open-source and for others to port to the Pi.
- Document findings in `docs/` as we go (protocol notes, port map, framing) — that doc IS the contribution.
- Prefer `scripts/push_to_pi.sh` then re-run `install_hdmi_service.sh` for deploy; don't hand-edit Pi copies.

## Environment
- Laptop/dev: Dell Latitude 5510, Ubuntu (USB boot), SATA **AHCI**.
- Deployment: **Raspberry Pi 4 Model B 8GB** (`o3pi.local` / LAN), user `pi`, passwordless sudo.
- Target: DJI Goggles 2 + O3 air unit. HDMI monitor on **HDMI-A-1**.
- Capture phone (Phase A, optional now): rooted Android + DJI Fly.
