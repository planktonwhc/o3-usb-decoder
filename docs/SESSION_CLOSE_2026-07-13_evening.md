# Session close — 2026-07-13 evening (Grok)

## For next session (priority: raise ~7 Mbit/s source)

Read in order:

1. `CLAUDE.md` (CURRENT STATUS + this session’s dual-HDMI / quality notes)
2. This file
3. `docs/protocol_notes.md` — top “Dual HDMI + presentation quality” + HDLnk probe table
4. `poc/pi_endpoint/README.md` — locked HDMI baseline (do not regress delivery flags)

**Next session goal:** increase the **encoded** USB live-view bitrate (today ~7 Mbit/s
H.264 High 1080p60 ES). Player polish is done; more sharpen will not invent detail.

---

## Wins this session

### Field
- Owner outdoor field test: **core path works** (AOA → deframe → HDMI, no phone).
- Feedback: picture “not ultra-impressive” → traced to **source ES ~7 Mbit/s**, not player.

### Quality (presentation only)
- Single-port mpv: GLSL `ewa_lanczossharp` + mild `deband` + `sharpen=0.45` (still
  zero-copy DRM-PRIME). **No** `--vf lavfi` (would force CPU download / breakup).
- Measured: gold + standalone ES both **~6.9–7.0 Mbit/s**, 1920×1080, ~60 fps, High profile.
  Earlier “~27–30 Mbit/s” was USB bulk framing, not elementary-stream bitrate.

### Dual HDMI
- `DRM_CONNECTOR=auto` (service default):
  - **1 port** → mpv quality path
  - **2 ports** → GStreamer `decodebin ! tee ! kmssink×2` (one process; dual-mpv =
    DRM master Permission denied)
  - Hotplug watcher ~2s
- GStreamer deps installed on Pi via `install_hdmi_service.sh`.
- Code: `poc/pi_endpoint/raw_endpoint.py` (`HdmiPlayer`, `list_hdmi_connectors`,
  `default_gst_cmd`, quality flags in `default_player_cmd`).
- Runner/unit: `scripts/o3_hdmi_run.sh`, `poc/pi_endpoint/o3-hdmi.service`.

---

## Pi state at close

| Item | Value |
|---|---|
| Host | `o3pi.local` / `192.168.1.57`, user `pi` |
| Service | **`o3-hdmi` active + enabled** (leave running) |
| Connector mode | `auto` — both HDMI-A-1 and A-2 report connected → **gstreamer dual** |
| Goggles | not attached at close (`udc=not attached` earlier; re-check before stop) |
| Deploy | laptop `scripts/push_to_pi.sh` then `ssh pi@o3pi.local 'sudo ~/o3test/scripts/install_hdmi_service.sh'` |

**Gotcha #4 still applies:** unplug goggles **before** `systemctl stop o3-hdmi`.

**HDMI-A-1 note:** sysfs `connected` but **0-byte EDID** (no real modes); A-2 has real
1080p EDID. If A-1 is black in the field, check cable/adapter — may be phantom connect.

Safe to power down air unit / goggles / monitors. Pi can stay on with service waiting.

---

## Do not relitigate / do not regress

- RNDIS as the DJI Fly video path (it’s AOA).
- `--profile=low-latency` / `--cache=no` / untimed+desync+nobuffer on mpv live path.
- Tiny hand-off queue (`max_chunks` stay 512).
- CPU `--vf lavfi=unsharp…` for “more quality”.
- Dual independent mpv on same DRM card (proven fail).
- Fabricating flight-safety SPECIAL low ids without owner sign-off.

---

## Next session: raise ~7 Mbit/s — plan

### Facts already nailed

| Fact | Evidence |
|---|---|
| ES ~7 Mbit/s 1080p60 High | `captures/pi_pull/2026-07-13_phaseB_STANDALONE_clean.h264` (~6.91 Mb/s); gold `video_extract/hardened.from_sps.h264` (~6.94 Mb/s) |
| Gold USB bulk goggles→app | ~7–10 Mbit/s steady (`…_GOLD_ANALYSIS.txt`) — consistent with ES |
| Onboard DVR | H.264 1080p50/60 **~18–26 Mbit/s** — **different pipeline**; proves camera can encode harder, not that USB out does |
| `gold_app_seq.APP_SEQ` | **No cmd_set=9 (HDLnk)** packets in first 6s |
| Camera traffic in gold | `cs=2/0xeb` (subscribe/keepalive-ish) + `0xe8` + a few others — not obvious bitrate sets |
| Prior blind HDLnk | `9/0x34` Mode Set + `9/0x39` Config Set **accepted (0x00)** on air unit but **never** started video (pre-Phase-B AOA probing). Payloads were guesses. |

### Ranked approaches (next session)

**A. Capture a quality-delta session (highest EV)**  
If DJI Fly or goggles UI has a transmission / liveview quality setting, change it while
recording a Phase A natural-flow MITM (or a long Phase B `.h264` with/without phone).

1. Baseline: current gold already = “whatever phone requested that day”.
2. New capture with quality maxed in app (if any control exists).
3. Diff **app→goggles** DUML (`cs=2`, `cs=9`, `cs=8`, `cs=7`) in the first 10s and
   around any UI toggle → new SET payloads to inject after arming.
4. Measure ES bitrate: `ffmpeg -i live.h264 -f null -` + size/duration, or live
   `raw_endpoint` stats.

**B. Mine gold full log for quality-related DUML beyond APP_SEQ window**  
`gold_app_seq` is only first **6s** of packet_type=0. Later steady-state might set
bitrate. Script idea:

```bash
# on laptop — parse full gold payload for cs in {2,7,8,9} unique payloads
python3 -c '...'  # extend analysis/aoa_capture_parse or new analysis/bitrate_hunt.py
```

Source: `captures/pi_pull/2026-07-13_103110_pi-mitm_natural_payload.log`

**C. Structured HDLnk Config Set (`9/0x39`) after video is already flowing**  
Once Phase B is live, inject accepted configs and watch ES bitrate (not bulk only):

- Target **air unit (type 9)** first; goggles (14) second.
- Record before/after 10s windows of deframed ES Mbit/s.
- Do **not** spam flight-safety SPECIAL `cmd_set=1` ids `0x00–0x05` blindly.
- Prefer capture-derived payloads over random bytes; if probing, log every
  (cmd_set, cmd_id, target, payload_hex, resp_code, bitrate_delta).

**D. CosmoStreamer / community reference (read-only)**  
Any public note that USB out can do 20+ Mbit/s would prove a higher mode exists.
If Cosmo only gets ~7 too, we may be at the product ceiling for USB consumer encode.

**E. Phone-path A/B bitrate**  
Record Phase A gold-style extract while phone shows FPV; compare ES rate to standalone.
If phone also ~7, app settings are the only lever; if phone higher, our gold_app_seq is
missing a quality SET.

### Success criteria

| Result | Meaning |
|---|---|
| ES ≥ ~15 Mbit/s sustained, clean decode | win — document command + add to arming seq |
| ES stays ~7 after exhaustive gold + UI toggles | likely product ceiling for USB out; document |
| Bitrate jumps but breakup | raise player cache slightly; do not drop zero-copy |

### Suggested first commands next session

```bash
# 1) Confirm current live ES rate on a short Phase B record (goggles + air unit on)
scripts/push_to_pi.sh
ssh pi@o3pi.local
# UNPLUG goggles before stop if attached
sudo systemctl stop o3-hdmi
sudo ~/o3test/scripts/phaseB_standalone.sh -v --hdmi
# after ~20s video, unplug goggles, Ctrl-C; pull .h264; measure bitrate

# 2) Offline gold hunt for quality cmds (laptop)
# new analysis script or one-shot parse of natural_payload.log → unique (cs,cid,payload)

# 3) If a candidate SET is found, inject after stream start in raw_endpoint
#    (post-SPS hook) and A/B measure ES Mbit/s
```

### Code hooks likely to touch

| File | Role |
|---|---|
| `poc/pi_endpoint/gold_app_seq.py` | extend / append quality SETs |
| `poc/pi_endpoint/raw_endpoint.py` | optional post-video inject + bitrate stats log |
| `analysis/` | new `bitrate_hunt.py` over gold payload |
| `docs/protocol_notes.md` | record accepted payloads + measured Mbit/s |

---

## Files touched this session (quality + dual HDMI)

- `poc/pi_endpoint/raw_endpoint.py` — multi-HDMI player, quality mpv flags, gst dual
- `poc/pi_endpoint/o3-hdmi.service` — `DRM_CONNECTOR=auto`
- `scripts/o3_hdmi_run.sh` — auto connector, DRM status log, kill stale mpv/gst
- `scripts/install_hdmi_service.sh` — apt install gstreamer if missing
- `poc/pi_endpoint/README.md`, `README.md`, `CLAUDE.md`, `docs/protocol_notes.md`

---

## One-line handoff

Field HDMI works; dual-HDMI auto shipped; picture limited by **~7 Mbit/s USB ES** —
next session hunt capture-derived or accepted HDLnk/camera SETs to raise encode bitrate.
