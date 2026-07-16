# Session close — 2026-07-14 field excellent (Grok)

Owner field-tested after a day of delivery/latency/console fixes. Verdict:
**“excellent, very close to ideal.”** Next session = polish, not core rescue.

---

## For next session

Read in order:

1. `CLAUDE.md` (CURRENT STATUS — re-read after this file)
2. **This file** (LOCKED field config + regressions to avoid)
3. `docs/GROK_STATE.md`
4. `docs/DUAL_HDMI_PARKED.md` (only if dual — default **do not**)

### Next session goals (priority: polish)

1. **Do not break the excellent path** — treat LOCKED config below as sacred  
2. Presentation polish (optional GLSL sharpen/deband **only** if zero-copy + live metrics stay green)  
3. Console idle polish (unplug → console more reliable; kill leftover mpv)  
4. Network re-stream (Pi → LAN)  
5. Arming-set trim (`SUSTAIN_SEQ` / minimal `APP_SEQ`)  
6. Packaging / public-repo polish  
7. Dual **only** per `DUAL_HDMI_PARKED.md` checklist  

---

## Wins this session (2026-07-14)

| Item | Result |
|---|---|
| Owner eyes-on field | ✅ **“excellent, very close to ideal”** |
| Continuous ~7–8 Mbit/s H.264 | ✅ `resyncs=0`, `drop_tot=0` on excellent flight |
| Phantom UDC / stuck `configured` | ✅ host-liveness re-arm + `o3_hdmi_safe_stop.sh` |
| Full gold re-arm spam (choppy) | ✅ arm once → **`SUSTAIN_SEQ` light keepalives** |
| Deframer resync≈chunks / bitty | ✅ AOA/video state machine + magic-prefix tail |
| Mid-GOP queue drops (unflyable) | ✅ SPS re-align on backpressure (no drop-oldest) |
| Painful lag from deep cache | ✅ reverted to **0.12s / 1 MiB** (was 0.35s / 4 MiB) |
| Black screen idle (no console) | ✅ `idle_until_video` + console rebind (polish remaining) |
| Dual HDMI | ⏸ still **PARKED** |

### Excellent flight snapshot (journal ~08:48–08:49)

```text
gold arm done → light keepalives
~5.6–8.5 Mbit/s continuous (mostly ~7–8)
resyncs=0  drop_tot=0  hdmi_drop=0  tx_fail=0 (while streaming)
SUSPEND → phantom unplug re-arm
mpv flags: readahead=0.12 cache=0.12 demuxer-max-bytes=1048576 framedrop=vo
```

---

## Working field config (LOCKED — do not regress)

### Service env

```text
O3_HDMI_PLAYER=mpv
O3_DUAL_HDMI=0
DRM_CONNECTOR=HDMI-A-2          # far micro-HDMI only
# leave near HDMI-A-1 unplugged
```

### mpv (`default_player_cmd`) — smoothness polish 2026-07-14 evening

```text
--vo=gpu --gpu-context=drm --hwdec=v4l2m2m
--hwdec-extra-frames=3
--drm-mode=1920x1080
--cache=yes --cache-pause=no
--demuxer-readahead-secs=0.18        # was 0.12; still << painful 0.35
--cache-secs=0.18
--demuxer-max-bytes=1572864          # 1.5 MiB integer (was 1 MiB)
--demuxer-max-back-bytes=0
--no-correct-pts --container-fps-override=60
--display-fps-override=60
--video-latency-hacks=yes
--interpolation=no
--framedrop=vo
--demuxer=lavf --demuxer-lavf-format=h264
--demuxer-lavf-probesize=32768 --demuxer-lavf-analyzeduration=0
# process: nice -n -10 mpv …
# Env knobs: O3_MPV_CACHE_SECS O3_MPV_MAX_BYTES O3_MPV_HWDEC_EXTRA_FRAMES
```

### Hand-off / sustain / deframe

| Piece | Locked choice |
|---|---|
| Queue | `max_chunks=512`; overflow → **clear + SPS re-align** (never mid-GOP drop-oldest) |
| Pipe | `F_SETPIPE_SZ` **384 KiB** (was 256; still not 1 MiB lag stack) |
| Spawn grace | **1.0 s**, grace queue ≤ 2× max |
| Arming | Full `APP_SEQ` **once**, then loop **`SUSTAIN_SEQ`** (~1 s, heartbeats + 2/0xeb) |
| Deframer | Mid-frame no magic scan; peel AOA; magic-prefix idle tail; strip zero pad |
| SPS level | **Patch L5.2→L4.2** before mpv (Pi v4l2 menu max 5.1; 1080p60 needs 4.2) |
| AU coalesce | Annex-B **AUD access units** into queue (whole frames, not 4 KiB slices) |
| Soft prebuffer | **4 frames / 48 KiB** before first stdin write after SPS (anti-underrun hitch) |
| Unplug | `idle_until_video` — kill mpv, release DRM/console; **no empty mpv** |
| Safe stop | `scripts/o3_hdmi_safe_stop.sh` — UDC `configured` alone ≠ host present |

### Never reintroduce on live mpv

- `--profile=low-latency` / `--cache=no`
- `--video-sync=display-desync` / `display-vdrop`
- `--untimed` + desync + lavf `nobuffer`
- Deep cache “to fix chop” (0.35s / 4 MiB → **painful lag**, owner eyes-on)
- Full `APP_SEQ` loop forever (bitrate 8→0 thrash)
- Drop-oldest mid-GOP on hand-off queue
- Dual in boot unit without park checklist
- Stop/restart service while goggles attached (gotcha #4 / D-state wedge)

---

## Code / ops map

| Role | Path |
|---|---|
| Endpoint + player | `poc/pi_endpoint/raw_endpoint.py` |
| Gold arm + sustain | `poc/pi_endpoint/gold_app_seq.py` (`APP_SEQ`, `SUSTAIN_SEQ`) |
| Deframer | `analysis/video_deframe.py` |
| Safe stop / clear phantom | `scripts/o3_hdmi_safe_stop.sh` |
| Boot wrapper | `scripts/o3_hdmi_run.sh` |
| Unit | `poc/pi_endpoint/o3-hdmi.service` |
| Install | `scripts/install_hdmi_service.sh` |

Deploy: `scripts/push_to_pi.sh` then (goggles **unplugged**):

```bash
ssh pi@192.168.1.57   # o3pi.local mDNS sometimes flaky
sudo ~/o3test/scripts/o3_hdmi_safe_stop.sh --restart
# or: sudo systemctl restart o3-hdmi   # only if udc=not attached
journalctl -u o3-hdmi -f
```

---

## Pi state at close

| Item | Value |
|---|---|
| Host | `o3pi` / **`192.168.1.57`** (prefer IP if mDNS fails) |
| Service | **`o3-hdmi` enabled + active** (leave running) |
| Env | `O3_HDMI_PLAYER=mpv` `O3_DUAL_HDMI=0` `DRM_CONNECTOR=HDMI-A-2` |
| Player | mpv zero-copy **0.12s / 1 MiB** (excellent field) |
| Monitors | A-2 connected edid=256; A-1 disconnected |
| Goggles | may still show `udc=configured` phantom after unplug — normal; re-arm handles it |
| throttled | `0x0` |
| Gotcha #4 | unplug goggles **before** stop/restart; D-state needs **power cycle** |

---

## Session narrative (compressed)

1. Field: bitty/choppy — not wrong dual flags; bulk-IN EAGAIN + mid-GOP drops + full gold loop.  
2. Phantom UDC `configured` with goggles unplugged blocked restarts → safe_stop + host liveness.  
3. Light sustain fixed 8→0 Mbit stalls; still choppy vs DJI Fly.  
4. Deframer always-magic-scan → resync≈chunks; fixed state machine → resyncs=0.  
5. Deep cache for “smoothness” → **painful lag** (owner); reverted shallow.  
6. Empty mpv after unplug → black no-console; idle_until_video.  
7. Owner: **excellent, very close to ideal.**

---

## Polish backlog (next session — not blockers)

- **Eyes-on A/B of smoothness polish** (this session, code on Pi, needs safe restart):
  L5.2→L4.2 SPS patch, 0.18s/1.5 MiB cushion, AUD coalesce, 4-frame prebuffer,
  `display-fps-override=60`, `nice -10`. Expect: less micro-stutter vs DJI Fly,
  lag still far below rejected 0.35s. If still choppy: try `O3_MPV_CACHE_SECS=0.22`
  (cap 0.28). If laggy: `O3_MPV_CACHE_SECS=0.14`.
- Console return after unplug 100% reliable (kill leftover mpv if any; fbcon rebind)  
- Optional presentation GLSL on single-port only (must not regress delivery metrics)  
- Network re-stream  
- Trim `SUSTAIN_SEQ` / measure minimal arm set  
- Packaging  
- Optional: multi-hour soak test metrics  
- Dual still parked  

Healthy live journal pattern:

```text
[*] gold arm done … light keepalives
[*] video … ~7–8 Mbit/s resyncs=0 drop_tot=0 hdmi_drop=0 tx_fail=0
[*] phantom unplug / idle — releasing display until next video
```
