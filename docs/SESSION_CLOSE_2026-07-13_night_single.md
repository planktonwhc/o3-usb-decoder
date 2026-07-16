# Session close — 2026-07-13 night (single HDMI field restore)

Owner field-tested dual/live paths; dual left **parked**. Session ends on **single
HDMI far-port mpv** (owner: “all good”).

---

## For next session

Read in order:

1. `CLAUDE.md` (CURRENT STATUS)
2. **This file**
3. `docs/DUAL_HDMI_PARKED.md` (dual only if resuming dual — default **do not**)
4. `docs/SESSION_CLOSE_2026-07-13_field_hdmi.md` (original single recovery notes)
5. `docs/GROK_STATE.md`

### Next session goals (priority order)

1. **Keep single field path green** — full flight / multi-hour if possible  
2. Network re-stream (Pi → LAN)  
3. Arming-set trim (`gold_app_seq` minimal subset)  
4. Open-source packaging  
5. Dual **only** after checklist in `DUAL_HDMI_PARKED.md` (not default work)

---

## Wins this session

| Item | Result |
|---|---|
| AOA → H.264 continuous | ✅ still works |
| Single HDMI far port | ✅ owner “all good” after restore |
| Dual offline full-frame | ✅ earlier (parked for live) |
| Dual live | ❌ lag / slow-mo / crash-loop / black after HDMI hotplug |
| Phantom EDID filter | ✅ usable = EDID ≥128 B |
| SPS level patch + v4l2 dual decode | partial offline; live dual not stable |
| Field revert | ✅ dual off, pin `HDMI-A-2`, stable mpv flags |

---

## Working field config (LOCKED — do not regress)

```text
O3_HDMI_PLAYER=mpv
O3_DUAL_HDMI=0
DRM_CONNECTOR=HDMI-A-2          # far micro-HDMI only
# leave near HDMI-A-1 unplugged
```

**mpv (in `default_player_cmd`):**

- `--vo=gpu --gpu-context=drm --hwdec=v4l2m2m`
- `--cache=yes --demuxer-readahead-secs=0.15 --cache-secs=0.15`
- `--demuxer-max-bytes=1572864` (integer — never default 150 MiB)
- `--no-correct-pts --container-fps-override=60`
- `--framedrop=vo --video-latency-hacks=yes`
- Lazy start on first SPS; re-align after respawn

**Never on live mpv:**

- `--profile=low-latency` / `--cache=no`
- `--video-sync=display-desync` or `display-vdrop` (crash-loop / slow-mo)
- `--untimed` + desync + lavf `nobuffer`

**Unit:** `poc/pi_endpoint/o3-hdmi.service`  
**Code:** `poc/pi_endpoint/raw_endpoint.py`  
**Wrapper:** `scripts/o3_hdmi_run.sh`

---

## Dual (parked — summary)

Full write-up: **`docs/DUAL_HDMI_PARKED.md`**.

- Offline: `dual_drm_mirror.py` both full-frame with EDID 1080p  
- Live: not field-stable; do **not** set `O3_DUAL_HDMI=1` in the boot service  
- Dead ends: dual-mpv, dual kmssink tee, fbdev crop, non-EDID modeset  

---

## Field narrative (compressed)

1. Live dual / dual-ish paths → lag, slow-mo, bad image (openh264 CPU; mpv crash-loop;
   phantom second connector; DRM orphan thrash).  
2. Goggles not fully seated → console both ports until USB attach.  
3. Revert: dual off, pin far **HDMI-A-2**, restore field mpv baseline.  
4. Owner: **all good**.

---

## Pi state at close

| Item | Value |
|---|---|
| Host | `o3pi.local` / `192.168.1.57`, user `pi` |
| Service | **`o3-hdmi` enabled + active** (leave running) |
| Env | `O3_HDMI_PLAYER=mpv` `O3_DUAL_HDMI=0` `DRM_CONNECTOR=HDMI-A-2` |
| Player | **mpv → HDMI-A-2** (lazy SPS) |
| Monitors | A-2 connected edid=256; A-1 disconnected |
| Goggles | UDC was **`configured`** at close snapshot — may still be plugged |
| throttled | `0x0` |
| Deploy | `scripts/push_to_pi.sh` then unplug → `install_hdmi_service.sh` |

**Gotcha #4:** unplug goggles **before** `systemctl stop/restart o3-hdmi`.  
**Stuck UDC:** unplugged but `state=configured` → force kill + start (owner confirm unplug).

---

## Do not relitigate / do not regress

- Re-enable dual in `o3-hdmi.service` without park checklist  
- Dual-mpv / dual kmssink tee / fbdev “quality” dual  
- Modeset non-EDID modes (esp. A-1 1024×768)  
- `display-desync` / `display-vdrop` / low-latency profile on live mpv  
- Stop service while goggles attached  
- Trust UDC `configured` without owner confirm / no-host check  

---

## After single stays green

- Network re-stream  
- Arming-set trim  
- Packaging / public repo polish  
- Dual only via `DUAL_HDMI_PARKED.md` checklist  
