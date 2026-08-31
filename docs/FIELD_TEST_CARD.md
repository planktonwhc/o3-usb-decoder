# Field test card — shipping path (2026-07-15+)

Print / keep on phone. **Shipping player is atomic-KMS** (`O3_HDMI_PLAYER=atomic`),
not mpv. Dual HDMI is the **default** — one monitor on **HDMI-A-2** (far) is enough;
both ports show the same feed when plugged.

## What you are validating

| # | Check |
|---|---|
| 1 | Boot → service waits → plug goggles → live video, no phone |
| 2 | Smooth ~50 fps feel (no chop / no slow-mo) |
| 3 | Correct color (no green OSD bar, no cyan edge ghosting) |
| 4 | 4:3 sources center-crop to 16:9 (`O3_HDMI_ASPECT=crop`); no anamorphic stretch. `letterbox` = full FOV + bars |
| 5 | Unplug goggles → console returns / service stays up → replug → video returns |
| 6 | Quality expectation: soft ~7 Mbit/s 1080p-class ES is normal (not DVR-sharp) |
| 7 | Drone/air-unit off → picture dies quickly (~1s), does not keep playing buffered past |

---

## Pre-flight

1. Pi powered from **GPIO / PoE** (not USB-C data port).
2. Goggles→Pi USB-C is **VBUS-isolated**.
3. Solid 5 V supply (`vcgencmd get_throttled` → `throttled=0x0`).
4. Monitor on **HDMI-A-2** (far) and/or HDMI-A-1. Dual is default.
5. Preflight (preferred):
   ```bash
   ssh pi@o3pi.local '~/o3test/scripts/event_health.sh --preflight'
   # want: verdict GREEN (exit 0). YELLOW = usable with care; RED = fix first.
   # Also confirm: o3-oops-watchdog active (auto-reboot on kernel wedge).
   ```
6. **Do not** `systemctl stop/restart o3-hdmi` with goggles still streaming (**gotcha #4**).
   Prefer: `sudo ~/o3test/scripts/o3_hdmi_safe_stop.sh --restart` (runs host probe;
   refuses only on live `au_feed`/ES). **`udc=configured` alone ≠ goggles plugged**
   (phantom after unplug is common — do not block restart on that).
   Check: `~/o3test/scripts/o3_host_probe.sh`

---

## Run procedure

```
1. Pi on (service auto-starts, waits for goggles — console should show)
2. Air unit ON, live video in goggles
3. Plug goggles → Pi USB-C (isolated)
4. HDMI should show FPV within a few seconds
5. Fly / walk high-contrast content (TV, bright edges) if checking chroma
6. Done: unplug goggles first; leave Pi running or power down cleanly
```

Logs:
```bash
journalctl -u o3-hdmi -b --no-pager   # this boot
journalctl -u o3-hdmi -f              # live tail
```

Healthy live-ish signals: AOA/configure, arm/sustain, player spawn, steady ES rate,
presenter frame cadence ~50/s, no tight crash-loop of `drmModeSetCrtc failed`.

---

## Pass / fail notes (fill in field)

| Check | OK? | Notes |
|---|---|---|
| 1 First plug → video | | |
| 2 Smoothness | | |
| 3 Color / chroma | | |
| 4 Aspect (crop 16:9 / no stretch) | | |
| 5 Unplug / replug re-arm | | |
| 6 Quality vs ~7 Mb expectation | | |
| 7 Air-unit off → dies ~1s | | |
| Black screen / console stuck? | | |
| Reboot required? | | |

---

## If something fails

| Symptom | Do |
|---|---|
| No video after plug | Confirm air unit live **in goggles** first; `journalctl -u o3-hdmi -n 80` |
| Console stuck forever | Check journal for `SetCrtc` / ENOSPC / crash-loop; if `python3` in **D-state** + UDC broken → **full reboot** (kernel oops; restart will not help) |
| Green bar / cyan ghosts | Should be fixed on atomic+`avdec_h264`; confirm drop-in/env is atomic not old mpv/HW path |
| 4:3 looks pillarboxed | Default is true center-crop to 16:9 (`O3_HDMI_ASPECT=crop`, matches native 16:9 FOV). For full FOV + bars set `letterbox` |
| 4:3 looks stretched/warped at corners | Bug fixed 2026-07-17: was anamorphic stretch via `add-borders=false`. Need `aspectratiocrop` path — redeploy atomic presenter |
| Service dead | Unplug goggles → `sudo systemctl start o3-hdmi` |
| After any stop with goggles plugged | May need **Pi reboot** (gotcha #4) |
| `throttled != 0x0` | Better power supply before blaming software |

### Quick health dump (SSH)

```bash
~/o3test/scripts/event_health.sh --preflight
# or manual:
systemctl is-active o3-hdmi o3-oops-watchdog
vcgencmd get_throttled
cat /sys/class/udc/*/state 2>/dev/null
ps -o pid,stat,cmd -C python3
journalctl -u o3-hdmi -n 60 --no-pager
dmesg | tail -40
```

If a `python3` process shows **`D`** (uninterruptible): **reboot** (or wait ~8–15s for
`o3-oops-watchdog` to auto-reboot). Do not thrash `systemctl restart`.
Breadcrumb after auto-reboot: `/var/log/o3/last-oops-reboot.txt`.

---

## After field

1. Paste pass/fail into chat or a new `docs/SESSION_*` note.
2. If all green for events → work `docs/EVENT_PRODUCTION_QA.md` items (watchdog, soak, packaging).
3. Dual HDMI only if needed — follow `docs/DUAL_HDMI_PARKED.md`, do not re-enable casually.
