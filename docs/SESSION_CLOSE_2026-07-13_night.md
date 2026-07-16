# Session close — 2026-07-13 night (Grok)

Owner going to **field-test last 2 sessions**. Pi left ready.

---

## Last 2 sessions — what shipped

### Evening (field HDMI product)

- Continuous AOA → H.264 → HDMI, no phone; boot unit **`o3-hdmi`**
- **Single port:** mpv zero-copy + GLSL sharpen/deband (locked baseline)
- **Dual port:** GStreamer tee + dual kmssink; `DRM_CONNECTOR=auto` + hotplug
- In-process re-arm on goggles unplug/replug
- Owner earlier outdoor: core path works; picture “not ultra-impressive”

### Night (bitrate hunt — offline)

- `analysis/bitrate_hunt.py` over full gold MITM log
- Phone ES ~6.94 vs standalone ~6.91 Mbit/s → **same encode**
- **0** app→goggles HDLnk in working FPV; no post-arm quality SET
- CosmoStreamer public: Goggles 2 USB ~8–10 Mbps → **likely product ceiling**
- Docs: `protocol_notes.md` bitrate section; no Pi runtime change

---

## Pi state at close (2026-07-13 ~15:12 BST)

| Item | Value |
|---|---|
| Host | `o3pi.local` / `192.168.1.57` |
| Service | **`o3-hdmi` enabled + active**, waiting for goggles |
| Connector | `DRM_CONNECTOR=auto` — both A-1 and A-2 report **connected** at close |
| Player at start | gstreamer dual (because both ports look connected) |
| UDC | `not attached` (goggles unplugged) |
| Throttled | `0x0` |
| Deploy | Pi `raw_endpoint.py` matches laptop evening quality build |

**Leave service running.** Unplug goggles before any `systemctl stop` (gotcha #4).

Safe: power down air unit / goggles / monitors; Pi can stay on waiting.

---

## Field test

**Card:** `docs/FIELD_TEST_CARD.md` (checklist + pass/fail table).

Priority eyes-on:
1. First plug after boot
2. Single-port quality path (unplug one monitor if both are attached)
3. Dual if time
4. Reconnect re-arm
5. Subjective: soft picture OK given ~7 Mbit/s expectation

---

## Do not regress (still)

- `--profile=low-latency` / `--cache=no` / untimed+desync+nobuffer
- Tiny hand-off queue; CPU `--vf lavfi` “sharpen”
- Dual independent mpv
- Blind HDLnk quality spam without capture delta
- Stop service while goggles attached

---

## Next session (after field)

| Field result | Next work |
|---|---|
| All green | Network re-stream **or** minimal arming trim **or** open-source packaging |
| Dual/hotplug fail | Journal-driven fix; keep single-port path locked |
| Single-port breakup | A/B against locked baseline before changing player flags |
| Quality complaint only | Point at ceiling doc; optional UI-toggle MITM later if owner wants |

Read order next time: `CLAUDE.md` → this file → `FIELD_TEST_CARD.md` (filled) → `GROK_STATE.md`.

---

## One-line handoff

Field card ready; Pi `o3-hdmi` waiting; evening dual-HDMI+quality + night bitrate-ceiling
docs are the last two sessions — next work depends on field pass/fail.
