# Session close — 2026-07-14 chop / 15 fps presentation fix

## Diagnosis (locked)

Same-flight twins:

| Source | File | Unique motion |
|---|---|---|
| Goggles DVR | `DJI_0516.MOV` | **50 fps**, every frame unique, ~18 Mbit |
| Pi HDMI | `2026-07-14 12-08-27.mp4` (Cam Link direct) | **~14.4 unique fps** (1 new + ~3 holds @ 60) |

Alignment: `OBS_time = goggles_time + 18.5 s` (corr 0.95).  
**Monitor eyes-on matches ~15 fps** — not a Cam Link artifact.  
Lag: **OK / almost ideal** — leave cache at **0.18 s / 1.5 MiB**.  
Sharpness at matched moments: Pi ≈ **95%** of goggles edge energy → problem is **temporal**, not mush.

Pi journal for that flight: `resyncs=0 drop_tot=0 hdmi_drop=0`, polish on.  
→ USB/deframe fine; **mpv presentation under-displays**.

Working theory: `--framedrop=vo` discarded ~3/4 of frames under chronic “behind” timing with forced 60 + no-correct-pts.

---

## Shipped this session (deploy to Pi)

### Code (`poc/pi_endpoint/raw_endpoint.py`)

1. **Default `framedrop=no`** (was `vo`). Env: `O3_MPV_FRAMEDROP`.
2. **`hwdec-extra-frames` default 5** (was 3). Env: `O3_MPV_HWDEC_EXTRA_FRAMES`.
3. **Lag unchanged**: cache **0.18 s / 1.5 MiB**.
4. **mpv JSON IPC** at `/tmp/o3-mpv.sock` (`O3_MPV_IPC`).
5. **Stats every ~2 s**:
   ```
   [*] HDMI mpv: vf_fps=… au_feed=…/s drop=… dec_drop=… mistimed=… vo_delay=… disp=… framedrop=…
   ```
6. **1 Hz video line** appends: `au_feed=…/s vf_fps=… mpv_drop=… dec_drop=…`
7. Optional (not default): `O3_MPV_VIDEO_SYNC` (banned: display-desync, display-vdrop), `O3_MPV_LATENCY_HACKS=0`.

### How to read stats on next plug

| Pattern | Meaning | Next knob |
|---|---|---|
| `au_feed≈50–60` and `vf_fps≈50–60`, drops flat | **Fixed** | Lock it; eyes-on |
| `au_feed≈50–60` but `vf_fps≈15`, drops rising | mpv still dropping / pacing | try `O3_MPV_FRAMEDROP=decoder`; or `O3_MPV_VIDEO_SYNC=display-resample` |
| `au_feed≈15` | Feed/coalesce only ~15 AU/s | inspect AUD coalesce / ES rate (not framedrop) |
| lag worse, chop better | trade-off | leave or slightly lower cache **only if owner asks** |

---

## Field test (owner)

```bash
# Code already pushed; UNPLUG goggles first if attached
ssh pi@192.168.1.57 'sudo ~/o3test/scripts/o3_hdmi_safe_stop.sh --restart'
journalctl -u o3-hdmi -f
# Plug goggles, fly ~20s, watch for:
#   framedrop=no in cmd line
#   au_feed ~50-60/s
#   vf_fps ~50-60
#   mpv_drop not rocketing
```

Eyes-on: is chop gone / closer to goggles & DJI Fly? Lag still OK?

Revert if broken:

```bash
# on Pi unit env or one-shot:
O3_MPV_FRAMEDROP=vo O3_MPV_HWDEC_EXTRA_FRAMES=3
```

---

## Still locked / never reintroduce

- Cache ≥0.35 s / 4 MiB  
- `display-desync` / `display-vdrop`  
- Dual HDMI without park checklist  
- Stop service while goggles attached (gotcha #4)

---

## One-line handoff

Chop is real **~15 unique fps on HDMI** vs **50 on goggles DVR** same flight; shipped **framedrop=no + hwdec-extra=5 + IPC fps/drop stats**, lag cushion frozen — **owner plug + eyes-on is next**.
