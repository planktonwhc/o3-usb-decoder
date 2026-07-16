# Session close — 2026-07-16 (event-tool verification; watchdog reboot bug found + fixed)

## TL;DR
Owner at the bench to verify the three event tools (the remaining item after the hot-swap
fix). Result:
- ✅ **Video still works** — fresh boot → clean goggles plug → live HDMI at **~50.1 fps**,
  ES ~5.5 Mbit/s, `resyncs=0 badlen=0 hdmi_drop=0 tx_fail=0`. Core path healthy.
- ✅ **`event_health.sh --preflight`** — verified. Every hardware check GREEN (monitor A-2,
  atomic, avdec_h264, python-gi, power/thermal/D-state). Only YELLOW was the journal
  heuristic catching *stale* STALL lines from yesterday's hot-swap tests (self-clears once
  fresh streaming pushes them out of the 80-line window).
- ✅ **`o3-oops-watchdog` — DETECTION verified live.** A real `raw_gadget.ko` kernel oops
  happened this session and the watchdog **caught it in ~10s** and wrote the reboot
  breadcrumb (`/var/log/o3/last-oops-reboot.txt`). Exactly as designed.
- ⚠️→✅ **`o3-oops-watchdog` — REBOOT method was broken, now fixed.** The watchdog detected
  the wedge but its `systemctl --no-block reboot` queued a *graceful* reboot that hung ~90s
  behind the un-killable D-state task (only recovered via systemd's own job-timeout). Fixed
  to **force** the reboot (`systemctl reboot -ff` / `reboot -f`) with a **sysrq-b last
  resort** after 10s. Committed to `main`. **Not yet deployed to the Pi** (needs a restart
  with goggles unplugged).
- ⏳ **`o3_soak.sh` — not completed.** Launched during the live stream, but the **drone
  started overheating** and the owner unplugged everything ~30s in. Deferred to next
  cool-drone outing. Not a tool fault.

## The kernel oops (what happened, root cause)
Timeline:
1. The `o3-hdmi` endpoint had been running since **yesterday's** hot-swap tests and was
   sitting in a stale "re-arm for next plug" state (burst #6 had failed at 10:14, host went
   silent). It had NOT been restarted.
2. Owner plugged the goggles in (10:48:10 `event RESUME`). The **stale** process misread it
   as a phantom unplug ("re-arming for next plug (AOA from scratch)") and released the
   display → **HDMI stayed black.** This was the owner's "live but no video" report — a
   stale-process artifact, **not** a defect in the current code path.
3. The service was then **stopped while the goggles were still attached** (gotcha #4). That
   raced the gadget mid-resume and hit a genuine `raw_gadget.ko` NULL-deref:
   ```
   raw_event_queue_add+0x34 [raw_gadget]
   gadget_resume+0x34 [raw_gadget]
   dwc2_hsotg_udc_stop
   raw_release [raw_gadget]
   ```
   → main PID stuck in **D-state** holding the UDC; only a reboot recovers. This is the
   known/deferred kernel race noted in CLAUDE.md ("phantom-unplug re-arm can crash the
   kernel if it races real bus activity").
4. Recovery: forced reboot (sysrq/systemd job-timeout). After reboot the endpoint was
   **fresh** (PID 777) and the very next plug produced perfect ~50 fps video.

**Lesson reinforced:** to recover a stuck endpoint, **unplug the goggles first, then**
restart — never stop the service with goggles attached. And a long-idle stale endpoint can
misread a fresh plug; a clean restart (goggles out) or reboot fixes it.

## The fix (`scripts/o3_oops_watchdog.sh`, `do_reboot()`)
Replaced:
```bash
systemctl --no-block reboot || /sbin/reboot || reboot   # graceful → hangs on D-state task
```
with a forced sequence (system is wedged by definition when this fires, so a graceful
shutdown is exactly what cannot complete):
```bash
log "forcing reboot (system wedged — not waiting for unit shutdown)"
( sleep 10; echo 1 >/proc/sys/kernel/sysrq; echo b >/proc/sysrq-trigger ) &   # last resort
systemctl reboot -ff 2>/dev/null || reboot -f 2>/dev/null || /sbin/reboot -f
```
`sync` still runs first (line ~170). `reboot -ff` / `reboot -f` call `reboot(2)` directly
without waiting for unit shutdown; the backgrounded sysrq-b fires only if even those are
blocked. Expected recovery now ~15s (the watchdog's promise) instead of ~90s.

## State at close
- Pi rebooted clean this session; `o3-hdmi` + `o3-oops-watchdog` **active**, **no D-state**,
  goggles unplugged (`udc=configured` phantom artifact, harmless).
- `throttled=0x50000` observed after the forced reboot = **sticky** "under-voltage/throttle
  occurred" bits from the reboot power transient; current-active bits were clear (no live
  under-voltage). Worth a glance next session but not acted on.
- WiFi link to the Pi was **flaky** during teardown (ping jitter 9–277ms; SSH hung after
  KEX intermittently). Pi itself was fine (answered ping + KEX throughout). Likely cable
  movement / 2.4GHz congestion.

## Deploy status
- ✅ **Watchdog reboot fix DEPLOYED** (2026-07-16, goggles out): `push_to_pi.sh` (rsync) +
  `systemctl restart o3-oops-watchdog`. Verified on the Pi — forced-reboot code present,
  bash syntax OK, watchdog **active** with fresh heartbeat, `o3-hdmi` untouched/active.
- ⏳ **Prior cleanup commit** `selected_player()` default → atomic + dead-field removal
  (`528a07c`) — the file is now **on the Pi** (same rsync) but the running `o3-hdmi` (PID
  777) keeps its old in-memory code until the next restart. Behaviourally inert (unit pins
  atomic), so no action needed; it goes live on the next natural restart/reboot.

## NEXT SESSION PLAN — the one remaining verification: 5-min flight soak
The soak default was **retuned to a realistic single-battery flight** (owner: "max flight
time on most batteries = 5 min"). `scripts/o3_soak.sh` now defaults to **`--duration 5m
--interval 15`** (20 samples) with **`O3_SOAK_MAX_RESPAWNS=3`** (a clean continuous flight
should be ~0 respawns; the old 20 was budgeted for a 2h run). New `--max-respawns` flag added.
**Deployed to the Pi** (rsync, verified; no restart needed — soak is on-demand).

Run it next session when a **cool** drone is streaming continuously (one battery):
```bash
ssh pi@192.168.1.57
# air unit ON, goggles plugged, live HDMI good, then:
sudo ~/o3test/scripts/o3_soak.sh          # 5 min / 15s samples (defaults now)
# strict variant (require real video the whole flight):
sudo O3_SOAK_REQUIRE_STREAM=1 ~/o3test/scripts/o3_soak.sh
cat /var/log/o3/soak_*/summary.txt        # want: RESULT: PASS
```
**PASS =** `RESULT: PASS`, rows show `present_fps` ~50, `mbit_s` >0, `dstate=0`,
`throttled=0x0`, `service_restarts_delta ≤ 3`. That closes event-tool verification.
Optional after a clean 5m: a multi-battery `--duration 2h --max-respawns 20` (hot-swaps
count as respawns) for a full-event proof.

## NEXT SESSION TASK 2 — idle "waiting for live FPV feed" splash (owner request 2026-07-16)
Owner report: on a standard boot and whenever no live feed is showing, the **Linux command
line / console is on the HDMI** again. He recalls a change that kept the screen black when
idle; that's gone. **Wants:** instead of console *or* plain black, show a **large on-screen
message "waiting for live FPV feed"** while idle (boot → first plug, and any unplug/idle gap),
replaced by video the instant a feed arrives.

Context / why it's partly by-design (don't just "revert"):
- The idle console is **intentional** today: `HdmiPlayer._release_console()`
  (`poc/pi_endpoint/raw_endpoint.py:~1805`) re-binds `vtcon0/1/2` so the text console repaints
  on unplug — the comment says "we must not leave a black 'no console' screen idle." Called from
  `idle_until_video()` (~1868), the stall/unplug paths (~1253, ~1268, ~1799), and
  `notify_stream_restart()` (~1865). So the console-on-idle is deliberate, not a pure bug.
- The **boot-time** console (getty on tty1 before the first feed) is separate — a `quiet` /
  `logo.nologo` / `vt.global_cursor_default=0` / `consoleblank` in `cmdline.txt`, or a plymouth
  splash, may have been lost across this session's forced reboots/reflashes. Check
  `/boot/firmware/cmdline.txt` + `config.txt` vs. what the repo/install script expects.

Suggested approach (pick next session):
1. **Idle splash via the presenter/DRM:** instead of `_release_console()`, have the idle path
   paint a static "waiting for live FPV feed" frame onto the HDMI plane (atomic_drm_present can
   draw a solid-bg + centered-text dumb buffer, or pre-render a PNG → one page-flip) and *hold*
   it (keep DRM master) until the first decoded frame flips it away. Must still release cleanly
   for the stall-watchdog/​respawn logic so it can't wedge the display (see the 2026-07-14
   "never hold DRM master with nothing to show" rule — the splash is the *something* to show, so
   this is compatible, but the stall/exit path must still be able to reclaim it).
2. **Boot splash:** plymouth or a fbcon splash image + `quiet`/`logo.nologo` in cmdline so the
   console never flashes up pre-service; service then owns the plane with the idle splash.
- Wire it behind an env knob (e.g. `O3_IDLE_SPLASH=1`, `O3_IDLE_SPLASH_TEXT="waiting for live
  FPV feed"`) so it's easy to toggle/field-tune. Verify: cold boot shows splash (no console),
  unplug shows splash, plug → video within a few seconds, and the oops/stall watchdogs still
  recover (splash must not become a new "wedged black hold").
- Owner acceptance: no command line ever visible; large legible "waiting for live FPV feed"
  when idle; instant switch to video on feed.

## Also open (lower priority, next session)
- Deploy note: the `selected_player()`→atomic cleanup (`528a07c`) is on the Pi's disk but the
  running `o3-hdmi` still has old in-memory code; goes live on the next natural restart (inert
  meanwhile — unit pins atomic). No action required.
- Optional follow-up: make a long-idle **stale** endpoint re-arm cleanly on an eventual real
  plug (this session it misread the plug after ~20h idle → needed a restart/reboot). Deeper fix.
