# Session close — 2026-07-14 live is 50 fps + buffer bank fix

## What the second flight proved

Twins: `2026-07-14 13-12-47.mp4` (OBS) + `DJI_0518.MOV` (goggles).  
Owner: **smoother** after `framedrop=no`, but **slow-mo**, and after drone unplug
(manual ~1s black marker) **HDMI kept playing** → deep cache/queue, not live.

### Journal smoking guns (same session)

| Observation | Meaning |
|---|---|
| `au_feed≈50/s` steady | Live USB content rate is **50 fps**, not 60 |
| `vf_fps=60` + `container-fps-override=60` | mpv stamped frames as 60 → always "late" |
| `framedrop=no` | Never discarded late frames → **lag stack / slow-mo** |
| `back-pressure — discarded 512 queued chunks` (×3) | Our hand-off queue held **~10s** of AUs |
| `soft prebuffer release (273 units, 3971814 bytes)` | Prebuffer dumped **whole backlog** into mpv |
| ES `0.00 Mbit/s` for ~13s before DISCONNECT | mpv still draining banked frames (owner clue) |

Earlier 15 fps chop (`framedrop=vo` + wrong 60): chronic late-drop of a 50 fps stream.  
Goggles DVR 50 fps was always the true content rate.

SPS VUI “60” from offline gold is **not** what field AU rate says — **measure wins**.

---

## Fix deployed (this session)

| Knob | Old (flight 2) | New default |
|---|---|---|
| `--container-fps-override` | **60** | **50** (`O3_MPV_CONTAINER_FPS`) |
| `--display-fps-override` | 60 | **60** (panel) |
| `--framedrop` | no | **vo** (safe again with correct fps) |
| cache / readahead | 0.18s | **0.12s** |
| `demuxer-max-bytes` | 1.5 MiB | **768 KiB** |
| hand-off `max_chunks` | 512 (~10s) | **48** (~1s @ 50) |
| soft prebuffer | could dump entire queue | **cap at N frames / byte limit only** |

Env overrides unchanged in spirit: `O3_MPV_CACHE_SECS`, `O3_MPV_MAX_BYTES`,
`O3_MPV_FRAMEDROP`, `O3_MPV_CONTAINER_FPS`, `O3_MPV_DISPLAY_FPS`, IPC stats stay.

---

## Field test checklist

```bash
journalctl -u o3-hdmi -f
# plug, fly, then kill air unit / disconnect drone briefly
```

Expect:

```text
--container-fps-override=50 --framedrop=vo --demuxer-readahead-secs=0.120
au_feed≈50/s  vf_fps≈50  (not stuck at 60)
mpv_drop may tick under bursts — OK if eyes-on live
NO multi-hundred unit prebuffer dumps
NO 512-chunk back-pressure loops in steady hover
After drone off: HDMI dies within ~1s (not 10s+ play-out)
```

| Feel | Action |
|---|---|
| Live + smooth | **Lock** |
| Slight chop, live OK | `O3_MPV_CACHE_SECS=0.15` (cap 0.28) |
| Still lag / play-after-stop | lower cache 0.10; check `au_feed` vs `vf_fps` |
| Slow-mo again | verify container fps still 50 in cmd line |

---

## One-line handoff

Live USB is **50 fps**; forcing **60** banked the pipeline. Defaults now **50 + framedrop=vo + 0.12s/768KiB + 48-chunk queue + capped prebuffer** — owner plug test for live + no slow-mo.
