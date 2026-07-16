# Session close — 2026-07-16 (later): air-unit hot-swap SOLVED (owner-confirmed)

## TL;DR — ✅ SOLVED
The P0 air-unit hot-swap fail (drone power-cycled with goggles still on the Pi → HDMI stays
black) is **fixed and owner-confirmed** ("big success, its excellent, tried it twice in a
row"). Two back-to-back drone power-cycles both recovered to smooth ~50 fps on their own, no
cable unplug / no service restart. It took **five stacked fixes** (video-silence re-arm →
gentle+settle cadence → deframer rebuild on re-arm → no-SIGTERM-of-live-presenter →
no-zombie-pump), each isolated by journal A/B across field tests #1–#5 (all detailed below).
Deployed on `o3pi`, committed to `main` (96e3e7d→87498e8).

## ⏭️ NEXT SESSION STARTS HERE
Hot-swap is done. The only remaining item from the 2026-07-16 plan is to **verify the three
event tools** (now unblocked). On `o3pi` with the monitor on HDMI-A-2 and the air unit live:
1. `~/o3test/scripts/event_health.sh --preflight` → want **GREEN** (exit 0).
2. Confirm `systemctl is-active o3-oops-watchdog` and no false reboots in normal use.
3. Plug goggles (air unit on) → `sudo ~/o3test/scripts/o3_soak.sh --duration 10m` while
   streaming → want `RESULT: PASS` (rows show ~50 fps, `dstate=0`, `throttled=0x0`).
   Then the full `sudo ~/o3test/scripts/o3_soak.sh` (2h) if the short one is clean.
Full checklist: `docs/SESSION_CLOSE_2026-07-16_event_ops_git.md`.
Optional cleanup (non-blocking): guard `notify_stream_restart` to skip the kill when the player
is already dead; remove the now-unused `_write_err_log_t` field; trim/keep the `who=` diag logs.

## Pi left-state at close
`o3-hdmi` **active**, `o3-oops-watchdog` **active**, `throttled=0x0`, goggles unplugged
(`udc` may read `configured` — that's the known phantom-stuck artifact, clears on next plug).
Git `main` clean. ⚠ Reminder: **unplug goggles before any `systemctl stop/restart o3-hdmi`**
(gotcha #4).

## Root cause (from the code, not a guess)
Live streaming state: AOA session in `accessory` mode, `configured=True`, bulk reader pulling
video, `_seed_handshake` thread sustaining.

- `_seed_handshake` arms USB video **once** with the full gold `APP_SEQ` burst, then loops
  only the **light** `SUSTAIN_SEQ` keepalives (heartbeats + version pings). This was
  deliberate — re-arming the full burst *every ~1s while streaming* caused the 2026-07-14
  chop (ES oscillating 8→0 Mbit/s).
- When the **air unit powers off** with the goggles still USB-plugged into the Pi:
  - The goggles keep the **AOA/USB link up** — they're still the USB host, still running
    logiclink, and our keepalive **TX keeps succeeding** (`_host_tx_ok_mono` stays fresh).
    So the phantom-unplug watchdog correctly does **not** fire (we must not tear down AOA).
  - But the goggles tear down their **camera/live-view session** (no drone) and **stop USB
    video-out**. ES stops. The atomic player stalls ~12s → exits → HDMI black. *Expected.*
- When the **air unit powers back on**: the goggles reacquire FPV internally, but **nothing
  re-subscribes USB video-out** on the still-live AOA session. The light `SUSTAIN_SEQ` alone
  does not re-arm the camera path — only the full `APP_SEQ` burst does. So ES never resumes →
  `feed()` is never called → player never respawns → **HDMI stays black.** Exactly the
  owner's report. The player/feed side was already correct; the gap was purely on the
  **arming** side.

This matches hypothesis **A** ("flat Mbit → re-arm/subscribe without full AOA unplug") from
`docs/SESSION_CLOSE_2026-07-16_event_ops_git.md`.

## The fix (`poc/pi_endpoint/raw_endpoint.py`)
A **video-silence re-arm** that re-fires the arming burst when USB video-out has gone silent
but the AOA host is still present.

- **Liveness signal:** `_write_video` stamps `_es_last_grow_mono` whenever `_es_written`
  increases (i.e. real ES is flowing).
- **Watchdog thread `video-resume-wd`** (in `main()`, 1 Hz) calls
  `AccessorySession.maybe_request_video_rearm()`, which sets `_rearm_video_event` when **all**
  of the following hold:
  - `configured` and `mode == accessory`, not mid AOA re-enum / phantom rearm;
  - video has flowed at least **once** this session (`_es_written > 0`);
  - ES silent ≥ `O3_VIDEO_SILENCE_REARM_S` (default **4s**);
  - the AOA host is still **alive** — recent keepalive TX **or** bulk RX (within
    silence+4s). If BOTH TX and RX are long-dead it's a real/phantom unplug → left to that
    path, re-arm does **not** fire.
  - rate-limited to once per `O3_VIDEO_REARM_PERIOD_S` (default **3s**) so a still-booting
    drone eventually catches without spamming.
- **Seed thread** services the event at the top of its sustain loop by replaying the full
  `APP_SEQ` via the new shared `_replay_arm()` helper (also used for the initial arm), then
  resumes light keepalives.

### Why this can't reintroduce the 2026-07-14 chop
That chop came from re-arming **while streaming**. This fires **only while there is no
video** (ES silent ≥4s), only after video has streamed once, and stops the instant ES
resumes (`_es_last_grow_mono` updates → silence resets → cadence cleared). While silent there
is nothing to chop.

### Knobs (env on the service; all optional)
| Env | Default | Meaning |
|---|---|---|
| `O3_VIDEO_REARM` | `1` | master enable; `0` disables the whole feature |
| `O3_VIDEO_SILENCE_REARM_S` | `4.0` | ES-silence seconds before first re-arm |
| `O3_VIDEO_REARM_PERIOD_S` | `3.0` | re-arm retry cadence while still silent |

## Deploy state (done this session)
- Code rsynced via `PI=pi@192.168.1.57 scripts/push_to_pi.sh`.
- Pi at deploy: `udc=not attached`, `configured=False`, `throttled=0x0`, goggles **unplugged**
  → safe to restart (gotcha #4 not in play).
- `sudo systemctl restart o3-hdmi` → **active**; monitor detected on **HDMI-A-2** (edid=256B);
  armed lazy; AST-clean on the Pi (`PI_AST_OK`), new symbols present.
- No unit-file or dependency changes → plain restart was sufficient (no `install_hdmi_service.sh`).

## Field test the owner must run (NOT yet done)
```bash
ssh pi@192.168.1.57
journalctl -u o3-hdmi -f
# Air unit ON, live HDMI good, goggles stay plugged into the Pi the whole time.
# 1) Power the drone OFF  → HDMI blanks (expected); service stays active.
# 2) Power the drone ON   → watch the journal.
```
**PASS (fix works) — expect this sequence after drone ON:**
```
[*] video silent 4s but AOA host alive (tx_ok=…) — requesting video re-arm (air-unit hot-swap)
[*] replaying video-resume re-arm burst #1 (… pkts)
[*] video-resume re-arm burst #1 done (…/… pkts)
[*] video chunks=… es_bytes=… ~X.XX Mbit/s …      ← ES back
[*] HDMI: ES aligned to SPS (… bytes from keyframe)
[*] HDMI player started: atomic-kms-present → HDMI-A-2
→ picture returns, NO cable unplug, NO systemctl restart
```
**Acceptance:** repeat **5× battery swaps**, all recover.

**If it FAILS a specific way — read the journal, it tells you which branch:**
- **`requesting video re-arm` never appears, and instead you see** `phantom unplug` /
  `event DISCONNECT` / `event SUSPEND` / TX failing → the goggles are dropping the AOA host
  on drone-off (not the silent-video case). Then the recovery belongs on the
  reconnect/disconnect path (goggles re-drive GET_PROTOCOL after our re-init), and the
  silence re-arm is a no-op by design. Capture that journal and hand it back.
- **`requesting video re-arm` fires + burst replays, but `Mbit/s` never resumes** → the
  goggles are ignoring the arming burst on a warm session; may need a specific
  camera/live-view SET or a full AOA re-enum. Capture and hand back.
- **ES resumes but no picture** → player/feed bug (should not happen; `feed()` lazy-respawn is
  unchanged). Capture the `HDMI player` lines.

## Then (unchanged from prior handoff)
Verify the three event tools per `docs/SESSION_CLOSE_2026-07-16_event_ops_git.md`:
`event_health.sh --preflight` GREEN, `o3-oops-watchdog` active/no false reboots,
`o3_soak.sh --duration 10m` PASS while streaming.

## Field test #1 result (2026-07-16) — re-arm CONFIRMED, but cadence destabilised the stream
Owner ran the power-cycle. Journal verdict:
- ✅ **The re-arm hypothesis is correct.** After a few `APP_SEQ` replays, USB video-out
  **resumed**: `~0.00` → `~13.05 Mbit/s` → `~7.70` etc. `SUSTAIN_SEQ` alone never
  re-subscribes; `APP_SEQ` does. Diagnosis A confirmed.
- ❌ **But HDMI stayed black** because the resumed stream was unstable and the atomic
  presenter thrashed: every spawn died `rc=-15` **during its cold-start** (none logged their
  `[atomic-drm] pipeline:` line → killed within <1s, before GStreamer finished importing).
  `garb=` exploded (16M→45M), ES came in ~10s on/off **bursts**, and after ~12 re-arms the
  goggles issued a USB **SUSPEND** → phantom unplug. Power was clean (`throttled=0x0`); the
  same presenter had just run `frames=518 ~50.5 fps` before drone-off, so the presenter is
  healthy — the **re-arm storm** was the problem. Re-arming every 3s while video was trying
  to come back kept resetting the goggles' encoder (the same failure class as the 2026-07-14
  "re-arm while streaming → ES 8→0" chop), so the presenter never got the ~8s of continuous
  stream it needs to lock.

### Refinement (deployed, pending field test #2)
Made the re-arm **gentle + self-limiting** in `maybe_request_video_rearm()`:
- Detection threshold 4s → **6s**; retry cadence 3s → **8s** (drone boot is slow anyway).
- **Settle latch:** once ES grows *after* a re-arm (`_es_last_grow_mono > _video_rearm_last_mono`),
  hold off re-arming for `O3_VIDEO_REARM_SETTLE_S` (**20s**) so the presenter can cold-start and
  lock undisturbed — only re-arm again if the stream stays truly dead for the full window.
  This stops us from churning the stream while it's coming back, which is what forced the SUSPEND.
- New knob `O3_VIDEO_REARM_SETTLE_S=20`.

Expected in field test #2: at most 1–2 gentle re-arms until the drone's video appears, then
**silence from us** while the presenter locks like a normal cold plug → picture returns.

## Field test #2 result (2026-07-16) — re-arm storm fixed, but stale deframer = corrupt resume
The gentle/settle-latched re-arm worked as designed: only **2** re-arms, and once video
resumed the settle latch held (no re-arm churn). But **HDMI still stayed black**, and the
journal exposed the *real* root cause:
- Cold plug (09:25:27) → **perfect**, `frames=518 ~50.5 fps`. Baseline good.
- Drone off → ES freezes at `es_bytes=10253783`. Re-arm #1/#2 → video **resumes** (09:26:00).
- **But** the resumed ES arrives in abnormal **13–20 Mbit/s bursts** (real is ~6–7),
  `garb=` explodes, `resyncs=0`, `au_feed` collapses to ~0.1/s, and the atomic presenter
  dies `rc=-15` **mid-cold-start** every ~7s (never logs its pipeline). After ~25s the
  goggles **SUSPEND** → phantom unplug.
- **Root cause:** the `StreamingVideoDeframer` is a long-lived session object. When the old
  stream cut off **mid-frame** (drone-off), it kept the partial frame; when video resumed it
  **spliced** the resumed bytes onto that stale partial → corrupt access units → decoder
  never locks. A cold plug works precisely because its deframer is brand-new. (This is why
  neither re-arm-cadence fix helped: the corruption is in framing, not cadence.)

### Fix #3 (deployed, pending field test #3)
On re-arm, **rebuild the deframer from scratch** before parsing the resumed stream, and
re-align the player:
- `maybe_request_video_rearm()` sets `_deframer_reset_evt` alongside the re-arm request.
- `_on_bulk_out()` (reader thread — no race) consumes it on the first bulk packet after
  re-arm: rebuilds `StreamingVideoDeframer`, clears `aoa_buf` / `_vid_pending` / `_vid_started`,
  and calls `HdmiPlayer.notify_stream_restart()`.
- `notify_stream_restart()` drops stale queued/held ES, forces `_need_sps`, and kills any
  running player so a fresh one respawns SPS-aligned on the clean stream.

Expected in field test #3: after drone-on, one/two re-arms → `hot-swap: deframer rebuilt for
resumed stream` → clean `~6–7 Mbit/s` (not 13–20 bursts) → presenter locks at ~50 fps →
picture back, no SUSPEND.

## Field test #3 result (2026-07-16) — deframer reset worked; found a player-kill bug
The deframer reset fixed the framing: after each re-arm the journal shows `hot-swap: deframer
rebuilt`, `garb` drops back to ~4k, and the resumed stream is now **clean ~5 Mbit/s** (no more
13–20 Mbit bursts). **But HDMI still black** — the presenter still thrashed. Root cause #3, now
unambiguous from the journal:
- Every warm-resume spawn logs `HDMI player pipe closed (rc=None)` → then `exited rc=-15`.
- `rc=None` at pipe-close means **the presenter was still ALIVE** when we killed it; `rc=-15`
  is our own `SIGTERM`. So `_pump` was **terminating a live, still-cold-starting presenter**
  on a transient stdin-write error. The atomic presenter installs its SIGTERM handler only
  *after* the ~5s `gi`/GStreamer import (`atomic_drm_present.py` main()), so a SIGTERM during
  that window kills it hard (`-15`) before it logs anything or decodes a frame — matching the
  "no `[atomic-drm]` line, dies <1s" signature. A cold plug survives because its first writes
  don't hit that transient.
- After ~22s of thrash the goggles gave up: real `DISCONNECT`, our re-arm's `rg.reinit()` hit
  `EBUSY`, process exited, systemd restarted clean.

### Fix #4 (deployed, pending field test #4)
In `HdmiPlayer._pump`, on a stdin-write failure, **check `poll()` first**:
- **Presenter alive (`rc is None`):** transient — do **not** terminate. Drop the stale queue,
  re-align to the next SPS, keep feeding the *same* live presenter so it can finish its ~5s
  cold-start and lock (like a cold plug). Rate-limited log with the captured exception.
- **Presenter actually exited (`rc` set):** respawn on next SPS as before.
`_write_stdin` now records the exception (`_last_write_err`) so test #4's journal shows the
exact transient if it recurs.

Expected in field test #4: after drone-on → clean ~5 Mbit → the (single) resumed presenter
**survives** its warm-up → `first frame` / `~50 fps` → picture back, no SUSPEND/DISCONNECT.

## Field test #4 result (2026-07-16) — VIDEO CAME BACK, but janky (~1.5 fps); zombie pump
Big milestone: **the picture returned** — the resumed presenter survived, logged `first frame`,
modeset, and decoded. But it ran at ~1.5 fps ("one frame then hold"). The diagnostic exception
revealed why: `ValueError('I/O operation on closed file')` — the player's **stdin was closed**
shortly after spawn (a `_kill_proc` set `self._proc = None`), but the old transient-retry
handler misread `self._proc.poll() → None` as "player still alive" and **spun forever, calling
`self._q.clear()` every 50 ms on the shared queue** → the real (new) player was starved → ~1.5
fps. So my fix #4's retry loop became a zombie that fought the live player.

### Fix #5 (deployed, pending field test #5)
- **Pump can't zombie:** `_pump` now captures its **own** process handle and exits immediately
  when `self._proc is not proc` (superseded/killed) — it never touches the shared queue for a
  player it no longer owns. On any write failure it exits cleanly and marks a respawn (no spin,
  no SIGTERM of a live cold-starting presenter).
- **Diagnostic:** every `_kill_proc` call is tagged (`who=spawn|hotplug|fps-adapt|stream-restart|
  idle:*|close`) and logs the player pid + alive state, so test #5's journal shows exactly which
  path is closing the freshly-spawned player's stdin (the remaining unknown).

Likely outcome: if the stdin-close was a one-time stray `_kill_proc` during cold-start, the
clean respawn now succeeds and the picture is smooth. If it recurs every spawn, the `who=` tag
names the culprit to fix next.

## ✅ Field test #5 result (2026-07-16) — SOLVED, owner-confirmed
Owner: **"big success, its excellent, tried it twice in a row."** Journal confirms two
back-to-back hot-swaps both recovered to smooth ~50 fps with no cable unplug / no restart:
- **10:13:17** (cycle 1): re-arm → `deframer rebuilt` → `HDMI player started` → `first frame`
  → `frames=260 ~49.7 fps` → `762 ~50.0 fps`, ES ~5–8 Mbit/s clean.
- **10:14:09** (cycle 2): re-arm #5 → player started → `~49.5 fps`, ES up to 35 Mbit.

The diagnostic also named the stdin-closer that fix #5 tamed:
`_kill_proc(who=stream-restart) closing player pid=… alive=True` — the re-arm's
`notify_stream_restart` kills the (stalled) player during drone-off; before fix #5 that kill
triggered the zombie-pump starvation. Now the pump exits cleanly and the next player locks at
full 50 fps.

**Net:** the P0 is solved with **five stacked fixes** (re-arm → gentle/settle cadence →
deframer rebuild → no-SIGTERM-live-presenter → no-zombie-pump), each isolated by journal A/B
across field tests #1–#5.

### Minor residual (benign, optional)
`_kill_proc(who=stream-restart)` still fires on the first re-arm after drone-off even though the
player has usually already stalled — harmless (releases the display a beat earlier). Could guard
`notify_stream_restart` to skip the kill when the player is already dead. The `who=` diagnostic
logging can stay (low volume, fires only on kills) or be trimmed. Unused `_write_err_log_t`
field can be removed on the next cleanup pass.

## Files touched
- `poc/pi_endpoint/raw_endpoint.py` — `_replay_arm()`, `maybe_request_video_rearm()`,
  `video-resume-wd` thread, ES-liveness stamp in `_write_video`, `__init__` + `rearm_for_next_plug`
  state.
- `CLAUDE.md` — CURRENT STATUS + Immediate next tasks #1.
