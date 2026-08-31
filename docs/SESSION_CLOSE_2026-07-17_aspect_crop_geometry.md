# Session close — 2026-07-17 true center-crop geometry (4:3 → 16:9)

## Result

**✅ Owner-confirmed: “looks identical to me, nice work.”**

When goggles send **4:3** (e.g. 1440×1080), HDMI full-bleed 16:9 now matches
**native goggles 16:9** framing — no corner warp. Owner A/B video
(`2026-07-17 09-39-30.mp4`) after the fix.

## Why it was wrong

2026-07-16 shipped `O3_HDMI_ASPECT=crop` using:

```
videoscale add-borders=false → fixed 1920×1080
```

That path was **documented as center-crop** but GStreamer **stretches** to fill
when `add-borders=false`. On 1440×1080 → 1920×1080 that is a pure horizontal
scale of **4/3** (anamorphic). Owner video (`2026-07-17 08-47-54.mp4`) showed
warped corners on 4:3 HDMI vs correct native 16:9 mid-clip.

Offline proof (circle on 1440×1080 test card → 1920×1080):

| Path | Circle width/height |
|---|---|
| `videoscale add-borders=false` (old) | **~1.34** (ellipse) |
| `aspectratiocrop` + uniform scale (new) | **~1.00** (circle) |
| letterbox (`add-borders=true`) | **~1.00** (circle + bars) |

## What shipped

| Piece | Change |
|---|---|
| `poc/pi_endpoint/atomic_drm_present.py` | crop → `aspectratiocrop` + `videoscale`; helper `_aspect_scale_chain` |
| `poc/pi_endpoint/o3-hdmi.service` | comments only (env still `O3_HDMI_ASPECT=crop`) |
| Docs | CLAUDE, README, FIELD_TEST_CARD, prior aspect-crop session close |

### Correct crop pipeline

```
O3_HDMI_ASPECT=crop      (default, unit)
  video/x-raw, pixel-aspect-ratio=1/1
  ! aspectratiocrop aspect-ratio=1920/1080
  ! videoscale
  ! video/x-raw, format=I420, width=1920, height=1080, pixel-aspect-ratio=1/1

  1440×1080 (4:3) → center-crop top/bottom → 1440×810 → uniform upscale → 1920×1080
  true 16:9 sources: no crop, no bars

O3_HDMI_ASPECT=letterbox
  videoscale add-borders=true → full FOV + black bars
```

**RULE: never use `videoscale add-borders=false` for “crop”** — that stretches.

Journal token:
```
[atomic-drm] pipeline aspect=crop: … aspectratiocrop aspect-ratio=1920/1080 ! videoscale …
```

## Deploy

```bash
scripts/push_to_pi.sh --restart
# Pi had aspectratiocrop (gstreamer1.0-plugins-good) already
```

Owner eyes-on after deploy: **identical** to native 16:9.

## Git

Local `main` only (no remote push this session). Feature + session-close docs
in this commit.

## Pi leave-state (end of session)

| Item | Value |
|---|---|
| Host | `o3pi` / `192.168.1.57` |
| `o3-hdmi` | **active** (leave running) |
| `o3-oops-watchdog` | **active** |
| Env | `O3_HDMI_ASPECT=crop`, dual=1, connector=auto, atomic |
| UDC | often `not attached` or phantom `configured` after unplug — normal |
| Probe | SAFE_STOP when idle; never gate restart on UDC alone |

Restart only via `scripts/restart_o3_hdmi.sh` / `push_to_pi.sh --restart`.

## Still open (next session)

1. **#1 `o3_soak.sh` 5m PASS** — cool drone continuous stream → `RESULT: PASS`
2. Dual soak / dual hot-swap eyes-on under unit default (optional)
3. Public export: `scripts/export_public_tree.sh` → `dr00min/o3-usb-decoder`
   (re-export picks up true center-crop)
4. Backlog: re-stream; arming trim; SD image; COLOR_ENCODING after master

## Related

- 2026-07-16: first crop ship (pillarbox → full-bleed) — `SESSION_CLOSE_2026-07-16_aspect_crop.md`
- 2026-07-15: letterbox path fixed ENOSPC when coded size ≠ DRM mode
- This session: geometry fix so crop matches native 16:9 FOV
