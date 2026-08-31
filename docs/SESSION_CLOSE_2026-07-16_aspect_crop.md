# Session close — 2026-07-16 4:3 → 16:9 center-crop for crowd HDMI

## Result

**✅ Owner-confirmed: works.** When the goggles send **4:3** (e.g. 1440×1080),
HDMI is now **full-bleed 16:9** on the monitor (center crop), not a pillarboxed
“square box.” Pilots keep native 4:3 in the goggles; crowd TVs fill the screen.

## Why

Event use-case split:

| Audience | Prefer |
|---|---|
| Pilot (goggles) | 4:3 FOV for flying |
| Crowd (HDMI monitor) | Full-screen 16:9, no side bars |

Previous presenter always **letterboxed** non-16:9 onto the fixed 1920×1080 DRM
canvas (`videoscale add-borders=true`, 2026-07-15 resolution crash-loop fix).
That kept full FOV but looked wrong on a 16:9 TV when the air unit / goggles
were in 4:3.

## What shipped

| Piece | Change |
|---|---|
| `poc/pi_endpoint/atomic_drm_present.py` | `O3_HDMI_ASPECT` → crop (default) or letterbox |
| `poc/pi_endpoint/o3-hdmi.service` | `Environment=O3_HDMI_ASPECT=crop` |
| Docs | README, FIELD_TEST_CARD, SUPPORTED_HARDWARE |

### Behavior

```
O3_HDMI_ASPECT=crop      (default)
  aspectratiocrop aspect-ratio=1920/1080 + videoscale + fixed canvas + PAR 1/1
  → true center-crop (top/bottom off 4:3), then *uniform* upscale
  → 1440×1080 (4:3) → crop to 1440×810 → 1920×1080
  → matches native goggles 16:9 FOV (no anamorphic warp)
  → true 16:9 sources unchanged (no crop, no bars)

O3_HDMI_ASPECT=letterbox
  videoscale add-borders=true → full FOV + black bars
```

Aliases: crop also accepts `fill` / `cover` / `zoom`; letterbox also accepts
`fit` / `pad` / `borders` / `pillarbox`.

Journal token when presenter starts:
```
[atomic-drm] pipeline aspect=crop: … aspectratiocrop aspect-ratio=1920/1080 ! videoscale …
```

**⚠️ 2026-07-17 geometry fix:** the first ship used `videoscale add-borders=false`,
which **stretches** 4:3 → 16:9 anamorphically (circle width/height ≈ 4/3) —
owner video showed warped corners vs native 16:9 mid-clip. Correct path is
`aspectratiocrop` then uniform scale. **Never** use `add-borders=false` for crop.

**PAR still forced 1/1** before crop/scale — do not remove (SPS
sample_aspect_ratio can otherwise re-crop unexpectedly; learned 2026-07-15).

## Deploy

```bash
scripts/push_to_pi.sh --restart
# unit reinstall also ran so Environment=O3_HDMI_ASPECT=crop is live:
# systemctl show o3-hdmi -p Environment | grep ASPECT
```

Owner eyes-on: **works**.

## Git

**`6b4aaed`** on local `main`:
`feat(hdmi): center-crop 4:3 feeds to full-bleed 16:9 on the monitor`

Working tree was clean after that commit; this session-close doc + status
updates commit separately if present.

## Pi leave-state (end of session)

| Item | Value |
|---|---|
| Host | `o3pi` / `192.168.1.57` |
| `o3-hdmi` | **active** |
| `o3-oops-watchdog` | **active** |
| Env | `O3_HDMI_ASPECT=crop`, dual=1, connector=auto, player=atomic |
| UDC | often **`configured` after unplug** (phantom — normal) |
| Probe | **SAFE_STOP** if no current `au_feed` (stale live ignored) |
| Journal | field test → phantom unplug / re-arm idle path |

Leave services running. Restart only via `scripts/restart_o3_hdmi.sh` — never
gate on `udc=configured` alone.

## Still open (next session)

1. **#1 `o3_soak.sh` 5m PASS** — cool drone continuous stream → `RESULT: PASS`
2. Dual soak / dual hot-swap eyes-on under unit default (optional polish)
3. Public export after monorepo fixes: `scripts/export_public_tree.sh` →
   `dr00min/o3-usb-decoder` (include this crop default if re-exporting)
4. Backlog: re-stream; arming trim; SD image; COLOR_ENCODING after master

## Related history

- 2026-07-15: letterbox path fixed ENOSPC crash-loop when coded size ≠ DRM mode
  (`docs/SESSION_CLOSE_2026-07-15_reliability_resolution_fix.md`)
- This session: same fixed canvas, default **fill/crop** for crowd display
