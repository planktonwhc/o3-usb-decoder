# Session close — 2026-07-14 smoothness polish (Grok)

Goal: close the remaining **choppy vs DJI Fly** gap on the already-excellent single
HDMI path. Core path left green; dual stayed parked. **Eyes-on A/B of this polish
not yet reported** — code is on the Pi and service is waiting for goggles.

---

## For next session

Read in order:

1. `CLAUDE.md` (CURRENT STATUS)
2. **This file** (what changed + field A/B checklist)
3. `docs/SESSION_CLOSE_2026-07-14_field_excellent.md` (prior “excellent” flight baseline)
4. `docs/GROK_STATE.md`
5. `docs/DUAL_HDMI_PARKED.md` only if dual — default **do not**

### Next session goals (priority)

1. **Eyes-on A/B** of smoothness polish vs prior excellent path (owner verdict)  
2. If still choppy: nudge `O3_MPV_CACHE_SECS` (0.18 → 0.22, cap 0.28) — not 0.35  
3. If laggy: drop to 0.14–0.15 (night-single “all good” zone)  
4. Keep single path green; dual parked  
5. Remaining polish backlog: console idle, optional GLSL (metrics-green only),
   network re-stream, arming trim, packaging  

---

## Wins this session

| Item | Result |
|---|---|
| Diagnosis | Delivery already clean (`resyncs=0`); chop = presentation / USB micro-jitter + L5.2 on v4l2≤5.1 |
| SPS L5.2→L4.2 on **mpv** path | ✅ same idea as dual; Pi bcm2835 menu max is **5.1** |
| Jitter cushion | ✅ **0.18s / 1.5 MiB** (was 0.12s / 1 MiB; still ≪ rejected 0.35s / 4 MiB) |
| AUD frame coalesce | ✅ whole access units into queue (not 4 KiB mid-slice crumbs) |
| Soft prebuffer | ✅ 4 frames / 48 KiB before first stdin write after SPS |
| Display lock | ✅ `--display-fps-override=60` `--drm-mode=1920x1080` |
| mpv priority | ✅ `nice -n -10` |
| Deploy | ✅ `push_to_pi.sh` + safe_stop restart (goggles unplugged) |
| Dual | ⏸ still **PARKED** |
| Owner eyes-on of polish | ⏳ **pending** (service idle waiting at close) |

### Root facts (offline, from clean Phase B ES)

- H.264 High **1920×1080**, I/P only, GOP 32, ~7 Mbit/s  
- SPS VUI: `time_scale=120000`, `num_units_in_tick=1000` → **60 fps**  
- Pi v4l2 `h264_level` menu max = **5.1**; stream tagged **L5.2** → patch to **4.2**  
- Monitor A-2: **1920×1080@60** (EDID also lists 4K — pin `drm-mode=1920x1080`)  

---

## Working field config (LOCKED after this polish)

### Service env (unchanged)

```text
O3_HDMI_PLAYER=mpv
O3_DUAL_HDMI=0
DRM_CONNECTOR=HDMI-A-2          # far micro-HDMI only
# leave near HDMI-A-1 unplugged
```

### mpv (`default_player_cmd`)

```text
--vo=gpu --gpu-context=drm --hwdec=v4l2m2m
--hwdec-extra-frames=3
--drm-mode=1920x1080
--cache=yes --cache-pause=no
--demuxer-readahead-secs=0.18
--cache-secs=0.18
--demuxer-max-bytes=1572864          # 1.5 MiB integer
--demuxer-max-back-bytes=0
--no-correct-pts --container-fps-override=60
--display-fps-override=60
--video-latency-hacks=yes
--interpolation=no
--framedrop=vo
--demuxer=lavf --demuxer-lavf-format=h264
--demuxer-lavf-probesize=32768 --demuxer-lavf-analyzeduration=0
# process: nice -n -10 mpv …
```

Env knobs (no code edit):

| Env | Default | Notes |
|---|---|---|
| `O3_MPV_CACHE_SECS` | `0.18` | hard clamp 0.08–0.28 |
| `O3_MPV_MAX_BYTES` | `1572864` | hard clamp 0.5–3 MiB |
| `O3_MPV_HWDEC_EXTRA_FRAMES` | `3` | 2–8 |
| `O3_HDMI_PREBUFFER_FRAMES` | `4` | 0–12 |
| `O3_HDMI_PREBUFFER_BYTES` | `49152` | |
| `O3_H264_LEVEL_PATCH` | `1` | `0` to disable |
| `O3_HDMI_AU_COALESCE` | `1` | `0` to disable |

### Hand-off / sustain / deframe

| Piece | Locked choice |
|---|---|
| Queue | `max_chunks=512`; overflow → **clear + SPS re-align** |
| Pipe | `F_SETPIPE_SZ` **384 KiB** |
| Spawn grace | **1.0 s** |
| Arming | Full `APP_SEQ` **once**, then **`SUSTAIN_SEQ`** |
| Deframer | Mid-frame state machine; peel AOA |
| SPS level | **L5.2→L4.2** before mpv |
| AU coalesce | AUD access units when possible |
| Soft prebuffer | 4 frames / 48 KiB after SPS |
| Unplug | `idle_until_video` |
| Safe stop | `o3_hdmi_safe_stop.sh` |

### Never reintroduce

- `--profile=low-latency` / `--cache=no`  
- `--video-sync=display-desync` / `display-vdrop`  
- `--untimed` + desync + lavf `nobuffer`  
- Deep cache **≥0.35s / 4 MiB** (owner: painful lag)  
- Full `APP_SEQ` forever  
- Mid-GOP drop-oldest  
- Dual in boot unit without park checklist  
- Stop service while goggles attached (gotcha #4)  

---

## Code / ops map

| Role | Path |
|---|---|
| Endpoint + player polish | `poc/pi_endpoint/raw_endpoint.py` |
| Gold arm + sustain | `poc/pi_endpoint/gold_app_seq.py` |
| Deframer | `analysis/video_deframe.py` |
| Safe stop | `scripts/o3_hdmi_safe_stop.sh` |
| Prior excellent close | `docs/SESSION_CLOSE_2026-07-14_field_excellent.md` |

Deploy:

```bash
scripts/push_to_pi.sh
# UNPLUG goggles first
ssh pi@192.168.1.57 'sudo ~/o3test/scripts/o3_hdmi_safe_stop.sh --restart'
journalctl -u o3-hdmi -f
```

### Field A/B checklist (next plug)

Expect after plug + live video:

```text
[*] gold arm done … light keepalives
[*] HDMI: ES aligned to SPS …
[*] HDMI: SPS level_idc patched → 4.2 (v4l2m2m)
[*] HDMI player started: mpv → HDMI-A-2
[*] video … ~7–8 Mbit/s resyncs=0 drop_tot=0 hdmi_drop=0 tx_fail=0
```

| Feel | Action |
|---|---|
| Smoother, lag OK | **Lock this baseline**; document owner verdict |
| Still choppy | `O3_MPV_CACHE_SECS=0.22` in unit env → safe restart |
| Laggy | `O3_MPV_CACHE_SECS=0.14` |
| Broken / bitty | Revert mentally to field-excellent 0.12/1 MiB; check `drop_tot` / `resyncs` |

---

## Pi state at close

| Item | Value |
|---|---|
| Host | `o3pi` / **`192.168.1.57`** |
| Time | 2026-07-14 ~09:30 BST |
| Service | **`o3-hdmi` enabled + active** (leave running) |
| Env | `O3_HDMI_PLAYER=mpv` `O3_DUAL_HDMI=0` `DRM_CONNECTOR=HDMI-A-2` |
| Player | lazy — **no mpv** until SPS (idle) |
| Monitors | A-2 connected edid=256; A-1 disconnected |
| Goggles | **unplugged** (`udc=not attached`) |
| throttled | `0x0` |
| Code | smoothness polish **on Pi** (restarted 09:15 BST) |

**Gotcha #4:** unplug goggles **before** stop/restart.  
**Phantom UDC:** `configured` alone ≠ host present — use `o3_hdmi_safe_stop.sh`.

---

## Session narrative (compressed)

1. Owner: excellent path still **choppy vs DJI Fly** — polish only, no dual, no regressions.  
2. Offline: stream is true 60 fps I/P; chop not mid-GOP drops; v4l2 max level 5.1 vs SPS 5.2.  
3. Shipped presentation path: L4.2 patch, 0.18s/1.5 MiB cushion, AUD coalesce, soft prebuffer, display 60 lock, nice.  
4. Pushed + safe_stop restart; service idle waiting.  
5. Close without owner eyes-on of the new flags — **first next-session task is that A/B**.  

---

## Files touched

- `poc/pi_endpoint/raw_endpoint.py` — all smoothness logic + mpv flags  
- `docs/SESSION_CLOSE_2026-07-14_field_excellent.md` — baseline note + backlog  
- `docs/GROK_STATE.md` — LOCKED single-port update  
- `docs/SESSION_CLOSE_2026-07-14_smoothness_polish.md` — **this file**  
- `CLAUDE.md` — CURRENT STATUS + LOCKED baseline  

---

## One-line handoff

Smoothness polish **deployed** (L4.2 + 0.18s cushion + AU prebuffer); service waiting;
**owner eyes-on A/B is the next action** — do not deepen cache past 0.28s or re-enable dual.
