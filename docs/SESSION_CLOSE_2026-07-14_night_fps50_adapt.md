# Session close — 2026-07-14 night (chop → 50 fps → adaptive)

## Read first (next session)

1. `CLAUDE.md` (CURRENT STATUS)
2. **This file**
3. `docs/SESSION_CLOSE_2026-07-14_fps50_live.md` (root-cause detail)
4. `docs/SESSION_CLOSE_2026-07-14_chop_15fps.md` (twin analysis)
5. `docs/GROK_STATE.md`
6. Dual only if needed: `docs/DUAL_HDMI_PARKED.md`

---

## Owner field arc this session

| Flight / clip | Result |
|---|---|
| OBS `2026-07-14 12-08-27.mp4` + goggles `DJI_0516.MOV` | Same flight (goggles clock wrong). Twin: goggles **50 unique fps**, Pi HDMI **~15 unique fps**. Monitor = same 15. Lag OK. Chop “barely bearable” vs Fly/goggles. |
| After `framedrop=no` | OBS `13-12-47.mp4` + `DJI_0518.MOV`. **Smoother** but **slow-mo**; drone off → **HDMI kept playing** (owner black-marker). |
| After 50 fps + short buffer + adapt | Journal live ~13:45 BST: `au_feed≈50` `vf_fps=50` `framedrop=vo` prebuffer **4 units**. **Owner formal A/B still needed.** |

Cam Link = **direct** HDMI→USB (not inventing geometry; pillarbox is native O3).

---

## Root cause (locked)

Live USB content rate is **~50 AU/s** (matches DVR 50). We forced **`container-fps-override=60`**.

| Mode | Symptom |
|---|---|
| `framedrop=vo` + wrong 60 | Chronic late → drop most frames → **~15 unique fps** |
| `framedrop=no` + wrong 60 | Bank multi-second queue → **slow-mo** + play-after-stop |
| Evidence | `au_feed≈50`, back-pressure **512** AUs, prebuffer dump **273 units / 4 MB**, ES 0 for ~13s while picture continued |

SPS VUI often says **60** with `fixed_frame_rate=0` — **hint only**, not truth. Measure `au_feed`.

---

## Shipped (on Pi, service active)

All in `poc/pi_endpoint/raw_endpoint.py` (pushed + safe_stop restarted).

### Presentation defaults (LOCKED for next plug)

| Knob | Value |
|---|---|
| `--container-fps-override` | **50** start; **adapts** from measured `au_feed` |
| `--display-fps-override` | **60** (panel) |
| `--framedrop` | **vo** |
| cache / readahead | **0.12 s** |
| `demuxer-max-bytes` | **768 KiB** @50 (scales with fps, cap 1.5 MiB) |
| hand-off queue | **~1 s of AUs** (`max_chunks` scales with fps, start 48) |
| soft prebuffer | **capped** (≤N frames / byte limit — never whole queue) |
| `hwdec-extra-frames` | **5** |
| SPS level patch | L5.2→L4.2 (disable `O3_H264_LEVEL_PATCH=0` if high-fps decode fails) |
| IPC | `/tmp/o3-mpv.sock` → `au_feed` / `vf_fps` / drops in journal |

### Adaptive FPS (goggles can change rate)

```text
O3_MPV_CONTAINER_FPS   # if set, locks rate (no adapt)
O3_MPV_FPS_ADAPT=1     # default on; 0 to disable
```

- Measure `au_feed` each second; snap to 24/25/30/50/60/100/120 when close.
- Stable ~2s different rate → log + **respawn mpv** with new override.
- SPS VUI logged only: `SPS VUI timing_info ≈ 60.0 fps (hint only; container=50…)`.

### 100–120 fps reality

| Goal | Status |
|---|---|
| Track goggles rate changes | ✅ adaptive |
| Stay **live** at 100–120 on 60 Hz panel | Design: `framedrop=vo` drops surplus |
| **See** 100–120 unique | Needs high-refresh monitor; current EDID is 1080p60-class |
| Pi4 decode 1080p100/120 | Unproven; may need `O3_H264_LEVEL_PATCH=0` |
| Does O3 USB live emit 100–120? | Unproven — watch `au_feed` after goggles change |

---

## Healthy journal pattern (seen ~13:45)

```text
[*] HDMI: SPS VUI timing_info ≈ 60.0 fps (hint only; container=50, will follow measured au_feed)
[*] HDMI: SPS level_idc patched → 4.2 (v4l2m2m)
[*] HDMI: soft prebuffer release (4 units, ~29 KiB)   # NOT hundreds of units
au_feed≈50/s  vf_fps=50.0  disp=60.0  framedrop=vo
resyncs=0  dec_drop=0
```

**Known residual (next session may polish):**

- Occasional `back-pressure — discarded 50 queued chunks` (~1s queue full) — better than 512, still a hitch.
- `mpv_drop` climbs (~30–40/s) while `vf_fps=50` — may be fine for live; eyes-on decides if chop remains.
- Formal owner verdict on lag + smoothness vs goggles/Fly still open.

---

## Never reintroduce

- `--container-fps-override=60` as fixed default while `au_feed≈50`
- Soft prebuffer dumping entire hand-off queue
- `max_chunks=512` (~10s bank)
- Deep cache ≥0.35s / 4 MiB
- `display-desync` / `display-vdrop`
- Dual HDMI without park checklist
- Stop service with goggles attached (gotcha #4)

---

## Env cheat sheet

| Env | Default | Role |
|---|---|---|
| `O3_MPV_CONTAINER_FPS` | (unset → 50 then adapt) | Lock content fps |
| `O3_MPV_FPS_ADAPT` | `1` | Auto retarget from `au_feed` |
| `O3_MPV_DISPLAY_FPS` | `60` | Panel |
| `O3_MPV_CACHE_SECS` | `0.12` | Jitter cushion (clamp ≤0.28) |
| `O3_MPV_MAX_BYTES` | ~768KiB@50 | Demuxer cap |
| `O3_MPV_FRAMEDROP` | `vo` | Live drop policy |
| `O3_MPV_HWDEC_EXTRA_FRAMES` | `5` | HW surfaces |
| `O3_H264_LEVEL_PATCH` | `1` | L5.2→L4.2 |
| `O3_HDMI_AU_COALESCE` | `1` | AUD whole frames |

---

## Deploy / ops

```bash
scripts/push_to_pi.sh
# UNPLUG goggles first
ssh pi@192.168.1.57 'sudo ~/o3test/scripts/o3_hdmi_safe_stop.sh --restart'
journalctl -u o3-hdmi -f
# Prefer IP if mDNS flaky: 192.168.1.57
```

---

## Pi state at close

| Item | Value |
|---|---|
| Host | `o3pi` / **`192.168.1.57`** |
| Time | 2026-07-14 ~13:50 BST |
| Service | **`o3-hdmi` enabled + active** (leave running) |
| Env | `O3_HDMI_PLAYER=mpv` `O3_DUAL_HDMI=0` `DRM_CONNECTOR=HDMI-A-2` |
| Code | fps50 + short buffer + adaptive + IPC **on Pi** |
| Monitors | A-2 connected edid=256; A-1 disconnected |
| UDC | may show `configured` (phantom or host — use safe_stop) |
| throttled | `0x0` |
| Dual | ⏸ PARKED |

---

## Next session priorities

1. **Owner eyes-on** of 50 fps + short buffer: live? smooth vs goggles/Fly? lag OK?  
2. If residual chop: look at `mpv_drop` rate + rare 50-chunk back-pressure (maybe queue 1.2s or cache 0.14 — not 0.35).  
3. Optional: drone-off marker test again — picture should die in **~1s**, not 10s+.  
4. Optional: switch goggles FPS and confirm `content fps adapt …` log.  
5. Backlog: console idle polish; GLSL only if metrics green; re-stream; arming trim; packaging.  
6. Dual only per `DUAL_HDMI_PARKED.md`.

---

## Files touched this session

- `poc/pi_endpoint/raw_endpoint.py` — main presentation + adapt + IPC + prebuffer cap  
- `docs/SESSION_CLOSE_2026-07-14_chop_15fps.md`  
- `docs/SESSION_CLOSE_2026-07-14_fps50_live.md`  
- `docs/SESSION_CLOSE_2026-07-14_night_fps50_adapt.md` (this)  
- `docs/GROK_STATE.md`  
- `CLAUDE.md`  

Evidence clips (repo root / workspace):

- `2026-07-14 12-08-27.mp4`, `DJI_0516.MOV` (15 fps twin)  
- `2026-07-14 13-12-47.mp4`, `DJI_0518.MOV` (slow-mo / buffer twin)  

---

## One-line handoff

Live USB is **50 fps**, not 60; forcing 60 caused 15 fps chop then slow-mo buffer. Defaults now **50 + framedrop=vo + short queue/cache + au_feed adaptive FPS**; **owner eyes-on A/B is #1** — do not re-hardcode 60 or deepen cache past 0.28s.
